#!/usr/bin/env python3
"""Fail-closed acceptance of a final Linux ZIP in stock Ubuntu, without installs.

Docker/Xvfb/xauth are BUILD/CI host tools, not customer prerequisites. No missing
prerequisite is a skip. The tested container has no network, host Python or Qt.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import subprocess
import sys
import tempfile

try:
    from .bundle_linux_runtime_libs import BASE, sha256
except ImportError:
    from bundle_linux_runtime_libs import BASE, sha256

IMAGE = 'ubuntu:24.04'
BASELINE_CHECK = '''set -eu
for name in python python3 pip pip3; do
  if command -v "$name" >/dev/null 2>&1; then echo "Unexpected host runtime: $name" >&2; exit 1; fi
done
for pattern in /usr/bin/python* /usr/local/bin/python* /usr/lib/libQt* /usr/lib/*/libQt* /usr/local/lib/libQt*; do
  if [ -e "$pattern" ]; then echo "Unexpected host Python/Qt: $pattern" >&2; exit 1; fi
done
printf 'Stock OS has no Python or Qt runtime\\n'
'''


def runtime_relative(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or '\\' in value or str(path) != value or not value.startswith('runtime/'):
        raise ValueError(f'Invalid bundled interpreter path: {value!r}')
    return value


def docker_base(bundle: Path, evidence: Path, image: str) -> list[str]:
    # --mount has comma-delimited options; reject ambiguous host paths.
    for path in (bundle, evidence):
        if ',' in str(path) or '\n' in str(path):
            raise ValueError('Docker mount paths cannot contain commas/newlines')
    return ['docker', 'run', '--rm', '--network', 'none', '--read-only',
            '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
            '--user', f'{os.getuid()}:{os.getgid()}', '--shm-size', '256m',
            '--tmpfs', '/tmp:rw,nosuid,nodev,size=512m',
            '--mount', f'type=bind,src={bundle},dst=/bundle,readonly',
            '--mount', f'type=bind,src={evidence},dst=/results',
            '--workdir', '/bundle']


def container_command(bundle: Path, evidence: Path, image: str, python: str,
                      arguments: list[str], case: str, *, display: str | None = None,
                      xauthority: Path | None = None) -> list[str]:
    runtime_relative(python)
    if not re.fullmatch(r'[a-z0-9-]+', case):
        raise ValueError('Invalid evidence case name')
    command = docker_base(bundle, evidence, image)
    environment = {
        'HOME': '/results/home', 'PATH': '/usr/bin:/bin',
        'LANG': 'C.UTF-8', 'USER': 'vse', 'LOGNAME': 'vse', 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1',
        'LD_LIBRARY_PATH': '/bundle/native-libs', 'LD_DEBUG': 'libs,files',
        'LD_DEBUG_OUTPUT': f'/results/loader/{case}',
        'QT_QPA_PLATFORM': 'offscreen', 'VSE_OFFLINE': '1',
        'PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK': 'True',
        'PADDLE_PDX_CACHE_HOME': '/results/paddlex',
        'XDG_CONFIG_HOME': '/results/config', 'XDG_CACHE_HOME': '/results/cache',
        'VSE_CONFIG_FILE': '/results/user/config.json', 'VSE_WORK_DIR': '/results/work',
        'VSE_SMOKE_SCREENSHOT_DIR': f'/results/{case}-screenshots',
    }
    if display is not None:
        if not re.fullmatch(r':\d+(?:\.\d+)?', display) or xauthority is None or not xauthority.is_file():
            raise ValueError('XCB requires a local Xvfb display and existing Xauthority cookie')
        if ',' in str(xauthority) or '\n' in str(xauthority):
            raise ValueError('Invalid Xauthority mount path')
        command += ['--hostname', socket.gethostname(), '--mount', 'type=bind,src=/tmp/.X11-unix,dst=/tmp/.X11-unix,readonly',
                    '--mount', f'type=bind,src={xauthority},dst=/xauthority,readonly']
        environment.update(DISPLAY=display, XAUTHORITY='/xauthority', QT_QPA_PLATFORM='xcb', QT_X11_NO_MITSHM='1')
    command += [image, '/usr/bin/env', '-i']
    command += [f'{key}={value}' for key, value in environment.items()]
    return command + [f'/bundle/{python}', *arguments]


def loaded_library_paths(text: str) -> set[str]:
    """Only actual initializer/finalizer events, never failed 'trying file' paths."""
    paths = set()
    for line in text.splitlines():
        match = re.search(r'calling (?:init|fini):\s*(/.*?)\s*$', line)
        if match:
            paths.add(re.sub(r'\s+\[\d+\]$', '', match[1]))
    return paths


def audit_loader_logs(evidence: Path, cases: tuple[str, ...]) -> dict:
    observed, violations = set(), set()
    counts = {}
    for case in cases:
        files = sorted((evidence / 'loader').glob(case + '.*'))
        paths = set()
        for file in files:
            paths.update(loaded_library_paths(file.read_text(errors='replace')))
        if not files or not any(p.startswith('/bundle/') for p in paths):
            raise RuntimeError(f'Missing actual bundled-library loader evidence for {case}')
        counts[case] = {'process_logs': len(files), 'loaded_libraries': len(paths)}
        observed.update(paths)
    for value in observed:
        path = PurePosixPath(value)
        if '..' in path.parts:
            # Loader output can retain lexical $ORIGIN paths; normalize safely.
            import posixpath
            path = PurePosixPath(posixpath.normpath(value))
        if path.is_relative_to('/bundle'):
            continue
        if BASE.fullmatch(path.name) and any(path.is_relative_to(base) for base in ('/lib', '/lib64', '/usr/lib', '/usr/lib64')):
            continue
        violations.add(value)
    report = {'result': 'failed' if violations else 'passed', 'cases': counts,
              'loaded_paths': sorted(observed), 'external_nonbase_libraries': sorted(violations)}
    (evidence / 'loader-audit.json').write_text(json.dumps(report, indent=2) + '\n')
    if violations:
        raise RuntimeError('Non-base libraries loaded outside bundle: ' + ', '.join(sorted(violations)))
    return report


def validate_workload_evidence(evidence: Path) -> None:
    screenshots = {p.name for p in (evidence / 'xcb-gui-screenshots').glob('*.png')}
    expected = {f'{theme}-{page}.png' for theme in ('light', 'dark') for page in ('home', 'timeline', 'settings')}
    if not expected <= screenshots:
        raise RuntimeError('XCB GUI screenshots incomplete: ' + ', '.join(sorted(expected - screenshots)))
    if 'GUI_SMOKE_OK' not in (evidence / 'xcb-gui.log').read_text(errors='replace'):
        raise RuntimeError('XCB GUI smoke success marker missing')
    ordinary = json.loads((evidence / 'acceptance/bundle-e2e/report.json').read_text())
    if ordinary.get('result') != 'passed':
        raise RuntimeError('Ordinary OCR E2E did not pass')
    issues = json.loads((evidence / 'acceptance/bundle-issues/report.json').read_text())
    results = {item['case']: item['result'] for item in issues}
    if results != {'multiline': 'passed', 'av1': 'passed', 'timeline': 'passed'}:
        raise RuntimeError('Issue E2E evidence incomplete or failed')


def execute(command: list[str], logfile: Path, timeout: int) -> None:
    with logfile.open('w', encoding='utf-8') as stream:
        stream.write('COMMAND ' + json.dumps(command) + '\n')
        stream.flush()
        subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, check=True, timeout=timeout)


def xcb_child(config_path: Path) -> None:
    config = json.loads(config_path.read_text())
    evidence, bundle = Path(config['evidence']), Path(config['bundle'])
    command = container_command(bundle, evidence, config['image'], config['python'],
                                ['gui.py', '--smoke-test'], 'xcb-gui',
                                display=os.environ.get('DISPLAY'),
                                xauthority=Path(os.environ['XAUTHORITY']))
    if not os.environ.get('DISPLAY'):
        raise RuntimeError('xvfb-run did not supply DISPLAY')
    execute(command, evidence / 'xcb-gui.log', 240)


def verify_clean_bundle(archive: Path, output: Path) -> dict:
    for tool in ('docker', 'xvfb-run', 'xauth'):
        if shutil.which(tool) is None:
            raise RuntimeError(f'Required CI tool unavailable: {tool}; clean acceptance cannot be skipped')
    output = output.resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError('Evidence directory must be new or empty (no stale acceptance evidence)')
    output.mkdir(parents=True, exist_ok=True)
    for directory in ('home', 'config', 'cache', 'work', 'user', 'paddlex', 'loader'):
        (output / directory).mkdir()
    archive = archive.resolve(strict=True)
    report = {'result': 'running', 'archive': str(archive), 'archive_sha256': sha256(archive), 'image_tag': IMAGE}
    try:
        expected = archive.with_suffix('.zip.sha256').read_text().split()[0]
        if report['archive_sha256'] != expected:
            raise RuntimeError('Final archive checksum mismatch')
        try:
            from .package_release import extract_checked
            from .bundle_bootstrap import verify_payload
        except ImportError:
            from package_release import extract_checked
            from bundle_bootstrap import verify_payload
        # Docker daemon must be available; docker image pull failure is fatal.
        execute(['docker', 'version'], output / 'docker-version.log', 60)
        execute(['docker', 'pull', IMAGE], output / 'docker-pull.log', 600)
        image = subprocess.check_output(['docker', 'image', 'inspect', '--format', '{{.Id}}', IMAGE], text=True).strip()
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', image):
            raise RuntimeError('Docker did not provide an immutable image ID')
        report['image_id'] = image
        with tempfile.TemporaryDirectory(prefix='vse-clean-linux-') as temporary:
            root = extract_checked(archive, Path(temporary))
            manifest = json.loads((root / 'bundle-manifest.json').read_text())
            if manifest.get('format') != 2 or manifest.get('platform') != 'linux':
                raise ValueError('Expected format 2 self-contained Linux bundle')
            python = runtime_relative(manifest['runtime_python'])
            verify_payload(root, manifest)
            report['bundle_commit'] = manifest.get('commit')
            report['runtime_python'] = python
            for name in ('bundle-manifest.json', 'native-libs/manifest.json'):
                target = output / ('bundle-manifest.json' if name.startswith('bundle') else 'native-libraries.json')
                shutil.copyfile(root / name, target)
            execute(docker_base(root, output, image) + [image, '/bin/sh', '-ec', BASELINE_CHECK], output / 'base-image-check.log', 60)
            cases = [('runtime', ['scripts/check_runtime.py'], 240),
                     ('offline-e2e', ['bootstrap.py', '--verify', '--results-dir', '/results/acceptance'], 4800)]
            for case, arguments, timeout in cases:
                execute(container_command(root, output, image, python, arguments, case), output / f'{case}.log', timeout)
            config = output / 'xcb-command.json'
            config.write_text(json.dumps({'bundle': str(root), 'evidence': str(output), 'image': image, 'python': python}))
            execute(['xvfb-run', '-a', '-s', '-screen 0 1920x1080x24 -nolisten tcp',
                     sys.executable, str(Path(__file__).resolve()), '--xcb-child', str(config)], output / 'xvfb-host.log', 300)
            report['loader_audit'] = audit_loader_logs(output, ('runtime', 'offline-e2e', 'xcb-gui'))
            validate_workload_evidence(output)
            report['result'] = 'passed'
    except Exception as error:
        report.update(result='failed', error=str(error))
        raise
    finally:
        (output / 'clean-linux-report.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--xcb-child', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.xcb_child:
        xcb_child(args.xcb_child)
    else:
        if args.archive is None or args.output is None:
            parser.error('--archive and --output are required')
        print(json.dumps(verify_clean_bundle(args.archive, args.output), indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
