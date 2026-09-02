"""Abstract interface shared by every platform-specific PTY backend.

See docs/plan.md's "总体架构" section: `pty_backend/base.py` is described
there as "抽象接口：spawn_shell(), read loop, marker channel". This module
is that interface, spelled out as a concrete `abc.ABC` so both
`unix_backend.py` (this stage) and the future `windows_backend.py` (stage
5, `pywinpty`/ConPTY-based) implement exactly the same shape. `recorder.py`
(stage 6) is the only module meant to import a concrete backend class by
name; everywhere else should be able to talk to "a `PtyBackendBase`"
without caring which platform it actually is.

Deliberately excluded from this interface (YAGNI, not needed by any
consumer yet):
  - Any parsing/interpretation of boundary events beyond JSON. What a
    "start" or "end" event *means* (matching them up, discarding orphans,
    driving a `LogWriter`) is `recorder.py`'s job (stage 6), not the
    backend's -- this class only guarantees delivery of whatever JSON
    objects the shell hook wrote to the marker channel, in order.

This module has no knowledge of `log_writer.py` or any other storage-layer
module, and must stay that way -- see docs/plan.md's P1 module-boundary
rationale ("这样任何一个平台后端出问题，都不会牵连到存储逻辑").
"""

import abc
from typing import Any, Dict, List


class PtyBackendBase(abc.ABC):
    """Spawns an interactive shell behind a PTY and exposes:

    - a byte-oriented read/write loop for mirroring the shell's real
      terminal I/O (so the user's experience of driving the shell is
      unaffected by aicap sitting in the middle), and
    - a side channel of structured "command boundary" JSON events (see
      docs/plan.md's "边界信号通道" section for the wire schema), produced
      by a shell hook script injected at spawn time.

    A backend instance is single-use: `start()` once, then any number of
    `write_input()`/`read_output()`/`read_new_boundary_events()` calls,
    then `close()` once. Also usable as a context manager (`with backend:
    ...`), which calls `start()`/`close()` for you.
    """

    @abc.abstractmethod
    def start(self) -> None:
        """Spawn the shell child process behind a PTY, with the
        command-boundary-detection hook injected into its startup.

        Must be called exactly once, before any other method except
        `close()`. Implementations should raise (not silently no-op) if
        spawning fails, e.g. the shell executable cannot be found.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def is_alive(self) -> bool:
        """Return whether the child shell process is still running.

        Must not block. Once this returns False the child has exited and
        further `write_input()` calls are meaningless (implementations may
        raise or silently drop -- see each implementation's docstring);
        `read_output()` may still return any output the child produced
        before exiting that has not been drained yet.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def write_input(self, data: bytes) -> None:
        """Write raw bytes to the child's stdin (i.e. forward keystrokes).

        Used both for real usage (forwarding the user's actual keypresses)
        and for tests (simulating "the user typed this"). `data` is raw
        bytes, not text -- callers are responsible for encoding.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def read_output(self, timeout: float) -> bytes:
        """Read whatever raw output the child has produced, waiting at
        most `timeout` seconds for at least one byte to become available.

        Returns `b""` if no output arrived within `timeout` -- this is
        ordinary "nothing to read yet", not necessarily end-of-stream;
        callers must consult `is_alive()` to tell the two apart. Never
        blocks longer than `timeout`, and never blocks indefinitely
        regardless of `timeout`'s value (callers are expected to loop).
        """
        raise NotImplementedError

    @abc.abstractmethod
    def read_new_boundary_events(self) -> List[Dict[str, Any]]:
        """Return command-boundary events written since the last call to
        this method, in the order they were written.

        Each returned item is one JSON object as decoded from a single
        line of the marker channel (docs/plan.md's "边界信号通道" schema,
        e.g. `{"event": "start", "command": "ls -la"}`). A line that
        cannot be parsed as JSON (including a line that was only
        partially written when read -- see implementation notes) is
        skipped rather than raised as an error; skipping one malformed
        line must never prevent later, well-formed lines from being
        returned on this or a future call. Returns an empty list if no
        new complete events are available.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def resize(self, rows: int, cols: int) -> None:
        """Tell the child's PTY its terminal is now `rows` x `cols`.

        Must be called with the real terminal's actual size before/soon
        after `start()` -- both backends default to a fixed size (see each
        implementation) if this is never called, which does not match any
        real terminal window and causes the child's own line-editor
        (PSReadLine, bash's readline, ...) to compute cursor positions for
        the wrong width. Mirrored output then lands in the wrong place on
        the real terminal -- garbled, overlapping text -- found during
        stage 6b real-world manual testing (see docs/plan.md), not a
        cosmetic nice-to-have.

        Safe to call again later if the real terminal is resized mid-session
        (not yet wired up to anything that detects that automatically).
        """
        raise NotImplementedError

    @abc.abstractmethod
    def close(self) -> None:
        """Tear down the child process and release every resource this
        backend created (PTY file descriptors, the marker channel, any
        temporary files/directories used to inject the shell hook).

        Must ensure the child process is not left running or zombied,
        even if it is still alive when `close()` is called. Safe to call
        more than once (later calls are no-ops).
        """
        raise NotImplementedError

    def __enter__(self) -> "PtyBackendBase":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()
