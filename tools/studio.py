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
    # ★ 2026-11-15：单例保护。
    #   实测这台机器上同时跑了 5 个 `tools/studio.py --port 7861` —— 每双击一次
    #   就多一个，端口在 Windows 上靠 SO_REUSEADDR 居然都能绑上，
    #   于是 5 个进程抢同一批文件、抢同一个 app 子进程，删模型/装模型互相打架。
    #   现在：端口已被占 → 认为已经有一个在跑，直接打开浏览器并退出。
    import socket as _sock
    with _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM) as _s:
        _s.settimeout(0.4)
        _busy = _s.connect_ex(("127.0.0.1", args.port)) == 0
    if _busy:
        print(f"ForgeForm Studio 已经在 {args.port} 跑着，直接打开页面。", flush=True)
        if not args.no_browser:
            webbrowser.open(f"http://127.0.0.1:{args.port}")
        return

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
