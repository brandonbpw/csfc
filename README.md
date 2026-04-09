# CSFC Document Crawler

A web crawler that scrapes NSA's [Commercial Solutions for Classified (CSfC) Program](https://www.nsa.gov/Resources/Commercial-Solutions-for-Classified-Program/) webpages and builds a local repository of all documentation — page HTML and linked PDFs — organized for use in RAG pipelines or manual review.

---

## What It Does

- Crawls all pages under the NSA CSfC section, starting from a set of seed URLs
- Saves the fully JS-rendered HTML of each page (`page.html`)
- Downloads every linked PDF found on each page
- Organizes output into one subdirectory per crawled page
- Writes a `catalog.json` index of all downloaded PDFs

---

## Output Structure

```
csfc_docs/
  catalog.json
  Commercial-Solutions-for-Classified-Program/
    page.html
  Capability-Packages/
    page.html
    Mobile-Access-Capability-Package.pdf
    Campus-WLAN-Capability-Package.pdf
    ...
  Archived-Capability-Packages/
    page.html
    ...
  CSFC-Components-List/
    page.html
    ...
```

### catalog.json

Each entry records metadata about a downloaded PDF:

```json
[
  {
    "filename": "Mobile-Access-Capability-Package.pdf",
    "url": "https://www.nsa.gov/Portals/75/...",
    "source_page": "https://www.nsa.gov/Resources/.../Capability-Packages/",
    "local_path": "csfc_docs/Capability-Packages/Mobile-Access-Capability-Package.pdf",
    "size_kb": 412.3,
    "downloaded_at": "2026-04-08T14:22:01Z"
  }
]
```

---

## Setup

**Requirements:** Python 3.10+, Windows/macOS/Linux

### 1. Create a virtual environment

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# macOS / Linux
source venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

### 3. Run

```bash
python csfc_crawler.py
```

The crawler will print progress as it runs. Output is written to `csfc_docs/` in the same directory. The catalog is saved after every page, so progress is preserved if the run is interrupted.

---

## Configuration

All tunable settings are at the top of `csfc_crawler.py`:

| Variable | Default | Description |
|---|---|---|
| `SEED_URLS` | 4 CSfC pages | Starting points for the crawl |
| `ALLOWED_HOST` | `www.nsa.gov` | Only links on this host are crawled |
| `CSFC_PATH_PREFIX` | `/Resources/Commercial-Solutions-for-Classified-Program` | Only paths under this prefix are crawled |
| `OUTPUT_DIR` | `csfc_docs/` | Root output directory |
| `DELAY_SECONDS` | `1.5` | Pause between page fetches (be a polite crawler) |
| `MAX_PAGES` | `200` | Safety ceiling on total pages crawled |

To crawl additional CSfC pages not reachable from the defaults, add their URLs to `SEED_URLS`.

---

## Notes

- **PDF downloads** use a full browser session (same cookies and headers as the crawl) to bypass the 403s that direct HTTP requests receive from the NSA server.
- **Page HTML** is captured after JavaScript has fully rendered, so content inside dynamically loaded components is included.
- The `catalog.json` is updated incrementally — if the crawler is stopped and restarted, already-downloaded files are skipped automatically.
- Links to external domains (e.g. `media.defense.gov`) are not crawled but their PDFs are still attempted for download.

---

## For RAG Use

Each `page.html` contains the fully rendered page DOM. To prepare it for ingestion:

1. Parse with BeautifulSoup and extract text: `soup.get_text(separator="\n", strip=True)`
2. Chunk by heading tags (`h1`–`h4`) to preserve document structure
3. PDFs can be parsed with a library like `pypdf` or `pdfplumber` and chunked similarly

The `catalog.json` is useful for building metadata-aware retrievers (filter by source page, date, etc.).
