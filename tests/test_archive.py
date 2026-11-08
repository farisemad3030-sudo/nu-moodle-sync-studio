"""
Test Suite: ZIP Archive Extraction, Junk Purging, and Unwrapping
Covers Feature:
- F13: ZIP Archive Extraction & Cleaning (Tier 1 & Tier 2)

Fully recoupled to production moodle_sync.archive:
- ArchiveHandler
- extract_and_organize
- is_zip_archive
- ArchiveExtractionResult
"""

import io
import os
import zipfile
import tempfile
import unittest
from pathlib import Path

from tests.fixtures import (
    create_clean_zip,
    create_macosx_zip,
    create_single_root_zip,
    create_corrupt_zip,
)
from moodle_sync.archive import (
    ArchiveHandler,
    extract_and_organize,
    is_zip_archive,
    ArchiveExtractionResult,
)
from moodle_sync.deduplication import LocalDirectoryScanner, DeduplicationEngine, MatchTier


class ReferenceArchiveExtractor:
    """Compatibility bridge for external tests delegating to production ArchiveHandler."""
    @staticmethod
    def extract_and_clean(zip_path: Path, dest_dir: Path):
        res = extract_and_organize(zip_path, dest_dir)
        return list(res)


class TestF13ArchiveExtraction(unittest.TestCase):
    """Tests for Feature F13: ZIP Archive Extraction & Cleaning (Tier 1 & Tier 2)."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.work_dir = Path(self.temp_dir.name)
        self.zip_dir = self.work_dir / "zips"
        self.zip_dir.mkdir()
        self.out_dir = self.work_dir / "output"
        self.out_dir.mkdir()

    def tearDown(self):
        self.temp_dir.cleanup()

    # ------------------ Tier 1 Tests ------------------

    def test_f13_t1_01_magic_bytes_zip_validation(self):
        """F13.T1.1: Verify archive magic bytes check validates PK\\x03\\x04 header."""
        valid_zip = self.zip_dir / "valid.zip"
        create_clean_zip(valid_zip, {"doc.txt": b"Hello World"})
        self.assertTrue(is_zip_archive(valid_zip))

        invalid_file = self.zip_dir / "invalid.pdf"
        invalid_file.write_bytes(b"%PDF-1.4 Not a zip")
        self.assertFalse(is_zip_archive(invalid_file))

    def test_f13_t1_02_staging_in_tempdir(self):
        """F13.T1.2: Verify extraction utilizes a temporary directory without polluting target parent."""
        clean_zip = self.zip_dir / "clean.zip"
        create_clean_zip(clean_zip, {"note.txt": b"Important note"})
        res = extract_and_organize(clean_zip, self.out_dir)
        self.assertTrue(res.success)
        # Exercise duck-typing interface contract as List[Path] per PROJECT.md line 79
        file_list = list(res)
        self.assertEqual(len(file_list), 1)
        self.assertTrue((self.out_dir / "note.txt").exists())

    def test_f13_t1_03_purge_macosx_directory_and_appledouble(self):
        """F13.T1.3: Verify __MACOSX directory and AppleDouble ._ files are purged during extraction."""
        mac_zip = self.zip_dir / "mac.zip"
        valid_files = {"lecture.pdf": b"%PDF-1.4 Data", "notes.docx": b"Word data"}
        create_macosx_zip(mac_zip, valid_files)

        res = extract_and_organize(mac_zip, self.out_dir)
        self.assertTrue(res.success)
        extracted_names = [f.name for f in res]  # Duck-typing __iter__

        # Valid files must be extracted
        self.assertIn("lecture.pdf", extracted_names)
        self.assertIn("notes.docx", extracted_names)

        # Junk files must NOT be extracted
        for f in self.out_dir.glob("**/*"):
            self.assertFalse(f.name.startswith("._"))
            self.assertNotIn("__MACOSX", str(f))

    def test_f13_t1_04_purge_ds_store_and_thumbs(self):
        """F13.T1.4: Verify .DS_Store and Thumbs.db are discarded during extraction."""
        mac_zip = self.zip_dir / "mac.zip"
        create_macosx_zip(mac_zip, {"sheet.pdf": b"%PDF-1.4 Sheet"})
        res = extract_and_organize(mac_zip, self.out_dir)
        self.assertTrue(res.success)

        self.assertFalse((self.out_dir / ".DS_Store").exists())
        self.assertFalse((self.out_dir / "Thumbs.db").exists())
        self.assertTrue((self.out_dir / "sheet.pdf").exists())

    def test_f13_t1_05_unwrap_single_root_folder(self):
        """F13.T1.5: Verify single root folder wrapping (like Intro-Organization.zip) is unwrapped."""
        wrapped_zip = self.zip_dir / "wrapped.zip"
        create_single_root_zip(
            wrapped_zip,
            "Intro-Organization",
            {
                "lec-1 Computer Organization.pdf": b"%PDF-1.4 Lec1",
                "Org-Arch-Design.docx": b"Docx Content",
            },
        )
        res = extract_and_organize(wrapped_zip, self.out_dir)
        self.assertTrue(res.success)
        # Verify files are placed directly inside out_dir, NOT nested in Intro-Organization/
        self.assertTrue((self.out_dir / "lec-1 Computer Organization.pdf").exists())
        self.assertTrue((self.out_dir / "Org-Arch-Design.docx").exists())
        self.assertFalse((self.out_dir / "Intro-Organization").exists())

    # ------------------ Tier 2 Tests (Boundaries & Corners) ------------------

    def test_f13_t2_01_corrupted_zip_archive_handling(self):
        """F13.T2.1: Verify corrupted/truncated ZIP archive returns error result and does not corrupt disk."""
        corrupt_zip = self.zip_dir / "bad.zip"
        create_corrupt_zip(corrupt_zip)
        res = extract_and_organize(corrupt_zip, self.out_dir)
        self.assertFalse(res.success)
        # Ensure output directory has 0 files
        self.assertEqual(len(list(self.out_dir.iterdir())), 0)

    def test_f13_t2_02_zip_slip_path_traversal_prevention(self):
        """F13.T2.2: Verify zip slip attack with ../ traversal throws security exception."""
        slip_zip = self.zip_dir / "slip.zip"
        with zipfile.ZipFile(slip_zip, "w") as z:
            z.writestr("../../evil.txt", b"evil exploit")

        with self.assertRaises(ValueError) as cm:
            extract_and_organize(slip_zip, self.out_dir)
        self.assertTrue("traversal" in str(cm.exception).lower() or "slip" in str(cm.exception).lower())

    def test_f13_t2_03_empty_zip_archive_handling(self):
        """F13.T2.3: Verify completely empty ZIP archive extracts 0 files cleanly without crashing."""
        empty_zip = self.zip_dir / "empty.zip"
        create_clean_zip(empty_zip, {})
        res = extract_and_organize(empty_zip, self.out_dir)
        self.assertTrue(res.success)
        self.assertEqual(list(res), [])

    def test_f13_t2_04_multi_folder_hierarchy_preservation(self):
        """F13.T2.4: Verify archives with multiple root folders preserve their subfolder hierarchy."""
        multi_zip = self.zip_dir / "multi.zip"
        files = {
            "Part1/chapter1.pdf": b"%PDF-1.4 Ch1",
            "Part2/chapter2.pdf": b"%PDF-1.4 Ch2",
        }
        create_clean_zip(multi_zip, files)
        res = extract_and_organize(multi_zip, self.out_dir)
        self.assertTrue(res.success)
        # Should preserve Part1 and Part2
        self.assertTrue((self.out_dir / "Part1" / "chapter1.pdf").exists())
        self.assertTrue((self.out_dir / "Part2" / "chapter2.pdf").exists())

    def test_f13_t2_05_extracted_file_dedup_check(self):
        """F13.T2.5: Verify extracted files are checked against existing local files before placement."""
        existing_file = self.out_dir / "lecture.pdf"
        existing_file.write_bytes(b"%PDF-1.4 EXISTING_VERSION")

        candidate_zip = self.zip_dir / "update.zip"
        create_clean_zip(candidate_zip, {"lecture.pdf": b"%PDF-1.4 EXISTING_VERSION"})

        # Check deduplicator evaluates existing file as exact match
        scanner = LocalDirectoryScanner(root_dir=self.work_dir)
        scanner.COURSE_MAP = {1: {"code": "ECE322", "dir": "output"}}
        scanner.scan()
        engine = DeduplicationEngine(scanner)
        res = extract_and_organize(candidate_zip, self.out_dir, course_code="ECE322", dedup_engine=engine)
        self.assertTrue(res.success)
        self.assertTrue(any("lecture.pdf" in s[0] for s in res.skipped_files))


if __name__ == "__main__":
    unittest.main()
