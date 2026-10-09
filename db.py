import sqlite3
from sqlite3 import Error
import pandas as pd

# Data types mapping from pandas to SQLite (unknown dtypes fall back to TEXT)
TYPE_MAPPING = {
    'int64': 'INTEGER',
    'float64': 'REAL',
    'datetime64[ns]': 'TIMESTAMP',
    'object': 'TEXT',
    'str': 'TEXT',  # pandas 3.x default string dtype
    'bool': 'INTEGER'
}

def sqlite_type(df, column):
    return TYPE_MAPPING.get(str(df.dtypes[column]), "TEXT")

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

def create_table(conn, df, table_name):
    ''''
    # Create a new table with the data from the dataframe
    df.to_sql(table_name, conn, if_exists='replace', index=False)
    print (f"Created the {table_name} table and added {len(df)} records")
    '''
    # Create a new table with the data from the DataFrame.
    # Skip empty DataFrames, otherwise the table would be created without the job columns.
    if df.empty:
        print(f"No records for the {table_name} table, not creating it yet")
        return

    # Prepare a string with column names and their types
    columns_with_types = ', '.join(
        f'"{column}" {sqlite_type(df, column)}'
        for column in df.columns
    )
    
    # Prepare SQL query to create a new table
    create_table_sql = f"""
        CREATE TABLE IF NOT EXISTS "{table_name}" (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            {columns_with_types}
        );
    """
    
    # Execute SQL query
    cursor = conn.cursor()
    cursor.execute(create_table_sql)
    
    # Commit the transaction
    conn.commit()

    create_job_posting_id_index(conn, table_name)
    added = insert_records(conn, df, table_name)

    print(f"Created the {table_name} table and added {added} records")

def update_table(conn, df, table_name):
    # Update the existing table with new records. Rows whose job_posting_id is already in the table are skipped.
    if df.empty:
        print (f"No new records to add to the {table_name} table")
        return
    create_job_posting_id_index(conn, table_name)
    add_missing_columns(conn, df, table_name)
    added = insert_records(conn, df, table_name)

    if added > 0:
        print (f"Added {added} new records to the {table_name} table")
    else:
        print (f"No new records to add to the {table_name} table")

def insert_records(conn, df, table_name):
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

def create_job_posting_id_index(conn, table_name):
    # Make job_posting_id unique. Tables created before this column existed get it added and backfilled from job_url.
    cursor = conn.cursor()
    columns = [row[1] for row in cursor.execute(f'PRAGMA table_info("{table_name}")')]
    if 'job_posting_id' not in columns:
        cursor.execute(f'ALTER TABLE "{table_name}" ADD COLUMN "job_posting_id" TEXT')
        if 'job_url' in columns:
            cursor.execute(f"""
                UPDATE "{table_name}"
                SET job_posting_id = rtrim(replace(job_url, 'https://www.linkedin.com/jobs/view/', ''), '/')
                WHERE job_url LIKE 'https://www.linkedin.com/jobs/view/%'
            """)
    cursor.execute(f'CREATE UNIQUE INDEX IF NOT EXISTS "idx_{table_name}_job_posting_id" ON "{table_name}" (job_posting_id)')
    conn.commit()

def add_missing_columns(conn, df, table_name):
    # Add any DataFrame columns the table doesn't have yet, so inserts don't fail on new fields
    add_columns(conn, table_name, {column: sqlite_type(df, column) for column in df.columns})

def add_columns(conn, table_name, columns):
    # Add the {name: sqlite type} columns that the table doesn't have yet
    cursor = conn.cursor()
    existing = [row[1] for row in cursor.execute(f'PRAGMA table_info("{table_name}")')]
    for column, column_type in columns.items():
        if column not in existing:
            cursor.execute(f'ALTER TABLE "{table_name}" ADD COLUMN "{column}" {column_type}')
    conn.commit()

def table_exists(conn, table_name):
    # Check if the table already exists in the database
    cur = conn.cursor()
    cur.execute(f"SELECT count(name) FROM sqlite_master WHERE type='table' AND name='{table_name}'")
    if cur.fetchone()[0]==1 :
        return True
    return False

def read_table(conn, table_name):
    # Read the whole table into a DataFrame
    return pd.read_sql_query(f"SELECT * FROM {table_name}", conn)

def read_visible_job_descriptions(conn, table_name):
    # (id, job_description) for every job that isn't hidden. Jobs without a description were marked by transform_job and are skipped.
    return conn.execute(f"""
        SELECT id, job_description FROM "{table_name}"
        WHERE hidden!= 1
          AND COALESCE(job_description, '') NOT IN ('', 'Could not find Job Description')
    """).fetchall()

def create_job_details_table(conn, jobs_table_name):
    # One row per job; job_id is unique so re-runs update the row instead of adding another.
    # sections_parsed: 1 = rules found responsibilities and requirements, 0 = they didn't (llm_sections.py retries these), 2 = filled by the LLM
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS job_details (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL UNIQUE REFERENCES "{jobs_table_name}"(id),
            responsibilities TEXT,
            requirements TEXT,
            preferences TEXT,
            sections_parsed INTEGER
        )
    """)
    add_columns(conn, 'job_details', {'sections_parsed': 'INTEGER'})  # tables created before the column existed
    conn.commit()

def upsert_job_details(conn, jobs_table_name, rows):
    # rows: (job_id, sections) where sections is a job_parser result. Inserts new jobs and updates ones already in the table,
    # except that rule-based results never overwrite what the LLM run filled in.
    create_job_details_table(conn, jobs_table_name)
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

def save_job_details_by_posting(conn, jobs_table_name, sections_by_posting):
    # The scraper only knows job_posting_id; look up each job's id in the jobs table, then save its sections
    if not sections_by_posting:
        return
    posting_ids = list(sections_by_posting)
    placeholders = ', '.join('?' for _ in posting_ids)
    id_rows = conn.execute(
        f'SELECT id, job_posting_id FROM "{jobs_table_name}" WHERE job_posting_id IN ({placeholders})', posting_ids
    ).fetchall()
    upsert_job_details(conn, jobs_table_name, [(job_id, sections_by_posting[posting_id]) for job_id, posting_id in id_rows])
    print(f"Saved sections for {len(id_rows)} jobs to the job_details table")

def read_jobs_without_sections(conn, jobs_table_name, limit=None):
    # Visible jobs the rule-based parser couldn't split (sections_parsed 0), or that have no job_details row yet.
    # Jobs without a description were already marked by transform_job and are skipped.
    create_job_details_table(conn, jobs_table_name)
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
    # (id, job_description) for visible jobs with a description but no job_details row yet
    create_job_details_table(conn, jobs_table_name)
    return conn.execute(f"""
        SELECT j.id, j.job_description
        FROM "{jobs_table_name}" j LEFT JOIN job_details d ON d.job_id = j.id
        WHERE d.job_id IS NULL
          AND COALESCE(j.hidden, 0) != 1
          AND COALESCE(j.job_description, '') NOT IN ('', 'Could not find Job Description')
        ORDER BY j.id
    """).fetchall()

def read_visible_job_requirements(conn, jobs_table_name):
    # (job_id, title, company, requirements) for visible jobs that have a job_details row
    return conn.execute(f"""
        SELECT j.id, j.title, j.company, d.requirements
        FROM "{jobs_table_name}" j JOIN job_details d ON d.job_id = j.id
        WHERE COALESCE(j.hidden, 0) != 1
        ORDER BY j.id
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
    # scores: {job_id: 0-100}. Adds the score column first on databases that don't have it yet.
    add_columns(conn, jobs_table_name, {column: 'INT DEFAULT 0'})
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
    add_columns(conn, jobs_table_name, {'ats_score': 'INT DEFAULT 0', 'ats_score_embed': 'INT DEFAULT 0'})
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
