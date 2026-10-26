#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Rebuild the included fixed header using only Python stdlib and native MSVC."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
sys.dont_write_bytecode = True
# Supports an isolated -I -S build from the extracted companion, not imports
# from a producer checkout, cwd, user site or PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from source_bundle import verify_extracted_sources


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--acknowledge-redistribution-license', action='store_true')
    args = parser.parse_args()
    if not args.acknowledge_redistribution_license: raise RuntimeError('Applicable redistribution rights must be confirmed; no automatic license acceptance')
    root = args.source_root.absolute(); output = args.output.absolute()
    if root.resolve() != Path(__file__).resolve().parents[2]: raise RuntimeError('Run the rebuild script contained in this exact source companion')
    if output.is_relative_to(root): raise RuntimeError('Place output outside the source companion')
    manifest = verify_extracted_sources(root)
    from build import compile_fixed, copy_notices
    output.mkdir(parents=False, exist_ok=False)
    copy_notices(root / 'notices', output / 'notices')
    result = compile_fixed(root / 'native/windows-restorer', root / 'generated', output)
    verify_extracted_sources(root)
    original = json.loads((root / 'evidence/original-build-record.json').read_bytes())
    original_tools = {name: row['sha256'] for name, row in original['toolchain']['tools'].items()}
    actual_tools = {name: row['sha256'] for name, row in result['toolchain']['tools'].items()}
    result.update({'toolchain_binaries_match_original': original_tools == actual_tools, 'schema': 1, 'scope': 'fixed-header-source-rebuild-not-client-certification',
                   'original_exe_sha256': manifest['original_exe_sha256'],
                   'bit_identical_to_original': result['exe_sha256'] == manifest['original_exe_sha256'],
                   'client_restore_verified': False, 'gpu_inference_verified': False})
    (output / 'rebuild-record.json').write_text(json.dumps(result, indent=2) + '\n')
    print(f'Rebuilt; bit-identical to original: {result["bit_identical_to_original"]}. This does not establish client/GPU acceptance.')


if __name__ == '__main__': main()
