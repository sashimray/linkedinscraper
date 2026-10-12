import io
import json
import os
import sys
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bs4 import BeautifulSoup

import scripts.single_link_run as single_link_run
from scripts.single_link_run import job_url, main, run

FIXTURES = os.path.join(ROOT, "tests", "fixtures")
JOB_ID = "4476740047"  # CODEplus fixture, also checked in test_job_parser.py
CANONICAL = f"https://www.linkedin.com/jobs/view/{JOB_ID}/"


def raises(exc, fn):
    try:
        fn()
    except exc as e:
        return e
    raise AssertionError(f"{exc.__name__} not raised")


def fixture_fetch(url, config):
    # Stands in for get_with_retry: serves the saved page instead of calling LinkedIn
    assert url == CANONICAL
    with open(os.path.join(FIXTURES, f"job_{JOB_ID}.html"), encoding="utf-8") as f:
        return BeautifulSoup(f.read(), "html.parser")


def test_job_url_accepts_urls_and_ids():
    for link in (CANONICAL, f"https://linkedin.com/jobs/view/{JOB_ID}", f"http://www.linkedin.com/jobs/view/{JOB_ID}/",
                 f"https://www.linkedin.com/jobs/view/{JOB_ID}/?refId=abc%3D&trackingId=xyz", JOB_ID, f"  {JOB_ID}  "):
        assert job_url(link) == CANONICAL, link


def test_job_url_rejects_other_input():
    for link in ("", None, "abc", "https://example.com/jobs/view/123/", f"{JOB_ID}?x=1",
                 f"https://www.linkedin.com.evil.com/jobs/view/{JOB_ID}/", f"https://www.linkedin.com/jobs/view/{JOB_ID}/ x",
                 "https://www.linkedin.com/in/someone/", "123; rm -rf /"):
        raises(ValueError, lambda: job_url(link))


def test_run_parses_the_fetched_page():
    details = run({}, JOB_ID, fetch=fixture_fetch)
    assert details["company"] == "CODEplus"
    assert details["company_industry"] == "IT Services and IT Consulting"
    assert details["sections_parsed"] == 1
    assert details["requirements"].startswith("- Java and Java Enterprise Edition")


def test_run_raises_when_fetch_fails():
    e = raises(RuntimeError, lambda: run({}, JOB_ID, fetch=lambda url, config: None))
    assert CANONICAL in str(e)


def test_run_validates_before_fetching():
    def no_fetch(url, config):
        raise AssertionError("should not fetch")
    raises(ValueError, lambda: run({}, "not-a-job", fetch=no_fetch))


def test_main_prints_json_and_reports_errors():
    original = single_link_run.get_with_retry
    out = io.StringIO()
    try:
        single_link_run.get_with_retry = fixture_fetch
        with redirect_stdout(out):
            assert main([JOB_ID, "--config", os.path.join(ROOT, "config_example.json")]) == 0
        assert json.loads(out.getvalue())["company"] == "CODEplus"

        out = io.StringIO()
        with redirect_stdout(out):
            assert main(["bad-link", "--config", os.path.join(ROOT, "config_example.json")]) == 1
            assert main([JOB_ID, "--config", "/nonexistent/config.json"]) == 1
        assert out.getvalue().count("Error:") == 2
    finally:
        single_link_run.get_with_retry = original


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"{len(tests)} tests passed")
