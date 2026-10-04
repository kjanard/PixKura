DARK_STYLESHEET = """
/* Global styling */
QWidget {
    background-color: #121214;
    color: #e5e7eb;
    font-family: 'Segoe UI', 'Arial', sans-serif;
    font-size: 13px;
}

/* Headers and labels */
QLabel {
    color: #f3f4f6;
    font-weight: 500;
}

/* Text Input Fields */
QLineEdit {
    background-color: #1a1a1f;
    border: 1px solid #2d2d34;
    border-radius: 6px;
    padding: 6px 12px;
    color: #f3f4f6;
    selection-background-color: #3b82f6;
}

QLineEdit:focus {
    border: 1px solid #3b82f6;
    background-color: #1c1c24;
}

/* Buttons */
QPushButton {
    background-color: #1e1e24;
    border: 1px solid #2d2d34;
    border-radius: 6px;
    padding: 6px 16px;
    color: #f3f4f6;
    font-weight: 600;
}

QPushButton:hover {
    background-color: #25252e;
    border: 1px solid #3b82f6;
}

QPushButton:pressed {
    background-color: #3b82f6;
    color: #ffffff;
}

QPushButton:disabled {
    background-color: #121214;
    border: 1px solid #202024;
    color: #4b5563;
}

/* Specific buttons with accents */
QPushButton#btn_back {
    background-color: #ef4444;
    border: 1px solid #dc2626;
    color: #ffffff;
}

QPushButton#btn_back:hover {
    background-color: #f87171;
}

QPushButton#btn_update {
    background-color: #8b5cf6;
    border: 1px solid #7c3aed;
    color: #ffffff;
}

QPushButton#btn_update:hover {
    background-color: #a78bfa;
}

QPushButton#btn_ai_tag {
    background-color: #0d9488;
    border: 1px solid #14b8a6;
    color: #ffffff;
}

QPushButton#btn_ai_tag:hover {
    background-color: #14b8a6;
}

QPushButton#btn_dashboard {
    background-color: #6366f1;
    border: 1px solid #818cf8;
    color: #ffffff;
}

QPushButton#btn_dashboard:hover {
    background-color: #818cf8;
}


/* Radio Buttons */
QRadioButton {
    spacing: 8px;
    color: #9ca3af;
}

QRadioButton::indicator {
    width: 16px;
    height: 16px;
    border-radius: 8px;
    border: 2px solid #4b5563;
    background: transparent;
}

QRadioButton::indicator:checked {
    border: 2px solid #3b82f6;
    background: #3b82f6;
}

QRadioButton:hover {
    color: #f3f4f6;
}

/* ComboBox */
QComboBox {
    background-color: #1a1a1f;
    border: 1px solid #2d2d34;
    border-radius: 6px;
    padding: 5px 25px 5px 12px;
    color: #f3f4f6;
    min-width: 100px;
}

QComboBox:hover {
    border: 1px solid #3b82f6;
}

QComboBox:editable {
    background-color: #1a1a1f;
    color: #f3f4f6;
}

QComboBox QLineEdit {
    background: transparent;
    border: none;
    color: #f3f4f6;
    padding: 0px;
}

QComboBox QAbstractItemView {
    background-color: #16161f;
    border: 1px solid #3b82f6;
    border-radius: 6px;
    padding: 4px;
    outline: 0;
    selection-background-color: #3b82f6;
    selection-color: #ffffff;
}

QComboBox QAbstractItemView::item {
    color: #f3f4f6;
    padding: 6px 12px;
    min-height: 24px;
    border-radius: 4px;
}

QComboBox QAbstractItemView::item:hover {
    background-color: #252538;
}

QComboBox QAbstractItemView::item:selected {
    background-color: #3b82f6;
    color: #ffffff;
}

/* Specific ScrollBar inside ComboBox Popup */
QComboBox QAbstractItemView QScrollBar:vertical {
    background: transparent;
    width: 8px;
    margin: 4px 2px 4px 0px;
    border: none;
}

QComboBox QAbstractItemView QScrollBar::handle:vertical {
    background: #374151;
    min-height: 25px;
    border-radius: 4px;
}

QComboBox QAbstractItemView QScrollBar::handle:vertical:hover {
    background: #3b82f6;
}

QComboBox QAbstractItemView QScrollBar::add-line:vertical,
QComboBox QAbstractItemView QScrollBar::sub-line:vertical,
QComboBox QAbstractItemView QScrollBar::add-page:vertical,
QComboBox QAbstractItemView QScrollBar::sub-page:vertical {
    height: 0px;
    background: transparent;
    border: none;
}

QComboBox QAbstractItemView QScrollBar:horizontal {
    height: 0px;
    width: 0px;
    border: none;
}

/* List Widgets (Grid Items) */
QListWidget {
    background-color: #121214;
    border: 1px solid #1a1a1f;
    padding: 10px;
}

QListWidget::item {
    background-color: #1a1a1f;
    border-radius: 8px;
    color: #e5e7eb;
    padding: 10px;
    border: 1px solid #25252b;
}

QListWidget::item:hover {
    background-color: #202029;
    border: 1px solid #3b82f6;
}

QListWidget::item:selected {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #1e3a8a, stop:1 #3b82f6);
    color: #ffffff;
    border: 1px solid #60a5fa;
}

/* ScrollBar Styling */
QScrollBar:vertical {
    background: #121214;
    width: 8px;
    margin: 0px;
    border: none;
}

QScrollBar::handle:vertical {
    background: #2d2d34;
    min-height: 25px;
    border-radius: 4px;
}

QScrollBar::handle:vertical:hover {
    background: #3b82f6;
}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    height: 0px;
    background: transparent;
    border: none;
}

QScrollBar:horizontal {
    background: #121214;
    height: 8px;
    margin: 0px;
    border: none;
}

QScrollBar::handle:horizontal {
    background: #2d2d34;
    min-width: 25px;
    border-radius: 4px;
}

QScrollBar::handle:horizontal:hover {
    background: #3b82f6;
}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal,
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
    width: 0px;
    background: transparent;
    border: none;
}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0px;
}

/* Progress Bar */
QProgressBar {
    background-color: #1a1a1f;
    border: 1px solid #2d2d34;
    border-radius: 4px;
    text-align: center;
    color: #ffffff;
    font-weight: bold;
    height: 16px;
}

QProgressBar::chunk {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #06b6d4, stop:1 #3b82f6);
    border-radius: 3px;
}

/* Status Bar */
QStatusBar {
    background-color: #1a1a1f;
    border-top: 1px solid #2d2d34;
    color: #9ca3af;
}
"""
