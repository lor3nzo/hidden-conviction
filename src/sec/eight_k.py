import html
import re
from dataclasses import dataclass


PARSER_VERSION = "8k-parser-v0.7.6"
SCORING_VERSION = "8k-score-v0.7.7"


@dataclass
class EightKEvent:
    cik: str | None
    company_name: str | None
    event_date: str | None
    item_numbers: list[str]
    event_type: str
    sentiment: str
    event_score: float
    matched_keywords: list[str]
    excerpt: str | None
    primary_item: str | None = None
    scoring_rule: str | None = None
    scoring_reason: str | None = None
    source_section: str | None = None
    parser_version: str = PARSER_VERSION
    scoring_version: str = SCORING_VERSION


@dataclass
class _ItemDecision:
    item: str
    score: float
    label: str
    rule: str
    reason: str
    matches: list[str]


def submission_recent_row(payload: dict, accession: str) -> dict | None:
    """Return primary-document metadata for an accession from SEC submissions JSON."""
    recent = ((payload.get("filings") or {}).get("recent") or {})
    accessions = recent.get("accessionNumber") or []
    for i, value in enumerate(accessions):
        if value != accession:
            continue

        def at(key):
            values = recent.get(key) or []
            return values[i] if i < len(values) else None

        return {
            "primary_document": at("primaryDocument"),
            "filing_date": at("filingDate"),
            "form": at("form"),
        }
    return None


def _header_value(text: str, label: str) -> str | None:
    pattern = rf"(?mi)^\s*{re.escape(label)}\s*:\s*(.+?)\s*$"
    match = re.search(pattern, text)
    return match.group(1).strip() if match else None


def _normalize_cik(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    if not digits:
        return None
    return digits.lstrip("0") or "0"


def _extract_main_8k_document(submission: str) -> str:
    """Return the primary 8-K document, excluding exhibits and attachments."""
    for match in re.finditer(r"(?is)<DOCUMENT>(.*?)</DOCUMENT>", submission):
        block = match.group(1)
        type_match = re.search(r"(?mi)^<TYPE>\s*([^\r\n<]+)", block)
        doc_type = type_match.group(1).strip().upper() if type_match else ""
        if doc_type in {"8-K", "8-K/A"}:
            text_match = re.search(r"(?is)<TEXT>(.*?)</TEXT>", block)
            return text_match.group(1) if text_match else block

    # Production normally supplies only the primary document. Keep a bounded
    # fallback for tests and legacy/plain-text filings.
    return submission[:250_000]


def _visible_text(document: str) -> str:
    """Convert bounded primary-document HTML to readable normalized text."""
    document = document[:250_000]
    document = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", document)
    document = re.sub(
        r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>|</h[1-6]>|</table>|</section>",
        "\n",
        document,
    )
    document = re.sub(r"(?is)<[^>]+>", " ", document)
    document = html.unescape(document)
    document = document.replace("\xa0", " ")
    document = re.sub(r"[ \t\f\v]+", " ", document)
    document = re.sub(r" *\n *", "\n", document)
    document = re.sub(r"\n{2,}", "\n", document)
    return document[:160_000].strip()


def _clean_section(section: str, limit: int = 8_000) -> str:
    """Return readable audit text without HTML artifacts or whitespace noise."""
    clean = html.unescape(section or "").replace("\xa0", " ")
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:limit]


def _extract_sections(text: str) -> dict[str, str]:
    """Return Item-number -> bounded clean section text from the primary 8-K."""
    heading_re = re.compile(r"(?im)(?:^|\n)\s*Item\s+([0-9]+\.[0-9]{2})\b")
    matches = list(heading_re.finditer(text))
    sections: dict[str, str] = {}
    for i, match in enumerate(matches):
        item = match.group(1)
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        section = text[match.start():end][:20_000].strip()
        # Preserve the first occurrence for deterministic scoring. Duplicated
        # table-of-contents Item labels should not overwrite substantive text.
        if item not in sections or len(section) > len(sections[item]):
            sections[item] = section
    return sections


def _section_body(section: str, item: str) -> str:
    """Remove the Item heading line before semantic phrase detection.

    SEC's standard Item 3.01 heading itself contains 'Transfer of Listing' and
    'Delisting'. Those heading words must never create a body-level signal.
    """
    if not section:
        return ""
    lines = section.splitlines()
    if lines and re.match(rf"(?i)^\s*Item\s+{re.escape(item)}\b", lines[0]):
        body = "\n".join(lines[1:]).strip()
        if body:
            return body
    return section


def _has(section: str, *phrases: str) -> bool:
    return any(phrase in section for phrase in phrases)


def _append_match(matches: list[str], phrase: str) -> None:
    if phrase not in matches:
        matches.append(phrase)


def _has_unnegated(section: str, phrase: str) -> bool:
    """Match a phrase unless nearby language explicitly negates it."""
    for match in re.finditer(re.escape(phrase), section):
        prefix = section[max(0, match.start() - 48):match.start()]
        if re.search(r"\b(?:no|not|without)\b[^.;:\n]{0,40}$", prefix):
            continue
        return True
    return False


def _decision(
    item: str,
    score: float,
    label: str,
    rule: str,
    reason: str,
    matches: list[str] | None = None,
) -> _ItemDecision:
    return _ItemDecision(item, score, label, rule, reason, matches or [])


RESULTS_PHRASES = {
    "record revenue": 4.0,
    "record revenues": 4.0,
    "raises guidance": 5.0,
    "raised guidance": 5.0,
    "increased guidance": 4.0,
    "lowered guidance": -6.0,
    "reduced guidance": -6.0,
}

ANNOUNCEMENT_PHRASES = {
    "strategic partnership": 3.0,
    "awarded a contract": 4.0,
    "contract award": 4.0,
    "received regulatory approval": 4.0,
    "regulatory approval received": 4.0,
    "raises guidance": 5.0,
    "raised guidance": 5.0,
    "lowered guidance": -6.0,
    "reduced guidance": -6.0,
}


def _score_listing_item(section: str) -> _ItemDecision:
    """Contextual Item 3.01 scoring using body text, never heading text."""
    body = _section_body(section, "3.01").lower()

    if _has(
        body,
        "regained compliance",
        "has regained compliance",
        "now in compliance",
        "compliance has been restored",
    ):
        return _decision(
            "3.01", 0.0, "Listing compliance restored", "3.01.compliance_restored",
            "The filing states that the issuer regained or restored listing compliance.",
            ["listing compliance restored"],
        )

    # Actual delisting/suspension requires an explicit operational consequence.
    if _has(
        body,
        "will be delisted",
        "securities will be delisted",
        "has been delisted",
        "was delisted",
        "delisting will become effective",
        "delisting is effective",
        "delisting became effective",
        "trading will be suspended",
        "trading has been suspended",
        "trading was suspended",
        "suspended from trading",
        "suspension of trading",
        "will be removed from listing",
        "removed from listing",
    ):
        return _decision(
            "3.01", -12.0, "Delisting or trading suspension", "3.01.actual_delisting",
            "The Item 3.01 body states an actual or scheduled delisting, removal, or trading suspension.",
            ["actual delisting or suspension"],
        )

    # Voluntary transfer requires affirmative body language. The generic SEC Item
    # heading phrase 'Transfer of Listing' is intentionally insufficient.
    voluntary = _has(
        body,
        "voluntarily withdraw",
        "voluntary withdrawal",
        "intends to voluntarily withdraw",
        "intention to voluntarily withdraw",
        "elects to voluntarily transfer",
        "voluntarily transfer its listing",
        "voluntarily transfer the listing",
    ) and _has(
        body,
        "transfer the primary listing",
        "transfer its primary listing",
        "transfer its listing",
        "transfer the listing",
    )
    if voluntary:
        return _decision(
            "3.01", 0.0, "Voluntary exchange transfer", "3.01.voluntary_transfer",
            "The issuer affirmatively states that it is voluntarily moving its listing to another exchange.",
            ["voluntary exchange transfer"],
        )

    if _has(
        body,
        "not in compliance",
        "no longer meets",
        "no longer complies",
        "minimum bid price",
        "continued listing",
        "deficiency notice",
        "listing deficiency",
        "staff determination letter",
        "stockholders' equity requirement",
        "shareholders' equity requirement",
    ):
        return _decision(
            "3.01", -8.0, "Listing-rule noncompliance", "3.01.noncompliance",
            "The issuer reports a continued-listing deficiency or noncompliance notice without an effective delisting or suspension.",
            ["listing noncompliance"],
        )

    return _decision(
        "3.01", 0.0, "Listing matter", "3.01.other",
        "Item 3.01 is present, but the body does not establish a scored listing event.",
    )


def _score_item_201(section: str) -> _ItemDecision:
    body = _section_body(section, "2.01").lower()

    if _has(body, "termination agreement", "terminated the merger", "transaction was terminated", "agreement was terminated"):
        return _decision(
            "2.01", -4.0, "Terminated transaction", "2.01.transaction_terminated",
            "The Item 2.01 body describes termination of a material acquisition, disposition, or merger transaction.",
            ["transaction terminated"],
        )

    if _has(body, "completed the acquisition", "consummated the acquisition", "acquisition was completed", "completed the merger", "merger was completed", "consummated the merger"):
        return _decision(
            "2.01", 6.0, "Completed acquisition or merger", "2.01.completed_transaction",
            "The Item 2.01 body states that an acquisition or merger was completed or consummated.",
            ["completed acquisition or merger"],
        )

    if _has(body, "entered into an agreement to acquire", "agreement and plan of merger", "merger agreement", "agreed to acquire"):
        return _decision(
            "2.01", 4.0, "Acquisition or merger agreement", "2.01.transaction_agreement",
            "The Item 2.01 body describes a signed acquisition or merger agreement that has not necessarily closed.",
            ["acquisition or merger agreement"],
        )

    if _has(body, "sold substantially all", "sale of substantially all", "disposed of", "asset sale", "sale of assets"):
        adverse = _has(body, "liquidation", "insolvency", "distressed", "foreclosure", "receivership")
        if adverse:
            return _decision(
                "2.01", -4.0, "Distressed disposition", "2.01.distressed_disposition",
                "The disposition is described in an explicitly distressed, liquidation, foreclosure, insolvency, or receivership context.",
                ["distressed disposition"],
            )
        return _decision(
            "2.01", 0.0, "Asset disposition", "2.01.disposition",
            "The filing reports an asset disposition; disposition alone is not treated as conviction-positive.",
            ["asset disposition"],
        )

    return _decision(
        "2.01", 0.0, "Acquisition or disposition", "2.01.other",
        "Item 2.01 is present, but the transaction context is not specific enough for a directional score.",
    )


def _explicit_negation(body: str, noun_pattern: str) -> bool:
    """Recognize the standard Item 304 negative disclosure for a risk term."""
    return bool(re.search(
        rf"\b(?:no|without)\b[^.;\n]{{0,90}}{noun_pattern}",
        body,
    ))


def _affirmative_disagreement(body: str) -> bool:
    """Require affirmative disagreement language, not the SEC's legal boilerplate."""
    if re.search(r"\b(?:auditor|accountant|firm)\s+disagreed\b", body):
        return True
    for match in re.finditer(r"\bdisagreements?\b", body):
        context = body[max(0, match.start() - 90):match.end() + 90]
        if re.search(r"\b(?:no|without)\b[^.;\n]{0,80}\bdisagreements?\b", context):
            continue
        if re.search(r"\b(?:there\s+(?:was|were)|reported|identified|involved|arose|had)\b", context):
            return True
        if re.search(r"\bdisagreements?\s+with\s+(?:management|the company|the registrant)\b", context):
            return True
    return False


def _affirmative_reportable_event(body: str) -> bool:
    """Require an affirmative Item 304 reportable-event assertion."""
    for match in re.finditer(r"\breportable events?\b", body):
        context = body[max(0, match.start() - 100):match.end() + 100]
        if re.search(r"\b(?:no|without)\b[^.;\n]{0,90}\breportable events?\b", context):
            continue
        if re.search(r"\b(?:there\s+(?:was|were)|constituted|identified|reported|included)\b", context):
            return True
        if re.search(r"\b(?:a|the|one or more)\s+reportable events?\b", context):
            return True
    return False


def _sentence_context(body: str, position: int) -> str:
    """Return the local sentence/clause containing a match position."""
    starts = [body.rfind(". ", 0, position), body.rfind("; ", 0, position), body.rfind("\n", 0, position)]
    start = max(starts) + 1
    ends = [idx for idx in (body.find(". ", position), body.find("; ", position), body.find("\n", position)) if idx != -1]
    end = min(ends) if ends else len(body)
    return body[start:end].strip()


def _affirmative_material_weakness(body: str) -> bool:
    """Recognize an actual material weakness, including an exception to a no-events clause."""
    if not re.search(r"\bmaterial weakness(?:es)?\b", body):
        return False
    # Explicit direct negations of the weakness itself are neutral. Do not let
    # 'no reportable events, except for ... material weaknesses' negate the
    # weakness merely because the word 'no' appears earlier in the sentence.
    if re.search(
        r"\b(?:no|without(?:\s+any)?|did\s+not\s+(?:identify|have|report)|"
        r"does\s+not\s+(?:have|report))\b[^.;]{0,35}\bmaterial weakness(?:es)?\b",
        body,
    ) and not re.search(r"\bexcept\s+for\b[^.;]{0,100}\bmaterial weakness(?:es)?\b", body):
        return False
    return True


def _current_auditor_resignation(body: str) -> bool:
    """Detect a current resignation while ignoring historical references.

    Item 4.01 often recites an older auditor change as background. Phrases such
    as 'as previously disclosed ... resigned' must not make the current filing
    look like a fresh resignation. Historical cues are evaluated in the local
    sentence/clause, not against an arbitrary character window that can spill
    into a later current event.
    """
    for match in re.finditer(r"\b(?:resigned|resignation)\b", body):
        local = _sentence_context(body, match.start())
        if re.search(
            r"\b(?:as\s+)?previously\s+(?:disclosed|reported|announced)\b|"
            r"\bprior\s+(?:current\s+report|8-k|auditor)\b|"
            r"\bformer\s+auditor\b|"
            r"\bhad\s+resigned\b",
            local,
        ):
            continue
        if re.search(
            r"\b(?:independent\s+registered\s+public\s+accounting\s+firm|auditor|accountant|firm)\b"
            r".{0,160}\b(?:resigned|resignation)\b|"
            r"\bnotified\b.{0,160}\bof\s+(?:its|his|her|their)\s+resignation\b|"
            r"\b(?:resigned|resignation)\b.{0,120}\beffective\b",
            local,
        ):
            return True
    return False


def _score_item_401(section: str) -> _ItemDecision:
    body = re.sub(r"\s+", " ", _section_body(section, "4.01")).lower()
    matches: list[str] = []

    # Item 304 disclosures routinely contain the literal terms 'disagreement'
    # and 'reportable event' while expressly stating that none occurred. Use
    # affirmative semantic patterns rather than raw keyword presence.
    disagreement = _affirmative_disagreement(body)
    reportable = _affirmative_reportable_event(body)
    material_weakness = _affirmative_material_weakness(body)
    resignation = _current_auditor_resignation(body)

    if disagreement:
        _append_match(matches, "auditor disagreement")
    if reportable:
        _append_match(matches, "reportable event")
    if material_weakness:
        _append_match(matches, "material weakness")
    if resignation:
        _append_match(matches, "auditor resignation")

    if disagreement or reportable:
        score = -7.0 if disagreement and reportable else -5.0
        if material_weakness:
            score -= 2.0
        return _decision(
            "4.01", max(-9.0, score), "Auditor change with accounting concerns", "4.01.accounting_concern",
            "The current auditor-change section affirmatively reports a disagreement or reportable event; material-weakness language can increase severity.",
            matches,
        )

    if material_weakness:
        return _decision(
            "4.01", -3.0, "Auditor change with material weakness", "4.01.material_weakness",
            "The current Item 4.01 section reports a material weakness but does not affirmatively report an auditor disagreement or reportable event.",
            matches,
        )

    if resignation:
        return _decision(
            "4.01", -2.0, "Auditor resignation", "4.01.auditor_resignation",
            "The current independent auditor resigned; historical auditor resignations disclosed only as background are ignored.",
            matches,
        )

    return _decision(
        "4.01", 0.0, "Change in certifying accountant", "4.01.routine_change",
        "The current filing reports an auditor change without an affirmative disagreement, reportable event, material weakness, or current auditor resignation signal.",
    )


def _score_item_502(section: str) -> _ItemDecision:
    body = _section_body(section, "5.02").lower()
    matches: list[str] = []

    planned_retirement = _has(body, "retirement", "retire") and _has(
        body, "planned", "previously announced", "in accordance with", "effective upon", "normal retirement"
    )
    termination_for_cause = _has(body, "terminated for cause", "termination for cause", "dismissed for cause")
    departure = _has(body, "resigned", "resignation", "terminated", "termination", "departed", "departure", "retire", "retirement")
    appointment = _has(body, "appointed", "named as", "elected as", "elected to the board")
    ceo_cfo = _has(body, "chief executive officer", "chief financial officer", " ceo ", " cfo ")
    officer = _has(body, "chief ", "president", "executive officer", "treasurer", "controller")
    director_only = _has(body, "director", "board of directors") and not officer

    if termination_for_cause:
        return _decision(
            "5.02", -6.0, "Executive termination for cause", "5.02.termination_for_cause",
            "The Item 5.02 body explicitly states that an executive was terminated for cause.",
            ["termination for cause"],
        )

    if planned_retirement and departure:
        return _decision(
            "5.02", 0.0, "Planned executive retirement", "5.02.planned_retirement",
            "The departure is described as a planned or previously announced retirement rather than an abrupt management change.",
            ["planned retirement"],
        )

    if departure:
        if ceo_cfo:
            matches.append("CEO/CFO departure")
            return _decision(
                "5.02", -4.0, "CEO or CFO departure", "5.02.key_executive_departure",
                "The Item 5.02 body reports departure or resignation of the CEO or CFO.",
                matches,
            )
        if officer:
            matches.append("executive departure")
            return _decision(
                "5.02", -2.0, "Executive departure", "5.02.executive_departure",
                "The Item 5.02 body reports departure or resignation of an executive officer other than the CEO or CFO.",
                matches,
            )
        if director_only:
            matches.append("director departure")
            return _decision(
                "5.02", -1.0, "Director departure", "5.02.director_departure",
                "The Item 5.02 body reports a director departure without a key-executive departure.",
                matches,
            )

    if appointment:
        if ceo_cfo:
            return _decision(
                "5.02", 2.0, "CEO or CFO appointment", "5.02.key_executive_appointment",
                "The Item 5.02 body reports appointment of a CEO or CFO.",
                ["CEO/CFO appointment"],
            )
        if officer:
            return _decision(
                "5.02", 1.0, "Executive appointment", "5.02.executive_appointment",
                "The Item 5.02 body reports appointment of an executive officer.",
                ["executive appointment"],
            )
        return _decision(
            "5.02", 0.0, "Director appointment", "5.02.director_appointment",
            "The Item 5.02 body reports a board appointment without a scored executive-management change.",
            ["director appointment"],
        )

    return _decision(
        "5.02", 0.0, "Director or executive change", "5.02.other",
        "Item 5.02 is present, but no directional appointment, departure, retirement, or termination rule was established.",
    )


def _score_generic_item(item: str, section: str) -> _ItemDecision | None:
    body = _section_body(section, item).lower()
    matches: list[str] = []

    if item == "1.03":
        return _decision(item, -15.0, "Bankruptcy or receivership", "1.03.bankruptcy", "Item 1.03 reports bankruptcy or receivership.")
    if item == "2.04":
        if "default" in body:
            matches.append("default")
        if "acceleration" in body:
            matches.append("acceleration")
        return _decision(item, -10.0, "Acceleration or default trigger", "2.04.default_or_acceleration", "Item 2.04 reports a trigger that accelerates or increases a direct financial obligation.", matches)
    if item == "2.06":
        if "impairment" in body:
            matches.append("impairment")
        return _decision(item, -8.0 if matches else -6.0, "Material impairment", "2.06.material_impairment", "Item 2.06 reports a material impairment charge or conclusion.", matches)
    if item == "4.02":
        score = -10.0
        if "restatement" in body or "restate" in body:
            score -= 3.0
            matches.append("restatement")
        if "material weakness" in body:
            score -= 2.0
            matches.append("material weakness")
        return _decision(item, max(-15.0, score), "Non-reliance or restatement", "4.02.nonreliance", "Item 4.02 states that previously issued financial statements or an audit report should not be relied upon, with restatement/material-weakness cues increasing severity.", matches)

    if item == "1.01":
        score = 3.0
        for phrase in ("strategic partnership", "awarded a contract", "contract award"):
            if phrase in body:
                score += 3.0 if phrase == "strategic partnership" else 4.0
                matches.append(phrase)
        return _decision(item, min(8.0, score), "Material definitive agreement", "1.01.material_agreement", "Item 1.01 reports a material definitive agreement; explicit partnership or contract-award language can add confirmation.", matches)
    if item == "1.02":
        return _decision(item, -3.0, "Termination of material agreement", "1.02.material_agreement_termination", "Item 1.02 reports termination of a material definitive agreement.")
    if item == "2.02":
        score = 0.0
        for phrase, points in RESULTS_PHRASES.items():
            if phrase in body:
                score += points
                matches.append(phrase)
        return _decision(item, score, "Results of operations", "2.02.results_context", "Results are neutral by themselves; only explicit section-local record-revenue or guidance language changes the score.", matches)
    if item == "2.03":
        return _decision(item, -2.0, "Material financing obligation", "2.03.financing_obligation", "Item 2.03 reports creation of a direct financial obligation or an off-balance-sheet arrangement.")
    if item == "3.02":
        return _decision(item, -1.0, "Unregistered sale of securities", "3.02.unregistered_sale", "Item 3.02 reports an unregistered sale of equity securities.")
    if item == "5.01":
        return _decision(item, 3.0, "Change in control", "5.01.change_in_control", "Item 5.01 reports a change in control.")
    if item == "5.03":
        return _decision(item, 0.0, "Charter or bylaw change", "5.03.neutral", "Charter or bylaw changes are neutral without another independently scored Item.")
    if item == "5.07":
        return _decision(item, 0.0, "Shareholder vote", "5.07.neutral", "Shareholder voting results are neutral without another independently scored Item.")
    if item in {"7.01", "8.01"}:
        score = 0.0
        for phrase, points in ANNOUNCEMENT_PHRASES.items():
            if phrase in body:
                score += points
                matches.append(phrase)
        if _has(
            body,
            "substantial doubt about the company's ability to continue as a going concern",
            "substantial doubt about its ability to continue as a going concern",
            "substantial doubt about our ability to continue as a going concern",
        ):
            score -= 5.0
            matches.append("substantial doubt about going concern")
        label = "Regulation FD disclosure" if item == "7.01" else "Other material event"
        rule = "7.01.section_local" if item == "7.01" else "8.01.section_local"
        return _decision(item, score, label, rule, "The generic disclosure Item is neutral unless an authorized phrase appears inside that Item's own section.", matches)

    return None


def _score_item(item: str, section: str) -> _ItemDecision | None:
    if item == "3.01":
        return _score_listing_item(section)
    if item == "2.01":
        return _score_item_201(section)
    if item == "4.01":
        return _score_item_401(section)
    if item == "5.02":
        return _score_item_502(section)
    return _score_generic_item(item, section)


def _choose_primary(decisions: list[_ItemDecision], items: list[str]) -> _ItemDecision | None:
    if decisions:
        # Largest absolute contribution is the primary rationale. For ties,
        # preserve filing Item order for deterministic audit output.
        order = {item: i for i, item in enumerate(items)}
        return sorted(decisions, key=lambda d: (-abs(d.score), order.get(d.item, 999)))[0]
    return None


def parse_8k_submission(
    text: str,
    fallback_cik: str | None = None,
    fallback_company_name: str | None = None,
    fallback_event_date: str | None = None,
) -> EightKEvent:
    cik = _normalize_cik(_header_value(text, "CENTRAL INDEX KEY")) or _normalize_cik(fallback_cik)
    company_name = _header_value(text, "COMPANY CONFORMED NAME") or fallback_company_name
    filed = _header_value(text, "FILED AS OF DATE")
    event_date = fallback_event_date
    if filed and re.fullmatch(r"\d{8}", filed):
        event_date = f"{filed[:4]}-{filed[4:6]}-{filed[6:8]}"

    primary = _extract_main_8k_document(text)
    visible = _visible_text(primary)
    sections = _extract_sections(visible)
    items = list(sections.keys())

    # Rare legacy/plain-text filings may expose ITEM INFORMATION metadata but no
    # normal Item headings. Preserve Item codes, but do not scan the whole filing
    # because section locality cannot be established safely.
    if not items:
        items = sorted(set(
            m.group(1)
            for m in re.finditer(
                r"(?mi)^\s*ITEM INFORMATION\s*:\s*([0-9]+\.[0-9]{2})",
                primary[:80_000],
            )
        ))

    decisions: list[_ItemDecision] = []
    event_types: list[str] = []
    matches: list[str] = []

    for item in items:
        # Item 9.01 is supporting exhibit material and never contributes to score.
        if item == "9.01":
            continue
        decision = _score_item(item, sections.get(item, ""))
        if decision is None:
            continue
        decisions.append(decision)
        event_types.append(decision.label)
        for phrase in decision.matches:
            _append_match(matches, phrase)

    score = round(max(-15.0, min(15.0, sum(d.score for d in decisions))), 1)
    sentiment = "positive" if score > 0 else "negative" if score < 0 else "neutral"
    event_type = "; ".join(dict.fromkeys(event_types)) if event_types else "8-K disclosure"

    primary_decision = _choose_primary(decisions, items)
    primary_item = primary_decision.item if primary_decision else next((i for i in items if i != "9.01"), None)
    source_section = _clean_section(sections.get(primary_item, ""), limit=8_000) if primary_item else None
    excerpt = source_section[:1_000] if source_section else None

    if primary_decision:
        scoring_rule = primary_decision.rule
        scoring_reason = primary_decision.reason
    elif primary_item:
        scoring_rule = f"{primary_item}.unscored"
        scoring_reason = "The filing contains a recognized Item but no directional scoring rule was triggered."
    else:
        scoring_rule = "8k.no_recognized_item"
        scoring_reason = "No recognized Item section was available for deterministic 8-K scoring."

    return EightKEvent(
        cik=cik,
        company_name=company_name,
        event_date=event_date,
        item_numbers=items,
        event_type=event_type,
        sentiment=sentiment,
        event_score=score,
        matched_keywords=matches,
        excerpt=excerpt,
        primary_item=primary_item,
        scoring_rule=scoring_rule,
        scoring_reason=scoring_reason,
        source_section=source_section,
        parser_version=PARSER_VERSION,
        scoring_version=SCORING_VERSION,
    )
