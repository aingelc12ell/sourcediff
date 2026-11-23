"""Defer main-thread interruptions until resource cleanup is installed."""

import signal
from contextlib import contextmanager
from threading import current_thread, main_thread


@contextmanager
def defer_interrupts():
    """Yield a callback that resumes handlers once the resource has an owner."""
    handlers, pending = {}, []

    def defer(signum, frame):
        if not pending:
            pending.append((signum, frame, handlers[signum]))

    def resume():
        while handlers:
            signum, handler = next(iter(handlers.items()))
            signal.signal(signum, handler)
            del handlers[signum]
        if pending:
            signum, frame, handler = pending.pop()
            handler(signum, frame)

    try:
        if current_thread() is main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                handler = signal.getsignal(signum)
                if callable(handler):
                    handlers[signum] = handler
                    signal.signal(signum, defer)
        yield resume
    finally:
        resume()
