"""Subprocess regression: an OCR crash must not hang Python's queue finalizer."""
import multiprocessing
from pathlib import Path
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.main import SubtitleExtractor
from backend.tools import subtitle_ocr
from backend.tools.hardware_accelerator import HardwareAccelerator
from backend.tools.process_manager import ProcessManager


def main():
    multiprocessing.set_start_method("spawn")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        extractor = SubtitleExtractor.__new__(SubtitleExtractor)
        extractor.lock = threading.RLock()
        extractor.subtitle_ocr_progress_queue = None
        extractor.video_cap = None

        def failed_run():
            options = dict(REC_CHAR_TYPE="en", DROP_SCORE=.75,
                           SUB_AREA_DEVIATION_RATE=0, DEBUG_OCR_LOSS=False,
                           HARDWARD_ACCELERATOR=HardwareAccelerator())
            process, tasks, progress = subtitle_ocr.async_start(
                str(root / "missing.mp4"), str(root / "raw.txt"), None, options)
            extractor._ocr_process = process
            extractor._ocr_process_id = ProcessManager.instance().add_process(process)
            extractor.subtitle_ocr_task_queue = tasks
            extractor.subtitle_ocr_progress_queue = progress
            for index in range(10000):
                tasks.put((10000, index, None, None, float(index), None))
            process.join(timeout=15)
            assert process.exitcode not in (None, 0), "Expected a real failed OCR process"
            raise RuntimeError("expected worker failure")

        extractor._run_impl = failed_run
        try:
            extractor.run()
        except RuntimeError as error:
            assert str(error) == "expected worker failure"
        else:
            raise AssertionError("Worker failure was suppressed")
        ProcessManager.instance().terminate_all()
        print("FAILED_WORKER_CLEANUP_OK", flush=True)


if __name__ == "__main__":
    main()
