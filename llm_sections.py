"""
Second pass for jobs the rule-based parser in job_parser.py couldn't split into sections.

Reads job_description text for visible jobs whose job_details row has sections_parsed 0 (or that have no
job_details row yet), asks an LLM to pull out the responsibilities, requirements and preferred qualifications,
and saves them to job_details with sections_parsed = 2. Run it separately from the scraper:

  python llm_sections.py                       # local Ollama (llama3.1:8b)
  python llm_sections.py --provider claude --limit 20
"""

import argparse
import sys

from db import create_connection, read_jobs_without_sections, upsert_job_details
from main import load_config
from resumekit.application_kit import ApplicationKit, ApplicationKitError, DEFAULT_CLAUDE_MODEL, DEFAULT_OLLAMA_MODEL

SECTION_FIELDS = ('responsibilities', 'requirements', 'preferred_qualifications')

SYSTEM = (
    "You extract sections from job postings. Postings name their sections in many ways: "
    "\"What you'll do\" or \"Your impact\" are responsibilities; \"Who you are\", \"What you bring\", "
    "\"Minimum qualifications\" are requirements; \"Nice to have\", \"What we'd like you to have\", "
    "\"Bonus points\" are preferred qualifications. Some postings have no headings at all, so decide from the content. "
    "Copy each item from the posting with the original wording, one item per list entry. "
    "Do not invent items. Leave out company descriptions, benefits, pay, location and EEO text. "
    "Use an empty list for a section the posting doesn't have."
)

SCHEMA = {
    "type": "object",
    "properties": {field: {"type": "array", "items": {"type": "string"}} for field in SECTION_FIELDS},
    "required": list(SECTION_FIELDS),
    "additionalProperties": False,
}


def extract_sections(kit, job):
    # Returns {field: text} with one "- item" per line, the same format job_parser produces for bullet lists
    user = (f"Job title: {job['title'] or 'unknown'}\nCompany: {job['company'] or 'unknown'}\n\n"
            f"<job_description>\n{job['job_description']}\n</job_description>")
    data = kit._generate_json(SYSTEM, user, SCHEMA)
    sections = {}
    for field in SECTION_FIELDS:
        items = [item.strip().lstrip('-•* ').strip() for item in data.get(field) or [] if isinstance(item, str)]
        sections[field] = '\n'.join('- ' + item for item in items if item)
    return sections


def run(config, kit, table_name, limit=None):
    conn = create_connection(config)
    jobs = read_jobs_without_sections(conn, table_name, limit)
    print(f"{len(jobs)} jobs in the {table_name} table need sections")
    done = failed = 0
    for job in jobs:
        try:
            sections = extract_sections(kit, job)
        except ApplicationKitError as e:
            # A failed call leaves sections_parsed at 0, so the job is retried on the next run
            print(f"Skipped job {job['id']} ({job['title']} at {job['company']}): {e}")
            failed += 1
            continue
        sections['sections_parsed'] = 2
        upsert_job_details(conn, table_name, [(job['id'], sections)])
        done += 1
        print(f"Parsed job {job['id']}: {job['title']} at {job['company']}")
    conn.close()
    print(f"Done: {done} parsed, {failed} skipped")
    return done, failed


def main(argv=None):
    parser = argparse.ArgumentParser(description="Use an LLM to split job descriptions the rule-based parser couldn't.")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--table", help="Jobs table to read from (default: jobs_tablename from the config)")
    parser.add_argument("--limit", type=int, help="Process at most this many jobs")
    parser.add_argument("--provider", choices=["ollama", "claude"], default="ollama")
    parser.add_argument("--model", help=f"Model name (default: {DEFAULT_OLLAMA_MODEL} / {DEFAULT_CLAUDE_MODEL})")
    parser.add_argument("--api-key", help="Claude API key (default: ANTHROPIC_API_KEY)")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    kit = ApplicationKit(args.provider, args.model, args.api_key)
    print(f"Using {kit.provider} ({kit.model})...")
    run(config, kit, args.table or config['jobs_tablename'], args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
