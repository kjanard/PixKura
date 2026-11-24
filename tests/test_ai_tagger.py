import os
import sys
import unittest
import sqlite3
import tempfile
import numpy as np
from PIL import Image

# Ensure workspace root is in sys.path
WORKSPACE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKSPACE_DIR not in sys.path:
    sys.path.insert(0, WORKSPACE_DIR)

ICON_JPG = os.path.join(WORKSPACE_DIR, "icon.jpg")

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt

from config import (
    DB_FILE, DEFAULT_TAGGER_MODEL, DEFAULT_TAGS_CSV,
    DEFAULT_CHARACTER_THRESHOLD, DEFAULT_GENERAL_THRESHOLD
)
from database import DatabaseSetup, get_file_tags, get_all_characters, get_all_series, get_tag_stats, get_all_search_suggestions
from tagger import WD14Tagger
from workers import AiTaggingWorker, StreamScanner
from ai_tag_dialog import AiTagDialog
from main import PixivManagerApp

# Create single QApplication instance for GUI tests
_app = QApplication.instance()
if _app is None:
    _app = QApplication(sys.argv)

class TestWD14Tagger(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tagger = WD14Tagger(use_gpu=True)
        if WD14Tagger.is_model_installed():
            cls.tagger.load_model()

    def test_01_provider_detection(self):
        providers = WD14Tagger.get_available_providers()
        self.assertIn("all_providers", providers)
        self.assertTrue(providers["has_directml"], "DirectML GPU should be supported on this system.")
        self.assertTrue(providers["has_gpu"])

    def test_02_model_installed(self):
        self.assertTrue(WD14Tagger.is_model_installed(), "WD14 model files should be present in models/ directory.")

    def test_03_load_tags(self):
        self.tagger.load_tags()
        self.assertGreater(len(self.tagger.tags_data), 5000, "Should have loaded at least 5,000 tags.")
        self.assertGreater(len(self.tagger.char_indices), 1000, "Should have loaded at least 1,000 character tags.")
        self.assertGreater(len(self.tagger.general_indices), 3000, "Should have loaded general tags.")

    def test_04_preprocess_image_modes(self):
        # 1. RGB
        img_rgb = Image.new("RGB", (600, 800), (255, 0, 0))
        arr_rgb = self.tagger.preprocess_image(img_rgb, target_size=448)
        self.assertEqual(arr_rgb.shape, (1, 448, 448, 3))
        self.assertEqual(arr_rgb.dtype, np.float32)

        # 2. RGBA (with transparency)
        img_rgba = Image.new("RGBA", (800, 600), (0, 255, 0, 128))
        arr_rgba = self.tagger.preprocess_image(img_rgba, target_size=448)
        self.assertEqual(arr_rgba.shape, (1, 448, 448, 3))

        # 3. Grayscale (L)
        img_l = Image.new("L", (448, 448), 128)
        arr_l = self.tagger.preprocess_image(img_l, target_size=448)
        self.assertEqual(arr_l.shape, (1, 448, 448, 3))

    def test_05_predict_real_image(self):
        if not os.path.exists(ICON_JPG):
            self.skipTest("icon.jpg not found for real inference test")

        res = self.tagger.predict(ICON_JPG, char_threshold=0.30, gen_threshold=0.30)
        self.assertIsNotNone(res)
        self.assertIn("characters", res)
        self.assertIn("series", res)
        self.assertIn("general", res)
        self.assertIn("rating", res)

    def test_06_decoupled_tensor_pipeline(self):
        if not os.path.exists(ICON_JPG):
            self.skipTest("icon.jpg not found for real inference test")

        tensor = self.tagger.prepare_media_tensor(ICON_JPG)
        self.assertIsNotNone(tensor)
        self.assertEqual(tensor.shape, (1, 448, 448, 3))
        self.assertEqual(tensor.dtype, np.float32)

        res = self.tagger.predict_tensor(tensor, char_threshold=0.30, gen_threshold=0.30)
        self.assertIsNotNone(res)
        self.assertIn("all_tags", res)
        self.assertGreater(len(res["all_tags"]), 0)
        self.assertIsInstance(res["general"], list)
        self.assertGreater(len(res["general"]), 0, "Should detect at least 1 general tag for icon.jpg")

    def test_07_predict_nonexistent_or_corrupt_file(self):
        res = self.tagger.predict("non_existent_image_12345.png")
        self.assertIsNone(res, "Predicting non-existent file should return None gracefully")

    def test_08_fp16_model_inference(self):
        if not os.path.exists(ICON_JPG):
            self.skipTest("icon.jpg not found for real inference test")
        
        tagger_fp16 = WD14Tagger(use_gpu=True, use_fp16=True)
        tagger_fp16.load_model()
        self.assertTrue(tagger_fp16.use_fp16)
        
        tensor = tagger_fp16.prepare_media_tensor(ICON_JPG)
        self.assertIsNotNone(tensor)
        self.assertEqual(tensor.dtype, np.float16)

        res = tagger_fp16.predict_tensor(tensor, char_threshold=0.30, gen_threshold=0.30)
        self.assertIsNotNone(res)
        self.assertIn("all_tags", res)
        self.assertGreater(len(res["all_tags"]), 0)

    def test_09_multi_model_registry(self):
        from tagger import get_model_paths
        from config import AVAILABLE_AI_MODELS
        self.assertIn("wd-v1-4-convnext-v2", AVAILABLE_AI_MODELS)
        self.assertIn("wd-v3-convnext", AVAILABLE_AI_MODELS)
        self.assertIn("wd-v3-vit", AVAILABLE_AI_MODELS)
        self.assertIn("wd-v3-swinv2", AVAILABLE_AI_MODELS)
        self.assertIn("wd-v3-eva02-large", AVAILABLE_AI_MODELS)
        self.assertIn("wd-v3-vit-large", AVAILABLE_AI_MODELS)
        self.assertIn("pixai-tagger-v0.9", AVAILABLE_AI_MODELS)
        self.assertIn("camie-tagger-v2", AVAILABLE_AI_MODELS)
        self.assertIn("wd-v3-ensemble", AVAILABLE_AI_MODELS)
        self.assertIn("wd-v3-ultra-ensemble", AVAILABLE_AI_MODELS)
        
        paths = get_model_paths("wd-v3-convnext")
        self.assertIn("wd_v3_convnext", paths["dir"])
        self.assertTrue(paths["model_fp32"].endswith("model.onnx"))

        paths_eva = get_model_paths("wd-v3-eva02-large")
        self.assertIn("wd_v3_eva02_large", paths_eva["dir"])
        self.assertTrue(paths_eva["model_fp32"].endswith("model.onnx"))

        paths_pixai = get_model_paths("pixai-tagger-v0.9")
        self.assertIn("pixai_tagger_v0_9", paths_pixai["dir"])
        self.assertTrue(paths_pixai["tags_csv"].endswith("selected_tags.csv"))
        self.assertEqual(paths_pixai["meta"]["preprocess_mode"], "pixai")

        paths_camie = get_model_paths("camie-tagger-v2")
        self.assertIn("camie_tagger_v2", paths_camie["dir"])
        self.assertTrue(paths_camie["tags_csv"].endswith("camie-tagger-v2-metadata.json"))
        self.assertEqual(paths_camie["meta"]["preprocess_mode"], "camie")
        self.assertEqual(paths_camie["meta"]["output_index"], 1)

    def test_11_pixai_v09_csv_ips_mapping_and_preprocess(self):
        import json
        from workers import decode_image_cv2_for_wd14

        fd, csv_path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        fd_img, img_path = tempfile.mkstemp(suffix=".png")
        os.close(fd_img)
        try:
            with open(csv_path, "w", encoding="utf-8", newline="") as f:
                f.write("id,tag_id,name,category,count,ips\n")
                f.write("0,470575,1girl,0,6893309,[]\n")
                f.write("1,12345,nude,0,500000,[]\n")
                f.write('2,1478495,artoria_pendragon_(fate),4,41941,"[""fate_(series)"", ""fate/stay_night""]"\n')

            tagger = WD14Tagger(model_key="pixai-tagger-v0.9", tags_path=csv_path, use_gpu=False)
            tagger.load_tags()
            self.assertEqual(len(tagger.tags_data), 3)
            self.assertTrue(tagger.has_ips_mapping)
            self.assertEqual(tagger.tags_data[2]["ips"], ["fate_(series)", "fate/stay_night"])

            # Verify automatic Series/IP extraction + fallback rating inference
            probs = np.array([0.95, 0.10, 0.88], dtype=np.float32)
            res = tagger._extract_tags_from_probs(probs, char_threshold=0.35, gen_threshold=0.35)
            self.assertEqual(res["characters"][0][0], "artoria_pendragon_(fate)")
            series_names = [s[0] for s in res["series"]]
            self.assertIn("fate_(series)", series_names)
            self.assertIn("fate/stay_night", series_names)
            self.assertEqual(res["rating"], "general")

            # Verify PixAI official character threshold calibration (0.85 floor blocks 0.60 false positives)
            probs_fp = np.array([0.95, 0.10, 0.60], dtype=np.float32)
            res_fp = tagger._extract_tags_from_probs(probs_fp, char_threshold=0.35, gen_threshold=0.35)
            self.assertEqual(res_fp["characters"], [])

            # Verify explicit rating fallback when NSFW general tag fires
            probs_nsfw = np.array([0.95, 0.90, 0.10], dtype=np.float32)
            res_nsfw = tagger._extract_tags_from_probs(probs_nsfw, char_threshold=0.35, gen_threshold=0.35)
            self.assertEqual(res_nsfw["rating"], "explicit")

            # Verify PIL & OpenCV preprocessing match NCHW [1, 3, 448, 448] in [-1, 1]
            img = Image.new("RGB", (300, 200), (255, 0, 128))
            img.save(img_path)
            arr_pil = tagger.preprocess_image(img, target_size=448)
            self.assertEqual(arr_pil.shape, (1, 3, 448, 448))
            self.assertAlmostEqual(float(arr_pil[0, 0, 0, 0]), 1.0, places=3)   # R=255 -> +1.0
            self.assertAlmostEqual(float(arr_pil[0, 1, 0, 0]), -1.0, places=3)  # G=0 -> -1.0

            arr_cv2 = decode_image_cv2_for_wd14(img_path, target_size=448, target_dtype=np.float32, nchw=True, preprocess_mode="pixai")
            self.assertIsNotNone(arr_cv2)
            self.assertEqual(arr_cv2.shape, (1, 3, 448, 448))
            self.assertTrue(np.allclose(arr_pil, arr_cv2, atol=1e-2))
        finally:
            for p in (csv_path, img_path):
                if os.path.exists(p):
                    try: os.remove(p)
                    except OSError: pass

    def test_12_camie_v2_json_metadata_and_preprocess(self):
        import json
        from workers import decode_image_cv2_for_wd14

        fd, json_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        fd_img, img_path = tempfile.mkstemp(suffix=".png")
        os.close(fd_img)
        try:
            mock_meta = {
                "model_info": {"img_size": 512},
                "dataset_info": {
                    "total_tags": 5,
                    "tag_mapping": {
                        "idx_to_tag": {
                            "0": "year_2025",
                            "1": "hatsune_miku",
                            "2": "vocaloid",
                            "3": "twintails",
                            "4": "rating_sensitive"
                        },
                        "tag_to_category": {
                            "year_2025": "year",
                            "hatsune_miku": "character",
                            "vocaloid": "copyright",
                            "twintails": "general",
                            "rating_sensitive": "rating"
                        }
                    }
                }
            }
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(mock_meta, f)

            tagger = WD14Tagger(model_key="camie-tagger-v2", tags_path=json_path, use_gpu=False)
            tagger.load_tags()
            self.assertEqual(len(tagger.tags_data), 5)
            self.assertEqual(tagger.char_indices, [1])
            self.assertEqual(tagger.series_indices, [2])
            self.assertEqual(tagger.general_indices, [3])
            self.assertEqual(tagger.rating_indices, [4])
            self.assertEqual(tagger.tags_data[4]["name"], "sensitive")

            # Raw logits (Camie v2 outputs unactivated logits) -> sigmoid applied automatically
            raw_logits = np.array([5.0, 4.0, 3.0, 2.0, 3.5], dtype=np.float32)
            res = tagger._extract_tags_from_probs(raw_logits, char_threshold=0.35, gen_threshold=0.35)
            self.assertEqual(res["characters"][0][0], "hatsune_miku")
            self.assertEqual(res["series"][0][0], "vocaloid")
            self.assertEqual(res["general"][0][0], "twintails")
            self.assertEqual(res["rating"], "sensitive")
            # Ensure 'year_2025' (category -1) is NOT leaked into general/characters/series
            all_names = [t[0] for t in res["all_tags"]]
            self.assertNotIn("year_2025", all_names)

            # Verify PIL & OpenCV preprocessing match NCHW [1, 3, 512, 512] ImageNet normalized
            img = Image.new("RGB", (512, 512), (255, 255, 255))
            img.save(img_path)
            arr_pil = tagger.preprocess_image(img, target_size=512)
            self.assertEqual(arr_pil.shape, (1, 3, 512, 512))
            self.assertAlmostEqual(float(arr_pil[0, 0, 0, 0]), (1.0 - 0.485) / 0.229, places=3)

            arr_cv2 = decode_image_cv2_for_wd14(img_path, target_size=512, target_dtype=np.float32, nchw=True, preprocess_mode="camie")
            self.assertIsNotNone(arr_cv2)
            self.assertEqual(arr_cv2.shape, (1, 3, 512, 512))
            self.assertTrue(np.allclose(arr_pil, arr_cv2, atol=1e-2))
        finally:
            for p in (json_path, img_path):
                if os.path.exists(p):
                    try: os.remove(p)
                    except OSError: pass

    def test_10_ensemble_registry_and_check(self):
        from tagger import get_model_paths, WD14Tagger
        paths = get_model_paths("wd-v3-ensemble")
        self.assertTrue(paths["meta"]["is_ensemble"])
        self.assertEqual(len(paths["meta"]["ensemble_keys"]), 3)
        
        # Test standard ensemble tagger initialization
        tagger = WD14Tagger("wd-v3-ensemble")
        self.assertTrue(tagger.is_ensemble)
        self.assertEqual(tagger.ensemble_keys, ["wd-v3-convnext", "wd-v3-vit", "wd-v3-swinv2"])

        # Test ultra ensemble tagger initialization
        paths_ultra = get_model_paths("wd-v3-ultra-ensemble")
        self.assertTrue(paths_ultra["meta"]["is_ensemble"])
        self.assertEqual(paths_ultra["meta"]["ensemble_keys"], ["wd-v3-eva02-large", "wd-v3-swinv2", "wd-v3-convnext"])
        tagger_ultra = WD14Tagger("wd-v3-ultra-ensemble")
        self.assertTrue(tagger_ultra.is_ensemble)
        self.assertFalse(tagger_ultra.is_heterogeneous)
        self.assertEqual(tagger_ultra.ensemble_keys, ["wd-v3-eva02-large", "wd-v3-swinv2", "wd-v3-convnext"])

        # Test All Best Model in One (heterogeneous 3-in-1)
        paths_all_best = get_model_paths("all-best-ensemble")
        self.assertTrue(paths_all_best["meta"]["is_ensemble"])
        self.assertTrue(paths_all_best["meta"]["is_heterogeneous"])
        self.assertEqual(
            paths_all_best["meta"]["ensemble_keys"],
            ["wd-v3-eva02-large", "pixai-tagger-v0.9", "camie-tagger-v2"]
        )
        tagger_all_best = WD14Tagger("all-best-ensemble")
        self.assertTrue(tagger_all_best.is_ensemble)
        self.assertTrue(tagger_all_best.is_heterogeneous)
        specs = tagger_all_best.get_sub_preprocess_specs()
        self.assertEqual(len(specs), 3)
        self.assertEqual([s[4] for s in specs], ["wd", "pixai", "camie"])
        self.assertEqual([s[1] for s in specs], [448, 448, 512])

    def test_13_all_best_heterogeneous_fusion_and_multi_decode(self):
        from workers import decode_image_cv2_multi

        # 1. Test single-decode OpenCV multi-tensor generation for All Best 3-in-1
        fd_img, img_path = tempfile.mkstemp(suffix=".png")
        os.close(fd_img)
        try:
            img = Image.new("RGB", (600, 400), (200, 100, 50))
            img.save(img_path)

            all_best = WD14Tagger("all-best-ensemble", use_gpu=False)
            specs = all_best.get_sub_preprocess_specs()
            cv2_dict = decode_image_cv2_multi(img_path, specs)
            self.assertIsNotNone(cv2_dict)
            self.assertEqual(cv2_dict["wd-v3-eva02-large"].shape, (1, 448, 448, 3))
            self.assertEqual(cv2_dict["pixai-tagger-v0.9"].shape, (1, 3, 448, 448))
            self.assertEqual(cv2_dict["camie-tagger-v2"].shape, (1, 3, 512, 512))

            pil_dict = all_best.prepare_media_tensor(img_path)
            self.assertIsNotNone(pil_dict)
            self.assertEqual(pil_dict["wd-v3-eva02-large"].shape, (1, 448, 448, 3))
            self.assertEqual(pil_dict["pixai-tagger-v0.9"].shape, (1, 3, 448, 448))
            self.assertEqual(pil_dict["camie-tagger-v2"].shape, (1, 3, 512, 512))
        finally:
            if os.path.exists(img_path):
                try: os.remove(img_path)
                except OSError: pass

        # 2. Test cross-vocabulary fusion (union + consensus boost + explicit rating priority)
        res_eva = {
            "characters": [("hatsune_miku", 0.85)],
            "series": [("vocaloid", 0.90)],
            "general": [("1girl", 0.95), ("twintails", 0.80)],
            "rating": "sensitive",
            "all_tags": [("hatsune_miku", 4, 0.85), ("vocaloid", 3, 0.90), ("1girl", 0, 0.95), ("twintails", 0, 0.80), ("sensitive", 5, 0.88)]
        }
        res_pixai = {
            "characters": [("hatsune_miku", 0.90), ("new_2026_char", 0.89)],
            "series": [("vocaloid", 0.90), ("new_2026_game", 0.89)],
            "general": [("1girl", 0.96)],
            "rating": "general",  # fallback rating (explicit flag=False)
            "all_tags": [("hatsune_miku", 4, 0.90), ("new_2026_char", 4, 0.89), ("vocaloid", 3, 0.90), ("new_2026_game", 3, 0.89), ("1girl", 0, 0.96), ("general", 5, 0.90)]
        }
        res_camie = {
            "characters": [("hatsune_miku", 0.88)],
            "series": [("vocaloid", 0.87)],
            "general": [("1girl", 0.94), ("rare_camie_detail", 0.72)],
            "rating": "sensitive",
            "all_tags": [("hatsune_miku", 4, 0.88), ("vocaloid", 3, 0.87), ("1girl", 0, 0.94), ("rare_camie_detail", 0, 0.72), ("sensitive", 5, 0.82)]
        }
        merged = WD14Tagger._merge_heterogeneous_results(
            [res_eva, res_pixai, res_camie],
            has_explicit_rating_flags=[True, False, True]
        )
        char_map = dict(merged["characters"])
        series_map = dict(merged["series"])
        gen_map = dict(merged["general"])
        # Consensus tag ranks highest (0.90 + 2*0.02 = 0.94)
        self.assertAlmostEqual(char_map["hatsune_miku"], 0.94, places=3)
        # Brand-new character known only to PixAI is preserved with full confidence!
        self.assertAlmostEqual(char_map["new_2026_char"], 0.89, places=3)
        self.assertIn("new_2026_game", series_map)
        # Rare Camie tag is preserved!
        self.assertAlmostEqual(gen_map["rare_camie_detail"], 0.72, places=3)
        # Explicit rating heads (EVA02 + Camie) win over PixAI's fallback rating
        self.assertEqual(merged["rating"], "sensitive")


class TestDatabaseLayer(unittest.TestCase):
    def setUp(self):
        # Use a temporary SQLite database for clean tests
        self.temp_db_fd, self.temp_db_path = tempfile.mkstemp(suffix=".db")
        self.db_setup = DatabaseSetup(self.temp_db_path)

    def tearDown(self):
        os.close(self.temp_db_fd)
        if os.path.exists(self.temp_db_path):
            try: os.remove(self.temp_db_path)
            except: pass

    def test_01_file_tags_schema_and_indexes(self):
        conn = sqlite3.connect(self.temp_db_path)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='file_tags'")
        self.assertEqual(cur.fetchone()[0], "file_tags")

        cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='file_tags'")
        indexes = {row[0] for row in cur.fetchall()}
        self.assertIn("idx_tag_name", indexes)
        self.assertIn("idx_tag_category", indexes)
        self.assertIn("idx_file_tags_path", indexes)
        conn.close()

    def test_02_insert_and_get_file_tags(self):
        conn = sqlite3.connect(self.temp_db_path)
        cur = conn.cursor()
        cur.execute("INSERT INTO file_tags (path, tag_name, category, confidence) VALUES (?, ?, ?, ?)",
                    ("/images/test1.png", "hatsune_miku", 4, 0.98))
        cur.execute("INSERT INTO file_tags (path, tag_name, category, confidence) VALUES (?, ?, ?, ?)",
                    ("/images/test1.png", "vocaloid", 3, 0.95))
        cur.execute("INSERT INTO file_tags (path, tag_name, category, confidence) VALUES (?, ?, ?, ?)",
                    ("/images/test1.png", "twintails", 0, 0.85))
        conn.commit()
        conn.close()

        tags = get_file_tags(self.temp_db_path, "/images/test1.png")
        self.assertEqual(len(tags["characters"]), 1)
        self.assertEqual(tags["characters"][0][0], "hatsune_miku")
        self.assertEqual(tags["characters"][0][1], 0.98)
        self.assertEqual(len(tags["series"]), 1)
        self.assertEqual(tags["series"][0][0], "vocaloid")
        self.assertEqual(len(tags["general"]), 1)
        self.assertEqual(tags["general"][0][0], "twintails")

    def test_03_character_and_series_aggregation(self):
        conn = sqlite3.connect(self.temp_db_path)
        cur = conn.cursor()
        # Insert multiple character tags
        cur.executemany("INSERT INTO file_tags (path, tag_name, category, confidence) VALUES (?, ?, ?, ?)", [
            ("/img1.jpg", "reimu_hakurei", 4, 0.9),
            ("/img2.jpg", "reimu_hakurei", 4, 0.88),
            ("/img3.jpg", "marisa_kirisame", 4, 0.92),
            ("/img1.jpg", "touhou", 3, 0.95),
            ("/img2.jpg", "touhou", 3, 0.95),
        ])
        conn.commit()
        conn.close()

        chars = get_all_characters(self.temp_db_path)
        self.assertEqual(chars, ["reimu_hakurei", "marisa_kirisame"])

        series = get_all_series(self.temp_db_path)
        self.assertEqual(series, ["touhou"])

        stats = get_tag_stats(self.temp_db_path)
        self.assertEqual(stats["total_tagged_files"], 3)
        self.assertEqual(stats["unique_characters"], 2)
        self.assertEqual(stats["unique_tags"], 3)

class TestAiTaggingWorker(unittest.TestCase):
    def setUp(self):
        self.temp_db_fd, self.temp_db_path = tempfile.mkstemp(suffix=".db")
        self.db_setup = DatabaseSetup(self.temp_db_path)

        # Seed file_index with ICON_JPG
        conn = sqlite3.connect(self.temp_db_path)
        cur = conn.cursor()
        cur.execute("INSERT INTO file_index (path, filename, parent_folder, mtime, size) VALUES (?, ?, ?, ?, ?)",
                    (ICON_JPG, "icon.jpg", WORKSPACE_DIR, 1000.0, 1024))
        conn.commit()
        conn.close()

    def tearDown(self):
        os.close(self.temp_db_fd)
        if os.path.exists(self.temp_db_path):
            try: os.remove(self.temp_db_path)
            except: pass

    def test_worker_tagging_execution(self):
        worker = AiTaggingWorker(
            root_path=None,  # Uses seeded file_index in self.temp_db_path directly
            db_file=self.temp_db_path,
            use_gpu=True,
            char_threshold=0.30,
            gen_threshold=0.30,
            rescan_existing=False
        )

        progress_emitted = []
        worker.signals.progress.connect(lambda cur, tot, fn, fps, eta: progress_emitted.append((cur, tot, fn)))
        
        finished_results = []
        worker.signals.finished.connect(lambda tagged, err, el: finished_results.append((tagged, err, el)))

        # Run synchronously for unit test verification
        worker.run()

        self.assertGreater(len(progress_emitted), 0, "Worker should emit progress signals")
        self.assertEqual(len(finished_results), 1, "Worker should emit finished signal once")
        self.assertGreaterEqual(finished_results[0][0], 1, "Should have tagged at least 1 file")

        # Verify tags were written to temp DB
        tags = get_file_tags(self.temp_db_path, ICON_JPG)
        self.assertGreater(len(tags["all"]), 0, "Tags should be saved in SQLite database")

    def test_worker_pause_resume_stop(self):
        worker = AiTaggingWorker(
            root_path=None,
            db_file=self.temp_db_path,
            use_gpu=True
        )
        self.assertFalse(worker.is_paused)
        worker.pause()
        self.assertTrue(worker.is_paused)
        worker.resume()
        self.assertFalse(worker.is_paused)
        worker.stop()
        self.assertFalse(worker.running)

    def test_worker_rescan_unknown_characters(self):
        worker1 = AiTaggingWorker(
            root_path=None,
            db_file=self.temp_db_path,
            use_gpu=False,
            rescan_existing=False,
            rescan_unknown_chars=False
        )
        self.assertFalse(worker1.rescan_unknown_chars)

        worker2 = AiTaggingWorker(
            root_path=None,
            db_file=self.temp_db_path,
            use_gpu=False,
            rescan_existing=False,
            rescan_unknown_chars=True
        )
        self.assertTrue(worker2.rescan_unknown_chars)


class TestThreadLocalConnLeak(unittest.TestCase):
    """Regression: QThreadPool runnables used to open a brand-new SQLite connection on every run
    (threading.local is wiped on Qt pool threads), leaking handles and commit memory without bound."""

    def setUp(self):
        import workers
        workers.close_all_thread_local_conns()
        self.temp_db_fd, self.temp_db_path = tempfile.mkstemp(suffix=".db")
        DatabaseSetup(self.temp_db_path)

    def tearDown(self):
        import workers
        workers.close_all_thread_local_conns()
        os.close(self.temp_db_fd)
        for suffix in ("", "-wal", "-shm"):
            try: os.remove(self.temp_db_path + suffix)
            except Exception: pass

    def test_qthreadpool_reuses_connections(self):
        import threading
        import workers
        from PyQt6.QtCore import QRunnable, QThreadPool

        db_path = self.temp_db_path
        seen = []
        lock = threading.Lock()

        class Task(QRunnable):
            def run(self):
                conn = workers.get_thread_local_conn(db_path)
                conn.execute("SELECT 1").fetchone()
                with lock:
                    seen.append(id(conn))

        pool = QThreadPool()
        pool.setMaxThreadCount(3)
        for _ in range(300):
            pool.start(Task())
        pool.waitForDone()

        self.assertEqual(len(seen), 300)
        self.assertLessEqual(len(set(seen)), 3, "Pool threads must reuse one connection each")
        conns_for_db = [c for (t_ident, db), c in workers._conns_by_thread.items() if db == db_path]
        self.assertLessEqual(len(conns_for_db), 3)

    def test_same_thread_gets_same_connection_and_close_all_resets(self):
        import workers
        c1 = workers.get_thread_local_conn(self.temp_db_path)
        c2 = workers.get_thread_local_conn(self.temp_db_path)
        self.assertIs(c1, c2)
        workers.close_all_thread_local_conns()
        c3 = workers.get_thread_local_conn(self.temp_db_path)
        self.assertIsNot(c1, c3)
        c3.execute("SELECT 1").fetchone()

class TestStreamScannerFiltering(unittest.TestCase):
    def setUp(self):
        self.temp_db_fd, self.temp_db_path = tempfile.mkstemp(suffix=".db")
        self.db_setup = DatabaseSetup(self.temp_db_path)

        conn = sqlite3.connect(self.temp_db_path)
        cur = conn.cursor()
        # Seed files
        cur.execute("INSERT INTO file_index (path, filename, size, mtime) VALUES (?, ?, ?, ?)",
                    ("D:\\art\\miku1.png", "miku1.png", 2000, 1000.0))
        cur.execute("INSERT INTO file_index (path, filename, size, mtime) VALUES (?, ?, ?, ?)",
                    ("D:\\art\\other2.png", "other2.png", 5000, 1000.0))
        cur.execute("INSERT INTO file_index (path, filename, size, mtime) VALUES (?, ?, ?, ?)",
                    ("D:\\art\\miku2.jpg", "miku2.jpg", 10000000, 2000.0))
        
        # Tag miku1.png and miku2.jpg
        cur.execute("INSERT INTO file_tags (path, tag_name, category, confidence) VALUES (?, ?, ?, ?)",
                    ("D:\\art\\miku1.png", "hatsune_miku", 4, 0.99))
        cur.execute("INSERT INTO file_tags (path, tag_name, category, confidence) VALUES (?, ?, ?, ?)",
                    ("D:\\art\\miku2.jpg", "hatsune_miku", 4, 0.95))
        conn.commit()
        conn.close()

    def tearDown(self):
        os.close(self.temp_db_fd)
        if os.path.exists(self.temp_db_path):
            try: os.remove(self.temp_db_path)
            except: pass

    def test_tag_filter_matching(self):
        scanner = StreamScanner(
            root="D:\\art",
            db_file=self.temp_db_path,
            gen_id=1,
            use_cache=True,
            tag_filter="hatsune_miku"
        )
        
        results = []
        scanner.signals.scan_batch.connect(lambda gid, batch: results.extend(batch))
        scanner.run()

        # Should only match miku1.png and miku2.jpg
        matched_paths = [r[0] for r in results]
        self.assertIn("D:\\art\\miku1.png", matched_paths)
        self.assertIn("D:\\art\\miku2.jpg", matched_paths)
        self.assertNotIn("D:\\art\\other2.png", matched_paths)

    def test_combined_tag_and_size_filter(self):
        # Filter for tag "hatsune_miku" AND max_size < 10000 bytes -> should only return miku1.png
        scanner = StreamScanner(
            root="D:\\art",
            db_file=self.temp_db_path,
            gen_id=1,
            use_cache=True,
            tag_filter="hatsune_miku",
            max_size=5000
        )
        results = []
        scanner.signals.scan_batch.connect(lambda gid, batch: results.extend(batch))
        scanner.run()

        matched_paths = [r[0] for r in results]
        self.assertIn("D:\\art\\miku1.png", matched_paths)
        self.assertNotIn("D:\\art\\miku2.jpg", matched_paths)
        self.assertNotIn("D:\\art\\other2.png", matched_paths)

    def test_cache_fast_path_and_cancel_never_deletes_index(self):
        # 1. Cache-only unfiltered scan (like clicking Clear) completes immediately without disk walk
        scanner = StreamScanner(
            root="D:\\art",
            db_file=self.temp_db_path,
            gen_id=2,
            use_cache=True,
            sync_disk=False
        )
        results = []
        finished_counts = []
        scanner.signals.scan_batch.connect(lambda gid, batch: results.extend(batch))
        scanner.signals.scan_finished.connect(lambda gid, cnt: finished_counts.append(cnt))
        scanner.run()
        self.assertEqual(len(results), 3)
        self.assertEqual(finished_counts, [3])

        # 2. Cancelled sync_disk scan MUST NOT delete existing DB rows
        scanner_cancel = StreamScanner(
            root="D:\\art",
            db_file=self.temp_db_path,
            gen_id=3,
            use_cache=True,
            sync_disk=True
        )
        scanner_cancel.stop()
        scanner_cancel.run()

        conn = sqlite3.connect(self.temp_db_path)
        cnt_files = conn.execute("SELECT COUNT(*) FROM file_index").fetchone()[0]
        cnt_tags = conn.execute("SELECT COUNT(*) FROM file_tags").fetchone()[0]
        conn.close()
        self.assertEqual(cnt_files, 3)
        self.assertEqual(cnt_tags, 2)

class TestAiTagDialogUI(unittest.TestCase):
    def test_dialog_components(self):
        dialog = AiTagDialog()
        self.assertIsNotNone(dialog.btn_start)
        self.assertIsNotNone(dialog.btn_pause)
        self.assertIsNotNone(dialog.btn_stop)
        self.assertIsNotNone(dialog.combo_provider)
        self.assertIsNotNone(dialog.combo_scan_mode)
        self.assertIsNotNone(dialog.combo_batch)
        self.assertEqual(dialog.combo_scan_mode.itemData(0), False) # Default is Quick Mode (rescan=False)
        self.assertEqual(dialog.combo_scan_mode.itemData(1), True)  # Full Re-scan (rescan=True)
        self.assertIsNotNone(dialog.slider_thresh)
        self.assertIsNotNone(dialog.progress_bar)

        self.assertIsNotNone(dialog.chk_unknown_chars)
        self.assertTrue(dialog.chk_unknown_chars.isEnabled())
        # Changing to Full Re-scan should disable chk_unknown_chars
        dialog.combo_scan_mode.setCurrentIndex(1)
        self.assertFalse(dialog.chk_unknown_chars.isEnabled())
        # Changing back to Quick Mode should re-enable it
        dialog.combo_scan_mode.setCurrentIndex(0)
        self.assertTrue(dialog.chk_unknown_chars.isEnabled())

        # Check default DirectML selection
        self.assertTrue(dialog.combo_provider.count() >= 2)
        self.assertTrue(dialog.combo_provider.itemData(0)) # First item should be GPU
        dialog.close()

class TestMainWindowAIIntegration(unittest.TestCase):
    def test_main_window_has_ai_tagger(self):
        window = PixivManagerApp()
        self.assertTrue(hasattr(window, 'btn_ai_tag'), "PixivManagerApp should have btn_ai_tag")
        self.assertTrue(hasattr(window, 'btn_favorites'), "PixivManagerApp should have btn_favorites")
        self.assertTrue(hasattr(window, 'btn_duplicates'), "PixivManagerApp should have btn_duplicates")
        self.assertTrue(hasattr(window, 'btn_export_dataset'), "PixivManagerApp should have btn_export_dataset")
        self.assertTrue(hasattr(window, 'char_filter'), "PixivManagerApp should have char_filter dropdown")
        self.assertTrue(hasattr(window, 'search_completer'), "PixivManagerApp should have search_completer")
        self.assertEqual(window.char_filter.itemText(0), "All Characters")

        # Test refresh_character_filters and search_completer model update
        window.refresh_character_filters()
        self.assertGreaterEqual(window.char_filter.count(), 1)
        self.assertIsNotNone(window.search_completer.model())
        window.close()

    def test_get_all_search_suggestions_helper(self):
        temp_db_fd, temp_db_path = tempfile.mkstemp(suffix=".db")
        DatabaseSetup(temp_db_path)
        conn = sqlite3.connect(temp_db_path)
        cur = conn.cursor()
        cur.execute("INSERT INTO file_tags VALUES ('/p1.png', 'hatsune_miku', 4, 0.99)")
        cur.execute("INSERT INTO file_tags VALUES ('/p1.png', 'blue_hair', 0, 0.95)")
        conn.commit()
        conn.close()

        suggestions = get_all_search_suggestions(temp_db_path)
        self.assertIn("tag:hatsune_miku", suggestions)
        self.assertIn("char:hatsune_miku", suggestions)
        self.assertIn("tag:blue_hair", suggestions)
        
        os.close(temp_db_fd)
        if os.path.exists(temp_db_path):
            try: os.remove(temp_db_path)
            except: pass

if __name__ == "__main__":
    unittest.main()

