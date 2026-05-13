"""
Atomic JSON writes — prevents readers from seeing a partial/truncated
file mid-write.

Both bots write their state files (bot_state.json, indian_bot_state.json,
trade_log.json, positions_state.json, ...) every loop tick. The dashboard
reads the same files every few seconds. Without atomic writes, a reader
that lands on the file *during* a write gets a half-written JSON document
and json.load() raises — surfacing as an HTTP 500 in the UI.

The fix: write to a sibling temp file in the same directory, fsync, then
os.replace() it onto the target. os.replace() is atomic on every modern
OS for files on the same filesystem.
"""
import json
import os
import tempfile


def write_json_atomic(path: str, data, *, indent=None) -> None:
    """Write `data` as JSON to `path` atomically.

    Crash-safe and reader-safe: the target file either contains the
    previous version or the new version, never a half-written one.
    """
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".tmp.", suffix=".json", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent, default=str)
            f.flush()
            try:
                os.fsync(f.fileno())
            except (OSError, AttributeError):
                # fsync not supported (Windows on some FSes) — best-effort
                pass
        os.replace(tmp, path)
    except Exception:
        # Don't leave the temp turd around on failure
        try: os.unlink(tmp)
        except OSError: pass
        raise


def append_json_list_atomic(path: str, entry, *, max_entries: int = 5000) -> None:
    """Append `entry` to a JSON list stored at `path`, atomically.

    Reads existing list (or [] if missing/corrupt), appends, trims to
    max_entries from the end, writes back atomically.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            existing = json.load(f)
            if not isinstance(existing, list):
                existing = []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        existing = []
    existing.append(entry)
    if len(existing) > max_entries:
        existing = existing[-max_entries:]
    write_json_atomic(path, existing)
