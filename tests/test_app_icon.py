"""Preserve existing rounded artwork throughout ICO and Qt icon export."""
from pathlib import Path

from PIL import Image

from scripts.build_app_icon import ICON_SIZES, export_icon

ROOT = Path(__file__).resolve().parents[1]


def test_committed_icon_matches_export_of_unchanged_rounded_source(tmp_path):
    source = ROOT / "design/icon_1024.png"
    before = source.read_bytes()
    output = tmp_path / "vse.ico"
    export_icon(source, output)
    assert source.read_bytes() == before
    # Native zlib encoders may compress identical PNG frames differently.
    # Compare every decoded RGBA pixel, not platform-specific compressed bytes.
    with Image.open(output) as actual, Image.open(ROOT / "design/vse.ico") as expected:
        assert actual.ico.sizes() == expected.ico.sizes() == set(ICON_SIZES)
        for size in ICON_SIZES:
            assert actual.ico.getimage(size).convert("RGBA").tobytes() == expected.ico.getimage(size).convert("RGBA").tobytes()


def test_all_packaged_icon_sizes_preserve_transparent_rounded_corners():
    with Image.open(ROOT / "design/vse.ico") as icon:
        assert icon.ico.sizes() == set(ICON_SIZES)
        for size in ICON_SIZES:
            image = icon.ico.getimage(size).convert("RGBA")
            width, height = size
            for point in ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)):
                assert image.getpixel(point)[3] == 0, (size, point)
            assert image.getpixel((width // 2, height // 2))[3] >= 250
            assert image.getpixel((width // 2, 0))[3] > 0
