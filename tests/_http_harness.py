"""Shared HTTP harness for PoC tests (stdlib only).

Starts the CoreHTTPServer on an ephemeral loopback port in a daemon
thread and provides small request helpers (single/batch infer + GET).
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from typing import Any, Optional


def make_pgm(width: int, height: int, pixels: list) -> bytes:
    """Build a binary PGM (P5) image; pixels = flat list of 0..255."""
    header = f"P5\n{width} {height}\n255\n".encode("ascii")
    return header + bytes(pixels)


class HttpHarness:
    def __init__(self, service: Any) -> None:
        self.service = service
        self.httpd = None
        self.thread: Optional[threading.Thread] = None
        self.port = 0

    def start(self) -> None:
        from sydeco_lightml_core.server import CoreHTTPServer

        self.httpd = CoreHTTPServer(("127.0.0.1", 0), self.service)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True
        )
        self.thread.start()

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()

    def request(
        self,
        method: str,
        path: str,
        payload: Optional[dict] = None,
        token: Optional[str] = None,
    ) -> tuple:
        url = f"http://127.0.0.1:{self.port}{path}"
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req) as resp:
                body = resp.read().decode("utf-8")
                return resp.status, json.loads(body) if body else None
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            try:
                return exc.code, json.loads(body)
            except (json.JSONDecodeError, ValueError):
                return exc.code, body

    def infer(self, app_id: str, payload: dict, token: str) -> tuple:
        return self.request(
            "POST", f"/api/v1/apps/{app_id}/infer", payload=payload, token=token
        )

    def get(self, path: str, token: Optional[str] = None) -> tuple:
        return self.request("GET", path, token=token)
