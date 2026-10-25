"""Export Windows/Qt icon sizes from the existing rounded PNG artwork.

This is a format export, not an artwork edit. Keep icon_1024.png unchanged.
"""
from pathlib import Path
import argparse

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
ICON_SIZES = ((16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256))


def export_icon(source: Path, output: Path) -> None:
    with Image.open(source) as original:
        image = original.convert("RGBA")
    if image.width != image.height or image.width < 256:
        raise ValueError("Icon source must be square and at least 256 pixels")
    corners = ((0, 0), (image.width - 1, 0), (0, image.height - 1),
               (image.width - 1, image.height - 1))
    if any(image.getpixel(point)[3] != 0 for point in corners):
        raise ValueError("Use the existing transparent rounded PNG source")
    output.parent.mkdir(parents=True, exist_ok=True)
    frames = []
    for width, height in ICON_SIZES:
        frame = image.resize((width, height), Image.Resampling.LANCZOS)
        # Downsampling can ring alpha into the four fully transparent source
        # corners. Preserve that boundary at every exported icon resolution.
        for point in ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)):
            pixel = frame.getpixel(point)
            frame.putpixel(point, (*pixel[:3], 0))
        frames.append(frame)
    frames[-1].save(output, format="ICO", sizes=ICON_SIZES, append_images=frames[:-1])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "design/icon_1024.png")
    parser.add_argument("--output", type=Path, default=ROOT / "design/vse.ico")
    args = parser.parse_args()
    export_icon(args.source, args.output)
    print(f"Exported rounded icon: {args.output}")


if __name__ == "__main__":
    main()
