#!/usr/bin/env python3
"""
Endpoint Isolation Forest Architecture (12-Feature Tabular Anomaly Scorer).
Pinning and mathematical isolation tree algorithm calibrated for Linux process telemetry.
Zero external library dependencies; 100% pure standard library math and structure.
"""
from __future__ import annotations

import math
import random
from typing import Any, List, Optional


def c_factor(n: int) -> float:
    """Average path length of unsuccessful searches in Binary Search Tree."""
    if n <= 1:
        return 0.0
    if n == 2:
        return 1.0
    return 2.0 * (math.log(n - 1) + 0.5772156649) - (2.0 * (n - 1) / n)


class IsolationTreeNode:
    def __init__(
        self,
        left: Optional[IsolationTreeNode] = None,
        right: Optional[IsolationTreeNode] = None,
        split_feature: Optional[int] = None,
        split_value: Optional[float] = None,
        size: int = 0,
    ) -> None:
        self.left = left
        self.right = right
        self.split_feature = split_feature
        self.split_value = split_value
        self.size = size
        self.is_leaf = left is None and right is None


class IsolationTree:
    def __init__(self, max_depth: int = 6) -> None:
        self.max_depth = max_depth
        self.root: Optional[IsolationTreeNode] = None

    def fit(self, X: List[List[float]], current_depth: int = 0) -> IsolationTreeNode:
        n = len(X)
        if current_depth >= self.max_depth or n <= 1:
            return IsolationTreeNode(size=n)

        feats = list(range(len(X[0])))
        random.shuffle(feats)
        for q in feats:
            vals = [row[q] for row in X]
            min_val, max_val = min(vals), max(vals)
            if min_val < max_val:
                p = random.uniform(min_val, max_val)
                left_X = [row for row in X if row[q] < p]
                right_X = [row for row in X if row[q] >= p]
                if left_X and right_X:
                    left_node = self.fit(left_X, current_depth + 1)
                    right_node = self.fit(right_X, current_depth + 1)
                    return IsolationTreeNode(
                        left=left_node,
                        right=right_node,
                        split_feature=q,
                        split_value=p,
                        size=n,
                    )
        return IsolationTreeNode(size=n)

    def path_length(
        self, x: List[float], node: Optional[IsolationTreeNode] = None, current_depth: int = 0
    ) -> float:
        if node is None:
            node = self.root
        if node is None or node.is_leaf:
            return current_depth + c_factor(node.size if node else 0)
        if x[node.split_feature] < node.split_value:
            return self.path_length(x, node.left, current_depth + 1)
        else:
            return self.path_length(x, node.right, current_depth + 1)


class EndpointIsolationForest:
    """Tabular Isolation Forest for Linux Endpoint Process Telemetry (12 Features)."""

    def __init__(
        self, n_estimators: int = 50, max_samples: int = 64, random_state: int = 42
    ) -> None:
        self.n_estimators = n_estimators
        self.max_samples = max_samples
        self.random_state = random_state
        self.trees: List[IsolationTree] = []
        self.c_psi = c_factor(max_samples)
        self.feature_names_in_ = [
            "pid_ratio",
            "ppid_ratio",
            "cmd_len",
            "arg_count",
            "token_entropy",
            "uid_class",
            "start_tick_delta",
            "mem_rss_ratio",
            "cpu_tick_ratio",
            "fd_count",
            "thread_count",
            "privilege_flag",
        ]
        self.n_features_in_ = 12

    def fit(self, X: List[List[float]]) -> EndpointIsolationForest:
        random.seed(self.random_state)
        n = len(X)
        self.trees = []
        depth_limit = int(math.ceil(math.log2(self.max_samples)))
        for _ in range(self.n_estimators):
            subsample = [X[i] for i in random.sample(range(n), min(self.max_samples, n))]
            tree = IsolationTree(max_depth=depth_limit)
            tree.root = tree.fit(subsample)
            self.trees.append(tree)
        return self

    def score_single(self, x: List[float]) -> float:
        if len(x) != self.n_features_in_:
            raise ValueError(
                f"Feature dimension mismatch: expected {self.n_features_in_}, got {len(x)}"
            )
        avg_path = sum(t.path_length(x) for t in self.trees) / len(self.trees)
        raw_score = 2.0 ** (-avg_path / self.c_psi)
        # Calibrated Sigmoid: normal processes baseline ~0.557 -> score ~0.16
        # Anomalies with shorter path lengths ~0.639 -> score ~0.84 (> 0.75 threshold)
        z = (raw_score - 0.598) / 0.025
        calibrated = 1.0 / (1.0 + math.exp(-z))
        return round(float(calibrated), 4)

    def predict_proba(self, X: Any) -> List[List[float]]:
        if isinstance(X, dict):
            X = [X.get(k, 0.0) for k in self.feature_names_in_]
        if isinstance(X, (list, tuple)) and X and isinstance(X[0], (int, float)):
            X = [X]
        results = []
        for row in X:
            anomaly_score = self.score_single(row)
            normal_score = round(1.0 - anomaly_score, 4)
            results.append([normal_score, anomaly_score])
        return results

    def predict(self, X: Any) -> List[int]:
        probs = self.predict_proba(X)
        return [-1 if p[1] > 0.75 else 1 for p in probs]
