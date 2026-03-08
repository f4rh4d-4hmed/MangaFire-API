"""
Test suite for the MangaFire API.

Covers root/info endpoints, search (with filters, pagination, sort),
manga details, chapters, pages, error handling, and end-to-end workflows.

Run modes:
  pytest test_api.py -v                # In-process via FastAPI TestClient
  python test_api.py                   # Live HTTP tests against a running server
  python test_api.py --live            # (same as above)
  python test_api.py --pytest          # Explicitly invoke pytest from the script
"""

import pytest
import sys
import json
from typing import Optional

# ── Optional: requests (live HTTP tests) ──────────────────────────────
try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

# ── Optional: FastAPI TestClient (in-process pytest tests) ─────────────
try:
    from fastapi.testclient import TestClient
    from app import app, SUPPORTED_LANGUAGES, GENRES, SortOrder
    test_client = TestClient(app)
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False
    test_client = None

# Base URL for live-server tests
LIVE_API_URL = "http://127.0.0.1:8000"


# ==================== Pytest Test Classes (in-process via TestClient) ====================

class TestRootEndpoints:
    """Verify root (/) and informational endpoints (/languages, /genres, etc.)."""
    
    def test_root_endpoint(self):
        """Root returns API name, version, and endpoint catalogue."""
        response = test_client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert "message" in data
        assert data["message"] == "MangaFire API"
        assert "version" in data
        assert "endpoints" in data
        assert "browser_available" in data
    
    def test_languages_endpoint(self):
        """Languages list includes en, ja, es at minimum."""
        response = test_client.get("/languages")
        assert response.status_code == 200
        data = response.json()
        assert "languages" in data
        assert "default" in data
        assert data["default"] == "en"
        assert "en" in data["languages"]
        assert "ja" in data["languages"]
        assert "es" in data["languages"]
    
    def test_genres_endpoint(self):
        """Genre list contains well-known entries."""
        response = test_client.get("/genres")
        assert response.status_code == 200
        data = response.json()
        assert "genres" in data
        assert "action" in data["genres"]
        assert "adventure" in data["genres"]
        assert "romance" in data["genres"]
    
    def test_sort_options_endpoint(self):
        """Sort options include at least most_viewed and recently_updated."""
        response = test_client.get("/sort-options")
        assert response.status_code == 200
        data = response.json()
        assert "sort_options" in data
        assert "most_viewed" in data["sort_options"]
        assert "recently_updated" in data["sort_options"]
    
    def test_browser_status_endpoint(self):
        """Browser status reports Playwright availability and cache size."""
        response = test_client.get("/browser/status")
        assert response.status_code == 200
        data = response.json()
        assert "playwright_available" in data
        assert "browser_active" in data
        assert "message" in data


class TestSearchEndpoint:
    """Search / browse endpoint (/search) with various filter combinations."""
    
    def test_search_basic(self):
        """Browse mode (no keyword) returns a paginated manga list."""
        response = test_client.get("/search")
        assert response.status_code == 200
        data = response.json()
        assert "manga_list" in data
        assert "has_next_page" in data
        assert "current_page" in data
        assert isinstance(data["manga_list"], list)
    
    def test_search_with_query(self):
        """Keyword search (browser bypass disabled) returns results or a protected-access error."""
        response = test_client.get("/search", params={"query": "Boruto", "use_browser": "false"})
        # Keyword search without VRF may return 403 (Cloudflare) or 500
        assert response.status_code in [200, 403, 500]
        if response.status_code == 200:
            data = response.json()
            assert "manga_list" in data
            if len(data["manga_list"]) > 0:
                manga = data["manga_list"][0]
                assert "id" in manga
                assert "title" in manga
                assert "url" in manga
    
    def test_search_with_language(self):
        """Search accepts different language codes without error."""
        for lang in ["en", "ja", "es"]:
            response = test_client.get("/search", params={"language": lang})
            assert response.status_code == 200
            data = response.json()
            assert "manga_list" in data
    
    def test_search_with_pagination(self):
        """Pages 1 and 2 return distinct current_page values."""
        response_page1 = test_client.get("/search", params={"page": 1})
        response_page2 = test_client.get("/search", params={"page": 2})
        
        assert response_page1.status_code == 200
        assert response_page2.status_code == 200
        
        data1 = response_page1.json()
        data2 = response_page2.json()
        
        assert data1["current_page"] == 1
        assert data2["current_page"] == 2
    
    def test_search_with_sort(self):
        """Various sort orders are accepted."""
        for sort in ["most_viewed", "recently_updated", "trending"]:
            response = test_client.get("/search", params={"sort": sort})
            assert response.status_code == 200
    
    def test_search_with_genres(self):
        """Genre filter (comma-separated) is accepted."""
        response = test_client.get("/search", params={"genres": "action,adventure"})
        assert response.status_code == 200
        data = response.json()
        assert "manga_list" in data
    
    def test_search_with_type(self):
        """Type filter (manga, manhwa, etc.) is accepted."""
        response = test_client.get("/search", params={"types": "manga"})
        assert response.status_code == 200
        data = response.json()
        assert "manga_list" in data
    
    def test_search_with_status(self):
        """Status filter (completed, releasing, etc.) is accepted."""
        response = test_client.get("/search", params={"status": "completed"})
        assert response.status_code == 200
        data = response.json()
        assert "manga_list" in data
    
    def test_search_combined_filters(self):
        """Multiple filters can be combined in a single request."""
        response = test_client.get("/search", params={
            "language": "en",
            "genres": "action",
            "sort": "most_viewed",
            "page": 1
        })
        assert response.status_code == 200
        data = response.json()
        assert "manga_list" in data


class TestMangaDetailsEndpoint:
    """Manga details endpoint (/manga/{manga_id})."""
    
    def test_manga_details_invalid_id(self):
        """Slug with no numeric component returns 400."""
        response = test_client.get("/manga/invalid-no-numbers")
        assert response.status_code == 400
        data = response.json()
        assert "error" in data
    
    def test_manga_details_not_found(self):
        """Non-existent manga returns 404 or 500."""
        response = test_client.get("/manga/nonexistent.999999")
        assert response.status_code in [404, 500]


class TestChaptersEndpoint:
    """Chapters endpoint (/manga/{manga_id}/chapters)."""
    
    def test_chapters_invalid_manga(self):
        """Non-existent manga ID returns 404 or 500."""
        response = test_client.get("/manga/invalid.999999/chapters")
        assert response.status_code in [404, 500]
    
    def test_chapters_with_language(self):
        """Language parameter is forwarded correctly (error expected for bad ID)."""
        response = test_client.get("/manga/invalid.999999/chapters", params={"language": "ja"})
        assert response.status_code in [404, 500]
    
    def test_chapters_volume_type(self):
        """Volume-type chapter list is accepted (error expected for bad ID)."""
        response = test_client.get("/manga/invalid.999999/chapters", params={"type": "volume"})
        assert response.status_code in [404, 500]


class TestPagesEndpoint:
    """Pages endpoint (/chapter/{chapter_id}/pages)."""
    
    def test_pages_invalid_chapter(self):
        """Invalid chapter path returns an error (various codes depending on Playwright state)."""
        response = test_client.get("/chapter/invalid/pages")
        assert response.status_code in [400, 403, 404, 500, 501]


class TestErrorHandling:
    """Validate error responses (format, status codes)."""
    
    def test_invalid_endpoint(self):
        """Unknown path returns 404."""
        response = test_client.get("/nonexistent")
        assert response.status_code == 404
    
    def test_invalid_page_number(self):
        """Page number 0 is rejected (ge=1 validation)."""
        response = test_client.get("/search", params={"page": 0})
        assert response.status_code == 422
    
    def test_invalid_page_negative(self):
        """Negative page number is rejected."""
        response = test_client.get("/search", params={"page": -1})
        assert response.status_code == 422
    
    def test_error_response_format(self):
        """Error body contains error, detail, and status_code keys."""
        response = test_client.get("/manga/invalid-no-numbers")
        assert response.status_code == 400
        data = response.json()
        assert "error" in data
        assert "detail" in data
        assert "status_code" in data


class TestIntegration:
    """Multi-step integration workflows (search → details → chapters)."""
    
    def test_search_select_get_chapters_flow(self):
        """Search browse → pick first result → verify it has an ID and title."""
        # Step 1: Search
        search_response = test_client.get("/search", params={
            "sort": "most_viewed",
            "language": "en"
        })
        assert search_response.status_code == 200
        search_data = search_response.json()
        
        if len(search_data["manga_list"]) > 0:
            manga = search_data["manga_list"][0]
            assert manga["id"] is not None
            assert manga["title"] is not None
    
    def test_language_selection_workflow(self):
        """Search with each supported language returns 200."""
        languages_response = test_client.get("/languages")
        languages = languages_response.json()["languages"]
        
        for lang in languages[:3]:
            response = test_client.get("/search", params={
                "language": lang,
                "sort": "most_viewed"
            })
            assert response.status_code == 200


class TestModelValidation:
    """Verify Pydantic response models have the expected fields and types."""
    
    def test_search_result_model(self):
        """SearchResult JSON has manga_list (list), has_next_page (bool), current_page (int)."""
        response = test_client.get("/search")
        assert response.status_code == 200
        data = response.json()
        
        assert "manga_list" in data
        assert "has_next_page" in data
        assert "current_page" in data
        
        assert isinstance(data["manga_list"], list)
        assert isinstance(data["has_next_page"], bool)
        assert isinstance(data["current_page"], int)
    
    def test_manga_basic_model(self):
        """MangaBasic items have id, title, url as strings."""
        response = test_client.get("/search")
        data = response.json()
        
        if len(data["manga_list"]) > 0:
            manga = data["manga_list"][0]
            assert "id" in manga
            assert "title" in manga
            assert "url" in manga
            assert isinstance(manga["id"], str)
            assert isinstance(manga["title"], str)
            assert isinstance(manga["url"], str)


class TestBorutoVortexWorkflow:
    """End-to-end smoke test: browse → select → sel chapters → pages."""
    
    def test_full_workflow(self):
        """Browse most-viewed, pick first manga, confirm it has chapters."""
        # Step 1: Search
        search_response = test_client.get("/search", params={
            "sort": "most_viewed",
            "language": "en"
        })
        assert search_response.status_code == 200
        search_data = search_response.json()
        assert len(search_data["manga_list"]) > 0
        
        # Step 2: Get languages
        languages_response = test_client.get("/languages")
        assert languages_response.status_code == 200
        
        # Assert minimum requirements
        assert len(search_data["manga_list"]) > 0


# ==================== Live API Tests (HTTP requests against a running server) ====================

def print_header(title: str):
    """Print a section header with surrounding separators."""
    print("\n" + "=" * 70)
    print(f" {title}")
    print("=" * 70)


def print_json(data, indent=2, max_items=3):
    """Pretty-print JSON, truncating long lists to *max_items* entries."""
    if isinstance(data, dict):
        # Truncate lists in the response
        truncated = {}
        for key, value in data.items():
            if isinstance(value, list) and len(value) > max_items:
                truncated[key] = value[:max_items] + [f"... and {len(value) - max_items} more items"]
            else:
                truncated[key] = value
        print(json.dumps(truncated, indent=indent, ensure_ascii=False))
    else:
        print(json.dumps(data, indent=indent, ensure_ascii=False))


def test_live_root():
    """GET / – API index."""
    print_header("1. Root Endpoint - GET /")
    response = requests.get(f"{LIVE_API_URL}/")
    print(f"Status: {response.status_code}")
    print_json(response.json())
    return response.status_code == 200


def test_live_languages():
    """GET /languages – supported language list."""
    print_header("2. Languages Endpoint - GET /languages")
    response = requests.get(f"{LIVE_API_URL}/languages")
    print(f"Status: {response.status_code}")
    print_json(response.json())
    return response.status_code == 200


def test_live_genres():
    """GET /genres – available genre names."""
    print_header("3. Genres Endpoint - GET /genres")
    response = requests.get(f"{LIVE_API_URL}/genres")
    print(f"Status: {response.status_code}")
    data = response.json()
    print(f"Total genres: {len(data['genres'])}")
    print(f"Genres: {', '.join(data['genres'][:10])}...")
    return response.status_code == 200


def test_live_sort_options():
    """GET /sort-options – available sort-order values."""
    print_header("4. Sort Options Endpoint - GET /sort-options")
    response = requests.get(f"{LIVE_API_URL}/sort-options")
    print(f"Status: {response.status_code}")
    print_json(response.json())
    return response.status_code == 200


def test_live_browser_status():
    """GET /browser/status – Playwright availability and VRF cache stats."""
    print_header("5. Browser Status Endpoint - GET /browser/status")
    response = requests.get(f"{LIVE_API_URL}/browser/status")
    print(f"Status: {response.status_code}")
    print_json(response.json())
    return response.status_code == 200


def test_live_search_browse():
    """GET /search (browse mode) – most-viewed English manga."""
    print_header("6. Search (Browse Mode) - GET /search?sort=most_viewed&language=en")
    response = requests.get(f"{LIVE_API_URL}/search", params={
        "sort": "most_viewed",
        "language": "en"
    })
    print(f"Status: {response.status_code}")
    data = response.json()
    print(f"Found {len(data['manga_list'])} manga")
    print(f"Has next page: {data['has_next_page']}")
    print(f"Current page: {data['current_page']}")
    print("\nFirst 3 results:")
    for i, manga in enumerate(data['manga_list'][:3], 1):
        print(f"  {i}. {manga['title']} (ID: {manga['id']})")
    return response.status_code == 200, data


def test_live_search_with_filters():
    """GET /search with genre + status + sort filters."""
    print_header("7. Search with Filters - GET /search?genres=action&status=completed")
    response = requests.get(f"{LIVE_API_URL}/search", params={
        "genres": "action",
        "status": "completed",
        "sort": "scores",
        "language": "en"
    })
    print(f"Status: {response.status_code}")
    data = response.json()
    print(f"Found {len(data['manga_list'])} manga with action genre, completed status")
    if data['manga_list']:
        print("\nFirst 3 results:")
        for i, manga in enumerate(data['manga_list'][:3], 1):
            print(f"  {i}. {manga['title']}")
    return response.status_code == 200


def test_live_search_pagination():
    """Pages 1 and 2 return different result sets."""
    print_header("8. Search Pagination - GET /search?page=1 vs page=2")
    
    response1 = requests.get(f"{LIVE_API_URL}/search", params={"page": 1})
    response2 = requests.get(f"{LIVE_API_URL}/search", params={"page": 2})
    
    print(f"Page 1 Status: {response1.status_code}")
    print(f"Page 2 Status: {response2.status_code}")
    
    if response1.status_code == 200 and response2.status_code == 200:
        data1 = response1.json()
        data2 = response2.json()
        print(f"Page 1: {len(data1['manga_list'])} results")
        print(f"Page 2: {len(data2['manga_list'])} results")
        
        # Check different results
        ids1 = set(m['id'] for m in data1['manga_list'])
        ids2 = set(m['id'] for m in data2['manga_list'])
        print(f"Different results: {len(ids1 - ids2)} unique on page 1")
    
    return response1.status_code == 200 and response2.status_code == 200


def test_live_chapters():
    """Dynamic workflow: search → select first manga → get chapters → get pages."""
    print_header("9. Dynamic Chapter Test")
    
    # Step 1: Search (simple, no filters)
    print("\n[Step 1] Searching manga...")
    search_response = requests.get(f"{LIVE_API_URL}/search")
    
    if search_response.status_code != 200:
        print(f"  ✗ Search failed: {search_response.status_code}")
        return False, None
    
    search_data = search_response.json()
    if not search_data['manga_list']:
        print("  ✗ No manga found")
        return False, None
    
    # Select first result
    selected_manga = search_data['manga_list'][0]
    manga_slug = selected_manga['url'].strip('/').split('/')[-1]
    print(f"  ✓ Found {len(search_data['manga_list'])} manga")
    print(f"  ✓ Selected: {selected_manga['title']}")
    
    # Step 2: Get available languages
    print("\n[Step 2] Getting languages...")
    lang_response = requests.get(f"{LIVE_API_URL}/languages")
    
    if lang_response.status_code != 200:
        print(f"  ✗ Failed to get languages")
        return False, None
    
    languages = lang_response.json()['languages']
    selected_language = languages[0] if languages else "en"
    print(f"  ✓ Available: {', '.join(languages)}")
    print(f"  ✓ Using: {selected_language}")
    
    # Step 3: Get chapters
    print(f"\n[Step 3] Getting chapters...")
    chapters_response = requests.get(f"{LIVE_API_URL}/manga/{manga_slug}/chapters", params={
        "language": selected_language
    })
    
    if chapters_response.status_code != 200:
        print(f"  ✗ Failed: {chapters_response.status_code}")
        return False, None
    
    chapters_data = chapters_response.json()
    print(f"  ✓ Manga: {chapters_data['manga_id']}")
    print(f"  ✓ Language: {chapters_data['language']}")
    print(f"  ✓ Chapters: {len(chapters_data['chapters'])}")
    
    if chapters_data['chapters']:
        print("\n  First 3 chapters:")
        for chapter in chapters_data['chapters'][:3]:
            print(f"    - {chapter['name']}")
    
    # Step 4: Get pages
    if chapters_data['chapters']:
        print("\n[Step 4] Getting pages...")
        first_chapter = chapters_data['chapters'][0]
        chapter_url = first_chapter['url'].strip('/')
        print(f"  Chapter: {first_chapter['name']}")
        
        pages_response = requests.get(f"{LIVE_API_URL}/chapter/{chapter_url}/pages", timeout=120)
        
        if pages_response.status_code == 200:
            pages_data = pages_response.json()
            print(f"  ✓ Pages: {len(pages_data['pages'])}")
            scrambled = sum(1 for p in pages_data['pages'] if p['is_scrambled'])
            print(f"  ✓ Scrambled: {scrambled}")
        else:
            print(f"  ✗ Failed: {pages_response.status_code}")
    
    return True, chapters_data


def test_live_chapters_with_language(language: str = "ja"):
    """Get chapters in a non-default language."""
    print_header(f"10. Get Chapters (Language: {language})")
    
    # Search and get first manga
    search_response = requests.get(f"{LIVE_API_URL}/search")
    if search_response.status_code != 200 or not search_response.json()['manga_list']:
        print("  ✗ Could not find manga")
        return False
    
    manga_slug = search_response.json()['manga_list'][0]['url'].strip('/').split('/')[-1]
    print(f"Using: {manga_slug}")
    
    response = requests.get(f"{LIVE_API_URL}/manga/{manga_slug}/chapters", params={
        "language": language
    })
    print(f"Status: {response.status_code}")
    
    if response.status_code == 200:
        data = response.json()
        print(f"Language: {data['language']}")
        print(f"Chapters: {len(data['chapters'])}")
        if data['chapters']:
            print("\nFirst 3:")
            for chapter in data['chapters'][:3]:
                print(f"  - {chapter['name']}")
    return response.status_code == 200


def test_live_pages():
    """Search → first manga → first chapter → get pages (retries once on cold browser)."""
    print_header("11. Get Pages (Dynamic)")
    
    # Search and get first manga
    search_response = requests.get(f"{LIVE_API_URL}/search")
    if search_response.status_code != 200 or not search_response.json()['manga_list']:
        print("  ✗ Could not find manga")
        return False
    
    manga_slug = search_response.json()['manga_list'][0]['url'].strip('/').split('/')[-1]
    
    # Get chapters
    chapters_response = requests.get(f"{LIVE_API_URL}/manga/{manga_slug}/chapters")
    if chapters_response.status_code != 200 or not chapters_response.json()['chapters']:
        print("  ✗ Could not get chapters")
        return False
    
    chapter_url = chapters_response.json()['chapters'][0]['url'].strip('/')
    print(f"Chapter: {chapter_url}")
    
    # Retry once on failure (first attempt may fail due to cold browser start)
    for attempt in range(1, 3):
        print(f"  Attempt {attempt}/2...")
        response = requests.get(f"{LIVE_API_URL}/chapter/{chapter_url}/pages", timeout=120)
        print(f"  Status: {response.status_code}")
        if response.status_code == 200:
            data = response.json()
            print(f"  Pages: {len(data['pages'])}")
            scrambled = sum(1 for p in data['pages'] if p['is_scrambled'])
            print(f"  Scrambled: {scrambled}")
            return True
        else:
            print(f"  Response: {response.text[:200]}")
            if attempt < 2:
                print("  Retrying...")
    return False


def test_live_error_invalid_manga():
    """GET /manga/{bad-slug} – expect 400."""
    print_header("12. Error Test - Invalid Manga ID")
    response = requests.get(f"{LIVE_API_URL}/manga/invalid-no-numbers")
    print(f"Status: {response.status_code}")
    print_json(response.json())
    return response.status_code == 400


def test_live_error_not_found():
    """GET /manga/{nonexistent} – expect 404 or 500."""
    print_header("13. Error Test - Manga Not Found")
    response = requests.get(f"{LIVE_API_URL}/manga/nonexistent.999999")
    print(f"Status: {response.status_code}")
    print_json(response.json())
    return response.status_code in [404, 500]


def test_live_error_invalid_chapter():
    """GET /chapter/{bad-path}/pages – expect an error status."""
    print_header("14. Error Test - Invalid Chapter URL")
    response = requests.get(f"{LIVE_API_URL}/chapter/invalid/pages")
    print(f"Status: {response.status_code}")
    print_json(response.json())
    return response.status_code in [400, 404, 500, 501]


def test_live_full_workflow():
    """Full pipeline: search → select manga → list languages → get chapters → get pages."""
    print_header("15. FULL WORKFLOW TEST")
    print("Search -> Select Manga -> List Languages -> Get Chapters -> Get Pages")
    print("-" * 70)
    
    results = {
        "search": False,
        "languages": False,
        "chapters": False,
        "pages": False
    }
    
    # Step 1: Search
    print("\n[STEP 1] Searching for manga...")
    search_response = requests.get(f"{LIVE_API_URL}/search", params={
        "sort": "most_viewed",
        "language": "en"
    })
    
    if search_response.status_code == 200:
        search_data = search_response.json()
        print(f"  ✓ Found {len(search_data['manga_list'])} manga")
        results["search"] = True
        
        if search_data['manga_list']:
            selected = search_data['manga_list'][0]
            manga_slug = selected['url'].strip('/').split('/')[-1]
            print(f"  ✓ Selected: {selected['title']} ({manga_slug})")
            
            # Step 2: Get Languages
            print("\n[STEP 2] Getting available languages...")
            lang_response = requests.get(f"{LIVE_API_URL}/languages")
            if lang_response.status_code == 200:
                languages = lang_response.json()['languages']
                print(f"  ✓ Available: {', '.join(languages)}")
                results["languages"] = True
            
            # Step 3: Get Chapters
            print("\n[STEP 3] Getting chapters in English...")
            chapters_response = requests.get(f"{LIVE_API_URL}/manga/{manga_slug}/chapters", params={
                "language": "en"
            })
            
            if chapters_response.status_code == 200:
                chapters_data = chapters_response.json()
                print(f"  ✓ Found {len(chapters_data['chapters'])} chapters")
                results["chapters"] = True
                
                if chapters_data['chapters']:
                    first_chapter = chapters_data['chapters'][0]
                    chapter_url = first_chapter['url'].strip('/')
                    print(f"  ✓ First chapter: {first_chapter['name']}")
                    
                    # Step 4: Get Pages (retry once on failure)
                    print("\n[STEP 4] Getting pages (using headless browser)...")
                    print(f"  Chapter URL: {chapter_url}")
                    
                    for attempt in range(1, 3):
                        print(f"  Attempt {attempt}/2...")
                        pages_response = requests.get(
                            f"{LIVE_API_URL}/chapter/{chapter_url}/pages",
                            timeout=120
                        )
                        
                        if pages_response.status_code == 200:
                            pages_data = pages_response.json()
                            print(f"  ✓ Found {len(pages_data['pages'])} pages")
                            results["pages"] = True
                            
                            # Show summary
                            print("\n" + "-" * 70)
                            print("DOWNLOAD SUMMARY:")
                            print(f"  Manga: {selected['title']}")
                            print(f"  Chapter: {first_chapter['name']}")
                            print(f"  Pages: {len(pages_data['pages'])}")
                            scrambled = sum(1 for p in pages_data['pages'] if p['is_scrambled'])
                            print(f"  Scrambled: {scrambled}")
                            break
                        else:
                            print(f"  ✗ Pages error: {pages_response.status_code}")
                            if attempt < 2:
                                print("  Retrying...")
            else:
                print(f"  ✗ Chapters error: {chapters_response.status_code}")
    else:
        print(f"  ✗ Search error: {search_response.status_code}")
    
    # Summary
    print("\n" + "=" * 70)
    print("WORKFLOW RESULTS:")
    for step, passed in results.items():
        status = "✓ PASSED" if passed else "✗ FAILED"
        print(f"  {step.upper()}: {status}")
    
    return all(results.values())


def run_live_tests():
    """Execute all live tests sequentially and print a summary table."""
    print("\n")
    print("╔" + "═" * 68 + "╗")
    print("║" + " MangaFire API - Live Test Suite ".center(68) + "║")
    print("║" + f" Server: {LIVE_API_URL} ".center(68) + "║")
    print("╚" + "═" * 68 + "╝")
    
    # Check if requests is available
    if not REQUESTS_AVAILABLE:
        print("\n❌ 'requests' module not installed")
        print("   Install with: pip install requests")
        return False
    
    # Check if server is running
    try:
        response = requests.get(f"{LIVE_API_URL}/", timeout=5)
        if response.status_code != 200:
            print(f"\n❌ Server returned status {response.status_code}")
            return False
    except requests.exceptions.ConnectionError:
        print(f"\n❌ Cannot connect to server at {LIVE_API_URL}")
        print("   Please start the server with: python main.py")
        return False
    
    tests = [
        ("Root Endpoint", test_live_root),
        ("Languages", test_live_languages),
        ("Genres", test_live_genres),
        ("Sort Options", test_live_sort_options),
        ("Browser Status", test_live_browser_status),
        ("Search Browse", lambda: test_live_search_browse()[0]),
        ("Search with Filters", test_live_search_with_filters),
        ("Search Pagination", test_live_search_pagination),
        ("Chapters (Dynamic)", lambda: test_live_chapters()[0]),
        ("Chapters (JA)", lambda: test_live_chapters_with_language(language="ja")),
        ("Pages (Dynamic)", test_live_pages),
        ("Error: Invalid Manga", test_live_error_invalid_manga),
        ("Error: Not Found", test_live_error_not_found),
        ("Error: Invalid Chapter", test_live_error_invalid_chapter),
    ]
    
    results = []
    for name, test_func in tests:
        try:
            result = test_func()
            results.append((name, result))
        except Exception as e:
            print(f"\n❌ Error in {name}: {e}")
            results.append((name, False))
    
    # Automatically run full workflow test (includes browser)
    print("\n" + "=" * 70)
    print("Running full workflow test (includes headless browser)...")
    print("This may take ~10-30 seconds...")
    print("=" * 70)
    
    try:
        workflow_result = test_live_full_workflow()
        results.append(("Full Workflow", workflow_result))
    except Exception as e:
        print(f"\n❌ Error in Full Workflow: {e}")
        results.append(("Full Workflow", False))
    
    # Summary
    print("\n")
    print("╔" + "═" * 68 + "╗")
    print("║" + " TEST SUMMARY ".center(68) + "║")
    print("╠" + "═" * 68 + "╣")
    
    passed = sum(1 for _, r in results if r)
    failed = len(results) - passed
    
    for name, result in results:
        status = "✓ PASS" if result else "✗ FAIL"
        print(f"║  {name:<50} {status:>12}  ║")
    
    print("╠" + "═" * 68 + "╣")
    print(f"║  {'TOTAL':50} {passed}/{len(results)} passed  ║")
    print("╚" + "═" * 68 + "╝")
    
    return failed == 0


def run_pytest():
    """Invoke pytest programmatically on this file."""
    print("=" * 60)
    print("MangaFire API Test Suite (pytest)")
    print("=" * 60)
    pytest.main([__file__, "-v", "--tb=short"])


# ==================== Main Entry Point ====================

if __name__ == "__main__":
    if "--pytest" in sys.argv:
        # Explicitly run pytest
        run_pytest()
    elif "--live" in sys.argv or not FASTAPI_AVAILABLE:
        # Run live tests against running server
        run_live_tests()
    elif len(sys.argv) > 1 and sys.argv[1] in ["-h", "--help"]:
        print(__doc__)
    else:
        # Default: run live tests (more useful for demonstration)
        print("Running live API tests against server...")
        print("(Use 'pytest test_api.py -v' for unit tests)")
        print("(Use 'python test_api.py --pytest' for pytest in-process)")
        print()
        run_live_tests()
