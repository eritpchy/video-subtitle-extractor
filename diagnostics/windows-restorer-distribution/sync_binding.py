#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Offline binding/bootstrap synchronizer. Never builds, downloads or dispatches."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import re

HERE=Path(__file__).resolve().parent

def expected_outputs():
    raw=(HERE/'pins.json').read_bytes();pin=json.loads(raw)
    if not isinstance(pin.get('commit'),str) or not re.fullmatch('[0-9a-f]{40}',pin['commit']):
        raise RuntimeError('Explicit final source SHA required even for incomplete preparation')
    source=(HERE/'source_only.py').read_text()
    for name,value in [('APPLICATION_PINS_B64',base64.b64encode(raw).decode()),('APPLICATION_PINS_SHA256',hashlib.sha256(raw).hexdigest())]:
        source,count=re.subn(r'^'+name+r' = .*$',name+' = '+repr(value),source,flags=re.M)
        if count!=1:raise RuntimeError('Exactly one embedded input anchor required')
    workflow_path=HERE.parents[1]/'.github/workflows/windows-restorer-distribution.yml'
    workflow=workflow_path.read_text()
    workflow,count=re.subn(r'^(        ref: )[0-9a-f]{40}$',lambda m:m[1]+pin['commit'],workflow,flags=re.M)
    if count!=7:raise RuntimeError('Exactly seven application source checkouts required')
    embedded=''.join(('        '+line if line else '')+'\n' for line in source.splitlines())
    workflow,count=re.subn(r'^        #!/usr/bin/env python3\n.*?(?=\n    - name: Preserve)',lambda m:embedded.rstrip('\n'),workflow,flags=re.M|re.S)
    if count!=2:raise RuntimeError('Exactly two source-only literal bootstraps required')
    return {HERE/'source_only.py':source,workflow_path:workflow}

def main():
    parser=argparse.ArgumentParser(description=__doc__);mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check',action='store_true');mode.add_argument('--write',action='store_true');args=parser.parse_args()
    for path,expected in expected_outputs().items():
        if args.write:path.write_text(expected)
        elif path.read_text()!=expected:raise RuntimeError('Binding/bootstrap drift: '+str(path))
    print(json.dumps({'result':'synchronized','network_build_or_dispatch':False,'complete_input_validation':'runtime load_pins remains mandatory; null future fields are not executable'}))

if __name__=='__main__':main()
