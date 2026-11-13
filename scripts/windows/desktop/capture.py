"""Screenshots with Pillow (physical pixels; win32.set_dpi_aware() must run first)."""
from pathlib import Path


def grab(box, path):
    """Save the screen rectangle (l, t, r, b) as PNG; returns the path."""
    from PIL import ImageGrab
    image = ImageGrab.grab(bbox=tuple(box), all_screens=True)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return path


def window(hwnd, path):
    import win32
    return grab(win32.window_rect(hwnd), path)


def is_blank(path, tolerance=4):
    """True when every channel varies by at most `tolerance` (a solid colour or black frame)."""
    from PIL import Image
    with Image.open(path) as image:
        extrema = image.convert("RGB").getextrema()
    return all(high - low <= tolerance for low, high in extrema)


def luminance(path, box=None):
    """Mean luminance 0-255 of the image (or of `box` within it)."""
    from PIL import Image, ImageStat
    with Image.open(path) as image:
        region = image.convert("L") if box is None else image.convert("L").crop(box)
        return ImageStat.Stat(region).mean[0]
