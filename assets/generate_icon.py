"""
Generates the EtherWave placeholder icon (a simple sound-wave mark) as a
512x512 PNG via Pillow. Re-run this script to regenerate assets/icon.png if
the design ever changes; nothing else in the project depends on Pillow at
runtime, only at icon-generation time.
"""

from pathlib import Path
from PIL import Image, ImageDraw

SIZE = 512
OUT_PATH = Path(__file__).parent / "icon.png"

BG_TOP = (14, 116, 144)      # teal-800
BG_BOTTOM = (8, 47, 73)      # teal-950
WAVE_COLOR = (226, 232, 240)  # slate-200


def generate() -> Image.Image:
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

    # Sound-wave bars: symmetric, varying heights, rounded caps.
    n_bars = 7
    bar_width = int(SIZE * 0.055)
    gap = int(SIZE * 0.035)
    total_width = n_bars * bar_width + (n_bars - 1) * gap
    start_x = (SIZE - total_width) // 2
    center_y = SIZE // 2

    heights_frac = [0.18, 0.34, 0.55, 0.72, 0.55, 0.34, 0.18]
    for i, hf in enumerate(heights_frac):
        bar_h = int(SIZE * hf)
        x0 = start_x + i * (bar_width + gap)
        x1 = x0 + bar_width
        y0 = center_y - bar_h // 2
        y1 = center_y + bar_h // 2
        draw.rounded_rectangle([x0, y0, x1, y1], radius=bar_width // 2, fill=WAVE_COLOR)

    return img


if __name__ == "__main__":
    generate().save(OUT_PATH)
    print(f"wrote {OUT_PATH}")
