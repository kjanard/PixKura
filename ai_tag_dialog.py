import os
import time
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QProgressBar, QTextEdit, QSlider, QCheckBox, QComboBox,
    QGroupBox, QMessageBox, QFrame, QWidget, QSplitter, QFileDialog
)
from PyQt6.QtCore import Qt, QThreadPool, pyqtSlot, QThread, pyqtSignal, QTimer, QObject, QRunnable
from PyQt6.QtGui import QFont, QColor, QIcon, QPixmap

from config import (
    DB_FILE, DEFAULT_CHARACTER_THRESHOLD, DEFAULT_GENERAL_THRESHOLD,
    AVAILABLE_AI_MODELS, DEFAULT_MODEL_KEY
)
from tagger import WD14Tagger, get_model_paths
from workers import AiTaggingWorker
from database import get_tag_stats
from utils import detect_drive_media_type

class ModelDownloadThread(QThread):
    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(bool, str)

    def __init__(self, model_key=DEFAULT_MODEL_KEY, parent=None):
        super().__init__(parent)
        self.model_key = model_key
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def run(self):
        try:
            WD14Tagger.download_models(
                model_key=self.model_key,
                progress_callback=lambda d, t, m: self.progress.emit(d, t, m),
                cancel_check=lambda: self.cancelled
            )
            self.finished.emit(True, "ดาวน์โหลดโมเดล AI สำเร็จเรียบร้อยแล้ว!")
        except Exception as e:
            self.finished.emit(False, str(e))

class TagStatsSignals(QObject):
    finished = pyqtSignal(dict)

class TagStatsWorker(QRunnable):
    def __init__(self, db_file, force_refresh=False):
        super().__init__()
        self.db_file = db_file
        self.force_refresh = force_refresh
        self.signals = TagStatsSignals()

    @pyqtSlot()
    def run(self):
        try:
            stats = get_tag_stats(self.db_file, force_refresh=self.force_refresh)
            self.signals.finished.emit(stats)
        except Exception:
            pass

from components import MinimizableDialog

class AiTagDialog(MinimizableDialog):
    def __init__(self, parent=None, initial_root_path=None, target_artist_name=None):
        super().__init__(parent)
        self.initial_root_path = initial_root_path
        self.target_artist_name = target_artist_name
        self.worker = None
        self.download_thread = None
        self.thread_pool = QThreadPool.globalInstance()
        self.scan_start_time = None
        self.timer_stopwatch = QTimer(self)
        self.timer_stopwatch.timeout.connect(self.update_stopwatch)

        # Determine library root
        self.library_root = None
        if parent and hasattr(parent, 'current_folder') and parent.current_folder:
            self.library_root = parent.current_folder
        elif initial_root_path and not target_artist_name:
            self.library_root = initial_root_path
        else:
            try:
                import json
                with open('config.json', 'r', encoding='utf-8') as f:
                    self.library_root = json.load(f).get('last_folder')
            except Exception:
                pass
        
        self.setWindowTitle("🤖 AI Character & Tag Scanner (WD14 Tagger)")
        self.resize(780, 680)
        self.setMinimumSize(640, 520)
        
        self.init_ui()
        self.check_system_status()

    def init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(12)
        main_layout.setContentsMargins(16, 16, 16, 16)

        # 1. Header Card
        header_card = QFrame()
        header_card.setObjectName("header_card")
        header_card.setStyleSheet("""
            #header_card {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #1e293b, stop:1 #0f172a);
                border: 1px solid #334155;
                border-radius: 8px;
                padding: 10px;
            }
        """)
        h_layout = QVBoxLayout(header_card)
        h_layout.setContentsMargins(10, 8, 10, 8)
        
        title_label = QLabel("✨ AI Character & Tag Classifier (Local DirectML / GPU)")
        title_label.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        title_label.setStyleSheet("color: #38bdf8;")
        
        self.desc_label = QLabel("ระบบวิเคราะห์ตัวละครและแท็กข้อมูลภาพอัตโนมัติด้วย AI ในเครื่อง (รองรับ NVIDIA, AMD, Intel GPU)")
        self.desc_label.setFont(QFont("Segoe UI", 9))
        self.desc_label.setStyleSheet("color: #94a3b8;")
        
        h_layout.addWidget(title_label)
        h_layout.addWidget(self.desc_label)
        main_layout.addWidget(header_card)

        # 2. Model & Device Status Card
        status_group = QGroupBox("⚙️ โมเดล AI และอุปกรณ์ประมวลผล (Model & Hardware)")
        status_layout = QVBoxLayout(status_group)

        # Model Selection Row
        model_select_row = QHBoxLayout()
        lbl_model_sel = QLabel("โมเดล AI (Model):")
        lbl_model_sel.setStyleSheet("color: #94a3b8;")
        
        self.combo_model = QComboBox()
        self.combo_model.setStyleSheet("background-color: #1e293b; color: white; border: 1px solid #475569; padding: 4px; border-radius: 4px; font-weight: bold;")
        for m_key, m_info in AVAILABLE_AI_MODELS.items():
            self.combo_model.addItem(m_info["name"], m_key)
        self.combo_model.currentIndexChanged.connect(self.on_model_changed)
        
        model_select_row.addWidget(lbl_model_sel)
        model_select_row.addWidget(self.combo_model, 1)
        status_layout.addLayout(model_select_row)

        # Model Status Row
        model_row = QHBoxLayout()
        self.model_status_label = QLabel("🔍 กำลังตรวจสอบสถานะโมเดล AI...")
        self.model_status_label.setStyleSheet("color: #e2e8f0; font-weight: bold;")
        
        self.btn_download_model = QPushButton("📥 ดาวน์โหลดโมเดล")
        self.btn_download_model.setStyleSheet("background-color: #2563eb; color: white; padding: 5px 12px; border-radius: 4px; font-weight: bold;")
        self.btn_download_model.clicked.connect(self.start_download_model)
        self.btn_download_model.setVisible(False)

        self.btn_check_update = QPushButton("🔄 ตรวจสอบการอัปเดต")
        self.btn_check_update.setStyleSheet("background-color: #334155; color: #cbd5e1; padding: 5px 10px; border-radius: 4px;")
        self.btn_check_update.clicked.connect(self.check_model_update)
        
        model_row.addWidget(self.model_status_label, 1)
        model_row.addWidget(self.btn_download_model)
        model_row.addWidget(self.btn_check_update)
        status_layout.addLayout(model_row)

        # Hardware & Execution Provider Row
        hw_row = QHBoxLayout()
        hw_label = QLabel("โหมดประมวลผล (Device):")
        hw_label.setStyleSheet("color: #94a3b8;")
        
        self.combo_provider = QComboBox()
        self.combo_provider.setStyleSheet("background-color: #1e293b; color: white; border: 1px solid #475569; padding: 4px; border-radius: 4px;")
        self.combo_provider.currentIndexChanged.connect(self.on_provider_changed)
        
        self.lbl_hw_info = QLabel("")
        self.lbl_hw_info.setStyleSheet("color: #10b981; font-weight: bold;")
        
        hw_row.addWidget(hw_label)
        hw_row.addWidget(self.combo_provider, 1)
        hw_row.addWidget(self.lbl_hw_info)
        status_layout.addLayout(hw_row)

        # Stats summary row
        self.lbl_stats = QLabel("📊 กำลังโหลดสถิติแท็กเดิมในฐานข้อมูล...")
        self.lbl_stats.setStyleSheet("color: #cbd5e1; font-size: 11px;")
        status_layout.addWidget(self.lbl_stats)

        main_layout.addWidget(status_group)

        # 3. Settings Card
        settings_group = QGroupBox("🎯 ตัวเลือกการสแกน (Scan Options)")
        settings_layout = QVBoxLayout(settings_group)

        # Scope Selection
        scope_row = QHBoxLayout()
        lbl_scope = QLabel("ขอบเขต:")
        lbl_scope.setStyleSheet("color: #94a3b8;")
        self.combo_scope = QComboBox()
        self.combo_scope.setStyleSheet("background-color: #1e293b; color: white; border: 1px solid #475569; padding: 4px; border-radius: 4px;")
        self.combo_scope.addItem("📁 ทั้งคลังรูปภาพ (All Files in Library)", self.library_root)
        
        if self.initial_root_path and os.path.abspath(self.initial_root_path) != os.path.abspath(self.library_root or ""):
            name = self.target_artist_name or os.path.basename(self.initial_root_path)
            self.combo_scope.addItem(f"👤 เฉพาะศิลปิน: {name}", self.initial_root_path)
            self.combo_scope.setCurrentIndex(1)

        self.btn_browse_artist = QPushButton("📂 เลือกโฟลเดอร์ศิลปิน...")
        self.btn_browse_artist.setStyleSheet("""
            QPushButton {
                background-color: #334155;
                color: #e2e8f0;
                padding: 4px 10px;
                border-radius: 4px;
                font-size: 11px;
            }
            QPushButton:hover { background-color: #475569; }
        """)
        self.btn_browse_artist.clicked.connect(self.browse_specific_folder)

        scope_row.addWidget(lbl_scope)
        scope_row.addWidget(self.combo_scope, 1)
        scope_row.addWidget(self.btn_browse_artist)
        settings_layout.addLayout(scope_row)


        # Scan Mode Row (Quick Mode vs Full Re-scan)
        mode_row = QHBoxLayout()
        lbl_mode = QLabel("โหมดการสแกน (Scan Mode):")
        lbl_mode.setStyleSheet("color: #94a3b8;")
        self.combo_scan_mode = QComboBox()
        self.combo_scan_mode.setStyleSheet("background-color: #1e293b; color: white; border: 1px solid #475569; padding: 4px; border-radius: 4px; font-weight: bold;")
        self.combo_scan_mode.addItem("⚡ โหมดด่วน (Quick Mode) - สแกนเฉพาะรูปที่ยังไม่มีแท็ก [แนะนำ]", False)
        self.combo_scan_mode.addItem("🔄 สแกนใหม่ทั้งหมด (Full Re-scan) - สแกนทับทุกรูปเพื่ออัปเดตแท็กใหม่", True)
        mode_row.addWidget(lbl_mode)
        mode_row.addWidget(self.combo_scan_mode, 1)
        settings_layout.addLayout(mode_row)

        # Threshold Slider Row
        thresh_row = QHBoxLayout()
        lbl_thresh = QLabel("ความมั่นใจขั้นต่ำ (Confidence):")
        lbl_thresh.setStyleSheet("color: #94a3b8;")
        self.slider_thresh = QSlider(Qt.Orientation.Horizontal)
        self.slider_thresh.setRange(20, 80)
        self.slider_thresh.setValue(int(DEFAULT_CHARACTER_THRESHOLD * 100))
        self.lbl_thresh_val = QLabel(f"{DEFAULT_CHARACTER_THRESHOLD:.2f}")
        self.lbl_thresh_val.setStyleSheet("color: #38bdf8; font-weight: bold; min-width: 35px;")
        self.slider_thresh.valueChanged.connect(lambda v: self.lbl_thresh_val.setText(f"{v/100:.2f}"))

        thresh_row.addWidget(lbl_thresh)
        thresh_row.addWidget(self.slider_thresh, 1)
        thresh_row.addWidget(self.lbl_thresh_val)
        settings_layout.addLayout(thresh_row)

        # Precision Mode Row
        prec_row = QHBoxLayout()
        lbl_prec = QLabel("ความแม่นยำ (Precision):")
        lbl_prec.setStyleSheet("color: #94a3b8;")
        self.combo_prec = QComboBox()
        self.combo_prec.setStyleSheet("background-color: #1e293b; color: white; border: 1px solid #475569; padding: 4px; border-radius: 4px;")
        self.combo_prec.addItem("⚡ FP16 Half-Precision (เร็วที่สุด - แนะนำสำหรับ AMD Radeon / NVIDIA RTX)", True)
        self.combo_prec.addItem("🔹 FP32 Standard (Full Precision 32-bit)", False)
        prec_row.addWidget(lbl_prec)
        prec_row.addWidget(self.combo_prec, 1)
        settings_layout.addLayout(prec_row)

        # Storage Drive Profile Row (Auto Detect HDD / SSD)
        storage_row = QHBoxLayout()
        lbl_storage = QLabel("แหล่งเก็บข้อมูล (Storage):")
        lbl_storage.setStyleSheet("color: #94a3b8;")
        self.combo_storage = QComboBox()
        self.combo_storage.setStyleSheet("background-color: #1e293b; color: white; border: 1px solid #475569; padding: 4px; border-radius: 4px;")
        storage_row.addWidget(lbl_storage)
        storage_row.addWidget(self.combo_storage, 1)
        settings_layout.addLayout(storage_row)

        self.update_storage_options()
        self.combo_scope.currentIndexChanged.connect(self.update_storage_options)

        main_layout.addWidget(settings_group)

        # 4. Progress & Live Preview Card
        prog_group = QGroupBox("📈 ความคืบหน้าและการประมวลผล (Live Progress)")
        prog_layout = QVBoxLayout(prog_group)

        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                background-color: #1e293b;
                border: 1px solid #334155;
                border-radius: 6px;
                text-align: center;
                color: white;
                font-weight: bold;
                height: 22px;
            }
            QProgressBar::chunk {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #3b82f6, stop:1 #06b6d4);
                border-radius: 5px;
            }
        """)
        prog_layout.addWidget(self.progress_bar)

        # Metrics row (Counts, FPS, Elapsed, ETA)
        metrics_row = QHBoxLayout()
        self.lbl_processed_count = QLabel("ไฟล์: 0 / 0")
        self.lbl_processed_count.setStyleSheet("color: #e2e8f0; font-weight: bold;")
        self.lbl_speed = QLabel("ความเร็ว: 0.0 FPS")
        self.lbl_speed.setStyleSheet("color: #38bdf8; font-weight: bold;")
        self.lbl_elapsed = QLabel("⏱️ ใช้ไป: 00:00")
        self.lbl_elapsed.setStyleSheet("color: #10b981; font-weight: bold;")
        self.lbl_eta = QLabel("⏳ เหลือ: --:--")
        self.lbl_eta.setStyleSheet("color: #a855f7; font-weight: bold;")
        
        metrics_row.addWidget(self.lbl_processed_count, 1)
        metrics_row.addWidget(self.lbl_speed, 1)
        metrics_row.addWidget(self.lbl_elapsed, 1)
        metrics_row.addWidget(self.lbl_eta, 1)
        prog_layout.addLayout(metrics_row)

        # Current Tagged Preview Label
        self.lbl_current_detected = QLabel("พร้อมเริ่มสแกน...")
        self.lbl_current_detected.setStyleSheet("color: #94a3b8; font-style: italic; font-size: 11px;")
        self.lbl_current_detected.setWordWrap(True)
        prog_layout.addWidget(self.lbl_current_detected)

        # Log Window
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setStyleSheet("""
            QTextEdit {
                background-color: #0f172a;
                color: #e2e8f0;
                border: 1px solid #1e293b;
                border-radius: 4px;
                font-family: 'Consolas', 'Courier New', monospace;
                font-size: 11px;
            }
        """)
        self.log_text.setMaximumHeight(130)
        prog_layout.addWidget(self.log_text)

        main_layout.addWidget(prog_group)

        # 5. Bottom Action Buttons
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)

        self.btn_start = QPushButton("▶ เริ่มสแกน AI (Start Scan)")
        self.btn_start.setStyleSheet("""
            QPushButton {
                background-color: #059669;
                color: white;
                font-size: 12px;
                font-weight: bold;
                padding: 8px 18px;
                border-radius: 6px;
            }
            QPushButton:hover { background-color: #10b981; }
            QPushButton:disabled { background-color: #334155; color: #64748b; }
        """)
        self.btn_start.clicked.connect(self.start_scan)

        self.btn_pause = QPushButton("⏸️ พักชั่วคราว (Pause)")
        self.btn_pause.setStyleSheet("""
            QPushButton {
                background-color: #d97706;
                color: white;
                font-size: 12px;
                font-weight: bold;
                padding: 8px 16px;
                border-radius: 6px;
            }
            QPushButton:hover { background-color: #f59e0b; }
            QPushButton:disabled { background-color: #334155; color: #64748b; }
        """)
        self.btn_pause.setEnabled(False)
        self.btn_pause.clicked.connect(self.toggle_pause)

        self.btn_stop = QPushButton("⏹️ ยกเลิก / หยุด (Stop)")
        self.btn_stop.setStyleSheet("""
            QPushButton {
                background-color: #dc2626;
                color: white;
                font-size: 12px;
                font-weight: bold;
                padding: 8px 16px;
                border-radius: 6px;
            }
            QPushButton:hover { background-color: #ef4444; }
            QPushButton:disabled { background-color: #334155; color: #64748b; }
        """)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_scan)

        self.btn_close = QPushButton("ปิด (Close)")
        self.btn_close.setStyleSheet("""
            QPushButton {
                background-color: #334155;
                color: #e2e8f0;
                font-size: 12px;
                padding: 8px 16px;
                border-radius: 6px;
            }
            QPushButton:hover { background-color: #475569; }
        """)
        self.btn_close.clicked.connect(self.close)

        btn_layout.addWidget(self.btn_start)
        btn_layout.addWidget(self.btn_pause)
        btn_layout.addWidget(self.btn_stop)
        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_close)

        main_layout.addLayout(btn_layout)

    def check_system_status(self):
        """Checks model availability and hardware execution providers."""
        # 1. Check Hardware
        providers_info = WD14Tagger.get_available_providers()
        self.combo_provider.clear()
        
        if providers_info["has_directml"]:
            self.combo_provider.addItem("🚀 GPU Acceleration (DirectML - NVIDIA / AMD / Intel)", True)
            self.lbl_hw_info.setText("✅ GPU DirectML พร้อมใช้งาน")
        elif providers_info["has_cuda"]:
            self.combo_provider.addItem("🚀 GPU Acceleration (NVIDIA CUDA)", True)
            self.lbl_hw_info.setText("✅ GPU CUDA พร้อมใช้งาน")
        else:
            self.lbl_hw_info.setText("⚠️ ไม่พบ GPU ที่รองรับ")
        
        self.combo_provider.addItem("🐢 CPU Only (ช้ากว่ามาก)", False)

        # 2. Check Model Installation
        self.on_model_changed(self.combo_model.currentIndex())

        # 3. Update Database Tag Stats
        self.refresh_stats()

    def on_model_changed(self, idx):
        model_key = self.combo_model.currentData() or DEFAULT_MODEL_KEY
        is_installed = WD14Tagger.is_model_installed(model_key)
        model_name = AVAILABLE_AI_MODELS.get(model_key, {}).get("short_name", model_key)
        if is_installed:
            self.model_status_label.setText(f"✅ ติดตั้งโมเดล {model_name} เรียบร้อยพร้อมใช้งาน")
            self.model_status_label.setStyleSheet("color: #10b981; font-weight: bold;")
            self.btn_download_model.setVisible(False)
            self.btn_start.setEnabled(True)
        else:
            self.model_status_label.setText(f"⚠️ ยังไม่ได้ดาวน์โหลด {model_name} (กดดาวน์โหลดก่อนใช้งาน)")
            self.model_status_label.setStyleSheet("color: #f59e0b; font-weight: bold;")
            self.btn_download_model.setText(f"📥 ดาวน์โหลด {model_name} (~370 MB)")
            self.btn_download_model.setVisible(True)
            self.btn_start.setEnabled(False)

    def update_storage_options(self):
        target_root = self.combo_scope.currentData() if hasattr(self, 'combo_scope') else None
        if not target_root:
            try:
                import json
                with open('config.json', 'r', encoding='utf-8') as f:
                    target_root = json.load(f).get('last_folder', '')
            except Exception:
                target_root = ''

        detected = detect_drive_media_type(target_root) if target_root else "UNKNOWN"
        drive_letter = os.path.splitdrive(os.path.abspath(target_root))[0] if target_root else ""
        if drive_letter and not drive_letter.endswith(':'):
            drive_letter += ':'

        current_val = self.combo_storage.currentData() if hasattr(self, 'combo_storage') and self.combo_storage.count() > 0 else "auto"

        if hasattr(self, 'combo_storage'):
            self.combo_storage.clear()
            if detected == "HDD":
                auto_label = f"🤖 อัตโนมัติ (Auto Detect - ตรวจพบ {drive_letter} เป็น HDD จานหมุน) [แนะนำ]"
            elif detected == "SSD":
                auto_label = f"🤖 อัตโนมัติ (Auto Detect - ตรวจพบ {drive_letter} เป็น SSD ความเร็วสูง) [แนะนำ]"
            else:
                auto_label = "🤖 อัตโนมัติ (Auto Detect - ปรับตามประเภทไดรฟ์) [แนะนำ]"

            self.combo_storage.addItem(auto_label, "auto")
            self.combo_storage.addItem("💾 HDD จานหมุน (Low I/O Concurrency / 3-4 Threads ถนอมหัวอ่าน)", "hdd")
            self.combo_storage.addItem("⚡ SSD / NVMe (High Concurrency / 16 Threads เร่งสปีดเต็มพิกัด)", "ssd")

            idx = self.combo_storage.findData(current_val)
            if idx >= 0:
                self.combo_storage.setCurrentIndex(idx)

    def update_stopwatch(self):
        if self.scan_start_time is not None:
            elapsed = time.time() - self.scan_start_time
            mins, secs = divmod(int(elapsed), 60)
            hrs, mins = divmod(mins, 60)
            if hrs > 0:
                self.lbl_elapsed.setText(f"⏱️ ใช้ไป: {hrs:02d}:{mins:02d}:{secs:02d}")
            else:
                self.lbl_elapsed.setText(f"⏱️ ใช้ไป: {mins:02d}:{secs:02d}")

    def refresh_stats(self, force_refresh=False):
        worker = TagStatsWorker(DB_FILE, force_refresh=force_refresh)
        worker.signals.finished.connect(self._on_stats_loaded)
        self.thread_pool.start(worker)

    def _on_stats_loaded(self, stats):
        try:
            self.lbl_stats.setText(
                f"📊 สถิติปัจจุบัน: รูปภาพที่สแกนแล้ว <b>{stats['total_tagged_files']:,}</b> ไฟล์ | "
                f"ตัวละครที่รู้จัก <b>{stats['unique_characters']:,}</b> ตัว | "
                f"แท็กทั้งหมด <b>{stats['unique_tags']:,}</b> รายการ"
            )
        except RuntimeError:
            pass

    def on_provider_changed(self, idx):
        use_gpu = self.combo_provider.currentData()
        if not use_gpu:
            QMessageBox.information(
                self, "CPU Mode Warning",
                "⚠️ คำเตือน: การประมวลผลด้วย CPU จะมีความเร็วเฉลี่ยเพียง 1 - 2 รูปต่อวินาที (ช้ากว่า GPU 10 - 20 เท่า)\n"
                "แนะนำให้ใช้ GPU DirectML เพื่อความรวดเร็วสูงสุดครับ"
            )

    def check_model_update(self):
        model_key = self.combo_model.currentData() or DEFAULT_MODEL_KEY
        model_name = AVAILABLE_AI_MODELS.get(model_key, {}).get("short_name", model_key)
        self.log_text.append(f"🔄 กำลังตรวจสอบการอัปเดตโมเดล {model_name} จาก Hugging Face...")
        has_update, remote_sz, local_sz = WD14Tagger.check_for_updates(model_key)
        if has_update:
            msg = f"มีโมเดล {model_name} เวอร์ชันใหม่บน Hugging Face (ขนาด {remote_sz/(1024*1024):.1f} MB vs ในเครื่อง {local_sz/(1024*1024):.1f} MB)\nคุณต้องการดาวน์โหลดอัปเดตใหม่หรือไม่?"
            ret = QMessageBox.question(self, "พบการอัปเดตโมเดล", msg, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if ret == QMessageBox.StandardButton.Yes:
                self.start_download_model()
        else:
            self.log_text.append(f"✅ โมเดล {model_name} ในเครื่องเป็นเวอร์ชันล่าสุดแล้ว")
            QMessageBox.information(self, "โมเดลเวอร์ชันล่าสุด", f"✅ โมเดล {model_name} ในเครื่องเป็นเวอร์ชันล่าสุดเรียบร้อยแล้วครับ")

    def start_download_model(self):
        model_key = self.combo_model.currentData() or DEFAULT_MODEL_KEY
        model_name = AVAILABLE_AI_MODELS.get(model_key, {}).get("short_name", model_key)
        self.btn_download_model.setEnabled(False)
        self.btn_start.setEnabled(False)
        self.combo_model.setEnabled(False)
        self.progress_bar.setValue(0)
        self.log_text.append(f"📥 เริ่มต้นการดาวน์โหลดโมเดล {model_name} จาก Hugging Face...")

        self.download_thread = ModelDownloadThread(model_key=model_key, parent=self)
        self.download_thread.progress.connect(self.on_download_progress)
        self.download_thread.finished.connect(self.on_download_finished)
        self.download_thread.start()

    def on_download_progress(self, downloaded, total, msg):
        if total > 0:
            pct = int((downloaded / total) * 100)
            self.progress_bar.setValue(pct)
            self.lbl_processed_count.setText(f"{downloaded / (1024*1024):.1f} MB / {total / (1024*1024):.1f} MB")
        self.desc_label.setText(msg)

    def on_download_finished(self, success, msg):
        self.combo_model.setEnabled(True)
        if success:
            self.on_model_changed(self.combo_model.currentIndex())
            self.progress_bar.setValue(100)
            self.log_text.append(f"🎉 {msg}")
            QMessageBox.information(self, "ดาวน์โหลดสำเร็จ", f"{msg}\nโมเดลพร้อมใช้งานทันทีครับ")
        else:
            self.btn_download_model.setEnabled(True)
            self.log_text.append(f"❌ ดาวน์โหลดล้มเหลว: {msg}")
            QMessageBox.critical(self, "เกิดข้อผิดพลาด", f"ไม่สามารถดาวน์โหลดโมเดลได้:\n{msg}")

    def browse_specific_folder(self):
        start_dir = self.library_root or ""
        folder = QFileDialog.getExistingDirectory(self, "เลือกโฟลเดอร์ศิลปินที่ต้องการสแกน", start_dir)
        if folder:
            name = os.path.basename(folder)
            item_text = f"👤 โฟลเดอร์: {name}"
            # Check if item already exists in combo
            idx = self.combo_scope.findData(folder)
            if idx >= 0:
                self.combo_scope.setCurrentIndex(idx)
            else:
                self.combo_scope.addItem(item_text, folder)
                self.combo_scope.setCurrentIndex(self.combo_scope.count() - 1)

    def start_scan(self):
        model_key = self.combo_model.currentData() or DEFAULT_MODEL_KEY
        if not WD14Tagger.is_model_installed(model_key):
            QMessageBox.warning(self, "ไม่พบโมเดล", "กรุณาดาวน์โหลดโมเดล AI ก่อนเริ่มการสแกน")
            return

        use_gpu = self.combo_provider.currentData()
        providers_info = WD14Tagger.get_available_providers()

        # Check if user requested GPU but DirectML is missing
        if use_gpu and not providers_info["has_gpu"]:
            reply = QMessageBox.warning(
                self, "ไม่พบ GPU",
                "⚠️ ไม่พบการ์ดจอที่รองรับ DirectML ในระบบ\n"
                "คุณต้องการสลับไปประมวลผลด้วย CPU แทนหรือไม่?\n\n"
                "(คำเตือน: การใช้ CPU จะใช้เวลานานและช้ากว่า GPU มาก)",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                use_gpu = False
            else:
                return

        target_root = self.combo_scope.currentData()
        char_thresh = self.slider_thresh.value() / 100.0
        rescan = self.combo_scan_mode.currentData()
        use_fp16 = self.combo_prec.currentData()
        storage_mode = self.combo_storage.currentData() or "auto"
        model_key = self.combo_model.currentData() or DEFAULT_MODEL_KEY

        self.btn_start.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_pause.setText("⏸️ พักชั่วคราว (Pause)")
        self.btn_stop.setEnabled(True)
        self.combo_model.setEnabled(False)
        self.combo_scope.setEnabled(False)
        self.combo_provider.setEnabled(False)
        self.combo_prec.setEnabled(False)
        self.combo_storage.setEnabled(False)
        self.combo_scan_mode.setEnabled(False)
        self.slider_thresh.setEnabled(False)
        self.progress_bar.setValue(0)

        # Start Stopwatch
        self.scan_start_time = time.time()
        self.lbl_elapsed.setText("⏱️ ใช้ไป: 00:00")
        self.lbl_eta.setText("⏳ เหลือ: คำนวณ...")
        self.timer_stopwatch.start(1000)

        self.worker = AiTaggingWorker(
            root_path=target_root,
            db_file=DB_FILE,
            use_gpu=use_gpu,
            use_fp16=use_fp16,
            model_key=model_key,
            storage_mode=storage_mode,
            char_threshold=char_thresh,
            gen_threshold=char_thresh,
            rescan_existing=rescan
        )
        self.worker.signals.progress.connect(self.on_scan_progress)
        self.worker.signals.item_tagged.connect(self.on_item_tagged)
        self.worker.signals.log.connect(self.on_scan_log)
        self.worker.signals.finished.connect(self.on_scan_finished)
        self.worker.signals.paused.connect(self.on_scan_paused)
        self.worker.signals.model_loading.connect(lambda msg: self.lbl_current_detected.setText(msg))

        self.thread_pool.start(self.worker)

    def toggle_pause(self):
        if not self.worker: return
        if self.worker.is_paused:
            self.worker.resume()
        else:
            self.worker.pause()

    def on_scan_paused(self, is_paused):
        if is_paused:
            self.btn_pause.setText("▶ สแกนต่อ (Resume)")
            self.lbl_speed.setText("⏸️ พักชั่วคราว")
        else:
            self.btn_pause.setText("⏸️ พักชั่วคราว (Pause)")

    def stop_scan(self):
        if self.worker:
            self.log_text.append("🛑 กำลังสั่งหยุดการสแกน...")
            self.worker.stop()
            self.btn_stop.setEnabled(False)
            self.btn_pause.setEnabled(False)

    @pyqtSlot(int, int, str, float, float)
    def on_scan_progress(self, current, total, filename, fps, eta_sec):
        pct = int((current / total) * 100) if total > 0 else 0
        self.progress_bar.setValue(pct)
        self.lbl_processed_count.setText(f"ไฟล์ที่ประมวลผล: {current:,} / {total:,} ({pct}%)")
        self.lbl_speed.setText(f"ความเร็ว: {fps:.1f} FPS")
        
        mins, secs = divmod(int(eta_sec), 60)
        hrs, mins = divmod(mins, 60)
        if hrs > 0:
            self.lbl_eta.setText(f"เวลาที่เหลือ: ~{hrs} ชม. {mins} นาที")
        else:
            self.lbl_eta.setText(f"เวลาที่เหลือ: ~{mins} น. {secs} วิ")

    @pyqtSlot(str, list, list, list, str)
    def on_item_tagged(self, path, chars, series, general, rating):
        fname = os.path.basename(path)
        char_text = ", ".join([f"{c[0]} ({int(c[1]*100)}%)" for c in chars]) if chars else "Unknown Character"
        series_text = f" [{series[0][0]}]" if series else ""
        self.lbl_current_detected.setText(f"🏷️ <b>{fname}</b>: <span style='color:#38bdf8;'>{char_text}{series_text}</span>")

    @pyqtSlot(str)
    def on_scan_log(self, text):
        self.log_text.append(text)
        # Scroll to bottom
        sb = self.log_text.verticalScrollBar()
        sb.setValue(sb.maximum())

    @pyqtSlot(int, int, float)
    def on_scan_finished(self, total_tagged, errors, elapsed):
        self.timer_stopwatch.stop()
        self.btn_start.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self.combo_model.setEnabled(True)
        self.combo_scope.setEnabled(True)
        self.combo_provider.setEnabled(True)
        self.combo_prec.setEnabled(True)
        self.combo_storage.setEnabled(True)
        self.combo_scan_mode.setEnabled(True)
        self.slider_thresh.setEnabled(True)
        self.lbl_speed.setText("ความเร็ว: 0.0 FPS")

        # Format final elapsed time stopwatch
        mins, secs = divmod(int(elapsed), 60)
        hrs, mins = divmod(mins, 60)
        if hrs > 0:
            self.lbl_elapsed.setText(f"⏱️ ใช้ไป: {hrs:02d}:{mins:02d}:{secs:02d} (เสร็จ)")
        else:
            self.lbl_elapsed.setText(f"⏱️ ใช้ไป: {mins:02d}:{secs:02d} (เสร็จ)")

        self.lbl_eta.setText("⏳ เหลือ: สำเร็จ ✨")
        self.refresh_stats(force_refresh=True)
        self.worker = None

    def closeEvent(self, event):
        if self.worker and self.worker.running:
            reply = QMessageBox.question(
                self, "ยืนยันการปิดหน้าต่าง",
                "กำลังดำเนินการสแกน AI อยู่ในขณะนี้ คุณต้องการหยุดการสแกนและปิดหน้าต่างใช่หรือไม่?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.worker.stop()
                event.accept()
            else:
                event.ignore()
                return
        event.accept()
