"""
ATS-style match score (0-100) between resume.json and each visible job's job_details requirements/preferences.

Every "- item" line gets a credit from 0 to 1:
  - skills: tech terms from SKILL_PATTERNS (or skills listed in the resume) -> share of them on the resume
    ("Python, C++, or Go" needs only one)
  - keywords: the line's other meaningful words ("numerical", "physics", "simulation") -> share found in the resume,
    so domain requirements that name no tech skill still count
  - years: "5+ years", "6-8 years" -> the line gets 0 if the resume's experience is below the lower bound
  - degree: bachelor / master / PhD and the field (physics, computer science...) -> 0 if the level or field is missing
Lines with only generic or soft-skill words ("strong sense of ownership") are skipped.

Score = 85% requirements + 15% preferences (requirements only when a job lists no preferences). If 2 or more
requirement lines get almost no credit, the score is capped at 60. Runs locally only; nothing is sent to an API.

  python -m workflow.score.ats_score                          # uses config.json and resume.json
  python -m workflow.score.ats_score other.json --resume my_resume.json --dry-run
  python -m workflow.score.ats_score --report                 # matched skills and gaps per job -> output/ats_report.md
  python -m workflow.score.ats_score --report --job 3 --job 34
"""

import argparse
import json
import os
import re
from datetime import date

from db.queries import read_visible_job_sections, replace_ats_scores
from db.schema import open_db

REQUIREMENTS_WEIGHT = 0.85
PREFERENCES_WEIGHT = 0.15
SKILLS_WEIGHT = 0.7       # share of a line's credit from tech skills when it also has other keywords
MISS_CREDIT = 0.25        # a requirement line below this counts as missed
KNOCKOUT_MISSES = 2       # this many missed requirement lines...
KNOCKOUT_CAP = 60         # ...cap the score here

# canonical skill -> regex (matched on lowercase text). Covers common terms that are NOT in the resume too,
# so a job asking for them counts as a miss instead of being skipped.
SKILL_PATTERNS = {
    'java': r'java(?!\s*script)', 'python': r'python', 'ruby': r'ruby', 'rails': r'rails|ror',
    'sql': r'sql', 'javascript': r'javascript|(?<!\.)js(?=\s*[,/)])', 'typescript': r'typescript', 'bash': r'bash|shell script\w*',
    'go': r'golang|go(?=\s*[,/)])|(?<=[,/(]\s)go|(?<=[,/(])go|(?<=\bor )go|(?<=\band )go', 'rust': r'rust', 'c++': r'c\+\+', 'c#': r'c#',
    '.net': r'\.net', 'kotlin': r'kotlin', 'scala': r'scala', 'php': r'php', 'swift': r'swift',
    'spring': r'spring(?:\s*boot|\s*mvc)?', 'django': r'django', 'flask': r'flask', 'fastapi': r'fastapi',
    'hibernate': r'hibernate', 'node.js': r'node(?:\.?js)?', 'react': r'react(?:\.?js| native)?(?!\s+(?:to|quickly)\b)', 'angular': r'angular',
    'vue': r'vue(?:\.?js)?', 'd3.js': r'd3(?:\.js)?', 'html': r'html5?', 'css': r'css3?',
    'rest api': r'restful|rest\s*(?:apis?|services?)', 'soap': r'soap', 'graphql': r'graphql', 'grpc': r'grpc',
    'microservices': r'micro-?services?', 'distributed systems': r'distributed (?:systems?|computing)',
    'kafka': r'kafka|msk', 'storm': r'apache storm|storm', 'oozie': r'oozie', 'mapreduce': r'map-?reduce',
    'hadoop': r'hadoop|hdfs', 'hbase': r'hbase', 'spark': r'spark|pyspark', 'flink': r'flink', 'airflow': r'airflow',
    'elasticsearch': r'elastic\s*search|elk', 'etl': r'etl|elt', 'data pipelines': r'data pipelines?|pipelines?',
    'mysql': r'mysql|aurora', 'postgresql': r'postgres(?:ql)?', 'oracle db': r'oracle (?:database|db)|oracle atp|pl/sql',
    'db2': r'db2', 'sql server': r'sql server|mssql', 'mongodb': r'mongo(?:db)?', 'redis': r'redis',
    'cassandra': r'cassandra', 'dynamodb': r'dynamo\s*db', 'snowflake': r'snowflake', 'nosql': r'nosql',
    'aws': r'aws|amazon web services', 'oci': r'oci|oracle cloud', 'azure': r'azure', 'gcp': r'gcp|google cloud',
    'docker': r'docker|containers?', 'kubernetes': r'kubernetes|k8s|eks|aks|gke', 'terraform': r'terraform',
    'ansible': r'ansible', 'helm': r'helm', 'jenkins': r'jenkins', 'ci/cd': r'ci\s*/\s*cd|continuous integration',
    'git': r'git', 'linux': r'linux|unix', 'machine learning': r'machine learning|\bml\b', 'llm': r'llms?|genai|generative ai',
}
# Skills that do the same job, used only to point out "different tool" gaps in the report (not in the score)
SKILL_GROUPS = (
    {'aws', 'gcp', 'azure', 'oci'},
    {'java', 'python', 'ruby', 'go', 'rust', 'c++', 'c#', '.net', 'kotlin', 'scala', 'php', 'node.js'},
    {'javascript', 'typescript', 'react', 'angular', 'vue', 'd3.js', 'html', 'css'},
    {'spring', 'django', 'flask', 'fastapi', 'rails', 'hibernate', '.net', 'node.js'},
    {'kafka', 'storm', 'spark', 'flink', 'hadoop', 'mapreduce', 'oozie', 'airflow', 'etl', 'data pipelines'},
    {'sql', 'mysql', 'postgresql', 'oracle db', 'db2', 'sql server', 'snowflake'},
    {'nosql', 'mongodb', 'redis', 'cassandra', 'dynamodb', 'hbase', 'elasticsearch'},
    {'docker', 'kubernetes', 'terraform', 'ansible', 'helm'},
    {'jenkins', 'ci/cd', 'git'},
    {'rest api', 'soap', 'graphql', 'grpc'},
)
DEGREE_NAMES = {1: "bachelor", 2: "master", 3: "PhD"}
ANY_OF = re.compile(r'\bor\b|\beither\b|\bone of\b|\bany of\b')

YEARS = re.compile(r'(\d{1,2})\s*\+?\s*(?:(?:-|–|to)\s*\d{1,2}\s*\+?\s*)?(?:years?|yrs?)\b')
DEGREE_LEVELS = (  # (level, regex); a line needs the lowest level it mentions ("bachelor's or master's" -> 1)
    (1, re.compile(r"bachelor|\bb\.?s\.?c?\b|\bb\.?a\.?\b|undergraduate")),
    (2, re.compile(r"master|\bm\.?s\.?c?\b|\bm\.?eng\b|graduate degree")),
    (3, re.compile(r"ph\.?\s?d|doctorate")),
)
DEGREE_FIELDS = {
    'computer science': r'computer science|\bcs\b', 'computer engineering': r'computer engineering',
    'software engineering': r'software engineering', 'electrical engineering': r'electrical|\bee\b',
    'electronics': r'electronics', 'information technology': r'information (?:technology|systems)|\bit\b',
    'mathematics': r'math(?:ematics)?\b|applied math', 'statistics': r'statistics', 'physics': r'physics',
    'data science': r'data science', 'aerospace': r'aerospace', 'mechanical engineering': r'mechanical',
    'chemistry': r'chemistry', 'biology': r'biology|bioinformatics',
}
ANY_FIELD = re.compile(r'related|equivalent|similar|stem\b|technical (?:field|discipline)|quantitative')

# Words that carry no fit signal: English filler, generic job-posting words and soft skills
GENERIC_WORDS = set(""" 
a about above across after all also an and any are as at be being both but by can could do does etc for from has have
having in including into is it its like may more most must not of on one or other our own per plus such than that the
their them then there these this those through to under up using via was we well were what when where which while who
will with within without would you your very properly triggers audiences diverse critical possess strategies vital over
informed rules data interest management desirable
ability able experience experienced expertise exposure familiarity familiar knowledge proficiency proficient strong
solid deep excellent good great proven demonstrated hands work working years year yrs plus preferred required
requirement requirements qualification qualifications minimum bonus nice role position candidate team teams company
skill skills understanding background environment environments professional industry related equivalent field degree
graduate concepts concept technologies technology tools tool level high new various multiple based least ideally
software engineering engineer engineers development developing develop developer build building built design designing
systems system application applications solution solutions product products code coding requires require need needs
quality foundation ownership bias action sense self motivated passion passionate attitude curious curiosity learn
learning communication communicate collaborate collaboration collaborative interpersonal written verbal detail oriented
problem problems solving solve fast paced comfort comfortable reasoning terms independently independent mindset
bachelor bachelors master masters phd doctorate undergraduate education diploma university college school
practice practices language languages modern business deliver delivering delivery similar effective effectively
information maintain maintaining full end record track fundamentals fundamental advanced relevant discipline
disciplines highly provide providing use part non clear current currently leading demonstrate prefer drive driving core
quickly focus focused open combination willing willingness challenging challenges contribute contributing person task
tasks state direct directly best standard standards improve improving identify issue issues organization real
decision decisions strategic attention responsible responsibilities partner partners priorities efficient continue
continuous lifecycle principles leadership authorization authorized assist presentation just set related area areas
love enjoy evidence sharp thing things pick velocity discomfort know knowing think carefully limit limits already
agency eager excited hungry grow growth wear hats ambiguity thrive player because could couldn didn made something yet
""".split())
_COMPILED = None


def _compiled_skills():
    global _COMPILED
    if _COMPILED is None:
        _COMPILED = {skill: re.compile(rf'(?<![a-z0-9])(?:{pattern})(?![a-z0-9+#])') for skill, pattern in SKILL_PATTERNS.items()}
    return _COMPILED


def _term_regex(term):
    return re.compile(rf'(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])')


def find_skills(text, extra_terms=()):
    # Canonical skill names mentioned in text, plus any extra literal terms (e.g. skills listed in the resume)
    text = (text or '').lower()
    found = {skill for skill, regex in _compiled_skills().items() if regex.search(text)}
    found.update(term for term in extra_terms if _term_regex(term).search(text))
    return found


def _remove_skills(text, extra_terms=()):
    # The text with skill mentions blanked out, so they aren't counted again as keywords
    text = (text or '').lower()
    for regex in list(_compiled_skills().values()) + [_term_regex(term) for term in extra_terms]:
        text = regex.sub(' ', text)
    return text


def keyword_words(text):
    """{stem: original word} for the meaningful words in text ("deploying" -> "deploy"), without generic or soft-skill words."""
    stems = {}
    for word in re.findall(r'[a-z][a-z0-9]+', (text or '').lower()):
        if len(word) < 3 or word in GENERIC_WORDS:
            continue
        singular = word[:-1] if len(word) > 3 and word.endswith('s') else word
        if singular not in GENERIC_WORDS:
            stems.setdefault(singular[:6], word)
    return stems


def keywords(text):
    """Set of rough word stems in text (see keyword_words)."""
    return set(keyword_words(text))


def similar_skills(skill, have):
    # Skills from `have` in the same group as `skill` (job wants GCP, resume has AWS -> {'aws'})
    return {other for group in SKILL_GROUPS if skill in group for other in group if other in have and other != skill}


def required_years(text):
    # Lower bound of the first "N years" in the line, or None ("6-8+ years" -> 6)
    match = YEARS.search((text or '').lower())
    return int(match.group(1)) if match else None


def degree_level(text):
    # Lowest degree level named in the text (1 bachelor, 2 master, 3 PhD), or None
    text = (text or '').lower()
    levels = [level for level, regex in DEGREE_LEVELS if regex.search(text)]
    return min(levels) if levels else None


def degree_fields(text):
    # Fields of study named in the text ("BS in Physics or Math" -> {'physics', 'mathematics'})
    text = (text or '').lower()
    return {field for field, pattern in DEGREE_FIELDS.items() if re.search(pattern, text)}


def _parse_month(value, today):
    if not value or str(value).lower() == 'present':
        return today.year, today.month
    year, _, month = str(value).partition('-')
    return int(year), int(month or 1)


def years_of_experience(experience, today=None):
    # Span from the earliest start to the latest end across all jobs, in whole years
    today = today or date.today()
    months = []
    for job in experience:
        if not job.get('start'):
            continue
        sy, sm = _parse_month(job['start'], today)
        ey, em = _parse_month(job.get('end'), today)
        months.append((sy * 12 + sm, ey * 12 + em))
    if not months:
        return 0
    return (max(end for _, end in months) - min(start for start, _ in months)) // 12


def _clean_term(item):
    # "Ruby (on Rails)" -> "ruby"; drop long phrases that are descriptions rather than skills
    term = re.sub(r'\(.*?\)', '', item).strip().lower()
    return term if term and len(term.split()) <= 3 else None


def load_resume_profile(resume):
    """Build {'skills', 'extra_terms', 'keywords', 'years', 'degree', 'fields'} from a resume dict (resume.json schema)."""
    if not isinstance(resume, dict):
        raise ValueError("Resume must be a JSON object.")
    experience = resume.get('experience') or []
    education = resume.get('education') or []
    listed = [item for group in resume.get('skills') or [] for item in group.get('items', [])]
    listed += [item for job in experience for item in job.get('tech', [])]
    extra_terms = {term for term in map(_clean_term, listed) if term}

    texts = listed + [(resume.get('summary') or {}).get('text', ''), (resume.get('basics') or {}).get('label', '')]
    texts += [job.get('title', '') for job in experience]
    for section in ('experience', 'projects'):
        texts += [bullet.get('text', '') for entry in resume.get(section) or [] for bullet in entry.get('bullets', [])]
    degree_texts = [f"{e.get('degree', '')} {e.get('field', '')}" for e in education]
    all_text = '\n'.join(texts + degree_texts)

    return {
        'skills': find_skills(all_text) | extra_terms,
        'extra_terms': extra_terms,
        'keywords': keywords(all_text),
        'years': years_of_experience(experience),
        'degree': max([d for d in map(degree_level, degree_texts) if d] or [0]),
        'fields': set().union(*map(degree_fields, degree_texts)) if degree_texts else set(),
    }


def split_lines(section_text):
    # job_details stores one "- item" per line; also handles plain paragraphs
    return [line.strip().lstrip('-•* ').strip() for line in (section_text or '').splitlines() if line.strip()]


def _degree_ok(line, profile):
    level = degree_level(line)
    if level is None:
        return None
    if profile['degree'] < level:
        return False
    fields = degree_fields(line)
    return not fields or bool(fields & profile['fields']) or bool(ANY_FIELD.search(line.lower()))


def explain_line(line, profile):
    """
    How the resume covers one requirement/preference line:
    {'line', 'credit' (0-1, None if nothing to check), 'matched_skills', 'missing_skills', 'alternatives' (other options
    in an "X or Y" line you don't need), 'similar' ({missing skill: resume skills that do the same job}),
    'matched_words', 'missing_words', 'years' (required, have, ok) or None, 'degree' (level, fields, ok) or None}
    """
    lowered = line.lower()
    years = required_years(line)
    degree_ok = _degree_ok(line, profile)
    gates = [ok for ok in (None if years is None else profile['years'] >= years, degree_ok) if ok is not None]

    parts = []  # (weight, credit)
    skills = find_skills(line, profile['extra_terms'])
    have = skills & profile['skills']
    any_of = bool(have) and bool(ANY_OF.search(lowered))
    if skills:
        parts.append((SKILLS_WEIGHT, 1.0 if any_of else len(have) / len(skills)))
    words = keyword_words(_remove_skills(line, profile['extra_terms']))
    if degree_ok:  # field names in a passed degree line are already checked
        for stem in keywords(' '.join(degree_fields(line))):
            words.pop(stem, None)
    matched_words = sorted(word for stem, word in words.items() if stem in profile['keywords'])
    missing_words = sorted(word for stem, word in words.items() if stem not in profile['keywords'])
    if words:
        parts.append((1 - SKILLS_WEIGHT if skills else 1.0, len(matched_words) / len(words)))

    if not gates and not parts:
        credit = None
    elif not all(gates):
        credit = 0.0
    elif not parts:
        credit = 1.0
    else:
        credit = sum(w * c for w, c in parts) / sum(w for w, _ in parts)

    missing = sorted(skills - have)
    return {
        'line': line,
        'credit': credit,
        'matched_skills': sorted(have),
        'missing_skills': [] if any_of else missing,
        'alternatives': missing if any_of else [],
        'similar': {} if any_of else {s: sorted(similar_skills(s, profile['skills'])) for s in missing
                                      if similar_skills(s, profile['skills'])},
        'matched_words': matched_words,
        'missing_words': missing_words,
        'years': None if years is None else (years, profile['years'], profile['years'] >= years),
        'degree': None if degree_ok is None else (degree_level(line), sorted(degree_fields(line)), degree_ok),
    }


def match_line(line, profile):
    """Credit 0-1 for how well the resume covers the line, or None if the line has nothing to check."""
    return explain_line(line, profile)['credit']


def section_credits(section_text, profile):
    # Credits of the checkable lines in a requirements/preferences section
    return [c for c in (match_line(line, profile) for line in split_lines(section_text)) if c is not None]


def score_job(requirements, preferences, profile):
    """ATS score 0-100, or None when neither section has a checkable line."""
    req = section_credits(requirements, profile)
    pref = section_credits(preferences, profile)
    if not req and not pref:
        return None
    req_score = sum(req) / len(req) if req else None
    pref_score = sum(pref) / len(pref) if pref else None
    if req_score is None:
        score = pref_score
    elif pref_score is None:
        score = req_score
    else:
        score = REQUIREMENTS_WEIGHT * req_score + PREFERENCES_WEIGHT * pref_score
    score = round(score * 100)
    if sum(c < MISS_CREDIT for c in req) >= KNOCKOUT_MISSES:
        score = min(score, KNOCKOUT_CAP)
    return score


def explain_job(requirements, preferences, profile):
    """{'score', 'requirements': [explain_line...], 'preferences': [...]} for one job."""
    return {
        'score': score_job(requirements, preferences, profile),
        'requirements': [explain_line(line, profile) for line in split_lines(requirements)],
        'preferences': [explain_line(line, profile) for line in split_lines(preferences)],
    }


def _unique(items):
    # Items in first-seen order without duplicates
    return list(dict.fromkeys(items))


def job_keywords(requirements, preferences, profile):
    """{'matched': [...], 'gaps': [...]} over a job's checkable lines: skills and words the resume covers, and the
    ones it doesn't (unmet years/degree included as gaps). A term matched on any line isn't listed as a gap."""
    matched, gaps = [], []
    for section in (requirements, preferences):
        for e in (explain_line(line, profile) for line in split_lines(section)):
            if e['credit'] is None:
                continue
            matched += e['matched_skills'] + e['matched_words']
            gaps += e['missing_skills'] + e['missing_words']
            if e['years'] and not e['years'][2]:
                gaps.append(f"{e['years'][0]}+ years")
            if e['degree'] and not e['degree'][2]:
                gaps.append(DEGREE_NAMES[e['degree'][0]] + "'s degree" if e['degree'][0] < 3 else "PhD")
    matched = _unique(matched)
    return {'matched': matched, 'gaps': [term for term in _unique(gaps) if term not in matched]}


def _format_line(e):
    if e['credit'] is None:
        return f"- `skip` {e['line']}"
    notes = []
    if e['matched_skills']:
        notes.append("✓ " + ", ".join(e['matched_skills']))
    for skill in e['missing_skills']:
        similar = e['similar'].get(skill)
        notes.append(f"✗ {skill}" + (f" (you have: {', '.join(similar)})" if similar else ""))
    if e['alternatives']:
        notes.append("or-list, not needed: " + ", ".join(e['alternatives']))
    if e['years']:
        need, have, ok = e['years']
        notes.append(f"{'✓' if ok else '✗'} {need}+ yrs (you: {have})")
    if e['degree']:
        level, fields, ok = e['degree']
        name = DEGREE_NAMES[level]
        notes.append(f"{'✓' if ok else '✗'} {name}" + (f" in {'/'.join(fields)}" if fields else ""))
    if e['matched_words']:
        notes.append("words ✓ " + ", ".join(e['matched_words']))
    if e['missing_words']:
        notes.append("words ✗ " + ", ".join(e['missing_words']))
    return f"- `{e['credit']:.2f}` {e['line']}\n  - " + " · ".join(notes)


def build_report(jobs, profile, top_gaps=40):
    """
    Markdown report. jobs: (job_id, title, company, requirements, preferences) rows.
    Starts with the gaps that cost points most often across all jobs, then every job's lines from lowest score up.
    """
    explained = [(job_id, title, company, explain_job(req, pref, profile)) for job_id, title, company, req, pref in jobs]
    explained = [row for row in explained if row[3]['score'] is not None]
    explained.sort(key=lambda row: (row[3]['score'], row[0]))

    skill_gaps, word_gaps = {}, {}
    for job_id, _, _, result in explained:
        for e in result['requirements'] + result['preferences']:
            for skill in e['missing_skills']:
                skill_gaps.setdefault(skill, set()).add(job_id)
            for word in e['missing_words']:
                word_gaps.setdefault(word, set()).add(job_id)

    out = [f"# ATS match report\n\n{len(explained)} jobs · resume: {profile['years']} years, "
           f"{len(profile['skills'])} skills\n",
           "## Most common skill gaps\n", "| Skill | Jobs | You have similar |", "|---|---|---|"]
    for skill, ids in sorted(skill_gaps.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:top_gaps]:
        out.append(f"| {skill} | {len(ids)} | {', '.join(sorted(similar_skills(skill, profile['skills']))) or '-'} |")
    out += ["\n## Most common missing words\n", "| Word | Jobs |", "|---|---|"]
    out += [f"| {word} | {len(ids)} |" for word, ids in sorted(word_gaps.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:top_gaps]]
    out.append("\n## Jobs (lowest score first)\n")
    for job_id, title, company, result in explained:
        out.append(f"### {result['score']} · {title} · {company} (job {job_id})\n")
        for name in ('requirements', 'preferences'):
            if result[name]:
                out.append(f"**{name.capitalize()}**\n")
                out += [_format_line(e) for e in result[name]]
                out.append("")
    return "\n".join(out) + "\n"


def load_resume(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        raise ValueError(f"Resume file not found: {path}")
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in {path}: {e}")


def _read_jobs(config, job_ids=None):
    conn = open_db(config)
    if conn is None:
        raise ValueError("Cannot create the database connection.")
    try:
        rows = read_visible_job_sections(conn, config['jobs_tablename'])
    finally:
        conn.close()
    return [row for row in rows if not job_ids or row[0] in job_ids]


def write_report(config, resume_path='resume.json', report_path='output/ats_report.md', job_ids=None):
    """Write the matched/gaps report for visible jobs (or only job_ids) to report_path. Returns the path."""
    profile = load_resume_profile(load_resume(resume_path))
    report = build_report(_read_jobs(config, job_ids), profile)
    os.makedirs(os.path.dirname(report_path) or '.', exist_ok=True)
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report)
    print(f"Wrote {report_path}")
    return report_path


def score_jobs(config, resume_path='resume.json', dry_run=False):
    """Score every visible job and save it to jobs.ats_score. Returns {job_id: score} for the scored jobs."""
    profile = load_resume_profile(load_resume(resume_path))
    jobs_tablename = config['jobs_tablename']
    conn = open_db(config)
    if conn is None:
        print("Error! cannot create the database connection.")
        return {}

    scores = {}
    try:
        for job_id, _, _, requirements, preferences in read_visible_job_sections(conn, jobs_tablename):
            score = score_job(requirements, preferences, profile)
            if score is not None:
                scores[job_id] = score
        if not dry_run:
            replace_ats_scores(conn, jobs_tablename, scores)
    finally:
        conn.close()

    print(f"{'Would score' if dry_run else 'Scored'} {len(scores)} jobs "
          f"(resume: {len(profile['skills'])} skills, {profile['years']} years); "
          f"average {sum(scores.values()) / len(scores):.0f}" if scores else "No jobs had checkable requirements")
    return scores


if __name__ == "__main__":
    from config import load_config  # here so tests can import this module without the scraper's dependencies

    parser = argparse.ArgumentParser(description="Score visible jobs against resume.json and save jobs.ats_score")
    parser.add_argument('config', nargs='?', default='config.json')
    parser.add_argument('--resume', default='resume.json')
    parser.add_argument('--dry-run', action='store_true', help="compute scores without saving them")
    parser.add_argument('--report', nargs='?', const='output/ats_report.md',
                        help="write matched skills and gaps per job to a Markdown file instead of scoring")
    parser.add_argument('--job', type=int, action='append', help="limit the report to this job id (repeatable)")
    args = parser.parse_args()
    if args.report:
        write_report(load_config(args.config), args.resume, args.report, args.job)
    else:
        score_jobs(load_config(args.config), args.resume, args.dry_run)
