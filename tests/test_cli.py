"""Tests for aicap.cli.

`start` is tested with a fake Recorder injected via monkeypatch, not a real
shell -- this module's job is argument parsing and wiring, which has nothing
to do with real PTY mechanics (already covered elsewhere).
"""

import aicap.cli as cli


class FakeRecorder:
    """Stands in for aicap.recorder.Recorder in start-command tests.

    Tracks whether run_interactive() was actually called -- this is exactly
    what a real bug (docs/plan.md stage 6b: `_run_start` was left calling
    the non-interactive run_until_exit() after run_interactive() was added,
    so a real `aicap start` spawned a child shell nothing was ever connected
    to) slipped past unnoticed, so the tests must pin it down explicitly
    rather than just checking "something ran to completion". `_run_start`
    now calls run_interactive() unconditionally on every platform (both
    Unix and Windows implement it as of stage 6b's Windows half).
    """

    instances = []

    def __init__(self, log_dir):
        self.log_dir = log_dir
        self.run_interactive_called = False

        class _FakeLogWriter:
            session_id = "fake-session-id"

        self.log_writer = _FakeLogWriter()
        FakeRecorder.instances.append(self)

    def run_interactive(self):
        self.run_interactive_called = True


def test_no_arguments_prints_help_and_exits_zero(capsys):
    exit_code = cli.main([])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "start" in captured.out
    assert "status" in captured.out


def test_start_requires_log_dir_argument():
    try:
        cli.main(["start"])
        assert False, "expected SystemExit for a missing required argument"
    except SystemExit as exc:
        assert exc.code == 2


def test_start_uses_run_interactive_regardless_of_platform(monkeypatch, tmp_path, capsys):
    # Regression test for the exact bug found during stage 6b manual
    # verification: aicap start must actually connect the real terminal
    # (run_interactive()), not silently fall back to the non-interactive
    # orchestration-only path. Parametrized-by-hand over both platform
    # values since _run_start no longer branches on sys.platform at all.
    for platform_value in ("win32", "linux"):
        FakeRecorder.instances = []
        monkeypatch.setattr(cli, "Recorder", FakeRecorder)
        monkeypatch.setattr(cli.sys, "platform", platform_value)

        exit_code = cli.main(["start", str(tmp_path)])

        assert exit_code == 0
        assert len(FakeRecorder.instances) == 1
        assert FakeRecorder.instances[0].log_dir == str(tmp_path)
        assert FakeRecorder.instances[0].run_interactive_called is True

        captured = capsys.readouterr()
        assert str(tmp_path) in captured.out
        assert "fake-session-id" in captured.out


def test_status_prints_status_md_contents(tmp_path, capsys):
    (tmp_path / "STATUS.md").write_text("# aicap status\n\nsomething\n", encoding="utf-8")

    exit_code = cli.main(["status", str(tmp_path)])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "aicap status" in captured.out
    assert "something" in captured.out


def test_status_reports_no_recording_when_status_md_is_missing(tmp_path, capsys):
    exit_code = cli.main(["status", str(tmp_path)])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert str(tmp_path) in captured.out
