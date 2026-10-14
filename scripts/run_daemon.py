#!/usr/bin/env python3
"""
run_daemon.py — SYDECO LightML V2 Local Loopback Service Daemon.
Starts LightML HTTP Service on 127.0.0.1:8000 serving:
  - GET  /health
  - POST /v2/apps/socsoar-alert-scoring/invocations
  - POST /api/v1/apps/socsoar-alert-scoring/infer
"""
from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sydeco_lightml_core.core import CoreService
from sydeco_lightml_core.secrets import write_token

DATA_DIR = REPO_ROOT / "run_data"
DEV_PUB_KEY = REPO_ROOT / "devkeys/DEVELOPMENT_TEST_KEY_DO_NOT_USE_IN_PRODUCTION.pub.pem"
SOCSOAR_MANIFEST = REPO_ROOT / "bundles/socsoar-alert-scoring/manifest.json"
DEFAULT_TOKEN = "sydeco_lightml_sovereign_token_default"
PORT = int(os.environ.get("SYDECO_LIGHTML_PORT", "8000"))
HOST = "127.0.0.1"


def setup_service() -> CoreService:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Setup trusted public keys
    with open(DEV_PUB_KEY, "r", encoding="ascii") as fh:
        pub_pem = fh.read()
    with open(DATA_DIR / "trusted_keys.json", "w", encoding="utf-8") as fh:
        json.dump({"sydeco-test-key-v1": pub_pem}, fh)

    # 2. Instantiate CoreService
    service = CoreService(data_dir=str(DATA_DIR), worker_mode="inprocess")

    # 3. Install bundle if not registered or update
    app_id = "socsoar-alert-scoring"
    if service.registry.get(app_id) is None:
        ok, msg, _ = service.install_app(str(SOCSOAR_MANIFEST), app_root=str(SOCSOAR_MANIFEST.parent))
        if not ok:
            print(f"Failed to install {app_id}: {msg}", file=sys.stderr)
            sys.exit(1)
        print(f"Successfully installed {app_id}")

    # 4. Provision default bearer token
    write_token(str(DATA_DIR), app_id, DEFAULT_TOKEN)

    # 5. Start worker
    ok, msg, _ = service.start_app(app_id)
    if not ok and "already running" not in msg.lower():
        print(f"Failed to start {app_id}: {msg}", file=sys.stderr)
        sys.exit(1)
    print(f"Worker {app_id} is ACTIVE and ready")

    return service


def main() -> None:
    print(f"Starting SYDECO LightML V2 Daemon on {HOST}:{PORT}...")
    service = setup_service()

    def handle_sigterm(signum, frame):
        print("\nStopping SYDECO LightML V2 Daemon gracefully...")
        service.stop_app("socsoar-alert-scoring")
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGINT, handle_sigterm)

    print(f"Serving loopback at http://{HOST}:{PORT}")
    service.serve(host=HOST, port=PORT)


if __name__ == "__main__":
    main()
