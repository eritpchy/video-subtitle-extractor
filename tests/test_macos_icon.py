"""Mac optical sizing, alpha, complete ICNS export and running Qt consistency."""
import math
from pathlib import Path
import plistlib
import shutil
import struct
import subprocess
import sys

from PIL import Image, ImageChops
import pytest

from scripts.build_macos_icon import (
    CANVAS_SIZE, CONTENT_SIZE, INSET, PIXEL_SIZES, REPRESENTATIONS, RGB_MASKS,
    export_macos_icon, export_macos_iconset, render_macos_frames,
)
from scripts.portable_python import write_launchers

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "design/icon_1024.png"


@pytest.fixture(scope="module")
def frames():
    return render_macos_frames(SOURCE)


@pytest.mark.parametrize("size", PIXEL_SIZES)
def test_every_size_has_centered_transparent_padding_and_original_rounding(frames, size):
    frame = frames[size]
    assert frame.mode == "RGBA"
    assert frame.size == (size, size)
    alpha = frame.getchannel("A")
    inset = math.floor(size * INSET / CANVAS_SIZE)
    bounds = (inset, inset, size - inset, size - inset)
    assert alpha.getbbox() == bounds
    # Not only four empty corner pixels: every complete outer edge is empty.
    for edge in ((0, 0, size, inset), (0, 0, inset, size),
                 (0, size - inset, size, size), (size - inset, 0, size, size)):
        assert alpha.crop(edge).getbbox() is None
    assert alpha.getpixel((size // 2, size // 2)) >= 250
    # The inset alone must not disguise a newly squared-off brand silhouette.
    # Test its own corners, in addition to the transparent canvas corners.
    for point in ((inset, inset), (size - inset - 1, inset),
                  (inset, size - inset - 1), (size - inset - 1, size - inset - 1)):
        assert alpha.getpixel(point) <= 20
    visible = alpha.point(lambda value: 255 if value >= 128 else 0).getbbox()
    for coordinate, expected in zip(visible, (size * INSET / CANVAS_SIZE,) * 2
                                   + (size * (1 - INSET / CANVAS_SIZE),) * 2):
        assert abs(coordinate - expected) <= 1


def test_brand_sources_and_windows_export_are_never_modified(tmp_path):
    original = {path: path.read_bytes() for path in (SOURCE, ROOT / "design/vse.ico")}
    export_macos_icon(SOURCE, tmp_path / "vse.icns")
    for path, data in original.items():
        assert path.read_bytes() == data
    with Image.open(SOURCE) as source:
        # Regression diagnosis: rounded corners alone did not supply padding.
        assert source.getchannel("A").getbbox() == (0, 0, 1024, 1024)


def test_small_frames_are_sampled_from_source_not_cascaded_or_upscaled(frames):
    for size in (16, 32, 64):
        cascaded = frames[1024].resize((size, size), Image.Resampling.LANCZOS)
        # Direct source sampling preserves information a second resize loses.
        assert ImageChops.difference(frames[size], cascaded).getbbox() is not None
        # At actual tiny sizes the existing red eyes still retain chroma.
        assert any(red > green + 40 and red > blue + 15 and alpha > 200
                   for red, green, blue, alpha in frames[size].getdata())


def test_icns_roundtrip_has_all_ten_exact_native_and_retina_representations(tmp_path, frames):
    output = tmp_path / "vse.icns"
    export_macos_icon(SOURCE, output)
    data = output.read_bytes()
    assert data[:4] == b"icns"
    assert struct.unpack(">I", data[4:8])[0] == len(data)
    with Image.open(output) as icon:
        expected = {(points, points, scale) for _, points, scale in REPRESENTATIONS}
        assert set(icon.info["sizes"]) == expected
        for representation in expected:
            image = icon.icns.getimage(representation).convert("RGBA")
            size = representation[0] * representation[2]
            assert image.size == (size, size)
            assert image.tobytes() == frames[size].tobytes()


def test_small_icns_members_are_planar_rgb_with_lossless_masks_not_png(tmp_path, frames):
    output = tmp_path / "vse.icns"
    export_macos_icon(SOURCE, output)
    data = output.read_bytes()
    members = {}
    offset = 8
    while offset < len(data):
        kind = data[offset:offset + 4]
        length = struct.unpack(">I", data[offset + 4:offset + 8])[0]
        assert length >= 8 and offset + length <= len(data)
        assert kind not in members
        members[kind] = data[offset + 8:offset + length]
        offset += length
    assert offset == len(data)
    assert set(members) == {kind for kind, _, _ in REPRESENTATIONS} | set(RGB_MASKS.values())
    assert not {b"icp4", b"icp5", b"icp6"} & members.keys()
    for kind, size in ((b"is32", 16), (b"il32", 32)):
        assert members[RGB_MASKS[kind]] == frames[size].getchannel("A").tobytes()
        # Independent literal-packet decoder: test channel order and boundaries,
        # not just what our own encoder or PNG-tolerant Qt/Pillow will accept.
        stream = iter(members[kind])
        for channel in ("R", "G", "B"):
            decoded = bytearray()
            while len(decoded) < size * size:
                count = next(stream) + 1
                assert 1 <= count <= 128
                decoded.extend(next(stream) for _ in range(count))
            assert bytes(decoded) == frames[size].getchannel(channel).tobytes()
        assert next(stream, None) is None


def test_iconset_matches_icns_frames_and_apple_naming(tmp_path, frames):
    output = tmp_path / "vse.iconset"
    export_macos_iconset(SOURCE, output)
    expected = set()
    for _, points, scale in REPRESENTATIONS:
        suffix = "@2x" if scale == 2 else ""
        name = f"icon_{points}x{points}{suffix}.png"
        expected.add(name)
        with Image.open(output / name) as image:
            assert image.tobytes() == frames[points * scale].tobytes()
    assert {path.name for path in output.iterdir()} == expected
    with pytest.raises(ValueError, match=".iconset"):
        export_macos_iconset(SOURCE, tmp_path / "not-an-iconset")


@pytest.mark.skipif(sys.platform != "darwin", reason="Requires Apple's native iconutil decoder")
def test_native_iconutil_decodes_every_exported_representation(tmp_path, frames):
    output = tmp_path / "vse.icns"
    decoded = tmp_path / "decoded.iconset"
    export_macos_icon(SOURCE, output)
    subprocess.run(["/usr/bin/iconutil", "-c", "iconset", "-o", str(decoded), str(output)],
                   check=True, capture_output=True, text=True, timeout=30)
    for _, points, scale in REPRESENTATIONS:
        suffix = "@2x" if scale == 2 else ""
        with Image.open(decoded / f"icon_{points}x{points}{suffix}.png") as image:
            actual = image.convert("RGBA")
            expected = frames[points * scale]
            assert actual.size == expected.size
            assert actual.tobytes() == expected.tobytes(), (
                f"icon_{points}x{points}{suffix}.png",
                actual.getchannel("A").getextrema(), expected.getchannel("A").getextrema(),
                ImageChops.difference(actual, expected).getextrema(),
            )


@pytest.mark.parametrize("shape,color", [((512, 512), (1, 2, 3, 0)),
                                         ((1024, 512), (1, 2, 3, 0)),
                                         ((1024, 1024), (1, 2, 3, 255)),
                                         ((1024, 1024), (1, 2, 3, 0))])
def test_invalid_source_is_rejected(tmp_path, shape, color):
    source = tmp_path / "invalid.png"
    Image.new("RGBA", shape, color).save(source)
    with pytest.raises(ValueError):
        render_macos_frames(source)


def test_portable_mac_launcher_uses_corrected_icon(tmp_path, frames):
    root = tmp_path / "VSE.app/Contents/Resources"
    (root / "design").mkdir(parents=True)
    shutil.copyfile(SOURCE, root / "design/icon_1024.png")
    write_launchers(root, "macos", "arm64")
    info = plistlib.loads((root.parent / "Info.plist").read_bytes())
    assert info["CFBundleIconFile"] == "vse.icns"
    with Image.open(root / info["CFBundleIconFile"]) as icon:
        assert icon.icns.getimage((512, 512, 2)).tobytes() == frames[1024].tobytes()
    assert not (root.parent / "_CodeSignature").exists()


@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PySide6")
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "cocoa" if sys.platform == "darwin" else "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("packaged", (False, True))
def test_running_mac_qt_icon_matches_finder_at_every_pixel_size(tmp_path, frames, qt_app, packaged):
    from ui.app_icon import load_app_icon
    root = tmp_path
    if packaged:
        export_macos_icon(SOURCE, root / "vse.icns")
    else:
        (root / "design").mkdir()
        shutil.copyfile(SOURCE, root / "design/icon_1024.png")
    icon = load_app_icon(root, "darwin")
    assert not icon.isNull()
    available = {(size.width(), size.height()) for size in icon.availableSizes()}
    assert {(size, size) for size in PIXEL_SIZES} <= available
    for size in PIXEL_SIZES:
        image = icon.pixmap(size, size).toImage()
        assert not image.isNull()
        assert image.width() == image.height() == size
        # QPixmap premultiplies RGB; alpha must nevertheless match exactly.
        expected = frames[size].getchannel("A")
        assert bytes(image.pixelColor(x, y).alpha() for y in range(size) for x in range(size)) == expected.tobytes()
    if not packaged:
        assert not (root / "vse.icns").exists()


def test_qt_icns_decoder_does_not_warn_and_recover_from_a_bad_table(tmp_path, qt_app):
    from PySide6.QtCore import qInstallMessageHandler
    from PySide6.QtGui import QIcon
    output = tmp_path / "vse.icns"
    export_macos_icon(SOURCE, output)
    messages = []
    previous = qInstallMessageHandler(lambda _kind, _context, text: messages.append(text))
    try:
        icon = QIcon(str(output))
        assert len(icon.availableSizes()) == len(PIXEL_SIZES)
        for size in PIXEL_SIZES:
            assert not icon.pixmap(size, size).isNull()
    finally:
        qInstallMessageHandler(previous)
    assert not [message for message in messages if "ICNS" in message.upper()]


@pytest.mark.parametrize("target", ("win32", "linux"))
def test_non_mac_qt_icon_remains_original_ico(qt_app, target):
    from ui.app_icon import load_app_icon
    icon = load_app_icon(ROOT, target)
    assert not icon.isNull()
    # The existing Windows/Linux icon intentionally remains full-bleed.
    assert icon.pixmap(128, 128).toImage().pixelColor(64, 0).alpha() > 0
