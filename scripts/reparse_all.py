"""
Re-split the job_description text of every visible job already in the database into
responsibilities / requirements / preferences, and save them in the job_details table.
Not part of the workflow, whose parse step only handles jobs without a job_details row.

  python -m scripts.reparse_all               # uses config.json
  python -m scripts.reparse_all other.json
"""

import sys

from config import load_config
from db.queries import read_visible_job_descriptions, upsert_job_details
from db.schema import open_db
from workflow.scrape.job_parser import parse_job_text


def reparse_all(config):
    # Re-split every visible job's job_description text into job_details (LLM-filled rows are kept)
    jobs_tablename = config['jobs_tablename']
    conn = open_db(config)
    if conn is None:
        print("Error! cannot create the database connection.")
        return 0

    jobs = read_visible_job_descriptions(conn, jobs_tablename)
    print(f"Parsing {len(jobs)} jobs from the {jobs_tablename} table")

    rows = []
    parsed = 0
    for job_id, job_description in jobs:
        sections = parse_job_text(job_description)
        parsed += sections['sections_parsed']
        rows.append((job_id, sections))

    upsert_job_details(conn, jobs_tablename, rows)
    conn.close()

    print(f"Saved {len(rows)} jobs to the job_details table; "
          f"{parsed} had both responsibilities and requirements, {len(rows) - parsed} are missing one or both "
          f"(llm_sections.py can fill those in; jobs it already filled were left as they are)")
    return len(rows)


if __name__ == "__main__":
    reparse_all(load_config(sys.argv[1] if len(sys.argv) > 1 else 'config.json'))
