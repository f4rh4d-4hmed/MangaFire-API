# MangaFire API

A Python FastAPI-based API for fetching manga from MangaFire.to, based on the Kotlin Tachiyomi extension.

## Features

- **Search Manga**: Search with filters (language, genres, types, status, year)
- **Multi-Language Support**: en, es, es-la, fr, ja, pt, pt-br
- **Manga Details**: Get detailed information about manga
- **Chapter Listing**: Get chapters with language selection
- **Page Retrieval**: Get chapter pages with scramble detection
- **Image Descrambling**: Support for descrambling protected images

## Installation

```bash
pip install -r requirements.txt
```

## Running the API

```bash
# Development
uvicorn app:app --reload --host 0.0.0.0 --port 8000

# Or directly
python app.py
```

## API Endpoints

### Info Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /` | API information |
| `GET /languages` | Supported languages |
| `GET /genres` | Available genres |
| `GET /sort-options` | Available sort options |

### Search & Browse

| Endpoint | Description |
|----------|-------------|
| `GET /search` | Search manga with filters |

**Query Parameters:**
- `query` - Search keyword
- `page` - Page number (default: 1)
- `language` - Language code (default: en)
- `types` - Comma-separated types (manga, manhwa, manhua, etc.)
- `genres` - Comma-separated genres
- `genre_mode` - 'and' for all genres, 'or' for any
- `status` - Comma-separated status
- `year` - Comma-separated years
- `min_chapters` - Minimum chapter count
- `sort` - Sort order

### Manga Operations

| Endpoint | Description |
|----------|-------------|
| `GET /manga/{manga_id}` | Get manga details |
| `GET /manga/{manga_id}/chapters` | Get chapters |
| `GET /chapter/{chapter_id}/pages` | Get chapter pages |

## Examples

### Search for manga
```bash
curl "http://localhost:8000/search?query=one+piece&language=en&sort=most_viewed"
```

### Get manga details
```bash
curl "http://localhost:8000/manga/one-piece.vy8"
```

### Get chapters
```bash
curl "http://localhost:8000/manga/one-piece.vy8/chapters?language=en"
```

### Get pages
```bash
curl "http://localhost:8000/chapter/read/one-piece.vy8/en/chapter/1/pages"
```

## Testing

Run the test suite:

```bash
pytest test_api.py -v
```

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

## Genres

action, adventure, avant_garde, boys_love, comedy, demons, drama, ecchi, fantasy, girls_love, gourmet, harem, horror, isekai, iyashikei, josei, kids, magic, mahou_shoujo, martial_arts, mecha, military, music, mystery, parody, psychological, reverse_harem, romance, school, sci_fi, seinen, shoujo, shounen, slice_of_life, space, sports, super_power, supernatural, suspense, thriller, vampire

## Sort Options

- most_relevance
- recently_updated
- recently_added
- release_date
- trending
- title_az
- scores
- mal_scores
- most_viewed
- most_favourited

## License

MIT

---
# Original MangaFire-API
A mangafire python wrapper api
