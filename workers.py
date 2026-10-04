import os
import sqlite3
import logging
import time
import random
import io
import queue
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
    DEFAULT_CHARACTER_THRESHOLD, DEFAULT_GENERAL_THRESHOLD
)
from utils import load_media_thumbnail, overlay_avatar_on_grid, detect_drive_media_type

import threading
_thread_local = threading.local()

def get_thread_local_conn(db_file, timeout=30.0):
    if not hasattr(_thread_local, "conn") or _thread_local.conn is None or getattr(_thread_local, "db_file", None) != db_file:
        if hasattr(_thread_local, "conn") and _thread_local.conn is not None:
            try: _thread_local.conn.close()
            except: pass
        conn = sqlite3.connect(db_file, timeout=timeout)
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
    else:
        try: _thread_local.conn.rollback()
        except: pass
    return _thread_local.conn

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
        self.signals.api_log.emit("🔍 กำลังตรวจสอบข้อมูลโฟลเดอร์จาก Database...")
        
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
                        self.signals.scan_batch.emit(self.gen_id, batch) 
                        batch = []

                if batch: 
                    self.signals.scan_batch.emit(self.gen_id, batch)
                    batch = []

            if not self.running: return

            # Phase 2: ตรวจสอบโฟลเดอร์จริงบนฮาร์ดดิสก์ และตรวจนับไฟล์ภายในทุกโฟลเดอร์ (ไส้ใน)
            self.signals.api_log.emit("📂 กำลังตรวจสอบการเปลี่ยนแปลงและนับไฟล์ในทุกโฟลเดอร์...")

            try:
                real_folders = [f.name for f in os.scandir(self.root) if f.is_dir() and not f.name.startswith('.')]
            except Exception as e:
                logging.error(f"Error scanning root dir: {e}")
                real_folders = []

            real_folders_set = set(real_folders)

            # จัดการลบโฟลเดอร์ที่ถูกลบออกจากตาราง thumbnails และ file_index ทันที
            deleted_aids = [aid for aid in db_folders_info.keys() if aid not in real_folders_set]
            if deleted_aids:
                try:
                    for i in range(0, len(deleted_aids), 500):
                        chunk = [(a,) for a in deleted_aids[i:i+500]]
                        cursor.executemany("DELETE FROM thumbnails WHERE artist_id = ?", chunk)
                        parent_patterns = [(os.path.join(self.root, a[0]) + '%',) for a in chunk]
                        cursor.executemany("DELETE FROM file_index WHERE parent_folder LIKE ?", parent_patterns)
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
                        self.signals.scan_batch.emit(self.gen_id, new_folders_batch)
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
                        self.signals.folder_updated.emit(self.gen_id, folder_name, dname, None, actual_fcount, actual_mtime, actual_ctime)

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
                self.signals.scan_batch.emit(self.gen_id, new_folders_batch)

            if thumbnails_updates:
                cursor.executemany("INSERT OR REPLACE INTO thumbnails (artist_id, image_data, folder_mtime, file_count, folder_ctime) VALUES (?, ?, ?, ?, ?)", thumbnails_updates)
            if file_index_inserts:
                cursor.executemany("INSERT OR REPLACE INTO file_index (path, filename, parent_folder, mtime, size, ctime) VALUES (?, ?, ?, ?, ?, ?)", file_index_inserts)
            conn.commit()

            total_folders = len(real_folders)
            self.signals.scan_finished.emit(self.gen_id, total_folders)
            log_msg = f"✅ ตรวจสอบเสร็จสิ้น: {total_folders} โฟลเดอร์ (พบใหม่ {new_count}, มีไฟล์เพิ่ม/ลด {modified_folders}, ลบออก {len(deleted_aids)})"
            self.signals.api_log.emit(log_msg)

        except Exception as e:
            logging.error(f"Error in FolderCacheScanner: {e}")
        finally:
            pass # conn.close()
        
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
                pass # conn.close()
            # Emit blob (ไม่ใช่ QImage) — UI จะ decode เฉพาะตอนแสดงผลจริง
            self.signals.folder_updated.emit(self.gen_id, self.artist_id, name, blob, file_count, current_mtime, current_ctime)
        except Exception as e: logging.error(f"Error: {e}")

    def composite(self, path, files):
        if not files: return None
        sample = random.sample(files, min(len(files), 4))
        canvas = Image.new('RGB', (240, 240), (30,30,30)); pos = [(0,0), (120,0), (0,120), (120,120)]
        for i, f in enumerate(sample):
            img, _ = load_media_thumbnail(os.path.join(path, f))
            if img:
                if img.mode!='RGB': img=img.convert('RGB')
                canvas.paste(ImageOps.fit(img, (120,120), Image.Resampling.LANCZOS), pos[i])
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
            pass # conn.close()
        except Exception as e:
            import traceback
            print(f"[workers.py] SQLite Read Error for {self.path}: {e}")
            traceback.print_exc()

        # 2. If not found in cache, generate it from disk (images only)
        if qimg is None or qimg.isNull():
            if ext in (EXT_VID | EXT_GIF | EXT_ZIP):
                pass # Skip generating heavy video/animation thumbnails on foreground thread pool
            else:
                try:
                    pil_img, _ = load_media_thumbnail(self.path)
                    if pil_img:
                        pil_img.thumbnail((240, 240))
                        if pil_img.mode != 'RGB':
                            pil_img = pil_img.convert('RGB')
                        bio = io.BytesIO()
                        pil_img.save(bio, 'JPEG', quality=80)
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
                        pass # conn.close()
                    except Exception as e:
                        import traceback
                        print(f"[workers.py] SQLite Write Error for {self.path}: {e}")
                        traceback.print_exc()
                except Exception as e:
                    import traceback
                    print(f"[workers.py] Thumbnail Generation Error for {self.path}: {e}")
                    traceback.print_exc()

        # 3. Emit the loaded thumbnail
        if qimg and not qimg.isNull():
            self.signals.image_loaded.emit(self.gen_id, self.path, qimg, ftype)

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

        # Check tag/rating filter (SFW/NSFW, characters, series, tags)
        if self.matched_tag_paths is not None and filepath and filepath not in self.matched_tag_paths:
            return False

        # Check text search keyword
        if self.search_keyword and self.search_keyword not in filename.lower():
            return False

        # Check parsed_query keywords (match filename OR tag)
        if self.parsed_query and self.parsed_query.keywords:
            fn_lower = filename.lower()
            for kw in self.parsed_query.keywords:
                matched_in_fn = kw in fn_lower
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
        self.signals.api_log.emit("กำลังสแกนหาไฟล์รูปภาพ/วิดีโอทั้งหมด...")
        conn = get_thread_local_conn(self.db_file); cursor = conn.cursor()

        # Resolve tag / character / rating queries via query_parser
        has_query = (
            self.parsed_query is not None or 
            (self.rating_filter and self.rating_filter != 'ALL') or 
            bool(self.tag_filter)
        )
        if has_query:
            try:
                from query_parser import resolve_matching_paths, parse_search_query, SearchQuery
                q = self.parsed_query or SearchQuery()
                if isinstance(q, str):
                    q = parse_search_query(q)

                resolved = resolve_matching_paths(conn, q, active_rating=self.rating_filter, active_char=self.tag_filter)
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
                        self.signals.scan_batch.emit(self.gen_id, db_list)
                        db_list = []
            if db_list:
                self.signals.scan_batch.emit(self.gen_id, db_list)

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
                        self.signals.scan_batch.emit(self.gen_id, ui_batch)
                        ui_batch = []

                    if len(new_or_updated) >= batch_size:
                        self.save_batch_to_db(cursor, new_or_updated)
                        new_or_updated = []

        if ui_batch:
            self.signals.scan_batch.emit(self.gen_id, ui_batch)
        if new_or_updated:
            self.save_batch_to_db(cursor, new_or_updated)

        # 3. Clean up deleted files from SQLite index
        deleted_paths = []
        for path in db_files.keys():
            if path not in seen_on_disk:
                deleted_paths.append((path,))

        if deleted_paths:
            try:
                for i in range(0, len(deleted_paths), 500):
                    batch_del = deleted_paths[i:i+500]
                    cursor.executemany("DELETE FROM file_index WHERE path = ?", batch_del)
                conn.commit()
            except Exception as e:
                pass

        conn.commit(); pass # conn.close()
        total_count = len(seen_on_disk)
        self.signals.scan_finished.emit(self.gen_id, total_count)
        self.signals.api_log.emit(f"สแกนเสร็จสิ้น พบไฟล์ทั้งหมด {total_count} ไฟล์ (ลบไฟล์ที่หายไป {len(deleted_paths)} ไฟล์)")

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
            self.signals.api_log.emit(str(e))
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
                        pass # conn_write.close()
                    except Exception as e: logging.error(f"Error: {e}")

                    self.signals.api_updated.emit(aid, name, blob)
                    self.signals.api_log.emit(f"Updated {aid}: {name}")
                time.sleep(1.5) # ลดความถี่ลงเพื่อหลีกเลี่ยง rate limit
            except Exception as e:
                self.signals.api_log.emit(f"Error {aid}: {e}")
                time.sleep(2)

        self.signals.api_log.emit("Done")

    def stop(self): self.running = False


class BooruNameUpdateWorker(QRunnable):
    def __init__(self, ids):
        super().__init__()
        self.ids = ids
        self.signals = WorkerSignals()
        self.running = True

    def _query_booru(self, base_url, site_name, aid):
        patterns = [
            f"*pixiv.net*/{aid}*",    
            f"*pixiv.net*id={aid}*"   
        ]
        headers = {
            'User-Agent': 'PixivManager/2.2.0 (Windows NT 10.0; Win64; x64)'
        }
        for pattern in patterns:
            if not self.running: break
            search_url = f"{base_url}/artists.json?search[url_matches]={pattern}"

            # --- Retry loop (3 attempts) ---
            for attempt in range(3):
                if not self.running: break
                try:
                    res = requests.get(search_url, headers=headers, timeout=5)
                    if res.status_code == 429:
                        self.signals.api_log.emit(f"{site_name} 429: Rate limited. Retrying...")
                        time.sleep(5)
                        continue
                    if res.status_code == 200:
                        data = res.json()
                        if data and len(data) > 0:
                            raw_name = data[0].get('name', '')
                            if raw_name:
                                return raw_name.replace('_', ' ').title()
                        break
                    break
                except Exception as e:
                    logging.debug(f"{site_name} query error: {e}")
            time.sleep(0.3)
        return None

    def fetch_artist(self, aid):
        if not self.running: return aid, None

        found_name = None

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
        except Exception as e:
            logging.error(f"Error: {e}")

        # --- Source 2: Danbooru (Complete SFW & NSFW artist registry) ---
        if not found_name and self.running:
            found_name = self._query_booru("https://danbooru.donmai.us", "Danbooru", aid)

        # --- Source 3: Safebooru Fallback (queries Booru metadata tags) ---
        if not found_name and self.running:
            found_name = self._query_booru("https://safebooru.donmai.us", "Safebooru", aid)

        return aid, found_name

    @pyqtSlot()
    def run(self):
        # [Turbo 3] ใช้ ThreadPool แยกร่างทำงานพร้อมกันทีละ 3 คน!
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            # โยนงานทั้งหมดให้ลูกน้อง 3 คนช่วยกันทำ
            futures = {executor.submit(self.fetch_artist, aid): aid for aid in self.ids}

            for future in concurrent.futures.as_completed(futures):
                if not self.running: 
                    executor.shutdown(wait=False, cancel_futures=True)
                    break

                aid, found_name = future.result()

                if found_name:
                    try:
                        conn_write = get_thread_local_conn(DB_FILE, timeout=5.0)
                        cursor_write = conn_write.cursor()
                        cursor_write.execute('UPDATE artists SET name=? WHERE artist_id=?', (found_name, aid))
                        if cursor_write.rowcount == 0:
                            cursor_write.execute('INSERT INTO artists (artist_id, name) VALUES(?,?)', (aid, found_name))
                        conn_write.commit()
                        pass # conn_write.close()
                    except Exception as e: logging.error(f"Error: {e}")

                    self.signals.api_updated.emit(aid, found_name, None)
                    self.signals.api_log.emit(f"Found: {found_name}")
                else:
                    self.signals.api_log.emit(f"ID {aid} not found in Pixiv/Danbooru/Safebooru")

        self.signals.api_log.emit("Name Update Finished")

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
                    pil_img.thumbnail((240, 240))
                    if pil_img.mode != 'RGB':
                        pil_img = pil_img.convert('RGB')
                    bio = io.BytesIO()
                    pil_img.save(bio, 'JPEG', quality=80)
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
                    except Exception as e:
                        pass
            finally:
                    executor.shutdown(wait=False, cancel_futures=True)

        finally:
            if uncommitted:
                try:
                    conn.commit()
                except Exception:
                    pass
            try:
                pass # conn.close()
            except Exception as e:
                logging.error(f"Error: {e}")

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
            self.signals.log.emit(f"❌ Error downloading {os.path.basename(filepath)}: {e}")
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
            pass # conn.close()
        except:
            pass

    def process_ugoira(self, ill_id, headers, dest_dir, filename_prefix, artist_id=None, artist_name=None):
        meta_url = f"https://www.pixiv.net/ajax/illust/{ill_id}/ugoira_meta"
        try:
            req_headers = headers.copy()
            req_headers['Referer'] = f"https://www.pixiv.net/artworks/{ill_id}"
            r = requests.get(meta_url, headers=req_headers, timeout=15)
            if r.status_code != 200:
                if r.status_code == 404:
                    if not self.cookie:
                        self.signals.log.emit(f"   ❌ Failed to fetch Ugoira metadata (HTTP 404): This artwork is likely R-18 / restricted. Pixiv requires a valid PHPSESSID cookie to download R-18 works.")
                    else:
                        self.signals.log.emit(f"   ❌ Failed to fetch Ugoira metadata (HTTP 404): Artwork not found or inaccessible. If this is an R-18 work, please verify your PHPSESSID is valid and R-18 display is enabled in Pixiv Settings.")
                else:
                    self.signals.log.emit(f"   ❌ Failed to fetch Ugoira metadata (HTTP {r.status_code})")
                return False
            data = r.json()
            if data.get('error'):
                self.signals.log.emit(f"   ❌ Ugoira API error: {data.get('message')}")
                return False
        
            body = data.get('body', {})
            zip_url = body.get('src')
            frames = body.get('frames', [])
            if not zip_url or not frames:
                self.signals.log.emit("   ❌ Ugoira metadata is incomplete (missing zip src or frames)")
                return False
        
            gif_filename = f"{filename_prefix}.gif"
            gif_filepath = os.path.join(dest_dir, gif_filename)
        
            import zipfile
            temp_zip = os.path.join(dest_dir, f"temp_{ill_id}.zip")
            self.signals.log.emit(f"   📥 Downloading Ugoira zip file...")
            if not self.download_image(zip_url, temp_zip):
                self.signals.log.emit(f"   ❌ Failed to download Ugoira zip file")
                if os.path.exists(temp_zip): os.remove(temp_zip)
                return False
        
            self.signals.log.emit(f"   🔄 Converting ZIP to animated GIF...")
            pil_frames = []
            durations = []
            with zipfile.ZipFile(temp_zip, 'r') as z:
                for frame_info in frames:
                    if not self.running: break
                    frame_file = frame_info.get('file')
                    delay = frame_info.get('delay', 100)
                    with z.open(frame_file) as f:
                        img = Image.open(io.BytesIO(f.read()))
                        pil_frames.append(img.convert('RGB'))
                        durations.append(delay)
        
            if not self.running:
                if os.path.exists(temp_zip):
                    try: os.remove(temp_zip)
                    except Exception: pass
                return False

            if pil_frames:
                os.makedirs(os.path.dirname(gif_filepath), exist_ok=True)
                pil_frames[0].save(
                    gif_filepath,
                    save_all=True,
                    append_images=pil_frames[1:],
                    duration=durations,
                    loop=0
                )
                self.register_file_in_db(gif_filepath, artist_id, artist_name)
            if os.path.exists(temp_zip):
                os.remove(temp_zip)
            return True
        except Exception as e:
            self.signals.log.emit(f"   ❌ Error processing Ugoira: {e}")
            if 'temp_zip' in locals() and os.path.exists(temp_zip):
                os.remove(temp_zip)
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
                self.signals.log.emit(f"   ⚠️ Failed to fetch pages details for Illust {ill_id}: {e}")
    
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
                self.signals.log.emit("🔑 Using PHPSESSID cookie for authenticated metadata query.")
    
        if self.mode == 'ILLUST':
            self.signals.log.emit(f"🔍 Fetching illust metadata for ID: {self.target_id}...")
            url = f"https://www.pixiv.net/ajax/illust/{self.target_id}"
            try:
                r = requests.get(url, headers=headers, timeout=10)
                if r.status_code != 200:
                    self.signals.finished.emit(False, f"HTTP Error {r.status_code} when fetching illust data.")
                    return
                data = r.json()
                if data.get('error'):
                    self.signals.finished.emit(False, f"Pixiv API Error: {data.get('message', 'Unknown error')}")
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
                    self.signals.log.emit("   ⚠️ Warning: This artwork is R-18 / R-18G. Pixiv requires a valid PHPSESSID cookie to download adult content.")
            
                if illust_type == 2:
                    self.signals.log.emit(f"🎬 Illust ID {self.target_id} is a Ugoira (Animation). Processing...")
                    filename_prefix = f"{self.target_id}_ugoira - {clean_title}" if clean_title else f"{self.target_id}_ugoira"
                    success = self.process_ugoira(self.target_id, headers, dest_dir, filename_prefix, artist_id=artist_id, artist_name=artist_name)
                    fail_msg = "Ugoira download failed (R-18 works require PHPSESSID cookie)" if (x_restrict > 0 and not self.cookie) else "Ugoira download failed"
                    self.signals.finished.emit(success, f"Finished downloading Ugoira {self.target_id}" if success else fail_msg)
                    return

                pages = self.get_illust_pages(self.target_id, headers, body)
                if not pages and x_restrict > 0 and not self.cookie:
                    self.signals.log.emit("   ❌ Cannot download R-18 image files without authentication. Please enter your PHPSESSID cookie in the download dialog.")
                    self.signals.finished.emit(False, "R-18 work requires PHPSESSID cookie to download.")
                    return
            
                total_pages = len(pages)
                self.signals.log.emit(f"🎨 Artist: {artist_name} | Found {total_pages} page(s)")
            
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
                        self.signals.log.emit(f"⏭️ File already exists, skipping: {filename}")
                        downloaded += 1
                        self.signals.progress.emit(downloaded, total_pages)
                        continue
                
                    self.signals.log.emit(f"📥 Downloading page {idx+1}/{total_pages}: {filename}...")
                    if self.download_image(orig_url, dest_file):
                        downloaded += 1
                        self.signals.log.emit(f"✅ Success: {filename}")
                        self.register_file_in_db(dest_file, artist_id, artist_name)
                    self.signals.progress.emit(downloaded, total_pages)
                
                    if idx < total_pages - 1:
                        delay = random.uniform(2.0, 4.0)
                        self.signals.log.emit(f"💤 Sleeping for {delay:.1f}s (Safe Mode)...")
                        self.interruptible_sleep(delay)
            
                if not self.running:
                    self.signals.finished.emit(False, "Download cancelled by user.")
                    return

                self.signals.finished.emit(True, f"Finished downloading illust {self.target_id}")
            except Exception as e:
                self.signals.finished.emit(False, str(e))

        elif self.mode == 'ARTIST':
            self.signals.log.emit(f"🔍 Fetching artist profile for ID: {self.target_id}...")
            url = f"https://www.pixiv.net/ajax/user/{self.target_id}/profile/all"
            try:
                r = requests.get(url, headers=headers, timeout=15)
                if r.status_code != 200:
                    self.signals.finished.emit(False, f"HTTP Error {r.status_code} when fetching artist profile.")
                    return
                data = r.json()
                if data.get('error'):
                    self.signals.finished.emit(False, f"Pixiv API Error: {data.get('message', 'Unknown error')}")
                    return
            
                body = data.get('body', {})
                illusts = body.get('illusts', {}) or {}
                manga = body.get('manga', {}) or {}
            
                # Sort descending (newest works first)
                illust_ids = sorted(list(illusts.keys()) + list(manga.keys()), key=int, reverse=True)
                total_illusts = len(illust_ids)
            
                if total_illusts == 0:
                    self.signals.finished.emit(True, "Artist has no illustrations or manga.")
                    return
            
                self.signals.log.emit(f"🎨 Found {total_illusts} works for Artist ID: {self.target_id}")
            
                artist_name = "Artist " + self.target_id
                first_url = f"https://www.pixiv.net/ajax/illust/{illust_ids[0]}"
                r_first = requests.get(first_url, headers=headers, timeout=15)
                if r_first.status_code == 200:
                    first_data = r_first.json()
                    if not first_data.get('error'):
                        artist_name = first_data.get('body', {}).get('userName', artist_name)
            
                artist_folder = self.target_id
                dest_dir = os.path.join(self.output_dir, artist_folder)
                self.signals.log.emit(f"📁 Destination Folder: {dest_dir}")
            
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
            
                self.signals.log.emit(f"🎨 Found {total_illusts} works total. {total_illusts - total_new} already exist in storage. {total_new} new works to download.")
            
                if total_new == 0:
                    self.signals.finished.emit(True, "All illustrations of this artist are already downloaded.")
                    return
            
                downloaded_works = 0
            
                for idx, ill_id in enumerate(new_illust_ids):
                    if not self.running: break
                
                    self.signals.log.emit(f"🖼️ [{idx+1}/{total_new}] Fetching metadata for Illust ID {ill_id}...")
                
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
                                    gif_filepath = os.path.join(dest_dir, f"{filename_prefix}.gif")
                                    if os.path.exists(gif_filepath):
                                        self.signals.log.emit(f"   ⏭️ Skipping existing Ugoira: {filename_prefix}.gif")
                                    else:
                                        self.signals.log.emit(f"   🎬 Illust ID {ill_id} is a Ugoira (Animation). Processing...")
                                        if self.process_ugoira(ill_id, headers, dest_dir, filename_prefix, artist_id=self.target_id, artist_name=artist_name):
                                            downloaded_works += 1
                                
                                    if self.max_limit and downloaded_works >= self.max_limit:
                                        self.signals.log.emit(f"🛑 Reached download limit of {self.max_limit} works. Stopping.")
                                        self.signals.finished.emit(True, f"Reached download limit of {self.max_limit} works.")
                                        return
                                else:
                                    pages = self.get_illust_pages(ill_id, headers, ill_body)
                                    if not pages and x_restrict > 0 and not self.cookie:
                                        self.signals.log.emit(f"   ⚠️ Skipping R-18 Illust ID {ill_id} (Requires PHPSESSID cookie)")
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
                                             self.signals.log.emit(f"   ⏭️ Skipping existing: {filename}")
                                             continue
                                        
                                        self.signals.log.emit(f"   📥 Downloading page {p_idx+1}: {filename}...")
                                        if self.download_image(orig_url, dest_file):
                                            self.signals.log.emit(f"   ✅ Success: {filename}")
                                            has_downloaded_any_page = True
                                            self.register_file_in_db(dest_file, self.target_id, artist_name)
                                    
                                        delay = random.uniform(2.0, 4.0)
                                        self.interruptible_sleep(delay)
                                
                                    if has_downloaded_any_page:
                                        downloaded_works += 1
                                    
                                    if self.max_limit and downloaded_works >= self.max_limit:
                                        self.signals.log.emit(f"🛑 Reached download limit of {self.max_limit} works. Stopping.")
                                        self.signals.finished.emit(True, f"Reached download limit of {self.max_limit} works.")
                                        return
                    except Exception as e:
                        self.signals.log.emit(f"   ❌ Error fetching details for Illust {ill_id}: {e}")
                
                    self.signals.progress.emit(idx + 1, total_new)
                
                    if idx < total_new - 1:
                        if (idx + 1) % 10 == 0:
                            session_delay = random.uniform(15.0, 30.0)
                            self.signals.log.emit(f"☕ [SESSION BREAK] Sleeping {session_delay:.1f}s to mimic human breaks...")
                            self.interruptible_sleep(session_delay)
                        else:
                            delay = random.uniform(3.0, 5.0)
                            self.signals.log.emit(f"💤 Sleeping for {delay:.1f}s...")
                            self.interruptible_sleep(delay)
            
                if not self.running:
                    self.signals.finished.emit(False, "Download cancelled by user.")
                    return

                self.signals.finished.emit(True, f"Finished downloading gallery for Artist {self.target_id}")
            except Exception as e:
                self.signals.finished.emit(False, str(e))

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
            self.signals.log.emit(f"🔍 Fetching bookmarks list for User: {self.target_id} ({self.bookmark_rest}) | Range: {page_str}...")
        
            try:
                while self.running:
                    current_page = (offset // limit) + 1
                    if self.end_page is not None and current_page > self.end_page:
                        break
                    
                    url = f"https://www.pixiv.net/ajax/user/{self.target_id}/illusts/bookmarks?tag=&offset={offset}&limit={limit}&rest={self.bookmark_rest}"
                    r = requests.get(url, headers=headers, timeout=15)
                    if r.status_code != 200:
                        self.signals.finished.emit(False, f"HTTP Error {r.status_code} while fetching bookmark offset {offset}")
                        return
                
                    data = r.json()
                    if data.get('error'):
                        self.signals.finished.emit(False, f"Pixiv API Error: {data.get('message', 'Unknown error')}")
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
                    self.signals.log.emit(f"📋 Found {len(illust_ids)} bookmarks so far (Current: Page {current_page})...")
                    self.interruptible_sleep(1.0)  # Small metadata fetch safety delay
            
                if not self.running:
                    self.signals.finished.emit(False, "Download cancelled by user.")
                    return

                total_illusts = len(illust_ids)
                if total_illusts == 0:
                    self.signals.finished.emit(True, "No bookmarked illustrations found in this range.")
                    return
            
                self.signals.log.emit(f"🚀 Total bookmarked illustrations collected: {total_illusts}")
            
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
            
                self.signals.log.emit(f"🚀 Collected {total_illusts} bookmarks total. {total_illusts - total_new} already exist in storage. {total_new} new works to download.")
            
                if total_new == 0:
                    self.signals.finished.emit(True, "All bookmarked illustrations are already downloaded.")
                    return
            
                downloaded_works = 0
            
                for idx, ill_id in enumerate(new_illust_ids):
                    if not self.running: break
                
                    self.signals.log.emit(f"🖼️ [{idx+1}/{total_new}] Fetching metadata for Bookmarked Illust ID {ill_id}...")
                
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
                                    gif_filepath = os.path.join(artist_dir, f"{filename_prefix}.gif")
                                    if os.path.exists(gif_filepath):
                                        self.signals.log.emit(f"   ⏭️ Skipping existing Ugoira: {filename_prefix}.gif")
                                    else:
                                        self.signals.log.emit(f"   🎬 Bookmarked Illust ID {ill_id} is a Ugoira (Animation). Processing...")
                                        if self.process_ugoira(ill_id, headers, artist_dir, filename_prefix, artist_id=artist_id, artist_name=artist_name):
                                            downloaded_works += 1
                                
                                    if self.max_limit and downloaded_works >= self.max_limit:
                                        self.signals.log.emit(f"🛑 Reached download limit of {self.max_limit} works. Stopping.")
                                        self.signals.finished.emit(True, f"Reached download limit of {self.max_limit} works.")
                                        return
                                else:
                                    pages = self.get_illust_pages(ill_id, headers, ill_body)
                                    if not pages and x_restrict > 0 and not self.cookie:
                                        self.signals.log.emit(f"   ⚠️ Skipping R-18 Bookmarked Illust ID {ill_id} (Requires PHPSESSID cookie)")
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
                                            self.signals.log.emit(f"   ⏭️ Skipping existing: {filename}")
                                            continue
                                        
                                        self.signals.log.emit(f"   📥 Downloading page {p_idx+1}: {filename}...")
                                        if self.download_image(orig_url, dest_file):
                                            self.signals.log.emit(f"   ✅ Success: {filename}")
                                            has_downloaded_any_page = True
                                            self.register_file_in_db(dest_file, artist_id, artist_name)
                                    
                                        delay = random.uniform(2.0, 4.0)
                                        self.interruptible_sleep(delay)
                                
                                    if has_downloaded_any_page:
                                        downloaded_works += 1
                                    
                                    if self.max_limit and downloaded_works >= self.max_limit:
                                        self.signals.log.emit(f"🛑 Reached download limit of {self.max_limit} works. Stopping.")
                                        self.signals.finished.emit(True, f"Reached download limit of {self.max_limit} works.")
                                        return
                    except Exception as e:
                        self.signals.log.emit(f"   ❌ Error fetching details for Illust {ill_id}: {e}")
                
                    self.signals.progress.emit(idx + 1, total_new)
                
                    if idx < total_new - 1:
                        if (idx + 1) % 10 == 0:
                            session_delay = random.uniform(15.0, 30.0)
                            self.signals.log.emit(f"☕ [SESSION BREAK] Sleeping {session_delay:.1f}s to mimic human breaks...")
                            self.interruptible_sleep(session_delay)
                        else:
                            delay = random.uniform(3.0, 5.0)
                            self.signals.log.emit(f"💤 Sleeping for {delay:.1f}s...")
                            self.interruptible_sleep(delay)
            
                if not self.running:
                    self.signals.finished.emit(False, "Download cancelled by user.")
                    return

                self.signals.finished.emit(True, f"Finished downloading bookmarks for User {self.target_id}")
            except Exception as e:
                self.signals.finished.emit(False, str(e))

class DatabaseOptimizerWorker(QRunnable):
    def __init__(self, db_file):
        super().__init__()
        self.db_file = db_file
        self.signals = WorkerSignals()

    @pyqtSlot()
    def run(self):
        try:
            conn = sqlite3.connect(self.db_file, timeout=60.0)
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()
            self.signals.api_log.emit("SUCCESS")
        except Exception as e:
            self.signals.api_log.emit(f"ERROR: {e}")

class AiTagWorkerSignals(QObject):
    progress = pyqtSignal(int, int, str, float, float)  # current, total, filename, fps, eta_seconds
    item_tagged = pyqtSignal(str, list, list, list, str) # path, chars, series, general, rating
    batch_saved = pyqtSignal(int)                        # total items committed to db
    log = pyqtSignal(str)                                # text log message
    finished = pyqtSignal(int, int, float)               # total_tagged, errors, elapsed_seconds
    paused = pyqtSignal(bool)
    model_loading = pyqtSignal(str)

import queue

class AiTaggingWorker(QRunnable):
    """
    High-Performance Multi-threaded Asynchronous AI Tagging Worker (DirectML GPU Accelerated):
    - Producer-Consumer Architecture: Parallel CPU prefetchers decode images and scale tensors in advance
    - DirectML GPU consumer executes back-to-back inference with zero disk I/O idle time
    - Chunked SQLite commits for minimal write lock overhead

    """
    def __init__(self, root_path=None, db_file=DB_FILE, use_gpu=True,
                 use_fp16=True,
                 model_key="wd-v1-4-convnext-v2",
                 storage_mode="auto",
                 char_threshold=DEFAULT_CHARACTER_THRESHOLD,
                 gen_threshold=DEFAULT_GENERAL_THRESHOLD,
                 rescan_existing=False, batch_size=50, throttle=0.0):
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
        self.signals = AiTagWorkerSignals()
        self.running = True
        self.is_paused = False
        self._pause_cond = threading.Condition()

    def pause(self):
        with self._pause_cond:
            self.is_paused = True
            self.signals.paused.emit(True)

    def resume(self):
        with self._pause_cond:
            self.is_paused = False
            self._pause_cond.notify_all()
            self.signals.paused.emit(False)

    def stop(self):
        self.running = False
        with self._pause_cond:
            self.is_paused = False
            self._pause_cond.notify_all()

    @pyqtSlot()
    def run(self):
        from tagger import WD14Tagger, get_model_paths
        if not WD14Tagger.is_model_installed(self.model_key):
            self.signals.log.emit("⚠️ ไม่พบไฟล์โมเดล AI กรุณาดาวน์โหลดโมเดลก่อนเริ่มสแกน")
            self.signals.finished.emit(0, 0, 0.0)
            return

        model_meta = get_model_paths(self.model_key)["meta"]
        precision_label = "FP16 (Turbo)" if (self.use_gpu and self.use_fp16) else "FP32 Standard"
        self.signals.model_loading.emit(f"กำลังโหลด {model_meta['short_name']} ({precision_label}) เข้า GPU...")
        try:
            tagger = WD14Tagger(model_key=self.model_key, use_gpu=self.use_gpu, use_fp16=self.use_fp16 if self.use_gpu else False)
            tagger.load_model()
            self.signals.log.emit(f"✅ โหลดโมเดล {model_meta['short_name']} สำเร็จ (ทำงานบน: {tagger.active_provider} | {precision_label})")
        except Exception as e:
            self.signals.log.emit(f"❌ โหลดโมเดลไม่สำเร็จ: {e}")
            self.signals.finished.emit(0, 1, 0.0)
            return

        conn = get_thread_local_conn(self.db_file)
        cursor = conn.cursor()

        self.signals.log.emit("🔍 กำลังค้นหาไฟล์ที่ต้องสแกนแท็ก (Direct Disk Walk)...")
        target_files = []
        try:
            # 1. Determine search folder on disk
            search_folder = self.root_path
            if not search_folder and self.db_file == DB_FILE:
                try:
                    import json
                    with open('config.json', 'r', encoding='utf-8') as f:
                        search_folder = json.load(f).get('last_folder')
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
                self.signals.log.emit(f"🔄 โหมดสแกนใหม่ทั้งหมด (Full Re-scan): เตรียมสแกนทับทั้งหมด {len(target_files):,} ไฟล์...")
            else:
                if self.root_path:
                    search_prefix = self.root_path + os.sep
                    search_prefix_upper = self.root_path + os.sep + "\uffff"
                    cursor.execute("SELECT DISTINCT path FROM file_tags WHERE path >= ? AND path < ? AND tag_name != '__none__'", (search_prefix, search_prefix_upper))
                else:
                    cursor.execute("SELECT DISTINCT path FROM file_tags WHERE tag_name != '__none__'")
                already_tagged_set = {row[0] for row in cursor.fetchall()}
                target_files = [f for f in disk_files if f not in already_tagged_set]
                skipped_count = len(disk_files) - len(target_files)
                self.signals.log.emit(
                    f"⚡ โหมดด่วน (Quick Mode): พบไฟล์ใหม่ที่ยังไม่มีแท็ก {len(target_files):,} ไฟล์ "
                    f"(ข้าม {skipped_count:,} ไฟล์ที่มีแท็กแล้ว จากทั้งหมด {len(disk_files):,} ไฟล์)..."
                )

        except Exception as e:
            self.signals.log.emit(f"❌ เกิดข้อผิดพลาดในการค้นหาไฟล์: {e}")
            self.signals.finished.emit(0, 1, 0.0)
            return

        total_files = len(target_files)
        if total_files == 0:
            self.signals.log.emit("✨ ไม่มีไฟล์ใหม่ที่ต้องสแกนแท็ก (ไฟล์ทั้งหมดมีแท็กครบแล้ว)")
            self.signals.finished.emit(0, 0, 0.0)
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

        INFERENCE_BATCH = 16  # Stack 16 images per GPU call (RX6800: ~490 FPS theoretical)

        self.signals.log.emit(
            f"🚀 เริ่มการสแกน Turbo Batch Pipeline ทั้งหมด {total_files:,} ไฟล์ "
            f"(GPU Batch={INFERENCE_BATCH} | Storage: {profile_label} [{num_prefetchers} I/O Threads])..."
        )

        start_time = time.time()
        processed_count = 0
        error_count = 0
        batch_tags = []

        # Multi-threaded Producer-Consumer Prefetch Pipeline
        prefetch_queue = queue.Queue(maxsize=INFERENCE_BATCH * 8)  # Deep buffer: always have next batch ready
        idx_queue = queue.Queue()
        for idx, fp in enumerate(target_files):
            idx_queue.put((idx, fp))

        def prefetch_worker():
            _TARGET_SIZE = 448  # WD14 model input size

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
                            # --- cv2 fast path: releases GIL, true thread parallelism ---
                            # Use np.fromfile + imdecode to support Unicode paths on Windows
                            raw = np.fromfile(path, dtype=np.uint8)
                            img = _cv2.imdecode(raw, _cv2.IMREAD_COLOR)
                            if img is not None:
                                h, w = img.shape[:2]
                                scale = _TARGET_SIZE / max(h, w)
                                nw = max(1, int(w * scale))
                                nh = max(1, int(h * scale))
                                img = _cv2.resize(img, (nw, nh), interpolation=_cv2.INTER_LINEAR)
                                padded = np.full((_TARGET_SIZE, _TARGET_SIZE, 3), 255, dtype=np.float16)
                                px = (_TARGET_SIZE - nw) // 2
                                py = (_TARGET_SIZE - nh) // 2
                                padded[py:py+nh, px:px+nw] = img.astype(np.float16)
                                tensor = np.expand_dims(padded, axis=0)  # (1, 448, 448, 3)
                        else:
                            # PIL fallback for GIF / ZIP / other formats
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

        # Spawn parallel prefetch threads — scaled to keep GPU fully fed at batch=16
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
                self.signals.log.emit("🛑 การสแกนถูกหยุดโดยผู้ใช้")
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
                    stacked = np.concatenate(tensors, axis=0)  # (N, 448, 448, 3)
                    batch_results = tagger.predict_tensor_batch(
                        stacked,
                        char_threshold=self.char_threshold,
                        gen_threshold=self.gen_threshold
                    )
                    for fp, res in zip(fps_list, batch_results):
                        per_file_results[fp] = res
                except Exception as e:
                    logging.error(f"Batch inference error: {e}")
                    # Mark all as failed so they fall into error path below
                    for fp, _ in valid_items:
                        per_file_results[fp] = None

            t_batch = time.time() - t0
            per_item_time = t_batch / max(len(current_batch), 1)

            # Process each item's result and emit progress
            for _, file_path, tensor in current_batch:
                filename = os.path.basename(file_path)
                res = per_file_results.get(file_path)

                if tensor is None:
                    # File missing or failed to decode — do not record dummy tag so it can be retried
                    error_count += 1
                elif res and res.get("all_tags"):
                    # Successful inference
                    for tag_name, category, conf in res["all_tags"]:
                        batch_tags.append((file_path, tag_name, category, conf))
                    self.signals.item_tagged.emit(
                        file_path,
                        res.get("characters", []),
                        res.get("series", []),
                        res.get("general", [])[:8],
                        res.get("rating", "general")
                    )
                    processed_count += 1
                else:
                    # Inference ran successfully but no tags met confidence threshold
                    if res is None and file_path in per_file_results:
                        error_count += 1
                    else:
                        processed_count += 1
                        # No valid tags detected above threshold — leave database clean without dummy records

                items_processed += 1

                # --- Stable FPS: use wall-clock average (immune to queue wait fluctuation) ---
                wall_elapsed = time.time() - start_time
                fps = items_processed / wall_elapsed if wall_elapsed > 0 else 0.0
                eta_sec = (total_files - items_processed) / fps if fps > 0 else 0.0
                self.signals.progress.emit(items_processed, total_files, filename, fps, eta_sec)

            # Batched commit to SQLite
            if len(batch_tags) >= self.batch_size * 5 or items_processed >= total_files:
                try:
                    cursor.executemany("""
                        INSERT OR REPLACE INTO file_tags (path, tag_name, category, confidence)
                        VALUES (?, ?, ?, ?)
                    """, batch_tags)
                    conn.commit()
                    batch_tags = []
                    self.signals.batch_saved.emit(processed_count)
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
        self.signals.log.emit(f"🎉 สแกนเสร็จสิ้น: สำเร็จ {processed_count:,} ไฟล์, ข้อผิดพลาด {error_count} (เวลา {total_elapsed:.1f} วินาที)")
        self.signals.finished.emit(processed_count, error_count, total_elapsed)

