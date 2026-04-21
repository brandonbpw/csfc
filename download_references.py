"""
CSFC Reference Document Downloader

Reads csfc_master_references.csv and downloads every reference whose `access`
column is `direct`. Non-direct entries (`category`, `paywall`, `landing`) are
recorded in the manifest with a status of `skipped` so they can be handled
manually.

Handled source types:
  - csrc.nist.gov/pubs/...      → landing page is scraped for the PDF link
  - rfc-editor.org/rfc/rfcNNNN  → downloaded as ...NNNN.pdf
  - direct *.pdf URLs           → downloaded as-is

Output layout:
    csfc_references/
      manifest.json
      CNSS_Policy/            (skipped — documented in manifest)
      FIPS/
        FIPS-140-3.pdf
        ...
      NIST_SP/
        SP-800-53.pdf
        ...
      IETF_RFC/
        RFC-5280.pdf
        ...
      DoD_Instruction/
        DoDI-8420.01.pdf
        ...

Usage:
    pip install -r requirements.txt
    python download_references.py
"""

from __future__ import annotations

import csv
import json
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import requests
import urllib3
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, Browser

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Config ────────────────────────────────────────────────────────────────────

CSV_FILE     = Path("csfc_master_references.csv")
OUTPUT_DIR   = Path("csfc_references")
MANIFEST     = OUTPUT_DIR / "manifest.json"
DELAY_SECS   = 1.0
TIMEOUT_SECS = 60
MAX_RETRIES  = 5

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

HEADERS = {"User-Agent": USER_AGENT}

# ── Helpers ───────────────────────────────────────────────────────────────────

def safe_name(s: str) -> str:
    s = re.sub(r'[<>:"/\\|?*]', "_", s)
    return s.strip("_. ") or "unnamed"


def request_with_retry(session: requests.Session, url: str, *, stream: bool = False, verify: bool = True) -> requests.Response | None:
    delay = 2
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(url, headers=HEADERS, timeout=TIMEOUT_SECS, stream=stream, allow_redirects=True, verify=verify)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            if attempt == MAX_RETRIES:
                print(f"  [ERROR] {url}: {e}")
                return None
            print(f"  [RETRY {attempt}/{MAX_RETRIES}] {url}: {e}")
            time.sleep(delay)
            delay *= 2
    return None


def save_stream(r: requests.Response, dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    with dest.open("wb") as f:
        for chunk in r.iter_content(chunk_size=64 * 1024):
            if chunk:
                f.write(chunk)
                size += len(chunk)
    return size


# ── Per-source resolvers ──────────────────────────────────────────────────────

def resolve_pdf_url(session: requests.Session, url: str, ref_id: str = "", browser: Browser | None = None) -> str | None:
    """Turn a reference URL into a direct PDF URL."""
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path

    if path.lower().endswith(".pdf"):
        return url

    # cnss.gov openDoc.cfm links are direct document downloads
    if host.endswith("cnss.gov") and "opendoc.cfm" in path.lower():
        return url

    if host.endswith("rfc-editor.org") and "/rfc/" in path:
        # rfc-editor.org no longer serves .pdf files; we'll print the HTML page
        rfc = path.rstrip("/").split("/")[-1]
        if rfc.lower().endswith(".pdf"):
            rfc = rfc[:-4]
        return f"rfc-print:https://www.rfc-editor.org/rfc/{rfc}"

    if host.endswith("cnss.gov") and browser and ref_id:
        return resolve_cnss_pdf_url(browser, url, ref_id)

    if host.endswith("niap-ccevs.org") and browser and ref_id:
        return resolve_niap_pdf(browser, url, ref_id)

    if host.endswith("csrc.nist.gov") and path.startswith("/pubs/"):
        return scrape_csrc_pdf_url(session, url)

    return None


def scrape_csrc_pdf_url(session: requests.Session, landing_url: str) -> str | None:
    """NIST CSRC publication pages link to nvlpubs.nist.gov PDFs. Find that link."""
    r = request_with_retry(session, landing_url)
    if not r:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    candidates: list[str] = []
    for a in soup.find_all("a", href=True):
        href = urllib.parse.urljoin(landing_url, a["href"].strip())
        if href.lower().endswith(".pdf") and "nvlpubs.nist.gov" in href.lower():
            candidates.append(href)
    # Prefer the "Local Download" / primary PDF which appears first in the list.
    return candidates[0] if candidates else None


def print_rfc_to_pdf(browser: Browser, url: str, dest: Path) -> int:
    """Load an RFC HTML page in Playwright and print it to PDF."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    page = browser.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(2000)
        page.pdf(path=str(dest), format="Letter", print_background=True)
        return dest.stat().st_size
    finally:
        page.close()


# ── CNSS listing-page scraper ─────────────────────────────────────────────────

_cnss_page_cache: dict[str, list[dict]] = {}


def _load_cnss_links(browser: Browser, listing_url: str) -> list[dict]:
    """Load a cnss.gov listing page and return all PDF links with their text."""
    if listing_url in _cnss_page_cache:
        return _cnss_page_cache[listing_url]

    ctx = browser.new_context(ignore_https_errors=True)
    page = ctx.new_page()
    try:
        page.goto(listing_url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(3000)
        links = page.eval_on_selector_all(
            "a[href]",
            """els => els.map(e => ({text: e.textContent.trim(), href: e.href}))
                       .filter(l => l.href.toLowerCase().endsWith('.pdf'))""",
        )
    except Exception as e:
        print(f"  [WARN] Could not load CNSS listing {listing_url}: {e}")
        links = []
    finally:
        ctx.close()

    _cnss_page_cache[listing_url] = links
    if links:
        print(f"  [CNSS] Found {len(links)} PDF link(s) on {listing_url}")
    else:
        print(f"  [WARN] No PDF links found on {listing_url} (site may be unavailable)")
    return links


def resolve_cnss_pdf_url(browser: Browser, listing_url: str, ref_id: str) -> str | None:
    """Find the PDF link for a specific CNSS document on its listing page."""
    links = _load_cnss_links(browser, listing_url)
    # Extract the numeric part: CNSSP-15 → "15", CNSSD-505 → "505"
    num = ref_id.split("-", 1)[-1]
    # Match against link text or href containing the document number
    for link in links:
        text = link["text"]
        href = link["href"]
        # Look for the number in the link text (e.g. "CNSSP No. 15" or "CNSSP-15")
        if re.search(rf'\b0*{re.escape(num)}\b', text):
            return href
    # Fallback: check the filename in the URL
    for link in links:
        fname = urllib.parse.unquote(link["href"].split("/")[-1]).lower()
        if num in fname:
            return link["href"]
    return None


# ── NIAP Protection Profile resolver ─────────────────────────────────────────

_niap_listing_cache: list[dict] | None = None

# Direct overrides for profiles not found in the listing grid
_NIAP_DIRECT_URLS: dict[str, str] = {
    "PP-IPsec-VPN-Client": "https://www.niap-ccevs.org/protectionprofiles/419",
    "PP-Application-Software-v1.2": "https://www.niap-ccevs.org/protectionprofiles/394",
}

# Map CSV ref_id keywords → NIAP short-name prefixes for matching
_NIAP_REF_MAP: dict[str, list[str]] = {
    "PP-Application-Software": ["PP_APP"],
    "PP-CA":                   ["PP_CA"],
    "PP-IPsec-VPN-Client":     ["MOD_VPN_CLI", "MOD_VPNC", "PP_VPNC"],
    "PP-MDF":                  ["PP_MDF"],
    "PP-Module-FE-EM":         ["MOD_FEEM"],
    "PP-Module-FE":            ["MOD_FE"],
    "PP-OS":                   ["PP_OS"],
}


def _load_niap_listing(browser: Browser) -> list[dict]:
    """Load the NIAP protection profiles listing and return rows."""
    global _niap_listing_cache
    if _niap_listing_cache is not None:
        return _niap_listing_cache

    page = browser.new_page()
    try:
        page.goto("https://www.niap-ccevs.org/protectionprofiles",
                   wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(5000)
        rows = page.eval_on_selector_all(
            '[role="row"][aria-rowindex]',
            """els => els.map(e => {
                let cells = e.querySelectorAll('[role="gridcell"]');
                let link = e.querySelector('a[href*="/protectionprofiles/"]');
                return {
                    shortName: cells.length > 0 ? cells[0].textContent.trim() : '',
                    title: cells.length > 1 ? cells[1].textContent.trim() : '',
                    href: link ? link.href : null
                };
            }).filter(r => r.href)""",
        )
    except Exception as e:
        print(f"  [WARN] Could not load NIAP listing: {e}")
        rows = []
    finally:
        page.close()

    _niap_listing_cache = rows
    print(f"  [NIAP] Loaded {len(rows)} profiles from listing")
    return rows


def _find_niap_detail_url(browser: Browser, ref_id: str) -> str | None:
    """Find the NIAP detail page URL for a given ref_id."""
    rows = _load_niap_listing(browser)
    prefixes = _NIAP_REF_MAP.get(ref_id, [])
    if not prefixes:
        # Fallback: convert ref_id to a prefix pattern (PP-OS → PP_OS)
        prefixes = [ref_id.replace("-", "_").upper()]

    for row in rows:
        sn = row["shortName"].upper()
        for prefix in prefixes:
            if sn.startswith(prefix.upper()):
                return row["href"]
    return None


def resolve_niap_pdf(browser: Browser, url: str, ref_id: str) -> str | None:
    """Resolve a NIAP ref_id to a niap-download: sentinel with the detail page URL."""
    # Check direct overrides first
    if ref_id in _NIAP_DIRECT_URLS:
        return f"niap-download:{_NIAP_DIRECT_URLS[ref_id]}"
    detail_url = _find_niap_detail_url(browser, ref_id)
    if detail_url:
        return f"niap-download:{detail_url}"
    return None


def download_niap_pdf(browser: Browser, detail_url: str, dest: Path) -> int:
    """Navigate to a NIAP detail page, click the first PDF button, and save the download."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    ctx = browser.new_context(accept_downloads=True)
    page = ctx.new_page()
    try:
        page.goto(detail_url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(3000)
        btn = page.locator("button.btn-link").first
        with page.expect_download(timeout=30_000) as dl_info:
            btn.click()
        download = dl_info.value
        download.save_as(str(dest))
        return dest.stat().st_size
    finally:
        ctx.close()


# ── Main ──────────────────────────────────────────────────────────────────────

def load_rows() -> list[dict]:
    with CSV_FILE.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_manifest() -> dict[str, dict]:
    if MANIFEST.exists():
        try:
            return {e["ref_id"]: e for e in json.loads(MANIFEST.read_text())}
        except (json.JSONDecodeError, KeyError):
            pass
    return {}


def save_manifest(entries: dict[str, dict]) -> None:
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(entries.values(), key=lambda e: (e.get("category", ""), e.get("ref_id", "")))
    MANIFEST.write_text(json.dumps(ordered, indent=2))


# Ref IDs to skip entirely (no downloadable source available)
_SKIP_REF_IDS = {
    "CNSSI-4003", "CNSSI-4004", "CNSSI-4005",
    "IEEE-802.1AE-2018", "IEEE-802.1X-2020", "ISO-9594-8",
}


def download_pdf_via_browser(browser: Browser, url: str, dest: Path) -> int:
    """Fallback: download a PDF using Playwright when requests gets 403."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    ctx = browser.new_context(accept_downloads=True, ignore_https_errors=True)
    page = ctx.new_page()
    try:
        with page.expect_download(timeout=60_000) as dl_info:
            try:
                page.goto(url, timeout=60_000)
            except Exception as nav_err:
                if "Download is starting" not in str(nav_err):
                    raise
        download = dl_info.value
        download.save_as(str(dest))
        # Validate it's actually a PDF
        with open(dest, "rb") as f:
            header = f.read(5)
        if header != b"%PDF-":
            dest.unlink(missing_ok=True)
            raise ValueError("downloaded file is not a valid PDF")
        return dest.stat().st_size
    finally:
        ctx.close()


def _infer_category(ref_id: str) -> str:
    """Infer a folder category from the ref_id prefix."""
    prefixes = {
        "CNSSI": "CNSS_Policy", "CNSSP": "CNSS_Policy", "CNSSD": "CNSS_Directive",
        "FIPS": "FIPS", "SP-": "NIST_SP", "IR-": "NIST_IR",
        "RFC-": "IETF_RFC", "IEEE": "IEEE", "ISO": "ISO",
        "TCG": "TCG", "UEFI": "UEFI", "DoDI": "DoD_Instruction",
        "PP-": "Protection_Profile", "FDE-": "Protection_Profile",
        "NSA": "NSA_Guidance",
    }
    for prefix, cat in prefixes.items():
        if ref_id.startswith(prefix):
            return cat
    return "Uncategorized"


def download_row(session: requests.Session, row: dict, browser: Browser) -> dict:
    ref_id     = row["ref_id"].strip()
    title      = row["title"].strip()
    category   = _infer_category(ref_id)
    source_url = row["download_url"].strip()

    entry = {
        "ref_id": ref_id,
        "title": title,
        "category": category,
        "source_url": source_url,
        "status": "pending",
    }

    if ref_id in _SKIP_REF_IDS:
        entry["status"] = "skipped"
        entry["reason"] = "no downloadable source available"
        print(f"  [SKIP] {ref_id}")
        return entry

    dest_dir = OUTPUT_DIR / safe_name(category)
    dest     = dest_dir / f"{safe_name(ref_id)}.pdf"

    if dest.exists() and dest.stat().st_size > 0:
        entry.update(
            status="cached",
            local_path=str(dest),
            size_kb=round(dest.stat().st_size / 1024, 1),
        )
        print(f"  [CACHED] {ref_id}")
        return entry

    pdf_url = resolve_pdf_url(session, source_url, ref_id=ref_id, browser=browser)
    if not pdf_url:
        entry["status"] = "failed"
        entry["reason"] = "could not resolve PDF URL"
        print(f"  [FAIL] {ref_id} — no PDF URL resolved from {source_url}")
        return entry

    entry["pdf_url"] = pdf_url

    # RFC pages: print HTML to PDF via Playwright
    if pdf_url.startswith("rfc-print:"):
        rfc_html_url = pdf_url[len("rfc-print:"):]
        entry["pdf_url"] = rfc_html_url
        try:
            size = print_rfc_to_pdf(browser, rfc_html_url, dest)
            entry.update(
                status="downloaded",
                local_path=str(dest),
                size_kb=round(size / 1024, 1),
                downloaded_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            )
            print(f"  [OK] {ref_id} → {dest} ({entry['size_kb']} KB) [printed]")
        except Exception as e:
            entry["status"] = "failed"
            entry["reason"] = f"print-to-pdf failed: {e}"
            print(f"  [FAIL] {ref_id} — print-to-pdf: {e}")
        return entry

    # NIAP profiles: download PDF from detail page via Playwright
    if pdf_url.startswith("niap-download:"):
        detail_url = pdf_url[len("niap-download:"):]
        entry["pdf_url"] = detail_url
        try:
            size = download_niap_pdf(browser, detail_url, dest)
            entry.update(
                status="downloaded",
                local_path=str(dest),
                size_kb=round(size / 1024, 1),
                downloaded_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            )
            print(f"  [OK] {ref_id} → {dest} ({entry['size_kb']} KB) [niap]")
        except Exception as e:
            entry["status"] = "failed"
            entry["reason"] = f"niap download failed: {e}"
            print(f"  [FAIL] {ref_id} — niap download: {e}")
        return entry

    skip_ssl = "cnss.gov" in pdf_url.lower()
    r = request_with_retry(session, pdf_url, stream=True, verify=not skip_ssl)

    if r:
        content_type = r.headers.get("Content-Type", "")
        if "pdf" not in content_type.lower() and not pdf_url.lower().endswith(".pdf") and "cnss.gov" not in pdf_url.lower():
            entry["status"] = "failed"
            entry["reason"] = f"unexpected Content-Type: {content_type}"
            print(f"  [FAIL] {ref_id} — content-type {content_type}")
            return entry

        size = save_stream(r, dest)
        entry.update(
            status="downloaded",
            local_path=str(dest),
            size_kb=round(size / 1024, 1),
            downloaded_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )
        print(f"  [OK] {ref_id} → {dest} ({entry['size_kb']} KB)")
        return entry

    # Fallback: use Playwright browser to download (bypasses 403 bot blocks)
    print(f"  [RETRY] {ref_id} — trying browser download...")
    try:
        size = download_pdf_via_browser(browser, pdf_url, dest)
        entry.update(
            status="downloaded",
            local_path=str(dest),
            size_kb=round(size / 1024, 1),
            downloaded_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )
        print(f"  [OK] {ref_id} → {dest} ({entry['size_kb']} KB) [browser]")
    except Exception as e:
        entry["status"] = "failed"
        entry["reason"] = f"download failed (requests + browser): {e}"
        print(f"  [FAIL] {ref_id} — browser fallback: {e}")
    return entry


def main() -> int:
    if not CSV_FILE.exists():
        print(f"ERROR: {CSV_FILE} not found in cwd ({Path.cwd()})", file=sys.stderr)
        return 1

    rows = load_rows()
    manifest = load_manifest()
    OUTPUT_DIR.mkdir(exist_ok=True)

    print(f"Loaded {len(rows)} references from {CSV_FILE}")
    counts = {"downloaded": 0, "cached": 0, "skipped": 0, "failed": 0}

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)

    try:
        with requests.Session() as session:
            for i, row in enumerate(rows, 1):
                ref_id = row["ref_id"].strip()
                print(f"\n[{i}/{len(rows)}] {ref_id} — {row['title'].strip()}")
                entry = download_row(session, row, browser)
                manifest[ref_id] = entry
                counts[entry["status"]] = counts.get(entry["status"], 0) + 1
                save_manifest(manifest)
                if entry["status"] == "downloaded":
                    time.sleep(DELAY_SECS)
    finally:
        browser.close()
        pw.stop()

    print("\n" + "─" * 60)
    print(f"  Total     : {len(rows)}")
    print(f"  Downloaded: {counts.get('downloaded', 0)}")
    print(f"  Cached    : {counts.get('cached', 0)}")
    print(f"  Skipped   : {counts.get('skipped', 0)}")
    print(f"  Failed    : {counts.get('failed', 0)}")
    print(f"  Output    : ./{OUTPUT_DIR}/")
    print(f"  Manifest  : {MANIFEST}")
    print("─" * 60)
    return 0 if counts.get("failed", 0) == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
