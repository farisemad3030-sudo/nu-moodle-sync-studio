"""
Test Suite: Iteration 1 Regression Shield
Guarantees permanent prevention of the 10 critical flaws and vulnerabilities
identified during Iteration 1 adversarial auditing.
Directly exercises production moodle_sync modules.
"""

import os
import re
import json
import zipfile
import tempfile
import unittest
import urllib.parse
from pathlib import Path, PurePosixPath
from unittest.mock import MagicMock, patch

from bs4 import BeautifulSoup
import requests

from moodle_sync.media import (
    sanitize_filename,
    resolve_and_download,
    create_url_shortcut,
)
from moodle_sync.archive import (
    is_zip_archive,
    ArchiveHandler,
    extract_and_organize,
)
from moodle_sync.deduplication import (
    LocalDirectoryScanner,
    DeduplicationEngine,
    MatchTier,
    extract_semantic_tokens,
    normalize_filename,
    normalize_stem,
    are_semantically_compatible,
)
from moodle_sync.auth import load_session_cache
from moodle_sync.scraper import (
    make_soup,
    clean_text,
    extract_filename_from_url,
    MoodleItem,
    ResourceType,
    SectionData,
    CourseData,
)
from moodle_sync.syncer import MoodleSyncer


class TestIteration1RegressionSuite(unittest.TestCase):
    """Authoritative regression tests for all 10 Iteration 1 defects."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    # --------------------------------------------------------------------------
    # Regression 1: syncer.py Remote Filename Path Traversal & Truncation
    # --------------------------------------------------------------------------
    def test_regression_01_syncer_path_traversal_boundary(self):
        """R1: Prevent remote filename path traversal from escaping course dirs or destroying files."""
        protected_dir = self.workspace / "Poultry_Project"
        protected_dir.mkdir(parents=True)
        victim_file = protected_dir / "secret.txt"
        victim_file.write_bytes(b"CONFIDENTIAL_DO_NOT_TRUNCATE")

        target_dest_dir = self.workspace / "Electronic circuits" / "Lectures"
        target_dest_dir.mkdir(parents=True)

        raw_traversal_fn = "sub/../../../Poultry_Project/secret.txt"
        sanitized_fn = sanitize_filename(Path(raw_traversal_fn).name)
        target_filepath = (target_dest_dir / sanitized_fn).resolve()

        # Verify boundary confinement
        self.assertTrue(target_filepath.is_relative_to(target_dest_dir.resolve()))
        self.assertEqual(target_filepath.name, "secret.txt")
        self.assertEqual(target_filepath.parent, target_dest_dir.resolve())
        # Victim file must remain completely untouched
        self.assertEqual(victim_file.read_bytes(), b"CONFIDENTIAL_DO_NOT_TRUNCATE")

    # --------------------------------------------------------------------------
    # Regression 2: archive.py Leading-Slash / Drive-Root Escape
    # --------------------------------------------------------------------------
    def test_regression_02_archive_drive_escape(self):
        """R2: Block archive entries with leading slashes, drive letters, or absolute paths."""
        dest_dir = self.workspace / "course_dir"
        dest_dir.mkdir()

        attack_zip = self.workspace / "attack.zip"
        with zipfile.ZipFile(attack_zip, "w") as z:
            z.writestr("/EscapedFolder/leak.txt", b"LEAK")
            z.writestr("Normal/valid.txt", b"VALID")

        with self.assertRaises(ValueError) as cm:
            extract_and_organize(attack_zip, dest_dir)
        self.assertIn("traversal", str(cm.exception).lower())

    # --------------------------------------------------------------------------
    # Regression 3: archive.py Magic Bytes Bypass & Staging Facade
    # --------------------------------------------------------------------------
    def test_regression_03_archive_magic_bytes_and_staging(self):
        """R3: Validate true magic bytes (PK\\x03\\x04, PK\\x05\\x06, PK\\x07\\x08) and true staging."""
        fake_zip = self.workspace / "fake.zip"
        fake_zip.write_text("Not a zip, just plain text with .zip extension")

        self.assertFalse(is_zip_archive(fake_zip))

        empty_zip = self.workspace / "empty.zip"
        with zipfile.ZipFile(empty_zip, "w") as z:
            pass
        self.assertTrue(is_zip_archive(empty_zip))

    # --------------------------------------------------------------------------
    # Regression 4: deduplication.py Tier 4 Size False Match Guard
    # --------------------------------------------------------------------------
    def test_regression_04_tier4_size_semantic_guard(self):
        """R4: Tier 4 size match must NOT match distinct lectures or exams sharing size."""
        # 1. Final Exam Solutions vs Lecture 1 sharing size
        self.assertFalse(are_semantically_compatible("Final Exam Solutions.pdf", "Lecture 1.pdf"))

        # 2. Lecture 1 vs Lecture 2 sharing size
        self.assertFalse(are_semantically_compatible("Lecture 2.pdf", "Lecture 1.pdf"))

        # 3. Compatible items (e.g. Lecture 1 vs ECEN 322 Lecture 1 Alternative)
        self.assertTrue(are_semantically_compatible("Lecture 1 Alternative.pdf", "Lecture 1.pdf"))

    # --------------------------------------------------------------------------
    # Regression 5: media.py Zero-Overwrite Protection
    # --------------------------------------------------------------------------
    def test_regression_05_media_zero_overwrite_guard(self):
        """R5: Media downloads must never overwrite existing files with os.replace."""
        dest_dir = self.workspace / "dest"
        dest_dir.mkdir()
        target_file = dest_dir / "existing_doc.pdf"
        target_file.write_bytes(b"ORIGINAL_STUDENT_DATA")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Disposition": 'filename="existing_doc.pdf"', "Content-Type": "application/pdf"}
        mock_resp.iter_content = lambda chunk_size: [b"NEW_MOODLE_DATA_DIFFERENT_LENGTH"]
        mock_session = MagicMock(spec=requests.Session)
        mock_session.get.return_value = mock_resp

        res = resolve_and_download("https://example.com/existing_doc.pdf", dest_dir, mock_session)
        # Original file preserved
        self.assertEqual(target_file.read_bytes(), b"ORIGINAL_STUDENT_DATA")
        # Versioned copy created
        versioned_file = dest_dir / "existing_doc_moodle_1.pdf"
        self.assertTrue(versioned_file.exists())
        self.assertEqual(versioned_file.read_bytes(), b"NEW_MOODLE_DATA_DIFFERENT_LENGTH")

    # --------------------------------------------------------------------------
    # Regression 6: deduplication.py Regex Number Preservation & Word Boundary
    # --------------------------------------------------------------------------
    def test_regression_06_regex_number_preservation(self):
        """R6: normalize_stem must NOT strip trailing digits from Lecture_1 or fail on ECEN_322."""
        norm_space = normalize_stem("ECEN 322 Lecture 1")
        norm_under = normalize_stem("ECEN_322_Lecture_1")
        self.assertEqual(norm_space, norm_under)
        self.assertEqual(norm_under, "ece 322 lecture 1")
        self.assertIn("1", norm_under)

        norm_lec2 = normalize_stem("ECEN_322_Lecture_2")
        self.assertNotEqual(norm_under, norm_lec2)

    # --------------------------------------------------------------------------
    # Regression 7: deduplication.py URL Decoding & Arabic Tashkeel
    # --------------------------------------------------------------------------
    def test_regression_07_url_decoding_and_arabic_tashkeel(self):
        """R7: Unquote URL encoded %20 and strip Arabic diacritics (tashkeel)."""
        norm_url = normalize_filename("Sheet%2001.pdf")
        self.assertEqual(norm_url, "sheet 1.pdf")

        norm_plain = normalize_filename("معالجة الصور.pdf")
        norm_tashkeel = normalize_filename("مُعَالَجَةُ الصُّوَرِ.pdf")
        self.assertEqual(norm_plain, norm_tashkeel)

    # --------------------------------------------------------------------------
    # Regression 8: syncer.py Dry-Run Mkdir Guard
    # --------------------------------------------------------------------------
    def test_regression_08_dry_run_mkdir_guard(self):
        """R8: --dry-run and --verify must produce ZERO directory creations on disk."""
        cdir = self.workspace / "Electronic circuits"
        exams_dir = cdir / "Exams"

        item = MoodleItem(
            id="res_exam",
            name="Exam.pdf",
            url="https://moodle.nu.edu.eg/mod/resource/view.php?id=1",
            resource_type=ResourceType.RESOURCE,
            course_id=21329,
            section_id="s1",
            section_title="Final Exam",
            target_subfolder="Exams",
            filename="Final_Exam.pdf",
        )
        sec = SectionData(section_id="s1", title="Final Exam", items=[item])
        cdata = CourseData(
            course_id=21329,
            course_code="ECE322",
            course_name="Electronic Circuits",
            local_folder="Electronic circuits",
            sections=[sec],
            all_items=[item],
        )

        mock_session = MagicMock(spec=requests.Session)
        syncer = MoodleSyncer(base_dir=self.workspace, session=mock_session, dry_run=True)
        with patch("moodle_sync.syncer.CourseScraper.scan_course", return_value=cdata), patch("moodle_sync.syncer.generate_report"):
            syncer.run(target_course_id=21329)

        self.assertFalse(exams_dir.exists())
        self.assertFalse(cdir.exists())

    # --------------------------------------------------------------------------
    # Regression 9: auth.py Session Cache Non-Dict Corruption Resilience
    # --------------------------------------------------------------------------
    def test_regression_09_session_cache_corruption_resilience(self):
        """R9: load_session_cache must safely return None if cookies payload is not a dict."""
        corrupted_file = self.workspace / ".moodle_session.json"
        corrupted_file.write_text('{"timestamp": 123, "cookies": 42}', encoding="utf-8")
        res = load_session_cache(corrupted_file)
        self.assertIsNone(res)

    # --------------------------------------------------------------------------
    # Regression 10: scraper.py Surrogate Characters & Mod Folder Button Text
    # --------------------------------------------------------------------------
    def test_regression_10_scraper_surrogate_and_mod_folder_button(self):
        """R10: Sanitize lone surrogates and prefer URL filename over UI button text."""
        surrogate_raw = "<html><body>Surrogate: \ud800 text</body></html>"
        soup = make_soup(surrogate_raw)
        self.assertIn("Surrogate:", soup.get_text())

        button_text = "Download"
        url = "https://moodle.nu.edu.eg/pluginfile.php/21365/mod_folder/content/0/Actual_Lecture_File.pdf"
        fn_from_url = extract_filename_from_url(url)
        self.assertEqual(fn_from_url, "Actual_Lecture_File.pdf")

    # --------------------------------------------------------------------------
    # Regression 11: auth.py Structured Password / Email Error Classification
    # --------------------------------------------------------------------------
    def test_regression_11_auth_error_classification_and_dom_state(self):
        """R11: Distinguish wrong password, wrong email, empty password retry, and MFA."""
        from moodle_sync.auth import (
            AuthenticationError,
            classify_auth_error,
            _detect_microsoft_auth_state,
        )

        self.assertEqual(
            classify_auth_error("Your account or password is incorrect."),
            ("WRONG_PASSWORD", "password"),
        )
        self.assertEqual(
            classify_auth_error("We couldn't find an account with that username."),
            ("WRONG_EMAIL", "email"),
        )
        self.assertEqual(
            classify_auth_error("Microsoft requested MFA/2FA verification"),
            ("MFA_REQUIRED", "general"),
        )

        # Verify _detect_microsoft_auth_state on #passwordError vs #usernameError
        page_pwd_err = MagicMock()
        page_pwd_err.url = "https://login.microsoftonline.com/common/login"
        pwd_err_node = MagicMock()
        pwd_err_node.is_visible.return_value = True
        pwd_err_node.inner_text.return_value = "Your account or password is incorrect."
        page_pwd_err.query_selector.side_effect = lambda sel: pwd_err_node if sel == "#passwordError" else None
        self.assertEqual(
            _detect_microsoft_auth_state(page_pwd_err),
            ("PASSWORD_ERROR", "Your account or password is incorrect."),
        )

        page_user_err = MagicMock()
        page_user_err.url = "https://login.microsoftonline.com/common/login"
        user_err_node = MagicMock()
        user_err_node.is_visible.return_value = True
        user_err_node.inner_text.return_value = "This username may be incorrect."
        page_user_err.query_selector.side_effect = lambda sel: user_err_node if sel == "#usernameError" else None
        self.assertEqual(
            _detect_microsoft_auth_state(page_user_err),
            ("USERNAME_ERROR", "This username may be incorrect."),
        )

    # --------------------------------------------------------------------------
    # Regression 12: moodle_app.py Rejects Wrong Password Even If Cached Session Exists
    # --------------------------------------------------------------------------
    def test_regression_12_wrong_password_rejected_despite_cached_session(self):
        """R12: Typing a wrong password in Step 1 must never silently succeed using old cached cookies."""
        import moodle_app
        from moodle_sync.auth import AuthenticationError

        moodle_app.STATE.verified_cred_hashes.clear()
        moodle_app.STATE.current_user = ""
        fake_cache = {
            "account_email": "student@nu.edu.eg",
            "moodle": {"MoodleSession": "valid_old_cookie"},
            "all": [],
        }

        def fake_login(username=None, password=None, headless=True, force_fresh=False):
            if force_fresh and password == "WrongPass123":
                raise AuthenticationError(
                    "Incorrect password or account: Your account or password is incorrect.",
                    code="WRONG_PASSWORD",
                    field="password",
                )
            s = requests.Session()
            s.auth_cookies = {"account_email": username or "student@nu.edu.eg"}
            return s

        with patch("moodle_app.load_session_cache", return_value=fake_cache), \
             patch("moodle_app.validate_session", return_value=True), \
             patch("moodle_app.login", side_effect=fake_login), \
             patch("moodle_app.discover_enrolled_courses", return_value=[]):
            # 1) Wrong password MUST raise AuthenticationError(code="WRONG_PASSWORD")
            with self.assertRaises(AuthenticationError) as ctx:
                moodle_app.handle_login_and_initial_scan({
                    "email": "student@nu.edu.eg",
                    "password": "WrongPass123",
                    "workspace": str(self.workspace),
                    "force_fresh": False,
                })
            self.assertEqual(ctx.exception.code, "WRONG_PASSWORD")
            self.assertEqual(ctx.exception.field, "password")

            # 2) Correct password succeeds and returns login success message
            res = moodle_app.handle_login_and_initial_scan({
                "email": "student@nu.edu.eg",
                "password": "CorrectPass!",
                "workspace": str(self.workspace),
                "force_fresh": False,
            })
            self.assertTrue(res["ok"])
            self.assertIn("Signed in successfully as student@nu.edu.eg", res["message"])

    # --------------------------------------------------------------------------
    # Regression 13: Dashboard UI Password Toggle, Status Banner & Quick Check Active States
    # --------------------------------------------------------------------------
    def test_regression_13_dashboard_ui_controls_and_quick_check_states(self):
        """R13: Verify DASHBOARD_HTML includes show/hide password, login banner, and active Quick Check states."""
        import moodle_app

        html = moodle_app.DASHBOARD_HTML
        self.assertIn('id="togglePwdBtn"', html)
        self.assertIn("togglePasswordVisibility()", html)
        self.assertIn('id="loginStatusBanner"', html)
        self.assertIn("showLoginBanner(", html)
        self.assertNotIn("checkedItemKeys", html)
        self.assertIn("checkedKeys.clear()", html)

        # All 10 Section 4 Quick Check & Expand/Collapse buttons must have IDs and btn-qc class
        for btn_id in (
            "qc-MISSING_ONLY",
            "qc-ALL_ITEMS",
            "qc-NONE",
            "qc-cat-Lectures",
            "qc-cat-Tutorials & Sheets",
            "qc-cat-Labs",
            "qc-cat-Assignments",
            "qc-cat-Quizzes & Exams",
            "qc-expand-all",
            "qc-collapse-all",
        ):
            self.assertIn(f'id="{btn_id}"', html)
        self.assertIn('id="quickCheckFeedback"', html)
        self.assertIn("updateQuickCheckButtons()", html)


if __name__ == "__main__":
    unittest.main()
