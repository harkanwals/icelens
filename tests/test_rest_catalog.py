"""icelens's own Iceberg REST catalog, held to what clients expect of one.

`icelens.rest` writes table metadata itself, so every rule a catalog enforces is a rule this
file has to enforce: requirements checked before anything is written, pointers moved with a
compare-and-swap, v3 row ids never reused, a purge that cannot reach outside the sandbox, and
nothing answered without the token. The clients are the real ones - PyIceberg here, DuckDB in
the tests marked for its extensions - because a catalog that only agrees with its own tests
has not been tested.
"""

from __future__ import annotations

import http.client
import json
import logging
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pyarrow as pa
import pytest

from icelens import rest


@pytest.fixture()
def server(tmp_path: Path) -> rest.Running:
    running = rest.serve(tmp_path / "sandbox", log=True)
    yield running
    running.stop()


@pytest.fixture()
def catalog(server: rest.Running):
    from pyiceberg.catalog.rest import RestCatalog

    return RestCatalog("t", uri=server.uri, token=server.token, **{"header.X-Iceberg-Access-Delegation": ""})


def _call(server: rest.Running, method: str, path: str, body: object = None, token: str | None = "ok") -> tuple[int, dict]:
    request = urllib.request.Request(
        f"{server.uri}{path}",
        method=method,
        data=None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode()),
        headers={"Content-Type": "application/json"},
    )
    if token is not None:
        request.add_header("Authorization", f"Bearer {server.token if token == 'ok' else token}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, json.loads(raw) if raw else {}


def _rows(n: int, start: int = 0) -> pa.Table:
    return pa.table({
        "id": pa.array(range(start, start + n), pa.int64()),
        "name": pa.array([f"r{i}" for i in range(start, start + n)], pa.string()),
    })


# -- who may ask -----------------------------------------------------------------------------


def test_nothing_is_answered_without_the_token(server: rest.Running) -> None:
    for token in (None, "wrong", server.token[:-1]):
        status, body = _call(server, "GET", "/v1/config", token=token)
        assert status == 401 and body["error"]["type"] == "NotAuthorizedException"
    assert _call(server, "GET", "/v1/config")[0] == 200


def test_it_listens_on_loopback_only(server: rest.Running) -> None:
    assert server.uri.startswith("http://127.0.0.1:")
    assert server._server.server_address[0] == "127.0.0.1"  # bound there, not merely advertised
    port = int(server.uri.rsplit(":", 1)[1])
    outward = socket.gethostbyname(socket.gethostname())
    if outward.startswith("127."):
        pytest.skip("this machine has no address but loopback to try")
    with pytest.raises(OSError):
        socket.create_connection((outward, port), timeout=1).close()


def test_each_sandbox_gets_one_server_and_a_fresh_token_per_start(tmp_path: Path) -> None:
    first = rest.serve(tmp_path / "a")
    assert rest.serve(tmp_path / "a") is first
    token = first.token
    first.stop()
    again = rest.serve(tmp_path / "a")
    try:
        assert again.token != token
    finally:
        again.stop()


def test_a_stopped_catalog_answers_nothing_even_on_a_connection_kept_open(tmp_path: Path) -> None:
    """`shutdown` stops a server accepting connections, not answering on ones it holds: after
    `stop()`, which a sandbox reset calls before deleting the sandbox, an open connection still
    committed with the old token."""
    running = rest.serve(tmp_path / "sandbox")
    auth = {"Authorization": f"Bearer {running.token}", "Content-Type": "application/json"}
    connection = _connection(running)
    connection.request("GET", "/v1/config", headers=auth)
    assert connection.getresponse().read()
    running.stop()
    connection.request("POST", "/v1/sandbox/namespaces", body=json.dumps({"namespace": ["after"]}), headers=auth)
    response = connection.getresponse()
    assert (response.status, response.getheader("Connection")) == (503, "close")
    connection.close()
    assert rest.Store(running.home / "catalog.db", running.home / "warehouse").namespaces() == set()


def test_config_names_the_prefix_and_every_endpoint_it_serves(server: rest.Running) -> None:
    for query in ("", f"?warehouse={rest.PREFIX}"):
        status, body = _call(server, "GET", f"/v1/config{query}")
        assert status == 200
        assert body["overrides"]["prefix"] == rest.PREFIX
        assert "POST /v1/{prefix}/transactions/commit" in body["endpoints"]


def test_config_for_a_warehouse_it_does_not_serve_is_refused(server: rest.Running) -> None:
    """It answered for any name, so a client configured for another warehouse wrote here."""
    status, body = _call(server, "GET", "/v1/config?warehouse=production")
    assert status == 400 and "'sandbox'" in body["error"]["message"]


def _connection(server: rest.Running) -> http.client.HTTPConnection:
    """One keep-alive connection, as PyIceberg's pool and DuckDB's client hold one."""
    host, port = server.uri.removeprefix("http://").split(":")
    return http.client.HTTPConnection(host, int(port), timeout=5)


def test_a_refused_request_does_not_spoil_the_next_one_on_its_connection(server: rest.Running, catalog) -> None:
    """A refusal can come before the body is read, and the body left behind was read as the next
    request on the connection: through PyIceberg, the call after a refused `create_table` got a
    501 page of HTML instead of its answer. Now every error closes its connection."""
    from pyiceberg.exceptions import BadRequestError

    catalog.create_namespace("demo")
    with pytest.raises(BadRequestError):
        catalog.create_table(("x.y", "t"), schema=_rows(0).schema)
    assert catalog.list_namespaces() == [("demo",)]

    auth = {"Authorization": f"Bearer {server.token}", "Content-Type": "application/json"}
    connection = _connection(server)
    for method, path, headers in (
        ("POST", "/v1/sandbox/namespaces", {"Authorization": "Bearer wrong"}),
        ("POST", "/v1/sandbox/namespaces/a.b/tables", auth),
        ("POST", "/v1/sandbox/namespaces/demo/tables/t/plan", auth),
        ("POST", "/v1/sandbox/namespaces/demo/tables/t/metrics", {**auth, "Transfer-Encoding": "chunked"}),
    ):
        connection.request(method, path, body=json.dumps({"namespace": ["x"]}), headers=headers)
        response = connection.getresponse()
        response.read()
        assert response.status >= 400 and response.getheader("Connection") == "close", path
        connection.request("GET", "/v1/config", headers=auth)  # http.client reconnects, as PyIceberg's pool does
        response = connection.getresponse()
        assert (response.status, json.loads(response.read())["overrides"]) == (200, {"prefix": rest.PREFIX}), path
    connection.close()


@pytest.mark.parametrize("length", [-1, rest.MAX_BODY + 1], ids=["negative", "over-max-body"])
def test_a_content_length_out_of_bounds_is_refused_before_the_body_is_read(server: rest.Running, length: int) -> None:
    """Refused on the header alone, with no body sent: reading it would wait for 16 MiB, or with
    `rfile.read(-1)`, until the client hung up, past MAX_BODY and holding a thread all along."""
    host, port = server.uri.removeprefix("http://").split(":")
    with socket.create_connection((host, int(port))) as raw:
        raw.settimeout(5)
        raw.sendall(f"POST /v1/sandbox/namespaces HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {server.token}\r\n"
                    f"Content-Length: {length}\r\n\r\n".encode())
        assert raw.recv(100).startswith(b"HTTP/1.1 400 ")  # with this side still open
    assert _call(server, "GET", "/v1/sandbox/namespaces")[1]["namespaces"] == []


def test_an_idle_connection_is_closed_in_the_end(server: rest.Running, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each connection holds a thread, and the stdlib's default is to wait on one forever."""
    assert rest._Handler.timeout is not None and 0 < rest._Handler.timeout <= 300
    monkeypatch.setattr(rest._Handler, "timeout", 0.5)
    host, port = server.uri.removeprefix("http://").split(":")
    with socket.create_connection((host, int(port))) as idle:
        idle.settimeout(5)
        assert idle.recv(1) == b""  # closed by the server, well before this side's 5 seconds


def test_an_unknown_endpoint_or_a_bad_body_is_an_error_not_a_crash(server: rest.Running) -> None:
    assert _call(server, "GET", "/v1/sandbox/views")[0] == 404
    assert _call(server, "POST", "/v1/sandbox/namespaces", b"{not json")[0] == 400
    assert _call(server, "POST", "/v1/sandbox/namespaces", [1, 2])[0] == 400
    assert _call(server, "POST", "/v1/sandbox/namespaces", {"namespace": ["a.b"]})[0] == 400
    assert _call(server, "GET", "/v1/config")[0] == 200  # and it is still up


# -- namespaces ------------------------------------------------------------------------------


def test_namespaces_nest_and_list_one_level_at_a_time(catalog) -> None:
    catalog.create_namespace("core", {"owner": "me"})
    catalog.create_namespace(("core", "nested"))
    catalog.create_namespace(("core", "nested", "deeper"))
    assert catalog.list_namespaces() == [("core",)]
    assert catalog.list_namespaces("core") == [("core", "nested")]
    assert catalog.list_namespaces(("core", "nested")) == [("core", "nested", "deeper")]
    assert catalog.load_namespace_properties("core") == {"owner": "me"}


def test_namespace_properties_change_and_say_what_changed(catalog) -> None:
    catalog.create_namespace("core", {"a": "1", "b": "2"})
    summary = catalog.update_namespace_properties("core", removals={"a", "zz"}, updates={"c": "3"})
    assert (summary.removed, summary.updated, summary.missing) == (["a"], ["c"], ["zz"])
    assert catalog.load_namespace_properties("core") == {"b": "2", "c": "3"}


def test_a_nested_namespace_needs_its_parent(catalog, server: rest.Running) -> None:
    """`x.y` with no `x` made `x` appear in listings, and answer 404 when it was asked for."""
    status, body = _call(server, "POST", "/v1/sandbox/namespaces", {"namespace": ["x", "y"]})
    assert (status, body["error"]["type"]) == (404, "NoSuchNamespaceException")
    assert catalog.list_namespaces() == []
    catalog.create_namespace("x")
    catalog.create_namespace(("x", "y"))
    assert catalog.list_namespaces("x") == [("x", "y")]


def test_the_namespace_marker_is_not_a_property_to_remove_or_set(catalog, server: rest.Running) -> None:
    """PyIceberg's SQL schema marks a namespace by a row `exists=true`: removing it as a property
    deleted the namespace."""
    catalog.create_namespace("demo")
    summary = catalog.update_namespace_properties("demo", removals={"exists"})
    assert (summary.removed, summary.missing) == ([], ["exists"])
    assert catalog.load_namespace_properties("demo") == {}
    assert _call(server, "POST", "/v1/sandbox/namespaces/demo/properties", {"updates": {"exists": "no"}})[0] == 400
    assert _call(server, "POST", "/v1/sandbox/namespaces", {"namespace": ["other"], "properties": {"exists": "no"}})[0] == 400


def test_a_property_both_removed_and_updated_is_unprocessable(server: rest.Running) -> None:
    _call(server, "POST", "/v1/sandbox/namespaces", {"namespace": ["demo"], "properties": {"k": "1"}})
    status, body = _call(server, "POST", "/v1/sandbox/namespaces/demo/properties", {"removals": ["k"], "updates": {"k": "2"}})
    assert (status, body["error"]["type"]) == (422, "UnprocessableEntityException")
    assert _call(server, "GET", "/v1/sandbox/namespaces/demo")[1]["properties"] == {"k": "1"}


def test_creates_of_one_namespace_racing_get_one_success_and_conflicts(server: rest.Running) -> None:
    """Two that both found the name free used to meet in SQLite, and the loser got a 500."""
    import threading

    statuses: list[int] = []
    body = {"namespace": ["race"], "properties": {f"p{i}": "v" for i in range(200)}}
    racers = [threading.Thread(target=lambda: statuses.append(_call(server, "POST", "/v1/sandbox/namespaces", body)[0]))
              for _ in range(8)]
    for racer in racers:
        racer.start()
    for racer in racers:
        racer.join()
    assert sorted(statuses) == [200] + [409] * 7, statuses


def test_connections_past_the_cap_are_closed_and_the_rest_still_served(server: rest.Running) -> None:
    """The token is checked per request, so a process without it can still open connections;
    each holds a thread and a descriptor, and with no cap enough of them exhaust the process."""
    import threading

    assert 16 <= rest._Server.max_connections <= 256
    server._server._slots = threading.BoundedSemaphore(2)
    host, port = server.uri.removeprefix("http://").split(":")
    held = [socket.create_connection((host, int(port))) for _ in range(2)]
    try:
        with socket.create_connection((host, int(port))) as extra:
            extra.settimeout(5)
            assert extra.recv(1) == b""  # closed at once, not left waiting for a slot
    finally:
        for connection in held:
            connection.close()
    for _ in range(50):  # the held connections' threads notice the close and give their slots back
        try:
            if _call(server, "GET", "/v1/config")[0] == 200:
                break
        except (ConnectionError, urllib.error.URLError):
            pass
    else:
        pytest.fail("the server never served again after the held connections closed")


def test_a_burst_of_connections_fits_the_listen_backlog() -> None:
    # socketserver's default backlog of 5 overflowed under load and macOS reset the connection
    # that did not fit: the race test above failed that way, 3 runs in 40 with the CPU busy.
    assert rest._Server.request_queue_size >= 64


def test_a_namespace_with_anything_in_it_will_not_drop(catalog) -> None:
    from pyiceberg.exceptions import NamespaceAlreadyExistsError, NamespaceNotEmptyError, NoSuchNamespaceError

    catalog.create_namespace("core")
    with pytest.raises(NamespaceAlreadyExistsError):
        catalog.create_namespace("core")
    catalog.create_table("core.t", schema=_rows(0).schema)
    with pytest.raises(NamespaceNotEmptyError):
        catalog.drop_namespace("core")
    catalog.drop_table("core.t")
    catalog.drop_namespace("core")
    with pytest.raises(NoSuchNamespaceError):
        catalog.load_namespace_properties("core")


# -- tables, through PyIceberg as the client ----------------------------------------------


def test_a_v2_table_lives_its_whole_life_through_the_catalog(catalog) -> None:
    """Everything the offline demo does - appends, a delete, schema and partition evolution,
    a tag - as PyIceberg asks a REST catalog to do it."""
    from pyiceberg.types import StringType

    catalog.create_namespace("demo")
    table = catalog.create_table("demo.t", schema=_rows(0).schema)
    table.append(_rows(50))
    first = table.current_snapshot().snapshot_id
    table.append(_rows(10, 50))
    table.delete("id < 5")
    with table.update_schema() as update:
        update.add_column("channel", StringType())
        update.rename_column("name", "label")
    with table.update_spec() as spec:
        spec.add_identity("channel")
    table.manage_snapshots().create_tag(first, "genesis").commit()

    table = catalog.load_table("demo.t")
    metadata = table.metadata
    assert (metadata.format_version, len(metadata.snapshots), len(metadata.schemas), len(metadata.partition_specs)) == (2, 3, 2, 2)
    assert sorted(metadata.refs) == ["genesis", "main"]
    assert table.scan().to_arrow().num_rows == 55
    assert table.scan(snapshot_id=first).to_arrow().num_rows == 50
    assert {f.name: f.field_id for f in table.schema().fields}["label"] == 2  # a rename keeps its id


def test_every_commit_is_a_new_metadata_file_and_the_old_one_is_remembered(catalog, server: rest.Running) -> None:
    catalog.create_namespace("demo")
    table = catalog.create_table("demo.t", schema=_rows(0).schema)
    table.append(_rows(3))
    table.append(_rows(3, 3))
    table = catalog.load_table("demo.t")
    folder = Path(table.metadata_location.removeprefix("file://")).parent
    names = sorted(p.name for p in folder.glob("*.metadata.json"))
    assert [n.split("-", 1)[0] for n in names] == ["00000", "00001", "00002"]
    assert [entry.metadata_file.rsplit("/", 1)[-1] for entry in table.metadata.metadata_log] == names[:2]
    assert not list(folder.glob(".*.tmp"))


def test_a_metadata_file_is_on_disk_before_it_takes_its_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Renamed into place so no reader sees half a file, and synced first: a crash after the
    rename but before the bytes are written would leave the table pointing at an empty file."""
    import os

    events: list[str] = []
    fsync, replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (events.append("fsync"), fsync(fd))[1])
    monkeypatch.setattr(os, "replace", lambda source, target: (events.append("replace"), replace(source, target))[1])
    metadata = {**_metadata(2), "location": rest._file_uri(tmp_path / "t")}
    written = rest.write_metadata(metadata, None)
    assert events == ["fsync", "replace"]
    assert json.loads(rest._local(written).read_text())["location"] == metadata["location"]


def test_a_stale_commit_is_refused_and_loses_nothing(catalog, server: rest.Running) -> None:
    """Two writers, one table: the one committing against what it read before the other wrote
    gets a 409 rather than a win - and PyIceberg, given that 409, re-reads and tries again, so
    both appends land, one after the other, and neither is lost."""
    catalog.create_namespace("demo")
    catalog.create_table("demo.t", schema=_rows(0).schema)
    first, second = catalog.load_table("demo.t"), catalog.load_table("demo.t")
    first.append(_rows(5))

    stale = {"requirements": [{"type": "assert-ref-snapshot-id", "ref": "main", "snapshot-id": None}],
             "updates": [{"action": "set-properties", "updates": {"stale": "yes"}}]}
    status, body = _call(server, "POST", "/v1/sandbox/namespaces/demo/tables/t", stale)
    assert (status, body["error"]["type"]) == (409, "CommitFailedException")

    second.append(_rows(3, 100))  # still believes `main` points nowhere; is refused, re-reads, retries
    table = catalog.load_table("demo.t")
    assert table.scan().to_arrow().num_rows == 8
    older, newer = sorted(table.metadata.snapshots, key=lambda s: s.sequence_number)
    assert newer.parent_snapshot_id == older.snapshot_id
    assert "stale" not in table.metadata.properties


def test_a_multi_table_commit_moves_every_table_or_none(catalog, server: rest.Running) -> None:
    catalog.create_namespace("demo")
    a = catalog.create_table("demo.a", schema=_rows(0).schema)
    b = catalog.create_table("demo.b", schema=_rows(0).schema)
    change = lambda table, uuid: {  # noqa: E731
        "identifier": {"namespace": ["demo"], "name": table},
        "requirements": [{"type": "assert-table-uuid", "uuid": uuid}],
        "updates": [{"action": "set-properties", "updates": {"touched": "yes"}}],
    }
    status, _ = _call(server, "POST", "/v1/sandbox/transactions/commit", {
        "table-changes": [change("a", str(a.metadata.table_uuid)), change("b", "00000000-0000-0000-0000-000000000000")]
    })
    assert status == 409
    assert "touched" not in catalog.load_table("demo.a").metadata.properties
    status, _ = _call(server, "POST", "/v1/sandbox/transactions/commit", {
        "table-changes": [change("a", str(a.metadata.table_uuid)), change("b", str(b.metadata.table_uuid))]
    })
    assert status == 204
    assert all(catalog.load_table(f"demo.{t}").metadata.properties["touched"] == "yes" for t in "ab")


def test_a_pointer_moves_only_from_where_its_mover_last_saw_it(tmp_path: Path) -> None:
    store = rest.Store(tmp_path / "catalog.db", tmp_path / "warehouse")
    store.swap([("demo", "t", None, "file:///v1")])
    with pytest.raises(rest.RestError) as caught:
        store.swap([("demo", "t", "file:///stale", "file:///v2")])
    assert (caught.value.status, caught.value.kind) == (409, "CommitFailedException")
    assert store.location_of("demo", "t") == "file:///v1"


def test_pointers_move_together_or_not_at_all(tmp_path: Path) -> None:
    store = rest.Store(tmp_path / "catalog.db", tmp_path / "warehouse")
    store.swap([("demo", "a", None, "file:///a1"), ("demo", "b", None, "file:///b1")])
    with pytest.raises(rest.RestError):
        store.swap([("demo", "a", "file:///a1", "file:///a2"), ("demo", "b", "file:///stale", "file:///b2")])
    assert (store.location_of("demo", "a"), store.location_of("demo", "b")) == ("file:///a1", "file:///b1")


def test_two_catalogs_committing_from_one_base_lose_nothing(server: rest.Running, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two icelens processes on one sandbox have a catalog each, and no lock in common: both read
    the same metadata and write their next file, and the pointer's compare-and-swap is all that
    decides. One commits; the other gets the 409 that makes its client re-read and retry."""
    import threading

    _call(server, "POST", "/v1/sandbox/namespaces", {"namespace": ["demo"]})
    _call(server, "POST", "/v1/sandbox/namespaces/demo/tables", {"name": "t", "schema": _SCHEMA})
    both_read = threading.Barrier(2)
    write = rest.write_metadata
    monkeypatch.setattr(rest, "write_metadata", lambda metadata, previous: (both_read.wait(5), write(metadata, previous))[1])
    outcomes: dict[str, int] = {}

    def commit(key: str) -> None:
        change = {"requirements": [], "updates": [{"action": "set-properties", "updates": {key: "yes"}}]}
        try:
            rest.Catalog(server.home).commit([("demo", "t", change)])
            outcomes[key] = 200
        except rest.RestError as exc:
            outcomes[key] = exc.status

    writers = [threading.Thread(target=commit, args=(key,)) for key in ("first", "second")]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join()
    assert sorted(outcomes.values()) == [200, 409]
    [winner] = [key for key, status in outcomes.items() if status == 200]
    assert set(rest.Catalog(server.home).load("demo", "t")[1]["properties"]) == {winner}


def test_a_transaction_whose_last_table_moved_meanwhile_moves_none(server: rest.Running, monkeypatch: pytest.MonkeyPatch) -> None:
    """Another writer moves `b` after this commit read it: `a`, already written, must not move alone."""
    _call(server, "POST", "/v1/sandbox/namespaces", {"namespace": ["demo"]})
    for name in "ab":
        _call(server, "POST", "/v1/sandbox/namespaces/demo/tables", {"name": name, "schema": _SCHEMA})
    store = rest.Store(server.home / "catalog.db", server.home / "warehouse")
    before = {name: store.location_of("demo", name) for name in "ab"}
    write = rest.write_metadata

    def raced(metadata: dict, previous: str | None) -> str:
        path = write(metadata, previous)
        if previous == before["b"]:
            store.swap([("demo", "b", previous, path)])  # the other writer, getting there first
        return path

    monkeypatch.setattr(rest, "write_metadata", raced)
    change = lambda name: {"identifier": {"namespace": ["demo"], "name": name}, "requirements": [],  # noqa: E731
                           "updates": [{"action": "set-properties", "updates": {"touched": "yes"}}]}
    status, _ = _call(server, "POST", "/v1/sandbox/transactions/commit", {"table-changes": [change("a"), change("b")]})
    assert status == 409
    assert store.location_of("demo", "a") == before["a"]


def test_a_purge_of_a_table_stored_outside_the_warehouse_deletes_nothing(server: rest.Running, tmp_path: Path) -> None:
    """Pointers this catalog did not write - an older one, or a hand-edited `catalog.db` - may
    lead anywhere, by a metadata file's `location` or, the file gone, by the pointer itself."""
    _call(server, "POST", "/v1/sandbox/namespaces", {"namespace": ["demo"]})
    _, made = _call(server, "POST", "/v1/sandbox/namespaces/demo/tables", {"name": "t", "schema": _SCHEMA})
    store = rest.Store(server.home / "catalog.db", server.home / "warehouse")
    outside = {}
    for name in ("readable", "missing"):
        root = tmp_path / f"outside-{name}"
        metadata = root / "metadata" / "00000-forged.metadata.json"
        metadata.parent.mkdir(parents=True)
        metadata.write_text(json.dumps({**made["metadata"], "location": rest._file_uri(root)}))
        (root / "precious.txt").write_text("not the sandbox's")
        store.swap([("demo", name, None, rest._file_uri(metadata))])
        outside[name] = root
    outside["missing"].joinpath("metadata", "00000-forged.metadata.json").unlink()
    for name, root in outside.items():
        assert _call(server, "DELETE", f"/v1/sandbox/namespaces/demo/tables/{name}?purgeRequested=true")[0] == 204
        assert (root / "precious.txt").exists(), name


def test_rename_and_register_move_names_not_files(catalog) -> None:
    from pyiceberg.exceptions import NoSuchTableError

    catalog.create_namespace("demo")
    catalog.create_namespace("other")
    table = catalog.create_table("demo.t", schema=_rows(0).schema)
    table.append(_rows(4))
    catalog.rename_table("demo.t", "other.renamed")
    with pytest.raises(NoSuchTableError):
        catalog.load_table("demo.t")
    location = catalog.load_table("other.renamed").metadata_location
    catalog.register_table("demo.again", location)
    assert catalog.load_table("demo.again").scan().to_arrow().num_rows == 4


def test_drop_keeps_files_and_purge_removes_them(catalog) -> None:
    catalog.create_namespace("demo")
    kept = catalog.create_table("demo.kept", schema=_rows(0).schema)
    kept.append(_rows(2))
    purged = catalog.create_table("demo.purged", schema=_rows(0).schema)
    purged.append(_rows(2))
    kept_root = Path(kept.location().removeprefix("file://"))
    purged_root = Path(purged.location().removeprefix("file://"))
    catalog.drop_table("demo.kept")
    catalog.purge_table("demo.purged")
    assert kept_root.exists() and not purged_root.exists()


def test_a_purge_keeps_files_another_table_still_uses(catalog, server: rest.Running, caplog) -> None:
    """Rename `t` away and create a new `t`, and the new one gets the old one's default
    location; register one metadata file twice, and both names point at it. A purge of either
    deleted the other's files too. Now it drops the name, keeps the files, and says why."""
    caplog.set_level(logging.INFO, logger="icelens.rest")
    catalog.create_namespace("demo")
    catalog.create_table("demo.t", schema=_rows(0).schema).append(_rows(3))
    catalog.rename_table("demo.t", "demo.t_old")
    catalog.create_table("demo.t", schema=_rows(0).schema).append(_rows(5))
    assert catalog.load_table("demo.t_old").location() == catalog.load_table("demo.t").location()
    catalog.purge_table("demo.t_old")
    assert catalog.load_table("demo.t").scan().to_arrow().num_rows == 5
    assert "another table's are at" in caplog.text
    catalog.register_table("demo.twin", catalog.load_table("demo.t").metadata_location)
    catalog.purge_table("demo.twin")
    assert catalog.load_table("demo.t").scan().to_arrow().num_rows == 5

    inner = rest._file_uri(Path(catalog.load_table("demo.t").location().removeprefix("file://")) / "inner")
    catalog.create_table("demo.inner", schema=_rows(0).schema, location=inner).append(_rows(2))
    catalog.purge_table("demo.inner")  # inside `t`'s directory
    assert catalog.load_table("demo.t").scan().to_arrow().num_rows == 5
    catalog.purge_table("demo.t")  # and once nothing else is there, a purge does delete
    assert not (server.home / "warehouse" / "demo.db" / "t").exists()


def test_a_table_whose_metadata_file_is_gone_still_drops(catalog, server: rest.Running) -> None:
    """Deleted by hand, or never finished writing: HEAD and DELETE both read the file first, so
    such a table answered 500 to everything, and its namespace could never be dropped."""
    catalog.create_namespace("demo")
    for name in ("kept", "purged"):
        table = catalog.create_table(f"demo.{name}", schema=_rows(0).schema)
        table.append(_rows(2))
        Path(table.metadata_location.removeprefix("file://")).unlink()
    status, body = _call(server, "GET", "/v1/sandbox/namespaces/demo/tables/kept")
    assert status == 404 and "/demo.db/kept/metadata/00001-" in body["error"]["message"]  # which file, not a traceback
    status, body = _call(server, "POST", "/v1/sandbox/namespaces/demo/tables/kept", {"updates": []})
    assert status == 404 and "/demo.db/kept/metadata/00001-" in body["error"]["message"]
    assert catalog.table_exists("demo.kept")
    folder = server.home / "warehouse" / "demo.db"
    catalog.drop_table("demo.kept")
    catalog.purge_table("demo.purged")
    assert (folder / "kept" / "data").is_dir() and not (folder / "purged").exists()
    catalog.drop_namespace("demo")


def test_a_table_cannot_be_put_outside_the_sandbox(server: rest.Running, tmp_path: Path) -> None:
    """A purge deletes a table's location, so a location outside the sandbox is refused up front."""
    _call(server, "POST", "/v1/sandbox/namespaces", {"namespace": ["demo"]})
    elsewhere = tmp_path / "not-the-sandbox"
    status, body = _call(server, "POST", "/v1/sandbox/namespaces/demo/tables", {
        "name": "t", "location": elsewhere.as_uri(), "schema": {"type": "struct", "fields": []},
    })
    assert status == 400 and "inside" in body["error"]["message"]
    assert not elsewhere.exists()
    status, body = _call(server, "POST", "/v1/sandbox/namespaces/demo/tables", {
        "name": "t", "location": "s3://bucket/t", "schema": {"type": "struct", "fields": []},
    })
    assert status == 400


# -- what the token reaches: the sandbox's warehouse, and nothing else ----------------------

_SCHEMA = {"type": "struct", "fields": [{"id": 1, "name": "a", "type": "long", "required": False}]}


def _create_by_commit(location: str | None = None) -> dict:
    """A table made by a commit alone, as a staged create finishes: everything from updates."""
    updates = [
        {"action": "add-schema", "schema": {**_SCHEMA, "schema-id": 0}},
        {"action": "set-current-schema", "schema-id": -1},
        {"action": "add-spec", "spec": {"spec-id": 0, "fields": []}},
        {"action": "set-default-spec", "spec-id": -1},
        {"action": "add-sort-order", "sort-order": {"order-id": 0, "fields": []}},
        {"action": "set-default-sort-order", "sort-order-id": -1},
    ]
    if location is not None:
        updates.append({"action": "set-location", "location": location})
    return {"requirements": [{"type": "assert-create"}], "updates": updates}


def test_register_reads_only_table_metadata_inside_the_sandbox(server: rest.Running, tmp_path: Path) -> None:
    """Registering answers with the file it read, so what it will read is the whole question.

    It read any local path it was given and echoed it back as the table's metadata, credentials
    files included, and took a table whose metadata named a location anywhere at all.
    """
    _call(server, "POST", "/v1/sandbox/namespaces", {"namespace": ["demo"]})
    warehouse = server.home / "warehouse"
    secret = tmp_path / "creds.json"
    secret.write_text(json.dumps({"aws_secret": "TOP-SECRET-VALUE"}))
    not_metadata = warehouse / "notes.json"
    not_metadata.write_text(json.dumps({"aws_secret": "TOP-SECRET-VALUE"}))
    _, made = _call(server, "POST", "/v1/sandbox/namespaces/demo/tables", {"name": "t", "schema": _SCHEMA})
    pointing_out = {**made["metadata"], "location": rest._file_uri(tmp_path / "elsewhere")}
    forged = warehouse / "demo.db" / "forged.metadata.json"
    forged.write_text(json.dumps(pointing_out))
    for path in (secret, not_metadata, warehouse / "missing.metadata.json", warehouse / "demo.db", warehouse, forged):
        status, body = _call(server, "POST", "/v1/sandbox/namespaces/demo/register",
                             {"name": "leak", "metadata-location": rest._file_uri(path)})
        assert status == 400, (path, body)
        assert "TOP-SECRET" not in json.dumps(body)
    assert _call(server, "GET", "/v1/sandbox/namespaces/demo/tables/leak")[0] == 404
    status, _ = _call(server, "POST", "/v1/sandbox/namespaces/demo/register",
                      {"name": "again", "metadata-location": made["metadata-location"]})
    assert status == 200  # and a table's own metadata still registers


def test_no_commit_moves_a_table_out_of_the_sandbox(server: rest.Running, tmp_path: Path) -> None:
    """`set-location` decides where the next metadata file is written and what a purge deletes,
    on a table that exists as much as on one being made - so every commit's location is checked,
    and the warehouse itself is not inside it."""
    _call(server, "POST", "/v1/sandbox/namespaces", {"namespace": ["demo"]})
    _call(server, "POST", "/v1/sandbox/namespaces/demo/tables", {"name": "t", "schema": _SCHEMA})
    warehouse = server.home / "warehouse"
    outside = tmp_path / "outside"
    for location in (rest._file_uri(outside), rest._file_uri(warehouse), rest._file_uri(warehouse / "demo.db" / ".." / "..")):
        move = {"requirements": [], "updates": [{"action": "set-location", "location": location}]}
        assert _call(server, "POST", "/v1/sandbox/namespaces/demo/tables/t", move)[0] == 400, location
        assert _call(server, "POST", "/v1/sandbox/namespaces/demo/tables/u", _create_by_commit(location))[0] == 400, location
    assert not outside.exists()
    assert sorted(p.name for p in warehouse.iterdir()) == ["demo.db"]
    assert _call(server, "POST", "/v1/sandbox/namespaces/demo/tables/u", _create_by_commit())[0] == 200


@pytest.mark.parametrize("name", [".", "..", "../demo.db", "a/b", "a\\b", "a\x00b", "a\x1fb", "a\x85b"])
def test_a_table_name_cannot_be_a_path(server: rest.Running, name: str) -> None:
    """A table's default location is `<namespace>.db/<name>`: `..` put one at the warehouse root,
    and a purge of it deleted every table in the sandbox."""
    _call(server, "POST", "/v1/sandbox/namespaces", {"namespace": ["demo"]})
    _call(server, "POST", "/v1/sandbox/namespaces/demo/tables", {"name": "t", "schema": _SCHEMA})
    quoted = urllib.parse.quote(name, safe="")
    assert _call(server, "POST", "/v1/sandbox/namespaces/demo/tables", {"name": name, "schema": _SCHEMA})[0] == 400
    assert _call(server, "POST", f"/v1/sandbox/namespaces/demo/tables/{quoted}", _create_by_commit())[0] == 400
    rename = {"source": {"namespace": ["demo"], "name": "t"}, "destination": {"namespace": ["demo"], "name": name}}
    assert _call(server, "POST", "/v1/sandbox/tables/rename", rename)[0] == 400
    assert _call(server, "GET", "/v1/sandbox/namespaces/demo/tables")[1]["identifiers"] == [{"namespace": ["demo"], "name": "t"}]


@pytest.mark.parametrize("level", ["a/b", "a\\b", "a.b", "a\x00b"])
def test_a_namespace_level_cannot_be_a_path(server: rest.Running, level: str) -> None:
    assert _call(server, "POST", "/v1/sandbox/namespaces", {"namespace": [level]})[0] == 400
    assert _call(server, "GET", f"/v1/sandbox/namespaces/{urllib.parse.quote(level, safe='')}")[0] == 400


def test_a_purge_never_deletes_the_warehouse_itself(server: rest.Running, catalog) -> None:
    """PyIceberg's own SQL catalog, which opens the same `catalog.db`, will put a table anywhere,
    the warehouse root included. Purging that drops the table; the warehouse, and every other
    table in it, stays."""
    from pyiceberg.catalog.sql import SqlCatalog

    catalog.create_namespace("demo")
    catalog.create_table("demo.kept", schema=_rows(0).schema).append(_rows(3))
    warehouse = server.home / "warehouse"
    direct = SqlCatalog(rest.CATALOG, uri=f"sqlite:///{server.home / 'catalog.db'}", warehouse=rest._file_uri(warehouse))
    direct.create_table("demo.everything", schema=_rows(0).schema, location=rest._file_uri(warehouse))
    catalog.purge_table("demo.everything")
    assert not catalog.table_exists("demo.everything")
    assert catalog.load_table("demo.kept").scan().to_arrow().num_rows == 3


def test_metadata_that_leaves_out_what_is_optional_can_still_be_committed_to(catalog) -> None:
    """The spec makes `refs`, the logs, the statistics, `properties` and the current snapshot
    optional, and another writer may leave them out. Such a table loaded, then answered every
    commit with a 400 KeyError."""
    catalog.create_namespace("demo")
    for name, rows in (("full", 3), ("empty", 0)):
        table = catalog.create_table(f"demo.{name}", schema=_rows(0).schema)
        if rows:
            table.append(_rows(rows))
        document = json.loads(Path(table.metadata_location.removeprefix("file://")).read_text())
        optional = ["refs", "snapshot-log", "metadata-log", "statistics", "partition-statistics", "properties"]
        for key in optional if rows else [*optional, "snapshots", "current-snapshot-id"]:
            document.pop(key, None)
        written = Path(table.location().removeprefix("file://")) / "metadata" / "99999-minimal.metadata.json"
        written.write_text(json.dumps(document))
        catalog.register_table(f"demo.{name}_minimal", rest._file_uri(written))
        minimal = catalog.load_table(f"demo.{name}_minimal")
        minimal.transaction().set_properties(touched="yes").commit_transaction()
        minimal.append(_rows(2, 100))  # asserts where `main` is: with no refs, at the current snapshot
        minimal = catalog.load_table(f"demo.{name}_minimal")
        assert (minimal.scan().to_arrow().num_rows, minimal.properties) == (rows + 2, {"touched": "yes"})


def test_the_catalog_is_still_there_after_a_restart(tmp_path: Path) -> None:
    from pyiceberg.catalog.rest import RestCatalog

    home = tmp_path / "sandbox"
    first = rest.serve(home)
    catalog = RestCatalog("t", uri=first.uri, token=first.token, **{"header.X-Iceberg-Access-Delegation": ""})
    catalog.create_namespace("demo")
    catalog.create_table("demo.t", schema=_rows(0).schema).append(_rows(7))
    first.stop()
    again = rest.serve(home)
    try:
        catalog = RestCatalog("t", uri=again.uri, token=again.token, **{"header.X-Iceberg-Access-Delegation": ""})
        assert catalog.load_table("demo.t").scan().to_arrow().num_rows == 7
    finally:
        again.stop()


def test_pyicebergs_own_sql_catalog_reads_what_the_server_wrote(catalog, server: rest.Running) -> None:
    """One `catalog.db`, two ways in - which is what lets a v2 sandbox become a v3 one."""
    from pyiceberg.catalog.sql import SqlCatalog

    catalog.create_namespace("demo")
    catalog.create_table("demo.t", schema=_rows(0).schema).append(_rows(9))
    direct = SqlCatalog(rest.CATALOG, uri=f"sqlite:///{server.home / 'catalog.db'}", warehouse=(server.home / "warehouse").as_uri())
    assert direct.list_namespaces() == [("demo",)]
    assert direct.load_table("demo.t").scan().to_arrow().num_rows == 9


@pytest.mark.parametrize(
    ("uri", "path"),
    [
        ("file:///tmp/My Lessons ü/w", "/tmp/My Lessons ü/w"),
        ("file://localhost/tmp/100% done/w", "/tmp/100% done/w"),
        ("file:///C:/Users/Zoë Smith/w", "C:/Users/Zoë Smith/w"),
    ],
)
def test_a_file_uri_is_the_path_as_it_is_not_percent_decoded(uri: str, path: str) -> None:
    """DuckDB and PyIceberg read `file://` paths undecoded, so the catalog has to as well."""
    assert rest._local(uri).as_posix() == path
    assert rest._file_uri(rest._local(uri)) == uri.replace("//localhost/", "///")


@pytest.mark.parametrize("uri", ["file://elsewhere/tmp/w", "s3://bucket/w", "/tmp/w", "file:///tmp/w?x=1", "file:///tmp/w#x"])
def test_a_location_that_is_not_a_plain_local_file_is_refused(uri: str) -> None:
    with pytest.raises(rest.RestError) as caught:
        rest._local(uri)
    assert caught.value.status == 400


# -- the rules, without HTTP -----------------------------------------------------------------


def _metadata(version: int = 3) -> dict:
    return rest.fresh_metadata(
        {"name": "t", "schema": {"type": "struct", "fields": [{"id": 1, "name": "a", "type": "long", "required": False}]},
         "properties": {"format-version": str(version)}},
        "file:///tmp/t",
    )


def _snapshot(snapshot_id: int, sequence: int, first_row_id: int | None, added: int, parent: int | None = None) -> dict:
    snapshot = {"snapshot-id": snapshot_id, "sequence-number": sequence, "timestamp-ms": 1, "summary": {"operation": "append"},
                "manifest-list": "file:///tmp/m.avro", "schema-id": 0, "added-rows": added}
    if first_row_id is not None:
        snapshot["first-row-id"] = first_row_id
    if parent is not None:
        snapshot["parent-snapshot-id"] = parent
    return snapshot


def test_format_version_leaves_the_properties_and_becomes_a_field() -> None:
    metadata = _metadata(3)
    assert metadata["format-version"] == 3 and "format-version" not in metadata["properties"]
    assert metadata["next-row-id"] == 0
    assert "next-row-id" not in _metadata(2)
    with pytest.raises(rest.RestError):
        _metadata(1)


def test_v3_row_ids_move_on_and_are_never_reused() -> None:
    metadata = rest.apply(_metadata(3), [{"action": "add-snapshot", "snapshot": _snapshot(1, 1, 0, 100)}])
    assert metadata["next-row-id"] == 100
    metadata = rest.apply(metadata, [{"action": "add-snapshot", "snapshot": _snapshot(2, 2, 100, 5, parent=1)}])
    assert metadata["next-row-id"] == 105
    with pytest.raises(rest.RestError, match="behind"):
        rest.apply(metadata, [{"action": "add-snapshot", "snapshot": _snapshot(3, 3, 50, 1, parent=2)}])
    with pytest.raises(rest.RestError, match="first-row-id"):
        rest.apply(metadata, [{"action": "add-snapshot", "snapshot": _snapshot(3, 3, None, 1, parent=2)}])


def test_a_sequence_number_never_goes_backwards() -> None:
    metadata = rest.apply(_metadata(2), [{"action": "add-snapshot", "snapshot": _snapshot(1, 5, None, 1)}])
    with pytest.raises(rest.RestError, match="sequence"):
        rest.apply(metadata, [{"action": "add-snapshot", "snapshot": _snapshot(2, 4, None, 1, parent=1)}])


def test_main_moves_the_current_snapshot_and_the_log_and_nothing_else_does() -> None:
    metadata = rest.apply(_metadata(2), [
        {"action": "add-snapshot", "snapshot": _snapshot(1, 1, None, 1)},
        {"action": "set-snapshot-ref", "ref-name": "tag", "type": "tag", "snapshot-id": 1},
    ])
    assert metadata["current-snapshot-id"] is None and metadata["snapshot-log"] == []
    metadata = rest.apply(metadata, [{"action": "set-snapshot-ref", "ref-name": "main", "type": "branch", "snapshot-id": 1}])
    assert metadata["current-snapshot-id"] == 1 and metadata["snapshot-log"][-1]["snapshot-id"] == 1
    with pytest.raises(rest.RestError):
        rest.apply(metadata, [{"action": "set-snapshot-ref", "ref-name": "main", "type": "branch", "snapshot-id": 99}])


def _log(metadata: dict) -> list[tuple[int, int]]:
    return [(entry["snapshot-id"], entry["timestamp-ms"]) for entry in metadata["snapshot-log"]]


def test_the_snapshot_log_runs_forward_through_a_rollback() -> None:
    """Java logs a snapshot the commit added at its own time, and any other at the commit's.
    Logging a rollback at its snapshot's time ran the log backwards: PyIceberg's time travel
    then named the wrong snapshot, and Java refuses an unsorted log."""
    main = lambda snapshot_id: {"action": "set-snapshot-ref", "ref-name": "main", "type": "branch", "snapshot-id": snapshot_id}  # noqa: E731
    metadata = rest.apply(_metadata(2), [{"action": "add-snapshot", "snapshot": {**_snapshot(1, 1, None, 1), "timestamp-ms": 100}}, main(1)], now=150)
    metadata = rest.apply(metadata, [{"action": "add-snapshot", "snapshot": {**_snapshot(2, 2, None, 1, parent=1), "timestamp-ms": 200}}, main(2)], now=250)
    metadata = rest.apply(metadata, [main(1)], now=300)
    assert _log(metadata) == [(1, 100), (2, 200), (1, 300)]
    assert _log(rest.apply(metadata, [main(1)], now=400)) == _log(metadata)  # main already there: nothing happened


def test_expiring_a_snapshot_takes_the_log_before_it_with_it() -> None:
    """As Java's `updateSnapshotLog`: with s2 gone from (s1, s2, s3), keeping (s1, s3) would say
    s1 was current until s3 - through the time s2 was."""
    metadata = _metadata(2)
    for snapshot_id in (1, 2, 3):
        snapshot = {**_snapshot(snapshot_id, snapshot_id, None, 1), "timestamp-ms": 100 * snapshot_id}
        metadata = rest.apply(metadata, [{"action": "add-snapshot", "snapshot": snapshot},
                                         {"action": "set-snapshot-ref", "ref-name": "main", "type": "branch", "snapshot-id": snapshot_id}])
    assert _log(rest.apply(metadata, [{"action": "remove-snapshots", "snapshot-ids": [2]}])) == [(3, 300)]
    assert _log(rest.apply(metadata, [{"action": "remove-snapshots", "snapshot-ids": [1]}])) == [(2, 200), (3, 300)]


def test_time_travel_after_a_rollback_names_what_was_current_then(catalog) -> None:
    import time

    catalog.create_namespace("demo")
    table = catalog.create_table("demo.t", schema=_rows(0).schema)
    table.append(_rows(1))
    first = table.current_snapshot()
    time.sleep(0.02)
    table.append(_rows(1, 1))
    second = table.current_snapshot()
    time.sleep(0.05)
    table.manage_snapshots().rollback_to_snapshot(first.snapshot_id).commit()
    table = catalog.load_table("demo.t")
    stamps = [entry.timestamp_ms for entry in table.metadata.snapshot_log]
    assert stamps == sorted(stamps) and table.current_snapshot().snapshot_id == first.snapshot_id
    assert table.snapshot_as_of_timestamp(second.timestamp_ms + 10).snapshot_id == second.snapshot_id


def test_minus_one_means_the_one_just_added() -> None:
    schema = {"type": "struct", "fields": [{"id": 1, "name": "a", "type": "long", "required": False},
                                            {"id": 2, "name": "b", "type": "string", "required": False}]}
    metadata = rest.apply(_metadata(2), [{"action": "add-schema", "schema": schema}, {"action": "set-current-schema", "schema-id": -1}])
    assert (metadata["current-schema-id"], metadata["last-column-id"]) == (1, 2)
    spec = {"fields": [{"source-id": 2, "transform": "identity", "name": "b"}]}
    metadata = rest.apply(metadata, [{"action": "add-spec", "spec": spec}, {"action": "set-default-spec", "spec-id": -1}])
    assert metadata["default-spec-id"] == 1 and metadata["partition-specs"][1]["fields"][0]["field-id"] == 1000
    assert metadata["last-partition-id"] == 1000


@pytest.mark.parametrize(
    ("requirement", "holds"),
    [
        ({"type": "assert-current-schema-id", "current-schema-id": 0}, True),
        ({"type": "assert-current-schema-id", "current-schema-id": 1}, False),
        ({"type": "assert-last-assigned-field-id", "last-assigned-field-id": 1}, True),
        ({"type": "assert-last-assigned-field-id", "last-assigned-field-id": 2}, False),
        ({"type": "assert-default-spec-id", "default-spec-id": 0}, True),
        ({"type": "assert-default-spec-id", "default-spec-id": 3}, False),
        ({"type": "assert-last-assigned-partition-id", "last-assigned-partition-id": 999}, True),
        ({"type": "assert-last-assigned-partition-id", "last-assigned-partition-id": 1000}, False),
        ({"type": "assert-default-sort-order-id", "default-sort-order-id": 0}, True),
        ({"type": "assert-default-sort-order-id", "default-sort-order-id": 1}, False),
        ({"type": "assert-ref-snapshot-id", "ref": "main", "snapshot-id": None}, True),
        ({"type": "assert-ref-snapshot-id", "ref": "main", "snapshot-id": 1}, False),
        ({"type": "assert-create"}, False),
    ],
)
def test_each_requirement_is_checked(requirement: dict, holds: bool) -> None:
    if holds:
        rest.check(_metadata(2), [requirement])
    else:
        with pytest.raises(rest.RestError) as caught:
            rest.check(_metadata(2), [requirement])
        assert caught.value.status == 409


@pytest.mark.parametrize(
    ("version", "updates", "message"),
    [
        (3, [{"action": "add-snapshot", "snapshot": {k: v for k, v in _snapshot(1, 1, 0, 0).items() if k != "added-rows"}}], "added-rows"),
        (3, [{"action": "add-snapshot", "snapshot": _snapshot(1, 1, 0, -50)}], "added-rows"),
        (2, [{"action": "add-snapshot", "snapshot": _snapshot(1, 1, None, 1)},
             {"action": "set-snapshot-ref", "ref-name": "main", "type": "tag", "snapshot-id": 1}], "branch"),
        (2, [{"action": "remove-partition-specs", "spec-ids": [0]}], "default partition spec"),
        (2, [{"action": "remove-schemas", "schema-ids": [0]}], "current schema"),
        (2, [{"action": "enable-row-lineage"}], "format-version 3"),
    ],
    ids=["v3-no-added-rows", "v3-negative-added-rows", "main-as-a-tag", "default-spec", "current-schema", "lineage-on-v2"],
)
def test_an_update_java_refuses_is_refused_not_applied_or_skipped(version: int, updates: list, message: str) -> None:
    """A v3 snapshot with no `added-rows`, or a negative count, let the next snapshot reuse its row
    ids; the rest were applied, or silently skipped, where Java answers that they cannot be."""
    with pytest.raises(rest.RestError, match=message) as caught:
        rest.apply(_metadata(version), updates)
    assert caught.value.status == 400


def test_row_lineage_is_simply_part_of_v3() -> None:
    assert rest.apply(_metadata(3), [{"action": "enable-row-lineage"}])["next-row-id"] == 0


def test_an_update_this_catalog_does_not_know_is_refused() -> None:
    with pytest.raises(rest.RestError, match="cannot apply"):
        rest.apply(_metadata(2), [{"action": "add-view-version"}])


# -- DuckDB, the client that writes v3 -----------------------------------------------------


@pytest.mark.duckdb_extensions
def test_duckdb_writes_a_v3_table_through_it(server: rest.Running, catalog) -> None:
    import duckdb

    catalog.create_namespace("core")
    con = duckdb.connect()
    for extension in ("iceberg", "httpfs"):
        con.execute(f"LOAD {extension}")
    con.execute(f"ATTACH 'sandbox' AS p (TYPE ICEBERG, ENDPOINT '{server.uri}', TOKEN '{server.token}', ACCESS_DELEGATION_MODE 'none')")
    for statement in (
        "CREATE TABLE p.core.items WITH (\"format-version\" = 3) AS SELECT i AS id, 'n' || i AS name FROM range(1, 101) t(i)",
        "DELETE FROM p.core.items WHERE id % 10 = 0",
        "UPDATE p.core.items SET name = 'changed' WHERE id = 4",
        "MERGE INTO p.core.items t USING (SELECT 999 AS id, 'new' AS name) s ON t.id = s.id "
        "WHEN MATCHED THEN UPDATE SET name = s.name WHEN NOT MATCHED THEN INSERT VALUES (s.id, s.name)",
        "ALTER TABLE p.core.items ADD COLUMN channel VARCHAR",
    ):
        con.execute(statement)
    count, distinct = con.execute("SELECT count(*), count(DISTINCT _row_id) FROM p.core.items").fetchone()
    assert count == distinct == 91
    assert con.execute("SELECT _row_id FROM p.core.items WHERE id = 4").fetchone()[0] == 3  # an update keeps its id

    table = catalog.load_table("core.items")
    assert table.metadata.format_version == 3
    assert table.metadata.next_row_id >= 101
    assert table.scan().to_arrow().num_rows == 91
    assert "POST /v1/sandbox/transactions/commit" in {r.split("?")[0] for r in server.requests}
