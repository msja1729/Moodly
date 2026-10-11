"""Workspace actuator: hot-swap VS Code and Windows Terminal themes.

Both apps keep settings in JSONC (JSON + comments + trailing commas) and
reload them as soon as the file changes. A json.load/json.dump round-trip
would delete the user's comments and reformat the file, so instead the file
is scanned with a small position-tracking JSONC parser and only the text span
of the one value being changed is rewritten (or a new key is inserted).

Before anything is written, the edited text is re-parsed and compared with
the original data plus the intended change; any difference aborts the write.
Writes are atomic (temp file + os.replace) and the first write of a session
leaves a .bak copy of the original next to the file.
"""
from __future__ import annotations

import copy
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from shared.config_io import atomic_write_text, resolve_path

log = logging.getLogger(__name__)


class JsoncError(ValueError):
    pass


class JsoncPathError(JsoncError):
    pass


# ------------------------------------------------------------------ JSONC parser

@dataclass
class _Node:
    start: int
    end: int  # exclusive


@dataclass
class _Leaf(_Node):
    pass


@dataclass
class _Member:
    key: str
    key_start: int
    value: _Node


@dataclass
class _Obj(_Node):
    members: list[_Member] = field(default_factory=list)


@dataclass
class _Arr(_Node):
    items: list[_Node] = field(default_factory=list)


_LITERAL = re.compile(r"-?\d+(\.\d+)?([eE][-+]?\d+)?|true|false|null")


class _Scanner:
    def __init__(self, text: str):
        self.t = text
        self.i = 0

    def error(self, msg: str) -> JsoncError:
        line = self.t.count("\n", 0, self.i) + 1
        return JsoncError(f"{msg} at line {line}")

    def skip(self) -> None:
        t = self.t
        while self.i < len(t):
            c = t[self.i]
            if c in " \t\r\n":
                self.i += 1
            elif t.startswith("//", self.i):
                nl = t.find("\n", self.i)
                self.i = len(t) if nl < 0 else nl + 1
            elif t.startswith("/*", self.i):
                close = t.find("*/", self.i + 2)
                if close < 0:
                    raise self.error("unterminated comment")
                self.i = close + 2
            else:
                break

    def peek(self) -> str:
        return self.t[self.i] if self.i < len(self.t) else ""

    def value(self) -> _Node:
        c = self.peek()
        if c == "{":
            return self.obj()
        if c == "[":
            return self.arr()
        if c == '"':
            s = self.i
            self.string()
            return _Leaf(s, self.i)
        m = _LITERAL.match(self.t, self.i)
        if not m:
            raise self.error("unexpected character")
        self.i = m.end()
        return _Leaf(m.start(), m.end())

    def string(self) -> str:
        s = self.i
        self.i += 1
        t = self.t
        while self.i < len(t):
            c = t[self.i]
            if c == "\\":
                self.i += 2
            elif c == '"':
                self.i += 1
                return json.loads(t[s:self.i])
            elif c == "\n":
                break
            else:
                self.i += 1
        raise self.error("unterminated string")

    def obj(self) -> _Obj:
        node = _Obj(self.i, -1)
        self.i += 1
        self.skip()
        while self.peek() != "}":
            if self.peek() != '"':
                raise self.error("expected a key")
            key_start = self.i
            key = self.string()
            self.skip()
            if self.peek() != ":":
                raise self.error("expected ':'")
            self.i += 1
            self.skip()
            node.members.append(_Member(key, key_start, self.value()))
            self.skip()
            if self.peek() == ",":
                self.i += 1
                self.skip()
            elif self.peek() != "}":
                raise self.error("expected ',' or '}'")
        self.i += 1
        node.end = self.i
        return node

    def arr(self) -> _Arr:
        node = _Arr(self.i, -1)
        self.i += 1
        self.skip()
        while self.peek() != "]":
            node.items.append(self.value())
            self.skip()
            if self.peek() == ",":
                self.i += 1
                self.skip()
            elif self.peek() != "]":
                raise self.error("expected ',' or ']'")
        self.i += 1
        node.end = self.i
        return node


def _parse_tree(text: str) -> _Node:
    sc = _Scanner(text)
    sc.skip()
    if not sc.peek():
        raise JsoncError("empty document")
    root = sc.value()
    sc.skip()
    if sc.i != len(text):
        raise sc.error("trailing content")
    return root


def _to_python(text: str, node: _Node) -> Any:
    if isinstance(node, _Obj):
        return {m.key: _to_python(text, m.value) for m in node.members}
    if isinstance(node, _Arr):
        return [_to_python(text, n) for n in node.items]
    return json.loads(text[node.start:node.end])


def parse_jsonc(text: str) -> Any:
    root = _parse_tree(text)
    return _to_python(text, root)


def get_path(data: Any, key_path: list[str]) -> Any:
    for k in key_path:
        if not isinstance(data, dict) or k not in data:
            return None
        data = data[k]
    return data


def _set_path(data: dict, key_path: list[str], value: Any) -> None:
    for k in key_path[:-1]:
        data = data.setdefault(k, {})
    data[key_path[-1]] = value


# ------------------------------------------------------------------ surgical edit

def _line_indent(text: str, pos: int) -> str:
    line_start = text.rfind("\n", 0, pos) + 1
    m = re.match(r"[ \t]*", text[line_start:])
    return m.group(0)


def _indent_unit(text: str) -> str:
    """Indentation of the first indented line (i.e. one nesting level)."""
    m = re.search(r"\n([ \t]+)\S", text)
    return m.group(1) if m else "    "


def _dumps(value: Any, indent: str, unit: str, nl: str) -> str:
    s = json.dumps(value, indent=unit if unit == "\t" else len(unit), ensure_ascii=False)
    return s.replace("\n", nl + indent)


def _insert_member(text: str, obj: _Obj, keys: list[str], value: Any) -> str:
    nl = "\r\n" if "\r\n" in text else "\n"
    unit = _indent_unit(text)
    nested = value
    for k in reversed(keys[1:]):
        nested = {k: nested}

    if obj.members:
        last = obj.members[-1]
        same_line_as_open = "\n" not in text[obj.start:last.key_start]
        indent = (_line_indent(text, obj.start) + unit) if same_line_as_open else _line_indent(text, last.key_start)
        entry = f"{json.dumps(keys[0], ensure_ascii=False)}: {_dumps(nested, indent, unit, nl)}"
        # Look past the last value: [spaces] [,] [spaces] [// comment] end-of-line?
        m = re.compile(r"[ \t]*(,?)[ \t]*(//[^\r\n]*)?(?=\r?\n)").match(text, last.value.end)
        if m:
            eol = m.end()
            if m.group(1):  # comma already there
                return text[:eol] + nl + indent + entry + text[eol:]
            ve = last.value.end
            return text[:ve] + "," + text[ve:eol] + nl + indent + entry + text[eol:]
        ve = last.value.end
        return text[:ve] + "," + nl + indent + entry + text[ve:]

    close_indent = _line_indent(text, obj.start)
    indent = close_indent + unit
    entry = f"{json.dumps(keys[0], ensure_ascii=False)}: {_dumps(nested, indent, unit, nl)}"
    inner = text[obj.start + 1:obj.end - 1]
    if inner.strip() == "":
        return text[:obj.start + 1] + nl + indent + entry + nl + close_indent + text[obj.end - 1:]
    return text[:obj.start + 1] + nl + indent + entry + text[obj.start + 1:]


def set_jsonc_key(text: str, key_path: list[str], value: Any) -> str:
    """Return `text` with the value at `key_path` set, touching nothing else."""
    if not key_path:
        raise JsoncPathError("empty key path")
    node = _parse_tree(text)
    for depth, key in enumerate(key_path):
        if not isinstance(node, _Obj):
            raise JsoncPathError(f"{'.'.join(key_path[:depth]) or '<root>'} is not an object")
        member = next((m for m in reversed(node.members) if m.key == key), None)  # last duplicate wins
        if member is None:
            return _insert_member(text, node, key_path[depth:], value)
        if depth == len(key_path) - 1:
            v = member.value
            indent = _line_indent(text, member.key_start)
            nl = "\r\n" if "\r\n" in text else "\n"
            return text[:v.start] + _dumps(value, indent, _indent_unit(text), nl) + text[v.end:]
        node = member.value
    raise AssertionError("unreachable")


def update_jsonc_file(path: Path | str, key_path: list[str], value: Any, backup: bool = False) -> bool:
    """Set one key in a JSONC file in place. Returns False if it already had that value."""
    path = Path(path)
    raw = path.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")

    original = parse_jsonc(text)
    if get_path(original, key_path) == value:
        return False

    new_text = set_jsonc_key(text, key_path, value)
    expected = copy.deepcopy(original)
    _set_path(expected, key_path, value)
    if parse_jsonc(new_text) != expected:
        raise JsoncError(f"edit verification failed for {path}; file left untouched")

    if backup:
        path.with_name(path.name + ".bak").write_bytes(raw)
    atomic_write_text(path, ("﻿" if bom else "") + new_text)
    return True


# ------------------------------------------------------------------ actuator

@dataclass(frozen=True)
class ThemeTarget:
    name: str
    path: Path
    key_path: tuple[str, ...]
    mood_key: str  # which field of the mood definition holds the value


# Where VS Code keeps its user settings when the configured (Windows) path doesn't apply.
VSCODE_SETTINGS_ELSEWHERE = {
    "darwin": "~/Library/Application Support/Code/User/settings.json",
    "linux": "~/.config/Code/User/settings.json",
}


def platform_path(raw: str, platform: str = sys.platform) -> str:
    """A Windows-only setting (%APPDATA%/%LOCALAPPDATA%) on macOS/Linux -> the usual place there, if known."""
    if platform == "win32" or "%" not in raw:
        return raw
    if raw.replace("\\", "/").endswith("Code/User/settings.json"):
        key = "darwin" if platform == "darwin" else "linux"
        return VSCODE_SETTINGS_ELSEWHERE[key]
    return ""  # e.g. Windows Terminal: not on this platform


class WorkspaceActuator:
    def __init__(self, cfg: Mapping):
        ws = cfg.get("workspace", {})
        self.enabled = bool(ws.get("enabled", True))
        self.targets: list[ThemeTarget] = []
        if vscode := platform_path(ws.get("vscode_settings") or ""):
            self.targets.append(ThemeTarget("vscode", resolve_path(vscode),
                                            ("workbench.colorTheme",), "vscode_theme"))
        if ws.get("terminal_settings") and platform_path(ws["terminal_settings"]):
            self.targets.append(ThemeTarget("terminal", resolve_path(ws["terminal_settings"]),
                                            ("profiles", "defaults", "colorScheme"), "terminal_scheme"))
        self._backed_up: set[Path] = set()

    def apply(self, mood: Mapping) -> list[str]:
        """Apply a mood's themes. Blocking; the daemon runs it in a worker thread."""
        if not self.enabled:
            return []
        changed: list[str] = []
        for t in self.targets:
            value = mood.get(t.mood_key)
            if not value:
                continue
            if not t.path.exists():
                log.debug("%s settings not found at %s", t.name, t.path)
                continue
            started = time.perf_counter()
            try:
                did = update_jsonc_file(t.path, list(t.key_path), value, backup=t.path not in self._backed_up)
            except (OSError, JsoncError) as e:
                log.warning("could not update %s theme: %s", t.name, e)
                continue
            if did:
                self._backed_up.add(t.path)
                changed.append(f"{t.name}={value}")
                log.info("%s theme -> %s (%.0f ms)", t.name, value, (time.perf_counter() - started) * 1000)
        return changed
