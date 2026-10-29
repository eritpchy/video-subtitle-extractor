# SPDX-License-Identifier: Apache-2.0
"""Offline binding/rights/network regressions; no build or CI execution."""
import ast
import base64
import hashlib
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import os
from types import SimpleNamespace
import urllib.error
import unittest
from unittest import mock
from binding_fixtures import configured,fixture_pins,encoded,H

HERE=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('_input_binding_driver',HERE/'driver.py')
d=importlib.util.module_from_spec(spec);spec.loader.exec_module(d)

class BindingContract(unittest.TestCase):
    def test_explicit_non2_attempt_is_accepted(self):
        with configured(d) as pin:
            self.assertEqual(d.load_pins(),pin)
            self.assertEqual(d.s.expected_pins()['run_attempt'],7)
    def test_incomplete_future_inputs_fail_before_platform_or_network(self):
        pin=fixture_pins();pin['run_id']=None
        with configured(d,pin),self.assertRaises(RuntimeError):d.load_pins()
    def test_no_docx_acquisition_function(self):
        tree=ast.parse((HERE/'driver.py').read_text())
        self.assertNotIn('acquire_notices',[n.name for n in tree.body if isinstance(n,ast.FunctionDef)])
    def test_no_implicit_push_ack(self):
        workflow=(HERE.parents[1]/'.github/workflows/windows-restorer-distribution.yml').read_text()
        self.assertFalse("github.event_name == 'push'" in workflow)
        self.assertFalse('  push:' in workflow)
    def test_changed_or_missing_explicit_fields_rejected(self):
        mutations=[lambda p:p.update(schema=1),lambda p:p.update(repository='other/repo'),
            lambda p:p.update(commit='bad'),lambda p:p.update(version='2.3.0'),lambda p:p.update(run_id=None),
            lambda p:p.update(run_attempt=0),lambda p:p.update(run_attempt=True),lambda p:p.update(candidate_set_job=201),
            lambda p:p['profiles']['paddle-cuda11.8'].update(canonical_artifact_id=None),
            lambda p:p['profiles']['paddle-cuda12.6'].update(canonical_artifact_id=101),
            lambda p:p['profiles']['paddle-cuda11.8'].update(receipt_sha256=None),
            lambda p:p['profiles']['paddle-cuda11.8'].update(checksum_sha256='0'),
            lambda p:p['profiles']['paddle-cuda11.8'].update(expiry_utc='2000-01-01T00:00:00Z')]
        for change in mutations:
            pin=fixture_pins();change(pin)
            with self.subTest(mutation=mutations.index(change)),configured(d,pin),self.assertRaises(RuntimeError):d.load_pins()
    def test_embedded_corruption_and_duplicate_keys_have_no_fallback(self):
        with configured(d):
            for field,value in [('APPLICATION_PINS_B64','!invalid!'),('APPLICATION_PINS_SHA256','b'*64)]:
                with self.subTest(field=field),mock.patch.object(d.s,field,value),self.assertRaises(RuntimeError):d.load_pins()
            raw=d.PINS_PATH.read_bytes().replace(b'"run_id": 301,',b'"run_id": 301, "run_id": 302,')
            d.PINS_PATH.write_bytes(raw)
            with mock.patch.object(d.s,'APPLICATION_PINS_B64',base64.b64encode(raw).decode()),mock.patch.object(d.s,'APPLICATION_PINS_SHA256',hashlib.sha256(raw).hexdigest()),self.assertRaises(RuntimeError):d.load_pins()
    def test_context_cannot_replace_any_selected_final_identity(self):
        with configured(d):
            pin=d.load_pins();profile='paddle-cuda11.8'
            ctx=dict(schema=1,source_commit=pin['commit'],source_version=pin['version'],profile=profile,
                application_identity=d.s.binding_identity(pin,profile),source_zip_bytes=123,
                **{key:H for key in ('source_zip_sha256','original_exe_sha256','handoff_sha256','embedded_header_sha256','generator_identity_sha256','source_closure_sha256','source_manifest_sha256','original_build_record_sha256')})
            d.s.validate_context(ctx)
            changes=[lambda x:x.update(source_commit='b'*40),lambda x:x.update(source_version='2.3.0'),
                lambda x:x['application_identity'].update(run_attempt=2),lambda x:x['application_identity'].update(run_id=302),
                lambda x:x['application_identity']['canonical'].update(canonical_artifact_id=999),
                lambda x:x['application_identity']['canonical'].update(receipt_sha256='b'*64),
                lambda x:x.update(archive_bytes=0),lambda x:x.update(expanded_bytes=1599),lambda x:x.update(inventory_entries=True)]
            for change in changes:
                bad=copy.deepcopy(ctx);change(bad)
                with self.subTest(change=changes.index(change)),self.assertRaises(RuntimeError):d.s.validate_context(bad)
    def test_committed_and_embedded_pin_bytes_must_match(self):
        with configured(d):
            d.PINS_PATH.write_bytes(d.PINS_PATH.read_bytes()+b' ')
            with self.assertRaises(RuntimeError):d.load_pins()

class OriginContract(unittest.TestCase):
    def setUp(self): self.enterContext(configured(d))
    def records(self):
        pin=d.load_pins();row=pin['profiles']['paddle-cuda11.8']
        run=dict(id=pin['run_id'],head_sha=pin['commit'],run_attempt=pin['run_attempt'],status='completed',
            conclusion='success',path='.github/workflows/release.yml',head_branch='fix/restore-release-matrix',
            repository={'full_name':pin['repository']})
        job=dict(id=row['job'],run_id=pin['run_id'],head_sha=pin['commit'],run_attempt=pin['run_attempt'],
            status='completed',conclusion='success',name='paddle-cuda11.8 / windows-2022 (GPU hardware not tested)')
        candidate={**job,'id':pin['candidate_set_job'],'name':'Verify approved candidate matrix (not publication)'}
        return pin,[run,job,candidate]
    def test_all_exact_origin_fields_pass_non2_attempt(self):
        pin,rows=self.records()
        with mock.patch.object(d,'api',side_effect=rows) as api:
            r=d.verify_origin(pin,'paddle-cuda11.8','fixture-token')
            self.assertEqual(r['run']['run_attempt'],7);self.assertEqual(api.call_count,3)
    def test_each_wrong_origin_field_is_rejected(self):
        changes=[(0,'id',99),(0,'head_sha','b'*40),(0,'run_attempt',2),(0,'status','in_progress'),
            (0,'conclusion','failure'),(0,'path','other.yml'),(0,'head_branch','other'),(0,'repository',{'full_name':'other/repo'}),
            (1,'id',99),(1,'run_id',99),(1,'run_attempt',2),(1,'head_sha','b'*40),(1,'status','queued'),
            (1,'conclusion','skipped'),(1,'name','paddle-cuda12.6 / windows-2022 (GPU hardware not tested)'),
            (2,'id',99),(2,'run_id',99),(2,'run_attempt',2),(2,'conclusion','cancelled'),(2,'name','fake collector')]
        for index,key,value in changes:
            pin,rows=self.records();rows[index][key]=value
            with self.subTest(index=index,key=key),mock.patch.object(d,'api',side_effect=rows),self.assertRaises(RuntimeError):d.verify_origin(pin,'paddle-cuda11.8','fixture-token')
    def test_incomplete_pins_never_reach_origin_api(self):
        pin=fixture_pins();pin['run_id']=None
        with mock.patch.object(d,'api') as api,self.assertRaises(RuntimeError):d.verify_origin(pin,'paddle-cuda11.8','t')
        api.assert_not_called()
    def test_403_is_not_retried_or_redirected(self):
        error=urllib.error.HTTPError('https://api.github.com',403,'denied',{},None);opener=mock.Mock();opener.open.side_effect=error
        with tempfile.TemporaryDirectory() as tmp,mock.patch.object(d.urllib.request,'build_opener',return_value=opener),mock.patch.object(d.urllib.request,'urlopen') as storage:
            with self.assertRaises(RuntimeError):d.download_artifact({'id':1,'size_in_bytes':1,'digest':'sha256:'+H},Path(tmp)/'outer.zip','fixture-token')
            opener.open.assert_called_once();storage.assert_not_called();self.assertFalse((Path(tmp)/'outer.zip').exists())

class NoticeContract(unittest.TestCase):
    def setUp(self): self.enterContext(configured(d))
    def source(self):
        root=Path(os.environ.get('VSE_REVIEW_SOURCE','application-source')).resolve()
        return root
    def test_real_current_project_notice_uses_no_network(self):
        with mock.patch.object(d.urllib.request,'urlopen',side_effect=AssertionError('network forbidden')) as network:
            r=d.s.verify_project_notice(self.source(),d.load_pins())
        network.assert_not_called();self.assertFalse(r['network_used']);self.assertFalse(r['licensing_review']['final_publication_authorized'])
    def fixture(self,root):
        source=self.source();target=root/'native/ocr';target.mkdir(parents=True)
        for name in ['vc-runtime-manifest.json','vc-runtime-NOTICE.txt']:(target/name).write_bytes((source/'native/ocr'/name).read_bytes())
        return target
    def test_changed_or_missing_notice_bytes_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);target=self.fixture(root);(target/'vc-runtime-NOTICE.txt').write_bytes(b'changed')
            with self.assertRaises(RuntimeError):d.s.verify_project_notice(root,d.load_pins())
            (target/'vc-runtime-NOTICE.txt').unlink()
            with self.assertRaises(FileNotFoundError):d.s.verify_project_notice(root,d.load_pins())
    def test_active_docx_or_false_legal_approval_is_rejected(self):
        for change in [lambda x:x.update(schema=1),lambda x:x['files'].append({'name':'license.docx'}),
                       lambda x:x['licensing_review'].update(final_publication_authorized=True),
                       lambda x:x['licensing_review'].update(status='legally-approved')]:
            with self.subTest(change=change),tempfile.TemporaryDirectory() as temp:
                root=Path(temp);target=self.fixture(root);m=json.loads((target/'vc-runtime-manifest.json').read_text());change(m);(target/'vc-runtime-manifest.json').write_text(json.dumps(m))
                with self.assertRaises(RuntimeError):d.s.verify_project_notice(root,d.load_pins())

class EarlyFailureContract(unittest.TestCase):
    def test_every_driver_entry_rejects_incomplete_input_before_side_effects(self):
        pin=fixture_pins();pin['run_id']=None
        for function,args in [(d.producer,SimpleNamespace(acknowledge_license=True)),(d.consumer,SimpleNamespace()),(d.collector,SimpleNamespace()),(d.collect_from_environment,Path('.'))]:
            with self.subTest(function=function.__name__),configured(d,pin),mock.patch.object(d,'api') as api,mock.patch.object(d,'fresh_work') as work,mock.patch.object(d.s,'native_server') as native:
                with self.assertRaises(RuntimeError):function(args)
                api.assert_not_called();work.assert_not_called();native.assert_not_called()
    def test_source_only_incomplete_binding_precedes_host_file_and_build(self):
        pin=fixture_pins();pin['run_id']=None
        with configured(d,pin),mock.patch.dict(os.environ,{'VSE_SOURCE_CONTEXT':'{}'}),mock.patch.object(d.s,'native_server') as native,mock.patch.object(d.s.subprocess,'run') as child:
            with self.assertRaises(RuntimeError):d.s.main()
            native.assert_not_called();child.assert_not_called()

if __name__=='__main__':unittest.main()
