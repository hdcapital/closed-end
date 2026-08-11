#!/usr/bin/env python3
"""Config loading. Every prior, weight and threshold lives in config.yaml;
this module only reads it and fails loudly when something is missing.

`cfg.get("a.b.c")` is the accessor used everywhere else, so a typo'd key
raises rather than silently returning a default that changes a ranking.
"""

import os
from typing import Any

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG_PATH = os.path.join(REPO_ROOT, "config.yaml")

_MISSING = object()


class Config:
    def __init__(self, data: dict, path: str):
        self._data = data
        self.path = path

    def get(self, dotted: str, default: Any = _MISSING) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                if default is _MISSING:
                    raise KeyError(f"config key not found: {dotted} (in {self.path})")
                return default
            node = node[part]
        return node

    def num(self, dotted: str, default: Any = _MISSING) -> float:
        """Numeric accessor that refuses YAML's string-shaped numbers.

        PyYAML resolves "50e6" to the *string* "50e6" (YAML 1.1 wants
        "5.0e+7"). Silently coercing that would be worse than crashing: a
        threshold read as a string compares false and quietly changes a score.
        """
        v = self.get(dotted, default)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise TypeError(
                f"config key {dotted} must be a number, got {type(v).__name__} {v!r}. "
                "If this looks like 50e6, write it as 50000000."
            )
        return float(v)

    @property
    def data(self) -> dict:
        return self._data


def load(path: str = None) -> Config:
    path = path or os.environ.get("CLOSED_END_CONFIG") or DEFAULT_CONFIG_PATH
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not parse to a mapping")
    return Config(data, path)
