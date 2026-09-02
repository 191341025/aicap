# aicap zsh command-boundary hook.
#
# Sourced from a generated .zshrc placed in a temporary ZDOTDIR (see
# aicap/pty_backend/unix_backend.py) -- that generated .zshrc first sources
# the user's real $HOME/.zshrc (since pointing ZDOTDIR elsewhere means zsh
# would otherwise never find it) and then sources this file, so this file
# itself does not need to worry about the user's original rc; it only
# needs to append its own hooks non-destructively (docs/plan.md
# architecture section, P4 non-destructive rule), in case the user's own
# .zshrc already defined preexec/precmd functions or populated the hook
# arrays.
#
# zsh's preexec/precmd are true special functions with dedicated
# `preexec_functions`/`precmd_functions` array hooks (unlike bash, which
# has no native equivalent and needs the `trap ... DEBUG` workaround in
# bash_hook.sh) -- appending to these arrays runs every registered
# function in order, ours included, without clobbering anything the user
# already had registered. No install-order tricks are needed here; stage 0
# of docs/plan.md verified plain preexec/precmd functions work correctly
# via a real `pty.fork()`-driven `zsh -i`, and appending to the array form
# is the documented non-destructive zsh idiom for the same mechanism.
#
# Wire format written to AICAP_MARKER_FILE (one JSON object per line, see
# docs/plan.md's boundary-signal-channel section):
#   {"event":"start","command":"..."}
#   {"event":"end","exit_code":N}

if [ -z "$AICAP_MARKER_FILE" ]; then
    echo "aicap: AICAP_MARKER_FILE is not set; boundary events will not be recorded." >&2
else
    _aicap_preexec() {
        # Escape backslashes before quotes (order matters: escaping quotes
        # first would introduce new backslashes that then get re-escaped).
        # Without this, a command containing a literal backslash produces
        # invalid JSON that read_new_boundary_events() silently drops.
        local escaped="${1//\\/\\\\}"
        escaped="${escaped//\"/\\\"}"
        printf '{"event":"start","command":"%s"}\n' "$escaped" >> "$AICAP_MARKER_FILE"
    }

    _aicap_precmd() {
        local ec=$?
        printf '{"event":"end","exit_code":%d}\n' "$ec" >> "$AICAP_MARKER_FILE"
    }

    preexec_functions+=(_aicap_preexec)
    precmd_functions+=(_aicap_precmd)
fi
