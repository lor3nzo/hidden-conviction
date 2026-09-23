import asyncio
import json

import src.pipeline_13f as p13f
from src.scoring.hcs import score_13f_position_details


def test_existing_position_small_drift_scores_zero():
    s = score_13f_position_details(58101, 57999, 13_537_539, 3.1279)
    assert s.total == 0
    assert s.change_score == 0
    assert s.is_new_position is False


def test_existing_position_thresholds():
    assert score_13f_position_details(103, 100, 1_000_000, 0.1).change_score == 1
    assert score_13f_position_details(107, 100, 1_000_000, 0.1).change_score == 2
    assert score_13f_position_details(115, 100, 1_000_000, 0.1).change_score == 4
    assert score_13f_position_details(130, 100, 1_000_000, 0.1).change_score == 6
    assert score_13f_position_details(175, 100, 1_000_000, 0.1).change_score == 8
    assert score_13f_position_details(250, 100, 1_000_000, 0.1).change_score == 10


def test_new_position_materiality_gate():
    tiny = score_13f_position_details(100, None, 1_000_000, 0.1)
    assert tiny.total == 0
    meaningful = score_13f_position_details(100, None, 10_000_000, 0.1)
    assert meaningful.total >= 4


def test_probable_funds_are_identified_conservatively():
    assert p13f._looks_like_fund('SPDR S&P 500 ETF TR', 'TR UNIT')
    assert p13f._looks_like_fund('ISHARES TR', 'RUS MID CAP ETF')
    assert p13f._looks_like_fund('VANGUARD INDEX FUNDS', 'ETF')
    assert not p13f._looks_like_fund('APPLE INC', 'COM')
    assert not p13f._looks_like_fund('JPMORGAN CHASE & CO.', 'COM')


def test_attachment_fallback_scans_beyond_old_eight_file_limit(monkeypatch):
    info_xml = '''<informationTable><infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>COM</titleOfClass><cusip>037833100</cusip><value>10000000</value><shrsOrPrnAmt><sshPrnamt>40000</sshPrnamt></shrsOrPrnAmt></infoTable></informationTable>'''
    items = [{"name": f"primary{i}.xml", "size": 100+i} for i in range(12)]
    items.append({"name": "q42025_13fv8.xml", "size": 999})
    index_json = json.dumps({"directory": {"item": items}})

    async def fake_fetch(url, user_agent=None):
        if url.endswith('/index.json'):
            return index_json
        if url.endswith('/q42025_13fv8.xml'):
            return info_xml
        return '<xml></xml>'

    monkeypatch.setattr(p13f, 'sec_fetch_text', fake_fetch)

    class Env:
        SEC_USER_AGENT = 'test@example.com'

    positions, total = asyncio.run(
        p13f._fallback_information_table(Env(), 'https://www.sec.gov/Archives/edgar/data/1/abc/primary.xml')
    )
    assert total == 1
    assert positions[0].cusip == '037833100'
