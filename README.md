# MangaFire API

A Python FastAPI-based API for fetching manga from MangaFire.to, based on the Kotlin Tachiyomi extension.

## Features

- **Search Manga**: Search with filters (language, genres, types, status, year)
- **Multi-Language Support**: en, es, es-la, fr, ja, pt, pt-br
- **Manga Details**: Get detailed information about manga
- **Chapter Listing**: Get chapters with language selection
- **Page Retrieval**: Get chapter pages with VRF bypass using headless browser
- **Image Descrambling**: Support for descrambling protected images
- **Headless Browser Integration**: Playwright-based VRF token bypass

## Installation

```bash
pip install -r requirements.txt

# Install Chromium for headless browser (required for VRF bypass)
playwright install chromium
```

## Running the API

```bash
# Development
uvicorn app:app --reload --host 0.0.0.0 --port 8000

# Or directly
python app.py

# Or using main.py
python main.py
```

## API Endpoints

### Info Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /` | API information |
| `GET /languages` | Supported languages |
| `GET /genres` | Available genres |
| `GET /sort-options` | Available sort options |
| `GET /browser/status` | Headless browser status |

### Search & Browse

| Endpoint | Description |
|----------|-------------|
| `GET /search` | Search manga with filters |

**Query Parameters:**
- `query` - Search keyword (requires VRF - use browser bypass)
- `page` - Page number (default: 1)
- `language` - Language code (default: en)
- `types` - Comma-separated types (manga, manhwa, manhua, etc.)
- `genres` - Comma-separated genres
- `genre_mode` - 'and' for all genres, 'or' for any
- `status` - Comma-separated status
- `year` - Comma-separated years
- `min_chapters` - Minimum chapter count
- `sort` - Sort order
- `use_browser` - Use headless browser for VRF bypass (default: true)

### Manga Operations

| Endpoint | Description |
|----------|-------------|
| `GET /manga/{manga_id}` | Get manga details |
| `GET /manga/{manga_id}/chapters` | Get chapters |
| `GET /chapter/{chapter_id}/pages` | Get chapter pages |

---

## API Examples with Responses

### 1. Root Endpoint

**Request:**
```bash
curl "http://localhost:8000/"
```

**Response:**
```json
{
  "message": "MangaFire API",
  "version": "1.0.0",
  "browser_available": true,
  "endpoints": {
    "search": "/search",
    "manga_details": "/manga/{manga_id}",
    "chapters": "/manga/{manga_id}/chapters",
    "pages": "/chapter/{chapter_id}/pages",
    "languages": "/languages",
    "genres": "/genres",
    "browser_status": "/browser/status"
  }
}
```

---

### 2. Get Languages

**Request:**
```bash
curl "http://localhost:8000/languages"
```

**Response:**
```json
{
  "languages": ["en", "es", "es-la", "fr", "ja", "pt", "pt-br"],
  "default": "en"
}
```

---

### 3. Get Genres

**Request:**
```bash
curl "http://localhost:8000/genres"
```

**Response:**
```json
{
  "genres": [
    "action", "adventure", "avant_garde", "boys_love", "comedy", 
    "demons", "drama", "ecchi", "fantasy", "girls_love", "gourmet", 
    "harem", "horror", "isekai", "iyashikei", "josei", "kids", 
    "magic", "mahou_shoujo", "martial_arts", "mecha", "military", 
    "music", "mystery", "parody", "psychological", "reverse_harem", 
    "romance", "school", "sci_fi", "seinen", "shoujo", "shounen", 
    "slice_of_life", "space", "sports", "super_power", "supernatural", 
    "suspense", "thriller", "vampire"
  ]
}
```

---

### 4. Get Sort Options

**Request:**
```bash
curl "http://localhost:8000/sort-options"
```

**Response:**
```json
{
  "sort_options": [
    "most_relevance", "recently_updated", "recently_added", 
    "release_date", "trending", "title_az", "scores", 
    "mal_scores", "most_viewed", "most_favourited"
  ]
}
```

---

### 5. Browser Status

**Request:**
```bash
curl "http://localhost:8000/browser/status"
```

**Response:**
```json
{
  "playwright_available": true,
  "browser_active": false,
  "search_vrf_cache_size": 0,
  "message": "Headless browser ready for VRF bypass"
}
```

---

### 6. Search Manga (Browse Mode)

**Request:**
```bash
curl "http://localhost:8000/search?language=en&sort=most_viewed"
```

**Response:**
```json
{
  "manga_list": [
    {
      "id": "dkw",
      "title": "One Piece",
      "url": "/manga/one-piecee.dkw",
      "thumbnail_url": "<thumbnail_url>"
    },
    {
      "id": "gry",
      "title": "The Seven Deadly Sins",
      "url": "/manga/nanatsu-no-taizaii.gry",
      "thumbnail_url": "<thumbnail_url>"
    },
    {
      "id": "ev2",
      "title": "The Promised Neverland",
      "url": "/manga/the-promised-neverlandd.ev2",
      "thumbnail_url": "<thumbnail_url>"
    }
  ],
  "has_next_page": true,
  "current_page": 1
}
```

---

### 7. Search with Filters

**Request:**
```bash
curl "http://localhost:8000/search?genres=action,adventure&status=completed&sort=scores&language=en"
```

**Response:**
```json
{
  "manga_list": [
    {
      "id": "abc123",
      "title": "Example Manga",
      "url": "/manga/example-manga.abc123",
      "thumbnail_url": "<thumbnail_url>"
    }
  ],
  "has_next_page": true,
  "current_page": 1
}
```

---

### 8. Get Chapters

**Request:**
```bash
curl "http://localhost:8000/manga/one-piecee.dkw/chapters?language=en"
```

**Response:**
```json
{
  "chapters": [
    {
      "id": "chapter-1172",
      "number": 1172.0,
      "name": "Chapter 1172: The Elbaf I Admire",
      "url": "/read/one-piecee.dkw/en/chapter-1172",
      "date_upload": "Feb 02, 2026"
    },
    {
      "id": "chapter-1171",
      "number": 1171.0,
      "name": "Chapter 1171: Ragnir",
      "url": "/read/one-piecee.dkw/en/chapter-1171",
      "date_upload": "Jan 26, 2026"
    },
    {
      "id": "chapter-1",
      "number": 1.0,
      "name": "Chapter 1: Romance Dawn",
      "url": "/read/one-piecee.dkw/en/chapter-1",
      "date_upload": "Sep 14, 2025"
    }
  ],
  "manga_id": "one-piecee.dkw",
  "language": "en"
}
```

---

### 9. Get Chapters in Different Language

**Request:**
```bash
curl "http://localhost:8000/manga/one-piecee.dkw/chapters?language=ja"
```

**Response:**
```json
{
  "chapters": [
    {
      "id": "chapter-1172",
      "number": 1172.0,
      "name": "第1172話: 僕の憧れのエルバフ",
      "url": "/read/one-piecee.dkw/ja/chapter-1172",
      "date_upload": "Feb 02, 2026"
    }
  ],
  "manga_id": "one-piecee.dkw",
  "language": "ja"
}
```

---

### 10. Get Pages (with Headless Browser VRF Bypass)

**Request:**
```bash
curl "http://localhost:8000/chapter/read/one-piecee.dkw/en/chapter-1172/pages"
```

**Response:**
```json
{
  "pages": [
    {
      "index": 0,
      "url": "<page_image_url>",
      "is_scrambled": false,
      "scramble_offset": 0
    },
    {
      "index": 1,
      "url": "<page_image_url>",
      "is_scrambled": false,
      "scramble_offset": 0
    },
    {
      "index": 2,
      "url": "<page_image_url>",
      "is_scrambled": true,
      "scramble_offset": 5
    }
  ],
  "chapter_id": "read/one-piecee.dkw/en/chapter-1172"
}
```

---

### 11. Error Response Examples

**Invalid Manga ID:**
```bash
curl "http://localhost:8000/manga/invalid-no-id"
```

**Response:**
```json
{
  "error": "HTTPException",
  "detail": "Invalid manga ID format",
  "status_code": 400
}
```

**Manga Not Found:**
```bash
curl "http://localhost:8000/manga/nonexistent.999999"
```

**Response:**
```json
{
  "error": "HTTPException",
  "detail": "Manga not found",
  "status_code": 404
}
```

**Invalid Chapter URL:**
```bash
curl "http://localhost:8000/chapter/invalid/pages"
```

**Response:**
```json
{
  "error": "HTTPException",
  "detail": "Could not parse chapter URL: invalid",
  "status_code": 400
}
```

**Pages Require Browser (VRF):**
```bash
curl "http://localhost:8000/chapter/read/manga.id/en/chapter-1/pages?use_browser=false"
```

**Response:**
```json
{
  "error": "HTTPException",
  "detail": "No pages found. Try with use_browser=true for VRF bypass.",
  "status_code": 404
}
```

---

## Testing

Run the test suite:

```bash
# Run all tests with pytest
pytest test_api.py -v

# Run tests directly with Python
python test_api.py

# Run specific test class
pytest test_api.py::TestBorutoVortexWorkflow -v -s
```

---

## Supported Languages

| Code | Language |
|------|----------|
| en | English |
| es | Spanish |
| es-la | Spanish (Latin America) |
| fr | French |
| ja | Japanese |
| pt | Portuguese |
| pt-br | Portuguese (Brazil) |

---

## Complete Genre List

| Genre | ID |
|-------|-----|
| action | 1 |
| adventure | 78 |
| avant_garde | 3 |
| boys_love | 4 |
| comedy | 5 |
| demons | 77 |
| drama | 6 |
| ecchi | 7 |
| fantasy | 79 |
| girls_love | 9 |
| gourmet | 10 |
| harem | 11 |
| horror | 530 |
| isekai | 13 |
| iyashikei | 531 |
| josei | 15 |
| kids | 532 |
| magic | 539 |
| mahou_shoujo | 533 |
| martial_arts | 534 |
| mecha | 19 |
| military | 535 |
| music | 21 |
| mystery | 22 |
| parody | 23 |
| psychological | 536 |
| reverse_harem | 25 |
| romance | 26 |
| school | 73 |
| sci_fi | 28 |
| seinen | 537 |
| shoujo | 30 |
| shounen | 31 |
| slice_of_life | 538 |
| space | 33 |
| sports | 34 |
| super_power | 75 |
| supernatural | 76 |
| suspense | 37 |
| thriller | 38 |
| vampire | 39 |

---

## Sort Options

| Option | Description |
|--------|-------------|
| most_relevance | Most relevant to search |
| recently_updated | Recently updated manga |
| recently_added | Recently added to site |
| release_date | By release date |
| trending | Currently trending |
| title_az | Alphabetical order |
| scores | By user scores |
| mal_scores | By MyAnimeList scores |
| most_viewed | Most viewed |
| most_favourited | Most favourited |

---

## License

MIT
