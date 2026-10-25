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
    with patch('scripts.package_release.host_platform',return_value=('linux','x64')), patch('scripts.package_release.subprocess.check_output',side_effect=['a'*40+'\n','',tracked]), patch('scripts.package_release.subprocess.run') as run, patch('scripts.package_release.runtime_wheels',return_value=([wheel],['sample==1.0'])), patch('scripts.package_release.build_runtime',side_effect=runtime), patch('scripts.package_release.portable_launchers'), patch('scripts.bundle_linux_runtime_libs.bundle_linux_libraries',return_value={}):
        output=create_bundle(root/'wheelhouse',root/'dist',root,profile='native',native_ocr=artifact)
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


@pytest.mark.parametrize('profile,minimum',[('paddle','13.0'),('native','14.0')])
def test_macos_minimum_matches_native_sdk(tmp_path,profile,minimum):
    import plistlib
    from scripts.package_release import portable_launchers
    stage=tmp_path/'Video Subtitle Extractor.app/Contents/Resources'
    stage.mkdir(parents=True)
    portable_launchers(stage,'macos','arm64','runtime/bin/python3.12',profile)
    plist=plistlib.loads((stage.parent/'Info.plist').read_bytes())
    assert plist['LSMinimumSystemVersion']==minimum


def test_runner_test_only_artifact_cannot_be_packaged(tmp_path):
    source=fixture_artifact(tmp_path/'input')
    (source/'RUNNER-TEST-ONLY.json').write_text('{"customer_distribution_permitted":false}')
    with pytest.raises(ValueError,match='Runner-test-only'):
        stage_native_artifact(source,tmp_path/'output','windows')
    assert not (tmp_path/'output').exists()
