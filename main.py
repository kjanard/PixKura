import io
import sys
import os
import html
# --- Suppress C-level library warnings (libpng, OpenCV, Qt font/imageio) ---
os.environ["OPENCV_LOG_LEVEL"] = "OFF"
os.environ["QT_LOGGING_RULES"] = (
    "qt.text.font.db=false;qt.text.font.db.debug=false;qt.text.font.db.warning=false;"
    "qt.gui.imageio=false;qt.gui.imageio.warning=false;qt.gui.icc=false;qt.gui.icc.warning=false;*.debug=false"
)

if __name__ == "__main__" and not any(flag in sys.argv for flag in ("--console", "-c", "--debug")):
    try:
        # Redirect C-runtime file descriptor 2 (stderr) to os.devnull to silence libpng/C warnings in GUI mode
        _c_devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(_c_devnull, 2)
        os.close(_c_devnull)
    except Exception:
        pass
# --------------------------------------------------------------------------

import sqlite3
from collections import OrderedDict

# --- SQLite High-Performance Tuning (Global Patch) ---
_orig_sqlite_connect = sqlite3.connect
def _optimized_sqlite_connect(*args, **kwargs):
    conn = _orig_sqlite_connect(*args, **kwargs)
    try:
        conn.execute('PRAGMA synchronous = NORMAL;')
        conn.execute('PRAGMA mmap_size = 134217728;')  # 128 MB
        conn.execute('PRAGMA temp_store = MEMORY;')
        conn.execute('PRAGMA cache_size = -4000;')     # 4 MB per connection
    except Exception:
        pass
    return conn
sqlite3.connect = _optimized_sqlite_connect
# -----------------------------------------------------

import logging
logging.basicConfig(level=logging.ERROR, filename='error.log', 
                    format='%(asctime)s - %(levelname)s - %(message)s')

def _global_excepthook(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logging.critical("Unhandled exception:", exc_info=(exc_type, exc_value, exc_traceback))
    import traceback
    print("Unhandled Exception:\n" + "".join(traceback.format_exception(exc_type, exc_value, exc_traceback)), file=sys.stdout)

sys.excepthook = _global_excepthook

import json
import time
from PyQt6.QtWidgets import (QApplication, QMainWindow, QListView, QVBoxLayout, QWidget, QPushButton, QProgressBar, 
                             QFileDialog, QHBoxLayout, QLineEdit, QStackedWidget, QComboBox, QMessageBox, QRadioButton, QButtonGroup, QFrame, QDialog, QLabel, QTextEdit, QMenu, QCompleter)
from PyQt6.QtCore import QSize, Qt, pyqtSlot, QUrl, QTimer, QThreadPool, QPoint, QStringListModel
from style_sheet import DARK_STYLESHEET
from PyQt6.QtGui import QIcon, QPixmap, QImage, QDesktopServices, QColor, QAction, QShortcut, QKeySequence

from config import DB_FILE, CONFIG_FILE, ALL_MEDIA_EXT, AppConfig, load_app_config, update_app_config
from database import (
    DatabaseSetup, get_file_tags, get_all_characters, get_all_series,
    get_all_search_suggestions, toggle_file_favorite, is_file_favorite
)
from utils import (
    add_indicator, format_size, overlay_avatar_on_grid, show_in_file_manager,
    make_thumbnail_rgb, load_media_thumbnail, safe_single_shot
)
from workers import (
    FolderCacheScanner, ThumbnailGeneratorWorker, ImageLoaderWorker, StreamScanner,
    ApiFetcherWorker, PixivDownloaderWorker, BooruNameUpdateWorker, BackgroundThumbnailPreloader,
    DatabaseOptimizerWorker, close_all_thread_local_conns
)
from components import LoadingOverlay, DuplicateFinderDialog, DatasetExporterDialog
from download_dialog import PixivDownloadDialog
from ai_tag_dialog import AiTagDialog
from models import ThumbnailListModel

class PixivManagerApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{AppConfig.APP_NAME} ({AppConfig.MILESTONE}) - by {AppConfig.AUTHOR}")
        self.resize(1120, 800)
        
        icon_path = os.path.join(os.path.dirname(__file__), "icon.ico")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        
        self.db_setup = DatabaseSetup()
        self.main_db_conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        self.setStyleSheet(DARK_STYLESHEET)
        
        self.bg_pool = QThreadPool(); self.bg_pool.setMaxThreadCount(min(6, max(4, os.cpu_count()))) 
        self.fg_pool = QThreadPool(); self.fg_pool.setMaxThreadCount(min(4, max(2, os.cpu_count() // 2)))
        self.api_pool = QThreadPool(); self.api_pool.setMaxThreadCount(1)
        self.scan_pool = QThreadPool(); self.scan_pool.setMaxThreadCount(1)
        
        self.current_folder = ""; self.config = {}
        self.folder_queue = []; self.file_queue = []
        self.scan_worker = None; self.total_tasks = 0; self.finished_tasks = 0
        self.folder_item_map = {}; self.last_filter_mode = 'ALL'
        self.force_thumbnail_update = False
        self.quick_randomize_covers = False
        self.current_view_mode = 'folders'
        self.bg_preloader = None
        
        # --- Lazy Load & Smooth Buffer (LRU Cache) Infrastructure ---
        self.file_item_map = {}
        self.detail_item_map = {}
        self.all_scanned_files = [] 
        self.scroll_timer = QTimer(); self.scroll_timer.setSingleShot(True)
        self.scroll_timer.timeout.connect(self.load_visible_thumbnails)
        
        # Folder lazy load timer (ultra-low debounce for instant smoothness)
        self.folder_scroll_timer = QTimer(); self.folder_scroll_timer.setSingleShot(True)
        self.folder_scroll_timer.timeout.connect(self.load_visible_folder_icons)

        # Detail view lazy load timer
        self.detail_scroll_timer = QTimer(); self.detail_scroll_timer.setSingleShot(True)
        self.detail_scroll_timer.timeout.connect(self.load_visible_detail_thumbnails)
        
        # LRU Cache stores ordered loaded items to keep scrolling buttery smooth while capping RAM
        self.folder_icon_lru = OrderedDict()
        self.file_icon_lru = OrderedDict()
        self.detail_icon_lru = OrderedDict()
        self.MAX_FOLDER_CACHE = 600  # ~135 MB RAM max for decoded folder covers
        self.MAX_FILE_CACHE = 400    # ~90 MB RAM max for decoded file thumbnails
        self.MAX_DETAIL_CACHE = 300  # ~65 MB RAM max for artist detail view thumbnails
        
        self.default_file_icon = QIcon()
        pix_file = QPixmap(180, 180); pix_file.fill(QColor(40, 40, 40))
        self.default_file_icon.addPixmap(pix_file)
        # ----------------------------------------------------

        # --- Generation ID Control ---
        self.scan_gen_id = 0
        self.total_to_generate = 0
        self.generated_count = 0

        self.scan_timer = QTimer(); self.scan_timer.setInterval(50); self.scan_timer.timeout.connect(self.process_loading_queue)

        # UI Setup
        central = QWidget(); self.setCentralWidget(central); main_layout = QVBoxLayout(central)
        top_bar = QHBoxLayout()
        self.btn_back = QPushButton("← Back"); self.btn_back.setObjectName("btn_back"); self.btn_back.setFixedWidth(80); self.btn_back.clicked.connect(self.go_back); self.btn_back.setVisible(False); top_bar.addWidget(self.btn_back)
        self.path_input = QLineEdit(); self.path_input.setReadOnly(True); top_bar.addWidget(self.path_input)
        self.btn_browse = QPushButton("Browse"); self.btn_browse.clicked.connect(self.browse_folder); top_bar.addWidget(self.btn_browse)
        self.btn_update = QPushButton("Update Names"); self.btn_update.setObjectName("btn_update"); self.btn_update.clicked.connect(self.start_name_update); top_bar.addWidget(self.btn_update)
        self.btn_download = QPushButton("Download Tool"); self.btn_download.setObjectName("btn_download"); self.btn_download.clicked.connect(self.open_download_tool); top_bar.addWidget(self.btn_download)
        self.btn_ai_tag = QPushButton("🤖 AI Tagger"); self.btn_ai_tag.setObjectName("btn_ai_tag"); self.btn_ai_tag.clicked.connect(lambda: self.open_ai_tag_dialog()); top_bar.addWidget(self.btn_ai_tag)
        self.btn_dashboard = QPushButton("📊 Dashboard"); self.btn_dashboard.setObjectName("btn_dashboard"); self.btn_dashboard.clicked.connect(self.open_dashboard_dialog); top_bar.addWidget(self.btn_dashboard)
        main_layout.addLayout(top_bar)

        control_bar = QHBoxLayout()
        view_group = QButtonGroup(self)
        self.rb_folders = QRadioButton("Folders"); self.rb_all = QRadioButton("All Files"); self.rb_photos = QRadioButton("All Photos"); self.rb_anim = QRadioButton("All Animate")
        self.rb_folders.setChecked(True)
        for rb in [self.rb_folders, self.rb_all, self.rb_photos, self.rb_anim]:
            view_group.addButton(rb); rb.toggled.connect(self.refresh_view); control_bar.addWidget(rb)
        line = QFrame(); line.setFrameShape(QFrame.Shape.VLine); line.setFrameShadow(QFrame.Shadow.Sunken); control_bar.addWidget(line)
        self.btn_rescan = QPushButton("Rescan"); self.btn_rescan.clicked.connect(self.force_rescan_files); control_bar.addWidget(self.btn_rescan)
        self.btn_randomize_covers = QPushButton("Randomize Covers"); self.btn_randomize_covers.setObjectName("btn_randomize_covers"); self.btn_randomize_covers.clicked.connect(self.randomize_folder_covers); control_bar.addWidget(self.btn_randomize_covers)
        self.btn_favorites = QPushButton("⭐ Favorites"); self.btn_favorites.setToolTip("Show Favorite Artworks (is:fav)"); self.btn_favorites.clicked.connect(lambda: self.apply_search_query_from_help("is:fav")); control_bar.addWidget(self.btn_favorites)
        self.btn_duplicates = QPushButton("🔍 Duplicates"); self.btn_duplicates.setToolTip("Find Duplicate or Similar Artworks (Perceptual dHash)"); self.btn_duplicates.clicked.connect(self.open_duplicate_finder_dialog); control_bar.addWidget(self.btn_duplicates)
        self.btn_export_dataset = QPushButton("📦 Export Dataset"); self.btn_export_dataset.setToolTip("Export Artworks + .txt Captions for LoRA / Colab"); self.btn_export_dataset.clicked.connect(self.open_dataset_exporter_dialog); control_bar.addWidget(self.btn_export_dataset)
        main_layout.addLayout(control_bar)
        
        # Unified Search & Filter Card Panel
        self.filter_panel = QFrame()
        self.filter_panel.setStyleSheet("""
            QFrame {
                background-color: #16161a;
                border: 1px solid #2d2d34;
                border-radius: 8px;
                padding: 4px;
            }
            QLabel {
                background: transparent;
                border: none;
                font-weight: bold;
                color: #9ca3af;
            }
        """)
        panel_layout = QVBoxLayout(self.filter_panel)
        panel_layout.setContentsMargins(12, 12, 12, 12)
        panel_layout.setSpacing(10)

        # Row 1: Search keyword input and buttons
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Search:"))
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search names, tag:..., char:..., artist:..., or is:fav")
        self.search_input.returnPressed.connect(self.perform_search)

        # Dynamic Auto-complete & Suggestions
        self.search_completer = QCompleter([], self)
        self.search_completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.search_completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.search_completer.setMaxVisibleItems(10)
        
        popup = self.search_completer.popup()
        popup.setStyleSheet("""
            QListView {
                background-color: #1a1a24;
                color: #f3f4f6;
                border: 1px solid #3b82f6;
                border-radius: 6px;
                padding: 4px;
                selection-background-color: #3b82f6;
                selection-color: #ffffff;
            }
            QListView::item {
                padding: 6px 10px;
                border-radius: 4px;
                min-height: 22px;
            }
            QListView::item:hover {
                background-color: #252538;
            }
        """)
        self.search_input.setCompleter(self.search_completer)
        row1.addWidget(self.search_input)

        btn_go = QPushButton("Go")
        btn_go.clicked.connect(self.perform_search)
        row1.addWidget(btn_go)

        btn_clear = QPushButton("Clear")
        btn_clear.clicked.connect(self.clear_search)
        row1.addWidget(btn_clear)

        btn_help = QPushButton("❓ Help")
        btn_help.setToolTip("Search Syntax Guide & Safety Help (คู่มือการค้นหาและความปลอดภัย)")
        btn_help.setStyleSheet("QPushButton { color: #60a5fa; font-weight: bold; }")
        btn_help.clicked.connect(self.show_search_help_dialog)
        row1.addWidget(btn_help)
        panel_layout.addLayout(row1)

        # Row 2: Sort, Size, and Date Modified dropdowns
        row2 = QHBoxLayout()
        row2.setContentsMargins(0, 0, 0, 0)
        row2.setSpacing(12)

        row2.addWidget(QLabel("Sort:"))
        self.sort_combo = QComboBox()
        self.sort_combo.addItems([
            "Name (A-Z)", "Name (Z-A)",
            "File Count (High->Low)", "File Count (Low->High)",
            "Date Modified (New->Old)", "Date Modified (Old->New)",
            "Date Created (New->Old)", "Date Created (Old->New)",
            "Random"
        ])
        self.sort_combo.currentIndexChanged.connect(self.apply_sort)
        row2.addWidget(self.sort_combo)

        self.advanced_filter_widget = QWidget()
        self.advanced_filter_widget.setStyleSheet("background: transparent; border: none;")
        adv_layout = QHBoxLayout(self.advanced_filter_widget)
        adv_layout.setContentsMargins(0, 0, 0, 0)
        adv_layout.setSpacing(12)

        adv_layout.addWidget(QLabel("Safety:"))
        self.rating_filter = QComboBox()
        self.rating_filter.addItem("All (ทั้งหมด)", "ALL")
        self.rating_filter.addItem("🟢 SFW Only (ปลอดภัย / ไม่โป๊)", "SFW")
        self.rating_filter.addItem("🔞 NSFW Only (18+ / ผู้ใหญ่)", "NSFW")
        self.rating_filter.addItem("⚠️ Sensitive / Ecchi", "SENSITIVE")
        self.rating_filter.addItem("🔞 Questionable", "QUESTIONABLE")
        self.rating_filter.addItem("🔞 Explicit", "EXPLICIT")
        self.rating_filter.setMinimumWidth(180)
        self.rating_filter.currentIndexChanged.connect(self.perform_search)
        adv_layout.addWidget(self.rating_filter)

        adv_layout.addWidget(QLabel("Size:"))
        self.size_filter = QComboBox()
        self.size_filter.addItems(["All Sizes", "Small (< 2 MB)", "Medium (2 - 10 MB)", "Large (10 - 50 MB)", "Huge (> 50 MB)"])
        self.size_filter.currentIndexChanged.connect(self.perform_search)
        adv_layout.addWidget(self.size_filter)

        adv_layout.addWidget(QLabel("Date Modified:"))
        self.date_filter = QComboBox()
        self.date_filter.addItems(["All Dates", "Today", "Last 7 Days", "Last 30 Days", "Last Year"])
        self.date_filter.currentIndexChanged.connect(self.perform_search)
        adv_layout.addWidget(self.date_filter)

        adv_layout.addWidget(QLabel("Character:"))
        self.char_filter = QComboBox()
        self.char_filter.setEditable(True)
        self.char_filter.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.char_filter.addItem("All Characters", None)
        self.char_filter.setMinimumWidth(190)
        self.char_filter.setMaximumWidth(270)

        char_view = self.char_filter.view()
        char_view.setTextElideMode(Qt.TextElideMode.ElideNone)
        char_view.setUniformItemSizes(True)
        char_view.setMinimumWidth(380)
        char_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        char_view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        char_comp = self.char_filter.completer()
        if char_comp:
            char_comp.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            char_comp.setFilterMode(Qt.MatchFlag.MatchContains)
            char_comp.setMaxVisibleItems(12)
            if char_comp.popup():
                char_comp.popup().setTextElideMode(Qt.TextElideMode.ElideNone)
                char_comp.popup().setUniformItemSizes(True)
                char_comp.popup().setMinimumWidth(380)
                char_comp.popup().setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
                char_comp.popup().setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        if self.char_filter.lineEdit():
            self.char_filter.lineEdit().setPlaceholderText("Search character...")
            self.char_filter.lineEdit().returnPressed.connect(self.perform_search)
        self.char_filter.currentIndexChanged.connect(self.perform_search)
        adv_layout.addWidget(self.char_filter)

        row2.addWidget(self.advanced_filter_widget)
        row2.addStretch()
        panel_layout.addLayout(row2)

        main_layout.addWidget(self.filter_panel)

        self.stacked_widget = QStackedWidget(); main_layout.addWidget(self.stacked_widget)
        self.loading_overlay = LoadingOverlay(self.stacked_widget)
        
        self.page_folders = QWidget(); l1 = QVBoxLayout(self.page_folders); l1.setContentsMargins(0,0,0,0)
        self.list_folders = QListView(); self.setup_list(self.list_folders)
        self.folders_model = ThumbnailListModel()
        self.list_folders.setModel(self.folders_model)
        self.list_folders.doubleClicked.connect(self.handle_main_click)
        self.list_folders.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_folders.customContextMenuRequested.connect(self.show_folder_context_menu)
        l1.addWidget(self.list_folders); self.stacked_widget.addWidget(self.page_folders)
        self.list_folders.verticalScrollBar().valueChanged.connect(lambda: self.folder_scroll_timer.start(15))
        
        self.page_files = QWidget(); l2 = QVBoxLayout(self.page_files); l2.setContentsMargins(0,0,0,0)
        self.list_files = QListView(); self.setup_list(self.list_files)
        self.files_model = ThumbnailListModel()
        self.list_files.setModel(self.files_model)
        self.list_files.doubleClicked.connect(self.handle_main_click)
        self.list_files.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_files.customContextMenuRequested.connect(self.show_file_context_menu)
        l2.addWidget(self.list_files); self.stacked_widget.addWidget(self.page_files)
        self.list_files.verticalScrollBar().valueChanged.connect(lambda: self.scroll_timer.start(25))
        
        self.page_detail = QWidget(); l3 = QVBoxLayout(self.page_detail); l3.setContentsMargins(0,0,0,0)
        self.list_detail = QListView(); self.setup_list(self.list_detail)
        self.detail_model = ThumbnailListModel()
        self.list_detail.setModel(self.detail_model)
        self.list_detail.doubleClicked.connect(self.open_image_external)
        self.list_detail.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_detail.customContextMenuRequested.connect(self.show_file_context_menu)
        l3.addWidget(self.list_detail); self.stacked_widget.addWidget(self.page_detail)
        self.list_detail.verticalScrollBar().valueChanged.connect(lambda: self.detail_scroll_timer.start(20))

        self.status_bar = self.statusBar(); self.progress_bar = QProgressBar(); self.progress_bar.setFixedWidth(200); self.progress_bar.setVisible(False); self.status_bar.addPermanentWidget(self.progress_bar); self.status_bar.showMessage("Ready")
        self.default_folder_icon = QIcon(); pix = QPixmap(180, 180); pix.fill(QColor(80, 80, 80)); self.default_folder_icon.addPixmap(pix)
        
        self.setAcceptDrops(True)

        # Keyboard Shortcuts
        QShortcut(QKeySequence("F5"), self, self.force_rescan_files)
        QShortcut(QKeySequence("Ctrl+F"), self, lambda: self.search_input.setFocus())
        QShortcut(QKeySequence("Backspace"), self, self.go_back)
        QShortcut(QKeySequence("Escape"), self, self.stop_all)

        self.load_config(); self.load_last_session()
        self.refresh_character_filters()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            if os.path.isdir(path):
                self.current_folder = path
                self.path_input.setText(path)
                self.config['last_folder'] = path
                self.save_config()
                self.folders_model.update_data([])
                self.folder_item_map.clear()
                self.current_view_mode = None
                self.refresh_view()

    def setup_list(self, lst):
        lst.setIconSize(QSize(180, 180)); lst.setViewMode(QListView.ViewMode.IconMode)
        lst.setResizeMode(QListView.ResizeMode.Adjust)
        lst.setSpacing(10)
        lst.setUniformItemSizes(True)
        lst.setGridSize(QSize(200, 220))
        lst.setWordWrap(True)
    
    def get_current_queue(self): return self.folder_queue if self.rb_folders.isChecked() else self.file_queue

    def reset_and_kill_all(self):
        if hasattr(self, 'loading_overlay') and self.loading_overlay:
            self.loading_overlay.stop()
        self.scan_gen_id += 1
        
        self.scan_timer.stop()
        self.folder_scroll_timer.stop()
        self.detail_scroll_timer.stop()

        if hasattr(self, 'bg_preloader') and self.bg_preloader:
            self.bg_preloader.stop()
            self.bg_preloader = None
        
        self.bg_pool.clear() 
        self.fg_pool.clear()
        self.scan_pool.clear()
        
        if self.scan_worker:
            self.scan_worker.stop()
            try: self.scan_worker.signals.scan_batch.disconnect()
            except Exception as e: logging.error(f"Disconnect error: {e}")
            try: self.scan_worker.signals.scan_finished.disconnect()
            except Exception as e: logging.error(f"Disconnect error: {e}")
            self.scan_worker = None
        
        self.folder_icon_lru.clear()
        self.file_icon_lru.clear()
        self.detail_icon_lru.clear()
            
        self.folder_queue = []
        self.file_queue = []

    def refresh_view(self):
        if not self.current_folder: return
        
        if self.rb_folders.isChecked():
            target_mode = 'folders'
        elif self.rb_all.isChecked():
            target_mode = 'all'
        elif self.rb_photos.isChecked():
            target_mode = 'photos'
        else:
            target_mode = 'anim'
            
        if hasattr(self, 'current_view_mode') and self.current_view_mode == target_mode:
            sender = self.sender()
            if sender is not None and isinstance(sender, QRadioButton):
                return
                
        self.current_view_mode = target_mode
        self.reset_and_kill_all()

        self.sort_combo.blockSignals(True)
        current_sort_idx = self.sort_combo.currentIndex()
        if current_sort_idx < 0: current_sort_idx = 0

        if self.rb_folders.isChecked():
            self.stacked_widget.setCurrentWidget(self.page_folders); self.btn_rescan.setText("Rescan Folders")
            self.btn_randomize_covers.setVisible(True)
            self.advanced_filter_widget.setVisible(False)
            
            self.sort_combo.clear()
            self.sort_combo.addItems([
                "Name (A-Z)", "Name (Z-A)",
                "File Count (High->Low)", "File Count (Low->High)",
                "Date Modified (New->Old)", "Date Modified (Old->New)",
                "Date Created (New->Old)", "Date Created (Old->New)",
                "Random"
            ])
            self.sort_combo.setCurrentIndex(min(current_sort_idx, self.sort_combo.count() - 1))
            self.sort_combo.blockSignals(False)
            
            self.apply_sort(self.sort_combo.currentIndex())
            
            if self.folders_model.rowCount() == 0: self.start_scan_folders_cached(self.current_folder)
            else:
                if self.folder_queue: self.scan_timer.start()
                self.folder_scroll_timer.start(50)
        else:
            self.stacked_widget.setCurrentWidget(self.page_files); self.btn_rescan.setText("Rescan Files")
            self.btn_randomize_covers.setVisible(False)
            self.advanced_filter_widget.setVisible(True)
            
            self.sort_combo.clear()
            self.sort_combo.addItems([
                "Name (A-Z)", "Name (Z-A)",
                "File Size (High->Low)", "File Size (Low->High)",
                "Date Modified (New->Old)", "Date Modified (Old->New)",
                "Date Created (New->Old)", "Date Created (Old->New)",
                "Random"
            ])
            self.sort_combo.setCurrentIndex(min(current_sort_idx, self.sort_combo.count() - 1))
            self.sort_combo.blockSignals(False)
            
            self.apply_sort(self.sort_combo.currentIndex())
            self.perform_search()

    def start_scan_folders_cached(self, folder, force_full=False):
        self.folders_model.update_data([])
        self.folder_item_map.clear()
        self.status_bar.showMessage("Loading folders..."); self.progress_bar.setVisible(True)
        self.loading_overlay.start("🔍 กำลังตรวจสอบข้อมูลโฟลเดอร์...")
        
        self.scan_worker = FolderCacheScanner(folder, DB_FILE, self.scan_gen_id, force_full=force_full)
        self.scan_worker.signals.scan_batch.connect(self.handle_folder_cache_batch)
        self.scan_worker.signals.folder_updated.connect(self.handle_folder_updated)
        self.scan_worker.signals.scan_finished.connect(self.handle_new_folder_found)
        self.scan_worker.signals.api_log.connect(self.status_bar.showMessage)
        self.scan_pool.start(self.scan_worker)

    @pyqtSlot(int, list)
    def handle_folder_cache_batch(self, gen_id, batch):
        if gen_id != self.scan_gen_id: return
        new_items = []
        for data in batch:
            aid = data[0]
            name = data[1]
            grid_blob = data[2]
            profile_blob = data[3]
            mtime = data[4] if len(data) > 4 and data[4] is not None else 0.0
            fcount = data[5] if len(data) > 5 and data[5] is not None else 0
            ctime = data[6] if len(data) > 6 and data[6] is not None else 0.0
            item_dict = {
                'id': aid,
                'name': name,
                'icon': self.default_folder_icon,
                'type': 'FOLDER',
                'count': fcount,
                'mtime': mtime,
                'ctime': ctime,
                'grid_blob': grid_blob,
                'profile_blob': profile_blob,
                'icon_loaded': False,
            }
            new_items.append(item_dict)
            self.folder_item_map[aid] = item_dict
            if grid_blob is None or self.force_thumbnail_update or self.quick_randomize_covers: self.folder_queue.append(aid)
        self.folders_model.append_items(new_items)
        if self.folder_queue:
            self.total_to_generate = len(self.folder_queue)
            self.generated_count = 0
            self.status_bar.showMessage(f"Loaded list. Generating {self.total_to_generate} thumbnails...")
            if self.rb_folders.isChecked(): self.scan_timer.start()
        self.folder_scroll_timer.start(50)

    @pyqtSlot(int, str, str, object, int, float, float)
    def handle_folder_updated(self, gen_id, aid, name, blob_data, fcount, mtime=0.0, ctime=0.0):
        if gen_id != self.scan_gen_id: return
        if aid in self.folder_item_map:
            item_dict = self.folder_item_map[aid]
            if blob_data is not None and isinstance(blob_data, (bytes, bytearray)):
                item_dict['grid_blob'] = blob_data
                if aid in self.folder_icon_lru or item_dict.get('icon_loaded'):
                    qimg = QImage.fromData(blob_data)
                    if not qimg.isNull():
                        profile_blob = item_dict.get('profile_blob')
                        if profile_blob:
                            qimg = overlay_avatar_on_grid(qimg, profile_blob)
                        item_dict['icon'] = QIcon(QPixmap.fromImage(qimg))
                        item_dict['icon_loaded'] = True
                        self.folder_icon_lru[aid] = True
                else:
                    item_dict['icon_loaded'] = False
            item_dict['name'] = name
            item_dict['count'] = fcount
            if mtime: item_dict['mtime'] = mtime
            if ctime: item_dict['ctime'] = ctime
            
            row = self.folders_model.get_row_by_id(aid)
            if row >= 0:
                idx = self.folders_model.index(row, 0)
                self.folders_model.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole, Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.UserRole + 2, Qt.ItemDataRole.UserRole + 3, Qt.ItemDataRole.UserRole + 4])

            if blob_data is None and aid not in self.folder_queue:
                self.folder_queue.append(aid)
                if not self.scan_timer.isActive() and self.rb_folders.isChecked():
                    self.scan_timer.start()

    @pyqtSlot(int, int)
    def handle_new_folder_found(self, gen_id, count):
        if gen_id != self.scan_gen_id: return
        self.progress_bar.setVisible(False); self.status_bar.showMessage("Folder list loaded."); self.apply_sort(self.sort_combo.currentIndex())
        self.loading_overlay.stop()
        self.folder_scroll_timer.start(50)

    def start_scan_all_files(self, folder, use_cache=True, filter_mode='ALL',
                             min_size=None, max_size=None, min_mtime=None, search_keyword=None,
                             tag_filter=None, parsed_query=None, rating_filter='ALL'):
        self.files_model.update_data([])
        self.all_scanned_files = []
        self.file_item_map.clear()

        info_parts = [filter_mode]
        if rating_filter and rating_filter != 'ALL':
            info_parts.append(f"Rating: {rating_filter}")
        if tag_filter:
            info_parts.append(f"Tag: {tag_filter}")
        if parsed_query and parsed_query.raw_text:
            info_parts.append(f"Query: {parsed_query.raw_text}")
        elif search_keyword:
            info_parts.append(f"Keyword: {search_keyword}")

        self.status_bar.showMessage(f"Indexing {' | '.join(info_parts)} files..."); self.progress_bar.setVisible(True)
        self.loading_overlay.start("📂 กำลังสแกนหาไฟล์...")
        
        self.scan_worker = StreamScanner(folder, DB_FILE, self.scan_gen_id, use_cache, filter_mode,
                                         min_size, max_size, min_mtime, search_keyword, tag_filter,
                                         parsed_query=parsed_query, rating_filter=rating_filter)
        self.scan_worker.signals.scan_batch.connect(self.handle_stream_batch); self.scan_worker.signals.scan_finished.connect(self.handle_stream_finished)
        self.scan_pool.start(self.scan_worker); self.scan_timer.start()

    def force_rescan_files(self):
        msgBox = QMessageBox(self)
        msgBox.setWindowTitle("Rescan Options")
        msgBox.setText("Select Action:\n\n"
                       "• Quick Sync: Checks for added, deleted, or modified items on the fly. Fast and efficient.\n"
                       "• Full Rebuild: Deletes database caches and fully rebuilds the index from scratch. Slow.\n"
                       "• Optimize DB: Defragments and compacts the database (VACUUM) to reclaim disk space.")
        
        btn_quick = msgBox.addButton("Quick Sync (Recommended)", QMessageBox.ButtonRole.AcceptRole)
        btn_full = msgBox.addButton("Full Rebuild", QMessageBox.ButtonRole.RejectRole)
        btn_optimize = msgBox.addButton("Optimize DB (VACUUM)", QMessageBox.ButtonRole.ActionRole)
        btn_cancel = msgBox.addButton("Cancel", QMessageBox.ButtonRole.DestructiveRole)
        msgBox.setEscapeButton(btn_cancel)
        msgBox.setDefaultButton(btn_cancel)
        
        msgBox.exec()
        clicked = msgBox.clickedButton()
        
        if clicked == btn_cancel or clicked is None:
            return
            
        if clicked == btn_optimize:
            self.optimize_database()
            return
            
        self.reset_and_kill_all()
        
        is_full = (clicked == btn_full)
        
        if self.rb_folders.isChecked():
            self.folders_model.update_data([])
            self.folder_item_map.clear()
            
            if is_full:
                try:
                    c = self.main_db_conn.cursor()
                    c.execute("DELETE FROM thumbnails")
                    c.execute("DELETE FROM file_index")
                    self.main_db_conn.commit()
                except Exception as e:
                    logging.error(f"DB Error: {e}")
            self.start_scan_folders_cached(self.current_folder, force_full=is_full)
        else:
            if is_full:
                try:
                    c = self.main_db_conn.cursor()
                    c.execute("DELETE FROM file_index")
                    self.main_db_conn.commit()
                except Exception as e:
                    logging.error(f"DB Error: {e}")
            self.files_model.update_data([])
            
            mode = self.last_filter_mode
            
            size_idx = self.size_filter.currentIndex()
            min_size, max_size = None, None
            if size_idx == 1: max_size = 2 * 1024 * 1024
            elif size_idx == 2: min_size, max_size = 2 * 1024 * 1024, 10 * 1024 * 1024
            elif size_idx == 3: min_size, max_size = 10 * 1024 * 1024, 50 * 1024 * 1024
            elif size_idx == 4: min_size = 50 * 1024 * 1024

            date_idx = self.date_filter.currentIndex()
            min_mtime = None
            if date_idx == 1: min_mtime = time.time() - 24 * 60 * 60
            elif date_idx == 2: min_mtime = time.time() - 7 * 24 * 60 * 60
            elif date_idx == 3: min_mtime = time.time() - 30 * 24 * 60 * 60
            elif date_idx == 4: min_mtime = time.time() - 365 * 24 * 60 * 60
            
            from query_parser import parse_search_query
            parsed_q = parse_search_query(self.search_input.text().strip())
            active_rating = self.rating_filter.currentData() if hasattr(self, 'rating_filter') else 'ALL'
            self.start_scan_all_files(self.current_folder, use_cache=not is_full, filter_mode=mode,
                                      min_size=min_size, max_size=max_size, min_mtime=min_mtime,
                                      search_keyword=None,
                                      parsed_query=parsed_q,
                                      rating_filter=active_rating)

    @pyqtSlot(int, list)
    def handle_stream_batch(self, gen_id, batch_files):
        if gen_id != self.scan_gen_id: return
        self.all_scanned_files.extend(batch_files)
        self.status_bar.showMessage(f"Found {len(self.all_scanned_files)} files...")

    @pyqtSlot(int, int)
    def handle_stream_finished(self, gen_id, total_count):
        if gen_id != self.scan_gen_id: return
        self.progress_bar.setVisible(False)
        self.status_bar.showMessage(f"Building UI for {total_count} files. Please wait...")
        QApplication.processEvents()

        new_items = []
        for item_data in self.all_scanned_files:
            path = item_data[0] if isinstance(item_data, tuple) else item_data
            size = item_data[1] if isinstance(item_data, tuple) and len(item_data) > 1 else 0
            mtime = item_data[2] if isinstance(item_data, tuple) and len(item_data) > 2 else 0.0
            ctime = item_data[3] if isinstance(item_data, tuple) and len(item_data) > 3 else 0.0

            item_dict = {
                'id': path,
                'name': os.path.basename(path),
                'icon': self.default_file_icon,
                'type': 'FILE',
                'count': size,
                'mtime': mtime or 0.0,
                'ctime': ctime or 0.0,
                'loaded': False
            }
            new_items.append(item_dict)
            self.file_item_map[path] = item_dict

        self.files_model.update_data(new_items)
        self.apply_sort(self.sort_combo.currentIndex())
        self.status_bar.showMessage(f"Ready. Showing {total_count} files. (Background caching started...)")

        self.scroll_timer.start(25)

        paths_only = [item[0] if isinstance(item, tuple) else item for item in self.all_scanned_files]
        self.all_scanned_files = []
        
        from workers import BackgroundThumbnailPreloader
        self.bg_preloader = BackgroundThumbnailPreloader(paths_only, DB_FILE, self.scan_gen_id)
        self.bg_pool.start(self.bg_preloader)
        self.loading_overlay.stop()

    def process_loading_queue(self):
        queue = self.get_current_queue()
        if not queue:
            self.scan_timer.stop()
            self.force_thumbnail_update = False
            self.quick_randomize_covers = False
            return
        if self.bg_pool.activeThreadCount() >= self.bg_pool.maxThreadCount() or self.fg_pool.activeThreadCount() >= self.fg_pool.maxThreadCount(): return
        batch = queue[:20]
        if self.rb_folders.isChecked(): self.folder_queue = self.folder_queue[20:]
        else: self.file_queue = self.file_queue[20:]
        
        is_folder_task = isinstance(batch[0], str) 
        if is_folder_task:
            for aid in batch:
                w = ThumbnailGeneratorWorker(aid, os.path.join(self.current_folder, aid), DB_FILE, self.scan_gen_id, force_update=self.force_thumbnail_update, quick_randomize=self.quick_randomize_covers)
                w.signals.folder_updated.connect(self.handle_folder_updated); self.bg_pool.start(w)

    def handle_main_click(self, index):
        if not index.isValid(): return
        item_data = index.model().get_item_data(index.row())
        if not item_data: return
        data_type = item_data.get('type')
        path_or_id = item_data.get('id')
        if data_type == 'FOLDER': 
            self.open_artist_folder(path_or_id)
        else: 
            self.open_lightbox_viewer(index.row(), index.model())

    def open_artist_folder(self, aid):
        path = os.path.join(self.current_folder, aid)
        if not os.path.exists(path): return
        self.scan_timer.stop()
        self.stacked_widget.setCurrentWidget(self.page_detail)
        self.btn_back.setVisible(True)
        self.path_input.setText(path)
        self.load_detail_images(path)

    def load_detail_images(self, path):
        """Loads artist folder images using Viewport Lazy Loading & LRU Cache for instant open and low RAM."""
        self.detail_model.update_data([])
        self.detail_item_map.clear()
        self.detail_icon_lru.clear()
        self.fg_pool.clear()
        try:
            files = [
                os.path.join(path, f)
                for f in os.listdir(path)
                if os.path.splitext(f)[1].lower() in ALL_MEDIA_EXT and not f.startswith('.')
            ]
        except Exception:
            files = []

        new_items = []
        for f in files:
            try:
                st = os.stat(f)
                size = st.st_size
                mtime = st.st_mtime
                ctime = st.st_ctime
            except Exception:
                size = 0
                mtime = 0.0
                ctime = 0.0

            item_dict = {
                'id': f,
                'name': os.path.basename(f),
                'icon': self.default_file_icon,
                'type': 'FILE',
                'count': size,
                'mtime': mtime,
                'ctime': ctime,
                'loaded': False
            }
            new_items.append(item_dict)
            self.detail_item_map[f] = item_dict

        self.detail_model.update_data(new_items)
        self.apply_sort(self.sort_combo.currentIndex())
        self.status_bar.showMessage(f"Showing {len(new_items)} files in {os.path.basename(path)}")
        self.detail_scroll_timer.start(15)

        if files:
            self.bg_preloader = BackgroundThumbnailPreloader(files, DB_FILE, self.scan_gen_id)
            self.bg_pool.start(self.bg_preloader)

    def load_visible_detail_thumbnails(self):
        try:
            if self.stacked_widget.currentWidget() != self.page_detail:
                return

            total_items = self.detail_model.rowCount()
            if total_items == 0:
                return

            viewport = self.list_detail.viewport()
            w = viewport.width()
            h = viewport.height()
            if w <= 0 or h <= 0:
                min_row, max_row = 0, min(total_items - 1, 40)
            else:
                rows = []
                step_y = max(30, h // 8)
                step_x = max(30, w // 6)
                for y in range(0, h + step_y, step_y):
                    cy = min(y, h - 1)
                    for x in range(0, w + step_x, step_x):
                        cx = min(x, w - 1)
                        idx = self.list_detail.indexAt(QPoint(cx, cy))
                        if idx.isValid():
                            rows.append(idx.row())
                if not rows:
                    min_row, max_row = 0, min(total_items - 1, 40)
                else:
                    min_row, max_row = min(rows), max(rows)

            buf_start = max(0, min_row - 25)
            buf_end = min(total_items, max_row + 60)

            current_active_paths = set()
            for r in range(buf_start, buf_end):
                item_dict = self.detail_model.get_item_data(r)
                if not item_dict:
                    continue
                path = item_dict['id']
                current_active_paths.add(path)

                if not item_dict.get('loaded', False):
                    item_dict['loaded'] = True
                    worker = ImageLoaderWorker(path, DB_FILE, self.scan_gen_id, lambda: self.scan_gen_id)
                    worker.signals.image_loaded.connect(self.update_single_detail_thumbnail)
                    self.fg_pool.start(worker)
                else:
                    if path in self.detail_icon_lru:
                        self.detail_icon_lru.move_to_end(path)

            while len(self.detail_icon_lru) > self.MAX_DETAIL_CACHE:
                old_path, _ = self.detail_icon_lru.popitem(last=False)
                if old_path not in current_active_paths:
                    item_dict = self.detail_item_map.get(old_path)
                    if item_dict and item_dict.get('loaded'):
                        item_dict['icon'] = self.default_file_icon
                        item_dict['loaded'] = False
                        row = self.detail_model.get_row_by_id(old_path)
                        if row >= 0:
                            idx = self.detail_model.index(row, 0)
                            self.detail_model.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])
        except Exception as e:
            logging.debug(f"Exception in load_visible_detail_thumbnails: {e}")

    @pyqtSlot(int, str, QImage, str)
    def update_single_detail_thumbnail(self, gen_id, path, qimg, ftype):
        try:
            if gen_id != self.scan_gen_id:
                return
            if path in self.detail_item_map:
                item_dict = self.detail_item_map[path]
                if qimg and not qimg.isNull():
                    pix = QPixmap.fromImage(qimg)
                else:
                    pix = QPixmap(240, 240)
                    pix.fill(QColor(40, 40, 40))
                pix = add_indicator(pix, ftype)
                item_dict['icon'] = QIcon(pix)

                self.detail_icon_lru[path] = True
                self.detail_icon_lru.move_to_end(path)

                row = self.detail_model.get_row_by_id(path)
                if row >= 0:
                    idx = self.detail_model.index(row, 0)
                    self.detail_model.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])
        except RuntimeError:
            pass

    @pyqtSlot(int, str, QImage, str)
    def add_detail_item(self, gen_id, path, qimg, ftype):
        if path in self.detail_item_map:
            self.update_single_detail_thumbnail(gen_id, path, qimg, ftype)
            return
        if qimg and not qimg.isNull():
            pix = QPixmap.fromImage(qimg)
        else:
            pix = QPixmap(240, 240)
            pix.fill(QColor(40, 40, 40))
            
        pix = add_indicator(pix, ftype)
        icon = QIcon(pix)
        
        try:
            st = os.stat(path)
            size = st.st_size
            mtime = st.st_mtime
            ctime = st.st_ctime
        except Exception:
            size = 0
            mtime = 0.0
            ctime = 0.0

        item_dict = {
            'id': path,
            'name': os.path.basename(path),
            'icon': icon,
            'type': 'FILE',
            'count': size,
            'mtime': mtime,
            'ctime': ctime
        }
        self.detail_item_map[path] = item_dict
        self.detail_model.append_items([item_dict])

    def open_image_external(self, index):
        if not index.isValid(): return
        self.open_lightbox_viewer(index.row(), self.detail_model)

    def open_lightbox_viewer(self, current_row, model):
        from lightbox_viewer import LightboxViewerDialog
        target_path = None
        selected_item = model.get_item_data(current_row)
        if selected_item:
            target_path = selected_item.get('id')

        file_paths = []
        target_idx = 0
        for r in range(model.rowCount()):
            item = model.get_item_data(r)
            if item and item.get('type') == 'FILE':
                pid = item.get('id')
                if pid:
                    if target_path and pid == target_path:
                        target_idx = len(file_paths)
                    file_paths.append(pid)
        
        if not file_paths:
            return
            
        dlg = LightboxViewerDialog(file_paths, current_index=target_idx, parent=self)
        dlg.exec()

    def show_folder_context_menu(self, pos):
        index = self.list_folders.indexAt(pos)
        if not index.isValid(): return
        item_data = self.folders_model.get_item_data(index.row())
        if not item_data: return
        aid = item_data.get('id')
        if not aid: return
        
        menu = QMenu(self)
        action_ai_scan = QAction("🤖 Scan AI Tags for this Folder", self)
        artist_name = item_data.get('name') or aid
        action_ai_scan.triggered.connect(lambda: self.open_ai_tag_dialog(os.path.join(self.current_folder, aid), artist_name))
        menu.addAction(action_ai_scan)

        action_update_name = QAction("🔄 Update Artist Name (ค้นหาชื่อศิลปิน)", self)
        action_update_name.triggered.connect(lambda: self.update_single_artist_name(aid))
        menu.addAction(action_update_name)

        action_randomize = QAction("Randomize Cover Collage", self)
        action_randomize.triggered.connect(lambda: self.randomize_single_folder_cover(aid))
        menu.addAction(action_randomize)
        menu.exec(self.list_folders.mapToGlobal(pos))

    def update_single_artist_name(self, aid):
        self.status_bar.showMessage(f"กำลังค้นหาชื่อศิลปิน ID {aid} (Pixiv / Danbooru)...")
        w = BooruNameUpdateWorker([aid])
        w.signals.api_updated.connect(self.handle_api_update)
        w.signals.api_log.connect(lambda msg: self.status_bar.showMessage(msg))
        self.api_pool.start(w)

    def show_file_context_menu(self, pos):
        sender_list = self.sender()
        if not sender_list: return
        index = sender_list.indexAt(pos)
        if not index.isValid(): return
        item_data = sender_list.model().get_item_data(index.row())
        if not item_data: return
        file_path = item_data.get('id')
        if not file_path: return

        menu = QMenu(self)
        
        action_lightbox = QAction("🔍 Open in Lightbox (ดูรูปทันใจ)", self)
        action_lightbox.triggered.connect(lambda: self.open_lightbox_viewer(index.row(), sender_list.model()))
        menu.addAction(action_lightbox)

        is_fav = is_file_favorite(DB_FILE, file_path)
        fav_label = "⭐ Remove from Favorites" if is_fav else "☆ Add to Favorites (is:fav)"
        action_fav = QAction(fav_label, self)
        action_fav.triggered.connect(lambda: self.toggle_favorite_for_file(file_path))
        menu.addAction(action_fav)

        action_open = QAction("🖼️ Open in Windows Photos (ภายนอก)", self)
        action_open.triggered.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(file_path)))
        menu.addAction(action_open)

        action_show_tags = QAction("🏷️ View AI Tags (ดูแท็กทั้งหมด)", self)
        action_show_tags.triggered.connect(lambda: self.show_ai_tags_dialog(file_path))
        menu.addAction(action_show_tags)

        action_explorer = QAction("📂 Show in Explorer", self)
        action_explorer.triggered.connect(lambda: self.open_in_explorer(file_path))
        menu.addAction(action_explorer)

        action_reload_thumb = QAction("🔄 Regenerate Thumbnail (สร้างภาพตัวอย่างใหม่)", self)
        action_reload_thumb.triggered.connect(lambda: self.regenerate_file_thumbnail(file_path, index.row(), sender_list.model()))
        menu.addAction(action_reload_thumb)

        menu.exec(sender_list.mapToGlobal(pos))

    def toggle_favorite_for_file(self, file_path):
        now_fav = toggle_file_favorite(DB_FILE, file_path)
        fname = os.path.basename(file_path)
        self.status_bar.showMessage(f"{'⭐ Added to Favorites' if now_fav else '☆ Removed from Favorites'}: {fname}", 3000)

    def regenerate_file_thumbnail(self, file_path, row_idx, model):
        try:
            pil_img, ftype = load_media_thumbnail(file_path)
            if pil_img:
                thumb_img = make_thumbnail_rgb(pil_img, (240, 240))
                bio = io.BytesIO()
                thumb_img.save(bio, 'JPEG', quality=85)
                blob = bio.getvalue()

                cursor = self.main_db_conn.cursor()
                cursor.execute("INSERT OR REPLACE INTO file_thumbnails (path, image_data) VALUES (?, ?)", (file_path, blob))
                self.main_db_conn.commit()

                qimg = QImage()
                qimg.loadFromData(blob)
                pix = QPixmap.fromImage(qimg)
                pix = add_indicator(pix, ftype)
                new_icon = QIcon(pix)

                if file_path in self.file_item_map:
                    self.file_item_map[file_path]['icon'] = new_icon
                    self.file_item_map[file_path]['loaded'] = True
                if file_path in self.detail_item_map:
                    self.detail_item_map[file_path]['icon'] = new_icon
                    self.detail_item_map[file_path]['loaded'] = True

                model.update_icon(row_idx, new_icon)
                self.status_bar.showMessage(f"Thumbnail regenerated for: {os.path.basename(file_path)}", 3000)
        except Exception as e:
            logging.error(f"Error regenerating thumbnail for {file_path}: {e}")

    def open_in_explorer(self, file_path):
        show_in_file_manager(file_path)

    def show_ai_tags_dialog(self, file_path):
        tags = get_file_tags(DB_FILE, file_path)
        dlg = QDialog(self)
        dlg.setWindowTitle("🏷️ AI Tags Information")
        dlg.resize(500, 420)
        
        dlg_layout = QVBoxLayout(dlg)
        dlg_layout.setContentsMargins(16, 16, 16, 16)
        dlg_layout.setSpacing(12)

        lbl_fn = QLabel(f"<b>File:</b> {html.escape(os.path.basename(file_path))}")
        lbl_fn.setWordWrap(True)
        dlg_layout.addWidget(lbl_fn)

        text_edit = QTextEdit()
        text_edit.setReadOnly(True)
        text_edit.setStyleSheet("""
            QTextEdit {
                background-color: #121216;
                color: #f3f4f6;
                border: 1px solid #2d2d34;
                border-radius: 6px;
                padding: 8px;
                font-size: 13px;
            }
        """)

        html_content = []
        if tags and (tags.get('all') or tags.get('custom') or tags.get('is_favorite')):
            if tags.get('is_favorite'):
                html_content.append("<b style='color: #fbbf24;'>⭐ Status:</b> Favorited (is:fav)<br><br>")

            if tags.get('custom'):
                html_content.append("<b style='color: #fcd34d;'>⭐ Custom Tags:</b><br>")
                custom_str = ", ".join([f"<b>#{html.escape(str(c[0]))}</b>" for c in tags['custom']])
                html_content.append(f"&nbsp;&nbsp;{custom_str}<br><br>")

            if tags.get('characters'):
                html_content.append("<b style='color: #60a5fa;'>👤 Characters:</b><br>")
                for c, conf in tags['characters']:
                    c_esc = html.escape(str(c))
                    c_title = html.escape(c.replace('_', ' ').title())
                    html_content.append(f"&nbsp;&nbsp;• <b>{c_title}</b> ({c_esc}) — <span style='color:#34d399;'>{int(conf*100)}%</span><br>")
                html_content.append("<br>")
            
            if tags.get('series'):
                html_content.append("<b style='color: #a78bfa;'>📚 Series:</b><br>")
                for s, conf in tags['series']:
                    s_esc = html.escape(str(s))
                    s_title = html.escape(s.replace('_', ' ').title())
                    html_content.append(f"&nbsp;&nbsp;• <b>{s_title}</b> ({s_esc}) — <span style='color:#34d399;'>{int(conf*100)}%</span><br>")
                html_content.append("<br>")

            if tags.get('rating'):
                r = str(tags['rating'])
                r_color = "#34d399" if r == "general" else "#f59e0b" if r in ("sensitive", "questionable") else "#ef4444"
                html_content.append(f"<b style='color: #fbbf24;'>🔞 Content Rating:</b> <span style='color: {r_color}; font-weight: bold;'>{html.escape(r.upper())}</span><br><br>")

            if tags.get('general'):
                html_content.append("<b style='color: #9ca3af;'>🏷️ General Tags:</b><br>")
                gen_tags = ", ".join([f"<span style='color:#d1d5db;'>{html.escape(str(g[0]))}</span> ({int(g[1]*100)}%)" for g in tags['general']])
                html_content.append(f"&nbsp;&nbsp;{gen_tags}<br>")
        else:
            html_content.append("<p style='color: #9ca3af;'>ℹ️ This image has not been scanned with AI Tagger yet.<br>Click <b>'🤖 AI Tagger'</b> on the top toolbar to scan your library.</p>")

        text_edit.setHtml("".join(html_content))
        dlg_layout.addWidget(text_edit)

        btn_row = QHBoxLayout()
        btn_copy = QPushButton("📋 Copy All Tags")
        def copy_tags():
            all_tag_names = [t[0] for t in (tags.get('all', []) if tags else []) if t[0] not in ('__none__', 'favorite')]
            if all_tag_names:
                QApplication.clipboard().setText(", ".join(all_tag_names))
                btn_copy.setText("✅ Copied!")
                safe_single_shot(1500, btn_copy, lambda: btn_copy.setText("📋 Copy All Tags"))
        btn_copy.clicked.connect(copy_tags)
        btn_row.addWidget(btn_copy)

        btn_close = QPushButton("Close")
        btn_close.clicked.connect(dlg.accept)
        btn_row.addWidget(btn_close)
        dlg_layout.addLayout(btn_row)

        dlg.exec()

    def randomize_single_folder_cover(self, aid):
        self.status_bar.showMessage(f"Randomizing cover for {aid}...")
        w = ThumbnailGeneratorWorker(aid, os.path.join(self.current_folder, aid), DB_FILE, self.scan_gen_id, force_update=True)
        w.signals.folder_updated.connect(self.handle_folder_updated)
        self.bg_pool.start(w)

    def randomize_folder_covers(self):
        msgBox = QMessageBox(self)
        msgBox.setWindowTitle("Randomize All Covers")
        msgBox.setText("Select Randomize Mode:\n\n"
                       "• Quick Scan: Checks for incomplete cover collages (folders with > 4 files showing blank spots) and regenerates them. Fast.\n"
                       "• Full Rebuild: Regenerates cover collages for ALL folders from scratch. Slow.")
        
        btn_quick = msgBox.addButton("Quick Scan (Recommended)", QMessageBox.ButtonRole.AcceptRole)
        btn_full = msgBox.addButton("Full Rebuild", QMessageBox.ButtonRole.RejectRole)
        btn_cancel = msgBox.addButton("Cancel", QMessageBox.ButtonRole.DestructiveRole)
        msgBox.setEscapeButton(btn_cancel)
        msgBox.setDefaultButton(btn_cancel)
        
        msgBox.exec()
        clicked = msgBox.clickedButton()
        if clicked == btn_cancel or clicked is None:
            return
            
        self.reset_and_kill_all()
        
        self.folders_model.update_data([])
        self.folder_item_map.clear()
        
        if clicked == btn_quick:
            self.force_thumbnail_update = False
            self.quick_randomize_covers = True
        else:
            self.force_thumbnail_update = True
            self.quick_randomize_covers = False
            
        self.start_scan_folders_cached(self.current_folder)

    def go_back(self):
        self.detail_scroll_timer.stop()
        self.fg_pool.clear()
        self.detail_model.update_data([])
        self.detail_item_map.clear()
        self.detail_icon_lru.clear()
        if self.rb_folders.isChecked():
            self.stacked_widget.setCurrentWidget(self.page_folders)
            self.folder_scroll_timer.start(25)
        else:
            self.stacked_widget.setCurrentWidget(self.page_files)
            self.scroll_timer.start(25)
        if self.get_current_queue(): self.scan_timer.start()
        self.btn_back.setVisible(False); self.path_input.setText(self.current_folder)

    def show_search_help_dialog(self):
        from components import SearchHelpDialog
        dlg = SearchHelpDialog(self, on_apply_query=self.apply_search_query_from_help)
        dlg.exec()

    def apply_search_query_from_help(self, query_text):
        self.search_input.setText(query_text)
        if self.rb_folders.isChecked():
            self.rb_all.setChecked(True)
        else:
            self.perform_search()

    def perform_search(self):
        raw_text = self.search_input.text().strip()
        from query_parser import parse_search_query
        parsed_q = parse_search_query(raw_text)

        active_rating = 'ALL'
        if hasattr(self, 'rating_filter'):
            active_rating = self.rating_filter.currentData() or 'ALL'
        if parsed_q.rating and parsed_q.rating != 'ALL':
            active_rating = parsed_q.rating

        if self.rb_folders.isChecked():
            has_tag_search = parsed_q.has_tag_or_rating_filter(active_rating=active_rating) or (active_rating != 'ALL') or bool(parsed_q.file_types)
            if has_tag_search:
                self.rb_all.setChecked(True)
                return

            kw = raw_text.lower()
            all_items = list(self.folder_item_map.values())
            if kw:
                filtered = [item for item in all_items if kw in item.get('name', '').lower() or kw in str(item.get('id', '').lower())]
            else:
                filtered = all_items
            self.folders_model.update_data(filtered)
            self.apply_sort(self.sort_combo.currentIndex())
            self.status_bar.showMessage(f"Found {len(filtered)} folders.")
            self.folder_scroll_timer.start(50)
        else:
            size_idx = self.size_filter.currentIndex()
            min_size, max_size = None, None
            if size_idx == 1:
                max_size = 2 * 1024 * 1024
            elif size_idx == 2:
                min_size = 2 * 1024 * 1024
                max_size = 10 * 1024 * 1024
            elif size_idx == 3:
                min_size = 10 * 1024 * 1024
                max_size = 50 * 1024 * 1024
            elif size_idx == 4:
                min_size = 50 * 1024 * 1024

            if parsed_q.min_size:
                min_size = parsed_q.min_size
            if parsed_q.max_size:
                max_size = parsed_q.max_size

            date_idx = self.date_filter.currentIndex()
            min_mtime = None
            if date_idx == 1:
                min_mtime = time.time() - 24 * 60 * 60
            elif date_idx == 2:
                min_mtime = time.time() - 7 * 24 * 60 * 60
            elif date_idx == 3:
                min_mtime = time.time() - 30 * 24 * 60 * 60
            elif date_idx == 4:
                min_mtime = time.time() - 365 * 24 * 60 * 60

            mode = 'ALL'
            if self.rb_photos.isChecked(): mode = 'PHOTO'
            elif self.rb_anim.isChecked(): mode = 'ANIMATE'

            char_filter_val = None
            if hasattr(self, 'char_filter'):
                char_filter_val = self.char_filter.currentData()
                if not char_filter_val:
                    raw_txt = self.char_filter.currentText().strip()
                    if raw_txt and raw_txt != "All Characters":
                        if '(' in raw_txt and raw_txt.endswith(')'):
                            char_filter_val = raw_txt[raw_txt.rfind('(') + 1 : -1].strip()
                        else:
                            char_filter_val = raw_txt.lower().replace(' ', '_')

            self.reset_and_kill_all()
            self.start_scan_all_files(self.current_folder, use_cache=True, filter_mode=mode,
                                      min_size=min_size, max_size=max_size, min_mtime=min_mtime,
                                      search_keyword=None,
                                      tag_filter=char_filter_val,
                                      parsed_query=parsed_q,
                                      rating_filter=active_rating)

    def clear_search(self):
        self.search_input.clear()
        self.size_filter.setCurrentIndex(0)
        self.date_filter.setCurrentIndex(0)
        if hasattr(self, 'char_filter'):
            self.char_filter.setCurrentIndex(0)
        if hasattr(self, 'rating_filter'):
            self.rating_filter.setCurrentIndex(0)
        self.perform_search()

    def open_ai_tag_dialog(self, specific_folder=None, artist_name=None):
        if not specific_folder:
            if hasattr(self, 'stacked_widget') and self.stacked_widget.currentWidget() == self.page_detail:
                specific_folder = self.path_input.text()
                artist_name = os.path.basename(specific_folder)
        dlg = AiTagDialog(self, initial_root_path=specific_folder, target_artist_name=artist_name)
        dlg.exec()
        self.refresh_character_filters()

    def open_dashboard_dialog(self):
        from dashboard_dialog import DashboardDialog
        dlg = DashboardDialog(self, on_apply_query=self.apply_search_query_from_help)
        dlg.exec()

    def open_duplicate_finder_dialog(self):
        dlg = DuplicateFinderDialog(self, root_path=self.current_folder or None, db_file=DB_FILE)
        dlg.exec()

    def open_dataset_exporter_dialog(self):
        current_paths = []
        if self.stacked_widget.currentWidget() == self.page_detail:
            current_paths = [it.get('id') for it in self.detail_model._data if it.get('id')]
        elif self.stacked_widget.currentWidget() == self.page_files:
            current_paths = [it.get('id') for it in self.files_model._data if it.get('id')]
        dlg = DatasetExporterDialog(self, file_paths=current_paths, db_file=DB_FILE)
        dlg.exec()

    def refresh_character_filters(self):
        if hasattr(self, 'char_filter'):
            current_data = self.char_filter.currentData()
            self.char_filter.blockSignals(True)
            self.char_filter.clear()
            self.char_filter.addItem("All Characters", None)
            chars = get_all_characters(DB_FILE, min_count=1)
            for c in chars:
                display_name = c.replace('_', ' ').title()
                self.char_filter.addItem(f"{display_name} ({c})", c)
            
            self.char_filter.view().setTextElideMode(Qt.TextElideMode.ElideNone)
            self.char_filter.view().setUniformItemSizes(True)
            self.char_filter.view().setMinimumWidth(380)
            self.char_filter.view().setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.char_filter.view().setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

            char_comp = self.char_filter.completer()
            if char_comp and char_comp.popup():
                char_comp.popup().setTextElideMode(Qt.TextElideMode.ElideNone)
                char_comp.popup().setUniformItemSizes(True)
                char_comp.popup().setMinimumWidth(380)
                char_comp.popup().setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
                char_comp.popup().setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

            idx = self.char_filter.findData(current_data)
            if idx >= 0:
                self.char_filter.setCurrentIndex(idx)
            else:
                self.char_filter.setCurrentIndex(0)
            self.char_filter.blockSignals(False)

        if hasattr(self, 'search_completer'):
            suggestions = get_all_search_suggestions(DB_FILE)
            model = QStringListModel(suggestions, self.search_completer)
            self.search_completer.setModel(model)

    def apply_sort(self, index):
        if index < 0: return
        if self.stacked_widget.currentWidget() == self.page_folders: model = self.folders_model
        elif self.stacked_widget.currentWidget() == self.page_files: model = self.files_model
        else: model = self.detail_model

        model.sort(index)
        
        if self.stacked_widget.currentWidget() == self.page_folders:
            self.folder_scroll_timer.start(10)
        elif self.stacked_widget.currentWidget() == self.page_files:
            self.scroll_timer.start(10)
        elif self.stacked_widget.currentWidget() == self.page_detail:
            self.detail_scroll_timer.start(10)

    def stop_all(self): 
        self.reset_and_kill_all()

    def load_config(self):
        try:
            self.config = load_app_config()
        except Exception as e:
            logging.error(f"Config load error: {e}")
            self.config = {}

    def save_config(self):
        try:
            self.config = update_app_config(self.config)
        except Exception as e:
            logging.error(f"Config save error: {e}")

    def browse_folder(self):
        f = QFileDialog.getExistingDirectory(self, "Select Folder")
        if f:
            self.current_folder = f
            self.path_input.setText(f)
            self.config['last_folder'] = f
            self.save_config()
            self.folders_model.update_data([])
            self.folder_item_map.clear()
            self.current_view_mode = None
            self.refresh_view()
            
    def load_last_session(self):
        f = self.config.get('last_folder')
        if f and os.path.exists(f):
            self.current_folder = f
            self.path_input.setText(f)
            self.folders_model.update_data([])
            self.folder_item_map.clear()
            self.current_view_mode = None
            QTimer.singleShot(100, self.refresh_view)

    def start_name_update(self):
        targets = []
        for item_dict in self.folders_model._data:
            aid = str(item_dict.get('id', ''))
            current_name = item_dict.get('name', '')
            if current_name == aid or current_name.startswith(f"{aid} ("):
                targets.append(aid)
                
        if not targets: 
            return QMessageBox.information(self, "Info", "ไม่มีโฟลเดอร์ไหนต้องอัปเดตชื่อแล้วครับ สมบูรณ์ 100%!")
            
        if len(targets) > 1000:
            reply = QMessageBox.question(self, "จัดชุดใหญ่!", 
                                         f"พบ {len(targets)} โฟลเดอร์ที่ต้องอัปเดต!\nการรันทั้งหมดรวดเดียวอาจใช้เวลาสักพัก\nยืนยันที่จะลุยเลยไหม?", 
                                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return
                
        self.status_bar.showMessage(f"กำลังลุยอัปเดตชื่อรวดเดียว {len(targets)} คน... (จิบกาแฟรอได้เลย ☕)")
        
        w = BooruNameUpdateWorker(targets)
        w.signals.api_updated.connect(self.handle_api_update)
        w.signals.api_log.connect(lambda msg: self.status_bar.showMessage(msg))
        self.api_pool.start(w)

    @pyqtSlot(str, str, object)
    def handle_api_update(self, aid, name, avatar_blob):
        item_dict = self.folder_item_map.get(aid)
        if item_dict:
            item_dict['name'] = f"{name} ({aid})"
            
            if avatar_blob:
                item_dict['profile_blob'] = avatar_blob
                if aid in self.folder_icon_lru or item_dict.get('icon_loaded'):
                    grid_blob = item_dict.get('grid_blob')
                    if grid_blob:
                        qimg = QImage.fromData(grid_blob)
                        if not qimg.isNull():
                            qimg = overlay_avatar_on_grid(qimg, avatar_blob)
                            item_dict['icon'] = QIcon(QPixmap.fromImage(qimg))
                            self.folder_icon_lru[aid] = True
            
            row = self.folders_model.get_row_by_id(aid)
            if row >= 0:
                idx = self.folders_model.index(row, 0)
                self.folders_model.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole, Qt.ItemDataRole.DisplayRole])

    # === Smooth Buffer & LRU Cache (Folder View) ===
    def load_visible_folder_icons(self):
        try:
            if self.stacked_widget.currentWidget() != self.page_folders:
                return

            total_items = self.folders_model.rowCount()
            if total_items == 0:
                return

            viewport = self.list_folders.viewport()
            w = viewport.width()
            h = viewport.height()
            if w <= 0 or h <= 0:
                return

            rows = []
            step_y = max(30, h // 8)
            step_x = max(30, w // 6)
            for y in range(0, h + step_y, step_y):
                cy = min(y, h - 1)
                for x in range(0, w + step_x, step_x):
                    cx = min(x, w - 1)
                    idx = self.list_folders.indexAt(QPoint(cx, cy))
                    if idx.isValid():
                        rows.append(idx.row())

            if not rows:
                min_row = 0
                max_row = min(total_items - 1, 60)
            else:
                min_row = min(rows)
                max_row = max(rows)

            buf_start = max(0, min_row - 50)
            buf_end = min(total_items, max_row + 120)

            current_active_ids = set()
            changed_rows = []

            for r in range(buf_start, buf_end):
                item_dict = self.folders_model.get_item_data(r)
                if not item_dict:
                    continue
                aid = item_dict['id']
                current_active_ids.add(aid)

                self.folder_icon_lru[aid] = True
                self.folder_icon_lru.move_to_end(aid)

                if not item_dict.get('icon_loaded', False):
                    grid_blob = item_dict.get('grid_blob')
                    if grid_blob:
                        qimg = QImage.fromData(grid_blob)
                        if not qimg.isNull():
                            profile_blob = item_dict.get('profile_blob')
                            if profile_blob:
                                qimg = overlay_avatar_on_grid(qimg, profile_blob)
                            item_dict['icon'] = QIcon(QPixmap.fromImage(qimg))
                    item_dict['icon_loaded'] = True
                    changed_rows.append(r)

            if changed_rows:
                min_c = min(changed_rows)
                max_c = max(changed_rows)
                top_idx = self.folders_model.index(min_c, 0)
                bottom_idx = self.folders_model.index(max_c, 0)
                self.folders_model.dataChanged.emit(top_idx, bottom_idx, [Qt.ItemDataRole.DecorationRole])

            while len(self.folder_icon_lru) > self.MAX_FOLDER_CACHE:
                old_aid, _ = self.folder_icon_lru.popitem(last=False)
                if old_aid not in current_active_ids:
                    item_dict = self.folder_item_map.get(old_aid)
                    if item_dict and item_dict.get('icon_loaded'):
                        item_dict['icon'] = self.default_folder_icon
                        item_dict['icon_loaded'] = False
                        row = self.folders_model.get_row_by_id(old_aid)
                        if row >= 0:
                            idx = self.folders_model.index(row, 0)
                            self.folders_model.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])

        except Exception as e:
            logging.debug(f"[main.py] Exception in load_visible_folder_icons: {e}")

    # === Smooth Buffer & LRU Cache (File View) ===
    def load_visible_thumbnails(self):
        try:
            if self.stacked_widget.currentWidget() != self.page_files:
                return

            total_items = self.files_model.rowCount()
            if total_items == 0:
                return

            viewport = self.list_files.viewport()
            w = viewport.width()
            h = viewport.height()
            if w <= 0 or h <= 0:
                return

            rows = []
            step_y = max(30, h // 8)
            step_x = max(30, w // 6)
            for y in range(0, h + step_y, step_y):
                cy = min(y, h - 1)
                for x in range(0, w + step_x, step_x):
                    cx = min(x, w - 1)
                    idx = self.list_files.indexAt(QPoint(cx, cy))
                    if idx.isValid():
                        rows.append(idx.row())

            if not rows:
                min_row = 0
                max_row = min(total_items - 1, 40)
            else:
                min_row = min(rows)
                max_row = max(rows)

            buf_start = max(0, min_row - 30)
            buf_end = min(total_items, max_row + 80)

            current_active_paths = set()
            for r in range(buf_start, buf_end):
                item_dict = self.files_model.get_item_data(r)
                if not item_dict:
                    continue
                path = item_dict['id']
                current_active_paths.add(path)

                if not item_dict.get('loaded', False):
                    item_dict['loaded'] = True
                    w = ImageLoaderWorker(path, DB_FILE, self.scan_gen_id, lambda: self.scan_gen_id)
                    w.signals.image_loaded.connect(self.update_single_file_thumbnail)
                    self.fg_pool.start(w)
                else:
                    if path in self.file_icon_lru:
                        self.file_icon_lru.move_to_end(path)

            while len(self.file_icon_lru) > self.MAX_FILE_CACHE:
                old_path, _ = self.file_icon_lru.popitem(last=False)
                if old_path not in current_active_paths:
                    item_dict = self.file_item_map.get(old_path)
                    if item_dict and item_dict.get('loaded'):
                        item_dict['icon'] = self.default_file_icon
                        item_dict['loaded'] = False
                        row = self.files_model.get_row_by_id(old_path)
                        if row >= 0:
                            idx = self.files_model.index(row, 0)
                            self.files_model.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])

        except Exception as e:
            logging.debug(f"[main.py] Exception in load_visible_thumbnails: {e}")

    @pyqtSlot(int, str, QImage, str)
    def update_single_file_thumbnail(self, gen_id, path, qimg, ftype):
        try:
            if gen_id != self.scan_gen_id: return
            if path in self.file_item_map:
                item_dict = self.file_item_map[path]
                if qimg and not qimg.isNull():
                    pix = QPixmap.fromImage(qimg)
                else:
                    pix = QPixmap(240, 240)
                    pix.fill(QColor(40, 40, 40))
                pix = add_indicator(pix, ftype)
                item_dict['icon'] = QIcon(pix)
                
                self.file_icon_lru[path] = True
                self.file_icon_lru.move_to_end(path)
                
                row = self.files_model.get_row_by_id(path)
                if row >= 0:
                    idx = self.files_model.index(row, 0)
                    self.files_model.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])
        except RuntimeError:
            pass
        except Exception as e:
            logging.debug(f"[main.py] Exception in update_single_file_thumbnail for {path}: {e}")

    def optimize_database(self):
        msg = QMessageBox.question(self, "Optimize Database",
                                   "This will clean up orphaned records, compact, and defragment the SQLite database (VACUUM & WAL Truncate) to reclaim disk space.\n\n"
                                   "This may take a few moments. Do you want to proceed?",
                                   QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if msg == QMessageBox.StandardButton.Yes:
            self.reset_and_kill_all()
            self.status_bar.showMessage("Optimizing & Compacting Database...")
            self.loading_overlay.start("🧹 กำลังบีบอัดและจัดระเบียบฐานข้อมูล (VACUUM)...")
            
            close_all_thread_local_conns()
            try:
                self.main_db_conn.close()
            except Exception:
                pass
                
            w = DatabaseOptimizerWorker(DB_FILE)
            w.signals.api_log.connect(self.handle_optimize_finished)
            self.bg_pool.start(w)

    def handle_optimize_finished(self, status):
        self.main_db_conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        self.loading_overlay.stop()
        if status == "SUCCESS":
            self.status_bar.showMessage("Database Optimization Complete!")
            QMessageBox.information(self, "Success", "Database compacted and optimized successfully!")
        else:
            self.status_bar.showMessage("Database Optimization Failed.")
            QMessageBox.warning(self, "Error", f"Failed to optimize database: {status}")

    def open_download_tool(self):
        dlg = PixivDownloadDialog(self, self.current_folder)
        dlg.exec()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'stacked_widget'):
            if self.stacked_widget.currentWidget() == self.page_folders:
                self.folder_scroll_timer.start(15)
            elif self.stacked_widget.currentWidget() == self.page_files:
                self.scroll_timer.start(25)
            elif self.stacked_widget.currentWidget() == self.page_detail:
                self.detail_scroll_timer.start(20)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == event.Type.WindowStateChange:
            if hasattr(self, 'stacked_widget'):
                if self.stacked_widget.currentWidget() == self.page_folders:
                    self.folder_scroll_timer.start(15)
                elif self.stacked_widget.currentWidget() == self.page_files:
                    self.scroll_timer.start(25)
                elif self.stacked_widget.currentWidget() == self.page_detail:
                    self.detail_scroll_timer.start(20)
            if not self.isMinimized():
                for w in QApplication.topLevelWidgets():
                    if isinstance(w, QDialog) and w.isVisible() and w.isMinimized():
                        w.showNormal()
                        w.raise_()
                        w.activateWindow()
            elif self.isMinimized():
                for w in QApplication.topLevelWidgets():
                    if isinstance(w, QDialog) and w.isVisible() and not w.isMinimized():
                        w.showMinimized()

    def closeEvent(self, e): 
        # 1. Hide window & accept close event immediately so UI vanishes in 0ms without freezing
        self.hide()
        e.accept()

        # 2. Stop background workers and interrupt any in-flight SQLite queries (<1ms)
        in_test = "unittest" in sys.modules
        self.stop_all()
        close_all_thread_local_conns(shutting_down=not in_test)

        # 3. Non-blocking PASSIVE WAL checkpoint (avoids multi-second exclusive lock wait on 5.8GB DB)
        try:
            self.main_db_conn.execute("PRAGMA busy_timeout = 100")
            self.main_db_conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception:
            pass
        try:
            self.main_db_conn.close()
        except Exception:
            pass
        if not in_test:
            QTimer.singleShot(30, lambda: os._exit(0))


if __name__ == "__main__":
    try:
        import ctypes
        myappid = 'krit.pixivmanager.app.v32'
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
    except Exception:
        pass

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = PixivManagerApp()
    window.show()
    sys.exit(app.exec())
