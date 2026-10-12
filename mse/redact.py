"""Mask personal identifiers in text that leaves for an agent (MCP, piped CLI) or for a cloud model (Ask).

The app shows everything. Agents and cloud models get these masked:
  * IBANs, with the country code and the last four characters kept;
  * national identification numbers of more than twenty countries (NATIONAL_IDS). Where a published check-digit rule
    exists, a number is masked only if it passes it. A written form that is not distinctive (a bare run of digits) is
    masked only next to its label. Where no check exists, the shape alone decides;
  * passport, ID-card, residence-permit and tax-ID numbers, social-security numbers and dates of birth, when a label
    names them (_LABELLED);
  * machine-readable zones of passports and ID cards;
  * any further format the user adds under config agents.extra_id_patterns (see _extra_rules).
Company identifiers (commercial register number, company tax number, VAT ID) stay.
"""

import json
import logging
import re
import threading
from collections import Counter
from datetime import date
from time import perf_counter

from . import paths

log = logging.getLogger(__name__)

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
_RX_MRZ = re.compile(r"(?<![A-Z0-9<])[A-Z0-9<]{30,46}(?![A-Z0-9<])")          # machine-readable zone of passports / ID cards


# ---------------------------------------------------------------------------------------------------- check rules
# Each validator takes the matched number as written (separators included) and returns True when it is a number the
# country's rule accepts. Digits are read with [0-9] only, never with \d, which also matches other scripts.

def _digits(s: str) -> str:
    return "".join(c for c in s if "0" <= c <= "9")


def _upper_compact(s: str) -> str:
    return re.sub(r"[\s.\-+/:]", "", s).upper()


def _date_ok(month: int, day: int, year: int = 2000) -> bool:
    """A real calendar date. The year defaults to a leap year, so 29 February is accepted for any year."""
    try:
        date(year, month, day)
        return True
    except ValueError:
        return False


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _mod_11_10_ok(digits: str) -> bool:
    """ISO 7064 MOD 11,10 over the whole number: a valid number ends at 1."""
    check = 5
    for ch in digits:
        check = (((check or 10) * 2) % 11 + int(ch)) % 10
    return check == 1


_VERHOEFF_MUL = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9), (1, 2, 3, 4, 0, 6, 7, 8, 9, 5), (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7), (4, 0, 1, 2, 3, 9, 5, 6, 7, 8), (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2), (7, 6, 5, 9, 8, 2, 1, 0, 4, 3), (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0))
_VERHOEFF_PERM = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9), (1, 5, 7, 6, 2, 8, 3, 0, 9, 4), (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7), (9, 4, 5, 3, 1, 2, 6, 8, 7, 0), (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5), (7, 0, 4, 6, 9, 1, 3, 2, 5, 8))


def _verhoeff_ok(digits: str) -> bool:
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _VERHOEFF_MUL[c][_VERHOEFF_PERM[i % 8][int(ch)]]
    return c == 0


def _icao_check_digit(text: str) -> int:
    """ICAO 9303 check digit: digits and letters A=10 .. Z=35, weights 7, 3, 1 repeating."""
    vals = [int(c) if "0" <= c <= "9" else ord(c) - 55 for c in text]
    return sum(v * (7, 3, 1)[i % 3] for i, v in enumerate(vals)) % 10


_AT_WEIGHTS = (3, 7, 9, 0, 5, 8, 4, 2, 1, 6)
_CF_ODD = (1, 0, 5, 7, 9, 13, 15, 17, 19, 21, 2, 4, 18, 20, 11, 3, 6, 8, 12, 14, 16, 10, 22, 25, 24, 23)
_DNI_LETTERS = "TRWAGMYFPDXBNJZSQVHLCKE"
_FI_CENTURY = {"+": 1800, "-": 1900, "A": 2000, "B": 2000, "C": 2000, "D": 2000, "E": 2000, "F": 2000,
               "U": 1900, "V": 1900, "W": 1900, "X": 1900, "Y": 1900}
_FI_CONTROL = "0123456789ABCDEFHJKLMNPRSTUVWXY"
_SSN_BLACKLIST = {"078051120", "457555462", "219099999"}


def _svnr_ok(s):
    """Austria: 3 serial digits, a check digit, then DDMMYY. Weighted sum mod 11 equals the check digit."""
    d = _digits(s)
    if len(d) != 10:
        return False
    v = [int(c) for c in d]
    if sum(w * x for w, x in zip(_AT_WEIGHTS, v)) % 11 != v[3]:
        return False
    return _date_ok(int(d[6:8]), int(d[4:6]))


def _rrn_ok(s):
    """Belgium: YYMMDD-SSS-CC, with CC = 97 - (first nine digits mod 97) for 1900 or 2000 births."""
    d = _digits(s)
    if len(d) != 11 or not 0 <= int(d[2:4]) <= 12:
        return False
    body, check = int(d[:9]), int(d[9:])
    return check in (97 - body % 97, 97 - (2000000000 + body) % 97)


def _cpf_ok(s):
    """Brazil: two check digits, mod 11 with weights 10..2 and 11..2; a run of one repeated digit is not valid."""
    d = _digits(s)
    if len(d) != 11 or len(set(d)) == 1:
        return False
    v = [int(c) for c in d]
    for pos in (9, 10):
        total = sum(v[i] * (pos + 1 - i) for i in range(pos))
        if (total * 10) % 11 % 10 != v[pos]:
            return False
    return True


def _sin_ok(s):
    """Canada: Luhn over nine digits; a number does not begin with 0 or 8."""
    d = _digits(s)
    return len(d) == 9 and d[0] not in "08" and _luhn(d)


def _ahv_ok(s):
    """Switzerland: 756 prefix, then an EAN-13 check digit."""
    d = _digits(s)
    if len(d) != 13 or not d.startswith("756"):
        return False
    total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(d[:12]))
    return (10 - total % 10) % 10 == int(d[12])


def _idnr_ok(s):
    """Germany tax ID: eleven digits, not starting with 0; exactly one digit repeated (twice or three times) among the
    first ten; ISO 7064 MOD 11,10 over the whole number."""
    d = _digits(s)
    if len(d) != 11 or d[0] == "0":
        return False
    repeated = [c for c in Counter(d[:10]).values() if c > 1]
    if len(repeated) != 1 or repeated[0] not in (2, 3):
        return False
    return _mod_11_10_ok(d)


def _ausweis_ok(s):
    """Germany ID card: the printed nine-character number carries no check digit (the check digit sits in the MRZ, which
    the MRZ rule masks). When a ten-character form is written with its check digit, that digit must match."""
    t = re.sub(r"\s", "", s).upper()
    if len(t) == 9:
        return True
    return len(t) == 10 and t[9] in "0123456789" and _icao_check_digit(t[:9]) == int(t[9])


def _cpr_ok(s):
    """Denmark: DDMMYY-SSSS. The modulus-11 check was abandoned in 2007, so only the embedded date is checked."""
    d = _digits(s)
    return len(d) == 10 and _date_ok(int(d[2:4]), int(d[0:2]))


def _dni_ok(s):
    t = _upper_compact(s)
    return len(t) == 9 and t[:8].isdigit() and t[8] == _DNI_LETTERS[int(t[:8]) % 23]


def _nie_ok(s):
    t = _upper_compact(s)
    if len(t) != 9 or t[0] not in "XYZ" or not t[1:8].isdigit():
        return False
    number = int({"X": "0", "Y": "1", "Z": "2"}[t[0]] + t[1:8])
    return t[8] == _DNI_LETTERS[number % 23]


def _hetu_ok(s):
    """Finland: DDMMYY, century sign, three individual digits, then a control character from a 31-character table."""
    t = s.upper()
    if len(t) != 11:
        return False
    day, month, year = int(t[0:2]), int(t[2:4]), int(t[4:6])
    if t[6] not in _FI_CENTURY or int(t[7:10]) < 2:
        return False
    if not _date_ok(month, day, _FI_CENTURY[t[6]] + year):
        return False
    return t[10] == _FI_CONTROL[int(t[0:6] + t[7:10]) % 31]


def _nir_ok(s):
    """France: 13 digits and a two-digit key, key = 97 - (number mod 97); Corsican departments 2A and 2B count as 19, 18."""
    t = re.sub(r"\s", "", s).upper()
    if len(t) != 15:
        return False
    base = t[:5] + {"2A": "19", "2B": "18"}.get(t[5:7], t[5:7]) + t[7:13]
    if not re.fullmatch(r"[0-9]{13}", base) or not t[13:].isdigit():
        return False
    return int(t[13:]) == 97 - int(base) % 97


def _nino_ok(s):
    """United Kingdom: letters with prefix exclusions, six digits, suffix A to D."""
    t = re.sub(r"\s", "", s).upper()
    if len(t) != 9:
        return False
    if t[0] in "DFIQUV" or t[1] in "DFIOQUV" or t[:2] in {"BG", "GB", "KN", "NK", "NT", "TN", "ZZ"}:
        return False
    return True


def _aadhaar_ok(s):
    """India: twelve digits, the first from 2 to 9, Verhoeff check."""
    d = _digits(s)
    return len(d) == 12 and d[0] in "23456789" and _verhoeff_ok(d)


def _pan_ok(s):
    """India PAN: the fourth letter gives the holder type."""
    t = re.sub(r"\s", "", s).upper()
    return len(t) == 10 and t[3] in "CPHFATBLJG"


def _cf_ok(s):
    """Italy: sixteen characters, check letter from the odd and even tables mod 26."""
    t = s.upper()
    if len(t) != 16:
        return False
    total = 0
    for i, ch in enumerate(t[:15]):
        n = int(ch) if ch.isdigit() else ord(ch) - 65
        total += _CF_ODD[n] if i % 2 == 0 else n
    return chr(65 + total % 26) == t[15]


def _bsn_ok(s):
    """Netherlands: nine digits, eleven-proof."""
    d = _digits(s)
    if len(d) != 9 or d == "000000000":
        return False
    return sum(int(c) * w for c, w in zip(d, (9, 8, 7, 6, 5, 4, 3, 2, -1))) % 11 == 0


def _fnr_ok(s):
    """Norway: two mod-11 check digits; a day or month above 40 marks a D-number or H-number (the date is then minus 40)."""
    d = _digits(s)
    if len(d) != 11:
        return False
    k1 = (11 - sum(int(c) * w for c, w in zip(d, (3, 7, 6, 1, 8, 9, 4, 5, 2)))) % 11
    k2 = (11 - sum(int(c) * w for c, w in zip(d, (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)))) % 11
    if k1 != int(d[9]) or k2 != int(d[10]):
        return False
    day, month = int(d[0:2]), int(d[2:4])
    if day > 40:
        day -= 40
    if month > 40:
        month -= 40
    return _date_ok(month, day)


def _pesel_ok(s):
    """Poland: eleven digits; weights 1,3,7,9 repeating; check digit as (10 - sum mod 10) mod 10; month offset by century."""
    d = _digits(s)
    if len(d) != 11:
        return False
    check = (10 - sum(int(c) * w for c, w in zip(d[:10], (1, 3, 7, 9, 1, 3, 7, 9, 1, 3)))) % 10
    return check == int(d[10]) and _date_ok(int(d[2:4]) % 20, int(d[4:6]))


def _personnummer_ok(s):
    """Sweden: YYMMDD-NNNN (or with a century), Luhn over the ten digits."""
    d = _digits(s)
    if len(d) == 12:
        d = d[2:]
    return len(d) == 10 and _luhn(d) and _date_ok(int(d[2:4]), int(d[4:6]))


def _tckn_ok(s):
    """Turkey: eleven digits; tenth digit from the odd and even sums, eleventh digit from the sum of the first ten."""
    d = _digits(s)
    if len(d) != 11 or d[0] == "0":
        return False
    v = [int(c) for c in d]
    tenth = ((v[0] + v[2] + v[4] + v[6] + v[8]) * 7 - (v[1] + v[3] + v[5] + v[7])) % 10
    return tenth == v[9] and sum(v[:10]) % 10 == v[10]


def _ssn_ok(s):
    """United States: area 000, 666 or 9xx, group 00, serial 0000 and the published sample numbers are never issued."""
    d = _digits(s)
    if len(d) != 9:
        return False
    area, group, serial = d[:3], d[3:5], d[5:]
    if area in ("000", "666") or area[0] == "9" or group == "00" or serial == "0000":
        return False
    return d not in _SSN_BLACKLIST


# ----------------------------------------------------------------------------------------------- the table
_LEFT = r"(?<![0-9A-Za-z])(?<!\d[ .\-/])"     # not a slice of a longer run of digit groups
_RIGHT = r"(?![0-9A-Za-z])(?![ .\-/]\d)"


def _row(country, name, shape, check=None, label=None):
    """(country, name, compiled pattern with a group "id" for the number, validator or None).

    A row with neither a check nor a label is a plain shape, compiled as written (AE and PK keep their original
    boundaries). A row with a check may fire on its shape alone. A written form that its check cannot tell apart from
    an ordinary number (a grouped or dashed run with no check, or a check that about one number in fifty passes) is
    given a label, and then it is masked only when the label comes first.
    """
    if check is None and label is None:
        return (country, name, re.compile(rf"(?P<id>{shape})"), None)
    if label is None:
        return (country, name, re.compile(rf"{_LEFT}(?P<id>{shape}){_RIGHT}"), check)
    return (country, name,
            re.compile(rf"(?<![0-9A-Za-z])(?P<label>{label})(?![0-9A-Za-z]){_GAP}(?P<id>{shape}){_RIGHT}", re.IGNORECASE),
            check)


# Sorted by country code. Shape-only rows (check None) run before IBAN, as the two original rules always did; every
# row with a check runs after the labelled rules, so a mask already written is never matched again.
NATIONAL_IDS = [
    _row("AE", "national ID, 784 series", r"\b784-?[0-9]{4}-?[0-9]{7}-?[0-9]\b"),
    _row("AT", "social security number, spaced", r"[0-9]{4} [0-9]{6}", _svnr_ok),
    _row("AT", "social security number, labelled", r"[0-9]{10}", _svnr_ok,
         r"sv-?nr|svnr|sv-?nummer|sozialversicherungs-?nummer|versicherungsnummer"),
    _row("BE", "national register number", r"[0-9]{2}\.[0-9]{2}\.[0-9]{2}-[0-9]{3}\.[0-9]{2}", _rrn_ok),
    _row("BE", "national register number, labelled", r"[0-9]{11}", _rrn_ok,
         r"rrn|rijksregisternummer|num[ée]ro de registre national|national (?:register|registry) number"),
    _row("BR", "CPF", r"[0-9]{3}\.[0-9]{3}\.[0-9]{3}-[0-9]{2}", _cpf_ok),
    _row("BR", "CPF, labelled", r"[0-9]{11}", _cpf_ok, r"cpf"),
    _row("CA", "social insurance number, labelled", r"[0-9]{3}[ -]?[0-9]{3}[ -]?[0-9]{3}", _sin_ok,
         r"sin|social insurance number|num[ée]ro d'assurance sociale"),
    _row("CH", "AHV number", r"756\.[0-9]{4}\.[0-9]{4}\.[0-9]{2}", _ahv_ok),
    _row("CH", "AHV number, labelled", r"756[0-9]{10}", _ahv_ok, r"ahvn13|ahvn|ahv|avs"),
    _row("DE", "tax ID (Steuer-ID)", r"[0-9]{2} ?[0-9]{3} ?[0-9]{3} ?[0-9]{3}", _idnr_ok),
    _row("DE", "ID card (Personalausweis), labelled", r"[A-Z0-9]{9,10}", _ausweis_ok,
         r"personalausweis|ausweis(?:nummer|-?nr)"),
    _row("DK", "CPR number, labelled", r"[0-9]{6}-?[0-9]{4}", _cpr_ok, r"cpr(?:-?nr|-?nummer)?"),
    _row("ES", "DNI, labelled", r"[0-9]{8}-?[A-Z]", _dni_ok, r"dni|nif|documento nacional de identidad"),
    _row("ES", "NIE", r"[XYZ]-?[0-9]{7}-?[A-Z]", _nie_ok),
    _row("FI", "henkilötunnus", r"[0-9]{6}[-+A-FUVWXY][0-9]{3}[0-9A-FHJ-NPR-Y]", _hetu_ok),
    _row("FR", "NIR", r"[12] ?[0-9]{2} ?[0-9]{2} ?(?:[0-9]{2}|2[AB]) ?[0-9]{3} ?[0-9]{3} ?[0-9]{2}", _nir_ok),
    _row("GB", "National Insurance number", r"[A-Z]{2} ?[0-9]{2} ?[0-9]{2} ?[0-9]{2} ?[A-D]", _nino_ok),
    _row("IN", "Aadhaar, labelled", r"[2-9][0-9]{3} ?[0-9]{4} ?[0-9]{4}", _aadhaar_ok, r"aadhaar|aadhar|uidai?"),
    _row("IN", "PAN", r"[A-Z]{3}[CPHFATBLJG][A-Z][0-9]{4}[A-Z]", _pan_ok),
    _row("IT", "codice fiscale", r"[A-Z]{6}[0-9LMNPQRSTUV]{2}[ABCDEHLMPRST][0-9LMNPQRSTUV]{2}[A-Z]"
                                 r"[0-9LMNPQRSTUV]{3}[A-Z]", _cf_ok),
    _row("NL", "BSN, labelled", r"[0-9]{9}", _bsn_ok, r"bsn|burgerservicenummer|sofi-?nummer|sofi-?nr|sofinummer"),
    _row("NO", "fødselsnummer, grouped", r"[0-9]{6} [0-9]{5}", _fnr_ok),
    _row("NO", "fødselsnummer, labelled", r"[0-9]{11}", _fnr_ok,
         r"f(?:ø|oe|o)dselsnummer|f(?:ø|oe|o)dselsnr|fnr"),
    _row("PK", "CNIC", r"\b[0-9]{5}-[0-9]{7}-[0-9]\b"),
    _row("PL", "PESEL, labelled", r"[0-9]{11}", _pesel_ok, r"pesel"),
    _row("SE", "personnummer", r"(?:[0-9]{2})?[0-9]{6}[-+][0-9]{4}", _personnummer_ok),
    _row("SE", "personnummer, labelled", r"[0-9]{10}|[0-9]{12}", _personnummer_ok,
         r"personnummer|personal identity number|personnr"),
    _row("TR", "TC kimlik number, labelled", r"[0-9]{11}", _tckn_ok,
         r"t\.?\s?c\.?\s?kimlik(?:\s?no)?|kimlik\s?no|tckn"),
    _row("US", "social security number, labelled", r"[0-9]{3}-?[0-9]{2}-?[0-9]{4}", _ssn_ok,
         r"ssn|social security(?:\s+number|\s+no\.?)?"),
]

_EARLY_ID_RULES = [row for row in NATIONAL_IDS if row[3] is None]    # shape only: before IBAN, as before
_LATE_ID_RULES = [row for row in NATIONAL_IDS if row[3] is not None]  # checked: after the labelled rules


# ------------------------------------------------------------------- user-defined formats (config)
_EXTRA_MAX_RULES = 32
_EXTRA_MAX_LENGTH = 200
_EXTRA_NESTED = re.compile(r"\([^()]*[+*][^()]*\)[+*{]")        # a repeated group that repeats something inside it
_EXTRA_BACKREF = re.compile(r"\\[1-9]|\(\?P=")
_EXTRA_PROBES = ("", "0", "a", "1 2-3 4 5", "x" * 20, "1" * 20 + "x", "AB12 CD34-56 ef78")
_EXTRA_PROBE_SECONDS = 0.05

_extra_lock = threading.Lock()
_extra_state = {"key": None, "rules": ()}


def _compile_extra(entry):
    """(compiled pattern, None) when accepted, else (None, reason). A refused pattern is skipped, never fatal."""
    if not isinstance(entry, dict) or not isinstance(entry.get("regex"), str):
        return None, "not an object with a text field regex"
    regex = entry["regex"]
    if not regex or len(regex) > _EXTRA_MAX_LENGTH:
        return None, "empty, or longer than the limit"
    if _EXTRA_NESTED.search(regex) or _EXTRA_BACKREF.search(regex):
        return None, "nested repetition or a back-reference"
    try:
        rx = re.compile(regex)
    except (re.error, RecursionError, OverflowError, ValueError) as exc:
        return None, f"does not compile ({type(exc).__name__})"
    started = perf_counter()
    for probe in _EXTRA_PROBES:
        for m in rx.finditer(probe):
            if m.end() == m.start():
                return None, "can match the empty string"
    for probe in _EXTRA_PROBES:
        rx.search(probe)
    if perf_counter() - started > _EXTRA_PROBE_SECONDS:
        return None, "too slow on a short probe"
    return rx, None


def _config_key():
    """The config file's identity: config is written by replacing the file, so a new inode marks every change."""
    try:
        st = paths.CONFIG.stat()
    except OSError:
        return ("absent", str(paths.CONFIG))
    return (str(paths.CONFIG), st.st_ino, st.st_mtime_ns, st.st_size)


def _load_extra_rules():
    """config agents.extra_id_patterns, read as the file is (no config.load: that writes the file on first use).
    Anything unreadable gives no extra patterns; the built-in rules always run."""
    try:
        cfg = json.loads(paths.CONFIG.read_text(encoding="utf-8"))
        entries = cfg.get("agents", {}).get("extra_id_patterns", []) if isinstance(cfg, dict) else []
        if not isinstance(entries, list):
            raise TypeError("agents.extra_id_patterns is not a list")
    except FileNotFoundError:
        return ()
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        log.warning("agents.extra_id_patterns ignored: %s", exc)
        return ()
    rules = []
    for index, entry in enumerate(entries[:_EXTRA_MAX_RULES]):
        rx, why = _compile_extra(entry)
        if rx is None:
            log.warning("agents.extra_id_patterns: entry %d skipped (%s)", index + 1, why)
        else:
            rules.append(rx)
    if len(entries) > _EXTRA_MAX_RULES:
        log.warning("agents.extra_id_patterns: only the first %d entries are used", _EXTRA_MAX_RULES)
    return tuple(rules)


def _extra_rules():
    """The compiled user patterns, reloaded only when the config file changes (path, mtime and size)."""
    key = _config_key()
    if _extra_state["key"] != key:
        with _extra_lock:
            if _extra_state["key"] != key:
                _extra_state["rules"] = _load_extra_rules()
                _extra_state["key"] = key
    return _extra_state["rules"]


# --------------------------------------------------------------------------------------- masking
def _iban(m) -> str:
    compact = re.sub(r"\s", "", m.group(0))
    return f"{compact[:2]}•••• {compact[-4:]}" if len(compact) >= 15 else m.group(0)


def _national(row):
    """Replacement for one row: the number is masked when the row's check accepts it (or it has none)."""
    check = row[3]

    def sub(m):
        if check is not None and not check(m.group("id")):
            return m.group(0)
        whole = m.group(0)
        start, end = m.start("id") - m.start(0), m.end("id") - m.start(0)
        return whole[:start] + MASK + whole[end:]
    return sub


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
    for row in _EARLY_ID_RULES:
        t = row[2].sub(counted(_national(row)), t)
    t = _RX_IBAN.sub(counted(_iban), t)
    for rx in _RX_LABELLED:
        t = rx.sub(counted(lambda m: m.group(1) + m.group(2) + MASK), t)
    for row in _LATE_ID_RULES:
        t = row[2].sub(counted(_national(row)), t)
    for rx in _extra_rules():
        t = rx.sub(counted(lambda m: MASK), t)
    return t, n


_RX_PATH_ID = re.compile(r"(?<![A-Za-z0-9])(?=[A-Za-z0-9-]*\d{6})[A-Za-z0-9-]{6,}(?![A-Za-z0-9])")


def redact_path(path: str) -> str:
    """For name-only (identity) files: mask long number-bearing tokens in the file name."""
    head, _, name = path.rpartition("/")
    return (head + "/" if head else "") + _RX_PATH_ID.sub("[id]", name)
