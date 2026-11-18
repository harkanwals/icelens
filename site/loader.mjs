// How icelens's Python gets loaded, shared by the page's worker (worker.js) and CI's Node check
// (tests/pyodide/run.mjs), so what CI checks is what a visitor runs.
//
// One loadPackage call fetches everything, in parallel: Pyodide's own packages, Textual and the
// three wheels it needs (from PyPI, at uv.lock's versions), and icelens itself. That replaces
// micropip, which asked PyPI about each package in turn before downloading any. Pyodide's
// lockfile is edited first, to leave out what nothing here imports: PyArrow lists pandas, and
// pandas brings pytz - 4.6 MB a visitor would download and never use.

// Remove `drop` from every package's dependencies and add `extra`, whose relative file names are
// resolved against `base` (the site's wheels/ directory).
export function editLock(lock, { drop = [], extra = [], base = "" }) {
  const edited = { ...lock, packages: { ...lock.packages } };
  for (const [name, entry] of Object.entries(edited.packages)) {
    if (entry.depends.some((d) => drop.includes(d))) {
      edited.packages[name] = { ...entry, depends: entry.depends.filter((d) => !drop.includes(d)) };
    }
  }
  for (const entry of extra) {
    const absolute = /^[a-z]+:\/\//.test(entry.file_name) || entry.file_name.startsWith("/");
    // The site's own entries drop the same packages: one of them depending on a dropped package
    // would otherwise quietly bring it back.
    const depends = (entry.depends || []).filter((d) => !drop.includes(d));
    edited.packages[entry.name] = { ...entry, depends, file_name: absolute ? entry.file_name : base + entry.file_name };
  }
  return edited;
}

// Start Pyodide on the edited lockfile and load `manifest.packages`. `onLoading` hears the names
// of the packages being fetched, once, before the download starts.
export async function loadIcelens({ loadPyodide, lock, latest, manifest, wheelsBase, options = {}, onLoading = () => {} }) {
  const edited = editLock(lock, { drop: manifest.drop, extra: latest.packages, base: wheelsBase });
  const pyodide = await loadPyodide({ ...options, lockFileContents: JSON.stringify(edited) });
  await pyodide.loadPackage(manifest.packages, {
    messageCallback: (line) => {
      const m = /^Loading (.+)$/.exec(line);
      if (m) onLoading(m[1].split(/,\s*/));
    },
    errorCallback: (line) => console.warn(line),
  });
  return pyodide;
}

