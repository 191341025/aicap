"""Tests for aicap.recorder.Recorder's orchestration logic.

Uses a fake PtyBackendBase implementation instead of a real shell: this
module's job (pairing boundary events, accumulating per-command output,
driving LogWriter/retention) is platform-independent and has nothing to do
with real PTY mechanics -- those are already covered separately by manual
verification of unix_backend.py (stage 4) and test_windows_backend.py
(stage 5). Testing Recorder against a fake backend is faster, deterministic,
and needs no real shell/OS-specific setup.
"""

import json

from aicap.pty_backend.base import PtyBackendBase
from aicap.recorder import Recorder


class FakePtyBackend(PtyBackendBase):
    """A scripted `PtyBackendBase` driven by a list of "ticks".

    Each tick is `(events, output)`: the boundary events and raw output
    bytes that become available together, during one `Recorder.pump()`
    call. This mirrors real timing (a command's start event and its output
    are not interleaved arbitrarily -- the start event is always available
    no later than the output it precedes) explicitly, rather than letting a
    naive independent-queues model produce misleading interleavings that
    could never happen against a real backend.

    One tick is consumed per `pump()` call: both `read_output()` and
    `read_new_boundary_events()` see the same current tick's data until
    *both* have been called once, at which point the tick advances.
    `is_alive()` stays True until every tick has been consumed.
    """

    def __init__(self, ticks) -> None:
        self._ticks = list(ticks)
        self._started = False
        self._closed = False
        self._output_seen_this_tick = False
        self._events_seen_this_tick = False

    def start(self) -> None:
        self._started = True

    def is_alive(self) -> bool:
        return self._started and not self._closed and bool(self._ticks)

    def write_input(self, data: bytes) -> None:
        pass

    def resize(self, rows: int, cols: int) -> None:
        pass

    def read_output(self, timeout: float) -> bytes:
        if not self._ticks:
            return b""
        output = self._ticks[0][1]
        self._output_seen_this_tick = True
        self._advance_if_tick_fully_consumed()
        return output

    def read_new_boundary_events(self):
        if not self._ticks:
            return []
        events = self._ticks[0][0]
        self._events_seen_this_tick = True
        self._advance_if_tick_fully_consumed()
        return events

    def _advance_if_tick_fully_consumed(self) -> None:
        if self._output_seen_this_tick and self._events_seen_this_tick:
            self._ticks.pop(0)
            self._output_seen_this_tick = False
            self._events_seen_this_tick = False

    def close(self) -> None:
        self._closed = True


class ExplodingPtyBackend(FakePtyBackend):
    """Like `FakePtyBackend`, but `read_new_boundary_events()` raises
    `KeyboardInterrupt` after `explode_after_calls` calls -- simulates the
    real bug found during stage 6b manual verification (Ctrl+C interrupting
    `run_until_exit()`'s loop left the child process orphaned because
    `finalize()` was never reached) to prove `close()` is now always called.
    """

    def __init__(self, ticks, explode_after_calls: int) -> None:
        super().__init__(ticks)
        self._explode_after_calls = explode_after_calls
        self._call_count = 0

    def read_new_boundary_events(self):
        self._call_count += 1
        if self._call_count > self._explode_after_calls:
            raise KeyboardInterrupt("simulated interruption")
        return super().read_new_boundary_events()


def _session_dir(log_dir):
    return next((log_dir / "sessions").iterdir())


def _read_index(log_dir):
    return json.loads((_session_dir(log_dir) / "index.json").read_text(encoding="utf-8"))


def test_single_command_is_recorded_end_to_end(tmp_path):
    backend = FakePtyBackend(
        ticks=[
            ([{"event": "start", "command": "echo hi"}], b""),
            ([], b"hi\n"),
            ([{"event": "end", "exit_code": 0}], b""),
            ([{"event": "start", "command": "exit"}], b""),
        ]
    )

    recorder = Recorder(str(tmp_path), backend=backend)
    recorder.run_until_exit()

    index = _read_index(tmp_path)
    echo_entry = next(e for e in index if e["command"] == "echo hi")
    assert echo_entry["exit_code"] == 0
    assert echo_entry["is_complete"] is True

    captured = (_session_dir(tmp_path) / echo_entry["output_file"]).read_text(encoding="utf-8")
    assert "hi" in captured


def test_dangling_last_command_is_finalized_not_left_incomplete(tmp_path):
    # "exit" itself never gets a matching "end" event in real usage (the
    # shell dies before the next prompt/boundary event could fire) --
    # finalize() must complete it, not leave is_complete: false forever.
    backend = FakePtyBackend(ticks=[([{"event": "start", "command": "exit"}], b"")])

    recorder = Recorder(str(tmp_path), backend=backend)
    recorder.run_until_exit()

    index = _read_index(tmp_path)
    exit_entry = next(e for e in index if e["command"] == "exit")
    assert exit_entry["is_complete"] is True
    assert exit_entry["exit_code"] is None


def test_orphan_end_with_no_pending_start_does_not_crash_or_record(tmp_path):
    backend = FakePtyBackend(
        ticks=[
            ([{"event": "end", "exit_code": 0}], b""),  # hook-install noise
            ([{"event": "start", "command": "echo hi"}], b""),
            ([{"event": "end", "exit_code": 0}], b""),
            ([{"event": "start", "command": "exit"}], b""),
        ]
    )

    recorder = Recorder(str(tmp_path), backend=backend)
    recorder.run_until_exit()  # must not raise

    index = _read_index(tmp_path)
    assert [e["command"] for e in index] == ["echo hi", "exit"]


def test_multiple_commands_each_get_their_own_output(tmp_path):
    backend = FakePtyBackend(
        ticks=[
            ([{"event": "start", "command": "echo first"}], b""),
            ([], b"first\n"),
            ([{"event": "end", "exit_code": 0}], b""),
            ([{"event": "start", "command": "echo second"}], b""),
            ([], b"second\n"),
            ([{"event": "end", "exit_code": 0}], b""),
            ([{"event": "start", "command": "exit"}], b""),
        ]
    )

    recorder = Recorder(str(tmp_path), backend=backend)
    recorder.run_until_exit()

    session_dir = _session_dir(tmp_path)
    index = _read_index(tmp_path)

    first_entry = next(e for e in index if e["command"] == "echo first")
    second_entry = next(e for e in index if e["command"] == "echo second")
    first_output = (session_dir / first_entry["output_file"]).read_text(encoding="utf-8")
    second_output = (session_dir / second_entry["output_file"]).read_text(encoding="utf-8")

    assert "first" in first_output and "second" not in first_output
    assert "second" in second_output and "first" not in second_output


def test_output_arriving_after_the_end_event_is_still_captured(tmp_path):
    # Real repro (docs/plan.md stage 6b): the marker-file channel and the
    # real console-output channel have no ordering guarantee relative to
    # each other. For a fast command, both its "start" and "end" marker
    # events can land before the shell has actually finished flushing its
    # formatted output through -- confirmed via a real `ls`/`dir` session
    # whose recorded output was empty because of exactly this. The output
    # arriving one tick *after* "end", with a further command starting
    # right after, must still end up attributed to the first command, not
    # lost or merged into the second one's output.
    backend = FakePtyBackend(
        ticks=[
            ([{"event": "start", "command": "ls"}], b""),
            ([{"event": "end", "exit_code": 0}], b""),  # end arrives before any output
            ([], b"directory listing\n"),  # real output trickles in late
            ([{"event": "start", "command": "echo second"}], b""),
            ([], b"second\n"),
            ([{"event": "end", "exit_code": 0}], b""),
            ([{"event": "start", "command": "exit"}], b""),
        ]
    )

    recorder = Recorder(str(tmp_path), backend=backend)
    recorder.run_until_exit()

    session_dir = _session_dir(tmp_path)
    index = _read_index(tmp_path)

    ls_entry = next(e for e in index if e["command"] == "ls")
    second_entry = next(e for e in index if e["command"] == "echo second")
    assert ls_entry["is_complete"] is True
    assert ls_entry["exit_code"] == 0

    ls_output = (session_dir / ls_entry["output_file"]).read_text(encoding="utf-8")
    second_output = (session_dir / second_entry["output_file"]).read_text(encoding="utf-8")
    assert "directory listing" in ls_output
    assert "second" not in ls_output
    assert "directory listing" not in second_output


def test_output_arriving_after_the_end_event_at_session_close_is_still_captured(tmp_path):
    # Same race as above, but the session ends (a clean "exit") before any
    # further command starts -- finalize() itself must pick up the
    # already-arrived-but-not-yet-finalized output rather than discarding
    # it or reporting no exit code (this command *did* get a real exit
    # code, unlike a genuinely still-open command).
    backend = FakePtyBackend(
        ticks=[
            ([{"event": "start", "command": "ls"}], b""),
            ([{"event": "end", "exit_code": 0}], b""),
            ([], b"directory listing\n"),
        ]
    )

    recorder = Recorder(str(tmp_path), backend=backend)
    recorder.run_until_exit()

    index = _read_index(tmp_path)
    ls_entry = next(e for e in index if e["command"] == "ls")
    assert ls_entry["is_complete"] is True
    assert ls_entry["exit_code"] == 0

    session_dir = _session_dir(tmp_path)
    ls_output = (session_dir / ls_entry["output_file"]).read_text(encoding="utf-8")
    assert "directory listing" in ls_output


def test_raw_output_between_commands_is_not_attributed_to_any_command(tmp_path):
    # Output that arrives while no command is "in flight" (prompt redraw
    # noise, hook-install noise) should be mirrored to session.log (via
    # append_raw_output) but never end up inside a commands/*.log file.
    backend = FakePtyBackend(
        ticks=[
            ([], b"noise-before-any-command\n"),
            ([{"event": "start", "command": "echo hi"}], b""),
            ([], b"hi\n"),
            ([{"event": "end", "exit_code": 0}], b""),
            ([{"event": "start", "command": "exit"}], b""),
        ]
    )

    recorder = Recorder(str(tmp_path), backend=backend)
    recorder.run_until_exit()

    session_dir = _session_dir(tmp_path)
    index = _read_index(tmp_path)
    echo_entry = next(e for e in index if e["command"] == "echo hi")
    captured = (session_dir / echo_entry["output_file"]).read_text(encoding="utf-8")
    assert "noise-before-any-command" not in captured

    session_log = (session_dir / "session.log").read_text(encoding="utf-8")
    assert "noise-before-any-command" in session_log


def test_run_until_exit_closes_backend_even_when_interrupted(tmp_path):
    backend = ExplodingPtyBackend(
        ticks=[
            ([{"event": "start", "command": "echo hi"}], b""),
            ([], b"hi\n"),
        ],
        explode_after_calls=1,
    )
    recorder = Recorder(str(tmp_path), backend=backend)

    try:
        recorder.run_until_exit()
        assert False, "expected the simulated KeyboardInterrupt to propagate"
    except KeyboardInterrupt:
        pass

    assert backend._closed is True


def test_finalize_is_idempotent(tmp_path):
    backend = FakePtyBackend(ticks=[([{"event": "start", "command": "exit"}], b"")])

    recorder = Recorder(str(tmp_path), backend=backend)
    recorder.run_until_exit()
    recorder.finalize()  # must not raise or double-write
