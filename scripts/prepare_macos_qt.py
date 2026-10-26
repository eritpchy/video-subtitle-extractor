#!/usr/bin/env python3
"""Explicitly prepare the reviewed Cocoa overlay in this user's isolated venv.

Close VSE and any other Qt application using this venv first. No system package,
OCR worker, compiler, permission setting or published asset is modified here.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import macos_qt_overlay as overlay


def native_load_probe(purelib: Path, *, policy: dict) -> dict:
    """Check our native loaded images, not an external AX client or full GUI."""
    code = '''import ctypes,json
from pathlib import Path
from PySide6 import QtCore,QtGui,QtWidgets
app=QtWidgets.QApplication([])
assert app.platformName()=='cocoa'
QtGui.QAccessible.setActive(True)
app.processEvents()
lib=ctypes.CDLL(None); lib._dyld_image_count.restype=ctypes.c_uint32
lib._dyld_get_image_name.argtypes=[ctypes.c_uint32];lib._dyld_get_image_name.restype=ctypes.c_char_p
images=[lib._dyld_get_image_name(i).decode() for i in range(lib._dyld_image_count())]
images=[str(Path(n).resolve()) for n in images if any(x in n for x in ('libqcocoa.dylib','QtCore.framework/','QtGui.framework/'))]
print('VSE_QT_OVERLAY_LOAD='+json.dumps({'qt':QtCore.qVersion(),'backend':app.platformName(),'accessibility_active':QtGui.QAccessible.isActive(),'images':images}))
'''
    environment = {k: v for k, v in os.environ.items() if not k.startswith(('PYTHON', 'DYLD_', 'QT_'))}
    environment['QT_QPA_PLATFORM'] = 'cocoa'
    result = subprocess.run([sys.executable, '-I', '-B', '-c', code], text=True,
                            capture_output=True, timeout=60, env=environment)
    records = [json.loads(line.split('=', 1)[1]) for line in result.stdout.splitlines()
               if line.startswith('VSE_QT_OVERLAY_LOAD=')]
    if result.returncode or len(records) != 1:
        raise RuntimeError('Native Qt load probe failed; no application/AX acceptance is claimed: '
                           + result.stderr[-2000:])
    observed = records[0]
    expected = {str((purelib / overlay.PLUGIN).resolve())}
    expected.update(str((purelib / f'PySide6/Qt/lib/{name}.framework/Versions/A/{name}').resolve())
                    for name in ('QtCore', 'QtGui'))
    if (observed.get('qt') != policy['qt_version'] or observed.get('backend') != 'cocoa'
            or observed.get('accessibility_active') is not True
            or set(observed.get('images', ())) != expected):
        raise RuntimeError('Qt did not load the exact prepared venv with accessibility active')
    return {**observed, 'external_ax_client_tested': False,
            'scope': 'Native load/public activation only, not external AX or VSE GUI acceptance'}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Explicitly replace only the reviewed venv Cocoa plugin')
    parser.add_argument('--archive', type=Path, help='Use local bytes with the same pinned hash (prepublication/offline testing)')
    parser.add_argument('--candidate-testing', action='store_true', help='Explicit prepublication testing, requires --archive; not production approval')
    args = parser.parse_args(argv)
    policy = overlay.load_policy()
    overlay._host(policy)
    _, purelib = overlay.inspect_current_venv()
    identity = overlay.inspect_target(purelib, policy=policy)
    if not args.apply:
        print(json.dumps({'status': 'read-only', **identity,
                          'production_integration_allowed': policy['production_integration_allowed']}, indent=2))
        print('No files changed. Close Qt GUI applications, then use --apply to explicitly prepare this venv.')
        return 0
    if args.candidate_testing and not args.archive:
        parser.error('--candidate-testing requires an explicit reviewed local --archive')
    overlay._admission(policy, args.candidate_testing)
    with tempfile.TemporaryDirectory(prefix='vse-qt-overlay-') as directory:
        temporary = Path(directory)
        archive = args.archive or overlay.fetch_archive(temporary / policy['resource']['name'], policy=policy)
        resource = overlay.prepare_resource(archive, temporary / 'resource', policy=policy)
        receipt = overlay.apply_to_venv(resource, candidate_testing=args.candidate_testing)
        try:
            proof = native_load_probe(purelib, policy=policy)
        except Exception:
            # Preserve an honest receipt of what was applied. Do not reinterpret
            # load failure as success or silently patch/disable accessibility.
            receipt['native_load_probe_passed'] = False
            overlay._atomic_json(purelib / overlay.RECEIPT, receipt)
            raise
        receipt['native_load_probe_passed'] = True
        receipt['native_load_probe'] = proof
        overlay._atomic_json(purelib / overlay.RECEIPT, receipt)
        overlay.verify_overlay(purelib, receipt, policy=policy)
    print(json.dumps(receipt, indent=2))
    print('Prepared only this venv. External AX, VSE GUI, OCR and release acceptance are separate checks.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
