#!/usr/bin/env python3
"""
LumiTrail - Combined entry point for PyInstaller exe.
Runs preprocessing then starts the server with browser.
"""

import argparse
import sys
import shutil
from pathlib import Path

# viewer.html is bundled as data - find it relative to exe or script
def get_bundled_viewer():
    """Get path to bundled viewer.html (works both frozen and unfrozen)."""
    if getattr(sys, 'frozen', False):
        # Running as PyInstaller bundle
        return Path(sys._MEIPASS) / 'viewer.html'
    else:
        return Path(__file__).parent / 'viewer.html'


def main():
    parser = argparse.ArgumentParser(
        description='LumiTrail - Visualize GPX tracks and geotagged photos on a map',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='Example:\n  gpx_photo_map.exe "X:\\Photos\\2025 Wandern Rad"'
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
    args = parser.parse_args()

    out_dir = Path(args.output).resolve()

    # --- Preprocessing ---
    if not args.serve_only:
        from preprocess import scan_directory, init_db, DB_NAME

        out_dir.mkdir(parents=True, exist_ok=True)

        db_path = out_dir / DB_NAME
        conn = init_db(str(db_path))

        for input_dir in args.input_dirs:
            input_path = Path(input_dir)
            if not input_path.exists():
                print(f"WARN: {input_dir} does not exist, skipping", file=sys.stderr)
                continue
            label = input_path.name
            print(f"\nScanning: {input_dir} ({label})")
            scan_directory(input_dir, str(out_dir), conn, label)

        conn.close()
        print(f"\nDone! Output in: {out_dir}")

        # Copy bundled viewer
        viewer_src = get_bundled_viewer()
        if viewer_src.exists():
            shutil.copy2(str(viewer_src), str(out_dir / 'index.html'))
            print(f"  Viewer copied to: {out_dir / 'index.html'}")

    if args.preprocess_only:
        return

    # --- Server ---
    from server import MapRequestHandler
    from functools import partial
    from http.server import HTTPServer
    from preprocess import DB_NAME
    import webbrowser

    db_path = out_dir / DB_NAME
    if not db_path.exists():
        print(f"ERROR: {db_path} not found. Run preprocessing first.", file=sys.stderr)
        sys.exit(1)

    MapRequestHandler.db_path = str(db_path)
    handler = partial(MapRequestHandler, directory=str(out_dir))

    server = HTTPServer(('127.0.0.1', args.port), handler)
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


if __name__ == '__main__':
    main()
