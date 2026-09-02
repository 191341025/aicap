"""Orchestrates one recording session: picks a `pty_backend`, drives it,
and feeds what it produces into `log_writer`/`retention`.

This is the only module that is allowed to know both "a platform backend"
and "the storage layer" exist (docs/plan.md, P1 module-boundary rationale) --
`pty_backend/*` never imports `log_writer`, and `log_writer`/`retention`
never import `pty_backend`.

`run_until_exit()` drives a session non-interactively (used by tests and
`Recorder.pump()`'s own unit tests, which exercise the platform-independent
orchestration core -- pairing boundary events, accumulating each command's
raw output, driving `LogWriter`/`retention` -- against a fake backend,
without needing a real shell). `run_interactive()` is what a real
`aicap start` invocation uses: it additionally forwards the real terminal's
keystrokes to the child shell and mirrors the child's output back (see its
own docstring).
"""

import os
import signal
import sys
import threading
import time
from typing import Any, Dict, Optional

from aicap.log_writer import LogWriter
from aicap.pty_backend.base import PtyBackendBase
from aicap.retention import enforce_retention_policy

# How often read_output()/read_new_boundary_events() are polled by
# run_until_exit() while nothing is happening. Long enough not to
# busy-loop the CPU while idle; run_interactive() uses the much shorter
# _INTERACTIVE_POLL_TIMEOUT_SECONDS below instead, for a real human typing.
_DEFAULT_POLL_TIMEOUT_SECONDS = 0.2

# Shorter poll timeout for run_interactive(): a real human typing expects
# near-immediate echo, unlike run_until_exit()'s non-interactive use where
# 0.2s of added latency is unobservable.
_INTERACTIVE_POLL_TIMEOUT_SECONDS = 0.03

# How long to wait for the real-terminal-size sync (get_size() + resize())
# before giving up on it for this session. Found the hard way during stage
# 6b real-world manual testing: the underlying platform call occasionally
# never returns at all (observed on Windows -- the child shell itself
# stayed alive and healthy the whole time, only this step never completed),
# which without a timeout hangs run_interactive() forever before it ever
# reaches the read/write loop. See _sync_terminal_size().
_TERMINAL_SIZE_SYNC_TIMEOUT_SECONDS = 2.0

# How long to keep accumulating output for a command after its "end"
# boundary event arrives before actually finalizing it. Confirmed via a
# real repro (docs/plan.md): the marker-file channel (a plain file write)
# and the real console-output channel (the ConPTY socket) have no ordering
# guarantee relative to each other -- for a fast command (e.g. `ls`/`dir`
# in a small directory), both its "start" and "end" marker events can land
# in the marker file before the shell has actually finished flushing the
# formatted table output through to the ConPTY's own read side. Finalizing
# immediately on "end" was losing that output entirely (it arrived one or
# more poll cycles later, by which point _has_in_flight_command was already
# false, so it was silently treated as unowned/ambient output). See
# `_maybe_finalize_pending_end()`.
_COMMAND_END_GRACE_PERIOD_SECONDS = 0.3


def _detect_unix_shell() -> str:
    """Pick "bash" or "zsh" from the user's own $SHELL, defaulting to bash
    if $SHELL is unset or names something else -- `UnixShellBackend` (see
    docs/plan.md "已知限制") only ever supports these two.
    """
    shell_path = os.environ.get("SHELL", "")
    if shell_path.endswith("zsh"):
        return "zsh"
    return "bash"


def create_backend(shell: Optional[str] = None) -> PtyBackendBase:
    """Build the right `PtyBackendBase` implementation for this platform.

    Imports the platform-specific backend module lazily (inside this
    function, not at module load time) so that `recorder.py` itself stays
    importable on both platforms -- `windows_backend.py` fails to import on
    Unix (no `winpty`) and `unix_backend.py` fails to import on Windows (no
    `termios`), and neither should be a problem for code that never actually
    needs to construct that platform's backend.
    """
    if sys.platform == "win32":
        from aicap.pty_backend.windows_backend import WindowsShellBackend

        return WindowsShellBackend(shell=shell or "powershell")

    from aicap.pty_backend.unix_backend import UnixShellBackend

    return UnixShellBackend(shell=shell or _detect_unix_shell())


class Recorder:
    """Drives one `PtyBackendBase` and one `LogWriter` for the lifetime of
    one recording session.

    Usage::

        recorder = Recorder(log_dir="/path/to/logs")
        recorder.run_until_exit()

    `backend` can be injected (e.g. a test double implementing
    `PtyBackendBase`) instead of letting `Recorder` build a real platform
    backend itself -- this is how `tests/test_recorder.py` exercises the
    orchestration logic quickly and deterministically, without spawning a
    real shell for every test case.
    """

    def __init__(
        self,
        log_dir: str,
        shell: Optional[str] = None,
        backend: Optional[PtyBackendBase] = None,
    ) -> None:
        self.log_writer = LogWriter(log_dir)
        self._backend = backend if backend is not None else create_backend(shell)
        self._has_in_flight_command = False
        self._in_flight_output = b""
        self._finalized = False
        # Set once an "end" boundary event arrives, cleared once that
        # command is actually finalized (either the grace period elapses
        # or the next "start" arrives first) -- see
        # `_COMMAND_END_GRACE_PERIOD_SECONDS` and `_maybe_finalize_pending_end()`.
        self._pending_end_exit_code: Optional[int] = None
        self._pending_end_since: Optional[float] = None

    def start(self) -> None:
        self._backend.start()

    def is_alive(self) -> bool:
        return self._backend.is_alive()

    def pump(
        self,
        timeout: float = _DEFAULT_POLL_TIMEOUT_SECONDS,
        output_sink: Optional[Any] = None,
    ) -> None:
        """Process whatever output/events are currently available and
        return. Call this repeatedly in a loop (see `run_until_exit`) --
        each call does at most `timeout` seconds of waiting, never more.

        `output_sink`, if given, is called with each raw output chunk as it
        arrives (in addition to it always being logged) -- this is how
        `run_interactive` mirrors the child's output back to the real
        terminal without `pump` itself needing to know anything about
        terminals.
        """
        # Events before output, not the reverse: a shell hook writes the
        # "start" marker essentially synchronously with dispatching the
        # command, which necessarily happens before that command can have
        # produced any output. Reading output first risks receiving a
        # command's first output bytes in the same pump() call as its own
        # not-yet-processed start event, misattributing them to whatever
        # command (if any) was previously in flight.
        for event in self._backend.read_new_boundary_events():
            self._handle_event(event)

        chunk = self._backend.read_output(timeout=timeout)
        if chunk:
            self.log_writer.append_raw_output(chunk)
            if output_sink is not None:
                output_sink(chunk)
            if self._has_in_flight_command:
                self._in_flight_output += chunk

        self._maybe_finalize_pending_end()

    def _handle_event(self, event: Dict[str, Any]) -> None:
        kind = event.get("event")

        if kind == "start":
            if self._pending_end_exit_code is not None:
                # The previous command already got its "end" event and was
                # just waiting out its grace period for trailing output
                # (see _maybe_finalize_pending_end()) -- a new command
                # starting is itself proof no more of that output is
                # coming, so finalize it now with whatever accumulated
                # instead of waiting out the rest of the grace period.
                self._finalize_pending_end()
            # Still needed for the other case -- a previous command that
            # never got an "end" event at all (genuinely orphaned, not
            # pending): LogWriter.handle_command_start already discards
            # that on its own (see docs/plan.md's "钩子安装时机噪音"
            # handling).
            self.log_writer.handle_command_start(event.get("command", ""))
            self._has_in_flight_command = True
            self._in_flight_output = b""

        elif kind == "end":
            if not self._has_in_flight_command:
                # Orphan end with nothing pending -- LogWriter.handle_command_end
                # would just no-op on this too, but skip the decode/call
                # entirely rather than pass it a meaningless empty command.
                return
            # Do not finalize yet -- see _COMMAND_END_GRACE_PERIOD_SECONDS:
            # the real command output can still be in flight on the
            # separate ConPTY-output channel even though the marker-file
            # channel already reports this command as done.
            # `_has_in_flight_command`/`_in_flight_output` deliberately stay
            # as they are, so `pump()`'s output handling keeps appending to
            # this same command until it is actually finalized.
            self._pending_end_exit_code = event.get("exit_code", 0)
            self._pending_end_since = time.monotonic()

        # An event with an unrecognized "event" value is ignored rather than
        # raised: the marker channel is produced by a shell script, not a
        # typed API, so tolerating an unexpected line here is consistent
        # with how the backends already tolerate an unparseable one.

    def _maybe_finalize_pending_end(self) -> None:
        if self._pending_end_exit_code is None:
            return
        assert self._pending_end_since is not None
        if time.monotonic() - self._pending_end_since >= _COMMAND_END_GRACE_PERIOD_SECONDS:
            self._finalize_pending_end()

    def _finalize_pending_end(self) -> None:
        """Actually record the command whose "end" event already arrived
        (see `_handle_event`'s "end" branch and
        `_COMMAND_END_GRACE_PERIOD_SECONDS`), using whatever output has
        accumulated in `_in_flight_output` up to this point.
        """
        assert self._pending_end_exit_code is not None
        output_text = self._in_flight_output.decode("utf-8", errors="replace")
        self.log_writer.handle_command_end(self._pending_end_exit_code, output_text)
        self._pending_end_exit_code = None
        self._pending_end_since = None
        self._has_in_flight_command = False
        self._in_flight_output = b""
        enforce_retention_policy(self.log_writer.log_dir)

    def run_until_exit(self, timeout: float = _DEFAULT_POLL_TIMEOUT_SECONDS) -> None:
        """Start the backend and pump it until the child shell exits, then
        finalize the session. Blocks for the lifetime of the session.

        `finalize()` (which closes the backend, tearing down the child
        process) runs in a `finally` block: if this loop is interrupted by
        anything -- Ctrl+C hitting aicap's own process, an unexpected
        exception -- the child must still be torn down rather than left
        running as an orphan. Found the hard way during stage 6b manual
        verification: a KeyboardInterrupt here previously propagated straight
        out with no cleanup, leaking the child shell process.
        """
        self.start()
        try:
            while self.is_alive():
                self.pump(timeout=timeout)
            # The child may have exited with output/events still sitting in
            # the backend's buffers (its very last bit of output, or a final
            # unmatched "start" for the "exit" command itself) -- drain
            # those before finalizing rather than losing them.
            self.pump(timeout=timeout)
        finally:
            self.finalize()

    def _sync_terminal_size(
        self, terminal, timeout: float = _TERMINAL_SIZE_SYNC_TIMEOUT_SECONDS
    ) -> Optional[tuple]:
        """Best-effort: propagate the real terminal's current size to the
        backend (`terminal.get_size()` + `self._backend.resize()`), but
        never let this block the session forever if the underlying platform
        call hangs. Returns the synced `(rows, cols)`, or `None` if the sync
        did not complete (timed out or raised `OSError`) -- the caller uses
        this as the baseline for `_maybe_resync_terminal_size()`'s later
        change detection.

        Found the hard way during stage 6b real-world manual testing: this
        step occasionally never returned at all on Windows, even though the
        recorded child shell had spawned successfully and stayed alive and
        healthy the whole time -- confirmed independently via the OS process
        list, not guessed. Because the read/write loop had not been reached
        yet, nothing the child produced was ever mirrored back, which looks
        exactly like "aicap froze" even though only this one, in hindsight
        skippable, step was actually stuck (see docs/plan.md). Running it on
        a timeout-guarded daemon thread turns that into, at worst, "the
        terminal keeps its default size this one session" instead of a
        total hang. Only used for the *initial* sync -- see
        `_maybe_resync_terminal_size()` for the ongoing, mid-session case,
        which does not need this same guard (see its own docstring for why).
        """
        result: Dict[str, Any] = {}

        def _do_sync() -> None:
            try:
                rows, cols = terminal.get_size()
                self._backend.resize(rows, cols)
                result["ok"] = True
                result["rows"] = rows
                result["cols"] = cols
            except OSError as exc:
                result["ok"] = False
                result["error"] = str(exc)

        sync_thread = threading.Thread(target=_do_sync, daemon=True)
        sync_thread.start()
        sync_thread.join(timeout=timeout)
        # If the thread is still alive here, the underlying call is hung;
        # give up waiting on it and move on. It is a daemon thread, so it
        # cannot block process exit even if the hang never resolves.

        if result.get("ok"):
            return (result["rows"], result["cols"])
        return None

    def _maybe_resync_terminal_size(self, terminal, last_size: Optional[tuple]) -> Optional[tuple]:
        """Re-check the real terminal's current size against `last_size`
        every loop iteration and push a fresh `resize()` to the backend if
        it changed; returns the size to use as `last_size` on the next call.

        Confirmed as the actual root cause of a real garbled-output repro
        (docs/plan.md stage 6b garbled-output investigation, reported
        directly by the user): changing the real terminal's font size
        (Ctrl+Plus/Ctrl+Minus in Windows Terminal) changes its character
        grid dimensions -- the same kind of change `_sync_terminal_size()`
        already handles, just *after* the session has already started
        rather than only at the very beginning. Without this, the child
        keeps computing cursor-position escape sequences for the stale
        size, which the real terminal (now a different size) renders at
        the wrong column/row -- exactly the "typed text visually jumps to
        the wrong line" symptom.

        Deliberately not wrapped in `_sync_terminal_size()`'s
        timeout-guarded daemon thread: that guard exists because the
        *initial* sync was observed to occasionally hang before the
        read/write loop was ever reached, with nothing to fall back on.
        Here, `get_size()` (a single `GetConsoleScreenBufferInfo` call --
        never implicated in any hang found so far, unlike the input-side
        calls) runs once per already-running loop iteration; spawning a
        new thread that often would be pure overhead for no observed
        benefit.
        """
        try:
            current_size = terminal.get_size()
        except OSError:
            return last_size

        if current_size == last_size:
            return last_size

        try:
            self._backend.resize(*current_size)
            ok = True
        except OSError:
            ok = False

        if ok:
            # Clear the real terminal's visible screen and move the cursor
            # home (does not touch scrollback history -- only the current
            # view). Confirmed via a real repro (docs/plan.md) that
            # detecting and forwarding the size change alone is not
            # enough: the child's own console and the real terminal each
            # reflow their *own* screen content independently, and the
            # child's reflowed content is much shorter (just its own
            # banner/prompt) than the real terminal's (which also has
            # everything printed before the child ever started, e.g. this
            # session's own startup messages) -- so after a resize the two
            # disagree on which row is "the current line," and the
            # child's cursor-position escape sequences (computed relative
            # to its own, shorter reflow) land on the wrong row of the
            # real terminal. Clearing gives both sides a shared, empty
            # baseline at the moment of resize instead of two
            # independently-reflowed histories of different lengths. This
            # is a mitigation, not a structural fix -- it trades a visible
            # clear/flash on resize (worse during a fast burst of resizes,
            # e.g. holding Ctrl+Plus/Minus) for not landing new keystrokes
            # on top of stale content.
            terminal.write_stdout(b"\x1b[2J\x1b[H")

        return current_size

    def run_interactive(
        self,
        timeout: float = _INTERACTIVE_POLL_TIMEOUT_SECONDS,
        stdin_fd: Optional[int] = None,
        stdout_fd: Optional[int] = None,
        stdin_handle: Optional[int] = None,
        stdout_handle: Optional[int] = None,
    ) -> None:
        """Like `run_until_exit`, but also forwards the real terminal's
        stdin to the child shell and mirrors the child's output back to the
        real terminal -- this is what makes a session actually usable to
        type into (docs/plan.md stage 6b).

        aicap does not interpret any keystroke itself: the real terminal is
        put into raw mode (via `termios` on Unix, via Win32 console-mode
        flags on Windows -- see `aicap/terminal_io.py` and
        `aicap/windows_terminal_io.py`) so control sequences (Ctrl+C
        included) pass through as plain bytes to the child shell's own
        terminal driver, instead of the OS intercepting them on aicap's own
        process. As a second, independent layer of defense against Ctrl+C
        ever reaching aicap's own process -- raw mode alone was not
        observed to be reliable enough on its own during real (Windows
        Terminal + WSL) manual testing of the Unix half (see docs/plan.md
        stage 6b) -- SIGINT is explicitly ignored in this process for the
        duration of the session, the same way `tmux`/`ssh -t`/`script` all
        do while attached to a wrapped session, and restored to whatever it
        was before once the session ends. Blocks for the lifetime of the
        session.

        `stdin_fd`/`stdout_fd` (Unix) and `stdin_handle`/`stdout_handle`
        (Windows) default to the real process stdin/stdout; overriding them
        is only meant for tests driving this method against a synthetic
        pty/ConPTY standing in for "the real terminal" (see
        tests/test_recorder_interactive_unix.py and
        tests/test_recorder_interactive_windows.py), not for real usage.
        """
        if sys.platform == "win32":
            # Imported lazily: this module uses ctypes.windll, which does
            # not exist on non-Windows platforms.
            from aicap.windows_terminal_io import WindowsTerminalIO

            terminal_context = WindowsTerminalIO(
                stdin_handle=stdin_handle, stdout_handle=stdout_handle
            )
        else:
            # Imported lazily (see aicap/terminal_io.py's own module
            # docstring): this module fails to import at all on Windows (no
            # termios/tty).
            from aicap.terminal_io import UnixTerminalIO

            terminal_context = UnixTerminalIO(stdin_fd=stdin_fd, stdout_fd=stdout_fd)

        # Belt-and-suspenders alongside cli.py's own flush=True on its
        # pre-flight prints: from here on, output goes straight to the raw
        # console handle/fd (bypassing Python's stdout buffer entirely, see
        # UnixTerminalIO.write_stdout()/WindowsTerminalIO.write_stdout()).
        # Any caller output still sitting in the buffer at this point would
        # race the child's own early output for display order -- flushing
        # here makes that race impossible regardless of whether every caller
        # remembered flush=True on its own prints.
        sys.stdout.flush()

        self.start()
        # signal.signal() only works from the main thread of the main
        # interpreter (a hard Python/OS constraint, not a design choice) --
        # true for a real `aicap start` invocation, but not for
        # tests/test_recorder_interactive_unix.py, which runs
        # run_interactive() on a background thread so its main thread is
        # free to act as "the human" driving the synthetic pty. Skip the
        # SIGINT-ignore layer gracefully there rather than crashing; raw
        # mode (the other, independent layer of defense) still applies
        # regardless.
        original_sigint_handler = None
        sigint_handler_changed = False
        try:
            original_sigint_handler = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            sigint_handler_changed = True
        except ValueError:
            pass

        try:
            with terminal_context as terminal:
                # Clear the real terminal and put its cursor at row 1
                # before the child ever draws anything. Confirmed via a
                # real repro (docs/plan.md) as a necessary fix, independent
                # of any resize: the child's own line editor (PSReadLine)
                # numbers rows starting from its own session's row 0/1 --
                # but by the time the child starts, the real terminal's
                # cursor is already several rows down (aicap's own
                # pre-flight prints: "recording started", "session id",
                # plus the echo of the command the user typed to launch
                # aicap in the first place). The child's absolute
                # cursor-position escape sequences get mirrored to the real
                # terminal unchanged, so "row 1" in the child's own,
                # shorter-history coordinate system lands wherever the real
                # terminal's row 1 actually is in its longer history --
                # nowhere near the child's actual prompt. Clearing here
                # makes both sides start counting from the same row 1 at
                # the moment the child takes over.
                terminal.write_stdout(b"\x1b[2J\x1b[H")

                # Sync the child's PTY to the real terminal's actual size
                # before it renders anything meaningful (see
                # _sync_terminal_size() for why this is timeout-guarded).
                last_terminal_size = self._sync_terminal_size(terminal)

                while self.is_alive():
                    last_terminal_size = self._maybe_resync_terminal_size(terminal, last_terminal_size)

                    stdin_chunk = terminal.read_stdin(timeout=timeout)
                    if stdin_chunk:
                        self._backend.write_input(stdin_chunk)

                    self.pump(timeout=timeout, output_sink=terminal.write_stdout)
                # Drain whatever the child produced right before it exited,
                # same reasoning as run_until_exit()'s own trailing pump().
                self.pump(timeout=timeout, output_sink=terminal.write_stdout)
        finally:
            # Same reasoning as run_until_exit()'s finally: an interruption
            # here must still tear down the child rather than leak it, and
            # aicap's own signal handling must not stay altered after the
            # session ends. The `with` block above already guarantees the
            # terminal mode gets restored even on an exception (__exit__
            # runs as it unwinds), independently of this finally.
            if sigint_handler_changed:
                signal.signal(signal.SIGINT, original_sigint_handler)
            self.finalize()

    def finalize(self) -> None:
        """Wrap up the session: complete any still-open command (a clean
        `exit` legitimately leaves one dangling, see
        `LogWriter.finalize_session`), close the backend, and run one last
        retention sweep. Safe to call more than once.
        """
        if self._finalized:
            return
        self._finalized = True

        if self._pending_end_exit_code is not None:
            # A command's "end" event already arrived and it was only
            # waiting out its grace period for trailing output (see
            # _COMMAND_END_GRACE_PERIOD_SECONDS) when the session ended --
            # this has a real exit code, unlike a genuinely still-open
            # command, so finalize it properly rather than through
            # finalize_session()'s "no exit code was ever observed" path.
            self._finalize_pending_end()

        trailing_output = (
            self._in_flight_output.decode("utf-8", errors="replace")
            if self._has_in_flight_command
            else ""
        )
        self.log_writer.finalize_session(trailing_output=trailing_output)
        self._backend.close()
        enforce_retention_policy(self.log_writer.log_dir)
