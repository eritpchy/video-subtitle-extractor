import sys
import subprocess
import threading
from typing import Optional, Callable, List, Any

from .process_manager import ProcessManager

class AsyncPythonRunner:
    def __init__(self):
        """初始化异步Python运行器"""
        self.process: Optional[subprocess.Popen] = None
        self._output_threads: List[threading.Thread] = []
        self._monitor_thread: Optional[threading.Thread] = None
        self.returncode: Optional[int] = None
        self._stdout_callbacks: List[Callable[[str], Any]] = []
        self._stderr_callbacks: List[Callable[[str], Any]] = []
        self._exit_callbacks: List[Callable[[int], Any]] = []
        self._token_exit_callbacks: List[Callable[[int, int], Any]] = []
        self.generation = 0

    def register_callback(self, 
                        stdout: Callable[[str], Any] = None,
                        stderr: Callable[[str], Any] = None,
                        complete: Callable[[int], Any] = None,
                        complete_with_token: Callable[[int, int], Any] = None):
        """注册回调函数"""
        if stdout:
            self._stdout_callbacks.append(stdout)
        if stderr:
            self._stderr_callbacks.append(stderr)
        if complete:
            self._exit_callbacks.append(complete)
        if complete_with_token:
            self._token_exit_callbacks.append(complete_with_token)

    def _notify_callbacks(self, 
                        stream_type: str, 
                        content: str):
        """通知已注册的回调"""
        if stream_type == 'stdout':
            for callback in self._stdout_callbacks:
                callback(content)
        elif stream_type == 'stderr':
            for callback in self._stderr_callbacks:
                callback(content)

    def start(self, 
             script_path: str,
             args: List[str] = None,
             python_args: List[str] = None,
             cwd: str = None):
        """
        启动子进程
        :param script_path: 要执行的Python脚本路径
        :param args: 传递给脚本的参数
        :param python_args: 传递给Python解释器的参数
        :param cwd: 设置工作目录
        """
        if self.process is not None and self.process.poll() is None:
            raise RuntimeError("Process is already running")
        
        python_path = sys.executable
        cmd = [python_path]
        # 添加Python解释器参数
        if python_args:
            cmd.extend(python_args)
        
        # 添加脚本路径和参数
        cmd.append(script_path)
        if args:
            cmd.extend(args)
        
        # 启动子进程
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding='utf-8',
            text=True,
            bufsize=1,
            universal_newlines=True,
            cwd=cwd
        )
        self.process = process
        self.generation += 1
        generation = self.generation
        self.returncode = None
        self._output_threads = []
        
        # 启动输出读取线程
        self._start_reader_thread('stdout', process.stdout, process)
        self._start_reader_thread('stderr', process.stderr, process)
        
        # 启动监控线程
        self._monitor_thread = threading.Thread(
            target=self._monitor_process,
            args=(process, tuple(self._output_threads), generation),
            daemon=True
        )
        self._monitor_thread.start()

    def _start_reader_thread(self, 
                           stream_type: str, 
                           stream, process):
        """启动输出读取线程"""
        def reader():
            for line in iter(stream.readline, ''):
                if self.process is process:
                    self._notify_callbacks(stream_type, line)
            stream.close()
        
        thread = threading.Thread(
            target=reader,
            daemon=True
        )
        thread.start()
        self._output_threads.append(thread)

    def _monitor_process(self, process, readers, generation):
        """监控进程状态"""
        returncode = process.wait()
        for reader in readers:
            reader.join()
        if self.process is process:
            self.returncode = returncode
            for callback in self._exit_callbacks:
                if self.process is process and self.generation == generation:
                    callback(returncode)
            for callback in self._token_exit_callbacks:
                if self.process is process and self.generation == generation:
                    callback(returncode, generation)

    def is_running(self) -> bool:
        """检查子进程是否在运行"""
        return self.process is not None and self.process.poll() is None

    def stop(self):
        """停止子进程"""
        process = self.process
        # Detach first so output/completion from a stopped job cannot update a
        # later run, and never send a signal to a completed/reused process ID.
        self.process = None
        self.generation += 1
        if process is not None and process.poll() is None:
            ProcessManager.instance().terminate_by_pid(process.pid)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def wait(self, timeout: float = None) -> bool:
        """等待进程结束"""
        if self._monitor_thread:
            self._monitor_thread.join(timeout)
            return not self._monitor_thread.is_alive()
        return True

# 使用示例
if __name__ == "__main__":
    def on_stdout(line: str):
        print(f"[STDOUT] {line}", end='')

    def on_stderr(line: str):
        print(f"[STDERR] {line}", end='', file=sys.stderr)

    def on_exit(code: int):
        print(f"\n[EXIT] Process finished with code {code}")

    # 创建运行器
    runner = AsyncPythonRunner()
    
    # 注册回调
    runner.register_callback(
        stdout=on_stdout,
        stderr=on_stderr,
        complete=on_exit
    )
    
    # 启动脚本 (带参数示例)
    runner.start(
        script_path="path/to/your_script.py",
        args=["--param1", "value1", "--flag"],
        python_args=["-u"]  # 无缓冲模式
    )
    
    # 主线程继续执行其他任务...
    try:
        while runner.is_running():
            # 模拟其他工作
            threading.Event().wait(0.5)
    except KeyboardInterrupt:
        print("\nStopping process...")
        runner.stop()
    
    # 等待进程结束
    runner.wait()
    print("Main thread exiting")
