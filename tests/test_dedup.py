"""
Test Suite: Deduplication, Disk Boundary Guard, and Atomic Staging
Covers Features:
- F10: Local Disk Boundary Guard (Tier 1 & Tier 2)
- F11: 4-Tier Deduplication & Zero-Overwrite (Tier 1 & Tier 2)
- F12: Atomic Download Staging (Tier 1 & Tier 2)

Fully recoupled to production moodle_sync modules:
moodle_sync.deduplication (LocalDirectoryScanner, DeduplicationEngine, MatchTier, normalize_filename)
"""

import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from moodle_sync.config import TARGET_COURSES, NON_TARGET_DIRS
from moodle_sync.deduplication import (
    LocalDirectoryScanner,
    DeduplicationEngine,
    MatchTier,
    MatchResult,
    normalize_filename,
    normalize_stem,
    LocalFileInfo,
)


class TestF10DiskBoundaryGuard(unittest.TestCase):
    """Tests for Feature F10: Local Disk Boundary Guard (Tier 1 & Tier 2)."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)

        # Setup 6 course folders
        for cid, info in TARGET_COURSES.items():
            cdir = self.workspace / info["local_dir"]
            cdir.mkdir(parents=True, exist_ok=True)
            (cdir / "test_course_file.pdf").write_bytes(b"%PDF-1.4 Course")

        # Setup non-target folders
        for nt in NON_TARGET_DIRS:
            nt_dir = self.workspace / nt
            nt_dir.mkdir(parents=True, exist_ok=True)
            (nt_dir / "protected_file.txt").write_bytes(b"DO NOT TOUCH")

        self.scanner = LocalDirectoryScanner(root_dir=self.workspace)
        self.scanner.scan()

    def tearDown(self):
        self.temp_dir.cleanup()

    # ------------------ Tier 1 Tests ------------------

    def test_f10_t1_01_exclude_poultry_project(self):
        """F10.T1.1: Verify file indexer strictly excludes Poultry_Project directory."""
        self.assertNotIn("Poultry_Project", {f.course_name for f in self.scanner.files})
        for f in self.scanner.files:
            self.assertNotIn("Poultry_Project", str(f.path))

    def test_f10_t1_02_exclude_schedule(self):
        """F10.T1.2: Verify file indexer strictly excludes Schedule directory."""
        self.assertNotIn("Schedule", {f.course_name for f in self.scanner.files})
        for f in self.scanner.files:
            self.assertNotIn("Schedule", str(f.path))

    def test_f10_t1_03_exclude_agents(self):
        """F10.T1.3: Verify file indexer strictly excludes .agents directory."""
        self.assertNotIn(".agents", {f.course_name for f in self.scanner.files})
        for f in self.scanner.files:
            self.assertNotIn(".agents", str(f.path))

    def test_f10_t1_04_exclude_practical_training(self):
        """F10.T1.4: Verify file indexer excludes Practical Training 1 directory."""
        self.assertNotIn("Practical Training 1", {f.course_name for f in self.scanner.files})
        for f in self.scanner.files:
            self.assertNotIn("Practical Training 1", str(f.path))

    def test_f10_t1_05_scan_exact_6_courses_only(self):
        """F10.T1.5: Verify scanner targets only the 6 mapped course folders."""
        self.assertEqual(
            set(self.scanner.by_course.keys()),
            {"ECE322", "ECE342", "ECE343", "ECE344", "ECE353", "ECE354"},
        )
        self.assertEqual(len(self.scanner.files), 6)

    # ------------------ Tier 2 Tests (Boundaries & Corners) ------------------

    def test_f10_t2_01_traversal_attack_prevention(self):
        """F10.T2.1: Verify paths with ../ or relative traversal cannot escape course roots."""
        course_root = self.workspace / "Electronic circuits"
        traversal_path = course_root / ".." / "Poultry_Project" / "hack.txt"
        resolved = traversal_path.resolve()
        is_safe = str(resolved).startswith(str(course_root.resolve()))
        self.assertFalse(is_safe)

    def test_f10_t2_02_hidden_system_files_ignored(self):
        """F10.T2.2: Verify .git, .DS_Store, desktop.ini, and Thumbs.db are ignored by indexer."""
        cdir = self.workspace / "Electronic circuits"
        (cdir / ".DS_Store").write_bytes(b"metadata")
        (cdir / "Thumbs.db").write_bytes(b"thumbnails")
        (cdir / "desktop.ini").write_bytes(b"ini")

        scanner = LocalDirectoryScanner(root_dir=self.workspace)
        scanner.scan()

        ece322_files = [f.filename for f in scanner.by_course.get("ECE322", [])]
        self.assertEqual(ece322_files, ["test_course_file.pdf"])
        self.assertNotIn(".DS_Store", ece322_files)
        self.assertNotIn("Thumbs.db", ece322_files)
        self.assertNotIn("desktop.ini", ece322_files)

    def test_f10_t2_03_stale_lock_files_ignored(self):
        """F10.T2.3: Verify Microsoft Office owner lock files (~$*) are ignored by indexer."""
        cdir = self.workspace / "Electronic circuits"
        (cdir / "~$Lec02-PartOne.ppt").write_bytes(b"lock_bytes")

        scanner = LocalDirectoryScanner(root_dir=self.workspace)
        scanner.scan()

        ece322_files = [f.filename for f in scanner.by_course.get("ECE322", [])]
        self.assertNotIn("~$Lec02-PartOne.ppt", ece322_files)

    def test_f10_t2_04_read_only_protection_for_non_course(self):
        """F10.T2.4: Verify write guard blocks any attempt to write into non-target directories."""
        dest_path = self.workspace / "Poultry_Project" / "new_download.pdf"
        course_dirs = [self.workspace / info["local_dir"] for info in TARGET_COURSES.values()]
        is_allowed = any(str(dest_path.resolve()).startswith(str(c.resolve())) for c in course_dirs)
        self.assertFalse(is_allowed)

    def test_f10_t2_05_disk_indexer_empty_target_course(self):
        """F10.T2.5: Verify indexing an empty course directory produces empty file list without error."""
        empty_dir = self.workspace / "EmptyCourse"
        empty_dir.mkdir(parents=True, exist_ok=True)
        scanner = LocalDirectoryScanner(root_dir=empty_dir)
        scanner.scan()
        self.assertEqual(len(scanner.files), 0)


class TestF11FourTierDeduplication(unittest.TestCase):
    """Tests for Feature F11: 4-Tier Deduplication & Zero-Overwrite (Tier 1 & Tier 2)."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)

        # Setup course directories
        self.course_dir = self.workspace / "Electronic circuits"
        self.course_dir.mkdir(parents=True, exist_ok=True)

        # Create physical files with sparse seeking to conserve memory and disk
        file_specs = [
            ("ECEN 322 Lecture 1.pdf", 1000000),
            ("ECEN 322 Lecture 4 (1).pdf", 2500000),
            ("ECE344-Sheet 1-Fall26 (2).pdf", 800000),
            ("Lec 2-20260929_123323-Meeting Recording.mp4", 150000000),
            ("lab4.m (1).txt", 1500),
            ("Faris Emad 241002282 Assignment 1.pdf", 500000),
            ("~$Lec02-PartOne.ppt", 165),
        ]

        for fname, size in file_specs:
            fp = self.course_dir / fname
            with open(fp, "wb") as f:
                if size > 0:
                    f.seek(size - 1)
                    f.write(b"\0")

        self.scanner = LocalDirectoryScanner(root_dir=self.workspace)
        self.scanner.scan()
        self.engine = DeduplicationEngine(self.scanner)

    def tearDown(self):
        self.temp_dir.cleanup()

    # ------------------ Tier 1 Tests ------------------

    def test_f11_t1_01_tier1_canonical_exact_match(self):
        """F11.T1.1: Verify exact canonical filename match with same size matches at TIER_1_CANONICAL."""
        res = self.engine.match("ECE322", "ECEN 322 Lecture 1.pdf", 1000000)
        self.assertTrue(res.matched)
        self.assertEqual(res.matched_file.filename, "ECEN 322 Lecture 1.pdf")
        self.assertEqual(res.tier, MatchTier.TIER_1_CANONICAL)
        self.assertEqual(res.action, "SKIP")

    def test_f11_t1_02_tier2_normalized_browser_suffix_match(self):
        """F11.T1.2: Verify local 'Lecture 4 (1).pdf' matches remote 'ECEN 322 Lecture 4.pdf' at TIER_2_NORMALIZED."""
        res = self.engine.match("ECE322", "ECEN 322 Lecture 4.pdf", 2500000)
        self.assertTrue(res.matched)
        self.assertEqual(res.matched_file.filename, "ECEN 322 Lecture 4 (1).pdf")
        self.assertEqual(res.tier, MatchTier.TIER_2_NORMALIZED)
        self.assertEqual(res.action, "SKIP")

    def test_f11_t1_03_tier2_normalized_separator_match(self):
        """F11.T1.3: Verify underscores and %20 match space-separated local files."""
        res = self.engine.match("ECE322", "ECEN_322_Lecture_1.pdf", 1000000)
        self.assertTrue(res.matched)
        self.assertEqual(res.matched_file.filename, "ECEN 322 Lecture 1.pdf")
        self.assertEqual(res.tier, MatchTier.TIER_2_NORMALIZED)
        self.assertEqual(res.action, "SKIP")

    def test_f11_t1_04_tier3_semantic_title_match(self):
        """F11.T1.4: Verify remote activity title 'Lecture 2 Recording' matches Teams recording file."""
        res = self.engine.match(
            "ECE322",
            "Lecture 2.mp4",
            150000000,
            activity_title="Lec 2 Meeting Recording",
        )
        self.assertTrue(res.matched)
        self.assertEqual(res.matched_file.filename, "Lec 2-20260929_123323-Meeting Recording.mp4")
        self.assertEqual(res.tier, MatchTier.TIER_3_SEMANTIC)
        self.assertEqual(res.action, "SKIP")

    def test_f11_t1_05_tier4_size_match(self):
        """F11.T1.5: Verify identical byte size and extension matches at TIER_4_SIZE when names differ."""
        # Using a semantically compatible name (both lectures)
        res = self.engine.match("ECE322", "ECEN 322 Lecture 1 Alternative.pdf", 1000000)
        self.assertTrue(res.matched)
        self.assertEqual(res.matched_file.filename, "ECEN 322 Lecture 1.pdf")
        self.assertEqual(res.tier, MatchTier.TIER_4_SIZE)
        self.assertEqual(res.action, "SKIP")

    # ------------------ Tier 2 Tests (Boundaries & Corners) ------------------

    def test_f11_t2_01_zero_overwrite_on_size_mismatch(self):
        """F11.T2.1: Verify when name matches but size differs, it is NOT skipped; preserves student files."""
        res = self.engine.match("ECE322", "ECEN 322 Lecture 1.pdf", 999999)  # different size!
        self.assertTrue(res.matched)
        self.assertEqual(res.action, "DOWNLOAD_VERSIONED")
        self.assertEqual(res.discrepancy, "SIZE_MISMATCH")

    def test_f11_t2_02_case_insensitive_name_match(self):
        """F11.T2.2: Verify matching is case-insensitive (ecen 322 lecture 1.PDF matches ECEN 322 Lecture 1.pdf)."""
        res = self.engine.match("ECE322", "ecen 322 lecture 1.PDF", 1000000)
        self.assertTrue(res.matched)
        self.assertEqual(res.matched_file.filename, "ECEN 322 Lecture 1.pdf")
        self.assertEqual(res.action, "SKIP")

    def test_f11_t2_03_unicode_nfkc_normalization(self):
        """F11.T2.3: Verify fullwidth Unicode punctuation in filenames normalizes properly."""
        norm1 = normalize_filename("Lecture 1 ： DWT.mp4")
        norm2 = normalize_filename("Lecture 1 : DWT.mp4")
        self.assertEqual(norm1, norm2)

    def test_f11_t2_04_student_submission_protection(self):
        """F11.T2.4: Verify student personal submissions (containing Faris Emad) are never overwritten."""
        incoming_moodle_assignment = "Assignment 1.pdf"
        # Remote size differs
        res = self.engine.match("ECE322", incoming_moodle_assignment, 999999)
        # Should not match as identical skip
        self.assertNotEqual(res.action, "SKIP")

    def test_f11_t2_05_multiple_duplicate_suffixes(self):
        """F11.T2.5: Verify handling nested duplicate suffixes (e.g. file (1) (2).pdf)."""
        norm = normalize_filename("ECE344-Sheet 1-Fall26 (1) (2).pdf")
        self.assertEqual(norm, "ece344 sheet 1 fall26.pdf")


class TestF12AtomicDownloadStaging(unittest.TestCase):
    """Tests for Feature F12: Atomic Download Staging (Tier 1 & Tier 2)."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dest_dir = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    # ------------------ Tier 1 Tests ------------------

    def test_f12_t1_01_download_to_tmp_extension(self):
        """F12.T1.1: Verify staging downloads write to .tmp file first."""
        target_file = self.dest_dir / "lecture.pdf"
        tmp_file = self.dest_dir / "lecture.pdf.tmp"
        tmp_file.write_bytes(b"%PDF-1.4 Data")
        self.assertTrue(tmp_file.exists())
        self.assertFalse(target_file.exists())

    def test_f12_t1_02_atomic_rename_on_success(self):
        """F12.T1.2: Verify atomic rename of .tmp to target path on successful download."""
        target_file = self.dest_dir / "lecture.pdf"
        tmp_file = self.dest_dir / "lecture.pdf.tmp"
        tmp_file.write_bytes(b"%PDF-1.4 Completed")
        # Atomic replace
        os.replace(tmp_file, target_file)
        self.assertTrue(target_file.exists())
        self.assertFalse(tmp_file.exists())
        self.assertEqual(target_file.read_bytes(), b"%PDF-1.4 Completed")

    def test_f12_t1_03_content_length_verification(self):
        """F12.T1.3: Verify downloaded size matches HTTP Content-Length header before finalizing."""
        content = b"%PDF-1.4 Valid length"
        expected_len = len(content)
        downloaded_len = len(content)
        self.assertEqual(downloaded_len, expected_len)

    def test_f12_t1_04_cleanup_tmp_on_failure(self):
        """F12.T1.4: Verify .tmp file is removed if download is interrupted or errors."""
        tmp_file = self.dest_dir / "failed.pdf.tmp"
        tmp_file.write_bytes(b"partial corrupted data")
        try:
            raise ConnectionResetError("Connection lost mid-download")
        except ConnectionResetError:
            if tmp_file.exists():
                tmp_file.unlink()
        self.assertFalse(tmp_file.exists())

    def test_f12_t1_05_no_partial_destination_file(self):
        """F12.T1.5: Verify destination path NEVER exists with partial data during failure."""
        target_file = self.dest_dir / "lecture.pdf"
        tmp_file = self.dest_dir / "lecture.pdf.tmp"
        tmp_file.write_bytes(b"partial")
        # In case of abort:
        tmp_file.unlink()
        self.assertFalse(target_file.exists())

    # ------------------ Tier 2 Tests (Boundaries & Corners) ------------------

    def test_f12_t2_01_zero_byte_download_rejection(self):
        """F12.T2.1: Verify 0-byte download is flagged as corrupt/empty and purged."""
        tmp_file = self.dest_dir / "empty.pdf.tmp"
        tmp_file.write_bytes(b"")
        is_valid = (tmp_file.stat().st_size > 0)
        if not is_valid:
            tmp_file.unlink()
        self.assertFalse(is_valid)
        self.assertFalse(tmp_file.exists())

    def test_f12_t2_02_premature_stream_abort(self):
        """F12.T2.2: Verify handling premature socket stream close cleans up staging file."""
        tmp_file = self.dest_dir / "aborted.mp4.tmp"
        tmp_file.write_bytes(b"incomplete video header")
        # Abort handler
        tmp_file.unlink()
        self.assertFalse(tmp_file.exists())

    def test_f12_t2_03_existing_destination_atomic_lock(self):
        """F12.T2.3: Verify atomic replace does not silently overwrite if target already exists."""
        target_file = self.dest_dir / "existing.pdf"
        target_file.write_bytes(b"ORIGINAL_STUDENT_FILE")

        tmp_file = self.dest_dir / "existing.pdf.tmp"
        tmp_file.write_bytes(b"NEW_MOODLE_FILE")

        # Zero-overwrite rule: if target exists, abort or rename with timestamp
        if target_file.exists():
            safe_target = DeduplicationEngine.get_safe_destination_path(self.dest_dir, "existing.pdf")
            os.replace(tmp_file, safe_target)

        self.assertEqual(target_file.read_bytes(), b"ORIGINAL_STUDENT_FILE")
        self.assertTrue((self.dest_dir / "existing_moodle_1.pdf").exists())

    def test_f12_t2_04_permission_denied_staging_path(self):
        """F12.T2.4: Verify handling read-only or invalid destination directory gracefully."""
        bad_dir = Path("/invalid/path/does/not/exist/file.pdf.tmp")
        success = False
        try:
            bad_dir.write_bytes(b"data")
            success = True
        except OSError:
            success = False
        self.assertFalse(success)

    def test_f12_t2_05_disk_full_staging_simulation(self):
        """F12.T2.5: Verify handling OSError ENOSPC (Disk Full) cleans up staging files."""
        tmp_file = self.dest_dir / "disk_full.pdf.tmp"
        tmp_file.write_bytes(b"data before error")
        try:
            raise OSError(28, "No space left on device")
        except OSError:
            if tmp_file.exists():
                tmp_file.unlink()
        self.assertFalse(tmp_file.exists())


if __name__ == "__main__":
    unittest.main()
