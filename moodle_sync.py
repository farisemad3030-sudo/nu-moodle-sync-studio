#!/usr/bin/env python3
"""
moodle_sync.py: Main CLI entry point for Nile University Moodle Synchronizer.
Automates authentication, course scraping, selective 4-tier deduplication,
ZIP archive extraction, external media ingestion, and audit reporting.
"""

import sys
import io

# CRITICAL WINDOWS ENCODING INITIALIZATION:
# Prevent CP1252 charmap encoding crash on Arabic filenames in ECE354
if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import argparse
import logging
from pathlib import Path

from moodle_sync import config
from moodle_sync.syncer import MoodleSyncer

def setup_logging(verbose: bool = False, quiet: bool = False):
    """Configure console logging level and format."""
    if quiet:
        level = logging.ERROR
    elif verbose:
        level = logging.DEBUG
    else:
        level = logging.INFO

    format_str = "[%(levelname)s] %(asctime)s - %(message)s"
    logging.basicConfig(
        level=level,
        format=format_str,
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)]
    )

def print_banner():
    banner = r"""
========================================================================
             Nile University Moodle Synchronization Tool
   Automated Course Mirroring, Selective Deduplication & Audit System
========================================================================
"""
    print(banner)

def build_parser() -> argparse.ArgumentParser:
    """Construct argument parser conforming to CLI specifications."""
    parser = argparse.ArgumentParser(
        prog="moodle_sync",
        description="Reusable Moodle Synchronizer Tool for Nile University courses",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python moodle_sync.py                     # Full live synchronization of all 6 courses
  python moodle_sync.py --dry-run           # Scan and diff without downloading
  python moodle_sync.py --verify            # Audit local files vs Moodle (0 downloads)
  python moodle_sync.py --course ECE322     # Sync only Electronic Circuits (ECE322)
  python moodle_sync.py --course 21365      # Sync Computer Organization by Moodle ID
        """
    )

    mode_group = parser.add_mutually_exclusive_group(required=False)
    mode_group.add_argument(
        "--sync",
        action="store_true",
        help="Execute synchronization and download missing materials (default)."
    )
    mode_group.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and simulate sync without writing to disk."
    )
    mode_group.add_argument(
        "--verify",
        action="store_true",
        help="Verify local files against Moodle without downloading."
    )

    valid_codes = list({info["code"] for info in config.COURSES_CONFIG.values()})
    parser.add_argument(
        "--course",
        type=str,
        default=None,
        choices=valid_codes + [str(cid) for cid in config.TARGET_COURSE_IDS],
        help="Restrict synchronization to a specific course code or ID (e.g. ECE322 or 21329)."
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=config.BASE_DIR,
        help=f"Target workspace path (default: {config.BASE_DIR})."
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Alias for --workspace."
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        default=True,
        help="Run browser in headless mode (default: True)."
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable detailed verbose debug logging."
    )
    parser.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="Suppress non-error output."
    )

    return parser

def main():
    parser = build_parser()
    args = parser.parse_args()

    setup_logging(verbose=args.verbose, quiet=args.quiet)

    target_dir = Path(args.output_dir) if args.output_dir else args.workspace
    if not target_dir.exists():
        print(f"Error: Target workspace directory '{target_dir}' does not exist.")
        sys.exit(1)

    if not args.quiet:
        print_banner()
        mode_label = "VERIFY AUDIT" if args.verify else ("DRY RUN" if args.dry_run else "LIVE SYNCHRONIZATION")
        print(f"[*] Workspace Root: {target_dir}")
        print(f"[*] Execution Mode: {mode_label}")
        if args.course:
            print(f"[*] Filter: Target Course '{args.course}'")
        else:
            print(f"[*] Target Scope: All 6 courses (ECE322, ECE342, ECE343, ECE344, ECE353, ECE354)")
        print("-" * 72)

    syncer = MoodleSyncer(
        base_dir=target_dir,
        dry_run=args.dry_run,
        verify_only=args.verify,
    )

    try:
        summary = syncer.run(target_course_id=args.course)
    except KeyboardInterrupt:
        print("\n[!] Synchronization interrupted by user.")
        sys.exit(130)
    except Exception as e:
        logging.exception(f"[!] Fatal error during synchronization: {e}")
        sys.exit(1)

    if not args.quiet:
        print("\n" + "=" * 72)
        print(f"                SYNCHRONIZATION & AUDIT SUMMARY")
        print("=" * 72)
        print(f"Total Moodle Resources Discovered:  {summary.total_moodle_resources_discovered}")
        print(f"Total Local Files Evaluated:        {summary.total_local_files_evaluated}")
        print(f"Pre-existing Files Skipped:         {summary.total_skipped_already_present}")
        print(f"Newly Downloaded Files:             {summary.total_newly_downloaded}")
        print(f"ZIP Archives Extracted:             {summary.total_archives_extracted}")
        print(f"External Links / Shortcuts Saved:   {summary.total_shortcuts_created}")
        print(f"Total Errors / Unresolved:          {summary.total_errors}")
        print("-" * 72)
        print(f"Report (JSON):     {target_dir / 'sync_report.json'}")
        print(f"Report (Markdown): {target_dir / 'verification_report.md'}")
        print("=" * 72)

    if summary.zero_missing_downloadables_confirmed:
        if not args.quiet:
            print("[SUCCESS] Zero unretrieved downloadable materials confirmed across target courses.")
        sys.exit(0)
    else:
        if not args.quiet:
            print("[WARNING] Synchronization completed with missing or unresolved items.")
        sys.exit(2)

if __name__ == "__main__":
    main()
