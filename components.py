import os
import re
import secrets
import hashlib
import base64
import sqlite3
import requests
from urllib.parse import urlparse, parse_qs
from PyQt6.QtWidgets import (
    QListWidgetItem, QDialog, QVBoxLayout, QPushButton, QTextEdit, 
    QLabel, QMessageBox, QLineEdit, QHBoxLayout, QWidget, QApplication,
    QProgressBar, QCheckBox, QComboBox, QFileDialog, QSlider, QListWidget,
    QGroupBox, QAbstractItemView
)
from PyQt6.QtCore import Qt, QUrl, QTimer, QRectF, QEvent, QThreadPool, QThread, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QPainter, QColor

from config import AppConfig, DB_FILE
from utils import format_size, show_in_file_manager

class NaturalSortItem(QListWidgetItem):
    sort_mode = 0 
    def __lt__(self, other):
        if self.sort_mode == 1:
            try:
                val1 = self.data(Qt.ItemDataRole.UserRole + 2) or 0
                val2 = other.data(Qt.ItemDataRole.UserRole + 2) or 0
                return val1 < val2
            except Exception: 
                pass
        try: 
            return self.natural_keys(self.text()) < self.natural_keys(other.text())
        except Exception: 
            return self.text() < other.text()
    def natural_keys(self, text):
        return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', text) if c]


class LoadingSpinner(QWidget):
    def __init__(self, parent=None, center_on_parent=True):
        super().__init__(parent)
        self.center_on_parent = center_on_parent
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.rotate)
        self.angle = 0
        self.setFixedSize(50, 50)
        self.setVisible(False)

    def start(self):
        self.timer.start(50)  # 50ms interval
        self.setVisible(True)
        if self.center_on_parent and self.parentWidget():
            self.center()

    def stop(self):
        self.timer.stop()
        self.setVisible(False)

    def rotate(self):
        self.angle = (self.angle + 30) % 360
        self.update()

    def center(self):
        if self.parentWidget():
            p_rect = self.parentWidget().rect()
            self.move(
                (p_rect.width() - self.width()) // 2,
                (p_rect.height() - self.height()) // 2
            )

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        painter.translate(self.width() / 2, self.height() / 2)
        painter.rotate(self.angle)
        
        color = QColor("#3b82f6")
        for i in range(12):
            painter.setPen(Qt.PenStyle.NoPen)
            alpha = int(255 * (i / 11))
            color.setAlpha(alpha)
            painter.setBrush(color)
            
            painter.drawRoundedRect(QRectF(-2.5, -20, 5, 12), 2.5, 2.5)
            painter.rotate(30)


class LoadingOverlay(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.parent = parent
        self.parent.installEventFilter(self)
        
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        self.spinner = LoadingSpinner(self, center_on_parent=False)
        layout.addWidget(self.spinner, alignment=Qt.AlignmentFlag.AlignCenter)
        
        self.label = QLabel("Loading...", self)
        self.label.setStyleSheet("color: #3b82f6; font-size: 15px; font-weight: bold; margin-top: 12px;")
        layout.addWidget(self.label, alignment=Qt.AlignmentFlag.AlignCenter)
        
        self.setVisible(False)

    def start(self, text="Loading..."):
        self.label.setText(text)
        self.spinner.start()
        self.resize(self.parent.size())
        self.raise_()
        self.setVisible(True)

    def stop(self):
        self.spinner.stop()
        self.setVisible(False)

    def eventFilter(self, obj, event):
        if obj == self.parent and event.type() == QEvent.Type.Resize:
            self.resize(self.parent.size())
        return super().eventFilter(obj, event)


class MinimizableDialog(QDialog):
    """
    Base QDialog supporting Minimize and Maximize buttons on Windows,
    with synchronized minimizing/restoring with the parent QMainWindow
    so the entire application cleanly minimizes to the Taskbar.
    """
    def __init__(self, parent=None, *args, **kwargs):
        super().__init__(parent, *args, **kwargs)
        self.setWindowFlags(
            self.windowFlags() 
            | Qt.WindowType.WindowMinimizeButtonHint 
            | Qt.WindowType.WindowMaximizeButtonHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

    def changeEvent(self, event):
        if event.type() == QEvent.Type.WindowStateChange:
            if self.isMinimized():
                p = self.parent()
                if not p:
                    for top in QApplication.topLevelWidgets():
                        if top != self and top.isWindow() and top.isVisible() and hasattr(top, 'showMinimized'):
                            p = top
                            break
                if p and hasattr(p, 'showMinimized') and not p.isMinimized():
                    p.showMinimized()
            elif not self.isMinimized():
                p = self.parent()
                if not p:
                    for top in QApplication.topLevelWidgets():
                        if top != self and top.isWindow() and top.isVisible() and hasattr(top, 'showNormal'):
                            p = top
                            break
                if p and hasattr(p, 'showNormal') and p.isMinimized():
                    p.showNormal()
        super().changeEvent(event)


class SearchHelpDialog(MinimizableDialog):
    """
    Dialog explaining Danbooru-style multi-tag search, safety rating filters,
    and query syntax with clickable example chips.
    """
    def __init__(self, parent=None, on_apply_query=None):
        super().__init__(parent)
        self.on_apply_query = on_apply_query
        self.setWindowTitle("❓ Search Syntax Guide & Safety Help (คู่มือการค้นหาและความปลอดภัย)")
        self.resize(740, 640)
        self.setStyleSheet("""
            QDialog {
                background-color: #121216;
                color: #e5e7eb;
            }
            QTextEdit {
                background-color: #181820;
                color: #f3f4f6;
                border: 1px solid #2d2d38;
                border-radius: 8px;
                padding: 12px;
                font-family: 'Segoe UI', Arial, sans-serif;
                font-size: 13px;
                line-height: 1.6;
            }
            QPushButton.chip {
                background-color: #222230;
                color: #60a5fa;
                border: 1px solid #3b82f6;
                border-radius: 12px;
                padding: 5px 12px;
                font-size: 12px;
                font-weight: bold;
            }
            QPushButton.chip:hover {
                background-color: #3b82f6;
                color: #ffffff;
            }
            QPushButton.chip-sfw {
                background-color: #142a20;
                color: #34d399;
                border: 1px solid #10b981;
                border-radius: 12px;
                padding: 5px 12px;
                font-size: 12px;
                font-weight: bold;
            }
            QPushButton.chip-sfw:hover {
                background-color: #10b981;
                color: #ffffff;
            }
            QPushButton.chip-nsfw {
                background-color: #301820;
                color: #f87171;
                border: 1px solid #ef4444;
                border-radius: 12px;
                padding: 5px 12px;
                font-size: 12px;
                font-weight: bold;
            }
            QPushButton.chip-nsfw:hover {
                background-color: #ef4444;
                color: #ffffff;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        title_label = QLabel("🔍 PixKura Search & Safety Engine")
        title_label.setStyleSheet("font-size: 16px; font-weight: bold; color: #60a5fa;")
        layout.addWidget(title_label)

        sub_label = QLabel("รองรับการค้นหาแบบ Danbooru Boolean Query (AND, OR, NOT), รายการโปรด (is:fav), การคัดกรองเรตติ้งความปลอดภัย และประเภท/ขนาดไฟล์")
        sub_label.setStyleSheet("font-size: 12px; color: #9ca3af;")
        sub_label.setWordWrap(True)
        layout.addWidget(sub_label)

        doc_view = QTextEdit()
        doc_view.setReadOnly(True)
        doc_html = """
        <style>
            h3 { color: #60a5fa; margin-top: 14px; margin-bottom: 6px; font-size: 14px; }
            code { background-color: #27273a; color: #38bdf8; padding: 2px 6px; border-radius: 4px; font-family: Consolas, monospace; }
            .badge-sfw { background-color: #064e3b; color: #34d399; padding: 2px 6px; border-radius: 4px; font-weight: bold; }
            .badge-nsfw { background-color: #7f1d1d; color: #f87171; padding: 2px 6px; border-radius: 4px; font-weight: bold; }
            .badge-sens { background-color: #78350f; color: #fbbf24; padding: 2px 6px; border-radius: 4px; font-weight: bold; }
            table { width: 100%; border-collapse: collapse; margin-top: 6px; }
            th { text-align: left; color: #9ca3af; border-bottom: 1px solid #374151; padding: 6px; }
            td { padding: 6px; border-bottom: 1px solid #1f2937; }
        </style>

        <h3>🛡️ 1. Safety, Favorites & Rating Filters (ความปลอดภัยและรายการโปรด)</h3>
        <p>สามารถเลือกจากช่อง <b>Safety</b> ในแถบเครื่องมือ หรือพิมพ์คีย์เวิร์ดในช่องค้นหา:</p>
        <table>
            <tr><th>คำสั่ง</th><th>ประเภท</th><th>คำอธิบาย</th></tr>
            <tr>
                <td><code>is:fav</code> หรือ <code>is:favorite</code></td>
                <td><span class="badge-sens">⭐ FAVORITES</span></td>
                <td>แสดงเฉพาะภาพที่กดติดดาวรายการโปรดไว้ (กดปุ่ม ⭐ หรือปุ่มลัด <code>S</code> ใน Lightbox)</td>
            </tr>
            <tr>
                <td><code>rating:sfw</code> หรือ <code>is:sfw</code></td>
                <td><span class="badge-sfw">🟢 SFW ONLY</span></td>
                <td>แสดงเฉพาะภาพปลอดภัย (General) ไม่โป๊ <b>ช่วยป้องกันภาพ 18+ หลุดเข้าไปเทรนใน Colab</b></td>
            </tr>
            <tr>
                <td><code>rating:nsfw</code> หรือ <code>is:nsfw</code></td>
                <td><span class="badge-nsfw">🔞 NSFW ONLY</span></td>
                <td>แสดงเฉพาะภาพเรตผู้ใหญ่ 18+ (Explicit / R-18) หรือมีแท็กทางเพศชัดเจน</td>
            </tr>
            <tr>
                <td><code>rating:sensitive</code> หรือ <code>is:ecchi</code></td>
                <td><span class="badge-sens">⚠️ SENSITIVE</span></td>
                <td>แสดงภาพล่อแหลม วับๆ แวมๆ เช่น ชุดว่ายน้ำ บิกินี่ ชุดชั้นใน (Ecchi)</td>
            </tr>
            <tr>
                <td><code>rating:all</code></td>
                <td><span>⚪ ALL</span></td>
                <td>แสดงทั้งหมดโดยไม่จำกัดเรตติ้งความปลอดภัย</td>
            </tr>
        </table>

        <h3>👤 2. Characters, Series & Artists (ตัวละคร ซีรีส์ และนักวาด)</h3>
        <table>
            <tr><th>ไวยากรณ์</th><th>ตัวอย่าง</th><th>ผลลัพธ์</th></tr>
            <tr>
                <td><code>char:name</code></td>
                <td><code>char:hatsune_miku</code></td>
                <td>ค้นหาเฉพาะภาพที่มีตัวละครนั้น</td>
            </tr>
            <tr>
                <td><code>series:name</code></td>
                <td><code>series:genshin_impact</code></td>
                <td>ค้นหาเฉพาะภาพที่มาจากซีรีส์หรือเกมนั้น</td>
            </tr>
            <tr>
                <td><code>artist:name</code></td>
                <td><code>artist:morikura</code></td>
                <td>ค้นหาตามชื่อนักวาดหรือรหัสโฟลเดอร์ Pixiv ID</td>
            </tr>
        </table>

        <h3>🏷️ 3. Boolean Logic & Tag Operators (การผสมเงื่อนไข)</h3>
        <table>
            <tr><th>เครื่องหมาย</th><th>ตัวอย่าง</th><th>ผลลัพธ์</th></tr>
            <tr>
                <td><b>AND</b> (เว้นวรรค หรือ <code>AND</code> / <code>&&</code>)</td>
                <td><code>solo 1girl</code> หรือ <code>tag:solo AND tag:swimsuit</code></td>
                <td>ภาพต้องมีครบทุกแท็กที่ระบุ (AND อัตโนมัติ)</td>
            </tr>
            <tr>
                <td><b>OR</b> (หรือ <code>||</code>)</td>
                <td><code>cat_ears OR dog_ears</code> หรือ <code>tag:a || tag:b</code></td>
                <td>ภาพที่มีแท็กใดแท็กหนึ่ง อย่างน้อยหนึ่งแท็ก (รองรับ OR หลายชั้น)</td>
            </tr>
            <tr>
                <td><b>NOT</b> (หรือ <code>-</code> / <code>!</code>)</td>
                <td><code>solo -glasses</code> หรือ <code>tag:swimsuit NOT tag:glasses</code></td>
                <td>คัดออก / ไม่รวมภาพที่มีแท็กหรือตัวละครนั้น</td>
            </tr>
            <tr>
                <td><b>NOR</b> (ไม่เอาทั้งคู่)</td>
                <td><code>cat_ears NOR dog_ears</code> หรือ <code>tag:a NOR tag:b</code></td>
                <td>คัดทิ้งทั้งสองเงื่อนไข (Neither A nor B = NOT A AND NOT B)</td>
            </tr>
        </table>

        <h3>📁 4. File Types & Sizes (ประเภทและขนาดไฟล์)</h3>
        <table>
            <tr><th>คำสั่ง</th><th>ตัวอย่าง</th><th>คำอธิบาย</th></tr>
            <tr>
                <td><code>type:ext</code></td>
                <td><code>type:mp4</code>, <code>type:gif</code>, <code>type:png</code>, <code>type:video</code></td>
                <td>กรองเฉพาะไฟล์ภาพเคลื่อนไหว หรือวิดีโอ หรือภาพนิ่ง</td>
            </tr>
            <tr>
                <td><code>size:&gt;XX</code> หรือ <code>size:&lt;XX</code></td>
                <td><code>size:>5mb</code>, <code>size:<1mb</code></td>
                <td>กรองตามขนาดไฟล์ (รองรับ kb, mb, gb)</td>
            </tr>
        </table>
        """
        doc_view.setHtml(doc_html)
        layout.addWidget(doc_view)

        layout.addWidget(QLabel("💡 Quick Try (คลิกตัวอย่างเพื่อใส่ลงช่องค้นหาทันที):"))
        chips_layout = QHBoxLayout()
        chips_layout.setSpacing(8)

        examples = [
            ("⭐ Favorites", "is:fav", "chip"),
            ("🛡️ SFW Only", "rating:sfw", "chip-sfw"),
            ("🔞 NSFW Only", "rating:nsfw", "chip-nsfw"),
            ("👗 Swimsuit -Glasses", "swimsuit -glasses", "chip"),
            ("👤 Miku SFW", "char:hatsune_miku rating:sfw", "chip-sfw"),
            ("🎬 MP4 Video", "type:mp4", "chip"),
            ("📦 Large (>5MB)", "size:>5mb", "chip"),
        ]

        for label_text, query_text, chip_class in examples:
            btn = QPushButton(label_text)
            btn.setProperty("class", chip_class)
            btn.setStyleSheet(self.styleSheet())
            btn.clicked.connect(lambda checked, q=query_text: self.apply_example(q))
            chips_layout.addWidget(btn)

        layout.addLayout(chips_layout)

        footer = QHBoxLayout()
        footer.addStretch()
        btn_close = QPushButton("ปิดหน้าต่าง (Close)")
        btn_close.clicked.connect(self.accept)
        footer.addWidget(btn_close)
        layout.addLayout(footer)

    def apply_example(self, query):
        if self.on_apply_query:
            self.on_apply_query(query)
            self.accept()
        else:
            QApplication.clipboard().setText(query)


class _DatasetCountWorker(QThread):
    """
    Background worker that resolves tagged candidate files and NSFW paths
    without blocking the GUI thread or calling os.path.exists() 268k+ times.
    """
    finished = pyqtSignal(list, set)

    def __init__(self, db_file, initial_paths=None):
        super().__init__()
        self.db_file = db_file
        self.initial_paths = list(initial_paths or [])
        self._conn = None
        self._is_cancelled = False

    def stop(self):
        self._is_cancelled = True
        c = self._conn
        if c is not None:
            try:
                c.interrupt()
            except Exception:
                pass

    def run(self):
        from query_parser import NSFW_ALL_FILTER_TAGS
        candidates = []
        nsfw_set = set()
        conn = None
        try:
            conn = sqlite3.connect(self.db_file, timeout=10.0)
            self._conn = conn
            cursor = conn.cursor()

            if self.initial_paths:
                candidates = self.initial_paths
            else:
                # Fast index semi-join on file_index + idx_file_tags_path instead of 7.7M-row scan
                cursor.execute(
                    "SELECT path FROM file_index f WHERE EXISTS ("
                    "SELECT 1 FROM file_tags t WHERE t.path = f.path AND t.tag_name != '__none__' AND t.category != 99)"
                )
                candidates = [r[0] for r in cursor.fetchall()]
                if not candidates and not self._is_cancelled:
                    cursor.execute("SELECT DISTINCT path FROM file_tags WHERE tag_name != '__none__' AND category != 99")
                    candidates = [r[0] for r in cursor.fetchall()]

            if not self._is_cancelled and candidates:
                nsfw_placeholders = ','.join(['?'] * len(NSFW_ALL_FILTER_TAGS))
                cursor.execute(
                    f"SELECT DISTINCT path FROM file_tags WHERE tag_name IN ({nsfw_placeholders})",
                    NSFW_ALL_FILTER_TAGS
                )
                nsfw_set = {r[0] for r in cursor.fetchall()}
        except Exception:
            if not candidates:
                candidates = list(self.initial_paths)
        finally:
            self._conn = None
            if conn is not None:
                try: conn.close()
                except Exception: pass

        if not self._is_cancelled:
            self.finished.emit(candidates, nsfw_set)


class DatasetExporterDialog(MinimizableDialog):
    """
    One-Click Dataset Exporter for Kohya_ss / Stable Diffusion LoRA / Colab training.
    Exports images alongside formatted `.txt` tag caption files.
    """
    def __init__(self, parent=None, file_paths=None, default_dir="", db_file=DB_FILE):
        super().__init__(parent)
        self.db_file = db_file or DB_FILE
        self.initial_file_paths = list(file_paths or [])
        self.output_dir = default_dir or ""
        self.worker = None

        self.setWindowTitle("📦 Dataset Exporter for LoRA / Colab (ส่งออกรูปภาพพร้อมไฟล์แท็ก .txt)")
        self.resize(640, 480)
        self.setStyleSheet("""
            QDialog { background-color: #121216; color: #e5e7eb; }
            QGroupBox {
                border: 1px solid #2d2d38;
                border-radius: 8px;
                margin-top: 10px;
                padding-top: 12px;
                font-weight: bold;
                color: #60a5fa;
            }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }
            QLineEdit {
                background-color: #1e1e2a; color: #f3f4f6;
                border: 1px solid #374151; border-radius: 6px; padding: 6px;
            }
            QPushButton {
                background-color: #252538; color: #f3f4f6;
                border: 1px solid #3b82f6; border-radius: 6px;
                padding: 7px 14px; font-weight: bold;
            }
            QPushButton:hover { background-color: #3b82f6; color: white; }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        lbl_title = QLabel("📦 Export AI Dataset (รูปภาพ + ไฟล์ Caption .txt สำหรับเทรน LoRA / Colab)")
        lbl_title.setStyleSheet("font-size: 15px; font-weight: bold; color: #38bdf8;")
        layout.addWidget(lbl_title)

        # Scope & Options Group
        opt_group = QGroupBox("⚙️ ตั้งค่าการส่งออก (Export Options)")
        opt_layout = QVBoxLayout(opt_group)

        self.lbl_count = QLabel("")
        self.lbl_count.setStyleSheet("color: #34d399; font-weight: bold;")
        opt_layout.addWidget(self.lbl_count)

        self.chk_sfw_only = QCheckBox("🛡️ คัดเฉพาะภาพปลอดภัย (SFW Only - ป้องกันภาพ 18+ หลุดเข้า Google Colab)")
        self.chk_sfw_only.setChecked(True)
        self.chk_sfw_only.toggled.connect(self._update_target_count)
        opt_layout.addWidget(self.chk_sfw_only)

        self.chk_spaces = QCheckBox("🔤 แปลง _ เป็นเว้นวรรค (เช่น hatsune_miku -> hatsune miku สำหรับ Kohya_ss)")
        self.chk_spaces.setChecked(True)
        opt_layout.addWidget(self.chk_spaces)

        self.chk_copy_img = QCheckBox("🖼️ คัดลอกไฟล์รูปภาพไปยังโฟลเดอร์ปลายทางพร้อมไฟล์ .txt")
        self.chk_copy_img.setChecked(True)
        opt_layout.addWidget(self.chk_copy_img)

        # Trigger word / prefix
        prefix_row = QHBoxLayout()
        prefix_row.addWidget(QLabel("Trigger Words (คั่นด้วย ,):"))
        self.input_prefix = QLineEdit()
        self.input_prefix.setPlaceholderText("เช่น masterpiece, best quality, 1girl (เว้นว่างได้)")
        prefix_row.addWidget(self.input_prefix, 1)
        opt_layout.addLayout(prefix_row)

        # Confidence threshold
        thresh_row = QHBoxLayout()
        thresh_row.addWidget(QLabel("ความมั่นใจแท็กขั้นต่ำ (Min Confidence):"))
        self.slider_conf = QSlider(Qt.Orientation.Horizontal)
        self.slider_conf.setRange(20, 85)
        self.slider_conf.setValue(35)
        self.lbl_conf_val = QLabel("0.35")
        self.lbl_conf_val.setStyleSheet("color: #38bdf8; font-weight: bold; min-width: 36px;")
        self.slider_conf.valueChanged.connect(lambda v: self.lbl_conf_val.setText(f"{v/100:.2f}"))
        thresh_row.addWidget(self.slider_conf, 1)
        thresh_row.addWidget(self.lbl_conf_val)
        opt_layout.addLayout(thresh_row)

        # Output dir
        dir_row = QHBoxLayout()
        dir_row.addWidget(QLabel("โฟลเดอร์ปลายทาง:"))
        self.input_dir = QLineEdit(self.output_dir)
        dir_row.addWidget(self.input_dir, 1)
        btn_browse = QPushButton("📂 เลือกโฟลเดอร์...")
        btn_browse.clicked.connect(self.browse_dir)
        dir_row.addWidget(btn_browse)
        opt_layout.addLayout(dir_row)

        layout.addWidget(opt_group)

        # Progress
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        self.lbl_status = QLabel("พร้อมส่งออก Dataset...")
        self.lbl_status.setStyleSheet("color: #9ca3af;")
        layout.addWidget(self.lbl_status)

        # Buttons
        btn_row = QHBoxLayout()
        self.btn_export = QPushButton("🚀 เริ่มส่งออก Dataset (Start Export)")
        self.btn_export.setStyleSheet("background-color: #059669; color: white; padding: 8px 18px;")
        self.btn_export.clicked.connect(self.start_export)
        btn_row.addWidget(self.btn_export)
        btn_row.addStretch()
        btn_close = QPushButton("ปิด (Close)")
        btn_close.clicked.connect(self.close)
        btn_row.addWidget(btn_close)
        layout.addLayout(btn_row)

        self._all_candidates = []
        self._nsfw_set = set()
        self._resolved_paths = []
        self._candidates_loaded = False
        self._count_worker = None

        if 1 <= len(self.initial_file_paths) <= 500:
            self._resolve_small_batch_sync()
        else:
            self.lbl_count.setText("⏳ กำลังคำนวณจำนวนไฟล์ที่พร้อมส่งออก...")
            QTimer.singleShot(0, self._start_async_count)

    def _resolve_small_batch_sync(self):
        """Fast (< 2 ms) indexed lookup when exporting a specific folder or small selection."""
        from query_parser import NSFW_ALL_FILTER_TAGS
        self._all_candidates = list(self.initial_file_paths)
        conn = None
        try:
            conn = sqlite3.connect(self.db_file, timeout=5.0)
            cursor = conn.cursor()
            path_ph = ','.join(['?'] * len(self._all_candidates))
            nsfw_ph = ','.join(['?'] * len(NSFW_ALL_FILTER_TAGS))
            cursor.execute(
                f"SELECT DISTINCT path FROM file_tags WHERE path IN ({path_ph}) AND tag_name IN ({nsfw_ph})",
                tuple(self._all_candidates) + tuple(NSFW_ALL_FILTER_TAGS)
            )
            self._nsfw_set = {r[0] for r in cursor.fetchall()}
        except Exception:
            self._nsfw_set = set()
        finally:
            if conn is not None:
                try: conn.close()
                except Exception: pass
        self._candidates_loaded = True
        self._apply_filter_from_cache()

    def _start_async_count(self):
        if self._candidates_loaded or not self.isVisible() and self._count_worker is not None:
            return
        self._stop_count_worker()
        self._count_worker = _DatasetCountWorker(self.db_file, self.initial_file_paths)
        self._count_worker.finished.connect(self._on_candidates_loaded)
        self._count_worker.start()

    def _on_candidates_loaded(self, candidates, nsfw_set):
        self._all_candidates = candidates
        self._nsfw_set = nsfw_set
        self._candidates_loaded = True
        self._apply_filter_from_cache()

    def _apply_filter_from_cache(self):
        if self.chk_sfw_only.isChecked() and self._nsfw_set:
            nsfw = self._nsfw_set
            self._resolved_paths = [p for p in self._all_candidates if p not in nsfw]
        else:
            self._resolved_paths = list(self._all_candidates)
        self.lbl_count.setText(f"📊 จำนวนไฟล์ที่จะส่งออก: {len(self._resolved_paths):,} ไฟล์")

    def _update_target_count(self):
        if self._candidates_loaded:
            self._apply_filter_from_cache()
        elif self._count_worker is None or not self._count_worker.isRunning():
            self._start_async_count()

    def _stop_count_worker(self):
        w = self._count_worker
        if w is not None:
            if w.isRunning():
                w.stop()
                w.wait(300)
            self._count_worker = None

    def closeEvent(self, event):
        self._stop_count_worker()
        super().closeEvent(event)

    def reject(self):
        self._stop_count_worker()
        super().reject()

    def browse_dir(self):
        d = QFileDialog.getExistingDirectory(self, "เลือกโฟลเดอร์สำหรับบันทึก Dataset", self.input_dir.text())
        if d:
            self.input_dir.setText(d)

    def start_export(self):
        out_dir = self.input_dir.text().strip()
        if not out_dir:
            QMessageBox.warning(self, "แจ้งเตือน", "กรุณาเลือกโฟลเดอร์ปลายทางสำหรับส่งออก Dataset")
            return
        if not self._candidates_loaded and self._count_worker is not None and self._count_worker.isRunning():
            self._count_worker.wait(3000)
        if not self._resolved_paths:
            QMessageBox.warning(self, "แจ้งเตือน", "ไม่พบไฟล์ที่ตรงตามเงื่อนไขสำหรับส่งออก")
            return

        from workers import DatasetExportWorker
        self.btn_export.setEnabled(False)
        self.progress_bar.setValue(0)
        self.lbl_status.setText("⏳ กำลังส่งออกไฟล์รูปภาพและ .txt captions...")

        self.worker = DatasetExportWorker(
            file_paths=self._resolved_paths,
            output_dir=out_dir,
            db_file=self.db_file,
            min_confidence=self.slider_conf.value() / 100.0,
            format_spaces=self.chk_spaces.isChecked(),
            prefix_tags=self.input_prefix.text().strip(),
            copy_images=self.chk_copy_img.isChecked()
        )
        self.worker.signals.progress.connect(self.on_progress)
        self.worker.signals.finished.connect(self.on_finished)
        QThreadPool.globalInstance().start(self.worker)

    def on_progress(self, cur, tot, fname):
        pct = int((cur / tot) * 100) if tot > 0 else 0
        self.progress_bar.setValue(pct)
        self.lbl_status.setText(f"กำลังส่งออก ({cur:,}/{tot:,}): {fname}")

    def on_finished(self, count, out_dir):
        self.btn_export.setEnabled(True)
        self.progress_bar.setValue(100)
        self.lbl_status.setText(f"✅ ส่งออกสำเร็จทั้งหมด {count:,} ไฟล์ ไปยัง {out_dir}")
        QMessageBox.information(self, "ส่งออก Dataset สำเร็จ", f"ส่งออกรูปภาพพร้อมไฟล์แท็ก .txt จำนวน {count:,} ชุด เรียบร้อยแล้ว!")


class DuplicateFinderDialog(MinimizableDialog):
    """
    Perceptual Hash (dHash) Duplicate & Similar Artwork Finder Dialog.
    Detects identical and visually similar artworks across folders.
    """
    def __init__(self, parent=None, root_path=None, db_file=DB_FILE):
        super().__init__(parent)
        self.root_path = root_path
        self.db_file = db_file or DB_FILE
        self.worker = None
        self.duplicate_groups = []

        self.setWindowTitle("🔍 Duplicate & Similar Artwork Finder (ค้นหาภาพซ้ำด้วย Perceptual dHash)")
        self.resize(820, 620)
        self.setStyleSheet("""
            QDialog { background-color: #121216; color: #e5e7eb; }
            QListWidget {
                background-color: #181822; color: #f3f4f6;
                border: 1px solid #2d2d38; border-radius: 8px; padding: 6px;
                font-size: 12px;
            }
            QListWidget::item { padding: 6px; border-bottom: 1px solid #222230; }
            QListWidget::item:selected { background-color: #2563eb; color: white; }
            QPushButton {
                background-color: #252538; color: #f3f4f6;
                border: 1px solid #3b82f6; border-radius: 6px;
                padding: 6px 12px; font-weight: bold;
            }
            QPushButton:hover { background-color: #3b82f6; color: white; }
            QComboBox {
                background-color: #1e1e2a; color: white;
                border: 1px solid #374151; border-radius: 6px; padding: 5px;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        lbl_title = QLabel("🔍 ค้นหาภาพซ้ำและภาพที่คล้ายกัน (64-bit Perceptual dHash)")
        lbl_title.setStyleSheet("font-size: 15px; font-weight: bold; color: #60a5fa;")
        layout.addWidget(lbl_title)

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("ความละเอียดการเทียบภาพ:"))
        self.combo_dist = QComboBox()
        self.combo_dist.addItem("🎯 เหมือนกันเป๊ะ 100% (Hamming Distance = 0)", 0)
        self.combo_dist.addItem("🔍 ภาพเดียวกันแต่ต่างขนาด/การบีบอัด (Distance <= 2)", 2)
        self.combo_dist.addItem("✨ ภาพคล้ายคลึง / Variations (Distance <= 4) [แนะนำ]", 4)
        self.combo_dist.addItem("🌐 ค้นหากว้าง (Distance <= 6)", 6)
        self.combo_dist.setCurrentIndex(1)
        top_row.addWidget(self.combo_dist, 1)

        self.btn_scan = QPushButton("🚀 เริ่มสแกนหาภาพซ้ำ (Start Scan)")
        self.btn_scan.setStyleSheet("background-color: #059669; color: white;")
        self.btn_scan.clicked.connect(self.start_scan)
        top_row.addWidget(self.btn_scan)
        layout.addLayout(top_row)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        self.lbl_status = QLabel("กดปุ่ม 'เริ่มสแกนหาภาพซ้ำ' เพื่อคำนวณ dHash และเปรียบเทียบภาพในคลัง")
        self.lbl_status.setStyleSheet("color: #9ca3af;")
        layout.addWidget(self.lbl_status)

        self.list_results = QListWidget()
        self.list_results.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list_results.itemDoubleClicked.connect(self.on_item_double_clicked)
        layout.addWidget(self.list_results, 1)

        footer = QHBoxLayout()
        btn_preview = QPushButton("🖼️ เปิดดูใน Lightbox")
        btn_preview.clicked.connect(self.open_selected_in_lightbox)
        footer.addWidget(btn_preview)

        btn_explorer = QPushButton("📂 แสดงใน Explorer")
        btn_explorer.clicked.connect(self.open_selected_in_explorer)
        footer.addWidget(btn_explorer)

        btn_delete = QPushButton("🗑️ ลบไฟล์ที่เลือกออกจากดิสก์")
        btn_delete.setStyleSheet("background-color: #7f1d1d; border-color: #ef4444; color: #fecaca;")
        btn_delete.clicked.connect(self.delete_selected_files)
        footer.addWidget(btn_delete)

        footer.addStretch()
        btn_close = QPushButton("ปิด (Close)")
        btn_close.clicked.connect(self.close)
        footer.addWidget(btn_close)
        layout.addLayout(footer)

    def start_scan(self):
        from workers import DuplicateScannerWorker
        self.btn_scan.setEnabled(False)
        self.list_results.clear()
        self.progress_bar.setValue(0)
        self.lbl_status.setText("⏳ กำลังคำนวณ Perceptual dHash และค้นหาภาพซ้ำ...")

        dist = self.combo_dist.currentData()
        self.worker = DuplicateScannerWorker(
            root_path=self.root_path,
            db_file=self.db_file,
            max_hamming_distance=dist if dist is not None else 2
        )
        self.worker.signals.progress.connect(self.on_progress)
        self.worker.signals.finished.connect(self.on_finished)
        QThreadPool.globalInstance().start(self.worker)

    def on_progress(self, cur, tot, fname):
        pct = int((cur / tot) * 100) if tot > 0 else 0
        self.progress_bar.setValue(pct)
        self.lbl_status.setText(f"กำลังตรวจสอบ ({cur:,}/{tot:,}): {fname}")

    def on_finished(self, groups):
        self.btn_scan.setEnabled(True)
        self.progress_bar.setValue(100)
        self.duplicate_groups = groups or []
        self.list_results.clear()

        if not self.duplicate_groups:
            self.lbl_status.setText("✨ ไม่พบภาพซ้ำในคลังรูปภาพของคุณ")
            return

        total_dups = sum(len(g) for g in self.duplicate_groups)
        self.lbl_status.setText(f"✅ พบกลุ่มภาพซ้ำ/คล้ายกันทั้งหมด {len(self.duplicate_groups):,} กลุ่ม (รวม {total_dups:,} ไฟล์) - ดับเบิลคลิกเพื่อดูภาพ")

        for g_idx, group in enumerate(self.duplicate_groups, 1):
            hdr = QListWidgetItem(f"━━━ 📁 กลุ่มที่ #{g_idx} ({len(group)} ไฟล์ที่คล้ายกัน) ━━━")
            hdr.setFlags(Qt.ItemFlag.NoItemFlags)
            hdr.setForeground(QColor("#60a5fa"))
            self.list_results.addItem(hdr)

            for rank, (path, size, mtime, dhash) in enumerate(group):
                tag = "👑 [ไฟล์ใหญ่สุด/ต้นฉบับ]" if rank == 0 else "📄 [ไฟล์ซ้ำ/ขนาดเล็กกว่า]"
                folder_name = os.path.basename(os.path.dirname(path))
                item = QListWidgetItem(f"   {tag} {os.path.basename(path)}  ({format_size(size)})  — โฟลเดอร์: {folder_name}  [dHash: {dhash}]")
                item.setData(Qt.ItemDataRole.UserRole, path)
                self.list_results.addItem(item)

    def on_item_double_clicked(self, item):
        path = item.data(Qt.ItemDataRole.UserRole)
        if path and os.path.exists(path):
            from lightbox_viewer import LightboxViewerDialog
            dlg = LightboxViewerDialog([path], 0, self, db_file=self.db_file)
            dlg.exec()

    def open_selected_in_lightbox(self):
        paths = [
            it.data(Qt.ItemDataRole.UserRole)
            for it in self.list_results.selectedItems()
            if it.data(Qt.ItemDataRole.UserRole) and os.path.exists(it.data(Qt.ItemDataRole.UserRole))
        ]
        if paths:
            from lightbox_viewer import LightboxViewerDialog
            dlg = LightboxViewerDialog(paths, 0, self, db_file=self.db_file)
            dlg.exec()

    def open_selected_in_explorer(self):
        for it in self.list_results.selectedItems():
            p = it.data(Qt.ItemDataRole.UserRole)
            if p and os.path.exists(p):
                show_in_file_manager(p)
                break

    def delete_selected_files(self):
        selected_items = [it for it in self.list_results.selectedItems() if it.data(Qt.ItemDataRole.UserRole)]
        if not selected_items:
            return
        reply = QMessageBox.question(
            self, "ยืนยันการลบไฟล์ซ้ำ",
            f"คุณต้องการลบไฟล์ที่เลือกจำนวน {len(selected_items)} ไฟล์ ออกจากดิสก์ถาวรใช่หรือไม่?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        deleted = 0
        conn = None
        try:
            conn = sqlite3.connect(self.db_file, timeout=15.0)
            cursor = conn.cursor()
            for it in selected_items:
                p = it.data(Qt.ItemDataRole.UserRole)
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                        cursor.execute("DELETE FROM file_index WHERE path = ?", (p,))
                        cursor.execute("DELETE FROM file_thumbnails WHERE path = ?", (p,))
                        cursor.execute("DELETE FROM file_tags WHERE path = ?", (p,))
                        row = self.list_results.row(it)
                        self.list_results.takeItem(row)
                        deleted += 1
                    except Exception:
                        pass
            conn.commit()
        finally:
            if conn is not None:
                try: conn.close()
                except Exception: pass
        self.lbl_status.setText(f"🗑️ ลบไฟล์ซ้ำเรียบร้อยแล้ว {deleted} ไฟล์")

    def closeEvent(self, event):
        if self.worker:
            self.worker.stop()
        super().closeEvent(event)
