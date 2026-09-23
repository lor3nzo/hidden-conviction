from src.sec.beneficial_ownership import parse_schedule_13
from src.scoring.hcs import score_beneficial_ownership


def test_parse_13d():
    xml = '''<?xml version="1.0"?>
    <edgarSubmission xmlns="http://www.sec.gov/edgar/schedule13D">
      <headerData><submissionType>SCHEDULE 13D</submissionType></headerData>
      <formData>
        <coverPageHeader><dateOfEvent>09/18/2026</dateOfEvent><issuerInfo>
          <issuerCIK>0000123456</issuerCIK><issuerName>Example Inc.</issuerName>
        </issuerInfo></coverPageHeader>
        <reportingPersons><reportingPersonInfo>
          <reportingPersonCIK>0000999999</reportingPersonCIK>
          <reportingPersonName>Whale Capital LP</reportingPersonName>
          <aggregateAmountOwned>1500000</aggregateAmountOwned>
          <percentOfClass>7.5</percentOfClass>
          <typeOfReportingPerson>PN</typeOfReportingPerson>
        </reportingPersonInfo></reportingPersons>
      </formData>
    </edgarSubmission>'''
    rows = parse_schedule_13(xml)
    assert len(rows) == 1
    assert rows[0].issuer_cik == '123456'
    assert rows[0].reporting_name == 'Whale Capital LP'
    assert rows[0].ownership_pct == 7.5
    assert score_beneficial_ownership('SCHEDULE 13D', 7.5, None) == 13.0


def test_parse_13g():
    xml = '''<?xml version="1.0"?>
    <edgarSubmission xmlns="http://www.sec.gov/edgar/schedule13g">
      <headerData><submissionType>SCHEDULE 13G/A</submissionType></headerData>
      <formData>
        <coverPageHeader><eventDateRequiresFilingThisStatement>09/17/2026</eventDateRequiresFilingThisStatement>
          <issuerInfo><issuerCik>0000654321</issuerCik><issuerName>Passive Co.</issuerName></issuerInfo>
        </coverPageHeader>
        <coverPageHeaderReportingPersonDetails>
          <reportingCik>0000888888</reportingCik>
          <reportingPersonName>Index Manager</reportingPersonName>
          <reportingPersonBeneficiallyOwnedAggregateNumberOfShares>2200000</reportingPersonBeneficiallyOwnedAggregateNumberOfShares>
          <classPercent>10.2</classPercent>
          <typeOfReportingPerson>IA</typeOfReportingPerson>
        </coverPageHeaderReportingPersonDetails>
      </formData>
    </edgarSubmission>'''
    rows = parse_schedule_13(xml)
    assert len(rows) == 1
    assert rows[0].issuer_cik == '654321'
    assert rows[0].ownership_pct == 10.2
    assert score_beneficial_ownership('SCHEDULE 13G/A', 10.2, 8.5) == 14.0
