"""Start the dependency-free graphical setup and model manager."""
import argparse
from pathlib import Path
import sys
import threading
import webbrowser
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.studio import Studio, make_server

def open_desktop(webview, url, server=None, studio=None):
    webview.settings["ALLOW_DOWNLOADS"] = True
    window = webview.create_window(
        "铸想 ForgeForm", url + "/?desktop=1", width=1360, height=900, min_size=(760, 560),
        background_color="#0a1019",
    )
    window.events.shown += lambda: print(f"ForgeForm Desktop: {url}", flush=True)
    if server is not None:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        webview.start(gui="edgechromium")
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
            studio.cancel()
            if studio.app and studio.app.poll() is None:
                studio.app.terminate()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=7861)
    parser.add_argument('--app-port', type=int, default=7860)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--desktop', action='store_true')
    args = parser.parse_args()
    if args.desktop:
        import webview
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
    # 避开创作空间(7860)与 API(7863)：Studio 只该占自己的端口
    _r = _ports.resolve(args.port, script="studio.py", avoid=(7860, 7862, 7863))
    # 端口上如果蹲着的是**创作空间**（旧版抢过 7861），那不是"Studio 在跑"，
    # 必须当冲突处理 —— 否则 Studio 直接退出，用户以为控制台坏了。
    _who = _ports.who_is(args.port)
    if _r["action"] == "reuse" and _who.get("kind") not in ("studio", None):
        _alt = _ports.pick_free(args.port + 1, 20, avoid=(7860, 7862, 7863))
        if _alt:
            print(f"[port] {args.port} 上是 {_who['kind']}（不是 Studio），Studio 改用 {_alt}",
                  flush=True)
            _r = {"action": "use", "port": _alt, "why": ""}
    if _r["action"] == "reuse":
        print(f"ForgeForm Studio {_r['why']}。", flush=True)
        url = f"http://127.0.0.1:{_r['port']}"
        if args.desktop:
            open_desktop(webview, url)
        elif not args.no_browser:
            webbrowser.open(url)
        return
    if _r["action"] == "fail":
        print(f"起不来：{_r['why']}", flush=True)
        raise SystemExit(1)
    _port = int(_r["port"])
    if _port != args.port:
        print(f"[port] {_r['why']}", flush=True)

    # 起 Studio 之前先把**我们自己的旧 app** 清掉：否则旧 app 还占着 7860，
    # 新的 app 会换到 7862 继续跑 —— 两份实例各自加载模型，显存被劈开。
    if not args.desktop:
        try:
            _killed = _ports.kill_stale("app.py")
            if _killed:
                print(f"[port] 清掉 {len(_killed)} 个残留的创作空间进程", flush=True)
        except Exception:
            pass

    studio = Studio(app_port=args.app_port)
    server = make_server(studio, _port)
    if args.desktop:
        open_desktop(webview, f'http://127.0.0.1:{_port}', server, studio)
        return
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
