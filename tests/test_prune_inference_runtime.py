import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import pytest

HELPER = Path(__file__).with_name('prune_inference_runtime.py')
if not HELPER.exists():
    HELPER = Path(__file__).resolve().parents[1] / 'scripts/prune_inference_runtime.py'
spec = importlib.util.spec_from_file_location('prune_inference_runtime', HELPER)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def fixture_runtime(tmp_path):
    root = tmp_path / 'site-packages'; root.mkdir()
    info = root / 'numpy-2.3.5.dist-info'; info.mkdir()
    (info / 'METADATA').write_text('Name: numpy\nVersion: 2.3.5\n')
    files = {'numpy/tests/test_sample.py': b'assert True\n', 'numpy/tests/THIRD_PARTY.txt': b'Attribution\n',
             'numpy/tests/LICENSE': b'License\n', 'numpy/module.py': b'VALUE=1\n'}
    rows = []
    for rel, content in files.items():
        path = root / rel; path.parent.mkdir(parents=True, exist_ok=True);path.write_bytes(content)
        hashed = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip('=')
        rows.append(f'{rel},sha256={hashed},{len(content)}')
    (info / 'RECORD').write_text('\n'.join(rows))
    return root


def test_plan_apply_verify_preserves_attribution_and_metadata(tmp_path):
    root = fixture_runtime(tmp_path)
    plan = m.build_plan(root)
    assert [x['path'] for x in plan['removed']] == ['numpy/tests/test_sample.py']
    original = (root / 'numpy-2.3.5.dist-info/RECORD').read_bytes()
    output = tmp_path / 'manifest.json'
    m.apply_plan(root, plan, output)
    assert m.verify(root, json.loads(output.read_text()))
    assert (root / 'numpy-2.3.5.dist-info/RECORD').read_bytes() == original
    (root / 'numpy/module.py').write_text('TAMPERED')
    with pytest.raises(ValueError, match='changed'):m.verify(root, plan)


def test_reject_changed_candidate_before_deletion(tmp_path):
    root = fixture_runtime(tmp_path)
    (root/'numpy/tests/test_sample.py').write_text('changed')
    with pytest.raises(ValueError, match='changed candidate'):m.build_plan(root)


def test_reject_symlinks(tmp_path):
    root = fixture_runtime(tmp_path)
    (root/'linked').symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match='Symlink'):m.build_plan(root)


def test_unknown_version_keeps_tests(tmp_path):
    root=fixture_runtime(tmp_path)
    (root/'numpy-2.3.5.dist-info/METADATA').write_text('Name: numpy\nVersion: 999\n')
    assert not m.build_plan(root)['removed']


def test_plugin_requires_reviewed_platform_and_vendor(tmp_path, monkeypatch):
    root=fixture_runtime(tmp_path)
    monkeypatch.setattr(m.platform,'system',lambda:'Windows')
    with pytest.raises(ValueError, match='Linux x86_64'):m.build_plan(root,True)
    for rel in ['paddle/libs/libmklml_intel.so','paddle/libs/libdnnl.so.3','paddle/base/libpaddle.so']:
        assert m.reason_for(rel,{'paddlepaddle':'3.3.1'},True) is None


def test_unsafe_manifest_path(tmp_path):
    with pytest.raises(ValueError, match='Unsafe'):m.child_path(tmp_path,'../escape')


def test_no_partial_delete_on_changed_plan(tmp_path):
    root=fixture_runtime(tmp_path);plan=m.build_plan(root)
    (root/'numpy/module.py').write_text('changed')
    with pytest.raises(ValueError, match='changed after planning'):m.apply_plan(root,plan,tmp_path/'manifest.json')
    assert (root/'numpy/tests/test_sample.py').exists()


def test_regenerated_cache_rejected(tmp_path):
    root=fixture_runtime(tmp_path);plan=m.build_plan(root)
    m.apply_plan(root,plan,tmp_path/'manifest.json')
    p=root/'numpy/__pycache__/module.pyc';p.parent.mkdir();p.write_bytes(b'cache')
    with pytest.raises(ValueError, match='file set changed'):m.verify(root,plan)


def test_repeat_apply_preserves_original_audit(tmp_path):
    root=fixture_runtime(tmp_path); manifest=tmp_path/'manifest.json'
    initial=m.build_plan(root); m.apply_plan(root,initial,manifest)
    m.apply_plan(root,m.build_plan(root),manifest)
    result=json.loads(manifest.read_text())
    assert result['removed']==initial['removed']
    assert result['removed_bytes']==initial['removed_bytes']
    assert m.verify(root,result)


def test_extend_manifest_merges_new_and_prior_removals(tmp_path):
    root=fixture_runtime(tmp_path); manifest=tmp_path/'manifest.json'
    # Two candidate assets, deliberately model first pass selecting only one.
    p=root/'numpy/__pycache__/module.pyc';p.parent.mkdir();p.write_bytes(b'bytecode')
    first=m.build_plan(root)
    later=next(e for e in first['removed'] if '__pycache__' in e['path'])
    first['removed'].remove(later)
    first['retained'].append({k:later[k] for k in ('path','size','sha256')})
    first['removed_bytes']-=later['size'];first['retained_bytes']+=later['size']
    m.apply_plan(root,first,manifest)
    m.apply_plan(root,m.build_plan(root),manifest)
    merged=json.loads(manifest.read_text())
    assert len(merged['removed'])==2
    assert merged['removed_bytes']==first['removed_bytes']+later['size']
    assert m.verify(root,merged)


def test_resume_interrupted_apply(tmp_path):
    root=fixture_runtime(tmp_path);plan=m.build_plan(root)
    # Simulate durable manifest written before interrupted first unlink.
    with pytest.raises(ValueError):m.verify(root,plan)
    m.resume_pending(root,plan)
    assert m.verify(root,plan)


def test_resume_refuses_mutated_pending_file_without_deleting(tmp_path):
    root=fixture_runtime(tmp_path);plan=m.build_plan(root)
    pending=root/'numpy/tests/test_sample.py';pending.write_text('tampered')
    with pytest.raises(ValueError,match='cannot resume'):m.resume_pending(root,plan)
    assert pending.exists()


def test_resume_refuses_mutated_retained_file_without_deleting(tmp_path):
    root=fixture_runtime(tmp_path);plan=m.build_plan(root)
    (root/'numpy/module.py').write_text('tampered')
    with pytest.raises(ValueError,match='cannot resume'):m.resume_pending(root,plan)
    assert (root/'numpy/tests/test_sample.py').exists()


def test_qt_development_and_orphan_pdf_plugins_are_version_guarded():
    for rel in ('PySide6/Qt/plugins/designer/libqwebengineview.so', 'PySide6/plugins/qmltooling/tool.dll', 'PySide6/Qt/plugins/imageformats/libqpdf.so', 'PySide6/plugins/imageformats/qpdf.dll'):
        assert m.reason_for(rel, {'pyside6-essentials': '6.12.0'})
        assert m.reason_for(rel, {'pyside6-essentials': '6.13.0'}) is None
    assert m.reason_for('PySide6/Qt/plugins/imageformats/libqjpeg.so', {'pyside6-essentials': '6.12.0'}) is None
