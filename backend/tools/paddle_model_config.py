"""Explicit PaddleOCR model selection, without importing the inference runtime."""
import os
from pathlib import Path
import sys


class PaddleModelConfig:
    LATIN_LANG = [
        'af', 'az', 'bs', 'cs', 'cy', 'da', 'de', 'es', 'et', 'fr', 'ga', 'hr',
        'hu', 'id', 'is', 'it', 'ku', 'la', 'lt', 'lv', 'mi', 'ms', 'mt', 'nl',
        'no', 'oc', 'pi', 'pl', 'pt', 'ro', 'rs_latin', 'sk', 'sl', 'sq', 'sv',
        'sw', 'tl', 'tr', 'uz', 'vi', 'latin', 'german', 'french',
        'fi', 'eu', 'gl', 'lb', 'rm', 'ca', 'qu',
    ]
    ARABIC_LANG = ['ar', 'fa', 'ug', 'ur', 'ps', 'sd', 'bal']
    CYRILLIC_LANG = [
        'ru', 'rs_cyrillic', 'be', 'bg', 'uk', 'mn', 'abq', 'ady', 'kbd', 'ava',
        'dar', 'inh', 'che', 'lbe', 'lez', 'tab', 'cyrillic', 'sr', 'kk', 'ky',
        'tg', 'mk', 'tt', 'cv', 'ba', 'mhr', 'mo', 'udm', 'kv', 'os', 'bua',
        'xal', 'tyv', 'sah', 'kaa',
    ]
    DEVANAGARI_LANG = [
        'hi', 'mr', 'ne', 'bh', 'mai', 'ang', 'bho', 'mah', 'sck', 'new',
        'gom', 'sa', 'bgc', 'devanagari',
    ]
    OTHER_LANG = ['ch', 'chinese_cht', 'en', 'japan', 'korean', 'th', 'el', 'ta', 'te', 'kn', 'ka']
    MULTI_LANG = LATIN_LANG + ARABIC_LANG + CYRILLIC_LANG + DEVANAGARI_LANG + OTHER_LANG
    # PP-OCRv6 small/medium supports Chinese, English, Japanese and Latin
    # scripts, but NOT Pali, Korean, Arabic or Cyrillic. Tiny omits Japanese.
    V6_LANG = (set(LATIN_LANG) - {'pi'}) | {'ch', 'chinese_cht', 'en', 'japan'}

    def __init__(self, hardware_accelerator, language=None, mode=None, model_base=None):
        if language is None or mode is None:
            from backend.config import config
            language = language if language is not None else config.language.value
            mode = mode if mode is not None else config.mode.value
        if language not in self.MULTI_LANG:
            raise ValueError(f'Unsupported OCR language: {language}')
        if mode not in ('fast', 'accurate', 'auto'):
            raise ValueError(f'Unsupported OCR mode: {mode}')
        self.hardware_accelerator = hardware_accelerator
        self.REC_CHAR_TYPE = language
        self.MODE = mode
        self.MODEL_BASE = str(model_base or os.environ.get('VSE_MODEL_DIR') or Path(__file__).resolve().parents[1] / 'models')
        self.REC_IMAGE_SHAPE = '3,48,320'
        self.TIER = 'medium' if mode == 'accurate' or (mode == 'auto' and hardware_accelerator.has_cuda()) else 'small'
        self.DET_MODEL_NAME = f'PP-OCRv6_{self.TIER}_det'
        if language in self.V6_LANG:
            self.MODEL_VERSION = 'V6'
            self.REC_MODEL_NAME = f'PP-OCRv6_{self.TIER}_rec'
        else:
            self.REC_MODEL_NAME = self._get_fallback_rec_model_name(language)
            self.MODEL_VERSION = 'V3' if 'PP-OCRv3' in self.REC_MODEL_NAME else 'V5'
        self.DET_MODEL_PATH = str(Path(self.MODEL_BASE) / 'V6' / f'{self.DET_MODEL_NAME}_infer')
        self.REC_MODEL_PATH = str(Path(self.MODEL_BASE) / self.MODEL_VERSION / f'{self.REC_MODEL_NAME}_infer')

    @classmethod
    def _get_fallback_rec_model_name(cls, lang):
        if lang in ('ru', 'be', 'uk'):
            return 'eslav_PP-OCRv5_mobile_rec'
        if lang in cls.LATIN_LANG:
            return 'latin_PP-OCRv5_mobile_rec'
        if lang in cls.ARABIC_LANG:
            return 'arabic_PP-OCRv5_mobile_rec'
        if lang in cls.CYRILLIC_LANG:
            return 'cyrillic_PP-OCRv5_mobile_rec'
        if lang in cls.DEVANAGARI_LANG:
            return 'devanagari_PP-OCRv5_mobile_rec'
        if lang in ('korean', 'th', 'el', 'ta', 'te'):
            return f'{lang}_PP-OCRv5_mobile_rec'
        if lang in ('kn', 'ka'):
            return 'ka_PP-OCRv3_mobile_rec'
        raise ValueError(f'No recognition model for language: {lang}')

    @staticmethod
    def _read_model_name_from_yaml(model_dir):
        import yaml
        path = Path(model_dir) / 'inference.yml'
        if not path.is_file():
            return None
        with path.open(encoding='utf-8') as stream:
            data = yaml.safe_load(stream)
        if not isinstance(data, dict):
            return None
        return data.get('Global', {}).get('model_name')

    @classmethod
    def _local_model_dir(cls, path, expected_name):
        directory = Path(path)
        if not directory.exists():
            # Explicit official name allows PaddleX to download/cache exactly
            # this model, never silently switch versions or languages.
            return None
        actual_name = cls._read_model_name_from_yaml(directory)
        if actual_name != expected_name:
            raise ValueError(f'Model identity mismatch at {directory}: expected {expected_name}, got {actual_name}')
        if not (directory / 'inference.pdiparams').is_file() or not any(
            (directory / filename).is_file() for filename in ('inference.json', 'inference.pdmodel')
        ):
            raise ValueError(f'Incomplete Paddle inference model: {directory}')
        return str(directory)

    def ensure_models(self, detection_only=False):
        """Fetch missing v6 weights from the pinned manifest into a user cache.

        The application directory can be read-only (installed macOS/Windows
        packages). Bundled files remain preferred; missing language-specific
        legacy models continue to use PaddleX's explicit official model names.
        """
        from backend.tools.model_download import download_model
        selected = [('DET_MODEL_PATH', self.DET_MODEL_NAME)]
        if not detection_only and self.MODEL_VERSION == 'V6':
            selected.append(('REC_MODEL_PATH', self.REC_MODEL_NAME))
        for attribute, name in selected:
            path = getattr(self, attribute)
            if self._local_model_dir(path, name):
                continue
            cache_root = Path(os.environ.get('VSE_MODEL_CACHE', Path.home() / '.cache' / 'video-subtitle-extractor' / 'models'))
            cached = cache_root / 'V6' / f'{name}_infer'
            if self._local_model_dir(cached, name):
                setattr(self, attribute, str(cached))
                continue
            if os.environ.get('VSE_OFFLINE') == '1':
                raise FileNotFoundError(f'Missing {name}. Run python -m backend.tools.model_download before using offline mode.')
            setattr(self, attribute, str(download_model(name, cache_root / 'V6')))

    def detection_kwargs(self):
        kwargs = {'model_name': self.DET_MODEL_NAME}
        local = self._local_model_dir(self.DET_MODEL_PATH, self.DET_MODEL_NAME)
        if local:
            kwargs['model_dir'] = self._paddle_native_model_dir(local)
        return kwargs

    def ocr_kwargs(self):
        kwargs = {
            'text_detection_model_name': self.DET_MODEL_NAME,
            'text_recognition_model_name': self.REC_MODEL_NAME,
        }
        for prefix, path, name in (
            ('text_detection', self.DET_MODEL_PATH, self.DET_MODEL_NAME),
            ('text_recognition', self.REC_MODEL_PATH, self.REC_MODEL_NAME),
        ):
            local = self._local_model_dir(path, name)
            if local:
                kwargs[f'{prefix}_model_dir'] = self._paddle_native_model_dir(local)
            elif os.environ.get('VSE_OFFLINE') == '1':
                raise FileNotFoundError(f'Missing {name} in offline mode. Pre-bundle the official model at {path}.')
        return kwargs

    @staticmethod
    def _paddle_native_model_dir(directory):
        """Avoid Windows narrow-character model opens without moving weights.

        Paddle 3.3.1 PIR ReadModule passes a std::string path to std::ifstream.
        Its Python configuration can see a Unicode filename that that native
        open may not read. Portable launchers already set the package as CWD;
        its model suffix is ASCII, even when the containing package is not.
        PaddleX preserves this relative directory through predictor creation.

        Compute in the consuming process, not when a config is pickled. Never
        chdir, copy/download weights, change the selected model or use 8.3 names
        (which need not exist). Other paths/platforms keep their original form.
        """
        if sys.platform != 'win32' or directory.isascii():
            return directory
        absolute = Path(directory).resolve(strict=True)
        try:
            relative = os.path.relpath(absolute, Path.cwd())
        except ValueError:  # Different drives cannot share a relative path.
            return directory
        if not relative.isascii():
            return directory
        candidate = Path(relative)
        if not candidate.samefile(absolute):
            raise RuntimeError('Paddle model path identity changed during native path preparation')
        for filename in ('inference.yml', 'inference.json', 'inference.pdmodel', 'inference.pdiparams'):
            original = absolute / filename
            if original.is_file() and not (candidate / filename).samefile(original):
                raise RuntimeError(f'Paddle model file identity changed: {filename}')
        return relative
