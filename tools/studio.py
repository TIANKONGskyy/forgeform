"""Start the dependency-free graphical setup and model manager."""
import argparse
from pathlib import Path
import sys
import webbrowser
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.studio import Studio, make_server

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=7861)
    parser.add_argument('--app-port', type=int, default=7860)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    studio = Studio(app_port=args.app_port)
    server = make_server(studio, args.port)
    if not args.no_browser:
        webbrowser.open(f'http://127.0.0.1:{args.port}')
    print(f'ForgeForm Studio: http://127.0.0.1:{args.port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        studio.cancel()
    finally:
        server.server_close()

if __name__ == '__main__':
    main()
