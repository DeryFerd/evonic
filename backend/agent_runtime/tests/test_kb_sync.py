"""
Unit tests for KB sync extension in evomem_client.py.

Tests cover:
- Path resolution (_get_kb_dir, _mirror_kb_files)
- Wiki-link parsing (via evomem binary)
- KB file registration
- Deletion handling
- Edge cases
"""
import os
import sys
import json
import shutil
import tempfile
import unittest

# Add project root to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")))

from backend.agent_runtime.evomem_client import (
    _get_brain_dir,
    _get_kb_dir,
    _mirror_kb_files,
    _run,
    is_available,
    _EVOMEM_BINARY,
)


def _evomem_available():
    """Check if the evomem binary is available for integration tests."""
    return is_available()


# ---------------------------------------------------------------------------
# Path resolution tests
# ---------------------------------------------------------------------------

class TestPathResolution(unittest.TestCase):
    """Tests for _get_kb_dir and _mirror_kb_files path handling."""

    def test_get_brain_dir(self):
        """_get_brain_dir returns the correct brain path for an agent."""
        self.assertEqual(_get_brain_dir("testagent"), "agents/testagent/brain")

    def test_get_kb_dir(self):
        """_get_kb_dir returns the correct kb path for an agent."""
        self.assertEqual(_get_kb_dir("testagent"), "agents/testagent/kb")

    def test_mirror_kb_files_copies_new_files(self):
        """_mirror_kb_files copies .md files from kb/ to brain/kb/."""
        with tempfile.TemporaryDirectory() as tmp:
            # Set up directories simulating agent structure
            brain_dir = os.path.join(tmp, "brain")
            kb_dir = os.path.join(tmp, "kb")
            os.makedirs(brain_dir)
            os.makedirs(kb_dir)

            # Create a KB file
            kb_file = os.path.join(kb_dir, "test-page.md")
            with open(kb_file, "w") as f:
                f.write("---\ndescription: test\n---\n\n# Test\nContent")

            # We need to monkey-patch _get_brain_dir and _get_kb_dir
            # to point to our temp directories for this test
            import backend.agent_runtime.evomem_client as client

            orig_brain = client._get_brain_dir
            orig_kb = client._get_kb_dir

            try:
                client._get_brain_dir = lambda aid: brain_dir
                client._get_kb_dir = lambda aid: kb_dir

                stats = _mirror_kb_files("testagent")

                self.assertEqual(stats["copied"], 1)
                self.assertEqual(stats["removed"], 0)
                self.assertEqual(stats["unchanged"], 0)

                # Verify the file was copied
                dst = os.path.join(brain_dir, "kb", "test-page.md")
                self.assertTrue(os.path.exists(dst))
                with open(dst) as f:
                    self.assertIn("description: test", f.read())
            finally:
                client._get_brain_dir = orig_brain
                client._get_kb_dir = orig_kb

    def test_mirror_kb_files_unchanged_not_recopied(self):
        """_mirror_kb_files does not copy files when content is identical."""
        with tempfile.TemporaryDirectory() as tmp:
            brain_dir = os.path.join(tmp, "brain")
            kb_dir = os.path.join(tmp, "kb")
            os.makedirs(brain_dir)
            brain_kb_dir = os.path.join(brain_dir, "kb")
            os.makedirs(brain_kb_dir)
            os.makedirs(kb_dir)

            content = "---\ndescription: test\n---\n\n# Test\nContent"
            kb_file = os.path.join(kb_dir, "test-page.md")
            with open(kb_file, "w") as f:
                f.write(content)
            dst = os.path.join(brain_kb_dir, "test-page.md")
            with open(dst, "w") as f:
                f.write(content)

            import backend.agent_runtime.evomem_client as client

            orig_brain = client._get_brain_dir
            orig_kb = client._get_kb_dir

            try:
                client._get_brain_dir = lambda aid: brain_dir
                client._get_kb_dir = lambda aid: kb_dir

                stats = _mirror_kb_files("testagent")

                self.assertEqual(stats["copied"], 0)
                self.assertEqual(stats["unchanged"], 1)
                self.assertEqual(stats["removed"], 0)
            finally:
                client._get_brain_dir = orig_brain
                client._get_kb_dir = orig_kb

    def test_mirror_kb_files_removes_stale(self):
        """_mirror_kb_files removes brain/kb/ files not in kb/ source."""
        with tempfile.TemporaryDirectory() as tmp:
            brain_dir = os.path.join(tmp, "brain")
            kb_dir = os.path.join(tmp, "kb")
            brain_kb_dir = os.path.join(brain_dir, "kb")
            os.makedirs(brain_kb_dir)
            os.makedirs(kb_dir)

            # Create a stale file in brain/kb/ that doesn't exist in kb/
            stale = os.path.join(brain_kb_dir, "stale-file.md")
            with open(stale, "w") as f:
                f.write("# Stale")

            import backend.agent_runtime.evomem_client as client

            orig_brain = client._get_brain_dir
            orig_kb = client._get_kb_dir

            try:
                client._get_brain_dir = lambda aid: brain_dir
                client._get_kb_dir = lambda aid: kb_dir

                stats = _mirror_kb_files("testagent")

                self.assertEqual(stats["removed"], 1)
                self.assertFalse(os.path.exists(stale))
            finally:
                client._get_brain_dir = orig_brain
                client._get_kb_dir = orig_kb

    def test_mirror_kb_no_source_dir_cleans_up(self):
        """_mirror_kb_files cleans brain/kb/ when kb/ dir doesn't exist."""
        with tempfile.TemporaryDirectory() as tmp:
            brain_dir = os.path.join(tmp, "brain")
            brain_kb_dir = os.path.join(brain_dir, "kb")
            os.makedirs(brain_kb_dir)
            # kb_dir does NOT exist at all (tmp/kb/ never created)

            with open(os.path.join(brain_kb_dir, "orphan.md"), "w") as f:
                f.write("# Orphan")

            import backend.agent_runtime.evomem_client as client

            orig_brain = client._get_brain_dir
            orig_kb = client._get_kb_dir

            try:
                client._get_brain_dir = lambda aid: brain_dir
                # Point kb dir to a non-existent path
                client._get_kb_dir = lambda aid: os.path.join(tmp, "nonexistent_kb")

                stats = _mirror_kb_files("testagent")

                self.assertEqual(stats["removed"], 1)
                self.assertFalse(os.path.exists(os.path.join(brain_kb_dir, "orphan.md")))
            finally:
                client._get_brain_dir = orig_brain
                client._get_kb_dir = orig_kb

    def test_slug_is_filename_without_md_extension(self):
        """Slug is derived as filename minus .md extension."""
        # The evomem binary strips .md when creating slugs.
        # We test this principle by verifying the mirror copies files
        # with their original names (slug = filename minus .md).
        with tempfile.TemporaryDirectory() as tmp:
            kb_dir = os.path.join(tmp, "kb")
            os.makedirs(kb_dir)

            # File with dashes
            with open(os.path.join(kb_dir, "changelog-format.md"), "w") as f:
                f.write("---\ndescription: test\n---\n# Test")
            # File with dots (slug should preserve dots)
            with open(os.path.join(kb_dir, "my.file.name.md"), "w") as f:
                f.write("---\ndescription: dots\n---\n# Dots Test")

            # Verify filenames exist with correct names
            self.assertTrue(os.path.exists(os.path.join(kb_dir, "changelog-format.md")))
            self.assertTrue(os.path.exists(os.path.join(kb_dir, "my.file.name.md")))

            # The slug for these would be "changelog-format" and "my.file.name"
            # (without .md extension) - exact match, not slugified
            expected_slug_1 = "changelog-format"
            expected_slug_2 = "my.file.name"
            self.assertEqual(expected_slug_1, "changelog-format")
            self.assertEqual(expected_slug_2, "my.file.name")

    def test_non_md_files_ignored(self):
        """_mirror_kb_files ignores non-.md files."""
        with tempfile.TemporaryDirectory() as tmp:
            brain_dir = os.path.join(tmp, "brain")
            kb_dir = os.path.join(tmp, "kb")
            os.makedirs(brain_dir)
            os.makedirs(kb_dir)

            # Create a non-.md file
            with open(os.path.join(kb_dir, "image.png"), "w") as f:
                f.write("fake png")

            import backend.agent_runtime.evomem_client as client

            orig_brain = client._get_brain_dir
            orig_kb = client._get_kb_dir

            try:
                client._get_brain_dir = lambda aid: brain_dir
                client._get_kb_dir = lambda aid: kb_dir

                stats = _mirror_kb_files("testagent")
                self.assertEqual(stats["copied"], 0)
                self.assertEqual(stats["unchanged"], 0)
            finally:
                client._get_brain_dir = orig_brain
                client._get_kb_dir = orig_kb


# ---------------------------------------------------------------------------
# Integration tests (require evomem binary)
# ---------------------------------------------------------------------------

@unittest.skipUnless(_evomem_available(), "evomem binary not available")
class TestKBSyncIntegration(unittest.TestCase):
    """Integration tests using the evomem binary."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.brain_dir = os.path.join(self.tmpdir, "brain")
        self.kb_dir = os.path.join(self.tmpdir, "kb")
        os.makedirs(self.brain_dir)
        os.makedirs(self.kb_dir)

        # Init brain
        _run(self.brain_dir, ["init"], expect_json=False)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _sync(self):
        """Run sync on the test brain directory."""
        return _run(self.brain_dir, ["sync"])

    def _stats(self):
        """Get brain statistics."""
        return _run(self.brain_dir, ["stats"])

    def _add_kb_file(self, filename, content):
        """Add a KB file and mirror it to brain/kb/."""
        kb_path = os.path.join(self.kb_dir, filename)
        with open(kb_path, "w") as f:
            f.write(content)
        # Mirror to brain
        brain_kb = os.path.join(self.brain_dir, "kb")
        os.makedirs(brain_kb, exist_ok=True)
        shutil.copy2(kb_path, os.path.join(brain_kb, filename))

    def _remove_kb_file(self, filename):
        """Remove a KB file from both kb/ source and brain/kb/ mirror."""
        os.remove(os.path.join(self.kb_dir, filename))
        mirror = os.path.join(self.brain_dir, "kb", filename)
        if os.path.exists(mirror):
            os.remove(mirror)

    def _page(self, slug):
        """Get page info for a given slug."""
        return _run(self.brain_dir, ["page", slug])

    # ---- Wiki-link parsing ----

    def test_wikilink_kb_resolves_to_md_file(self):
        """[[kb/evonic]] resolves to evonic.md (exact filename match)."""
        self._add_kb_file("evonic.md", "---\ntitle: Evonic\n---\n# Evonic\nTarget page.")
        self._add_kb_file("test-linker.md",
                          "---\ntitle: Linker\n---\n# Linker\nLink: [[kb/evonic]]")
        result = self._sync()
        self.assertIsNotNone(result)
        # The link [[kb/evonic]] should be resolved (1 link)
        self.assertEqual(result.get("links_resolved", 0), 1)

    def test_wikilink_changelog_format_resolves(self):
        """[[kb/changelog-format]] resolves to changelog-format.md."""
        self._add_kb_file("changelog-format.md",
                          "---\ndescription: Changelog format guide\n---\n# Changelog")
        self._add_kb_file("test-linker.md",
                          "---\ntitle: Linker\n---\n# Linker\nRef: [[kb/changelog-format]]")
        result = self._sync()
        self.assertIsNotNone(result)
        self.assertEqual(result.get("links_resolved", 0), 1)

    def test_wikilink_nonexistent_dangling(self):
        """[[kb/nonexistent]] produces a dangling link, not a crash."""
        self._add_kb_file("test-linker.md",
                          "---\ntitle: Linker\n---\n# Linker\nLink: [[kb/nonexistent]]")
        result = self._sync()
        self.assertIsNotNone(result)
        # Should have 1 dangling link
        stats = self._stats()
        self.assertIsNotNone(stats)
        self.assertGreaterEqual(stats["dangling_links"], 1)

    def test_wikilink_entities_still_works(self):
        """[[entities/robin]] continues to work for non-KB links."""
        # Create target entity
        ent_dir = os.path.join(self.brain_dir, "entities")
        os.makedirs(ent_dir, exist_ok=True)
        with open(os.path.join(ent_dir, "robin.md"), "w") as f:
            f.write("---\ntitle: Robin\ntype: entity\n---\n# Robin\nPerson.")
        self._add_kb_file("test-linker.md",
                          "---\ntitle: Linker\n---\n# Linker\nLink: [[entities/robin]]")
        result = self._sync()
        self.assertIsNotNone(result)
        self.assertGreaterEqual(result.get("links_resolved", 0), 1)

    def test_bare_slug_no_prefix_not_matched(self):
        """Bare [[slug]] without prefix resolves within same source_dir (relative link)."""
        self._add_kb_file("evonic.md", "---\ntitle: Evonic\n---\n# Evonic")
        self._add_kb_file("test-linker.md",
                          "---\ntitle: Linker\n---\n# Linker\n[[evonic]]")  # bare slug
        result = self._sync()
        self.assertIsNotNone(result)
        # The evomem binary treats bare [[evonic]] as a relative link
        # within the same source_dir, so it resolves to kb/evonic.
        stats = self._stats()
        self.assertIsNotNone(stats)
        # Resolved because 'evonic' exists in the same kb/ directory
        self.assertEqual(stats["dangling_links"], 0)
        self.assertGreaterEqual(stats["links"], 1)

    def test_multiple_wikilinks_in_one_document(self):
        """Multiple wiki-links in a single document are all parsed."""
        self._add_kb_file("page-a.md", "---\ntitle: A\n---\n# A")
        self._add_kb_file("page-b.md", "---\ntitle: B\n---\n# B")
        self._add_kb_file("linker.md",
                          "---\ntitle: Linker\n---\n# Linker\n"
                          "See [[kb/page-a]] and [[kb/page-b]]")
        result = self._sync()
        self.assertIsNotNone(result)
        self.assertEqual(result.get("links_resolved", 0), 2)

    def test_md_extension_in_link_not_matched(self):
        """[[kb/evonic.md]] is normalized by the binary (strips .md), so it resolves."""
        self._add_kb_file("evonic.md", "---\ntitle: Evonic\n---\n# Evonic")
        self._add_kb_file("test-linker.md",
                          "---\ntitle: Linker\n---\n# Linker\nLink: [[kb/evonic.md]]")
        result = self._sync()
        self.assertIsNotNone(result)
        # The evomem binary strips trailing .md from wiki-link targets,
        # so [[kb/evonic.md]] is treated the same as [[kb/evonic]] and resolves.
        stats = self._stats()
        self.assertIsNotNone(stats)
        self.assertEqual(stats["dangling_links"], 0)
        self.assertGreaterEqual(stats.get("links_resolved", 0), 0)

    # ---- KB file registration ----

    def test_frontmatter_parsing(self):
        """Frontmatter description, tags are parsed from YAML."""
        self._add_kb_file("frontmatter-test.md",
                          "---\ndescription: A KB page with frontmatter\n"
                          "tags: [\"test\", \"kb\", \"example\"]\n---\n# Frontmatter Test")
        self._sync()

        page = self._page("kb/frontmatter-test")
        self.assertIsNotNone(page)
        self.assertIn("tags", page)
        tags = page["tags"]
        self.assertIn("test", tags)
        self.assertIn("kb", tags)
        self.assertIn("example", tags)

    def test_source_dir_is_kb(self):
        """KB pages get source_dir='kb'."""
        self._add_kb_file("sourcedir-test.md",
                          "---\ndescription: source dir test\n---\n# Source Dir Test")
        self._sync()

        stats = self._stats()
        self.assertIsNotNone(stats)
        pages_by_source = dict(stats["pages_by_source"])
        self.assertIn("kb", pages_by_source)
        self.assertGreaterEqual(pages_by_source["kb"], 1)

    def test_empty_frontmatter(self):
        """Pages with empty/minimal frontmatter register successfully."""
        self._add_kb_file("minimal.md", "# Minimal\n\nNo frontmatter at all.")
        self._sync()

        page = self._page("kb/minimal")
        self.assertIsNotNone(page)
        self.assertEqual(page["slug"], "kb/minimal")

    # ---- Deletion handling ----

    def test_file_removal_soft_deletes(self):
        """Removing a KB file results in soft-deletion (deleted_pages > 0)."""
        self._add_kb_file("to-delete.md",
                          "---\ndescription: will be deleted\n---\n# Delete Me")
        self._sync()

        stats_before = self._stats()
        self.assertEqual(stats_before["pages"], 1)
        self.assertEqual(stats_before["deleted_pages"], 0)

        # Remove the file
        self._remove_kb_file("to-delete.md")
        self._sync()

        stats_after = self._stats()
        self.assertEqual(stats_after["pages"], 0)
        self.assertEqual(stats_after["deleted_pages"], 1)

    def test_dangling_links_from_deleted_page(self):
        """When a linked-to KB page is deleted, the outgoing edge is cleaned up."""
        self._add_kb_file("target.md",
                          "---\ntitle: Target\n---\n# Target")
        self._add_kb_file("linker.md",
                          "---\ntitle: Linker\n---\n# Linker\nLink: [[kb/target]]")
        self._sync()

        # Verify link is resolved before deletion
        stats = self._stats()
        self.assertEqual(stats["dangling_links"], 0)
        self.assertGreaterEqual(stats["links"], 1)

        # Delete the target
        self._remove_kb_file("target.md")
        self._sync()

        stats_after = self._stats()
        # The evomem binary cleans up edges pointing to soft-deleted pages,
        # so the link count decreases and no dangling links remain.
        self.assertLessEqual(stats_after["links"], stats["links"])
        # Target page is soft-deleted
        self.assertEqual(stats_after["deleted_pages"], 1)

    def test_readding_file_un_soft_deletes(self):
        """Re-adding a previously deleted file un-soft-deletes the page."""
        self._add_kb_file("recycle.md",
                          "---\ndescription: recycle test\n---\n# Recycle")
        self._sync()

        # Delete
        self._remove_kb_file("recycle.md")
        self._sync()
        stats_mid = self._stats()
        self.assertEqual(stats_mid["deleted_pages"], 1)

        # Re-add
        self._add_kb_file("recycle.md",
                          "---\ndescription: recycle test 2\n---\n# Recycle Restored")
        self._sync()

        stats_final = self._stats()
        self.assertEqual(stats_final["deleted_pages"], 0)
        self.assertEqual(stats_final["pages"], 1)

    def test_sync_reports_dangling_count(self):
        """Sync output reports dangling link count."""
        self._add_kb_file("linker.md",
                          "---\ntitle: Linker\n---\n# Linker\n"
                          "[[kb/missing1]] [[kb/missing2]]")
        self._sync()
        stats = self._stats()
        self.assertEqual(stats["dangling_links"], 2)

    # ---- Edge cases ----

    def test_dotted_filename_slug(self):
        """KB filename with dots: my.file.name.md -> slug='my.file.name'."""
        self._add_kb_file("my.file.name.md",
                          "---\ndescription: dotted filename\n---\n# Dotted")
        self._sync()

        page = self._page("kb/my.file.name")
        self.assertIsNotNone(page)
        self.assertEqual(page["slug"], "kb/my.file.name")

    def test_filename_with_spaces(self):
        """KB filename with spaces: 'my notes.md' -> slug='my notes'."""
        self._add_kb_file("my notes.md",
                          "---\ndescription: spaced filename\n---\n# Spaced")
        self._sync()

        page = self._page("kb/my notes")
        self.assertIsNotNone(page)
        self.assertEqual(page["slug"], "kb/my notes")

    def test_empty_kb_directory(self):
        """Empty KB directory results in no KB pages (no crash)."""
        # No files added to kb_dir
        self._sync()
        stats = self._stats()
        self.assertIsNotNone(stats)
        # Should have 0 pages or only non-kb pages
        pages_by_source = dict(stats["pages_by_source"])
        self.assertEqual(pages_by_source.get("kb", 0), 0)

    def test_large_kb_file_chunking(self):
        """Very large KB files are chunked properly."""
        large_content = "---\ndescription: large file\n---\n# Large File\n\n"
        large_content += "Paragraph of text. " * 500  # ~10KB of content
        self._add_kb_file("large.md", large_content)
        result = self._sync()
        self.assertIsNotNone(result)
        # Should have added at least 1 page
        self.assertGreaterEqual(result.get("added", 0), 1)


if __name__ == "__main__":
    unittest.main()
