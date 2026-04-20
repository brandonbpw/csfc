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
from bs4 import BeautifulSoup

# ── Config ────────────────────────────────────────────────────────────────────

CSV_FILE     = Path("csfc_master_references.csv")
OUTPUT_DIR   = Path("csfc_references")
MANIFEST     = OUTPUT_DIR / "manifest.json"
DELAY_SECS   = 1.0
TIMEOUT_SECS = 60
MAX_RETRIES  = 3

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


def request_with_retry(session: requests.Session, url: str, *, stream: bool = False) -> requests.Response | None:
    delay = 2
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(url, headers=HEADERS, timeout=TIMEOUT_SECS, stream=stream, allow_redirects=True)
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

def resolve_pdf_url(session: requests.Session, url: str) -> str | None:
    """Turn a reference URL into a direct PDF URL."""
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path

    if path.lower().endswith(".pdf"):
        return url

    if host.endswith("rfc-editor.org") and "/rfc/" in path:
        rfc = path.rstrip("/").split("/")[-1]
        if not rfc.lower().endswith(".pdf"):
            return f"https://www.rfc-editor.org/rfc/{rfc}.pdf"
        return url

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


def download_row(session: requests.Session, row: dict) -> dict:
    ref_id     = row["ref_id"].strip()
    title      = row["title"].strip()
    category   = row["category"].strip() or "Uncategorized"
    access     = row["access"].strip().lower()
    source_url = row["download_url"].strip()

    entry = {
        "ref_id": ref_id,
        "title": title,
        "category": category,
        "access": access,
        "source_url": source_url,
        "status": "pending",
    }

    if access != "direct":
        entry["status"] = "skipped"
        entry["reason"] = f"access={access} (manual retrieval required)"
        print(f"  [SKIP] {ref_id} — {access}")
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

    pdf_url = resolve_pdf_url(session, source_url)
    if not pdf_url:
        entry["status"] = "failed"
        entry["reason"] = "could not resolve PDF URL"
        print(f"  [FAIL] {ref_id} — no PDF URL resolved from {source_url}")
        return entry

    entry["pdf_url"] = pdf_url
    r = request_with_retry(session, pdf_url, stream=True)
    if not r:
        entry["status"] = "failed"
        entry["reason"] = "download request failed"
        return entry

    content_type = r.headers.get("Content-Type", "")
    if "pdf" not in content_type.lower() and not pdf_url.lower().endswith(".pdf"):
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


def main() -> int:
    if not CSV_FILE.exists():
        print(f"ERROR: {CSV_FILE} not found in cwd ({Path.cwd()})", file=sys.stderr)
        return 1

    rows = load_rows()
    manifest = load_manifest()
    OUTPUT_DIR.mkdir(exist_ok=True)

    print(f"Loaded {len(rows)} references from {CSV_FILE}")
    counts = {"downloaded": 0, "cached": 0, "skipped": 0, "failed": 0}

    with requests.Session() as session:
        for i, row in enumerate(rows, 1):
            ref_id = row["ref_id"].strip()
            print(f"\n[{i}/{len(rows)}] {ref_id} — {row['title'].strip()}")
            entry = download_row(session, row)
            manifest[ref_id] = entry
            counts[entry["status"]] = counts.get(entry["status"], 0) + 1
            save_manifest(manifest)
            if entry["status"] == "downloaded":
                time.sleep(DELAY_SECS)

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
