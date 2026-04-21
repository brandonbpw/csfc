"""
CSFC Document Crawler
Crawls NSA's CSFC webpages and:
  - Expands all dropdowns/accordions then extracts clean Markdown per page
  - Downloads all linked PDFs
  - Organises everything into per-page subdirectories
  - Writes a catalog.json index

Usage:
    pip install -r requirements.txt
    python -m playwright install chromium
    python csfc_crawler.py

Output layout:
    csfc_docs/
      catalog.json
      Commercial-Solutions-for-Classified-Program/
        page_content.md
      Capability-Packages/
        page_content.md
        Mobile-Access-CP.pdf
        ...
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Page

# ── Config ────────────────────────────────────────────────────────────────────

SEED_URLS = [
    "https://www.nsa.gov/Resources/Commercial-Solutions-for-Classified-Program/",
    "https://www.nsa.gov/Resources/Commercial-Solutions-for-Classified-Program/Capability-Packages/",
    "https://www.nsa.gov/Resources/Commercial-Solutions-for-Classified-Program/CSFC-Components-List/",
    "https://www.nsa.gov/Resources/Commercial-Solutions-for-Classified-Program/Registration/",
]

ALLOWED_HOST     = "www.nsa.gov"
CSFC_PATH_PREFIX = "/Resources/Commercial-Solutions-for-Classified-Program"
OUTPUT_DIR       = Path("csfc_docs")
CATALOG_FILE     = OUTPUT_DIR / "catalog.json"
DELAY_SECONDS    = 1.5
MAX_PAGES        = 200

# ── Helpers ───────────────────────────────────────────────────────────────────

def is_pdf_url(url: str) -> bool:
    return urllib.parse.urlparse(url).path.lower().endswith(".pdf")

def is_csfc_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    return (
        parsed.scheme in ("http", "https")
        and parsed.netloc == ALLOWED_HOST
        and parsed.path.startswith(CSFC_PATH_PREFIX)
        and not is_pdf_url(url)
    )

def normalize_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse(parsed._replace(fragment=""))

def safe_name(s: str) -> str:
    s = re.sub(r'[<>:"/\\|?*]', "_", s)
    return s.strip("_. ") or "unnamed"

def page_subdir(url: str) -> Path:
    path = urllib.parse.urlparse(url).path.rstrip("/")
    segment = path.split("/")[-1] or "root"
    return OUTPUT_DIR / safe_name(segment)

def safe_filename(url: str) -> str:
    name = urllib.parse.urlparse(url).path.split("/")[-1]
    name = urllib.parse.unquote(name)
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    return name or "unnamed.pdf"

# ── Crawler ───────────────────────────────────────────────────────────────────

class CSFCCrawler:
    def __init__(self):
        self.crawl_queue: asyncio.Queue = asyncio.Queue()
        self.visited: set[str] = set()
        self.catalog: list[dict] = []
        OUTPUT_DIR.mkdir(exist_ok=True)

    # ── Page fetch ────────────────────────────────────────────────────────────

    async def fetch_page(self, page: Page, url: str) -> str | None:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            # Give JS a moment to render dynamic content
            await page.wait_for_timeout(3000)
            return await page.content()
        except Exception as e:
            print(f"  [WARN] Could not fetch {url}: {e}")
            return None

    # ── Expand dropdowns/accordions ───────────────────────────────────────────

    async def save_html(self, page: Page, url: str, dest_dir: Path) -> None:
        """Save the fully rendered HTML of the current page."""
        dest = dest_dir / "page.html"
        if dest.exists():
            return
        html = await page.content()
        dest.write_text(html, encoding="utf-8")
        print(f"  [PAGE ✓] page.html  ({len(html):,} chars)")

    # ── Link extraction ───────────────────────────────────────────────────────

    def extract_links(self, html: str, base_url: str) -> tuple[list[str], list[str]]:
        soup = BeautifulSoup(html, "html.parser")
        pdf_links, page_links = [], []
        for tag in soup.find_all("a", href=True):
            abs_url = normalize_url(urllib.parse.urljoin(base_url, tag["href"].strip()))
            if is_pdf_url(abs_url):
                pdf_links.append(abs_url)
            elif is_csfc_url(abs_url):
                page_links.append(abs_url)
        return pdf_links, page_links

    # ── PDF download ──────────────────────────────────────────────────────────

    async def download_pdf(
        self,
        pdf_url: str,
        source_page: str,
        dest_dir: Path,
    ) -> None:
        filename = safe_filename(pdf_url)
        dest = dest_dir / filename

        if dest.exists():
            size_kb = round(dest.stat().st_size / 1024, 1)
            self.catalog.append({
                "filename": filename,
                "url": pdf_url,
                "source_page": source_page,
                "local_path": str(dest),
                "size_kb": size_kb,
                "downloaded_at": None,
            })
            print(f"  [SKIP] {filename}")
            return

        try:
            resp = requests.get(
                pdf_url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    "Referer": source_page,
                },
                timeout=60,
                stream=True,
            )
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            size_kb = round(dest.stat().st_size / 1024, 1)
            self.catalog.append({
                "filename": filename,
                "url": pdf_url,
                "source_page": source_page,
                "local_path": str(dest),
                "size_kb": size_kb,
                "downloaded_at": datetime.now(timezone.utc).isoformat(),
            })
            print(f"  [PDF ✓] {filename}  ({size_kb} KB)")
        except Exception as e:
            print(f"  [WARN] Failed to download {filename}: {e}")

    # ── Catalog ───────────────────────────────────────────────────────────────

    def save_catalog(self):
        CATALOG_FILE.write_text(json.dumps(self.catalog, indent=2))

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(self):
        for url in SEED_URLS:
            norm = normalize_url(url)
            if norm not in self.visited:
                self.visited.add(norm)
                await self.crawl_queue.put(url)

        pages_crawled = 0

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                accept_downloads=True,
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            )
            crawl_page = await context.new_page()

            while not self.crawl_queue.empty() and pages_crawled < MAX_PAGES:
                url = await self.crawl_queue.get()
                pages_crawled += 1
                print(f"\n[{pages_crawled}/{MAX_PAGES}] {url}")

                subdir = page_subdir(url)
                subdir.mkdir(parents=True, exist_ok=True)

                html = await self.fetch_page(crawl_page, url)
                if not html:
                    continue

                # Extract page content as Markdown (expands dropdowns first)
                await self.save_html(crawl_page, url, subdir)

                # Re-parse the original HTML for links (before dropdowns mutated the DOM)
                pdf_urls, page_urls = self.extract_links(html, url)

                for page_url in page_urls:
                    norm = normalize_url(page_url)
                    if norm not in self.visited:
                        self.visited.add(norm)
                        await self.crawl_queue.put(page_url)
                        print(f"  [ENQUEUE] {page_url}")

                for pdf_url in pdf_urls:
                    norm = normalize_url(pdf_url)
                    if norm not in self.visited:
                        self.visited.add(norm)
                        await self.download_pdf(pdf_url, source_page=url, dest_dir=subdir)

                self.save_catalog()
                await asyncio.sleep(DELAY_SECONDS)

            await browser.close()

        print(f"\n{'─'*60}")
        print(f"  Done.")
        print(f"  Pages crawled   : {pages_crawled}")
        print(f"  PDFs downloaded : {len(self.catalog)}")
        print(f"  Output          : ./{OUTPUT_DIR}/")
        print(f"  Catalog         : {CATALOG_FILE}")
        print(f"{'─'*60}")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    asyncio.run(CSFCCrawler().run())
