"""
Pytest configuration and shared fixtures for moodle-sync test suite.
"""

import sys
import tempfile
import shutil
from pathlib import Path
from typing import Generator

# Add root directory to sys.path so modules and tests can import freely
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

try:
    import pytest
except ImportError:
    pytest = None

if pytest:
    @pytest.fixture
    def temp_workspace() -> Generator[Path, None, None]:
        """Provides an isolated clean temporary workspace directory."""
        temp_dir = tempfile.mkdtemp(prefix="moodle_sync_test_")
        path = Path(temp_dir)
        try:
            yield path
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    @pytest.fixture
    def mock_course_structure(temp_workspace: Path) -> Path:
        """Sets up the 6 course directories and sample files in a temp workspace."""
        from tests.fixtures import TARGET_COURSES
        for cid, info in TARGET_COURSES.items():
            cdir = temp_workspace / info["local_dir"]
            (cdir / "Lectures").mkdir(parents=True, exist_ok=True)
            (cdir / "Tutorials & Sheets").mkdir(parents=True, exist_ok=True)
            (cdir / "Labs").mkdir(parents=True, exist_ok=True)
            (cdir / "Assignments").mkdir(parents=True, exist_ok=True)
            # Add a sample lecture
            (cdir / "Lectures" / "Lecture 1.pdf").write_bytes(b"%PDF-1.4 Mock Lecture 1")
        return temp_workspace
