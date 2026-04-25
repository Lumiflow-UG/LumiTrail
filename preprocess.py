#!/usr/bin/env python3
"""
LumiTrail Preprocessor
Scans directories for GPX tracks and geotagged photos,
stores metadata in a SQLite database + generates thumbnails.
"""

import argparse
import json
import os
import sys
import hashlib
import sqlite3
from pathlib import Path
from datetime import datetime

# Ensure stdout can handle arbitrary Unicode (e.g. filenames with special chars)
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(errors='replace')

import gpxpy
from PIL import Image, ExifTags

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
THUMB_SIZE = (300, 300)
PHOTO_EXTENSIONS = {'.jpg', '.jpeg', '.png'}
GPX_EXTENSIONS = {'.gpx'}
# Douglas-Peucker simplification: max points per track for overview
MAX_POINTS_OVERVIEW = 150

# ---------------------------------------------------------------------------
# EXIF GPS extraction
# ---------------------------------------------------------------------------

def _get_exif_data(image: Image.Image) -> dict:
    """Return EXIF data as a dict with human-readable tag names."""
    exif = image._getexif()
    if not exif:
        return {}
    return {ExifTags.TAGS.get(k, k): v for k, v in exif.items()}


def _convert_to_degrees(value):
    """Convert GPS coordinates stored as rational tuples to float degrees."""
    d, m, s = value
    return float(d) + float(m) / 60.0 + float(s) / 3600.0


def get_gps_from_exif(filepath: str) -> tuple[float, float] | None:
    """Extract (lat, lon) from a JPEG's EXIF GPS data. Returns None if missing."""
    try:
        img = Image.open(filepath)
        exif = _get_exif_data(img)
        gps_info = exif.get('GPSInfo')
        if not gps_info:
            return None

        # Decode GPS tags
        gps = {ExifTags.GPSTAGS.get(k, k): v for k, v in gps_info.items()}

        lat = _convert_to_degrees(gps.get('GPSLatitude', []))
        lon = _convert_to_degrees(gps.get('GPSLongitude', []))
        if gps.get('GPSLatitudeRef', 'N') == 'S':
            lat = -lat
        if gps.get('GPSLongitudeRef', 'E') == 'W':
            lon = -lon
        if lat == 0.0 and lon == 0.0:
            return None
        return (lat, lon)
    except Exception:
        return None


def get_photo_date(filepath: str) -> str | None:
    """Extract date taken from EXIF. Returns ISO date string or None."""
    try:
        img = Image.open(filepath)
        exif = _get_exif_data(img)
        date_str = exif.get('DateTimeOriginal') or exif.get('DateTime')
        if date_str:
            dt = datetime.strptime(date_str, '%Y:%m:%d %H:%M:%S')
            return dt.isoformat()
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# GPX processing
# ---------------------------------------------------------------------------

def simplify_track(points: list[tuple[float, float]], max_points: int) -> list[tuple[float, float]]:
    """Simple decimation to reduce point count. Keeps first/last and evenly samples."""
    if len(points) <= max_points:
        return points
    step = len(points) / max_points
    result = []
    for i in range(max_points):
        idx = int(i * step)
        result.append(points[idx])
    if result[-1] != points[-1]:
        result.append(points[-1])
    return result


def parse_gpx(filepath: str) -> dict | None:
    """Parse a GPX file, return track info dict or None on error."""
    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            gpx = gpxpy.parse(f)
    except Exception as e:
        print(f"  WARN: Could not parse {filepath}: {e}", file=sys.stderr)
        return None

    all_points = []
    for track in gpx.tracks:
        for segment in track.segments:
            for pt in segment.points:
                all_points.append((pt.latitude, pt.longitude))

    # Also check routes (some GPX files use routes instead of tracks)
    for route in gpx.routes:
        for pt in route.points:
            all_points.append((pt.latitude, pt.longitude))

    if not all_points:
        return None

    # Get track name
    name = None
    if gpx.tracks:
        name = gpx.tracks[0].name
    elif gpx.routes:
        name = gpx.routes[0].name
    name = name or Path(filepath).stem

    # Get date from first point or filename
    date = None
    for track in gpx.tracks:
        for seg in track.segments:
            if seg.points and seg.points[0].time:
                date = seg.points[0].time.isoformat()
                break
        if date:
            break

    # Simplify for overview
    overview = simplify_track(all_points, MAX_POINTS_OVERVIEW)

    # Bounding box
    lats = [p[0] for p in all_points]
    lons = [p[1] for p in all_points]

    return {
        'name': name,
        'date': date,
        'point_count': len(all_points),
        'overview': overview,  # simplified [[lat,lon], ...]
        'bounds': [[min(lats), min(lons)], [max(lats), max(lons)]],
        'full_points': all_points,  # stored in separate file
    }


# ---------------------------------------------------------------------------
# Thumbnail generation
# ---------------------------------------------------------------------------

def make_thumbnail(src: str, dst: str):
    """Create a JPEG thumbnail, preserving EXIF orientation."""
    try:
        img = Image.open(src)
        try:
            from PIL import ImageOps
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass
        img.thumbnail(THUMB_SIZE, Image.LANCZOS)
        img.save(dst, 'JPEG', quality=80)
    except Exception as e:
        print(f"  WARN: Thumbnail failed for {src}: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# SQLite database
# ---------------------------------------------------------------------------

DB_NAME = 'lumitrail.db'

def init_db(db_path: str) -> sqlite3.Connection:
    """Initialize SQLite database with schema."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS photos (
            id TEXT PRIMARY KEY,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            date TEXT,
            tour TEXT,
            thumb TEXT,
            original TEXT,
            filename TEXT,
            source TEXT UNIQUE,
            fingerprint TEXT
        );
        CREATE TABLE IF NOT EXISTS tracks (
            id TEXT PRIMARY KEY,
            name TEXT,
            date TEXT,
            tour TEXT,
            point_count INTEGER,
            overview TEXT,
            bounds TEXT,
            full_points TEXT,
            source TEXT UNIQUE,
            fingerprint TEXT
        );
        CREATE TABLE IF NOT EXISTS skipped (
            source TEXT PRIMARY KEY,
            fingerprint TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_photos_lat ON photos(lat);
        CREATE INDEX IF NOT EXISTS idx_photos_lon ON photos(lon);
        CREATE INDEX IF NOT EXISTS idx_photos_tour ON photos(tour);
        CREATE INDEX IF NOT EXISTS idx_tracks_tour ON tracks(tour);
    """)
    conn.commit()
    return conn


def _fingerprint_from_stat(st) -> str:
    """Quick fingerprint from an existing stat result (no extra syscall)."""
    return f"{st.st_size}_{st.st_mtime_ns}"


def _walk_scandir(top: str):
    """Recursively yield (DirEntry, stat) using os.scandir — reuses cached stat."""
    try:
        with os.scandir(top) as it:
            dirs = []
            for entry in it:
                if entry.is_file(follow_symlinks=False):
                    yield entry, entry.stat(follow_symlinks=False)
                elif entry.is_dir(follow_symlinks=False):
                    dirs.append(entry.path)
            for d in dirs:
                yield from _walk_scandir(d)
    except PermissionError:
        pass


# ---------------------------------------------------------------------------
# Directory scanning
# ---------------------------------------------------------------------------

def scan_directory(base_dir: str, output_dir: str, conn: sqlite3.Connection, source_label: str = ""):
    """
    Recursively scan base_dir for GPX and photo files.
    Generate thumbnails in output_dir/thumbs/.
    Uses SQLite fingerprints for delta processing - unchanged files are skipped.
    """
    base = Path(base_dir)
    out = Path(output_dir)
    thumb_dir = out / 'thumbs'
    thumb_dir.mkdir(parents=True, exist_ok=True)

    # Build fingerprint lookup from DB
    existing_fp = {}
    for row in conn.execute("SELECT source, fingerprint FROM photos"):
        existing_fp[row[0]] = row[1]
    for row in conn.execute("SELECT source, fingerprint FROM tracks"):
        existing_fp[row[0]] = row[1]
    for row in conn.execute("SELECT source, fingerprint FROM skipped"):
        existing_fp[row[0]] = row[1]

    new_tracks = 0
    new_photos = 0
    cached = 0
    skipped_photos = 0

    # Collect files via os.scandir (reuses stat from directory listing on Windows)
    all_files = [(entry, st) for entry, st in _walk_scandir(str(base))]
    total = len(all_files)

    for i, (entry, st) in enumerate(all_files):
        ext = os.path.splitext(entry.name)[1].lower()
        if ext not in PHOTO_EXTENSIONS and ext not in GPX_EXTENSIONS:
            if (i + 1) % 50 == 0 or i == total - 1:
                print(f"  [{i+1}/{total}] Scanning...", flush=True)
            continue

        filepath = Path(entry.path)
        rel = filepath.relative_to(base)
        rel_str = str(rel)
        fp = _fingerprint_from_stat(st)

        # Determine tour/group from parent folder
        parts = rel.parts
        if len(parts) > 1:
            tour = parts[-2]
        else:
            tour = source_label or base.name

        # Progress
        if (i + 1) % 50 == 0 or i == total - 1:
            print(f"  [{i+1}/{total}] Scanning...", flush=True)
            conn.commit()  # persist incrementally so Ctrl+C doesn't lose progress

        # Delta check: skip if fingerprint matches
        if existing_fp.get(rel_str) == fp:
            cached += 1
            continue

        # --- GPX files ---
        if ext in GPX_EXTENSIONS:
            track_id = hashlib.md5(rel_str.encode()).hexdigest()[:12]
            print(f"  GPX: {rel}", flush=True)
            track_data = parse_gpx(str(filepath))
            if track_data:
                conn.execute("""
                    INSERT OR REPLACE INTO tracks (id, name, date, tour, point_count,
                        overview, bounds, full_points, source, fingerprint)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    track_id, track_data['name'], track_data['date'], tour,
                    track_data['point_count'],
                    json.dumps(track_data['overview']),
                    json.dumps(track_data['bounds']),
                    json.dumps(track_data['full_points']),
                    rel_str, fp
                ))
                new_tracks += 1

        # --- Photo files ---
        elif ext in PHOTO_EXTENSIONS:
            photo_id = hashlib.md5(rel_str.encode()).hexdigest()[:12]
            thumb_name = photo_id + '.jpg'
            thumb_path = thumb_dir / thumb_name

            gps = get_gps_from_exif(str(filepath))
            if gps is None:
                skipped_photos += 1
                conn.execute(
                    "INSERT OR REPLACE INTO skipped (source, fingerprint) VALUES (?, ?)",
                    (rel_str, fp)
                )
                continue

            lat, lon = gps
            date = get_photo_date(str(filepath))

            if not thumb_path.exists():
                make_thumbnail(str(filepath), str(thumb_path))

            conn.execute("""
                INSERT OR REPLACE INTO photos (id, lat, lon, date, tour, thumb,
                    original, filename, source, fingerprint)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                photo_id, lat, lon, date, tour,
                f'thumbs/{thumb_name}',
                str(filepath.resolve()),
                filepath.name, rel_str, fp
            ))
            new_photos += 1

    conn.commit()

    # Get totals from DB
    total_tracks = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    total_photos = conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]

    print(f"\n  Result: {total_tracks} tracks, {total_photos} geotagged photos "
          f"({skipped_photos} without GPS skipped)")
    if cached:
        print(f"  Cache hits: {cached} files unchanged (skipped)")
    if new_tracks or new_photos:
        print(f"  New/updated: {new_tracks} tracks, {new_photos} photos")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='LumiTrail Preprocessor')
    parser.add_argument('input_dirs', nargs='+', help='Directories to scan')
    parser.add_argument('-o', '--output', default='./map_output',
                        help='Output directory (default: ./map_output)')
    args = parser.parse_args()

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    db_path = str(out_dir / DB_NAME)
    conn = init_db(db_path)

    for input_dir in args.input_dirs:
        input_path = Path(input_dir)
        if not input_path.exists():
            print(f"WARN: {input_dir} does not exist, skipping", file=sys.stderr)
            continue
        label = input_path.name
        print(f"\nScanning: {input_dir} ({label})")
        scan_directory(input_dir, str(out_dir), conn, label)

    # Summary
    total_tracks = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    total_photos = conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
    tours = [r[0] for r in conn.execute(
        "SELECT DISTINCT tour FROM (SELECT tour FROM tracks UNION SELECT tour FROM photos) ORDER BY tour"
    )]
    db_size = Path(db_path).stat().st_size / 1024

    print(f"\nDone! Output in: {out_dir}")
    print(f"  {DB_NAME}: {db_size:.1f} KB")
    print(f"  {total_tracks} tracks, {total_photos} photos, {len(tours)} tours")

    # Copy viewer HTML
    viewer_src = Path(__file__).parent / 'viewer.html'
    if viewer_src.exists():
        import shutil
        shutil.copy2(viewer_src, out_dir / 'index.html')
        print(f"  Viewer copied to: {out_dir / 'index.html'}")

    conn.close()


if __name__ == '__main__':
    main()
