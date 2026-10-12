"""Structure of what the MCP server advertises: every tool's input schema (checked by hand, jsonschema is not a dependency),
the annotations, the prompt and resource-template definitions, and the cost of importing the module. Agents depend on these
contracts, so a malformed schema is a test failure, not a client-side surprise.
Run: .venv/bin/python -m unittest -v tests.test_mcp_schema"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("MSE_HOME", tempfile.mkdtemp(prefix="mse-test-"))

from mse import config, docmeta, mcp  # noqa: E402

# the keywords the codebase already used, plus pattern and enum
KEYWORDS = {"type", "description", "default", "minimum", "maximum", "enum", "items", "required", "properties", "maxItems",
            "minItems", "pattern", "anyOf", "title"}
JSON_TYPES = {"string", "integer", "number", "boolean", "array", "object"}
PY_TYPES = {"string": str, "boolean": bool, "array": list, "object": dict}


def is_type(value, t) -> bool:
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, PY_TYPES[t])


def check(test, where: str, s) -> None:
    """One (sub)schema. Raises through `test` with the path of the problem."""
    test.assertIsInstance(s, dict, where)
    test.assertTrue(set(s) <= KEYWORDS, f"{where}: unexpected keyword(s) {sorted(set(s) - KEYWORDS)}")
    test.assertTrue("type" in s or "enum" in s or "anyOf" in s, f"{where}: no type, enum or anyOf")
    t = s.get("type")
    if t is not None:
        test.assertIn(t, JSON_TYPES, f"{where}: type {t!r}")
    if "description" in s:
        test.assertIsInstance(s["description"], str, where)
        test.assertTrue(s["description"].strip(), f"{where}: empty description")
    if "enum" in s:
        test.assertIsInstance(s["enum"], list, where)
        test.assertTrue(s["enum"], f"{where}: empty enum")
        test.assertEqual(len(set(map(repr, s["enum"]))), len(s["enum"]), f"{where}: duplicate enum values")
        if t:
            test.assertTrue(all(is_type(v, t) for v in s["enum"]), f"{where}: enum value of the wrong type")
    if "default" in s:
        if t:
            test.assertTrue(is_type(s["default"], t), f"{where}: default {s['default']!r} is not a {t}")
        if "enum" in s:
            test.assertIn(s["default"], s["enum"], f"{where}: default not in enum")
    lo, hi = s.get("minimum"), s.get("maximum")
    for name, v in (("minimum", lo), ("maximum", hi)):
        if v is not None:
            test.assertTrue(is_type(v, "number"), f"{where}: {name} is not a number")
            test.assertIn(t, ("integer", "number"), f"{where}: {name} on a {t}")
    if lo is not None and hi is not None:
        test.assertLessEqual(lo, hi, where)
    if "default" in s and isinstance(s["default"], (int, float)) and not isinstance(s["default"], bool):
        if lo is not None:
            test.assertGreaterEqual(s["default"], lo, where)
        if hi is not None:
            test.assertLessEqual(s["default"], hi, where)
    for name in ("maxItems", "minItems"):
        if name in s:
            test.assertTrue(is_type(s[name], "integer") and s[name] >= 0, f"{where}: {name}")
    if "maxItems" in s and "minItems" in s:
        test.assertLessEqual(s["minItems"], s["maxItems"], where)
    if "pattern" in s:
        test.assertEqual(t, "string", f"{where}: pattern on a {t}")
        re.compile(s["pattern"])
    if t == "array":
        test.assertIn("items", s, f"{where}: array without items")
    if "items" in s:
        test.assertEqual(t, "array", f"{where}: items on a {t}")
        check(test, where + "[]", s["items"])
    if "anyOf" in s:
        test.assertIsInstance(s["anyOf"], list, where)
        test.assertTrue(s["anyOf"], where)
        for i, sub in enumerate(s["anyOf"]):
            check(test, f"{where}.anyOf[{i}]", sub)
    if "properties" in s:
        test.assertEqual(t, "object", f"{where}: properties on a {t}")
        check_object(test, where, s)
    elif "required" in s:
        test.assertEqual(t, "object", f"{where}: required on a {t}")
        check_object(test, where, s)


def check_object(test, where: str, s: dict) -> None:
    props = s.get("properties", {})
    test.assertIsInstance(props, dict, where)
    for name, sub in props.items():
        test.assertRegex(name, r"^[A-Za-z_][A-Za-z0-9_]*$", f"{where}.{name}")
        check(test, f"{where}.{name}", sub)
    req = s.get("required", [])
    test.assertIsInstance(req, list, where)
    test.assertEqual(len(set(req)), len(req), f"{where}: duplicate required")
    test.assertTrue(set(req) <= set(props), f"{where}: required {sorted(set(req) - set(props))} is not a property")


def tool_sets():
    nowhere = tempfile.mkdtemp(prefix="mse-mcp-schema-")
    root = Path(tempfile.mkdtemp(prefix="mse-mcp-schema-")).resolve()
    name = "ms" + root.name[-6:].lower().replace("_", "x")
    config.add_collection(str(root), name)
    try:
        yield "unlocked", mcp.tools_for(None)
        yield "locked", mcp.tools_for(mcp.Lock(str(root)))
        yield "locked, no collection", mcp.tools_for(mcp.Lock(nowhere))
    finally:
        config.remove_collection(name)


class Tools(unittest.TestCase):
    def test_every_input_schema_is_well_formed(self):
        for label, tools in tool_sets():
            names = [t["name"] for t in tools]
            self.assertEqual(len(set(names)), len(names), label)
            self.assertEqual(names, ["search", "get", "multi_get", "similar", "collections", "mark_best", "sync", "ask", "timeline", "facets"])
            for t in tools:
                where = f"{label}:{t['name']}"
                self.assertRegex(t["name"], r"^[A-Za-z0-9_-]{1,64}$", where)
                self.assertTrue(t["title"].strip() and t["description"].strip(), where)
                schema = t["inputSchema"]
                self.assertEqual(schema["type"], "object", where)
                self.assertIsInstance(schema["properties"], dict, where)
                check_object(self, where, schema)
                self.assertTrue(set(schema) <= {"type", "properties", "required"}, where)
                json.dumps(t, ensure_ascii=False)                                  # serialisable as it is

    def test_documented_defaults_and_required_arguments_did_not_move(self):
        tools = {t["name"]: t["inputSchema"] for t in mcp.TOOLS}
        self.assertEqual(tools["search"]["required"], ["query"])
        self.assertEqual(tools["get"]["required"], ["ref"])
        self.assertEqual(tools["multi_get"]["required"], ["refs"])
        self.assertEqual(tools["similar"]["required"], ["ref"])
        self.assertEqual(tools["mark_best"]["required"], ["query", "ref"])
        self.assertEqual(tools["ask"]["required"], ["question"])
        self.assertNotIn("required", tools["timeline"])
        self.assertNotIn("required", tools["facets"])
        self.assertEqual(tools["search"]["properties"]["limit"], {"type": "integer", "default": 10, "minimum": 1, "maximum": 50})
        self.assertEqual(tools["search"]["properties"]["mode"]["enum"], ["hybrid", "keyword", "semantic"])
        self.assertIs(tools["search"]["properties"]["precise"]["default"], True)
        self.assertEqual(tools["get"]["properties"]["max_chars"]["default"], 12000)
        self.assertEqual(tools["get"]["properties"]["page"]["minimum"], 1)

    def test_the_doctype_enum_is_the_documents_module_s(self):
        self.assertEqual(mcp.DOCTYPES, docmeta.DOCTYPES)
        tools = {t["name"]: t["inputSchema"]["properties"] for t in mcp.TOOLS}
        self.assertEqual(tools["search"]["doctype"]["items"]["enum"], list(docmeta.DOCTYPES))
        self.assertEqual(tools["timeline"]["type"]["items"]["enum"], list(docmeta.DOCTYPES))
        self.assertEqual(tools["facets"]["type"]["items"]["enum"], list(docmeta.DOCTYPES))
        self.assertIn("other", tools["search"]["doctype"]["items"]["enum"])
        for name in ("doctype",):
            self.assertEqual(tools["search"][name]["type"], "array")

    def test_schema_patterns_agree_with_the_validators(self):
        props = {t["name"]: t["inputSchema"]["properties"] for t in mcp.TOOLS}
        date = re.compile(props["search"]["docdate_from"]["pattern"])
        for ok in ("2025", "2025-03", "2025-03-14"):
            self.assertTrue(date.search(ok), ok)
            self.assertEqual(mcp._check_date("x", ok), ok)
        for bad in ("25", "2025-3", "2025-03-1", "2025/03", "2025-03-14T", "x2025", "2025-03-14 "):
            self.assertFalse(date.search(bad), bad)
        for key in ("docdate_to",):
            self.assertEqual(props["search"][key]["pattern"], props["search"]["docdate_from"]["pattern"])
        self.assertEqual(props["timeline"]["from"]["pattern"], props["search"]["docdate_from"]["pattern"])
        cur = re.compile(props["search"]["currency"]["pattern"])
        for ok in ("EUR", "usd", "Chf"):
            self.assertTrue(cur.search(ok) and mcp._doc_filter_pairs({"currency": ok}), ok)
        for bad in ("EU", "EURO", "E1R"):
            self.assertFalse(cur.search(bad), bad)
        self.assertEqual(props["search"]["amount_min"]["maximum"], 999999999999)
        self.assertEqual(props["search"]["amount_min"]["minimum"], 0)
        self.assertIn("120 characters", props["search"]["party"]["description"])             # enforced at call time (see test_mcp_filters)

    def test_annotations(self):
        allowed = {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}
        for t in mcp.TOOLS:
            self.assertTrue(set(t["annotations"]) <= allowed, t["name"])
            self.assertTrue(all(isinstance(v, bool) for v in t["annotations"].values()), t["name"])
            if not t["annotations"]["readOnlyHint"]:
                self.assertIs(t["annotations"].get("destructiveHint"), False, t["name"])           # nothing here deletes
        self.assertEqual({t["name"] for t in mcp.TOOLS if not t["annotations"]["readOnlyHint"]}, {"mark_best", "sync"})

    def test_the_lock_rewrites_only_descriptions(self):
        for label, tools in tool_sets():
            if label == "unlocked":
                continue
            for new, old in zip(tools, mcp.TOOLS):
                self.assertEqual(new["name"], old["name"])
                a, b = json.loads(json.dumps(new)), json.loads(json.dumps(old))
                for d in (a, b):
                    d.pop("description", None)
                    d["inputSchema"]["properties"].get("collection", {}).pop("description", None)
                self.assertEqual(a, b, (label, new["name"]))


class Definitions(unittest.TestCase):
    def test_prompts(self):
        names = [p["name"] for p in mcp.PROMPTS]
        self.assertEqual(len(set(names)), len(names))
        for p in mcp.PROMPTS:
            self.assertRegex(p["name"], r"^[a-z][a-z0-9-]*$")
            self.assertTrue(p["title"].strip() and p["description"].strip())
            keys = [a["name"] for a in p["arguments"]]
            self.assertEqual(len(set(keys)), len(keys), p["name"])
            for a in p["arguments"]:
                self.assertIsInstance(a["required"], bool)
                self.assertTrue(a["description"].strip())
            self.assertIn(p["name"], mcp._PROMPT_BUILDERS)
        json.dumps(mcp.PROMPTS)

    def test_resource_templates(self):
        for t in mcp.RESOURCE_TEMPLATES:
            self.assertRegex(t["uriTemplate"], r"^mysearch://[a-z]+/\{[a-z]+\}$")
            self.assertTrue(all(t[k].strip() for k in ("name", "title", "description")))
        self.assertEqual(mcp.URI_COLLECTIONS, "mysearch://collections")
        json.dumps(mcp.RESOURCE_TEMPLATES)


class ImportCost(unittest.TestCase):
    """`mse mcp` starts once per agent session: importing the module must not pull in the document-facts module (about 30 ms
    and a large table of patterns), and serving the protocol calls that do not touch a document must not either."""

    def run_code(self, code: str) -> str:
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "MSE_HOME": os.environ["MSE_HOME"]}
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=str(Path(mcp.__file__).parent.parent),
                             timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip()

    def test_docmeta_is_loaded_lazily(self):
        code = """
import sys
from mse import mcp
loaded = lambda: 'mse.docmeta' in sys.modules
assert not loaded(), 'import'
for m, p in (('initialize', None), ('tools/list', None), ('prompts/list', None), ('resources/templates/list', None),
             ('prompts/get', {'name': 'missing-receipts', 'arguments': {'year': '2025'}})):
    r = mcp.handle({'jsonrpc': '2.0', 'id': 1, 'method': m, 'params': p or {}}, None, None)
    assert 'result' in r, (m, r)
assert not loaded(), 'protocol calls'
class C:
    def search(self, q, colls, limit, filters, mode, precise=False):
        return {'query': q, 'collections': [], 'results': [], 'bridge': {}}
mcp.call_tool('search', {'query': 'x', 'doctype': ['invoice'], 'party': 'Acme Parts'}, C(), None)
assert not loaded(), 'search with filters that need no date check'
mcp.call_tool('search', {'query': 'x', 'docdate_from': '2025'}, C(), None)
assert loaded(), 'a date range is checked with the real parser'
print('ok')
"""
        self.assertEqual(self.run_code(code), "ok")


if __name__ == "__main__":
    unittest.main()
