"""
The workflow's hide step: hide (hidden = 1) visible jobs whose description asks for a security clearance or
US citizenship. Runs before the parse step, on the whole description.

  python -m workflow.hide               # uses config.json
  python -m workflow.hide other.json
  python -m workflow.hide --dry-run     # list the jobs that would be hidden without changing them
"""

import re
import sys

from db.queries import hide_jobs, read_visible_job_texts
from db.schema import open_db
from config import load_config


# A security clearance needs US citizenship, so any required clearance counts, including "ability to obtain" one
CLEARANCE = re.compile(r"ts/sci|ts/sc\b|top secret|\bsecret\b.{0,40}clearance|security clearance|polygraph|\bci poly"
                       r"|full.scope poly|w/ ?poly|with poly|(active|current|government|dod|dow|federal) .{0,20}clearance"
                       r"|clearance (is )?required|^(top )?secret$")
CITIZENSHIP = re.compile(r"(\bu\.?\s?s\.?|united states|american) citizen|citizenship")
# Sentences that also accept permanent residents are fine ("U.S. Person" includes lawful permanent residents)
PERMANENT_RESIDENT_OK = re.compile(r"permanent (u\.?\s?s\.? )?residen|green card|\bu\.?\s?s\.? persons?\b|lawful(ly)? permanent|\blpr\b")
NOT_REQUIRED = re.compile(r"desired|preferred|preference given|is a plus|nice to have|not required|\bbonus\b")
# Company boilerplate that mentions citizenship or clearances without requiring them: EEO statements
# ("without regard to ... citizenship"), background-check and fair-chance notices
BOILERPLATE = re.compile(r"without regard to|regardless of|discriminat|equal (employment )?opportunit"
                         r"|protected (veteran|characteristic|status)|arrest or conviction|background (check|investigation)")
# Labels such as "Security Clearance Type:" (the answer, often "Not Required", is on the next line)
LABEL = re.compile(r":$|^work authorization\s*/\s*security clearance$")


def clearance_or_citizenship_requirement(text):
    # Returns the sentence of a job description (or its requirements) that requires a clearance or US citizenship, or None
    # Split into sentences on new lines, ';' and '. ' after a lowercase word (so "U.S. citizen" stays together)
    for sentence in re.split(r"\n|;|(?<=[a-z]{2}\.)\s+", text or ''):
        text = sentence.strip().lstrip('-•* ').lower()
        if not text or NOT_REQUIRED.search(text) or BOILERPLATE.search(text) or LABEL.search(text):
            continue
        if CLEARANCE.search(text):
            return sentence.strip()
        if CITIZENSHIP.search(text) and not PERMANENT_RESIDENT_OK.search(text):
            return sentence.strip()
    return None


def hide_clearance_jobs(config, dry_run=False):
    # Set hidden = 1 for visible jobs whose description needs a security clearance or US citizenship.
    # Reads the whole description, so it works before the jobs are split into job_details.
    jobs_tablename = config['jobs_tablename']
    conn = open_db(config)
    if conn is None:
        print("Error! cannot create the database connection.")
        return []

    to_hide = []
    for job_id, title, company, description in read_visible_job_texts(conn, jobs_tablename):
        reason = clearance_or_citizenship_requirement(description)
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
    hide_clearance_jobs(load_config(config_file), dry_run='--dry-run' in sys.argv)
