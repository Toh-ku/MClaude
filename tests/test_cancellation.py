"""Signal protection is bounded to resource and history cleanup."""

import signal

import pytest

from mclaude.cancellation import protect_cleanup


def test_cleanup_ignores_repeated_interrupts_and_restores_handler():
    original = signal.getsignal(signal.SIGINT)
    with pytest.raises(RuntimeError, match="cleanup failure"):
        with protect_cleanup():
            assert signal.getsignal(signal.SIGINT) == signal.SIG_IGN
            signal.raise_signal(signal.SIGINT)
            raise RuntimeError("cleanup failure")
    assert signal.getsignal(signal.SIGINT) == original
