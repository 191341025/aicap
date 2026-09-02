"""Integration test for Recorder.run_interactive() (docs/plan.md stage 6b,
Unix half): real raw-mode terminal passthrough driving a real bash session,
verified with a synthetic pty standing in for "the real terminal" -- no
human at a keyboard needed.

Unix/WSL only: uses `pty`/`termios`, which do not exist on Windows.
`pytest.importorskip` makes this file skip cleanly during collection on
Windows (rather than crashing `pytest tests/`'s Windows-side run) instead of
using a `skipif` marker, which would still need to import `termios` at
module load time to even evaluate the condition.

This does not need a human to actually type anything: a second pty pair
(`pty.openpty()`) stands in for "the real terminal" aicap itself would be
running in -- the test writes bytes to its master end (simulating
keystrokes) and reads from it (simulating what a human would see on
screen), while `run_interactive()` runs against the slave end in a
background thread.
"""

termios = __import__("pytest").importorskip("termios")

import fcntl
import glob
import json
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import threading
import time

from aicap.pty_backend.unix_backend import UnixShellBackend
from aicap.recorder import Recorder

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read_available(fd, duration):
    deadline = time.time() + duration
    buf = b""
    while time.time() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.1)
        if ready:
            try:
                buf += os.read(fd, 65536)
            except OSError:
                break
    return buf


def _read_index(log_dir):
    session_index_paths = glob.glob(os.path.join(str(log_dir), "sessions", "*", "index.json"))
    assert session_index_paths, "no session was recorded at all"
    with open(session_index_paths[0], "r", encoding="utf-8") as handle:
        return json.load(handle)


def test_typed_command_is_forwarded_executed_and_recorded(tmp_path):
    master_fd, slave_fd = pty.openpty()
    recorder = Recorder(str(tmp_path), backend=UnixShellBackend(shell="bash"))
    thread = threading.Thread(
        target=recorder.run_interactive,
        kwargs={"stdin_fd": slave_fd, "stdout_fd": slave_fd},
        daemon=True,
    )
    thread.start()
    try:
        _read_available(master_fd, 2.5)  # let the shell + hook finish loading

        os.write(master_fd, b"echo hello_interactive\n")
        echoed = _read_available(master_fd, 1.0)
        assert b"hello_interactive" in echoed

        os.write(master_fd, b"exit\n")
        thread.join(timeout=5.0)
        assert not thread.is_alive()
    finally:
        os.close(master_fd)

    index = _read_index(tmp_path)
    echo_entry = next(e for e in index if e["command"] == "echo hello_interactive")
    assert echo_entry["exit_code"] == 0


def test_ctrl_c_interrupts_the_child_command_not_aicap_itself(tmp_path):
    # This is the design requirement the user set for stage 6b: aicap must
    # not intercept Ctrl+C -- putting the real terminal in raw mode and
    # forwarding the raw 0x03 byte verbatim should let the CHILD shell's own
    # terminal driver interrupt whatever is running in its foreground,
    # while aicap's own process (and this test thread driving it) keeps
    # running normally afterward.
    master_fd, slave_fd = pty.openpty()
    recorder = Recorder(str(tmp_path), backend=UnixShellBackend(shell="bash"))
    thread = threading.Thread(
        target=recorder.run_interactive,
        kwargs={"stdin_fd": slave_fd, "stdout_fd": slave_fd},
        daemon=True,
    )
    thread.start()
    try:
        _read_available(master_fd, 2.5)

        os.write(master_fd, b"sleep 5\n")
        time.sleep(0.5)
        os.write(master_fd, b"\x03")  # raw Ctrl+C byte
        output = _read_available(master_fd, 2.0)
        assert b"^C" in output  # bash's own echo of the interrupt

        os.write(master_fd, b"exit\n")
        thread.join(timeout=5.0)
        assert not thread.is_alive(), "aicap's own process should not have been killed by Ctrl+C"
    finally:
        os.close(master_fd)

    index = _read_index(tmp_path)
    sleep_entry = next(e for e in index if e["command"].strip() == "sleep 5")
    # 130 = 128 + SIGINT(2), the standard shell convention for "killed by signal N".
    assert sleep_entry["exit_code"] == 130


def test_real_terminal_is_put_into_raw_mode_and_restored_on_exit(tmp_path):
    master_fd, slave_fd = pty.openpty()
    original_termios = termios.tcgetattr(slave_fd)

    recorder = Recorder(str(tmp_path), backend=UnixShellBackend(shell="bash"))
    thread = threading.Thread(
        target=recorder.run_interactive,
        kwargs={"stdin_fd": slave_fd, "stdout_fd": slave_fd},
        daemon=True,
    )
    thread.start()
    try:
        _read_available(master_fd, 2.5)
        raw_mode_termios = termios.tcgetattr(slave_fd)
        assert raw_mode_termios != original_termios

        os.write(master_fd, b"exit\n")
        thread.join(timeout=5.0)

        # Must read this back while master_fd (the pty's other end) is
        # still open: once it is closed, the slave end reports EIO on
        # further termios calls, which is a property of pty teardown, not
        # of whether UnixTerminalIO actually restored the settings.
        restored_termios = termios.tcgetattr(slave_fd)
    finally:
        os.close(master_fd)

    assert restored_termios == original_termios


def test_previous_session_interruption_is_detected_after_a_real_crash(tmp_path):
    """docs/plan.md "完整性/崩溃检测": if aicap's own process is killed
    outright (not a graceful `exit`), the next `aicap start` against the
    same log_dir must notice the previous session's dangling command and
    flag it in STATUS.md.

    Driven as a genuinely separate OS process (subprocess.Popen), not a
    thread within this test process like the other tests here -- a real
    crash means nothing runs, not even Python-level cleanup code, and only
    SIGKILL against a truly separate process reproduces that faithfully.
    """
    log_dir = tmp_path / "logs"
    inner_script = tmp_path / "inner_crash.py"
    inner_script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {_PROJECT_ROOT!r})\n"
        "from aicap.recorder import Recorder\n"
        f"Recorder({str(log_dir)!r}, shell='bash').run_interactive()\n",
        encoding="utf-8",
    )

    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(
        [sys.executable, str(inner_script)],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        start_new_session=True,
    )
    os.close(slave_fd)
    try:
        _read_available(master_fd, 2.5)  # let the shell + hook finish loading

        os.write(master_fd, b"sleep 30\n")
        time.sleep(0.5)  # let the "start" boundary event actually land on disk

        # Simulate a real crash: no graceful shutdown path runs at all.
        proc.kill()  # SIGKILL
        proc.wait(timeout=5.0)
    finally:
        os.close(master_fd)

    first_run_index = _read_index(log_dir)
    dangling_entry = next(e for e in first_run_index if e["command"].strip() == "sleep 30")
    assert dangling_entry["is_complete"] is False

    # A second, real "aicap start" against the same log_dir should notice.
    master_fd2, slave_fd2 = pty.openpty()
    proc2 = subprocess.Popen(
        [sys.executable, str(inner_script)],
        stdin=slave_fd2,
        stdout=slave_fd2,
        stderr=slave_fd2,
        start_new_session=True,
    )
    os.close(slave_fd2)
    try:
        _read_available(master_fd2, 2.5)
        os.write(master_fd2, b"exit\n")
        proc2.wait(timeout=5.0)
    finally:
        os.close(master_fd2)

    status_md = (log_dir / "STATUS.md").read_text(encoding="utf-8")
    assert "interrupted" in status_md
    assert "sleep 30" in status_md


def test_piped_command_text_is_recorded_in_full_not_truncated_to_last_stage(tmp_path):
    """Regression test for a real bug found during real-world testing
    (docs/plan.md): the DEBUG trap fires once per *simple command*, not
    once per top-level command line -- for a pipeline like "a | b", it
    fires separately for "a" and for "b", each time with $BASH_COMMAND set
    to only that one stage. bash_hook.sh previously recorded whatever
    $BASH_COMMAND was on the *last* firing, silently truncating a real
    session's "env | head -20" down to just "head -20". Confirmed fixed by
    switching to `fc -ln -1` (bash's own history, always the full verbatim
    line) captured only on the first DEBUG firing per command.
    """
    master_fd, slave_fd = pty.openpty()
    recorder = Recorder(str(tmp_path), backend=UnixShellBackend(shell="bash"))
    thread = threading.Thread(
        target=recorder.run_interactive,
        kwargs={"stdin_fd": slave_fd, "stdout_fd": slave_fd},
        daemon=True,
    )
    thread.start()
    try:
        _read_available(master_fd, 2.5)

        os.write(master_fd, b"echo aicap_pipe_test | cat | wc -l\n")
        output = _read_available(master_fd, 1.5)
        assert b"1" in output

        os.write(master_fd, b"exit\n")
        thread.join(timeout=5.0)
    finally:
        os.close(master_fd)

    index = _read_index(tmp_path)
    pipe_entry = next(e for e in index if "aicap_pipe_test" in e["command"])
    assert pipe_entry["command"].strip() == "echo aicap_pipe_test | cat | wc -l"


def test_child_pty_is_resized_to_match_the_real_terminal(tmp_path):
    """Regression test for a real bug found during stage 6b manual testing:
    the child's PTY defaulted to a fixed size that never matched the real
    terminal window, so the child's own line-editor computed cursor
    positions for the wrong width -- garbled/overlapping text once mirrored
    back. Sets the synthetic "real terminal" pty to a deliberately unusual
    size *before* starting, then asks the child shell to report what size
    it thinks it has (`stty size`), proving the propagation actually
    reaches the child rather than just not-crashing.
    """
    master_fd, slave_fd = pty.openpty()
    # struct winsize: rows, cols, x-pixels, y-pixels (pixels unused here).
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 51, 137, 0, 0))

    recorder = Recorder(str(tmp_path), backend=UnixShellBackend(shell="bash"))
    thread = threading.Thread(
        target=recorder.run_interactive,
        kwargs={"stdin_fd": slave_fd, "stdout_fd": slave_fd},
        daemon=True,
    )
    thread.start()
    try:
        _read_available(master_fd, 2.5)

        os.write(master_fd, b"stty size\n")
        output = _read_available(master_fd, 1.5)
        assert b"51 137" in output, (
            f"child did not report the real terminal's size (51 rows, 137 cols): {output!r}"
        )

        os.write(master_fd, b"exit\n")
        thread.join(timeout=5.0)
    finally:
        os.close(master_fd)
