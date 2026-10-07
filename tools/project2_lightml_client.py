#!/usr/bin/env python3
"""
project2_lightml_client.py — Standalone Zero-Dependency LightML V2 Client Connector.

Pure Python 3 Standard Library implementation conforming strictly to
PROJECT2_LIGHTML_INTEGRATION_CONTRACT_V1.md.
Connects Project 2 telemetry ingestion to LightML V2 endpoint-scoring service.
Implements bounded timeout enforcement and non-blocking fail-safe fallback.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import math
import os
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple


class Project2LightMLClient:
    """Sovereign air-gapped client for LightML V2 endpoint anomaly scoring."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8000,
        token: Optional[str] = None,
        timeout_ms: float = 5.0,
    ) -> None:
        self.host = host
        self.port = port
        self.token = token or os.environ.get("LIGHTML_BEARER_TOKEN", "")
        self.timeout_sec = max(0.001, timeout_ms / 1000.0)

    def score_process_telemetry(
        self,
        trace_id: str,
        features: List[float],
        process_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Query local LightML runtime for advisory endpoint anomaly scoring.

        Returns structured dictionary. NEVER raises exceptions to the caller;
        traps all socket, HTTP, JSON, and timeout errors, returning a
        standardized fail-safe fallback envelope ('status': 'unavailable').
        """
        start_time = time.perf_counter()

        # Feature vector validation
        if not isinstance(features, (list, tuple)) or len(features) != 12:
            return {
                "inference_id": str(uuid.uuid4()),
                "model_version": "unknown",
                "anomaly_score": 0.0,
                "confidence": 0.0,
                "status": "unavailable",
                "latency_ms": round((time.perf_counter() - start_time) * 1000.0, 3),
                "receipt_hash": "",
                "error": f"Invalid feature dimensions: expected 12, got {len(features) if isinstance(features, list) else type(features)}",
            }

        payload_dict = {
            "trace_id": trace_id,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "process_context": process_context or {},
            "features": [float(x) for x in features],
        }

        body_bytes = json.dumps(payload_dict, sort_keys=True).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(body_bytes)),
            "User-Agent": "SYDECO-Project2-LightML-Connector/1.0",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        conn: Optional[http.client.HTTPConnection] = None
        try:
            conn = http.client.HTTPConnection(
                self.host, self.port, timeout=self.timeout_sec
            )
            conn.request(
                "POST",
                "/v2/apps/project2-endpoint-scoring/invocations",
                body=body_bytes,
                headers=headers,
            )
            response = conn.getresponse()
            raw_reply = response.read()

            elapsed_ms = round((time.perf_counter() - start_time) * 1000.0, 3)

            if response.status == 200:
                reply_data = json.loads(raw_reply.decode("utf-8"))
                # Handle standard Core envelope wrap or direct result
                if "result" in reply_data and isinstance(reply_data["result"], list):
                    # DefaultModelAdapter returns {"result": [[prob_norm, prob_anom]], ...}
                    res_row = reply_data["result"][0]
                    score = float(res_row[1]) if isinstance(res_row, list) and len(res_row) > 1 else float(res_row)
                    receipt_hash = hashlib.sha256(f"{trace_id}:{score}:1.0.0".encode("utf-8")).hexdigest()
                    return {
                        "inference_id": reply_data.get("inference_id", str(uuid.uuid4())),
                        "model_version": reply_data.get("model_version", "1.0.0"),
                        "anomaly_score": round(score, 4),
                        "confidence": round(1.0 - abs(0.5 - score) * 0.2, 4),
                        "status": "ok",
                        "latency_ms": elapsed_ms,
                        "receipt_hash": receipt_hash,
                    }
                elif "anomaly_score" in reply_data:
                    return {
                        "inference_id": reply_data.get("inference_id", str(uuid.uuid4())),
                        "model_version": reply_data.get("model_version", "1.0.0"),
                        "anomaly_score": float(reply_data["anomaly_score"]),
                        "confidence": float(reply_data.get("confidence", 0.95)),
                        "status": reply_data.get("status", "ok"),
                        "latency_ms": elapsed_ms,
                        "receipt_hash": reply_data.get("receipt_hash", ""),
                    }
                else:
                    return {
                        "inference_id": str(uuid.uuid4()),
                        "model_version": "1.0.0",
                        "anomaly_score": 0.0,
                        "confidence": 0.0,
                        "status": "degraded",
                        "latency_ms": elapsed_ms,
                        "receipt_hash": "",
                        "error": f"Unexpected response schema: {reply_data}",
                    }
            else:
                return {
                    "inference_id": str(uuid.uuid4()),
                    "model_version": "unknown",
                    "anomaly_score": 0.0,
                    "confidence": 0.0,
                    "status": "unavailable",
                    "latency_ms": elapsed_ms,
                    "receipt_hash": "",
                    "error": f"HTTP {response.status}: {raw_reply.decode('utf-8', errors='replace')}",
                }

        except Exception as exc:
            elapsed_ms = round((time.perf_counter() - start_time) * 1000.0, 3)
            return {
                "inference_id": str(uuid.uuid4()),
                "model_version": "unknown",
                "anomaly_score": 0.0,
                "confidence": 0.0,
                "status": "unavailable",
                "latency_ms": elapsed_ms,
                "receipt_hash": "",
                "error": f"{type(exc).__name__}: {exc}",
            }
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass


# ==============================================================================
# Helper Utilities for Project 2 Integration
# ==============================================================================

def calculate_shannon_entropy(text: str) -> float:
    """Compute Shannon entropy of a string."""
    if not text:
        return 0.0
    freq: Dict[str, int] = {}
    for char in text:
        freq[char] = freq.get(char, 0) + 1
    total = len(text)
    ent = 0.0
    for count in freq.values():
        p = count / total
        ent -= p * math.log2(p)
    return ent


def extract_telemetry_features(
    pid: int = 1000,
    ppid: int = 1,
    cmdline: str = "/bin/bash",
    uid: int = 1000,
    start_ticks: int = 100,
    rss_kb: int = 4096,
    cpu_ticks: int = 10,
    open_fds: int = 12,
    threads: int = 1,
    is_privileged: bool = False,
) -> List[float]:
    """Convert raw Linux process attributes into normalized 12-feature vector."""
    pid_r = min(1.0, pid / 65535.0)
    ppid_r = min(1.0, ppid / 65535.0)
    cmd_l = min(1.0, math.log1p(len(cmdline)) / 10.0)
    args = cmdline.strip().split()
    arg_c = min(1.0, len(args) / 50.0)
    tok_e = min(1.0, calculate_shannon_entropy(cmdline) / 8.0)
    uid_c = 0.0 if uid == 0 else 1.0
    st_delta = min(1.0, start_ticks / 10000.0)
    rss_r = min(1.0, (rss_kb * 1024) / (16 * 1024 * 1024 * 1024))  # normalized against 16GB
    cpu_r = min(1.0, cpu_ticks / 1000.0)
    fd_c = min(1.0, open_fds / 1024.0)
    thr_c = min(1.0, threads / 100.0)
    priv_f = 1.0 if is_privileged else 0.0

    return [
        round(pid_r, 4),
        round(ppid_r, 4),
        round(cmd_l, 4),
        round(arg_c, 4),
        round(tok_e, 4),
        round(uid_c, 4),
        round(st_delta, 4),
        round(rss_r, 4),
        round(cpu_r, 4),
        round(fd_c, 4),
        round(thr_c, 4),
        round(priv_f, 4),
    ]


def evaluate_advisory_policy(
    anomaly_score: float, default_mode: str = "allow"
) -> Tuple[str, str]:
    """Apply advisory decision logic to augment Project 2 policy evaluation.

    Returns (target_mode, risk_level).
    - If anomaly_score > 0.75: escalates to ('require_review', 'high').
    - If anomaly_score <= 0.75: retains (default_mode, 'low').
    """
    if anomaly_score > 0.75:
        return "require_review", "high"
    return default_mode, "low"


if __name__ == "__main__":
    print("Testing Project2LightMLClient offline fallback...")
    client = Project2LightMLClient(host="127.0.0.1", port=59999, timeout_ms=5)
    sample_feat = extract_telemetry_features(
        pid=1234, ppid=100, cmdline="/bin/ls -la", uid=1000
    )
    result = client.score_process_telemetry(
        trace_id=str(uuid.uuid4()), features=sample_feat
    )
    print("Fallback response when service stopped:")
    print(json.dumps(result, indent=2))
    assert result["status"] == "unavailable"
    assert result["anomaly_score"] == 0.0
    print("Safe fallback verification: PASS")
