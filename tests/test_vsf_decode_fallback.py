"""Native VSF codecs may differ from Python OpenCV, notably on Windows."""
from contextlib import contextmanager
from pathlib import Path
import queue
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import backend.main as main
from backend.tools.videosubfinder import ExtractionResult, VideoSubFinderDecodeError, VideoSubFinderError


def extractor(tmp_path, monkeypatch):
    obj = main.SubtitleExtractor.__new__(main.SubtitleExtractor)
    obj.video_path = str(tmp_path/'original.mkv')
    obj.video_cap = Mock()
    obj.temp_output_dir = str(tmp_path)
    obj.vsf_subtitle = str(tmp_path/'raw_vsf.srt')
    obj.sub_area = SimpleNamespace(xmin=0, xmax=100, ymin=50, ymax=100)
    obj.frame_height = obj.frame_width = obj.frame_count = 100
    obj.fps = 25
    obj.subtitle_ocr_task_queue = queue.Queue()
    obj.append_output = Mock()
    obj.update_progress = Mock()
    obj.manage_process = Mock()
    obj._needs_compatible_video = False  # Python OpenCV has decoded successfully.
    obj.use_vsf = False
    monkeypatch.setattr(main, 'resolve_binary', lambda _: Path('native-vsf'))
    monkeypatch.setattr(main, 'verify_binary', lambda _: '6.10')
    manager = Mock()
    manager.add_process.side_effect = lambda process: str(process.pid)
    monkeypatch.setattr(main.ProcessManager, 'instance', lambda: manager)
    return obj, manager


def setup_conversion(monkeypatch, tmp_path):
    events = []
    @contextmanager
    def compatible(*args, on_spawn):
        events.append('enter')
        target = tmp_path/'temporary.mkv'
        target.write_bytes(b'converted')
        on_spawn(SimpleNamespace(pid=200))
        try:
            yield target
        finally:
            target.unlink()
            events.append('cleanup')
    monkeypatch.setattr(main, 'compatible_video', compatible)
    return events


def test_native_decoder_failure_retries_once_when_python_decode_succeeds(tmp_path, monkeypatch):
    obj, manager = extractor(tmp_path, monkeypatch)
    events = setup_conversion(monkeypatch, tmp_path)
    inputs = []
    def run(command, **callbacks):
        inputs.append(command[command.index('-i')+1])
        callbacks['on_spawn'](SimpleNamespace(pid=100+len(inputs)))
        if len(inputs) == 1:
            Path(obj.vsf_subtitle).write_text('stale failed output')
            raise VideoSubFinderDecodeError('Failed to get pixel format')
        assert not Path(obj.vsf_subtitle).exists()
        Path(obj.vsf_subtitle).write_text('timing')
        return ExtractionResult((1000,), 0)
    monkeypatch.setattr(main, 'run_command', run)
    obj.extract_frame_by_vsf()
    assert inputs == [obj.video_path, str(tmp_path/'temporary.mkv')]
    assert events == ['enter', 'cleanup']
    assert obj.use_vsf and not obj.vsf_running
    assert obj.subtitle_ocr_task_queue.get_nowait()[4] == 1000
    assert manager.remove_process.call_count == 3


@pytest.mark.parametrize('already_compatible', [False, True])
def test_failed_compatibility_decode_stops_and_cleans(tmp_path, monkeypatch, already_compatible):
    obj, manager = extractor(tmp_path, monkeypatch)
    obj._needs_compatible_video = already_compatible
    events = setup_conversion(monkeypatch, tmp_path)
    calls = []
    def fail(command, **callbacks):
        calls.append(command[:])
        callbacks['on_spawn'](SimpleNamespace(pid=100+len(calls)))
        raise VideoSubFinderDecodeError('Get current frame error')
    monkeypatch.setattr(main, 'run_command', fail)
    with pytest.raises(VideoSubFinderDecodeError):
        obj.extract_frame_by_vsf()
    assert len(calls) == (1 if already_compatible else 2)
    assert events == ['enter', 'cleanup']
    assert not obj.use_vsf and not obj.vsf_running
    assert obj.subtitle_ocr_task_queue.empty()
    assert manager.remove_process.called


@pytest.mark.parametrize('failure', [None, VideoSubFinderError('unrelated launch failure')])
def test_empty_video_or_unrelated_error_does_not_convert(tmp_path, monkeypatch, failure):
    obj, _ = extractor(tmp_path, monkeypatch)
    conversion = Mock(side_effect=AssertionError('must not convert'))
    monkeypatch.setattr(main, 'compatible_video', conversion)
    def run(*args, **kwargs):
        if failure:
            raise failure
        Path(obj.vsf_subtitle).write_text('')
        return ExtractionResult((), 0)
    monkeypatch.setattr(main, 'run_command', run)
    if failure:
        with pytest.raises(VideoSubFinderError, match='unrelated'):
            obj.extract_frame_by_vsf()
    else:
        obj.extract_frame_by_vsf()
        assert obj.use_vsf
    assert not obj.vsf_running
    conversion.assert_not_called()
