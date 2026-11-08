"""
moodle_sync/archive.py
Authoritative archive extraction pipeline, macOS junk purging, single-root unwrapping,
embedded .url extraction, and atomic placement.
"""

import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import List, Optional, Tuple, Union, Any

try:
    from moodle_sync.deduplication import DeduplicationEngine, MatchTier
except ImportError:
    DeduplicationEngine = None
    MatchTier = None

@dataclass
class EmbeddedUrlInfo:
    source_zip: Path
    entry_name: str
    stem: str
    url: str
    target_dir: Path

@dataclass
class ArchiveExtractionResult:
    zip_path: Path
    dest_dir: Path
    success: bool
    extracted_files: List[Path] = field(default_factory=list)
    skipped_files: List[Tuple[str, str]] = field(default_factory=list)
    purged_entries: List[str] = field(default_factory=list)
    embedded_urls: List[EmbeddedUrlInfo] = field(default_factory=list)
    error_message: Optional[str] = None

    def __iter__(self):
        """Duck-typing iterator satisfying PROJECT.md contract: extract_and_organize -> List[Path]."""
        return iter(self.extracted_files)

    def __len__(self) -> int:
        return len(self.extracted_files)

    def __getitem__(self, index: int) -> Path:
        return self.extracted_files[index]

# ==============================================================================
# ARCHIVE UTILITIES & CLEANING
# ==============================================================================

JUNK_FILENAMES = {".ds_store", "thumbs.db", "desktop.ini"}

VALID_ZIP_HEADERS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")

# ZIP-based document containers that must be saved as-is, never extracted.
CONTAINER_DOC_EXTENSIONS = {
    ".docx", ".docm", ".dotx", ".xlsx", ".xlsm", ".xltx", ".pptx", ".pptm", ".potx", ".ppsx",
    ".odt", ".ods", ".odp", ".epub", ".jar", ".apk", ".vsdx", ".xps", ".mlx", ".slx", ".mlapp",
}
# Marker entries that identify OOXML / ODF / JAR containers regardless of filename.
CONTAINER_MARKER_ENTRIES = {"[Content_Types].xml", "mimetype", "META-INF/MANIFEST.MF"}


def _is_document_container(zf: "zipfile.ZipFile") -> bool:
    try:
        names = set(zf.namelist())
    except Exception:
        return False
    return bool(names & CONTAINER_MARKER_ENTRIES)


def is_zip_archive(path_or_bytes: Union[Path, str, bytes, bytearray]) -> bool:
    """Detect whether file or bytes represent a genuine ZIP archive (not an Office/ODF document container)."""
    if isinstance(path_or_bytes, (str, Path)):
        p = Path(path_or_bytes)
        if p.suffix.lower() in CONTAINER_DOC_EXTENSIONS:
            return False
        if not p.is_file() or p.stat().st_size < 4:
            return False
        try:
            with open(p, "rb") as f:
                header = f.read(4)
            if header not in VALID_ZIP_HEADERS:
                return False
            if not zipfile.is_zipfile(p):
                return False
            with zipfile.ZipFile(p, "r") as zf:
                return not _is_document_container(zf)
        except (OSError, ValueError, zipfile.BadZipFile):
            return False
    elif isinstance(path_or_bytes, (bytes, bytearray)):
        if len(path_or_bytes) < 4:
            return False
        header = bytes(path_or_bytes[:4])
        if header not in VALID_ZIP_HEADERS:
            return False
        try:
            import io
            buf = io.BytesIO(path_or_bytes)
            if not zipfile.is_zipfile(buf):
                return False
            with zipfile.ZipFile(buf, "r") as zf:
                return not _is_document_container(zf)
        except Exception:
            return False
    return False

def is_junk_entry(entry_name: str) -> bool:
    """Check if entry is macOS or system metadata junk."""
    norm = entry_name.replace("\\", "/")
    parts = PurePosixPath(norm).parts
    if any(p == "__MACOSX" for p in parts):
        return True
    filename = parts[-1]
    if filename.startswith("._") or filename.lower() in JUNK_FILENAMES:
        return True
    if filename.startswith("~$") or filename.endswith(".tmp"):
        return True
    return False

def extract_url_from_content(raw_data: bytes) -> Optional[str]:
    """Parse target URL from Windows .url Internet Shortcut file bytes."""
    for enc in ["utf-8", "cp1252", "latin-1"]:
        try:
            text = raw_data.decode(enc)
            m = re.search(r"^\s*URL\s*=\s*(.+)$", text, re.IGNORECASE | re.MULTILINE)
            if m:
                return m.group(1).strip()
        except UnicodeDecodeError:
            continue
    return None

# ==============================================================================
# ARCHIVE EXTRACTION PIPELINE
# ==============================================================================

class ArchiveHandler:
    """Manages secure extraction, cleaning, unwrapping, and placement of ZIP archives."""

    def __init__(self, dedup_engine: Optional[Any] = None):
        self.dedup_engine = dedup_engine

    def extract_and_organize(
        self,
        zip_path: Union[str, Path],
        dest_dir: Union[str, Path],
        course_code: Optional[str] = None,
        local_index: Optional[Any] = None,
    ) -> ArchiveExtractionResult:
        """
        Extracts ZIP archive with atomic staging, purges macOS junk, unwraps single-folder
        archives, extracts embedded .url shortcuts, and applies zero-overwrite placement.
        """
        zip_p = Path(zip_path).resolve()
        dest_p = Path(dest_dir).resolve()
        dest_p.mkdir(parents=True, exist_ok=True)

        res = ArchiveExtractionResult(zip_path=zip_p, dest_dir=dest_p, success=False)

        if not is_zip_archive(zip_p):
            res.error_message = f"File is not a valid ZIP archive: {zip_p}"
            return res

        dedup = self.dedup_engine
        if not dedup and local_index:
            try:
                from moodle_sync.deduplication import DeduplicationEngine
                dedup = DeduplicationEngine(local_index)
            except Exception:
                pass

        # 1. Open and test ZIP integrity
        try:
            z = zipfile.ZipFile(zip_p, "r")
            bad_crc = z.testzip()
            if bad_crc:
                res.error_message = f"Corrupted file in archive: {bad_crc}"
                z.close()
                return res
        except (zipfile.BadZipFile, OSError) as e:
            res.error_message = f"Failed to open ZIP archive: {e}"
            return res

        # 2. Filter entries and check Zip Slip
        valid_entries: List[Tuple[zipfile.ZipInfo, str, Tuple[str, ...]]] = []
        for info in z.infolist():
            if info.is_dir():
                continue
            name = info.filename
            norm_name = name.replace("\\", "/")
            posix_path = PurePosixPath(norm_name)
            win_drive = bool(PureWindowsPath(name).drive) or bool(PureWindowsPath(norm_name).drive)

            # Security check for Zip Slip, absolute paths, and drive-root escape
            if (
                norm_name.startswith("/")
                or norm_name.startswith("\\")
                or posix_path.is_absolute()
                or win_drive
                or re.match(r"^[a-zA-Z]:", name)
                or ".." in posix_path.parts
            ):
                z.close()
                res.error_message = f"Path traversal attack detected in archive: {name}"
                raise ValueError(res.error_message)

            if is_junk_entry(name):
                res.purged_entries.append(name)
                continue
            parts = posix_path.parts
            valid_entries.append((info, name, parts))

        if not valid_entries:
            res.success = True
            z.close()
            return res

        # Check single root unwrapping condition
        first_components = [p[0] for _, _, p in valid_entries if len(p) > 1]
        has_single_root = (
            len(first_components) == len(valid_entries)
            and len(set(first_components)) == 1
        )

        # 3. True Staging: extract and verify all entries in TemporaryDirectory first
        with tempfile.TemporaryDirectory(prefix="moodle_zip_staging_") as staging_temp:
            staging_dir = Path(staging_temp)
            staged_items: List[Tuple[Path, PurePosixPath, int, str]] = []

            for info, name, parts in valid_entries:
                if has_single_root:
                    rel_dest = PurePosixPath(*parts[1:])
                else:
                    rel_dest = PurePosixPath(*parts)

                staged_file = (staging_dir / Path(rel_dest)).resolve()
                if not staged_file.is_relative_to(staging_dir.resolve()):
                    z.close()
                    res.error_message = f"Staged path traversal escape detected: {name}"
                    raise ValueError(res.error_message)

                staged_file.parent.mkdir(parents=True, exist_ok=True)

                # Read and verify CRC integrity
                try:
                    entry_data = z.read(info)
                except zipfile.BadZipFile as crc_err:
                    z.close()
                    res.error_message = f"CRC failure on archive member {name}: {crc_err}"
                    return res

                entry_size = len(entry_data)

                # Check if entry is an embedded .url shortcut
                entry_target_dir = (dest_p / Path(rel_dest).parent).resolve()
                if rel_dest.suffix.lower() == ".url":
                    url_val = extract_url_from_content(entry_data)
                    if url_val:
                        res.embedded_urls.append(
                            EmbeddedUrlInfo(
                                source_zip=zip_p,
                                entry_name=name,
                                stem=rel_dest.stem,
                                url=url_val,
                                target_dir=entry_target_dir,
                            )
                        )

                with open(staged_file, "wb") as f_out:
                    f_out.write(entry_data)

                staged_items.append((staged_file, rel_dest, entry_size, name))

            # 4. Atomic Placement into destination directory
            for staged_file, rel_dest, entry_size, orig_name in staged_items:
                final_filename = rel_dest.name
                target_subpath = (dest_p / Path(rel_dest).parent).resolve()
                if not target_subpath.is_relative_to(dest_p.resolve()):
                    z.close()
                    res.error_message = f"Destination path traversal escape detected: {target_subpath}"
                    raise ValueError(res.error_message)

                target_subpath.mkdir(parents=True, exist_ok=True)
                final_dest_file = target_subpath / final_filename

                # Deduplication & Zero-Overwrite check against existing disk files
                if dedup and course_code:
                    match_res = dedup.match(
                        course_code=course_code,
                        remote_name=final_filename,
                        remote_size=entry_size,
                        target_subfolder=str(dest_p.name),
                    )
                    if match_res.matched and match_res.action == "SKIP":
                        res.skipped_files.append((orig_name, match_res.reason))
                        continue
                    elif match_res.action == "DOWNLOAD_VERSIONED":
                        from moodle_sync.deduplication import DeduplicationEngine
                        final_dest_file = DeduplicationEngine.get_safe_destination_path(target_subpath, final_filename)
                elif final_dest_file.exists():
                    if final_dest_file.stat().st_size == entry_size:
                        res.skipped_files.append((orig_name, f"Identical file exists ({entry_size}B)"))
                        continue
                    else:
                        from moodle_sync.deduplication import DeduplicationEngine
                        final_dest_file = DeduplicationEngine.get_safe_destination_path(target_subpath, final_filename)

                # Intra-volume atomic placement
                tmp_dest = target_subpath / f".tmp_{final_dest_file.name}"
                try:
                    shutil.copy2(staged_file, tmp_dest)
                    os.replace(tmp_dest, final_dest_file)
                    res.extracted_files.append(final_dest_file)
                    if dedup:
                        dedup.scanner.register_file(final_dest_file, course_code)
                    elif local_index:
                        local_index.register_file(final_dest_file, course_code)
                except OSError as write_err:
                    if tmp_dest.exists():
                        try:
                            tmp_dest.unlink()
                        except OSError:
                            pass
                    res.error_message = f"Failed to place extracted file {final_dest_file}: {write_err}"
                    z.close()
                    return res

        z.close()
        res.success = True
        return res

def scan_zip_for_embedded_urls(
    zip_path: Union[str, Path],
    dest_dir: Optional[Union[str, Path]] = None,
) -> List[EmbeddedUrlInfo]:
    """
    Non-destructively inspects a ZIP archive on disk and returns any embedded .url
    video/resource shortcuts inside it without extracting files.
    """
    zip_p = Path(zip_path).resolve()
    dest_p = Path(dest_dir).resolve() if dest_dir else zip_p.parent
    embedded: List[EmbeddedUrlInfo] = []
    if not is_zip_archive(zip_p):
        return embedded
    try:
        with zipfile.ZipFile(zip_p, "r") as z:
            for info in z.infolist():
                if info.is_dir() or is_junk_entry(info.filename):
                    continue
                posix_path = PurePosixPath(info.filename.replace("\\", "/"))
                if posix_path.suffix.lower() == ".url":
                    raw = z.read(info)
                    url_val = extract_url_from_content(raw)
                    if url_val:
                        embedded.append(
                            EmbeddedUrlInfo(
                                source_zip=zip_p,
                                entry_name=info.filename,
                                stem=posix_path.stem,
                                url=url_val,
                                target_dir=dest_p,
                            )
                        )
    except Exception:
        pass
    return embedded


def resolve_embedded_urls(
    extract_res: ArchiveExtractionResult,
    session: Any,
    course_code: Optional[str] = None,
    local_index: Optional[Any] = None,
) -> List[Path]:
    """
    Follows all embedded .url shortcuts discovered inside a ZIP archive (e.g., SharePoint,
    OneDrive, Panopto, YouTube, or Google Drive video links), downloads the actual media
    into the destination folder, and removes the temporary .url shortcut once resolved.
    """
    if not extract_res.embedded_urls or session is None:
        return []

    from moodle_sync.media import resolve_and_download

    resolved_paths: List[Path] = []
    for emb in extract_res.embedded_urls:
        mres = resolve_and_download(
            url=emb.url,
            dest_dir=emb.target_dir,
            session=session,
            title=emb.stem,
            suggested_name=emb.stem,
        )
        if mres.file_path and mres.status in ("DOWNLOADED", "SKIPPED_EXISTING"):
            if mres.file_path not in extract_res.extracted_files:
                extract_res.extracted_files.append(mres.file_path)
            resolved_paths.append(mres.file_path)
            if local_index and course_code and mres.status == "DOWNLOADED":
                try:
                    local_index.register_file(mres.file_path, course_code)
                except Exception:
                    pass
            # Remove the temporary .url shortcut from extracted_files & disk if real video is on disk
            if mres.file_path.suffix.lower() != ".url":
                url_stub = emb.target_dir / f"{emb.stem}.url"
                if url_stub.exists() and url_stub.is_file():
                    try:
                        url_stub.unlink()
                        if url_stub in extract_res.extracted_files:
                            extract_res.extracted_files.remove(url_stub)
                    except OSError:
                        pass
    return resolved_paths


def extract_and_organize(
    zip_path: Union[str, Path],
    dest_dir: Union[str, Path],
    course_code: Optional[str] = None,
    local_index: Optional[Any] = None,
    dedup_engine: Optional[Any] = None,
    session: Optional[Any] = None,
) -> ArchiveExtractionResult:
    """Convenience function wrapping ArchiveHandler and optional embedded URL resolution."""
    handler = ArchiveHandler(dedup_engine=dedup_engine)
    res = handler.extract_and_organize(
        zip_path=zip_path,
        dest_dir=dest_dir,
        course_code=course_code,
        local_index=local_index,
    )
    if res.success and res.embedded_urls and session is not None:
        resolve_embedded_urls(res, session=session, course_code=course_code, local_index=local_index)
    return res
