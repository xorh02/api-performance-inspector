"""Lightweight, local-first performance profiling for FastAPI applications."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("api-performance-inspector")
except PackageNotFoundError:  # running from a source tree without installation
    __version__ = "0.0.0"

from .config import ConfigError
from .inspector import PerformanceInspector

__all__ = ["ConfigError", "PerformanceInspector", "__version__"]
