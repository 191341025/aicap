# aicap

[中文文档](https://github.com/191341025/aicap/blob/main/README.zh-CN.md)

Record what happens in your terminal so an AI assistant can read it directly — no more copy-pasting command output or screenshots back and forth.

## The problem

A common workflow when pairing with an AI coding assistant: the AI suggests a command, you copy it into another terminal (a local shell, a remote SSH session, whatever), run it, and then copy the output back so the AI can see what happened. That round trip is slow and easy to mess up, especially for long or frequent commands.

## What aicap does

`aicap` takes over an interactive shell session. You start it once, pointing it at a directory; from then on you use that terminal exactly as before — same shell, same aliases, same PATH, same everything — and every command you run gets recorded (its text, its output, its exit code) as structured files in that directory. When you want the AI to see what just happened, you just tell it where to look.

Nothing about the terminal changes from your side. `aicap` sits between you and your real shell, mirrors everything back to your screen unchanged, and writes a structured copy to disk on the side.

## Supported platforms

- Windows: PowerShell 5.1 (Windows PowerShell, the default) — this is the extensively tested path. PowerShell 7 (`pwsh`) is also supported via `aicap start <log_dir> --shell pwsh`, but has seen far less real-world testing so far.
- Linux / macOS: bash, zsh

fish and cmd.exe are not supported.

## Install

Not yet published to PyPI — install directly from this repository for now:

```bash
pipx install "git+https://github.com/191341025/aicap.git"
```

[`pipx`](https://pipx.pypa.io/) is the recommended way to install a command-line tool like this one: it puts `aicap` in its own isolated environment so it can't conflict with dependencies from your other Python projects, while still making the `aicap` command available everywhere. It's also the *only* method below that reliably works on newer Linux distros (see the note below).

Don't have `pipx` yet?

```bash
# Debian/Ubuntu (Debian 12+, Ubuntu 23.04+)
sudo apt install pipx
pipx ensurepath

# macOS
brew install pipx
pipx ensurepath

# Other platforms (incl. Windows)
pip install --user pipx
pipx ensurepath
```

See the [official pipx install guide](https://pipx.pypa.io/latest/how-to/install-pipx.html) for anything not covered above.

Plain `pip` also works, on most systems:

```bash
pip install "git+https://github.com/191341025/aicap.git"
```

> **Note:** on newer Linux distros (Debian 12+, Ubuntu 23.04+, and others that follow [PEP 668](https://peps.python.org/pep-0668/)), a plain `pip install` like this — and even `pip install --user` — is blocked by default with `error: externally-managed-environment`, specifically to steer you toward `pipx` (or a virtual environment) for exactly this kind of standalone command-line tool. If you hit that error, use the `pipx` install above instead.

Requires Python 3.9+ and `git` on your PATH.

## Quick start

`aicap start` takes one argument: a directory to write the recording to. That directory has nothing to do with where you're working — it's just where the logs go. Most people point it at one fixed location (e.g. somewhere under their home directory) and reuse it across every project, so their AI assistant always knows where to look, no matter what they're currently working on.

1. Open a terminal wherever you're actually working (a project directory, for example) and start a session:

   ```bash
   # bash / zsh
   aicap start ~/aicap-logs
   ```

   ```powershell
   # PowerShell (note: use $HOME, not ~ — PowerShell doesn't expand ~ the way bash does)
   aicap start $HOME\aicap-logs
   ```

   This does **not** change your terminal's working directory — you're still exactly where you started. The path you pass is only where the recording gets written; it can be anywhere, and doesn't need to relate to your project at all.

   You'll see:

   ```
   aicap: recording started, writing to /home/you/aicap-logs
   aicap: session id 20260101-120000-a1b2c3
   ```

2. Use the terminal exactly as you normally would. Nothing looks different.

   ```bash
   $ npm test
   $ git status
   $ python train.py --epochs 5
   ```

3. When you're done, exit the shell like you always do:

   ```bash
   $ exit
   ```

   ```
   aicap: recording finished, session 20260101-120000-a1b2c3 saved
   ```

4. Point your AI assistant at `~/aicap-logs` (or wherever you passed to `start`). The most useful entry points:

   - `STATUS.md` — a short, human-readable summary of the last several commands and their exit codes. This is the file to read first.
   - `latest.log` — the full output of the most recently finished command.
   - `sessions/<session-id>/index.json` — structured metadata (command text, timestamps, exit code, output file, completion status) for every command in the session.
   - `sessions/<session-id>/commands/NNNN-*.log` — the full output of any individual command.

Check on a session at any time (even a past one) without starting a new recording:

```bash
aicap status ~/aicap-logs
```

## How it works, briefly

`aicap` spawns your real shell (PowerShell, bash, or zsh) as a child process behind a pseudo-terminal, the same mechanism tools like `tmux` and `ssh` use. It forwards your keystrokes to that shell and mirrors its output back to your screen unchanged — `aicap` never interprets or intercepts what you type, including Ctrl+C. In parallel, a small shell hook tells `aicap` when each command starts and ends (and with what exit code), which is how it knows where to split the recorded output into per-command files.

## Known limitations

- **Windows: a running command can't be interrupted with Ctrl+C.** This is a platform limitation in how Windows' ConPTY delivers console control events to a hosted child process, not something `aicap`'s own code can work around. Everything else (command recording, exit codes, normal input/output) works normally on Windows; only interrupting a long-running command doesn't.
- **Nested SSH isn't split into individual commands.** If you `ssh` into another machine from within a recorded session, `aicap` has no visibility into that remote shell — the whole SSH session is recorded as one large "command" from the moment `ssh` starts to the moment it exits.
- fish and cmd.exe are not supported.

## License

MIT — see [`LICENSE`](https://github.com/191341025/aicap/blob/main/LICENSE).
