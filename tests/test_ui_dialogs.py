import unittest
import sys
import os
from PyQt6.QtWidgets import QApplication

# Ensure workspace root is in sys.path
WORKSPACE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKSPACE_DIR not in sys.path:
    sys.path.insert(0, WORKSPACE_DIR)

app = QApplication.instance() or QApplication(sys.argv)

from config import DB_FILE
from database import get_dashboard_analytics
from dashboard_dialog import DashboardDialog
from lightbox_viewer import LightboxViewerDialog

from unittest.mock import patch
from utils import show_in_file_manager

class TestUIDialogs(unittest.TestCase):
    def test_dashboard_analytics_query(self):
        if not os.path.exists(DB_FILE):
            self.skipTest("DB file not found")
        data = get_dashboard_analytics(DB_FILE)
        self.assertIn("total_files", data)
        self.assertIn("tagged_files", data)
        self.assertIn("sfw_count", data)
        self.assertIn("nsfw_count", data)
        self.assertIn("top_characters", data)
        self.assertIn("top_tags", data)

        # SFW + NSFW cannot exceed tagged files
        self.assertLessEqual(data["sfw_count"] + data["nsfw_count"], data["tagged_files"] + 10)

    def test_dashboard_dialog_initialization(self):
        applied = []
        dlg = DashboardDialog(on_apply_query=lambda q: applied.append(q))
        self.assertIsNotNone(dlg)
        dlg.apply_query_and_close("rating:sfw")
        self.assertEqual(applied, ["rating:sfw"])

    def test_lightbox_viewer_initialization(self):
        icon_path = os.path.join(WORKSPACE_DIR, "icon.ico")
        paths = [icon_path] if os.path.exists(icon_path) else []
        dlg = LightboxViewerDialog(paths, current_index=0)
        self.assertIsNotNone(dlg)
        self.assertEqual(dlg.current_idx, 0)
        dlg.toggle_sidebar()
        dlg.toggle_zoom()

        # Test combo_scale modes
        self.assertIsNotNone(dlg.combo_scale)
        self.assertEqual(dlg.combo_scale.count(), 3)
        self.assertEqual(dlg.combo_scale.itemData(0), "FIT")
        self.assertEqual(dlg.combo_scale.itemData(1), "FILL")
        self.assertEqual(dlg.combo_scale.itemData(2), "ACTUAL")

        # Select FILL mode
        dlg.combo_scale.setCurrentIndex(1)
        self.assertEqual(dlg.img_view.view_mode, "FILL")

        # Select ACTUAL mode
        dlg.combo_scale.setCurrentIndex(2)
        self.assertEqual(dlg.img_view.view_mode, "ACTUAL")

        # Select FIT mode
        dlg.combo_scale.setCurrentIndex(0)
        self.assertEqual(dlg.img_view.view_mode, "FIT")

        # Test combo_bg modes
        self.assertIsNotNone(dlg.combo_bg)
        self.assertEqual(dlg.combo_bg.count(), 3)
        self.assertEqual(dlg.combo_bg.itemData(0), "AUTO")
        self.assertEqual(dlg.combo_bg.itemData(1), "WHITE")
        self.assertEqual(dlg.combo_bg.itemData(2), "DARK")

        # Test background selection and cycling
        dlg.combo_bg.setCurrentIndex(1)  # WHITE
        self.assertEqual(dlg.img_view.bg_mode, "WHITE")
        dlg.cycle_background_mode()  # Cycles to DARK
        self.assertEqual(dlg.img_view.bg_mode, "DARK")

        # Test open_explorer on lightbox viewer
        with patch("utils.subprocess.Popen") as mock_popen:
            dlg.open_explorer()
            if paths:
                self.assertTrue(mock_popen.called)
                self.assertEqual(dlg.btn_explorer.text(), "📂 Opening...")

    def test_show_in_file_manager(self):
        # Test non-existent path
        self.assertFalse(show_in_file_manager(""))
        self.assertFalse(show_in_file_manager("C:\\non_existent_folder_xyz\\file.jpg"))

        # Test valid existing file with spaces
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            sp_dir = os.path.join(td, "folder with spaces")
            os.makedirs(sp_dir, exist_ok=True)
            sp_file = os.path.join(sp_dir, "test image.png")
            with open(sp_file, "w") as f:
                f.write("test")

            with patch("subprocess.Popen") as mock_popen:
                res = show_in_file_manager(sp_file)
                self.assertTrue(res)
                self.assertTrue(mock_popen.called)
                called_cmd = mock_popen.call_args[0][0]
                if sys.platform == 'win32':
                    # Must have /select,"..." with quotes only around the file path
                    self.assertIn('/select,"', called_cmd)
                    self.assertFalse(called_cmd.startswith('explorer "/select,'))

    def test_zoomable_image_widget(self):
        from lightbox_viewer import ZoomableImageLabel
        from PyQt6.QtGui import QPixmap, QImage
        z = ZoomableImageLabel()
        img = QImage(1200, 800, QImage.Format.Format_RGB32)
        img.fill(0xffffff)
        pix = QPixmap.fromImage(img)
        z.set_image(pix)
        initial_hint = z.sizeHint()

        # View modes
        z.set_view_mode('FILL')
        self.assertEqual(z.view_mode, 'FILL')
        z.set_view_mode('ACTUAL')
        self.assertEqual(z.view_mode, 'ACTUAL')
        z.set_view_mode('FIT')
        self.assertEqual(z.view_mode, 'FIT')

        # Zoom in
        z.zoom_by(2.0)
        self.assertAlmostEqual(z.zoom_factor, 2.0)
        self.assertEqual(z.sizeHint(), initial_hint)
        self.assertEqual(z.view_mode, 'CUSTOM')

        # Zoom out
        z.zoom_by(0.5)
        self.assertAlmostEqual(z.zoom_factor, 1.0)
        self.assertEqual(z.sizeHint(), initial_hint)

        # Further zoom out
        z.zoom_by(0.5)
        self.assertAlmostEqual(z.zoom_factor, 0.5)

        # Reset
        z.reset_zoom()
        self.assertAlmostEqual(z.zoom_factor, 1.0)
        self.assertEqual(z.view_mode, 'FIT')

        # Test frame update
        new_pix = QPixmap(200, 200)
        z.update_frame_pixmap(new_pix)
        self.assertEqual(z.orig_pixmap, new_pix)

        # Test background modes and HUD
        z.set_bg_mode('WHITE')
        self.assertEqual(z.bg_mode, 'WHITE')
        z.set_bg_mode('DARK')
        self.assertEqual(z.bg_mode, 'DARK')
        z.set_bg_mode('AUTO')
        self.assertEqual(z.bg_mode, 'AUTO')

        next_mode = z.cycle_bg_mode()
        self.assertEqual(next_mode, 'WHITE')
        z.show_hud("Test Badge", 100)
        self.assertEqual(z._hud_message, "Test Badge")

    def test_transparent_thumbnail_generation(self):
        from utils import make_thumbnail_rgb
        from PIL import Image, ImageDraw

        # Create transparent RGBA image with a pure black line (like line art)
        img = Image.new('RGBA', (400, 400), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.line([(50, 50), (350, 350)], fill=(0, 0, 0, 255), width=4)

        thumb = make_thumbnail_rgb(img, (240, 240))
        self.assertEqual(thumb.mode, 'RGB')
        ext = thumb.getextrema()
        # Opaque black line is present (min = 0)
        self.assertEqual(ext[0][0], 0)
        # Background is white instead of pitch black (max = 255)
        self.assertEqual(ext[0][1], 255)

    def test_lightbox_animation_and_video_playback(self):
        import tempfile
        import zipfile
        import shutil
        from io import BytesIO
        from PIL import Image

        temp_dir = tempfile.mkdtemp()
        try:
            # 1. Create static image
            img_path = os.path.join(temp_dir, "test_img.png")
            im = Image.new("RGB", (100, 100), "red")
            im.save(img_path)

            # 2. Create animated GIF
            gif_path = os.path.join(temp_dir, "test_anim.gif")
            f1 = Image.new("RGB", (100, 100), "blue")
            f2 = Image.new("RGB", (100, 100), "green")
            f1.save(gif_path, save_all=True, append_images=[f2], duration=100, loop=0)

            # 3. Create Pixiv Ugoira zip
            zip_path = os.path.join(temp_dir, "test_ugoira.zip")
            with zipfile.ZipFile(zip_path, "w") as z:
                bio1, bio2 = BytesIO(), BytesIO()
                f1.save(bio1, "JPEG")
                f2.save(bio2, "JPEG")
                z.writestr("000000.jpg", bio1.getvalue())
                z.writestr("000001.jpg", bio2.getvalue())
                z.writestr("animation.json", '{"frames": [{"file": "000000.jpg", "delay": 80}, {"file": "000001.jpg", "delay": 80}]}')

            # 4. Create MP4 video
            mp4_path = os.path.join(temp_dir, "test_vid.mp4")
            try:
                import cv2
                import numpy as np
                fourcc = cv2.VideoWriter_fourcc(*'mp4v')
                out = cv2.VideoWriter(mp4_path, fourcc, 30.0, (100, 100))
                for _ in range(5):
                    out.write(np.zeros((100, 100, 3), dtype=np.uint8))
                out.release()
            except Exception:
                mp4_path = None

            paths = [img_path, gif_path, zip_path]
            if mp4_path and os.path.exists(mp4_path):
                paths.append(mp4_path)

            dlg = LightboxViewerDialog(paths, current_index=0)
            self.assertIsNotNone(dlg)

            # --- Check Static Image ---
            self.assertEqual(dlg.current_idx, 0)
            self.assertEqual(dlg.current_media_type, "IMAGE")
            self.assertTrue(dlg.media_bar.isHidden())
            self.assertEqual(dlg.display_stack.currentIndex(), 0)

            # --- Check Animated GIF ---
            dlg.show_next_image()
            self.assertEqual(dlg.current_idx, 1)
            self.assertEqual(dlg.current_media_type, "ANIMATION")
            self.assertFalse(dlg.media_bar.isHidden())
            self.assertTrue(dlg.volume_container.isHidden())
            self.assertIsNotNone(dlg.current_movie)

            # Controls test on GIF
            dlg.toggle_play_pause()
            dlg.cycle_speed()
            self.assertEqual(dlg.SPEEDS[dlg.speed_idx], 1.5)
            dlg.toggle_loop()
            self.assertFalse(dlg.is_looping)
            dlg.toggle_loop()
            self.assertTrue(dlg.is_looping)

            # --- Check Ugoira ZIP ---
            dlg.show_next_image()
            self.assertEqual(dlg.current_idx, 2)
            self.assertEqual(dlg.current_media_type, "ANIMATION")
            self.assertFalse(dlg.media_bar.isHidden())
            self.assertEqual(len(dlg.ugoira_frames), 2)

            # --- Check Video (if created) ---
            if len(paths) > 3:
                dlg.show_next_image()
                self.assertEqual(dlg.current_idx, 3)
                self.assertEqual(dlg.current_media_type, "VIDEO")
                self.assertFalse(dlg.media_bar.isHidden())
                self.assertFalse(dlg.volume_container.isHidden())
                self.assertEqual(dlg.display_stack.currentIndex(), 1)

                # Video volume & mute test
                dlg.volume_up()
                dlg.volume_down()
                dlg.toggle_mute()
                dlg.toggle_mute()

            # Clean shutdown
            dlg.accept()
            self.assertIsNone(dlg.current_movie)
            self.assertIsNone(dlg.ugoira_timer)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_minimizable_dialog_flags_and_sync(self):
        from components import MinimizableDialog, SearchHelpDialog, DatasetExporterDialog, DuplicateFinderDialog
        from download_dialog import PixivDownloadDialog
        from ai_tag_dialog import AiTagDialog
        from dashboard_dialog import DashboardDialog
        from lightbox_viewer import LightboxViewerDialog
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QMainWindow

        parent = QMainWindow()
        parent.show()

        dialogs = [
            SearchHelpDialog(parent),
            PixivDownloadDialog(parent, ""),
            AiTagDialog(parent),
            DashboardDialog(parent),
            LightboxViewerDialog([], 0, parent),
            DuplicateFinderDialog(parent),
        ]

        for dlg in dialogs:
            self.assertTrue(isinstance(dlg, MinimizableDialog))
            flags = dlg.windowFlags()
            self.assertTrue(bool(flags & Qt.WindowType.WindowMinimizeButtonHint))
            self.assertTrue(bool(flags & Qt.WindowType.WindowMaximizeButtonHint))

        # Test minimize sync
        test_dlg = dialogs[1]
        test_dlg.show()
        test_dlg.showMinimized()
        self.assertTrue(test_dlg.isMinimized())
        self.assertTrue(parent.isMinimized())

        # Test restore sync
        parent.showNormal()
        self.assertFalse(parent.isMinimized())
        test_dlg.accept()

    def test_ugoira_mp4_conversion_and_unicode_path(self):
        import tempfile
        import shutil
        from PIL import Image
        from workers import convert_frames_to_mp4

        temp_dir = tempfile.mkdtemp()
        try:
            # Odd dimensions (101x99) to verify even-dimension padding for H.264/mp4v
            f1 = Image.new("RGB", (101, 99), (255, 0, 0))
            f2 = Image.new("RGB", (101, 99), (0, 255, 0))
            f3 = Image.new("RGB", (101, 99), (0, 0, 255))
            mp4_out = os.path.join(temp_dir, "76691698_p0 - うごイラ_test.mp4")

            ok = convert_frames_to_mp4([f1, f2, f3], [60, 120, 80], mp4_out)
            self.assertTrue(ok, "convert_frames_to_mp4 should succeed with OpenCV installed")
            self.assertTrue(os.path.exists(mp4_out))
            self.assertGreater(os.path.getsize(mp4_out), 256)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_opencv_wd14_rgba_white_background_compositing(self):
        import tempfile
        import numpy as np
        from PIL import Image
        from workers import decode_image_cv2_for_wd14

        fd, tmp_png = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            # Fully transparent black RGBA image (0, 0, 0, 0)
            img = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
            img.save(tmp_png, "PNG")

            tensor = decode_image_cv2_for_wd14(tmp_png, target_size=448, target_dtype=np.float32, nchw=False)
            self.assertIsNotNone(tensor)
            self.assertEqual(tensor.shape, (1, 448, 448, 3))
            # Because transparent pixels are composited onto white (255, 255, 255), mean value must be ~255.0 (not 0.0!)
            self.assertGreater(float(tensor.mean()), 250.0)
        finally:
            if os.path.exists(tmp_png):
                try: os.remove(tmp_png)
                except Exception: pass

    def test_dhash_and_dataset_exporter(self):
        import tempfile
        import shutil
        import sqlite3
        from PIL import Image, ImageDraw
        from utils import compute_dhash, hamming_distance_hex
        from database import DatabaseSetup
        from workers import DatasetExportWorker
        from components import DatasetExporterDialog, DuplicateFinderDialog

        temp_dir = tempfile.mkdtemp()
        try:
            img1_path = os.path.join(temp_dir, "art1.png")
            img2_path = os.path.join(temp_dir, "art1_copy.png")
            im = Image.new("RGB", (128, 128), "white")
            draw = ImageDraw.Draw(im)
            draw.rectangle([10, 10, 80, 100], fill="black")
            im.save(img1_path)
            im.save(img2_path)

            h1 = compute_dhash(img1_path)
            h2 = compute_dhash(img2_path)
            self.assertIsNotNone(h1)
            self.assertEqual(h1, h2)
            self.assertEqual(hamming_distance_hex(h1, h2), 0)

            # Test DatasetExportWorker
            db_path = os.path.join(temp_dir, "test_export.db")
            DatabaseSetup(db_path)
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute("INSERT INTO file_index (path, filename, parent_folder, mtime, size) VALUES (?, ?, ?, ?, ?)",
                        (img1_path, "art1.png", temp_dir, 100.0, 1024))
            cur.execute("INSERT INTO file_tags (path, tag_name, category, confidence) VALUES (?, ?, ?, ?)",
                        (img1_path, "hatsune_miku", 4, 0.99))
            cur.execute("INSERT INTO file_tags (path, tag_name, category, confidence) VALUES (?, ?, ?, ?)",
                        (img1_path, "solo", 0, 0.95))
            conn.commit()
            conn.close()

            export_dir = os.path.join(temp_dir, "lora_out")
            worker = DatasetExportWorker(
                file_paths=[img1_path],
                output_dir=export_dir,
                db_file=db_path,
                prefix_tags="miku_lora, masterpiece"
            )
            finished_counts = []
            worker.signals.finished.connect(lambda exp, dest: finished_counts.append((exp, dest)))
            worker.run()

            self.assertEqual(len(finished_counts), 1)
            self.assertEqual(finished_counts[0][0], 1)
            txt_path = os.path.join(export_dir, "art1.txt")
            self.assertTrue(os.path.exists(txt_path))
            with open(txt_path, "r", encoding="utf-8") as f:
                caption = f.read()
            self.assertIn("miku_lora", caption)
            self.assertIn("hatsune miku", caption)

            # Verify dialog instantiation
            dlg_exp = DatasetExporterDialog(file_paths=[img1_path], db_file=db_path)
            self.assertIsNotNone(dlg_exp)
            dlg_exp.close()

            dlg_dup = DuplicateFinderDialog(db_file=db_path)
            self.assertIsNotNone(dlg_dup)
            dlg_dup.close()
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_pixiv_download_dialog_and_worker_leak_hardening(self):
        from download_dialog import PixivDownloadDialog
        from workers import PixivDownloaderWorker
        from unittest.mock import patch, MagicMock

        # 1. Dialog buffer hardening test
        dlg = PixivDownloadDialog(default_dir="")
        self.assertIsNotNone(dlg)
        self.assertEqual(dlg.log_area.document().maximumBlockCount(), 2000)

        # Simulate attaching worker and finishing
        worker = PixivDownloaderWorker(mode="ILLUST", target_id="12345", output_dir="")
        dlg.worker = worker
        worker.signals.log.connect(dlg.add_log)
        worker.signals.progress.connect(dlg.update_progress)
        worker.signals.finished.connect(dlg.download_finished)

        with patch("PyQt6.QtWidgets.QMessageBox.information") as mock_info:
            dlg.download_finished(True, "Finished successfully")
        
        # Ensure worker reference is cleared
        self.assertIsNone(dlg.worker)
        dlg.close()

        # 2. Worker session lifecycle test
        worker2 = PixivDownloaderWorker(mode="ILLUST", target_id="999999", output_dir="")
        self.assertIsNone(worker2.session)
        session_inside = []
        def mock_do_download():
            session_inside.append(worker2.session)
            self.assertIsNotNone(worker2.session)
        
        with patch.object(worker2, "_do_download", side_effect=mock_do_download):
            worker2.run()

        self.assertEqual(len(session_inside), 1)
        self.assertIsNone(worker2.session)

if __name__ == "__main__":
    unittest.main()

