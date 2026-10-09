import json
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import db
from ats_score_embed import (EMBED_FULL, MISSING_DOMAIN_WEIGHT, MISSING_TECH_WEIGHT, explain, group_lines, line_credit, match_credit,
                             score_job, score_jobs, section_score, skill_profile, alias_coverage)
from embedder import DEFAULT_THRESHOLD, SCHEMA

PROFILE = skill_profile([{'name': 'Python', 'weight': 3}, {'name': 'Docker', 'aliases': ['containerization'], 'weight': 3},
                         {'name': 'distributed systems', 'weight': 2}])


def raises(exc, fn):
    try:
        fn()
    except exc as e:
        return e
    raise AssertionError(f"{exc.__name__} not raised")


def test_match_credit():
    assert match_credit('alias', 1.0) == 1.0
    assert match_credit('embed', DEFAULT_THRESHOLD - 0.01) is None
    assert match_credit('embed', DEFAULT_THRESHOLD) == 0.0
    assert abs(match_credit('embed', (DEFAULT_THRESHOLD + EMBED_FULL) / 2) - 0.5) < 1e-9
    assert match_credit('embed', 0.95) == 1.0


def test_line_credit_cases():
    assert line_credit('Python', [('Python', 'alias', 1.0)], PROFILE) == (3, 1.0)
    # best match wins; its weight is used
    assert line_credit('x', [('distributed systems', 'embed', 0.83), ('Python', 'alias', 1.0)], PROFILE) == (3, 1.0)
    assert line_credit('Experience with Rust', [], PROFILE) == (MISSING_TECH_WEIGHT, 0.0)  # tech you lack
    assert line_credit('Experience in low earth orbit satellites', [], PROFILE) == (MISSING_DOMAIN_WEIGHT, 0.0)
    assert line_credit('Strong communicator', [], PROFILE) is None
    assert line_credit("5+ years and a Bachelor's degree in Computer Science", [], PROFILE) is None
    assert line_credit('5+ years of Rust', [], PROFILE) == (MISSING_TECH_WEIGHT, 0.0)
    # several tech skills on one line: partial credit for the alias match
    assert line_credit('Rust and Docker', [('Docker', 'alias', 1.0)], PROFILE) == (3, 0.5)
    assert line_credit('Excellent written and verbal communication skills', [], PROFILE) is None
    # an embedding match below the floor doesn't count, so the soft line is still skipped
    assert line_credit('Team player', [('distributed systems', 'embed', 0.70)], PROFILE) is None


def test_section_score_is_weighted():
    assert section_score([]) is None
    assert section_score([(3, 1.0), (1, 0.0)]) == 0.75


def test_score_job_combines_sections():
    req = [('requirement', 'Python', [('Python', 'alias', 1.0)]), ('requirement', 'Rust', [])]
    pref = [('preference', 'Kafka', [])]
    assert score_job(req, PROFILE) == 50
    assert score_job(req + pref, PROFILE) == round((0.85 * 0.5) * 100)
    assert score_job(pref, PROFILE) == 0
    assert score_job([('requirement', 'Be kind', [])], PROFILE) is None


def test_group_lines_collects_matches_per_line():
    rows = [(1, 'requirement', 0, 'Python', 'Python', 'alias', 1.0),
            (1, 'requirement', 0, 'Python', 'distributed systems', 'embed', 0.8),
            (1, 'preference', 0, 'Kind', None, None, None),
            (2, 'requirement', 0, 'Go', None, None, None)]
    assert group_lines(rows) == {
        1: [('requirement', 'Python', [('Python', 'alias', 1.0), ('distributed systems', 'embed', 0.8)]),
            ('preference', 'Kind', [])],
        2: [('requirement', 'Go', [])]}


def test_skill_profile_validates_entries():
    assert skill_profile([{'name': 'A', 'weight': 2}, {'name': 'B', 'aliases': ['Bee']}]) == {
        'weights': {'A': 2.0, 'B': float(MISSING_TECH_WEIGHT)}, 'terms': {'a', 'b', 'bee'}}
    raises(ValueError, lambda: skill_profile([{'weight': 2}]))
    raises(ValueError, lambda: skill_profile([{'name': 'A', 'aliases': [1]}]))


def test_alias_coverage_counts_tech_you_lack():
    terms = PROFILE['terms']
    assert alias_coverage('Rust, distributed systems and Docker', 2, terms) == 2 / 3
    assert alias_coverage('Python or Rust', 1, terms) == 1.0
    assert alias_coverage('Docker containerization', 1, terms) == 1.0


def test_explain_marks_skipped_and_missing():
    out = explain([('requirement', 'Rust', []), ('requirement', 'Be kind', [])], PROFILE)
    assert '0.00  Rust' in out and 'missed' in out and '  -   Be kind' in out


def test_score_jobs_saves_visible_jobs_only():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, 'jobs.db')
    skills_path = os.path.join(tmp, 'skills.json')
    with open(skills_path, 'w') as f:
        json.dump({'skills': [{'name': 'Python', 'type': 'tech', 'weight': 3}]}, f)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.execute('CREATE TABLE jobs (id INTEGER PRIMARY KEY, hidden INTEGER)')
    con.executemany('INSERT INTO jobs VALUES (?, ?)', [(1, 0), (2, 1)])
    con.executemany('INSERT INTO job_items (id, job_id, kind, idx, text, text_hash) VALUES (?, ?, ?, ?, ?, ?)',
                    [(1, 1, 'requirement', 0, 'Python', 'h'), (2, 1, 'requirement', 1, 'Rust', 'h'),
                     (3, 2, 'requirement', 0, 'Python', 'h')])
    con.executemany('INSERT INTO matches VALUES (?, ?, ?, ?)', [(1, 'Python', 'alias', 1.0), (3, 'Python', 'alias', 1.0)])
    con.commit()
    config = {'db_path': path, 'jobs_tablename': 'jobs'}
    assert score_jobs(config, skills_path, dry_run=True) == {1: 50}
    assert 'ats_score_embed' not in [r[1] for r in con.execute('PRAGMA table_info(jobs)')]
    score_jobs(config, skills_path)
    assert con.execute('SELECT id, ats_score_embed FROM jobs ORDER BY id').fetchall() == [(1, 50), (2, 0)]
    con.close()


def test_score_jobs_without_embedder_tables():
    tmp = tempfile.mkdtemp()
    skills_path = os.path.join(tmp, 'skills.json')
    with open(skills_path, 'w') as f:
        json.dump({'skills': []}, f)
    path = os.path.join(tmp, 'jobs.db')
    sqlite3.connect(path).execute('CREATE TABLE jobs (id INTEGER PRIMARY KEY, hidden INTEGER)')
    e = raises(ValueError, lambda: score_jobs({'db_path': path, 'jobs_tablename': 'jobs'}, skills_path))
    assert 'embedder.py' in str(e)


def test_update_ats_scores_custom_column():
    con = sqlite3.connect(':memory:')
    con.execute('CREATE TABLE jobs (id INTEGER PRIMARY KEY)')
    con.execute('INSERT INTO jobs VALUES (1)')
    db.update_ats_scores(con, 'jobs', {1: 70}, 'other_score')
    assert con.execute('SELECT other_score FROM jobs').fetchone() == (70,)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"{len(tests)} tests passed")
