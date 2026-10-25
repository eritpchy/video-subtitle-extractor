import io
import json
import os
import sys
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


@pytest.mark.skipif(os.name != "posix", reason="Executes a POSIX shell fixture; construction is tested on every OS")
def test_launcher_without_host_python_or_tools(tmp_path):
    root = tmp_path / '空间 with spaces'
    (root / 'runtime/bin').mkdir(parents=True)
    python = root / 'runtime/bin/python3.12'
    # A tiny shell stand-in records all arguments and inherited native lib path.
    python.write_text('#!/bin/sh\nprintf "%s\\n" "$@" "$LD_LIBRARY_PATH"\n')
    python.chmod(0o755)
    write_launchers(root, 'linux', 'x64')
    (root / 'bootstrap.py').touch()
    result = subprocess.run([str(root / 'Start-VSE.sh'), '参数 with space'], cwd='/', env={'PATH': '/nonexistent', 'LD_LIBRARY_PATH': '/untrusted/inherited'}, text=True, capture_output=True, check=True)
    lines = result.stdout.splitlines()
    assert Path(lines[4]).samefile(root / 'bootstrap.py')
    assert '参数 with space' in result.stdout
    assert Path(lines[-1]).resolve() == (root / 'native-libs').resolve()
    assert '/untrusted/inherited' not in result.stdout


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


@pytest.mark.skipif(os.name != "posix", reason="Executes a POSIX shell fixture; app construction is tested on every OS")
def test_mac_app_alone_can_move(tmp_path):
    app = tmp_path / 'build' / 'Video Subtitle Extractor.app'
    payload = app / 'Contents/Resources'
    (payload / 'runtime/bin').mkdir(parents=True)
    python = payload / 'runtime/bin/python3.12'
    python.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    python.chmod(0o755)
    write_launchers(payload, 'macos', 'arm64')
    (payload / 'bootstrap.py').touch()
    moved = tmp_path / 'Applications 测试' / app.name
    moved.parent.mkdir()
    app.rename(moved)
    result = subprocess.run([str(moved / 'Contents/MacOS/VSE'), '参数'], cwd='/', env={'PATH': '/nonexistent'}, text=True, capture_output=True, check=True)
    assert Path(result.stdout.splitlines()[4]).samefile(moved / 'Contents/Resources/bootstrap.py')
    assert '参数' in result.stdout
    assert str(app) not in result.stdout


def test_linux_launcher_construction_is_isolated(tmp_path):
    write_launchers(tmp_path, 'linux', 'x64')
    content = (tmp_path / 'Start-VSE.sh').read_text()
    assert 'export LD_LIBRARY_PATH="$ROOT/native-libs"' in content
    assert 'exec "$ROOT/runtime/bin/python3.12" -I -B -X utf8 "$ROOT/bootstrap.py" "$@"' in content
    assert 'unset PYTHONHOME PYTHONPATH' in content


@pytest.mark.skipif(os.name != 'nt', reason='Executes native Windows cmd.exe launcher')
def test_windows_launcher_executes_without_path_python(tmp_path):
    # This is a launcher quoting/argument test, not standalone runtime acceptance.
    # An explicit absolute interpreter bridge avoids network downloads in unit
    # tests; package verification separately runs the actual bundled interpreter.
    root = tmp_path / '空间 with spaces & bang!'
    (root / 'runtime').mkdir(parents=True)
    bridge = root / 'runtime/python.cmd'
    bridge.write_text(f'@"{sys.executable}" %*\n', encoding='utf-8')
    probe = root / 'bootstrap.py'
    probe.write_text('import json,os,sys\nfrom pathlib import Path\nPath(os.environ["PROBE_OUTPUT"]).write_text(json.dumps({"args":sys.argv,"home":os.environ.get("PYTHONHOME"),"path":os.environ.get("PYTHONPATH")}),encoding="utf-8")\n', encoding='utf-8')
    output = tmp_path / 'probe.json'
    write_launchers(root, 'windows', 'x64', 'runtime/python.cmd')
    env = dict(os.environ, PATH=str(tmp_path / 'no-python'), PROBE_OUTPUT=str(output), PYTHONHOME='invalid', PYTHONPATH='invalid')
    cmd = os.environ.get('COMSPEC', str(Path(os.environ['SystemRoot']) / 'System32/cmd.exe'))
    command = f'""{root / "Start-VSE.bat"}" "参数 with space""'
    subprocess.run(f'"{cmd}" /d /s /c {command}', cwd=tmp_path, env=env, check=True)
    actual = json.loads(output.read_text(encoding='utf-8'))
    assert Path(actual['args'][0]).samefile(probe)
    assert actual['args'][1:] == ['参数 with space']
    assert not actual['home'] and not actual['path']
