from __future__ import annotations

from http.client import HTTPConnection, HTTPSConnection
from typing import Any
from urllib.parse import urlsplit, urlunsplit


V2_PREFIX = "/v2"
_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def is_v2_path(raw_path: str) -> bool:
    path = urlsplit(raw_path).path
    return path == V2_PREFIX or path.startswith(f"{V2_PREFIX}/")


def v2_upstream_path(raw_path: str) -> str:
    parsed = urlsplit(raw_path)
    if parsed.path == V2_PREFIX:
        path = "/"
    elif parsed.path.startswith(f"{V2_PREFIX}/"):
        path = parsed.path[len(V2_PREFIX) :]
    else:
        raise ValueError("request path is outside the V2 mount")
    return urlunsplit(("", "", path or "/", parsed.query, ""))


def create_gateway_handler(legacy_handler: type[Any], v2_upstream: str) -> type[Any]:
    upstream = urlsplit(v2_upstream)
    if upstream.scheme not in {"http", "https"} or not upstream.hostname:
        raise ValueError("V2 upstream must be an absolute HTTP(S) URL")
    if upstream.path not in {"", "/"} or upstream.query or upstream.fragment:
        raise ValueError("V2 upstream must not contain a path, query or fragment")

    class GatewayHandler(legacy_handler):
        _v2_upstream = upstream

        def do_GET(self) -> None:
            if self._handle_v2_request():
                return
            super().do_GET()

        def do_POST(self) -> None:
            if self._handle_v2_request():
                return
            super().do_POST()

        def do_DELETE(self) -> None:
            if self._handle_v2_request():
                return
            super().do_DELETE()

        def _handle_v2_request(self) -> bool:
            if not is_v2_path(self.path):
                return False
            parsed = urlsplit(self.path)
            if parsed.path == V2_PREFIX:
                location = f"{V2_PREFIX}/"
                if parsed.query:
                    location = f"{location}?{parsed.query}"
                self.send_response(308)
                self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return True
            self._proxy_v2()
            return True

        def _proxy_v2(self) -> None:
            length = int(self.headers.get("Content-Length") or "0")
            body = self.rfile.read(length) if length > 0 else None
            headers = {
                name: value
                for name, value in self.headers.items()
                if name.casefold() not in _HOP_BY_HOP_HEADERS
                and name.casefold() not in {"host", "content-length"}
            }
            port = self._v2_upstream.port
            if port is None:
                port = 443 if self._v2_upstream.scheme == "https" else 80
            connection_type = (
                HTTPSConnection
                if self._v2_upstream.scheme == "https"
                else HTTPConnection
            )
            connection = connection_type(
                self._v2_upstream.hostname,
                port,
                timeout=120,
            )
            try:
                connection.request(
                    self.command,
                    v2_upstream_path(self.path),
                    body=body,
                    headers=headers,
                )
                response = connection.getresponse()
                response_body = response.read()
                self.send_response(response.status, response.reason)
                for name, value in response.getheaders():
                    normalized = name.casefold()
                    if normalized in _HOP_BY_HOP_HEADERS or normalized == "content-length":
                        continue
                    if normalized == "location" and value.startswith("/"):
                        value = f"{V2_PREFIX}{value}"
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(response_body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(response_body)
            except Exception as exc:
                self.log_error("V2 upstream proxy failed: %s", exc)
                payload = b'{"status":"error","detail":{"code":"v2_upstream_unavailable"}}'
                self.send_response(502)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)
            finally:
                connection.close()

    GatewayHandler.__name__ = "AIBalancesGatewayHandler"
    return GatewayHandler
