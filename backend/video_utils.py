"""Video utilities — extract frames, probe info."""
import subprocess, tempfile, json
from pathlib import Path

import PIL.Image


def extract_middle_frame(video_path: str) -> Path:
    """Extract the middle frame of a video as PNG."""
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", video_path],
        capture_output=True, text=True,
    )
    duration = float(r.stdout.strip() or "5.0")
    mid = duration / 2

    out = Path(tempfile.mktemp(suffix=".png"))
    subprocess.run(
        ["ffmpeg", "-y", "-ss", str(mid), "-i", video_path,
         "-frames:v", "1", str(out)],
        capture_output=True, timeout=30,
    )
    if out.exists() and out.stat().st_size > 0:
        return out
    # Fallback: first frame
    subprocess.run(
        ["ffmpeg", "-y", "-i", video_path, "-frames:v", "1", str(out)],
        capture_output=True, timeout=30,
    )
    if not out.exists() or out.stat().st_size == 0:
        raise RuntimeError("Failed to extract frame from video")
    return out


def get_video_info(video_path: str) -> dict:
    """Get video duration and resolution."""
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_format", "-show_streams", video_path],
        capture_output=True, text=True,
    )
    data = json.loads(r.stdout)
    duration = float(data.get("format", {}).get("duration", 0))
    w = h = 0
    for s in data.get("streams", []):
        if s.get("codec_type") == "video":
            w = int(s.get("width", 0))
            h = int(s.get("height", 0))
            break
    return {"duration": round(duration, 1), "width": w, "height": h}
