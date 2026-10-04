import os
import sys
import unittest
import sqlite3
import tempfile
import numpy as np
from PIL import Image

# Ensure workspace root is in sys.path
WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))
if WORKSPACE_DIR not in sys.path:
    sys.path.insert(0, WORKSPACE_DIR)

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
        if not os.path.exists("icon.jpg"):
            self.skipTest("icon.jpg not found for real inference test")

        res = self.tagger.predict("icon.jpg", char_threshold=0.30, gen_threshold=0.30)
        self.assertIsNotNone(res)
        self.assertIn("characters", res)
        self.assertIn("series", res)
        self.assertIn("general", res)
        self.assertIn("rating", res)

    def test_06_decoupled_tensor_pipeline(self):
        if not os.path.exists("icon.jpg"):
            self.skipTest("icon.jpg not found for real inference test")

        tensor = self.tagger.prepare_media_tensor("icon.jpg")
        self.assertIsNotNone(tensor)
        self.assertEqual(tensor.shape, (1, 448, 448, 3))
        self.assertEqual(tensor.dtype, np.float32)

        res = self.tagger.predict_tensor(tensor, char_threshold=0.30, gen_threshold=0.30)
        self.assertIsNotNone(res)
        self.assertIn("all_tags", res)
        self.assertGreater(len(res["all_tags"]), 0)

        self.assertIn("all_tags", res)
        self.assertIsInstance(res["general"], list)
        self.assertGreater(len(res["general"]), 0, "Should detect at least 1 general tag for icon.jpg")

    def test_07_predict_nonexistent_or_corrupt_file(self):
        res = self.tagger.predict("non_existent_image_12345.png")
        self.assertIsNone(res, "Predicting non-existent file should return None gracefully")

    def test_08_fp16_model_inference(self):
        if not os.path.exists("icon.jpg"):
            self.skipTest("icon.jpg not found for real inference test")
        
        tagger_fp16 = WD14Tagger(use_gpu=True, use_fp16=True)
        tagger_fp16.load_model()
        self.assertTrue(tagger_fp16.use_fp16)
        
        tensor = tagger_fp16.prepare_media_tensor("icon.jpg")
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
        self.assertIn("wd-v3-ensemble", AVAILABLE_AI_MODELS)
        
        paths = get_model_paths("wd-v3-convnext")
        self.assertIn("wd_v3_convnext", paths["dir"])
        self.assertTrue(paths["model_fp32"].endswith("model.onnx"))

    def test_10_ensemble_registry_and_check(self):
        from tagger import get_model_paths, WD14Tagger
        paths = get_model_paths("wd-v3-ensemble")
        self.assertTrue(paths["meta"]["is_ensemble"])
        self.assertEqual(len(paths["meta"]["ensemble_keys"]), 3)
        
        # Test ensemble tagger initialization
        tagger = WD14Tagger("wd-v3-ensemble")
        self.assertTrue(tagger.is_ensemble)
        self.assertEqual(tagger.ensemble_keys, ["wd-v3-convnext", "wd-v3-vit", "wd-v3-swinv2"])


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

        # Seed file_index with icon.jpg
        conn = sqlite3.connect(self.temp_db_path)
        cur = conn.cursor()
        cur.execute("INSERT INTO file_index (path, filename, parent_folder, mtime, size) VALUES (?, ?, ?, ?, ?)",
                    (os.path.abspath("icon.jpg"), "icon.jpg", "", 1000.0, 1024))
        conn.commit()
        conn.close()

    def tearDown(self):
        os.close(self.temp_db_fd)
        if os.path.exists(self.temp_db_path):
            try: os.remove(self.temp_db_path)
            except: pass

    def test_worker_tagging_execution(self):
        worker = AiTaggingWorker(
            root_path=os.path.abspath("."),  # Disk walk from current dir to find icon.jpg
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
        tags = get_file_tags(self.temp_db_path, os.path.abspath("icon.jpg"))
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

class TestAiTagDialogUI(unittest.TestCase):
    def test_dialog_components(self):
        dialog = AiTagDialog()
        self.assertIsNotNone(dialog.btn_start)
        self.assertIsNotNone(dialog.btn_pause)
        self.assertIsNotNone(dialog.btn_stop)
        self.assertIsNotNone(dialog.combo_provider)
        self.assertIsNotNone(dialog.combo_scan_mode)
        self.assertEqual(dialog.combo_scan_mode.itemData(0), False) # Default is Quick Mode (rescan=False)
        self.assertEqual(dialog.combo_scan_mode.itemData(1), True)  # Full Re-scan (rescan=True)
        self.assertIsNotNone(dialog.slider_thresh)
        self.assertIsNotNone(dialog.progress_bar)

        # Check default DirectML selection
        self.assertTrue(dialog.combo_provider.count() >= 2)
        self.assertTrue(dialog.combo_provider.itemData(0)) # First item should be GPU
        dialog.close()

class TestMainWindowAIIntegration(unittest.TestCase):
    def test_main_window_has_ai_tagger(self):
        window = PixivManagerApp()
        self.assertTrue(hasattr(window, 'btn_ai_tag'), "PixivManagerApp should have btn_ai_tag")
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

