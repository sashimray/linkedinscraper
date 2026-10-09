import json
import os
import sqlite3
import sys
import tempfile
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import ats_score
import db
from ats_score import (build_report, degree_fields, degree_level, explain_job, explain_line, find_skills, keyword_words,
                       keywords, load_resume, load_resume_profile, match_line, required_years, score_job, score_jobs,
                       section_credits, similar_skills, split_lines, write_report, years_of_experience)

RESUME = {
    "basics": {"name": "Test Person"},
    "summary": {"id": "summary", "text": "Backend engineer building microservices and cloud deployments."},
    "skills": [{"category": "Languages", "items": ["Java", "Python", "Ruby (on Rails)"]},
               {"category": "Domain", "items": ["Large-scale enterprise data platforms"]}],
    "experience": [
        {"id": "a", "start": "2015-08", "end": "2018-07", "tech": ["Spring"], "bullets": [{"id": "a1", "text": "Used Kafka."}]},
        {"id": "b", "start": "2021-04", "end": "2026-04", "tech": ["Kubernetes"], "bullets": []},
    ],
    "education": [{"degree": "M.S.", "field": "Computer Science"}, {"degree": "B.S.", "field": "Engineering"}],
}


def profile():
    return load_resume_profile(RESUME)


def test_find_skills_aliases_and_boundaries():
    assert find_skills("Experience with K8s, Postgres and RESTful APIs") == {"kubernetes", "postgresql", "rest api"}
    assert find_skills("JavaScript and TypeScript") == {"javascript", "typescript"}  # not java
    assert find_skills("React, Next.js, or Vue") == {"react", "vue"}              # .js suffix isn't JavaScript
    assert find_skills("Go, Rust or C++") == {"go", "rust", "c++"}
    assert find_skills("Python, C++, or Go.") == {"python", "c++", "go"}
    assert find_skills("Go above and beyond for the rest of the team") == set()
    assert find_skills("React to incidents quickly") == set()
    assert find_skills("MySQL, not SQL Server") == {"mysql", "sql", "sql server"}
    assert find_skills("Uses Snowpipe", extra_terms={"snowpipe"}) == {"snowpipe"}
    assert find_skills(None) == set()


def test_required_years():
    assert required_years("5+ years of professional software development") == 5
    assert required_years("6-8+ years of experience") == 6
    assert required_years("10 years in software engineering") == 10
    assert required_years("3 to 5 yrs Java") == 3
    assert required_years("Strong communication") is None


def test_degree_level():
    assert degree_level("Bachelor's degree in Computer Science") == 1
    assert degree_level("BS/MS in CS or equivalent") == 1
    assert degree_level("Master's degree required") == 2
    assert degree_level("PhD in Machine Learning") == 3
    assert degree_level("Team player") is None


def test_years_of_experience():
    jobs = [{"start": "2015-08", "end": "2018-07"}, {"start": "2021-04", "end": "present"}]
    assert years_of_experience(jobs, today=date(2026, 8, 1)) == 11
    assert years_of_experience([{"start": "2020-01", "end": "2022-06"}]) == 2
    assert years_of_experience([{"title": "no dates"}]) == 0
    assert years_of_experience([]) == 0


def test_degree_fields():
    assert degree_fields("BS in Physics or Applied Mathematics") == {"physics", "mathematics"}
    assert degree_fields("Degree in CS or Electrical Engineering") == {"computer science", "electrical engineering"}
    assert degree_fields("Bachelor's degree") == set()


def test_keywords():
    assert keywords("Deploying numerical simulations") == {"deploy", "numeri", "simula"}
    assert keywords("Strong sense of ownership and a bias for action") == set()   # soft skills only
    assert keywords(None) == set()


def test_keyword_words():
    assert keyword_words("Deploying numerical simulations") == {"deploy": "deploying", "numeri": "numerical",
                                                                "simula": "simulations"}


def test_similar_skills():
    assert similar_skills("gcp", {"aws", "oci", "java"}) == {"aws", "oci"}
    assert similar_skills("rust", {"java", "aws"}) == {"java"}
    assert similar_skills("helm", {"java"}) == set()
    assert similar_skills("unknown", {"java"}) == set()


def test_load_resume_profile():
    p = profile()
    assert {"java", "python", "ruby", "spring", "kafka", "kubernetes", "microservices"} <= p["skills"]
    assert "large-scale enterprise data platforms" not in p["extra_terms"]  # long phrases aren't skills
    assert {"backen", "cloud", "deploy"} <= p["keywords"]
    assert p["years"] == 10
    assert p["degree"] == 2
    assert p["fields"] == {"computer science"}
    try:
        load_resume_profile(["not", "a", "dict"])
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_split_lines():
    assert split_lines("- Java\n\n• Python  \n* Go") == ["Java", "Python", "Go"]
    assert split_lines(None) == []


def test_match_line():
    p = profile()
    assert match_line("5+ years of Java", p) == 1.0
    assert match_line("15+ years of Java", p) == 0.0              # years gate fails
    assert match_line("Experience with Golang", p) == 0.0
    assert match_line("Java or Golang", p) == 1.0                  # "or": one listed skill is enough
    assert match_line("Java and Golang", p) == 0.5                 # otherwise the share of skills
    assert match_line("Mathematical models and numerical methods", p) == 0.0   # domain words count
    assert match_line("Deploy backend services to cloud and edge", p) == 0.6     # services, edge missing
    assert match_line("Java microservices for physics simulations", p) == 0.7  # 0.7 * skills + 0.3 * keywords
    assert match_line("PhD in Computer Science", p) == 0.0
    assert match_line("Bachelor's degree in Computer Science", p) == 1.0
    assert match_line("Bachelor's degree in Physics", p) == 0.0    # level ok, field missing
    assert match_line("BS in Physics or related field", p) == 1.0
    assert match_line("Excellent communication skills", p) is None


def test_explain_line():
    p = profile()
    e = explain_line("5+ years of Java and GCP for physics simulations", p)
    assert e["credit"] == 0.35                                     # 0.7 * 1/2 skills + 0.3 * 0/2 words
    assert e["matched_skills"] == ["java"]
    assert e["missing_skills"] == ["gcp"]
    assert e["similar"] == {}                                      # test resume has no cloud skill
    assert e["missing_words"] == ["physics", "simulations"]
    assert e["years"] == (5, 10, True)
    assert e["degree"] is None

    e = explain_line("Python, Rust, or Go", p)
    assert e["credit"] == 1.0
    assert e["missing_skills"] == [] and e["alternatives"] == ["go", "rust"]

    e = explain_line("Experience with Rust", p)
    assert e["missing_skills"] == ["rust"] and e["similar"] == {"rust": ["java", "python", "ruby"]}

    e = explain_line("Bachelor's degree in Physics", p)
    assert e["credit"] == 0.0 and e["degree"] == (1, ["physics"], False)
    assert explain_line("Strong communication", p)["credit"] is None


def test_explain_job():
    result = explain_job("- Java\n- Team player", "- Rust", profile())
    assert result["score"] == score_job("- Java\n- Team player", "- Rust", profile())
    assert [e["credit"] for e in result["requirements"]] == [1.0, None]
    assert result["preferences"][0]["missing_skills"] == ["rust"]


def test_build_report():
    jobs = [(1, "Dev", "A", "- Java\n- Rust", ""), (2, "Dev", "B", "- Java", "- Rust"), (3, "Dev", "C", "- Team player", "")]
    report = build_report(jobs, profile())
    assert "| rust | 2 | java, python, ruby |" in report         # counted once per job
    assert report.index("(job 1)") < report.index("(job 2)")       # lowest score first
    assert "(job 3)" not in report                                 # nothing checkable
    assert "✗ rust (you have: java, python, ruby)" in report
    report = build_report([(4, "Dev", "D", "- Java\n- Strong communication", "")], profile())
    assert "- `skip` Strong communication" in report               # skipped lines are still listed


def test_section_and_job_score():
    p = profile()
    req = "- 5+ years of Java\n- Experience with Golang\n- Strong communication"
    assert section_credits(req, p) == [1.0, 0.0]
    assert section_credits("- Team player", p) == []
    assert score_job(req, "- Kafka a plus", p) == 57               # 0.85 * 50 + 0.15 * 100 (57.4999 in floats)
    assert score_job(req, "", p) == 50                             # no preferences -> requirements only
    assert score_job("- Team player", "- Kafka a plus", p) == 100  # no checkable requirements -> preferences only
    assert score_job("", None, p) is None


def test_knockout_cap():
    p = profile()
    req = "\n".join(["- Java", "- Python", "- Kafka", "- Spring", "- Kubernetes", "- Golang", "- Rust"])
    assert score_job(req, "", p) == 60                             # 5/7 = 71, but 2 misses cap it at 60
    assert score_job("\n".join(req.splitlines()[:6]), "", p) == 83  # one miss: no cap


def test_load_resume_errors():
    with tempfile.TemporaryDirectory() as tmp:
        bad = os.path.join(tmp, "bad.json")
        with open(bad, "w") as f:
            f.write("{not json")
        for path in (bad, os.path.join(tmp, "missing.json")):
            try:
                load_resume(path)
                assert False, "expected ValueError"
            except ValueError:
                pass


def make_db(tmp, with_score_column=True):
    path = os.path.join(tmp, "jobs.db")
    conn = sqlite3.connect(path)
    score_col = ", ats_score INT DEFAULT 0" if with_score_column else ""
    conn.execute(f"CREATE TABLE jobs (id INTEGER PRIMARY KEY, title TEXT, company TEXT, hidden INTEGER{score_col})")
    conn.executemany("INSERT INTO jobs (id, title, hidden) VALUES (?, ?, ?)",
                     [(1, "A", 0), (2, "B", 1), (3, "C", 0), (4, "D", 0), (5, "E", None)])
    db.create_job_details_table(conn, "jobs")
    conn.executemany("INSERT INTO job_details (job_id, requirements, preferences) VALUES (?, ?, ?)", [
        (1, "- 5+ years of Java\n- Experience with Golang", "- Kafka a plus"),  # 57
        (2, "- Java", ""),                                                       # hidden
        (3, "- Strong communication", ""),                                              # nothing checkable
        (5, "- Python", None),                                                   # hidden NULL counts as visible
    ])                                                                           # 4 has no job_details row
    conn.commit()
    conn.close()
    return path


def scores(path):
    conn = sqlite3.connect(path)
    rows = dict(conn.execute("SELECT id, ats_score FROM jobs"))
    conn.close()
    return rows


def test_db_read_visible_job_sections():
    with tempfile.TemporaryDirectory() as tmp:
        conn = sqlite3.connect(make_db(tmp))
        assert [row[0] for row in db.read_visible_job_sections(conn, "jobs")] == [1, 3, 5]
        conn.close()


def test_db_update_ats_scores_adds_missing_column():
    with tempfile.TemporaryDirectory() as tmp:
        path = make_db(tmp, with_score_column=False)
        conn = sqlite3.connect(path)
        db.update_ats_scores(conn, "jobs", {1: 80, 3: 20})
        conn.close()
        assert scores(path) == {1: 80, 2: 0, 3: 20, 4: 0, 5: 0}


def test_score_jobs():
    with tempfile.TemporaryDirectory() as tmp:
        path = make_db(tmp)
        resume_path = os.path.join(tmp, "resume.json")
        with open(resume_path, "w") as f:
            json.dump(RESUME, f)
        config = {"db_path": path, "jobs_tablename": "jobs"}

        assert score_jobs(config, resume_path, dry_run=True) == {1: 57, 5: 100}
        assert set(scores(path).values()) == {0}                       # dry run saves nothing
        assert score_jobs(config, resume_path) == {1: 57, 5: 100}
        assert scores(path) == {1: 57, 2: 0, 3: 0, 4: 0, 5: 100}


def test_write_report():
    with tempfile.TemporaryDirectory() as tmp:
        path = make_db(tmp)
        resume_path = os.path.join(tmp, "resume.json")
        with open(resume_path, "w") as f:
            json.dump(RESUME, f)
        config = {"db_path": path, "jobs_tablename": "jobs"}
        report_path = write_report(config, resume_path, os.path.join(tmp, "out", "report.md"), job_ids=[1])
        with open(report_path) as f:
            report = f.read()
        assert "(job 1)" in report and "(job 5)" not in report
        assert "✗ go (you have: java, python, ruby)" in report


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"{len(tests)} tests passed")
