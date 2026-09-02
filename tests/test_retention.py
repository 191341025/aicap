"""Unit tests for aicap.retention.enforce_retention_policy.

Per the task instructions, these tests build session directories /
index.json / commands/*.log files by hand rather than driving them
through LogWriter -- that keeps each test focused on retention behavior
alone and avoids depending on LogWriter's event-sequencing rules.
"""

import json

import pytest

from aicap.retention import enforce_retention_policy


def _write_session(
    log_dir,
    session_id,
    entries,
):
    """Build one `sessions/{session_id}/` directory by hand.

    `entries` is a list of dicts, one per index.json record, with keys:
      - sequence (int, required)
      - started_at (str, required) -- ISO-ish, used for retention ordering
      - is_complete (bool, default True)
      - is_pruned (bool, default False)
      - output_size (int or None, default 0) -- bytes of dummy content to
        write to this entry's commands/*.log file; None means "no output
        file at all" (e.g. to model an in-flight command).

    Returns the session directory Path.
    """
    session_dir = log_dir / "sessions" / session_id
    commands_dir = session_dir / "commands"
    commands_dir.mkdir(parents=True)

    index_entries = []
    for spec in entries:
        sequence = spec["sequence"]
        is_complete = spec.get("is_complete", True)
        output_size = spec.get("output_size", 0)

        entry = {
            "sequence": sequence,
            "command": spec.get("command", f"cmd-{sequence}"),
            "started_at": spec["started_at"],
            "ended_at": spec["started_at"],
            "exit_code": spec.get("exit_code", 0),
            "output_file": None,
            "is_complete": is_complete,
            "is_pruned": spec.get("is_pruned", False),
            "is_truncated": False,
        }

        if output_size is not None:
            filename = f"{sequence:04d}-cmd.log"
            entry["output_file"] = f"commands/{filename}"
            (commands_dir / filename).write_bytes(b"x" * output_size)

        index_entries.append(entry)

    index_path = session_dir / "index.json"
    index_path.write_text(json.dumps(index_entries, indent=2), encoding="utf-8")
    return session_dir


def _read_index(session_dir):
    return json.loads((session_dir / "index.json").read_text(encoding="utf-8"))


def _entries_by_sequence(session_dir):
    return {entry["sequence"]: entry for entry in _read_index(session_dir)}


class TestPruneByCommandCount:
    def test_prunes_oldest_first_down_to_max_commands(self, tmp_path):
        # 5 complete commands, each with a distinct started_at, oldest to
        # newest by sequence. max_commands=3 should prune the 2 oldest
        # (sequence 1 and 2) and leave 3, 4, 5 alone.
        session_dir = _write_session(
            tmp_path,
            "session-a",
            [
                {"sequence": i, "started_at": f"2026-08-31T21:{i:02d}:00", "output_size": 100}
                for i in range(1, 6)
            ],
        )

        result = enforce_retention_policy(tmp_path, max_commands=3, max_size_bytes=10**9)

        assert result.pruned_command_count == 2
        assert result.freed_bytes == 200
        assert result.pruned_commands == [("session-a", 1), ("session-a", 2)]

        entries = _entries_by_sequence(session_dir)
        assert entries[1]["is_pruned"] is True
        assert entries[2]["is_pruned"] is True
        assert entries[3]["is_pruned"] is False
        assert entries[4]["is_pruned"] is False
        assert entries[5]["is_pruned"] is False

        # Index entries themselves are always kept in full -- only content
        # (the .log file) and the is_pruned flag change.
        assert entries[1]["sequence"] == 1
        assert entries[1]["command"] == "cmd-1"
        assert entries[1]["started_at"] == "2026-08-31T21:01:00"

        # The pruned entries' log files are gone; the kept ones remain.
        commands_dir = session_dir / "commands"
        assert not (commands_dir / "0001-cmd.log").exists()
        assert not (commands_dir / "0002-cmd.log").exists()
        assert (commands_dir / "0003-cmd.log").exists()
        assert (commands_dir / "0004-cmd.log").exists()
        assert (commands_dir / "0005-cmd.log").exists()

    def test_exactly_at_limit_prunes_nothing(self, tmp_path):
        session_dir = _write_session(
            tmp_path,
            "session-a",
            [
                {"sequence": i, "started_at": f"2026-08-31T21:{i:02d}:00", "output_size": 10}
                for i in range(1, 4)
            ],
        )

        result = enforce_retention_policy(tmp_path, max_commands=3, max_size_bytes=10**9)

        assert result.pruned_command_count == 0
        assert result.freed_bytes == 0
        entries = _entries_by_sequence(session_dir)
        assert all(entry["is_pruned"] is False for entry in entries.values())


class TestPruneBySizeEvenWithoutCountOverage(object):
    def test_prunes_when_total_size_exceeds_cap_regardless_of_count(self, tmp_path):
        # Only 3 commands (well under a generous max_commands), but their
        # combined output size exceeds max_size_bytes -- size alone must
        # trigger pruning.
        session_dir = _write_session(
            tmp_path,
            "session-a",
            [
                {"sequence": 1, "started_at": "2026-08-31T21:01:00", "output_size": 50},
                {"sequence": 2, "started_at": "2026-08-31T21:02:00", "output_size": 50},
                {"sequence": 3, "started_at": "2026-08-31T21:03:00", "output_size": 50},
            ],
        )

        # Total is 150 bytes; cap it at 80 -- must prune the oldest (seq 1,
        # 50 bytes) leaving 100, still over; prune seq 2 as well leaving 50,
        # which is <= 80, so seq 3 survives.
        result = enforce_retention_policy(tmp_path, max_commands=100, max_size_bytes=80)

        assert result.pruned_command_count == 2
        assert result.freed_bytes == 100
        entries = _entries_by_sequence(session_dir)
        assert entries[1]["is_pruned"] is True
        assert entries[2]["is_pruned"] is True
        assert entries[3]["is_pruned"] is False


class TestIncompleteCommandsAreNeverPruned:
    def test_incomplete_command_survives_even_when_oldest(self, tmp_path):
        session_dir = _write_session(
            tmp_path,
            "session-a",
            [
                # Oldest of all, but still in flight -- must never be pruned.
                {
                    "sequence": 1,
                    "started_at": "2026-08-31T20:00:00",
                    "is_complete": False,
                    "output_size": None,
                },
                {"sequence": 2, "started_at": "2026-08-31T21:01:00", "output_size": 100},
                {"sequence": 3, "started_at": "2026-08-31T21:02:00", "output_size": 100},
                {"sequence": 4, "started_at": "2026-08-31T21:03:00", "output_size": 100},
            ],
        )

        # max_commands=2 among the *eligible* (complete) entries: 2, 3, 4
        # are all complete, so the oldest complete one (seq 2) must be
        # pruned to get down to 2 -- but seq 1 (incomplete) must be
        # untouched throughout.
        result = enforce_retention_policy(tmp_path, max_commands=2, max_size_bytes=10**9)

        entries = _entries_by_sequence(session_dir)
        assert entries[1]["is_complete"] is False
        assert entries[1]["is_pruned"] is False
        assert entries[2]["is_pruned"] is True
        assert entries[3]["is_pruned"] is False
        assert entries[4]["is_pruned"] is False
        assert result.pruned_commands == [("session-a", 2)]


class TestAlreadyPrunedEntriesAreIdempotent:
    def test_already_pruned_entry_is_not_reprocessed_or_recounted(self, tmp_path):
        # seq 1 is already pruned (no log file on disk, matching what a
        # real prior prune would have left behind) and must not count
        # toward "current occupancy", nor get touched again.
        session_dir = _write_session(
            tmp_path,
            "session-a",
            [
                {
                    "sequence": 1,
                    "started_at": "2026-08-31T20:00:00",
                    "is_pruned": True,
                    "output_size": None,
                },
                {"sequence": 2, "started_at": "2026-08-31T21:01:00", "output_size": 100},
                {"sequence": 3, "started_at": "2026-08-31T21:02:00", "output_size": 100},
            ],
        )

        # max_commands=2 counts only the 2 eligible entries (seq 2, 3),
        # which is exactly at budget -- nothing should be pruned, and
        # certainly not seq 1 again.
        result = enforce_retention_policy(tmp_path, max_commands=2, max_size_bytes=10**9)

        assert result.pruned_command_count == 0
        entries = _entries_by_sequence(session_dir)
        assert entries[1]["is_pruned"] is True  # unchanged, still true
        assert entries[2]["is_pruned"] is False
        assert entries[3]["is_pruned"] is False

    def test_calling_twice_in_a_row_is_safe_and_second_call_is_a_no_op(self, tmp_path):
        session_dir = _write_session(
            tmp_path,
            "session-a",
            [
                {"sequence": i, "started_at": f"2026-08-31T21:{i:02d}:00", "output_size": 100}
                for i in range(1, 6)
            ],
        )

        first = enforce_retention_policy(tmp_path, max_commands=3, max_size_bytes=10**9)
        assert first.pruned_command_count == 2

        second = enforce_retention_policy(tmp_path, max_commands=3, max_size_bytes=10**9)
        assert second.pruned_command_count == 0
        assert second.freed_bytes == 0
        assert second.pruned_commands == []

        entries = _entries_by_sequence(session_dir)
        assert entries[1]["is_pruned"] is True
        assert entries[2]["is_pruned"] is True
        assert entries[3]["is_pruned"] is False


class TestCrossSessionGlobalBudget:
    def test_budget_and_order_are_global_across_sessions_not_per_session(self, tmp_path):
        # Two sessions, 3 commands each (6 total). A global max_commands=4
        # must prune the 2 globally-oldest commands, which both happen to
        # live in session-old (since it is entirely older than
        # session-new) -- not one from each session.
        session_old = _write_session(
            tmp_path,
            "session-old",
            [
                {"sequence": 1, "started_at": "2026-08-31T10:00:00", "output_size": 10},
                {"sequence": 2, "started_at": "2026-08-31T10:01:00", "output_size": 10},
                {"sequence": 3, "started_at": "2026-08-31T10:02:00", "output_size": 10},
            ],
        )
        session_new = _write_session(
            tmp_path,
            "session-new",
            [
                {"sequence": 1, "started_at": "2026-08-31T12:00:00", "output_size": 10},
                {"sequence": 2, "started_at": "2026-08-31T12:01:00", "output_size": 10},
                {"sequence": 3, "started_at": "2026-08-31T12:02:00", "output_size": 10},
            ],
        )

        result = enforce_retention_policy(tmp_path, max_commands=4, max_size_bytes=10**9)

        assert result.pruned_command_count == 2
        assert result.pruned_commands == [
            ("session-old", 1),
            ("session-old", 2),
        ]

        old_entries = _entries_by_sequence(session_old)
        new_entries = _entries_by_sequence(session_new)
        assert old_entries[1]["is_pruned"] is True
        assert old_entries[2]["is_pruned"] is True
        assert old_entries[3]["is_pruned"] is False
        assert all(entry["is_pruned"] is False for entry in new_entries.values())

    def test_size_budget_is_summed_across_sessions(self, tmp_path):
        session_a = _write_session(
            tmp_path,
            "session-a",
            [{"sequence": 1, "started_at": "2026-08-31T10:00:00", "output_size": 60}],
        )
        session_b = _write_session(
            tmp_path,
            "session-b",
            [{"sequence": 1, "started_at": "2026-08-31T11:00:00", "output_size": 60}],
        )

        # Neither session alone exceeds 100 bytes, but the two together
        # (120 bytes) do -- the global sum must be what triggers pruning.
        result = enforce_retention_policy(tmp_path, max_commands=100, max_size_bytes=100)

        assert result.pruned_command_count == 1
        assert result.pruned_commands == [("session-a", 1)]
        assert _entries_by_sequence(session_a)[1]["is_pruned"] is True
        assert _entries_by_sequence(session_b)[1]["is_pruned"] is False


class TestNoOpWhenNothingToClean:
    def test_empty_log_dir_is_a_safe_no_op(self, tmp_path):
        result = enforce_retention_policy(tmp_path)

        assert result.pruned_command_count == 0
        assert result.freed_bytes == 0
        assert result.pruned_commands == []

    def test_log_dir_with_no_sessions_directory_is_a_safe_no_op(self, tmp_path):
        (tmp_path / "latest.log").write_text("hello", encoding="utf-8")

        result = enforce_retention_policy(tmp_path)

        assert result.pruned_command_count == 0

    def test_everything_within_budget_is_a_safe_no_op(self, tmp_path):
        session_dir = _write_session(
            tmp_path,
            "session-a",
            [
                {"sequence": 1, "started_at": "2026-08-31T21:01:00", "output_size": 10},
                {"sequence": 2, "started_at": "2026-08-31T21:02:00", "output_size": 10},
            ],
        )

        result = enforce_retention_policy(tmp_path, max_commands=50, max_size_bytes=10**9)

        assert result.pruned_command_count == 0
        assert result.freed_bytes == 0
        entries = _entries_by_sequence(session_dir)
        assert entries[1]["is_pruned"] is False
        assert entries[2]["is_pruned"] is False
