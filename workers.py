import os
import shutil
import tempfile
import sqlite3
import logging
import time
import random
import io
import queue
import zipfile
import requests
import concurrent.futures
import numpy as np
from PIL import Image, ImageOps
try:
    import cv2 as _cv2  # Fast C++ decode — releases GIL, enables true thread parallelism
    import cv2.utils.logging as _cvlog
    _cvlog.setLogLevel(_cvlog.LOG_LEVEL_SILENT)
    _CV2_AVAILABLE = True
except Exception:
    _cv2 = None
    _CV2_AVAILABLE = False
from PyQt6.QtCore import QObject, QRunnable, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QImage, QColor
from pixivpy3 import AppPixivAPI

from config import (
    DB_FILE, ALL_MEDIA_EXT, EXT_IMG, EXT_GIF, EXT_VID, EXT_ZIP,
    DEFAULT_CHARACTER_THRESHOLD, DEFAULT_GENERAL_THRESHOLD,
    MAX_UGOIRA_FRAME_BYTES, MAX_UGOIRA_TOTAL_BYTES, MAX_UGOIRA_FRAMES,
    load_app_config
)
from utils import (
    load_media_thumbnail, make_thumbnail_rgb, overlay_avatar_on_grid,
    detect_drive_media_type, compute_dhash, hamming_distance_hex
)
from danbooru_api import DanbooruClient

import threading
_thread_local = threading.local()
_conn_lock = threading.Lock()
_all_thread_conns = set()
_shutting_down = False


def _safe_emit(signal, *args):
    """Safely emits a Qt signal, ignoring RuntimeError if the underlying QObject was deleted."""
    try:
        signal.emit(*args)
    except RuntimeError:
        pass


def get_thread_local_conn(db_file, timeout=30.0):
    if _shutting_down:
        raise sqlite3.OperationalError("Application is shutting down")
    need_new = (
        not hasattr(_thread_local, "conn")
        or _thread_local.conn is None
        or getattr(_thread_local, "db_file", None) != db_file
    )
    if not need_new:
        try:
            _thread_local.conn.rollback()
        except sqlite3.ProgrammingError:
            need_new = True
        except Exception:
            pass

    if need_new:
        if hasattr(_thread_local, "conn") and _thread_local.conn is not None:
            try:
                with _conn_lock:
                    _all_thread_conns.discard(_thread_local.conn)
                _thread_local.conn.close()
            except Exception:
                pass
        conn = sqlite3.connect(db_file, timeout=timeout, check_same_thread=False)
        try:
            conn.execute('PRAGMA journal_mode = WAL;')
            conn.execute('PRAGMA synchronous = NORMAL;')
            conn.execute('PRAGMA cache_size = -4000;')
            conn.execute('PRAGMA temp_store = MEMORY;')
            conn.execute('PRAGMA mmap_size = 134217728;')
        except Exception:
            pass
        _thread_local.conn = conn
        _thread_local.db_file = db_file
        with _conn_lock:
            _all_thread_conns.add(conn)
    return _thread_local.conn


def close_all_thread_local_conns(shutting_down=False):
    """
    Interrupts and closes all tracked thread-local SQLite connections immediately.
    Calling c.interrupt() aborts any in-flight SQLite query in <1ms so shutdown never hangs.
    """
    global _shutting_down
    if shutting_down:
        _shutting_down = True
    with _conn_lock:
        conns = list(_all_thread_conns)
        _all_thread_conns.clear()
    for c in conns:
        try:
            c.interrupt()
        except Exception:
            pass
    for c in conns:
        try:
            c.close()
        except Exception:
            pass
    try:
        if hasattr(_thread_local, "conn"):
            _thread_local.conn = None
    except Exception:
        pass


def convert_frames_to_mp4(pil_frames, durations, mp4_filepath):
    """
    Converts a list of PIL Images and frame durations (in ms) into an MP4 video using OpenCV.
    Handles odd dimensions by padding/resizing to even numbers and preserves variable frame timing.
    Writes to an ASCII temp file first to support Unicode paths on Windows.
    """
    if not _CV2_AVAILABLE or _cv2 is None or not pil_frames:
        return False

    try:
        w, h = pil_frames[0].size
        if w <= 0 or h <= 0:
            return False
        w_even = w + (w % 2)
        h_even = h + (h % 2)

        clean_delays = [max(10, int(d) if d else 100) for d in (durations or [100] * len(pil_frames))]
        if len(clean_delays) < len(pil_frames):
            clean_delays.extend([100] * (len(pil_frames) - len(clean_delays)))

        min_delay = min(clean_delays) if clean_delays else 100
        max_delay = max(clean_delays) if clean_delays else 100

        if max_delay - min_delay <= 5:
            avg_delay = sum(clean_delays) / len(clean_delays)
            fps = min(60.0, max(1.0, 1000.0 / avg_delay))
            frame_repeats = [1] * len(pil_frames)
        else:
            fps = min(60.0, max(10.0, round(1000.0 / min_delay)))
            frame_interval_ms = 1000.0 / fps
            frame_repeats = []
            acc_ms = 0.0
            for d in clean_delays[:len(pil_frames)]:
                acc_ms += d
                rep = max(1, int(round(acc_ms / frame_interval_ms)))
                acc_ms -= rep * frame_interval_ms
                frame_repeats.append(min(rep, 300))

        os.makedirs(os.path.dirname(os.path.abspath(mp4_filepath)), exist_ok=True)
        fd, tmp_mp4 = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)

        writer = None
        for fourcc_str in ('mp4v', 'avc1'):
            try:
                fourcc = _cv2.VideoWriter_fourcc(*fourcc_str)
                vw = _cv2.VideoWriter(tmp_mp4, fourcc, float(fps), (w_even, h_even))
                if vw is not None and vw.isOpened():
                    writer = vw
                    break
            except Exception:
                continue

        if writer is None:
            if os.path.exists(tmp_mp4):
                try:
                    os.remove(tmp_mp4)
                except OSError:
                    pass
            return False

        try:
            for img, rep in zip(pil_frames, frame_repeats):
                if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in getattr(img, 'info', {})):
                    canvas = Image.new('RGB', img.size, (255, 255, 255))
                    rgba = img.convert('RGBA')
                    canvas.paste(rgba, mask=rgba.split()[3])
                    img_rgb = canvas
                else:
                    img_rgb = img.convert('RGB')

                if img_rgb.size != (w_even, h_even):
                    img_rgb = img_rgb.resize((w_even, h_even), Image.Resampling.BILINEAR)

                bgr = np.asarray(img_rgb, dtype=np.uint8)[:, :, ::-1].copy()
                for _ in range(rep):
                    writer.write(bgr)
        finally:
            writer.release()

        if os.path.exists(tmp_mp4) and os.path.getsize(tmp_mp4) > 0:
            if os.path.exists(mp4_filepath):
                try:
                    os.remove(mp4_filepath)
                except OSError:
                    pass
            shutil.move(tmp_mp4, mp4_filepath)
            return True
        else:
            if os.path.exists(tmp_mp4):
                try:
                    os.remove(tmp_mp4)
                except OSError:
                    pass
            return False
    except Exception as e:
        logging.error(f"Error converting Ugoira frames to MP4: {e}")
        return False


def decode_image_cv2_for_wd14(path, target_size=448, target_dtype=np.float32, nchw=False):
    """
    Fast C++ OpenCV image decoder & preprocessor for WD14 Tagger.
    Properly handles 4-channel RGBA/BGRA transparency (compositing onto a white background)
    and 2D grayscale images, matching WD14Tagger.preprocess_image.
    """
    if not _CV2_AVAILABLE or _cv2 is None:
        return None
    raw = np.fromfile(path, dtype=np.uint8)
    img = _cv2.imdecode(raw, _cv2.IMREAD_UNCHANGED)
    if img is None:
        return None

    if img.ndim == 2:
        img = _cv2.cvtColor(img, _cv2.COLOR_GRAY2BGR)
    elif img.ndim == 3 and img.shape[2] == 4:
        if img.dtype == np.uint16:
            img = (img / 256).astype(np.uint8)
        b_ch, g_ch, r_ch, a_ch = _cv2.split(img)
        alpha = a_ch.astype(np.float32) / 255.0
        inv_alpha = 1.0 - alpha
        b = (b_ch.astype(np.float32) * alpha + 255.0 * inv_alpha).astype(np.uint8)
        g = (g_ch.astype(np.float32) * alpha + 255.0 * inv_alpha).astype(np.uint8)
        r = (r_ch.astype(np.float32) * alpha + 255.0 * inv_alpha).astype(np.uint8)
        img = _cv2.merge((b, g, r))
    elif img.ndim == 3 and img.shape[2] > 3:
        img = img[:, :, :3]

    if img.dtype == np.uint16:
        img = (img / 256).astype(np.uint8)

    h, w = img.shape[:2]
    if h <= 0 or w <= 0:
        return None

    scale = target_size / max(h, w)
    nw = max(1, int(w * scale))
    nh = max(1, int(h * scale))
    resized = _cv2.resize(img, (nw, nh), interpolation=_cv2.INTER_LINEAR)
    padded = np.full((target_size, target_size, 3), 255, dtype=target_dtype)
    px = (target_size - nw) // 2
    py = (target_size - nh) // 2
    padded[py:py+nh, px:px+nw] = resized.astype(target_dtype)

    if nchw:
        padded = np.transpose(padded, (2, 0, 1))
    return np.expand_dims(padded, axis=0)


class WorkerSignals(QObject):
    result = pyqtSignal(int, object) 
    image_loaded = pyqtSignal(int, str, QImage, str)
    api_log = pyqtSignal(str)
    # อัปเกรด Signal นี้ให้ส่งรูป (QImage) กลับมาด้วย
    api_updated = pyqtSignal(str, str, object) 
    scan_batch = pyqtSignal(int, list)
    scan_finished = pyqtSignal(int, int)
    folder_updated = pyqtSignal(int, str, str, object, int, float, float)

class FolderCacheScanner(QRunnable):
    def __init__(self, root, db_file, gen_id, force_full=False):
        super().__init__()
        self.root = root; self.db_file = db_file; self.gen_id = gen_id 
        self.force_full = force_full
        self.signals = WorkerSignals(); self.running = True

    @pyqtSlot()
    def run(self):
        # 1. แจ้งเตือนตอนเริ่มอ่านฐานข้อมูล
        _safe_emit(self.signals.api_log, "🔍 กำลังตรวจสอบข้อมูลโฟลเดอร์จาก Database...")
        
        conn = get_thread_local_conn(self.db_file)
        try:
            cursor = conn.cursor()
            known_names = {}
            try:
                cursor.execute("SELECT artist_id, name FROM artists")
                for row in cursor.fetchall(): known_names[row[0]] = row[1]
            except Exception as e: logging.error(f"Error fetching artists: {e}")

            # Phase 1: Fast Cache Emit - ดึงข้อมูลแคชเดิมขึ้นจอทันที ไม่ให้หน้าจอว่าง
            db_folders_info = {}
            batch = []; batch_size = 200

            if not self.force_full:
                try:
                    cursor.execute('''SELECT t.artist_id, a.name, a.profile_blob, t.image_data, t.folder_mtime, t.file_count, t.folder_ctime 
                                      FROM thumbnails t LEFT JOIN artists a ON t.artist_id = a.artist_id''')
                    rows = cursor.fetchall()
                except sqlite3.OperationalError:
                    cursor.execute('''SELECT t.artist_id, a.name, a.profile_blob, t.image_data, t.folder_mtime, t.file_count, 0 
                                      FROM thumbnails t LEFT JOIN artists a ON t.artist_id = a.artist_id''')
                    rows = cursor.fetchall()

                for row in rows:
                    if not self.running: break
                    aid, name, profile_blob, grid_blob, mtime, fcount, ctime = row

                    db_folders_info[aid] = {
                        'name': name,
                        'profile_blob': profile_blob,
                        'grid_blob': grid_blob,
                        'mtime': mtime or 0.0,
                        'fcount': fcount or 0,
                        'ctime': ctime or 0.0
                    }

                    display_name = f"{name} ({aid})" if name else aid
                    # ส่ง raw blob (JPEG ~10KB) แทน QImage (225KB) เพื่อประหยัด RAM
                    batch.append((aid, display_name, grid_blob, profile_blob, mtime or 0.0, fcount or 0, ctime or 0.0))
                    if len(batch) >= batch_size: 
                        _safe_emit(self.signals.scan_batch, self.gen_id, batch)
                        batch = []

                if batch: 
                    _safe_emit(self.signals.scan_batch, self.gen_id, batch)
                    batch = []

            if not self.running: return

            # Phase 2: ตรวจสอบโฟลเดอร์จริงบนฮาร์ดดิสก์ และตรวจนับไฟล์ภายในทุกโฟลเดอร์ (ไส้ใน)
            _safe_emit(self.signals.api_log, "📂 กำลังตรวจสอบการเปลี่ยนแปลงและนับไฟล์ในทุกโฟลเดอร์...")

            try:
                real_folders = [f.name for f in os.scandir(self.root) if f.is_dir() and not f.name.startswith('.')]
            except Exception as e:
                logging.error(f"Error scanning root dir: {e}")
                real_folders = []

            real_folders_set = set(real_folders)

            # จัดการลบโฟลเดอร์ที่ถูกลบออกจากตาราง thumbnails, file_index, file_thumbnails และ file_tags ทันที
            deleted_aids = [aid for aid in db_folders_info.keys() if aid not in real_folders_set]
            if deleted_aids:
                try:
                    for i in range(0, len(deleted_aids), 500):
                        chunk = [(a,) for a in deleted_aids[i:i+500]]
                        cursor.executemany("DELETE FROM thumbnails WHERE artist_id = ?", chunk)
                        parent_patterns = [(os.path.join(self.root, a[0]) + '%',) for a in chunk]
                        cursor.executemany("DELETE FROM file_index WHERE parent_folder LIKE ?", parent_patterns)
                        for a in chunk:
                            prefix = os.path.join(self.root, a[0]) + os.sep
                            prefix_upper = prefix + "\uffff"
                            cursor.execute("DELETE FROM file_thumbnails WHERE path >= ? AND path < ?", (prefix, prefix_upper))
                            cursor.execute("DELETE FROM file_tags WHERE path >= ? AND path < ?", (prefix, prefix_upper))
                    conn.commit()
                except Exception as e: logging.error(f"Error deleting missing folders: {e}")

            new_folders_batch = []
            modified_folders = 0
            new_count = 0
            file_index_inserts = []
            thumbnails_updates = []

            for folder_name in real_folders:
                if not self.running: break
                dir_path = os.path.join(self.root, folder_name)

                # สแกนไฟล์มีเดียภายในโฟลเดอร์นี้
                media_files = []
                try:
                    with os.scandir(dir_path) as it:
                        for entry in it:
                            if not entry.name.startswith('.'):
                                ext = os.path.splitext(entry.name)[1].lower()
                                if ext in ALL_MEDIA_EXT:
                                    media_files.append(entry)
                except Exception:
                    continue

                actual_fcount = len(media_files)
                try:
                    st = os.stat(dir_path)
                    actual_mtime = st.st_mtime
                    actual_ctime = st.st_ctime
                except Exception:
                    actual_mtime = 0.0
                    actual_ctime = 0.0

                dname = f"{known_names[folder_name]} ({folder_name})" if folder_name in known_names else folder_name

                # กรณี A: เป็นโฟลเดอร์ใหม่ (ไม่เคยอยู่ใน DB)
                if folder_name not in db_folders_info:
                    new_count += 1
                    new_folders_batch.append((folder_name, dname, None, None, actual_mtime, actual_fcount, actual_ctime))
                    thumbnails_updates.append((folder_name, None, actual_mtime, actual_fcount, actual_ctime))

                    for entry in media_files:
                        try:
                            est = entry.stat()
                            file_index_inserts.append((entry.path, entry.name, dir_path, est.st_mtime, est.st_size, est.st_ctime))
                        except Exception:
                            pass

                    if len(new_folders_batch) >= batch_size:
                        _safe_emit(self.signals.scan_batch, self.gen_id, new_folders_batch)
                        new_folders_batch = []

                # กรณี B: เป็นโฟลเดอร์เดิม ตรวจสอบว่าไฟล์ภายใน (ไส้ใน) มีการเปลี่ยนแปลงหรือไม่
                else:
                    stored_info = db_folders_info[folder_name]
                    stored_fcount = stored_info['fcount']
                    stored_mtime = stored_info['mtime']
                    grid_blob = stored_info['grid_blob']

                    count_changed = (actual_fcount != stored_fcount)
                    mtime_changed = (abs(actual_mtime - stored_mtime) > 1.0)
                    cover_missing = (grid_blob is None and actual_fcount > 0)

                    if count_changed or mtime_changed or cover_missing:
                        modified_folders += 1

                        # อัปเดตข้อมูลตาราง thumbnails (ถ้า count เปลี่ยน หรือปกหาย ให้เคลียร์ image_data เพื่อให้ ThumbnailGeneratorWorker เจนปกใหม่)
                        new_blob = None if (count_changed or cover_missing) else grid_blob
                        thumbnails_updates.append((folder_name, new_blob, actual_mtime, actual_fcount, actual_ctime))

                        # อัปเดตตาราง file_index ให้ตรงกับไฟล์จริง
                        try:
                            cursor.execute("DELETE FROM file_index WHERE parent_folder = ?", (dir_path,))
                            for entry in media_files:
                                try:
                                    est = entry.stat()
                                    file_index_inserts.append((entry.path, entry.name, dir_path, est.st_mtime, est.st_size, est.st_ctime))
                                except Exception:
                                    pass
                        except Exception as e:
                            logging.error(f"Error updating file_index for {dir_path}: {e}")

                        # ส่งสัญญาณแจ้ง UI ให้อัปเดตจำนวนไฟล์และคิวเจนปกทันที
                        _safe_emit(self.signals.folder_updated, self.gen_id, folder_name, dname, None, actual_fcount, actual_mtime, actual_ctime)

                # Batch save to DB
                if len(thumbnails_updates) >= 500:
                    cursor.executemany("INSERT OR REPLACE INTO thumbnails (artist_id, image_data, folder_mtime, file_count, folder_ctime) VALUES (?, ?, ?, ?, ?)", thumbnails_updates)
                    conn.commit()
                    thumbnails_updates = []

                if len(file_index_inserts) >= 1000:
                    cursor.executemany("INSERT OR REPLACE INTO file_index (path, filename, parent_folder, mtime, size, ctime) VALUES (?, ?, ?, ?, ?, ?)", file_index_inserts)
                    conn.commit()
                    file_index_inserts = []

            if new_folders_batch:
                _safe_emit(self.signals.scan_batch, self.gen_id, new_folders_batch)

            if thumbnails_updates:
                cursor.executemany("INSERT OR REPLACE INTO thumbnails (artist_id, image_data, folder_mtime, file_count, folder_ctime) VALUES (?, ?, ?, ?, ?)", thumbnails_updates)
            if file_index_inserts:
                cursor.executemany("INSERT OR REPLACE INTO file_index (path, filename, parent_folder, mtime, size, ctime) VALUES (?, ?, ?, ?, ?, ?)", file_index_inserts)
            conn.commit()

            total_folders = len(real_folders)
            _safe_emit(self.signals.scan_finished, self.gen_id, total_folders)
            log_msg = f"✅ ตรวจสอบเสร็จสิ้น: {total_folders} โฟลเดอร์ (พบใหม่ {new_count}, มีไฟล์เพิ่ม/ลด {modified_folders}, ลบออก {len(deleted_aids)})"
            _safe_emit(self.signals.api_log, log_msg)

        except Exception as e:
            logging.error(f"Error in FolderCacheScanner: {e}")
        finally:
            pass
        
    def stop(self): self.running = False

class ThumbnailGeneratorWorker(QRunnable):
    def __init__(self, artist_id, folder_path, db_file, gen_id, force_update=False, quick_randomize=False):
        super().__init__()
        self.artist_id = artist_id; self.folder_path = folder_path; self.db_file = db_file
        self.gen_id = gen_id; self.force_update = force_update; self.quick_randomize = quick_randomize; self.signals = WorkerSignals()

    def check_if_cover_incomplete(self, blob):
        try:
            # 1. Count actual media files in folder
            from config import ALL_MEDIA_EXT
            all_media = [f for f in os.listdir(self.folder_path) if os.path.splitext(f)[1].lower() in ALL_MEDIA_EXT and not f.startswith('.')]
            total_files = len(all_media)
            if total_files == 0:
                return False

            # 2. Decode the old blob using PIL
            img = Image.open(io.BytesIO(blob))
            if img.size != (240, 240):
                return True # Incorrect size, regenerate

            # 3. Check the 4 quadrants
            empty_quadrants = 0
            pos_boxes = [
                (0, 0, 120, 120),
                (120, 0, 240, 120),
                (0, 120, 120, 240),
                (120, 120, 240, 240)
            ]
            for box in pos_boxes:
                quad = img.crop(box)
                quad_small = quad.resize((10, 10))
                pixels = list(quad_small.getdata())
                is_empty = True
                for p in pixels:
                    if isinstance(p, tuple) and len(p) >= 3:
                        if abs(p[0] - 30) > 8 or abs(p[1] - 30) > 8 or abs(p[2] - 30) > 8:
                            is_empty = False
                            break
                    else:
                        is_empty = False
                        break
                if is_empty:
                    empty_quadrants += 1

            filled_quadrants = 4 - empty_quadrants
            # If we have empty quadrants and we have more files than filled quadrants, it's incomplete!
            if filled_quadrants < 4 and total_files > filled_quadrants:
                return True
        except Exception:
            return True # If anything fails, regenerate to be safe
        return False

    @pyqtSlot()
    def run(self):
        try:
            conn = get_thread_local_conn(self.db_file)
            try:
                cursor = conn.cursor()
                cursor.execute('SELECT image_data, folder_mtime, file_count FROM thumbnails WHERE artist_id=?', (self.artist_id,))
                row = cursor.fetchone()
                old_blob = row[0] if row else None
                old_mtime = row[1] if row else 0

                try:
                    st = os.stat(self.folder_path)
                    current_mtime = st.st_mtime
                    current_ctime = st.st_ctime
                except:
                    current_mtime = 0.0
                    current_ctime = 0.0

                cursor.execute('SELECT name FROM artists WHERE artist_id=?',(self.artist_id,))
                res = cursor.fetchone()
                name = f"{res[0]} ({self.artist_id})" if (res and res[0]) else self.artist_id

                blob = None
                file_count = 0

                if old_blob and abs(current_mtime - old_mtime) < 1.0 and not self.force_update:
                    is_incomplete = False
                    if self.quick_randomize:
                        is_incomplete = self.check_if_cover_incomplete(old_blob)

                    if not is_incomplete:
                        blob = old_blob; file_count = row[2] if row else 0

                if blob is None:
                    # 1. เข้าไปนับและดึงไฟล์มีเดียทั้งหมดในโฟลเดอร์จริงๆ
                    try: 
                        all_media = [f for f in os.listdir(self.folder_path) if os.path.splitext(f)[1].lower() in ALL_MEDIA_EXT and not f.startswith('.')]
                        file_count = len(all_media)
                    except: 
                        all_media = []
                        file_count = 0

                    files = all_media

                    # โยนไฟล์ไปทำรูปปกตามปกติ
                    pil = self.composite(self.folder_path, files)
                    if pil:
                        bio = io.BytesIO(); pil.save(bio, 'JPEG', quality=85); blob = bio.getvalue()
                        try:
                            cursor.execute('INSERT OR REPLACE INTO thumbnails (artist_id, image_data, folder_mtime, file_count, folder_ctime) VALUES(?,?,?,?,?)',(self.artist_id,blob,current_mtime,file_count,current_ctime))
                            conn.commit()
                        except Exception as e: logging.error(f"Error: {e}")

            finally:
                pass
            # Emit blob (ไม่ใช่ QImage) — UI จะ decode เฉพาะตอนแสดงผลจริง
            _safe_emit(self.signals.folder_updated, self.gen_id, self.artist_id, name, blob, file_count, current_mtime, current_ctime)
        except Exception as e: logging.error(f"Error: {e}")

    def composite(self, path, files):
        if not files: return None
        sample = random.sample(files, min(len(files), 4))
        canvas = Image.new('RGB', (240, 240), (30,30,30)); pos = [(0,0), (120,0), (0,120), (120,120)]
        for i, f in enumerate(sample):
            img, _ = load_media_thumbnail(os.path.join(path, f))
            if img:
                img_rgb = make_thumbnail_rgb(img, (120, 120))
                canvas.paste(ImageOps.fit(img_rgb, (120,120), Image.Resampling.LANCZOS), pos[i])
        return canvas

class ImageLoaderWorker(QRunnable):
    def __init__(self, path, db_file, gen_id, get_gen_id_fn=None): 
        super().__init__(); self.path = path; self.db_file = db_file; self.gen_id = gen_id; self.signals = WorkerSignals()
        self.get_gen_id_fn = get_gen_id_fn
        self.file_size = 0 # Default

    @pyqtSlot()
    def run(self):
        # [ดักจับ] ถ้ารุ่นบัตรคิวในเครื่องนี้มันเก่ากว่าของแอปหลักแล้ว แปลว่าผู้ใช้เปลี่ยนหน้าหนีไปแล้ว ให้ทิ้งงานนี้ทันทีเพื่อคืน CPU!
        if self.get_gen_id_fn and self.gen_id != self.get_gen_id_fn():
            return

        blob = None; qimg = None; ftype = None
        ext = os.path.splitext(self.path)[1].lower()
        if ext in EXT_VID: ftype = 'VIDEO'
        elif ext in EXT_GIF: ftype = 'GIF'
        elif ext in EXT_ZIP: ftype = 'UGOIRA'

        # 1. Try to fetch from SQLite cache (resilient to locks)
        try:
            conn = get_thread_local_conn(self.db_file, timeout=10.0)
            cursor = conn.cursor()
            cursor.execute("SELECT image_data FROM file_thumbnails WHERE path=?", (self.path,))
            row = cursor.fetchone()
            if row:
                blob = row[0]
                qimg = QImage.fromData(blob)
        except Exception as e:
            logging.debug(f"[workers.py] SQLite Read Error for {self.path}: {e}")

        # 2. If not found in cache, generate it from disk (images only)
        if qimg is None or qimg.isNull():
            if ext in (EXT_VID | EXT_GIF | EXT_ZIP):
                pass # Skip generating heavy video/animation thumbnails on foreground thread pool
            else:
                try:
                    pil_img, _ = load_media_thumbnail(self.path)
                    if pil_img:
                        thumb_img = make_thumbnail_rgb(pil_img, (240, 240))
                        bio = io.BytesIO()
                        thumb_img.save(bio, 'JPEG', quality=80)
                        blob = bio.getvalue()

                        bio_load = io.BytesIO(blob)
                        qimg = QImage()
                        qimg.loadFromData(bio_load.getvalue())

                    # Try to save back to SQLite cache
                    try:
                        conn = get_thread_local_conn(self.db_file, timeout=10.0)
                        cursor = conn.cursor()
                        cursor.execute("INSERT OR REPLACE INTO file_thumbnails (path, image_data) VALUES (?, ?)", (self.path, blob))
                        conn.commit()
                    except Exception as e:
                        logging.debug(f"[workers.py] SQLite Write Error for {self.path}: {e}")
                except Exception as e:
                    logging.debug(f"[workers.py] Thumbnail Generation Error for {self.path}: {e}")

        # 3. Emit the loaded thumbnail
        if qimg and not qimg.isNull():
            _safe_emit(self.signals.image_loaded, self.gen_id, self.path, qimg, ftype)

class StreamScanner(QRunnable):
    def __init__(self, root, db_file, gen_id, use_cache=True, filter_mode='ALL',
                 min_size=None, max_size=None, min_mtime=None, search_keyword=None,
                 tag_filter=None, parsed_query=None, rating_filter='ALL'):
        super().__init__()
        self.root = root; self.db_file = db_file; self.gen_id = gen_id
        self.use_cache = use_cache; self.filter_mode = filter_mode
        self.min_size = min_size; self.max_size = max_size
        self.min_mtime = min_mtime; self.search_keyword = search_keyword
        self.tag_filter = tag_filter
        self.parsed_query = parsed_query
        self.rating_filter = rating_filter or 'ALL'
        self.matched_tag_paths = None
        self.kw_tag_paths = {}
        self.artist_name_map = {}
        self.signals = WorkerSignals(); self.running = True

        if self.parsed_query:
            if self.parsed_query.min_size and (self.min_size is None or self.parsed_query.min_size > self.min_size):
                self.min_size = self.parsed_query.min_size
            if self.parsed_query.max_size and (self.max_size is None or self.parsed_query.max_size < self.max_size):
                self.max_size = self.parsed_query.max_size

    def check_filter(self, filename, filepath=None, fsize=None, fmtime=None):
        ext = os.path.splitext(filename)[1].lower()
        if self.filter_mode == 'PHOTO' and ext not in EXT_IMG: return False
        if self.filter_mode == 'ANIMATE' and ext not in (EXT_GIF | EXT_VID | EXT_ZIP): return False
        if ext not in ALL_MEDIA_EXT: return False

        # Check file type filter from parsed_query (e.g. type:gif, type:png)
        if self.parsed_query and self.parsed_query.file_types:
            clean_ext = ext.lstrip('.')
            if clean_ext not in self.parsed_query.file_types:
                return False

        # Check artist filter from parsed_query (e.g. artist:12345 or artist:ask)
        if self.parsed_query and (self.parsed_query.include_artists or self.parsed_query.exclude_artists) and filepath:
            parent_folder_name = os.path.basename(os.path.dirname(filepath)).lower()
            artist_name = self.artist_name_map.get(parent_folder_name, "")
            for inc_art in self.parsed_query.include_artists:
                art_l = inc_art.lower()
                if not (art_l == parent_folder_name or art_l in parent_folder_name or (artist_name and art_l in artist_name)):
                    return False
            for exc_art in self.parsed_query.exclude_artists:
                art_l = exc_art.lower()
                if art_l == parent_folder_name or art_l in parent_folder_name or (artist_name and art_l in artist_name):
                    return False

        # Check tag/rating/favorite filter (SFW/NSFW, characters, series, tags, is:fav)
        if self.matched_tag_paths is not None and filepath and filepath not in self.matched_tag_paths:
            return False

        # Check text search keyword
        if self.search_keyword and self.search_keyword not in filename.lower():
            return False

        # Check parsed_query keywords (match filename OR tag OR artist name)
        if self.parsed_query and self.parsed_query.keywords:
            fn_lower = filename.lower()
            parent_folder_name = os.path.basename(os.path.dirname(filepath)).lower() if filepath else ""
            artist_name = self.artist_name_map.get(parent_folder_name, "") if parent_folder_name else ""
            for kw in self.parsed_query.keywords:
                matched_in_fn = kw in fn_lower or (artist_name and kw in artist_name)
                matched_in_tags = (filepath is not None and filepath in self.kw_tag_paths.get(kw, set()))
                if not matched_in_fn and not matched_in_tags:
                    return False

        # Get size/mtime from disk if not provided but filepath is available
        if filepath and (fsize is None or fmtime is None):
            need_size = (fsize is None and (self.min_size is not None or self.max_size is not None))
            need_mtime = (fmtime is None and self.min_mtime is not None)
            if need_size or need_mtime:
                try:
                    st = os.stat(filepath)
                    if need_size: fsize = st.st_size
                    if need_mtime: fmtime = st.st_mtime
                except Exception:
                    return False

        # Apply size filter
        if fsize is not None:
            if self.min_size is not None and fsize < self.min_size: return False
            if self.max_size is not None and fsize > self.max_size: return False
            
        # Apply mtime filter
        if fmtime is not None:
            if self.min_mtime is not None and fmtime < self.min_mtime: return False

        return True

    @pyqtSlot()
    def run(self):
        _safe_emit(self.signals.api_log, "กำลังสแกนหาไฟล์รูปภาพ/วิดีโอทั้งหมด...")
        conn = get_thread_local_conn(self.db_file); cursor = conn.cursor()

        # Load artist names for artist: filter and keyword search
        try:
            cursor.execute("SELECT artist_id, name FROM artists WHERE name IS NOT NULL")
            for aid, name in cursor.fetchall():
                if aid and name:
                    self.artist_name_map[str(aid).lower()] = str(name).lower()
        except Exception:
            pass

        # Resolve tag / character / rating / favorite queries via query_parser
        has_query = (
            self.parsed_query is not None or 
            (self.rating_filter and self.rating_filter != 'ALL') or 
            bool(self.tag_filter)
        )
        if has_query:
            try:
                import copy
                from query_parser import resolve_matching_paths, parse_search_query, SearchQuery
                q = self.parsed_query or SearchQuery()
                if isinstance(q, str):
                    q = parse_search_query(q)
                    self.parsed_query = q

                # Artist filtering is handled directly in check_filter so unindexed disk files also match
                q_for_tags = copy.copy(q)
                q_for_tags.include_artists = []
                q_for_tags.exclude_artists = []

                resolved = resolve_matching_paths(conn, q_for_tags, active_rating=self.rating_filter, active_char=self.tag_filter)
                if resolved is not None:
                    self.matched_tag_paths = resolved

                # Pre-fetch keyword matches from file_tags so user can search tags by plain text
                if q.keywords:
                    for kw in q.keywords:
                        try:
                            cursor.execute("SELECT DISTINCT path FROM file_tags WHERE tag_name = ? OR tag_name LIKE ?",
                                           (kw, f"%{kw}%"))
                            self.kw_tag_paths[kw] = {r[0] for r in cursor.fetchall()}
                        except Exception:
                            pass
            except Exception as e:
                logging.error(f"Error resolving query parser in StreamScanner: {e}")
        elif self.tag_filter:
            try:
                cursor.execute("SELECT DISTINCT path FROM file_tags WHERE tag_name = ? OR tag_name LIKE ?", 
                               (self.tag_filter, f"%{self.tag_filter}%"))
                self.matched_tag_paths = {row[0] for row in cursor.fetchall()}
            except Exception as e:
                logging.error(f"Error filtering by tag: {e}")
                self.matched_tag_paths = set()

        # 1. Fetch all files currently in DB under this root
        db_files = {} # path -> (size, mtime, ctime)
        try:
            search_prefix = self.root + os.sep
            search_prefix_upper = self.root + os.sep + "\uffff"
            try:
                cursor.execute("SELECT path, size, mtime, ctime FROM file_index WHERE path >= ? AND path < ?", (search_prefix, search_prefix_upper))
                for row in cursor.fetchall():
                    if self.matched_tag_paths is not None and row[0] not in self.matched_tag_paths:
                        continue
                    db_files[row[0]] = (row[1], row[2], row[3] if len(row) > 3 and row[3] is not None else 0.0)
            except sqlite3.OperationalError:
                cursor.execute("SELECT path, size, mtime FROM file_index WHERE path >= ? AND path < ?", (search_prefix, search_prefix_upper))
                for row in cursor.fetchall():
                    if self.matched_tag_paths is not None and row[0] not in self.matched_tag_paths:
                        continue
                    db_files[row[0]] = (row[1], row[2], 0.0)
        except Exception as e:
            pass

        seen_on_disk = set()
        new_or_updated = []
        ui_batch = []; batch_size = 500

        # If using cache, immediately send the existing DB entries to the UI
        if self.use_cache:
            db_list = []
            for path, (size, mtime, ctime) in db_files.items():
                if self.check_filter(os.path.basename(path), filepath=path, fsize=size, fmtime=mtime):
                    db_list.append((path, size, mtime, ctime))
                    if len(db_list) >= batch_size:
                        _safe_emit(self.signals.scan_batch, self.gen_id, db_list)
                        db_list = []
            if db_list:
                _safe_emit(self.signals.scan_batch, self.gen_id, db_list)

        # 2. Walk directory to find additions and modifications
        for r, d, f in os.walk(self.root):
            if not self.running: break
            d[:] = [dirname for dirname in d if not dirname.startswith('.')]
            for file in f:
                if not self.running: break
                if file.startswith('.'): continue
                full_path = os.path.join(r, file)
                if self.check_filter(file, full_path):
                    seen_on_disk.add(full_path)

                    try:
                        st = os.stat(full_path)
                        fsize = st.st_size
                        fmtime = st.st_mtime
                        fctime = st.st_ctime
                    except:
                        continue

                    in_db = full_path in db_files
                    if not in_db:
                        new_or_updated.append((full_path, fsize, fmtime, fctime, r))
                        ui_batch.append((full_path, fsize, fmtime, fctime)) # ส่งไฟล์ที่เพิ่งค้นพบใหม่ให้ UI ทันที!
                    else:
                        db_size, db_mtime, db_ctime = db_files[full_path]
                        if abs(fsize - db_size) > 0.01 or abs(fmtime - db_mtime) > 1.0:
                            new_or_updated.append((full_path, fsize, fmtime, fctime, r))
                            if not self.use_cache:
                                ui_batch.append((full_path, fsize, fmtime, fctime))
                        else:
                            if not self.use_cache:
                                ui_batch.append((full_path, fsize, fmtime, fctime))

                    if len(ui_batch) >= batch_size:
                        _safe_emit(self.signals.scan_batch, self.gen_id, ui_batch)
                        ui_batch = []

                    if len(new_or_updated) >= batch_size:
                        self.save_batch_to_db(cursor, new_or_updated)
                        new_or_updated = []

        if ui_batch:
            _safe_emit(self.signals.scan_batch, self.gen_id, ui_batch)
        if new_or_updated:
            self.save_batch_to_db(cursor, new_or_updated)

        # 3. Clean up deleted files from SQLite index (only when not filtering by subset)
        deleted_paths = []
        is_unfiltered_scan = (
            self.filter_mode == 'ALL'
            and self.matched_tag_paths is None
            and not self.search_keyword
            and self.min_size is None
            and self.max_size is None
            and self.min_mtime is None
            and (not self.parsed_query or (
                not self.parsed_query.file_types
                and not self.parsed_query.keywords
                and not self.parsed_query.include_artists
                and not self.parsed_query.exclude_artists
            ))
        )
        for path in db_files.keys():
            if path not in seen_on_disk:
                if is_unfiltered_scan or not os.path.exists(path):
                    deleted_paths.append((path,))

        if deleted_paths:
            try:
                for i in range(0, len(deleted_paths), 500):
                    batch_del = deleted_paths[i:i+500]
                    cursor.executemany("DELETE FROM file_index WHERE path = ?", batch_del)
                    cursor.executemany("DELETE FROM file_thumbnails WHERE path = ?", batch_del)
                    cursor.executemany("DELETE FROM file_tags WHERE path = ?", batch_del)
                conn.commit()
            except Exception as e:
                pass

        conn.commit()
        total_count = len(seen_on_disk)
        _safe_emit(self.signals.scan_finished, self.gen_id, total_count)
        _safe_emit(self.signals.api_log, f"สแกนเสร็จสิ้น พบไฟล์ทั้งหมด {total_count} ไฟล์ (ลบไฟล์ที่หายไป {len(deleted_paths)} ไฟล์)")

    def save_batch_to_db(self, cursor, data_tuples):
        try:
            db_data = [(p, os.path.basename(p), parent, mt, s, ct) for p, s, mt, ct, parent in data_tuples]
            cursor.executemany("INSERT OR REPLACE INTO file_index (path, filename, parent_folder, mtime, size, ctime) VALUES (?, ?, ?, ?, ?, ?)", db_data)
        except Exception as e: logging.error(f"Error: {e}")
    def stop(self): self.running = False

# ApiFetcherWorker ไม่ต้องแก้เยอะ เพราะมันทำงานแยกอิสระ แต่เพื่อความเข้ากันได้
class ApiFetcherWorker(QRunnable):
    def __init__(self, ids, token): 
        super().__init__()
        self.ids = ids; self.token = token
        self.signals = WorkerSignals()
        self.running = True

    def download_image(self, url):
        # Pixiv ต้องการ Referer ไม่งั้น 403
        headers = {'Referer': 'https://app-api.pixiv.net/'}
        try:
            r = requests.get(url, headers=headers, stream=True, timeout=5)
            if r.status_code == 200:
                return r.content
        except Exception as e: logging.error(f"Error: {e}")
        return None

    @pyqtSlot()
    def run(self):
        try: 
            api = AppPixivAPI()
            api.auth(refresh_token=self.token)
        except Exception as e: 
            _safe_emit(self.signals.api_log, str(e))
            return

        for i, aid in enumerate(self.ids):
            if not self.running: break
            try:
                res = api.user_detail(int(aid))
                if res.user: 
                    name = res.user.name
                    # ดึง URL รูป profile (เอา medium ก็พอ)
                    avatar_url = res.user.profile_image_urls.medium

                    # ดาวน์โหลดรูป
                    blob = self.download_image(avatar_url)

                    try:
                        conn_write = get_thread_local_conn(DB_FILE, timeout=5.0)
                        cursor_write = conn_write.cursor()
                        if blob:
                            cursor_write.execute('UPDATE artists SET name=?, profile_blob=? WHERE artist_id=?', (name, blob, aid))
                            if cursor_write.rowcount == 0:
                                cursor_write.execute('INSERT INTO artists (artist_id, name, profile_blob) VALUES(?,?,?)', (aid, name, blob))
                        else:
                            cursor_write.execute('UPDATE artists SET name=? WHERE artist_id=?', (name, aid))
                            if cursor_write.rowcount == 0:
                                cursor_write.execute('INSERT INTO artists (artist_id, name) VALUES(?,?)', (aid, name))
                        conn_write.commit()
                    except Exception as e: logging.error(f"Error: {e}")

                    _safe_emit(self.signals.api_updated, aid, name, blob)
                    _safe_emit(self.signals.api_log, f"Updated {aid}: {name}")
                time.sleep(1.5) # ลดความถี่ลงเพื่อหลีกเลี่ยง rate limit
            except Exception as e:
                _safe_emit(self.signals.api_log, f"Error {aid}: {e}")
                time.sleep(2)

        _safe_emit(self.signals.api_log, "Done")

    def stop(self): self.running = False


class BooruNameUpdateWorker(QRunnable):
    def __init__(self, ids):
        super().__init__()
        self.ids = ids
        self.signals = WorkerSignals()
        self.running = True
        self.danbooru_client = DanbooruClient()

    def fetch_artist(self, aid):
        if not self.running:
            return aid, None, None, "cancelled"

        found_name = None
        avatar_blob = None
        is_pixiv_deleted = False

        # --- Source 1: Official Pixiv AJAX API (No login/cookie needed for public profile details) ---
        try:
            pixiv_url = f"https://www.pixiv.net/ajax/user/{aid}?lang=en"
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Referer': f'https://www.pixiv.net/en/users/{aid}'
            }
            res = requests.get(pixiv_url, headers=headers, timeout=5)
            if res.status_code == 200:
                data = res.json()
                if not data.get('error'):
                    raw_name = data.get('body', {}).get('name')
                    if raw_name:
                        found_name = raw_name
                        # ดึงภาพ Avatar ประจำตัว (ถ้ามี)
                        img_url = data.get('body', {}).get('imageBig')
                        if img_url:
                            try:
                                r_img = requests.get(img_url, headers={'Referer': 'https://www.pixiv.net/', 'User-Agent': headers['User-Agent']}, timeout=5)
                                if r_img.status_code == 200 and r_img.content:
                                    avatar_blob = r_img.content
                            except Exception:
                                pass
                else:
                    msg = str(data.get('message', '')).lower()
                    if 'left pixiv' in msg or 'not exist' in msg:
                        is_pixiv_deleted = True
            elif res.status_code == 404:
                is_pixiv_deleted = True
        except Exception as e:
            logging.debug(f"Pixiv fetch error for ID {aid}: {e}")

        # --- Source 2: Danbooru (DanbuDL Engine with Exact URL Verification) ---
        if not found_name and self.running:
            found_name = self.danbooru_client.find_artist_by_pixiv_id(aid)

        # --- Source 3: Safebooru Fallback (queries Booru metadata tags) ---
        if not found_name and self.running:
            found_name = self.danbooru_client.find_artist_on_safebooru(aid)

        if found_name:
            return aid, found_name, avatar_blob, "found"
        elif is_pixiv_deleted:
            # ผู้ใช้ปิด/ลบบัญชีไปแล้วจาก Pixiv และไม่มีบน Booru
            return aid, "[Deleted User]", None, "deleted"
        else:
            return aid, None, None, "not_found"

    @pyqtSlot()
    def run(self):
        # ใช้ ThreadPool แยกร่างทำงานพร้อมกันทีละ 3 คน
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            futures = {executor.submit(self.fetch_artist, aid): aid for aid in self.ids}

            for future in concurrent.futures.as_completed(futures):
                if not self.running: 
                    executor.shutdown(wait=False, cancel_futures=True)
                    break

                aid, found_name, avatar_blob, status = future.result()

                if found_name:
                    try:
                        conn_write = get_thread_local_conn(DB_FILE, timeout=5.0)
                        cursor_write = conn_write.cursor()
                        if avatar_blob:
                            cursor_write.execute('UPDATE artists SET name=?, profile_blob=? WHERE artist_id=?', (found_name, avatar_blob, aid))
                            if cursor_write.rowcount == 0:
                                cursor_write.execute('INSERT INTO artists (artist_id, name, profile_blob) VALUES (?, ?, ?)', (aid, found_name, avatar_blob))
                        else:
                            cursor_write.execute('UPDATE artists SET name=?, profile_blob=COALESCE(?, profile_blob) WHERE artist_id=?', (found_name, None, aid))
                            if cursor_write.rowcount == 0:
                                cursor_write.execute('INSERT INTO artists (artist_id, name) VALUES (?, ?)', (aid, found_name))
                        conn_write.commit()
                    except Exception as e:
                        logging.error(f"Error saving artist {aid}: {e}")

                    _safe_emit(self.signals.api_updated, aid, found_name, avatar_blob)
                    if status == "deleted":
                        _safe_emit(self.signals.api_log, f"ID {aid}: บัญชีถูกลบ/ปิดไปแล้วจาก Pixiv -> บันทึกเป็น [Deleted User]")
                    else:
                        _safe_emit(self.signals.api_log, f"Found: {found_name}")
                else:
                    _safe_emit(self.signals.api_log, f"ID {aid} not found in Pixiv/Danbooru/Safebooru")

        _safe_emit(self.signals.api_log, "Name Update Finished")

    def stop(self):
        self.running = False

class BackgroundThumbnailPreloader(QRunnable):
    def __init__(self, files, db_file, gen_id):
        super().__init__()
        self.files = files  # รับลิสต์ไฟล์ทั้งหมดมา
        self.db_file = db_file
        self.gen_id = gen_id
        self.running = True

    @pyqtSlot()
    def run(self):
        import concurrent.futures

        # 1. กรองหาไฟล์ที่ยังไม่มีแคชโดยตรวจสอบเป็นชุดๆ (Chunked Batch Query) เพื่อประหยัด RAM
        pending_files = []
        try:
            conn = get_thread_local_conn(self.db_file, timeout=10.0)
            cursor = conn.cursor()
            for i in range(0, len(self.files), 500):
                if not self.running: return
                chunk = self.files[i:i+500]
                placeholders = ','.join(['?'] * len(chunk))
                cursor.execute(f"SELECT path FROM file_thumbnails WHERE path IN ({placeholders})", chunk)
                cached_in_chunk = {row[0] for row in cursor.fetchall()}
                for p in chunk:
                    if p not in cached_in_chunk:
                        pending_files.append(p)
        except Exception as e:
            logging.error(f"[BackgroundThumbnailPreloader] Chunk query error: {e}")
            pending_files = list(self.files)

        if not pending_files or not self.running:
            return

        # 3. ฟังก์ชันแปลงปกแยกบน Thread ต่างหาก (ใช้พลัง CPU หลายคอร์)
        def generate_thumbnail(path):
            try:
                pil_img, _ = load_media_thumbnail(path)
                if pil_img:
                    thumb_img = make_thumbnail_rgb(pil_img, (240, 240))
                    bio = io.BytesIO()
                    thumb_img.save(bio, 'JPEG', quality=80)
                    return path, bio.getvalue()
            except Exception:
                pass
            return path, None

        # ใช้ Thread น้อยลงเพื่อไม่แย่ง RAM และ CPU กับการทำงานหลักของ UI
        num_workers = min(3, os.cpu_count() or 2)
        uncommitted = []

        try:
            conn = get_thread_local_conn(self.db_file, timeout=30.0)
            cursor = conn.cursor()

            executor = concurrent.futures.ThreadPoolExecutor(max_workers=num_workers)
            try:
                # ส่งงานย่อยทั้งหมดเข้าคิวขนาน
                future_to_path = {executor.submit(generate_thumbnail, p): p for p in pending_files}

                for future in concurrent.futures.as_completed(future_to_path):
                    if not self.running:
                        # หากโปรแกรมถูกสั่งหยุด ให้ยกเลิกงานที่เหลือในคิวขนานทันที
                        executor.shutdown(wait=False, cancel_futures=True)
                        break

                    try:
                        path, blob = future.result()
                        if blob:
                            cursor.execute("INSERT OR REPLACE INTO file_thumbnails (path, image_data) VALUES (?, ?)", (path, blob))
                            uncommitted.append(path)

                            if len(uncommitted) >= 50:
                                conn.commit()
                                uncommitted = []
                    except Exception:
                        pass
            finally:
                executor.shutdown(wait=False, cancel_futures=True)

        finally:
            if uncommitted:
                try:
                    conn.commit()
                except Exception:
                    pass

    def stop(self):
        self.running = False


class DownloadSignals(QObject):
    log = pyqtSignal(str)
    progress = pyqtSignal(int, int)
    finished = pyqtSignal(bool, str)


class PixivDownloaderWorker(QRunnable):
    def __init__(self, mode, target_id, output_dir, cookie=None, bookmark_rest='show', max_limit=None, start_page=None, end_page=None):
        super().__init__()
        self.mode = mode  # 'ILLUST', 'ARTIST', or 'BOOKMARK'
        self.target_id = target_id
        self.output_dir = output_dir
        self.cookie = cookie
        self.bookmark_rest = bookmark_rest
        self.max_limit = max_limit
        self.start_page = start_page
        self.end_page = end_page
        self.signals = DownloadSignals()
        self.running = True

    def stop(self):
        self.running = False

    def interruptible_sleep(self, seconds, step=0.2):
        elapsed = 0.0
        while self.running and elapsed < seconds:
            time.sleep(min(step, seconds - elapsed))
            elapsed += step

    def download_image(self, url, filepath):
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://www.pixiv.net/'
        }
        try:
            r = requests.get(url, headers=headers, stream=True, timeout=15)
            if r.status_code == 200:
                os.makedirs(os.path.dirname(filepath), exist_ok=True)
                with open(filepath, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if not self.running:
                            f.close()
                            if os.path.exists(filepath):
                                try: os.remove(filepath)
                                except Exception: pass
                            return False
                        f.write(chunk)
                return True
        except Exception as e:
            _safe_emit(self.signals.log, f"❌ Error downloading {os.path.basename(filepath)}: {e}")
            if os.path.exists(filepath):
                try: os.remove(filepath)
                except Exception: pass
        return False

    def register_file_in_db(self, filepath, artist_id, artist_name):
        try:
            conn = get_thread_local_conn(DB_FILE, timeout=10.0)
            cursor = conn.cursor()
        
            # 1. Register or update artist name
            if artist_id and artist_name:
                cursor.execute('UPDATE artists SET name=? WHERE artist_id=?', (artist_name, artist_id))
                if cursor.rowcount == 0:
                    cursor.execute('INSERT OR IGNORE INTO artists (artist_id, name) VALUES (?, ?)', (artist_id, artist_name))
        
            # 2. Register file in index
            filename = os.path.basename(filepath)
            parent_folder = os.path.dirname(filepath)
            try:
                st = os.stat(filepath)
                size = st.st_size
                mtime = st.st_mtime
                ctime = st.st_ctime
            except:
                size = 0
                mtime = 0.0
                ctime = 0.0
        
            cursor.execute('INSERT OR REPLACE INTO file_index (path, filename, parent_folder, mtime, size, ctime) VALUES (?, ?, ?, ?, ?, ?)',
                           (filepath, filename, parent_folder, mtime, size, ctime))
            conn.commit()
        except Exception:
            pass

    def process_ugoira(self, ill_id, headers, dest_dir, filename_prefix, artist_id=None, artist_name=None):
        meta_url = f"https://www.pixiv.net/ajax/illust/{ill_id}/ugoira_meta"
        temp_zip = None
        try:
            req_headers = headers.copy()
            req_headers['Referer'] = f"https://www.pixiv.net/artworks/{ill_id}"
            r = requests.get(meta_url, headers=req_headers, timeout=15)
            if r.status_code != 200:
                if r.status_code == 404:
                    if not self.cookie:
                        _safe_emit(self.signals.log, "   ❌ Failed to fetch Ugoira metadata (HTTP 404): This artwork is likely R-18 / restricted. Pixiv requires a valid PHPSESSID cookie to download R-18 works.")
                    else:
                        _safe_emit(self.signals.log, "   ❌ Failed to fetch Ugoira metadata (HTTP 404): Artwork not found or inaccessible. If this is an R-18 work, please verify your PHPSESSID is valid and R-18 display is enabled in Pixiv Settings.")
                else:
                    _safe_emit(self.signals.log, f"   ❌ Failed to fetch Ugoira metadata (HTTP {r.status_code})")
                return False
            data = r.json()
            if data.get('error'):
                _safe_emit(self.signals.log, f"   ❌ Ugoira API error: {data.get('message')}")
                return False
        
            body = data.get('body', {})
            zip_url = body.get('src')
            frames = body.get('frames', [])
            if not zip_url or not frames:
                _safe_emit(self.signals.log, "   ❌ Ugoira metadata is incomplete (missing zip src or frames)")
                return False

            if len(frames) > MAX_UGOIRA_FRAMES:
                _safe_emit(self.signals.log, f"   ⚠️ Ugoira has too many frames ({len(frames)}), truncating to {MAX_UGOIRA_FRAMES}.")
                frames = frames[:MAX_UGOIRA_FRAMES]

            mp4_filename = f"{filename_prefix}.mp4"
            mp4_filepath = os.path.join(dest_dir, mp4_filename)
            gif_filepath = os.path.join(dest_dir, f"{filename_prefix}.gif")
        
            temp_zip = os.path.join(dest_dir, f"temp_{ill_id}.zip")
            _safe_emit(self.signals.log, "   📥 Downloading Ugoira zip file...")
            if not self.download_image(zip_url, temp_zip):
                _safe_emit(self.signals.log, "   ❌ Failed to download Ugoira zip file")
                if os.path.exists(temp_zip): os.remove(temp_zip)
                return False
        
            _safe_emit(self.signals.log, "   🔄 Converting Ugoira ZIP to MP4 video...")
            pil_frames = []
            durations = []
            total_uncompressed = 0
            with zipfile.ZipFile(temp_zip, 'r') as z:
                for frame_info in frames:
                    if not self.running: break
                    frame_file = frame_info.get('file')
                    delay = frame_info.get('delay', 100)
                    try:
                        zinfo = z.getinfo(frame_file)
                        if zinfo.file_size > MAX_UGOIRA_FRAME_BYTES:
                            _safe_emit(self.signals.log, f"   ⚠️ Skipping oversized Ugoira frame ({zinfo.file_size} bytes)")
                            continue
                        total_uncompressed += zinfo.file_size
                        if total_uncompressed > MAX_UGOIRA_TOTAL_BYTES:
                            _safe_emit(self.signals.log, "   ⚠️ Ugoira archive exceeded safe memory limit, stopping frame extraction.")
                            break
                    except KeyError:
                        continue
                    with z.open(frame_file) as f:
                        img = Image.open(io.BytesIO(f.read()))
                        pil_frames.append(img.convert('RGB'))
                        durations.append(delay)
        
            if not self.running:
                if os.path.exists(temp_zip):
                    try: os.remove(temp_zip)
                    except Exception: pass
                return False

            saved_path = None
            if pil_frames:
                os.makedirs(os.path.dirname(mp4_filepath), exist_ok=True)
                if convert_frames_to_mp4(pil_frames, durations, mp4_filepath):
                    saved_path = mp4_filepath
                    _safe_emit(self.signals.log, f"   ✅ Saved Ugoira as MP4: {mp4_filename}")
                else:
                    # Fallback to GIF only if OpenCV MP4 encoding is unavailable
                    pil_frames[0].save(
                        gif_filepath,
                        save_all=True,
                        append_images=pil_frames[1:],
                        duration=durations,
                        loop=0
                    )
                    saved_path = gif_filepath
                    _safe_emit(self.signals.log, f"   ✅ Saved Ugoira as GIF (fallback): {os.path.basename(gif_filepath)}")
                self.register_file_in_db(saved_path, artist_id, artist_name)
            if os.path.exists(temp_zip):
                os.remove(temp_zip)
            return saved_path is not None
        except Exception as e:
            _safe_emit(self.signals.log, f"   ❌ Error processing Ugoira: {e}")
            if temp_zip and os.path.exists(temp_zip):
                try: os.remove(temp_zip)
                except Exception: pass
            return False

    def get_illust_pages(self, ill_id, headers, ill_body):
        page_count = ill_body.get('pageCount', 1)
        if page_count > 1:
            pages_url = f"https://www.pixiv.net/ajax/illust/{ill_id}/pages"
            try:
                r = requests.get(pages_url, headers=headers, timeout=15)
                if r.status_code == 200:
                    data = r.json()
                    if not data.get('error'):
                        pages = data.get('body', [])
                        if pages:
                            return pages
            except Exception as e:
                _safe_emit(self.signals.log, f"   ⚠️ Failed to fetch pages details for Illust {ill_id}: {e}")
    
        # Fallback to single page urls.original
        orig_url = ill_body.get('urls', {}).get('original')
        if orig_url:
            return [{"urls": {"original": orig_url}}]
        return []

    @pyqtSlot()
    def run(self):
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://www.pixiv.net/'
        }
        if self.cookie:
            clean_cookie = self.cookie.strip()
            if clean_cookie.lower().startswith("phpsessid="):
                clean_cookie = clean_cookie.split("=", 1)[1].strip()
            clean_cookie = clean_cookie.strip("; \"'")
            if clean_cookie:
                headers['Cookie'] = f"PHPSESSID={clean_cookie}"
                _safe_emit(self.signals.log, "🔑 Using PHPSESSID cookie for authenticated metadata query.")
    
        if self.mode == 'ILLUST':
            _safe_emit(self.signals.log, f"🔍 Fetching illust metadata for ID: {self.target_id}...")
            url = f"https://www.pixiv.net/ajax/illust/{self.target_id}"
            try:
                r = requests.get(url, headers=headers, timeout=10)
                if r.status_code != 200:
                    _safe_emit(self.signals.finished, False, f"HTTP Error {r.status_code} when fetching illust data.")
                    return
                data = r.json()
                if data.get('error'):
                    _safe_emit(self.signals.finished, False, f"Pixiv API Error: {data.get('message', 'Unknown error')}")
                    return
            
                body = data.get('body', {})
                artist_id = str(body.get('userId'))
                artist_name = body.get('userName', 'Unknown Artist')
                artist_folder = artist_id
            
                title = body.get('illustTitle', '')
                clean_title = "".join([c for c in title if c not in r'\/:*?"<>|']).strip()[:100]
            
                illust_type = body.get('illustType')
                dest_dir = os.path.join(self.output_dir, artist_folder)
                x_restrict = body.get('xRestrict', 0)
            
                if x_restrict > 0 and not self.cookie:
                    _safe_emit(self.signals.log, "   ⚠️ Warning: This artwork is R-18 / R-18G. Pixiv requires a valid PHPSESSID cookie to download adult content.")
            
                if illust_type == 2:
                    _safe_emit(self.signals.log, f"🎬 Illust ID {self.target_id} is a Ugoira (Animation). Processing...")
                    filename_prefix = f"{self.target_id}_ugoira - {clean_title}" if clean_title else f"{self.target_id}_ugoira"
                    success = self.process_ugoira(self.target_id, headers, dest_dir, filename_prefix, artist_id=artist_id, artist_name=artist_name)
                    fail_msg = "Ugoira download failed (R-18 works require PHPSESSID cookie)" if (x_restrict > 0 and not self.cookie) else "Ugoira download failed"
                    _safe_emit(self.signals.finished, success, f"Finished downloading Ugoira {self.target_id}" if success else fail_msg)
                    return

                pages = self.get_illust_pages(self.target_id, headers, body)
                if not pages and x_restrict > 0 and not self.cookie:
                    _safe_emit(self.signals.log, "   ❌ Cannot download R-18 image files without authentication. Please enter your PHPSESSID cookie in the download dialog.")
                    _safe_emit(self.signals.finished, False, "R-18 work requires PHPSESSID cookie to download.")
                    return
            
                total_pages = len(pages)
                _safe_emit(self.signals.log, f"🎨 Artist: {artist_name} | Found {total_pages} page(s)")
            
                downloaded = 0
                for idx, page in enumerate(pages):
                    if not self.running: break
                    orig_url = page.get('urls', {}).get('original')
                    if not orig_url: continue
                
                    orig_filename = os.path.basename(orig_url)
                    base_name, ext = os.path.splitext(orig_filename)
                    filename = f"{base_name} - {clean_title}{ext}" if clean_title else orig_filename
                    dest_file = os.path.join(dest_dir, filename)
                
                    if os.path.exists(dest_file):
                        _safe_emit(self.signals.log, f"⏭️ File already exists, skipping: {filename}")
                        downloaded += 1
                        _safe_emit(self.signals.progress, downloaded, total_pages)
                        continue
                
                    _safe_emit(self.signals.log, f"📥 Downloading page {idx+1}/{total_pages}: {filename}...")
                    if self.download_image(orig_url, dest_file):
                        downloaded += 1
                        _safe_emit(self.signals.log, f"✅ Success: {filename}")
                        self.register_file_in_db(dest_file, artist_id, artist_name)
                    _safe_emit(self.signals.progress, downloaded, total_pages)
                
                    if idx < total_pages - 1:
                        delay = random.uniform(2.0, 4.0)
                        _safe_emit(self.signals.log, f"💤 Sleeping for {delay:.1f}s (Safe Mode)...")
                        self.interruptible_sleep(delay)
            
                if not self.running:
                    _safe_emit(self.signals.finished, False, "Download cancelled by user.")
                    return

                _safe_emit(self.signals.finished, True, f"Finished downloading illust {self.target_id}")
            except Exception as e:
                _safe_emit(self.signals.finished, False, str(e))

        elif self.mode == 'ARTIST':
            _safe_emit(self.signals.log, f"🔍 Fetching artist profile for ID: {self.target_id}...")
            url = f"https://www.pixiv.net/ajax/user/{self.target_id}/profile/all"
            try:
                r = requests.get(url, headers=headers, timeout=15)
                if r.status_code != 200:
                    _safe_emit(self.signals.finished, False, f"HTTP Error {r.status_code} when fetching artist profile.")
                    return
                data = r.json()
                if data.get('error'):
                    _safe_emit(self.signals.finished, False, f"Pixiv API Error: {data.get('message', 'Unknown error')}")
                    return
            
                body = data.get('body', {})
                illusts = body.get('illusts', {}) or {}
                manga = body.get('manga', {}) or {}
            
                # Sort descending (newest works first)
                illust_ids = sorted(list(illusts.keys()) + list(manga.keys()), key=int, reverse=True)
                total_illusts = len(illust_ids)
            
                if total_illusts == 0:
                    _safe_emit(self.signals.finished, True, "Artist has no illustrations or manga.")
                    return
            
                _safe_emit(self.signals.log, f"🎨 Found {total_illusts} works for Artist ID: {self.target_id}")
            
                artist_name = "Artist " + self.target_id
                first_url = f"https://www.pixiv.net/ajax/illust/{illust_ids[0]}"
                r_first = requests.get(first_url, headers=headers, timeout=15)
                if r_first.status_code == 200:
                    first_data = r_first.json()
                    if not first_data.get('error'):
                        artist_name = first_data.get('body', {}).get('userName', artist_name)
            
                artist_folder = self.target_id
                dest_dir = os.path.join(self.output_dir, artist_folder)
                _safe_emit(self.signals.log, f"📁 Destination Folder: {dest_dir}")
            
                # Scan destination folder first to get already downloaded IDs
                downloaded_ids = set()
                if os.path.exists(dest_dir):
                    for fname in os.listdir(dest_dir):
                        if '_' in fname:
                            parts = fname.split('_')
                            if parts[0].isdigit():
                                downloaded_ids.add(parts[0])
            
                # Filter out already downloaded ones BEFORE starting
                new_illust_ids = [i for i in illust_ids if i not in downloaded_ids]
                total_new = len(new_illust_ids)
            
                _safe_emit(self.signals.log, f"🎨 Found {total_illusts} works total. {total_illusts - total_new} already exist in storage. {total_new} new works to download.")
            
                if total_new == 0:
                    _safe_emit(self.signals.finished, True, "All illustrations of this artist are already downloaded.")
                    return
            
                downloaded_works = 0
            
                for idx, ill_id in enumerate(new_illust_ids):
                    if not self.running: break
                
                    _safe_emit(self.signals.log, f"🖼️ [{idx+1}/{total_new}] Fetching metadata for Illust ID {ill_id}...")
                
                    illust_url = f"https://www.pixiv.net/ajax/illust/{ill_id}"
                    try:
                        r_ill = requests.get(illust_url, headers=headers, timeout=15)
                        if r_ill.status_code == 200:
                            ill_data = r_ill.json()
                            if not ill_data.get('error'):
                                ill_body = ill_data.get('body', {})
                                illust_type = ill_body.get('illustType')
                                title = ill_body.get('illustTitle', '')
                                clean_title = "".join([c for c in title if c not in r'\/:*?"<>|']).strip()[:100]
                                x_restrict = ill_body.get('xRestrict', 0)
                            
                                if illust_type == 2:
                                    filename_prefix = f"{ill_id}_ugoira - {clean_title}" if clean_title else f"{ill_id}_ugoira"
                                    mp4_filepath = os.path.join(dest_dir, f"{filename_prefix}.mp4")
                                    gif_filepath = os.path.join(dest_dir, f"{filename_prefix}.gif")
                                    if os.path.exists(mp4_filepath) or os.path.exists(gif_filepath):
                                        _safe_emit(self.signals.log, f"   ⏭️ Skipping existing Ugoira: {filename_prefix}.mp4")
                                    else:
                                        _safe_emit(self.signals.log, f"   🎬 Illust ID {ill_id} is a Ugoira (Animation). Processing...")
                                        if self.process_ugoira(ill_id, headers, dest_dir, filename_prefix, artist_id=self.target_id, artist_name=artist_name):
                                            downloaded_works += 1
                                
                                    if self.max_limit and downloaded_works >= self.max_limit:
                                        _safe_emit(self.signals.log, f"🛑 Reached download limit of {self.max_limit} works. Stopping.")
                                        _safe_emit(self.signals.finished, True, f"Reached download limit of {self.max_limit} works.")
                                        return
                                else:
                                    pages = self.get_illust_pages(ill_id, headers, ill_body)
                                    if not pages and x_restrict > 0 and not self.cookie:
                                        _safe_emit(self.signals.log, f"   ⚠️ Skipping R-18 Illust ID {ill_id} (Requires PHPSESSID cookie)")
                                        continue
                                
                                    has_downloaded_any_page = False
                                    for p_idx, page in enumerate(pages):
                                        if not self.running: break
                                        orig_url = page.get('urls', {}).get('original')
                                        if not orig_url: continue
                                    
                                        orig_filename = os.path.basename(orig_url)
                                        base_name, ext = os.path.splitext(orig_filename)
                                        filename = f"{base_name} - {clean_title}{ext}" if clean_title else orig_filename
                                        dest_file = os.path.join(dest_dir, filename)
                                    
                                        if os.path.exists(dest_file):
                                             _safe_emit(self.signals.log, f"   ⏭️ Skipping existing: {filename}")
                                             continue
                                        
                                        _safe_emit(self.signals.log, f"   📥 Downloading page {p_idx+1}: {filename}...")
                                        if self.download_image(orig_url, dest_file):
                                            _safe_emit(self.signals.log, f"   ✅ Success: {filename}")
                                            has_downloaded_any_page = True
                                            self.register_file_in_db(dest_file, self.target_id, artist_name)
                                    
                                        delay = random.uniform(2.0, 4.0)
                                        self.interruptible_sleep(delay)
                                
                                    if has_downloaded_any_page:
                                        downloaded_works += 1
                                    
                                    if self.max_limit and downloaded_works >= self.max_limit:
                                        _safe_emit(self.signals.log, f"🛑 Reached download limit of {self.max_limit} works. Stopping.")
                                        _safe_emit(self.signals.finished, True, f"Reached download limit of {self.max_limit} works.")
                                        return
                    except Exception as e:
                        _safe_emit(self.signals.log, f"   ❌ Error fetching details for Illust {ill_id}: {e}")
                
                    _safe_emit(self.signals.progress, idx + 1, total_new)
                
                    if idx < total_new - 1:
                        if (idx + 1) % 10 == 0:
                            session_delay = random.uniform(15.0, 30.0)
                            _safe_emit(self.signals.log, f"☕ [SESSION BREAK] Sleeping {session_delay:.1f}s to mimic human breaks...")
                            self.interruptible_sleep(session_delay)
                        else:
                            delay = random.uniform(3.0, 5.0)
                            _safe_emit(self.signals.log, f"💤 Sleeping for {delay:.1f}s...")
                            self.interruptible_sleep(delay)
            
                if not self.running:
                    _safe_emit(self.signals.finished, False, "Download cancelled by user.")
                    return

                _safe_emit(self.signals.finished, True, f"Finished downloading gallery for Artist {self.target_id}")
            except Exception as e:
                _safe_emit(self.signals.finished, False, str(e))

        elif self.mode == 'BOOKMARK':
            limit = 48
            start_p = self.start_page if self.start_page is not None else 1
            offset = (start_p - 1) * limit
            illust_ids = []
        
            page_str = f"Page {start_p}"
            if self.end_page is not None:
                page_str += f" to {self.end_page}"
            else:
                page_str += " onwards"
            _safe_emit(self.signals.log, f"🔍 Fetching bookmarks list for User: {self.target_id} ({self.bookmark_rest}) | Range: {page_str}...")
        
            try:
                while self.running:
                    current_page = (offset // limit) + 1
                    if self.end_page is not None and current_page > self.end_page:
                        break
                    
                    url = f"https://www.pixiv.net/ajax/user/{self.target_id}/illusts/bookmarks?tag=&offset={offset}&limit={limit}&rest={self.bookmark_rest}"
                    r = requests.get(url, headers=headers, timeout=15)
                    if r.status_code != 200:
                        _safe_emit(self.signals.finished, False, f"HTTP Error {r.status_code} while fetching bookmark offset {offset}")
                        return
                
                    data = r.json()
                    if data.get('error'):
                        _safe_emit(self.signals.finished, False, f"Pixiv API Error: {data.get('message', 'Unknown error')}")
                        return
                
                    body = data.get('body', {})
                    works = body.get('works', [])
                    if not works:
                        break  # No more bookmarks
                
                    for work in works:
                        ill_id = str(work.get('id'))
                        if ill_id:
                            illust_ids.append(ill_id)
                
                    offset += limit
                    _safe_emit(self.signals.log, f"📋 Found {len(illust_ids)} bookmarks so far (Current: Page {current_page})...")
                    self.interruptible_sleep(1.0)  # Small metadata fetch safety delay
            
                if not self.running:
                    _safe_emit(self.signals.finished, False, "Download cancelled by user.")
                    return

                total_illusts = len(illust_ids)
                if total_illusts == 0:
                    _safe_emit(self.signals.finished, True, "No bookmarked illustrations found in this range.")
                    return
            
                _safe_emit(self.signals.log, f"🚀 Total bookmarked illustrations collected: {total_illusts}")
            
                # Scan output folders (which are artist folders) to get already downloaded IDs
                downloaded_ids = set()
                if os.path.exists(self.output_dir):
                    for entry in os.listdir(self.output_dir):
                        sub_path = os.path.join(self.output_dir, entry)
                        if os.path.isdir(sub_path) and entry.isdigit():
                            for fname in os.listdir(sub_path):
                                if '_' in fname:
                                    parts = fname.split('_')
                                    if parts[0].isdigit():
                                        downloaded_ids.add(parts[0])
            
                # Filter out already downloaded ones BEFORE starting
                new_illust_ids = [i for i in illust_ids if i not in downloaded_ids]
                total_new = len(new_illust_ids)
            
                _safe_emit(self.signals.log, f"🚀 Collected {total_illusts} bookmarks total. {total_illusts - total_new} already exist in storage. {total_new} new works to download.")
            
                if total_new == 0:
                    _safe_emit(self.signals.finished, True, "All bookmarked illustrations are already downloaded.")
                    return
            
                downloaded_works = 0
            
                for idx, ill_id in enumerate(new_illust_ids):
                    if not self.running: break
                
                    _safe_emit(self.signals.log, f"🖼️ [{idx+1}/{total_new}] Fetching metadata for Bookmarked Illust ID {ill_id}...")
                
                    illust_url = f"https://www.pixiv.net/ajax/illust/{ill_id}"
                    try:
                        r_ill = requests.get(illust_url, headers=headers, timeout=15)
                        if r_ill.status_code == 200:
                            ill_data = r_ill.json()
                            if not ill_data.get('error'):
                                ill_body = ill_data.get('body', {})
                                illust_type = ill_body.get('illustType')
                                artist_id = str(ill_body.get('userId'))
                                artist_name = ill_body.get('userName', 'Unknown Artist')
                                artist_dir = os.path.join(self.output_dir, artist_id)
                            
                                title = ill_body.get('illustTitle', '')
                                clean_title = "".join([c for c in title if c not in r'\/:*?"<>|']).strip()[:100]
                                x_restrict = ill_body.get('xRestrict', 0)
                            
                                if illust_type == 2:
                                    filename_prefix = f"{ill_id}_ugoira - {clean_title}" if clean_title else f"{ill_id}_ugoira"
                                    mp4_filepath = os.path.join(artist_dir, f"{filename_prefix}.mp4")
                                    gif_filepath = os.path.join(artist_dir, f"{filename_prefix}.gif")
                                    if os.path.exists(mp4_filepath) or os.path.exists(gif_filepath):
                                        _safe_emit(self.signals.log, f"   ⏭️ Skipping existing Ugoira: {filename_prefix}.mp4")
                                    else:
                                        _safe_emit(self.signals.log, f"   🎬 Bookmarked Illust ID {ill_id} is a Ugoira (Animation). Processing...")
                                        if self.process_ugoira(ill_id, headers, artist_dir, filename_prefix, artist_id=artist_id, artist_name=artist_name):
                                            downloaded_works += 1
                                
                                    if self.max_limit and downloaded_works >= self.max_limit:
                                        _safe_emit(self.signals.log, f"🛑 Reached download limit of {self.max_limit} works. Stopping.")
                                        _safe_emit(self.signals.finished, True, f"Reached download limit of {self.max_limit} works.")
                                        return
                                else:
                                    pages = self.get_illust_pages(ill_id, headers, ill_body)
                                    if not pages and x_restrict > 0 and not self.cookie:
                                        _safe_emit(self.signals.log, f"   ⚠️ Skipping R-18 Bookmarked Illust ID {ill_id} (Requires PHPSESSID cookie)")
                                        continue
                                
                                    has_downloaded_any_page = False
                                    for p_idx, page in enumerate(pages):
                                        if not self.running: break
                                        orig_url = page.get('urls', {}).get('original')
                                        if not orig_url: continue
                                    
                                        orig_filename = os.path.basename(orig_url)
                                        base_name, ext = os.path.splitext(orig_filename)
                                        filename = f"{base_name} - {clean_title}{ext}" if clean_title else orig_filename
                                        dest_file = os.path.join(artist_dir, filename)
                                    
                                        if os.path.exists(dest_file):
                                            _safe_emit(self.signals.log, f"   ⏭️ Skipping existing: {filename}")
                                            continue
                                        
                                        _safe_emit(self.signals.log, f"   📥 Downloading page {p_idx+1}: {filename}...")
                                        if self.download_image(orig_url, dest_file):
                                            _safe_emit(self.signals.log, f"   ✅ Success: {filename}")
                                            has_downloaded_any_page = True
                                            self.register_file_in_db(dest_file, artist_id, artist_name)
                                    
                                        delay = random.uniform(2.0, 4.0)
                                        self.interruptible_sleep(delay)
                                
                                    if has_downloaded_any_page:
                                        downloaded_works += 1
                                    
                                    if self.max_limit and downloaded_works >= self.max_limit:
                                        _safe_emit(self.signals.log, f"🛑 Reached download limit of {self.max_limit} works. Stopping.")
                                        _safe_emit(self.signals.finished, True, f"Reached download limit of {self.max_limit} works.")
                                        return
                    except Exception as e:
                        _safe_emit(self.signals.log, f"   ❌ Error fetching details for Illust {ill_id}: {e}")
                
                    _safe_emit(self.signals.progress, idx + 1, total_new)
                
                    if idx < total_new - 1:
                        if (idx + 1) % 10 == 0:
                            session_delay = random.uniform(15.0, 30.0)
                            _safe_emit(self.signals.log, f"☕ [SESSION BREAK] Sleeping {session_delay:.1f}s to mimic human breaks...")
                            self.interruptible_sleep(session_delay)
                        else:
                            delay = random.uniform(3.0, 5.0)
                            _safe_emit(self.signals.log, f"💤 Sleeping for {delay:.1f}s...")
                            self.interruptible_sleep(delay)
            
                if not self.running:
                    _safe_emit(self.signals.finished, False, "Download cancelled by user.")
                    return

                _safe_emit(self.signals.finished, True, f"Finished downloading bookmarks for User {self.target_id}")
            except Exception as e:
                _safe_emit(self.signals.finished, False, str(e))

class DatabaseOptimizerWorker(QRunnable):
    def __init__(self, db_file):
        super().__init__()
        self.db_file = db_file
        self.signals = WorkerSignals()

    @pyqtSlot()
    def run(self):
        try:
            from database import cleanup_orphaned_records
            close_all_thread_local_conns()
            cleanup_orphaned_records(self.db_file)
            conn = sqlite3.connect(self.db_file, timeout=60.0)
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()
            _safe_emit(self.signals.api_log, "SUCCESS")
        except Exception as e:
            _safe_emit(self.signals.api_log, f"ERROR: {e}")

class AiTagWorkerSignals(QObject):
    progress = pyqtSignal(int, int, str, float, float)  # current, total, filename, fps, eta_seconds
    item_tagged = pyqtSignal(str, list, list, list, str) # path, chars, series, general, rating
    batch_saved = pyqtSignal(int)                        # total items committed to db
    log = pyqtSignal(str)                                # text log message
    finished = pyqtSignal(int, int, float)               # total_tagged, errors, elapsed_seconds
    paused = pyqtSignal(bool)
    model_loading = pyqtSignal(str)


class AiTaggingWorker(QRunnable):
    """
    High-Performance Multi-threaded Asynchronous AI Tagging Worker (DirectML / CUDA / NPU Accelerated):
    - Producer-Consumer Architecture: Parallel CPU prefetchers decode images and scale tensors in advance
    - DirectML/CUDA/NPU consumer executes back-to-back inference with zero disk I/O idle time
    - Chunked SQLite commits for minimal write lock overhead
    """
    def __init__(self, root_path=None, db_file=DB_FILE, use_gpu=True,
                 use_fp16=True,
                 model_key="wd-v1-4-convnext-v2",
                 storage_mode="auto",
                 char_threshold=DEFAULT_CHARACTER_THRESHOLD,
                 gen_threshold=DEFAULT_GENERAL_THRESHOLD,
                 rescan_existing=False, batch_size=50, throttle=0.0,
                 inference_batch=16):
        super().__init__()
        self.root_path = root_path
        self.db_file = db_file
        self.use_gpu = use_gpu
        self.use_fp16 = use_fp16
        self.model_key = model_key or "wd-v1-4-convnext-v2"
        self.storage_mode = storage_mode.lower() if storage_mode else "auto"
        self.char_threshold = char_threshold
        self.gen_threshold = gen_threshold
        self.rescan_existing = rescan_existing
        self.batch_size = batch_size
        self.throttle = throttle
        self.inference_batch = max(1, int(inference_batch or 16))
        self.signals = AiTagWorkerSignals()
        self.running = True
        self.is_paused = False
        self._pause_cond = threading.Condition()

    def pause(self):
        with self._pause_cond:
            self.is_paused = True
            _safe_emit(self.signals.paused, True)

    def resume(self):
        with self._pause_cond:
            self.is_paused = False
            self._pause_cond.notify_all()
            _safe_emit(self.signals.paused, False)

    def stop(self):
        self.running = False
        with self._pause_cond:
            self.is_paused = False
            self._pause_cond.notify_all()

    @pyqtSlot()
    def run(self):
        from tagger import WD14Tagger, get_model_paths
        if not WD14Tagger.is_model_installed(self.model_key):
            _safe_emit(self.signals.log, "⚠️ ไม่พบไฟล์โมเดล AI กรุณาดาวน์โหลดโมเดลก่อนเริ่มสแกน")
            _safe_emit(self.signals.finished, 0, 0, 0.0)
            return

        model_meta = get_model_paths(self.model_key)["meta"]
        precision_label = "FP16 (Turbo)" if (self.use_gpu and self.use_fp16) else "FP32 Standard"
        _safe_emit(self.signals.model_loading, f"กำลังโหลด {model_meta['short_name']} ({precision_label}) เข้า GPU...")
        try:
            tagger = WD14Tagger(model_key=self.model_key, use_gpu=self.use_gpu, use_fp16=self.use_fp16 if self.use_gpu else False)
            tagger.load_model()
            _safe_emit(self.signals.log, f"✅ โหลดโมเดล {model_meta['short_name']} สำเร็จ (ทำงานบน: {tagger.active_provider} | {precision_label})")
        except Exception as e:
            _safe_emit(self.signals.log, f"❌ โหลดโมเดลไม่สำเร็จ: {e}")
            _safe_emit(self.signals.finished, 0, 1, 0.0)
            return

        conn = get_thread_local_conn(self.db_file)
        cursor = conn.cursor()

        _safe_emit(self.signals.log, "🔍 กำลังค้นหาไฟล์ที่ต้องสแกนแท็ก (Direct Disk Walk)...")
        target_files = []
        try:
            # 1. Determine search folder on disk
            search_folder = self.root_path
            if not search_folder and self.db_file == DB_FILE:
                try:
                    search_folder = load_app_config().get('last_folder')
                except Exception:
                    pass

            # 2. Fast direct disk walk to find all physical media files
            disk_files = []
            if search_folder and os.path.exists(search_folder):
                for dirpath, dirnames, filenames in os.walk(search_folder):
                    for fn in filenames:
                        if not fn.startswith('.') and os.path.splitext(fn)[1].lower() in ALL_MEDIA_EXT:
                            disk_files.append(os.path.join(dirpath, fn))

            # Fallback to file_index table if disk walk found nothing
            if not disk_files:
                if self.root_path:
                    search_prefix = self.root_path + os.sep
                    search_prefix_upper = self.root_path + os.sep + "\uffff"
                    cursor.execute("SELECT path FROM file_index WHERE path >= ? AND path < ? ORDER BY mtime DESC", (search_prefix, search_prefix_upper))
                else:
                    cursor.execute("SELECT path FROM file_index ORDER BY mtime DESC")
                disk_files = [row[0] for row in cursor.fetchall()]

            # 3. Filter target files
            if self.rescan_existing:
                target_files = disk_files
                _safe_emit(self.signals.log, f"🔄 โหมดสแกนใหม่ทั้งหมด (Full Re-scan): เตรียมสแกนทับทั้งหมด {len(target_files):,} ไฟล์...")
            else:
                if self.root_path:
                    search_prefix = self.root_path + os.sep
                    search_prefix_upper = self.root_path + os.sep + "\uffff"
                    cursor.execute("SELECT DISTINCT path FROM file_tags WHERE path >= ? AND path < ? AND tag_name != '__none__' AND category NOT IN (10, 11, 99)", (search_prefix, search_prefix_upper))
                else:
                    cursor.execute("SELECT DISTINCT path FROM file_tags WHERE tag_name != '__none__' AND category NOT IN (10, 11, 99)")
                already_tagged_set = {row[0] for row in cursor.fetchall()}
                target_files = [f for f in disk_files if f not in already_tagged_set]
                skipped_count = len(disk_files) - len(target_files)
                _safe_emit(
                    self.signals.log,
                    f"⚡ โหมดด่วน (Quick Mode): พบไฟล์ใหม่ที่ยังไม่มีแท็ก {len(target_files):,} ไฟล์ "
                    f"(ข้าม {skipped_count:,} ไฟล์ที่มีแท็กแล้ว จากทั้งหมด {len(disk_files):,} ไฟล์)..."
                )

        except Exception as e:
            _safe_emit(self.signals.log, f"❌ เกิดข้อผิดพลาดในการค้นหาไฟล์: {e}")
            _safe_emit(self.signals.finished, 0, 1, 0.0)
            return

        total_files = len(target_files)
        if total_files == 0:
            _safe_emit(self.signals.log, "✨ ไม่มีไฟล์ใหม่ที่ต้องสแกนแท็ก (ไฟล์ทั้งหมดมีแท็กครบแล้ว)")
            _safe_emit(self.signals.finished, 0, 0, 0.0)
            return

        # Storage profile determination & Auto-detect
        sample_path = search_folder or (target_files[0] if target_files else self.root_path)
        detected_media = detect_drive_media_type(sample_path) if sample_path else "UNKNOWN"

        if self.storage_mode == "hdd":
            is_hdd = True
            profile_label = "HDD จานหมุน (Low I/O)"
        elif self.storage_mode == "ssd":
            is_hdd = False
            profile_label = "SSD/NVMe (High Concurrency)"
        else:  # auto
            is_hdd = (detected_media == "HDD")
            profile_label = f"Auto ({'ตรวจพบ HDD จานหมุน' if is_hdd else ('ตรวจพบ SSD' if detected_media == 'SSD' else 'ความเร็วสูง')})"

        if is_hdd:
            # Files in same folder are physically adjacent on HDD → reduces random I/O dramatically
            target_files.sort(key=lambda p: (os.path.dirname(p), p))
            num_prefetchers = min(4, max(2, (os.cpu_count() or 4) // 2))
        else:
            num_prefetchers = min(16, max(8, (os.cpu_count() or 8) * 2))

        INFERENCE_BATCH = self.inference_batch

        _safe_emit(
            self.signals.log,
            f"🚀 เริ่มการสแกน Turbo Batch Pipeline ทั้งหมด {total_files:,} ไฟล์ "
            f"(GPU Batch={INFERENCE_BATCH} | Storage: {profile_label} [{num_prefetchers} I/O Threads])..."
        )

        start_time = time.time()
        processed_count = 0
        error_count = 0
        batch_tags = []

        # Determine model input size, dtype, and NCHW flag dynamically
        target_size = 448
        is_nchw = False
        if len(tagger.input_shape) == 4:
            if tagger.input_shape[1] == 3:
                is_nchw = True
                if isinstance(tagger.input_shape[2], int) and tagger.input_shape[2] > 3:
                    target_size = tagger.input_shape[2]
            elif isinstance(tagger.input_shape[1], int) and tagger.input_shape[1] > 3:
                target_size = tagger.input_shape[1]
        target_dtype = tagger.input_dtype

        # Multi-threaded Producer-Consumer Prefetch Pipeline
        prefetch_queue = queue.Queue(maxsize=INFERENCE_BATCH * 8)  # Deep buffer: always have next batch ready
        idx_queue = queue.Queue()
        for idx, fp in enumerate(target_files):
            idx_queue.put((idx, fp))

        def prefetch_worker():
            while self.running and not idx_queue.empty():
                with self._pause_cond:
                    while self.is_paused and self.running:
                        self._pause_cond.wait(0.2)
                if not self.running:
                    break
                try:
                    i, path = idx_queue.get_nowait()
                except queue.Empty:
                    break

                tensor = None
                try:
                    if not os.path.exists(path):
                        pass  # tensor stays None
                    elif _CV2_AVAILABLE:
                        ext = os.path.splitext(path)[1].lower()
                        if ext in ('.jpg', '.jpeg', '.png', '.webp', '.bmp'):
                            tensor = decode_image_cv2_for_wd14(
                                path,
                                target_size=target_size,
                                target_dtype=target_dtype,
                                nchw=is_nchw
                            )
                            if tensor is None:
                                tensor = tagger.prepare_media_tensor(path)
                        else:
                            # PIL fallback for GIF / ZIP / MP4 / other formats
                            tensor = tagger.prepare_media_tensor(path)
                    else:
                        # PIL fallback when cv2 not available
                        tensor = tagger.prepare_media_tensor(path)
                except Exception:
                    tensor = None

                while self.running:
                    try:
                        prefetch_queue.put((i, path, tensor), timeout=0.1)
                        idx_queue.task_done()
                        break
                    except queue.Full:
                        continue

        # Spawn parallel prefetch threads
        prefetch_threads = []
        for _ in range(num_prefetchers):
            t = threading.Thread(target=prefetch_worker, daemon=True)
            t.start()
            prefetch_threads.append(t)

        items_processed = 0

        while items_processed < total_files and self.running:
            with self._pause_cond:
                while self.is_paused and self.running:
                    self._pause_cond.wait(0.2)
            if not self.running:
                _safe_emit(self.signals.log, "🛑 การสแกนถูกหยุดโดยผู้ใช้")
                break

            # Collect up to INFERENCE_BATCH items from prefetch queue
            current_batch = []
            target_batch_size = min(INFERENCE_BATCH, total_files - items_processed)

            while len(current_batch) < target_batch_size and self.running:
                try:
                    item = prefetch_queue.get(timeout=0.2)
                    prefetch_queue.task_done()
                    current_batch.append(item)
                except queue.Empty:
                    # If all prefetchers are done and queue is empty, stop waiting
                    if not any(t.is_alive() for t in prefetch_threads):
                        break

            if not current_batch:
                continue

            t0 = time.time()

            # Separate valid tensors from failed/missing files
            valid_items = [(fp, t) for _, fp, t in current_batch if t is not None]
            per_file_results = {}  # file_path -> result dict

            if valid_items:
                try:
                    fps_list, tensors = zip(*valid_items)
                    stacked = np.concatenate(tensors, axis=0)
                    batch_results = tagger.predict_tensor_batch(
                        stacked,
                        char_threshold=self.char_threshold,
                        gen_threshold=self.gen_threshold
                    )
                    for fp, res in zip(fps_list, batch_results):
                        per_file_results[fp] = res
                except Exception as e:
                    logging.error(f"Batch inference error: {e}")
                    for fp, _ in valid_items:
                        per_file_results[fp] = None

            # Process each item's result and emit progress
            for _, file_path, tensor in current_batch:
                filename = os.path.basename(file_path)
                res = per_file_results.get(file_path)

                if tensor is None:
                    error_count += 1
                elif res and res.get("all_tags"):
                    for tag_name, category, conf in res["all_tags"]:
                        batch_tags.append((file_path, tag_name, category, conf))
                    _safe_emit(
                        self.signals.item_tagged,
                        file_path,
                        res.get("characters", []),
                        res.get("series", []),
                        res.get("general", [])[:8],
                        res.get("rating", "general")
                    )
                    processed_count += 1
                else:
                    if res is None and file_path in per_file_results:
                        error_count += 1
                    else:
                        processed_count += 1

                items_processed += 1

                wall_elapsed = time.time() - start_time
                fps = items_processed / wall_elapsed if wall_elapsed > 0 else 0.0
                eta_sec = (total_files - items_processed) / fps if fps > 0 else 0.0
                _safe_emit(self.signals.progress, items_processed, total_files, filename, fps, eta_sec)

            # Batched commit to SQLite
            if len(batch_tags) >= self.batch_size * 5 or items_processed >= total_files:
                try:
                    cursor.executemany("""
                        INSERT OR REPLACE INTO file_tags (path, tag_name, category, confidence)
                        VALUES (?, ?, ?, ?)
                    """, batch_tags)
                    conn.commit()
                    batch_tags = []
                    _safe_emit(self.signals.batch_saved, processed_count)
                except Exception as e:
                    logging.error(f"Error committing tags batch: {e}")

        # Signal prefetch workers to finish
        self.running = False
        while not idx_queue.empty():
            try: idx_queue.get_nowait(); idx_queue.task_done()
            except: break

        # Commit any leftover batch
        if batch_tags:
            try:
                cursor.executemany("""
                    INSERT OR REPLACE INTO file_tags (path, tag_name, category, confidence)
                    VALUES (?, ?, ?, ?)
                """, batch_tags)
                conn.commit()
            except Exception as e:
                logging.error(f"Error committing final tags batch: {e}")

        total_elapsed = time.time() - start_time
        _safe_emit(self.signals.log, f"🎉 สแกนเสร็จสิ้น: สำเร็จ {processed_count:,} ไฟล์, ข้อผิดพลาด {error_count} (เวลา {total_elapsed:.1f} วินาที)")
        _safe_emit(self.signals.finished, processed_count, error_count, total_elapsed)


class DuplicateScannerSignals(QObject):
    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(list)


class DuplicateScannerWorker(QRunnable):
    """
    Background Perceptual Hash (dHash) Duplicate & Similar Artwork Scanner.
    Caches 64-bit dHash values in `file_index.dhash` and groups visually identical/similar files.
    """
    def __init__(self, root_path=None, db_file=DB_FILE, max_hamming_distance=4):
        super().__init__()
        self.root_path = root_path
        self.db_file = db_file
        self.max_hamming_distance = max(0, int(max_hamming_distance))
        self.signals = DuplicateScannerSignals()
        self.running = True

    def stop(self):
        self.running = False

    @pyqtSlot()
    def run(self):
        conn = get_thread_local_conn(self.db_file)
        cursor = conn.cursor()
        files_info = []
        try:
            if self.root_path:
                prefix = self.root_path + os.sep
                prefix_upper = prefix + "\uffff"
                cursor.execute(
                    "SELECT path, size, mtime, dhash FROM file_index WHERE path >= ? AND path < ?",
                    (prefix, prefix_upper)
                )
            else:
                cursor.execute("SELECT path, size, mtime, dhash FROM file_index")
            files_info = cursor.fetchall()
        except Exception as e:
            logging.error(f"Error querying file_index for duplicate scan: {e}")

        if not files_info and self.root_path and os.path.exists(self.root_path):
            for dp, _, fns in os.walk(self.root_path):
                for fn in fns:
                    if not fn.startswith('.') and os.path.splitext(fn)[1].lower() in ALL_MEDIA_EXT:
                        fp = os.path.join(dp, fn)
                        try:
                            st = os.stat(fp)
                            files_info.append((fp, st.st_size, st.st_mtime, None))
                        except OSError:
                            pass

        total = len(files_info)
        if total == 0:
            _safe_emit(self.signals.finished, [])
            return

        hashed_items = []
        db_updates = []

        for idx, (path, size, mtime, dhash) in enumerate(files_info):
            if not self.running:
                break
            if not os.path.exists(path):
                continue
            if not dhash:
                pil_img, _ = load_media_thumbnail(path)
                if pil_img:
                    dhash = compute_dhash(pil_img)
                    if dhash:
                        db_updates.append((dhash, path))
            if dhash:
                hashed_items.append((path, size or 0, mtime or 0.0, dhash))

            if len(db_updates) >= 200:
                try:
                    cursor.executemany("UPDATE file_index SET dhash = ? WHERE path = ?", db_updates)
                    conn.commit()
                    db_updates = []
                except Exception:
                    pass

            if (idx + 1) % 25 == 0 or idx + 1 == total:
                _safe_emit(self.signals.progress, idx + 1, total, os.path.basename(path))

        if db_updates:
            try:
                cursor.executemany("UPDATE file_index SET dhash = ? WHERE path = ?", db_updates)
                conn.commit()
            except Exception:
                pass

        if not self.running:
            _safe_emit(self.signals.finished, [])
            return

        # Group duplicates
        groups = []
        if self.max_hamming_distance == 0:
            by_hash = {}
            for item in hashed_items:
                by_hash.setdefault(item[3], []).append(item)
            for h, items in by_hash.items():
                if len(items) > 1:
                    items.sort(key=lambda x: x[1], reverse=True)
                    groups.append(items)
        else:
            # Bucket first by exact hash, then compare unique hashes
            by_hash = {}
            for item in hashed_items:
                by_hash.setdefault(item[3], []).append(item)
            unique_hashes = list(by_hash.keys())
            visited = set()
            for i, h1 in enumerate(unique_hashes):
                if not self.running:
                    break
                if h1 in visited:
                    continue
                cluster = list(by_hash[h1])
                visited.add(h1)
                for j in range(i + 1, len(unique_hashes)):
                    h2 = unique_hashes[j]
                    if h2 in visited:
                        continue
                    if hamming_distance_hex(h1, h2) <= self.max_hamming_distance:
                        cluster.extend(by_hash[h2])
                        visited.add(h2)
                if len(cluster) > 1:
                    cluster.sort(key=lambda x: x[1], reverse=True)
                    groups.append(cluster)

        groups.sort(key=lambda g: len(g), reverse=True)
        _safe_emit(self.signals.finished, groups)


class DatasetExportSignals(QObject):
    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(int, str)


class DatasetExportWorker(QRunnable):
    """
    Exports selected/filtered artworks alongside `.txt` caption files for Kohya_ss / LoRA / Colab training.
    """
    def __init__(self, file_paths, output_dir, db_file=DB_FILE,
                 min_confidence=0.35, format_spaces=True, prefix_tags="",
                 copy_images=True, include_ratings=False):
        super().__init__()
        self.file_paths = list(file_paths or [])
        self.output_dir = output_dir
        self.db_file = db_file
        self.min_confidence = float(min_confidence)
        self.format_spaces = bool(format_spaces)
        self.prefix_tags = (prefix_tags or "").strip()
        self.copy_images = bool(copy_images)
        self.include_ratings = bool(include_ratings)
        self.signals = DatasetExportSignals()
        self.running = True

    def stop(self):
        self.running = False

    @pyqtSlot()
    def run(self):
        os.makedirs(self.output_dir, exist_ok=True)
        conn = get_thread_local_conn(self.db_file)
        cursor = conn.cursor()

        exported = 0
        total = len(self.file_paths)
        used_names = set()

        for idx, src_path in enumerate(self.file_paths):
            if not self.running:
                break
            if not os.path.exists(src_path):
                continue

            base_name = os.path.basename(src_path)
            stem, ext = os.path.splitext(base_name)
            candidate_stem = stem
            counter = 1
            while candidate_stem.lower() in used_names:
                candidate_stem = f"{stem}_{counter}"
                counter += 1
            used_names.add(candidate_stem.lower())

            try:
                if self.copy_images:
                    dst_img_path = os.path.join(self.output_dir, f"{candidate_stem}{ext}")
                    shutil.copy2(src_path, dst_img_path)
                    txt_path = os.path.join(self.output_dir, f"{candidate_stem}.txt")
                else:
                    txt_path = os.path.splitext(src_path)[0] + ".txt"

                cursor.execute(
                    "SELECT tag_name, category, confidence FROM file_tags WHERE path = ? ORDER BY category DESC, confidence DESC",
                    (src_path,)
                )
                rows = cursor.fetchall()
                tags_out = []
                if self.prefix_tags:
                    for pt in self.prefix_tags.split(","):
                        if pt.strip():
                            tags_out.append(pt.strip())

                for tag_name, cat, conf in rows:
                    if tag_name in ('__none__', 'favorite') or cat in (10, 99):
                        continue
                    if cat in (5, 9) and not self.include_ratings:
                        continue
                    if conf < self.min_confidence and cat not in (8, 11):
                        continue
                    formatted = tag_name.replace('_', ' ') if self.format_spaces else tag_name
                    if formatted not in tags_out:
                        tags_out.append(formatted)

                with open(txt_path, "w", encoding="utf-8") as f:
                    f.write(", ".join(tags_out))

                exported += 1
            except Exception as e:
                logging.error(f"Error exporting dataset item {src_path}: {e}")

            _safe_emit(self.signals.progress, idx + 1, total, base_name)

        _safe_emit(self.signals.finished, exported, self.output_dir)
