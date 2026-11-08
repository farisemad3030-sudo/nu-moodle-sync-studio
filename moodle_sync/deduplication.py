"""
moodle_sync/deduplication.py
Authoritative 4-tier deduplication engine and local directory scanner.
"""

import os
import re
import urllib.parse
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Union, Any

from moodle_sync.config import (
    BASE_STUDY_DIR,
    COURSES_CONFIG,
    EXCLUDED_TOP_LEVEL_DIRS,
    IGNORED_FILE_PREFIXES,
    IGNORED_FILENAMES,
    normalize_course_id,
)

class MatchTier(str, Enum):
    TIER_1_CANONICAL = "TIER_1_CANONICAL"
    TIER_2_NORMALIZED = "TIER_2_NORMALIZED"
    TIER_3_SEMANTIC = "TIER_3_SEMANTIC"
    TIER_4_SIZE = "TIER_4_SIZE"
    NO_MATCH = "NO_MATCH"

    def __str__(self) -> str:
        return self.value

@dataclass(frozen=True)
class LocalFileInfo:
    path: Path
    rel_path: Path
    course_code: str
    course_name: str
    subfolder: str
    filename: str
    size_bytes: int
    mtime: float
    extension: str
    normalized_name: str
    tokens: Set[str] = field(default_factory=set)

@dataclass
class MatchResult:
    matched: bool = False
    tier: Union[MatchTier, str] = MatchTier.NO_MATCH
    matched_file: Optional[Union[LocalFileInfo, Path, str]] = None
    confidence: float = 0.0
    reason: str = ""
    action: str = "DOWNLOAD"  # "SKIP", "DOWNLOAD", "DOWNLOAD_VERSIONED"
    discrepancy: Optional[str] = None  # e.g. "SIZE_MISMATCH"
    size_bytes: int = 0

    @property
    def is_match(self) -> bool:
        return self.matched

# ==============================================================================
# NORMALIZATION UTILITIES
# ==============================================================================

STOPWORDS = {
    "the", "and", "in", "of", "to", "for", "with", "a", "an", "on", "at",
    "by", "fall", "fall26", "fall2026", "2026", "2027", "meeting", "recording",
    "video", "lecture", "lec", "tutorial", "tut", "sheet", "lab", "quiz", "exam", "file",
    "presentation", "slides", "notes", "handout"
}

def normalize_stem(stem: str) -> str:
    """Normalize stem for Tier 2 matching."""
    stem = urllib.parse.unquote(stem)
    s = unicodedata.normalize("NFKC", stem)
    s = s.lower()

    # Strip Arabic diacritics / tashkeel and tatweel
    s = re.sub(r"[\u064b-\u0652\u0670\u0640]", "", s)

    # Strip all browser copy duplicate suffixes: ' (1)', ' (2)', ' [1]'
    while True:
        new_s = re.sub(r"[\s_\-\.]*(?:\(\d+\)|\[\d+\])$", "", s)
        if new_s == s:
            break
        s = new_s

    s = re.sub(r"\s*\(copy\)$", "", s)
    s = re.sub(r"\s+copy(?:\s+\d+)?$", "", s)
    s = re.sub(r"\s*-\s*copy(?:\s+\d+)?$", "", s)

    # Replace non-alphanumeric separators (keep Arabic letters \u0600-\u06FF) with space
    # Crucial: replacing separators BEFORE word boundary substitutions ensures \b matches
    # tokens that were originally delimited by underscores or dashes (e.g. ECEN_322_Lecture_1).
    s = re.sub(r"[^a-z0-9\u0600-\u06ff]+", " ", s)

    # Expand course codes and common abbreviations
    s = re.sub(r"\becen\b", "ece", s)
    s = re.sub(r"\blec\b", "lecture", s)
    s = re.sub(r"\btut\b", "tutorial", s)
    s = re.sub(r"\bsolutions?\b", "solution", s)
    s = re.sub(r"\bch\b|\bchapter\b", "ch", s)

    # Remove leading zeros from standalone numbers: '01' -> '1'
    s = re.sub(r"\b0+(\d+)\b", r"\1", s)
    return " ".join(s.split())

def normalize_filename(name: str) -> str:
    """Normalize full filename (stem + lowercased extension)."""
    name = urllib.parse.unquote(name)
    norm = unicodedata.normalize("NFKC", name)
    stem, ext = os.path.splitext(norm)
    norm_stem = normalize_stem(stem)
    return f"{norm_stem}{ext.lower()}"

def extract_semantic_tokens(text: str) -> Tuple[Dict[str, int], Set[str]]:
    """Extract category numbers and keyword tokens for Tier 3 semantic matching."""
    text = urllib.parse.unquote(text)
    t = unicodedata.normalize("NFKC", text).lower()
    t = re.sub(r"[\u064b-\u0652\u0670\u0640]", "", t)
    t = re.sub(r"[\s_\-\.]*(?:\(\d+\)|\[\d+\])$", "", t)

    categories: Dict[str, int] = {}
    patterns = [
        (r"\b(?:lec|lecture)\s*0*(\d+)\b", "lecture"),
        (r"\b(?:sheet|tut|tutorial)\s*0*(\d+)\b", "sheet"),
        (r"\b(?:lab)\s*0*(\d+)\b", "lab"),
        (r"\b(?:quiz)\s*0*(\d+)\b", "quiz"),
        (r"\b(?:assign|assignment|hw)\s*0*(\d+)\b", "assignment"),
    ]
    for pat, cat in patterns:
        m = re.search(pat, t)
        if m:
            categories[cat] = int(m.group(1))

    # Clean words
    words = set(re.findall(r"[a-z0-9\u0600-\u06ff]+", t)) - STOPWORDS
    return categories, words

DOC_TYPE_PATTERNS = {
    "lecture": [r"\blec\b", r"\blecture\b"],
    "sheet": [r"\bsheet\b", r"\btut\b", r"\btutorial\b"],
    "lab": [r"\blab\b", r"\blaboratory\b"],
    "quiz": [r"\bquiz\b"],
    "assignment": [r"\bassign\b", r"\bassignment\b", r"\bhw\b", r"\bhomework\b"],
    "exam": [r"\bexam\b", r"\bmidterm\b", r"\bfinal\b"],
    "project": [r"\bproject\b"],
    "handout": [r"\bhandout\b", r"\bsyllabus\b", r"\boutline\b"],
    "solution": [r"\bsolution\b", r"\bsolutions\b", r"\bmodel\s+answer\b"],
}

def get_doc_types(text: str) -> Set[str]:
    """Extract recognized document classification categories from text."""
    t = urllib.parse.unquote(text).lower()
    t = re.sub(r"[\u064b-\u0652\u0670\u0640]", "", t)
    t = re.sub(r"[^a-z0-9\u0600-\u06ff]+", " ", t)
    found: Set[str] = set()
    for dtype, patterns in DOC_TYPE_PATTERNS.items():
        for pat in patterns:
            if re.search(pat, t):
                found.add(dtype)
                break
    return found

def are_semantically_compatible(
    remote_name: str,
    cand_name: str,
    activity_title: Optional[str] = None
) -> bool:
    """
    Guard for Tier 4: verify candidate and remote items do not have conflicting
    category numbers or mutually exclusive document types.
    """
    remote_combined = f"{remote_name} {activity_title or ''}"
    remote_cats, _ = extract_semantic_tokens(remote_combined)
    cand_cats, _ = extract_semantic_tokens(cand_name)

    # 1. Category numbers conflict check (e.g. Lecture 1 vs Lecture 2)
    for cat, num in remote_cats.items():
        if cat in cand_cats and cand_cats[cat] != num:
            return False

    # 2. Numbered category disjointness (e.g. Lecture 1 vs Sheet 1)
    if remote_cats and cand_cats:
        if not set(remote_cats.keys()).intersection(cand_cats.keys()):
            return False

    # 3. Document types conflict check (e.g. Handout vs Final Exam)
    remote_types = get_doc_types(remote_combined)
    cand_types = get_doc_types(cand_name)
    if remote_types and cand_types:
        if not remote_types.intersection(cand_types):
            return False

    return True

# ==============================================================================
# LOCAL DIRECTORY SCANNER
# ==============================================================================

class LocalDirectoryScanner:
    """Indexes files strictly within target course folders, ignoring non-target folders and locks."""

    COURSE_MAP = {
        21329: {"code": "ECE322", "dir": "Electronic circuits"},
        21365: {"code": "ECE342", "dir": "Computer Organization"},
        21383: {"code": "ECE343", "dir": "Microprocessor System Design"},
        21395: {"code": "ECE344", "dir": "Control Systems"},
        21425: {"code": "ECE353", "dir": "Software Engineering"},
        21443: {"code": "ECE354", "dir": "Image and Video Processing"},
    }

    EXCLUDED_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}

    def __init__(self, root_dir: Union[str, Path] = BASE_STUDY_DIR):
        self.root_dir = Path(root_dir)
        self.files: List[LocalFileInfo] = []
        self.by_course: Dict[str, List[LocalFileInfo]] = {}
        self.by_canonical: Dict[Tuple[str, str], List[LocalFileInfo]] = {}
        self.by_normalized: Dict[Tuple[str, str], List[LocalFileInfo]] = {}
        self.by_size: Dict[Tuple[str, int], List[LocalFileInfo]] = {}
        self.by_ext: Dict[Tuple[str, str], List[LocalFileInfo]] = {}

    @property
    def total_files(self) -> int:
        return len(self.files)

    def scan(self, base_dir: Optional[Union[str, Path]] = None, courses: Optional[Any] = None) -> int:
        """Scan all 6 target course directories and build lookup tables."""
        if base_dir:
            self.root_dir = Path(base_dir)

        self.files.clear()
        self.by_course.clear()
        self.by_canonical.clear()
        self.by_normalized.clear()
        self.by_size.clear()
        self.by_ext.clear()

        courses_map = self.COURSE_MAP
        if courses and isinstance(courses, dict):
            # adapt if custom courses passed
            courses_map = {}
            for cid, info in courses.items():
                courses_map[cid] = {"code": info.get("code", ""), "dir": info.get("local_dir", info.get("folder", ""))}
        elif courses and isinstance(courses, list):
            courses_map = {}
            for idx, info in enumerate(courses):
                courses_map[idx] = {"code": info.get("code", ""), "dir": info.get("local_dir", info.get("folder", ""))}

        for cid, info in courses_map.items():
            code = info["code"]
            cname = info["dir"]
            cdir = self.root_dir / cname
            if not cdir.exists():
                continue

            course_file_list: List[LocalFileInfo] = []

            for p in cdir.rglob("*"):
                if not p.is_file():
                    continue

                fname = p.name
                # Exclude lock files and system junk
                if fname.startswith("~$") or fname.startswith("._") or fname.lower() in self.EXCLUDED_NAMES:
                    continue
                if fname.endswith(".tmp") or fname.endswith(".crdownload"):
                    continue

                try:
                    st = p.stat()
                    size_bytes = st.st_size
                    mtime = st.st_mtime
                except OSError:
                    continue

                rel = p.relative_to(cdir)
                subfolder = str(rel.parent).replace("\\", "/") if len(rel.parts) > 1 else ""
                ext = p.suffix.lower()
                norm_name = normalize_filename(fname)
                _, tokens = extract_semantic_tokens(fname)

                info_obj = LocalFileInfo(
                    path=p,
                    rel_path=rel,
                    course_code=code,
                    course_name=cname,
                    subfolder=subfolder,
                    filename=fname,
                    size_bytes=size_bytes,
                    mtime=mtime,
                    extension=ext,
                    normalized_name=norm_name,
                    tokens=tokens,
                )

                self.files.append(info_obj)
                course_file_list.append(info_obj)

                # Lookups
                self.by_canonical.setdefault((code, fname.lower()), []).append(info_obj)
                self.by_normalized.setdefault((code, norm_name), []).append(info_obj)
                self.by_size.setdefault((code, size_bytes), []).append(info_obj)
                self.by_ext.setdefault((code, ext), []).append(info_obj)

            self.by_course[code] = course_file_list

        return len(self.files)

    def register_file(self, path: Path, course_code: Optional[str] = None):
        """Dynamically add newly downloaded or extracted file into index."""
        path = Path(path)
        if not path.is_file() or path.name.startswith("~$"):
            return

        # Determine course code if not provided
        code = course_code or ""
        cname = ""
        if not code:
            try:
                rel = path.relative_to(self.root_dir)
                top = rel.parts[0]
                for cid, info in self.COURSE_MAP.items():
                    if info["dir"].lower() == top.lower():
                        code = info["code"]
                        cname = info["dir"]
                        break
            except Exception:
                code = "COURSE"

        st = path.stat()
        fname = path.name
        ext = path.suffix.lower()
        norm_name = normalize_filename(fname)
        _, tokens = extract_semantic_tokens(fname)

        info_obj = LocalFileInfo(
            path=path,
            rel_path=Path(path.name),
            course_code=code,
            course_name=cname,
            subfolder="",
            filename=fname,
            size_bytes=st.st_size,
            mtime=st.st_mtime,
            extension=ext,
            normalized_name=norm_name,
            tokens=tokens,
        )
        self.files.append(info_obj)
        self.by_course.setdefault(code, []).append(info_obj)
        self.by_canonical.setdefault((code, fname.lower()), []).append(info_obj)
        self.by_normalized.setdefault((code, norm_name), []).append(info_obj)
        self.by_size.setdefault((code, st.st_size), []).append(info_obj)
        self.by_ext.setdefault((code, ext), []).append(info_obj)

    def add_file(self, path: Path, course_code: Optional[str] = None):
        """Alias for register_file."""
        self.register_file(path, course_code)

# Alias for compatibility with Explorer 3 & syncer
LocalDiskIndex = LocalDirectoryScanner

# ==============================================================================
# 4-TIER DEDUPLICATION ENGINE
# ==============================================================================

class DeduplicationEngine:
    """Executes 4-tier matching against indexed course files."""

    def __init__(self, scanner: LocalDirectoryScanner):
        self.scanner = scanner

    def match(
        self,
        course_code: str,
        remote_name: str,
        remote_size: Optional[int] = None,
        activity_title: Optional[str] = None,
        target_subfolder: Optional[str] = None,
    ) -> MatchResult:
        """
        Evaluate candidate against local indexed files in 4 tiers:
        Tier 1: Canonical case-insensitive match
        Tier 2: Normalized match
        Tier 3: Semantic activity title match
        Tier 4: Content-Length / File size match
        """
        candidates = self.scanner.by_course.get(course_code, [])
        if not candidates:
            return MatchResult(matched=False, tier=MatchTier.NO_MATCH, action="DOWNLOAD")

        # ------------------ Tier 1: Canonical Match ------------------
        canonical_key = (course_code, remote_name.lower())
        canonical_matches = self.scanner.by_canonical.get(canonical_key, [])
        if canonical_matches:
            chosen = canonical_matches[0]
            if target_subfolder:
                target_sub_norm = target_subfolder.strip("/\\").lower()
                sub_matches = [
                    c for c in canonical_matches
                    if c.subfolder and (target_sub_norm in c.subfolder.lower() or c.subfolder.lower() in target_sub_norm)
                ]
                if sub_matches:
                    size_sub_matches = [c for c in sub_matches if remote_size is not None and c.size_bytes == remote_size]
                    chosen = size_sub_matches[0] if size_sub_matches else sub_matches[0]
                else:
                    size_matches = [c for c in canonical_matches if remote_size is not None and c.size_bytes == remote_size]
                    if size_matches:
                        chosen = size_matches[0]
            elif remote_size is not None:
                size_matches = [c for c in canonical_matches if c.size_bytes == remote_size]
                if size_matches:
                    chosen = size_matches[0]

            if remote_size is None or chosen.size_bytes == remote_size:
                return MatchResult(
                    matched=True,
                    tier=MatchTier.TIER_1_CANONICAL,
                    matched_file=chosen,
                    confidence=1.0,
                    reason=f"Canonical match '{chosen.filename}' with identical size ({chosen.size_bytes}B)",
                    action="SKIP",
                    size_bytes=chosen.size_bytes,
                )
            else:
                return MatchResult(
                    matched=True,
                    tier=MatchTier.TIER_1_CANONICAL,
                    matched_file=chosen,
                    confidence=0.8,
                    reason=f"Canonical match '{chosen.filename}', but size differs (local: {chosen.size_bytes}B vs remote: {remote_size}B)",
                    action="DOWNLOAD_VERSIONED",
                    discrepancy="SIZE_MISMATCH",
                    size_bytes=chosen.size_bytes,
                )

        # ------------------ Tier 2: Normalized Match ------------------
        norm_remote = normalize_filename(remote_name)
        norm_matches = self.scanner.by_normalized.get((course_code, norm_remote), [])
        if norm_matches:
            chosen = norm_matches[0]
            if target_subfolder:
                target_sub_norm = target_subfolder.strip("/\\").lower()
                sub_matches = [
                    c for c in norm_matches
                    if c.subfolder and (target_sub_norm in c.subfolder.lower() or c.subfolder.lower() in target_sub_norm)
                ]
                if sub_matches:
                    size_sub_matches = [c for c in sub_matches if remote_size is not None and c.size_bytes == remote_size]
                    chosen = size_sub_matches[0] if size_sub_matches else sub_matches[0]
                else:
                    size_matches = [c for c in norm_matches if remote_size is not None and c.size_bytes == remote_size]
                    if size_matches:
                        chosen = size_matches[0]
            elif remote_size is not None:
                size_matches = [c for c in norm_matches if c.size_bytes == remote_size]
                if size_matches:
                    chosen = size_matches[0]

            if remote_size is None or chosen.size_bytes == remote_size:
                return MatchResult(
                    matched=True,
                    tier=MatchTier.TIER_2_NORMALIZED,
                    matched_file=chosen,
                    confidence=0.95,
                    reason=f"Normalized match '{chosen.filename}' (norm: '{norm_remote}')",
                    action="SKIP",
                    size_bytes=chosen.size_bytes,
                )
            else:
                return MatchResult(
                    matched=True,
                    tier=MatchTier.TIER_2_NORMALIZED,
                    matched_file=chosen,
                    confidence=0.75,
                    reason=f"Normalized match '{chosen.filename}', but size differs (local: {chosen.size_bytes}B vs remote: {remote_size}B)",
                    action="DOWNLOAD_VERSIONED",
                    discrepancy="SIZE_MISMATCH",
                    size_bytes=chosen.size_bytes,
                )

        # ------------------ Tier 3: Semantic Match ------------------
        if activity_title:
            title_cats, title_words = extract_semantic_tokens(activity_title)
            remote_ext = os.path.splitext(remote_name)[1].lower()

            for cand in candidates:
                if cand.extension != remote_ext:
                    continue
                cand_cats, cand_words = extract_semantic_tokens(cand.filename)

                # Check category match (e.g. ('lecture', 2))
                cat_match = False
                for cat, num in title_cats.items():
                    if cand_cats.get(cat) == num:
                        cat_match = True
                        break

                if cat_match:
                    return MatchResult(
                        matched=True,
                        tier=MatchTier.TIER_3_SEMANTIC,
                        matched_file=cand,
                        confidence=0.90,
                        reason=f"Semantic category match '{activity_title}' -> '{cand.filename}'",
                        action="SKIP",
                        size_bytes=cand.size_bytes,
                    )

                # Check word token overlap
                if title_words and title_words.issubset(cand_words):
                    return MatchResult(
                        matched=True,
                        tier=MatchTier.TIER_3_SEMANTIC,
                        matched_file=cand,
                        confidence=0.85,
                        reason=f"Semantic token overlap '{activity_title}' -> '{cand.filename}'",
                        action="SKIP",
                        size_bytes=cand.size_bytes,
                    )

        # ------------------ Tier 4: Exact Size Match ------------------
        if remote_size and remote_size > 0:
            size_matches = self.scanner.by_size.get((course_code, remote_size), [])
            remote_ext = os.path.splitext(remote_name)[1].lower()
            matching_ext_cands = [c for c in size_matches if c.extension == remote_ext]

            # Semantic compatibility filter: never match across conflicting categories
            compatible_cands = [
                c for c in matching_ext_cands
                if are_semantically_compatible(remote_name, c.filename, activity_title)
            ]

            if compatible_cands:
                chosen = compatible_cands[0]
                if target_subfolder:
                    target_sub_norm = target_subfolder.strip("/\\").lower()
                    for cand in compatible_cands:
                        if cand.subfolder and (target_sub_norm in cand.subfolder.lower() or cand.subfolder.lower() in target_sub_norm):
                            chosen = cand
                            break

                return MatchResult(
                    matched=True,
                    tier=MatchTier.TIER_4_SIZE,
                    matched_file=chosen,
                    confidence=0.95,
                    reason=f"Exact byte size match ({remote_size}B) with '{chosen.filename}' (semantically compatible)",
                    action="SKIP",
                    size_bytes=chosen.size_bytes,
                )

        return MatchResult(
            matched=False,
            tier=MatchTier.NO_MATCH,
            action="DOWNLOAD",
            reason="No match found across 4 tiers",
        )

    @staticmethod
    def get_safe_destination_path(dest_dir: Path, filename: str) -> Path:
        """Enforces zero-overwrite policy: returns non-colliding path strictly confined to dest_dir."""
        dest_dir = Path(dest_dir)
        clean_fn = Path(filename).name
        target = dest_dir / clean_fn
        dest_resolved = dest_dir.resolve()
        if not target.resolve().is_relative_to(dest_resolved):
            raise ValueError(f"Boundary confinement violation: '{clean_fn}' escapes '{dest_dir}'")
        if not target.exists():
            return target
        stem = target.stem
        ext = target.suffix
        seq = 1
        while True:
            candidate = dest_dir / f"{stem}_moodle_{seq}{ext}"
            if not candidate.exists():
                if not candidate.resolve().is_relative_to(dest_resolved):
                    raise ValueError(f"Boundary confinement violation: '{candidate}' escapes '{dest_dir}'")
                return candidate
            seq += 1

def match_resource(resource: Any, target_dest_dir: Path, local_index: LocalDiskIndex) -> MatchResult:
    """Convenience helper to match a scraped MoodleResource/MoodleItem against LocalDiskIndex."""
    engine = DeduplicationEngine(local_index)

    # Determine course code
    course_code = ""
    course_id = getattr(resource, "course_id", None)
    if course_id:
        try:
            cid = normalize_course_id(course_id)
            course_code = COURSES_CONFIG[cid]["code"]
        except Exception:
            pass

    remote_name = getattr(resource, "filename", "") or getattr(resource, "name", "") or getattr(resource, "title", "")
    remote_size = getattr(resource, "filesize", None)
    activity_title = getattr(resource, "name", "") or getattr(resource, "title", "")
    subfolder = getattr(resource, "target_subfolder", "")

    return engine.match(
        course_code=course_code,
        remote_name=remote_name,
        remote_size=remote_size,
        activity_title=activity_title,
        target_subfolder=subfolder,
    )
