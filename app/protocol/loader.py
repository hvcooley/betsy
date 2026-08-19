"""Parses YAML protocol definitions — TODO"""

from pathlib import Path

import yaml


def load_protocol(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)
