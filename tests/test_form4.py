from src.sec.form4 import parse_form4

SAMPLE = """<ownershipDocument>
<issuer>
  <issuerCik>0000320193</issuerCik>
  <issuerName>APPLE INC</issuerName>
  <issuerTradingSymbol>AAPL</issuerTradingSymbol>
</issuer>
<reportingOwner>
  <reportingOwnerId><rptOwnerName>DOE JOHN</rptOwnerName></reportingOwnerId>
  <reportingOwnerRelationship><isDirector>0</isDirector><isOfficer>1</isOfficer><officerTitle>CFO</officerTitle></reportingOwnerRelationship>
</reportingOwner>
<nonDerivativeTable>
  <nonDerivativeTransaction>
    <transactionDate><value>2026-09-20</value></transactionDate>
    <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
    <transactionAmounts><transactionShares><value>1000</value></transactionShares><transactionPricePerShare><value>25</value></transactionPricePerShare></transactionAmounts>
    <postTransactionAmounts><sharesOwnedFollowingTransaction><value>5000</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
    <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
  </nonDerivativeTransaction>
</nonDerivativeTable>
</ownershipDocument>"""


def test_parse_purchase():
    rows = parse_form4(SAMPLE)
    assert len(rows) == 1
    assert rows[0].issuer_cik == "320193"
    assert rows[0].issuer_ticker == "AAPL"
    assert rows[0].issuer_name == "APPLE INC"
    assert rows[0].is_open_market_purchase
    assert rows[0].transaction_value == 25000
    assert rows[0].officer_title == "CFO"
