import os
import base64

DB_FILE = "pixiv_artists.db"
CONFIG_FILE = "config.json"

# File Extensions
EXT_IMG = {'.jpg', '.jpeg', '.png', '.webp', '.bmp'}
EXT_GIF = {'.gif'}
EXT_VID = {'.mp4', '.mov', '.avi', '.webm', '.mkv'}
EXT_ZIP = {'.zip', '.ugoira'}
ALL_MEDIA_EXT = EXT_IMG | EXT_GIF | EXT_VID | EXT_ZIP

# AI Model Configuration
AI_MODELS_DIR = "models"
DEFAULT_TAGGER_MODEL = os.path.join(AI_MODELS_DIR, "model.onnx")
DEFAULT_TAGGER_MODEL_FP16 = os.path.join(AI_MODELS_DIR, "model_fp16.onnx")
DEFAULT_TAGS_CSV = os.path.join(AI_MODELS_DIR, "selected_tags.csv")
DEFAULT_CHARACTER_THRESHOLD = 0.35
DEFAULT_GENERAL_THRESHOLD = 0.35

# Hugging Face Model Registry (WD14 & WD v3 Series)
AVAILABLE_AI_MODELS = {
    "wd-v1-4-convnext-v2": {
        "id": "wd-v1-4-convnext-v2",
        "name": "🔹 WD14 ConvNeXt V2 (Waifu Diffusion 1.4 - สแตนดาร์ด ConvNeXt V2)",
        "short_name": "WD14 ConvNeXt V2",
        "repo": "SmilingWolf/wd-v1-4-convnextv2-tagger-v2",
        "model_url": "https://huggingface.co/SmilingWolf/wd-v1-4-convnextv2-tagger-v2/resolve/main/model.onnx",
        "tags_url": "https://huggingface.co/SmilingWolf/wd-v1-4-convnextv2-tagger-v2/resolve/main/selected_tags.csv",
        "subdir": "",
        "legacy": True
    },
    "wd-v3-convnext": {
        "id": "wd-v3-convnext",
        "name": "⭐ WD Tagger v3 - ConvNeXt (~10,000+ แท็ก / แนะนำ 🔥)",
        "short_name": "WD v3 ConvNeXt",
        "repo": "SmilingWolf/wd-convnext-tagger-v3",
        "model_url": "https://huggingface.co/SmilingWolf/wd-convnext-tagger-v3/resolve/main/model.onnx",
        "tags_url": "https://huggingface.co/SmilingWolf/wd-convnext-tagger-v3/resolve/main/selected_tags.csv",
        "subdir": "wd_v3_convnext",
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
        "legacy": False
    },
    "wd-v3-ensemble": {
        "id": "wd-v3-ensemble",
        "name": "👑 WD Tagger v3 - Ensemble (รวมพลัง 3 โมเดล: ConvNeXt + ViT + SwinV2)",
        "short_name": "WD v3 Ensemble (3-in-1)",
        "repo": "SmilingWolf/wd-v3-ensemble",
        "model_url": "",
        "tags_url": "",
        "subdir": "wd_v3_ensemble",
        "is_ensemble": True,
        "ensemble_keys": ["wd-v3-convnext", "wd-v3-vit", "wd-v3-swinv2"],
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
    VERSION = "2.3.4"