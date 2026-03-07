"""
MangaFire API - Python implementation based on Kotlin version at https://github.com/yuzono/tachiyomi-extensions
FastAPI-based manga reader API for MangaFire.to
"""

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from typing import Optional, List
from enum import Enum
from pydantic import BaseModel
import aiohttp
from bs4 import BeautifulSoup
import re
from datetime import datetime
from io import BytesIO
import math
import asyncio
import threading
import json
import logging
from urllib.parse import urlparse, parse_qs, urlencode

# Configure logging for page fetching diagnostics
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('mangafire.pages')
logger.setLevel(logging.DEBUG)

# Optional PIL support for image descrambling
try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# Playwright for headless browser VRF bypass
#
# Uses sync_api + asyncio.to_thread() instead of async_api because:
#   1. async_playwright().start() calls asyncio.create_subprocess_exec() which
#      fails with NotImplementedError on Windows where uvicorn forces
#      SelectorEventLoop (it does not support subprocess creation).
#   2. The original code stored the playwright instance as a local variable
#      inside get_browser(), so close_browser() could never call
#      playwright.stop() — leaking Node.js driver subprocesses on every
#      browser restart (affects ALL platforms).
#   3. No concurrency guard on get_browser() meant parallel requests could
#      race and create duplicate browser instances.
#
# sync_api avoids all three issues: subprocess spawning goes through the
# normal subprocess module (works on any event loop), the instance is
# stored as a class attribute, and a threading.Lock serialises init.
#
try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

app = FastAPI(
    title="MangaFire API",
    description="API for fetching manga from MangaFire.to",
    version="1.0.0"
)

# ==================== Constants ====================
BASE_URL = "https://mangafire.to"

# Supported Languages
SUPPORTED_LANGUAGES = {
    "en": "en",
    "es": "es",
    "es-la": "es-la",
    "fr": "fr",
    "ja": "ja",
    "pt": "pt",
    "pt-br": "pt-br",
}

# ==================== Enums ====================
class MangaType(str, Enum):
    MANGA = "manga"
    ONE_SHOT = "one_shot"
    DOUJINSHI = "doujinshi"
    NOVEL = "novel"
    MANHWA = "manhwa"
    MANHUA = "manhua"

class MangaStatus(str, Enum):
    COMPLETED = "completed"
    RELEASING = "releasing"
    ON_HIATUS = "on_hiatus"
    DISCONTINUED = "discontinued"
    NOT_YET_PUBLISHED = "info"

class SortOrder(str, Enum):
    MOST_RELEVANCE = "most_relevance"
    RECENTLY_UPDATED = "recently_updated"
    RECENTLY_ADDED = "recently_added"
    RELEASE_DATE = "release_date"
    TRENDING = "trending"
    TITLE_AZ = "title_az"
    SCORES = "scores"
    MAL_SCORES = "mal_scores"
    MOST_VIEWED = "most_viewed"
    MOST_FAVOURITED = "most_favourited"

# Genre mapping
GENRES = {
    "action": "1",
    "adventure": "78",
    "avant_garde": "3",
    "boys_love": "4",
    "comedy": "5",
    "demons": "77",
    "drama": "6",
    "ecchi": "7",
    "fantasy": "79",
    "girls_love": "9",
    "gourmet": "10",
    "harem": "11",
    "horror": "530",
    "isekai": "13",
    "iyashikei": "531",
    "josei": "15",
    "kids": "532",
    "magic": "539",
    "mahou_shoujo": "533",
    "martial_arts": "534",
    "mecha": "19",
    "military": "535",
    "music": "21",
    "mystery": "22",
    "parody": "23",
    "psychological": "536",
    "reverse_harem": "25",
    "romance": "26",
    "school": "73",
    "sci_fi": "28",
    "seinen": "537",
    "shoujo": "30",
    "shounen": "31",
    "slice_of_life": "538",
    "space": "33",
    "sports": "34",
    "super_power": "75",
    "supernatural": "76",
    "suspense": "37",
    "thriller": "38",
    "vampire": "39",
}


# ==================== Models ====================
class MangaBasic(BaseModel):
    """Basic manga information from search results"""
    id: str
    title: str
    url: str
    thumbnail_url: Optional[str] = None


class MangaDetails(BaseModel):
    """Detailed manga information"""
    id: str
    title: str
    url: str
    thumbnail_url: Optional[str] = None
    status: Optional[str] = None
    description: Optional[str] = None
    author: Optional[str] = None
    genres: Optional[List[str]] = None
    alternative_title: Optional[str] = None


class Chapter(BaseModel):
    """Chapter information"""
    id: str
    number: float
    name: str
    url: str
    date_upload: Optional[str] = None


class Page(BaseModel):
    """Page information"""
    index: int
    url: str
    is_scrambled: bool = False
    scramble_offset: int = 0


class SearchResult(BaseModel):
    """Search result with pagination"""
    manga_list: List[MangaBasic]
    has_next_page: bool
    current_page: int


class ChapterList(BaseModel):
    """List of chapters"""
    chapters: List[Chapter]
    manga_id: str
    language: str


class PageList(BaseModel):
    """List of pages for a chapter"""
    pages: List[Page]
    chapter_id: str


class ErrorResponse(BaseModel):
    """Error response model"""
    error: str
    detail: str
    status_code: int


# ==================== Image Descrambler ====================
class ImageDescrambler:
    """Descramble MangaFire images (requires Pillow)"""
    PIECE_SIZE = 200
    MIN_SPLIT_COUNT = 5
    
    @staticmethod
    def ceil_div(a: int, b: int) -> int:
        return (a + (b - 1)) // b
    
    @classmethod
    async def descramble(cls, image_data: bytes, offset: int) -> bytes:
        """Descramble an image with given offset"""
        if not PIL_AVAILABLE:
            raise HTTPException(
                status_code=501,
                detail="Image descrambling requires Pillow. Install with: pip install Pillow"
            )
        
        img = Image.open(BytesIO(image_data))
        width, height = img.size
        
        result = Image.new('RGB', (width, height))
        
        piece_width = min(cls.PIECE_SIZE, cls.ceil_div(width, cls.MIN_SPLIT_COUNT))
        piece_height = min(cls.PIECE_SIZE, cls.ceil_div(height, cls.MIN_SPLIT_COUNT))
        x_max = cls.ceil_div(width, piece_width) - 1
        y_max = cls.ceil_div(height, piece_height) - 1
        
        for y in range(y_max + 1):
            for x in range(x_max + 1):
                x_dst = piece_width * x
                y_dst = piece_height * y
                w = min(piece_width, width - x_dst)
                h = min(piece_height, height - y_dst)
                
                if x == x_max:
                    x_src = piece_width * x
                else:
                    x_src = piece_width * ((x_max - x + offset) % x_max)
                    
                if y == y_max:
                    y_src = piece_height * y
                else:
                    y_src = piece_height * ((y_max - y + offset) % y_max)
                
                # Crop from source position
                piece = img.crop((x_src, y_src, x_src + w, y_src + h))
                # Paste at destination position
                result.paste(piece, (x_dst, y_dst))
        
        output = BytesIO()
        result.save(output, format='JPEG', quality=90)
        return output.getvalue()


# ==================== VRF Token Helper (Headless Browser) ====================
class VRFHelper:
    """
    Helper class to extract VRF tokens using headless browser.
    MangaFire uses VRF tokens for search and page requests to prevent scraping.
    This mimics the WebView approach used in the Kotlin version.

    All Playwright work is done through the **sync** API inside a worker thread
    (via asyncio.to_thread) so that:
      - The browser subprocess is spawned outside the async event loop
        (fixing Windows NotImplementedError).
      - The Playwright instance is properly tracked and stopped on cleanup
        (fixing Node.js subprocess leaks).
      - A threading.Lock prevents duplicate initialisation from concurrent
        requests.
    
    VRF Cache: Uses a size-limited dict (max 20 entries) matching Kotlin's
    LinkedHashMap behavior with automatic removal of eldest entries.
    """

    _lock = threading.Lock()
    _playwright = None
    _browser = None
    _context = None
    _vrf_cache = {}
    _search_vrf_cache = {}
    _MAX_CACHE_SIZE = 20  # Match Kotlin's cache size limit

    # ---- low-level sync helpers (run inside a worker thread) ----

    @classmethod
    def _ensure_browser_sync(cls):
        """Ensure the sync browser/context is initialised (called in thread).
        
        Note: The browser allows specific resource loading to match Kotlin WebViewHelper:
        1. Main page URL (chapter page)
        2. Scripts from mfcdn.cc (MangaFire CDN)
        3. jQuery scripts from cloudflare.com
        4. Specific AJAX requests based on intercept callback
        All other requests should be blocked for performance.
        """
        with cls._lock:
            if cls._browser is not None:
                # Check if browser is still alive
                try:
                    cls._browser.contexts  # will throw if browser crashed
                    return
                except Exception:
                    logger.warning("[VRF] Browser crashed, restarting...")
                    cls._browser = None
                    cls._context = None
                    if cls._playwright:
                        try:
                            cls._playwright.stop()
                        except Exception:
                            pass
                        cls._playwright = None
            cls._playwright = sync_playwright().start()
            cls._browser = cls._playwright.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-accelerated-2d-canvas',
                    '--disable-gpu',
                    '--no-first-run',
                    '--no-zygote',
                ]
            )
            cls._context = cls._browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
                ignore_https_errors=True
            )

    @classmethod
    def _close_browser_sync(cls):
        """Close browser and Playwright driver (called in thread)."""
        with cls._lock:
            if cls._context:
                cls._context.close()
                cls._context = None
            if cls._browser:
                cls._browser.close()
                cls._browser = None
            if cls._playwright:
                cls._playwright.stop()
                cls._playwright = None

    @classmethod
    def _get_search_vrf_sync(cls, query: str) -> str:
        """Blocking VRF extraction for a search query."""
        cls._ensure_browser_sync()
        page = cls._context.new_page()
        vrf_token = None

        try:
            def handle_request(request):
                nonlocal vrf_token
                url = request.url
                if "mangafire.to" in url and "ajax/manga/search" in url:
                    parsed = urlparse(url)
                    params = parse_qs(parsed.query)
                    if 'vrf' in params:
                        vrf_token = params['vrf'][0]

            page.on("request", handle_request)
            page.goto(f"{BASE_URL}/home", wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(1000)
            search_input = page.locator(".search-inner input[name=keyword]")
            search_input.fill(query)
            search_input.press("Enter")
            for _ in range(10):
                if vrf_token:
                    break
                page.wait_for_timeout(500)

            if vrf_token:
                cls._search_vrf_cache[query] = vrf_token
                # Enforce cache size limit (matching Kotlin's LinkedHashMap with max 20 entries)
                if len(cls._search_vrf_cache) > cls._MAX_CACHE_SIZE:
                    # Remove oldest entry
                    oldest_key = next(iter(cls._search_vrf_cache))
                    del cls._search_vrf_cache[oldest_key]

        except PlaywrightTimeout:
            pass
        except Exception as e:
            print(f"VRF extraction error: {e}")
        finally:
            page.close()

        return vrf_token

    @classmethod
    def _get_chapter_pages_vrf_sync(cls, chapter_url: str) -> str:
        """
        Capture the VRF-protected AJAX URL for chapter pages.

        Mirrors Kotlin fetchPageList + WebViewHelper.loadInWebView:
          1. Load chapter page in a fresh browser context (like Kotlin's new WebView).
          2. Route interception:
             - mangafire.to ajax/read/chapter or ajax/read/volume → Capture
             - mangafire.to other ajax/read                       → Allow
             - mangafire.to (non-image resources)                 → Allow
             - mfcdn.cc (non-image resources)                     → Allow
             - cloudflare.com jQuery                              → Allow
             - everything else                                    → Block
          3. Return the captured URL (contains ?vrf= parameter).
        """
        cls._ensure_browser_sync()

        # Fresh context per request — mirrors Kotlin creating a new WebView each time.
        # This avoids stale Cloudflare cookies breaking subsequent requests.
        context = cls._browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            ignore_https_errors=True,
        )
        page = context.new_page()
        captured_url = None
        full_url = chapter_url if chapter_url.startswith("http") else f"{BASE_URL}{chapter_url}"

        try:
            def handle_route(route, request):
                nonlocal captured_url
                url = request.url
                rtype = request.resource_type

                # mangafire.to ajax/read requests
                if "mangafire.to" in url and "/ajax/read/" in url:
                    if "/ajax/read/chapter/" in url or "/ajax/read/volume/" in url:
                        if not captured_url:
                            captured_url = url
                        route.abort()          # Capture — don't need the response
                    else:
                        route.continue_()      # Allow (VRF generation flow)
                    return

                # mangafire.to — allow everything except images (Kotlin: blockNetworkImage)
                if "mangafire.to" in url:
                    route.abort() if rtype in ("image", "media", "font") else route.continue_()
                    return

                # CDN scripts
                if "mfcdn.cc" in url:
                    route.abort() if rtype in ("image", "media", "font") else route.continue_()
                    return

                # jQuery from cloudflare
                if "cloudflare.com" in url and "jquery" in url.lower():
                    route.continue_()
                    return

                # Block everything else
                route.abort()

            page.route("**/*", handle_route)
            page.goto(full_url, wait_until="networkidle", timeout=30000)

            # Wait up to 10s for the AJAX request to fire
            for _ in range(20):
                if captured_url:
                    break
                page.wait_for_timeout(500)

        except PlaywrightTimeout:
            logger.warning(f"[VRF] Timeout loading {full_url}")
        except Exception as e:
            logger.error(f"[VRF] Error: {e}")
        finally:
            context.close()

        return captured_url

    @classmethod
    def _get_page_with_cloudflare_bypass_sync(cls, url: str) -> str:
        """Blocking Cloudflare-bypass page fetch."""
        cls._ensure_browser_sync()
        page = cls._context.new_page()
        try:
            page.goto(url, wait_until="networkidle", timeout=30000)
            return page.content()
        except Exception as e:
            raise RuntimeError(f"Browser fetch failed: {e}")
        finally:
            page.close()

    # ---- public async interface (delegates to thread) ----

    @classmethod
    async def get_browser(cls):
        """Ensure browser is started (async wrapper)."""
        if not PLAYWRIGHT_AVAILABLE:
            raise HTTPException(
                status_code=501,
                detail="Playwright not installed. Run: pip install playwright && playwright install chromium"
            )
        await asyncio.to_thread(cls._ensure_browser_sync)
        return cls._browser, cls._context

    @classmethod
    async def close_browser(cls):
        """Close browser instance (async wrapper)."""
        await asyncio.to_thread(cls._close_browser_sync)

    @classmethod
    async def get_search_vrf(cls, query: str) -> str:
        """Get VRF token for a search query (async wrapper)."""
        if not PLAYWRIGHT_AVAILABLE:
            raise HTTPException(
                status_code=501,
                detail="Playwright not installed. Run: pip install playwright && playwright install chromium"
            )
        if query in cls._search_vrf_cache:
            return cls._search_vrf_cache[query]
        return await asyncio.to_thread(cls._get_search_vrf_sync, query)

    @classmethod
    async def get_chapter_pages_vrf(cls, chapter_url: str) -> str:
        """
        Get VRF-protected AJAX URL for chapter pages (async wrapper).
        Returns the intercepted AJAX URL with VRF token, matching Kotlin behavior.
        """
        if not PLAYWRIGHT_AVAILABLE:
            raise HTTPException(
                status_code=501,
                detail="Playwright not installed. Run: pip install playwright && playwright install chromium"
            )
        return await asyncio.to_thread(cls._get_chapter_pages_vrf_sync, chapter_url)

    @classmethod
    async def get_page_with_cloudflare_bypass(cls, url: str) -> str:
        """Fetch a page with Cloudflare bypass (async wrapper)."""
        if not PLAYWRIGHT_AVAILABLE:
            raise HTTPException(
                status_code=501,
                detail="Playwright not installed. Run: pip install playwright && playwright install chromium"
            )
        try:
            return await asyncio.to_thread(cls._get_page_with_cloudflare_bypass_sync, url)
        except RuntimeError as e:
            raise HTTPException(status_code=500, detail=str(e))


# ==================== API Client ====================
class MangaFireClient:
    """Client for making requests to MangaFire"""
    
    def __init__(self):
        self.base_url = BASE_URL
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": f"{BASE_URL}/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
    
    async def _fetch(self, url: str, params: dict = None) -> str:
        """Fetch a URL and return the response text"""
        async with aiohttp.ClientSession(headers=self.headers) as session:
            async with session.get(url, params=params, ssl=False) as response:
                if response.status != 200:
                    raise HTTPException(
                        status_code=response.status,
                        detail=f"Failed to fetch {url}: HTTP {response.status}"
                    )
                return await response.text()
    
    async def _fetch_json(self, url: str, params: dict = None) -> dict:
        """Fetch a URL and return JSON response"""
        async with aiohttp.ClientSession(headers=self.headers) as session:
            async with session.get(url, params=params, ssl=False) as response:
                if response.status != 200:
                    raise HTTPException(
                        status_code=response.status,
                        detail=f"Failed to fetch {url}: HTTP {response.status}"
                    )
                return await response.json()
    
    async def _fetch_image(self, url: str) -> bytes:
        """Fetch an image and return bytes"""
        async with aiohttp.ClientSession(headers=self.headers) as session:
            async with session.get(url, ssl=False) as response:
                if response.status != 200:
                    raise HTTPException(
                        status_code=response.status,
                        detail=f"Failed to fetch image: HTTP {response.status}"
                    )
                return await response.read()

    async def search(
        self,
        query: str = "",
        page: int = 1,
        language: str = "en",
        types: List[str] = None,
        genres: List[str] = None,
        genre_mode: str = None,
        status: List[str] = None,
        year: List[str] = None,
        min_chapters: int = None,
        sort: str = "most_relevance",
        use_browser: bool = True
    ) -> SearchResult:
        """Search for manga"""
        params = {
            "page": page,
            "sort": sort,
        }
        
        # Add language
        lang_code = SUPPORTED_LANGUAGES.get(language, language)
        params["language[]"] = lang_code
        
        # Add keyword if provided
        if query:
            params["keyword"] = query
            
            # If keyword search and browser is available, get VRF token
            if use_browser and PLAYWRIGHT_AVAILABLE:
                vrf = await VRFHelper.get_search_vrf(query)
                if vrf:
                    params["vrf"] = vrf
        
        # Build filter URL
        url = f"{self.base_url}/filter"
        
        # Add types
        if types:
            for t in types:
                params[f"type"] = t
        
        # Add genres
        if genres:
            for g in genres:
                genre_id = GENRES.get(g.lower().replace(" ", "_"), g)
                params[f"genre[]"] = genre_id
        
        # Genre mode (and/or)
        if genre_mode == "and":
            params["genre_mode"] = "and"
        
        # Status filter
        if status:
            for s in status:
                params[f"status[]"] = s
        
        # Year filter
        if year:
            for y in year:
                params[f"year[]"] = y
        
        # Minimum chapters
        if min_chapters and min_chapters > 0:
            params["minchap"] = min_chapters
        
        html = await self._fetch(url, params)
        return self._parse_search_results(html, page)
    
    def _parse_search_results(self, html: str, current_page: int) -> SearchResult:
        """Parse search results from HTML"""
        soup = BeautifulSoup(html, "lxml")
        
        manga_list = []
        for item in soup.select(".original.card-lg .unit .inner"):
            info_link = item.select_one(".info > a")
            if not info_link:
                continue
            
            url = info_link.get("href", "")
            title = info_link.get_text(strip=True)
            manga_id = url.split(".")[-1] if "." in url else url.split("/")[-1]
            
            img = item.select_one("img")
            thumbnail = img.get("src") if img else None
            
            manga_list.append(MangaBasic(
                id=manga_id,
                title=title,
                url=url,
                thumbnail_url=thumbnail
            ))
        
        # Check for next page
        has_next = soup.select_one(".page-item.active + .page-item .page-link") is not None
        
        return SearchResult(
            manga_list=manga_list,
            has_next_page=has_next,
            current_page=current_page
        )
    
    async def get_manga_details(self, manga_id: str) -> MangaDetails:
        """Get detailed manga information"""
        # manga_id can be the full slug like "one-piece.vy8" or just the id
        if not any(c.isdigit() for c in manga_id):
            raise HTTPException(status_code=400, detail="Invalid manga ID format")
        
        url = f"{self.base_url}/manga/{manga_id}"
        html = await self._fetch(url)
        return self._parse_manga_details(html, manga_id)
    
    def _parse_manga_details(self, html: str, manga_id: str) -> MangaDetails:
        """Parse manga details from HTML"""
        soup = BeautifulSoup(html, "lxml")
        
        main = soup.select_one(".main-inner:not(.manga-bottom)")
        if not main:
            raise HTTPException(status_code=404, detail="Manga not found")
        
        title = main.select_one("h1")
        title = title.get_text(strip=True) if title else "Unknown"
        
        poster = main.select_one(".poster img")
        thumbnail = poster.get("src") if poster else None
        
        # Status parsing - match Kotlin logic
        # MangaFire marks manga as "completed" when original publication is completed
        # even if translation is not complete
        status_elem = main.select_one(".info > p")
        status_text = status_elem.get_text(strip=True).lower() if status_elem else None
        
        # Map status to match Kotlin logic
        status_map = {
            "releasing": "ongoing",
            "completed": "publishing_finished",
            "on_hiatus": "on_hiatus",
            "discontinued": "cancelled"
        }
        status = status_map.get(status_text, status_text) if status_text else None
        
        # Description with alternative title appended (matching Kotlin)
        description_parts = []
        synopsis = soup.select_one("#synopsis .modal-content")
        if synopsis:
            # Get text nodes (matching Kotlin's textNodes approach)
            synopsis_text = synopsis.get_text(separator="\n\n", strip=True)
            if synopsis_text:
                description_parts.append(synopsis_text)
        
        # Alternative title
        alt_title_elem = main.select_one("h6")
        if alt_title_elem:
            alt_title = alt_title_elem.get_text(strip=True)
            description_parts.append(f"Alternative title: {alt_title}")
        else:
            alt_title = None
        
        description = "\n\n".join(description_parts) if description_parts else None
        
        # Meta info - improved extraction matching Kotlin
        meta = main.select_one(".meta")
        author = None
        type_info = None
        genres = []
        
        if meta:
            # Extract author
            for span in meta.select("span"):
                text = span.get_text(strip=True)
                if "Author" in text or "author" in text.lower():
                    next_span = span.find_next_sibling("span")
                    if next_span:
                        author = next_span.get_text(strip=True)
                    break
            
            # Extract type
            for span in meta.select("span"):
                text = span.get_text(strip=True)
                if "Type" in text or "type" in text.lower():
                    next_span = span.find_next_sibling("span")
                    if next_span:
                        type_info = next_span.get_text(strip=True)
                    break
            
            # Extract genres
            for span in meta.select("span"):
                text = span.get_text(strip=True)
                if "Genres" in text or "genres" in text.lower():
                    next_span = span.find_next_sibling("span")
                    if next_span:
                        genres_text = next_span.get_text(strip=True)
                        genres = [g.strip() for g in genres_text.split(",") if g.strip()]
                    break
            
            # Combine type and genres (matching Kotlin's genre field logic)
            if type_info and genres:
                genres = [type_info] + genres
            elif type_info:
                genres = [type_info]
        
        return MangaDetails(
            id=manga_id,
            title=title,
            url=f"/manga/{manga_id}",
            thumbnail_url=thumbnail,
            status=status,
            description=description,
            author=author,
            genres=genres if genres else None,
            alternative_title=alt_title
        )
    
    async def get_chapters(
        self,
        manga_id: str,
        language: str = "en",
        chapter_type: str = "chapter"
    ) -> ChapterList:
        """Get chapters for a manga"""
        # Extract numeric id from slug if needed
        if "." in manga_id:
            numeric_id = manga_id.split(".")[-1]
        else:
            numeric_id = manga_id
        
        lang_code = SUPPORTED_LANGUAGES.get(language, language)
        url = f"{self.base_url}/ajax/manga/{numeric_id}/{chapter_type}/{lang_code}"
        
        data = await self._fetch_json(url)
        
        if "result" not in data:
            raise HTTPException(status_code=404, detail="No chapters found")
        
        return self._parse_chapters(data["result"], manga_id, language, chapter_type)
    
    def _parse_chapters(
        self,
        html: str,
        manga_id: str,
        language: str,
        chapter_type: str
    ) -> ChapterList:
        """Parse chapters from HTML response"""
        soup = BeautifulSoup(html, "lxml")
        
        chapters = []
        # Match Kotlin selectors
        selector = ".vol-list > .item" if chapter_type == "volume" else "li"
        
        for item in soup.select(selector):
            link = item.select_one("a")
            if not link:
                continue
            
            url = link.get("href", "")
            number_str = item.get("data-number", "0")
            
            try:
                number = float(number_str)
            except ValueError:
                number = -1
            
            spans = item.select("span")
            # First span contains the name
            name_elem = spans[0] if spans else None
            # Second span contains the date
            date_str = spans[1].get_text(strip=True) if len(spans) > 1 else None
            
            # Build chapter name with better formatting (matching Kotlin logic)
            if name_elem:
                raw_name = name_elem.get_text(strip=True)
                # Kotlin uses abbreviated prefix in data, full prefix in display
                abbr_prefix = "Vol" if chapter_type == "volume" else "Chap"
                full_prefix = "Volume" if chapter_type == "volume" else "Chapter"
                prefix = f"{abbr_prefix} {number_str}: "
                
                # If name starts with abbreviated prefix, replace with full
                if raw_name.startswith(prefix):
                    real_name = raw_name[len(prefix):]
                    # Only use full prefix if the number isn't already in the name
                    if number_str not in real_name:
                        name = f"{full_prefix} {number_str}: {real_name}"
                    else:
                        name = real_name
                else:
                    name = raw_name
            else:
                name = f"{'Volume' if chapter_type == 'volume' else 'Chapter'} {number}"
            
            # Extract chapter ID from URL
            chapter_id = url.split("/")[-1] if "/" in url else url
            
            chapters.append(Chapter(
                id=chapter_id,
                number=number,
                name=name,
                url=url,
                date_upload=date_str
            ))
        
        return ChapterList(
            chapters=chapters,
            manga_id=manga_id,
            language=language
        )
    
    async def get_pages(self, chapter_url: str) -> PageList:
        """
        Get pages for a chapter.  Mirrors Kotlin fetchPageList:
          1. Use headless browser to capture ajax/read/chapter/{id}?vrf=... URL
          2. Validate VRF token is present
          3. HTTP GET the captured URL
          4. Parse ResponseDto<PageListDto>  →  images: [[url, ?, offset], ...]
        """
        if not PLAYWRIGHT_AVAILABLE:
            raise HTTPException(
                status_code=501,
                detail="Pages require VRF token. Install playwright: pip install playwright && playwright install chromium",
            )

        # Build chapter path (matches Kotlin: "$baseUrl${chapter.url}")
        chapter_url = chapter_url.strip("/")
        chapter_path = f"/{chapter_url}" if chapter_url.startswith("read") else f"/read/{chapter_url}"

        # Step 1 — capture VRF-protected AJAX URL via headless browser
        ajax_url = await VRFHelper.get_chapter_pages_vrf(chapter_path)
        if not ajax_url:
            raise HTTPException(status_code=500, detail="Unable to capture AJAX URL for chapter pages")

        # Step 2 — validate VRF (matches Kotlin: intercepted.toHttpUrl().queryParameter("vrf") == null)
        if "vrf" not in parse_qs(urlparse(ajax_url).query):
            raise HTTPException(status_code=500, detail="Unable to find vrf token")

        # Step 3 — fetch page data (matches Kotlin: client.newCall(GET(intercepted, headers)))
        data = await self._fetch_json(ajax_url)
        if "result" not in data:
            raise HTTPException(status_code=404, detail="No pages found in API response")

        # Step 4 — parse images (matches Kotlin PageListDto)
        return self._parse_pages(data["result"], chapter_url)

    @staticmethod
    def _parse_pages(result: dict, chapter_id: str) -> PageList:
        """
        Parse Kotlin's ResponseDto<PageListDto>:
          images: [[url: str, ?, offset: int], ...]
        """
        pages = []
        for idx, img in enumerate(result.get("images", [])):
            if isinstance(img, list) and len(img) >= 3:
                url = img[0]
                offset = img[2] if isinstance(img[2], int) else 0
                pages.append(Page(index=idx, url=url, is_scrambled=offset > 0, scramble_offset=offset))
        return PageList(pages=pages, chapter_id=chapter_id)


# ==================== Global Client ====================
client = MangaFireClient()


# ==================== API Endpoints ====================

@app.get("/", tags=["Root"])
async def root():
    """Root endpoint"""
    return {
        "message": "MangaFire API",
        "version": "1.0.0",
        "browser_available": PLAYWRIGHT_AVAILABLE,
        "endpoints": {
            "search": "/search",
            "manga_details": "/manga/{manga_id}",
            "chapters": "/manga/{manga_id}/chapters",
            "pages": "/chapter/{chapter_id}/pages",
            "languages": "/languages",
            "genres": "/genres",
            "browser_status": "/browser/status",
        }
    }


@app.get("/browser/status", tags=["Browser"])
async def browser_status():
    """Check headless browser status for VRF bypass"""
    return {
        "playwright_available": PLAYWRIGHT_AVAILABLE,
        "browser_active": VRFHelper._browser is not None if PLAYWRIGHT_AVAILABLE else False,
        "search_vrf_cache_size": len(VRFHelper._search_vrf_cache) if PLAYWRIGHT_AVAILABLE else 0,
        "message": "Headless browser ready for VRF bypass" if PLAYWRIGHT_AVAILABLE else "Install playwright: pip install playwright && playwright install chromium"
    }


@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup browser on shutdown"""
    if PLAYWRIGHT_AVAILABLE:
        await VRFHelper.close_browser()


@app.get("/languages", tags=["Info"])
async def get_languages():
    """Get supported languages"""
    return {
        "languages": list(SUPPORTED_LANGUAGES.keys()),
        "default": "en"
    }


@app.get("/genres", tags=["Info"])
async def get_genres():
    """Get available genres"""
    return {"genres": list(GENRES.keys())}


@app.get("/sort-options", tags=["Info"])
async def get_sort_options():
    """Get available sort options"""
    return {"sort_options": [e.value for e in SortOrder]}


@app.get("/search", response_model=SearchResult, tags=["Search"])
async def search_manga(
    query: str = Query(default="", description="Search query"),
    page: int = Query(default=1, ge=1, description="Page number"),
    language: str = Query(default="en", description="Language code"),
    types: Optional[str] = Query(default=None, description="Comma-separated types"),
    genres: Optional[str] = Query(default=None, description="Comma-separated genres"),
    genre_mode: Optional[str] = Query(default=None, description="Genre mode: 'and' or 'or'"),
    status: Optional[str] = Query(default=None, description="Comma-separated status"),
    year: Optional[str] = Query(default=None, description="Comma-separated years (e.g., 2024, 2023 or decades like 1990s, 1980s)"),
    min_chapters: Optional[int] = Query(default=None, ge=1, description="Minimum chapters (must be > 0)"),
    sort: str = Query(default="most_relevance", description="Sort order"),
    use_browser: bool = Query(default=True, description="Use headless browser for VRF bypass (keyword search)")
):
    """
    Search for manga with various filters
    
    - **query**: Search keyword (requires VRF token - use browser for bypass)
    - **page**: Page number (starts at 1)
    - **language**: Language code (en, es, fr, ja, pt, pt-br)
    - **types**: Comma-separated manga types (manga, manhwa, manhua, etc.)
    - **genres**: Comma-separated genres (action, adventure, comedy, etc.)
    - **genre_mode**: 'and' to require all genres, 'or' for any (default)
    - **status**: Comma-separated status (completed, releasing, on_hiatus, discontinued)
    - **year**: Comma-separated years (e.g., 2024, 2023 or decades like 1990s, 1980s)
    - **min_chapters**: Minimum number of chapters (must be > 0)
    - **sort**: Sort order (most_viewed, recently_updated, etc.)
    - **use_browser**: Use headless browser to bypass VRF (default: true)
    """
    try:
        type_list = types.split(",") if types else None
        genre_list = genres.split(",") if genres else None
        status_list = status.split(",") if status else None
        year_list = year.split(",") if year else None
        
        return await client.search(
            query=query,
            page=page,
            language=language,
            types=type_list,
            genres=genre_list,
            genre_mode=genre_mode,
            status=status_list,
            year=year_list,
            min_chapters=min_chapters,
            sort=sort,
            use_browser=use_browser
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/manga/{manga_id}", response_model=MangaDetails, tags=["Manga"])
async def get_manga_details(manga_id: str):
    """
    Get detailed information about a manga
    
    - **manga_id**: Manga ID or slug (e.g., 'one-piece.vy8')
    """
    try:
        return await client.get_manga_details(manga_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/manga/{manga_id}/chapters", response_model=ChapterList, tags=["Chapters"])
async def get_chapters(
    manga_id: str,
    language: str = Query(default="en", description="Language code"),
    chapter_type: str = Query(default="chapter", alias="type", description="Type: 'chapter' or 'volume'")
):
    """
    Get chapters for a manga
    
    - **manga_id**: Manga ID or slug
    - **language**: Language code
    - **type**: 'chapter' for chapters, 'volume' for volumes
    """
    try:
        return await client.get_chapters(manga_id, language, chapter_type)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/chapter/{chapter_id:path}/pages", response_model=PageList, tags=["Pages"])
async def get_pages(chapter_id: str):
    """
    Get pages for a chapter (uses headless browser for VRF bypass)

    - **chapter_id**: Chapter URL path (e.g., 'read/one-piece.dkw/en/chapter-1')
    """
    try:
        return await client.get_pages(chapter_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Error Handlers ====================

@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    """Handle HTTP exceptions"""
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(
            error=type(exc).__name__,
            detail=exc.detail,
            status_code=exc.status_code
        ).model_dump()
    )


@app.exception_handler(Exception)
async def general_exception_handler(request, exc):
    """Handle general exceptions"""
    return JSONResponse(
        status_code=500,
        content=ErrorResponse(
            error="InternalServerError",
            detail=str(exc),
            status_code=500
        ).model_dump()
    )


# ==================== Run Server ====================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
