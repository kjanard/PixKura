import os
import zipfile
try:
    import cv2
except ImportError:
    cv2 = None
from PIL import Image, PngImagePlugin, ImageFile
PngImagePlugin.MAX_TEXT_CHUNK = 104857600  # 100MB to allow large metadata chunks in AI-generated PNGs
ImageFile.LOAD_TRUNCATED_IMAGES = True
from PyQt6.QtGui import QPainter, QColor, QPen, QBrush, QPixmap, QRegion, QPainterPath, QImage, QFont
from PyQt6.QtCore import Qt, QRect
from config import EXT_IMG, EXT_GIF, EXT_VID, EXT_ZIP

def add_indicator(pixmap, file_type):
    if not file_type: return pixmap
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    if file_type == 'VIDEO': color, label = QColor(255, 50, 50), "▶"
    elif file_type in ['GIF', 'UGOIRA']: color, label = QColor(50, 100, 255), ("GIF" if file_type=='GIF' else "ZIP")
    elif file_type == 'ERROR': color, label = QColor(255, 0, 0), "ERR"
    else: painter.end(); return pixmap
    
    pen = QPen(color); pen.setWidth(6); painter.setPen(pen)
    painter.drawRect(0, 0, pixmap.width(), pixmap.height())
    painter.setBrush(color); painter.setPen(Qt.PenStyle.NoPen); painter.drawRect(0, 0, 60, 30)
    painter.setPen(QColor(255, 255, 255)); painter.setFont(QFont("Arial", 14, QFont.Weight.Bold))
    painter.drawText(0, 0, 60, 30, Qt.AlignmentFlag.AlignCenter, label)
    painter.end()
    return pixmap

def load_media_thumbnail(path):
    if not os.path.exists(path):
        return None, None
    try:
        ext = os.path.splitext(path)[1].lower()
        if ext in EXT_VID:
            if cv2:
                cap = cv2.VideoCapture(path); ret, frame = cap.read(); cap.release()
                if ret: return Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)), 'VIDEO'
            return Image.new('RGB', (240, 240), (0,0,0)), 'VIDEO'
        elif ext in EXT_ZIP:
            with zipfile.ZipFile(path, 'r') as z:
                l = sorted([f for f in z.namelist() if f.lower().endswith(tuple(EXT_IMG))])
                if l:
                    with z.open(l[0]) as zf:
                        with Image.open(zf) as raw_img:
                            return raw_img.copy(), 'UGOIRA'
        elif ext in EXT_GIF: 
            with Image.open(path) as raw_img:
                return raw_img.convert('RGB'), 'GIF'
        elif ext in EXT_IMG: 
            with Image.open(path) as raw_img:
                return raw_img.copy(), None
    except Exception as e:
        import logging
        logging.error(f"Error loading media {path}: {e}")
        # Return a red placeholder indicating corrupt/unreadable file
        err_img = Image.new('RGB', (240, 240), (80, 20, 20))
        return err_img, 'ERROR'
    return None, None

def format_size(size_bytes):
    if size_bytes == 0: return "0B"
    size_name = ("B", "KB", "MB", "GB")
    i = 0
    while size_bytes >= 1024 and i < len(size_name) - 1:
        size_bytes /= 1024.0
        i += 1
    return f"{size_bytes:.1f} {size_name[i]}"

def overlay_avatar_on_grid(grid_qimg, avatar_blob):
    """
    แปะรูป Avatar (วงกลม) ลงมุมซ้ายบนของ Grid Thumbnail
    """
    if not avatar_blob or grid_qimg.isNull():
        return grid_qimg

    # 1. แปลง Blob เป็น QImage
    avatar_img = QImage.fromData(avatar_blob)
    if avatar_img.isNull(): return grid_qimg

    # 2. เตรียม Canvas (Copy Grid มาเป็นฐาน)
    result = grid_qimg.copy()
    painter = QPainter(result)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    # 3. กำหนดขนาดและตำแหน่ง
    # Grid ขนาดปกติคือ 240x240
    # เราจะให้ Avatar ขนาด 60x60 (1/4 ของความกว้าง)
    icon_size = 60 
    margin = 5
    x, y = margin, margin

    # 4. วาดวงกลมพื้นหลังสีขาว (ทำขอบ)
    border_size = 2
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor("white")))
    painter.drawEllipse(x, y, icon_size, icon_size)

    # 5. วาดรูป Avatar ลงในวงกลม
    # สร้าง Path วงกลมสำหรับ Clip
    path = QPainterPath()
    path.addEllipse(x + border_size, y + border_size, icon_size - (border_size*2), icon_size - (border_size*2))
    painter.setClipPath(path)
    
    # วาดรูป (ย่อขยายให้พอดี)
    painter.drawImage(QRect(x + border_size, y + border_size, icon_size - (border_size*2), icon_size - (border_size*2)), avatar_img)

    painter.end()
    return result

_DRIVE_MEDIA_CACHE = {}
_DRIVE_CACHE_TIME = 0.0

def get_all_drive_media_types(force_refresh=False):
    """
    Detects physical disk media type (SSD vs HDD) for all mounted drive letters on Windows.
    Returns dict mapping drive letter (uppercase) to media type string: 'SSD', 'HDD', or 'UNKNOWN'.
    """
    global _DRIVE_MEDIA_CACHE, _DRIVE_CACHE_TIME
    import time
    if not force_refresh and _DRIVE_MEDIA_CACHE and (time.time() - _DRIVE_CACHE_TIME < 300):
        return _DRIVE_MEDIA_CACHE

    drive_map = {}
    if os.name == 'nt':
        import subprocess, json
        try:
            cmd_parts = 'Get-Partition | Where-Object DriveLetter | Select-Object DriveLetter, DiskNumber | ConvertTo-Json'
            p_out = subprocess.check_output(['powershell', '-NoProfile', '-Command', cmd_parts], text=True, timeout=5)
            parts = json.loads(p_out)

            cmd_disks = 'Get-PhysicalDisk | Select-Object DeviceId, MediaType | ConvertTo-Json'
            d_out = subprocess.check_output(['powershell', '-NoProfile', '-Command', cmd_disks], text=True, timeout=5)
            disks = json.loads(d_out)

            disk_map = {str(d['DeviceId']): str(d.get('MediaType', '')).upper() for d in (disks if isinstance(disks, list) else [disks])}
            for p in (parts if isinstance(parts, list) else [parts]):
                if not p or not p.get('DriveLetter'): continue
                dl = str(p['DriveLetter']).upper()
                dn = str(p.get('DiskNumber', ''))
                media_str = disk_map.get(dn, 'UNKNOWN')
                if 'SSD' in media_str:
                    drive_map[dl] = 'SSD'
                elif 'HDD' in media_str:
                    drive_map[dl] = 'HDD'
                else:
                    drive_map[dl] = 'UNKNOWN'
        except Exception:
            pass

    _DRIVE_MEDIA_CACHE = drive_map
    _DRIVE_CACHE_TIME = time.time()
    return drive_map

def detect_drive_media_type(path):
    """
    Detects whether the given path resides on an SSD or HDD.
    Returns 'SSD', 'HDD', or 'UNKNOWN'.
    """
    if not path:
        return 'UNKNOWN'
    drive = os.path.splitdrive(os.path.abspath(path))[0].rstrip(':').upper()
    if not drive:
        return 'UNKNOWN'
    drive_map = get_all_drive_media_types()
    return drive_map.get(drive, 'UNKNOWN')