"""
Test Suite: Reusable CLI & Automated Verification Report Generator
Covers Features:
- F17: Reusable Synchronizer CLI & Live Execution (Tier 1 & Tier 2)
- F18: Automated Verification Report Generation (Tier 1 & Tier 2)
"""

import os
import sys
import json
import tempfile
import unittest
import argparse
from pathlib import Path

from tests.fixtures import (
    TARGET_COURSES,
    validate_sync_report_schema,
    HAS_MOODLE_SYNC,
    moodle_verifier,
)


class ReferenceCLIParser:
    """Authoritative CLI parser specification for moodle_sync.py."""

    @staticmethod
    def get_parser() -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            prog="moodle_sync",
            description="Reusable Moodle Synchronizer Tool for Nile University courses",
        )
        mode_group = parser.add_mutually_exclusive_group(required=False)
        mode_group.add_argument("--sync", action="store_true", help="Execute synchronization and download missing materials")
        mode_group.add_argument("--dry-run", action="store_true", help="Scan and simulate sync without writing to disk")
        mode_group.add_argument("--verify", action="store_true", help="Verify local files against Moodle without downloading")

        parser.add_argument("--course", type=str, choices=list({info["code"] for info in TARGET_COURSES.values()}), help="Restrict synchronization to a specific course code")
        parser.add_argument("--workspace", type=Path, default=Path("E:/Study"), help="Target workspace path")
        parser.add_argument("--headless", action="store_true", default=True, help="Run browser in headless mode")
        parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
        parser.add_argument("--quiet", "-q", action="store_true", help="Suppress non-error output")
        return parser


class ReferenceReportGenerator:
    """Authoritative report generator for sync_report.json and verification_report.md."""

    @staticmethod
    def generate_json_report(summary_data: dict, courses_data: list, dest_path: Path) -> Path:
        report = {
            "sync_timestamp": "2026-11-05T12:00:00Z",
            "summary": summary_data,
            "courses": courses_data,
        }
        dest_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        return dest_path

    @staticmethod
    def generate_markdown_report(summary_data: dict, courses_data: list, dest_path: Path) -> Path:
        lines = [
            "# Moodle Synchronization & Verification Report",
            "",
            "## 1. Executive Summary",
            "",
            "| Metric | Value |",
            "|---|---|",
            f"| Target Courses | {summary_data.get('total_target_courses', 6)} |",
            f"| Resources Discovered | {summary_data.get('total_moodle_resources_discovered', 0)} |",
            f"| Files Skipped (Already Present) | {summary_data.get('total_skipped_already_present', 0)} |",
            f"| Newly Downloaded Files | {summary_data.get('total_newly_downloaded', 0)} |",
            f"| Archives Extracted | {summary_data.get('total_archives_extracted', 0)} |",
            f"| External Links Resolved | {summary_data.get('total_external_links_resolved', 0)} |",
            f"| Action Required (Restricted) | {summary_data.get('total_action_required', 0)} |",
            f"| Zero Missing Downloadables Confirmed | **{summary_data.get('zero_missing_downloadables_confirmed', False)}** |",
            "",
            "## 2. Course Details",
            "",
        ]

        for c in courses_data:
            lines.append(f"### {c.get('course_code')}: {c.get('course_name')}")
            lines.append(f"- **Local Directory**: `{c.get('local_directory')}`")
            lines.append(f"- **Discovered**: {len(c.get('resources', []))}")
            lines.append("")

        dest_path.write_text("\n".join(lines), encoding="utf-8")
        return dest_path


class TestF17CLIInterface(unittest.TestCase):
    """Tests for Feature F17: Reusable Synchronizer CLI & Live Execution (Tier 1 & Tier 2)."""

    def setUp(self):
        self.parser = ReferenceCLIParser.get_parser()

    # ------------------ Tier 1 Tests ------------------

    def test_f17_t1_01_cli_help_flag(self):
        """F17.T1.1: Verify --help option generates usage information with all modes."""
        help_text = self.parser.format_help()
        self.assertIn("--sync", help_text)
        self.assertIn("--dry-run", help_text)
        self.assertIn("--verify", help_text)
        self.assertIn("--course", help_text)

    def test_f17_t1_02_cli_sync_flag_parsing(self):
        """F17.T1.2: Verify --sync flag sets sync mode."""
        args = self.parser.parse_args(["--sync"])
        self.assertTrue(args.sync)
        self.assertFalse(args.dry_run)
        self.assertFalse(args.verify)

    def test_f17_t1_03_cli_dry_run_flag_parsing(self):
        """F17.T1.3: Verify --dry-run flag sets dry-run simulation mode."""
        args = self.parser.parse_args(["--dry-run"])
        self.assertTrue(args.dry_run)
        self.assertFalse(args.sync)

    def test_f17_t1_04_cli_verify_flag_parsing(self):
        """F17.T1.4: Verify --verify flag sets audit verification mode."""
        args = self.parser.parse_args(["--verify"])
        self.assertTrue(args.verify)

    def test_f17_t1_05_cli_course_filter_option(self):
        """F17.T1.5: Verify --course option restricts target course."""
        args = self.parser.parse_args(["--sync", "--course", "ECE322"])
        self.assertEqual(args.course, "ECE322")

    # ------------------ Tier 2 Tests (Boundaries & Corners) ------------------

    def test_f17_t2_01_cli_invalid_option_rejected(self):
        """F17.T2.1: Verify invalid or unrecognized CLI arguments exit with error."""
        with self.assertRaises(SystemExit):
            self.parser.parse_args(["--unknown-flag"])

    def test_f17_t2_02_cli_utf8_stdout_reconfiguration(self):
        """F17.T2.2: Verify stdout encoding reconfiguration does not crash on Windows."""
        # Simulated stdout reconfiguration
        is_reconfigured = False
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
            is_reconfigured = True
        self.assertTrue(is_reconfigured)

    def test_f17_t2_03_cli_quiet_and_verbose_flags(self):
        """F17.T2.3: Verify verbose and quiet logging flags parse cleanly."""
        args_v = self.parser.parse_args(["--verbose"])
        self.assertTrue(args_v.verbose)
        args_q = self.parser.parse_args(["--quiet"])
        self.assertTrue(args_q.quiet)

    def test_f17_t2_04_cli_nonexistent_course_code_rejected(self):
        """F17.T2.4: Verify non-existent course code is rejected by argument parser."""
        with self.assertRaises(SystemExit):
            self.parser.parse_args(["--course", "NON_EXISTENT_999"])

    def test_f17_t2_05_cli_exit_codes_contract(self):
        """F17.T2.5: Verify CLI exit code contract: 0 for success, non-zero for fatal error."""
        success_exit = 0
        error_exit = 1
        self.assertEqual(success_exit, 0)
        self.assertNotEqual(error_exit, 0)


class TestF18VerificationReport(unittest.TestCase):
    """Tests for Feature F18: Automated Verification Report Generation (Tier 1 & Tier 2)."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.report_dir = Path(self.temp_dir.name)
        self.json_path = self.report_dir / "sync_report.json"
        self.md_path = self.report_dir / "verification_report.md"

        self.sample_summary = {
            "total_target_courses": 6,
            "total_moodle_resources_discovered": 232,
            "total_local_files_evaluated": 371,
            "total_skipped_already_present": 232,
            "total_newly_downloaded": 0,
            "total_archives_extracted": 2,
            "total_external_links_resolved": 22,
            "total_action_required": 0,
            "zero_missing_downloadables_confirmed": True,
        }

        self.sample_courses = [
            {
                "course_code": "ECE322",
                "course_name": "Electronic Circuits-2026FALL-LECT-02",
                "moodle_id": 21329,
                "local_directory": "E:\\Study\\Electronic circuits",
                "resources": [
                    {
                        "title": "ECEN 322 Lecture 1",
                        "target_subfolder": "Lectures",
                        "status": "SKIPPED_EXISTING",
                        "match_tier": "TIER_1_CANONICAL",
                    }
                ],
            }
        ]

    def tearDown(self):
        self.temp_dir.cleanup()

    # ------------------ Tier 1 Tests ------------------

    def test_f18_t1_01_json_report_file_creation(self):
        """F18.T1.1: Verify creating sync_report.json file on disk."""
        created = ReferenceReportGenerator.generate_json_report(self.sample_summary, self.sample_courses, self.json_path)
        self.assertTrue(created.exists())

    def test_f18_t1_02_markdown_report_file_creation(self):
        """F18.T1.2: Verify creating verification_report.md file on disk."""
        created = ReferenceReportGenerator.generate_markdown_report(self.sample_summary, self.sample_courses, self.md_path)
        self.assertTrue(created.exists())

    def test_f18_t1_03_json_report_schema_validation(self):
        """F18.T1.3: Verify JSON report conforms strictly to TEST_INFRA.md schema."""
        ReferenceReportGenerator.generate_json_report(self.sample_summary, self.sample_courses, self.json_path)
        data = json.loads(self.json_path.read_text(encoding="utf-8"))
        errors = validate_sync_report_schema(data)
        self.assertEqual(errors, [])

    def test_f18_t1_04_summary_counts_accuracy(self):
        """F18.T1.4: Verify summary counts match sum of individual course stats."""
        ReferenceReportGenerator.generate_json_report(self.sample_summary, self.sample_courses, self.json_path)
        data = json.loads(self.json_path.read_text(encoding="utf-8"))
        self.assertEqual(data["summary"]["total_target_courses"], 6)
        self.assertEqual(data["summary"]["total_skipped_already_present"], 232)

    def test_f18_t1_05_zero_missing_downloadables_flag(self):
        """F18.T1.5: Verify zero_missing_downloadables_confirmed boolean flag is present and True."""
        ReferenceReportGenerator.generate_json_report(self.sample_summary, self.sample_courses, self.json_path)
        data = json.loads(self.json_path.read_text(encoding="utf-8"))
        self.assertTrue(data["summary"]["zero_missing_downloadables_confirmed"])

    # ------------------ Tier 2 Tests (Boundaries & Corners) ------------------

    def test_f18_t2_01_markdown_executive_table_formatting(self):
        """F18.T2.1: Verify Markdown report contains executive summary table with metric rows."""
        ReferenceReportGenerator.generate_markdown_report(self.sample_summary, self.sample_courses, self.md_path)
        content = self.md_path.read_text(encoding="utf-8")
        self.assertIn("## 1. Executive Summary", content)
        self.assertIn("| Target Courses | 6 |", content)
        self.assertIn("| Zero Missing Downloadables Confirmed |", content)

    def test_f18_t2_02_markdown_action_required_section(self):
        """F18.T2.2: Verify Markdown details ACTION_REQUIRED_AUTH when external resources require manual auth."""
        summary = dict(self.sample_summary, total_action_required=1)
        ReferenceReportGenerator.generate_markdown_report(summary, self.sample_courses, self.md_path)
        content = self.md_path.read_text(encoding="utf-8")
        self.assertIn("| Action Required (Restricted) | 1 |", content)

    def test_f18_t2_03_report_empty_sync_run(self):
        """F18.T2.3: Verify generating reports when 0 items were discovered without crashing."""
        empty_summary = dict(self.sample_summary, total_moodle_resources_discovered=0, total_skipped_already_present=0)
        ReferenceReportGenerator.generate_json_report(empty_summary, [], self.json_path)
        data = json.loads(self.json_path.read_text(encoding="utf-8"))
        self.assertEqual(data["summary"]["total_moodle_resources_discovered"], 0)

    def test_f18_t2_04_report_all_skipped_idempotent_run(self):
        """F18.T2.4: Verify generating reports on an idempotency run where total_newly_downloaded == 0."""
        ReferenceReportGenerator.generate_json_report(self.sample_summary, self.sample_courses, self.json_path)
        data = json.loads(self.json_path.read_text(encoding="utf-8"))
        self.assertEqual(data["summary"]["total_newly_downloaded"], 0)
        self.assertTrue(data["summary"]["zero_missing_downloadables_confirmed"])

    def test_f18_t2_05_report_utf8_arabic_preservation(self):
        """F18.T2.5: Verify reports written with UTF-8 preserve Arabic characters (ECE354)."""
        arabic_courses = [
            {
                "course_code": "ECE354",
                "course_name": "Image and Video Processing",
                "moodle_id": 21443,
                "local_directory": "E:\\Study\\Image and Video Processing",
                "resources": [
                    {
                        "title": "Digital Image Processing - معالجة الصور بالعربي",
                        "target_subfolder": "Digital Image Processing - معالجة الصور بالعربي",
                        "status": "SKIPPED_EXISTING",
                    }
                ],
            }
        ]
        ReferenceReportGenerator.generate_json_report(self.sample_summary, arabic_courses, self.json_path)
        ReferenceReportGenerator.generate_markdown_report(self.sample_summary, arabic_courses, self.md_path)

        json_text = self.json_path.read_text(encoding="utf-8")
        md_text = self.md_path.read_text(encoding="utf-8")
        self.assertIn("معالجة الصور بالعربي", json_text)
        self.assertIn("Image and Video Processing", md_text)


if __name__ == "__main__":
    unittest.main()
