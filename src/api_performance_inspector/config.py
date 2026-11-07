"""Validated configuration for :class:`PerformanceInspector`."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


class ConfigError(ValueError):
    """Raised when the inspector is given an invalid configuration."""


StorageMode = Literal["memory", "sqlite"]


@dataclass(frozen=True)
class InspectorConfig:
    storage: StorageMode = "memory"
    database_path: Path = Path("api_metrics.db")
    slow_request_threshold_ms: float = 500.0
    max_samples: int = 100_000
    retention_seconds: float = 7 * 24 * 3600
    exclude_paths: tuple[str, ...] = field(default_factory=tuple)
    max_routes: int = 500
    queue_size: int = 10_000

    def __post_init__(self) -> None:
        if self.storage not in ("memory", "sqlite"):
            raise ConfigError(f"storage must be 'memory' or 'sqlite', got {self.storage!r}")
        _positive_number("slow_request_threshold_ms", self.slow_request_threshold_ms)
        _positive_number("retention_seconds", self.retention_seconds)
        _int_at_least("max_samples", self.max_samples, 1)
        _int_at_least("max_routes", self.max_routes, 1)
        _int_at_least("queue_size", self.queue_size, 1)
        for path in self.exclude_paths:
            if not isinstance(path, str) or not path.startswith("/"):
                raise ConfigError(
                    f"exclude_paths entries must be strings starting with '/', got {path!r}"
                )


def _positive_number(name: str, value: object) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ConfigError(f"{name} must be a positive number, got {value!r}")


def _int_at_least(name: str, value: object, minimum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError(f"{name} must be an integer >= {minimum}, got {value!r}")
