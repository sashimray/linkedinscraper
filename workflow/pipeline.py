"""
Run the whole pipeline in order, stopping at the first step that fails:

  1. scrape       workflow/scrape/scraper.py: new LinkedIn jobs -> jobs table (job_description text and job_description_html)
  2. hide         workflow/hide.py: hide (hidden = 1) visible jobs whose description needs a security clearance or US citizenship
  3. parse        workflow/parse.py: visible jobs without a job_details row -> job_details, split from job_description_html
                  (or the text, for jobs scraped before the HTML was stored); sections_parsed = 1 when both
                  responsibilities and requirements were found, else 0. With --llm, the LLM also fills the 0s (= 2)
  4. score        workflow/score/ats_score.py: jobs.ats_score from resume.json
  5. embed        workflow/score/embedder.py: job_details lines -> job_items, embeddings and skill matches (needed by score_embed)
  6. score_embed  workflow/score/ats_score_embed.py: jobs.ats_score_embed from the embedder matches and skills_flat.json

  python -m workflow                          # every step, config.json
  python -m workflow --skip scrape            # rescore what's already in the database
  python -m workflow --only embed --only score_embed
  python -m workflow --llm ollama             # also fill unsplit jobs with the local LLM

Everything runs locally except the scrape and, with --llm claude, the job descriptions sent to the Claude API.
"""

import argparse
import sys
import time

DEFAULT_RESUME = 'resume.json'
DEFAULT_SKILLS = 'skills_flat.json'


def step_scrape(_config, options):
    from workflow.scrape.scraper import main as scrape
    scrape(options.config)


def step_parse(config, options):
    from workflow.parse import parse_missing_details
    parse_missing_details(config)
    if options.llm:
        import workflow.llm_sections as llm_sections
        from resumekit.application_kit import ApplicationKit
        llm_sections.run(config, ApplicationKit(options.llm, options.llm_model), config['jobs_tablename'])


def step_hide(config, _options):
    from workflow.hide import hide_clearance_jobs
    hide_clearance_jobs(config)


def step_embed(config, options):
    import workflow.score.embedder as embedder
    embedder.run(config['db_path'], options.skills, options.model or embedder.DEFAULT_MODEL)


def step_score(config, options):
    import workflow.score.ats_score as ats_score
    ats_score.score_jobs(config, options.resume)


def step_score_embed(config, options):
    import workflow.score.ats_score_embed as ats_score_embed
    ats_score_embed.score_jobs(config, options.skills)


STEPS = {
    'scrape': step_scrape,
    'hide': step_hide,
    'parse': step_parse,
    'score': step_score,
    'embed': step_embed,
    'score_embed': step_score_embed,
}


def selected_steps(only=None, skip=None):
    """Step names to run, in pipeline order."""
    unknown = (set(only or ()) | set(skip or ())) - set(STEPS)
    if unknown:
        raise ValueError(f"Unknown steps: {', '.join(sorted(unknown))}")
    return [name for name in STEPS if (not only or name in only) and name not in (skip or ())]


def run_workflow(config, options, steps, step_funcs=STEPS):
    """Run steps in order. Returns [(name, ok, seconds)]; stops after the first failure."""
    results = []
    for name in steps:
        print(f"\n=== {name} ===")
        start = time.perf_counter()
        try:
            step_funcs[name](config, options)
            ok = True
        except (Exception, SystemExit) as e:  # SystemExit: some steps' helpers exit on bad input
            print(f"Step '{name}' failed: {type(e).__name__}: {e}")
            ok = False
        results.append((name, ok, time.perf_counter() - start))
        if not ok:
            break
    return results


def print_summary(results, steps):
    print("\n=== summary ===")
    done = {name: (ok, seconds) for name, ok, seconds in results}
    for name in steps:
        if name in done:
            ok, seconds = done[name]
            print(f"{name:12} {'ok' if ok else 'FAILED':7} {seconds:6.1f}s")
        else:
            print(f"{name:12} skipped (an earlier step failed)")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('config', nargs='?', default='config.json')
    parser.add_argument('--only', action='append', metavar='STEP', help=f"run only this step (repeatable): {', '.join(STEPS)}")
    parser.add_argument('--skip', action='append', metavar='STEP', help="skip this step (repeatable)")
    parser.add_argument('--resume', default=DEFAULT_RESUME, help=f"resume JSON for ats_score (default: {DEFAULT_RESUME})")
    parser.add_argument('--skills', default=DEFAULT_SKILLS, help=f"skills for the embedder (default: {DEFAULT_SKILLS})")
    parser.add_argument('--model', help="sentence-transformers model for the embedder (default: embedder.DEFAULT_MODEL)")
    parser.add_argument('--llm', choices=['ollama', 'claude'], help="also split unparsed jobs with this LLM in the parse step")
    parser.add_argument('--llm-model', help="model for --llm (default: llm_sections' default)")
    options = parser.parse_args(argv)

    try:
        steps = selected_steps(options.only, options.skip)
    except ValueError as e:
        parser.error(str(e))
    from config import load_config
    try:
        config = load_config(options.config)
    except (OSError, ValueError) as e:
        print(f"Could not read {options.config}: {e}")
        return 1

    results = run_workflow(config, options, steps)
    print_summary(results, steps)
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
