"""CLI skeleton (V2.1 proposal 5.3 + 3.1 / N1 + N4).

Root CLI, non-interactive, structured --json output; management
operations serialized by an exclusive lock (N4).

Day 1 commands:
  sydeco-lightml install <manifest.json> [--root <app_root>] [--json]
  sydeco-lightml list [--json]
  sydeco-lightml status <app_id> [--json]
  sydeco-lightml show <app_id> [--json]      (full registry entry)

Day 2 commands:
  sydeco-lightml start <app_id> [--json]     (start one app's worker)
  sydeco-lightml stop <app_id> [--json]
  sydeco-lightml crash <app_id> [--json]     (DEV-ONLY test hook: isolation)
  sydeco-lightml serve [--host] [--port]     (HTTP surface, 5.1)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, Optional

from .core import CoreService


class ExclusiveLock:
    """N4: exclusive lock for management operations (mkdir-based)."""

    def __init__(self, lock_path: str) -> None:
        self.lock_path = lock_path

    def __enter__(self) -> "ExclusiveLock":
        try:
            os.mkdir(self.lock_path)
        except FileExistsError:
            raise RuntimeError(
                f"another management operation holds the lock: {self.lock_path}"
            )
        except OSError as exc:
            raise RuntimeError(f"cannot acquire lock {self.lock_path}: {exc}")
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            os.rmdir(self.lock_path)
        except OSError:
            pass


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True))


def cmd_install(service: CoreService, args: argparse.Namespace) -> int:
    lock = ExclusiveLock(os.path.join(service.data_dir, "lock"))
    with lock:
        ok, message, entry = service.install_app(args.manifest, args.root)
        if args.json:
            _print_json(
                {
                    "ok": ok,
                    "message": message,
                    "entry": entry,
                }
            )
        else:
            print(message)
        return 0 if ok else 1


def cmd_list(service: CoreService, args: argparse.Namespace) -> int:
    apps = service.registry.list_apps()
    if args.json:
        _print_json({"apps": apps})
    else:
        for app in apps:
            print(
                f"{app['app_id']:<32} {app['status']:<10} "
                f"v{app['active_version']}"
            )
    return 0


def cmd_status(service: CoreService, args: argparse.Namespace) -> int:
    entry = service.registry.get(args.app_id)
    if entry is None:
        if args.json:
            _print_json({"app_id": args.app_id, "registered": False})
        else:
            print(f"app not registered: {args.app_id}")
        return 1
    status = {
        "app_id": entry.get("app_id"),
        "registered": True,
        "status": entry.get("status"),
        "active_version": entry.get("active_version"),
        "installed_at": entry.get("installed_at"),
        "readiness": service.readiness.status(args.app_id),
    }
    if args.json:
        _print_json(status)
    else:
        print(
            f"{status['app_id']}: {status['status']} (active v"
            f"{status['active_version']}), readiness={status['readiness']}"
        )
    return 0


def cmd_show(service: CoreService, args: argparse.Namespace) -> int:
    entry = service.registry.get(args.app_id)
    if entry is None:
        if args.json:
            _print_json({"app_id": args.app_id, "registered": False})
        else:
            print(f"app not registered: {args.app_id}")
        return 1
    if args.json:
        _print_json(entry)
    else:
        print(json.dumps(entry, indent=2, sort_keys=True))
    return 0


def _run_action(service: CoreService, args: argparse.Namespace, action: str) -> int:
    fn = {
        "start": service.start_app,
        "stop": service.stop_app,
        "crash": service.crash_app,
    }[action]
    ok, message, data = fn(args.app_id)
    if args.json:
        out = {"ok": ok, "message": message}
        if data:
            out.update(data)
        _print_json(out)
    else:
        print(message)
    return 0 if ok else 1


def cmd_start(service: CoreService, args: argparse.Namespace) -> int:
    return _run_action(service, args, "start")


def cmd_stop(service: CoreService, args: argparse.Namespace) -> int:
    return _run_action(service, args, "stop")


def cmd_crash(service: CoreService, args: argparse.Namespace) -> int:
    return _run_action(service, args, "crash")


def cmd_serve(service: CoreService, args: argparse.Namespace) -> int:
    service.serve(host=args.host, port=args.port)
    return 0  # unreachable while serving


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sydeco-lightml",
        description="SYDECO LightML Universal Runtime V2 - Core CLI (Day 1)",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="override SYDECO_LIGHTML_DATA_DIR (dev registry/audit location)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_install = sub.add_parser("install", help="install/register an app")
    p_install.add_argument("manifest", help="path to manifest.json")
    p_install.add_argument("--root", default=None, help="app root dir (default: manifest dir)")
    p_install.add_argument("--json", action="store_true")
    p_install.set_defaults(func=cmd_install)

    p_list = sub.add_parser("list", help="list registered apps")
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=cmd_list)

    p_status = sub.add_parser("status", help="status of one app")
    p_status.add_argument("app_id")
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=cmd_status)

    p_show = sub.add_parser("show", help="full registry entry of one app")
    p_show.add_argument("app_id")
    p_show.add_argument("--json", action="store_true")
    p_show.set_defaults(func=cmd_show)

    p_start = sub.add_parser("start", help="start one app's worker")
    p_start.add_argument("app_id")
    p_start.add_argument("--json", action="store_true")
    p_start.set_defaults(func=cmd_start)

    p_stop = sub.add_parser("stop", help="stop one app's worker")
    p_stop.add_argument("app_id")
    p_stop.add_argument("--json", action="store_true")
    p_stop.set_defaults(func=cmd_stop)

    p_crash = sub.add_parser(
        "crash", help="DEV-ONLY test hook: simulate a worker crash (isolation test)"
    )
    p_crash.add_argument("app_id")
    p_crash.add_argument("--json", action="store_true")
    p_crash.set_defaults(func=cmd_crash)

    p_serve = sub.add_parser("serve", help="start the HTTP surface (5.1)")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", default=None, type=int)
    p_serve.set_defaults(func=cmd_serve)
    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.data_dir:
        os.environ["SYDECO_LIGHTML_DATA_DIR"] = args.data_dir
    service = CoreService(data_dir=args.data_dir)
    return args.func(service, args)


if __name__ == "__main__":
    sys.exit(main())
