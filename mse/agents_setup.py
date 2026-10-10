"""Connect AI agents to a MySearch folder: add (or take back) a `mysearch` MCP server entry in each agent's config.

Per folder (the root of a collection) the entry starts `mse mcp` with MSE_CWD=<folder> and MSE_LOCK=1, so an
agent working there searches only that folder's collection:

  .mcp.json                     Claude Code (Grok CLI reads it too)            mcpServers.mysearch
  .claude/settings.local.json   Claude Code's approval of that project server  enabledMcpjsonServers += "mysearch"
  .cursor/mcp.json              Cursor (the Grok Bot app reads Cursor's file)  mcpServers.mysearch
  .codex/config.toml            Codex (read only in a trusted Codex project)   [mcp_servers.mysearch]
  .grok/config.toml             Grok CLI, project scope                        [mcp_servers.mysearch]
  opencode.json                 OpenCode                                       mcp.mysearch

Global (not tied to a folder; the server scopes itself from the agent's working directory, so no env):
Claude Desktop, ~/.cursor/mcp.json, ~/.grok/config.toml, ~/.codex/config.toml, ~/.gemini/config/mcp_config.json
(Antigravity). A global file is only written when its app's folder exists.

These files can hold other servers' API keys, so:
  - an edit is a minimal text splice: every byte outside the mysearch entry stays as it was (install then remove
    gives the original file back), and the result is re-parsed and compared before it is written;
  - writes are atomic, keep the file's mode (new files 0600) and go through symlinks instead of replacing them;
  - a file that does not parse (JSON with comments, odd TOML) is reported and left alone;
  - nothing here returns or prints a value read from these files: only file names, server names and states;
  - no file is ever deleted, not even one left empty.
"""

import json
import os
import re
import shutil
import stat
import sys
import tempfile
import threading
import tomllib
from copy import deepcopy
from pathlib import Path

from . import config, paths

SERVER = "mysearch"
LOCK_ENV = ("MSE_CWD", "MSE_LOCK")
OPENCODE_SCHEMA = "https://opencode.ai/config.json"
_write_lock = threading.Lock()


class Unreadable(Exception):
    """The file exists but cannot be edited safely; it is left untouched."""


def mse_bin() -> str:
    """Absolute path of this installation's `mse` command, found from the running interpreter / package."""
    for cand in (Path(sys.executable).parent / "mse", Path(sys.prefix) / "bin" / "mse",
                 paths.PROJECT / ".venv" / "bin" / "mse"):
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand.absolute())
    found = shutil.which("mse")
    return str(Path(found).absolute()) if found else str(paths.PROJECT / ".venv" / "bin" / "mse")


# ====================================================================== targets

class Target:
    def __init__(self, id, agent, rel, fmt, note="", app_dir=None):
        self.id, self.agent, self.rel, self.fmt, self.note, self.app_dir = id, agent, rel, fmt, note, app_dir

    # fmt: "json" (mcpServers.mysearch) | "opencode" (mcp.mysearch) | "toml" ([mcp_servers.mysearch]) | "approval"


FOLDER_TARGETS = [
    Target("claude", "Claude Code", ".mcp.json", "json", "Grok CLI also reads this file"),
    Target("claude-approval", "Claude Code (approval)", ".claude/settings.local.json", "approval"),
    Target("cursor", "Cursor", ".cursor/mcp.json", "json", "the Grok Bot app reads Cursor's MCP config"),
    Target("codex", "Codex", ".codex/config.toml", "toml"),
    Target("grok", "Grok CLI", ".grok/config.toml", "toml"),
    Target("opencode", "OpenCode", "opencode.json", "opencode"),
]

GLOBAL_TARGETS = [
    Target("claude-desktop", "Claude Desktop", "Library/Application Support/Claude/claude_desktop_config.json",
           "json", "quit Claude Desktop before installing (it can rewrite this file while running), then reopen it",
           app_dir="Library/Application Support/Claude"),
    Target("cursor", "Cursor", ".cursor/mcp.json", "json", app_dir=".cursor"),
    Target("grok", "Grok CLI", ".grok/config.toml", "toml", app_dir=".grok"),
    Target("codex", "Codex", ".codex/config.toml", "toml", app_dir=".codex"),
    Target("antigravity", "Antigravity", ".gemini/config/mcp_config.json", "json", app_dir=".gemini"),
]


def _folder_entry(fmt: str, root: Path) -> dict:
    env = {"MSE_CWD": str(root), "MSE_LOCK": "1"}
    if fmt == "opencode":
        return {"type": "local", "command": [mse_bin(), "mcp"], "environment": env, "enabled": True}
    return {"command": mse_bin(), "args": ["mcp"], "env": env}


def _global_entry() -> dict:
    return {"command": mse_bin(), "args": ["mcp"]}


def _env_of(entry: dict, fmt: str) -> dict:
    env = entry.get("environment" if fmt == "opencode" else "env")
    return env if isinstance(env, dict) else {}


def _merge_entry(cur, desired: dict, fmt: str, pinned: bool) -> dict:
    """The entry to write: the desired keys over the current entry (extra keys the user added are kept). For a
    global entry, MSE_CWD / MSE_LOCK are taken out of its env (other env vars stay)."""
    new = dict(cur) if isinstance(cur, dict) else {}
    env_key = "environment" if fmt == "opencode" else "env"
    cur_env = _env_of(new, fmt)
    for k, v in desired.items():
        if k != env_key:
            new[k] = v
    if pinned:
        new[env_key] = {**cur_env, **desired[env_key]}
    elif any(k in cur_env for k in LOCK_ENV):
        rest = {k: v for k, v in cur_env.items() if k not in LOCK_ENV}
        if rest:
            new[env_key] = rest
        else:
            new.pop(env_key, None)
    return new


def _same_dir(a, b: Path) -> bool:
    try:
        return Path(str(a)).expanduser().resolve() == Path(b).resolve()
    except Exception:
        return False


def _command_ok(entry: dict, fmt: str) -> bool:
    if fmt == "opencode":
        return entry.get("command") == [mse_bin(), "mcp"]
    return entry.get("command") == mse_bin() and entry.get("args") == ["mcp"]


# ====================================================================== file I/O

def _read(p: Path) -> str | None:
    """File text, None when the file does not exist; Unreadable when it cannot be read."""
    if not p.exists() and not p.is_symlink():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except FileNotFoundError:                      # dangling symlink
        return None
    except (OSError, UnicodeDecodeError) as e:
        raise Unreadable(f"cannot read ({type(e).__name__})")


def _write(p: Path, text: str) -> None:
    """Atomic write that keeps the file's mode (0600 for a new file) and writes through a symlink."""
    real = p.resolve() if p.is_symlink() else p
    mode = stat.S_IMODE(real.stat().st_mode) if real.exists() else 0o600
    real.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{real.name}.", suffix=".mse-tmp", dir=real.parent)   # created 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, real)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)          # our own temporary copy only; it may hold the file's secrets
        raise


# ====================================================================== JSON: minimal text splices
# Only ever run on text that json.loads accepted, so the scanner can assume valid JSON.

_WS = " \t\r\n"


def _ws(s: str, i: int) -> int:
    while i < len(s) and s[i] in _WS:
        i += 1
    return i


def _str_end(s: str, i: int) -> int:
    i += 1
    while s[i] != '"':
        i += 2 if s[i] == "\\" else 1
    return i + 1


def _val_end(s: str, i: int) -> int:
    c = s[i]
    if c == '"':
        return _str_end(s, i)
    if c in "{[":
        depth = 0
        while True:
            c = s[i]
            if c == '"':
                i = _str_end(s, i)
                continue
            if c in "{[":
                depth += 1
            elif c in "}]":
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
    j = i
    while j < len(s) and s[j] not in ",}]" + _WS:
        j += 1
    return j


def _members(s: str, start: int):
    """Object at s[start] == '{' -> ([(key, key_start, key_end, value_start, value_end)], index of '}')."""
    out, i = [], _ws(s, start + 1)
    if s[i] == "}":
        return out, i
    while True:
        ks = i
        ke = _str_end(s, i)
        i = _ws(s, _ws(s, ke) + 1)                   # past ':'
        ve = _val_end(s, i)
        out.append((json.loads(s[ks:ke]), ks, ke, i, ve))
        i = _ws(s, ve)
        if s[i] == "}":
            return out, i
        i = _ws(s, i + 1)                            # past ','


def _elements(s: str, start: int):
    """Array at s[start] == '[' -> ([(value_start, value_end)], index of ']')."""
    out, i = [], _ws(s, start + 1)
    if s[i] == "]":
        return out, i
    while True:
        ve = _val_end(s, i)
        out.append((i, ve))
        i = _ws(s, ve)
        if s[i] == "]":
            return out, i
        i = _ws(s, i + 1)


def _line_indent(s: str, pos: int) -> str:
    ls = s.rfind("\n", 0, pos) + 1
    m = re.match(r"[ \t]*", s[ls:pos])
    return m.group(0)


def _indent_unit(s: str) -> str:
    m = re.search(r"\n([ \t]+)\S", s)
    return m.group(1) if m else "  "


def _pretty(v, unit: str, base: str) -> str:
    """Indented JSON like json.dumps(indent=...), but lists of plain values stay on one line (["mcp"])."""
    inner = base + unit
    if isinstance(v, dict) and v:
        return "{\n" + ",\n".join(f"{inner}{json.dumps(k, ensure_ascii=False)}: {_pretty(x, unit, inner)}"
                                   for k, x in v.items()) + "\n" + base + "}"
    if isinstance(v, list) and any(isinstance(x, (dict, list)) for x in v):
        return "[\n" + ",\n".join(inner + _pretty(x, unit, inner) for x in v) + "\n" + base + "]"
    return json.dumps(v, ensure_ascii=False)


def _dump(value, s: str, base: str, compact: bool = False) -> str:
    if compact or "\n" not in s:
        return json.dumps(value, ensure_ascii=False)
    return _pretty(value, _indent_unit(s), base)


def _last(members, key):
    found = None
    for m in members:
        if m[0] == key:
            found = m
    return found


def _insert_member(s: str, obj_start: int, members, close: int, key: str, value) -> str:
    k = json.dumps(key, ensure_ascii=False)
    if members:
        last = members[-1]
        sep = s[last[2]:last[3]]
        sep = sep if sep.strip() == ":" and "\n" not in sep else ": "
        if "\n" in s[obj_start:close + 1]:
            ind = _line_indent(s, last[1])
            return s[:last[4]] + ",\n" + ind + k + sep + _dump(value, s, ind) + s[last[4]:]
        return s[:last[4]] + ", " + k + sep + _dump(value, s, "", compact=True) + s[last[4]:]
    if "\n" in s:
        outer = _line_indent(s, obj_start)
        ind = outer + _indent_unit(s)
        return s[:obj_start] + "{\n" + ind + k + ": " + _dump(value, s, ind) + "\n" + outer + "}" + s[close + 1:]
    return s[:obj_start] + "{" + k + ": " + _dump(value, s, "") + "}" + s[close + 1:]


def _cut(s: str, items, idx: int, start: int, close: int, first_of=lambda it: it[0], end_of=lambda it: it[-1]) -> str:
    """Remove item idx of an object/array (its separator included) so that the rest keeps its formatting."""
    if idx > 0:
        return s[:end_of(items[idx - 1])] + s[end_of(items[idx]):]
    if len(items) > 1:
        return s[:first_of(items[0])] + s[first_of(items[1]):]
    return s[:start + 1] + s[close:]


def _root(s: str) -> int:
    i = _ws(s, 0)
    if i >= len(s) or s[i] != "{":
        raise Unreadable("not a JSON object")
    return i


def _loads_json(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise Unreadable("not plain JSON (comments or trailing commas?)")


def json_upsert(text: str | None, parent: str, name: str, merge, new_doc) -> tuple[str, str]:
    """Set parent.name = merge(current or None). -> (new text, added|updated|present)."""
    if text is None or not text.strip():
        return _pretty(new_doc(), "  ", "") + "\n", "added"
    data = _loads_json(text)
    r = _root(text)
    if not isinstance(data, dict):
        raise Unreadable("not a JSON object")
    members, close = _members(text, r)
    pm = _last(members, parent)
    expected = deepcopy(data)
    if pm is None:
        value = merge(None)
        out, action = _insert_member(text, r, members, close, parent, {name: value}), "added"
        expected[parent] = {name: value}
    else:
        if text[pm[3]] != "{":
            raise Unreadable(f"'{parent}' is not an object")
        inner, iclose = _members(text, pm[3])
        em = _last(inner, name)
        if em is None:
            value = merge(None)
            out, action = _insert_member(text, pm[3], inner, iclose, name, value), "added"
        else:
            cur = json.loads(text[em[3]:em[4]])
            value = merge(cur)
            if value == cur:
                return text, "present"
            out = text[:em[3]] + _dump(value, text, _line_indent(text, em[1])) + text[em[4]:]
            action = "updated"
        expected[parent][name] = value
    if json.loads(out) != expected:
        raise Unreadable("could not be edited safely")
    return out, action


def json_get(text: str | None, parent: str, name: str):
    """(entry or None). Unreadable if the file is not a JSON object."""
    if text is None or not text.strip():
        return None
    data = _loads_json(text)
    if not isinstance(data, dict):
        raise Unreadable("not a JSON object")
    p = data.get(parent)
    if p is not None and not isinstance(p, dict):
        raise Unreadable(f"'{parent}' is not an object")
    return (p or {}).get(name)


def json_remove(text: str | None, parent: str, name: str, ours) -> tuple[str, str]:
    """Remove parent.name when ours(entry). -> (new text, removed|absent|kept). Containers left empty stay."""
    cur = json_get(text, parent, name)
    if cur is None:
        return text, "absent"
    if not ours(cur):
        return text, "kept"
    data = json.loads(text)
    r = _root(text)
    members, _ = _members(text, r)
    pm = _last(members, parent)
    inner, iclose = _members(text, pm[3])
    idx = max(i for i, m in enumerate(inner) if m[0] == name)
    out = _cut(text, inner, idx, pm[3], iclose, first_of=lambda m: m[1], end_of=lambda m: m[4])
    expected = deepcopy(data)
    del expected[parent][name]
    if json.loads(out) != expected:
        raise Unreadable("could not be edited safely")
    return out, "removed"


def json_list_add(text: str | None, key: str, item: str) -> tuple[str, str]:
    if text is None or not text.strip():
        return _pretty({key: [item]}, "  ", "") + "\n", "added"
    data = _loads_json(text)
    r = _root(text)
    if not isinstance(data, dict):
        raise Unreadable("not a JSON object")
    lst = data.get(key)
    if lst is not None and not isinstance(lst, list):
        raise Unreadable(f"'{key}' is not a list")
    if lst and item in lst:
        return text, "present"
    members, close = _members(text, r)
    m = _last(members, key)
    if m is None:
        out = _insert_member(text, r, members, close, key, [item])
    else:
        els, aclose = _elements(text, m[3])
        it = json.dumps(item, ensure_ascii=False)
        if not els:
            out = text[:m[3]] + "[" + it + "]" + text[aclose + 1:]
        elif "\n" in text[m[3]:aclose]:
            out = text[:els[-1][1]] + ",\n" + _line_indent(text, els[-1][0]) + it + text[els[-1][1]:]
        else:
            out = text[:els[-1][1]] + ", " + it + text[els[-1][1]:]
    expected = deepcopy(data)
    expected[key] = list(lst or []) + [item]
    if json.loads(out) != expected:
        raise Unreadable("could not be edited safely")
    return out, "added"


def json_list_remove(text: str | None, key: str, item: str) -> tuple[str, str]:
    if text is None or not text.strip():
        return text, "absent"
    data = _loads_json(text)
    if not isinstance(data, dict) or not isinstance(data.get(key), list) or item not in data[key]:
        return text, "absent"
    out = text
    while True:                                   # every occurrence, last first
        members, _ = _members(out, _root(out))
        m = _last(members, key)
        els, aclose = _elements(out, m[3])
        hits = [i for i, (a, b) in enumerate(els) if json.loads(out[a:b]) == item]
        if not hits:
            break
        out = _cut(out, els, hits[-1], m[3], aclose, first_of=lambda e: e[0], end_of=lambda e: e[1])
    expected = deepcopy(data)
    expected[key] = [x for x in data[key] if x != item]
    if json.loads(out) != expected:
        raise Unreadable("could not be edited safely")
    return out, "removed"


# ====================================================================== TOML: the [mcp_servers.mysearch] block

_HEADER = re.compile(r"^[ \t]*\[\[?(?P<key>[^\]]*)\]\]?[ \t]*(?:#.*)?$")
_BARE = re.compile(r"^[A-Za-z0-9_-]+$")
_BLOCK_KEY = "mcp_servers." + SERVER


def _hkey(line: str) -> str | None:
    m = _HEADER.match(line.rstrip("\r\n"))
    return re.sub(r"[\s\"']", "", m.group("key")) if m else None


def _toml_loads(text: str | None) -> dict:
    try:
        return tomllib.loads(text or "")
    except tomllib.TOMLDecodeError:
        raise Unreadable("not valid TOML")


def _toml_entry(data: dict):
    servers = data.get("mcp_servers")
    if servers is None:
        return None
    if not isinstance(servers, dict):
        raise Unreadable("'mcp_servers' is not a table")
    return servers.get(SERVER)


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float) and v == v and v not in (float("inf"), float("-inf")):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)          # a JSON string is a valid TOML basic string
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_toml_key(k)} = {_toml_value(x)}" for k, x in v.items()) + " }" if v else "{}"
    raise Unreadable("the mysearch entry holds a value this tool does not write; edit it by hand")


def _toml_key(k: str) -> str:
    return k if _BARE.match(k) else json.dumps(k, ensure_ascii=False)


def _toml_block(entry: dict) -> str:
    return f"[{_BLOCK_KEY}]\n" + "".join(f"{_toml_key(k)} = {_toml_value(v)}\n" for k, v in entry.items())


def _toml_span(lines: list[str]) -> tuple[int, int] | None:
    """[start, end) of the [mcp_servers.mysearch] block (sub-tables included, trailing blank/comment lines not)."""
    start = end = None
    for i, ln in enumerate(lines):
        k = _hkey(ln)
        if k is None:
            continue
        mine = k == _BLOCK_KEY or k.startswith(_BLOCK_KEY + ".")
        if start is None and mine:
            start = i
        elif start is not None and not mine:
            end = i
            break
    if start is None:
        return None
    end = len(lines) if end is None else end
    while end > start + 1 and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")):
        end -= 1
    return start, end


def _norm_data(d: dict) -> dict:
    d = deepcopy(d)
    if d.get("mcp_servers") == {}:
        del d["mcp_servers"]
    return d


def toml_upsert(text: str | None, merge, comment: str) -> tuple[str, str]:
    data = _toml_loads(text)
    cur = _toml_entry(data)
    if cur is not None and not isinstance(cur, dict):
        raise Unreadable("the mysearch entry is not a table")
    value = merge(cur)
    if cur is not None and value == cur:
        return text, "present"
    block = _toml_block(value)
    if cur is None:
        head = text or ""
        if head.strip():
            head = (head if head.endswith("\n") else head + "\n") + "\n"
        else:
            head = ""
        out, action = head + comment + block, "added"
    else:
        lines = (text or "").splitlines(keepends=True)
        span = _toml_span(lines)
        if span is None:
            raise Unreadable("the mysearch entry is written in a form this tool does not edit; edit it by hand")
        out, action = "".join(lines[:span[0]]) + block + "".join(lines[span[1]:]), "updated"
    expected = deepcopy(data)
    expected.setdefault("mcp_servers", {})[SERVER] = value
    if _norm_data(_toml_loads(out)) != _norm_data(expected):
        raise Unreadable("could not be edited safely")
    return out, action


def toml_remove(text: str | None, ours) -> tuple[str, str]:
    data = _toml_loads(text)
    cur = _toml_entry(data)
    if cur is None:
        return text, "absent"
    if not isinstance(cur, dict) or not ours(cur):
        return text, "kept"
    lines = text.splitlines(keepends=True)
    span = _toml_span(lines)
    if span is None:
        raise Unreadable("the mysearch entry is written in a form this tool does not edit; edit it by hand")
    start, end = span
    if start > 0 and lines[start - 1].startswith("# MySearch"):
        start -= 1
    if start > 0 and not lines[start - 1].strip() and (end == len(lines) or not lines[end].strip()):
        start -= 1                                       # the blank line `install` put before the block
    out = "".join(lines[:start]) + "".join(lines[end:])
    expected = deepcopy(data)
    del expected["mcp_servers"][SERVER]
    if _norm_data(_toml_loads(out)) != _norm_data(expected):
        raise Unreadable("could not be edited safely")
    return out, "removed"


# ====================================================================== status

def codex_trust(root: Path, home: Path | None = None) -> str | None:
    """trust_level of [projects."<root>"] in ~/.codex/config.toml, or None. That file holds tokens: only this one
    key is looked at, nothing else is kept or returned."""
    p = (home or Path.home()) / ".codex" / "config.toml"
    try:
        projects = tomllib.loads(p.read_text(encoding="utf-8")).get("projects")
    except Exception:
        return None
    if not isinstance(projects, dict):
        return None
    for key in dict.fromkeys((str(root), str(Path(root).resolve()))):
        level = projects.get(key, {}).get("trust_level") if isinstance(projects.get(key), dict) else None
        if level is not None:
            return str(level)
    return None


def _entry_of(t: Target, text: str | None):
    if t.fmt == "toml":
        return _toml_entry(_toml_loads(text))
    return json_get(text, "mcp" if t.fmt == "opencode" else "mcpServers", SERVER)


def _row(t: Target, p: Path, state: str, note: str = "") -> dict:
    notes = "; ".join(x for x in (note, t.note) if x)
    return {"id": t.id, "agent": t.agent, "file": str(p), "state": state, "note": notes}


def _folder_row(t: Target, root: Path, home: Path | None) -> dict:
    p = root / t.rel
    try:
        text = _read(p)
        if t.fmt == "approval":
            return _approval_row(t, p, text, root)
        entry = _entry_of(t, text)
    except Unreadable as e:
        return _row(t, p, "unreadable", f"{e}; left untouched")
    trust = ""
    if t.id == "codex":
        level = codex_trust(root, home)
        trust = ("trusted Codex project" if level == "trusted" else
                 "Codex reads this file only in a trusted project, and this folder is not trusted in "
                 "~/.codex/config.toml (open Codex here once and trust it)")
    if entry is None:
        return _row(t, p, "missing", trust)
    if not isinstance(entry, dict):
        return _row(t, p, "unreadable", "the mysearch entry is not an object; left untouched")
    env = _env_of(entry, t.fmt)
    cwd = env.get("MSE_CWD")
    if not cwd:
        return _row(t, p, "other", "a mysearch entry not pinned to a folder (no MSE_CWD): not locked")
    if not _same_dir(cwd, root):
        return _row(t, p, "other", "a mysearch entry pinned to another folder")
    notes = []
    if str(env.get("MSE_LOCK", "")).strip().lower() not in ("1", "true", "yes", "on"):
        notes.append("not locked (MSE_LOCK missing)")
    if not _command_ok(entry, t.fmt):
        notes.append("starts a different mse command than this installation; `mse agents install` updates it")
    if t.fmt == "opencode" and entry.get("enabled") is False:
        notes.append("disabled in opencode.json")
    if trust:
        notes.append(trust)
    return _row(t, p, "connected", "; ".join(notes))


def _approval_row(t: Target, p: Path, text: str | None, root: Path) -> dict:
    data = _loads_json(text) if text and text.strip() else {}
    if not isinstance(data, dict):
        raise Unreadable("not a JSON object")
    enabled = data.get("enabledMcpjsonServers")
    disabled = data.get("disabledMcpjsonServers")
    if isinstance(disabled, list) and SERVER in disabled:
        return _row(t, p, "other", "mysearch is listed in disabledMcpjsonServers")
    if isinstance(enabled, list) and SERVER in enabled:
        return _row(t, p, "connected")
    if data.get("enableAllProjectMcpServers") is True:
        return _row(t, p, "connected", "all project MCP servers are enabled")
    return _row(t, p, "missing", "Claude Code will ask to approve the server on first start")


def _global_row(t: Target, home: Path) -> dict:
    p = home / t.rel
    if not (home / t.app_dir).is_dir():
        return _row(t, p, "missing", f"{t.agent} is not installed (no ~/{t.app_dir})")
    try:
        entry = _entry_of(t, _read(p))
    except Unreadable as e:
        return _row(t, p, "unreadable", f"{e}; left untouched")
    if entry is None:
        return _row(t, p, "missing")
    if not isinstance(entry, dict):
        return _row(t, p, "unreadable", "the mysearch entry is not an object; left untouched")
    if any(k in _env_of(entry, t.fmt) for k in LOCK_ENV):
        return _row(t, p, "other", "a mysearch entry pinned to a folder (MSE_CWD/MSE_LOCK)")
    note = "" if _command_ok(entry, t.fmt) else \
        "starts a different mse command than this installation; `mse agents install --global` updates it"
    return _row(t, p, "connected", note)


# ====================================================================== public API

def folder_of(target: str) -> tuple[Path, str | None]:
    """A collection name or a folder path -> (folder, collection name or None)."""
    cfg = config.load()
    c = config.get_collection(cfg, target)
    if c:
        return Path(c["path"]), c["name"]
    if "/" in target or target.startswith(("~", ".")):
        root = Path(target).expanduser().resolve()
        c = config.collection_for_path(cfg, root)
        return root, c["name"] if c and _same_dir(c["path"], root) else None
    raise KeyError(f"no collection named '{target}'")


def status(target: str, home: Path | None = None) -> list[dict]:
    """Per agent file: {id, agent, file, state: connected|missing|other|unreadable, note}."""
    root, _ = folder_of(target)
    return [_folder_row(t, root, home) for t in FOLDER_TARGETS]


def install(target: str, home: Path | None = None) -> list[dict]:
    """Write or merge the mysearch entry (MSE_CWD=<folder>, MSE_LOCK=1) into every agent file of a collection's
    folder. -> [{id, agent, file, action: added|updated|present|skipped, note}]."""
    root, name = folder_of(target)
    if not name:
        raise ValueError(f"{root} is not the folder of a collection: add it first (`mse add {root}`)")
    if not root.is_dir():
        raise ValueError(f"folder not found: {root}")
    out = []
    with _write_lock:
        for t in FOLDER_TARGETS:
            out.append(_apply(t, root / t.rel, "install", _folder_entry(t.fmt, root), root, home))
    return out


def remove(target: str) -> list[dict]:
    """Take back only the mysearch entries that point at this folder (and the Claude Code approval once no
    mysearch server is left in .mcp.json). Works for a removed collection too, given its folder path.
    -> [{id, agent, file, action: removed|absent|kept|skipped, note}]."""
    root, _ = folder_of(target)
    out = []
    with _write_lock:
        for t in FOLDER_TARGETS:
            out.append(_apply(t, root / t.rel, "remove", None, root))
    return out


def status_global(home: Path | None = None) -> list[dict]:
    home = home or Path.home()
    return [_global_row(t, home) for t in GLOBAL_TARGETS]


def install_global(home: Path | None = None) -> list[dict]:
    """The unpinned entry (no env) in each installed app's user config."""
    home = home or Path.home()
    out = []
    with _write_lock:
        for t in GLOBAL_TARGETS:
            p = home / t.rel
            if not (home / t.app_dir).is_dir():
                out.append(_result(t, p, "skipped", f"{t.agent} is not installed (no ~/{t.app_dir})"))
                continue
            out.append(_apply(t, p, "install", _global_entry(), None))
    return out


def remove_global(home: Path | None = None) -> list[dict]:
    """Take back the unpinned mysearch entries from the user configs (a pinned one is left alone)."""
    home = home or Path.home()
    out = []
    with _write_lock:
        for t in GLOBAL_TARGETS:
            p = home / t.rel
            out.append(_apply(t, p, "remove", None, None) if p.exists() else _result(t, p, "absent"))
    return out


def _result(t: Target, p: Path, action: str, note: str = "") -> dict:
    return {"id": t.id, "agent": t.agent, "file": str(p), "action": action, "note": note}


def _apply(t: Target, p: Path, op: str, desired: dict | None, root: Path | None, home: Path | None = None) -> dict:
    pinned = root is not None

    def ours(entry: dict) -> bool:
        env = _env_of(entry, t.fmt)
        if pinned:
            return bool(env.get("MSE_CWD")) and _same_dir(env["MSE_CWD"], root)
        return not any(k in env for k in LOCK_ENV) and Path(str(
            (entry.get("command") or [""])[0] if t.fmt == "opencode" else entry.get("command") or "")).name == "mse"

    try:
        text = _read(p)
        if t.fmt == "approval":
            if op == "install":
                out, action = json_list_add(text, "enabledMcpjsonServers", SERVER)
            else:
                still = None
                try:
                    still = json_get(_read(root / ".mcp.json"), "mcpServers", SERVER)
                except Unreadable:
                    still = True                       # cannot tell: keep the approval
                if still is not None:
                    return _result(t, p, "kept", "a mysearch server is still defined in .mcp.json")
                out, action = json_list_remove(text, "enabledMcpjsonServers", SERVER)
        elif t.fmt == "toml":
            if op == "install":
                comment = ("# MySearch: local search over this folder, locked to its collection "
                           "(written by `mse agents install`)\n" if pinned else
                           "# MySearch: local search; scopes itself to the folder the agent works in "
                           "(written by `mse agents install --global`)\n")
                out, action = toml_upsert(text, lambda cur: _merge_entry(cur, desired, t.fmt, pinned), comment)
            else:
                out, action = toml_remove(text, ours)
        else:
            parent = "mcp" if t.fmt == "opencode" else "mcpServers"
            if op == "install":
                def new_doc():
                    doc = {parent: {SERVER: desired}}
                    return {"$schema": OPENCODE_SCHEMA, **doc} if t.fmt == "opencode" else doc
                out, action = json_upsert(text, parent, SERVER,
                                          lambda cur: _merge_entry(cur, desired, t.fmt, pinned), new_doc)
            else:
                out, action = json_remove(text, parent, SERVER, ours)
    except Unreadable as e:
        return _result(t, p, "skipped", f"{e}; left untouched")
    note = ""
    if action == "kept":
        note = "a mysearch entry that does not point at this folder; left alone" if pinned else \
            "a mysearch entry pinned to a folder; left alone"
    if action in ("added", "updated", "removed") and out != text:
        try:
            _write(p, out)
        except OSError as e:
            return _result(t, p, "skipped", f"could not write ({type(e).__name__}); left untouched")
    if t.id == "codex" and pinned and op == "install" and codex_trust(root, home) != "trusted":
        note = "Codex reads this file only when the folder is a trusted Codex project; trust it in Codex"
    return _result(t, p, action, note)
