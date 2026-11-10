(function () {
  "use strict";
  const media = matchMedia("(prefers-color-scheme: dark)");
  const syncTheme = () => {
    let preference = "auto";
    try { preference = localStorage.getItem("s2-proto-theme") || "auto"; } catch (_) {}
    document.documentElement.dataset.theme = preference === "auto" ? (media.matches ? "dark" : "light") : preference;
  };
  syncTheme(); media.addEventListener("change", syncTheme);
  const $ = id => document.getElementById(id);
  const workspace = $("workspace");
  let principal, current = "health", restore = null;
  const date = value => value ? new Date(value * 1000).toLocaleString() : "—";
  const text = (tag, value, className) => { const el = document.createElement(tag); el.textContent = value; if (className) el.className = className; return el; };
  const allowed = scope => principal?.scopes?.includes(scope);
  function status(message, error = false) { $("status").textContent = message; $("status").className = error ? "error" : ""; $("status").hidden = !message; }
  async function api(path, body) {
    const token = sessionStorage.getItem("s2-session") || "";
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), path === "restore" ? 180000 : path.endsWith("fleet") ? 90000 : 45000);
    try {
      const response = await fetch(path.startsWith("/api/") ? path : "/api/ops/" + path, {
        method: body === undefined ? "GET" : "POST", credentials: "omit", cache: "no-store", signal: controller.signal,
        headers: {...(token ? {Authorization: "Bearer " + token} : {}), ...(body === undefined ? {} : {"Content-Type": "application/json", "X-S2-Request": "1"})},
        body: body === undefined ? undefined : JSON.stringify(body)
      });
      if (response.status === 401) { sessionStorage.removeItem("s2-session"); location.replace("/login"); throw new Error("请重新登录"); }
      let result; try { result = await response.json(); } catch (_) { throw new Error("设备响应无法识别"); }
      if (!response.ok || result?.ok !== true) throw new Error(result?.msg || "操作失败");
      return result.data === undefined ? result : result.data;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("设备响应超时，请刷新确认操作结果");
      throw error;
    } finally { clearTimeout(timer); }
  }
  function button(label, action, scope, className) {
    const el = text("button", label, "btn" + (className ? " " + className : "")); el.type = "button"; el.disabled = !!scope && !allowed(scope);
    el.addEventListener("click", async () => {
      el.disabled = true;
      status("");
      try { await action(); } catch (error) { status(error.message || "操作失败", true); }
      finally { el.disabled = !!scope && !allowed(scope); }
    });
    return el;
  }
  function card(title, parent = workspace) { const el = text("section", "", "card"); el.append(text("h2", title)); parent.append(el); return el; }
  function fields(parent) { const el = text("div", "", "fields"); parent.append(el); return el; }
  function field(parent, label, value = "", type = "text", options) {
    const wrap = text("label", "", "field" + (type === "checkbox" ? " check" : ""));
    const el = document.createElement(type === "select" ? "select" : type === "textarea" ? "textarea" : "input");
    if (type === "select") for (const [v, title] of options) { const item = text("option", title); item.value = v; el.append(item); }
    else if (type !== "textarea") el.type = type;
    if (type === "checkbox") el.checked = !!value; else el.value = value ?? "";
    el.setAttribute("aria-label", label); wrap.append(text("span", label), el); parent.append(wrap); return el;
  }
  function actions(parent, ...buttons) { const row = text("div", "", "actions"); row.append(...buttons); parent.append(row); }
  function table(parent, columns, rows) {
    if (!rows.length) { parent.append(text("p", "暂无记录", "empty")); return; }
    const wrap = text("div", "", "table-scroll"), table = document.createElement("table"), head = document.createElement("tr");
    for (const column of columns) head.append(text("th", column[0]));
    const thead = document.createElement("thead"); thead.append(head); table.append(thead);
    const body = document.createElement("tbody");
    for (const row of rows) { const tr = document.createElement("tr"); for (const [, render] of columns) { const td = document.createElement("td"), v = render(row); if (v instanceof Node) td.append(v); else td.textContent = v ?? "—"; tr.append(td); } body.append(tr); }
    table.append(body); wrap.append(table); parent.append(wrap);
  }
  function download(filename, value, mime = "application/json") {
    const blob = new Blob([typeof value === "string" ? value : JSON.stringify(value, null, 2)], {type: mime + ";charset=utf-8"});
    const url = URL.createObjectURL(blob), link = document.createElement("a"); link.href = url; link.download = filename; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function csv(rows) {
    if (!rows.length) return "";
    const keys = Object.keys(rows[0]);
    const escape = value => { let s = typeof value === "object" ? JSON.stringify(value) : String(value ?? ""); if (/^[=+@\-\t\r]/.test(s)) s = "'" + s; return '"' + s.replaceAll('"', '""') + '"'; };
    return "\ufeff" + [keys, ...rows.map(row => keys.map(k => row[k]))].map(row => row.map(escape).join(",")).join("\r\n");
  }
  function chart(parent, points, key, label) {
    parent.append(text("h3", label));
    const valid = points.filter(p => typeof p[key] === "number" && Number.isFinite(p[key]));
    if (!valid.length) { parent.append(text("p", "还没有有效采样。后台每 30 秒记录一次。", "empty")); return; }
    const ns = "http://www.w3.org/2000/svg", svg = document.createElementNS(ns, "svg"); svg.setAttribute("viewBox", "0 0 700 220"); svg.setAttribute("class", "chart"); svg.setAttribute("role", "img"); svg.setAttribute("aria-label", label + "历史趋势");
    const max = Math.max(1, ...valid.map(p => p[key])) * 1.1, first = points[0].ts, span = Math.max(1, points.at(-1).ts - first);
    const line = document.createElementNS(ns, "polyline"); line.setAttribute("points", valid.map(p => `${45 + (p.ts - first) / span * 640},${185 - p[key] / max * 160}`).join(" ")); svg.append(line);
    for (const [x, y, value] of [[5, 25, max.toFixed(1)], [15, 185, "0"], [45, 210, date(first)], [480, 210, date(points.at(-1).ts)]]) { const t = document.createElementNS(ns, "text"); t.setAttribute("x", x); t.setAttribute("y", y); t.textContent = value; svg.append(t); }
    parent.append(svg);
  }
  async function health() {
    const [overview, report] = await Promise.all([api("overview"), api("report")]);
    const cards = text("div", "", "cards"); workspace.append(cards);
    const summary = card("设备健康", cards);
    summary.append(text("div", report.alerts.active.length ? report.alerts.active.length + " 项待关注" : "当前无活动告警", "metric"));
    summary.append(text("p", "最近采样：" + date(overview.last_sample), "muted"));
    if (overview.last_error) summary.append(text("p", overview.last_error, "bad"));
    if (overview.maintenance.active) summary.append(text("p", "维护中，自动恢复时间：" + date(overview.maintenance.until), "pill"));
    actions(summary, button("下载健康报告", () => download("s2-health-report.json", report)));
    const problems = card("活动告警", cards);
    table(problems, [["问题", r => r.title], ["级别", r => r.level], ["开始时间", r => date(r.since)]], report.alerts.active);
    const diagnostics = card("故障诊断", cards); diagnostics.classList.add("wide");
    table(diagnostics, [["检查", r => r.title], ["结果", r => text("span", r.ok ? "正常" : "待检查", r.ok ? "good" : "bad")], ["说明", r => r.detail], ["操作", r => r.repair ? button("重启服务", async () => { await api("diagnostics/repair", {service: r.repair}); status("已请求修复，正在重新检查"); await render(); }, "control") : "—"]], report.diagnostics);
  }
  async function history() {
    const panel = card("历史监控"); const row = fields(panel);
    const hours = field(row, "时间范围", "24", "select", [["24", "最近 24 小时"], ["168", "最近 7 天"], ["720", "最近 30 天"], ["8760", "最近一年"]]);
    const metric = field(row, "指标", "cpu_temp", "select", [["cpu_temp", "CPU 温度 ℃"], ["hdd_max_temp", "最高硬盘温度 ℃"], ["fan_duty", "风扇占空比 %"], ["cpu_pct", "CPU 使用率 %"], ["mem_pct", "内存使用率 %"]]);
    const plot = text("div", ""); panel.append(plot); let points = [];
    const refresh = async () => { points = await api("history?hours=" + hours.value); plot.replaceChildren(); chart(plot, points, metric.value, metric.selectedOptions[0].textContent); };
    hours.onchange = () => refresh().catch(e => status(e.message, true)); metric.onchange = () => { plot.replaceChildren(); chart(plot, points, metric.value, metric.selectedOptions[0].textContent); };
    actions(panel, button("导出 CSV", () => download("s2-history.csv", csv(points), "text/csv")));
    await refresh();
    const overview = await api("overview");
    panel.append(text("p", overview.history_enabled ? "历史记录已开启，会定期写入存储。" : "历史记录已暂停，已有数据保留。", "note"));
    if (allowed("admin")) {
      const retention = card("历史数据保留");
      const enabled = field(retention, "记录历史数据", overview.history_enabled ? "on" : "off", "select", [["off", "关闭（减少后台写盘）"], ["on", "开启"]]);
      const days = field(retention, "保留天数（1–365）", overview.retention_days, "number");
      retention.append(text("p", "需要硬盘休眠时请关闭记录，或将运维数据放在 SSD/eMMC。关闭不会删除已有数据。", "note"));
      actions(retention, button("保存保留期限", async () => { await api("retention", {days: Number(days.value), enabled: enabled.value === "on"}); await render(); status("历史记录设置已更新"); }, "admin"));
    }
  }
  async function disks() {
    const panel = card("磁盘健康趋势"), selector = field(panel, "磁盘", "", "select", []), content = text("div", ""); panel.append(content);
    const draw = async identity => {
      const data = await api("disks" + (identity ? "?identity=" + encodeURIComponent(identity) : ""));
      if (!selector.options.length) for (const device of data.devices) { const option = text("option", device.identity); option.value = device.identity; selector.append(option); }
      content.replaceChildren(); table(content, [["指标", r => r.metric], ["最早值", r => r.before], ["最新值", r => r.after], ["变化", r => r.delta]], data.changes);
      content.append(text("p", "采样复用 SMART 缓存，不主动唤醒休眠盘；增量仅表示观测变化。", "note"));
      table(content, [["时间", r => date(r.ts)], ["设备", r => r.name], ["SMART", r => text("pre", JSON.stringify(r.smart, null, 2))]], data.points.slice(-30).reverse());
      actions(content, button("下载磁盘记录", () => download("s2-disk-history.json", data)));
    };
    selector.onchange = () => draw(selector.value).catch(e => status(e.message, true)); await draw("");
  }
  async function previewRestore(body) { restore = await api("restore/preview", body); $("review-diff").textContent = restore.diff || "配置没有变化"; $("review").showModal(); }
  async function backups() {
    const panel = card("配置备份与恢复"), label = field(panel, "备份说明", "手动备份");
    panel.append(text("p", "包含风扇、灯光、蜂鸣、计划和风扇保护。账号、访问密钥和通知凭据不会导出。", "note"));
    const file = field(panel, "导入备份文件", "", "file"); file.accept = ".json,application/json";
    actions(panel, button("创建并下载备份", async () => { const data = await api("backup", {label: label.value}); download("s2-config-" + data.id + ".json", data); status("备份已保存"); await render(); }, "admin", "primary"),
      button("预览导入差异", async () => { const selected = file.files[0]; if (!selected || selected.size > 65536) throw new Error("请选择不超过 64 KB 的备份 JSON 文件"); const data = JSON.parse(await selected.text()); if (data.format !== "s2-config-v1") throw new Error("不是本应用的配置备份"); await previewRestore({config: data.config}); }, "admin"));
    const versions = card("配置修改历史"); table(versions, [["时间", r => date(r.ts)], ["账号", r => r.actor], ["说明", r => r.label], ["操作", r => button("比较并回退", () => previewRestore({id: r.id}), "admin")]], await api("versions"));
  }
  async function schedule() {
    const data = await api("schedule"), panel = card("每周定时计划"), row = fields(panel);
    const enabled = field(row, "启用计划", data.enabled, "checkbox");
    const off = field(row, "关机时间（留空不关机）", data.shutdown_time, "time"), boot = field(row, "开机时间（留空不开机）", data.boot_time, "time");
    const weekdays = text("div", "", "actions"); panel.append(weekdays);
    const checks = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"].map((day, index) => field(weekdays, day, data.weekdays.includes(index), "checkbox"));
    const skips = field(panel, "临时跳过日期（每行 YYYY-MM-DD，最多 64 天）", data.skip_dates.join("\n"), "textarea");
    panel.append(text("p", `按设备时区 ${data.timezone} 执行。下次关机：${data.next_shutdown?.local || "未安排"}；下次开机：${data.next_boot?.local || "未安排"}`, "note"));
    if (!data.rtc_wake) panel.append(text("p", "未检测到 RTC 唤醒接口，保存开机时间不代表设备具备自动开机能力。", "note"));
    actions(panel, button("保存周计划", async () => { await api("schedule", {enabled: enabled.checked, shutdown_time: off.value, boot_time: boot.value, weekdays: checks.map((c, i) => c.checked ? i : -1).filter(i => i >= 0), skip_dates: skips.value.split(/[\n,]/).map(s => s.trim()).filter(Boolean)}); status("计划已保存"); await render(); }, "control", "primary"));
    const maintenance = card("维护模式"), state = (await api("overview")).maintenance;
    maintenance.append(text("p", state.active ? "维护至 " + date(state.until) + " · " + state.reason : "当前未处于维护模式"));
    maintenance.append(text("p", "暂停维护期内的预定开关机与服务异常推送，到期自动恢复；温度、硬盘告警和风扇保护继续工作。", "note"));
    const period = field(maintenance, "维护时长（分钟，最多 1440）", "60", "number"), reason = field(maintenance, "维护原因", "设备维护");
    actions(maintenance, button("开始维护", async () => { await api("maintenance", {minutes: Number(period.value), reason: reason.value}); status("维护模式已启用"); await render(); }, "control"), button("立即结束维护", async () => { await api("maintenance", {minutes: 0}); status("维护模式已结束"); await render(); }, "control"));
  }
  async function guard() {
    const data = await api("guard"), panel = card("风扇保护策略");
    panel.append(text("p", data.active ? "保护中：" + data.reason : "当前未触发保护", data.active ? "bad" : "good"));
    panel.append(text("p", "CPU 传感器失效或温度连续超限时切到全速。温度低于阈值 3℃ 且连续两次正常后恢复温控。策略由设备风扇服务执行。", "note"));
    const row = fields(panel), enabled = field(row, "启用保护", data.enabled, "checkbox"), cpu = field(row, "CPU 阈值（50–85℃）", data.cpu_limit, "number"), disk = field(row, "硬盘阈值（40–60℃）", data.disk_limit, "number"), count = field(row, "连续异常采样次数（1–10）", data.fail_samples, "number");
    actions(panel, button("保存保护策略", async () => { await api("guard", {enabled: enabled.checked, cpu_limit: Number(cpu.value), disk_limit: Number(disk.value), fail_samples: Number(count.value)}); status("保护策略已保存"); await render(); }, "control", "primary"));
  }
  async function notifications() {
    const data = await api("notifications"), c = data.config, panel = card("外部告警通知"), row = fields(panel);
    const enabled = field(row, "启用通知", c.enabled, "checkbox"), kind = field(row, "通知方式", c.kind, "select", [["webhook", "Webhook"], ["email", "邮件（STARTTLS）"]]);
    const webhook = fields(panel), url = field(webhook, "Webhook 地址", c.url || "", "url"), secret = field(webhook, "Bearer 凭据（留空保留原值）", "", "password"); secret.placeholder = c.has_secret ? "已设置" : "可选";
    const email = fields(panel), inputs = {};
    for (const [key, label, type, value] of [["host", "SMTP 服务器", "text", c.host], ["port", "端口", "number", c.port || 587], ["username", "邮箱账号", "text", c.username], ["password", "密码（留空保留）", "password", ""], ["sender", "发件地址", "email", c.sender], ["recipient", "收件地址", "email", c.recipient]]) inputs[key] = field(email, label, value, type);
    const toggle = () => { webhook.hidden = kind.value !== "webhook"; email.hidden = kind.value !== "email"; }; kind.onchange = toggle; toggle();
    panel.append(text("p", "启用后只推送新告警。失败最多重试 4 次。邮件连接必须支持 STARTTLS；更换地址或账号时请重新填写凭据。", "note"));
    actions(panel, button("保存通知设置", async () => { const body = {enabled: enabled.checked, kind: kind.value}; if (kind.value === "webhook") Object.assign(body, {url: url.value, secret: secret.value}); else { for (const [key, input] of Object.entries(inputs)) body[key] = key === "port" ? Number(input.value) : input.value; } await api("notifications", body); status("通知设置已保存"); await render(); }, "admin", "primary"));
    const deliveries = card("投递记录"); table(deliveries, [["时间", r => date(r.ts)], ["状态", r => ({sent: "已发送", pending: "等待重试", failed: "失败", suppressed: "维护期间抑制"})[r.status]], ["尝试次数", r => r.attempts], ["说明", r => r.error]], data.deliveries);
  }
  async function audit() {
    const panel = card("操作审计"), row = fields(panel), actor = field(row, "账号筛选"), action = field(row, "动作筛选"), results = text("div", ""); let records = [];
    const search = async () => { records = await api("audit?actor=" + encodeURIComponent(actor.value) + "&action=" + encodeURIComponent(action.value)); results.replaceChildren(); table(results, [["时间", r => date(r.ts)], ["账号", r => r.actor], ["来源", r => r.ip], ["动作", r => r.action], ["结果", r => r.ok ? "成功" : "失败"]], records); };
    actions(panel, button("查询", search), button("导出 CSV", () => download("s2-audit.csv", csv(records), "text/csv"))); panel.append(results); await search();
  }
  async function users() {
    const panel = card("账号管理"), row = fields(panel), name = field(row, "账号（2–32 位）"), password = field(row, "新密码（12–128 位；编辑时留空保持）", "", "password"), role = field(row, "权限", "viewer", "select", [["viewer", "只读"], ["operator", "硬件操作员"], ["admin", "管理员"]]), enabled = field(row, "启用账号", true, "checkbox");
    panel.append(text("p", "操作员可以调整硬件与计划。脚本、配置恢复、账号和密钥仅管理员可用。修改账号后，其已有会话会失效。", "note"));
    actions(panel, button("保存账号", async () => { await api("users", {name: name.value, password: password.value, role: role.value, enabled: enabled.checked}); password.value = ""; status("账号已保存"); await render(); }, "admin", "primary"));
    const list = card("已有账号"); table(list, [["账号", r => r.name], ["角色", r => ({admin: "管理员", operator: "操作员", viewer: "只读"})[r.role]], ["状态", r => r.enabled ? "启用" : "停用"], ["操作", r => button("编辑", () => { name.value = r.name; password.value = ""; role.value = r.role; enabled.checked = !!r.enabled; name.focus(); }, "admin")]], await api("users"));
  }
  async function keys() {
    const panel = card("自动化访问密钥"), row = fields(panel), label = field(row, "用途名称"), days = field(row, "有效天数（1–365）", "30", "number"), permissions = fields(panel);
    const scopes = ["read", "control", "scripts", "admin"].map((scope, i) => [scope, field(permissions, ["读取状态", "控制硬件", "执行脚本", "管理设备"][i], i === 0, "checkbox")]);
    panel.append(text("p", "密钥只在创建时显示一次。请按用途选择权限，多设备监控只需读取状态。", "note"));
    const result = text("pre", ""); result.hidden = true; panel.append(result);
    actions(panel, button("创建密钥", async () => { const data = await api("keys", {label: label.value, days: Number(days.value), scopes: scopes.filter(([, el]) => el.checked).map(([scope]) => scope)}); result.textContent = data.token; result.hidden = false; status("密钥已创建，请立即复制保存"); }, "admin", "primary"));
    const list = card("密钥列表"); table(list, [["用途", r => r.label], ["权限", r => r.scopes.join(", ")], ["到期", r => date(r.expires)], ["最近使用", r => date(r.last_used)], ["操作", r => r.revoked ? "已撤销" : button("撤销", async () => { await api("keys/revoke", {id: r.id}); status("密钥已撤销"); await render(); }, "admin")]], await api("keys"));
  }
  async function devices() {
    const panel = card("设备列表"); panel.append(text("p", "登记目标设备的控制台地址与只读密钥；监控最多 20 台设备。远程凭据只保存在当前设备服务端。", "note"));
    if (allowed("admin")) {
      const row = fields(panel), label = field(row, "设备名称"), url = field(row, "控制台地址（建议 HTTPS）", "", "url"), token = field(row, "目标设备只读密钥", "", "password");
      actions(panel, button("添加设备", async () => { await api("devices", {label: label.value, url: url.value, token: token.value}); token.value = ""; status("设备已添加"); await render(); }, "admin"));
    }
    const list = card("多设备状态"), states = await api("fleet");
    table(list, [["设备", r => { const link = text("a", r.label); link.href = r.url; link.target = "_blank"; link.rel = "noopener noreferrer"; return link; }], ["连接", r => r.online ? "在线" : r.error], ["CPU 温度", r => r.sensor?.cpu_temp ?? "—"], ["活动告警", r => r.alerts?.active?.length ?? "—"], ["检查时间", r => date(r.checked_at)], ["管理", r => allowed("admin") ? button("移除", async () => { await api("devices/delete", {id: r.id}); await render(); }, "admin") : "—"]], states);
  }
  async function jobs() {
    const panel = card("脚本任务"); panel.append(text("p", "从硬件控制台审查脚本内容后执行。同一脚本不能重复启动，任务超时自动终止。", "note"));
    const rows = await api("/api/script/jobs"); table(panel, [["脚本", r => r.name], ["状态", r => r.done ? "已结束，退出码 " + r.rc : "运行中"], ["超时", r => r.timeout + " 秒"], ["操作", r => { const div = text("div", "", "actions"); div.append(button("查看输出", () => { const d = document.createElement("dialog"); d.className = "ops-review"; d.append(text("h2", r.name), text("pre", r.lines.join("\n")), button("关闭", () => { d.close(); d.remove(); })); document.body.append(d); d.showModal(); })); if (!r.done) div.append(button("取消任务", async () => { await api("/api/script/cancel", {id: r.id}); status("已请求取消，请稍后刷新"); }, "scripts")); return div; }]], rows);
  }
  const pages = {health: ["设备健康", health, "read"], history: ["历史监控", history, "read"], disks: ["磁盘趋势", disks, "read"], backups: ["备份与版本", backups, "admin"], schedule: ["计划与维护", schedule, "read"], guard: ["风扇保护", guard, "read"], notifications: ["告警推送", notifications, "admin"], audit: ["操作审计", audit, "admin"], users: ["账号权限", users, "admin"], keys: ["访问密钥", keys, "admin"], devices: ["多设备", devices, "read"], jobs: ["脚本任务", jobs, "scripts"]};
  let loading = false;
  async function render() {
    if (loading) return;
    loading = true; $("refresh").disabled = true; workspace.replaceChildren(); $("page-title").textContent = pages[current][0];
    document.querySelectorAll("#navigation button").forEach(b => b.setAttribute("aria-current", b.dataset.page === current ? "page" : "false"));
    try { await pages[current][1](); } catch (error) { status(error.message, true); }
    finally { loading = false; $("refresh").disabled = false; }
  }
  $("refresh").onclick = () => render();
  $("review-cancel").onclick = () => $("review").close();
  $("review-apply").onclick = async () => { $("review-apply").disabled = true; try { await api("restore", restore); $("review").close(); status("配置已恢复"); await render(); } catch (error) { status(error.message, true); } finally { $("review-apply").disabled = false; } };
  $("signout").onclick = async () => { try { await api("/api/logout", {}); sessionStorage.removeItem("s2-session"); location.replace("/login"); } catch (error) { status(error.message, true); } };
  (async () => {
    try {
      principal = await api("me"); $("identity").textContent = principal.name + " · " + ({admin: "管理员", operator: "操作员", viewer: "只读"}[principal.role] || "访问密钥");
      for (const [key, [label, , scope]] of Object.entries(pages)) if (allowed(scope)) { const btn = button(label, async () => { if (loading) return; current = key; status(""); location.hash = key; await render(); }); btn.dataset.page = key; $("navigation").append(btn); }
      const hash = location.hash.slice(1); if (pages[hash] && allowed(pages[hash][2])) current = hash;
      await render();
    } catch (error) { status(error.message, true); }
  })();
})();
