import json
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bs4 import BeautifulSoup

import db
import llm_sections
from job_parser import classify_heading, parse_job_details, parse_job_text
from resumekit.application_kit import ApplicationKit

FIXTURES = os.path.join(ROOT, "tests", "fixtures")

# job id -> (company, industry, sections_parsed, {field: (line count, start of first line)})
EXPECTED = {
    "4476740047": ("CODEplus", "IT Services and IT Consulting", 1, {
        "responsibilities": (10, "- Monitor EIE and its interfaces"),
        "requirements": (6, "- Java and Java Enterprise Edition"),
        "preferred_qualifications": (0, "")}),
    "4400259782": ("GRVTY", "Defense and Space Manufacturing", 1, {
        "responsibilities": (17, "Join a mission-focused team"),
        "requirements": (16, "- Active TS/SCI clearance"),
        "preferred_qualifications": (8, "- Experience supporting government")}),
    "4465578013": ("LinkedIn", "Technology, Information and Internet", 1, {
        "responsibilities": (3, "- You will design, implement"),
        "requirements": (3, "- BA/BS Degree in Computer Science"),
        "preferred_qualifications": (6, "- 2+ years of relevant work experience")}),
    "4466103929": ("Safety Mojo", "Software Development", 1, {
        "responsibilities": (8, "- Backend Development:"),
        "requirements": (6, "- 6-12 years of general development"),
        "preferred_qualifications": (3, "- Experience with Microservices")}),
    "4466592173": ("Tyler Technologies", "Software Development", 1, {
        "responsibilities": (4, "- Develop and/or modify software modules"),
        "requirements": (6, "- Pursuing a bachelor"),
        "preferred_qualifications": (0, "")}),
    "4467794566": ("Slack", "Technology, Information and Internet", 1, {
        "responsibilities": (6, "- Conceptualize and Build:"),
        "requirements": (7, "- 10+ years of professional experience"),
        "preferred_qualifications": (0, "")}),
    "4467946091": ("CGI", "IT Services and IT Consulting", 1, {
        "responsibilities": (5, "Develop, test, and deploy software"),
        "requirements": (7, "Bachelor's degree in Computer Science"),
        "preferred_qualifications": (2, "Familiarity with modern development")}),
    "4474476083": ("Clearwater Analytics", "Software Development", 1, {
        "responsibilities": (10, "- Design, build, deploy"),
        "requirements": (15, "- Proficiency in a typed"),
        "preferred_qualifications": (0, "")}),
    "4475721579": ("Scribd, Inc.", "Software Development", 1, {
        "responsibilities": (8, "- Your main focus will be"),
        "requirements": (7, "- You bring significant experience"),
        "preferred_qualifications": (4, "- Strong coding ability in Ruby.")}),
    "4476586331": ("JPMorganChase", "Financial Services", 1, {
        "responsibilities": (8, "- Executes software solutions"),
        "requirements": (10, "- Formal training or certification"),
        "preferred_qualifications": (3, "- Exposure to cloud technologies")}),
    "4476780543": ("AAA Global",
                   "Financial Services, Securities and Commodity Exchanges, and Capital Markets", 0, {
        "responsibilities": (0, ""),
        "requirements": (6, "- Strong Java development experience"),
        "preferred_qualifications": (0, "")}),
    "4476908482": ("Harvard Medical School and Massachusetts General Hospital", "Higher Education", 1, {
        "responsibilities": (6, "- Assist in the development"),
        "requirements": (6, "- Current undergraduate or graduate"),
        "preferred_qualifications": (6, "- Experience with medical imaging")}),
    "4476935007": ("ELEVI Associates", "Information Technology & Services", 1, {
        "responsibilities": (3, "- Support the modernization of malware"),
        "requirements": (6, "- You must have a current or active security clearance"),
        "preferred_qualifications": (4, "- Experience building and publishing")}),
}


def load_fixture(job_id):
    with open(os.path.join(FIXTURES, f"job_{job_id}.html"), encoding="utf-8") as f:
        return BeautifulSoup(f.read(), "html.parser")


def test_fixtures():
    for job_id, (company, industry, parsed, sections) in EXPECTED.items():
        details = parse_job_details(load_fixture(job_id))
        assert details["company"] == company, (job_id, details["company"])
        assert details["company_industry"] == industry, (job_id, details["company_industry"])
        assert details["sections_parsed"] == parsed, (job_id, details["sections_parsed"])
        for field, (count, start) in sections.items():
            lines = details[field].split("\n") if details[field] else []
            assert len(lines) == count, (job_id, field, len(lines), lines)
            if count:
                assert lines[0].replace("’", "'").startswith(start), (job_id, field, lines[0])


def test_no_boilerplate_in_sections():
    for job_id in EXPECTED:
        details = parse_job_details(load_fixture(job_id))
        text = " ".join(details[f] for f in ("responsibilities", "requirements", "preferred_qualifications")).lower()
        for boilerplate in ("equal opportunity", "salary range", "$200,000", "#internship"):
            assert boilerplate not in text, (job_id, boilerplate)


def test_classify_heading_variants():
    cases = {
        "What you'll do": "responsibilities",
        "What You’ll Be Owning": "responsibilities",
        "Some of the day-to-day activities:": "responsibilities",
        "Who you are": "requirements",
        "What you'll bring": "requirements",
        "Minimum Qualifications": "requirements",
        "Required Skills/Experience:": "requirements",
        "What we'd like you to have": "preferred_qualifications",
        "Preferred Qualifications, Capabilities, And Skills": "preferred_qualifications",
        "Desired qualifications/non essential skills required:": "preferred_qualifications",
        "It’s a plus if you also have -": "preferred_qualifications",
        "Benefits": "other",
        "About Us": "other",
        "Background Check Requirement": "other",
        "Pay Range:": "other",
        "You Have:": "requirements",
        "Nice If You Have:": "preferred_qualifications",
        "What You Will Bring to the Role:": "requirements",
        "In This Role, You Will": "responsibilities",
        "We'll Trust You To": "responsibilities",
        "Required Qualifications To Be Successful In This Role": "requirements",
        "Employee Referral Bonus": "other",
        "Talents Needed For Success": "requirements",
        "To be successful in this role you will": "requirements",
        "Tech Stack": "requirements",
        "Technology Expertise": "requirements",
        "Technologies you’ll likely be working with:": "requirements",
        "What You Will Have": "requirements",
        "You Are": "requirements",
        "Strong Candidates Demonstrate": "requirements",
        "What makes you stand out:": "preferred_qualifications",
        "Ideally, you’ll also have": "preferred_qualifications",
        "Top candidates will also have": "preferred_qualifications",
        "Ideally, you’ll bring:": "requirements",                   # the posting's only qualifications list
        "Essential Functions": "responsibilities",
        "We will count on you to:": "responsibilities",
        "Expect to:": "responsibilities",
        "The role you'll play": "responsibilities",
        "What we offer": "other",
        "Talent Community": "other",
    }
    for heading, expected in cases.items():
        assert classify_heading(heading) == expected, (heading, classify_heading(heading))


def test_text_matches_html_parse():
    # The stored job_description text has no bold, but these postings' headings don't depend on it
    import copy
    from main import transform_job
    for job_id in EXPECTED:
        if job_id == "4400259782":  # transform_job glues two intro paragraphs together, so 3 prose lines differ
            continue
        soup = load_fixture(job_id)
        from_html = parse_job_details(copy.copy(soup))
        from_text = parse_job_text(transform_job(soup))
        for field in ("responsibilities", "requirements", "preferred_qualifications", "sections_parsed"):
            html_value, text_value = from_html[field], from_text[field]
            if isinstance(html_value, str):
                html_value, text_value = html_value.replace("- ", ""), text_value.replace("- ", "")
            assert html_value == text_value, (job_id, field)


def test_missing_page_returns_empty_details():
    details = parse_job_details(None)
    assert details["sections_parsed"] == 0
    assert details["company"] == "" and details["requirements"] == ""


def make_jobs_db(tmp, jobs):
    # jobs: (title, company, job_posting_id, job_description, hidden)
    path = os.path.join(tmp, "jobs.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, company TEXT, "
                 "job_posting_id TEXT, job_description TEXT, hidden INTEGER)")
    conn.executemany("INSERT INTO jobs (title, company, job_posting_id, job_description, hidden) VALUES (?, ?, ?, ?, ?)", jobs)
    conn.commit()
    conn.close()
    return path


def job_details(path):
    conn = sqlite3.connect(path)
    rows = {r[0]: r[1:] for r in conn.execute(
        "SELECT j.company, d.responsibilities, d.requirements, d.preferences, d.sections_parsed "
        "FROM job_details d JOIN jobs j ON j.id = d.job_id")}
    conn.close()
    return rows


def fake_llm(calls):
    def generate(system, user, schema):
        calls.append(user)
        return json.dumps({"responsibilities": ["- Build APIs"], "requirements": ["Python"],
                           "preferred_qualifications": []})
    return generate


PARSEABLE = "Responsibilities:\n- Build things\nRequirements:\n- Python\nNice to have:\n- Go"


def test_hist_run_then_llm_run():
    import hist_run
    with tempfile.TemporaryDirectory() as tmp:
        path = make_jobs_db(tmp, [
            ("Dev", "A", "1", PARSEABLE, 0),
            ("Dev", "B", "2", "We need someone good. Apply now.", 0),
            ("Dev", "C", "3", PARSEABLE, 1),                               # hidden
            ("Dev", "D", "4", "Could not find Job Description", 0),
        ])
        config = {"db_path": path, "jobs_tablename": "jobs"}
        assert hist_run.hist_run(config) == 2
        rows = job_details(path)
        assert rows["A"] == ("- Build things", "- Python", "- Go", 1)
        assert rows["B"] == ("", "", "", 0)
        assert set(rows) == {"A", "B"}

        # The LLM run only picks up B (sections_parsed 0)
        calls = []
        done, failed = llm_sections.run(config, ApplicationKit("ollama", generate_fn=fake_llm(calls)), "jobs")
        assert (done, failed, len(calls)) == (1, 0, 1)
        assert job_details(path)["B"] == ("- Build APIs", "- Python", "", 2)

        # Re-running hist_run updates rule-parsed rows but keeps what the LLM filled in
        assert hist_run.hist_run(config) == 2
        rows = job_details(path)
        assert rows["B"] == ("- Build APIs", "- Python", "", 2)
        assert rows["A"] == ("- Build things", "- Python", "- Go", 1)
        conn = sqlite3.connect(path)
        assert conn.execute("SELECT count(*) FROM job_details").fetchone()[0] == 2
        conn.close()


def test_clearance_or_citizenship_requirement():
    from hist_run import clearance_or_citizenship_requirement as check
    hide = [
        "- Active TS/SCI clearance (eligible for CI Poly)",
        "- Ability to obtain a Secret clearance",
        "- U.S. citizenship is required.",
        "- Must be a US Citizen and able to obtain and maintain a Public Trust.",
        "Clearance Requirements\nSecret",
        "- Must have a current/active TS/SCI w/Polygraph security clearance",
        "- Active Secret security clearance, with the ability to obtain/maintain a Top Secret clearance if required.",
        "- Python 5+ years\n- United States Citizen \u2014 able to gain CJIS clearance",
        "- Must be eligible for a clearance if required for the work. U.S. citizenship is required to be considered.",
    ]
    keep = [
        "- US citizen or green card holder: this position requires access to export-controlled documentation",
        "- Applicants must be a U.S. citizen or national, U.S. permanent resident (i.e. current Green Card holder)",
        "- Candidates must provide proof of either US citizenship, Permanent US residency, or protected individual status",
        "- Must be a U.S. Person due to required access to export-controlled information",
        "- Must be legally authorized to work in the United States without sponsorship",
        "- Experience with authentication and authorization (OAuth, RBAC)",
        "- Experience working with DoD, Civilian Agencies, or the Intelligence Community",
        "- An active DoD Top Secret clearance is desired.",
        "- Experience with AWS Secrets Manager and secret rotation",
        "",
        None,
    ]
    for text in hide:
        assert check(text), text
    for text in keep:
        assert check(text) is None, (text, check(text))


def test_hide_clearance_jobs():
    import hist_run
    with tempfile.TemporaryDirectory() as tmp:
        path = make_jobs_db(tmp, [
            ("Dev", "A", "1", "Requirements:\n- Python\n- Active TS/SCI clearance", 0),
            ("Dev", "B", "2", "Requirements:\n- Python\n- US citizen or green card holder", 0),
            ("Dev", "C", "3", "Requirements:\n- Python", 0),
        ])
        config = {"db_path": path, "jobs_tablename": "jobs"}
        hist_run.hist_run(config)
        assert hist_run.hide_clearance_jobs(config, dry_run=True) == [1]
        conn = sqlite3.connect(path)
        assert conn.execute("SELECT count(*) FROM jobs WHERE hidden = 1").fetchone()[0] == 0  # dry run changes nothing
        conn.close()

        assert hist_run.hide_clearance_jobs(config) == [1]
        conn = sqlite3.connect(path)
        assert conn.execute("SELECT company, hidden FROM jobs ORDER BY id").fetchall() == [("A", 1), ("B", 0), ("C", 0)]
        conn.close()


def test_llm_run_picks_up_jobs_without_details_row():
    with tempfile.TemporaryDirectory() as tmp:
        path = make_jobs_db(tmp, [("Dev", "A", "1", "text", 0)])
        conn = sqlite3.connect(path)
        jobs = db.read_jobs_without_sections(conn, "jobs")
        conn.close()
        assert [j["company"] for j in jobs] == ["A"]


def test_scraper_saves_sections_by_posting_id():
    with tempfile.TemporaryDirectory() as tmp:
        path = make_jobs_db(tmp, [("Dev", "A", "111", "text", 0), ("Dev", "B", "222", "text", 0)])
        sections = parse_job_text(PARSEABLE)
        conn = sqlite3.connect(path)
        db.save_job_details_by_posting(conn, "jobs", {"222": sections, "999": sections})  # 999 isn't in jobs
        conn.close()
        assert job_details(path) == {"B": ("- Build things", "- Python", "- Go", 1)}


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"{len(tests)} tests passed")
