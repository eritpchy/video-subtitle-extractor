"""Synthetic Mach-O structures only; not real macOS/package execution proof."""
from __future__ import annotations
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from scripts import macos_minimum_version as minimum


def version(major=14, minor=4, patch=0): return major << 16 | minor << 8 | patch


def build_command(value=None, *, endian='<', platform=1, sdk=None, tools=()):
    value = version() if value is None else value
    return struct.pack(endian + '6I', 0x32, 24 + 8 * len(tools), platform, value, value if sdk is None else sdk, len(tools)) + b''.join(struct.pack(endian + 'II', *tool) for tool in tools)


def legacy(value=None, *, endian='<', command=0x24, sdk=None):
    value = version(10, 9) if value is None else value
    return struct.pack(endian + '4I', command, 16, value, value if sdk is None else sdk)


def thin(*, bits=64, endian='<', cpu=None, subtype=None, kind=6, commands=None):
    cpu = (0x100000c if bits == 64 else 7) if cpu is None else cpu
    subtype = (0 if cpu == 0x100000c else 3) if subtype is None else subtype
    commands = [build_command(endian=endian)] if commands is None else commands
    region = b''.join(commands)
    header = struct.pack(endian + '7I', 0xfeedfacf if bits == 64 else 0xfeedface, cpu, subtype, kind, len(commands), len(region), 0)
    return header + (b'\0' * 4 if bits == 64 else b'') + region


def fat(images, *, bits=32, endian='>', table_override=None):
    width = 32 if bits == 64 else 20
    table_end = 8 + len(images) * width
    offset = (table_end + 7) & ~7
    rows, payload = [], bytearray(b'\0' * (offset - table_end))
    for cpu, subtype, image in images:
        values = [cpu, subtype, offset, len(image), 3] + ([0] if bits == 64 else [])
        rows.append(values)
        payload.extend(image)
        padding = (-len(image)) % 8
        payload.extend(b'\0' * padding); offset += len(image) + padding
    if table_override: table_override(rows)
    return (struct.pack(endian + '2I', 0xcafebabf if bits == 64 else 0xcafebabe, len(images)) +
            b''.join(struct.pack(endian + ('IIQQII' if bits == 64 else 'IIIII'), *row) for row in rows) + payload)


class MachOMinimumTests(unittest.TestCase):
    def assert_bad(self, value, pattern=None):
        if pattern:
            with self.assertRaisesRegex(RuntimeError, pattern): minimum.macho_minimums(value)
        else:
            with self.assertRaises(RuntimeError): minimum.macho_minimums(value)

    def test_thin_all_header_widths_and_byte_orders(self):
        for bits in (32, 64):
            for endian in ('<', '>'):
                with self.subTest(bits=bits, endian=endian):
                    row = minimum.macho_minimums(thin(bits=bits, endian=endian))[0]
                    self.assertEqual(row['bits'], bits); self.assertEqual(row['minimum_macos_version'], '14.4')
                    self.assertEqual(row['byte_order'], 'little' if endian == '<' else 'big')

    def inspect_two_stat_clocks(self, *, fd_before=None, fd_after=None, path_after=None):
        # Synthetic Windows lstat/fstat time semantics, exercised on every
        # host; the file descriptor and Mach-O structural reads remain real.
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary).resolve() / 'image'
            data = thin(); path.write_bytes(data)
            common = {'st_dev': 7, 'st_ino': 11, 'st_mode': path.stat().st_mode,
                      'st_size': len(data), 'st_mtime_ns': 1000, 'st_birthtime_ns': 100}
            original_path = {**common, 'st_ctime_ns': 100}
            original_fd = {**common, 'st_ctime_ns': 900, **(fd_before or {})}
            with patch.object(Path, 'lstat', side_effect=[SimpleNamespace(**original_path),
                    SimpleNamespace(**{**original_path, **(path_after or {})})]), \
                    patch.object(minimum.os, 'fstat', side_effect=[SimpleNamespace(**original_fd),
                    SimpleNamespace(**{**original_fd, **(fd_after or {})})]):
                return minimum.macho_minimums(path)

    def test_distinct_stable_lstat_and_fstat_ctime_clocks_are_allowed(self):
        self.assertEqual(self.inspect_two_stat_clocks()[0]['minimum_macos_version'], '14.4')

    def test_same_api_ctime_changes_are_never_ignored(self):
        for mutation in ({'fd_after': {'st_ctime_ns': 901}},
                         {'path_after': {'st_ctime_ns': 101}}):
            with self.subTest(mutation=mutation), self.assertRaisesRegex(RuntimeError, 'changed during scan'):
                self.inspect_two_stat_clocks(**mutation)

    def test_cross_api_identity_changes_fail_while_opening(self):
        for field in ('st_dev', 'st_ino', 'st_mode', 'st_size', 'st_mtime_ns', 'st_birthtime_ns'):
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, 'changed while opening'):
                self.inspect_two_stat_clocks(fd_before={field: 9999})

    def test_same_api_identity_changes_fail_after_scan(self):
        for scope in ('fd_after', 'path_after'):
            for field in ('st_dev', 'st_ino', 'st_mode', 'st_size', 'st_mtime_ns', 'st_birthtime_ns'):
                with self.subTest(scope=scope, field=field), self.assertRaisesRegex(RuntimeError, 'changed during scan'):
                    self.inspect_two_stat_clocks(**{scope: {field: 9999}})

    def test_rewriting_same_file_does_not_fake_a_during_scan_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary).resolve() / 'image'
            for minor in range(4):
                path.write_bytes(thin(commands=[build_command(version(14, minor))]))
                self.assertEqual(minimum.macho_minimums(path)[0]['minimum_encoded'], version(14, minor))

    def test_legacy_patch_version_and_zero_sdk(self):
        rows = minimum.macho_minimums(thin(commands=[legacy(version(10, 13, 6), sdk=0)]))
        self.assertEqual(rows[0]['minimum_macos_version'], '10.13.6')
        self.assertEqual(rows[0]['deployment_commands'][0]['command'], 'LC_VERSION_MIN_MACOSX')

    def test_fat_32_64_both_byte_orders_all_slices_kept(self):
        images = [(0x1000007, 3, thin(cpu=0x1000007, commands=[legacy(version(14, 6))])),
                  (0x100000c, 0, thin(commands=[build_command(version(14, 4))]))]
        for bits in (32, 64):
            for endian in ('>', '<'):
                with self.subTest(bits=bits, endian=endian):
                    rows = minimum.macho_minimums(fat(images, bits=bits, endian=endian))
                    self.assertEqual([r['architecture'] for r in rows], ['x86_64', 'arm64'])
                    self.assertEqual(max(r['minimum_encoded'] for r in rows), version(14, 6))
                    self.assertTrue(all(r['slice_offset'] > 0 for r in rows))

    def test_big_endian_legacy_ppc_and_ppc64(self):
        for bits, cpu in ((32, 18), (64, 0x1000012)):
            rows = minimum.macho_minimums(thin(bits=bits, endian='>', cpu=cpu, subtype=0, commands=[legacy(endian='>')]))
            self.assertEqual(rows[0]['architecture'], 'ppc' if bits == 32 else 'ppc64')

    def test_runtime_types_all_require_deployment_target(self):
        for kind in minimum.RUNTIME_TYPES:
            self.assertEqual(len(minimum.macho_minimums(thin(kind=kind))), 1)
            self.assert_bad(thin(kind=kind, commands=[]), 'no supported macOS')

    def test_source_static_objects_debug_cores_are_not_runtime(self):
        for value in (b'', b'#!/bin/sh\necho source', b'plain .dylib text', minimum.AR_MAGIC + b'not parsed as runtime', minimum.THIN_AR_MAGIC):
            self.assertEqual(minimum.macho_minimums(value), [])
        for kind in minimum.NON_RUNTIME_TYPES:
            self.assertEqual(minimum.macho_minimums(thin(kind=kind, commands=[])), [])
        self.assertEqual(minimum.macho_minimums(fat([(0x100000c, 0, minimum.AR_MAGIC + b'object members')])), [])

    def test_nonruntime_header_command_structure_still_checked(self):
        self.assert_bad(thin(kind=1, commands=[struct.pack('<II', 1, 4)]))

    def test_unknown_filetype_architecture_subtype_and_width_fail(self):
        for value in (thin(kind=12), thin(kind=11), thin(kind=5), thin(kind=999),
                      thin(cpu=12), thin(cpu=0x100000c, subtype=77), thin(bits=32, cpu=0x100000c)):
            self.assert_bad(value)

    def test_other_platform_never_silently_ignored(self):
        for platform in (0, 2, 6, 7, 10, 999):
            self.assert_bad(thin(commands=[build_command(platform=platform)]), 'platform')
        for command in minimum.OTHER_VERSION_COMMANDS:
            self.assert_bad(thin(commands=[legacy(command=command)]), 'platform')
        self.assert_bad(thin(commands=[build_command(), legacy(command=0x25)]), 'platform')

    def test_duplicate_and_conflicting_minimum_commands_fail(self):
        self.assert_bad(thin(commands=[build_command(), build_command()]), 'Duplicate')
        self.assert_bad(thin(commands=[legacy(), legacy()]), 'Duplicate')
        self.assert_bad(thin(commands=[build_command(), legacy(version(13))]), 'Conflicting')
        self.assert_bad(thin(commands=[build_command(sdk=version(15)), legacy(version(), sdk=version(16))]), 'SDK')
        # Two historical encodings are tolerated only if their declarations agree.
        self.assertEqual(minimum.macho_minimums(thin(commands=[build_command(), legacy(version())]))[0]['minimum_encoded'], version())

    def test_build_tools_and_size_checked(self):
        good = thin(commands=[build_command(tools=((1, version(16)), (3, version(1000))))])
        self.assertEqual(len(minimum.macho_minimums(good)[0]['deployment_commands'][0]['tools']), 2)
        self.assert_bad(thin(commands=[build_command(tools=((1, 1), (1, 1)))]), 'Duplicate build-tool')
        command = bytearray(build_command()); struct.pack_into('<I', command, 20, 2)
        self.assert_bad(thin(commands=[bytes(command)]), 'tool count')
        self.assert_bad(thin(commands=[struct.pack('<II', 0x32, 8)]), 'Truncated LC_BUILD')

    def test_required_bit_deployment_variant_cannot_hide_foreign_platform(self):
        value = bytearray(build_command(platform=2)); struct.pack_into('<I', value, 0, 0x80000032)
        self.assert_bad(thin(commands=[legacy(), bytes(value)]), 'required-bit')

    def test_invalid_version_and_sdk_rejected(self):
        for value in (build_command(0), build_command(version(9)), build_command(sdk=version(13))):
            self.assert_bad(thin(commands=[value]), 'version/SDK')

    def test_header_truncation_and_reserved_field(self):
        good = thin()
        for length in (1, 2, 3, 4, 12, 27, 28, 31, 33, len(good) - 1): self.assert_bad(good[:length])
        altered = bytearray(good); struct.pack_into('<I', altered, 28, 1)
        self.assert_bad(bytes(altered), 'reserved')

    def test_command_region_count_and_trailing_bytes_fail(self):
        for offset, value in ((16, 2), (16, minimum.MAX_COMMANDS + 1), (20, minimum.MAX_COMMAND_BYTES + 1), (20, 0)):
            data = bytearray(thin()); struct.pack_into('<I', data, offset, value); self.assert_bad(bytes(data))
        data = bytearray(thin()); struct.pack_into('<I', data, 16, 0)
        self.assert_bad(bytes(data), 'exactly consume')

    def test_command_width_underflow_overflow_and_alignment(self):
        for width in (0, 4, 12, 4096):
            data = bytearray(thin()); struct.pack_into('<I', data, 36, width); self.assert_bad(bytes(data))
        self.assert_bad(thin(commands=[struct.pack('<II', 0x24, 24) + b'\0' * 16]), 'legacy')

    def test_java_shared_magic_is_not_reported_as_a_runtime_library(self):
        self.assert_bad(b'\xca\xfe\xba\xbe\x00\x00\x00\x34' + b'not a supported fat directory')

    def test_fat_truncation_count_and_nested_header(self):
        for value in (b'\xca\xfe\xba\xbe', struct.pack('>II', 0xcafebabe, 0), struct.pack('>II', 0xcafebabe, minimum.MAX_SLICES + 1), struct.pack('>II', 0xcafebabe, 2)):
            self.assert_bad(value)
        self.assert_bad(fat([(0x100000c, 0, fat([(0x100000c, 0, thin())]))]), 'not a supported thin')

    def test_fat_offsets_table_overlap_outside_alignment_and_zero_length(self):
        images = [(0x100000c, 0, thin())]
        changes = [(2, 0), (2, 33), (2, 1 << 30), (3, 0), (3, 1 << 30), (4, 64)]
        for index, value in changes:
            self.assert_bad(fat(images, table_override=lambda rows: rows[0].__setitem__(index, value)))
        self.assert_bad(fat(images, bits=64, table_override=lambda rows: rows[0].__setitem__(5, 1)), 'reserved')

    def test_fat_duplicate_overlap_and_cpu_subtype_disagreement(self):
        images = [(0x100000c, 0, thin()), (0x1000007, 3, thin(cpu=0x1000007))]
        self.assert_bad(fat([images[0], images[0]]), 'Duplicate')
        self.assert_bad(fat(images, table_override=lambda rows: rows[1].__setitem__(2, rows[0][2])), 'Overlapping')
        self.assert_bad(fat([(0x1000007, 3, thin())]), 'differs')
        self.assert_bad(fat([(0x100000c, 2, thin())]), 'differs')

    def test_fat_unknown_slice_mixed_static_runtime_and_foreign_platform_fail(self):
        self.assert_bad(fat([(0x100000c, 0, b'not Mach-O')]))
        self.assert_bad(fat([(0x100000c, 0, thin()), (0x1000007, 3, minimum.AR_MAGIC)]), 'Mixed')
        self.assert_bad(fat([(0x100000c, 0, thin()), (0x1000007, 3, thin(cpu=0x1000007, commands=[build_command(platform=2)]))]), 'platform')

    def test_stage_reports_actual_max_and_all_limiting_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); (root / 'runtime').mkdir()
            (root / 'runtime/python').write_bytes(thin(kind=2, commands=[legacy(version(13))]))
            (root / 'runtime/QtCocoa').write_bytes(thin())
            (root / 'native-worker').write_bytes(thin(commands=[build_command(version(14, 4, 1))]))
            (root / 'helper').write_bytes(thin(commands=[legacy(version(14, 4, 1))]))
            (root / 'models.bin').write_bytes(b'x' * 1024 * 1024)
            (root / 'source.a').write_bytes(minimum.AR_MAGIC)
            with patch.object(Path, 'read_bytes', side_effect=AssertionError('scanner must not read whole files')):
                report = minimum.audit_minimum_system(root, expected_arches=('arm64',))
            self.assertEqual(report['minimum_macos_version'], '14.4.1')
            self.assertEqual(report['limiting_files'], ['helper', 'native-worker'])
            self.assertEqual(report['runtime_macho_files'], 4)
            self.assertEqual(report['scanned_regular_files'], 6)
            self.assertEqual(set(report['file_sha256']), set(report['images']))
            self.assertIn('not oldest-OS execution proof', report['scope'])

    def test_required_architecture_does_not_filter_higher_other_slice(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'universal').write_bytes(fat([(0x100000c, 0, thin()), (0x1000007, 3, thin(cpu=0x1000007, commands=[build_command(version(15, 0))]))]))
            self.assertEqual(minimum.audit_minimum_system(root, expected_arches=('arm64',))['minimum_macos_version'], '15.0')
            with self.assertRaisesRegex(RuntimeError, 'required architecture'): minimum.audit_minimum_system(root, expected_arches=('ppc',))

    def test_empty_tree_and_runtime_without_min_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); (root / 'source.py').write_text('source')
            with self.assertRaisesRegex(RuntimeError, 'No runtime'): minimum.audit_minimum_system(root)
            (root / 'bad').write_bytes(thin(commands=[]))
            with self.assertRaisesRegex(RuntimeError, 'no supported'): minimum.audit_minimum_system(root)

    @unittest.skipIf(os.name == 'nt', 'POSIX symbolic-link test')
    def test_internal_framework_links_recorded_without_duplicate_scan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); binary = root / 'QtCore.framework/Versions/A/QtCore'; binary.parent.mkdir(parents=True); binary.write_bytes(thin())
            (binary.parent.parent / 'Current').symlink_to('A', target_is_directory=True)
            (root / 'QtCore.framework/QtCore').symlink_to('Versions/Current/QtCore')
            report = minimum.audit_minimum_system(root)
            self.assertEqual(report['runtime_macho_files'], 1); self.assertEqual(len(report['symlinks']), 2)

    @unittest.skipIf(os.name == 'nt', 'POSIX symbolic-link test')
    def test_external_broken_and_cyclic_links_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'stage'; root.mkdir(); (root / 'real').write_bytes(thin())
            for target in ('../outside', 'missing', 'link'):
                if target == '../outside': (root.parent / 'outside').write_bytes(thin())
                (root / 'link').symlink_to(target)
                with self.assertRaises(RuntimeError): minimum.audit_minimum_system(root)
                (root / 'link').unlink()
            (root / 'link').symlink_to('.', target_is_directory=True)
            with self.assertRaises(RuntimeError): minimum.audit_minimum_system(root)

    @unittest.skipIf(os.name == 'nt', 'POSIX symbolic-link test')
    def test_directory_alias_graph_cycles_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); (root / 'a').mkdir(); (root / 'b').mkdir()
            (root / 'a/image').write_bytes(thin())
            (root / 'a/other').symlink_to('../b', target_is_directory=True)
            (root / 'b/other').symlink_to('../a', target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, 'Cyclic'): minimum.audit_minimum_system(root)

    def test_expected_architecture_configuration_is_strict(self):
        for arches in ('arm64', (), ('unknown',), ('arm64', 'arm64')):
            with self.assertRaises(ValueError): minimum.audit_minimum_system(Path('.'), expected_arches=arches)

    def test_file_and_tree_mutation_are_detected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); path = root / 'binary'; path.write_bytes(thin())
            original = minimum.macho_minimums
            def mutate(source, **kwargs):
                result = original(source, **kwargs); (root / 'new').write_text('changed'); return result
            with patch.object(minimum, 'macho_minimums', side_effect=mutate), self.assertRaisesRegex(RuntimeError, 'tree changed'):
                minimum.audit_minimum_system(root)

    def test_cli_creates_report_without_overwrite_or_file_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); stage = root / 'stage'; stage.mkdir(); image = thin(); (stage / 'image').write_bytes(image)
            report = root / 'report.json'; command = [sys.executable, '-S', str(Path(minimum.__file__)), '--stage', str(stage), '--output', str(report)]
            subprocess.run(command, check=True, capture_output=True)
            self.assertEqual(json.loads(report.read_bytes())['minimum_macos_version'], '14.4')
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertEqual((stage / 'image').read_bytes(), image)

    def test_bounded_reads_even_for_large_declared_file(self):
        class Guarded(io.BytesIO):
            def read(self, count=-1):
                if count < 0 or count > minimum.MAX_COMMAND_BYTES: raise AssertionError('unbounded read')
                return super().read(count)
        data = thin()
        rows = minimum._parse(minimum._Reader(Guarded(data), 10 * 1024 ** 3, 'large sparse test'))
        self.assertEqual(rows[0]['minimum_macos_version'], '14.4')
        self.assertEqual(rows[0]['slice_size'], 10 * 1024 ** 3)


if __name__ == '__main__': unittest.main()
