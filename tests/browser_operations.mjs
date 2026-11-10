// node tests/browser_operations.mjs; requires Chrome and Python, no npm dependencies.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { mkdtempSync, rmSync, realpathSync, mkdirSync, readFileSync, existsSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, relative, resolve } from "node:path";
import { once } from "node:events";

const sleep = ms => new Promise(r => setTimeout(r, ms));
const root = resolve(import.meta.dirname, "..");
const profile = mkdtempSync(join(tmpdir(), "s2-review-"));
const fixture = spawn(process.env.PYTHON || "python", ["-I", "-B", join(import.meta.dirname, "browser_fixture.py"), "--ops"],
  { cwd: root, stdio: ["pipe", "pipe", "inherit"], windowsHide: true });
let chrome, ws, peer, checks = 0;
const check = (condition, message) => { assert.ok(condition, message); checks++; console.log("PASS " + message); };
const pending = new Map();
let id = 0;
const cmd = (method, params = {}) => new Promise((res, rej) => {
  const n = ++id;
  const timer = setTimeout(() => { pending.delete(n); rej(new Error("CDP timeout: " + method)); }, 15000);
  pending.set(n, {res, rej, timer});
  ws.send(JSON.stringify({id: n, method, params}));
});
const ev = async expression => {
  const result = await cmd("Runtime.evaluate", {expression, returnByValue: true, awaitPromise: true});
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
  return result.result?.value;
};
const until = async (expression, message) => {
  for (let i = 0; i < 80; i++) { if (await ev(expression)) return; await sleep(100); }
  throw new Error("Timed out: " + message);
};
const errors = [];
try {
  const line = await new Promise((res, rej) => {
    let output = "";
    fixture.stdout.on("data", data => { output += data; if (output.includes("\n")) res(output.split("\n")[0]); });
    fixture.on("exit", code => { if (!output) rej(new Error("fixture exited: " + code)); });
  });
  const {port} = JSON.parse(line), base = `http://127.0.0.1:${port}`;
  const control = async body => {
    const response = await fetch(base + "/__fixture/control", {method: "POST", body: JSON.stringify(body)});
    assert.equal(response.status, 200);
  };
  chrome = spawn(process.env.CHROME || "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe", [
    "--headless=new", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "--no-first-run", "--disable-background-networking", "about:blank"
  ], {stdio: ["ignore", "ignore", "pipe"], windowsHide: true});
  const debug = await new Promise((res, rej) => {
    let output = "";
    const timer = setTimeout(() => rej(new Error("Chrome startup timed out")), 15000);
    chrome.stderr.on("data", data => {
      output += data;
      const match = output.match(/DevTools listening on ws:\/\/127\.0\.0\.1:(\d+)/);
      if (match) { clearTimeout(timer); res(match[1]); }
    });
    chrome.on("error", rej);
  });
  const targets = await (await fetch(`http://127.0.0.1:${debug}/json/list`)).json();
  ws = new WebSocket(targets.find(t => t.type === "page").webSocketDebuggerUrl);
  await new Promise(res => ws.addEventListener("open", res, {once: true}));
  ws.addEventListener("message", event => {
    const data = JSON.parse(event.data);
    if (data.id && pending.has(data.id)) {
      const item = pending.get(data.id); pending.delete(data.id); clearTimeout(item.timer);
      if (data.error) item.rej(new Error(data.error.message)); else item.res(data.result);
    }
    if (data.method === "Runtime.exceptionThrown") errors.push(data.params.exceptionDetails.text);
  });
  await cmd("Page.enable"); await cmd("Runtime.enable"); await cmd("Network.enable");
  const downloads = join(profile, "downloads"); mkdirSync(downloads);
  await cmd("Browser.setDownloadBehavior", {behavior: "allow", downloadPath: downloads});
  await cmd("Page.navigate", {url: base + "/login"});
  await until(`!!document.getElementById('tk')`, "login form");
  await ev(`document.getElementById('tk').value='fixture-password'; document.querySelector('form').requestSubmit()`);
  await until(`location.pathname === '/' && !!document.getElementById('set-host')`, "login");
  await cmd("Page.navigate", {url: base + "/manage.html"});
  await until(`document.querySelectorAll('#navigation button').length === 12 && !document.getElementById('refresh').disabled && !!document.querySelector('#workspace table')`, "operations health");
  check(await ev(`document.getElementById('workspace').textContent.includes('当前无活动告警')`), "health report and diagnostics render from backend");
  const nav = async key => {
    await ev(`document.querySelector('#navigation [data-page="${key}"]').click()`);
    await until(`location.hash === '#${key}' && !document.getElementById('refresh').disabled`, "navigation " + key);
    check(await ev(`document.getElementById('status').hidden`), key + " page loads successfully");
  };
  const set = async (label, value, checkbox=false) => ev(`(() => { const el=document.querySelector('[aria-label='+${JSON.stringify(JSON.stringify(label))}+']'); el.${checkbox ? "checked" : "value"}=${JSON.stringify(value)}; el.dispatchEvent(new Event('change',{bubbles:true})); })()`);
  const click = label => ev(`Array.from(document.querySelectorAll('#workspace button')).find(b => b.textContent === ${JSON.stringify(label)}).click()`);
  const saved = message => until(`document.getElementById('status').textContent.includes(${JSON.stringify(message)}) && !document.getElementById('refresh').disabled`, message);
  await nav("history");
  check(await ev(`document.querySelectorAll('svg.chart polyline').length === 1`), "persistent telemetry renders a real trend chart");
  await click("导出 CSV");
  for(let i=0;i<30&&!existsSync(join(downloads,"s2-history.csv"));i++) await sleep(100);
  check(readFileSync(join(downloads,"s2-history.csv"),"utf8").includes("cpu_temp"), "history CSV downloads with recorded values");
  await set("记录历史数据", "off"); await click("保存保留期限"); await saved("历史记录设置已更新");
  check(await ev(`document.getElementById('workspace').textContent.includes('历史记录已暂停')`), "history recording can be paused");
  check(await ev(`document.querySelectorAll('svg.chart polyline').length === 1`), "pausing history preserves existing records");
  await set("记录历史数据", "on"); await click("保存保留期限"); await saved("历史记录设置已更新");
  check(await ev(`document.getElementById('workspace').textContent.includes('历史记录已开启')`), "history recording can be resumed");
  await nav("disks"); check(await ev(`document.getElementById('workspace').textContent.includes('reallocated')`), "disk history displays SMART samples");
  await nav("backups"); await click("创建并下载备份"); await saved("备份已保存");
  for(let i=0;i<30&&!existsSync(join(downloads,"s2-config-1.json"));i++) await sleep(100);
  const backup = JSON.parse(readFileSync(join(downloads,"s2-config-1.json"),"utf8"));
  check(backup.format === "s2-config-v1" && backup.config.guard.enabled === false, "configuration backup downloads and contains hardware settings");
  await nav("guard"); await set("启用保护",true,true); await set("CPU 阈值（50–85℃）",75); await click("保存保护策略"); await saved("保护策略已保存");
  check(await ev(`document.querySelector('[aria-label="CPU 阈值（50–85℃）"]').value === '75'`), "fan protection form saves and reloads policy");
  await nav("backups");
  await ev(`Array.from(document.querySelectorAll('tbody tr')).find(r=>r.textContent.includes('手动备份')).querySelector('button').click()`);
  await until(`document.getElementById('review').open`, "restore diff");
  check(await ev(`document.getElementById('review-diff').textContent.includes('75') && document.getElementById('review-diff').textContent.includes('80')`), "restore preview shows differences before applying");
  await ev(`document.getElementById('review-apply').click()`); await saved("配置已恢复");
  await nav("guard"); check(await ev(`document.querySelector('[aria-label="CPU 阈值（50–85℃）"]').value === '80' && !document.querySelector('[aria-label="启用保护"]').checked`), "configuration rollback restores previous values");
  await nav("schedule"); await set("启用计划",true,true); await set("关机时间（留空不关机）","21:00"); await set("开机时间（留空不开机）","08:00"); await set("周日",false,true); await set("临时跳过日期（每行 YYYY-MM-DD，最多 64 天）","2026-12-25"); await click("保存周计划"); await saved("计划已保存");
  check(await ev(`!document.querySelector('[aria-label="周日"]').checked && document.getElementById('workspace').textContent.includes('下次关机')`), "weekly schedule persists selected days and previews next run");
  await click("开始维护"); await saved("维护模式已启用");
  check(await ev(`document.getElementById('workspace').textContent.includes('维护至')`), "maintenance mode displays its automatic expiry");
  await click("立即结束维护"); await saved("维护模式已结束");
  await nav("notifications"); await set("Webhook 地址","https://notify.example/alerts"); await set("Bearer 凭据（留空保留原值）","fixture-notify-secret"); await set("启用通知",true,true); await click("保存通知设置"); await saved("通知设置已保存");
  check(await ev(`document.querySelector('[aria-label="Bearer 凭据（留空保留原值）"]').value === '' && !document.getElementById('workspace').textContent.includes('fixture-notify-secret')`), "saved notification credentials are not exposed back to the page");
  await nav("audit"); check(await ev(`document.querySelectorAll('tbody tr').length >= 5`), "audit page lists performed actions");
  await nav("users"); await set("账号（2–32 位）","admin-fixture"); await set("新密码（12–128 位；编辑时留空保持）","fixture-user-password"); await set("权限","admin"); await click("保存账号"); await saved("账号已保存");
  await set("账号（2–32 位）","viewer-fixture"); await set("新密码（12–128 位；编辑时留空保持）","fixture-user-password"); await set("权限","viewer"); await click("保存账号"); await saved("账号已保存");
  check(await ev(`document.querySelectorAll('tbody tr').length === 2`), "account creation supports admin and viewer roles");
  await nav("keys"); await set("用途名称","browser-monitor"); await click("创建密钥"); await saved("密钥已创建");
  const key = await ev(`document.querySelector('#workspace pre').textContent`);
  check(key.startsWith("s2k_"), "API key is displayed once after creation");
  const response = await fetch(base + "/api/ops/users", {headers:{Authorization:"Bearer " + key}});
  check(response.status === 403, "new read-only key cannot access accounts");
  await nav("devices"); await set("设备名称","Fixture NAS"); await set("控制台地址（建议 HTTPS）","https://nas.example"); await set("目标设备只读密钥",key); await click("添加设备"); await saved("设备已添加");
  check(await ev(`document.getElementById('workspace').textContent.includes('Fixture NAS') && document.getElementById('workspace').textContent.includes('在线')`), "fleet dashboard aggregates an explicitly registered device using a mock transport");
  await nav("keys"); await click("撤销"); await saved("密钥已撤销");
  check((await fetch(base + "/api/state",{headers:{Authorization:"Bearer " + key}})).status === 401, "revoked key immediately stops working");
  await cmd("Page.navigate", {url: base + "/"});
  await until(`!!document.querySelector('.script .run')`, "script library");
  await ev(`document.querySelector('.topbar [data-view="control"]').click(); document.querySelector('.script .run').click()`);
  await until(`!!document.querySelector('dialog[open]')`, "script review");
  check(await ev(`document.querySelector('dialog[open]').textContent.includes('SHA256') && document.querySelector('dialog[open]').textContent.includes('fixture-safe-test')`), "script run opens a content and digest review before execution");
  await ev(`Array.from(document.querySelectorAll('dialog[open] button')).find(b=>b.textContent==='已审查，执行此版本').click()`);
  await until(`document.getElementById('term-status')?.textContent === '已完成' || document.body.textContent.includes('fixture output')`, "stubbed script completion");
  await cmd("Page.navigate", {url: base + "/manage.html#jobs"});
  await until(`document.querySelector('#navigation [data-page="jobs"]')?.getAttribute('aria-current') === 'page' && !document.getElementById('refresh').disabled`, "jobs page");
  check(await ev(`document.getElementById('workspace').textContent.includes('fixture.sh')`), "job history displays a completed execution and result");
  for(const width of [1440,390,360]) {
    await cmd("Emulation.setDeviceMetricsOverride",{width,height:900,deviceScaleFactor:1,mobile:width<620});
    for(const page of ["health","history","backups","schedule","users","devices"]) {
      await nav(page); check(await ev(`document.documentElement.scrollWidth <= innerWidth + 1`), `${width}px ${page} has no horizontal page overflow`);
    }
  }
  const shots=join(root,"..","_build"); mkdirSync(shots,{recursive:true});
  await nav("health"); const shot=await cmd("Page.captureScreenshot",{format:"png"}); writeFileSync(join(shots,"operations-mobile.png"),Buffer.from(shot.data,"base64"));
  await cmd("Emulation.setDeviceMetricsOverride",{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
  const desktop=await cmd("Page.captureScreenshot",{format:"png"}); writeFileSync(join(shots,"operations-desktop.png"),Buffer.from(desktop.data,"base64"));
  await ev(`document.getElementById('signout').click()`); await until(`location.pathname === '/login' && !!document.getElementById('username')`, "account login");
  await ev(`document.getElementById('username').value='viewer-fixture'; document.getElementById('tk').value='fixture-user-password'; document.querySelector('form').requestSubmit()`);
  await until(`location.pathname === '/'`, "viewer login");
  await cmd("Page.navigate",{url:base+"/manage.html"}); await until(`document.getElementById('identity')?.textContent.includes('viewer-fixture') && !document.getElementById('refresh').disabled`, "viewer center");
  check(await ev(`!document.querySelector('#navigation [data-page="users"]') && !document.querySelector('#navigation [data-page="keys"]')`), "viewer navigation excludes account and key administration");
  await nav("guard"); check(await ev(`Array.from(document.querySelectorAll('#workspace button')).find(b=>b.textContent==='保存保护策略').disabled`), "viewer cannot submit hardware policy changes");
  check(errors.length===0,"no uncaught frontend exceptions across operations workflows");
  console.log(`Operations browser checks passed: ${checks}`);

} finally {
  if (peer) { peer.closeAllConnections(); await new Promise(res => peer.close(res)); }
  if (ws?.readyState === 1) { await cmd("Browser.close").catch(() => {}); ws.close(); }
  if (chrome && chrome.exitCode === null) { chrome.kill(); await sleep(600); }
  fixture.stdin.end("stop\n");
  if (fixture.exitCode === null) await Promise.race([once(fixture, "exit"), sleep(3000)]);
  if (fixture.exitCode === null) fixture.kill();
  const resolved = realpathSync(profile), tempRoot = realpathSync(tmpdir());
  const rel = relative(tempRoot, resolved);
  if (!rel.startsWith("..") && !rel.includes("\\") && !rel.includes("/") && rel.startsWith("s2-review-")) {
    rmSync(resolved, {recursive: true, force: true, maxRetries: 5, retryDelay: 200});
  }
}
