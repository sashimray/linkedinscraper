"""
The workflow's parse step: split visible jobs that have no job_details row yet into
responsibilities / requirements / preferences (from job_description_html, or the text for older jobs).
"""

from db.queries import read_jobs_without_details, upsert_job_details
from db.schema import open_db
from workflow.scrape.job_parser import parse_description_html, parse_job_text


def parse_job_sections(description, html):
    """job_parser sections from the stored description HTML, or from the text when there is no HTML."""
    return parse_description_html(html) if html else parse_job_text(description)


def parse_missing_details(config):
    """Rule-based job_details rows for visible jobs that have none. Returns how many were saved."""
    conn = open_db(config)
    if conn is None:
        raise RuntimeError("Cannot create the database connection.")
    try:
        rows = [(job_id, parse_job_sections(description, html))
                for job_id, description, html in read_jobs_without_details(conn, config['jobs_tablename'])]
        if rows:
            upsert_job_details(conn, config['jobs_tablename'], rows)
    finally:
        conn.close()
    print(f"Parsed {len(rows)} jobs that had no job_details row")
    return len(rows)
