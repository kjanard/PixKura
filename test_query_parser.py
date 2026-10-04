import unittest
import sqlite3
import os
import sys

from query_parser import parse_search_query, resolve_matching_paths, SearchQuery
from config import DB_FILE

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
