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


class DefaultModelAdapter(Adapter):
    """Built-in universal adapter for zero-code model bundles.

    When an application bundle contains model artifacts but no custom
    adapter/main.py, the worker activates this default adapter. It automatically
    inspects loaded models, binds the primary model, and routes inference
    requests to the model's standard inference methods (.predict / .predict_proba).
    """

    def __init__(self) -> None:
        self.models: dict[str, Any] = {}
        self.primary_model: Any = None
        self.primary_role: str = ""

    def initialize(self, context: dict[str, Any]) -> None:
        self.models = context.get("models", {})
        if not self.models:
            raise RuntimeError(
                "DefaultModelAdapter requires at least one loaded model in context['models']"
            )

        # Determine primary model: prefer role 'primary', 'classifier', 'model', or first role
        for preferred in ("primary", "classifier", "model"):
            if preferred in self.models:
                self.primary_role = preferred
                self.primary_model = self.models[preferred]
                break
        if self.primary_model is None:
            self.primary_role = next(iter(self.models))
            self.primary_model = self.models[self.primary_role]

    def infer(self, request: dict[str, Any], context: dict[str, Any]) -> Any:
        if self.primary_model is None:
            raise RuntimeError(
                "DefaultModelAdapter is not initialized or has no primary model"
            )

        # Extract inputs from request: look for 'inputs', 'features', 'text', 'data', or request itself
        inputs = None
        for key in ("inputs", "features", "text", "data"):
            if key in request:
                inputs = request[key]
                break
        if inputs is None:
            inputs = request

        # Execute model prediction
        model = self.primary_model
        if hasattr(model, "predict_proba"):
            try:
                preds = model.predict_proba(inputs)
            except Exception:
                preds = model.predict(inputs) if hasattr(model, "predict") else model(inputs)
        elif hasattr(model, "predict"):
            preds = model.predict(inputs)
        elif callable(model):
            preds = model(inputs)
        else:
            raise RuntimeError(
                f"Primary model {self.primary_role} is not callable and has no predict method"
            )

        # Convert numpy array / tensor to python native types if needed
        if hasattr(preds, "tolist"):
            result_val = preds.tolist()
        else:
            result_val = preds

        return {
            "result": result_val,
            "status": "ok",
            "model_role": self.primary_role,
        }

    def shutdown(self) -> None:
        self.models.clear()
        self.primary_model = None

