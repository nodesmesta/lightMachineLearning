from __future__ import annotations

import http.client
import os
import tempfile
import threading
import unittest
from unittest import mock

from sydeco_lightml_core import service_main
from sydeco_lightml_core.core import CoreService
from sydeco_lightml_core.server import CoreHTTPServer


class ProductionServiceEntrypointTests(unittest.TestCase):
    def test_default_service_args_are_production_oriented(self) -> None:
        parser = service_main.build_parser()
        args = parser.parse_args([])
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8765)
        self.assertEqual(args.data_dir, "/var/lib/sydeco/lightml")
        self.assertEqual(args.worker_mode, "systemd")

    def test_environment_overrides_service_args(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "SYDECO_LIGHTML_DATA_DIR": "/tmp/lightml-state",
                "SYDECO_LIGHTML_WORKER_MODE": "inprocess",
                "SYDECO_LIGHTML_HOST": "127.0.0.1",
                "SYDECO_LIGHTML_PORT": "9876",
            },
            clear=False,
        ):
            parser = service_main.build_parser()
            args = parser.parse_args([])
        self.assertEqual(args.data_dir, "/tmp/lightml-state")
        self.assertEqual(args.worker_mode, "inprocess")
        self.assertEqual(args.port, 9876)

    def test_core_http_health_runs_with_service_entrypoint_components(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sydeco-prod-service-") as tmp:
            service = CoreService(data_dir=os.path.join(tmp, "state"), worker_mode="inprocess")
            httpd = CoreHTTPServer(("127.0.0.1", 0), service)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                port = httpd.server_address[1]
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                conn.request("GET", "/health/live")
                resp = conn.getresponse()
                body = resp.read().decode("utf-8")
                conn.close()
                self.assertEqual(resp.status, 200)
                self.assertEqual(body, '{"status": "ok"}')
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
