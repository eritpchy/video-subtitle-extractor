"""Report the explicitly selected inference backend and enabled device."""
import logging
import os

logger = logging.getLogger(__name__)


class HardwareAccelerator:
    _instance = None

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = cls()
            cls._instance.initialize()
        return cls._instance

    def __init__(self):
        self.__cuda = False
        self.__onnx_providers = []
        self.__enabled = True
        self.__backend = 'paddle'

    def initialize(self):
        from backend.tools.native_ocr import selected_backend
        self.__backend = selected_backend()
        self.__cuda = False
        if self.__backend == 'directml':
            self.check_onnx()
            return
        if self.__backend == 'native':
            self.__cuda = False
            return
        self.check_paddle()

    def check_paddle(self):
        import paddle
        self.__cuda = False
        try:
            self.__cuda = paddle.is_compiled_with_cuda() and paddle.device.cuda.device_count() > 0
        except (RuntimeError, OSError) as error:
            logger.warning('CUDA unavailable, using CPU: %s', error)

    def check_onnx(self):
        # Provider discovery is retained for callers, but the installed Paddle
        # path must not advertise DirectML/CoreML while actually executing CPU.
        try:
            import onnxruntime as ort
            self.__onnx_providers = [name for name in ort.get_available_providers() if name not in ('CPUExecutionProvider', 'AzureExecutionProvider')]
        except ImportError:
            self.__onnx_providers = []

    def has_accelerator(self):
        return self.has_cuda() or self.has_directml()

    @property
    def accelerator_name(self):
        if self.__backend == 'directml' and self.__enabled:
            return 'DirectML' if self.has_directml() else 'DirectML (unavailable; CPU fallback disabled)'
        return 'GPU (CUDA)' if self.has_cuda() else 'CPU'

    @property
    def onnx_providers(self):
        return list(self.__onnx_providers) if self.__enabled else []

    @property
    def is_enabled(self):
        return self.__enabled

    def has_directml(self):
        return self.__backend == 'directml' and self.__enabled and 'DmlExecutionProvider' in self.__onnx_providers

    def has_cuda(self):
        return self.__enabled and self.__cuda

    def set_enabled(self, enable):
        self.__enabled = bool(enable)

    def inference_kwargs(self):
        threads = int(os.environ.get('VSE_OCR_CPU_THREADS', min(4, os.cpu_count() or 1)))
        if threads < 1:
            raise ValueError('VSE_OCR_CPU_THREADS must be at least 1')
        # Paddle 3.3.1 oneDNN/PIR fails on PP-OCRv6 with DoubleAttribute.
        # Keep the tested CPU path portable; allow explicit future opt-in.
        return {
            'device': 'gpu:0' if self.has_cuda() else 'cpu',
            'engine': 'paddle',
            'cpu_threads': threads,
            'enable_mkldnn': os.environ.get('VSE_ENABLE_MKLDNN', '0') == '1',
        }
