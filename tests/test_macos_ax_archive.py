"""Archive safety contracts only; no native macOS acceptance is implied."""
import hashlib
import os
from pathlib import Path
import stat
import zipfile

import pytest

from scripts import macos_ax_archive as module


def make_zip(path, members):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for member in members:
            name, data, *mode = member
            info = zipfile.ZipInfo(name)
            # ZipInfo normalizes backslashes on Windows. These are deliberately
            # raw unsafe fixtures, so preserve the requested on-disk name in
            # both headers instead of accidentally writing a safe slash path.
            info.filename = name
            info.create_system = 3
            info.external_attr = (mode[0] if mode else
                                  (stat.S_IFDIR | 0o755 if name.endswith("/") else stat.S_IFREG | 0o644)) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            output.writestr(info, data)
    return path


def test_single_root_roundtrip_inventory_and_executable_permissions(tmp_path):
    root = tmp_path / "payload"
    (root / "nested").mkdir(parents=True)
    (root / "nested/readme").write_bytes(b"hello\n")
    executable = root / "launcher"
    executable.write_bytes(b"#!/bin/sh\nexit 0\n")
    executable.chmod(0o6751)
    (root / "empty").mkdir()
    output = tmp_path / "candidate.zip"
    module.write_archive(root, output)
    extracted = module.extract_archive(output, tmp_path / "extracted")
    assert extracted == tmp_path / "extracted/payload"
    assert module.inventory(root) == module.inventory(extracted) == {
        "launcher": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "nested/readme": hashlib.sha256(b"hello\n").hexdigest(),
    }
    if os.name != "nt":
        assert stat.S_IMODE((extracted / "launcher").stat().st_mode) == 0o755
        assert stat.S_IMODE((extracted / "nested/readme").stat().st_mode) == 0o644
    assert (extracted / "empty").is_dir()
    with zipfile.ZipFile(output) as archive:
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())
        assert archive.getinfo("payload/launcher").compress_type == zipfile.ZIP_DEFLATED


def test_github_artifact_variant_allows_multiple_roots_and_top_level_files(tmp_path):
    archive = make_zip(tmp_path / "artifact.zip", [("candidate/plugin", b"plugin"), ("pins.json", b"{}")])
    with pytest.raises(ValueError, match="directory root"):
        module.extract_archive(archive, tmp_path / "not-created")
    destination = tmp_path / "artifact"
    assert module.extract_archive(archive, destination, require_single_root=False) == destination
    assert module.inventory(destination) == {"candidate/plugin": hashlib.sha256(b"plugin").hexdigest(),
                                             "pins.json": hashlib.sha256(b"{}").hexdigest()}


def test_relative_destination_is_supported_without_changing_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    archive = make_zip(Path("input.zip"), [("root/file", b"data")])
    root = module.extract_archive(archive, Path("output"))
    assert root == tmp_path / "output/root"
    assert (root / "file").read_bytes() == b"data"


@pytest.mark.parametrize("name", ["/absolute/file", "../escape", "root/../escape", "root/./file",
                                  "root//file", "root\\file", "C:/root/file", "root/file:stream",
                                  "root/line\nbreak", "root/tab\tname", "root/control\x7f", "root/a\u202eb"])
def test_all_names_are_checked_before_any_payload_is_written(tmp_path, name):
    archive = make_zip(tmp_path / "bad.zip", [("root/good", b"good"), (name, b"bad")])
    destination = tmp_path / "output"
    with pytest.raises(ValueError, match="Unsafe archive member"):
        module.extract_archive(archive, destination)
    assert not destination.exists()
    assert not (tmp_path / "escape").exists()


def test_raw_nul_name_is_rejected_before_zipfile_truncation_can_hide_it(tmp_path):
    archive = make_zip(tmp_path / "bad.zip", [("root/badXtail", b"bad")])
    archive.write_bytes(archive.read_bytes().replace(b"root/badXtail", b"root/bad\x00tail"))
    with pytest.raises(ValueError, match="Unsafe archive member"):
        module.extract_archive(archive, tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_raw_backslash_fixture_survives_windows_constructor_normalization(tmp_path, monkeypatch):
    original = zipfile.ZipInfo
    class WindowsNormalizedZipInfo(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.filename = self.filename.replace("\\", "/")
    with monkeypatch.context() as context:
        context.setattr(zipfile, "ZipInfo", WindowsNormalizedZipInfo)
        archive = make_zip(tmp_path / "raw-backslash.zip", [("root\\file", b"payload")])
    # Local and central names really contain the invalid byte, on every host.
    assert archive.read_bytes().count(b"root\\file") == 2
    with zipfile.ZipFile(archive) as source:
        assert source.infolist()[0].orig_filename == "root\\file"
    with pytest.raises(ValueError, match="Unsafe archive member"):
        module.extract_archive(archive, tmp_path / "never-published")
    assert not (tmp_path / "never-published").exists()


@pytest.mark.parametrize("names", [
    ("root/file", "root/file"), ("root/Dir/", "root/dir/file"),
    ("root/A/file", "root/a/other"), ("ROOT/file", "root/other"),
    ("root/\u00e9", "root/e\u0301"), ("root/file", "root/file/child"),
    ("root/dir/child", "root/dir"), ("root/dir/", "root/dir/"),
])
def test_duplicate_alias_and_file_directory_conflicts_fail_before_extract(tmp_path, names):
    with pytest.warns(UserWarning) if names[0] == names[1] else _no_warning_context():
        archive = make_zip(tmp_path / "bad.zip", [(name, b"" if name.endswith("/") else b"x") for name in names])
    with pytest.raises(ValueError, match="collision|conflict|Duplicate"):
        module.extract_archive(archive, tmp_path / "output")
    assert not (tmp_path / "output").exists()


def _no_warning_context():
    from contextlib import nullcontext
    return nullcontext()


@pytest.mark.parametrize("kind", [stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFSOCK])
def test_special_file_types_are_rejected(tmp_path, kind):
    archive = make_zip(tmp_path / "bad.zip", [("root/special", b"elsewhere", kind | 0o777)])
    with pytest.raises(ValueError, match="symlink/special"):
        module.extract_archive(archive, tmp_path / "output")


@pytest.mark.parametrize("members", [[("root/a", b"", stat.S_IFDIR | 0o755)],
                                      [("root/a/", b"", stat.S_IFREG | 0o644)],
                                      [("root/a/", b"not-empty", stat.S_IFDIR | 0o755)]])
def test_directory_type_or_data_mismatch_is_rejected(tmp_path, members):
    archive = make_zip(tmp_path / "bad.zip", members)
    with pytest.raises(ValueError, match="type disagrees|member size"):
        module.extract_archive(archive, tmp_path / "output")


def test_empty_multiple_root_and_size_or_count_limits(tmp_path):
    archive = make_zip(tmp_path / "empty.zip", [])
    with pytest.raises(ValueError, match="entry count"):
        module.extract_archive(archive, tmp_path / "empty")
    archive = make_zip(tmp_path / "multi.zip", [("one/a", b"a"), ("two/b", b"b")])
    with pytest.raises(ValueError, match="exactly one"):
        module.extract_archive(archive, tmp_path / "multi")
    with pytest.raises(ValueError, match="entry count"):
        module.extract_archive(archive, tmp_path / "limit", max_entries=1)
    archive = make_zip(tmp_path / "size.zip", [("root/a", b"1234")])
    with pytest.raises(ValueError, match="uncompressed size"):
        module.extract_archive(archive, tmp_path / "size", max_uncompressed_bytes=3)
    assert not (tmp_path / "limit").exists() and not (tmp_path / "size").exists()


def test_encrypted_and_unsupported_compression_are_rejected_before_extract(tmp_path):
    archive = make_zip(tmp_path / "encrypted.zip", [("root/a", b"data")])
    content = bytearray(archive.read_bytes())
    content[6] |= 1
    content[content.index(b"PK\x01\x02") + 8] |= 1
    archive.write_bytes(content)
    with pytest.raises(ValueError, match="Unsupported encrypted"):
        module.extract_archive(archive, tmp_path / "encrypted")
    compressed = tmp_path / "bzip2.zip"
    with zipfile.ZipFile(compressed, "w", compression=zipfile.ZIP_BZIP2) as output:
        output.writestr("root/a", b"data")
    with pytest.raises(ValueError, match="Unsupported encrypted/compressed"):
        module.extract_archive(compressed, tmp_path / "bzip2")


def test_preexisting_nonempty_destination_is_never_overwritten(tmp_path):
    archive = make_zip(tmp_path / "input.zip", [("root/file", b"new")])
    destination = tmp_path / "output"
    destination.mkdir()
    (destination / "keep").write_bytes(b"old")
    with pytest.raises(ValueError, match="fresh empty"):
        module.extract_archive(archive, destination)
    assert (destination / "keep").read_bytes() == b"old"
    assert len(list(destination.iterdir())) == 1


def test_crc_failure_cleans_unpublished_payload(tmp_path):
    archive = tmp_path / "bad-crc.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as output:
        output.writestr("root/file", b"KNOWN-PAYLOAD")
    archive.write_bytes(archive.read_bytes().replace(b"KNOWN-PAYLOAD", b"BROKN-PAYLOAD"))
    destination = tmp_path / "output"
    destination.mkdir()
    with pytest.raises(zipfile.BadZipFile, match="CRC"):
        module.extract_archive(archive, destination)
    assert list(destination.iterdir()) == []


def test_write_and_extract_stream_in_bounded_chunks(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    (root / "file").write_bytes(b"123456789" * 30)
    monkeypatch.setattr(module, "CHUNK_SIZE", 7)
    monkeypatch.setattr(Path, "read_bytes", lambda *_: pytest.fail("whole-file reads are not allowed"))
    archive = tmp_path / "result.zip"
    module.write_archive(root, archive)
    assert module.inventory(module.extract_archive(archive, tmp_path / "out")) == module.inventory(root)


def test_output_must_be_new_and_outside_source_tree(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "file").write_bytes(b"original")
    existing = tmp_path / "existing.zip"
    existing.write_bytes(b"keep")
    with pytest.raises(ValueError, match="already exist"):
        module.write_archive(root, existing)
    with pytest.raises(ValueError, match="outside"):
        module.write_archive(root, root / "generated.zip")
    assert existing.read_bytes() == b"keep"
    assert not (root / "generated.zip").exists()


@pytest.mark.skipif(os.name == "nt", reason="Creating test symlinks needs optional Windows privilege")
def test_symlink_sources_destinations_and_outputs_are_never_followed(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"untouched")
    root = tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink/special"):
        module.inventory(root)
    with pytest.raises(ValueError, match="symlink/special"):
        module.write_archive(root, tmp_path / "result.zip")
    destination = tmp_path / "linked-output"
    destination.symlink_to(outside, target_is_directory=True)
    archive = make_zip(tmp_path / "input.zip", [("root/file", b"new")])
    with pytest.raises(ValueError, match="destination must not be a symlink"):
        module.extract_archive(archive, destination)
    (root / "link").unlink()
    archive_link = tmp_path / "linked-archive.zip"
    archive_link.symlink_to(archive)
    with pytest.raises(ValueError, match="regular file"):
        module.extract_archive(archive_link, tmp_path / "new-output")
    output_link = tmp_path / "linked-new.zip"
    output_link.symlink_to(outside / "missing")
    with pytest.raises(ValueError, match="already exist"):
        module.write_archive(root, output_link)
    assert (outside / "secret").read_bytes() == b"untouched"
    assert not (outside / "missing").exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO fixture is POSIX-specific")
def test_inventory_rejects_special_files_before_reading(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    os.mkfifo(root / "fifo")
    with pytest.raises(ValueError, match="symlink/special"):
        module.inventory(root)
