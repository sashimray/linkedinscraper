import glob
import io
import os
import sys
import tempfile
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bs4 import BeautifulSoup

import preview_job_html
from job_parser import SECTION_FIELDS, description_html, parse_description_html, parse_job_details
from preview_job_html import main, preview, report, write_page

FIXTURES = os.path.join(ROOT, "tests", "fixtures")
JOB_ID = "4476740047"
URL = f"https://www.linkedin.com/jobs/view/{JOB_ID}/"
CONFIG = os.path.join(ROOT, "config_example.json")


def load(path):
    with open(path, encoding="utf-8") as f:
        return BeautifulSoup(f.read(), "html.parser")


def fixture_soup(job_id=JOB_ID):
    return load(os.path.join(FIXTURES, f"job_{job_id}.html"))


def test_description_html_keeps_structure_and_drops_noise():
    saved = description_html(fixture_soup())
    assert '<li>' in saved and '<strong>' in saved and '<br/>' in saved
    for noise in ('<button', '<icon', 'class=', 'data-', '<!--', 'Show more', 'Show less'):
        assert noise not in saved, noise
    assert saved.startswith('<strong>Job Description')


def test_description_html_leaves_page_untouched():
    soup = fixture_soup()
    before = str(soup)
    description_html(soup)
    assert str(soup) == before


def test_description_html_missing_or_empty():
    assert description_html(None) == ''
    assert description_html(BeautifulSoup('<html><body><p>no job here</p></body></html>', 'html.parser')) == ''


def test_parse_description_html_matches_live_page_for_every_fixture():
    paths = sorted(glob.glob(os.path.join(FIXTURES, "job_*.html")))
    assert paths
    for path in paths:
        soup = load(path)
        live, stored = parse_job_details(soup), parse_description_html(description_html(soup))
        for field in SECTION_FIELDS + ('sections_parsed',):
            assert live[field] == stored[field], (path, field)


def test_parse_description_html_empty():
    assert parse_description_html('') == {**{field: '' for field in SECTION_FIELDS}, 'sections_parsed': 0}
    assert parse_description_html(None)['sections_parsed'] == 0


def test_preview_and_report():
    result = preview(fixture_soup())
    assert result['same'] and result['bytes'] == len(result['html'].encode('utf-8'))
    assert result['tags']['li'] == 16 and 'button' not in result['tags']
    text = report(result)
    assert 'company: CODEplus | industry: IT Services and IT Consulting' in text
    assert 'the same sections as the live page' in text
    assert '<html>' not in report(result, pretty=True) and '\n' in report(result, pretty=True)


def test_write_page_creates_viewable_file():
    path = write_page(preview(fixture_soup()), URL, tempfile.mkdtemp())
    assert path.endswith(f"job_{JOB_ID}.html")
    with open(path, encoding='utf-8') as f:
        page = f.read()
    assert page.startswith('<!doctype html>') and URL in page and '<li>' in page


def test_main_with_stubbed_fetch():
    original = preview_job_html.get_with_retry
    try:
        preview_job_html.get_with_retry = lambda url, config: fixture_soup() if url == URL else None
        out_dir = tempfile.mkdtemp()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            assert main([URL + "?trackingId=x", "--config", CONFIG, "--out", out_dir]) == 0
        assert '=== job_description_html ===' in buffer.getvalue()
        assert os.path.exists(os.path.join(out_dir, f"job_{JOB_ID}.html"))

        preview_job_html.get_with_retry = lambda url, config: None
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            assert main([JOB_ID, "--config", CONFIG]) == 1
            assert main(["not-a-link", "--config", CONFIG]) == 1
            assert main([JOB_ID, "--config", "/nonexistent.json"]) == 1
        assert buffer.getvalue().count("Error:") == 3
    finally:
        preview_job_html.get_with_retry = original


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"{len(tests)} tests passed")
