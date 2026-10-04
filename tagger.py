import os
import csv
import logging
import requests
import numpy as np
from PIL import Image, ImageOps
import onnxruntime as ort
import onnx  # type: ignore[import-untyped]
from onnxconverter_common import float16  # type: ignore[import-untyped]

from config import (
    AI_MODELS_DIR,
    DEFAULT_TAGGER_MODEL,
    DEFAULT_TAGGER_MODEL_FP16,
    DEFAULT_TAGS_CSV,
    DEFAULT_CHARACTER_THRESHOLD,
    DEFAULT_GENERAL_THRESHOLD,
    AVAILABLE_AI_MODELS,
    DEFAULT_MODEL_KEY,
    HF_MODEL_URL,
    HF_TAGS_URL
)
from utils import load_media_thumbnail

def get_model_paths(model_key=DEFAULT_MODEL_KEY):
    """
    Returns a dictionary of filesystem paths and metadata for a specific model key.
    """
    if model_key not in AVAILABLE_AI_MODELS:
        model_key = DEFAULT_MODEL_KEY
    meta = AVAILABLE_AI_MODELS[model_key]

    if meta.get("is_ensemble"):
        ensemble_keys = meta.get("ensemble_keys", [])
        first_sub = get_model_paths(ensemble_keys[0]) if ensemble_keys else None
        return {
            "key": model_key,
            "dir": os.path.join(AI_MODELS_DIR, meta.get("subdir", "wd_v3_ensemble")),
            "model_fp32": "",
            "model_fp16": "",
            "tags_csv": first_sub["tags_csv"] if first_sub else os.path.join(AI_MODELS_DIR, "selected_tags.csv"),
            "meta": meta
        }

    subdir = meta.get("subdir", "")
    if subdir:
        model_dir = os.path.join(AI_MODELS_DIR, subdir)
    else:
        model_dir = AI_MODELS_DIR

    model_fp32 = os.path.join(model_dir, "model.onnx")
    model_fp16 = os.path.join(model_dir, "model_fp16.onnx")
    tags_csv = os.path.join(model_dir, "selected_tags.csv")
    return {
        "key": model_key,
        "dir": model_dir,
        "model_fp32": model_fp32,
        "model_fp16": model_fp16,
        "tags_csv": tags_csv,
        "meta": meta
    }

class WD14Tagger:
    """
    Local AI Tagger supporting WD14 Tagger (v1.4), WD Tagger v3 Series, and Ensemble 3-in-1 in ONNX format.
    Supports GPU acceleration via DirectML on Windows, FP16 Half-Precision (AMD RDNA / NVIDIA), and CPU fallback.
    """
    def __init__(self, model_key=DEFAULT_MODEL_KEY, model_path=None, tags_path=None, use_gpu=True, use_fp16=False):
        self.model_key = model_key or DEFAULT_MODEL_KEY
        self.use_fp16 = use_fp16
        self.use_gpu = use_gpu
        paths = get_model_paths(self.model_key)
        self.meta = paths["meta"]
        self.is_ensemble = self.meta.get("is_ensemble", False)
        self.ensemble_keys = self.meta.get("ensemble_keys", [])
        self.sub_taggers = []

        if not self.is_ensemble:
            if model_path is None:
                self.model_path = paths["model_fp16"] if self.use_fp16 else paths["model_fp32"]
            else:
                self.model_path = model_path

            if tags_path is None:
                self.tags_path = paths["tags_csv"]
            else:
                self.tags_path = tags_path
        else:
            self.model_path = ""
            self.tags_path = paths["tags_csv"]

        self.session = None
        self.tags_data = [] # List of dict: {id, name, category, count}
        self.char_indices = []
        self.series_indices = []
        self.general_indices = []
        self.rating_indices = []
        self.input_name = None
        self.input_shape = [1, 448, 448, 3] # Default shape [1, 448, 448, 3]
        self.input_dtype = np.float16 if self.use_fp16 else np.float32
        self.output_name = None
        self.active_provider = None

    @staticmethod
    def get_available_providers():
        """Returns available ONNX execution providers and checks if DirectML is supported."""
        all_providers = ort.get_available_providers()
        has_directml = 'DmlExecutionProvider' in all_providers
        has_cuda = 'CUDAExecutionProvider' in all_providers
        return {
            "all_providers": all_providers,
            "has_directml": has_directml,
            "has_cuda": has_cuda,
            "has_gpu": has_directml or has_cuda
        }

    @classmethod
    def is_model_installed(cls, model_key=DEFAULT_MODEL_KEY, model_path=None, tags_path=None):
        """Checks whether the ONNX model (FP32 or FP16) and tags CSV exist and have non-zero size."""
        if model_path is not None and tags_path is not None:
            return (
                os.path.exists(model_path) and os.path.getsize(model_path) > 1024 * 1024 and
                os.path.exists(tags_path) and os.path.getsize(tags_path) > 1024
            )

        paths = get_model_paths(model_key or DEFAULT_MODEL_KEY)
        meta = paths["meta"]
        if meta.get("is_ensemble"):
            ensemble_keys = meta.get("ensemble_keys", [])
            return len(ensemble_keys) > 0 and all(cls.is_model_installed(k) for k in ensemble_keys)

        model_installed = (
            (os.path.exists(paths["model_fp32"]) and os.path.getsize(paths["model_fp32"]) > 1024 * 1024) or
            (os.path.exists(paths["model_fp16"]) and os.path.getsize(paths["model_fp16"]) > 1024 * 1024)
        )
        tags_installed = os.path.exists(paths["tags_csv"]) and os.path.getsize(paths["tags_csv"]) > 1024
        return model_installed and tags_installed

    @staticmethod
    def convert_to_fp16(src_path, dst_path):
        """Converts FP32 ONNX model to FP16 half-precision with dynamic batch size for GPU acceleration."""
        logging.info(f"Converting {src_path} to FP16 half-precision -> {dst_path}...")
        try:
            model = onnx.load(src_path)
            try:
                # Ensure dynamic batch size is set on input & output
                inp = model.graph.input[0]
                inp.type.tensor_type.shape.dim[0].dim_value = 0
                inp.type.tensor_type.shape.dim[0].dim_param = 'batch_size'
                out = model.graph.output[0]
                out.type.tensor_type.shape.dim[0].dim_value = 0
                out.type.tensor_type.shape.dim[0].dim_param = 'batch_size'
                onnx.save(model, src_path)
            except Exception as e:
                logging.debug(f"Dynamic batch patch on source: {e}")

            model_fp16 = float16.convert_float_to_float16(
                model,
                keep_io_types=False,
                op_block_list=['ReduceSumSquare', 'Sqrt', 'Div', 'ReduceMean', 'GlobalAveragePool']
            )
            onnx.save(model_fp16, dst_path)

            # Verify the converted model can actually be loaded by ONNXRuntime
            sess_test = ort.InferenceSession(dst_path, providers=['CPUExecutionProvider'])
            del sess_test
            logging.info(f"Saved and verified FP16 model successfully: {dst_path}")
            return dst_path
        except Exception as e:
            logging.warning(f"FP16 conversion not compatible with this model architecture ({e}). Keeping FP32 model.")
            if os.path.exists(dst_path):
                try: os.remove(dst_path)
                except Exception: pass
            return None

    @classmethod
    def check_for_updates(cls, model_key=DEFAULT_MODEL_KEY):
        """
        Checks Hugging Face remote headers (Content-Length, ETag) to detect model updates.
        Returns (has_update: bool, remote_size: int, local_size: int)
        """
        paths = get_model_paths(model_key)
        meta = paths["meta"]
        if meta.get("is_ensemble"):
            for k in meta.get("ensemble_keys", []):
                has_up, rem, loc = cls.check_for_updates(k)
                if has_up:
                    return True, rem, loc
            return False, 0, 0

        model_url = meta.get("model_url")
        if not model_url:
            return False, 0, 0
        local_model_path = paths["model_fp32"]
        if not os.path.exists(local_model_path):
            return True, 0, 0
        try:
            resp = requests.head(model_url, timeout=10, allow_redirects=True)
            if resp.status_code == 200:
                remote_size = int(resp.headers.get('content-length', 0))
                local_size = os.path.getsize(local_model_path)
                has_update = remote_size > 0 and abs(remote_size - local_size) > 1024
                return has_update, remote_size, local_size
        except Exception as e:
            logging.warning(f"Failed to check model updates from Hugging Face: {e}")
        return False, 0, 0

    @classmethod
    def download_models(cls, model_key=DEFAULT_MODEL_KEY, progress_callback=None, cancel_check=None):
        """
        Downloads ONNX model and tags CSV from Hugging Face repository for the chosen model_key.
        Automatically applies dynamic batch size patch and creates FP16 version.
        Supports ensemble models by sequentially downloading missing sub-models.
        """
        paths = get_model_paths(model_key)
        meta = paths["meta"]

        if meta.get("is_ensemble"):
            ensemble_keys = meta.get("ensemble_keys", [])
            for idx, sub_key in enumerate(ensemble_keys):
                sub_meta = AVAILABLE_AI_MODELS[sub_key]
                if not cls.is_model_installed(sub_key):
                    def sub_cb(cur, tot, msg, i=idx, n=len(ensemble_keys), sm=sub_meta):
                        if progress_callback:
                            progress_callback(cur, tot, f"[{i+1}/{n}] {msg}")
                    cls.download_models(sub_key, progress_callback=sub_cb, cancel_check=cancel_check)
            return True

        target_dir = paths["dir"]
        os.makedirs(target_dir, exist_ok=True)

        files = [
            ("selected_tags.csv", meta["tags_url"], paths["tags_csv"]),
            ("model.onnx", meta["model_url"], paths["model_fp32"])
        ]

        for filename, url, dest_path in files:
            temp_path = dest_path + ".download"

            if os.path.exists(dest_path) and os.path.getsize(dest_path) > 1024 * 1024:
                continue

            resp = requests.get(url, stream=True, timeout=30)
            resp.raise_for_status()
            total_size = int(resp.headers.get('content-length', 0))
            downloaded = 0

            with open(temp_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if cancel_check and cancel_check():
                        f.close()
                        if os.path.exists(temp_path):
                            os.remove(temp_path)
                        raise InterruptedError("Download cancelled by user.")
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress_callback:
                            progress_callback(downloaded, total_size, f"กำลังดาวน์โหลด {meta['short_name']} ({filename})...")

            if os.path.exists(dest_path):
                os.remove(dest_path)
            os.rename(temp_path, dest_path)

        # Patch FP32 dynamic batch & convert to FP16
        try:
            if os.path.exists(paths["model_fp32"]):
                if progress_callback:
                    progress_callback(100, 100, f"กำลังแปลง {meta['short_name']} เป็น FP16 DirectML...")
                cls.convert_to_fp16(paths["model_fp32"], paths["model_fp16"])
        except Exception as e:
            logging.warning(f"Auto FP16 conversion failed: {e}")

        return True

    def load_tags(self):
        """Loads and parses selected_tags.csv."""
        if not os.path.exists(self.tags_path):
            raise FileNotFoundError(f"ไม่พบไฟล์แท็ก: {self.tags_path}")

        self.tags_data = []
        self.char_indices = []
        self.series_indices = []
        self.general_indices = []
        self.rating_indices = []

        with open(self.tags_path, mode='r', encoding='utf-8') as f:
            reader = csv.reader(f)
            header = next(reader, None)
            for idx, row in enumerate(reader):
                if not row or len(row) < 3: continue
                tag_id = int(row[0]) if row[0].isdigit() else idx
                name = row[1]
                category = int(row[2]) if row[2].isdigit() else 0
                count = int(row[3]) if len(row) > 3 and row[3].isdigit() else 0

                tag_item = {
                    "index": idx,
                    "id": tag_id,
                    "name": name,
                    "category": category, # 0: General, 3: Copyright/Series, 4: Character, 5: Rating
                    "count": count
                }
                self.tags_data.append(tag_item)

                if category == 4:
                    self.char_indices.append(idx)
                elif category == 3:
                    self.series_indices.append(idx)
                elif category == 0:
                    self.general_indices.append(idx)
                elif category in (5, 9) or name in ('general', 'sensitive', 'questionable', 'explicit'):
                    self.rating_indices.append(idx)

        logging.info(f"Loaded {len(self.tags_data)} tags ({len(self.char_indices)} characters, {len(self.series_indices)} series).")

    def load_model(self):
        """Initializes ONNX Runtime InferenceSession with DirectML GPU or CPU."""
        if self.is_ensemble:
            self.sub_taggers = []
            for k in self.ensemble_keys:
                sub = WD14Tagger(model_key=k, use_gpu=self.use_gpu, use_fp16=self.use_fp16)
                sub.load_model()
                self.sub_taggers.append(sub)
            self.tags_data = self.sub_taggers[0].tags_data
            self.char_indices = self.sub_taggers[0].char_indices
            self.series_indices = self.sub_taggers[0].series_indices
            self.general_indices = self.sub_taggers[0].general_indices
            self.rating_indices = self.sub_taggers[0].rating_indices
            self.input_dtype = self.sub_taggers[0].input_dtype
            self.input_shape = self.sub_taggers[0].input_shape
            provider_type = self.sub_taggers[0].active_provider
            self.active_provider = f"Ensemble ({len(self.sub_taggers)} models on {provider_type})"
            logging.info(f"Initialized Ensemble WD14Tagger with {len(self.sub_taggers)} sub-models on {self.active_provider}.")
            return

        paths = get_model_paths(self.model_key)
        self.load_tags()

        providers = []
        if self.use_gpu:
            available = ort.get_available_providers()
            if 'DmlExecutionProvider' in available:
                providers.append('DmlExecutionProvider')
            elif 'CUDAExecutionProvider' in available:
                providers.append('CUDAExecutionProvider')
        
        providers.append('CPUExecutionProvider')

        sess_options = ort.SessionOptions()
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        sess_options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        sess_options.enable_mem_pattern = True
        sess_options.enable_cpu_mem_arena = True
        sess_options.log_severity_level = 3

        # 1. Try loading FP16 model first if requested
        if self.use_fp16 and paths["model_fp16"] and os.path.exists(paths["model_fp16"]):
            try:
                self.session = ort.InferenceSession(paths["model_fp16"], sess_options=sess_options, providers=providers)
                self.active_provider = self.session.get_providers()[0]
                inputs = self.session.get_inputs()
                self.input_name = inputs[0].name
                self.input_shape = inputs[0].shape
                self.input_dtype = np.float16 if "float16" in inputs[0].type else np.float32
                self.output_name = self.session.get_outputs()[0].name
                logging.info(f"Initialized WD14Tagger on {self.active_provider} (FP16). Input: {self.input_name} {self.input_shape} ({self.input_dtype})")
                return
            except Exception as e:
                logging.warning(f"FP16 load failed for {self.model_key} ({e}), falling back to FP32 DirectML...")

        # 2. Fallback to FP32 model
        if not os.path.exists(paths["model_fp32"]):
            raise FileNotFoundError(f"ไม่พบไฟล์โมเดล AI: {paths['model_fp32']}")

        self.session = ort.InferenceSession(paths["model_fp32"], sess_options=sess_options, providers=providers)
        self.active_provider = self.session.get_providers()[0]
        inputs = self.session.get_inputs()
        self.input_name = inputs[0].name
        self.input_shape = inputs[0].shape
        self.input_dtype = np.float16 if "float16" in inputs[0].type else np.float32
        self.output_name = self.session.get_outputs()[0].name
        logging.info(f"Initialized WD14Tagger on {self.active_provider} (FP32). Input: {self.input_name} {self.input_shape} ({self.input_dtype})")

    def preprocess_image(self, pil_image, target_size=448):
        """
        Preprocesses PIL Image for WD14 Tagger:
        - Alpha composition with white background for transparent PNGs
        - Fast Bilinear resize with aspect ratio preserved and padded to target_size (square)
        - Format: BGR float32 or float16 array normalized according to ConvNeXt specs
        """
        if pil_image.mode in ('RGBA', 'LA') or (pil_image.mode == 'P' and hasattr(pil_image, 'info') and 'transparency' in pil_image.info):
            canvas = Image.new('RGB', pil_image.size, (255, 255, 255))
            pil_image = pil_image.convert('RGBA')
            canvas.paste(pil_image, mask=pil_image.split()[3])
            pil_image = canvas
        else:
            pil_image = pil_image.convert('RGB')

        w, h = pil_image.size
        scale = target_size / max(w, h)
        new_w, new_h = max(1, int(w * scale)), max(1, int(h * scale))
        resized = pil_image.resize((new_w, new_h), Image.Resampling.BILINEAR)

        padded = Image.new('RGB', (target_size, target_size), (255, 255, 255))
        paste_x = (target_size - new_w) // 2
        paste_y = (target_size - new_h) // 2
        padded.paste(resized, (paste_x, paste_y))

        img_array = np.asarray(padded, dtype=self.input_dtype)
        img_array = img_array[:, :, ::-1] # RGB to BGR

        if len(self.input_shape) == 4 and self.input_shape[1] == 3: # NCHW
            img_array = np.transpose(img_array, (2, 0, 1))
        
        img_array = np.expand_dims(img_array, axis=0)
        return img_array

    def prepare_media_tensor(self, media_path):
        """
        Loads and pre-processes a media file into a GPU-ready float32/float16 tensor.
        Ideal for multi-threaded async prefetching pipeline.
        """
        if self.is_ensemble:
            if self.sub_taggers:
                return self.sub_taggers[0].prepare_media_tensor(media_path)
            first_sub = WD14Tagger(self.ensemble_keys[0], use_gpu=self.use_gpu, use_fp16=self.use_fp16)
            return first_sub.prepare_media_tensor(media_path)

        try:
            pil_img, _ = load_media_thumbnail(media_path)
            if pil_img is None or pil_img.size == (0, 0):
                return None

            target_size = 448
            if len(self.input_shape) == 4:
                if isinstance(self.input_shape[1], int) and self.input_shape[1] > 3:
                    target_size = self.input_shape[1]
                elif isinstance(self.input_shape[2], int) and self.input_shape[2] > 3:
                    target_size = self.input_shape[2]

            return self.preprocess_image(pil_img, target_size=target_size)
        except Exception as e:
            logging.debug(f"Error preparing tensor for {media_path}: {e}")
            return None

    def predict_tensor(self, input_data, char_threshold=DEFAULT_CHARACTER_THRESHOLD,
                       gen_threshold=DEFAULT_GENERAL_THRESHOLD):
        """
        Executes GPU inference directly on preprocessed float32/float16 input tensor.
        Vectorized tag extraction for near-zero CPU overhead.
        Supports single model and Ensemble (Soft-Voting probability average).
        """
        if self.is_ensemble:
            if not self.sub_taggers:
                self.load_model()
            if input_data is None:
                return None

            all_probs = []
            for sub in self.sub_taggers:
                t = input_data.astype(sub.input_dtype) if input_data.dtype != sub.input_dtype else input_data
                outputs = sub.session.run([sub.output_name], {sub.input_name: t})
                sub_probs = outputs[0][0].astype(np.float32)
                if np.max(sub_probs) > 1.0 or np.min(sub_probs) < 0.0:
                    sub_probs = 1.0 / (1.0 + np.exp(-sub_probs))
                all_probs.append(sub_probs)
            probs = np.mean(all_probs, axis=0)
        else:
            if self.session is None:
                self.load_model()

            if input_data is None:
                return None

            # Auto cast dtype if needed
            if input_data.dtype != self.input_dtype:
                input_data = input_data.astype(self.input_dtype)

            # Run ONNX inference
            outputs = self.session.run([self.output_name], {self.input_name: input_data})
            probs = outputs[0][0].astype(np.float32)

            # Apply sigmoid if raw logits were returned
            if np.max(probs) > 1.0 or np.min(probs) < 0.0:
                probs = 1.0 / (1.0 + np.exp(-probs))

        characters = []
        series = []
        general = []
        all_tags = []

        min_thresh = min(char_threshold, gen_threshold, 0.20)
        passing_indices = np.flatnonzero(probs >= min_thresh)

        for idx in passing_indices:
            tag_info = self.tags_data[idx]
            cat = tag_info["category"]
            conf = float(probs[idx])
            name = tag_info["name"]

            if cat == 4 and conf >= char_threshold:
                characters.append((name, conf))
                all_tags.append((name, 4, conf))
            elif cat == 3 and conf >= char_threshold:
                series.append((name, conf))
                all_tags.append((name, 3, conf))
            elif cat == 0 and conf >= gen_threshold:
                general.append((name, conf))
                all_tags.append((name, 0, conf))

        # 4. Rating (Category 5)
        rating = "general"
        if self.rating_indices:
            best_rating_idx = max(self.rating_indices, key=lambda i: probs[i])
            rating = self.tags_data[best_rating_idx]["name"]
            all_tags.append((rating, 5, float(probs[best_rating_idx])))

        # Sort by confidence descending
        characters.sort(key=lambda x: x[1], reverse=True)
        series.sort(key=lambda x: x[1], reverse=True)
        general.sort(key=lambda x: x[1], reverse=True)

        return {
            "characters": characters,
            "series": series,
            "general": general,
            "rating": rating,
            "all_tags": all_tags
        }

    def predict(self, media_path, char_threshold=DEFAULT_CHARACTER_THRESHOLD,
                gen_threshold=DEFAULT_GENERAL_THRESHOLD):
        """
        Convenience method: Prepares media tensor and runs inference.
        """
        tensor = self.prepare_media_tensor(media_path)
        if tensor is None:
            return None
        return self.predict_tensor(tensor, char_threshold=char_threshold, gen_threshold=gen_threshold)

    def predict_tensor_batch(self, batch_tensor, char_threshold=DEFAULT_CHARACTER_THRESHOLD,
                             gen_threshold=DEFAULT_GENERAL_THRESHOLD):
        """
        Batched GPU Inference: runs N images through ONNX in a single GPU call.
        batch_tensor shape: (N, H, W, C) — stacked by np.concatenate().
        Returns a list of N result dicts (same format as predict_tensor).
        Supports single model and Ensemble (Soft-Voting probability average).
        """
        if self.is_ensemble:
            if not self.sub_taggers:
                self.load_model()

            if batch_tensor is None or len(batch_tensor) == 0:
                return []

            all_batch_probs = []
            for sub in self.sub_taggers:
                t = batch_tensor.astype(sub.input_dtype) if batch_tensor.dtype != sub.input_dtype else batch_tensor
                outputs = sub.session.run([sub.output_name], {sub.input_name: t})
                sub_probs = outputs[0].astype(np.float32)
                if np.max(sub_probs) > 1.0 or np.min(sub_probs) < 0.0:
                    sub_probs = 1.0 / (1.0 + np.exp(-sub_probs))
                all_batch_probs.append(sub_probs)

            batch_probs = np.mean(all_batch_probs, axis=0)  # shape: (N, num_tags)
        else:
            if self.session is None:
                self.load_model()

            if batch_tensor is None or len(batch_tensor) == 0:
                return []

            # Auto cast dtype if needed
            if batch_tensor.dtype != self.input_dtype:
                batch_tensor = batch_tensor.astype(self.input_dtype)

            # Try single GPU call for entire batch
            if len(batch_tensor) > 1:
                try:
                    outputs = self.session.run([self.output_name], {self.input_name: batch_tensor})
                    batch_probs = outputs[0]  # shape: (N, num_tags)
                    if len(batch_probs) != len(batch_tensor):
                        batch_probs = None
                except Exception:
                    batch_probs = None
            else:
                outputs = self.session.run([self.output_name], {self.input_name: batch_tensor})
                batch_probs = outputs[0]

            if batch_probs is None:
                # Fallback to sequential inference if model ONNX graph has fixed batch dimension (legacy WD14 v1.4)
                probs_list = []
                for item in batch_tensor:
                    item_in = np.expand_dims(item, axis=0)
                    out = self.session.run([self.output_name], {self.input_name: item_in})
                    probs_list.append(out[0][0])
                batch_probs = np.array(probs_list)

        min_thresh = min(char_threshold, gen_threshold, 0.20)
        results = []

        for probs in batch_probs:
            probs = probs.astype(np.float32)

            # Apply sigmoid if raw logits were returned
            if np.max(probs) > 1.0 or np.min(probs) < 0.0:
                probs = 1.0 / (1.0 + np.exp(-probs))

            characters = []
            series = []
            general = []
            all_tags = []

            passing_indices = np.flatnonzero(probs >= min_thresh)
            for idx in passing_indices:
                tag_info = self.tags_data[idx]
                cat = tag_info["category"]
                conf = float(probs[idx])
                name = tag_info["name"]

                if cat == 4 and conf >= char_threshold:
                    characters.append((name, conf))
                    all_tags.append((name, 4, conf))
                elif cat == 3 and conf >= char_threshold:
                    series.append((name, conf))
                    all_tags.append((name, 3, conf))
                elif cat == 0 and conf >= gen_threshold:
                    general.append((name, conf))
                    all_tags.append((name, 0, conf))

            rating = "general"
            if self.rating_indices:
                best_rating_idx = max(self.rating_indices, key=lambda i: probs[i])
                rating = self.tags_data[best_rating_idx]["name"]
                all_tags.append((rating, 5, float(probs[best_rating_idx])))

            characters.sort(key=lambda x: x[1], reverse=True)
            series.sort(key=lambda x: x[1], reverse=True)
            general.sort(key=lambda x: x[1], reverse=True)

            results.append({
                "characters": characters,
                "series": series,
                "general": general,
                "rating": rating,
                "all_tags": all_tags
            })

        return results



