import queue
from threading import Thread
from types import SimpleNamespace

import pytest

from backend.tools import subtitle_ocr
from backend.tools.hardware_accelerator import HardwareAccelerator


def options():
    return SimpleNamespace(REC_CHAR_TYPE='japan', MODE='accurate', REC_BATCH_NUMBER=11,
                           HARDWARD_ACCELERATOR=HardwareAccelerator(), DROP_SCORE=.75,
                           SUB_AREA_DEVIATION_RATE=0, DEBUG_OCR_LOSS=False)


def test_consumer_gets_spawned_settings_and_commits_atomically(monkeypatch, tmp_path):
    received = {}
    monkeypatch.setattr(subtitle_ocr, 'OcrRecogniser', lambda **kwargs: received.update(kwargs))
    tasks, progress = queue.Queue(), queue.Queue()
    tasks.put((-1, 0, None, None))
    output = tmp_path / 'raw.txt'
    subtitle_ocr.ocr_task_consumer(tasks, str(output), None, str(tmp_path/'video.mp4'), options(), progress)
    assert received['language'] == 'japan'
    assert received['mode'] == 'accurate'
    assert received['rec_batch_number'] == 11
    assert output.read_text() == ''
    assert not output.with_suffix('.txt.partial').exists()


def test_worker_failure_unblocks_full_producer_queue(monkeypatch, tmp_path):
    def producer(ocr_queue, *args, stop_event):
        while subtitle_ocr._queue_put(ocr_queue, object(), stop_event):
            pass
    def consumer(*args, **kwargs):
        raise RuntimeError('test inference failure')
    monkeypatch.setattr(subtitle_ocr, 'ocr_task_producer', producer)
    monkeypatch.setattr(subtitle_ocr, 'ocr_task_consumer', consumer)
    errors = []
    def run():
        try:
            subtitle_ocr.subtitle_extract_handler(queue.Queue(), queue.Queue(), 'unused.mp4', str(tmp_path/'raw.txt'), None, options())
        except RuntimeError as error:
            errors.append(error)
    worker = Thread(target=run, daemon=True)
    worker.start()
    worker.join(3)
    assert not worker.is_alive(), 'consumer failure left producer deadlocked'
    assert errors and 'test inference failure' in str(errors[0])
    assert not (tmp_path/'raw.txt').exists()


def test_invalid_video_is_a_worker_error_not_success(tmp_path):
    with pytest.raises(RuntimeError, match='Cannot open video'):
        subtitle_ocr.subtitle_extract_handler(queue.Queue(), queue.Queue(), str(tmp_path/'missing.mp4'),
                                             str(tmp_path/'raw.txt'), None, options())


@pytest.mark.parametrize('selection,blank,kept', [
    (subtitle_ocr.SubtitleArea.LOWER_PART, slice(0, 50), slice(50, 100)),
    (subtitle_ocr.SubtitleArea.UPPER_PART, slice(50, 100), slice(0, 50)),
])
def test_half_frame_selection_preserves_full_frame_coordinates(selection, blank, kept):
    import numpy as np
    frame = np.full((100, 200, 3), 255, dtype=np.uint8)
    processed = subtitle_ocr.frame_preprocess(selection, frame)
    assert processed.shape == frame.shape
    assert not processed[blank].any()
    assert (processed[kept] == 255).all()
    assert (frame == 255).all(), 'preprocessing must not mutate cached original frames'
