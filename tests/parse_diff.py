"""How many queries of a gold set are read differently by this version of search.parse than by an older one. Prints
only counts unless --show is given, so a private gold set can be checked without printing it.

    git archive HEAD mse | tar -x -C /some/empty/folder           # the older version (any commit)
    .venv/bin/python -m tests.parse_diff --old /some/empty/folder path/to/your_gold_sets/*.json [--show]

The gold files are JSON like eval/gold_example.json: {"queries": [{"q": "...", ...}]}.
"""

import json
import os
import subprocess
import sys
import tempfile

os.environ.setdefault("MSE_HOME", tempfile.mkdtemp(prefix="mse-parse-diff-"))

CHILD = """
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ['MSE_HOME'] = sys.argv[2]
from mse import search
qs = json.load(sys.stdin)
print(json.dumps([[search.parse(q)[0], search.parse(q)[1]] for q in qs], default=list))
"""


def queries(paths):
    out = []
    for p in paths:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        items = data.get("queries", data) if isinstance(data, dict) else data
        out += [x["q"] if isinstance(x, dict) else str(x) for x in items if (x.get("q") if isinstance(x, dict) else x)]
    return out


def parse_with(root, qs):
    r = subprocess.run([sys.executable, "-c", CHILD, root, tempfile.mkdtemp(prefix="mse-parse-diff-")], input=json.dumps(qs),
                       capture_output=True, text=True)
    if r.returncode:
        sys.exit(r.stderr[-1500:])
    return json.loads(r.stdout)


def main(argv):
    show = "--show" in argv
    if "--old" not in argv:
        sys.exit(__doc__)
    old = argv[argv.index("--old") + 1]
    files = [a for a in argv[1:] if not a.startswith("--") and a != old]
    qs = queries(files)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    a, b = parse_with(old, qs), parse_with(here, qs)
    diff = [q for q, x, y in zip(qs, a, b) if x != y]
    print(f"{len(qs)} queries, {len(diff)} read differently")
    if show:
        for q in diff:
            print("  ", q)


if __name__ == "__main__":
    main(sys.argv)
