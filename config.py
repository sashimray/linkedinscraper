"""Loading config.json, shared by the app, the workflow and the scripts."""
import json


def load_config(file_name='config.json'):
    """The parsed JSON config. Raises OSError if the file can't be read, ValueError if it isn't valid JSON."""
    with open(file_name) as f:
        return json.load(f)
