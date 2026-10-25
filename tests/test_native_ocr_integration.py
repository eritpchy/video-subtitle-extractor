"""Opt-in real native worker and video regressions; no Paddle import allowed.

VSE_RUN_NATIVE_INTEGRATION=1 VSE_NATIVE_OCR_ROOT=/staged/native-worker pytest ...
The native worker must be built independently; tests do not fetch/build assets.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.environ.get('VSE_RUN_NATIVE_INTEGRATION') != '1',
                               reason='Requires independently built native OCR stage')


def guarded_environment(output):
    root = os.environ.get('VSE_NATIVE_OCR_ROOT')
    if not root:
        pytest.fail('VSE_NATIVE_OCR_ROOT is required for real native integration')
    return dict(os.environ, VSE_OCR_BACKEND='native', VSE_NATIVE_OCR_PROVIDER='cpu',
                VSE_OFFLINE='1', QT_QPA_PLATFORM='offscreen',
                VSE_NATIVE_TEST_IMPORT_GUARD='1',
                VSE_NATIVE_TEST_IMPORT_LOG=str(output / 'imports'),
                PYTHONPATH=os.pathsep.join([str(ROOT / 'tests/helpers/native_no_paddle'), str(ROOT)]))


def assert_guard(output):
    records = [json.loads(line) for file in (output / 'imports').glob('*.jsonl')
               for line in file.read_text().splitlines()]
    assert records and any(r['event'] == 'guard_active' for r in records)
    assert any(r['event'] == 'exit_checked' for r in records)
    assert not any(r['event'] == 'forbidden_import' or r.get('forbidden_loaded') for r in records), records


def test_real_native_small_medium_and_detector(tmp_path):
    command = '''
import sys
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from backend.tools.ocr import OcrRecogniser
from backend.tools.subtitle_detect import SubtitleDetect
image=Image.new('RGB',(1000,240),'black')
ImageDraw.Draw(image).text((60,70),'Hello world 字幕测试',font=ImageFont.truetype('backend/tools/NotoSansCJK-Bold.otf',48),fill='white')
frame=np.ascontiguousarray(np.asarray(image)[:,:,::-1])
for mode in ('fast','accurate','auto'):
 recognizer=OcrRecogniser('ch',mode,3)
 try:
  boxes,records=recognizer.predict(frame)
  assert 'Helloworld字幕测试' in ''.join(text for text,score in records).replace(' ','')
  assert len(boxes)==len(records)>0
  assert recognizer.predict(np.zeros_like(frame))==([],[])
 finally: recognizer.close()
detector=SubtitleDetect('ch','fast')
try:
 polygons,elapsed=detector.detect_subtitle(frame)
 assert polygons.shape[1:]==(4,2) and len(polygons)>0
finally: detector.close()
assert not any(name.split('.')[0] in {'paddle','paddlex','paddleocr','onnxruntime'} for name in sys.modules)
'''
    result = subprocess.run([sys.executable, '-c', command], cwd=ROOT,
                            env=guarded_environment(tmp_path), capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
    assert_guard(tmp_path)


@pytest.mark.parametrize('script', ['e2e_smoke.py', 'e2e_multiline.py', 'e2e_av1.py'])
def test_real_native_video_to_srt(tmp_path, script):
    result = subprocess.run([sys.executable, str(ROOT / 'scripts' / script), '--output', str(tmp_path)],
                            cwd=ROOT, env=guarded_environment(tmp_path),
                            capture_output=True, text=True, timeout=600)
    extraction_log = tmp_path / 'extraction.log'
    assert result.returncode == 0, (result.stdout + result.stderr +
                                   (extraction_log.read_text() if extraction_log.exists() else ''))
    report = json.loads((tmp_path / 'report.json').read_text())
    assert report['result'] == 'passed'
    assert_guard(tmp_path)


@pytest.mark.parametrize('evidence_variable,expected_cases', [
    ('VSE_NATIVE_LEGACY_EVIDENCE', 48), ('VSE_NATIVE_MISSING_EVIDENCE', 18)])
def test_real_native_legacy_pipeline_parity(tmp_path, evidence_variable, expected_cases):
    evidence = os.environ.get(evidence_variable)
    if not evidence:
        pytest.skip(f'Set {evidence_variable} to pinned legacy fixture/baseline directory')
    command = r'''
import cv2, json, os, sys
from pathlib import Path
from backend.tools.ocr import OcrRecogniser
root=Path(os.environ['VSE_NATIVE_LEGACY_EVIDENCE'])
manifest=json.loads((root/'conversion-manifest.json').read_text())
languages={row['name']:row['languages'][0] for row in manifest if row.get('present')}
rows=json.loads((root/'pipeline-comparison.json').read_text())
recognizer=None
previous=None
report=[]
try:
 for row in rows:
  key=(row['model'],row['mode'])
  if key!=previous:
   if recognizer: recognizer.close()
   recognizer=OcrRecogniser(languages[key[0]],key[1],6)
   previous=key
  frame=cv2.imread(str(root/'fixtures'/f"{row['model']}-{row['case']}.png"))
  boxes,records=recognizer.predict(frame)
  baseline=row['paddle']
  assert json.loads(json.dumps(boxes))==baseline[0], (key,row['case'],'boxes')
  assert [text for text,score in records]==[r[0] for r in baseline[1]], (key,row['case'],records,baseline)
  delta=max((abs(score-original[1]) for (_,score),original in zip(records,baseline[1])),default=0)
  assert delta<3e-5,(key,row['case'],delta)
  report.append({'model':key[0],'mode':key[1],'case':row['case'],'same_text':True,'same_boxes':True,'max_confidence_delta':delta})
finally:
 if recognizer:recognizer.close()
assert len(report)==int(os.environ['NATIVE_EXPECTED_CASES'])
Path(os.environ['NATIVE_PARITY_REPORT']).write_text(json.dumps(report,ensure_ascii=False,indent=2))
assert not any(name.split('.')[0] in {'paddle','paddlex','paddleocr','onnxruntime'} for name in sys.modules)
'''
    environment = guarded_environment(tmp_path)
    environment['NATIVE_PARITY_REPORT'] = str(tmp_path / 'legacy-adapter-parity.json')
    environment['NATIVE_EXPECTED_CASES'] = str(expected_cases)
    environment['VSE_NATIVE_LEGACY_EVIDENCE'] = evidence
    result = subprocess.run([sys.executable, '-c', command], cwd=ROOT,
                            env=environment, capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stdout + result.stderr
    assert_guard(tmp_path)


def test_real_native_standalone_detector_parity(tmp_path):
    evidence = os.environ.get('VSE_NATIVE_DETECTION_EVIDENCE')
    if not evidence:
        pytest.skip('Set VSE_NATIVE_DETECTION_EVIDENCE to pinned standalone detector evidence directory')
    command = r'''
import cv2, hashlib, json, os, sys
from pathlib import Path
from backend.tools.subtitle_detect import SubtitleDetect
root=Path(os.environ['VSE_NATIVE_DETECTION_EVIDENCE'])
profiles=json.loads((root/'direct-standalone-detection-baseline.json').read_text())
report=[]
for profile in profiles:
 detector=SubtitleDetect('ch','accurate' if profile['tier']=='medium' else 'fast')
 try:
  for case in profile['cases']:
   path=root.parent/'fixtures'/(case['fixture']+'.png')
   assert hashlib.sha256(path.read_bytes()).hexdigest()==case['fixture_sha256']
   polygons,elapsed=detector.detect_subtitle(cv2.imread(str(path)))
   assert polygons.tolist()==case['dt_polys'], (profile['tier'],case['fixture'],polygons.tolist(),case['dt_polys'])
   report.append({'tier':profile['tier'],'fixture':case['fixture'],'same_polygons':True})
 finally: detector.close()
assert len(report)==22
Path(os.environ['NATIVE_DETECTION_REPORT']).write_text(json.dumps(report,indent=2))
assert not any(name.split('.')[0] in {'paddle','paddlex','paddleocr','onnxruntime'} for name in sys.modules)
'''
    environment = guarded_environment(tmp_path)
    environment['NATIVE_DETECTION_REPORT'] = str(tmp_path / 'standalone-detector-adapter-parity.json')
    result = subprocess.run([sys.executable, '-c', command], cwd=ROOT,
                            env=environment, capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stdout + result.stderr
    assert_guard(tmp_path)


def test_real_native_existing_kannada_aliases(tmp_path):
    evidence = os.environ.get('VSE_NATIVE_MISSING_EVIDENCE')
    if not evidence:
        pytest.skip('Requires recovered Kannada model evidence and complete native stage')
    command = r'''
import cv2,json,os
from pathlib import Path
from backend.tools.ocr import OcrRecogniser
root=Path(os.environ['VSE_NATIVE_MISSING_EVIDENCE'])
rows=json.loads((root/'pipeline-comparison.json').read_text())
baseline=next(row['paddle'] for row in rows if row['model']=='ka_PP-OCRv3_mobile_rec' and row['mode']=='fast' and row['case']=='script')
frame=cv2.imread(str(root/'fixtures/ka_PP-OCRv3_mobile_rec-script.png'))
for language in ('kn','ka'):
 recognizer=OcrRecogniser(language,'fast',6)
 try:
  boxes,records=recognizer.predict(frame)
  assert json.loads(json.dumps(boxes))==baseline[0]
  assert [text for text,score in records]==[r[0] for r in baseline[1]]
 finally:recognizer.close()
'''
    result = subprocess.run([sys.executable, '-c', command], cwd=ROOT,
                            env=guarded_environment(tmp_path), capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert_guard(tmp_path)
