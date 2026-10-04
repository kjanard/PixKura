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
        # เพิ่ม profile_blob ในตาราง artists
        cursor.execute('''CREATE TABLE IF NOT EXISTS artists (artist_id TEXT PRIMARY KEY, name TEXT, profile_blob BLOB)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS thumbnails (artist_id TEXT PRIMARY KEY, image_data BLOB, folder_mtime REAL, last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP, file_count INTEGER DEFAULT 0, folder_ctime REAL DEFAULT 0)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS file_index (path TEXT PRIMARY KEY, filename TEXT, parent_folder TEXT, mtime REAL, size INTEGER DEFAULT 0, ctime REAL DEFAULT 0)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS file_thumbnails (path TEXT PRIMARY KEY, image_data BLOB, last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        cursor.execute('''CREATE INDEX IF NOT EXISTS idx_filename ON file_index (filename)''')
        # ตารางจัดเก็บแท็ก AI
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
        try: cursor.execute("ALTER TABLE thumbnails ADD COLUMN file_count INTEGER DEFAULT 0")
        except sqlite3.OperationalError: pass
        try: cursor.execute("ALTER TABLE thumbnails ADD COLUMN folder_ctime REAL DEFAULT 0")
        except sqlite3.OperationalError: pass
        try: cursor.execute("ALTER TABLE file_index ADD COLUMN size INTEGER DEFAULT 0")
        except sqlite3.OperationalError: pass
        try: cursor.execute("ALTER TABLE file_index ADD COLUMN ctime REAL DEFAULT 0")
        except sqlite3.OperationalError: pass
        try: cursor.execute("ALTER TABLE file_index ADD COLUMN parent_folder TEXT")
        except sqlite3.OperationalError: pass
        try: cursor.execute("CREATE INDEX IF NOT EXISTS idx_parent ON file_index (parent_folder)")
        except sqlite3.OperationalError: pass
        try: cursor.execute('''CREATE TABLE IF NOT EXISTS file_thumbnails (path TEXT PRIMARY KEY, image_data BLOB, last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        except sqlite3.OperationalError: pass
        # V.25 Migration: เพิ่ม profile_blob
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
        try: cursor.execute("DELETE FROM file_tags WHERE tag_name = '__none__'")
        except sqlite3.OperationalError: pass
        self.conn.commit()

def get_file_tags(db_file, path):
    """Returns all AI tags for a specific file path."""
    tags = {"characters": [], "series": [], "general": [], "rating": "general", "all": []}
    try:
        conn = sqlite3.connect(db_file, timeout=10.0)
        cursor = conn.cursor()
        cursor.execute("SELECT tag_name, category, confidence FROM file_tags WHERE path = ? ORDER BY confidence DESC", (path,))
        for tag_name, category, conf in cursor.fetchall():
            if tag_name == "__none__": continue
            tags["all"].append((tag_name, category, conf))
            if category == 4:
                tags["characters"].append((tag_name, conf))
            elif category == 3:
                tags["series"].append((tag_name, conf))
            elif category == 0:
                tags["general"].append((tag_name, conf))
            elif category == 5:
                tags["rating"] = tag_name
        conn.close()
    except Exception:
        pass
    return tags

def get_all_characters(db_file, min_count=1):
    """Returns list of unique character tags ordered by frequency."""
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
    except Exception:
        pass
    return chars

def get_all_series(db_file, min_count=1):
    """Returns list of unique series/copyright tags ordered by frequency."""
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
    except Exception:
        pass
    return series

def get_tag_stats(db_file):
    """Returns statistics of AI tagged images."""
    stats = {"total_tagged_files": 0, "unique_characters": 0, "unique_tags": 0}
    try:
        conn = sqlite3.connect(db_file, timeout=10.0)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(DISTINCT path) FROM file_tags WHERE tag_name != '__none__'")
        stats["total_tagged_files"] = cursor.fetchone()[0] or 0
        cursor.execute("SELECT COUNT(DISTINCT tag_name) FROM file_tags WHERE category = 4 AND tag_name != '__none__'")
        stats["unique_characters"] = cursor.fetchone()[0] or 0
        cursor.execute("SELECT COUNT(DISTINCT tag_name) FROM file_tags WHERE tag_name != '__none__'")
        stats["unique_tags"] = cursor.fetchone()[0] or 0
        conn.close()
    except Exception:
        pass
    return stats

def get_all_search_suggestions(db_file, min_count=1, limit=2000):
    """Returns formatted list of suggestions for search auto-complete."""
    suggestions = []
    try:
        conn = sqlite3.connect(db_file, timeout=10.0)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT tag_name, category, COUNT(*) as cnt 
            FROM file_tags 
            WHERE tag_name != '__none__' 
            GROUP BY tag_name 
            HAVING cnt >= ? 
            ORDER BY cnt DESC, tag_name ASC
            LIMIT ?
        """, (min_count, limit))
        syntax_suggestions = [
            "rating:sfw", "rating:nsfw", "rating:sensitive",
            "type:gif", "type:png", "type:video", "type:ugoira",
            "size:>5mb", "size:<2mb"
        ]
        suggestions.extend(syntax_suggestions)

        for tag_name, category, _ in cursor.fetchall():
            suggestions.append(f"tag:{tag_name}")
            if category == 4:
                suggestions.append(f"char:{tag_name}")
        conn.close()
    except Exception:
        pass
    return suggestions


def get_dashboard_analytics(db_file):
    """
    Computes aggregated library statistics for the Visual Dashboard:
    total files, folders, tagged files, SFW/NSFW breakdown, top characters, top series, and top general tags.
    """
    data = {
        "total_files": 0,
        "total_folders": 0,
        "tagged_files": 0,
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

        cursor.execute("SELECT COUNT(DISTINCT path) FROM file_tags")
        data["tagged_files"] = cursor.fetchone()[0] or 0

        # SFW / NSFW Counts
        from query_parser import NSFW_EXPLICIT_TAGS, SENSITIVE_ECCHI_TAGS
        nsfw_placeholders = ','.join(['?'] * len(NSFW_EXPLICIT_TAGS))
        cursor.execute(f"""
            SELECT COUNT(DISTINCT path) FROM file_tags 
            WHERE (category IN (5, 9) AND tag_name IN ('questionable', 'explicit'))
               OR (tag_name IN ({nsfw_placeholders}))
        """, NSFW_EXPLICIT_TAGS)
        nsfw = cursor.fetchone()[0] or 0
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

        # Top General Tags (Category 0)
        cursor.execute("""
            SELECT tag_name, COUNT(*) as cnt 
            FROM file_tags 
            WHERE category = 0 AND tag_name != '__none__' 
            GROUP BY tag_name 
            ORDER BY cnt DESC, tag_name ASC 
            LIMIT 60
        """)
        data["top_tags"] = cursor.fetchall()

        conn.close()
    except Exception as e:
        logging.error(f"Error computing dashboard analytics: {e}")
    return data




