"""Unix (Linux/macOS) PTY backend: `pty.fork()` + bash/zsh command hooks.

Implements `PtyBackendBase` (see `aicap/pty_backend/base.py`) using the
exact `pty.fork()` pattern verified in docs/plan.md's stage 0c:

  - `pty.fork()`'s child branch already creates a new session and makes
    the pty its controlling terminal; no manual `os.setsid()` needed.
  - The parent's `os.read()` on the master fd raises `OSError(EIO)` once
    the child has exited and closed its end of the pty, instead of
    returning `b""` -- this is real PTY EOF signalling on Linux, not a
    bug, and must be caught explicitly.
  - bash gets its hook via `bash --rcfile <bash_hook.sh> -i`; zsh gets its
    hook via a temporary `ZDOTDIR` (zsh has no `--rcfile` equivalent).

This module deliberately does not import `aicap.log_writer` or anything
else from the storage layer -- see docs/plan.md's P1 rationale for why
`pty_backend` and `log_writer` must stay decoupled (a broken backend must
never be able to take the storage logic down with it). Wiring the two
together is `recorder.py`'s job (stage 6, not built yet).
"""

import errno
import fcntl
import json
import os
import pty
import select
import shutil
import signal
import struct
import tempfile
import termios
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from aicap.pty_backend.base import PtyBackendBase

# The two shells this backend knows how to drive. Fixed set on purpose
# (docs/plan.md "已知限制": "本次只做 bash / zsh / PowerShell；fish、cmd.exe
# 不在范围内") -- not meant to be open-ended/pluggable.
_SUPPORTED_SHELLS = ("bash", "zsh")

_SHELL_HOOKS_DIR = Path(__file__).resolve().parent.parent / "shell_hooks"
_BASH_HOOK_PATH = _SHELL_HOOKS_DIR / "bash_hook.sh"
_ZSH_HOOK_PATH = _SHELL_HOOKS_DIR / "zsh_hook.sh"

# How long to wait, after a polite SIGTERM/SIGHUP, before escalating to
# SIGKILL in close(). Short on purpose: close() is a cleanup path, not a
# place a caller expects to sit waiting.
_TERMINATE_GRACE_PERIOD_SECONDS = 1.0
_TERMINATE_POLL_INTERVAL_SECONDS = 0.05

# Single-read chunk size for the master fd. Comfortably larger than any
# one terminal line/escape burst; not a hard cap on total output, just how
# much one read_output() call retrieves at a time.
_READ_CHUNK_BYTES = 65536


class UnixShellBackend(PtyBackendBase):
    """Drives an interactive bash or zsh session behind a real PTY.

    Usage::

        backend = UnixShellBackend(shell="bash")
        backend.start()
        try:
            backend.write_input(b"echo hi\\n")
            data = backend.read_output(timeout=0.5)
            events = backend.read_new_boundary_events()
        finally:
            backend.close()

    or as a context manager: ``with UnixShellBackend(shell="zsh") as
    backend: ...``.
    """

    def __init__(self, shell: str = "bash", env: Optional[Dict[str, str]] = None) -> None:
        """
        Args:
            shell: "bash" or "zsh". Selects both which executable is
                spawned and which hook-injection mechanism is used
                (`--rcfile` for bash, `ZDOTDIR` for zsh) -- see module
                docstring.
            env: Base environment for the child process. Defaults to a
                copy of this process's own environment (`os.environ`), so
                the spawned shell sees the same PATH/locale/etc as
                whatever launched aicap -- matching the tool's stated goal
                of taking over the user's *actual* configured shell
                environment (docs/plan.md "发布方式"), not a stripped-down
                one. `AICAP_MARKER_FILE` (and `ZDOTDIR`, for zsh) are added
                on top of this by `start()`; callers should not set those
                themselves.
        """
        if shell not in _SUPPORTED_SHELLS:
            raise ValueError(
                f"unsupported shell {shell!r}; UnixShellBackend only supports "
                f"{_SUPPORTED_SHELLS!r} (see docs/plan.md known limitations)"
            )
        self._shell = shell
        self._base_env = dict(env) if env is not None else dict(os.environ)

        self._pid: Optional[int] = None
        self._master_fd: Optional[int] = None
        self._exited = False
        self._exit_status: Optional[int] = None
        self._closed = False

        # mkstemp rather than a fixed path: multiple aicap sessions may run
        # concurrently, each needs its own marker channel (task requirement).
        marker_fd, marker_path = tempfile.mkstemp(prefix="aicap-marker-", suffix=".jsonl")
        os.close(marker_fd)
        self._marker_file_path = Path(marker_path)
        self._marker_read_offset = 0

        # Only used for zsh (temporary ZDOTDIR holding a generated .zshrc).
        self._zdotdir_path: Optional[Path] = None

    # -- PtyBackendBase interface -------------------------------------

    def start(self) -> None:
        if self._pid is not None:
            raise RuntimeError("UnixShellBackend.start() called more than once")

        env = dict(self._base_env)
        env["AICAP_MARKER_FILE"] = str(self._marker_file_path)

        argv = self._build_argv(env)

        pid, master_fd = pty.fork()
        if pid == 0:
            # Child branch: pty.fork() already made this process a new
            # session leader with the pty as its controlling terminal (see
            # module docstring) -- go straight to exec. If exec fails, this
            # is a forked child of a Python process: it must not unwind
            # back into normal Python control flow (that would re-run
            # parent-process cleanup/atexit machinery in a forked copy),
            # so report the error and exit the low-level way.
            try:
                os.execvpe(argv[0], argv, env)
            except OSError as exc:
                os.write(
                    2, f"aicap: failed to exec {argv[0]!r}: {exc}\n".encode("utf-8", "replace")
                )
                os._exit(127)

        self._pid = pid
        self._master_fd = master_fd

    def is_alive(self) -> bool:
        if self._pid is None:
            return False
        if self._exited:
            return False

        try:
            reaped_pid, status = os.waitpid(self._pid, os.WNOHANG)
        except ChildProcessError:
            # Already reaped elsewhere (e.g. by close()) -- treat as dead.
            self._exited = True
            return False

        if reaped_pid == 0:
            return True

        self._exited = True
        self._exit_status = status
        return False

    def write_input(self, data: bytes) -> None:
        if self._master_fd is None:
            raise RuntimeError("UnixShellBackend.write_input() called before start()")
        os.write(self._master_fd, data)

    def read_output(self, timeout: float) -> bytes:
        if self._master_fd is None:
            raise RuntimeError("UnixShellBackend.read_output() called before start()")

        try:
            ready, _, _ = select.select([self._master_fd], [], [], timeout)
        except (OSError, ValueError):
            # Master fd was closed (e.g. racing with close()) -- nothing
            # more to read.
            return b""

        if not ready:
            return b""

        try:
            return os.read(self._master_fd, _READ_CHUNK_BYTES)
        except OSError as exc:
            if exc.errno == errno.EIO:
                # Child exited and closed its end of the pty -- real PTY
                # EOF signalling on Linux (see module docstring), not an
                # error condition.
                return b""
            raise

    def resize(self, rows: int, cols: int) -> None:
        if self._master_fd is None:
            raise RuntimeError("UnixShellBackend.resize() called before start()")
        # TIOCSWINSZ: the traditional ioctl for setting a pty's window size
        # (struct winsize: rows, cols, x-pixels, y-pixels -- the pixel
        # fields are unused by shells and left 0). Used `fcntl`/`termios`'s
        # ioctl constant rather than `termios.tcsetwinsize` (Python 3.11+
        # only) since this project targets 3.9+.
        packed = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, packed)

    def read_new_boundary_events(self) -> List[Dict[str, Any]]:
        try:
            with open(self._marker_file_path, "rb") as handle:
                handle.seek(self._marker_read_offset)
                chunk = handle.read()
        except OSError:
            # Marker file gone (e.g. close() already ran) -- nothing to read.
            return []

        if not chunk:
            return []

        # Only consume up through the last newline: a line with no
        # trailing '\n' yet may be a write from the hook script still in
        # progress (printf followed by shell-buffered append), and must be
        # left for the next call rather than parsed as-is.
        last_newline_index = chunk.rfind(b"\n")
        if last_newline_index == -1:
            return []

        complete_chunk = chunk[: last_newline_index + 1]
        self._marker_read_offset += len(complete_chunk)

        events: List[Dict[str, Any]] = []
        for line in complete_chunk.split(b"\n"):
            if not line:
                continue
            try:
                event = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                # Malformed line (e.g. a torn write) -- skip it, don't let
                # one bad line take down the whole read.
                continue
            events.append(event)
        return events

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        if self._pid is not None:
            self._terminate_child()

        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None

        try:
            self._marker_file_path.unlink(missing_ok=True)
        except OSError:
            pass

        if self._zdotdir_path is not None:
            shutil.rmtree(self._zdotdir_path, ignore_errors=True)
            self._zdotdir_path = None

    # -- internals -------------------------------------------------------

    def _build_argv(self, env: Dict[str, str]) -> List[str]:
        if self._shell == "bash":
            return ["bash", "--rcfile", str(_BASH_HOOK_PATH), "-i"]

        # zsh: no --rcfile equivalent, so point ZDOTDIR at a fresh temp
        # directory holding a generated .zshrc that (a) sources the user's
        # real $HOME/.zshrc, since pointing ZDOTDIR elsewhere means zsh
        # would otherwise never find it, then (b) sources the static
        # zsh_hook.sh -- see that file's own docstring-comment for why
        # this order keeps things non-destructive to the user's setup.
        zdotdir = Path(tempfile.mkdtemp(prefix="aicap-zdotdir-"))
        self._zdotdir_path = zdotdir
        real_home = env.get("HOME", os.path.expanduser("~"))
        generated_zshrc = (
            "# generated by aicap.pty_backend.unix_backend -- do not edit\n"
            f'if [ -f "{real_home}/.zshrc" ]; then\n'
            f'    source "{real_home}/.zshrc"\n'
            "fi\n"
            f'source "{_ZSH_HOOK_PATH}"\n'
        )
        (zdotdir / ".zshrc").write_text(generated_zshrc, encoding="utf-8", newline="\n")
        env["ZDOTDIR"] = str(zdotdir)
        return ["zsh", "-i"]

    def _terminate_child(self) -> None:
        """Best-effort graceful-then-forceful shutdown of the child,
        always ending with a blocking `waitpid()` so it cannot linger as a
        zombie (task requirement: "确保子进程被清理（不留僵尸进程）").
        """
        if not self.is_alive():
            # Already exited; still must reap it if nothing has yet.
            self._reap_if_needed()
            return

        assert self._pid is not None
        for sig in (signal.SIGHUP, signal.SIGTERM):
            try:
                os.kill(self._pid, sig)
            except ProcessLookupError:
                break

            deadline = time.monotonic() + _TERMINATE_GRACE_PERIOD_SECONDS
            while time.monotonic() < deadline:
                if not self.is_alive():
                    return
                time.sleep(_TERMINATE_POLL_INTERVAL_SECONDS)
            if not self.is_alive():
                return

        # Still alive after SIGHUP and SIGTERM: escalate.
        try:
            os.kill(self._pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self._reap_if_needed()

    def _reap_if_needed(self) -> None:
        if self._exited or self._pid is None:
            return
        try:
            os.waitpid(self._pid, 0)
        except ChildProcessError:
            pass
        self._exited = True
