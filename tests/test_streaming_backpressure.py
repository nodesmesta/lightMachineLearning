"""Phase 2 / Day 4 — K5 streaming backpressure RED tests.

Reviewer P5 requires a selected bounded behavior for slow consumers. The chosen
behavior for this workspace is controlled stream termination when the bounded
buffer remains full past the configured backpressure timeout.
"""
from __future__ import annotations

import time
import unittest

from sydeco_lightml_core.adapter import Adapter
from sydeco_lightml_core.worker import InProcessWorkerHost


class FastStreamingAdapter(Adapter):
    def __init__(self) -> None:
        self.produced: list[int] = []
        self.closed = False

    def initialize(self, context):
        pass

    def infer(self, request, context):
        return {"label": "normal"}

    def shutdown(self):
        pass

    def stream(self, request, context):
        try:
            for i in range(50):
                self.produced.append(i)
                yield {"label": str(i)}
        finally:
            self.closed = True


class StreamingBackpressureTests(unittest.TestCase):
    def _host(self, adapter: FastStreamingAdapter) -> InProcessWorkerHost:
        host = InProcessWorkerHost()
        host.start(
            "stream-backpressure-app",
            "1.0.0",
            adapter,
            {
                "config": {
                    "resource_limits": {
                        "inference_timeout": 2.0,
                        "stream_first_chunk_timeout": 1.0,
                        "stream_idle_timeout": 1.0,
                        "stream_total_timeout": 2.0,
                        "stream_buffer_size": 1,
                        "stream_backpressure_timeout": 0.05,
                    }
                }
            },
        )
        return host

    def test_slow_consumer_gets_controlled_backpressure_error(self) -> None:
        adapter = FastStreamingAdapter()
        host = self._host(adapter)
        gen = host.stream({"text": "hello"}, "r-backpressure")

        first = next(gen)
        self.assertEqual(first, {"label": "0"})
        time.sleep(0.2)

        with self.assertRaises(Exception) as raised:
            next(gen)
        self.assertIn("backpressure", str(raised.exception).lower())
        self.assertLess(len(adapter.produced), 50)
        self.assertTrue(adapter.closed)

    def test_backpressure_does_not_recycle_healthy_worker(self) -> None:
        adapter = FastStreamingAdapter()
        host = self._host(adapter)
        gen = host.stream({"text": "hello"}, "r-backpressure")
        self.assertEqual(next(gen), {"label": "0"})
        time.sleep(0.2)
        with self.assertRaises(Exception):
            next(gen)

        self.assertEqual(host.infer({"text": "after-backpressure"}, "r-normal"), {"label": "normal"})


if __name__ == "__main__":
    unittest.main()
