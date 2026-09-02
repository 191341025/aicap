# aicap

Record what happens in your terminal so an AI assistant can read it directly — no more copy-pasting command output or screenshots back and forth.

## The problem

A common workflow when pairing with an AI coding assistant: the AI suggests a command, you copy it into another terminal (a local shell, a remote SSH session, whatever), run it, and then copy the output back so the AI can see what happened. That round trip is slow and easy to mess up.

## What aicap does

`aicap` takes over an interactive shell session. You start it once, pointing it at a directory; from then on you use that terminal exactly as before, and every command you run gets recorded — its text, its output, its exit code — as structured files in that directory. When you want the AI to see what just happened, you just tell it where to look.

## Status

Early development. The design is done (see `docs/plan.md`) and the core platform assumptions (PowerShell on Windows, bash/zsh on Linux/macOS) have been validated against real environments. Implementation is in progress — not yet usable.

## Supported platforms

- Windows: PowerShell 5.1+
- Linux / macOS: bash, zsh

fish and cmd.exe are not planned for the initial release.

## Install

Not published yet. Once released:

```
pipx install aicap
```

## Design

See [`docs/plan.md`](docs/plan.md) for the full architecture and design decisions.

## License

MIT — see [`LICENSE`](LICENSE).
