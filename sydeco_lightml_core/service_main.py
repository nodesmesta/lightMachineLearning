"""Production service entry point for SYDECO LightML Core."""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from typing import Sequence

from .core import CoreService
from .server import CoreHTTPServer


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_DATA_DIR = "/var/lib/sydeco/lightml"


def _env_default(name: str, fallback: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else fallback


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run SYDECO LightML Core as a production service")
    parser.add_argument("--host", default=_env_default("SYDECO_LIGHTML_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(_env_default("SYDECO_LIGHTML_PORT", str(DEFAULT_PORT))))
    parser.add_argument("--data-dir", default=_env_default("SYDECO_LIGHTML_DATA_DIR", DEFAULT_DATA_DIR))
    parser.add_argument(
        "--worker-mode",
        choices=("systemd", "inprocess"),
        default=_env_default("SYDECO_LIGHTML_WORKER_MODE", "systemd"),
    )
    parser.add_argument("--body-limit", type=int, default=int(_env_default("SYDECO_LIGHTML_BODY_LIMIT", str(1024 * 1024))))
    return parser


class IncomingBundleWatcher(threading.Thread):
    """Daemon thread for continuous scanning and deployment of drop-in bundles."""

    def __init__(
        self,
        service: CoreService,
        incoming_dir: str | None = None,
        interval: float = 2.0,
    ) -> None:
        super().__init__(daemon=True, name="IncomingBundleWatcher")
        self.service = service
        self.incoming_dir = incoming_dir
        self.interval = interval
        self._stopping = threading.Event()

    def stop(self) -> None:
        self._stopping.set()

    def run(self) -> None:
        while not self._stopping.is_set():
            try:
                self.service.scan_incoming(self.incoming_dir)
            except Exception:
                logging.getLogger("sydeco-lightml.service").exception("incoming scanner error")
            self._stopping.wait(timeout=self.interval)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    service = CoreService(data_dir=args.data_dir, worker_mode=args.worker_mode)
    httpd = CoreHTTPServer((args.host, args.port), service, body_limit=args.body_limit)

    stopping = False

    def _shutdown(signum: int, _frame: object) -> None:
        nonlocal stopping
        if stopping:
            return
        stopping = True
        logging.getLogger("sydeco-lightml.service").info("received signal %s; stopping", signum)
        httpd.shutdown()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    watcher_thread = IncomingBundleWatcher(service=service)
    watcher_thread.start()

    logging.getLogger("sydeco-lightml.service").info(
        "serving host=%s port=%s data_dir=%s worker_mode=%s",
        args.host,
        args.port,
        args.data_dir,
        args.worker_mode,
    )
    try:
        httpd.serve_forever(poll_interval=0.2)
    finally:
        httpd.server_close()
        watcher_thread.join(timeout=1.0)
        for app_id in list(service._hosts):
            try:
                service.stop_app(app_id)
            except Exception:  # best-effort shutdown; systemd will own final cleanup
                logging.getLogger("sydeco-lightml.service").exception("failed to stop app %s", app_id)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
