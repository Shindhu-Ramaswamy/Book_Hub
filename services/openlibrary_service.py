"""
Open Library service
====================
Two main jobs:
  1. cover_url(isbn)       → cover image URL from covers.openlibrary.org (no request needed)
  2. fetch_book(isbn)      → dict with full metadata from the Works/Editions API
  3. enrich_books(list)    → attaches .cover_url to each book object (used in templates)
  4. search_and_import(q)  → search OL by title/author, returns list of dicts ready to import

No API key required. Uses the public Open Library REST API.
All network calls have a 3-second timeout and return None on failure.
"""

import re
import requests
from functools import lru_cache

_COVERS  = "https://covers.openlibrary.org/b/isbn/{isbn}-M.jpg"
_BIBLIO  = "https://openlibrary.org/api/books?bibkeys=ISBN:{isbn}&jscmd=details&format=json"
_SEARCH  = "https://openlibrary.org/search.json"
_TIMEOUT = 3

# Common Open Library language codes → display names. Falls back to the
# raw code (uppercased) for anything not in this short list.
_LANGUAGE_NAMES = {
    "eng": "English", "fre": "French", "fra": "French", "ger": "German",
    "deu": "German", "spa": "Spanish", "ita": "Italian", "por": "Portuguese",
    "rus": "Russian", "chi": "Chinese", "zho": "Chinese", "jpn": "Japanese",
    "kor": "Korean", "ara": "Arabic", "hin": "Hindi", "ben": "Bengali",
    "tam": "Tamil", "tel": "Telugu", "mar": "Marathi", "guj": "Gujarati",
    "kan": "Kannada", "mal": "Malayalam", "pan": "Punjabi", "urd": "Urdu",
    "dut": "Dutch", "nld": "Dutch", "swe": "Swedish", "pol": "Polish",
    "gre": "Greek", "ell": "Greek", "heb": "Hebrew", "tur": "Turkish",
    "lat": "Latin",
}


# ── Cover URL ─────────────────────────────────────────────────────────
def cover_url(isbn: str) -> str | None:
    """Return the Open Library cover image URL for an ISBN. No HTTP call."""
    if not isbn or not isbn.strip():
        return None
    clean = isbn.strip().replace("-", "").replace(" ", "")
    return _COVERS.format(isbn=clean)


# ── Single book metadata ───────────────────────────────────────────────
@lru_cache(maxsize=256)
def fetch_book(isbn: str) -> dict | None:
    """
    Fetch full metadata for a single ISBN from Open Library.
    Returns a dict or None on failure.

    Keys returned:
        title, author, publisher, publish_date, publish_year,
        num_pages, language, description, subjects, cover_url, ol_url
    """
    if not isbn:
        return None
    clean = isbn.strip().replace("-", "").replace(" ", "")
    try:
        resp = requests.get(_BIBLIO.format(isbn=clean), timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None

    key     = f"ISBN:{clean}"
    details = data.get(key, {}).get("details", {})
    if not details:
        return None

    # author — can be a list of dicts or a plain string
    authors_raw = details.get("authors", [])
    if authors_raw and isinstance(authors_raw[0], dict):
        author = ", ".join(a.get("name", "") for a in authors_raw)
    else:
        author = str(authors_raw[0]) if authors_raw else "Unknown"

    # description — sometimes a dict, sometimes a string
    desc_raw = details.get("description", "")
    if isinstance(desc_raw, dict):
        description = desc_raw.get("value", "")
    else:
        description = str(desc_raw)

    subjects = details.get("subjects", [])[:5]     # first 5 subjects only

    # publisher — list of strings
    publishers = details.get("publishers", [])
    publisher  = ", ".join(publishers) if publishers else None

    # publish date / year
    publish_date = details.get("publish_date")
    publish_year = None
    if publish_date:
        m = re.search(r"\d{4}", publish_date)
        if m:
            publish_year = int(m.group())

    # language — languages is a list of {"key": "/languages/eng"}
    languages_raw = details.get("languages", [])
    language = None
    if languages_raw:
        code = languages_raw[0].get("key", "").rsplit("/", 1)[-1]
        language = _LANGUAGE_NAMES.get(code, code.upper() if code else None)

    return {
        "title":        details.get("title", ""),
        "author":       author,
        "publisher":    publisher,
        "publish_date": publish_date,
        "publish_year": publish_year,
        "num_pages":    details.get("number_of_pages"),
        "language":     language,
        "description":  description[:600] if description else "",
        "subjects":     subjects,
        "cover_url":    cover_url(clean),
        "ol_url":       f"https://openlibrary.org{details.get('key', '')}",
    }


# ── Search Open Library ────────────────────────────────────────────────
def search_open_library(query: str, limit: int = 10) -> list[dict]:
    """
    Search Open Library by title/author keyword.
    Returns a list of dicts with keys: isbn, title, author, cover_url
    Only results that have an ISBN-13 are returned.
    """
    if not query or not query.strip():
        return []
    try:
        resp = requests.get(
            _SEARCH,
            params={"q": query.strip(), "limit": limit, "fields": "title,author_name,isbn"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        docs = resp.json().get("docs", [])
    except Exception:
        return []

    results = []
    for doc in docs:
        isbns = doc.get("isbn", [])
        # prefer ISBN-13
        isbn13 = next((i for i in isbns if len(i) == 13), None)
        isbn   = isbn13 or (isbns[0] if isbns else None)
        if not isbn:
            continue
        authors = doc.get("author_name", [])
        results.append({
            "isbn":      isbn,
            "title":     doc.get("title", "Unknown"),
            "author":    ", ".join(authors[:2]) if authors else "Unknown",
            "cover_url": cover_url(isbn),
        })
    return results


# ── Enrich a list of Book model objects ───────────────────────────────
def enrich_books(book_list):
    """
    Attach .cover_url to each Book object.
    Used in templates: {{ book.cover_url }}
    Pure URL construction — no HTTP call.
    """
    for book in book_list:
        book.cover_url = cover_url(book.isbn)
    return book_list
