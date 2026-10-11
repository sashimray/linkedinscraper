"""
Every table the project uses, and the statements that create and migrate them. db.py only reads and writes rows.

  jobs           config['jobs_tablename']           scraped jobs (created from the first scrape's columns)
  filtered_jobs  config['filtered_jobs_tablename']  jobs dropped by desc_words, kept so they aren't scraped again
  job_details    JOB_DETAILS                        responsibilities / requirements / preferences per job
  job_items      JOB_ITEMS                          one row per requirement/preference line, with its embedding
  skill_vectors  SKILL_VECTORS                      embeddings of the skills in skills_flat.json
  matches        MATCHES                            job_items line -> skill matches (alias or embedding)

Entry points call open_db(config) (or init_db on an open connection) before using the database.
"""

from db import create_connection

JOB_DETAILS = 'job_details'
JOB_ITEMS = 'job_items'
SKILL_VECTORS = 'skill_vectors'
MATCHES = 'matches'

# Columns the jobs table gets after it was created from the scrape's DataFrame
JOBS_EXTRA_COLUMNS = {
    'company_industry': 'TEXT',
    'job_description_html': 'TEXT',
    'cover_letter': 'TEXT',
    'resume': 'TEXT',
    'ats_score': 'INT DEFAULT 0',
    'ats_score_embed': 'INT DEFAULT 0',
}

# Data types mapping from pandas to SQLite (unknown dtypes fall back to TEXT)
TYPE_MAPPING = {
    'int64': 'INTEGER',
    'float64': 'REAL',
    'datetime64[ns]': 'TIMESTAMP',
    'object': 'TEXT',
    'str': 'TEXT',  # pandas 3.x default string dtype
    'bool': 'INTEGER'
}

EMBEDDER_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {JOB_ITEMS}(
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL,
    kind TEXT NOT NULL,            -- requirement | preference
    idx INTEGER NOT NULL,          -- order within the job and kind
    text TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    model TEXT,
    embedding BLOB,                -- float32, L2-normalized
    UNIQUE(job_id, kind, idx)
);
CREATE TABLE IF NOT EXISTS {SKILL_VECTORS}(
    name TEXT PRIMARY KEY,
    text_hash TEXT NOT NULL,
    model TEXT NOT NULL,
    embedding BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS {MATCHES}(
    job_item_id INTEGER NOT NULL,
    skill TEXT NOT NULL,
    method TEXT NOT NULL,          -- alias | embed
    similarity REAL NOT NULL,
    PRIMARY KEY(job_item_id, skill)
);
"""


def sqlite_type(df, column):
    return TYPE_MAPPING.get(str(df.dtypes[column]), "TEXT")


def table_columns(conn, table_name):
    return [row[1] for row in conn.execute(f'PRAGMA table_info("{table_name}")')]


def table_exists(conn, table_name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,)).fetchone() is not None


def add_columns(conn, table_name, columns):
    # Add the {name: sqlite type} columns that the table doesn't have yet
    existing = table_columns(conn, table_name)
    for column, column_type in columns.items():
        if column not in existing:
            conn.execute(f'ALTER TABLE "{table_name}" ADD COLUMN "{column}" {column_type}')
    conn.commit()


def create_job_posting_id_index(conn, table_name):
    # Make job_posting_id unique. Tables created before this column existed get it added and backfilled from job_url.
    columns = table_columns(conn, table_name)
    if 'job_posting_id' not in columns:
        conn.execute(f'ALTER TABLE "{table_name}" ADD COLUMN "job_posting_id" TEXT')
        if 'job_url' in columns:
            conn.execute(f"""
                UPDATE "{table_name}"
                SET job_posting_id = rtrim(replace(job_url, 'https://www.linkedin.com/jobs/view/', ''), '/')
                WHERE job_url LIKE 'https://www.linkedin.com/jobs/view/%'
            """)
    conn.execute(f'CREATE UNIQUE INDEX IF NOT EXISTS "idx_{table_name}_job_posting_id" ON "{table_name}" (job_posting_id)')
    conn.commit()


def prepare_jobs_table(conn, df, table_name):
    """Make sure a jobs-style table can take df's rows: create it from df's columns, or add the columns it lacks.
    Returns False (and creates nothing) for an empty df, so the table isn't created without the job columns."""
    if df.empty:
        return False
    columns = {column: sqlite_type(df, column) for column in df.columns}
    if table_exists(conn, table_name):
        add_columns(conn, table_name, columns)
    else:
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS "{table_name}" (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                {', '.join(f'"{column}" {column_type}' for column, column_type in columns.items())}
            )
        """)
        conn.commit()
    create_job_posting_id_index(conn, table_name)
    return True


def create_job_details_table(conn, jobs_table_name):
    # One row per job; job_id is unique so re-runs update the row instead of adding another.
    # sections_parsed: 1 = rules found responsibilities and requirements, 0 = they didn't (llm_sections.py retries these),
    # 2 = filled by the LLM
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {JOB_DETAILS} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL UNIQUE REFERENCES "{jobs_table_name}"(id),
            responsibilities TEXT,
            requirements TEXT,
            preferences TEXT,
            sections_parsed INTEGER
        )
    """)
    add_columns(conn, JOB_DETAILS, {'sections_parsed': 'INTEGER'})  # tables created before the column existed
    conn.commit()


def create_embedder_tables(conn):
    conn.executescript(EMBEDDER_SCHEMA)


def init_db(conn, jobs_table_name):
    """Create the tables that don't exist yet and add missing columns. Safe to run on every start.
    The jobs table itself is created by the first scrape (prepare_jobs_table), so it's only migrated if it exists."""
    create_job_details_table(conn, jobs_table_name)
    create_embedder_tables(conn)
    if table_exists(conn, jobs_table_name):
        add_columns(conn, jobs_table_name, JOBS_EXTRA_COLUMNS)


def open_db(config):
    """db.create_connection with the schema brought up to date; None when it can't connect, like create_connection."""
    conn = create_connection(config)
    if conn is not None:
        init_db(conn, config['jobs_tablename'])
    return conn
