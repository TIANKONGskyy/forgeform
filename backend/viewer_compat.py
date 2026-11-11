"""Serve the bundled 3D viewer with a stable WebGL backend.

The bundled Babylon viewer selects WebGPU from property presence alone. Chromium
WebViews can expose navigator.gpu while requestAdapter returns null.
"""
from pathlib import Path

CHOICE = 'function ad(){return"gpu"in navigator&&"chrome"in window?"WebGPU":"WebGL"}'

def webgl_bundle(source: str) -> str:
    return source.replace(CHOICE, 'function ad(){return"WebGL"}')

def register_viewer_compat(server_app):
    import gradio
    from starlette.responses import Response
    from starlette.routing import Route
    assets = Path(gradio.__file__).parent / 'templates' / 'frontend' / 'assets'
    for file in assets.glob('index-*.js'):
        source = file.read_text(encoding='utf-8')
        if CHOICE not in source:
            continue
        content = webgl_bundle(source)
        async def serve(_request, content=content):
            return Response(content, media_type='application/javascript',
                            headers={'Cache-Control':'no-cache'})
        server_app.router.routes.insert(0, Route('/assets/'+file.name, serve))
        print('[viewer] 3D 查看器采用 WebGL（兼容无 WebGPU adapter 的设备）')
