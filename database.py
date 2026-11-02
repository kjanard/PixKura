import os
import time
import sqlite3
import logging
from config import DB_FILE


class DatabaseSetup:
    def __init__(self, db_file=DB_FILE):
        self.conn = sqlite3.connect(db_file)
        self.create_tables()
        self.migrate_tables()
        self.conn.close()

    def create_tables(self):
        cursor = self.conn.cursor()
        try:
            cursor.execute('PRAGMA journal_mode = WAL;')
            cursor.execute('PRAGMA synchronous = NORMAL;')
            cursor.execute('PRAGMA cache_size = -20000;')
            cursor.execute('PRAGMA temp_store = MEMORY;')
            cursor.execute('PRAGMA mmap_size = 2147483648;')
        except Exception:
            pass
        # ตารางศิลปินและรูปปกโฟลเดอร์
        cursor.execute('''CREATE TABLE IF NOT EXISTS artists (artist_id TEXT PRIMARY KEY, name TEXT, profile_blob BLOB)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS thumbnails (artist_id TEXT PRIMARY KEY, image_data BLOB, folder_mtime REAL, last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP, file_count INTEGER DEFAULT 0, folder_ctime REAL DEFAULT 0)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS file_index (path TEXT PRIMARY KEY, filename TEXT, parent_folder TEXT, mtime REAL, size INTEGER DEFAULT 0, ctime REAL DEFAULT 0, dhash TEXT)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS file_thumbnails (path TEXT PRIMARY KEY, image_data BLOB, last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        cursor.execute('''CREATE INDEX IF NOT EXISTS idx_filename ON file_index (filename)''')
        # ตารางจัดเก็บแท็ก AI และแท็กผู้ใช้
        cursor.execute('''CREATE TABLE IF NOT EXISTS file_tags (
            path TEXT,
            tag_name TEXT,
            category INTEGER,
            confidence REAL,
            PRIMARY KEY (path, tag_name)
        )''')
        cursor.execute('''CREATE INDEX IF NOT EXISTS idx_tag_name ON file_tags (tag_name)''')
        cursor.execute('''CREATE INDEX IF NOT EXISTS idx_tag_category ON file_tags (category)''')
        cursor.execute('''CREATE INDEX IF NOT EXISTS idx_file_tags_path ON file_tags (path)''')
        self.conn.commit()

    def migrate_tables(self):
        cursor = self.conn.cursor()
        cursor.execute("PRAGMA table_info(thumbnails)")
        thumb_cols = {r[1] for r in cursor.fetchall()}
        if "file_count" not in thumb_cols:
            try: cursor.execute("ALTER TABLE thumbnails ADD COLUMN file_count INTEGER DEFAULT 0")
            except sqlite3.OperationalError: pass
        if "folder_ctime" not in thumb_cols:
            try: cursor.execute("ALTER TABLE thumbnails ADD COLUMN folder_ctime REAL DEFAULT 0")
            except sqlite3.OperationalError: pass

        cursor.execute("PRAGMA table_info(file_index)")
        fi_cols = {r[1] for r in cursor.fetchall()}
        if "size" not in fi_cols:
            try: cursor.execute("ALTER TABLE file_index ADD COLUMN size INTEGER DEFAULT 0")
            except sqlite3.OperationalError: pass
        if "ctime" not in fi_cols:
            try: cursor.execute("ALTER TABLE file_index ADD COLUMN ctime REAL DEFAULT 0")
            except sqlite3.OperationalError: pass
        if "parent_folder" not in fi_cols:
            try: cursor.execute("ALTER TABLE file_index ADD COLUMN parent_folder TEXT")
            except sqlite3.OperationalError: pass
        if "dhash" not in fi_cols:
            try: cursor.execute("ALTER TABLE file_index ADD COLUMN dhash TEXT")
            except sqlite3.OperationalError: pass

        try: cursor.execute("CREATE INDEX IF NOT EXISTS idx_parent ON file_index (parent_folder)")
        except sqlite3.OperationalError: pass
        try: cursor.execute("CREATE INDEX IF NOT EXISTS idx_dhash ON file_index (dhash)")
        except sqlite3.OperationalError: pass
        try: cursor.execute('''CREATE TABLE IF NOT EXISTS file_thumbnails (path TEXT PRIMARY KEY, image_data BLOB, last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        except sqlite3.OperationalError: pass

        cursor.execute("PRAGMA table_info(artists)")
        art_cols = {r[1] for r in cursor.fetchall()}
        if "profile_blob" not in art_cols:
            try: cursor.execute("ALTER TABLE artists ADD COLUMN profile_blob BLOB")
            except sqlite3.OperationalError: pass

        # AI Tags Migration
        try:
            cursor.execute('''CREATE TABLE IF NOT EXISTS file_tags (
                path TEXT,
                tag_name TEXT,
                category INTEGER,
                confidence REAL,
                PRIMARY KEY (path, tag_name)
            )''')
            cursor.execute('''CREATE INDEX IF NOT EXISTS idx_tag_name ON file_tags (tag_name)''')
            cursor.execute('''CREATE INDEX IF NOT EXISTS idx_tag_category ON file_tags (category)''')
            cursor.execute('''CREATE INDEX IF NOT EXISTS idx_file_tags_path ON file_tags (path)''')
        except sqlite3.OperationalError: pass
        try:
            # Index seek on idx_tag_name first before issuing write lock
            cursor.execute("SELECT 1 FROM file_tags WHERE tag_name = '__none__' LIMIT 1")
            if cursor.fetchone():
                cursor.execute("DELETE FROM file_tags WHERE tag_name = '__none__'")
        except sqlite3.OperationalError: pass
        self.conn.commit()


def get_file_tags(db_file, path):
    """Returns all AI and custom tags for a specific file path."""
    tags = {
        "characters": [],
        "series": [],
        "general": [],
        "custom": [],
        "rating": "general",
        "is_favorite": False,
        "all": []
    }
    try:
        conn = sqlite3.connect(db_file, timeout=5.0)
        cursor = conn.cursor()
        cursor.execute("SELECT tag_name, category, confidence FROM file_tags WHERE path = ? ORDER BY confidence DESC", (path,))
        for tag_name, category, conf in cursor.fetchall():
            if tag_name == "__none__":
                continue
            if category == 10 and tag_name == "favorite":
                tags["is_favorite"] = True
                continue
            tags["all"].append((tag_name, category, conf))
            if category == 4:
                tags["characters"].append((tag_name, conf))
            elif category == 3:
                tags["series"].append((tag_name, conf))
            elif category in (0, 11):
                tags["general"].append((tag_name, conf))
                if category == 11:
                    tags["custom"].append((tag_name, conf))
            elif category in (5, 9):
                tags["rating"] = tag_name
        conn.close()
    except Exception:
        pass
    return tags


def is_file_favorite(db_file, path) -> bool:
    """Checks if a file is marked as favorite (category=10, tag_name='favorite')."""
    if not path:
        return False
    try:
        conn = sqlite3.connect(db_file, timeout=5.0)
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM file_tags WHERE path = ? AND tag_name = 'favorite' AND category = 10 LIMIT 1", (path,))
        res = cursor.fetchone() is not None
        conn.close()
        return res
    except Exception:
        return False


def toggle_file_favorite(db_file, path, fav_state=None) -> bool:
    """
    Toggles or sets favorite status for a file path.
    Returns the new favorite state (True = Favorited, False = Unfavorited).
    """
    if not path:
        return False
    try:
        conn = sqlite3.connect(db_file, timeout=10.0)
        cursor = conn.cursor()
        if fav_state is None:
            cursor.execute("SELECT 1 FROM file_tags WHERE path = ? AND tag_name = 'favorite' AND category = 10 LIMIT 1", (path,))
            currently_fav = cursor.fetchone() is not None
            new_state = not currently_fav
        else:
            new_state = bool(fav_state)

        if new_state:
            cursor.execute(
                "INSERT OR REPLACE INTO file_tags (path, tag_name, category, confidence) VALUES (?, 'favorite', 10, 1.0)",
                (path,)
            )
        else:
            cursor.execute(
                "DELETE FROM file_tags WHERE path = ? AND tag_name = 'favorite' AND category = 10",
                (path,)
            )
        conn.commit()
        conn.close()
        invalidate_tag_stats_cache(db_file)
        return new_state
    except Exception as e:
        logging.error(f"Error toggling favorite for {path}: {e}")
        return False


def add_custom_file_tag(db_file, path, tag_name: str, category: int = 11) -> bool:
    """
    Adds a user-defined tag to a file (default category=11 for custom manual tag, confidence=1.0).
    """
    if not path or not tag_name:
        return False
    clean_tag = tag_name.strip().lower().replace(' ', '_')
    if not clean_tag or clean_tag == '__none__':
        return False
    try:
        conn = sqlite3.connect(db_file, timeout=10.0)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO file_tags (path, tag_name, category, confidence) VALUES (?, ?, ?, 1.0)",
            (path, clean_tag, category)
        )
        conn.commit()
        conn.close()
        invalidate_tag_stats_cache(db_file)
        return True
    except Exception as e:
        logging.error(f"Error adding custom tag '{clean_tag}' to {path}: {e}")
        return False


def remove_file_tag(db_file, path, tag_name: str) -> bool:
    """Removes a specific tag from a file."""
    if not path or not tag_name:
        return False
    clean_tag = tag_name.strip().lower().replace(' ', '_')
    try:
        conn = sqlite3.connect(db_file, timeout=10.0)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM file_tags WHERE path = ? AND tag_name = ?", (path, clean_tag))
        conn.commit()
        conn.close()
        invalidate_tag_stats_cache(db_file)
        return True
    except Exception as e:
        logging.error(f"Error removing tag '{clean_tag}' from {path}: {e}")
        return False


def cleanup_orphaned_records(db_file=DB_FILE) -> dict:
    """
    Purges orphaned entries from file_tags and file_thumbnails whose file paths
    are no longer present in file_index (when file_index is populated).
    """
    removed = {"tags": 0, "thumbnails": 0, "orphaned_tags": 0, "orphaned_thumbnails": 0}
    try:
        conn = sqlite3.connect(db_file, timeout=30.0)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM file_index")
        index_count = cursor.fetchone()[0] or 0
        if index_count > 0:
            cursor.execute("DELETE FROM file_thumbnails WHERE path NOT IN (SELECT path FROM file_index)")
            t_cnt = max(0, cursor.rowcount)
            removed["thumbnails"] = t_cnt
            removed["orphaned_thumbnails"] = t_cnt
            cursor.execute("DELETE FROM file_tags WHERE path NOT IN (SELECT path FROM file_index)")
            g_cnt = max(0, cursor.rowcount)
            removed["tags"] = g_cnt
            removed["orphaned_tags"] = g_cnt
            conn.commit()
        conn.close()
        invalidate_tag_stats_cache(db_file)
    except Exception as e:
        logging.error(f"Error cleaning orphaned records: {e}")
    return removed


_CHAR_CACHE = {}
_SERIES_CACHE = {}
_SUGGESTIONS_CACHE = {}


def get_all_characters(db_file, min_count=1, max_age_seconds=60):
    """Returns list of unique character tags ordered by frequency with in-memory caching."""
    now = time.time()
    cache_key = (db_file, min_count)
    if cache_key in _CHAR_CACHE and (now - _CHAR_CACHE[cache_key][0]) < max_age_seconds:
        return list(_CHAR_CACHE[cache_key][1])

    chars = []
    try:
        conn = sqlite3.connect(db_file, timeout=10.0)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT tag_name, COUNT(*) as cnt 
            FROM file_tags 
            WHERE category = 4 AND tag_name != '__none__' 
            GROUP BY tag_name 
            HAVING cnt >= ? 
            ORDER BY cnt DESC, tag_name ASC
        """, (min_count,))
        chars = [row[0] for row in cursor.fetchall()]
        conn.close()
        _CHAR_CACHE[cache_key] = (now, chars)
    except Exception:
        pass
    return chars


def get_all_series(db_file, min_count=1, max_age_seconds=60):
    """Returns list of unique series/copyright tags ordered by frequency with in-memory caching."""
    now = time.time()
    cache_key = (db_file, min_count)
    if cache_key in _SERIES_CACHE and (now - _SERIES_CACHE[cache_key][0]) < max_age_seconds:
        return list(_SERIES_CACHE[cache_key][1])

    series = []
    try:
        conn = sqlite3.connect(db_file, timeout=10.0)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT tag_name, COUNT(*) as cnt 
            FROM file_tags 
            WHERE category = 3 AND tag_name != '__none__' 
            GROUP BY tag_name 
            HAVING cnt >= ? 
            ORDER BY cnt DESC, tag_name ASC
        """, (min_count,))
        series = [row[0] for row in cursor.fetchall()]
        conn.close()
        _SERIES_CACHE[cache_key] = (now, series)
    except Exception:
        pass
    return series


_TAG_STATS_CACHE = {}
_TAG_STATS_CACHE_TIME = {}


def invalidate_tag_stats_cache(db_file=None):
    """Clears the cached tag statistics, characters, series, and search suggestions."""
    global _TAG_STATS_CACHE, _TAG_STATS_CACHE_TIME, _CHAR_CACHE, _SERIES_CACHE, _SUGGESTIONS_CACHE
    if db_file:
        _TAG_STATS_CACHE.pop(db_file, None)
        _TAG_STATS_CACHE_TIME.pop(db_file, None)
        for k in [k for k in _CHAR_CACHE if k[0] == db_file]:
            _CHAR_CACHE.pop(k, None)
        for k in [k for k in _SERIES_CACHE if k[0] == db_file]:
            _SERIES_CACHE.pop(k, None)
        for k in [k for k in _SUGGESTIONS_CACHE if k[0] == db_file]:
            _SUGGESTIONS_CACHE.pop(k, None)
    else:
        _TAG_STATS_CACHE.clear()
        _TAG_STATS_CACHE_TIME.clear()
        _CHAR_CACHE.clear()
        _SERIES_CACHE.clear()
        _SUGGESTIONS_CACHE.clear()


def get_tag_stats(db_file, force_refresh=False, max_age_seconds=60):
    """Returns statistics of AI tagged images with in-memory caching."""
    global _TAG_STATS_CACHE, _TAG_STATS_CACHE_TIME
    now = time.time()
    if not force_refresh and db_file in _TAG_STATS_CACHE:
        if now - _TAG_STATS_CACHE_TIME.get(db_file, 0) < max_age_seconds:
            return dict(_TAG_STATS_CACHE[db_file])

    stats = {"total_tagged_files": 0, "unique_characters": 0, "unique_tags": 0}
    try:
        conn = sqlite3.connect(db_file, timeout=10.0)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(DISTINCT path) FROM file_tags WHERE tag_name != '__none__' AND category != 10")
        stats["total_tagged_files"] = cursor.fetchone()[0] or 0
        cursor.execute("SELECT COUNT(DISTINCT tag_name) FROM file_tags WHERE category = 4 AND tag_name != '__none__'")
        stats["unique_characters"] = cursor.fetchone()[0] or 0
        cursor.execute("SELECT COUNT(DISTINCT tag_name) FROM file_tags WHERE tag_name != '__none__' AND category != 10")
        stats["unique_tags"] = cursor.fetchone()[0] or 0
        conn.close()

        _TAG_STATS_CACHE[db_file] = stats
        _TAG_STATS_CACHE_TIME[db_file] = now
    except Exception:
        pass
    return stats


def get_all_search_suggestions(db_file, min_count=1, limit=2000, max_age_seconds=60):
    """Returns formatted list of suggestions for search auto-complete with in-memory caching."""
    now = time.time()
    cache_key = (db_file, min_count, limit)
    if cache_key in _SUGGESTIONS_CACHE and (now - _SUGGESTIONS_CACHE[cache_key][0]) < max_age_seconds:
        return list(_SUGGESTIONS_CACHE[cache_key][1])

    suggestions = []
    try:
        conn = sqlite3.connect(db_file, timeout=10.0)
        cursor = conn.cursor()
        syntax_suggestions = [
            "rating:sfw", "rating:nsfw", "rating:sensitive",
            "is:favorite", "is:fav",
            "type:mp4", "type:gif", "type:png", "type:video", "type:ugoira",
            "size:>5mb", "size:<2mb"
        ]
        suggestions.extend(syntax_suggestions)

        seen_suggestions = set(syntax_suggestions)
        # 1. Fast index-backed character & series lookup (< 300ms even on 7.7M rows)
        for c_name in get_all_characters(db_file, min_count=min_count):
            for s in (f"char:{c_name}", f"tag:{c_name}"):
                if s not in seen_suggestions:
                    suggestions.append(s)
                    seen_suggestions.add(s)

        for s_name in get_all_series(db_file, min_count=min_count):
            for s in (f"series:{s_name}", f"tag:{s_name}"):
                if s not in seen_suggestions:
                    suggestions.append(s)
                    seen_suggestions.add(s)

        # 2. Index-only scan on idx_tag_name for general tags (avoids 20s full-table GROUP BY on 7.7M rows)
        cursor.execute(
            "SELECT DISTINCT tag_name FROM file_tags WHERE tag_name != '__none__' AND tag_name != 'favorite' LIMIT ?",
            (limit,)
        )
        for (tag_name,) in cursor.fetchall():
            s = f"tag:{tag_name}"
            if s not in seen_suggestions:
                suggestions.append(s)
                seen_suggestions.add(s)

        # Add known artist names for artist: autocomplete
        try:
            cursor.execute("SELECT name FROM artists WHERE name IS NOT NULL AND name != '' AND name != '[Deleted User]' LIMIT 300")
            for (a_name,) in cursor.fetchall():
                clean_artist = a_name.strip().replace(' ', '_').lower()
                if clean_artist:
                    s = f"artist:{clean_artist}"
                    if s not in seen_suggestions:
                        suggestions.append(s)
                        seen_suggestions.add(s)
        except Exception:
            pass

        conn.close()
        _SUGGESTIONS_CACHE[cache_key] = (now, suggestions)
    except Exception:
        pass
    return suggestions


def get_dashboard_analytics(db_file):
    """
    Computes aggregated library statistics for the Visual Dashboard:
    total files, folders, tagged files, SFW/NSFW breakdown, favorites, top characters, top series, and top general tags.
    """
    data = {
        "total_files": 0,
        "total_folders": 0,
        "tagged_files": 0,
        "favorite_count": 0,
        "sfw_count": 0,
        "nsfw_count": 0,
        "sensitive_count": 0,
        "top_characters": [],
        "top_series": [],
        "top_tags": []
    }
    try:
        conn = sqlite3.connect(db_file, timeout=15.0)
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM file_index")
        data["total_files"] = cursor.fetchone()[0] or 0

        cursor.execute("SELECT COUNT(*) FROM thumbnails")
        data["total_folders"] = cursor.fetchone()[0] or 0

        cursor.execute("SELECT COUNT(DISTINCT path) FROM file_tags WHERE category != 10")
        raw_tagged = cursor.fetchone()[0] or 0
        if data["total_files"] > 0 and raw_tagged > data["total_files"]:
            # Exclude orphaned tag records not in file_index
            cursor.execute("""
                SELECT COUNT(DISTINCT ft.path)
                FROM file_tags ft
                INNER JOIN file_index fi ON ft.path = fi.path
                WHERE ft.category != 10
            """)
            data["tagged_files"] = cursor.fetchone()[0] or 0
        else:
            data["tagged_files"] = raw_tagged

        cursor.execute("SELECT COUNT(DISTINCT path) FROM file_tags WHERE category = 10 AND tag_name = 'favorite'")
        data["favorite_count"] = cursor.fetchone()[0] or 0

        # SFW / NSFW Counts
        from query_parser import NSFW_EXPLICIT_TAGS, SENSITIVE_ECCHI_TAGS
        nsfw_placeholders = ','.join(['?'] * len(NSFW_EXPLICIT_TAGS))
        cursor.execute(f"""
            SELECT COUNT(DISTINCT path) FROM file_tags 
            WHERE (category IN (5, 9) AND tag_name IN ('questionable', 'explicit'))
               OR (tag_name IN ({nsfw_placeholders}))
        """, NSFW_EXPLICIT_TAGS)
        nsfw = min(cursor.fetchone()[0] or 0, data["tagged_files"])
        data["nsfw_count"] = nsfw
        data["sfw_count"] = max(0, data["tagged_files"] - nsfw)

        # Sensitive Count
        sens_placeholders = ','.join(['?'] * len(SENSITIVE_ECCHI_TAGS))
        cursor.execute(f"""
            SELECT COUNT(DISTINCT path) FROM file_tags 
            WHERE (category IN (5, 9) AND tag_name = 'sensitive')
               OR (tag_name IN ({sens_placeholders}))
        """, SENSITIVE_ECCHI_TAGS)
        data["sensitive_count"] = cursor.fetchone()[0] or 0

        # Top Characters (Category 4)
        cursor.execute("""
            SELECT tag_name, COUNT(*) as cnt 
            FROM file_tags 
            WHERE category = 4 AND tag_name != '__none__' 
            GROUP BY tag_name 
            ORDER BY cnt DESC, tag_name ASC 
            LIMIT 25
        """)
        data["top_characters"] = cursor.fetchall()

        # Top Series (Category 3)
        cursor.execute("""
            SELECT tag_name, COUNT(*) as cnt 
            FROM file_tags 
            WHERE category = 3 AND tag_name != '__none__' 
            GROUP BY tag_name 
            ORDER BY cnt DESC, tag_name ASC 
            LIMIT 15
        """)
        data["top_series"] = cursor.fetchall()

        # Top General Tags (Category 0 & 11)
        cursor.execute("""
            SELECT tag_name, COUNT(*) as cnt 
            FROM file_tags 
            WHERE category IN (0, 11) AND tag_name != '__none__' 
            GROUP BY tag_name 
            ORDER BY cnt DESC, tag_name ASC 
            LIMIT 60
        """)
        data["top_tags"] = cursor.fetchall()

        conn.close()
    except Exception as e:
        logging.error(f"Error computing dashboard analytics: {e}")
    return data
