"""
ApplicationKit - tailor a job application with a local Ollama model or the Claude API.

Two tasks:
  1. suggest_rewording()     -> JSON with reword suggestions for resume bullets, based on a job description
  2. generate_cover_letter() -> JSON (+ .txt) cover letter built from the master resume and a template

Usage (from the repo root):
  python -m resumekit.application_kit reword --resume resume.json --jd-file jd.txt
  python -m resumekit.application_kit cover  --resume resume.json --template cl.json --job-id 42 --provider claude
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime

import requests

DEFAULT_OLLAMA_MODEL = "llama3.1:8b"
DEFAULT_CLAUDE_MODEL = "claude-opus-5"
DEFAULT_OLLAMA_URL = "http://localhost:11434"
# Server-side refusal fallback: if Claude declines a request, the API re-runs it on a fallback model.
CLAUDE_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ApplicationKitError(Exception):
    """Raised for bad input files or failed model calls, with a message meant for the user."""


# ---------------------------------------------------------------------------
# Resume helpers
# ---------------------------------------------------------------------------

def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise ApplicationKitError(f"File not found: {path}")
    except json.JSONDecodeError as e:
        raise ApplicationKitError(f"Invalid JSON in {path}: {e}")


def validate_resume(resume):
    """Check the resume follows the schema in resumekit/examples/resume.example.json."""
    if not isinstance(resume, dict):
        raise ApplicationKitError("Resume must be a JSON object.")
    if not resume.get("basics", {}).get("name"):
        raise ApplicationKitError("Resume is missing basics.name.")
    if not isinstance(resume.get("experience", []), list):
        raise ApplicationKitError("Resume 'experience' must be a list.")
    resume_texts(resume)  # raises on missing or duplicate ids


def resume_texts(resume):
    """Map every rewordable id (summary + experience/project bullets) to its text."""
    texts = {}

    def add(item_id, text, where):
        if not item_id:
            raise ApplicationKitError(f"Missing 'id' in {where}.")
        if item_id in texts:
            raise ApplicationKitError(f"Duplicate id '{item_id}' in resume.")
        texts[item_id] = text or ""

    summary = resume.get("summary")
    if summary:
        add(summary.get("id"), summary.get("text"), "summary")
    for section in ("experience", "projects"):
        for i, entry in enumerate(resume.get(section, [])):
            for j, bullet in enumerate(entry.get("bullets", [])):
                add(bullet.get("id"), bullet.get("text"), f"{section}[{i}].bullets[{j}]")
    return texts


def _normalize(text):
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _slug(text, default="job"):
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:40] or default


def _fill(template, values):
    # Plain replace instead of str.format so stray braces in templates don't break anything
    for key, value in values.items():
        template = template.replace("{" + key + "}", value or "")
    return template


# ---------------------------------------------------------------------------
# Job description input
# ---------------------------------------------------------------------------

def load_job(jd=None, jd_file=None, job_id=None, config_path="config.json", title="", company=""):
    """Return {'description', 'title', 'company'} from text, a .txt file, or the scraper's jobs table."""
    if job_id is not None:
        config = load_json(config_path)
        table = config.get("jobs_tablename", "jobs")
        conn = sqlite3.connect(config["db_path"])
        try:
            row = conn.execute(
                f'SELECT job_description, title, company FROM "{table}" WHERE id = ?', (job_id,)
            ).fetchone()
        except sqlite3.OperationalError as e:
            raise ApplicationKitError(f"Could not read the '{table}' table in {config['db_path']}: {e}")
        finally:
            conn.close()
        if row is None:
            raise ApplicationKitError(f"No job with id {job_id} in the '{table}' table.")
        return {"description": row[0] or "", "title": title or row[1], "company": company or row[2]}

    if jd_file:
        try:
            with open(jd_file, encoding="utf-8") as f:
                jd = f.read()
        except FileNotFoundError:
            raise ApplicationKitError(f"File not found: {jd_file}")
    if not jd or not jd.strip():
        raise ApplicationKitError("Provide a job description with --jd, --jd-file or --job-id.")
    return {"description": jd.strip(), "title": title, "company": company}


# ---------------------------------------------------------------------------
# Prompts and output schemas
# ---------------------------------------------------------------------------

REWORD_SYSTEM = """You are an expert technical resume editor. You suggest rewordings of existing resume content so it matches a specific job description.

Rules:
- Only rephrase what is already in the resume. Never invent employers, tools, technologies, metrics or responsibilities the candidate has not listed.
- Where a number would strengthen a bullet but none is given, use a placeholder like [X%] or [N users] for the candidate to fill in.
- Mirror the job description's terminology where it truthfully describes the candidate's experience.
- Each suggestion must use a target_id from the provided list, and "original" must be copied exactly from that id's text.
- Prioritise the 5-12 changes with the biggest impact on fit for this role. Skip content that already fits well.
- missing_keywords: important skills/terms from the job description that do not appear in the resume.
- skills_to_add: items from missing_keywords the candidate plausibly has based on their resume (leave empty if none)."""

REWORD_SCHEMA = {
    "type": "object",
    "properties": {
        "missing_keywords": {"type": "array", "items": {"type": "string"}},
        "skills_to_add": {"type": "array", "items": {"type": "string"}},
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "target_id": {"type": "string"},
                    "original": {"type": "string"},
                    "suggested": {"type": "string"},
                    "reason": {"type": "string"},
                    "keywords": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["target_id", "original", "suggested", "reason", "keywords"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["missing_keywords", "skills_to_add", "suggestions"],
    "additionalProperties": False,
}

COVER_SYSTEM = """You are an experienced career coach writing a cover letter for the candidate whose resume is provided.

Rules:
- Use only facts from the resume. Never invent experience, employers, metrics or credentials.
- Write only the body sections requested, each following its instructions. Do not include a greeting, sign-off or the candidate's name; those are added separately.
- Keep the whole body within the word limit given.
- Plain text only: no markdown, no bullet characters, no placeholders in brackets.
- Focus on what the candidate can contribute to this company, not a list of accomplishments."""


def cover_letter_schema(section_names):
    return {
        "type": "object",
        "properties": {name: {"type": "string"} for name in section_names},
        "required": list(section_names),
        "additionalProperties": False,
    }


# ---------------------------------------------------------------------------
# ApplicationKit
# ---------------------------------------------------------------------------

class ApplicationKit:
    def __init__(self, provider="ollama", model=None, api_key=None, ollama_url=DEFAULT_OLLAMA_URL,
                 output_dir="output", timeout=600, generate_fn=None):
        """
        provider:    "ollama" (local) or "claude"
        model:       defaults to llama3.1:8b for Ollama, claude-opus-5 for Claude
        api_key:     Claude only; if omitted the SDK reads ANTHROPIC_API_KEY (or an `ant auth login` profile)
        generate_fn: optional fn(system, user, schema) -> str, replaces the model call (used in tests)
        """
        if provider not in ("ollama", "claude"):
            raise ApplicationKitError(f"Unknown provider '{provider}'. Use 'ollama' or 'claude'.")
        self.provider = provider
        self.model = model or (DEFAULT_OLLAMA_MODEL if provider == "ollama" else DEFAULT_CLAUDE_MODEL)
        self.api_key = api_key
        self.ollama_url = ollama_url.rstrip("/")
        self.output_dir = output_dir
        self.timeout = timeout
        self._generate_fn = generate_fn
        self._claude_client = None

    # --- model calls -------------------------------------------------------

    def _generate(self, system, user, schema=None):
        if self._generate_fn:
            return self._generate_fn(system, user, schema)
        if self.provider == "ollama":
            return self._call_ollama(system, user, schema)
        return self._call_claude(system, user, schema)

    def _call_ollama(self, system, user, schema):
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False,
            # Ollama's default context is small and silently truncates long resumes + job descriptions
            "options": {"temperature": 0.3, "num_ctx": 16384},
        }
        if schema:
            payload["format"] = schema
        try:
            r = requests.post(f"{self.ollama_url}/api/chat", json=payload, timeout=self.timeout)
        except requests.exceptions.ConnectionError:
            raise ApplicationKitError(f"Cannot reach Ollama at {self.ollama_url}. Start it with `ollama serve`.")
        except requests.exceptions.Timeout:
            raise ApplicationKitError(f"Ollama did not respond within {self.timeout}s.")
        if r.status_code == 404:
            raise ApplicationKitError(f"Ollama model '{self.model}' not found. Run `ollama pull {self.model}`.")
        if r.status_code != 200:
            raise ApplicationKitError(f"Ollama error {r.status_code}: {r.text[:300]}")
        return r.json()["message"]["content"]

    def _call_claude(self, system, user, schema):
        try:
            import anthropic
        except ImportError:
            raise ApplicationKitError("The 'anthropic' package is not installed. Run `pip install anthropic`.")

        if self._claude_client is None:
            self._claude_client = anthropic.Anthropic(api_key=self.api_key) if self.api_key else anthropic.Anthropic()

        kwargs = {
            "model": self.model,
            "max_tokens": 16000,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "betas": [CLAUDE_FALLBACK_BETA],
            "fallbacks": "default",
        }
        if schema:
            kwargs["output_config"] = {"format": {"type": "json_schema", "schema": schema}}

        try:
            response = self._claude_client.beta.messages.create(**kwargs)
        except anthropic.AuthenticationError:
            raise ApplicationKitError("Claude API key is invalid or missing. Set ANTHROPIC_API_KEY or pass --api-key.")
        except anthropic.NotFoundError:
            raise ApplicationKitError(f"Claude model '{self.model}' not found.")
        except anthropic.RateLimitError:
            raise ApplicationKitError("Claude API rate limit reached. Try again shortly.")
        except anthropic.APIStatusError as e:
            raise ApplicationKitError(f"Claude API error {e.status_code}: {e.message}")
        except anthropic.APIConnectionError:
            raise ApplicationKitError("Cannot reach the Claude API. Check your network or proxy.")

        if response.stop_reason == "refusal":
            raise ApplicationKitError("Claude declined this request.")
        if response.stop_reason == "max_tokens":
            raise ApplicationKitError("Claude's response was cut off (max_tokens reached).")
        return "".join(block.text for block in response.content if block.type == "text")

    def _generate_json(self, system, user, schema):
        """Generate and parse JSON, retrying once with a stricter instruction if the model returns invalid JSON."""
        text = self._generate(system, user, schema)
        try:
            return parse_json(text)
        except ValueError:
            retry = user + "\n\nYour previous reply was not valid JSON. Respond with a single valid JSON object only."
            try:
                return parse_json(self._generate(system, retry, schema))
            except ValueError as e:
                raise ApplicationKitError(f"Model did not return valid JSON: {e}")

    # --- tasks ---------------------------------------------------------------

    def suggest_rewording(self, resume_path, job, output_path=None):
        """
        Suggest rewordings of resume bullets for a job.

        job: dict from load_job() ({'description', 'title', 'company'}) or a plain job description string.
        Returns the result dict and writes it to output_path (default: output/<date>_<company>_<title>_reword.json).
        """
        job = job if isinstance(job, dict) else {"description": job, "title": "", "company": ""}
        resume = load_json(resume_path)
        validate_resume(resume)
        texts = resume_texts(resume)

        user = (
            f"Job title: {job['title'] or 'unknown'}\nCompany: {job['company'] or 'unknown'}\n\n"
            f"<job_description>\n{job['description']}\n</job_description>\n\n"
            f"<resume>\n{json.dumps(resume, indent=2)}\n</resume>\n\n"
            f"Rewordable ids: {', '.join(texts)}"
        )
        data = self._generate_json(REWORD_SYSTEM, user, REWORD_SCHEMA)

        # Keep only suggestions that point at real resume text, so nothing made-up gets through
        kept, dropped = [], 0
        for s in data.get("suggestions", []):
            target = s.get("target_id")
            if target in texts and _normalize(s.get("original")) == _normalize(texts[target]) and s.get("suggested"):
                s["original"] = texts[target]
                kept.append(s)
            else:
                dropped += 1

        result = {
            "meta": self._meta(job, resume_path, dropped_suggestions=dropped),
            "missing_keywords": data.get("missing_keywords", []),
            "skills_to_add": data.get("skills_to_add", []),
            "suggestions": kept,
        }
        path = output_path or self._output_path(job, "reword", ".json")
        _write(path, json.dumps(result, indent=2, ensure_ascii=False))
        result["meta"]["output_path"] = path
        return result

    def generate_cover_letter(self, master_resume_path, template_path, job, output_path=None):
        """
        Write a cover letter from the master resume and a cover letter template.

        Writes <name>.json (meta, sections, full letter, word count) and <name>.txt (the letter only).
        Returns the result dict.
        """
        job = job if isinstance(job, dict) else {"description": job, "title": "", "company": ""}
        resume = load_json(master_resume_path)
        validate_resume(resume)
        template = load_json(template_path)
        sections = template.get("sections") or []
        if not sections or not all(s.get("name") for s in sections):
            raise ApplicationKitError(f"Template {template_path} needs a non-empty 'sections' list, each with a 'name'.")
        max_words = int(template.get("max_words", 250))

        values = {
            "name": resume["basics"].get("name", ""),
            "email": resume["basics"].get("email", ""),
            "phone": resume["basics"].get("phone", ""),
            "company": job["company"] or "your company",
            "title": job["title"] or "this role",
            "hiring_manager": template.get("hiring_manager", "Hiring Manager"),
        }
        section_specs = "\n".join(f"- {s['name']}: {_fill(s.get('instructions', ''), values)}" for s in sections)
        user = (
            f"Job title: {job['title'] or 'unknown'}\nCompany: {job['company'] or 'unknown'}\n"
            f"Tone: {template.get('tone', 'professional')}\nWord limit for all sections combined: {max_words}\n\n"
            f"Write these sections, returned as a JSON object keyed by section name:\n{section_specs}\n\n"
            f"<job_description>\n{job['description']}\n</job_description>\n\n"
            f"<resume>\n{json.dumps(resume, indent=2)}\n</resume>"
        )
        names = [s["name"] for s in sections]
        data = self._generate_json(COVER_SYSTEM, user, cover_letter_schema(names))
        missing = [n for n in names if not str(data.get(n, "")).strip()]
        if missing:
            raise ApplicationKitError(f"Model left these cover letter sections empty: {', '.join(missing)}")
        body = {n: str(data[n]).strip() for n in names}

        parts = [_fill(template.get("greeting", "Dear {hiring_manager},"), values)]
        parts += [body[n] for n in names]
        parts.append(_fill(template.get("closing", "Best regards,\n{name}"), values))
        letter = "\n\n".join(p for p in parts if p)
        word_count = sum(len(body[n].split()) for n in names)

        base = os.path.splitext(output_path)[0] if output_path else self._output_path(job, "cover_letter", "")
        result = {
            "meta": self._meta(job, master_resume_path, template=template_path, max_words=max_words),
            "sections": body,
            "cover_letter": letter,
            "word_count": word_count,
        }
        _write(base + ".txt", letter + "\n")
        _write(base + ".json", json.dumps(result, indent=2, ensure_ascii=False))
        result["meta"]["output_path"] = base + ".json"
        result["meta"]["text_path"] = base + ".txt"
        return result

    # --- helpers ---------------------------------------------------------------

    def _meta(self, job, resume_path, **extra):
        meta = {
            "provider": self.provider,
            "model": self.model,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "job_title": job.get("title", ""),
            "company": job.get("company", ""),
            "resume": resume_path,
        }
        meta.update(extra)
        return meta

    def _output_path(self, job, kind, ext):
        name = f"{datetime.now():%Y-%m-%d}_{_slug(job.get('company'), 'company')}_{_slug(job.get('title'))}_{kind}"
        return os.path.join(self.output_dir, name + ext)


def parse_json(text):
    """Parse a JSON object from model output, tolerating markdown fences and surrounding prose."""
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no JSON object found")
        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError as e:
            raise ValueError(str(e))
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data


def _write(path, content):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description="Reword resume bullets and write cover letters with Ollama or Claude.")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--resume", required=True, help="Path to the (master) resume JSON")
        p.add_argument("--jd", help="Job description text")
        p.add_argument("--jd-file", help="Path to a .txt job description")
        p.add_argument("--job-id", type=int, help="Job id from the scraper's jobs table")
        p.add_argument("--title", default="", help="Job title (filled in automatically with --job-id)")
        p.add_argument("--company", default="", help="Company (filled in automatically with --job-id)")
        p.add_argument("--provider", choices=["ollama", "claude"], default="ollama")
        p.add_argument("--model", help=f"Model name (default: {DEFAULT_OLLAMA_MODEL} / {DEFAULT_CLAUDE_MODEL})")
        p.add_argument("--api-key", help="Claude API key (default: ANTHROPIC_API_KEY)")
        p.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
        p.add_argument("--config", default="config.json", help="Scraper config, used for db_path with --job-id")
        p.add_argument("--out-dir", default="output")
        p.add_argument("--out", help="Exact output path (overrides --out-dir naming)")

    common(sub.add_parser("reword", help="Generate reword suggestions as JSON"))
    cover = sub.add_parser("cover", help="Generate a cover letter (JSON + .txt)")
    common(cover)
    cover.add_argument("--template", required=True, help="Path to the cover letter template JSON")

    args = parser.parse_args(argv)
    try:
        job = load_job(args.jd, args.jd_file, args.job_id, args.config, args.title, args.company)
        kit = ApplicationKit(args.provider, args.model, args.api_key, args.ollama_url, args.out_dir)
        print(f"Using {kit.provider} ({kit.model})...")
        if args.command == "reword":
            result = kit.suggest_rewording(args.resume, job, args.out)
            print(f"{len(result['suggestions'])} suggestions written to {result['meta']['output_path']}")
            if result["meta"]["dropped_suggestions"]:
                print(f"Dropped {result['meta']['dropped_suggestions']} suggestions that didn't match resume text.")
        else:
            result = kit.generate_cover_letter(args.resume, args.template, job, args.out)
            print(f"Cover letter ({result['word_count']} words) written to "
                  f"{result['meta']['output_path']} and {result['meta']['text_path']}")
    except ApplicationKitError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
