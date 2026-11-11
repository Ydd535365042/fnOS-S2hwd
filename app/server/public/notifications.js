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
  let principal = {scopes:["admin"]}, current = "notifications", generation = 0, fieldId = 0, mutating = 0;
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
      const response = await fetch(path.startsWith("/api/") ? path : "/api/" + path, {
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
    actions(f, button("发送测试通知", async () => { await api("notifications/test", {}); status("测试通知已发送，请检查收件端"); }, "admin"));
    note(f, "发送测试通知会使用已保存的配置。修改设置后请先保存。");
    const delivery = card(root, "投递记录", "失败最多重试 4 次，结果保留在这里。");
    table(delivery, [["时间", r => when(r.ts)], ["状态", r => badge(({sent: "已发送", pending: "等待重试", failed: "失败", suppressed: "维护期间抑制"})[r.status] || r.status, r.status === "sent" ? "good" : r.status === "failed" ? "bad" : "")], ["尝试", r => r.attempts + " 次"], ["说明", r => r.error || "—"]], data.deliveries, ["还没有投递记录", "设置渠道后，新告警的投递结果会显示在这里。"]);
  }

  async function render() {
    const root=text("div","","notify-view"); $("refresh").disabled=true;
    try { await notifications(root); workspace.replaceChildren(root); }
    catch(e) { status(e.message,true); }
    finally { $("refresh").disabled=false; }
  }
  $("refresh").onclick=()=>render();
  render();
})();
