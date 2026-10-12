#!/usr/bin/env python3
"""Flatten resume.json skills into a scoring-ready skills file.

Usage:
    python -m scripts.resume_parser resume.json                 # writes skills_flat.json
    python -m scripts.resume_parser resume.json -o my_skills.json

What it does:
  - splits compound items ("AWS (MSK / Kafka, EMR)") into atomic skills
  - fixes known typos and canonicalizes names ("Kafka" -> "Apache Kafka")
  - dedupes by canonical name and merges aliases
  - tags each skill "tech" (alias match) or "concept" (embedding match)
  - keeps weights from an existing output file, so re-running never loses tuning

The input file is never modified.
"""
import argparse
import json
import sys
from pathlib import Path

DEFAULT_WEIGHT = {"tech": 3, "concept": 2}

# lowercase raw name -> canonical name
CANON = {
    "jenkin": "Jenkins",
    "kafka": "Apache Kafka",
    "msk": "Amazon MSK",
    "emr": "Amazon EMR",
    "eks": "Amazon EKS",
    "s3": "Amazon S3",
    "ci/cd tools": "CI/CD",
}
TYPOS = {"jenkin"}

# Inside "Domain Experience", only these are named technologies.
# Everything else in that category is a concept (embedding match).
DOMAIN_TECH = {
    "apache kafka", "hadoop", "nosql", "ci/cd", "claude code",
    "infrastructure as code",
}

# canonical name (lowercase) -> extra aliases. Edit freely.
DEFAULT_ALIASES = {
    "javascript": ["JS", "ECMAScript"],
    "ruby on rails": ["Rails", "RoR", "Ruby"],
    "bash": ["Shell scripting", "Unix shell"],
    "spring boot": ["SpringBoot"],
    "rest apis": ["REST", "REST API", "RESTful", "RESTful APIs", "RESTful services"],
    "soap apis": ["SOAP", "SOAP API", "WSDL"],
    "apache kafka": ["Kafka"],
    "apache storm": ["Storm"],
    "hadoop": ["Hadoop ecosystem", "Apache Hadoop"],
    "oozie": ["Apache Oozie"],
    "mapreduce": ["Map Reduce", "Map-Reduce"],
    "hbase": ["Apache HBase"],
    "postgresql": ["Postgres"],
    "oracle database": ["Oracle DB", "Oracle ATP", "Oracle Autonomous Database"],
    "ibm db2": ["DB2", "IBM Db2"],
    "microsoft sql server": ["SQL Server", "MSSQL", "MS SQL"],
    "oracle cloud infrastructure": ["OCI", "Oracle Cloud"],
    "aws": ["Amazon Web Services"],
    "amazon msk": ["MSK", "AWS MSK", "Managed Streaming for Apache Kafka"],
    "amazon emr": ["EMR", "AWS EMR", "Elastic MapReduce"],
    "amazon eks": ["EKS", "AWS EKS"],
    "amazon s3": ["S3", "AWS S3"],
    "microsoft azure": ["Azure"],
    "docker": ["containerization"],
    "kubernetes": ["K8s"],
    "git": ["GitHub", "GitLab", "version control"],
    "jenkins": ["Jenkin"],
    "ci/cd": ["CI/CD tools", "CICD", "CI-CD", "continuous integration",
              "continuous delivery", "continuous deployment"],
    "infrastructure as code": ["IaC", "infrastructure-as-code"],
    "nosql": ["No-SQL", "NoSQL databases"],
    "ai-assisted coding tools": ["AI coding assistants", "GitHub Copilot",
                                 "Copilot", "AI-assisted development"],
    "relational databases": ["RDBMS", "relational database"],
    "cloud platforms": ["cloud computing", "public cloud", "cloud services"],
    "software development life cycle": ["SDLC", "software development lifecycle",
                                        "software development life-cycle"],
    "object-oriented programming": ["OOP", "OOD", "object-oriented design",
                                    "object oriented"],
    "agile methodologies": ["Agile", "Scrum", "Kanban"],
    "microservices architecture": ["microservices", "microservice", "micro-services"],
    "streaming": ["stream processing", "real-time streaming", "real-time data"],
    "batch processing": ["batch jobs", "batch pipelines"],
    "messaging queue": ["message queue", "message queues", "messaging queues",
                        "message broker", "messaging systems"],
    "large-scale enterprise data platforms": ["data platform", "enterprise data platform"],
    "multi-region cloud migrations": ["cloud migration", "multi-region"],
    "data ingestion": ["ingestion pipelines"],
    "healthcare": ["health care", "health IT", "EHR"],
}


def expand(raw):
    """Return [(name, extra_aliases)] for one raw item."""
    raw = " ".join(str(raw).split())
    if raw.count("(") != 1 or not raw.endswith(")"):
        return [(raw, [])]
    base, inner = raw[:-1].split("(", 1)
    base, inner = base.strip(), inner.strip()
    if not base or not inner:
        return [(raw, [])]

    parts = [p.strip() for p in inner.replace("/", ",").split(",") if p.strip()]
    if len(parts) > 1:  # compound: parent plus children
        parent = base[: -len(" ecosystem")] if base.lower().endswith(" ecosystem") else base
        out = [(parent, [base] if parent != base else [])]
        out += [(p, []) for p in parts]
        return out
    if inner[0].islower():  # "Ruby (on Rails)" -> "Ruby on Rails"
        return [(f"{base} {inner}", [base])]
    return [(base, [inner])]  # "Oracle Cloud Infrastructure (OCI)"


def classify(name, category):
    if category.lower() != "domain experience":
        return "tech"
    return "tech" if name.lower() in DOMAIN_TECH else "concept"


def add_aliases(rec, new):
    seen = {a.lower() for a in rec["aliases"]} | {rec["name"].lower()}
    for a in new:
        a = a.strip()
        if a and a.lower() not in seen:
            rec["aliases"].append(a)
            seen.add(a.lower())


def lower_first(s):
    return s[0].lower() + s[1:] if len(s) > 1 and s[1].islower() else s


def build(groups, prev_weights):
    skills = {}
    typos, splits, merges = [], [], []

    for g in groups:
        cat = g["category"].strip()
        for raw in g["items"]:
            raw = " ".join(str(raw).split())
            entries = expand(raw)
            if len(entries) > 1:
                splits.append(f"{raw}  ->  {', '.join(n for n, _ in entries)}")

            for name, extra in entries:
                low = name.lower()
                canon = CANON.get(low, name)
                key = canon.lower()
                if low in TYPOS:
                    typos.append(f"{name}  ->  {canon}")

                aliases = list(extra)
                if canon != name:
                    aliases.append(name)
                aliases += DEFAULT_ALIASES.get(key, [])
                typ = classify(canon, cat)

                if key in skills:
                    rec = skills[key]
                    if typ == "tech":
                        rec["type"] = "tech"
                    merges.append(f"{name} ({cat})  ->  {rec['name']}")
                else:
                    rec = {"name": canon, "type": typ, "category": cat, "aliases": []}
                    skills[key] = rec
                add_aliases(rec, aliases)

    out = []
    for key, rec in skills.items():
        rec["weight"] = prev_weights.get(key, DEFAULT_WEIGHT[rec["type"]])
        if rec["type"] == "concept":
            rec["embed_text"] = f"Experience with {lower_first(rec['name'])}"
        out.append(rec)
    return out, typos, splits, merges


def load_prev_weights(path):
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {s["name"].lower(): s["weight"]
                for s in data.get("skills", []) if "weight" in s}
    except (json.JSONDecodeError, KeyError, AttributeError, TypeError):
        return {}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("resume", help="path to resume.json")
    ap.add_argument("-o", "--out", default="skills_flat.json",
                    help="output file (default: skills_flat.json)")
    args = ap.parse_args()

    src, dst = Path(args.resume), Path(args.out)
    if src.resolve() == dst.resolve():
        sys.exit("Output must be a different file than the input.")

    doc = json.loads(src.read_text(encoding="utf-8"))
    groups = doc.get("skills") if isinstance(doc, dict) else doc
    ok = isinstance(groups, list) and all(
        isinstance(g, dict) and "category" in g and isinstance(g.get("items"), list)
        for g in groups
    )
    if not ok:
        sys.exit('Expected "skills": [{"category": ..., "items": [...]}, ...]')

    prev = load_prev_weights(dst)
    skills, typos, splits, merges = build(groups, prev)
    dst.write_text(json.dumps({"skills": skills}, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")

    n_tech = sum(s["type"] == "tech" for s in skills)
    print(f"Wrote {len(skills)} skills to {dst}  "
          f"({n_tech} tech, {len(skills) - n_tech} concept)")
    if prev:
        print(f"Kept {sum(k in prev for k in (s['name'].lower() for s in skills))} "
              f"weights from the previous output")
    for title, rows in (("Typos fixed", typos), ("Split", splits),
                        ("Duplicates merged", merges)):
        if rows:
            print(f"\n{title}:")
            for r in rows:
                print(f"  {r}")


if __name__ == "__main__":
    main()