import io
from pathlib import Path
import subprocess
import tarfile

import pytest

from scripts.portable_python import extract_runtime, write_launchers


def archive(tmp_path, entries):
    output = tmp_path / 'python.tar.gz'
    with tarfile.open(output, 'w:gz') as tar:
        for name, contents, link in entries:
            item = tarfile.TarInfo(name)
            if link:
                item.type = tarfile.SYMTYPE
                item.linkname = contents
                tar.addfile(item)
            else:
                data = contents.encode()
                item.size = len(data)
                tar.addfile(item, io.BytesIO(data))
    return output


def test_internal_alias_materialized(tmp_path):
    source = archive(tmp_path, [('python/bin/python3.12', 'test', False), ('python/bin/python3', 'python3.12', True)])
    extract_runtime(source, tmp_path / 'runtime')
    alias = tmp_path / 'runtime/bin/python3'
    assert not alias.is_symlink()
    assert alias.read_text() == 'test'


@pytest.mark.parametrize('entries', [
    [('python/../../evil', 'x', False)],
    [('python/bin/a', '/etc/passwd', True)],
    [('python/bin/a', '../../../etc/passwd', True)],
    [('python/bin/a', 'b', True), ('python/bin/b', 'a', True)],
    [('python/bin/a', 'x', False), ('python/bin/a', 'y', False)],
    [('python/bin/a', 'x', False), ('python/bin/a/b', 'y', False)],
])
def test_invalid_archive_rejected(tmp_path, entries):
    with pytest.raises(ValueError):
        extract_runtime(archive(tmp_path, entries), tmp_path / 'runtime')


def test_launcher_without_host_python_or_tools(tmp_path):
    root = tmp_path / '空间 with spaces'
    (root / 'runtime/bin').mkdir(parents=True)
    python = root / 'runtime/bin/python3.12'
    # A tiny shell stand-in records all arguments and inherited native lib path.
    python.write_text('#!/bin/sh\nprintf "%s\\n" "$@" "$LD_LIBRARY_PATH"\n')
    python.chmod(0o755)
    write_launchers(root, 'linux', 'x64')
    result = subprocess.run([str(root / 'Start-VSE.sh'), '参数 with space'], cwd='/', env={'PATH': '/nonexistent'}, text=True, capture_output=True, check=True)
    assert str(root / 'bootstrap.py') in result.stdout
    assert '参数 with space' in result.stdout
    assert str(root / 'native-libs') in result.stdout


def test_windows_launcher_no_host_interpreter(tmp_path):
    write_launchers(tmp_path, 'windows', 'x64')
    content = (tmp_path / 'Start-VSE.bat').read_text()
    assert '"%~dp0runtime\\python.exe"' in content
    assert 'DisableDelayedExpansion' in content


def test_mac_launcher_relative_and_unsigned(tmp_path):
    app = tmp_path / 'Video Subtitle Extractor.app/Contents'
    root = app / 'Resources'
    root.mkdir(parents=True)
    write_launchers(root, 'macos', 'arm64')
    assert (app / 'Info.plist').exists()
    assert '${0%/*}/../Resources' in (app / 'MacOS/VSE').read_text()
    assert not (app / '_CodeSignature').exists()


def test_mac_app_alone_can_move(tmp_path):
    app = tmp_path / 'build' / 'Video Subtitle Extractor.app'
    payload = app / 'Contents/Resources'
    (payload / 'runtime/bin').mkdir(parents=True)
    python = payload / 'runtime/bin/python3.12'
    python.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    python.chmod(0o755)
    write_launchers(payload, 'macos', 'arm64')
    moved = tmp_path / 'Applications 测试' / app.name
    moved.parent.mkdir()
    app.rename(moved)
    result = subprocess.run([str(moved / 'Contents/MacOS/VSE'), '参数'], cwd='/', env={'PATH': '/nonexistent'}, text=True, capture_output=True, check=True)
    assert str(moved / 'Contents/Resources/bootstrap.py') in result.stdout
    assert '参数' in result.stdout
    assert str(app) not in result.stdout
