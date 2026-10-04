import os
import re
import json
import logging
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QRadioButton, QLabel, 
    QLineEdit, QComboBox, QPushButton, QProgressBar, QTextEdit, 
    QMessageBox, QFileDialog
)
from PyQt6.QtGui import QIcon
from components import MinimizableDialog

class PixivDownloadDialog(MinimizableDialog):
    def __init__(self, parent=None, default_dir=""):
        super().__init__(parent)
        self.setWindowTitle("Pixiv Download Manager (Safe Mode)")
        self.resize(650, 580)
        
        icon_path = os.path.join(os.path.dirname(__file__), "icon.ico")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        self.parent_app = parent
        self.output_dir = default_dir
        self.worker = None

        layout = QVBoxLayout(self)

        # Download Type Selection
        type_layout = QHBoxLayout()
        self.rb_illust = QRadioButton("Download by Illust ID")
        self.rb_artist = QRadioButton("Download by Artist ID")
        self.rb_bookmark = QRadioButton("Download by Bookmark")
        self.rb_illust.setChecked(True)
        type_layout.addWidget(self.rb_illust)
        type_layout.addWidget(self.rb_artist)
        type_layout.addWidget(self.rb_bookmark)
        layout.addLayout(type_layout)

        # ID Input
        id_layout = QHBoxLayout()
        self.id_label = QLabel("Target ID:")
        id_layout.addWidget(self.id_label)
        self.id_input = QLineEdit()
        self.id_input.setPlaceholderText("Enter numeric ID (e.g. 11029485)")
        id_layout.addWidget(self.id_input)
        layout.addLayout(id_layout)

        # PHPSESSID Cookie Input (for Bookmarks & R-18 works)
        cookie_layout = QHBoxLayout()
        self.cookie_label = QLabel("PHPSESSID Cookie:")
        cookie_layout.addWidget(self.cookie_label)
        self.cookie_input = QLineEdit()
        self.cookie_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.cookie_input.setPlaceholderText("Paste PHPSESSID here (Required for R-18 works / Private bookmarks)")
        self.cookie_input.setEnabled(True)
        cookie_layout.addWidget(self.cookie_input)
        layout.addLayout(cookie_layout)

        # Bookmark Rest Select (Public vs Private)
        rest_layout = QHBoxLayout()
        self.rest_label = QLabel("Bookmark Type:")
        rest_layout.addWidget(self.rest_label)
        self.rest_combo = QComboBox()
        self.rest_combo.addItems(["Public Bookmarks", "Private Bookmarks"])
        self.rest_combo.setEnabled(False)
        rest_layout.addWidget(self.rest_combo)
        layout.addLayout(rest_layout)

        # Bookmark Page Range
        range_layout = QHBoxLayout()
        self.range_label = QLabel("Bookmark Page Range:")
        range_layout.addWidget(self.range_label)
        self.start_page_input = QLineEdit()
        self.start_page_input.setPlaceholderText("Start Page (e.g. 1)")
        self.start_page_input.setFixedWidth(130)
        self.start_page_input.setEnabled(False)
        self.end_page_input = QLineEdit()
        self.end_page_input.setPlaceholderText("End Page (e.g. 5)")
        self.end_page_input.setFixedWidth(130)
        self.end_page_input.setEnabled(False)
        range_layout.addWidget(self.start_page_input)
        range_layout.addWidget(QLabel("to"))
        range_layout.addWidget(self.end_page_input)
        layout.addLayout(range_layout)

        # Max limit layout
        limit_layout = QHBoxLayout()
        self.limit_label = QLabel("Max Download Limit:")
        limit_layout.addWidget(self.limit_label)
        self.limit_input = QLineEdit()
        self.limit_input.setPlaceholderText("Enter max number of works to download (e.g. 500), leave empty for unlimited")
        limit_layout.addWidget(self.limit_input)
        layout.addLayout(limit_layout)

        # Directory Selection
        dir_layout = QHBoxLayout()
        dir_layout.addWidget(QLabel("Output Folder:"))
        self.dir_input = QLineEdit(self.output_dir)
        self.dir_input.setReadOnly(True)
        dir_layout.addWidget(self.dir_input)
        self.btn_browse_dir = QPushButton("Browse")
        self.btn_browse_dir.clicked.connect(self.browse_output_dir)
        dir_layout.addWidget(self.btn_browse_dir)
        layout.addLayout(dir_layout)

        # Status and Progress Bar
        self.status_label = QLabel("Status: Idle")
        layout.addWidget(self.status_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        # Real-time console log
        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setStyleSheet("background-color: #1a1a1f; color: #a7f3d0; font-family: Consolas; font-size: 11px;")
        layout.addWidget(self.log_area)

        # Actions Layout
        btn_layout = QHBoxLayout()
        self.btn_start = QPushButton("Start Download")
        self.btn_start.clicked.connect(self.start_download)
        btn_layout.addWidget(self.btn_start)

        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.clicked.connect(self.cancel_download)
        self.btn_cancel.setEnabled(False)
        btn_layout.addWidget(self.btn_cancel)
        layout.addLayout(btn_layout)

        # Load saved cookie if available
        self.load_saved_cookie()

        # Signals
        self.rb_illust.toggled.connect(self.update_ui_state)
        self.rb_artist.toggled.connect(self.update_ui_state)
        self.rb_bookmark.toggled.connect(self.update_ui_state)
        self.update_ui_state()

    def load_saved_cookie(self):
        try:
            if os.path.exists("config.json"):
                with open("config.json", "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    saved_cookie = cfg.get("pixiv_cookie", "")
                    if saved_cookie:
                        self.cookie_input.setText(saved_cookie)
        except Exception as e:
            logging.error(f"Error loading saved cookie: {e}")

    def save_cookie_to_config(self, cookie):
        try:
            cfg = {}
            if os.path.exists("config.json"):
                with open("config.json", "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            cfg["pixiv_cookie"] = cookie if cookie else ""
            with open("config.json", "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=4)
        except Exception as e:
            logging.error(f"Error saving cookie: {e}")

    def set_inputs_enabled(self, enabled: bool):
        self.rb_illust.setEnabled(enabled)
        self.rb_artist.setEnabled(enabled)
        self.rb_bookmark.setEnabled(enabled)
        self.id_input.setEnabled(enabled)
        self.cookie_input.setEnabled(enabled)
        self.limit_input.setEnabled(enabled)
        self.btn_browse_dir.setEnabled(enabled)
        if enabled:
            self.update_ui_state()
        else:
            self.rest_combo.setEnabled(False)
            self.start_page_input.setEnabled(False)
            self.end_page_input.setEnabled(False)

    def update_ui_state(self):
        is_bookmark = self.rb_bookmark.isChecked()
        self.cookie_input.setEnabled(True)  # เปิดตลอดเพื่อให้กรอก Cookie โหลดงาน R-18 ในโหมด Artist/Illust ได้ด้วย
        self.rest_combo.setEnabled(is_bookmark)
        self.start_page_input.setEnabled(is_bookmark)
        self.end_page_input.setEnabled(is_bookmark)
        if is_bookmark:
            self.id_label.setText("Pixiv User ID:")
            self.id_input.setPlaceholderText("Enter your numeric Pixiv User ID")
        else:
            self.id_label.setText("Target ID:")
            self.id_input.setPlaceholderText("Enter numeric ID or Pixiv URL (e.g. 105365468)")

    def browse_output_dir(self):
        dir_path = QFileDialog.getExistingDirectory(self, "Select Output Directory", self.output_dir)
        if dir_path:
            self.output_dir = dir_path
            self.dir_input.setText(dir_path)

    def start_download(self):
        from workers import PixivDownloaderWorker
        raw_id = self.id_input.text().strip()

        # Auto-extract ID if user pasted a Pixiv URL
        target_id = None
        if "artworks/" in raw_id or "illust_id=" in raw_id:
            m = re.search(r'(?:artworks/|illust_id=)(\d+)', raw_id)
            if m:
                target_id = m.group(1)
                self.rb_illust.setChecked(True)
        elif "users/" in raw_id:
            m = re.search(r'users/(\d+)', raw_id)
            if m:
                target_id = m.group(1)
                if not self.rb_bookmark.isChecked():
                    self.rb_artist.setChecked(True)
        elif raw_id.isdigit():
            target_id = raw_id
        else:
            digits = re.findall(r'\d+', raw_id)
            if digits:
                target_id = digits[0]
            else:
                QMessageBox.warning(self, "Validation Error", "Please enter a valid numeric Pixiv ID or Pixiv URL.")
                return

        self.id_input.setText(target_id)

        if not self.output_dir:
            QMessageBox.warning(self, "Validation Error", "Please select an output folder first.")
            return

        mode = 'ILLUST'
        if self.rb_artist.isChecked():
            mode = 'ARTIST'
        elif self.rb_bookmark.isChecked():
            mode = 'BOOKMARK'

        cookie = self.cookie_input.text().strip()
        if cookie:
            # Clean cookie if user pasted full "PHPSESSID=xxxx"
            if cookie.lower().startswith("phpsessid="):
                cookie = cookie.split("=", 1)[1].strip()
            cookie = cookie.strip("; \"'")
            self.save_cookie_to_config(cookie)
        else:
            cookie = None
            self.save_cookie_to_config("")

        bookmark_rest = 'hide' if (mode == 'BOOKMARK' and self.rest_combo.currentIndex() == 1) else 'show'
        if mode == 'BOOKMARK' and bookmark_rest == 'hide' and not cookie:
            QMessageBox.warning(
                self, "Cookie Required",
                "Private Bookmarks require your PHPSESSID cookie to access.\n\nPlease paste your PHPSESSID cookie before starting."
            )
            return

        limit_text = self.limit_input.text().strip()
        max_limit = None
        if limit_text:
            if limit_text.isdigit():
                max_limit = int(limit_text)
            else:
                QMessageBox.warning(self, "Validation Error", "Please enter a valid number for max download limit.")
                return

        start_page = None
        end_page = None
        if mode == 'BOOKMARK':
            sp_text = self.start_page_input.text().strip()
            ep_text = self.end_page_input.text().strip()
            if sp_text:
                if sp_text.isdigit() and int(sp_text) > 0:
                    start_page = int(sp_text)
                else:
                    QMessageBox.warning(self, "Validation Error", "Start Page must be a valid positive number.")
                    return
            if ep_text:
                if ep_text.isdigit() and int(ep_text) > 0:
                    end_page = int(ep_text)
                else:
                    QMessageBox.warning(self, "Validation Error", "End Page must be a valid positive number.")
                    return
                if start_page and end_page < start_page:
                    QMessageBox.warning(self, "Validation Error", "End Page cannot be smaller than Start Page.")
                    return

        self.log_area.clear()
        self.progress_bar.setValue(0)
        self.set_inputs_enabled(False)
        self.btn_start.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.status_label.setText("Status: Downloading...")

        self.worker = PixivDownloaderWorker(mode, target_id, self.output_dir, cookie=cookie, bookmark_rest=bookmark_rest, max_limit=max_limit, start_page=start_page, end_page=end_page)
        self.worker.signals.log.connect(self.add_log)
        self.worker.signals.progress.connect(self.update_progress)
        self.worker.signals.finished.connect(self.download_finished)
        
        # Start worker on background pool
        self.parent_app.bg_pool.start(self.worker)

    def cancel_download(self):
        if self.worker:
            try:
                self.worker.stop()
            except Exception as e:
                pass
            self.add_log("🛑 Cancel requested by user. Cleaning up and stopping...")
            self.btn_cancel.setEnabled(False)
            self.status_label.setText("Status: Stopping...")

    def add_log(self, text):
        self.log_area.append(text)
        self.log_area.ensureCursorVisible()

    def update_progress(self, current, total):
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(current)
        self.status_label.setText(f"Status: Downloading {current}/{total}...")

    def download_finished(self, success, message):
        self.set_inputs_enabled(True)
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        if success:
            self.status_label.setText("Status: Finished Successfully")
            self.progress_bar.setValue(self.progress_bar.maximum())
            QMessageBox.information(self, "Success", "Download completed successfully!")
            if self.parent_app:
                try:
                    self.parent_app.current_view_mode = None
                    self.parent_app.folders_model.update_data([])
                    self.parent_app.folder_item_map.clear()
                    self.parent_app.refresh_view()
                except Exception as e:
                    logging.error(f"Error refreshing parent app: {e}")
        else:
            is_cancelled = "cancelled" in message.lower()
            self.status_label.setText("Status: Cancelled" if is_cancelled else "Status: Failed/Stopped")
            if is_cancelled:
                QMessageBox.information(self, "Download Cancelled", message)
            else:
                QMessageBox.critical(self, "Error", f"Download failed: {message}")
        self.worker = None

    def closeEvent(self, event):
        if self.worker:
            reply = QMessageBox.question(
                self, "Exit Confirmation",
                "A download is currently running. Do you want to stop it and exit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.worker.stop()
                try: self.worker.signals.log.disconnect(self.add_log)
                except Exception as e: logging.error(f"Disconnect error: {e}")
                try: self.worker.signals.progress.disconnect(self.update_progress)
                except Exception as e: logging.error(f"Disconnect error: {e}")
                try: self.worker.signals.finished.disconnect(self.download_finished)
                except Exception as e: logging.error(f"Disconnect error: {e}")
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()
