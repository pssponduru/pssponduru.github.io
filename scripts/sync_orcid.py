#!/usr/bin/env python3
"""Add newly public ORCID works to this GitHub Pages academic website.

The workflow intentionally never downloads a publisher PDF by default.  It can
only download a direct .pdf URL after the repository owner explicitly enables
that option and allow-lists the hosting domain.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
from datetime import date
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
PUBLICATIONS = ROOT / "publications"
PDFS = ROOT / "pdfs"
STATE_FILE = ROOT / "data" / "orcid-sync.json"
INDEX_FILE = ROOT / "index.html"
SITEMAP_FILE = ROOT / "sitemap.xml"

ORCID_ID = os.environ.get("ORCID_ID", "0009-0009-5930-8265")
AUTHOR_NAME = os.environ.get("AUTHOR_NAME", "Prudvi Saisaran Ponduru")
SITE_URL = os.environ.get("SITE_URL", "https://pssponduru.github.io").rstrip("/")
ALLOW_PDF = os.environ.get("ALLOW_ORCID_PDF_DOWNLOAD", "false").lower() == "true"
TRUSTED_PDF_HOSTS = {
    item.strip().lower()
    for item in os.environ.get("TRUSTED_PDF_HOSTS", "").split(",")
    if item.strip()
}


def fetch_json(url: str, headers: dict[str, str] | None = None, data: bytes | None = None) -> dict:
    request = Request(url, data=data, headers=headers or {})
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def access_token() -> str:
    client_id = os.environ.get("ORCID_CLIENT_ID")
    client_secret = os.environ.get("ORCID_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError("Missing ORCID_CLIENT_ID or ORCID_CLIENT_SECRET GitHub secret.")
    payload = (
        "client_id=" + client_id + "&client_secret=" + client_secret
        + "&grant_type=client_credentials&scope=/read-public"
    ).encode("utf-8")
    result = fetch_json(
        "https://orcid.org/oauth/token",
        {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        payload,
    )
    return result["access_token"]


def value(node: object) -> str:
    if isinstance(node, dict):
        raw = node.get("value", "")
        return str(raw).strip() if raw else ""
    return ""


def doi_from(work: dict) -> str:
    ids = work.get("external-ids", {}).get("external-id", []) or []
    for item in ids:
        if str(item.get("external-id-type", "")).lower() == "doi":
            return value(item.get("external-id-value", {})).removeprefix("https://doi.org/").strip()
    return ""


def year_from(work: dict) -> str:
    return value(work.get("publication-date", {}).get("year", {})) or str(date.today().year)


def people_from(work: dict) -> str:
    names = []
    for item in work.get("contributors", {}).get("contributor", []) or []:
        name = value(item.get("credit-name", {}))
        if name and name not in names:
            names.append(name)
    return ", ".join(names) or AUTHOR_NAME


def existing_identifiers() -> set[str]:
    found: set[str] = set()
    for page in PUBLICATIONS.glob("*.html"):
        source = page.read_text(encoding="utf-8", errors="ignore")
        for match in re.findall(r'<meta name="citation_(?:doi|orcid_put_code)" content="([^"]+)"', source):
            found.add(html.unescape(match).strip().lower())
    return found


def safe_pdf_download(source_url: str, page_stem: str) -> str:
    if not ALLOW_PDF or not source_url.lower().split("?")[0].endswith(".pdf"):
        return ""
    host = (urlparse(source_url).hostname or "").lower()
    if not host or host not in TRUSTED_PDF_HOSTS:
        print(f"PDF skipped (host is not allow-listed): {source_url}")
        return ""
    try:
        request = Request(source_url, headers={"User-Agent": "ORCID-site-sync/1.0"})
        with urlopen(request, timeout=45) as response:
            content_type = response.headers.get("Content-Type", "").lower()
            content = response.read(5_000_001)
        if len(content) > 5_000_000 or not content.startswith(b"%PDF") or "pdf" not in content_type:
            print(f"PDF skipped (not a verified PDF under 5 MB): {source_url}")
            return ""
        filename = f"{page_stem}.pdf"
        (PDFS / filename).write_bytes(content)
        return f"/pdfs/{filename}"
    except HTTPError as error:
        print(f"PDF skipped (HTTP {error.code}): {source_url}")
    except Exception as error:  # Network failures should not stop metadata publishing.
        print(f"PDF skipped ({error}): {source_url}")
    return ""


def publication_html(title: str, authors: str, venue: str, year: str, doi: str, put_code: str,
                     source_url: str, pdf_path: str) -> str:
    title_e, authors_e, venue_e = map(html.escape, (title, authors, venue))
    doi_e, put_e = map(html.escape, (doi, put_code))
    source_e = html.escape(source_url, quote=True)
    doi_line = f'<a href="https://doi.org/{doi_e}">https://doi.org/{doi_e}</a>' if doi else "Not supplied in ORCID"
    pdf_line = f'<p><a href="{pdf_path}">Download full-text PDF</a></p>' if pdf_path else ""
    citation_doi = f'  <meta name="citation_doi" content="{doi_e}">\n' if doi else ""
    citation_pdf = f'  <meta name="citation_pdf_url" content="{SITE_URL}{pdf_path}">\n' if pdf_path else ""
    return f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="{title_e} — {authors_e}">
  <meta name="citation_title" content="{title_e}">
  <meta name="citation_author" content="{authors_e}">
  <meta name="citation_publication_date" content="{year}-01-01">
  <meta name="citation_journal_title" content="{venue_e}">
{citation_doi}  <meta name="citation_orcid_put_code" content="{put_e}">
{citation_pdf}  <title>{title_e} | {AUTHOR_NAME}</title>
  <style>
    body {{ margin:0; font:18px/1.7 system-ui,sans-serif; color:#14283d; background:#f7f4ed; }}
    main {{ max-width:850px; margin:0 auto; padding:64px 24px; }}
    a {{ color:#0b5ea8; }} .eyebrow {{ color:#0b5ea8; font-weight:700; letter-spacing:.08em; text-transform:uppercase; }}
    h1 {{ font:700 clamp(2rem,5vw,3.7rem)/1.05 Georgia,serif; margin:.3rem 0 1rem; }}
    .card {{ background:#fff; border:1px solid #dde4eb; border-radius:16px; padding:24px; margin-top:28px; }}
  </style>
</head>
<body><main>
  <p class="eyebrow">ORCID-synchronized publication</p>
  <h1>{title_e}</h1>
  <p><strong>{authors_e}</strong></p>
  <p><em>{venue_e}</em>, {year}</p>
  <section class="card">
    <p><strong>DOI:</strong> {doi_line}</p>
    <p><strong>ORCID work record:</strong> <a href="{source_e}">View source record</a></p>
    {pdf_line}
  </section>
  <p><a href="/">← Back to academic profile</a></p>
</main></body></html>
'''


def update_sitemap(page_name: str) -> None:
    sitemap = SITEMAP_FILE.read_text(encoding="utf-8")
    loc = f"{SITE_URL}/publications/{page_name}"
    if f"<loc>{loc}</loc>" in sitemap:
        return
    entry = f"  <url>\n    <loc>{loc}</loc>\n    <lastmod>{date.today().isoformat()}</lastmod>\n  </url>\n"
    SITEMAP_FILE.write_text(sitemap.replace("</urlset>", entry + "</urlset>"), encoding="utf-8")


def update_home(entries: list[dict]) -> None:
    if not entries:
        return
    source = INDEX_FILE.read_text(encoding="utf-8")
    cards = []
    for item in entries:
        doi = item["doi"]
        doi_html = (f' <a class="doi" href="https://doi.org/{html.escape(doi)}">DOI: {html.escape(doi)}</a>' if doi else "")
        cards.append(
            f'        <li><a class="work-title" href="/publications/{item["page"]}">{html.escape(item["title"])}</a>'
            f'<p>{html.escape(item["authors"])}. <em>{html.escape(item["venue"])}</em>, {item["year"]}.{doi_html}</p></li>'
        )
    block = "<!-- ORCID_SYNC_START -->\n      <div class=\"orcid-sync\"><h3>Newly synchronized works</h3><ol class=\"works\">\n" + "\n".join(cards) + "\n      </ol></div>\n      <!-- ORCID_SYNC_END -->"
    pattern = r"<!-- ORCID_SYNC_START -->.*?<!-- ORCID_SYNC_END -->"
    if not re.search(pattern, source, flags=re.S):
        raise RuntimeError("The ORCID sync markers are missing from index.html.")
    INDEX_FILE.write_text(re.sub(pattern, block, source, flags=re.S), encoding="utf-8")


def main() -> None:
    token = access_token()
    headers = {"Accept": "application/vnd.orcid+json", "Authorization": f"Bearer {token}"}
    listing = fetch_json(f"https://pub.orcid.org/v3.0/{ORCID_ID}/works", headers)
    known = existing_identifiers()
    saved = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {"works": []}
    saved_ids = {str(x.get("key", "")).lower() for x in saved.get("works", [])}
    new_entries = []

    for group in listing.get("group", []) or []:
        for summary in group.get("work-summary", []) or []:
            put_code = str(summary.get("put-code", ""))
            key = doi_from(summary).lower() or put_code.lower()
            if not key or key in known or key in saved_ids:
                continue
            work = fetch_json(f"https://pub.orcid.org/v3.0/{ORCID_ID}/work/{put_code}", headers)
            title = value(work.get("title", {}).get("title", {}))
            if not title:
                print(f"Skipping ORCID put-code {put_code}: no title.")
                continue
            doi = doi_from(work)
            year = year_from(work)
            venue = value(work.get("journal-title", {})) or value(work.get("source", {})) or "Scholarly work"
            authors = people_from(work)
            source_url = value(work.get("url", {})) or (f"https://doi.org/{doi}" if doi else f"https://orcid.org/{ORCID_ID}")
            page_name = "orcid-" + hashlib.sha256((doi or put_code).encode()).hexdigest()[:12] + ".html"
            pdf_path = safe_pdf_download(source_url, Path(page_name).stem)
            (PUBLICATIONS / page_name).write_text(
                publication_html(title, authors, venue, year, doi, put_code, source_url, pdf_path),
                encoding="utf-8",
            )
            update_sitemap(page_name)
            new_entries.append({"key": key, "title": title, "authors": authors, "venue": venue,
                                "year": year, "doi": doi, "page": page_name})
            known.add(key)

    if new_entries:
        update_home(new_entries)
        saved["works"] = new_entries + saved.get("works", [])
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")
        print(f"Added {len(new_entries)} new ORCID work(s).")
    else:
        print("No new public ORCID works found.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ORCID sync failed: {error}", file=sys.stderr)
        sys.exit(1)
