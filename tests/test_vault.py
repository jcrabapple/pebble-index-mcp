import pytest
from pathlib import Path

from pebble_index_mcp.vault import Vault, VaultAccessError


@pytest.fixture()
def vault(tmp_path):
    v = Vault(tmp_path / "vault")
    v.root.mkdir(parents=True)
    return v


class TestResolve:
    def test_normal_relative_path(self, vault):
        assert vault._resolve("notes/idea.md") == vault.root / "notes" / "idea.md"

    def test_absolute_path_rejected(self, vault):
        with pytest.raises(VaultAccessError):
            vault._resolve("/etc/passwd")

    def test_parent_traversal_rejected(self, vault):
        with pytest.raises(VaultAccessError):
            vault._resolve("../escape.md")

    def test_symlink_escape_rejected(self, vault):
        (vault.root / "notes").mkdir()
        outside = vault.root.parent / "outside.txt"
        outside.write_text("secret")
        (vault.root / "notes" / "link.md").symlink_to(outside)
        with pytest.raises(VaultAccessError):
            vault._resolve("notes/link.md")

    def test_symlink_inside_vault_ok(self, vault):
        (vault.root / "a.md").write_text("hello")
        (vault.root / "b.md").symlink_to(vault.root / "a.md")
        assert vault._resolve("b.md").is_file()


class TestAppend:
    def test_creates_file_and_dirs(self, vault):
        out = vault.append("Writing/ideas.md", "ring idea")
        p = vault.root / "Writing" / "ideas.md"
        assert p.is_file()
        assert out == "Appended to Writing/ideas.md"
        assert "ring idea" in p.read_text()

    def test_appends_to_existing(self, vault):
        p = vault.root / "notes.md"
        p.write_text("- 08:00 first\n")
        vault.append("notes.md", "second")
        lines = p.read_text().strip().splitlines()
        assert len(lines) == 2 and lines[1].endswith("second")

    def test_rejects_empty_text(self, vault):
        with pytest.raises(VaultAccessError):
            vault.append("n.md", "   ")

    def test_rejects_traversal(self, vault):
        with pytest.raises(VaultAccessError):
            vault.append("../evil.md", "x")

    def test_rejects_non_regular_file(self, vault):
        import os

        fifo = vault.root / "pipe"
        os.mkfifo(fifo)
        with pytest.raises(VaultAccessError):
            vault.append("pipe", "x")

    def test_appends_when_file_lacks_trailing_newline(self, vault):
        p = vault.root / "notes.md"
        p.write_text("- 08:00 first")  # no trailing newline
        vault.append("notes.md", "second")
        lines = p.read_text().splitlines()
        assert len(lines) == 2
        assert lines[0] == "- 08:00 first"
        assert lines[1].endswith("second")


class TestRead:
    def test_reads_note(self, vault):
        (vault.root / "n.md").write_text("hello world")
        assert vault.read("n.md") == "hello world"

    def test_missing_note(self, vault):
        with pytest.raises(VaultAccessError):
            vault.read("nope.md")

    def test_truncates(self, vault):
        (vault.root / "n.md").write_text("x" * 100)
        assert vault.read("n.md", max_chars=10) == "x" * 10 + "..."


class TestSearch:
    def test_finds_match(self, vault):
        (vault.root / "mead.md").write_text("racked the cyser today")
        out = vault.search("cyser")
        assert "mead.md" in out and "racked" in out

    def test_no_match(self, vault):
        assert vault.search("zzzznope") == "No matches."

    def test_excludes_hidden_dirs(self, vault):
        (vault.root / ".obsidian").mkdir()
        (vault.root / ".obsidian" / "cfg.json").write_text("cyser cfg")
        (vault.root / "real.md").write_text("no cyser here")
        out = vault.search("cyser")
        # NOTE: spec literally had `assert "real.md" not in out`, but the
        # setup writes "cyser" into real.md, so real.md is a legitimate
        # match and must appear. The test's stated purpose is to verify the
        # HIDDEN dir is excluded, so we assert the hidden dir/file is absent
        # instead (and the legit match is present). This is strictly stronger.
        assert ".obsidian" not in out
        assert "cfg.json" not in out
        assert "real.md" in out

    def test_query_is_fixed_string_not_regex(self, vault):
        (vault.root / "code.md").write_text("use a|b as a separator")
        out = vault.search("a|b")
        assert "code.md" in out
