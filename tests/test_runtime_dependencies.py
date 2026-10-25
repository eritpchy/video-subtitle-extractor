"""Inference dependency selection must be strict except one Qt meta requirement."""
from pathlib import Path
import unittest
from unittest.mock import patch
from scripts.runtime_dependencies import active, resolve
from packaging.requirements import Requirement


def records():
    return {
        'pyside6-essentials': {'version': '6.12.0', 'requires': ['shiboken6==6.12.0']},
        'shiboken6': {'version': '6.12.0', 'requires': []},
        'pyside6-fluent-widgets': {'version': '1.11.3', 'requires': ['PySide6', 'darkdetect']},
        'darkdetect': {'version': '0.8.0', 'requires': []},
    }


class RuntimeDependencyTests(unittest.TestCase):
    def test_only_meta_is_substituted(self):
        selected = resolve(records(), ['pyside6-essentials~=6.11', 'pyside6-fluent-widgets'])
        self.assertEqual(set(selected), set(records()))
        self.assertNotIn('pyside6', selected)

    def test_meta_version_requirement_still_enforced(self):
        data = records()
        data['pyside6-fluent-widgets']['requires'] = ['PySide6>=7']
        with self.assertRaisesRegex(RuntimeError, 'Essentials/native provider cannot satisfy'):
            resolve(data, ['pyside6-essentials', 'pyside6-fluent-widgets'])

    def test_missing_real_dependency_is_not_ignored(self):
        data = records()
        del data['darkdetect']
        with self.assertRaisesRegex(RuntimeError, 'Missing runtime dependency'):
            resolve(data, ['pyside6-essentials', 'pyside6-fluent-widgets'])

    def test_exception_is_limited_to_fluent_widgets(self):
        data = records()
        data['other'] = {'version': '1', 'requires': ['PySide6']}
        with self.assertRaisesRegex(RuntimeError, 'Missing runtime dependency'):
            resolve(data, ['pyside6-essentials', 'other'])

    def test_real_qt_provider_is_mandatory(self):
        with self.assertRaisesRegex(RuntimeError, 'requires pyside6-essentials'):
            resolve(records(), ['pyside6-fluent-widgets'])

    def test_incompatible_qt_versions_rejected(self):
        data = records()
        data['shiboken6']['version'] = '6.11.0'
        with self.assertRaisesRegex(RuntimeError, 'Incompatible'):
            resolve(data, ['pyside6-essentials'])

    def test_requested_extras_are_enforced(self):
        data = records()
        data['ocr'] = {'version': '1', 'requires': ['missing; extra == "ocr-core"']}
        resolve(data, ['pyside6-essentials', 'ocr'])
        with self.assertRaisesRegex(RuntimeError, 'Missing'):
            resolve(data, ['pyside6-essentials', 'ocr[ocr-core]'])

    def test_extra_union_is_revisited(self):
        data = records()
        data['ocr'] = {'version': '1', 'requires': ['missing; extra == "ocr-core"']}
        with self.assertRaisesRegex(RuntimeError, 'Missing'):
            resolve(data, ['pyside6-essentials', 'ocr[ocr-core]', 'ocr'])

    def test_macos_umbrella_only_uses_matching_real_providers(self):
        data = records()
        data['pysidesix-frameless-window'] = {'version': '0.8.2', 'requires': ['pyobjc>=10']}
        for name in ('pyobjc-core', 'pyobjc-framework-cocoa', 'pyobjc-framework-quartz'):
            data[name] = {'version': '11.0', 'requires': []}
        selected = resolve(data, ['pyside6-essentials', 'pysidesix-frameless-window'])
        self.assertNotIn('pyobjc', selected)
        self.assertIn('pyobjc-framework-quartz', selected)
        data['pyobjc-framework-quartz']['version'] = '10.0'
        with self.assertRaisesRegex(RuntimeError, 'Incompatible'):
            resolve(data, ['pyside6-essentials', 'pysidesix-frameless-window'])

    def test_inactive_root_is_not_required(self):
        resolve(records(), ['pyside6-essentials', 'missing; python_version < "3.0"'])

    def test_platform_markers_follow_native_builder(self):
        self.assertFalse(active(Requirement('missing; python_version < "3.0"'), set()))


if __name__ == '__main__':
    unittest.main()
