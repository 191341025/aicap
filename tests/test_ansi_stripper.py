"""Unit tests for aicap.ansi_stripper.

All escape sequences below are real byte sequences (\\x1b == ESC), not
descriptions of what they mean -- this matches the raw ConPTY/PTY output
the function is meant to clean up (see stage 0 findings in docs/plan.md).
"""

from aicap.ansi_stripper import strip_ansi_sequences


class TestPlainTextUnaffected:
    """Plain text with no escape sequences or control characters must pass
    through unchanged."""

    def test_plain_ascii_text_is_unchanged(self):
        text = "hello world\nsecond line\n"
        assert strip_ansi_sequences(text) == text

    def test_plain_text_with_non_ascii_characters_is_unchanged(self):
        # session.log / commands/*.log may legitimately contain non-ASCII
        # bytes from the user's real command output (see plan.md's
        # character-set policy exception for recorded content).
        text = "你好，世界\n"
        assert strip_ansi_sequences(text) == text

    def test_empty_string_returns_empty_string(self):
        assert strip_ansi_sequences("") == ""


class TestCsiSequencesAreStripped:
    """CSI sequences (ESC '[' ...) must be removed: colors, cursor moves,
    erase-line, and DEC private mode toggles -- not just SGR colors."""

    def test_sgr_color_code_is_stripped(self):
        assert strip_ansi_sequences("\x1b[93mwarning\x1b[0m") == "warning"

    def test_multiple_sgr_parameters_are_stripped(self):
        assert strip_ansi_sequences("\x1b[1;31mbold red\x1b[0m") == "bold red"

    def test_cursor_show_hide_private_mode_is_stripped(self):
        # Observed in real ConPTY output during stage 0 verification.
        text = "\x1b[?25lworking\x1b[?25h"
        assert strip_ansi_sequences(text) == "working"

    def test_cursor_movement_sequence_is_stripped(self):
        # e.g. cursor up 2 lines, then forward 4 columns.
        text = "before\x1b[2A\x1b[4Cafter"
        assert strip_ansi_sequences(text) == "beforeafter"

    def test_erase_in_line_sequence_is_stripped(self):
        text = "abc\x1b[2Kdef"
        assert strip_ansi_sequences(text) == "abcdef"

    def test_cursor_position_report_sequence_is_stripped(self):
        text = "abc\x1b[10;20Hdef"
        assert strip_ansi_sequences(text) == "abcdef"


class TestOscAndSimpleEscapesAreStripped:
    def test_osc_window_title_terminated_by_bel_is_stripped(self):
        text = "\x1b]0;My Title\x07prompt> "
        assert strip_ansi_sequences(text) == "prompt> "

    def test_osc_window_title_terminated_by_st_is_stripped(self):
        text = "\x1b]0;My Title\x1b\\prompt> "
        assert strip_ansi_sequences(text) == "prompt> "

    def test_simple_two_byte_escape_is_stripped(self):
        # ESC 'M' -- reverse index, a non-CSI/OSC escape.
        text = "abc\x1bMdef"
        assert strip_ansi_sequences(text) == "abcdef"


class TestBackspaceHandling:
    """Backspace (\\x08) runs from PowerShell/PSReadLine-style character-
    by-character line redraws must resolve to what a human would actually
    see, not leave raw control bytes in the output."""

    def test_single_backspace_deletes_preceding_character(self):
        assert strip_ansi_sequences("ab\x08c") == "ac"

    def test_consecutive_backspaces_delete_multiple_characters(self):
        # Regression case: a naive "strip one char per backspace *run*"
        # implementation would wrongly produce "abd" here instead of "ad".
        assert strip_ansi_sequences("abc\x08\x08d") == "ad"

    def test_backspace_run_deleting_entire_word(self):
        assert strip_ansi_sequences("wrong\x08\x08\x08\x08\x08right") == "right"

    def test_leading_stray_backspace_is_dropped(self):
        assert strip_ansi_sequences("\x08\x08hello") == "hello"

    def test_backspace_does_not_erase_across_newline(self):
        text = "line one\n\x08\x08line two"
        assert strip_ansi_sequences(text) == "line one\nline two"


class TestCarriageReturnRedraw:
    """A bare \\r moves the cursor to the start of the line, so later text
    overwrites earlier text on the same line rather than appending -- this
    is how progress-bar-style redraws and PSReadLine's full-line rewrites
    render on a real terminal."""

    def test_carriage_return_overwrite_keeps_final_render(self):
        # "abc", \r back to column 0, "xy" overwrites 'a' and 'b';
        # trailing 'c' is untouched -- matches real terminal rendering.
        assert strip_ansi_sequences("abc\rxy") == "xyc"

    def test_carriage_return_with_longer_replacement(self):
        assert strip_ansi_sequences("50%\r100%") == "100%"

    def test_carriage_return_newline_is_treated_as_normal_newline(self):
        assert strip_ansi_sequences("line one\r\nline two") == "line one\nline two"

    def test_multiple_redraws_on_one_line(self):
        assert strip_ansi_sequences("a\rb\rc") == "c"


class TestMixedRealisticOutput:
    """Combinations closer to what real ConPTY/PTY output looks like."""

    def test_colored_prompt_with_cursor_toggle_and_backspace_redraw(self):
        # Simulates PSReadLine coloring input then the user correcting a
        # typo with backspace, roughly as observed in stage 0 verification.
        text = "\x1b[?25l\x1b[93mgid\x08t status\x1b[0m\x1b[?25h"
        assert strip_ansi_sequences(text) == "git status"

    def test_ansi_colored_multiline_command_output(self):
        text = "\x1b[32mOK\x1b[0m: step one\n\x1b[31mFAIL\x1b[0m: step two\n"
        assert strip_ansi_sequences(text) == "OK: step one\nFAIL: step two\n"
