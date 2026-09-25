"""Obsidian vault access with path sandboxing."""
from __future__ import annotations

import datetime
import os
import subprocess
import threading
from pathlib import Path


class VaultAccessError(Exception):
    pass


class Vault:
    """Sandboxed accessor for an Obsidian-style note vault.

    Security boundary: _resolve() blocks absolute paths, parent traversal,
    and symlink escapes at resolution time, and file opens use O_NOFOLLOW on
    the final path component. A concurrent local process that swaps a parent
    directory component for a symlink between resolve and open can still
    defeat this (TOCTOU); the vault is a single-user directory, so the
    sandbox is designed against accidental and remote misuse, not a hostile
    local process.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self._append_lock = threading.Lock()

    def _resolve(self, rel: str) -> Path:
        """Validate and resolve a relative path against the vault root."""
        if not rel or not rel.strip():
            raise VaultAccessError("Empty path")
        p = Path(rel)
        if p.is_absolute():
            raise VaultAccessError("Absolute paths not allowed")
        full = (self.root / p).resolve()
        if full != self.root and not full.is_relative_to(self.root):
            raise VaultAccessError("Path outside vault")
        return full

    def _open_nofollow(self, path: Path, flags: int, mode: int = 0o644) -> int:
        """open(2) with O_NOFOLLOW so a symlinked final component fails."""
        try:
            return os.open(path, flags | os.O_NOFOLLOW, mode)
        except OSError as e:
            raise VaultAccessError(str(e)) from e

    def append(self, rel: str, text: str) -> str:
        """Append a timestamped line to a note, creating it if absent."""
        if not text.strip():
            raise VaultAccessError("Empty text")
        p = self._resolve(rel)
        stamp = datetime.datetime.now().strftime("%H:%M")
        line = f"- {stamp} {text.strip()}"
        with self._append_lock:
            try:
                if p.exists() and not p.is_file():
                    raise VaultAccessError(f"Not a regular file: {rel}")
                p.parent.mkdir(parents=True, exist_ok=True)
                if p.exists() and p.stat().st_size > 0:
                    fd = self._open_nofollow(p, os.O_RDONLY)
                    with os.fdopen(fd, "rb") as rf:
                        rf.seek(-1, 2)
                        if rf.read(1) != b"\n":
                            line = "\n" + line
                fd = self._open_nofollow(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
                with os.fdopen(fd, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except VaultAccessError:
                raise
            except OSError as e:
                raise VaultAccessError(str(e)) from e
        return f"Appended to {rel}"

    def read(self, rel: str, max_chars: int = 1500) -> str:
        """Read up to max_chars of a note, truncating with an ellipsis."""
        max_chars = max(1, min(int(max_chars), 4000))
        p = self._resolve(rel)
        if not p.is_file():
            raise VaultAccessError(f"Note not found: {rel}")
        try:
            fd = self._open_nofollow(p, os.O_RDONLY)
            with os.fdopen(fd, "rb") as f:
                text = f.read().decode("utf-8", errors="replace")
        except OSError as e:
            raise VaultAccessError(str(e)) from e
        return text[:max_chars] + ("..." if len(text) > max_chars else "")

    def search(self, query: str, max_results: int = 5) -> str:
        """Search vault notes for a fixed string via ripgrep, return excerpts."""
        max_results = max(1, min(int(max_results), 10))
        try:
            files = subprocess.run(
                ["rg", "-l", "-i", "-F", "-I", "--", query, str(self.root)],
                capture_output=True, text=True, timeout=15,
            )
        except FileNotFoundError:
            return "Search unavailable (ripgrep not installed)"
        except subprocess.TimeoutExpired:
            return "Search timed out"
        if files.returncode == 2:
            return "Search error (invalid query)"
        paths = [line for line in files.stdout.splitlines() if line.strip()][:max_results]
        if not paths:
            return "No matches."
        out = []
        for p in paths:
            try:
                rel = str(Path(p).relative_to(self.root))
            except ValueError:
                continue
            try:
                excerpt = subprocess.run(
                    ["rg", "-i", "-F", "-I", "--max-count", "1", "--", query, p],
                    capture_output=True, text=True, timeout=15,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
            first = excerpt.stdout.splitlines()[0].strip() if excerpt.stdout.strip() else ""
            out.append(f"{rel}: {first[:200]}")
        return "\n".join(out)
