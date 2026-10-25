"""Auditable inference dependency closure without altering upstream wheels.

Two broad meta-package edges are explicitly substituted: Fluent Widgets uses
Qt Essentials, and macOS Frameless Window uses PyObjC Core/Cocoa/Quartz. Every
other active requirement (including OCR extras) must resolve compatibly.
"""
from __future__ import annotations

from email.parser import BytesParser
from importlib.metadata import distributions
from pathlib import Path
import json
import shutil
import zipfile

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

QT_EXCEPTION = ('pyside6-fluent-widgets', 'pyside6')
META_SUBSTITUTIONS = {QT_EXCEPTION: 'pyside6-essentials', ('pysidesix-frameless-window', 'pyobjc'): 'pyobjc-core'}


def active(requirement, extras):
    return requirement.marker is None or any(
        requirement.marker.evaluate({'extra': extra}) for extra in {'', *extras}
    )


def resolve(records, roots):
    selected = {}
    pending = [(None, Requirement(root)) for root in roots if active(Requirement(root), set())]
    while pending:
        parent, requirement = pending.pop()
        name = canonicalize_name(requirement.name)
        if (parent, name) in META_SUBSTITUTIONS:
            provider = META_SUBSTITUTIONS[parent, name]
            replacement = records.get(provider)
            if not replacement or not requirement.specifier.contains(replacement['version'], prereleases=True):
                raise RuntimeError(f'Essentials/native provider cannot satisfy version required by {parent}: {requirement}')
            if name == 'pyobjc':
                for package in ('pyobjc-core', 'pyobjc-framework-cocoa', 'pyobjc-framework-quartz'):
                    pending.append((parent, Requirement(f'{package}=={replacement["version"]}')))
            continue
        if name not in records:
            raise RuntimeError(f'Missing runtime dependency: {parent or "root"} requires {requirement}')
        record = records[name]
        if not requirement.specifier.contains(record['version'], prereleases=True):
            raise RuntimeError(f'Incompatible runtime dependency: {requirement}, installed {record["version"]}')
        extras = set(requirement.extras) | selected.get(name, set())
        if name in selected and extras == selected[name]:
            continue
        selected[name] = extras
        for raw in record['requires']:
            dependency = Requirement(raw)
            if active(dependency, extras):
                pending.append((name, dependency))
    # A real Essentials/Shiboken distribution is mandatory, not a fake meta wheel.
    for name in ('pyside6-essentials', 'shiboken6'):
        if name not in selected:
            raise RuntimeError(f'Inference profile requires {name}')
    if records['pyside6-essentials']['version'] != records['shiboken6']['version']:
        raise RuntimeError('PySide6-Essentials and Shiboken6 versions must match')
    return selected


def runtime_wheels(wheelhouse, requirements):
    records = {}
    for path in sorted(Path(wheelhouse).glob('*.whl')):
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist() if name.endswith('.dist-info/METADATA') and name.count('/') == 1]
            if len(names) != 1:
                raise RuntimeError(f'Invalid wheel metadata: {path.name}')
            metadata = BytesParser().parsebytes(archive.read(names[0]))
        name = canonicalize_name(metadata['Name'])
        if name in records:
            raise RuntimeError(f'Duplicate wheel for {name}')
        records[name] = {'version': metadata['Version'], 'requires': metadata.get_all('Requires-Dist', []), 'path': path}
    roots = [line.strip() for line in Path(requirements).read_text().splitlines() if line.strip() and not line.lstrip().startswith('#')]
    selected = resolve(records, roots)
    return [records[name]['path'] for name in sorted(selected)], roots


def audit_installed(roots):
    records = {canonicalize_name(d.metadata['Name']): {'version': d.version, 'requires': d.requires or []} for d in distributions()}
    selected = resolve(records, roots)
    # Audit every installed distribution as well, not only the root closure.
    for name, record in records.items():
        for raw in record['requires']:
            requirement = Requirement(raw)
            dependency = canonicalize_name(requirement.name)
            if not active(requirement, selected.get(name, set())):
                continue
            if (name, dependency) in META_SUBSTITUTIONS:
                provider = META_SUBSTITUTIONS[name, dependency]
                if provider not in records or not requirement.specifier.contains(records[provider]['version'], prereleases=True):
                    raise RuntimeError(f'Native provider cannot satisfy {requirement}')
                continue
            if dependency not in records or not requirement.specifier.contains(records[dependency]['version'], prereleases=True):
                raise RuntimeError(f'Invalid installed dependency: {name} requires {requirement}')
    import cv2
    for attribute in ('VideoCapture', 'VideoWriter', 'cvtColor', 'findContours', 'minAreaRect'):
        if not hasattr(cv2, attribute):
            raise RuntimeError(f'Missing OpenCV inference/video API: {attribute}')
    for module in ('QtCore', 'QtGui', 'QtWidgets', 'QtSvg', 'QtSvgWidgets', 'QtXml'):
        __import__(f'PySide6.{module}')
    import sys
    if sys.platform == 'darwin':
        import Cocoa, objc, Quartz.CoreGraphics
    print('Inference dependencies verified;  explicit PySide6 meta-package substitution: Essentials (no WebEngine/Addons/PDF); macOS PyObjC limited to Core/Cocoa/Quartz.')


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--wheelhouse', type=Path)
    parser.add_argument('--requirements', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.wheelhouse and args.requirements and args.output:
        wheels, roots = runtime_wheels(args.wheelhouse, args.requirements)
        args.output.mkdir(parents=True, exist_ok=False)
        for wheel in wheels:
            shutil.copy2(wheel, args.output / wheel.name)
        (args.output / 'runtime-manifest.json').write_text(json.dumps({'runtime_requirements': roots}, indent=2) + '\n')
    elif args.manifest:
        audit_installed(json.loads(args.manifest.read_text())['runtime_requirements'])
    else:
        parser.error('Specify --manifest, or --wheelhouse/--requirements/--output')
