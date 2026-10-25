"""Fetch reviewed native source bytes into an isolated build directory."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parent

def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()

def relative(value):
    p = PurePosixPath(value)
    if not value or p.is_absolute() or '..' in p.parts or '\\' in value or ':' in value or str(p) != value:
        raise ValueError('Unsafe relative path: ' + value)
    return p

def fetch(output, cache, offline=False, pins=None):
    output, cache = Path(output), Path(cache)
    if output.is_symlink() or cache.is_symlink():
        raise ValueError('Source output/cache cannot be symlinks')
    output.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    records = json.loads(Path(pins or ROOT / 'deps/source-pins.json').read_text())
    for row in records:
        name = relative(row['file'])
        if len(name.parts) != 1:
            raise ValueError('Pinned downloads must have basename paths')
        archive = cache / row['file']
        if archive.is_symlink():
            raise ValueError('Cached source cannot be a symlink')
        if not archive.exists():
            if offline:
                raise FileNotFoundError('Offline cache missing: ' + str(archive))
            fd, temp = tempfile.mkstemp(prefix='.source-', dir=cache)
            os.close(fd)
            try:
                with urllib.request.urlopen(row['url'], timeout=120) as src, open(temp, 'wb') as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
                if Path(temp).stat().st_size != row['bytes'] or sha256(temp) != row['sha256']:
                    raise ValueError('Downloaded source checksum mismatch: ' + row['file'])
                os.replace(temp, archive)
            finally:
                Path(temp).unlink(missing_ok=True)
        if archive.stat().st_size != row['bytes'] or sha256(archive) != row['sha256']:
            raise ValueError('Cached source checksum mismatch: ' + row['file'])
        target = output / row['file']
        if target.is_symlink():
            raise ValueError('Source destination cannot be a symlink')
        if target.exists():
            if sha256(target) != row['sha256']:
                raise ValueError('Existing source checksum mismatch: ' + row['file'])
        elif archive.resolve() != target.resolve():
            shutil.copy2(archive, target)
    source = output / 'opencv-4.10.0'
    if not source.exists():
        with tarfile.open(output / 'opencv-4.10.0.tar.gz', 'r:gz') as tar:
            # Python 3.12+ data filter rejects traversal and unsafe links/devices.
            tar.extractall(output, filter='data')
    # A cached extracted tree is not trusted merely because its tar was pinned.
    # Recompare every regular member and reject extra/symlink source files.
    expected = set()
    with tarfile.open(output / 'opencv-4.10.0.tar.gz', 'r:gz') as tar:
        for member in tar:
            if not member.isfile():
                if member.issym() or member.islnk():
                    raise ValueError('Unexpected source archive link')
                continue
            name = relative(member.name.removeprefix('./'))
            if name.parts[0] != 'opencv-4.10.0':
                raise ValueError('Unexpected source archive root')
            target = output / str(name)
            expected.add(str(name))
            if target.is_symlink() or not target.is_file() or target.stat().st_size != member.size:
                raise ValueError('Extracted source mismatch: ' + str(name))
            h = hashlib.sha256()
            with tar.extractfile(member) as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    h.update(block)
            if sha256(target) != h.hexdigest():
                raise ValueError('Extracted source checksum mismatch: ' + str(name))
    actual = {str(p.relative_to(output)).replace('\\', '/') for p in source.rglob('*') if p.is_file() or p.is_symlink()}
    if actual != expected:
        raise ValueError('Unexpected files in extracted source tree')
    if source.is_symlink() or not (source / 'CMakeLists.txt').is_file():
        raise ValueError('Invalid OpenCV source extraction')
    return output

if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--cache', required=True, type=Path)
    p.add_argument('--offline', action='store_true')
    a = p.parse_args()
    print(fetch(a.output, a.cache, a.offline))
