// Does once, when the site is built, what the page would otherwise do in every visitor's tab:
//
//   node prebuild.mjs ../../_site
//
// 1. The demo's five commits, written by Pyodide's Python at the paths the page uses, packed as
//    _site/demo/<hash>.tar.xz (icelens.browser.pack_demo), so the page unpacks them instead.
// 2. The first frame drawn from those same files, as _site/first-frame.json: the page's preview
//    is then the live app's first screen exactly, ids and all.
// 3. The code of every module imported to get there, as _site/bytecode/<hash>.xz
//    (icelens.bytecode.pack), so the page does not compile them. Bytecode is particular to the
//    Python that wrote it, which is why this is Pyodide's own Python and not the build's.
//
// Each runs in a fresh runtime that has done only what the page will have done, so the bundle
// holds exactly what the page imports. wheels/latest.json names the files for the worker.
import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { bootSite } from "./boot.mjs";

const site = process.argv[2] && resolve(process.argv[2]);
if (!site) {
  console.error("usage: node prebuild.mjs path/to/_site   (built by scripts/build_site.py)");
  process.exit(2);
}
const save = (dir, ext, body) => {
  const name = `${dir}/${createHash("sha256").update(body).digest("hex").slice(0, 16)}${ext}`;
  mkdirSync(join(site, dir), { recursive: true });
  writeFileSync(join(site, name), body);
  console.log(`${name}: ${(body.length / 1e6).toFixed(2)} MB`);
  return name;
};

let { py } = await bootSite(site, { quiet: true });
py.runPython(`
import icelens.browser as browser
browser.prepare()
open("/tmp/demo.tar.xz", "wb").write(browser.pack_demo())
`);
const demo = save("demo", ".tar.xz", py.FS.readFile("/tmp/demo.tar.xz"));

({ py } = await bootSite(site, { quiet: true }));
py.FS.writeFile("/tmp/demo.tar.xz", readFileSync(join(site, demo)));
const frame = await py.runPythonAsync(`
import json
import icelens.browser as browser
assert browser.restore_demo("/tmp/demo.tar.xz") == "demo.orders", "the demo archive did not restore"
browser.prepare()
json.dumps(await browser.frames(120, 36, browser.HOME))
`);
writeFileSync(join(site, "first-frame.json"), frame);
py.runPython(`import icelens.bytecode; open("/tmp/bytecode.xz", "wb").write(icelens.bytecode.pack())`);
const bytecode = save("bytecode", ".xz", py.FS.readFile("/tmp/bytecode.xz"));

const latest = JSON.parse(readFileSync(join(site, "wheels", "latest.json"), "utf8"));
Object.assign(latest, { bytecode, demo });
writeFileSync(join(site, "wheels", "latest.json"), JSON.stringify(latest, null, 1) + "\n");
process.exit(0);
