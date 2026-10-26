# Mac icon optical sizing

## Diagnosis and scope

The original `design/icon_1024.png` is already rounded, but its nonzero alpha
bounds are `(0, 0, 1024, 1024)`. The old ICNS export filled the full canvas, so its
visible body appeared larger than inset icons at the same Dock/Finder allocation.
In addition, the GUI set the application icon to the full-bleed Windows ICO after
launch, which could undo a Finder-only fix.

`scripts/build_macos_icon.py` keeps the original brand image and original corner
mask unchanged. It places that image at **824 × 824 inside a 1024 × 1024 canvas**,
with **100 pixels (9.765625%) of transparent space per side**. This is a project
optical-sizing choice for the existing flattened ICNS pipeline, not an Apple
requirement or a claim of native visual approval.

Every pixel size (16–1024) is sampled directly from the original with a centered
fractional sampling box and alpha-aware LANCZOS. Each ICNS contains all ten
standard/Retina representations, including true 16px and 32px images. The
transparent margin is retained at every size; no round-corner redraw, shadows,
artwork crop, color adjustment, or Windows/Linux icon change is performed.

The two small @1x images use native planar RGB (`is32`, `il32`) plus complete
8-bit alpha masks (`s8mk`, `l8mk`). The other eight images use PNG. Literal-only
ICNS PackBits packets preserve each RGB byte and avoid raw-pixel layout ambiguity.
The optional table-of-contents member is omitted: Qt otherwise emits a
corrupt-table warning even when reading Pillow's own ICNS export. All member
types/lengths and the complete file length are independently verified.

### Native-only regression caught in the first Server run

The initial export stored the small images as PNG inside `icp4`/`icp5`. Pillow
and Qt accepted those, but macOS Server CI run `40252263584` on `b12124b` caught
Apple `iconutil` decoding the 16px representation as opaque garbage (first RGBA
byte `0x50`, all alpha bytes 255). That was a real format bug, not a pixel
tolerance issue. The RGB-plus-mask export fixes the encoding while retaining
the strict, byte-for-byte native RGBA gate and all original alpha/geometry tests.

This decoder distinction is independently documented in the
[icon-format compatibility research](https://github.com/relikd/icns-analysis)
and a [first-party exporter regression fix](https://github.com/electron-userland/electron-builder-binaries/pull/220).
The latter used newer ARGB slots; this exporter uses the long-supported
RGB-plus-mask slots, which are also readable by the existing Pillow/Qt versions.

`ui/app_icon.py` uses the same packaged ICNS for the running Mac application.
Source checkouts render the same frames in memory without writing to the source
tree or touching system caches. Windows/Linux continue using `design/vse.ico`.

## Apple references (checked 2026-10-25)

- [High Resolution Guidelines for OS X](https://developer.apple.com/library/archive/documentation/GraphicsAnimation/Conceptual/HighResolutionOSX/Optimizing/Optimizing.html)
  specifies paired standard/Retina icon representations, complete iconset naming,
  and inspecting packaged artwork in Quick Look. This is the relevant legacy
  ICNS packaging guidance for this app's macOS 13+ flattened icon pipeline.
- [Current App Icons HIG](https://developer.apple.com/design/human-interface-guidelines/app-icons)
  emphasizes centered content, small-size legibility and system/template
  consistency. Its current unmasked-layer guidance targets the newer layered
  Icon Composer pipeline; do not apply that instruction by stripping the rounded
  transparency from this legacy flattened ICNS.

## Reproducible review assets

```bash
python scripts/build_macos_icon.py \
  --output test-results/macos-icon/vse.icns \
  --iconset test-results/macos-icon/vse.iconset \
  --preview test-results/macos-icon/before-after.png
python -m pytest tests/test_app_icon.py tests/test_macos_icon.py tests/test_portable_python.py
```

The preview is explicitly labelled **generated asset preview, not a macOS
screenshot**. It shows both light/dark backgrounds at actual 16/32/64/128px sizes.
Tests decode all ICNS representations, verify exact canvas/alpha bounds and
rounded corners, and compare the running Qt icon's alpha to the Finder asset.

## Native visual acceptance still required

Use the newly built `.app` from its own fresh extraction directory (not a replaced
older bundle), check the source revision and verify `CFBundleIconFile=vse.icns`.
Do not delete icon caches, restart the Dock, or alter already published releases.

1. On macOS, decode the generated ICNS with Apple's `iconutil -c iconset -o
   /tmp/vse-review.iconset <bundle>/Contents/Resources/vse.icns` and compare the
   PNG dimensions/alpha with the generated iconset. Use a new output directory.
2. Inspect the new application in Finder at list and large-icon sizes. Capture a
   real screenshot alongside standard apps at the same Finder icon size.
3. Launch this exact new bundle and capture the real Dock alongside neighboring
   apps, with the pointer away from the Dock to avoid magnification. Confirm the
   running icon remains the same size and keeps its original rounded silhouette.
4. Capture light and dark appearances where available. Record OS version, display
   scale and bundle revision; send the actual screenshots for user confirmation.

Linux Qt/offscreen checks establish decoding and geometry, **not native Dock or
Finder visual approval**. If an old cached icon remains, report that observation
and request permission before considering any cache/Dock modification.
