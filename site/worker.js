// icelens in a Web Worker (a module worker). The page (demo.js) owns the terminal; this owns Python.
//
// Pyodide runs here rather than on the page because PyIceberg is synchronous: a scan that
// takes a second would otherwise freeze the tab for that second, scrolling and all.
//
// Messages in:  {type: "start", cols, rows, dark}  {type: "input", data}  {type: "resize", cols, rows}
//               {type: "theme", dark}: the page switched between light and dark
// Messages out: {type: "progress", done, total, label}  {type: "output", data}  {type: "ready"}
//               {type: "sql", ready}  {type: "exit", reason}  {type: "error", message}
//               {type: "mark", name, t}: where the time went, for anyone measuring the page

import { loadIcelens } from "./loader.mjs";

let session = null;
let pending = "";
let flushing = false;

function post(message) {
  self.postMessage(message);
}

function mark(name, extra = {}) {
  post({ type: "mark", name, t: Math.round(performance.now()), ...extra });
}

// Textual writes a frame in many small pieces; one message per frame keeps the page's
// terminal from repainting a dozen times for one screen.
function write(data) {
  pending += data;
  if (!flushing) {
    flushing = true;
    setTimeout(() => {
      flushing = false;
      const data = pending;
      pending = "";
      if (data) post({ type: "output", data });
    }, 0);
  }
}

const STAGES = 4;
const stage = (done, label) => post({ type: "progress", done, total: STAGES, label });

// The site's compiled modules (icelens.bytecode): 4 MB that saves about 1.8 s of compiling
// Python in this tab. On a link slow enough that 4 MB takes longer than that, it is skipped
// where the browser says the link is slow; anything that goes wrong only means compiling instead.
function fetchBundle(latest) {
  const link = self.navigator.connection;
  if (link && (link.saveData || /(^|-)2g$|^3g$/.test(link.effectiveType || ""))) return null;
  return fetchBytes(latest.bytecode);
}

// For what the site prebuilt: a missing or failed file only means the page does that work itself.
async function fetchBytes(path) {
  if (!path) return null;
  try {
    const response = await fetch(path);
    return response.ok ? new Uint8Array(await response.arrayBuffer()) : null;
  } catch {
    return null;
  }
}

async function boot() {
  stage(0, "Downloading Python");
  // A 404 or a 503 is said as one, rather than as the JSON parser choking on an error page.
  const json = async (url, init = { cache: "no-store" }) => {
    const response = await fetch(url, init);
    if (!response.ok) throw new Error(`${url}: HTTP ${response.status}`);
    return response.json();
  };
  const manifest = await json("pyodide.json");
  const indexURL = `https://cdn.jsdelivr.net/pyodide/v${manifest.pyodide}/full/`;
  // The runtime, Pyodide's lockfile and the site's own list all at once: none waits on another.
  // A module worker: Pyodide 314 is ES modules, and its classic loader fails under importScripts.
  const [{ loadPyodide }, lock, [latest, bundle, demo]] = await Promise.all([
    import(indexURL + "pyodide.mjs"),
    json(indexURL + "pyodide-lock.json", {}),
    json("wheels/latest.json").then((latest) => Promise.all([latest, fetchBundle(latest), fetchBytes(latest.demo)])),
  ]);
  const pyodide = await loadIcelens({
    loadPyodide,
    lock,
    latest,
    manifest,
    wheelsBase: new URL("wheels/", self.location.href).href,
    options: { indexURL, packageBaseUrl: indexURL },
    onLoading: (names) => {
      mark("runtime");
      stage(1, `Downloading ${names.length} Python packages`);
    },
  });
  mark("packages");
  // Before anything imports icelens.browser: from here on, imports come compiled from the bundle.
  if (bundle) {
    pyodide.FS.writeFile("/tmp/bytecode.xz", bundle);
    const held = pyodide.runPython("import icelens.bytecode; icelens.bytecode.install('/tmp/bytecode.xz')");
    mark("bytecode", { modules: held });
  }
  stage(2, "Opening the demo table");
  const browser = pyodide.pyimport("icelens.browser");
  // The demo's five commits, written when the site was built; without them prepare() writes
  // them here, which is slower but the same table.
  if (demo) {
    pyodide.FS.writeFile("/tmp/demo.tar.xz", demo);
    browser.restore_demo("/tmp/demo.tar.xz");
  }
  browser.prepare();
  mark("prepared");
  return { pyodide, browser, manifest };
}

// Booting needs no terminal, so it starts the moment the page creates this worker - before
// the page has loaded xterm.js or measured a terminal to ask for a size. A failed boot is not
// retried here: Pyodide may be half loaded, so the page starts a fresh worker instead.
let booting = boot();
booting.catch(() => {});
let started = false;

// DuckDB answers only the SQL pane, so it loads after the first frame instead of before it.
async function loadLater({ pyodide, manifest }) {
  try {
    await pyodide.loadPackage(manifest.later, { messageCallback: () => {} });
    mark("later");
    post({ type: "sql", ready: true });
  } catch (err) {
    console.warn("DuckDB did not load; SQL will say so", err);
  }
}

async function start(cols, rows, dark) {
  try {
    const booted = await booting;
    stage(3, "Starting icelens");
    const app = await booted.browser.start.callKwargs(write, cols, rows, (reason) => {
      session = null;
      post({ type: "exit", reason: String(reason || "") });
    }, { dark: dark !== false });
    // An app that stopped before its first frame has already said so with `exit`; it is not
    // ready, and input sent to it would go nowhere.
    if (!app.running) return;
    session = app;
    const first = !started;
    started = true;
    mark("first frame");
    // Bytes by host, as far as the browser will say: a host that sends no
    // Timing-Allow-Origin (PyPI's) reports 0, so this is jsDelivr's and the site's own.
    const bytes = {};
    for (const r of performance.getEntriesByType("resource")) {
      const host = new URL(r.name).host;
      bytes[host] = (bytes[host] || 0) + (r.transferSize || 0);
    }
    mark("bytes", { bytes });
    post({ type: "ready" });
    if (first) loadLater(booted);
  } catch (err) {
    // A Python exception arrives with its whole traceback; the page shows its last line.
    const lines = String(err && err.message ? err.message : err).trim().split("\n");
    post({ type: "error", message: lines[lines.length - 1] });
  }
}

self.onmessage = (event) => {
  const msg = event.data;
  if (msg.type === "start") {
    start(msg.cols, msg.rows, msg.dark);
  } else if (msg.type === "input" && session) {
    session.feed(msg.data);
  } else if (msg.type === "resize" && session) {
    session.resize(msg.cols, msg.rows);
  } else if (msg.type === "theme" && session) {
    session.set_theme(msg.dark);
  }
};
