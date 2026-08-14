"""File-driven Claude worker loop scaffold."""

__version__ = "0.1.0"

from .client import ProjectLoop, ProjectLoopError

__all__ = ["ProjectLoop", "ProjectLoopError", "__version__"]

