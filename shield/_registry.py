#!/usr/bin/env python3
"""_registry.py -- shared loader for the shield trigger registry.

The registry is a small YAML file: a list of triggers, each carrying a regex
pattern, the rule to inject when it matches, the slug of the memory note the
rule comes from, and an active flag. Both shield layers read it -- layer 1 to
match the incoming prompt, layer 3 to rebuild the rubric for the armed rules --
so the loader lives in one place.

PyYAML is used when it is installed. The harness ships with zero third-party
dependency, so a minimal fallback parser covers the single-line scalar subset
the registry format needs. Anything richer (block scalars, anchors, nested
mappings) requires PyYAML; the fallback returns what it can and never raises.

Fail-open: a missing, unreadable or malformed registry returns an empty list.
No trigger means no injection and no armed reviewer, never a broken turn.

Environment:
- HARNESS_SHIELD_REGISTRY  path to the trigger registry
                           (default: trigger-registry.example.yaml next to
                           this file -- copy it, then point the variable at
                           your own)
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_REGISTRY = os.path.join(HERE, "trigger-registry.example.yaml")

# "  - key: value" opens a new entry; "    key: value" continues the current
# one. A top-level key such as `triggers:` carries no indentation, so it never
# matches either pattern and cannot be mistaken for a field.
_ITEM_RE = re.compile(r"^\s*-\s+([A-Za-z_][\w-]*)\s*:\s*(.*)$")
_FIELD_RE = re.compile(r"^\s+([A-Za-z_][\w-]*)\s*:\s*(.*)$")
_TRUE = ("true", "yes", "on")
_FALSE = ("false", "no", "off")


def registry_path():
    """Path of the trigger registry in use."""
    return os.path.expanduser(
        os.environ.get("HARNESS_SHIELD_REGISTRY") or DEFAULT_REGISTRY)


def _scalar(raw):
    """One single-line YAML scalar: quoted string, boolean, or bare text."""
    v = raw.strip()
    if v[:1] in ("'", '"'):
        quote = v[0]
        end = v.find(quote, 1)
        while end != -1 and quote == "'" and v[end + 1:end + 2] == "'":
            end = v.find(quote, end + 2)
        if end == -1:
            return v[1:]
        inner = v[1:end]
        return inner.replace("''", "'") if quote == "'" else inner
    v = v.split(" #", 1)[0].strip()   # trailing comment, unquoted values only
    low = v.lower()
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    return v


def _mini_yaml(text):
    """Fallback parser for the registry subset, used when PyYAML is absent."""
    entries, current = [], None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        item = _ITEM_RE.match(line)
        if item:
            current = {}
            entries.append(current)
            current[item.group(1)] = _scalar(item.group(2))
            continue
        field = _FIELD_RE.match(line)
        if field and current is not None:
            current[field.group(1)] = _scalar(field.group(2))
    return entries


def load_triggers(path=None):
    """Every entry of the registry, active or not. [] on any problem."""
    try:
        with open(path or registry_path(), encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return []
    try:
        import yaml
    except Exception:
        try:
            return _mini_yaml(text)
        except Exception:
            return []
    try:
        data = yaml.safe_load(text)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    items = data.get("triggers") or []
    if not isinstance(items, list):
        return []
    return [e for e in items if isinstance(e, dict)]


def active_triggers(path=None):
    """Entries flagged active, with a pattern AND a rule. An entry missing
    either one is dead weight: it would arm the reviewer with nothing to say."""
    out = []
    for e in load_triggers(path):
        if not e.get("active"):
            continue
        if not e.get("pattern") or not e.get("rule"):
            continue
        out.append(e)
    return out
