import io
import tempfile
import unittest
from pathlib import Path

from sydeco_lightml_core.ingress import IngressStore


class IngressClaimQuotaTests(unittest.TestCase):
    def test_claimed_reference_is_not_counted_as_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = IngressStore(tmp)
            manifest = {
                "limits": {
                    "max_pending_refs_per_app": 1,
                    "max_pending_ingest_bytes_per_app": 1024,
                }
            }
            upload = store.create(
                "app", manifest, io.BytesIO(b"payload"), 7, "x.bin", "application/octet-stream"
            )
            claim = store.claim("app", upload["file_ref"])

            self.assertEqual(store.pending_usage("app"), {"pending_refs": 0, "pending_bytes": 0})
            store.consume("app", claim["claim_ref"])


if __name__ == "__main__":
    unittest.main()
