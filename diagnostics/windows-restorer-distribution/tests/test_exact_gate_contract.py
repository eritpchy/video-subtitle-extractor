# SPDX-License-Identifier: Apache-2.0
"""Use the real unchanged exact8f validators, not a recreated permissive gate."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from test_distribution import d, s, context, source_report, compile_record, H


class ExactGateContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root=Path(os.environ.get('VSE_REVIEW_SOURCE','application-source')).absolute()
        if not (root/'native/windows-restorer/release_gate.py').is_file():
            raise RuntimeError('Set VSE_REVIEW_SOURCE to the reviewed exact8f small source tree; no network fallback')
        cls.gate=d.gate_at(root);cls.source=root
    def handoff(self):
        return {'exe_sha256':H,'source_zip_sha256':H,'archive':{'sha256':H,'identity':{'commit':s.SOURCE_COMMIT}}}
    def consumer(self,runner):
        return {'schema':1,'scope':'same-exe-final-zip-server-restore','result':'passed','runner':runner,
            'native_os':{'runner':runner,'build':s.RUNNERS[runner],'product_type':3},'exe_sha256':H,
            'source_zip_sha256':H,'archive_sha256':H,'handoff_sha256':H,'compile_performed':False,
            'restore_returncode':0,'post_run_errors':[],'inputs_unchanged_verified':True,
            'gpu_inference_verified':False,'client_windows_10_11_verified':False,
            'restored_before_launcher':{'result':'passed','all_file_sha256_checked':True,'no_extra_or_reparse_entries':True,
                'entries':1,'inventory_sha256':H},
            'restored_after_launcher':{'result':'passed','all_file_sha256_checked':True,'no_extra_or_reparse_entries':True,
                'entries':1,'inventory_sha256':H},
            'launcher_raw_inventory':{'a':{'bytes':1,'sha256':H}},
            'raw_logs':{name:{'bytes':1,'sha256':H} for name in ('restore.stdout.log','restore.stderr.log','launcher.log')},
            'launcher':{'result':'passed','commit':s.SOURCE_COMMIT,'archive_extraction_used':False,'gpu_inference_verified':False,
                'application_source_after':{'result':'passed'},'post_run_errors':[],
                'scenarios':[{'scenario':name,'result':'passed','returncode':0,'audit':{'passed':True,'errors':[]}}
                    for name in ('clean-host-path','contaminated-host-environment')]}}
    def test_valid_new_join_uses_real_consumer_validator(self):
        reports=[self.consumer(runner) for runner in s.RUNNERS]
        result=d.validate_join(self.gate,self.handoff(),reports,source_report(),context())
        self.assertTrue(result['rebuilt_exe_not_used_as_delivery']);self.assertFalse(result['legacy_rebuild_transport_proof_claimed'])
    def test_same_exe_two_servers_and_launcher_fail_closed(self):
        mutations=[lambda x:x[1].update(runner='windows-2022'),lambda x:x[1].update(exe_sha256='b'*64),
            lambda x:x[1].update(compile_performed=True),lambda x:x[0].update(handoff_sha256='b'*64),
            lambda x:x[0].update(source_zip_sha256='b'*64),lambda x:x[0]['launcher']['scenarios'].pop(),
            lambda x:x[0]['launcher'].update(archive_extraction_used=True),lambda x:x[0].update(raw_logs={}),
            lambda x:x[1]['restored_after_launcher'].update(inventory_sha256='b'*64),
            lambda x:x[1].update(inputs_unchanged_verified=False),lambda x:x[1].update(gpu_inference_verified=True)]
        for change in mutations:
            rows=[self.consumer(runner) for runner in s.RUNNERS];change(rows)
            with self.subTest(mutation=mutations.index(change)),self.assertRaises(RuntimeError):
                d.validate_join(self.gate,self.handoff(),rows,source_report(),context())
    def test_report_sha_external_anchor_is_required(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'report.json';s.write_json(path,{'schema':1})
            data,pin=self.gate.read_bound_report(path,s.sha(path));self.assertEqual(data,{'schema':1})
            with self.assertRaises(RuntimeError):self.gate.read_bound_report(path,'b'*64)
            path.write_text('{}')
            with self.assertRaises(RuntimeError):self.gate.read_bound_report(path,pin['sha256'])
    def test_original_build_schema_with_foreign_global_build_module(self):
        record=compile_record();record.update(schema=2,result='built-not-client-certified',client_restore_verified=False,
            gpu_inference_verified=False,git={'commit':s.SOURCE_COMMIT,'dirty':False},git_after_build={'commit':s.SOURCE_COMMIT,'dirty':False})
        foreign=mock.Mock(spec=['assert_distributable_record']);foreign.assert_distributable_record.side_effect=AssertionError('Foreign build invoked')
        with mock.patch.dict('sys.modules',{'build':foreign}):self.gate.validate_build(record)
        foreign.assert_distributable_record.assert_not_called()

    def test_collector_exact_zlib_loader_not_gate_allowlist_or_global_cache(self):
        with self.assertRaises(RuntimeError):self.gate.local_helper('zlib_build')
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'link-map.txt'
            path.write_text(''.join(' 0001:00000001 '+name+' 0000000140000001 f zlib.obj\n' for name in ('inflateInit2_','inflate','inflateEnd')))
            expected=s.load_exact(self.source/'native/windows-restorer/zlib_build.py','test_zlib').inspect_link_map(path)
            foreign=mock.Mock();foreign.inspect_link_map.side_effect=AssertionError('Foreign zlib helper invoked')
            with mock.patch.dict('sys.modules',{'zlib_build':foreign,'_restorer_distribution_zlib_build':foreign}):
                self.assertEqual(d.verify_rebuilt_map(self.source,path,expected),expected)
            foreign.inspect_link_map.assert_not_called()
            with path.open('a') as output:output.write(' 0001:00000002 gzwrite 0000000140000002 f zlib.obj\n')
            with self.assertRaises(RuntimeError):d.verify_rebuilt_map(self.source,path,expected)

    def test_collection_plan_all_eight_actual_jobs_bind_independent_hashes(self):
        needs={}
        for short,profile in (('cu118','paddle-cuda11.8'),('cu126','paddle-cuda12.6')):
            ctx=context();ctx['profile']=profile
            needs['produce-'+short]={'result':'success','outputs':{'context':json.dumps(ctx),'handoff_sha256':H,
                'index_sha256':H,'source_artifact_id':'11','delivery_artifact_id':'12','raw_artifact_id':'13','delivery_bytes':'1234'}}
            needs['rebuild-'+short]={'result':'success','outputs':{'report_sha256':H,'report_artifact_id':'14','raw_artifact_id':'15'}}
            for year in ('2022','2025'):
                needs['consume-'+short+'-'+year]={'result':'success','outputs':{'report_sha256':H,'report_artifact_id':'16','raw_artifact_id':'17'}}
        plan=d.collection_plan(needs);self.assertEqual(set(plan),{'paddle-cuda11.8','paddle-cuda12.6'})
        self.assertEqual(plan['paddle-cuda11.8']['consumer_report_sha256'],{'windows-2022':H,'windows-2025':H})
        for job,field,value in [('produce-cu118','handoff_sha256','b'*64),('rebuild-cu126','report_sha256',''),
            ('consume-cu126-2025','report_artifact_id',''),('produce-cu126','context',json.dumps(context()))]:
            bad=copy.deepcopy(needs);bad[job]['outputs'][field]=value
            with self.subTest(job=job,field=field),self.assertRaises(RuntimeError):d.collection_plan(bad)
