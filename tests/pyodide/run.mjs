// The browser build, checked where CI can run it: the built site's wheel under Pyodide in Node.
//
//   cd tests/pyodide && npm ci && node run.mjs ../../_site
//
// Loads Python exactly as the page's worker does (boot.mjs), installs the site's compiled modules
// and demo files as the worker does, then runs checks.py. Exit status is the checks' status.
import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { bootSite } from "./boot.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const site = process.argv[2] && resolve(process.argv[2]);
if (!site) {
  console.error("usage: node run.mjs path/to/_site   (built by scripts/build_site.py)");
  process.exit(2);
}
const { py, manifest, latest, loading } = await bootSite(site);
// As the worker does: the compiled modules first, then the demo's files, before the checks
// import anything else.
let bundled = 0, restored = "";
if (latest.bytecode) {
  py.FS.writeFile("/tmp/bytecode.xz", readFileSync(join(site, latest.bytecode)));
  bundled = py.runPython("import icelens.bytecode; icelens.bytecode.install('/tmp/bytecode.xz')");
}
if (latest.demo) {
  py.FS.writeFile("/tmp/demo.tar.xz", readFileSync(join(site, latest.demo)));
  restored = py.runPython("import icelens.browser; icelens.browser.restore_demo('/tmp/demo.tar.xz') or ''");
}
py.globals.set("CHECKS", readFileSync(join(here, "checks.py"), "utf8"));
await py.runPythonAsync('ns = {"__name__": "checks"}\nexec(compile(CHECKS, "checks.py", "exec"), ns)');
const phase = (name, args = "") => py.runPythonAsync(`await ns["main"](${JSON.stringify(name)}${args})`);
py.globals.set("LOADED", loading);
py.globals.set("DROPPED", manifest.drop);
py.globals.set("BUNDLED", bundled);
py.globals.set("RESTORED", restored);
let failed = await phase("page", ", list(LOADED), list(DROPPED), BUNDLED, RESTORED");
await py.loadPackage(manifest.later, { messageCallback: () => {} });
failed += await phase("sql");
// The page never loads SQLAlchemy; the command line's sandbox is SQLite and needs it.
await py.loadPackage(["sqlalchemy"], { messageCallback: () => {} });
failed += await phase("cli");
console.log(failed ? `${failed} failed` : "all browser checks passed");
process.exit(failed ? 1 : 0);
