# SPDX-License-Identifier: Apache-2.0
"""Real small OFFLINE Git fixtures; no Server/native-build certification."""
import copy
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
import zipfile
from binding_fixtures import configured, fixture_pins
from test_distribution import d
from test_source_binding_archive import declared_source_pin


def conversion(value):
    return mock.patch.dict(os.environ, GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='core.autocrlf',
                           GIT_CONFIG_VALUE_0=value)


class CanonicalGitSources(unittest.TestCase):
    def git(self, root, *args):
        return subprocess.check_output(['git', '-c', 'user.name=jason', '-c', 'user.email=eritpchy@qq.com',
                                        '-C', str(root), *args], stderr=subprocess.PIPE, text=True).strip()

    def fixture(self, root):
        source=Path(os.environ.get('VSE_REVIEW_SOURCE','application-source')).resolve()
        with configured(d, declared_source_pin()):
            _,_,snapshot,_=d.authenticated_source_snapshot(source)
        self.git(root,'init')
        for name,(raw,_) in snapshot.items():
            path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
        (root/'.gitattributes').write_bytes((source/'.gitattributes').read_bytes())
        self.git(root,'-c','core.autocrlf=false','add','--all')
        self.git(root,'commit','-m','OFFLINE canonical source fixture')
        pins=fixture_pins();pins['commit']=self.git(root,'rev-parse','HEAD')
        return pins,snapshot

    def test_real_clean_CRLF_checkout_is_rejected_and_job_scoped_LF_passes(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);pins,snapshot=self.fixture(root)
            name='native/windows-restorer/core.hpp';path=root/name;raw=snapshot[name][0]
            path.unlink()
            with conversion('true'):
                self.git(root,'checkout-index','-u','--force','--',name)
                self.assertEqual(path.read_bytes(),raw.replace(b'\n',b'\r\n'))
                self.assertEqual(self.git(root,'status','--porcelain'),'')
                with configured(d,pins),self.assertRaisesRegex(RuntimeError,'checkout differs'):
                    d.canonicalize_source_checkout(root)
                self.assertEqual(path.read_bytes(),raw.replace(b'\n',b'\r\n'))
            path.unlink()
            with conversion('false'),configured(d,pins):
                self.git(root,'checkout-index','-u','--force','--',name)
                self.assertEqual(path.read_bytes(),raw)
                proof=d.canonicalize_source_checkout(root)
                self.assertEqual(proof['file_count'],55);self.assertFalse(proof['source_files_written'])
                self.assertEqual(proof['tree'],self.git(root,'rev-parse','HEAD^{tree}'))
                self.assertEqual(d.verify_canonical_source_audit(root,proof)['file_count'],55)
                self.assertEqual(self.git(root,'status','--porcelain'),'')
            self.assertEqual(self.git(root,'config','--local','--list').count('core.autocrlf='),0)

    def test_material_difference_rejected_without_repair_or_build(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);pins,_=self.fixture(root)
            changed=root/'native/windows-restorer/restorer.cpp';raw=changed.read_bytes()+b'\n// material mutation\n';changed.write_bytes(raw)
            self.git(root,'update-index','--assume-unchanged','--','native/windows-restorer/restorer.cpp')
            with conversion('false'),configured(d,pins),self.assertRaisesRegex(RuntimeError,'checkout differs'):
                d.canonicalize_source_checkout(root)
            self.assertEqual(changed.read_bytes(),raw)

    def test_audit_empty_duplicate_wrong_tree_blob_and_hash_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);pins,_=self.fixture(root)
            with conversion('false'),configured(d,pins):
                good=d.canonicalize_source_checkout(root)
                mutations=[lambda x:x.update(files=[]),lambda x:x['files'].__setitem__(1,copy.deepcopy(x['files'][0])),
                           lambda x:x.update(tree='a'*40),lambda x:x['files'][0].update(git_blob='a'*40),
                           lambda x:x['files'][0]['after'].update(sha256='a'*64)]
                for change in mutations:
                    bad=copy.deepcopy(good);change(bad)
                    with self.subTest(change=mutations.index(change)),self.assertRaises(RuntimeError):
                        d.verify_canonical_source_audit(root,bad)

    def test_sourceZIP_requires_all_literal_blobs_not_just_normalized_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'repo';root.mkdir();pins,snapshot=self.fixture(root)
            for mutation in ('none','CRLF','material','missing'):
                archive=Path(temp)/(mutation+'.zip')
                with zipfile.ZipFile(archive,'w') as z:
                    for name,(raw,_) in snapshot.items():
                        if name=='native/windows-restorer/core.hpp':
                            if mutation=='missing':continue
                            if mutation=='CRLF':raw=raw.replace(b'\n',b'\r\n')
                            if mutation=='material':raw+=b'bad'
                        z.writestr(name,raw)
                with conversion('false'),configured(d,pins):
                    if mutation=='none':self.assertEqual(d.verify_source_zip_git_membership(root,archive)['file_count'],55)
                    else:
                        with self.assertRaises((RuntimeError,KeyError)):d.verify_source_zip_git_membership(root,archive)

    def test_wrong_target_and_corrupt_raw_commit_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);pins,_=self.fixture(root)
            with configured(d,dict(pins,commit='b'*40)),self.assertRaisesRegex(RuntimeError,'Exact clean'):
                d.authenticated_source_snapshot(root)
            original=d.source_git_bytes
            def corrupt(path,*args):
                raw=original(path,*args)
                return raw+b'wrong' if args[:2]==('cat-file','commit') else raw
            with configured(d,pins),mock.patch.object(d,'source_git_bytes',side_effect=corrupt),self.assertRaisesRegex(RuntimeError,'object bytes'):
                d.authenticated_source_snapshot(root)

    def test_clean_toolchain_child_keeps_only_fixed_Git_configuration(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);self.fixture(root)
            self.git(root,'config','core.autocrlf','true')  # Simulated Windows runner default, fixture only.
            safe={key:value for key,value in os.environ.items() if not key.startswith('GIT_')}
            report={}
            with conversion('true'),mock.patch.object(d.s,'initialize_msvc',return_value=safe):
                env=d.canonical_build_environment(report)
            self.assertEqual({key:value for key,value in env.items() if key.startswith('GIT_')},
                             {'GIT_CONFIG_COUNT':'1','GIT_CONFIG_KEY_0':'core.autocrlf','GIT_CONFIG_VALUE_0':'false'})
            status=subprocess.check_output(['git','-C',str(root),'status','--porcelain'],env=env,text=True)
            self.assertEqual(status,'')
            self.assertFalse(report['build_git_command_configuration']['persistent_config_written'])

    def test_workflow_job_configuration_precedes_checkout_in_both_producers(self):
        workflow=(d.HERE.parents[1]/'.github/workflows/windows-restorer-distribution.yml').read_text()
        self.assertEqual(workflow.count("'canonical-source'"),2)
        self.assertEqual(workflow.count("GIT_CONFIG_VALUE_0: 'false'"),2)
        for profile in ('118','126'):
            block=workflow.split('  produce-cu'+profile+':',1)[1]
            block=block.split('\n  consume-',1)[0].split('\n  source-',1)[0]
            self.assertLess(block.index("GIT_CONFIG_VALUE_0: 'false'"),block.index('uses: actions/checkout@v4'))
            self.assertLess(block.index("'canonical-source'"),block.index('Offline diagnostic contracts before large input download'))
