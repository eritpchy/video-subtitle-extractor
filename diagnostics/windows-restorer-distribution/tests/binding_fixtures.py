# SPDX-License-Identifier: Apache-2.0
"""Synthetic OFFLINE input identities only; never real canonical CI pins."""
import base64
import contextlib
import hashlib
import json
from pathlib import Path
import tempfile
from unittest import mock

H = 'a' * 64

def fixture_pins():
    profiles = {}
    for index, (profile, suffix) in enumerate((('paddle-cuda11.8','cuda11.8'),('paddle-cuda12.6','cuda12.6'))):
        profiles[profile] = dict(canonical_artifact_id=101+index, canonical_artifact_name='release-candidate-windows-x64-'+profile,
            outer_container_bytes=1000, outer_api_sha256=H, inner_zip='vse-2.3.1-windows-x64-'+suffix+'.zip',
            inner_zip_bytes=800, inner_zip_sha256=H, receipt_bytes=80, receipt_sha256=H,
            checksum_bytes=102, checksum_sha256=H, expanded_bytes=1600, expiry_utc='2099-01-01T00:00:00Z',job=201+index)
    return dict(schema=2,scope='explicit-final-cuda-inputs-build-only',repository='eritpchy/video-subtitle-extractor',
        commit='c'*40,version='2.3.1',run_id=301,run_attempt=7,candidate_set_job=203,
        project_notice=dict(source='native/ocr/vc-runtime-NOTICE.txt',name='Microsoft-Visual-Cpp-runtime-NOTICE.txt',
            bytes=2001,sha256='08f9ea68589360f62df35b6232e4fe6307b11d46e4f0fbb327e160bfb6f0e50f'),profiles=profiles)

def encoded(pins):
    raw=(json.dumps(pins,indent=2)+'\n').encode()
    return raw,base64.b64encode(raw).decode(),hashlib.sha256(raw).hexdigest()

@contextlib.contextmanager
def configured(driver,pins=None):
    pins=pins or fixture_pins();raw,b64,digest=encoded(pins)
    with tempfile.TemporaryDirectory() as temp, contextlib.ExitStack() as stack:
        path=Path(temp)/'pins.json';path.write_bytes(raw)
        stack.enter_context(mock.patch.object(driver,'PINS_PATH',path))
        stack.enter_context(mock.patch.object(driver.s,'APPLICATION_PINS_B64',b64))
        stack.enter_context(mock.patch.object(driver.s,'APPLICATION_PINS_SHA256',digest))
        yield pins
