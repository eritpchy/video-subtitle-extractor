"""Download genuine, checksum-pinned official PP-OCRv6 inference bundles.

Run ``python -m backend.tools.model_download --tiers small medium`` before
building an offline distribution. This module deliberately does not import Qt
or Paddle so it can also run during a lightweight packaging step.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import urllib.request

MODEL_ROOT = Path(__file__).resolve().parents[1] / 'models'
MANIFEST_PATH = MODEL_ROOT / 'ppocrv6-manifest.json'


def load_manifest():
    with MANIFEST_PATH.open(encoding='utf-8') as stream:
        return json.load(stream)['models']


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def verify_model(model_dir, model_name):
    """Verify all model bytes, not merely a directory or display name."""
    entry = load_manifest()[model_name]
    for filename, expected in entry['files'].items():
        path = Path(model_dir) / filename
        if not path.is_file() or file_sha256(path) != expected:
            return False
    return True


def download_model(model_name, output=None, archive_dir=None):
    from filelock import FileLock

    entry = load_manifest()[model_name]
    output = Path(output or MODEL_ROOT / 'V6')
    output.mkdir(parents=True, exist_ok=True)
    destination = output / f'{model_name}_infer'
    # Never leave half-downloaded models visible to concurrent OCR workers.
    with FileLock(str(output / f'.{model_name}.lock')):
        if verify_model(destination, model_name):
            return destination
        if destination.exists():
            raise RuntimeError(f'Incomplete or modified model: {destination}. Move it aside before downloading again.')
        with tempfile.TemporaryDirectory(prefix=f'.{model_name}-', dir=output) as temporary:
            temporary = Path(temporary)
            archive = Path(archive_dir) / f'{model_name}_infer.tar' if archive_dir else temporary / 'model.tar'
            if not archive_dir:
                print(f'Downloading {model_name} from the official Paddle model host...', flush=True)
                with urllib.request.urlopen(entry['url'], timeout=120) as response, archive.open('wb') as target:
                    shutil.copyfileobj(response, target)
            if file_sha256(archive) != entry['sha256']:
                raise RuntimeError(f'SHA256 verification failed for {model_name}')
            extracted = temporary / 'model'
            extracted.mkdir()
            with tarfile.open(archive) as bundle:
                # Read only the pinned regular files. Never extract arbitrary tar
                # paths, symlinks, device nodes, or executable archive metadata.
                for filename in entry['files']:
                    member = bundle.getmember(f'{model_name}_infer/{filename}')
                    if not member.isfile():
                        raise RuntimeError(f'Unexpected archive member: {member.name}')
                    with bundle.extractfile(member) as source, (extracted / filename).open('wb') as target:
                        shutil.copyfileobj(source, target)
            if not verify_model(extracted, model_name):
                raise RuntimeError(f'Extracted model verification failed: {model_name}')
            os.replace(extracted, destination)
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tiers', nargs='+', choices=['small', 'medium'], default=['small', 'medium'])
    parser.add_argument('--output', type=Path, default=MODEL_ROOT / 'V6')
    parser.add_argument('--archive-dir', type=Path, help='Use already downloaded, checksum-verified archives')
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args(argv)
    for tier in dict.fromkeys(args.tiers):
        for task in ('det', 'rec'):
            name = f'PP-OCRv6_{tier}_{task}'
            if args.verify_only:
                if not verify_model(args.output / f'{name}_infer', name):
                    parser.exit(1, f'Model verification failed: {name}\n')
            else:
                download_model(name, args.output, args.archive_dir)
            print(f'Verified {name}', flush=True)


if __name__ == '__main__':
    main()
