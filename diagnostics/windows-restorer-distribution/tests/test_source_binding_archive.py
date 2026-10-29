# SPDX-License-Identifier: Apache-2.0
"""Real source-ZIP bytes with MOCK build records; never an MSVC/Server claim."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from binding_fixtures import configured,fixture_pins,H

HERE=Path(__file__).resolve().parents[1]

def declared_source_pin():
    # The reviewed input declaration is the oracle, never the checkout under test.
    selected=json.loads((HERE/'pins.json').read_bytes())
    pin=fixture_pins();pin['commit']=selected['commit'];pin['version']=selected['version']
    return pin
from test_distribution import d,s,context,compile_record

class SourceArchiveBinding(unittest.TestCase):
    def setUp(self):
        self.source=Path(os.environ.get('VSE_REVIEW_SOURCE','application-source')).resolve()
        pin=declared_source_pin()
        self.enterContext(configured(d,pin));self.pin=pin
        d.checked_source(self.source)  # Authenticate fixed selected SHA before importing source helpers.
        self.helper=s.load_exact(self.source/'native/windows-restorer/source_bundle.py','binding_test_source_bundle')
        self.build=s.load_exact(self.source/'native/windows-restorer/build.py','binding_test_build')
    def archive(self,root,mutate=lambda doc:None,changed_notice=False,other_notice=None):
        snapshot=self.helper.snapshot_sources(self.source)
        notice_dir=root/'notices';self.build.copy_notices(None,notice_dir)
        notices={p.name:p.read_bytes() for p in notice_dir.iterdir()}
        if other_notice is not None:notices[other_notice]=b'changed third-party notice'
        if changed_notice:
            notices[self.pin['project_notice']['name']]=b'changed project notice'
        profile='paddle-cuda11.8';row=self.pin['profiles'][profile]
        document={'archive':{'name':row['inner_zip'],'size':row['inner_zip_bytes'],'sha256':row['inner_zip_sha256'],'uncompressed_bytes':row['expanded_bytes'],
            'identity':{'commit':self.pin['commit'],'version':self.pin['version'],'build_profile':profile}},
            'receipt':{'name':row['inner_zip']+'.acceptance.json','size':row['receipt_bytes'],'sha256':row['receipt_sha256']},
            'checksum':{'name':row['inner_zip']+'.sha256','size':row['checksum_bytes'],'sha256':row['checksum_sha256']}}
        mutate(document);transport=(json.dumps(document)+'\n').encode();header=b'// OFFLINE MOCK FIXED HEADER\n'
        record=compile_record();record.update(git={'commit':self.pin['commit'],'dirty':False},git_after_build={'commit':self.pin['commit'],'dirty':False},
            embedded_header_sha256=s.digest(header),generator_dependencies={'offline_fixture':True},
            archive_transport_manifest_sha256=s.digest(transport),sources={n:s.digest(v) for n,v in snapshot.items()},
            notices={n:s.digest(v) for n,v in notices.items()})
        archive=root/'source.zip';self.helper.create_source_bundle(archive,sources=snapshot,generated_header=header,notices=notices,build_record=record,transport_manifest=transport)
        tree=root/'source';s.unpack(archive,tree,limit=s.SOURCE_LIMIT)
        ctx=context();ctx.update(source_zip_bytes=archive.stat().st_size,source_zip_sha256=s.sha(archive),
            embedded_header_sha256=record['embedded_header_sha256'],generator_identity_sha256=s.digest(s.canonical(record['generator_dependencies'])),
            source_closure_sha256=s.digest(s.canonical(record['sources'])),source_manifest_sha256=s.sha(tree/'source-manifest.json'),
            original_build_record_sha256=s.sha(tree/'evidence/original-build-record.json'))
        return tree,ctx
    def test_actual_current_source_closure_and_four_notices_validate_offline(self):
        with tempfile.TemporaryDirectory() as temp:
            tree,ctx=self.archive(Path(temp));r=s.verify_source_inputs(tree,ctx)
            self.assertEqual(r['result'],'passed');self.assertEqual(len(list((tree/'notices').iterdir())),4)
    def test_self_consistent_source_zip_cannot_swap_bound_transport_identity(self):
        changes=[lambda x:x['archive']['identity'].update(commit='b'*40),lambda x:x['archive']['identity'].update(version='2.3.0'),
            lambda x:x['archive']['identity'].update(build_profile='paddle-cuda12.6'),lambda x:x['archive'].update(sha256='b'*64),
            lambda x:x['archive'].update(size=799),lambda x:x['receipt'].update(sha256='b'*64),
            lambda x:x['checksum'].update(sha256='b'*64),lambda x:x['archive'].update(name='other.zip'),
            lambda x:x['archive'].update(uncompressed_bytes=1),lambda x:x['receipt'].update(name='fake.json')]
        for change in changes:
            with self.subTest(change=changes.index(change)),tempfile.TemporaryDirectory() as temp:
                tree,ctx=self.archive(Path(temp),change)
                with self.assertRaises(RuntimeError):s.verify_source_inputs(tree,ctx)
    def test_self_consistent_source_zip_cannot_replace_project_notice(self):
        with tempfile.TemporaryDirectory() as temp:
            tree,ctx=self.archive(Path(temp),changed_notice=True)
            with self.assertRaises(RuntimeError):s.verify_source_inputs(tree,ctx)

    def test_other_required_notices_cannot_be_rewritten_even_with_matching_manifests(self):
        for name in ['Apache-2.0.txt','THIRD_PARTY_NOTICES.txt','zlib-LICENSE.txt']:
            with self.subTest(name=name),tempfile.TemporaryDirectory() as temp:
                tree,ctx=self.archive(Path(temp),other_notice=name)
                with self.assertRaises(RuntimeError):s.verify_source_inputs(tree,ctx)

class RealGitBinding(unittest.TestCase):
    def test_actual_declared_target_clean_checkout_matches_fixed_config(self):
        source=Path(os.environ.get('VSE_REVIEW_SOURCE','application-source')).resolve();pin=declared_source_pin()
        with configured(d,pin):self.assertEqual(d.checked_source(source),source)
    def git(self,root,*args):
        return subprocess.check_output(['git','-c','user.name=jason','-c','user.email=eritpchy@qq.com',*args],cwd=root,stderr=subprocess.PIPE,text=True)
    def test_arbitrary_clean_HEAD_is_not_declared_target(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);self.git(root,'init');(root/'file').write_text('arbitrary clean source')
            self.git(root,'add','file');self.git(root,'commit','-m','offline foreign source fixture')
            self.assertFalse(self.git(root,'status','--porcelain').strip())
            pin=declared_source_pin();self.assertNotEqual(self.git(root,'rev-parse','HEAD').strip(),pin['commit'])
            with configured(d,pin),self.assertRaises(RuntimeError):d.checked_source(root)
    def test_synthetic_git_dirty_untracked_or_foreign_head_rejected(self):
        for kind in ['dirty','untracked','foreign']:
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as temp:
                root=Path(temp);self.git(root,'init');(root/'file').write_text('original');self.git(root,'add','file');self.git(root,'commit','-m','offline binding fixture')
                pin=fixture_pins();pin['commit']=self.git(root,'rev-parse','HEAD').strip()
                if kind=='dirty':(root/'file').write_text('changed')
                elif kind=='untracked':(root/'untracked').write_text('new')
                else:pin['commit']='b'*40
                with configured(d,pin),self.assertRaises(RuntimeError):d.checked_source(root)

if __name__=='__main__':unittest.main()
