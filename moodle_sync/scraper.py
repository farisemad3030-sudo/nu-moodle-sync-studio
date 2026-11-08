"""
moodle_sync/scraper.py
High-performance DOM scraper for Nile University Moodle courses.
Extracts sections, mod_resource, mod_folder (standard and inline),
mod_assign instructor attachments, and mod_url activities.
"""

import os
import logging
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Any, Tuple, Union

import requests
from bs4 import BeautifulSoup

from moodle_sync.config import (
    BASE_URL,
    BASE_STUDY_DIR,
    COURSES_CONFIG,
    TARGET_COURSE_IDS,
    DEFAULT_TIMEOUT,
    resolve_destination_subfolder,
    normalize_course_id,
    register_dynamic_course,
    infer_course_code_and_folder,
)

logger = logging.getLogger("moodle_sync.scraper")

class ResourceType(str, Enum):
    RESOURCE = "mod_resource"
    FOLDER = "mod_folder"
    INLINE_FOLDER = "mod_inline_folder"
    ASSIGNMENT = "mod_assign"
    URL = "mod_url"
    OTHER = "other"

    def __str__(self) -> str:
        return self.value

@dataclass
class MoodleItem:
    id: str
    name: str
    url: str
    resource_type: Union[ResourceType, str]
    course_id: int
    section_id: str
    section_title: str
    target_subfolder: str
    filename: str = ""
    filesize: Optional[int] = None
    mimetype: Optional[str] = None
    is_external: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def title(self) -> str:
        return self.name

    @property
    def section_name(self) -> str:
        return self.section_title

    @property
    def activity_type(self) -> str:
        return str(self.resource_type)

    @property
    def download_url(self) -> str:
        return self.url

# Alias for backward compatibility
MoodleResource = MoodleItem

@dataclass
class SectionData:
    section_id: str
    title: str
    items: List[MoodleItem] = field(default_factory=list)

@dataclass
class CourseData:
    course_id: int
    course_code: str
    course_name: str
    local_folder: str
    sections: List[SectionData] = field(default_factory=list)
    all_items: List[MoodleItem] = field(default_factory=list)

    @property
    def resources(self) -> List[MoodleItem]:
        return self.all_items

    @property
    def downloadable_items(self) -> List[MoodleItem]:
        return [it for it in self.all_items if not it.is_external]

    @property
    def external_links(self) -> List[MoodleItem]:
        return [it for it in self.all_items if it.is_external]

# --- Helper Utilities ---

def make_soup(html_text: Optional[str]) -> BeautifulSoup:
    """Safely construct BeautifulSoup with lxml, sanitizing lone Unicode surrogates."""
    if not html_text:
        return BeautifulSoup("", "lxml")
    sanitized = html_text.encode("utf-8", errors="replace").decode("utf-8")
    return BeautifulSoup(sanitized, "lxml")

def clean_text(text: Optional[str]) -> str:
    """Strips whitespace and non-breaking space anomalies."""
    if not text:
        return ""
    return re.sub(r'\s+', ' ', text).strip()

def extract_filename_from_url(url: str) -> str:
    """Extracts decoded filename stem and extension from URL path."""
    parsed = urllib.parse.urlparse(url)
    path = urllib.parse.unquote(parsed.path)
    parts = [p for p in path.split("/") if p]
    if parts:
        last = parts[-1]
        if "." in last:
            return last
    return ""

def extract_filename_from_cd(content_disposition: str) -> str:
    """Extracts filename from Content-Disposition header."""
    if not content_disposition:
        return ""
    # Matches filename*=UTF-8''... or filename="..."
    m_utf8 = re.search(r"filename\*\s*=\s*(?:UTF-8''|utf-8'')([^;]+)", content_disposition, re.I)
    if m_utf8:
        return urllib.parse.unquote(m_utf8.group(1).strip('"\''))
    m_std = re.search(r'filename\s*=\s*"([^"]+)"', content_disposition, re.I)
    if m_std:
        return m_std.group(1).strip()
    m_simple = re.search(r'filename\s*=\s*([^;]+)', content_disposition, re.I)
    if m_simple:
        return m_simple.group(1).strip('"\' ')
    return ""

# --- Activity Parsers ---

def parse_mod_resource(
    activity_el,
    course_id: int,
    section_id: str,
    section_title: str,
) -> Optional[MoodleItem]:
    """Parses a mod_resource activity element."""
    link_el = activity_el.find("a", href=lambda h: h and "/mod/resource/view.php" in h)
    if not link_el:
        return None
    url = link_el["href"]
    act_id = activity_el.get("id", "") or (url.split("id=")[-1] if "id=" in url else "res")

    inst = activity_el.find(class_="instancename")
    raw_name = inst.get_text() if inst else link_el.get_text()
    for ah in activity_el.find_all(class_="accesshide"):
        raw_name = raw_name.replace(ah.get_text(), "")
    name = clean_text(raw_name)

    subfolder = resolve_destination_subfolder(course_id, section_title, name)

    return MoodleItem(
        id=act_id,
        name=name,
        url=url,
        resource_type=ResourceType.RESOURCE,
        course_id=course_id,
        section_id=section_id,
        section_title=section_title,
        target_subfolder=subfolder,
        filename=name,
    )

def parse_inline_folder_items(
    activity_el,
    course_id: int,
    section_id: str,
    section_title: str,
) -> List[MoodleItem]:
    """
    Parses an inline folder activity (e.g. ECE342 Model Answers)
    where pluginfile.php links are embedded directly in the course page DOM.
    """
    items: List[MoodleItem] = []
    plugin_links = activity_el.find_all("a", href=lambda h: h and "pluginfile.php" in h)
    if not plugin_links:
        return []

    # Strip accesshide elements
    for ah in activity_el.find_all(class_="accesshide"):
        ah.decompose()

    act_id = activity_el.get("id", "folder")
    inst = activity_el.find(class_="instancename")
    folder_name = clean_text(inst.get_text()) if inst else section_title
    GENERIC_UI_TEXTS = {"download", "download folder", "view", "open", "file", "download file", "get"}

    for idx, pl in enumerate(plugin_links):
        url = pl["href"]
        url_fn = extract_filename_from_url(url)
        raw_fn = clean_text(pl.get_text())
        if raw_fn.lower() in GENERIC_UI_TEXTS or not os.path.splitext(raw_fn)[1]:
            fn = url_fn or raw_fn or f"file_{idx}"
        else:
            fn = raw_fn or url_fn or f"file_{idx}"
        subfolder = resolve_destination_subfolder(course_id, section_title, folder_name, fn)
        items.append(
            MoodleItem(
                id=f"{act_id}_file_{idx}",
                name=fn,
                url=url,
                resource_type=ResourceType.INLINE_FOLDER,
                course_id=course_id,
                section_id=section_id,
                section_title=section_title,
                target_subfolder=subfolder,
                filename=fn,
                metadata={"folder_title": folder_name},
            )
        )
    return items

def parse_standard_folder(
    session: requests.Session,
    activity_el,
    course_id: int,
    section_id: str,
    section_title: str,
) -> List[MoodleItem]:
    """
    Parses a standard mod_folder activity by requesting /mod/folder/view.php
    and extracting all contained individual files.
    """
    link_el = activity_el.find("a", href=lambda h: h and "/mod/folder/view.php" in h)
    if not link_el:
        return []
    folder_url = link_el["href"]
    act_id = activity_el.get("id", "") or (folder_url.split("id=")[-1] if "id=" in folder_url else "folder")
    inst = activity_el.find(class_="instancename")
    folder_name = clean_text(inst.get_text()) if inst else "Folder"

    items: List[MoodleItem] = []
    try:
        r = session.get(folder_url, timeout=DEFAULT_TIMEOUT)
        if r.status_code != 200:
            logger.warning(f"Failed to fetch folder {folder_url}: status {r.status_code}")
            return []
        soup = make_soup(r.text)

        # Strip accesshide elements from parsed folder view
        for ah in soup.find_all(class_="accesshide"):
            ah.decompose()

        GENERIC_UI_TEXTS = {"download", "download folder", "view", "open", "file", "download file", "get"}
        file_links = soup.find_all("a", href=lambda h: h and "pluginfile.php" in h)
        for idx, fl in enumerate(file_links):
            f_url = fl["href"]
            url_fn = extract_filename_from_url(f_url)
            fp_span = fl.find(class_="fp-filename") or (fl.parent and fl.parent.find(class_="fp-filename"))
            if fp_span:
                raw_fn = clean_text(fp_span.get_text())
            else:
                raw_fn = clean_text(fl.get_text())

            if raw_fn.lower() in GENERIC_UI_TEXTS or not os.path.splitext(raw_fn)[1]:
                fn = url_fn or raw_fn or f"folder_file_{idx}"
            else:
                fn = raw_fn or url_fn or f"folder_file_{idx}"
            subfolder = resolve_destination_subfolder(course_id, section_title, folder_name, fn)
            items.append(
                MoodleItem(
                    id=f"{act_id}_item_{idx}",
                    name=fn,
                    url=f_url,
                    resource_type=ResourceType.FOLDER,
                    course_id=course_id,
                    section_id=section_id,
                    section_title=section_title,
                    target_subfolder=subfolder,
                    filename=fn,
                    metadata={"folder_url": folder_url, "folder_name": folder_name},
                )
            )
    except Exception as e:
        logger.error(f"Error scraping standard folder {folder_url}: {e}")
    return items

def parse_mod_assign_attachments(
    session: requests.Session,
    activity_el,
    course_id: int,
    section_id: str,
    section_title: str,
) -> List[MoodleItem]:
    """
    Parses a mod_assign activity page. Scrapes instructor prompt files and sheets
    located in intro and introattachment, strictly ignoring student submissions.
    """
    link_el = activity_el.find("a", href=lambda h: h and "/mod/assign/view.php" in h)
    if not link_el:
        return []
    assign_url = link_el["href"]
    act_id = activity_el.get("id", "") or (assign_url.split("id=")[-1] if "id=" in assign_url else "assign")
    inst = activity_el.find(class_="instancename")
    assign_name = clean_text(inst.get_text()) if inst else "Assignment"

    items: List[MoodleItem] = []
    try:
        r = session.get(assign_url, timeout=DEFAULT_TIMEOUT)
        if r.status_code != 200:
            return []
        soup = make_soup(r.text)

        all_links = soup.find_all("a", href=lambda h: h and "pluginfile.php" in h)
        seen_urls = set()

        for idx, al in enumerate(all_links):
            href = al["href"]

            # Filter rule: MUST contain /intro/ or /introattachment/
            # Strictly EXCLUDE /assignsubmission_file/
            if "/assignsubmission_file/" in href:
                continue
            if not ("/mod_assign/intro/" in href or "/mod_assign/introattachment/" in href):
                continue

            canon_url = href.split("?")[0]
            if canon_url in seen_urls:
                continue
            seen_urls.add(canon_url)

            fn = clean_text(al.get_text()) or extract_filename_from_url(href)
            subfolder = resolve_destination_subfolder(course_id, section_title, assign_name, fn)

            items.append(
                MoodleItem(
                    id=f"{act_id}_att_{idx}",
                    name=fn,
                    url=href,
                    resource_type=ResourceType.ASSIGNMENT,
                    course_id=course_id,
                    section_id=section_id,
                    section_title=section_title,
                    target_subfolder=subfolder,
                    filename=fn,
                    metadata={"assign_name": assign_name, "assign_url": assign_url},
                )
            )
    except Exception as e:
        logger.error(f"Error scraping assignment {assign_url}: {e}")
    return items

def parse_mod_url(
    session: requests.Session,
    activity_el,
    course_id: int,
    section_id: str,
    section_title: str,
) -> Optional[MoodleItem]:
    """Parses a mod_url activity and resolves destination target URL."""
    link_el = activity_el.find("a", href=lambda h: h and "/mod/url/view.php" in h)
    if not link_el:
        # Check direct link if already pointing to external
        direct_link = activity_el.find("a")
        if direct_link and direct_link.get("href"):
            link_el = direct_link
        else:
            return None

    url_activity_link = link_el["href"]
    act_id = activity_el.get("id", "") or (url_activity_link.split("id=")[-1] if "id=" in url_activity_link else "url")
    inst = activity_el.find(class_="instancename")
    raw_name = inst.get_text() if inst else link_el.get_text()
    for ah in activity_el.find_all(class_="accesshide"):
        raw_name = raw_name.replace(ah.get_text(), "")
    name = clean_text(raw_name)

    target_url = url_activity_link
    if "/mod/url/view.php" in url_activity_link:
        try:
            r = session.get(url_activity_link, allow_redirects=False, timeout=DEFAULT_TIMEOUT)
            if r.status_code in (301, 302, 303) and "Location" in r.headers:
                target_url = r.headers["Location"]
            elif r.status_code == 200:
                soup = make_soup(r.text)
                wa = soup.find(class_="urlworkaround")
                if wa and wa.find("a"):
                    target_url = wa.find("a")["href"]
        except Exception as e:
            logger.debug(f"Failed to resolve external redirect for {url_activity_link}: {e}")

    subfolder = resolve_destination_subfolder(course_id, section_title, name)
    return MoodleItem(
        id=act_id,
        name=name,
        url=target_url,
        resource_type=ResourceType.URL,
        course_id=course_id,
        section_id=section_id,
        section_title=section_title,
        target_subfolder=subfolder,
        filename=name,
        is_external=True,
        metadata={"moodle_activity_url": url_activity_link},
    )

# --- Enrichment: HEAD Metadata Resolution ---

def enrich_resource_metadata(session: requests.Session, item: MoodleItem) -> MoodleItem:
    """Queries HTTP HEAD to extract exact Content-Disposition filename and size."""
    if item.is_external or not item.url:
        return item
    try:
        resp = session.head(item.url, allow_redirects=True, timeout=DEFAULT_TIMEOUT)
        cd = resp.headers.get("Content-Disposition", "")
        real_fn = extract_filename_from_cd(cd)
        if not real_fn:
            real_fn = extract_filename_from_url(resp.url)
        if real_fn:
            item.filename = real_fn

        cl = resp.headers.get("Content-Length")
        if cl and cl.isdigit():
            item.filesize = int(cl)
        item.mimetype = resp.headers.get("Content-Type")
    except Exception as e:
        logger.debug(f"HEAD metadata enrichment failed for {item.url}: {e}")
    return item

# --- Top-Level Scraper APIs ---

def discover_enrolled_courses(
    session: requests.Session,
    base_dir=BASE_STUDY_DIR,
    include_hidden_and_past: bool = True,
) -> List[Dict[str, Any]]:
    """
    Discovers all courses the authenticated user is enrolled in on Moodle via
    Moodle's AJAX timeline service (including hidden/past courses when requested),
    registers them in COURSES_CONFIG, and returns rich metadata for UI selection.
    """
    from pathlib import Path
    bdir = Path(base_dir)
    courses_url = f"{BASE_URL}/my/courses.php"
    r = session.get(courses_url, timeout=DEFAULT_TIMEOUT)
    if r.status_code != 200:
        r = session.get(f"{BASE_URL}/my/", timeout=DEFAULT_TIMEOUT)

    m = re.search(r'"sesskey"\s*:\s*"([^"]+)"', r.text)
    sesskey = m.group(1) if m else None

    discovered: List[Dict[str, Any]] = []
    seen_ids = set()

    if sesskey:
        ajax_url = (
            f"{BASE_URL}/lib/ajax/service.php?"
            f"sesskey={sesskey}&info=core_course_get_enrolled_courses_by_timeline_classification"
        )
        clf = "allincludinghidden" if include_hidden_and_past else "all"
        payload = [{
            "index": 0,
            "methodname": "core_course_get_enrolled_courses_by_timeline_classification",
            "args": {
                "offset": 0,
                "limit": 0,
                "classification": clf,
                "sort": "fullname",
                "customfieldname": "",
                "customfieldvalue": "",
            },
        }]
        try:
            resp = session.post(ajax_url, json=payload, timeout=DEFAULT_TIMEOUT)
            data = resp.json()
            if isinstance(data, list) and data and not data[0].get("error"):
                raw_courses = data[0].get("data", {}).get("courses", [])
                for c in raw_courses:
                    cid = int(c["id"])
                    fullname = clean_text(c.get("fullname") or c.get("shortname") or f"Course {cid}")
                    category = clean_text(c.get("coursecategory") or "")
                    is_hidden_flag = bool(c.get("hidden", False))
                    cfg = register_dynamic_course(cid, fullname, base_dir=bdir)
                    upper_name = fullname.upper()
                    if "-LECT" in upper_name:
                        section_kind = "LECT"
                    elif "-LAB" in upper_name:
                        section_kind = "LAB"
                    elif "-TUT" in upper_name:
                        section_kind = "TUT"
                    elif "-PRAC" in upper_name:
                        section_kind = "PRAC"
                    else:
                        section_kind = "LECT"

                    term_match = re.search(r"(20\d{2}(?:FALL|SPRG|SPRING|SUMM|SUMMER|WINT))", upper_name)
                    term = term_match.group(1) if term_match else "UNKNOWN"
                    is_past = is_hidden_flag or (term != "2026FALL" and term != "UNKNOWN")

                    seen_ids.add(cid)
                    discovered.append({
                        "course_id": cid,
                        "code": cfg["code"],
                        "fullname": fullname,
                        "name": cfg["name"],
                        "category": category,
                        "section_kind": section_kind,
                        "term": term,
                        "is_past": is_past,
                        "is_hidden_moodle": is_hidden_flag,
                        "local_folder": cfg["folder"],
                        "url": c.get("viewurl") or cfg["url"],
                        "is_default_target": cid in TARGET_COURSE_IDS,
                    })
        except Exception as e:
            logger.warning(f"AJAX course discovery failed, falling back to HTML/defaults: {e}")

    # Ensure the 6 default courses are included if not already returned
    if not discovered:
        for cid, cfg in list(COURSES_CONFIG.items()):
            if cid not in seen_ids:
                fullname = f"{cfg['code']}: {cfg['name']}"
                discovered.append({
                    "course_id": cid,
                    "code": cfg["code"],
                    "fullname": fullname,
                    "name": cfg["name"],
                    "category": "Electronics and Computer Engineering(ECE)",
                    "section_kind": "LECT",
                    "term": "2026FALL",
                    "is_past": False,
                    "is_hidden_moodle": False,
                    "local_folder": cfg["folder"],
                    "url": cfg["url"],
                    "is_default_target": True,
                })

    return discovered


def scan_course(session: requests.Session, course_id: Union[int, str]) -> CourseData:
    """Scans and parses an entire Moodle course page and all contained activities."""
    cid = normalize_course_id(course_id)
    cfg = COURSES_CONFIG[cid]
    course_url = cfg["url"]
    logger.info(f"Scanning course {cfg['code']} (ID: {cid}) from {course_url}...")

    r = session.get(course_url, timeout=DEFAULT_TIMEOUT)
    if r.status_code != 200:
        raise ConnectionError(f"Failed to access course {cid}: HTTP {r.status_code}")

    soup = make_soup(r.text)
    sections_el = soup.find_all("li", class_="section")

    course_data = CourseData(
        course_id=cid,
        course_code=cfg["code"],
        course_name=cfg["name"],
        local_folder=cfg["folder"],
    )

    resources_to_enrich: List[MoodleItem] = []

    for sec in sections_el:
        sec_id = sec.get("id", "section-0")
        sec_title_el = sec.find(["h3", "h4"], class_="sectionname")
        sec_title = clean_text(sec_title_el.get_text()) if sec_title_el else "General"
        section_data = SectionData(section_id=sec_id, title=sec_title)

        activities = sec.find_all("li", class_="activity")
        for act in activities:
            classes = act.get("class", [])

            # 1. mod_resource
            if any("resource" in c for c in classes) or act.find("a", href=lambda h: h and "/mod/resource/view.php" in h):
                item = parse_mod_resource(act, cid, sec_id, sec_title)
                if item:
                    section_data.items.append(item)
                    resources_to_enrich.append(item)

            # 2. mod_folder (inline or standard)
            elif any("folder" in c for c in classes) or act.find("a", href=lambda h: h and "/mod/folder/view.php" in h):
                inline_items = parse_inline_folder_items(act, cid, sec_id, sec_title)
                if inline_items:
                    section_data.items.extend(inline_items)
                else:
                    std_items = parse_standard_folder(session, act, cid, sec_id, sec_title)
                    section_data.items.extend(std_items)

            # 3. mod_assign
            elif any("assign" in c for c in classes) or act.find("a", href=lambda h: h and "/mod/assign/view.php" in h):
                assign_items = parse_mod_assign_attachments(session, act, cid, sec_id, sec_title)
                section_data.items.extend(assign_items)

            # 4. mod_url
            elif any("url" in c for c in classes) or act.find("a", href=lambda h: h and "/mod/url/view.php" in h):
                url_item = parse_mod_url(session, act, cid, sec_id, sec_title)
                if url_item:
                    section_data.items.append(url_item)

        course_data.sections.append(section_data)
        course_data.all_items.extend(section_data.items)

    # Concurrency for HEAD enrichment
    if resources_to_enrich:
        logger.debug(f"Enriching {len(resources_to_enrich)} mod_resource items via HEAD...")
        with ThreadPoolExecutor(max_workers=6) as executor:
            list(executor.map(lambda it: enrich_resource_metadata(session, it), resources_to_enrich))

    logger.info(
        f"Course {cfg['code']} scan complete: {len(course_data.downloadable_items)} downloadable items, "
        f"{len(course_data.external_links)} external links across {len(course_data.sections)} sections."
    )
    return course_data

def scan_all_courses(
    session: requests.Session,
    course_ids: Optional[List[int]] = None,
) -> Dict[int, CourseData]:
    """Scans all specified courses (defaults to all 6 target courses)."""
    ids = course_ids or TARGET_COURSE_IDS
    results: Dict[int, CourseData] = {}
    for cid in ids:
        results[cid] = scan_course(session, cid)
    return results

class CourseScraper:
    """Class wrapper around course scanning for object-oriented workflows."""
    def __init__(self, session: requests.Session):
        self.session = session

    def scan_course(self, course_id: Union[int, str]) -> CourseData:
        return scan_course(self.session, course_id)

    def scan_all_courses(self, course_ids: Optional[List[int]] = None) -> Dict[int, CourseData]:
        return scan_all_courses(self.session, course_ids)

    @staticmethod
    def extract_filename(response: requests.Response) -> str:
        """Extract filename from response headers or URL."""
        cd = response.headers.get("Content-Disposition", "")
        fn = extract_filename_from_cd(cd)
        if not fn:
            fn = extract_filename_from_url(response.url)
        return fn
