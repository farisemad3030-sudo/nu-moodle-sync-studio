"""
Moodle Synchronizer Test Suite - Shared Fixtures, Oracles, and Mock Utilities
Authoritative source for expected outputs derived from PROJECT.md, TEST_INFRA.md, and Survey Reports.
"""

import io
import os
import re
import sys
import json
import zipfile
import tempfile
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

# Dynamic import of moodle_sync if available
try:
    import moodle_sync
    import moodle_sync.config as moodle_config
    import moodle_sync.auth as moodle_auth
    import moodle_sync.scraper as moodle_scraper
    import moodle_sync.deduplication as moodle_dedup
    import moodle_sync.archive as moodle_archive
    import moodle_sync.media as moodle_media
    import moodle_sync.syncer as moodle_syncer
    import moodle_sync.verifier as moodle_verifier
    HAS_MOODLE_SYNC = True
except ImportError:
    HAS_MOODLE_SYNC = False
    moodle_sync = None
    moodle_config = None
    moodle_auth = None
    moodle_scraper = None
    moodle_dedup = None
    moodle_archive = None
    moodle_media = None
    moodle_syncer = None
    moodle_verifier = None


# ==============================================================================
# AUTHORITATIVE CONSTANTS & MAPPINGS
# ==============================================================================

TARGET_COURSES = {
    21329: {
        "code": "ECE322",
        "name": "Electronic Circuits-2026FALL-LECT-02",
        "local_dir": "Electronic circuits",
        "url": "https://moodle.nu.edu.eg/course/view.php?id=21329",
    },
    21365: {
        "code": "ECE342",
        "name": "Computer Organization-2026FALL-LECT-02",
        "local_dir": "Computer Organization",
        "url": "https://moodle.nu.edu.eg/course/view.php?id=21365",
    },
    21383: {
        "code": "ECE343",
        "name": "Microprocessor System Design-2026FALL-LECT-02",
        "local_dir": "Microprocessor System Design",
        "url": "https://moodle.nu.edu.eg/course/view.php?id=21383",
    },
    21395: {
        "code": "ECE344",
        "name": "Control Systems-2026FALL-LECT-01",
        "local_dir": "Control Systems",
        "url": "https://moodle.nu.edu.eg/course/view.php?id=21395",
    },
    21425: {
        "code": "ECE353",
        "name": "Software Engineering-2026FALL-LECT-01",
        "local_dir": "Software Engineering",
        "url": "https://moodle.nu.edu.eg/course/view.php?id=21425",
    },
    21443: {
        "code": "ECE354",
        "name": "Image and Video Processing-2026FALL-LECT-02",
        "local_dir": "Image and Video Processing",
        "url": "https://moodle.nu.edu.eg/course/view.php?id=21443",
    },
}

NON_TARGET_DIRS = [
    "Poultry_Project",
    "Schedule",
    "Practical Training 1",
    ".agents",
]

MOODLE_HOST = "moodle.nu.edu.eg"
MOODLE_STATIC_IP = "68.183.242.170"

STANDARD_SUBFOLDERS = [
    "Lectures",
    "Tutorials & Sheets",
    "Labs",
    "Assignments",
    "General",
    "Quizzes",
    "Model Answers",
    "Exams",
    "Project",
]


# ==============================================================================
# SYNTHETIC HTML FIXTURES
# ==============================================================================

SAMPLE_ECE322_HTML = """<!DOCTYPE html>
<html lang="en">
<head><title>Course: ECE322: Electronic Circuits-2026FALL-LECT-02</title></head>
<body>
<div id="page-content">
  <ul class="topics">
    <li id="section-0" class="section main clearfix">
      <h3 class="sectionname"><span>General</span></h3>
      <ul class="section img-text">
        <li class="activity resource modtype_resource" id="module-1001">
          <div><div class="mod-indent-outer"><div class="activityinstance">
            <a href="https://moodle.nu.edu.eg/mod/resource/view.php?id=1001">
              <span class="instancename">ECE322_cal_2026<span class="accesshide"> File</span></span>
            </a>
          </div></div></div>
        </li>
      </ul>
    </li>
    <li id="section-1" class="section main clearfix">
      <h3 class="sectionname"><span>Lectures</span></h3>
      <ul class="section img-text">
        <li class="activity resource modtype_resource" id="module-1002">
          <div><div class="activityinstance">
            <a href="https://moodle.nu.edu.eg/mod/resource/view.php?id=1002">
              <span class="instancename">ECEN 322 Lecture 1<span class="accesshide"> File</span></span>
            </a>
          </div></div>
        </li>
        <li class="activity resource modtype_resource" id="module-1003">
          <div><div class="activityinstance">
            <a href="https://moodle.nu.edu.eg/mod/resource/view.php?id=1003">
              <span class="instancename">ECEN 322 Lecture 2<span class="accesshide"> File</span></span>
            </a>
          </div></div>
        </li>
      </ul>
    </li>
    <li id="section-2" class="section main clearfix">
      <h3 class="sectionname"><span>Sheets</span></h3>
      <ul class="section img-text">
        <li class="activity resource modtype_resource" id="module-1004">
          <div><div class="activityinstance">
            <a href="https://moodle.nu.edu.eg/mod/resource/view.php?id=1004">
              <span class="instancename">Sheet 1 Solution<span class="accesshide"> File</span></span>
            </a>
          </div></div>
        </li>
        <li class="activity url modtype_url" id="module-1005">
          <div><div class="activityinstance">
            <a href="https://moodle.nu.edu.eg/mod/url/view.php?id=1005">
              <span class="instancename">Sheet 1 Solution Tutorial Recording<span class="accesshide"> URL</span></span>
            </a>
          </div></div>
        </li>
      </ul>
    </li>
    <li id="section-3" class="section main clearfix">
      <h3 class="sectionname"><span>Assignments</span></h3>
      <ul class="section img-text">
        <li class="activity assign modtype_assign" id="module-1006">
          <div><div class="activityinstance">
            <a href="https://moodle.nu.edu.eg/mod/assign/view.php?id=1006">
              <span class="instancename">Assignment 1<span class="accesshide"> Assignment</span></span>
            </a>
          </div></div>
        </li>
      </ul>
    </li>
  </ul>
</div>
</body>
</html>
"""

SAMPLE_ECE342_INLINE_FOLDER_HTML = """<!DOCTYPE html>
<html lang="en">
<head><title>Course: ECE342: Computer Organization-2026FALL-LECT-02</title></head>
<body>
<div id="page-content">
  <ul class="topics">
    <li id="section-9" class="section main clearfix">
      <h3 class="sectionname"><span>Model Answers</span></h3>
      <ul class="section img-text">
        <li class="activity folder modtype_folder inline" id="module-2001">
          <div class="activityinstance">
            <span class="instancename">Quiz 1 Model Answers</span>
            <div class="box generalbox foldertree">
              <span class="fp-filename-icon">
                <a href="https://moodle.nu.edu.eg/pluginfile.php/12345/mod_folder/content/0/quiz1-1.pdf?forcedownload=1">
                  <span class="fp-filename">quiz1-1.pdf</span>
                </a>
              </span>
              <span class="fp-filename-icon">
                <a href="https://moodle.nu.edu.eg/pluginfile.php/12345/mod_folder/content/0/quiz1-2.pdf?forcedownload=1">
                  <span class="fp-filename">quiz1-2.pdf</span>
                </a>
              </span>
            </div>
          </div>
        </li>
      </ul>
    </li>
    <li id="section-7" class="section main clearfix">
      <h3 class="sectionname"><span>Labs</span></h3>
      <ul class="section img-text">
        <li class="activity assign modtype_assign" id="module-2002">
          <div class="activityinstance">
            <a href="https://moodle.nu.edu.eg/mod/assign/view.php?id=2002">
              <span class="instancename">Lab 2 - MIPS Control Flow</span>
            </a>
          </div>
        </li>
      </ul>
    </li>
  </ul>
</div>
</body>
</html>
"""

SAMPLE_ECE344_STANDALONE_FOLDER_HTML = """<!DOCTYPE html>
<html lang="en">
<head><title>Course: ECE344: Control Systems-2026FALL-LECT-01</title></head>
<body>
<div id="page-content">
  <ul class="topics">
    <li id="section-7" class="section main clearfix">
      <h3 class="sectionname"><span>Tutorials</span></h3>
      <ul class="section img-text">
        <li class="activity folder modtype_folder" id="module-3001">
          <div class="activityinstance">
            <a href="https://moodle.nu.edu.eg/mod/folder/view.php?id=3001">
              <span class="instancename">Sheet 1 Materials<span class="accesshide"> Folder</span></span>
            </a>
          </div>
        </li>
      </ul>
    </li>
  </ul>
</div>
</body>
</html>
"""

SAMPLE_STANDALONE_FOLDER_VIEW_HTML = """<!DOCTYPE html>
<html lang="en">
<head><title>Folder: Sheet 1 Materials</title></head>
<body>
<div id="page-content">
  <h2>Sheet 1 Materials</h2>
  <div class="box generalbox foldertree">
    <span class="fp-filename-icon">
      <a href="https://moodle.nu.edu.eg/pluginfile.php/4567/mod_folder/content/0/ECE344-Sheet%201-Fall26%20%282%29.pdf?forcedownload=1">
        <span class="fp-filename">ECE344-Sheet 1-Fall26 (2).pdf</span>
      </a>
    </span>
    <span class="fp-filename-icon">
      <a href="https://moodle.nu.edu.eg/pluginfile.php/4567/mod_folder/content/0/ECE344-Sheet_1%20Solution-Fall26.pdf?forcedownload=1">
        <span class="fp-filename">ECE344-Sheet_1 Solution-Fall26.pdf</span>
      </a>
    </span>
  </div>
  <div class="singlebutton">
    <form method="post" action="https://moodle.nu.edu.eg/mod/folder/download_folder.php">
      <input type="hidden" name="id" value="3001">
      <button type="submit">Download folder</button>
    </form>
  </div>
</div>
</body>
</html>
"""

SAMPLE_ASSIGN_VIEW_HTML = """<!DOCTYPE html>
<html lang="en">
<head><title>Assignment: Lab 2 - MIPS Control Flow</title></head>
<body>
<div id="page-content">
  <div id="intro" class="box generalbox">
    <p>Please implement the branch instructions as requested.</p>
    <div class="introattachment">
      <a href="https://moodle.nu.edu.eg/pluginfile.php/999/mod_assign/introattachment/0/lab2_starter.asm?forcedownload=1">
        lab2_starter.asm
      </a>
    </div>
    <div class="introattachment">
      <a href="https://moodle.nu.edu.eg/pluginfile.php/999/mod_assign/introattachment/0/Lab%202%20Manual.pdf?forcedownload=1">
        Lab 2 Manual.pdf
      </a>
    </div>
  </div>
  <div class="submissionstatustable">
    <h3>Submission status</h3>
    <div class="fileuploadsubmission">
      <a href="https://moodle.nu.edu.eg/pluginfile.php/999/assignsubmission_file/submission_files/100/Faris%20Emad%20241002282%20Lab2.asm">
        Faris Emad 241002282 Lab2.asm
      </a>
    </div>
  </div>
</div>
</body>
</html>
"""

SAMPLE_ECE354_ARABIC_HTML = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Course: ECE354: Image and Video Processing-2026FALL-LECT-02</title></head>
<body>
<div id="page-content">
  <ul class="topics">
    <li id="section-1" class="section main clearfix">
      <h3 class="sectionname"><span>Digital Image Processing - معالجة الصور بالعربي</span></h3>
      <ul class="section img-text">
        <li class="activity resource modtype_resource" id="module-4001">
          <div class="activityinstance">
            <a href="https://moodle.nu.edu.eg/mod/resource/view.php?id=4001">
              <span class="instancename">01 - Lecture 1 - مقدمة الى معالجة الصور.mp4</span>
            </a>
          </div>
        </li>
      </ul>
    </li>
  </ul>
</div>
</body>
</html>
"""

SAMPLE_EXTERNAL_URL_HTML = """<!DOCTYPE html>
<html lang="en">
<head><title>External URLs Test</title></head>
<body>
<ul class="section img-text">
  <li class="activity url" id="url-1">
    <a href="https://nileuniversity-my.sharepoint.com/:v:/g/personal/sabdelnassr_nu_edu_eg/IQAtXOu4nGtZTbEbc6gob-YoAaOhJuIXmlJL0TN6aCSHpL8?e=JcUORf">
      SharePoint Stream Video
    </a>
  </li>
  <li class="activity url" id="url-2">
    <a href="https://www.youtube.com/watch?v=vXqlIOX2itM">
      YouTube Lecture Video
    </a>
  </li>
  <li class="activity url" id="url-3">
    <a href="https://teams.microsoft.com/l/team/19%3Ameeting...%40thread.tacv2/conversations">
      Microsoft Teams Channel
    </a>
  </li>
  <li class="activity url" id="url-4">
    <a href="https://forms.cloud.microsoft/r/mi26f9Qp2z">
      Requests MS Form
    </a>
  </li>
  <li class="activity url" id="url-5">
    <a href="https://www.dsi.unive.it/~gasparetto/materials/MIPS_Instruction_Set.pdf">
      External MIPS PDF
    </a>
  </li>
  <li class="activity url" id="url-6">
    <a href="https://nu.hosted.panopto.com/Panopto/Pages/Viewer.aspx?id=12345-abcde">
      Panopto Recording
    </a>
  </li>
  <li class="activity url" id="url-7">
    <a href="https://drive.google.com/file/d/1A2B3C4D5E/view">
      Google Drive Document
    </a>
  </li>
</ul>
</body>
</html>
"""


# ==============================================================================
# ARCHIVE TEST HELPERS
# ==============================================================================

def create_clean_zip(dest_path: Path, files: Dict[str, bytes]) -> Path:
    """Create a clean standard ZIP file with given files."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest_path, "w", zipfile.ZIP_DEFLATED) as z:
        for fname, data in files.items():
            z.writestr(fname, data)
    return dest_path


def create_macosx_zip(dest_path: Path, valid_files: Dict[str, bytes]) -> Path:
    """Create a ZIP containing macOS metadata junk (__MACOSX and AppleDouble ._ files)."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest_path, "w", zipfile.ZIP_DEFLATED) as z:
        for fname, data in valid_files.items():
            z.writestr(fname, data)
            # Add AppleDouble junk
            z.writestr(f"__MACOSX/._ {fname}", b"Mac OS X AppleDouble header metadata")
        z.writestr("__MACOSX/.DS_Store", b"junk")
        z.writestr(".DS_Store", b"junk desktop settings")
        z.writestr("Thumbs.db", b"junk windows thumbnails")
    return dest_path


def create_single_root_zip(dest_path: Path, root_name: str, files: Dict[str, bytes]) -> Path:
    """Create a ZIP where all files are nested inside a single root directory."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest_path, "w", zipfile.ZIP_DEFLATED) as z:
        for fname, data in files.items():
            z.writestr(f"{root_name}/{fname}", data)
    return dest_path


def create_corrupt_zip(dest_path: Path) -> Path:
    """Create an invalid corrupt ZIP file."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(b"PK\x03\x04CORRUPTED_ZIP_DATA_TRUNCATED_OFFSET_HERE")
    return dest_path


# ==============================================================================
# REFERENCE ORACLES & SPECIFICATION ALGORITHMS
# (Authoritative reference models per PROJECT.md and survey findings)
# ==============================================================================

class ReferenceDeduplicator:
    """Authoritative reference implementation of 4-tier deduplication."""

    @staticmethod
    def normalize_filename(name: str) -> str:
        """Tier 2 normalization: NFKC, strip browser suffixes (1), separators to space, lower."""
        # Normalize Unicode
        norm = unicodedata.normalize("NFKC", name)
        # Separate stem and ext
        stem, ext = os.path.splitext(norm)
        # Strip all parenthesized duplicate suffixes like ' (1)', ' (2)', ' (1) (2)'
        while True:
            new_stem = re.sub(r"\s*\(\d+\)$", "", stem)
            if new_stem == stem:
                break
            stem = new_stem
        # Replace %20, _, - with space
        stem = stem.replace("%20", " ").replace("_", " ").replace("-", " ")
        # Strip trailing copy numbers like ' 1', ' 2' ONLY if preceded by standard copy pattern
        stem = re.sub(r"\s+copy(?:\s+\d+)?$", "", stem, flags=re.IGNORECASE)
        # Collapse multiple spaces
        stem = re.sub(r"\s+", " ", stem).strip()
        return (stem + ext).lower()

    @staticmethod
    def match(
        local_files: List[Tuple[str, int]],  # List of (filename, size_bytes)
        remote_name: str,
        remote_size: Optional[int] = None,
        activity_title: Optional[str] = None,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Evaluate 4-tier matching against local files.
        Returns: (is_match, matched_local_name, match_tier)
        """
        # Exclude lock files from candidate pool
        candidates = [(f, sz) for f, sz in local_files if not f.startswith("~$")]

        # Tier 1: Canonical exact name match (case-insensitive)
        for loc_name, loc_size in candidates:
            if loc_name.lower() == remote_name.lower():
                if remote_size is None or loc_size == remote_size:
                    return True, loc_name, "TIER_1_CANONICAL"

        # Tier 2: Normalized name match
        norm_remote = ReferenceDeduplicator.normalize_filename(remote_name)
        for loc_name, loc_size in candidates:
            if ReferenceDeduplicator.normalize_filename(loc_name) == norm_remote:
                if remote_size is None or loc_size == remote_size:
                    return True, loc_name, "TIER_2_NORMALIZED"

        # Tier 3: Semantic activity title match
        if activity_title:
            norm_title = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", activity_title)).lower().strip()
            title_tokens = set(norm_title.split())
            remote_ext = os.path.splitext(remote_name)[1].lower()

            for loc_name, loc_size in candidates:
                loc_ext = os.path.splitext(loc_name)[1].lower()
                if loc_ext == remote_ext:
                    norm_loc = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", loc_name)).lower().strip()
                    loc_tokens = set(norm_loc.split())
                    # Check token overlap
                    if title_tokens and title_tokens.issubset(loc_tokens):
                        return True, loc_name, "TIER_3_SEMANTIC"

        # Tier 4: Content-Length / File Size match
        if remote_size and remote_size > 0:
            remote_ext = os.path.splitext(remote_name)[1].lower()
            for loc_name, loc_size in candidates:
                if loc_size == remote_size and os.path.splitext(loc_name)[1].lower() == remote_ext:
                    return True, loc_name, "TIER_4_SIZE"

        return False, None, None


class ReferenceClassifier:
    """Authoritative reference classification for course subfolders."""

    @staticmethod
    def classify(course_code: str, section_name: str, filename: str) -> str:
        sec = section_name.lower()
        fn = filename.lower()
        combined = f"{sec} {fn}"
        # Normalize underscores, dashes, dots to spaces for word boundary matching
        cleaned = re.sub(r"[_\-.]+", " ", combined)

        # 1. Lectures
        if re.search(r"\b(?:lecture|lec|slides|chapter|presentation)\b", cleaned) or "ch0" in cleaned or "ch1" in cleaned:
            return "Lectures"

        # 2. Tutorials & Sheets
        if re.search(r"\b(?:sheet|tutorial|tut|problem set)\b", cleaned):
            # Extract number if present
            num_match = re.search(r"(?:sheet|tutorial|tut)\s*0*(\d+)", cleaned)
            if num_match:
                num = int(num_match.group(1))
                if course_code == "ECE353":
                    return f"Tutorials & Sheets/Tutorial {num:02d}"
                else:
                    return f"Tutorials & Sheets/Sheet {num:02d}"
            return "Tutorials & Sheets"

        # 3. Labs (word boundary so 'syllabus' does not match)
        if re.search(r"\b(?:lab|labs|experiment|practical|qtspim|proteus)\b", cleaned):
            return "Labs"

        # 4. Assignments
        if re.search(r"\b(?:assign|assignment|assignments|homework|hw|task)\b", cleaned):
            return "Assignments"

        # 5. Quizzes / Exams / Model Answers
        if "model answer" in cleaned or "solution manual" in cleaned:
            return "Model Answers"
        if re.search(r"\b(?:quiz|quizzes)\b", cleaned):
            return "Quizzes"
        if re.search(r"\b(?:exam|exams|midterm|final)\b", cleaned):
            return "Exams"

        # 6. Project
        if re.search(r"\b(?:project|capstone)\b", cleaned):
            return "Project"

        # Fallback
        return "General"


class ReferenceURLClassifier:
    """Authoritative URL categorizer."""

    @staticmethod
    def classify(url: str) -> str:
        u = url.lower()
        if "sharepoint.com" in u or "1drv.ms" in u or "onedrive" in u:
            return "SHAREPOINT"
        if "youtube.com" in u or "youtu.be" in u:
            return "YOUTUBE"
        if "panopto.com" in u:
            return "PANOPTO"
        if "teams.microsoft.com" in u:
            return "TEAMS"
        if "forms.cloud.microsoft" in u or "forms.office.com" in u or "docs.google.com/forms" in u:
            return "FORMS"
        if "drive.google.com" in u:
            return "GOOGLE_DRIVE"
        if any(u.endswith(ext) for ext in [".pdf", ".docx", ".zip", ".mp4", ".pptx"]):
            return "DIRECT_BINARY"
        return "GENERIC_WEB"


# ==============================================================================
# REPORT VALIDATOR UTILITY
# ==============================================================================

def validate_sync_report_schema(data: Dict[str, Any]) -> List[str]:
    """Validate JSON report structure against TEST_INFRA.md specification."""
    errors = []
    if "sync_timestamp" not in data:
        errors.append("Missing 'sync_timestamp'")
    if "summary" not in data:
        errors.append("Missing 'summary'")
    else:
        summary = data["summary"]
        required_summary_keys = [
            "total_target_courses",
            "total_moodle_resources_discovered",
            "total_local_files_evaluated",
            "total_skipped_already_present",
            "total_newly_downloaded",
            "total_archives_extracted",
            "total_external_links_resolved",
            "total_action_required",
            "zero_missing_downloadables_confirmed",
        ]
        for key in required_summary_keys:
            if key not in summary:
                errors.append(f"Missing summary key: {key}")

    if "courses" not in data or not isinstance(data["courses"], list):
        errors.append("Missing or invalid 'courses' array")
    return errors
