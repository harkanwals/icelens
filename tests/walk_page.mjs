// Drives the built home page in headless Chrome for tests/test_walk_page.py: performs each
// action on its terminal and records the screen after it, as text and style per run.
//   node walk_page.mjs CHROME URL ACTIONS.json OUT.json PROFILE_DIR
import { spawn } from "node:child_process";
import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { join } from "node:path";

const [binary, url, actionsFile, out, dir] = process.argv.slice(2);
const actions = JSON.parse(readFileSync(actionsFile, "utf8"));
const chrome = spawn(binary, ["--headless=new", "--remote-debugging-port=0", `--user-data-dir=${dir}`,
  "--no-first-run", "--no-default-browser-check",
  // A test never reaches the internet: every name but this machine's fails to resolve.
  "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1", "about:blank"], { stdio: "ignore" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const done = (code) => { chrome.kill(); process.exit(code); };
setTimeout(() => { console.error("timed out"); done(1); }, 120000);

// Port 0: Chrome picks one and writes it here, so tests never collide over a port. A loaded CI
// runner has taken over ten seconds to start Chrome.
const portFile = join(dir, "DevToolsActivePort");
for (let i = 0; i < 600 && !existsSync(portFile); i++) await sleep(100);
if (!existsSync(portFile)) { console.error("Chrome did not start within 60s"); done(1); }
const [port, path] = readFileSync(portFile, "utf8").split("\n");
const ws = new WebSocket(`ws://127.0.0.1:${port}${path}`);
await new Promise((r) => (ws.onopen = r));
let id = 0;
const waiting = new Map(), errors = [];
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.method === "Runtime.exceptionThrown") errors.push(m.params.exceptionDetails.exception?.description ?? m.params.exceptionDetails.text);
  if (m.id && waiting.has(m.id)) { waiting.get(m.id)(m); waiting.delete(m.id); }
};
const send = (method, params = {}, sessionId) => new Promise((r) => {
  const i = ++id; waiting.set(i, r); ws.send(JSON.stringify({ id: i, method, params, sessionId }));
});
const { result: { targetId } } = await send("Target.createTarget", { url: "about:blank" });
const { result: { sessionId: page } } = await send("Target.attachToTarget", { targetId, flatten: true });
const ev = async (expression) => (await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true }, page)).result?.result?.value;
await send("Page.enable", {}, page);
await send("Runtime.enable", {}, page);
await send("Emulation.setDeviceMetricsOverride", { width: 1400, height: 1300, deviceScaleFactor: 1, mobile: false }, page);
// No tour: a visitor who asks for less motion gets the first chapter, still.
await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-reduced-motion", value: "reduce" }] }, page);
await send("Page.navigate", { url }, page);
for (let i = 0; i < 100 && !(await ev(`!!document.querySelector('.shot.live')`)); i++) await sleep(100);
await ev(`document.documentElement.dataset.theme = 'dark'; document.querySelector('.shot').scrollIntoView()`);

const screen = `[...document.querySelectorAll('.walk pre')].find((p) => getComputedStyle(p).display !== 'none')`;
const grab = () => ev(`(() => {
  const pre = ${screen};
  return { theme: pre.classList.contains('t-light') ? 'light' : 'dark', pins: document.querySelectorAll('.walk .pin').length, rows: pre.innerHTML.split('\\n').map((line) => {
    const d = document.createElement('div'); d.innerHTML = line;
    return [...d.childNodes].map((n) => [n.textContent, n.nodeType === 3 ? -1 : +n.className.slice(1)]);
  }) };
})()`);
const box = () => ev(`(() => { const r = ${screen}.getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; })()`);
const cols = await ev(`fetch(document.querySelector('.shot').dataset.walk).then((r) => r.json()).then((w) => [w.cols, w.rows])`);
const mouse = async (type, x, y) => send("Input.dispatchMouseEvent", { type, x, y, button: "left", clickCount: 1 }, page);
const VK = { ArrowDown: 40, ArrowUp: 38, ArrowLeft: 37, ArrowRight: 39, Enter: 13, Escape: 27, Tab: 9, PageDown: 34, PageUp: 33, Home: 36, End: 35 };

// A click on the title row, which the app ignores, gives the terminal the keyboard; then the
// mouse leaves it. Time stops for the page, so no toast expires between two actions. That click
// takes over from the tour and clears its pins, so actions that click a pin go without it.
let b = await box();
if (actions[0]?.[0] !== "pin") {
  await mouse("mouseMoved", b[0] + b[2] / 2, b[1] + 2);
  await mouse("mousePressed", b[0] + b[2] / 2, b[1] + 2);
  await mouse("mouseReleased", b[0] + b[2] / 2, b[1] + 2);
  await mouse("mouseMoved", 2, 2);
}
for (let i = 0; i < 100 && !(await ev(`!!${screen}.textContent.trim()`)); i++) await sleep(100);
await sleep(200);
await send("Emulation.setVirtualTimePolicy", { policy: "pause" }, page);
const shots = [await grab()];
for (const [kind, a, c] of actions) {
  if (kind === "click") {
    b = await box();
    const x = b[0] + ((a + 0.5) / cols[0]) * b[2], y = b[1] + ((c + 0.5) / cols[1]) * b[3];
    await mouse("mouseMoved", x, y);
    await mouse("mousePressed", x, y);
    await mouse("mouseReleased", x, y);
  } else if (kind === "pin") {
    // A pin that is gone is not clicked, so the test says how many are left rather than crash.
    const at = await ev(`(() => { const p = document.querySelectorAll('.walk .pin b')[${a - 1}]; if (!p) return null; const r = p.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; })()`);
    if (at) {
      await mouse("mouseMoved", ...at);
      await mouse("mousePressed", ...at);
      await mouse("mouseReleased", ...at);
    }
  } else if (kind === "theme") {
    await ev(`document.documentElement.dataset.theme = ${JSON.stringify(a)}`);
  } else {
    const one = a.length === 1;
    await send("Input.dispatchKeyEvent", { type: "keyDown", key: a, code: one ? "" : a, text: one ? a : undefined,
      windowsVirtualKeyCode: VK[a] ?? a.toUpperCase().charCodeAt(0) }, page);
    await send("Input.dispatchKeyEvent", { type: "keyUp", key: a }, page);
  }
  await sleep(60);
  shots.push(await grab());
}
writeFileSync(out, JSON.stringify({ shots, errors }));
done(0);
