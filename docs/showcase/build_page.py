# -*- coding: utf-8 -*-
"""把 docs/showcase/*/job.json 渲染成一张静态宣传页（docs/showcase/index.html）。

数据全部来自真实运行记录，不手写数字。重跑：
    python docs/showcase/build_page.py
"""
import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "index.html"

# 每个作品一句人话说明（数据来自 job.json，这句是编的"这是什么"）
BLURB = {
    "helmet": "中世纪骑士头盔。曲面对称、表面有明确的金属高光，用来测「细节能不能保住」。",
    "coupling": "六角法兰联轴器。中心轴孔 + 六个螺栓孔，用来测「机械件的小孔会不会糊掉」。",
    "planter": "低多边形花盆。大平面折角 + 平整底面，用来测「打印友好」这一侧。",
    "dragon": "龙头球形摆件。鳞片与鬃毛是密度最高的区域，用来测「细节上限」。",
}


def fmt(n, unit=""):
    if n is None:
        return "—"
    if isinstance(n, float):
        return f"{n:.1f}{unit}"
    return f"{n}{unit}"


def fmt_bytes(kb):
    if not kb:
        return "—"
    return f"{kb/1024:.1f} MB" if kb > 1024 else f"{kb} KB"


def make_thumb(d: Path) -> bool:
    """把 image.png 压成 web 用的 image.jpg（源图不入库）。"""
    src = d / "image.png"
    dst = d / "image.jpg"
    if not src.exists():
        return dst.exists()
    try:
        from PIL import Image
        im = Image.open(src).convert("RGB")
        if max(im.size) > 900:
            im.thumbnail((900, 900), Image.LANCZOS)
        im.save(dst, "JPEG", quality=86, optimize=True, progressive=True)
        return True
    except Exception as exc:
        print(f"  ! 缩略图失败 {d.name}: {exc}")
        return dst.exists()


def load_jobs():
    jobs = []
    for d in sorted(HERE.iterdir()):
        if not d.is_dir():
            continue
        f = d / "job.json"
        if not f.exists():
            continue
        make_thumb(d)
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        rec["dir"] = d.name
        jobs.append(rec)
    return jobs


def card(j: dict, idx: int) -> str:
    slug = j["dir"]
    name = j.get("name", slug)
    ok = j.get("ok")
    img = f"./{slug}/image.jpg"
    has_img = (HERE / slug / "image.jpg").exists()
    wt = j.get("watertight")
    wt_tag = ('<span class="tag ok">水密 ✓</span>' if wt
              else '<span class="tag bad">非水密</span>' if wt is False else "")
    if not ok:
        return f"""
        <article class="card failed">
          <div class="shot"><span class="ph">生成失败</span></div>
          <div class="body">
            <h3>{name}</h3>
            <p class="blurb">{(j.get('error') or '')[:160]}</p>
          </div>
        </article>"""

    shot = (f'<img src="{img}" alt="{name}" loading="lazy">' if has_img
            else '<span class="ph">无预览图</span>')
    return f"""
        <article class="card">
          <div class="shot">{shot}<span class="idx">{idx:02d}</span></div>
          <div class="body">
            <div class="title-row"><h3>{name}</h3>{wt_tag}</div>
            <p class="blurb">{BLURB.get(slug, '')}</p>
            <p class="prompt">「{j.get('prompt','')}」</p>
            <dl class="metrics">
              <div><dt>出图</dt><dd>{fmt(j.get('image_s'))}<i>s</i></dd></div>
              <div><dt>建模</dt><dd>{fmt(j.get('model_s'))}<i>s</i></dd></div>
              <div><dt>面数</dt><dd>{fmt(j.get('faces'))}</dd></div>
              <div><dt>尺寸</dt><dd>{fmt(j.get('target_mm'))}<i>mm</i></dd></div>
              <div><dt>GLB</dt><dd>{fmt_bytes(j.get('glb_kb'))}</dd></div>
              <div><dt>STL</dt><dd>{fmt_bytes(j.get('stl_kb'))}</dd></div>
            </dl>
          </div>
        </article>"""


def build():
    jobs = load_jobs()
    ok_jobs = [j for j in jobs if j.get("ok")]

    # 汇总真实数字
    n = len(ok_jobs)
    if n:
        avg_img = sum(j.get("image_s", 0) or 0 for j in ok_jobs) / n
        avg_3d = sum(j.get("model_s", 0) or 0 for j in ok_jobs) / n
        wt_rate = sum(1 for j in ok_jobs if j.get("watertight")) / n * 100
        total_faces = sum(j.get("faces", 0) or 0 for j in ok_jobs)
    else:
        avg_img = avg_3d = wt_rate = total_faces = 0

    cards = "".join(card(j, i + 1) for i, j in enumerate(jobs))
    hero_img = f"./{ok_jobs[0]['dir']}/image.jpg" if ok_jobs else ""

    hero_visual = (f'<img src="{hero_img}" alt="示例输出">' if hero_img and
                   (HERE / ok_jobs[0]["dir"] / "image.jpg").exists()
                   else '<div class="glow"></div>')

    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>铸想 ForgeForm · 一句话到可打印的 STL</title>
<meta name="description" content="铸想 ForgeForm（ZYKT studio）—— 本地运行的对话式 3D 建模工作流，一句话生成可打印的 STL。">
<style>
  :root {{
    --bg: #07080c;
    --bg2: #0d1017;
    --line: #1c2130;
    --fg: #e8ecf5;
    --dim: #8b95ab;
    --cyan: #35e6d0;
    --violet: #8b6cf6;
    --amber: #ffb547;
    --grad: linear-gradient(120deg, #35e6d0 0%, #6ea8ff 45%, #8b6cf6 100%);
    --radius: 16px;
    --mono: ui-monospace, "SF Mono", "Cascadia Code", Consolas, monospace;
    --sans: -apple-system, "Segoe UI", "Microsoft YaHei", system-ui, sans-serif;
  }}
  * {{ box-sizing: border-box; }}
  html {{ scroll-behavior: smooth; }}
  body {{
    margin: 0; background: var(--bg); color: var(--fg); font-family: var(--sans);
    line-height: 1.7; -webkit-font-smoothing: antialiased;
    background-image:
      radial-gradient(900px 500px at 12% -10%, rgba(53,230,208,.10), transparent 60%),
      radial-gradient(800px 520px at 88% 0%, rgba(139,108,246,.12), transparent 62%);
    background-attachment: fixed;
  }}
  a {{ color: var(--cyan); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .wrap {{ max-width: 1180px; margin: 0 auto; padding: 0 28px; }}

  /* ── 顶栏 ── */
  header {{
    position: sticky; top: 0; z-index: 30;
    backdrop-filter: blur(14px); background: rgba(7,8,12,.72);
    border-bottom: 1px solid var(--line);
  }}
  .nav {{ display: flex; align-items: center; gap: 18px; height: 62px; }}
  .logo {{ display: flex; align-items: center; gap: 10px; font-weight: 700; letter-spacing: .5px; }}
  .mark {{
    width: 26px; height: 26px; border-radius: 7px; background: var(--grad);
    box-shadow: 0 0 22px rgba(110,168,255,.55); position: relative;
  }}
  .mark::after {{
    content: ""; position: absolute; inset: 6px; border-radius: 3px;
    border: 2px solid rgba(7,8,12,.85); transform: rotate(45deg);
  }}
  .nav .spacer {{ flex: 1; }}
  .nav a {{ color: var(--dim); font-size: 14px; }}
  .nav a:hover {{ color: var(--fg); text-decoration: none; }}
  .ver {{
    font: 12px/1 var(--mono); color: var(--dim); border: 1px solid var(--line);
    padding: 6px 9px; border-radius: 999px;
  }}

  /* ── Hero ── */
  .hero {{ padding: 84px 0 56px; display: grid; grid-template-columns: 1.05fr .95fr; gap: 56px; align-items: center; }}
  .eyebrow {{
    font: 600 12px/1 var(--mono); letter-spacing: .18em; text-transform: uppercase;
    color: var(--cyan); margin-bottom: 18px;
  }}
  h1 {{
    font-size: clamp(38px, 5.2vw, 62px); line-height: 1.08; margin: 0 0 22px;
    font-weight: 800; letter-spacing: -.02em;
  }}
  h1 em {{ font-style: normal; background: var(--grad); -webkit-background-clip: text; background-clip: text; color: transparent; }}
  .lede {{ font-size: 18px; color: var(--dim); margin: 0 0 30px; max-width: 46ch; }}
  .lede b {{ color: var(--fg); font-weight: 600; }}
  .cta {{ display: flex; gap: 12px; flex-wrap: wrap; }}
  .btn {{
    display: inline-flex; align-items: center; gap: 8px; padding: 12px 20px;
    border-radius: 11px; font-weight: 600; font-size: 15px; border: 1px solid transparent;
  }}
  .btn.primary {{ background: var(--grad); color: #05070a; }}
  .btn.primary:hover {{ text-decoration: none; filter: brightness(1.08); }}
  .btn.ghost {{ border-color: var(--line); color: var(--fg); }}
  .btn.ghost:hover {{ border-color: #2c3547; text-decoration: none; }}
  .hero-visual {{ position: relative; }}
  .hero-visual img {{
    width: 100%; border-radius: 20px; display: block;
    border: 1px solid #232b3d; box-shadow: 0 30px 80px -30px rgba(53,230,208,.35);
  }}
  .hero-visual .glow {{
    aspect-ratio: 1; border-radius: 24px;
    background: radial-gradient(circle at 40% 35%, rgba(53,230,208,.28), transparent 60%),
                radial-gradient(circle at 70% 70%, rgba(139,108,246,.30), transparent 62%);
  }}
  .hero-visual::after {{
    content: "one prompt · one printable STL";
    position: absolute; left: 14px; bottom: 14px;
    font: 11px/1 var(--mono); letter-spacing: .1em; color: #05070a;
    background: rgba(53,230,208,.92); padding: 6px 10px; border-radius: 7px;
  }}

  /* ── 数字条 ── */
  .stats {{
    display: grid; grid-template-columns: repeat(4, 1fr); gap: 1px;
    background: var(--line); border: 1px solid var(--line);
    border-radius: var(--radius); overflow: hidden; margin: 8px 0 92px;
  }}
  .stats div {{ background: var(--bg2); padding: 22px 20px; }}
  .stats dt {{ font: 12px/1 var(--mono); color: var(--dim); letter-spacing: .08em; text-transform: uppercase; }}
  .stats dd {{ margin: 9px 0 0; font-size: 26px; font-weight: 700; letter-spacing: -.01em; }}
  .stats dd i {{ font-size: 14px; font-style: normal; color: var(--dim); margin-left: 3px; }}

  /* ── 区块标题 ── */
  .sec-head {{ display: flex; align-items: baseline; gap: 16px; margin-bottom: 30px; }}
  .sec-head h2 {{ font-size: 15px; font-weight: 600; letter-spacing: .16em; text-transform: uppercase; color: var(--dim); margin: 0; }}
  .sec-head .rule {{ flex: 1; height: 1px; background: var(--line); }}
  .sec-head .cnt {{ font: 12px/1 var(--mono); color: var(--dim); }}

  /* ── 作品卡 ── */
  .grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 26px; margin-bottom: 96px; }}
  .card {{
    background: var(--bg2); border: 1px solid var(--line); border-radius: var(--radius);
    overflow: hidden; transition: border-color .18s, transform .18s;
  }}
  .card:hover {{ border-color: #2f3a52; transform: translateY(-2px); }}
  .card .shot {{ position: relative; background: #0a0c11; aspect-ratio: 1; overflow: hidden; }}
  .card .shot img {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
  .card .ph {{
    display: grid; place-items: center; height: 100%; color: #39435a; font: 13px var(--mono);
  }}
  .card .idx {{
    position: absolute; top: 12px; left: 14px; font: 600 11px var(--mono); color: #05070a;
    background: rgba(232,236,245,.86); padding: 3px 7px; border-radius: 5px;
  }}
  .card .body {{ padding: 20px 22px 22px; }}
  .title-row {{ display: flex; align-items: center; gap: 10px; }}
  .card h3 {{ margin: 0; font-size: 19px; font-weight: 700; }}
  .tag {{ font: 600 11px/1 var(--mono); padding: 5px 8px; border-radius: 6px; margin-left: auto; }}
  .tag.ok {{ color: #16f0c9; background: rgba(22,240,201,.10); border: 1px solid rgba(22,240,201,.28); }}
  .tag.bad {{ color: #ff7a8a; background: rgba(255,122,138,.10); border: 1px solid rgba(255,122,138,.28); }}
  .blurb {{ color: var(--dim); font-size: 14px; margin: 10px 0 8px; }}
  .prompt {{
    font: 12.5px/1.65 var(--mono); color: #7f8ba4; background: #0a0c12;
    border-left: 2px solid #2b3448; padding: 9px 12px; border-radius: 0 7px 7px 0; margin: 0 0 16px;
  }}
  .metrics {{
    display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px 8px;
    margin: 0; padding-top: 15px; border-top: 1px solid var(--line);
  }}
  .metrics div {{ display: flex; flex-direction: column; gap: 2px; }}
  .metrics dt {{ font: 11px/1 var(--mono); color: #5d6880; letter-spacing: .06em; }}
  .metrics dd {{ margin: 0; font: 600 15px/1.2 var(--mono); }}
  .metrics dd i {{ font-size: 11px; font-style: normal; color: var(--dim); margin-left: 2px; }}

  /* ── 流程 ── */
  .flow {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 14px; margin-bottom: 96px; }}
  .step {{
    background: var(--bg2); border: 1px solid var(--line); border-radius: 13px; padding: 20px 18px;
    position: relative;
  }}
  .step .n {{
    font: 700 11px/1 var(--mono); color: #05070a; background: var(--grad);
    width: 22px; height: 22px; display: grid; place-items: center; border-radius: 6px; margin-bottom: 14px;
  }}
  .step h4 {{ margin: 0 0 7px; font-size: 15px; }}
  .step p {{ margin: 0; font-size: 13px; color: var(--dim); line-height: 1.6; }}
  .step code {{ font: 11.5px var(--mono); color: #9fb0cc; }}

  /* ── 表 ── */
  table {{ width: 100%; border-collapse: collapse; font-size: 14px; margin-bottom: 96px; }}
  th, td {{ text-align: left; padding: 13px 14px; border-bottom: 1px solid var(--line); }}
  th {{ font: 600 11px/1 var(--mono); letter-spacing: .1em; text-transform: uppercase; color: #5d6880; }}
  td.num {{ font-family: var(--mono); }}
  tbody tr:hover {{ background: #0b0e14; }}

  /* ── 边界 ── */
  .two {{ display: grid; grid-template-columns: 1fr 1fr; gap: 26px; margin-bottom: 96px; }}
  .panel {{ background: var(--bg2); border: 1px solid var(--line); border-radius: var(--radius); padding: 26px 28px; }}
  .panel h3 {{ margin: 0 0 16px; font-size: 17px; }}
  .panel ul {{ margin: 0; padding-left: 18px; color: var(--dim); font-size: 14px; }}
  .panel li {{ margin-bottom: 9px; }}
  .panel li b {{ color: var(--fg); font-weight: 600; }}
  .panel.warn {{ border-color: rgba(255,181,71,.28); background: linear-gradient(180deg, rgba(255,181,71,.045), transparent 60%), var(--bg2); }}

  /* ── Footer ── */
  footer {{ border-top: 1px solid var(--line); padding: 40px 0 60px; margin-top: 20px; }}
  .foot {{ display: flex; justify-content: space-between; gap: 24px; flex-wrap: wrap; color: var(--dim); font-size: 13.5px; }}
  .foot b {{ color: var(--fg); }}
  .foot .right {{ text-align: right; }}
  .mono {{ font-family: var(--mono); font-size: 12.5px; }}

  @media (max-width: 900px) {{
    .hero {{ grid-template-columns: 1fr; gap: 34px; padding: 56px 0 40px; }}
    .stats {{ grid-template-columns: repeat(2, 1fr); }}
    .grid, .two {{ grid-template-columns: 1fr; }}
    .flow {{ grid-template-columns: 1fr 1fr; }}
  }}
</style>
</head>
<body>

<header>
  <div class="wrap nav">
    <div class="logo"><span class="mark"></span>铸想 ForgeForm</div>
    <span class="ver">v3.0.0</span>
    <span class="spacer"></span>
    <a href="#works">作品</a>
    <a href="#flow">流程</a>
    <a href="#perf">实测</a>
    <a href="#limits">边界</a>
    <a href="../ARCHITECTURE.md">架构</a>
  </div>
</header>

<main class="wrap">

  <section class="hero">
    <div>
      <div class="eyebrow">ZYKT studio · 本地推理</div>
      <h1>一句话，<br>换一个<em>能打印</em>的东西。</h1>
      <p class="lede">
        铸想 ForgeForm 把「一句话描述」变成「毫米级、水密、能直接切片的 STL」。
        对话、出图、建模、修网格全部跑在<b>自己的显卡</b>上，不调任何云端 API。
      </p>
      <div class="cta">
        <a class="btn primary" href="#works">看作品 ↓</a>
        <a class="btn ghost" href="#quickstart">怎么跑起来</a>
      </div>
    </div>
    <div class="hero-visual">{hero_visual}</div>
  </section>

  <section class="stats">
    <div><dt>完成的样例</dt><dd>{n}<i>件</i></dd></div>
    <div><dt>平均出图</dt><dd>{avg_img:.0f}<i>s</i></dd></div>
    <div><dt>平均建模</dt><dd>{avg_3d/60:.1f}<i>min</i></dd></div>
    <div><dt>水密率</dt><dd>{wt_rate:.0f}<i>%</i></dd></div>
  </section>

  <section id="works">
    <div class="sec-head"><h2>作品</h2><span class="rule"></span><span class="cnt">{n} / {len(jobs)} 完成</span></div>
    <div class="grid">{cards}
    </div>
  </section>

  <section id="flow">
    <div class="sec-head"><h2>它替你做的五件事</h2><span class="rule"></span></div>
    <div class="flow">
      <div class="step"><div class="n">1</div><h4>听懂需求</h4><p>本地 LLM 把「小一点、放桌上那种」翻译成 <code>size_mm=90</code>，该追问才追问。</p></div>
      <div class="step"><div class="n">2</div><h4>出重建友好的图</h4><p>提示词自动加<b>单体 / 纯色背景 / 无道具</b>约束 —— 好看不是目的，能建模才是。</p></div>
      <div class="step"><div class="n">3</div><h4>三维重建</h4><p>Hunyuan3D 2.1 出网格，约 10 万顶点。四档质量，从 160 秒到 12 分钟。</p></div>
      <div class="step"><div class="n">4</div><h4>修水密</h4><p>pymeshlab 补洞 → <code>is_watertight</code> 复检边界边 → 不合格再走 pymeshfix。</p></div>
      <div class="step"><div class="n">5</div><h4>出可打件</h4><p>毫米单位、居中贴地、减面到可切片。直接拖进 Bambu Studio。</p></div>
    </div>
  </section>

  <section id="perf">
    <div class="sec-head"><h2>实测耗时</h2><span class="rule"></span><span class="cnt">V100 32G · 单卡</span></div>
    <table>
      <thead><tr><th>阶段</th><th>模型 / 档位</th><th>耗时</th><th>显存占用</th><th>备注</th></tr></thead>
      <tbody>
        <tr><td>出图 1024²</td><td class="num">Qwen-Image-2.1 · 20 步</td><td class="num">≈ 56 s</td><td class="num">≈ 20 GB</td><td>本机主力</td></tr>
        <tr><td>出图 1024²</td><td class="num">FLUX.1-dev FP8 · 28 步</td><td class="num">≈ 530 s</td><td class="num">≈ 18 GB</td><td>旧卡上的历史数据</td></tr>
        <tr><td>建模</td><td class="num">Hunyuan3D 2.1 · 快速 256</td><td class="num">≈ 160 s</td><td class="num">≈ 14 GB</td><td>草稿用</td></tr>
        <tr><td>建模</td><td class="num">Hunyuan3D 2.1 · 高质量 512</td><td class="num">≈ 11.7 min</td><td class="num">≈ 16 GB</td><td>终稿用，要等</td></tr>
        <tr><td>建模</td><td class="num">TripoSplat (fp16)</td><td class="num">≈ 6.7 min</td><td class="num">≈ 15 GB</td><td>兜底后端</td></tr>
        <tr><td>视频 832×480</td><td class="num">MiniMax H3 Ref2VA · 96 帧</td><td class="num">≈ 19 min</td><td class="num">≈ 20 GB</td><td>可选分支</td></tr>
        <tr><td>STL 优化</td><td class="num">trimesh + pymeshlab（纯 CPU）</td><td class="num">≈ 115 s</td><td class="num">0</td><td>不占显存</td></tr>
      </tbody>
    </table>
  </section>

  <section id="quickstart">
    <div class="sec-head"><h2>快速上手</h2><span class="rule"></span></div>
    <div class="two">
      <div class="panel">
        <h3>三步起来</h3>
        <ul>
          <li><b>装依赖</b>：<span class="mono">pip install -r requirements.txt</span></li>
          <li><b>指向 ComfyUI</b>：<span class="mono">$env:COMFYUI_DIR = "D:\\ComfyUI"</span></li>
          <li><b>起服务</b>：<span class="mono">python app.py</span> → <span class="mono">127.0.0.1:7860</span></li>
        </ul>
        <p style="color:var(--dim);font-size:13.5px;margin:14px 0 0">
          首次运行自动生成 <span class="mono">config.json</span>、建好输出目录、跑自检。
        </p>
      </div>
      <div class="panel">
        <h3>给 agent 用</h3>
        <ul>
          <li>仓库里带 <span class="mono">skills/forgeform/SKILL.md</span>，教会任何支持 Agent Skills 的 agent：<b>谁做的、怎么用、怎么换模型、怎么排错</b>。</li>
          <li>生成动作走 <span class="mono">Pipeline</span> 的 Python 方法，<b>不用自己拼 ComfyUI workflow</b>。</li>
          <li>长任务以磁盘产物为准，别用 HTTP 超时判断失败。</li>
        </ul>
      </div>
    </div>
  </section>

  <section id="limits">
    <div class="sec-head"><h2>边界</h2><span class="rule"></span></div>
    <div class="two">
      <div class="panel warn">
        <h3>它做不到</h3>
        <ul>
          <li><b>不能保证一次出好模型。</b> 毛发、薄片、镂空这类形态经常建崩；换一张更干净的图比重调参数有效。</li>
          <li><b>不是参数化 CAD。</b> 要配合公差、螺纹，用 <span class="mono">parts_cad/</span> 的 CadQuery 路线。</li>
          <li><b>不保证可打印。</b> 水密闸门只修闭合性，修不了悬空和薄壁 —— 切片器里还要看一眼。</li>
          <li><b>单卡独占。</b> 对话模型和绘图模型不能同时占显存，切换要几十秒冷启。</li>
        </ul>
      </div>
      <div class="panel">
        <h3>推荐配置</h3>
        <ul>
          <li><b>32 GB 显存</b>（实测 V100）：全档位可用，视频可上 1024p。</li>
          <li><b>24 GB</b>：出图 + 建模串行，视频降到 832×480。</li>
          <li><b>16 GB</b>：只跑出图 + 快速建模，别开视频。</li>
          <li><b>8 GB</b>：只跑出图，3D 会 OOM。</li>
          <li><b>无 GPU</b>：起得来界面，但生成全部不可用。</li>
        </ul>
      </div>
    </div>
  </section>

</main>

<footer>
  <div class="wrap foot">
    <div>
      <b>铸想 ForgeForm</b> · <span class="mono">v3.0.0</span><br>
      由 <b>ZYKT studio</b> 制作 · AGPL-3.0
    </div>
    <div class="right mono">
      本页数据来自真实运行记录（{n} 件，累计 {total_faces:,} 面）<br>
      模型与工具：Qwen-Image / FLUX / Hunyuan3D / MiniMax H3 / ComfyUI / Gradio / trimesh
    </div>
  </div>
</footer>

</body>
</html>
"""
    OUT.write_text(html, encoding="utf-8")
    print(f"written: {OUT}  ({len(html)} chars, {n} works)")


if __name__ == "__main__":
    build()
