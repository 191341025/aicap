"""Enforce the retention policy described in plan.md's "保留策略" section.

`log_writer.py` already creates every `index.json` entry with
`"is_pruned": false` and never flips it -- this module is the piece that
actually flips it, and actually deletes the corresponding
`commands/NNNN-xxx.log` file body. Per plan.md (P4, non-destructive): the
`index.json` *entry* itself is never deleted, only its recorded content.

Scope note (see docs/plan.md "已确认决策" #3): a `log_dir` can contain many
`sessions/{session_id}/` directories, and the retention policy ("most
recent N commands" / "total size") is a *global* budget across all of
them, not a per-session limit of N each. So this module scans every
session under a `log_dir`, pools all eligible commands together, and
prunes the globally oldest ones first -- it does not touch `LogWriter`
itself, which only ever knows about the one session it is currently
writing.

Pruning eligibility (this module's own design judgment, plan.md does not
spell it out explicitly but it follows from the same non-destructive/safety
spirit as the rest of the retention design): only entries with
`is_complete: true` are eligible. A command that is still in flight (or was
left `is_complete: false` by a crash, per plan.md's crash-detection
section) has no finished output yet -- or output whose completeness is
already in question -- so pruning it would either race the writer still
producing that file, or destroy the last evidence of what an interrupted
command was doing. Leaving incomplete entries alone is a strict safety
margin, not a documented requirement.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from aicap.log_writer import LogWriter, PathLike

# plan.md "保留策略" section: "默认保留最近 50 条命令的完整内容，或总大小不
# 超过 200MB（先触发者生效）". These are just the defaults; both are meant
# to be overridable (plan.md: "--max-commands/--max-size 可覆盖"), which is
# why they are plain function parameters below rather than baked-in
# constants used directly in the logic.
DEFAULT_MAX_COMMANDS = 50

_BYTES_PER_MEGABYTE = 1024 * 1024
DEFAULT_MAX_SIZE_BYTES = 200 * _BYTES_PER_MEGABYTE  # 200 MB, per plan.md


@dataclass(frozen=True)
class RetentionResult:
    """Outcome of one `enforce_retention_policy()` call.

    Kept as a small, explicit value object (rather than a bare tuple or
    dict) so callers -- a future `recorder.py` reporting to the user, or a
    test asserting on the outcome -- get named, self-documenting fields.
    """

    # How many command entries were newly flipped to is_pruned=true by
    # this call (entries that were already pruned before the call don't
    # count -- this is "work done this call", not "total pruned so far").
    pruned_command_count: int

    # Total bytes reclaimed by deleting those entries' commands/*.log
    # files (0 for any entry whose file was already missing on disk).
    freed_bytes: int

    # (session_id, sequence) for every entry pruned by this call, in the
    # order they were pruned (oldest-first). Useful for tests and for a
    # future caller that wants to log/report specifics, not just counts.
    pruned_commands: List[Tuple[str, int]] = field(default_factory=list)


@dataclass
class _PruneCandidate:
    """One is_complete=true / is_pruned=false index.json entry, gathered
    from across all sessions, with everything needed to sort it and prune
    it without re-reading anything from disk.
    """

    session_id: str
    index_position: int
    sequence: int
    sort_key: Tuple[datetime, str, int]
    output_path: Optional[Path]
    output_size_bytes: int


def _parse_started_at(started_at: Optional[str]) -> datetime:
    """Parse an index.json `started_at` string into a `datetime` for
    chronological sorting.

    Per the task's explicit instruction, sorting uses `started_at` (a
    value this project writes itself, via `LogWriter`'s `_now_iso()`) --
    not filesystem mtime, which could drift from the recorded timeline if
    files are copied/moved.

    A missing or unparseable value is treated as the oldest possible
    timestamp (`datetime.min`) rather than raising: it is safer for a
    retention sweep to prune a command with corrupt/missing timing data
    first than to crash the whole cross-session sweep over one bad entry.
    """
    if started_at:
        try:
            return datetime.fromisoformat(started_at)
        except ValueError:
            pass
    return datetime.min


def _write_index_json(index_path: Path, index_entries: List[Dict[str, Any]]) -> None:
    """Write `index_entries` back to `index_path`.

    Mirrors `log_writer.py`'s own JSON formatting choices (pretty-printed,
    UTF-8, no BOM, `ensure_ascii=False` so a non-ASCII `command` value --
    e.g. a Chinese argument the user actually typed -- round-trips
    unchanged rather than getting escaped) so retention-modified
    `index.json` files stay byte-style-consistent with ones `LogWriter`
    writes directly.
    """
    with open(index_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(index_entries, handle, indent=2, ensure_ascii=False)


def _load_session_index(index_path: Path) -> Optional[List[Dict[str, Any]]]:
    """Load one session's index.json, or None if it can't be read.

    A missing/corrupt index.json for one session is skipped rather than
    raised -- a malformed or half-written session directory should not
    block the retention sweep from doing its job for every other, healthy
    session sharing the same log_dir.
    """
    try:
        raw = index_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    return data


def enforce_retention_policy(
    log_dir: PathLike,
    max_commands: int = DEFAULT_MAX_COMMANDS,
    max_size_bytes: int = DEFAULT_MAX_SIZE_BYTES,
) -> RetentionResult:
    """Prune the oldest eligible command output across every session under
    `log_dir` until both the command count and total output size are
    within budget (or there is nothing left eligible to prune).

    "Eligible" means `is_complete: true` and `is_pruned: false` -- see the
    module docstring for why incomplete commands are excluded. Already
    pruned entries (`is_pruned: true`) are also excluded, which is what
    makes repeated calls idempotent: an entry that was pruned by an
    earlier call is never reconsidered or double-counted.

    Whichever limit is hit first governs how much gets pruned (plan.md:
    "先触发者生效") -- both are checked on every iteration, so pruning
    stops as soon as *both* the count and the size are within budget.

    This function only ever sets `is_pruned: true` and deletes the
    matching `commands/NNNN-xxx.log` file; the `index.json` entry itself
    (command text, timestamps, exit code, ...) is always kept in full,
    per plan.md's non-destructive retention rule.

    Safe to call with nothing to prune (empty log_dir, or everything
    already within budget): returns a zero-valued `RetentionResult`
    without touching any file.
    """
    log_dir = Path(log_dir)
    sessions_root = log_dir / LogWriter.SESSIONS_DIRNAME
    if not sessions_root.is_dir():
        return RetentionResult(pruned_command_count=0, freed_bytes=0, pruned_commands=[])

    # session_id -> (session_dir, index_entries). Loaded once up front so
    # every candidate's index_position stays valid to mutate in place, and
    # so each session is written back to disk at most once at the end.
    sessions: Dict[str, Tuple[Path, List[Dict[str, Any]]]] = {}
    candidates: List[_PruneCandidate] = []

    for session_dir in sorted(entry for entry in sessions_root.iterdir() if entry.is_dir()):
        index_path = session_dir / LogWriter.INDEX_FILENAME
        index_entries = _load_session_index(index_path)
        if index_entries is None:
            continue

        sessions[session_dir.name] = (session_dir, index_entries)

        for position, entry in enumerate(index_entries):
            if not entry.get("is_complete", False):
                continue  # in-flight or crash-interrupted -- never pruned, see module docstring
            if entry.get("is_pruned", False):
                continue  # already pruned -- idempotency: don't reprocess or recount

            output_file = entry.get("output_file")
            output_path = (session_dir / output_file) if output_file else None
            output_size_bytes = (
                output_path.stat().st_size
                if output_path is not None and output_path.is_file()
                else 0
            )

            candidates.append(
                _PruneCandidate(
                    session_id=session_dir.name,
                    index_position=position,
                    sequence=entry.get("sequence", 0),
                    sort_key=(
                        _parse_started_at(entry.get("started_at")),
                        session_dir.name,
                        entry.get("sequence", 0),
                    ),
                    output_path=output_path,
                    output_size_bytes=output_size_bytes,
                )
            )

    # Oldest-first, globally across all sessions (see module docstring on
    # why this is a cross-session pool, not a per-session limit). Ties
    # (identical started_at) break on session_id then sequence purely for
    # deterministic ordering -- session_id sorts chronologically in
    # practice since LogWriter names sessions "{timestamp}-{suffix}".
    candidates.sort(key=lambda candidate: candidate.sort_key)

    remaining_count = len(candidates)
    remaining_size_bytes = sum(candidate.output_size_bytes for candidate in candidates)

    pruned_commands: List[Tuple[str, int]] = []
    freed_bytes = 0
    modified_session_ids = set()

    for candidate in candidates:
        if remaining_count <= max_commands and remaining_size_bytes <= max_size_bytes:
            break  # both budgets satisfied -- nothing older needs pruning

        if candidate.output_path is not None:
            candidate.output_path.unlink(missing_ok=True)

        _, index_entries = sessions[candidate.session_id]
        index_entries[candidate.index_position]["is_pruned"] = True
        modified_session_ids.add(candidate.session_id)

        remaining_count -= 1
        remaining_size_bytes -= candidate.output_size_bytes
        freed_bytes += candidate.output_size_bytes
        pruned_commands.append((candidate.session_id, candidate.sequence))

    for session_id in modified_session_ids:
        session_dir, index_entries = sessions[session_id]
        _write_index_json(session_dir / LogWriter.INDEX_FILENAME, index_entries)

    return RetentionResult(
        pruned_command_count=len(pruned_commands),
        freed_bytes=freed_bytes,
        pruned_commands=pruned_commands,
    )
