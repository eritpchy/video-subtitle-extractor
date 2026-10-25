"""Noninteractive, cross-platform video-to-SRT entry point.

Run ``python -m backend.cli --help`` without importing Qt or Paddle.  The GUI and
CLI use the same extraction engine; options affect this run, not saved settings.
"""

import argparse
import ast
import errno
import multiprocessing
import os
from pathlib import Path
import shutil
import sys
import tempfile


def project_version():
    """Read the authoritative version without importing GUI/OCR dependencies."""
    source = Path(__file__).with_name("config.py").read_text(encoding="utf-8")
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "VERSION" for target in node.targets):
            return ast.literal_eval(node.value)
    raise RuntimeError("Project VERSION is missing")


def parse_area(value):
    """Validate normalized top/bottom/left/right coordinates."""
    try:
        ymin, ymax, xmin, xmax = map(float, value.split(","))
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("area must be ymin,ymax,xmin,xmax") from None
    if not (0 <= ymin < ymax <= 1 and 0 <= xmin < xmax <= 1):
        raise argparse.ArgumentTypeError("area must satisfy 0 <= min < max <= 1")
    return ymin, ymax, xmin, xmax


def build_parser():
    parser = argparse.ArgumentParser(description="Extract hardcoded subtitles to UTF-8 SRT.")
    parser.add_argument("--version", action="version", version=f"Video Subtitle Extractor {project_version()}")
    parser.add_argument("input", type=Path, help="input video path")
    parser.add_argument("--output", "-o", type=Path, help="output SRT (default: beside input)")
    parser.add_argument("--language", default="ch", help="OCR language code (e.g. ch, en, japan, korean)")
    parser.add_argument("--mode", choices=("fast", "auto", "accurate"), default="fast")
    parser.add_argument("--area", type=parse_area, default=parse_area("0.65,1,0,1"),
                        help="normalized ymin,ymax,xmin,xmax, measured from top-left (default: bottom 35%%)")
    parser.add_argument("--cpu", action="store_true", help="disable GPU acceleration")
    parser.add_argument("--threads", type=int, default=0, help="VideoSubFinder threads; 0 is automatic")
    parser.add_argument("--overwrite", action="store_true", help="replace an existing output SRT")
    parser.add_argument("--keep-work", type=Path, help="keep intermediate data in this NEW directory")
    parser.add_argument("--no-word-segmentation", action="store_true",
                        help="preserve recognized whitespace instead of post-processing")
    return parser


def publish_output(staged, destination, overwrite=False):
    """Publish complete bytes without clobbering a concurrent creator."""
    with tempfile.NamedTemporaryFile(prefix=".vse-", suffix=".srt", dir=destination.parent, delete=False) as stream:
        publish_path = Path(stream.name)
    try:
        shutil.copyfile(staged, publish_path)
        if overwrite:
            publish_path.replace(destination)
        else:
            # Both paths are on the same filesystem. A hard link atomically
            # creates a complete destination, or fails if anything exists there.
            # Unlike exists()+replace(), another task's new output is safe.
            try:
                os.link(publish_path, destination)
            except OSError as error:
                unsupported_link = error.errno in {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV}
                unsupported_link |= getattr(error, "winerror", None) in {1, 50}
                if not unsupported_link:
                    raise
                # FAT/exFAT and some network mounts cannot hard-link. Exclusive
                # creation still guarantees no existing/concurrent file is
                # replaced. Remove a partial copy only if it is still ours.
                descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
                identity = os.fstat(descriptor)
                try:
                    with os.fdopen(descriptor, "wb") as output, publish_path.open("rb") as source:
                        shutil.copyfileobj(source, output)
                        output.flush()
                        os.fsync(output.fileno())
                except BaseException:
                    try:
                        current = destination.stat(follow_symlinks=False)
                        if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                            destination.unlink()
                    except FileNotFoundError:
                        pass
                    raise
    finally:
        publish_path.unlink(missing_ok=True)


def extract(args):
    # Validate paths before loading heavyweight libraries or downloading models.
    source = args.input.expanduser().resolve()
    destination = (args.output or source.with_suffix(".srt")).expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"Input video does not exist: {source}")
    if source == destination:
        raise ValueError("Output must not replace the input video")
    if destination.exists() and not args.overwrite:
        raise ValueError(f"Output already exists (use --overwrite): {destination}")
    if args.threads < 0:
        raise ValueError("--threads must be nonnegative")
    if args.keep_work and args.keep_work.expanduser().exists():
        raise ValueError("--keep-work must point to a new directory to protect existing data")

    # Qt is used for shared configuration, not to open a window.  Respect an
    # explicitly selected platform (for example, CI's offscreen renderer).
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from backend.config import config
    from backend.bean.subtitle_area import SubtitleArea
    from backend.tools.constant import SubtitleArea as SubtitleAreaMode
    from backend.main import SubtitleExtractor
    from backend.tools.process_manager import ProcessManager

    if args.language not in config.language.validator.options:
        raise ValueError(f"Unsupported language code: {args.language}")
    if args.threads > (os.cpu_count() or 1):
        raise ValueError(f"--threads cannot exceed the available CPU count ({os.cpu_count() or 1})")
    for item, value in (
        (config.language, args.language), (config.mode, args.mode),
        (config.hardwareAcceleration, not args.cpu),
        (config.videoSubFinderCpuCores, args.threads),
        # CLI --area always uses full-video coordinates, independent of a GUI
        # pre-crop preference saved by an earlier interactive run.
        (config.subtitleArea, SubtitleAreaMode.UNKNOWN),
        (config.generateTxt, False),
        (config.wordSegmentation, not args.no_word_segmentation),
        (config.debugNoDeleteCache, bool(args.keep_work)),
    ):
        config.set(item, value, save=False)

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_work = tempfile.TemporaryDirectory(prefix="vse-") if not args.keep_work else None
    work_dir = Path(temporary_work.name) if temporary_work else args.keep_work.expanduser().resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    extractor = None
    try:
        extractor = SubtitleExtractor(str(source), work_dir=str(work_dir))
        ymin, ymax, xmin, xmax = args.area
        extractor.sub_area = SubtitleArea(
            round(ymin * extractor.frame_height), round(ymax * extractor.frame_height),
            round(xmin * extractor.frame_width), round(xmax * extractor.frame_width),
        )
        # Generate into the work directory and atomically replace only after
        # successful extraction, so a failed --overwrite preserves old output.
        staged = work_dir / "result.srt"
        extractor.subtitle_output_path = str(staged)
        config.set(config.debugNoDeleteCache, True, save=False)
        extractor.run()
        if not extractor.isFinished or not staged.is_file():
            raise RuntimeError("Extraction did not produce an SRT file")
        # The work directory can be on another volume; stage beside destination.
        publish_output(staged, destination, args.overwrite)
        print(f"SRT written: {destination}")
        return 0
    finally:
        if extractor is not None and extractor.video_cap is not None:
            extractor.video_cap.release()
        ProcessManager.instance().terminate_all()
        if temporary_work:
            temporary_work.cleanup()


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        multiprocessing.set_start_method("spawn", force=True)
        return extract(args)
    except KeyboardInterrupt:
        print("Extraction cancelled", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Extraction failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
