"""Real-terminal raw I/O for stage 6b interactive passthrough (docs/plan.md).

Unix only for now -- Windows needs a different mechanism (no `termios`, and
`select` does not work on console stdin) and is deliberately deferred to a
later stage. Callers must import this module lazily (inside a function, not
at module load time), the same way `recorder.create_backend()` lazily
imports the platform-specific `pty_backend` module -- importing this at
Windows module-load time would fail immediately (`termios`/`tty` do not
exist there).

The whole point of this module is to do as little as possible: put the real
terminal into raw mode so the OS stops intercepting control sequences
(Ctrl+C, Ctrl+Z, ...) on aicap's own process, then move raw bytes verbatim
in both directions. aicap does not interpret any keystroke itself -- the
recorded child shell's own terminal driver is what gives Ctrl+C etc. their
usual meaning, exactly as if the user were typing directly into that shell
with nothing in between (see docs/plan.md's stage 6b design discussion).
"""

import os
import select
import sys
import termios
import tty
from typing import Optional, Tuple


class UnixTerminalIO:
    """Context manager: puts `stdin_fd` into raw mode for its duration and
    restores the original settings on exit (even if an exception propagates
    out of the `with` block -- a crashed aicap must not leave the user's
    real terminal stuck in raw mode). Also gives non-blocking-with-timeout
    read/write access to the real terminal.
    """

    def __init__(self, stdin_fd: Optional[int] = None, stdout_fd: Optional[int] = None) -> None:
        self._stdin_fd = stdin_fd if stdin_fd is not None else sys.stdin.fileno()
        self._stdout_fd = stdout_fd if stdout_fd is not None else sys.stdout.fileno()
        self._saved_termios = None

    def __enter__(self) -> "UnixTerminalIO":
        self._saved_termios = termios.tcgetattr(self._stdin_fd)
        tty.setraw(self._stdin_fd)
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self._saved_termios is not None:
            termios.tcsetattr(self._stdin_fd, termios.TCSADRAIN, self._saved_termios)
            self._saved_termios = None

    def read_stdin(self, timeout: float) -> bytes:
        """Return whatever is available on stdin within `timeout` seconds,
        or b"" if nothing arrived within that window -- mirrors
        `PtyBackendBase.read_output()`'s same "no data within timeout is
        not EOF" contract, for the same reason (callers loop).
        """
        ready, _, _ = select.select([self._stdin_fd], [], [], timeout)
        if not ready:
            return b""
        return os.read(self._stdin_fd, 65536)

    def write_stdout(self, data: bytes) -> None:
        os.write(self._stdout_fd, data)

    def get_size(self) -> Tuple[int, int]:
        """Return (rows, cols) of the real terminal, so the caller can
        propagate it to the recorded child's PTY (`PtyBackendBase.resize()`)
        -- without this, the child assumes a fixed default size that does
        not match the real window, and its own line-editor's cursor-position
        math comes out wrong once mirrored back (garbled/overlapping text,
        found during stage 6b real-world manual testing).
        """
        size = os.get_terminal_size(self._stdout_fd)
        return size.lines, size.columns
