"""Integration tests for Recorder.run_interactive() (docs/plan.md stage 6b,
Windows half): real Win32 console raw-mode passthrough driving a real
PowerShell session, verified with a *second*, outer pywinpty-spawned
process standing in for "the real terminal" -- no human at a keyboard
needed, mirroring the technique test_recorder_interactive_unix.py uses with
a synthetic pty on the Unix side.

Windows only: uses `winpty`/Win32 console APIs that do not exist elsewhere.
`pytest.mark.skipif` alone is not enough here (evaluating the skip
condition must not itself trigger a failing import), so the platform check
happens before any Windows-only import.
"""

import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only backend")

if sys.platform == "win32":
    import glob
    import json
    import select
    import time

    import winpty

_INNER_SCRIPT_TEMPLATE = """\
import sys
sys.path.insert(0, {project_root!r})
from aicap.recorder import Recorder
Recorder({log_dir!r}, shell="powershell").run_interactive()
"""


def _spawn_outer(tmp_path, python_exe, project_root, dimensions=None):
    """Spawn a Python process (via pywinpty, standing in for "the real
    terminal") that itself runs Recorder.run_interactive() against a real
    PowerShell child -- i.e. two nested ConPTY sessions, exactly mirroring
    how a real `aicap start` invocation looks from the outside.

    `dimensions`, if given, is `(rows, cols)` for the *outer* ConPTY (the
    "real terminal" stand-in) -- used to test that the real terminal's size
    actually propagates down to the recorded child.
    """
    log_dir = tmp_path / "logs"
    inner_script = tmp_path / "inner.py"
    inner_script.write_text(
        _INNER_SCRIPT_TEMPLATE.format(project_root=project_root, log_dir=str(log_dir)),
        encoding="utf-8",
    )
    spawn_kwargs = {"dimensions": dimensions} if dimensions is not None else {}
    proc = winpty.PtyProcess.spawn([python_exe, str(inner_script)], **spawn_kwargs)
    return proc, log_dir


def _drain(proc, seconds):
    end = time.time() + seconds
    out = ""
    while time.time() < end:
        remaining = max(0.0, end - time.time())
        ready, _, _ = select.select([proc.fileno()], [], [], min(0.2, remaining))
        if not ready:
            continue
        try:
            out += proc.read(4096)
        except EOFError:
            break
    return out


def _read_index(log_dir):
    paths = glob.glob(str(log_dir / "sessions" / "*" / "index.json"))
    assert paths, "no session was recorded at all"
    with open(paths[0], "r", encoding="utf-8") as handle:
        return json.load(handle)


def test_typed_command_is_forwarded_executed_and_recorded(tmp_path):
    proc, log_dir = _spawn_outer(
        tmp_path, sys.executable, r"D:\IdeaProjects\cli-screen-recording-tool"
    )
    try:
        _drain(proc, 4.0)  # let the hook script + PowerShell profile finish loading

        proc.write("echo hello_aicap_windows\r\n")
        _drain(proc, 1.5)

        proc.write("exit\r\n")
        deadline = time.time() + 5.0
        while proc.isalive() and time.time() < deadline:
            time.sleep(0.2)
        assert not proc.isalive(), "aicap's own process should have exited after 'exit'"
    finally:
        proc.close()

    index = _read_index(log_dir)
    echo_entry = next(e for e in index if e["command"] == "echo hello_aicap_windows")
    assert echo_entry["exit_code"] == 0
    assert echo_entry["is_complete"] is True


def test_known_limitation_ctrl_c_does_not_interrupt_the_child_command(tmp_path):
    """Documents a verified Windows/ConPTY platform limitation (see
    docs/plan.md stage 6b): a raw Ctrl+C byte does not interrupt a running
    foreground command in a ConPTY-hosted child, because ConPTY creates the
    child with CREATE_NEW_PROCESS_GROUP, which breaks the normal console
    control-event delivery path -- confirmed against four different
    mechanisms (raw byte, pywinpty's sendintr(), GenerateConsoleCtrlEvent
    via os.kill(pid, signal.CTRL_C_EVENT), explicit ConPTY backend), all
    with identical results. This is not something aicap's code can fix.

    This test exists so that if a future Windows/ConPTY update ever changes
    this behavior, the assertion below starts failing -- a signal to revisit
    and update docs/plan.md's known-limitations note rather than the
    behavior silently drifting unnoticed.
    """
    proc, log_dir = _spawn_outer(
        tmp_path, sys.executable, r"D:\IdeaProjects\cli-screen-recording-tool"
    )
    try:
        _drain(proc, 4.0)

        start = time.time()
        proc.write("Start-Sleep -Seconds 6\r\n")
        time.sleep(1.0)
        proc.write("\x03")  # raw Ctrl+C byte

        # Known limitation: this does NOT interrupt Start-Sleep early. Give
        # it a window shorter than the sleep itself and confirm no output
        # resembling command completion shows up yet.
        early_output = _drain(proc, 3.0)
        elapsed = time.time() - start
        assert elapsed < 6.0, "test bug: waited too long before checking for early completion"
        # PSReadLine's own redraw noise while typing is expected here; what
        # we're checking is the *absence* of a fresh prompt/command
        # completion this early, which would indicate the sleep really was
        # interrupted (i.e. this known limitation stopped applying).
        prompt_reappeared_early = "PS " in early_output and "Start-Sleep" not in early_output
        assert not prompt_reappeared_early, (
            "Ctrl+C appears to have interrupted the child on this system -- "
            "the known ConPTY limitation documented in docs/plan.md stage 6b "
            "may no longer apply; if so, update that note (this is good news, "
            "not a real test failure)."
        )

        proc.write("exit\r\n")
        deadline = time.time() + 10.0
        while proc.isalive() and time.time() < deadline:
            time.sleep(0.2)
    finally:
        proc.close()

    index = _read_index(log_dir)
    sleep_entry = next(e for e in index if e["command"].strip().startswith("Start-Sleep"))
    # Ran to completion normally (exit_code 0), not interrupted.
    assert sleep_entry["exit_code"] == 0


def test_child_console_is_resized_to_match_the_real_terminal(tmp_path):
    """Regression test for a real bug found during stage 6b manual testing:
    the recorded child's console defaulted to a fixed size that never
    matched the real terminal window, so PSReadLine computed cursor
    positions for the wrong width -- garbled/overlapping text once mirrored
    back. Spawns the outer "real terminal" ConPTY at a deliberately unusual
    size, then asks the recorded PowerShell child to report what size it
    thinks it has, proving the propagation actually reaches the child
    rather than just not-crashing.
    """
    proc, log_dir = _spawn_outer(
        tmp_path,
        sys.executable,
        r"D:\IdeaProjects\cli-screen-recording-tool",
        dimensions=(51, 137),
    )
    try:
        # Two nested ConPTY layers (outer python.exe + inner powershell.exe
        # hook loading) start up slower than the single-layer tests above --
        # empirically needed more than the 4.0s/1.5s those use (see
        # scratchpad debug run, docs/plan.md stage 6b).
        _drain(proc, 6.0)

        proc.write("[Console]::WindowHeight.ToString() + ' ' + [Console]::WindowWidth.ToString()\r\n")
        output = _drain(proc, 3.0)
        assert "51 137" in output, (
            f"child did not report the real terminal's size (51 rows, 137 cols): {output!r}"
        )

        proc.write("exit\r\n")
        deadline = time.time() + 5.0
        while proc.isalive() and time.time() < deadline:
            time.sleep(0.2)
    finally:
        proc.close()
