# SPDX-License-Identifier: Apache-2.0
"""Offline contract fixtures only. No test here claims Windows/MSVC acceptance."""
import ast
import copy
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import unittest
from unittest import mock
import zipfile
from binding_fixtures import configured, fixture_pins

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('_distribution_test_driver', HERE/'driver.py')
d = importlib.util.module_from_spec(spec); spec.loader.exec_module(d)
s = d.s
H = 'a'*64


def require_stdlib_imports(source):
    roots=set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node,ast.Import):
            roots.update(alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node,ast.ImportFrom):
            if node.level or not node.module:raise RuntimeError('Relative bootstrap import rejected')
            roots.add(node.module.split('.')[0])
    if not roots <= sys.stdlib_module_names:raise RuntimeError('Non-stdlib bootstrap import: '+repr(sorted(roots-sys.stdlib_module_names)))
    return roots


class ConfiguredCase(unittest.TestCase):
    def setUp(self):
        self.enterContext(configured(d))


def context(profile='paddle-cuda11.8'):
    pin=d.load_pins()
    return dict(schema=1,source_commit=pin['commit'],source_version=pin['version'],
        application_identity=s.binding_identity(pin,profile),profile=profile,source_zip_bytes=123,
        **{key:H for key in ('source_zip_sha256','original_exe_sha256','handoff_sha256','embedded_header_sha256',
            'generator_identity_sha256','source_closure_sha256','source_manifest_sha256','original_build_record_sha256')})


def compile_record():
    tool={'path':'C:/MSVC/14.44/tool.exe','bytes':100,'sha256':H}
    return {'command':['cl.exe','/MT','/W4','/WX','/GS','/guard:cf','/DEPENDENTLOADFLAG:0x800'],
        'synthetic_space_fixture':False,'synthetic_translation_units':[], 'exe_bytes':17,'exe_sha256':H,
        'imports':['KERNEL32.DLL','BCRYPT.DLL'], 'pe_dependency_policy':{'machine':0x8664,'optional_header_magic':0x20b,
            'dependent_load_flags':0x800,'source':'PE binary LoadConfig, not dumpbin text'},
        'restricted_zlib':{'excluded_gzwrite_deflate_crc_archive_symbols_absent':True,'pin_sha256':H,
            'compiled_objects':[{'source':name,'object_sha256':H,'defined_symbols':['inflate'],
                'command':['/MT','/W4','/WX','/DZ_SOLO','/DNO_GZIP']} for name in ('inflate.c','inftrees.c','inffast.c','adler32.c','zutil.c')],
            'final_link_map':{'synthetic_space_shim_present':False,'required_inflate_symbols_present':True,
                'excluded_gzwrite_deflate_crc_archive_symbols_absent':True,'sha256':H,'bytes':10}},
        'toolchain':{'tools':{name:tool.copy() for name in ('cl','link','dumpbin')},
            'actual_headers':[tool.copy()],'actual_libraries':[tool.copy()],'actual_sdk_headers':[tool.copy()],
            'compiler_bv_and_link_log':'actual mock compilation\n'}}


def source_report(ctx=None):
    ctx=ctx or context(); record=compile_record()
    record.update(scope='fixed-header-source-rebuild-not-client-certification',original_exe_sha256=H,
        client_restore_verified=False,gpu_inference_verified=False,bit_identical_to_original=True)
    source={'result':'passed',**{key:ctx[key] for key in ('source_manifest_sha256','original_build_record_sha256',
        'embedded_header_sha256','source_closure_sha256','generator_identity_sha256')}}
    names=('rebuild.log','rebuilt/compiler.log','rebuilt/link-map.txt','rebuilt/rebuild-record.json','rebuilt/compile-command-record.json')
    return {'schema':1,'scope':'source-zip-only-msvc-rebuild-no-repository-or-application-archive','result':'passed',
        'producer_context':ctx,'native_os':{'runner':'windows-2025','build':26100,'product_type':3},
        'repository_checkout_used':False,'application_archive_or_volumes_used':False,'post_run_errors':[],
        'gpu_inference_verified':False,'source_before':source,'source_after':copy.deepcopy(source),
        'source_zip_before':{'bytes':123,'sha256':H},'source_zip_after':{'bytes':123,'sha256':H},
        'rebuild_record':record,'rebuilt_exe':{'bytes':17,'sha256':H},
        'raw_artifacts':{name:{'bytes':10,'sha256':H} for name in names},
        'toolchain_initialization':{'scope':'installed-14.44-child-environment-only','toolset':'14.44.12345',
            'compiler':{'bytes':100,'sha256':H}},
        'rebuild_command':['python','-I','-S','-B','rebuild.py','--source-root','s','--output','o','--acknowledge-redistribution-license']}


class SourceContract(ConfiguredCase):
    def test_valid_source_scope_is_distinct(self): s.validate_source_report(source_report(),context())
    def test_truthful_non_bit_identical_rebuild_is_allowed(self):
        row = source_report()
        row['rebuilt_exe']['sha256'] = 'b' * 64
        row['rebuild_record']['exe_sha256'] = 'b' * 64
        row['rebuild_record']['bit_identical_to_original'] = False
        s.validate_source_report(row, context())
    def test_rejects_legacy_transport_impersonation(self):
        row=source_report();row['inputs_after']={'result':'passed'}
        with self.assertRaises(RuntimeError):s.validate_source_report(row,context())
    def test_wrong_or_missing_bindings_rejected(self):
        mutations=[lambda r:r.update(native_os={'runner':'windows-2022','build':20348,'product_type':3}),
            lambda r:r.update(repository_checkout_used=True),lambda r:r.update(application_archive_or_volumes_used=True),
            lambda r:r.update(post_run_errors=['bad']),lambda r:r.update(gpu_inference_verified=True),
            lambda r:r['source_after'].update(source_closure_sha256='b'*64),
            lambda r:(r['source_before'].update(embedded_header_sha256='b'*64),r['source_after'].update(embedded_header_sha256='b'*64)),
            lambda r:r['source_zip_before'].update(sha256='b'*64),lambda r:r['source_zip_after'].update(bytes=122),
            lambda r:r['rebuilt_exe'].update(sha256='b'*64),lambda r:r['rebuild_record'].update(bit_identical_to_original=False),
            lambda r:r['rebuild_record'].update(original_exe_sha256='b'*64),lambda r:r.update(raw_artifacts={}),
            lambda r:r['raw_artifacts']['rebuilt/link-map.txt'].update(sha256='b'*64),
            lambda r:r['rebuild_command'].remove('-S'),lambda r:r['toolchain_initialization'].update(toolset='14.45.12345'),
            lambda r:r['toolchain_initialization']['compiler'].update(sha256='b'*64)]
        for mutation in mutations:
            with self.subTest(mutation=mutations.index(mutation)):
                row=source_report();mutation(row)
                with self.assertRaises(RuntimeError):s.validate_source_report(row,context())
    def test_compile_full_gates_not_just_distributable_flag(self):
        mutations=[lambda r:r.update(synthetic_space_fixture=True),lambda r:r.update(synthetic_translation_units=[{}]),
            lambda r:r['command'].remove('/MT'),lambda r:r['command'].append('/MD'),lambda r:r['command'].append('/GL'),
            lambda r:r['pe_dependency_policy'].update(dependent_load_flags=0),lambda r:r['imports'].append('VCRUNTIME140.DLL'),
            lambda r:r['restricted_zlib']['final_link_map'].update(required_inflate_symbols_present=False),
            lambda r:r['restricted_zlib']['final_link_map'].update(excluded_gzwrite_deflate_crc_archive_symbols_absent=False),
            lambda r:r['restricted_zlib']['final_link_map'].update(synthetic_space_shim_present=True),
            lambda r:r['restricted_zlib']['compiled_objects'].pop(),lambda r:r['toolchain'].update(actual_sdk_headers=[]),
            lambda r:r['toolchain']['tools']['cl'].update(sha256='x')]
        s.validate_compile(compile_record())
        for mutation in mutations:
            with self.subTest(mutation=mutations.index(mutation)):
                row=compile_record();mutation(row)
                with self.assertRaises(RuntimeError):s.validate_compile(row)
    def test_missing_raw_artifact_fails_even_report_claims_good(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises((RuntimeError,FileNotFoundError)):s.validate_source_report(source_report(),context(),Path(root))
    def test_source_bootstrap_no_bytecode_and_no_third_party_imports(self):
        require_stdlib_imports((HERE/'source_only.py').read_text())
        self.assertIn('sys.dont_write_bytecode = True',(HERE/'source_only.py').read_text())
    def test_stdlib_scan_checks_ImportFrom_and_all_Import_aliases(self):
        for source in ('import os, nonstdlib_bootstrap_dependency',
                       'from nonstdlib_bootstrap_dependency.submodule import value',
                       'def nested():\n    import os, nonstdlib_bootstrap_dependency as alias'):
            with self.subTest(source=source),self.assertRaises(RuntimeError):require_stdlib_imports(source)
    def test_stdlib_scan_rejects_relative_imports(self):
        for source in ('from . import unknown','from ..pathlib import Path'):
            with self.subTest(source=source),self.assertRaises(RuntimeError):require_stdlib_imports(source)
    def test_stdlib_scan_accepts_all_supported_stdlib_import_forms(self):
        self.assertEqual(require_stdlib_imports('import os, sys as system\nfrom pathlib import Path, PurePath\nfrom importlib.util import spec_from_file_location'),{'os','sys','pathlib','importlib'})
    def test_safe_child_environment_drops_tokens_and_compiler_injection(self):
        with mock.patch.dict(os.environ,{'TOKEN':'hidden','GITHUB_TOKEN':'hidden','SECRET':'hidden','CL':'/evil','PATH':'safe'},clear=True):
            self.assertEqual(s.clean_environment(),{'PATH':'safe','PYTHONUTF8':'1','PYTHONDONTWRITEBYTECODE':'1'})


class ExtractionContract(ConfiguredCase):
    def archive(self,root,entries):
        path=root/'input.zip'
        with zipfile.ZipFile(path,'w') as archive:
            for name,attrs,data in entries:
                row=zipfile.ZipInfo(name);row.external_attr=attrs;archive.writestr(row,data)
        return path
    def test_exact_plain_source_extract(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); archive=self.archive(root,[('a/x.py',0o100644<<16,b'good')])
            s.unpack(archive,root/'out',limit=4,expected={'a/x.py':{'bytes':4,'sha256':s.digest(b'good')}})
            self.assertEqual((root/'out/a/x.py').read_bytes(),b'good')
    def test_unsafe_zip_cases_rejected_before_output(self):
        cases=[[('../escape',0,b'a')],[('a',0x400,b'a')],[('a',stat.S_IFLNK<<16,b'x')],
            [('a',0,b'a'),('A',0,b'b')],[('a',0,b'a'),('a/b',0,b'b')],
            [('e\u0301',0,b'a'),('é',0,b'b')],[('CON.txt',0,b'a')],[('a/',0,b'a')]]
        for entries in cases:
            with self.subTest(entries=entries),tempfile.TemporaryDirectory() as temp:
                root=Path(temp);archive=self.archive(root,entries)
                with self.assertRaises(RuntimeError):s.unpack(archive,root/'out',limit=100)
                self.assertFalse((root/'out').exists())
    def test_expansion_inventory_and_hash_bounds(self):
        for limit,expected in [(3,None),(4,{}),(4,{'a':{'bytes':4,'sha256':'0'*64}})]:
            with self.subTest(limit=limit,expected=expected),tempfile.TemporaryDirectory() as temp:
                root=Path(temp);archive=self.archive(root,[('a',0,b'good')])
                with self.assertRaises(RuntimeError):s.unpack(archive,root/'out',limit=limit,expected=expected)
    def test_existing_destination_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);archive=self.archive(root,[('a',0,b'good')]);out=root/'out';out.mkdir();(out/'user').write_bytes(b'user')
            with self.assertRaises(FileExistsError):s.unpack(archive,out,limit=4)
            self.assertEqual((out/'user').read_bytes(),b'user')
    def test_space_failure_does_not_remove_anything(self):
        with tempfile.TemporaryDirectory() as temp,mock.patch.object(s.shutil,'disk_usage',return_value=type('Usage',(),{'free':0})()):
            root=Path(temp);archive=self.archive(root,[('a',0,b'good')])
            with self.assertRaises(RuntimeError):s.unpack(archive,root/'out',limit=4)
            self.assertTrue(archive.exists());self.assertFalse((root/'out').exists())


class CleanupContract(ConfiguredCase):
    def setUp(self):
        super().setUp()
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.runner=Path(self.temp.name)
        env=mock.patch.dict(os.environ,{'RUNNER_TEMP':str(self.runner),'GITHUB_RUN_ID':'123','GITHUB_RUN_ATTEMPT':'1','GITHUB_JOB':'consume-test'});env.start();self.addCleanup(env.stop)
        self.work=d.fresh_work(self.runner/'work');self.outer=self.work/'transport-outer.zip';self.outer.write_bytes(b'outer')
        self.extracted=self.work/'delivery';self.extracted.mkdir();(self.extracted/'part').write_bytes(b'part')
        self.index={'files':d.files(self.extracted)};s.write_json(self.extracted/'delivery-index.json',self.index)
        self.evidence=self.work/'transport-download-and-extract.json'
        self.document={'scope':'verified-redundant-transport-outer','whole_outer_sha256_verified':True,
            'all_extracted_members_verified':True,'outer':s.file_record(self.outer),'index_sha256':s.digest(s.canonical(self.index)),
            'extracted_members':d.files(self.extracted),'upstream_artifact':{'id':123,'digest':'sha256:'+H}}
        s.write_json(self.evidence,self.document)
    def remove(self,**kw):
        args=dict(temp_root=self.work,evidence_path=self.evidence,extraction_root=self.extracted,index=self.index);args.update(kw)
        d.safe_remove_verified_transport_outer(self.outer,**args)
    def change_evidence(self,key,value):
        self.document[key]=value;self.evidence.write_text(json.dumps(self.document))
    def test_only_exact_verified_outer_removed(self):
        before=d.files(self.extracted);self.remove()
        self.assertFalse(self.outer.exists());self.assertEqual(before,d.files(self.extracted));self.assertTrue(self.evidence.exists())
        audit=s.read_json(self.work/'transport-outer-removal.json');self.assertFalse(audit['parts_exe_source_and_reports_deleted'])
    def test_no_persisted_evidence_rejected(self):
        self.evidence.unlink()
        with self.assertRaises(FileNotFoundError):self.remove()
        self.assertTrue(self.outer.exists())
    def test_incomplete_validation_rejected(self):
        for field in ('whole_outer_sha256_verified','all_extracted_members_verified'):
            with self.subTest(field=field):
                self.change_evidence(field,False)
                with self.assertRaises(RuntimeError):self.remove()
                self.change_evidence(field,True)
        self.assertTrue(self.outer.exists())
    def test_changed_extraction_or_outer_rejected(self):
        (self.extracted/'part').write_bytes(b'other')
        with self.assertRaises(RuntimeError):self.remove()
        self.assertTrue(self.outer.exists())
    def test_wrong_owner_rejected(self):
        with mock.patch.dict(os.environ,{'GITHUB_JOB':'other-job'}):
            with self.assertRaises(RuntimeError):self.remove()
        self.assertTrue(self.outer.exists())
    def test_unowned_and_runner_root_rejected(self):
        (self.work/'job-owner.json').unlink()
        with self.assertRaises(FileNotFoundError):self.remove()
        with self.assertRaises(RuntimeError):d.fresh_work(self.runner)
        with self.assertRaises(RuntimeError):d.private_location(self.runner/'..'/'outside')
    def test_original_candidate_outer_never_removed(self):
        original=self.work/'original-canonical-outer.zip';self.outer.rename(original);self.outer=original
        with self.assertRaises(RuntimeError):self.remove()
        self.assertTrue(original.exists())
    def test_outside_runner_temp_rejected(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            with self.assertRaises(RuntimeError):d.fresh_work(Path(elsewhere)/'work')
    def test_hardlink_rejected(self):
        os.link(self.outer,self.work/'linked')
        with self.assertRaises(RuntimeError):self.remove()
        self.assertTrue(self.outer.exists())
    def test_reparse_attribute_rejected(self):
        actual=self.outer.lstat();fake=type('Info',(),{'st_mode':actual.st_mode,'st_file_attributes':0x400})()
        with mock.patch.object(Path,'lstat',return_value=fake):
            with self.assertRaises(RuntimeError):self.remove()
    @unittest.skipIf(os.name=='nt','Actual POSIX symlink fixture only; Windows reparse rejection separately mocked without privilege')
    def test_symlink_ancestor_rejected(self):
        link=self.runner/'alias';link.symlink_to(self.work,target_is_directory=True)
        with self.assertRaises(RuntimeError):d.check_owned_work(link)
    def test_no_removal_without_upstream_identity(self):
        self.change_evidence('upstream_artifact',{})
        with self.assertRaises(RuntimeError):self.remove()
        self.assertTrue(self.outer.exists())

    def test_same_real_parent_with_different_83_display_is_accepted(self):
        original=Path.resolve;canonical=self.runner/'canonical-long-name'
        def resolve(path,*args,**kwargs):
            if path==self.work:return canonical
            if path==self.extracted:return canonical/'delivery'
            return original(path,*args,**kwargs)
        with mock.patch.object(Path,'resolve',resolve):self.remove()
        self.assertFalse(self.outer.exists());self.assertTrue((self.extracted/'part').exists())
    def test_different_resolved_parent_still_rejected(self):
        original=Path.resolve
        def resolve(path,*args,**kwargs):
            if path==self.extracted:return self.runner/'different-parent'/'delivery'
            return original(path,*args,**kwargs)
        with mock.patch.object(Path,'resolve',resolve):
            with self.assertRaises(RuntimeError):self.remove()
        self.assertTrue(self.outer.exists())


class ArtifactAndPlanContract(ConfiguredCase):
    def test_metadata_strict_identity_expiry_and_full_digest(self):
        good={'id':1,'name':'n','expired':False,'workflow_run':{'id':2,'head_sha':fixture_pins()['commit']},
            'size_in_bytes':3,'digest':'sha256:'+H,'expires_at':'2099-01-01T00:00:00Z'}
        with mock.patch.object(d,'api',return_value=good):
            self.assertEqual(d.artifact_metadata(1,'t',name='n',run_id=2,commit=fixture_pins()['commit']),good)
        for field,value in [('id',2),('name','other'),('expired',True),('size_in_bytes',0),('digest','missing'),('expires_at','2000-01-01T00:00:00Z')]:
            row=copy.deepcopy(good);row[field]=value
            with self.subTest(field=field),mock.patch.object(d,'api',return_value=row):
                with self.assertRaises((RuntimeError,ValueError)):d.artifact_metadata(1,'t',name='n',run_id=2,commit=fixture_pins()['commit'])
    def test_api_rejects_nonrepository_paths(self):
        with self.assertRaises(RuntimeError):d.api('https://evil.invalid','secret')
        self.assertIsNone(d.NoRedirect().redirect_request(None,None,None,None,None,None))
    def test_complete_reviewed_binding_is_exact_and_not_fixed_to_attempt2(self):
        self.assertEqual(d.load_pins(),fixture_pins())
        self.assertEqual(d.load_pins()['run_attempt'],7)
    def test_collection_missing_failed_skipped_jobs_rejected(self):
        with self.assertRaises(RuntimeError):d.collection_plan({})
        keys={'produce-'+x for x in ('cu118','cu126')}|{'rebuild-'+x for x in ('cu118','cu126')}|{'consume-'+x+'-'+y for x in ('cu118','cu126') for y in ('2022','2025')}
        for status in ('failure','skipped','cancelled'):
            needs={key:{'result':'success'} for key in keys};needs['consume-cu126-2025']={'result':status}
            with self.subTest(status=status),self.assertRaises(RuntimeError):d.collection_plan(needs)
    def test_delivery_inventory_excludes_original_zip_objects_and_no_duplicate_parts(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'volumes').mkdir();(root/'build/distribution').mkdir(parents=True)
            for name in ('handoff.json','producer-context.json','volumes/archive.part001','build/distribution/restore.exe','build/restorer.obj','producer.log'):
                (root/name).write_bytes(b'fixed')
            inventory=d.delivery_files(root)
            self.assertEqual(set(inventory),{'handoff.json','producer-context.json','volumes/archive.part001','build/distribution/restore.exe'})
        self.assertNotIn('copytree',(HERE/'driver.py').read_text())


class WorkflowContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow=(HERE.parents[1]/'.github/workflows/windows-restorer-distribution.yml').read_text()
    def test_exact_nine_explicit_jobs_manual_only(self):
        jobs=re.findall(r'^  ([a-z0-9-]+):$',self.workflow.split('jobs:\n',1)[1],re.M)
        self.assertEqual(len(jobs),9);self.assertEqual(len(set(jobs)),9)
        self.assertIn('fix/windows-restorer',self.workflow);self.assertIn('workflow_dispatch:',self.workflow);self.assertFalse('  push:' in self.workflow)
        self.assertIn('cancel-in-progress: false',self.workflow);self.assertNotIn('pull_request',self.workflow)
        self.assertNotIn('contents: write',self.workflow);self.assertNotIn('actions: write',self.workflow)
    def test_source_job_has_no_checkout_and_verbatim_bootstrap(self):
        for short in ('cu118','cu126'):
            block=self.workflow.split('  rebuild-'+short+':\n',1)[1].split('\n  ',1)[0] if False else re.search(r'^  rebuild-'+short+r':\n(.*?)(?=^  [a-z0-9-]+:|\Z)',self.workflow,re.M|re.S).group(1)
            self.assertNotIn('actions/checkout',block);self.assertNotIn('pip install',block);self.assertIn('name: restorer-final-source-',block);self.assertNotIn('artifact-ids:',block)
            needle='        #!/usr/bin/env python3\n';start=block.index(needle);end=block.index('\n    - name: Preserve',start)
            embedded='\n'.join(line[8:] for line in block[start:end].splitlines())+'\n'
            self.assertEqual(embedded,(HERE/'source_only.py').read_text())
    def test_prepared_allowlist_and_actual_report_raw_retention(self):
        self.assertIn('/prepared/volumes/**',self.workflow);self.assertIn('/prepared/build/distribution/**',self.workflow)
        self.assertNotIn('/prepared/**',self.workflow);self.assertNotIn('/restorer-final-collector-evidence/**',self.workflow)
        self.assertEqual(self.workflow.count('ref: '+json.loads((HERE/'pins.json').read_text())['commit']),7)
        self.assertEqual(self.workflow.count('Same producer EXE /'),4)
        self.assertEqual(self.workflow.count('One production EXE /'),2)
        self.assertEqual(self.workflow.count('raw_artifact_id:'),8)
        self.assertIn('if: always()',self.workflow);self.assertIn('default: false',self.workflow)
        self.assertNotIn('Bypass',self.workflow);self.assertNotIn('taskkill',self.workflow);self.assertNotIn('Get-Process',self.workflow)
    def test_local_attributes_preserve_fixed_bytes(self):
        self.assertEqual((HERE/'.gitattributes').read_text(),'*.py -text\n*.json -text\n')
    def test_python_syntax_every_embedded_run(self):
        runs=re.findall(r'^      run: \|\n((?:^        .*\n|^\n)+)',self.workflow,re.M)
        self.assertGreaterEqual(len(runs),9)
        for run in runs:ast.parse('\n'.join(line[8:] if line.startswith('        ') else line for line in run.splitlines()))



class AdditionalEvidenceContract(ConfiguredCase):
    def test_raw_source_artifacts_bound_to_actual_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'rebuilt').mkdir();row=source_report();record=row['rebuild_record']
            (root/'rebuilt/link-map.txt').write_bytes(b'actual map')
            record['restricted_zlib']['final_link_map'].update(s.file_record(root/'rebuilt/link-map.txt'))
            (root/'rebuilt/compiler.log').write_text(record['toolchain']['compiler_bv_and_link_log'],encoding='utf-8')
            s.write_json(root/'rebuilt/rebuild-record.json',record);s.write_json(root/'rebuilt/compile-command-record.json',{'command':record['command']})
            (root/'rebuild.log').write_bytes(b'actual bootstrap stdout')
            row['raw_artifacts']={name:s.file_record(root/name) for name in row['raw_artifacts']}
            s.validate_source_report(row,context(),root)
            (root/'rebuilt/link-map.txt').write_bytes(b'changed map')
            with self.assertRaises(RuntimeError):s.validate_source_report(row,context(),root)
    def test_download_token_not_forwarded_and_whole_outer_sha_verified(self):
        import io
        import urllib.error
        data=b'outer';meta={'id':17,'size_in_bytes':len(data),'digest':'sha256:'+s.digest(data)}
        redirect=urllib.error.HTTPError('https://api.github.com',302,'redirect',{'Location':'https://storage.invalid/outer?signature=private'},None)
        opener=mock.Mock();opener.open.side_effect=redirect
        with tempfile.TemporaryDirectory() as temp,mock.patch.object(d.urllib.request,'build_opener',return_value=opener),mock.patch.object(d.urllib.request,'urlopen',return_value=io.BytesIO(data)) as get:
            result=d.download_artifact(meta,Path(temp)/'outer.zip','token-secret')
            get.assert_called_once_with('https://storage.invalid/outer?signature=private',timeout=120)
            self.assertNotIn('token-secret',json.dumps(result));self.assertNotIn('signature',json.dumps(result))
            self.assertTrue(result['whole_outer_sha256_verified'])
        meta['digest']='sha256:'+'0'*64
        with tempfile.TemporaryDirectory() as temp,mock.patch.object(d.urllib.request,'build_opener',return_value=opener),mock.patch.object(d.urllib.request,'urlopen',return_value=io.BytesIO(data)):
            with self.assertRaises(RuntimeError):d.download_artifact(meta,Path(temp)/'outer.zip','token-secret')
    def test_success_outputs_only_after_producer_after_checks(self):
        tree=ast.parse((HERE/'driver.py').read_text());function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='producer')
        final_try=next(n for n in function.body if isinstance(n,ast.Try))
        calls=[n for n in ast.walk(function) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='output_values']
        self.assertEqual(len(calls),1);self.assertGreater(calls[0].lineno,final_try.end_lineno)
    def test_exact_phase_budget_does_not_require_deleted_outer_during_restore(self):
        tree=ast.parse((HERE/'driver.py').read_text());function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='acquire_delivery')
        source=ast.get_source_segment((HERE/'driver.py').read_text(),function)
        self.assertIn('max(metadata[\'size_in_bytes\'] + expected_bytes, expected_bytes + extra) + s.RESERVE',source)
        self.assertLess(source.index('safe_remove_verified_transport_outer('),source.index("post-removal-restore-budget.json"))

if __name__=='__main__':unittest.main()
