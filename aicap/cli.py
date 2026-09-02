"""Command-line entry point for the aicap tool.

This module wires up the ``aicap`` console script (see ``[project.scripts]``
in pyproject.toml). ``start`` spawns a ``Recorder`` (see ``recorder.py``) and
runs ``run_interactive()`` -- forwarding the real terminal's keystrokes to
the recorded shell and mirroring its output back -- until the child shell
exits; ``status`` reads and prints the ``STATUS.md`` a ``LogWriter``
maintains at the root of a log directory.
"""

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from aicap.recorder import Recorder

_STATUS_FILENAME = "STATUS.md"


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser for the aicap command."""
    parser = argparse.ArgumentParser(
        prog="aicap",
        description=(
            "aicap takes over an interactive shell session and records "
            "every command's input and output to disk in a structured "
            "format, so an AI assistant can read the results directly "
            "instead of having them copy-pasted by hand."
        ),
    )
    subparsers = parser.add_subparsers(
        dest="command",
        metavar="{start,status}",
        help="Subcommand to run",
    )

    start_parser = subparsers.add_parser(
        "start",
        help="Start recording an interactive shell session",
    )
    start_parser.add_argument(
        "log_dir",
        help="Directory to write session logs to (required)",
    )
    start_parser.add_argument(
        "--shell",
        default=None,
        help=(
            "Which shell to record. Windows: 'powershell' (Windows "
            "PowerShell 5.1, the default) or 'pwsh' (PowerShell 7+). "
            "Unix: 'bash' or 'zsh' (default: auto-detected from $SHELL)."
        ),
    )

    status_parser = subparsers.add_parser(
        "status",
        help="Show the current recording status for a log directory",
    )
    status_parser.add_argument(
        "log_dir",
        help="Directory previously passed to 'aicap start' (required)",
    )

    return parser


def _run_start(log_dir: str, shell: Optional[str] = None) -> int:
    recorder = Recorder(log_dir, shell=shell)
    # flush=True: run_interactive() switches to writing the child's output
    # directly to the raw console handle (bypassing Python's own stdout
    # buffer entirely) almost immediately after this. Without an explicit
    # flush here, these two lines can still be sitting in the buffer when
    # that switch happens, racing with the child's own early output for who
    # actually reaches the screen first -- found as an intermittent "session
    # id printed but the shell prompt after it doesn't reliably show up"
    # symptom during stage 6b real-world manual testing (see docs/plan.md).
    print(f"aicap: recording started, writing to {log_dir}", flush=True)
    print(f"aicap: session id {recorder.log_writer.session_id}", flush=True)

    recorder.run_interactive()

    print(f"aicap: recording finished, session {recorder.log_writer.session_id} saved")
    return 0


def _run_status(log_dir: str) -> int:
    status_path = Path(log_dir) / _STATUS_FILENAME
    if not status_path.is_file():
        print(f"aicap: no recording found in {log_dir} (no {_STATUS_FILENAME} there yet)")
        return 1
    print(status_path.read_text(encoding="utf-8"))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point for the aicap console script.

    Returns the process exit code rather than calling sys.exit() directly,
    so this function stays easy to unit test.
    """
    parser = build_parser()
    arguments = parser.parse_args(argv)

    if arguments.command is None:
        # No subcommand given: show help instead of doing nothing or
        # raising an unhandled exception.
        parser.print_help()
        return 0

    if arguments.command == "start":
        return _run_start(arguments.log_dir, shell=arguments.shell)

    if arguments.command == "status":
        return _run_status(arguments.log_dir)

    raise AssertionError(f"unreachable: unknown command {arguments.command!r}")


if __name__ == "__main__":
    sys.exit(main())
