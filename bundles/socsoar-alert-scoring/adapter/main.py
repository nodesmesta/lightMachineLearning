from __future__ import annotations

import hashlib
import math
import os
import sys
import time
from typing import Any, Dict, List

from sydeco_lightml_core.adapter import Adapter

FEATURE_KEYS = [
    'pid_ratio', 'ppid_ratio', 'cmd_len', 'arg_count', 'token_entropy',
    'uid_class', 'start_tick_delta', 'mem_rss_ratio', 'cpu_tick_ratio',
    'fd_count', 'thread_count', 'privilege_flag'
]


class Adapter(Adapter):
    def initialize(self, context: Dict[str, Any]) -> None:
        self.model = context.get('models', {}).get('primary')
        if self.model is None:
            models = context.get('models', {})
            if models:
                self.model = next(iter(models.values()))
            else:
                raise RuntimeError('No primary model found in context')

    def infer(self, request: Dict[str, Any], context: Dict[str, Any]) -> Any:
        event_id = request.get('event_id', context.get('request_id', 'evt-default'))
        now_iso = request.get('timestamp') or time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())

        # Strict input validation preventing HTTP 500 crashes
        try:
            if not isinstance(request, dict):
                raise ValueError("Request body must be a JSON object")

            telemetry = request.get('telemetry')
            if telemetry is None:
                telemetry = request.get('features')

            if telemetry is None:
                raise ValueError("Missing required 'telemetry' or 'features' property")

            if isinstance(telemetry, dict):
                missing = [k for k in FEATURE_KEYS if k not in telemetry]
                if missing:
                    raise ValueError(f"Missing required telemetry feature keys: {missing}")
                vec = []
                for k in FEATURE_KEYS:
                    val = telemetry[k]
                    if isinstance(val, bool):
                        raise ValueError(f"Feature '{k}' cannot be boolean")
                    if not isinstance(val, (int, float)):
                        raise ValueError(f"Feature '{k}' must be numeric (int/float), got {type(val).__name__}")
                    if math.isnan(val) or math.isinf(val):
                        raise ValueError(f"Feature '{k}' contains non-finite number: {val}")
                    vec.append(float(val))
            elif isinstance(telemetry, list):
                if len(telemetry) != 12:
                    raise ValueError(f"Expected exactly 12 features, got {len(telemetry)}")
                vec = []
                for idx, val in enumerate(telemetry):
                    if isinstance(val, bool):
                        raise ValueError(f"Feature at index {idx} cannot be boolean")
                    if not isinstance(val, (int, float)):
                        raise ValueError(f"Feature at index {idx} must be numeric, got {type(val).__name__}")
                    if math.isnan(val) or math.isinf(val):
                        raise ValueError(f"Feature at index {idx} contains non-finite number: {val}")
                    vec.append(float(val))
            else:
                raise ValueError(f"Invalid telemetry payload format: expected object or array, got {type(telemetry).__name__}")

            if len(vec) != 12:
                raise ValueError(f"Expected exactly 12 features, got {len(vec)}")

        except (ValueError, TypeError) as exc:
            return {
                'status': 'error',
                'http_status': 400,
                'app_id': 'socsoar-alert-scoring',
                'inference_id': f'err-{str(event_id)[:8]}',
                'model_version': '1.0.0',
                'anomaly_score': None,
                'confidence': None,
                'receipt_hash': '',
                'error': str(exc),
                'timestamp': now_iso,
            }

        # Predict
        if hasattr(self.model, 'predict_proba'):
            preds = self.model.predict_proba([vec])
            row = preds[0]
            score = float(row[1]) if len(row) > 1 else float(row[0])
        elif hasattr(self.model, 'predict'):
            preds = self.model.predict([vec])
            score = float(preds[0])
        else:
            raise RuntimeError('Model not callable or missing predict')

        score = round(max(0.0, min(1.0, score)), 4)
        confidence = round(1.0 - abs(0.5 - score) * 0.2, 4)
        model_version = '1.0.0'
        receipt_hash = hashlib.sha256(
            f'{event_id}:{score}:{model_version}:{now_iso}'.encode('utf-8')
        ).hexdigest()

        return {
            'status': 'ok',
            'app_id': 'socsoar-alert-scoring',
            'inference_id': f'inf-{str(event_id)[:8]}',
            'model_version': model_version,
            'anomaly_score': score,
            'confidence': confidence,
            'receipt_hash': receipt_hash,
            'timestamp': now_iso,
        }

    def shutdown(self) -> None:
        self.model = None
