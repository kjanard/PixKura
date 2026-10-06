import os
import sys
import subprocess
import logging
from PIL import Image

try:
    import cv2
except ImportError:
    cv2 = None

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QWidget,
    QScrollArea, QFrame, QApplication, QSplitter, QSizePolicy, QComboBox,
    QStackedWidget, QSlider
)
from PyQt6.QtCore import Qt, QSize, QPoint, QPointF, QTimer, QRectF, QUrl, QEvent
from PyQt6.QtGui import (
    QPixmap, QImage, QPainter, QColor, QFont, QKeySequence, QShortcut,
    QWheelEvent, QMouseEvent, QMovie
)
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
from PyQt6.QtMultimediaWidgets import QVideoWidget

from config import DB_FILE, ALL_MEDIA_EXT, EXT_IMG, EXT_GIF, EXT_VID, EXT_ZIP
from database import get_file_tags
from utils import load_media_thumbnail, format_size, show_in_file_manager


def format_time_ms(ms: int) -> str:
    """Format milliseconds into MM:SS or HH:MM:SS string."""
    if ms < 0:
        return "00:00"
    total_sec = int(ms // 1000)
    m, s = divmod(total_sec, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


class ZoomableImageLabel(QWidget):
    """
    High-performance zoomable & pannable image display widget using custom paintEvent.
    Guarantees stable window dimensions without overflowing dialog layouts.
    Supports FIT, FILL, and ACTUAL (1:1) viewing modes.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(100, 100)
        self.setStyleSheet("background-color: #0c0c0e;")
        self.orig_pixmap = None
        self.placeholder_text = ""
        self.view_mode = 'FIT'  # 'FIT', 'FILL', 'ACTUAL', 'CUSTOM'
        self.zoom_factor = 1.0
        self.pan_offset = QPointF(0.0, 0.0)
        self.is_panning = False
        self.pan_start = QPointF(0.0, 0.0)
        self.on_zoom_changed = None
        self.setMouseTracking(True)

    def sizeHint(self):
        return QSize(750, 550)

    def set_image(self, pixmap):
        self.orig_pixmap = pixmap
        self.placeholder_text = ""
        # Maintain user's chosen view mode or default to FIT
        active_mode = self.view_mode if self.view_mode in ('FIT', 'FILL', 'ACTUAL') else 'FIT'
        self.set_view_mode(active_mode)

    def update_frame_pixmap(self, pixmap):
        """Updates the pixmap for animated frames without resetting pan and zoom."""
        if pixmap and not pixmap.isNull():
            self.orig_pixmap = pixmap
            self.update()

    def setText(self, text):
        self.orig_pixmap = None
        self.placeholder_text = text
        self.update()

    def set_view_mode(self, mode: str):
        """
        Sets image scaling mode:
          'FIT'    - Maintain aspect ratio, entirely visible inside viewport
          'FILL'   - Maintain aspect ratio, expand to cover viewport without letterboxing
          'ACTUAL' - 1:1 original pixel mapping (1 image px = 1 screen px)
        """
        self.view_mode = mode
        if not self.orig_pixmap or self.orig_pixmap.isNull():
            self.update()
            return

        vw = self.width()
        vh = self.height()
        iw = self.orig_pixmap.width()
        ih = self.orig_pixmap.height()
        if iw <= 0 or ih <= 0 or vw <= 0 or vh <= 0:
            return

        base_scale = min(vw / iw, vh / ih)

        if mode == 'FIT':
            self.zoom_factor = 1.0
            self.pan_offset = QPointF(0.0, 0.0)
        elif mode == 'FILL':
            fill_scale = max(vw / iw, vh / ih)
            self.zoom_factor = (fill_scale / base_scale) if base_scale > 0 else 1.0
            self.pan_offset = QPointF(0.0, 0.0)
        elif mode == 'ACTUAL':
            self.zoom_factor = (1.0 / base_scale) if base_scale > 0 else 1.0
            self.pan_offset = QPointF(0.0, 0.0)

        self.update()
        if self.on_zoom_changed:
            self.on_zoom_changed(self.zoom_factor, self.view_mode)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.view_mode in ('FIT', 'FILL', 'ACTUAL'):
            self.set_view_mode(self.view_mode)

    def reset_zoom(self):
        self.set_view_mode('FIT')

    def zoom_to_actual(self):
        self.set_view_mode('ACTUAL')

    def zoom_by(self, factor, center_pos=None):
        if not self.orig_pixmap or self.orig_pixmap.isNull():
            return

        old_zoom = self.zoom_factor
        new_zoom = max(0.2, min(20.0, old_zoom * factor))
        if abs(new_zoom - old_zoom) < 1e-4:
            return

        if center_pos is None:
            center_pos = QPointF(self.width() / 2.0, self.height() / 2.0)
        else:
            center_pos = QPointF(center_pos)

        cx = self.width() / 2.0
        cy = self.height() / 2.0

        rel_x = center_pos.x() - (cx + self.pan_offset.x())
        rel_y = center_pos.y() - (cy + self.pan_offset.y())
        scale_ratio = new_zoom / old_zoom

        new_pan_x = center_pos.x() - cx - (rel_x * scale_ratio)
        new_pan_y = center_pos.y() - cy - (rel_y * scale_ratio)

        self.zoom_factor = new_zoom
        self.view_mode = 'CUSTOM'
        if self.zoom_factor <= 1.0:
            self.pan_offset = QPointF(0.0, 0.0)
        else:
            self.pan_offset = QPointF(new_pan_x, new_pan_y)

        self.update()
        if self.on_zoom_changed:
            self.on_zoom_changed(self.zoom_factor, self.view_mode)

    def wheelEvent(self, event: QWheelEvent):
        delta = event.angleDelta().y()
        if delta == 0:
            delta = event.angleDelta().x()
        factor = 1.2 if delta > 0 else (1.0 / 1.2)
        pos = event.position() if hasattr(event, 'position') else event.pos()
        self.zoom_by(factor, pos)

    def mousePressEvent(self, event: QMouseEvent):
        pos = event.position() if hasattr(event, 'position') else event.pos()
        if event.button() == Qt.MouseButton.LeftButton:
            self.is_panning = True
            self.pan_start = QPointF(pos)
            if self.zoom_factor > 1.0 or self.view_mode in ('FILL', 'ACTUAL'):
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif event.button() == Qt.MouseButton.MiddleButton:
            self.reset_zoom()

    def mouseMoveEvent(self, event: QMouseEvent):
        pos = event.position() if hasattr(event, 'position') else event.pos()
        if self.is_panning and (self.zoom_factor > 1.0 or self.view_mode in ('FILL', 'ACTUAL')):
            curr_pos = QPointF(pos)
            delta = curr_pos - self.pan_start
            self.pan_start = curr_pos
            self.pan_offset += delta
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            self.is_panning = False
            self.setCursor(Qt.CursorShape.ArrowCursor)

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            if self.view_mode == 'FIT':
                self.set_view_mode('ACTUAL')
            else:
                self.set_view_mode('FIT')

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.fillRect(self.rect(), QColor("#0c0c0e"))

        if not self.orig_pixmap or self.orig_pixmap.isNull():
            painter.setPen(QColor("#6b7280"))
            painter.setFont(QFont("Segoe UI", 13))
            text = self.placeholder_text or "No Image"
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, text)
            return

        vw = self.width()
        vh = self.height()
        iw = self.orig_pixmap.width()
        ih = self.orig_pixmap.height()
        if iw <= 0 or ih <= 0 or vw <= 0 or vh <= 0:
            return

        base_scale = min(vw / iw, vh / ih)
        total_scale = base_scale * self.zoom_factor

        draw_w = iw * total_scale
        draw_h = ih * total_scale

        cx = vw / 2.0
        cy = vh / 2.0

        if self.zoom_factor > 1.0:
            max_pan_x = max(0.0, (draw_w - vw) / 2.0 + vw * 0.4)
            max_pan_y = max(0.0, (draw_h - vh) / 2.0 + vh * 0.4)
            self.pan_offset.setX(max(-max_pan_x, min(max_pan_x, self.pan_offset.x())))
            self.pan_offset.setY(max(-max_pan_y, min(max_pan_y, self.pan_offset.y())))
        else:
            self.pan_offset = QPointF(0.0, 0.0)

        draw_x = cx + self.pan_offset.x() - draw_w / 2.0
        draw_y = cy + self.pan_offset.y() - draw_h / 2.0

        target_rect = QRectF(draw_x, draw_y, draw_w, draw_h)
        source_rect = QRectF(0.0, 0.0, float(iw), float(ih))

        painter.drawPixmap(target_rect, self.orig_pixmap, source_rect)

        # Subtle zoom percentage badge in bottom-left corner when zoomed
        if abs(self.zoom_factor - 1.0) > 0.01:
            zoom_pct = int(self.zoom_factor * 100)
            badge_text = f"🔍 {zoom_pct}%"
            painter.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
            badge_rect = QRectF(16, vh - 40, 75, 24)
            painter.setBrush(QColor(20, 20, 28, 190))
            painter.setPen(QColor(255, 255, 255, 40))
            painter.drawRoundedRect(badge_rect, 6, 6)
            painter.setPen(QColor("#93c5fd"))
            painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, badge_text)


class VideoDisplayWidget(QVideoWidget):
    """
    Hardware-accelerated video display widget supporting click-to-play/pause,
    double-click fullscreen, and scroll wheel volume control.
    """
    def __init__(self, parent_dialog=None):
        super().__init__(parent_dialog)
        self.parent_dialog = parent_dialog
        self.setStyleSheet("background-color: #0c0c0e;")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            if self.parent_dialog:
                self.parent_dialog.toggle_play_pause()
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            if self.parent_dialog:
                self.parent_dialog.toggle_fullscreen()
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event: QWheelEvent):
        delta = event.angleDelta().y()
        if self.parent_dialog:
            if delta > 0:
                self.parent_dialog.volume_up()
            elif delta < 0:
                self.parent_dialog.volume_down()
        super().wheelEvent(event)


from components import MinimizableDialog


class LightboxViewerDialog(MinimizableDialog):
    """
    Fast In-App Media Viewer (Lightbox) with instant keyboard navigation,
    animated GIF/WebP playback, Pixiv Ugoira viewer, full video player,
    and comprehensive AI tag metadata sidebar.
    """
    SPEEDS = [0.5, 1.0, 1.5, 2.0]

    def __init__(self, file_paths, current_index=0, parent=None):
        super().__init__(parent)
        self.file_paths = file_paths or []
        self.current_idx = max(0, min(current_index, len(self.file_paths) - 1)) if self.file_paths else 0

        # Playback & state variables
        self.current_media_type = 'IMAGE'  # 'IMAGE', 'ANIMATION', 'VIDEO'
        self.current_movie = None
        self.ugoira_frames = []
        self.ugoira_idx = 0
        self.ugoira_timer = None
        self.cv2_cap = None
        self.cv2_timer = None
        self.cv2_fps = 30.0
        self.cv2_total_frames = 0
        self.cv2_curr_frame = 0
        self.is_looping = True
        self.speed_idx = 1  # 1.0x default
        self.current_video_res = ""
        self.current_tags = []

        # Setup Qt Multimedia
        self.media_player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.media_player.setAudioOutput(self.audio_output)
        self.audio_output.setVolume(0.8)  # 80% default volume

        self.setWindowTitle("🖼️ PixKura Lightbox Viewer")
        self.resize(1180, 800)
        self.setStyleSheet("""
            QDialog {
                background-color: #0d0d11;
                color: #e5e7eb;
            }
            QFrame.sidebar {
                background-color: #14141c;
                border-left: 1px solid #272733;
            }
            QPushButton.nav-btn {
                background-color: rgba(24, 24, 32, 0.7);
                color: #ffffff;
                border: 1px solid rgba(255, 255, 255, 0.15);
                border-radius: 22px;
                font-size: 20px;
                font-weight: bold;
                width: 44px;
                height: 44px;
            }
            QPushButton.nav-btn:hover {
                background-color: #3b82f6;
                border-color: #60a5fa;
            }
            QPushButton.action-btn {
                background-color: #222230;
                color: #f3f4f6;
                border: 1px solid #2d2d38;
                border-radius: 6px;
                padding: 6px 12px;
                font-weight: 600;
                font-size: 12px;
            }
            QPushButton.action-btn:hover {
                background-color: #3b82f6;
                color: #ffffff;
            }
            QLabel.section-hdr {
                color: #9ca3af;
                font-size: 11px;
                font-weight: bold;
                text-transform: uppercase;
                margin-top: 10px;
                margin-bottom: 4px;
            }
            QScrollArea {
                border: none;
                background: transparent;
            }
            QComboBox.view-mode-combo {
                background-color: #1f1f2b;
                color: #f3f4f6;
                border: 1px solid #323244;
                border-radius: 6px;
                padding: 4px 10px;
                font-weight: 600;
                font-size: 12px;
                min-width: 140px;
            }
            QComboBox.view-mode-combo:hover {
                border-color: #3b82f6;
                background-color: #272738;
            }
            QComboBox.view-mode-combo::drop-down {
                border: none;
                width: 20px;
            }
            QComboBox.view-mode-combo::down-arrow {
                image: none;
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-top: 5px solid #9ca3af;
                margin-right: 6px;
            }
            QComboBox.view-mode-combo QAbstractItemView {
                background-color: #181822;
                color: #f3f4f6;
                selection-background-color: #3b82f6;
                selection-color: #ffffff;
                border: 1px solid #323244;
                border-radius: 6px;
                padding: 4px;
                outline: none;
            }
        """)

        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Left / Center Area: Media Display with Top Bar, Stacked Viewers, and Media Bar
        img_container = QWidget()
        img_layout = QVBoxLayout(img_container)
        img_layout.setContentsMargins(0, 0, 0, 0)
        img_layout.setSpacing(0)

        # Top Bar (Filename, Resolution, Counter, Close)
        top_bar = QWidget()
        top_bar.setStyleSheet("background-color: #121218; padding: 6px 12px; border-bottom: 1px solid #22222e;")
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(12, 6, 12, 6)

        self.lbl_counter = QLabel("0 / 0")
        self.lbl_counter.setStyleSheet("font-size: 13px; font-weight: bold; color: #60a5fa; min-width: 80px;")
        top_layout.addWidget(self.lbl_counter)

        self.lbl_filename = QLabel("")
        self.lbl_filename.setStyleSheet("font-size: 13px; font-weight: 600; color: #f3f4f6;")
        top_layout.addWidget(self.lbl_filename, 1)

        self.lbl_resolution = QLabel("")
        self.lbl_resolution.setStyleSheet("font-size: 12px; color: #9ca3af; margin-right: 12px;")
        top_layout.addWidget(self.lbl_resolution)

        btn_toggle_sidebar = QPushButton("🏷️ Tags")
        btn_toggle_sidebar.setProperty("class", "action-btn")
        btn_toggle_sidebar.clicked.connect(self.toggle_sidebar)
        top_layout.addWidget(btn_toggle_sidebar)

        # Sizing Mode ComboBox (Fit, Fill, Actual Size)
        self.combo_scale = QComboBox()
        self.combo_scale.setProperty("class", "view-mode-combo")
        self.combo_scale.addItem("🔍 Fit to Window", "FIT")
        self.combo_scale.addItem("📐 Fill / Cover", "FILL")
        self.combo_scale.addItem("🎯 Actual Size (1:1)", "ACTUAL")
        self.combo_scale.currentIndexChanged.connect(self.on_scale_mode_selected)
        top_layout.addWidget(self.combo_scale)
        self.btn_fit = self.combo_scale  # Backward compatibility

        btn_close = QPushButton("✕")
        btn_close.setStyleSheet("background-color: #272733; color: white; border-radius: 14px; font-weight: bold; width: 28px; height: 28px;")
        btn_close.clicked.connect(self.accept)
        top_layout.addWidget(btn_close)

        img_layout.addWidget(top_bar)

        # Viewer area with Previous & Next hover buttons
        viewer_area = QWidget()
        viewer_layout = QHBoxLayout(viewer_area)
        viewer_layout.setContentsMargins(10, 10, 10, 10)
        viewer_layout.setSpacing(6)

        # Prev Button
        self.btn_prev = QPushButton("⟨")
        self.btn_prev.setProperty("class", "nav-btn")
        self.btn_prev.clicked.connect(self.show_prev_image)
        viewer_layout.addWidget(self.btn_prev, alignment=Qt.AlignmentFlag.AlignVCenter)

        # Center Display Area (Stack + Media Bar)
        center_area = QWidget()
        center_layout = QVBoxLayout(center_area)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(4)

        # Stacked Viewer (0: ZoomableImageLabel, 1: VideoDisplayWidget)
        self.display_stack = QStackedWidget()

        self.img_view = ZoomableImageLabel(self)
        self.img_view.on_zoom_changed = self.on_zoom_changed
        self.display_stack.addWidget(self.img_view)  # Page 0

        self.video_widget = VideoDisplayWidget(self)
        self.media_player.setVideoOutput(self.video_widget)
        self.display_stack.addWidget(self.video_widget)  # Page 1

        center_layout.addWidget(self.display_stack, 1)

        # Media Control Bar (Play/Pause, Timeline, Frame/Time, Loop, Speed, Volume)
        self.setup_media_bar(center_layout)

        viewer_layout.addWidget(center_area, 1)

        # Next Button
        self.btn_next = QPushButton("⟩")
        self.btn_next.setProperty("class", "nav-btn")
        self.btn_next.clicked.connect(self.show_next_image)
        viewer_layout.addWidget(self.btn_next, alignment=Qt.AlignmentFlag.AlignVCenter)

        img_layout.addWidget(viewer_area, 1)
        main_layout.addWidget(img_container, 1)

        # Right Area: AI Tags & Metadata Sidebar
        self.sidebar_frame = QFrame()
        self.sidebar_frame.setProperty("class", "sidebar")
        self.sidebar_frame.setFixedWidth(330)
        self.setup_sidebar()
        main_layout.addWidget(self.sidebar_frame)

        # Connect QMediaPlayer signals
        self.media_player.positionChanged.connect(self.on_player_position_changed)
        self.media_player.durationChanged.connect(self.on_player_duration_changed)
        self.media_player.playbackStateChanged.connect(self.on_player_playback_state_changed)
        self.media_player.mediaStatusChanged.connect(self.on_player_media_status_changed)
        self.media_player.errorOccurred.connect(self.on_player_error)

        # Keyboard Navigation & Media Shortcuts
        QShortcut(QKeySequence("Left"), self, self.show_prev_image)
        QShortcut(QKeySequence("A"), self, self.show_prev_image)
        QShortcut(QKeySequence("Right"), self, self.show_next_image)
        QShortcut(QKeySequence("D"), self, self.show_next_image)
        QShortcut(QKeySequence("Escape"), self, self.accept)
        QShortcut(QKeySequence("Space"), self, self.on_space_pressed)
        QShortcut(QKeySequence("F"), self, self.toggle_fullscreen)
        QShortcut(QKeySequence("L"), self, self.toggle_loop)
        QShortcut(QKeySequence("M"), self, self.toggle_mute)
        QShortcut(QKeySequence("K"), self, self.toggle_play_pause)
        QShortcut(QKeySequence("Up"), self, self.volume_up)
        QShortcut(QKeySequence("Down"), self, self.volume_down)
        QShortcut(QKeySequence("+"), self, lambda: self.img_view.zoom_by(1.25))
        QShortcut(QKeySequence("="), self, lambda: self.img_view.zoom_by(1.25))
        QShortcut(QKeySequence("-"), self, lambda: self.img_view.zoom_by(0.8))
        QShortcut(QKeySequence("0"), self, self.img_view.reset_zoom)
        QShortcut(QKeySequence("R"), self, self.img_view.reset_zoom)
        QShortcut(QKeySequence("1"), self, lambda: self.set_scale_mode("FIT"))
        QShortcut(QKeySequence("2"), self, lambda: self.set_scale_mode("FILL"))
        QShortcut(QKeySequence("3"), self, lambda: self.set_scale_mode("ACTUAL"))
        QShortcut(QKeySequence("Ctrl+E"), self, self.open_explorer)
        QShortcut(QKeySequence("E"), self, self.open_explorer)

        # Load initial media
        self.update_current_display()

    def setup_media_bar(self, parent_layout):
        """Constructs sleek media playback controls bar for video and animations."""
        self.media_bar = QFrame()
        self.media_bar.setProperty("class", "media-bar")
        self.media_bar.setStyleSheet("""
            QFrame.media-bar {
                background-color: #14141c;
                border: 1px solid #272733;
                border-radius: 8px;
                padding: 4px 12px;
                margin: 0px 4px 6px 4px;
            }
        """)
        media_layout = QHBoxLayout(self.media_bar)
        media_layout.setContentsMargins(6, 4, 6, 4)
        media_layout.setSpacing(10)

        # Play / Pause button
        self.btn_play_pause = QPushButton("⏸")
        self.btn_play_pause.setProperty("class", "action-btn")
        self.btn_play_pause.setToolTip("Play / Pause (Space or K)")
        self.btn_play_pause.setStyleSheet("font-size: 14px; min-width: 36px; padding: 4px 8px;")
        self.btn_play_pause.clicked.connect(self.toggle_play_pause)
        media_layout.addWidget(self.btn_play_pause)

        # Time / Frame indicator
        self.lbl_time = QLabel("00:00 / 00:00")
        self.lbl_time.setStyleSheet("font-size: 12px; font-weight: 600; color: #9ca3af; min-width: 95px; font-family: Consolas, monospace;")
        media_layout.addWidget(self.lbl_time)

        # Progress / Timeline Slider
        self.slider_timeline = QSlider(Qt.Orientation.Horizontal)
        self.slider_timeline.setRange(0, 100)
        self.slider_timeline.setValue(0)
        self.slider_timeline.setStyleSheet("""
            QSlider::groove:horizontal {
                height: 6px;
                background: #272738;
                border-radius: 3px;
            }
            QSlider::sub-page:horizontal {
                background: #3b82f6;
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                background: #ffffff;
                border: 2px solid #3b82f6;
                width: 14px;
                height: 14px;
                margin: -4px 0;
                border-radius: 7px;
            }
            QSlider::handle:horizontal:hover {
                background: #60a5fa;
            }
        """)
        self.slider_timeline.sliderMoved.connect(self.on_timeline_slider_moved)
        self.slider_timeline.sliderReleased.connect(self.on_timeline_slider_released)
        media_layout.addWidget(self.slider_timeline, 1)

        # Loop Toggle Button
        self.btn_loop = QPushButton("🔁 Loop")
        self.btn_loop.setProperty("class", "action-btn")
        self.btn_loop.setToolTip("Toggle Repeat (L)")
        self.btn_loop.setCheckable(True)
        self.btn_loop.setChecked(True)
        self.btn_loop.setStyleSheet("background-color: #2563eb; color: #ffffff; border-color: #3b82f6; font-size: 11px; padding: 4px 8px;")
        self.btn_loop.clicked.connect(self.toggle_loop)
        media_layout.addWidget(self.btn_loop)

        # Speed Button
        self.btn_speed = QPushButton("1.0x")
        self.btn_speed.setProperty("class", "action-btn")
        self.btn_speed.setToolTip("Playback Speed (0.5x, 1x, 1.5x, 2x)")
        self.btn_speed.setStyleSheet("font-size: 11px; padding: 4px 8px; min-width: 44px;")
        self.btn_speed.clicked.connect(self.cycle_speed)
        media_layout.addWidget(self.btn_speed)

        # Volume Container (Volume button + slider, visible for video)
        self.volume_container = QWidget()
        vol_layout = QHBoxLayout(self.volume_container)
        vol_layout.setContentsMargins(0, 0, 0, 0)
        vol_layout.setSpacing(6)

        self.btn_mute = QPushButton("🔊")
        self.btn_mute.setProperty("class", "action-btn")
        self.btn_mute.setToolTip("Mute / Unmute (M)")
        self.btn_mute.setStyleSheet("font-size: 13px; min-width: 32px; padding: 4px 6px;")
        self.btn_mute.clicked.connect(self.toggle_mute)
        vol_layout.addWidget(self.btn_mute)

        self.slider_volume = QSlider(Qt.Orientation.Horizontal)
        self.slider_volume.setRange(0, 100)
        self.slider_volume.setValue(80)
        self.slider_volume.setFixedWidth(70)
        self.slider_volume.setToolTip("Volume (Up/Down arrow)")
        self.slider_volume.setStyleSheet("""
            QSlider::groove:horizontal {
                height: 4px;
                background: #272738;
                border-radius: 2px;
            }
            QSlider::sub-page:horizontal {
                background: #10b981;
                border-radius: 2px;
            }
            QSlider::handle:horizontal {
                background: #ffffff;
                border: 2px solid #10b981;
                width: 12px;
                height: 12px;
                margin: -4px 0;
                border-radius: 6px;
            }
        """)
        self.slider_volume.valueChanged.connect(self.on_volume_slider_changed)
        vol_layout.addWidget(self.slider_volume)

        media_layout.addWidget(self.volume_container)

        parent_layout.addWidget(self.media_bar)
        self.media_bar.setVisible(False)

    def setup_sidebar(self):
        sidebar_layout = QVBoxLayout(self.sidebar_frame)
        sidebar_layout.setContentsMargins(16, 16, 16, 16)
        sidebar_layout.setSpacing(10)

        # Sidebar Title
        lbl_info = QLabel("🏷️ Image & AI Metadata")
        lbl_info.setStyleSheet("font-size: 15px; font-weight: bold; color: #60a5fa;")
        sidebar_layout.addWidget(lbl_info)

        # Rating Badge
        self.lbl_rating_badge = QLabel("RATING: UNKNOWN")
        self.lbl_rating_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_rating_badge.setStyleSheet("""
            background-color: #1f2937;
            color: #9ca3af;
            border-radius: 6px;
            padding: 6px;
            font-size: 13px;
            font-weight: bold;
        """)
        sidebar_layout.addWidget(self.lbl_rating_badge)

        # Scrollable metadata area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content_w = QWidget()
        self.content_layout = QVBoxLayout(content_w)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(6)

        # Details Section
        self.lbl_filesize = QLabel("Size: -")
        self.lbl_filesize.setStyleSheet("color: #d1d5db; font-size: 12px;")
        self.content_layout.addWidget(self.lbl_filesize)

        # Characters Section
        self.hdr_chars = QLabel("👤 Characters:")
        self.hdr_chars.setProperty("class", "section-hdr")
        self.content_layout.addWidget(self.hdr_chars)
        self.lbl_chars = QLabel("None")
        self.lbl_chars.setWordWrap(True)
        self.lbl_chars.setStyleSheet("color: #93c5fd; font-size: 12px; line-height: 1.4;")
        self.content_layout.addWidget(self.lbl_chars)

        # Series Section
        self.hdr_series = QLabel("📚 Series / Copyright:")
        self.hdr_series.setProperty("class", "section-hdr")
        self.content_layout.addWidget(self.hdr_series)
        self.lbl_series = QLabel("None")
        self.lbl_series.setWordWrap(True)
        self.lbl_series.setStyleSheet("color: #c4b5fd; font-size: 12px; line-height: 1.4;")
        self.content_layout.addWidget(self.lbl_series)

        # General Tags Section
        self.hdr_tags = QLabel("🏷️ General Tags:")
        self.hdr_tags.setProperty("class", "section-hdr")
        self.content_layout.addWidget(self.hdr_tags)
        self.lbl_tags = QLabel("None")
        self.lbl_tags.setWordWrap(True)
        self.lbl_tags.setStyleSheet("color: #d1d5db; font-size: 11px; line-height: 1.4;")
        self.content_layout.addWidget(self.lbl_tags)

        self.content_layout.addStretch()
        scroll.setWidget(content_w)
        sidebar_layout.addWidget(scroll, 1)

        # Action Buttons
        self.btn_copy_tags = QPushButton("📋 Copy All Tags")
        self.btn_copy_tags.setProperty("class", "action-btn")
        self.btn_copy_tags.clicked.connect(self.copy_tags)
        sidebar_layout.addWidget(self.btn_copy_tags)

        self.btn_explorer = QPushButton("📂 Open in Explorer")
        self.btn_explorer.setProperty("class", "action-btn")
        self.btn_explorer.clicked.connect(self.open_explorer)
        sidebar_layout.addWidget(self.btn_explorer)

    def stop_all_playback(self):
        """Immediately stops all audio, video, movie, and timer playback."""
        if hasattr(self, 'media_player') and self.media_player:
            try:
                self.media_player.stop()
                self.media_player.setSource(QUrl())
            except Exception:
                pass
        if hasattr(self, 'current_movie') and self.current_movie:
            try:
                self.current_movie.stop()
            except Exception:
                pass
            self.current_movie = None
        if hasattr(self, 'ugoira_timer') and self.ugoira_timer:
            try:
                self.ugoira_timer.stop()
            except Exception:
                pass
            self.ugoira_timer = None
        self.ugoira_frames = []
        if hasattr(self, 'cv2_timer') and self.cv2_timer:
            try:
                self.cv2_timer.stop()
            except Exception:
                pass
            self.cv2_timer = None
        if hasattr(self, 'cv2_cap') and self.cv2_cap:
            try:
                self.cv2_cap.release()
            except Exception:
                pass
            self.cv2_cap = None

    def update_current_display(self):
        """Loads and plays the current media file (Image, GIF/WebP animation, Ugoira, or Video)."""
        self.stop_all_playback()

        if not self.file_paths or self.current_idx >= len(self.file_paths):
            self.lbl_counter.setText("0 / 0")
            self.lbl_filename.setText("No file")
            self.lbl_resolution.setText("")
            self.img_view.setText("No Image")
            self.media_bar.setVisible(False)
            return

        path = self.file_paths[self.current_idx]
        filename = os.path.basename(path)
        ext = os.path.splitext(path)[1].lower()

        # Update Top Bar Counter
        self.lbl_counter.setText(f"{self.current_idx + 1} / {len(self.file_paths)}")

        # Reset Media Controls UI State
        self.slider_timeline.blockSignals(True)
        self.slider_timeline.setRange(0, 100)
        self.slider_timeline.setValue(0)
        self.slider_timeline.blockSignals(False)
        self.btn_play_pause.setText("⏸")
        self.lbl_time.setText("00:00 / 00:00")
        self.btn_speed.setText(f"{self.SPEEDS[self.speed_idx]}x")

        # ----------------------------------------------------
        # 1. VIDEO FILES (.mp4, .webm, .mkv, .mov, .avi)
        # ----------------------------------------------------
        if ext in EXT_VID:
            self.current_media_type = 'VIDEO'
            self.lbl_filename.setText(f"🎬 {filename}")
            self.display_stack.setCurrentIndex(1)  # Video widget
            self.media_bar.setVisible(True)
            self.volume_container.setVisible(True)

            vw, vh = 0, 0
            if cv2:
                try:
                    cap = cv2.VideoCapture(path)
                    vw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                    vh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    cap.release()
                except Exception:
                    pass
            self.current_video_res = f"{vw} × {vh}" if vw > 0 and vh > 0 else "Video"
            self.lbl_resolution.setText(self.current_video_res)

            # Start playback with QMediaPlayer
            try:
                self.media_player.setSource(QUrl.fromLocalFile(os.path.abspath(path)))
                self.media_player.setLoops(QMediaPlayer.Loops.Infinite if self.is_looping else 1)
                self.media_player.setPlaybackRate(self.SPEEDS[self.speed_idx])
                self.media_player.play()
            except Exception as e:
                logging.error(f"Error starting video playback: {e}")
                self.start_cv2_video_playback(path)

        # ----------------------------------------------------
        # 2. ANIMATED GIF (.gif)
        # ----------------------------------------------------
        elif ext in EXT_GIF:
            self.current_media_type = 'ANIMATION'
            self.lbl_filename.setText(f"🎞️ {filename}")
            self.display_stack.setCurrentIndex(0)  # Zoomable image label
            self.media_bar.setVisible(True)
            self.volume_container.setVisible(False)

            movie = QMovie(path)
            movie.setCacheMode(QMovie.CacheMode.CacheAll)
            if movie.isValid():
                self.current_movie = movie
                movie.start()
                first_pix = movie.currentPixmap()
                if not first_pix.isNull():
                    self.img_view.set_image(first_pix)
                    fc = movie.frameCount()
                    fc_str = f" • {fc} frames" if fc > 0 else ""
                    self.lbl_resolution.setText(f"{first_pix.width()} × {first_pix.height()}{fc_str}")
                movie.frameChanged.connect(self.on_movie_frame_changed)
                total = movie.frameCount()
                if total > 0:
                    self.slider_timeline.setRange(0, total - 1)
                    self.slider_timeline.setValue(0)
                    self.lbl_time.setText(f"Frame 1 / {total}")
                else:
                    self.slider_timeline.setRange(0, 100)
                    self.lbl_time.setText("Frame 1")
                self.current_movie.setSpeed(int(self.SPEEDS[self.speed_idx] * 100))
            else:
                pix = QPixmap(path)
                if pix and not pix.isNull():
                    self.lbl_resolution.setText(f"{pix.width()} × {pix.height()}")
                    self.img_view.set_image(pix)
                else:
                    self.img_view.setText("Cannot preview GIF")
                self.media_bar.setVisible(False)

        # ----------------------------------------------------
        # 3. PIXIV UGOIRA / ZIP ARCHIVES (.zip, .ugoira)
        # ----------------------------------------------------
        elif ext in EXT_ZIP:
            frames = self.load_ugoira_frames(path)
            if frames and len(frames) > 1:
                self.current_media_type = 'ANIMATION'
                self.lbl_filename.setText(f"📦 {filename}")
                self.display_stack.setCurrentIndex(0)
                self.media_bar.setVisible(True)
                self.volume_container.setVisible(False)
                self.ugoira_frames = frames
                self.ugoira_idx = 0
                first_pix, delay = frames[0]
                self.img_view.set_image(first_pix)
                self.lbl_resolution.setText(f"{first_pix.width()} × {first_pix.height()} • {len(frames)} frames")
                self.slider_timeline.setRange(0, len(frames) - 1)
                self.slider_timeline.setValue(0)
                self.lbl_time.setText(f"Frame 1 / {len(frames)}")
                self.start_ugoira_timer(delay)
            elif frames and len(frames) == 1:
                self.current_media_type = 'IMAGE'
                self.lbl_filename.setText(f"📦 {filename}")
                self.display_stack.setCurrentIndex(0)
                self.media_bar.setVisible(False)
                self.img_view.set_image(frames[0][0])
                self.lbl_resolution.setText(f"{frames[0][0].width()} × {frames[0][0].height()}")
            else:
                # Fallback to thumbnail generator
                self.current_media_type = 'IMAGE'
                self.lbl_filename.setText(f"📦 {filename}")
                self.display_stack.setCurrentIndex(0)
                self.media_bar.setVisible(False)
                pil_img, _ = load_media_thumbnail(path)
                if pil_img:
                    from io import BytesIO
                    bio = BytesIO()
                    pil_img.save(bio, "JPEG")
                    pix = QPixmap.fromImage(QImage.fromData(bio.getvalue()))
                    self.lbl_resolution.setText(f"{pil_img.width} × {pil_img.height}")
                    self.img_view.set_image(pix)
                else:
                    self.lbl_resolution.setText("Unknown")
                    self.img_view.setText("Cannot preview archive")

        # ----------------------------------------------------
        # 4. WEBP (CAN BE ANIMATED OR STATIC)
        # ----------------------------------------------------
        elif ext == '.webp':
            is_animated = False
            try:
                with Image.open(path) as im:
                    is_animated = getattr(im, 'is_animated', False) and getattr(im, 'n_frames', 1) > 1
            except Exception:
                pass

            if is_animated:
                self.current_media_type = 'ANIMATION'
                self.lbl_filename.setText(f"🎞️ {filename}")
                self.display_stack.setCurrentIndex(0)
                self.media_bar.setVisible(True)
                self.volume_container.setVisible(False)

                movie = QMovie(path)
                movie.setCacheMode(QMovie.CacheMode.CacheAll)
                if movie.isValid():
                    self.current_movie = movie
                    movie.start()
                    first_pix = movie.currentPixmap()
                    if not first_pix.isNull():
                        self.img_view.set_image(first_pix)
                        fc = movie.frameCount()
                        fc_str = f" • {fc} frames" if fc > 0 else ""
                        self.lbl_resolution.setText(f"{first_pix.width()} × {first_pix.height()}{fc_str}")
                    movie.frameChanged.connect(self.on_movie_frame_changed)
                    total = movie.frameCount()
                    if total > 0:
                        self.slider_timeline.setRange(0, total - 1)
                        self.slider_timeline.setValue(0)
                        self.lbl_time.setText(f"Frame 1 / {total}")
                    else:
                        self.slider_timeline.setRange(0, 100)
                        self.lbl_time.setText("Frame 1")
                    self.current_movie.setSpeed(int(self.SPEEDS[self.speed_idx] * 100))
                else:
                    pix = QPixmap(path)
                    self.img_view.set_image(pix)
                    self.media_bar.setVisible(False)
            else:
                self.current_media_type = 'IMAGE'
                self.lbl_filename.setText(f"🖼️ {filename}")
                self.display_stack.setCurrentIndex(0)
                self.media_bar.setVisible(False)
                pix = QPixmap(path)
                if pix and not pix.isNull():
                    self.lbl_resolution.setText(f"{pix.width()} × {pix.height()}")
                    self.img_view.set_image(pix)
                else:
                    self.img_view.setText("Cannot preview file")

        # ----------------------------------------------------
        # 5. STATIC IMAGES (PNG, JPG, JPEG, BMP, ICO)
        # ----------------------------------------------------
        else:
            self.current_media_type = 'IMAGE'
            self.lbl_filename.setText(f"🖼️ {filename}")
            self.display_stack.setCurrentIndex(0)
            self.media_bar.setVisible(False)

            pix = QPixmap(path)
            if pix and not pix.isNull():
                self.lbl_resolution.setText(f"{pix.width()} × {pix.height()}")
                self.img_view.set_image(pix)
            else:
                pil_img, _ = load_media_thumbnail(path)
                if pil_img:
                    from io import BytesIO
                    bio = BytesIO()
                    pil_img.save(bio, "JPEG")
                    pix = QPixmap.fromImage(QImage.fromData(bio.getvalue()))
                    self.lbl_resolution.setText(f"{pil_img.width} × {pil_img.height}")
                    self.img_view.set_image(pix)
                else:
                    self.lbl_resolution.setText("Unknown Resolution")
                    self.img_view.setText("Cannot preview file")

        # Update File Size
        try:
            sz = os.path.getsize(path)
            self.lbl_filesize.setText(f"File Size: <b>{format_size(sz)}</b>")
        except Exception:
            self.lbl_filesize.setText("File Size: Unknown")

        # Load AI Tags & Rating
        self.load_metadata(path)

    def load_ugoira_frames(self, zip_path):
        """Extracts and parses animation frames and delay metadata from Pixiv Ugoira archives."""
        import zipfile
        import json
        try:
            with zipfile.ZipFile(zip_path, 'r') as z:
                names = z.namelist()
                frame_delays = {}
                for meta_name in ['animation.json', 'ugoira.json']:
                    if meta_name in names:
                        try:
                            meta = json.loads(z.read(meta_name).decode('utf-8'))
                            for item in meta.get('frames', []):
                                if 'file' in item:
                                    frame_delays[item['file']] = item.get('delay', 100)
                        except Exception:
                            pass
                        break

                img_names = sorted([
                    f for f in names
                    if os.path.splitext(f)[1].lower() in EXT_IMG
                    and not os.path.basename(f).startswith('.')
                    and not f.startswith('__MACOSX')
                ])
                if not img_names:
                    return []

                frames = []
                for f in img_names:
                    data = z.read(f)
                    qimg = QImage.fromData(data)
                    if not qimg.isNull():
                        pix = QPixmap.fromImage(qimg)
                        delay = frame_delays.get(f, frame_delays.get(os.path.basename(f), 100))
                        frames.append((pix, max(15, delay)))
                return frames
        except Exception as e:
            logging.error(f"Error loading ugoira {zip_path}: {e}")
            return []

    def start_ugoira_timer(self, delay_ms):
        """Starts frame-by-frame timer for Pixiv Ugoira animation."""
        if not self.ugoira_frames:
            return
        if not self.ugoira_timer:
            self.ugoira_timer = QTimer(self)
            self.ugoira_timer.setSingleShot(True)
            self.ugoira_timer.timeout.connect(self.on_ugoira_tick)
        speed = self.SPEEDS[self.speed_idx]
        interval = max(10, int(delay_ms / speed))
        self.ugoira_timer.start(interval)

    def on_ugoira_tick(self):
        """Handles frame advancement for Ugoira animation."""
        if not self.ugoira_frames or self.current_media_type != 'ANIMATION':
            return
        next_idx = self.ugoira_idx + 1
        if next_idx >= len(self.ugoira_frames):
            if not self.is_looping:
                self.btn_play_pause.setText("▶")
                return
            next_idx = 0
        self.ugoira_idx = next_idx
        pix, delay = self.ugoira_frames[self.ugoira_idx]
        self.img_view.update_frame_pixmap(pix)
        if not self.slider_timeline.isSliderDown():
            self.slider_timeline.blockSignals(True)
            self.slider_timeline.setValue(self.ugoira_idx)
            self.slider_timeline.blockSignals(False)
        self.lbl_time.setText(f"Frame {self.ugoira_idx + 1} / {len(self.ugoira_frames)}")
        self.start_ugoira_timer(delay)

    def on_movie_frame_changed(self, frame_num):
        """Called when QMovie advances a frame in GIF or animated WebP."""
        if not self.current_movie or self.current_media_type != 'ANIMATION':
            return
        pix = self.current_movie.currentPixmap()
        if pix and not pix.isNull():
            self.img_view.update_frame_pixmap(pix)
        total = self.current_movie.frameCount()
        if total > 0:
            if not self.slider_timeline.isSliderDown():
                self.slider_timeline.blockSignals(True)
                self.slider_timeline.setValue(frame_num)
                self.slider_timeline.blockSignals(False)
            self.lbl_time.setText(f"Frame {frame_num + 1} / {total}")
            if not self.is_looping and frame_num >= total - 1:
                self.current_movie.setPaused(True)
                self.btn_play_pause.setText("▶")
        else:
            self.lbl_time.setText(f"Frame {frame_num + 1}")

    def on_player_position_changed(self, pos_ms):
        """Updates timeline slider and time label during video playback."""
        if self.current_media_type != 'VIDEO':
            return
        if not self.slider_timeline.isSliderDown():
            self.slider_timeline.blockSignals(True)
            self.slider_timeline.setValue(pos_ms)
            self.slider_timeline.blockSignals(False)
        dur_ms = self.media_player.duration()
        self.lbl_time.setText(f"{format_time_ms(pos_ms)} / {format_time_ms(dur_ms)}")

    def on_player_duration_changed(self, dur_ms):
        """Sets timeline slider range and updates duration."""
        if self.current_media_type != 'VIDEO':
            return
        if dur_ms > 0:
            self.slider_timeline.setRange(0, dur_ms)
            pos_ms = self.media_player.position()
            self.lbl_time.setText(f"{format_time_ms(pos_ms)} / {format_time_ms(dur_ms)}")
            if self.current_video_res:
                self.lbl_resolution.setText(f"{self.current_video_res} • {format_time_ms(dur_ms)}")

    def on_player_playback_state_changed(self, state):
        """Updates play/pause button icon."""
        if self.current_media_type != 'VIDEO':
            return
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self.btn_play_pause.setText("⏸")
        else:
            self.btn_play_pause.setText("▶")

    def on_player_media_status_changed(self, status):
        """Handles loop repeat when video reaches the end."""
        if self.current_media_type != 'VIDEO':
            return
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            if self.is_looping:
                self.media_player.setPosition(0)
                self.media_player.play()
            else:
                self.btn_play_pause.setText("▶")

    def on_player_error(self, error, error_string):
        """Catches video decode errors and falls back to OpenCV player."""
        logging.warning(f"QMediaPlayer error: {error_string} ({error}), attempting OpenCV fallback...")
        if self.file_paths and self.current_idx < len(self.file_paths):
            self.start_cv2_video_playback(self.file_paths[self.current_idx])

    def start_cv2_video_playback(self, path):
        """Robust fallback video player using OpenCV decoding directly onto canvas."""
        if not cv2:
            return
        try:
            self.stop_all_playback()
            self.cv2_cap = cv2.VideoCapture(path)
            if not self.cv2_cap.isOpened():
                return
            fps = self.cv2_cap.get(cv2.CAP_PROP_FPS) or 30.0
            if fps <= 0 or fps > 240:
                fps = 30.0
            self.cv2_fps = fps
            self.cv2_total_frames = int(self.cv2_cap.get(cv2.CAP_PROP_FRAME_COUNT))
            self.cv2_curr_frame = 0

            self.current_media_type = 'VIDEO'
            self.display_stack.setCurrentIndex(0)  # Use ZoomableImageLabel for cv2 frames
            self.media_bar.setVisible(True)
            self.volume_container.setVisible(False)

            ret, frame = self.cv2_cap.read()
            if ret:
                h, w, ch = frame.shape
                bytes_per_line = ch * w
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                qimg = QImage(rgb.data, w, h, bytes_per_line, QImage.Format.Format_RGB888)
                pix = QPixmap.fromImage(qimg.copy())
                self.img_view.set_image(pix)
                self.lbl_resolution.setText(f"{w} × {h} • {fps:.1f} fps (CV2)")

            if self.cv2_total_frames > 0:
                self.slider_timeline.setRange(0, self.cv2_total_frames - 1)
                self.slider_timeline.setValue(0)

            interval_ms = max(10, int(1000 / (self.cv2_fps * self.SPEEDS[self.speed_idx])))
            self.cv2_timer = QTimer(self)
            self.cv2_timer.timeout.connect(self.on_cv2_tick)
            self.cv2_timer.start(interval_ms)
            self.btn_play_pause.setText("⏸")
        except Exception as e:
            logging.error(f"Error in cv2 fallback: {e}")

    def on_cv2_tick(self):
        """Advances video frame using OpenCV."""
        if not self.cv2_cap or not self.cv2_cap.isOpened():
            return
        ret, frame = self.cv2_cap.read()
        if not ret:
            if self.is_looping:
                self.cv2_cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                self.cv2_curr_frame = 0
                ret, frame = self.cv2_cap.read()
                if not ret:
                    self.cv2_timer.stop()
                    return
            else:
                self.cv2_timer.stop()
                self.btn_play_pause.setText("▶")
                return

        self.cv2_curr_frame += 1
        h, w, ch = frame.shape
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        qimg = QImage(rgb.data, w, h, ch * w, QImage.Format.Format_RGB888)
        pix = QPixmap.fromImage(qimg.copy())
        self.img_view.update_frame_pixmap(pix)
        if not self.slider_timeline.isSliderDown():
            self.slider_timeline.setValue(self.cv2_curr_frame)
        dur_sec = (self.cv2_total_frames / self.cv2_fps) if self.cv2_fps > 0 else 0
        curr_sec = (self.cv2_curr_frame / self.cv2_fps) if self.cv2_fps > 0 else 0
        self.lbl_time.setText(f"{format_time_ms(int(curr_sec * 1000))} / {format_time_ms(int(dur_sec * 1000))}")

    def on_timeline_slider_moved(self, val):
        """Displays target position/frame in time label while dragging timeline."""
        if self.current_media_type == 'VIDEO':
            if hasattr(self, 'cv2_timer') and self.cv2_cap:
                dur_sec = (self.cv2_total_frames / self.cv2_fps) if self.cv2_fps > 0 else 0
                curr_sec = (val / self.cv2_fps) if self.cv2_fps > 0 else 0
                self.lbl_time.setText(f"{format_time_ms(int(curr_sec * 1000))} / {format_time_ms(int(dur_sec * 1000))}")
            else:
                dur_ms = self.media_player.duration()
                self.lbl_time.setText(f"{format_time_ms(val)} / {format_time_ms(dur_ms)}")
        elif self.current_media_type == 'ANIMATION':
            if self.current_movie:
                total = self.current_movie.frameCount()
                self.lbl_time.setText(f"Frame {val + 1} / {total}" if total > 0 else f"Frame {val + 1}")
            elif self.ugoira_frames:
                self.lbl_time.setText(f"Frame {val + 1} / {len(self.ugoira_frames)}")

    def on_timeline_slider_released(self):
        """Seeks to the selected position/frame when user finishes dragging timeline."""
        val = self.slider_timeline.value()
        if self.current_media_type == 'VIDEO':
            if hasattr(self, 'cv2_timer') and self.cv2_cap:
                self.cv2_cap.set(cv2.CAP_PROP_POS_FRAMES, val)
                self.cv2_curr_frame = val
                ret, frame = self.cv2_cap.read()
                if ret:
                    h, w, ch = frame.shape
                    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    qimg = QImage(rgb.data, w, h, ch * w, QImage.Format.Format_RGB888)
                    self.img_view.update_frame_pixmap(QPixmap.fromImage(qimg.copy()))
            else:
                self.media_player.setPosition(val)
        elif self.current_media_type == 'ANIMATION':
            if self.current_movie:
                self.current_movie.jumpToFrame(val)
                pix = self.current_movie.currentPixmap()
                if not pix.isNull():
                    self.img_view.update_frame_pixmap(pix)
            elif self.ugoira_frames:
                self.ugoira_idx = max(0, min(val, len(self.ugoira_frames) - 1))
                self.img_view.update_frame_pixmap(self.ugoira_frames[self.ugoira_idx][0])

    def toggle_play_pause(self):
        """Toggles play and pause across video, GIF, WebP, and Ugoira."""
        if self.current_media_type == 'VIDEO':
            if hasattr(self, 'cv2_timer') and self.cv2_timer:
                if self.cv2_timer.isActive():
                    self.cv2_timer.stop()
                    self.btn_play_pause.setText("▶")
                else:
                    interval_ms = max(10, int(1000 / (self.cv2_fps * self.SPEEDS[self.speed_idx])))
                    self.cv2_timer.start(interval_ms)
                    self.btn_play_pause.setText("⏸")
            elif self.media_player:
                if self.media_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                    self.media_player.pause()
                else:
                    if self.media_player.position() >= self.media_player.duration() and self.media_player.duration() > 0:
                        self.media_player.setPosition(0)
                    self.media_player.play()
        elif self.current_media_type == 'ANIMATION':
            if self.current_movie:
                if self.current_movie.state() == QMovie.MovieState.Running:
                    self.current_movie.setPaused(True)
                    self.btn_play_pause.setText("▶")
                else:
                    if self.current_movie.currentFrameNumber() >= self.current_movie.frameCount() - 1 and self.current_movie.frameCount() > 1:
                        self.current_movie.jumpToFrame(0)
                    self.current_movie.setPaused(False)
                    self.btn_play_pause.setText("⏸")
            elif self.ugoira_timer:
                if self.ugoira_timer.isActive():
                    self.ugoira_timer.stop()
                    self.btn_play_pause.setText("▶")
                else:
                    delay = self.ugoira_frames[self.ugoira_idx][1] if self.ugoira_frames else 100
                    self.start_ugoira_timer(delay)
                    self.btn_play_pause.setText("⏸")

    def on_space_pressed(self):
        """Spacebar pauses/plays animations and videos, or moves to next image if static."""
        if self.current_media_type in ('VIDEO', 'ANIMATION'):
            self.toggle_play_pause()
        else:
            self.show_next_image()

    def toggle_loop(self):
        """Toggles repeat/loop mode for media."""
        self.is_looping = not self.is_looping
        self.btn_loop.setChecked(self.is_looping)
        if self.is_looping:
            self.btn_loop.setStyleSheet("background-color: #2563eb; color: #ffffff; border-color: #3b82f6; font-size: 11px; padding: 4px 8px;")
        else:
            self.btn_loop.setStyleSheet("background-color: #1f1f2b; color: #9ca3af; border-color: #323244; font-size: 11px; padding: 4px 8px;")

        if self.current_media_type == 'VIDEO' and self.media_player:
            self.media_player.setLoops(QMediaPlayer.Loops.Infinite if self.is_looping else 1)

    def cycle_speed(self):
        """Cycles playback speed (0.5x, 1.0x, 1.5x, 2.0x)."""
        self.speed_idx = (self.speed_idx + 1) % len(self.SPEEDS)
        speed = self.SPEEDS[self.speed_idx]
        self.btn_speed.setText(f"{speed}x")

        if self.current_media_type == 'VIDEO':
            if hasattr(self, 'media_player') and self.media_player:
                self.media_player.setPlaybackRate(speed)
            elif hasattr(self, 'cv2_timer') and self.cv2_timer and self.cv2_timer.isActive():
                interval_ms = max(10, int(1000 / (self.cv2_fps * speed)))
                self.cv2_timer.setInterval(interval_ms)
        elif self.current_media_type == 'ANIMATION':
            if self.current_movie:
                self.current_movie.setSpeed(int(speed * 100))
            elif self.ugoira_timer and self.ugoira_timer.isActive():
                delay = self.ugoira_frames[self.ugoira_idx][1] if self.ugoira_frames else 100
                self.ugoira_timer.setInterval(max(10, int(delay / speed)))

    def toggle_mute(self):
        """Toggles audio mute."""
        if hasattr(self, 'audio_output') and self.audio_output:
            new_muted = not self.audio_output.isMuted()
            self.audio_output.setMuted(new_muted)
            self.update_volume_ui()

    def volume_up(self):
        """Increases volume by 5%."""
        if self.current_media_type == 'VIDEO':
            val = min(100, self.slider_volume.value() + 5)
            self.slider_volume.setValue(val)

    def volume_down(self):
        """Decreases volume by 5%."""
        if self.current_media_type == 'VIDEO':
            val = max(0, self.slider_volume.value() - 5)
            self.slider_volume.setValue(val)

    def on_volume_slider_changed(self, val):
        """Adjusts audio output volume."""
        if hasattr(self, 'audio_output') and self.audio_output:
            if self.audio_output.isMuted() and val > 0:
                self.audio_output.setMuted(False)
            self.audio_output.setVolume(val / 100.0)
            self.update_volume_ui()

    def update_volume_ui(self):
        """Reflects current audio volume and mute state in UI buttons."""
        if not hasattr(self, 'audio_output') or not self.audio_output:
            return
        is_muted = self.audio_output.isMuted()
        vol = int(self.audio_output.volume() * 100)
        if is_muted or vol == 0:
            self.btn_mute.setText("🔇")
            self.btn_mute.setToolTip("Unmute (M)")
        elif vol < 50:
            self.btn_mute.setText("🔉")
            self.btn_mute.setToolTip("Mute (M)")
        else:
            self.btn_mute.setText("🔊")
            self.btn_mute.setToolTip("Mute (M)")

    def load_metadata(self, path):
        """Fetches AI tags, character, and series metadata from SQLite database."""
        tags_info = get_file_tags(DB_FILE, path)
        if not tags_info or not tags_info.get('all'):
            self.lbl_rating_badge.setText("⚪ NOT SCANNED")
            self.lbl_rating_badge.setStyleSheet("background-color: #1f2937; color: #9ca3af; border-radius: 6px; padding: 6px; font-weight: bold;")
            self.lbl_chars.setText("<i>No AI tags found for this file.</i>")
            self.lbl_series.setText("<i>None</i>")
            self.lbl_tags.setText("<i>None</i>")
            self.current_tags = []
            return

        # Rating
        rating = tags_info.get('rating', 'unknown')
        if rating == 'general':
            self.lbl_rating_badge.setText("🟢 SFW / GENERAL (ปลอดภัย)")
            self.lbl_rating_badge.setStyleSheet("background-color: #064e3b; color: #34d399; border-radius: 6px; padding: 6px; font-weight: bold;")
        elif rating in ('sensitive', 'ecchi'):
            self.lbl_rating_badge.setText("⚠️ SENSITIVE / ECCHI (ล่อแหลม)")
            self.lbl_rating_badge.setStyleSheet("background-color: #78350f; color: #fbbf24; border-radius: 6px; padding: 6px; font-weight: bold;")
        elif rating in ('questionable', 'explicit', 'r18'):
            self.lbl_rating_badge.setText("🔞 NSFW / EXPLICIT (18+)")
            self.lbl_rating_badge.setStyleSheet("background-color: #7f1d1d; color: #f87171; border-radius: 6px; padding: 6px; font-weight: bold;")
        else:
            self.lbl_rating_badge.setText(f"⚪ RATING: {rating.upper()}")
            self.lbl_rating_badge.setStyleSheet("background-color: #1f2937; color: #9ca3af; border-radius: 6px; padding: 6px; font-weight: bold;")

        # Characters
        chars = tags_info.get('characters', [])
        if chars:
            c_lines = [f"• <b>{c[0].replace('_', ' ').title()}</b> ({int(c[1]*100)}%)" for c in chars]
            self.lbl_chars.setText("<br>".join(c_lines))
        else:
            self.lbl_chars.setText("<i>None detected</i>")

        # Series
        series = tags_info.get('series', [])
        if series:
            s_lines = [f"• <b>{s[0].replace('_', ' ').title()}</b> ({int(s[1]*100)}%)" for s in series]
            self.lbl_series.setText("<br>".join(s_lines))
        else:
            self.lbl_series.setText("<i>None detected</i>")

        # General Tags
        gen = tags_info.get('general', [])
        if gen:
            gen_names = [f"{g[0]} ({int(g[1]*100)}%)" for g in gen[:40]]
            self.lbl_tags.setText(", ".join(gen_names))
        else:
            self.lbl_tags.setText("<i>None</i>")

        self.current_tags = [t[0] for t in tags_info.get('all', []) if t[0] != '__none__']

    def show_prev_image(self):
        """Navigates to previous media item."""
        if self.current_idx > 0:
            self.stop_all_playback()
            self.current_idx -= 1
            self.update_current_display()

    def show_next_image(self):
        """Navigates to next media item."""
        if self.current_idx < len(self.file_paths) - 1:
            self.stop_all_playback()
            self.current_idx += 1
            self.update_current_display()

    def toggle_sidebar(self):
        """Shows/hides AI tag sidebar."""
        self.sidebar_frame.setVisible(not self.sidebar_frame.isVisible())

    def on_scale_mode_selected(self, index):
        """Handles view mode combobox change."""
        if index < 0 or index >= self.combo_scale.count():
            return
        mode = self.combo_scale.itemData(index)
        if mode in ('FIT', 'FILL', 'ACTUAL'):
            if self.combo_scale.count() > 3:
                self.combo_scale.blockSignals(True)
                self.combo_scale.removeItem(3)
                self.combo_scale.blockSignals(False)
            self.set_scale_mode(mode)

    def set_scale_mode(self, mode: str):
        """Sets scale mode for both image canvas and video widget."""
        self.img_view.set_view_mode(mode)
        if hasattr(self, 'video_widget') and self.video_widget:
            if mode == 'FILL':
                self.video_widget.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatioByExpanding)
            else:
                self.video_widget.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)

    def on_zoom_changed(self, zoom_factor, view_mode='CUSTOM'):
        """Synchronizes combo scale dropdown with zoom level."""
        self.combo_scale.blockSignals(True)
        if view_mode == 'FIT':
            if self.combo_scale.count() > 3:
                self.combo_scale.removeItem(3)
            self.combo_scale.setCurrentIndex(0)
        elif view_mode == 'FILL':
            if self.combo_scale.count() > 3:
                self.combo_scale.removeItem(3)
            self.combo_scale.setCurrentIndex(1)
        elif view_mode == 'ACTUAL':
            if self.combo_scale.count() > 3:
                self.combo_scale.removeItem(3)
            self.combo_scale.setCurrentIndex(2)
        else:
            pct = int(zoom_factor * 100)
            custom_label = f"🔍 Zoom: {pct}%"
            if self.combo_scale.count() > 3:
                self.combo_scale.setItemText(3, custom_label)
            else:
                self.combo_scale.addItem(custom_label, "CUSTOM")
            self.combo_scale.setCurrentIndex(3)
        self.combo_scale.blockSignals(False)

    def toggle_zoom(self):
        """Toggles between FIT, FILL, and ACTUAL view modes."""
        current = self.img_view.view_mode
        if current == 'FIT':
            self.set_scale_mode('FILL')
        elif current == 'FILL':
            self.set_scale_mode('ACTUAL')
        else:
            self.set_scale_mode('FIT')

    def toggle_fullscreen(self):
        """Toggles window fullscreen mode."""
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def copy_tags(self):
        """Copies all AI tags to system clipboard."""
        if hasattr(self, 'current_tags') and self.current_tags:
            tag_str = ", ".join(self.current_tags)
            QApplication.clipboard().setText(tag_str)
            self.btn_copy_tags.setText("✅ Copied!")
            QTimer.singleShot(1500, lambda: self.btn_copy_tags.setText("📋 Copy All Tags"))

    def open_explorer(self):
        """Selects current file in Windows File Explorer or native file manager."""
        if self.file_paths and 0 <= self.current_idx < len(self.file_paths):
            p = self.file_paths[self.current_idx]
            if show_in_file_manager(p):
                if hasattr(self, 'btn_explorer') and self.btn_explorer:
                    self.btn_explorer.setText("📂 Opening...")
                    QTimer.singleShot(1200, lambda: self.btn_explorer.setText("📂 Open in Explorer"))

    def closeEvent(self, event):
        """Ensures all video, audio, and animations stop when window is closed."""
        self.stop_all_playback()
        super().closeEvent(event)

    def accept(self):
        """Ensures playback stops on dialog accept."""
        self.stop_all_playback()
        super().accept()

    def reject(self):
        """Ensures playback stops on dialog reject."""
        self.stop_all_playback()
        super().reject()
