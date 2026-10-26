# -*- coding: utf-8 -*-
"""
@Author  : Fang Yao
@Time    : 2021/3/24 9:28 上午
@FileName: main.py
@desc: 主程序入口文件
"""
import os
import re
import random
import math
import logging
import shutil
import traceback
import queue
from contextlib import nullcontext
from collections import Counter, namedtuple
import unicodedata
from threading import Thread
from pathlib import Path
import cv2
from Levenshtein import ratio
from PIL import Image
from numpy import average, dot, linalg
from tqdm import tqdm
import sys

sys.path.insert(0, os.path.dirname(__file__))
import subprocess
from backend.config import *
from backend.tools.hardware_accelerator import HardwareAccelerator
from backend.tools.native_ocr import selected_backend
from backend.tools import reformat
from backend.tools.ocr import OcrRecogniser, get_coordinates
from backend.tools import subtitle_ocr
from backend.tools.video_decode import read_frame, compatible_video
from backend.tools.paddle_model_config import PaddleModelConfig
from backend.tools.process_manager import ProcessManager
from backend.tools.videosubfinder import (build_command, resolve_binary, run_command,
                                         timestamp_to_frame, verify_binary, VideoSubFinderError, VideoSubFinderDecodeError)
from backend.tools.subtitle_detect import SubtitleDetect
from backend.bean.subtitle_area import SubtitleArea
import threading
import platform
import multiprocessing
import time
import pysrt

def default_extraction_work_dir(video_name):
    root = os.environ.get('VSE_WORK_DIR') or os.path.join(os.path.dirname(BASE_DIR), 'output')
    return os.path.join(root, str(video_name))


def use_detection_frame_extraction(hardware_accelerator, mode):
    """Preserve native CPU/CoreML algorithm parity; acceleration is not routing."""
    return selected_backend() not in ('native', 'python-coreml') and hardware_accelerator.has_accelerator() and mode == 'accurate'


class SubtitleExtractor:
    """
    视频字幕提取类
    """

    def __init__(self, vd_path, work_dir=None):
        # 线程锁
        self.lock = threading.RLock()
        # 用户指定的字幕区域位置
        self.sub_area = None
        self.hardware_accelerator = HardwareAccelerator.instance()
        # 是否使用硬件加速
        self.hardware_accelerator.set_enabled(config.hardwareAcceleration.value)
        self.model_config = PaddleModelConfig(self.hardware_accelerator)
        # 创建字幕检测对象
        # The VSF path does not need a second detection model in the parent.
        self.sub_detector = None
        # 视频路径
        self.video_path = str(Path(vd_path).expanduser().resolve())
        self.video_cap = cv2.VideoCapture(self.video_path)
        if not self.video_cap.isOpened():
            self.video_cap.release()
            raise ValueError(f"Cannot open video: {self.video_path}")
        # 通过视频路径获取视频名称
        self.vd_name = Path(self.video_path).stem
        # 临时存储文件夹
        self.temp_output_dir = (str(Path(work_dir).resolve()) if work_dir is not None else
                                default_extraction_work_dir(self.vd_name))
        # 视频帧总数
        self.frame_count = self.video_cap.get(cv2.CAP_PROP_FRAME_COUNT)
        # 视频帧率
        self.fps = self.video_cap.get(cv2.CAP_PROP_FPS)
        # 视频尺寸
        self.frame_height = int(self.video_cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.frame_width = int(self.video_cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        if (not math.isfinite(self.fps) or self.fps <= 0 or
                not math.isfinite(self.frame_count) or self.frame_count <= 0 or
                self.frame_width <= 0 or self.frame_height <= 0):
            self.video_cap.release()
            raise ValueError(f"Invalid video dimensions, frame rate or duration: {self.video_path}")
        # 提取的视频帧储存目录
        self.frame_output_dir = os.path.join(self.temp_output_dir, 'frames')
        # 提取的字幕文件存储目录
        self.subtitle_output_dir = os.path.join(self.temp_output_dir, 'subtitle')
        # 定义是否使用vsf提取字幕帧
        self.use_vsf = False
        # 定义vsf的字幕输出路径
        self.vsf_subtitle = os.path.join(self.subtitle_output_dir, 'raw_vsf.srt')
        # 提取的原始字幕文本存储路径
        self.raw_subtitle_path = os.path.join(self.subtitle_output_dir, 'raw.txt')
        # 定义输出字幕文件路径
        self.subtitle_output_path = os.path.splitext(self.video_path)[0] + '.srt'
        # 自定义ocr对象
        self.ocr = None
        # 总处理进度（帧提取100 + OCR100 + 后处理100 = 300）
        self.progress_total = 300
        # 视频帧提取进度
        self.progress_frame_extract = 0
        # OCR识别进度
        self.progress_ocr = 0
        # 后处理进度
        self.progress_post = 0
        # 是否完成
        self.isFinished = False
        # 字幕OCR任务队列
        self.subtitle_ocr_task_queue = None
        # 字幕OCR进度队列
        self.subtitle_ocr_progress_queue = None
        # vsf运行状态
        self.vsf_running = False
        # 进度监听器列表
        self.progress_listeners = []

    def run(self):
        """Run extraction, releasing the lock and child process on every path."""
        with self.lock:
            self._ocr_process = None
            self._ocr_process_id = None
            self._ocr_progress_stop = threading.Event()
            self._ocr_progress_thread = None
            self.isFinished = False
            self.use_vsf = False
            try:
                return self._run_impl()
            finally:
                self.vsf_running = False
                process = self._ocr_process
                if process is not None:
                    if process.is_alive():
                        process.terminate()
                        process.join(timeout=5)
                        if process.is_alive():
                            process.kill()
                            process.join(timeout=5)
                    if self._ocr_process_id is not None:
                        ProcessManager.instance().remove_process(self._ocr_process_id)
                self._ocr_progress_stop.set()
                if self._ocr_progress_thread is not None:
                    self._ocr_progress_thread.join(timeout=2)
                # A failed consumer can leave thousands of pending task writes.
                # Do not let multiprocessing's feeder-thread finalizer wait
                # forever on a pipe whose reader has already exited.
                for name in ('subtitle_ocr_task_queue', 'subtitle_ocr_progress_queue'):
                    channel = getattr(self, name, None)
                    if channel is not None:
                        for method in ('cancel_join_thread', 'close'):
                            close = getattr(channel, method, None)
                            if close is not None:
                                try:
                                    close()
                                except (OSError, ValueError):
                                    pass
                primary_error = sys.exc_info()[1]
                cleanup_errors = []
                for name in ('ocr', 'sub_detector'):
                    recognizer = getattr(self, name, None)
                    if recognizer is not None and hasattr(recognizer, 'close'):
                        try:
                            recognizer.close()
                        except Exception as error:
                            cleanup_errors.append(error)
                            logging.getLogger(__name__).exception('Failed to close %s', name)
                        finally:
                            setattr(self, name, None)
                if self.video_cap is not None:
                    self.video_cap.release()
                if cleanup_errors and primary_error is None:
                    raise cleanup_errors[0]

    def _run_impl(self):
        """
        运行整个提取视频的步骤
        """
        # 记录开始运行的时间
        start_time = time.time()
        # 重置进度条
        self.update_progress(ocr=0, frame_extract=0, post=0)
        self.append_output('-----------------------------')
        # 打印识别语言与识别模式
        self.append_output(f"  {tr['Main']['RecSubLang']}：{config.language.value}  |  {tr['Main']['RecMode']}：{config.mode.value}")
        # 如果使用GPU加速，则打印GPU加速提示
        if self.hardware_accelerator.has_accelerator():
            self.append_output(f"  {tr['Main']['AcceleratorON'].format(self.hardware_accelerator.accelerator_name)}")

        # 打印视频帧数与帧率
        self.append_output(f"  {tr['Main']['FrameCount']}：{self.frame_count}"
              f"  |  {tr['Main']['FrameRate']}：{self.fps}")
        # 打印加载模型信息
        self.append_output(f"  DET: {os.path.basename(self.model_config.DET_MODEL_PATH)}  |  REC: {os.path.basename(self.model_config.REC_MODEL_PATH)}")
        self.append_output('-----------------------------')
        # 打印视频帧提取开始提示
        self.append_output(tr['Main']['StartProcessFrame'])
        # 删除缓存
        self.__delete_frame_cache()
        # 若目录不存在，则创建文件夹
        if not os.path.exists(self.frame_output_dir):
            os.makedirs(self.frame_output_dir)
        if not os.path.exists(self.subtitle_output_dir):
            os.makedirs(self.subtitle_output_dir)
        self.capture_frame_with_subtitle_area()
        # 创建一个字幕OCR识别进程
        subtitle_ocr_process = self.start_subtitle_ocr_async()
        if self.sub_area is not None:
            if platform.system() in ['Windows', 'Linux', 'Darwin']:
                # Native CoreML and explicit native CPU retain the SAME VSF
                # pipeline/models. CoreML must not silently change the accurate
                # algorithm merely because an accelerator is requested.
                if use_detection_frame_extraction(self.hardware_accelerator, config.mode.value):
                    self.extract_frame_by_det()
                else:
                    self.extract_frame_by_vsf()
            else:
                self.extract_frame_by_fps()
        else:
            self.extract_frame_by_fps()

        # 往字幕OCR任务队列中，添加OCR识别任务结束标志
        # 任务格式为：(total_frame_count总帧数, current_frame_no当前帧, dt_box检测框, rec_res识别结果, 当前帧时间， subtitle_area字幕区域)
        self.subtitle_ocr_task_queue.put((self.frame_count, -1, None, None, None, None))
        # 等待子线程完成
        subtitle_ocr_process.join()
        if subtitle_ocr_process.exitcode != 0:
            raise RuntimeError(f"Subtitle OCR worker failed with exit code {subtitle_ocr_process.exitcode}")
        # 打印完成提示
        self.append_output(tr['Main']['FinishProcessFrame'])
        self.append_output(tr['Main']['FinishFindSub'])

        if self.sub_area is None:
            self.append_output(tr['Main']['StartDetectWaterMark'])
            # 询问用户视频是否有水印区域
            user_input = input(tr['Main']['checkWaterMark']).strip()
            if user_input == 'y':
                self.filter_watermark()
                self.append_output(tr['Main']['FinishDetectWaterMark'])
            else:
                self.append_output('-----------------------------')

        if self.sub_area is None:
            self.append_output(tr['Main']['StartDeleteNonSub'])
            self.filter_scene_text()
            self.append_output(tr['Main']['FinishDeleteNonSub'])

        self.update_progress(post=20)

        # 打印开始字幕生成提示
        self.append_output(tr['Main']['StartGenerateSub'])
        # 判断是否使用了vsf提取字幕
        if self.use_vsf:
            # 如果使用了vsf提取字幕，则使用vsf的字幕生成方法
            self.generate_subtitle_file_vsf()
        else:
            # 如果未使用vsf提取字幕，则使用常规字幕生成方法
            self.generate_subtitle_file()

        self.update_progress(post=90)

        if config.wordSegmentation.value:
            reformat.execute(self.subtitle_output_path, config.language.value)
        self.append_output(tr['Main']['FinishGenerateSub'], f"{round(time.time() - start_time, 2)}s")
        self.append_output('-----------------------------')
        self.update_progress(ocr=100, frame_extract=100, post=100)
        self.isFinished = True
        # 删除缓存文件
        self.empty_cache()
        if config.generateTxt.value:
            self.srt2txt(self.subtitle_output_path)

    def capture_frame_with_subtitle_area(self):
        """
        截取视频的一帧，并在上面绘制字幕区域，保存到temp_output_dir/sub_area.jpg
        """
        # 确保输出目录存在
        if not os.path.exists(self.temp_output_dir):
            os.makedirs(self.temp_output_dir)
            
        # 确保视频已打开
        if self.video_cap is None or not self.video_cap.isOpened():
            self.video_cap = cv2.VideoCapture(self.video_path)
            
        # 将视频指针设置到第一帧
        # self.video_cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        
        # 读取第一帧
        self._needs_compatible_video = False
        ret, frame = read_frame(self.video_cap, self.video_path, frame_index=0,
                               on_fallback=lambda: setattr(self, '_needs_compatible_video', True))
        
        if ret:
            # 如果有字幕区域，绘制矩形
            sub_area = self.sub_area
            if sub_area is not None:
                # 绘制绿色矩形框
                cv2.rectangle(frame, (sub_area.xmin, sub_area.ymin), (sub_area.xmax, sub_area.ymax), (0, 255, 0), 2)
                # 添加文字标注
                cv2.putText(frame, "Subtitle Area", (sub_area.xmin, sub_area.ymin - 10), 
                           cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 0), 2)
            
            # 保存图像
            output_path = os.path.join(self.temp_output_dir, 'sub_area.jpg')
            cv2.imwrite(output_path, frame)
            
            # 重置视频指针到第一帧
            self.video_cap.set(cv2.CAP_PROP_POS_FRAMES, 0)

    def extract_frame_by_fps(self):
        """
        根据帧率，定时提取视频帧，容易丢字幕，但速度快，将提取到的视频帧加入ocr识别任务队列
        """
        # 计算采样间隔（每隔多少帧取一帧）
        frame_interval = int(self.fps // config.extractFrequency.value)
        if frame_interval < 1:
            frame_interval = 1
        # 当前视频帧的帧号
        current_frame_no = 0
        total_frames = int(self.frame_count)
        while current_frame_no < total_frames:
            # 直接跳到目标帧
            ret, frame = read_frame(self.video_cap, self.video_path, frame_index=current_frame_no)
            if not ret:
                break
            current_frame_no += 1
            # subtitle_ocr_task_queue: (total_frame_count总帧数, current_frame_no当前帧, dt_box检测框, rec_res识别结果, 当前帧时间，subtitle_area字幕区域)
            task = (self.frame_count, current_frame_no, None, None, None, config.subtitleArea.value)
            self.subtitle_ocr_task_queue.put(task)
            # 更新进度条
            self.update_progress(frame_extract=(current_frame_no / self.frame_count) * 100)
            # 跳到下一个采样帧
            current_frame_no = current_frame_no + frame_interval - 1

        self.video_cap.release()

    def extract_frame_by_det(self):
        """
        通过检测字幕区域位置提取字幕帧
        """
        if self.sub_detector is None:
            self.sub_detector = SubtitleDetect()

        # 当前视频帧的帧号
        current_frame_no = 0
        frame_lru_list = []
        frame_lru_list_max_size = 2
        ocr_args_list = []
        compare_ocr_result_cache = {}
        tbar = tqdm(total=int(self.frame_count), unit='f', position=0, file=sys.__stdout__)
        first_flag = True
        is_finding_start_frame_no = False
        is_finding_end_frame_no = False
        start_frame_no = 0
        start_end_frame_no = []
        start_frame = None
        if self.ocr is None:
            self.ocr = OcrRecogniser()
        while self.video_cap.isOpened():
            ret, frame = read_frame(self.video_cap, self.video_path, frame_index=current_frame_no)
            # 如果读取视频帧失败（视频读到最后一帧）
            if not ret:
                break
            # 读取视频帧成功
            current_frame_no += 1
            tbar.update(1)
            dt_boxes, elapse = self.sub_detector.detect_subtitle(frame)
            has_subtitle = False
            sub_area = self.sub_area
            if sub_area is not None:
                coordinate_list = get_coordinates(dt_boxes.tolist())
                if coordinate_list:
                    for coordinate in coordinate_list:
                        xmin, xmax, ymin, ymax = coordinate
                        if (sub_area.xmin <= xmin and xmax <= sub_area.xmax
                                and sub_area.ymin <= ymin
                                and ymax <= sub_area.ymax):
                            has_subtitle = True
                            # 检测到字幕时，如果列表为空，则为字幕头
                            if first_flag:
                                is_finding_start_frame_no = True
                                first_flag = False
                            break
            else:
                has_subtitle = len(dt_boxes) > 0
            # 检测到包含字幕帧的起始帧号与结束帧号
            if has_subtitle:
                # 判断是字幕头还是尾
                if is_finding_start_frame_no:
                    start_frame_no = current_frame_no
                    dt_box, rec_res = self.ocr.predict(frame)
                    area_text1 = "".join(self.__get_area_text((dt_box, rec_res)))
                    if start_frame_no not in compare_ocr_result_cache.keys():
                        compare_ocr_result_cache[current_frame_no] = {'text': area_text1, 'dt_box': dt_box, 'rec_res': rec_res}
                        frame_lru_list.append((frame, current_frame_no))
                        ocr_args_list.append((self.frame_count, current_frame_no))
                        # 缓存头帧
                        start_frame = frame
                    # 开始找尾
                    is_finding_start_frame_no = False
                    is_finding_end_frame_no = True
                # 判断是否为最后一帧
                if is_finding_end_frame_no and current_frame_no == self.frame_count:
                    is_finding_end_frame_no = False
                    is_finding_start_frame_no = False
                    end_frame_no = current_frame_no
                    frame_lru_list.append((frame, current_frame_no))
                    ocr_args_list.append((self.frame_count, current_frame_no))
                    start_end_frame_no.append((start_frame_no, end_frame_no))
                # 如果在找结束帧的时候
                if is_finding_end_frame_no:
                    # 判断该帧与头帧ocr内容是否一致,若不一致则找到尾，尾巴为前一帧
                    if not self._compare_ocr_result(compare_ocr_result_cache, None, start_frame_no, frame, current_frame_no):
                        is_finding_end_frame_no = False
                        is_finding_start_frame_no = True
                        end_frame_no = current_frame_no - 1
                        frame_lru_list.append((start_frame, end_frame_no))
                        ocr_args_list.append((self.frame_count, end_frame_no))
                        start_end_frame_no.append((start_frame_no, end_frame_no))

            else:
                # 如果检测到字幕头后有没有字幕，则找到结尾，尾巴为前一帧
                if is_finding_end_frame_no:
                    end_frame_no = current_frame_no - 1
                    is_finding_end_frame_no = False
                    is_finding_start_frame_no = True
                    frame_lru_list.append((start_frame, end_frame_no))
                    ocr_args_list.append((self.frame_count, end_frame_no))
                    start_end_frame_no.append((start_frame_no, end_frame_no))

            while len(frame_lru_list) > frame_lru_list_max_size:
                frame_lru_list.pop(0)

            # if len(start_end_frame_no) > 0:
                # self.append_output(start_end_frame_no)

            while len(ocr_args_list) > 1:
                total_frame_count, ocr_info_frame_no = ocr_args_list.pop(0)
                if current_frame_no in compare_ocr_result_cache:
                    predict_result = compare_ocr_result_cache[current_frame_no]
                    dt_box, rec_res = predict_result['dt_box'], predict_result['rec_res']
                else:
                    dt_box, rec_res = None, None
                # subtitle_ocr_task_queue: (total_frame_count总帧数, current_frame_no当前帧, dt_box检测框, rec_res识别结果, 当前帧时间， subtitle_area字幕区域)
                task = (total_frame_count, ocr_info_frame_no, dt_box, rec_res, None, config.subtitleArea.value)
                # 添加任务
                self.subtitle_ocr_task_queue.put(task)
                self.update_progress(frame_extract=(current_frame_no / self.frame_count) * 100)

        while len(ocr_args_list) > 0:
            total_frame_count, ocr_info_frame_no = ocr_args_list.pop(0)
            if current_frame_no in compare_ocr_result_cache:
                predict_result = compare_ocr_result_cache[current_frame_no]
                dt_box, rec_res = predict_result['dt_box'], predict_result['rec_res']
            else:
                dt_box, rec_res = None, None
            task = (total_frame_count, ocr_info_frame_no, dt_box, rec_res, None, config.subtitleArea.value)
            # 添加任务
            self.subtitle_ocr_task_queue.put(task)
        self.video_cap.release()

    def extract_frame_by_vsf(self):
        """Extract timed subtitle frames with the portable CLI on every OS."""
        binary = resolve_binary(BASE_DIR)
        version = verify_binary(binary)
        self.append_output(f"VideoSubFinderCli {version}: {binary}")
        if self.video_cap:
            self.video_cap.release()
            self.video_cap = None
        threads = config.videoSubFinderCpuCores.value or max(multiprocessing.cpu_count() - 2, 2)
        command = build_command(
            binary, self.video_path, self.temp_output_dir, self.vsf_subtitle,
            crop=(1 - self.sub_area.ymin / self.frame_height,
                  1 - self.sub_area.ymax / self.frame_height,
                  self.sub_area.xmin / self.frame_width,
                  self.sub_area.xmax / self.frame_width),
            threads=threads, decoder=config.videoSubFinderDecoder.value.value.lower(),
            use_cuda=os.environ.get("VSE_VSF_CUDA") == "1")
        process_id = None

        def register_process(process):
            nonlocal process_id
            if process_id is not None:
                ProcessManager.instance().remove_process(process_id)
            process_id = ProcessManager.instance().add_process(process)
            self.manage_process(process.pid)

        self.vsf_running = True
        try:
            needs_compatible = getattr(self, '_needs_compatible_video', False)
            while True:
                source = nullcontext(self.video_path)
                if needs_compatible:
                    self.append_output("Video decoder cannot read this codec; preparing a lossless temporary video for VideoSubFinder")
                    source = compatible_video(self.video_path, self.temp_output_dir, on_spawn=register_process)
                try:
                    with source as compatible_path:
                        command[command.index('-i') + 1] = str(compatible_path)
                        result = run_command(command,
                            on_progress=lambda value: self.update_progress(frame_extract=value),
                            on_log=self.append_output, on_spawn=register_process)
                    break
                except VideoSubFinderDecodeError:
                    if needs_compatible:
                        raise  # Never loop or accept a failed compatibility decode.
                    # Python OpenCV and the native VSF package have independent
                    # decoder builds. Retry only an explicit native decode error,
                    # not a legitimate empty subtitle result or unrelated failure.
                    needs_compatible = True
                    if os.path.isfile(self.vsf_subtitle):
                        os.remove(self.vsf_subtitle)
            if not os.path.isfile(self.vsf_subtitle):
                raise VideoSubFinderError("VideoSubFinderCli did not write the requested SRT timing file")
            # Frame events can arrive out of order from the CLI worker pool.
            # Keep timestamp zero and sort/deduplicate before feeding the OCR
            # worker, which expects chronological frames when merging text.
            for total_ms in result.timestamps_ms:
                frame_no = self._timestamp_to_frameno(total_ms)
                self.subtitle_ocr_task_queue.put(
                    (self.frame_count, frame_no, None, None, total_ms, config.subtitleArea.value))
            self.use_vsf = True
        finally:
            self.vsf_running = False
            if process_id is not None:
                ProcessManager.instance().remove_process(process_id)

    def filter_watermark(self):
        """
        去除原始字幕文本中的水印区域的文本
        """
        # 获取潜在水印区域
        watermark_areas = self._detect_watermark_area()

        # 随机选择一帧, 将所水印区域标记出来，用户看图判断是否是水印区域
        cap = cv2.VideoCapture(self.video_path)
        ret, sample_frame = False, None
        try:
            for i in range(10):
                frame_no = random.randint(int(self.frame_count * 0.1), int(self.frame_count * 0.9))
                ret, sample_frame = read_frame(cap, self.video_path, frame_index=frame_no)
                if ret:
                    break
        finally:
            cap.release()

        if not ret:
            self.append_output("Error in filter_watermark: reading frame from video")
            return

        # 给潜在的水印区域编号
        area_num = ['E', 'D', 'C', 'B', 'A']

        for watermark_area in watermark_areas:
            ymin = min(watermark_area[0][2], watermark_area[0][3])
            ymax = max(watermark_area[0][3], watermark_area[0][2])
            xmin = min(watermark_area[0][0], watermark_area[0][1])
            xmax = max(watermark_area[0][1], watermark_area[0][0])
            cover = sample_frame[ymin:ymax, xmin:xmax]
            cover = cv2.blur(cover, (10, 10))
            cv2.rectangle(cover, pt1=(0, cover.shape[0]), pt2=(cover.shape[1], 0), color=(0, 0, 255), thickness=3)
            sample_frame[ymin:ymax, xmin:xmax] = cover
            position = ((xmin + xmax) // 2, ymax)

            cv2.putText(sample_frame, text=area_num.pop(), org=position, fontFace=cv2.FONT_HERSHEY_SIMPLEX,
                        fontScale=1, color=(255, 0, 0), thickness=2, lineType=cv2.LINE_AA)

        sample_frame_file_path = os.path.join(os.path.dirname(self.frame_output_dir), 'watermark_area.jpg')
        cv2.imwrite(sample_frame_file_path, sample_frame)
        self.append_output(f"{tr['Main']['WatchPicture']}: {sample_frame_file_path}")

        area_num = ['E', 'D', 'C', 'B', 'A']
        for watermark_area in watermark_areas:
            user_input = input(f"{area_num.pop()}{str(watermark_area)} "
                               f"{tr['Main']['QuestionDelete']}").strip()
            if user_input == 'y' or user_input == '\n':
                with open(self.raw_subtitle_path, mode='r+', encoding='utf-8') as f:
                    content = f.readlines()
                    f.seek(0)
                    for i in content:
                        if i.find(str(watermark_area[0])) == -1:
                            f.write(i)
                    f.truncate()
                self.append_output(tr['Main']['FinishDelete'])
        self.append_output(tr['Main']['FinishWaterMarkFilter'])
        # 删除缓存
        if os.path.exists(sample_frame_file_path):
            os.remove(sample_frame_file_path)

    def filter_scene_text(self):
        """
        将场景里提取的文字过滤，仅保留字幕区域
        """
        # 获取潜在字幕区域
        subtitle_area = self._detect_subtitle_area()[0][0]

        # 随机选择一帧，将所水印区域标记出来，用户看图判断是否是水印区域
        cap = cv2.VideoCapture(self.video_path)
        ret, sample_frame = False, None
        try:
            for i in range(10):
                frame_no = random.randint(int(self.frame_count * 0.1), int(self.frame_count * 0.9))
                ret, sample_frame = read_frame(cap, self.video_path, frame_index=frame_no)
                if ret:
                    break
        finally:
            cap.release()

        if not ret:
            self.append_output("Error in filter_scene_text: reading frame from video")
            return

        # 为了防止有双行字幕，根据容忍度，将字幕区域y范围加高
        ymin = abs(subtitle_area[0] - config.subtitleAreaDeviationPixel.value)
        ymax = subtitle_area[1] + config.subtitleAreaDeviationPixel.value
        # 画出字幕框的区域
        cv2.rectangle(sample_frame, pt1=(0, ymin), pt2=(sample_frame.shape[1], ymax), color=(0, 0, 255), thickness=3)
        sample_frame_file_path = os.path.join(os.path.dirname(self.frame_output_dir), 'subtitle_area.jpg')
        cv2.imwrite(sample_frame_file_path, sample_frame)
        self.append_output(f"{tr['Main']['CheckSubArea']} {sample_frame_file_path}")

        user_input = input(f"{(ymin, ymax)} {tr['Main']['DeleteNoSubArea']}").strip()
        if user_input == 'y' or user_input == '\n':
            with open(self.raw_subtitle_path, mode='r+', encoding='utf-8') as f:
                content = f.readlines()
                f.seek(0)
                for i in content:
                    i_ymin = int(i.split('\t')[1].split('(')[1].split(')')[0].split(', ')[2])
                    i_ymax = int(i.split('\t')[1].split('(')[1].split(')')[0].split(', ')[3])
                    if ymin <= i_ymin and i_ymax <= ymax:
                        f.write(i)
                f.truncate()
            self.append_output(tr['Main']['FinishDeleteNoSubArea'])
        # 删除缓存
        if os.path.exists(sample_frame_file_path):
            os.remove(sample_frame_file_path)

    def generate_subtitle_file(self):
        """
        生成srt格式的字幕文件
        """
        if not self.use_vsf:
            subtitle_content = self._remove_duplicate_subtitle()
            # 保存持续时间不足1秒的字幕行，用于后续处理
            post_process_subtitle = []
            with open(self.subtitle_output_path, mode='w', encoding='utf-8') as f:
                for index, content in enumerate(subtitle_content):
                    line_code = index + 1
                    frame_start = self._frame_to_timecode(int(content[0]))
                    # 比较起始帧号与结束帧号， 如果字幕持续时间不足1秒，则将显示时间设为1s
                    if abs(int(content[1]) - int(content[0])) < self.fps:
                        frame_end = self._frame_to_timecode(int(int(content[0]) + self.fps))
                        post_process_subtitle.append(line_code)
                    else:
                        frame_end = self._frame_to_timecode(int(content[1]))
                    frame_content = content[2]
                    subtitle_line = f'{line_code}\n{frame_start} --> {frame_end}\n{frame_content}\n\n'
                    f.write(subtitle_line)
            self.append_output(tr['Main']['SubLocation'].format(self.subtitle_output_path))
            # 返回持续时间低于1s的字幕行
            return post_process_subtitle

    def generate_subtitle_file_vsf(self):
        if not self.use_vsf:
            return
        subs = pysrt.open(self.vsf_subtitle)
        sub_no_map = {}
        for sub in subs:
            sub.start.no = self._timestamp_to_frameno(sub.start.ordinal)
            sub_no_map[sub.start.no] = sub

        subtitle_content = self._remove_duplicate_subtitle()
        subtitle_content_start_map = {int(a[0]): a for a in subtitle_content}
        final_subtitles = []
        for sub in subs:
            found = sub.start.no in subtitle_content_start_map
            if found:
                subtitle_content_line = subtitle_content_start_map[sub.start.no]
                sub.text = subtitle_content_line[2]
                end_no = int(subtitle_content_line[1])
                sub.end = sub_no_map[end_no].end if end_no in sub_no_map else sub.end
                sub.index = len(final_subtitles) + 1
                final_subtitles.append(sub)

            if not found and not config.deleteEmptyTimeStamp.value:
                # 保留时间轴
                sub.text = ""
                sub.index = len(final_subtitles) + 1
                final_subtitles.append(sub)
                continue

        pysrt.SubRipFile(final_subtitles).save(self.subtitle_output_path, encoding='utf-8')
        self.append_output(tr['Main']['SubLocation'].format(self.subtitle_output_path))

    def _detect_watermark_area(self):
        """
        根据识别出来的raw txt文件中的坐标点信息，查找水印区域
        假定：水印区域（台标）的坐标在水平和垂直方向都是固定的，也就是具有(xmin, xmax, ymin, ymax)相对固定
        根据坐标点信息，进行统计，将一直具有固定坐标的文本区域选出
        :return 返回最有可能的水印区域
        """
        with open(self.raw_subtitle_path, mode='r', encoding='utf-8') as f:
            lines = f.readlines()
        # 一次性解析所有行
        parsed = []
        for line in lines:
            parts = line.split('\t')
            if len(parts) < 3:
                continue
            frame_no = parts[0]
            text_position = parts[1].split('(')[1].split(')')[0].split(', ')
            content = parts[2]
            coordinate = (int(text_position[0]), int(text_position[1]),
                          int(text_position[2]), int(text_position[3]))
            parsed.append((frame_no, coordinate, content))

        frame_no_list = [p[0] for p in parsed]
        coordinates_list = [p[1] for p in parsed]
        content_list = [p[2] for p in parsed]
        # 将坐标列表的相似值统一
        coordinates_list = self._unite_coordinates(coordinates_list)

        # 将原txt文件的坐标更新为归一后的坐标
        with open(self.raw_subtitle_path, mode='w', encoding='utf-8') as f:
            for frame_no, coordinate, content in zip(frame_no_list, coordinates_list, content_list):
                f.write(f'{frame_no}\t{coordinate}\t{content}')

        count = Counter(coordinates_list).most_common()
        num = min(config.waterarkAreaNum.value, len(count))
        return count[:num]

    def _detect_subtitle_area(self):
        """
        读取过滤水印区域后的raw txt文件，根据坐标信息，查找字幕区域
        假定：字幕区域在y轴上有一个相对固定的坐标范围，相对于场景文本，这个范围出现频率更高
        :return 返回字幕的区域位置
        """
        with open(self.raw_subtitle_path, mode='r', encoding='utf-8') as f:
            lines = f.readlines()
        y_coordinates_list = []
        for line in lines:
            parts = line.split('\t')
            if len(parts) < 2:
                continue
            text_position = parts[1].split('(')[1].split(')')[0].split(', ')
            y_coordinates_list.append((int(text_position[2]), int(text_position[3])))
        return Counter(y_coordinates_list).most_common(1)

    def _frame_to_timecode(self, frame_no):
        """
        将视频帧转换成时间（纯计算，不打开视频文件）
        :param frame_no: 视频的帧号，i.e. 第几帧视频帧
        :returns: SMPTE格式时间戳 as string, 如'01:02:12,032'
        """
        total_ms = frame_no / self.fps * 1000
        total_ms_int = int(total_ms)
        milliseconds = total_ms_int % 1000
        total_seconds = total_ms_int // 1000
        seconds = total_seconds % 60
        minutes = (total_seconds // 60) % 60
        hours = total_seconds // 3600
        return "%02d:%02d:%02d,%03d" % (hours, minutes, seconds, milliseconds)

    def _timestamp_to_frameno(self, time_ms):
        return timestamp_to_frame(time_ms, self.fps)

    def _frameno_to_milliseconds(self, frame_no):
        return float(int(frame_no / self.fps * 1000))

    def _remove_duplicate_subtitle(self):
        """
        读取原始的raw txt，去除重复行，返回去除了重复后的字幕列表
        """
        RawInfo = namedtuple('RawInfo', 'no content')
        content_list = [RawInfo(frame_no, content)
                        for frame_no, _, content in self._concat_content_with_same_frameno()]
        # 去重后的字幕列表
        unique_subtitle_list = []
        idx_i = 0
        content_list_len = len(content_list)
        # 循环遍历每行字幕，记录开始时间与结束时间
        last_reported_idx = -1
        report_interval = max(1, content_list_len // 100)
        while idx_i < content_list_len:
            i = content_list[idx_i]
            start_frame = i.no
            idx_j = idx_i
            while idx_j < content_list_len:
                # 定期更新去重进度（post: 20→85），基于 idx_j 推进
                if idx_j - last_reported_idx >= report_interval:
                    last_reported_idx = idx_j
                    self.update_progress(post=20 + int(65 * idx_j / content_list_len))
                # 计算当前行与下一行的Levenshtein距离
                # 判决idx_j的下一帧是否与idx_i不同，若不同（或者是最后一帧）则找到结束帧
                if idx_j + 1 == content_list_len or ratio(i.content.replace(' ', ''), content_list[idx_j + 1].content.replace(' ', '')) < (config.thresholdTextSimilarity.value / 100.0):
                    # 若找到终点帧,定义字幕结束帧帧号
                    end_frame = content_list[idx_j].no
                    if not self.use_vsf:
                        if end_frame == start_frame and idx_j + 1 < content_list_len:
                            # 针对只有一帧的情况，以下一帧的开始时间为准(除非是最后一帧)
                            end_frame = content_list[idx_j + 1][0]
                    # 寻找最长字幕
                    similar_list = content_list[idx_i:idx_j + 1]
                    similar_content_strip_list = [item.content.replace(' ', '') for item in similar_list]
                    index, _ = max(enumerate(similar_content_strip_list), key=lambda x: len(x[1]))

                    # 添加进列表
                    unique_subtitle_list.append((start_frame, end_frame, similar_list[index].content))
                    idx_i = idx_j + 1
                    break
                else:
                    idx_j += 1
                    continue
        return unique_subtitle_list

    def _concat_content_with_same_frameno(self):
        """Group OCR boxes into visual rows without destroying the raw cache.

        Same-row fragments are ordered left-to-right; distinct rows retain an
        SRT newline. Returning structured text avoids embedding newlines in TSV.
        """
        from ast import literal_eval
        from collections import OrderedDict
        from backend.tools.subtitle_layout import join_visual_rows

        grouped = OrderedDict()
        with open(self.raw_subtitle_path, encoding='utf-8') as stream:
            for line in stream:
                parts = line.rstrip('\r\n').split('\t', 2)
                if len(parts) != 3:
                    continue
                frame_no, coordinate, content = parts
                bounds = literal_eval(coordinate)
                grouped.setdefault(frame_no, []).append((bounds, content))
        return [(frame_no, boxes[0][0], join_visual_rows(boxes))
                for frame_no, boxes in grouped.items()]

    def _unite_coordinates(self, coordinates_list):
        """
        给定一个坐标列表，将这个列表中相似的坐标统一为一个值
        e.g. 由于检测框检测的结果不是一致的，相同位置文字的坐标可能一次检测为(255,123,456,789)，另一次检测为(253,122,456,799)
        因此要对相似的坐标进行值的统一
        :param coordinates_list 包含坐标点的列表
        :return: 返回一个统一值后的坐标列表
        """
        if not coordinates_list:
            return coordinates_list
        # 按xmin排序后用滑动窗口，相似坐标的xmin差值在容忍范围内
        n = len(coordinates_list)
        indexed = sorted(enumerate(coordinates_list), key=lambda x: x[1][0])
        # parent数组用于并查集
        parent = list(range(n))
        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        def union(i, j):
            ri, rj = find(i), find(j)
            if ri != rj:
                # 保留较小索引的坐标作为代表
                if ri > rj:
                    ri, rj = rj, ri
                parent[rj] = ri
        # 滑动窗口：xmin已排序，只要xmin差值超过容忍度就移动左边界
        left = 0
        for right in range(n):
            while left < right and indexed[right][1][0] - indexed[left][1][0] >= config.tolerantPixelX:
                left += 1
            # 检查窗口内所有元素与当前元素是否相似
            for k in range(left, right):
                if self.__is_coordinate_similar(indexed[k][1], indexed[right][1]):
                    union(indexed[k][0], indexed[right][0])
        # 将所有坐标统一为其组代表的坐标
        result = list(coordinates_list)
        for i in range(n):
            result[i] = coordinates_list[find(i)]
        return result

    def _compute_image_similarity(self, image1, image2):
        """
        计算两张图片的余弦相似度
        """
        image1 = self.__get_thum(image1)
        image2 = self.__get_thum(image2)
        images = [image1, image2]
        vectors = []
        norms = []
        for image in images:
            vector = []
            for pixel_tuple in image.getdata():
                vector.append(average(pixel_tuple))
            vectors.append(vector)
            # linalg=linear（线性）+algebra（代数），norm则表示范数
            # 求图片的范数
            norms.append(linalg.norm(vector, 2))
        a, b = vectors
        a_norm, b_norm = norms
        # dot返回的是点积，对二维数组（矩阵）进行计算
        res = dot(a / a_norm, b / b_norm)
        return res

    def __get_area_text(self, ocr_result):
        """
        获取字幕区域内的文本内容
        """
        box, text = ocr_result
        coordinates = get_coordinates(box)
        area_text = []
        for content, coordinate in zip(text, coordinates):
            sub_area = self.sub_area
            if sub_area is not None:
                xmin = coordinate[0]
                xmax = coordinate[1]
                ymin = coordinate[2]
                ymax = coordinate[3]
                if sub_area.xmin <= xmin and xmax <= sub_area.xmax and sub_area.ymin <= ymin and ymax <= sub_area.ymax:
                    area_text.append(content[0])
        return area_text

    def _compare_ocr_result(self, result_cache, img1, img1_no, img2, img2_no):
        """
        比较两张图片预测出的字幕区域文本是否相同
        """
        if self.ocr is None:
            self.ocr = OcrRecogniser()
        if img1_no in result_cache:
            area_text1 = result_cache[img1_no]['text']
        else:
            dt_box, rec_res = self.ocr.predict(img1)
            area_text1 = "".join(self.__get_area_text((dt_box, rec_res)))
            result_cache[img1_no] = {'text': area_text1, 'dt_box': dt_box, 'rec_res': rec_res}

        if img2_no in result_cache:
            area_text2 = result_cache[img2_no]['text']
        else:
            dt_box, rec_res = self.ocr.predict(img2)
            area_text2 = "".join(self.__get_area_text((dt_box, rec_res)))
            result_cache[img2_no] = {'text': area_text2, 'dt_box': dt_box, 'rec_res': rec_res}
        delete_no_list = []
        for no in result_cache:
            if no < min(img1_no, img2_no) - 10:
                delete_no_list.append(no)
        for no in delete_no_list:
            del result_cache[no]
        if ratio(area_text1, area_text2) > config.thresholdTextSimilarity.value / 100.0:
            return True
        else:
            return False

    @staticmethod
    def __is_coordinate_similar(coordinate1, coordinate2):
        """
        计算两个坐标是否相似，如果两个坐标点的xmin,xmax,ymin,ymax的差值都在像素点容忍度内
        则认为这两个坐标点相似
        """
        return abs(coordinate1[0] - coordinate2[0]) < config.tolerantPixelX and \
            abs(coordinate1[1] - coordinate2[1]) < config.tolerantPixelX and \
            abs(coordinate1[2] - coordinate2[2]) < config.tolerantPixelY and \
            abs(coordinate1[3] - coordinate2[3]) < config.tolerantPixelY

    @staticmethod
    def __get_thum(image, size=(64, 64), greyscale=False):
        """
        对图片进行统一化处理
        """
        # 利用image对图像大小重新设置, Image.Resampling.LANCZOS为高质量的
        image = image.resize(size, Image.Resampling.LANCZOS)
        if greyscale:
            # 将图片转换为L模式，其为灰度图，其每个像素用8个bit表示
            image = image.convert('L')
        return image

    def __delete_frame_cache(self):
        if os.path.exists(self.temp_output_dir):
            shutil.rmtree(self.temp_output_dir, True)

    def empty_cache(self):
        """
        删除字幕提取过程中所有生产的缓存文件
        """
        if not config.debugNoDeleteCache.value:
            if os.path.exists(self.temp_output_dir):
                shutil.rmtree(self.temp_output_dir, True)

    def update_progress(self, ocr=None, frame_extract=None, post=None):
        """
        更新进度条
        :param ocr ocr进度
        :param frame_extract 视频帧提取进度
        :param post 后处理进度
        """
        if ocr is not None:
            self.progress_ocr = max(0, min(100, ocr))  # Clamp value between 0 and 100
        if frame_extract is not None:
            self.progress_frame_extract = max(0, min(100, frame_extract))
        if post is not None:
            self.progress_post = max(0, min(100, post))
        # 通知所有监听器
        self.notify_progress_listeners()

    def start_subtitle_ocr_async(self):
        def get_ocr_progress():
            """
            获取ocr识别进度
            生产者在所有帧入队后发送 (-2, total_tasks) 告知总帧数
            消费者每处理一帧发送 (frame_no, processed_count)
            消费者处理完全部帧后发送 (-1, total_tasks)
            """
            notify = True
            total_tasks = None
            while not self._ocr_progress_stop.is_set():
                try:
                    value = self.subtitle_ocr_progress_queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                except (EOFError, OSError, ValueError):
                    return
                if value is None:
                    return
                if notify:
                    self.append_output(tr['Main']['StartFindSub'])
                    notify = False
                # 生产者告知总帧数 value = (-2, total_tasks)
                if isinstance(value, tuple) and len(value) == 2 and value[0] == -2:
                    total_tasks = value[1]
                    continue
                # 终止条件
                if value == -1:
                    self.update_progress(ocr=100)
                    return
                if isinstance(value, tuple) and value[0] == -1:
                    self.update_progress(ocr=100)
                    return
                # value = (frame_no, processed_count)
                if isinstance(value, tuple) and len(value) == 2:
                    _, processed_count = value
                    if total_tasks and total_tasks > 0:
                        ocr_pct = min(99, processed_count / total_tasks * 100)
                    else:
                        ocr_pct = min(99, processed_count)
                    self.update_progress(ocr=ocr_pct)
        options = {
            'REC_CHAR_TYPE': config.language.value,
            'MODE': config.mode.value,
            'REC_BATCH_NUMBER': config.recBatchNumber.value,
            'DROP_SCORE': config.dropScore.value / 100.0,
            'SUB_AREA_DEVIATION_RATE': config.subtitleAreaDeviationRate.value / 100.0,
            'DEBUG_OCR_LOSS': config.debugOcrLoss.value,
            'HARDWARD_ACCELERATOR': self.hardware_accelerator,
        }
        process, task_queue, progress_queue = subtitle_ocr.async_start(self.video_path, self.raw_subtitle_path, self.sub_area, options)
        self._ocr_process = process
        self._ocr_process_id = ProcessManager.instance().add_process(process)
        self.manage_process(process.pid)
        self.subtitle_ocr_task_queue = task_queue
        self.subtitle_ocr_progress_queue = progress_queue
        # 开启线程负责更新OCR进度
        self._ocr_progress_thread = Thread(target=get_ocr_progress, daemon=True)
        self._ocr_progress_thread.start()
        return process

    def srt2txt(self, srt_file):
        subs = pysrt.open(srt_file, encoding='utf-8')
        output_path = os.path.join(os.path.dirname(srt_file), Path(srt_file).stem + '.txt')
        self.append_output(output_path)
        with open(output_path, 'w', encoding='utf-8') as f:
            for sub in subs:
                f.write(f'{sub.text}\n')

    def append_output(self, *args):
        """输出信息到控制台
        Args:
            *args: 要输出的内容，多个参数将用空格连接
        """
        print(*args)

    def add_progress_listener(self, listener):
        """
        添加进度监听器
        
        Args:
            listener: 一个回调函数，接收参数 (progress_ocr, progress_frame_extract, progress_total, isFinished)
        """
        if listener not in self.progress_listeners:
            self.progress_listeners.append(listener)
    
    def remove_progress_listener(self, listener):
        """
        移除进度监听器
        
        Args:
            listener: 要移除的监听器函数
        """
        if listener in self.progress_listeners:
            self.progress_listeners.remove(listener)
            
    def notify_progress_listeners(self):
        """
        通知所有进度监听器当前进度
        """
        for listener in self.progress_listeners:
            try:
                listener(self.progress_ocr, self.progress_frame_extract, self.progress_total, self.isFinished, self.progress_post)
            except Exception as e:
                traceback.print_exc()

    def manage_process(self, pid):
        pass

if __name__ == '__main__':
    multiprocessing.set_start_method("spawn")
    # 提示用户输入视频路径
    video_path = input(f"{tr['Main']['InputVideo']}").strip()
    # 提示用户输入字幕区域
    try:
        y_min, y_max, x_min, x_max = map(int, input(
            f"{tr['Main']['ChooseSubArea']} (ymin ymax xmin xmax)：").split())
        subtitle_area = SubtitleArea(y_min, y_max, x_min, x_max)
    except ValueError as e:
        subtitle_area = None
    # 新建字幕提取对象
    se = SubtitleExtractor(video_path)
    se.sub_area = subtitle_area
    # 开始提取字幕
    se.run()
