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
    # ★ 2026-11-16：从"端口通不通"改成"先认人再决定"。
    #   原来的判据只看端口能不能连 —— 如果占着 7861 的是**别的程序**（nginx、
    #   同事的 dev server），它会以为"Studio 已经在跑"，打开浏览器给人看别人的页面。
    #   现在：是我们的 Studio → 复用；是别人的 → 换一个空闲端口；没占 → 用原端口。
    from backend import ports as _ports
    _r = _ports.resolve(args.port, script="studio.py")
    if _r["action"] == "reuse":
        print(f"ForgeForm Studio {_r['why']}。", flush=True)
        if not args.no_browser:
            webbrowser.open(f"http://127.0.0.1:{_r['port']}")
        return
    if _r["action"] == "fail":
        print(f"起不来：{_r['why']}", flush=True)
        raise SystemExit(1)
    _port = int(_r["port"])
    if _port != args.port:
        print(f"[port] {_r['why']}", flush=True)

    # 起 Studio 之前先把**我们自己的旧 app** 清掉：否则旧 app 还占着 7860，
    # 新的 app 会换到 7862 继续跑 —— 两份实例各自加载模型，显存被劈开。
    try:
        _killed = _ports.kill_stale("app.py")
        if _killed:
            print(f"[port] 清掉 {len(_killed)} 个残留的创作空间进程", flush=True)
    except Exception:
        pass

    studio = Studio(app_port=args.app_port)
    server = make_server(studio, _port)
    if not args.no_browser:
        webbrowser.open(f'http://127.0.0.1:{_port}')
    print(f'ForgeForm Studio: http://127.0.0.1:{_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        studio.cancel()
    finally:
        server.server_close()

if __name__ == '__main__':
    main()
