// node tests/capture_readme.mjs; requires Chrome and Python, no npm dependencies.
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
const fixture = spawn(process.env.PYTHON || "python", ["-I", "-B", join(import.meta.dirname, "readme_fixture.py")],
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
  const output=join(root,"docs","screenshots","v1.0.2"); mkdirSync(output,{recursive:true});
  const shot = async name => {
    await ev(`document.fonts.ready`);
    check(await ev(`document.documentElement.scrollWidth <= innerWidth + 1`),name+" fits viewport");
    const result=await cmd("Page.captureScreenshot",{format:"png"});
    writeFileSync(join(output,name+".png"),Buffer.from(result.data,"base64"));
    console.log("Captured "+name);
  };
  await cmd("Emulation.setTimezoneOverride",{timezoneId:"Asia/Shanghai"});
  await cmd("Emulation.setDeviceMetricsOverride",{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
  await cmd("Page.navigate",{url:base+"/login"});
  await until(`!!document.getElementById('tk')`,"login");
  await shot("login-desktop");
  await ev(`document.getElementById('tk').value='fixture-password'; document.querySelector('form').requestSubmit()`);
  await until(`document.getElementById('set-host')?.textContent==='fnOS-S2-DEMO' && document.getElementById('cpu-model')?.textContent.includes('RK3568')`,"overview data");
  await ev(`document.querySelector('#poll-seg [data-poll="1"]')?.click()`);
  await sleep(2500);
  await sleep(12000);
  await shot("overview-desktop");
  await ev(`document.querySelector('.topbar a[href="/manage.html"]').click()`);
  await until(`document.querySelectorAll('#navigation button').length===11 && !document.getElementById('refresh').disabled`,"operations");
  await shot("operations-desktop");
  for(const [page,name] of [["history","history-desktop"],["backups","backups-desktop"],["schedule","schedule-desktop"],["guard","guard-desktop"]]) {
    await ev(`document.querySelector('#navigation [data-page="${page}"]').click()`);
    await until(`location.hash==='#${page}' && !document.getElementById('refresh').disabled`,page);
    await shot(name);
  }
  await cmd("Emulation.setDeviceMetricsOverride",{width:390,height:844,deviceScaleFactor:1,mobile:true});
  await ev(`document.querySelector('#navigation [data-page="history"]').click()`);
  await until(`location.hash==='#history' && !document.getElementById('refresh').disabled`,"mobile history");
  await shot("history-mobile");
  await ev(`document.querySelector('a[href="/"]').click()`);
  await until(`document.getElementById('set-host')?.textContent==='fnOS-S2-DEMO' && document.getElementById('cpu-model')?.textContent.includes('RK3568')`,"mobile overview");
  await sleep(2500);
  await shot("overview-mobile");
  check(errors.length===0,"documentation capture has no uncaught frontend errors");
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
