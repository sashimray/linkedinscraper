import json
import os
import sqlite3
import sys
import tempfile
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import db.queries as db
import db.schema as schema
import workflow.score.embedder as embedder
from workflow.parse import parse_job_sections, parse_missing_details
from workflow.pipeline import STEPS, run_workflow, selected_steps

DESCRIPTION = "About the role\nRequirements:\n- 5+ years of Python\n- Kubernetes experience\nWhat you'll do:\n- Build services"


def raises(exc, fn):
    try:
        fn()
    except exc as e:
        return e
    raise AssertionError(f"{exc.__name__} not raised")


CLEARANCE_DESCRIPTION = "Requirements:\n- Python\n- Active TS/SCI clearance required\nWhat you'll do:\n- Build services"
HTML = ("<strong>What you'll do:</strong><ul><li>Build services</li></ul>"
        "<strong>Requirements:</strong><ul><li>Rust experience</li></ul>")


def make_db():
    # 1 visible, text only; 2 hidden; 3 no description; 4 has an LLM-filled row; 5 needs a clearance; 6 has stored HTML
    path = os.path.join(tempfile.mkdtemp(), 'jobs.db')
    con = sqlite3.connect(path)
    con.execute('CREATE TABLE jobs (id INTEGER PRIMARY KEY, title TEXT, company TEXT, hidden INTEGER, '
                'job_description TEXT, job_description_html TEXT)')
    con.executemany('INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?)', [
        (1, 'a', 'A', 0, DESCRIPTION, None), (2, 'b', 'B', 1, DESCRIPTION, None),
        (3, 'c', 'C', 0, 'Could not find Job Description', None), (4, 'd', 'D', 0, DESCRIPTION, None),
        (5, 'e', 'E', 0, CLEARANCE_DESCRIPTION, None), (6, 'f', 'F', 0, 'text the parser ignores', HTML)])
    schema.create_job_details_table(con, 'jobs')
    con.execute("INSERT INTO job_details (job_id, requirements, sections_parsed) VALUES (4, '- Go', 2)")
    con.commit()
    con.close()
    return {'db_path': path, 'jobs_tablename': 'jobs'}


def test_selected_steps_keeps_pipeline_order():
    assert selected_steps() == list(STEPS)
    assert selected_steps(only=['score_embed', 'embed']) == ['embed', 'score_embed']
    assert list(STEPS) == ['scrape', 'hide', 'parse', 'score', 'embed', 'score_embed']
    assert selected_steps(skip=['scrape', 'hide']) == ['parse', 'score', 'embed', 'score_embed']
    assert 'nope' in str(raises(ValueError, lambda: selected_steps(only=['nope'])))
    raises(ValueError, lambda: selected_steps(skip=['nope']))


def test_run_workflow_stops_at_first_failure():
    calls = []

    def ok(name):
        return lambda config, options: calls.append(name)

    def fail(config, options):
        calls.append('b')
        raise RuntimeError('boom')

    funcs = {'a': ok('a'), 'b': fail, 'c': ok('c')}
    results = run_workflow({}, None, ['a', 'b', 'c'], funcs)
    assert calls == ['a', 'b']
    assert [(name, ok) for name, ok, _ in results] == [('a', True), ('b', False)]


def test_run_workflow_catches_system_exit():
    def exits(config, options):
        sys.exit('bad input')
    assert [ok for _, ok, _ in run_workflow({}, None, ['x'], {'x': exits})] == [False]


def test_parse_job_sections_prefers_html():
    from_html = parse_job_sections('text the parser ignores', HTML)
    assert from_html['requirements'] == '- Rust experience' and from_html['sections_parsed'] == 1
    from_text = parse_job_sections(DESCRIPTION, None)
    assert 'Python' in from_text['requirements'] and from_text['sections_parsed'] == 1
    assert parse_job_sections(DESCRIPTION, '') == from_text


def test_read_jobs_without_details_and_parse_missing_details():
    config = make_db()
    con = sqlite3.connect(config['db_path'])
    rows = db.read_jobs_without_details(con, 'jobs')
    assert [row[0] for row in rows] == [1, 5, 6]  # 2 hidden, 3 no description, 4 has a row
    assert rows[2][2] == HTML
    assert parse_missing_details(config) == 3
    assert db.read_jobs_without_details(con, 'jobs') == []
    details = {job_id: (req, parsed) for job_id, req, parsed in
               con.execute('SELECT job_id, requirements, sections_parsed FROM job_details')}
    assert 'Python' in details[1][0] and details[1][1] == 1
    assert details[6] == ('- Rust experience', 1)  # from the HTML, not the text
    assert details[4] == ('- Go', 2)  # the LLM-filled row is left alone
    assert parse_missing_details(config) == 0


def test_embedder_run_with_fake_model():
    config = make_db()
    parse_missing_details(config)
    fd, skills_path = tempfile.mkstemp(suffix='.json')
    with os.fdopen(fd, 'w') as f:
        json.dump({'skills': [{'name': 'Python', 'type': 'tech', 'aliases': [], 'weight': 3},
                              {'name': 'Kubernetes', 'type': 'tech', 'aliases': [], 'weight': 3}]}, f)
    stats = embedder.run(config['db_path'], skills_path, model='fake')
    assert stats['jobs'] == 4 and stats['alias'] == 3  # jobs 1, 4, 5, 6 (no hide step here) and stats['embedded'] == stats['lines']
    assert embedder.run(config['db_path'], skills_path, model='fake')['embedded'] == 0
    raises(ValueError, lambda: embedder.run(config['db_path'], '/nonexistent.json', model='fake'))


def test_workflow_steps_run_end_to_end_without_scrape():
    config = make_db()
    fd, skills_path = tempfile.mkstemp(suffix='.json')
    with os.fdopen(fd, 'w') as f:
        json.dump({'skills': [{'name': 'Python', 'type': 'tech', 'aliases': [], 'weight': 3}]}, f)
    from test_ats_score import RESUME
    fd, resume_path = tempfile.mkstemp(suffix='.json')
    with os.fdopen(fd, 'w') as f:
        json.dump(RESUME, f)
    options = SimpleNamespace(config=None, llm=None, llm_model=None, model='fake', skills=skills_path, resume=resume_path)
    results = run_workflow(config, options, selected_steps(skip=['scrape']))
    assert [ok for _, ok, _ in results] == [True] * 5
    con = sqlite3.connect(config['db_path'])
    assert con.execute('SELECT hidden FROM jobs WHERE id = 5').fetchone() == (1,)  # clearance job hidden
    # hidden before the parse step, so it is neither parsed nor scored
    assert con.execute('SELECT COUNT(*) FROM job_details WHERE job_id = 5').fetchone() == (0,)
    assert con.execute('SELECT ats_score, ats_score_embed, overall_score FROM jobs WHERE id = 5').fetchone() == (None, None, None)
    scores = {job_id: (ats, embed, overall) for job_id, ats, embed, overall in
              con.execute('SELECT id, ats_score, ats_score_embed, overall_score FROM jobs WHERE id IN (1, 4, 6)')}
    assert scores[1][1] > 0 and scores[1][2] == round((scores[1][0] + scores[1][1]) / 2)
    assert scores[4] == (None, None, None)  # its only line ('Go') isn't a skill the scorers know: not scored
    assert scores[6] == (0, 0, 0)           # asks only for Rust: scored, and 0
    assert con.execute('SELECT COUNT(*) FROM jobs WHERE ats_score > 0').fetchone()[0] >= 1


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"{len(tests)} tests passed")
