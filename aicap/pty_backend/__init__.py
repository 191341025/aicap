"""Platform-specific PTY backends implementing `PtyBackendBase`.

See `aicap/pty_backend/base.py` for the shared abstract interface and
docs/plan.md's "总体架构" section for why this exists as a separate,
storage-layer-agnostic package: `unix_backend.py` (this stage) and the
future `windows_backend.py` (stage 5) are the only platform-specific
pieces of aicap; `recorder.py` (stage 6) is the only module that will know
both "a backend" and "a storage layer" exist.
"""
