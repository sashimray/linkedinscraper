"""
Matched keywords and gaps for one job, for the job page in app.py: one summary from the rule-based ats_score.py
(resume.json) and one from the embedding-based ats_score_embed.py (skills_flat.json). Runs locally only.
"""

import workflow.score.ats_score as ats_score
import workflow.score.ats_score_embed as ats_score_embed
from db.queries import read_job_item_matches, read_job_sections
from workflow.score.embedder import load_skills


def load_profiles(resume_path='resume.json', skills_path='skills_flat.json'):
    """{'rule': resume profile or None, 'embed': skill profile or None, 'errors': [...]} - a missing or invalid file
    disables that summary instead of failing."""
    profiles = {'rule': None, 'embed': None, 'errors': []}
    try:
        profiles['rule'] = ats_score.load_resume_profile(ats_score.load_resume(resume_path))
    except ValueError as e:
        profiles['errors'].append(str(e))
    try:
        profiles['embed'] = ats_score_embed.skill_profile(load_skills(skills_path))
    except ValueError as e:
        profiles['errors'].append(str(e))
    return profiles


def job_match(conn, job_id, profiles):
    """{'rule': {'matched', 'gaps'} or None, 'embed': {'matched', 'gaps'} or None} for one job.
    None when the profile is missing or the job has nothing to compare (no job_details / job_items rows)."""
    result = {'rule': None, 'embed': None}
    sections = read_job_sections(conn, job_id)
    if profiles.get('rule') is not None and sections is not None:
        result['rule'] = ats_score.job_keywords(sections[0], sections[1], profiles['rule'])
    lines = ats_score_embed.group_lines(read_job_item_matches(conn, job_id)).get(job_id)
    if profiles.get('embed') is not None and lines:
        result['embed'] = ats_score_embed.job_keywords(lines, profiles['embed'])
    return result
