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

import asyncio
import json
import re
import urllib.parse
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, BrowserContext, Page

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
        parsed.netloc == ALLOWED_HOST
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
            await page.goto(url, wait_until="networkidle", timeout=30_000)
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
        context: BrowserContext,
        pdf_url: str,
        source_page: str,
        dest_dir: Path,
    ) -> None:
        filename = safe_filename(pdf_url)
        dest = dest_dir / filename

        if dest.exists():
            print(f"  [SKIP] {filename}")
            return

        dl_page = await context.new_page()
        loop = asyncio.get_event_loop()
        download_future: asyncio.Future = loop.create_future()

        def on_download(download):
            if not download_future.done():
                download_future.set_result(download)

        dl_page.once("download", on_download)

        try:
            await dl_page.goto(pdf_url, referer=source_page, timeout=60_000)
        except Exception as e:
            if "Download is starting" not in str(e):
                print(f"  [WARN] Navigation error for {pdf_url}: {e}")
                await dl_page.close()
                return

        try:
            download = await asyncio.wait_for(download_future, timeout=60)
        except asyncio.TimeoutError:
            print(f"  [WARN] Timed out waiting for download: {pdf_url}")
            await dl_page.close()
            return

        try:
            await download.save_as(dest)
            size_kb = round(dest.stat().st_size / 1024, 1)
            self.catalog.append({
                "filename": filename,
                "url": pdf_url,
                "source_page": source_page,
                "local_path": str(dest),
                "size_kb": size_kb,
                "downloaded_at": datetime.utcnow().isoformat() + "Z",
            })
            print(f"  [PDF ✓] {filename}  ({size_kb} KB)")
        except Exception as e:
            print(f"  [WARN] Failed to save {filename}: {e}")
        finally:
            await dl_page.close()

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
                        await self.download_pdf(context, pdf_url, source_page=url, dest_dir=subdir)

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
