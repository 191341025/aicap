"""Unit tests for aicap.log_writer.LogWriter.

All tests drive LogWriter with fully simulated start/end events -- no real
PTY or shell involved, per plan.md's "第 2 阶段" instructions (log_writer
is platform-independent and must be independently unit-testable).
"""

import json

import pytest

from aicap.log_writer import LogWriter

EXPECTED_INDEX_ENTRY_KEYS = {
    "sequence",
    "command",
    "started_at",
    "ended_at",
    "exit_code",
    "output_file",
    "is_complete",
    "is_pruned",
    "is_truncated",
}


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _read_text(path):
    return path.read_text(encoding="utf-8")


class TestAppendRawOutput:
    def test_raw_output_is_mirrored_into_session_log(self, tmp_path):
        writer = LogWriter(tmp_path)
        writer.append_raw_output(b"raw bytes from the pty\n")
        content = _read_text(writer.session_log_path)
        assert "raw bytes from the pty" in content

    def test_empty_bytes_is_a_safe_no_op(self, tmp_path):
        writer = LogWriter(tmp_path)
        before = _read_text(writer.session_log_path)
        writer.append_raw_output(b"")
        assert _read_text(writer.session_log_path) == before

    def test_interleaves_with_boundary_markers(self, tmp_path):
        writer = LogWriter(tmp_path)
        writer.handle_command_start("echo hi")
        writer.append_raw_output(b"hi\n")
        writer.handle_command_end(0, "hi\n")
        content = _read_text(writer.session_log_path)
        start_pos = content.find("[start]")
        raw_pos = content.find("hi\n")
        end_pos = content.find("[end]")
        assert start_pos < raw_pos < end_pos


class TestNormalStartEndFlow:
    def test_index_json_entry_matches_schema_after_start(self, tmp_path):
        writer = LogWriter(tmp_path)
        writer.handle_command_start("echo hello")

        index = _read_json(writer.index_path)
        assert len(index) == 1
        entry = index[0]
        assert set(entry.keys()) == EXPECTED_INDEX_ENTRY_KEYS
        assert entry["sequence"] == 1
        assert entry["command"] == "echo hello"
        assert entry["started_at"]  # non-empty timestamp
        assert entry["ended_at"] is None
        assert entry["exit_code"] is None
        assert entry["output_file"] is None
        assert entry["is_complete"] is False
        assert entry["is_pruned"] is False
        assert entry["is_truncated"] is False

    def test_index_json_entry_is_completed_after_end(self, tmp_path):
        writer = LogWriter(tmp_path)
        writer.handle_command_start("echo hello")
        writer.handle_command_end(exit_code=0, output="hello\n")

        index = _read_json(writer.index_path)
        assert len(index) == 1
        entry = index[0]
        assert entry["is_complete"] is True
        assert entry["exit_code"] == 0
        assert entry["ended_at"]
        assert entry["output_file"] == "commands/0001-echo-hello.log"
        assert entry["is_truncated"] is False
        assert entry["is_pruned"] is False

    def test_command_output_file_contains_ansi_stripped_text(self, tmp_path):
        writer = LogWriter(tmp_path)
        writer.handle_command_start("colorized")
        writer.handle_command_end(exit_code=0, output="\x1b[31mred text\x1b[0m\n")

        output_path = writer.session_dir / "commands" / "0001-colorized.log"
        assert output_path.exists()
        assert _read_text(output_path) == "red text\n"

    def test_latest_log_and_json_reflect_last_command(self, tmp_path):
        writer = LogWriter(tmp_path)
        writer.handle_command_start("first")
        writer.handle_command_end(exit_code=0, output="first output\n")
        writer.handle_command_start("second")
        writer.handle_command_end(exit_code=7, output="second output\n")

        assert _read_text(tmp_path / "latest.log") == "second output\n"
        latest_meta = _read_json(tmp_path / "latest.json")
        assert latest_meta["command"] == "second"
        assert latest_meta["exit_code"] == 7
        assert latest_meta["sequence"] == 2
        assert latest_meta["session_id"] == writer.session_id

    def test_multiple_commands_get_increasing_sequence_numbers(self, tmp_path):
        writer = LogWriter(tmp_path)
        for index in range(3):
            writer.handle_command_start(f"command-{index}")
            writer.handle_command_end(exit_code=0, output=f"output-{index}\n")

        index_data = _read_json(writer.index_path)
        assert [entry["sequence"] for entry in index_data] == [1, 2, 3]
        assert [entry["command"] for entry in index_data] == [
            "command-0",
            "command-1",
            "command-2",
        ]


class TestOutputTruncation:
    def test_output_over_size_cap_is_truncated_and_marked(self, tmp_path):
        writer = LogWriter(tmp_path)
        oversized_output = "a" * (LogWriter.MAX_COMMAND_OUTPUT_BYTES + 1024)

        writer.handle_command_start("dump-a-lot")
        writer.handle_command_end(exit_code=0, output=oversized_output)

        entry = _read_json(writer.index_path)[0]
        assert entry["is_truncated"] is True

        output_path = writer.session_dir / "commands" / "0001-dump-a-lot.log"
        written_bytes = output_path.read_bytes()
        assert len(written_bytes) == LogWriter.MAX_COMMAND_OUTPUT_BYTES

        latest_meta = _read_json(tmp_path / "latest.json")
        assert latest_meta["is_truncated"] is True

    def test_output_under_size_cap_is_not_truncated(self, tmp_path):
        writer = LogWriter(tmp_path)
        writer.handle_command_start("small")
        writer.handle_command_end(exit_code=0, output="just a bit of output\n")

        entry = _read_json(writer.index_path)[0]
        assert entry["is_truncated"] is False


class TestBoundaryEventNoiseIsDiscarded:
    """Covers plan.md's stage-0 finding: the first boundary event after a
    shell hook installs can be an orphan that never pairs into a complete
    start->end, and must be dropped rather than recorded as a command."""

    def test_orphan_end_with_no_prior_start_is_discarded(self, tmp_path):
        writer = LogWriter(tmp_path)
        # PowerShell/zsh stage-0 finding: a stray "end" event with nothing
        # to pair it with, right when the hook was installed.
        writer.handle_command_end(exit_code=0, output="noise")

        index_data = _read_json(writer.index_path)
        assert index_data == []
        assert list(writer.commands_dir.iterdir()) == []
        assert not (tmp_path / "latest.log").exists()

    def test_orphan_start_superseded_by_next_start_is_discarded(self, tmp_path):
        writer = LogWriter(tmp_path)
        # bash stage-0 finding: the hook-install line itself is misread as
        # a fake "start" that is never followed by a matching "end" before
        # the real first command's "start" arrives.
        writer.handle_command_start("PROMPT_COMMAND=aicap_preexec")
        writer.handle_command_start("echo real command")
        writer.handle_command_end(exit_code=0, output="real output\n")

        index_data = _read_json(writer.index_path)
        assert len(index_data) == 1
        assert index_data[0]["sequence"] == 1
        assert index_data[0]["command"] == "echo real command"

        command_files = list(writer.commands_dir.iterdir())
        assert len(command_files) == 1


class TestSessionDirectoryIsolation:
    def test_repeated_init_on_same_log_dir_creates_distinct_sessions(self, tmp_path):
        writer_one = LogWriter(tmp_path)
        writer_two = LogWriter(tmp_path)

        assert writer_one.session_id != writer_two.session_id
        assert writer_one.session_dir.exists()
        assert writer_two.session_dir.exists()

        session_dirs = list((tmp_path / "sessions").iterdir())
        assert len(session_dirs) == 2

    def test_first_session_data_is_not_overwritten_by_second(self, tmp_path):
        writer_one = LogWriter(tmp_path)
        writer_one.handle_command_start("first-session-command")
        writer_one.handle_command_end(exit_code=0, output="first session output\n")

        LogWriter(tmp_path)

        # writer_one's own index.json must still show its command untouched.
        index_data = _read_json(writer_one.index_path)
        assert len(index_data) == 1
        assert index_data[0]["command"] == "first-session-command"


class TestInterruptedSessionNotice:
    def test_status_md_flags_previous_incomplete_session(self, tmp_path):
        writer_one = LogWriter(tmp_path)
        writer_one.handle_command_start("some-long-running-thing")
        # Simulate a crash: no matching end event, no finalize_session()
        # call -- index.json is left with is_complete: false on disk,
        # exactly like a process that died mid-command.

        writer_two = LogWriter(tmp_path)

        status_text = _read_text(tmp_path / "STATUS.md")
        assert "interrupted" in status_text
        assert "some-long-running-thing" in status_text
        assert writer_one.session_id in status_text

    def test_status_md_has_no_notice_when_previous_session_completed_cleanly(
        self, tmp_path
    ):
        writer_one = LogWriter(tmp_path)
        writer_one.handle_command_start("clean-command")
        writer_one.handle_command_end(exit_code=0, output="done\n")
        writer_one.finalize_session()

        LogWriter(tmp_path)

        status_text = _read_text(tmp_path / "STATUS.md")
        assert "interrupted" not in status_text

    def test_no_notice_on_very_first_session(self, tmp_path):
        LogWriter(tmp_path)
        status_text = _read_text(tmp_path / "STATUS.md")
        assert "interrupted" not in status_text


class TestFinalizeSession:
    def test_finalize_completes_dangling_last_command(self, tmp_path):
        writer = LogWriter(tmp_path)
        writer.handle_command_start("exit")

        writer.finalize_session(trailing_output="goodbye\n")

        entry = _read_json(writer.index_path)[0]
        assert entry["is_complete"] is True
        assert entry["exit_code"] is None
        assert entry["ended_at"]
        assert entry["output_file"] == "commands/0001-exit.log"

        output_path = writer.session_dir / "commands" / "0001-exit.log"
        assert _read_text(output_path) == "goodbye\n"
        assert _read_text(tmp_path / "latest.log") == "goodbye\n"

    def test_finalize_with_nothing_pending_is_a_safe_no_op(self, tmp_path):
        writer = LogWriter(tmp_path)
        writer.handle_command_start("already-done")
        writer.handle_command_end(exit_code=0, output="done\n")

        # Should not raise, and should not disturb the already-completed entry.
        writer.finalize_session()

        entry = _read_json(writer.index_path)[0]
        assert entry["is_complete"] is True
        assert entry["exit_code"] == 0


class TestStatusMarkdown:
    def test_status_md_lists_recent_commands_newest_first(self, tmp_path):
        writer = LogWriter(tmp_path)
        for index in range(3):
            writer.handle_command_start(f"command-{index}")
            writer.handle_command_end(exit_code=index, output=f"output-{index}\n")

        status_text = _read_text(tmp_path / "STATUS.md")
        position_of_2 = status_text.index("command-2")
        position_of_1 = status_text.index("command-1")
        position_of_0 = status_text.index("command-0")
        assert position_of_2 < position_of_1 < position_of_0

    def test_command_left_unfinished_stays_incomplete_in_index_until_ended(
        self, tmp_path
    ):
        # STATUS.md is only regenerated on command-end/finalize per
        # plan.md (not on every start); the in-flight state itself is
        # still verifiable directly on index.json, which is what crash
        # detection (TestInterruptedSessionNotice) relies on.
        writer = LogWriter(tmp_path)
        writer.handle_command_start("still-running")

        index_data = _read_json(writer.index_path)
        assert index_data[0]["is_complete"] is False
