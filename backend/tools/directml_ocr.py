"""Offline ONNX OCR for the restored DirectML package.

The graphs/configs are the same hash-pinned FP32 assets as the native worker.
Only the original onnxruntime-directml 1.20.1 is supported. GPU is the default;
CPU is an explicit user selection, never an error recovery path. No Paddle,
converter, model hub or package installer is imported by this module.

DB/CTC preprocessing follows native/ocr/worker.cpp and PaddleX (Apache-2.0).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import tempfile
import threading

ORT_VERSION = '1.20.1'
MODEL_SOURCE_PINS_SHA256 = 'db66f8583dd143a21a2949b4a4833da41ce98dcfd5e7837df7c8800f8884c015'
DML_PROVIDER = 'DmlExecutionProvider'
CPU_PROVIDER = 'CPUExecutionProvider'


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _asset(root, relative):
    path = PurePosixPath(relative)
    if (not path.parts or path.is_absolute() or '..' in path.parts or '\\' in relative
            or ':' in relative or str(path) != relative):
        raise ValueError(f'Unsafe DirectML model path: {relative}')
    candidate = root / relative
    if (candidate.is_symlink() or not candidate.is_file()
            or not candidate.resolve().is_relative_to(root.resolve())):
        raise ValueError(f'Missing/unsafe DirectML model asset: {relative}')
    return candidate


def validate_model_assets(root, names=None):
    """Authenticate inputs against repository pins, not a self-asserted manifest."""
    root = Path(root).resolve(strict=True)
    pins_file = _asset(root, 'model-source-pins.json')
    pins_bytes = pins_file.read_bytes()
    if hashlib.sha256(pins_bytes).hexdigest() != MODEL_SOURCE_PINS_SHA256:
        raise ValueError('DirectML model source pins differ from the reviewed 15-model set')
    pins = json.loads(pins_bytes)
    manifest_bytes = _asset(root, 'model-manifest.json').read_bytes()
    manifest = json.loads(manifest_bytes)
    if (not isinstance(manifest, list) or len(manifest) != 15
            or any(not isinstance(row, dict) or not isinstance(row.get('name'), str) for row in manifest)):
        raise ValueError('DirectML requires all 15 distinct model manifest entries')
    entries = {row['name']: row for row in manifest}
    expected = {row['name'] + '_infer': row for row in pins['models']}
    if len(entries) != 15 or set(entries) != set(expected):
        raise ValueError('DirectML manifest has duplicate or substituted models')
    selected = set(names) if names is not None else {row['name'] for row in pins['models']}
    if selected - {row['name'] for row in pins['models']}:
        raise ValueError('Unvalidated DirectML model requested; language substitution refused')
    files = {'model-source-pins.json': pins_file, 'model-manifest.json': root / 'model-manifest.json'}
    hashes = {'model-source-pins.json': MODEL_SOURCE_PINS_SHA256,
              'model-manifest.json': hashlib.sha256(manifest_bytes).hexdigest()}
    configs = {}
    for name in sorted(selected):
        row, entry = expected[name + '_infer'], entries[name + '_infer']
        relative = f'models/{name}_infer.onnx'
        config = f'config/{name.split("_")[1]}.json' if name.startswith('PP-OCRv6_') else f'config/{name}.json'
        path, config_path = _asset(root, relative), _asset(root, config)
        if (entry.get('onnx_sha256') != row['onnx_sha256'] or entry.get('onnx_bytes') != row['onnx_bytes']
                or path.stat().st_size != row['onnx_bytes'] or _sha(path) != row['onnx_sha256']):
            raise ValueError(f'DirectML model checksum mismatch: {name}')
        config_hash = pins['config_sha256'][config]
        config_bytes = config_path.read_bytes()
        if (entry.get('config_file') != config or entry.get('config_sha256') != config_hash
                or hashlib.sha256(config_bytes).hexdigest() != config_hash):
            raise ValueError(f'DirectML config checksum mismatch: {name}')
        data = json.loads(config_bytes)
        if name.startswith('PP-OCRv6_'):
            data = data[name.rsplit('_', 1)[1]]
        if data.get('Global', {}).get('model_name') != name:
            raise ValueError(f'DirectML model identity mismatch: {name}')
        configs[name] = data
        files[relative], files[config] = path, config_path
        hashes[relative], hashes[config] = row['onnx_sha256'], config_hash
    return {'files': files, 'hashes': hashes, 'configs': configs, 'pins': pins}


class OnnxSession:
    """One sequential ORT session, with checked provider and execution evidence."""
    def __init__(self, path, provider):
        import onnxruntime as ort
        if hasattr(ort, 'disable_telemetry_events'):
            ort.disable_telemetry_events()
        native_version = ort.get_version_string()
        if ort.__version__ != ORT_VERSION or native_version != ORT_VERSION:
            raise RuntimeError(f'DirectML profile requires ONNX Runtime {ORT_VERSION}; got Python {ort.__version__}, native {native_version}')
        if provider not in ('cpu', 'directml'):
            raise ValueError('OCR provider must be explicitly cpu or directml')
        self.provider = provider
        requested = DML_PROVIDER if provider == 'directml' else CPU_PROVIDER
        if requested not in ort.get_available_providers():
            raise RuntimeError(f'{requested} unavailable; refusing silent CPU fallback. Use the explicit CPU option if desired.')
        options = ort.SessionOptions()
        options.enable_mem_pattern = False
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.intra_op_num_threads = int(os.environ.get('VSE_OCR_CPU_THREADS', min(4, os.cpu_count() or 1)))
        if not 1 <= options.intra_op_num_threads <= 128:
            raise ValueError('VSE_OCR_CPU_THREADS must be in 1..128')
        device = int(os.environ.get('VSE_DIRECTML_DEVICE_ID', '0')) if provider == 'directml' else 0
        if device < 0:
            raise ValueError('VSE_DIRECTML_DEVICE_ID must be non-negative')
        self._profile = tempfile.TemporaryDirectory(prefix='vse-directml-profile-') if provider == 'directml' else None
        self.evidence = {'requested_provider': requested, 'verified': provider == 'cpu', 'cpu_explicit': provider == 'cpu'}
        if self._profile:
            options.enable_profiling = True
            options.profile_file_prefix = str(Path(self._profile.name) / 'execution')
        providers = [(DML_PROVIDER, {'device_id': str(device)})] if provider == 'directml' else [CPU_PROVIDER]
        try:
            self.session = ort.InferenceSession(str(path), sess_options=options, providers=providers)
            # ORT's constructor may internally retry with CPU after EP init fails.
            # Check the constructed session, then disable its run-time retry too.
            self.session.disable_fallback()
            if requested not in self.session.get_providers():
                raise RuntimeError(f'{requested} initialization failed; CPU fallback was refused')
            self.input_name = self.session.get_inputs()[0].name
        except BaseException:
            self.close()
            raise

    def run(self, value):
        if self.session is None:
            raise RuntimeError('DirectML OCR session is closed')
        try:
            result = self.session.run(None, {self.input_name: value})[0]
            if self.provider == 'directml' and not self.evidence['verified']:
                profile = Path(self.session.end_profiling())
                if not profile.resolve().is_relative_to(Path(self._profile.name).resolve()):
                    raise RuntimeError('Unexpected ONNX Runtime profiling path')
                events = json.loads(profile.read_text(encoding='utf-8'))
                counts = {}
                for event in events:
                    provider = event.get('args', {}).get('provider')
                    if event.get('cat') == 'Node' and provider:
                        counts[provider] = counts.get(provider, 0) + 1
                if not counts.get(DML_PROVIDER):
                    raise RuntimeError('No DirectML kernel executed; refusing all-CPU inference labeled as GPU')
                # DML may intentionally execute shape/helper nodes on CPU. Keep
                # their counts visible; provider registration alone proves nothing.
                self.evidence.update(verified=True, executed_nodes=counts)
                self._profile.cleanup()
                self._profile = None
            return result
        except BaseException:
            self.close()
            raise

    def close(self):
        self.session = None
        if getattr(self, '_profile', None) is not None:
            self._profile.cleanup()
            self._profile = None


def _mini(contour):
    import cv2
    import numpy as np
    rectangle = cv2.minAreaRect(np.asarray(contour, dtype=np.float32))
    points = sorted(cv2.boxPoints(rectangle), key=lambda p: p[0])
    left = sorted(points[:2], key=lambda p: p[1])
    right = sorted(points[2:], key=lambda p: p[1])
    return np.asarray([left[0], right[0], right[1], left[1]], dtype=np.float32), min(rectangle[1])


def _boxes(prediction, width, height, *, threshold, box_threshold, unclip, max_candidates, ordered):
    import cv2
    import numpy as np
    import pyclipper
    if prediction.ndim != 4 or prediction.shape[:2] != (1, 1) or not np.isfinite(prediction).all():
        raise ValueError('Invalid DirectML detector output shape')
    pred = prediction[0, 0]
    h, w = pred.shape
    contours, _ = cv2.findContours((pred > threshold).astype(np.uint8) * 255, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    for contour in contours[:max_candidates]:
        box, side = _mini(contour)
        if side < 3:
            continue
        x0, y0 = np.maximum(np.floor(box.min(axis=0)).astype(int), 0)
        x1, y1 = np.minimum(np.ceil(box.max(axis=0)).astype(int), [w - 1, h - 1])
        mask = np.zeros((y1 - y0 + 1, x1 - x0 + 1), dtype=np.uint8)
        cv2.fillPoly(mask, [(box - [x0, y0]).astype(np.int32)], 1)
        if cv2.mean(pred[y0:y1 + 1, x0:x1 + 1], mask)[0] < box_threshold:
            continue
        length = cv2.arcLength(box, True)
        if length <= 0:
            continue
        offset = pyclipper.PyclipperOffset()
        offset.AddPath(box.astype(np.int64).tolist(), pyclipper.JT_ROUND, pyclipper.ET_CLOSEDPOLYGON)
        expanded = offset.Execute(cv2.contourArea(box) * unclip / length)
        if not expanded:
            continue
        box, side = _mini(np.concatenate(expanded))
        if side < 5:
            continue
        box[:, 0] = np.clip(np.rint(box[:, 0].astype(np.float64) * width / w), 0, width)
        box[:, 1] = np.clip(np.rint(box[:, 1].astype(np.float64) * height / h), 0, height)
        boxes.append(box)
    if ordered:
        boxes.sort(key=lambda box: (box[0, 1], box[0, 0]))
        for i in range(len(boxes) - 1):
            for j in range(i, -1, -1):
                if abs(boxes[j + 1][0, 1] - boxes[j][0, 1]) < 10 and boxes[j + 1][0, 0] < boxes[j][0, 0]:
                    boxes[j], boxes[j + 1] = boxes[j + 1], boxes[j]
                else:
                    break
    return boxes


def _crop(image, box):
    import cv2
    import numpy as np
    box, _ = _mini(box.astype(np.int32))
    width = int(max(np.linalg.norm(box[0] - box[1]), np.linalg.norm(box[2] - box[3])))
    height = int(max(np.linalg.norm(box[0] - box[3]), np.linalg.norm(box[1] - box[2])))
    if not width or not height:
        return None
    target = np.float32([[0, 0], [width, 0], [width, height], [0, height]])
    crop = cv2.warpPerspective(image, cv2.getPerspectiveTransform(box, target), (width, height),
                               flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    return np.rot90(crop) if height / width >= 1.5 else crop


class DirectMlOcr:
    def __init__(self, model_config, batch_number=6, *, stop_event=None, root=None, detection_only=False, provider=None):
        self.provider = provider or ('directml' if model_config.hardware_accelerator.is_enabled else 'cpu')
        if self.provider not in ('cpu', 'directml'):
            raise ValueError('DirectML OCR provider must be cpu or directml')
        self.batch_number = int(batch_number)
        if not 1 <= self.batch_number <= 100:
            raise ValueError('DirectML OCR batch size must be in 1..100')
        value = root or os.environ.get('VSE_DIRECTML_MODEL_ROOT')
        if not value:
            raise ValueError('DirectML OCR requires VSE_DIRECTML_MODEL_ROOT; no runtime models are downloaded')
        self.root = Path(value).resolve(strict=True)
        self.det_name, self.rec_name = model_config.DET_MODEL_NAME, model_config.REC_MODEL_NAME
        self.detection_only, self.stop_event = detection_only, stop_event
        self._lock, self._closed = threading.Lock(), False
        self.det_session = self.rec_session = None
        names = [self.det_name] + ([] if detection_only else [self.rec_name])
        verified = validate_model_assets(self.root, names)
        self.det_config = verified['configs'][self.det_name]
        self.rec_config = None if detection_only else verified['configs'][self.rec_name]

    def _check_open(self):
        if self._closed or (self.stop_event is not None and self.stop_event.is_set()):
            raise RuntimeError('DirectML OCR cancelled or closed')

    def _session(self, role):
        attribute = role + '_session'
        if getattr(self, attribute) is None:
            setattr(self, attribute, OnnxSession(self.root / 'models' / f'{getattr(self, role + "_name")}_infer.onnx', self.provider))
        return getattr(self, attribute)

    @property
    def execution_evidence(self):
        return {role: session.evidence for role in ('det', 'rec')
                if (session := getattr(self, role + '_session')) is not None}

    def _detect(self, image):
        import cv2
        import numpy as np
        original_height, original_width = image.shape[:2]
        if original_height + original_width < 64:
            padded = np.zeros((max(32, original_height), max(32, original_width), 3), dtype=np.uint8)
            padded[:original_height, :original_width] = image
            image = padded
        height, width = image.shape[:2]
        limit, maximum = 64, False
        threshold, box_threshold, unclip = .3, .6, 1.5
        operations = self.det_config['PreProcess']['transform_ops']
        if self.detection_only:
            limit, maximum = 960, True
            for op in operations:
                if isinstance(op.get('DetResizeForTest'), dict):
                    resize = op['DetResizeForTest']
                    limit = resize.get('resize_long', 960)
                    if resize.get('limit_type', 'max') not in ('min', 'max'):
                        raise ValueError('Unsupported detector resize profile')
                    maximum = resize.get('limit_type', 'max') == 'max'
            post = self.det_config['PostProcess']
            threshold, box_threshold, unclip = post.get('thresh', .3), post.get('box_thresh', .6), post.get('unclip_ratio', 2.)
        ratio = min(1., limit / max(height, width)) if maximum else max(1., limit / min(height, width))
        h, w = int(height * ratio), int(width * ratio)
        if max(h, w) > 4000:
            ratio = 4000 / max(h, w)
            h, w = int(h * ratio), int(w * ratio)
        h, w = max(32, round(h / 32) * 32), max(32, round(w / 32) * 32)
        resized = cv2.resize(image, (w, h)).astype(np.float32)
        normalization = next(op['NormalizeImage'] for op in operations if 'NormalizeImage' in op)
        alpha = np.asarray([(1 / 255) / sd for sd in normalization['std']], dtype=np.float32)
        beta = np.asarray([-mean / sd for mean, sd in zip(normalization['mean'], normalization['std'])], dtype=np.float32)
        tensor = np.ascontiguousarray((resized * alpha + beta).transpose(2, 0, 1)[None])
        prediction = self._session('det').run(tensor)
        return _boxes(prediction, original_width, original_height, threshold=threshold, box_threshold=box_threshold,
                      unclip=unclip, max_candidates=self.det_config['PostProcess'].get('max_candidates', 1000), ordered=not self.detection_only)

    def _recognize(self, image, boxes):
        import cv2
        import numpy as np
        entries = [(box, crop) for box in boxes if (crop := _crop(image, box)) is not None]
        records = [('', 0.) for _ in entries]
        order = sorted(range(len(entries)), key=lambda i: entries[i][1].shape[1] / entries[i][1].shape[0])
        operations = self.rec_config['PreProcess']['transform_ops']
        shape = next(op['RecResizeImg']['image_shape'] for op in operations if 'RecResizeImg' in op)
        _, rec_h, rec_w = shape
        chars = ['blank'] + self.rec_config['PostProcess']['character_dict'] + [' ']
        for start in range(0, len(order), self.batch_number):
            self._check_open()
            ids = order[start:start + self.batch_number]
            widths = [min(3200, int(rec_h * max(rec_w / rec_h, entries[i][1].shape[1] / entries[i][1].shape[0]))) for i in ids]
            width = max(widths)
            if len(ids) * (width // 4 + 8) * len(chars) > 100000000 or len(ids) * 3 * rec_h * width > 64000000:
                raise ValueError('DirectML OCR recognition batch exceeds memory limit')
            tensor = np.zeros((len(ids), 3, rec_h, width), dtype=np.float32)
            for b, i in enumerate(ids):
                crop = entries[i][1]
                ratio = crop.shape[1] / crop.shape[0]
                resized_width = 3200 if rec_h * max(rec_w / rec_h, ratio) > 3200 else min(widths[b], math.ceil(rec_h * ratio))
                resized = cv2.resize(crop, (resized_width, rec_h)).astype(np.float32)
                tensor[b, :, :, :resized_width] = ((resized / np.float32(255) - np.float32(.5)) / np.float32(.5)).transpose(2, 0, 1)
            output = self._session('rec').run(tensor)
            if output.ndim != 3 or output.shape[0] != len(ids) or output.shape[2] != len(chars) or not np.isfinite(output).all():
                raise ValueError('Invalid DirectML recognizer shape/dictionary')
            for b, i in enumerate(ids):
                indices = output[b].argmax(axis=1)
                keep = np.r_[True, indices[1:] != indices[:-1]] & (indices != 0)
                text = ''.join(chars[index] for index in indices[keep])
                score = float(output[b].max(axis=1)[keep].mean()) if keep.any() else 0.
                if self.rec_name.startswith('arabic_'):
                    from bidi.algorithm import get_display
                    text = get_display(text)
                records[i] = (text, score)
        return {'rec_polys': [box.tolist() for box, crop in entries],
                'rec_texts': [text for text, score in records], 'rec_scores': [score for text, score in records]}

    def _predict(self, image):
        import cv2
        import numpy as np
        with self._lock:
            self._check_open()
            if not isinstance(image, np.ndarray) or image.dtype != np.uint8 or image.ndim not in (2, 3):
                raise ValueError('DirectML OCR expects uint8 BGR, BGRA or grayscale input')
            h, w = image.shape[:2]
            if not h or not w or max(h, w) > 16384 or h * w > 16000000 or (image.ndim == 3 and image.shape[2] not in (3, 4)):
                raise ValueError('DirectML OCR image dimensions exceed supported limits')
            if image.ndim == 2:
                image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
            elif image.shape[2] == 4:
                image = cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
            boxes = self._detect(image)
            self._check_open()
            return {'dt_polys': [box.tolist() for box in boxes]} if self.detection_only else self._recognize(image, boxes)

    def predict_iter(self, image):
        if self.detection_only:
            raise RuntimeError('Detection-only DirectML OCR cannot perform recognition')
        yield self._predict(image)

    def predict(self, image):
        yield self._predict(image)

    def close(self):
        self._closed = True
        for role in ('det', 'rec'):
            session = getattr(self, role + '_session')
            if session is not None:
                session.close()
