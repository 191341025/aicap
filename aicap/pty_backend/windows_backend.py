"""Windows PTY backend: pywinpty (ConPTY) + a PSReadLine command hook.

Implements `PtyBackendBase` (see `aicap/pty_backend/base.py`) using
`winpty.PtyProcess.spawn()` to drive `powershell.exe` (or `pwsh.exe`) behind
a real ConPTY, with `aicap/shell_hooks/pwsh_hook.ps1` injected via
`-File` at spawn time.

This module's design choices are pinned to facts re-verified for stage 5 of
docs/plan.md (see that document's "第 5 阶段" notes for the full write-up),
not assumed from the stage-0 PowerShell/pywinpty spikes, which validated
similar-looking pieces in isolation but never this exact combination:

  - `winpty.PtyProcess.read()` (see `winpty/ptyprocess.py` in the installed
    pywinpty 3.0.5 package) already returns a decoded `str`, not `bytes` --
    it reads raw bytes off an internal socket and decodes them as UTF-8
    itself, retrying a byte at a time on `UnicodeDecodeError` so a read
    never returns on a torn multi-byte boundary. `read_output()`'s contract
    here (see `base.py`) is `bytes`, matching `unix_backend.py`'s
    byte-oriented interface, so this backend re-encodes that `str` back to
    UTF-8 bytes before returning it. Whether the *original* bytes ConPTY
    produced were actually valid UTF-8 in the first place (as opposed to,
    say, the console output codepage's bytes) is exactly the "open risk"
    docs/plan.md flagged as unverified -- see this module's test suite
    (`tests/test_windows_backend.py`) for the live non-ASCII round-trip
    check that answers it empirically rather than by inspection.
  - `winpty.PtyProcess.fileno()` returns the file descriptor of a real
    Windows socket (pywinpty proxies ConPTY's output through a background
    thread into a loopback `socket.socket`, see `_read_in_thread` in
    `winpty/ptyprocess.py`), not a Unix-style pipe/pty fd. `select.select()`
    works on it for exactly the reason it wouldn't work on an arbitrary
    Windows file handle: it *is* a socket. This lets `read_output()` use
    the same "select with a timeout, then read" shape as
    `unix_backend.py`'s `os.read()`-based version instead of needing a
    polling loop.
  - Windows has no SIGHUP/SIGTERM/SIGKILL. `os.kill(pid, sig)` on Windows
    maps any `sig` that isn't `signal.CTRL_C_EVENT`/`CTRL_BREAK_EVENT`
    (0/1) straight to `TerminateProcess` -- so pywinpty's own
    `PtyProcess.terminate()`, which internally calls
    `self.kill(signal.SIGINT)`, is already a hard kill on this platform
    regardless of its `force` argument; there is no softer signal to send
    first. `close()` below approximates "graceful" the same way a human
    closing the window would -- by asking the shell to exit on its own
    (writing "exit\\r\\n") and giving it a brief window to do so -- before
    falling back to `PtyProcess.close(force=True)`, which both tears down
    pywinpty's socket-reader plumbing and guarantees a `TerminateProcess`
    kill of anything still alive. See `_terminate_child()` below.

Like `unix_backend.py`, this module does not import `aicap.log_writer` or
anything else from the storage layer -- see docs/plan.md's P1 rationale
for why `pty_backend` and the storage layer must stay decoupled.
"""

import os
import select
import tempfile
import time
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import winpty

from aicap.pty_backend.base import PtyBackendBase

# The two PowerShell executables this backend knows how to drive (Windows
# PowerShell 5.1 and PowerShell 7+ -- docs/plan.md "已知限制": fish/cmd.exe
# are out of scope). Fixed set on purpose, same rationale as
# `unix_backend.py`'s `_SUPPORTED_SHELLS` -- not meant to be open-ended.
_SHELL_EXECUTABLES = {
    "powershell": "powershell.exe",
    "pwsh": "pwsh.exe",
}

_SHELL_HOOKS_DIR = Path(__file__).resolve().parent.parent / "shell_hooks"
_PWSH_HOOK_PATH = _SHELL_HOOKS_DIR / "pwsh_hook.ps1"

# How long to give the shell to exit on its own (in response to a simulated
# "exit\r\n" keystroke) before close() falls back to a forced kill. Short on
# purpose: close() is a cleanup path, not somewhere a caller expects to sit
# waiting -- mirrors `unix_backend.py`'s grace period for the same reason,
# just a little longer since starting a fresh PowerShell "prompt" function
# call and the ConPTY teardown involved is slower than a Unix shell exiting.
_GRACEFUL_EXIT_GRACE_PERIOD_SECONDS = 2.0
_GRACEFUL_EXIT_POLL_INTERVAL_SECONDS = 0.05

# Single-read chunk size, mirrors `unix_backend.py`'s `_READ_CHUNK_BYTES`:
# comfortably larger than any one terminal line/escape burst, not a hard cap
# on total output.
_READ_CHUNK_BYTES = 65536


class WindowsShellBackend(PtyBackendBase):
    """Drives an interactive PowerShell session behind a real ConPTY.

    Usage::

        backend = WindowsShellBackend(shell="powershell")
        backend.start()
        try:
            backend.write_input(b"echo hi\\r\\n")
            data = backend.read_output(timeout=0.5)
            events = backend.read_new_boundary_events()
        finally:
            backend.close()

    or as a context manager: ``with WindowsShellBackend() as backend: ...``.
    """

    def __init__(self, shell: str = "powershell", env: Optional[Dict[str, str]] = None) -> None:
        """
        Args:
            shell: "powershell" (Windows PowerShell 5.1, the default -- the
                only version stage 0/5 have actually been verified against)
                or "pwsh" (PowerShell 7+). Selects which executable is
                spawned; both use the same `pwsh_hook.ps1` hook script.
            env: Base environment for the child process. Defaults to a copy
                of this process's own environment (`os.environ`), so the
                spawned shell sees the same PATH/profile locations/etc as
                whatever launched aicap -- matching `unix_backend.py`'s same
                choice and docs/plan.md's "发布方式" goal of taking over the
                user's *actual* configured shell environment, not a
                stripped-down one. `AICAP_MARKER_FILE` is added on top of
                this by `start()`; callers should not set it themselves.
        """
        if shell not in _SHELL_EXECUTABLES:
            raise ValueError(
                f"unsupported shell {shell!r}; WindowsShellBackend only supports "
                f"{tuple(_SHELL_EXECUTABLES)!r} (see docs/plan.md known limitations)"
            )
        self._executable = _SHELL_EXECUTABLES[shell]
        self._base_env = dict(env) if env is not None else dict(os.environ)

        self._proc: Optional["winpty.PtyProcess"] = None
        self._exited = False
        self._closed = False

        # mkstemp rather than a fixed path: multiple aicap sessions may run
        # concurrently, each needs its own marker channel (task requirement;
        # matches unix_backend.py's same choice).
        marker_fd, marker_path = tempfile.mkstemp(prefix="aicap-marker-", suffix=".jsonl")
        os.close(marker_fd)
        self._marker_file_path = Path(marker_path)
        self._marker_read_offset = 0

    # -- PtyBackendBase interface -------------------------------------

    def start(self) -> None:
        if self._proc is not None:
            raise RuntimeError("WindowsShellBackend.start() called more than once")

        env = dict(self._base_env)
        env["AICAP_MARKER_FILE"] = str(self._marker_file_path)

        argv = [
            self._executable,
            "-NoExit",
            # -ExecutionPolicy Bypass is scoped to this one spawned process
            # only (it does not touch the user's persistent LocalMachine/
            # CurrentUser policy) -- without it, a user whose configured
            # policy is Restricted (the historical Windows client default)
            # would have pwsh_hook.ps1 refused outright, silently disabling
            # command-boundary detection. This mirrors bash/zsh needing no
            # equivalent flag only because Unix shells have no analogous
            # script-execution policy to trip over.
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(_PWSH_HOOK_PATH),
        ]

        # winpty.PtyProcess.spawn() raises FileNotFoundError itself (via an
        # internal shutil.which() lookup) if `self._executable` cannot be
        # found on PATH, satisfying base.py's "must raise, not silently
        # no-op, if spawning fails" requirement with no extra code here.
        self._proc = winpty.PtyProcess.spawn(argv, env=env)

    def is_alive(self) -> bool:
        if self._proc is None:
            return False
        if self._exited:
            return False

        try:
            alive = self._proc.isalive()
        except Exception:
            # Treat any failure to query liveness (e.g. racing with close())
            # as "no longer alive" rather than propagating -- is_alive() is
            # documented as must-not-block/must-not-raise.
            alive = False

        if not alive:
            self._exited = True
        return alive

    def write_input(self, data: bytes) -> None:
        if self._proc is None:
            raise RuntimeError("WindowsShellBackend.write_input() called before start()")
        # PtyProcess.write() takes str, not bytes (see module docstring on
        # why pywinpty is string-oriented internally); decode with
        # "replace" rather than raising, since write_input()'s contract is
        # "forward these raw bytes", not "validate they're clean UTF-8" --
        # a stray invalid byte from a real user keystroke sequence should
        # not blow up the whole write. Note PtyProcess.write() itself raises
        # EOFError if the child has already exited (base.py: "implementations
        # may raise or silently drop" once is_alive() is False -- this one
        # raises, propagated as-is).
        self._proc.write(data.decode("utf-8", errors="replace"))

    def resize(self, rows: int, cols: int) -> None:
        if self._proc is None:
            raise RuntimeError("WindowsShellBackend.resize() called before start()")
        # winpty.PtyProcess.setwinsize(rows, cols) -- confirmed against the
        # real installed pywinpty 3.0.5 API in stage 0a (docs/plan.md); not
        # guessed.
        self._proc.setwinsize(rows, cols)

    def read_output(self, timeout: float) -> bytes:
        if self._proc is None:
            raise RuntimeError("WindowsShellBackend.read_output() called before start()")

        try:
            ready, _, _ = select.select([self._proc.fileno()], [], [], timeout)
        except (OSError, ValueError):
            # Underlying socket already closed (e.g. racing with close()) --
            # nothing more to read.
            return b""

        if not ready:
            return b""

        try:
            text = self._proc.read(_READ_CHUNK_BYTES)
        except EOFError:
            # Child exited and closed its end of the pty -- real EOF
            # signalling from pywinpty (raised by PtyProcess.read()), not an
            # error condition. Mirrors unix_backend.py treating OSError(EIO)
            # the same way.
            return b""
        except OSError:
            return b""

        return text.encode("utf-8", errors="replace")

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

        # Only consume up through the last newline: a line with no trailing
        # '\n' yet may be a write from the hook script still in progress
        # (Add-Content followed by a partial flush), and must be left for
        # the next call rather than parsed as-is. Mirrors
        # unix_backend.py's identical reasoning.
        last_newline_index = chunk.rfind(b"\n")
        if last_newline_index == -1:
            return []

        complete_chunk = chunk[: last_newline_index + 1]
        self._marker_read_offset += len(complete_chunk)

        events: List[Dict[str, Any]] = []
        for line in complete_chunk.split(b"\n"):
            if not line:
                continue
            # Windows text-mode line endings: Add-Content writes "\r\n".
            line = line.rstrip(b"\r")
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

        if self._proc is not None:
            self._terminate_child()

        try:
            self._marker_file_path.unlink(missing_ok=True)
        except OSError:
            pass

    # -- internals -------------------------------------------------------

    def _terminate_child(self) -> None:
        """Best-effort graceful-then-forceful shutdown of the child (task
        requirement: "close() ... 保证子进程被清理不留残留"). See the module
        docstring for why "graceful" on Windows means "ask the shell to
        exit on its own", not a softer signal -- there isn't one.
        """
        assert self._proc is not None

        try:
            alive = self._proc.isalive()
        except Exception:
            alive = False

        if alive:
            try:
                self._proc.write("exit\r\n")
            except (EOFError, OSError):
                pass

            deadline = time.monotonic() + _GRACEFUL_EXIT_GRACE_PERIOD_SECONDS
            while time.monotonic() < deadline:
                try:
                    alive = self._proc.isalive()
                except Exception:
                    alive = False
                if not alive:
                    break
                time.sleep(_GRACEFUL_EXIT_POLL_INTERVAL_SECONDS)

        # PtyProcess.close(force=True) tears down pywinpty's socket-reader
        # thread/plumbing and, if the child is still alive at this point
        # (the graceful "exit" above didn't take effect in time), force-
        # terminates it via TerminateProcess -- see module docstring.
        try:
            self._proc.close(force=True)
        except Exception:
            pass

        self._exited = True
