import multiprocessing
import threading
from enum import Enum

class Command(Enum):
    FINISH = 0,
    PROGRESS = 1,
    LOG = 2,
    MANAGE_PROCESS = 3,
    ERROR = 4,

class SubtitleExtractorRemoteCall:
    """
    远程回调函数类，用于在多进程环境中传递回调函数
    """
    def __init__(self):
        self.queue = multiprocessing.Queue()
        self.callbacks = {}
        self.running = True
        self.finished = False
        self.stopped = False
        self.error = None
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()
    
    def run(self):
        try:
            while self.running:
                cmd, args = self.queue.get(block=True)
                if cmd == Command.FINISH:
                    self.finished = True
                    break
                callback = self.callbacks.get(cmd)
                if callback:
                    try:
                        callback(*args)
                    except Exception as error:
                        # Keep draining the child pipe after delivery failure;
                        # otherwise its Queue feeder can block process.join().
                        if self.error is None:
                            self.error = error
        except Exception as error:
            self.error = error
        finally:
            self.running = False

    def wait(self, timeout=None):
        """Wait for FINISH after a joined child, draining earlier callbacks."""
        self.thread.join(timeout)
        return not self.thread.is_alive() and self.finished and not self.stopped and self.error is None

    def stop(self):
        self.stopped = True
        self.running = False
        # A stopped/failed child may never send FINISH. Wake an idle reader;
        # never leave a daemon blocked forever in Queue.get().
        if self.thread.is_alive():
            self.queue.put((Command.FINISH, ()))
            self.thread.join(timeout=1)

    def register_update_progress_callback(self, callback):
        self.callbacks[Command.PROGRESS] = callback

    def register_log_callback(self, callback):
        self.callbacks[Command.LOG] = callback
    
    def register_manage_process_callback(self, callback):
        self.callbacks[Command.MANAGE_PROCESS] = callback

    def register_error_callback(self, callback):
        self.callbacks[Command.ERROR] = callback

    @staticmethod
    def remote_call_update_progress(queue, progress_ocr, progress_frame_extract, progress_total, isFinished, progress_post=0):
        queue.put((Command.PROGRESS, (progress_ocr, progress_frame_extract, progress_total, isFinished, progress_post,)))

    @staticmethod
    def remote_call_append_log(queue, *args):
        queue.put((Command.LOG, (*args,)))

    @staticmethod
    def remote_call_finish(queue, *args):
        queue.put((Command.FINISH, (None,)))
        
    @staticmethod
    def remote_call_catch_error(queue, e):
        queue.put((Command.ERROR, (e,)))

    @staticmethod
    def remote_call_manage_process(queue, pid):
        queue.put((Command.MANAGE_PROCESS, (pid,)))
