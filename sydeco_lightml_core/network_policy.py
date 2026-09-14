"""Manifest-derived runtime network policy helpers.

Minimal P1 scope: make ``permissions.network: none`` meaningful for Python
adapter execution without adding app-specific logic.
"""
from __future__ import annotations

import contextlib
import socket
import threading
from typing import Any, Dict, Iterator

NETWORK_NONE = "none"
NETWORK_OUTBOUND = "outbound"
ALLOWED_NETWORK_POLICIES = frozenset({NETWORK_NONE, NETWORK_OUTBOUND})


class NetworkPermissionDenied(PermissionError):
    """Raised when an adapter attempts network access without permission."""


def network_policy_from_config(config: Dict[str, Any]) -> str:
    permissions = config.get("permissions", {})
    if not isinstance(permissions, dict):
        return NETWORK_NONE
    value = permissions.get("network", NETWORK_NONE)
    return value if value in ALLOWED_NETWORK_POLICIES else NETWORK_NONE


def network_policy_from_context(context: Dict[str, Any]) -> str:
    config = context.get("config", {})
    return network_policy_from_config(config if isinstance(config, dict) else {})


_guard_state = threading.local()
_original_socket = socket.socket
_original_create_connection = socket.create_connection
_original_getaddrinfo = socket.getaddrinfo
_original_gethostbyname = socket.gethostbyname
_original_gethostbyname_ex = socket.gethostbyname_ex


def _network_denied() -> bool:
    return bool(getattr(_guard_state, "deny_network", False))


def _raise_denied() -> None:
    raise NetworkPermissionDenied("network access denied by permissions.network: none")


class _GuardedSocket(_original_socket):  # type: ignore[misc]
    def __new__(cls, *args: Any, **kwargs: Any) -> Any:
        if _network_denied():
            _raise_denied()
        return _original_socket.__new__(cls, *args, **kwargs)


def _guarded_create_connection(*args: Any, **kwargs: Any) -> Any:
    if _network_denied():
        _raise_denied()
    return _original_create_connection(*args, **kwargs)


def _guarded_getaddrinfo(*args: Any, **kwargs: Any) -> Any:
    if _network_denied():
        _raise_denied()
    return _original_getaddrinfo(*args, **kwargs)


def _guarded_gethostbyname(*args: Any, **kwargs: Any) -> Any:
    if _network_denied():
        _raise_denied()
    return _original_gethostbyname(*args, **kwargs)


def _guarded_gethostbyname_ex(*args: Any, **kwargs: Any) -> Any:
    if _network_denied():
        _raise_denied()
    return _original_gethostbyname_ex(*args, **kwargs)


socket.socket = _GuardedSocket  # type: ignore[assignment]
socket.create_connection = _guarded_create_connection  # type: ignore[assignment]
socket.getaddrinfo = _guarded_getaddrinfo  # type: ignore[assignment]
socket.gethostbyname = _guarded_gethostbyname  # type: ignore[assignment]
socket.gethostbyname_ex = _guarded_gethostbyname_ex  # type: ignore[assignment]


@contextlib.contextmanager
def adapter_network_guard(context: Dict[str, Any]) -> Iterator[None]:
    """Apply the manifest network policy while adapter code is running.

    ``network: outbound`` is an explicit grant and therefore leaves Python's
    socket module untouched. ``network: none`` denies common stdlib socket and
    DNS entry points. This is a dev/runtime Python guard; system-level sandboxing
    remains a separate production hardening boundary.
    """
    if network_policy_from_context(context) != NETWORK_NONE:
        yield
        return

    previous = _network_denied()
    _guard_state.deny_network = True
    try:
        yield
    finally:
        _guard_state.deny_network = previous
