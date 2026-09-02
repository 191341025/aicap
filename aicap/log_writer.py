"""Write session/command logs to disk per docs/plan.md's storage layout.

See plan.md sections "总体架构" and "日志目录结构" for the directory
layout and the `index.json` entry schema this module implements -- those
field names and the directory shape are fixed design decisions, not
reinvented here.

``LogWriter`` is driven by explicit "command start" / "command end" events
(as produced by a future ``recorder.py`` reading marker events from a PTY
backend -- see plan.md's "命令边界检测" section). This module has no
knowledge of PTYs or shells; it only knows how to turn a sequence of
start/end events into the on-disk layout, which is what makes it possible
to unit test with fully simulated events (no real PTY needed).
"""

import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from aicap.ansi_stripper import strip_ansi_sequences

PathLike = Union[str, Path]


def _now_iso() -> str:
    """Current local time as an ISO-8601 string with second precision.

    Matches the format used in plan.md's index.json example
    ("2026-08-31T21:10:00"): no timezone offset, no microseconds.
    """
    return datetime.now().isoformat(timespec="seconds")


def _write_text_utf8(path: Path, text: str) -> None:
    """Write text to `path` as UTF-8 with no BOM (project character-set
    policy, see CLAUDE.md). Uses builtin `open()` rather than
    `Path.write_text(..., newline=...)` because the `newline` keyword on
    `Path.write_text` is Python 3.10+ only and this project targets 3.9+.
    """
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _append_text_utf8(path: Path, text: str) -> None:
    """Append text to `path` as UTF-8 with no BOM, creating it if needed."""
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _write_json_utf8(path: Path, data: Any) -> None:
    """Write `data` as pretty-printed JSON, UTF-8, no BOM.

    `ensure_ascii=False` is deliberate: index.json field values we generate
    ourselves are English per the character-set policy, but a `command`
    field can legitimately contain the user's real, possibly non-ASCII
    input (e.g. a Chinese argument) -- that content must round-trip
    unchanged, not get escaped into \\uXXXX sequences.
    """
    _write_text_utf8(path, json.dumps(data, indent=2, ensure_ascii=False))


_SLUG_INVALID_CHARS = re.compile(r"[^A-Za-z0-9]+")


def _slugify_command(command: str, max_length: int = 24) -> str:
    """Turn a command string into a short, filesystem-safe, ASCII slug.

    Used only for building `commands/NNNN-xxx.log` filenames (plan.md
    example: `0001-xxx.log`). Non-alphanumeric characters (including any
    non-ASCII characters from the user's real command text) are collapsed
    to '-' rather than kept verbatim, so the resulting filename is always
    plain ASCII regardless of what the recorded command contained -- this
    sidesteps any filesystem/encoding edge cases entirely rather than
    relying on the filesystem to handle arbitrary Unicode names correctly.
    """
    slug = _SLUG_INVALID_CHARS.sub("-", command.strip().lower()).strip("-")
    if not slug:
        slug = "cmd"
    return slug[:max_length]


class LogWriter:
    """Writes one recording session's logs to `{log_dir}/sessions/{id}/`.

    One `LogWriter` instance owns exactly one session directory, created
    fresh on construction. See module docstring and plan.md for the
    directory layout and index.json schema this implements.
    """

    # 5 MB per plan.md's "完整性/崩溃检测" section -- fixed value, not
    # configurable, so a runaway full-screen program (vim/top left running)
    # cannot silently blow up disk usage.
    MAX_COMMAND_OUTPUT_BYTES = 5 * 1024 * 1024

    # How many recent commands STATUS.md lists. Chosen as a small number
    # that comfortably fits in a single glance/short read for an AI or
    # human opening STATUS.md as an "entry page" (plan.md calls it exactly
    # that), while still giving enough recent history to spot a pattern
    # across a few commands. Not specified by plan.md; this is this
    # module's own design judgment.
    STATUS_RECENT_COMMAND_COUNT = 10

    SESSIONS_DIRNAME = "sessions"
    COMMANDS_DIRNAME = "commands"
    SESSION_LOG_FILENAME = "session.log"
    INDEX_FILENAME = "index.json"

    def __init__(self, log_dir: PathLike) -> None:
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        sessions_root = self.log_dir / self.SESSIONS_DIRNAME
        sessions_root.mkdir(parents=True, exist_ok=True)

        # Detect an interrupted previous session *before* creating this
        # session's directory, so the scan only ever sees genuinely older
        # sessions.
        self._interrupted_notice = self._detect_previous_interruption(sessions_root)

        self.session_id = self._generate_session_id(sessions_root)
        self.session_dir = sessions_root / self.session_id
        self.commands_dir = self.session_dir / self.COMMANDS_DIRNAME
        # exist_ok=False: session_id is freshly generated to be unique, so
        # this directory must not already exist -- plan.md's non-destructive
        # rule (never overwrite an existing session) means a collision here
        # is a real bug, not something to paper over with exist_ok=True.
        self.commands_dir.mkdir(parents=True, exist_ok=False)

        self.session_log_path = self.session_dir / self.SESSION_LOG_FILENAME
        self.index_path = self.session_dir / self.INDEX_FILENAME

        self._index: List[Dict[str, Any]] = []
        self._sequence = 0
        self._pending: Optional[Dict[str, Any]] = None

        _write_text_utf8(self.session_log_path, "")
        self._flush_index()
        self._write_status()

    # -- session id / directory setup ------------------------------------

    @staticmethod
    def _generate_session_id(sessions_root: Path) -> str:
        """Timestamp + short random suffix, retried on the (astronomically
        unlikely) chance of collision -- guarantees a repeated `aicap
        start` against the same log_dir always creates a brand-new session
        directory and never overwrites an existing one (plan.md's
        non-destructive retention rule).
        """
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        while True:
            suffix = uuid.uuid4().hex[:6]
            candidate = f"{timestamp}-{suffix}"
            if not (sessions_root / candidate).exists():
                return candidate

    @staticmethod
    def _detect_previous_interruption(sessions_root: Path) -> Optional[Dict[str, str]]:
        """Look at the most recently created sibling session (if any) and
        report its first still-`is_complete: false` command, if it has one.

        Directories are compared by filesystem mtime (not by name) to
        reliably pick "the most recent one" even if two sessions were
        created within the same wall-clock second (same timestamp prefix,
        different random suffix) -- name comparison would not reliably
        order those.
        """
        candidates = [entry for entry in sessions_root.iterdir() if entry.is_dir()]
        if not candidates:
            return None
        candidates.sort(key=lambda entry: entry.stat().st_mtime)
        previous_session_dir = candidates[-1]

        previous_index_path = previous_session_dir / LogWriter.INDEX_FILENAME
        if not previous_index_path.exists():
            return None
        try:
            previous_index = json.loads(previous_index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

        for entry in previous_index:
            if not entry.get("is_complete", True):
                return {
                    "session_id": previous_session_dir.name,
                    "command": entry.get("command", ""),
                }
        return None

    # -- event handlers ----------------------------------------------------

    def handle_command_start(self, command: str, started_at: Optional[str] = None) -> None:
        """Record a "command started" boundary event.

        Appends a new `index.json` entry with `is_complete: false` and
        flushes immediately (plan.md's crash-detection requirement: the
        on-disk state must reflect "started but not finished" even if the
        process dies right after this call, without waiting for the
        matching end event).
        """
        if self._pending is not None:
            # A new start arrived while the previous command never got a
            # matching end. In a correctly behaving single interactive
            # shell this only happens for the boundary-event noise
            # documented in plan.md's stage 0 findings (e.g. bash's
            # PROMPT_COMMAND-assignment line getting misread as a fake
            # "start" that is never followed by a real "end" before the
            # real first command's start arrives). Discard the stale
            # pending command entirely -- it never produced any output, so
            # it should not appear in the recorded history at all.
            self._discard_pending()

        started_at = started_at or _now_iso()
        self._sequence += 1
        entry: Dict[str, Any] = {
            "sequence": self._sequence,
            "command": command,
            "started_at": started_at,
            "ended_at": None,
            "exit_code": None,
            "output_file": None,
            "is_complete": False,
            "is_pruned": False,
            "is_truncated": False,
        }
        self._index.append(entry)
        self._flush_index()
        self._append_session_log_marker("start", entry)

        self._pending = {"sequence": self._sequence, "entry": entry}

    def handle_command_end(
        self,
        exit_code: int,
        output: str = "",
        ended_at: Optional[str] = None,
    ) -> None:
        """Record a "command ended" boundary event.

        `output` is the raw (not yet ANSI-stripped) text captured for this
        command. It is truncated at `MAX_COMMAND_OUTPUT_BYTES` if needed,
        ANSI-stripped, and written to `commands/NNNN-xxx.log`; the matching
        `index.json` entry is updated to `is_complete: true` and flushed;
        `latest.log`/`latest.json`/`STATUS.md` are regenerated.

        An `end` event with no matching in-flight `start` (e.g. the
        PowerShell/zsh hook-install noise documented in plan.md's stage 0
        findings, where the very first event after the hook installs is an
        orphan `end`) is discarded silently: nothing to pair it with means
        nothing can be recorded.
        """
        if self._pending is None:
            return

        pending = self._pending
        self._pending = None
        entry = pending["entry"]

        clean_output, was_truncated = self._render_command_output(
            entry["sequence"], entry["command"], output
        )

        entry["ended_at"] = ended_at or _now_iso()
        entry["exit_code"] = exit_code
        entry["output_file"] = f"{self.COMMANDS_DIRNAME}/{self._output_filename(entry['sequence'], entry['command'])}"
        entry["is_complete"] = True
        entry["is_truncated"] = was_truncated
        self._flush_index()
        self._append_session_log_marker("end", entry)

        self._write_latest(entry, clean_output)
        self._write_status()

    def finalize_session(self, trailing_output: str = "") -> None:
        """Handle a clean subprocess exit and session wrap-up.

        This implements plan.md's "会话退出/收尾流程": when the recorder
        detects the child shell exited normally, it calls this once to
        flush any not-yet-finalized state so `index.json`/`STATUS.md` end
        up in a final, consistent shape.

        If a command is still open at this point, it is completed here
        rather than left as a dangling `is_complete: false` record -- this
        is expected, not a crash: the shell's very last input is commonly
        `exit` itself, whose `start` event fires but can never get a
        matching `end` because the shell process is gone before the next
        prompt (and thus the next boundary event) could ever be produced.
        Per plan.md, a clean exit should "补全" (fill in/complete) that
        last entry rather than have it look like a crash. `trailing_output`
        lets the caller pass whatever output was captured for that final,
        never-closed command (may be empty, e.g. a bare `exit` with no
        further output).

        A command left pending here always gets `exit_code: None`: the
        shell exited before ever reporting a real exit code for it, and
        fabricating `0` would be dishonest about what was actually
        observed.
        """
        if self._pending is not None:
            pending = self._pending
            self._pending = None
            entry = pending["entry"]

            clean_output, was_truncated = self._render_command_output(
                entry["sequence"], entry["command"], trailing_output
            )
            entry["ended_at"] = _now_iso()
            entry["exit_code"] = None
            entry["output_file"] = (
                f"{self.COMMANDS_DIRNAME}/{self._output_filename(entry['sequence'], entry['command'])}"
            )
            entry["is_complete"] = True
            entry["is_truncated"] = was_truncated
            self._append_session_log_marker("end", entry)
            self._write_latest(entry, clean_output)

        self._flush_index()
        self._write_status()

    # -- internals -----------------------------------------------------

    def _discard_pending(self) -> None:
        """Drop the in-flight command without writing any output file and
        remove its provisional `index.json` record -- see
        `handle_command_start` for when this applies.
        """
        pending = self._pending
        assert pending is not None
        self._pending = None
        # The discarded entry is always the last one appended (it was the
        # most recently issued sequence number), so this is a plain pop,
        # not a search.
        if self._index and self._index[-1]["sequence"] == pending["sequence"]:
            self._index.pop()
        self._sequence -= 1
        self._flush_index()

    def _render_command_output(
        self, sequence: int, command: str, raw_output: str
    ) -> Tuple[str, bool]:
        """Truncate `raw_output` to the size cap if needed, ANSI-strip it,
        and write the result to this command's `commands/NNNN-xxx.log`.

        Truncation is applied to the raw captured output (before ANSI
        stripping): the size cap exists to bound how much raw data a
        runaway full-screen program can force onto disk, which is a
        property of what was actually captured, not of the cleaned-up
        text derived from it.

        Returns the ANSI-stripped text that was written and whether
        truncation occurred.
        """
        raw_bytes = raw_output.encode("utf-8")
        was_truncated = len(raw_bytes) > self.MAX_COMMAND_OUTPUT_BYTES
        if was_truncated:
            raw_output = raw_bytes[: self.MAX_COMMAND_OUTPUT_BYTES].decode(
                "utf-8", errors="ignore"
            )

        clean_output = strip_ansi_sequences(raw_output)
        output_path = self.commands_dir / self._output_filename(sequence, command)
        _write_text_utf8(output_path, clean_output)
        return clean_output, was_truncated

    def _output_filename(self, sequence: int, command: str) -> str:
        return f"{sequence:04d}-{_slugify_command(command)}.log"

    def append_raw_output(self, data: bytes) -> None:
        """Mirror raw PTY output bytes into `session.log`, interleaved with
        the boundary markers `handle_command_start`/`handle_command_end`
        already write there.

        This is `recorder.py`'s way of fulfilling plan.md's "完整原始字节流
        （含 ANSI）" description of `session.log` -- this class only ever
        wrote the boundary markers itself (see `_append_session_log_marker`)
        since it never received a continuous raw stream on its own.
        Decoding errors are replaced rather than raised: this is a
        best-effort archival mirror for later human digging, not addressable
        structured data, so a stray invalid byte should not interrupt
        recording.
        """
        if not data:
            return
        _append_text_utf8(self.session_log_path, data.decode("utf-8", errors="replace"))

    def _flush_index(self) -> None:
        _write_json_utf8(self.index_path, self._index)

    def _append_session_log_marker(self, event: str, entry: Dict[str, Any]) -> None:
        """Append a boundary marker line to `session.log`.

        `session.log` is the complete raw stream for the whole session
        (plan.md: "完整原始字节流（含 ANSI），带分隔标记，用于深挖历史").
        This module does not receive a continuous raw PTY stream (that is
        a future recorder.py concern -- see module docstring), so what it
        can guarantee here is a marker at every command boundary; a future
        recorder.py is expected to also mirror raw PTY bytes into this
        same file as they arrive.
        """
        marker = f"=== [{event}] sequence={entry['sequence']} command={entry['command']!r} ===\n"
        _append_text_utf8(self.session_log_path, marker)

    def _write_latest(self, entry: Dict[str, Any], clean_output: str) -> None:
        _write_text_utf8(self.log_dir / "latest.log", clean_output)
        latest_meta = dict(entry)
        latest_meta["session_id"] = self.session_id
        _write_json_utf8(self.log_dir / "latest.json", latest_meta)

    def _write_status(self) -> None:
        lines: List[str] = ["# aicap status", ""]

        if self._interrupted_notice is not None:
            lines.append(
                "previous session was interrupted while running "
                f'"{self._interrupted_notice["command"]}" '
                f'(session {self._interrupted_notice["session_id"]}); '
                "its output may be incomplete."
            )
            lines.append("")

        lines.append(f"session_id: {self.session_id}")
        lines.append(f"total_commands: {len(self._index)}")
        lines.append("")
        lines.append(f"## recent commands (last {self.STATUS_RECENT_COMMAND_COUNT}, newest first)")
        lines.append("")

        recent = self._index[-self.STATUS_RECENT_COMMAND_COUNT :]
        if not recent:
            lines.append("(no commands recorded yet)")
        else:
            for entry in reversed(recent):
                status = "complete" if entry["is_complete"] else "incomplete"
                lines.append(
                    f"- [{entry['sequence']}] `{entry['command']}` "
                    f"exit_code={entry['exit_code']} status={status}"
                )
        lines.append("")

        _write_text_utf8(self.log_dir / "STATUS.md", "\n".join(lines))
