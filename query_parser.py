import os
import re
import shlex
import sqlite3
import logging

class SearchQuery:
    def __init__(self):
        self.raw_text = ""
        self.keywords = []          # General words (filename or tag search)
        self.include_tags = []       # General tags to require (AND)
        self.exclude_tags = []       # Tags to exclude (NOT)
        self.include_chars = []      # Characters to require
        self.exclude_chars = []      # Characters to exclude
        self.include_series = []     # Series/Copyright to require
        self.include_artists = []    # Artist name / ID
        self.rating = None           # 'ALL', 'SFW', 'NSFW', 'SENSITIVE', 'QUESTIONABLE', 'EXPLICIT'
        self.min_size = None         # Bytes
        self.max_size = None         # Bytes
        self.file_types = set()      # e.g. {'jpg', 'png'}, {'gif'}, {'video'}, {'zip'}
        self.or_tag_groups = []      # List of lists of alternative tags: [[t1, t2], [t3, t4]]

    def has_tag_or_rating_filter(self, active_rating=None, active_char=None):
        return (
            bool(self.include_tags) or
            bool(self.exclude_tags) or
            bool(self.include_chars) or
            bool(self.exclude_chars) or
            bool(self.include_series) or
            bool(self.or_tag_groups) or
            (self.rating is not None and self.rating != 'ALL') or
            (active_rating is not None and active_rating != 'ALL') or
            bool(active_char)
        )

# Explicit NSFW tags for Danbooru classification
NSFW_EXPLICIT_TAGS = (
    'nude', 'nipples', 'pussy', 'penis', 'sex', 'fellatio', 'masturbation', 
    'areola', 'cum', 'breasts_out', 'topless', 'bottomless', 'spread_legs', 
    'explicit', 'nsfw', 'uncensored', 'dildo', 'penetration', 'paizuri',
    'anus', 'anal', 'fingering', 'lactation', 'cameltoe', 'erection'
)

SENSITIVE_ECCHI_TAGS = (
    'sensitive', 'cleavage', 'bikini', 'swimsuit', 'panties', 'underwear', 
    'lingerie', 'bra', 'sideboob', 'underboob', 'thong', 'ass', 'crotch'
)

def parse_size_str(s):
    s = s.strip().lower()
    mult = 1
    if s.endswith('gb') or s.endswith('g'):
        mult = 1024 * 1024 * 1024
        s = s.rstrip('gb')
    elif s.endswith('mb') or s.endswith('m'):
        mult = 1024 * 1024
        s = s.rstrip('mb')
    elif s.endswith('kb') or s.endswith('k'):
        mult = 1024
        s = s.rstrip('kb')
    elif s.endswith('b'):
        s = s.rstrip('b')
    try:
        return int(float(s) * mult)
    except Exception:
        return None

def clean_tag_token(token):
    """Strips leading -, !, or tag: prefix and formats for DB matching."""
    t = token.lstrip('-!').strip()
    if t.lower().startswith(('tag:', 'char:', 'character:', 'series:', 'copyright:', 'artist:', 'author:')):
        t = t.split(':', 1)[1]
    return t.replace(' ', '_').lower()

def parse_search_query(text):
    """
    Parses user input into a structured SearchQuery object.
    Supports prefixes, quoted phrases, AND, OR, NOT, NOR operators.
    """
    query = SearchQuery()
    if not text:
        return query

    query.raw_text = text.strip()

    try:
        raw_tokens = shlex.split(text)
    except Exception:
        raw_tokens = text.split()

    i = 0
    while i < len(raw_tokens):
        token = raw_tokens[i].strip()
        if not token:
            i += 1
            continue

        # 1. Explicit AND / && conjunction operator (AND logic is default)
        if token.upper() in ('AND', '&&') and len(raw_tokens) > 1:
            i += 1
            continue

        # 2. NOR operator (neither A nor B == NOT A AND NOT B)
        if token.upper() == 'NOR' and i > 0 and i + 1 < len(raw_tokens):
            prev_token = raw_tokens[i - 1].strip()
            next_token = raw_tokens[i + 1].strip()
            clean_prev = clean_tag_token(prev_token)
            clean_next = clean_tag_token(next_token)
            for lst in (query.keywords, query.include_tags, query.include_chars):
                if clean_prev in lst: lst.remove(clean_prev)
            if prev_token.lower().startswith(('char:', 'character:')):
                if clean_prev not in query.exclude_chars: query.exclude_chars.append(clean_prev)
            else:
                if clean_prev not in query.exclude_tags: query.exclude_tags.append(clean_prev)
            if next_token.lower().startswith(('char:', 'character:')):
                if clean_next not in query.exclude_chars: query.exclude_chars.append(clean_next)
            else:
                if clean_next not in query.exclude_tags: query.exclude_tags.append(clean_next)
            i += 2
            continue

        # 3. OR / || operator (A OR B, tag:A OR tag:B, A || B)
        if token.upper() in ('OR', '||') and i > 0 and i + 1 < len(raw_tokens):
            prev_token = raw_tokens[i - 1].strip()
            next_token = raw_tokens[i + 1].strip()
            clean_prev = clean_tag_token(prev_token)
            clean_next = clean_tag_token(next_token)
            for lst in (query.keywords, query.include_tags, query.include_chars):
                if clean_prev in lst: lst.remove(clean_prev)
            if query.or_tag_groups and clean_prev in query.or_tag_groups[-1]:
                query.or_tag_groups[-1].append(clean_next)
            else:
                query.or_tag_groups.append([clean_prev, clean_next])
            i += 2
            continue

        # 4. Standalone NOT / ! operator (e.g. tag:a NOT tag:b, solo NOT glasses)
        is_standalone_not = False
        if token.upper() in ('NOT', '!') and i + 1 < len(raw_tokens):
            is_standalone_not = True
            token = raw_tokens[i + 1].strip()
            i += 1

        is_negated = is_standalone_not or (token.startswith(('-', '!')) and len(token) > 1)
        clean_token = token.lstrip('-!') if is_negated else token
        lower_token = clean_token.lower()

        # 1. Character
        if lower_token.startswith(('char:', 'character:')):
            val = clean_token.split(':', 1)[1].strip().replace(' ', '_').lower()
            if val:
                if is_negated: query.exclude_chars.append(val)
                else: query.include_chars.append(val)

        # 2. Series / Copyright
        elif lower_token.startswith(('series:', 'copyright:')):
            val = clean_token.split(':', 1)[1].strip().replace(' ', '_').lower()
            if val: query.include_series.append(val)

        # 3. Artist / Author
        elif lower_token.startswith(('artist:', 'author:')):
            val = clean_token.split(':', 1)[1].strip().replace(' ', '_').lower()
            if val: query.include_artists.append(val)

        # 4. Tag
        elif lower_token.startswith('tag:'):
            val = clean_token.split(':', 1)[1].strip().replace(' ', '_').lower()
            if val:
                if is_negated: query.exclude_tags.append(val)
                else: query.include_tags.append(val)

        # 5. Rating / Safety
        elif lower_token.startswith(('rating:', 'is:', 'r:')):
            val = clean_token.split(':', 1)[1].strip().lower()
            if val in ('sfw', 'safe', 'general'):
                query.rating = 'NSFW' if is_negated else 'SFW'
            elif val in ('nsfw', 'r18', 'r-18', 'explicit'):
                query.rating = 'SFW' if is_negated else 'NSFW'
            elif val in ('sensitive', 'ecchi'):
                query.rating = 'SENSITIVE'
            elif val in ('questionable',):
                query.rating = 'QUESTIONABLE'

        # 6. Size
        elif lower_token.startswith('size:'):
            val = clean_token.split(':', 1)[1].strip()
            if val.startswith(('>', '>=')):
                sz = parse_size_str(val.lstrip('>='))
                if sz: query.min_size = sz
            elif val.startswith(('<', '<=')):
                sz = parse_size_str(val.lstrip('<='))
                if sz: query.max_size = sz

        # 7. File Type
        elif lower_token.startswith(('type:', 'ext:')):
            val = clean_token.split(':', 1)[1].strip().lower().lstrip('.')
            if val in ('photo', 'image', 'img'):
                query.file_types.update({'jpg', 'jpeg', 'png', 'webp', 'bmp'})
            elif val in ('gif',):
                query.file_types.add('gif')
            elif val in ('video', 'vid', 'mp4', 'webm', 'mov'):
                query.file_types.update({'mp4', 'webm', 'mov', 'avi', 'mkv'})
            elif val in ('zip', 'ugoira', 'anim', 'animate'):
                query.file_types.update({'zip', 'ugoira', 'gif', 'mp4', 'webm'})
            else:
                query.file_types.add(val)

        # 8. Negated general tag: -tag or !tag
        elif is_negated:
            val = clean_token.replace(' ', '_').lower()
            query.exclude_tags.append(val)

        # 9. Plain keyword (matches filename or tag)
        else:
            val = clean_token.replace(' ', '_').lower()
            query.keywords.append(val)

        i += 1

    return query


def resolve_matching_paths(conn, query, active_rating='ALL', active_char=None):
    """
    Executes an optimized multi-tag and rating query against the SQLite database.
    Returns:
        set[str] of matching file paths, OR None if no tag/rating restrictions apply.
    """
    effective_rating = query.rating or active_rating or 'ALL'
    all_include_chars = list(query.include_chars)
    if active_char and active_char not in all_include_chars:
        all_include_chars.append(active_char)

    has_tag_filters = (
        bool(query.include_tags) or
        bool(query.exclude_tags) or
        bool(all_include_chars) or
        bool(query.exclude_chars) or
        bool(query.include_series) or
        bool(query.or_tag_groups) or
        (effective_rating and effective_rating != 'ALL')
    )

    if not has_tag_filters:
        return None

    cursor = conn.cursor()
    matched_paths = None

    # 1. Require Characters (AND)
    for ch in all_include_chars:
        try:
            cursor.execute("SELECT DISTINCT path FROM file_tags WHERE (category = 4 OR category = 0) AND (tag_name = ? OR tag_name LIKE ?)",
                           (ch, f"%{ch}%"))
            paths = {r[0] for r in cursor.fetchall()}
            matched_paths = paths if matched_paths is None else (matched_paths & paths)
        except Exception as e:
            logging.error(f"Error querying character '{ch}': {e}")

    # 2. Require Series (AND)
    for sr in query.include_series:
        try:
            cursor.execute("SELECT DISTINCT path FROM file_tags WHERE category = 3 AND (tag_name = ? OR tag_name LIKE ?)",
                           (sr, f"%{sr}%"))
            paths = {r[0] for r in cursor.fetchall()}
            matched_paths = paths if matched_paths is None else (matched_paths & paths)
        except Exception as e:
            logging.error(f"Error querying series '{sr}': {e}")

    # 3. Require General Tags (AND)
    for tg in query.include_tags:
        try:
            cursor.execute("SELECT DISTINCT path FROM file_tags WHERE tag_name = ? OR tag_name LIKE ?",
                           (tg, f"%{tg}%"))
            paths = {r[0] for r in cursor.fetchall()}
            matched_paths = paths if matched_paths is None else (matched_paths & paths)
        except Exception as e:
            logging.error(f"Error querying tag '{tg}': {e}")

    # 4. OR Tag Groups
    for or_group in query.or_tag_groups:
        try:
            group_paths = set()
            for t in or_group:
                cursor.execute("SELECT DISTINCT path FROM file_tags WHERE tag_name = ? OR tag_name LIKE ?",
                               (t, f"%{t}%"))
                group_paths.update(r[0] for r in cursor.fetchall())
            matched_paths = group_paths if matched_paths is None else (matched_paths & group_paths)
        except Exception as e:
            logging.error(f"Error querying OR group {or_group}: {e}")

    # 5. Rating / Safety Filter
    if effective_rating and effective_rating != 'ALL':
        try:
            if effective_rating == 'SFW':
                # SFW: Must NOT have any explicit NSFW tags or category 5/9 explicit/questionable
                nsfw_placeholders = ','.join(['?'] * len(NSFW_EXPLICIT_TAGS))
                cursor.execute(f"""
                    SELECT DISTINCT path FROM file_tags 
                    WHERE (category IN (5, 9) AND tag_name IN ('questionable', 'explicit'))
                       OR (tag_name IN ({nsfw_placeholders}))
                """, NSFW_EXPLICIT_TAGS)
                nsfw_set = {r[0] for r in cursor.fetchall()}

                if matched_paths is not None:
                    matched_paths = matched_paths - nsfw_set
                else:
                    # Get all scanned paths minus NSFW
                    cursor.execute("SELECT DISTINCT path FROM file_tags")
                    all_scanned = {r[0] for r in cursor.fetchall()}
                    matched_paths = all_scanned - nsfw_set

            elif effective_rating == 'NSFW':
                # NSFW: Has explicit/questionable rating or any explicit NSFW tags
                nsfw_placeholders = ','.join(['?'] * len(NSFW_EXPLICIT_TAGS))
                cursor.execute(f"""
                    SELECT DISTINCT path FROM file_tags 
                    WHERE (category IN (5, 9) AND tag_name IN ('questionable', 'explicit'))
                       OR (tag_name IN ({nsfw_placeholders}))
                """, NSFW_EXPLICIT_TAGS)
                nsfw_paths = {r[0] for r in cursor.fetchall()}
                matched_paths = nsfw_paths if matched_paths is None else (matched_paths & nsfw_paths)

            elif effective_rating == 'SENSITIVE':
                # SENSITIVE: Sensitive/Ecchi tags
                sens_placeholders = ','.join(['?'] * len(SENSITIVE_ECCHI_TAGS))
                cursor.execute(f"""
                    SELECT DISTINCT path FROM file_tags 
                    WHERE (category IN (5, 9) AND tag_name = 'sensitive')
                       OR (tag_name IN ({sens_placeholders}))
                """, SENSITIVE_ECCHI_TAGS)
                sens_paths = {r[0] for r in cursor.fetchall()}
                matched_paths = sens_paths if matched_paths is None else (matched_paths & sens_paths)

            elif effective_rating in ('QUESTIONABLE', 'EXPLICIT'):
                target_tag = effective_rating.lower()
                cursor.execute("SELECT DISTINCT path FROM file_tags WHERE category IN (5, 9) AND tag_name = ?", (target_tag,))
                r_paths = {r[0] for r in cursor.fetchall()}
                matched_paths = r_paths if matched_paths is None else (matched_paths & r_paths)

        except Exception as e:
            logging.error(f"Error applying rating filter '{effective_rating}': {e}")

    # 6. Exclude Tags & Characters (NOT / Negation / NOR)
    all_excludes = set(query.exclude_tags) | set(query.exclude_chars)
    if all_excludes:
        try:
            placeholders = ','.join(['?'] * len(all_excludes))
            cursor.execute(f"SELECT DISTINCT path FROM file_tags WHERE tag_name IN ({placeholders})", list(all_excludes))
            excluded_paths = {r[0] for r in cursor.fetchall()}
            if matched_paths is not None:
                matched_paths = matched_paths - excluded_paths
            else:
                cursor.execute("SELECT DISTINCT path FROM file_tags")
                all_scanned = {r[0] for r in cursor.fetchall()}
                matched_paths = all_scanned - excluded_paths
        except Exception as e:
            logging.error(f"Error excluding tags: {e}")

    return matched_paths if matched_paths is not None else set()
