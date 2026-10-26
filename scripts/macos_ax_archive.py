"""Strict, streaming archive helpers for the isolated macOS AX candidate.

These do not bypass or change the normal release packager. Callers must verify
the independently pinned input archive digest before using these helpers.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unicodedata
import zipfile

MAX_ENTRIES = 100_000
MAX_UNCOMPRESSED_BYTES = 8 * 1024**3
CHUNK_SIZE = 1024 * 1024


def _name_parts(name: str, directory: bool) -> tuple[str, ...]:
    if (not name or name.startswith("/") or "\\" in name or ":" in name
            or any(unicodedata.category(char) in {"Cc", "Cf"} for char in name)):
        raise ValueError(f"Unsafe archive member name: {name!r}")
    trimmed = name[:-1] if directory and name.endswith("/") else name
    parts = tuple(trimmed.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"Unsafe archive member name: {name!r}")
    return parts


def _validate_paths(entries: list[tuple[tuple[str, ...], bool]]) -> set[str]:
    # Keep implicit parent directories too: A/file and a/other collide on the
    # usual macOS filesystem even without an explicit A/ directory entry.
    nodes: dict[tuple[str, ...], tuple[tuple[str, ...], bool, bool]] = {}
    roots = set()
    for parts, directory in entries:
        roots.add(parts[0])
        for length in range(1, len(parts) + 1):
            original = parts[:length]
            key = tuple(unicodedata.normalize("NFC", part).casefold() for part in original)
            explicit = length == len(parts)
            is_directory = directory if explicit else True
            previous = nodes.get(key)
            if previous:
                old_name, old_directory, old_explicit = previous
                if old_name != original:
                    raise ValueError(f"Casefold/Unicode archive path collision: {'/'.join(original)!r}")
                if old_directory != is_directory or not old_directory:
                    raise ValueError(f"Archive file/directory path conflict: {'/'.join(original)!r}")
                if explicit and old_explicit:
                    raise ValueError(f"Duplicate archive member: {'/'.join(original)!r}")
                nodes[key] = (original, True, explicit or old_explicit)
            else:
                nodes[key] = (original, is_directory, explicit)
    return roots


@contextmanager
def _regular_input(path: Path):
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"Expected a regular file, not a symlink/special file: {path}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
            raise ValueError(f"Input file identity changed while opening: {path}")
        yield stream


def _empty_destination(destination: Path) -> bool:
    """Return whether we created the fresh destination; reject an existing link."""
    if destination.is_symlink():
        raise ValueError("Extraction destination must not be a symlink")
    if destination.exists():
        if not destination.is_dir() or any(destination.iterdir()):
            raise ValueError("Extraction destination must be a fresh empty directory")
        return False
    destination.mkdir(parents=True, mode=0o700)
    return True


def extract_archive(archive: Path, destination: Path, *, require_single_root: bool = True,
                    max_entries: int = MAX_ENTRIES,
                    max_uncompressed_bytes: int = MAX_UNCOMPRESSED_BYTES) -> Path:
    """Validate every entry before extracting; publish only complete contents.

    Normal candidate/base packages require one directory root. The explicit
    ``False`` variant permits GitHub artifact wrappers with multiple roots and
    returns ``destination`` instead. Both variants use the same safety checks.
    """
    archive = Path(archive)
    # mkdtemp returns an absolute path on Python 3.12; use the same form for
    # publication checks without resolving away a destination symlink.
    destination = Path(os.path.abspath(destination))
    if max_entries <= 0 or max_uncompressed_bytes < 0:
        raise ValueError("Archive limits must be nonnegative with at least one entry")
    with _regular_input(archive) as input_stream, zipfile.ZipFile(input_stream) as source:
        infos = source.infolist()
        if not infos or len(infos) > max_entries:
            raise ValueError("Archive entry count is empty or exceeds the limit")
        members = []
        total = 0
        for info in infos:
            # zipfile truncates filename at NUL; orig_filename retains the raw
            # decoded central-directory name, which must be validated first.
            if info.orig_filename != info.filename:
                raise ValueError(f"Unsafe archive member name: {info.orig_filename!r}")
            directory = info.is_dir()
            parts = _name_parts(info.orig_filename, directory)
            kind = stat.S_IFMT(info.external_attr >> 16)
            if kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise ValueError(f"Archive contains a symlink/special file: {info.filename!r}")
            if ((kind == stat.S_IFDIR and not directory)
                    or (kind == stat.S_IFREG and directory)
                    or (info.external_attr & 0x10 and not directory)):
                raise ValueError(f"Archive member type disagrees with its name: {info.filename!r}")
            if info.flag_bits & 1 or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise ValueError(f"Unsupported encrypted/compressed archive member: {info.filename!r}")
            if info.file_size < 0 or (directory and info.file_size):
                raise ValueError(f"Invalid archive member size: {info.filename!r}")
            total += info.file_size
            if total > max_uncompressed_bytes:
                raise ValueError("Archive uncompressed size exceeds the limit")
            if require_single_root and len(parts) == 1 and not directory:
                raise ValueError("Archive must have one directory root, not a root file")
            members.append((info, parts, directory))
        roots = _validate_paths([(parts, directory) for _, parts, directory in members])
        if require_single_root and len(roots) != 1:
            raise ValueError("Archive must have exactly one directory root")

        created = _empty_destination(destination)
        # A private staging directory prevents incomplete/CRC-failed payloads
        # from being mistaken for successfully extracted artifacts.
        staging = Path(tempfile.mkdtemp(prefix=".ax-extract-", dir=destination))
        published: list[Path] = []
        try:
            for info, parts, directory in members:
                target = staging.joinpath(*parts)
                if directory:
                    target.mkdir(parents=True, exist_ok=True)
                    target.chmod(0o755)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                count = 0
                with source.open(info) as incoming, target.open("xb") as outgoing:
                    while chunk := incoming.read(CHUNK_SIZE):
                        count += len(chunk)
                        if count > info.file_size:
                            raise ValueError("Archive stream exceeds declared member size")
                        outgoing.write(chunk)
                if count != info.file_size:
                    raise ValueError("Archive stream differs from declared member size")
                target.chmod(0o644 | ((info.external_attr >> 16) & 0o111))
            # Recheck the empty destination except for our own staging area.
            if destination.is_symlink() or set(destination.iterdir()) != {staging}:
                raise ValueError("Extraction destination changed during extraction")
            for name in sorted(roots):
                target = destination / name
                staging.joinpath(name).rename(target)
                published.append(target)
        except BaseException:
            for path in published:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
            raise
        finally:
            shutil.rmtree(staging)
            if created and not any(destination.iterdir()):
                destination.rmdir()
    return destination / next(iter(roots)) if require_single_root else destination


def _tree(root: Path) -> tuple[Path, list[tuple[Path, os.stat_result]]]:
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Archive source root must be a real directory")
    root = root.resolve(strict=True)
    entries = []
    def walk_error(error):
        raise error

    for parent, directories, files in os.walk(root, followlinks=False, onerror=walk_error):
        for name in sorted(directories + files):
            path = Path(parent) / name
            information = path.lstat()
            if not (stat.S_ISDIR(information.st_mode) or stat.S_ISREG(information.st_mode)):
                raise ValueError(f"Source tree contains a symlink/special file: {path}")
            if not path.resolve(strict=True).is_relative_to(root):
                raise ValueError(f"Source tree path escaped its root: {path}")
            entries.append((path, information))
    return root, sorted(entries, key=lambda item: item[0].relative_to(root).as_posix())


def inventory(root: Path) -> dict[str, str]:
    """Hash regular files only, rejecting links/special files anywhere in tree."""
    root, entries = _tree(root)
    result = {}
    for path, information in entries:
        if stat.S_ISREG(information.st_mode):
            with _regular_input(path) as stream:
                result[path.relative_to(root).as_posix()] = hashlib.file_digest(stream, "sha256").hexdigest()
    return result


def write_archive(root: Path, output: Path) -> None:
    """Stream a new single-root ZIP; never overwrite inputs or follow links."""
    root, entries = _tree(root)
    output = Path(output)
    if output.is_symlink() or output.exists():
        raise ValueError("Output archive must not already exist")
    if output.resolve(strict=False).is_relative_to(root):
        raise ValueError("Output archive must be outside its source root")
    named = [(root, root.lstat()), *entries]
    validated = []
    for path, information in named:
        directory = stat.S_ISDIR(information.st_mode)
        name = root.name + ("/" + path.relative_to(root).as_posix() if path != root else "")
        if directory:
            name += "/"
        validated.append((_name_parts(name, directory), directory))
    _validate_paths(validated)
    output.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with output.open("xb") as archive_stream:
            created = True
            with zipfile.ZipFile(archive_stream, "w", compression=zipfile.ZIP_DEFLATED,
                                 compresslevel=6, allowZip64=True) as target:
                for (path, information), (parts, directory) in zip(named, validated):
                    info = zipfile.ZipInfo("/".join(parts) + ("/" if directory else ""),
                                           date_time=(1980, 1, 1, 0, 0, 0))
                    info.create_system = 3
                    info.external_attr = ((stat.S_IFDIR | 0o755) if directory else
                                          (stat.S_IFREG | 0o644 | (information.st_mode & 0o111))) << 16
                    info.compress_type = zipfile.ZIP_STORED if directory else zipfile.ZIP_DEFLATED
                    if directory:
                        info.external_attr |= 0x10
                        target.writestr(info, b"")
                    else:
                        info.file_size = information.st_size
                        with _regular_input(path) as incoming, target.open(info, "w", force_zip64=True) as outgoing:
                            shutil.copyfileobj(incoming, outgoing, length=CHUNK_SIZE)
    except BaseException:
        if created:
            output.unlink(missing_ok=True)
        raise
