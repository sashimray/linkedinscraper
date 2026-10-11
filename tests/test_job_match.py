import json
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ats_score
import ats_score_embed
import db
import schema
from embedder import load_skills
from schema import EMBEDDER_SCHEMA as SCHEMA
from job_match import job_match, load_profiles
from test_ats_score import RESUME

SKILLS = {'skills': [{'name': 'Python', 'type': 'tech', 'aliases': [], 'weight': 3},
                     {'name': 'distributed systems', 'type': 'concept', 'aliases': [], 'weight': 2}]}


def raises(exc, fn):
    try:
        fn()
    except exc as e:
        return e
    raise AssertionError(f"{exc.__name__} not raised")


def write_json(data):
    fd, path = tempfile.mkstemp(suffix='.json')
    with os.fdopen(fd, 'w') as f:
        json.dump(data, f)
    return path


def make_conn():
    conn = sqlite3.connect(':memory:')
    conn.executescript(SCHEMA)
    conn.execute('CREATE TABLE jobs (id INTEGER PRIMARY KEY, title TEXT, hidden INTEGER, ats_score INT, ats_score_embed INT)')
    schema.create_job_details_table(conn, 'jobs')
    return conn


def test_rule_job_keywords_lists_matches_and_gaps():
    profile = ats_score.load_resume_profile(RESUME)
    result = ats_score.job_keywords('- Python and Rust\n- 15+ years of experience', '- PhD in physics', profile)
    assert 'python' in result['matched']
    assert 'rust' in result['gaps'] and '15+ years' in result['gaps'] and 'PhD' in result['gaps']
    assert not set(result['matched']) & set(result['gaps'])
    assert ats_score.job_keywords(None, None, profile) == {'matched': [], 'gaps': []}


def test_embed_job_keywords_lists_matches_and_gaps():
    profile = ats_score_embed.skill_profile(SKILLS['skills'])
    lines = [('requirement', 'Python and Rust', [('Python', 'alias', 1.0)]),
             ('requirement', 'Scale distributed services', [('distributed systems', 'embed', 0.85)]),
             ('requirement', 'Experience in low earth orbit satellites', []),
             ('preference', 'Great communicator', []),
             ('preference', 'Weak', [('distributed systems', 'embed', 0.70)])]
    result = ats_score_embed.job_keywords(lines, profile)
    assert result['matched'] == ['Python', 'distributed systems']
    assert result['gaps'] == ['rust', 'low', 'earth', 'orbit', 'satellites']


def test_read_ranked_jobs_latest_date_first_then_average_score():
    conn = make_conn()
    conn.execute('ALTER TABLE jobs ADD COLUMN date TEXT')
    conn.executemany('INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?)', [
        (1, 'a', 0, 50, 90, '2026-10-08'),    # avg 70
        (2, 'b', 0, 100, 0, '2026-10-08'),    # avg 50, though its best single score is higher
        (3, 'c', 1, 100, 100, '2026-10-09'),  # hidden
        (4, 'd', None, 20, 30, '2026-10-09'), # latest day wins over a better score
        (5, 'e', 0, None, None, '2026-10-08'),
        (6, 'f', 0, 60, 80, '2026-10-08'),    # avg 70, ties with 1 -> newer id first
        (7, 'g', 0, 99, 99, None)])           # no date sorts last
    assert [job['id'] for job in db.read_ranked_jobs(conn, 'jobs')] == [4, 6, 1, 2, 5, 7]


def test_read_ranked_jobs_on_old_table_after_init_db():
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE jobs (id INTEGER PRIMARY KEY, hidden INTEGER, date TEXT)')
    conn.execute("INSERT INTO jobs VALUES (1, 0, '2026-10-09')")
    schema.init_db(conn, 'jobs')
    job, = db.read_ranked_jobs(conn, 'jobs')
    assert (job['id'], job['ats_score'], job['ats_score_embed']) == (1, 0, 0)


def test_single_job_readers_handle_missing_tables():
    conn = sqlite3.connect(':memory:')
    assert db.read_job_sections(conn, 1) is None
    assert db.read_job_item_matches(conn, 1) == []


def test_load_skills_raises_value_error():
    assert 'skills file' in str(raises(ValueError, lambda: load_skills('/nonexistent/skills.json')))
    assert 'skills file' in str(raises(ValueError, lambda: load_skills(write_json({'other': []}))))


def test_load_profiles_reports_errors_instead_of_failing():
    profiles = load_profiles('/nonexistent/resume.json', '/nonexistent/skills.json')
    assert profiles['rule'] is None and profiles['embed'] is None and len(profiles['errors']) == 2
    profiles = load_profiles(write_json(RESUME), write_json(SKILLS))
    assert profiles['rule'] is not None and profiles['embed']['weights'] == {'Python': 3.0, 'distributed systems': 2.0}
    assert profiles['errors'] == []


def test_job_match_combines_both_summaries():
    conn = make_conn()
    conn.execute("INSERT INTO jobs VALUES (1, 'a', 0, 0, 0)")
    conn.execute("INSERT INTO job_details (job_id, requirements, preferences) VALUES (1, '- Python and Rust', NULL)")
    conn.execute("INSERT INTO job_items (id, job_id, kind, idx, text, text_hash) VALUES (1, 1, 'requirement', 0, 'Python and Rust', 'h')")
    conn.execute("INSERT INTO matches VALUES (1, 'Python', 'alias', 1.0)")
    profiles = load_profiles(write_json(RESUME), write_json(SKILLS))
    result = job_match(conn, 1, profiles)
    assert 'python' in result['rule']['matched'] and 'rust' in result['rule']['gaps']
    assert result['embed'] == {'matched': ['Python'], 'gaps': ['rust']}
    assert job_match(conn, 2, profiles) == {'rule': None, 'embed': None}
    assert job_match(conn, 1, {'rule': None, 'embed': None}) == {'rule': None, 'embed': None}


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"{len(tests)} tests passed")
