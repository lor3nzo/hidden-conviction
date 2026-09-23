from dataclasses import dataclass
from datetime import datetime
from xml.etree import ElementTree as ET


@dataclass
class BeneficialOwner:
    issuer_cik: str
    issuer_name: str | None
    reporting_cik: str | None
    reporting_name: str
    schedule_type: str
    event_date: str | None
    shares_owned: float | None
    ownership_pct: float | None
    reporting_person_types: str | None


def _local(tag: str) -> str:
    return tag.rsplit('}', 1)[-1]


def _children(node: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in list(node) if _local(child.tag) == name]


def _first_desc(node: ET.Element, name: str) -> ET.Element | None:
    for item in node.iter():
        if _local(item.tag) == name:
            return item
    return None


def _text(node: ET.Element | None, name: str) -> str | None:
    if node is None:
        return None
    target = _first_desc(node, name)
    if target is None or target.text is None:
        return None
    value = target.text.strip()
    return value or None


def _float(value: str | None) -> float | None:
    if value is None:
        return None
    cleaned = value.replace(',', '').replace('%', '').strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _normalize_cik(value: str | None) -> str | None:
    if not value:
        return None
    return value.strip().lstrip('0') or '0'



def _normalize_date(value: str | None) -> str | None:
    if not value:
        return None
    raw = value.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            pass
    return raw


def _submission_type(root: ET.Element) -> str:
    value = _text(root, 'submissionType') or ''
    return value.strip().upper()


def parse_schedule_13(xml_text: str) -> list[BeneficialOwner]:
    """Parse structured Schedule 13D/13G XML introduced by the SEC.

    We keep each reporting person separately and never sum percentages because
    joint filers frequently report the same underlying shares.
    """
    root = ET.fromstring(xml_text)
    submission_type = _submission_type(root)

    if '13D' in submission_type:
        return _parse_13d(root, submission_type)
    if '13G' in submission_type:
        return _parse_13g(root, submission_type)

    raise ValueError(f'Unsupported Schedule 13 submission type: {submission_type or "unknown"}')


def _parse_13d(root: ET.Element, submission_type: str) -> list[BeneficialOwner]:
    issuer_cik = _normalize_cik(_text(root, 'issuerCIK'))
    issuer_name = _text(root, 'issuerName')
    event_date = _normalize_date(_text(root, 'dateOfEvent'))

    if not issuer_cik:
        raise ValueError('Schedule 13D issuer CIK missing')

    owners: list[BeneficialOwner] = []
    for node in root.iter():
        if _local(node.tag) != 'reportingPersonInfo':
            continue

        name = _text(node, 'reportingPersonName')
        if not name:
            continue

        types = [
            (child.text or '').strip()
            for child in node.iter()
            if _local(child.tag) == 'typeOfReportingPerson' and (child.text or '').strip()
        ]

        owners.append(
            BeneficialOwner(
                issuer_cik=issuer_cik,
                issuer_name=issuer_name,
                reporting_cik=_normalize_cik(_text(node, 'reportingPersonCIK')),
                reporting_name=name,
                schedule_type=submission_type,
                event_date=event_date,
                shares_owned=_float(_text(node, 'aggregateAmountOwned')),
                ownership_pct=_float(_text(node, 'percentOfClass')),
                reporting_person_types=','.join(types) or None,
            )
        )

    return owners


def _parse_13g(root: ET.Element, submission_type: str) -> list[BeneficialOwner]:
    issuer_cik = _normalize_cik(_text(root, 'issuerCik'))
    issuer_name = _text(root, 'issuerName')
    event_date = _normalize_date(_text(root, 'eventDateRequiresFilingThisStatement'))

    if not issuer_cik:
        raise ValueError('Schedule 13G issuer CIK missing')

    owners: list[BeneficialOwner] = []
    for node in root.iter():
        if _local(node.tag) != 'coverPageHeaderReportingPersonDetails':
            continue

        name = _text(node, 'reportingPersonName')
        if not name:
            continue

        types = [
            (child.text or '').strip()
            for child in node.iter()
            if _local(child.tag) == 'typeOfReportingPerson' and (child.text or '').strip()
        ]

        owners.append(
            BeneficialOwner(
                issuer_cik=issuer_cik,
                issuer_name=issuer_name,
                reporting_cik=_normalize_cik(_text(node, 'reportingCik')),
                reporting_name=name,
                schedule_type=submission_type,
                event_date=event_date,
                shares_owned=_float(_text(node, 'reportingPersonBeneficiallyOwnedAggregateNumberOfShares')),
                ownership_pct=_float(_text(node, 'classPercent')),
                reporting_person_types=','.join(types) or None,
            )
        )

    return owners
