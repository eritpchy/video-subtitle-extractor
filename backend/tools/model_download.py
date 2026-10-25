"""Download genuine, checksum-pinned official PP-OCRv6 inference bundles.

Run ``python -m backend.tools.model_download --tiers small medium`` before
building an offline distribution. This module deliberately does not import Qt
or Paddle so it can also run during a lightweight packaging step.
"""
import argparse
import errno
import http.client
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import tarfile
import tempfile
import time
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


def _is_transient_download_error(error):
    if isinstance(error, urllib.error.HTTPError):
        return error.code in (429, 500, 502, 503, 504)
    if isinstance(error, urllib.error.URLError):
        error = error.reason
    # Never retry certificate failures or relax TLS verification.
    if isinstance(error, ssl.SSLError):
        return False
    if isinstance(error, (TimeoutError, http.client.IncompleteRead,
                          http.client.RemoteDisconnected)):
        return True
    if isinstance(error, socket.gaierror):
        return error.errno == socket.EAI_AGAIN
    return isinstance(error, OSError) and error.errno in (
        errno.ECONNRESET, errno.ECONNABORTED, errno.ETIMEDOUT,
    )


def _download_file(url, destination):
    """Atomically download one official URL, with bounded transient retries.

    At most three attempts, each with a 120-second socket timeout, separated
    by 1 and 2 seconds. Denials, TLS failures and unknown errors fail closed;
    retries never change the URL, headers, credentials or certificate checks.
    """
    if os.environ.get('VSE_OFFLINE') == '1':
        raise RuntimeError(f'Offline mode forbids downloading official model source: {url}')
    destination = Path(destination)
    for attempt in range(3):
        partial = None
        try:
            # Stage beside the destination so replacement is atomic. Existing
            # complete files survive failures; partial responses never escape.
            with tempfile.NamedTemporaryFile(prefix=f'.{destination.name}-', suffix='.part',
                                             dir=destination.parent, delete=False) as target:
                partial = Path(target.name)
                with urllib.request.urlopen(url, timeout=120) as response:
                    shutil.copyfileobj(response, target)
            os.replace(partial, destination)
            return
        except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
            if isinstance(error, urllib.error.HTTPError):
                error.close()
            if attempt < 2 and _is_transient_download_error(error):
                retry_delay = 2 ** attempt
            else:
                if isinstance(error, urllib.error.HTTPError):
                    raise RuntimeError(f'Official model source returned HTTP {error.code}: {url}') from error
                reason = getattr(error, 'reason', error)
                raise RuntimeError(f'Cannot reach official model source {url}: {reason}') from error
        finally:
            if partial is not None:
                partial.unlink(missing_ok=True)
        time.sleep(retry_delay)


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
