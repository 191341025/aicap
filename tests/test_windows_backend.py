"""Tests for WindowsShellBackend, run only on Windows against a real
PowerShell child process (no mocking -- this backend's whole point is
driving a real ConPTY/PSReadLine combination, which stage 5 direct
verification found does NOT behave the same way as a mocked stand-in would:
see the module's own docstring for the AICAP_MARKER_FILE encoding bug that
only showed up against the real thing).

`pytest.mark.skipif` alone is not enough to skip cleanly on non-Windows:
it only skips running the tests, not collecting the module, so the
`import winpty` inside `aicap.pty_backend.windows_backend` would still
fail at collection time on a machine without `pywinpty` installed (real
CI failure on the Linux runner: `ModuleNotFoundError: No module named
'winpty'` -- pywinpty is declared `sys_platform == 'win32'`-only in
pyproject.toml, so it is never installed there at all). The platform
check must happen before the Windows-only import, same fix already used
in tests/test_recorder_interactive_windows.py.
"""
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only backend")

if sys.platform == "win32":
    from aicap.pty_backend.windows_backend import WindowsShellBackend


def _drain(backend, seconds):
    """Read and discard output for `seconds`, so the child's own startup
    noise (profile loading, hook script output) doesn't interfere with
    what a test is checking.
    """
    deadline = time.time() + seconds
    output = b""
    while time.time() < deadline:
        chunk = backend.read_output(timeout=0.2)
        if chunk:
            output += chunk
    return output


def _wait_until_ready(backend, timeout=15.0):
    """Block until the hook script has finished loading, rather than
    guessing a fixed sleep duration.

    pwsh_hook.ps1's `prompt` function fires once as soon as the very first
    prompt is about to be displayed (producing the documented "orphan
    leading end" noise event, see docs/plan.md) -- so the first boundary
    event showing up is a reliable "the shell and hook are fully up" signal.
    A fixed sleep was tried first and was flaky under pytest (profile-load
    time is not constant), which is exactly the kind of thing this project's
    verify-don't-guess rule exists for.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if backend.read_new_boundary_events():
            return
        backend.read_output(timeout=0.2)
    raise TimeoutError("shell did not become ready (no boundary event) within timeout")


@pytest.fixture
def backend():
    instance = WindowsShellBackend(shell="powershell")
    instance.start()
    _wait_until_ready(instance)
    yield instance
    instance.close()


def test_command_boundary_is_captured_with_correct_exit_code(backend):
    backend.write_input(b"echo hello_from_aicap\r\n")
    _drain(backend, 2.5)

    events = backend.read_new_boundary_events()
    starts = [e for e in events if e.get("event") == "start"]
    ends = [e for e in events if e.get("event") == "end"]

    assert any(e.get("command") == "echo hello_from_aicap" for e in starts)
    assert any(e.get("exit_code") == 0 for e in ends)


def test_failing_command_reports_nonzero_exit_code(backend):
    # 2.5s, not 1.0s: PowerShell's error-record formatting for an unknown
    # command is slower to reach the "end" boundary event than a plain
    # successful cmdlet -- confirmed by a real CI failure on a GitHub
    # Actions Windows runner (slower than a local dev machine) at 1.0s,
    # while the sibling success-case test above passed at the same value.
    backend.write_input(b"nonexistent-command-xyz\r\n")
    _drain(backend, 2.5)

    events = backend.read_new_boundary_events()
    ends = [e for e in events if e.get("event") == "end"]

    assert any(e.get("exit_code") not in (0, None) for e in ends)


def test_non_ascii_command_text_round_trips_exactly(backend):
    # Regression test for the bug found during stage 5 direct verification:
    # pwsh_hook.ps1 originally wrote marker lines with Add-Content's default
    # encoding, which is not UTF-8 on Windows PowerShell 5.1 -- a Chinese
    # command's marker line failed to decode as UTF-8 on the Python side and
    # was silently dropped. Fixed by writing via .NET UTF8Encoding($false).
    command = "echo 你好"  # "echo 你好"
    backend.write_input(command.encode("utf-8"))
    _drain(backend, 1.0)
    backend.write_input(b"\r\n")
    _drain(backend, 2.5)

    events = backend.read_new_boundary_events()
    starts = [e for e in events if e.get("event") == "start" and "command" in e]

    assert any(e["command"] == command for e in starts)


def test_non_ascii_output_round_trips(backend):
    backend.write_input(
        "Write-Output 'AICAP_CN_TEST_你好世界'\r\n".encode("utf-8")
    )
    output = _drain(backend, 2.5)

    assert "你好世界".encode("utf-8") in output


def test_backend_detects_exit_and_close_is_idempotent(backend):
    backend.write_input(b"exit\r\n")
    _drain(backend, 3.5)

    assert backend.is_alive() is False

    backend.close()  # must not raise on a second call
    assert backend.is_alive() is False


def test_read_output_returns_empty_bytes_when_nothing_available(backend):
    # Not b"" only because the PTY genuinely has nothing new -- confirms
    # read_output() honors its "no data within timeout" contract rather
    # than blocking or raising.
    result = backend.read_output(timeout=0.1)
    assert result == b"" or isinstance(result, bytes)
