#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Audit the native-restored tree through its original launcher; never extract ZIPs."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-source', required=True, type=Path)
    parser.add_argument('--baseline-commit', required=True)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--runner', choices=('windows-2022', 'windows-2025'), required=True)
    args = parser.parse_args(argv)
    baseline = args.baseline_source.resolve(strict=True)
    if subprocess.check_output(['git', '-C', str(baseline), 'rev-parse', 'HEAD'], text=True).strip() != args.baseline_commit:
        raise RuntimeError('Model-fixture/audit source checkout is not the exact accepted baseline')
    if subprocess.check_output(['git', '-C', str(baseline), 'status', '--porcelain'], text=True).strip():
        raise RuntimeError('Baseline source checkout is dirty')
    # Separate process: all audit helpers come from the accepted source revision,
    # not a mixture with the restorer branch's Python module cache.
    sys.path.insert(0, str(baseline))
    from scripts import verify_portable_environment as portable
    from scripts import check_paddle_models
    from scripts import accept_release_archive as acceptance
    from scripts.portable_python import launcher_command
    root = args.root.resolve(strict=True)
    manifest = json.loads((root / 'bundle-manifest.json').read_bytes())
    if (manifest['commit'] != args.baseline_commit or manifest['platform'] != 'windows'
            or manifest['arch'] != 'x64' or manifest['ocr_backend'] != 'paddle'):
        raise RuntimeError('Restored launcher identity differs from accepted baseline')
    fixtures = baseline / 'native/ocr/tests/evidence'
    fixture_hashes = {p.relative_to(fixtures).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in fixtures.rglob('*') if p.is_file()}
    if not fixture_hashes or any(p.is_symlink() for p in fixtures.rglob('*')):
        raise RuntimeError('Exact plain baseline model fixtures required')
    golden = fixtures.parent / 'goldens/smoke.json'
    golden_sha = hashlib.sha256(golden.read_bytes()).hexdigest()
    if (golden_sha != check_paddle_models.GOLDEN_SHA256
            or fixture_hashes.get('fixture-manifest.json') != check_paddle_models.FIXTURE_MANIFEST_SHA256):
        raise RuntimeError('Baseline golden/fixture-manifest bytes differ from the original frozen pins')
    args.output.mkdir(parents=True, exist_ok=False)
    report = {'scope': 'restored-baseline-launcher-explicit-cpu-not-gpu-certification', 'result': 'failed',
              'commit': args.baseline_commit, 'root': str(root), 'fixtures_sha256': fixture_hashes, 'golden_sha256': golden_sha,
              'gpu_inference_verified': False, 'archive_extraction_used': False, 'scenarios': []}
    # Do not expose CI download credentials to application children.
    allowed = {'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP', 'NUMBER_OF_PROCESSORS',
               'PROCESSOR_ARCHITECTURE', 'PROGRAMFILES', 'PROGRAMFILES(X86)', 'PROGRAMDATA',
               'SYSTEMDRIVE', 'USERPROFILE', 'USERNAME', 'USERDOMAIN', 'OS'}
    inherited = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    inherited.update(VSE_WINDOWS_RUNNER_LABEL=args.runner, PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1')
    try:
        for scenario in portable.SCENARIOS:
            evidence = args.output / scenario; evidence.mkdir()
            case = {'scenario': scenario, 'result': 'failed'}; report['scenarios'].append(case)
            try:
                env, fixture = portable.fixture_environment(args.output / ('fixture-' + scenario), scenario == 'contaminated-host-environment', inherited)
                portable.write_json(evidence / 'fixture.json', fixture)
                portable.verify_payload(root, manifest)
                audit = evidence / 'dependency-isolation'
                portable.audit_environment(root, audit)
                portable.audit_windows_executables(root, root / manifest['runtime_python'], audit)
                command = launcher_command(root, 'windows', ['--verify', '--results-dir', str(evidence),
                    '--runtime-audit-dir', str(audit), '--paddle-model-fixtures', str(fixtures)])
                case['command'] = command
                with (evidence / 'launcher.log').open('wb') as log:
                    result = subprocess.run(command, cwd=fixture['cwd'], env=env, stdout=log, stderr=subprocess.STDOUT, timeout=5400)
                case['returncode'] = result.returncode; result.check_returncode()
                case['audit'] = portable.summarize_reports(root, audit, ('PySide6.QtWidgets', 'cv2', 'qfluentwidgets', 'paddle'), (), require_isolated=True)
                portable.check_environment_evidence(evidence, fixture, 'paddle', root)
                # These authoritative baseline validators require real CPU mode/issue
                # reports; a launcher exit code alone does not constitute acceptance.
                acceptance._workload_evidence(acceptance.Evidence(), evidence, '', 'restored')
                model_report = json.loads((evidence / 'paddle-models/report.json').read_bytes())
                check_paddle_models.validate_report(model_report, identity={key: manifest[key] for key in ('commit', 'version', 'build_profile')})
                portable.verify_payload(root, manifest)
                case['result'] = 'passed'
            except Exception as error:
                case['error'] = f'{type(error).__name__}: {error}'
            finally:
                portable.write_json(evidence / 'scenario-report.json', case)
        if any(case['result'] != 'passed' for case in report['scenarios']):
            raise RuntimeError('One or more restored-launcher scenarios failed; all available case evidence is retained')
        if fixture_hashes != {p.relative_to(fixtures).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in fixtures.rglob('*') if p.is_file()}:
            raise RuntimeError('Frozen source fixtures changed during acceptance')
        if (hashlib.sha256(golden.read_bytes()).hexdigest() != golden_sha
                or subprocess.check_output(['git', '-C', str(baseline), 'status', '--porcelain'], text=True).strip()):
            raise RuntimeError('Baseline audit source/golden changed during acceptance')
        report['audit_source_sha256'] = {name: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
            for name, module in sorted(sys.modules.items()) if name.startswith('scripts.') and getattr(module, '__file__', None)}
        report['result'] = 'passed'
    except Exception as error:
        report['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        (args.output / 'restored-launcher-report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return 0


if __name__ == '__main__': raise SystemExit(main())
