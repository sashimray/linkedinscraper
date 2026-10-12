import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from config import load_config


def raises(exc, fn):
    try:
        fn()
    except exc as e:
        return e
    raise AssertionError(f"{exc.__name__} not raised")


def test_load_config_reads_json():
    fd, path = tempfile.mkstemp(suffix='.json')
    with os.fdopen(fd, 'w') as f:
        json.dump({'db_path': 'x.db', 'jobs_tablename': 'jobs'}, f)
    assert load_config(path) == {'db_path': 'x.db', 'jobs_tablename': 'jobs'}


def test_load_config_errors():
    raises(OSError, lambda: load_config('/nonexistent/config.json'))
    fd, path = tempfile.mkstemp(suffix='.json')
    with os.fdopen(fd, 'w') as f:
        f.write('{not json')
    raises(ValueError, lambda: load_config(path))


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_") and callable(fn)]
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"{len(tests)} passed")
