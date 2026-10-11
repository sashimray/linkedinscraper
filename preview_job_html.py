"""
Fetch one LinkedIn job page and show the description HTML that would be stored as job_description_html,
and whether parsing that stored HTML gives the same sections as parsing the live page. Nothing is saved to the database.

  python preview_job_html.py https://www.linkedin.com/jobs/view/4478314628/
  python preview_job_html.py 4478314628 --pretty          # indented HTML
  python preview_job_html.py 4478314628 --out output/      # also write job_<id>.html to open in a browser
"""
import argparse
import html
import os
import re
import sys
from collections import Counter

from bs4 import BeautifulSoup

from job_parser import SECTION_FIELDS, description_html, parse_description_html, parse_job_details
from main import get_with_retry, load_config
from single_link_run import job_url


def preview(soup):
    """{'html', 'bytes', 'tags': Counter, 'live', 'stored', 'same'} for one fetched job page."""
    saved = description_html(soup)
    live = parse_job_details(soup)
    stored = parse_description_html(saved)
    same = all(live[field] == stored[field] for field in SECTION_FIELDS)
    tags = Counter(tag.name for tag in BeautifulSoup(saved, 'html.parser').find_all(True))
    return {'html': saved, 'bytes': len(saved.encode('utf-8')), 'tags': tags, 'live': live, 'stored': stored, 'same': same}


def report(result, pretty=False):
    # Printable summary: the stored HTML, its size and tags, and the section line counts from both parses
    shown = BeautifulSoup(result['html'], 'html.parser').prettify() if pretty else result['html']
    count = lambda text: len(text.split('\n')) if text else 0
    lines = ["=== job_description_html ===", shown or "(no description found on the page)", "",
             "=== summary ===",
             f"size: {result['bytes']:,} bytes",
             "tags: " + (", ".join(f"{name} x{n}" for name, n in result['tags'].most_common()) or "none"),
             f"company: {result['live']['company'] or '-'} | industry: {result['live']['company_industry'] or '-'}",
             f"{'section':26} live page  stored html"]
    lines += [f"{field:26} {count(result['live'][field]):>9}  {count(result['stored'][field]):>11}" for field in SECTION_FIELDS]
    lines.append("parsing the stored HTML gives " + ("the same sections as the live page" if result['same'] else
                                                     "DIFFERENT sections from the live page"))
    return "\n".join(lines)


def write_page(result, url, out_dir):
    """Write the stored HTML as a standalone page (job_<id>.html) for viewing in a browser. Returns the path."""
    job_id = re.search(r'/(\d+)/$', url).group(1)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"job_{job_id}.html")
    with open(path, 'w', encoding='utf-8') as f:
        f.write(f"<!doctype html>\n<meta charset=\"utf-8\">\n<title>job {job_id}</title>\n"
                f"<p><a href=\"{html.escape(url)}\">{html.escape(url)}</a></p>\n<hr>\n{result['html']}\n")
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description="Show the description HTML that would be stored for one LinkedIn job.")
    parser.add_argument("link", help="job URL or numeric job id")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--pretty", action="store_true", help="print the HTML indented")
    parser.add_argument("--out", metavar="DIR", help="also write job_<id>.html to this directory")
    args = parser.parse_args(argv)
    try:
        url = job_url(args.link)
        config = load_config(args.config)
    except (ValueError, OSError) as e:
        print(f"Error: {e}")
        return 1
    soup = get_with_retry(url, config)
    if soup is None:
        print(f"Error: Could not fetch {url}")
        return 1
    result = preview(soup)
    print(report(result, args.pretty))
    if args.out:
        print(f"wrote {write_page(result, url, args.out)}")
    return 0 if result['html'] else 1


if __name__ == "__main__":
    sys.exit(main())
