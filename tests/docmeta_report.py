"""Accuracy and speed of mse/docmeta.py on the fictional corpora.

    .venv/bin/python -m tests.docmeta_report            # dev corpus + held-out set (if present) + timing
    .venv/bin/python -m tests.docmeta_report --errors   # also list every wrong field
    .venv/bin/python -m tests.docmeta_report --corpus tests/data/docmeta_heldout2.json [--module /path/to/older/docmeta.py]
                                                         # one corpus only, optionally with an older copy of the rules

The held-out set (tests/data/docmeta_heldout.json) was written without sight of the rules. Numbers are per field:
`correct` counts a right value and a right "nothing"; `precision` is right values among the values returned;
`recall` is right values among the values that exist.
"""

import json
import os
import random
import re
import statistics
import sys
import tempfile
import time
import unicodedata
from datetime import date
from pathlib import Path

os.environ.setdefault("MSE_HOME", tempfile.mkdtemp(prefix="mse-docmeta-report-"))

from mse import docmeta, extract  # noqa: E402
from tests.docmeta_corpus import DEV, TODAY  # noqa: E402

FIELDS = ("doctype", "doc_date", "amount", "currency", "counterparty", "reference")
HELDOUT = Path(__file__).parent / "data" / "docmeta_heldout.json"


def norm(s):
    if s is None:
        return None
    s = unicodedata.normalize("NFKD", str(s).lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", s)


def same(field, got, want) -> bool:
    if field == "amount":
        return (got is None and want is None) or (got is not None and want is not None and abs(got - want) < 0.005)
    if field in ("counterparty", "reference"):
        return norm(got) == norm(want)
    return got == want


def run(doc, chunked: bool = False):
    text = doc["text"]
    if chunked:
        text = "\n".join(extract.chunk_text(text))
    meta = {"kind": doc.get("kind")}
    return docmeta.analyse(text, doc.get("filename", ""), doc.get("ext", ""), meta, today=date.fromisoformat(TODAY))


def evaluate(docs, chunked: bool = False):
    stats = {f: {"n": 0, "ok": 0, "pred": 0, "pred_ok": 0, "exist": 0, "exist_ok": 0} for f in FIELDS}
    errors = []
    for d in docs:
        got = run(d, chunked)
        for f in FIELDS:
            want = d["expected"].get(f)
            ok = same(f, got.get(f), want)
            st = stats[f]
            st["n"] += 1
            st["ok"] += ok
            if got.get(f) is not None:
                st["pred"] += 1
                st["pred_ok"] += ok
            if want is not None:
                st["exist"] += 1
                st["exist_ok"] += ok
            if not ok:
                errors.append((d["id"], d.get("filename"), f, got.get(f), want))
    return stats, errors


def table(title, stats, n_docs):
    print(f"\n{title}  ({n_docs} documents)")
    print(f"  {'field':14s} {'correct':>9s} {'precision':>10s} {'recall':>8s}")
    for f in FIELDS:
        s = stats[f]
        pr = f"{100 * s['pred_ok'] / s['pred']:.1f}%" if s["pred"] else "-"
        rc = f"{100 * s['exist_ok'] / s['exist']:.1f}%" if s["exist"] else "-"
        print(f"  {f:14s} {100 * s['ok'] / s['n']:>8.1f}% {pr:>10s} {rc:>8s}")


def load_heldout():
    if not HELDOUT.exists():
        return []
    return json.loads(HELDOUT.read_text())["docs"]


def synthetic(n: int, seed: int = 7):
    """n documents made from the dev corpus by changing names, numbers and dates (for timing only)."""
    rnd = random.Random(seed)
    out = []
    for i in range(n):
        d = rnd.choice(DEV)
        text = d["text"]
        text = re.sub(r"\d{2}\.\d{2}\.\d{4}", lambda m: f"{rnd.randint(1, 28):02d}.{rnd.randint(1, 12):02d}.2025", text)
        text = re.sub(r"\d{1,3}(?:\.\d{3})*,\d{2}", lambda m: f"{rnd.randint(1, 9)}.{rnd.randint(100, 999)},{rnd.randint(10, 99)}", text)
        out.append({**d, "text": text + ("\n" + d["text"]) * rnd.randint(0, 3)})
    return out


def timing(n: int = 1000):
    docs = synthetic(n)
    t0 = time.perf_counter()
    per = []
    for d in docs:
        t = time.perf_counter()
        run(d)
        per.append((time.perf_counter() - t) * 1000)
    total = time.perf_counter() - t0
    per.sort()
    chars = statistics.mean(len(d["text"]) for d in docs)
    print(f"\ntiming: {n} synthetic documents, mean {chars:.0f} characters")
    print(f"  {total / n * 1000:.2f} ms per document (mean) · median {per[n // 2]:.2f} ms · p95 {per[int(n * .95)]:.2f} ms · "
          f"max {per[-1]:.2f} ms · {n / total:.0f} documents/s")


def use_module(path: str) -> None:
    """Evaluate an older copy of mse/docmeta.py (e.g. the rules as they were before a corpus was seen)."""
    global docmeta
    import importlib.util
    spec = importlib.util.spec_from_file_location("mse.docmeta_frozen", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mse.docmeta_frozen"] = mod
    spec.loader.exec_module(mod)
    docmeta = mod


def main():
    show_errors = "--errors" in sys.argv
    if "--module" in sys.argv:
        use_module(sys.argv[sys.argv.index("--module") + 1])
    if "--corpus" in sys.argv:
        docs = json.loads(Path(sys.argv[sys.argv.index("--corpus") + 1]).read_text())["docs"]
        for label, chunked in (("text as written", False), ("text as the indexer stores it (chunked)", True)):
            stats, errors = evaluate(docs, chunked)
            table(f"{Path(sys.argv[sys.argv.index('--corpus') + 1]).name}, {label}", stats, len(docs))
            if show_errors:
                for e in errors:
                    print("   wrong:", e)
        return
    for label, chunked in (("dev corpus, text as written", False), ("dev corpus, text as the indexer stores it (chunked)", True)):
        stats, errors = evaluate(DEV, chunked)
        table(label, stats, len(DEV))
        if show_errors:
            for e in errors:
                print("   wrong:", e)
    held = load_heldout()
    if held:
        for label, chunked in (("HELD-OUT set, text as written", False), ("HELD-OUT set, chunked", True)):
            stats, errors = evaluate(held, chunked)
            table(label, stats, len(held))
            if show_errors:
                for e in errors:
                    print("   wrong:", e)
    else:
        print("\n(no held-out set at tests/data/docmeta_heldout.json)")
    timing()


if __name__ == "__main__":
    main()
