# aicap PowerShell command-boundary hook.
#
# Injected by aicap/pty_backend/windows_backend.py via a spawn argv shaped
# like:
#   powershell.exe -NoExit -ExecutionPolicy Bypass -File <this file>
#
# Deliberately no -NoProfile: PowerShell's console host runs the user's own
# $PROFILE scripts before executing a -File script, the same way bash_hook.sh
# and zsh_hook.sh explicitly source the user's ~/.bashrc / ~/.zshrc before
# installing their own hooks (docs/plan.md architecture section, P4
# non-destructive rule) -- letting the normal profile-loading sequence run
# first gets the same non-destructive behavior here for free, with no
# manual dot-sourcing needed.
#
# Wire format written to AICAP_MARKER_FILE (one JSON object per line, see
# docs/plan.md's boundary-signal-channel section):
#   {"event":"start","command":"..."}
#   {"event":"end","exit_code":N}
#
# Event objects are built with ConvertTo-Json rather than manual string
# concatenation/escaping -- this is what docs/plan.md stage 0b validated and
# recommends: ConvertTo-Json correctly escapes arbitrary special characters
# (quotes, backslashes, ...) in the command text, avoiding the class of bug
# that had to be found and fixed by hand in bash_hook.sh/zsh_hook.sh (which
# have no built-in JSON encoder and escape manually).
#
# IMPORTANT: state is kept in $global: scoped variables/functions, not in
# plain script-local variables or closures. The Set-PSReadLineKeyHandler
# scriptblock and the "prompt" function below both run *later*, after this
# script has already finished executing and its own local scope is gone --
# a plain "$var = ..." here would be unreachable ($null) by the time either
# of them runs. This was an empirically found and fixed bug in docs/plan.md
# stage 0b; do not change these back to script-local variables.

# Force this session's console output encoding to UTF-8. Without this,
# Windows PowerShell 5.1 on a non-English-locale Windows install (e.g.
# Chinese, GBK/cp936) writes *some* output categories -- native error
# formatting, the console window title -- through the console's legacy
# ANSI/OEM codepage instead of UTF-8, while ordinary cmdlet output (e.g.
# Write-Output) already goes out as UTF-8. windows_backend.py's read_output()
# decodes everything it reads from this child as UTF-8 (see that module's
# own docstring on this exact, previously-flagged-as-unverified risk) --
# a real repro confirmed the mismatch: Chinese text in a native error
# message and the window title came through as mojibake, while Chinese text
# from Write-Output (already covered by
# tests/test_windows_backend.py::test_non_ascii_output_round_trips) did not.
# Setting this makes every one of this session's own output paths agree on
# one encoding, matching what the read side already assumes. Best-effort:
# some hosts do not allow changing it, so a failure here should not stop the
# hook from installing the rest of its command-boundary tracking.
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {
    Write-Warning "aicap: could not set console output encoding to UTF-8; non-ASCII output may be garbled."
}

if (-not $env:AICAP_MARKER_FILE) {
    Write-Warning "aicap: AICAP_MARKER_FILE is not set; boundary events will not be recorded."
} else {
    $global:AicapMarkerFile = $env:AICAP_MARKER_FILE
    # Add-Content's own -Encoding utf8 writes a BOM on Windows PowerShell 5.1
    # (a well-known PS5.1 quirk; PowerShell 7's utf8 does not), and a BOM
    # landing mid-file would corrupt whichever JSON line follows it. Writing
    # via .NET's UTF8Encoding($false) (no BOM) directly sidesteps the
    # question entirely instead of depending on Add-Content's version-
    # dependent default encoding -- empirically, that default was NOT UTF-8
    # here: a Chinese command's marker line failed to decode as UTF-8 on the
    # Python side and was silently dropped before this fix (see docs/plan.md
    # stage 5 notes).
    $global:AicapUtf8NoBom = New-Object System.Text.UTF8Encoding($false)

    function global:AicapWriteMarkerLine([string]$line) {
        [System.IO.File]::AppendAllText($global:AicapMarkerFile, $line + "`n", $global:AicapUtf8NoBom)
    }

    Set-PSReadLineKeyHandler -Key Enter -ScriptBlock {
        param($key, $arg)

        $line = $null
        $cursor = $null
        [Microsoft.PowerShell.PSConsoleReadLine]::GetBufferState([ref]$line, [ref]$cursor)

        $evt = [ordered]@{ event = 'start'; command = $line } | ConvertTo-Json -Compress
        AicapWriteMarkerLine $evt

        [Microsoft.PowerShell.PSConsoleReadLine]::AcceptLine($key, $arg)
    }

    function global:prompt {
        # $LASTEXITCODE alone is not enough: PowerShell only updates it for
        # *native* executable invocations. A cmdlet-level failure (e.g. a
        # mistyped/unknown command, a terminating cmdlet error) leaves
        # $LASTEXITCODE untouched -- reading it directly would misreport
        # such a failure using whatever stale value (often 0/null) was left
        # over from an earlier native command. $? reflects success/failure
        # for both cmdlets and native commands, so it takes priority; only
        # when $? is false do we fall back to $LASTEXITCODE for the actual
        # numeric code, with 1 as a last resort when no native code exists
        # (found empirically: docs/plan.md stage 5 notes).
        if ($?) {
            $ec = 0
        } elseif ($LASTEXITCODE) {
            $ec = $LASTEXITCODE
        } else {
            $ec = 1
        }

        $evt = [ordered]@{ event = 'end'; exit_code = $ec } | ConvertTo-Json -Compress
        AicapWriteMarkerLine $evt

        "PS $($executionContext.SessionState.Path.CurrentLocation)> "
    }
}
