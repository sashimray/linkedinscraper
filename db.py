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
    # Visible jobs as dicts: latest posting date first, then within a day the average of ats_score and
    # ats_score_embed (a missing score counts as 0), then newest id
    cursor = conn.execute(f"""
        SELECT * FROM "{jobs_table_name}"
        WHERE COALESCE(hidden, 0) != 1
        ORDER BY COALESCE(date, '') DESC,
                 (COALESCE(ats_score, 0) + COALESCE(ats_score_embed, 0)) / 2.0 DESC,
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

def hide_jobs(conn, jobs_table_name, job_ids):
    conn.executemany(f'UPDATE "{jobs_table_name}" SET hidden = 1 WHERE id = ?', [(job_id,) for job_id in job_ids])
    conn.commit()
