#!/usr/bin/env python3
"""
LumiTrail - Combined entry point for PyInstaller exe.
Runs preprocessing then starts the server with browser.
Optionally watches input directories for new/changed files.
"""

import argparse
import os
import shutil
import sys
import threading
from pathlib import Path

# viewer.html is bundled as data - find it relative to exe or script
def get_bundled_viewer():
    """Get path to bundled viewer.html (works both frozen and unfrozen)."""
    if getattr(sys, 'frozen', False):
        # Running as PyInstaller bundle
        return Path(sys._MEIPASS) / 'viewer.html'
    else:
        return Path(__file__).parent / 'viewer.html'


def _copy_viewer(out_dir: Path):
    viewer_src = get_bundled_viewer()
    if viewer_src.exists():
        shutil.copy2(str(viewer_src), str(out_dir / 'index.html'))


def _run_scan(input_dirs, out_dir: Path, db_path: Path):
    """Run scan_directory for all input dirs. Returns after completion."""
    from preprocess import scan_directory, init_db
    conn = init_db(str(db_path))
    for input_dir in input_dirs:
        input_path = Path(input_dir)
        if not input_path.exists():
            print(f"WARN: {input_dir} does not exist, skipping", file=sys.stderr)
            continue
        label = input_path.name
        print(f"\nScanning: {input_dir} ({label})", flush=True)
        scan_directory(input_dir, str(out_dir), conn, label)
    conn.close()


def start_watcher(input_dirs, out_dir: Path, db_path: Path, poll_interval: int):
    """
    Watch input directories for new/changed GPX and photo files using polling.
    Runs in a background daemon thread.
    Uses PollingObserver (no inotify needed) for NAS / Docker volume compatibility.
    """
    try:
        from watchdog.observers.polling import PollingObserver
        from watchdog.events import FileSystemEventHandler
    except ImportError:
        print("WARN: watchdog not installed, file watching disabled. "
              "Install it with: pip install watchdog", file=sys.stderr)
        return None

    WATCH_EXTENSIONS = {'.gpx', '.jpg', '.jpeg', '.png'}

    class RescanHandler(FileSystemEventHandler):
        def __init__(self):
            self._timer = None
            self._lock = threading.Lock()

        def _relevant(self, path: str) -> bool:
            return os.path.splitext(path)[1].lower() in WATCH_EXTENSIONS

        def on_created(self, event):
            if not event.is_directory and self._relevant(event.src_path):
                self._schedule()

        def on_modified(self, event):
            if not event.is_directory and self._relevant(event.src_path):
                self._schedule()

        def on_moved(self, event):
            if not event.is_directory and self._relevant(event.dest_path):
                self._schedule()

        def _schedule(self):
            # Debounce: wait 5s after last event before rescanning.
            # This avoids hammering the DB while a batch of files is being copied.
            with self._lock:
                if self._timer:
                    self._timer.cancel()
                self._timer = threading.Timer(5.0, self._rescan)
                self._timer.daemon = True
                self._timer.start()

        def _rescan(self):
            print("\nWatcher: changes detected, rescanning...", flush=True)
            try:
                _run_scan(input_dirs, out_dir, db_path)
                _copy_viewer(out_dir)
                print("Watcher: rescan complete", flush=True)
            except Exception as e:
                print(f"Watcher: rescan error: {e}", file=sys.stderr)

    handler = RescanHandler()
    observer = PollingObserver(timeout=poll_interval)
    for input_dir in input_dirs:
        if Path(input_dir).exists():
            observer.schedule(handler, input_dir, recursive=True)
            print(f"Watching: {input_dir} (poll interval: {poll_interval}s)")

    observer.daemon = True
    observer.start()
    return observer


def main():
    parser = argparse.ArgumentParser(
        description='LumiTrail - Visualize GPX tracks and geotagged photos on a map',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='Example:\n  lumitrail.py /data -o /output --watch'
    )
    parser.add_argument('input_dirs', nargs='+', help='Directories to scan for GPX and photos')
    parser.add_argument('-o', '--output', default='./map_output',
                        help='Output directory (default: ./map_output)')
    parser.add_argument('-p', '--port', type=int, default=8080,
                        help='Server port (default: 8080)')
    parser.add_argument('--no-browser', action='store_true',
                        help='Do not auto-open browser')
    parser.add_argument('--preprocess-only', action='store_true',
                        help='Only preprocess, do not start server')
    parser.add_argument('--serve-only', action='store_true',
                        help='Only start server (skip preprocessing)')
    parser.add_argument('--watch', action='store_true',
                        help='Watch input directories for new/changed files and reprocess automatically')
    parser.add_argument('--watch-interval', type=int, default=30,
                        help='File system poll interval in seconds for --watch (default: 30)')
    args = parser.parse_args()

    out_dir = Path(args.output).resolve()

    # --- Preprocessing ---
    if not args.serve_only:
        from preprocess import init_db, DB_NAME
        out_dir.mkdir(parents=True, exist_ok=True)
        db_path = out_dir / DB_NAME
        _run_scan(args.input_dirs, out_dir, db_path)
        print(f"\nDone! Output in: {out_dir}")
        _copy_viewer(out_dir)
        if viewer_dst := out_dir / 'index.html':
            if viewer_dst.exists():
                print(f"  Viewer copied to: {viewer_dst}")

    if args.preprocess_only:
        return

    # --- File watcher (background thread) ---
    from preprocess import DB_NAME
    db_path = out_dir / DB_NAME

    observer = None
    if args.watch:
        observer = start_watcher(args.input_dirs, out_dir, db_path, args.watch_interval)

    # --- Server ---
    from server import MapRequestHandler
    from functools import partial
    from http.server import HTTPServer
    import webbrowser

    if not db_path.exists():
        print(f"ERROR: {db_path} not found. Run preprocessing first.", file=sys.stderr)
        sys.exit(1)

    MapRequestHandler.db_path = str(db_path)
    handler = partial(MapRequestHandler, directory=str(out_dir))

    server = HTTPServer(('0.0.0.0', args.port), handler)
    url = f'http://localhost:{args.port}'
    print(f"Serving at {url}")
    print(f"  Press Ctrl+C to stop\n")

    if not args.no_browser:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.shutdown()
    finally:
        if observer:
            observer.stop()
            observer.join()


if __name__ == '__main__':
    main()
