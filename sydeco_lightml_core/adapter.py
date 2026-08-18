"""Application / Capability Adapter contract (V2.1 proposal 3.0, R1-R4).

Core knows ONLY this interface. Application-specific executable logic
lives in the application bundle (the Adapter), never in Core.

Contract (verbatim from proposal 3.0 / R1):
    initialize(context)        - called once at worker start, AFTER Core
                                 loaded the models and verified the adapter
                                 hash.
    infer(request, context)    - called per inference request; must return
                                 a result matching the manifest
                                 output_schema.
    shutdown()                 - graceful, bounded (default 30 s from
                                 resource_limits), then SIGKILL.

context is injected by Core and is strictly bounded (read-only except the
app's own data dir):
    models     {role -> loaded model object}   read
    config     manifest (read-only view)       read
    data_dir   app's own writable data dir     write (only when declared)
    request_id current request id (infer only) read
    logger     app-level logging (NOT Core audit) read

NEVER injected: registry, Core config, secrets, other apps' paths.
"""
from __future__ import annotations

import abc
from typing import Any


class Adapter(abc.ABC):
    """Stable Core-facing adapter contract.

    A concrete adapter in an application bundle subclasses this class and
    implements the three lifecycle methods. Core imports and drives the
    adapter only through this interface.
    """

    @abc.abstractmethod
    def initialize(self, context: dict[str, Any]) -> None:
        """Prepare application resources once, after models are loaded.

        Called once at worker start. Failure -> worker not_ready +
        crash-loop backoff (A3).
        """
        raise NotImplementedError

    @abc.abstractmethod
    def infer(self, request: dict[str, Any], context: dict[str, Any]) -> Any:
        """Run one inference request and return an output-schema result.

        Called per inference request (single / batch / streaming, K5).
        The return value is validated against the manifest output_schema
        (H2) before being wrapped in the B1 envelope.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def shutdown(self) -> None:
        """Release application resources gracefully (bounded 30 s)."""
        raise NotImplementedError
