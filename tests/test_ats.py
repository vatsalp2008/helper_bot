"""Which system a posting link belongs to."""

from helper.ats import detect_ats


def test_sap_s_own_career_site_is_successfactors() -> None:  # SAP iXp internship, 2026-11-23
    url = ("https://careers.sap.com/job/Alpharetta-SAP-iXp-Intern-Digital-Value-Advisory-GA-30009/1389644833/"
           "?locale=en_US")
    assert detect_ats(url) == "successfactors"
    assert detect_ats("https://jobs.sap.com/job/Newtown-Square-Intern/123/") == "successfactors"
    assert detect_ats("https://career5.successfactors.eu/career?company=x") == "successfactors"
    assert detect_ats("https://www.sap.com/products.html") == "unknown"  # SAP's other pages aren't job sites


def test_oracle_on_a_company_domain_is_oracle() -> None:  # Dell, 2026-11-23
    dell = "https://enterpriseplatform.dell.com/hcmUI/CandidateExperience/en/sites/careers/job/R271283/?utm_medium=x"
    assert detect_ats(dell) == "oracle"
    assert detect_ats("https://hdhl.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/1009005") \
        == "oracle"
    assert detect_ats("https://www.dell.com/en-us/shop") == "unknown"
