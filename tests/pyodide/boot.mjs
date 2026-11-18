// Loads the built site's Python under Pyodide in Node, exactly as the page's worker does:
// site/loader.mjs, site/pyodide.json and the site's wheels/latest.json. Used by run.mjs (the
// checks) and prebuild.mjs (the compiled-module bundle and demo files the build adds to the site).
import { createHash } from "node:crypto";
import { mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { createRequire } from "node:module";
import { basename, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { loadPyodide, version } from "pyodide";
import { loadIcelens } from "../../site/loader.mjs";

const here = dirname(fileURLToPath(import.meta.url));
export const read = (path) => JSON.parse(readFileSync(path, "utf8"));
const sha256 = (body) => createHash("sha256").update(body).digest("hex");

// Pyodide under Node fetches its packages from jsDelivr without checking the lockfile's sha256 (a
// browser checks it), and the build compiles what it loaded into the bytecode the site serves in
// place of those packages' sources. So every file in the package cache is held to the lockfile,
// which npm checked: a stale or unknown one is dropped before loading, so it is fetched again,
// and anything that does not match after loading stops the build.
export function checkCache(dir, lock, { drop }) {
  const expected = new Map(Object.values(lock.packages).map((p) => [p.file_name, p.sha256]));
  let names = [];
  try {
    names = readdirSync(dir);
  } catch {
    return;
  }
  for (const name of names) {
    const want = expected.get(name);
    const got = want && sha256(readFileSync(join(dir, name)));
    if (want && got === want) continue;
    if (!drop) throw new Error(`${name} in Pyodide's package cache: sha256 ${got || "(not in the lockfile)"}, expected ${want || "nothing"}`);
    rmSync(join(dir, name), { recursive: true, force: true });
  }
}

export async function bootSite(site, { quiet = false } = {}) {
  const manifest = read(join(site, "pyodide.json"));
  if (manifest.pyodide !== version) {
    throw new Error(`site/pyodide.json pins Pyodide ${manifest.pyodide}, tests/pyodide has ${version}: make them agree`);
  }
  const lock = read(createRequire(import.meta.url).resolve("pyodide/pyodide-lock.json"));
  const latest = read(join(site, "wheels", "latest.json"));
  // Pyodide under Node resolves a lockfile's file names against its package cache, so a URL there
  // never loads; a browser fetches it. Here the wheels the site lists by URL are fetched first,
  // checked against the same hashes the browser checks, and listed by local path instead.
  const cache = mkdtempSync(join(tmpdir(), "icelens-wheels-"));
  for (const entry of latest.packages) {
    if (!/^https?:\/\//.test(entry.file_name)) continue;
    const body = Buffer.from(await (await fetch(entry.file_name)).arrayBuffer());
    const sha = sha256(body);
    if (sha !== entry.sha256) throw new Error(`${entry.file_name}: sha256 ${sha}, expected ${entry.sha256}`);
    const path = join(cache, basename(new URL(entry.file_name).pathname));
    writeFileSync(path, body);
    entry.file_name = path;
  }
  const indexURL = `https://cdn.jsdelivr.net/pyodide/v${manifest.pyodide}/full/`;
  // Outside node_modules, which `npm ci` deletes: inside it, every build fetched 28 MB again.
  const packageCacheDir = join(here, ".pyodide-cache");
  // Checked against Pyodide's own lockfile, before loader.mjs edits a copy of it.
  const pristine = structuredClone(lock);
  checkCache(packageCacheDir, pristine, { drop: true });
  const loading = [];
  const out = quiet ? () => {} : (s) => console.log(s);
  const py = await loadIcelens({
    loadPyodide,
    lock,
    latest,
    manifest,
    wheelsBase: join(site, "wheels") + "/",
    // Pyodide under Node caches what it downloads; left to itself, with a URL for a base, it
    // writes an `https:` directory wherever node was started.
    options: { packageBaseUrl: indexURL, packageCacheDir, stdout: out, stderr: (s) => console.error(s) },
    onLoading: (names) => loading.push(...names),
  });
  checkCache(packageCacheDir, pristine, { drop: false });
  rmSync(cache, { recursive: true, force: true }); // Pyodide has installed the wheels by now
  return { py, manifest, latest, loading };
}
