"""Derive the integration's brand images from the project logo.

Since Home Assistant 2026.3 a custom integration carries its own brand images:
``custom_components/<domain>/brand/`` with ``icon.png`` (256x256) and
``icon@2x.png`` (512x512) as the required pair, plus the optional ``logo.png`` /
``logo@2x.png`` lockup. The HACS validation checks for that same folder and only
falls back to ``brands.home-assistant.io`` when it is missing, and
``home-assistant/brands`` no longer accepts new custom integrations - it closes
those pull requests automatically (``.github/workflows/close-new-custom-integrations.yml``
there) - so these files are the only way to give the integration an icon.

``.github/logo.png`` is the 512x512 marketing image the README shows: the
power-button-with-heart glyph above the "Eufy API" wordmark, drawn on a white
plate. This tool removes that plate, splits the artwork into the glyph and the
wordmark, and writes the four files the documentation asks for, with the artwork
transparent so it reads on a light and on a dark theme alike. Dark variants are
deliberately not generated: the artwork is a vivid teal and pink that needs no
separate dark-theme version, and Home Assistant serves the plain file when a
``dark_*`` one is absent.

The images are committed, so this only needs to be run when the logo changes::

    pip install -r scripts/requirements-dev.txt
    python scripts/make_brand_assets.py

Usage::

    python scripts/make_brand_assets.py [--check]
"""

from __future__ import annotations

import argparse
import colorsys
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / ".github" / "logo.png"
BRAND_DIR = ROOT / "custom_components" / "eufylife_api" / "brand"

# Home Assistant's sizes: an icon's shortest side is 256px (512px for @2x) and a
# logo canvas is 2:1 - so 256x256/512x512 and 512x256/1024x512 here.
ICON_SIZE = 256
ICON_SCALE = 2
LOGO_SIZE = (512, 256)
LOGO_SCALE = 2

# How much of the icon canvas the glyph spans, the logo canvas' margin, and the
# space between the glyph and the wordmark in the logo.
ICON_FILL = 0.86
LOGO_PADDING = 44
LOGO_GAP = 48

# A pixel is artwork once it is this far from white.
INK_THRESHOLD = 0.02

# A pixel counts as artwork from this alpha up. Fainter pixels are kept only when
# they sit within this many pixels of the artwork, which keeps the artwork's own
# anti-aliased edge while the plate's soft shadow is dropped.
HAZE_ALPHA = 150
HAZE_RADIUS = 2

# The logo's own tones, used to decide how opaque a pixel was before the white
# plate behind it was removed. The gradients a tint belongs to are listed first,
# so a mid-gradient pixel is measured against its own end of the gradient.
TEAL = (
    (20, 224, 208),  # ring teal
    (10, 190, 186),  # ring teal, dark end of the gradient
)
PINK = (
    (239, 92, 121),  # wordmark pink
    (236, 100, 130),  # heart pink, dark end of the gradient
    (247, 138, 160),  # heart pink, light end of the gradient
)
PALETTE = TEAL + PINK

# A colour belongs to the ring or to the pink from this much difference between
# its strongest and its weakest channel up; a fainter difference has no hue of its
# own and is matched to the closest tone instead.
MIN_SATURATION = 40

# Hue, in degrees, from which up to the pink's own range a colour is the teal: the
# logo's teal sits around 175 and its pink around 348.
TEAL_HUE = (120, 240)

# Rows without an inked pixel break the pink tones into bands - the heart and the
# wordmark - from this many rows apart.
BAND_GAP = 12


def _ink(pixel: tuple[int, int, int]) -> float:
    """Return how far a colour is from white: 0.0 for white, 1.0 for black."""
    return max(255 - channel for channel in pixel) / 255


def _distance(colour: tuple[int, int, int], rgb: tuple[int, int, int]) -> float:
    """Return how far ``rgb`` is from the palette tone ``colour``."""
    return sum((a - b) ** 2 for a, b in zip(colour, rgb)) ** 0.5


def _hue(rgb: tuple[int, int, int]) -> float | None:
    """Return the hue of ``rgb`` in degrees, or None when it has no colour of its own."""
    if max(rgb) - min(rgb) < MIN_SATURATION:
        return None
    return colorsys.rgb_to_hsv(*(channel / 255 for channel in rgb))[0] * 360


def _tone(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    """Return the palette tone ``rgb`` is a tint of.

    The hue decides between the ring's teal and the logo's pink rather than the
    closest colour: a pale tint is much further from the saturated teal in RGB
    than it is from the pink, which would file the ring's halo with the wordmark.
    A colour without a hue of its own is matched to the closest tone of all.
    """
    hue = _hue(rgb)
    if hue is None:
        return min(PALETTE, key=lambda colour: _distance(colour, rgb))
    tones = TEAL if TEAL_HUE[0] <= hue < TEAL_HUE[1] else PINK
    return min(tones, key=lambda colour: _distance(colour, rgb))


def _unblend(pixel: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """Return ``pixel`` unblended from the white background it was drawn on.

    A pixel of the artwork on a white plate is ``alpha * tone + (1 - alpha) * white``.
    The palette tone the pixel is a tint of gives the alpha that recovers, which
    keeps the logo's gradients intact instead of flattening them. The alpha the
    source itself carries is kept as well: the plate's rounded corners are
    transparent there, and their colour channels are meaningless.
    """
    rgb, source_alpha = pixel[:3], pixel[3]
    ink = _ink(rgb)
    if source_alpha <= 20 or ink <= INK_THRESHOLD:
        return (0, 0, 0, 0)
    alpha = min(1.0, ink / _ink(_tone(rgb)))
    recovered = rgb
    if alpha < 0.999:
        recovered = tuple(
            max(0, min(255, round((channel - (1 - alpha) * 255) / alpha)))
            for channel in rgb
        )
    return (*recovered, round(alpha * source_alpha))


def brand_artwork(source: Image.Image) -> Image.Image:
    """Return the logo's artwork alone: plate removed, shadow dropped, opaque.

    Every pixel is first unblended from the white plate, which recovers the alpha
    it had there. That alpha is only trusted at the artwork's rim: everything
    opaque is taken from the source untouched, because a tint inside the artwork
    is its own gradient rather than a blend with the plate, and unblending it
    would draw seams across the logo. The plate's faint tint and its soft shadow
    are dropped by keeping fainter pixels only within ``HAZE_RADIUS`` pixels of
    the artwork.
    """
    plate = source.convert("RGBA")
    unblended = Image.new("RGBA", plate.size, (0, 0, 0, 0))
    pixels = plate.load()
    target = unblended.load()
    for y in range(plate.size[1]):
        for x in range(plate.size[0]):
            target[x, y] = _unblend(pixels[x, y])

    alpha = unblended.getchannel("A")
    artwork = alpha.point(lambda value: 255 if value >= HAZE_ALPHA else 0)
    nearby = artwork.filter(ImageFilter.MaxFilter(2 * HAZE_RADIUS + 1))
    rim = ImageChops.multiply(alpha, nearby)
    isolated = Image.composite(plate, unblended, artwork)
    isolated.putalpha(ImageChops.lighter(artwork, rim))
    return isolated


def _is_teal(rgb: tuple[int, int, int]) -> bool:
    """Return whether ``rgb`` is a tint of the ring's teal rather than of the pink."""
    return _tone(rgb) in TEAL


def _mask(size: tuple[int, int], points: list[tuple[int, int]]) -> Image.Image:
    """Return an 8-bit mask that is set at ``points``."""
    mask = Image.new("1", size, 0)
    mask_pixels = mask.load()
    for x, y in points:
        mask_pixels[x, y] = 1
    return mask.convert("L")


def _bands(rows: set[int]) -> list[tuple[int, int]]:
    """Return the ranges of consecutive rows in ``rows``, up to ``BAND_GAP`` apart."""
    bands: list[list[int]] = []
    for row in sorted(rows):
        if bands and row - bands[-1][1] <= BAND_GAP:
            bands[-1][1] = row
        else:
            bands.append([row, row])
    return [(band[0], band[1]) for band in bands]


def _extract(image: Image.Image, points: list[tuple[int, int]]) -> Image.Image:
    """Return ``image`` cropped to ``points``, with every other pixel cleared."""
    extracted = image.copy()
    extracted.putalpha(
        ImageChops.multiply(image.getchannel("A"), _mask(image.size, points))
    )
    return extracted.crop(
        (
            min(x for x, _ in points),
            min(y for _, y in points),
            max(x for x, _ in points) + 1,
            max(y for _, y in points) + 1,
        )
    )


def split_artwork(image: Image.Image) -> tuple[Image.Image, Image.Image]:
    """Return the glyph (the ring and the heart) and the wordmark separately.

    The two are told apart by their tones rather than by a row: the ring's bottom
    arc reaches down into the wordmark's first rows, so a horizontal cut would
    clip the ring. The lowest band of pink artwork is the wordmark, the teal
    artwork and the pink band above it (the heart) are the glyph. An
    anti-aliased edge follows the artwork it borders instead of the tone it looks
    like, because an edge that is almost white says nothing about which half of
    the logo it belongs to.
    """
    pixels = image.load()
    solid_teal: list[tuple[int, int]] = []
    solid_pink: list[tuple[int, int]] = []
    edges: list[tuple[int, int]] = []
    for y in range(image.size[1]):
        for x in range(image.size[0]):
            rgb, alpha = pixels[x, y][:3], pixels[x, y][3]
            if alpha <= 20:
                continue
            target = (
                edges
                if alpha < HAZE_ALPHA
                else (solid_teal if _is_teal(rgb) else solid_pink)
            )
            target.append((x, y))

    bands = _bands({y for _, y in solid_pink})
    if len(bands) < 2:
        raise SystemExit(
            f"the heart and the wordmark could not be told apart in {SOURCE}"
        )
    wordmark_top = bands[-1][0]
    heart = [point for point in solid_pink if point[1] < wordmark_top]
    letters = [point for point in solid_pink if point[1] >= wordmark_top]

    # An edge pixel joins the artwork it borders, which is what the dilations of
    # the three parts of the logo answer.
    dilate = ImageFilter.MaxFilter(2 * HAZE_RADIUS + 1)
    next_to_ring = _mask(image.size, solid_teal).filter(dilate)
    next_to_heart = _mask(image.size, heart).filter(dilate)
    next_to_letters = _mask(image.size, letters).filter(dilate)
    glyph = solid_teal + heart
    wordmark = list(letters)
    for x, y in edges:
        if next_to_ring.getpixel((x, y)) or next_to_heart.getpixel((x, y)):
            glyph.append((x, y))
        elif next_to_letters.getpixel((x, y)) or y >= wordmark_top:
            wordmark.append((x, y))
        else:
            glyph.append((x, y))

    return _extract(image, glyph), _extract(image, wordmark)


def fit(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Return ``image`` scaled to fit inside ``size`` without distortion."""
    scale = min(size[0] / image.size[0], size[1] / image.size[1])
    scaled = (
        max(1, round(image.size[0] * scale)),
        max(1, round(image.size[1] * scale)),
    )
    return image.resize(scaled, Image.LANCZOS)


def build_icon(glyph: Image.Image) -> Image.Image:
    """Return the @2x icon: the glyph centred on a transparent square."""
    canvas_size = ICON_SIZE * ICON_SCALE
    scaled = fit(glyph, (round(canvas_size * ICON_FILL),) * 2)
    canvas = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
    offset = ((canvas_size - scaled.size[0]) // 2, (canvas_size - scaled.size[1]) // 2)
    canvas.paste(scaled, offset, scaled)
    return canvas


def build_logo(glyph: Image.Image, wordmark: Image.Image) -> Image.Image:
    """Return the @2x logo: the glyph beside the wordmark on a 2:1 canvas."""
    width, height = (size * LOGO_SCALE for size in LOGO_SIZE)
    room = (width - 2 * LOGO_PADDING, height - 2 * LOGO_PADDING)
    glyph_scaled = fit(glyph, (room[0] // 2, room[1]))
    # The wordmark keeps the share of the glyph's height it has in the source
    # artwork, so the lockup keeps the proportions the logo was drawn with.
    ratio = wordmark.size[1] / glyph.size[1]
    wordmark_scaled = fit(
        wordmark,
        (
            room[0] - glyph_scaled.size[0] - LOGO_GAP,
            round(glyph_scaled.size[1] * ratio),
        ),
    )
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    left = (width - glyph_scaled.size[0] - LOGO_GAP - wordmark_scaled.size[0]) // 2
    canvas.paste(
        glyph_scaled, (left, (height - glyph_scaled.size[1]) // 2), glyph_scaled
    )
    canvas.paste(
        wordmark_scaled,
        (
            left + glyph_scaled.size[0] + LOGO_GAP,
            (height - wordmark_scaled.size[1]) // 2,
        ),
        wordmark_scaled,
    )
    return canvas


def half(image: Image.Image) -> Image.Image:
    """Return the 1x version of an @2x image."""
    return image.resize((image.size[0] // 2, image.size[1] // 2), Image.LANCZOS)


def build_images() -> dict[str, Image.Image]:
    """Return the brand images to write, keyed by their file name."""
    artwork = brand_artwork(Image.open(SOURCE))
    glyph, wordmark = split_artwork(artwork)
    icon, logo = build_icon(glyph), build_logo(glyph, wordmark)
    return {
        "icon.png": half(icon),
        "icon@2x.png": icon,
        "logo.png": half(logo),
        "logo@2x.png": logo,
    }


def main() -> int:
    """Write the brand images, or report whether the committed ones are current."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="write nothing and fail when the committed images differ",
    )
    args = parser.parse_args()

    if not SOURCE.is_file():
        raise SystemExit(f"missing source logo: {SOURCE}")

    stale = []
    for name, image in build_images().items():
        path = BRAND_DIR / name
        if args.check:
            with Image.open(path) as committed:
                current = (
                    committed.convert("RGBA").tobytes()
                    == image.convert("RGBA").tobytes()
                )
            print(f"{name}: {'up to date' if current else 'differs'}")
            if not current:
                stale.append(name)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path, format="PNG", optimize=True)
        print(f"{name}: {image.size[0]}x{image.size[1]} written to {path}")

    if stale:
        print(f"out of date: {', '.join(stale)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
