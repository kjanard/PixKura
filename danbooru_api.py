import os
import re
import json
import time
import logging
import threading
from pathlib import Path
from typing import Optional, Dict, Any, List
import requests
from requests.auth import HTTPBasicAuth

from config import AppConfig, CONFIG_FILE

logger = logging.getLogger("PixKura.DanbooruClient")


class DanbooruClient:
    """
    Danbooru REST API Client adapted from DanbuDownloader (DanbuDL).
    Adheres to official Danbooru API guidelines:
    - Standard User-Agent identification
    - HTTP Basic Authentication (RFC 7617)
    - Thread-safe polite rate limiting (~1 req/sec) to avoid HTTP 429
    - Reusable Session connection pooling
    - Exponential backoff on HTTP 429 (handling Retry-After) and 5xx errors
    - Lightweight field querying via 'only' parameter
    - Precise regex boundary URL verification to prevent ID prefix collisions
    """
    BASE_URL = "https://danbooru.donmai.us"
    SAFEBOORU_URL = "https://safebooru.donmai.us"

    def __init__(self, username: str = "", api_key: str = "", delay: float = 0.8):
        self.username = username.strip()
        self.api_key = api_key.strip()
        self.delay = max(0.5, delay)
        self.last_request_time = 0.0
        self._lock = threading.Lock()

        # Auto-load credentials if not supplied
        if not self.username:
            self._load_credentials()

        self.session = requests.Session()
        self._update_session_auth_and_headers()

    def _load_credentials(self):
        """
        Loads Danbooru API credentials from:
        1. Local test_pixiv config.json ('danbooru_username', 'danbooru_api_key')
        2. DanbuDownloader config.json ('api_username', 'api_key') if available
        """
        try:
            # 1. Try local config.json
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    u = cfg.get("danbooru_username", "").strip()
                    k = cfg.get("danbooru_api_key", "").strip()
                    if u:
                        self.username = u
                        self.api_key = k
                        return

            # 2. Try sibling DanbuDownloader project config.json
            danbudl_cfg = Path(__file__).resolve().parent.parent / "DanbuDownloader" / "config.json"
            if danbudl_cfg.exists():
                with open(danbudl_cfg, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    u = cfg.get("api_username", "").strip()
                    k = cfg.get("api_key", "").strip()
                    if u:
                        self.username = u
                        self.api_key = k
                        return
        except Exception as e:
            logger.debug(f"Could not load Danbooru credentials: {e}")

    def _update_session_auth_and_headers(self):
        """Configure User-Agent and Basic Auth according to Danbooru specifications."""
        version = getattr(AppConfig, "VERSION", "2.3.3")
        if self.username:
            user_agent = f"PixKura/{version} (DanbuDL-Engine; user: {self.username}; +https://danbooru.donmai.us/)"
        else:
            user_agent = f"PixKura/{version} (DanbuDL-Engine; anonymous; +https://danbooru.donmai.us/)"

        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept": "application/json, text/plain, */*",
        })

        if self.username and self.api_key:
            self.session.auth = HTTPBasicAuth(self.username, self.api_key)
        else:
            self.session.auth = None

    def _wait_rate_limit(self):
        """Enforce polite rate limiting between requests (thread-safe)."""
        with self._lock:
            elapsed = time.time() - self.last_request_time
            if elapsed < self.delay:
                time.sleep(self.delay - elapsed)
            self.last_request_time = time.time()

    def _request_with_retry(self, method: str, url: str, params: Optional[Dict] = None, max_retries: int = 3, **kwargs) -> Optional[requests.Response]:
        """Execute HTTP request with exponential backoff on 429 or transient server errors."""
        params = params or {}
        backoff = 2.0

        for attempt in range(max_retries):
            self._wait_rate_limit()
            try:
                response = self.session.request(method, url, params=params, timeout=12, **kwargs)

                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    wait_time = float(retry_after) if retry_after else backoff
                    logger.warning(f"Danbooru 429 User Throttled. Waiting {wait_time:.1f}s (Attempt {attempt+1}/{max_retries})")
                    time.sleep(wait_time)
                    backoff *= 2.0
                    continue

                if response.status_code in (500, 502, 503, 504):
                    logger.warning(f"Danbooru server error {response.status_code}. Retrying in {backoff:.1f}s...")
                    time.sleep(backoff)
                    backoff *= 1.5
                    continue

                return response

            except (requests.ConnectionError, requests.Timeout) as e:
                logger.warning(f"Network error querying Danbooru: {e}. Retrying in {backoff:.1f}s...")
                time.sleep(backoff)
                backoff *= 1.5

        return None

    def find_artist_by_pixiv_id(self, aid: str) -> Optional[str]:
        """
        Searches Danbooru for an artist matching the Pixiv User ID.
        Uses exact boundary verification against returned URLs to prevent prefix collisions.
        Returns formatted display name (e.g. 'Kiri Amai') or None.
        """
        aid = str(aid).strip()
        if not aid:
            return None

        # Danbooru search patterns
        patterns = [
            f"*pixiv.net*/{aid}*",
            f"*pixiv.net*id={aid}*"
        ]

        for pattern in patterns:
            url = f"{self.BASE_URL}/artists.json"
            params = {
                "search[url_matches]": pattern,
                "only": "id,name,other_names,urls",
                "limit": 5
            }
            try:
                res = self._request_with_retry("GET", url, params=params)
                if res and res.status_code == 200:
                    data = res.json()
                    if isinstance(data, list):
                        for artist in data:
                            # Verify that at least one associated URL exactly matches this user ID
                            urls = [u.get("url", "") for u in artist.get("urls", []) if isinstance(u, dict)]
                            if not urls and "url" in artist:
                                urls = [artist.get("url")]

                            matched = False
                            for target_url in urls:
                                if (re.search(rf'pixiv\.net/(?:users|fanbox/creator|u)/{aid}(?:/|$|\?|#)', target_url) or
                                    re.search(rf'pixiv\.net/.*[?&]id={aid}(?:&|$|#)', target_url) or
                                    re.search(rf'pixiv\.me/{aid}(?:/|$|\?|#)', target_url)):
                                    matched = True
                                    break

                            if matched:
                                raw_name = artist.get("name", "")
                                if raw_name:
                                    return raw_name.replace("_", " ").title()
            except Exception as e:
                logger.debug(f"Error querying Danbooru for ID {aid}: {e}")

        return None

    def find_artist_on_safebooru(self, aid: str) -> Optional[str]:
        """
        Fallback query to Safebooru using polite rate limiting.
        """
        aid = str(aid).strip()
        if not aid:
            return None

        patterns = [
            f"*pixiv.net*/{aid}*",
            f"*pixiv.net*id={aid}*"
        ]
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        for pattern in patterns:
            url = f"{self.SAFEBOORU_URL}/artists.json?search[url_matches]={pattern}"
            try:
                self._wait_rate_limit()
                res = requests.get(url, headers=headers, timeout=8)
                if res.status_code == 200:
                    data = res.json()
                    if isinstance(data, list) and len(data) > 0:
                        raw_name = data[0].get("name", "")
                        if raw_name:
                            return raw_name.replace("_", " ").title()
            except Exception as e:
                logger.debug(f"Error querying Safebooru for ID {aid}: {e}")
        return None
