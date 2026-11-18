# icelens --sandbox

> Design notes, kept for the reasoning. They were written before the sandbox was built and are
> marked where things shipped; for how it behaves now, the guide's sandbox sections are the
> reference. Line numbers that once appeared here have been removed, since they drift.

A writable scratch catalog with no setup, where every Iceberg verb is a command and every
command's effect lands in the panes that are already open. The browser becomes an instrument:
make a commit, watch the manifest chain grow a link, read the rail to find out what the link is.

**Verification key**

| mark | meaning |
|---|---|
| ✅ | run against a real SQLite catalog in a temp dir; the numbers below are real |
| ◐ | the API exists and was inspected, not built end to end |
| ○ | argued only |

Everything marked ✅ below came from one script: eleven operations against a local catalog,
start to finish, no server and no network.

---

## The four decisions that shape everything

### 1. The sandbox is v2 offline, v3 on a catalog. Both shipped. ✅

Every route to writing v3 was checked, and only one exists:

| route | result |
|---|---|
| PyIceberg `create_table(properties={"format-version": "3"})` | `NotImplementedError: Writing V3 is not yet supported` - `TableMetadataV3.model_dump_json` is a stub |
| PyIceberg `upgrade_table_version(3)` | `ValueError: Unsupported table format version: 3` |
| a newer PyIceberg | none: 0.12.0 is the latest on PyPI |
| DuckDB `ATTACH` a directory or `sqlite://` | refused - *"accepted options are: glue, s3_tables"* |
| DuckDB `COPY t TO '<dir>' (FORMAT ICEBERG)` | a whole real Iceberg table, no catalog needed - and **always v2**. It ignored `FORMAT_VERSION 3`, `VERSION 3` and an option that does not exist, silently, writing v2 each time |
| DuckDB `CREATE TABLE … WITH ("format-version" = 3)` on an attached REST catalog | **v3** |

So the offline sandbox teaches **v2** - snapshots, manifests, copy-on-write deletes, partition
and schema evolution - and `--sandbox --rest` puts the sandbox in a namespace of a catalog that
is already running, where DuckDB writes **v3**: deletion vectors and row lineage, the flagship
features of v3.

**Shipped.** The status bar says which you are in (`sandbox lesson (in sandbox_lesson, v3)`),
and the Writer pane opens on a different example in each. Never pretend: a v3 lesson in a v2
sandbox is a lie the tool would be teaching.

Two things the build found that this plan did not:

- **The version has to be checked, not asked for.** DuckDB ignored three invented `COPY`
  options without a word, so `_commit_attached` reads `format_version` back off the table and
  fails loudly if a catalog gave it v2.
- **The snapshot summary cannot count a merge-on-read delete.** Three deletes of ten rows each
  reported `added-position-deletes` of 10, then 20, then 30 - a deletion vector is rewritten
  whole and carries every position ever deleted from its file - while `total-records` sat at
  100 throughout. The ledger counts rows instead and derives deletions from what the count did.
  Counting files instead overstates deletions several times over; here that can be driven by hand.

### 1a. An attached sandbox writes in one namespace and nowhere else. ✅

The catalog on the other end is somebody's real one. So the sandbox gets `sandbox_<name>`, a
bare table name means that namespace, and a name outside it is refused with the name it should
have used. The SQL pane stays read-only there as well: the sandbox has to *attach* writable for
the Writer pane to commit, which would otherwise leave `DROP TABLE crm.accounts` one keystroke
away. `--allow-writes` is still the one way past that, as everywhere else.

### 2. DuckDB writes the data, PyIceberg commits it. ✅

DuckDB's iceberg extension attaches REST, Glue and S3 Tables and nothing else — checked against
extension `45163a28`: a directory, a `sqlite://` URI and `ENDPOINT_TYPE 'FILE'` are all refused,
the last one by name (*"accepted options are: glue, s3_tables"*). So a local sandbox cannot be
attached, and `iceberg_scan` is read-only by construction.

That does not mean DuckDB sits out. It means the two halves split along their strengths:

```
DuckDB          parses the statement, runs the query, writes the Parquet
PyIceberg       commits the Parquet as a snapshot
```

**The classifier already exists.** `only_reads` (`catalog.py`) runs
`duckdb.extract_statements(sql)` and checks each statement's type against `READS_ONLY`. The
sandbox needs the same call with a routing table instead of a refusal — verified that DuckDB's
own parser returns `INSERT`, `DELETE`, `UPDATE`, `SELECT` and `CREATE` for the five shapes that
matter, so icelens never hand-rolls a parser.

| statement | route | verified |
|---|---|---|
| `SELECT …` | as today, through `iceberg_scan` | ✅ shipped |
| `INSERT INTO t SELECT …` | DuckDB runs the SELECT and `COPY … TO` a Parquet; `table.add_files([…])` | ✅ `op=APPEND`, +30 records, 1 data file; a second one gives 2 snapshots / 40 rows / 2 files |
| `DELETE FROM t WHERE p` | `table.delete(p)` | ✅ `op=OVERWRITE`, copy-on-write |
| `UPDATE t SET …` | delete + append inside one `Transaction` | ◐ API exists, not built |
| `CREATE TABLE t AS SELECT …` | Parquet schema → `create_table`, then `add_files` | ✅ used to seed the table above |

The payoff is that the user's whole DuckDB vocabulary produces Iceberg rows — `read_csv_auto`,
`range`, joins, window functions, another Iceberg table, an HTTP Parquet file — and the data
files in the sandbox are genuinely DuckDB-written. "Play around with it" means the SQL pane, not
a form.

Known limit ◐: `add_files` expects Parquet already laid out to match the partition spec, so
inserting into a partitioned table needs one file per partition. Either write them that way from
DuckDB (`COPY … TO … (PARTITION_BY dy)`) or fall back to `table.append` for the partitioned case.

### 2b. The loopback REST shim ✅ *(shipped as `--v3`)*

A small Iceberg REST server over the sandbox's `SqlCatalog`, bound to `127.0.0.1`, turns the
sandbox into a catalog DuckDB can genuinely `ATTACH`. That unlocks three things at once:

1. Native DuckDB writes — `INSERT`, `UPDATE`, `DELETE`, `MERGE` handled by DuckDB itself, no
   routing table.
2. **v3**: DuckDB writes format-version 3 through a REST catalog, so deletion vectors and row
   lineage become reachable offline, which decision 1 says they otherwise are not.
3. `tests/test_attach.py` stops skipping. Attach mode — the mode real runs use — becomes testable
   in CI with no Lakekeeper and no network.

Cost: enough of the REST spec to serve config, namespaces, table load and commit. **3–4 days**,
and it is a real server with real correctness risk. Point 3 is the reason to build it anyway:
it pays for itself in the test suite regardless of the sandbox.

**Shipped**, as `icelens/rest.py` and `--sandbox NAME --v3`, and all three payoffs arrived: DuckDB
writes natively (`UPDATE` and `MERGE` included), v3 is reachable offline, and `tests/test_attach.py`
runs everywhere against it. What the build found:

- **The catalog writing the metadata is what makes it small.** In REST the client writes data,
  manifests and the manifest list; the server applies `add-snapshot` and `set-snapshot-ref` to a
  JSON document. No Iceberg library had to support v3 on this side - only the spec's one line
  for it, `next-row-id += added-rows`.
- **DuckDB stages its creates.** `CREATE TABLE` is a `stage-create` that stores nothing, then a
  multi-table commit with `assert-create` that builds the table from empty. Both had to work.
- **DuckDB makes `data/` for a local table and not `metadata/`**, so its first manifest write
  fails unless the catalog makes the folder - which this one does at create.
- **PyIceberg retries a commit refused as stale**, re-reading first. A 409 is therefore not a
  failure a person sees; losing a write would be, and the compare-and-swap is what prevents it.
- **One `MERGE` is two snapshots** in DuckDB: an overwrite for the matched rows and an append
  for the new ones. The v3 twin of the v2 lesson that one `overwrite()` is two snapshots.

### 3. Sandboxes are persistent and named. ✅ *(shipped)*

`icelens --sandbox` opens the default one; `--sandbox lesson2` opens another; `--sandbox-reset`
wipes it. A temp directory is wrong for a teaching tool — the history you built is the thing
worth coming back to.

Location: `~/.local/share/icelens/sandboxes/<name>/`, catalog in `catalog.db`, data beside it.

### 4. Writable if and only if sandbox. ✅ *(and this fixes a live bug)*

When this was written, `cli.py` read `read_only=headless and not args.allow_writes`, so the
**interactive app was read-write**, deliberately. In the app, against any catalog,
`INSERT INTO icelens.demo.orders …` in the SQL pane went through, and the README's "Read only.
icelens never writes to a catalog" was false. That is fixed.

The invariant this feature needs, and that the tool should have had already:

```
Lake.read_only == False   iff   the lake is a sandbox, or --allow-writes was passed explicitly
```

Write commands are bound only when `lake.is_sandbox`. They are absent, not disabled, everywhere
else — a key that does nothing teaches nothing.

---

## What a local catalog can actually do ✅

Eleven operations, one after another, against `demo.sales` in a SQLite catalog:

| operation | API | result |
|---|---|---|
| append | `table.append(arrow)` | `op=APPEND`, +50 records, 1 data file |
| append again | `table.append(arrow)` | `op=APPEND`, +10 records, 2 data files |
| delete rows | `table.delete("id < 10")` | **`op=OVERWRITE`, +41 / −50** |
| overwrite | `table.overwrite(arrow)` | **two snapshots**: `DELETE` −51, then `APPEND` +10 |
| add column | `update_schema().add_column` | schema 3, new field id 4 |
| rename column | `update_schema().rename_column` | `amount`→`value`, **field id 3 unchanged** |
| evolve partitioning | `update_spec().add_field` | 2 partition specs |
| partitioned append | `table.append(arrow)` | `op=APPEND`, written under the new spec |
| tag | `manage_snapshots().create_tag` | ref `genesis`, type TAG |
| branch | `manage_snapshots().create_branch` | ref `wip`, type BRANCH |
| time travel | `scan(snapshot_id=…)` | 15 rows now, 50 at `genesis` |

Final state: format-version 2, 3 schemas, 2 specs, 3 refs, 6 snapshots.

**Not available in PyIceberg 0.12:** `expire_snapshots` and `rewrite_manifests` are on neither
`Table` nor `Transaction`. Expiry and true compaction are therefore out of reach without forging
metadata, which a teaching tool must not do. See Phase 3.

---

## The three surprises worth building lessons around ✅

These came out of the verification run, not from theory. Each is a thing a competent engineer
gets wrong, and each is now demonstrable in under a second.

### Deleting 9 rows rewrote 50

`delete("id < 10")` produced an **OVERWRITE** snapshot, +41 / −50 — not a delete file. PyIceberg's
v2 delete is copy-on-write: it rewrote the whole data file without the nine rows. Read the
snapshot summary and it looks like fifty rows died. Read the Files pane and one file was retired
and one was born.

This is *the* lesson. It is the same trap a commit diff falls into at v3 scale, and
here it costs nothing to stage.

### One `overwrite()` is two snapshots

`table.overwrite(rows)` committed a `DELETE` (−51) and then an `APPEND` (+10). One call, two
entries in the Snapshots pane, and any code that assumes one API call means one snapshot is
already wrong.

### A rename is not a drop plus an add

`amount` → `value` kept **field id 3**. The Schema pane can prove it, and that proof is the entire
justification for keying a schema timeline on field ids.

---

## Phases

### Phase 1 — the sandbox exists, and the invariant holds. ✅ **shipped**

1. ✅ `read_only=not args.allow_writes`, so the app is read-only too, and the README sentence
   that claimed it already was is now true rather than aspirational.
2. ✅ `Lake.is_sandbox`, `Target.sandbox` (marked by the `icelens.sandbox` property, not by the
   catalog type - plenty of people run SQLite for real), and `sandbox_target(name)` /
   `reset_sandbox(name)` over `~/.local/share/icelens/sandboxes/<name>/`.
3. ✅ `route(sql)` beside `only_reads`, sharing its `duckdb.extract_statements` call: SELECT
   reads, INSERT/CREATE go DuckDB-Parquet-then-`add_files`, DELETE goes to `table.delete`.
   Anything unclassified is refused with the statement quoted back. UPDATE stays deferred.
4. ✅ A **Writer pane**, not the SQL pane: `e` opens it, ctrl+r commits, and under the editor
   is a ledger - one row per commit, the statement beside the operation, snapshot id and row
   counts it produced. The tab does not exist outside a sandbox.
5. ✅ A sandbox banner in the status bar, and `--sandbox NAME --write "<sql>"` printing the
   same ledger headlessly, `--json` included.
6. ✅ `tests/test_sandbox.py`: the invariant, the router (including the shapes it must refuse),
   one test per command asserting the snapshot it produced, the on-disk shape, and the pane.

Two things the build found that the plan did not:

- **`add_files` adopts the Parquet exactly as written**, so `INSERT INTO t SELECT …` has to
  project the query onto the table's columns by position *and* cast to the table's types. Left
  alone, DuckDB names a column `(i * 1.5)` and types `9.99` as `DECIMAL(3,2)`, and the commit
  is refused. `Lake._shape` asks DuckDB to describe the table's own schema rather than carrying
  an Iceberg-to-DuckDB type map.
- **`extract_statements` keeps the comments around a statement attached to it**, and the Writer
  pane opens on a commented example - so the first statement anybody runs would not match any
  shape, and the trailing comments would land inside the query.

### Phase 2 — the tool says what just happened. **2 days**

The write is not the feature; the reaction is.

1. After each commit, a **what-changed line**: the operation, the snapshot id, and which panes
   moved — "OVERWRITE · 1 file retired, 1 born, 41 rows kept. Files and Commit changed."
2. A row-level commit diff lands here as the body of that line. The sandbox is its
   best possible demo, and at v2 the algorithm is the easy half.
3. A **before/after toggle**: hold a key to see the pane as it was one snapshot ago. Every pane
   is already `View -> pa.Table` and Views are already snapshot-pinned, so this is a second
   render of data the app has in hand, not new plumbing.
4. Tests: each command's what-changed line asserted against the real snapshot it produced.

### Phase 3 — lessons. **2 days**

`icelens --sandbox --lesson <name>` seeds a table and drops you into the pane where the thing is
visible, with the rail already on the right cell.

| lesson | seeds | shows |
|---|---|---|
| `first-commit` | empty table | metadata.json, one snapshot, one manifest, the pointer chain |
| `copy-on-write` | 50 rows, then delete 9 | the OVERWRITE surprise above ✅ |
| `two-snapshots` | one `overwrite()` | one call, two snapshots ✅ |
| `rename` | add then rename a column | field id survives; drop+add does not ✅ |
| `partitions` | evolve the spec, append | old files keep the old spec; two specs coexist ✅ |
| `time-travel` | tag an early snapshot | `genesis` still reads 50 rows ✅ |
| `small-files` | 20 appends of 5 rows | 20 files; then rewrite via `overwrite` and watch it collapse ◐ |
| `expiry` | — | **blocked**: PyIceberg 0.12 has no `expire_snapshots` ○ |

Each lesson is a fixture the test suite can run, so the lessons are also regression tests. That
is the same trick `test_driving.py` already plays.

---

## Risks, in the order they will bite

1. **The safety story weakens by increments.** A `--sandbox` flag advertises that this tool
   writes. The next request is "let me do that against dev", and the one after that is a
   production incident. Mitigation is the Phase-1 invariant, in code and tested, before any
   command exists.
2. **v3 is where the interesting material lives, and the offline sandbox cannot reach it.** Half
   the reason this tool is worth using — deletion vectors, row lineage — needs a REST catalog.
   Accept the split, name it in the status bar, never simulate it.
3. **Lessons rot.** A seeded scenario that stops matching what PyIceberg does is worse than no
   lesson. Wiring each lesson into the suite as a fixture is not optional.
4. **Scope.** "Play around with tables" has no natural edge. The edge proposed here: the sandbox
   supports exactly the operations verified in the table above, and no others.

## Open questions

- Should `--sandbox` seed a table by default, or open empty? Empty is honest; seeded is the
  better first thirty seconds.
- Does a sandbox belong in the `ctrl+o` catalog switcher alongside real profiles, or is mixing
  a writable catalog into that list precisely the confusion to avoid?
- Is `--lesson` a flag, or a pane?
