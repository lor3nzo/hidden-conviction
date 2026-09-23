from src.sec.discovery import parse_latest_form4_atom

ATOM = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns='http://www.w3.org/2005/Atom'>
  <entry>
    <title>4 - Example Corp</title>
    <updated>2026-09-20T14:31:00-04:00</updated>
    <link href='https://www.sec.gov/Archives/edgar/data/320193/000032019326000123/0000320193-26-000123-index.htm'/>
  </entry>
</feed>"""


def test_parse_latest_form4_atom():
    rows = parse_latest_form4_atom(ATOM)
    assert len(rows) == 1
    assert rows[0]["accession_number"] == "0000320193-26-000123"
    assert rows[0]["filing_url"].endswith("/0000320193-26-000123.txt")
    assert rows[0]["filed_at"] == "2026-09-20"
    assert rows[0]["form_type"] == "4"
