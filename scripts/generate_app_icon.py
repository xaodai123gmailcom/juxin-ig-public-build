r"""Rebuild the wolf SVG, PNG and Windows ICO using the existing Pillow dependency.

Run from the project root: .venv\Scripts\python.exe scripts/generate_app_icon.py
The generated assets are committed; normal builds do not need to regenerate them.
"""

from io import BytesIO
from pathlib import Path
import struct

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
SIZE = 1024
SCALE = SIZE / 64
ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)
# The existing app's geometric wolf, enlarged and without lettering so its
# silhouette remains distinct at Windows taskbar and desktop shortcut sizes.
SHAPES = (
    ("fur", ((12, 8), (28, 19), (36, 19), (52, 8), (50, 34), (41, 48), (32, 55), (23, 48), (14, 34))),
    ("#263c59", ((16, 15), (27, 26), (17, 30))),
    ("#263c59", ((48, 15), (37, 26), (47, 30))),
    ("#182e49", ((16, 32), (29, 37), (23, 42))),
    ("#182e49", ((48, 32), (35, 37), (41, 42))),
    ("#69e2ff", ((20, 34), (27, 37), (23, 39))),
    ("#69e2ff", ((44, 34), (37, 37), (41, 39))),
    ("#eaf4ff", ((32, 31), (25, 45), (32, 52), (39, 45))),
    ("#152a42", ((26, 44), (32, 46), (38, 44), (32, 50))),
)


def gradient(start, end, top=0, bottom=64):
    colors = [tuple(bytes.fromhex(value.lstrip("#"))) for value in (start, end)]
    strip = Image.new("RGBA", (1, SIZE))
    strip.putdata([
        tuple(round(a + (b - a) * max(0, min(1, (y / SCALE - top) / (bottom - top))))
              for a, b in zip(*colors)) + (255,)
        for y in range(SIZE)
    ])
    return strip.resize((SIZE, SIZE))


def svg_source():
    polygons = "\n".join(
        f'  <polygon points="{" ".join(f"{x},{y}" for x, y in points)}" fill="{("url(#fur)" if fill == "fur" else fill)}"/>'
        for fill, points in SHAPES
    )
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" fill="none">
  <defs>
    <linearGradient id="base" x1="0" y1="0" x2="0" y2="64" gradientUnits="userSpaceOnUse"><stop stop-color="#223d60"/><stop offset="1" stop-color="#101827"/></linearGradient>
    <linearGradient id="fur" x1="0" y1="8" x2="0" y2="55" gradientUnits="userSpaceOnUse"><stop stop-color="#f0f6ff"/><stop offset="1" stop-color="#91b0cf"/></linearGradient>
  </defs>
  <rect x="1" y="1" width="62" height="62" rx="16" fill="url(#base)" stroke="#528fba" stroke-width="0.8"/>
{polygons}
</svg>
'''


def render():
    canvas = Image.new("RGBA", (SIZE, SIZE))
    mask = Image.new("L", canvas.size)
    ImageDraw.Draw(mask).rounded_rectangle(tuple(round(v * SCALE) for v in (1, 1, 63, 63)), radius=round(16 * SCALE), fill=255)
    canvas.paste(gradient("#223d60", "#101827"), (0, 0), mask)
    ImageDraw.Draw(canvas).rounded_rectangle(tuple(round(v * SCALE) for v in (1, 1, 63, 63)), radius=round(16 * SCALE), outline="#528fba", width=round(0.8 * SCALE))
    for fill, points in SHAPES:
        coordinates = [(round(x * SCALE), round(y * SCALE)) for x, y in points]
        if fill == "fur":
            mask = Image.new("L", canvas.size)
            ImageDraw.Draw(mask).polygon(coordinates, fill=255)
            canvas.paste(gradient("#f0f6ff", "#91b0cf", 8, 55), (0, 0), mask)
        else:
            ImageDraw.Draw(canvas).polygon(coordinates, fill=fill)
    return canvas


def write_ico(path, source):
    # Explicit PNG-backed frames retain alpha and provide native sizes at
    # 100%, 125%, 150% and 200% Windows display scaling.
    frames = []
    for size in ICON_SIZES:
        stream = BytesIO()
        source.resize((size, size), Image.Resampling.LANCZOS).save(stream, "PNG")
        frames.append((size, stream.getvalue()))
    offset = 6 + 16 * len(frames)
    entries = []
    for size, content in frames:
        entries.append(struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(content), offset))
        offset += len(content)
    path.write_bytes(struct.pack("<HHH", 0, 1, len(frames)) + b"".join(entries) + b"".join(content for _, content in frames))


def main():
    assets = ROOT / "desktop" / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    svg = svg_source()
    (assets / "war-wolf.svg").write_text(svg, encoding="utf-8")
    (ROOT / "renderer" / "public" / "war-wolf.svg").write_text(svg, encoding="utf-8")
    source = render()
    source.resize((512, 512), Image.Resampling.LANCZOS).save(assets / "war-wolf.png")
    write_ico(assets / "war-wolf.ico", source)
    print("Wolf assets rebuilt: SVG, 512px PNG, ICO " + ", ".join(map(str, ICON_SIZES)))


if __name__ == "__main__":
    main()
