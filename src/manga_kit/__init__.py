"""Independent manga-library tooling; no acquisition functionality."""

__version__ = "0.1.0.dev2"


def create_app(*args, **kwargs):
    from .app import create_app as factory
    return factory(*args, **kwargs)
