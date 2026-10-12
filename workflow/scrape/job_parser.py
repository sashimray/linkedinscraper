import re
from bs4 import BeautifulSoup, Comment, NavigableString

# Parse structured fields out of a LinkedIn job page:
# company name, industry, and the responsibilities / requirements / preferred qualifications sections.

SECTION_FIELDS = ('responsibilities', 'requirements', 'preferred_qualifications')

# Heading text -> section. Checked in order, first match wins, so the more specific rules come first
# ("Preferred Qualifications" must hit preferred before "qualif" sends it to requirements).
# 'other' headings (benefits, pay, about us...) end the current section.
HEADING_RULES = [
    ('other', r"referral|sign.on bonus|annual bonus|bonus (plan|program|eligib)"),
    ('preferred_qualifications', r"prefer|desir|nice.to.have|nice if|bonus|\bplus\b|would be nice|non.?essential|good to have"
                                 r"|suggested|like you to have|we'?d love|would love|extra credit|stand out|also have"
                                 r"|top candidates"),
    ('other', r"benefit|pay range|salary|compensation|equal opportunit|\beeoc?\b|perks|why (join|choose|work)"
              r"|background check|about (us|the company|the team)|culture|privacy|disabilit|at.will"),
    # Before responsibilities so "To be successful in this role you will" isn't read as a duties list
    ('requirements', r"talents? needed|needed for success|to be successful|what it takes|tech(nology|nical)? (stack|expertise)"
                     r"|technologies you|strong candidates|drawn to engineers|who we hope|what you will have|^you are$"),
    ('responsibilities', r"responsib|duties|day.to.day|impact|what you'?(ll| will| would)?\s*(be\s+)?(do|doing|work|own|owning|build|accomplish)"
                         r"|what you'?ll be|your role|about the role|the role involves|key activities|activities"
                         r"|^(in this role, )?you'?(ll| will):?$|^in this role|trust you to|essential functions|count on you"
                         r"|^expect to|role objectives|the role you'?ll play"),
    ('requirements', r"requir|qualif|must.?have|should have|who you are|you'?(ll| will)? need|you'?(ll| will)?\s*bring"
                     r"|excited about you|about you|skills|experience|education|look(ing)? for|competenc|what you have"
                     r"|^you have|candidate profile"),
]

# Plain-text lines (not bold, no colon, no list after them) only count as headings with one of these words,
# so a short line like "Strong Java experience" isn't mistaken for one
PLAIN_HEADING_WORDS = r"responsib|duties|qualifications|requirements|preferred|nice.to.have|what you"

MAX_HEADING_WORDS = 12
MAX_HEADING_CHARS = 100

BLOCK_TAGS = {'p', 'div', 'ul', 'ol', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'br', 'section'}
EMPHASIS_TAGS = {'strong', 'b', 'u', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}


def parse_job_details(soup):
    # Returns company, company_industry, the three section fields, and sections_parsed
    # (1 if both responsibilities and requirements were found, else 0 so the LLM run picks it up later).
    details = {'company': '', 'company_industry': ''}
    details.update({field: '' for field in SECTION_FIELDS})
    details['sections_parsed'] = 0
    if soup is None:
        return details

    details.update(parse_company_info(soup))
    div = soup.find('div', class_='description__text description__text--rich')
    if div:
        details.update(parse_job_sections(div))
    details['sections_parsed'] = int(bool(details['responsibilities'] and details['requirements']))
    return details


def parse_company_info(soup):
    company = soup.find('a', class_='topcard__org-name-link')
    industry = ''
    for item in soup.find_all('li', class_='description__job-criteria-item'):
        label = item.find('h3')
        value = item.find('span')
        if label and value and label.get_text(strip=True).lower() == 'industries':
            industry = value.get_text(' ', strip=True)
    return {'company': company.get_text(' ', strip=True) if company else '', 'company_industry': industry}


DESCRIPTION_CLASS = 'description__text description__text--rich'
NOISE_TAGS = ('button', 'icon', 'script', 'style', 'svg', 'img')


def description_html(soup):
    """The job description markup worth storing: LinkedIn's description div without the show more/less buttons,
    icons, comments and attributes, keeping the tags that carry structure (strong, ul/li, br, p, h*). '' if missing.
    Works on a copy, so the page soup is left as it was."""
    if soup is None:
        return ''
    div = soup.find('div', class_=DESCRIPTION_CLASS)
    if div is None:
        return ''
    markup = div.find('div', class_='show-more-less-html__markup') or div
    copy = BeautifulSoup(markup.decode_contents(), 'html.parser')
    for tag in copy.find_all(NOISE_TAGS):
        tag.decompose()
    for comment in copy.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()
    for tag in copy.find_all(True):
        tag.attrs = {}
    return copy.decode().strip()


def parse_description_html(html):
    """Sections from stored description_html, the same way parse_job_details reads the live page."""
    sections = _sections_from_blocks(_to_blocks(BeautifulSoup(f'<div>{html or ""}</div>', 'html.parser').div))
    sections['sections_parsed'] = int(bool(sections['responsibilities'] and sections['requirements']))
    return sections


def parse_job_text(text):
    # Same as the section part of parse_job_details, for the plain job_description text stored by transform_job.
    # That text has no bold, so headings marked only by bold are missed and their items land in the previous section or nowhere.
    sections = _sections_from_blocks(_text_blocks(text or ''))
    sections['sections_parsed'] = int(bool(sections['responsibilities'] and sections['requirements']))
    return sections


def parse_job_sections(div):
    return _sections_from_blocks(_to_blocks(div))


def _sections_from_blocks(blocks):
    # Walk the description as a list of lines and list items, and file each one under the last recognised heading
    sections = {field: [] for field in SECTION_FIELDS}
    current = None
    seen_li = False
    for i, (kind, text, emphasised) in enumerate(blocks):
        next_is_li = i + 1 < len(blocks) and blocks[i + 1][0] == 'li'
        if kind == 'line' and _is_heading(text, emphasised, next_is_li):
            current = classify_heading(text)
            seen_li = False
            continue
        if current not in sections:
            continue
        # Prose after a section's bullet list, or prose about pay/benefits/EEO, is usually boilerplate: end the section
        if kind == 'line' and (seen_li or _matches(text, 'other')):
            current = None
            continue
        seen_li = seen_li or kind == 'li'
        sections[current].append('- ' + text if kind == 'li' else text)
    return {field: '\n'.join(lines) for field, lines in sections.items()}


def classify_heading(text):
    text = _normalise_quotes(text).lower()
    for section, pattern in HEADING_RULES:
        if re.search(pattern, text):
            return section
    return 'other'


def _matches(text, section):
    pattern = next(pattern for name, pattern in reversed(HEADING_RULES) if name == section)
    return bool(re.search(pattern, _normalise_quotes(text).lower()))


def _is_heading(text, emphasised, next_is_li):
    if len(text) > MAX_HEADING_CHARS or len(text.split()) > MAX_HEADING_WORDS:
        return False
    if emphasised or text.endswith((':', '-', '…', '...')):
        return True
    if text.endswith(('.', '!', '?', ',', ';')):
        return False
    return next_is_li or bool(re.search(PLAIN_HEADING_WORDS, _normalise_quotes(text).lower()))


def _to_blocks(div):
    # Flatten the description into ('line' | 'li', text, emphasised). A line is emphasised when all of its text is bold/underlined/a heading.
    blocks = []
    line = []  # (text, emphasised) pieces of the line being built

    def flush():
        text = _clean(' '.join(piece for piece, _ in line))
        if text:
            blocks.append(('line', text, all(emph for _, emph in line)))
        line.clear()

    def walk(node, emphasised):
        for child in node.children:
            if isinstance(child, NavigableString):
                if _clean(child):
                    line.append((str(child), emphasised))
            elif child.name == 'li':
                flush()
                text = _clean(child.get_text(' '))
                if text:
                    blocks.append(('li', text, False))
            elif child.name in BLOCK_TAGS:
                flush()
                walk(child, emphasised or child.name in EMPHASIS_TAGS)
                flush()
            else:
                walk(child, emphasised or child.name in EMPHASIS_TAGS)

    walk(div, False)
    flush()
    return blocks


def _text_blocks(text):
    # transform_job writes list items as "- item" lines
    blocks = []
    for line in text.split('\n'):
        line = _clean(line)
        if line.startswith('- '):
            blocks.append(('li', line[2:].strip(), False))
        elif re.search(r'\w', line):  # skip punctuation left behind where transform_job removed links
            blocks.append(('line', line, False))
    return blocks


def _clean(text):
    text = re.sub(r'\s+', ' ', str(text)).strip()
    return text.replace('Show more', '').replace('Show less', '').strip()


def _normalise_quotes(text):
    return text.replace('’', "'").replace('‘', "'")
