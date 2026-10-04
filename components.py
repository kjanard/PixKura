import re
import secrets
import hashlib
import base64
import requests
from urllib.parse import urlparse, parse_qs
from PyQt6.QtWidgets import (
    QListWidgetItem, QDialog, QVBoxLayout, QPushButton, QTextEdit, 
    QLabel, QMessageBox, QLineEdit, QHBoxLayout, QWidget, QApplication
)
from PyQt6.QtCore import Qt, QUrl, QTimer, QRectF, QEvent
from PyQt6.QtGui import QDesktopServices, QPainter, QColor

from config import AppConfig

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
        
        # Translate origin to center of widget
        painter.translate(self.width() / 2, self.height() / 2)
        painter.rotate(self.angle)
        
        color = QColor("#3b82f6")  # Modern electric blue
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
        self.resize(720, 620)
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

        # Header Title
        title_label = QLabel("🔍 PixKura Search & Safety Engine")
        title_label.setStyleSheet("font-size: 16px; font-weight: bold; color: #60a5fa;")
        layout.addWidget(title_label)

        sub_label = QLabel("รองรับการค้นหาแบบ Danbooru Boolean Query (AND, OR, NOT), การคัดกรองเรตติ้งความปลอดภัย และประเภท/ขนาดไฟล์")
        sub_label.setStyleSheet("font-size: 12px; color: #9ca3af;")
        sub_label.setWordWrap(True)
        layout.addWidget(sub_label)

        # Rich text documentation
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

        <h3>🛡️ 1. Safety & Rating Filters (การคัดกรองความปลอดภัย)</h3>
        <p>สามารถเลือกจากช่อง <b>Safety</b> ในแถบเครื่องมือ หรือพิมพ์คีย์เวิร์ดในช่องค้นหา:</p>
        <table>
            <tr><th>คำสั่ง</th><th>ประเภท</th><th>คำอธิบาย</th></tr>
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

        <h3>👤 2. Characters & Series (ตัวละครและซีรีส์)</h3>
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
                <td>ค้นหาชื่อนักวาดหรือโฟลเดอร์</td>
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
                <td><code>type:gif</code>, <code>type:png</code>, <code>type:video</code></td>
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

        # Clickable quick example chips
        layout.addWidget(QLabel("💡 Quick Try (คลิกตัวอย่างเพื่อใส่ลงช่องค้นหาทันที):"))
        chips_layout = QHBoxLayout()
        chips_layout.setSpacing(8)

        examples = [
            ("🛡️ SFW Only", "rating:sfw", "chip-sfw"),
            ("🔞 NSFW Only", "rating:nsfw", "chip-nsfw"),
            ("👗 Swimsuit -Glasses", "swimsuit -glasses", "chip"),
            ("👤 Miku SFW", "char:hatsune_miku rating:sfw", "chip-sfw"),
            ("🎬 GIF Only", "type:gif", "chip"),
            ("📦 Large (>5MB)", "size:>5mb", "chip"),
        ]

        for label_text, query_text, chip_class in examples:
            btn = QPushButton(label_text)
            btn.setProperty("class", chip_class)
            btn.setStyleSheet(self.styleSheet())
            btn.clicked.connect(lambda checked, q=query_text: self.apply_example(q))
            chips_layout.addWidget(btn)

        layout.addLayout(chips_layout)

        # Footer Buttons
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

