#!/usr/bin/env python3
"""
moodle_app.py
Interactive Web Dashboard for Nile University Moodle Course Discovery,
Expandable Course Content Explorer (Lectures, Sheets, Labs, Videos, Local Files),
Hidden/Past Course Separation, and Selective Synchronization.
"""

import argparse
import hashlib
import importlib
import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


def _ensure_runtime_packages() -> None:
    """Automatically installs any missing required libraries on first run when not frozen."""
    if getattr(sys, "frozen", False):
        return
    required = {
        "requests": "requests",
        "urllib3": "urllib3",
        "bs4": "beautifulsoup4",
        "playwright": "playwright",
        "yt_dlp": "yt-dlp",
    }
    missing = []
    for mod_name, pip_name in required.items():
        try:
            importlib.import_module(mod_name)
        except ImportError:
            missing.append(pip_name)
    if missing:
        print(f"[Auto-Setup] Installing required libraries ({', '.join(missing)})...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", *missing])
        except subprocess.CalledProcessError:
            subprocess.check_call([
                sys.executable, "-m", "pip", "install", "--quiet", "--user", "--break-system-packages", *missing
            ])


_ensure_runtime_packages()

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from moodle_sync import config
from moodle_sync.archive import extract_and_organize, is_zip_archive
from moodle_sync.auth import (
    AuthenticationError,
    classify_auth_error,
    clear_session_cache,
    create_requests_session,
    load_session_cache,
    login,
    patch_dns,
    validate_session,
)
from moodle_sync.deduplication import (
    DeduplicationEngine,
    LocalDiskIndex,
    match_resource,
)
from moodle_sync.media import MediaResult, resolve_and_download, sanitize_filename
from moodle_sync.scraper import (
    CourseData,
    MoodleItem,
    discover_enrolled_courses,
    scan_course,
)

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(asctime)s - %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("moodle_app")


# ==============================================================================
# CLASSIFICATION HELPERS (CATEGORY & MEDIA TYPE)
# ==============================================================================

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v", ".wmv", ".flv"}
DOC_EXTENSIONS = {".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls", ".txt", ".md", ".rtf", ".csv", ".html"}
CODE_EXTENSIONS = {".py", ".asm", ".m", ".c", ".cpp", ".h", ".java", ".js", ".ts", ".vhd", ".v", ".ino", ".slx", ".mdl"}
ARCHIVE_EXTENSIONS = {".zip", ".rar", ".7z", ".tar", ".gz"}


def classify_content_category(target_subfolder: str) -> str:
    """Maps a target_subfolder or relative local path into one of 7 clean UI categories."""
    sf = (target_subfolder or "").lower()
    if "lecture" in sf or "digital image processing" in sf or "textbook" in sf or "cheatsheet" in sf:
        return "Lectures"
    if "tutorial" in sf or "sheet" in sf:
        return "Tutorials & Sheets"
    if "lab" in sf:
        return "Labs"
    if "assign" in sf:
        return "Assignments"
    if "quiz" in sf or "exam" in sf or "model answer" in sf:
        return "Quizzes & Exams"
    if "project" in sf:
        return "Project"
    return "General"


def classify_file_extension_type(filename: str, url: str = "", is_external: bool = False, resource_type: str = "") -> str:
    fn = (filename or "").lower()
    u = (url or "").lower()
    ext = os.path.splitext(fn)[1].lower()
    combined = f"{fn} {u}"

    if (
        ext in VIDEO_EXTENSIONS
        or any(k in combined for k in ["recording", "youtu.be", "youtube.com", "panopto", "/:v:/", "stream.aspx", ".mp4"])
    ):
        return "Videos & Recordings"
    if ext in ARCHIVE_EXTENSIONS or "/:f:/" in u:
        return "ZIP Archives"
    if ext in CODE_EXTENSIONS:
        return "Code & Scripts"
    if ext == ".url" or (is_external and ext not in DOC_EXTENSIONS and ".pdf" not in u):
        return "External Links"
    return "Documents & Slides"


def classify_media_type(item: MoodleItem) -> str:
    return classify_file_extension_type(
        filename=item.filename or item.name or "",
        url=item.url or "",
        is_external=bool(item.is_external or str(item.resource_type) == "mod_url"),
        resource_type=str(item.resource_type),
    )


# ==============================================================================
# APPLICATION STATE
# ==============================================================================

class AppState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.session = None
        self.current_user: str = config.DEFAULT_USERNAME
        self.verified_cred_hashes: Set[str] = set()
        self.workspace: Path = config.BASE_STUDY_DIR
        self.enrolled_courses: List[Dict[str, Any]] = []
        self.scanned_courses: Dict[int, CourseData] = {}
        self.scanned_items_map: Dict[str, Tuple[int, MoodleItem, Dict[str, Any]]] = {}

        # Live sync job tracking
        self.sync_running: bool = False
        self.sync_total: int = 0
        self.sync_completed: int = 0
        self.sync_current_title: str = ""
        self.sync_bytes: int = 0
        self.sync_logs: List[str] = []
        self.sync_results: List[Dict[str, Any]] = []
        self.sync_error: Optional[str] = None

    def log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        logger.info(msg)
        with self.lock:
            self.sync_logs.append(line)
            if len(self.sync_logs) > 250:
                self.sync_logs = self.sync_logs[-250:]


STATE = AppState()


def load_manifest(workspace: Path) -> Dict[str, str]:
    mpath = workspace / ".moodle_sync_state.json"
    if mpath.is_file():
        try:
            data = json.loads(mpath.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except Exception:
            pass
    return {}


def save_manifest(workspace: Path, manifest: Dict[str, str]) -> None:
    mpath = workspace / ".moodle_sync_state.json"
    try:
        tmp = mpath.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, mpath)
    except Exception as e:
        logger.warning(f"Failed to save state manifest: {e}")


# ==============================================================================
# COURSE SCANNING + LOCAL FOLDER INVENTORY BUILDER
# ==============================================================================

def build_course_payload(
    cid: int,
    cdata: CourseData,
    course_meta: Dict[str, Any],
    workspace: Path,
    local_index: LocalDiskIndex,
    manifest: Dict[str, str],
) -> Dict[str, Any]:
    """
    Combines Moodle items for a course (marked as 'Will Be Downloaded' vs 'Already in Folder')
    PLUS any extra local files inside E:\\Study\\<course_folder> so the user can inspect
    100% of what will be downloaded and what is already in their folder.
    """
    course_dir = workspace / cdata.local_folder
    items_out: List[Dict[str, Any]] = []
    matched_local_paths: Set[str] = set()
    c_missing = 0
    c_existing = 0

    from moodle_sync.media import (
        is_sharepoint_folder_url,
        expand_sharepoint_folder_items,
        _find_existing_by_size_or_question,
    )
    from moodle_sync.scraper import MoodleItem

    expanded_moodle_items: List[MoodleItem] = []
    for raw_item in cdata.all_items:
        base_subfolder = config.resolve_destination_subfolder(
            cdata.course_code, raw_item.section_title, raw_item.name, raw_item.filename
        )
        if is_sharepoint_folder_url(raw_item.url):
            sp_children = expand_sharepoint_folder_items(raw_item.url, STATE.session)
            if sp_children:
                # Determine sheet subfolder (e.g., Tutorials & Sheets/Sheet 02)
                sheet_m = re.search(r"sheet\s*0*(\d+)", raw_item.name, re.IGNORECASE)
                sub_folder = f"{base_subfolder}/Sheet {int(sheet_m.group(1)):02d}" if sheet_m else base_subfolder
                for idx_sp, sp_child in enumerate(sp_children, 1):
                    child_fn = sp_child["filename"]
                    expanded_moodle_items.append(
                        MoodleItem(
                            id=f"{raw_item.id}_sp_{idx_sp}",
                            name=sp_child["title"],
                            url=sp_child["url"],
                            resource_type="mod_url",
                            course_id=cid,
                            section_id=raw_item.section_id,
                            section_title=f"{raw_item.section_title} ({raw_item.name})",
                            target_subfolder=sub_folder,
                            filename=child_fn,
                            filesize=sp_child.get("size_bytes") or 0,
                            is_external=True,
                            metadata={"origin": sp_child.get("origin", "SharePoint Folder")},
                        )
                    )
                continue
        raw_item.target_subfolder = base_subfolder
        expanded_moodle_items.append(raw_item)

    for item in expanded_moodle_items:
        target_subfolder = item.target_subfolder or config.resolve_destination_subfolder(
            cdata.course_code, item.section_title, item.name, item.filename
        )
        item.target_subfolder = target_subfolder
        target_dest_dir = course_dir / target_subfolder

        unique_key = f"{cid}::{item.id}"
        prev_path = manifest.get(str(item.id)) or manifest.get(unique_key)
        prev_valid = False
        if prev_path and Path(prev_path).exists():
            try:
                prev_valid = Path(prev_path).resolve().is_relative_to(course_dir.resolve())
            except Exception:
                prev_valid = False

        if prev_valid:
            is_on_disk = True
            mp_obj = Path(prev_path).resolve()
            matched_path = str(mp_obj)
            matched_local_paths.add(os.path.normcase(matched_path))
            match_reason = f"Already in folder ({mp_obj.name})"
            size_bytes = mp_obj.stat().st_size if mp_obj.is_file() else (item.filesize or 0)
        else:
            # Also check exact size / question video alias in target_dest_dir (e.g. Sheet 2_Q2.mp4 <-> 2.mp4)
            q_existing = _find_existing_by_size_or_question(
                target_dest_dir, item.filename or item.name, item.filesize or 0
            )
            if q_existing is not None and q_existing.exists():
                is_on_disk = True
                mp_obj = q_existing.resolve()
                matched_path = str(mp_obj)
                matched_local_paths.add(os.path.normcase(matched_path))
                size_bytes = mp_obj.stat().st_size
                match_reason = f"Matched existing file ({mp_obj.name})"
            else:
                mres = match_resource(item, target_dest_dir, local_index)
                is_on_disk = bool(mres.is_match and mres.action == "SKIP")
                mp = getattr(mres.matched_file, "path", mres.matched_file) if is_on_disk else None
                if mp and Path(mp).exists():
                    mp_obj = Path(mp).resolve()
                    matched_path = str(mp_obj)
                    matched_local_paths.add(os.path.normcase(matched_path))
                    size_bytes = mp_obj.stat().st_size if mp_obj.is_file() else (mres.size_bytes or item.filesize or 0)
                else:
                    matched_path = ""
                    size_bytes = mres.size_bytes or (item.filesize or 0)
                match_reason = mres.reason if is_on_disk else (
                    item.metadata.get("origin") or "Will be downloaded from Moodle"
                )

        cat = classify_content_category(target_subfolder)
        mtype = classify_media_type(item)

        if is_on_disk:
            c_existing += 1
        else:
            c_missing += 1

        rel_local = ""
        if matched_path:
            try:
                rel_local = str(Path(matched_path).relative_to(course_dir)).replace("\\", "/")
            except Exception:
                rel_local = Path(matched_path).name

        item_meta = {
            "key": unique_key,
            "resource_id": item.id,
            "course_id": cid,
            "course_code": cdata.course_code,
            "title": item.name,
            "filename": Path(matched_path).name if matched_path else (item.filename or item.name),
            "section": item.section_title,
            "activity_type": str(item.resource_type),
            "category": cat,
            "media_type": mtype,
            "target_subfolder": target_subfolder.replace("\\", "/"),
            "local_rel_path": rel_local or f"{target_subfolder}/{item.filename or item.name}".replace("\\", "/"),
            "url": item.url,
            "size_bytes": size_bytes or 0,
            "on_disk": is_on_disk,
            "source": "MOODLE_SYNCED" if is_on_disk else "MOODLE_MISSING",
            "matched_path": matched_path,
            "reason": match_reason,
        }
        STATE.scanned_items_map[unique_key] = (cid, item, item_meta)
        items_out.append(item_meta)

    # Also include extra local files in course_dir that weren't already listed above
    local_extra_count = 0
    if course_dir.exists() and course_dir.is_dir():
        for dp, _, filenames in os.walk(course_dir):
            for fn in sorted(filenames):
                if fn.startswith(("~$", "._")) or fn in config.IGNORED_FILENAMES or fn.endswith(".tmp"):
                    continue
                full_p = Path(dp) / fn
                try:
                    resolved_str = str(full_p.resolve())
                except Exception:
                    resolved_str = str(full_p)
                if os.path.normcase(resolved_str) in matched_local_paths:
                    continue
                try:
                    rel_dir = str(Path(dp).relative_to(course_dir)).replace("\\", "/")
                    if rel_dir == ".":
                        rel_dir = "Root"
                    rel_file = str(full_p.relative_to(course_dir)).replace("\\", "/")
                    sz = full_p.stat().st_size
                except Exception:
                    rel_dir = "General"
                    rel_file = fn
                    sz = 0

                cat = classify_content_category(rel_dir)
                mtype = classify_file_extension_type(fn)
                local_extra_count += 1
                items_out.append({
                    "key": f"{cid}::local::{rel_file}",
                    "resource_id": f"local_{local_extra_count}",
                    "course_id": cid,
                    "course_code": cdata.course_code,
                    "title": fn,
                    "filename": fn,
                    "section": "Local Study Folder",
                    "activity_type": "local_file",
                    "category": cat,
                    "media_type": mtype,
                    "target_subfolder": rel_dir,
                    "local_rel_path": rel_file,
                    "url": "",
                    "size_bytes": sz,
                    "on_disk": True,
                    "source": "LOCAL_ONLY",
                    "matched_path": resolved_str,
                    "reason": "Already in your local Study folder",
                })

                # If this local file is a ZIP archive containing embedded .url video shortcuts (like Sheet 2 Solution.zip),
                # surface each embedded .url video link so the scanner & downloader can resolve them!
                if fn.lower().endswith(".zip"):
                    from moodle_sync.archive import scan_zip_for_embedded_urls
                    from moodle_sync.media import _find_existing_by_size_or_question
                    from moodle_sync.scraper import MoodleItem
                    emb_list = scan_zip_for_embedded_urls(full_p, Path(dp))
                    for emb in emb_list:
                        base_emb_url = emb.url.split("&nav=")[0]
                        if any(it.get("url", "").split("&nav=")[0] == base_emb_url for it in items_out):
                            continue
                        existing_vid = _find_existing_by_size_or_question(Path(dp), f"{emb.stem}.mp4", 0)
                        vid_on_disk = existing_vid is not None
                        if vid_on_disk:
                            c_existing += 1
                            vid_rel = str(existing_vid.relative_to(course_dir)).replace("\\", "/")
                            vid_sz = existing_vid.stat().st_size
                            vid_reason = f"Resolved from {fn} -> {existing_vid.name}"
                        else:
                            c_missing += 1
                            vid_rel = f"{rel_dir}/{emb.stem}.mp4".replace("\\", "/")
                            vid_sz = 0
                            vid_reason = f"Video link inside {fn} ({emb.entry_name}) — ready to download"

                        emb_key = f"{cid}::zipurl::{rel_file}::{emb.stem}"
                        synthetic_item = MoodleItem(
                            id=f"zipurl_{emb.stem}",
                            name=f"{emb.stem} (from {fn})",
                            url=emb.url,
                            resource_type="mod_url",
                            course_id=cid,
                            section_id="zip_embedded",
                            section_title=rel_dir,
                            target_subfolder=rel_dir,
                            filename=f"{emb.stem}.mp4",
                            is_external=True,
                        )
                        emb_meta = {
                            "key": emb_key,
                            "resource_id": synthetic_item.id,
                            "course_id": cid,
                            "course_code": cdata.course_code,
                            "title": f"{emb.stem} (Video Link inside {fn})",
                            "filename": existing_vid.name if existing_vid else f"{emb.stem}.mp4",
                            "section": f"ZIP Embedded Link ({fn})",
                            "activity_type": "zip_embedded_url",
                            "category": cat,
                            "media_type": "Videos & Recordings",
                            "target_subfolder": rel_dir,
                            "local_rel_path": vid_rel,
                            "url": emb.url,
                            "size_bytes": vid_sz,
                            "on_disk": vid_on_disk,
                            "source": "MOODLE_SYNCED" if vid_on_disk else "MOODLE_MISSING",
                            "matched_path": str(existing_vid) if existing_vid else "",
                            "reason": vid_reason,
                        }
                        STATE.scanned_items_map[emb_key] = (cid, synthetic_item, emb_meta)
                        items_out.append(emb_meta)

    # Compute per-category counts for this course
    cat_breakdown: Dict[str, Dict[str, int]] = {}
    for it in items_out:
        c = it["category"]
        if c not in cat_breakdown:
            cat_breakdown[c] = {"total": 0, "missing": 0, "on_disk": 0}
        cat_breakdown[c]["total"] += 1
        if it["on_disk"]:
            cat_breakdown[c]["on_disk"] += 1
        else:
            cat_breakdown[c]["missing"] += 1

    moodle_count = len(expanded_moodle_items)
    is_past = bool(course_meta.get("is_past", False))
    is_empty = (moodle_count == 0)

    return {
        "course_id": cid,
        "code": cdata.course_code,
        "fullname": course_meta.get("fullname") or f"{cdata.course_code}: {cdata.course_name}",
        "name": cdata.course_name,
        "section_kind": course_meta.get("section_kind", "LECT"),
        "term": course_meta.get("term", "2026FALL"),
        "is_past": is_past,
        "is_empty": is_empty,
        "tab_group": "HIDDEN" if (is_past or is_empty) else "ACTIVE",
        "hidden_reason": (
            f"Past Course ({course_meta.get('term', 'Archive')})"
            if is_past
            else ("Empty Course Shell (0 items on Moodle)" if is_empty else "")
        ),
        "scanned": True,
        "local_folder": cdata.local_folder,
        "local_path": str(course_dir),
        "moodle_items_count": moodle_count,
        "local_extra_count": local_extra_count,
        "total_items": len(items_out),
        "missing_count": c_missing,
        "existing_count": c_existing + local_extra_count,
        "category_breakdown": cat_breakdown,
        "items": items_out,
    }


def _credential_hash(email: str, password: str) -> str:
    return hashlib.sha256(f"{email.strip().lower()}:{password}".encode("utf-8")).hexdigest()


def handle_login_and_initial_scan(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Authenticates with Moodle, discovers all enrolled courses (current + past/hidden),
    automatically scans all current-semester courses in parallel so the user immediately sees:
      - Active Courses Tab: Courses with content (expandable by Lec, Sheets, Labs, etc.)
      - Hidden Tab: Current courses with 0 content + Past semester courses
    """
    raw_email = (payload.get("email") or "").strip()
    raw_password = payload.get("password") or ""
    cached = load_session_cache()
    cached_email = (cached.get("account_email") or "").strip() if isinstance(cached, dict) else ""

    email = raw_email or config.DEFAULT_USERNAME
    password = raw_password or config.DEFAULT_PASSWORD
    workspace_str = (payload.get("workspace") or "").strip() or str(config.BASE_STUDY_DIR)
    force_fresh = bool(payload.get("force_fresh", False))

    workspace = Path(workspace_str)
    if not workspace.is_absolute():
        workspace = (config.BASE_STUDY_DIR.parent / workspace).resolve()
    STATE.workspace = workspace
    patch_dns()

    switching_user = bool(email and STATE.current_user and email.lower() != STATE.current_user.lower())
    if switching_user:
        force_fresh = True

    cred_hash = ""
    if raw_password:
        if not email:
            if cached_email:
                email = cached_email
            else:
                raise AuthenticationError(
                    "Please enter your Nile University email address in Step 1.",
                    code="MISSING_CREDENTIALS",
                    field="email",
                )
        cred_hash = _credential_hash(email, raw_password)
        with STATE.lock:
            already_verified = cred_hash in STATE.verified_cred_hashes
        if not already_verified:
            # Always verify a newly typed password against Microsoft SSO so wrong passwords
            # never silently succeed via an earlier .moodle_session.json cookie cache.
            force_fresh = True
    elif email and password:
        cred_hash = _credential_hash(email, password)

    used_cache = False
    if not force_fresh:
        same_cached_user = (not email) or (not cached_email) or (email.lower() == cached_email.lower())
        if not same_cached_user:
            force_fresh = True
        has_valid_cache = False
        if same_cached_user and isinstance(cached, dict) and isinstance(cached.get("moodle"), dict):
            try:
                test_s = create_requests_session(cached)
                has_valid_cache = validate_session(test_s)
            except Exception:
                has_valid_cache = False
        if has_valid_cache:
            used_cache = True
            if not email and cached_email:
                email = cached_email
        else:
            if not email and not password:
                raise AuthenticationError(
                    "Please enter your Nile University email and password in Step 1.",
                    code="MISSING_CREDENTIALS",
                    field="email",
                )
            if not email:
                raise AuthenticationError(
                    "Please enter your Nile University email address in Step 1.",
                    code="MISSING_CREDENTIALS",
                    field="email",
                )
            if not password:
                msg = (
                    f"Please enter the password for {email} in Step 1."
                    if (switching_user or (cached_email and email.lower() != cached_email.lower()))
                    else "Your session expired or is not active yet. Please enter your Moodle / Microsoft password in Step 1."
                )
                raise AuthenticationError(msg, code="MISSING_CREDENTIALS", field="password")
    elif not email or not password:
        raise AuthenticationError(
            "Please enter both your Nile University email and password in Step 1.",
            code="MISSING_CREDENTIALS",
            field="email" if not email else "password",
        )

    STATE.log(f"Authenticating with Moodle as {email or 'cached session'} (force_fresh={force_fresh})...")
    session = login(
        username=email,
        password=password,
        headless=True,
        force_fresh=force_fresh,
    )
    effective_email = (
        email
        or cached_email
        or (getattr(session, "auth_cookies", {}) or {}).get("account_email", "")
    )
    with STATE.lock:
        STATE.session = session
        if effective_email:
            STATE.current_user = effective_email
        if effective_email and password:
            STATE.verified_cred_hashes.add(_credential_hash(effective_email, password))

    STATE.log("Discovering all enrolled & past courses on Moodle...")
    all_enrolled = discover_enrolled_courses(session, base_dir=workspace, include_hidden_and_past=True)
    STATE.enrolled_courses = all_enrolled

    # Separate current courses vs past courses
    current_courses = [c for c in all_enrolled if not c.get("is_past")]
    past_courses = [c for c in all_enrolled if c.get("is_past")]

    # Scan all current courses in parallel right away so we know which ones have content vs 0 content
    manifest = load_manifest(workspace)
    local_index = LocalDiskIndex(root_dir=workspace)
    current_configs = {
        c["course_id"]: config.COURSES_CONFIG[c["course_id"]]
        for c in current_courses
        if c["course_id"] in config.COURSES_CONFIG
    }
    local_index.scan(workspace, current_configs)

    STATE.scanned_courses.clear()
    STATE.scanned_items_map.clear()

    scanned_by_id: Dict[int, CourseData] = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = {pool.submit(scan_course, session, c["course_id"]): c["course_id"] for c in current_courses}
        for fut in as_completed(futs):
            cid = futs[fut]
            try:
                scanned_by_id[cid] = fut.result()
            except Exception as e:
                logger.warning(f"Failed to scan course {cid}: {e}")

    courses_out: List[Dict[str, Any]] = []
    for cmeta in current_courses:
        cid = cmeta["course_id"]
        cdata = scanned_by_id.get(cid)
        if cdata:
            STATE.scanned_courses[cid] = cdata
            cpayload = build_course_payload(cid, cdata, cmeta, workspace, local_index, manifest)
            courses_out.append(cpayload)

    # Add past courses into the Hidden tab (can be scanned on demand with 1 click)
    for cmeta in past_courses:
        cid = cmeta["course_id"]
        courses_out.append({
            "course_id": cid,
            "code": cmeta["code"],
            "fullname": cmeta["fullname"],
            "name": cmeta["name"],
            "section_kind": cmeta.get("section_kind", "LECT"),
            "term": cmeta.get("term", "PAST"),
            "is_past": True,
            "is_empty": False,
            "tab_group": "HIDDEN",
            "hidden_reason": f"Past Semester ({cmeta.get('term', 'Archive')})",
            "scanned": False,
            "local_folder": cmeta["local_folder"],
            "local_path": str(workspace / cmeta["local_folder"]),
            "moodle_items_count": 0,
            "local_extra_count": 0,
            "total_items": 0,
            "missing_count": 0,
            "existing_count": 0,
            "category_breakdown": {},
            "items": [],
        })

    return _build_full_response(effective_email, workspace, courses_out, used_cache=used_cache)


def get_os_roots() -> List[Dict[str, str]]:
    """
    Discovers all available drives and common folders across Windows, macOS, and Linux
    so the user can select any folder from a table without typing OS-specific paths.
    """
    roots: List[Dict[str, str]] = []
    if os.name == "nt":
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            drive = f"{letter}:\\"
            if os.path.exists(drive):
                roots.append({"label": f"💾 Drive {drive}", "path": drive})
        if Path(r"E:\Study").exists():
            roots.append({"label": "⭐ E:\\Study", "path": r"E:\Study"})
    else:
        roots.append({"label": "📁 Root (/)", "path": "/"})
        for mount_dir in ("/Volumes", "/mnt", "/media"):
            if os.path.isdir(mount_dir):
                roots.append({"label": f"💾 {mount_dir}", "path": mount_dir})

    home = Path.home()
    if home.exists():
        roots.append({"label": "🏠 Home", "path": str(home)})
    for sub_name, icon in [("Desktop", "🖥️"), ("Documents", "📄"), ("Downloads", "⬇️")]:
        sub_p = home / sub_name
        if sub_p.exists() and sub_p.is_dir():
            roots.append({"label": f"{icon} {sub_name}", "path": str(sub_p)})
    return roots


def handle_browse_dirs(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Returns a table of folders inside `path` plus quick-jump OS roots."""
    req_path_str = (payload.get("path") or "").strip()
    if not req_path_str:
        target = STATE.workspace if STATE.workspace.exists() else Path.home()
    else:
        target = Path(req_path_str)
        if not target.is_absolute():
            target = (config.BASE_STUDY_DIR.parent / target).resolve()

    while not target.exists() and target.parent != target:
        target = target.parent
    if not target.exists():
        target = Path.home()

    try:
        target = target.resolve()
    except Exception:
        pass

    parent_path = str(target.parent) if target.parent != target else ""
    folders_out: List[Dict[str, Any]] = []
    skip_System = {"$recycle.bin", "system volume information", "recovery", "perflogs"}

    try:
        with os.scandir(target) as it:
            entries = [
                e for e in it
                if e.is_dir(follow_symlinks=False)
                and not e.name.startswith(".")
                and e.name.lower() not in skip_System
            ]
        entries.sort(key=lambda x: x.name.lower())

        for entry in entries:
            sub_dirs = 0
            sub_files = 0
            try:
                with os.scandir(entry.path) as sub_it:
                    for s in sub_it:
                        if s.is_dir(follow_symlinks=False):
                            sub_dirs += 1
                        elif s.is_file(follow_symlinks=False):
                            sub_files += 1
            except Exception:
                pass

            try:
                mtime = datetime.fromtimestamp(entry.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            except Exception:
                mtime = "—"

            folders_out.append({
                "name": entry.name,
                "path": str(Path(entry.path)),
                "subfolders_count": sub_dirs,
                "files_count": sub_files,
                "modified": mtime,
            })
    except PermissionError:
        pass
    except Exception as e:
        logger.warning(f"Error browsing directory {target}: {e}")

    return {
        "ok": True,
        "os_name": "Windows" if os.name == "nt" else ("macOS" if sys.platform == "darwin" else "Linux"),
        "current_path": str(target),
        "parent_path": parent_path,
        "roots": get_os_roots(),
        "folders": folders_out,
    }


def handle_logout(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Wipes in-memory credentials, active session cookies, and deletes .moodle_session.json from disk."""
    with STATE.lock:
        if STATE.session is not None:
            try:
                STATE.session._runtime_username = ""  # type: ignore
                STATE.session._runtime_password = ""  # type: ignore
                STATE.session.cookies.clear()
                STATE.session.close()
            except Exception:
                pass
        STATE.session = None
        STATE.current_user = ""
        STATE.verified_cred_hashes.clear()
        STATE.enrolled_courses.clear()
        STATE.scanned_courses.clear()
        STATE.scanned_items_map.clear()

    clear_session_cache()
    ws_cache = STATE.workspace / ".moodle_session.json"
    if ws_cache.exists():
        clear_session_cache(ws_cache)
    logger.info("User signed out: cleared in-memory credentials and deleted .moodle_session.json.")
    return {
        "ok": True,
        "message": "Signed out — email, password, and session cookies have been wiped from memory and disk.",
    }


def handle_create_dir(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Creates a new subfolder inside `parent_path` and returns the updated directory table."""
    parent_str = (payload.get("parent_path") or "").strip() or str(STATE.workspace)
    new_name = sanitize_filename((payload.get("new_folder_name") or "").strip())
    if not new_name:
        return {"ok": False, "error": "Please enter a valid folder name."}
    parent_p = Path(parent_str)
    new_dir = (parent_p / new_name).resolve()
    new_dir.mkdir(parents=True, exist_ok=True)
    res = handle_browse_dirs({"path": str(parent_p)})
    res["created_path"] = str(new_dir)
    return res


def handle_scan_specific(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Scans one or more specific courses (e.g. a past course in the Hidden tab or re-scanning after folder edit)."""
    if not STATE.session:
        STATE.session = login(headless=True)

    course_ids = [int(x) for x in payload.get("course_ids", [])]
    custom_folders: Dict[str, str] = payload.get("custom_folders") or {}
    workspace_str = (payload.get("workspace") or "").strip()
    if workspace_str:
        STATE.workspace = Path(workspace_str)

    workspace = STATE.workspace
    manifest = load_manifest(workspace)

    for cid in course_ids:
        custom_f = custom_folders.get(str(cid), "").strip()
        if cid in config.COURSES_CONFIG and custom_f:
            config.COURSES_CONFIG[cid]["folder"] = custom_f
            config.COURSES_CONFIG[cid]["local_dir"] = custom_f

    local_index = LocalDiskIndex(root_dir=workspace)
    selected_configs = {cid: config.COURSES_CONFIG[cid] for cid in course_ids if cid in config.COURSES_CONFIG}
    local_index.scan(workspace, selected_configs)

    enrolled_by_id = {c["course_id"]: c for c in STATE.enrolled_courses}
    scanned_results: List[Dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = {pool.submit(scan_course, STATE.session, cid): cid for cid in course_ids}
        for fut in as_completed(futs):
            cid = futs[fut]
            try:
                cdata = fut.result()
                STATE.scanned_courses[cid] = cdata
                cmeta = enrolled_by_id.get(cid, {
                    "course_id": cid,
                    "code": cdata.course_code,
                    "fullname": f"{cdata.course_code}: {cdata.course_name}",
                    "name": cdata.course_name,
                    "section_kind": "LECT",
                    "term": "UNKNOWN",
                    "is_past": False,
                    "local_folder": cdata.local_folder,
                })
                cpayload = build_course_payload(cid, cdata, cmeta, workspace, local_index, manifest)
                scanned_results.append(cpayload)
            except Exception as e:
                logger.error(f"Error scanning course {cid}: {e}")

    return {"ok": True, "scanned_courses": scanned_results}


def _build_full_response(
    email: str,
    workspace: Path,
    courses_out: List[Dict[str, Any]],
    used_cache: bool = False,
) -> Dict[str, Any]:
    active_courses = [c for c in courses_out if c["tab_group"] == "ACTIVE"]
    hidden_courses = [c for c in courses_out if c["tab_group"] == "HIDDEN"]

    category_counts: Dict[str, int] = {}
    media_type_counts: Dict[str, int] = {}
    total_moodle_items = 0
    total_missing = 0
    total_existing = 0

    for c in active_courses:
        total_moodle_items += c["moodle_items_count"]
        total_missing += c["missing_count"]
        total_existing += c["existing_count"]
        for it in c["items"]:
            cat = it["category"]
            mt = it["media_type"]
            category_counts[cat] = category_counts.get(cat, 0) + 1
            media_type_counts[mt] = media_type_counts.get(mt, 0) + 1

    display_user = email or "your Nile University account"
    login_msg = (
        f"Signed in successfully as {display_user}!"
        if not used_cache
        else f"Signed in successfully as {display_user} (using active session)!"
    )

    return {
        "ok": True,
        "email": email,
        "message": login_msg,
        "login_method": "cached_session" if used_cache else "microsoft_sso",
        "workspace": str(workspace),
        "summary": {
            "active_courses_count": len(active_courses),
            "hidden_courses_count": len(hidden_courses),
            "empty_current_count": sum(1 for c in hidden_courses if not c["is_past"]),
            "past_courses_count": sum(1 for c in hidden_courses if c["is_past"]),
            "total_moodle_items": total_moodle_items,
            "total_missing": total_missing,
            "total_existing": total_existing,
            "categories": category_counts,
            "media_types": media_type_counts,
        },
        "courses": courses_out,
    }


def _download_worker(selected_keys: List[str], extract_zips: bool) -> None:
    try:
        workspace = STATE.workspace
        session = STATE.session or login(headless=True)
        manifest = load_manifest(workspace)
        local_index = LocalDiskIndex(root_dir=workspace)
        local_index.scan(workspace, config.COURSES_CONFIG)

        STATE.log(f"Starting download of {len(selected_keys)} selected items into {workspace}...")

        for idx, key in enumerate(selected_keys, 1):
            entry = STATE.scanned_items_map.get(key)
            if not entry:
                continue
            cid, res, meta = entry
            cdata = STATE.scanned_courses.get(cid)
            course_folder = cdata.local_folder if cdata else config.COURSES_CONFIG[cid]["folder"]
            course_code = cdata.course_code if cdata else config.COURSES_CONFIG[cid]["code"]
            course_dir = workspace / course_folder
            target_subfolder = meta["target_subfolder"]
            target_dest_dir = course_dir / target_subfolder
            target_dest_dir.mkdir(parents=True, exist_ok=True)

            with STATE.lock:
                STATE.sync_completed = idx - 1
                STATE.sync_current_title = f"[{course_code}] {res.name}"

            # Safety guard: if already on disk, never delete, overwrite, or duplicate it
            existing_path = meta.get("matched_path") or manifest.get(str(res.id)) or manifest.get(key)
            if meta.get("on_disk") and existing_path and Path(existing_path).exists():
                STATE.log(f"({idx}/{len(selected_keys)}) SKIPPED (Already in Study folder): {existing_path}")
                with STATE.lock:
                    STATE.sync_results.append({
                        "key": key,
                        "course_code": course_code,
                        "title": res.name,
                        "category": meta["category"],
                        "media_type": meta["media_type"],
                        "status": "SKIPPED_EXISTING",
                        "saved_path": str(existing_path),
                        "bytes": 0,
                        "notes": "Already exists in Study folder — kept untouched.",
                    })
                continue

            STATE.log(f"({idx}/{len(selected_keys)}) Downloading [{course_code}] {res.name} -> {course_folder}/{target_subfolder}")

            try:
                if str(res.resource_type) == "mod_url" or res.is_external:
                    clean_suggested = sanitize_filename(Path(res.filename).name) if res.filename else None
                    media_res: MediaResult = resolve_and_download(
                        url=res.url,
                        dest_dir=target_dest_dir,
                        session=session,
                        title=res.name,
                        suggested_name=clean_suggested,
                    )
                    saved_path = str(media_res.file_path or "")
                    status = media_res.status
                    nbytes = media_res.bytes_downloaded
                    notes = media_res.message or "External link processed."
                    if saved_path and Path(saved_path).exists():
                        manifest[str(res.id)] = saved_path
                        manifest[key] = saved_path
                else:
                    with session.get(res.url, stream=True, allow_redirects=True, timeout=config.DEFAULT_TIMEOUT) as r:
                        r.raise_for_status()
                        from moodle_sync.scraper import CourseScraper
                        cd_fn = CourseScraper.extract_filename(r)
                        raw_fn = cd_fn or res.filename or res.name or f"resource_{res.id}"
                        clean_fn = sanitize_filename(Path(raw_fn).name)
                        if not Path(clean_fn).suffix and Path(res.filename or "").suffix:
                            clean_fn += Path(res.filename).suffix

                        safe_dest = DeduplicationEngine.get_safe_destination_path(target_dest_dir, clean_fn)
                        tmp_path = safe_dest.with_name(safe_dest.name + ".part.tmp")
                        nbytes = 0
                        with open(tmp_path, "wb") as f:
                            for chunk in r.iter_content(chunk_size=65536):
                                if chunk:
                                    f.write(chunk)
                                    nbytes += len(chunk)

                        if extract_zips and is_zip_archive(tmp_path):
                            zip_final = target_dest_dir / clean_fn
                            os.replace(tmp_path, zip_final)
                            ext_res = extract_and_organize(
                                zip_path=zip_final,
                                dest_dir=target_dest_dir,
                                course_code=course_code,
                                local_index=local_index,
                                session=session,
                            )
                            try:
                                zip_final.unlink(missing_ok=True)
                            except OSError:
                                pass
                            status = "EXTRACTED" if ext_res.success else "DOWNLOADED"
                            saved_path = str(ext_res.extracted_files[0]) if ext_res.extracted_files else str(target_dest_dir)
                            notes = f"Extracted {len(ext_res.extracted_files)} files (including any embedded .url videos)."
                        else:
                            os.replace(tmp_path, safe_dest)
                            status = "DOWNLOADED"
                            saved_path = str(safe_dest)
                            notes = "Downloaded cleanly."
                            manifest[str(res.id)] = saved_path
                            manifest[key] = saved_path

                meta["on_disk"] = True
                meta["matched_path"] = saved_path

                with STATE.lock:
                    STATE.sync_bytes += nbytes
                    STATE.sync_results.append({
                        "key": key,
                        "course_code": course_code,
                        "title": res.name,
                        "category": meta["category"],
                        "media_type": meta["media_type"],
                        "status": status,
                        "saved_path": saved_path,
                        "bytes": nbytes,
                        "notes": notes,
                    })
                STATE.log(f"  -> {status}: {Path(saved_path).name if saved_path else res.name} ({nbytes:,} B)")
            except Exception as item_err:
                STATE.log(f"  -> ERROR on {res.name}: {item_err}")
                with STATE.lock:
                    STATE.sync_results.append({
                        "key": key,
                        "course_code": course_code,
                        "title": res.name,
                        "category": meta["category"],
                        "media_type": meta["media_type"],
                        "status": "FAILED",
                        "saved_path": "",
                        "bytes": 0,
                        "notes": str(item_err),
                    })

            save_manifest(workspace, manifest)

        with STATE.lock:
            STATE.sync_completed = len(selected_keys)
            STATE.sync_current_title = "Completed!"
        STATE.log(f"Finished synchronizing {len(selected_keys)} items.")
    except Exception as e:
        STATE.sync_error = str(e)
        STATE.log(f"Sync worker fatal error: {e}")
    finally:
        with STATE.lock:
            STATE.sync_running = False


def handle_start_sync(payload: Dict[str, Any]) -> Dict[str, Any]:
    with STATE.lock:
        if STATE.sync_running:
            return {"ok": False, "error": "A synchronization job is already running."}
        selected_keys = list(payload.get("selected_keys") or [])
        extract_zips = bool(payload.get("extract_zips", True))
        if not selected_keys:
            return {"ok": False, "error": "No items selected to download."}

        STATE.sync_running = True
        STATE.sync_total = len(selected_keys)
        STATE.sync_completed = 0
        STATE.sync_current_title = "Initializing..."
        STATE.sync_bytes = 0
        STATE.sync_logs = []
        STATE.sync_results = []
        STATE.sync_error = None

    t = threading.Thread(target=_download_worker, args=(selected_keys, extract_zips), daemon=True)
    t.start()
    return {"ok": True, "started": len(selected_keys)}


# ==============================================================================
# EMBEDDED MODERN WEB DASHBOARD HTML
# ==============================================================================

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NU Moodle Sync Studio — Made by farisEmad</title>
<link rel="icon" type="image/png" href="/icon.png">
<style>
  :root {
    --bg: #0b0f19;
    --card: #131a2a;
    --card-hover: #182238;
    --border: #24324f;
    --text: #e6edf7;
    --muted: #8ea0c0;
    --accent: #3b82f6;
    --accent-hover: #2563eb;
    --success: #10b981;
    --warning: #f59e0b;
    --danger: #ef4444;
    --purple: #8b5cf6;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.5;
    padding-bottom: 70px;
  }
  header {
    background: linear-gradient(135deg, #111827 0%, #1e293b 100%);
    border-bottom: 1px solid var(--border);
    padding: 18px 32px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    position: sticky;
    top: 0;
    z-index: 50;
  }
  .brand { display: flex; align-items: center; gap: 14px; }
  .brand-icon {
    width: 42px; height: 42px; border-radius: 10px;
    background: linear-gradient(135deg, #3b82f6, #8b5cf6);
    display: flex; align-items: center; justify-content: center;
    font-weight: 800; font-size: 20px; color: #fff;
  }
  .brand h1 { font-size: 20px; font-weight: 700; }
  .brand p { font-size: 13px; color: var(--muted); }
  .status-pill {
    padding: 6px 14px; border-radius: 999px; font-size: 13px; font-weight: 600;
    background: rgba(16, 185, 129, 0.15); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.3);
    transition: all 0.2s ease;
  }
  .status-pill.status-error {
    background: rgba(239, 68, 68, 0.2); color: #fca5a5; border-color: rgba(239, 68, 68, 0.5);
  }
  .status-pill.status-loading {
    background: rgba(59, 130, 246, 0.2); color: #93c5fd; border-color: rgba(59, 130, 246, 0.5);
  }
  .container { max-width: 1320px; margin: 24px auto; padding: 0 24px; display: flex; flex-direction: column; gap: 22px; }
  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 22px;
    box-shadow: 0 10px 30px rgba(0,0,0,0.25);
  }
  .step-header {
    display: flex; justify-content: space-between; align-items: center;
    margin-bottom: 16px; padding-bottom: 14px; border-bottom: 1px solid var(--border);
    flex-wrap: wrap; gap: 12px;
  }
  .step-title { display: flex; align-items: center; gap: 12px; font-size: 17px; font-weight: 700; }
  .step-num {
    width: 28px; height: 28px; border-radius: 50%; background: var(--accent);
    color: #fff; display: inline-flex; align-items: center; justify-content: center;
    font-size: 14px; font-weight: 700;
  }
  .form-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 16px;
    align-items: end;
  }
  .field label { display: block; font-size: 12px; font-weight: 600; color: var(--muted); margin-bottom: 6px; text-transform: uppercase; letter-spacing: 0.04em; }
  .field input[type="text"], .field input[type="email"], .field input[type="password"] {
    width: 100%; padding: 10px 14px; border-radius: 8px; border: 1px solid var(--border);
    background: #0d1322; color: var(--text); font-size: 14px; outline: none;
    transition: border-color 0.15s, box-shadow 0.15s, background 0.15s;
  }
  .field input:focus { border-color: var(--accent); }
  .field input.input-error {
    border-color: #ef4444 !important;
    box-shadow: 0 0 0 3px rgba(239, 68, 68, 0.24);
    background: rgba(127, 29, 29, 0.18) !important;
  }
  .password-wrap {
    display: flex; align-items: stretch; gap: 6px;
  }
  .password-wrap input { flex: 1; min-width: 0; }
  .pwd-toggle-btn {
    padding: 0 12px; border-radius: 8px; border: 1px solid var(--border);
    background: #11192c; color: var(--muted); font-size: 12.5px; font-weight: 600;
    cursor: pointer; white-space: nowrap; display: inline-flex; align-items: center; gap: 5px;
    transition: all 0.15s ease; user-select: none;
  }
  .pwd-toggle-btn:hover { border-color: var(--accent); color: var(--text); background: var(--card-hover); }
  .pwd-toggle-btn:active { transform: scale(0.95); }
  .pwd-toggle-btn.active {
    background: rgba(59, 130, 246, 0.24); border-color: #60a5fa; color: #ffffff;
  }
  .login-banner {
    margin-top: 14px; padding: 13px 16px; border-radius: 10px; font-size: 13.5px;
    display: flex; align-items: flex-start; justify-content: space-between; gap: 12px;
    line-height: 1.45; animation: bannerFadeIn 0.22s ease;
  }
  .login-banner.banner-info {
    background: rgba(59, 130, 246, 0.15); border: 1px solid rgba(59, 130, 246, 0.5); color: #bfdbfe;
  }
  .login-banner.banner-success {
    background: rgba(16, 185, 129, 0.17); border: 1px solid #10b981; color: #a7f3d0;
    box-shadow: 0 0 18px rgba(16, 185, 129, 0.2);
  }
  .login-banner.banner-error {
    background: rgba(239, 68, 68, 0.17); border: 1px solid #ef4444; color: #fecaca;
    box-shadow: 0 0 18px rgba(239, 68, 68, 0.22);
    animation: shakeX 0.35s ease;
  }
  .banner-close {
    background: transparent; border: none; color: inherit; opacity: 0.7;
    cursor: pointer; font-size: 15px; line-height: 1; padding: 2px 6px; border-radius: 6px;
  }
  .banner-close:hover { opacity: 1; background: rgba(255,255,255,0.1); }
  @keyframes bannerFadeIn { from { opacity: 0; transform: translateY(-4px); } to { opacity: 1; transform: translateY(0); } }
  @keyframes shakeX {
    0%, 100% { transform: translateX(0); }
    20% { transform: translateX(-6px); }
    40% { transform: translateX(6px); }
    60% { transform: translateX(-4px); }
    80% { transform: translateX(4px); }
  }
  .btn {
    padding: 10px 18px; border-radius: 8px; border: none; font-size: 13.5px; font-weight: 600;
    cursor: pointer; transition: all 0.15s ease; display: inline-flex; align-items: center; justify-content: center; gap: 8px;
  }
  .btn:active:not(:disabled) { transform: scale(0.96); }
  .btn-primary { background: var(--accent); color: #fff; }
  .btn-primary:hover:not(:disabled) { background: var(--accent-hover); }
  .btn-success { background: var(--success); color: #052e16; font-weight: 700; }
  .btn-success:hover:not(:disabled) { filter: brightness(1.1); }
  .btn-outline { background: transparent; color: var(--text); border: 1px solid var(--border); padding: 7px 13px; font-size: 12.5px; }
  .btn-outline:hover { background: var(--card-hover); border-color: var(--accent); }
  .btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .btn-qc {
    background: #0d1322; color: #cbd5e1; border: 1px solid var(--border);
    padding: 7px 13px; font-size: 12.5px; font-weight: 600; border-radius: 8px;
    cursor: pointer; transition: all 0.16s ease; display: inline-flex; align-items: center; gap: 6px;
  }
  .btn-qc:hover { background: #18253e; border-color: #60a5fa; color: #ffffff; transform: translateY(-1px); }
  .btn-qc:active { transform: scale(0.95); }
  .btn-qc.qc-cat { border-color: rgba(59, 130, 246, 0.35); color: #93c5fd; }
  .btn-qc.qc-active {
    background: linear-gradient(135deg, #2563eb, #1d4ed8) !important;
    border-color: #93c5fd !important; color: #ffffff !important; font-weight: 700;
    box-shadow: 0 0 14px rgba(59, 130, 246, 0.5);
  }
  .btn-qc.qc-active-amber {
    background: linear-gradient(135deg, rgba(245, 158, 11, 0.35), rgba(217, 119, 6, 0.45)) !important;
    border-color: #fbbf24 !important; color: #fef3c7 !important; font-weight: 700;
    box-shadow: 0 0 14px rgba(245, 158, 11, 0.42);
  }
  .btn-qc.qc-active-emerald {
    background: linear-gradient(135deg, rgba(16, 185, 129, 0.32), rgba(5, 150, 105, 0.44)) !important;
    border-color: #34d399 !important; color: #ecfdf5 !important; font-weight: 700;
    box-shadow: 0 0 14px rgba(16, 185, 129, 0.42);
  }
  .btn-qc.qc-active-slate {
    background: rgba(148, 163, 184, 0.25) !important;
    border-color: #cbd5e1 !important; color: #ffffff !important; font-weight: 700;
    box-shadow: 0 0 10px rgba(148, 163, 184, 0.32);
  }
  .btn-qc.qc-flash { animation: qcPulse 0.32s ease; }
  @keyframes qcPulse { 0% { transform: scale(0.94); } 50% { transform: scale(1.05); } 100% { transform: scale(1); } }
  .qc-feedback {
    margin-top: 9px; font-size: 12.5px; color: #93c5fd;
    display: inline-flex; align-items: center; gap: 8px; padding: 4px 12px;
    border-radius: 999px; background: rgba(59, 130, 246, 0.12); border: 1px solid rgba(59, 130, 246, 0.32);
  }

  /* Main Tabs: Active Courses vs Hidden (Empty & Past) */
  .main-tabs {
    display: flex; gap: 10px; border-bottom: 2px solid var(--border); padding-bottom: 12px; margin-bottom: 18px;
  }
  .main-tab {
    padding: 10px 20px; border-radius: 10px; font-size: 14px; font-weight: 700;
    cursor: pointer; border: 1px solid var(--border); background: #0d1322; color: var(--muted);
    display: inline-flex; align-items: center; gap: 10px; transition: all 0.15s;
  }
  .main-tab.active {
    background: linear-gradient(135deg, rgba(59,130,246,0.25), rgba(139,92,246,0.2));
    color: #fff; border-color: var(--accent);
  }
  .main-tab .tab-badge {
    padding: 2px 9px; border-radius: 999px; font-size: 12px; background: rgba(255,255,255,0.12);
  }

  /* Filter Pills */
  .pill-group { display: flex; flex-wrap: wrap; gap: 8px; }
  .pill {
    padding: 6px 13px; border-radius: 999px; font-size: 12.5px; font-weight: 600;
    border: 1px solid var(--border); background: #0d1322; color: var(--muted);
    cursor: pointer; user-select: none; display: inline-flex; align-items: center; gap: 7px;
    transition: all 0.15s;
  }
  .pill.active { background: rgba(59, 130, 246, 0.18); color: #60a5fa; border-color: #3b82f6; }
  .pill.active-purple { background: rgba(139, 92, 246, 0.2); color: #c4b5fd; border-color: #8b5cf6; }
  .pill.active-amber { background: rgba(245, 158, 11, 0.2); color: #fbbf24; border-color: #f59e0b; }
  .pill .count { background: rgba(255,255,255,0.1); padding: 1px 7px; border-radius: 99px; font-size: 11px; }

  /* Stats Bar */
  .stats-bar {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin-bottom: 18px;
  }
  .stat-box {
    background: #0d1322; border: 1px solid var(--border); border-radius: 10px; padding: 12px 16px;
  }
  .stat-box .num { font-size: 23px; font-weight: 800; }
  .stat-box .lbl { font-size: 12px; color: var(--muted); }

  /* Expandable Course Cards */
  .course-card {
    border: 1px solid var(--border); border-radius: 12px; background: #0e1525;
    margin-bottom: 14px; overflow: hidden; transition: border-color 0.15s;
  }
  .course-card.expanded { border-color: var(--accent); }
  .course-card-header {
    padding: 16px 20px; display: flex; justify-content: space-between; align-items: center;
    cursor: pointer; background: #11192c; gap: 14px; flex-wrap: wrap;
  }
  .course-card-header:hover { background: var(--card-hover); }
  .course-code-badge {
    font-size: 11.5px; font-weight: 700; padding: 3px 9px; border-radius: 6px;
    background: rgba(59,130,246,0.2); color: #93c5fd;
  }
  .kind-LECT { background: rgba(16, 185, 129, 0.2); color: #6ee7b7; }
  .kind-LAB { background: rgba(245, 158, 11, 0.2); color: #fcd34d; }
  .kind-TUT { background: rgba(139, 92, 246, 0.2); color: #c4b5fd; }
  .kind-PRAC { background: rgba(236, 72, 153, 0.2); color: #f9a8d4; }
  .kind-HIDDEN { background: rgba(142, 160, 192, 0.15); color: #94a3b8; }

  /* Inside-Course Folder Sub-Tabs (Lec, Tutorials & Sheets, Labs, Assignments, Quizzes, etc.) */
  .course-drawer { display: none; border-top: 1px solid var(--border); padding: 16px 20px; background: #0a0f1c; }
  .course-card.expanded .course-drawer { display: block; }
  .subtabs-row {
    display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 14px; padding-bottom: 12px;
    border-bottom: 1px solid rgba(36, 50, 79, 0.6); align-items: center; justify-content: space-between;
  }
  .folder-tab {
    padding: 6px 13px; border-radius: 8px; font-size: 12.5px; font-weight: 600;
    background: #121a2d; border: 1px solid var(--border); color: var(--muted);
    cursor: pointer; display: inline-flex; align-items: center; gap: 6px;
  }
  .folder-tab:hover { border-color: var(--accent); color: var(--text); }
  .folder-tab.active { background: var(--accent); color: #fff; border-color: var(--accent); }

  table.items-table { width: 100%; border-collapse: collapse; font-size: 13px; }
  table.items-table th, table.items-table td {
    padding: 9px 12px; text-align: left; border-bottom: 1px solid rgba(36, 50, 79, 0.45);
  }
  table.items-table th { background: #0b101d; color: var(--muted); font-size: 11px; text-transform: uppercase; position: sticky; top: 0; }
  table.items-table tr:hover { background: rgba(59, 130, 246, 0.06); }
  .badge { padding: 3px 9px; border-radius: 99px; font-size: 11px; font-weight: 600; display: inline-block; }
  .badge-new { background: rgba(245, 158, 11, 0.2); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.4); }
  .badge-disk { background: rgba(16, 185, 129, 0.16); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.3); }
  .badge-local { background: rgba(59, 130, 246, 0.15); color: #60a5fa; border: 1px solid rgba(59, 130, 246, 0.3); }
  .badge-cat { background: rgba(59, 130, 246, 0.13); color: #93c5fd; }
  .badge-type { background: rgba(139, 92, 246, 0.15); color: #c4b5fd; }

  .progress-wrap { background: #090d16; border: 1px solid var(--border); border-radius: 10px; height: 22px; overflow: hidden; margin: 12px 0; }
  .progress-bar { height: 100%; background: linear-gradient(90deg, #3b82f6, #10b981); width: 0%; transition: width 0.25s; }
  .log-box {
    background: #070a12; border: 1px solid var(--border); border-radius: 8px;
    padding: 12px; font-family: Consolas, monospace; font-size: 12px; color: #93c5fd;
    height: 210px; overflow-y: auto; white-space: pre-wrap;
  }
  .hidden { display: none !important; }
</style>
</head>
<body>

<header>
  <div class="brand">
    <img src="/icon.png" alt="NU Moodle Sync" style="width:54px; height:54px; object-fit:contain; flex-shrink:0; filter:drop-shadow(0 4px 12px rgba(59,130,246,0.4));">
    <div>
      <h1>Nile University Moodle Sync Studio</h1>
      <p>Click any course (e.g. LECT) to inspect Lectures, Sheets, Labs & Videos — Will Download vs Already in Folder</p>
      <div style="margin-top:4px; display:flex; align-items:center; gap:10px; flex-wrap:wrap; font-size:12.5px;">
        <span style="padding:2px 10px; border-radius:999px; background:rgba(59,130,246,0.18); border:1px solid rgba(59,130,246,0.4); color:#93c5fd; font-weight:700;">
          ✨ Made by : farisEmad
        </span>
        <a href="https://wa.me/201020933516" target="_blank" rel="noopener" style="padding:2px 10px; border-radius:999px; background:rgba(16,185,129,0.16); border:1px solid rgba(16,185,129,0.4); color:#34d399; font-weight:700; text-decoration:none;">
          💬 For any bug or recommendation : WhatsApp 01020933516
        </a>
      </div>
    </div>
  </div>
  <div id="sessionBadge" class="status-pill">Ready</div>
</header>

<div class="container">

  <!-- STEP 1: LOGIN & WORKSPACE -->
  <div class="card">
    <div class="step-header">
      <div class="step-title">
        <span class="step-num">1</span>
        <span>Moodle Account & Target Study Folder</span>
      </div>
      <div style="display:flex; align-items:center; gap:14px; flex-wrap:wrap;">
        <label style="font-size:13px; color:var(--muted); display:flex; align-items:center; gap:6px; cursor:pointer;">
          <input type="checkbox" id="forceFresh"> Force fresh Microsoft SSO login
        </label>
        <button type="button" class="btn btn-outline" style="border-color:rgba(239,68,68,0.5); color:#fca5a5;" onclick="signOutAndClearSession()">
          🔒 Sign Out & Clear Session
        </button>
      </div>
    </div>
    <div style="font-size:12px; color:#8ea0c0; margin-bottom:14px; padding:8px 12px; border-radius:8px; background:rgba(16,185,129,0.08); border:1px solid rgba(16,185,129,0.25);">
      🛡️ <b>Local Security:</b> Runs 100% locally on your computer (<code>127.0.0.1</code>). Your password is used only to log into Microsoft SSO and is <b>never</b> saved to disk or source files. Click <b>🔒 Sign Out & Clear Session</b> anytime to wipe saved session cookies.
    </div>
    <div class="form-grid">
      <div class="field">
        <label for="emailInput">Nile University Email</label>
        <input type="email" id="emailInput" placeholder="username@nu.edu.eg" autocomplete="username"
               oninput="clearLoginFeedbackHighlight()"
               onkeydown="if(event.key==='Enter'){connectAndScanAll();}">
      </div>
      <div class="field">
        <label for="passwordInput">Moodle / Microsoft Password</label>
        <div class="password-wrap">
          <input type="password" id="passwordInput" placeholder="••••••••" autocomplete="current-password"
                 oninput="clearLoginFeedbackHighlight()"
                 onkeydown="if(event.key==='Enter'){connectAndScanAll();}">
          <button type="button" class="pwd-toggle-btn" id="togglePwdBtn"
                  onclick="togglePasswordVisibility()"
                  aria-label="Show password" aria-pressed="false" title="Show or hide password">
            👁️ Show
          </button>
        </div>
      </div>
      <div class="field">
        <label for="workspaceInput">Local Study Folder (Windows / macOS / Linux)</label>
        <div style="display:flex; gap:8px;">
          <input type="text" id="workspaceInput" value="E:\Study" style="flex:1;">
          <button type="button" class="btn btn-outline" style="white-space:nowrap; border-color:#3b82f6; color:#93c5fd;" onclick="toggleFolderBrowser()">
            📂 Select Folder (Table)
          </button>
        </div>
      </div>
      <div>
        <button type="button" class="btn btn-primary" id="loginBtn" onclick="connectAndScanAll()" style="width:100%;">
          Connect & Scan My Courses
        </button>
      </div>
    </div>

    <div id="loginStatusBanner" class="login-banner hidden" role="status" aria-live="polite"></div>

    <!-- INTERACTIVE FOLDER SELECTION TABLE PANEL -->
    <div id="folderBrowserPanel" class="hidden" style="margin-top:16px; padding:16px; border-radius:12px; background:#0d1322; border:1px solid #2563eb;">
      <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px; margin-bottom:12px;">
        <div>
          <div style="font-weight:700; font-size:14.5px; color:#93c5fd;">📂 Select Target Study Folder (Interactive Folder Table)</div>
          <div style="font-size:12px; color:var(--muted);">
            Click any drive/location below, click a folder name to open it, or click <b>"✓ Select Folder"</b> to use it.
          </div>
        </div>
        <button type="button" class="btn btn-outline" onclick="toggleFolderBrowser()">✕ Close Folder Table</button>
      </div>

      <!-- Quick-Jump Drives & OS Locations -->
      <div style="margin-bottom:12px;">
        <div style="font-size:11px; font-weight:700; color:var(--muted); text-transform:uppercase; margin-bottom:6px;">
          Available Drives & Quick Locations (<span id="detectedOsBadge">OS</span>):
        </div>
        <div class="pill-group" id="osRootsBar"></div>
      </div>

      <!-- Current Browsed Path + Up One Level + Select Current + Create New Folder -->
      <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px; padding:10px 12px; border-radius:8px; background:#090d16; border:1px solid var(--border); margin-bottom:12px;">
        <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
          <button type="button" class="btn btn-outline" style="padding:6px 10px; font-size:12px;" id="upDirBtn" onclick="browseUpParent()">
            ⬆️ Up One Level
          </button>
          <span style="font-size:12px; color:var(--muted);">Current Path:</span>
          <code id="browserCurrentPath" style="font-size:13px; color:#60a5fa; font-weight:700;">E:\Study</code>
          <button type="button" class="btn btn-success" style="padding:6px 12px; font-size:12px;" onclick="selectCurrentBrowsedFolder()">
            ✓ Use This Current Folder
          </button>
        </div>
        <div style="display:flex; align-items:center; gap:6px;">
          <input type="text" id="newFolderNameInput" placeholder="New folder name (e.g. stud)"
                 style="padding:6px 10px; border-radius:7px; border:1px solid var(--border); background:#111827; color:var(--text); font-size:12.5px; width:190px;">
          <button type="button" class="btn btn-outline" style="padding:6px 11px; font-size:12px; border-color:#10b981; color:#6ee7b7;" onclick="createNewFolderInBrowser()">
            ➕ Create & Select
          </button>
        </div>
      </div>

      <!-- Folder Table -->
      <div style="max-height:320px; overflow-y:auto; border:1px solid var(--border); border-radius:8px;">
        <table class="items-table">
          <thead>
            <tr>
              <th>Folder Name (Click to Open Inside)</th>
              <th>Contents</th>
              <th>Last Modified</th>
              <th style="text-align:right;">Actions</th>
            </tr>
          </thead>
          <tbody id="folderTableBody"></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- STEP 2: COURSES & CONTENT EXPLORER (WITH ACTIVE vs HIDDEN TABS) -->
  <div class="card hidden" id="step2Card">
    <div class="step-header">
      <div class="step-title">
        <span class="step-num">2</span>
        <span>Courses & Content Explorer (Click any course to open its Lectures, Sheets, Labs & Files)</span>
      </div>
      <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
        <label style="font-size:13px; color:var(--muted); display:flex; align-items:center; gap:6px; cursor:pointer;">
          <input type="checkbox" id="extractZipsToggle" checked> Auto-extract ZIP archives
        </label>
        <button class="btn btn-success" id="downloadBtn" onclick="startDownloadSelected()">
          Download Checked Items (<span id="checkedCountBtn">0</span>)
        </button>
      </div>
    </div>

    <!-- MAIN TABS: Active Courses vs Hidden (Empty & Past Courses) -->
    <div class="main-tabs">
      <div class="main-tab active" id="tabBtnActive" onclick="switchMainTab('ACTIVE')">
        <span>📚 Active Courses (With Content)</span>
        <span class="tab-badge" id="activeTabCount">0</span>
      </div>
      <div class="main-tab" id="tabBtnHidden" onclick="switchMainTab('HIDDEN')">
        <span>🗄️ Hidden (Empty Current Shells & Past Courses)</span>
        <span class="tab-badge" id="hiddenTabCount">0</span>
      </div>
    </div>

    <!-- ACTIVE TAB VIEW -->
    <div id="activeTabContainer">
      <div class="stats-bar" id="scanStatsBar"></div>

      <!-- Status Filter + Category Filter + Media Type Filter -->
      <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap:14px; margin-bottom:16px;">
        <div>
          <div style="font-size:11.5px; font-weight:700; color:var(--muted); text-transform:uppercase; margin-bottom:7px;">
            1. Show by Download Status:
          </div>
          <div class="pill-group" id="statusPills"></div>
        </div>
        <div>
          <div style="font-size:11.5px; font-weight:700; color:var(--muted); text-transform:uppercase; margin-bottom:7px;">
            2. Filter by Content / Media Type:
          </div>
          <div class="pill-group" id="mediaTypePills"></div>
        </div>
      </div>

      <div style="margin-bottom:16px;">
        <div style="font-size:11.5px; font-weight:700; color:var(--muted); text-transform:uppercase; margin-bottom:7px;">
          3. Filter by Folder Category (Lec, Tutorials & Sheets, Labs, Assignments, Quizzes, Project, General):
        </div>
        <div class="pill-group" id="categoryPills"></div>
      </div>

      <div style="margin-bottom:14px;">
        <div style="font-size:11.5px; font-weight:700; color:var(--muted); text-transform:uppercase; margin-bottom:7px;">
          4. Quick Check Across All Courses (Select What to Download):
        </div>
        <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px;">
          <div style="display:flex; gap:7px; flex-wrap:wrap;" id="quickCheckBar">
            <button type="button" class="btn btn-qc" id="qc-MISSING_ONLY" onclick="applyQuickCheck('MISSING_ONLY', this)">🟡 Check All Missing</button>
            <button type="button" class="btn btn-qc" id="qc-ALL_ITEMS" onclick="applyQuickCheck('ALL_ITEMS', this)">☑ Check All Files</button>
            <button type="button" class="btn btn-qc" id="qc-NONE" onclick="applyQuickCheck('NONE', this)">☐ Uncheck All</button>
            <button type="button" class="btn btn-qc qc-cat" id="qc-cat-Lectures" onclick="checkCategoryGlobal('Lectures', true, this)">☑ Check Only Lectures (All)</button>
            <button type="button" class="btn btn-qc qc-cat" id="qc-cat-Tutorials & Sheets" onclick="checkCategoryGlobal('Tutorials & Sheets', true, this)">☑ Check Only Tutorials & Sheets (All)</button>
            <button type="button" class="btn btn-qc qc-cat" id="qc-cat-Labs" onclick="checkCategoryGlobal('Labs', true, this)">☑ Check Only Labs (All)</button>
            <button type="button" class="btn btn-qc qc-cat" id="qc-cat-Assignments" onclick="checkCategoryGlobal('Assignments', true, this)">☑ Check Only Assignments (All)</button>
            <button type="button" class="btn btn-qc qc-cat" id="qc-cat-Quizzes & Exams" onclick="checkCategoryGlobal('Quizzes & Exams', true, this)">☑ Check Only Quizzes (All)</button>
            <button type="button" class="btn btn-qc" id="qc-expand-all" onclick="expandCollapseAll(true, this)">📂 Expand All Courses</button>
            <button type="button" class="btn btn-qc" id="qc-collapse-all" onclick="expandCollapseAll(false, this)">📁 Collapse All</button>
          </div>
          <input type="text" id="searchBox" placeholder="Search lecture, sheet, lab, video..." oninput="renderActiveCourses()"
                 style="padding:8px 14px; border-radius:8px; border:1px solid var(--border); background:#0d1322; color:var(--text); font-size:13px; width:260px;">
        </div>
        <div id="quickCheckFeedback" class="qc-feedback hidden"></div>
      </div>

      <div id="activeCoursesList"></div>
    </div>

    <!-- HIDDEN TAB VIEW (Empty Current Courses + Past Semester Courses) -->
    <div id="hiddenTabContainer" class="hidden">
      <div style="margin-bottom:16px; padding:12px 16px; border-radius:10px; background:#0d1322; border:1px solid var(--border); display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px;">
        <div>
          <div style="font-weight:700; font-size:14px;">Hidden Courses (Empty Current Section Shells & Past Semesters)</div>
          <div style="font-size:12.5px; color:var(--muted);">
            Courses where the scanner found 0 items on Moodle, plus past courses from earlier semesters (2024FALL – 2026SPRG). Click <b>"Scan & Inspect Content"</b> on any course below to load its files.
          </div>
        </div>
        <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
          <button type="button" class="btn btn-qc qc-active" onclick="setHiddenSubFilter('ALL')" id="hsub-ALL">All Hidden</button>
          <button type="button" class="btn btn-qc" onclick="setHiddenSubFilter('EMPTY')" id="hsub-EMPTY">Empty 2026FALL Shells (0 items)</button>
          <button type="button" class="btn btn-qc" onclick="setHiddenSubFilter('PAST')" id="hsub-PAST">Past Semesters</button>
          <input type="text" id="hiddenSearchBox" placeholder="Search hidden/past courses..." oninput="renderHiddenCourses()"
                 style="padding:7px 12px; border-radius:8px; border:1px solid var(--border); background:#090d16; color:var(--text); font-size:12.5px; width:220px;">
        </div>
      </div>
      <div id="hiddenCoursesList"></div>
    </div>
  </div>

  <!-- STEP 3: LIVE DOWNLOAD PROGRESS -->
  <div class="card hidden" id="step4Card">
    <div class="step-header">
      <div class="step-title">
        <span class="step-num">3</span>
        <span id="syncStageTitle">Live Synchronization Progress</span>
      </div>
      <span id="syncCounterBadge" class="status-pill">0 / 0</span>
    </div>
    <div id="currentFileLabel" style="font-size:14px; font-weight:600; color:#93c5fd;">Preparing...</div>
    <div class="progress-wrap">
      <div class="progress-bar" id="syncProgressBar"></div>
    </div>
    <div class="log-box" id="syncLogBox"></div>
  </div>

</div>

<script>
const ALL_CATEGORIES = ["Lectures", "Tutorials & Sheets", "Labs", "Assignments", "Quizzes & Exams", "Project", "General"];
const ALL_MEDIA_TYPES = ["Videos & Recordings", "Documents & Slides", "ZIP Archives", "Code & Scripts", "External Links"];

let allCourses = [];
let summaryData = {};
let currentMainTab = "ACTIVE"; // "ACTIVE" or "HIDDEN"
let hiddenSubFilter = "ALL";   // "ALL", "EMPTY", "PAST"
let statusFilter = "ALL";      // "ALL", "MISSING", "ON_DISK"
let activeCategories = new Set(ALL_CATEGORIES);
let activeMediaTypes = new Set(ALL_MEDIA_TYPES);
let perCourseSubTab = {};      // course_id -> "ALL" or category name
let expandedCourseIds = new Set();
let checkedKeys = new Set();
let activeQuickCheckMode = "MISSING_ONLY";
let lastQuickCheckMessage = "";
let pollTimer = null;
let currentBrowserPath = "";
let parentBrowserPath = "";

function escapeHtml(str) {
  return String(str || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function togglePasswordVisibility() {
  const inp = document.getElementById('passwordInput');
  const btn = document.getElementById('togglePwdBtn');
  if (!inp || !btn) return;
  const isShowing = inp.type === 'text';
  inp.type = isShowing ? 'password' : 'text';
  btn.classList.toggle('active', !isShowing);
  btn.setAttribute('aria-pressed', String(!isShowing));
  btn.setAttribute('aria-label', isShowing ? 'Show password' : 'Hide password');
  btn.textContent = isShowing ? '👁️ Show' : '🙈 Hide';
  inp.focus();
}

function clearLoginFeedbackHighlight() {
  const e = document.getElementById('emailInput');
  const p = document.getElementById('passwordInput');
  if (e) e.classList.remove('input-error');
  if (p) p.classList.remove('input-error');
}

function setSessionBadge(text, mode) {
  const badge = document.getElementById('sessionBadge');
  if (!badge) return;
  badge.textContent = text;
  badge.classList.remove('status-error', 'status-loading');
  if (mode === 'error') badge.classList.add('status-error');
  else if (mode === 'loading') badge.classList.add('status-loading');
}

function showLoginBanner(kind, title, detail, targetField) {
  const banner = document.getElementById('loginStatusBanner');
  if (!banner) return;
  clearLoginFeedbackHighlight();
  banner.className = 'login-banner';
  if (kind === 'hidden' || !title) {
    banner.classList.add('hidden');
    banner.innerHTML = '';
    return;
  }
  banner.classList.add('banner-' + kind);
  banner.innerHTML = `
    <div>
      <div style="font-weight:700; font-size:14px; margin-bottom:2px;">${escapeHtml(title)}</div>
      ${detail ? `<div style="font-size:12.5px; opacity:0.95;">${escapeHtml(detail)}</div>` : ''}
    </div>
    <button type="button" class="banner-close" title="Dismiss message" onclick="showLoginBanner('hidden')">✕</button>
  `;
  if (targetField === 'password') {
    const pinp = document.getElementById('passwordInput');
    if (pinp) { pinp.classList.add('input-error'); pinp.focus(); pinp.select(); }
  } else if (targetField === 'email') {
    const einp = document.getElementById('emailInput');
    if (einp) { einp.classList.add('input-error'); einp.focus(); einp.select(); }
  }
}

function formatBytes(b) {
  if (!b || b <= 0) return "—";
  if (b >= 1048576) return (b / 1048576).toFixed(1) + " MB";
  if (b >= 1024) return (b / 1024).toFixed(1) + " KB";
  return b + " B";
}

function toggleFolderBrowser() {
  const panel = document.getElementById('folderBrowserPanel');
  const isHidden = panel.classList.contains('hidden');
  if (isHidden) {
    panel.classList.remove('hidden');
    loadFolderTable(document.getElementById('workspaceInput').value);
  } else {
    panel.classList.add('hidden');
  }
}

async function loadFolderTable(targetPath) {
  try {
    const res = await fetch('/api/browse_dirs', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({path: targetPath || ''})
    });
    const data = await res.json();
    if (!data.ok) return;

    currentBrowserPath = data.current_path;
    parentBrowserPath = data.parent_path || "";
    document.getElementById('detectedOsBadge').textContent = data.os_name || 'System';
    document.getElementById('browserCurrentPath').textContent = currentBrowserPath;
    document.getElementById('upDirBtn').disabled = !parentBrowserPath;

    // Render OS quick-jump roots
    const rootsBar = document.getElementById('osRootsBar');
    rootsBar.innerHTML = '';
    (data.roots || []).forEach(rt => {
      const p = document.createElement('div');
      p.className = 'pill' + (rt.path.toLowerCase() === currentBrowserPath.toLowerCase() ? ' active' : '');
      p.innerHTML = `<span>${rt.label}</span>`;
      p.onclick = () => loadFolderTable(rt.path);
      rootsBar.appendChild(p);
    });

    // Render folders table
    const tbody = document.getElementById('folderTableBody');
    const folders = data.folders || [];
    if (folders.length === 0) {
      tbody.innerHTML = `
        <tr>
          <td colspan="4" style="text-align:center; padding:18px; color:var(--muted);">
            No subfolders inside <b>${currentBrowserPath}</b>. You can click <b>"✓ Use This Current Folder"</b> above or create a new folder.
          </td>
        </tr>`;
      return;
    }

    tbody.innerHTML = folders.map((f, idx) => `
      <tr>
        <td style="cursor:pointer;" data-path="${encodeURIComponent(f.path)}" onclick="loadFolderTable(decodeURIComponent(this.dataset.path))">
          <span style="font-size:15px; margin-right:6px;">📁</span>
          <b style="color:#93c5fd; text-decoration:underline;">${f.name}</b>
        </td>
        <td style="font-size:12px; color:var(--muted);">
          ${f.subfolders_count} subfolders · ${f.files_count} files
        </td>
        <td style="font-size:12px; color:var(--muted);">${f.modified}</td>
        <td style="text-align:right; white-space:nowrap;">
          <button type="button" class="btn btn-outline" style="padding:5px 10px; font-size:12px; margin-right:6px;"
                  data-path="${encodeURIComponent(f.path)}"
                  onclick="loadFolderTable(decodeURIComponent(this.dataset.path))">
            📂 Open Inside
          </button>
          <button type="button" class="btn btn-success" style="padding:5px 12px; font-size:12px;"
                  data-path="${encodeURIComponent(f.path)}"
                  onclick="selectFolderFromTable(decodeURIComponent(this.dataset.path))">
            ✓ Select Folder
          </button>
        </td>
      </tr>
    `).join('');
  } catch (e) {
    console.error(e);
  }
}

function browseUpParent() {
  if (parentBrowserPath) loadFolderTable(parentBrowserPath);
}

function selectFolderFromTable(chosenPath) {
  document.getElementById('workspaceInput').value = chosenPath;
  document.getElementById('folderBrowserPanel').classList.add('hidden');
}

function selectCurrentBrowsedFolder() {
  if (currentBrowserPath) {
    document.getElementById('workspaceInput').value = currentBrowserPath;
    document.getElementById('folderBrowserPanel').classList.add('hidden');
  }
}

async function createNewFolderInBrowser() {
  const inp = document.getElementById('newFolderNameInput');
  const name = (inp.value || '').trim();
  if (!name) {
    alert('Please enter a name for the new folder.');
    return;
  }
  const res = await fetch('/api/create_dir', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      parent_path: currentBrowserPath,
      new_folder_name: name
    })
  });
  const data = await res.json();
  if (!data.ok) {
    alert(data.error || 'Could not create folder');
    return;
  }
  inp.value = '';
  if (data.created_path) {
    selectFolderFromTable(data.created_path);
  } else {
    loadFolderTable(currentBrowserPath);
  }
}

async function initApp() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();
    if (data.email) document.getElementById('emailInput').value = data.email;
    if (data.workspace) document.getElementById('workspaceInput').value = data.workspace;
    if (data.has_cached_session) {
      setSessionBadge('Cached Session Ready — Click "Connect & Scan My Courses"', 'ready');
      showLoginBanner(
        'info',
        '⚡ Saved Moodle Session Ready' + (data.email ? ` (${data.email})` : ''),
        'Click "Connect & Scan My Courses" to scan right away (leave Password blank to reuse your active session, or enter your password to verify / switch account).'
      );
    } else {
      setSessionBadge('Ready to Connect', 'ready');
    }
  } catch (e) {}
}

async function signOutAndClearSession() {
  try {
    const res = await fetch('/api/logout', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: '{}'
    });
    const data = await res.json();
    document.getElementById('emailInput').value = '';
    const pwdInp = document.getElementById('passwordInput');
    pwdInp.value = '';
    pwdInp.type = 'password';
    const tBtn = document.getElementById('togglePwdBtn');
    if (tBtn) {
      tBtn.classList.remove('active');
      tBtn.setAttribute('aria-pressed', 'false');
      tBtn.textContent = '👁️ Show';
    }
    allCourses = [];
    summaryData = {};
    checkedKeys.clear();
    expandedCourseIds.clear();
    perCourseSubTab = {};
    activeQuickCheckMode = 'MISSING_ONLY';
    lastQuickCheckMessage = '';
    document.getElementById('step2Card').classList.add('hidden');
    document.getElementById('step4Card').classList.add('hidden');
    setSessionBadge('Signed Out • Session Cleared', 'ready');
    updateCheckedCountButton();
    showLoginBanner(
      'success',
      '🔓 Signed Out Successfully',
      data.message || 'Email, password, and saved Moodle session cookies have been cleared.'
    );
  } catch (err) {
    showLoginBanner('error', '❌ Sign-Out Error', 'Could not clear session cleanly: ' + err.message);
  }
}

async function connectAndScanAll() {
  const btn = document.getElementById('loginBtn');
  clearLoginFeedbackHighlight();
  btn.disabled = true;
  btn.textContent = 'Connecting & Scanning Moodle Courses...';
  setSessionBadge('Signing In & Scanning Moodle...', 'loading');
  showLoginBanner(
    'info',
    '⏳ Signing in to Nile University Moodle...',
    'Authenticating via Microsoft SSO and scanning your courses and Study folder...'
  );
  try {
    const res = await fetch('/api/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        email: document.getElementById('emailInput').value,
        password: document.getElementById('passwordInput').value,
        workspace: document.getElementById('workspaceInput').value,
        force_fresh: document.getElementById('forceFresh').checked
      })
    });
    let data = {};
    try {
      data = await res.json();
    } catch (parseErr) {
      throw new Error(`Unexpected response from server (HTTP ${res.status})`);
    }
    if (!res.ok || !data.ok) {
      const e = new Error(data.error || 'Sign-in failed');
      e.error_code = data.error_code || 'AUTH_ERROR';
      e.error_field = data.error_field || 'general';
      throw e;
    }

    allCourses = data.courses || [];
    summaryData = data.summary || {};

    // Reset all view filters on fresh scan so no files are accidentally hidden
    activeCategories = new Set(ALL_CATEGORIES);
    activeMediaTypes = new Set(ALL_MEDIA_TYPES);
    statusFilter = 'ALL';
    perCourseSubTab = {};
    const sb = document.getElementById('searchBox');
    if (sb) sb.value = '';

    // Expand the first active course by default so user immediately sees how it opens
    const actives = allCourses.filter(c => c.tab_group === 'ACTIVE');
    if (actives.length && expandedCourseIds.size === 0) {
      expandedCourseIds.add(actives[0].course_id);
    }

    setSessionBadge(
      `✅ Signed In • ${summaryData.active_courses_count} Active Courses • ${summaryData.hidden_courses_count} Hidden`,
      'ready'
    );
    showLoginBanner(
      'success',
      `✅ ${data.message || 'Signed in successfully!'}`,
      `Found ${summaryData.active_courses_count} Active Courses (${summaryData.total_missing} missing to download, ${summaryData.total_existing} already in folder) and ${summaryData.hidden_courses_count} Hidden / Past courses.`
    );
    document.getElementById('step2Card').classList.remove('hidden');

    applyQuickCheck('MISSING_ONLY');
  } catch (err) {
    const code = err.error_code || 'AUTH_ERROR';
    const field = err.error_field || 'general';
    let title = '❌ Sign-In Failed';
    let detail = err.message || 'Could not connect to Moodle.';
    let badgeText = '❌ Sign-In Failed';
    if (code === 'WRONG_PASSWORD') {
      title = '❌ Wrong Password — Could Not Sign In';
      detail = `The password you entered is incorrect. Click "👁️ Show" to inspect what you typed, check Caps Lock, and try again. (${err.message})`;
      badgeText = '❌ Wrong Password';
    } else if (code === 'WRONG_EMAIL') {
      title = '❌ Wrong Email Address — Account Not Found';
      detail = `Microsoft could not find that Nile University email account. Check spelling (e.g. name@nu.edu.eg). (${err.message})`;
      badgeText = '❌ Wrong Email';
    } else if (code === 'MISSING_CREDENTIALS') {
      title = '⚠️ Email & Password Required';
      detail = err.message;
      badgeText = '⚠️ Credentials Needed';
    } else if (code === 'MFA_REQUIRED') {
      title = '🔐 Microsoft Authenticator / 2FA Required';
      detail = err.message;
      badgeText = '🔐 MFA Required';
    } else if (code === 'AUTH_TIMEOUT') {
      title = '⏱️ Connection Timed Out';
      detail = err.message;
      badgeText = '⏱️ Timeout';
    }
    showLoginBanner('error', title, detail, field);
    setSessionBadge(badgeText, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Connect & Scan My Courses';
  }
}

function switchMainTab(tab) {
  currentMainTab = tab;
  document.getElementById('tabBtnActive').classList.toggle('active', tab === 'ACTIVE');
  document.getElementById('tabBtnHidden').classList.toggle('active', tab === 'HIDDEN');
  document.getElementById('activeTabContainer').classList.toggle('hidden', tab !== 'ACTIVE');
  document.getElementById('hiddenTabContainer').classList.toggle('hidden', tab !== 'HIDDEN');
  renderAll();
}

function setHiddenSubFilter(mode) {
  hiddenSubFilter = mode;
  renderHiddenCourses();
}

function flashQcButton(btnEl) {
  if (!btnEl) return;
  btnEl.classList.remove('qc-flash');
  void btnEl.offsetWidth;
  btnEl.classList.add('qc-flash');
}

function updateQuickCheckButtons() {
  const bar = document.getElementById('quickCheckBar');
  if (bar) {
    bar.querySelectorAll('.btn-qc').forEach(b => {
      b.classList.remove('qc-active', 'qc-active-amber', 'qc-active-emerald', 'qc-active-slate');
    });
  }
  if (activeQuickCheckMode === 'MISSING_ONLY') {
    const el = document.getElementById('qc-MISSING_ONLY');
    if (el) el.classList.add('qc-active-amber');
  } else if (activeQuickCheckMode === 'ALL_ITEMS') {
    const el = document.getElementById('qc-ALL_ITEMS');
    if (el) el.classList.add('qc-active-emerald');
  } else if (activeQuickCheckMode === 'NONE') {
    const el = document.getElementById('qc-NONE');
    if (el) el.classList.add('qc-active-slate');
  } else if (activeQuickCheckMode.startsWith('CAT::')) {
    const cat = activeQuickCheckMode.slice(5);
    const el = document.getElementById('qc-cat-' + cat);
    if (el) el.classList.add('qc-active');
  }

  const actives = allCourses.filter(c => c.tab_group === 'ACTIVE');
  const expEl = document.getElementById('qc-expand-all');
  const colEl = document.getElementById('qc-collapse-all');
  if (actives.length > 0 && actives.every(c => expandedCourseIds.has(c.course_id))) {
    if (expEl) expEl.classList.add('qc-active-emerald');
  } else if (actives.length > 0 && actives.every(c => !expandedCourseIds.has(c.course_id))) {
    if (colEl) colEl.classList.add('qc-active-slate');
  }

  const fb = document.getElementById('quickCheckFeedback');
  if (fb) {
    if (lastQuickCheckMessage && actives.length > 0) {
      fb.textContent = lastQuickCheckMessage;
      fb.classList.remove('hidden');
    } else {
      fb.classList.add('hidden');
    }
  }
}

function syncQuickCheckAfterManualEdit() {
  activeQuickCheckMode = checkedKeys.size === 0 ? 'NONE' : 'CUSTOM';
  lastQuickCheckMessage = checkedKeys.size === 0
    ? '☐ Unchecked all items (0 selected for download)'
    : `✓ Custom selection: ${checkedKeys.size} items checked for download`;
  updateCheckedCountButton();
  updateQuickCheckButtons();
}

function renderAll() {
  const actives = allCourses.filter(c => c.tab_group === 'ACTIVE');
  const hiddens = allCourses.filter(c => c.tab_group === 'HIDDEN');
  document.getElementById('activeTabCount').textContent = actives.length;
  document.getElementById('hiddenTabCount').textContent = hiddens.length;
  renderTopControls();
  renderActiveCourses();
  renderHiddenCourses();
  updateCheckedCountButton();
  updateQuickCheckButtons();
}

function renderTopControls() {
  const actives = allCourses.filter(c => c.tab_group === 'ACTIVE');
  let totalItems = 0, totalMissing = 0, totalOnDisk = 0;
  const catCounts = {}, mediaCounts = {};
  actives.forEach(c => {
    c.items.forEach(it => {
      totalItems++;
      if (it.on_disk) totalOnDisk++;
      else totalMissing++;
      catCounts[it.category] = (catCounts[it.category] || 0) + 1;
      mediaCounts[it.media_type] = (mediaCounts[it.media_type] || 0) + 1;
    });
  });

  document.getElementById('scanStatsBar').innerHTML = `
    <div class="stat-box"><div class="num">${actives.length}</div><div class="lbl">Active Courses (With Content)</div></div>
    <div class="stat-box"><div class="num" style="color:#fbbf24;">${totalMissing}</div><div class="lbl">Will Be Downloaded (Missing)</div></div>
    <div class="stat-box"><div class="num" style="color:#34d399;">${totalOnDisk}</div><div class="lbl">Already in Study Folder</div></div>
    <div class="stat-box"><div class="num" style="color:#94a3b8;">${allCourses.length - actives.length}</div><div class="lbl">Hidden (Empty & Past Courses)</div></div>
  `;

  // Status Pills
  const sp = document.getElementById('statusPills');
  sp.innerHTML = '';
  [
    {id: 'ALL', label: 'All Files (Download + On Disk)', cnt: totalItems},
    {id: 'MISSING', label: '🟡 Will Be Downloaded (Missing)', cnt: totalMissing},
    {id: 'ON_DISK', label: '🟢 Already in My Folder', cnt: totalOnDisk},
  ].forEach(st => {
    const p = document.createElement('div');
    p.className = 'pill' + (statusFilter === st.id ? ' active-amber' : '');
    p.innerHTML = `<span>${st.label}</span><span class="count">${st.cnt}</span>`;
    p.onclick = () => { statusFilter = st.id; renderAll(); };
    sp.appendChild(p);
  });

  // Category Pills
  const catContainer = document.getElementById('categoryPills');
  catContainer.innerHTML = '';
  const allCatsSelected = activeCategories.size === ALL_CATEGORIES.length;
  const allCatPill = document.createElement('div');
  allCatPill.className = 'pill' + (allCatsSelected ? ' active' : '');
  allCatPill.innerHTML = `<span>All Categories</span><span class="count">${totalItems}</span>`;
  allCatPill.onclick = () => {
    activeCategories = new Set(ALL_CATEGORIES);
    renderAll();
  };
  catContainer.appendChild(allCatPill);

  ALL_CATEGORIES.forEach(cat => {
    const cnt = catCounts[cat] || 0;
    const pill = document.createElement('div');
    pill.className = 'pill' + (activeCategories.has(cat) ? ' active' : '');
    pill.innerHTML = `<span>${cat}</span><span class="count">${cnt}</span>`;
    pill.onclick = () => {
      if (activeCategories.has(cat)) activeCategories.delete(cat);
      else activeCategories.add(cat);
      if (activeCategories.size === 0) activeCategories = new Set(ALL_CATEGORIES);
      renderAll();
    };
    catContainer.appendChild(pill);
  });

  // Media Type Pills
  const mediaContainer = document.getElementById('mediaTypePills');
  mediaContainer.innerHTML = '';
  const allMediaSelected = activeMediaTypes.size === ALL_MEDIA_TYPES.length;
  const allMediaPill = document.createElement('div');
  allMediaPill.className = 'pill' + (allMediaSelected ? ' active-purple' : '');
  allMediaPill.innerHTML = `<span>All Media Types</span><span class="count">${totalItems}</span>`;
  allMediaPill.onclick = () => {
    activeMediaTypes = new Set(ALL_MEDIA_TYPES);
    renderAll();
  };
  mediaContainer.appendChild(allMediaPill);

  ALL_MEDIA_TYPES.forEach(mt => {
    const cnt = mediaCounts[mt] || 0;
    const pill = document.createElement('div');
    pill.className = 'pill' + (activeMediaTypes.has(mt) ? ' active-purple' : '');
    pill.innerHTML = `<span>${mt}</span><span class="count">${cnt}</span>`;
    pill.onclick = () => {
      if (activeMediaTypes.has(mt)) activeMediaTypes.delete(mt);
      else activeMediaTypes.add(mt);
      if (activeMediaTypes.size === 0) activeMediaTypes = new Set(ALL_MEDIA_TYPES);
      renderAll();
    };
    mediaContainer.appendChild(pill);
  });
}

function resetAllFilters(cid) {
  activeCategories = new Set(ALL_CATEGORIES);
  activeMediaTypes = new Set(ALL_MEDIA_TYPES);
  statusFilter = 'ALL';
  if (cid) perCourseSubTab[cid] = 'ALL';
  else perCourseSubTab = {};
  const sb = document.getElementById('searchBox');
  if (sb) sb.value = '';
  renderAll();
}

function itemMatchesGlobalAndCourseFilter(it, cid) {
  if (activeCategories.size > 0 && !activeCategories.has(it.category)) return false;
  if (activeMediaTypes.size > 0 && !activeMediaTypes.has(it.media_type)) return false;
  if (statusFilter === 'MISSING' && it.on_disk) return false;
  if (statusFilter === 'ON_DISK' && !it.on_disk) return false;

  const ctab = perCourseSubTab[cid] || 'ALL';
  if (ctab !== 'ALL' && it.category !== ctab) return false;

  const q = (document.getElementById('searchBox').value || '').trim().toLowerCase();
  if (q && !(it.title.toLowerCase().includes(q) || it.filename.toLowerCase().includes(q) || it.target_subfolder.toLowerCase().includes(q))) {
    return false;
  }
  return true;
}

function toggleCourseExpand(cid) {
  if (expandedCourseIds.has(cid)) expandedCourseIds.delete(cid);
  else expandedCourseIds.add(cid);
  renderActiveCourses();
  renderHiddenCourses();
  updateQuickCheckButtons();
}

function expandCollapseAll(open, btnEl) {
  expandedCourseIds.clear();
  const actives = allCourses.filter(c => c.tab_group === 'ACTIVE');
  if (open) {
    actives.forEach(c => expandedCourseIds.add(c.course_id));
    lastQuickCheckMessage = `📂 Expanded all ${actives.length} active courses (${checkedKeys.size} items checked)`;
  } else {
    lastQuickCheckMessage = `📁 Collapsed all ${actives.length} active courses (${checkedKeys.size} items checked)`;
  }
  renderActiveCourses();
  updateQuickCheckButtons();
  flashQcButton(btnEl || document.getElementById(open ? 'qc-expand-all' : 'qc-collapse-all'));
}

function setCourseSubTab(cid, tabName) {
  perCourseSubTab[cid] = tabName;
  renderActiveCourses();
  renderHiddenCourses();
}

function applyQuickCheck(mode, btnEl) {
  checkedKeys.clear();
  if (mode === 'MISSING_ONLY') {
    allCourses.forEach(c => {
      c.items.forEach(it => {
        if (!it.on_disk && it.source !== 'LOCAL_ONLY') checkedKeys.add(it.key);
      });
    });
    perCourseSubTab = {};
    lastQuickCheckMessage = `🟡 Selected ${checkedKeys.size} missing items (will download across active courses)`;
  } else if (mode === 'ALL_ITEMS') {
    allCourses.forEach(c => {
      c.items.forEach(it => {
        if (it.source !== 'LOCAL_ONLY') checkedKeys.add(it.key);
      });
    });
    perCourseSubTab = {};
    lastQuickCheckMessage = `☑ Selected all ${checkedKeys.size} downloadable Moodle items`;
  } else {
    lastQuickCheckMessage = `☐ Unchecked all items (0 selected)`;
  }
  activeQuickCheckMode = mode;
  renderAll();
  flashQcButton(btnEl || document.getElementById('qc-' + mode));
}

function checkCategoryGlobal(cat, onlyThisCategory, btnEl) {
  if (onlyThisCategory) checkedKeys.clear();
  let matchedCount = 0;
  let coursesWithCat = 0;
  allCourses.forEach(c => {
    if (c.tab_group !== 'ACTIVE') return;
    let hasCat = false;
    c.items.forEach(it => {
      if (it.source === 'LOCAL_ONLY') return;
      if (cat === 'ALL' || it.category === cat) {
        checkedKeys.add(it.key);
        matchedCount++;
        hasCat = true;
      }
    });
    if (hasCat) {
      coursesWithCat++;
      perCourseSubTab[c.course_id] = cat;
    } else if (onlyThisCategory) {
      perCourseSubTab[c.course_id] = 'ALL';
    }
  });
  if (cat !== 'ALL' && !activeCategories.has(cat)) {
    activeCategories.add(cat);
  }
  activeQuickCheckMode = onlyThisCategory ? ('CAT::' + cat) : 'CUSTOM';
  lastQuickCheckMessage = `☑ Selected ${matchedCount} "${cat}" items across ${coursesWithCat} courses (${checkedKeys.size} total checked)`;
  renderAll();
  flashQcButton(btnEl || document.getElementById('qc-cat-' + cat));
}

function checkCourseTab(cid, cat, isChecked) {
  const c = allCourses.find(x => x.course_id === cid);
  if (!c) return;
  c.items.forEach(it => {
    if (it.source === 'LOCAL_ONLY') return;
    if (cat === 'ALL' || it.category === cat) {
      if (isChecked) checkedKeys.add(it.key);
      else checkedKeys.delete(it.key);
    }
  });
  activeQuickCheckMode = checkedKeys.size === 0 ? 'NONE' : 'CUSTOM';
  lastQuickCheckMessage = `${isChecked ? '☑ Checked' : '☐ Unchecked'} ${cat === 'ALL' ? 'all items' : cat} in ${c.code} (${checkedKeys.size} total checked)`;
  renderAll();
}

function checkOnlyCourseTab(cid, cat) {
  const c = allCourses.find(x => x.course_id === cid);
  if (!c) return;
  c.items.forEach(it => {
    if (it.source === 'LOCAL_ONLY') return;
    if (cat === 'ALL' || it.category === cat) checkedKeys.add(it.key);
    else checkedKeys.delete(it.key);
  });
  perCourseSubTab[cid] = cat;
  activeQuickCheckMode = checkedKeys.size === 0 ? 'NONE' : 'CUSTOM';
  lastQuickCheckMessage = `⚡ Selected ONLY "${cat === 'ALL' ? 'All Content' : cat}" inside ${c.code} (${checkedKeys.size} total checked)`;
  renderAll();
}

function toggleVisibleCourseItems(cid, isChecked) {
  const c = allCourses.find(x => x.course_id === cid);
  if (!c) return;
  const visibleItems = (c.items || []).filter(it => itemMatchesGlobalAndCourseFilter(it, cid));
  visibleItems.forEach(it => {
    if (it.source === 'LOCAL_ONLY') return;
    if (isChecked) checkedKeys.add(it.key);
    else checkedKeys.delete(it.key);
  });
  activeQuickCheckMode = checkedKeys.size === 0 ? 'NONE' : 'CUSTOM';
  lastQuickCheckMessage = `${isChecked ? '☑ Checked' : '☐ Unchecked'} visible items in ${c.code} (${checkedKeys.size} total checked)`;
  renderAll();
}

function toggleSingleItem(key, isChecked) {
  if (isChecked) checkedKeys.add(key);
  else checkedKeys.delete(key);
  syncQuickCheckAfterManualEdit();
}

function toggleCourseMissingOrFiltered(cid, isChecked) {
  const c = allCourses.find(x => x.course_id === cid);
  if (!c) return;
  const hasMissing = c.items.some(it => !it.on_disk && it.source !== 'LOCAL_ONLY');
  c.items.forEach(it => {
    if (it.source === 'LOCAL_ONLY') return;
    if (itemMatchesGlobalAndCourseFilter(it, cid)) {
      if (isChecked) {
        if (!hasMissing || !it.on_disk) checkedKeys.add(it.key);
      } else {
        checkedKeys.delete(it.key);
      }
    }
  });
  activeQuickCheckMode = checkedKeys.size === 0 ? 'NONE' : 'CUSTOM';
  lastQuickCheckMessage = `${isChecked ? '☑ Checked' : '☐ Unchecked'} items in ${c.code} (${checkedKeys.size} total checked)`;
  renderAll();
}

function updateCheckedCountButton() {
  document.getElementById('checkedCountBtn').textContent = checkedKeys.size;
}

function buildCourseCardHtml(c) {
  const cid = c.course_id;
  const isExpanded = expandedCourseIds.has(cid);
  const currentSubTab = perCourseSubTab[cid] || 'ALL';
  const visibleItems = (c.items || []).filter(it => itemMatchesGlobalAndCourseFilter(it, cid));
  const downloadableVisible = visibleItems.filter(it => it.source !== 'LOCAL_ONLY');
  const allVisibleChecked = downloadableVisible.length > 0 && downloadableVisible.every(it => checkedKeys.has(it.key));
  const bd = c.category_breakdown || {};

  const subTabsHtml = ['ALL', ...ALL_CATEGORIES].map(cat => {
    const totalInCat = cat === 'ALL' ? c.total_items : ((bd[cat] && bd[cat].total) || 0);
    if (cat !== 'ALL' && totalInCat === 0) return '';
    const catDownloadables = (c.items || []).filter(it => it.source !== 'LOCAL_ONLY' && (cat === 'ALL' || it.category === cat));
    const catAllChecked = catDownloadables.length > 0 && catDownloadables.every(it => checkedKeys.has(it.key));
    const label = cat === 'ALL' ? `All Content (${totalInCat})` : `${cat} (${totalInCat})`;
    return `<div class="folder-tab ${currentSubTab === cat ? 'active' : ''}"
                 style="display:inline-flex; align-items:center; gap:6px;"
                 onclick="event.stopPropagation(); setCourseSubTab(${cid}, '${cat}')">
              <input type="checkbox" ${catAllChecked ? 'checked' : ''}
                     title="Check/Uncheck all in ${cat}"
                     onclick="event.stopPropagation(); checkCourseTab(${cid}, '${cat}', this.checked)">
              <span>${label}</span>
            </div>`;
  }).join('');

  const activeTabLabel = currentSubTab === 'ALL' ? 'All Content' : currentSubTab;
  const tabDownloadables = (c.items || []).filter(it => it.source !== 'LOCAL_ONLY' && (currentSubTab === 'ALL' || it.category === currentSubTab));
  const otherDownloadables = (c.items || []).filter(it => it.source !== 'LOCAL_ONLY' && currentSubTab !== 'ALL' && it.category !== currentSubTab);
  const allTabChecked = tabDownloadables.length > 0 && tabDownloadables.every(it => checkedKeys.has(it.key));
  const noneTabChecked = tabDownloadables.length > 0 && tabDownloadables.every(it => !checkedKeys.has(it.key));
  const onlyTabChecked = allTabChecked && otherDownloadables.every(it => !checkedKeys.has(it.key)) && currentSubTab !== 'ALL';

  return `
    <div class="course-card ${isExpanded ? 'expanded' : ''}" id="ccard-${cid}">
      <div class="course-card-header" onclick="toggleCourseExpand(${cid})">
        <div style="display:flex; align-items:center; gap:12px; flex:1;">
          <input type="checkbox" ${c.items.some(it => checkedKeys.has(it.key)) ? 'checked' : ''}
                 title="Check/Uncheck files in this course"
                 onclick="event.stopPropagation(); toggleCourseMissingOrFiltered(${cid}, this.checked)">
          <div>
            <div style="display:flex; gap:8px; align-items:center; margin-bottom:4px; flex-wrap:wrap;">
              <span class="course-code-badge">${c.code}</span>
              <span class="course-code-badge kind-${c.section_kind}">${c.section_kind}</span>
              <span style="font-size:12px; color:var(--muted);">ID: ${cid}</span>
              <span style="font-size:12px; color:#60a5fa;">📁 ${c.local_path}</span>
            </div>
            <div style="font-size:16px; font-weight:700;">${c.fullname}</div>
          </div>
        </div>
        <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
          <span class="badge badge-new">${c.missing_count} Will Download</span>
          <span class="badge badge-disk">${c.existing_count} Already in Folder</span>
          <button type="button" class="btn ${isExpanded ? 'btn-qc qc-active' : 'btn-outline'}" onclick="event.stopPropagation(); toggleCourseExpand(${cid})">
            ${isExpanded ? 'Hide Content ▴' : 'Open Course Content ▾'}
          </button>
        </div>
      </div>

      <div class="course-drawer">
        <div class="subtabs-row" style="gap:10px;">
          <div style="display:flex; gap:6px; flex-wrap:wrap;">
            ${subTabsHtml}
          </div>
          <div style="display:flex; gap:7px; align-items:center; flex-wrap:wrap;">
            <button type="button" class="btn btn-qc qc-cat ${allTabChecked && !onlyTabChecked ? 'qc-active' : ''}" style="padding:5px 10px; font-size:12px;"
                    onclick="event.stopPropagation(); checkCourseTab(${cid}, '${currentSubTab}', true)">
              ☑ Check All in ${activeTabLabel}
            </button>
            <button type="button" class="btn btn-qc ${onlyTabChecked ? 'qc-active-emerald' : ''}" style="padding:5px 10px; font-size:12px; border-color:rgba(16,185,129,0.4); color:#6ee7b7;"
                    onclick="event.stopPropagation(); checkOnlyCourseTab(${cid}, '${currentSubTab}')">
              ⚡ Check ONLY ${activeTabLabel}
            </button>
            <button type="button" class="btn btn-qc ${noneTabChecked ? 'qc-active-slate' : ''}" style="padding:5px 10px; font-size:12px;"
                    onclick="event.stopPropagation(); checkCourseTab(${cid}, '${currentSubTab}', false)">
              ☐ Uncheck ${activeTabLabel}
            </button>
            <span style="font-size:12px; color:var(--muted); margin-left:4px;">
              Showing <b>${visibleItems.length}</b> items
            </span>
          </div>
        </div>

        ${visibleItems.length === 0 ? `
          <div style="padding:20px; text-align:center; color:var(--muted); font-size:13px;">
            <div style="margin-bottom:10px;">No files match the current filter in this folder tab.</div>
            <button type="button" class="btn btn-outline" onclick="resetAllFilters(${cid})">Reset Filters & Show All (${c.total_items}) Files</button>
          </div>
        ` : `
          <div style="max-height:480px; overflow-y:auto;">
            <table class="items-table">
              <thead>
                <tr>
                  <th style="width:52px;">
                    <label style="display:flex; align-items:center; gap:5px; cursor:pointer;" title="Check/Uncheck all shown in this tab">
                      <input type="checkbox" ${allVisibleChecked ? 'checked' : ''}
                             onchange="toggleVisibleCourseItems(${cid}, this.checked)">
                      <span>DL</span>
                    </label>
                  </th>
                  <th>File / Lecture / Video Name</th>
                  <th>Folder Category</th>
                  <th>Media Type</th>
                  <th>Path in Course Folder</th>
                  <th>Size</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                ${visibleItems.map(it => `
                  <tr>
                    <td>
                      ${it.source === 'LOCAL_ONLY' ? '' : `
                        <input type="checkbox" ${checkedKeys.has(it.key) ? 'checked' : ''}
                               data-key="${encodeURIComponent(it.key)}"
                               onchange="toggleSingleItem(decodeURIComponent(this.dataset.key), this.checked)">
                      `}
                    </td>
                    <td>
                      <div style="font-weight:600;">${it.title}</div>
                      ${it.filename && it.filename !== it.title ? `<div style="font-size:11px; color:var(--muted);">${it.filename}</div>` : ''}
                    </td>
                    <td><span class="badge badge-cat">${it.category}</span></td>
                    <td><span class="badge badge-type">${it.media_type}</span></td>
                    <td style="font-family:Consolas,monospace; font-size:12px; color:#93c5fd;">${it.local_rel_path}</td>
                    <td style="font-size:12px; color:var(--muted);">${formatBytes(it.size_bytes)}</td>
                    <td>
                      ${!it.on_disk
                        ? `<span class="badge badge-new">🟡 Will Download (Missing)</span>`
                        : (it.source === 'MOODLE_SYNCED'
                            ? `<span class="badge badge-disk" title="${it.reason}">🟢 Already Downloaded (On Moodle & Disk)</span>`
                            : `<span class="badge badge-local" title="${it.reason}">📁 In Your Folder (Local File)</span>`)}
                    </td>
                  </tr>
                `).join('')}
              </tbody>
            </table>
          </div>
        `}
      </div>
    </div>
  `;
}

function renderActiveCourses() {
  const list = document.getElementById('activeCoursesList');
  const actives = allCourses.filter(c => c.tab_group === 'ACTIVE');
  list.innerHTML = actives.map(buildCourseCardHtml).join('');
}

async function scanHiddenCourseOnDemand(cid) {
  const res = await fetch('/api/scan', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      course_ids: [cid],
      workspace: document.getElementById('workspaceInput').value
    })
  });
  const data = await res.json();
  if (data.ok && data.scanned_courses && data.scanned_courses.length) {
    const updated = data.scanned_courses[0];
    // Keep it in HIDDEN tab if it's past or empty, but mark it scanned and expand it
    updated.tab_group = 'HIDDEN';
    const idx = allCourses.findIndex(x => x.course_id === cid);
    if (idx >= 0) allCourses[idx] = updated;
    expandedCourseIds.add(cid);
    renderAll();
  }
}

function renderHiddenCourses() {
  ['ALL', 'EMPTY', 'PAST'].forEach(m => {
    const b = document.getElementById('hsub-' + m);
    if (b) b.classList.toggle('qc-active', hiddenSubFilter === m);
  });
  const list = document.getElementById('hiddenCoursesList');
  const q = (document.getElementById('hiddenSearchBox').value || '').trim().toLowerCase();
  const hiddens = allCourses.filter(c => {
    if (c.tab_group !== 'HIDDEN') return false;
    if (hiddenSubFilter === 'EMPTY' && c.is_past) return false;
    if (hiddenSubFilter === 'PAST' && !c.is_past) return false;
    if (q && !c.fullname.toLowerCase().includes(q) && !c.code.toLowerCase().includes(q)) return false;
    return true;
  });

  list.innerHTML = hiddens.map(c => {
    if (c.scanned && c.total_items > 0) {
      return buildCourseCardHtml(c);
    }
    return `
      <div class="course-card">
        <div class="course-card-header" style="cursor:default;">
          <div>
            <div style="display:flex; gap:8px; align-items:center; margin-bottom:4px;">
              <span class="course-code-badge">${c.code}</span>
              <span class="course-code-badge kind-${c.section_kind}">${c.section_kind}</span>
              <span class="course-code-badge kind-HIDDEN">${c.hidden_reason}</span>
              <span style="font-size:12px; color:var(--muted);">ID: ${c.course_id}</span>
            </div>
            <div style="font-size:15px; font-weight:700;">${c.fullname}</div>
          </div>
          <div>
            ${c.scanned && c.moodle_items_count === 0
              ? `<span class="badge kind-HIDDEN">Scanner confirmed: 0 files in this section shell</span>`
              : `<button class="btn btn-outline" onclick="scanHiddenCourseOnDemand(${c.course_id})">Scan & Inspect Content ▾</button>`}
          </div>
        </div>
      </div>
    `;
  }).join('');
}

async function startDownloadSelected() {
  if (!checkedKeys.size) {
    alert('No missing files are checked for download. All your current courses are already 100% up to date (or check any item you wish to download).');
    return;
  }
  const res = await fetch('/api/sync', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      selected_keys: Array.from(checkedKeys),
      extract_zips: document.getElementById('extractZipsToggle').checked
    })
  });
  const data = await res.json();
  if (!data.ok) {
    alert('Error starting download: ' + data.error);
    return;
  }
  document.getElementById('step4Card').classList.remove('hidden');
  document.getElementById('step4Card').scrollIntoView({behavior: 'smooth'});
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(pollSyncProgress, 800);
}

async function pollSyncProgress() {
  try {
    const res = await fetch('/api/progress');
    const p = await res.json();
    const pct = p.total > 0 ? Math.round((p.completed / p.total) * 100) : 0;
    document.getElementById('syncCounterBadge').textContent = `${p.completed} / ${p.total} (${pct}%)`;
    document.getElementById('currentFileLabel').textContent = p.current_title || '';
    document.getElementById('syncProgressBar').style.width = `${pct}%`;
    const logBox = document.getElementById('syncLogBox');
    logBox.textContent = (p.logs || []).join('\n');
    logBox.scrollTop = logBox.scrollHeight;
    if (!p.running && p.total > 0 && p.completed >= p.total) {
      clearInterval(pollTimer);
      document.getElementById('syncStageTitle').textContent = 'Synchronization Complete!';
    }
  } catch (e) {}
}

initApp();
</script>
<footer style="position:fixed; bottom:0; left:0; right:0; background:rgba(11,15,25,0.95); border-top:1px solid var(--border); padding:10px 24px; display:flex; justify-content:center; align-items:center; gap:18px; flex-wrap:wrap; font-size:13px; z-index:60; backdrop-filter:blur(8px);">
  <span style="color:#93c5fd; font-weight:700;">✨ Made by : farisEmad</span>
  <span style="color:var(--muted);">|</span>
  <a href="https://wa.me/201020933516" target="_blank" rel="noopener" style="color:#34d399; font-weight:700; text-decoration:none;">
    💬 For any bug or recommendation : WhatsApp 01020933516
  </a>
</footer>
</body>
</html>
"""


# ==============================================================================
# HTTP SERVER HANDLER
# ==============================================================================

class MoodleAppRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _send_json(self, data: Dict[str, Any], status: int = 200) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    APP_ICON_BYTES = __import__("base64").b64decode("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")

    def do_GET(self) -> None:
        if self.path in ("/icon.png", "/favicon.ico"):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(self.APP_ICON_BYTES)))
            self.end_headers()
            self.wfile.write(self.APP_ICON_BYTES)
            return
        if self.path in ("/", "/index.html"):
            raw = DASHBOARD_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        elif self.path == "/api/status":
            cached = load_session_cache()
            has_cached = bool(isinstance(cached, dict) and cached.get("moodle"))
            cached_email = (cached.get("account_email") or "").strip() if isinstance(cached, dict) else ""
            self._send_json({
                "ok": True,
                "email": STATE.current_user or cached_email,
                "workspace": str(STATE.workspace),
                "has_cached_session": has_cached,
            })
        elif self.path == "/api/progress":
            with STATE.lock:
                self._send_json({
                    "ok": True,
                    "running": STATE.sync_running,
                    "total": STATE.sync_total,
                    "completed": STATE.sync_completed,
                    "current_title": STATE.sync_current_title,
                    "bytes_downloaded": STATE.sync_bytes,
                    "logs": list(STATE.sync_logs),
                    "results": list(STATE.sync_results),
                    "error": STATE.sync_error,
                })
        else:
            self._send_json({"ok": False, "error": "Not found"}, status=404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        try:
            payload = json.loads(body)
        except Exception:
            payload = {}

        try:
            if self.path == "/api/login":
                res = handle_login_and_initial_scan(payload)
                self._send_json(res)
            elif self.path == "/api/scan":
                res = handle_scan_specific(payload)
                self._send_json(res)
            elif self.path == "/api/sync":
                res = handle_start_sync(payload)
                self._send_json(res)
            elif self.path == "/api/browse_dirs":
                res = handle_browse_dirs(payload)
                self._send_json(res)
            elif self.path == "/api/create_dir":
                res = handle_create_dir(payload)
                self._send_json(res)
            elif self.path == "/api/logout":
                res = handle_logout(payload)
                self._send_json(res)
            else:
                self._send_json({"ok": False, "error": "Unknown endpoint"}, status=404)
        except AuthenticationError as ae:
            logger.warning(f"Authentication rejected on {self.path} [{ae.code}]: {ae}")
            self._send_json({
                "ok": False,
                "error": str(ae),
                "error_code": ae.code,
                "error_field": ae.field,
            }, status=401)
        except Exception as e:
            err_msg = str(e)
            if self.path == "/api/login":
                err_code, err_field = classify_auth_error(err_msg)
                status_code = 401 if err_code in ("WRONG_PASSWORD", "WRONG_EMAIL", "MFA_REQUIRED", "MISSING_CREDENTIALS") else 500
            else:
                err_code, err_field, status_code = ("SERVER_ERROR", "general", 500)
            logger.error(f"API error on {self.path}: {e}\n{traceback.format_exc()}")
            self._send_json({
                "ok": False,
                "error": err_msg,
                "error_code": err_code,
                "error_field": err_field,
            }, status=status_code)


def find_free_port(preferred: int = 8765) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex(("127.0.0.1", preferred)) != 0:
            return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="Launch the Nile University Moodle Sync Studio Web App")
    parser.add_argument("--port", type=int, default=8765, help="Port to run the local web dashboard on")
    parser.add_argument("--no-browser", action="store_true", help="Do not automatically open the browser")
    args = parser.parse_args()

    port = find_free_port(args.port)
    url = f"http://127.0.0.1:{port}"
    server = ThreadingHTTPServer(("127.0.0.1", port), MoodleAppRequestHandler)

    print("=" * 72)
    print("         Nile University Moodle Sync Studio - Web Dashboard")
    print("         Made by : farisEmad")
    print("         For any bug or recommendation : WhatsApp 01020933516")
    print("=" * 72)
    print(f"  Dashboard URL : {url}")
    print(f"  Study Folder  : {STATE.workspace}")
    print("  Press Ctrl+C to stop the server.")
    print("=" * 72)

    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down Moodle Sync Studio...")
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
