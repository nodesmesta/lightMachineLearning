"""Generic local file ingestion for LightML applications.

This module owns Core-controlled temporary upload references. It is generic:
it knows app ids, byte limits, file references, and cleanup; it does not know
what a document, image, invoice, or PDF means.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import threading
import time
from typing import Any, Dict, Tuple

DEFAULT_INGEST_LIMIT = 5 * 1024 * 1024
HARD_INGEST_LIMIT = 10 * 1024 * 1024
DEFAULT_MAX_PENDING_REFS_PER_APP = 32
DEFAULT_MAX_PENDING_BYTES_PER_APP = DEFAULT_INGEST_LIMIT * 4
HARD_MAX_PENDING_REFS_PER_APP = 256
HARD_MAX_PENDING_BYTES_PER_APP = HARD_INGEST_LIMIT * 32
INGEST_TTL_SECONDS = 15 * 60
CHUNK_SIZE = 64 * 1024


def _rmtree_force(path: str) -> None:
    for root, dirs, files in os.walk(path):
        try:
            os.chmod(root, 0o700)
        except OSError:
            pass
        for name in dirs:
            try:
                os.chmod(os.path.join(root, name), 0o700)
            except OSError:
                pass
        for name in files:
            try:
                os.chmod(os.path.join(root, name), 0o600)
            except OSError:
                pass

    def _make_writable(func: Any, failed_path: str, _exc_info: Any) -> None:
        try:
            os.chmod(failed_path, 0o700)
            func(failed_path)
        except Exception:
            pass

    shutil.rmtree(path, ignore_errors=False, onerror=_make_writable)


class IngestError(Exception):
    """Controlled ingest/reference error."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class IngressStore:
    """App-scoped temporary local file reference store."""

    def __init__(self, data_dir: str) -> None:
        self.data_dir = os.path.realpath(os.path.abspath(data_dir))
        self._locks_guard = threading.Lock()
        self._app_locks: Dict[str, threading.RLock] = {}

    def _app_lock(self, app_id: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._app_locks.get(app_id)
            if lock is None:
                lock = threading.RLock()
                self._app_locks[app_id] = lock
            return lock

    def _app_root(self, app_id: str) -> str:
        return os.path.join(self.data_dir, "apps", app_id, "ingress")

    def _safe_dir(self, app_id: str, file_ref: str) -> str:
        root = os.path.realpath(os.path.abspath(self._app_root(app_id)))
        path = os.path.realpath(os.path.abspath(os.path.join(root, file_ref)))
        if os.path.commonpath([root, path]) != root:
            raise IngestError(400, "invalid file reference")
        return path

    @staticmethod
    def limit_from_manifest(manifest: Dict[str, Any]) -> int:
        raw = manifest.get("limits", {}).get("max_ingest_bytes", DEFAULT_INGEST_LIMIT)
        try:
            limit = int(raw)
        except (TypeError, ValueError):
            limit = DEFAULT_INGEST_LIMIT
        if limit < 1:
            limit = DEFAULT_INGEST_LIMIT
        return min(limit, HARD_INGEST_LIMIT)

    @staticmethod
    def pending_ref_limit_from_manifest(manifest: Dict[str, Any]) -> int:
        raw = manifest.get("limits", {}).get(
            "max_pending_refs_per_app", DEFAULT_MAX_PENDING_REFS_PER_APP
        )
        try:
            limit = int(raw)
        except (TypeError, ValueError):
            limit = DEFAULT_MAX_PENDING_REFS_PER_APP
        if limit < 1:
            limit = DEFAULT_MAX_PENDING_REFS_PER_APP
        return min(limit, HARD_MAX_PENDING_REFS_PER_APP)

    @staticmethod
    def pending_bytes_limit_from_manifest(manifest: Dict[str, Any]) -> int:
        raw = manifest.get("limits", {}).get(
            "max_pending_ingest_bytes_per_app", DEFAULT_MAX_PENDING_BYTES_PER_APP
        )
        try:
            limit = int(raw)
        except (TypeError, ValueError):
            limit = DEFAULT_MAX_PENDING_BYTES_PER_APP
        if limit < 1:
            limit = DEFAULT_MAX_PENDING_BYTES_PER_APP
        return min(limit, HARD_MAX_PENDING_BYTES_PER_APP)

    def pending_usage(self, app_id: str) -> Dict[str, int]:
        with self._app_lock(app_id):
            return self._pending_usage_unlocked(app_id)

    def _pending_usage_unlocked(self, app_id: str) -> Dict[str, int]:
        root = self._app_root(app_id)
        usage = {"pending_refs": 0, "pending_bytes": 0}
        if not os.path.isdir(root):
            return usage
        for name in os.listdir(root):
            path = os.path.join(root, name)
            meta = os.path.join(path, "metadata.json")
            if not os.path.isdir(path) or not os.path.isfile(meta):
                continue
            try:
                with open(meta, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if data.get("app_id") != app_id:
                    continue
                usage["pending_refs"] += 1
                usage["pending_bytes"] += max(0, int(data.get("size_bytes", 0)))
            except Exception:
                continue
        return usage

    def cleanup_expired(self, app_id: str, now: float | None = None) -> None:
        with self._app_lock(app_id):
            self._cleanup_expired_unlocked(app_id, now)

    def _cleanup_expired_unlocked(self, app_id: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        root = self._app_root(app_id)
        if not os.path.isdir(root):
            return
        for name in os.listdir(root):
            path = os.path.join(root, name)
            meta = os.path.join(path, "metadata.json")
            if not os.path.isdir(path) or not os.path.isfile(meta):
                continue
            try:
                with open(meta, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if float(data.get("expires_at_epoch", 0)) <= now:
                    shutil.rmtree(path, ignore_errors=True)
            except Exception:
                shutil.rmtree(path, ignore_errors=True)

    def create(
        self,
        app_id: str,
        manifest: Dict[str, Any],
        rfile: Any,
        content_length: int,
        filename: str,
        content_type: str,
    ) -> Dict[str, Any]:
        if content_length <= 0:
            raise IngestError(400, "empty upload")
        limit = self.limit_from_manifest(manifest)
        if content_length > limit:
            raise IngestError(413, "file exceeds upload size limit")
        with self._app_lock(app_id):
            self._cleanup_expired_unlocked(app_id)
            usage = self._pending_usage_unlocked(app_id)
            ref_limit = self.pending_ref_limit_from_manifest(manifest)
            byte_limit = self.pending_bytes_limit_from_manifest(manifest)
            if usage["pending_refs"] >= ref_limit:
                raise IngestError(429, "pending file reference limit exceeded")
            if usage["pending_bytes"] + content_length > byte_limit:
                raise IngestError(413, "pending ingress byte limit exceeded")
            root = self._app_root(app_id)
            os.makedirs(root, exist_ok=True)
            file_ref = secrets.token_urlsafe(24)
            ref_dir = self._safe_dir(app_id, file_ref)
            os.makedirs(ref_dir, mode=0o700, exist_ok=False)
            payload_path = os.path.join(ref_dir, "payload.bin")
            remaining = content_length
            written = 0
            try:
                with open(payload_path, "wb") as out:
                    while remaining > 0:
                        chunk = rfile.read(min(CHUNK_SIZE, remaining))
                        if not chunk:
                            raise IngestError(400, "incomplete upload body")
                        out.write(chunk)
                        written += len(chunk)
                        remaining -= len(chunk)
                expires = int(time.time() + INGEST_TTL_SECONDS)
                meta = {
                    "app_id": app_id,
                    "file_ref": file_ref,
                    "filename": os.path.basename(filename or "upload.bin"),
                    "content_type": content_type or "application/octet-stream",
                    "size_bytes": written,
                    "path": payload_path,
                    "expires_at_epoch": expires,
                    "created_at_epoch": int(time.time()),
                }
                with open(os.path.join(ref_dir, "metadata.json"), "w", encoding="utf-8") as fh:
                    json.dump(meta, fh, indent=2, sort_keys=True)
                return dict(meta)
            except Exception:
                shutil.rmtree(ref_dir, ignore_errors=True)
                raise

    def resolve(self, app_id: str, file_ref: str) -> Dict[str, Any]:
        if not isinstance(file_ref, str) or not file_ref:
            raise IngestError(400, "invalid file reference")
        ref_dir = self._safe_dir(app_id, file_ref)
        meta_path = os.path.join(ref_dir, "metadata.json")
        if not os.path.isfile(meta_path):
            raise IngestError(404, "file reference not found")
        try:
            with open(meta_path, "r", encoding="utf-8") as fh:
                meta = json.load(fh)
        except Exception:
            raise IngestError(404, "file reference not found")
        if meta.get("app_id") != app_id:
            raise IngestError(404, "file reference not found")
        if float(meta.get("expires_at_epoch", 0)) <= time.time():
            self.consume(app_id, file_ref)
            raise IngestError(404, "file reference expired")
        payload = os.path.realpath(os.path.abspath(meta.get("path", "")))
        if os.path.commonpath([ref_dir, payload]) != ref_dir:
            raise IngestError(400, "invalid file reference")
        if not os.path.isfile(payload):
            raise IngestError(404, "file reference not found")
        return {
            "ref": file_ref,
            "path": payload,
            "filename": meta.get("filename", "upload.bin"),
            "content_type": meta.get("content_type", "application/octet-stream"),
            "size_bytes": int(meta.get("size_bytes", 0)),
            "expires_at_epoch": int(meta.get("expires_at_epoch", 0)),
        }

    def claim(self, app_id: str, file_ref: str) -> Dict[str, Any]:
        """Atomically claim a one-shot file reference.

        A successful claim renames the available reference directory before
        returning the payload path, so simultaneous consumers cannot both
        resolve the same reference. The returned ``claim_ref`` must be passed
        to ``consume()`` for deterministic cleanup after the request path exits.
        """
        if not isinstance(file_ref, str) or not file_ref:
            raise IngestError(400, "invalid file reference")
        ref_dir = self._safe_dir(app_id, file_ref)
        claim_ref = file_ref + ".claimed-" + secrets.token_urlsafe(8)
        claim_dir = self._safe_dir(app_id, claim_ref)
        try:
            os.rename(ref_dir, claim_dir)
        except FileNotFoundError:
            raise IngestError(404, "file reference not found")
        except OSError:
            raise IngestError(404, "file reference not found")

        meta_path = os.path.join(claim_dir, "metadata.json")
        try:
            with open(meta_path, "r", encoding="utf-8") as fh:
                meta = json.load(fh)
        except Exception:
            shutil.rmtree(claim_dir, ignore_errors=True)
            raise IngestError(404, "file reference not found")
        if meta.get("app_id") != app_id:
            shutil.rmtree(claim_dir, ignore_errors=True)
            raise IngestError(404, "file reference not found")
        if float(meta.get("expires_at_epoch", 0)) <= time.time():
            shutil.rmtree(claim_dir, ignore_errors=True)
            raise IngestError(404, "file reference expired")
        payload = os.path.realpath(os.path.abspath(os.path.join(claim_dir, "payload.bin")))
        if os.path.commonpath([claim_dir, payload]) != claim_dir:
            shutil.rmtree(claim_dir, ignore_errors=True)
            raise IngestError(400, "invalid file reference")
        if not os.path.isfile(payload):
            shutil.rmtree(claim_dir, ignore_errors=True)
            raise IngestError(404, "file reference not found")
        # Production systemd workers run as a dedicated capability user. Core
        # owns the one-shot reference lifecycle, but after claim the worker must
        # be able to traverse the claimed directory and read the payload path
        # passed in the generic request. The path remains unguessable,
        # app-scoped, and is consumed in the server finally block.
        os.chmod(claim_dir, 0o555)
        os.chmod(payload, 0o444)
        return {
            "ref": file_ref,
            "claim_ref": claim_ref,
            "path": payload,
            "filename": meta.get("filename", "upload.bin"),
            "content_type": meta.get("content_type", "application/octet-stream"),
            "size_bytes": int(meta.get("size_bytes", 0)),
            "expires_at_epoch": int(meta.get("expires_at_epoch", 0)),
        }

    def consume(self, app_id: str, file_ref: str) -> None:
        try:
            path = self._safe_dir(app_id, file_ref)
            if os.path.exists(path):
                _rmtree_force(path)
        except IngestError:
            return
        except OSError:
            return
