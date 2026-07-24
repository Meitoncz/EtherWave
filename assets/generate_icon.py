"""
Generates the EtherWave icon assets (a simple sound-wave mark) via Pillow:

- icon.png: the full-color app/window/dock icon (512x512, gradient
  background), used everywhere except the system tray.
- icon_tray_white.png / icon_tray_black.png: flat, transparent-background
  monochrome versions of just the wave mark (no background shape), sized
  for tray/menu-bar use, since tray icons are conventionally simple
  silhouettes that adapt to the OS's light/dark tray theme rather than a
  fixed-color badge.

Re-run this script to regenerate assets/ if the design ever changes;
nothing else in the project depends on Pillow at runtime, only here.
"""

from pathlib import Path
from PIL import Image, ImageDraw

SIZE = 512
TRAY_SIZE = 256
ASSETS_DIR = Path(__file__).parent

BG_TOP = (14, 116, 144)      # teal-800
BG_BOTTOM = (8, 47, 73)      # teal-950
WAVE_COLOR = (226, 232, 240)  # slate-200

# Symmetric bar heights (fraction of canvas height), shared by every variant.
BAR_HEIGHTS_FRAC = [0.18, 0.34, 0.55, 0.72, 0.55, 0.34, 0.18]


def _draw_wave_bars(draw: ImageDraw.ImageDraw, size: int, fill):
    n_bars = len(BAR_HEIGHTS_FRAC)
    bar_width = int(size * 0.055)
    gap = int(size * 0.035)
    total_width = n_bars * bar_width + (n_bars - 1) * gap
    start_x = (size - total_width) // 2
    center_y = size // 2

    for i, hf in enumerate(BAR_HEIGHTS_FRAC):
        bar_h = int(size * hf)
        x0 = start_x + i * (bar_width + gap)
        x1 = x0 + bar_width
        y0 = center_y - bar_h // 2
        y1 = center_y + bar_h // 2
        draw.rounded_rectangle([x0, y0, x1, y1], radius=bar_width // 2, fill=fill)


def generate_app_icon() -> Image.Image:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Rounded-square background with a vertical gradient.
    margin = int(SIZE * 0.04)
    radius = int(SIZE * 0.22)
    bg = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    bg_draw = ImageDraw.Draw(bg)
    for y in range(SIZE):
        t = y / SIZE
        r = int(BG_TOP[0] * (1 - t) + BG_BOTTOM[0] * t)
        g = int(BG_TOP[1] * (1 - t) + BG_BOTTOM[1] * t)
        b = int(BG_TOP[2] * (1 - t) + BG_BOTTOM[2] * t)
        bg_draw.line([(0, y), (SIZE, y)], fill=(r, g, b, 255))

    mask = Image.new("L", (SIZE, SIZE), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle(
        [margin, margin, SIZE - margin, SIZE - margin], radius=radius, fill=255
    )
    img.paste(bg, (0, 0), mask)

    _draw_wave_bars(draw, SIZE, WAVE_COLOR)
    return img


def generate_tray_icon(color) -> Image.Image:
    img = Image.new("RGBA", (TRAY_SIZE, TRAY_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    _draw_wave_bars(draw, TRAY_SIZE, color)
    return img


if __name__ == "__main__":
    generate_app_icon().save(ASSETS_DIR / "icon.png")
    print(f"wrote {ASSETS_DIR / 'icon.png'}")

    generate_tray_icon((255, 255, 255, 255)).save(ASSETS_DIR / "icon_tray_white.png")
    print(f"wrote {ASSETS_DIR / 'icon_tray_white.png'}")

    generate_tray_icon((0, 0, 0, 255)).save(ASSETS_DIR / "icon_tray_black.png")
    print(f"wrote {ASSETS_DIR / 'icon_tray_black.png'}")
