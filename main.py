"""
MangaFire API - Entry Point
Run with: python main.py
Or: uvicorn app:app --reload
"""

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )
