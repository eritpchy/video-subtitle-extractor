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
import urllib.error
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


def _download_file(url, destination):
    """One unauthenticated request to an independently published official URL.

    Do not retry denied URLs, alter headers to evade access restrictions, or
    silently switch origins after an HTTP error. Source choice is explicit.
    """
    try:
        with urllib.request.urlopen(url, timeout=120) as response, destination.open('wb') as target:
            shutil.copyfileobj(response, target)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f'Official model source returned HTTP {error.code}: {url}') from error
    except urllib.error.URLError as error:
        raise RuntimeError(f'Cannot reach official model source {url}: {error.reason}') from error


def _download_huggingface_files(entry, extracted):
    source = entry['huggingface']
    for filename, expected in entry['files'].items():
        destination = extracted / filename
        _download_file(f"{source['base_url']}/{filename}", destination)
        if file_sha256(destination) != expected:
            raise RuntimeError(f'SHA256 verification failed for {source["repository"]}/{filename}')


def _extract_verified_archive(archive, entry, model_name, extracted):
    if file_sha256(archive) != entry['sha256']:
        raise RuntimeError(f'SHA256 verification failed for {model_name}')
    with tarfile.open(archive) as bundle:
        # Read only pinned regular files. Never extract arbitrary tar paths,
        # symlinks, device nodes, or executable archive metadata.
        for filename in entry['files']:
            member = bundle.getmember(f'{model_name}_infer/{filename}')
            if not member.isfile():
                raise RuntimeError(f'Unexpected archive member: {member.name}')
            with bundle.extractfile(member) as source, (extracted / filename).open('wb') as target:
                shutil.copyfileobj(source, target)


def download_model(model_name, output=None, archive_dir=None, source='huggingface'):
    from filelock import FileLock

    if source not in ('huggingface', 'bos'):
        raise ValueError(f'Unknown official model source: {source}')
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
            extracted = temporary / 'model'
            extracted.mkdir()
            if archive_dir:
                archive = Path(archive_dir) / f'{model_name}_infer.tar'
                _extract_verified_archive(archive, entry, model_name, extracted)
            elif source == 'huggingface':
                print(f'Downloading {model_name} from the official PaddlePaddle Hugging Face repository...', flush=True)
                _download_huggingface_files(entry, extracted)
            else:
                archive = temporary / 'model.tar'
                print(f'Downloading {model_name} from the official Paddle BOS model host...', flush=True)
                _download_file(entry['url'], archive)
                _extract_verified_archive(archive, entry, model_name, extracted)
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
    parser.add_argument('--source', choices=['huggingface', 'bos'], default='huggingface',
                        help='Official source; immutable Hugging Face files by default, BOS archive opt-in')
    args = parser.parse_args(argv)
    for tier in dict.fromkeys(args.tiers):
        for task in ('det', 'rec'):
            name = f'PP-OCRv6_{tier}_{task}'
            if args.verify_only:
                if not verify_model(args.output / f'{name}_infer', name):
                    parser.exit(1, f'Model verification failed: {name}\n')
            else:
                download_model(name, args.output, args.archive_dir, args.source)
            print(f'Verified {name}', flush=True)


if __name__ == '__main__':
    main()
