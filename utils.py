import os
import sys
import subprocess
import logging
import zipfile
try:
    import cv2
except ImportError:
    cv2 = None
from PIL import Image, PngImagePlugin, ImageFile, ImageDraw
PngImagePlugin.MAX_TEXT_CHUNK = 104857600  # 100MB to allow large metadata chunks in AI-generated PNGs
ImageFile.LOAD_TRUNCATED_IMAGES = True
from PyQt6.QtGui import QPainter, QColor, QPen, QBrush, QPixmap, QRegion, QPainterPath, QImage, QFont
from PyQt6.QtCore import Qt, QRect
from config import EXT_IMG, EXT_GIF, EXT_VID, EXT_ZIP


def show_in_file_manager(file_path: str) -> bool:
    """
    Opens native system file manager (Explorer on Windows, Finder on macOS, etc.)
    and highlights/selects the given file without blocking the GUI.
    Handles spaces, quotes, and non-existent files gracefully.
    """
    if not file_path:
        return False

    try:
        p = os.path.abspath(file_path)
        if not os.path.exists(p):
            p_dir = os.path.dirname(p)
            if os.path.exists(p_dir):
                p = p_dir
            else:
                logging.warning(f"File manager target path does not exist: {file_path}")
                return False

        if sys.platform == 'win32':
            norm_p = os.path.normpath(p)
            if os.path.isfile(norm_p):
                # Windows Explorer command syntax: explorer.exe /select,"<path>"
                # Quotes must surround ONLY the path, not the /select switch
                subprocess.Popen(f'explorer /select,"{norm_p}"')
            else:
                subprocess.Popen(f'explorer "{norm_p}"')
            return True
        elif sys.platform == 'darwin':
            if os.path.isfile(p):
                subprocess.Popen(['open', '-R', p])
            else:
                subprocess.Popen(['open', p])
            return True
        else:
            # Linux / Unix
            target = os.path.dirname(p) if os.path.isfile(p) else p
            subprocess.Popen(['xdg-open', target])
            return True
    except Exception as e:
        logging.error(f"Error opening file manager for {file_path}: {e}")
        return False


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


def make_thumbnail_rgb(pil_img, max_size=(240, 240)):
    """
    Downsamples a PIL Image for thumbnail generation and converts safely to RGB.
    If the image has an alpha channel or transparency (RGBA, LA, P with transparency),
    it intelligently decides between a clean White background (for dark line art/manga)
    or Dark background (for bright/white art), preventing transparent PNGs from turning pitch black.
    """
    if not pil_img:
        return None

    has_alpha = pil_img.mode in ('RGBA', 'LA') or (pil_img.mode == 'P' and 'transparency' in getattr(pil_img, 'info', {}))

    if has_alpha:
        rgba = pil_img.convert('RGBA')
        rgba.thumbnail(max_size, Image.Resampling.LANCZOS)
        tw, th = rgba.size

        # Quick 16x16 sample to measure luminance of non-transparent content
        bg_color = (255, 255, 255)
        try:
            sample = rgba.resize((16, 16), Image.Resampling.NEAREST)
            pixels = sample.load()
            total_lum = 0
            vis_count = 0
            for y in range(16):
                for x in range(16):
                    r, g, b, a = pixels[x, y]
                    if a > 30:
                        total_lum += 0.299 * r + 0.587 * g + 0.114 * b
                        vis_count += 1
            if vis_count > 0 and (total_lum / vis_count) > 140:
                bg_color = (24, 24, 30)  # Dark background for bright/white line art
        except Exception:
            bg_color = (255, 255, 255)

        bg = Image.new('RGB', (tw, th), bg_color)
        bg.paste(rgba, mask=rgba.split()[3])
        return bg
    else:
        rgb = pil_img if pil_img.mode == 'RGB' else pil_img.convert('RGB')
        rgb.thumbnail(max_size, Image.Resampling.LANCZOS)
        return rgb


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

def get_drive_media_type_win32(drive_letter):
    """
    Fast Win32 API check using DeviceIoControl (StorageDeviceSeekPenaltyProperty).
    Takes < 0.1 ms and requires no administrative privileges or subprocesses.
    Returns 'SSD', 'HDD', or 'UNKNOWN'.
    """
    if os.name != 'nt' or not drive_letter:
        return 'UNKNOWN'
    import ctypes, ctypes.wintypes
    try:
        clean_drive = drive_letter.rstrip('\\/:')
        if not clean_drive:
            return 'UNKNOWN'
        volume_path = r'\\.\\' + clean_drive + ':'
        handle = ctypes.windll.kernel32.CreateFileW(
            volume_path,
            0,      # Desired access: 0 (metadata query only, no read/write rights needed)
            1 | 2,  # FILE_SHARE_READ | FILE_SHARE_WRITE
            None,
            3,      # OPEN_EXISTING
            0,
            None
        )
        if handle in (-1, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF):
            return 'UNKNOWN'

        class STORAGE_PROPERTY_QUERY(ctypes.Structure):
            _fields_ = [
                ('PropertyId', ctypes.c_int),
                ('QueryType', ctypes.c_int),
                ('AdditionalParameters', ctypes.c_byte * 1)
            ]

        class DEVICE_SEEK_PENALTY_DESCRIPTOR(ctypes.Structure):
            _fields_ = [
                ('Version', ctypes.wintypes.DWORD),
                ('Size', ctypes.wintypes.DWORD),
                ('IncursSeekPenalty', ctypes.c_byte)
            ]

        query = STORAGE_PROPERTY_QUERY()
        query.PropertyId = 7  # StorageDeviceSeekPenaltyProperty
        query.QueryType = 0   # PropertyStandardQuery
        desc = DEVICE_SEEK_PENALTY_DESCRIPTOR()
        bytes_returned = ctypes.wintypes.DWORD()

        res = ctypes.windll.kernel32.DeviceIoControl(
            handle,
            0x002D1400,  # IOCTL_STORAGE_QUERY_PROPERTY
            ctypes.byref(query),
            ctypes.sizeof(query),
            ctypes.byref(desc),
            ctypes.sizeof(desc),
            ctypes.byref(bytes_returned),
            None
        )
        ctypes.windll.kernel32.CloseHandle(handle)
        if res:
            return 'HDD' if desc.IncursSeekPenalty else 'SSD'
    except Exception:
        pass
    return 'UNKNOWN'

def get_all_drive_media_types(force_refresh=False):
    """
    Detects physical disk media type (SSD vs HDD) for all mounted drive letters on Windows.
    Uses ultra-fast Win32 API (< 2 ms total) without spawning slow PowerShell processes.
    Returns dict mapping drive letter (uppercase) to media type string: 'SSD', 'HDD', or 'UNKNOWN'.
    """
    global _DRIVE_MEDIA_CACHE, _DRIVE_CACHE_TIME
    import time
    if not force_refresh and _DRIVE_MEDIA_CACHE and (time.time() - _DRIVE_CACHE_TIME < 300):
        return _DRIVE_MEDIA_CACHE

    drive_map = {}
    if os.name == 'nt':
        import string
        for d in string.ascii_uppercase:
            if os.path.exists(d + ':\\'):
                drive_map[d] = get_drive_media_type_win32(d)

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
    # 1. Ultra-fast Win32 direct check (< 0.1 ms)
    if os.name == 'nt':
        res = get_drive_media_type_win32(drive)
        if res in ('SSD', 'HDD'):
            return res

    # 2. Fallback to cached all-drives mapping
    drive_map = get_all_drive_media_types()
    return drive_map.get(drive, 'UNKNOWN')