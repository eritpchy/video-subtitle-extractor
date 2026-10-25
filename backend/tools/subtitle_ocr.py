from backend.tools.video_decode import read_frame
import os
import re
from multiprocessing import Queue, Process
import cv2
from PIL import ImageFont, ImageDraw, Image
from tqdm import tqdm
from backend.tools.ocr import OcrRecogniser, get_coordinates
from backend.tools.constant import SubtitleArea
from backend.tools import constant
from threading import Event, Thread
import queue
from types import SimpleNamespace
import shutil
import numpy as np
from collections import namedtuple
from backend.config import tr


def extract_subtitles(data, text_recogniser, img, raw_subtitles,
                      sub_area, options, dt_box_arg, rec_res_arg, ocr_loss_debug_path):
    """
    提取视频帧中的字幕信息
    """
    # 从参数中获取检测框与检测结果
    dt_box = dt_box_arg
    rec_res = rec_res_arg
    # 如果没有检测结果，则获取检测结果
    if dt_box is None or rec_res is None:
        dt_box, rec_res = text_recogniser.predict(img)
        # rec_res格式为： ("hello", 0.997)
    # 获取文本坐标
    coordinates = get_coordinates(dt_box)
    # 将结果写入txt文本中
    if options.REC_CHAR_TYPE == 'en':
        # 如果识别语言为英文，则去除中文
        text_res = [(re.sub('[\u4e00-\u9fa5]', '', res[0]), res[1]) for res in rec_res]
    else:
        text_res = [(res[0], res[1]) for res in rec_res]
    line = ''
    loss_list = []
    for content, coordinate in zip(text_res, coordinates):
        text = content[0]
        prob = content[1]
        if sub_area is not None:
            selected = False
            # 初始化超界偏差为0
            overflow_area_rate = 0
            # 使用AABB矩形重叠判断（比Shapely Polygon快得多）
            c_xmin, c_xmax, c_ymin, c_ymax = coordinate
            # 计算交集矩形
            inter_xmin = max(sub_area.xmin, c_xmin)
            inter_ymin = max(sub_area.ymin, c_ymin)
            inter_xmax = min(sub_area.xmax, c_xmax)
            inter_ymax = min(sub_area.ymax, c_ymax)
            has_intersection = inter_xmin < inter_xmax and inter_ymin < inter_ymax
            drop_reason = ''
            # 如果有交集
            if has_intersection:
                sub_area_w = sub_area.xmax - sub_area.xmin
                sub_area_h = sub_area.ymax - sub_area.ymin
                sub_area_size = sub_area_w * sub_area_h
                inter_area = (inter_xmax - inter_xmin) * (inter_ymax - inter_ymin)
                coord_area = (c_xmax - c_xmin) * (c_ymax - c_ymin)
                # 计算越界允许偏差
                overflow_area_rate = ((sub_area_size + coord_area - inter_area) / sub_area_size) - 1
                # 如果越界比例低于设定阈值且该行文本识别的置信度高于设定阈值
                not_overflow = overflow_area_rate <= options.SUB_AREA_DEVIATION_RATE
                confident = prob > options.DROP_SCORE
                if not_overflow and confident:
                    # 保留该帧
                    selected = True
                    line += f'{str(data["i"]).zfill(8)}\t{coordinate}\t{text}\n'
                    raw_subtitles.append(f'{str(data["i"]).zfill(8)}\t{coordinate}\t{text}\n')
                else:
                    if not not_overflow:
                        drop_reason = tr['Main']['OcrDropOutOfBoxRate'].format(int(options.SUB_AREA_DEVIATION_RATE * 100), int(overflow_area_rate * 100))
                    elif not confident:
                        drop_reason = tr['Main']['OcrDropConfidentLow'].format(int(options.DROP_SCORE * 100))
            else:
                drop_reason = tr['Main']['OcrDropNoIntercetion']
            if drop_reason:
                tqdm.write(tr['Main']['OcrResultWithDropReason'].format(text, round(prob * 100,1), drop_reason))
            else:
                tqdm.write(tr['Main']['OcrResult'].format(text, round(prob * 100,1)))
            # 保存丢掉的识别结果
            loss_info = namedtuple('loss_info', 'text prob overflow_area_rate coordinate selected')
            loss_list.append(loss_info(text, prob, overflow_area_rate, coordinate, selected))
        else:
            raw_subtitles.append(f'{str(data["i"]).zfill(8)}\t{coordinate}\t{text}\n')
    # 输出调试信息
    dump_debug_info(options, line, img, loss_list, ocr_loss_debug_path, sub_area, data)


def dump_debug_info(options, line, img, loss_list, ocr_loss_debug_path, sub_area, data):
    loss = False
    if options.DEBUG_OCR_LOSS and options.REC_CHAR_TYPE in ('ch', 'japan ', 'korea', 'ch_tra'):
        loss = len(line) > 0 and re.search(r'[\u4e00-\u9fa5\u3400-\u4db5\u3130-\u318F\uAC00-\uD7A3\u0800-\u4e00]', line) is None
    if loss:
        if not os.path.exists(ocr_loss_debug_path):
            os.makedirs(ocr_loss_debug_path, mode=0o777, exist_ok=True)
        img = cv2.rectangle(img, (sub_area.xmin, sub_area.ymin), (sub_area.xmax, sub_area.ymax), constant.BGR_COLOR_BLUE, 2)
        for loss_info in loss_list:
            coordinate = loss_info.coordinate
            color = constant.BGR_COLOR_GREEN if loss_info.selected else constant.BGR_COLOR_RED
            text = f"[{loss_info.text}] prob:{loss_info.prob:.4f} or:{loss_info.overflow_area_rate:.2f}"
            img = paint_chinese_opencv(img, text, pos=(coordinate[0], coordinate[2] - 30), color=color)
            img = cv2.rectangle(img, (coordinate[0], coordinate[2]), (coordinate[1], coordinate[3]), color, 2)
        cv2.imwrite(os.path.join(os.path.abspath(ocr_loss_debug_path), f'{str(data["i"]).zfill(8)}.png'), img)


FONT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'NotoSansCJK-Bold.otf')
FONT = ImageFont.truetype(FONT_PATH, 20)


def paint_chinese_opencv(im, chinese, pos, color):
    img_pil = Image.fromarray(im)
    fill_color = color  # (color[2], color[1], color[0])
    position = pos
    draw = ImageDraw.Draw(img_pil)
    draw.text(position, chinese, font=FONT, fill=fill_color)
    img = np.array(img_pil)
    return img


def _queue_get(work_queue, stop_event):
    while not stop_event.is_set():
        try:
            return work_queue.get(timeout=0.2)
        except queue.Empty:
            continue
    return None


def _queue_put(work_queue, value, stop_event):
    while not stop_event.is_set():
        try:
            work_queue.put(value, timeout=0.2)
            return True
        except queue.Full:
            continue
    return False


def ocr_task_consumer(ocr_queue, raw_subtitle_path, sub_area, video_path, options, progress_queue, stop_event=None):
    """Recognize frames; publish raw subtitles only after all tasks succeed."""
    stop_event = stop_event or Event()
    text_recogniser = OcrRecogniser(
        language=options.REC_CHAR_TYPE,
        mode=options.MODE,
        rec_batch_number=options.REC_BATCH_NUMBER,
        hardware_accelerator=options.HARDWARD_ACCELERATOR,
    )
    ocr_loss_debug_path = os.path.join(os.path.abspath(os.path.splitext(video_path)[0]), 'loss')
    if os.path.exists(ocr_loss_debug_path):
        shutil.rmtree(ocr_loss_debug_path, True)
    raw_subtitles = []
    processed_count = 0
    while not stop_event.is_set():
        item = _queue_get(ocr_queue, stop_event)
        if item is None:
            return
        frame_no, frame, dt_box, rec_res = item
        if frame_no == -1:
            temporary_path = raw_subtitle_path + '.partial'
            try:
                with open(temporary_path, 'w', encoding='utf-8') as output:
                    output.writelines(raw_subtitles)
                os.replace(temporary_path, raw_subtitle_path)
            finally:
                if os.path.exists(temporary_path):
                    os.remove(temporary_path)
            progress_queue.put((-1, frame if frame is not None else processed_count))
            return
        extract_subtitles({'i': frame_no}, text_recogniser, frame, raw_subtitles,
                          sub_area, options, dt_box, rec_res, ocr_loss_debug_path)
        processed_count += 1
        progress_queue.put((frame_no, processed_count))


def ocr_task_producer(ocr_queue, task_queue, progress_queue, video_path, raw_subtitle_path, stop_event=None):
    """Decode requested frames, cancelling bounded queue waits after failures."""
    stop_event = stop_event or Event()
    if not os.path.isfile(video_path):
        raise RuntimeError(f'Cannot open video for OCR: {video_path}')
    cap = cv2.VideoCapture(video_path)
    tbar = None
    frame_count = 0
    try:
        if not cap.isOpened():
            raise RuntimeError(f'Cannot open video for OCR: {video_path}')
        while not stop_event.is_set():
            item = _queue_get(task_queue, stop_event)
            if item is None:
                return
            total_frame_count, current_frame_no, dt_box, rec_res, total_ms, default_subtitle_area = item
            if tbar is None:
                tbar = tqdm(total=round(total_frame_count), position=1)
            if current_frame_no == -1:
                if _queue_put(ocr_queue, (-1, frame_count, None, None), stop_event):
                    progress_queue.put((-2, frame_count))
                    tbar.update(tbar.total - tbar.n)
                return
            tbar.update(round(current_frame_no - tbar.n))
            ret, frame = read_frame(cap, video_path,
                                    milliseconds=total_ms,
                                    frame_index=None if total_ms is not None else current_frame_no - 1,
                                    stop_event=stop_event)
            if not ret or frame is None:
                raise RuntimeError(f'Cannot decode OCR frame {current_frame_no} at {total_ms} ms: {video_path}')
            if default_subtitle_area is not None:
                frame = frame_preprocess(default_subtitle_area, frame)
            if not _queue_put(ocr_queue, (current_frame_no, frame, dt_box, rec_res), stop_event):
                return
            frame_count += 1
    finally:
        cap.release()
        if tbar is not None:
            tbar.close()


def subtitle_extract_handler(task_queue, progress_queue, video_path, raw_subtitle_path, sub_area, options):
    """Propagate thread failures as a nonzero OCR process exit, not a fake SRT."""
    if os.path.exists(raw_subtitle_path):
        os.remove(raw_subtitle_path)
    ocr_queue = queue.Queue(20)
    stop_event = Event()
    errors = queue.Queue()

    def guarded(target, *args):
        try:
            target(*args, stop_event=stop_event)
        except BaseException as error:
            errors.put(error)
            stop_event.set()
            progress_queue.put(-1)

    producer = Thread(target=guarded, args=(ocr_task_producer, ocr_queue, task_queue,
                      progress_queue, video_path, raw_subtitle_path), daemon=True)
    consumer = Thread(target=guarded, args=(ocr_task_consumer, ocr_queue, raw_subtitle_path,
                      sub_area, video_path, options, progress_queue), daemon=True)
    producer.start()
    consumer.start()
    producer.join()
    consumer.join()
    if not errors.empty():
        error = errors.get()
        raise RuntimeError(f'OCR worker failed: {error}') from error


def async_start(video_path, raw_subtitle_path, sub_area, options):
    """Snapshot all OCR settings before spawning on Windows/macOS/Linux."""
    required = ('REC_CHAR_TYPE', 'DROP_SCORE', 'SUB_AREA_DEVIATION_RATE',
                'DEBUG_OCR_LOSS', 'HARDWARD_ACCELERATOR')
    missing = [key for key in required if key not in options]
    if missing:
        raise ValueError(f'Missing OCR options: {", ".join(missing)}')
    from backend.config import config
    options = dict(options)
    options.setdefault('MODE', config.mode.value)
    options.setdefault('REC_BATCH_NUMBER', config.recBatchNumber.value)
    task_queue = Queue()
    progress_queue = Queue()
    process = Process(target=subtitle_extract_handler,
                      args=(task_queue, progress_queue, video_path, raw_subtitle_path,
                            sub_area, SimpleNamespace(**options)))
    process.start()
    return process, task_queue, progress_queue


def frame_preprocess(subtitle_area, frame):
    """Mask the unwanted half without changing full-frame OCR coordinates.

    Both explicit GUI/CLI selections and cached detector polygons use the
    original video coordinate system. Cropping off the upper half shifted new
    OCR boxes upward and incorrectly rejected captions against the full ROI.
    """
    if subtitle_area not in (SubtitleArea.LOWER_PART, SubtitleArea.UPPER_PART):
        return frame
    processed = frame.copy()
    midpoint = frame.shape[0] // 2
    if subtitle_area == SubtitleArea.LOWER_PART:
        processed[:midpoint] = 0
    else:
        processed[midpoint:] = 0
    return processed


if __name__ == "__main__":
    pass
