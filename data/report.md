# NameError from systemd stream cleanup before HTTP response exists

## Summary
The P1 systemd parity RED tests reproduce the reviewer-reported cleanup regression in `SystemdTransientWorkerHost.stream()`. When stream setup fails before `resp = conn.getresponse()` assigns a response object, the cleanup path raises `NameError` and masks the intended K5 exception.

## Root cause
**location:** /home/sydeco/Dev/SYDECO_LIGHTML_V2_DEV/sydeco_lightml_core/worker.py
```text
SystemdTransientWorkerHost.stream()
  -> conn = None
  -> resp is not initialized
  -> _close_stream_connection() closes resp and conn
  -> conn.request(...) or conn.getresponse() fails before resp assignment
  -> exception handler / finally calls _close_stream_connection()
  -> _close_stream_connection() reads resp
  -> NameError masks WorkerNotReady or StreamTimeout
```

Observed code shape:
```text
1059        conn = None
...
1095        def _close_stream_connection() -> None:
1096            if resp is not None:
...
1132            resp = conn.getresponse()
```

Trace source observed with:
```bash
python3 -m unittest discover -s tests -p 'test_streaming_systemd_parity.py' -v
```

Relevant RED result:
```text
test_systemd_initial_connection_refused_before_response_raises_worker_not_ready ... ERROR
test_systemd_initial_timeout_before_response_raises_first_chunk_timeout ... ERROR

Ran 13 tests in 5.446s
FAILED (errors=2)
```

Relevant trace:
```text
File "/home/sydeco/Dev/SYDECO_LIGHTML_V2_DEV/sydeco_lightml_core/worker.py", line 1096, in _close_stream_connection
    if resp is not None:
NameError: free variable 'resp' referenced before assignment in enclosing scope
```

## Impact
The bug affects the production/systemd stream path. A connection refusal before an HTTP response should remain `WorkerNotReady`, and a pre-response timeout should remain `StreamTimeout("first_chunk")`. Both currently can be hidden by `NameError`, so the artifact cannot be treated as final K5 evidence.

## Recommendation
Initialize `resp` before any cleanup helper can access it:
```diff
  conn = None
+ resp = None
```

Keep the correction surgical. After the fix, rerun the two P1 tests, then rerun systemd parity with `ResourceWarning` hardened to error. The final README should mention this bug, the RED evidence, the `resp = None` fix, and the GREEN verification.
