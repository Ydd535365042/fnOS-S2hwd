(function () {
  "use strict";

  /* ---------- 本机偏好存取 ----------
     localStorage 不是「总是可用」：隐私模式、禁用站点数据、被策略拦的跨源 iframe
     都会直接抛 SecurityError。裸调用会让整个 IIFE 在初始化阶段就炸掉 ——
     页面看着还在，但后面几百行绑定一条都不会执行，所有按钮全是死的。
     这里统一兜一层：存不进去就退化成「本次会话内有效」，绝不让存储失败连累功能。 */
  const store = (() => {
    const mem = {};
    let ok = true;
    try {
      localStorage.setItem("s2-probe", "1");
      localStorage.removeItem("s2-probe");
    } catch (e) {
      ok = false;
    }
    return {
      get(k) {
        if (ok) {
          try { return localStorage.getItem(k); } catch (e) { ok = false; }
        }
        return Object.prototype.hasOwnProperty.call(mem, k) ? mem[k] : null;
      },
      set(k, v) {
        mem[k] = v;
        if (ok) {
          try { localStorage.setItem(k, v); } catch (e) { ok = false; }
        }
      },
    };
  })();

  /* 按来源持久保存随机会话，只通过显式请求头发送。 */
  let SESSION = window.S2Auth.get();
  const authHeaders = () => { SESSION = window.S2Auth.get(); return SESSION ? { Authorization: "Bearer " + SESSION } : {}; };
  async function fetchAPI(path, options = {}, timeout = 20000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const response = await fetch(path, { ...options, cache: "no-store", credentials: "omit", signal: controller.signal });
      let data;
      try { data = await response.json(); }
      catch (e) {
        if (e.name === "AbortError") throw e;
        throw new Error("设备返回的数据无法识别");
      }
      if (!data || typeof data !== "object" || Array.isArray(data) || typeof data.ok !== "boolean") {
        throw new Error("设备返回的数据无法识别");
      }
      return { response, data };
    } finally {
      clearTimeout(timer);
    }
  }

  /* 评审用 ?demo=empty：看脚本库 / 模块 / 服务三处空态。
     必须在 applyState() 里也认这个标记：初始化清空之后紧跟着就是第一次
     refreshState()，10 秒后还有轮询 —— 只在初始化清一次，页面会在 1 秒内
     被真实数据填回去，这个入口等于从来没有生效过（审查时实测过）。 */
  const DEMO_EMPTY = new URLSearchParams(location.search).get("demo") === "empty";

  /* ---------- §4：界面只用中文 ----------
     浏览器 / HTTP 层的报错天生是英文（"Failed to fetch"、"HTTP 404"……），这些句子
     只能走 console.error，绝不能原样流进 toast 或 S2.lastErr——那两处都是用户会看到的。
     这里统一翻一遍，和 HW_SPEC.md §4 最后一张表逐条对应；翻不出来的 HTTP 状态码
     退到「设备内部错误」，不是把数字甩出来装句子。 */
  function httpErrText(status) {
    const map = { 401: "需要访问口令", 403: "没有权限", 404: "接口不存在（后端版本过旧）", 413: "文件过大",
                  429: "口令错误次数太多，请 5 分钟后再试" };
    if (map[status]) return map[status];
    if (status >= 500) return "设备内部错误";
    return "设备内部错误";
  }
  // fetch() 的 reject 理由（网络层）翻成中文；认不出的网络错误也归到「无法连接设备」，
  // 不把 e.message 的英文原文递给用户——原文仍然进 console.error，排障用得上
  function netErrText(e) {
    const m = String((e && e.message) || e || "");
    if (/abort|timeout/i.test(m)) return "设备响应超时";
    return "无法连接设备";
  }

  /* 401 = 会话已撤销或口令已变更。只发起一次登录跳转。 */
  let loginRedirecting = false;
  function authExpired() {
    if (loginRedirecting) return;
    loginRedirecting = true;
    SESSION = "";
    window.S2Auth.clear();
    location.replace("/login");
  }

  async function apiGet(path) {
    let result;
    try { result = await fetchAPI(path, { headers: authHeaders() }); }
    catch (e) { throw new Error(e.message === "设备返回的数据无法识别" ? e.message : netErrText(e)); }
    const { response, data } = result;
    if (response.status === 401) authExpired();
    // 服务器给了明确原因（脚本不存在、参数错误…）就用它，别一律换成「接口不存在」
    if (!response.ok) throw new Error(data && typeof data.msg === "string" && data.msg ? data.msg : httpErrText(response.status));
    return data;
  }

  // 写请求不自动重试，避免超时后重复执行硬件操作或脚本。
  async function apiPost(path, obj, raw, timeout = 60000) {
    const opt = { method: "POST", headers: { ...authHeaders(), "X-S2-Request": "1" } };
    opt.headers["Content-Type"] = raw !== undefined ? "application/octet-stream" : "application/json";
    opt.body = raw !== undefined ? raw : JSON.stringify(obj || {});
    try {
      const { response, data } = await fetchAPI(path, opt, timeout);
      if (response.status === 401) authExpired();
      if (!response.ok) {
        data.ok = false;
        if (typeof data.msg !== "string") data.msg = httpErrText(response.status);
      }
      if (data.detail) console.error("[S2] 接口 detail:", path, data.detail);
      return data;
    } catch (e) {
      return { ok: false, msg: e.message === "设备返回的数据无法识别" ? e.message : netErrText(e) };
    }
  }

  /* 硬件操作统一走 /api/hw。返回 {ok, msg, data}，失败时 msg 就是给人看的那句话。 */
  const hw = (op, params) => {
    if (S2.access && S2.access.danger === false && !["status", "hwinfo", "logs"].includes(op)) {
      return Promise.resolve({ ok: false, msg: "请先在飞牛「应用设置 → 配置」设置访问口令" });
    }
    return apiPost("/api/hw", { op: op, params: params || {} }, undefined, op === "patch" ? 200000 : 60000);
  };

  /* 全局状态。state 是 /api/state 的完整快照（含配置、脚本、模块），
     sensor 是 /api/sensor 的实时快照，hwinfo 是 /api/hwinfo 的慢变静态信息
     （型号、序列号、SMART……60 秒一拉，见下面「硬件静态信息」一节）。三者分开：
     state 最重，sensor 轻而快，hwinfo 慢而几乎不变。 */
  const S2 = { state: null, sensor: null, hwinfo: null, online: false, lastErr: "" };

  /* ---------- §6 采样间隔（可自定义，默认 10 秒） ----------
     只影响 /api/sensor 这一条轮询（仪表盘、趋势图、硬件卡、服务状态跟着一起变）；
     /api/state 60 秒、/api/hwinfo 120 秒是各自独立的节奏，不受这个设置影响。
     存 localStorage（经 store 包装，存不进去就退化成本次会话有效），非法值一律按 10 处理。 */
  const POLL_KEY = "s2-poll-sec";
  // 1 = 「实时」：每秒一次。/api/sensor 只读 /proc /sys、不起进程，设备上一次几毫秒，每秒拉也不费事
  const POLL_CHOICES = [1, 3, 5, 10, 30, 60];
  function loadPollSec() {
    const n = parseInt(store.get(POLL_KEY), 10);
    return POLL_CHOICES.indexOf(n) >= 0 ? n : 10;
  }
  let pollSec = loadPollSec();   // 当前采样间隔（秒），由 #poll-seg 的按钮改写，经 store 持久化

  /* ---------- 主题 ---------- */
  const root = document.documentElement;
  const THEME_KEY = "s2-proto-theme";        // 存的是「偏好」：light / dark / auto
  const darkQuery = matchMedia("(prefers-color-scheme: dark)");
  const themeBtn = document.getElementById("btn-theme");
  const themeIcon = document.getElementById("theme-icon");

  // 跟随系统必须存 "auto" 而不是解析后的结果，否则刷新一次就退化成固定主题，
  // 系统换了配色也不再跟随
  function themePref() {
    const v = store.get(THEME_KEY);
    return v === "light" || v === "dark" || v === "auto" ? v : "auto";
  }
  const resolveTheme = (pref) =>
    pref === "auto" ? (darkQuery.matches ? "dark" : "light") : pref;

  /* 主题按钮三态循环：浅色 → 深色 → 跟随系统。
     原来「跟随系统」是设置区那张外观卡里的一个按钮。那张卡撤掉之后，如果这里只做
     两态切换，点过一次就再也回不到跟随系统 —— 删掉一个界面，不能顺手删掉它唯一的能力。
     图标画的是「现在是什么」，不是「点了会变成什么」：深色主题下看到的就是月亮。 */
  const THEME_CYCLE = ["light", "dark", "auto"];
  const THEME_ICON = { light: "i-sun", dark: "i-moon", auto: "i-auto" };
  const THEME_LABEL = {
    light: "当前浅色主题，点击切换到深色",
    dark: "当前深色主题，点击切换到跟随系统",
    auto: "当前跟随系统，点击切换到浅色",
  };

  // redraw=false 供首次调用：此时 chart 等常量还在 TDZ 里，不能画图
  function applyTheme(pref, redraw) {
    const t = resolveTheme(pref);
    root.dataset.theme = t;
    themeIcon.innerHTML = '<use href="#' + THEME_ICON[pref] + '"/>';
    themeBtn.setAttribute("aria-label", THEME_LABEL[pref]);
    themeBtn.title = THEME_LABEL[pref];
    if (redraw !== false && chartVisible()) drawChart(true);
  }
  applyTheme(themePref(), false);
  // 系统配色变化时，只有「跟随系统」才需要跟
  const onSchemeChange = () => {
    if (themePref() === "auto") applyTheme("auto");
  };
  if (darkQuery.addEventListener) darkQuery.addEventListener("change", onSchemeChange);
  else if (darkQuery.addListener) darkQuery.addListener(onSchemeChange);

  themeBtn.addEventListener("click", () => {
    const next = THEME_CYCLE[(THEME_CYCLE.indexOf(themePref()) + 1) % THEME_CYCLE.length];
    store.set(THEME_KEY, next);
    applyTheme(next);
    toast(next === "auto" ? "已跟随系统主题" : "已切换到" + (next === "dark" ? "深色" : "浅色") + "主题");
  });

  /* ---------- 提示条 ---------- */
  let toastTimer = null;
  function toast(msg, kind) {
    const el = document.getElementById("toast");
    document.getElementById("toast-msg").textContent = msg;
    // 用 classList 而不是整体赋值 className：以后给 toast 加类时不会被一次覆盖清掉
    el.classList.remove("ok", "err");
    el.classList.add("on", kind || "ok");
    el.querySelector("use").setAttribute("href", kind === "err" ? "#i-alert" : "#i-check");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("on"), 2200);
  }

  /* ---------- 导航 ---------- */
  const navItems = [...document.querySelectorAll(".nav-item[data-view]")];
  const views = [...document.querySelectorAll(".view")];

  // 页面标题就是导航里那颗点亮的按钮，不再另起一个 h1 ——
  // 顶栏和正文各写一遍「概览」，等于把同一个信息说两次
  // 视图切进来之后要补算的东西。计划页的标签需要量实际宽度才能判断会不会叠字，
  // 风扇曲线要按实际宽度重画，而隐藏状态下 offsetWidth / clientWidth 都是 0 ——
  // 所以必须等它真的显示出来再算一次。
  // 这里必须是「一组回调」而不是「一个回调」：早先写成单个变量，计划页那段
  // 后执行直接把它覆盖掉了，硬件页的重画从此再也没被调用过。
  const viewHooks = [];
  function onViewShown(fn) { viewHooks.push(fn); }

  let currentView = "overview";
  // 每个页各记一份滚动位置：概览看到一半去控制页跑个脚本，切回来还停在原处。
  // 只有刷新页面才从顶部开始（浏览器自己的滚动恢复关掉，免得刷新后停在半截）。
  const viewScroll = {};
  if ("scrollRestoration" in history) history.scrollRestoration = "manual";
  function go(view) {
    // 顶栏和手机底栏各有一组 .nav-item，同一个 data-view 出现两次：
    // 只点亮 find() 找到的第一颗，底栏就永远停在「概览」上。按 view 名把两组一起点亮。
    const target = navItems.some((n) => n.dataset.view === view) ? view : navItems[0].dataset.view;
    const entering = target !== currentView;
    if (entering) viewScroll[currentView] = window.scrollY || document.documentElement.scrollTop || 0;
    currentView = target;
    navItems.forEach((n) => {
      if (n.dataset.view === target) n.setAttribute("aria-current", "page");
      else n.removeAttribute("aria-current");
    });
    views.forEach((v) => v.classList.toggle("is-active", v.dataset.view === target));
    // 切页时把顶栏放出来：刚切过去用户要看自己在哪一页
    showTopbar();
    // 切页回到这一页上次停留的位置（第一次进来是 0）。原地再点当前页不动。
    // 用两参数 scrollTo：behavior:"instant" 不是标准值，旧版 Safari/Chrome 会忽略整个调用。
    // 新页刚从 display:none 变成可见，内容高度要到下一帧才算准，所以等一帧再滚。
    if (entering) {
      const y = viewScroll[target] || 0;
      window.scrollTo(0, y);
      // 这次跳转不是用户在滑：把方向判断的基线挪过去，否则跳到半截会被当成「下滑」把顶栏收起来
      lastY = y;
      requestAnimationFrame(() => {
        if (Math.abs((window.scrollY || 0) - y) > 2) window.scrollTo(0, y);
        lastY = window.scrollY || 0;
        showTopbar();
      });
    }
    // 隐藏的视图不渲染，切进来才补一次：既然之前被跳过的轮询可能已经推了新样本
    // 但没人画，进来必须强制重画一次，不能等下一次「有新样本」才补上
    if (target === "overview") {
      flushHwCardsIfStale();
      requestAnimationFrame(() => drawChart(true));
    }
    // 从别的页切进概览：仪表盘重播一次自检扫动画（跟 JK 一样，每次进入页面都先自检）。
    // 已经在概览上再点一次「概览」不重播，免得手一抖就转一圈。
    // 等 is-active 生效后的下一帧再播，隐藏状态下播放的那几帧用户看不见。
    if (target === "overview" && entering) requestAnimationFrame(replayGaugeBoot);
    requestAnimationFrame(() => viewHooks.forEach((fn) => fn()));
  }
  navItems.forEach((n) => n.addEventListener("click", () => go(n.dataset.view)));

  /* ---------- 手机端：下滑收起顶栏，上滑放出 ----------
     只在窄屏（底栏出现的那个断点）生效：桌面上顶栏只有 56px，收起来反而让人找不到刷新和主题。
     判方向用累计位移而不是单次 delta：iOS 惯性滚动会发很多 1–2px 的回弹事件，
     逐次判断的话顶栏会在滚动末尾抖几下。一帧最多处理一次，passive 监听不挡滚动。 */
  const topbarEl = document.querySelector(".topbar");
  const narrowQ = matchMedia("(max-width:620px)");
  const HIDE_AFTER = 56;      // 滚过顶栏自身高度之后才允许收起，页首永远看得见
  const DIR_SLOP = 12;        // 同方向累计超过 12px 才算真的换了方向
  let lastY = 0, accum = 0, scrollRaf = 0;
  function showTopbar() { if (topbarEl) topbarEl.classList.remove("is-hidden"); accum = 0; }
  function onScrollDir() {
    scrollRaf = 0;
    if (!topbarEl) return;
    const y = Math.max(0, window.scrollY || document.documentElement.scrollTop || 0);
    const dy = y - lastY;
    lastY = y;
    if (!narrowQ.matches || y < HIDE_AFTER) { showTopbar(); return; }
    // 焦点在顶栏里（键盘用户 Tab 到了刷新按钮）时不收，否则焦点落在看不见的地方
    if (topbarEl.contains(document.activeElement)) { showTopbar(); return; }
    if ((dy > 0) !== (accum > 0)) accum = 0;   // 方向变了，从零重新累计
    accum += dy;
    if (accum > DIR_SLOP) topbarEl.classList.add("is-hidden");
    else if (accum < -DIR_SLOP) showTopbar();
  }
  window.addEventListener("scroll", () => { if (!scrollRaf) scrollRaf = requestAnimationFrame(onScrollDir); }, { passive: true });
  // 从窄屏转到宽屏（横竖屏切换、拖窗口）时顶栏必须回来
  const onNarrowChange = () => { if (!narrowQ.matches) showTopbar(); };
  if (narrowQ.addEventListener) narrowQ.addEventListener("change", onNarrowChange);
  else if (narrowQ.addListener) narrowQ.addListener(onNarrowChange);
  if (topbarEl) topbarEl.addEventListener("focusin", showTopbar);
  document.addEventListener("keydown", (e) => {
    // 输入框里按数字是打字，不是切页。这里不用 e.target.matches 直接调：
    // 事件从 document 派发时 target 不是元素，没有 matches，整条监听会抛错失效
    const t = e.target;
    // 焦点在可编辑控件上时不抢快捷键；[role=slider] 也算 —— 否则在时间轴圆点上
    // 按「1」会直接跳去概览页，而用户以为自己在调时间
    if (t && t.matches && t.matches('input, select, textarea, [role="slider"]')) return;
    // 四个视图对应 1–4，顺序跟顶栏从左到右一致 —— 记的是位置，不是名字
    const map = { 1: "overview", 2: "control" };
    if (map[e.key]) go(map[e.key]);
  });

  /* ---------- 假数据 ---------- */
  /* ---------- 趋势缓冲 ----------
     最多 121 个采样点，每个点带真实时间戳（§6：间隔可切换为 3/5/10/30/60 秒，
     旧样本的时刻不能因为切换间隔就被重新解释）。用一个数组存 {t, cpu, hdd, fan}
     而不是三条各自独立的数组：三条曲线共享同一份时间轴，混用独立数组时某一轮
     只采到硬盘没采到 CPU，两条数组长度就会错位，下标 i 在两条曲线里代表不同时刻。
     数据只来自本页连接期间的轮询：没有历史库，也就不假装有——
     采满 121 点之前，副标题会老老实实写「已采样 N 点」。 */
  const N = 121;
  const TREND = [];   // [{t: 毫秒时间戳, cpu, hdd, fan}, …]，按 t 升序，最旧的在前

  function num(v) {
    const n = parseFloat(v);
    return isFinite(n) ? n : null;
  }

  /* KB → "1.2 GB"/"830 MB" 这类短字符串，1024 进制。供仪表盘副标题和
     硬件信息卡的内存明细共用，口径只写一遍。kb 不是有效数字时返回 null，
     调用方自己决定拿不到的时候显示什么（"—" 或者整段不显示）。 */
  function fmtKb(kb, digits) {
    const n = num(kb);
    if (n === null) return null;
    let v = n * 1024, i = 0;
    const units = ["B", "KB", "MB", "GB", "TB"];
    while (Math.abs(v) >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    const d = digits == null ? (i ? 1 : 0) : digits;
    return v.toFixed(d) + " " + units[i];
  }
  // 仪表盘副标题「13.1 / 31.7 GB」：跟内存卡同一个一位小数口径，单位只写一次，表盘下面那行放得下
  function gbToStr(kb) {
    const n = num(kb);
    if (n === null) return "—";
    return (n / 1024 / 1024).toFixed(1);
  }
  /* B/s → "12.3 MB/s" 这类字符串，1024 进制。硬盘读写速度、网口上下行
     都走这个，单位换算只有一处。 */
  function fmtBps(v) {
    const n = num(v);
    if (n === null) return "—";
    let x = n, i = 0;
    const units = ["B/s", "KB/s", "MB/s", "GB/s"];
    while (Math.abs(x) >= 1024 && i < units.length - 1) { x /= 1024; i++; }
    return x.toFixed(x < 10 && i ? 1 : 0) + " " + units[i];
  }

  /* 往固定元素里填一句话。id 不存在时静默跳过 —— 这些字段是「有就更好」，
     不该因为某个 DOM 节点被删掉就让整轮状态刷新抛错、后面的卡片全部不更新。
     写入前比一次旧值：10 秒轮询里大多数格子的文字根本没变，裸写 textContent
     仍然会触发一次样式重算，值没变就跳过（§5「值没变不写 DOM」）。 */
  function setText(id, s) {
    const el = document.getElementById(id);
    if (el && el.textContent !== s) el.textContent = s;
  }
  // innerHTML 版本：列表类容器（硬盘表、网卡格、文件系统…）整表重建开销比单个文本节点大得多，
  // 拼好的字符串先跟上一次比较，相同就整体跳过——不必维护「哪一行变了」这种细粒度 diff。
  function setHtmlIfChanged(el, html, keepSelection) {
    if (!el) return;
    if (el.__lastHtml === html) return;
    // 每次轮询都会变的列表（硬盘表、网卡格）：用户正在里面选中文字（比如复制序列号）时
    // 先不重建，否则选区每轮都被冲掉；不记 __lastHtml，选区一松开下一轮照常更新。
    // 只给轮询驱动的列表用：由用户操作触发的重画（删脚本、模块操作完成）必须立刻生效
    if (keepSelection) {
      const sel = window.getSelection && window.getSelection();
      if (sel && !sel.isCollapsed && sel.anchorNode && el.contains(sel.anchorNode)) return;
    }
    el.__lastHtml = html;
    el.innerHTML = html;
  }

  /* ---------- 运行时长 ----------
     格式「1天1小时 24分」：天和小时紧挨着、分钟前空一格，跟系统托盘那种紧凑写法一致；
     前导的 0 天 / 0 小时不写（「5小时 3分」「12分」），不到一分钟写「<1分」。
     upBase 是 /api/state 回来时的开机秒数，upAt 是那一刻的本地时间；
     两次轮询之间按本地时钟往前推，分钟数会自己走。 */
  let upBase = null, upAt = 0;
  function fmtUptime(sec) {
    const s = Math.max(0, Math.floor(sec));
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    if (!d && !h && !m) return "<1分";
    let head = "";
    if (d) head += d + "天";
    if (d || h) head += h + "小时";
    return head ? head + " " + m + "分" : m + "分";
  }
  // 胶囊里「已运行」是固定标签，只改后面的数字 —— 以前整个 textContent 覆盖，标签就没了
  const upEl = document.getElementById("top-uptime");
  const upVal = document.getElementById("top-uptime-v");
  function renderUptime() {
    if (upBase === null) {
      if (upVal) upVal.textContent = "—";
      if (upEl) { upEl.title = "运行时长未知"; upEl.classList.add("is-na"); }
      return "";
    }
    const txt = fmtUptime(upBase + (Date.now() - upAt) / 1000);
    if (upVal && upVal.textContent !== txt) {
      upVal.textContent = txt;
      upEl.title = "设备已运行 " + txt;
      upEl.setAttribute("aria-label", "设备已运行 " + txt);
    }
    if (upEl) upEl.classList.remove("is-na");
    setText("set-uptime", txt);
    return txt;
  }

  /* ---------- 顶栏时钟：设备的年月日时分秒，每秒走 ----------
     时间取设备的（/api/state 的 now + tz_offset），不是这台电脑/手机的：定时开关机按设备时间执行，
     两边时区或对时不一致时，用户看到的应该是设备上的钟。拿到之前先显示浏览器时间。
     只记一个「设备 − 本机」的差，每秒本地推算，不为时钟多发请求；隐藏标签页时停表。
     运行时长（精确到分钟）也挂在这一拍上，跨分钟时跟时钟同一秒翻。 */
  const clockEl = document.getElementById("top-clock");
  let clockSkew = null, clockTz = null, clockTimer = 0;
  const pad2 = (n) => (n < 10 ? "0" : "") + n;
  function setDeviceClock(now, tz) {
    if (now === null) return;
    const skew = now * 1000 - Date.now();
    // 网络延迟会让差值每次抖几十毫秒：差得不多就不改，免得秒数偶尔跳一下或停一下
    if (clockSkew === null || Math.abs(skew - clockSkew) > 1000) clockSkew = skew;
    if (tz !== null && tz !== clockTz) {
      clockTz = tz;
      const h = Math.floor(Math.abs(tz) / 3600), m = Math.round((Math.abs(tz) % 3600) / 60);
      clockEl.title = "设备时间（UTC" + (tz < 0 ? "−" : "+") + h + (m ? ":" + pad2(m) : "") + "）";
    }
    renderClock();
  }
  function renderClock() {
    if (!clockEl) return;
    let d, txt;
    if (clockSkew === null || clockTz === null) {
      d = new Date();
      txt = d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate()) + " " +
            pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds());
    } else {
      // 先按设备时区平移，再用 UTC 读出各字段 —— 得到的就是设备墙上的钟，跟浏览器所在时区无关
      d = new Date(Date.now() + clockSkew + clockTz * 1000);
      txt = d.getUTCFullYear() + "-" + pad2(d.getUTCMonth() + 1) + "-" + pad2(d.getUTCDate()) + " " +
            pad2(d.getUTCHours()) + ":" + pad2(d.getUTCMinutes()) + ":" + pad2(d.getUTCSeconds());
    }
    if (clockEl.textContent !== txt) {
      clockEl.textContent = txt;
      clockEl.dateTime = txt.replace(" ", "T");
    }
  }
  function scheduleClock() {
    clearTimeout(clockTimer);
    if (document.hidden) return;
    renderClock();
    if (upBase !== null) renderUptime();
    // 对齐到下一个整秒再跳，不然 setInterval 慢慢漂移，秒数会偶尔一次跳两格
    const ms = Date.now() + (clockSkew || 0);
    clockTimer = setTimeout(scheduleClock, 1000 - (((ms % 1000) + 1000) % 1000) + 5);
  }
  scheduleClock();

  /* 拼进 innerHTML 之前必须过一遍这个。
     脚本名不是我们写的：除了上传时起的名字，还会收共享目录（SMB 里谁都能放文件）
     下的 *.sh；模块标题/说明来自 module.conf。以前直接 + 拼接，一个名叫
     `a" onmouseover=…` 的 .sh 就能从属性里逃出去执行 —— 而这份页面是有 root 执行权的。 */
  const ESC_MAP = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ESC_MAP[c]);
  }

  /* ---------- §4 统一用词：所有英文状态值在这里一次性翻完 ----------
     HW_SPEC.md §4 的那张表直接搬成字典，渲染函数只管查表，不各自翻译——
     否则「服务状态」在三处各翻一遍，用词迟早对不上。原文（sda / eth0 / systemd
     单元名 / RAID 原始状态……）仍然留一份在 title 里，排障时用得上。 */
  const ZH_SVC = {
    active: "运行中", done: "已执行", failed: "失败", inactive: "未运行",
    activating: "启动中", deactivating: "停止中", reloading: "重载中", unknown: "未知",
  };
  const zhSvc = (s) => ZH_SVC[s] || "异常";
  const ZH_HEALTH = { ok: "正常", warn: "注意", fail: "故障" };
  const zhHealth = (s) => (s ? (ZH_HEALTH[s] || "无数据") : "无数据");
  const ZH_RAID_LEVEL = { raid0: "条带", raid1: "镜像", raid5: "单盘容错", raid6: "双盘容错", raid10: "镜像条带", linear: "线性" };
  function zhRaidLevel(level) {
    const k = String(level || "").toLowerCase();
    return ZH_RAID_LEVEL[k] || level || "—";
  }
  // RAID 状态：clean/active → 正常；resync/recover/reshape → 同步中；degraded/failed/inactive → 对应档；其余未知
  function zhRaidState(state, pct) {
    const s = String(state || "").toLowerCase();
    if (/clean|active/.test(s)) return "正常";
    if (/resync|recover|reshape/.test(s)) return "同步中" + (has(num(pct)) ? " " + Math.round(num(pct)) + "%" : "");
    if (/degraded/.test(s)) return "降级";
    if (/failed|inactive/.test(s)) return "故障";
    return "未知";
  }
  const ZH_GOV = { schedutil: "自动调度", performance: "性能", powersave: "省电", ondemand: "按需", conservative: "保守", userspace: "手动" };
  const zhGov = (g) => ZH_GOV[g] || g || "—";
  const ZH_NIC_STATE = { up: "已连接", down: "未连接" };
  const ZH_DUPLEX = { full: "全双工", half: "半双工" };
  // 硬盘「主文字」：系统盘单独叫系统盘，其余按 name 排序后编号「硬盘 N」，原始设备名进 title
  function diskDisplayName(d, idx) {
    if (d.role === "系统盘" || /系统盘/.test(d.role || "")) return "系统盘";
    return "硬盘 " + (idx + 1);
  }
  function diskKindZh(d) {
    if (d.bus === "nvme") return "固态盘";
    if (d.bus === "mmc") return "闪存";
    if (d.bus === "usb") return "移动硬盘";
    return d.rotational ? "机械盘" : "固态盘";
  }

  /* 一次采样推入一个点。按时间窗去重：applyState() 会被每一处 refreshState() 调用，而
     「保存曲线」「切页」「点刷新」都会额外触发一次——不设闸门的话，用户点三下按钮
     就能把一整屏时间轴塞进几秒钟的数据，横轴时间全是假的。闸门宽度是当前间隔的
     0.8 倍：略小于轮询间隔，手点刷新不会把下一次正常轮询顶掉。 */
  let lastSampleAt = 0;
  let trendDirty = false;   // 本轮 applyState 是否真的推入了新样本——只有这样才值得重画图表
  function sampleGapMs() { return pollSec * 1000 * 0.8; }

  /* 时间刻度：从 1/2/5/10/15/30 分钟、1/2 小时里选一个步长，让窗口内落 4–6 个刻度，
     且刻度本身落在整分钟/整小时上（10:05 而不是 10:04:37）。两张图共用，
     位置、锚点、贴边规则才完全一致。xAt(ms) 把「时间戳」换算成像素 x 坐标。 */
  const TICK_STEPS_MIN = [1, 2, 5, 10, 15, 30, 60, 120];
  function pickTickStepMs(spanMs) {
    const spanMin = spanMs / 60000;
    for (const m of TICK_STEPS_MIN) { if (spanMin / m <= 6) return m * 60000; }
    return TICK_STEPS_MIN[TICK_STEPS_MIN.length - 1] * 60000;
  }
  function timeTicks(winStart, winEnd, xAt, yText) {
    const stepMs = pickTickStepMs(winEnd - winStart);
    // 第一个刻度对齐到 stepMs 的整数倍（本地时间），再往后每 stepMs 一个
    let t = Math.ceil(winStart / stepMs) * stepMs;
    let out = "";
    for (; t <= winEnd; t += stepMs) {
      const d = new Date(t);
      const label = String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
      const x = xAt(t);
      // 太靠近右边缘（最新样本）的刻度不画：文字会和「当前值」的圆点/标签叠在一起
      const anchor = x < 24 ? "start" : winEnd - t < stepMs * 0.4 ? "end" : "middle";
      out += '<text class="axis" x="' + x.toFixed(1) + '" y="' + yText + '" text-anchor="' + anchor + '">' + label + "</text>";
    }
    return out;
  }

  let trendFrom = null;
  function pushTrend(sensor) {
    trendDirty = false;
    // 同一份采样只记一次：状态刷新、静态信息到达都会带着旧采样再调进来，
    // 占掉这一格之后，紧跟着到的真采样反而被间隔限制挡掉
    if (sensor === trendFrom) return;
    trendFrom = sensor;
    const now = Date.now();
    if (now - lastSampleAt < sampleGapMs()) return;
    const c = num(sensor.cpu_temp);
    let h = num(sensor.hdd_max_temp);
    if (h !== null && h <= 0) h = null;
    const f = num(sensor.fan_duty);
    // 三个读数都没拿到就不要占一个采样位，否则图上会留下一段「值为 null」的洞
    if (c === null && h === null && f === null) return;
    lastSampleAt = now;
    TREND.push({ t: now, cpu: c, hdd: h, fan: f });
    while (TREND.length > N) TREND.shift();
    trendDirty = true;
  }

  /* ---------- 迷你折线 ----------
     当前版本的 index.html 没有 .spark 节点（被仪表盘取代），这个函数留着做无害的
     空壳：document.querySelectorAll 在零节点时直接返回空列表，不会抛错。
     万一以后哪张卡又加回 .spark，直接能用——取值已经改成从 TREND 的 {t,...} 对象里挑字段。 */
  function drawSparks() {
    const nodes = document.querySelectorAll(".spark");
    if (!nodes.length) return;
    nodes.forEach((svg) => {
      const key = svg.dataset.series;
      const arr = TREND.map((p) => p[key]).filter(has);
      if (arr.length < 2) return;
      const W = 200, H = 34, pad = 2;
      const min = Math.min(...arr), max = Math.max(...arr);
      const span = max - min || 1;
      const x = (i) => pad + ((W - pad * 2) * i) / (arr.length - 1);
      const y = (v) => H - pad - ((v - min) / span) * (H - pad * 2);
      const line = arr.map((v, i) => (i ? "L" : "M") + x(i).toFixed(1) + " " + y(v).toFixed(1)).join(" ");
      const area = line + " L" + x(arr.length - 1).toFixed(1) + " " + H + " L" + pad + " " + H + " Z";
      // 同一个量在大图里是什么颜色，迷你图上就得是什么颜色：
      // 风扇转速在大图里是紫的，迷你图还画成蓝的就会让人以为是 CPU
      const color = key === "hdd" ? "var(--teal)" : key === "fan" ? "var(--violet)" : "var(--accent)";
      const id = "g-" + key;
      svg.setAttribute("viewBox", "0 0 " + W + " " + H);
      svg.innerHTML =
        '<defs><linearGradient id="' + id + '" x1="0" y1="0" x2="0" y2="1">' +
        '<stop offset="0" stop-color="' + color + '" stop-opacity=".18"/>' +
        '<stop offset="1" stop-color="' + color + '" stop-opacity="0"/></linearGradient></defs>' +
        '<path d="' + area + '" fill="url(#' + id + ')"/>' +
        '<path d="' + line + '" fill="none" stroke="' + color + '" stroke-width="1.6" ' +
        'stroke-linecap="round" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>';
    });
  }

  /* ---------- 指针仪表（gbar） ----------
     移植自 JK 监控台（jkmon/web/app.js 132-220 行）：220° 弧形表盘，0 在左下、
     满量程在右下；分段色带 + 刻度数字 + 告警/临界外圈，锥形指针用 CSS
     transform 转动，过渡交给 CSS（--dur），这里只负责算角度和弧长。
     去掉了 JK 版本里的提示条（data-tip/tipAttr 的悬浮说明）：这四块只是
     CPU / 硬盘 / 风扇 / 内存的即时读数，概览页下面还有趋势图和硬件信息卡
     把「为什么」讲清楚，仪表盘本身不需要再负责解释。 */
  function has(v) { return v !== null && v !== undefined && !(typeof v === "number" && isNaN(v)); }
  function gClamp(v, a, b) { return Math.max(a, Math.min(b, v)); }
  function gLv(v, w, c) {
    if (!has(v)) return "";
    if (!has(w) || !has(c)) return "ok";
    return v >= c ? "crit" : v >= w ? "warn" : "ok";
  }
  const REDUCE_MOTION = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);

  const GZ = { A0: 200, SW: 220, C: 80, CY: 70, R: 58 };
  function gPt(a, r) { a = a * Math.PI / 180; return [GZ.C + r * Math.cos(a), GZ.CY - r * Math.sin(a)]; }
  function gAng(x, max) { return GZ.A0 - GZ.SW * gClamp(x / max, 0, 1); }
  function gArc(x0, x1, max, r) {
    const a0 = gAng(x0, max), a1 = gAng(x1, max), p = gPt(a0, r), q = gPt(a1, r);
    return "M" + p[0].toFixed(2) + " " + p[1].toFixed(2) + "A" + r + " " + r + " 0 " +
      (a0 - a1 > 180 ? 1 : 0) + " 1 " + q[0].toFixed(2) + " " + q[1].toFixed(2);
  }
  function gLine(a, r0, r1, cls) {
    const p = gPt(a, r0), q = gPt(a, r1);
    return '<line class="' + cls + '" x1="' + p[0].toFixed(2) + '" y1="' + p[1].toFixed(2) +
      '" x2="' + q[0].toFixed(2) + '" y2="' + q[1].toFixed(2) + '"/>';
  }
  /* g: {id, label, sub, max, warn, crit, unit, v} */
  function gaugeHtml(g) {
    const max = g.max || 100, step = max % 20 === 0 && max / 20 <= 6 ? 20 : max / 5, R = GZ.R;
    let t = "", lab = "";
    for (let x = 0; x <= max + 1e-6; x += step / 5) {
      const a = gAng(x, max), major = Math.abs(x / step - Math.round(x / step)) < 1e-6;
      t += gLine(a, R - 7, R + 7, major ? "cut cut-m" : "cut");
      if (major) {
        const p = gPt(a, R - 22);
        lab += '<text x="' + p[0].toFixed(1) + '" y="' + (p[1] + 3.5).toFixed(1) +
          '" text-anchor="middle">' + Math.round(x) + "</text>";
      }
    }
    const zone = has(g.warn) && has(g.crit) && g.warn < max
      ? '<path class="zw" d="' + gArc(g.warn, Math.min(g.crit, max), max, R + 10) + '"/>' +
        (g.crit < max ? '<path class="zc" d="' + gArc(g.crit, max, max, R + 10) + '"/>' : "")
      : "";
    // 指针画在 0 的位置，转动用 CSS rotate（顺时针为正）
    const a0 = GZ.A0, tip = gPt(a0, R - 9), b1 = gPt(a0 + 90, 4.6), b2 = gPt(a0 - 90, 4.6), tail = gPt(a0 + 180, 11);
    const nd = "M" + tip.map((v) => v.toFixed(2)).join(" ") + "L" + b1.map((v) => v.toFixed(2)).join(" ") +
      "L" + tail.map((v) => v.toFixed(2)).join(" ") + "L" + b2.map((v) => v.toFixed(2)).join(" ") + "Z";
    return '<div class="gauge na" id="' + g.id + '" data-max="' + max + '" tabindex="-1">' +
      '<svg viewBox="0 0 160 116" role="img" aria-label="' + esc(g.label) + '">' +
      '<path class="trk" d="' + gArc(0, max, max, R) + '"/>' +
      '<path class="arc" pathLength="100" stroke-dasharray="0 100" d="' + gArc(0, max, max, R) + '"/>' +
      t + zone + lab +
      '<g class="ndl"><path class="needle" d="' + nd + '"/></g>' +
      '<circle class="hub" cx="' + GZ.C + '" cy="' + GZ.CY + '" r="5.4"/>' +
      '<text class="gv" x="' + GZ.C + '" y="' + (GZ.CY + 36) + '" text-anchor="middle">—</text></svg>' +
      '<div class="lb"><b>' + esc(g.label) + "</b><small>" + esc(g.sub || "") + "</small></div></div>";
  }
  function gFmt(v, unit) { return !has(v) ? "—" : (unit === "%" && v < 10 ? v.toFixed(1) : String(Math.round(v))); }
  /* 把读数画到仪表上（shown 为指针位置，用于自检动画；v 为用来判断颜色的真实读数） */
  function gPaint(el, shown, v, g) {
    const max = g.max || 100, lvl = has(v) ? (gLv(v, g.warn, g.crit) || "ok") : "na";
    if (el.__lvl !== lvl) {
      el.__lvl = lvl;
      el.className = "gauge " + lvl + (el.__boot ? " booting" : "");
    }
    const f = has(shown) ? gClamp(shown / max, 0, 1) : 0;
    el.__ndl.style.transform = "rotate(" + (f * GZ.SW).toFixed(2) + "deg)";
    el.__arc.style.strokeDasharray = (f * 100).toFixed(2) + " 100";
    const txt = gFmt(has(shown) ? shown : null, g.unit);
    const h = txt === "—" ? "—" : txt + '<tspan class="gu" dx="2">' + esc(g.unit) + "</tspan>";
    if (el.__txt !== h) { el.__txt = h; el.__gv.innerHTML = h; }
  }
  function gBind(el) {
    el.__ndl = el.querySelector(".ndl");
    el.__arc = el.querySelector(".arc");
    el.__gv = el.querySelector(".gv");
    el.__sub = el.querySelector(".lb small");
  }
  // 自检轮次：跟 JK 一致，切回可见标签页时想重新播放自检，只需要把这个数字加一
  let gaugeBootN = 0;
  function gaugeSet(g) {
    const el = document.getElementById(g.id);
    if (!el) return;
    if (!el.__ndl) gBind(el);
    el.__g = g;
    if (el.__sub && el.__sub.textContent !== (g.sub || "")) {
      el.__sub.textContent = g.sub || "";
      el.__sub.title = g.sub || "";
    }
    if (REDUCE_MOTION) { gPaint(el, g.v, g.v, g); return; }
    if (el.__bootN !== gaugeBootN) { el.__bootN = gaugeBootN; gBoot(el); return; }
    if (el.__boot) return;  // 自检过程中不打断，结束时取最新读数
    // 读数和等级都没变就跳过 gPaint（§5）：轮询大多数轮次里温度/占用根本没变化，
    // 裸写 transform/strokeDasharray 仍会触发一次样式重算，值相同时这些写入毫无意义
    const sameV = el.__paintedV === g.v || (Number.isNaN(el.__paintedV) && Number.isNaN(g.v));
    if (sameV && el.__paintedLvl === (has(g.v) ? (gLv(g.v, g.warn, g.crit) || "ok") : "na")) return;
    el.__paintedV = g.v;
    el.__paintedLvl = has(g.v) ? (gLv(g.v, g.warn, g.crit) || "ok") : "na";
    gPaint(el, g.v, g.v, g);
  }
  /* 自检：0 → 满量程（0.75 秒）→ 真实读数（0.85 秒），数字随指针变化，颜色按指针所在区域变化。
     首次拿到数据画一次；标签页从隐藏切回可见时（visibilitychange）gaugeBootN 会被
     再次加一，于是每个仪表都会重播这段动画。 */
  function gBoot(el) {
    gBoot.n = gBoot.n || 0;
    el.__boot = true;
    el.classList.add("booting");
    const t0 = Date.now(), up = 750, down = 850, run = ++gBoot.n;
    el.__run = run;
    const ease = (x) => 1 - Math.pow(1 - x, 3);
    function done() {
      if (el.__run !== run || !el.__boot) return;
      el.__boot = false;
      el.classList.remove("booting");
      el.__lvl = null;
      gPaint(el, el.__g.v, el.__g.v, el.__g);
    }
    function frame() {
      if (el.__run !== run || !el.__boot) return;
      const g = el.__g, max = g.max || 100, dt = Date.now() - t0;
      let pos;
      if (dt >= up + down) { done(); return; }
      if (dt < up) {
        pos = max * ease(dt / up);
      } else {
        const to = has(g.v) ? gClamp(g.v, 0, max) : 0;
        pos = max + (to - max) * ease((dt - up) / down);
      }
      gPaint(el, pos, pos, g);
      requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
    setTimeout(done, up + down + 60);  // 页面在后台时浏览器会暂停逐帧动画，到点直接落到真实读数
  }

  const gbar = document.getElementById("gbar");
  /* 骨架在启动时先画一次（四块都是 na、显示「—」），概览页打开瞬间
     就有四块仪表的轮廓，不会在第一次 /api/sensor 回来之前空着一条。 */
  if (gbar) {
    gbar.innerHTML = [
      { id: "g-cpu", label: "处理器温度", max: 120, warn: 70, crit: 85, unit: "℃" },
      { id: "g-hdd", label: "硬盘温度", max: 80, warn: 50, crit: 60, unit: "℃" },
      { id: "g-fan", label: "风扇转速", max: 100, unit: "%" },
      { id: "g-mem", label: "内存占用", max: 100, warn: 80, crit: 92, unit: "%" },
    ].map(gaugeHtml).join("");
  }

  /* sensor 来自 /api/sensor。四块仪表各自算 v / sub，拿不到就是 na（显示「—」）。 */
  function applyGauges(sensor) {
    if (!gbar || !sensor) return;
    const cpu = num(sensor.cpu_temp);
    const duty = num(sensor.fan_duty);
    const cpuPct = num(sensor.cpu_pct);
    const disks = Array.isArray(sensor.disk_list) ? sensor.disk_list : [];
    const mem = sensor.mem || null;
    const memPct = mem ? num(mem.used_pct) : null;
    const memUsed = mem ? num(mem.used_kb) : null;
    const memTotal = mem ? num(mem.total_kb) : null;

    // 硬盘温度优先用 disk_list 里非休眠盘的最高温：休眠盘的温度传感器读数是采集前最后一次
    // 清醒时留下的旧值，掺进「当前最高温」会把表盘钉在一个盘早就不在跑的温度上。
    // disk_list 没有任何可用温度时（旧后端、或字段缺失）才退回 hdd_max_temp 这个整机口径。
    let hdd = null;
    const liveTemps = disks.filter((d) => !d.standby && has(num(d.temp))).map((d) => num(d.temp));
    if (liveTemps.length) hdd = Math.max(...liveTemps);
    else hdd = num(sensor.hdd_max_temp);
    if (hdd !== null && hdd <= 0) hdd = null;   // 旧控制器在全部盘休眠时写 0，不是温度

    gaugeSet({
      id: "g-cpu", label: "处理器温度", max: 120, warn: 70, crit: 85, unit: "℃",
      v: cpu, sub: has(cpuPct) ? "占用 " + Math.round(cpuPct) + "%" : "",
    });
    gaugeSet({
      id: "g-hdd", label: "硬盘温度", max: 80, warn: 50, crit: 60, unit: "℃",
      v: hdd, sub: disks.length ? disks.length + " 块盘" : "",
    });
    gaugeSet({
      id: "g-fan", label: "风扇转速", max: 100, unit: "%",
      v: duty, sub: fanSourceText(cpu, hdd, sensor),
    });
    gaugeSet({
      id: "g-mem", label: "内存占用", max: 100, warn: 80, crit: 92, unit: "%",
      v: memPct, sub: has(memUsed) && has(memTotal) ? gbToStr(memUsed) + " / " + gbToStr(memTotal) + " GB" : "",
    });
  }

  /* 标签页从隐藏切回可见时，让四块仪表重新播放一次自检扫动画——
     和「刚打开页面」给用户的视觉反馈保持一致，而不是直接跳到当前读数。 */
  function replayGaugeBoot() {
    gaugeBootN++;
    // 立刻用上一次的读数重播，不等下一次轮询：只加计数的话，动画要等最多 10 秒后
    // 的 /api/sensor 回来才开始，切回概览时看到的是一排不动的指针。
    // 还没拿到过数据的仪表（骨架，没有 __g）跳过，等首次数据到了自然会播。
    if (!gbar) return;
    gbar.querySelectorAll(".gauge").forEach((el) => { if (el.__g) gaugeSet(el.__g); });
  }

  /* ---------- 硬件信息卡：处理器 / 内存 / 存储 / 网络 ----------
     静态信息（型号、核数、容量、序列号、SMART）来自 /api/hwinfo（S2.hwinfo，60 秒拉一次，
     见下面「硬件静态信息」一节）；实时读数（占用、频率、速率、温度）来自 /api/sensor（10 秒轮询）。
     两者字段名不完全对应，按 disk.name / nic.name 手动拼在一起——谁都可能缺：hwinfo 还没采
     （旧后端/没装 smartctl）就只画 sensor 那部分，sensor 某块缺了就显示「—」，绝不用 0 顶替。
     四个 render* 各自 try/catch（调用处见 applyState），一张卡自己炸了不连累其余三张。 */
  function fmtCapacity(bytes) {
    const n = num(bytes);
    if (n === null) return "—";
    let v = n, i = 0;
    const units = ["B", "KB", "MB", "GB", "TB", "PB"];
    while (Math.abs(v) >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return v.toFixed(i >= 3 ? 1 : 0) + " " + units[i];
  }
  // GB 摘要专用：固定一位小数，跟仪表「13.1 / 31.7 GB」同一个口径，同一屏不出现两种写法
  function fmtGB(bytes) {
    const n = num(bytes);
    if (n === null) return "—";
    return (n / 1024 / 1024 / 1024).toFixed(1) + " GB";
  }
  // sensor.mem 的字段是 KB，不是字节；复用 fmtGB 之前先 ×1024
  function fmtGBFromKb(kb) {
    const n = num(kb);
    return n === null ? "—" : fmtGB(n * 1024);
  }
  // 相对时间，供 SMART 采样时刻、展开行里的「X 分钟前」共用口径
  function agoText(ts) {
    const n = num(ts);
    if (n === null) return "—";
    const s = Math.max(0, Date.now() / 1000 - n);
    if (s < 60) return Math.round(s) + " 秒前";
    if (s < 3600) return Math.round(s / 60) + " 分钟前";
    if (s < 86400) return Math.round(s / 3600) + " 小时前";
    return Math.round(s / 86400) + " 天前";
  }
  // 通电时长："1.4 年" / "502 天" / "12 小时"——按量级挑一个读起来顺口的单位，
  // 不是「总是显示天数」：新盘通电 12 小时，写「0.0005 年」比写「12 小时」难读得多。
  function fmtPowerOn(hours) {
    const n = num(hours);
    if (n === null) return "—";
    const days = n / 24;
    if (days >= 365) return (days / 365).toFixed(1) + " 年";
    if (days >= 1) return Math.round(days) + " 天";
    return Math.round(n) + " 小时";
  }

  /* ===== 处理器 ===== */
  function renderCpu(hwinfo, sensor) {
    const cpu = (hwinfo && hwinfo.cpu) || null;
    const s = (sensor && sensor.cpu) || null;

    // 卡头摘要：「Rockchip RK3568 · 4 核 Cortex-A55 · 最高 1.99 GHz」，缺一段就跳过那一段，
    // 不用「—」占位把整句话填满占位符
    const bits = [];
    if (cpu && cpu.model) bits.push(String(cpu.model));
    if (cpu && cpu.cores) bits.push(cpu.cores + " 核" + (cpu.core_name ? " " + cpu.core_name : ""));
    if (cpu && has(num(cpu.max_mhz))) bits.push("最高 " + (num(cpu.max_mhz) / 1000).toFixed(2) + " GHz");
    setText("cpu-model", bits.length ? bits.join(" · ") : "—");

    const pct = s ? num(s.pct) : null;
    const mhzArr = s && Array.isArray(s.mhz) ? s.mhz.map(num).filter(has) : [];
    const avgMhz = mhzArr.length ? mhzArr.reduce((a, b) => a + b, 0) / mhzArr.length : null;
    const maxMhz = cpu ? num(cpu.max_mhz) : null;
    const temp = s ? num(s.temp) : null;
    const load = s && Array.isArray(s.load) ? s.load.map(num) : null;

    const statsEl = document.getElementById("cpu-stats");
    if (statsEl) {
      const rows = [];
      rows.push(hwStat("占用", has(pct) ? Math.round(pct) : null, "%", null,
        load && load.some(has) ? "负载 " + load.map((v) => (has(v) ? v.toFixed(2) : "—")).join(" / ") : ""));
      rows.push(hwStat("频率", has(avgMhz) ? (avgMhz / 1000).toFixed(2) : null, "GHz", null,
        has(maxMhz) ? "最高 " + (maxMhz / 1000).toFixed(2) + " GHz" : ""));
      const tLvl = !has(temp) ? "" : temp >= 85 ? "crit" : temp >= 70 ? "warn" : "";
      rows.push(hwStat("温度", has(temp) ? temp : null, "℃", tLvl, ""));
      rows.push(hwStat("负载", load && has(load[0]) ? load[0].toFixed(2) : null, "", null,
        load && load.length >= 3 ? "1 / 5 / 15 分钟" : ""));
      setHtmlIfChanged(statsEl, rows.join("") || '<span class="hw-wait">等待采样…</span>');
    }

    const coresEl = document.getElementById("cpu-cores");
    if (coresEl) {
      const per = s && Array.isArray(s.per_core) ? s.per_core : [];
      const html = per.length ? per.map((v, i) => {
        const p = num(v);
        const mhz = has(mhzArr[i]) ? (mhzArr[i] / 1000).toFixed(1) + " GHz" : "—";
        const hi = has(p) && p >= 85 ? " hi" : "";
        return '<div class="core' + hi + '" style="--p:' + (has(p) ? clampPct(p) : 0) + '%">' +
          '<span class="cn">#' + i + '</span><b>' + (has(p) ? Math.round(p) + "%" : "—") + "</b><small>" + mhz + "</small></div>";
      }).join("") : "";
      setHtmlIfChanged(coresEl, html);
    }
  }
  function clampPct(v) { return Math.max(0, Math.min(100, v)); }
  // CPU / 内存共用的统计块一条：拿不到值时整条跳过（返回空串），不占一个「—」格子，
  // 除非调用方传了说明文字——缺 v 但有 extra（比如负载）时仍然值得显示
  function hwStat(label, v, unit, lvl, extra) {
    if (v === null && !extra) return "";
    const vTxt = v === null ? "—" : esc(String(v));
    const cls = lvl ? " " + lvl : "";
    return '<div class="hw-stat"><span class="k">' + esc(label) + '</span><b class="v' + cls + '">' + vTxt +
      (unit ? "<small>" + esc(unit) + "</small>" : "") + "</b>" +
      (extra ? '<span class="d">' + esc(extra) + "</span>" : "") + "</div>";
  }

  /* ===== 内存 ===== */
  function renderMem(hwinfo, sensor) {
    const mi = (hwinfo && hwinfo.memory) || null;
    const m = (sensor && sensor.mem) || null;

    const totalKb = m ? num(m.total_kb) : (mi && has(num(mi.total)) ? num(mi.total) / 1024 : null);
    const availKb = m ? num(m.avail_kb) : null;
    setText("mem-sum", has(totalKb)
      ? fmtGBFromKb(totalKb) + (has(availKb) ? " · 可用 " + fmtGBFromKb(availKb) : "")
      : "—");

    const usedKb = m ? num(m.used_kb) : null;
    const usedPct = m ? num(m.used_pct) : null;
    const cachedKb = m ? num(m.cached_kb) : null;
    const swapTotalKb = m ? num(m.swap_total_kb) : null;
    const swapUsedKb = m ? num(m.swap_used_kb) : null;

    const statsEl = document.getElementById("mem-stats");
    if (statsEl) {
      const rows = [];
      rows.push(hwStat("已用", has(usedPct) ? Math.round(usedPct) : null, "%", null,
        has(usedKb) ? fmtGBFromKb(usedKb) : ""));
      rows.push(hwStat("可用", has(availKb) ? fmtGBFromKb(availKb).replace(" GB", "") : null, "GB", null, ""));
      rows.push(hwStat("缓存", has(cachedKb) ? fmtGBFromKb(cachedKb).replace(" GB", "") : null, "GB", null, ""));
      // 没有交换分区（swap_total 为 0）时整块隐藏，而不是显示一行永远 0% 的交换——
      // 那样看起来像读数坏了，而不是「这台设备没配 swap」
      if (has(swapTotalKb) && swapTotalKb > 0) {
        const swapPct = swapUsedKb !== null ? (swapUsedKb * 100 / swapTotalKb) : null;
        rows.push(hwStat("交换", has(swapPct) ? Math.round(swapPct) : null, "%", null,
          has(swapUsedKb) ? fmtGBFromKb(swapUsedKb) + " / " + fmtGBFromKb(swapTotalKb) : ""));
      }
      setHtmlIfChanged(statsEl, rows.join("") || '<span class="hw-wait">等待采样…</span>');
    }

    const stackEl = document.getElementById("mem-stack");
    const legendEl = document.getElementById("mem-legend");
    if (stackEl && legendEl) {
      if (has(usedKb) && has(totalKb) && totalKb > 0) {
        const usedW = clampPct(usedKb * 100 / totalKb);
        const cacheW = has(cachedKb) ? clampPct(cachedKb * 100 / totalKb) : 0;
        setHtmlIfChanged(stackEl, '<i class="seg used" style="width:' + usedW.toFixed(1) + '%"></i>' +
          '<i class="seg cache" style="width:' + cacheW.toFixed(1) + '%"></i>');
        const legend = [];
        legend.push('<span><i class="sw used"></i>已用 ' + fmtGBFromKb(usedKb) + "</span>");
        if (has(cachedKb)) legend.push('<span><i class="sw cache"></i>缓存 ' + fmtGBFromKb(cachedKb) + "</span>");
        const freeKb = m ? num(m.free_kb) : null;
        if (has(freeKb)) legend.push('<span><i class="sw free"></i>空闲 ' + fmtGBFromKb(freeKb) + "</span>");
        if (has(swapTotalKb) && swapTotalKb > 0 && has(swapUsedKb)) {
          legend.push('<span><i class="sw swap"></i>交换 ' + fmtGBFromKb(swapUsedKb) + "</span>");
        }
        setHtmlIfChanged(legendEl, legend.join(""));
      } else {
        setHtmlIfChanged(stackEl, "");
        setHtmlIfChanged(legendEl, "");
      }
    }
  }

  /* ===== 存储 ===== */
  // 展开的 SMART 详情按盘名记在这个 Set 里：renderStorage 每次都整表重画（10 秒一次的
  // 轮询 + 速率变化），不按 Set 恢复的话，刚展开的详情每轮都会被重画打回收起状态。
  const openDisks = new Set();
  // 行的 smart.ts 比这个毫秒数还旧，就不信任它当「当前温度」——宁可显示「—」
  // 也不能拿休眠前采到的旧读数冒充现在的温度
  const SMART_TEMP_MAX_AGE = 15 * 60 * 1000;

  function diskHealthPill(d) {
    const m = d && d.smart;
    const cls = { ok: "ok", warn: "warn", fail: "fail" };
    if (m && m.health && cls[m.health]) {
      return '<span class="pill ' + cls[m.health] + '">' + zhHealth(m.health) + "</span>";
    }
    // 读不到健康检测（SMART）：na 胶囊，固定显示「无数据」，原因（需要管理员权限/盘在休眠……）放 title
    const err = (d && d.smart_err) || "";
    return '<span class="pill na" title="' + esc(err || "没有健康检测数据") + '">' + zhHealth(null) + "</span>";
  }

  // 一行 <dt>/<dd> 对；值缺失（null/undefined/空串）的直接跳过，不摆「—」凑数——
  // SMART 详情本来就是「有什么就显示什么」，凑一排「—」只会让真正缺失的字段更难分辨
  function kvRow(label, val) {
    if (val === null || val === undefined || val === "") return "";
    // 一对 dt/dd 包在一个 div 里：网格按「一格一对」排，标签和值不会被拆到两行两列
    return "<div><dt>" + esc(label) + "</dt><dd>" + val + "</dd></div>";
  }

  function diskDetailHtml(d) {
    const m = d.smart || null;
    const rows = [
      kvRow("盘位", d.slot_name ? esc(d.slot_name) : ""),
      kvRow("型号", d.model ? esc(d.model) : ""),
      kvRow("序列号", d.serial ? '<span class="mono">' + esc(d.serial) + "</span>" : ""),
      kvRow("固件版本", d.firmware ? esc(d.firmware) : ""),
      kvRow("路径", d.path ? '<span class="mono">' + esc(d.path) + "</span>" : ""),
      kvRow("总线", d.bus ? esc(d.bus.toUpperCase()) : ""),
      kvRow("转速", has(num(d.rpm)) ? num(d.rpm) + " RPM" : ""),
      kvRow("用途", d.role ? esc(d.role) : ""),
      kvRow("挂载点", Array.isArray(d.mounts) && d.mounts.length ? '<span class="mono">' + d.mounts.map(esc).join(", ") + "</span>" : ""),
      kvRow("已挂载容量", d.usage && has(num(d.usage.total)) ? fmtCapacity(d.usage.total) : ""),
      kvRow("已用", d.usage && has(num(d.usage.used)) ? fmtCapacity(d.usage.used) + (has(num(d.usage.pct)) ? "（" + num(d.usage.pct).toFixed(1) + "%）" : "") : ""),
      kvRow("空闲", d.usage && has(num(d.usage.free)) ? fmtCapacity(d.usage.free) : ""),
    ];
    if (m) {
      rows.push(kvRow("温度", has(num(m.temp)) ? num(m.temp) + " ℃" : ""));
      rows.push(kvRow("通电时间", has(num(m.power_on_hours)) ? num(m.power_on_hours).toLocaleString() + " 小时" : ""));
      rows.push(kvRow("通电次数", has(num(m.power_cycles)) ? num(m.power_cycles).toLocaleString() + " 次" : ""));
      rows.push(kvRow("重映射扇区", has(num(m.reallocated)) ? String(num(m.reallocated)) : ""));
      rows.push(kvRow("待映射扇区", has(num(m.pending)) ? String(num(m.pending)) : ""));
      rows.push(kvRow("不可纠正错误", has(num(m.uncorrectable)) ? String(num(m.uncorrectable)) : ""));
      rows.push(kvRow("寿命已用", has(num(m.wear_pct)) ? num(m.wear_pct) + "%" : ""));
      rows.push(kvRow("累计写入", has(num(m.written_bytes)) ? fmtCapacity(m.written_bytes) : ""));
      rows.push(kvRow("检测时间", has(num(m.ts)) ? agoText(m.ts) : ""));
    } else if (d.smart_err) {
      rows.push(kvRow("健康检测", esc(d.smart_err)));
    }
    const body = rows.filter(Boolean).join("");
    return body ? '<dl class="smart-kv">' + body + "</dl>" : '<p class="help">没有可显示的详情</p>';
  }

  // 合并 hwinfo.disks（静态：型号/序列号/SMART）与 sensor.disk_list（实时：温度/繁忙/速率），
  // 以 name 为键；任一侧独有的盘也要出现——新插的 USB 盘可能 sensor 已经采到但 hwinfo
  // 60 秒还没刷新，反过来 hwinfo 刚采到的盘 sensor 下一轮才会跟上，两种情况都不该把盘丢掉。
  function mergeDisks(hwinfo, sensor) {
    const byName = new Map();
    const order = [];
    const hiDisks = (hwinfo && Array.isArray(hwinfo.disks)) ? hwinfo.disks : [];
    const liveDisks = (sensor && Array.isArray(sensor.disk_list)) ? sensor.disk_list : [];
    hiDisks.forEach((d) => {
      const name = d && d.name;
      if (!name) return;
      byName.set(name, Object.assign({}, d));
      order.push(name);
    });
    liveDisks.forEach((d) => {
      const name = d && d.name;
      if (!name) return;
      if (!byName.has(name)) { byName.set(name, {}); order.push(name); }
      const merged = byName.get(name);
      merged.name = name;
      merged.live = d;
      // hwinfo 没给容量/型号时拿 sensor 那份顶上，sensor 的字段名不同（bytes/model 同名，直接合并）
      if (!has(num(merged.bytes)) && has(num(d.bytes))) merged.bytes = d.bytes;
      if (!merged.model && d.model) merged.model = d.model;
      if (merged.rotational === undefined) merged.rotational = d.rotational;
      // standby 字段实时性更强，sensor 的优先；hwinfo 采集时的 standby 只是「采集那一刻」的状态
      if (has(d.standby) || d.standby === false) merged.standby = !!d.standby;
    });
    return order.map((n) => byName.get(n));
  }

  function diskRowHtml(d, dispName) {
    const name = d.name;
    const live = d.live || null;
    const standby = !!d.standby;
    const open = openDisks.has(name);
    const cap = has(num(d.bytes)) ? fmtCapacity(d.bytes) : "—";

    // 温度：休眠盘绝不显示——哪怕 sensor 或缓存的 SMART 里还留着旧值，那也是盘醒着时的读数，
    // 当前它根本没在转，显示出来等于撒谎说「现在是这个温度」。
    // 非休眠盘：live.temp（sensor 实时）优先；没有时退回 smart.temp，但只在那份 SMART
    // 是 15 分钟内采的才采信——更旧的「当前温度」还是假的，只是假得没那么明显。
    let tempTxt = "—", tempCls = "";
    if (!standby) {
      let t = live ? num(live.temp) : null;
      if (!has(t) && d.smart && has(num(d.smart.temp)) && has(num(d.smart.ts))) {
        if (Date.now() / 1000 - num(d.smart.ts) < SMART_TEMP_MAX_AGE / 1000) t = num(d.smart.temp);
      }
      if (has(t)) {
        tempTxt = t + " ℃";
        tempCls = t >= 60 ? " crit" : t >= 50 ? " warn" : "";
      }
    }

    const busy = live ? num(live.busy_pct) : null;
    const busyTxt = has(busy) ? Math.round(busy) + "%" : "—";
    const rd = live ? fmtBps(live.rd_bps) : "—";
    const wr = live ? fmtBps(live.wr_bps) : "—";
    const powerOn = d.smart && has(num(d.smart.power_on_hours)) ? fmtPowerOn(d.smart.power_on_hours) : "—";

    // 型号一行、序列号单独一行（等宽字体，方便对着盘身上的标签核对）
    const modelBits = [d.vendor, d.model].filter(Boolean).join(" ");
    const serialHtml = d.serial
      ? '<small class="dserial" title="序列号">序列号 <span class="mono">' + esc(d.serial) + "</span></small>" : "";

    // 盘位：S2 的两个仓（盘位 1 / 2），或接口名（M.2 / USB / 板载闪存）
    const slotTxt = d.slot_name || "—";

    // 容量 · 已用 / 空闲：数字取挂载的文件系统（statvfs），没挂载就只写总容量
    const u = d.usage || null;
    let capHtml = '<span class="cap-total">' + cap + "</span>";
    if (u && has(num(u.total)) && num(u.total) > 0) {
      const pct = has(num(u.pct)) ? num(u.pct) : (u.used * 100 / u.total);
      const cls = pct >= 95 ? " crit" : pct >= 85 ? " warn" : "";
      capHtml += '<span class="cap-use">已用 ' + fmtCapacity(u.used) + " · 空闲 " + fmtCapacity(u.free) +
        (u.shared ? " · 阵列共享" : "") + "</span>" +
        '<span class="bar cap-bar" title="已用 ' + pct.toFixed(1) + '%"><span class="fill' + cls + '" style="width:' + clampPct(pct) + '%"></span></span>';
    } else {
      capHtml += '<span class="cap-use muted">未挂载</span>';
    }

    const tags = '<span class="dtag">' + esc(diskKindZh(d)) + "</span>" +
      (standby ? '<span class="dtag sleep">休眠</span>' : "");

    // 主文字是中文名（系统盘 / 硬盘 N），原始设备名 sda 只放在 title 里——排障时还要靠它。
    // 详情只由最后一列的按钮展开：整行可点时，用户想复制序列号、看读写数字都会误触把行展开
    const detailId = "disk-detail-" + name.replace(/[^\w-]/g, "_");
    const row = '<tr class="disk-row' + (standby ? " is-standby" : "") + (open ? " is-open" : "") + '" data-disk="' + esc(name) + '">' +
      '<td class="dslot" data-label="盘位">' + esc(slotTxt) + "</td>" +
      '<td><div class="dname"><b title="' + esc(name) + '">' + esc(dispName) + "</b>" + tags + "</div>" +
      (modelBits ? '<small class="dmodel">' + esc(modelBits) + "</small>" : "") + serialHtml + "</td>" +
      '<td class="dcap" data-label="容量">' + capHtml + "</td>" +
      '<td class="n' + tempCls + '" data-label="温度">' + tempTxt + "</td>" +
      '<td data-label="健康">' + diskHealthPill(d) + "</td>" +
      '<td class="n" data-label="通电">' + powerOn + "</td>" +
      '<td class="n" data-label="繁忙">' + busyTxt + "</td>" +
      '<td class="n" data-label="读取">' + rd + "</td>" +
      '<td class="n" data-label="写入">' + wr + "</td>" +
      '<td class="dact"><button type="button" class="icon-btn disk-more" data-disk-more="' + esc(name) + '"' +
      ' aria-expanded="' + open + '" aria-controls="' + detailId + '"' +
      ' title="' + (open ? "收起详细信息" : "查看详细信息") + '" aria-label="' + esc(dispName) + (open ? " 收起详细信息" : " 查看详细信息") + '">' +
      '<svg aria-hidden="true"><use href="#i-chevron"/></svg></button></td>' +
      "</tr>";
    const detail = open
      ? '<tr class="disk-detail" id="' + detailId + '" data-disk-detail="' + esc(name) + '"><td colspan="10">' + diskDetailHtml(d) + "</td></tr>"
      : "";
    return row + detail;
  }

  function toggleDiskRow(name) {
    if (openDisks.has(name)) openDisks.delete(name);
    else openDisks.add(name);
  }

  function renderStorage(hwinfo, sensor) {
    const disks = mergeDisks(hwinfo, sensor);
    // 展开状态只认当前还存在的盘名，盘被拔掉之后它的名字不该一直占着 Set
    const names = new Set(disks.map((d) => d.name));
    Array.from(openDisks).forEach((n) => { if (!names.has(n)) openDisks.delete(n); });

    const tbody = document.getElementById("disk-tbody");
    if (tbody) {
      // 编号跳过系统盘：系统盘单独叫「系统盘」，其余按当前顺序（已经是 name 排序，见 mergeDisks）编「硬盘 N」
      let diskIdx = 0;
      const html = disks.length
        ? disks.map((d) => {
          const dispName = diskDisplayName(d, diskIdx);
          if (dispName !== "系统盘") diskIdx++;
          return diskRowHtml(d, dispName);
        }).join("")
        : '<tr><td colspan="10" class="empty-row">未检测到硬盘</td></tr>';
      setHtmlIfChanged(tbody, html, true);
    }

    const totalBytes = disks.reduce((a, d) => a + (has(num(d.bytes)) ? num(d.bytes) : 0), 0);
    // 卡头再带一句实时总读写：每块盘的速率来自 /api/sensor，跟采样间隔一起刷新
    let rdSum = 0, wrSum = 0, haveRate = false;
    disks.forEach((d) => {
      if (!d.live) return;
      if (has(num(d.live.rd_bps))) { rdSum += num(d.live.rd_bps); haveRate = true; }
      if (has(num(d.live.wr_bps))) { wrSum += num(d.live.wr_bps); haveRate = true; }
    });
    setText("disk-sum", disks.length
      ? disks.length + " 块 · 共 " + fmtCapacity(totalBytes) + (haveRate ? " · 读 " + fmtBps(rdSum) + " · 写 " + fmtBps(wrSum) : "")
      : "—");

    // 文件系统列表已撤掉：每块盘的已用 / 空闲已经直接显示在硬盘行里，不再单列一份挂载点。

    // 阵列：同样只在有数据时露标题；level/state 按 §4 glossary 翻成中文，原文进 title
    const raidTitle = document.getElementById("raid-title");
    const raidList = document.getElementById("raid-list");
    const raidArr = (hwinfo && Array.isArray(hwinfo.raids)) ? hwinfo.raids : [];
    if (raidTitle) raidTitle.hidden = !raidArr.length;
    if (raidList) {
      const html = raidArr.map((r) => {
        const st = String(r.state || "").toLowerCase();
        const lvl = /clean|active/.test(st) ? "ok" : /resync|recover|reshape/.test(st) ? "warn" : /degraded|failed|inactive/.test(st) ? "fail" : "warn";
        const members = Array.isArray(r.members) ? r.members.join(" ") : "";
        return '<div class="raid"><b>' + esc(r.name) + '</b><span class="pill ' + lvl + '" title="' + esc(r.state || "") + '">' +
          esc(zhRaidState(r.state, r.sync_pct)) + "</span>" +
          '<span class="raid-lv">' + esc([zhRaidLevel(r.level), members].filter(Boolean).join(" · ")) + "</span></div>";
      }).join("");
      setHtmlIfChanged(raidList, html);
    }
  }

  // 每行最后的「详情」按钮展开 / 收起 SMART 详情；委托到 tbody，重渲染后不必重新绑定。
  // 用的是真 <button>，回车 / 空格由浏览器自己转成 click，不必再单独处理键盘。
  // 重渲染会换掉按钮节点，渲染完把焦点还给同一块盘的按钮，键盘用户不会被甩回页首。
  const diskTbody = document.getElementById("disk-tbody");
  if (diskTbody) {
    diskTbody.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-disk-more]");
      if (!btn) return;
      const name = btn.dataset.diskMore;
      toggleDiskRow(name);
      renderStorage(S2.hwinfo, S2.sensor);
      const again = [...diskTbody.querySelectorAll("[data-disk-more]")].find((b) => b.dataset.diskMore === name);
      if (again) again.focus();
    });
  }

  /* ===== 网络 ===== */
  // 每张网卡最近 12 次 rx/tx 速率样本，画迷你折线；键是网卡名，刷新页面就清空（不跨会话）。
  // 12 点覆盖的时长随采样间隔变化（10 秒→2 分钟，60 秒→12 分钟……），标注跟着算，不再硬编码「近 2 分钟」。
  const nicHist = new Map();
  const NIC_HIST_N = 12;

  function pushNicHist(name, rx, tx) {
    if (!nicHist.has(name)) nicHist.set(name, []);
    const arr = nicHist.get(name);
    arr.push([has(rx) ? rx : 0, has(tx) ? tx : 0]);
    while (arr.length > NIC_HIST_N) arr.shift();
  }
  // 网卡拔掉/改名之后它的历史不该一直占着 Map——跟 openDisks 对现存盘名做裁剪是同一个道理
  function pruneNicHist(liveNames) {
    Array.from(nicHist.keys()).forEach((n) => { if (!liveNames.has(n)) nicHist.delete(n); });
  }
  function nicSparkSpanText() {
    const sec = NIC_HIST_N * pollSec;
    return sec >= 60 ? Math.round(sec / 60) + " 分钟" : sec + " 秒";
  }

  // 两条 <polyline>（rx/tx），scale 到两条序列的最大值；min 1KB/s 地板，否则空闲网卡的折线
  // 会被拉到和满速网卡一样「高」，看着像在跑流量
  function nicSparkSvg(hist) {
    if (!hist || hist.length < 2) return "";
    const W = 100, H = 30;
    let max = 1024;
    hist.forEach(([rx, tx]) => { max = Math.max(max, rx, tx); });
    const x = (i) => (hist.length > 1 ? (W * i) / (hist.length - 1) : 0);
    const y = (v) => H - (Math.min(v, max) / max) * H;
    const pts = (idx) => hist.map((p, i) => x(i).toFixed(1) + "," + y(p[idx]).toFixed(1)).join(" ");
    return '<svg class="nic-spark" viewBox="0 0 ' + W + " " + H + '" preserveAspectRatio="none">' +
      '<title>近 ' + esc(nicSparkSpanText()) + "收发</title>" +
      '<polyline class="rx" points="' + pts(0) + '"/>' +
      '<polyline class="tx" points="' + pts(1) + '"/></svg>';
  }

  // 合并 hwinfo.nics（静态：MAC、驱动、链路速率）与 sensor.nics（实时：up/rx/tx），以 name 为键
  function mergeNics(hwinfo, sensor) {
    const byName = new Map();
    const order = [];
    const hiNics = (hwinfo && Array.isArray(hwinfo.nics)) ? hwinfo.nics : [];
    const liveNics = (sensor && Array.isArray(sensor.nics)) ? sensor.nics : [];
    hiNics.forEach((n) => {
      if (!n || !n.name) return;
      byName.set(n.name, Object.assign({}, n));
      order.push(n.name);
    });
    liveNics.forEach((n) => {
      if (!n || !n.name) return;
      if (!byName.has(n.name)) { byName.set(n.name, {}); order.push(n.name); }
      const merged = byName.get(n.name);
      merged.name = n.name;
      merged.live = n;
      if (merged.up === undefined) merged.up = n.up;
      if (!has(num(merged.speed_mbps)) && has(num(n.speed_mbps))) merged.speed_mbps = n.speed_mbps;
    });
    return order.map((n) => byName.get(n));
  }

  // 网口主文字按 name 排序编号「网口 N」，eth0 这类原始设备名只进 title——跟硬盘编号是同一条规则
  function nicDisplayName(idx) { return "网口 " + (idx + 1); }

  function nicCardHtml(n, idx) {
    const live = n.live || null;
    const up = live ? !!live.up : !!n.up;
    const rx = live ? num(live.rx_bps) : null;
    const tx = live ? num(live.tx_bps) : null;

    const speedMbps = has(num(n.speed_mbps)) ? num(n.speed_mbps) : null;
    const speedTxt = !up ? "未连接" : has(speedMbps) ? (speedMbps >= 1000 ? (speedMbps / 1000) + " Gbps" : speedMbps + " Mbps") : "—";
    const speedPill = '<span class="pill ' + (up ? "ok" : "na") + '">' + esc(speedTxt) + "</span>";

    const ipv4 = Array.isArray(n.ipv4) ? n.ipv4 : [];
    const ipv6 = Array.isArray(n.ipv6) ? n.ipv6 : [];
    const allIps = ipv4.concat(ipv6);
    const firstIp = allIps[0] || "";
    const restIps = allIps.slice(1);
    const ipHtml = firstIp
      ? '<div class="nic-ip" title="' + esc(restIps.join(", ")) + '">' + esc(firstIp) +
        (restIps.length ? " <small>+" + restIps.length + "</small>" : "") + "</div>"
      : '<div class="nic-ip">—</div>';

    const duplexTxt = n.duplex && ZH_DUPLEX[n.duplex] ? " · " + ZH_DUPLEX[n.duplex] : "";

    return '<div class="nic' + (up ? "" : " down") + '">' +
      '<div class="nic-hd"><b title="' + esc(n.name) + '">' + esc(nicDisplayName(idx)) + "</b>" + speedPill +
      (n.mac ? '<span class="nic-mac" title="物理地址">' + esc(n.mac) + "</span>" : "") + "</div>" +
      (duplexTxt ? '<div class="nic-ip">' + esc(duplexTxt.replace(" · ", "")) + "</div>" : "") +
      '<div class="nic-rates"><div><span>上传</span><b class="up">' + (has(tx) ? fmtBps(tx) : "—") + "</b></div>" +
      '<div><span>下载</span><b class="down">' + (has(rx) ? fmtBps(rx) : "—") + "</b></div></div>" +
      ipHtml + nicSparkSvg(nicHist.get(n.name)) +
      "</div>";
  }

  let nicHistFrom = null;
  function renderNet(hwinfo, sensor) {
    const nics = mergeNics(hwinfo, sensor);
    pruneNicHist(new Set(nics.map((n) => n.name)));
    // 一份采样只记一次历史：这个函数还会被状态刷新、静态信息到达、切回概览重复调用
    if (sensor && sensor !== nicHistFrom) {
      nicHistFrom = sensor;
      nics.forEach((n) => pushNicHist(n.name, n.live ? num(n.live.rx_bps) : null, n.live ? num(n.live.tx_bps) : null));
    }
    const grid = document.getElementById("nic-grid");
    if (grid) {
      const html = nics.length
        ? nics.map((n, i) => nicCardHtml(n, i)).join("")
        : '<span class="hw-wait">等待采样…</span>';
      setHtmlIfChanged(grid, html, true);
    }
    const upN = nics.filter((n) => (n.live ? n.live.up : n.up)).length;
    setText("net-sum", nics.length ? nics.length + " 个网卡 · " + upN + " 个在线" : "—");
  }

  /* ---------- 趋势图 ---------- */
  const chart = document.getElementById("chart");
  const chartSvg = document.getElementById("chart-svg");
  const cross = document.getElementById("chart-cross");
  const tip = document.getElementById("chart-tip");
  // 右边距要放得下「100%」这排右轴刻度，10px 的时候会被裁掉一半
  const PAD = { l: 38, r: 46, t: 12, b: 22 };

  function chartVisible() {
    // 注意：导航按钮上也有 data-view，必须限定 .view 才是内容区
    return document.querySelector('.view[data-view="overview"]').classList.contains("is-active");
  }

  // 窗口覆盖的时长（毫秒）文字，供副标题使用：3 秒→6 分钟、10 秒→20 分钟、60 秒→2 小时……
  function windowSpanText(sec) {
    const totalMin = Math.round((sec * (N - 1)) / 60);
    if (totalMin >= 60 && totalMin % 60 === 0) return (totalMin / 60) + " 小时";
    return totalMin + " 分钟";
  }

  // 记上一次真正重画时用的数据指纹：只有「数据变了」或「窗口大小变了（resize）」才值得
  // 重新拼一整份 SVG 字符串——10 秒轮询里有很多轮其实啥都没推入新样本（3 个读数都拿不到），
  // 这些轮次应该直接跳过，不产生一次布局/绘制
  let lastChartSig = "";
  function drawChart(force) {
    if (!chartVisible()) return;
    if (!force && !trendDirty && !chartResizePending) return;
    chartResizePending = false;
    const W = chart.clientWidth;
    // 高度不能写回 SVG 的 height 属性：CSS 已经是 height:100%，再写一次就会和
    // 容器形成「量高度 → 设高度 → 容器变高 → 再量」的循环，轮询每 10 秒放大一圈，
    // 几十秒后图表能把整屏撑满。只设 viewBox，尺寸交给 CSS。
    const H = Math.max(120, chart.clientHeight);
    if (!W || !H) return;
    // 没有样本时直接清空，不画一条「0 到 0」的假时间轴
    if (!TREND.length) {
      if (lastChartSig !== "empty") { chartSvg.setAttribute("viewBox", "0 0 " + W + " " + H); chartSvg.innerHTML = ""; lastChartSig = "empty"; }
      return;
    }
    const sig = W + "x" + H + "@" + TREND.length + ":" + TREND[TREND.length - 1].t;
    if (!force && sig === lastChartSig) return;   // 尺寸和最新样本都没变，没必要重画
    lastChartSig = sig;
    chartSvg.setAttribute("viewBox", "0 0 " + W + " " + H);

    const min = 20, max = 100;
    const iw = W - PAD.l - PAD.r;
    const ih = H - PAD.t - PAD.b;
    // 横轴按真实时间：窗口宽度 = (N-1) 个采样间隔，右端贴最新样本的时刻，
    // 不是贴 Date.now()——否则下一次轮询还没回来时，最新的点会被画在轴的内侧而不是贴边。
    const winEnd = TREND[TREND.length - 1].t;
    const winStart = winEnd - (N - 1) * pollSec * 1000;
    const x = (t) => PAD.l + iw * ((t - winStart) / (winEnd - winStart || 1));
    const y = (v) => PAD.t + ih * (1 - (v - min) / (max - min));
    // 右轴：转速 0–100%，铺满同一块高度。两条轴量纲不同，所以右轴刻度
    // 刻意和左侧网格线对齐（20℃↔0%、40℃↔25%……100℃↔100%），
    // 每排数字都挂在一条看得见的线上，不用靠眼估位置。
    const yDuty = (d) => PAD.t + ih * (1 - d / 100);

    let out = "";
    for (let v = 20; v <= 100; v += 20) {
      out += '<line class="gridline" x1="' + PAD.l + '" x2="' + (W - PAD.r) + '" y1="' + y(v).toFixed(1) + '" y2="' + y(v).toFixed(1) + '"/>';
      out += '<text class="axis" x="' + (PAD.l - 8) + '" y="' + (y(v) + 3.5).toFixed(1) + '" text-anchor="end">' + v + "°</text>";
      out += '<text class="axis ax-r" x="' + (W - PAD.r + 8) + '" y="' + (y(v) + 3.5).toFixed(1) +
        '" text-anchor="start">' + ((v - 20) * 100) / 80 + "%</text>";
    }
    out += timeTicks(winStart, winEnd, x, H - 6);

    // 预警 / 临界参考线：让「离红线还有多远」一眼可见。
    // 标签放左端 —— 它是温度值，属于左轴；放右端会和转速刻度挤成一列，
    // 一边是 85° 一边是 75%，两种量纲挨着排最容易读串。
    [[70, "var(--warn)"], [85, "var(--danger)"]].forEach(([v, c]) => {
      out += '<line x1="' + PAD.l + '" x2="' + (W - PAD.r) + '" y1="' + y(v).toFixed(1) + '" y2="' + y(v).toFixed(1) +
        '" stroke="' + c + '" stroke-width="1" stroke-dasharray="4 4" opacity=".5"/>';
      out += '<text class="axis" x="' + (PAD.l + 4) + '" y="' + (y(v) - 5).toFixed(1) +
        '" text-anchor="start" fill="' + c + '">' + v + "°</text>";
    });

    // 第三条是转速：量纲是百分比，走右轴；不铺面积 —— 三条面积叠在一起会糊成一片，
    // 而且转速的面积会从右轴一路填到底，看起来像「转速占了 60% 的高度」这种误读
    [["cpu", "var(--accent)", y, true],
     ["hdd", "var(--teal)", y, true],
     ["fan", "var(--violet)", yDuty, false]].forEach(([key, color, yf, filled]) => {
      const pts = TREND.filter((p) => has(p[key]));
      if (pts.length < 2) return;
      const xa = (p) => x(p.t);
      const line = pts.map((p, i) => (i ? "L" : "M") + xa(p).toFixed(1) + " " + yf(p[key]).toFixed(1)).join(" ");
      if (filled) {
        const area = line + " L" + xa(pts[pts.length - 1]).toFixed(1) + " " + (PAD.t + ih) + " L" + xa(pts[0]).toFixed(1) + " " + (PAD.t + ih) + " Z";
        out += '<path d="' + area + '" fill="' + color + '" opacity=".07"/>';
      }
      out += '<path d="' + line + '" fill="none" stroke="' + color + '" stroke-width="' + (filled ? 2 : 1.6) +
        '" stroke-linecap="round" stroke-linejoin="round"/>';
      const last = pts[pts.length - 1];
      out += '<circle cx="' + xa(last).toFixed(1) + '" cy="' + yf(last[key]).toFixed(1) + '" r="3" fill="' + color + '"/>';
      if (key === "cpu") {
        out += '<circle class="halo" cx="' + xa(last).toFixed(1) + '" cy="' + yf(last[key]).toFixed(1) +
          '" r="3.5" fill="none" stroke="' + color + '" stroke-width="1.5"/>';
      }
    });

    chartSvg.innerHTML = out;
    // 读屏只有一句「图像」等于没有信息：把三条曲线的当前值和量程写成一句人话
    const last = TREND[TREND.length - 1];
    const cur = (k) => (has(last[k]) ? last[k] : "未知");
    out = null;   // 大字符串用完就丢，帮助 GC（这条 SVG 字符串在长会话里反复分配，是堆里最大的临时对象之一）
    chartSvg.setAttribute("aria-label",
      "近 " + windowSpanText(pollSec) + "趋势。处理器当前 " + cur("cpu") + " 摄氏度，硬盘当前 " + cur("hdd") +
      " 摄氏度，风扇转速 " + cur("fan") + "%；左轴 20 到 100 摄氏度（70 度预警、85 度临界），右轴 0 到 100%。");

    // 缓冲没采满时如实说明，别让一屏时间轴配几个点还装作很完整
    const sub = document.getElementById("chart-sub");
    if (sub) {
      const have = TREND.length;
      const txt = have >= N
        ? "近 " + windowSpanText(pollSec) + " · " + (pollSec === 1 ? "实时" : "每 " + pollSec + " 秒采样")
        : "已采 " + have + " / " + N + " 点 · " + (pollSec === 1 ? "实时" : "每 " + pollSec + " 秒采样");
      if (sub.textContent !== txt) sub.textContent = txt;
    }
  }
  let chartResizePending = false;

  /* 十字线 + 数值浮层。原来只监听 mousemove —— 触屏没有 hover，手指划过去什么都不出；
     改用 pointer 事件后，鼠标移动和手指按住拖动都能读到数值。
     按时间找最近的样本，而不是按下标——窗口现在是真实时间窗，鼠标位置先换算成
     时间戳，再在 TREND 里找最接近的一条，不同曲线缺样本的时刻也不会找错点。 */
  function chartTip(clientX, clientY) {
    if (TREND.length < 2) return;
    const rect = chart.getBoundingClientRect();
    const W = rect.width;
    const iw = W - PAD.l - PAD.r;
    const winEnd = TREND[TREND.length - 1].t;
    const winStart = winEnd - (N - 1) * pollSec * 1000;
    const rel = clamp01((clientX - rect.left - PAD.l) / iw);
    const targetT = winStart + rel * (winEnd - winStart);
    // 找时间上最接近 targetT 的样本
    let best = TREND[0], bestDiff = Math.abs(TREND[0].t - targetT);
    for (let i = 1; i < TREND.length; i++) {
      const diff = Math.abs(TREND[i].t - targetT);
      if (diff < bestDiff) { bestDiff = diff; best = TREND[i]; }
    }
    const px = PAD.l + iw * ((best.t - winStart) / (winEnd - winStart || 1));
    cross.style.left = px + "px";
    cross.classList.add("on");
    const d = new Date(best.t);
    const at = (v, unit) => (has(v) ? Math.round(v) + unit : "—");
    tip.innerHTML =
      '<div class="t">' + String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0") + ":" + String(d.getSeconds()).padStart(2, "0") + "</div>" +
      '<div class="r"><i style="background:var(--accent)"></i>处理器<b>' + at(best.cpu, "°C") + "</b></div>" +
      '<div class="r"><i style="background:var(--teal)"></i>硬盘<b>' + at(best.hdd, "°C") + "</b></div>" +
      '<div class="r"><i style="background:var(--violet)"></i>转速<b>' + at(best.fan, "%") + "</b></div>";
    tip.style.left = px + "px";
    tip.style.top = Math.max(8, clientY - rect.top - 14) + "px";
    tip.classList.add("on");
  }
  function clamp01(v) { return Math.max(0, Math.min(1, v)); }
  function chartTipOff() {
    cross.classList.remove("on");
    tip.classList.remove("on");
  }
  // 这里不对 pointermove/pointerdown 调 preventDefault：手指在图表上往下滑
  // 本来是想翻页，抢了默认行为就等于把整条趋势图变成一块不能滚动的死区
  // （配合 style.css 里图表容器的 touch-action:pan-y）。
  chart.addEventListener("pointermove", (e) => chartTip(e.clientX, e.clientY));
  chart.addEventListener("pointerdown", (e) => chartTip(e.clientX, e.clientY));
  chart.addEventListener("pointerleave", chartTipOff);
  chart.addEventListener("pointercancel", chartTipOff);
  // 鼠标移开用 pointerleave 就够了，但触屏没有「移开」这个概念——手指一抬，
  // 十字线和浮层会一直停在抬手前的位置，看起来像卡住。pointerup 补上这一刀。
  chart.addEventListener("pointerup", chartTipOff);
  // resize 会连着触发几十次，每次全量重写 SVG 太浪费，一帧最多画一次
  let resizeRaf = 0;
  window.addEventListener("resize", () => {
    if (resizeRaf) return;
    resizeRaf = requestAnimationFrame(() => {
      resizeRaf = 0;
      chartResizePending = true;
      drawChart();
      // 卡片高度由栅格决定，窗口一变可用高度也变，「还需不需要淡出」得重量一次
      markScriptScroll();
    });
  });
  requestAnimationFrame(drawChart);

  /* ---------- 服务矩阵 ---------- */
  // 后端把 systemd 的 ActiveState/Type/Result 映射到这几个值；中文用统一的 zhSvc()
  // 查表（定义在 §4 术语表那一节），不在这里再翻一遍。点的颜色单独一张表——
  // 「颜色」和「文字」是两套独立的派生，分开定义才不会漏掉新状态值的某一边。
  // unknown（查不到 / 开发机没有 systemctl）用灰点，不能跟 failed 一样标红
  const SVC_DOT = { active: "on", done: "done", failed: "off", unknown: "unk",
    inactive: "off", activating: "warn", deactivating: "warn", reloading: "warn" };
  // systemd 单元名是给 SSH 用的：光看 fan-control 不知道它管什么。
  // 中文名做主标签，单元名降级成灰色小字跟在后面 —— 排障时还得照着它敲
  // systemctl restart，只塞进 title 就多一次鼠标操作。≤760 小字让位给中文。
  const SVC_NAMES = {
    "fnos-s2-native": "原生硬件控制",
    "fan-control": "风扇控制",
    "sata-led-manager": "硬盘指示灯管理",
    "power-led-net": "电源网络指示灯管理",
    "power-key-daemon": "电源按键服务",
    "random-mac": "网卡物理地址",
    "set-hostname": "设置主机名",
    "selfheal": "自我修复服务",
    "boot-version-guard": "启动版本保护",
    "s2-power-schedule": "定时开关机服务",
  };
  // 名字先摆出来，页面不会在拿到 /api/state 之前空一片
  let SERVICES = Object.keys(SVC_NAMES).map((n) => ({ n: n, s: "unknown" }));
  const svcGrid = document.getElementById("svc-grid");
  const svcSummary = document.getElementById("svc-summary");
  function renderServices() {
    const html = SERVICES.map((v) => {
      const cn = SVC_NAMES[v.n] || v.n;
      // 原始状态值（可能是认不出的新值）放 title，中文译名是主文字——
      // 认不出的状态归到 zhSvc() 的「异常」兜底，而不是把英文原文漏出来
      // 系统服务名（fan-control 这类英文）不上主文字，只放悬停提示，排障时还要用
      const stc = v.s === "active" || v.s === "done" ? " on" : v.s === "failed" ? " bad" : "";
      return '<div class="svc-row" title="' + esc(cn) + "（" + esc(v.n) + "）" + '">' +
        '<span class="dot ' + (SVC_DOT[v.s] || "off") + '"></span>' +
        '<span class="name">' + esc(cn) + '</span>' +
        '<span class="state' + stc + '">' + esc(zhSvc(v.s)) + "</span></div>";
    }).join("");
    setHtmlIfChanged(svcGrid, html);
    // 汇总必须算出来：写死「9 项全部正常」会在真机上撒谎。
    // 只有明确 failed 才算异常；unknown 是「查不到」（开发机没有 systemctl），
    // 不能算坏，否则本地一打开就是满屏红。
    const bad = SERVICES.filter((v) => v.s === "failed").length;
    const allUnk = SERVICES.every((v) => v.s === "unknown");
    svcSummary.className = "chip " + (bad ? "danger" : allUnk ? "neutral" : "ok");
    svcSummary.innerHTML = '<i class="dot"></i>' +
      (allUnk ? "状态不可用" : bad ? bad + " 项异常" : SERVICES.length + " 项全部正常");
    // 只有全挂才给「去运行自检」：单挂一两项不值得打断视线
    document.getElementById("svc-hint").hidden = allUnk || bad !== SERVICES.length;
    // 顶栏那颗「系统正常」也归这里管：服务全挂还说正常，是在骗人
    const top = document.getElementById("top-status");
    const allDown = bad === SERVICES.length && !allUnk;
    top.className = "chip " + (allDown ? "danger" : bad ? "warn" : allUnk ? "neutral" : "ok");
    const topTxt = allDown ? "系统异常" : bad ? "部分异常" : allUnk ? "状态不可用" : "系统正常";
    top.innerHTML = '<i class="dot live"></i>' + topTxt;
    top.title = topTxt;   // 手机上正常状态只显示一颗点，文字靠这里
  }
  renderServices();
  document.getElementById("btn-svc-refresh").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    if (btn.disabled) return;
    // 按钮先闭上嘴，否则连点会排队发请求
    btn.disabled = true;
    btn.classList.add("is-busy");
    const label = btn.textContent;
    btn.textContent = "检测中…";
    await refreshState();
    btn.disabled = false;
    btn.classList.remove("is-busy");
    btn.textContent = label;
    toast(S2.online ? "已重新检测 " + SERVICES.length + " 项服务" : "读取失败：" + S2.lastErr,
      S2.online ? "ok" : "err");
  });

  /* ---------- 刷新 ---------- */
  document.getElementById("btn-refresh").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.classList.add("spin");
    // 手动点刷新才带 ?force=1：用户明确要看最新的，值得多等一次采集（SMART 不唤醒休眠盘）；
    // 后台 60 秒轮询不带，老实用后端缓存，别把每一次自动轮询都变成一次强制重采
    await Promise.all([refreshState(), fetchHwinfo(true)]);
    setTimeout(() => btn.classList.remove("spin"), 600);
    toast(S2.online ? "状态已刷新" : "读取失败：" + S2.lastErr, S2.online ? "ok" : "err");
  });

  /* ---------- 分段控件（通用） ---------- */
  document.querySelectorAll(".seg").forEach((seg) => {
    seg.addEventListener("click", (e) => {
      const btn = e.target.closest("button");
      if (!btn) return;
      [...seg.children].forEach((b) => b.setAttribute("aria-pressed", String(b === btn)));
    });
  });

  /* ---------- 风扇：三档温控曲线（概览卡正面切档、背面调锚点） ----------
     模型和固件 fan-control.sh 的 curve_duty() 完全一致：四个温度锚点 + 百分比线性插值。
         t <  T1        → 0%（停转）
         T1 ~ T2        → D1% → D2%
         T2 ~ T3        → D2% → D3%
         T3 ~ T4        → D3% → D4%
         t >= T4        → D4%（全速）
     四个点的温度和转速都可以改（起转转速最低 30%，转速逐点不降）。
     旧版风扇脚本的起转 30% / 全速 100% 是写死的，那种设备上这两格不可改。
     界面上只有一个转速数字：实际占空比。没有「挡位」这层中间量，
     就不会再出现「10 挡 / 85%」两个数互相打架的情况。 */

  // 出厂曲线，与 app/firmware/fan-control.conf 的默认值一致
  const FAN_PRESET = {
    quiet: {
      hdd: { t1: 42, t2: 48, t3: 52, t4: 55, d1: 30, d2: 30, d3: 70, d4: 100 },
      cpu: { t1: 60, t2: 65, t3: 72, t4: 80, d1: 30, d2: 30, d3: 70, d4: 100 },
    },
    performance: {
      hdd: { t1: 40, t2: 45, t3: 50, t4: 55, d1: 30, d2: 50, d3: 85, d4: 100 },
      cpu: { t1: 60, t2: 65, t3: 75, t4: 80, d1: 30, d2: 60, d3: 85, d4: 100 },
    },
  };
  const PROFILE_CN = { quiet: "静音", performance: "性能", custom: "自定义" };
  const FAN_KEYS = ["t1", "t2", "t3", "t4", "d1", "d2", "d3", "d4"];

  /* 硬盘 T4 天花板 55℃（原来是 65℃）。
     老设备的 /etc/fan-control.conf 里可能还写着 60 —— 直接显示会让用户
     「什么都没改，点保存却报错」。所以读进来先夹一次：只夹越界的锚点，
     并把前三个一起压下来，保证 t1<t2<t3<t4 仍然成立。 */
  const HDD_T4_MAX = 55;
  function clampHddCurve(a) {
    if (a.t4 <= HDD_T4_MAX) return a;
    return {
      ...a,
      t4: HDD_T4_MAX,
      t3: Math.min(a.t3, HDD_T4_MAX - 1),
      t2: Math.min(a.t2, HDD_T4_MAX - 2),
      t1: Math.min(a.t1, HDD_T4_MAX - 3),
    };
  }

  const ovFanSvg = document.getElementById("ov-fan-svg");
  const ovFanCard = ovFanSvg.closest(".flip-front");
  const ovFill = document.getElementById("ov-fan-fill");
  const ovDuty = document.getElementById("ov-fan-duty");
  const ovSource = document.getElementById("ov-fan-source");

  let fanCurve = JSON.parse(JSON.stringify(FAN_PRESET.quiet));
  let fanProfile = "quiet";
  // 用户动过曲线格子之后，轮询就不再拿后端配置覆盖它 —— 否则每 10 秒把正在编辑的值改回去
  let curveDirty = false;
  let fanTesting = false;   // 「全速 10 秒」进行中：挡住重复点击
  let fanTestGrace = 0, fanTestUntil = 0;
  let fanManualTarget = null;   // 正在拖 / 刚设的手动目标值：设备确认之前读数先按它显示

  // /api/state 的 conf.fan（KEY=VALUE 字符串）→ {hdd, cpu} 曲线；缺的格子用出厂值，硬盘 T4 夹到上限
  function curveFromConf(fan) {
    return {
      hdd: clampHddCurve({
        t1: num(fan.HDD_T1) || 42, t2: num(fan.HDD_T2) || 48,
        t3: num(fan.HDD_T3) || 52, t4: num(fan.HDD_T4) || 55,
        d1: num(fan.HDD_D1) || 30, d2: num(fan.HDD_D2) || 30,
        d3: num(fan.HDD_D3) || 70, d4: num(fan.HDD_D4) || 100,
      }),
      cpu: { t1: num(fan.CPU_T1) || 60, t2: num(fan.CPU_T2) || 65,
             t3: num(fan.CPU_T3) || 72, t4: num(fan.CPU_T4) || 80,
             d1: num(fan.CPU_D1) || 30, d2: num(fan.CPU_D2) || 30,
             d3: num(fan.CPU_D3) || 70, d4: num(fan.CPU_D4) || 100 },
    };
  }
  // T4 越界夹值的提示只弹一次
  let hddClamped = false;

  /* 与固件同一条算式。整数除法（Math.round 而不是保留小数）也要一致，
     否则界面上预览的曲线和风扇实际走的那条会差几个百分点。 */
  function curveDuty(t, a) {
    if (t === null || t === undefined) return null;
    if (t < a.t1) return 0;
    if (t >= a.t4) return a.d4;
    const lerp = (x, x0, x1, y0, y1) =>
      x1 === x0 ? y1 : Math.round(y0 + ((x - x0) * (y1 - y0)) / (x1 - x0));
    if (t < a.t2) return lerp(t, a.t1, a.t2, a.d1, a.d2);
    if (t < a.t3) return lerp(t, a.t2, a.t3, a.d2, a.d3);
    return lerp(t, a.t3, a.t4, a.d3, a.d4);
  }

  /* 叶轮转速跟着实际占空比走。30% → 2.2s/圈，100% → 0.75s/圈，线性；
     再快就是一团糊，再慢看着像卡住。0% 直接停。 */
  const fanPeriod = (duty) => (duty > 0 ? Math.max(0.75, 2.2 - ((duty - 30) / 70) * 1.45) : 0);

  /* 温度卡：数字 + 状态胶囊 + 刻度条 + 「较 N 分钟前」。
     刻度条的量程是 20–100℃（和下面的图例一致），所以填的是绝对位置而不是相对变化 ——
     以前写死 width:45% 那种，换台机器就是错的。
     预警/临界线按设备类型不同：机械盘 50℃ 就该预警，CPU 到 70℃ 才值得看一眼。

     概览页的 CPU / 硬盘温度卡已经被仪表盘（见下面「指针仪表」段）取代，
     ov-cpu-* / ov-hdd-* 这些节点在新版 index.html 里不存在了。这个函数
     不再被调用，但留着做无害的空壳：万一哪里还有遗留调用，四个节点
     一个取不到就直接返回，不读写 null，也不会让 applyState() 中途炸掉。 */
  function applyTempCard(kind, v, warn, danger) {
    const chip = document.getElementById("ov-" + kind + "-chip");
    const numEl = document.getElementById("ov-" + kind + "-temp");
    const fill = document.getElementById("ov-" + kind + "-fill");
    const delta = document.getElementById("ov-" + kind + "-delta");
    if (!chip || !numEl || !fill || !delta) return;
    if (v === null) {
      numEl.textContent = "--";
      fill.style.width = "0%";
      chip.className = "chip neutral";
      chip.innerHTML = '<i class="dot"></i>无数据';
      delta.textContent = "等待采样";
      return;
    }
    numEl.textContent = v;
    fill.style.width = Math.max(0, Math.min(100, ((v - 20) / 80) * 100)) + "%";
    const lvl = v >= danger ? "danger" : v >= warn ? "warn" : "ok";
    chip.className = "chip " + lvl;
    chip.innerHTML = '<i class="dot"></i>' +
      (lvl === "danger" ? "过高" : lvl === "warn" ? "偏高" : "正常");

    // 「较 N 分钟前」用缓冲里最早一个有该字段读数的样本，和真实经过的时间——
    // 不再假设每点固定 10 秒：采样间隔可由用户改（见 §6 的 pollSec）。
    const key = kind === "cpu" ? "cpu" : "hdd";
    const first = TREND.find((p) => has(p[key]));
    if (first && TREND.length >= 2) {
      const diff = +(v - first[key]).toFixed(1);
      const spanSec = Math.round((Date.now() - first.t) / 1000);
      const span = spanSec >= 60 ? Math.round(spanSec / 60) + " 分钟" : spanSec + " 秒";
      delta.textContent = "较 " + span + "前 " + (diff > 0 ? "+" : diff < 0 ? "−" : "±") +
        Math.abs(diff) + "°";
    } else {
      delta.textContent = "采样中…";
    }
  }

  /* 风扇转速是谁触发的：两路曲线（处理器 / 硬盘）取高的那一路。
     概览卡的 ov-fan-source 和仪表盘 g-fan 的副标题说的是同一件事，
     抽成一个函数，两处永远一致，不会一个说「处理器触发」另一个说「单路采样中」。 */
  function fanSourceText(cpu, hdd, sensor) {
    const s = sensor || {};
    const duty = num(s.fan_duty);
    // 控制器自己报的状态优先：保护 / 测试 / 手动时转速不是曲线决定的
    if (s.fan_mode === "protect") return "保护全速" + (s.fan_guard ? " · " + s.fan_guard : "");
    if (s.fan_mode === "test") {
      const left = Math.ceil((fanTestUntil - Date.now()) / 1000);
      return "全速测试中" + (left > 0 ? " · 剩 " + left + " 秒" : "");
    }
    if (s.fan_mode === "manual") {
      const m = num(s.fan_manual);
      return "手动调速" + (duty !== null && m !== null && duty > m ? " · 温度偏高，已按曲线提速" : "");
    }
    if (cpu === null && hdd === null) return "等待采样";
    const cpuD = curveDuty(cpu, fanCurve.cpu) || 0;
    const hddD = curveDuty(hdd, fanCurve.hdd) || 0;
    if (!cpuD && !hddD) {
      // 两路都没到起转点却在转：硬盘温度读不到时保底转速，或降温时的 2℃ 回差
      if (duty > 0) return s.fan_source === "unknown" ? "硬盘温度未知 · 保底 " + duty + "%" : "降温回差 · 保持 " + duty + "%";
      return "未达起转温度";
    }
    const by = cpuD >= hddD ? "处理器" : "硬盘";
    const t = cpuD >= hddD ? cpu : hdd;
    return by + " " + Math.round(t) + "℃ 触发";
  }

  /* 把一次实时采样画到界面上。sensor 来自 /api/sensor，字段可能是空串（没装固件）。 */
  function applyFanReading(sensor) {
    // 全速测试刚发出的头几秒，控制器还没采样到：先按测试中显示，别被旧读数打回去
    if (fanTesting && Date.now() < fanTestGrace && sensor.fan_mode !== "test" && sensor.fan_mode !== "protect") {
      sensor = Object.assign({}, sensor, { fan_duty: 100, fan_mode: "test" });
    }
    // 手动滑块刚设了目标值、设备还没确认：轮询拉回来的旧转速不能把读数拽回去，否则拖动时数字来回跳
    if (fanManualTarget !== null && (fanManualBusy || Date.now() < fanManualHold) && sensor.fan_mode !== "protect" && sensor.fan_mode !== "test"
        && num(sensor.fan_manual) !== fanManualTarget) {
      sensor = Object.assign({}, sensor, { fan_duty: fanManualTarget, fan_mode: "manual", fan_manual: String(fanManualTarget) });
    }
    const duty = num(sensor.fan_duty);
    const cpu = num(sensor.cpu_temp);
    const hdd = num(sensor.hdd_max_temp) || null;

    // 概览卡：只写实际值，拿不到就写 --
    ovDuty.textContent = duty === null ? "--" : duty;
    ovFill.style.width = (duty || 0) + "%";
    // 说清楚这个转速是谁决定的：两路曲线取高的那一路
    ovSource.textContent = fanSourceText(cpu, hdd, sensor);

    // 叶轮周期只写在叶轮自己身上：写在 :root 上会让整页样式跟着重算一遍
    const spin = (fanPeriod(duty || 0) || 2.4).toFixed(2) + "s";
    if (ovFanSvg.dataset.spin !== spin) { ovFanSvg.dataset.spin = spin; ovFanSvg.style.setProperty("--spin", spin); }
    ovFanSvg.classList.toggle("still", !duty);
    renderFanRows(cpu, hdd);
    syncFanManual(sensor);

    // 转速分档着色（叶轮 + 读数 + 进度条一起变）：起转 30% 起算，
    // <60% 慢速绿、60–89% 中速橙、≥90% 全速红，停转 / 没数据灰。只在档位变化时改 class。
    const lvl = !duty ? "" : duty >= 90 ? "lv-hi" : duty >= 60 ? "lv-mid" : "lv-lo";
    if (ovFanCard.dataset.lv !== lvl) {
      ovFanCard.classList.remove("lv-lo", "lv-mid", "lv-hi");
      if (lvl) ovFanCard.classList.add(lvl);
      ovFanCard.dataset.lv = lvl;
      ovFanSvg.setAttribute("aria-label", !duty ? "风扇停转" : lvl === "lv-hi" ? "风扇全速" : lvl === "lv-mid" ? "风扇中速" : "风扇低速");
    }
  }

  /* 表格 → fanCurve。返回错误信息（空串=合法），和 op_set_fan 的校验一一对应。 */
  function readCurveInputs() {
    const next = { hdd: {}, cpu: {} };
    document.querySelectorAll(".curve tr[data-curve]").forEach((tr) => {
      const k = tr.dataset.curve;
      tr.querySelectorAll("input").forEach((inp) => {
        next[k][inp.dataset.k] = parseInt(inp.value, 10);
      });
    });
    const bad = [];
    // 提示语里的「起转点/第二点/第三点/全速点」要跟 index.html 温控曲线表头的中文列名对上——
    // 用户是照着那张表填数字的，错误提示说「T2」而表头写「第二点」，两边就对不上号
    const chk = (label, a, tMin, tMax) => {
      FAN_KEYS.forEach((f) => {
        if (!Number.isInteger(a[f])) bad.push(label + " 有空值或非整数");
      });
      if (bad.length) return;
      if (a.t1 < tMin) bad.push(label + " 起转点不能低于 " + tMin + "℃");
      if (a.t2 <= a.t1) bad.push(label + " 第二点必须大于起转点");
      if (a.t3 <= a.t2) bad.push(label + " 第三点必须大于第二点");
      if (a.t4 <= a.t3 || a.t4 > tMax) bad.push(label + " 全速点必须大于第三点且 ≤ " + tMax + "℃");
      if (a.d1 < 30 || a.d1 > 100) bad.push(label + " 起转转速须在 30–100%");
      if (a.d2 < a.d1) bad.push(label + " 第二点转速不能低于起转转速");
      if (a.d3 < a.d2) bad.push(label + " 第三点转速不能低于第二点");
      if (a.d4 < a.d3 || a.d4 > 100) bad.push(label + " 全速点转速必须 ≥ 第三点且 ≤ 100%");
    };
    chk("硬盘", next.hdd, 30, 55);
    chk("处理器", next.cpu, 40, 80);
    return { curve: next, err: bad[0] || "" };
  }

  /* 正面两行：每一路的当前温度、曲线锚点、按曲线算出的需求转速，标出起决定作用的那一路。 */
  const fanRowEls = ["cpu", "hdd"].map((k) => ({
    k, row: document.querySelector('.fan-row[data-src="' + k + '"]'),
    t: document.getElementById("ov-fan-" + k + "-t"),
    a: document.getElementById("ov-fan-" + k + "-a"),
    d: document.getElementById("ov-fan-" + k + "-d"),
  }));
  let fanRowTemps = { cpu: null, hdd: null };
  function renderFanRows(cpu, hdd) {
    if (cpu !== undefined) fanRowTemps = { cpu, hdd };
    const need = {};
    fanRowEls.forEach((e) => {
      const t = fanRowTemps[e.k], a = fanCurve[e.k];
      need[e.k] = curveDuty(t, a);
      e.t.textContent = t === null ? "--" : Math.round(t) + "℃";
      e.a.textContent = a.t1 + " / " + a.t2 + " / " + a.t3 + " / " + a.t4 + "℃";
      e.a.title = "四个锚点对应转速 " + a.d1 + " / " + a.d2 + " / " + a.d3 + " / " + a.d4 + "%";
      e.d.textContent = need[e.k] === null ? "无读数" : need[e.k] === 0 ? "未达起转" : "需 " + need[e.k] + "%";
    });
    const lead = need.cpu === null && need.hdd === null ? "" : (need.cpu || 0) >= (need.hdd || 0) ? "cpu" : "hdd";
    fanRowEls.forEach((e) => e.row.classList.toggle("is-lead", e.k === lead && need[e.k] > 0));
  }

  function renderCurveInputs() {
    renderFanRows();
    document.querySelectorAll(".curve tr[data-curve]").forEach((tr) => {
      const a = fanCurve[tr.dataset.curve];
      tr.querySelectorAll("input").forEach((inp) => {
        inp.value = a[inp.dataset.k];
      });
    });
  }

  /* ---------- 风扇卡：正面切档位，背面调曲线 ----------
     这张卡是双面的。正面右上角三个档位，背面是四锚点表。
     翻面用 inert 把当前看不见的那一面从 Tab 顺序和读屏里摘掉 ——
     只是 backface-visibility:hidden 的话，键盘一按 Tab 就会跳进背面那些输入框，
     屏幕上却什么都没有。 */
  const fanCard = document.getElementById("ov-fan");
  const ovFanMode = document.getElementById("ov-fan-mode");
  const flipFront = fanCard.querySelector(".flip-front");
  const flipBack = fanCard.querySelector(".flip-back");
  const errBox = document.getElementById("curve-err");
  const backBtn = document.getElementById("curve-back");
  let fanBusy = false;

  // iOS Safari 15.4 之前没有 inert：赋值不报错但什么都不做，背面的输入框
  // 依然挡在 Tab 顺序里，读屏也照样会念到它们。手动给每个可聚焦子节点
  // 扣 tabindex=-1 + aria-hidden 做同样的事；真支持 inert 的浏览器用原生的，
  // 不叠加两套，否则恢复时还要记住「这个 tabindex 到底是不是我自己加的」。
  const HAS_INERT = "inert" in HTMLElement.prototype;
  const FOCUSABLE_SEL = "a[href],button,input,select,textarea,[tabindex]";
  function setInertFallback(el, on) {
    el.querySelectorAll(FOCUSABLE_SEL).forEach((node) => {
      if (on) {
        if (!node.hasAttribute("data-prev-tabindex")) {
          node.setAttribute("data-prev-tabindex", node.getAttribute("tabindex") || "");
        }
        node.setAttribute("tabindex", "-1");
        node.setAttribute("aria-hidden", "true");
      } else {
        const prev = node.getAttribute("data-prev-tabindex");
        if (prev === "") node.removeAttribute("tabindex");
        else if (prev !== null) node.setAttribute("tabindex", prev);
        node.removeAttribute("data-prev-tabindex");
        node.removeAttribute("aria-hidden");
      }
    });
  }

  function flipFan(back, moveFocus) {
    fanCard.classList.toggle("is-back", back);
    if (HAS_INERT) {
      flipFront.inert = back;
      flipBack.inert = !back;
    } else {
      setInertFallback(flipFront, back);
      setInertFallback(flipBack, !back);
    }
    // 焦点得跟着翻过去。不这么做的话，焦点留在刚被 inert 掉的按钮上，
    // 浏览器直接把它扔回 body —— 键盘用户按一下 Tab 就从页首重新开始。
    // 初始化那次不抢焦点，所以由调用方决定要不要移动。
    if (!moveFocus) return;
    const target = back ? backBtn : ovFanMode.querySelector('button[aria-pressed="true"]');
    if (target) target.focus({ preventScroll: true });
  }

  function setFanProfile(p) {
    fanProfile = p;
    [...ovFanMode.children].forEach((b) =>
      b.setAttribute("aria-pressed", String(b.dataset.profile === p))
    );
  }

  /* 静音 / 性能 立刻写盘生效，自定义 翻到背面调锚点。
     以前是「点档位 → 载入预设 → 再点保存」两步，中间那段「看起来已经改了」
     的窗口最容易出事：用户以为存了，其实没存。 */
  ovFanMode.addEventListener("click", async (e) => {
    const b = e.target.closest("button");
    if (!b || fanBusy) return;
    const p = b.dataset.profile;

    if (p === "custom") {
      // 这里不改 fanProfile：点「自定义」只是把卡翻过去看看，设备上跑的还是原来那一档。
      // 真正落到自定义是「改了某个格子」那一刻（下面的 input 监听）。
      // 但通用分段控件那条监听已经把按钮点亮了，所以要按真实档位再刷一次，
      // 否则翻过去又翻回来，按钮会停在一个没生效的档位上
      flipFan(true, true);
      setFanProfile(fanProfile);
      return;
    }
    if (!FAN_PRESET[p]) return;

    fanCurve = JSON.parse(JSON.stringify(FAN_PRESET[p]));
    renderCurveInputs();
    errBox.hidden = true;
    setFanProfile(p);
    fanBusy = true;
    ovFanMode.classList.add("is-busy");
    const res = await hw("set_fan", { tempProfile: p, hdd: fanCurve.hdd, cpu: fanCurve.cpu });
    fanBusy = false;
    ovFanMode.classList.remove("is-busy");
    if (res.ok) {
      curveDirty = false;
      toast("已切到" + PROFILE_CN[p] + "曲线");
      fanFollowUp();
    } else {
      toast(res.msg || "切换失败", "err");
    }
    // 成不成功都以设备上的实际配置为准：失败时按钮不能停在没生效的那一档上
    refreshState();
  });

  /* 切档 / 保存曲线之后：先按新曲线和当前温度在页面上算出转速立刻显示（不等 10 秒轮询），
     再每秒拉一次真实读数、拉 4 次，看到设备上的实际占空比变过来。固件 0.25 秒内就会响应。 */
  let fanFollowTimer = 0;
  function fanFollowUp() {
    const s = S2.sensor || {};
    const cpu = num(s.cpu_temp), hdd = num(s.hdd_max_temp);
    const a = curveDuty(cpu, fanCurve.cpu), b = curveDuty(hdd, fanCurve.hdd);
    const guess = a === null && b === null ? null : Math.max(a || 0, b || 0);
    if (guess !== null) applyFanReading(Object.assign({}, s, { fan_duty: guess }));
    clearTimeout(fanFollowTimer);
    let n = 0;
    const tick = () => {
      if (++n > 4 || document.hidden) return;
      pollSensorOnly();
      fanFollowTimer = setTimeout(tick, 1000);
    };
    fanFollowTimer = setTimeout(tick, 400);
  }

  document.getElementById("curve-back").addEventListener("click", () => {
    // 没保存就返回：撤销改动，回到设备上实际在跑的曲线和档位。
    // 不撤销的话档位一直显示「自定义」、轮询也不再同步，直到刷新页面。
    // 填了非法值（报错但没算成改动）同样要还原，不能留在格子里
    const touched = curveDirty || !errBox.hidden;
    const fan = S2.state && S2.state.conf && S2.state.conf.fan;
    curveDirty = false;
    errBox.hidden = true;
    if (fan) { fanCurve = curveFromConf(fan); setFanProfile(fan.TEMP_PROFILE || "custom"); }
    renderCurveInputs();
    if (touched && fan) toast("未保存的改动已撤销，风扇仍按原曲线运行");
    flipFan(false, true);
  });

  // 手改任一格就自动落到「自定义」  // 手改任一格就自动落到「自定义」：改完数值档位还写着「静音」是在撒谎
  document.querySelectorAll(".curve input").forEach((inp) => {
    inp.addEventListener("input", () => {
      const r = readCurveInputs();
      if (r.err) {
        errBox.textContent = r.err;
        errBox.hidden = false;
        return;
      }
      errBox.hidden = true;
      fanCurve = r.curve;
      curveDirty = true;
      setFanProfile("custom");
    });
  });

  document.getElementById("curve-reset").addEventListener("click", () => {
    fanCurve = JSON.parse(JSON.stringify(FAN_PRESET.quiet));
    setFanProfile("quiet");
    renderCurveInputs();
    errBox.hidden = true;
    // 只填表格、不落盘：用户可能是想从默认值改起
    curveDirty = true;
    toast("已填回出厂曲线（静音），点「保存并生效」才会写入设备");
  });

  async function saveFanCurve() {
    const r = readCurveInputs();
    if (r.err) {
      errBox.textContent = r.err;
      errBox.hidden = false;
      toast(r.err, "err");
      return;
    }
    fanCurve = r.curve;
    const btn = document.getElementById("curve-save");
    btn.disabled = true;
    const res = await hw("set_fan", { tempProfile: fanProfile, hdd: fanCurve.hdd, cpu: fanCurve.cpu });
    btn.disabled = false;
    toast(res.msg || (res.ok ? "曲线已保存" : "保存失败"), res.ok ? "ok" : "err");
    // 存进去了就不再算「本地脏数据」，让轮询重新接管（顺便确认真的落盘了）
    if (res.ok) {
      curveDirty = false;
      // 存完翻回正面：背面没有任何「现在风扇多少转」的反馈，
      // 转回去正好让新转速出现在眼前
      flipFan(false, true);
      fanFollowUp();
      refreshState();
    }
  }
  document.getElementById("curve-save").addEventListener("click", saveFanCurve);

  /* 滑动手动转速：边拖边生效。拖动时界面立刻跟手（读数、进度条、叶轮先按目标值显示），
     请求按「最新值优先」发：上一条没回来就不排队，回来后只补发最后停在的那个值，
     所以不会积压，也不用等松手。设备上的真实状态以 /api/sensor 的 fan_mode / fan_manual 为准，
     拖动中和刚停手的几秒内不让轮询把滑块拽回去。 */
  const fanManualBox = document.getElementById("ov-fan-manual");
  const fanSlider = document.getElementById("fan-slider");
  const fanSliderVal = document.getElementById("fan-slider-val");
  const fanAutoBtn = document.getElementById("fan-auto");
  const AUTO = "auto";   // 目标值要么是 30–100 的手动转速，要么是「回到温控曲线」
  // 轨道刻度固定 0–100%（滑块位置和上面的转速条对得上），但最低只能设到 30%：再往左拖停在 30%
  const FAN_MANUAL_MIN = 30;
  let fanManualOn = false, fanManualHold = 0, fanManualWant = null, fanManualSent = null, fanManualBusy = false, fanManualSentAt = 0;
  // 轨道的填充长度由 --p 决定（全站滑块共用的样式），值一变就要跟着写
  function paintFanSlider() {
    fanSlider.style.setProperty("--p", fanSlider.value + "%");
  }
  function showFanManual(on, duty) {
    fanManualOn = on;
    fanManualBox.classList.toggle("is-on", on);
    // 「恢复自动」一直在原位，自动时只是置灰：按钮和数字的位置固定，轨道宽度才不会变
    fanAutoBtn.disabled = !on;
    if (on && duty !== null && duty !== undefined) fanSlider.value = duty;
    paintFanSlider();
    // 百分比只在这一格显示，永远是数字、等宽右对齐：不在「自动」和数字之间来回换，也不在别处重复
    fanSliderVal.textContent = fanSlider.value + "%";
  }
  function syncFanManual(sensor) {
    const usable = sensor.fan_manual_ok !== false && !(S2.access && S2.access.danger === false);
    fanSlider.disabled = !usable;
    fanSlider.title = usable ? "" : sensor.fan_manual_ok === false
      ? "当前由旧版风扇脚本控制，完成设备树升级启用原生控制后才能手动调速" : "设置访问口令后才能手动调速";
    // 旧版风扇脚本的起转 / 全速转速是写死的，这两格只在原生控制下可改
    document.querySelectorAll('.curve input[data-k="d1"], .curve input[data-k="d4"]').forEach((inp) => {
      inp.disabled = sensor.fan_manual_ok === false;
      inp.title = inp.disabled ? "旧版风扇脚本不支持修改起转和全速转速" : "";
    });
    if (Date.now() < fanManualHold || fanManualBusy) return;
    const manual = num(sensor.fan_manual);
    const on = sensor.fan_mode === "manual" || (manual !== null && manual >= FAN_MANUAL_MIN);
    // 自动时滑块停在当前实际转速上，拖动就是从现在的转速起调
    if (!on) fanSlider.value = Math.max(0, Math.min(100, num(sensor.fan_duty) || 0));
    showFanManual(on, manual);
  }
  // 发送泵：同一时刻只有一条请求在路上，回来后如果目标值又变了就再发一条
  async function pumpFanManual() {
    if (fanManualBusy) return;
    fanManualBusy = true;
    let res = { ok: true }, last = null;
    while (res.ok && fanManualWant !== null && fanManualWant !== fanManualSent) {
      // 第一条立刻发，之后每条至少隔 80 毫秒：手感上仍是连续的，设备一秒最多收十来条
      const wait = fanManualSentAt + 80 - Date.now();
      if (wait > 0) await new Promise((r) => setTimeout(r, wait));
      last = fanManualWant;
      fanManualSentAt = Date.now();
      res = await hw("fan_manual", last === AUTO ? { auto: true } : { percent: last });
      if (res.ok) fanManualSent = last;
    }
    fanManualBusy = false;
    fanManualWant = null;
    if (!res.ok) {
      fanManualHold = 0;
      fanManualSent = null;
      toast(res.msg || "设置手动转速失败", "err");
      pollSensorOnly();
      return;
    }
    if (last === null) return;
    fanManualHold = Date.now() + 2500;      // 控制器 0.25 秒内执行，留一个读数周期再让轮询接管
    if (last === AUTO) {
      fanManualSent = null;
      toast("已恢复温控曲线");
    }
    clearTimeout(fanFollowTimer);
    let n = 0;
    const tick = () => { if (++n > 3 || document.hidden) return; pollSensorOnly(); fanFollowTimer = setTimeout(tick, 1000); };
    fanFollowTimer = setTimeout(tick, 600);
  }
  function setFanManual(want) {
    fanManualWant = want;
    fanManualTarget = want === AUTO ? null : want;
    fanManualHold = Date.now() + 5000;
    if (want === AUTO) {
      showFanManual(false);
    } else {
      showFanManual(true, want);
      applyFanReading(Object.assign({}, S2.sensor || {}, { fan_duty: want, fan_mode: "manual", fan_manual: String(want) }));
    }
    pumpFanManual();
  }
  fanSlider.addEventListener("input", () => {
    const want = Math.max(FAN_MANUAL_MIN, parseInt(fanSlider.value, 10) || 0);
    if (String(want) !== fanSlider.value) fanSlider.value = want;
    setFanManual(want);
  });
  fanAutoBtn.addEventListener("click", () => setFanManual(AUTO));

  /* 全速测试：后端写 force_until，固件 3 秒内把风扇拉到 100%，到期自己回温控。
     不再改曲线、不重启服务 —— 以前靠「把 CPU 曲线压到当前温度以下」来骗出全速，
     但 T1 有 40℃ 下限，设备凉的时候根本够不到，风扇反而停了；还要在结束时把曲线写回去。 */
  const FAN_TEST_SEC = 10;
  document.getElementById("fan-test").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    if (fanTesting) return;
    const label = btn.textContent;
    fanTesting = true;
    btn.disabled = true;
    const res = await hw("fan_test", { sec: FAN_TEST_SEC });
    if (!res.ok) {
      fanTesting = false;
      btn.disabled = false;
      toast(res.msg || "全速测试失败", "err");
      return;
    }
    toast("风扇全速运行 " + FAN_TEST_SEC + " 秒，之后自动回到原来的曲线");
    // 控制器 2–3 秒才采样一次：这段时间拉回来的还是旧转速，不能让它把读数打回去
    fanTestGrace = Date.now() + 3500;
    fanTestUntil = Date.now() + FAN_TEST_SEC * 1000;
    // 背面没有转速读数，测试时翻回正面才看得到风扇在全速；正在改曲线就不打断
    if (!curveDirty) flipFan(false, true);
    applyFanReading(S2.sensor || {});
    let responded = false;
    for (let left = FAN_TEST_SEC; left > 0; left--) {
      btn.textContent = "全速中（" + left + "）";
      await new Promise((r) => setTimeout(r, 1000));
      await pollSensorOnly();
      const s = S2.sensor || {};
      if (s.fan_mode === "test" || s.fan_mode === "protect" || num(s.fan_duty) >= 95) responded = true;
      if (!responded && left === FAN_TEST_SEC - 5) {
        toast("风扇控制器没有响应全速测试，请到「控制 → 脚本库」运行「00-硬件自检」", "err");
      }
      applyFanReading(s);
    }
    fanTesting = false;
    fanTestGrace = fanTestUntil = 0;
    btn.textContent = label;
    btn.disabled = false;
    fanFollowUp();
    if (responded) toast("全速测试结束，风扇回到原来的曲线");
    refreshState();
  });

  setFanProfile("quiet");
  renderCurveInputs();
  flipFan(false);

  /* 视图切出来之后要补算的东西。
     隐藏状态下 clientWidth 是 0，曲线只能退回默认宽度画一遍，
     等真正显示出来必须按实际宽度重画一次 —— 否则整张图会被拉伸一倍。 */
  onViewShown(() => {
    const cur = document.querySelector(".nav-item[aria-current='page']");
    const view = cur && cur.dataset.view;
    if (view === "overview") {
      drawSparks();
      drawChart(true);
    }
  });

  /* ---------- 指示灯 ---------- */
  // 状态只存在 LED 里（定义在下面的「指示灯」段），分段控件和圆点都从它渲染。
  // 档位取自按钮的 data-mode，不再靠文案比对 —— 「呼吸」和「全灭」都是两个字，
  // 拿 textContent 判断档位迟早会认错。
  const LED_MODE_TOAST = { auto: "已恢复自动", breathe: "已切到休眠呼吸", off: "已强制全灭" };
  document.querySelectorAll("[data-led]").forEach((seg) => {
    seg.addEventListener("click", async (e) => {
      const btn = e.target.closest("button");
      if (!btn) return;
      const k = seg.dataset.led;
      const mode = btn.dataset.mode;
      if (!mode || seg.classList.contains("is-busy")) return;
      // 先发请求再改本地状态：写失败了界面上就不该显示「已全灭」。
      // 通用分段控件的点击监听已经先把按钮点亮了，失败时要按真实状态重画回去
      seg.classList.add("is-busy");
      const res = await hw("set_led_override", k === "pwr" ? { power: mode } : { sata: mode });
      seg.classList.remove("is-busy");
      if (!res.ok) {
        renderLed();
        toast(res.msg || "设置失败", "err");
        return;
      }
      LED[k].mode = mode;
      renderLed();
      toast(LED[k].name + (LED_MODE_TOAST[mode] || "已更新"));
    });
  });

  /* ---------- 蜂鸣器 ----------
     只剩概览页那张迷你卡：点数字原地换成滑块，松手即落盘。
     原来控制页还有一张「先试后存」的完整卡，和这张说的是同一件事 ——
     两个地方都能改同一个值时，用户改了一边、另一边不跟着变，就没人知道该信哪个。 */
  const BEEP_MIN = 30, BEEP_MAX = 5000;   // 与后端 beep() / op_set_beep 的 30–5000ms 对齐
  const beepPct = (v) => ((v - BEEP_MIN) / (BEEP_MAX - BEEP_MIN)) * 100 + "%";
  let beepSaved = 280;
  // 正在拖 / 刚落盘还没等到回包：这期间不让轮询把手底下的数字改回去
  let beepDirty = false;

  function syncBeep() {
    // 概览卡展示的是「已保存的默认值」，不是滑块上正在试的值
    const numEl = document.getElementById("ov-beep-num");
    numEl.textContent = beepSaved;
    // 读屏只会念出「280 按钮」，得补一句点了会怎样，否则看不出这是个可拖动的入口
    numEl.setAttribute("aria-label", "蜂鸣时长 " + beepSaved + " 毫秒，点击可拖动调节");
    document.getElementById("ov-beep-long").textContent = "鸣叫 " + beepSaved + " ms";
  }

  // 试听直接落到硬件上：真机有蜂鸣器，没装的时候后端会回一句明确的失败原因
  async function beepTest(ms) {
    const res = await hw("beep_test", { ms: ms });
    toast(res.ok ? "已鸣叫 " + ms + " ms" : (res.msg || "蜂鸣失败"), res.ok ? "ok" : "err");
  }
  // 试听用的是「已保存的默认值」
  document.getElementById("ov-beep-test").addEventListener("click", () => beepTest(beepSaved));

  /* ---------- 概览卡上直接拖蜂鸣时长 ----------
     点数字 → 脚注那行原地换成滑块，松手即落盘。改错了再拖回来就行，
     不需要「先试后存」两步 —— 这个值没有试听才准的场景，试听按钮本来就单列。
     拖动期间大数字实时跟着走，试听按钮也用当前值，所以松手前就能听出来。
     量程常量与 beepPct() 在上面「蜂鸣器」段统一定义，这里不再重复一份。 */
  const ovBeepNum = document.getElementById("ov-beep-num");
  const ovBeepVal = ovBeepNum.parentElement;
  const ovBeepFoot = document.getElementById("ov-beep-foot");
  const ovBeepSlide = document.getElementById("ov-beep-slide");
  const ovBeepRange = document.getElementById("ov-beep-ov");

  function openBeepSlide() {
    if (!ovBeepSlide.hidden) return;
    ovBeepRange.value = beepSaved;
    ovBeepRange.style.setProperty("--p", beepPct(beepSaved));
    ovBeepFoot.hidden = true;
    ovBeepSlide.hidden = false;
    ovBeepVal.classList.add("is-editing");
    ovBeepRange.focus();
  }
  function closeBeepSlide(refocus) {
    if (ovBeepSlide.hidden) return;
    ovBeepSlide.hidden = true;
    ovBeepFoot.hidden = false;
    ovBeepVal.classList.remove("is-editing");
    if (refocus) ovBeepNum.focus();
  }

  ovBeepRange.addEventListener("input", () => {
    const v = +ovBeepRange.value;
    ovBeepRange.style.setProperty("--p", beepPct(v));
    beepSaved = v;
    syncBeep();
  });
  /* 松手才落盘：拖动过程中每一格都发一次请求没有意义，而且会拖着串口等回复。
     这里必须真的写设备 —— 以前只改本地变量、靠轮询再读回来，等于「数字变了但没生效」，
     而这张卡现在是唯一能改蜂鸣时长的地方，更不能只在屏幕上改。 */
  let beepSaveTimer = 0;
  ovBeepRange.addEventListener("change", () => {
    beepDirty = true;
    // 键盘方向键每按一下都触发 change：合并成停手后的一次写入
    clearTimeout(beepSaveTimer);
    beepSaveTimer = setTimeout(saveBeep, 250);
  });
  async function saveBeep() {
    const v = beepSaved;
    const res = await hw("set_beep", { ms: v });
    if (v !== beepSaved) return;      // 期间又调了一次，以后面那次为准
    beepDirty = false;
    if (!res.ok) {
      toast(res.msg || "保存失败", "err");
      refreshState();       // 写失败就以设备上的值为准，别把数字停在没生效的地方
      return;
    }
    toast("蜂鸣时长已改为 " + v + " ms");
  }
  // 点到别处就收起，不抢焦点；Esc/Enter 收起并把焦点还回数字
  ovBeepRange.addEventListener("blur", () => closeBeepSlide(false));
  ovBeepRange.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === "Escape") {
      e.preventDefault();
      closeBeepSlide(true);
    }
  });
  ovBeepNum.addEventListener("click", openBeepSlide);

  /* ---------- 定时开关机：只有概览页那张迷你卡 ----------
     这里曾经有一整页「计划」：可拖动的时间轴、计划状态、执行记录。
     撤掉那页之后，启停、两个时间、倒计时全在这张迷你卡里 —— 它本来就能做这三件事，
     完整卡片只是同一件事的第二份界面，而两份界面迟早会不一致。
     状态只存一份 SCH，DOM 只是它的投影。 */
  const SCH = { on: true, off: "03:00", boot: "08:00", extended: false, next: null, maintenance: 0 };

  function minutes(t) {
    const [h, m] = (t || "00:00").split(":").map(Number);
    return h * 60 + (m || 0);
  }
  // 「今天/明天 + 还有多久」——下次触发永远在将来，过点就顺延到明天
  function nextRun(hhmm) {
    const deviceClock = clockSkew !== null && clockTz !== null;
    const now = new Date(Date.now() + (deviceClock ? clockSkew + clockTz * 1000 : 0));
    const nowMin = deviceClock ? now.getUTCHours() * 60 + now.getUTCMinutes()
      : now.getHours() * 60 + now.getMinutes();
    const target = minutes(hhmm);
    const today = target > nowMin;
    return {
      day: today ? "今天" : "明天",
      mins: today ? target - nowMin : 1440 - nowMin + target,
    };
  }
  // 跟顶栏「已运行」同一种写法（「23小时 58分」），同一屏不出现两种时长格式
  function humanGap(m) {
    return fmtUptime(m * 60);
  }

  const ovSchOn = document.getElementById("ov-sch-on");
  const ovSchOff = document.getElementById("ov-sch-off");
  const ovSchBoot = document.getElementById("ov-sch-boot");

  function renderSchedule() {
    ovSchOn.checked = SCH.on;
    ovSchOff.textContent = SCH.off || "未设置";
    ovSchBoot.textContent = SCH.boot || "未设置";
    // 时间是可点的，无障碍名必须带上「点了能改」和当前值，否则读屏只会念出一个时间
    ovSchOff.setAttribute("aria-label", "修改计划关机时间，当前 " + SCH.off);
    ovSchBoot.setAttribute("aria-label", "修改计划开机时间，当前 " + SCH.boot);
    document.getElementById("ov-sch-gap").textContent =
      !SCH.on ? "已停用" : SCH.maintenance > (Date.now() + (clockSkew || 0)) / 1000 ? "维护中"
      // 刚改过、设备还没回新的排期时按本地时间先算一个，别显示成「未安排」等几秒
      : SCH.extended && !schDirty ? (SCH.next ? humanGap(Math.max(0, Math.ceil((SCH.next * 1000 - Date.now() - (clockSkew || 0)) / 60000))) : "未安排")
      : SCH.off ? humanGap(nextRun(SCH.off).mins) : "未设置关机时间";
    document.getElementById("ov-sch-card").classList.toggle("is-off", !SCH.on);
  }

  /* 用户刚改过、请求还没回来 —— 这期间不许轮询把旧值写回去。
     没有这个闸门时，改完时间等 10 秒就自己弹回原值，用户会以为输入框坏了。 */
  let schDirty = false;
  let schSaving = false;

  // 设备最后一次确认接受的计划：保存失败时退回到这里，而不是退回到某一次点击之前
  let schConfirmed = null, schQueued = null;

  function syncScheduleControls() {
    // 保存途中不锁控件：再改一次就排队，等这次回来后把最新的值再发一遍
    [ovSchOn, ovSchOff, ovSchBoot].forEach((button) => {
      button.disabled = !!(S2.access && S2.access.danger === false);
    });
  }

  /* 把 SCH 整份写到后端。界面先变、请求后发；失败时退回设备确认过的值 ——
     界面停在后端并不认的值，比不让改更糟：设备上根本不是这个值。 */
  async function saveSchedule(okMsg) {
    schDirty = true;
    if (schSaving) { schQueued = okMsg || ""; return; }
    schSaving = true;
    syncSchBusy();
    let res, msg = okMsg;
    do {
      if (schQueued !== null) msg = schQueued;
      schQueued = null;
      const sent = { on: SCH.on, off: SCH.off, boot: SCH.boot };
      res = await hw("set_power_schedule", { enabled: sent.on, shutdown_time: sent.off, boot_time: sent.boot });
      if (res.ok) schConfirmed = sent;
    } while (res.ok && schQueued !== null);
    schSaving = schDirty = false;
    schQueued = null;
    if (!res.ok) {
      syncSchBusy();
      if (schConfirmed) Object.assign(SCH, schConfirmed);
      renderSchedule();
      toast(res.msg || "保存失败", "err");
      refreshState();
      return;
    }
    schApplying = true;          // 设备已收下，后台还在落到系统服务：等待状态接着亮到它做完
    syncSchBusy();
    toast(msg || res.msg || "已保存");
    schWatch(0);
  }
  /* 设备收到计划后立即回应，系统服务和硬件时钟在后台跟上。这里跟到它做完：
     服务状态及时更新，后台应用失败（设备会退回上一次生效的计划）也能马上提示。 */
  let schWatchTimer = 0, schErrShown = "", schApplying = false, schBusyTimer = 0;
  function schWatch(n) {
    clearTimeout(schWatchTimer);
    refreshState().then(() => {
      const ps = S2.state && S2.state.conf && S2.state.conf.power_schedule;
      if (ps && ps.applying && n < 20 && !document.hidden) schWatchTimer = setTimeout(() => schWatch(n + 1), 800);
      else { schApplying = false; syncSchBusy(); }
    });
  }
  /* 等待状态：请求在路上或设备还在后台应用时，开关旁边转圈并写「正在应用…」。
     0.25 秒内做完的不显示，免得每点一下都闪一下。 */
  function syncSchBusy() {
    const busy = schSaving || schApplying;
    const el = document.getElementById("ov-sch-busy");
    const card = document.getElementById("ov-sch-card");
    clearTimeout(schBusyTimer);
    if (!busy) { el.hidden = true; card.classList.remove("is-applying"); return; }
    if (!el.hidden) return;
    schBusyTimer = setTimeout(() => { el.hidden = false; card.classList.add("is-applying"); }, 250);
  }
  /* 概览卡上的开关：就地生效，直接写后端。
     以前这里只改本地 checked 再弹一句「已启用」，其实一个字节都没落盘 ——
     10 秒后的轮询把它弹回原状，用户会以为开关坏了。小卡片上的操作
     （指示灯、蜂鸣时长）本来就是即时生效，这里跟它们保持一致。 */
  ovSchOn.addEventListener("change", async (e) => {
    const want = e.target.checked;
    SCH.on = want;
    schDirty = true;       // 先标脏再画：倒计时按本地时间先算，不等设备回新的排期
    renderSchedule();
    await saveSchedule(want ? "定时计划已启用" : "定时计划已停用");

  });

  /* ---------- 概览卡上的时间就地编辑 ----------
     点数字换成原生 time 输入，改完写回 SCH 再统一 renderSchedule()。
     不另存一份状态：卡片上的文字和 SCH 必须始终是同一个值。 */
  function editTime(kind, btn) {
    if (btn.disabled || btn.dataset.editing === "1") return;
    const wrap = btn.parentElement;

    const input = document.createElement("input");
    input.type = "time";
    input.value = SCH[kind];
    input.setAttribute("aria-label", kind === "off" ? "计划关机时间" : "计划开机时间");

    btn.dataset.editing = "1";
    btn.hidden = true;
    wrap.insertBefore(input, btn);
    input.focus();
    // 能直接弹原生选择器就弹，弹不出来（Safari / 无手势）也不影响手输
    if (input.showPicker) {
      try { input.showPicker(); } catch (e) {}
    }

    let done = false;
    const finish = (commit, refocus) => {
      if (done) return;
      done = true;
      const v = input.value;
      input.remove();
      btn.hidden = false;
      delete btn.dataset.editing;
      // 只在「焦点因为元素被移除而掉到 body」时回焦；用户点到别处时别把焦点抢回来
      const lost = !document.activeElement || document.activeElement === document.body;
      if (refocus && lost) btn.focus();
      if (!commit || !v || v === SCH[kind]) return;
      // 关机与开机同一时刻：时段宽度变成 0，设备上等于一整天都在关机区间里
      if (v === (kind === "off" ? SCH.boot : SCH.off)) {
        toast("关机与开机不能是同一时间", "err");
        return;
      }
      // 卡片上没有「保存」按钮了（计划页撤掉时一并删了），所以改完立刻落盘 ——
      // 只改本地再等下一次轮询，等于改了个寂寞
      SCH[kind] = v;
      schDirty = true;
      renderSchedule();
      saveSchedule((kind === "off" ? "计划关机" : "计划开机") + "已改为 " + v);

    };

    // 移动端选完直接 change，桌面端失焦才触发；done 保证只结算一次。
    // change 也回焦：输入框被移除后焦点会掉到 body，键盘用户接着按 Tab 会从头开始；
    // 但用户点到别处时 activeElement 已经是别的控件，lost 为 false，焦点不会被抢回来
    input.addEventListener("change", () => finish(true, true));
    input.addEventListener("blur", () => finish(true, false));
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); finish(true, true); }
      else if (e.key === "Escape") { e.preventDefault(); finish(false, true); }
    });
  }

  document.querySelectorAll("[data-edit-time]").forEach((btn) => {
    const kind = btn.dataset.editTime;
    btn.addEventListener("click", () => editTime(kind, btn));
  });
  renderSchedule();
  /* ---------- 指示灯：概览卡上的分段控件与圆点共用一份状态 ---------- */
  // mode 三态：auto（固件自动）/ breathe（盘休眠时呼吸）/ off（强制全灭）。
  // 电源灯没有 breathe —— 它由网络状态灯接管，呼吸对一条链路状态灯没有意义。
  const LED = {
    pwr: { name: "电源灯", auto: "跟随网络链路：连通绿灯、断开红灯", mode: "auto" },
    dsk: { name: "硬盘灯", auto: "跟随硬盘读写闪烁", mode: "auto" },
  };

  function renderLed() {
    Object.keys(LED).forEach((k) => {
      const v = LED[k];
      // 圆点与分段控件一律按 data 属性找，不写死 id —— 同一份状态可能有好几处
      // 渲染点，写死一处就会漏掉另一处
      document.querySelectorAll('[data-led-dot="' + k + '"]').forEach((dot) => {
        dot.className = "led-dot " +
          (v.mode === "off" ? "grey" : v.mode === "breathe" ? "violet" : "green");
        // 「为什么是绿灯」那句话原来写在控制页那张卡的状态行里。卡撤掉之后
        // 挂在圆点的 title 上：概览卡一行放不下，但鼠标停一下就能看到。
        dot.title = v.mode === "off" ? "手动覆盖：已强制全灭"
          : v.mode === "breathe" ? "休眠呼吸：盘休眠时绿灯 2 秒亮 / 2 秒灭，活动时常亮"
          : v.auto;
      });
      document.querySelectorAll('.seg[data-led="' + k + '"]').forEach((seg) => {
        [...seg.children].forEach((btn) =>
          btn.setAttribute("aria-pressed", String(btn.dataset.mode === v.mode))
        );
      });
    });
    // 概览卡上的汇总：全灭最优先，其次呼吸，最后才是「全部自动」
    const offN = Object.keys(LED).filter((k) => LED[k].mode === "off").length;
    const br = LED.dsk.mode === "breathe";
    const chip = document.getElementById("ov-led-chip");
    if (chip) {
      chip.className = "chip " + (offN ? "warn" : "neutral");
      chip.textContent = offN ? offN + " 盏已全灭" : br ? "硬盘灯呼吸中" : "全部自动";
    }
  }
  renderLed();

  /* ---------- 脚本 + 终端 ---------- */
  const termLog = document.getElementById("term-log");
  const termStatus = document.getElementById("term-status");
  const termTarget = document.getElementById("term-target");
  /* 选中项与「日志卡头写谁的名字」是两件事：
       selectedScript —— 左侧列表高亮哪一行（只管列表，不碰卡头）
       卡头 #term-target —— 只写「这份日志是哪个脚本跑出来的」，所以只有真的运行了才写
     以前选中、轮询重渲染 renderScripts() 都会往卡头写名字，首次打开页面一行日志都没有，
     卡头却挂着「00-部署全部硬件配置.sh」，看起来像跑过一次。现在默认是空的（CSS 把空标签收掉），
     运行脚本时写入，清空日志时抹掉。 */
  let selectedScript = null;
  function setTermTarget(name) {
    termTarget.textContent = name || "";
  }
  let running = false;
  let runTimer = null;
  let runJob = null;

  function stamp() {
    const d = new Date();
    return (
      '<span class="ts">[' +
      String(d.getHours()).padStart(2, "0") + ":" +
      String(d.getMinutes()).padStart(2, "0") + ":" +
      String(d.getSeconds()).padStart(2, "0") +
      "]</span> "
    );
  }

  // 输出按行渲染成 DOM，不走 innerHTML —— 脚本能往 stdout 写任意内容，
  // 包括 <script> 或 <img onerror>，拼字符串等于把 root 的输出当代码执行。
  function termWrite(text, cls) {
    const line = document.createElement("div");
    if (cls) {
      const span = document.createElement("span");
      span.className = cls;
      span.textContent = text;
      line.appendChild(span);
    } else {
      line.textContent = text;
    }
    termLog.appendChild(line);
    termLog.scrollTop = termLog.scrollHeight;
  }

  function termReset(msg) {
    termLog.textContent = "";
    if (msg) termWrite(msg, "dim");
  }

  // 空终端就是「还没跑过」，不摆一段编好的输出冒充历史
  function seedTerminal() {
    termReset("// 选一个脚本点「运行」，输出会实时出现在这里");
    termStatus.textContent = "就绪";
    termStatus.style.color = "";
  }

  // 运行期间把脚本行里的「运行」按钮整体置灰：以前第二次点击被静默吞掉，
  // 用户只会以为按钮坏了。日志卡头已经没有运行按钮，这里别再去够一个不存在的 id。
  function setRunning(on) {
    running = on;
    scriptList.querySelectorAll(".run").forEach((b) => {
      b.disabled = on;
      b.classList.toggle("is-busy", on);
    });
  }

  function stopPolling() {
    clearTimeout(runTimer);
    runTimer = null;
    runJob = null;
  }

  // 常见退出码的人话：只翻几个固定含义的，其余只报数字
  function rcHint(rc) {
    return ({ 1: "", 2: "，参数或用法错误", 124: "，运行超时", 126: "，脚本没有执行权限", 127: "，找不到命令",
      130: "，被中断", 137: "，被系统强行结束", 143: "，被终止" })[rc] || "";
  }

  function reviewScript(info) {
    const dialog = document.createElement("dialog");
    dialog.className = "script-review";
    dialog.setAttribute("aria-labelledby", "script-review-title");
    const previous = document.activeElement;
    const node = (tag, value, className = "") => { const el = document.createElement(tag); el.textContent = value; el.className = className; return el; };
    const head = node("div", "", "sr-head");
    head.append(node("span", "执行前确认", "sr-eyebrow"));
    const title = node("h2", info.name.replace(/^\d+-/, "").replace(/\.sh$/, "")); title.id = "script-review-title";
    head.append(title, node("p", info.source + " · " + (info.changed ? "首次执行或内容已更新" : "与上次批准的版本一致"), "sr-meta"));
    const body = node("div", "", "sr-body"), footer = node("div", "", "sr-footer");
    dialog.append(head, body, footer);
    const risks = Array.isArray(info.risks) ? info.risks : [];
    const impact = node("div", "", "sr-impact" + (risks.length ? " has-risk" : ""));
    impact.append(node("h3", risks.length ? "本次操作会修改系统" : "运行内容"));
    if (risks.length) {
      const list = node("ul", ""); risks.forEach(risk => list.append(node("li", risk))); impact.append(list);
    } else impact.append(node("p", "请确认下面的脚本内容后再执行。"));
    body.append(impact);
    const details = node("details", "", "sr-details");
    details.append(node("summary", "查看脚本内容与校验信息"), node("p", info.name, "sr-filename"), node("pre", info.content || ""));
    if (info.diff && info.changed) {
      const diff = node("details", "", "sr-diff"); diff.append(node("summary", "查看与上次批准版本的差异"), node("pre", info.diff)); details.append(diff);
    }
    details.append(node("span", "SHA256", "sr-hash-label"), node("code", info.sha256, "sr-hash")); body.append(details);
    const options = node("div", "", "sr-options");
    const timeoutLabel = node("label", "最长执行时间（秒）", "sr-field");
    const timeout = document.createElement("input"); Object.assign(timeout, {type: "number", min: "10", max: "3600", step: "1", value: "600", required: true});
    timeout.setAttribute("aria-label", "最长执行时间（秒）"); timeoutLabel.append(timeout, node("small", "10–3600 秒，超时后终止任务")); options.append(timeoutLabel);
    const acknowledgement = document.createElement("input");
    if (risks.length) {
      const label = node("label", "确认系统改动", "sr-field");
      Object.assign(acknowledgement, {placeholder: "输入 yes", autocomplete: "off", spellcheck: false});
      acknowledgement.setAttribute("aria-label", "确认脚本风险"); label.append(acknowledgement, node("small", "输入 yes 表示已了解本次操作影响")); options.append(label);
    }
    body.append(options);
    const cancel = node("button", "取消", "btn"), start = node("button", "确认执行", "btn primary");
    cancel.type = start.type = "button";
    const validity = () => { start.disabled = (risks.length > 0 && acknowledgement.value !== "yes") || !timeout.validity.valid; };
    acknowledgement.oninput = validity; timeout.oninput = validity; validity();
    footer.append(node("span", "仅执行本次确认的版本", "sr-footnote"), cancel, start);
    document.body.append(dialog); dialog.showModal(); (risks.length ? acknowledgement : cancel).focus();
    return new Promise(resolve => {
      let done = false;
      const finish = value => { if (done) return; done = true; dialog.close(); dialog.remove(); if (previous?.isConnected) previous.focus(); resolve(value); };
      cancel.onclick = () => finish(null); dialog.oncancel = event => { event.preventDefault(); finish(null); };
      start.onclick = () => { const seconds = Number(timeout.value); if (risks.length && acknowledgement.value !== "yes") return; if (Number.isInteger(seconds) && seconds >= 10 && seconds <= 3600) finish({timeout: seconds, ...(risks.length ? {risk_ack: info.sha256, risk_confirm: "yes"} : {})}); else timeout.reportValidity(); };
    });
  }

  async function runScript(name, factory = false) {
    if (running) {
      toast("已有脚本在运行，等它结束再试", "err");
      return;
    }
    let inspected;
    try { inspected = (await apiGet("/api/script/inspect?name=" + encodeURIComponent(name) + (factory ? "&factory=1" : ""))).data; }
    catch (error) { toast(error.message, "err"); return; }
    const approved = await reviewScript(inspected);
    if (!approved) return;
    setRunning(true);
    setTermTarget(name);
    termStatus.textContent = "运行中";
    termStatus.style.color = "var(--term-warn)";
    termReset("$ " + name);

    /* 手机上脚本库与执行日志是上下两摞，点「运行」后输出在 500px 之外，
       用户看不到任何东西在动，会以为按钮没生效。窄屏把日志带到眼前；
       桌面两张卡并排可见，不滚。scroll-margin-top 在 CSS 里避开粘顶栏。 */
    if (window.matchMedia("(max-width:760px)").matches) {
      const termCard = document.querySelector(".term-card");
      if (termCard) termCard.scrollIntoView({ behavior: "smooth", block: "start" });
    }

    const res = await apiPost("/api/script/start", { name: name, sha256: inspected.sha256, factory: inspected.factory === true, ...approved });
    if (!res.ok) {
      termWrite(res.msg || "启动失败", "err");
      termStatus.textContent = "失败";
      termStatus.style.color = "var(--term-err)";
      setRunning(false);
      return;
    }
    followJob(res.job);
  }

  // 跟随一个正在设备上跑的任务，把输出接到执行日志。启动脚本和「页面重开后接回」共用。
  function followJob(job, cancellable) {
    runJob = job;
    // 内置模块的安装任务不能中途终止（没有可终止的进程组），不摆一个点了必然失败的按钮
    termStop.hidden = cancellable === false;
    termStop.disabled = false;
    let seen = 0, missFrom = 0, missNoted = false;
    const finish = () => { termStop.hidden = true; setRunning(false); stopPolling(); };
    const poll = async () => {
      if (!runJob) return;
      let r;
      try {
        r = await apiGet("/api/script/poll?job=" + encodeURIComponent(runJob));
      } catch (err) {
        // 脚本可能重启了控制台自己（部署脚本会 restart 一堆服务），脚本本身还在跑。
        // 连不上就继续等，2 分钟都连不上才放弃；恢复后从断开处接着显示
        if (!missFrom) missFrom = Date.now();
        if (Date.now() - missFrom < 120000) {
          if (!missNoted) { termWrite("与控制台的连接中断，正在重连…（脚本仍在设备上运行）", "dim"); missNoted = true; }
          runTimer = setTimeout(poll, 2000);
          return;
        }
        termWrite("读取输出失败：" + err.message + "。脚本可能仍在设备上运行，稍后刷新页面再看", "err");
        termStatus.textContent = "失联";
        termStatus.style.color = "var(--term-err)";
        finish();
        return;
      }
      if (missFrom) { termWrite("已重新连上", "dim"); missFrom = 0; missNoted = false; }
      if (!runJob) return;
      const lines = r.lines || [];
      // lines 只是最后 400 行，total 是总行数：新行 = total - seen，从 lines 尾部取
      const total = typeof r.total === "number" ? r.total : lines.length;
      const fresh = Math.min(lines.length, Math.max(0, total - seen));
      for (let i = lines.length - fresh; i < lines.length; i++) termWrite(lines[i]);
      seen = Math.max(seen, total);
      if (r.done) {
        const ok = r.rc === 0;
        const unknown = r.rc === null || r.rc === undefined;
        // 退出码拿不到：控制台在脚本运行期间重启过，且脚本在那之前就结束了。不能说「失败」，
        // 上面的输出就是全部结果，让用户自己判断
        termWrite(unknown ? "脚本已结束，但控制台中途重启过，没有拿到退出码；以上就是脚本的全部输出"
          : ok ? "执行完成（退出码 0）" : "执行失败（退出码 " + r.rc + rcHint(r.rc) + "）",
          unknown ? "dim" : ok ? "ok" : "err");
        termStatus.textContent = unknown ? "已结束" : ok ? "已完成" : r.rc === 130 ? "已停止" : "失败";
        termStatus.style.color = unknown ? "var(--term-warn)" : ok ? "var(--term-ok)" : "var(--term-err)";
        finish();
        return;
      }
      runTimer = setTimeout(poll, 700);
    };
    poll();
  }

  // 停止：让设备上的监督进程结束整个脚本进程组；结果（「任务已取消」+ 退出码 130）仍由轮询显示
  const termStop = document.getElementById("term-stop");
  termStop.addEventListener("click", async () => {
    if (!runJob || termStop.disabled) return;
    termStop.disabled = true;
    const res = await apiPost("/api/script/cancel", { id: runJob });
    if (!res.ok) {
      // 设备确认不了这个任务（已结束、或进程早就不在了）：不再跟随，否则运行按钮会一直灰着没有出路
      termWrite((res.msg || "停止失败") + "，已不再跟随这个任务", "err");
      termStatus.textContent = "已放弃";
      termStatus.style.color = "var(--term-err)";
      termStop.hidden = true;
      setRunning(false);
      stopPolling();
      return;
    }
    toast("已请求停止，等待脚本退出");
  });

  // 页面重开后把还在跑的任务接回来：否则脚本在设备上继续跑，界面却显示「就绪」还能再点运行
  async function reattachJob() {
    let jobs;
    try { jobs = (await apiGet("/api/script/jobs")).data; } catch (e) { return; }
    const live = Array.isArray(jobs) ? jobs.filter((j) => j && !j.done && j.id) : [];
    if (!live.length || running) return;
    const job = live[live.length - 1];
    setRunning(true);
    setTermTarget(job.name || "");
    termStatus.textContent = "运行中";
    termStatus.style.color = "var(--term-warn)";
    termReset("$ " + (job.name || "任务") + "　（已接回正在运行的任务）");
    followJob(job.id, job.cancellable !== false);
  }

  /* ---------- 脚本库 ---------- */
  // 真实清单来自 /api/state 的 scripts 字段（后端扫 var/scripts 与共享目录）
  let SCRIPTS = [];
  const scriptList = document.getElementById("script-list");
  const scriptCount = document.getElementById("script-count");
  const dropzone = document.getElementById("dropzone");

  function fmtSize(n) {
    return n >= 1024 ? (n / 1024).toFixed(1) + " KB" : (n || 0) + " B";
  }
  function fmtTime(ts) {
    if (!ts) return "—";
    const d = new Date(ts * 1000);
    const now = new Date();
    const hm = String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
    if (d.toDateString() === now.toDateString()) return "今天 " + hm;
    return (d.getMonth() + 1) + " 月 " + d.getDate() + " 日 " + hm;
  }

  function renderScripts() {
    // 数量只报一处：卡片头这颗胶囊。上传、删除、10 秒轮询都走这里重算，
    // 所以数字永远等于列表里真实看得见的行数，不另设一份会漂移的计数。
    scriptCount.textContent = SCRIPTS.length + " 个";
    scriptCount.title = "脚本库里可见的脚本总数：" + SCRIPTS.length;
    // 一个脚本都没有时，空态自己就是拖放目标，别让上面再挂一个同义的大框
    dropzone.hidden = SCRIPTS.length === 0;
    // 服务卡那条提示的出口跟着走：没有脚本时「去运行自检」是空头支票
    document.getElementById("svc-hint-go").textContent = SCRIPTS.length ? "去运行自检" : "去上传脚本";
    if (!SCRIPTS.length) {
      // 空态本身就是「选择文件」按钮，用 <button> 而不是 role="button"：
      // 后者键盘按 Enter 不会触发任何东西，用户会卡在这里
      setHtmlIfChanged(scriptList,
        '<button class="empty drop-target" id="script-empty" type="button">' +
          '<span class="ico"><svg aria-hidden="true"><use href="#i-upload"/></svg></span>' +
          '<span class="t">脚本库是空的</span>' +
          '<span class="d">把 <b>.sh</b> 脚本或 <b>.run</b> 安装包拖进来，或点这里选择。脚本以管理员权限执行，只放你自己确认过的文件。</span>' +
        "</button>");
      selectedScript = null;
      return;
    }
    /* 重渲染不能把选中项踹回第一行：原来按 i===0 定激活行，而 10 秒轮询、
       上传、删除都会重渲染 —— 用户刚选好的脚本会自己跳回第一个。按脚本名
       恢复（存在 selectedScript 里，不再借卡头那行字），名字没了才回落第一行。
       日志被清空后卡头保持空白，轮询只恢复左侧选中态，不把名字写回来。 */
    const prev = scriptList.querySelector(".script.is-active");
    const keep = prev ? prev.dataset.script : selectedScript;
    const active = SCRIPTS.some((s) => s.name === keep) ? keep : SCRIPTS[0].name;
    // 一行 = 选中按钮 + 查看 / 删除 / 运行 三颗真按钮。以前整行是 role="button"，
    // 里面又塞了真按钮，读屏会念成「按钮 里 按钮」，而且 Enter 在整行上会直接
    // 执行 root 脚本 —— 太危险。查看与删除不选中、不运行，避免误触派生操作。
    const html = SCRIPTS.map((s) => {
      // 名字要过两遍 esc：一次进 data-script 属性，一次进可见文本。
      // 少了前者，带引号的文件名能把属性截断；少了后者，带尖括号的能塞标签进来。
      // API 调用（运行/查看/删除）一律用 s.name 这个完整文件名，只是「主文字」去掉扩展名——
      // 扩展名本身不是「话」，是给 API 用的值，界面上用类型标签「脚本/安装包」说明同一件事。
      const nm = esc(s.name);
      const dispNm = esc(s.name.replace(/\.(sh|run)$/i, ""));
      const on = s.name === active;
      return '<div class="script' + (on ? " is-active" : "") + '" data-script="' + nm + '">' +
        '<button class="pick" type="button"' + (on ? ' aria-current="true"' : "") + ">" +
          '<span class="ico"><svg aria-hidden="true"><use href="#i-file"/></svg></span>' +
          '<span class="pick-text">' +
            '<span class="nm" title="' + nm + '">' +
              '<span class="nm-t">' + dispNm + "</span>" +
              '<span class="tag">' + (s.run ? "安装包" : "脚本") + "</span>" +
            "</span>" +
            '<span class="meta">' + fmtSize(s.size) + " · " + fmtTime(s.mtime) + "</span>" +
          "</span>" +
        "</button>" +
        '<div class="row-acts">' +
          '<button class="btn sm ghost act-view" type="button" title="查看 ' + nm + ' 的内容（只读）">查看</button>' +
          '<button class="btn sm ghost act-del" type="button" title="删除 ' + nm + '（需二次确认）">删除</button>' +
          '<button class="btn sm run" type="button">运行</button>' +
        "</div>" +
      "</div>";
    }).join("");
    setHtmlIfChanged(scriptList, html);
    selectedScript = active;
    markScriptScroll();
  }

  /* 列表真能滚的时候才加 .is-scroll —— 底边淡出是个「还有更多」的提示，
     3 个脚本、根本没超高的时候它也淡出，就变成把最后一行无端削掉一半。
     阈值留 8px：行间分割线改成贴边后，6 个脚本恰好只高出 5px，那点余量
     滚动条几乎拖不动，却会把最后一行压暗 —— 提示「还有更多」不该在
     「其实没什么可滚」的时候出现。放在下一帧量：innerHTML 刚写完时
     布局还没落定，scrollHeight 会是旧值。 */
  function markScriptScroll() {
    requestAnimationFrame(() => {
      scriptList.classList.toggle("is-scroll", scriptList.scrollHeight > scriptList.clientHeight + 8);
    });
  }
  renderScripts();

  /* ---- 查看 ----
     只读预览，不提供编辑：脚本以 root 身份执行，页面上留一个能改文件的框，
     等于把 root 权限包成一段 textarea。内容一律走 textContent，避免脚本里的
     尖括号被当成 HTML。.run 是「shell 头 + tar.gz 负载」，服务端只回可读的
     头部并带 note/binary 标记，这里如实说明而不是把乱码倒给用户。 */
  const viewer = document.getElementById("script-viewer");
  const viewerTitle = document.getElementById("viewer-title");
  const viewerNote = document.getElementById("viewer-note");
  const viewerBody = document.getElementById("viewer-body");
  const viewerClose = document.getElementById("viewer-close");
  let viewerOpener = null;

  function restoreViewerFocus() {
    if (viewerOpener && viewerOpener.isConnected) viewerOpener.focus({ preventScroll: true });
    viewerOpener = null;
  }
  function closeViewer() {
    if (typeof viewer.close === "function" && viewer.open) viewer.close();
    else viewer.removeAttribute("open");
    viewer.classList.remove("no-modal");
    restoreViewerFocus();
  }
  viewer.addEventListener("close", restoreViewerFocus);
  viewerClose.addEventListener("click", closeViewer);
  // 点到 dialog 自己（即 ::backdrop 区域）才算点遮罩；内容区冒泡上来时 target 不会是 dialog
  viewer.addEventListener("click", (e) => { if (e.target === viewer) closeViewer(); });
  // 老 WebKit 没有 showModal()，走 no-modal 兜底时补一个 Esc
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && viewer.open && viewer.classList.contains("no-modal")) closeViewer();
  });

  async function viewScript(name, btn) {
    let data;
    try {
      data = await apiGet("/api/script/get?name=" + encodeURIComponent(name));
    } catch (e) {
      toast("读取脚本失败：" + ((e && e.message) || "网络错误"), "err");
      return;
    }
    if (!data.ok) { toast(data.msg || "读取脚本失败", "err"); return; }
    let content = data.content || "";
    let note = data.note || "只读预览 · 脚本以管理员权限执行，运行前请确认内容";
    if (/\.run$/i.test(name)) {
      /* 老版服务端会把整个 .run 按文本读出来，负载是一堆替换字符和控制字节。
         前端这边再截一次，新旧后端都只显示负载之前的可读 shell 头 —— 改后端
         不该以「必须重启用户正在用的服务」为前提。 */
      const head = [];
      for (const line of content.split("\n")) {
        if (/[\u0000-\u0008\u000e-\u001f\ufffd]/.test(line)) break;
        head.push(line);
        if (head.length >= 200) break;
      }
      content = head.join("\n").replace(/\s+$/, "") + "\n";
      note = "模块安装包是自解压格式：下面只有 shell 头，压缩负载是二进制，不展示。确认内容后点「运行」安装。";
    }
    viewerTitle.textContent = name;
    viewerNote.textContent = note;
    viewerBody.textContent = content || "（空文件）";
    viewerBody.scrollTop = 0;
    viewerOpener = btn || null;
    if (typeof viewer.showModal === "function") viewer.showModal();
    else { viewer.setAttribute("open", ""); viewer.classList.add("no-modal"); }
    viewerClose.focus();
  }

  /* ---- 删除 ----
     删掉的是真文件，两步确认沿用「卸载模块」的做法：第一次点只把按钮变成
     红色「确认删除」，5 秒不点自动还原。同一时刻只留一颗待确认的按钮，
     免得手一抖连点两行、把两个脚本都送走。 */
  function disarmDelete(btn) {
    if (!btn || !btn.isConnected) return;
    clearTimeout(btn._armTimer);
    btn.dataset.armed = "0";
    btn.textContent = "删除";
    btn.classList.remove("armed");
  }

  async function deleteScript(row, btn) {
    const name = row.dataset.script;
    if (btn.dataset.armed !== "1") {
      scriptList.querySelectorAll(".act-del.armed").forEach((b) => {
        if (b !== btn) disarmDelete(b);
      });
      btn.dataset.armed = "1";
      btn.textContent = "确认删除";
      btn.classList.add("armed");
      btn._armTimer = setTimeout(() => disarmDelete(btn), 5000);
      return;
    }
    clearTimeout(btn._armTimer);
    btn.disabled = true;
    btn.textContent = "删除中…";
    const res = await apiPost("/api/script/delete", { name: name });
    disarmDelete(btn);
    btn.disabled = false;
    toast(res.msg || (res.ok ? "已删除" : "删除失败"), res.ok ? "ok" : "err");
    if (!res.ok) return;
    // 用服务端回的新清单，而不是本地 splice —— 共享目录、_common.sh 过滤
    // 都只在后端算得准，顺带让胶囊数字跟着减一
    SCRIPTS = Array.isArray(res.scripts) ? res.scripts : SCRIPTS.filter((s) => s.name !== name);
    renderScripts();
  }

  function selectScript(row) {
    scriptList.querySelectorAll(".script").forEach((r) => {
      r.classList.remove("is-active");
      r.querySelector(".pick").removeAttribute("aria-current");
    });
    row.classList.add("is-active");
    row.querySelector(".pick").setAttribute("aria-current", "true");
    // 重新选中脚本就是新动作的起点，卡头重新显示它
    setTermTarget(row.dataset.script);
  }

  /* 上传：一个隐藏的 file input 反复用，不在 DOM 里堆一堆 input。
     服务端只收原始字节，文件名走 ?name=，所以前端不需要 base64 撑大三分之一。 */
  // iOS Safari 的文件选择器把 accept 里的扩展名翻译成它认识的 MIME 类型，
  // 翻不出来的（.sh、.run 都不是系统注册的类型）整块变灰，用户连「我知道
  // 自己在干什么，选一下」都做不到。真正的校验在下面 uploadFile() 里按
  // 扩展名和大小做，accept 只是个「少点几次」的提示，iOS 上宁可不提示，
  // 也不能让选择器直接把人挡在门外。
  const IS_IOS = /iP(hone|ad|od)/.test(navigator.userAgent) ||
    (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  const filePicker = document.createElement("input");
  filePicker.type = "file";
  if (!IS_IOS) filePicker.accept = ".sh,.run";
  filePicker.hidden = true;
  document.body.appendChild(filePicker);

  async function uploadFile(file) {
    if (!file) return;
    if (!/\.(sh|run)$/i.test(file.name)) {
      toast("只接受维护脚本或模块安装包", "err");
      return;
    }
    // .run 自解压安装包和后端 MAX_RUN_BYTES 对齐放到 4MB；.sh 仍是 256KB。
    // 之前这里对所有文件一刀切 256KB，把后端明明放行的 .run 模块包全挡在了门外，
    // 等于让「拖 .run 进来装模块」这个核心功能对任何正常大小的包都失效。
    const isRun = /\.run$/i.test(file.name);
    const limit = isRun ? 4 * 1024 * 1024 : 256 * 1024;
    if (file.size > limit) {
      toast("文件过大（上限 " + (limit / 1024) + " KB）", "err");
      return;
    }
    const buf = await file.arrayBuffer();
    const res = await apiPost("/api/script/upload?name=" + encodeURIComponent(file.name), null, buf);
    toast(res.ok ? "已上传 " + file.name : (res.msg || "上传失败"), res.ok ? "ok" : "err");
    if (res.ok) {
      SCRIPTS = res.scripts || SCRIPTS;
      renderScripts();
    }
  }

  filePicker.addEventListener("change", () => {
    uploadFile(filePicker.files && filePicker.files[0]);
    filePicker.value = "";
  });

  // 恢复默认脚本：只补缺的，同名脚本（包括用户改过的）原样保留
  document.getElementById("script-defaults").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    if (btn.disabled) return;
    btn.disabled = true;
    const res = await apiPost("/api/script/defaults", {});
    btn.disabled = false;
    if (!res.ok) { toast(res.msg || "恢复默认脚本失败", "err"); return; }
    if (Array.isArray(res.scripts)) { SCRIPTS = res.scripts; renderScripts(); }
    const added = (res.added || []).length;
    toast(added ? "已补回 " + added + " 个默认脚本" : "默认脚本都在，没有需要补回的");
  });

  // 委托到容器：重渲染后不用重新绑定。四颗按钮都是真按钮，键盘行为交给浏览器
  scriptList.addEventListener("click", (e) => {
    if (e.target.closest("#script-empty")) {
      filePicker.click();
      return;
    }
    const row = e.target.closest(".script");
    if (!row) return;
    const view = e.target.closest(".act-view");
    if (view) { viewScript(row.dataset.script, view); return; }
    const del = e.target.closest(".act-del");
    if (del) { deleteScript(row, del); return; }
    if (e.target.closest(".run")) {
      selectScript(row);
      runScript(row.dataset.script);
    } else if (e.target.closest(".pick")) {
      selectScript(row);
    }
  });

  /* 服务全挂时那条提示的出口：切到「控制」页并把自检脚本选好。
     不写死文件名 —— 出厂脚本从 08- 改成 40- 之后，这里还找旧名字，
     实测点下去只会弹「没找到 08-自检并修复.sh」，一个永远落空的出口。
     按用途找：先认出厂名，再退到名字里带「自检 / selfheal」的自定义脚本。 */
  document.getElementById("svc-hint-go").addEventListener("click", () => {
    go("control");
    const rows = Array.from(scriptList.querySelectorAll(".script"));
    const target = rows.find((r) => r.dataset.script === "00-硬件自检.sh") ||
      rows.find((r) => /自检|self[-_]?heal/i.test(r.dataset.script));
    if (target) {
      selectScript(target);
      // 硬件与工具合成一页之后，脚本库不再是页首那张卡 —— 焦点带了 preventScroll，
      // 只聚焦不滚动的话，选中的那一行还在屏幕外，用户看不到任何反馈
      target.scrollIntoView({ block: "center" });
      target.querySelector(".pick").focus({ preventScroll: true });
      toast("已选好自检脚本，确认后点运行");
      return;
    }
    // 脚本库是空的，切过去也选不出东西，别假装成功
    const empty = document.getElementById("script-empty");
    if (empty) { empty.focus({ preventScroll: true }); toast("脚本库还是空的，先上传自检脚本"); }
    else toast("没找到自检修复脚本，请先上传一个");
  });

  /* 评审用：?demo=empty 直接看空态，不用改代码。
     初始清空之外，applyState() 每次刷新都会尊重 DEMO_EMPTY —— 否则第一次
     refreshState() 就把空态覆盖回真实数据了。 */
  if (DEMO_EMPTY) {
    SCRIPTS.length = 0;
    SERVICES.forEach((v) => (v.s = "failed"));
    renderScripts();
    renderServices();
  }

  /* 日志卡头不再挂「运行」：脚本行里每颗「运行」都先 selectScript() 再跑，
     目标明确；卡头那颗只能猜「上次选的是谁」，清空日志之后更会指向一个
     已经不存在的输出。运行入口只留一处，就不必再维护第二套目标解析。 */
  /* 复制：内网多半是 http + 局域网 IP，不是安全上下文，navigator.clipboard 用不了，
     必须留 execCommand 兜底，否则在真机上点了没反应 */
  document.getElementById("term-copy").addEventListener("click", async () => {
    const text = termLog.textContent;
    // execCommand 抽成一个函数：内网 http 下 clipboard API 不存在要走它；
    // API 存在却被拒（权限、页面失焦）时也得再试一次。实测无头环境里
    // writeText 抛 NotAllowedError，原来会直接弹「复制失败」，兜底一次都没跑。
    const legacyCopy = () => {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.cssText = "position:fixed;top:0;left:0;opacity:0";
      document.body.appendChild(ta);
      ta.select();
      let ok = false;
      try { ok = document.execCommand("copy"); } catch (e) { /* 老浏览器直接抛 */ }
      ta.remove();
      return ok;
    };
    try {
      let copied = false;
      if (navigator.clipboard && window.isSecureContext) {
        try {
          await navigator.clipboard.writeText(text);
          copied = true;
        } catch (e) {
          copied = legacyCopy();
        }
      } else {
        copied = legacyCopy();
      }
      if (copied) toast("日志已复制到剪贴板");
      else toast("复制失败，请手动选中", "err");
    } catch (e) {
      toast("复制失败，请手动选中", "err");
    }
  });

  /* 清空是破坏性操作：第一次点变成「确认清空」，5 秒内不点就还原。
     真正清空时连卡头的脚本名一起抹掉 —— 日志都没了，标题还挂着上一次
     跑过的文件名，看起来像残留内容。轮询重渲染不碰卡头，所以抹掉之后不会被补回来。 */
  const termClear = document.getElementById("term-clear");
  let clearArmed = false, clearTimer = null;
  function clearDisarm() {
    clearArmed = false;
    clearTimeout(clearTimer);
    termClear.textContent = "清空";
    termClear.classList.remove("armed");
  }
  termClear.addEventListener("click", () => {
    if (!clearArmed) {
      clearArmed = true;
      termClear.textContent = "确认清空";
      termClear.classList.add("armed");
      clearTimer = setTimeout(clearDisarm, 5000);
      return;
    }
    // 脚本还在跑时，清空只清屏、继续跟随输出：以前这里直接放弃跟随，
    // 脚本在设备上照跑，界面却回到「就绪」还能再点一次运行。要中止请用「停止」
    if (running && runJob) {
      termLog.innerHTML = '<span class="dim">// 日志已清空，脚本仍在运行</span><span class="cursor"></span>';
      clearDisarm();
      toast("日志已清空，脚本仍在运行");
      return;
    }
    setRunning(false);
    stopPolling();
    termLog.innerHTML = '<span class="dim">// 日志已清空</span><span class="cursor"></span>';
    termStatus.textContent = "就绪";
    termStatus.style.color = "";
    setTermTarget("");
    clearDisarm();
    toast("日志已清空");
  });
  // 拖放：dragover 必须 preventDefault，否则浏览器会直接打开那个文件，
  // 整个页面被替换掉 —— 这是拖放最常见的「点了没反应，页面没了」
  ["dragenter", "dragover"].forEach((ev) =>
    dropzone.addEventListener(ev, (e) => {
      e.preventDefault();
      dropzone.classList.add("is-over");
    })
  );
  ["dragleave", "drop"].forEach((ev) =>
    dropzone.addEventListener(ev, (e) => {
      e.preventDefault();
      dropzone.classList.remove("is-over");
    })
  );
  dropzone.addEventListener("drop", (e) => {
    const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    uploadFile(f);
  });
  dropzone.addEventListener("click", () => filePicker.click());
  seedTerminal();

  /* ============================================================
     数据层：轮询 /api/state 与 /api/sensor，把结果铺到各个视图
     ============================================================ */

  /* 模块清单。后端只做只读扫描（不执行模块脚本），
     所以这里可以放心跟着 10 秒轮询一起刷新。 */
  let MODULES = [];

  function renderModules() {
    const list = document.getElementById("mod-list");
    const warnBox = document.getElementById("mod-warn");
    document.getElementById("mod-count").textContent = MODULES.length + " 个";
    document.getElementById("mod-fw").textContent = S2.state && S2.state.framework_ver || "1.0.0";

    const warns = (S2.state && S2.state.patch_warnings) || [];
    if (warns.length) {
      warnBox.hidden = false;
      // 扫描告警本身就是不可信文本（来自模块目录里的文件名），照样用 DOM 拼
      warnBox.textContent = warns.length + " 个模块被跳过：";
      const ul = document.createElement("ul");
      warns.forEach((w) => {
        const li = document.createElement("li");
        li.textContent = w;
        ul.appendChild(li);
      });
      warnBox.appendChild(ul);
    } else {
      warnBox.hidden = true;
      warnBox.textContent = "";
    }

    // 模块操作进行中（内置包安装最长 3 分钟）或「确认卸载」待点时不重画：
    // 重画会把「处理中…」的按钮恢复成可点，或者把已经武装的确认按钮还原
    if (modPending || list.querySelector('[data-armed="1"]')) { modRenderStale = true; return; }
    modRenderStale = false;

    if (!MODULES.length) {
      setHtmlIfChanged(list,
        '<div class="empty" style="padding:26px 14px;text-align:center;color:var(--text-3)">' +
          '<div style="font-size:var(--t-base);color:var(--text);margin-bottom:4px">还没有安装任何模块</div>' +
          '<div style="font-size:var(--t-sm)">把 .run 单文件包拖到上面的脚本库，上传后点「运行」完成安装</div>' +
        "</div>");
      return;
    }

    setHtmlIfChanged(list, MODULES.map((m) => {
      // module.conf 是 .run 安装包写进来的，标题/说明/服务名同样不可信
      const svc = Object.keys(m.services || {}).map((k) => {
        const st = m.services[k];
        return '<span class="svc' + (st === "active" || st === "done" ? " on" : "") + '" title="' + esc(k) + '">' +
          esc(zhSvc(st)) + "</span>";
      }).join("");
      const desc = m.desc || "（无说明）";
      // 角标取模块中文标题的前 2 个字，不再显示 module.conf 里的英文 CODE——
      // 标题都没写、desc 之类都读不到的极端情况才退回「模块」两个字占位
      const badge = (m.title ? String(m.title).slice(0, 2) : "") || "模块";
      return '<div class="mod-row' + (m.enabled ? "" : " is-off") + '" data-mod="' + esc(m.id) + '">' +
        '<div class="mod-badge" title="' + esc(m.code || "") + '">' + esc(badge) + "</div>" +
        '<div class="mod-main">' +
          '<div class="mod-title">' + esc(m.title) +
            (m.version ? '<span class="ver" title="版本">' + esc(m.version) + "</span>" : "") +
          "</div>" +
          '<div class="mod-desc" title="' + esc(desc) + '">' + esc(desc) + "</div>" +
          (svc ? '<div class="mod-svc">' + svc + "</div>" : "") +
        "</div>" +
        '<div class="mod-acts">' +
          // 三个同级操作，原来三种形态：启用/停用有边框、卸载是裸文字。
          // 卸载虽然危险，但它是个按钮，不是一条链接 —— 给 danger 变体（红字 + 淡红描边），
          // 第一次点变成实心红确认态，两级都和「停用」区分得开。
          // 模块停着的时候，「启用」才是这一行唯一该做的事，用 primary 指出来。
          '<button class="btn sm' + (m.enabled ? "" : " primary") + '" data-act="apply"' + (m.enabled && !m.bundled ? " disabled" : "") + ">" + (m.bundled ? (m.enabled ? "更新/重装" : "安装") : "启用") + "</button>" +
          (m.bundled ? '<button class="btn sm" data-act="status">查看状态</button>' : "") +
          '<button class="btn sm" data-act="remove"' + (m.enabled ? "" : " disabled") + ">停用</button>" +
          // 内置完整包「卸载」会把包本身删掉，之后只能重装应用才回来；内置包只给停用
          (m.bundled ? "" : '<button class="btn sm danger" data-act="uninstall">卸载</button>') +
        "</div>" +
      "</div>";
    }).join(""));
  }

  let modPending = false, modRenderStale = false;

  async function modAction(id, act, btn) {
    const m = MODULES.find((x) => x.id === id);
    if (!m || modPending) return;
    if (m.bundled && act === "apply") {
      const approved = await reviewScript({name: m.title, source: "内置完整包 " + m.version, changed: true,
        sha256: m.sha256 || "", content: m.desc,
        risks: [id === "s2-selfheal" ? "安装或更新完整自愈包，包括设备树、引导配置和守护服务；原包会备份，不会自动重启。" : "安装开关机指示灯与提示音组件，停用旧的重复灯光服务。"]});
      if (!approved) return;
    }
    if (act === "uninstall") {
      // 卸载会删目录、删 service —— 两步确认，第一次点只是把按钮变成警告
      if (btn.dataset.armed !== "1") {
        btn.dataset.armed = "1";
        btn.textContent = "确认卸载";
        btn.classList.add("armed");
        btn._armTimer = setTimeout(() => {
          btn.dataset.armed = "0";
          btn.textContent = "卸载";
          btn.classList.remove("armed");
          if (modRenderStale) renderModules();
        }, 5000);
        return;
      }
      // 已经确认了：撤掉 5 秒还原计时，否则它会在请求途中把按钮文字改回「卸载」
      clearTimeout(btn._armTimer);
    }
    btn.disabled = true;
    const label = act === "uninstall" ? "卸载" : btn.textContent;
    btn.textContent = "处理中…";
    modPending = true;
    document.querySelectorAll("#mod-list button").forEach((b) => { if (b !== btn) b.disabled = true; });
    let res;
    try {
      res = await hw("patch", { id: id, action: act });
    } finally {
      modPending = false;
    }
    btn.disabled = false;
    btn.textContent = label;
    if (act === "uninstall") {
      btn.dataset.armed = "0";
      btn.classList.remove("armed");
    }
    document.getElementById("mod-list").__lastHtml = null;
    renderModules();
    setTermTarget(m.title);
    termReset("$ " + m.title + " · " + act);
    termWrite(res.data?.text || res.detail || res.msg || (res.ok ? "完成" : "失败"), res.ok ? "ok" : "err");
    termStatus.textContent = res.ok ? "完成" : "失败";
    toast(m.title + (res.ok ? "：操作完成，详情见执行日志" : "：操作失败，详情见执行日志"), res.ok ? "ok" : "err");
    refreshState();
  }

  document.getElementById("mod-list").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-act]");
    if (!btn) return;
    const row = e.target.closest(".mod-row");
    if (row) modAction(row.dataset.mod, btn.dataset.act, btn);
  });
  document.getElementById("mod-refresh").addEventListener("click", refreshState);

  /* ---------- 状态刷新 ----------
     两条独立节奏（§5）：refreshState() 拉重的 /api/state + 轻的 /api/sensor 一起刷，
     给「手动刷新」「写操作成功后」「进入控制页」「首次加载」这些一次性触发点用；
     pollSensorOnly() 只拉 /api/sensor，给可自定义间隔（默认 10 秒，见 §6 pollSec）
     的常驻轮询用——config、脚本、模块这些慢变的东西不需要跟着仪表盘一起刷。 */
  // 正在刷新时再来的请求不能丢：写操作之后那次刷新被吞掉，按钮就会停在旧状态，
  // 一直等到下一轮 60 秒。这里合并成「当前这轮结束后再刷一轮」，调用方 await 到的是最新数据。
  let refreshing = null, refreshAgain = false;

  function refreshState() {
    if (refreshing) { refreshAgain = true; return refreshing; }
    refreshing = (async () => {
      do { refreshAgain = false; await refreshStateOnce(); } while (refreshAgain);
    })().finally(() => { refreshing = null; });
    return refreshing;
  }

  // 采样按「发出的先后」编号：慢的那次（比如跟着 /api/state 一起等的）晚到时，
  // 不能盖掉后发先到的新读数，否则风扇读数会往回跳一下
  let sensorSent = 0, sensorTaken = 0;
  async function fetchSensor() {
    const seq = ++sensorSent;
    const se = await apiGet("/api/sensor");
    if (se) se.__seq = seq;
    return se;
  }
  function takeSensor(se) {
    if (se.__seq < sensorTaken) return false;
    sensorTaken = se.__seq;
    return true;
  }
  async function refreshStateOnce() {
    try {
      const [st, se] = await Promise.all([
        apiGet("/api/state"),
        fetchSensor().catch(() => null),
      ]);
      if (!st || !st.ok) throw new Error((st && st.msg) || "接口返回失败");
      S2.state = st.data;
      S2.access = st.access || null;
      S2.online = true;
      S2.lastErr = "";
      if (se && se.ok && takeSensor(se)) S2.sensor = se.data;
      applyState();
      syncAccessUI();
    } catch (e) {
      S2.online = false;
      S2.lastErr = e.message || String(e);
      // 留在控制台里：设备上没有开发者工具，出问题时这段是唯一的线索
      console.error("[S2] 状态刷新失败:", e && e.stack || e);
      // 顶栏那颗状态胶囊是唯一的全局信号，连不上就得说清楚
      const top = document.getElementById("top-status");
      top.className = "chip danger";
      top.innerHTML = '<i class="dot"></i>连接中断';
    }
  }

  // 只拉 /api/sensor：仪表盘、趋势图、四张硬件卡、服务矩阵、指示灯、链路状态全部能从
  // 这一份活数据里刷新（HW_SPEC §1：/api/sensor 本来就带 services/led_override/net_link），
  // 不必为了这些常驻轮询去动 /api/state 那份重快照。失败时不动 S2.online——
  // 真正的在线判断仍然看 /api/state 那条（refreshState），sensor 单独掉线只记日志。
  let sensorPolling = false, sensorFails = 0;
  async function pollSensorOnly() {
    if (sensorPolling) return;
    sensorPolling = true;
    try {
      const se = await fetchSensor();
      if (se && se.ok && takeSensor(se)) {
        S2.sensor = se.data;
        if (sensorFails) { sensorFails = 0; S2.online = true; }
        applySensorOnly();
      }
    } catch (e) {
      console.error("[S2] 传感器轮询失败:", e && e.stack || e);
      // 连续两次拉不到就在顶栏说出来：否则读数冻住却毫无提示，要等 60 秒的重快照才暴露
      if (++sensorFails >= 2) {
        S2.online = false;
        const top = document.getElementById("top-status");
        top.className = "chip danger";
        top.innerHTML = '<i class="dot"></i>连接中断';
      }
    } finally {
      sensorPolling = false;
    }
  }

  // /api/hwinfo 慢变信息：120 秒一拉（§5），失败/404（后端还没实现这个接口，或老版本）
  // 一律当作「没有静态信息」——S2.hwinfo 保持原值（通常是 null），四张硬件卡退到
  // 只显示实时部分，绝不能让这一条额外接口的失败连累 refreshState() 那条主链路。
  let hwinfoFetching = false;
  async function fetchHwinfo(force) {
    if (hwinfoFetching) return;
    hwinfoFetching = true;
    try {
      const r = await apiGet("/api/hwinfo" + (force ? "?force=1" : ""));
      if (r && r.ok) {
        S2.hwinfo = r.data;
        // hwinfo 到了之后四张卡要补一次：型号、序列号这些静态字段不会等到下一次
        // /api/sensor 回来才出现——但只有数据真的变了才重画，避免每 120 秒一次空炉重绘
        try { applySensorOnly(); } catch (e) { console.error("[S2] 硬件卡重绘失败:", e && e.stack || e); }
        try {
          const sys = S2.hwinfo.system || null;
          setText("set-os", sys && sys.os ? sys.os : "—");
          setText("set-kernel", sys && sys.kernel ? sys.kernel : "—");
          setText("set-arch", sys && sys.arch ? sys.arch : "—");
        } catch (e) { console.error("[S2] 设备信息补齐失败:", e && e.stack || e); }
      }
    } catch (e) {
      // 404/旧后端/网络失败都归为「暂时没有静态信息」，只记日志，不打扰用户、
      // 也不把 S2.hwinfo 清空——已经拿到过的静态信息还能接着用
      console.error("[S2] 获取硬件静态信息失败:", e && e.stack || e);
    } finally {
      hwinfoFetching = false;
    }
  }

  /* 服务矩阵 / 指示灯覆盖 / 链路状态：§5 要求改从 /api/sensor 取（它本来就带
     services/led_override/net_link），这样概览页这几块能跟着快轮询一起活，
     不必依赖 60 秒一次的重快照。旧后端 sensor 里没有这些字段时退回 state 那份——
     两边都没有就维持上一次的值，不拿「查不到」覆盖成一片「未知」。 */
  function applyLiveBits(sensor, state) {
    const svc = (sensor && sensor.services) || (state && state.services) || null;
    if (svc || DEMO_EMPTY) {
      SERVICES = (svc && Object.keys(svc).length ? Object.keys(svc) : Object.keys(SVC_NAMES)).map((name) => ({ n: name, s: DEMO_EMPTY ? "failed" : ((svc && svc[name]) || "unknown") }));
      renderServices();
    }
    const lo = (sensor && sensor.led_override) || (state && state.conf && state.conf.led_override) || null;
    if (lo) {
      LED.pwr.mode = lo.power === "off" ? "off" : "auto";
      // 配置里可能是 auto / breathe / off；认不出来的值一律按 auto 处理，
      // 免得一个手工写坏的文件让分段控件三个按钮全都不亮
      LED.dsk.mode = lo.sata === "off" ? "off" : lo.sata === "breathe" ? "breathe" : "auto";
      renderLed();
    }
    const nl = (sensor && sensor.net_link) || (state && state.net_link) || null;
    if (nl && nl.deployed) {
      LED.pwr.auto = nl.up === null ? "链路状态未知"
        : (nl.up ? "链路已连接" : "链路断开") + (nl.iface ? " · " + nl.iface : "");
    }
  }

  /* 只处理「实时」那一半：仪表盘、趋势图、四张硬件卡、服务矩阵、指示灯、链路状态。
     pollSensorOnly()（§5 的 pollSec 常驻轮询）和 applyState()（60 秒重快照/写操作后）
     都调用这一份，逻辑只写一遍，两条节奏才不会对同一块 DOM 给出不同的字段来源。 */
  // 处理器/内存/存储/网络四张卡只活在概览页：控制页里它们在 display:none 的 .view 下，
  // 这期间拼 HTML、量尺寸全是白费——隐藏视图不渲染，标记一下欠了一次，切回来再补（§5）。
  let hwCardsStale = false;
  function applySensorOnly() {
    const sensor = S2.sensor;
    applyLiveBits(sensor, S2.state);
    const overviewShown = currentView === "overview";
    if (!overviewShown) hwCardsStale = true;
    if (sensor) {
      pushTrend(sensor);
      applyFanReading(sensor);
      // 仪表盘和硬件信息卡各自拼的 HTML 和字段推断比前面那些老函数复杂得多。
      // 单独兜一层 try/catch：某块卡自己的渲染炸了只该让那一块停在上一次成功的样子，
      // 不能把这一整轮刷新带下去——否则会看到「整页卡死」而不是「一张卡没刷出来」。
      // 仪表盘不跟着隐藏/显示切换：它的开销已经靠 gaugeSet() 内部的「值不变就跳过
      // gPaint」压得很低（见上面「指针仪表」一节），不值得为了省这点开销牺牲正确性——
      // 隐藏期间不画会导致 replayGaugeBoot() 拿着几十秒前的旧读数重播，看起来像卡住。
      try { applyGauges(sensor); } catch (e) { console.error("[S2] 仪表盘渲染失败:", e && e.stack || e); }
      if (overviewShown) {
        try { renderCpu(S2.hwinfo, sensor); } catch (e) { console.error("[S2] 处理器卡渲染失败:", e && e.stack || e); }
        try { renderMem(S2.hwinfo, sensor); } catch (e) { console.error("[S2] 内存卡渲染失败:", e && e.stack || e); }
        try { renderStorage(S2.hwinfo, sensor); } catch (e) { console.error("[S2] 存储卡渲染失败:", e && e.stack || e); }
        try { renderNet(S2.hwinfo, sensor); } catch (e) { console.error("[S2] 网络卡渲染失败:", e && e.stack || e); }
        hwCardsStale = false;
      }
      drawSparks();
      drawChart();
    } else if (overviewShown) {
      // 没有 sensor 时（接口 404/失败/旧后端）也要让四张卡从「等待采样…」换成
      // 看得出「读不到」的状态，而不是停在骨架上一直转——hwinfo 可能已经到了，
      // 静态字段（型号、序列号）应该照样显示，只是实时读数全是「—」
      try { renderCpu(S2.hwinfo, null); } catch (e) { console.error("[S2] 处理器卡渲染失败:", e && e.stack || e); }
      try { renderMem(S2.hwinfo, null); } catch (e) { console.error("[S2] 内存卡渲染失败:", e && e.stack || e); }
      try { renderStorage(S2.hwinfo, null); } catch (e) { console.error("[S2] 存储卡渲染失败:", e && e.stack || e); }
      try { renderNet(S2.hwinfo, null); } catch (e) { console.error("[S2] 网络卡渲染失败:", e && e.stack || e); }
      hwCardsStale = false;
    }
  }
  // 切回概览时补一次之前被跳过的硬件卡渲染；用的是最近一次的 S2.sensor/S2.hwinfo，
  // 不等下一个轮询周期——否则用户切回来看到的还是几十秒前（甚至更久）的旧卡面
  function flushHwCardsIfStale() {
    if (!hwCardsStale) return;
    hwCardsStale = false;
    const sensor = S2.sensor;
    try { renderCpu(S2.hwinfo, sensor); } catch (e) { console.error("[S2] 处理器卡渲染失败:", e && e.stack || e); }
    try { renderMem(S2.hwinfo, sensor); } catch (e) { console.error("[S2] 内存卡渲染失败:", e && e.stack || e); }
    try { renderStorage(S2.hwinfo, sensor); } catch (e) { console.error("[S2] 存储卡渲染失败:", e && e.stack || e); }
    try { renderNet(S2.hwinfo, sensor); } catch (e) { console.error("[S2] 网络卡渲染失败:", e && e.stack || e); }
  }

  function applyState() {
    const d = S2.state;
    if (!d) return;
    setDeviceClock(num(d.now), num(d.tz_offset));

    // ---- 风扇策略与曲线（先于读数：读数的「哪一路触发」要用到曲线）----
    const fan = d.conf && d.conf.fan;
    if (fan) {
      // 只有用户没在编辑时才用后端的值覆盖本地（档位和曲线一起），
      // 否则正在改的格子会被这一轮 60 秒的重快照改回去，保存时还会带上错的档位
      if (!curveDirty) {
        fanProfile = fan.TEMP_PROFILE || "custom";
        const rawT4 = num(fan.HDD_T4) || 55;
        fanCurve = curveFromConf(fan);
        // 夹过一次就告诉用户，只提示一次 —— /api/state 每轮都带着这段配置，每次都弹就成噪音了
        if (rawT4 > HDD_T4_MAX && !hddClamped) {
          hddClamped = true;
          toast("硬盘 T4 已按新上限从 " + rawT4 + "℃ 调整到 " + HDD_T4_MAX + "℃，确认后请点「保存并生效」", "err");
        }
        setFanProfile(fanProfile);
        renderCurveInputs();
      }
    }

    // ---- 实时读数 + 服务 / 指示灯 / 链路：和 pollSensorOnly() 共用同一份逻辑 ----
    applySensorOnly();

    // ---- 蜂鸣 ----
    const beep = d.conf && d.conf.beep;
    // 正在拖、或刚落盘还没等到回包时不覆盖：这一轮重快照把手指下的数字弹回去，
    // 是最容易被当成「滑块坏了」的一类 bug
    if (beep && beep.beep_ms && !beepDirty && ovBeepSlide.hidden) {
      beepSaved = beep.beep_ms;
      syncBeep();
    }

    // ---- 定时开关机 ----
    // 用户刚改过、请求还没回来时不覆盖：轮询把刚改好的时间弹回去，
    // 是最容易被当成「输入框坏了」的一类 bug
    const ps = d.conf && d.conf.power_schedule;
    if (ps) {
      const err = ps.apply_error || "";
      if (err && err !== schErrShown) toast(err, "err");
      schErrShown = err;
    }
    if (ps && !schDirty) {
      SCH.extended = Array.isArray(ps.weekdays);
      SCH.next = ps.next_shutdown && ps.next_shutdown.ts;
      SCH.maintenance = Number(ps.maintenance_until) || 0;
      SCH.on = ps.enabled === "1" || ps.enabled === "true" || ps.enabled === true;
      if (typeof ps.shutdown_time === "string") SCH.off = ps.shutdown_time;
      if (typeof ps.boot_time === "string") SCH.boot = ps.boot_time;
      schConfirmed = { on: SCH.on, off: SCH.off, boot: SCH.boot };
      renderSchedule();
    }

    // ---- 端口 / 主机名 / 运行时长 ----
    if (d.port && document.activeElement !== portInput) portInput.value = d.port;
    if (d.port) portUrl.textContent = portAddress(d.port);

    // 运行时长一处算、两处用（顶栏 + 设备信息卡）：口径只写一遍，
    // 免得以后改格式只改了一边，两处对不上。
    // 顶栏是紧凑胶囊「1天1小时 24分」（精确到分钟），设备信息卡是同一个数。
    // 秒数以「拿到数据那一刻」为锚，之后跟顶栏时钟同一拍（scheduleClock）本地往前推，
    // 不然两次轮询之间分钟数不走，看着像卡住了。
    const up = num(d.uptime);
    if (up !== null) { upBase = up; upAt = Date.now(); }
    const upText = renderUptime();

    // ---- 概览页「设备信息」----
    // 这几行以前是写死的演示数据（主机名、8 天 20 小时、v1.5.1），升版后
    // 页面还挂着旧号。现在一律取 /api/state 的真实值，拿不到就显示「—」。
    const isRoot = d.root === true;
    setText("set-board", d.board || "—");
    setText("set-host", d.hostname || "—");
    setText("set-uptime", upText || "—");
    setText("set-ver", d.app_ver ? String(d.app_ver) : "未知");
    setText("set-root", isRoot ? "管理员（直控）" : "普通用户（只读）");
    setText("set-note", isRoot
      ? "数值来自设备实时状态，" + (pollSec === 1 ? "每秒刷新" : "每 " + pollSec + " 秒刷新")
      : "当前不是以管理员权限运行，写操作会被拒绝");
    // 这行字只有「不是 root」时才是真的需要留意的，别让它一直挂着警告色
    const noteEl = document.getElementById("set-note");
    if (noteEl && noteEl.parentElement) {
      noteEl.parentElement.classList.toggle("warn", !isRoot);
    }

    // 操作系统 / 内核 / 架构来自 /api/hwinfo（S2.hwinfo.system），不是 /api/state——
    // hwinfo 还没采到（旧后端、刚启动还没轮到第一次 60 秒拉取）时这三格先留「—」，
    // 不该因为 hwinfo 慢一步就让整个 applyState() 空等
    const sys = (S2.hwinfo && S2.hwinfo.system) || null;
    setText("set-os", sys && sys.os ? sys.os : "—");
    setText("set-kernel", sys && sys.kernel ? sys.kernel : "—");
    setText("set-arch", sys && sys.arch ? sys.arch : "—");

    // ---- 脚本库 ----
    if (DEMO_EMPTY) {
      SCRIPTS.length = 0;
      renderScripts();
    } else if (Array.isArray(d.scripts)) {
      SCRIPTS = d.scripts;
      renderScripts();
      seedTerminalIfFresh();
    }

    // ---- 模块 ----
    MODULES = DEMO_EMPTY ? [] : (Array.isArray(d.patches) ? d.patches : []);
    renderModules();
  }

  /* 只在第一次拿到脚本清单时铺一次终端提示，别每次轮询都清空正在跑的日志 */
  let termSeeded = false;
  function seedTerminalIfFresh() {
    if (termSeeded) return;
    termSeeded = true;
    seedTerminal();
  }

  /* ---------- 设置 ---------- */
  /* 改端口是整个控制台最危险的操作：写错就把自己锁在外面。
     所以先校验，再两步确认，并且把「改完从哪个地址进」当场写出来。 */
  const portInput = document.getElementById("port-input");
  const portSave = document.getElementById("port-save");
  const portErr = document.getElementById("port-err");
  const portConfirm = document.getElementById("port-confirm");
  const portNext = document.getElementById("port-next");
  const portUrl = document.getElementById("port-url");
  let portArmed = false, portTimer = null;
  // 没口令且不在设备本机：改端口、重启都会被后端拒绝（403），见 syncAccessUI
  let dangerLocked = false;

  // 地址取自 location —— 写死会在用 IP 或域名访问时说假话
  if (location.host) portUrl.textContent = location.protocol + "//" + location.host;
  if (location.port) portInput.value = location.port;

  const portValue = () => Number(portInput.value);
  function portAddress(port) { const url = new URL(location.href); url.port = String(port); url.pathname = "/"; url.search = ""; url.hash = ""; return url.href; }
  const portValid = () => {
    const v = portValue();
    return Number.isInteger(v) && v >= 1024 && v <= 65535;
  };
  function portDisarm() {
    portArmed = false;
    clearTimeout(portTimer);
    portSave.textContent = "保存并重启应用";
    portConfirm.hidden = true;
  }
  function portCheck() {
    const raw = portInput.value.trim();
    let msg = "";
    if (!raw) msg = "端口不能为空";
    else if (!portValid()) msg = "请输入 1024 – 65535 之间的整数";
    portErr.textContent = msg;
    portErr.hidden = !msg;
    portInput.setAttribute("aria-invalid", msg ? "true" : "false");
    portSave.disabled = !!msg || dangerLocked;
    portSave.title = dangerLocked ? "需要先设置访问口令" : "";
    return !msg && !dangerLocked;
  }
  portInput.addEventListener("input", () => { portDisarm(); portCheck(); });
  portSave.addEventListener("click", async () => {
    if (!portCheck()) return;
    if (!portArmed) {
      portArmed = true;
      portSave.textContent = "确认重启并跳转";
      portNext.textContent = portAddress(portValue());
      portConfirm.hidden = false;
      portTimer = setTimeout(portDisarm, 8000);
      return;
    }
    const p = portValue();
    portDisarm();
    portSave.disabled = true;
    const res = await apiPost("/api/restart", { port: p });
    portSave.disabled = false;
    if (!res.ok) {
      toast(res.msg || "改端口失败", "err");
      return;
    }
    portUrl.textContent = portAddress(p);
    toast("端口已改为 " + p + "，服务重启中…3 秒后请访问新地址");
    // 服务会停 1 秒再起，起在新端口上。3 秒后自动跳，跳不过去也有上面那句地址兜底。
    const next = portAddress(p);
    setTimeout(() => { location.href = next; }, 3000);
  });
  portCheck();

  /* ---------- §6 采样间隔控件（#poll-seg） ----------
     趋势图卡头的 3/5/10/30/60 秒按钮：点哪个就按哪个重排 pollSensorOnly() 的节奏，
     立即拉一次、toast 告知、经 store 持久化（只影响这台浏览器，手机和电脑各设各的）。
     通用 .seg 点击处理器（见上面「分段控件」一节）已经会切 aria-pressed，这里
     只在初始化时把按钮点到 loadPollSec() 读回来的那个值上——不然刷新后控件显示
     「10 秒」但其实用户上次选的是「30 秒」，直到下一次点击才会被纠正。 */
  const pollSeg = document.getElementById("poll-seg");
  function syncPollSegUI() {
    if (!pollSeg) return;
    [...pollSeg.children].forEach((b) => b.setAttribute("aria-pressed", String(Number(b.dataset.poll) === pollSec)));
  }
  syncPollSegUI();
  if (pollSeg) {
    pollSeg.addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-poll]");
      if (!btn) return;
      const v = Number(btn.dataset.poll);
      if (!POLL_CHOICES.includes(v) || v === pollSec) return;
      pollSec = v;
      store.set(POLL_KEY, String(v));
      syncPollSegUI();
      startSensorPolling();   // 按新间隔重排定时器
      pollSensorOnly();       // 立即按新间隔拉一次，不等下一个周期
      toast(v === 1 ? "已切到实时：每秒刷新一次" : "采样间隔已改为每 " + v + " 秒");
    });
  }

  /* ---------- 电源：只有重启（关机不放在网页上：关了网页就够不着设备了） ----------
     二次确认分两层：
       · 前端：卡片上的按钮只打开确认弹窗；弹窗里的「确认」有 3 秒倒计时才可点，
         防止手机上连点两下直接把设备重启了。焦点默认在「取消」上，Esc / 点遮罩都是取消。
       · 后端：弹窗打开时向 /api/power/prepare 取一次性确认码，确认时带上；码 60 秒过期、
         用一次就作废，旧页面残留的请求、重复提交都不会生效。
     执行后停掉常规轮询，盖上整页的等待遮罩，探测设备掉线 / 回来：回来后自动刷新页面，
     5 分钟还没回来就提示重启失败。 */
  const POWER_UI = {
    reboot: {
      title: "确认重启设备？", ok: "确认重启", ic: "#i-refresh", tone: "accent",
      desc: "正在运行的任务和网页连接会中断，大约 1–2 分钟后恢复，页面会等设备恢复后自动刷新。",
    },
  };
  // 最长等 5 分钟。?rbmax= 只给测试缩短用（夹在 10–300 秒之间，只能更短不能更长）
  const REBOOT_MAX_SEC = Math.min(300, Math.max(10, Number(new URLSearchParams(location.search).get("rbmax")) || 300));
  const POWER_COUNTDOWN = 3;
  const powerDlg = document.getElementById("power-dlg");
  const pdIc = document.getElementById("pd-ic");
  const pdTitle = document.getElementById("pd-title");
  const pdDesc = document.getElementById("pd-desc");
  const pdErr = document.getElementById("pd-err");
  const pdOk = document.getElementById("pd-ok");
  const pdCancel = document.getElementById("pd-cancel");
  const powerSub = document.getElementById("power-sub");
  const powerBtns = document.querySelectorAll("[data-power]");
  let powerAction = "", powerNonce = "", powerNonceAt = 0, powerLeft = 0, powerTick = null;
  let powerBusy = false, powerOpener = null;

  function syncPowerOk() {
    const cfg = POWER_UI[powerAction];
    if (!cfg) return;
    pdOk.textContent = powerLeft > 0 ? cfg.ok + "（" + powerLeft + "）" : cfg.ok;
    pdOk.disabled = powerBusy || powerLeft > 0 || !powerNonce;
  }
  function setPdIcon(href, tone, spin) {
    pdIc.className = "pd-ic" + (tone === "accent" ? " accent" : "") + (spin ? " busy" : "");
    pdIc.innerHTML = '<svg aria-hidden="true"><use href="' + href + '"/></svg>';
  }
  async function armPowerNonce() {
    powerNonce = "";
    syncPowerOk();
    const r = await apiPost("/api/power/prepare", { action: powerAction });
    if (r && r.ok && r.nonce) {
      powerNonce = r.nonce;
      powerNonceAt = Date.now();
      pdErr.hidden = true;
    } else {
      pdErr.textContent = (r && r.msg) || "设备暂时无法确认这个操作";
      pdErr.hidden = false;
    }
    syncPowerOk();
  }

  function openPower(action, opener) {
    const cfg = POWER_UI[action];
    if (!cfg || powerBusy) return;
    powerAction = action;
    powerOpener = opener || null;
    setPdIcon(cfg.ic, cfg.tone, false);
    pdTitle.textContent = cfg.title;
    pdDesc.textContent = cfg.desc;
    pdErr.hidden = true;
    pdOk.className = "btn " + (cfg.tone === "accent" ? "accent-solid" : "danger-solid");
    pdOk.hidden = false;
    pdCancel.textContent = "取消";
    pdCancel.disabled = false;
    powerLeft = POWER_COUNTDOWN;
    clearInterval(powerTick);
    powerTick = setInterval(() => {
      powerLeft = Math.max(0, powerLeft - 1);
      syncPowerOk();
      if (!powerLeft) clearInterval(powerTick);
    }, 1000);
    syncPowerOk();
    if (typeof powerDlg.showModal === "function") powerDlg.showModal();
    else { powerDlg.setAttribute("open", ""); powerDlg.classList.add("no-modal"); }
    pdCancel.focus();
    armPowerNonce();
  }
  function onPowerClosed() {
    clearInterval(powerTick);
    powerDlg.classList.remove("no-modal");
    if (!powerBusy) { powerAction = ""; powerNonce = ""; }
    if (powerOpener && powerOpener.isConnected) powerOpener.focus({ preventScroll: true });
    powerOpener = null;
  }
  function closePower() {
    if (typeof powerDlg.close === "function" && powerDlg.open) powerDlg.close();   // 触发 close 事件
    else { powerDlg.removeAttribute("open"); onPowerClosed(); }
  }
  powerDlg.addEventListener("close", onPowerClosed);
  powerDlg.addEventListener("click", (e) => { if (e.target === powerDlg) closePower(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && powerDlg.open && powerDlg.classList.contains("no-modal")) closePower();
  });
  pdCancel.addEventListener("click", closePower);

  pdOk.addEventListener("click", async () => {
    if (pdOk.disabled) return;
    const action = powerAction;
    pdOk.disabled = true;
    pdCancel.disabled = true;
    // 弹窗开着放了快一分钟：码要过期了，先换一张新的，别让用户点了确认才收到「已过期」
    if (Date.now() - powerNonceAt > 50000) await armPowerNonce();
    const body = { action: action, nonce: powerNonce };
    powerNonce = "";   // 一次性：发出去就作废，失败重试会重新取
    // 记下发令前的开机时长：回来时开机时长变小 = 真的重启过（哪怕 3 秒一次的探测没撞上掉线那几秒）
    const upBefore = Number(S2.state && S2.state.uptime);
    const r = await apiPost("/api/power", body);
    pdCancel.disabled = false;
    if (!r || !r.ok) {
      pdErr.textContent = (r && r.msg) || "操作失败";
      pdErr.hidden = false;
      armPowerNonce();
      return;
    }
    if (r.dryrun) {   // 本地测试服务器：流程走完但不真关
      closePower();
      toast(r.msg || "已演练");
      return;
    }
    enterRebootWait(upBefore);
  });

  /* ---------- 重启等待遮罩 ----------
     三步：已发指令 → 设备关闭服务（探测到连不上）→ 设备启动、等服务恢复（又连上了 = 完成）。
     判定「恢复」看两条，任一成立即可：
       · 探测时见过掉线，之后又连上；
       · 一直没撞上掉线（重启很快、3 秒一次的探测正好错过），但 /api/state 的开机时长比发令前小。
     连上时的 HTTP 401（口令改过 / cookie 失效）也算服务恢复，刷新后服务端会给出登录页。
     5 分钟（REBOOT_MAX_SEC）内没恢复 → 「设备重启失败」，给「继续等待」和「刷新页面」。 */
  const rbOv = document.getElementById("reboot-ov");
  const rbRing = document.getElementById("rb-ring");
  const rbIc = document.getElementById("rb-ic");
  const rbTitle = document.getElementById("rb-title");
  const rbDesc = document.getElementById("rb-desc");
  const rbSteps = document.getElementById("rb-steps");
  const rbBar = document.getElementById("rb-bar");
  const rbElapsed = document.getElementById("rb-elapsed");
  const rbFoot = document.getElementById("rb-foot");
  const rbWait = document.getElementById("rb-wait");
  const rbReload = document.getElementById("rb-reload");
  let rbT0 = 0, rbClock = null, rbRun = 0, rbUpBefore = NaN;

  const mmss = (s) => Math.floor(s / 60) + ":" + String(Math.floor(s % 60)).padStart(2, "0");
  function rbStep(name, cls) {
    // 当前步之前的都标成完成，当前步标 now/ok/bad，之后的保持灰色
    let hit = false;
    rbSteps.querySelectorAll("li").forEach((li) => {
      if (li.dataset.step === name) { hit = true; li.className = cls; }
      else if (!hit) li.className = "ok";
      else li.className = "";
    });
  }
  function rbIcon(href) { rbIc.innerHTML = '<svg aria-hidden="true"><use href="' + href + '"/></svg>'; }
  document.getElementById("rb-max").textContent = mmss(REBOOT_MAX_SEC);
  function rbTick() {
    const sec = Math.min(REBOOT_MAX_SEC, (Date.now() - rbT0) / 1000);
    rbElapsed.textContent = mmss(sec);
    rbBar.style.width = (sec / REBOOT_MAX_SEC * 100).toFixed(1) + "%";
  }

  function enterRebootWait(upBefore) {
    powerBusy = true;
    stopAllPolling();
    powerBtns.forEach((b) => { b.disabled = true; });
    closePower();
    const top = document.getElementById("top-status");
    top.className = "chip warn";
    top.innerHTML = '<i class="dot"></i>正在重启';
    powerSub.textContent = "设备正在重启…";
    document.documentElement.classList.add("rebooting");
    rbOv.hidden = false;
    rbUpBefore = upBefore;
    rbWaitFrom(Date.now(), false);
    rbOv.focus({ preventScroll: true });   // 读屏软件从遮罩开始读；底下的页面已经不可用了
  }

  // wentDown0：「继续等待」时沿用已经观察到的掉线状态
  function rbWaitFrom(t0, wentDown0) {
    const run = ++rbRun;
    rbT0 = t0;
    rbRing.className = "rb-ring";
    rbIcon("#i-refresh");
    rbTitle.textContent = "设备正在重启";
    rbDesc.textContent = "请不要关闭这个页面，设备恢复后会自动刷新";
    rbFoot.hidden = true;
    rbStep(wentDown0 ? "up" : "down", "now");
    clearInterval(rbClock);
    rbTick();
    rbClock = setInterval(rbTick, 1000);
    probeReboot(run, rbUpBefore, wentDown0);
  }

  // 返回 "up" / "auth"（服务在，但要重新登录）/ "down"
  async function rbPing(path) {
    const ctl = typeof AbortController === "function" ? new AbortController() : null;
    const to = setTimeout(() => ctl && ctl.abort(), 2500);
    try {
      const r = await fetch(path, { cache: "no-store", signal: ctl ? ctl.signal : undefined,
        credentials: "omit", headers: authHeaders() });
      if (r.status === 401) return { st: "auth" };
      if (!r.ok) return { st: "down" };
      return { st: "up", body: path === "/api/state" ? await r.json().catch(() => null) : null };
    } catch (e) {
      return { st: "down" };
    } finally {
      clearTimeout(to);
    }
  }

  async function probeReboot(run, upBefore, wentDown) {
    for (;;) {
      await new Promise((res) => setTimeout(res, 3000));
      if (run !== rbRun) return;   // 「继续等待」开了新一轮，这一轮作废
      const sec = (Date.now() - rbT0) / 1000;
      const p = await rbPing("/api/sensor");
      if (run !== rbRun) return;
      if (p.st === "down") {
        if (!wentDown) { wentDown = true; rbStep("up", "now"); }
      } else {
        let back = wentDown || p.st === "auth";
        if (!back && Number.isFinite(upBefore) && upBefore > 0) {
          const s = await rbPing("/api/state");
          const upNow = Number(s.body && s.body.data && s.body.data.uptime);
          if (s.st === "auth" || (Number.isFinite(upNow) && upNow < upBefore)) back = true;
        }
        if (back) { rbDone(); return; }
      }
      if (sec >= REBOOT_MAX_SEC) { rbFail(wentDown); return; }
    }
  }

  function rbDone() {
    clearInterval(rbClock);
    rbBar.style.width = "100%";
    rbRing.className = "rb-ring done";
    rbIcon("#i-check");
    rbSteps.querySelectorAll("li").forEach((li) => { li.className = "ok"; });
    rbTitle.textContent = "设备已恢复";
    rbDesc.textContent = "正在刷新页面…";
    try { sessionStorage.removeItem("s2-auth-reload"); } catch (e) {}
    setTimeout(() => location.reload(), 900);
  }

  function rbFail(wentDown) {
    clearInterval(rbClock);
    rbTick();
    rbRing.className = "rb-ring fail";
    rbIcon("#i-alert");
    rbStep(wentDown ? "up" : "down", "bad");
    rbTitle.textContent = "设备重启失败";
    const waited = REBOOT_MAX_SEC >= 60 ? Math.round(REBOOT_MAX_SEC / 60) + " 分钟" : REBOOT_MAX_SEC + " 秒";
    rbDesc.textContent = wentDown
      ? "已等待 " + waited + "，设备还没有重新连上。请检查设备的电源和网线；设备恢复后点「刷新页面」。"
      : "已等待 " + waited + "，设备一直在线但没有重启。可以刷新页面后再试一次。";
    rbFoot.hidden = false;
    rbWait.dataset.down = wentDown ? "1" : "";
    rbReload.focus();
    powerSub.textContent = "设备重启失败";
  }

  rbWait.addEventListener("click", () => rbWaitFrom(Date.now(), rbWait.dataset.down === "1"));
  rbReload.addEventListener("click", () => location.reload());

  powerBtns.forEach((b) => b.addEventListener("click", () => openPower(b.dataset.power, b)));

  /* ---------- 访问口令：决定高危按钮能不能点 ----------
     /api/state 带 access: {token: 设没设口令, danger: 这次来源能否做高危操作}。
     没口令且不是在设备本机打开：重启 / 改端口锁住，并直接说清楚去哪里设口令，
     别让用户点了确认、等完倒计时才收到一个 403。旧后端没有 access 字段时不锁。 */
  const POWER_SUB_DEFAULT = powerSub.textContent;
  const accToken = document.getElementById("acc-token");
  const accHint = document.getElementById("acc-hint");
  function syncAccessUI() {
    const a = S2.access;
    if (!a || powerBusy) return;
    dangerLocked = a.danger === false;
    accToken.textContent = a.token ? "已开启" : "未开启";
    accToken.className = a.token ? "on" : "off";
    accHint.hidden = !!a.token;
    powerBtns.forEach((b) => {
      b.disabled = dangerLocked;
      b.title = dangerLocked ? "需要先设置访问口令" : "";
    });
    powerSub.textContent = dangerLocked
      ? "需要先设置访问口令：在飞牛「应用设置 → 配置」里设置后，才能从这台设备之外重启"
      : POWER_SUB_DEFAULT;
    powerSub.classList.toggle("locked", dangerLocked);
    syncScheduleControls();
    portCheck();
  }

  const logoutButton = document.getElementById("logout");
  logoutButton.hidden = !SESSION;
  logoutButton.addEventListener("click", async () => {
    logoutButton.disabled = true;
    const result = await apiPost("/api/logout", {});
    if (result.ok || loginRedirecting) authExpired();
    else { toast(result.msg || "退出失败，请重试", "err"); logoutButton.disabled = false; }
  });

  /* ---------- 启动：三条节奏各自独立轮询（§5） ----------
     /api/sensor 按 pollSec（默认 10 秒，§6 可改）；/api/state 固定 60 秒 + 写操作成功后
     + 进入「控制」页；/api/hwinfo 固定 120 秒。页面在后台时三个定时器全停——
     切到别的标签页还在发请求是白耗电，回来时每条各自补拉一次就够新了。 */
  let sensorTimer = null, stateTimer = null, hwinfoTimer = null, alertTimer = null;

  /* ===== 通知（右上角铃铛） =====
     告警由后端线程判（页面关着也在查），这里只读 /api/alerts：未读数进红点，点开看记录。
     30 秒拉一次，跟后端检查周期一样；页面隐藏时跟其它轮询一起停。
     新出现的严重 / 警告：页面内弹一条提示；用户勾了「弹系统通知」且浏览器允许时再发一条系统通知
     （局域网 http 下多数浏览器不给系统通知权限，勾选时会如实提示）。 */
  const bellBtn = document.getElementById("btn-bell");
  const bellBadge = document.getElementById("bell-badge");
  const ntfPanel = document.getElementById("ntf-panel");
  const ntfList = document.getElementById("ntf-list");
  const ntfActive = document.getElementById("ntf-active");
  const ntfSum = document.getElementById("ntf-sum");
  const ntfSys = document.getElementById("ntf-sys");
  const NTF_SYS_KEY = "s2-ntf-sys";
  const LV_ZH = { crit: "严重", warn: "警告", ok: "已恢复" };
  let ALERTS = { events: [], active: [], unread: 0, unread_crit: 0, last_id: 0 };
  let alertSeenId = null;          // 上次看到的最大 id：只对比它更新的事件弹提示，刷新页面不会把旧告警再弹一遍

  try { ntfSys.checked = localStorage.getItem(NTF_SYS_KEY) === "1" && "Notification" in window && Notification.permission === "granted"; } catch (e) {}

  function ntfTime(ts) {
    const d = new Date(ts * 1000), now = new Date();
    const hm = pad2(d.getHours()) + ":" + pad2(d.getMinutes());
    if (d.toDateString() === now.toDateString()) return hm;
    const y = new Date(now); y.setDate(now.getDate() - 1);
    if (d.toDateString() === y.toDateString()) return "昨天 " + hm;
    return (d.getMonth() + 1) + "月" + d.getDate() + "日 " + hm;
  }
  function ntfItemHtml(e) {
    const lv = LV_ZH[e.level] ? e.level : "warn";
    return '<li class="ntf-item lv-' + lv + (e.read ? "" : " unread") + '" data-id="' + num(e.id) + '">' +
      '<i class="ntf-dot" aria-hidden="true"></i>' +
      '<div class="ntf-body"><div class="ntf-t">' + esc(e.title) + "</div>" +
      (e.detail ? '<div class="ntf-d">' + esc(e.detail) + "</div>" : "") + "</div>" +
      '<div class="ntf-meta"><span class="ntf-lv">' + LV_ZH[lv] + "</span>" +
      '<time datetime="' + new Date(e.ts * 1000).toISOString() + '" title="' + esc(new Date(e.ts * 1000).toLocaleString()) + '">' + esc(ntfTime(e.ts)) + "</time></div></li>";
  }
  function renderAlerts() {
    const a = ALERTS;
    const n = a.unread || 0;
    bellBadge.hidden = !n;
    bellBadge.textContent = n > 99 ? "99+" : String(n);
    bellBtn.classList.toggle("has-crit", !!a.unread_crit);
    bellBtn.classList.toggle("has-unread", !!n);
    bellBtn.setAttribute("aria-label", n ? "通知，" + n + " 条未读" + (a.unread_crit ? "，其中 " + a.unread_crit + " 条严重" : "") : "通知，没有未读");
    bellBtn.title = n ? n + " 条未读通知" : "通知";
    if (ntfPanel.hidden) return;   // 面板关着就不拼列表
    const act = a.active || [];
    ntfSum.textContent = act.length ? act.length + " 个问题未解决" : "设备一切正常";
    ntfSum.className = "ntf-sum" + (act.some((x) => x.level === "crit") ? " crit" : act.length ? " warn" : " ok");
    ntfActive.hidden = !act.length;
    setHtmlIfChanged(ntfActive, act.length
      ? '<div class="ntf-sec">正在发生</div>' + act.map((x) =>
          '<div class="ntf-act lv-' + (x.level === "crit" ? "crit" : "warn") + '"><i class="ntf-dot" aria-hidden="true"></i>' +
          '<span class="ntf-t">' + esc(x.title) + '</span><span class="ntf-since">' + esc("已持续 " + agoText(x.since).replace(" 前", "").replace("前", "")) + "</span></div>").join("")
      : "");
    const ev = a.events || [];
    setHtmlIfChanged(ntfList, ev.length
      ? ev.map(ntfItemHtml).join("")
      : '<li class="ntf-empty"><svg aria-hidden="true"><use href="#i-check"/></svg><span>还没有通知</span>' +
        '<small>温度、容量、硬盘健康、服务状态出问题时会记在这里</small></li>');
    document.getElementById("ntf-readall").disabled = !n;
    document.getElementById("ntf-clear").disabled = !ev.length;
  }
  function announceNew(events) {
    // 只提醒真正「新」的问题：恢复类不打扰；一次来了好几条就合成一句
    const fresh = events.filter((e) => e.level === "crit" || e.level === "warn");
    if (!fresh.length) return;
    const top = fresh.find((e) => e.level === "crit") || fresh[0];
    toast(fresh.length > 1 ? top.title + " 等 " + fresh.length + " 条新告警" : top.title, top.level === "crit" ? "err" : undefined);
    if (ntfSys.checked && "Notification" in window && Notification.permission === "granted") {
      try { new Notification("fnOS-S2 硬件告警", { body: fresh.map((e) => e.title).join("\n"), tag: "s2-alert-" + top.id }); } catch (e) {}
    }
  }
  async function fetchAlerts() {
    let r;
    try { r = await apiGet("/api/alerts"); } catch (e) { return; }   // 掉线时铃铛保持上一次的样子
    if (!r || !r.ok || !r.data) return;
    const d = r.data;
    if (alertSeenId !== null && d.last_id > alertSeenId) {
      announceNew((d.events || []).filter((e) => e.id > alertSeenId && !e.read));
    }
    alertSeenId = Math.max(alertSeenId || 0, d.last_id || 0);
    ALERTS = d;
    renderAlerts();
  }
  function startAlertPolling() {
    clearInterval(alertTimer);
    alertTimer = setInterval(fetchAlerts, 30000);
  }

  function openNtf(open) {
    ntfPanel.hidden = !open;
    bellBtn.setAttribute("aria-expanded", String(open));
    if (open) {
      renderAlerts();
      fetchAlerts();
      ntfPanel.querySelector("#ntf-readall").focus({ preventScroll: true });
    }
  }
  bellBtn.addEventListener("click", (e) => { e.stopPropagation(); openNtf(ntfPanel.hidden); });
  document.addEventListener("click", (e) => {
    if (!ntfPanel.hidden && !ntfPanel.contains(e.target) && !bellBtn.contains(e.target)) openNtf(false);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !ntfPanel.hidden) { openNtf(false); bellBtn.focus(); }
  });
  document.getElementById("ntf-readall").addEventListener("click", async () => {
    const r = await apiPost("/api/alerts/read", {});
    if (!r.ok) { toast(r.msg || "操作失败", "err"); return; }
    (ALERTS.events || []).forEach((e) => { e.read = true; });
    ALERTS.unread = 0; ALERTS.unread_crit = 0;
    renderAlerts();
  });
  // 点某一条：只把这一条标已读
  ntfList.addEventListener("click", async (e) => {
    const li = e.target.closest(".ntf-item.unread");
    if (!li) return;
    const id = num(li.dataset.id);
    const r = await apiPost("/api/alerts/read", { ids: [id] });
    if (!r.ok) return;
    const ev = (ALERTS.events || []).find((x) => x.id === id);
    if (ev && !ev.read) {
      ev.read = true;
      ALERTS.unread = Math.max(0, ALERTS.unread - 1);
      if (ev.level === "crit") ALERTS.unread_crit = Math.max(0, ALERTS.unread_crit - 1);
    }
    renderAlerts();
  });
  const ntfClear = document.getElementById("ntf-clear");
  let ntfClearArm = 0;
  ntfClear.addEventListener("click", async () => {
    // 两步确认，跟「清空日志」一个手感；3 秒内不点第二下就还原
    if (!ntfClearArm) {
      ntfClear.textContent = "确认清空";
      ntfClear.classList.add("armed");
      ntfClearArm = setTimeout(() => { ntfClearArm = 0; ntfClear.textContent = "清空记录"; ntfClear.classList.remove("armed"); }, 3000);
      return;
    }
    clearTimeout(ntfClearArm); ntfClearArm = 0;
    ntfClear.textContent = "清空记录"; ntfClear.classList.remove("armed");
    const r = await apiPost("/api/alerts/clear", {});
    toast(r.ok ? (r.msg || "通知记录已清空") : (r.msg || "清空失败"), r.ok ? undefined : "err");
    if (r.ok) fetchAlerts();
  });
  ntfSys.addEventListener("change", async () => {
    let on = ntfSys.checked;
    if (on) {
      if (!("Notification" in window)) {
        on = false; toast("这个浏览器不支持系统通知", "err");
      } else if (!window.isSecureContext) {
        on = false; toast("浏览器只允许 https 或本机地址弹系统通知，局域网 http 下用不了", "err");
      } else {
        const p = Notification.permission === "default" ? await Notification.requestPermission() : Notification.permission;
        if (p !== "granted") { on = false; toast("浏览器没有允许通知，请在地址栏左侧的网站设置里打开", "err"); }
      }
    }
    ntfSys.checked = on;
    try { localStorage.setItem(NTF_SYS_KEY, on ? "1" : "0"); } catch (e) {}
    if (on) toast("已开启：有新告警时会弹系统通知");
  });

  function startSensorPolling() {
    clearInterval(sensorTimer);
    sensorTimer = setInterval(pollSensorOnly, pollSec * 1000);
  }
  function startStatePolling() {
    clearInterval(stateTimer);
    stateTimer = setInterval(refreshState, 60000);
  }
  function startHwinfoPolling() {
    clearInterval(hwinfoTimer);
    hwinfoTimer = setInterval(() => fetchHwinfo(false), 120000);
  }
  function stopAllPolling() {
    clearInterval(sensorTimer); sensorTimer = null;
    clearInterval(stateTimer); stateTimer = null;
    clearInterval(hwinfoTimer); hwinfoTimer = null;
    clearInterval(alertTimer); alertTimer = null;
  }
  // 兼容旧调用点：代码里散落的几处 startPolling() 现在等价于「几条一起重排」。
  // 铃铛也挂在这里：切回标签页 / 从 bfcache 恢复时顺手补拉一次通知
  function startPolling() {
    startSensorPolling();
    startStatePolling();
    startHwinfoPolling();
    startAlertPolling();
    fetchAlerts();
  }

  /* ---------- §5 资源：页面隐藏时不只停定时器，还要让 CSS 侧知道「闲着」 ----------
     html.is-idle 配合 style.css 的 animation-play-state:paused，暂停风扇叶片、
     状态灯呼吸、终端光标这些常驻 CSS 动画——页面隐藏时它们还在跑等于纯粹的空转。
     IntersectionObserver 给滚出视口的 .card 加 is-offscreen：同样配合 CSS 暂停动画，
     且渲染函数可以用它跳过「看不见也要重新拼 HTML」的工作。老内置浏览器可能没有
     IntersectionObserver（iOS Safari 12 以下），特性检测不到就什么都不做——
     跳过这层优化不影响功能，强行 polyfill 反而增加体积和复杂度。 */
  function setIdle(idle) {
    root.classList.toggle("is-idle", idle);
  }
  if ("IntersectionObserver" in window) {
    const io = new IntersectionObserver((entries) => {
      entries.forEach((en) => en.target.classList.toggle("is-offscreen", !en.isIntersecting));
    }, { rootMargin: "80px" });   // 留一点余量：卡片刚滚出视口边缘时不要立刻停画，避免来回滚动时抖动
    document.querySelectorAll(".card").forEach((el) => io.observe(el));
  }

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      stopAllPolling();
      setIdle(true);
      clearTimeout(clockTimer);
    } else {
      setIdle(false);
      scheduleClock();
      if (powerBusy) return;   // 正在重启：探测循环自己在跑，别重开常规轮询
      replayGaugeBoot();  // 从隐藏切回来，仪表盘重播一次自检扫动画
      refreshState();
      fetchHwinfo(false);
      startPolling();
    }
  });
  /* iOS Safari 的后退/前进缓存（bfcache）会把整页从内存快照恢复，不重新执行
     <script>——visibilitychange 不一定补得上这种情况：有的版本恢复时
     document.hidden 已经是 false，不会触发上面那条「从隐藏到可见」的分支。
     pageshow 的 event.persisted 专门标记「这是从 bfcache 恢复的」，这里
     兜底重新拉一次数据、重开轮询，否则页面会一直停在离开前的旧读数上，
     计时器也早被浏览器回收，轮询就此停摆却看不出任何异常。 */
  window.addEventListener("pageshow", (e) => {
    if (!e.persisted) return;
    setIdle(false);
    scheduleClock();
    if (powerBusy) return;
    replayGaugeBoot();
    refreshState();
    fetchHwinfo(false);
    startPolling();
  });

  // 切到「控制」页时立刻重扫一次：模块是外部装进来的，等 60 秒才出现太迟钝（§5）
  navItems.forEach((n) =>
    n.addEventListener("click", () => {
      if (n.dataset.view === "control" && !powerBusy) refreshState();
    })
  );

  refreshState().then(() => {
    fetchHwinfo(false); startPolling();
    const review = new URLSearchParams(location.search).get("review_factory");
    if (review) { go("control"); runScript(review, true); }
    else reattachJob();
  });
})();
