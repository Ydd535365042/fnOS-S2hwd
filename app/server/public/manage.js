(function () {
  "use strict";
  const media = matchMedia("(prefers-color-scheme: dark)");
  const syncTheme = () => {
    let preference = "auto";
    try { preference = localStorage.getItem("s2-proto-theme") || "auto"; } catch (_) {}
    document.documentElement.dataset.theme = preference === "auto" ? (media.matches ? "dark" : "light") : preference;
  };
  syncTheme(); media.addEventListener("change", syncTheme);
  const $ = id => document.getElementById(id), workspace = $("workspace");
  let principal, current = "backups", generation = 0, fieldId = 0, mutating = 0;
  const allowed = scope => !scope || principal?.scopes?.includes(scope);
  const text = (tag, value = "", className = "") => { const el = document.createElement(tag); el.textContent = value; if (className) el.className = className; return el; };
  const date = value => value ? new Date(value * 1000).toLocaleString("zh-CN", {hour12: false}) : "—";
  const when = value => { const el = text("time", date(value)); if (value) el.dateTime = new Date(value * 1000).toISOString(); return el; };
  const badge = (label, kind = "") => text("span", label, "badge " + kind);
  const actorName = value => ({"owner": "访问口令", "passphrase": "访问口令", "local": "设备本机", "guest": "只读访问"}[value] || value);
  function status(message, error = false) {
    const el = $("status"); el.textContent = message; el.className = error ? "error" : ""; el.hidden = !message;
    if (error && message) el.focus({preventScroll: false});
  }
  async function api(path, body) {
    const token = window.S2Auth.get(), controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), path === "restore" ? 180000 : path === "fleet" ? 90000 : 45000);
    if (body !== undefined) mutating++;
    try {
      const response = await fetch(path.startsWith("/api/") ? path : "/api/ops/" + path, {
        method: body === undefined ? "GET" : "POST", credentials: "omit", cache: "no-store", signal: controller.signal,
        headers: {...(token ? {Authorization: "Bearer " + token} : {}), ...(body === undefined ? {} : {"Content-Type": "application/json", "X-S2-Request": "1"})},
        body: body === undefined ? undefined : JSON.stringify(body)
      });
      if (response.status === 401) { window.S2Auth.clear(); location.replace("/login"); throw new Error("请重新登录"); }
      let result; try { result = await response.json(); } catch (_) { throw new Error("设备响应无法识别，请稍后重试"); }
      if (!response.ok || result?.ok !== true) throw new Error(result?.msg || "操作失败，请重试");
      return result.data === undefined ? result : result.data;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("设备响应超时，请刷新确认操作结果");
      if (error instanceof TypeError) throw new Error("连接中断，请检查设备连接后重试");
      throw error;
    } finally { clearTimeout(timer); if (body !== undefined) mutating--; }
  }
  function button(label, action, scope, className = "") {
    const el = text("button", label, "btn " + className); el.type = "button"; el.disabled = !allowed(scope);
    if (el.disabled) el.title = "当前访问方式没有此操作权限";
    el.addEventListener("click", async event => {
      event.preventDefault();
      if (el.disabled) return;
      el.disabled = true; el.setAttribute("aria-busy", "true"); status("");
      try { await action(); } catch (error) { status(error.message || "操作失败", true); }
      finally { el.disabled = !allowed(scope); el.removeAttribute("aria-busy"); }
    });
    return el;
  }
  function card(parent, title, description = "", wide = false) {
    const el = text("section", "", "card" + (wide ? " wide" : "")), head = text("div", "", "panel-head");
    head.append(text("h2", title)); if (description) head.append(text("p", description)); el.append(head); parent.append(el); return el;
  }
  function fields(parent, className = "fields") { const el = text("div", "", className); parent.append(el); return el; }
  function field(parent, label, value = "", type = "text", options = [], props = {}) {
    const wrap = text("label", "", "field" + (type === "checkbox" ? " check" : ""));
    const el = document.createElement(type === "select" ? "select" : type === "textarea" ? "textarea" : "input");
    if (type === "select") for (const [v, title] of options) { const item = text("option", title); item.value = v; el.append(item); }
    else if (type !== "textarea") el.type = type;
    if (type === "checkbox") el.checked = !!value; else el.value = value ?? "";
    el.id = "ops-field-" + (++fieldId); wrap.htmlFor = el.id; el.setAttribute("aria-label", label); Object.assign(el, props);
    wrap.append(text("span", label), el); parent.append(wrap); return el;
  }
  function numberField(parent, label, value, min, max) { return field(parent, label, value, "number", [], {min: String(min), max: String(max), step: "1", required: true}); }
  function toggle(parent, label, value) { return field(fields(parent, "switch-row"), label, value, "checkbox"); }
  function form(parent) {
    const el = document.createElement("form"); parent.append(el);
    el.addEventListener("submit", e => { e.preventDefault(); el.querySelector('[data-submit="true"]')?.click(); }); return el;
  }
  function actions(parent, ...buttons) { const row = fields(parent, "actions"); row.append(...buttons); return row; }
  function save(parent, label, action, scope) {
    if (!allowed(scope)) { parent.querySelectorAll("input,select,textarea").forEach(el => { el.disabled = true; }); note(parent, "当前为只读访问，可查看设置。"); }
    const el = button(label, async () => { if (parent.reportValidity()) await action(); }, scope, "primary");
    el.dataset.submit = "true"; el.type = "submit"; actions(parent, el); return el;
  }
  function note(parent, value, className = "note") { parent.append(text("p", value, className)); }
  function empty(parent, title, detail) { const el = fields(parent, "empty"); el.append(text("strong", title), text("p", detail)); }
  function stats(parent, rows) { const list = text("dl", "", "stat-list"); for (const [label, value] of rows) { const row = text("div"); const dd = text("dd"); dd.append(value instanceof Node ? value : text("span", value)); row.append(text("dt", label), dd); list.append(row); } parent.append(list); }
  function table(parent, columns, rows, blank = ["暂无记录", "新的记录会在这里显示。"] ) {
    if (!rows.length) { empty(parent, ...blank); return; }
    const wrap = fields(parent, "table-scroll"), grid = document.createElement("table"), head = document.createElement("tr");
    for (const [label] of columns) { const th = text("th", label); th.scope = "col"; head.append(th); }
    const thead = document.createElement("thead"); thead.append(head); grid.append(thead);
    const body = document.createElement("tbody"); grid.append(body); wrap.append(grid);
    let page = 0; const size = 10, total = Math.ceil(rows.length / size);
    const pager = text("div", "", "pagination"), position = text("span"), previous = text("button", "上一页", "btn"), next = text("button", "下一页", "btn");
    previous.type = next.type = "button"; position.setAttribute("aria-live", "polite");
    const draw = () => {
      body.replaceChildren();
      for (const row of rows.slice(page * size, (page + 1) * size)) { const tr = document.createElement("tr"); for (const [label, render] of columns) { const td = document.createElement("td"), value = render(row); td.dataset.label = label; if (value instanceof Node) td.append(value); else td.append(text("span", value ?? "—")); tr.append(td); } body.append(tr); }
      position.textContent = `${page + 1} / ${total} 页 · 共 ${rows.length} 条`; previous.disabled = page === 0; next.disabled = page + 1 === total;
    };
    previous.onclick = () => { if (page > 0) { page--; draw(); } }; next.onclick = () => { if (page + 1 < total) { page++; draw(); } };
    if (total > 1) { pager.append(position, previous, next); parent.append(pager); } draw();
  }
  function download(filename, value, mime = "application/json") {
    const blob = new Blob([typeof value === "string" ? value : JSON.stringify(value, null, 2)], {type: mime + ";charset=utf-8"});
    const url = URL.createObjectURL(blob), link = text("a"); link.href = url; link.download = filename; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function csv(rows) {
    if (!rows.length) return "";
    const keys = Object.keys(rows[0]);
    const escape = value => { let s = typeof value === "object" ? JSON.stringify(value) : String(value ?? ""); if (/^[=+@\-\t\r]/.test(s)) s = "'" + s; return '"' + s.replaceAll('"', '""') + '"'; };
    return "\ufeff" + [keys, ...rows.map(row => keys.map(k => row[k]))].map(row => row.map(escape).join(",")).join("\r\n");
  }
  function modal(title, message) {
    const previous = document.activeElement, dialog = text("dialog", "", "ops-review"), head = fields(dialog, "dialog-head"), body = fields(dialog, "dialog-body"), foot = fields(dialog, "dialog-foot");
    const heading = text("h2", title); heading.id = "ops-dialog-" + (++fieldId); head.append(heading); dialog.setAttribute("aria-labelledby", heading.id);
    if (message) body.append(text("p", message, "dialog-message"));
    const close = () => { dialog.close(); dialog.remove(); if (previous?.isConnected) previous.focus(); };
    dialog.addEventListener("cancel", e => { e.preventDefault(); if (dialog.dataset.busy !== "true") close(); });
    document.body.append(dialog); dialog.showModal(); return {dialog, body, foot, close};
  }
  async function confirm(title, message, label, action, dangerous = false) {
    const m = modal(title, message), error = text("p", "", "dialog-error"); error.setAttribute("role", "alert"); m.body.append(error);
    const cancel = button("取消", m.close), apply = button(label, async () => {
      cancel.disabled = true; m.dialog.dataset.busy = "true"; error.textContent = "";
      try { await action(); m.close(); } catch (e) { error.textContent = e.message; } finally { cancel.disabled = false; m.dialog.dataset.busy = "false"; }
    }, null, dangerous ? "danger" : "primary");
    m.foot.append(cancel, apply); cancel.focus(); return m;
  }
  async function previewRestore(body) {
    const restore = await api("restore/preview", body);
    const m = await confirm("恢复配置", "请核对变更。恢复前会自动备份当前配置，访问口令与密钥不受影响。", "确认恢复", async () => { await api("restore", restore); await render(); status("配置已恢复，原配置已自动备份"); });
    m.body.prepend(text("pre", restore.diff || "配置没有变化"));
  }
  async function backups(root) {
    const versions = await api("versions");
    const create = card(root, "创建备份", "将当前配置保存为一个可恢复的版本。"), f = form(create);
    const label = field(f, "备份说明", "", "text", [], {placeholder: "例如：调整风扇前", maxLength: 80});
    note(f, "包含风扇、灯光、蜂鸣、定时计划和保护策略。", "callout");
    save(f, "创建并下载备份", async () => { const data = await api("backup", {label: label.value.trim() || "手动备份"}); download("s2-config-" + data.id + ".json", data); await render(); status("备份已保存，下载已开始"); }, "admin");
    const imported = card(root, "从文件恢复", "先比较差异，再决定是否应用。"), importForm = form(imported);
    const file = field(importForm, "选择备份文件", "", "file", [], {accept: ".json,application/json", required: true});
    note(importForm, "支持本应用导出的 JSON 文件，最大 64 KB。凭据和监控记录不包含在备份中。", "callout");
    save(importForm, "预览配置差异", async () => { const selected = file.files[0]; if (!selected || selected.size > 65536) throw new Error("请选择不超过 64 KB 的备份文件"); let data; try { data = JSON.parse(await selected.text()); } catch (_) { throw new Error("文件不是有效的 JSON，请重新选择备份"); } if (data.format !== "s2-config-v1") throw new Error("这不是本应用的配置备份"); await previewRestore({config: data.config}); }, "admin");
    const history = card(root, "配置版本", "手动备份和配置变更都会保留记录。", true);
    table(history, [["保存时间", r => when(r.ts)], ["说明", r => r.label], ["操作身份", r => actorName(r.actor)], ["操作", r => button("比较并回退", () => previewRestore({id: r.id}), "admin")]], versions, ["还没有配置版本", "创建一次备份，或保存配置后，版本会出现在这里。"]);
  }
  async function schedule(root) {
    const [data, overview] = await Promise.all([api("schedule"), api("overview")]);
    const panel = card(root, "每周执行规则", "为概览页的定时开关机补充执行日期。"), f = form(panel);
    note(f, `当前计划${data.enabled ? "已开启" : "已关闭"} · 关机 ${data.shutdown_time || "未设置"} · 开机 ${data.boot_time || "未设置"}。时间和总开关在概览页调整。`, "callout");
    const group = fields(f, "weekdays"); group.setAttribute("role", "group"); group.setAttribute("aria-label", "执行星期");
    const checks = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"].map((day, i) => field(group, day, data.weekdays.includes(i), "checkbox"));
    const skips = field(f, "跳过日期（可选）", data.skip_dates.join("\n"), "textarea", [], {placeholder: "2026-12-01\n2026-12-02"});
    note(f, "每行一个 YYYY-MM-DD 日期，最多 64 天。按设备时区 " + data.timezone + " 执行。");
    save(f, "保存执行规则", async () => {
      const weekdays = checks.flatMap((c, i) => c.checked ? [i] : []), skip_dates = [...new Set(skips.value.split(/[\n,，]/).map(s => s.trim()).filter(Boolean))];
      if (!weekdays.length) throw new Error("请至少选择一个执行日");
      if (skip_dates.length > 64 || skip_dates.some(d => !/^\d{4}-\d{2}-\d{2}$/.test(d) || !Number.isFinite(Date.parse(d)) || new Date(d).toISOString().slice(0,10) !== d)) throw new Error("跳过日期格式有误，请使用 YYYY-MM-DD，最多 64 天");
      await api("schedule", {weekdays, skip_dates}); await render(); status("执行规则已保存，开关机时间保持原设置");
    }, "control");
    const state = overview.maintenance, maintenance = card(root, "临时维护", "维护结束后自动恢复计划。"), mf = form(maintenance);
    note(mf, state.active ? "维护中，至 " + date(state.until) + (state.reason ? " · " + state.reason : "") : "当前未开启维护，计划正常执行。", "callout");
    const period = numberField(mf, "维护时长（分钟）", 60, 1, 1440), reason = field(mf, "维护说明", "", "text", [], {placeholder: "例如：更换硬盘", maxLength: 200});
    note(mf, "暂停期间的定时开关机与服务异常推送；温度、磁盘告警和风扇保护继续工作。最长 24 小时。");
    save(mf, state.active ? "更新维护时间" : "开始维护", async () => { await api("maintenance", {minutes: Number(period.value), reason: reason.value.trim() || "设备维护"}); await render(); status("维护时间已更新"); }, "control");
    if (state.active) actions(mf, button("立即结束维护", () => confirm("结束维护", "结束后恢复定时计划与服务异常推送。", "结束维护", async () => { await api("maintenance", {minutes: 0}); await render(); status("维护已结束"); }), "control"));
    const next = card(root, "下一次执行", "设备端计算的执行时间，保存规则后更新。", true);
    stats(next, [["下次关机", data.next_shutdown?.local || "未安排"], ["下次开机", data.next_boot?.local || "未安排"]]);
    if (!data.rtc_wake) note(next, "未检测到 RTC 唤醒接口，设备可能无法按计划自动开机。");
  }
  async function guard(root) {
    const data = await api("guard"), panel = card(root, "保护策略", "达到保护条件时，风扇将切换到全速。"), f = form(panel);
    const enabled = toggle(f, "启用风扇保护", data.enabled), row = fields(f);
    const cpu = numberField(row, "CPU 温度阈值（℃）", data.cpu_limit, 50, 85), disk = numberField(row, "硬盘温度阈值（℃）", data.disk_limit, 40, 60), count = numberField(f, "连续异常采样次数", data.fail_samples, 1, 10);
    note(f, "CPU：50–85℃ · 硬盘：40–60℃ · 采样：1–10 次。");
    save(f, "保存保护策略", async () => { await api("guard", {enabled: enabled.checked, cpu_limit: Number(cpu.value), disk_limit: Number(disk.value), fail_samples: Number(count.value)}); await render(); status("风扇保护策略已保存"); }, "control");
    const explanation = card(root, "运行状态", "保护策略由设备服务执行，关闭网页后依然有效。");
    stats(explanation, [["保护状态", badge(data.active ? "保护中" : data.enabled ? "正常监测" : "未启用", data.active ? "bad" : data.enabled ? "good" : "")], ["触发条件", "CPU 温度失效 / 温度连续超限"], ["保护动作", "风扇全速运行"], ["恢复条件", "低于阈值 3℃，连续两次正常"]]);
    if (data.active && data.reason) note(explanation, data.reason, "callout warn");
    note(explanation, "风扇曲线与日常调速在控制页设置；这里仅管理异常保护。");
  }
  async function notifications(root) {
    const data = await api("notifications"), c = data.config, panel = card(root, "告警渠道", "仅推送启用后的新告警。"), f = form(panel);
    const enabled = toggle(f, "启用告警推送", c.enabled), kind = field(f, "推送方式", c.kind, "select", [["webhook", "Webhook"], ["email", "邮件（STARTTLS）"]]);
    const webhook = fields(f, "channel-fields"), url = field(webhook, "Webhook 地址", c.url || "", "url", [], {placeholder: "https://example.com/webhook"}), secret = field(webhook, "Bearer 凭据", "", "password", [], {placeholder: c.has_secret ? "已保存，留空保留" : "可选", autoComplete: "new-password"});
    const email = fields(f, "channel-fields"), inputs = {};
    for (const [key, label, type, value] of [["host", "SMTP 服务器", "text", c.host], ["port", "端口", "number", c.port || 587], ["username", "邮箱账号", "text", c.username], ["password", "邮箱授权码 / 密码", "password", ""], ["sender", "发件地址", "email", c.sender], ["recipient", "收件地址", "email", c.recipient]]) inputs[key] = field(email, label, value, type);
    Object.assign(inputs.port, {min: "1", max: "65535", step: "1"}); inputs.password.placeholder = c.has_password ? "已保存，留空保留" : "填写邮箱授权码"; inputs.password.autocomplete = "new-password";
    const change = () => { const mail = kind.value === "email"; webhook.hidden = mail; email.hidden = !mail; for (const input of webhook.querySelectorAll("input")) input.disabled = mail; for (const input of email.querySelectorAll("input")) input.disabled = !mail; url.required = !mail; for (const key of ["host", "port", "username", "sender", "recipient"]) inputs[key].required = mail; };
    kind.onchange = change; enabled.onchange = change; change();
    note(f, "更换地址、服务器或邮箱账号时，请重新填写对应凭据。邮件服务器需支持 STARTTLS。");
    save(f, "保存推送设置", async () => { const body = {enabled: enabled.checked, kind: kind.value}; if (kind.value === "webhook") Object.assign(body, {url: url.value.trim(), secret: secret.value}); else for (const [key, input] of Object.entries(inputs)) body[key] = key === "port" ? Number(input.value) : input.value; await api("notifications", body); await render(); status("告警推送设置已保存"); }, "admin");
    const delivery = card(root, "投递记录", "失败最多重试 4 次，结果保留在这里。");
    table(delivery, [["时间", r => when(r.ts)], ["状态", r => badge(({sent: "已发送", pending: "等待重试", failed: "失败", suppressed: "维护期间抑制"})[r.status] || r.status, r.status === "sent" ? "good" : r.status === "failed" ? "bad" : "")], ["尝试", r => r.attempts + " 次"], ["说明", r => r.error || "—"]], data.deliveries, ["还没有投递记录", "设置渠道后，新告警的投递结果会显示在这里。"]);
  }
  async function audit(root) {
    const panel = card(root, "操作记录", "查看最近 500 条符合条件的操作。", true), f = form(panel), filters = fields(f, "filter-row");
    const actor = field(filters, "操作身份", "", "text", [], {placeholder: "输入身份关键词"}), action = field(filters, "操作动作", "", "text", [], {placeholder: "例如：backup、script"}), results = fields(panel, "results"); let records = [], searchId = 0;
    const exportButton = button("导出 CSV", () => download("s2-audit.csv", csv(records), "text/csv"));
    const search = async () => { const id = ++searchId; const rows = await api("audit?actor=" + encodeURIComponent(actor.value.trim()) + "&action=" + encodeURIComponent(action.value.trim())); if (id !== searchId) return; records = rows; results.replaceChildren(); table(results, [["时间", r => when(r.ts)], ["操作身份", r => actorName(r.actor)], ["来源", r => r.ip], ["动作", r => r.action], ["结果", r => badge(r.ok ? "成功" : "失败", r.ok ? "good" : "bad")]], records, ["没有匹配的操作", "调整筛选条件，或完成一次配置操作后再查看。"]); exportButton.disabled = !records.length; };
    const searchButton = button("查询", search, null, "primary"); searchButton.dataset.submit = "true"; searchButton.type = "submit";
    actions(filters, searchButton, button("重置", async () => { actor.value = ""; action.value = ""; await search(); })); actions(panel, exportButton); await search();
  }
  async function keys(root) {
    const rows = await api("keys"), panel = card(root, "创建访问密钥", "供自动化程序使用，与网页口令登录相互独立。"), f = form(panel);
    const row = fields(f), label = field(row, "用途名称", "", "text", [], {required: true, maxLength: 80, placeholder: "例如：客厅 NAS 监控"}), days = numberField(row, "有效天数", 30, 1, 365);
    const permissions = fields(f, "scope-options"), labels = {read: "读取状态", control: "控制硬件", scripts: "执行脚本", admin: "管理设备"};
    const scopes = Object.entries(labels).map(([scope, title], i) => [scope, field(permissions, title, i === 0, "checkbox")]);
    note(f, "只授予需要的权限。多设备监控只需“读取状态”，管理权限不会自动包含其他权限。");
    save(f, "创建密钥", async () => {
      const selected = scopes.filter(([, el]) => el.checked).map(([scope]) => scope); if (!selected.length) throw new Error("请至少选择一项权限");
      const data = await api("keys", {label: label.value.trim(), days: Number(days.value), scopes: selected}); await render();
      const m = modal("密钥已创建", "完整密钥只显示这一次，请复制保存后再关闭。"), value = text("pre", data.token, "secret-value"); value.tabIndex = 0; m.body.append(value);
      const hint = text("p", "", "note"); m.body.append(hint);
      m.foot.append(button("复制密钥", async () => { try { await navigator.clipboard.writeText(data.token); hint.textContent = "已复制到剪贴板"; } catch (_) { const range = document.createRange(); range.selectNodeContents(value); const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range); hint.textContent = "已选中密钥，请按 Ctrl+C 或长按复制"; } }), button("已保存，关闭", m.close, null, "primary"));
      status("密钥已创建，列表已更新");
    }, "admin");
    const help = card(root, "使用说明", "每个用途使用独立密钥，便于撤销和排查。");
    stats(help, [["显示次数", "仅创建时一次"], ["到期处理", "自动失效"], ["撤销后", "立即停止访问"]]); note(help, "网页仍只使用访问口令登录，登录记忆不受这里的密钥有效期影响。");
    const list = card(root, "已创建的密钥", "撤销后无法恢复，需要重新创建。", true);
    table(list, [["用途", r => r.label], ["权限", r => r.scopes.map(s => labels[s] || s).join("、")], ["状态", r => badge(r.revoked ? "已撤销" : r.expires <= Date.now()/1000 ? "已过期" : "有效", !r.revoked && r.expires > Date.now()/1000 ? "good" : "")], ["到期时间", r => when(r.expires)], ["最近使用", r => when(r.last_used)], ["操作", r => r.revoked ? "—" : button("撤销", () => confirm("撤销访问密钥", `“${r.label}”关联的程序将立即失去访问权限。`, "确认撤销", async () => { await api("keys/revoke", {id: r.id}); await render(); status("密钥已撤销"); }, true), "admin")]], rows, ["尚未创建密钥", "为监控或自动化程序创建专用密钥后，可在这里管理。"]);
  }
  async function devices(root) {
    const states = await api("fleet"), panel = card(root, "设备状态", "汇总已登记设备的只读状态，最多 20 台。", true);
    if (allowed("admin")) {
      const details = text("details"); details.append(text("summary", "添加设备")); panel.append(details); const f = form(details), row = fields(f);
      const label = field(row, "设备名称", "", "text", [], {required: true, maxLength: 80, placeholder: "例如：书房 S2"}), url = field(row, "控制台地址", "", "url", [], {required: true, placeholder: "http://192.168.1.10:18899"}), token = field(f, "目标设备只读密钥", "", "password", [], {required: true, autoComplete: "new-password"});
      note(f, "在目标设备的“访问密钥”中创建仅有读取权限的密钥。凭据只保存在当前设备服务端。");
      save(f, "添加设备", async () => { const parsed = new URL(url.value); if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password) throw new Error("请填写 HTTP 或 HTTPS 控制台地址，不要在地址中包含口令"); await api("devices", {label: label.value.trim(), url: url.value.trim(), token: token.value}); token.value = ""; await render(); status("设备已添加"); }, "admin");
      note(panel, "点击设备名称可打开对应控制台。");
    }
    table(panel, [["设备", r => { const box = text("div"), link = text("a", r.label, "device-link"); try { const u = new URL(r.url); if (["http:", "https:"].includes(u.protocol)) { link.href = u.href; link.target = "_blank"; link.rel = "noopener noreferrer"; } } catch (_) {} box.append(link, text("span", r.url, "subtext")); return box; }], ["连接", r => { const box = text("div"); box.append(badge(r.online ? "在线" : "离线", r.online ? "good" : "bad")); if (!r.online && r.error) box.append(text("span", r.error, "subtext")); return box; }], ["CPU 温度", r => r.sensor?.cpu_temp != null ? r.sensor.cpu_temp + " ℃" : "—"], ["活动告警", r => r.online ? r.alerts?.active?.length ?? 0 : "—"], ["检查时间", r => when(r.checked_at)], ["操作", r => allowed("admin") ? button("移除", () => confirm("移除设备", `仅从监控列表移除“${r.label}”，不会修改目标设备。`, "确认移除", async () => { await api("devices/delete", {id: r.id}); await render(); status("设备已移除"); }, true), "admin") : "—"]], states, ["还没有添加设备", "展开“添加设备”，填写控制台地址和只读密钥。"]);
  }
  async function jobs(root) {
    const rows = await api("/api/script/jobs"), panel = card(root, "任务记录", "这里查看执行结果与取消任务；脚本管理和运行统一在控制页。", true), filters = fields(panel, "filter-row single");
    const filter = field(filters, "任务状态", "all", "select", [["all", "全部任务"], ["running", "运行中"], ["success", "执行成功"], ["failed", "执行失败"]]), results = fields(panel, "results");
    const draw = () => { results.replaceChildren(); const selected = rows.filter(r => filter.value === "all" || (filter.value === "running" ? !r.done : filter.value === "success" ? r.done && r.rc === 0 : r.done && r.rc !== 0));
      table(results, [["脚本", r => r.name], ["状态", r => badge(!r.done ? "运行中" : r.rc === 0 ? "执行成功" : "执行失败 · " + r.rc, !r.done ? "info" : r.rc === 0 ? "good" : "bad")], ["超时限制", r => r.timeout + " 秒"], ["操作", r => { const group = text("div", "", "actions compact"); group.append(button("查看输出", () => { const m = modal(r.name, r.done ? "任务已结束，退出码 " + r.rc : "运行中的任务；关闭后刷新列表可获取最新输出。"); m.body.append(text("pre", (r.lines || []).join("\n") || "任务尚未产生输出")); m.foot.append(button("关闭", m.close)); })); if (!r.done) group.append(button("取消任务", () => confirm("取消任务", `将终止“${r.name}”的执行。已完成的系统改动不会自动撤销。`, "确认取消", async () => { await api("/api/script/cancel", {id: r.id}); await render(); status("已请求取消任务，请刷新查看最终状态"); }, true), "scripts")); return group; }]], selected, [rows.length ? "没有符合条件的任务" : "还没有执行记录", rows.length ? "选择其他状态查看任务。" : "在控制页运行脚本后，可在这里查看结果。"]);
    }; filter.onchange = draw; draw();
  }
  const pages = {
    backups: ["备份与版本", "保留配置，变更前有据可查。", backups, "admin"],
    schedule: ["计划与维护", "安排执行日期，为设备维护留出时间。", schedule, "read"],
    guard: ["风扇保护", "设置异常阈值，让设备在高温时主动保护。", guard, "read"],
    notifications: ["告警推送", "把需要关注的设备异常发送到你的通知渠道。", notifications, "admin"],
    audit: ["操作审计", "查清谁在什么时候做了什么。", audit, "admin"],
    keys: ["访问密钥", "为监控与自动化分配独立的访问权限。", keys, "admin"],
    devices: ["多设备", "集中查看其他设备的连接与告警状态。", devices, "read"],
    jobs: ["脚本任务", "查看脚本输出，跟进执行结果。", jobs, "scripts"]
  };
  async function render() {
    const id = ++generation, page = pages[current], root = text("div", "", "ops-view");
    $("refresh").disabled = true; workspace.setAttribute("aria-busy", "true"); workspace.replaceChildren(text("p", "正在读取设备数据…", "loading"));
    $("page-title").textContent = page[0]; $("page-description").textContent = page[1]; document.title = page[0] + " · fnOS-S2 运维";
    document.querySelectorAll("#navigation button").forEach(b => { if (b.dataset.page === current) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current"); });
    try { await page[2](root); if (id === generation) workspace.replaceChildren(root); }
    catch (error) { if (id === generation) { workspace.replaceChildren(); empty(workspace, "暂时无法读取数据", error.message); actions(workspace, button("重新加载", render)); status(error.message, true); } }
    finally { if (id === generation) { workspace.setAttribute("aria-busy", "false"); $("refresh").disabled = false; } }
  }
  function navigate() {
    if (!principal) return;
    if (mutating) { history.replaceState(null, "", "#" + current); status("正在保存，请等待操作完成"); return; }
    const hash = location.hash.slice(1), fallback = Object.keys(pages).find(key => allowed(pages[key][3]));
    if (!fallback) { empty(workspace, "没有可用的运维权限", "请使用访问口令登录。"); workspace.setAttribute("aria-busy", "false"); $("refresh").disabled = true; return; }
    current = pages[hash] && allowed(pages[hash][3]) ? hash : fallback;
    if (hash !== current) history.replaceState(null, "", "#" + current);
    status(""); render();
  }
  window.addEventListener("hashchange", navigate);
  $("refresh").onclick = () => { if (!mutating) { status(""); render(); } };
  $("signout").onclick = async () => { try { await api("/api/logout", {}); window.S2Auth.clear(); location.replace("/login"); } catch (error) { status(error.message, true); } };
  (async () => {
    try {
      principal = await api("me"); $("identity").textContent = ({owner: "访问口令已验证", local: "设备本机访问", guest: "只读访问"}[principal.kind] || "访问密钥");
      for (const [key, [label, , , scope]] of Object.entries(pages)) if (allowed(scope)) {
        const btn = button(label, () => { if (mutating) { status("正在保存，请等待操作完成"); return; } if (current !== key) location.hash = key; }); btn.dataset.page = key; $("navigation").append(btn);
      }
      navigate();
    } catch (error) { workspace.setAttribute("aria-busy", "false"); status(error.message, true); empty(workspace, "连接失败", "请刷新页面重试。"); }
  })();
})();
