"""PoC A adapter — text classifier (V2.1 proposal 7.1 / 4.2 / R1-R4).

Application-specific logic lives in the BUNDLE (R2/R3), never in Core.
Core loads the artifacts (E3: vectorizer -> model via depends_on) and
injects them via context.models; this adapter consumes them and
implements the stable 3.0 contract:
    initialize(context) / infer(request, context) / shutdown()
"""
from __future__ import annotations

import math
import re
from typing import Any, Dict


class Adapter:
    """Prototype text classifier (stdlib-only, log-odds model)."""

    def initialize(self, context: Dict[str, Any]) -> None:
        self._model = context["models"]["model"]  # {words, weights, bias}
        self._words = self._model["words"]
        self._w2i = {w: i for i, w in enumerate(self._words)}
        self._weights = self._model["weights"]
        self._bias = self._model["bias"]

    def infer(self, request: Dict[str, Any], context: Dict[str, Any]) -> Any:
        text = request["text"]
        tokens = re.findall(r"[a-z0-9']+", text.lower())
        score = self._bias
        for tok in tokens:
            idx = self._w2i.get(tok)
            if idx is not None:
                score += self._weights[idx]
        confidence = 1.0 / (1.0 + math.exp(-score))
        label = "positive" if confidence >= 0.5 else "negative"
        return {"label": label, "confidence": round(confidence, 4)}

    def shutdown(self) -> None:
        pass
