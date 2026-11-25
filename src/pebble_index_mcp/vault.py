"""Obsidian vault access with path sandboxing."""
from __future__ import annotations

import datetime
import json
import os
import re
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

    def _resolve_note(self, rel: str, *, for_write: bool = False) -> tuple[Path, str]:
        """Resolve a note path, matching case-insensitive .md when extension omitted."""
        p = self._resolve(rel)
        if p.exists() or p.is_symlink() or p.suffix:
            return p, rel
        parent = p.parent
        target_md = f"{p.name.lower()}.md"
        if parent.is_dir():
            for child in sorted(parent.iterdir()):
                if child.name.lower() == target_md and child.is_file():
                    matched_rel = str(child.relative_to(self.root))
                    return self._resolve(matched_rel), matched_rel
        if for_write:
            md_rel = f"{rel}.md"
            return self._resolve(md_rel), md_rel
        return p, rel

    def append(self, rel: str, text: str) -> str:
        """Append a dated timestamped line to a note, creating it if absent."""
        if not text.strip():
            raise VaultAccessError("Empty text")
        p, display_rel = self._resolve_note(rel, for_write=True)
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        line = f"- {stamp} {text.strip()}"
        with self._append_lock:
            try:
                if p.exists() and not p.is_file():
                    raise VaultAccessError(f"Not a regular file: {display_rel}")
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
        return f"Appended to {display_rel}"

    def read(self, rel: str, max_chars: int = 1500, tail: bool = False) -> str:
        """Read up to max_chars of a note (from head or tail), truncating with ellipsis."""
        max_chars = max(1, min(int(max_chars), 4000))
        p, display_rel = self._resolve_note(rel, for_write=False)
        if not p.is_file():
            raise VaultAccessError(f"Note not found: {display_rel}")
        try:
            fd = self._open_nofollow(p, os.O_RDONLY)
            with os.fdopen(fd, "rb") as f:
                text = f.read().decode("utf-8", errors="replace")
        except OSError as e:
            raise VaultAccessError(str(e)) from e
        if len(text) <= max_chars:
            return text
        if tail:
            return "..." + text[-max_chars:]
        return text[:max_chars] + "..."

    def _first_excerpt(self, path: Path, terms: list[str] | None = None) -> str:
        """Read a short representative line from a note for search previews."""
        try:
            fd = self._open_nofollow(path, os.O_RDONLY)
            with os.fdopen(fd, "rb") as f:
                text = f.read(32768).decode("utf-8", errors="replace")
        except (VaultAccessError, OSError):
            return ""
        first_nonempty = ""
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line == "---":
                continue
            if not first_nonempty:
                first_nonempty = line
            if terms and any(t in line.lower() for t in terms):
                return line[:200]
        return first_nonempty[:200]

    def _filename_matches(self, terms: list[str], limit: int) -> list[tuple[str, str]]:
        """Find non-hidden markdown notes whose relative path contains all terms."""
        hits: list[tuple[str, str]] = []
        if not terms:
            return hits
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for fname in sorted(filenames):
                if fname.startswith(".") or not fname.lower().endswith(".md"):
                    continue
                full = Path(dirpath) / fname
                try:
                    rel = str(full.relative_to(self.root))
                except ValueError:
                    continue
                rel_low = rel.lower()
                if all(t in rel_low for t in terms):
                    excerpt = self._first_excerpt(full, terms)
                    hits.append((rel, excerpt or "(filename match)"))
                    if len(hits) >= limit:
                        return hits
        return hits

    def _rg_fixed_matches(self, query: str, limit: int) -> list[tuple[str, str]]:
        """Run fixed-string ripgrep and return (rel_path, excerpt) pairs."""
        proc = subprocess.run(
            [
                "rg",
                "--json",
                "-i",
                "-F",
                "-I",
                "--max-count",
                "1",
                "--",
                query,
                str(self.root),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode == 2:
            raise VaultAccessError("Search error (invalid query)")
        out: list[tuple[str, str]] = []
        for raw in proc.stdout.splitlines():
            if len(out) >= limit:
                break
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("type") != "match":
                continue
            data = msg.get("data") or {}
            path_text = (data.get("path") or {}).get("text")
            if not path_text:
                continue
            try:
                rel = str(Path(path_text).relative_to(self.root))
            except ValueError:
                continue
            line_text = ((data.get("lines") or {}).get("text") or "").strip()
            out.append((rel, line_text[:200]))
        return out

    def _rg_multi_term_matches(self, terms: list[str], limit: int) -> list[tuple[str, str]]:
        """Find notes containing all terms (across path + body), ranked by filename hit."""
        anchor = max(terms, key=len)
        proc = subprocess.run(
            ["rg", "-i", "-F", "-I", "-l", "--", anchor, str(self.root)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode == 2:
            return []
        hits: list[tuple[str, str]] = []
        for path_text in proc.stdout.splitlines():
            if len(hits) >= limit:
                break
            full = Path(path_text)
            try:
                rel = str(full.relative_to(self.root))
            except ValueError:
                continue
            try:
                fd = self._open_nofollow(full, os.O_RDONLY)
                with os.fdopen(fd, "rb") as f:
                    text = f.read(131072).decode("utf-8", errors="replace")
            except (VaultAccessError, OSError):
                continue
            haystack = f"{rel}\n{text}".lower()
            if not all(t in haystack for t in terms):
                continue
            excerpt = ""
            for raw_line in text.splitlines():
                line = raw_line.strip()
                if line and any(t in line.lower() for t in terms):
                    excerpt = line[:200]
                    break
            hits.append((rel, excerpt or "(match)"))
        return hits

    def search(self, query: str, max_results: int = 5) -> str:
        """Search vault notes by filename + fixed-string + multi-term content match."""
        q = query.strip()
        if not q:
            return "No matches."
        max_results = max(1, min(int(max_results), 10))
        terms = [t for t in re.split(r"\s+", q.lower()) if t]

        combined: list[tuple[str, str]] = []
        seen: set[str] = set()

        def _add(items: list[tuple[str, str]]) -> None:
            for rel, excerpt in items:
                if rel not in seen and len(combined) < max_results:
                    seen.add(rel)
                    combined.append((rel, excerpt))

        # 1. Filename/path matches rank first (e.g. Mead/Cyser.md beats random mentions)
        _add(self._filename_matches(terms, max_results))

        # 2. Exact fixed-string ripgrep matches (preserves literal punctuation like "a|b")
        try:
            if len(combined) < max_results:
                _add(self._rg_fixed_matches(q, max_results * 2))
            # 3. Multi-term AND search across path + file body when exact phrase wasn't enough
            if len(terms) > 1 and len(combined) < max_results:
                _add(self._rg_multi_term_matches(terms, max_results * 2))
        except FileNotFoundError:
            if not combined:
                return "Search unavailable (ripgrep not installed)"
        except subprocess.TimeoutExpired:
            if not combined:
                return "Search timed out"
        except VaultAccessError as e:
            if not combined:
                return str(e)

        if not combined:
            return "No matches."
        return "\n".join(f"{rel}: {excerpt}" for rel, excerpt in combined)
