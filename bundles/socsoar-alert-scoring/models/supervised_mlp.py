#!/usr/bin/env python3
"""Dependency-free supervised MLP for the 12-feature LightML contract."""
from __future__ import annotations

import math
import random
from typing import Any, Sequence


class SupervisedMLP:
    """Small deterministic binary classifier with ``predict_proba`` compatibility."""

    FEATURE_COUNT = 12

    def __init__(
        self,
        input_dim: int = FEATURE_COUNT,
        hidden_dims: tuple[int, int] = (32, 16),
        *,
        seed: int = 42,
        epochs: int = 400,
        learning_rate: float = 0.03,
        l2: float = 1e-4,
        patience: int = 50,
    ) -> None:
        if input_dim != self.FEATURE_COUNT:
            raise ValueError(f"input_dim must be {self.FEATURE_COUNT}")
        if len(hidden_dims) != 2 or any(width < 1 for width in hidden_dims):
            raise ValueError("hidden_dims must contain two positive widths")
        if epochs < 1 or learning_rate <= 0.0 or l2 < 0.0 or patience < 1:
            raise ValueError("invalid training hyperparameters")
        self.input_dim = input_dim
        self.hidden_dims = tuple(hidden_dims)
        self.seed = seed
        self.epochs = epochs
        self.learning_rate = learning_rate
        self.l2 = l2
        self.patience = patience
        self.feature_names_in_ = [
            "pid_ratio", "ppid_ratio", "cmd_len", "arg_count", "token_entropy",
            "uid_class", "start_tick_delta", "mem_rss_ratio", "cpu_tick_ratio",
            "fd_count", "thread_count", "privilege_flag",
        ]
        self.n_features_in_ = input_dim
        self.means_: list[float] = []
        self.scales_: list[float] = []
        self.weights_: list[list[list[float]]] = []
        self.biases_: list[list[float]] = []
        self.training_epochs_: int = 0
        self.fitted_ = False

    @staticmethod
    def _validate_rows(rows: Any, expected_dim: int) -> list[list[float]]:
        if not isinstance(rows, (list, tuple)) or not rows:
            raise ValueError("X must be a non-empty sequence of rows")
        result: list[list[float]] = []
        for row_index, row in enumerate(rows):
            if not isinstance(row, (list, tuple)) or len(row) != expected_dim:
                raise ValueError(
                    f"row {row_index} must contain exactly {expected_dim} features"
                )
            converted = []
            for feature_index, value in enumerate(row):
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(f"feature {feature_index} must be numeric")
                number = float(value)
                if not math.isfinite(number):
                    raise ValueError(f"feature {feature_index} must be finite")
                converted.append(number)
            result.append(converted)
        return result

    @staticmethod
    def _sigmoid(value: float) -> float:
        if value >= 0.0:
            e = math.exp(-value)
            return 1.0 / (1.0 + e)
        e = math.exp(value)
        return e / (1.0 + e)

    def _standardize(self, rows: list[list[float]]) -> list[list[float]]:
        if not self.means_ or not self.scales_:
            raise RuntimeError("model is not fitted")
        return [
            [(value - self.means_[index]) / self.scales_[index] for index, value in enumerate(row)]
            for row in rows
        ]

    def _forward(self, row: list[float]) -> tuple[list[list[float]], list[list[float]]]:
        activations = [row]
        preactivations: list[list[float]] = []
        current = row
        for layer_index, (weights, biases) in enumerate(zip(self.weights_, self.biases_)):
            z = [
                sum(weights[unit][feature] * current[feature] for feature in range(len(current)))
                + biases[unit]
                for unit in range(len(weights))
            ]
            preactivations.append(z)
            if layer_index == len(self.weights_) - 1:
                current = [self._sigmoid(z[0])]
            else:
                current = [max(0.0, value) for value in z]
            activations.append(current)
        return activations, preactivations

    def fit(self, X: Sequence[Sequence[float]], y: Sequence[int]) -> "SupervisedMLP":
        rows = self._validate_rows(list(X), self.input_dim)
        labels = list(y)
        if len(rows) != len(labels) or not labels:
            raise ValueError("X and y must have equal non-zero length")
        if any(label not in (0, 1) for label in labels):
            raise ValueError("labels must contain only 0 or 1")
        if not any(label == 0 for label in labels) or not any(label == 1 for label in labels):
            raise ValueError("training data must contain both classes")

        self.means_ = [sum(row[index] for row in rows) / len(rows) for index in range(self.input_dim)]
        variances = [
            sum((row[index] - self.means_[index]) ** 2 for row in rows) / len(rows)
            for index in range(self.input_dim)
        ]
        self.scales_ = [math.sqrt(value) if value > 1e-12 else 1.0 for value in variances]
        standardized = self._standardize(rows)

        layer_sizes = [self.input_dim, *self.hidden_dims, 1]
        rng = random.Random(self.seed)
        self.weights_ = []
        self.biases_ = []
        for input_size, output_size in zip(layer_sizes, layer_sizes[1:]):
            limit = math.sqrt(6.0 / (input_size + output_size))
            self.weights_.append([
                [rng.uniform(-limit, limit) for _ in range(input_size)]
                for _ in range(output_size)
            ])
            self.biases_.append([0.0 for _ in range(output_size)])

        positive = sum(labels)
        negative = len(labels) - positive
        class_weights = {0: len(labels) / (2.0 * negative), 1: len(labels) / (2.0 * positive)}
        best_loss = float("inf")
        stale = 0
        for epoch in range(self.epochs):
            gradients_w = [
                [[0.0 for _ in row] for row in layer]
                for layer in self.weights_
            ]
            gradients_b = [[0.0 for _ in layer] for layer in self.biases_]
            loss = 0.0
            for row, label in zip(standardized, labels):
                activations, preactivations = self._forward(row)
                probability = min(max(activations[-1][0], 1e-12), 1.0 - 1e-12)
                weight = class_weights[label]
                loss -= weight * (label * math.log(probability) + (1 - label) * math.log(1 - probability))
                delta = [weight * (probability - label)]
                for layer_index in range(len(self.weights_) - 1, -1, -1):
                    previous = activations[layer_index]
                    for unit in range(len(delta)):
                        gradients_b[layer_index][unit] += delta[unit]
                        for feature in range(len(previous)):
                            gradients_w[layer_index][unit][feature] += delta[unit] * previous[feature]
                    if layer_index > 0:
                        propagated = []
                        for feature in range(len(previous)):
                            value = sum(
                                self.weights_[layer_index][unit][feature] * delta[unit]
                                for unit in range(len(delta))
                            )
                            propagated.append(value if preactivations[layer_index - 1][feature] > 0.0 else 0.0)
                        delta = propagated
            normalizer = float(len(rows))
            loss /= normalizer
            for layer_index, layer in enumerate(self.weights_):
                for unit, unit_weights in enumerate(layer):
                    for feature in range(len(unit_weights)):
                        gradient = gradients_w[layer_index][unit][feature] / normalizer
                        gradient += self.l2 * unit_weights[feature]
                        unit_weights[feature] -= self.learning_rate * gradient
                    self.biases_[layer_index][unit] -= self.learning_rate * gradients_b[layer_index][unit] / normalizer
            loss += 0.5 * self.l2 * sum(
                weight * weight
                for layer in self.weights_
                for unit in layer
                for weight in unit
            )
            self.training_epochs_ = epoch + 1
            if loss + 1e-10 < best_loss:
                best_loss = loss
                stale = 0
            else:
                stale += 1
                if stale >= self.patience:
                    break
        self.fitted_ = True
        return self

    def predict_proba(self, X: Any) -> list[list[float]]:
        rows = self._validate_rows(X, self.input_dim)
        standardized = self._standardize(rows)
        results = []
        for row in standardized:
            activations, _ = self._forward(row)
            probability = min(max(float(activations[-1][0]), 0.0), 1.0)
            results.append([1.0 - probability, probability])
        return results

    def predict(self, X: Any) -> list[int]:
        return [1 if row[1] >= 0.5 else 0 for row in self.predict_proba(X)]
