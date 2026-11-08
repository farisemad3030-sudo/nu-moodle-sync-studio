"""
moodle_sync/syncer.py
Full synchronization pipeline orchestrator.
Coordinates auth, scraping, deduplication, atomic downloads, archive handling,
external media resolution, and verification reporting.
"""

import os
import sys
import time
import logging
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Union, Any

import requests

from moodle_sync import config
from moodle_sync.auth import get_session
from moodle_sync.scraper import CourseScraper, CourseData, MoodleItem
from moodle_sync.deduplication import LocalDiskIndex, DeduplicationEngine, match_resource
from moodle_sync.archive import extract_and_organize, is_zip_archive
from moodle_sync.media import resolve_and_download, MediaResult, sanitize_filename
from moodle_sync.verifier import generate_report

logger = logging.getLogger("moodle_sync.syncer")

@dataclass
class ResourceSyncRecord:
    resource_id: str
    title: str
    section: str
    activity_type: str
    url: str
    target_subfolder: str
    target_filepath: str
    status: str  # "SKIPPED_EXISTING", "DOWNLOADED", "EXTRACTED", "SHORTCUT_FALLBACK", "ACTION_REQUIRED_AUTH", "FAILED", "PLANNED", "MISSING_UNRETRIEVED"
    matched_local_file: Optional[str] = None
    match_tier: Optional[str] = None
    size_bytes: int = 0
    notes: str = ""

@dataclass
class CourseSyncResult:
    course_code: str
    course_name: str
    moodle_id: int
    local_directory: str
    stats: Dict[str, int]
    resources: List[ResourceSyncRecord] = field(default_factory=list)

@dataclass
class SyncSummary:
    start_time: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    end_time: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    mode: str = "SYNC"  # "SYNC", "DRY_RUN", "VERIFY"
    total_target_courses: int = 6
    total_moodle_resources_discovered: int = 0
    total_local_files_evaluated: int = 0
    total_skipped_already_present: int = 0
    total_newly_downloaded: int = 0
    total_archives_extracted: int = 0
    total_external_links_resolved: int = 0
    total_shortcuts_created: int = 0
    total_action_required: int = 0
    total_errors: int = 0
    bytes_downloaded: int = 0
    zero_missing_downloadables_confirmed: bool = True
    courses: List[CourseSyncResult] = field(default_factory=list)

class MoodleSyncer:
    def __init__(
        self,
        base_dir: Path = config.BASE_DIR,
        session: Optional[requests.Session] = None,
        dry_run: bool = False,
        verify_only: bool = False
    ):
        self.base_dir = Path(base_dir)
        self.session = session
        self.dry_run = dry_run
        self.verify_only = verify_only
        self.local_index = LocalDiskIndex(root_dir=self.base_dir)
        # Persistent resource-id -> local-path manifest so URL/media resources are not re-downloaded.
        self.manifest_path = self.base_dir / ".moodle_sync_state.json"
        self.manifest: Dict[str, str] = self._load_manifest()

    def _load_manifest(self) -> Dict[str, str]:
        import json
        try:
            if self.manifest_path.is_file():
                data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return {str(k): str(v) for k, v in data.items()}
        except Exception as e:
            logger.warning(f"Could not read sync state manifest: {e}")
        return {}

    def _save_manifest(self) -> None:
        import json
        try:
            tmp = self.manifest_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.manifest, indent=1, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.manifest_path)
        except Exception as e:
            logger.warning(f"Could not write sync state manifest: {e}")

    def run(self, target_course_id: Optional[Union[int, str]] = None) -> SyncSummary:
        """Execute synchronization workflow."""
        start_time = datetime.now(timezone.utc).isoformat()
        mode_str = "VERIFY" if self.verify_only else ("DRY_RUN" if self.dry_run else "SYNC")
        logger.info(f"Starting Moodle Synchronization (mode={mode_str}) in {self.base_dir}")

        # 1. Initialize HTTP Session
        if not self.session:
            logger.info("Initializing authenticated Moodle session...")
            self.session = get_session()

        # 2. Index Local Disk across the 6 courses
        logger.info(f"Indexing local files in {self.base_dir} across target courses...")
        self.local_index.scan(self.base_dir, config.COURSES)
        initial_file_count = self.local_index.total_files
        logger.info(f"Indexed {initial_file_count} valid local files across target courses.")

        # 3. Filter Target Courses
        courses_to_sync: Dict[int, Dict[str, Any]] = {}
        if target_course_id is not None:
            cid = config.normalize_course_id(target_course_id)
            if cid in config.COURSES:
                courses_to_sync[cid] = config.COURSES[cid]
            else:
                raise ValueError(f"Course ID or code '{target_course_id}' not found in target courses.")
        else:
            courses_to_sync = config.COURSES

        scraper = CourseScraper(self.session)
        course_results: List[CourseSyncResult] = []

        overall_stats = {
            "discovered": 0,
            "skipped": 0,
            "downloaded": 0,
            "extracted": 0,
            "external_resolved": 0,
            "shortcuts": 0,
            "action_required": 0,
            "errors": 0,
            "bytes": 0,
        }

        # 4. Iterate and Synchronize Each Course
        for course_id, course_info in courses_to_sync.items():
            code = course_info["code"]
            name = course_info["name"]
            folder_name = course_info["local_dir"]
            course_dir = self.base_dir / folder_name
            if not self.dry_run and not self.verify_only:
                course_dir.mkdir(parents=True, exist_ok=True)

            logger.info(f"\n==========================================")
            logger.info(f"Processing Course {code}: {name} (ID: {course_id})")
            logger.info(f"Local Directory: {course_dir}")

            course_data: CourseData = scraper.scan_course(course_id)
            course_stats = {
                "discovered": len(course_data.resources),
                "skipped": 0,
                "downloaded": 0,
                "extracted": 0,
                "external_resolved": 0,
                "shortcuts": 0,
                "action_required": 0,
                "errors": 0,
                "bytes": 0,
            }
            resource_records: List[ResourceSyncRecord] = []

            for res in course_data.resources:
                target_subfolder = config.resolve_destination_subfolder(
                    code, res.section_title, res.name, res.filename
                )
                target_dest_dir = course_dir / target_subfolder
                if not self.dry_run and not self.verify_only:
                    target_dest_dir.mkdir(parents=True, exist_ok=True)

                # 0. Previously synchronized by this tool (state manifest), file still on disk
                prev_path = self.manifest.get(str(res.id))
                if prev_path and Path(prev_path).exists():
                    resource_records.append(ResourceSyncRecord(
                        resource_id=res.id,
                        title=res.name,
                        section=res.section_title,
                        activity_type=str(res.resource_type),
                        url=res.url,
                        target_subfolder=target_subfolder,
                        target_filepath=prev_path,
                        status="SKIPPED_EXISTING",
                        matched_local_file=prev_path,
                        match_tier="STATE_MANIFEST",
                        size_bytes=Path(prev_path).stat().st_size if Path(prev_path).is_file() else 0,
                        notes="Previously synchronized (state manifest)."
                    ))
                    course_stats["skipped"] += 1
                    continue

                # Check 4-tier deduplication against local files
                match_result = match_resource(res, target_dest_dir, self.local_index)

                if match_result.is_match and match_result.action == "SKIP":
                    matched_path = getattr(match_result.matched_file, "path", match_result.matched_file)
                    record = ResourceSyncRecord(
                        resource_id=res.id,
                        title=res.name,
                        section=res.section_title,
                        activity_type=str(res.resource_type),
                        url=res.url,
                        target_subfolder=target_subfolder,
                        target_filepath=str(matched_path or (target_dest_dir / (res.filename or res.name))),
                        status="SKIPPED_EXISTING",
                        matched_local_file=str(matched_path or ""),
                        match_tier=str(match_result.tier),
                        size_bytes=match_result.size_bytes or (res.filesize or 0),
                        notes=match_result.reason
                    )
                    resource_records.append(record)
                    course_stats["skipped"] += 1
                    continue

                # Missing File Handling in Verify Mode
                if self.verify_only:
                    record = ResourceSyncRecord(
                        resource_id=res.id,
                        title=res.name,
                        section=res.section_title,
                        activity_type=str(res.resource_type),
                        url=res.url,
                        target_subfolder=target_subfolder,
                        target_filepath=str(target_dest_dir / (res.filename or res.name)),
                        status="MISSING_UNRETRIEVED",
                        notes="Identified missing during audit verification mode."
                    )
                    resource_records.append(record)
                    course_stats["action_required"] += 1
                    continue

                # Dry-run Simulation Mode
                if self.dry_run:
                    record = ResourceSyncRecord(
                        resource_id=res.id,
                        title=res.name,
                        section=res.section_title,
                        activity_type=str(res.resource_type),
                        url=res.url,
                        target_subfolder=target_subfolder,
                        target_filepath=str(target_dest_dir / (res.filename or res.name)),
                        status="PLANNED",
                        notes="Planned for download in live execution."
                    )
                    resource_records.append(record)
                    course_stats["downloaded"] += 1
                    continue

                # Live Download Execution
                try:
                    if str(res.resource_type) == "mod_url" or res.is_external:
                        clean_suggested_name = sanitize_filename(Path(res.filename).name) if res.filename else None
                        media_res: MediaResult = resolve_and_download(
                            url=res.url,
                            dest_dir=target_dest_dir,
                            session=self.session,
                            title=res.name,
                            suggested_name=clean_suggested_name
                        )
                        record = ResourceSyncRecord(
                            resource_id=res.id,
                            title=res.name,
                            section=res.section_title,
                            activity_type=str(res.resource_type),
                            url=res.url,
                            target_subfolder=target_subfolder,
                            target_filepath=str(media_res.file_path or ""),
                            status=media_res.status,
                            size_bytes=media_res.bytes_downloaded,
                            notes=media_res.message
                        )
                        if media_res.status == "DOWNLOADED":
                            course_stats["downloaded"] += 1
                            course_stats["external_resolved"] += 1
                            course_stats["bytes"] += media_res.bytes_downloaded
                            if media_res.file_path:
                                self.local_index.add_file(media_res.file_path, code)
                        elif media_res.status == "SHORTCUT_FALLBACK":
                            course_stats["shortcuts"] += 1
                            course_stats["action_required"] += 1
                        elif media_res.status == "SKIPPED_EXISTING":
                            course_stats["skipped"] += 1
                        resource_records.append(record)

                    else:
                        # Direct Moodle Resource / Assignment / Folder File
                        download_url = res.download_url or res.url
                        r = self.session.get(download_url, stream=True, timeout=30)
                        
                        raw_fn = res.filename or CourseScraper.extract_filename(r) or f"{res.name}.dat"
                        target_filename = sanitize_filename(Path(raw_fn).name) or f"resource_{res.id}.dat"
                        target_dest_dir_resolved = target_dest_dir.resolve()
                        target_filepath = (target_dest_dir / target_filename).resolve()
                        tmp_filepath = target_dest_dir / f".tmp_{target_filename}"

                        # Strict boundary confinement assertions
                        if not target_filepath.is_relative_to(target_dest_dir_resolved):
                            raise ValueError(
                                f"Path traversal detected: '{target_filename}' escapes '{target_dest_dir}'"
                            )
                        if not tmp_filepath.resolve().is_relative_to(target_dest_dir_resolved):
                            raise ValueError(
                                f"Path traversal detected in temporary filepath: '{tmp_filepath}' escapes '{target_dest_dir}'"
                            )

                        target_dest_dir.mkdir(parents=True, exist_ok=True)
                        bytes_downloaded = 0
                        try:
                            with open(tmp_filepath, "wb") as f:
                                for chunk in r.iter_content(chunk_size=1024 * 1024):
                                    if chunk:
                                        f.write(chunk)
                                        bytes_downloaded += len(chunk)
                        except Exception:
                            tmp_filepath.unlink(missing_ok=True)
                            raise

                        # Check if payload is a ZIP archive
                        if is_zip_archive(tmp_filepath):
                            extract_res = extract_and_organize(
                                zip_path=tmp_filepath,
                                dest_dir=target_dest_dir,
                                course_code=code,
                                local_index=self.local_index,
                                session=self.session,
                            )
                            tmp_filepath.unlink(missing_ok=True)
                            course_stats["extracted"] += 1
                            course_stats["downloaded"] += len(extract_res.extracted_files)
                            course_stats["bytes"] += bytes_downloaded
                            
                            record = ResourceSyncRecord(
                                resource_id=res.id,
                                title=res.name,
                                section=res.section_title,
                                activity_type=str(res.resource_type),
                                url=res.url,
                                target_subfolder=target_subfolder,
                                target_filepath=str(target_dest_dir),
                                status="EXTRACTED",
                                size_bytes=bytes_downloaded,
                                notes=f"Extracted {len(extract_res.extracted_files)} files cleanly."
                            )
                            resource_records.append(record)
                        else:
                            # Standard atomic file placement with zero overwrite check
                            if target_filepath.exists():
                                if target_filepath.stat().st_size == bytes_downloaded:
                                    tmp_filepath.unlink(missing_ok=True)
                                    record = ResourceSyncRecord(
                                        resource_id=res.id,
                                        title=res.name,
                                        section=res.section_title,
                                        activity_type=str(res.resource_type),
                                        url=res.url,
                                        target_subfolder=target_subfolder,
                                        target_filepath=str(target_filepath),
                                        status="SKIPPED_EXISTING",
                                        size_bytes=bytes_downloaded,
                                        notes="File arrived but target existed with identical size; preserved disk file."
                                    )
                                    course_stats["skipped"] += 1
                                else:
                                    # Versioned non-colliding save
                                    safe_dest = DeduplicationEngine.get_safe_destination_path(target_dest_dir, target_filename)
                                    if not safe_dest.resolve().is_relative_to(target_dest_dir_resolved):
                                        raise ValueError(
                                            f"Boundary confinement violation for versioned destination: '{safe_dest}'"
                                        )
                                    os.replace(tmp_filepath, safe_dest)
                                    self.local_index.add_file(safe_dest, code)
                                    record = ResourceSyncRecord(
                                        resource_id=res.id,
                                        title=res.name,
                                        section=res.section_title,
                                        activity_type=str(res.resource_type),
                                        url=res.url,
                                        target_subfolder=target_subfolder,
                                        target_filepath=str(safe_dest),
                                        status="DOWNLOADED",
                                        size_bytes=bytes_downloaded,
                                        notes="Target existed with different size; saved versioned copy to prevent overwrite."
                                    )
                                    course_stats["downloaded"] += 1
                                    course_stats["bytes"] += bytes_downloaded
                            else:
                                if not target_filepath.resolve().is_relative_to(target_dest_dir_resolved):
                                    raise ValueError(
                                        f"Boundary confinement violation for destination: '{target_filepath}'"
                                    )
                                os.replace(tmp_filepath, target_filepath)
                                self.local_index.add_file(target_filepath, code)
                                record = ResourceSyncRecord(
                                    resource_id=res.id,
                                    title=res.name,
                                    section=res.section_title,
                                    activity_type=str(res.resource_type),
                                    url=res.url,
                                    target_subfolder=target_subfolder,
                                    target_filepath=str(target_filepath),
                                    status="DOWNLOADED",
                                    size_bytes=bytes_downloaded,
                                    notes="Clean atomic download."
                                )
                                course_stats["downloaded"] += 1
                                course_stats["bytes"] += bytes_downloaded
                            resource_records.append(record)

                except Exception as e:
                    logger.error(f"Error synchronizing resource '{res.name}': {e}")
                    record = ResourceSyncRecord(
                        resource_id=res.id,
                        title=res.name,
                        section=res.section_title,
                        activity_type=str(res.resource_type),
                        url=res.url,
                        target_subfolder=target_subfolder,
                        target_filepath=str(target_dest_dir / (res.filename or res.name)),
                        status="FAILED",
                        notes=f"Exception: {e}"
                    )
                    resource_records.append(record)
                    course_stats["errors"] += 1

            for k in overall_stats:
                overall_stats[k] += course_stats[k]

            if not self.dry_run and not self.verify_only:
                for r in resource_records:
                    if r.status in ("DOWNLOADED", "EXTRACTED") and r.target_filepath and Path(r.target_filepath).exists():
                        self.manifest[str(r.resource_id)] = str(r.target_filepath)
                self._save_manifest()

            course_results.append(
                CourseSyncResult(
                    course_code=code,
                    course_name=name,
                    moodle_id=course_id,
                    local_directory=str(course_dir),
                    stats=course_stats,
                    resources=resource_records
                )
            )

        end_time = datetime.now(timezone.utc).isoformat()

        def _unresolved(r) -> bool:
            if r.status in ("FAILED", "MISSING_UNRETRIEVED", "ACTION_REQUIRED_AUTH"):
                return True
            notes = (r.notes or "").lower()
            return r.status == "SHORTCUT_FALLBACK" and ("authentication" in notes or "network" in notes or "timed out" in notes)

        zero_missing = not any(_unresolved(r) for c in course_results for r in c.resources)

        summary = SyncSummary(
            start_time=start_time,
            end_time=end_time,
            mode=mode_str,
            total_target_courses=len(courses_to_sync),
            total_moodle_resources_discovered=overall_stats["discovered"],
            total_local_files_evaluated=initial_file_count,
            total_skipped_already_present=overall_stats["skipped"],
            total_newly_downloaded=overall_stats["downloaded"],
            total_archives_extracted=overall_stats["extracted"],
            total_external_links_resolved=overall_stats["external_resolved"],
            total_shortcuts_created=overall_stats["shortcuts"],
            total_action_required=overall_stats["action_required"],
            total_errors=overall_stats["errors"],
            bytes_downloaded=overall_stats["bytes"],
            zero_missing_downloadables_confirmed=zero_missing,
            courses=course_results
        )

        generate_report(summary, base_dir=self.base_dir)
        return summary
