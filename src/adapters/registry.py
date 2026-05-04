"""Adapter registry.

Each adapter owns one Terraform resource type and turns a single
`resource_changes[]` entry into a `ResourceCostEstimate`. The registry
itself is intentionally minimal: a module-level dict, two functions.
"""
from __future__ import annotations

from typing import Any, Awaitable, Protocol

from src.models import ResourceCostEstimate


class CostAdapter(Protocol):
    resource_type: str

    def estimate(self, change: dict[str, Any]) -> Awaitable[ResourceCostEstimate]: ...


_REGISTRY: dict[str, CostAdapter] = {}


def register_adapter(adapter: CostAdapter) -> None:
    """Register an adapter under its declared `resource_type`.

    Last-write-wins: re-registering the same type replaces the prior
    adapter, which makes monkey-patching easy in tests.
    """
    _REGISTRY[adapter.resource_type] = adapter


def get_adapter(resource_type: str) -> CostAdapter | None:
    """Return the adapter for a given Terraform resource type, or None."""
    return _REGISTRY.get(resource_type)


def registered_types() -> list[str]:
    """For diagnostics — what's currently wired in."""
    return sorted(_REGISTRY.keys())
