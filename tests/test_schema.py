import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pandas as pd

import schema


def tables(conn):
    return {name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def test_table_exists_and_columns():
    conn = sqlite3.connect(':memory:')
    assert not schema.table_exists(conn, 'jobs')
    conn.execute('CREATE TABLE jobs (id INTEGER PRIMARY KEY, title TEXT)')
    assert schema.table_exists(conn, 'jobs')
    assert not schema.table_exists(conn, "jobs' OR '1'='1")  # the name is a parameter, not SQL
    assert schema.table_columns(conn, 'jobs') == ['id', 'title']


def test_add_columns_only_adds_missing():
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT)')
    schema.add_columns(conn, 't', {'a': 'TEXT', 'b': 'INT DEFAULT 0'})
    schema.add_columns(conn, 't', {'b': 'INT DEFAULT 0'})
    assert schema.table_columns(conn, 't') == ['id', 'a', 'b']


def test_init_db_creates_side_tables_without_jobs_table():
    conn = sqlite3.connect(':memory:')
    schema.init_db(conn, 'jobs')
    assert tables(conn) >= {schema.JOB_DETAILS, schema.JOB_ITEMS, schema.SKILL_VECTORS, schema.MATCHES}
    assert 'jobs' not in tables(conn)  # created by the first scrape, from its columns


def test_init_db_migrates_old_tables_and_is_idempotent():
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE jobs (id INTEGER PRIMARY KEY, title TEXT, hidden INTEGER)')
    conn.execute('CREATE TABLE job_details (id INTEGER PRIMARY KEY, job_id INTEGER UNIQUE, requirements TEXT)')
    schema.init_db(conn, 'jobs')
    schema.init_db(conn, 'jobs')
    assert set(schema.JOBS_EXTRA_COLUMNS) <= set(schema.table_columns(conn, 'jobs'))
    assert 'sections_parsed' in schema.table_columns(conn, schema.JOB_DETAILS)
    conn.execute("INSERT INTO jobs (id, title) VALUES (1, 'a')")
    assert conn.execute('SELECT ats_score, ats_score_embed FROM jobs').fetchone() == (0, 0)


def test_prepare_jobs_table_creates_with_index_then_adds_columns():
    conn = sqlite3.connect(':memory:')
    assert not schema.prepare_jobs_table(conn, pd.DataFrame(), 'jobs')
    assert not schema.table_exists(conn, 'jobs')
    df = pd.DataFrame([{'job_posting_id': '1', 'title': 'a', 'applied': 0}])
    assert schema.prepare_jobs_table(conn, df, 'jobs')
    assert schema.table_columns(conn, 'jobs') == ['id', 'job_posting_id', 'title', 'applied']
    indexes = [row[1] for row in conn.execute('PRAGMA index_list(jobs)')]
    assert 'idx_jobs_job_posting_id' in indexes
    schema.prepare_jobs_table(conn, pd.DataFrame([{'job_posting_id': '2', 'company_industry': 'x'}]), 'jobs')
    assert 'company_industry' in schema.table_columns(conn, 'jobs')


def test_create_job_posting_id_index_backfills_from_url():
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE jobs (id INTEGER PRIMARY KEY, job_url TEXT)')
    conn.execute("INSERT INTO jobs (job_url) VALUES ('https://www.linkedin.com/jobs/view/123/')")
    schema.create_job_posting_id_index(conn, 'jobs')
    assert conn.execute('SELECT job_posting_id FROM jobs').fetchone() == ('123',)


def test_sqlite_type():
    df = pd.DataFrame([{'i': 1, 'f': 1.5, 's': 'x', 'b': True}])
    assert [schema.sqlite_type(df, c) for c in 'ifsb'] == ['INTEGER', 'REAL', 'TEXT', 'INTEGER']


def test_open_db_initialises_and_returns_none_when_it_cannot_connect():
    path = os.path.join(tempfile.mkdtemp(), 'jobs.db')
    conn = schema.open_db({'db_path': path, 'jobs_tablename': 'jobs'})
    assert schema.JOB_DETAILS in tables(conn)
    conn.close()
    assert schema.open_db({'db_path': '/nonexistent/dir/jobs.db', 'jobs_tablename': 'jobs'}) is None


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_") and callable(fn)]
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"{len(tests)} passed")
