"""
moodle_sync: Nile University Moodle Course Synchronization Tool.
"""

__version__ = "1.0.0"

from moodle_sync import config
from moodle_sync import auth
from moodle_sync import scraper
from moodle_sync import deduplication
from moodle_sync import archive
from moodle_sync import media
from moodle_sync import syncer
from moodle_sync import verifier

from moodle_sync.auth import login, patch_dns, unpatch_dns, get_session
from moodle_sync.scraper import CourseScraper, scan_course, scan_all_courses
from moodle_sync.deduplication import LocalDirectoryScanner, LocalDiskIndex, DeduplicationEngine, match_resource
from moodle_sync.archive import ArchiveHandler, extract_and_organize
from moodle_sync.media import resolve_and_download
from moodle_sync.syncer import MoodleSyncer, SyncSummary
from moodle_sync.verifier import generate_report

__all__ = [
    "__version__",
    "config",
    "auth",
    "scraper",
    "deduplication",
    "archive",
    "media",
    "syncer",
    "verifier",
    "login",
    "get_session",
    "patch_dns",
    "unpatch_dns",
    "CourseScraper",
    "scan_course",
    "scan_all_courses",
    "LocalDirectoryScanner",
    "LocalDiskIndex",
    "DeduplicationEngine",
    "match_resource",
    "ArchiveHandler",
    "extract_and_organize",
    "resolve_and_download",
    "MoodleSyncer",
    "SyncSummary",
    "generate_report",
]
