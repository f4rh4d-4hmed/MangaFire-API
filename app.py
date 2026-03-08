"""
MangaFire API – FastAPI-based manga reader for MangaFire.to

Based on: https://github.com/yuzono/tachiyomi-extensions

Provides REST endpoints for searching manga, fetching details/chapters/pages,
and descrambling protected images.  A headless Chromium browser (Playwright)
handles Cloudflare challenges and VRF token extraction.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from typing import Optional, List
from enum import Enum
from pydantic import BaseModel
import aiohttp
from bs4 import BeautifulSoup
from io import BytesIO
import asyncio
import logging
from urllib.parse import urlparse, parse_qs

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('mangafire')

# Optional: Pillow for image descrambling 
try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# Optional: Playwright (headless Chromium) for VRF bypass
# Uses the async API – the sync adapter breaks page.route() callbacks
# when called from a non-main thread.
try:
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

@asynccontextmanager
async def lifespan(app):
    yield
    if PLAYWRIGHT_AVAILABLE:
        await VRFHelper.close_browser()

app = FastAPI(
    title="MangaFire API",
    description="API for fetching manga from MangaFire.to",
    version="1.0.0",
    lifespan=lifespan,
)

# ==================== Constants ====================
BASE_URL = "https://mangafire.to"

# Language codes accepted by MangaFire's /filter and /ajax/manga endpoints.
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

# Genre name → numeric ID used in MangaFire's filter query-string.
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


# ==================== Response Models ====================

class MangaBasic(BaseModel):
    """Compact manga card returned by search / browse listings."""
    id: str
    title: str
    url: str
    thumbnail_url: Optional[str] = None


class MangaDetails(BaseModel):
    """Full manga profile."""
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
    """Single chapter entry."""
    id: str
    number: float
    name: str
    url: str
    date_upload: Optional[str] = None


class Page(BaseModel):
    """Single page image.  `scramble_offset` > 0 means the image tiles are
    shuffled and must be descrambled (see ImageDescrambler)."""
    index: int
    url: str
    is_scrambled: bool = False
    scramble_offset: int = 0


class SearchResult(BaseModel):
    """Paginated search / browse result."""
    manga_list: List[MangaBasic]
    has_next_page: bool
    current_page: int


class ChapterList(BaseModel):
    """All chapters for a manga in a given language."""
    chapters: List[Chapter]
    manga_id: str
    language: str


class PageList(BaseModel):
    """Page image list for a single chapter."""
    pages: List[Page]
    chapter_id: str


class ErrorResponse(BaseModel):
    """Standardised error envelope returned by exception handlers."""
    error: str
    detail: str
    status_code: int


# ==================== Image Descrambler ====================
class ImageDescrambler:
    """Descramble MangaFire page images.

    Some images are split into a grid of tiles and shuffled.  The `offset`
    value (provided alongside each image URL) is used to reverse the shuffle.

    Requires pillow
    """
    PIECE_SIZE = 200       # max tile dimension (px)
    MIN_SPLIT_COUNT = 5    # minimum number of tiles per axis
    
    @staticmethod
    def ceil_div(a: int, b: int) -> int:
        """Integer ceiling division."""
        return (a + (b - 1)) // b
    
    @classmethod
    async def descramble(cls, image_data: bytes, offset: int) -> bytes:
        """Reverse the tile-shuffle for a single image.

        Args:
            image_data: Raw JPEG/PNG bytes of the scrambled image.
            offset:     Shuffle key from the page-list API response.

        Returns:
            JPEG bytes of the descrambled image.
        """
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
                
                piece = img.crop((x_src, y_src, x_src + w, y_src + h))

                result.paste(piece, (x_dst, y_dst))
        
        output = BytesIO()
        result.save(output, format='JPEG', quality=90)
        return output.getvalue()


# ==================== VRF Token Helper (Headless Browser) ====================
class VRFHelper:
    """Extract VRF tokens using a headless Chromium browser (Playwright).

    MangaFire protects its AJAX endpoints with VRF tokens computed by
    obfuscated client-side JS.  This class loads pages in a headless
    browser, intercepts outgoing requests, and captures the VRF-signed URLs.

    Notes:
      * A single persistent browser context is reused across requests;
        Cloudflare clearance cookies from the first warmup carry over.
      * Search VRF tokens are cached (max 20 entries, oldest evicted first).
      * Chapter-page retrieval uses a two-pass strategy (see
        ``_two_pass_page_fetch``) to outrace anti-automation redirects.
    """

    _lock = asyncio.Lock()       # serialises browser init to prevent duplicate instances
    _playwright = None
    _browser = None
    _context = None                # persistent context that retains Cloudflare cookies
    _search_vrf_cache: dict[str, str] = {}  # query → VRF token
    _MAX_CACHE_SIZE = 20           # evicts oldest entry when exceeded

    # browser lifecycle 

    @classmethod
    async def _ensure_browser(cls):
        """Lazily initialise Playwright + Chromium and warm up Cloudflare.

        On first call, launches a headless browser, creates a context, and
        visits the home page so the Cloudflare JS challenge is solved once.
        Subsequent calls are no-ops unless the browser process has crashed.
        """
        async with cls._lock:
            if cls._browser is not None:
                try:
                    cls._browser.contexts  # throws if browser crashed
                    return
                except Exception:
                    logger.warning("[VRF] Browser crashed, restarting...")
                    cls._browser = None
                    cls._context = None
                    if cls._playwright:
                        try:
                            await cls._playwright.stop()
                        except Exception:
                            pass
                        cls._playwright = None

            cls._playwright = await async_playwright().start()
            cls._browser = await cls._playwright.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-accelerated-2d-canvas',
                    '--disable-gpu',
                    '--no-first-run',
                    '--no-zygote',
                    '--disable-blink-features=AutomationControlled',
                ],
            )
            cls._context = await cls._browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
                ignore_https_errors=True,
            )

            # Warmup: visit the home page to solve Cloudflare challenge once.
            # Subsequent page loads will reuse the clearance cookies.
            logger.info("[VRF] Warming up browser (solving Cloudflare)...")
            page = await cls._context.new_page()
            try:
                await page.goto(f"{BASE_URL}/home", wait_until="networkidle", timeout=30000)
                await page.wait_for_timeout(2000)
                logger.info("[VRF] Browser warmup complete")
            except Exception as e:
                logger.warning(f"[VRF] Warmup visit failed (non-fatal): {e}")
            finally:
                await page.close()

    @classmethod
    async def close_browser(cls):
        """Shut down the browser, context, and Playwright driver (called at app shutdown)."""
        async with cls._lock:
            if cls._context:
                await cls._context.close()
                cls._context = None
            if cls._browser:
                await cls._browser.close()
                cls._browser = None
            if cls._playwright:
                await cls._playwright.stop()
                cls._playwright = None

    # search VRF

    @classmethod
    async def get_search_vrf(cls, query: str) -> str:
        """Obtain a VRF token for a keyword search.

        Navigates to the home page, types the query into the search box, and
        intercepts the outgoing ``ajax/manga/search?…&vrf=…`` request to
        extract the token.  The result is cached for subsequent identical
        queries.
        """
        if not PLAYWRIGHT_AVAILABLE:
            raise HTTPException(status_code=501, detail="Playwright not installed")
        if query in cls._search_vrf_cache:
            return cls._search_vrf_cache[query]

        await cls._ensure_browser()
        page = await cls._context.new_page()
        vrf_token = None

        try:
            def handle_request(request):
                nonlocal vrf_token
                url = request.url
                if "mangafire.to" in url and "ajax/manga/search" in url:
                    params = parse_qs(urlparse(url).query)
                    if 'vrf' in params:
                        vrf_token = params['vrf'][0]

            page.on("request", handle_request)
            await page.goto(f"{BASE_URL}/home", wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(1000)
            search_input = page.locator(".search-inner input[name=keyword]")
            await search_input.fill(query)
            await search_input.press("Enter")

            for _ in range(10):
                if vrf_token:
                    break
                await page.wait_for_timeout(500)

            if vrf_token:
                cls._search_vrf_cache[query] = vrf_token
                if len(cls._search_vrf_cache) > cls._MAX_CACHE_SIZE:
                    oldest_key = next(iter(cls._search_vrf_cache))
                    del cls._search_vrf_cache[oldest_key]

        except PlaywrightTimeout:
            pass
        except Exception as e:
            logger.error(f"[VRF] Search VRF error: {e}")
        finally:
            await page.close()

        return vrf_token

    # chapter pages data

    # Skip browser-fingerprint headers that would conflict with our User-Agent.
    _SKIP_HEADERS = frozenset({
        "user-agent", "sec-ch-ua", "sec-ch-ua-mobile",
        "sec-ch-ua-platform", "x-requested-with",
    })
    _PROXY_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": f"{BASE_URL}/",
    }

    @classmethod
    async def _fetch_web_resource(cls, url: str, request_headers: dict) -> tuple:
        """Fetch a URL via aiohttp.

        Merges the caller's headers with ``_PROXY_HEADERS`` (excluding
        browser-fingerprint fields) and forwards any Cloudflare cookies
        from the persistent browser context.

        Returns:
            (status_code, content_type, body_bytes)
        """
        headers = dict(cls._PROXY_HEADERS)
        for name, value in request_headers.items():
            if name.lower() not in cls._SKIP_HEADERS:
                headers[name] = value
        cookies = {}
        if cls._context:
            for c in await cls._context.cookies():
                cookies[c["name"]] = c["value"]
        try:
            async with aiohttp.ClientSession(cookies=cookies) as session:
                async with session.get(
                    url, headers=headers, ssl=False,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    body = await resp.read()
                    ct = resp.headers.get("content-type", "text/plain")
                    return resp.status, ct, body
        except Exception as exc:
            logger.warning(f"[Pages] fetchWebResource failed for {url[:80]}: {exc}")
            return 200, "text/plain", b""

    @classmethod
    async def get_chapter_pages_data(cls, chapter_url: str) -> Optional[dict]:
        """Load chapter page images using a two-pass browser approach.

        Background:
            MangaFire's reader JS (`scripts.js` on mfcdn.cc) has anti-bot
            detection that redirects to the home page after a short delay.
            To outrace it we need the first AJAX response to arrive instantly.

        Strategy (two passes):
            Pass 1 – Load the chapter page; intercept and record the first
                     AJAX URL (`ajax/read/{manga_id}/{type}/{lang}?…vrf=…`).
                     Pre-fetch its JSON response via aiohttp.
            Pass 2 – Load the page again; this time fulfil the first AJAX
                     instantly from cache (0 ms network wait).  The JS
                     callback fires the second AJAX (`ajax/read/chapter/…`
                     or `ajax/read/volume/…`) before the redirect triggers.
                     We capture that URL and re-fetch via aiohttp.

        Retries up to 3 times on failure.

        Returns:
            Parsed JSON ``{"result": {"images": [[url, ?, offset], …]}}``
            or ``None`` on failure.
        """
        if not PLAYWRIGHT_AVAILABLE:
            raise HTTPException(status_code=501, detail="Playwright not installed")

        await cls._ensure_browser()
        full_url = chapter_url if chapter_url.startswith("http") else f"{BASE_URL}{chapter_url}"

        for attempt in range(3):
            try:
                result = await cls._two_pass_page_fetch(full_url)
                if result:
                    return result
                logger.warning(f"[Pages] Two-pass attempt {attempt + 1} failed")
            except Exception as e:
                logger.error(f"[Pages] Error (attempt {attempt + 1}): {e}")

        return None

    @classmethod
    async def _two_pass_page_fetch(cls, full_url: str) -> Optional[dict]:
        """Execute one iteration of the two-pass VRF capture strategy.

        See ``get_chapter_pages_data`` for the high-level description.
        Each pass runs in its own short-lived browser context (cloned from
        the persistent one) so that a redirect in one pass cannot pollute
        the next.
        """
        import json as _json

        # helpers
        async def _make_ctx():
            storage = await cls._context.storage_state()
            return await cls._browser.new_context(
                storage_state=storage,
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
                ignore_https_errors=True,
            )

        async def _sync_cookies(ctx):
            try:
                for cookie in await ctx.cookies():
                    await cls._context.add_cookies([cookie])
            except Exception:
                pass

        # Pass 1: capture the first AJAX URL
        first_ajax_url = None
        first_ajax_event = asyncio.Event()
        main_page_body: bytes | None = None

        ctx1 = await _make_ctx()
        page1 = await ctx1.new_page()
        try:
            async def _pass1_route(route):
                """Route handler for Pass 1 – allow page + scripts, capture first AJAX."""
                nonlocal first_ajax_url, main_page_body
                req = route.request
                url = req.url
                host = urlparse(url).hostname or ""
                path = urlparse(url).path or ""

                # Allow the main chapter page (cache HTML for Pass 2)
                if url.rstrip("/") == full_url.rstrip("/"):
                    status, ct, body = await cls._fetch_web_resource(url, req.headers)
                    main_page_body = body
                    await route.fulfill(status=status, headers={"content-type": ct}, body=body)
                    return
                # Allow jQuery from CDN (required by scripts.js)
                if "cloudflare.com" in host and "jquery" in path:
                    status, ct, body = await cls._fetch_web_resource(url, req.headers)
                    await route.fulfill(status=status, headers={"content-type": ct}, body=body)
                    return
                # Allow reader JS from mfcdn.cc (contains VRF logic)
                if "mfcdn.cc" in host and path.endswith(".js"):
                    status, ct, body = await cls._fetch_web_resource(url, req.headers)
                    await route.fulfill(status=status, headers={"content-type": ct}, body=body)
                    return
                # Capture first AJAX (VRF-signed chapter-list request)
                if "mangafire.to" in host and "ajax/read" in path:
                    first_ajax_url = url
                    first_ajax_event.set()
                    await route.fulfill(status=200, content_type="application/json", body='{"status":200,"result":{}}')
                    return
                # Block everything else (images, analytics, ads, etc.)
                await route.fulfill(status=200, content_type="text/plain", body="")

            await page1.route("**/*", _pass1_route)
            try:
                await page1.goto(full_url, wait_until="commit", timeout=20000)
            except Exception:
                pass
            await asyncio.wait_for(first_ajax_event.wait(), timeout=15)
        except asyncio.TimeoutError:
            logger.warning("[Pages] Pass 1: first AJAX URL not captured")
            return None
        finally:
            await _sync_cookies(ctx1)
            await page1.close()
            await ctx1.close()

        logger.info(f"[Pages] Pass 1 OK: {first_ajax_url[:80]}")

        # Pre-fetch the first AJAX response via aiohttp so we can serve it
        # instantly during Pass 2 (eliminating network latency for the JS).
        ajax_headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }
        _, _, first_ajax_body = await cls._fetch_web_resource(first_ajax_url, ajax_headers)
        if not first_ajax_body:
            logger.warning("[Pages] First AJAX response empty")
            return None

        # ── Pass 2: serve cached first-AJAX instantly, capture second AJAX
        second_ajax_url = None
        second_ajax_event = asyncio.Event()

        ctx2 = await _make_ctx()
        page2 = await ctx2.new_page()
        try:
            async def _pass2_route(route):
                """Route handler for Pass 2 – instant first-AJAX, capture second AJAX."""
                nonlocal second_ajax_url
                req = route.request
                url = req.url
                host = urlparse(url).hostname or ""
                path = urlparse(url).path or ""

                # Serve cached chapter HTML (no network round-trip)
                if url.rstrip("/") == full_url.rstrip("/"):
                    await route.fulfill(
                        status=200,
                        headers={"content-type": "text/html; charset=utf-8"},
                        body=main_page_body or b"",
                    )
                    return
                # Allow jQuery from CDN (required by scripts.js)
                if "cloudflare.com" in host and "jquery" in path:
                    status, ct, body = await cls._fetch_web_resource(url, req.headers)
                    await route.fulfill(status=status, headers={"content-type": ct}, body=body)
                    return
                # Allow reader JS from mfcdn.cc
                if "mfcdn.cc" in host and path.endswith(".js"):
                    status, ct, body = await cls._fetch_web_resource(url, req.headers)
                    await route.fulfill(status=status, headers={"content-type": ct}, body=body)
                    return
                # AJAX requests to MangaFire
                if "mangafire.to" in host and "ajax/read" in path:
                    # Second AJAX (page images) → capture URL, return stub
                    if "ajax/read/chapter/" in path or "ajax/read/volume/" in path:
                        second_ajax_url = url
                        second_ajax_event.set()
                        await route.fulfill(status=200, content_type="application/json", body='{"status":200,"result":{}}')
                        return
                    else:
                        # First AJAX → fulfil instantly from pre-fetched cache
                        await route.fulfill(
                            status=200,
                            headers={"content-type": "application/json"},
                            body=first_ajax_body,
                        )
                        return
                # Block everything else (images, analytics, redirect navigations)
                await route.fulfill(status=200, content_type="text/plain", body="")

            await page2.route("**/*", _pass2_route)
            try:
                await page2.goto(full_url, wait_until="commit", timeout=20000)
            except Exception:
                pass
            await asyncio.wait_for(second_ajax_event.wait(), timeout=20)
        except asyncio.TimeoutError:
            logger.warning("[Pages] Pass 2: second AJAX URL not captured")
            return None
        finally:
            await _sync_cookies(ctx2)
            await page2.close()
            await ctx2.close()

        logger.info(f"[Pages] Pass 2 OK: {second_ajax_url[:80]}")

        # Re-fetch the second AJAX URL via aiohttp with Cloudflare cookies.
        _, _, page_body = await cls._fetch_web_resource(second_ajax_url, ajax_headers)
        if not page_body:
            logger.warning("[Pages] Page images response empty")
            return None

        try:
            data = _json.loads(page_body)
            if data and "result" in data and "images" in data.get("result", {}):
                logger.info(f"[Pages] Success: {len(data['result']['images'])} page images")
                return data
        except Exception as e:
            logger.error(f"[Pages] Failed to parse page images: {e}")

        return None


# ==================== API Client ====================
class MangaFireClient:
    """Client for MangaFire.to.

    Provides search/browse, manga details, chapter listing, and page
    retrieval.  HTTP requests go through aiohttp; VRF-protected operations
    delegate to ``VRFHelper``.
    """
    
    def __init__(self):
        self.base_url = BASE_URL
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": f"{BASE_URL}/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
    
    async def _fetch(self, url: str, params: dict = None) -> str:
        """GET a URL and return the response body as text."""
        async with aiohttp.ClientSession(headers=self.headers) as session:
            async with session.get(url, params=params, ssl=False) as response:
                if response.status != 200:
                    raise HTTPException(
                        status_code=response.status,
                        detail=f"Failed to fetch {url}: HTTP {response.status}"
                    )
                return await response.text()
    
    async def _fetch_json(self, url: str, params: dict = None) -> dict:
        """GET a URL and return the parsed JSON body."""
        async with aiohttp.ClientSession(headers=self.headers) as session:
            async with session.get(url, params=params, ssl=False) as response:
                if response.status != 200:
                    raise HTTPException(
                        status_code=response.status,
                        detail=f"Failed to fetch {url}: HTTP {response.status}"
                    )
                return await response.json()
    
    async def _fetch_image(self, url: str) -> bytes:
        """GET an image URL and return raw bytes."""
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
        """Search / browse manga via the ``/filter`` page.

        When *query* is non-empty and *use_browser* is True, a VRF token is
        obtained via ``VRFHelper.get_search_vrf`` to authorise the keyword
        search (MangaFire rejects keyword queries without a valid VRF).
        """
        params = {
            "page": page,
            "sort": sort,
        }
        
        lang_code = SUPPORTED_LANGUAGES.get(language, language)
        params["language[]"] = lang_code
        
        if query:
            params["keyword"] = query
            
            # Keyword searches require a VRF token (obtained via headless browser)
            if use_browser and PLAYWRIGHT_AVAILABLE:
                vrf = await VRFHelper.get_search_vrf(query)
                if vrf:
                    params["vrf"] = vrf
        
        url = f"{self.base_url}/filter"
        
        if types:
            for t in types:
                params[f"type"] = t
        
        if genres:
            for g in genres:
                genre_id = GENRES.get(g.lower().replace(" ", "_"), g)
                params[f"genre[]"] = genre_id
        
        if genre_mode == "and":
            params["genre_mode"] = "and"
        
        if status:
            for s in status:
                params[f"status[]"] = s
        
        if year:
            for y in year:
                params[f"year[]"] = y
        
        if min_chapters and min_chapters > 0:
            params["minchap"] = min_chapters
        
        html = await self._fetch(url, params)
        return self._parse_search_results(html, page)
    
    def _parse_search_results(self, html: str, current_page: int) -> SearchResult:
        """Parse the ``/filter`` HTML response into a SearchResult."""
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
        
        has_next = soup.select_one(".page-item.active + .page-item .page-link") is not None
        
        return SearchResult(
            manga_list=manga_list,
            has_next_page=has_next,
            current_page=current_page
        )
    
    async def get_manga_details(self, manga_id: str) -> MangaDetails:
        """Fetch and parse the full manga profile page."""
        # manga_id can be the full slug (e.g. "one-piece.vy8") or a bare numeric id
        if not any(c.isdigit() for c in manga_id):
            raise HTTPException(status_code=400, detail="Invalid manga ID format")
        
        url = f"{self.base_url}/manga/{manga_id}"
        html = await self._fetch(url)
        return self._parse_manga_details(html, manga_id)
    
    def _parse_manga_details(self, html: str, manga_id: str) -> MangaDetails:
        """Extract manga metadata from the profile HTML."""
        soup = BeautifulSoup(html, "lxml")
        
        main = soup.select_one(".main-inner:not(.manga-bottom)")
        if not main:
            raise HTTPException(status_code=404, detail="Manga not found")
        
        title = main.select_one("h1")
        title = title.get_text(strip=True) if title else "Unknown"
        
        poster = main.select_one(".poster img")
        thumbnail = poster.get("src") if poster else None
        
        # MangaFire's "completed" means original publication is finished
        # (translation may still be ongoing).
        status_elem = main.select_one(".info > p")
        status_text = status_elem.get_text(strip=True).lower() if status_elem else None
        
        status_map = {
            "releasing": "ongoing",
            "completed": "publishing_finished",
            "on_hiatus": "on_hiatus",
            "discontinued": "cancelled"
        }
        status = status_map.get(status_text, status_text) if status_text else None
        
        # Build description: synopsis + alt-title
        description_parts = []
        synopsis = soup.select_one("#synopsis .modal-content")
        if synopsis:
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
        
        # Extract author, type, and genres from meta section
        meta = main.select_one(".meta")
        author = None
        type_info = None
        genres = []
        
        if meta:
            for span in meta.select("span"):
                text = span.get_text(strip=True)
                if "Author" in text or "author" in text.lower():
                    next_span = span.find_next_sibling("span")
                    if next_span:
                        author = next_span.get_text(strip=True)
                    break
            
            for span in meta.select("span"):
                text = span.get_text(strip=True)
                if "Type" in text or "type" in text.lower():
                    next_span = span.find_next_sibling("span")
                    if next_span:
                        type_info = next_span.get_text(strip=True)
                    break
            
            for span in meta.select("span"):
                text = span.get_text(strip=True)
                if "Genres" in text or "genres" in text.lower():
                    next_span = span.find_next_sibling("span")
                    if next_span:
                        genres_text = next_span.get_text(strip=True)
                        genres = [g.strip() for g in genres_text.split(",") if g.strip()]
                    break
            
            # Prepend type to genres list
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
        """Fetch the chapter list via the ``/ajax/manga/{id}/{type}/{lang}`` JSON endpoint."""
        # Extract numeric id from slug if needed (e.g. "one-piece.vy8" → "vy8")
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
        """Parse the HTML fragment returned by the chapters AJAX endpoint."""
        soup = BeautifulSoup(html, "lxml")
        
        chapters = []
        # Volumes use ".vol-list > .item", chapters use "li"
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
            name_elem = spans[0] if spans else None    # first <span> = chapter name
            date_str = spans[1].get_text(strip=True) if len(spans) > 1 else None  # second = date
            
            # Replace abbreviated prefix ("Chap"/"Vol") with full version
            if name_elem:
                raw_name = name_elem.get_text(strip=True)
                abbr_prefix = "Vol" if chapter_type == "volume" else "Chap"
                full_prefix = "Volume" if chapter_type == "volume" else "Chapter"
                prefix = f"{abbr_prefix} {number_str}: "
                
                if raw_name.startswith(prefix):
                    real_name = raw_name[len(prefix):]
                    if number_str not in real_name:
                        name = f"{full_prefix} {number_str}: {real_name}"
                    else:
                        name = real_name
                else:
                    name = raw_name
            else:
                name = f"{'Volume' if chapter_type == 'volume' else 'Chapter'} {number}"
            
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
        """Retrieve the page-image list for a chapter.

        Uses ``VRFHelper.get_chapter_pages_data`` to run a headless browser,
        capture the VRF-signed AJAX response, and parse the page images JSON.
        """
        if not PLAYWRIGHT_AVAILABLE:
            raise HTTPException(
                status_code=501,
                detail="Pages require headless browser. Install playwright: pip install playwright && playwright install chromium",
            )

        # Normalise to a path starting with /read/
        chapter_url = chapter_url.strip("/")
        chapter_path = f"/{chapter_url}" if chapter_url.startswith("read") else f"/read/{chapter_url}"

        data = await VRFHelper.get_chapter_pages_data(chapter_path)
        if not data:
            raise HTTPException(status_code=500, detail="Unable to capture page data for chapter")

        if "result" not in data:
            raise HTTPException(status_code=404, detail="No pages found in API response")

        return self._parse_pages(data["result"], chapter_url)

    @staticmethod
    def _parse_pages(result: dict, chapter_id: str) -> PageList:
        """Parse the images array from the page-list response.

        Each entry is ``[url, unknown, offset]`` where *offset* > 0 means
        the image tiles are shuffled and need descrambling.
        """
        pages = []
        for idx, img in enumerate(result.get("images", [])):
            if isinstance(img, list) and len(img) >= 3:
                url = img[0]
                offset = img[2] if isinstance(img[2], int) else 0
                pages.append(Page(index=idx, url=url, is_scrambled=offset > 0, scramble_offset=offset))
        return PageList(pages=pages, chapter_id=chapter_id)


# ==================== Singleton Client ====================
client = MangaFireClient()


# ==================== API Endpoints ====================

@app.get("/", tags=["Root"])
async def root():
    """API index – lists available endpoints and browser status."""
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
    """Report Playwright/Chromium availability and VRF cache stats."""
    return {
        "playwright_available": PLAYWRIGHT_AVAILABLE,
        "browser_active": VRFHelper._browser is not None if PLAYWRIGHT_AVAILABLE else False,
        "search_vrf_cache_size": len(VRFHelper._search_vrf_cache) if PLAYWRIGHT_AVAILABLE else 0,
        "message": "Headless browser ready for VRF bypass" if PLAYWRIGHT_AVAILABLE else "Install playwright: pip install playwright && playwright install chromium"
    }


@app.get("/languages", tags=["Info"])
async def get_languages():
    """List supported language codes."""
    return {
        "languages": list(SUPPORTED_LANGUAGES.keys()),
        "default": "en"
    }


@app.get("/genres", tags=["Info"])
async def get_genres():
    """List available genre filter names."""
    return {"genres": list(GENRES.keys())}


@app.get("/sort-options", tags=["Info"])
async def get_sort_options():
    """List available sort-order values."""
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
        detail = str(e) or repr(e)
        logger.error(f"[Pages] Unhandled error for {chapter_id}: {detail}", exc_info=True)
        raise HTTPException(status_code=500, detail=detail)


# ==================== Error Handlers ====================

@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    """Convert HTTPException to a standardised ErrorResponse JSON body."""
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
    """Catch-all handler – returns 500 with the exception message."""
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
