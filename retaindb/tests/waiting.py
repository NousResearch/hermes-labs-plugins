"""``wait_until``: poll a condition instead of sleeping a guessed interval (core's tests/waiting.py)."""

import time


def wait_until(predicate, *, timeout=10.0, interval=0.01, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")
