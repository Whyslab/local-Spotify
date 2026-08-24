#!/usr/bin/env python3
"""Library audit script - read-only validation of M4A files.

Usage:
    python audit_library.py [--json] [library_path]

Checks:
- Valid M4A files
- Metadata presence
- Artwork presence
- Filename safety
- Directory structure
- Suspicious duplicates
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

try:
    from mutagen.mp4 import MP4, MP4StreamInfoError
except ImportError:
    print("Error: mutagen not installed. Run: pip install mutagen")
    sys.exit(1)

# Add parent directory to path to import config
sys.path.insert(0, str(Path(__file__).parent.parent / "adder"))
try:
    from config import LIBRARY
except ImportError:
    LIBRARY = Path.home() / "Music" / "Normalized Library"


def is_valid_m4a(filepath: Path) -> tuple[bool, str]:
    """Check if file is a valid M4A."""
    try:
        MP4(filepath)  # constructing it is the validation; the object is unused
        return True, ""
    except MP4StreamInfoError as e:
        return False, f"Invalid M4A stream: {str(e)[:100]}"
    except Exception as e:
        return False, f"Cannot read file: {str(e)[:100]}"


def check_metadata(audio: MP4) -> dict:
    """Check metadata presence."""
    result = {
        "has_title": bool(audio.get("\xa9nam")),
        "has_artist": bool(audio.get("\xa9ART") or audio.get("aART")),
        "has_album": bool(audio.get("\xa9alb")),
        "has_artwork": bool(audio.get("covr")),
    }
    result["complete"] = all([result["has_title"], result["has_artist"]])
    return result


def check_filename_safety(filepath: Path) -> tuple[bool, list[str]]:
    """Check if filename is safe for filesystems."""
    issues = []
    name = filepath.name

    # Check for problematic characters
    unsafe_chars = set('\\/*?:"<>|')
    if any(c in name for c in unsafe_chars):
        issues.append(f"Contains unsafe characters: {unsafe_chars & set(name)}")

    # Check length (filesystem limits ~255 bytes)
    if len(name.encode("utf-8")) > 250:
        issues.append(f"Filename too long: {len(name)} chars")

    # Check for leading/trailing spaces or dots
    if name.startswith(" ") or name.endswith(" "):
        issues.append("Leading/trailing spaces")
    if name.startswith(".") and len(name) > 1:
        issues.append("Hidden file (starts with .)")

    return len(issues) == 0, issues


def find_duplicates(files: list[Path]) -> list[list[Path]]:
    """Find suspicious duplicate groups based on metadata."""
    groups = defaultdict(list)

    for f in files:
        try:
            audio = MP4(f)
            title = (audio.get("\xa9nam") or "").lower().strip()
            artist = (audio.get("\xa9ART") or audio.get("aART") or "").lower().strip()

            if title and artist:
                key = f"{artist}|{title}"
                groups[key].append(f)
        except Exception:
            continue

    # Return groups with more than one file
    return [files for files in groups.values() if len(files) > 1]


def audit_library(library_path: Path) -> dict:
    """Perform full library audit."""
    result = {
        "library_path": str(library_path),
        "total_files": 0,
        "valid_files": 0,
        "invalid_files": 0,
        "missing_metadata": 0,
        "missing_artwork": 0,
        "corrupted_files": [],
        "filename_issues": [],
        "duplicate_groups": [],
        "directory_structure": {},
    }

    if not library_path.exists():
        result["error"] = f"Library path does not exist: {library_path}"
        return result

    # Find all M4A files
    m4a_files = list(library_path.rglob("*.m4a"))
    result["total_files"] = len(m4a_files)

    # Analyze each file
    for filepath in m4a_files:
        rel_path = str(filepath.relative_to(library_path))

        # Check validity
        is_valid, error_msg = is_valid_m4a(filepath)
        if is_valid:
            result["valid_files"] += 1
        else:
            result["invalid_files"] += 1
            result["corrupted_files"].append({"path": rel_path, "error": error_msg})
            continue

        # Check metadata
        try:
            audio = MP4(filepath)
            meta_status = check_metadata(audio)

            if not meta_status["has_title"] or not meta_status["has_artist"]:
                result["missing_metadata"] += 1

            if not meta_status["has_artwork"]:
                result["missing_artwork"] += 1
        except Exception as e:
            result["invalid_files"] += 1
            result["corrupted_files"].append({"path": rel_path, "error": str(e)[:100]})
            continue

        # Check filename safety
        is_safe, issues = check_filename_safety(filepath)
        if not is_safe:
            result["filename_issues"].append({"path": rel_path, "issues": issues})

        # Track directory structure
        artist_dir = filepath.parent.name
        parent_dir = filepath.parent.parent.name if filepath.parent.parent else ""
        if parent_dir not in result["directory_structure"]:
            result["directory_structure"][parent_dir] = {}
        if artist_dir not in result["directory_structure"][parent_dir]:
            result["directory_structure"][parent_dir][artist_dir] = 0
        result["directory_structure"][parent_dir][artist_dir] += 1

    # Find duplicates
    dup_groups = find_duplicates(m4a_files)
    for group in dup_groups:
        result["duplicate_groups"].append([str(f.relative_to(library_path)) for f in group])

    return result


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Audit music library for issues")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("library_path", nargs="?", default=None, help="Path to library")
    args = parser.parse_args()

    library_path = Path(args.library_path) if args.library_path else LIBRARY

    if args.json:
        result = audit_library(library_path)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        result = audit_library(library_path)

        print(f"\n{'=' * 60}")
        print("LIBRARY AUDIT REPORT")
        print(f"{'=' * 60}")
        print(f"Library: {result['library_path']}")
        print()

        if "error" in result:
            print(f"ERROR: {result['error']}")
            return

        print(f"Total files:      {result['total_files']}")
        print(f"Valid files:      {result['valid_files']}")
        print(f"Invalid files:    {result['invalid_files']}")
        print(f"Missing metadata: {result['missing_metadata']}")
        print(f"Missing artwork:  {result['missing_artwork']}")
        print()

        if result["corrupted_files"]:
            print(f"CORRUPTED FILES ({len(result['corrupted_files'])}):")
            for item in result["corrupted_files"][:10]:
                print(f"  - {item['path']}: {item['error']}")
            if len(result["corrupted_files"]) > 10:
                print(f"  ... and {len(result['corrupted_files']) - 10} more")
            print()

        if result["filename_issues"]:
            print(f"FILENAME ISSUES ({len(result['filename_issues'])}):")
            for item in result["filename_issues"][:10]:
                print(f"  - {item['path']}: {', '.join(item['issues'])}")
            if len(result["filename_issues"]) > 10:
                print(f"  ... and {len(result['filename_issues']) - 10} more")
            print()

        if result["duplicate_groups"]:
            print(f"SUSPICIOUS DUPLICATES ({len(result['duplicate_groups'])} groups):")
            for i, group in enumerate(result["duplicate_groups"][:5], 1):
                print(f"  Group {i}:")
                for f in group:
                    print(f"    - {f}")
            if len(result["duplicate_groups"]) > 5:
                print(f"  ... and {len(result['duplicate_groups']) - 5} more groups")
            print()

        print(f"{'=' * 60}")
        print("Audit complete. This report is read-only - no changes were made.")
        print(f"{'=' * 60}\n")


if __name__ == "__main__":
    main()
