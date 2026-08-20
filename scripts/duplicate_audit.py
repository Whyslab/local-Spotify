#!/usr/bin/env python3
"""Duplicate audit script - find potential duplicate tracks in library.

Usage:
    python duplicate_audit.py [--json] [library_path]

Compares files by:
- Artist name
- Track title  
- Duration (if available)
- Album name
- Optional: file hash for exact duplicates

Does NOT delete anything - read-only report.
"""
import json
import sys
import hashlib
from pathlib import Path
from collections import defaultdict

try:
    from mutagen.mp4 import MP4
except ImportError:
    print("Error: mutagen not installed. Run: pip install mutagen")
    sys.exit(1)

# Add parent directory to path to import config
sys.path.insert(0, str(Path(__file__).parent.parent / "adder"))
try:
    from config import LIBRARY
except ImportError:
    LIBRARY = Path.home() / "Music" / "Normalized Library"


def get_file_hash(filepath: Path, chunk_size: int = 8192) -> str:
    """Calculate SHA256 hash of file (for exact duplicate detection)."""
    sha256 = hashlib.sha256()
    try:
        with open(filepath, 'rb') as f:
            for chunk in iter(lambda: f.read(chunk_size), b''):
                sha256.update(chunk)
        return sha256.hexdigest()
    except Exception:
        return ""


def extract_metadata(filepath: Path) -> dict:
    """Extract metadata from M4A file."""
    result = {
        "path": str(filepath),
        "size": filepath.stat().st_size if filepath.exists() else 0,
        "hash": "",
        "artist": "",
        "title": "",
        "album": "",
        "duration": 0,
    }
    
    try:
        audio = MP4(filepath)
        result["artist"] = (audio.get("\xa9ART") or audio.get("aART") or "").lower().strip()
        result["title"] = (audio.get("\xa9nam") or "").lower().strip()
        result["album"] = (audio.get("\xa9alb") or "").lower().strip()
        
        # Get duration if available
        info = audio.info
        if info and hasattr(info, 'length'):
            result["duration"] = round(info.length, 1)
    except Exception:
        pass
    
    return result


def normalize_for_comparison(meta: dict) -> tuple:
    """Create normalized key for comparison."""
    # Remove common variations for matching
    artist = meta["artist"]
    title = meta["title"]
    
    # Remove common suffixes/prefixes that might vary
    for pattern in [" feat. ", " ft. ", " vs. ", " & ", " - ", " (", " ["]:
        if pattern in artist:
            artist = artist.split(pattern)[0].strip()
    
    # Normalize duration to 5-second buckets
    duration_bucket = round(meta["duration"] / 5) * 5 if meta["duration"] else 0
    
    return (artist, title, meta["album"], duration_bucket)


def find_duplicate_groups(files: list[Path], use_hash: bool = False) -> list[dict]:
    """Find groups of potentially duplicate files."""
    groups = defaultdict(list)
    exact_hashes = defaultdict(list)
    
    for i, filepath in enumerate(files):
        if i % 100 == 0 and i > 0:
            print(f"  Processing {i}/{len(files)} files...", file=sys.stderr)
        
        meta = extract_metadata(filepath)
        
        # For exact duplicate detection
        if use_hash and meta["size"] > 0:
            meta["hash"] = get_file_hash(filepath)
            if meta["hash"]:
                exact_hashes[meta["hash"]].append(meta)
        
        # Group by metadata similarity
        key = normalize_for_comparison(meta)
        if key[0] and key[1]:  # Only if we have artist and title
            groups[key].append(meta)
    
    # Build result: groups with more than one candidate
    duplicate_groups = []
    
    # Exact duplicates (same hash)
    for hash_val, metas in exact_hashes.items():
        if len(metas) > 1:
            duplicate_groups.append({
                "type": "exact_duplicate",
                "hash": hash_val,
                "files": [{"path": m["path"], "size": m["size"]} for m in metas],
                "reason": "Identical file content (SHA256 match)",
            })
    
    # Metadata-based duplicates
    for key, metas in groups.items():
        if len(metas) > 1:
            # Check if this group is already covered by exact hash matching
            paths_in_group = {m["path"] for m in metas}
            already_covered = any(
                paths_in_group <= {m["path"] for m in g["files"]}
                for g in duplicate_groups
                if g["type"] == "exact_duplicate"
            )
            
            if not already_covered:
                artist, title, album, duration = key
                duplicate_groups.append({
                    "type": "metadata_similar",
                    "artist": artist,
                    "title": title,
                    "album": album,
                    "duration": duration,
                    "files": [
                        {
                            "path": m["path"],
                            "size": m["size"],
                            "duration": m["duration"],
                            "hash": m["hash"][:16] if m["hash"] else "",
                        }
                        for m in metas
                    ],
                    "reason": f"Similar metadata: artist='{artist}', title='{title}', duration~{duration}s",
                })
    
    return duplicate_groups


def audit_duplicates(library_path: Path, use_hash: bool = False) -> dict:
    """Perform duplicate audit on library."""
    result = {
        "library_path": str(library_path),
        "total_files_scanned": 0,
        "duplicate_groups_found": 0,
        "exact_duplicates": 0,
        "metadata_duplicates": 0,
        "potential_space_savings_bytes": 0,
        "groups": [],
    }
    
    if not library_path.exists():
        result["error"] = f"Library path does not exist: {library_path}"
        return result
    
    # Find all M4A files
    m4a_files = list(library_path.rglob("*.m4a"))
    result["total_files_scanned"] = len(m4a_files)
    
    if len(m4a_files) == 0:
        return result
    
    print(f"Scanning {len(m4a_files)} files for duplicates...", file=sys.stderr)
    
    # Find duplicates
    dup_groups = find_duplicate_groups(m4a_files, use_hash)
    
    result["duplicate_groups_found"] = len(dup_groups)
    result["groups"] = dup_groups
    
    # Calculate stats
    for group in dup_groups:
        if group["type"] == "exact_duplicate":
            result["exact_duplicates"] += 1
        else:
            result["metadata_duplicates"] += 1
        
        # Estimate space savings (all but largest file in group could be removed)
        sizes = [f.get("size", 0) for f in group["files"]]
        if sizes:
            sizes.sort(reverse=True)
            result["potential_space_savings_bytes"] += sum(sizes[1:])
    
    return result


def format_size(bytes_val: int) -> str:
    """Format bytes as human-readable size."""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if abs(bytes_val) < 1024:
            return f"{bytes_val:.1f} {unit}"
        bytes_val /= 1024
    return f"{bytes_val:.1f} TB"


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="Find duplicate tracks in music library")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--hash", action="store_true", dest="use_hash", 
                       help="Use file hashing for exact duplicate detection (slower)")
    parser.add_argument("library_path", nargs="?", default=None, help="Path to library")
    args = parser.parse_args()
    
    library_path = Path(args.library_path) if args.library_path else LIBRARY
    
    if args.json:
        result = audit_duplicates(library_path, use_hash=args.use_hash)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        result = audit_duplicates(library_path, use_hash=args.use_hash)
        
        print(f"\n{'='*60}")
        print(f"DUPLICATE AUDIT REPORT")
        print(f"{'='*60}")
        print(f"Library: {result['library_path']}")
        print()
        
        if "error" in result:
            print(f"ERROR: {result['error']}")
            return
        
        print(f"Files scanned:           {result['total_files_scanned']}")
        print(f"Duplicate groups found:  {result['duplicate_groups_found']}")
        print(f"  - Exact duplicates:    {result['exact_duplicates']}")
        print(f"  - Metadata matches:    {result['metadata_duplicates']}")
        print(f"Potential space savings: {format_size(result['potential_space_savings_bytes'])}")
        print()
        
        if result["groups"]:
            print(f"DUPLICATE GROUPS:")
            print(f"{'-'*60}")
            for i, group in enumerate(result["groups"][:10], 1):
                print(f"\n[{i}] {group['type'].upper()}")
                print(f"    Reason: {group['reason']}")
                print(f"    Files:")
                for f in group["files"]:
                    print(f"      - {f['path']}")
                    if f.get('size'):
                        print(f"        Size: {format_size(f['size'])}")
                    if f.get('duration'):
                        print(f"        Duration: {f['duration']}s")
            
            if len(result["groups"]) > 10:
                print(f"\n... and {len(result['groups']) - 10} more groups")
            
            print()
        
        print(f"{'='*60}")
        print("This report is READ-ONLY. No files were modified or deleted.")
        print("Review duplicate groups manually before taking any action.")
        print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
