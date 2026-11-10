// node tests/browser_security.mjs; requires Chrome and Python, no npm dependencies.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { mkdtempSync, rmSync, realpathSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, relative, resolve } from "node:path";
import { once } from "node:events";

const sleep = ms => new Promise(r => setTimeout(r, ms));
const root = resolve(import.meta.dirname, "..");
const profile = mkdtempSync(join(tmpdir(), "s2-review-"));
const fixture = spawn(process.env.PYTHON || "python", ["-I", "-B", join(import.meta.dirname, "browser_fixture.py")],
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
  await cmd("Network.setCookie", {name: "s2token", value: "fixture-password", url: base, path: "/", httpOnly: true});
  await cmd("Emulation.setTimezoneOverride", {timezoneId: "America/New_York"});
  await cmd("Page.navigate", {url: base + "/"});
  await until(`location.pathname === '/login' && !!document.querySelector('form')`, "login redirect");
  check(true, "protected data redirects an unauthenticated browser to login");
  await ev(`document.getElementById('tk').value='incorrect'; document.querySelector('form').requestSubmit()`);
  await until(`!document.getElementById('login-error').hidden`, "wrong password");
  check(await ev(`document.getElementById('login-error').textContent.includes('口令不正确') && !document.querySelector('form button').disabled`), "wrong password stays on login with a usable retry button");
  await ev(`document.getElementById('tk').value='fixture-password'; document.querySelector('form').requestSubmit()`);
  await until(`location.pathname === '/' && document.getElementById('set-host')?.textContent === 'fixture-host'`, "login complete");
  const session = await ev(`sessionStorage.getItem('s2-session')`);
  check(session && session !== "fixture-password", "login stores a random session scoped to this origin");
  const cookies = await cmd("Network.getCookies", {urls: [base]});
  check(!cookies.cookies.some(c => c.name === "s2token"), "login creates no password cookie");
  check(await ev(`document.getElementById('ov-sch-gap').textContent === '1小时 0分'`), "schedule countdown uses device UTC+8 while browser uses New York");
  check(await ev(`!window.xss && !document.querySelector('#script-list img') && document.querySelector('#script-list').textContent.includes('<img')`), "script names render as text, including hostile markup");
  for (const width of [1440, 390, 360]) {
    await cmd("Emulation.setDeviceMetricsOverride", {width, height: 900, deviceScaleFactor: 1, mobile: width < 620});
    for (const view of ["overview", "control"]) {
      await ev(`document.querySelector('${width < 620 ? '.tabbar' : '.topbar'} [data-view="${view}"]').click()`);
      await sleep(200);
      check(await ev(`document.documentElement.scrollWidth <= innerWidth + 1`), `${width}px ${view} has no horizontal page overflow`);
    }
  }
  await ev(`document.querySelector('.tabbar [data-view="overview"]').click()`);
  await control({fail_schedule: true});
  await ev(`document.getElementById('ov-sch-on').click()`);
  await until(`document.getElementById('toast-msg').textContent.includes('模拟保存失败')`, "failed schedule");
  check(await ev(`document.getElementById('ov-sch-on').checked && !document.getElementById('ov-sch-on').disabled`), "failed schedule rolls back and releases controls");
  await control({schedule: {shutdown_time: "11:00"}});
  await ev(`document.getElementById('btn-refresh').click()`);
  await until(`document.getElementById('ov-sch-off').textContent === '11:00'`, "refresh after failed schedule");
  check(true, "polling can refresh schedule after a failed save");
  await control({fail_schedule: false});
  await ev(`document.getElementById('ov-sch-on').click()`);
  await until(`document.getElementById('toast-msg').textContent.includes('定时计划已停用')`, "successful schedule");
  check(await ev(`!document.getElementById('ov-sch-on').checked`), "authorized frontend hardware mutation succeeds");
  let stats = await (await fetch(base + "/__fixture/stats")).json();
  check(stats.posts.filter(p => p.path === "/api/hw").every(p => p.bearer && p.marker === "1" && !p.cookie), "frontend writes send bearer plus CSRF header and omit cookies");
  // Force malformed responses and accelerated aborts at the transport boundary.
  await ev(`window.realFetch = fetch; window.fetch = (url, options) => url === '/api/hw' ? Promise.resolve(new Response('[]',{headers:{'Content-Type':'application/json'}})) : realFetch(url,options); document.getElementById('ov-sch-on').click()`);
  await until(`document.getElementById('toast-msg').textContent.includes('设备返回的数据无法识别')`, "malformed response");
  check(await ev(`!document.getElementById('ov-sch-on').disabled`), "malformed API response shows an error and releases controls");
  await ev(`window.realSetTimeout = setTimeout; window.setTimeout = (fn, ms, ...args) => realSetTimeout(fn, ms === 60000 ? 30 : ms, ...args); window.fetch = (url, options) => url === '/api/hw' ? new Promise((resolve,reject) => options.signal.addEventListener('abort', () => reject(new DOMException('aborted','AbortError')))) : realFetch(url,options); document.getElementById('ov-sch-on').click()`);
  await until(`document.getElementById('toast-msg').textContent.includes('设备响应超时')`, "request timeout");
  check(await ev(`!document.getElementById('ov-sch-on').disabled`), "request timeout releases controls without retry");
  await ev(`window.fetch = realFetch; window.setTimeout = realSetTimeout`);
  // Same host, different port: old code sent the password cookie here and accepted the POST.
  const peerCookies = [];
  peer = createServer((req, res) => {
    peerCookies.push(req.headers.cookie || "");
    res.setHeader("Content-Type", "text/html");
    res.end(`<script>window.peerSession=sessionStorage.getItem('s2-session'); fetch('${base}/api/script/save',{method:'POST',credentials:'include',headers:{'Content-Type':'text/plain'},body:JSON.stringify({name:'cross-origin.sh',content:'echo blocked'})}).catch(()=>{}).finally(()=>window.attackDone=true)</script>`);
  });
  peer.listen(0, "127.0.0.1"); await once(peer, "listening");
  const beforeJobs = stats.jobs.length;
  await cmd("Page.navigate", {url: `http://127.0.0.1:${peer.address().port}/`});
  await until(`window.attackDone === true`, "hostile peer request");
  check(await ev(`window.peerSession === null`), "another port cannot read the origin-scoped session");
  check(peerCookies.every(c => !c.includes("s2token")), "another port receives no password cookie");
  stats = await (await fetch(base + "/__fixture/stats")).json();
  check(stats.jobs.length === beforeJobs && stats.posts.some(p => p.path === "/api/script/save"), "real cross-origin browser POST reaches the guard without execution");
  await cmd("Page.navigate", {url: base + "/"});
  await until(`document.getElementById('set-host')?.textContent === 'fixture-host'`, "return to app");
  await ev(`document.getElementById('logout').click()`);
  await until(`location.pathname === '/login'`, "logout");
  check((await fetch(base + "/api/state", {headers: {Authorization: "Bearer " + session}})).status === 401, "logout revokes the server-side session");
  await control({password: false});
  await cmd("Page.navigate", {url: base + "/"});
  await until(`document.getElementById('acc-token')?.textContent === '未开启'`, "read-only mode");
  check(await ev(`document.getElementById('ov-sch-on').disabled && document.getElementById('port-save').disabled`), "password-free remote UI disables schedule and port writes");
  check(errors.length === 0, "no uncaught frontend exceptions during all flows");
  console.log(`Browser checks passed: ${checks}`);
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
