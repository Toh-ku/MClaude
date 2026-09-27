"""Turn interruption and protection for cleanup that must finish first."""

import signal
import threading
from collections.abc import Iterator
from contextlib import contextmanager


class TurnCancelled(KeyboardInterrupt):
    """An interrupted tool with a result explaining its cleanup and partial output."""


@contextmanager
def protect_cleanup() -> Iterator[None]:
    """Ignore repeated Ctrl+C while restoring resources and conversation history."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)
