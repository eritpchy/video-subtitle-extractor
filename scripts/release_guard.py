"""Preview/publish a new release without ever replacing a tag or release.

Uses the dispatch checkout's exact SHA. Network/API failures fail closed.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import zipfile
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

try:
    from .package_release import ROOT, read_version
except ImportError:
    from package_release import ROOT, read_version


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
    parser.add_argument("--tag", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--publish", choices=("draft", "prerelease", "release"))
    parser.add_argument("--assets", type=Path, default=Path("dist"))
    args = parser.parse_args()
    preflight(args.tag, args.sha)
    if not args.publish:
        return 0
    archives = sorted(args.assets.glob("*.zip"))
    expected = {"linux-x64", "windows-x64", "macos-arm64"}
    if len(archives) != 3 or any(not any(f"-{target}-cpu-python312.zip" in path.name for path in archives) for target in expected):
        raise RuntimeError("Expected exactly Linux x64, Windows x64 and macOS arm64 CPU bundles")
    try:
        from .package_release import sha256
    except ImportError:
        from package_release import sha256
    assets = []
    for archive in archives:
        checksum = archive.with_suffix(".zip.sha256")
        if checksum.read_text(encoding="utf-8").split()[0] != sha256(archive):
            raise RuntimeError(f"Archive checksum mismatch: {archive.name}")
        with zipfile.ZipFile(archive) as bundle:
            manifest_names = [name for name in bundle.namelist() if name.endswith("/bundle-manifest.json")]
            if len(manifest_names) != 1:
                raise RuntimeError(f"Missing unique bundle manifest: {archive.name}")
            manifest = json.loads(bundle.read(manifest_names[0]))
        if manifest.get("commit") != args.sha or manifest.get("version") != read_version():
            raise RuntimeError(f"Bundle does not match release commit/version: {archive.name}")
        target = f"{manifest.get('platform')}-{manifest.get('arch')}"
        if target not in expected or not archive.name.endswith(f"-{target}-cpu-python312.zip"):
            raise RuntimeError(f"Bundle platform does not match its filename: {archive.name}")
        assets.extend([str(archive), str(checksum)])
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
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Release blocked: {exc}", file=sys.stderr)
        raise SystemExit(1)
