import sqlite3
from sqlite3 import Error
import pandas as pd

def create_connection(config):
    # Create a database connection to a SQLite database
    conn = None
    path = config['db_path']
    try:
        conn = sqlite3.connect(path) # creates a SQL database in the 'data' directory
        #print(sqlite3.version)
    except Error as e:
        print(e)

    return conn

def insert_records(conn, df, table_name):
    # Run schema.prepare_jobs_table first so the table has df's columns and the job_posting_id index
    # Insert DataFrame records one by one, ignoring any whose job_posting_id already exists. Returns the number inserted.
    insert_sql = f"""
        INSERT OR IGNORE INTO "{table_name}" ({', '.join(f'"{column}"' for column in df.columns)})
        VALUES ({', '.join(['?' for _ in df.columns])})
    """
    cursor = conn.cursor()
    added = 0
    for record in df.to_dict(orient='records'):
        cursor.execute(insert_sql, list(record.values()))
        added += cursor.rowcount

    # Commit the transaction
    conn.commit()
    return added

def read_table(conn, table_name):
    # Read the whole table into a DataFrame
    return pd.read_sql_query(f"SELECT * FROM {table_name}", conn)

def read_visible_job_descriptions(conn, table_name):
    # (id, job_description) for every job that isn't hidden. Jobs without a description were marked by transform_job and are skipped.
    return conn.execute(f"""
        SELECT id, job_description FROM "{table_name}"
        WHERE COALESCE(hidden, 0) != 1
          AND COALESCE(job_description, '') NOT IN ('', 'Could not find Job Description')
    """).fetchall()

def upsert_job_details(conn, jobs_table_name, rows):
    # rows: (job_id, sections) where sections is a job_parser result. Inserts new jobs and updates ones already in the table,
    # except that rule-based results never overwrite what the LLM run filled in.
    conn.executemany("""
        INSERT INTO job_details (job_id, responsibilities, requirements, preferences, sections_parsed)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(job_id) DO UPDATE SET
            responsibilities = excluded.responsibilities,
            requirements = excluded.requirements,
            preferences = excluded.preferences,
            sections_parsed = excluded.sections_parsed
        WHERE COALESCE(job_details.sections_parsed, 0) != 2 OR excluded.sections_parsed = 2
    """, [(job_id, s['responsibilities'], s['requirements'], s['preferred_qualifications'], s['sections_parsed'])
          for job_id, s in rows])
    conn.commit()

def read_jobs_without_sections(conn, jobs_table_name, limit=None):
    # Visible jobs the rule-based parser couldn't split (sections_parsed 0), or that have no job_details row yet.
    # Jobs without a description were already marked by transform_job and are skipped.
    query = f"""
        SELECT j.id, j.title, j.company, j.job_description
        FROM "{jobs_table_name}" j LEFT JOIN job_details d ON d.job_id = j.id
        WHERE COALESCE(d.sections_parsed, 0) = 0
          AND COALESCE(j.hidden, 0) != 1
          AND COALESCE(j.job_description, '') NOT IN ('', 'Could not find Job Description')
        ORDER BY j.id
    """
    params = ()
    if limit:
        query += " LIMIT ?"
        params = (limit,)
    cursor = conn.execute(query, params)
    column_names = [column[0] for column in cursor.description]
    return [dict(zip(column_names, row)) for row in cursor.fetchall()]

def read_jobs_without_details(conn, jobs_table_name):
    # (id, job_description, job_description_html) for visible jobs with a description but no job_details row yet.
    # job_description_html is NULL for jobs scraped before the scraper saved it.
    return conn.execute(f"""
        SELECT j.id, j.job_description, j.job_description_html
        FROM "{jobs_table_name}" j LEFT JOIN job_details d ON d.job_id = j.id
        WHERE d.job_id IS NULL
          AND COALESCE(j.hidden, 0) != 1
          AND COALESCE(j.job_description, '') NOT IN ('', 'Could not find Job Description')
        ORDER BY j.id
    """).fetchall()

def read_visible_job_texts(conn, jobs_table_name):
    # (job_id, title, company, job_description) for visible jobs with a description
    return conn.execute(f"""
        SELECT id, title, company, job_description FROM "{jobs_table_name}"
        WHERE COALESCE(hidden, 0) != 1
          AND COALESCE(job_description, '') NOT IN ('', 'Could not find Job Description')
        ORDER BY id
    """).fetchall()

def read_visible_job_sections(conn, jobs_table_name):
    # (job_id, title, company, requirements, preferences) for visible jobs that have a job_details row
    return conn.execute(f"""
        SELECT j.id, j.title, j.company, d.requirements, d.preferences
        FROM "{jobs_table_name}" j JOIN job_details d ON d.job_id = j.id
        WHERE COALESCE(j.hidden, 0) != 1
        ORDER BY j.id
    """).fetchall()

def update_ats_scores(conn, jobs_table_name, scores, column='ats_score'):
    # scores: {job_id: 0-100}
    conn.executemany(f'UPDATE "{jobs_table_name}" SET "{column}" = ? WHERE id = ?',
                     [(score, job_id) for job_id, score in scores.items()])
    conn.commit()

def replace_ats_scores(conn, jobs_table_name, scores, column='ats_score'):
    # Set column for every visible job in one transaction: the score from scores ({job_id: 0-100}), NULL for jobs
    # without one (nothing checkable, or no job_details yet), so a stale score or a default 0 isn't read as a result
    with conn:
        conn.execute(f'UPDATE "{jobs_table_name}" SET "{column}" = NULL WHERE COALESCE(hidden, 0) != 1')
        conn.executemany(f'UPDATE "{jobs_table_name}" SET "{column}" = ? WHERE id = ?',
                         [(score, job_id) for job_id, score in scores.items()])

def read_visible_job_item_matches(conn, jobs_table_name):
    # (job_id, kind, idx, text, skill, method, similarity) for visible jobs' job_items (written by embedder.py),
    # one row per match; skill/method/similarity are None for lines that matched nothing
    return conn.execute(f"""
        SELECT i.job_id, i.kind, i.idx, i.text, m.skill, m.method, m.similarity
        FROM job_items i
        JOIN "{jobs_table_name}" j ON j.id = i.job_id
        LEFT JOIN matches m ON m.job_item_id = i.id
        WHERE COALESCE(j.hidden, 0) != 1
        ORDER BY i.job_id, i.kind DESC, i.idx
    """).fetchall()

def read_ranked_jobs(conn, jobs_table_name):
    # Visible jobs as dicts: latest posting date first, then within a day overall_score (the generated average of
    # the two ATS scores; jobs missing a score come last), then newest id
    cursor = conn.execute(f"""
        SELECT * FROM "{jobs_table_name}"
        WHERE COALESCE(hidden, 0) != 1
        ORDER BY COALESCE(date, '') DESC,
                 overall_score DESC NULLS LAST,
                 id DESC
    """)
    column_names = [column[0] for column in cursor.description]
    return [dict(zip(column_names, row)) for row in cursor.fetchall()]

def read_job_sections(conn, job_id):
    # (requirements, preferences) from job_details, or None when the job has no row (or the table doesn't exist yet)
    try:
        return conn.execute('SELECT requirements, preferences FROM job_details WHERE job_id = ?', (job_id,)).fetchone()
    except sqlite3.OperationalError:
        return None

def read_job_item_matches(conn, job_id):
    # Same rows as read_visible_job_item_matches for one job; [] when embedder.py hasn't created its tables yet
    try:
        return conn.execute("""
            SELECT i.job_id, i.kind, i.idx, i.text, m.skill, m.method, m.similarity
            FROM job_items i LEFT JOIN matches m ON m.job_item_id = i.id
            WHERE i.job_id = ?
            ORDER BY i.kind DESC, i.idx
        """, (job_id,)).fetchall()
    except sqlite3.OperationalError:
        return []

JOB_FLAGS = ('applied', 'hidden', 'interview', 'rejected')
GENERATED_TEXT_COLUMNS = ('cover_letter', 'resume')

def read_job(conn, jobs_table_name, job_id, columns=None):
    # One job as a dict (all columns, or only those named), or None when there is no job with that id
    selected = ', '.join(f'"{column}"' for column in columns) if columns else '*'
    cursor = conn.execute(f'SELECT {selected} FROM "{jobs_table_name}" WHERE id = ?', (job_id,))
    row = cursor.fetchone()
    return None if row is None else dict(zip([column[0] for column in cursor.description], row))

def save_generated_text(conn, jobs_table_name, job_id, column, text):
    # Store a generated cover letter or resume for one job; column must be in GENERATED_TEXT_COLUMNS
    if column not in GENERATED_TEXT_COLUMNS:
        raise ValueError(f"Not a generated-text column: {column!r}")
    conn.execute(f'UPDATE "{jobs_table_name}" SET "{column}" = ? WHERE id = ?', (text, job_id))
    conn.commit()


def update_job_flags(conn, jobs_table_name, job_id, flags):
    # flags: {name: 0 or 1} with names from JOB_FLAGS. Returns False when there is no job with that id.
    unknown = set(flags) - set(JOB_FLAGS)
    if not flags or unknown:
        raise ValueError(f"Unknown or missing job flags: {sorted(unknown) or 'none given'}")
    assignments = ', '.join(f'"{name}" = ?' for name in flags)
    cursor = conn.execute(f'UPDATE "{jobs_table_name}" SET {assignments} WHERE id = ?', [*flags.values(), job_id])
    conn.commit()
    return cursor.rowcount > 0

def hide_jobs(conn, jobs_table_name, job_ids):
    conn.executemany(f'UPDATE "{jobs_table_name}" SET hidden = 1 WHERE id = ?', [(job_id,) for job_id in job_ids])
    conn.commit()
