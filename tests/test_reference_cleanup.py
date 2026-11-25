from PIL import Image, ImageDraw
import numpy as np
from scipy.ndimage import label

from backend.preprocess import prepare


def test_reference_cleanup_removes_small_detached_fragments(tmp_path):
    image = Image.new("RGBA", (128, 96))
    draw = ImageDraw.Draw(image)
    draw.rectangle((24, 20, 90, 72), fill="red")
    draw.rectangle((114, 83, 116, 85), fill="black")
    source = tmp_path / "upload.png"
    image.save(source)

    result = prepare(str(source), size=128, clean_fragments=True,
                     require_foreground=True, do_enhance=False, do_sharpen=False)

    assert result["ok"]
    with Image.open(result["path"]) as cleaned:
        alpha = np.asarray(cleaned.getchannel("A"))
        assert cleaned.size == (128, 128)
        assert label(alpha > 16)[1] == 1
