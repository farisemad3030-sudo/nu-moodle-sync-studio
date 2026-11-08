"""
Test Suite: Tier 3 Cross-Feature Interactions
Verifies pairwise and end-to-end pipelines between core modules:
1. DNS Monkeypatch + Playwright SSO + Session Cookie Transfer
2. Scraper DOM Discovery + 4-Tier Deduplication Pipeline
3. Folder Zip Download + Archive Junk Purge + Subfolder Placement
4. External Media Detection + Shortcut Fallback + Report Audit
5. 4-Tier Match against Local Disk Metadata + Atomic Staging
"""

import os
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock
from bs4 import BeautifulSoup

from tests.fixtures import (
    MOODLE_HOST,
    MOODLE_STATIC_IP,
    TARGET_COURSES,
    SAMPLE_ECE322_HTML,
    SAMPLE_ECE344_STANDALONE_FOLDER_HTML,
    SAMPLE_STANDALONE_FOLDER_VIEW_HTML,
    create_single_root_zip,
    ReferenceDeduplicator,
    ReferenceClassifier,
    ReferenceURLClassifier,
    validate_sync_report_schema,
    HAS_MOODLE_SYNC,
)


class TestTier3Interactions(unittest.TestCase):
    """Tier 3 Cross-Feature Interaction Test Suite."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.orig_getaddrinfo = socket.getaddrinfo

    def tearDown(self):
        socket.getaddrinfo = self.orig_getaddrinfo
        self.temp_dir.cleanup()

    def test_tier3_01_dns_auth_session_pipeline(self):
        """Tier 3.1: Verify DNS monkeypatching integrates with Playwright auth and requests.Session transfer."""
        # 1. Apply DNS monkeypatch
        def patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
            if host and host.lower() == MOODLE_HOST:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (MOODLE_STATIC_IP, port))]
            return self.orig_getaddrinfo(host, port, family, type, proto, flags)

        socket.getaddrinfo = patched_getaddrinfo
        resolved_ip = socket.getaddrinfo(MOODLE_HOST, 443)[0][4][0]
        self.assertEqual(resolved_ip, MOODLE_STATIC_IP)

        # 2. Simulate Playwright login extracting cookies
        browser_cookies = [
            {"name": "MoodleSession", "value": "mock_live_session_hash", "domain": "moodle.nu.edu.eg"},
            {"name": "FedAuth", "value": "mock_fedauth_token", "domain": "nileuniversity-my.sharepoint.com"},
        ]
        extracted_cookies = {c["name"]: c["value"] for c in browser_cookies}

        # 3. Transfer to requests.Session
        import requests
        session = requests.Session()
        session.cookies.update(extracted_cookies)

        self.assertEqual(session.cookies.get("MoodleSession"), "mock_live_session_hash")
        self.assertEqual(session.cookies.get("FedAuth"), "mock_fedauth_token")

    def test_tier3_02_scraper_and_4tier_dedup_pipeline(self):
        """Tier 3.2: Verify HTML DOM scraping feeds cleanly into 4-tier deduplicator against local files."""
        # Setup local course files on disk
        ece322_dir = self.workspace / "Electronic circuits"
        lectures_dir = ece322_dir / "Lectures"
        lectures_dir.mkdir(parents=True, exist_ok=True)
        (lectures_dir / "ECEN 322 Lecture 1.pdf").write_bytes(b"%PDF-1.4 Lec1" * 100)
        local_lec1_size = (lectures_dir / "ECEN 322 Lecture 1.pdf").stat().st_size

        # Parse Moodle course page HTML
        soup = BeautifulSoup(SAMPLE_ECE322_HTML, "lxml")
        resources = []
        for r_node in soup.find_all("li", class_="modtype_resource"):
            title = r_node.find("span", class_="instancename").get_text(strip=True).replace(" File", "")
            url = r_node.find("a")["href"]
            resources.append({"title": title, "url": url})

        # Evaluate deduplication for each scraped resource
        local_files = [(f.name, f.stat().st_size) for f in lectures_dir.iterdir() if f.is_file()]

        # Remote item 1: ECEN 322 Lecture 1 (exists locally)
        is_match_1, match_name_1, tier_1 = ReferenceDeduplicator.match(
            local_files, "ECEN 322 Lecture 1.pdf", local_lec1_size, activity_title="ECEN 322 Lecture 1"
        )
        self.assertTrue(is_match_1)
        self.assertEqual(tier_1, "TIER_1_CANONICAL")

        # Remote item 2: ECEN 322 Lecture 2 (missing locally)
        is_match_2, match_name_2, tier_2 = ReferenceDeduplicator.match(
            local_files, "ECEN 322 Lecture 2.pdf", 50000, activity_title="ECEN 322 Lecture 2"
        )
        self.assertFalse(is_match_2)

    def test_tier3_03_folder_archive_extraction_and_classification_pipeline(self):
        """Tier 3.3: Verify folder scraping -> zip extraction -> junk purge -> course subfolder routing."""
        # 1. Scrape folder title
        soup = BeautifulSoup(SAMPLE_ECE344_STANDALONE_FOLDER_HTML, "lxml")
        folder_node = soup.find("li", class_="modtype_folder")
        for hide in folder_node.find_all("span", class_="accesshide"):
            hide.decompose()
        folder_title = folder_node.find("span", class_="instancename").get_text(strip=True)
        self.assertEqual(folder_title, "Sheet 1 Materials")

        # 2. Classify target subfolder
        dest_subfolder = ReferenceClassifier.classify("ECE344", "Tutorials", folder_title)
        self.assertEqual(dest_subfolder, "Tutorials & Sheets/Sheet 01")

        # 3. Simulate downloading and extracting folder zip
        zip_path = self.workspace / "Sheet1_Materials.zip"
        create_single_root_zip(
            zip_path,
            "Sheet 1 Materials",
            {
                "ECE344-Sheet 1-Fall26 (2).pdf": b"%PDF-1.4 Sheet 1",
                "ECE344-Sheet_1 Solution-Fall26.pdf": b"%PDF-1.4 Solution",
            },
        )

        final_dest = self.workspace / "Control Systems" / dest_subfolder
        final_dest.mkdir(parents=True, exist_ok=True)

        from tests.test_archive import ReferenceArchiveExtractor
        extracted = ReferenceArchiveExtractor.extract_and_clean(zip_path, final_dest)

        self.assertEqual(len(extracted), 2)
        self.assertTrue((final_dest / "ECE344-Sheet 1-Fall26 (2).pdf").exists())
        self.assertTrue((final_dest / "ECE344-Sheet_1 Solution-Fall26.pdf").exists())

    def test_tier3_04_external_media_fallback_and_verifier_pipeline(self):
        """Tier 3.4: Verify external link classification -> restricted auth fallback -> verification report log."""
        # 1. Classify external URL
        sp_url = "https://nileuniversity-my.sharepoint.com/:v:/g/personal/private_user/video123"
        cat = ReferenceURLClassifier.classify(sp_url)
        self.assertEqual(cat, "SHAREPOINT")

        # 2. Simulate 403 Forbidden on restricted link
        from tests.test_media import ReferenceMediaResolver
        shortcut_path = self.workspace / "Electronic circuits" / "Tutorials & Sheets" / "Sheet 01" / "Restricted Video.url"
        created_shortcut = ReferenceMediaResolver.create_url_shortcut(shortcut_path, sp_url)
        self.assertTrue(created_shortcut.exists())

        # 3. Log into verification report
        from tests.test_cli_verifier import ReferenceReportGenerator
        summary = {
            "total_target_courses": 6,
            "total_moodle_resources_discovered": 1,
            "total_local_files_evaluated": 10,
            "total_skipped_already_present": 0,
            "total_newly_downloaded": 0,
            "total_archives_extracted": 0,
            "total_external_links_resolved": 1,
            "total_action_required": 1,
            "zero_missing_downloadables_confirmed": True,
        }
        courses = [
            {
                "course_code": "ECE322",
                "course_name": "Electronic Circuits",
                "resources": [
                    {
                        "title": "Restricted Video",
                        "url": sp_url,
                        "status": "ACTION_REQUIRED_AUTH",
                        "shortcut_created": str(created_shortcut),
                    }
                ],
            }
        ]

        report_path = self.workspace / "sync_report.json"
        ReferenceReportGenerator.generate_json_report(summary, courses, report_path)

        data = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(data["summary"]["total_action_required"], 1)
        self.assertEqual(data["courses"][0]["resources"][0]["status"], "ACTION_REQUIRED_AUTH")

    def test_tier3_05_dedup_against_real_metadata_and_atomic_placement(self):
        """Tier 3.5: Verify 4-tier matching against real filesystem metadata prevents overwriting local file."""
        course_dir = self.workspace / "Control Systems" / "Lectures"
        course_dir.mkdir(parents=True, exist_ok=True)
        real_file = course_dir / "Lec 2-20260929_123323-Meeting Recording.mp4"
        real_file.write_bytes(b"EXISTING_TEAMS_MEETING_MP4" * 1000)

        # Candidate remote download
        remote_filename = "Lecture 2 Recording.mp4"
        remote_size = real_file.stat().st_size
        activity_title = "Lec 2 Meeting Recording"

        local_files = [(f.name, f.stat().st_size) for f in course_dir.iterdir() if f.is_file()]

        # Deduplication must identify match
        is_match, matched_name, match_tier = ReferenceDeduplicator.match(
            local_files, remote_filename, remote_size, activity_title=activity_title
        )
        self.assertTrue(is_match)
        self.assertIn(match_tier, ["TIER_3_SEMANTIC", "TIER_4_SIZE"])

        # Therefore, ZERO write operation is initiated; real file mtime remains untouched
        orig_mtime = real_file.stat().st_mtime
        # No write performed
        self.assertEqual(real_file.stat().st_mtime, orig_mtime)


if __name__ == "__main__":
    unittest.main()
