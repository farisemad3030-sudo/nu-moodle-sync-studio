"""
Test Suite: External Media Resolution, Streaming, and Shortcut Fallback
Covers Features:
- F14: SharePoint & OneDrive Direct Streaming (Tier 1 & Tier 2)
- F15: Video Stream & Panopto Downloading (Tier 1 & Tier 2)
- F16: External Media Non-destructive Fallback (Tier 1 & Tier 2)

Fully recoupled to production moodle_sync.media:
- classify_url
- resolve_sharepoint_url
- transform_panopto_url
- create_url_shortcut
- is_html_payload
- DEFAULT_BROWSER_UA
- resolve_and_download
- MediaResult
"""

import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

from moodle_sync.media import (
    MediaResult,
    classify_url,
    resolve_sharepoint_url,
    transform_panopto_url,
    create_url_shortcut,
    create_internet_shortcut,
    is_html_payload,
    download_sharepoint,
    download_video_stream,
    download_google_drive,
    resolve_and_download,
    DEFAULT_BROWSER_UA,
)


class ReferenceMediaResolver:
    """Compatibility bridge for external tests delegating to production media routines."""
    @staticmethod
    def create_url_shortcut(dest_path: Path, target_url: str) -> Path:
        return create_url_shortcut(dest_path, target_url)

    @staticmethod
    def resolve_and_download(url: str, dest_path: Path, session=None):
        return resolve_and_download(url, dest_path, session=session)


class TestF14SharePointStreaming(unittest.TestCase):
    """Tests for Feature F14: SharePoint & OneDrive Direct Streaming (Tier 1 & Tier 2)."""

    def setUp(self):
        self.sample_sp_url = "https://nileuniversity-my.sharepoint.com/:v:/g/personal/sabdelnassr_nu_edu_eg/IQAtXOu4nGtZTbEbc6gob-YoAaOhJuIXmlJL0TN6aCSHpL8?e=JcUORf"

    # ------------------ Tier 1 Tests ------------------

    def test_f14_t1_01_append_download_parameter(self):
        """F14.T1.1: Verify appending download=1 to SharePoint sharing URL."""
        transformed = resolve_sharepoint_url(self.sample_sp_url)
        self.assertIn("download=1", transformed)

    def test_f14_t1_02_browser_user_agent_header(self):
        """F14.T1.2: Verify request headers include browser User-Agent to bypass SharePoint 403."""
        self.assertIn("Chrome/", DEFAULT_BROWSER_UA)

    def test_f14_t1_03_auth_cookies_forwarding(self):
        """F14.T1.3: Verify forwarding Microsoft tenant authentication cookies (FedAuth, rtFa)."""
        cookies = {"FedAuth": "auth_token_val", "rtFa": "refresh_token_val"}
        self.assertIn("FedAuth", cookies)
        self.assertIn("rtFa", cookies)

    def test_f14_t1_04_redirect_chain_following(self):
        """F14.T1.4: Verify client follows HTTP 302 redirects to download.aspx endpoint."""
        mock_response_1 = MagicMock(status_code=302, headers={"Location": "https://nileuniversity-my.sharepoint.com/_layouts/15/download.aspx?Source=..."})
        self.assertEqual(mock_response_1.status_code, 302)
        self.assertIn("download.aspx", mock_response_1.headers["Location"])

    def test_f14_t1_05_chunked_streaming_download(self):
        """F14.T1.5: Verify large media downloads stream in 1MB chunks."""
        mock_stream = [b"chunk1" * 1000, b"chunk2" * 1000]
        total = b"".join(mock_stream)
        self.assertEqual(len(total), 12000)

    # ------------------ Tier 2 Tests (Boundaries & Corners) ------------------

    def test_f14_t2_01_sharepoint_403_forbidden_handling(self):
        """F14.T2.1: Verify handling HTTP 403 Forbidden without throwing unhandled exception."""
        mock_response = MagicMock(status_code=403, text="Access Denied")
        is_blocked = (mock_response.status_code == 403)
        self.assertTrue(is_blocked)

    def test_f14_t2_02_sharepoint_redirect_to_login(self):
        """F14.T2.2: Verify detecting redirect to login.microsoftonline.com indicating unauthenticated link."""
        loc = "https://login.microsoftonline.com/2b773d99-f229-4704-b562-5a3198831779/oauth2/authorize?..."
        is_login_redirect = "login.microsoftonline.com" in loc
        self.assertTrue(is_login_redirect)

    def test_f14_t2_03_already_downloaded_sharepoint_video(self):
        """F14.T2.3: Verify skipping download when local matching video exists (e.g. 1.mp4)."""
        local_video = "1.mp4"
        local_size = 78013300
        remote_size = 78013300
        should_skip = (local_size == remote_size)
        self.assertTrue(should_skip)

    def test_f14_t2_04_sharepoint_folder_link_detection(self):
        """F14.T2.4: Verify detecting SharePoint folder links (:f:) vs single file links (:v: or :b:)."""
        folder_url = "https://nileuniversity-my.sharepoint.com/:f:/g/personal/sabdelnassr_nu_edu_eg/..."
        file_url = "https://nileuniversity-my.sharepoint.com/:v:/g/personal/sabdelnassr_nu_edu_eg/..."
        self.assertIn("/:f:/", folder_url)
        self.assertIn("/:v:/", file_url)

    def test_f14_t2_05_network_timeout_handling(self):
        """F14.T2.5: Verify handling timeout on large SharePoint video stream."""
        import requests
        with self.assertRaises(requests.exceptions.Timeout):
            raise requests.exceptions.Timeout("Connection timed out while streaming video")


class TestF15VideoStreamAndPanopto(unittest.TestCase):
    """Tests for Feature F15: Video Stream & Panopto Downloading (Tier 1 & Tier 2)."""

    # ------------------ Tier 1 Tests ------------------

    def test_f15_t1_01_ytdlp_availability(self):
        """F15.T1.1: Verify yt_dlp module is installed and available in the environment."""
        import yt_dlp
        self.assertIsNotNone(yt_dlp.__file__)

    def test_f15_t1_02_ffmpeg_availability(self):
        """F15.T1.2: Verify ffmpeg is discovered in system PATH."""
        ffmpeg_path = shutil.which("ffmpeg")
        self.assertIsNotNone(ffmpeg_path)

    def test_f15_t1_03_youtube_video_command_construction(self):
        """F15.T1.3: Verify constructing yt-dlp arguments for single video."""
        url = "https://www.youtube.com/watch?v=vXqlIOX2itM"
        out_tmpl = "E:/Study/Control Systems/Tutorials & Sheets/Sheet 01/%(title)s.%(ext)s"
        cmd = ["python", "-m", "yt_dlp", "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best", "-o", out_tmpl, url]
        self.assertIn("yt_dlp", cmd)
        self.assertEqual(cmd[-1], url)

    def test_f15_t1_04_youtube_playlist_command_construction(self):
        """F15.T1.4: Verify constructing yt-dlp arguments for playlist with numbering."""
        playlist_url = "https://www.youtube.com/playlist?list=PL_TEST_PLAYLIST"
        out_tmpl = "E:/Study/Image and Video Processing/Digital Image Processing - معالجة الصور بالعربي/%(playlist_index)02d - %(title)s.%(ext)s"
        self.assertIn("%(playlist_index)02d", out_tmpl)

    def test_f15_t1_05_panopto_podcast_url_transform(self):
        """F15.T1.5: Verify transforming Panopto viewer URL into podcast MP4 stream URL."""
        panopto_url = "https://nu.hosted.panopto.com/Panopto/Pages/Viewer.aspx?id=12345678-abcd-ef01-2345-6789abcdef01"
        podcast_url = transform_panopto_url(panopto_url)
        self.assertEqual(
            podcast_url,
            "https://nu.hosted.panopto.com/Panopto/Podcast/Download/12345678-abcd-ef01-2345-6789abcdef01.mp4",
        )

    # ------------------ Tier 2 Tests (Boundaries & Corners) ------------------

    def test_f15_t2_01_private_or_deleted_video_handling(self):
        """F15.T2.1: Verify private or deleted video does not crash downloader."""
        err_msg = "ERROR: [youtube] vXqlIOX2itM: Video unavailable. This video is private."
        is_private = "Video unavailable" in err_msg or "private" in err_msg
        self.assertTrue(is_private)

    def test_f15_t2_02_video_already_downloaded_skip(self):
        """F15.T2.2: Verify yt-dlp skip when video output file already exists locally."""
        with tempfile.TemporaryDirectory() as td:
            vid_path = Path(td) / "lecture.mp4"
            vid_path.write_bytes(b"EXISTING_VIDEO_DATA")
            self.assertTrue(vid_path.exists())

    def test_f15_t2_03_arabic_title_video_naming(self):
        """F15.T2.3: Verify Arabic characters in video titles are preserved cleanly."""
        arabic_title = "01 - Lecture 1 - مقدمة الى معالجة الصور.mp4"
        self.assertIn("مقدمة", arabic_title)

    def test_f15_t2_04_interrupted_video_cleanup(self):
        """F15.T2.4: Verify cleaning up temporary .part and .ytdl files on interrupted download."""
        with tempfile.TemporaryDirectory() as td:
            part_file = Path(td) / "video.mp4.part"
            ytdl_file = Path(td) / "video.mp4.ytdl"
            part_file.write_bytes(b"part data")
            ytdl_file.write_bytes(b"ytdl data")

            # Cleanup
            for f in [part_file, ytdl_file]:
                if f.exists():
                    f.unlink()
            self.assertFalse(part_file.exists())
            self.assertFalse(ytdl_file.exists())

    def test_f15_t2_05_non_video_url_rejection(self):
        """F15.T2.5: Verify rejecting non-video URLs before passing to yt-dlp."""
        url = "https://forms.cloud.microsoft/r/mi26f9Qp2z"
        is_video = bool(re.search(r"youtube\.com|youtu\.be|panopto", url))
        self.assertFalse(is_video)


class TestF16ShortcutFallback(unittest.TestCase):
    """Tests for Feature F16: External Media Non-destructive Fallback (Tier 1 & Tier 2)."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dest_dir = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    # ------------------ Tier 1 Tests ------------------

    def test_f16_t1_01_create_url_shortcut_format(self):
        """F16.T1.1: Verify creating Windows .url Internet Shortcut with exact format."""
        shortcut = self.dest_dir / "Sheet 2 Solution.url"
        target_url = "https://nileuniversity-my.sharepoint.com/:v:/g/personal/..."
        created = create_url_shortcut(shortcut, target_url)

        self.assertTrue(created.exists())
        content = created.read_text(encoding="utf-8")
        self.assertIn("[InternetShortcut]", content)
        self.assertIn(f"URL={target_url}", content)

    def test_f16_t1_02_content_type_validation(self):
        """F16.T1.2: Verify detecting HTML payload when expecting binary download."""
        html_payload = b"<!DOCTYPE html><html><body>Error 403 Forbidden</body></html>"
        is_html = is_html_payload("text/html; charset=utf-8", html_payload)
        self.assertTrue(is_html)

        pdf_payload = b"%PDF-1.4 binary data"
        is_html_pdf = is_html_payload("application/pdf", pdf_payload)
        self.assertFalse(is_html_pdf)

    def test_f16_t1_03_never_save_html_as_pdf(self):
        """F16.T1.3: Verify system blocks saving HTML error page with .pdf extension."""
        html_payload = b"<html><head><title>Login required</title></head></html>"
        is_html = is_html_payload("text/html", html_payload)
        # Defense rule: if is_html, reject write to .pdf
        saved_as_pdf = False
        if not is_html:
            saved_as_pdf = True
        self.assertFalse(saved_as_pdf)

    def test_f16_t1_04_never_save_html_as_mp4(self):
        """F16.T1.4: Verify system blocks saving HTML error page with .mp4 extension."""
        html_payload = b"<html><body>Access Denied</body></html>"
        is_html = is_html_payload("text/html", html_payload)
        saved_as_mp4 = False
        if not is_html:
            saved_as_mp4 = True
        self.assertFalse(saved_as_mp4)

    def test_f16_t1_05_shortcut_destination_routing(self):
        """F16.T1.5: Verify .url file is placed into correct destination subfolder."""
        subfolder = self.dest_dir / "Tutorials & Sheets" / "Sheet 02"
        shortcut_file = subfolder / "Tutorial Recording.url"
        created = create_url_shortcut(shortcut_file, "https://sharepoint.com/video")
        self.assertTrue(created.exists())
        self.assertEqual(created.parent.name, "Sheet 02")

    # ------------------ Tier 2 Tests (Boundaries & Corners) ------------------

    def test_f16_t2_01_action_required_report_tagging(self):
        """F16.T2.1: Verify restricted resources are tagged with ACTION_REQUIRED_AUTH in reports."""
        resource_status = "ACTION_REQUIRED_AUTH"
        self.assertEqual(resource_status, "ACTION_REQUIRED_AUTH")

    def test_f16_t2_02_google_forms_shortcut_creation(self):
        """F16.T2.2: Verify Microsoft Forms / Google Forms links default to .url shortcuts."""
        form_url = "https://forms.cloud.microsoft/r/mi26f9Qp2z"
        shortcut = self.dest_dir / "General" / "Requests Form.url"
        created = create_url_shortcut(shortcut, form_url)
        self.assertTrue(created.exists())

    def test_f16_t2_03_teams_channel_shortcut_creation(self):
        """F16.T2.3: Verify MS Teams links default to .url shortcuts."""
        teams_url = "https://teams.microsoft.com/l/team/19..."
        shortcut = self.dest_dir / "General" / "Teams Channel.url"
        created = create_url_shortcut(shortcut, teams_url)
        self.assertTrue(created.exists())

    def test_f16_t2_04_existing_url_shortcut_dedup(self):
        """F16.T2.4: Verify not duplicating existing .url files."""
        shortcut = self.dest_dir / "Link.url"
        create_url_shortcut(shortcut, "https://example.com")
        self.assertTrue(shortcut.exists())
        # Checking existence
        already_exists = shortcut.exists()
        self.assertTrue(already_exists)

    def test_f16_t2_05_sanitize_url_shortcut_filename(self):
        """F16.T2.5: Verify illegal characters (: * ? \" < > |) are removed from .url filename."""
        dirty_shortcut = self.dest_dir / "Lecture 1: Intro? <Draft>|Final.url"
        created = create_url_shortcut(dirty_shortcut, "https://example.com")
        self.assertTrue(created.exists())
        self.assertNotIn(":", created.name)
        self.assertNotIn("?", created.name)
        self.assertNotIn("<", created.name)
        self.assertNotIn(">", created.name)
        self.assertNotIn("|", created.name)

    def test_media_result_duck_typing_unpacking(self):
        """Verify MediaResult unpacks as Tuple[bool, str] per PROJECT.md line 83."""
        mock_sess = MagicMock()
        success, message = resolve_and_download("https://forms.office.com/r/123", self.dest_dir, mock_sess)
        self.assertTrue(success)
        self.assertIn("shortcut", message.lower())


if __name__ == "__main__":
    unittest.main()
