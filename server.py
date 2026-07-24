#!/usr/bin/env python3
"""
LumiTrail Server.
Serves the viewer + thumbnails statically, and provides a REST API
backed by SQLite for lazy-loading tracks/photos by viewport.
Original photos are served from their real filesystem locations.
"""

import argparse
import json
import os
import sys
import sqlite3
import time
import traceback
import webbrowser
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse, parse_qs

from preprocess import DB_NAME

# Mit VERBOSE=1 (env var) werden ALLE Requests geloggt, nicht nur Fehler.
# Wichtig fuer's Debuggen eines haengenden/langsamen Servers in Docker:
#   docker run/compose -e VERBOSE=1 ...
VERBOSE = os.environ.get('VERBOSE', '') not in ('', '0', 'false')


class MapRequestHandler(SimpleHTTPRequestHandler):
    """Handler: static files + /api/* + /original/*"""

    db_path = ""  # set by server setup
    # Socket-Timeout: ein Client, der die Verbindung nicht sauber schliesst
    # (Tab zu, Netzwerkabbruch), darf einen write()-Syscall nicht ewig
    # blockieren lassen. Ohne das haengt der komplette Server fest, weil
    # HTTPServer/ThreadingHTTPServer sonst kein Limit setzt.
    timeout = 30

    def end_headers(self):
        # Nie cached ausliefern, damit Änderungen an index.html (viewer.html)
        # sofort sichtbar werden.
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Pragma', 'no-cache')
        super().end_headers()

    def _get_db(self):
        """Get a thread-local SQLite connection (read-only).
        timeout=10s: falls preprocess.py/--watch gerade schreibt und die DB
        kurz gelockt ist, warten statt sofort mit 'database is locked' zu
        crashen (bzw. den Request auf unbestimmte Zeit haengen zu lassen)."""
        return sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=10)

    def do_GET(self):
        started = time.monotonic()
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path.startswith('/api/'):
                self.handle_api(path, parse_qs(parsed.query))
            elif path.startswith('/original/'):
                self.serve_original(path)
            else:
                super().do_GET()
        finally:
            elapsed = time.monotonic() - started
            if VERBOSE or elapsed > 2.0:
                print(f"[req] {self.path} took {elapsed:.2f}s", file=sys.stderr, flush=True)

    def handle_api(self, path, params):
        """Route /api/* requests."""
        try:
            if path == '/api/stats':
                self.api_stats()
            elif path == '/api/tours':
                self.api_tours()
            elif path == '/api/tree':
                self.api_tree()
            elif path == '/api/photos':
                self.api_photos(params)
            elif path == '/api/tracks':
                self.api_tracks(params)
            elif path.startswith('/api/track/'):
                track_id = path.split('/')[-1]
                self.api_track_detail(track_id)
            elif path.startswith('/api/photo/'):
                photo_id = path.split('/')[-1]
                self.api_photo_detail(photo_id)
            else:
                self.send_error(404, "Unknown API endpoint")
        except Exception as e:
            print(f"[ERROR] {path}: {e}", file=sys.stderr, flush=True)
            traceback.print_exc(file=sys.stderr)
            sys.stderr.flush()
            self.send_json({"error": str(e)}, 500)

    def api_stats(self):
        conn = self._get_db()
        tracks = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        photos = conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
        tours = conn.execute(
            "SELECT COUNT(DISTINCT tour) FROM "
            "(SELECT tour FROM tracks UNION ALL SELECT tour FROM photos)"
        ).fetchone()[0]
        conn.close()
        self.send_json({"tracks": tracks, "photos": photos, "tours": tours})

    def api_tours(self):
        conn = self._get_db()
        rows = conn.execute(
            "SELECT DISTINCT tour FROM "
            "(SELECT tour FROM tracks UNION SELECT tour FROM photos) ORDER BY tour"
        ).fetchall()
        conn.close()
        self.send_json([r[0] for r in rows])

    def api_tree(self):
        """Build hierarchical folder tree from photos and tracks."""
        conn = self._get_db()

        # Fetch all photos and tracks with their source paths and dates
        photos = conn.execute(
            "SELECT source, date, id, filename, tour FROM photos"
        ).fetchall()
        tracks = conn.execute(
            "SELECT source, date, id, name, tour FROM tracks"
        ).fetchall()
        conn.close()

        # Build tree structure
        root = {"name": "", "path": "", "type": "folder", "children": {}, "files": [],
                "track_count": 0, "photo_count": 0, "date_min": None, "date_max": None}

        def update_counts_upwards(node, type_, date):
            """Update counts and date range up the tree to root."""
            while node is not None:
                if type_ == 'track':
                    node['track_count'] += 1
                else:
                    node['photo_count'] += 1
                if date:
                    if node['date_min'] is None or date < node['date_min']:
                        node['date_min'] = date
                    if node['date_max'] is None or date > node['date_max']:
                        node['date_max'] = date
                # Move to parent (we need to track parents)
                # For now, we'll use a different approach - build with parent refs
                node = node.get('_parent')

        def add_to_tree(source, type_, id_, name, date, tour):
            # Normalize path separators (Windows uses backslash, Unix uses forward slash)
            import re
            parts = re.split(r'[/\\\\]+', source)
            if len(parts) < 2:
                return  # no folder structure
            folder_parts = parts[:-1]  # all but filename
            node = root
            path_parts = []
            for part in folder_parts:
                path_parts.append(part)
                path = '/'.join(path_parts)
                if part not in node['children']:
                    new_node = {"name": part, "path": path, "type": "folder",
                                "children": {}, "files": [],
                                "track_count": 0, "photo_count": 0,
                                "date_min": None, "date_max": None,
                                "_parent": node}
                    node['children'][part] = new_node
                node = node['children'][part]
            # Add file to leaf folder
            file_info = {"id": id_, "name": name, "type": type_, "date": date, "tour": tour}
            node['files'].append(file_info)
            # Update counts up the tree
            update_counts_upwards(node, type_, date)

        for src, date, id_, filename, tour in photos:
            add_to_tree(src, 'photo', id_, filename, date, tour)

        for src, date, id_, name, tour in tracks:
            add_to_tree(src, 'track', id_, name, date, tour)

        # Convert children dicts to lists for JSON serialization
        def convert_node(node):
            result = {
                "name": node["name"],
                "path": node["path"],
                "type": node["type"],
                "track_count": node["track_count"],
                "photo_count": node["photo_count"],
                "date_min": node["date_min"],
                "date_max": node["date_max"],
                "children": [convert_node(c) for c in sorted(node["children"].values(), key=lambda x: x["name"].lower())],
                "files": sorted(node["files"], key=lambda x: (x["type"], x["name"].lower()))
            }
            return result

        tree = convert_node(root)
        self.send_json(tree["children"])

    def api_photos(self, params):
        """Return photos within bounds. Params: south, west, north, east, tours (comma-sep), date_min, date_max."""
        south = float(params.get('south', ['-90'])[0])
        north = float(params.get('north', ['90'])[0])
        west = float(params.get('west', ['-180'])[0])
        east = float(params.get('east', ['180'])[0])
        tour_filter = params.get('tours', [''])[0]
        date_min = params.get('date_min', [''])[0]
        date_max = params.get('date_max', [''])[0]

        conn = self._get_db()
        sql = "SELECT id, lat, lon, date, tour, thumb, filename FROM photos WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?"
        args = [south, north, west, east]

        if tour_filter:
            tours = tour_filter.split(',')
            placeholders = ','.join('?' * len(tours))
            sql += f" AND tour IN ({placeholders})"
            args.extend(tours)
        if date_min:
            sql += " AND substr(date, 1, 10) >= ?"
            args.append(date_min)
        if date_max:
            sql += " AND substr(date, 1, 10) <= ?"
            args.append(date_max)

        rows = conn.execute(sql, args).fetchall()
        conn.close()

        photos = [
            {"id": r[0], "lat": r[1], "lon": r[2], "date": r[3],
             "tour": r[4], "thumb": (('/' + r[5]) if r[5] and not r[5].startswith('/') else r[5]), "filename": r[6]}
            for r in rows
        ]
        self.send_json(photos)

    def api_tracks(self, params):
        """Return track overviews within bounds. Params: south, west, north, east, tours (comma-sep), date_min, date_max."""
        south = float(params.get('south', ['-90'])[0])
        north = float(params.get('north', ['90'])[0])
        west = float(params.get('west', ['-180'])[0])
        east = float(params.get('east', ['180'])[0])
        tour_filter = params.get('tours', [''])[0]
        date_min = params.get('date_min', [''])[0]
        date_max = params.get('date_max', [''])[0]

        conn = self._get_db()
        # Filter tracks whose bounding box intersects the viewport
        sql = """SELECT id, name, date, tour, point_count, overview, bounds
                 FROM tracks"""
        rows = conn.execute(sql).fetchall()
        conn.close()

        result = []
        for r in rows:
            bounds = json.loads(r[6])  # [[min_lat, min_lon], [max_lat, max_lon]]
            t_south, t_west = bounds[0]
            t_north, t_east = bounds[1]
            # Check bounding box intersection
            if t_north < south or t_south > north or t_east < west or t_west > east:
                continue
            if tour_filter:
                if r[3] not in tour_filter.split(','):
                    continue
            if date_min and r[2] and r[2][:10] < date_min:
                continue
            if date_max and r[2] and r[2][:10] > date_max:
                continue
            result.append({
                "id": r[0], "name": r[1], "date": r[2], "tour": r[3],
                "point_count": r[4], "overview": json.loads(r[5]),
                "bounds": bounds,
            })
        self.send_json(result)

    def api_track_detail(self, track_id):
        """Return full-resolution points for a single track."""
        conn = self._get_db()
        row = conn.execute(
            "SELECT full_points FROM tracks WHERE id = ?", (track_id,)
        ).fetchone()
        conn.close()
        if not row:
            self.send_error(404, f"Track {track_id} not found")
            return
        # full_points is already a JSON string, send directly
        self.send_raw_json(row[0])

    def api_photo_detail(self, photo_id):
        """Return a single photo's metadata (incl. coordinates) by id."""
        conn = self._get_db()
        row = conn.execute(
            "SELECT id, lat, lon, date, tour, thumb, filename FROM photos WHERE id = ?",
            (photo_id,),
        ).fetchone()
        conn.close()
        if not row:
            self.send_error(404, f"Photo {photo_id} not found")
            return
        photo = {
            "id": row[0], "lat": row[1], "lon": row[2], "date": row[3],
            "tour": row[4],
            "thumb": (('/' + row[5]) if row[5] and not row[5].startswith('/') else row[5]),
            "filename": row[6],
        }
        self.send_json(photo)

    def serve_original(self, path):
        """Serve an original photo by its ID hash."""
        filename = unquote(path.split('/')[-1])
        photo_id = filename.rsplit('.', 1)[0] if '.' in filename else filename

        conn = self._get_db()
        row = conn.execute(
            "SELECT original FROM photos WHERE id = ?", (photo_id,)
        ).fetchone()
        conn.close()

        if not row or not os.path.isfile(row[0]):
            self.send_error(404, f"Original not found: {photo_id}")
            return

        original_path = row[0]
        try:
            ext = os.path.splitext(original_path)[1].lower()
            content_types = {
                '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                '.png': 'image/png', '.gif': 'image/gif',
            }
            content_type = content_types.get(ext, 'application/octet-stream')

            with open(original_path, 'rb') as f:
                data = f.read()

            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', len(data))
            self.send_header('Cache-Control', 'public, max-age=86400')
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self.send_error(500, f"Error reading {original_path}: {e}")

    def send_json(self, obj, status=200):
        """Send a JSON response."""
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def send_raw_json(self, json_str, status=200):
        """Send a pre-serialized JSON string."""
        body = json_str.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Standard: nur Fehler (4xx/5xx) loggen, um das Log nicht mit jedem
        # Tile/API-Request zuzumuellen. Mit VERBOSE=1 wird alles geloggt -
        # hilfreich, um einen haengenden/langsamen Server zu debuggen.
        msg = str(args[0]) if args else ''
        if VERBOSE or '404' in msg or ' 5' in msg:
            super().log_message(format, *args)


def main():
    parser = argparse.ArgumentParser(description='LumiTrail Server')
    parser.add_argument('-d', '--directory', default='./map_output',
                        help='Map output directory to serve (default: ./map_output)')
    parser.add_argument('-p', '--port', type=int, default=8080,
                        help='Port (default: 8080)')
    parser.add_argument('--no-browser', action='store_true',
                        help='Do not auto-open browser')
    args = parser.parse_args()

    serve_dir = Path(args.directory).resolve()
    db_path = serve_dir / DB_NAME

    if not db_path.exists():
        print(f"ERROR: {db_path} not found. Run preprocess.py first.", file=sys.stderr)
        sys.exit(1)

    # Quick stats
    conn = sqlite3.connect(str(db_path))
    tracks = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    photos = conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
    conn.close()
    print(f"Database: {tracks} tracks, {photos} photos")

    MapRequestHandler.db_path = str(db_path)
    handler = partial(MapRequestHandler, directory=str(serve_dir))

    # ThreadingHTTPServer statt HTTPServer: ein einzelner haengender/langsamer
    # Request (z.B. abgebrochene Verbindung beim Schreiben der Antwort) darf
    # nicht mehr den kompletten Server fuer alle anderen Clients blockieren.
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler)
    server.daemon_threads = True
    url = f'http://localhost:{args.port}'
    print(f"Serving at {url}")
    print(f"  Map data: {serve_dir}")
    print(f"  Press Ctrl+C to stop\n")

    if not args.no_browser:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.shutdown()


if __name__ == '__main__':
    main()
