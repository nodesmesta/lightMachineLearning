"""PoC C adapter — document triage pipeline (V2.1 proposal 7.3 / R9).

The full application workflow lives HERE, in the bundle, never in Core
(R2/R3): input text -> preprocessing/normalization -> topic model ->
decision/rule (confidence < 0.5 -> route "needs review") -> sentiment
model -> post-processing/aggregation -> structured result
{topic, sentiment, urgency, confidence} matching output_schema.

Models are stdlib-only prototype artifacts (plain dicts, pickled):
  vectorizer : vocabulary {word: index}        (role vectorizer)
  topic      : per-topic keyword weights       (role topic, depends_on
               vectorizer — E3 dependency resolution exercised)
  sentiment  : log-odds weights + bias         (role sentiment)
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

_WORD_RE = re.compile(r"[a-z0-9']+")


class Adapter:
    """Prototype document-triage pipeline (workflow-style PoC)."""

    def initialize(self, context: Dict[str, Any]) -> None:
        self._vectorizer = context["models"]["vectorizer"]  # {"vocab": {...}}
        self._topic = context["models"]["topic"]            # {"topics": {...}}
        self._sentiment = context["models"]["sentiment"]    # {"weights", "bias"}

    def infer(self, request: Dict[str, Any], context: Dict[str, Any]) -> Any:
        text = request["text"]

        # 1) preprocessing / normalization (length + tokenization)
        if len(text) > 100000:
            raise ValueError("text exceeds maxLength 100000")
        tokens = self._normalize(text)

        # 2) topic model (keywords scored against the vocabulary)
        topic_label, topic_conf = self._predict_topic(tokens)

        # 3) sentiment model (always computed, used by rule + aggregation)
        sentiment_label, senti_conf = self._predict_sentiment(tokens)

        # 4) decision / rule: low confidence -> route "needs review"
        if topic_conf < self._topic["threshold"]:
            topic_label = "needs_review"
            urgency = "review"
        else:
            urgency = (
                "high"
                if sentiment_label == "negative" and topic_conf >= 0.75
                else "low"
            )

        # 5) post-processing / aggregation -> structured result
        confidence = round(min(topic_conf, senti_conf), 4)
        return {
            "topic": topic_label,
            "sentiment": sentiment_label,
            "urgency": urgency,
            "confidence": confidence,
        }

    # ---- pipeline internals (all application logic, stdlib-only) ------

    @staticmethod
    def _normalize(text: str) -> List[str]:
        """Lowercase, strip non-alphanumerics, split into tokens."""
        return _WORD_RE.findall(text.lower())

    def _predict_topic(self, tokens: List[str]) -> tuple:
        vocab = self._vectorizer.get("vocab", {})
        filtered = [t for t in tokens if t in vocab]
        topics = self._topic.get("topics", {})
        bias = float(self._topic.get("default_bias", 0.2))
        scores: Dict[str, float] = {}
        for topic_name, weights in topics.items():
            score = bias
            for token in filtered:
                score += float(weights.get(token, 0.0))
            scores[topic_name] = score
        best = max(scores, key=lambda k: scores[k])
        score = scores[best]
        # logistic-ish confidence: score / (score + 1) in [0, 1)
        confidence = round(score / (score + 1.0), 4)
        return best, confidence

    def _predict_sentiment(self, tokens: List[str]) -> tuple:
        vocab = self._vectorizer.get("vocab", {})
        filtered = [t for t in tokens if t in vocab]
        weights = self._sentiment.get("weights", {})
        bias = float(self._sentiment.get("bias", 0.0))
        score = bias + sum(float(weights.get(t, 0.0)) for t in filtered)
        probability = 1.0 / (1.0 + 2.718281828459045 ** (-score))
        label = "positive" if probability >= 0.5 else "negative"
        confidence = round(probability if label == "positive" else 1.0 - probability, 4)
        return label, confidence

    def shutdown(self) -> None:
        pass
