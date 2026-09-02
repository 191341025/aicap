"""Real-terminal raw I/O for stage 6b interactive passthrough (docs/plan.md),
Windows half.

Windows has no `termios`/`tty` and `select.select()` does not work on
console handles, so this is a separate implementation from
`aicap/terminal_io.py` (Unix), using the Win32 console API directly via
`ctypes` (stdlib only, no new dependency). Callers must import this module
lazily (inside a function, not at module load time) -- `ctypes.windll` does
not exist on non-Windows platforms.

Verified empirically (docs/plan.md stage 6b) by spawning a probe script as a
real ConPTY child via pywinpty and feeding it a raw Ctrl+C byte, the same
"drive it like a human would, but automated" technique used for the Unix
half's tests:
  - Setting the input console mode to *only* `ENABLE_VIRTUAL_TERMINAL_INPUT`
    (clearing `ENABLE_PROCESSED_INPUT`, which is what makes the console
    turn Ctrl+C into a console control event instead of passing it through
    as data) makes Ctrl+C's raw 0x03 byte arrive via `ReadFile` like any
    other byte, with no `KeyboardInterrupt` raised, exactly mirroring what
    Unix raw mode (clearing `ISIG`) does.
  - `WaitForSingleObject(stdin_handle, timeout_ms)` correctly waits with a
    timeout on a console input handle and returns promptly when input
    arrives, giving the same "wait up to timeout, then read" shape as
    Unix's `select.select()`-based `read_stdin()` -- `select.select()`
    itself does not work here, `WaitForSingleObject` is the Windows-native
    equivalent for this purpose.

Also see `read_stdin()`/`_discard_non_key_events()`: `WaitForSingleObject`
being signaled is *not* sufficient proof that a following `ReadFile` call
will return promptly -- confirmed via a real stack-trace capture of a
reproducible hang (docs/plan.md) plus Microsoft's own "Reading Input
Buffer Events" doc, not guessed.
"""

import ctypes
import ctypes.wintypes
from typing import Optional, Tuple


class _COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class _SMALL_RECT(ctypes.Structure):
    _fields_ = [
        ("Left", ctypes.c_short),
        ("Top", ctypes.c_short),
        ("Right", ctypes.c_short),
        ("Bottom", ctypes.c_short),
    ]


class _CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
    _fields_ = [
        ("dwSize", _COORD),
        ("dwCursorPosition", _COORD),
        ("wAttributes", ctypes.c_ushort),
        ("srWindow", _SMALL_RECT),
        ("dwMaximumWindowSize", _COORD),
    ]


# INPUT_RECORD and its event-type structs (see `_discard_non_key_events()`):
# needed to inspect *which kind* of console input event is queued, which
# `ReadFile`/`ReadConsole` -- deliberately kept for their built-in
# ENABLE_VIRTUAL_TERMINAL_INPUT translation of real keystrokes into VT
# byte sequences -- cannot tell us themselves.
class _KEY_EVENT_UCHAR(ctypes.Union):
    _fields_ = [("UnicodeChar", ctypes.c_wchar), ("AsciiChar", ctypes.c_char)]


class _KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [
        ("bKeyDown", ctypes.wintypes.BOOL),
        ("wRepeatCount", ctypes.c_ushort),
        ("wVirtualKeyCode", ctypes.c_ushort),
        ("wVirtualScanCode", ctypes.c_ushort),
        ("uChar", _KEY_EVENT_UCHAR),
        ("dwControlKeyState", ctypes.wintypes.DWORD),
    ]


class _MOUSE_EVENT_RECORD(ctypes.Structure):
    _fields_ = [
        ("dwMousePosition", _COORD),
        ("dwButtonState", ctypes.wintypes.DWORD),
        ("dwControlKeyState", ctypes.wintypes.DWORD),
        ("dwEventFlags", ctypes.wintypes.DWORD),
    ]


class _WINDOW_BUFFER_SIZE_RECORD(ctypes.Structure):
    _fields_ = [("dwSize", _COORD)]


class _MENU_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("dwCommandId", ctypes.wintypes.UINT)]


class _FOCUS_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("bSetFocus", ctypes.wintypes.BOOL)]


class _INPUT_RECORD_EVENT(ctypes.Union):
    _fields_ = [
        ("KeyEvent", _KEY_EVENT_RECORD),
        ("MouseEvent", _MOUSE_EVENT_RECORD),
        ("WindowBufferSizeEvent", _WINDOW_BUFFER_SIZE_RECORD),
        ("MenuEvent", _MENU_EVENT_RECORD),
        ("FocusEvent", _FOCUS_EVENT_RECORD),
    ]


class _INPUT_RECORD(ctypes.Structure):
    _fields_ = [
        ("EventType", ctypes.c_ushort),
        ("Event", _INPUT_RECORD_EVENT),
    ]


_KEY_EVENT = 0x0001

_STD_INPUT_HANDLE = -10
_STD_OUTPUT_HANDLE = -11

# Leaves ENABLE_PROCESSED_INPUT, ENABLE_LINE_INPUT, ENABLE_ECHO_INPUT all
# off: no line buffering, no local echo (the child's own ConPTY output
# mirror provides echo), and -- critically -- Ctrl+C is not intercepted as
# a console control event, matching the project's "aicap does not interpret
# any keystroke" design requirement (docs/plan.md stage 6b).
#
# ENABLE_EXTENDED_FLAGS (0x0080) must be included even though its own bit
# means nothing to us: per the Win32 docs, SetConsoleMode only touches
# Quick Edit Mode when this flag is present in the call, and Quick Edit
# Mode defaults to on for many console hosts. Quick Edit Mode makes a
# single click/drag in the window enter text-selection state, which blocks
# any read on the input handle (our ReadFile/WaitForSingleObject in
# read_stdin()) until the user clicks again or presses a key -- suspected
# during a real intermittent-hang investigation (docs/plan.md), though the
# actual root cause there turned out to be a separate issue (see
# `_discard_non_key_events()`). Kept anyway: this is a real, independently
# documented Win32 hang cause in its own right.
# Omitting ENABLE_QUICK_EDIT_MODE (0x0040) from this value is what turns
# it off.
_RAW_INPUT_MODE = 0x0200 | 0x0080  # ENABLE_VIRTUAL_TERMINAL_INPUT | ENABLE_EXTENDED_FLAGS

# Output-side flag (same numeric value as ENABLE_ECHO_INPUT on the input
# side, but a completely different meaning here -- Win32 reuses bit
# positions per-handle-type): makes the console interpret ANSI/VT escape
# sequences in what we write instead of showing them as literal text, which
# the recorded child's output is full of (see docs/plan.md stage 0a's ANSI
# noise findings).
_ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004

_WAIT_OBJECT_0 = 0x0

# UTF-8 code page id, for SetConsoleOutputCP() (see __enter__()).
_CP_UTF8 = 65001

_kernel32 = ctypes.windll.kernel32


class WindowsTerminalIO:
    """Context manager: puts the console's real input mode into "raw" for
    its duration and restores the original mode on exit (even if an
    exception propagates out of the `with` block), mirroring
    `aicap.terminal_io.UnixTerminalIO`'s same guarantee. Also gives
    non-blocking-with-timeout read/write access to the real terminal.
    """

    def __init__(
        self,
        stdin_handle: Optional[int] = None,
        stdout_handle: Optional[int] = None,
    ) -> None:
        self._stdin_handle = (
            stdin_handle if stdin_handle is not None else _kernel32.GetStdHandle(_STD_INPUT_HANDLE)
        )
        self._stdout_handle = (
            stdout_handle if stdout_handle is not None else _kernel32.GetStdHandle(_STD_OUTPUT_HANDLE)
        )
        self._saved_input_mode: Optional[int] = None
        self._saved_output_mode: Optional[int] = None
        self._saved_output_cp: Optional[int] = None

    def __enter__(self) -> "WindowsTerminalIO":
        saved_input = ctypes.wintypes.DWORD()
        _kernel32.GetConsoleMode(self._stdin_handle, ctypes.byref(saved_input))
        self._saved_input_mode = saved_input.value
        _kernel32.SetConsoleMode(self._stdin_handle, _RAW_INPUT_MODE)

        saved_output = ctypes.wintypes.DWORD()
        _kernel32.GetConsoleMode(self._stdout_handle, ctypes.byref(saved_output))
        self._saved_output_mode = saved_output.value
        _kernel32.SetConsoleMode(
            self._stdout_handle, saved_output.value | _ENABLE_VIRTUAL_TERMINAL_PROCESSING
        )

        # This process's *own* real console's output code page -- separate
        # from, and just as necessary as, the child's `[Console]::OutputEncoding`
        # (set in pwsh_hook.ps1). That setting only makes the *child* emit
        # correct UTF-8 bytes for its own text; write_stdout() below hands
        # those raw bytes to this process's own real console via `WriteFile`,
        # which still interprets them using *this console's own* output code
        # page. On a non-English-locale Windows install (e.g. Chinese,
        # GBK/cp936 by default) that is not UTF-8 unless set here, so
        # otherwise-correct UTF-8 bytes still render as mojibake -- confirmed
        # via a real repro (docs/plan.md) where the child-side fix alone
        # did not resolve it. `SetConsoleOutputCP` is process/console-wide,
        # not per-handle (no handle argument in its signature).
        self._saved_output_cp = _kernel32.GetConsoleOutputCP()
        _kernel32.SetConsoleOutputCP(_CP_UTF8)

        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self._saved_input_mode is not None:
            _kernel32.SetConsoleMode(self._stdin_handle, self._saved_input_mode)
            self._saved_input_mode = None
        if self._saved_output_mode is not None:
            _kernel32.SetConsoleMode(self._stdout_handle, self._saved_output_mode)
            self._saved_output_mode = None
        if self._saved_output_cp is not None:
            _kernel32.SetConsoleOutputCP(self._saved_output_cp)
            self._saved_output_cp = None

    def _discard_non_key_events(self) -> None:
        """Remove any queued console input events that are not a real
        keydown keystroke, without touching a real keystroke if one is
        next in line.

        Root cause of a real intermittent-hang bug (docs/plan.md),
        confirmed via a real stack-trace capture rather than guessed:
        `WaitForSingleObject` on a console input handle is
        signaled by *any* queued event -- keyboard, mouse, window-resize,
        menu, or focus -- but `ReadFile`/`ReadConsole` (used below for
        their built-in VT-sequence translation of real keystrokes) only
        ever return for actual character data, and silently discard any
        other event type internally *while continuing to wait, with no
        timeout of their own*, for a real one to show up. A brand new
        console window gets an OS-injected event (e.g. a focus event) in
        its input buffer before the user ever touches the keyboard --
        `WaitForSingleObject` returns signaled on that alone, but the
        `ReadFile` call that followed then waited forever for a keystroke
        that might not come for a while, which looked exactly like a
        random full hang. Confirmed via Microsoft's own "Reading Input
        Buffer Events" doc: "informational events...are discarded" by
        ReadConsole/ReadFile, and only `ReadConsoleInput` exposes them.
        Draining those event types ourselves first (using
        `ReadConsoleInputW`, which -- unlike `ReadFile`/`ReadConsole` --
        does expose every event's real type) means the `ReadFile` call
        below is only ever reached when we already know either nothing is
        pending (so we skip it and return b"" for this poll instead) or a
        real keystroke is next (so it cannot block past this call).
        """
        record = _INPUT_RECORD()
        peeked = ctypes.wintypes.DWORD()
        removed = ctypes.wintypes.DWORD()
        while True:
            pending = ctypes.wintypes.DWORD()
            if not _kernel32.GetNumberOfConsoleInputEvents(self._stdin_handle, ctypes.byref(pending)):
                return
            if pending.value == 0:
                return
            if not _kernel32.PeekConsoleInputW(
                self._stdin_handle, ctypes.byref(record), 1, ctypes.byref(peeked)
            ) or peeked.value == 0:
                return
            if record.EventType == _KEY_EVENT and record.Event.KeyEvent.bKeyDown:
                return
            _kernel32.ReadConsoleInputW(self._stdin_handle, ctypes.byref(record), 1, ctypes.byref(removed))

    def read_stdin(self, timeout: float) -> bytes:
        """Return whatever is available on stdin within `timeout` seconds,
        or b"" if nothing arrived within that window -- mirrors
        `UnixTerminalIO.read_stdin()`'s same "no data within timeout is not
        EOF" contract.
        """
        timeout_ms = max(0, int(timeout * 1000))
        wait_result = _kernel32.WaitForSingleObject(self._stdin_handle, timeout_ms)
        if wait_result != _WAIT_OBJECT_0:
            return b""

        self._discard_non_key_events()

        pending = ctypes.wintypes.DWORD()
        if not _kernel32.GetNumberOfConsoleInputEvents(self._stdin_handle, ctypes.byref(pending)) or pending.value == 0:
            return b""

        buf = ctypes.create_string_buffer(65536)
        bytes_read = ctypes.wintypes.DWORD()
        ok = _kernel32.ReadFile(self._stdin_handle, buf, 65536, ctypes.byref(bytes_read), None)
        if not ok or not bytes_read.value:
            return b""
        return buf.raw[: bytes_read.value]

    def write_stdout(self, data: bytes) -> None:
        if not data:
            return
        bytes_written = ctypes.wintypes.DWORD()
        _kernel32.WriteFile(self._stdout_handle, data, len(data), ctypes.byref(bytes_written), None)

    def get_size(self) -> Tuple[int, int]:
        """Return (rows, cols) of the real console's *visible* window (not
        its full scrollback buffer, which `GetConsoleScreenBufferInfo`'s
        `dwSize` reports and is usually much taller) -- verified against a
        real ConPTY console spawned at a known size (docs/plan.md); struct
        layout and the `srWindow` computation matched exactly.

        See `PtyBackendBase.resize()`: without propagating this to the
        recorded child's PTY, it assumes a fixed default size that does not
        match the real window, and cursor-position math for mirrored output
        comes out wrong (garbled/overlapping text).
        """
        info = _CONSOLE_SCREEN_BUFFER_INFO()
        ok = _kernel32.GetConsoleScreenBufferInfo(self._stdout_handle, ctypes.byref(info))
        if not ok:
            raise OSError("GetConsoleScreenBufferInfo failed")
        cols = info.srWindow.Right - info.srWindow.Left + 1
        rows = info.srWindow.Bottom - info.srWindow.Top + 1
        return rows, cols
