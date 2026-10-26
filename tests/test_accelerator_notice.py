"""All existing UI languages keep a readable, evidence-honest accelerator notice."""
import configparser
from pathlib import Path
import unicodedata

import pytest


ROOT = Path(__file__).resolve().parents[1] / 'backend' / 'interface'
PHRASES = {
    'ch': ('相同输入和模式', '不能单独证明', '随时附上日志反馈'),
    'chinese_cht': ('相同輸入和模式', '不能單獨證明', '隨時附上日誌回報'),
    'en': ('same input and mode', 'alone do not prove', 'logs at any time'),
    'es': ('misma entrada y el mismo modo', 'por sí solas no demuestran', 'registros en cualquier momento'),
    'japan': ('同じ入力とモード', '証明できません', 'いつでもログ'),
    'ko': ('같은 입력과 모드', '확인할 수는 없습니다', '언제든 로그'),
    'tr': ('aynı girdi ve mod', 'tek başına GPU kullanımını kanıtlamaz', 'günlükleri ekleyerek her zaman'),
    'vi': ('cùng đầu vào và chế độ', 'không đủ để chứng minh', 'nhật ký bất cứ lúc nào'),
}


def test_no_existing_interface_language_was_removed_or_replaced_by_english():
    assert {path.stem for path in ROOT.glob('*.ini')} == set(PHRASES)
    notices = []
    for locale in PHRASES:
        parser = configparser.ConfigParser()
        parser.read(ROOT / (locale + '.ini'), encoding='utf-8')
        notices.append(parser['Main']['AcceleratorWarning'])
        assert parser['Main']['AcceleratorON']
    assert len(set(notices)) == len(PHRASES)


@pytest.mark.parametrize('locale', PHRASES)
def test_notice_is_short_valid_localized_text_without_timing_as_gpu_proof(locale):
    data = (ROOT / (locale + '.ini')).read_bytes()
    text = data.decode('utf-8', errors='strict')
    assert sum(line.startswith('AcceleratorWarning = ') for line in text.splitlines()) == 1
    parser = configparser.ConfigParser()
    parser.read_string(text)
    notice = parser['Main']['AcceleratorWarning']
    assert 40 <= len(notice) <= 320
    assert notice == unicodedata.normalize('NFC', notice)
    assert '\ufffd' not in notice and '\x00' not in notice
    assert all(unicodedata.category(char) != 'Cc' for char in notice)
    assert 'GPU' in notice and 'CPU' in notice
    assert all(phrase in notice for phrase in PHRASES[locale])
    assert 'PaddlePaddle works on' not in notice
    assert not any(word in notice.lower() for word in ('provenance', 'request_id', 'benchmark'))
