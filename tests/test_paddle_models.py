import configparser
from pathlib import Path

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
