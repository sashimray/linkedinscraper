import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pandas as pd
from bs4 import BeautifulSoup

from job_parser import SECTION_FIELDS, description_html, parse_description_html, parse_job_details
from main import apply_job_page, save_jobs

FIXTURES = os.path.join(ROOT, "tests", "fixtures")
JOB_ID = "4476740047"


def fixture_soup(job_id=JOB_ID):
    with open(os.path.join(FIXTURES, f"job_{job_id}.html"), encoding="utf-8") as f:
        return BeautifulSoup(f.read(), "html.parser")


def card(company=''):
    return {'title': 'Engineer', 'company': company, 'job_posting_id': JOB_ID, 'job_description': ''}


def test_apply_job_page_saves_description_html():
    job = card()
    expected = description_html(fixture_soup())
    apply_job_page(job, fixture_soup())
    assert job['job_description_html'] == expected and expected
    assert 'class=' not in job['job_description_html']


def test_apply_job_page_html_parses_like_the_live_page():
    job = card()
    apply_job_page(job, fixture_soup())
    stored = parse_description_html(job['job_description_html'])
    live = parse_job_details(fixture_soup())
    for field in SECTION_FIELDS:
        assert stored[field] == live[field], field


def test_apply_job_page_still_fills_text_description_and_company():
    job = card()
    assert apply_job_page(job, fixture_soup()) is None  # sections are split later, by the parse step
    assert job['job_description'] and job['job_description'] != "Could not find Job Description"
    assert job['company'] and job['company_industry']


def test_apply_job_page_keeps_company_from_the_card():
    job = card(company='From Card')
    apply_job_page(job, fixture_soup())
    assert job['company'] == 'From Card'


def test_apply_job_page_handles_failed_fetch():
    job = card()
    apply_job_page(job, None)
    assert job['job_description_html'] == '' and job['company_industry'] == ''
    assert job['job_description'] == "Could not find Job Description"


def test_apply_job_page_page_without_description():
    job = card()
    apply_job_page(job, BeautifulSoup('<html><body><p>nothing</p></body></html>', 'html.parser'))
    assert job['job_description_html'] == ''
    assert job['job_description'] == "Could not find Job Description"


def test_save_jobs_creates_then_extends_the_table():
    conn = sqlite3.connect(':memory:')
    assert save_jobs(conn, pd.DataFrame([{'job_posting_id': '1', 'title': 'old', 'job_description': 'text'}]), 'jobs') == 1
    assert save_jobs(conn, pd.DataFrame([{'job_posting_id': '2', 'title': 'new', 'job_description': 'text',
                                          'job_description_html': '<ul><li>Python</li></ul>'}]), 'jobs') == 1
    rows = conn.execute('SELECT job_posting_id, job_description_html FROM jobs ORDER BY job_posting_id').fetchall()
    assert rows == [('1', None), ('2', '<ul><li>Python</li></ul>')]
    assert save_jobs(conn, pd.DataFrame([{'job_posting_id': '2', 'title': 'again'}]), 'jobs') == 0  # already there


def test_save_jobs_skips_empty_frames():
    conn = sqlite3.connect(':memory:')
    assert save_jobs(conn, pd.DataFrame(), 'filtered_jobs') == 0
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'filtered_jobs'").fetchone() is None


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_") and callable(fn)]
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"{len(tests)} passed")
