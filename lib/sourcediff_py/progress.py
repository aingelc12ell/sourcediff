"""Coordinator-owned operational events, separate from artifact protocols."""
import os
import sys
import time
from datetime import datetime, timezone

from .format import encode_bytes
from .interrupts import defer_interrupts
from .output import output_workspace, publish, validate_destination


class Reporter:
    def __init__(self, verbose=False, enabled=False):
        self.verbose = verbose
        self.enabled = verbose or enabled
        self.sink = None
        self.failed = False
        self.started = time.monotonic()
        self.last_tick = self.started
        self.phase_name = "validate"
        self.completed = self.hashed = self.reused = self.active = self.errors = 0

    def open_log(self, path, overwrite, protected, identities=()):
        # Publish a private, already-open regular file. Never truncate a user
        # pathname, including a symlink or hardlink to a protected input.
        validate_destination(path, overwrite, protected, forbidden_identities=identities)
        with output_workspace(path, overwrite, protected) as workspace:
            staged = workspace / "log"
            with defer_interrupts() as resume:
                self.sink = staged.open("w", encoding="ascii", newline="\n")
                try:
                    resume()
                    publish(staged, path, overwrite, protected, forbidden_identities=identities)
                except BaseException:
                    self.close()
                    raise

    def event(self, event, **fields):
        if not self.enabled:
            return
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        fields = " ".join(f"{key}={encode_bytes(os.fsencode(str(value)) if not isinstance(value, bytes) else value)}"
                          for key, value in fields.items())
        line = f"{timestamp} {event} {fields}\n"
        try:
            if self.sink is not None:
                self.sink.write(line)
                self.sink.flush()
            if self.verbose:
                sys.stderr.write(line)
                sys.stderr.flush()
        except OSError:
            self.failed = True
            raise

    def progress(self, force=False):
        if not self.enabled:
            return
        now = time.monotonic()
        if not force and now - self.last_tick < 1:
            return
        self.last_tick = now
        self.event("PROGRESS", phase=self.phase_name, completed=self.completed, hashed=self.hashed,
                   reused=self.reused, active=self.active, errors=self.errors, elapsed=int(now - self.started))

    def phase(self, name):
        if self.enabled:
            self.phase_name = name
            self.event("PHASE", phase=name)
            self.progress(force=True)

    def error(self, path, operation, message):
        self.errors += 1
        self.event("ERROR", path=path, operation=operation, message=message)

    def finish(self, status):
        if self.failed:
            return
        self.active = 0
        self.progress(force=True)
        self.event("SUMMARY", outcome="completed" if status in (0, 1) else "incomplete",
                   status=status, completed=self.completed, hashed=self.hashed, reused=self.reused,
                   errors=self.errors, elapsed=int(time.monotonic() - self.started))

    def close(self):
        if self.sink is not None:
            sink, self.sink = self.sink, None
            sink.close()

    def identities(self):
        """Protect the opened log even if its pathname is later rebound."""
        return () if self.sink is None else (os.fstat(self.sink.fileno()),)
