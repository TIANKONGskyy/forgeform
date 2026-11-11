"""联网找参考图 → 抠主体 → 白底 —— 2026-09-23 宇凯要求的新链路第一步。

用户真实需要往往网上已有现成图像（玩偶、产品、角色周边…），直接拿来当参考，
比让扩散模型凭空画更贴需求，也给建模更干净的输入。流程：

    search_images(query)      必应图片搜索 → 直链列表（无需 API key）
    fetch_image(url)          下载 + 尺寸/格式校验（挡掉图标、损坏、超大图）
    cutout_white_bg(img)      调 ComfyUI 的 birefnet 出 alpha → PIL 合成纯白底
    find_reference(query)     串起来：搜→下→抠→白底，返回第一个可用的

为什么抠图走 ComfyUI：birefnet 权重本机已装（models/background_removal/birefnet.safetensors），
不引新依赖、不额外下模型。合成白底留在本地用 PIL 做，方便控制留白与尺寸。

水印说明：本模块**不做**水印擦除（那是 inpaint 的活）。策略是先把候选图拿给用户看，
5 秒内没提修改才自动往下走 —— 有水印的图用户能当场否掉。后续可接 ComfyUI inpaint 做局部擦除。
"""
from __future__ import annotations

import html
import io
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
BING = "https://www.bing.com/images/search"
UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")}
# 挡掉图标/表情/过小的图；上限避免下到几千万像素的巨图
MIN_PIXELS = 220 * 220
MAX_PIXELS = 4096 * 4096
# Bing 结果里每条：&quot;t&quot;:&quot;标题&quot; ... &quot;murl&quot;:&quot;直链&quot;（只用来建"直链→标题"映射）
TITLE_PAIR = re.compile(r'&quot;murl&quot;:&quot;(?P<u>https?://[^&]+)&quot;'
                        r'[^}]{0,200}?&quot;t&quot;:&quot;(?P<t>[^&]*)&quot;', re.S)


def search_images(query: str, limit: int = 8, timeout: int = 20) -> list[dict]:
    """必应图片搜索 → [{url, title, page}]。无需 API key。

    必须带上 title/page：只取 `murl` 直链会混进无关内容（实测搜"熊大 玩偶 白底"
    拿回来一堆手账素材 ✗）—— 有了标题就能按关键词过滤相关性。
    """
    q = urllib.parse.quote(query)
    url = f"{BING}?q={q}&qft=+filterui:photo-photo&form=IRFLTR&setlang=zh-CN&cc=CN"
    try:
        raw = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout)
        body = raw.read().decode("utf-8", "ignore")
    except Exception as e:
        print(f"[web_ref] 图片搜索失败: {e!r}")
        return []
    # 标题与直链分开抓，别写成"成对匹配"：Bing 的字段顺序/转义不稳，成对正则一旦只中一条，
    # 后面就再也不会补（我第一版就踩了：35 条直链只拿到 1 条）。
    titles = {m.group("u"): m.group("t") for m in TITLE_PAIR.finditer(body)}
    out, seen = [], set()
    for u in re.findall(r'&quot;murl&quot;:&quot;(https?://[^&]+)&quot;', body):
        base = u.split("?")[0]
        if base in seen:
            continue
        seen.add(base)
        out.append({"url": u, "title": titles.get(u, ""), "page": ""})
        if len(out) >= limit * 3:          # 多取一些，后面按相关性筛
            break
    print(f"[web_ref] 搜索「{query}」→ {len(out)} 条候选（{sum(1 for i in out if i['title'])} 条带标题）")
    return out


def rank_by_relevance(items: list[dict], query: str) -> list[dict]:
    """按"标题/来源页是否含查询词"排序。

    查询里的空格分词后逐个命中加分（"熊大"这种专有名词命中权重最高）。
    一条都不命中时原样返回 —— 有可能只是标题没写全，不该直接放弃。
    """
    words = [w for w in re.split(r"\s+", query.strip()) if len(w) >= 2]
    if not words:
        return items

    def score(it: dict) -> int:
        hay = f"{it.get('title', '')} {it.get('page', '')}"
        return sum(len(w) for w in words if w in hay)

    scored = sorted(items, key=score, reverse=True)
    hit = [it for it in scored if score(it) > 0]
    if hit:
        print(f"[web_ref] 相关性过滤：{len(hit)}/{len(items)} 条命中查询词")
        return hit
    print("[web_ref] 标题里没命中查询词，保留全部候选（可能是标题没写全）")
    return scored


def fetch_image(url: str, timeout: int = 25) -> Optional[Image.Image]:
    """下载并校验：太小/太大/非图片/损坏 → None（宁可跳过换下一张）"""
    try:
        req = urllib.request.Request(url, headers={**UA, "Referer": "https://www.bing.com/"})
        data = urllib.request.urlopen(req, timeout=timeout).read(30 * 1024 * 1024)
        img = Image.open(io.BytesIO(data))
        img.load()
        img = img.convert("RGB")
    except Exception as e:
        print(f"[web_ref] 下载/解码失败 {url[:60]}: {type(e).__name__}")
        return None
    px = img.width * img.height
    if not (MIN_PIXELS <= px <= MAX_PIXELS):
        print(f"[web_ref] 尺寸不合适 {img.width}×{img.height} → 跳过")
        return None
    return img


def cutout_white_bg(img: Image.Image, comfy=None, pad_ratio: float = 0.08) -> Optional[Image.Image]:
    """抠主体 + 纯白底（走 ComfyUI 的 birefnet 拿 alpha，再本地合成）。"""
    try:
        from .comfy_client import ComfyUIClient
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        c = comfy or ComfyUIClient(cfg)
        stage = "image"
        uploaded = _upload_pil(c, img, stage)
        if not uploaded:
            return None
        wf = {
            "1": {"class_type": "LoadBackgroundRemovalModel",
                  "inputs": {"bg_removal_name": "birefnet.safetensors"}},
            "2": {"class_type": "LoadImage", "inputs": {"image": uploaded}},
            "3": {"class_type": "RemoveBackground",
                  "inputs": {"bg_removal_model": ["1", 0], "image": ["2", 0]}},
            # birefnet 的 RemoveBackground 输出就是前景 mask（1=主体）→ 直接当 alpha
            "4": {"class_type": "JoinImageWithAlpha",
                  "inputs": {"image": ["2", 0], "alpha": ["3", 0]}},
            "5": {"class_type": "SaveImage",
                  "inputs": {"images": ["4", 0], "filename_prefix": "ref/subject"}},
        }
        pid = c.submit_workflow({"prompt": wf, "negative_prompt": ""}, stage=stage)
        res = c.wait_for_completion(pid, timeout=180)
        png = _grab_saved_image(c, res, "ref")
        if not png:
            print("[web_ref] 抠图没拿到输出图")
            return None
        cut = _read_image_any(png)
        return _to_white_bg(cut, pad_ratio) if cut is not None else None
    except Exception as e:
        print(f"[web_ref] 抠图异常: {e!r}")
        return None


def _upload_pil(c, img: Image.Image, stage: str) -> str:
    """客户端只认路径 → 先落盘再上传"""
    tmp = ROOT / "outputs" / "ref"
    tmp.mkdir(parents=True, exist_ok=True)
    p = tmp / f"ref_in_{int(time.time()*1000)}.png"
    img.save(p)
    return c.upload_image(str(p), stage=stage)


def _grab_saved_image(c, result: dict, subdir: str) -> Optional[Path]:
    """取本次 SaveImage 落盘的文件：先按返回的文件名找，再退回该子目录里最新的"""
    out_dir = Path(c.comfyui_dir) / "output"
    for node in ((result or {}).get("outputs") or {}).values():
        for im in (node.get("images") or []):
            cand = out_dir / (im.get("subfolder") or "") / im["filename"]
            if cand.exists():
                return cand
    d = out_dir / subdir
    files = sorted(d.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True) if d.exists() else []
    return files[0] if files else None


def _read_image_any(path: Path) -> Optional[Image.Image]:
    try:
        img = Image.open(path)
        img.load()
        return img.convert("RGBA")
    except Exception as e:
        print(f"[web_ref] 读取抠图结果失败: {e!r}")
        return None


def _to_white_bg(cut: Image.Image, pad_ratio: float = 0.08) -> Image.Image:
    """RGBA → 纯白底方图（裁到主体 + 四周留白），喂给建模更稳"""
    bbox = cut.getchannel("A").getbbox()
    if bbox:
        cut = cut.crop(bbox)
    side = int(max(cut.size) * (1 + 2 * pad_ratio))
    canvas = Image.new("RGB", (side, side), (255, 255, 255))
    canvas.paste(cut, ((side - cut.width) // 2, (side - cut.height) // 2), cut)
    return canvas


def find_reference(query: str, limit: int = 8, timeout: int = 25) -> Optional[dict]:
    """搜→筛相关性→下→抠→白底。返回 {"image","url","title","tried"} 或 None。"""
    urls = rank_by_relevance(search_images(query, limit=limit, timeout=timeout), query)
    for i, item in enumerate(urls):
        src = item["url"]
        img = fetch_image(src, timeout=timeout)
        if img is None:
            continue
        out = cutout_white_bg(img)
        if out is None:
            continue
        print(f"[web_ref] ✅ 参考图就绪：{src[:70]} → 白底 {out.width}×{out.height}")
        return {"image": out, "url": src, "title": item.get("title", ""), "tried": i + 1}
    print(f"[web_ref] 没找到可用参考图（试了 {len(urls)} 条）")
    return None


# 角色查询扩展表：用户输入中文 IP 角色名时，自动补尝试对应的英文/官方名
# —— Bing 对"熊大"会返回动物"熊"，对"Briar Bear"才返回 Boonie Bears 角色图。
# 这种多语映射是手动写的，覆盖常见 IP；不在表里时直接用原名。
CHARACTER_NAME_ALIASES = {
    # 熊出没
    "熊大": ["Briar Bear", "熊大 熊出没 角色"],
    "熊二": ["Bobo Bear", "熊二 熊出没"],
    "光头强": ["Logger Vick", "光头强 熊出没"],
    "吉吉": ["Briar Bear sidekick", "吉吉国王"],
    # 经典动画 IP（按需再加）
    "海绵宝宝": ["SpongeBob SquarePants character"],
    "派大星": ["Patrick Star"],
    "章鱼哥": ["Squidward Tentacles"],
    "小猪佩奇": ["Peppa Pig character"],
    "乔治": ["George Pig"],
    "汤姆": ["Tom Cat", "Tom and Jerry"],
    "杰瑞": ["Jerry Mouse", "Tom and Jerry"],
    # ── 漫威 / 超级英雄（2026-10-16 加）──────────────────────────────
    #
    # ★ 为什么必须加：宇凯报「我要钢铁侠，出图是小黄人」。
    #   实测根因 —— 这张表原来只有 11 项且**没有钢铁侠**，
    #   于是走 LLM 扩写那条路，而扩写模型把用户的长句
    #   （"漫威钢铁侠——穿金色红色机甲，胸口有弧形反应堆，名字叫托尼·斯塔克
    #     风格的零件，红色，机械风格"）**认成了 Minion / yellow** ✗
    #   同一句「钢铁侠」单独说就正确（Iron Man）—— 说明修饰语一多
    #   主体就被淹没。加进表里走确定性模板，不再赌 LLM 认不认得出 ✓
    "钢铁侠": ["Iron Man", "Marvel Iron Man character"],
    "托尼·斯塔克": ["Iron Man", "Tony Stark Marvel"],
    "托尼斯塔克": ["Iron Man", "Tony Stark Marvel"],
    "蜘蛛侠": ["Spider-Man", "Marvel Spider-Man character"],
    "美国队长": ["Captain America", "Marvel Captain America"],
    "雷神": ["Thor", "Marvel Thor character"],
    "绿巨人": ["Hulk", "Marvel Hulk character"],
    "黑寡妇": ["Black Widow", "Marvel Black Widow"],
    "蝙蝠侠": ["Batman", "DC Batman character"],
    "超人": ["Superman", "DC Superman character"],
    "小黄人": ["Minion", "Despicable Me Minion character"],
    # ── 日本动漫（高频）────────────────────────────────────────────
    "哆啦A梦": ["Doraemon", "Doraemon character"],
    "哆啦a梦": ["Doraemon", "Doraemon character"],
    "皮卡丘": ["Pikachu", "Pokemon Pikachu"],
    "马里奥": ["Mario", "Super Mario character"],
    "奥特曼": ["Ultraman", "Ultraman character"],
    "柯南": ["Conan Edogawa", "Detective Conan"],
}


def _expand_query(q: str) -> list[str]:
    """把用户输入的角色名扩成多个查询词：原名 + 英文别名 + 表里的"X 角色"模板。
    优先英文（Bing 索引里英文 IP 角色页最准）。"""
    q = q.strip()
    out = [q]
    if q in CHARACTER_NAME_ALIASES:
        out.extend(CHARACTER_NAME_ALIASES[q])
    else:
        # 启发式：加 "角色" 后缀，帮助 Bing 偏向人物页
        out.append(f"{q} 角色 设定")
    # 去重保序
    seen = set(); uniq = []
    for x in out:
        if x and x not in seen:
            seen.add(x); uniq.append(x)
    return uniq


def find_character_image(query: str, limit: int = 8, timeout: int = 25) -> Optional[dict]:
    """面向 IP 角色的联网找图：扩成多个查询词逐一尝试，命中即返回。

    返回的是 Image/PIL 对象 dict（与 find_reference 同样形态）。"""
    for q in _expand_query(query):
        print(f"[web_ref] 试查询: {q!r}")
        ref = find_reference(q, limit=limit, timeout=timeout)
        if ref:
            return ref
    return None


if __name__ == "__main__":       # 手动试：python -m backend.web_ref 熊大
    import sys
    q = " ".join(sys.argv[1:]) or "熊大"
    r = find_character_image(q)
    if r:
        out = ROOT / "outputs" / "ref" / "manual_ref.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        r["image"].save(out)
        print("已保存:", out)
    else:
        print("未找到")
