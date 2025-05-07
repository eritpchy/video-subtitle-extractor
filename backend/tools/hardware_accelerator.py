from backend.config import tr
import paddle

class HardwareAccelerator:

    # 类变量，用于存储单例实例
    _instance = None

    @classmethod
    def instance(cls):
        """获取单例实例"""
        if cls._instance is None:
            cls._instance = HardwareAccelerator()
            cls._instance.initialize()
        return cls._instance

    def __init__(self):
        self.__gpu = False
        self.__onnx_providers = []
        self.__enabled = True

    def initialize(self):
        self.check_paddle()
        self.check_onnx()

    def check_paddle(self):
        # 如果paddlepaddle编译了gpu的版本
        if paddle.is_compiled_with_cuda():
        # 查看是否有可用的gpu
            if len(paddle.static.cuda_places()) > 0:
                # 如果有GPU则使用GPU
                self.__gpu = True

    def check_onnx(self):
        if self.__gpu:
            return
        try:
            import onnxruntime as ort
            available_providers = ort.get_available_providers()
            for provider in available_providers:
                if provider in [
                    "CPUExecutionProvider"
                ]:
                    continue
                if provider not in [
                    "DmlExecutionProvider",         # DirectML，适用于 Windows GPU
                    "ROCMExecutionProvider",        # AMD ROCm
                    "MIGraphXExecutionProvider",    # AMD MIGraphX
                    "VitisAIExecutionProvider",     # AMD VitisAI，适用于 RyzenAI & Windows, 实测和DirectML性能似乎差不多
                    "OpenVINOExecutionProvider",    # Intel GPU
                    "MetalExecutionProvider",       # Apple macOS
                    "CoreMLExecutionProvider",      # Apple macOS
                    "CUDAExecutionProvider",        # Nvidia GPU
                ]:
                    print(tr['Main']['OnnxExectionProviderNotSupportedSkipped'].format(provider))
                    continue
                print(tr['Main']['OnnxExecutionProviderDetected'].format(provider))
                self.__onnx_providers.append(provider)
        except ModuleNotFoundError as e:
            print(tr['Main']['OnnxRuntimeNotInstall'])

    def has_accelerator(self):
        if not self.__enabled:
            return False
        return self.__gpu or len(self.__onnx_providers) > 0

    def get_accelerator_name(self):
        if not self.__enabled:
            return "CPU"
        if self.__gpu:
            return "GPU"
        elif len(self.__onnx_providers) > 0:
            return ", ".join(self.__onnx_providers)
        else:
            return "CPU"

    def get_onnx_providers(self):
        if not self.__enabled:
            return []
        return self.__onnx_providers

    def has_gpu(self):
        if not self.__enabled:
            return False
        return self.__gpu
    
    def set_enabled(self, enable):
        self.__enabled = enable

        

    