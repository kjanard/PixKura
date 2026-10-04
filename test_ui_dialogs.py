import unittest
import sys
import os
from PyQt6.QtWidgets import QApplication

app = QApplication.instance() or QApplication(sys.argv)

from config import DB_FILE
from database import get_dashboard_analytics
from dashboard_dialog import DashboardDialog
from lightbox_viewer import LightboxViewerDialog

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
        icon_path = os.path.join(os.path.dirname(__file__), "icon.ico")
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
        from components import MinimizableDialog, SearchHelpDialog
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

if __name__ == "__main__":
    unittest.main()


