"""Turn interruption and protection for cleanup that must finish first."""

import signal
import threading
from collections.abc import Iterator
from contextlib import contextmanager


class TurnCancelled(KeyboardInterrupt):
    """An interrupted tool with a result explaining its cleanup and partial output."""


_read_state = threading.local()


@contextmanager
def cancellable_reads(event: threading.Event) -> Iterator[None]:
    previous = getattr(_read_state, "event", None)
    _read_state.event = event
    try:
        yield
    finally:
        _read_state.event = previous


def check_read_cancelled() -> None:
    event = getattr(_read_state, "event", None)
    if event is not None and event.is_set():
        raise TurnCancelled("Read-only tool cancelled; no workspace writes performed.")


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
