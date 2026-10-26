import json
from pathlib import Path
import pytest
from scripts.bundle_bootstrap import sha256
from scripts.native_package import stage_native_artifact, assert_native_requirements, safe_file
from scripts.package_release import create_bundle
from scripts.prune_inference_runtime import build_plan, verify


def fixture_artifact(root):
    (root / 'models').mkdir(parents=True)
    (root / 'config').mkdir()
    (root / 'licenses').mkdir()
    for name in ('native-ocr', 'libonnxruntime.so.1.31.0', 'dependency-source-manifest.json', 'source_manifest.json', 'licenses/LICENSE'):
        (root / name).write_bytes(b'verified fixture')
    rows=[]
    for index in range(15):
        name=f'model_{index}'
        model=root / 'models' / f'{name}.onnx';model.write_bytes(b'model')
        config=root / 'config' / f'{name}.json';config.write_text('{}')
        rows.append({'name':name,'onnx_sha256':sha256(model),'config_file':f'config/{name}.json','config_sha256':sha256(config)})
    (root / 'model-manifest.json').write_text(json.dumps(rows))
    files=[{'path':p.relative_to(root).as_posix(),'bytes':p.stat().st_size,'sha256':sha256(p)} for p in root.rglob('*') if p.is_file()]
    (root / 'size-audit.json').write_text(json.dumps({'files':files,'release_ready':False}))
    return root


def test_native_stage_complete_no_duplicate_ort(tmp_path):
    source=fixture_artifact(tmp_path / 'input');dest=tmp_path / 'output'
    report=stage_native_artifact(source,dest,'linux')
    assert report['model_count']==15
    assert (dest / 'libonnxruntime.so.1').is_file()
    assert not (dest / 'libonnxruntime.so.1.31.0').exists()
    assert not report['upstream_artifact_release_ready']
    assert not (dest / 'size-audit.json').exists()
    assert 'size-audit.json' not in report['files']


def fixture_windows_artifact(root):
    fixture_artifact(root)
    (root / 'native-ocr').rename(root / 'native-ocr.exe')
    (root / 'libonnxruntime.so.1.31.0').rename(root / 'onnxruntime.dll')
    for name in ('concrt140.dll', 'msvcp140.dll', 'msvcp140_1.dll', 'vcruntime140.dll', 'vcruntime140_1.dll'):
        (root / name).write_bytes(b'reviewed fixture ' + name.encode())
    files = [{'path': p.relative_to(root).as_posix(), 'bytes': p.stat().st_size, 'sha256': sha256(p)}
             for p in root.rglob('*') if p.is_file() and p.name != 'size-audit.json']
    (root / 'size-audit.json').write_bytes((json.dumps({'files': files, 'release_ready': False}, indent=3) + '\r\n').encode())
    return root


def test_windows_native_stage_preserves_original_inventory_bytes_and_outer_hash(tmp_path):
    source = fixture_windows_artifact(tmp_path / 'input')
    original = (source / 'size-audit.json').read_bytes()
    dest = tmp_path / 'output'
    report = stage_native_artifact(source, dest, 'windows')
    assert (dest / 'size-audit.json').read_bytes() == original
    assert report['files']['size-audit.json'] == sha256(source / 'size-audit.json')
    assert not any(row['path'] == 'size-audit.json' for row in json.loads(original)['files'])


@pytest.mark.parametrize('change', ['symlink', 'case-duplicate', 'self', 'malformed'])
def test_windows_native_stage_rejects_unsafe_inventory_before_publication(tmp_path, change):
    source = fixture_windows_artifact(tmp_path / 'input')
    path = source / 'size-audit.json'
    inventory = json.loads(path.read_bytes())
    if change == 'symlink':
        original = tmp_path / 'outside-inventory.json'
        original.write_bytes(path.read_bytes())
        path.unlink(); path.symlink_to(original)
    else:
        if change == 'case-duplicate':
            row = next(row for row in inventory['files'] if row['path'] == 'native-ocr.exe')
            inventory['files'].append({**row, 'path': 'NATIVE-OCR.EXE'})
        elif change == 'self':
            inventory['files'].append({'path': 'size-audit.json'})
        else:
            inventory['files'] = [None]
        path.write_text(json.dumps(inventory))
    with pytest.raises(ValueError):
        stage_native_artifact(source, tmp_path / 'output', 'windows')
    assert not (tmp_path / 'output').exists()


def test_macos_native_staging_does_not_add_windows_inventory(tmp_path):
    source = fixture_artifact(tmp_path / 'input')
    report = stage_native_artifact(source, tmp_path / 'output', 'macos')
    assert 'size-audit.json' not in report['files']
    assert not (tmp_path / 'output/size-audit.json').exists()


def test_native_stage_rejects_modified_model(tmp_path):
    source=fixture_artifact(tmp_path / 'input')
    (source / 'models/model_0.onnx').write_bytes(b'corrupt')
    with pytest.raises(ValueError,match='checksum'):
        stage_native_artifact(source,tmp_path / 'output','linux')
    assert not (tmp_path / 'output').exists()


@pytest.mark.parametrize('relative',['../escape','/absolute','a\\b','C:evil','./a'])
def test_native_stage_rejects_path_escape(tmp_path, relative):
    with pytest.raises(ValueError):safe_file(tmp_path,relative)


def test_native_rejects_framework_closure():
    assert_native_requirements(['PySide6-Essentials==6.12.0','numpy==2.3.5'])
    for package in ('PaddlePaddle','paddleocr','paddlex','onnxruntime','onnxruntime-gpu','torch'):
        with pytest.raises(RuntimeError):assert_native_requirements([package+'==1.0'])


def test_native_requires_explicit_artifact(tmp_path):
    with pytest.raises(ValueError):create_bundle(tmp_path,tmp_path,profile='native')
    with pytest.raises(ValueError):create_bundle(tmp_path,tmp_path,native_ocr=tmp_path)


def test_native_prune_no_paddle_required(tmp_path):
    plan=build_plan(tmp_path,engine='native')
    assert plan['engine']=='native'
    assert verify(tmp_path,plan)
    with pytest.raises(ValueError):build_plan(tmp_path,True,engine='native')


def test_native_bundle_omits_paddle_weights_and_selects_backend(tmp_path):
    from unittest.mock import patch
    import zipfile
    from scripts.package_release import verify_bundle
    root=tmp_path/'repo'
    for directory in ('backend/subfinder/linux/x64','backend/models/V6/PP-OCRv6_small_rec_infer','scripts','wheelhouse'):
        (root/directory).mkdir(parents=True)
    (root/'backend/config.py').write_text('VERSION = "2.3.0"\n')
    (root/'backend/models/V6/PP-OCRv6_small_rec_infer/model.json').write_text('DO NOT SHIP')
    (root/'backend/subfinder/linux/x64/VideoSubFinderCli').write_text('verified VSF')
    (root/'scripts/bundle_bootstrap.py').write_text('# bootstrap')
    wheel=root/'wheelhouse/sample-1.0-py3-none-any.whl'
    with zipfile.ZipFile(wheel,'w') as archive:
        archive.writestr('sample-1.0.dist-info/METADATA','Name: sample\nVersion: 1.0\n')
    artifact=fixture_artifact(tmp_path/'native-input')
    def runtime(stage,*args):
        assert args[-1]=='native'
        (stage/'runtime/bin').mkdir(parents=True)
        (stage/'runtime/bin/python3.12').write_text('portable CPython')
        return {'python':'runtime/bin/python3.12'}
    tracked=b'backend/config.py\0backend/models/V6/PP-OCRv6_small_rec_infer/model.json\0scripts/bundle_bootstrap.py\0'
    with patch('scripts.package_release.host_platform',return_value=('linux','x64')), patch('scripts.package_release.subprocess.check_output',side_effect=['a'*40+'\n','',tracked]), patch('scripts.package_release.subprocess.run') as run, patch('scripts.package_release.runtime_wheels',return_value=([wheel],['sample==1.0'])), patch('scripts.package_release.build_runtime',side_effect=runtime), patch('scripts.package_release.portable_launchers'), patch('scripts.bundle_linux_runtime_libs.bundle_linux_libraries',return_value={}) as native_closure:
        output=create_bundle(root/'wheelhouse',root/'dist',root,profile='native',native_ocr=artifact)
        assert native_closure.call_count == 1
        assert native_closure.call_args.kwargs == {'profile': 'native'}
        assert not any('backend.tools.model_download' in str(call) for call in run.call_args_list)
    verify_bundle(output,False)
    with zipfile.ZipFile(output) as archive:
        names=archive.namelist()
        assert not any('/backend/models/' in name for name in names)
        manifest=json.loads(archive.read(next(name for name in names if name.endswith('/bundle-manifest.json'))))
        assert manifest['ocr_backend']=='native'
        assert manifest['native_ocr']['model_count']==15
        assert not any('/wheelhouse/' in name for name in names)
        assert 'native-cpu' in output.name


def test_clean_container_direct_gui_uses_native_manifest(tmp_path, monkeypatch):
    from scripts.verify_clean_linux_bundle import container_command
    # This tests the Linux command contract on every CI host, including Windows.
    # Production continues to read the actual Linux caller's UID/GID.
    monkeypatch.setattr('scripts.verify_clean_linux_bundle.linux_identity', lambda: (1000, 1000))
    bundle=tmp_path/'bundle';bundle.mkdir()
    (bundle/'bundle-manifest.json').write_text(json.dumps({'ocr_backend':'native'}))
    command=container_command(bundle,tmp_path/'evidence','sha256:'+'a'*64,'runtime/bin/python3.12',['gui.py','--smoke-test'],'xcb-gui')
    assert 'VSE_OCR_BACKEND=native' in command
    assert 'VSE_NATIVE_OCR_ROOT=/bundle/native-ocr' in command
    assert 'ORT_DISABLE_TELEMETRY=1' in command
    assert '1000:1000' in command
    (bundle/'bundle-manifest.json').write_text(json.dumps({'ocr_backend':'unknown'}))
    with pytest.raises(ValueError,match='Unknown'):
        container_command(bundle,tmp_path/'evidence','sha256:'+'a'*64,'runtime/bin/python3.12',['gui.py'],'xcb-gui')


def test_native_gpu_control_is_off_without_changing_saved_preference(monkeypatch):
    from PySide6.QtWidgets import QApplication, QWidget
    from backend.config import config
    from ui.setting_interface import SettingInterface
    app=QApplication.instance() or QApplication([])
    monkeypatch.setenv('VSE_OCR_BACKEND','native')
    monkeypatch.delenv('VSE_NATIVE_OCR_PROVIDER',raising=False)
    before=config.hardwareAcceleration.value
    parent=QWidget()
    settings=SettingInterface(parent)
    assert not settings.hardware_acceleration.switchButton.isChecked()
    assert not settings.hardware_acceleration.switchButton.isEnabled()
    assert config.hardwareAcceleration.value==before
    assert 'CPU' in settings.hardware_acceleration.contentLabel.text()
    from ui.advanced_setting_interface import AdvancedSettingInterface
    advanced=AdvancedSettingInterface()
    assert 'GPU' not in advanced.rec_batch_number.contentLabel.text()
    advanced.deleteLater()
    parent.deleteLater();app.processEvents()


def _macos_image(minimum, kind=6):
    import struct
    encoded = (minimum[0] << 16) | (minimum[1] << 8) | (minimum[2] if len(minimum) > 2 else 0)
    return (struct.pack('<8I', 0xfeedfacf, 0x100000c, 0, kind, 1, 24, 0, 0)
            + struct.pack('<6I', 0x32, 24, 1, encoded, 26 << 16, 0))


def _macos_stage(tmp_path, qt_minimum=(14, 4)):
    stage = tmp_path / 'Video Subtitle Extractor.app/Contents/Resources'
    (stage / 'runtime/bin').mkdir(parents=True)
    (stage / 'runtime/bin/python3.12').write_bytes(_macos_image((13, 0), 2))
    (stage / 'runtime/QtCocoa.dylib').write_bytes(_macos_image(qt_minimum))
    return stage


@pytest.mark.parametrize('profile,qt_minimum,expected',
                         [('paddle', (14, 4), '14.4'), ('native', (15, 2, 1), '15.2.1')])
def test_macos_minimum_comes_from_all_actual_images_not_profile(tmp_path, profile, qt_minimum, expected):
    import plistlib
    from scripts.package_release import portable_launchers, verify_macos_deployment
    stage = _macos_stage(tmp_path, qt_minimum)
    report = portable_launchers(stage, 'macos', 'arm64', 'runtime/bin/python3.12', profile)
    plist = plistlib.loads((stage.parent / 'Info.plist').read_bytes())
    assert plist['LSMinimumSystemVersion'] == expected
    assert report['limiting_files'] == ['runtime/QtCocoa.dylib']
    # The final manifest and launcher/icon files add regular files; they are
    # not new Mach-O deployment requirements and must not corrupt this gate.
    (stage / 'bundle-manifest.json').write_text('{}')
    actual = verify_macos_deployment(stage, {'arch': 'arm64', 'macos_deployment': report})
    assert actual['minimum_macos_version'] == expected
    assert actual['scanned_regular_files'] > report['scanned_regular_files']


@pytest.mark.parametrize('change', ['plist', 'record', 'new-higher-image', 'hash', 'missing-report'])
def test_final_macos_minimum_rejects_inconsistent_declared_or_actual_payload(tmp_path, change):
    import plistlib
    from scripts.package_release import portable_launchers, verify_macos_deployment
    stage = _macos_stage(tmp_path)
    report = portable_launchers(stage, 'macos', 'arm64', 'runtime/bin/python3.12')
    manifest = {'arch': 'arm64', 'macos_deployment': report}
    if change == 'plist':
        path = stage.parent / 'Info.plist'
        info = plistlib.loads(path.read_bytes()); info['LSMinimumSystemVersion'] = '14.0'
        path.write_bytes(plistlib.dumps(info))
    elif change == 'record':
        report['minimum_macos_version'] = '14.0'
    elif change == 'new-higher-image':
        (stage / 'runtime/higher.dylib').write_bytes(_macos_image((16, 0)))
    elif change == 'hash':
        report['file_sha256']['runtime/QtCocoa.dylib'] = '0' * 64
    else:
        del manifest['macos_deployment']
    with pytest.raises(RuntimeError):
        verify_macos_deployment(stage, manifest)


def test_macos_empty_stage_never_falls_back_to_old_sdk_baseline(tmp_path):
    from scripts.package_release import portable_launchers
    stage = tmp_path / 'empty'; stage.mkdir()
    with pytest.raises(RuntimeError, match='No runtime Mach-O'):
        portable_launchers(stage, 'macos', 'arm64', 'runtime/bin/python3.12', 'native')


def test_runner_test_only_artifact_cannot_be_packaged(tmp_path):
    source=fixture_artifact(tmp_path/'input')
    (source/'RUNNER-TEST-ONLY.json').write_text('{"customer_distribution_permitted":false}')
    with pytest.raises(ValueError,match='Runner-test-only'):
        stage_native_artifact(source,tmp_path/'output','windows')
    assert not (tmp_path/'output').exists()


def test_native_server_workflow_includes_macos_deployment_parser_regressions():
    workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/native-ocr.yml').read_text()
    assert 'tests/test_macos_minimum_version.py' in workflow
