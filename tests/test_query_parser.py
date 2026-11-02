import unittest
import sqlite3
import os
import sys
import tempfile

# Ensure workspace root is in sys.path
WORKSPACE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKSPACE_DIR not in sys.path:
    sys.path.insert(0, WORKSPACE_DIR)

from query_parser import parse_search_query, resolve_matching_paths, SearchQuery
from config import DB_FILE, encrypt_secret, decrypt_secret, load_app_config, update_app_config
from database import (
    DatabaseSetup, is_file_favorite, toggle_file_favorite,
    add_custom_file_tag, remove_file_tag, cleanup_orphaned_records,
    get_file_tags
)

class TestQueryParser(unittest.TestCase):
    def test_parse_rating(self):
        q1 = parse_search_query("rating:sfw")
        self.assertEqual(q1.rating, "SFW")

        q2 = parse_search_query("rating:nsfw")
        self.assertEqual(q2.rating, "NSFW")

        q3 = parse_search_query("is:ecchi")
        self.assertEqual(q3.rating, "SENSITIVE")

        q4 = parse_search_query("is:safe")
        self.assertEqual(q4.rating, "SFW")

        q5 = parse_search_query("rating:explicit")
        self.assertEqual(q5.rating, "NSFW")

    def test_parse_character_and_series(self):
        q = parse_search_query('char:miku series:"blue archive" -glasses')
        self.assertIn("miku", q.include_chars)
        self.assertIn("blue_archive", q.include_series)
        self.assertIn("glasses", q.exclude_tags)

    def test_parse_artist_and_favorites(self):
        q = parse_search_query('artist:ask -artist:deleted_user is:fav type:mp4')
        self.assertIn("ask", q.include_artists)
        self.assertIn("deleted_user", q.exclude_artists)
        self.assertTrue(q.only_favorites)
        self.assertIn("mp4", q.file_types)

        q2 = parse_search_query("is:favorite author:kurito")
        self.assertTrue(q2.only_favorites)
        self.assertIn("kurito", q2.include_artists)

    def test_parse_size_and_type(self):
        q = parse_search_query("type:gif size:>5mb")
        self.assertIn("gif", q.file_types)
        self.assertEqual(q.min_size, 5 * 1024 * 1024)

        q2 = parse_search_query("type:video size:<500kb")
        self.assertTrue(bool({'mp4', 'webm'} & q2.file_types))
        self.assertEqual(q2.max_size, 500 * 1024)

    def test_parse_or_group(self):
        q = parse_search_query("cat_ears OR dog_ears")
        self.assertEqual(len(q.or_tag_groups), 1)
        self.assertEqual(q.or_tag_groups[0], ["cat_ears", "dog_ears"])

        # Chained OR and prefix stripping
        q2 = parse_search_query("tag:cat_ears OR tag:dog_ears OR fox_ears")
        self.assertEqual(q2.or_tag_groups, [["cat_ears", "dog_ears", "fox_ears"]])
        self.assertEqual(q2.include_tags, [])

        # || syntax
        q3 = parse_search_query("cat_ears || dog_ears")
        self.assertEqual(q3.or_tag_groups, [["cat_ears", "dog_ears"]])

    def test_parse_and_conjunction(self):
        q = parse_search_query("tag:crossed_legs AND tag:toes")
        self.assertIn("crossed_legs", q.include_tags)
        self.assertIn("toes", q.include_tags)
        self.assertNotIn("and", q.keywords)
        self.assertEqual(len(q.keywords), 0)

        q2 = parse_search_query("tag:crossed_legs and tag:toes")
        self.assertEqual(q2.include_tags, ["crossed_legs", "toes"])
        self.assertEqual(q2.keywords, [])

        q3 = parse_search_query("char:miku && series:vocaloid")
        self.assertEqual(q3.include_chars, ["miku"])
        self.assertEqual(q3.include_series, ["vocaloid"])
        self.assertEqual(q3.keywords, [])

    def test_parse_not_operator(self):
        # Standalone NOT operator
        q1 = parse_search_query("tag:swimsuit NOT tag:glasses")
        self.assertEqual(q1.include_tags, ["swimsuit"])
        self.assertEqual(q1.exclude_tags, ["glasses"])
        self.assertEqual(q1.keywords, [])

        q2 = parse_search_query("solo NOT glasses")
        self.assertEqual(q2.keywords, ["solo"])
        self.assertEqual(q2.exclude_tags, ["glasses"])

        # Exclamation mark negation
        q3 = parse_search_query("solo !glasses -hat")
        self.assertEqual(q3.keywords, ["solo"])
        self.assertIn("glasses", q3.exclude_tags)
        self.assertIn("hat", q3.exclude_tags)

    def test_parse_nor_operator(self):
        # NOR operator: neither A nor B
        q1 = parse_search_query("cat_ears NOR dog_ears")
        self.assertEqual(q1.keywords, [])
        self.assertIn("cat_ears", q1.exclude_tags)
        self.assertIn("dog_ears", q1.exclude_tags)

        q2 = parse_search_query("tag:cat_ears NOR tag:dog_ears")
        self.assertEqual(q2.include_tags, [])
        self.assertIn("cat_ears", q2.exclude_tags)
        self.assertIn("dog_ears", q2.exclude_tags)

        q3 = parse_search_query("char:miku NOR char:luka")
        self.assertEqual(q3.include_chars, [])
        self.assertIn("miku", q3.exclude_chars)
        self.assertIn("luka", q3.exclude_chars)

    def test_resolve_artist_favorites_and_orphans_on_temp_db(self):
        fd, temp_db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            DatabaseSetup(temp_db)
            conn = sqlite3.connect(temp_db)
            cur = conn.cursor()
            cur.execute("INSERT INTO artists (artist_id, name) VALUES ('12345', 'Ask_Art')")
            cur.execute("INSERT INTO file_index (path, filename, parent_folder, mtime, size) VALUES (?, ?, ?, ?, ?)",
                        ("D:\\Pixiv\\12345\\img1.png", "img1.png", "D:\\Pixiv\\12345", 100.0, 2048))
            cur.execute("INSERT INTO file_index (path, filename, parent_folder, mtime, size) VALUES (?, ?, ?, ?, ?)",
                        ("D:\\Pixiv\\99999\\img2.png", "img2.png", "D:\\Pixiv\\99999", 100.0, 2048))
            # Insert an orphaned tag row not in file_index
            cur.execute("INSERT INTO file_tags (path, tag_name, category, confidence) VALUES (?, ?, ?, ?)",
                        ("D:\\Pixiv\\deleted\\ghost.png", "1girl", 0, 0.99))
            conn.commit()
            conn.close()

            # Test favorite toggle & custom tag
            self.assertFalse(is_file_favorite(temp_db, "D:\\Pixiv\\12345\\img1.png"))
            new_state = toggle_file_favorite(temp_db, "D:\\Pixiv\\12345\\img1.png")
            self.assertTrue(new_state)
            self.assertTrue(is_file_favorite(temp_db, "D:\\Pixiv\\12345\\img1.png"))

            self.assertTrue(add_custom_file_tag(temp_db, "D:\\Pixiv\\12345\\img1.png", "wallpaper masterpiece"))
            tags_info = get_file_tags(temp_db, "D:\\Pixiv\\12345\\img1.png")
            self.assertTrue(tags_info["is_favorite"])
            self.assertIn(("wallpaper_masterpiece", 1.0), tags_info["custom"])

            conn = sqlite3.connect(temp_db)
            try:
                # Resolve artist:ask_art
                q_art = parse_search_query("artist:ask_art")
                art_paths = resolve_matching_paths(conn, q_art)
                self.assertEqual(art_paths, {"D:\\Pixiv\\12345\\img1.png"})

                # Resolve is:fav
                q_fav = parse_search_query("is:fav")
                fav_paths = resolve_matching_paths(conn, q_fav)
                self.assertEqual(fav_paths, {"D:\\Pixiv\\12345\\img1.png"})

                # Resolve custom tag
                q_custom = parse_search_query("tag:wallpaper_masterpiece")
                custom_paths = resolve_matching_paths(conn, q_custom)
                self.assertEqual(custom_paths, {"D:\\Pixiv\\12345\\img1.png"})
            finally:
                conn.close()

            # Test orphan cleanup
            removed = cleanup_orphaned_records(temp_db)
            self.assertGreaterEqual(removed["orphaned_tags"], 1)

            # Remove custom tag
            self.assertTrue(remove_file_tag(temp_db, "D:\\Pixiv\\12345\\img1.png", "wallpaper_masterpiece"))
        finally:
            if os.path.exists(temp_db):
                try: os.remove(temp_db)
                except Exception: pass

    def test_dpapi_secret_encryption_and_atomic_config(self):
        secret = "12345678_secret_pixiv_phpsessid_token"
        enc = encrypt_secret(secret)
        if os.name == "nt":
            self.assertTrue(enc.startswith("dpapi:"), "Should encrypt with Windows DPAPI prefix on Windows")
            self.assertNotEqual(enc, secret)
        dec = decrypt_secret(enc)
        self.assertEqual(dec, secret)

        # Plaintext backward compatibility
        self.assertEqual(decrypt_secret("plain_legacy_value"), "plain_legacy_value")

        # Atomic config read/write test on temporary file
        fd, temp_cfg = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            ok = update_app_config({"pixiv_cookie": secret, "custom_key": 42}, config_path=temp_cfg)
            self.assertTrue(ok)
            loaded = load_app_config(config_path=temp_cfg)
            self.assertEqual(loaded.get("pixiv_cookie"), secret)
            self.assertEqual(loaded.get("custom_key"), 42)
        finally:
            if os.path.exists(temp_cfg):
                try: os.remove(temp_cfg)
                except Exception: pass

    def test_db_resolve_matching_paths(self):
        if not os.path.exists(DB_FILE):
            self.skipTest("Database file not found.")

        conn = sqlite3.connect(DB_FILE)
        try:
            # Test SFW resolution
            q_sfw = parse_search_query("rating:sfw")
            sfw_paths = resolve_matching_paths(conn, q_sfw)
            self.assertIsNotNone(sfw_paths)
            self.assertGreater(len(sfw_paths), 0)

            # Test NSFW resolution
            q_nsfw = parse_search_query("rating:nsfw")
            nsfw_paths = resolve_matching_paths(conn, q_nsfw)
            self.assertIsNotNone(nsfw_paths)
            self.assertGreater(len(nsfw_paths), 0)

            # SFW and NSFW must have zero intersection (strict safety guarantee)
            overlap = sfw_paths & nsfw_paths
            self.assertEqual(len(overlap), 0, f"Found {len(overlap)} files overlapping between SFW and NSFW!")

            # Test character filtering
            q_char = parse_search_query("char:hatsune_miku rating:sfw")
            char_paths = resolve_matching_paths(conn, q_char)
            self.assertIsNotNone(char_paths)
            self.assertTrue(char_paths.issubset(sfw_paths))

        finally:
            conn.close()

if __name__ == "__main__":
    unittest.main()

