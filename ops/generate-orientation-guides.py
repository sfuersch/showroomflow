#!/usr/bin/env python3
"""Create calibrated, line-only camera guides from the orientation artwork."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter


CANVAS_SIZE = (640, 480)
SOURCE_DIR = (
    Path(__file__).resolve().parents[1]
    / "backend/app/static/orientation-silhouettes"
)
OUTPUT_DIR = (
    Path(__file__).resolve().parents[1]
    / "backend/app/static/orientation-guides"
)

EXTERIOR_KEYS = {
    "front",
    "front-left",
    "front-lower-left",
    "left",
    "rear-left",
    "rear",
    "rear-right",
    "right",
    "front-right",
    "front-lower-right",
    "driver-door-open",
    "passenger-door-open",
}
FRONTAL_KEYS = {"front", "rear"}
SIDE_KEYS = {"left", "right"}
LOWER_KEYS = {"front-lower-left", "front-lower-right"}
WHEEL_KEYS = {
    "wheel-front-left",
    "wheel-front-right",
    "wheel-rear-left",
    "wheel-rear-right",
}
NEUTRAL_KEYS = {"damage", "special"}


@dataclass(frozen=True)
class TargetBox:
    left: int
    top: int
    right: int
    bottom: int
    align_bottom: bool = False

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


def target_box(key: str) -> TargetBox:
    if key in FRONTAL_KEYS:
        return TargetBox(88, 92, 552, 390, align_bottom=True)
    if key in SIDE_KEYS:
        return TargetBox(48, 142, 592, 390, align_bottom=True)
    if key in LOWER_KEYS:
        return TargetBox(46, 62, 594, 404, align_bottom=True)
    if key in EXTERIOR_KEYS:
        return TargetBox(56, 74, 584, 394, align_bottom=True)
    if key in WHEEL_KEYS:
        return TargetBox(142, 70, 498, 410, align_bottom=True)
    if key in {"tire-tread", "key", "odometer", "infotainment", "instruments"}:
        return TargetBox(142, 72, 498, 400)
    if key in {"steering-wheel", "center-console", "engine-bay"}:
        return TargetBox(112, 56, 528, 410)
    if key in {"panoramic-roof", "windshield", "front-interior"}:
        return TargetBox(78, 54, 562, 412)
    return TargetBox(92, 58, 548, 410)


def source_line_mask(image: Image.Image) -> Image.Image:
    rgba = image.convert("RGBA")
    red, green, blue, alpha = rgba.split()
    brightness = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    bright_lines = brightness.point(lambda value: 255 if value >= 170 else 0)
    mask = ImageChops.multiply(bright_lines, alpha)
    return mask.filter(ImageFilter.GaussianBlur(0.35))


def fit_mask(mask: Image.Image, box: TargetBox) -> tuple[Image.Image, tuple[int, int]]:
    bbox = mask.getbbox()
    if not bbox:
        raise ValueError("The source artwork contains no visible line work")
    cropped = mask.crop(bbox)
    scale = min(box.width / cropped.width, box.height / cropped.height)
    size = (
        max(1, round(cropped.width * scale)),
        max(1, round(cropped.height * scale)),
    )
    resized = cropped.resize(size, Image.Resampling.LANCZOS)
    x = box.left + (box.width - size[0]) // 2
    if box.align_bottom:
        y = box.bottom - size[1]
    else:
        y = box.top + (box.height - size[1]) // 2
    return resized, (x, y)


def dashed_line(
    draw: ImageDraw.ImageDraw,
    start: tuple[int, int],
    end: tuple[int, int],
    *,
    fill: tuple[int, int, int, int],
    width: int,
    dash: int = 13,
    gap: int = 10,
) -> None:
    x1, y1 = start
    x2, y2 = end
    length = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
    if length == 0:
        return
    dx = (x2 - x1) / length
    dy = (y2 - y1) / length
    position = 0.0
    while position < length:
        segment_end = min(position + dash, length)
        draw.line(
            (
                x1 + dx * position,
                y1 + dy * position,
                x1 + dx * segment_end,
                y1 + dy * segment_end,
            ),
            fill=fill,
            width=width,
        )
        position += dash + gap


def draw_corner_frame(draw: ImageDraw.ImageDraw) -> None:
    color = (255, 255, 255, 105)
    dark = (0, 0, 0, 95)
    left, top, right, bottom = 28, 24, 612, 448
    arm = 42
    segments = [
        ((left, top + arm), (left, top), (left + arm, top)),
        ((right - arm, top), (right, top), (right, top + arm)),
        ((left, bottom - arm), (left, bottom), (left + arm, bottom)),
        ((right - arm, bottom), (right, bottom), (right, bottom - arm)),
    ]
    for points in segments:
        draw.line(points, fill=dark, width=7, joint="curve")
        draw.line(points, fill=color, width=3, joint="curve")


def draw_center_ticks(draw: ImageDraw.ImageDraw) -> None:
    dark = (0, 0, 0, 95)
    light = (255, 255, 255, 95)
    for start, end in [
        ((320, 24), (320, 47)),
        ((320, 425), (320, 448)),
        ((28, 236), (51, 236)),
        ((589, 236), (612, 236)),
    ]:
        draw.line((start, end), fill=dark, width=6)
        draw.line((start, end), fill=light, width=2)


def render_neutral_guide() -> Image.Image:
    canvas = Image.new("RGBA", CANVAS_SIZE, (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    draw_corner_frame(draw)
    draw_center_ticks(draw)
    draw.ellipse((270, 190, 370, 290), outline=(0, 0, 0, 105), width=7)
    draw.ellipse((270, 190, 370, 290), outline=(255, 255, 255, 125), width=3)
    draw.line((294, 240, 346, 240), fill=(255, 255, 255, 120), width=2)
    draw.line((320, 214, 320, 266), fill=(255, 255, 255, 120), width=2)
    return canvas


def render_guide(path: Path) -> Image.Image:
    key = path.stem
    if key in NEUTRAL_KEYS:
        return render_neutral_guide()

    source = Image.open(path)
    fitted, position = fit_mask(source_line_mask(source), target_box(key))

    canvas = Image.new("RGBA", CANVAS_SIZE, (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    draw_corner_frame(draw)
    draw_center_ticks(draw)

    if key in EXTERIOR_KEYS:
        dashed_line(
            draw,
            (52, 400),
            (588, 400),
            fill=(0, 0, 0, 100),
            width=7,
        )
        dashed_line(
            draw,
            (52, 400),
            (588, 400),
            fill=(255, 255, 255, 125),
            width=3,
        )

    line_alpha = Image.new("L", CANVAS_SIZE, 0)
    line_alpha.paste(fitted, position)

    dark_alpha = line_alpha.filter(ImageFilter.MaxFilter(7)).point(
        lambda value: round(value * 0.65)
    )
    dark_layer = Image.new("RGBA", CANVAS_SIZE, (0, 0, 0, 0))
    dark_layer.putalpha(dark_alpha)
    canvas.alpha_composite(dark_layer)

    white_alpha = line_alpha.point(lambda value: round(value * 0.92))
    white_layer = Image.new("RGBA", CANVAS_SIZE, (255, 255, 255, 0))
    white_layer.putalpha(white_alpha)
    canvas.alpha_composite(white_layer)
    return canvas


def main() -> None:
    paths = sorted(SOURCE_DIR.glob("*.png"))
    if not paths:
        raise SystemExit(f"No orientation artwork found in {SOURCE_DIR}")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for path in paths:
        rendered = render_guide(path)
        output_path = OUTPUT_DIR / path.name
        rendered.save(output_path, optimize=True)
        print(f"generated {output_path.relative_to(OUTPUT_DIR.parent)}")


if __name__ == "__main__":
    main()
