import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)  # app.server reads config.json from the working directory at import

import app.server as server
import db.queries as db
from db import schema


def raises(exc, fn):
    try:
        fn()
    except exc as e:
        return e
    raise AssertionError(f"{exc.__name__} not raised")


def client_with_db(table='jobs'):
    # A test client whose app reads a temporary database with two jobs in the given table
    path = os.path.join(tempfile.mkdtemp(), 'jobs.db')
    conn = sqlite3.connect(path)
    conn.execute(f'CREATE TABLE {table} (id INTEGER PRIMARY KEY, title TEXT, company TEXT, location TEXT, date TEXT, '
                 'job_url TEXT, job_description TEXT, applied INTEGER, hidden INTEGER, interview INTEGER, rejected INTEGER)')
    conn.executemany(f'INSERT INTO {table} VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0)', [
        (1, 'Dev <b>', 'A', 'LA', '2026-10-11', 'https://www.linkedin.com/jobs/view/1/', 'Build things'),
        (2, 'Eng', 'B', 'NY', '2026-10-10', 'https://www.linkedin.com/jobs/view/2/', 'Run things')])
    schema.init_db(conn, table)
    conn.commit()
    conn.close()
    server.config = dict(server.config, db_path=path, jobs_tablename=table)
    return server.app.test_client(), path


def flags(path, job_id):
    conn = sqlite3.connect(path)
    row = conn.execute('SELECT applied, hidden, interview, rejected FROM jobs WHERE id = ?', (job_id,)).fetchone()
    conn.close()
    return dict(zip(db.JOB_FLAGS, row))


def test_job_flags_validates_the_body():
    assert server.job_flags({'applied': 1, 'hidden': False}) == {'applied': 1, 'hidden': 0}
    for bad in (None, [], {}, {'title': 'x'}, {'applied': 2}, {'applied': 'yes'}, {'applied': 1.0}):
        raises(ValueError, lambda: server.job_flags(bad))


def test_patch_sets_only_the_given_flag():
    client, path = client_with_db()
    response = client.patch('/jobs/1', json={'applied': 1})
    assert response.status_code == 200 and response.get_json() == {'id': 1, 'updated': {'applied': 1}}
    # the old routes sent every status to hide_job; applied must not hide the job
    assert flags(path, 1) == {'applied': 1, 'hidden': 0, 'interview': 0, 'rejected': 0}
    client.patch('/jobs/1', json={'interview': 1, 'applied': 0})
    assert flags(path, 1) == {'applied': 0, 'hidden': 0, 'interview': 1, 'rejected': 0}
    assert flags(path, 2) == {'applied': 0, 'hidden': 0, 'interview': 0, 'rejected': 0}


def test_patch_hide_removes_the_job_from_the_list():
    client, _ = client_with_db()
    assert client.patch('/jobs/1', json={'hidden': 1}).status_code == 200
    assert [job['id'] for job in client.get('/jobs').get_json()] == [2]


def test_patch_rejects_bad_requests():
    client, path = client_with_db()
    assert client.patch('/jobs/1', json={'title': 'x'}).status_code == 400
    assert client.patch('/jobs/1', json={'hidden': 5}).status_code == 400
    assert client.patch('/jobs/1', data='not json', content_type='text/plain').status_code == 400
    assert client.patch('/jobs/99', json={'hidden': 1}).status_code == 404
    assert client.post('/jobs/1', json={'hidden': 1}).status_code == 405
    assert flags(path, 1)['hidden'] == 0


def test_job_details_and_job_page():
    client, _ = client_with_db()
    details = client.get('/jobs/1/details')
    assert details.status_code == 200 and details.get_json()['title'] == 'Dev <b>'
    assert 'match' in details.get_json()
    assert client.get('/jobs/99/details').status_code == 404
    assert client.get('/jobs/2').status_code == 200  # looked up by id, not list position
    assert client.get('/jobs/99').status_code == 404


def test_generation_routes_return_404_for_unknown_jobs_before_calling_openai():
    client, _ = client_with_db()
    assert client.post('/jobs/99/cover-letter').status_code == 404
    assert client.post('/jobs/99/resume').status_code == 404
    assert client.get('/jobs/99/cover-letter').status_code == 404
    assert client.get('/jobs/1/cover-letter').get_json() == {'cover_letter': None}


def test_update_job_flags_rejects_unknown_columns():
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE jobs (id INTEGER PRIMARY KEY, applied INTEGER, hidden INTEGER)')
    conn.execute('INSERT INTO jobs VALUES (1, 0, 0)')
    assert db.update_job_flags(conn, 'jobs', 1, {'hidden': 1}) is True
    assert db.update_job_flags(conn, 'jobs', 2, {'hidden': 1}) is False
    raises(ValueError, lambda: db.update_job_flags(conn, 'jobs', 1, {'title = 1; --': 1}))
    raises(ValueError, lambda: db.update_job_flags(conn, 'jobs', 1, {}))


def test_app_uses_the_configured_table_name():
    client, path = client_with_db('my_jobs')
    assert [job['id'] for job in client.get('/jobs').get_json()] == [1, 2]
    assert client.get('/jobs/2/details').get_json()['title'] == 'Eng'
    assert client.patch('/jobs/2', json={'rejected': 1}).status_code == 200
    conn = sqlite3.connect(path)
    assert conn.execute('SELECT rejected FROM my_jobs WHERE id = 2').fetchone() == (1,)
    conn.close()


def test_jobs_table_rejects_names_that_are_not_identifiers():
    saved = server.config
    try:
        for bad in ('jobs; DROP TABLE jobs', 'jobs"', '1jobs', '', 7):
            server.config = dict(saved, jobs_tablename=bad)
            raises(ValueError, server.jobs_table)
        server.config = {k: v for k, v in saved.items() if k != 'jobs_tablename'}
        assert server.jobs_table() == 'jobs'
    finally:
        server.config = saved


def test_read_job_and_save_generated_text():
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE t (id INTEGER PRIMARY KEY, title TEXT, cover_letter TEXT, resume TEXT)')
    conn.execute("INSERT INTO t (id, title) VALUES (1, 'Dev')")
    assert db.read_job(conn, 't', 1) == {'id': 1, 'title': 'Dev', 'cover_letter': None, 'resume': None}
    assert db.read_job(conn, 't', 1, ['title']) == {'title': 'Dev'}
    assert db.read_job(conn, 't', 2) is None
    db.save_generated_text(conn, 't', 1, 'cover_letter', 'Dear team')
    assert db.read_job(conn, 't', 1, ['cover_letter']) == {'cover_letter': 'Dear team'}
    raises(ValueError, lambda: db.save_generated_text(conn, 't', 1, 'title', 'x'))


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_") and callable(fn)]
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"{len(tests)} passed")
