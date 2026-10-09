import json
import os
import sqlite3
import sys
import tempfile
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import db
import embedder
from workflow import STEPS, parse_missing_details, run_workflow, selected_steps

DESCRIPTION = "About the role\nRequirements:\n- 5+ years of Python\n- Kubernetes experience\nWhat you'll do:\n- Build services"


def raises(exc, fn):
    try:
        fn()
    except exc as e:
        return e
    raise AssertionError(f"{exc.__name__} not raised")


def make_db():
    path = os.path.join(tempfile.mkdtemp(), 'jobs.db')
    con = sqlite3.connect(path)
    con.execute('CREATE TABLE jobs (id INTEGER PRIMARY KEY, title TEXT, company TEXT, hidden INTEGER, job_description TEXT)')
    con.executemany('INSERT INTO jobs VALUES (?, ?, ?, ?, ?)', [
        (1, 'a', 'A', 0, DESCRIPTION), (2, 'b', 'B', 1, DESCRIPTION), (3, 'c', 'C', 0, 'Could not find Job Description'),
        (4, 'd', 'D', 0, DESCRIPTION)])
    db.create_job_details_table(con, 'jobs')
    con.execute("INSERT INTO job_details (job_id, requirements, sections_parsed) VALUES (4, '- Go', 2)")
    con.execute("INSERT INTO jobs VALUES (5, 'e', 'E', 0, NULL)")
    con.execute("INSERT INTO job_details (job_id, requirements, sections_parsed) "
                "VALUES (5, '- Python\n- Active TS/SCI clearance required', 1)")
    con.commit()
    con.close()
    return {'db_path': path, 'jobs_tablename': 'jobs'}


def test_selected_steps_keeps_pipeline_order():
    assert selected_steps() == list(STEPS)
    assert selected_steps(only=['score_embed', 'embed']) == ['embed', 'score_embed']
    assert list(STEPS) == ['scrape', 'parse', 'hide', 'embed', 'score', 'score_embed']
    assert selected_steps(skip=['scrape', 'parse']) == ['hide', 'embed', 'score', 'score_embed']
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


def test_read_jobs_without_details_and_parse_missing_details():
    config = make_db()
    con = sqlite3.connect(config['db_path'])
    assert [job_id for job_id, _ in db.read_jobs_without_details(con, 'jobs')] == [1]  # 5 has a row
    assert parse_missing_details(config) == 1
    assert db.read_jobs_without_details(con, 'jobs') == []
    requirements, parsed = con.execute('SELECT requirements, sections_parsed FROM job_details WHERE job_id = 1').fetchone()
    assert 'Python' in requirements and parsed == 1
    # the LLM-filled row is left alone
    assert con.execute('SELECT requirements FROM job_details WHERE job_id = 4').fetchone() == ('- Go',)
    assert parse_missing_details(config) == 0


def test_embedder_run_with_fake_model():
    config = make_db()
    parse_missing_details(config)
    fd, skills_path = tempfile.mkstemp(suffix='.json')
    with os.fdopen(fd, 'w') as f:
        json.dump({'skills': [{'name': 'Python', 'type': 'tech', 'aliases': [], 'weight': 3},
                              {'name': 'Kubernetes', 'type': 'tech', 'aliases': [], 'weight': 3}]}, f)
    stats = embedder.run(config['db_path'], skills_path, model='fake')
    assert stats['jobs'] == 3 and stats['alias'] == 3 and stats['embedded'] == stats['lines']
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
    assert con.execute('SELECT ats_score_embed FROM jobs WHERE id = 5').fetchone() == (0,)  # and not scored
    scores = dict(con.execute('SELECT id, ats_score_embed FROM jobs WHERE hidden = 0 AND id IN (1, 4)').fetchall())
    assert scores[1] > 0 and scores[4] == 0  # job 4 only asks for Go
    assert con.execute('SELECT COUNT(*) FROM jobs WHERE ats_score > 0').fetchone()[0] >= 1


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"{len(tests)} tests passed")
