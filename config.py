import os
import json
import base64
import logging
import threading

DB_FILE = "pixiv_artists.db"
CONFIG_FILE = "config.json"

_CONFIG_LOCK = threading.RLock()
_SENSITIVE_KEYS = {"pixiv_cookie", "danbooru_api_key"}
_DPAPI_PREFIX = "dpapi:"


def encrypt_secret(plain_text: str) -> str:
    """
    Encrypts a sensitive string using Windows DPAPI (CryptProtectData) tied to the current Windows user.
    Falls back to plaintext on non-Windows platforms.
    """
    if not plain_text:
        return ""
    if plain_text.startswith(_DPAPI_PREFIX):
        return plain_text
    if os.name == "nt":
        try:
            import ctypes
            import ctypes.wintypes

            class DATA_BLOB(ctypes.Structure):
                _fields_ = [
                    ("cbData", ctypes.wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))
                ]

            raw_bytes = plain_text.encode("utf-8")
            buf = ctypes.create_string_buffer(raw_bytes, len(raw_bytes))
            in_blob = DATA_BLOB(len(raw_bytes), buf)
            out_blob = DATA_BLOB()

            # CRYPTPROTECT_UI_FORBIDDEN = 0x01
            if ctypes.windll.crypt32.CryptProtectData(
                ctypes.byref(in_blob), None, None, None, None, 0x01, ctypes.byref(out_blob)
            ):
                try:
                    encrypted_bytes = ctypes.string_at(out_blob.pbData, out_blob.cbData)
                    return _DPAPI_PREFIX + base64.b64encode(encrypted_bytes).decode("ascii")
                finally:
                    ctypes.windll.kernel32.LocalFree(out_blob.pbData)
        except Exception as e:
            logging.debug(f"DPAPI encryption fallback: {e}")
    return plain_text


def decrypt_secret(stored_value: str) -> str:
    """
    Decrypts a Windows DPAPI-protected string ('dpapi:...').
    Transparently returns plaintext if the value is not prefixed with 'dpapi:'.
    """
    if not stored_value or not isinstance(stored_value, str):
        return ""
    if not stored_value.startswith(_DPAPI_PREFIX):
        return stored_value
    b64_part = stored_value[len(_DPAPI_PREFIX):]
    if os.name == "nt":
        try:
            import ctypes
            import ctypes.wintypes

            class DATA_BLOB(ctypes.Structure):
                _fields_ = [
                    ("cbData", ctypes.wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))
                ]

            enc_bytes = base64.b64decode(b64_part)
            buf = ctypes.create_string_buffer(enc_bytes, len(enc_bytes))
            in_blob = DATA_BLOB(len(enc_bytes), buf)
            out_blob = DATA_BLOB()

            if ctypes.windll.crypt32.CryptUnprotectData(
                ctypes.byref(in_blob), None, None, None, None, 0x01, ctypes.byref(out_blob)
            ):
                try:
                    dec_bytes = ctypes.string_at(out_blob.pbData, out_blob.cbData)
                    return dec_bytes.decode("utf-8")
                finally:
                    ctypes.windll.kernel32.LocalFree(out_blob.pbData)
        except Exception as e:
            logging.debug(f"DPAPI decryption error: {e}")
            return ""
    return ""


def load_app_config(config_path: str = CONFIG_FILE) -> dict:
    """
    Thread-safe configuration loader with UTF-8 encoding and automatic DPAPI decryption
    for sensitive keys ('pixiv_cookie', 'danbooru_api_key').
    """
    with _CONFIG_LOCK:
        if not os.path.exists(config_path):
            return {}
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if not isinstance(cfg, dict):
                return {}
            for k in _SENSITIVE_KEYS:
                if k in cfg and isinstance(cfg[k], str) and cfg[k]:
                    cfg[k] = decrypt_secret(cfg[k])
            return cfg
        except Exception as e:
            logging.error(f"Error loading {config_path}: {e}")
            return {}


def update_app_config(updates: dict, config_path: str = CONFIG_FILE) -> dict:
    """
    Thread-safe atomic read-modify-write update for config.json.
    Prevents different dialogs/windows from overwriting each other's saved settings,
    and automatically encrypts sensitive credentials via Windows DPAPI on disk.
    """
    if not isinstance(updates, dict):
        return {}
    with _CONFIG_LOCK:
        current = {}
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        current = loaded
            except Exception:
                current = {}

        # Merge updates
        for k, v in updates.items():
            current[k] = v

        # Encrypt sensitive keys before writing to disk
        disk_copy = dict(current)
        for k in _SENSITIVE_KEYS:
            if k in disk_copy and isinstance(disk_copy[k], str) and disk_copy[k]:
                disk_copy[k] = encrypt_secret(disk_copy[k])

        try:
            temp_path = config_path + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(disk_copy, f, indent=4, ensure_ascii=False)
            os.replace(temp_path, config_path)
        except Exception as e:
            logging.error(f"Error saving {config_path}: {e}")

        # Return decrypted view for in-memory usage
        mem_copy = dict(current)
        for k in _SENSITIVE_KEYS:
            if k in mem_copy and isinstance(mem_copy[k], str) and mem_copy[k]:
                mem_copy[k] = decrypt_secret(mem_copy[k])
        return mem_copy


# File Extensions
EXT_IMG = {'.jpg', '.jpeg', '.png', '.webp', '.bmp'}
EXT_GIF = {'.gif'}
EXT_VID = {'.mp4', '.mov', '.avi', '.webm', '.mkv'}
EXT_ZIP = {'.zip', '.ugoira'}
ALL_MEDIA_EXT = EXT_IMG | EXT_GIF | EXT_VID | EXT_ZIP

# Safety limits for archive / media extraction (Zip Bomb & OOM protection)
MAX_UGOIRA_FRAME_BYTES = 50 * 1024 * 1024    # 50 MB max per single frame
MAX_UGOIRA_TOTAL_BYTES = 500 * 1024 * 1024   # 500 MB max uncompressed archive total
MAX_UGOIRA_FRAMES = 2000                     # Max 2,000 frames per Ugoira archive

# AI Model Configuration
AI_MODELS_DIR = "models"
DEFAULT_TAGGER_MODEL = os.path.join(AI_MODELS_DIR, "model.onnx")
DEFAULT_TAGGER_MODEL_FP16 = os.path.join(AI_MODELS_DIR, "model_fp16.onnx")
DEFAULT_TAGS_CSV = os.path.join(AI_MODELS_DIR, "selected_tags.csv")
DEFAULT_CHARACTER_THRESHOLD = 0.35
DEFAULT_GENERAL_THRESHOLD = 0.35

# Hugging Face Model Registry (WD14, WD v3, PixAI & Camie Series)
AVAILABLE_AI_MODELS = {
    "wd-v1-4-convnext-v2": {
        "id": "wd-v1-4-convnext-v2",
        "name": "🔹 WD14 ConvNeXt V2 (Waifu Diffusion 1.4 - สแตนดาร์ด ConvNeXt V2)",
        "short_name": "WD14 ConvNeXt V2",
        "repo": "SmilingWolf/wd-v1-4-convnextv2-tagger-v2",
        "model_url": "https://huggingface.co/SmilingWolf/wd-v1-4-convnextv2-tagger-v2/resolve/main/model.onnx",
        "tags_url": "https://huggingface.co/SmilingWolf/wd-v1-4-convnextv2-tagger-v2/resolve/main/selected_tags.csv",
        "subdir": "",
        "size_hint": "~370 MB",
        "preprocess_mode": "wd",
        "input_size": 448,
        "legacy": True
    },
    "wd-v3-convnext": {
        "id": "wd-v3-convnext",
        "name": "⭐ WD Tagger v3 - ConvNeXt (~10,800+ แท็ก / แนะนำ 🔥)",
        "short_name": "WD v3 ConvNeXt",
        "repo": "SmilingWolf/wd-convnext-tagger-v3",
        "model_url": "https://huggingface.co/SmilingWolf/wd-convnext-tagger-v3/resolve/main/model.onnx",
        "tags_url": "https://huggingface.co/SmilingWolf/wd-convnext-tagger-v3/resolve/main/selected_tags.csv",
        "subdir": "wd_v3_convnext",
        "size_hint": "~377 MB",
        "preprocess_mode": "wd",
        "input_size": 448,
        "legacy": False
    },
    "wd-v3-vit": {
        "id": "wd-v3-vit",
        "name": "🎯 WD Tagger v3 - ViT (Vision Transformer / แม่นยำสูง)",
        "short_name": "WD v3 ViT",
        "repo": "SmilingWolf/wd-vit-tagger-v3",
        "model_url": "https://huggingface.co/SmilingWolf/wd-vit-tagger-v3/resolve/main/model.onnx",
        "tags_url": "https://huggingface.co/SmilingWolf/wd-vit-tagger-v3/resolve/main/selected_tags.csv",
        "subdir": "wd_v3_vit",
        "size_hint": "~361 MB",
        "preprocess_mode": "wd",
        "input_size": 448,
        "legacy": False
    },
    "wd-v3-swinv2": {
        "id": "wd-v3-swinv2",
        "name": "👑 WD Tagger v3 - SwinV2 (Ultra Accuracy / รายละเอียดสูง)",
        "short_name": "WD v3 SwinV2",
        "repo": "SmilingWolf/wd-swinv2-tagger-v3",
        "model_url": "https://huggingface.co/SmilingWolf/wd-swinv2-tagger-v3/resolve/main/model.onnx",
        "tags_url": "https://huggingface.co/SmilingWolf/wd-swinv2-tagger-v3/resolve/main/selected_tags.csv",
        "subdir": "wd_v3_swinv2",
        "size_hint": "~446 MB",
        "preprocess_mode": "wd",
        "input_size": 448,
        "legacy": False
    },
    "wd-v3-eva02-large": {
        "id": "wd-v3-eva02-large",
        "name": "🏆 WD Tagger v3 - EVA02-Large (King of Taggers / แม่นยำที่สุด 🔥)",
        "short_name": "WD v3 EVA02-Large",
        "repo": "SmilingWolf/wd-eva02-large-tagger-v3",
        "model_url": "https://huggingface.co/SmilingWolf/wd-eva02-large-tagger-v3/resolve/main/model.onnx",
        "tags_url": "https://huggingface.co/SmilingWolf/wd-eva02-large-tagger-v3/resolve/main/selected_tags.csv",
        "subdir": "wd_v3_eva02_large",
        "size_hint": "~1.2 GB",
        "preprocess_mode": "wd",
        "input_size": 448,
        "legacy": False
    },
    "wd-v3-vit-large": {
        "id": "wd-v3-vit-large",
        "name": "🎯 WD Tagger v3 - ViT-Large (Vision Transformer Large / วิเคราะห์ภาพซับซ้อน)",
        "short_name": "WD v3 ViT-Large",
        "repo": "SmilingWolf/wd-vit-large-tagger-v3",
        "model_url": "https://huggingface.co/SmilingWolf/wd-vit-large-tagger-v3/resolve/main/model.onnx",
        "tags_url": "https://huggingface.co/SmilingWolf/wd-vit-large-tagger-v3/resolve/main/selected_tags.csv",
        "subdir": "wd_v3_vit_large",
        "size_hint": "~1.2 GB",
        "preprocess_mode": "wd",
        "input_size": 448,
        "legacy": False
    },
    "pixai-tagger-v0.9": {
        "id": "pixai-tagger-v0.9",
        "name": "🔥 PixAI Tagger v0.9 (13,461 แท็ก + ผูก Series อัตโนมัติ / ตัวละครใหม่แม่นมาก)",
        "short_name": "PixAI Tagger v0.9",
        "repo": "deepghs/pixai-tagger-v0.9-onnx",
        "model_url": "https://huggingface.co/deepghs/pixai-tagger-v0.9-onnx/resolve/main/model.onnx",
        "tags_url": "https://huggingface.co/deepghs/pixai-tagger-v0.9-onnx/resolve/main/selected_tags.csv",
        "tags_filename": "selected_tags.csv",
        "subdir": "pixai_tagger_v0_9",
        "size_hint": "~1.2 GB",
        "preprocess_mode": "pixai",
        "input_size": 448,
        "output_index": 2,
        "output_name": "prediction",
        "prune_fp16_output": True,
        "min_char_threshold": 0.85,
        "legacy": False
    },
    "camie-tagger-v2": {
        "id": "camie-tagger-v2",
        "name": "🌟 Camie Tagger v2 (70,527 แท็ก / ละเอียดสุดขีด สาย LoRA & Illustrious)",
        "short_name": "Camie Tagger v2",
        "repo": "Camais03/camie-tagger-v2",
        "model_url": "https://huggingface.co/Camais03/camie-tagger-v2/resolve/main/camie-tagger-v2.onnx",
        "tags_url": "https://huggingface.co/Camais03/camie-tagger-v2/resolve/main/camie-tagger-v2-metadata.json",
        "tags_filename": "camie-tagger-v2-metadata.json",
        "subdir": "camie_tagger_v2",
        "size_hint": "~755 MB",
        "preprocess_mode": "camie",
        "input_size": 512,
        "output_index": 1,
        "output_name": "refined_predictions",
        "min_char_threshold": 0.50,
        "min_gen_threshold": 0.45,
        "legacy": False
    },
    "wd-v3-ensemble": {
        "id": "wd-v3-ensemble",
        "name": "👑 WD Tagger v3 - Standard Ensemble (3-in-1: ConvNeXt + ViT + SwinV2)",
        "short_name": "WD v3 Ensemble (3-in-1)",
        "repo": "SmilingWolf/wd-v3-ensemble",
        "model_url": "",
        "tags_url": "",
        "subdir": "wd_v3_ensemble",
        "size_hint": "~1.2 GB",
        "is_ensemble": True,
        "ensemble_keys": ["wd-v3-convnext", "wd-v3-vit", "wd-v3-swinv2"],
        "legacy": False
    },
    "wd-v3-ultra-ensemble": {
        "id": "wd-v3-ultra-ensemble",
        "name": "👑 WD Tagger v3 - Ultra Ensemble (รวมพลัง 3 ตัวท็อป: EVA02-Large + SwinV2 + ConvNeXt 🔥)",
        "short_name": "WD v3 Ultra Ensemble",
        "repo": "SmilingWolf/wd-v3-ultra-ensemble",
        "model_url": "",
        "tags_url": "",
        "subdir": "wd_v3_ultra_ensemble",
        "size_hint": "~2.0 GB",
        "is_ensemble": True,
        "ensemble_keys": ["wd-v3-eva02-large", "wd-v3-swinv2", "wd-v3-convnext"],
        "legacy": False
    },
    "all-best-ensemble": {
        "id": "all-best-ensemble",
        "name": "🌌 All Best Model in One (รวมสุดยอด 3 ค่าย: WD v3 EVA02 + PixAI v0.9 + Camie v2 🔥)",
        "short_name": "All Best 3-in-1 (EVA02 + PixAI + Camie)",
        "repo": "PixKura/all-best-ensemble",
        "model_url": "",
        "tags_url": "",
        "subdir": "all_best_ensemble",
        "size_hint": "~3.2 GB",
        "is_ensemble": True,
        "is_heterogeneous": True,
        "ensemble_keys": ["wd-v3-eva02-large", "pixai-tagger-v0.9", "camie-tagger-v2"],
        "legacy": False
    }
}
DEFAULT_MODEL_KEY = "wd-v1-4-convnext-v2"

# Hugging Face Model URLs (Legacy fallback)
HF_MODEL_URL = AVAILABLE_AI_MODELS[DEFAULT_MODEL_KEY]["model_url"]
HF_TAGS_URL = AVAILABLE_AI_MODELS[DEFAULT_MODEL_KEY]["tags_url"]


class AppConfig:
    APP_NAME = "PixKura"
    AUTHOR = "Kurito"
    VERSION = "2.4.0"
    MILESTONE = "V.32"
