"""Mask personal identifiers in text that leaves for an agent (MCP, piped CLI).

The user sees everything in the app. Agents get IBANs, passport / ID / permit numbers, personal tax IDs,
social-security numbers and dates of birth masked. Company identifiers (commercial register number, company
tax number, VAT id) stay.
"""

import re

MASK = "[redacted]"

_LABELLED = [
    # label words, then the value that follows within a few characters
    (r"passport|reisepass|pass(?:port)?[\s-]*(?:no|nr|number|nummer)\.?|passnummer", r"[A-Z0-9]{6,12}"),
    (r"aufenthaltstitel|residence\s+permit|dokumentennummer|document\s+(?:no|number)|karten-?nr|card\s+(?:no|number)"
     r"|ausweis(?:nummer)?|id[\s-]+card|personalausweis|seriennummer|serial\s+number", r"[A-Z0-9]{8,12}"),
    (r"steuer-?id|steueridentifikationsnummer|identifikationsnummer|tax\s+id(?:entification)?(?:\s+number)?|tin",
     r"\d{2}\s?\d{3}\s?\d{3}\s?\d{3}"),
    (r"sozialversicherungsnummer|rentenversicherungsnummer|social\s+security(?:\s+number)?|sv-?nummer",
     r"[0-9]{2}\s?[0-9]{6}\s?[A-Z]\s?[0-9]{3}"),
    (r"date\s+of\s+birth|birth\s*date|d\.?o\.?b\.?|geburtsdatum|geb\.|geboren\s+am",
     r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{4}-\d{2}-\d{2}|\d{1,2}\.?\s+[A-Za-zäöü]+\s+\d{4}"),
]
_GAP = r"(?:\W{0,4}(?:nr|no|nummer|number|#)\.?)?\W{0,15}?"
_RX_LABELLED = [re.compile(rf"(?i)\b({label})({_GAP})({value})\b") for label, value in _LABELLED]
_RX_IBAN = re.compile(r"\b([A-Z]{2}\d{2})((?:[ ]?[A-Z0-9]{4}){2,7})([ ]?[A-Z0-9]{1,4})\b")
_RX_UAE_ID = re.compile(r"\b784-?\d{4}-?\d{7}-?\d\b")
_RX_CNIC = re.compile(r"\b\d{5}-\d{7}-\d\b")
_RX_MRZ = re.compile(r"(?<![A-Z0-9<])[A-Z0-9<]{30,46}(?![A-Z0-9<])")          # machine-readable zone of passports / ID cards


def _iban(m) -> str:
    compact = re.sub(r"\s", "", m.group(0))
    return f"{compact[:2]}•••• {compact[-4:]}" if len(compact) >= 15 else m.group(0)


def redact(text: str | None) -> str | None:
    if not text:
        return text
    return redact_count(text)[0]


def redact_count(text: str | None) -> tuple[str | None, int]:
    """(redact(text), how many identifiers were masked): Ask reports the count of what it masked before a cloud call."""
    if not text:
        return text, 0
    n = 0

    def counted(fn):
        def sub(m):
            nonlocal n
            out = fn(m)
            if out != m.group(0):
                n += 1
            return out
        return sub

    t = _RX_MRZ.sub(counted(lambda m: MASK if "<<" in m.group(0) else m.group(0)), text)
    t = _RX_UAE_ID.sub(counted(lambda m: MASK), t)
    t = _RX_CNIC.sub(counted(lambda m: MASK), t)
    t = _RX_IBAN.sub(counted(_iban), t)
    for rx in _RX_LABELLED:
        t = rx.sub(counted(lambda m: m.group(1) + m.group(2) + MASK), t)
    return t, n


_RX_PATH_ID = re.compile(r"(?<![A-Za-z0-9])(?=[A-Za-z0-9-]*\d{6})[A-Za-z0-9-]{6,}(?![A-Za-z0-9])")


def redact_path(path: str) -> str:
    """For name-only (identity) files: mask long number-bearing tokens in the file name."""
    head, _, name = path.rpartition("/")
    return (head + "/" if head else "") + _RX_PATH_ID.sub("[id]", name)
