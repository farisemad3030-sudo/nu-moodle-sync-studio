"""
moodle_sync/config.py
Configuration constants, course mappings, network settings, and subfolder taxonomy.
"""

import os
import re
from pathlib import Path
from typing import Dict, Any, Tuple, Union, List

# --- Network & DNS Settings ---
MOODLE_HOST = "moodle.nu.edu.eg"
MOODLE_IP = os.environ.get("MOODLE_IP", "68.183.242.170")
MOODLE_STATIC_IP = "68.183.242.170"

DNS_MAPPINGS = {
    MOODLE_HOST: MOODLE_IP
}

# Chrome CLI flags for Playwright host resolution
CHROME_DNS_ARGS = [
    f"--host-rules=MAP {MOODLE_HOST} {MOODLE_IP}",
    f"--host-resolver-rules=MAP {MOODLE_HOST} {MOODLE_IP}",
    "--ignore-certificate-errors",
    "--no-sandbox",
    "--disable-dev-shm-usage",
]

# --- URLs & Endpoints ---
BASE_URL = f"https://{MOODLE_HOST}"
LOGIN_URL = f"https://{MOODLE_HOST}/login/index.php"
MY_URL = f"https://{MOODLE_HOST}/my/"

# --- Credentials ---
DEFAULT_USERNAME = ""
DEFAULT_PASSWORD = ""
ENV_USER_KEY = "MOODLE_USER"
ENV_PASS_KEY = "MOODLE_PASS"

def get_credentials() -> Tuple[str, str]:
    """Retrieve username and password from environment or defaults (never hardcoded in source)."""
    user = os.environ.get(ENV_USER_KEY, DEFAULT_USERNAME)
    password = os.environ.get(ENV_PASS_KEY, DEFAULT_PASSWORD)
    return user, password

# --- HTTP Request Settings ---
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

CONNECT_TIMEOUT = 10
REQUEST_TIMEOUT = 30
DEFAULT_TIMEOUT = (CONNECT_TIMEOUT, REQUEST_TIMEOUT)
MAX_RETRIES = 3
BACKOFF_FACTOR = 0.5
RETRY_STATUS_CODES = (429, 500, 502, 503, 504)

# --- File System Paths & Isolation ---
def _resolve_default_study_dir() -> Path:
    env_dir = os.environ.get("MOODLE_STUDY_DIR", "").strip()
    if env_dir:
        return Path(env_dir)
    if os.name == "nt" and Path("E:/").exists():
        return Path(r"E:\Study")
    return Path.home() / "Study"


BASE_STUDY_DIR = _resolve_default_study_dir()
BASE_DIR = BASE_STUDY_DIR
SESSION_CACHE_FILE = BASE_STUDY_DIR / ".moodle_session.json"

# Strict boundaries: directories in E:\Study to NEVER touch
EXCLUDED_TOP_LEVEL_DIRS = {
    ".agents",
    "Poultry_Project",
    "Schedule",
    "Practical Training 1",
    "Safe_To_Share_With_Friends",
    "$RECYCLE.BIN",
    "System Volume Information",
}
NON_TARGET_DIRS = list(EXCLUDED_TOP_LEVEL_DIRS)

# Files to ignore during scanning
IGNORED_FILE_PREFIXES = ("~$", "._")
IGNORED_FILENAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}

# --- Target 6 Courses Specification ---
COURSES_CONFIG: Dict[int, Dict[str, Any]] = {
    21329: {
        "code": "ECE322",
        "name": "Electronic Circuits-2026FALL-LECT-02",
        "folder": "Electronic circuits",
        "local_dir": "Electronic circuits",
        "moodle_id": 21329,
        "sheet_prefix": "Sheet",
        "url": f"https://{MOODLE_HOST}/course/view.php?id=21329",
    },
    21365: {
        "code": "ECE342",
        "name": "Computer Organization-2026FALL-LECT-02",
        "folder": "Computer Organization",
        "local_dir": "Computer Organization",
        "moodle_id": 21365,
        "sheet_prefix": "Sheet",
        "url": f"https://{MOODLE_HOST}/course/view.php?id=21365",
    },
    21383: {
        "code": "ECE343",
        "name": "Microprocessor System Design-2026FALL-LECT-02",
        "folder": "Microprocessor System Design",
        "local_dir": "Microprocessor System Design",
        "moodle_id": 21383,
        "sheet_prefix": "Sheet",
        "url": f"https://{MOODLE_HOST}/course/view.php?id=21383",
    },
    21395: {
        "code": "ECE344",
        "name": "Control Systems-2026FALL-LECT-01",
        "folder": "Control Systems",
        "local_dir": "Control Systems",
        "moodle_id": 21395,
        "sheet_prefix": "Sheet",
        "url": f"https://{MOODLE_HOST}/course/view.php?id=21395",
    },
    21425: {
        "code": "ECE353",
        "name": "Software Engineering-2026FALL-LECT-01",
        "folder": "Software Engineering",
        "local_dir": "Software Engineering",
        "moodle_id": 21425,
        "sheet_prefix": "Tutorial",  # Naming convention uses Tutorial 01..05
        "url": f"https://{MOODLE_HOST}/course/view.php?id=21425",
    },
    21443: {
        "code": "ECE354",
        "name": "Image and Video Processing-2026FALL-LECT-02",
        "folder": "Image and Video Processing",
        "local_dir": "Image and Video Processing",
        "moodle_id": 21443,
        "sheet_prefix": "Sheet",
        "url": f"https://{MOODLE_HOST}/course/view.php?id=21443",
    },
}

COURSES = COURSES_CONFIG
TARGET_COURSES = COURSES_CONFIG
TARGET_COURSE_IDS = list(COURSES_CONFIG.keys())

def infer_course_code_and_folder(fullname: str, base_dir: Path = BASE_STUDY_DIR) -> Tuple[str, str]:
    """
    Infers (course_code, local_folder_name) from any Moodle course fullname such as
    'ECE322: Electronic Circuits-2026FALL-LECT-02' or 'ECE291: Practical Training 1-2026FALL-PRAC-01'.
    Matches case-insensitively against existing directories in base_dir.
    """
    raw = (fullname or "").strip()
    m = re.match(r"^([A-Za-z]{2,6}\s*\d{3}[A-Za-z]?)\s*[:\-]\s*(.+)$", raw)
    if m:
        code = re.sub(r"\s+", "", m.group(1)).upper()
        rest = m.group(2).strip()
    else:
        code = raw.split()[0].upper() if raw else "COURSE"
        rest = raw

    # Strip trailing semester/section suffixes like -2026FALL-LECT-02, -2026SPRING-LAB-01A, etc.
    clean_title = re.sub(r"\s*-\s*20\d{2}[A-Za-z0-9\-_]+$", "", rest).strip()
    if not clean_title:
        clean_title = rest

    # Check if a matching folder (case-insensitive) already exists in base_dir
    try:
        if base_dir.exists():
            for child in base_dir.iterdir():
                if child.is_dir() and child.name.lower() == clean_title.lower():
                    return code, child.name
    except Exception:
        pass

    return code, clean_title


def register_dynamic_course(
    course_id: int,
    fullname: str,
    local_folder: str = "",
    base_dir: Path = BASE_STUDY_DIR,
) -> Dict[str, Any]:
    """Registers or updates a dynamic Moodle course in COURSES_CONFIG."""
    cid = int(course_id)
    if cid in COURSES_CONFIG and not local_folder:
        return COURSES_CONFIG[cid]

    code, inferred_folder = infer_course_code_and_folder(fullname, base_dir=base_dir)
    folder = local_folder.strip() if local_folder else inferred_folder
    sheet_prefix = "Tutorial" if "353" in code else "Sheet"
    entry = {
        "code": code,
        "name": fullname.split(":", 1)[-1].strip() if ":" in fullname else fullname,
        "fullname": fullname,
        "folder": folder,
        "local_dir": folder,
        "moodle_id": cid,
        "sheet_prefix": sheet_prefix,
        "url": f"https://{MOODLE_HOST}/course/view.php?id={cid}",
    }
    COURSES_CONFIG[cid] = entry
    return entry


def normalize_course_id(val: Union[int, str]) -> int:
    """Normalizes course ID or course code to target integer course ID."""
    if isinstance(val, int):
        if val in COURSES_CONFIG:
            return val
        if val > 0:
            register_dynamic_course(val, f"Course {val}")
            return val
    val_str = str(val).strip()
    if val_str.isdigit():
        cid = int(val_str)
        if cid not in COURSES_CONFIG and cid > 0:
            register_dynamic_course(cid, f"Course {cid}")
        if cid in COURSES_CONFIG:
            return cid
    val_upper = val_str.upper()
    for cid, cfg in COURSES_CONFIG.items():
        if cfg["code"].upper() == val_upper:
            return cid
    raise ValueError(f"Unknown course identifier: {val}")

def get_course_config(course_id: Union[int, str]) -> Dict[str, Any]:
    """Retrieve course configuration by ID or code."""
    cid = normalize_course_id(course_id)
    return COURSES_CONFIG[cid]

def get_course_dir(course_id: Union[int, str], base_dir: Path = BASE_STUDY_DIR) -> Path:
    """Return absolute local directory Path for given course ID or code."""
    cfg = get_course_config(course_id)
    return base_dir / cfg["folder"]

# --- Subfolder Taxonomy ---
SUBFOLDER_LECTURES = "Lectures"
SUBFOLDER_TUTORIALS = "Tutorials & Sheets"
SUBFOLDER_LABS = "Labs"
SUBFOLDER_ASSIGNMENTS = "Assignments"
SUBFOLDER_PROJECT = "Project"
SUBFOLDER_QUIZZES = "Quizzes"
SUBFOLDER_EXAMS = "Exams"
SUBFOLDER_GENERAL = "General"
SUBFOLDER_MODEL_ANSWERS = "Model Answers"

STANDARD_SUBFOLDERS = [
    SUBFOLDER_LECTURES,
    SUBFOLDER_TUTORIALS,
    SUBFOLDER_LABS,
    SUBFOLDER_ASSIGNMENTS,
    SUBFOLDER_GENERAL,
    SUBFOLDER_QUIZZES,
    SUBFOLDER_MODEL_ANSWERS,
    SUBFOLDER_EXAMS,
    SUBFOLDER_PROJECT,
]

def resolve_destination_subfolder(
    course_id_or_code: Union[int, str],
    section_title: str,
    activity_title: str,
    filename: str = ""
) -> str:
    """
    Classifies a resource into its canonical course subfolder based on section,
    activity title, and filename, adhering to course-specific conventions.
    """
    sec = (section_title or "").strip()
    act = (activity_title or "").strip()
    fn = (filename or "").strip()
    combined = f"{sec} {act} {fn}".lower()
    cleaned = re.sub(r"[_\-.]+", " ", combined)

    # Determine sheet prefix for the course
    sheet_prefix = "Sheet"
    course_code = ""
    try:
        cfg = get_course_config(course_id_or_code)
        sheet_prefix = cfg.get("sheet_prefix", "Sheet")
        course_code = cfg.get("code", "")
    except Exception:
        if isinstance(course_id_or_code, str):
            course_code = course_id_or_code.upper()
            if "353" in course_code:
                sheet_prefix = "Tutorial"

    sec_lower = sec.lower()
    act_lower = act.lower()

    # 1. Special section: Digital Image Processing in Arabic (ECE354)
    if "معالجة الصور بالعربي" in sec or "digital image processing" in sec_lower:
        return "Digital Image Processing - معالجة الصور بالعربي"

    # 2. Exams & Midterms
    if any(k in cleaned for k in ["midterm", "final exam", "exam room", "attendance sheet", "exams"]):
        return SUBFOLDER_EXAMS
    if re.search(r"\b(?:exam|exams|midterm|final)\b", cleaned):
        return SUBFOLDER_EXAMS

    # 3. Project
    if any(k in cleaned for k in ["project", "capstone", "milestone"]):
        return SUBFOLDER_PROJECT

    # 4. Model Answers & Quizzes
    if "model answer" in sec_lower or "model answer" in act_lower or "solution manual" in cleaned:
        q_match = re.search(r'quiz\s*0?(\d+)', cleaned)
        if q_match:
            return f"{SUBFOLDER_QUIZZES}/Quiz {q_match.group(1)}"
        return SUBFOLDER_MODEL_ANSWERS
    if re.search(r'\bquiz(?:zes)?\b', cleaned):
        q_match = re.search(r'quiz\s*0?(\d+)', cleaned)
        if q_match:
            return f"{SUBFOLDER_QUIZZES}/Quiz {q_match.group(1)}"
        return SUBFOLDER_QUIZZES

    # 5. Textbooks & CheatSheets
    if "cheatsheet" in cleaned or "cheat sheet" in cleaned:
        return "CheatSheets"
    if "textbook" in cleaned or ("book" in sec_lower and course_code == "ECE342"):
        return "Textbooks"

    # 6. Lectures
    if re.search(r"\b(?:lecture|lec|slides|presentation)\b", cleaned) or "ch0" in cleaned or "ch1" in cleaned:
        return SUBFOLDER_LECTURES

    # 7. Labs
    if re.search(r"\b(?:lab|labs|experiment|practical|qtspim|proteus)\b", cleaned):
        return SUBFOLDER_LABS

    # 8. Tutorials & Sheets
    if re.search(r"\b(?:sheet|tutorial|tut|problem set)\b", cleaned):
        num_match = re.search(r'(?:sheet|tutorial|tut)\s*0?(\d+)', cleaned)
        if num_match:
            num = int(num_match.group(1))
            return f"{SUBFOLDER_TUTORIALS}/{sheet_prefix} {num:02d}"
        return SUBFOLDER_TUTORIALS

    # 9. Assignments
    if re.search(r"\b(?:assign|assignment|assignments|homework|hw|task)\b", cleaned):
        return SUBFOLDER_ASSIGNMENTS

    # 10. Fallback to General
    return SUBFOLDER_GENERAL

def resolve_subfolder(
    course_code_or_id: Union[int, str],
    section_title: str,
    activity_title: str,
    filename: str = ""
) -> str:
    """Alias for resolve_destination_subfolder for compatibility."""
    return resolve_destination_subfolder(course_code_or_id, section_title, activity_title, filename)
