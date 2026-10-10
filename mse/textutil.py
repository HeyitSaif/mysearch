"""Small text helpers: language guess, query terms, snippets, document dates."""

import html
import re
import unicodedata

STOP_EN = set("""a about above after again all also am an and any are as at be because been before being below
between both but by can could did do does doing down during each few for from further had has have having he her
here hers him his how i if in into is it its just me more most my no nor not now of off on once only or other our
out over own same she should so some such than that the their them then there these they this those through to too
under until up very was we were what when where which while who whom why will with would you your yours
please show find give get need want me my""".split())

STOP_DE = set("""aber alle allem allen aller alles als also am an ander andere anderem anderen anderer anderes anders
auch auf aus bei bin bis bist da damit dann das dass dein deine dem den der des dessen dich die dies diese diesem
diesen dieser dieses dir doch dort du durch ein eine einem einen einer eines er es etwas euch euer für gegen gewesen
hab habe haben hat hatte hatten hier hin hinter ich ihm ihn ihnen ihr ihre im in indem ins ist jede jedem jeden
jeder jedes jetzt kann kein keine können man manche mein meine mich mir mit muss nach nicht nichts noch nun nur ob
oder ohne sehr sein seine sich sie sind so solche soll sollte sondern sonst über um und uns unser unter vom von vor
war waren warum was weil welche wenn wer werde werden wie wieder will wir wird wo wollen würde zu zum zur zwar
zwischen wieviel""".split())

STOP = STOP_EN | STOP_DE
WORD = re.compile(r"[\w][\w\-./@&]*\w|\w", re.UNICODE)


def fold(s: str) -> str:
    """Lowercase and strip diacritics (matches FTS5 remove_diacritics 2)."""
    if s.isascii():                       # the common case (file names, English text): nothing to strip
        return s.lower()
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(ch for ch in s if not unicodedata.combining(ch)).replace("ß", "ss")


def fold_map(s: str) -> tuple[str, list[int]]:
    """fold() plus, for every folded character, the index of the original character it came from
    ('ß' becomes 'ss', so positions shift — highlights must be mapped back)."""
    out, idx = [], []
    for i, ch in enumerate(s):
        f = "".join(c for c in unicodedata.normalize("NFKD", ch.lower()) if not unicodedata.combining(c))
        f = f.replace("ß", "ss")
        out.append(f)
        idx.extend([i] * len(f))
    return "".join(out), idx


def detect_lang(text: str) -> str:
    words = [w.lower() for w in re.findall(r"[A-Za-zÄÖÜäöüß]+", text[:4000])]
    if not words:
        return "other"
    en = sum(w in STOP_EN for w in words)
    de = sum(w in STOP_DE for w in words) + 2 * sum(any(c in w for c in "äöüß") for w in words)
    if en == de == 0:
        # single terms: German compounds and umlauts are the strongest cue
        if any(c in text for c in "äöüßÄÖÜ"):
            return "de"
        return "unknown"
    return "de" if de > en else "en"


def query_terms(q: str) -> list[str]:
    """Meaningful words of a query, in order, without stop words or duplicates."""
    seen, out = set(), []
    for w in WORD.findall(q):
        w = w.strip("-./@&")
        if not w:
            continue
        lw = w.lower()
        if lw in STOP and len(re.findall(r"\w+", q)) > 1:
            continue
        if lw not in seen:
            seen.add(lw)
            out.append(w)
    return out


def fts_query(terms: list[str], phrases: list[str] | None = None, mode: str = "or") -> str | None:
    parts = []
    for p in phrases or []:
        p = p.replace('"', " ").strip()
        if p:
            parts.append(f'"{p}"')
    for t in terms:
        for tok in re.findall(r"\w+", fold(t)):
            if len(tok) >= 4 and not tok.isdigit():
                parts.append(f'"{tok}"*')
            else:
                parts.append(f'"{tok}"')
    if not parts:
        return None
    return (" AND " if mode == "and" else " OR ").join(parts)


def name_tokens(relpath: str) -> str:
    """Searchable words from a path: separators and camelCase split, extension kept."""
    s = relpath.replace("/", " / ")
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)
    s = re.sub(r"[_\-.+]+", " ", s)
    return f"{relpath} {s}"


_DATE_PATTERNS = [
    re.compile(r"(?<!\d)(20\d\d|19\d\d)[-_.](0[1-9]|1[0-2])[-_.](0[1-9]|[12]\d|3[01])(?!\d)"),
    re.compile(r"(?<!\d)(0[1-9]|[12]\d|3[01])\.(0[1-9]|1[0-2])\.(20\d\d)(?!\d)"),
    re.compile(r"(?<!\d)(20\d\d)[-_](0[1-9]|1[0-2])(?![\d])"),
]


def date_from_path(relpath: str) -> str | None:
    name = relpath.rsplit("/", 1)[-1]
    for target in (name, relpath):
        m = _DATE_PATTERNS[0].search(target)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        m = _DATE_PATTERNS[1].search(target)
        if m:
            return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
        m = _DATE_PATTERNS[2].search(target)
        if m:
            return f"{m.group(1)}-{m.group(2)}"
    return None


def snippet(text: str, terms: list[str], width: int = 260) -> str:
    """HTML-safe excerpt around the densest cluster of matching terms, matches wrapped in <mark>."""
    if not text:
        return ""
    clean = text
    # display only: drop markdown/table syntax so excerpts read as prose
    clean = re.sub(r"^\s*\|?[-:| ]{3,}\|?\s*$", " ", clean, flags=re.M)      # table rules
    clean = re.sub(r"\s*\|\s*(\|\s*)*", " · ", clean)                         # cell separators
    clean = re.sub(r"\*\*|__|`+|^#+\s*|^\s*[-*]\s+\[[ x]\]\s*", "", clean, flags=re.M)
    clean = re.sub(r"\[([^\]]{1,120})\]\([^)]{1,300}\)", r"\1", clean)       # [label](url) -> label
    clean = re.sub(r"\s+", " ", clean).strip(" ·")
    folded, fmap = fold_map(clean)
    keys = sorted({fold(t) for t in terms if len(t) >= 2}, key=len, reverse=True)
    hits = []
    for k in keys:
        for m in re.finditer(r"(?<!\w)" + re.escape(k), folded):
            hits.append((fmap[m.start()], fmap[m.end() - 1] + 1))     # back to positions in `clean`
    start = 0
    if hits:
        best, best_n = hits[0][0], 0
        for s, _ in hits:
            n = sum(1 for s2, _ in hits if s <= s2 < s + width)
            if n > best_n:
                best, best_n = s, n
        start = max(0, best - width // 4)
    end = min(len(clean), start + width)
    if start > 0:
        sp = clean.find(" ", start)
        start = sp + 1 if 0 <= sp < start + 30 else start
    piece_hits = sorted((s - start, e - start) for s, e in hits if s >= start and e <= end)
    out, pos = [], 0
    for s, e in piece_hits:
        if s < pos:
            continue
        out.append(html.escape(clean[start + pos:start + s]))
        out.append("<mark>" + html.escape(clean[start + s:start + e]) + "</mark>")
        pos = e
    out.append(html.escape(clean[start + pos:end]))
    return ("… " if start > 0 else "") + "".join(out) + (" …" if end < len(clean) else "")
