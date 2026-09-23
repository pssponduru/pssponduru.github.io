#!/usr/bin/env python3
"""Synchronize new ORCID works into a Google Scholar-friendly static site.

This version keeps one consecutively numbered publication list, writes
APA-style visible citations, adds high-value citation meta tags, and downloads
only direct, public PDFs from an explicit allow-list.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
PUBLICATIONS = ROOT / "publications"
PDFS = ROOT / "pdfs"
DATA = ROOT / "data"
STATE_FILE = DATA / "orcid-sync.json"
INDEX_FILE = ROOT / "index.html"
SITEMAP_FILE = ROOT / "sitemap.xml"

ORCID_ID = os.environ.get("ORCID_ID", "0009-0009-5930-8265")
AUTHOR_NAME = os.environ.get("AUTHOR_NAME", "Prudvi Saisaran Ponduru")
SITE_URL = os.environ.get("SITE_URL", "https://pssponduru.github.io").rstrip("/")
ALLOW_PDF = os.environ.get("ALLOW_ORCID_PDF_DOWNLOAD", "false").lower() == "true"
REBUILD_LIST = os.environ.get("REBUILD_PUBLICATION_LIST", "false").lower() == "true"
TRUSTED_PDF_HOSTS = {
    item.strip().lower()
    for item in os.environ.get("TRUSTED_PDF_HOSTS", "").split(",")
    if item.strip()
}


def as_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def as_list(value: object) -> list:
    return value if isinstance(value, list) else []


def value(node: object) -> str:
    if isinstance(node, dict):
        node = node.get("value", "")
    return str(node).strip() if isinstance(node, (str, int, float)) else ""


def fetch_json(url: str, headers: dict[str, str] | None = None, data: bytes | None = None) -> dict:
    request = Request(url, data=data, headers=headers or {"Accept": "application/json"})
    with urlopen(request, timeout=30) as response:
        return as_dict(json.loads(response.read().decode("utf-8")))


def access_token() -> str:
    client_id = os.environ.get("ORCID_CLIENT_ID")
    client_secret = os.environ.get("ORCID_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError("Missing ORCID_CLIENT_ID or ORCID_CLIENT_SECRET GitHub secret.")
    payload = urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials",
        "scope": "/read-public",
    }).encode("utf-8")
    result = fetch_json(
        "https://orcid.org/oauth/token",
        {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        payload,
    )
    token = value(result.get("access_token"))
    if not token:
        raise RuntimeError("ORCID did not return an access token.")
    return token


def doi_from(work: dict) -> str:
    external_ids = as_list(as_dict(as_dict(work).get("external-ids")).get("external-id"))
    for external_id in external_ids:
        external_id = as_dict(external_id)
        if value(external_id.get("external-id-type")).lower() == "doi":
            return value(external_id.get("external-id-value")).removeprefix("https://doi.org/").strip()
    return ""


def orcid_authors(work: dict) -> list[str]:
    authors = []
    for contributor in as_list(as_dict(as_dict(work).get("contributors")).get("contributor")):
        name = value(as_dict(contributor).get("credit-name"))
        if name and name not in authors:
            authors.append(name)
    return authors or [AUTHOR_NAME]


def normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def similar_title(one: str, two: str) -> bool:
    a, b = normalize_title(one), normalize_title(two)
    return bool(a and b) and (
        a == b or a in b or b in a or SequenceMatcher(None, a, b).ratio() >= 0.78
    )


def doi_metadata(doi: str) -> dict:
    """Get bibliographic data without inventing missing APA fields."""
    if not doi:
        return {}
    headers = {"Accept": "application/json", "User-Agent": "ORCID-publication-sync/2.0"}
    try:
        result = fetch_json(f"https://api.crossref.org/works/{quote(doi, safe='')}", headers)
        message = as_dict(result.get("message"))
        if message:
            return {"source": "crossref", "raw": message}
    except Exception:
        pass
    try:
        result = fetch_json(f"https://api.datacite.org/dois/{quote(doi, safe='')}", headers)
        attributes = as_dict(as_dict(result.get("data")).get("attributes"))
        if attributes:
            return {"source": "datacite", "raw": attributes}
    except Exception:
        pass
    return {}


def initials(given: str) -> str:
    return " ".join(f"{part[0]}." for part in re.findall(r"[A-Za-z]+", given) if part)


def join_apa_names(names: list[str]) -> str:
    if not names:
        return AUTHOR_NAME
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + ", & " + names[-1]


def crossref_authors(raw: dict) -> list[str]:
    names = []
    for author in as_list(raw.get("author")):
        author = as_dict(author)
        family, given = value(author.get("family")), value(author.get("given"))
        literal = value(author.get("name")) or value(author.get("literal"))
        if family:
            names.append(f"{family}, {initials(given)}".rstrip())
        elif literal:
            names.append(literal)
    return names


def datacite_authors(raw: dict) -> list[str]:
    names = []
    for author in as_list(raw.get("creators")):
        author = as_dict(author)
        family, given = value(author.get("familyName")), value(author.get("givenName"))
        if family:
            names.append(f"{family}, {initials(given)}".rstrip())
        elif value(author.get("name")):
            names.append(value(author.get("name")))
    return names


def first_text(value_or_list: object) -> str:
    if isinstance(value_or_list, list):
        return value(value_or_list[0]) if value_or_list else ""
    return value(value_or_list)


def datacite_title(raw: dict) -> str:
    for title in as_list(raw.get("titles")):
        text = value(as_dict(title).get("title")) or value(title)
        if text:
            return text
    return ""


def work_details(work: dict, doi: str) -> dict:
    """Prefer DOI registry metadata; use ORCID only as a fallback."""
    details = doi_metadata(doi)
    raw, source = as_dict(details.get("raw")), value(details.get("source"))
    title = value(as_dict(work.get("title")).get("title"))
    year = value(as_dict(as_dict(work.get("publication-date")).get("year"))) or str(date.today().year)
    venue = value(work.get("journal-title")) or value(work.get("source")) or "Scholarly work"
    authors = orcid_authors(work)
    volume = issue = pages = ""
    pdf_candidates = [value(work.get("url"))]

    if source == "crossref":
        title = first_text(raw.get("title")) or title
        authors = crossref_authors(raw) or authors
        venue = first_text(raw.get("container-title")) or venue
        volume, issue, pages = value(raw.get("volume")), value(raw.get("issue")), value(raw.get("page"))
        date_parts = as_list(as_dict(raw.get("published-print")).get("date-parts")) or as_list(as_dict(raw.get("published-online")).get("date-parts"))
        if date_parts and isinstance(date_parts[0], list) and date_parts[0]:
            year = str(date_parts[0][0])
        for link in as_list(raw.get("link")):
            link = as_dict(link)
            if "pdf" in value(link.get("content-type")).lower():
                pdf_candidates.append(value(link.get("URL")))
    elif source == "datacite":
        title = datacite_title(raw) or title
        authors = datacite_authors(raw) or authors
        venue = value(raw.get("container-title")) or value(raw.get("publisher")) or venue
        volume, issue, pages = value(raw.get("volume")), value(raw.get("issue")), value(raw.get("page"))
        year = value(raw.get("publicationYear")) or year
        pdf_candidates.append(value(raw.get("contentUrl")))

    return {
        "title": title, "authors": authors, "year": year, "venue": venue,
        "volume": volume, "issue": issue, "pages": pages,
        "pdf_candidates": [item for item in pdf_candidates if item],
    }


def apa_reference(item: dict) -> str:
    authors = join_apa_names(item["authors"])
    journal = html.escape(item["venue"])
    volume = f" <em>{html.escape(item['volume'])}</em>" if item.get("volume") else ""
    issue = f"({html.escape(item['issue'])})" if item.get("issue") else ""
    pages = f", {html.escape(item['pages'])}" if item.get("pages") else ""
    doi = f' <a class="doi" href="https://doi.org/{html.escape(item["doi"])}">https://doi.org/{html.escape(item["doi"])} </a>' if item.get("doi") else ""
    return (
        f'{html.escape(authors)} ({html.escape(item["year"])}). '
        f'{html.escape(item["title"])}. <em>{journal}</em>{volume}{issue}{pages}.{doi}'
    )


def safe_pdf_download(candidates: list[str], page_stem: str) -> str:
    if not ALLOW_PDF:
        return ""
    for source_url in candidates:
        host = (urlparse(source_url).hostname or "").lower()
        if not host or host not in TRUSTED_PDF_HOSTS:
            continue
        if not source_url.lower().split("?")[0].endswith(".pdf"):
            continue
        try:
            request = Request(source_url, headers={"User-Agent": "ORCID-site-sync/2.0"})
            with urlopen(request, timeout=45) as response:
                content_type = response.headers.get("Content-Type", "").lower()
                content = response.read(5_000_001)
            if len(content) <= 5_000_000 and content.startswith(b"%PDF") and "pdf" in content_type:
                filename = f"{page_stem}.pdf"
                PDFS.mkdir(parents=True, exist_ok=True)
                (PDFS / filename).write_bytes(content)
                return f"/pdfs/{filename}"
        except HTTPError as error:
            print(f"PDF skipped (HTTP {error.code}): {source_url}")
        except Exception as error:
            print(f"PDF skipped ({error}): {source_url}")
    return ""


def publication_html(item: dict) -> str:
    title, venue = html.escape(item["title"]), html.escape(item["venue"])
    authors = item["authors"]
    author_tags = "\n".join(f'  <meta name="citation_author" content="{html.escape(name)}">' for name in authors)
    doi_tag = f'  <meta name="citation_doi" content="{html.escape(item["doi"])}">\n' if item.get("doi") else ""
    pdf_tag = f'  <meta name="citation_pdf_url" content="{SITE_URL}{item["pdf_path"]}">\n' if item.get("pdf_path") else ""
    volume_tag = f'  <meta name="citation_volume" content="{html.escape(item["volume"])}">\n' if item.get("volume") else ""
    issue_tag = f'  <meta name="citation_issue" content="{html.escape(item["issue"])}">\n' if item.get("issue") else ""
    pages = item.get("pages", "").split("-", 1)
    pages_tag = (f'  <meta name="citation_firstpage" content="{html.escape(pages[0])}">\n' +
                 (f'  <meta name="citation_lastpage" content="{html.escape(pages[1])}">\n' if len(pages) == 2 else "")) if item.get("pages") else ""
    pdf_line = f'<p><a href="{item["pdf_path"]}">Download full-text PDF</a></p>' if item.get("pdf_path") else ""
    doi_line = f'<a href="https://doi.org/{html.escape(item["doi"])}">https://doi.org/{html.escape(item["doi"])} </a>' if item.get("doi") else "Not supplied in ORCID"
    return f'''<!doctype html>
<html lang="en"><head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="{title} — {html.escape(join_apa_names(authors))}">
  <meta name="citation_title" content="{title}">
{author_tags}
  <meta name="citation_publication_date" content="{html.escape(item["year"])}-01-01">
  <meta name="citation_journal_title" content="{venue}">
{volume_tag}{issue_tag}{pages_tag}{doi_tag}{pdf_tag}  <title>{title} | {html.escape(AUTHOR_NAME)}</title>
  <style>body{{margin:0;font:18px/1.7 system-ui,sans-serif;color:#14283d;background:#f7f4ed}}main{{max-width:850px;margin:auto;padding:64px 24px}}a{{color:#0b5ea8}}h1{{font:700 clamp(2rem,5vw,3.7rem)/1.05 Georgia,serif}}.card{{background:#fff;border:1px solid #dde4eb;border-radius:16px;padding:24px;margin-top:28px}}</style>
</head><body><main><p>ORCID-synchronized publication</p><h1>{title}</h1><p><strong>{html.escape(join_apa_names(authors))}</strong></p><section class="card"><p>{apa_reference(item)}</p><p><strong>DOI:</strong> {doi_line}</p>{pdf_line}</section><p><a href="/">← Back to academic profile</a></p></main></body></html>'''


def current_titles_and_ids() -> tuple[set[str], list[str]]:
    source = INDEX_FILE.read_text(encoding="utf-8", errors="ignore")
    doi_ids = {html.unescape(item).lower().rstrip(".,;)") for item in re.findall(r'https?://doi\.org/([^"\'<\s]+)', source, re.I)}
    titles = [html.unescape(item) for item in re.findall(r'class="work-title"[^>]*>(.*?)</a>', source, re.S)]
    for page in PUBLICATIONS.glob("*.html"):
        content = page.read_text(encoding="utf-8", errors="ignore")
        doi_ids.update(html.unescape(item).lower() for item in re.findall(r'<meta name="citation_doi" content="([^"]+)"', content))
        titles.extend(html.unescape(item) for item in re.findall(r'<meta name="citation_title" content="([^"]+)"', content))
    return doi_ids, titles


def update_sitemap(page_name: str) -> None:
    source = SITEMAP_FILE.read_text(encoding="utf-8")
    loc = f"{SITE_URL}/publications/{page_name}"
    if f"<loc>{loc}</loc>" not in source:
        entry = f"  <url>\n    <loc>{loc}</loc>\n    <lastmod>{date.today().isoformat()}</lastmod>\n  </url>\n"
        SITEMAP_FILE.write_text(source.replace("</urlset>", entry + "</urlset>"), encoding="utf-8")


def update_home(entries: list[dict]) -> None:
    if not entries:
        return
    source = INDEX_FILE.read_text(encoding="utf-8")
    cards = []
    for item in entries:
        cards.append(
            f'<li><span class="work-num">00</span><div><a class="work-title" href="publications/{item["page"]}">{html.escape(item["title"])}</a>'
            f'<p>{apa_reference(item)}</p>'
            + (f'<a class="doi" href="{item["pdf_path"]}">Full text PDF</a>' if item.get("pdf_path") else "")
            + "</div></li>"
        )
    pattern = r'(<ol class="works">)(.*?)(</ol>)'
    match = re.search(pattern, source, flags=re.S)
    if not match:
        raise RuntimeError("The main publication list is missing from index.html.")
    combined = "\n".join(cards) + "\n" + match.group(2)
    counter = 0
    def number(match: re.Match) -> str:
        nonlocal counter
        counter += 1
        return f'<span class="work-num">{counter:02d}</span>'
    combined = re.sub(r'<span class="work-num">.*?</span>', number, combined)
    source = source[:match.start()] + match.group(1) + combined + match.group(3) + source[match.end():]
    source = re.sub(r'(<strong>)\d+(</strong><span>Research publications</span>)', rf'\g<1>{counter}\g<2>', source, count=1)
    INDEX_FILE.write_text(source, encoding="utf-8")


def existing_page_details(page: str) -> tuple[list[str], str]:
    path = ROOT / page.lstrip("/")
    if not path.exists():
        return [AUTHOR_NAME], ""
    source = path.read_text(encoding="utf-8", errors="ignore")
    authors = [html.unescape(item) for item in re.findall(r'<meta name="citation_author" content="([^"]+)"', source)]
    pdf = ""
    match = re.search(r'<meta name="citation_pdf_url" content="https?://[^/]+(/pdfs/[^"]+)"', source)
    if match:
        pdf = match.group(1)
    return authors or [AUTHOR_NAME], pdf


def details_from_doi(title: str, doi: str, page: str) -> dict:
    record = doi_metadata(doi)
    raw, source = as_dict(record.get("raw")), value(record.get("source"))
    authors, pdf_path = existing_page_details(page)
    item = {
        "title": title, "doi": doi, "page": page, "authors": authors,
        "year": "n.d.", "venue": "Scholarly work", "volume": "", "issue": "",
        "pages": "", "pdf_path": pdf_path,
    }
    if source == "crossref":
        item.update({
            "title": first_text(raw.get("title")) or title,
            "authors": crossref_authors(raw) or authors,
            "venue": first_text(raw.get("container-title")) or item["venue"],
            "volume": value(raw.get("volume")), "issue": value(raw.get("issue")),
            "pages": value(raw.get("page")),
        })
        date_parts = as_list(as_dict(raw.get("published-print")).get("date-parts")) or as_list(as_dict(raw.get("published-online")).get("date-parts"))
        if date_parts and isinstance(date_parts[0], list) and date_parts[0]:
            item["year"] = str(date_parts[0][0])
    elif source == "datacite":
        item.update({
            "title": datacite_title(raw) or title,
            "authors": datacite_authors(raw) or authors,
            "venue": value(raw.get("container-title")) or value(raw.get("publisher")) or item["venue"],
            "volume": value(raw.get("volume")), "issue": value(raw.get("issue")),
            "pages": value(raw.get("page")), "year": value(raw.get("publicationYear")) or item["year"],
        })
    return item


def extract_home_records() -> list[dict]:
    source = INDEX_FILE.read_text(encoding="utf-8", errors="ignore")
    match = re.search(r'<ol class="works">(.*?)</ol>', source, flags=re.S)
    if not match:
        raise RuntimeError("The main publication list is missing from index.html.")
    records = []
    for item in re.findall(r'<li>.*?</li>', match.group(1), flags=re.S):
        title_match = re.search(r'class="work-title" href="([^"]+)">(.*?)</a>', item, flags=re.S)
        doi_match = re.search(r'https?://doi\.org/([^"\'<\s]+)', item, flags=re.I)
        if not title_match:
            continue
        page, title = title_match.group(1), html.unescape(re.sub(r'<.*?>', '', title_match.group(2))).strip()
        doi = html.unescape(doi_match.group(1)).rstrip(".,;)") if doi_match else ""
        records.append({"page": page, "title": title, "doi": doi})
    return records


def remove_sitemap_page(page: str) -> None:
    source = SITEMAP_FILE.read_text(encoding="utf-8")
    loc = re.escape(f"{SITE_URL}/publications/{Path(page).name}")
    source = re.sub(rf'  <url>\s*<loc>{loc}</loc>\s*<lastmod>.*?</lastmod>\s*</url>\s*', '', source, flags=re.S)
    SITEMAP_FILE.write_text(source, encoding="utf-8")


def rebuild_publication_list() -> None:
    """One-time cleanup: preserves curated pages and removes generated duplicates."""
    records = extract_home_records()
    curated = [record for record in records if not Path(record["page"]).name.startswith("orcid-")]
    automatic = [record for record in records if Path(record["page"]).name.startswith("orcid-")]
    kept = list(curated)
    duplicate_pages = []
    for record in automatic:
        if any(similar_title(record["title"], old["title"]) for old in curated):
            duplicate_pages.append(record["page"])
        else:
            kept.append(record)
    items = [details_from_doi(record["title"], record["doi"], record["page"]) for record in kept]
    cards = []
    for number, item in enumerate(items, start=1):
        pdf = f'<a class="doi" href="{item["pdf_path"]}">Full text PDF</a>' if item.get("pdf_path") else ""
        cards.append(
            f'<li><span class="work-num">{number:02d}</span><div>'
            f'<a class="work-title" href="{html.escape(item["page"], quote=True)}">{html.escape(item["title"])}</a>'
            f'<p>{apa_reference(item)}</p>{pdf}</div></li>'
        )
    source = INDEX_FILE.read_text(encoding="utf-8")
    source = re.sub(r'(<ol class="works">).*?(</ol>)', r'\1' + "\n" + "\n".join(cards) + r'\2', source, count=1, flags=re.S)
    source = re.sub(r'(<strong>)\d+(</strong><span>Research publications</span>)', rf'\g<1>{len(items)}\g<2>', source, count=1)
    INDEX_FILE.write_text(source, encoding="utf-8")
    for page in duplicate_pages:
        path = ROOT / page.lstrip("/")
        if path.exists():
            path.unlink()
        remove_sitemap_page(page)
    print(f"Rebuilt {len(items)} numbered APA-style publication entries; removed {len(duplicate_pages)} duplicates.")


def main() -> None:
    if REBUILD_LIST:
        rebuild_publication_list()
    token = access_token()
    headers = {"Accept": "application/vnd.orcid+json", "Authorization": f"Bearer {token}"}
    listing = fetch_json(f"https://pub.orcid.org/v3.0/{ORCID_ID}/works", headers)
    known_dois, known_titles = current_titles_and_ids()
    state = as_dict(json.loads(STATE_FILE.read_text(encoding="utf-8"))) if STATE_FILE.exists() else {"works": []}
    saved = {value(as_dict(item).get("key")).lower() for item in as_list(state.get("works"))}
    new_entries = []
    for group in as_list(listing.get("group")):
        for summary in as_list(as_dict(group).get("work-summary")):
            put_code = value(as_dict(summary).get("put-code"))
            work = fetch_json(f"https://pub.orcid.org/v3.0/{ORCID_ID}/work/{put_code}", headers)
            doi = doi_from(work)
            details = work_details(work, doi)
            key = (doi or put_code).lower()
            if not key or key in known_dois or key in saved or any(similar_title(details["title"], old) for old in known_titles):
                continue
            page = "orcid-" + hashlib.sha256(key.encode()).hexdigest()[:12] + ".html"
            item = {"key": key, "doi": doi, "page": page, "put_code": put_code, **details}
            item["pdf_path"] = safe_pdf_download(item.pop("pdf_candidates"), Path(page).stem)
            (PUBLICATIONS / page).write_text(publication_html(item), encoding="utf-8")
            update_sitemap(page)
            new_entries.append(item)
            known_dois.add(key)
            known_titles.append(item["title"])
    if new_entries:
        update_home(new_entries)
        state["works"] = [{"key": item["key"]} for item in new_entries] + as_list(state.get("works"))
        DATA.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        print(f"Added {len(new_entries)} genuinely new ORCID work(s).")
    else:
        print("No genuinely new public ORCID works found.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ORCID sync failed: {error}", file=sys.stderr)
        sys.exit(1)
