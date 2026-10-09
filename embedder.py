#!/usr/bin/env python3
"""Match job lines against skills_flat.json (output of resume_parser.py).

Steps:
  1. split job_details.requirements / job_details.preferences into lines -> job_items
     (same line splitting as ats_score.py); jobs no longer in job_details are dropped
  2. embed new or changed lines and the concept skills (local model, cached)
  3. match each line to skills -> matches table
       - alias hit (any skill, tech or concept)      similarity 1.0, method "alias"
       - embedding similarity (concept skills only)  similarity = cosine, method "embed"

Usage:
  python embedder.py data/my_database.db skills_flat.json
  python embedder.py data/my_database.db skills_flat.json --threshold 0.80
  python embedder.py data/my_database.db skills_flat.json --show 42     # inspect one job

Requires: numpy, sentence-transformers (the latter only when embedding).
Weights are NOT applied here; ats_score_embed.py turns matches into jobs.ats_score_embed.
"""
import argparse
import hashlib
import json
import re
import sqlite3
import sys
import zlib
from pathlib import Path

import numpy as np

from ats_score import split_lines

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
# bge-small puts loosely related lines at 0.75-0.78 ("8+ years of data engineering" vs "relational
# databases"), so concept matches start at 0.78. ats_score_embed.py gives these 0 credit and ramps up from here.
DEFAULT_THRESHOLD = 0.78
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

PREP_VERSION = "prep1"   # bump when LEAD_IN / embed_input change; forces re-embed
LEAD_IN = re.compile(
    r"^(?:\d+\+?\s*years?\s+(?:of\s+)?)?"
    r"(?:(?:strong|solid|deep|proven|hands-on|working|practical|good)\s+)*"
    r"(?:experience|knowledge|familiarity|proficiency|expertise|understanding|"
    r"background|exposure)\s+(?:with|in|of|using|building|developing|designing)\s+",
    re.IGNORECASE)

SCHEMA = """
CREATE TABLE IF NOT EXISTS job_items(
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL,
    kind TEXT NOT NULL,            -- requirement | preference
    idx INTEGER NOT NULL,          -- order within the job and kind
    text TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    model TEXT,
    embedding BLOB,                -- float32, L2-normalized
    UNIQUE(job_id, kind, idx)
);
CREATE TABLE IF NOT EXISTS skill_vectors(
    name TEXT PRIMARY KEY,
    text_hash TEXT NOT NULL,
    model TEXT NOT NULL,
    embedding BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS matches(
    job_item_id INTEGER NOT NULL,
    skill TEXT NOT NULL,
    method TEXT NOT NULL,          -- alias | embed
    similarity REAL NOT NULL,
    PRIMARY KEY(job_item_id, skill)
);
"""


def sha1(s):
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


def embed_input(text):
    """Drop boilerplate lead-ins so 'Experience with X' lines don't all look alike."""
    stripped = LEAD_IN.sub("", text, count=1).strip()
    return stripped if len(stripped) >= 3 else text


def ident(name):
    if not IDENT.match(name):
        sys.exit(f"Invalid SQL identifier: {name!r}")
    return name


# ---------- embedders ----------

class HashEmbedder:
    """Test-only bag-of-words embedder. Not semantic."""
    dim = 256

    def encode(self, texts):
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            for w in re.findall(r"[a-z0-9]+", t.lower()):
                out[i, zlib.crc32(w.encode()) % self.dim] += 1
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        norms[norms == 0] = 1
        return out / norms


class LazyEmbedder:
    """Loads the model only when something actually needs embedding."""

    def __init__(self, name):
        self.name = name
        self._m = None

    def encode(self, texts):
        if self._m is None:
            if self.name == "fake":
                self._m = HashEmbedder()
            else:
                try:
                    from sentence_transformers import SentenceTransformer
                except ImportError:
                    sys.exit("pip install sentence-transformers")
                st = SentenceTransformer(self.name)

                class _ST:
                    def encode(self, t):
                        return np.asarray(
                            st.encode(t, normalize_embeddings=True, batch_size=64,
                                      show_progress_bar=False),
                            dtype=np.float32)
                self._m = _ST()
        return self._m.encode(texts)


# ---------- job_items ----------

def sync_items(con, jobs_table, id_col, req_col, pref_col):
    q = f"SELECT {ident(id_col)}, {ident(req_col)}, {ident(pref_col)} FROM {ident(jobs_table)}"
    try:
        jobs = con.execute(q).fetchall()
    except sqlite3.OperationalError as e:
        sys.exit(f"Could not read jobs ({e}). Use --jobs-table/--id-col/--req-col/--pref-col.")

    for job_id, req, pref in jobs:
        new = {}
        for kind, cell in (("requirement", req), ("preference", pref)):
            for i, text in enumerate(split_lines(cell)):
                new[(kind, i)] = text
        old = {(k, i): (rid, h) for rid, k, i, h in con.execute(
            "SELECT id, kind, idx, text_hash FROM job_items WHERE job_id=?", (job_id,))}

        for key, text in new.items():
            h = sha1(text)
            if key in old:
                rid, old_h = old[key]
                if old_h != h:
                    con.execute("UPDATE job_items SET text=?, text_hash=?, model=NULL, "
                                "embedding=NULL WHERE id=?", (text, h, rid))
            else:
                con.execute("INSERT INTO job_items(job_id, kind, idx, text, text_hash) "
                            "VALUES (?,?,?,?,?)", (job_id, key[0], key[1], text, h))
        for key, (rid, _) in old.items():
            if key not in new:
                con.execute("DELETE FROM job_items WHERE id=?", (rid,))

    current = {job_id for job_id, _, _ in jobs}
    stale = [(job_id,) for (job_id,) in con.execute("SELECT DISTINCT job_id FROM job_items")
             if job_id not in current]
    con.executemany("DELETE FROM job_items WHERE job_id=?", stale)
    return len(jobs)


def embed_missing(con, emb, tag):
    rows = con.execute("SELECT id, text FROM job_items "
                       "WHERE embedding IS NULL OR model IS NOT ?", (tag,)).fetchall()
    for start in range(0, len(rows), 512):
        chunk = rows[start:start + 512]
        vecs = emb.encode([embed_input(t) for _, t in chunk])
        con.executemany("UPDATE job_items SET embedding=?, model=? WHERE id=?",
                        [(v.tobytes(), tag, rid) for (rid, _), v in zip(chunk, vecs)])
    return len(rows)


# ---------- skills ----------

def load_skills(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))["skills"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as e:
        raise ValueError(f"Could not read skills file: {e}")


def sync_skill_vectors(con, skills, emb, model):
    # Concept skills are embedded as the bare name. The "embed_text" field written by
    # migrate_skills.py is ignored: a shared "Experience with" prefix inflates
    # similarity against every line that starts the same way.
    concept = [s for s in skills if s.get("type") == "concept"]
    todo = []
    for s in concept:
        text = s["name"]
        h = sha1(text)
        row = con.execute("SELECT text_hash, model FROM skill_vectors WHERE name=?",
                          (s["name"],)).fetchone()
        if row != (h, model):
            todo.append((s["name"], text, h))
    if todo:
        vecs = emb.encode([t for _, t, _ in todo])
        con.executemany(
            "INSERT OR REPLACE INTO skill_vectors(name, text_hash, model, embedding) "
            "VALUES (?,?,?,?)",
            [(n, h, model, v.tobytes()) for (n, _, h), v in zip(todo, vecs)])
    names = [s["name"] for s in concept]
    if not names:
        return [], None, 0
    mat = np.stack([
        np.frombuffer(con.execute("SELECT embedding FROM skill_vectors WHERE name=?",
                                  (n,)).fetchone()[0], dtype=np.float32)
        for n in names])
    return names, mat, len(todo)


def compile_alias(alias):
    """Whole-token match. Short aliases (<= 4 chars) are case-sensitive."""
    pat = r"(?<![\w+#])" + re.escape(alias) + r"(?![\w+#])"
    return re.compile(pat, 0 if len(alias) <= 4 else re.IGNORECASE)


def build_patterns(skills):
    return [(s["name"], [compile_alias(a) for a in [s["name"], *s.get("aliases", [])]])
            for s in skills]


# ---------- matching ----------

def alias_hits(text, patterns):
    """Skills whose alias appears in text. A skill is dropped when every one of its
    matches sits inside a longer match of another skill ('SQL' inside 'SQL Server')."""
    spans = {}
    for name, pats in patterns:
        for p in pats:
            for m in p.finditer(text):
                spans.setdefault(name, []).append(m.span())
    hits = set()
    for name, mine in spans.items():
        others = [o for n, sps in spans.items() if n != name for o in sps]
        if any(not any(o[0] <= s[0] and s[1] <= o[1] and o[1] - o[0] > s[1] - s[0]
                       for o in others) for s in mine):
            hits.add(name)
    return hits


def match_all(con, patterns, names, mat, threshold):
    con.execute("DELETE FROM matches")
    counts = {"alias": 0, "embed": 0}
    unmatched = 0
    rows = con.execute("SELECT id, text, embedding FROM job_items").fetchall()
    for iid, text, blob in rows:
        hits = {name: ("alias", 1.0) for name in alias_hits(text, patterns)}
        if names:
            sims = mat @ np.frombuffer(blob, dtype=np.float32)
            for name, sim in zip(names, sims):
                if name not in hits and sim >= threshold:
                    hits[name] = ("embed", float(sim))
        for name, (method, sim) in hits.items():
            con.execute("INSERT INTO matches VALUES (?,?,?,?)", (iid, name, method, sim))
            counts[method] += 1
        unmatched += not hits
    return len(rows), counts, unmatched


def show(con, job_id, names, mat, threshold):
    rows = con.execute("SELECT id, kind, text, embedding FROM job_items WHERE job_id=? "
                       "ORDER BY kind DESC, idx", (job_id,)).fetchall()
    if not rows:
        print(f"No job_items for job {job_id}")
        return
    for iid, kind, text, blob in rows:
        print(f"[{kind[:4]}] {text}")
        ms = con.execute("SELECT skill, method, similarity FROM matches WHERE job_item_id=? "
                         "ORDER BY similarity DESC, skill", (iid,)).fetchall()
        for skill, method, sim in ms:
            print(f"    {method:5} {sim:.2f}  {skill}")
        if names:
            sims = mat @ np.frombuffer(blob, dtype=np.float32)
            j = int(np.argmax(sims))
            if names[j] not in {m[0] for m in ms}:
                print(f"    best concept below threshold: {names[j]} "
                      f"{sims[j]:.2f} (threshold {threshold})")
        if not ms:
            print("    (no match)")


def run(db_path, skills_path, model=DEFAULT_MODEL, threshold=DEFAULT_THRESHOLD, jobs_table="job_details",
        id_col="job_id", req_col="requirements", pref_col="preferences", show_job=None):
    """Refresh job_items, embeddings and matches. Returns a stats dict. Raises ValueError for a bad skills file."""
    skills = load_skills(skills_path)
    con = sqlite3.connect(db_path)
    try:
        con.executescript(SCHEMA)
        emb = LazyEmbedder(model)
        n_jobs = sync_items(con, jobs_table, id_col, req_col, pref_col)
        tag = f"{model}|{PREP_VERSION}"
        n_embedded = embed_missing(con, emb, tag)
        names, mat, n_sk = sync_skill_vectors(con, skills, emb, tag)
        n_items, counts, unmatched = match_all(con, build_patterns(skills), names, mat, threshold)
        con.commit()

        print(f"{n_jobs} jobs, {n_items} lines ({n_embedded} newly embedded), "
              f"{n_sk} skill vectors refreshed")
        print(f"matches: {counts['alias']} alias, {counts['embed']} embed; "
              f"{unmatched} lines matched nothing")
        if show_job is not None:
            print()
            show(con, show_job, names, mat, threshold)
    finally:
        con.close()
    return {"jobs": n_jobs, "lines": n_items, "embedded": n_embedded, "skill_vectors": n_sk,
            "alias": counts["alias"], "embed": counts["embed"], "unmatched": unmatched}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db")
    ap.add_argument("skills", help="skills_flat.json")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"sentence-transformers model (default {DEFAULT_MODEL}); "
                         "'fake' is a test-only embedder")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                    help="min cosine for concept matches; tune with --show")
    ap.add_argument("--jobs-table", default="job_details")
    ap.add_argument("--id-col", default="job_id")
    ap.add_argument("--req-col", default="requirements")
    ap.add_argument("--pref-col", default="preferences")
    ap.add_argument("--show", type=int, metavar="JOB_ID",
                    help="print line-by-line matches for one job")
    a = ap.parse_args()

    try:
        run(a.db, a.skills, a.model, a.threshold, a.jobs_table, a.id_col, a.req_col, a.pref_col, a.show)
    except ValueError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()