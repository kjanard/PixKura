import os
import csv
import json
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
    tags_filename = meta.get("tags_filename", "selected_tags.csv")
    tags_csv = os.path.join(model_dir, tags_filename)
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
    Local AI Tagger supporting WD14 Tagger (v1.4), WD Tagger v3 Series, PixAI Tagger v0.9,
    Camie Tagger v2, and Ensemble 3-in-1 in ONNX format.
    Supports GPU & NPU acceleration via DirectML, CUDA, OpenVINO, QNN, FP16 Half-Precision, and CPU fallback.
    """
    def __init__(self, model_key=DEFAULT_MODEL_KEY, model_path=None, tags_path=None, use_gpu=True, use_fp16=False):
        self.model_key = model_key or DEFAULT_MODEL_KEY
        self.use_fp16 = use_fp16
        self.use_gpu = use_gpu
        paths = get_model_paths(self.model_key)
        self.meta = paths["meta"]
        self.is_ensemble = self.meta.get("is_ensemble", False)
        self.is_heterogeneous = self.meta.get("is_heterogeneous", False)
        self.ensemble_keys = self.meta.get("ensemble_keys", [])
        self.preprocess_mode = self.meta.get("preprocess_mode", "wd")
        self.output_index = int(self.meta.get("output_index", 0))
        self.default_input_size = int(self.meta.get("input_size", 448))
        self.has_ips_mapping = False
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
        self.tags_data = [] # List of dict: {id, name, category, count, ips}
        self.char_indices = []
        self.series_indices = []
        self.general_indices = []
        self.rating_indices = []
        self.input_name = None
        if self.preprocess_mode in ("pixai", "camie"):
            self.input_shape = [1, 3, self.default_input_size, self.default_input_size]
        else:
            self.input_shape = [1, self.default_input_size, self.default_input_size, 3]
        self.input_dtype = np.float16 if self.use_fp16 else np.float32
        self.output_name = None
        self.active_provider = None

    @staticmethod
    def get_available_providers():
        """Returns available ONNX execution providers and checks GPU/NPU support."""
        all_providers = ort.get_available_providers()
        has_directml = 'DmlExecutionProvider' in all_providers
        has_cuda = 'CUDAExecutionProvider' in all_providers
        has_openvino = 'OpenVINOExecutionProvider' in all_providers
        has_qnn = 'QNNExecutionProvider' in all_providers
        return {
            "all_providers": all_providers,
            "has_directml": has_directml,
            "has_cuda": has_cuda,
            "has_openvino": has_openvino,
            "has_qnn": has_qnn,
            "has_npu": has_openvino or has_qnn,
            "has_gpu": has_directml or has_cuda or has_openvino or has_qnn
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
                # Ensure dynamic batch size is set on input & outputs if currently fixed
                patched = False
                inp = model.graph.input[0]
                if len(inp.type.tensor_type.shape.dim) > 0 and not inp.type.tensor_type.shape.dim[0].dim_param:
                    inp.type.tensor_type.shape.dim[0].dim_value = 0
                    inp.type.tensor_type.shape.dim[0].dim_param = 'batch_size'
                    patched = True
                for out in model.graph.output:
                    if len(out.type.tensor_type.shape.dim) > 0 and not out.type.tensor_type.shape.dim[0].dim_param:
                        out.type.tensor_type.shape.dim[0].dim_value = 0
                        out.type.tensor_type.shape.dim[0].dim_param = 'batch_size'
                        patched = True
                if patched:
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

        tags_fname = os.path.basename(paths["tags_csv"])
        files = [
            (tags_fname, meta["tags_url"], paths["tags_csv"]),
            ("model.onnx", meta["model_url"], paths["model_fp32"])
        ]

        for filename, url, dest_path in files:
            temp_path = dest_path + ".download"

            if os.path.exists(dest_path) and os.path.getsize(dest_path) > 1024 * 1024:
                continue

            resp = requests.get(url, stream=True, timeout=30)
            try:
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
            finally:
                try:
                    resp.close()
                except Exception:
                    pass

            if total_size > 0 and downloaded != total_size:
                if os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
                raise IOError(f"ดาวน์โหลดไฟล์ {filename} ไม่สมบูรณ์ ({downloaded}/{total_size} bytes)")

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
        """
        Loads and parses tag metadata:
        - SmilingWolf WD CSV (tag_id, name, category, count)
        - PixAI v0.9 CSV (id, tag_id, name, category, count, ips) with automatic character->series IP mapping
        - Camie Tagger v2 JSON metadata (idx_to_tag, tag_to_category)
        """
        if not os.path.exists(self.tags_path):
            raise FileNotFoundError(f"ไม่พบไฟล์แท็ก: {self.tags_path}")

        self.tags_data = []
        self.char_indices = []
        self.series_indices = []
        self.general_indices = []
        self.rating_indices = []
        self.has_ips_mapping = False

        if self.tags_path.lower().endswith(".json"):
            with open(self.tags_path, mode='r', encoding='utf-8') as f:
                metadata = json.load(f)
            dataset_info = metadata.get("dataset_info", {})
            tag_mapping = dataset_info.get("tag_mapping", {})
            idx_to_tag = tag_mapping.get("idx_to_tag", {})
            tag_to_category = tag_mapping.get("tag_to_category", {})
            total_tags = int(dataset_info.get("total_tags", len(idx_to_tag)))

            cat_str_to_int = {
                "general": 0,
                "copyright": 3,
                "character": 4,
                "rating": 5,
            }
            rating_name_map = {
                "rating_general": "general",
                "rating_sensitive": "sensitive",
                "rating_questionable": "questionable",
                "rating_explicit": "explicit",
            }

            for idx in range(total_tags):
                raw_name = idx_to_tag.get(str(idx), idx_to_tag.get(idx, ""))
                cat_str = tag_to_category.get(raw_name, "")
                category = cat_str_to_int.get(cat_str, -1)
                name = rating_name_map.get(raw_name, raw_name) if category == 5 else raw_name

                tag_item = {
                    "index": idx,
                    "id": idx,
                    "name": name,
                    "category": category,
                    "count": 0,
                    "ips": []
                }
                self.tags_data.append(tag_item)

                if category == 4:
                    self.char_indices.append(idx)
                elif category == 3:
                    self.series_indices.append(idx)
                elif category == 0:
                    self.general_indices.append(idx)
                elif category == 5:
                    self.rating_indices.append(idx)

            logging.info(
                f"Loaded {len(self.tags_data)} JSON tags "
                f"({len(self.char_indices)} characters, {len(self.series_indices)} series, {len(self.rating_indices)} ratings)."
            )
            return

        with open(self.tags_path, mode='r', encoding='utf-8') as f:
            reader = csv.reader(f)
            header = next(reader, None) or []
            header_lower = [h.strip().lower() for h in header]

            col_id = header_lower.index("tag_id") if "tag_id" in header_lower else 0
            col_name = header_lower.index("name") if "name" in header_lower else 1
            col_cat = header_lower.index("category") if "category" in header_lower else 2
            col_count = header_lower.index("count") if "count" in header_lower else 3
            col_ips = header_lower.index("ips") if "ips" in header_lower else -1

            min_cols = max(col_name, col_cat) + 1
            for idx, row in enumerate(reader):
                if not row or len(row) < min_cols:
                    continue
                tag_id = int(row[col_id]) if col_id < len(row) and row[col_id].isdigit() else idx
                name = row[col_name]
                category = int(row[col_cat]) if row[col_cat].isdigit() else 0
                count = int(row[col_count]) if col_count < len(row) and row[col_count].isdigit() else 0

                ips_list = []
                if col_ips >= 0 and col_ips < len(row) and row[col_ips]:
                    try:
                        parsed_ips = json.loads(row[col_ips])
                        if isinstance(parsed_ips, list) and parsed_ips:
                            ips_list = [str(ip).strip() for ip in parsed_ips if str(ip).strip()]
                            if ips_list:
                                self.has_ips_mapping = True
                    except Exception:
                        ips_list = []

                tag_item = {
                    "index": idx,
                    "id": tag_id,
                    "name": name,
                    "category": category, # 0: General, 3: Copyright/Series, 4: Character, 5: Rating
                    "count": count,
                    "ips": ips_list
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

        logging.info(
            f"Loaded {len(self.tags_data)} tags ({len(self.char_indices)} characters, "
            f"{len(self.series_indices)} series, ips_mapping={self.has_ips_mapping})."
        )

    def load_model(self):
        """Initializes ONNX Runtime InferenceSession with DirectML/CUDA/NPU or CPU."""
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
            for ep in ('DmlExecutionProvider', 'CUDAExecutionProvider', 'OpenVINOExecutionProvider', 'QNNExecutionProvider'):
                if ep in available:
                    providers.append(ep)
        
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
                outputs = self.session.get_outputs()
                out_idx = self.output_index if self.output_index < len(outputs) else 0
                self.input_name = inputs[0].name
                self.input_shape = inputs[0].shape
                self.input_dtype = np.float16 if "float16" in inputs[0].type else np.float32
                self.output_name = outputs[out_idx].name
                logging.info(f"Initialized WD14Tagger on {self.active_provider} (FP16). Input: {self.input_name} {self.input_shape} ({self.input_dtype}) -> Output: {self.output_name}")
                return
            except Exception as e:
                logging.warning(f"FP16 load failed for {self.model_key} ({e}), falling back to FP32 DirectML...")

        # 2. Fallback to FP32 model
        if not os.path.exists(paths["model_fp32"]):
            raise FileNotFoundError(f"ไม่พบไฟล์โมเดล AI: {paths['model_fp32']}")

        self.session = ort.InferenceSession(paths["model_fp32"], sess_options=sess_options, providers=providers)
        self.active_provider = self.session.get_providers()[0]
        inputs = self.session.get_inputs()
        outputs = self.session.get_outputs()
        out_idx = self.output_index if self.output_index < len(outputs) else 0
        self.input_name = inputs[0].name
        self.input_shape = inputs[0].shape
        self.input_dtype = np.float16 if "float16" in inputs[0].type else np.float32
        self.output_name = outputs[out_idx].name
        logging.info(f"Initialized WD14Tagger on {self.active_provider} (FP32). Input: {self.input_name} {self.input_shape} ({self.input_dtype}) -> Output: {self.output_name}")

    def unload(self):
        """Releases ONNX Runtime InferenceSession and GPU / NPU device memory buffers."""
        if self.is_ensemble:
            for sub in self.sub_taggers:
                try: sub.unload()
                except Exception: pass
            self.sub_taggers = []
        self.session = None

    def preprocess_image(self, pil_image, target_size=None):
        """
        Preprocesses PIL Image for AI Tagger:
        - Alpha composition with white background for transparent PNGs
        - 'wd': Aspect-ratio preserve + white padding, BGR [0..255]
        - 'pixai': Direct bilinear resize to 448x448, RGB normalized to [-1, 1], NCHW
        - 'camie': Aspect-ratio LANCZOS resize + ImageNet mean (124,116,104) padding to 512x512,
                   RGB ImageNet normalized, NCHW
        """
        if target_size is None:
            target_size = self.default_input_size

        if pil_image.mode in ('RGBA', 'LA') or (pil_image.mode == 'P' and hasattr(pil_image, 'info') and 'transparency' in pil_image.info):
            canvas = Image.new('RGB', pil_image.size, (255, 255, 255))
            pil_image = pil_image.convert('RGBA')
            canvas.paste(pil_image, mask=pil_image.split()[3])
            pil_image = canvas
        else:
            pil_image = pil_image.convert('RGB')

        if self.preprocess_mode == "pixai":
            resized = pil_image.resize((target_size, target_size), Image.Resampling.BILINEAR)
            img_array = (np.asarray(resized, dtype=np.float32) * (1.0 / 127.5) - 1.0).astype(self.input_dtype)
            img_array = np.transpose(img_array, (2, 0, 1))
            return np.expand_dims(img_array, axis=0)

        if self.preprocess_mode == "camie":
            w, h = pil_image.size
            scale = target_size / max(w, h)
            new_w, new_h = max(1, int(w * scale)), max(1, int(h * scale))
            resized = pil_image.resize((new_w, new_h), Image.Resampling.LANCZOS)
            padded = Image.new('RGB', (target_size, target_size), (124, 116, 104))
            paste_x = (target_size - new_w) // 2
            paste_y = (target_size - new_h) // 2
            padded.paste(resized, (paste_x, paste_y))
            mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
            std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
            img_array = ((np.asarray(padded, dtype=np.float32) / 255.0 - mean) / std).astype(self.input_dtype)
            img_array = np.transpose(img_array, (2, 0, 1))
            return np.expand_dims(img_array, axis=0)

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

    def get_sub_preprocess_specs(self):
        """
        Returns a list of (model_key, target_size, input_dtype, is_nchw, preprocess_mode)
        for each sub-model in a heterogeneous ensemble so OpenCV can decode each image once
        and generate all required input tensors in one pass.
        """
        specs = []
        source_taggers = self.sub_taggers if self.sub_taggers else [
            WD14Tagger(k, use_gpu=self.use_gpu, use_fp16=self.use_fp16) for k in self.ensemble_keys
        ]
        for sub in source_taggers:
            target_size = sub.default_input_size
            is_nchw = sub.preprocess_mode in ("pixai", "camie")
            if len(sub.input_shape) == 4:
                if sub.input_shape[1] == 3:
                    is_nchw = True
                    if isinstance(sub.input_shape[2], int) and sub.input_shape[2] > 3:
                        target_size = sub.input_shape[2]
                elif isinstance(sub.input_shape[1], int) and sub.input_shape[1] > 3:
                    target_size = sub.input_shape[1]
            specs.append((sub.model_key, target_size, sub.input_dtype, is_nchw, sub.preprocess_mode))
        return specs

    @staticmethod
    def _merge_heterogeneous_results(per_model_results, has_explicit_rating_flags=None):
        """
        Fuses predictions from heterogeneous models with different vocabularies
        (e.g., WD v3 EVA02-Large + PixAI v0.9 + Camie v2):
        - Takes union of detected tags across all sub-models so newer/rare characters
          known only to PixAI or Camie are never suppressed by older vocabularies.
        - Uses max confidence with a small consensus boost (+0.02 per extra agreeing model,
          capped at 0.999) so tags agreed upon by multiple architectures rank highest.
        - Prioritizes models with explicit trained rating classes (WD v3 / Camie v2)
          when selecting the final content rating.
        """
        valid_entries = [
            (res, has_explicit_rating_flags[i] if has_explicit_rating_flags and i < len(has_explicit_rating_flags) else True)
            for i, res in enumerate(per_model_results)
            if res is not None
        ]
        if not valid_entries:
            return None

        char_scores = {}    # name -> [confs]
        series_scores = {}  # name -> [confs]
        gen_scores = {}     # name -> [confs]
        explicit_rating_votes = {}  # rating_name -> [confs]
        fallback_rating_votes = {}  # rating_name -> [confs]

        for res, has_explicit_rating in valid_entries:
            for name, conf in res.get("characters", []):
                char_scores.setdefault(name, []).append(float(conf))
            for name, conf in res.get("series", []):
                series_scores.setdefault(name, []).append(float(conf))
            for name, conf in res.get("general", []):
                gen_scores.setdefault(name, []).append(float(conf))

            # Find rating confidence from all_tags (cat 5)
            r_name = res.get("rating", "general")
            r_conf = 0.5
            for t_name, t_cat, t_conf in res.get("all_tags", []):
                if t_cat == 5 and t_name == r_name:
                    r_conf = float(t_conf)
                    break
            target_votes = explicit_rating_votes if has_explicit_rating else fallback_rating_votes
            target_votes.setdefault(r_name, []).append(r_conf)

        def _fuse_bucket(bucket_dict, cat_id):
            fused = []
            tags_cat = []
            for name, confs in bucket_dict.items():
                best = max(confs)
                votes = len(confs)
                score = min(0.999, best + 0.02 * (votes - 1)) if votes > 1 else best
                fused.append((name, score))
                tags_cat.append((name, cat_id, score))
            fused.sort(key=lambda x: x[1], reverse=True)
            return fused, tags_cat

        characters, char_tags = _fuse_bucket(char_scores, 4)
        series, series_tags = _fuse_bucket(series_scores, 3)
        general, gen_tags = _fuse_bucket(gen_scores, 0)
        all_tags = char_tags + series_tags + gen_tags

        # Rating fusion: prefer explicit trained rating heads; break ties conservatively
        rating_pool = explicit_rating_votes if explicit_rating_votes else fallback_rating_votes
        rating = "general"
        rating_conf = 0.90
        if rating_pool:
            severity_order = {"general": 0, "sensitive": 1, "questionable": 2, "explicit": 3}
            best_r = max(
                rating_pool.items(),
                key=lambda kv: (sum(kv[1]), severity_order.get(kv[0], 0))
            )
            rating = best_r[0]
            rating_conf = max(best_r[1])
        all_tags.append((rating, 5, rating_conf))

        return {
            "characters": characters,
            "series": series,
            "general": general,
            "rating": rating,
            "all_tags": all_tags
        }

    def prepare_media_tensor(self, media_path):
        """
        Loads and pre-processes a media file into a GPU-ready float32/float16 tensor
        (or a dict of {sub_model_key: tensor} for heterogeneous ensembles).
        Ideal for multi-threaded async prefetching pipeline.
        """
        if self.is_ensemble:
            if self.is_heterogeneous:
                try:
                    pil_img, _ = load_media_thumbnail(media_path)
                    if pil_img is None or pil_img.size == (0, 0):
                        return None
                    source_taggers = self.sub_taggers if self.sub_taggers else [
                        WD14Tagger(k, use_gpu=self.use_gpu, use_fp16=self.use_fp16) for k in self.ensemble_keys
                    ]
                    tensor_dict = {}
                    try:
                        for sub in source_taggers:
                            t_size = sub.default_input_size
                            if len(sub.input_shape) == 4:
                                if isinstance(sub.input_shape[1], int) and sub.input_shape[1] > 3:
                                    t_size = sub.input_shape[1]
                                elif isinstance(sub.input_shape[2], int) and sub.input_shape[2] > 3:
                                    t_size = sub.input_shape[2]
                            tensor_dict[sub.model_key] = sub.preprocess_image(pil_img, target_size=t_size)
                    finally:
                        try:
                            pil_img.close()
                        except Exception:
                            pass
                    return tensor_dict
                except Exception as e:
                    logging.debug(f"Error preparing heterogeneous tensors for {media_path}: {e}")
                    return None

            if self.sub_taggers:
                return self.sub_taggers[0].prepare_media_tensor(media_path)
            first_sub = WD14Tagger(self.ensemble_keys[0], use_gpu=self.use_gpu, use_fp16=self.use_fp16)
            return first_sub.prepare_media_tensor(media_path)

        try:
            pil_img, _ = load_media_thumbnail(media_path)
            if pil_img is None or pil_img.size == (0, 0):
                return None

            target_size = self.default_input_size
            if len(self.input_shape) == 4:
                if isinstance(self.input_shape[1], int) and self.input_shape[1] > 3:
                    target_size = self.input_shape[1]
                elif isinstance(self.input_shape[2], int) and self.input_shape[2] > 3:
                    target_size = self.input_shape[2]

            try:
                return self.preprocess_image(pil_img, target_size=target_size)
            finally:
                try:
                    pil_img.close()
                except Exception:
                    pass
        except Exception as e:
            logging.debug(f"Error preparing tensor for {media_path}: {e}")
            return None

    def _extract_tags_from_probs(self, probs, char_threshold, gen_threshold):
        """
        Converts probability/logit vector into structured characters, series, general, rating, and all_tags.
        - Applies numerically stable sigmoid if raw logits are detected.
        - Automatically infers Series/Copyright tags from character `ips` metadata (PixAI v0.9).
        - Infers fallback rating from explicit/sensitive general tags if model has no explicit rating class.
        """
        probs = probs.astype(np.float32)
        if probs.size > 0 and (np.max(probs) > 1.0 or np.min(probs) < 0.0):
            probs = 1.0 / (1.0 + np.exp(-np.clip(probs, -60.0, 60.0)))

        characters = []
        series = []
        general = []
        all_tags = []
        seen_series = {}
        inferred_series = {}

        min_thresh = min(char_threshold, gen_threshold, 0.20)
        passing_indices = np.flatnonzero(probs >= min_thresh)
        num_tags = len(self.tags_data)

        for idx in passing_indices:
            if idx >= num_tags:
                continue
            tag_info = self.tags_data[idx]
            cat = tag_info["category"]
            conf = float(probs[idx])
            name = tag_info["name"]

            if cat == 4 and conf >= char_threshold:
                characters.append((name, conf))
                all_tags.append((name, 4, conf))
                # Automatic Character -> Series/IP mapping (e.g. PixAI Tagger v0.9 `ips` column)
                for ip_name in tag_info.get("ips", ()):
                    if ip_name and conf > inferred_series.get(ip_name, 0.0):
                        inferred_series[ip_name] = conf
            elif cat == 3 and conf >= char_threshold:
                if conf > seen_series.get(name, 0.0):
                    seen_series[name] = conf
                series.append((name, conf))
                all_tags.append((name, 3, conf))
            elif cat == 0 and conf >= gen_threshold:
                general.append((name, conf))
                all_tags.append((name, 0, conf))

        # Merge auto-mapped IP/Series tags without duplicating directly detected series
        for ip_name, ip_conf in inferred_series.items():
            if ip_name not in seen_series:
                series.append((ip_name, ip_conf))
                all_tags.append((ip_name, 3, ip_conf))

        # Rating (Category 5)
        rating = "general"
        if self.rating_indices:
            valid_rating_indices = [i for i in self.rating_indices if i < probs.size]
            if valid_rating_indices:
                best_rating_idx = max(valid_rating_indices, key=lambda i: probs[i])
                rating = self.tags_data[best_rating_idx]["name"]
                all_tags.append((rating, 5, float(probs[best_rating_idx])))
        else:
            # Smart rating inference for models without rating classes (e.g. PixAI v0.9)
            try:
                from query_parser import NSFW_EXPLICIT_TAGS, SENSITIVE_ECCHI_TAGS
                gen_names = {g[0] for g in general}
                if gen_names & set(NSFW_EXPLICIT_TAGS):
                    rating = "explicit"
                    all_tags.append(("explicit", 5, 0.85))
                elif gen_names & set(SENSITIVE_ECCHI_TAGS):
                    rating = "sensitive"
                    all_tags.append(("sensitive", 5, 0.75))
                else:
                    all_tags.append(("general", 5, 0.90))
            except Exception:
                all_tags.append(("general", 5, 0.90))

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

            if self.is_heterogeneous:
                per_model_results = []
                explicit_flags = []
                for sub in self.sub_taggers:
                    sub_input = input_data.get(sub.model_key) if isinstance(input_data, dict) else input_data
                    if sub_input is None:
                        continue
                    per_model_results.append(
                        sub.predict_tensor(sub_input, char_threshold=char_threshold, gen_threshold=gen_threshold)
                    )
                    explicit_flags.append(bool(sub.rating_indices))
                return self._merge_heterogeneous_results(per_model_results, explicit_flags)

            all_probs = []
            for sub in self.sub_taggers:
                t = input_data.astype(sub.input_dtype) if input_data.dtype != sub.input_dtype else input_data
                outputs = sub.session.run([sub.output_name], {sub.input_name: t})
                sub_probs = outputs[0][0].astype(np.float32)
                if np.max(sub_probs) > 1.0 or np.min(sub_probs) < 0.0:
                    sub_probs = 1.0 / (1.0 + np.exp(-np.clip(sub_probs, -60.0, 60.0)))
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
            probs = outputs[0][0]

        return self._extract_tags_from_probs(probs, char_threshold=char_threshold, gen_threshold=gen_threshold)

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
        batch_tensor shape: (N, H, W, C) or (N, C, H, W) — stacked by np.concatenate().
        Returns a list of N result dicts (same format as predict_tensor).
        Supports single model and Ensemble (Soft-Voting probability average).
        """
        if self.is_ensemble:
            if not self.sub_taggers:
                self.load_model()

            if batch_tensor is None or len(batch_tensor) == 0:
                return []

            if self.is_heterogeneous and isinstance(batch_tensor, dict):
                per_model_batches = []
                explicit_flags = []
                batch_len = 0
                for sub in self.sub_taggers:
                    sub_batch = batch_tensor.get(sub.model_key)
                    if sub_batch is None or len(sub_batch) == 0:
                        continue
                    batch_len = len(sub_batch)
                    per_model_batches.append(
                        sub.predict_tensor_batch(sub_batch, char_threshold=char_threshold, gen_threshold=gen_threshold)
                    )
                    explicit_flags.append(bool(sub.rating_indices))

                if not per_model_batches or batch_len == 0:
                    return []

                return [
                    self._merge_heterogeneous_results([mb[i] for mb in per_model_batches], explicit_flags)
                    for i in range(batch_len)
                ]

            all_batch_probs = []
            for sub in self.sub_taggers:
                t = batch_tensor.astype(sub.input_dtype) if batch_tensor.dtype != sub.input_dtype else batch_tensor
                outputs = sub.session.run([sub.output_name], {sub.input_name: t})
                sub_probs = outputs[0].astype(np.float32)
                if np.max(sub_probs) > 1.0 or np.min(sub_probs) < 0.0:
                    sub_probs = 1.0 / (1.0 + np.exp(-np.clip(sub_probs, -60.0, 60.0)))
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

        return [
            self._extract_tags_from_probs(probs, char_threshold=char_threshold, gen_threshold=gen_threshold)
            for probs in batch_probs
        ]
