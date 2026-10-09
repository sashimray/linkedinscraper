"""
Embedding-based ATS score (0-100) saved to jobs.ats_score_embed, next to the rule-based jobs.ats_score.

Reads the job_items/matches tables written by embedder.py and the skill weights in skills_flat.json.
Every requirement/preference line gets a credit from 0 to 1:
  - alias match on one of your skills            -> share of the line's tech skills you have
                                                    (1 for "X or Y" lines, like ats_score.py)
  - embedding match on one of your concept skills -> 0 at the embedder threshold (0.78), rising to 1 at 0.88
  - no match, but the line names a tech skill (ats_score.find_skills) -> 0, you lack it
  - no match, but a years/degree line ("5+ years", "Bachelor's") -> skipped; ats_score.py checks those
  - no match, but 2+ domain words ("low earth orbit satellites", ats_score.keyword_words) -> 0
  - no match and only generic/soft-skill words ("strong communicator") -> skipped
Each line counts with the weight of its best-matching skill (a missed line counts as a tech or concept skill),
so tech lines weigh more than concept lines. Score = 85% requirements + 15% preferences, like ats_score.py.
Runs locally only; nothing is sent to an API.

  python embedder.py data/my_database.db skills_flat.json    # first: refresh job_items and matches
  python ats_score_embed.py                                   # uses config.json and skills_flat.json
  python ats_score_embed.py --dry-run --show 42
"""

import argparse
import sqlite3
import sys

from ats_score import (ANY_OF, PREFERENCES_WEIGHT, REQUIREMENTS_WEIGHT, degree_level, find_skills, keyword_words,
                       required_years)
from db import create_connection, read_visible_job_item_matches, update_ats_scores
from embedder import DEFAULT_THRESHOLD, load_skills

SCORE_COLUMN = 'ats_score_embed'
EMBED_FULL = 0.88           # embedding similarity that earns full credit
MISSING_TECH_WEIGHT = 3     # same as a tech skill in skills_flat.json
MISSING_DOMAIN_WEIGHT = 2   # same as a concept skill
MIN_DOMAIN_WORDS = 2        # fewer meaningful words than this and an unmatched line is skipped


def match_credit(method, similarity, floor=DEFAULT_THRESHOLD, full=EMBED_FULL):
    """Credit 0-1 for one match, or None when an embedding match is below the floor and doesn't count."""
    if method == 'alias':
        return 1.0
    if similarity is None or similarity < floor:
        return None
    return min(1.0, (similarity - floor) / (full - floor))


def alias_coverage(text, alias_count, terms):
    """Credit for a line with alias matches: matches / (matches + tech skills named in the line that you lack).
    terms: lowercase names and aliases of your skills."""
    if ANY_OF.search(text.lower()):
        return 1.0
    missing = find_skills(text) - terms
    return alias_count / (alias_count + len(missing))


def line_credit(text, line_matches, profile):
    """(weight, credit) for one line, or None when the line has nothing to check.
    line_matches: [(skill, method, similarity)]; profile: skill_profile() result."""
    weights = profile['weights']
    aliases = [skill for skill, method, _ in line_matches if method == 'alias']
    alias_credit = alias_coverage(text, len(aliases), profile['terms']) if aliases else None
    scored = [(alias_credit if method == 'alias' else credit, weights.get(skill, MISSING_TECH_WEIGHT))
              for skill, method, similarity in line_matches
              if (credit := match_credit(method, similarity)) is not None]
    if scored:
        credit, weight = max(scored)
        return weight, credit
    if find_skills(text):
        return MISSING_TECH_WEIGHT, 0.0
    if required_years(text) is not None or degree_level(text) is not None:
        return None
    if len(keyword_words(text)) >= MIN_DOMAIN_WORDS:
        return MISSING_DOMAIN_WEIGHT, 0.0
    return None


def section_score(credits):
    # Weighted average of (weight, credit) pairs, or None when there are none
    total = sum(weight for weight, _ in credits)
    return sum(weight * credit for weight, credit in credits) / total if total else None


def score_job(lines, profile):
    """ATS score 0-100, or None when no line is checkable.
    lines: [(kind 'requirement'|'preference', text, [(skill, method, similarity)])]."""
    credits = {'requirement': [], 'preference': []}
    for kind, text, line_matches in lines:
        result = line_credit(text, line_matches, profile)
        if result is not None and kind in credits:
            credits[kind].append(result)
    req = section_score(credits['requirement'])
    pref = section_score(credits['preference'])
    if req is None and pref is None:
        return None
    if req is None or pref is None:
        score = pref if req is None else req
    else:
        score = REQUIREMENTS_WEIGHT * req + PREFERENCES_WEIGHT * pref
    return round(score * 100)


def group_lines(rows):
    """{job_id: [(kind, text, [(skill, method, similarity)])]} from read_visible_job_item_matches rows."""
    jobs = {}
    for job_id, kind, idx, text, skill, method, similarity in rows:
        lines = jobs.setdefault(job_id, {})
        line = lines.setdefault((kind, idx), (kind, text, []))
        if skill is not None:
            line[2].append((skill, method, similarity))
    return {job_id: list(lines.values()) for job_id, lines in jobs.items()}


def skill_profile(skills):
    """{'weights': {name: weight}, 'terms': lowercase names and aliases} from skills_flat.json skills."""
    weights, terms = {}, set()
    for skill in skills:
        try:
            weights[skill['name']] = float(skill.get('weight', MISSING_TECH_WEIGHT))
            terms.update(term.lower() for term in [skill['name'], *skill.get('aliases', [])])
        except (AttributeError, KeyError, TypeError, ValueError):
            raise ValueError(f"Invalid skill entry in skills file: {skill!r}")
    return {'weights': weights, 'terms': terms}


def job_keywords(lines, profile):
    """{'matched': [...], 'gaps': [...]} for one job's lines: your skills that matched, and the tech skills and domain
    words of lines you didn't fully cover. A term matched on any line isn't listed as a gap."""
    matched, gaps = [], []
    for _, text, line_matches in lines:
        result = line_credit(text, line_matches, profile)
        if result is None:
            continue
        matched += [skill for skill, method, similarity in line_matches
                    if match_credit(method, similarity) is not None]
        if result[1] < 1:
            missing = sorted(find_skills(text) - profile['terms'])
            gaps += missing or (list(keyword_words(text).values()) if result[1] == 0 else [])
    matched = list(dict.fromkeys(matched))
    lowered = {term.lower() for term in matched}
    return {'matched': matched, 'gaps': [term for term in dict.fromkeys(gaps) if term.lower() not in lowered]}


def explain(lines, profile):
    # One printable row per line: kind, credit (or '-' when skipped), best match, text
    out = []
    for kind, text, line_matches in lines:
        result = line_credit(text, line_matches, profile)
        best = max(line_matches, key=lambda m: (m[1] == 'alias', m[2] or 0), default=None)
        label = f"{best[0]} ({best[1]} {best[2]:.2f})" if best else ('missed' if result else '')
        credit = '  - ' if result is None else f"{result[1]:.2f}"
        out.append(f"[{kind[:4]}] {credit}  {text[:90]}" + (f"\n             {label}" if label else ''))
    return '\n'.join(out)


def score_jobs(config, skills_path='skills_flat.json', dry_run=False, show=None):
    """Score visible jobs and save them to jobs.ats_score_embed. Returns {job_id: score}."""
    profile = skill_profile(load_skills(skills_path))
    jobs_table = config['jobs_tablename']
    conn = create_connection(config)
    if conn is None:
        raise ValueError("Cannot create the database connection.")
    try:
        try:
            jobs = group_lines(read_visible_job_item_matches(conn, jobs_table))
        except sqlite3.OperationalError as e:
            raise ValueError(f"Could not read job_items/matches ({e}); run embedder.py first.")
        scores = {job_id: score for job_id, lines in jobs.items()
                  if (score := score_job(lines, profile)) is not None}
        if not dry_run:
            update_ats_scores(conn, jobs_table, scores, SCORE_COLUMN)
    finally:
        conn.close()

    if show is not None:
        print(f"Job {show}: {scores.get(show, 'not scored')}\n{explain(jobs.get(show, []), profile)}\n")
    print(f"{'Would score' if dry_run else 'Scored'} {len(scores)} jobs; average "
          f"{sum(scores.values()) / len(scores):.0f}" if scores else "No jobs had checkable lines")
    return scores


def main(argv=None):
    parser = argparse.ArgumentParser(description="Score visible jobs from embedder.py matches and save jobs.ats_score_embed")
    parser.add_argument('config', nargs='?', default='config.json')
    parser.add_argument('--skills', default='skills_flat.json')
    parser.add_argument('--dry-run', action='store_true', help="compute scores without saving them")
    parser.add_argument('--show', type=int, metavar='JOB_ID', help="print line-by-line credits for one job")
    args = parser.parse_args(argv)

    from main import load_config  # here so tests can import this module without the scraper's dependencies
    try:
        score_jobs(load_config(args.config), args.skills, args.dry_run, args.show)
    except ValueError as e:
        print(f"Error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
