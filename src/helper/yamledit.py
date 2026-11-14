"""Set one scalar in a block-style YAML file without losing its comments.

PyYAML drops comments on rewrite, and the data files are commented on purpose. This edits the one
line that holds the value instead. It handles block-style mappings only, which is how helper's data
files are written.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_KEY_RE = re.compile(r"^(?P<indent> *)(?P<key>[A-Za-z0-9_]+):(?P<rest>.*)$")


def yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)  # a JSON string is a valid YAML double-quoted scalar


def _split_comment(rest: str) -> str:
    """Return the trailing ` # comment` part of a value, or ''. Quotes are respected."""
    quote = None
    for i, ch in enumerate(rest):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or rest[i - 1] in " \t"):
            j = i
            while j > 0 and rest[j - 1] in " \t":
                j -= 1
            return rest[j:]
    return ""


def set_scalar(text: str, dotted_path: str, value: Any) -> str:
    keys = dotted_path.split(".")
    lines = text.splitlines(keepends=True)
    stack: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        body = line.rstrip("\r\n")
        if not body.strip() or body.lstrip().startswith("#"):
            continue
        m = _KEY_RE.match(body)
        if not m:
            continue
        indent = len(m.group("indent"))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, m.group("key")))
        if [k for _, k in stack] == keys:
            rest = m.group("rest")
            if rest.strip() and not rest.strip().startswith("#") and rest.strip()[0] in "{[|>":
                raise ValueError(f"{dotted_path} is not a plain scalar; edit it by hand.")
            comment = _split_comment(rest)
            newline = line[len(body):]
            lines[i] = f"{' ' * indent}{m.group('key')}: {yaml_scalar(value)}{comment}{newline}"
            return "".join(lines)
    raise KeyError(dotted_path)


def set_scalar_in_file(path: Path, dotted_path: str, value: Any) -> None:
    path.write_text(set_scalar(path.read_text(encoding="utf-8"), dotted_path, value), encoding="utf-8")
