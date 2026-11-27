"""Build the deliverable images and videos for the motion direction."""
import os, sys, subprocess, shutil
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from PIL import Image, ImageDraw, ImageFont
from sheet import make_sheet

BASE = r"D:\ForgeForm-oss\work\research\motion"
OLD = os.path.join(BASE, "render_old")
NEW = os.path.join(BASE, "render_new")
FF = r"D:\miniconda3\Library\bin\ffmpeg.exe"
N = 24


def font(sz, bold=False):
    for p in (r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc",
              r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\simhei.ttf"):
        try:
            return ImageFont.truetype(p, sz)
        except Exception:
            pass
    return ImageFont.load_default()


def fpath(d, i):
    return os.path.join(d, "f%02d.png" % i)


# ---------------------------------------------------------------- 4-up sheets
FR = [1, 7, 13, 19]
make_sheet([fpath(NEW, f) for f in FR],
           os.path.join(BASE, "步态_新_侧视四联.png"),
           labels=["第 %d 帧" % f for f in FR], cols=4,
           title="BVH 重定向步态（CMU 02_01，24 帧循环）侧视四联")

make_sheet([fpath(OLD, f) for f in FR] + [fpath(NEW, f) for f in FR],
           os.path.join(BASE, "步态_对比_旧vs新_四联.png"),
           labels=["旧 f%d" % f for f in FR] + ["新 f%d" % f for f in FR], cols=4,
           title="上排：旧 IK 程序化步态    下排：BVH 重定向步态    同一相机 / 同一地面线")

# -------------------------------------------------------- side-by-side frames
CMP = os.path.join(BASE, "cmp_frames")
os.makedirs(CMP, exist_ok=True)
fw, fh = Image.open(fpath(NEW, 1)).size
for i in range(1, N + 1):
    a = Image.open(fpath(OLD, i)).convert("RGB")
    b = Image.open(fpath(NEW, i)).convert("RGB")
    canvas = Image.new("RGB", (fw * 2 + 12, fh + 34), (22, 24, 28))
    canvas.paste(a, (0, 34))
    canvas.paste(b, (fw + 12, 34))
    d = ImageDraw.Draw(canvas)
    d.text((8, 8), "旧 IK 步态", fill=(230, 120, 110), font=font(19, True))
    d.text((fw + 20, 8), "新 BVH 重定向（真 mocap）", fill=(120, 210, 150), font=font(19, True))
    d.text((fw - 74, 40), "%d/24" % i, fill=(200, 200, 200), font=font(15))
    d.text((fw * 2 - 62, 40), "%d/24" % i, fill=(200, 200, 200), font=font(15))
    canvas.save(os.path.join(CMP, "c%02d.png" % i))
print("built %d comparison frames" % N)


def encode(pattern, out, fps=24, loops=1):
    vf = "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p"
    cmd = [FF, "-y", "-framerate", str(fps), "-stream_loop", str(loops - 1),
           "-i", pattern, "-c:v", "libx264", "-preset", "medium", "-crf", "18",
           "-pix_fmt", "yuv420p", "-vf", vf, "-movflags", "+faststart", out]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    ok = os.path.exists(out) and os.path.getsize(out) > 2000
    print("%s %s (%s bytes)" % ("OK " if ok else "FAIL", out,
                                os.path.getsize(out) if os.path.exists(out) else 0))
    if not ok:
        print(r.stderr[-1500:])
    return ok


encode(os.path.join(NEW, "f%02d.png"), os.path.join(BASE, "步态_新_24帧循环.mp4"), 24, 3)
encode(os.path.join(CMP, "c%02d.png"), os.path.join(BASE, "步态_对比_旧vs新.mp4"), 24, 3)
encode(os.path.join(OLD, "f%02d.png"), os.path.join(BASE, "步态_旧_24帧循环.mp4"), 24, 3)
print("DONE_BUILD")
