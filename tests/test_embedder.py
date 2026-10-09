import os
import sqlite3
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from embedder import (SCHEMA, HashEmbedder, LazyEmbedder, alias_hits, build_patterns, compile_alias, embed_input,
                      embed_missing, match_all, sync_items, sync_skill_vectors)

SKILLS = [
    {"name": "SQL", "type": "tech", "aliases": [], "weight": 3},
    {"name": "Microsoft SQL Server", "type": "tech", "aliases": ["SQL Server"], "weight": 3},
    {"name": "Kubernetes", "type": "tech", "aliases": ["K8s"], "weight": 3},
    {"name": "distributed systems", "type": "concept", "aliases": [], "weight": 2},
]


def make_conn(details):
    con = sqlite3.connect(':memory:')
    con.executescript(SCHEMA)
    con.execute('CREATE TABLE job_details (job_id INTEGER, requirements TEXT, preferences TEXT)')
    con.executemany('INSERT INTO job_details VALUES (?, ?, ?)', details)
    return con


def items(con):
    return con.execute('SELECT job_id, kind, idx, text FROM job_items ORDER BY job_id, kind DESC, idx').fetchall()


def sync(con):
    return sync_items(con, 'job_details', 'job_id', 'requirements', 'preferences')


def test_sync_items_uses_ats_score_line_splitting():
    con = make_conn([(1, '- Python\n\n• SQL', '* Kafka'), (2, None, '')])
    assert sync(con) == 2
    assert items(con) == [(1, 'requirement', 0, 'Python'), (1, 'requirement', 1, 'SQL'), (1, 'preference', 0, 'Kafka')]


def test_sync_items_updates_changed_lines_and_drops_removed_jobs():
    con = make_conn([(1, '- Python\n- SQL', None), (2, '- Go', None)])
    sync(con)
    con.execute("UPDATE job_items SET embedding = x'00', model = 'm'")
    con.execute("UPDATE job_details SET requirements = '- Python\n- Rust' WHERE job_id = 1")
    con.execute('DELETE FROM job_details WHERE job_id = 2')
    sync(con)
    assert items(con) == [(1, 'requirement', 0, 'Python'), (1, 'requirement', 1, 'Rust')]
    assert con.execute('SELECT text FROM job_items WHERE embedding IS NULL').fetchall() == [('Rust',)]


def test_embed_input_strips_lead_ins():
    assert embed_input('5+ years of strong experience with distributed systems') == 'distributed systems'
    assert embed_input('Experience with Go') == 'Experience with Go'  # too short once stripped


def test_compile_alias_short_aliases_are_case_sensitive():
    assert compile_alias('K8s').search('Run K8s')
    assert not compile_alias('REST').search('the rest of it')
    assert compile_alias('Kubernetes').search('kubernetes clusters')
    assert not compile_alias('C').search('C++')


def test_alias_hits_drops_skill_nested_in_longer_match():
    patterns = build_patterns(SKILLS)
    assert alias_hits('Experience with SQL Server', patterns) == {'Microsoft SQL Server'}
    assert alias_hits('SQL and SQL Server', patterns) == {'SQL', 'Microsoft SQL Server'}


def test_hash_embedder_is_normalized():
    vecs = HashEmbedder().encode(['a b c', ''])
    assert vecs.dtype == np.float32
    assert abs(np.linalg.norm(vecs[0]) - 1) < 1e-6 and not vecs[1].any()


def test_embed_and_match_end_to_end_with_fake_model():
    con = make_conn([(1, '- Kubernetes, K8s\n- Distributed computing', '- Excellent communication')])
    sync(con)
    emb = LazyEmbedder('fake')
    assert embed_missing(con, emb, 'fake|t') == 3
    assert embed_missing(con, emb, 'fake|t') == 0
    names, mat, refreshed = sync_skill_vectors(con, SKILLS, emb, 'fake|t')
    assert names == ['distributed systems'] and refreshed == 1
    assert sync_skill_vectors(con, SKILLS, emb, 'fake|t')[2] == 0  # cached
    n, counts, unmatched = match_all(con, build_patterns(SKILLS), names, mat, 0.4)
    assert (n, unmatched) == (3, 1)
    assert con.execute('SELECT i.text, m.skill, m.method FROM matches m JOIN job_items i ON i.id = m.job_item_id '
                       'ORDER BY i.idx').fetchall() == [
        ('Kubernetes, K8s', 'Kubernetes', 'alias'), ('Distributed computing', 'distributed systems', 'embed')]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"{len(tests)} tests passed")
