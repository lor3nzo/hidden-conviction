import html
import re
from dataclasses import dataclass


@dataclass
class ThirteenFPosition:
    issuer_name: str
    cusip: str
    value_dollars: float
    shares: float
    title_of_class: str | None = None
    put_call: str | None = None
    source_rows: int = 1

    @property
    def value_thousands(self) -> float:
        """Backward-compatible view of the SEC value in thousands of dollars."""
        return self.value_dollars / 1000.0


@dataclass
class ThirteenFReport:
    manager_cik: str | None
    manager_name: str | None
    filing_date: str | None
    period_of_report: str | None
    positions: list[ThirteenFPosition]
    report_type: str | None = None


@dataclass
class ThirteenFTopReport(ThirteenFReport):
    # v0.6.8 semantics: total_positions is the number of unique, aggregated,
    # non-option CUSIPs in the filing. raw_position_rows preserves the original
    # information-table row count for auditability.
    total_positions: int = 0
    raw_position_rows: int = 0
    option_rows: int = 0
    portfolio_value_dollars: float = 0.0


def _header_value(text: str, label: str) -> str | None:
    match = re.search(rf"(?mi)^\s*{re.escape(label)}\s*:\s*(.+?)\s*$", text)
    return match.group(1).strip() if match else None


def _normalize_cik(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    return (digits.lstrip("0") or "0") if digits else None


def _iso_date(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().replace("/", "-")
    if re.fullmatch(r"\d{8}", value):
        return f"{value[:4]}-{value[4:6]}-{value[6:8]}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    if re.fullmatch(r"\d{2}-\d{2}-\d{4}", value):
        mm, dd, yyyy = value.split("-")
        return f"{yyyy}-{mm}-{dd}"
    return None


def _period_of_report(text: str) -> str | None:
    period = _iso_date(_header_value(text, "CONFORMED PERIOD OF REPORT"))
    if period:
        return period

    match = re.search(
        r"<(?:\w+:)?periodOfReport>\s*([^<]+)\s*</(?:\w+:)?periodOfReport>",
        text,
        re.I,
    )
    if match:
        period = _iso_date(match.group(1))
        if period:
            return period

    match = re.search(
        r"Report\s+for\s+the\s+Calendar\s+Year\s+or\s+Quarter\s+Ended\s*:?\s*</?[^>]*>?\s*(\d{2}[-/]\d{2}[-/]\d{4}|\d{4}[-/]\d{2}[-/]\d{2})",
        text,
        re.I,
    )
    return _iso_date(match.group(1)) if match else None


def _report_type(text: str) -> str | None:
    match = re.search(
        r"<(?:\w+:)?reportType>\s*([^<]+)\s*</(?:\w+:)?reportType>",
        text,
        re.I,
    )
    if match:
        return " ".join(match.group(1).split()).upper()
    upper = text.upper()
    if "13F NOTICE" in upper:
        return "13F NOTICE"
    if "13F COMBINATION REPORT" in upper:
        return "13F COMBINATION REPORT"
    if "13F HOLDINGS REPORT" in upper:
        return "13F HOLDINGS REPORT"
    return None


def _document_type(block: str) -> str | None:
    match = re.search(r"(?mi)^<TYPE>\s*([^\r\n<]+)", block)
    return match.group(1).strip() if match else None


def _document_filename(block: str) -> str | None:
    match = re.search(r"(?mi)^<FILENAME>\s*([^\r\n<]+)", block)
    return match.group(1).strip() if match else None


def _document_body(block: str) -> str:
    xml_match = re.search(r"<XML>\s*([\s\S]*?)\s*</XML>", block, re.I)
    if xml_match:
        return xml_match.group(1)
    text_match = re.search(r"<TEXT>\s*([\s\S]*?)\s*</TEXT>", block, re.I)
    if text_match:
        return text_match.group(1)
    return block


def extract_information_table_document(text: str) -> tuple[str | None, str | None]:
    """Return the canonical INFORMATION TABLE body and filename, if present."""
    for match in re.finditer(r"<DOCUMENT>([\s\S]*?)</DOCUMENT>", text, re.I):
        block = match.group(1)
        doc_type = (_document_type(block) or "").upper().strip()
        filename = _document_filename(block)
        marker = f"{doc_type} {filename or ''}".upper()
        if doc_type == "INFORMATION TABLE" or "INFORMATION TABLE" in marker or "INFOTABLE" in marker:
            return _document_body(block), filename
    return None, None


def has_information_table_document(text: str) -> bool:
    return re.search(r"(?mi)^<TYPE>\s*INFORMATION TABLE\s*$", text) is not None


def information_table_filename(text: str) -> str | None:
    """Return the INFORMATION TABLE attachment filename without parsing holdings.

    This is intentionally lightweight for Cloudflare Python Workers. It scans
    only the document header around the INFORMATION TABLE type marker instead
    of materializing the XML body.
    """
    match = re.search(r"(?mi)^<TYPE>\s*INFORMATION TABLE\s*$", text)
    if not match:
        return None
    window = text[match.start():match.start() + 4096]
    filename = re.search(r"(?mi)^<FILENAME>\s*([^\r\n<]+)", window)
    return filename.group(1).strip() if filename else None


def parse_13f_submission_metadata(text: str) -> ThirteenFTopReport:
    """Parse only cover/header metadata, never the information-table rows."""
    return ThirteenFTopReport(
        manager_cik=_normalize_cik(_header_value(text, "CENTRAL INDEX KEY")),
        manager_name=_header_value(text, "COMPANY CONFORMED NAME"),
        filing_date=_iso_date(_header_value(text, "FILED AS OF DATE")),
        period_of_report=_period_of_report(text),
        positions=[],
        report_type=_report_type(text),
        total_positions=0,
        raw_position_rows=0,
        option_rows=0,
        portfolio_value_dollars=0.0,
    )


def parse_13f_information_table_batch(
    text: str, *, cursor: int = 0, block_ordinal: int = 0, limit: int = 100
):
    """Parse at most ``limit`` raw infoTable blocks starting at ``cursor``.

    Returns ``(rows, next_cursor, next_block_ordinal, exhausted)``. Rows are
    deliberately *not* aggregated. A durable D1 staging table handles
    idempotency, and the final aggregate is performed by SQLite rather than
    inside Pyodide. This bounds Python CPU for very large 13F filings.
    """
    if limit <= 0:
        return [], max(0, int(cursor or 0)), max(0, int(block_ordinal or 0)), False

    start_re = re.compile(r"<(?:\w+:)?infoTable\b", re.I)
    end_re = re.compile(r"</(?:\w+:)?infoTable>", re.I)
    pos = max(0, int(cursor or 0))
    ordinal = max(0, int(block_ordinal or 0))
    rows = []
    scanned = 0

    while scanned < limit:
        start = start_re.search(text, pos)
        if not start:
            return rows, len(text), ordinal, True
        end = end_re.search(text, start.end())
        if not end:
            return rows, len(text), ordinal, True

        block = text[start.start():end.end()]
        row_ordinal = ordinal
        ordinal += 1
        scanned += 1
        pos = end.end()

        issuer = _tag_text(block, "nameOfIssuer")
        cusip = _tag_text(block, "cusip")
        value = _tag_text(block, "value")
        shares = _tag_text(block, "sshPrnamt")
        title = _tag_text(block, "titleOfClass")
        put_call = _normalized_put_call(_tag_text(block, "putCall"))
        if not issuer or not cusip:
            continue
        try:
            value_num = float((value or "0").replace(",", ""))
            shares_num = float((shares or "0").replace(",", ""))
        except ValueError:
            continue

        rows.append({
            "row_ordinal": row_ordinal,
            "issuer_name": issuer,
            "title_of_class": title,
            "cusip": cusip.upper().strip(),
            "value_dollars": value_num,
            "shares": shares_num,
            "put_call": put_call,
        })

    exhausted = start_re.search(text, pos) is None
    return rows, pos, ordinal, exhausted


def _tag_text(block: str, tag: str) -> str | None:
    match = re.search(
        rf"<(?:\w+:)?{re.escape(tag)}\b[^>]*>\s*([\s\S]*?)\s*</(?:\w+:)?{re.escape(tag)}>",
        block,
        re.I,
    )
    if not match:
        return None
    value = re.sub(r"<[^>]+>", "", match.group(1))
    value = html.unescape(value).strip()
    return value or None


def _iter_info_table_blocks(text: str):
    pattern = re.compile(
        r"(<(?:\w+:)?infoTable\b[\s\S]*?</(?:\w+:)?infoTable>)",
        re.I,
    )
    for match in pattern.finditer(text):
        yield match.group(1)


def _normalized_put_call(value: str | None) -> str | None:
    value = " ".join((value or "").upper().split())
    return value if value in {"PUT", "CALL"} else None



def infer_13f_value_scale(
    price_samples: int,
    low_price_samples: int,
    average_raw_position_value: float | None,
) -> tuple[float, str]:
    """Infer whether a filing's <value> field is dollars or legacy thousands.

    SEC Form 13F moved to nearest-dollar value reporting in 2023, but some
    later filings still appear to contain legacy-thousands values.  We only
    apply a 1,000x correction when the cross-section is strongly inconsistent
    with ordinary per-share prices *and* the raw average position value is
    unusually small.  Ambiguous filings remain unscaled.
    """
    samples = max(0, int(price_samples or 0))
    low = max(0, int(low_price_samples or 0))
    avg_value = float(average_raw_position_value or 0.0)
    if samples <= 0:
        return 1.0, "dollars_assumed_no_samples"

    low_fraction = low / samples
    if samples >= 20 and low_fraction >= 0.70 and 0 < avg_value <= 100_000:
        return 1000.0, "legacy_thousands_inferred"
    if samples >= 8 and low_fraction >= 0.90 and 0 < avg_value <= 50_000:
        return 1000.0, "legacy_thousands_inferred"
    return 1.0, "dollars_assumed"

def parse_13f_information_table_summary(text: str, limit: int = 250):
    """Parse, aggregate, filter, and rank one 13F information table.

    v0.6.8 aggregates duplicate disclosure rows by CUSIP before quarter over
    quarter comparison. This matters because one manager can report the same
    security on separate SOLE/DFND/OTHER investment-discretion rows.

    PUT/CALL rows are excluded from the long-equity signal and from the long
    portfolio denominator. The denominator is computed across the entire
    eligible filing before the top-N retention limit is applied.

    Returns: (selected_positions, unique_eligible_positions,
              full_long_portfolio_value_dollars, raw_rows, option_rows)
    """
    import heapq

    aggregates: dict[str, ThirteenFPosition] = {}
    representative_value: dict[str, float] = {}
    raw_rows = 0
    option_rows = 0

    for block in _iter_info_table_blocks(text):
        issuer = _tag_text(block, "nameOfIssuer")
        cusip = _tag_text(block, "cusip")
        value = _tag_text(block, "value")
        shares = _tag_text(block, "sshPrnamt")
        title = _tag_text(block, "titleOfClass")
        put_call = _normalized_put_call(_tag_text(block, "putCall"))
        if not issuer or not cusip:
            continue
        try:
            value_num = float((value or "0").replace(",", ""))
            shares_num = float((shares or "0").replace(",", ""))
        except ValueError:
            continue

        raw_rows += 1
        cusip_key = cusip.upper().strip()
        if put_call:
            option_rows += 1
            continue

        existing = aggregates.get(cusip_key)
        if existing is None:
            aggregates[cusip_key] = ThirteenFPosition(
                issuer_name=issuer,
                cusip=cusip_key,
                value_dollars=value_num,
                shares=shares_num,
                title_of_class=title,
                put_call=None,
                source_rows=1,
            )
            representative_value[cusip_key] = value_num
        else:
            existing.value_dollars += value_num
            existing.shares += shares_num
            existing.source_rows += 1
            # Keep descriptive text from the economically largest component.
            if value_num > representative_value.get(cusip_key, 0.0):
                existing.issuer_name = issuer
                existing.title_of_class = title
                representative_value[cusip_key] = value_num

    total_positions = len(aggregates)
    price_samples = [
        p for p in aggregates.values()
        if p.shares > 0 and p.value_dollars > 0
    ]
    low_price_samples = sum(
        1 for p in price_samples if (p.value_dollars / p.shares) < 2.0
    )
    average_raw_position_value = (
        sum(p.value_dollars for p in price_samples) / len(price_samples)
        if price_samples else 0.0
    )
    value_scale, _ = infer_13f_value_scale(
        len(price_samples), low_price_samples, average_raw_position_value
    )
    if value_scale != 1.0:
        for p in aggregates.values():
            p.value_dollars *= value_scale

    portfolio_value = sum(max(0.0, p.value_dollars) for p in aggregates.values())

    if limit <= 0 or not aggregates:
        return [], total_positions, portfolio_value, raw_rows, option_rows

    heap: list[tuple[float, str, ThirteenFPosition]] = []
    for cusip_key, position in aggregates.items():
        entry = (position.value_dollars, cusip_key, position)
        if len(heap) < limit:
            heapq.heappush(heap, entry)
        elif position.value_dollars > heap[0][0]:
            heapq.heapreplace(heap, entry)

    positions = [item[2] for item in sorted(heap, key=lambda x: (x[0], x[1]), reverse=True)]
    return positions, total_positions, portfolio_value, raw_rows, option_rows


def parse_13f_information_table(text: str, limit: int = 250):
    """Backward-compatible two-value parser result."""
    positions, total, _, _, _ = parse_13f_information_table_summary(text, limit=limit)
    return positions, total


def parse_13f_submission(text: str) -> ThirteenFReport:
    top = parse_13f_submission_top(text, limit=1_000_000)
    return ThirteenFReport(
        manager_cik=top.manager_cik,
        manager_name=top.manager_name,
        filing_date=top.filing_date,
        period_of_report=top.period_of_report,
        positions=top.positions,
        report_type=top.report_type,
    )


def parse_13f_submission_top(text: str, limit: int = 250) -> ThirteenFTopReport:
    """Parse a complete 13F submission and keep the largest aggregated longs."""
    manager_cik = _normalize_cik(_header_value(text, "CENTRAL INDEX KEY"))
    manager_name = _header_value(text, "COMPANY CONFORMED NAME")
    filing_date = _iso_date(_header_value(text, "FILED AS OF DATE"))
    period = _period_of_report(text)
    report_type = _report_type(text)

    info_body, _ = extract_information_table_document(text)
    if info_body is not None:
        positions, total, portfolio_value, raw_rows, option_rows = parse_13f_information_table_summary(
            info_body, limit=limit
        )
    elif re.search(r"<(?:\w+:)?infoTable\b", text, re.I):
        positions, total, portfolio_value, raw_rows, option_rows = parse_13f_information_table_summary(
            text, limit=limit
        )
    else:
        positions, total, portfolio_value, raw_rows, option_rows = [], 0, 0.0, 0, 0

    return ThirteenFTopReport(
        manager_cik=manager_cik,
        manager_name=manager_name,
        filing_date=filing_date,
        period_of_report=period,
        positions=positions,
        report_type=report_type,
        total_positions=total,
        raw_position_rows=raw_rows,
        option_rows=option_rows,
        portfolio_value_dollars=portfolio_value,
    )
