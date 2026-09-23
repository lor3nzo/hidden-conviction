import json
import re
from datetime import datetime, timezone
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

from .client import sec_fetch_text

LATEST_FORM4_ATOM = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&owner=only&count=100&output=atom"
LATEST_13D_ATOM = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=SCHEDULE%2013D&count=100&output=atom"
LATEST_13G_ATOM = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=SCHEDULE%2013G&count=100&output=atom"
LATEST_8K_ATOM = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&count=100&output=atom"
LATEST_13F_ATOM = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=13F-HR&count=100&output=atom"


def _accession_from_url(url: str) -> str | None:
    match = re.search(r"(\d{10}-\d{2}-\d{6})", url)
    return match.group(1) if match else None


def _submission_text_url(link: str, accession: str) -> str:
    parsed = urlparse(link)
    path = parsed.path
    if "/Archives/" not in path:
        raise ValueError("Unexpected SEC filing link")
    directory = path.rsplit("/", 1)[0]
    return f"https://www.sec.gov{directory}/{accession}.txt"


def _entry_form(title: str) -> str | None:
    upper = title.upper()
    for form in (
        "SCHEDULE 13D/A",
        "SCHEDULE 13D",
        "SCHEDULE 13G/A",
        "SCHEDULE 13G",
        "13F-HR/A",
        "13F-HR",
        "8-K/A",
        "8-K",
        "4/A",
        "4",
    ):
        if upper.startswith(form + " -") or upper == form:
            return form
    return None


def parse_current_atom(text: str, forced_form: str | None = None) -> list[dict]:
    root = ET.fromstring(text)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    rows: list[dict] = []

    for entry in root.findall("a:entry", ns):
        title = (entry.findtext("a:title", default="", namespaces=ns) or "").strip()
        updated = (entry.findtext("a:updated", default="", namespaces=ns) or "").strip()
        link_node = entry.find("a:link", ns)
        link = link_node.get("href") if link_node is not None else None
        if not link:
            continue

        accession = _accession_from_url(link)
        if not accession:
            continue

        form_type = _entry_form(title) or forced_form
        if not form_type:
            continue

        rows.append({
            "company_name": title,
            "form_type": form_type,
            "cik": None,
            "filed_at": updated[:10] if updated else datetime.now(timezone.utc).date().isoformat(),
            "accepted_at": updated or None,
            "filename": None,
            "accession_number": accession,
            "filing_url": _submission_text_url(link, accession),
        })

    # The same Schedule 13 filing can appear multiple times in Current Filings,
    # once for the filer and once for the subject. Deduplicate by accession.
    deduped: dict[str, dict] = {}
    for row in rows:
        deduped[row["accession_number"]] = row
    return list(deduped.values())


async def discover_latest_form4(user_agent: str | None = None) -> list[dict]:
    return parse_current_atom(
        await sec_fetch_text(LATEST_FORM4_ATOM, user_agent=user_agent),
        forced_form="4",
    )


async def discover_latest_ownership(user_agent: str | None = None) -> list[dict]:
    rows: list[dict] = []
    # Treat one feed failing as non-fatal so Form 4 ingestion continues.
    for url, form, feed_name in (
        (LATEST_13D_ATOM, "SCHEDULE 13D", "13d"),
        (LATEST_13G_ATOM, "SCHEDULE 13G", "13g"),
    ):
        try:
            parsed = parse_current_atom(
                await sec_fetch_text(url, user_agent=user_agent),
                forced_form=form,
            )
            for row in parsed:
                row["discovery_feed"] = feed_name
            rows.extend(parsed)
        except Exception:
            continue

    deduped: dict[str, dict] = {}
    for row in rows:
        deduped[row["accession_number"]] = row
    return list(deduped.values())


async def discover_latest_8k(user_agent: str | None = None) -> list[dict]:
    return parse_current_atom(
        await sec_fetch_text(LATEST_8K_ATOM, user_agent=user_agent),
        forced_form="8-K",
    )


async def discover_latest_13f(user_agent: str | None = None) -> list[dict]:
    return parse_current_atom(
        await sec_fetch_text(LATEST_13F_ATOM, user_agent=user_agent),
        forced_form="13F-HR",
    )

# Backward-compatible test/helper name from v0.2.x.
def parse_latest_form4_atom(text: str) -> list[dict]:
    return parse_current_atom(text, forced_form="4")

_DAILY_FORM_MAP = {
    "4": "4",
    "4/A": "4/A",
    "SC 13D": "SCHEDULE 13D",
    "SC 13D/A": "SCHEDULE 13D/A",
    "SC 13G": "SCHEDULE 13G",
    "SC 13G/A": "SCHEDULE 13G/A",
    "8-K": "8-K",
    "8-K/A": "8-K/A",
    "13F-HR": "13F-HR",
    "13F-HR/A": "13F-HR/A",
}




def daily_index_directory_url(day: str) -> str:
    """Return the SEC quarterly daily-index directory metadata URL for YYYY-MM-DD."""
    parsed = datetime.strptime(day, "%Y-%m-%d")
    quarter = ((parsed.month - 1) // 3) + 1
    return (
        f"https://www.sec.gov/Archives/edgar/daily-index/{parsed.year}/"
        f"QTR{quarter}/index.json"
    )


def parse_daily_index_directory(text: str) -> list[dict]:
    """Parse SEC index.json and return published master index files in date order.

    SEC directory metadata is the authority for whether a daily index exists.
    This avoids guessing a same-day master.YYYYMMDD.idx URL before SEC publishes it.
    """
    payload = json.loads(text)
    directory = payload.get("directory") if isinstance(payload, dict) else None
    items = directory.get("item", []) if isinstance(directory, dict) else []
    rows: list[dict] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        match = re.fullmatch(r"master\.(\d{8})\.idx", name)
        if not match:
            continue
        stamp = match.group(1)
        try:
            day = datetime.strptime(stamp, "%Y%m%d").date().isoformat()
        except ValueError:
            continue
        rows.append({
            "name": name,
            "date": day,
            "last_modified": item.get("last-modified") or item.get("last_modified"),
            "size": item.get("size"),
        })
    rows.sort(key=lambda row: row["date"])
    return rows


async def discover_available_daily_indexes(
    anchor_day: str,
    user_agent: str | None = None,
) -> list[dict]:
    """List master daily indexes the SEC has actually published for anchor_day's quarter."""
    url = daily_index_directory_url(anchor_day)
    text = await sec_fetch_text(url, user_agent=user_agent)
    return parse_daily_index_directory(text)


async def discover_latest_available_daily_index(
    anchor_day: str,
    user_agent: str | None = None,
) -> tuple[str, list[dict], str] | None:
    """Fetch the latest SEC-published master index on or before anchor_day.

    Returns (index_date, rows, index_name), or None if the quarter has no published
    master index yet.
    """
    available = await discover_available_daily_indexes(anchor_day, user_agent=user_agent)
    eligible = [row for row in available if row["date"] <= anchor_day]
    if not eligible:
        return None
    selected = eligible[-1]
    rows = await discover_daily_index(selected["date"], user_agent=user_agent)
    return selected["date"], rows, selected["name"]

def daily_master_index_url(day: str) -> str:
    """Build an SEC daily master-index URL for YYYY-MM-DD."""
    parsed = datetime.strptime(day, "%Y-%m-%d")
    quarter = ((parsed.month - 1) // 3) + 1
    return (
        f"https://www.sec.gov/Archives/edgar/daily-index/{parsed.year}/"
        f"QTR{quarter}/master.{parsed.strftime('%Y%m%d')}.idx"
    )


def parse_daily_master_index(text: str) -> list[dict]:
    """Parse relevant SEC master-index rows into the normal discovery shape."""
    rows: list[dict] = []
    for raw in text.splitlines():
        if "|" not in raw:
            continue
        parts = raw.split("|", 4)
        if len(parts) != 5:
            continue
        cik, company_name, form, filed_at, filename = [part.strip() for part in parts]
        mapped = _DAILY_FORM_MAP.get(form.upper())
        if not mapped:
            continue
        accession = _accession_from_url(filename)
        if not accession:
            continue
        path = filename.lstrip("/")
        filing_url = f"https://www.sec.gov/Archives/{path}"
        rows.append({
            "company_name": company_name or None,
            "form_type": mapped,
            "cik": (cik.lstrip("0") or "0") if cik else None,
            "filed_at": filed_at,
            "accepted_at": None,
            "filename": filename,
            "accession_number": accession,
            "filing_url": filing_url,
        })
    deduped = {row["accession_number"]: row for row in rows}
    return list(deduped.values())


async def discover_daily_index(day: str, user_agent: str | None = None) -> list[dict]:
    """Backfill relevant forms from the SEC daily master index when feeds saturate."""
    url = daily_master_index_url(day)
    text = await sec_fetch_text(url, user_agent=user_agent)
    return parse_daily_master_index(text)
