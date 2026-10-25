import configparser
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from backend.tools.hardware_accelerator import HardwareAccelerator
from backend.tools.paddle_model_config import PaddleModelConfig


@pytest.fixture(autouse=True)
def isolated_model_environment(monkeypatch):
    # Tests of missing bundle selection explicitly control network policy.
    monkeypatch.delenv('VSE_OFFLINE', raising=False)
    monkeypatch.delenv('VSE_MODEL_DIR', raising=False)


@pytest.mark.parametrize('language', ['ch', 'chinese_cht', 'en', 'japan', 'fr', 'german', 'latin', 'vi'])
@pytest.mark.parametrize('mode,tier', [('fast', 'small'), ('accurate', 'medium'), ('auto', 'small')])
def test_v6_names_are_genuine_and_explicit(language, mode, tier, tmp_path):
    models = PaddleModelConfig(HardwareAccelerator(), language, mode, tmp_path)
    assert models.MODEL_VERSION == 'V6'
    assert models.DET_MODEL_NAME == f'PP-OCRv6_{tier}_det'
    assert models.REC_MODEL_NAME == f'PP-OCRv6_{tier}_rec'
    assert models.ocr_kwargs() == {'text_detection_model_name': models.DET_MODEL_NAME,
                                  'text_recognition_model_name': models.REC_MODEL_NAME}
    assert models.DET_MODEL_PATH and models.REC_MODEL_PATH


def test_auto_uses_medium_only_for_actual_cuda(tmp_path):
    hardware = HardwareAccelerator()
    hardware.has_cuda = lambda: True
    assert PaddleModelConfig(hardware, 'en', 'auto', tmp_path).TIER == 'medium'


@pytest.mark.parametrize('language,name', [
    ('korean', 'korean_PP-OCRv5_mobile_rec'), ('ar', 'arabic_PP-OCRv5_mobile_rec'),
    ('ru', 'eslav_PP-OCRv5_mobile_rec'), ('bg', 'cyrillic_PP-OCRv5_mobile_rec'),
    ('pi', 'latin_PP-OCRv5_mobile_rec'), ('hi', 'devanagari_PP-OCRv5_mobile_rec'),
    ('th', 'th_PP-OCRv5_mobile_rec'), ('el', 'el_PP-OCRv5_mobile_rec'),
    ('kn', 'ka_PP-OCRv3_mobile_rec'), ('ka', 'ka_PP-OCRv3_mobile_rec'),
    ('ta', 'ta_PP-OCRv5_mobile_rec'), ('te', 'te_PP-OCRv5_mobile_rec'),
])
def test_language_fallback_is_not_mislabeled_v6(language, name, tmp_path):
    models = PaddleModelConfig(HardwareAccelerator(), language, 'fast', tmp_path)
    assert models.REC_MODEL_NAME == name
    assert models.MODEL_VERSION != 'V6'
    assert models.DET_MODEL_NAME == 'PP-OCRv6_small_det'


def test_every_ui_language_resolves(tmp_path):
    interface = configparser.ConfigParser()
    interface.read(Path(__file__).parents[1] / 'backend/interface/en.ini', encoding='utf-8')
    for language in interface['Language']:
        models = PaddleModelConfig(HardwareAccelerator(), language, 'fast', tmp_path)
        assert models.REC_MODEL_NAME is not None, language


@pytest.mark.parametrize('language,mode', [('bogus', 'fast'), ('en', 'bogus')])
def test_invalid_selection_fails_instead_of_loading_chinese(language, mode):
    with pytest.raises(ValueError, match='Unsupported OCR'):
        PaddleModelConfig(HardwareAccelerator(), language, mode)


def test_wrong_model_identity_is_rejected(tmp_path):
    models = PaddleModelConfig(HardwareAccelerator(), 'en', 'fast', tmp_path)
    model = Path(models.DET_MODEL_PATH)
    model.mkdir(parents=True)
    (model / 'inference.yml').write_text('Global:\n  model_name: PP-OCRv5_mobile_det\n')
    with pytest.raises(ValueError, match='identity mismatch'):
        models.ocr_kwargs()


def test_incomplete_model_is_rejected(tmp_path):
    models = PaddleModelConfig(HardwareAccelerator(), 'en', 'fast', tmp_path)
    model = Path(models.DET_MODEL_PATH)
    model.mkdir(parents=True)
    (model / 'inference.yml').write_text(f'Global:\n  model_name: {models.DET_MODEL_NAME}\n')
    with pytest.raises(ValueError, match='Incomplete'):
        models.detection_kwargs()


def test_yaml_global_section_is_parsed_structurally(tmp_path):
    (tmp_path / 'inference.yml').write_text('Other:\n  model_name: incorrect\nGlobal:\n  metadata:\n    name: ignored\n  model_name: "PP-OCRv6_small_rec"\n')
    assert PaddleModelConfig._read_model_name_from_yaml(tmp_path) == 'PP-OCRv6_small_rec'


def test_accelerator_reports_actual_runtime(monkeypatch):
    hardware = HardwareAccelerator()
    monkeypatch.setattr(hardware, '_HardwareAccelerator__onnx_providers', ['DmlExecutionProvider'])
    assert not hardware.has_accelerator()
    assert hardware.accelerator_name == 'CPU'
    monkeypatch.setattr(hardware, '_HardwareAccelerator__cuda', True)
    assert hardware.inference_kwargs()['device'] == 'gpu:0'
    hardware.set_enabled(False)
    assert hardware.inference_kwargs()['device'] == 'cpu'
    assert not hardware.has_accelerator()


def test_onednn_portable_default(monkeypatch):
    monkeypatch.delenv('VSE_ENABLE_MKLDNN', raising=False)
    assert HardwareAccelerator().inference_kwargs()['enable_mkldnn'] is False
    monkeypatch.setenv('VSE_OCR_CPU_THREADS', '0')
    with pytest.raises(ValueError, match='at least 1'):
        HardwareAccelerator().inference_kwargs()


def test_offline_missing_v6_fails_with_actionable_instruction(monkeypatch, tmp_path):
    monkeypatch.setenv('VSE_OFFLINE', '1')
    monkeypatch.setenv('VSE_MODEL_CACHE', str(tmp_path / 'cache'))
    models = PaddleModelConfig(HardwareAccelerator(), 'en', 'fast', tmp_path / 'bundle')
    with pytest.raises(FileNotFoundError, match='model_download'):
        models.ensure_models()


def test_missing_v6_uses_pinned_downloader_not_implicit_model_hub(monkeypatch, tmp_path):
    monkeypatch.delenv('VSE_OFFLINE', raising=False)
    monkeypatch.setenv('VSE_MODEL_CACHE', str(tmp_path / 'cache'))
    requested = []
    def download(name, output):
        requested.append(name)
        return output / f'{name}_infer'
    monkeypatch.setattr('backend.tools.model_download.download_model', download)
    models = PaddleModelConfig(HardwareAccelerator(), 'japan', 'fast', tmp_path / 'bundle')
    models.ensure_models()
    assert requested == ['PP-OCRv6_small_det', 'PP-OCRv6_small_rec']
    assert str(tmp_path / 'cache') in models.REC_MODEL_PATH


def test_detector_does_not_download_unused_recognizer(monkeypatch, tmp_path):
    monkeypatch.delenv('VSE_OFFLINE', raising=False)
    monkeypatch.setenv('VSE_MODEL_CACHE', str(tmp_path / 'cache'))
    requested = []
    def download(name, output):
        requested.append(name)
        return output / f'{name}_infer'
    monkeypatch.setattr('backend.tools.model_download.download_model', download)
    models = PaddleModelConfig(HardwareAccelerator(), 'ch', 'fast', tmp_path / 'bundle')
    models.ensure_models(detection_only=True)
    assert requested == ['PP-OCRv6_small_det']


def test_offline_language_fallback_never_silently_downloads(monkeypatch, tmp_path):
    monkeypatch.setenv('VSE_OFFLINE', '1')
    models = PaddleModelConfig(HardwareAccelerator(), 'korean', 'fast', tmp_path)
    # A detector is normally ensured before kwargs. Here any absent model
    # must fail, so no PaddleX constructor can initiate a hidden download.
    with pytest.raises(FileNotFoundError, match='offline mode'):
        models.ocr_kwargs()


def local_model_fixture(root):
    models = PaddleModelConfig(HardwareAccelerator(), 'en', 'fast', root / 'backend/models')
    for name, path in ((models.DET_MODEL_NAME, models.DET_MODEL_PATH), (models.REC_MODEL_NAME, models.REC_MODEL_PATH)):
        directory = Path(path)
        directory.mkdir(parents=True)
        (directory / 'inference.yml').write_text(f'Global:\n  model_name: {name}\n', encoding='utf-8')
        (directory / 'inference.json').write_text('{"fixture":true}', encoding='utf-8')
        (directory / 'inference.pdiparams').write_bytes(b'unchanged model fixture bytes')
    return models


@pytest.mark.parametrize('cwd_suffix', ['', 'backend', 'unrelated launch & spaces', '启动 字幕'])
def test_windows_model_adapter_uses_same_files_from_consuming_child_cwd(tmp_path, cwd_suffix):
    root = tmp_path / 'package 字幕 & spaces'
    models = local_model_fixture(root)
    cwd = root / cwd_suffix
    cwd.mkdir(exist_ok=True)
    repository = Path(__file__).resolve().parents[1]
    # Native CI uses Windows; other hosts explicitly exercise only the path
    # adaptation mechanism, not a claim about Windows C++ file-opening behavior.
    code = f'''import json, os, sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, {str(repository)!r})
from backend.tools import paddle_model_config as module
from backend.tools.hardware_accelerator import HardwareAccelerator
module.sys = SimpleNamespace(platform='win32')
before = Path.cwd()
models = module.PaddleModelConfig(HardwareAccelerator(), 'en', 'fast', {models.MODEL_BASE!r})
originals = (models.DET_MODEL_PATH, models.REC_MODEL_PATH)
ocr = models.ocr_kwargs()
detector = models.detection_kwargs()
for selected, original in ((ocr['text_detection_model_dir'], originals[0]),
                           (ocr['text_recognition_model_dir'], originals[1]),
                           (detector['model_dir'], originals[0])):
    assert selected.isascii() and not Path(selected).is_absolute(), selected
    for filename in ('inference.yml', 'inference.json', 'inference.pdiparams'):
        assert (Path(selected) / filename).samefile(Path(original) / filename)
assert originals == (models.DET_MODEL_PATH, models.REC_MODEL_PATH)
assert Path.cwd() == before
print(json.dumps(dict(ocr=ocr, detector=detector)))
'''
    before = {path: path.read_bytes() for path in root.rglob('*') if path.is_file()}
    result = subprocess.run([sys.executable, '-I', '-B', '-X', 'utf8', '-c', code], cwd=cwd,
                            check=True, capture_output=True, timeout=30)
    selected = json.loads(result.stdout.decode('utf-8'))
    assert selected['ocr']['text_detection_model_name'] == models.DET_MODEL_NAME
    assert selected['ocr']['text_recognition_model_name'] == models.REC_MODEL_NAME
    assert before == {path: path.read_bytes() for path in root.rglob('*') if path.is_file()}


@pytest.mark.parametrize('platform,unicode_root', [('linux', True), ('darwin', True), ('win32', False)])
def test_native_model_path_adaptation_preserves_unaffected_paths(tmp_path, monkeypatch, platform, unicode_root):
    from backend.tools import paddle_model_config as module
    root = tmp_path / ('package 字幕' if unicode_root else 'package ascii & spaces')
    models = local_model_fixture(root)
    monkeypatch.setattr(module, 'sys', SimpleNamespace(platform=platform))
    monkeypatch.chdir(root)
    if platform == 'win32' and not models.DET_MODEL_PATH.isascii():
        # A developer's own username/temp prefix may itself contain Unicode.
        models.DET_MODEL_PATH = os.path.relpath(models.DET_MODEL_PATH, root)
    assert models.detection_kwargs()['model_dir'] == models.DET_MODEL_PATH


@pytest.mark.parametrize('mismatch', ['directory', 'file'])
def test_native_model_alias_fails_closed_if_identity_cannot_be_verified(tmp_path, monkeypatch, mismatch):
    from backend.tools import paddle_model_config as module
    root = tmp_path / 'package 字幕'
    models = local_model_fixture(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(module, 'sys', SimpleNamespace(platform='win32'))
    monkeypatch.setattr(Path, 'samefile', lambda path, other: mismatch == 'file' and path.is_dir() and other.is_dir())
    with pytest.raises(RuntimeError, match='identity changed'):
        models.ocr_kwargs()


@pytest.mark.parametrize('case', ['different-drive', 'non-ascii-suffix'])
def test_no_weight_copy_or_model_switch_when_ascii_relative_alias_is_unavailable(tmp_path, monkeypatch, case):
    from backend.tools import paddle_model_config as module
    models = local_model_fixture(tmp_path / 'different model 字幕')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(module, 'sys', SimpleNamespace(platform='win32'))
    if case == 'different-drive':
        def different_drive(*args):
            raise ValueError('path is on another drive')
        monkeypatch.setattr(module.os.path, 'relpath', different_drive)
    assert models.detection_kwargs()['model_dir'] == models.DET_MODEL_PATH
