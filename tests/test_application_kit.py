import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from resumekit.application_kit import (ApplicationKit, ApplicationKitError, load_job, parse_json,
                                       validate_resume)

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "resumekit", "examples")
RESUME = os.path.join(EXAMPLES, "resume.example.json")
TEMPLATE = os.path.join(EXAMPLES, "cover_letter_template.example.json")
JOB = {"description": "We need a Python backend engineer with Airflow and Kubernetes.",
       "title": "Senior Backend Engineer", "company": "Data Corp"}


def fake_model(reply):
    calls = []

    def generate(system, user, schema):
        calls.append({"system": system, "user": user, "schema": schema})
        return reply(len(calls)) if callable(reply) else reply

    return generate, calls


def test_parse_json_variants():
    assert parse_json('{"a": 1}') == {"a": 1}
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Here you go:\n{"a": 1}\nThanks') == {"a": 1}
    for bad in ["no json here", "[1, 2]"]:
        try:
            parse_json(bad)
            assert False, f"expected ValueError for {bad!r}"
        except ValueError:
            pass


def test_validate_resume_rejects_duplicate_ids():
    resume = json.load(open(RESUME))
    validate_resume(resume)
    resume["experience"][1]["bullets"][0]["id"] = "exp-acme-1"
    try:
        validate_resume(resume)
        assert False, "expected duplicate id error"
    except ApplicationKitError as e:
        assert "Duplicate id" in str(e)


def test_reword_keeps_only_real_suggestions():
    reply = json.dumps({
        "missing_keywords": ["Kubernetes"],
        "skills_to_add": [],
        "suggestions": [
            {"target_id": "exp-acme-1", "original": "Built data pipelines that load customer events into  PostgreSQL.",
             "suggested": "Built Airflow-orchestrated Python pipelines loading [N] customer events/day into PostgreSQL.",
             "reason": "JD emphasises Airflow", "keywords": ["Airflow"]},
            {"target_id": "exp-fake-9", "original": "Invented bullet", "suggested": "x", "reason": "x", "keywords": []},
            {"target_id": "exp-acme-2", "original": "Something the resume never said", "suggested": "x",
             "reason": "x", "keywords": []},
        ],
    })
    generate, calls = fake_model(reply)
    with tempfile.TemporaryDirectory() as tmp:
        kit = ApplicationKit("ollama", output_dir=tmp, generate_fn=generate)
        result = kit.suggest_rewording(RESUME, JOB)
        saved = json.load(open(result["meta"]["output_path"]))

    assert [s["target_id"] for s in result["suggestions"]] == ["exp-acme-1"]
    # original is replaced with the exact resume text (whitespace differences tolerated)
    assert result["suggestions"][0]["original"] == "Built data pipelines that load customer events into PostgreSQL."
    assert result["meta"]["dropped_suggestions"] == 2
    assert result["meta"]["model"] == "llama3.1:8b"
    assert saved["suggestions"] == result["suggestions"]
    assert "Airflow and Kubernetes" in calls[0]["user"] and calls[0]["schema"]["required"]


def test_reword_retries_once_on_invalid_json():
    good = json.dumps({"missing_keywords": [], "skills_to_add": [], "suggestions": []})
    generate, calls = fake_model(lambda n: "Sorry, here is my answer" if n == 1 else good)
    with tempfile.TemporaryDirectory() as tmp:
        result = ApplicationKit("claude", output_dir=tmp, generate_fn=generate).suggest_rewording(RESUME, JOB)
    assert len(calls) == 2 and "not valid JSON" in calls[1]["user"]
    assert result["meta"]["model"] == "claude-opus-5"


def test_cover_letter_writes_json_and_txt():
    reply = json.dumps({"hook": "Data Corp's pipeline work excites me.",
                        "fit": "At Acme I built data pipelines into PostgreSQL.",
                        "closing": "I'd welcome a conversation."})
    generate, calls = fake_model(reply)
    with tempfile.TemporaryDirectory() as tmp:
        kit = ApplicationKit("ollama", output_dir=tmp, generate_fn=generate)
        result = kit.generate_cover_letter(RESUME, TEMPLATE, JOB)
        txt = open(result["meta"]["text_path"]).read()
        saved = json.load(open(result["meta"]["output_path"]))

    assert txt.startswith("Dear Hiring Manager,\n\nData Corp's pipeline work")
    assert txt.rstrip().endswith("Alex Rivera\nalex.rivera@example.com | +1 555 010 0000")
    assert saved["cover_letter"] == txt.rstrip("\n")
    assert saved["word_count"] == 18
    assert set(calls[0]["schema"]["required"]) == {"hook", "fit", "closing"}
    assert "{company}" not in calls[0]["user"] and "at Data Corp" in calls[0]["user"]
    assert os.path.basename(result["meta"]["output_path"]).endswith("_data-corp_senior-backend-engineer_cover_letter.json")


def test_cover_letter_rejects_empty_section():
    generate, _ = fake_model(json.dumps({"hook": "Hi", "fit": "", "closing": "Bye"}))
    with tempfile.TemporaryDirectory() as tmp:
        try:
            ApplicationKit(output_dir=tmp, generate_fn=generate).generate_cover_letter(RESUME, TEMPLATE, JOB)
            assert False, "expected empty section error"
        except ApplicationKitError as e:
            assert "fit" in str(e)


def test_load_job_from_db():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "jobs.db")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, title TEXT, company TEXT, job_description TEXT)")
        conn.execute("INSERT INTO jobs VALUES (7, 'Data Engineer', 'Acme', 'Build pipelines')")
        conn.commit()
        conn.close()
        config = os.path.join(tmp, "config.json")
        json.dump({"db_path": db, "jobs_tablename": "jobs"}, open(config, "w"))

        assert load_job(job_id=7, config_path=config) == {
            "description": "Build pipelines", "title": "Data Engineer", "company": "Acme"}
        try:
            load_job(job_id=99, config_path=config)
            assert False, "expected missing job error"
        except ApplicationKitError as e:
            assert "No job with id 99" in str(e)


def test_ollama_unreachable_gives_clear_error():
    kit = ApplicationKit("ollama", ollama_url="http://127.0.0.1:9")
    try:
        kit._generate("s", "u")
        assert False, "expected connection error"
    except ApplicationKitError as e:
        assert "ollama serve" in str(e)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"{len(tests)} tests passed")
