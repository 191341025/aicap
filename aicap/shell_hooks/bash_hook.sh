# aicap bash command-boundary hook.
#
# Injected by aicap/pty_backend/unix_backend.py via:
#   bash --rcfile <this file> -i
#
# --rcfile makes bash read *only* this file instead of ~/.bashrc, so the
# first thing this hook does (after checking AICAP_MARKER_FILE) is source
# the user's own ~/.bashrc, to stay non-destructive to their existing
# aliases/prompt/customizations (docs/plan.md architecture section, P4
# non-destructive rule) instead of silently replacing their shell setup
# with a bare one.
#
# Wire format written to AICAP_MARKER_FILE (one JSON object per line, see
# docs/plan.md's boundary-signal-channel section):
#   {"event":"start","command":"..."}
#   {"event":"end","exit_code":N}
#
# IMPORTANT: `trap ... DEBUG` must be installed AFTER PROMPT_COMMAND is
# assigned, not before. Installing it first means the PROMPT_COMMAND
# assignment statement itself fires the just-installed DEBUG trap while
# $PROMPT_COMMAND is still unset, so the "is this BASH_COMMAND actually
# PROMPT_COMMAND" guard can't match yet, and the assignment gets
# misrecorded as a fake command. This was empirically verified in stage 0
# of docs/plan.md using this exact `bash --rcfile ... -i` invocation style
# (not a heredoc-fed interactive shell, which hid the bug) -- do not
# reorder these two blocks.

if [ -z "$AICAP_MARKER_FILE" ]; then
    echo "aicap: AICAP_MARKER_FILE is not set; boundary events will not be recorded." >&2
else
    if [ -f "$HOME/.bashrc" ]; then
        # shellcheck disable=SC1090
        source "$HOME/.bashrc"
    fi

    # Needed for `history 1` in _aicap_preexec below to have anything to
    # read -- on by default for an interactive shell, but made explicit
    # rather than assumed.
    set -o history

    # Set once here (before the DEBUG trap below is even installed, so this
    # assignment cannot itself be mistaken for a real command) and reset
    # once per prompt cycle in _aicap_precmd -- see _aicap_preexec's own
    # comment for why this exists.
    _aicap_should_capture=1

    _aicap_precmd() {
        local ec=$?
        printf '{"event":"end","exit_code":%d}\n' "$ec" >> "$AICAP_MARKER_FILE"
        # A plain assignment here, inside a function body, does not itself
        # re-trigger the DEBUG trap below: bash does not propagate a DEBUG
        # trap into function bodies unless `set -o functrace` is on, which
        # this hook does not enable -- verified empirically (docs/plan.md).
        _aicap_should_capture=1
        return "$ec"
    }

    # Append rather than overwrite, in case the user's own .bashrc (just
    # sourced above) already set PROMPT_COMMAND -- P4 non-destructive rule.
    if [ -n "$PROMPT_COMMAND" ]; then
        PROMPT_COMMAND="$PROMPT_COMMAND"$'\n''_aicap_precmd'
    else
        PROMPT_COMMAND='_aicap_precmd'
    fi

    _aicap_preexec() {
        # Bash-completion internals run with COMP_LINE set; not a real
        # user command, skip it.
        [ -n "$COMP_LINE" ] && return
        # Skip the DEBUG trap firing for our own precmd function, and for
        # PROMPT_COMMAND itself when it is still a single statement (the
        # common case: no pre-existing user PROMPT_COMMAND).
        case "$BASH_COMMAND" in
            _aicap_precmd) return ;;
        esac
        [ "$BASH_COMMAND" = "$PROMPT_COMMAND" ] && return
        # The DEBUG trap fires once per *simple command*, not once per
        # top-level command line -- for a pipeline like "a | b", it fires
        # separately for "a" and for "b", each time with $BASH_COMMAND set
        # to only that one stage, never the full pipeline text. Confirmed
        # both empirically and via a real repro (docs/plan.md): a real
        # session recorded "env | head -20" as just "head -20", the *last*
        # pipe stage having silently overwritten the first. Capturing only
        # on the first DEBUG firing for a given command line (guarded by
        # _aicap_should_capture, reset once per prompt cycle in
        # _aicap_precmd above) and reading the actual command text from
        # bash's own history via `history 1` -- instead of $BASH_COMMAND --
        # sidesteps this entirely: history always holds the complete,
        # verbatim line the user typed, pipes and all. This is the same
        # technique the well-established bash-preexec project uses for this
        # exact, well-known DEBUG-trap-and-pipelines limitation.
        [ -n "$_aicap_should_capture" ] || return
        unset _aicap_should_capture
        # `history 1`, not `fc -ln -1`: both claim to return "the last
        # history entry", but verified empirically (docs/plan.md) that at
        # DEBUG-trap time, `fc -ln -1` is stale by exactly one entry (it
        # returns the *previous* command, not the one currently about to
        # run), while `history 1` is not -- a real difference between the
        # two, not interchangeable despite the docs suggesting otherwise.
        # `history 1`'s output is "<right-justified number><whitespace
        # >-separated><command text>"; strip only that fixed number+
        # whitespace prefix via regex (not a plain leading-whitespace trim,
        # which would leave the number itself in place) so a command with
        # meaningful internal whitespace still round-trips exactly.
        _aicap_raw_command="$(history 1)"
        if [[ "$_aicap_raw_command" =~ ^[[:space:]]*[0-9]+[[:space:]]+(.*)$ ]]; then
            _aicap_command="${BASH_REMATCH[1]}"
        else
            _aicap_command="$_aicap_raw_command"
        fi
        # Escape backslashes before quotes (order matters: escaping quotes
        # first would introduce new backslashes that then get re-escaped).
        # Without this, a command containing a literal backslash (regex,
        # a Windows-style path string, etc.) produces invalid JSON that
        # read_new_boundary_events() silently drops -- the command would
        # vanish from the recording instead of erroring loudly.
        _aicap_escaped="${_aicap_command//\\/\\\\}"
        _aicap_escaped="${_aicap_escaped//\"/\\\"}"
        printf '{"event":"start","command":"%s"}\n' "$_aicap_escaped" >> "$AICAP_MARKER_FILE"
    }
    trap '_aicap_preexec' DEBUG
fi
