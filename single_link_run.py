"""
Fetch one LinkedIn job page and print what job_parser extracts from it (for checking the parser).
Nothing is saved to the database.

  python single_link_run.py https://www.linkedin.com/jobs/view/4478314628/
  python single_link_run.py 4478314628 --config other.json
"""
import argparse
import json
import re
import sys

from job_parser import parse_job_details
from main import get_with_retry, load_config

# A bare id, or a job URL; the query string LinkedIn adds when you copy a link (?refId=...&trackingId=...) is dropped
JOB_URL = re.compile(r"^(?:https?://(?:www\.)?linkedin\.com/jobs/view/(\d+)/?(?:[?#]\S*)?|(\d+))$")


def job_url(link):
    """Canonical job URL from a LinkedIn job URL or a bare job id; raises ValueError otherwise."""
    match = JOB_URL.match((link or '').strip())
    if not match:
        raise ValueError(f"Not a LinkedIn job URL or id: {link!r}")
    return f"https://www.linkedin.com/jobs/view/{match.group(1) or match.group(2)}/"


def run(config, link, fetch=None):
    """Parsed job_details fields for one job page. fetch(url, config) returns a BeautifulSoup or None
    (default: main.get_with_retry)."""
    url = job_url(link)
    soup = (fetch or get_with_retry)(url, config)
    if soup is None:
        raise RuntimeError(f"Could not fetch {url}")
    return parse_job_details(soup)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Print the parsed sections of one LinkedIn job page.")
    parser.add_argument("link", help="job URL or numeric job id")
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args(argv)
    try:
        details = run(load_config(args.config), args.link)
    except (ValueError, RuntimeError, OSError) as e:
        print(f"Error: {e}")
        return 1
    print(json.dumps(details, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
