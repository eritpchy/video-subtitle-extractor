"""Preview/publish a new release without ever replacing a tag or release.

Uses the dispatch checkout's exact SHA. Network/API failures fail closed.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import zipfile
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

try:
    from .package_release import ROOT, read_version, sha256
    from .release_profiles import PROFILES, CANDIDATE_TARGETS, BLOCKED_PROFILES, archive_name
except ImportError:
    from package_release import ROOT, read_version, sha256
    from release_profiles import PROFILES, CANDIDATE_TARGETS, BLOCKED_PROFILES, archive_name


# The approved current matrix is not a claim that CUDA 10.2 supports the new
# application. Its historic 2.2.0 download remains separate; never relabel a
# current CPU package or silently package the old OCR application instead.
ACCEPTANCE_CHECKS = (
    'payload', 'launcher_smoke', 'clean_host_path',
    'contaminated_host_environment', 'cpu_mode_fast',
    'cpu_mode_accurate', 'cpu_mode_auto',
)
PADDLE_MODEL_CHECK = 'paddle_all_models_cpu_modes'


def required_acceptance_checks(build_profile: str) -> tuple[str, ...]:
    profile = PROFILES.get(build_profile)
    if profile is None:
        raise RuntimeError(f'Unknown acceptance build profile: {build_profile}')
    return ACCEPTANCE_CHECKS + ((PADDLE_MODEL_CHECK,) if profile['backend'] == 'paddle' else ())


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeError(f'Duplicate JSON key: {key}')
        result[key] = value
    return result


def _read_json(data: bytes, label: str) -> dict:
    try:
        result = json.loads(data, object_pairs_hook=_unique_json_object)
    except (UnicodeError, ValueError) as error:
        raise RuntimeError(f'Invalid JSON: {label}') from error
    if not isinstance(result, dict):
        raise RuntimeError(f'Expected JSON object: {label}')
    return result


def _checked_file(path: Path) -> Path:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f'Missing or unsafe release asset: {path.name}')
    return path


def candidate_asset_names(version: str) -> dict[str, tuple]:
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?', version):
        raise RuntimeError('Invalid application version')
    return {
        archive_name(version, platform, arch, profile): (platform, arch, profile)
        for platform, arch, profile in CANDIDATE_TARGETS
    }


def assert_complete_release_matrix(matrix: str) -> None:
    if matrix != 'full':
        raise RuntimeError('Publication requires the full approved current release matrix; candidates are build-only')
    if BLOCKED_PROFILES:
        details = '; '.join(f'{name}: {reason}' for name, reason in BLOCKED_PROFILES.items())
        raise RuntimeError(f'Full release matrix is blocked: {details}')


def validate_acceptance_receipt(path: Path, archive: Path, digest: str, manifest: dict) -> None:
    _checked_file(path)
    if path.stat().st_size > 1024 * 1024:
        raise RuntimeError(f'Oversized acceptance receipt: {path.name}')
    receipt = _read_json(path.read_bytes(), path.name)
    expected = {'schema': 1, 'archive': archive.name, 'archive_sha256': digest,
                **{name: manifest[name] for name in
                   ('commit', 'version', 'platform', 'arch', 'build_profile')}}
    for key, value in expected.items():
        if receipt.get(key) != value or type(receipt.get(key)) is not type(value):
            raise RuntimeError(f'Acceptance receipt does not match {key}: {archive.name}')
    checks = receipt.get('checks')
    required = required_acceptance_checks(manifest['build_profile'])
    if not isinstance(checks, dict) or any(checks.get(name) != 'passed' for name in required):
        raise RuntimeError(f'Final package acceptance is missing or failed: {archive.name}')
    # Hosted Server CI has no real GPU acceptance. It may validate an explicit
    # CPU mode, but may not turn a fallback or a packaging pass into GPU proof.
    if receipt.get('gpu_inference_verified') is not False:
        raise RuntimeError(f'Unsupported GPU certification in Server CI receipt: {archive.name}')
    if receipt.get('gpu_validation') != 'not-run-server-ci':
        raise RuntimeError(f'Missing explicit GPU validation limit: {archive.name}')


def validate_assets(directory: Path, sha: str, version: str, *, matrix: str = 'restored-candidates') -> list[str]:
    """Validate exact final ZIPs and hash-bound acceptance without network I/O.

    Receipts are emitted by the final-package acceptance runner only after its
    real launcher/isolation/mode checks pass. They are not independent GPU or
    cryptographic attestations, and are never a substitute for those checks.
    """
    if not re.fullmatch(r'[a-f0-9]{40}', sha):
        raise RuntimeError('Asset validation requires an exact 40-character commit')
    if matrix == 'full':
        assert_complete_release_matrix(matrix)
    elif matrix != 'restored-candidates':
        raise RuntimeError(f'Unknown release matrix: {matrix}')
    if not directory.is_dir() or directory.is_symlink():
        raise RuntimeError('Release asset directory is missing or unsafe')
    expected = candidate_asset_names(version)
    # Reject older archives and unexpected split 7z/ZIP files as well as a
    # missing candidate, instead of accidentally uploading a partial matrix.
    archives = {path.name: path for path in directory.iterdir()
                if path.name.lower().endswith('.zip') or '.7z' in path.name.lower()}
    if set(archives) != set(expected):
        raise RuntimeError('Release asset matrix mismatch: '
                           f'missing={sorted(set(expected) - set(archives))}; '
                           f'unexpected={sorted(set(archives) - set(expected))}')
    assets = []
    for filename, target in sorted(expected.items()):
        platform, arch, profile = target
        engine, accelerator = PROFILES[profile]['backend'], PROFILES[profile]['accelerator']
        archive = _checked_file(archives[filename])
        digest = sha256(archive)
        checksum = _checked_file(archive.with_suffix('.zip.sha256'))
        # Check both digest and filename, not just split()[0]. A checksum for
        # another bundle must not accompany a newly named/replaced artifact.
        expected_checksum = f'{digest}  {archive.name}'
        if checksum.read_text(encoding='utf-8').strip() != expected_checksum:
            raise RuntimeError(f'Archive checksum/name mismatch: {archive.name}')
        with zipfile.ZipFile(archive) as bundle:
            names = bundle.namelist()
            if len(names) != len(set(names)) or (platform == 'windows' and len(names) != len({name.casefold() for name in names})):
                raise RuntimeError(f'Duplicate ZIP entry: {archive.name}')
            for entry in bundle.infolist():
                # ZipInfo.filename is already sanitized: on Windows a raw
                # backslash becomes '/', and every platform truncates at NUL.
                # Validate the archive's original spelling, not that lossy view.
                name = entry.orig_filename
                relative = PurePosixPath(name)
                if (name != entry.filename or '\x00' in name
                        or relative.is_absolute() or '..' in relative.parts or '\\' in name or ':' in name
                        or not name.startswith(archive.stem + '/') or str(relative) != name.rstrip('/')
                        or stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1):
                    raise RuntimeError(f'Unsafe or unexpected ZIP entry: {name}')
            payload_root = archive.stem + ('/Video Subtitle Extractor.app/Contents/Resources' if platform == 'macos' else '')
            manifest_path = payload_root + '/bundle-manifest.json'
            manifests = [name for name in names if name.endswith('/bundle-manifest.json')]
            if manifests != [manifest_path] or bundle.getinfo(manifest_path).file_size > 16 * 1024 * 1024:
                raise RuntimeError(f'Missing unique expected bundle manifest: {archive.name}')
            manifest = _read_json(bundle.read(manifest_path), archive.name)
        identity = {'commit': sha, 'version': version, 'platform': platform, 'arch': arch,
                    'ocr_backend': engine, 'accelerator': accelerator, 'build_profile': profile}
        if any(manifest.get(key) != value for key, value in identity.items()):
            raise RuntimeError(f'Bundle identity does not match commit/version/profile/filename: {archive.name}')
        if manifest.get('format') != 2 or manifest.get('python') != '3.12':
            raise RuntimeError(f'Unsupported self-contained bundle format/runtime: {archive.name}')
        receipt = archive.with_suffix('.zip.acceptance.json')
        validate_acceptance_receipt(receipt, archive, digest, manifest)
        assets.extend(map(str, (archive, checksum, receipt)))
    return assets


def validate_tag(tag: str, version: str) -> None:
    # Allow a NEW preview suffix for a version whose immutable base tag exists.
    if not re.fullmatch(r"v?" + re.escape(version) + r"(?:-[0-9A-Za-z][0-9A-Za-z.-]*)?", tag):
        raise RuntimeError(f"Tag must match version {version}, optionally v-prefixed or with a new preview suffix")
    if tag.endswith(".") or ".." in tag or "@{" in tag:
        raise RuntimeError("Invalid release tag")


def remote_tag_commit(output: str, tag: str) -> str | None:
    refs = {}
    for line in output.splitlines():
        sha, ref = line.split(maxsplit=1)
        refs[ref] = sha
    return refs.get(f"refs/tags/{tag}^{{}}", refs.get(f"refs/tags/{tag}"))


def assert_target(tag: str, expected_sha: str, actual_sha: str, remote_sha: str | None, release_exists: bool) -> None:
    if not re.fullmatch(r"[a-f0-9]{40}", expected_sha) or actual_sha != expected_sha:
        raise RuntimeError("Release SHA must be the exact checked-out 40-character commit")
    if remote_sha is not None and remote_sha != expected_sha:
        raise RuntimeError(f"Refusing to move immutable tag {tag}: existing {remote_sha}, requested {expected_sha}")
    if release_exists:
        raise RuntimeError(f"Release {tag} already exists; this workflow never overwrites releases or assets")


def check_remote_release(tag: str) -> bool:
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GH_TOKEN", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not token:
        raise RuntimeError("GITHUB_REPOSITORY and GH_TOKEN are required for a release check")
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    if not api.startswith("https://"):
        raise RuntimeError("GitHub API must use HTTPS")
    request = Request(f"{api}/repos/{repository}/releases/tags/{quote(tag, safe='')}", headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urlopen(request, timeout=30) as response:
            json.load(response)
        return True
    except HTTPError as exc:
        if exc.code == 404:
            return False
        raise RuntimeError(f"Release lookup failed with HTTP {exc.code}; refusing publication") from exc


def preflight(tag: str, sha: str) -> None:
    validate_tag(tag, read_version())
    actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    refs = subprocess.check_output(["git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"], cwd=ROOT, text=True)
    remote_sha = remote_tag_commit(refs, tag)
    assert_target(tag, sha, actual, remote_sha, False)
    assert_target(tag, sha, actual, remote_sha, check_remote_release(tag))
    print(f"Verified new release target {tag} at {sha}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="Required only for immutable-target preflight/publication")
    parser.add_argument("--sha", required=True)
    parser.add_argument("--publish", choices=("draft", "prerelease", "release"))
    parser.add_argument("--validate-assets", action="store_true", help="Offline, build-only final candidate validation; never publishes")
    parser.add_argument("--matrix", choices=("restored-candidates", "full"), default="restored-candidates")
    parser.add_argument("--assets", type=Path, default=Path("dist"))
    args = parser.parse_args()
    if args.validate_assets and args.publish:
        parser.error("--validate-assets is build-only and cannot be combined with --publish")
    if not args.validate_assets and not args.tag:
        parser.error("--tag is required for preflight/publication")
    if args.validate_assets:
        actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        assert_target('', args.sha, actual, None, False)
        assets = validate_assets(args.assets, args.sha, read_version(), matrix=args.matrix)
        print(f"Validated {len(assets) // 3} exact candidates at {args.sha}; build-only, GPU inference not run")
        return 0
    if not args.publish:
        preflight(args.tag, args.sha)
        return 0
    # Publication requires an explicit complete-current-matrix choice. The
    # approved CUDA 10.2 legacy migration is not a claim of new model support;
    # any new unresolved current-profile blocker still fails closed.
    assert_complete_release_matrix(args.matrix)
    assets = validate_assets(args.assets, args.sha, read_version(), matrix=args.matrix)
    preflight(args.tag, args.sha)
    # An ordinary non-force push is race-safe and refuses a different existing tag.
    subprocess.run(["git", "push", "origin", f"{args.sha}:refs/tags/{args.tag}"], cwd=ROOT, check=True)
    command = ["gh", "release", "create", args.tag, "--verify-tag", "--title", f"Video Subtitle Extractor {args.tag}", "--generate-notes"]
    if args.publish == "draft":
        command.append("--draft")
    elif args.publish == "prerelease":
        command.append("--prerelease")
    command.extend(assets)
    subprocess.run(command, cwd=ROOT, check=True)
    print(f"Created {args.publish} {args.tag} at {args.sha}; no existing tag/release was replaced")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, subprocess.SubprocessError) as exc:
        print(f"Release blocked: {exc}", file=sys.stderr)
        raise SystemExit(1)
