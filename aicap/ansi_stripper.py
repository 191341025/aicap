"""Strip ANSI/VT escape sequences from raw terminal output.

Real ConPTY/PTY output is not clean text: it is interleaved with cursor
positioning, color, and mode-toggle escape sequences, and with control
characters (``\\r``, ``\\x08``) that terminals use to redraw a line in
place. Stage 0 verification of pywinpty confirmed this empirically -- real
PowerShell output contained sequences like ``\\x1b[93m`` (color) and
``\\x1b[?25h`` / ``\\x1b[?25l`` (cursor show/hide), not just simple color
codes.

This module turns that raw stream into the text a human would actually see
if they were looking at the rendered terminal, so it can be written to
``commands/*.log`` as readable plain text. It does not aim to be a full
terminal emulator (no cursor-addressed overwrite, no scrollback
reconstruction) -- see ``strip_ansi_sequences`` docstring for the exact
scope.
"""

import re

# CSI (Control Sequence Introducer) sequences: ESC '[' followed by any
# number of parameter bytes (0x30-0x3F, i.e. "0-9;:<=>?"), then any number
# of intermediate bytes (0x20-0x2F, i.e. space and "!\"#$%&'()*+,-./"),
# then exactly one final byte (0x40-0x7E, i.e. "@A-Z[\\]^_`a-z{|}~").
# This is the general ANSI X3.64 / ECMA-48 CSI grammar, not just the
# "\x1b[<n>m" color subset -- it also matches cursor movement (\x1b[2K),
# cursor show/hide (\x1b[?25h, \x1b[?25l), scroll region setup, etc.
_CSI_SEQUENCE = re.compile(r"\x1b\[[0-9;:<=>?]*[ -/]*[@-~]")

# OSC (Operating System Command) sequences: ESC ']' ... terminated by BEL
# (\x07) or the two-byte ST terminator ESC '\\'. Used for things like
# setting the terminal window title.
_OSC_SEQUENCE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

# Other common two-byte escape sequences that are not CSI/OSC: ESC followed
# by a single byte in 0x40-0x5F (e.g. ESC 'M' = reverse index, ESC '=' /
# ESC '>' = keypad mode). Kept last and narrow on purpose so it cannot
# accidentally eat a CSI/OSC sequence that failed to match above.
_SIMPLE_ESCAPE = re.compile(r"\x1b[@-Z\\-_=>]")



def strip_ansi_sequences(text: str) -> str:
    """Remove ANSI/VT escape sequences and resolve simple line redraws.

    This turns raw PTY/ConPTY output into the plain text a human would see
    on screen, well enough for AI/human reading of a recorded command's
    output. Specifically it:

    1. Removes CSI sequences (``\\x1b[...``), covering the full ANSI
       parameter/intermediate/final-byte grammar -- not just SGR color
       codes (``\\x1b[31m``) but also cursor movement, erase-line, and
       DEC private mode toggles like ``\\x1b[?25l`` / ``\\x1b[?25h``.
    2. Removes OSC sequences (``\\x1b]...BEL`` or ``...ESC \\``), e.g.
       terminal title changes.
    3. Removes other simple two-byte escape sequences (``\\x1b`` + one
       byte).
    4. Resolves backspace (``\\x08``) runs by deleting the preceding
       character(s) they erase, so "abc\\x08\\x08d" reads as "ad" instead
       of leaving raw control bytes in the output.
    5. Resolves carriage-return (``\\r``) line redraws: when a line is
       rewritten in place with ``\\r...``, only the final version of that
       line is kept, matching what a real terminal would display.

    This is intentionally not a full terminal emulator: it does not track
    cursor position for arbitrary cursor-addressing sequences (e.g.
    ``\\x1b[<row>;<col>H``) and does not reconstruct scrollback. For the
    redraw patterns actually observed from ConPTY/PSReadLine and POSIX
    shells (repeated backspace, or carriage-return-then-reprint), the
    result matches what a human would see; anything fancier (full-screen
    TUIs like vim/top) is out of scope by design -- plan.md's 5MB
    truncation limit exists specifically to bound that case.

    Args:
        text: Raw text captured from a PTY/ConPTY, potentially containing
            escape sequences and redraw control characters.

    Returns:
        Plain, human-readable text with escape sequences removed and
        backspace/carriage-return redraws resolved.

    Examples:
        >>> strip_ansi_sequences("\\x1b[93mwarning\\x1b[0m")
        'warning'
        >>> strip_ansi_sequences("ab\\x08c")
        'ac'
    """
    result = _CSI_SEQUENCE.sub("", text)
    result = _OSC_SEQUENCE.sub("", result)
    result = _SIMPLE_ESCAPE.sub("", result)
    result = _resolve_backspaces(result)
    result = _resolve_carriage_returns(result)
    return result


def _resolve_backspaces(text: str) -> str:
    """Delete characters erased by runs of backspace (``\\x08``) bytes.

    Each backspace erases exactly one preceding character, so this walks
    the text once with an explicit output stack: a normal character is
    pushed, a backspace pops the last pushed character. This handles
    consecutive backspaces correctly (e.g. "abc\\x08\\x08d" -> "ad"), unlike
    a naive regex that only removes one character per backspace run.
    A backspace is not allowed to erase across a newline (nothing to erase
    on a fresh line) or past the start of the string -- either case just
    drops the stray backspace.
    """
    output = []
    for character in text:
        if character == "\x08":
            if output and output[-1] != "\n":
                output.pop()
            # else: stray backspace with nothing to erase; drop it.
        else:
            output.append(character)
    return "".join(output)


def _resolve_carriage_returns(text: str) -> str:
    """Resolve ``\\r``-driven in-place line redraws, line by line.

    A terminal treats ``\\r`` as "move the cursor to the start of the
    current line", so text written after a ``\\r`` overwrites what was
    already on that line rather than appending to it. This function
    applies that rule per output line, keeping only the final rendered
    content of each line and dropping a trailing bare ``\\r`` (as opposed
    to ``\\r\\n``, which is a normal newline).
    """
    lines = text.split("\n")
    resolved_lines = []
    for line in lines:
        segments = line.split("\r")
        rendered = ""
        for segment in segments:
            if len(segment) >= len(rendered):
                rendered = segment
            else:
                rendered = segment + rendered[len(segment):]
        resolved_lines.append(rendered)
    return "\n".join(resolved_lines)
