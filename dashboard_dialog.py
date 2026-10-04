import os
import sys
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QProgressBar,
    QTabWidget, QWidget, QScrollArea, QFrame, QGridLayout, QApplication
)
from PyQt6.QtCore import Qt, pyqtSignal, QThread
from PyQt6.QtGui import QFont, QColor

from config import DB_FILE
from database import get_dashboard_analytics

class AnalyticsWorker(QThread):
    finished = pyqtSignal(dict)

    def __init__(self, db_file):
        super().__init__()
        self.db_file = db_file

    def run(self):
        data = get_dashboard_analytics(self.db_file)
        self.finished.emit(data)


from components import MinimizableDialog


class DashboardDialog(MinimizableDialog):
    """
    Visual Dashboard & Interactive Tag Cloud for PixKura.
    Displays KPI statistics, SFW/NSFW safety ratio, character leaderboards,
    and a clickable tag cloud.
    """
    query_selected = pyqtSignal(str)

    def __init__(self, parent=None, on_apply_query=None):
        super().__init__(parent)
        self.on_apply_query = on_apply_query
        self.setWindowTitle("📊 Library Visual Dashboard & Tag Cloud (สถิติคลังภาพและกลุ่มแท็ก)")
        self.resize(920, 720)
        self.setStyleSheet("""
            QDialog {
                background-color: #121216;
                color: #e5e7eb;
            }
            QFrame.kpi-card {
                background-color: #1a1a24;
                border: 1px solid #2d2d38;
                border-radius: 10px;
                padding: 12px;
            }
            QFrame.kpi-card:hover {
                border-color: #3b82f6;
            }
            QLabel.kpi-title {
                color: #9ca3af;
                font-size: 12px;
                font-weight: 600;
                text-transform: uppercase;
            }
            QLabel.kpi-value {
                color: #f9fafb;
                font-size: 24px;
                font-weight: bold;
                margin-top: 4px;
            }
            QLabel.kpi-sub {
                color: #6b7280;
                font-size: 11px;
                margin-top: 2px;
            }
            QTabWidget::pane {
                border: 1px solid #2d2d38;
                background-color: #16161f;
                border-radius: 8px;
            }
            QTabBar::tab {
                background-color: #1a1a24;
                color: #9ca3af;
                padding: 8px 20px;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
                margin-right: 4px;
                font-weight: 600;
            }
            QTabBar::tab:selected {
                background-color: #252538;
                color: #60a5fa;
                border-bottom: 2px solid #3b82f6;
            }
            QTabBar::tab:hover {
                color: #ffffff;
            }
            QPushButton.tag-chip {
                background-color: #222230;
                color: #93c5fd;
                border: 1px solid #3b82f6;
                border-radius: 12px;
                padding: 4px 10px;
                font-size: 12px;
            }
            QPushButton.tag-chip:hover {
                background-color: #3b82f6;
                color: #ffffff;
            }
            QPushButton.tag-chip-hot {
                background-color: #3b2230;
                color: #fca5a5;
                border: 1px solid #ef4444;
                border-radius: 14px;
                padding: 6px 14px;
                font-size: 13px;
                font-weight: bold;
            }
            QPushButton.tag-chip-hot:hover {
                background-color: #ef4444;
                color: #ffffff;
            }
            QProgressBar {
                border: 1px solid #2d2d38;
                border-radius: 6px;
                background-color: #1e1e28;
                height: 14px;
                text-align: center;
                color: white;
                font-size: 10px;
                font-weight: bold;
            }
            QProgressBar::chunk {
                background-color: #3b82f6;
                border-radius: 5px;
            }
            QScrollArea {
                border: none;
                background: transparent;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)

        # Header with Title and Refresh
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        lbl_title = QLabel("📊 Library Visual Dashboard & Analytics")
        lbl_title.setStyleSheet("font-size: 18px; font-weight: bold; color: #60a5fa;")
        lbl_sub = QLabel("วิเคราะห์สถิติคลังภาพ Pixiv, สัดส่วนความปลอดภัย (SFW/NSFW), อันดับตัวละครยอดนิยม และ Tag Cloud")
        lbl_sub.setStyleSheet("font-size: 12px; color: #9ca3af;")
        title_box.addWidget(lbl_title)
        title_box.addWidget(lbl_sub)
        header.addLayout(title_box)
        header.addStretch()

        self.btn_refresh = QPushButton("🔄 Refresh Data")
        self.btn_refresh.clicked.connect(self.load_data)
        header.addWidget(self.btn_refresh)
        layout.addLayout(header)

        # KPI Cards Row
        kpi_row = QHBoxLayout()
        kpi_row.setSpacing(12)

        self.card_files = self.create_kpi_card("📁 Total Artworks", "0", "0 Folders")
        self.card_tagged = self.create_kpi_card("🤖 AI Tagged", "0", "0% Coverage")
        self.card_sfw = self.create_kpi_card("🟢 SFW Safe Art", "0", "Safe for Colab", "#34d399")
        self.card_nsfw = self.create_kpi_card("🔞 NSFW (18+)", "0", "Adult Content", "#f87171")

        kpi_row.addWidget(self.card_files['frame'])
        kpi_row.addWidget(self.card_tagged['frame'])
        kpi_row.addWidget(self.card_sfw['frame'])
        kpi_row.addWidget(self.card_nsfw['frame'])
        layout.addLayout(kpi_row)

        # Safety Segmented Progress Bar
        safety_box = QVBoxLayout()
        safety_header = QHBoxLayout()
        safety_title = QLabel("🛡️ Content Safety Ratio (สัดส่วนความปลอดภัย)")
        safety_title.setStyleSheet("font-size: 12px; font-weight: bold; color: #d1d5db;")
        self.lbl_safety_ratio = QLabel("SFW: 0% | NSFW: 0%")
        self.lbl_safety_ratio.setStyleSheet("font-size: 12px; color: #9ca3af;")
        safety_header.addWidget(safety_title)
        safety_header.addStretch()
        safety_header.addWidget(self.lbl_safety_ratio)
        safety_box.addLayout(safety_header)

        self.safety_bar = QProgressBar()
        self.safety_bar.setRange(0, 100)
        self.safety_bar.setValue(100)
        self.safety_bar.setStyleSheet("""
            QProgressBar {
                border: 1px solid #2d2d38;
                border-radius: 6px;
                background-color: #7f1d1d;
                height: 16px;
                text-align: center;
                color: #ffffff;
                font-weight: bold;
                font-size: 10px;
            }
            QProgressBar::chunk {
                background-color: #059669;
                border-radius: 5px;
            }
        """)
        safety_box.addWidget(self.safety_bar)
        layout.addLayout(safety_box)

        # Tabs for Detailed Analytics
        self.tabs = QTabWidget()
        
        # Tab 1: Characters & Series
        self.tab_chars = QWidget()
        self.setup_characters_tab()
        self.tabs.addTab(self.tab_chars, "🏆 Top Characters & Series")

        # Tab 2: Interactive Tag Cloud
        self.tab_tags = QWidget()
        self.setup_tag_cloud_tab()
        self.tabs.addTab(self.tab_tags, "☁️ Interactive Tag Cloud")

        layout.addWidget(self.tabs)

        # Footer Actions
        footer = QHBoxLayout()
        btn_view_sfw = QPushButton("🟢 View All SFW (ดูภาพปลอดภัย)")
        btn_view_sfw.clicked.connect(lambda: self.apply_query_and_close("rating:sfw"))
        footer.addWidget(btn_view_sfw)

        btn_view_nsfw = QPushButton("🔞 View All NSFW (ดูภาพ 18+)")
        btn_view_nsfw.clicked.connect(lambda: self.apply_query_and_close("rating:nsfw"))
        footer.addWidget(btn_view_nsfw)

        footer.addStretch()
        btn_close = QPushButton("ปิดหน้าต่าง (Close)")
        btn_close.clicked.connect(self.accept)
        footer.addWidget(btn_close)
        layout.addLayout(footer)

        # Trigger data loading
        self.load_data()

    def create_kpi_card(self, title, val, sub, val_color="#f9fafb"):
        frame = QFrame()
        frame.setProperty("class", "kpi-card")
        l = QVBoxLayout(frame)
        l.setContentsMargins(12, 10, 12, 10)
        l.setSpacing(2)

        lbl_t = QLabel(title)
        lbl_t.setProperty("class", "kpi-title")
        l.addWidget(lbl_t)

        lbl_v = QLabel(val)
        lbl_v.setProperty("class", "kpi-value")
        lbl_v.setStyleSheet(f"color: {val_color}; font-size: 22px; font-weight: bold;")
        l.addWidget(lbl_v)

        lbl_s = QLabel(sub)
        lbl_s.setProperty("class", "kpi-sub")
        l.addWidget(lbl_s)

        return {'frame': frame, 'val': lbl_v, 'sub': lbl_s}

    def setup_characters_tab(self):
        tab_layout = QHBoxLayout(self.tab_chars)
        tab_layout.setContentsMargins(12, 12, 12, 12)
        tab_layout.setSpacing(16)

        # Left Column: Top Characters
        col_chars = QVBoxLayout()
        lbl_c = QLabel("👤 Top Characters (25 อันดับตัวละครที่มีภาพเยอะที่สุด)")
        lbl_c.setStyleSheet("font-size: 13px; font-weight: bold; color: #60a5fa; margin-bottom: 6px;")
        col_chars.addWidget(lbl_c)

        scroll_c = QScrollArea()
        scroll_c.setWidgetResizable(True)
        self.widget_chars = QWidget()
        self.layout_chars = QVBoxLayout(self.widget_chars)
        self.layout_chars.setSpacing(6)
        self.layout_chars.addStretch()
        scroll_c.setWidget(self.widget_chars)
        col_chars.addWidget(scroll_c)
        tab_layout.addLayout(col_chars, 3)

        # Right Column: Top Series
        col_series = QVBoxLayout()
        lbl_s = QLabel("📚 Top Series / Copyright (ซีรีส์ยอดนิยม)")
        lbl_s.setStyleSheet("font-size: 13px; font-weight: bold; color: #a78bfa; margin-bottom: 6px;")
        col_series.addWidget(lbl_s)

        scroll_s = QScrollArea()
        scroll_s.setWidgetResizable(True)
        self.widget_series = QWidget()
        self.layout_series = QVBoxLayout(self.widget_series)
        self.layout_series.setSpacing(6)
        self.layout_series.addStretch()
        scroll_s.setWidget(self.widget_series)
        col_series.addWidget(scroll_s)
        tab_layout.addLayout(col_series, 2)

    def setup_tag_cloud_tab(self):
        tab_layout = QVBoxLayout(self.tab_tags)
        tab_layout.setContentsMargins(12, 12, 12, 12)
        tab_layout.setSpacing(8)

        lbl = QLabel("🏷️ Tag Cloud (คลิกที่แท็กเพื่อนำไปค้นหาทันที):")
        lbl.setStyleSheet("font-size: 13px; font-weight: bold; color: #38bdf8;")
        tab_layout.addWidget(lbl)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.widget_cloud = QWidget()
        self.layout_cloud = QGridLayout(self.widget_cloud)
        self.layout_cloud.setSpacing(8)
        scroll.setWidget(self.widget_cloud)
        tab_layout.addWidget(scroll)

    def load_data(self):
        self.btn_refresh.setEnabled(False)
        self.btn_refresh.setText("⏳ Loading...")
        self.worker = AnalyticsWorker(DB_FILE)
        self.worker.finished.connect(self.on_data_loaded)
        self.worker.start()

    def on_data_loaded(self, data):
        self.btn_refresh.setEnabled(True)
        self.btn_refresh.setText("🔄 Refresh Data")

        tot_files = data.get("total_files", 0)
        tot_folders = data.get("total_folders", 0)
        tagged = data.get("tagged_files", 0)
        sfw = data.get("sfw_count", 0)
        nsfw = data.get("nsfw_count", 0)

        # Update KPI Cards
        self.card_files['val'].setText(f"{tot_files:,}")
        self.card_files['sub'].setText(f"{tot_folders:,} Artist Folders")

        coverage = (tagged / tot_files * 100) if tot_files > 0 else 0
        self.card_tagged['val'].setText(f"{tagged:,}")
        self.card_tagged['sub'].setText(f"{coverage:.1f}% Coverage")

        sfw_pct = (sfw / tagged * 100) if tagged > 0 else 0
        nsfw_pct = (nsfw / tagged * 100) if tagged > 0 else 0

        self.card_sfw['val'].setText(f"{sfw:,}")
        self.card_sfw['sub'].setText(f"{sfw_pct:.1f}% Safe Art")

        self.card_nsfw['val'].setText(f"{nsfw:,}")
        self.card_nsfw['sub'].setText(f"{nsfw_pct:.1f}% Adult Art")

        # Update Safety Bar
        self.safety_bar.setValue(int(sfw_pct))
        self.lbl_safety_ratio.setText(f"🟢 SFW: {sfw:,} ({sfw_pct:.1f}%)  |  🔞 NSFW: {nsfw:,} ({nsfw_pct:.1f}%)")

        # Populate Characters Leaderboard
        self.clear_layout(self.layout_chars)
        top_chars = data.get("top_characters", [])
        max_char_cnt = top_chars[0][1] if top_chars else 1

        for rank, (c_name, cnt) in enumerate(top_chars, 1):
            row_w = QWidget()
            row_l = QHBoxLayout(row_w)
            row_l.setContentsMargins(4, 2, 4, 2)
            row_l.setSpacing(8)

            lbl_rank = QLabel(f"#{rank}")
            lbl_rank.setStyleSheet("color: #9ca3af; font-size: 11px; font-weight: bold; width: 24px;")
            row_l.addWidget(lbl_rank)

            display_name = c_name.replace('_', ' ').title()
            btn_char = QPushButton(f"{display_name} ({cnt:,})")
            btn_char.setStyleSheet("""
                QPushButton {
                    background-color: #222230;
                    color: #93c5fd;
                    border: 1px solid #3b82f6;
                    border-radius: 6px;
                    padding: 4px 8px;
                    text-align: left;
                    font-weight: 600;
                }
                QPushButton:hover {
                    background-color: #3b82f6;
                    color: #ffffff;
                }
            """)
            btn_char.clicked.connect(lambda checked, name=c_name: self.apply_query_and_close(f"char:{name}"))
            row_l.addWidget(btn_char, 1)

            # Mini Bar
            bar = QProgressBar()
            bar.setRange(0, max_char_cnt)
            bar.setValue(cnt)
            bar.setTextVisible(False)
            bar.setFixedWidth(80)
            bar.setFixedHeight(8)
            bar.setStyleSheet("""
                QProgressBar {
                    background-color: #1e1e28;
                    border-radius: 4px;
                    border: none;
                }
                QProgressBar::chunk {
                    background-color: #3b82f6;
                    border-radius: 4px;
                }
            """)
            row_l.addWidget(bar)
            self.layout_chars.addWidget(row_w)

        self.layout_chars.addStretch()

        # Populate Top Series
        self.clear_layout(self.layout_series)
        top_series = data.get("top_series", [])
        for rank, (s_name, cnt) in enumerate(top_series, 1):
            btn_s = QPushButton(f"#{rank} {s_name.replace('_', ' ').title()} ({cnt:,})")
            btn_s.setStyleSheet("""
                QPushButton {
                    background-color: #201e2c;
                    color: #c4b5fd;
                    border: 1px solid #8b5cf6;
                    border-radius: 6px;
                    padding: 5px 10px;
                    text-align: left;
                    font-weight: 500;
                }
                QPushButton:hover {
                    background-color: #8b5cf6;
                    color: #ffffff;
                }
            """)
            btn_s.clicked.connect(lambda checked, name=s_name: self.apply_query_and_close(f"series:{name}"))
            self.layout_series.addWidget(btn_s)
        self.layout_series.addStretch()

        # Populate Tag Cloud
        self.clear_layout(self.layout_cloud)
        top_tags = data.get("top_tags", [])
        
        # Grid of tag chips (4 columns)
        cols = 4
        for idx, (t_name, cnt) in enumerate(top_tags):
            is_hot = idx < 8
            btn_tag = QPushButton(f"{t_name} ({cnt:,})")
            btn_tag.setProperty("class", "tag-chip-hot" if is_hot else "tag-chip")
            btn_tag.setStyleSheet(self.styleSheet())
            btn_tag.clicked.connect(lambda checked, name=t_name: self.apply_query_and_close(f"tag:{name}"))
            
            row = idx // cols
            col = idx % cols
            self.layout_cloud.addWidget(btn_tag, row, col)

    def clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

    def apply_query_and_close(self, query):
        if self.on_apply_query:
            self.on_apply_query(query)
            self.accept()
        else:
            self.query_selected.emit(query)
            self.accept()

    def closeEvent(self, event):
        if hasattr(self, 'worker') and self.worker.isRunning():
            self.worker.wait(500)
        super().closeEvent(event)

