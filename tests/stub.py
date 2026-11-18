"""A REST catalog that answers just enough, from 127.0.0.1, for the paths that begin with one.

The warehouse questions - "which one?", "there is only one, so take it", ctrl+w to switch - all
start with a server answering, and the live suite needs Lakekeeper running to ask them. This
answers the handful of requests those paths make, the way Lakekeeper answers them: a 400 asking
for a warehouse from `/v1/config`, the list from the management API beside the catalog, and an
Iceberg REST prefix per warehouse so each one can hold namespaces of its own. Nothing else.
"""

from __future__ import annotations

import http.server
import json
import threading
import urllib.parse
from typing import Any


class StubCatalog:
    """`with StubCatalog({"wh-a": ["demo"]}) as stub:` and point icelens at `stub.uri`."""

    def __init__(self, warehouses: dict[str, list[str]], listed: bool = True) -> None:
        self.warehouses = warehouses
        #: whether the management API will list the warehouses at all
        self.listed = listed
        self.requests: list[str] = []
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server's spelling
                stub.requests.append(self.path)
                status, body = stub.answer(self.path)
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args: object) -> None:
                pass

        self._server = http.server.HTTPServer(("127.0.0.1", 0), Handler)

    @property
    def uri(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}/catalog"

    def __enter__(self) -> StubCatalog:
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    @staticmethod
    def _error(status: int, kind: str, message: str) -> tuple[int, dict[str, Any]]:
        return status, {"error": {"message": message, "type": kind, "code": status}}

    def answer(self, raw_path: str) -> tuple[int, dict[str, Any]]:
        split = urllib.parse.urlsplit(raw_path)
        query = urllib.parse.parse_qs(split.query)
        parts = [urllib.parse.unquote(p) for p in split.path.split("/") if p]
        if parts == ["management", "v1", "warehouse"]:
            if not self.listed:
                return self._error(404, "NotFoundException", "no management API here")
            return 200, {"warehouses": [{"name": name} for name in self.warehouses]}
        if parts == ["catalog", "v1", "config"]:
            warehouse = (query.get("warehouse") or [""])[0]
            if not warehouse:
                return self._error(400, "BadRequestException", "Please specify a warehouse")
            if warehouse not in self.warehouses:
                return self._error(404, "NotFoundException", f"warehouse {warehouse} does not exist")
            return 200, {"defaults": {}, "overrides": {"prefix": warehouse}}
        if len(parts) >= 4 and parts[:2] == ["catalog", "v1"] and parts[2] in self.warehouses and parts[3] == "namespaces":
            namespaces = self.warehouses[parts[2]]
            if len(parts) == 4:
                if query.get("parent"):
                    return 200, {"namespaces": []}
                return 200, {"namespaces": [[name] for name in namespaces]}
            if len(parts) == 6 and parts[5] == "tables" and parts[4] in namespaces:
                return 200, {"identifiers": []}
        return self._error(404, "NotFoundException", f"nothing at {split.path}")
