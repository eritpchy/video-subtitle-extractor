"""Export the unchanged rounded brand art at a Mac-specific optical size.

This is a deterministic build-time layout/format export, not an artwork redraw.
The Windows/Qt source PNG and ICO remain unchanged.  The 100/1024 inset is our
legacy ICNS optical-sizing choice, not a numeric requirement from Apple's HIG.
See docs/MACOS_ICON.md for the format scope and native visual acceptance.
"""
from __future__ import annotations

import argparse
from io import BytesIO
import math
from pathlib import Path
import struct

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
CANVAS_SIZE = 1024
CONTENT_SIZE = 824
INSET = (CANVAS_SIZE - CONTENT_SIZE) // 2
PIXEL_SIZES = (16, 32, 64, 128, 256, 512, 1024)
# Complete standard/@2x iconset. The 16/32-point @1x representations MUST use
# planar RGB plus separate alpha masks: Apple's app-icon decoder interprets
# icp4/icp5 payloads as RGB, not PNG, even though Pillow/Qt accept PNG there.
# The remaining eight representations support PNG on our macOS 13+ target.
REPRESENTATIONS = (
    (b"is32", 16, 1), (b"ic11", 16, 2),
    (b"il32", 32, 1), (b"ic12", 32, 2),
    (b"ic07", 128, 1), (b"ic13", 128, 2),
    (b"ic08", 256, 1), (b"ic14", 256, 2),
    (b"ic09", 512, 1), (b"ic10", 512, 2),
)
RGB_MASKS = {b"is32": b"s8mk", b"il32": b"l8mk"}


def render_macos_frames(source: Path) -> dict[int, Image.Image]:
    """Sample each size directly from the original, preserving its alpha mask.

    A fractional sampling box keeps the artwork precisely centered even at
    16px, where the inset is not an integer. Premultiplied-alpha LANCZOS (Pillow's
    RGBA resize path) avoids dark/white fringes. No tiny frame is upscaled and
    no smaller frame is made from an already-downsampled export.
    """
    with Image.open(source) as original:
        artwork = original.convert("RGBA")
    if artwork.width != artwork.height or artwork.width < CANVAS_SIZE:
        raise ValueError("Mac icon source must be square and at least 1024 pixels")
    edge = artwork.width - 1
    if any(artwork.getpixel(point)[3] for point in ((0, 0), (edge, 0), (0, edge), (edge, edge))):
        raise ValueError("Mac icon must retain the original transparent rounded corners")
    if artwork.getchannel("A").getbbox() is None:
        raise ValueError("Mac icon source must contain visible artwork")

    # Pad the source once, without resampling it. A fractional crop then maps
    # the same 824/1024 artwork extent directly into every output resolution.
    extent = artwork.width * CANVAS_SIZE / CONTENT_SIZE
    padding = math.ceil((extent - artwork.width) / 2) + 3
    padded = Image.new("RGBA", (artwork.width + 2 * padding,) * 2)
    padded.paste(artwork, (padding, padding))
    start = (padded.width - extent) / 2
    box = (start, start, start + extent, start + extent)
    frames = {}
    for size in PIXEL_SIZES:
        sampled = padded.resize((size, size), Image.Resampling.LANCZOS, box=box)
        # LANCZOS can ring faint alpha into otherwise empty pixels. Preserve a
        # real transparent margin, not merely four transparent corner pixels.
        # Fractional edge pixels remain intact for symmetric small-size AA.
        inset = math.floor(size * INSET / CANVAS_SIZE)
        bounds = (inset, inset, size - inset, size - inset)
        frame = Image.new("RGBA", (size, size))
        frame.paste(sampled.crop(bounds), (inset, inset))
        frames[size] = frame
    return frames


def _png_bytes(frame: Image.Image) -> bytes:
    output = BytesIO()
    frame.save(output, format="PNG")
    return output.getvalue()


def _rgb_bytes(frame: Image.Image) -> bytes:
    """Encode ICNS planar RGB with literal PackBits packets, not raw pixels.

    Each channel is a separate stream: control 0..127 precedes 1..128 literal
    bytes. Literal-only packets avoid both the raw-ARGB length ambiguity and
    native decoders' end-of-stream repeat-run quirks. The small-size overhead
    is only 6/24 bytes. Alpha is stored losslessly in s8mk/l8mk, never discarded.
    """
    output = bytearray()
    for channel in ("R", "G", "B"):
        data = frame.getchannel(channel).tobytes()
        for start in range(0, len(data), 128):
            packet = data[start:start + 128]
            output.append(len(packet) - 1)
            output.extend(packet)
    return bytes(output)


def export_macos_icon(source: Path, output: Path) -> None:
    """Write ten ICNS representations with native-compatible small RGB masks.

    ICNS is a big-endian length-delimited container. Keeping the small members
    explicit prevents the OS/Qt from inventing a small icon from a 1024px one.
    Both Pillow's ICNS decoder and Qt's image plugin are tested against this.
    """
    frames = render_macos_frames(source)
    pngs = {size: _png_bytes(frame) for size, frame in frames.items()}
    members = []
    for kind, points, scale in REPRESENTATIONS:
        frame = frames[points * scale]
        if kind in RGB_MASKS:
            members.append((kind, _rgb_bytes(frame)))
            members.append((RGB_MASKS[kind], frame.getchannel("A").tobytes()))
        else:
            members.append((kind, pngs[points * scale]))
    # TOC is optional. Qt reports a corrupt-TOC warning even for Pillow's own
    # conforming TOC output; omit it instead of relying on a warning-and-recover
    # path. Every member remains independently type/length-delimited.
    body = b"".join(kind + struct.pack(">I", len(data) + 8) + data for kind, data in members)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"icns" + struct.pack(">I", len(body) + 8) + body)


def export_macos_iconset(source: Path, output: Path) -> None:
    """Export Apple's named PNG pairs for native iconutil/Quick Look review."""
    output = Path(output)
    if output.suffix != ".iconset":
        raise ValueError("Iconset directory must end in .iconset")
    frames = render_macos_frames(source)
    output.mkdir(parents=True, exist_ok=True)
    for _, points, scale in REPRESENTATIONS:
        suffix = "@2x" if scale == 2 else ""
        frames[points * scale].save(output / f"icon_{points}x{points}{suffix}.png")


def export_preview(source: Path, output: Path) -> None:
    """Generate a clearly labelled comparison, never a simulated OS screenshot."""
    with Image.open(source) as original:
        before = original.convert("RGBA")
    after = render_macos_frames(source)
    preview = Image.new("RGB", (1120, 760), "#eef1f5")
    draw = ImageDraw.Draw(preview)
    font = ImageFont.load_default(size=18)
    title = ImageFont.load_default(size=26)
    draw.text((32, 22), "VSE Mac icon: same artwork, corrected optical size", fill="#172033", font=title)
    draw.text((32, 62), "Generated asset preview - NOT a macOS Dock/Finder screenshot", fill="#475569", font=font)
    draw.text((32, 92), "1024px canvas: visible artwork 1024px -> 824px; 100px transparent inset per edge", fill="#475569", font=font)
    for top, background, foreground, label in (
        (140, "#ffffff", "#172033", "LIGHT"),
        (440, "#242932", "#f1f5f9", "DARK"),
    ):
        draw.rounded_rectangle((24, top, 1096, top + 286), radius=14, fill=background)
        draw.text((44, top + 16), label, fill=foreground, font=font)
        for column, (heading, frames) in enumerate((
            ("Before / full-bleed", None), ("After / inset + original rounded corners", after)
        )):
            left = 44 + column * 530
            draw.text((left, top + 48), heading, fill=foreground, font=font)
            for size, x in ((128, 0), (64, 166), (32, 268), (16, 344)):
                y = top + 106 + (128 - size) // 2
                frame = before.resize((size, size), Image.Resampling.LANCZOS) if frames is None else frames[size]
                preview.paste(frame, (left + x, y), frame)
                draw.text((left + x, top + 250), f"{size}px", fill=foreground, font=font)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    preview.save(output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "design/icon_1024.png")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--iconset", type=Path)
    parser.add_argument("--preview", type=Path)
    args = parser.parse_args()
    export_macos_icon(args.source, args.output)
    if args.iconset:
        export_macos_iconset(args.source, args.iconset)
    if args.preview:
        export_preview(args.source, args.preview)
    print(f"Exported rounded Mac icon: {args.output}")


if __name__ == "__main__":
    main()
