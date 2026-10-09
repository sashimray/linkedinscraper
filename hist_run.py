"""
Historical run: split the job_description text of every job already in the database into
responsibilities / requirements / preferences, and save them in the job_details table.
Then hide (hidden = 1) jobs whose requirements ask for a security clearance or US citizenship.

  python hist_run.py               # uses config.json
  python hist_run.py other.json
  python hist_run.py --dry-run     # list the jobs that would be hidden without changing them
"""

import re
import sys

from db import (create_connection, hide_jobs, read_visible_job_descriptions, read_visible_job_requirements,
                upsert_job_details)
from job_parser import parse_job_text
from main import load_config


def hist_run(config):
    jobs_tablename = config['jobs_tablename']
    conn = create_connection(config)
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


# A security clearance needs US citizenship, so any required clearance counts, including "ability to obtain" one
CLEARANCE = re.compile(r"ts/sci|ts/sc\b|top secret|\bsecret\b.{0,40}clearance|security clearance|polygraph|\bci poly"
                       r"|full.scope poly|w/ ?poly|with poly|(active|current|government|dod|dow|federal) .{0,20}clearance"
                       r"|clearance (is )?required|^(top )?secret$")
CITIZENSHIP = re.compile(r"(\bu\.?\s?s\.?|united states|american) citizen|citizenship")
# Sentences that also accept permanent residents are fine ("U.S. Person" includes lawful permanent residents)
PERMANENT_RESIDENT_OK = re.compile(r"permanent (u\.?\s?s\.? )?residen|green card|\bu\.?\s?s\.? persons?\b|lawful(ly)? permanent|\blpr\b")
NOT_REQUIRED = re.compile(r"desired|preferred|is a plus|nice to have|not required|\bbonus\b")


def clearance_or_citizenship_requirement(requirements):
    # Returns the sentence that requires a clearance or US citizenship, or None
    # Split into sentences on new lines, ';' and '. ' after a lowercase word (so "U.S. citizen" stays together)
    for sentence in re.split(r"\n|;|(?<=[a-z]{2}\.)\s+", requirements or ''):
        text = sentence.strip().lstrip('-•* ').lower()
        if not text or NOT_REQUIRED.search(text):
            continue
        if CLEARANCE.search(text):
            return sentence.strip()
        if CITIZENSHIP.search(text) and not PERMANENT_RESIDENT_OK.search(text):
            return sentence.strip()
    return None


def hide_clearance_jobs(config, dry_run=False):
    # Set hidden = 1 for visible jobs whose job_details requirements need a security clearance or US citizenship
    jobs_tablename = config['jobs_tablename']
    conn = create_connection(config)
    if conn is None:
        print("Error! cannot create the database connection.")
        return []

    to_hide = []
    for job_id, title, company, requirements in read_visible_job_requirements(conn, jobs_tablename):
        reason = clearance_or_citizenship_requirement(requirements)
        if reason:
            to_hide.append(job_id)
            print(f"{'Would hide' if dry_run else 'Hiding'} job {job_id}: {title} at {company} -- {reason[:120]}")

    if not dry_run:
        hide_jobs(conn, jobs_tablename, to_hide)
    conn.close()
    print(f"{'Would hide' if dry_run else 'Hid'} {len(to_hide)} jobs that require a security clearance or US citizenship")
    return to_hide


if __name__ == "__main__":
    args = [arg for arg in sys.argv[1:] if arg != '--dry-run']
    config_file = args[0] if args else 'config.json'  # default config file
    config = load_config(config_file)

    hist_run(config)
    hide_clearance_jobs(config, dry_run='--dry-run' in sys.argv)
