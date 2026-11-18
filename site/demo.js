// The page half of the live demo, on the home page and full screen on try.html: an xterm.js
// terminal wired to icelens in worker.js.
//
// Python takes seconds to load, so until it is ready the terminal shows the app's real first
// screen, captured when the site was built (first-frame.json, one per theme), dimmed and scaled
// to fill the box, under a loading card that says what is happening. Keys typed meanwhile are
// kept and sent the moment the live app takes over. The app follows the page's light or dark
// mode. Where the page's <head> chose not to start loading (a phone, a slow or metered link),
// the card offers a button instead.
(() => {
  const $ = (id) => document.getElementById(id);
  const launch = $("launch"), boot = $("boot"), bar = $("bar"), label = $("label"), steps = $("steps");
  const host = $("terminal"), status = $("status"), sqlNote = $("sql-note");
  // A loading step that has said nothing for this long has hung (a request that never answers);
  // the visitor is told and offered a fresh start rather than left watching a card.
  const PATIENCE = 90000;
  let watchdog = 0;
  // Full screen, the demo is the page and takes the keyboard; on the home page it waits for a
  // click, so the page's own keys (space to scroll, tab to move) keep working.
  const fullScreen = document.body.classList.contains("try");
  let sqlReady = false;
  let worker = null, term = null, fit = null, frame = null;
  // `live`: the app has drawn into the terminal. Before that the terminal holds the preview,
  // sized as it was captured, and must not be refitted or it would wrap into nonsense.
  let live = false;
  const queued = [];
  let lastEscape = 0;
  // Whether a person pressed a key in the terminal before it went live. Not `queued.length`:
  // xterm.js queues its own answers to the app's terminal queries there too.
  let pressed = false;

  // The cells' own colours come from the app's Textual theme (icelens/browser.py); these are
  // for the terminal around them: its background, the cursor, a selection, and plain ANSI.
  const THEMES = {
    dark: {
      background: "#0b141c", foreground: "#e4ecf2", cursor: "#6fd6f7", cursorAccent: "#0b141c",
      selectionBackground: "#6fd6f744",
      black: "#1f2d3a", red: "#f7768e", green: "#7dd3a8", yellow: "#f5d67f", blue: "#8ab4f8",
      magenta: "#c6a0f6", cyan: "#6fd6f7", white: "#d5e2ec",
      brightBlack: "#4b6072", brightRed: "#ff9aac", brightGreen: "#9ee6c1", brightYellow: "#ffe6a3",
      brightBlue: "#a9c8ff", brightMagenta: "#dcc1ff", brightCyan: "#a6e9ff", brightWhite: "#ffffff",
    },
    light: {
      background: "#f6f9fb", foreground: "#0f1b26", cursor: "#0a7ea4", cursorAccent: "#f6f9fb",
      selectionBackground: "#0a7ea433",
      black: "#0f1b26", red: "#c53030", green: "#2f855a", yellow: "#a86b00", blue: "#3d6fb5",
      magenta: "#8b4fc4", cyan: "#0a7ea4", white: "#c3d1dc",
      brightBlack: "#52677a", brightRed: "#e05252", brightGreen: "#38a169", brightYellow: "#c98a10",
      brightBlue: "#5a8bd6", brightMagenta: "#a56ee0", brightCyan: "#1597c2", brightWhite: "#ffffff",
    },
  };
  const isDark = () => (window.icelensTheme ? window.icelensTheme.dark() : matchMedia("(prefers-color-scheme: dark)").matches);

  function say(text) {
    status.textContent = text;
    status.hidden = !text;
  }

  function send(message) {
    if (worker) worker.postMessage(message);
  }

  function watch() {
    clearTimeout(watchdog);
    watchdog = setTimeout(() => fail("nothing has loaded for 90 seconds"), PATIENCE);
  }

  function progress(done, total, text) {
    watch();
    bar.max = total;
    bar.value = done;
    label.textContent = text;
    Array.from(steps.children).forEach((step, i) => {
      step.className = i < done ? "done" : i === done ? "active" : "";
    });
  }

  function makeTerminal() {
    term = new Terminal({
      fontFamily: '"JetBrains Mono", "SF Mono", Menlo, Consolas, "DejaVu Sans Mono", monospace',
      // A phone gets about 40 columns at 14px, too few for the catalog and a table; 12px
      // gives it nearer 50, and the app stacks its panes below 80.
      fontSize: matchMedia("(max-width: 560px)").matches ? 12 : 14,
      cursorBlink: false,
      allowProposedApi: false,
      theme: THEMES[isDark() ? "dark" : "light"],
      // Textual draws every cell itself, so scrollback would only ever hold old frames.
      scrollback: 0,
    });
    fit = new FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open(host);
    fit.fit();
    // A full-screen app with no scrollback turns the wheel into arrow keys; over a page that
    // would trap whoever scrolls past. The wheel is the app's only once the terminal has focus:
    // until then it is stopped on the way down, before xterm.js sees it, and never cancelled, so
    // the browser scrolls the page. (xterm.js's own custom wheel handler still swallows it.)
    host.addEventListener("wheel", (event) => {
      if (!host.contains(document.activeElement)) event.stopPropagation();
    }, { capture: true, passive: true });
    // The app takes every key, Tab and Escape included, so the terminal would be a trap for
    // anyone on a keyboard: Escape twice, quickly, leaves it for whatever follows the demo.
    term.attachCustomKeyEventHandler((event) => {
      if (event.type === "keydown") pressed = true;
      if (event.type !== "keydown" || event.key !== "Escape") return true;
      const now = performance.now();
      if (now - lastEscape > 600) {
        lastEscape = now;
        return true;
      }
      lastEscape = 0;
      leave();
      return false;
    });
    term.onData((data) => (live ? send({ type: "input", data }) : queued.push(data)));
    term.onResize(({ cols, rows }) => live && send({ type: "resize", cols, rows }));
    new ResizeObserver(() => (live ? fit.fit() : scalePreview())).observe(host);
  }

  function leave() {
    const after = Array.from(document.querySelectorAll("a[href], button, [tabindex]:not([tabindex='-1'])")).find(
      (el) => !host.contains(el) && !el.hidden && el.getClientRects().length &&
        host.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING,
    );
    if (after) after.focus();
    else term.blur();
  }

  // The preview is drawn at the size it was captured; scaled, it fills the box as the live
  // app will, whatever the size of the screen.
  function scalePreview() {
    const screen = host.querySelector(".xterm-screen"), xterm = host.querySelector(".xterm");
    if (live || !screen || !xterm || !host.classList.contains("preview")) return;
    const style = getComputedStyle(host);
    const width = host.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    const height = host.clientHeight - parseFloat(style.paddingTop) - parseFloat(style.paddingBottom);
    const scale = Math.min(width / screen.offsetWidth, height / screen.offsetHeight);
    xterm.style.transform = scale > 0 && isFinite(scale) ? `scale(${scale})` : "";
  }

  function drawPreview() {
    if (live || !frame) return;
    const ansi = !isDark() && frame.light ? frame.light : frame.ansi;
    term.reset();
    term.resize(frame.cols, frame.rows);
    term.write(ansi, () => requestAnimationFrame(scalePreview));
  }

  async function showPreview() {
    try {
      frame = await (await fetch("first-frame.json")).json();
      if (live) return;
      host.classList.add("preview");
      drawPreview();
      if (fullScreen) term.focus(); // so keys typed while it loads reach the queue
    } catch (err) {
      // No preview is only a plainer wait; the loading card still says what is happening.
    }
  }

  function goLive() {
    live = true;
    host.classList.remove("preview");
    const xterm = host.querySelector(".xterm");
    if (xterm) xterm.style.transform = "";
    term.reset();
    fit.fit();
  }

  function applyTheme() {
    if (!term) return;
    term.options.theme = THEMES[isDark() ? "dark" : "light"];
    if (live) send({ type: "theme", dark: isDark() });
    else drawPreview();
  }
  addEventListener("icelens-theme", applyTheme);
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyTheme);

  function start() {
    launch.hidden = true;
    bar.hidden = false;
    boot.classList.remove("waiting", "gone");
    say("");
    watch();
    if (!worker) {
      const early = window.icelens;
      worker = (early && early.worker) || new Worker("worker.js", { type: "module" });
      if (early) early.worker = null; // taken: a retry after a failure makes its own
      worker.onmessage = onMessage;
      worker.onerror = (e) => fail(e.message || "the worker could not start");
      if (early) early.early.splice(0).forEach(onMessage);
    }
    // The size the live app should draw at is the fitted one, whatever the preview is showing.
    const size = fit.proposeDimensions() || { cols: term.cols, rows: term.rows };
    send({ type: "start", cols: size.cols, rows: size.rows, dark: isDark() });
  }

  function wait(button, text) {
    clearTimeout(watchdog);
    const hadFocus = host.contains(document.activeElement);
    boot.classList.remove("gone");
    boot.classList.add("waiting");
    bar.hidden = true;
    launch.hidden = false;
    launch.textContent = button;
    label.textContent = text;
    if (hadFocus) launch.focus(); // not left typing into a terminal that has stopped
  }

  function fail(message) {
    // A worker that failed may hold a half-loaded Pyodide, or be the one that never started;
    // either way "Try again" gets a fresh one rather than the same failure.
    if (worker) worker.terminate();
    worker = null;
    progress(0, 4, "");
    wait("Try again", "icelens could not start");
    say("icelens could not start: " + message);
  }

  function onMessage(event) {
    const msg = event.data;
    if (msg.type === "progress") {
      progress(msg.done, msg.total, msg.label);
    } else if (msg.type === "output") {
      if (!live) goLive();
      term.write(msg.data);
    } else if (msg.type === "ready") {
      progress(4, 4, "Ready");
      clearTimeout(watchdog);
      boot.classList.add("gone");
      sqlNote.hidden = sqlReady;
      // The theme may have changed while the app was starting.
      send({ type: "theme", dark: isDark() });
      const typed = queued.splice(0);
      for (const data of typed) send({ type: "input", data });
      if (fullScreen || pressed || host.contains(document.activeElement)) term.focus();
    } else if (msg.type === "exit") {
      // Back to the preview under the card: keys typed while it starts again are kept, and the
      // terminal is refitted for the new app rather than holding the old one's last screen.
      live = false;
      host.classList.add("preview");
      drawPreview();
      wait("Start icelens again", "icelens has stopped");
      say(msg.reason ? "icelens stopped: " + msg.reason : "icelens has quit.");
    } else if (msg.type === "sql") {
      sqlReady = true;
      sqlNote.hidden = true;
    } else if (msg.type === "mark") {
      (window.icelensMarks = window.icelensMarks || []).push(msg);
    } else if (msg.type === "error") {
      fail(msg.message);
    }
  }

  launch.addEventListener("click", start);
  if (typeof Terminal === "undefined" || typeof FitAddon === "undefined") {
    // xterm.js comes from a CDN; without it there is nothing to draw in.
    if (window.icelens && window.icelens.worker) window.icelens.worker.terminate();
    label.textContent = "The terminal could not load";
    bar.hidden = true;
    boot.classList.add("waiting");
    say("xterm.js did not load from cdn.jsdelivr.net. Reload the page to try again.");
    return;
  }
  makeTerminal();
  showPreview();
  // Started as the page opens rather than on a click: loading is most of the wait, and the
  // visitor came to see the thing run. Unless the page's <head> said not to.
  if (window.icelens && window.icelens.auto === false) wait("Start the demo", "The demo downloads about 29 MB, and 8.5 MB more for SQL");
  else start();
})();
