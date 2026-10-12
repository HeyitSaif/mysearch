"""Document understanding, all on this Mac: what kind of document a file is, and its date, amount, sender and number.

classify()        the document type (invoice, receipt, bank_statement, contract, letter, quote, reminder, payslip,
                  tax_document, other) from weighted English and German keyword rules, the title lines, the file and
                  folder name and the layout (a totals line, a signature block, a run of dated money lines)
extract_fields()  doc_date, amount + currency, counterparty and reference, with German (1.234,56) and English
                  (1,234.56) numbers, labelled totals and dates first
analyse()         both, as one row of the doc_meta table
SQL helpers       upsert / delete / backfill / reindex, and the facets and timeline behind /api/docs/*

Pure Python, no model, no network. The rules are conservative: an unclear document is `other`, an unclear field is None.
Accuracy over recall; wrong data is worse than none.

Privacy. No field ever holds an IBAN, a card number, a personal or tax ID, a passport or permit number or a date of
birth: labelled birth dates and validity dates are skipped before a date is chosen, a reference is checked against the
patterns of mse/redact.py and against its own context, identity documents get no fields at all, and payslips and tax
documents get no reference. Data derived from a file lives only in that collection's own database and is removed with
the file, or when the file turns metadata-only.

Known limits (see docs/TECHNICAL.md):
- The counterparty is "who is printed in the letterhead / the From name". For an outgoing invoice that is you. Pass
  `own_names` (config docmeta.own_names) to skip your own company names.
- The indexer stores text in chunks of about 1,100 characters; long paragraphs lose their line breaks. Every rule here
  works on label + value within a short distance, not on whole lines.
- Embedding similarity is left as a hook: classify(prior=...) adds an optional per-type bonus (PROTOTYPES lists a
  description per type to embed).
"""

import html
import math
import re
import threading
import time
import unicodedata
from datetime import date, timedelta
from email.utils import parseaddr, parsedate_to_datetime

from .textutil import detect_lang

EXTRACTOR_VERSION = 1            # bump when the rules change: stored rows with an older version are redone by the backfill
MAX_CHARS = 20_000               # text used for the fields
CLASSIFY_CHARS = 8_000           # text used for the type
HEAD_CHARS = 1_500               # "the top of the document": keywords there weigh more
MIN_CONFIDENCE = 0.5             # below this the type is `other`
MIN_CONFIDENCE_PLAIN = 0.5       # ... for plain text and markdown notes, which mention invoices without being one

DOCTYPES = ("invoice", "receipt", "bank_statement", "contract", "letter", "quote", "reminder", "payslip",
            "tax_document", "other")
DOCTYPE_ALIASES = {
    "invoice": ("invoices", "rechnung", "rechnungen", "faktura"),
    "receipt": ("receipts", "quittung", "quittungen", "kassenbon", "kassenbons", "kassenbeleg"),
    "bank_statement": ("bank-statement", "bankstatement", "statement", "statements", "bank_statements", "kontoauszug",
                       "kontoauszuege", "kontoauszüge", "kontoauszuge"),
    "contract": ("contracts", "vertrag", "vertraege", "verträge", "vertrage", "agreement", "agreements"),
    "letter": ("letters", "brief", "briefe"),
    "quote": ("quotes", "quotation", "quotations", "angebot", "angebote", "offer", "offers"),
    "reminder": ("reminders", "mahnung", "mahnungen", "dunning"),
    "payslip": ("payslips", "lohnabrechnung", "lohnabrechnungen", "gehaltsabrechnung", "gehaltsabrechnungen"),
    "tax_document": ("tax-document", "tax_documents", "tax", "steuer", "steuerdokument", "steuerbescheid"),
}
_ALIAS_TO_TYPE = {a: t for t, al in DOCTYPE_ALIASES.items() for a in al}
_ALIAS_TO_TYPE.update({t: t for t in DOCTYPES if t != "other"})

# a description per type, for callers that want to add embedding similarity as `prior` (see classify)
PROTOTYPES = {
    "invoice": "An invoice: invoice number, invoice date, line items, net amount, VAT, total amount due, payment terms.",
    "receipt": "A receipt: a shop or card payment confirmation with items, total paid, payment method and date.",
    "bank_statement": "A bank statement: account number, opening and closing balance, a list of dated transactions.",
    "contract": "A contract: parties, clauses or sections, term and termination, governing law, signatures.",
    "letter": "A formal letter: salutation, subject line, body text, closing and signature.",
    "quote": "A quote or offer: priced items or services, total, valid until a date, acceptance terms.",
    "reminder": "A payment reminder or dunning letter: an overdue invoice, reminder fee, deadline to pay.",
    "payslip": "A payslip: gross pay, net pay, income tax, social security contributions, pay period.",
    "tax_document": "A tax document: a tax assessment, tax return or VAT return issued by or filed with a tax office.",
}

# ------------------------------------------------------------------ text normalisation

def _build_fold() -> dict:
    table = {}
    for cp in range(0xC0, 0x180):
        ch = chr(cp)
        base = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
        if len(base) == 1:
            table[cp] = base
    table[0xDF] = "s"                                        # ß -> s: keeps every position (the rules are written that way)
    for cp, rep in ((0x2018, "'"), (0x2019, "'"), (0x201C, '"'), (0x201D, '"'), (0x2013, "-"), (0x2014, "-"),
                    (0x2212, "-"), (0xA0, " "), (0x202F, " "), (0x2009, " "), (0x2007, " ")):
        table[cp] = rep
    return table


_FOLD = _build_fold()


def _fold(s: str) -> str:
    """Lower case, diacritics stripped, one output character per input character (so positions map back)."""
    return s.translate(_FOLD).lower()


_TL = re.compile(r"ae|oe|ue")


def _tl(s: str) -> str:
    """Collapse the German transliterations (ae, oe, ue) to the bare vowel: "Rueckgeld" and "Rückgeld" (folded "ruckgeld")
    meet. Used for classification only, on the text AND on the rule patterns, so a literal like "due date" still matches
    ("du date" on both sides). Not position-preserving."""
    return _TL.sub(lambda m: m.group(0)[0], s)


def _c(pattern: str, flags: int = 0) -> re.Pattern:
    return re.compile(_tl(pattern), flags)


_RX_HTML_HINT = re.compile(r"</?(?:p|br|div|span|b|i|u|a|table|tr|td|th|ul|ol|li|html|body|strong|em|h[1-6])\b[^>]{0,200}>", re.I)


def _strip_html(t: str) -> str:
    """A mail body or a copied web page may still carry tags and entities (<br>, &nbsp;): lines and cells are kept."""
    if not _RX_HTML_HINT.search(t):
        return t
    t = re.sub(r"(?i)<\s*(?:br|/p|/div|/li|/tr|/h[1-6]|/table)\b[^>]*>", "\n", t)
    t = re.sub(r"(?i)</t[dh]\s*>\s*<t[dh]\b[^>]*>", "  ", t)
    return html.unescape(re.sub(r"<[^>\n]{1,200}>", "", t))


def _prep(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\t", "  ")
    return _strip_html(text[:MAX_CHARS * 2])[:MAX_CHARS]


_OCR_EXT = {"jpg", "jpeg", "png", "heic", "heif", "tif", "tiff", "webp", "gif", "bmp"}
_OCR_DIGIT = re.compile(r"(?<=[a-z])[015](?=[a-z])")


def _ocr_fix(low: str) -> str:
    """Text read from a picture: a digit inside a word is a letter ("cl0sing", "b1s", "5umme"), and a q that is not
    followed by u is a g ("rechnunq"). Same length, so positions stay valid."""
    def digit(m):
        c = m.group(0)
        if c == "1":
            return "i" if low[m.end():m.end() + 1] in ("s", "t", "n", "g", "e") else "l"
        return "o" if c == "0" else "s"
    return re.sub(r"q(?!u)", "g", _OCR_DIGIT.sub(digit, low))


# ------------------------------------------------------------------ classification rules (folded alphabet)

def _rx(*parts: str) -> re.Pattern:
    return re.compile(_tl("|".join(parts)))


def _trx(*parts: str) -> re.Pattern:
    """A title pattern: whole words only ("standard" is not "nda"), a plural tail allowed ("Rechnungen")."""
    return re.compile(_tl(r"(?<![a-z-])(?:" + "|".join(parts) + r")(?:e|en|n|s)?(?![a-z])"))


_TITLE = {
    "invoice": _trx(r"rechnung", r"(?:jahres|monats|quartals|schluss|teil|abschlags|sammel|strom|gas|wasser|handy|telefon|mobilfunk|"
                   r"internet|energie|nebenkosten)rechnung", r"invoice", r"tax invoice", r"faktura", r"gutschrift",
                   r"credit note", r"credit memo", r"abschlagsrechnung", r"schlussrechnung", r"teilrechnung",
                   r"sammelrechnung"),
    "receipt": _trx(r"kassenbon", r"kassenbeleg", r"kassenzettel", r"quittung", r"receipt", r"zahlungsbeleg", r"hotel receipt",
                   r"zahlungsbestatigung", r"payment confirmation", r"payment receipt", r"sales receipt", r"kaufbeleg",
                   r"barzahlungsbeleg", r"eigenbeleg", r"tankbeleg", r"bon"),
    "bank_statement": _trx(r"kontoauszug", r"account statement", r"bank statement", r"statement of account",
                          r"kreditkartenabrechnung", r"credit card statement", r"kontoumsatze", r"umsatzubersicht",
                          r"account activity", r"transaction history", r"transaktionsubersicht", r"giro-?konto",
                          r"tagesauszug", r"monthly statement", r"umsatzliste"),
    "contract": _trx(r"[a-z]{0,25}vertrag", r"(?:\w+ )?contract", r"(?:\w+ )?agreement", r"vereinbarung", r"dienstleistungsvertrag",
                    r"werkvertrag", r"arbeitsvertrag", r"mietvertrag", r"kaufvertrag", r"nda", r"non-?disclosure",
                    r"geheimhaltungsvereinbarung", r"rahmenvertrag", r"lease(?: agreement)?", r"licen[cs]e agreement",
                    r"terms of service", r"allgemeine geschaftsbedingungen", r"verschwiegenheitserklarung"),
    "quote": _trx(r"angebot", r"quotation", r"quote", r"offer", r"kostenvoranschlag", r"kostenschatzung", r"estimate",
                 r"proposal", r"offerte", r"preisangebot", r"pro[- ]?forma(?:[- ]?rechnung|[- ]?invoice)?"),
    "reminder": _trx(r"mahnung", r"zahlungserinnerung", r"payment reminder", r"reminder", r"final notice",
                    r"letzte mahnung", r"erinnerung", r"dunning", r"zahlungsaufforderung", r"[123]\. ?mahnung",
                    r"(?:first|second|third|1st|2nd|3rd) reminder", r"overdue(?: notice)?", r"past due",
                    r"inkasso", r"letzte zahlungsaufforderung"),
    "payslip": _trx(r"verdienstbescheinigung", r"entgeltbescheinigung", r"lohnabrechnung", r"gehaltsabrechnung", r"entgeltabrechnung", r"verdienstabrechnung", r"payslip",
                   r"pay slip", r"salary slip", r"payroll statement", r"wage statement", r"lohnzettel",
                   r"gehaltszettel", r"earnings statement", r"bezugsabrechnung", r"abrechnung (?:fur|von) "),
    "tax_document": _trx(r"[a-z]{0,25}steuerbescheid", r"einkommensteuer(?:erklarung|berechnung)?", r"umsatzsteuer(?:erklarung)?",
                        r"korperschaftsteuer", r"steuerrechnung", r"steuerveranlagung", r"steuerabrechnung", r"grundsteuer", r"gewerbesteuer", r"property tax", r"council tax",
                        r"tax calculation", r"tax statement",
                        r"steuererklarung", r"einkommensteuererklarung", r"umsatzsteuer-?voranmeldung",
                        r"umsatzsteuererklarung", r"tax assessment", r"notice of assessment", r"tax return",
                        r"vat return", r"tax notice", r"self.?assessment", r"tax certificate", r"steuerbescheinigung",
                        r"lohnsteuerbescheinigung", r"korperschaftsteuer", r"tax computation", r"steuerfestsetzung",
                        r"\bbescheid (?:uber|fur)"),
}

_STRONG = {
    "invoice": _rx(r"rechnungsnummer", r"rechnungsdatum", r"rechnungs-?nr", r"rechnung nr", r"invoice (?:no|number|date|#)",
                   r"inv(?:oice)?\.? ?(?:no|nr)", r"zahlungsziel", r"zahlbar (?:innerhalb|bis|sofort)", r"payment terms",
                   r"zahlungsbedingungen", r"leistungsdatum", r"leistungszeitraum", r"ust-?id\w*", r"vat (?:id|no|number|reg\w*)",
                   r"bill to", r"invoice to", r"rechnungsempfanger", r"rechnungsbetrag", r"bitte uberweisen sie",
                   r"please (?:transfer|remit|pay)", r"remittance", r"amount due", r"balance due", r"total due",
                   r"nettobetrag", r"bruttobetrag", r"net amount", r"zzgl\.? ?(?:mwst|ust)", r"due date", r"fallig am",
                   r"faelligkeit", r"falligkeit", r"vielen dank fur ihren auftrag", r"thank you for your (?:business|order)",
                   r"credit note", r"gutschrift"),
    "receipt": _rx(r"ruckgeld", r"wechselgeld", r"change due", r"besten dank", r"danke und bis bald", r"belegnummer", r"kassenbeleg",
                   r"bezahlt mit", r"paid by card", r"status\s*:\s*bezahlt", r"zahlung wurde erfolgreich", r"payment (?:was )?successful",
                   r"kasse \d", r"filiale", r"\bbon\b", r"bar gezahlt", r"bar bezahlt", r"barzahlung", r"vielen dank fur ihren einkauf",
                   r"thank you for (?:shopping|your purchase|your visit)", r"cashier", r"kassierer\w*", r"bon-?nr",
                   r"beleg-?nr", r"terminal-?id", r"\btse\b", r"kartenzahlung", r"card payment", r"girocard",
                   r"ec-?karte", r"kontaktlos", r"contactless", r"mastercard", r"\bvisa\b", r"auth(?:orization)? code",
                   r"payment received", r"zahlung erhalten", r"paid in full", r"transaction id", r"zahlart",
                   r"gegeben", r"\bwechselgeld\b", r"mwst-?satz", r"eingenommen", r"amount tendered", r"\bcash\b"),
    "bank_statement": _rx(r"buchungstag", r"wertstellung", r"valuta", r"value date", r"booking date", r"buchungstext",
                          r"verwendungszweck", r"anfangssaldo", r"endsaldo", r"opening balance", r"closing balance",
                          r"neuer kontostand", r"alter kontostand", r"kontostand", r"balance brought forward",
                          r"balance carried", r"dauerauftrag", r"standing order", r"lastschrift", r"direct debit",
                          r"\bsepa\b", r"umsatze", r"statement date", r"zahlungsein-?gang", r"kontonummer", r"account number",
                          r"sort code", r"\bbic\b", r"\bswift\b", r"haben", r"\bsoll\b", r"debit", r"credit"),
    "contract": _rx(r"licen[cs]e", r"licensor", r"licensee", r"vermieter", r"mieter", r"landlord", r"tenant", r"warranty", r"gewahrleistung",
                    r"zwischen", r"between", r"nachfolgend", r"hereinafter", r"im folgenden", r"vertragsparteien",
                    r"the parties", r"die parteien", r"\bpartei\b", r"\bparty\b", r"laufzeit", r"term and termination",
                    r"kundigung\w*", r"termination", r"haftung", r"liability", r"governing law", r"anwendbares recht",
                    r"gerichtsstand", r"jurisdiction", r"confidential\w*", r"vertraulich\w*", r"salvatorisch\w*",
                    r"severability", r"entire agreement", r"in witness whereof", r"unterzeichn\w*", r"unterschrift",
                    r"vertragsbeginn", r"effective date", r"inkrafttreten", r"obligations?", r"pflichten",
                    r"verpflichtet sich", r"agrees? to", r"\bclause\b", r"klausel", r"anlage \d", r"annex"),
    "letter": _rx(r"sehr geehrte\w*(?: damen| herr\w*| frau)?", r"dear (?:sir|madam|mr|ms|mrs|dr|prof)\b", r"dear [a-z]+",
                  r"mit freundlichen gruse\w*", r"freundliche gruse", r"beste gruse", r"viele gruse", r"hochachtungsvoll",
                  r"best regards", r"kind regards", r"yours (?:sincerely|faithfully)", r"sincerely", r"warm regards",
                  r"betreff\s*:", r"\bsubject\s*:", r"ihr schreiben", r"your letter", r"bezugnehmend", r"with reference to",
                  r"hiermit", r"ich schreibe", r"wir schreiben", r"i am writing", r"we are writing", r"anbei erhalten sie",
                  r"to whom it may concern", r"unser zeichen", r"ihr zeichen", r"our ref(?:erence)?\b", r"your ref(?:erence)?\b",
                  r"we refer to", r"wir beziehen uns", r"thank you for your", r"vielen dank fur ihr", r"bitte bestatigen sie",
                  r"please (?:confirm|find|let us know)", r"i look forward", r"wir freuen uns auf", r"freundliche gru"),
    "quote": _rx(r"gultig bis", r"valid until", r"valid for \d+", r"valid through", r"gultigkeit", r"bindefrist",
                 r"angebotsnummer", r"angebots-?nr", r"quot(?:e|ation) (?:no|number|#)", r"offer (?:no|number)",
                 r"freibleibend", r"unverbindlich", r"bei auftragserteilung", r"we would be pleased to",
                 r"hiermit bieten wir", r"wir bieten ihnen", r"we are pleased to (?:offer|quote)", r"zur annahme",
                 r"to accept (?:this|the) (?:quote|offer|quotation)", r"kostenvoranschlag", r"auftragserteilung",
                 r"annahme des angebots", r"acceptance", r"angebotssumme", r"quote total", r"our offer", r"unser angebot"),
    "reminder": _rx(r"mahngebuhr\w*", r"mahnkosten", r"verzugszinsen", r"zahlungsverzug", r"in verzug", r"noch offen",
                    r"offen(?:e|er|en)? (?:betrag|posten|rechnung\w*|forderung\w*)", r"nicht (?:beglichen|bezahlt|eingegangen)",
                    r"noch nicht (?:beglichen|eingegangen|bezahlt)", r"bitte begleichen", r"zahlung ausstehend",
                    r"outstanding (?:amount|balance|invoice)", r"overdue", r"past due", r"late fee", r"late payment",
                    r"reminder fee", r"we have not (?:yet )?received", r"has not (?:yet )?been (?:paid|settled)",
                    r"\bunpaid\b", r"please settle", r"pay immediately", r"nachfrist", r"letzte frist",
                    r"rechtliche schritte", r"legal action", r"inkasso", r"collection agency", r"gesamtforderung",
                    r"total claim", r"fallig seit", r"uberfallig", r"dunning", r"payment is due", r"zahlungserinnerung",
                    r"zahlungsaufforderung", r"trotz unserer", r"despite our"),
    "payslip": _rx(r"bruttoverdienst", r"nettoverdienst", r"sozialabgaben", r"verdienstbescheinigung", r"bruttolohn", r"nettolohn", r"gross (?:pay|salary|wages)", r"net (?:pay|salary|wages)", r"lohnsteuer",
                   r"kirchensteuer", r"solidaritatszuschlag", r"sozialversicherung\w*", r"rentenversicherung",
                   r"krankenversicherung", r"arbeitslosenversicherung", r"pflegeversicherung", r"steuerklasse",
                   r"personalnummer", r"employee (?:id|no|number)", r"sv-?nummer", r"income tax", r"social security",
                   r"national insurance", r"pension", r"deductions", r"abzuge", r"auszahlungsbetrag", r"nettoauszahlung",
                   r"tax class", r"\bytd\b", r"year to date", r"kumuliert", r"gesamtbrutto", r"steuerpflichtig\w*",
                   r"eintrittsdatum", r"gehalt", r"\bbrutto\b", r"\bnetto\b", r"abrechnungsmonat", r"pay period",
                   r"basic salary", r"grundgehalt", r"stundenlohn", r"hourly rate"),
    "tax_document": _rx(r"grundsteuer", r"gewerbesteuer", r"property tax", r"council tax", r"tax calculation", r"balancing payment",
                        r"tax already deducted", r"income tax due", r"steuernummer", r"steuer-?id\w*", r"identifikationsnummer", r"veranlagung\w*",
                        r"festgesetzt\w*", r"bemessungsgrundlage", r"zu versteuernde\w*", r"taxable income", r"tax due",
                        r"tax payable", r"abschlusszahlung", r"steuerschuld", r"rechtsbehelf\w*",
                        r"einspruch", r"objection", r"\bbescheid\b", r"assessment", r"steuerjahr", r"tax year",
                        r"tax period", r"umsatzsteuer-?id\w*", r"vorauszahlung\w*", r"erstattung", r"nachzahlung",
                        r"steuerlich\w*", r"umsatzsteuer-?voranmeldung", r"zu erstattend\w*",
                        r"steuerabzug", r"tax authority", r"steuerbehorde", r"tax office", r"steueramt"),
}

_NAME = {      # (strong tokens, weak tokens) of the file name
    "invoice": ({"invoice", "rechnung", "rechnungen", "faktura", "rechn"}, {"re", "bill", "inv", "rg"}),
    "receipt": ({"receipt", "quittung", "kassenbon", "kassenzettel", "kassenbeleg", "bon", "eigenbeleg"}, {"beleg", "belege"}),
    "bank_statement": ({"kontoauszug", "kontoauszuge", "bankstatement", "umsatze", "umsatzliste"}, {"statement", "auszug", "konto"}),
    "contract": ({"vertrag", "contract", "agreement", "nda", "vereinbarung", "mietvertrag", "arbeitsvertrag", "lease"}, {"vertrage"}),
    "quote": ({"angebot", "quote", "quotation", "offerte", "kostenvoranschlag", "proposal", "estimate"}, {"offer"}),
    "reminder": ({"mahnung", "reminder", "zahlungserinnerung", "dunning", "mahnungen"}, {"erinnerung"}),
    "payslip": ({"lohnabrechnung", "gehaltsabrechnung", "payslip", "entgeltabrechnung", "lohnzettel", "gehaltszettel"}, {"gehalt", "lohn", "salary"}),
    "tax_document": ({"steuererklarung", "einkommensteuer", "umsatzsteuer"}, {"steuer", "tax", "vat", "ust"}),
    "letter": ({"brief", "letter", "schreiben"}, set()),
}
_NAME_NUMBERED = {"invoice": re.compile(r"(?:^|[^a-z])(?:re|inv|rg)[-_ ]?\d{2,}")}

_IDENTITY_WORDS = _c(
    r"passport|reisepass|personalausweis|identity card|id card|aufenthaltstitel|residence permit|residence card|"
    r"nationality|staatsangehorigkeit|place of birth|geburtsort|driving licen[cs]e|fuhrerschein|national id|"
    r"national identity|id number|surname|given names?|vorname\(n\)|ausweis")
_MRZ = re.compile(r"[A-Z0-9<]{25,}<<[A-Z0-9<]*")

_RX_SALUTATION = _c(r"sehr geehrte|dear (?:sir|madam|mr|ms|mrs|dr|prof)\b|dear [a-z]+|hallo [a-z]+|liebe[r]? [a-z]+|to whom it may concern|guten tag|hello [a-z]+")
_RX_CLOSING = _c(r"mit freundlichen gru|freundliche gru|beste gru|viele gru|hochachtungsvoll|best regards|"
                         r"kind regards|yours (?:sincerely|faithfully)|sincerely|warm regards|mfg|with kind regards|mit besten gru|regards,")
_RX_SUBJECT_LINE = _c(r"^\s*(?:betreff|subject|re|ihr zeichen|your ref(?:erence)?)\s*[:.]")
_RX_TOTAL_LINE = _c(r"(?:total|gesamt|summe|amount due|zu zahlen|betrag|endbetrag|grand total)\W+[^\n]{0,40}?\d")
_RX_IBAN_LINE = re.compile(r"\b[a-z]{2}\d{2}(?: ?[a-z0-9]{4}){3,7}(?: ?[a-z0-9]{1,4})?\b")
_RX_SIGNATURE = _c(r"_{6,}|-{8,}|(?:unterschrift|signature|signed|unterzeichnet|gezeichnet)\s*[:.]|"
                           r"(?:ort|place)[,/ ]+(?:datum|date)")
_RX_ITEM_HEADER = _c(r"\b(?:pos|position|description|beschreibung|bezeichnung|menge|qty|quantity|unit price|"
                             r"einzelpreis|e-preis|preis|price|amount|betrag|artikel|item|mwst|vat)\b")
_RX_DATE_START = re.compile(r"^\s*(?:\d{1,2}[./-]\d{1,2}[./-](?:\d{4}|\d{2})|\d{4}-\d{2}-\d{2})\b")
_CSV_BANK_HEADERS = _c(r"buchungstag|valuta|wertstellung|verwendungszweck|booking date|value date|buchungstext|"
                               r"beguenstigter|begunstigter|auftraggeber|payee|beneficiary|counterparty|saldo|balance|"
                               r"umsatz|iban")
_SPREADSHEET_EXT = {"xlsx", "xlsm", "xls", "ods", "numbers"}
_PLAIN_EXT = {"txt", "text", "md", "markdown", "rst", "html", "htm"}

# classification weights
W_TITLE, W_SUBJECT, W_NAME, W_NAME_WEAK, W_FOLDER = 6.0, 2.5, 3.5, 1.5, 0.8
W_TITLE_PLAIN = 0.85          # a heading in a text or markdown file counts less
W_STRONG_HEAD, W_STRONG_BODY, W_STRONG_CAP = 1.5, 0.7, 7.0
CONF_SCALE = 6.0


def _title_lines(llines: list[str], limit: int = 14) -> list[str]:
    """The first lines that look like a heading: short, or starting with the document's name ("Rechnung Nr. 4711")."""
    out = []
    for l in llines:
        s = l.strip(" \t#*>-_=|:.")
        if not s:
            continue
        out.append(s)
        if len(out) >= limit:
            break
    return out


def classify(text: str, filename: str = "", ext: str = "", meta: dict | None = None,
             prior: dict | None = None, extra_keywords: dict | None = None) -> tuple[str, float]:
    """(doctype, confidence 0..1). Low confidence -> ("other", ...). `prior` is an optional {doctype: 0..1} bonus
    (embedding similarity to PROTOTYPES, computed by the caller when vectors are at hand); it never decides alone.
    `extra_keywords` ({doctype: [phrase, ...]}, config docmeta.extra_keywords) are counted like the built-in keywords."""
    doctype, conf, _ = classify_detail(text, filename, ext, meta, prior, extra_keywords)
    return doctype, conf


_extra_cache: dict = {}


def _extra_rx(extra_keywords: dict | None) -> dict:
    """{doctype: compiled alternation of the phrases}, cached by content. Phrases are literal (case and accents ignored)."""
    if not extra_keywords:
        return {}
    key = tuple(sorted((t, tuple(v)) for t, v in extra_keywords.items() if t in _STRONG and v))
    got = _extra_cache.get(key)
    if got is None:
        got = _extra_cache[key] = {t: re.compile("|".join(re.escape(_tl(_fold(k))) for k in v if k.strip())) for t, v in key}
    return got


_RX_PAID = _c(r"bezahlt mit|bar bezahlt|paid by|paid with|kartenzahlung|card payment|girocard|ec-karte|trinkgeld|\btip\b|gratuity|"
              r"vielen dank fur ihren besuch|thank you for your visit")
_RX_DUE = _c(r"zahlungsziel|zahlbar|payment terms|due date|fallig|\biban\b|bank details|bankverbindung|within \d+ days|innert|skonto|"
             r"amount due|balance due|total due|invoice (?:number|date)|rechnungsdatum|rechnungsnummer|rechnungs-nr")
_RX_DAYMON_START = re.compile(r"^\s*\d{1,2}\.?\s+(?:jan|feb|mar|apr|may|mai|jun|jul|aug|sep|oct|okt|nov|dec|dez)[a-z]*\b")
_RX_DATETIME = _c(r"\d{1,2}[./]\d{1,2}[./]\d{2,4},?\s+(?:um\s+|at\s+)?\d{1,2}:\d{2}")
_RX_PRICE_END = re.compile(r"\d[.,']?\d{2}\s*[€$£]?\s*[a-z]{0,3}\s*$")
_RX_LOGISTICS = _c(r"has shipped|have shipped|tracking (?:number|no|link)|sendungsnummer|versandbestatigung|your order is on its way|"
                   r"out for delivery|dispatched|delivery note|lieferschein|packing slip|order confirmation|auftragsbestatigung|"
                   r"bestellbestatigung|goods receipt|wareneingang|versandt")
_NAME_RX = {
    "reminder": _c(r"final[-_ ]?notice|payment[-_ ]?reminder|zahlungserinnerung|mahnung|dunning"),
    "invoice": _c(r"credit[-_ ]?note|gutschrift"),
    "quote": _c(r"pro[-_ ]?forma"),
    "tax_document": _c(r"steuer\w*bescheid|tax[-_ ]?(?:return|assessment|calculation)|vat[-_ ]?return|grundsteuer"),
    "bank_statement": _c(r"kontoauszug|bank[-_ ]?statement|account[-_ ]?statement|quartalsauszug|umsatze"),
    "payslip": _c(r"verdienstbescheinigung|payslip|lohnabrechnung|gehaltsabrechnung"),
    "receipt": _c(r"kassen\w*|zahlungsbestatigung|receipt|quittung"),
}


def classify_detail(text: str, filename: str = "", ext: str = "", meta: dict | None = None,
                    prior: dict | None = None, extra_keywords: dict | None = None) -> tuple[str, float, dict]:
    meta = meta or {}
    ext = (ext or "").lower().lstrip(".")
    raw = _prep(text)[:CLASSIFY_CHARS]
    low = _fold(raw)
    if ext in _OCR_EXT:
        low = _ocr_fix(low)
    low = _tl(low)
    llines = low.split("\n")
    title_lines = _title_lines(llines)
    if meta.get("title"):
        title_lines = [_tl(_fold(str(meta["title"])))[:120]] + title_lines
    is_email = ext in ("eml", "emlx") or meta.get("kind") == "email"
    plain = ext in _PLAIN_EXT
    sheet = ext in _SPREADSHEET_EXT

    # identity documents: nothing is derived
    if _MRZ.search(raw) or len(set(_IDENTITY_WORDS.findall(low[:HEAD_CHARS]))) >= 3:
        return "other", 0.9, {"identity": True}

    types = [t for t in DOCTYPES if t != "other"]
    title = dict.fromkeys(types, 0.0)       # the document names itself in a heading (or the subject of a mail)
    subject = dict.fromkeys(types, 0.0)     # "Re: your invoice ..." in a letter
    kw = dict.fromkeys(types, 0.0)
    nm = dict.fromkeys(types, 0.0)
    st = dict.fromkeys(types, 0.0)
    sal = bool(_RX_SALUTATION.search(low[:HEAD_CHARS + 600]))
    clo = bool(_RX_CLOSING.search(low[-1200:] if len(low) > 1200 else low))
    letter_form = sal and clo

    # --- title lines (the first lines of the document) and the subject line of a mail
    for t, rx in _TITLE.items():
        for n, ln0 in enumerate(title_lines):
            ln = ln0
            m = rx.match(ln)
            if not m:                                         # "BILL TO        INVOICE": the heading is a column of its own
                for seg in re.split(r"\s{2,}", ln0)[1:]:
                    m = rx.match(seg.strip(" \t#*>-_=|:."))
                    if m:
                        ln = seg.strip(" \t#*>-_=|:.")
                        break
            if not m and len(ln) <= 70 and len(ln.split()) <= 5:
                m = rx.search(ln[:30])
                if m and re.search(r"[\d;,:]", ln[:m.start()]):
                    m = None
            if not m:
                continue
            tail = ln[m.end():].strip()
            short = len(ln) <= 70 and len(ln.split()) <= 9
            numbered = m.start() == 0 and bool(re.match(r"^(?:nr|no|number|nummer|#|:|\d|vom|from|dated|-|\()", tail))
            if short or numbered:
                # a heading that starts with the name counts fully; "overdue invoice", "tax invoice" (found inside) less
                inside = 0.6 if m.start() > 0 else 1.0
                title[t] = max(title[t], W_TITLE * inside * (W_TITLE_PLAIN if plain else 1.0) * (0.85 if n >= 6 else 1.0))
        for l in llines[:40]:
            if is_email and l.startswith("subject:") and rx.search(l):
                title[t] = max(title[t], W_SUBJECT + 1.5)
            elif _RX_SUBJECT_LINE.match(l) and rx.search(l):
                subject[t] = max(subject[t], W_SUBJECT)

    # --- keywords: distinct matches, the top of the document counts double
    extra_rx = _extra_rx(extra_keywords)
    for t, rx in _STRONG.items():
        seen_head, seen_body = set(), set()
        for m in (list(rx.finditer(low)) + (list(extra_rx[t].finditer(low)) if t in extra_rx else [])):
            (seen_head if m.start() < HEAD_CHARS else seen_body).add(m.group(0))
        seen_body -= seen_head
        v = W_STRONG_HEAD * min(len(seen_head), 5) + W_STRONG_BODY * min(len(seen_body), 6)
        if plain:
            v *= 0.8
        if sheet and t == "bank_statement":
            v *= 0.4
        kw[t] = min(v, W_STRONG_CAP)

    # --- file and folder names
    base, folders = _split_name(filename)
    fbase = _tl(_fold(base))
    toks = set(re.split(r"[^a-z0-9]+", fbase))
    ftoks = set(re.split(r"[^a-z0-9]+", _tl(_fold(" ".join(folders)))))
    for t, (strong, weak) in _NAME.items():
        if t == "letter" and not (toks & strong):
            continue
        hit = 0.0
        if toks & strong:
            hit = W_NAME
        elif toks & weak:
            hit = W_NAME_WEAK
        rxn = _NAME_NUMBERED.get(t)
        if rxn and rxn.search(fbase):
            hit = max(hit, W_NAME_WEAK * 2)
        if t in _NAME_RX and _NAME_RX[t].search(fbase):
            hit = max(hit, W_NAME)
        if ftoks & strong:
            hit += W_FOLDER
        nm[t] = hit

    # --- structure
    n_total = len(_RX_TOTAL_LINE.findall(low))
    if n_total:
        for t in ("invoice", "receipt", "quote", "reminder"):
            st[t] += 1.0
    if sum(1 for l in llines[:80] if len(_RX_ITEM_HEADER.findall(l)) >= 3):
        st["invoice"] += 1.5
        st["quote"] += 1.5
        st["receipt"] += 0.5
    n_iban = len(_RX_IBAN_LINE.findall(_fold(raw)))        # the text as folded, not transliterated: "AE07 ..." is not "A07 ..."
    if n_iban:
        st["invoice"] += 0.5
        st["bank_statement"] += 1.5
    date_lines = sum(1 for l in llines if _RX_DATE_START.match(l))
    daymon_lines = sum(1 for l in llines if _RX_DAYMON_START.match(l))
    if daymon_lines >= 3 and re.search(r"balance|saldo|kontostand|statement|kontoauszug", low):
        st["bank_statement"] += 3.0                                       # "03 Feb  Interest  £10.34" rows
    if date_lines >= 6 and not sheet:
        st["bank_statement"] += 2.0 + (1.0 if n_iban or "saldo" in low or "balance" in low else 0.0)
    if ext in ("csv", "tsv") and llines and len(set(_CSV_BANK_HEADERS.findall(llines[0]))) >= 2:
        st["bank_statement"] += 6.0
    tail_lines = llines[int(len(llines) * 0.6):]
    if sum(1 for l in tail_lines if _RX_SIGNATURE.search(l)) >= 1 and kw["contract"] > 0:
        st["contract"] += 2.0
    if letter_form:
        st["letter"] += 3.5 + (1.0 if not is_email and any(_RX_SUBJECT_LINE.match(l) for l in llines[:40]) else 0.0)
    elif sal or clo:
        st["letter"] += 1.0
    # the shape of a till slip: a short document, several price lines and a total (not a statement: no bank words)
    body = [l for l in llines if l.strip()]
    banky = kw["bank_statement"] >= 3.0 or n_iban or date_lines >= 4
    if not banky and len(body) <= 35 and n_total and sum(1 for l in body if _RX_PRICE_END.search(l)) >= 3:
        st["receipt"] += 3.0
        kw["receipt"] += W_STRONG_HEAD
    if not banky and _RX_DATETIME.search(low[:HEAD_CHARS * 2]):
        st["receipt"] += 1.5
    # shipping notices, order confirmations and delivery notes talk about money without being the invoice or receipt
    if _RX_LOGISTICS.search(low[:HEAD_CHARS * 2]):
        for t in ("invoice", "receipt", "quote"):
            kw[t] *= 0.5
            st[t] *= 0.5

    # a bill headed "Rechnung" that was settled on the spot (card, cash, tip) and states no payment terms is a receipt
    if title["invoice"] > 0 and title["receipt"] == 0 and kw["receipt"] >= W_STRONG_HEAD and _RX_PAID.search(low) \
            and not _RX_DUE.search(low):
        title["receipt"] = 0.8 * W_TITLE
        title["invoice"] *= 0.7

    # --- a formal letter that merely talks about an invoice in its subject (or its file name) is a letter
    if letter_form:
        for t in types:
            if t != "letter" and title[t] == 0:
                subject[t] *= 0.3
                nm[t] *= 0.5
                if kw[t] < 4.5:
                    kw[t] *= 0.6                         # a letter to tenants says "Mieter" without being a contract
    if is_email:
        for t in types:
            if t != "letter" and letter_form:
                title[t] *= 0.6
        st["letter"] *= 0.75
        kw["letter"] *= 0.75

    # --- a reminder cites its invoice, a pro forma invoice is a quote, a till slip may say "Rechnung": when the cited type
    # has a heading of its own (or plenty of its own words), the type it mentions counts for little
    for sup, targets in (("reminder", ("invoice", "quote")), ("quote", ("invoice",)), ("receipt", ("invoice",)),
                         ("tax_document", ("invoice",))):
        if title[sup] > 0 or kw[sup] >= 4.5:
            for t in targets:
                kw[t] *= 0.4
                st[t] *= 0.4
                nm[t] *= 0.6
                if title[sup] > 0:
                    title[t] *= 0.6                      # "Reminder: invoice X overdue", "PRO FORMA INVOICE": the first word rules
    # --- a title decides: the other types' keyword evidence counts half (a reminder cites its invoice, a quote has totals)
    scores = {t: title[t] + subject[t] + kw[t] + nm[t] + st[t] for t in types}
    if any(title.values()):
        top_title = max(title.values())
        for t in types:
            if title[t] < top_title - 1e-9:
                scores[t] = title[t] + 0.5 * (scores[t] - title[t])
    if prior:
        for t, p in prior.items():
            if t in scores:
                scores[t] += 2.0 * max(0.0, min(1.0, float(p)))

    if plain:
        # a note or a markdown page that mentions invoices is not one: it needs a strong keyword of the type itself
        for t in types:
            if kw[t] < W_STRONG_HEAD:
                scores[t] = 0.0
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    (best_t, s1), (_, s2) = ranked[0], ranked[1]
    if s1 <= 0:
        return "other", 0.9, scores
    strength = 1.0 - math.exp(-s1 / CONF_SCALE)
    margin = (s1 - s2) / s1
    conf = strength * (0.35 + 0.65 * margin)
    floor = MIN_CONFIDENCE_PLAIN if plain else MIN_CONFIDENCE
    if conf < floor:
        return "other", round(min(0.95, max(0.3, 1.0 - conf)), 3), scores
    return best_t, round(conf, 3), scores


def _split_name(filename: str) -> tuple[str, list[str]]:
    parts = (filename or "").replace("\\", "/").split("/")
    base = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", parts[-1])
    return base, parts[:-1][-3:]


# ------------------------------------------------------------------ numbers, currencies, dates

# ISO 4217 codes that are not also ordinary words (so "ron", "try", "all", "mad" are left out), plus the usual signs
_ISO_CODES = ("aed aud bgn brl cad chf clp cny cop czk dkk egp eur gbp hkd huf idr ils inr isk jpy krw mxn myr nok nzd php pkr pln "
              "rsd rub sek sgd thb twd uah usd vnd zar").split()
_CURRENCY = {c: c.upper() for c in _ISO_CODES}
_CURRENCY.update({"fr.": "CHF", "euro": "EUR", "euros": "EUR", "€": "EUR", "us$": "USD", "$": "USD", "£": "GBP",
                  "sfr": "CHF", "sfr.": "CHF", "₹": "INR", "₩": "KRW"})


def canon_currency(v: str) -> str | None:
    """'eur', '€', 'Euro' -> 'EUR'; anything unknown -> None."""
    return _CURRENCY.get((v or "").strip().lower())


_CUR_TOK = r"(?:euros?|€|us\$|\$|£|sfr\.?|fr\.|₹|₩|" + "|".join(_ISO_CODES) + r")"
_NUM_CORE = (r"\d{1,3}(?:[.,'’   ]\d{3})+(?:[.,]\d{1,2})?|\d{1,3}(?: \d{3})+,\d{2}|\d+(?:[.,]\d{1,2})?")
_MONEY = re.compile(rf"(?<![\w.,/'’-])(?P<neg0>[-−])?(?:(?P<c1>{_CUR_TOK})[ \t]{{0,15}})?(?P<neg>[-−])?[ \t]?(?P<num>{_NUM_CORE})(?P<tneg>-)?(?!\d)"
                    rf"(?:[ \t]{{0,15}}(?P<c2>{_CUR_TOK})(?![a-z]))?")


def parse_number(s: str) -> float | None:
    """'1.234,56' / '1,234.56' / "1'234.50" / '1 234,56' / '125,000' / '12,5' -> float. None when it is not a number."""
    s = re.sub(r"[\s'’   ]", "", s or "")
    if not s or not re.fullmatch(r"[\d.,]+", s):
        return None
    dots, commas = s.count("."), s.count(",")
    if dots and commas:
        dec = "." if s.rfind(".") > s.rfind(",") else ","
        thou = "," if dec == "." else "."
        s = s.replace(thou, "").replace(dec, ".")
    elif commas:
        if commas > 1:
            s = s.replace(",", "")
        else:
            left, right = s.split(",")
            s = left + right if len(right) == 3 and 1 <= len(left) <= 3 and left != "0" else left + "." + right
    elif dots:
        if dots > 1:
            s = s.replace(".", "")
        else:
            left, right = s.split(".")
            s = left + right if len(right) == 3 and 1 <= len(left) <= 3 and left != "0" else s
    try:
        v = float(s)
    except ValueError:
        return None
    return v if math.isfinite(v) and abs(v) < 1e12 else None


_MONTHS = {}
for _i, _names in enumerate([("january", "januar", "janner", "jan"), ("february", "februar", "feb"),
                             ("march", "marz", "maerz", "mar"), ("april", "apr"), ("may", "mai"),
                             ("june", "juni", "jun"), ("july", "juli", "jul"), ("august", "aug"),
                             ("september", "sept", "sep"), ("october", "oktober", "oct", "okt"),
                             ("november", "nov"), ("december", "dezember", "dec", "dez")], 1):
    for _n in _names:
        _MONTHS[_n] = _i
_MONTH_RX = "|".join(sorted(_MONTHS, key=len, reverse=True))
_D_ISO = re.compile(r"(?<![\d.])(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?![\d])")
_D_DMONY = re.compile(rf"(?<![\d])(\d{{1,2}})(?:st|nd|rd|th)?\.?[\s-]*(?:of\s+)?({_MONTH_RX})\.?,?[\s-]*(\d{{4}}|\d{{2}})(?![\d])")
_D_MONDY = re.compile(rf"(?<![a-z])({_MONTH_RX})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})(?![\d])")
_D_NUM = re.compile(r"(?<![\d.,/-])(\d{1,2})([./-])(\d{1,2})\2(\d{4}|\d{2})(?![\d])")
_M_MONY = re.compile(rf"(?<![a-z\d])({_MONTH_RX})\.?\s+(\d{{4}})(?![\d])")
_M_NUM = re.compile(r"(?<![\d./-])(\d{1,2})/(\d{4})(?![\d/])")
_M_ISO = re.compile(r"(?<![\d-])(\d{4})-(\d{2})(?![\d-])")
_DATE_MASK = re.compile(r"(?<![\d.,/-])(?:\d{1,2}[./-]\d{1,2}[./-](?:\d{4}|\d{2})|\d{4}[-/.]\d{1,2}[-/.]\d{1,2})(?![\d])")

# labels that stand near a date; negative ones are never a document date
_DL_NEG = (r"fallig\w*(?: am)?", r"zahlbar (?:bis|innerhalb|am)", r"zahlungsziel", r"zahlung bis", r"zahlungsfrist", r"nachfrist",
           r"frist", r"payable (?:by|on)", r"due(?: date| on| by)?", r"pay(?:ment)? (?:by|due)", r"valid(?: until| through| to| thru| from)?",
           r"gultig\w*(?: bis| ab| fur)?", r"bindefrist", r"expir\w*", r"ablauf\w*", r"verfall\w*", r"lieferdatum", r"liefertermin",
           r"lieferung", r"leistungsdatum", r"leistungszeitraum", r"leistungszeit", r"leistung", r"delivery(?: date)?",
           r"date of service", r"service (?:date|period)", r"performance period", r"geburts\w*", r"geboren(?: am)?", r"geb\.",
           r"date of birth", r"d\.?o\.?b\.?", r"birth\w*", r"born", r"renewal", r"kundigung\w*", r"termination", r"deadline",
           r"spatestens", r"bis zum", r"bis", r"until", r"end date", r"ende", r"laufzeit", r"beginn", r"start(?: date)?",
           r"begin", r"from", r"von", r"ab", r"to", r"order date", r"bestelldatum", r"auftragsdatum", r"ihre bestellung",
           r"reference date", r"referenzdatum", r"shipping date", r"versanddatum", r"ship date", r"date of supply", r"supply date",
           r"tax point", r"arrival", r"departure", r"check-?in", r"check-?out", r"anreise", r"abreise", r"date of birth")
_DL_POS3 = (r"rechnungsdatum", r"datum der rechnung", r"rechnung vom", r"invoice date", r"date of invoice", r"date of issue",
            r"issue date", r"issued(?: on)?", r"ausstellungsdatum", r"ausgestellt am", r"belegdatum", r"angebotsdatum",
            r"angebot vom", r"quote date", r"date of quote", r"quotation date", r"offer date", r"statement date",
            r"date of statement", r"kontoauszug vom", r"auszug vom", r"auszugsdatum", r"kontoauszugsdatum", r"mahndatum",
            r"datum der mahnung", r"reminder date", r"date of reminder", r"kaufdatum", r"date of purchase", r"purchase date",
            r"transaction date", r"bescheid vom", r"bescheiddatum", r"datum des bescheids", r"date of assessment",
            r"assessment date", r"ausgefertigt am", r"abrechnungsdatum", r"abrechnung vom", r"payslip date", r"pay date",
            r"payment date", r"zahlungsdatum", r"auszahlungsdatum", r"paid on", r"bezahlt am", r"vertragsdatum",
            r"date of agreement", r"agreement date", r"creation date", r"created(?: on)?", r"erstellt am",
            r"erstellungsdatum", r"abschlussdatum", r"letter date", r"briefdatum", r"date of receipt", r"date paid", r"credit note date",
            r"date of offer", r"abrechnung erstellt am", r"date of letter", r"printed", r"gedruckt am", r"print date")
_DL_POS2 = (r"datum", r"date", r"stand", r"as of", r"per", r"tag der ausstellung", r"last updated", r"updated", r"zuletzt aktualisiert",
            r"aktualisiert am", r"last modified", r"geandert am")
_DL_POS1 = (r"vom", r"am", r"on", r"den", r"dated", r"effective(?: date)?", r"vertragsbeginn", r"inkrafttreten")
_DL_PERIOD = (r"abrechnungsmonat", r"abrechnungszeitraum", r"abrechnungsperiode", r"pay period", r"payroll period",
              r"zeitraum", r"periode", r"period", r"monat", r"month", r"for the month of", r"lohnabrechnung(?: fur)?",
              r"gehaltsabrechnung(?: fur)?", r"payslip(?: for)?", r"entgeltabrechnung(?: fur)?", r"abrechnung fur")
_DATE_LABELS = re.compile(
    r"(?<![a-z])(?:(?P<neg>" + "|".join(_DL_NEG) + r")|(?P<per>" + "|".join(_DL_PERIOD) + r")|(?P<p3>" + "|".join(_DL_POS3) +
    r")|(?P<p2>" + "|".join(_DL_POS2) + r")|(?P<p1>" + "|".join(_DL_POS1) + r"))(?![a-z])")
_RANGE_GAP = re.compile(r"^\s*(?:-|–|bis|to|until|thru|through|und|and)\s*$")
_PLACE_DATE = re.compile(r"^[a-z][a-z .\-]{2,28},\s*(?:den |am |the |on )?$")


def _norm_year(y: int, today: date) -> int:
    if y >= 100:
        return y
    return 2000 + y if 2000 + y <= today.year + 1 else 1900 + y


def _mk_date(y: int, m: int, d: int | None, today: date) -> str | None:
    try:
        if d is None:
            if not 1 <= m <= 12:
                return None
            y = _norm_year(y, today)
            if y < 1980 or (y, m) > (today.year, today.month):
                return None
            return f"{y:04d}-{m:02d}"
        y = _norm_year(y, today)
        dt = date(y, m, d)
    except ValueError:
        return None
    if y < 1980 or dt > today + timedelta(days=1):
        return None
    return dt.isoformat()


def _date_candidates(llines: list[str], today: date, prefer_mdy: bool) -> list[dict]:
    """Every plausible date: {iso, full, line, pos, label (kind of the nearest label before it), end_of_range}."""
    out = []
    prev_labels: list[tuple[str, int]] = []
    prev_aligned_done = False
    for li, line in enumerate(llines):
        if not line.strip():
            continue
        found = []
        work = line
        for rx, kind in ((_D_ISO, "iso"), (_D_DMONY, "dmony"), (_D_MONDY, "mondy"), (_D_NUM, "num")):
            for m in rx.finditer(work):
                iso = None
                if kind == "iso":
                    iso = _mk_date(int(m.group(1)), int(m.group(2)), int(m.group(3)), today)
                elif kind == "dmony":
                    iso = _mk_date(int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1)), today)
                elif kind == "mondy":
                    iso = _mk_date(int(m.group(3)), _MONTHS[m.group(1)], int(m.group(2)), today)
                else:
                    a, sep, b, y = int(m.group(1)), m.group(2), int(m.group(3)), int(m.group(4))
                    if sep == "." or a > 12:
                        d_, m_ = a, b
                    elif b > 12:
                        d_, m_ = b, a
                    else:
                        d_, m_ = (b, a) if prefer_mdy else (a, b)
                    iso = _mk_date(y, m_, d_, today)
                if iso:
                    found.append({"iso": iso, "full": True, "pos": m.start(), "end": m.end()})
                work = work[:m.start()] + " " * (m.end() - m.start()) + work[m.end():]
        for rx, kind in ((_M_MONY, "mony"), (_M_NUM, "mnum"), (_M_ISO, "miso")):
            for m in rx.finditer(work):
                if kind == "mony":
                    iso = _mk_date(int(m.group(2)), _MONTHS[m.group(1)], None, today)
                elif kind == "mnum":
                    iso = _mk_date(int(m.group(2)), int(m.group(1)), None, today)
                else:
                    iso = _mk_date(int(m.group(1)), int(m.group(2)), None, today)
                if iso:
                    found.append({"iso": iso, "full": False, "pos": m.start(), "end": m.end()})
                    work = work[:m.start()] + " " * (m.end() - m.start()) + work[m.end():]
        found.sort(key=lambda c: c["pos"])
        labels = [(m.lastgroup, m.start(), m.end()) for m in _DATE_LABELS.finditer(line)]
        for k, c in enumerate(found):
            c["line"] = li
            lab = None
            gap0 = line[found[k - 1]["end"]:c["pos"]] if k > 0 else ""
            in_range = k > 0 and (_RANGE_GAP.match(gap0) or (re.fullmatch(r"[\s;,\"'|]+", gap0) and any(kd == "per" for kd, _, _ in labels)))
            for kind, ls, le in labels:
                if in_range and ls >= found[k - 1]["end"]:
                    continue                                # "01.02. bis 28.02.": the connector does not label the end of the range
                if le <= c["pos"] and c["pos"] - le <= 45 and not any(f["end"] <= ls and f is not c and f["pos"] > le - 1 for f in found[:k] if f["pos"] >= le):
                    lab = (kind, le, ls)
            c["label"] = lab[0] if lab else (found[k - 1]["label"] if in_range else None)
            c["labtext"] = line[lab[2]:lab[1]] if lab else (found[k - 1].get("labtext", "") if in_range else "")
            c["placedate"] = bool(_PLACE_DATE.match(line[:c["pos"]])) and c["pos"] < 40
            c["alone"] = not re.search(r"[a-z0-9]", (line[:c["pos"]] + " " + line[c["end"]:]))
            # a range "a - b": the second date is its end, both dates are the period
            gap = line[found[k - 1]["end"]:c["pos"]] if k > 0 else ""
            if k > 0 and (_RANGE_GAP.match(gap) or (re.fullmatch(r"[\s;,\"'|]+", gap) and
                                                   any(kd == "per" for kd, _, _ in labels))):
                c["range_end"] = True
                found[k - 1]["range_start"] = True
        if found and not any(c["label"] for c in found) and prev_labels and not prev_aligned_done:
            # a table: labels on the previous line, values below. Align from the right.
            date_labels = [kd for kd, _ in prev_labels if kd in ("neg", "p3", "p2", "p1", "per")]
            if len(date_labels) >= len(found):
                for j, c in enumerate(reversed(found), 1):
                    c["label"] = date_labels[-j]
                    c["aligned"] = True
                prev_aligned_done = True            # only the first row under a header takes its column labels
        elif found:
            prev_aligned_done = True
        new_labels = [(m.lastgroup, m.start()) for m in _DATE_LABELS.finditer(line)]
        if new_labels:
            prev_labels, prev_aligned_done = new_labels, False
        else:
            prev_labels = []                              # only the line right above a row can be its header
        out.extend(found)
    return out


_LABEL_SCORE = {"p3": 3.0, "p2": 2.0, "p1": 1.2, "per": 1.0, None: 1.0}


_FOREIGN_LABEL = {   # words in a date label that name another kind of document: "Rechnung vom ...", "Angebot vom ..."
    "invoice": re.compile(r"(?<![a-z])(?:angebot|quot|offer|bestell|order|auftrag|mahn|remind)"),
    "receipt": re.compile(r"(?<![a-z])(?:angebot|quot|offer|bestell|order|auftrag|mahn|remind)"),
    "quote": re.compile(r"(?<![a-z])(?:bestell|order|auftrag|mahn|remind)"),
    "tax_document": re.compile(r"(?<![a-z])(?:angebot|quot|offer|bestell|order|auftrag|mahn|remind)"),     # a tax bill is a "Steuerrechnung"
}
_FOREIGN_DEFAULT = re.compile(r"(?<![a-z])(?:rechnung|invoice|angebot|quot|offer|bestell|order|auftrag|mahn|remind|credit note|gutschrift)")


def _pick_date(cands: list[dict], doctype: str, nlines: int, csv_like: bool = False) -> str | None:
    foreign = _FOREIGN_LABEL.get(doctype, _FOREIGN_DEFAULT)
    usable = [c for c in cands if c["label"] != "neg" and not foreign.search(c.get("labtext", ""))]
    if doctype not in ("bank_statement", "payslip"):
        usable = [c for c in usable if c["full"] and not c.get("range_end") and not c.get("range_start")]
    if not usable:
        return None
    if doctype == "payslip":
        best, best_s = None, -1.0
        for c in usable:
            s = _LABEL_SCORE.get(c["label"], 1.0)
            if c["label"] == "p3" and c["full"]:
                s = 3.8                      # an issue / payment date that is printed beats the month
            if c["label"] == "per":
                s = 3.2 if not c["full"] else 3.1
            if c.get("range_end"):
                s += 2.0
            if c.get("range_start"):
                s -= 0.5
            s += 0.3 if c["line"] < nlines * 0.4 else 0
            if s > best_s + 1e-9:
                best, best_s = c, s
        if best is None:
            return None
        if best["label"] == "per" or best.get("range_end") or not best["full"]:
            return best["iso"][:7]
        return best["iso"]
    if doctype == "bank_statement":
        plain_ok = lambda c: c["full"] and not c.get("range_start") and not c.get("aligned")
        stmt = [c for c in usable if c["label"] == "p3" and plain_ok(c)]
        if stmt:
            return min(stmt, key=lambda c: c["line"])["iso"]
        ends = [c for c in usable if c.get("range_end") and c["full"]]
        if ends:
            return ends[0]["iso"]
        dated = [c for c in usable if c["label"] == "p2" and plain_ok(c)]
        if dated:
            return min(dated, key=lambda c: c["line"])["iso"]
        fulls = [c["iso"] for c in usable if c["full"]]
        return max(fulls) if fulls and not csv_like else None
    best, best_s = None, -1.0
    for c in usable:
        s = _LABEL_SCORE.get(c["label"], 1.0)
        if c.get("aligned"):
            s *= 0.6
        if c.get("placedate") and doctype in ("letter", "contract", "quote", "reminder", "tax_document", "invoice", "receipt"):
            s = max(s, 2.1)
        if c.get("alone") and doctype != "other":
            s = max(s, 2.0)                     # a line with nothing but the date: the date of the document
        if doctype == "other":
            if c.get("alone") and c["line"] <= 3 and s < 2.0:
                s = 2.0
            if s < 2.0 or c.get("aligned") or c["line"] > 14:
                continue
        s += 0.3 if c["line"] < max(3, nlines * 0.35) else 0
        if s > best_s + 1e-9:
            best, best_s = c, s
    return best["iso"] if best else None


# ------------------------------------------------------------------ amounts

_ZONE_GROSS = re.compile(r"(?:incl\.?|inkl\.?|including|inklusive|einschl\.?|brutto|gross)\s*(?:vat|mwst|ust|tax|mehrwertsteuer|umsatzsteuer|tva)\.?")
_AM_COMPOSITE_EXCL = (r"net(?:to)? (?:total|gesamt|summe|amount|betrag)", r"(?:total|gesamt|summe|betrag|amount) (?:net|netto|excl\w*|exkl\w*|ex\.? vat|vat|mwst|ust|tax|steuer|hours|stunden|quantity|menge|qty|items)",
                      r"total before (?:tax|vat)", r"sub ?-?total", r"zwischensumme", r"zwischensaldo")
_AM_EXCL = (r"netto", r"\bnet\b", r"excl\w*", r"exkl\w*", r"zzgl", r"\bplus\b", r"\bvat\b", r"mwst", r"mehrwertsteuer", r"\bust\b", r"umsatzsteuer", r"\btax\b",
            r"steuer", r"\btva\b", r"discount", r"rabatt", r"skonto", r"versand\w*", r"shipping", r"porto", r"fracht", r"gebuhr\w*", r"\bfee\b",
            r"surcharge", r"zuschlag", r"anzahlung", r"deposit", r"abzug", r"vortrag", r"ubertrag", r"carried", r"previous", r"opening",
            r"anfangs\w*", r"limit", r"menge", r"quantity", r"\bqty\b", r"anzahl", r"stuck", r"\bstk\b", r"hours", r"stunden", r"items", r"positionen",
            r"gewicht", r"weight", r"seiten", r"pages", r"\btel\b", r"\bfax\b", r"phone", r"telefon", r"\bplz\b", r"\bzip\b", r"\biban\b", r"\bbic\b",
            r"konto\w*", r"account", r"zins\w*", r"interest")
_PAID_EXCL = (r"paid", r"bezahlt", r"bereits gezahlt", r"payment received", r"zahlung erhalten")
_AM_HI_GENERIC = (r"gesamtbetrag", r"gesamtsumme", r"gesamt ?preis", r"rechnungsbetrag", r"rechnungssumme", r"endbetrag", r"endsumme",
                  r"brutto(?:betrag|summe)?", r"summe brutto", r"total brutto", r"grand total", r"total amount", r"total gross",
                  r"gross (?:total|amount)", r"invoice (?:total|amount)", r"total invoice(?: amount)?", r"total ttc", r"\bttc\b",
                  r"total incl\w*", r"total including", r"angebotssumme", r"angebotspreis", r"total price", r"quote total", r"offer total",
                  r"amount payable", r"total payable", r"zu zahlender gesamtbetrag", r"montant total")
_AM_MID_GENERIC = (r"total", r"gesamt", r"summe", r"\bsum\b", r"betrag", r"amount", r"amount due", r"balance due", r"total due",
                   r"zahlbetrag", r"zahlungsbetrag", r"zu zahlen\w*", r"noch zu zahlen", r"payable", r"to pay", r"\bdue\b", r"saldo",
                   r"totaal", r"zahlung")
_AM_HI_REMINDER = (r"total overdue", r"overdue total", r"total outstanding balance", r"gesamtforderung", r"gesamt ?forderung", r"forderung gesamt", r"total claim", r"total outstanding",
                   r"total amount due", r"total due", r"zu zahlender gesamtbetrag", r"gesamtbetrag", r"summe offener posten",
                   r"insgesamt(?: zu zahlen)?", r"grand total", r"total payable")
_AM_MID_REMINDER = (r"offener betrag", r"offene forderung", r"\boffen\b", r"outstanding(?: amount| balance)?", r"open amount",
                    r"amount due", r"balance due", r"rechnungsbetrag", r"invoice amount", r"forderung", r"betrag", r"total", r"summe",
                    r"gesamt", r"amount", r"zu zahlen\w*")
_AM_HI_PAYSLIP = (r"auszahlungsbetrag", r"auszahlung", r"nettoauszahlung", r"netto-?auszahlung", r"ausgezahlt", r"net pay",
                  r"net salary", r"net wages", r"take-?home(?: pay)?", r"nettolohn", r"nettogehalt", r"netto-?verdienst",
                  r"uberweisungsbetrag", r"payout", r"amount paid")
_AM_MID_PAYSLIP = (r"\bnetto\b", r"\bnet\b", r"net amount")
_AM_EXCL_PAYSLIP = (r"brutto", r"gross", r"steuer", r"\btax\b", r"versicherung", r"insurance", r"abzug", r"deduction", r"zuschlag", r"\bytd\b",
                    r"kumul\w*", r"vorjahr", r"gesamtbrutto")
_AM_HI_TAX = (r"balance (?:of )?tax (?:payable|due)", r"balance payable", r"net tax payable", r"tax balance", r"vat payable", r"net vat", r"vat to pay", r"zahllast", r"nachzahlung", r"abschlusszahlung", r"erstattung",
              r"ruckerstattung", r"guthaben", r"zu zahlender betrag", r"balancing payment", r"amount due", r"balance due", r"refund",
              r"amount payable", r"verbleibende\w*", r"zu zahlen", r"zu erstattend\w*", r"jahresbetrag", r"annual amount",
              r"payment due")
_AM_MID_TAX = (r"vat due", r"tax due", r"tax payable", r"festgesetzte steuer", r"steuerschuld", r"total tax")
_AM_HI_CONTRACT = (r"gesamtwert", r"total contract value", r"contract total", r"licen[cs]e fee", r"total (?:\w+ )?fee", r"vertragswert", r"vertragssumme", r"vertragswert", r"vertragsvolumen", r"gesamtvergutung", r"gesamthonorar", r"contract (?:value|sum|price|amount)",
                   r"total (?:contract|fee|consideration|compensation)", r"fixed fee", r"pauschalpreis", r"kaufpreis", r"purchase price")


def _label_rx(*groups) -> re.Pattern:
    names = ("cx", "hi", "mid", "ex")
    return re.compile("|".join(f"(?P<{n}>" + "|".join(g) + ")" for n, g in zip(names, groups) if g))


_AMOUNT_RX = {
    "generic": _label_rx(_AM_COMPOSITE_EXCL, _AM_HI_GENERIC, _AM_MID_GENERIC, _AM_EXCL + _PAID_EXCL),
    "receipt": _label_rx(_AM_COMPOSITE_EXCL, _AM_HI_GENERIC, _AM_MID_GENERIC + (r"you paid", r"amount paid", r"total paid", r"paid in total"), _AM_EXCL),
    "reminder": _label_rx(_AM_COMPOSITE_EXCL, _AM_HI_REMINDER, _AM_MID_REMINDER, _AM_EXCL),
    "payslip": _label_rx((), _AM_HI_PAYSLIP, _AM_MID_PAYSLIP, _AM_EXCL_PAYSLIP),
    "tax_document": _label_rx((), _AM_HI_TAX, _AM_MID_TAX, _AM_EXCL),
    "contract": _label_rx((), _AM_HI_CONTRACT, (), ()),
}
_AMOUNT_FALLBACK = {"invoice", "receipt", "quote", "reminder"}


def _mask_line(l: str) -> str:
    l = _DATE_MASK.sub(lambda m: " " * len(m.group(0)), l)
    l = _D_DMONY.sub(lambda m: " " * len(m.group(0)), l)
    l = _D_MONDY.sub(lambda m: " " * len(m.group(0)), l)
    l = _RX_IBAN_LINE.sub(lambda m: " " * len(m.group(0)), l)
    l = re.sub(r"\d{1,3}(?:[.,]\d+)?\s?%", lambda m: " " * len(m.group(0)), l)               # 19 % / 7,5%
    l = re.sub(r"\+?\d[\d ()/\-]{7,}\d", lambda m: " " * len(m.group(0)) if len(re.sub(r"\D", "", m.group(0))) >= 8 else m.group(0), l)
    l = re.sub(r"\d{9,}", lambda m: " " * len(m.group(0)), l)
    return l


def _money_candidates(llines: list[str]) -> list[dict]:
    out = []
    last_label_line = ""
    for li, line in enumerate(llines):
        if not line.strip():
            continue
        masked = _mask_line(line)
        prev_end = 0
        for m in _MONEY.finditer(masked):
            num = m.group("num")
            value = parse_number(num)
            if value is None:
                continue
            neg = bool(m.group("neg")) or bool(m.group("tneg")) or bool(m.group("neg0"))
            s, e = m.start(), m.end()
            if s > 0 and masked[s - 1] == "(" and e < len(masked) and masked[e:e + 1] == ")":
                neg = True
            if neg:
                value = -value
            cur = _CURRENCY.get((m.group("c1") or m.group("c2") or "").lower())
            zone = masked[prev_end:s].rstrip()[-72:]
            if len(zone.strip(" :.-")) <= 1 and li > 0:
                zone = last_label_line[-44:] + " " + zone
            prev_end = e
            out.append({"value": value, "cur": cur, "line": li, "start": s, "zone": _ZONE_GROSS.sub(" gross ", zone),
                        "has_dec": bool(re.search(r"[.,]\d{1,2}$", num)) or bool(re.search(r"\d[.,'’ ]\d{3}", num))})
        if line.strip():
            last_label_line = line.strip()
    return out


_RX_DE_VAT = re.compile(r"(?:\bust\b|umsatzsteuer|mwst|mehrwertsteuer)\W{0,12}(?:19|7)\s?%|(?:19|7)\s?%\s*(?:\bust\b|umsatzsteuer|mwst|mehrwertsteuer)")


def _extract_amount(llines: list[str], doctype: str, kind_ext: str, lang: str | None = None) -> tuple[float | None, str | None]:
    cands = _money_candidates(llines)
    if not cands:
        return None, None
    cur_count: dict = {}
    for c in cands:
        if c["cur"]:
            cur_count[c["cur"]] = cur_count.get(c["cur"], 0) + 1
    dominant = max(cur_count, key=cur_count.get) if cur_count else None
    if doctype in ("bank_statement", "letter", "other"):
        return None, None
    rx = _AMOUNT_RX.get(doctype, _AMOUNT_RX["generic"])
    hi, mid = [], []
    for c in cands:
        last = None
        for m in rx.finditer(c["zone"]):
            last = m
        if last is None or last.lastgroup in ("cx", "ex"):
            c["label"] = last.lastgroup if last else None
            continue
        c["label"] = last.lastgroup
        if not (c["has_dec"] or c["cur"] or any(x["cur"] for x in cands if x["line"] == c["line"])):
            continue                                                  # a bare small number is not an amount
        if not (c["has_dec"] or c["cur"]) and any(x["cur"] for x in cands if x["line"] == c["line"]):
            continue                                                  # "total for 2 adults £34.50": 2 is a count
        (hi if last.lastgroup == "hi" else mid).append(c)
    pick = None
    if hi:
        pick = max(hi, key=lambda c: abs(c["value"]))
    elif mid:
        pick = max(mid, key=lambda c: abs(c["value"]))
    elif doctype in _AMOUNT_FALLBACK:
        withcur = [c for c in cands if c["cur"] and c.get("label") not in ("ex", "cx") and c["value"] != 0]
        if withcur:
            pick = max(withcur, key=lambda c: abs(c["value"]))
    if pick is None:
        return None, None
    if doctype == "contract":
        values = {round(c["value"], 2) for c in hi}
        if len(values) != 1:
            return None, None
    value = pick["value"]
    if doctype in ("tax_document", "payslip"):
        value = abs(value)
    cur = pick["cur"] or dominant
    if cur is None and lang == "de" and not cur_count and _RX_DE_VAT.search("\n".join(llines)):
        cur = "EUR"                         # German VAT rates and no other currency anywhere: euro
    return round(value, 2), cur


# ------------------------------------------------------------------ reference, counterparty

_RX_IBAN_STRICT = re.compile(r"[A-Za-z]{2}\d{2}[A-Za-z0-9]{10,30}")
_REF_KINDS = (       # (group, labels): the number a document of that kind carries
    ("inv", r"rechnungs?-?(?:nummer|nr|no)|rechnung\s*(?:nr|no|nummer)|invoice\s*(?:no|nr|number|num|#)|inv\.?\s*(?:no|nr|#)|"
            r"credit\s*note\s*(?:no|nr|number|#)|gutschrift\s*(?:nr|no|nummer)"),
    ("rec", r"beleg-?(?:nummer|nr)|belegnummer|belegnr|quittungs?-?(?:nummer|nr)|quittung\s*(?:nr|no|nummer)|receipt\s*(?:no|nr|number|#)|"
            r"bon-?(?:nr|nummer)|kassenbeleg\s*(?:nr|no)|kassenbon\s*(?:nr|no)|folio(?:\s*(?:no|nr|number|#))?"),
    ("quo", r"angebots-?(?:nummer|nr)|angebot\s*(?:nr|nummer)|quot(?:e|ation)\s*(?:no|nr|number|#)|offer\s*(?:no|nr|number|#)|"
            r"estimate\s*(?:no|nr|number|#)|pro-?forma\s*(?:no|nr|number|#)"),
    ("con", r"vertrags-?(?:nummer|nr)|vertrag\s*(?:nr|nummer)|contract\s*(?:no|nr|number|#)|agreement\s*(?:no|number)|"
            r"licen[cs]e\s*(?:no|nr|number|#)|policy\s*(?:no|nr|number|#)"),
    ("rem", r"mahn-?(?:nummer|nr)|mahnung\s*(?:nr|nummer)|reminder\s*(?:no|nr|number|#)|zahlungserinnerung\s*(?:nr|nummer)|"
            r"erinnerung\s*(?:nr|nummer)"),
    ("stm", r"statement\s*(?:no|number)|(?:konto)?auszug\s*(?:nr|nummer)|document\s*(?:no|number)|dokument-?(?:nummer|nr)"),
    ("ord", r"order(?:\s*(?:no|nr|number|id|#))?|bestell-?(?:nummer|nr)|auftrags-?(?:nummer|nr)|auftragsnummer"),
    ("gen", r"aktenzeichen|az\.|referenz(?:nummer)?|reference(?:\s*(?:no|number|nr))?|ref\.?(?:\s*(?:no|nr|#))?|ihr zeichen|"
            r"unser zeichen|your ref(?:erence)?|our ref(?:erence)?|unsere referenz|ihre referenz|booking\s*(?:no|nr|number|ref\w*)|"
            r"confirmation\s*(?:no|nr|number)|membership\s*(?:no|nr|number)|case\s*(?:no|nr|number)"),
)
_REF_LABEL = re.compile("|".join(f"(?<![a-z])(?P<{g}>{p})(?![a-z])" for g, p in _REF_KINDS))
# which kinds are the document's own number: a till slip headed "Rechnung" and a pro forma invoice carry an invoice number
_REF_OWN = {"invoice": ("inv", "rec"), "receipt": ("rec", "inv"), "quote": ("quo", "inv"), "reminder": ("rem",),
            "contract": ("con",), "bank_statement": ("stm",), "letter": ()}
_REF_VALUE = re.compile(r"[\s:#.\-]*(?:(?:no|nr|number|nummer)\b[\s:#.\-]*)?([A-Za-z0-9][A-Za-z0-9/_.\-]{1,34}[A-Za-z0-9])")
_REF_FOREIGN_LABEL = re.compile(r"customer|kunde|client|account|konto|vat|ust\b|tax|steuer|iban|tel|phone|member|mitglied|policy|page|seite|zip|plz")
_REF_BARE = re.compile(r"^(?:no|nr|number|nummer|#)\.?\s*[:#]?\s*((?:[a-z]{1,6}[-/]){0,3}\d[\w\-/]{1,24})\s*$")
_REF_CONTEXT_BAD = re.compile(
    r"iban|bic\b|swift|steuer-?id|steueridentifikation|steuernummer|tax ?id|\btin\b|vat ?id|ust-?id|passport|reisepass|ausweis|aufenthalts|"
    r"sozialversicherung|social security|personalausweis|date of birth|geburts|konto-?nr|account (?:no|number)|card|karten|"
    r"mandat|gl[aä]ubiger|creditor id|pin\b|password|passwort")
_REF_NO_TYPES = {"payslip", "tax_document", "other"}
_REF_TYPEWORDS = {   # the document's own name, followed by its number: "Angebot A-2025-31", "OFFER Q-2025-322"
    "invoice": r"[a-z]{0,12}rechnung|invoice|gutschrift|credit note", "receipt": r"quittung|kassenbeleg|kassenbon|receipt|beleg|rechnung",
    "quote": r"angebot|quote|quotation|offer|estimate|kostenvoranschlag|kostenschatzung|pro[- ]?forma(?:[- ]?rechnung|[- ]?invoice)?|offerte", 
    "reminder": r"mahnung|zahlungserinnerung|reminder|final notice|erinnerung", "contract": r"vertrag|rahmenvertrag|contract|agreement",
    "bank_statement": r"kontoauszug|statement"}
_REF_TYPEWORD_RX = {t: re.compile(r"(?<![a-z])(?:" + w + r")\s+(?:nr\.?|no\.?|#)?\s*((?:[a-z]{1,6}[-/]){0,3}\d[\w\-/]{2,24})")
                    for t, w in _REF_TYPEWORDS.items()}


_IBAN_LENGTH = {"DE": 22, "GB": 22, "FR": 27, "AE": 23, "PK": 24, "CH": 21, "AT": 20, "NL": 18, "ES": 24, "IT": 27, "BE": 16, "IE": 22,
                "LU": 20, "PT": 25, "SE": 24, "DK": 18, "NO": 15, "PL": 28, "SA": 24, "TR": 26, "FI": 18, "GR": 27, "CZ": 24}


def _iban_like(compact: str) -> bool:
    """An IBAN, not just something shaped like one: the length of its country, or a valid check number (mod 97)."""
    c = compact.upper()
    if not _RX_IBAN_STRICT.fullmatch(c):
        return False
    if _IBAN_LENGTH.get(c[:2]) == len(c):
        return True
    try:
        return int("".join(str(int(ch, 36)) for ch in c[4:] + c[:4])) % 97 == 1
    except ValueError:
        return False


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _safe_reference(value: str, context: str) -> bool:
    """Never an IBAN, a card number, a personal / tax / passport / permit number or a date or amount."""
    from .redact import _RX_IBAN, redact_count
    v = value.strip()
    if not (3 <= len(v) <= 36) or not re.search(r"\d", v):
        return False
    compact = re.sub(r"[\s-]", "", v)
    if _RX_IBAN.search(v) or _iban_like(compact):
        return False
    if compact.isdigit() and 13 <= len(compact) <= 19 and _luhn(compact):
        return False
    if re.fullmatch(r"(?:19|20)\d{2}", v) or _MONEY.fullmatch(v) and re.fullmatch(r"[\d.,]+", v) and re.search(r"[.,]\d{2}$", v):
        return False
    if _D_ISO.fullmatch(v) or _D_NUM.fullmatch(v):
        return False
    if _REF_CONTEXT_BAD.search(_fold(context)):
        return False
    at = context.find(v)
    if at >= 0:
        run = re.match(r"(?:\d[ -]?){13,19}", context[at:])
        if run and _luhn(re.sub(r"\D", "", run.group(0))):
            return False                        # the first block of a card number written with spaces
    red, n = redact_count(context)
    if n and (v not in red):
        return False
    red_v, n_v = redact_count(v)
    return n_v == 0


def _extract_reference(raw_lines: list[str], llines: list[str], doctype: str, ext: str = "") -> str | None:
    """The document's own number. A number it only cites (the invoice a reminder is about) is not it."""
    if doctype in _REF_NO_TYPES:
        return None
    own = _REF_OWN.get(doctype, ())
    found = []                                                         # (priority, line, position, value)
    for li, low in enumerate(llines[:200]):
        if not low.strip():
            continue
        for m in _REF_LABEL.finditer(low):
            kind = m.lastgroup
            if kind == "ord" and doctype not in ("invoice", "quote", "receipt"):
                continue
            if kind in own:
                prio = 0
            elif kind == "gen" and doctype != "bank_statement" and li < 40:
                prio = 1
            elif kind == "ord" and doctype in ("invoice", "quote", "receipt"):
                prio = 2
            else:
                continue
            value = None
            vm = _REF_VALUE.match(raw_lines[li], m.end())
            if vm:
                value = vm.group(1).rstrip(".-/_")
            if value is None or _fold(value) in {"no", "nr", "number", "nummer", "datum", "date"} or _DATE_LABELS.fullmatch(_fold(value)):
                # label at the end of a header row: take the value of the same column in the next line
                value = _column_value(raw_lines, llines, li, m)
                if value is None:
                    continue
            ctx = raw_lines[li][max(0, m.start() - 30):m.end() + len(value) + 20]
            if _safe_reference(value, ctx):
                found.append((prio, li, m.start(), value))
    head = [(i, l) for i, l in enumerate(raw_lines) if l.strip()][:10]
    if not found:
        for i, l in head:                                              # "No. PI-5532" under the heading, in a column of its own
            for seg in re.split(r"\s{2,}", l.strip()):
                bm = _REF_BARE.match(_fold(seg).strip())
                if bm:
                    v = seg.strip()[bm.start(1):bm.end(1)]
                    if _safe_reference(v, l):
                        found.append((1, i, 0, v))
                        break
            if found:
                break
    if not found:
        # a label on one line, its value on the next ("Nr." / "NL-26-00418", "Advice no." / "RA-2026-0331")
        nonempty = [(i, l) for i, l in enumerate(raw_lines) if l.strip()][:30]
        for (i, l), (_, nxt) in zip(nonempty, nonempty[1:]):
            fl = _fold(l).strip(" :.")
            if len(fl) <= 30 and re.search(r"(?:^|\s)(?:no|nr|number|nummer)$", fl) and not _REF_FOREIGN_LABEL.search(fl):
                v = nxt.strip()
                if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/_.\-]{2,34}[A-Za-z0-9]", v) and _safe_reference(v, l + " " + nxt):
                    found.append((1, i, 0, v))
                    break
    rx = _REF_TYPEWORD_RX.get(doctype)
    if not found and rx:
        # "Angebot A-2025-31", "Rechnung 2025-77" in the heading or the subject line
        for li, l in head + [(i, x) for i, x in enumerate(raw_lines[:12]) if x[:8].lower() == "subject:"]:
            m = rx.search(_fold(l))
            if not m:
                continue
            value = l[m.start(1):m.end(1)].rstrip(".-/_")
            if (len(value) >= 4 and (not value.isdigit() or len(value) >= 5) and not re.fullmatch(r"\d{1,2}/\d{4}|\d{4}/\d{1,2}", value)
                    and _safe_reference(value, l[max(0, m.start() - 20):m.end() + 20])):
                found.append((3, li, m.start(), value))
                break
    if not found:
        return None
    found.sort(key=lambda t: (t[0], t[1], t[2]))
    return found[0][3]


def _column_value(raw_lines, llines, li, m) -> str | None:
    """A header row ("Rechnungsnummer  Rechnungsdatum  Fällig") above a value row ("RE-1042  14.03.2025  13.04.2025"):
    the value under this label, when the number of columns agrees."""
    header = llines[li]
    labels = [mm.start() for mm in _REF_LABEL.finditer(header)] + [mm.start() for mm in _DATE_LABELS.finditer(header)]
    labels = sorted(set(labels))
    nxt = None
    for j in range(li + 1, min(li + 3, len(raw_lines))):
        if raw_lines[j].strip():
            nxt = raw_lines[j]
            break
    if nxt is None:
        return None
    toks = nxt.split()
    if len(toks) != len(labels) or m.start() not in labels:
        return None
    v = toks[labels.index(m.start())].rstrip(".-/_")
    return v if re.search(r"\d", v) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/_.\-]{1,34}[A-Za-z0-9]", v) else None


# Legal forms of companies in many countries. Owners add the ones of their own country in the configuration
# (docmeta.extra_org_suffixes). Short forms that are also ordinary words are only taken at the end of a name.
_ORG_SUFFIXES = (r"GmbH(?: & Co\.? ?KG)?", r"mbH", r"AG", r"KG", r"OHG", r"GbR", r"e\.? ?K\.", r"eG", r"e\.? ?V\.", r"SE",
                 r"Ltd\.?", r"Limited", r"PLC", r"plc", r"LLP", r"LLC", r"L\.L\.C\.", r"Inc\.?", r"Incorporated", r"Corp\.?",
                 r"Corporation", r"Co\.(?: Ltd\.?)?", r"S\.A\.S\.", r"SAS", r"S\.A\.", r"SA", r"SARL", r"S\.à ?r\.l\.", r"Sàrl",
                 r"B\.V\.", r"BV", r"N\.V\.", r"NV", r"AB", r"A/S", r"AS", r"ApS", r"Oy", r"S\.p\.A\.", r"SpA", r"S\.r\.l\.",
                 r"Srl", r"S\.L\.", r"SL", r"Pty\.? ?Ltd\.?", r"Pvt\.? ?Ltd\.?", r"Pte\.? ?Ltd\.?",
                 r"Sdn\.? ?Bhd\.?")
_AMBIGUOUS_SUFFIX = {"SA", "SL", "AB", "AS", "SE", "Oy", "NV", "BV", "SAS", "Co."}
_suffix_cache: dict = {}


def _org_suffix_rx(extra: tuple = ()) -> re.Pattern:
    key = tuple(extra)
    rx = _suffix_cache.get(key)
    if rx is None:
        forms = list(_ORG_SUFFIXES) + [re.escape(x) for x in key]
        forms += [f.upper() for f in forms]                  # a letterhead in capitals
        rx = _suffix_cache[key] = re.compile(r"(?<![\w])(" + "|".join(dict.fromkeys(forms)) + r")(?![\w])")
    return rx


_NAME_TOKEN = re.compile(r"^[A-ZÄÖÜ0-9&][\w&'’.\-]*$")
_NAME_JOIN = {"&", "und", "and", "of", "+", "de", "von", "for", "für"}
_BILLTO = re.compile(r"(?:^an (?:die|den|das|herrn|frau)\b|^herrn?\b|^frau\b|^to\b|consignee|importer|bill(?:ed)? to|invoice to|ship(?:ped)? to|deliver to|sold to|rechnung an|rechnungsempf\w+|lieferanschrift|kunde|customer|"
                     r"attn|an:|empf\w+|client|buyer|besteller|auftraggeber|to:)")
_SENDER_WORDS = (r"absender|sender|issued by|seller|sold by|vendor|supplier|lieferant|verkaufer|rechnungssteller|aussteller|"
                 r"arbeitgeber|employer|from|von")
_SENDER_LABEL = re.compile(r"^\s*(?:" + _SENDER_WORDS + r")\s*[:\-]\s*(\S.*)")
_SENDER_ONLY = re.compile(r"^\s*(?:" + _SENDER_WORDS + r")\s*[:\-]?\s*$")
_STREET = re.compile(r"\d.*(?:str\b|stra|weg\b|platz|allee|gasse|ring\b|damm|ufer|street|road|\brd\b|avenue|\bave\b|lane|drive|boulevard|"
                     r"way\b|court|square|postfach|po box|p\.o\.|suite|floor|rue\b|via\b)|\b\d{4,5}\s+[a-z]|\b[a-z]{1,2}\d{1,2}\s?\d[a-z]{2}\b|"
                     r"(?:str\b|stra|weg\b|platz|allee|gasse|street|road|avenue|lane|drive|boulevard|way\b|court|square)\w*\.?\s+\d")
_AUTHORITY = re.compile(r"\b(?:tax office|tax authority|steueramt|steuerverwaltung|finanzverwaltung|revenue (?:service|office|agency)|finance office|"
                        r"inland revenue)\b")
_BANKISH = re.compile(r"\b(?:bank|credit union|sparkasse|volksbank|building society|savings)\b")
_GENERIC_NAMES = {"invoice", "rechnung", "receipt", "quittung", "angebot", "quote", "vertrag", "contract", "agreement", "mahnung",
                  "reminder", "kontoauszug", "statement", "page", "seite", "datum", "date", "dear", "sehr", "hallo", "betreff", "subject",
                  "total", "summe", "gesamt", "bank", "tax", "steuer", "lohnabrechnung", "payslip", "brief", "letter", "bill", "scan", "copy",
                  "bar", "cash", "card", "kasse", "beleg", "recommendation", "notes", "notizen", "agenda", "protokoll", "minutes",
                  "quotation", "proforma", "gutschrift", "credit", "debit", "konto", "account", "address", "adresse", "to", "an",
                  "betrag", "amount", "erhalten", "fur", "for"}
_FREE_MAIL = {"gmail", "googlemail", "outlook", "hotmail", "live", "yahoo", "gmx", "web", "icloud", "me", "mac", "protonmail", "proton",
              "aol", "t-online", "freenet", "mail", "msn", "yandex", "zoho", "example"}


def _clean_party(s: str) -> str | None:
    s = re.sub(r"\s+", " ", s or "").strip(" \t,;:-|·•\"'<>()[]")
    s = re.sub(r"\s+(?:via|über|ueber|through|by)\s+.*$", "", s, flags=re.I) if len(s) > 12 else s
    if not (2 <= len(s) <= 80):
        return None
    if _fold(s) in _GENERIC_NAMES or "@" in s or re.search(r"https?://|www\.", s, re.I):
        return None
    if re.search(r"\d{5,}", s):
        return None
    from .redact import redact
    if redact(s) != s:
        return None
    return s


_ORG_SUFFIX_RX_ANY = _org_suffix_rx(())


def _org_in_line(line: str, rx: re.Pattern) -> str | None:
    for m in rx.finditer(line):
        if m.group(1).title() in {x.title() for x in _AMBIGUOUS_SUFFIX} and re.match(r"\s*[\w]", line[m.end():m.end() + 2]):
            continue                                        # "AS IS", "SA 5": a word, not a legal form
        toks = line[:m.start()].split()
        name = []
        for tok in reversed(toks):
            if _NAME_TOKEN.match(tok) or tok in _NAME_JOIN:
                name.append(tok)
                if len(name) >= 6:
                    break
            else:
                break
        while name and name[-1] in _NAME_JOIN:           # a leading "und"/"of" is not part of the name
            name.pop()
        if not name:
            continue
        cand = " ".join(reversed(name)) + " " + m.group(1)
        # a sentence fragment ("Rechnung an Beispiel GmbH") keeps only the capitalised tail, which the walk already did
        if _fold(name[-1]).strip(".:") in _GENERIC_NAMES and len(name) == 1:
            continue
        return cand
    return None


def _party_from_email(raw_lines: list[str]) -> str | None:
    for l in raw_lines[:15]:
        if l[:5].lower() == "from:":
            name, addr = parseaddr(l[5:].strip())
            name = re.sub(r"\s+", " ", name or "").strip().strip("\"'")
            if name and "@" not in name and _fold(name) not in ("noreply", "no-reply", "no reply", "donotreply", "info", "billing", "support"):
                return _clean_party(name)
            dom = addr.split("@")[-1].lower() if "@" in addr else ""
            labels = [x for x in dom.split(".") if x]
            if len(labels) >= 2:
                core = labels[:-1]
                if core and core[-1] in ("co", "com", "org", "net", "gov", "ac") and len(core) >= 2:
                    core = core[:-1]
                stem = core[-1] if core else ""
                if stem and stem not in _FREE_MAIL:
                    return _clean_party(stem.replace("-", " ").replace("_", " ").title())
            return None
    return None


def _party_from_filename(filename: str, doctype: str) -> str | None:
    base, _ = _split_name(filename)
    toks = [t for t in re.split(r"[\s_\-.]+", base) if t]
    stop = set(_GENERIC_NAMES) | {t for s, w in _NAME.values() for t in (s | w)} | {"final", "neu", "new", "copy", "kopie", "scan", "img", "doc", "pdf", "no", "nr"}
    keep, best = [], []
    for t in toks:
        f = _fold(t)
        if re.fullmatch(r"[\d]+|(?:19|20)\d\d|q[1-4]|re|inv", f) or f in stop or re.fullmatch(r"\d.*", f) or len(f) < 2:
            if keep:
                best = best if len(best) >= len(keep) else keep
                keep = []
            continue
        keep.append(t)
    best = best if len(best) >= len(keep) else keep
    if 2 <= len(best) <= 4 and all(len(x) >= 3 for x in best):
        return _clean_party(" ".join(w.capitalize() if w.islower() or w.isupper() else w for w in best))
    return None


def _segment(line: str) -> str:
    """The first block of a letterhead line: columns, bullets and bars cut it ("Muster Grafik  Inh. Anna Muster")."""
    line = line.strip().strip('"\'')
    parts = re.split(r" [·|•] ", line)
    first = parts[0]
    for nxt in parts[1:]:                                   # "Stadt Musterhausen · Bürgeramt" is one name, "X GmbH · Weg 1" is not
        if re.search(r"\d", nxt) or _STREET.search(_fold(nxt)) or len(nxt) > 40:
            break
        first += " · " + nxt
    first = re.split(r"\s{2,}|\t|;", first, maxsplit=1)[0].strip().strip('",\' ')
    return re.sub(r"\s+(?:inh\.|inhaber)\b.*$", "", first, flags=re.I).strip()


def _trim_address(s: str) -> str:
    """"Muster Gartenservice, Musterweg 4, 10115 Musterstadt" -> "Muster Gartenservice"."""
    return re.split(r",\s*(?=[^,]*\d)", s, maxsplit=1)[0].strip()


def _nameish(seg: str) -> bool:
    words = seg.split()
    fs = _fold(seg)
    return (2 <= len(seg) <= 50 and 1 <= len(words) <= 5 and not re.search(r"\d|@|www\.|http|\btel\b|\bfax\b|:$", fs)
            and not any(rx.search(_tl(fs)) for rx in _TITLE.values())
            and bool(re.match(r"^[A-ZÄÖÜ]", seg)) and fs.split()[0].strip(".:,") not in _GENERIC_NAMES and not _BILLTO.search(fs)
            and not _DATE_LABELS.search(fs) and sum(1 for w in words if w[:1].isupper() or w in _NAME_JOIN) >= max(1, len(words) - 1)
            and (len(seg) >= 4 or len(words) > 1))


def _letter_sender(raw_lines: list[str]) -> str | None:
    """A letter signed with a person's name, whose address block stands at the top: that person sends it, whatever
    organisation's block comes first (the recipient)."""
    lines = [l.strip() for l in raw_lines]
    sig = None
    for i, l in enumerate(lines):
        if l and _RX_CLOSING.search(_tl(_fold(l))):
            for nxt in lines[i + 1:i + 4]:
                if nxt:
                    seg = _segment(nxt)
                    if _nameish(seg) and not _ORG_SUFFIX_RX_ANY.search(seg):
                        sig = seg
                    break
            if sig:
                break
    if not sig:
        return None
    nonempty = [(i, l) for i, l in enumerate(lines) if l][:30]
    for k, (i, l) in enumerate(nonempty[:-1]):
        if _fold(_segment(l)) == _fold(sig) and _STREET.search(_fold(nonempty[k + 1][1])):
            return sig
    return None


def _extract_party(raw_lines: list[str], doctype: str, is_email: bool, filename: str, own_names: tuple,
                   extra_suffixes: tuple = (), ext: str = "") -> str | None:
    org_rx = _org_suffix_rx(extra_suffixes)
    own = [_fold(n) for n in own_names if n]
    ok = lambda s: bool(s) and not any(o and (o in _fold(s) or _fold(s) in o) for o in own)
    if is_email:
        p = _party_from_email(raw_lines)
        if ok(p):
            return p
    tabular = ext in ("csv", "tsv")
    plain_other = doctype == "other" and not is_email
    head = [(i, l) for i, l in enumerate(raw_lines) if l.strip()][:16]
    reach = 3 if (plain_other or tabular) else 16          # a letterhead is at the top; a note or a data table has none below
    if tabular:
        # a data export: the first cell of the first lines, when it names a bank or an organisation
        for i, l in head[:2]:
            cell = _segment(re.sub(r'"\s*,\s*"', ";", l))
            fc = _fold(cell)
            org = _org_in_line(cell, org_rx) if _ORG_SUFFIX_RX_ANY.search(cell) else None
            if (_BANKISH.search(fc) and _nameish(cell)) or org:
                p = _clean_party(org or cell)
                if ok(p):
                    return p
        return None
    # a labelled sender (the name on the same line, or on the next one)
    for k, (i, l) in enumerate(head[:reach]):
        sm = _SENDER_LABEL.search(_fold(l) + " ") or None
        if sm is None:
            only = _SENDER_ONLY.match(_fold(l))
            if only and k + 1 < len(head):
                raw = head[k + 1][1]
                org = _org_in_line(raw, org_rx) or _trim_address(_segment(raw))
                org = _clean_party(org)
                if ok(org):
                    return org
            continue
        if sm:
            raw = l[sm.start(1):sm.end(1)]
            org = _org_in_line(raw, org_rx) or _trim_address(_segment(raw))
            org = _clean_party(org)
            if ok(org):
                return org
    # a tax office is the sender of its notices
    if doctype == "tax_document":
        for i, l in head[:8]:
            if _AUTHORITY.search(_fold(l)) and not _BILLTO.search(_fold(l)[:12]):
                p = _clean_party(_segment(l))
                if ok(p):
                    return p
    # a bank is the sender of its statements, not the account holder named further down
    if doctype == "bank_statement":
        for i, l in head[:10]:
            seg = _segment(l)
            if _BANKISH.search(_fold(seg)) and len(seg.split()) <= 6 and not re.search(r"\d|@", seg) and not _BILLTO.search(_fold(seg)):
                p = _clean_party(_org_in_line(seg, org_rx) or seg)
                if ok(p):
                    return p
    if doctype == "letter":
        p = _letter_sender(raw_lines)
        if ok(p):
            return p
    # name and address at the top: a private sender, or a business letterhead without a legal form
    if len(head) >= 2:
        seg = _segment(head[0][1])
        if not org_rx.search(seg) and _nameish(seg) and _STREET.search(_fold(head[1][1])):
            p = _clean_party(seg)
            if ok(p):
                return p
    skip_until = -1
    for i, l in head[:reach]:
        fl = _fold(l)
        if _BILLTO.search(fl[:40]):
            skip_until = i + 3
            continue
        if i <= skip_until:
            continue
        org = _org_in_line(l[:300], org_rx)
        if org and plain_other:
            tail = l[l.find(org) + len(org):] if org in l else ""
            if re.match(r"\s+[a-zäöüß]", tail):
                org = None                      # "Sample Trading Ltd was mentioned": a sentence, not a letterhead
        org = _clean_party(org) if org else None
        if ok(org):
            return org
    if plain_other:
        return None
    # a letterhead without a legal form: the first short, name-like line
    for i, l in head[:3]:
        seg = _segment(l)
        if _nameish(seg) and not _RX_PRICE_END.search(l.strip()) and not re.match(r"^\w+\s*:", l.strip()):
            p = _clean_party(seg)
            if ok(p):
                return p
    p = _party_from_filename(filename, doctype)
    return p if ok(p) else None


# ------------------------------------------------------------------ fields

def _email_date(raw_lines: list[str], today: date) -> str | None:
    for l in raw_lines[:15]:
        if l[:5].lower() == "date:":
            try:
                dt = parsedate_to_datetime(l[5:].strip())
            except (TypeError, ValueError, IndexError):
                return None
            if dt is None:
                return None
            d = dt.date()
            return d.isoformat() if date(1980, 1, 1) <= d <= today + timedelta(days=1) else None
    return None


def extract_fields(text: str, doctype: str, lang: str | None = None, *, filename: str = "", ext: str = "",
                   meta: dict | None = None, today: date | None = None, own_names: tuple | list = (),
                   extra_org_suffixes: tuple | list = ()) -> dict:
    """{doc_date, amount, currency, counterparty, reference}. Every value may be None. `today` makes "never in the
    future" testable. A document of type `other` only gets the fields that are safe without a type: a labelled date and,
    for a mail, the sender."""
    meta = meta or {}
    today = today or date.today()
    ext = (ext or "").lower().lstrip(".")
    raw = _prep(text)
    low = _fold(raw)
    if ext in _OCR_EXT:
        low = _ocr_fix(low)
    raw_lines, llines = raw.split("\n"), low.split("\n")
    is_email = ext in ("eml", "emlx") or meta.get("kind") == "email"
    out = {"doc_date": None, "amount": None, "currency": None, "counterparty": None, "reference": None}
    if _MRZ.search(raw):
        return out
    prefer_mdy = ("$" in raw or "usd" in low) and (lang or "") != "de" and not any(k in low for k in ("eur", "€", "gbp", "£"))
    if is_email:
        out["doc_date"] = _email_date(raw_lines, today)
    if out["doc_date"] is None:
        cands = _date_candidates(llines, today, prefer_mdy)
        out["doc_date"] = _pick_date(cands, doctype, len([l for l in llines if l.strip()]),
                                     ext in ("csv", "tsv", "xlsx", "xlsm", "xls", "ods"))
    out["counterparty"] = _extract_party(raw_lines, doctype, is_email, filename, tuple(own_names or ()),
                                         tuple(extra_org_suffixes or ()), ext)
    if doctype != "other":
        out["amount"], out["currency"] = _extract_amount(llines, doctype, ext, lang)
        out["reference"] = _extract_reference(raw_lines, llines, doctype, ext)
    return out


def analyse(text: str, filename: str = "", ext: str = "", meta: dict | None = None, *, today: date | None = None,
            own_names: tuple | list = (), prior: dict | None = None, extra_org_suffixes: tuple | list = (),
            extra_keywords: dict | None = None) -> dict:
    """The doc_meta row for one document: {doctype, confidence, doc_date, amount, currency, counterparty, reference,
    lang}. An empty text gives doctype None (not classified yet)."""
    meta = dict(meta or {})
    raw = _prep(text)
    if not raw.strip():
        return {"doctype": None, "confidence": None, "doc_date": None, "amount": None, "currency": None,
                "counterparty": None, "reference": None, "lang": None}
    lang = detect_lang(raw[:4000])
    lang = lang if lang in ("en", "de") else None
    doctype, conf, detail = classify_detail(raw, filename, ext, meta, prior, extra_keywords)
    if detail.get("identity"):
        return {"doctype": "other", "confidence": conf, "doc_date": None, "amount": None, "currency": None,
                "counterparty": None, "reference": None, "lang": lang}
    f = extract_fields(raw, doctype, lang, filename=filename, ext=ext, meta=meta, today=today, own_names=own_names,
                       extra_org_suffixes=extra_org_suffixes)
    return {"doctype": doctype, "confidence": conf, "lang": lang, **f}


# ------------------------------------------------------------------ eligibility and settings

_PLAIN_CLASSIFIABLE = ("txt", "text", "md", "markdown", "csv", "tsv", "html", "htm", "rst")
_OFFICE_SKIP = ("pptx", "pptm", "odp", "key", "ppt", "odg")
_CLASSIFIABLE_KINDS = ("pdf", "office", "email", "image")


def eligible(kind: str | None, ext: str | None) -> bool:
    """Files worth classifying: documents, mails (not whole mailboxes), scans; plain text and notes only by their usual
    extensions (source code, configuration and data files never)."""
    ext = (ext or "").lower().lstrip(".")
    if kind == "text":
        return ext in _PLAIN_CLASSIFIABLE
    if kind == "email":
        return ext != "mbox"
    if kind == "office":
        return ext not in _OFFICE_SKIP
    return kind in _CLASSIFIABLE_KINDS


def eligible_sql(alias: str = "f.") -> str:
    plain = ",".join(f"'{e}'" for e in _PLAIN_CLASSIFIABLE)
    skip = ",".join(f"'{e}'" for e in _OFFICE_SKIP)
    return (f"(({alias}kind IN ('pdf','image')) OR ({alias}kind='office' AND {alias}ext NOT IN ({skip}))"
            f" OR ({alias}kind='email' AND {alias}ext != 'mbox') OR ({alias}kind='text' AND {alias}ext IN ({plain})))")


_settings_cache: tuple[float, dict] | None = None


def settings() -> dict:
    """config docmeta: {enabled, own_names, extra_org_suffixes, extra_keywords}; cached for 30 s (the indexer asks once per file)."""
    global _settings_cache
    now = time.monotonic()
    if _settings_cache and now - _settings_cache[0] < 30:
        return _settings_cache[1]
    try:
        from . import config
        cfg = config.load().get("docmeta") or {}
        own = cfg.get("own_names") or []
        suff = cfg.get("extra_org_suffixes") or []
        kws = cfg.get("extra_keywords") or {}
        out = {"enabled": cfg.get("enabled", True) is not False,
               "own_names": tuple(str(n).strip() for n in own if str(n).strip()) if isinstance(own, list) else (),
               "extra_org_suffixes": tuple(str(n).strip() for n in suff if str(n).strip()) if isinstance(suff, list) else (),
               "extra_keywords": {t: [str(k) for k in v if str(k).strip()] for t, v in kws.items() if isinstance(v, list)}
               if isinstance(kws, dict) else {}}
    except Exception:
        out = {"enabled": True, "own_names": (), "extra_org_suffixes": (), "extra_keywords": {}}
    _settings_cache = (now, out)
    return out


def reset_settings_cache() -> None:
    global _settings_cache
    _settings_cache = None


# ------------------------------------------------------------------ database

_COLS = ("doctype", "confidence", "doc_date", "amount", "currency", "counterparty", "reference", "lang")


def has_table(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='doc_meta'").fetchone() is not None


def upsert(conn, file_id: int, row: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO doc_meta(file_id, doctype, confidence, doc_date, amount, currency, counterparty, reference,"
        " lang, extractor_version, extracted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (file_id, *(row.get(c) for c in _COLS), EXTRACTOR_VERSION, time.time()))


def delete(conn, file_id: int) -> None:
    conn.execute("DELETE FROM doc_meta WHERE file_id=?", (file_id,))


def get(conn, file_id: int) -> dict | None:
    if not has_table(conn):
        return None
    r = conn.execute("SELECT * FROM doc_meta WHERE file_id=?", (file_id,)).fetchone()
    return dict(r) if r else None


def text_from_items(items: list[dict]) -> str:
    """The text of a freshly extracted file (store.add_items rows), as the backfill would read it from the database."""
    parts, n = [], 0
    for it in items:
        if it.get("modality") not in ("text", "ocr") or not it.get("text"):
            continue
        parts.append(it["text"])
        n += len(it["text"])
        if n >= MAX_CHARS:
            break
    return "\n".join(parts)[:MAX_CHARS]


def file_text(conn, file_id: int) -> str:
    parts, n = [], 0
    for r in conn.execute("SELECT text FROM items WHERE file_id=? AND modality IN ('text','ocr') AND text IS NOT NULL"
                          " ORDER BY ord LIMIT 80", (file_id,)):
        parts.append(r[0])
        n += len(r[0])
        if n >= MAX_CHARS:
            break
    return "\n".join(parts)[:MAX_CHARS]


def compute_for_file(conn, f) -> bool:
    """Compute and store the row of one `files` row (id, path, ext, kind, title, lang, pages). Never raises. Returns
    whether a row was written. A text-less file gets a stub (doctype NULL): not classified, not retried until OCR."""
    st = settings()
    try:
        text = file_text(conn, f["id"])
        meta = {"title": f["title"], "kind": f["kind"], "lang": f["lang"], "pages": f["pages"]}
        row = analyse(text, f["path"], f["ext"] or "", meta, own_names=st["own_names"],
                      extra_org_suffixes=st["extra_org_suffixes"], extra_keywords=st["extra_keywords"])
        upsert(conn, f["id"], row)
        return True
    except Exception:
        try:
            upsert(conn, f["id"], {})
            return True
        except Exception:
            return False


def store_light(conn, file_id: int, rel: str, prep: dict, full: bool, error: str | None) -> None:
    """The indexer hook, after one file's light extraction. Never raises: a classifier problem must not cost a scan its
    transaction. Metadata-only, failed and ineligible files hold no derived data. A scan waiting for OCR leaves the row
    to the backfill, which runs once the OCR text is there."""
    try:
        st = settings()
        if not st["enabled"]:
            return
        if not full or error or not eligible(prep.get("kind"), prep.get("ext")):
            delete(conn, file_id)
            return
        heavy = prep.get("heavy") or {}
        if prep.get("kind") == "image" or (isinstance(heavy, dict) and heavy.get("ocr")):
            delete(conn, file_id)
            return
        text = text_from_items(prep.get("items") or [])
        meta = {"title": prep.get("title"), "kind": prep.get("kind"), "lang": prep.get("lang"), "pages": prep.get("pages")}
        upsert(conn, file_id, analyse(text, rel, prep.get("ext") or "", meta, own_names=st["own_names"],
                                      extra_org_suffixes=st["extra_org_suffixes"], extra_keywords=st["extra_keywords"]))
    except Exception:
        try:
            delete(conn, file_id)
        except Exception:
            pass


def _candidates_sql(force: bool, cols: str = "f.id, f.path, f.ext, f.kind, f.title, f.lang, f.pages") -> str:
    stale = "" if force else (" AND (d.file_id IS NULL OR d.extractor_version < ?"
                              " OR (f.heavy_at IS NOT NULL AND f.heavy_at > d.extracted_at"
                              " AND EXISTS (SELECT 1 FROM items i WHERE i.file_id=f.id AND i.modality='ocr')))")
    return (f"SELECT {cols} FROM files f"
            " LEFT JOIN doc_meta d ON d.file_id = f.id WHERE f.mode='full' AND f.state != 'error'"
            " AND f.missing_since IS NULL AND f.heavy IS NULL AND " + eligible_sql("f.") + stale)


def backfill(conn, budget: int, cancel=None, batch: int = 100) -> int:
    """Classify up to `budget` files that have no row, an older extractor version, or OCR text newer than their row.
    Commits every `batch` files. Files whose OCR has not run yet (files.heavy set) wait."""
    if budget <= 0 or not settings()["enabled"] or not has_table(conn):
        return 0
    done = 0
    while done < budget:
        if cancel and cancel():
            break
        rows = conn.execute(_candidates_sql(False) + " ORDER BY f.id LIMIT ?",
                            (EXTRACTOR_VERSION, min(batch, budget - done))).fetchall()
        if not rows:
            break
        wrote = sum(1 for r in rows if compute_for_file(conn, r))
        conn.commit()
        done += len(rows)
        if wrote == 0:
            break                                               # the database refuses: do not spin
    return done


REINDEX: dict[str, dict] = {}
_reindex_lock = threading.Lock()


def reindex(name: str, cancel=None, progress=None, batch: int = 200) -> dict:
    """Redo every eligible file of a collection, whatever its stored version. Takes the collection's write lock only
    for one batch at a time and commits after each, so a scan running beside it never waits long."""
    from . import store
    info = REINDEX[name] = {"state": "running", "done": 0, "total": 0, "started": time.time()}
    try:
        conn = store.connect(name, readonly=True)
        try:
            ids = [r[0] for r in conn.execute(_candidates_sql(True, "f.id") + " ORDER BY f.id")] if has_table(conn) else []
        finally:
            conn.close()
        info["total"] = len(ids)
        for i in range(0, len(ids), batch):
            if cancel and cancel():
                info["state"] = "cancelled"
                return info
            part = ids[i:i + batch]
            with store.write_lock(name):
                conn = store.connect(name)
                try:
                    q = ",".join("?" * len(part))
                    for r in conn.execute(f"SELECT id, path, ext, kind, title, lang, pages FROM files WHERE id IN ({q})", part).fetchall():
                        compute_for_file(conn, r)
                    conn.commit()
                finally:
                    conn.close()
            info["done"] = min(len(ids), i + batch)
            if progress:
                progress(info["done"], info["total"])
        info["state"] = "done"
    except Exception as e:
        info["state"] = "error"
        info["error"] = f"{type(e).__name__}: {e}"
    info["finished"] = time.time()
    return info


def start_reindex(name: str) -> dict:
    """reindex() in a thread (the daemon). A run already going on for the collection is returned as it is."""
    with _reindex_lock:
        cur = REINDEX.get(name)
        if cur and cur.get("state") == "running":
            return cur
        REINDEX[name] = {"state": "running", "done": 0, "total": 0, "started": time.time()}
        threading.Thread(target=reindex, args=(name,), daemon=True, name="docmeta-reindex").start()
        return REINDEX[name]


# ------------------------------------------------------------------ filters of the query language

def canon_doctype(v: str) -> str | None:
    """'invoice', 'Rechnungen', 'bank-statement' -> a doctype; unknown -> None. ('other' is only `doctype:other`: in
    type: it stays the file kind 'other'.)"""
    key = (v or "").strip().lower()
    return _ALIAS_TO_TYPE.get(key) or _ALIAS_TO_TYPE.get(key.replace("-", "_")) or _ALIAS_TO_TYPE.get(key.replace("_", "-"))


def split_types(values) -> tuple[set, list]:
    """The values of type: / doctype: filters -> ({doctypes}, [the rest: file kinds and extensions])."""
    types, rest = set(), []
    for v in values or []:
        for part in str(v).split(","):
            part = part.strip()
            if not part:
                continue
            t = canon_doctype(part)
            if t:
                types.add(t)
            else:
                rest.append(part)
    return types, rest


def doctype_values(filters: dict) -> set:
    """Doc types named by `doctype:` (all of them, `other` included) and by `type:` (all but `other`, which stays a file kind there)."""
    out = set()
    for v in filters.get("doctype", []):
        for part in str(v).split(","):
            part = part.strip().lower()
            if part == "other":
                out.add("other")
            elif part:
                t = canon_doctype(part)
                if not t:
                    raise ValueError(f"doctype: unknown document type '{part}' (use {', '.join(DOCTYPES)})")
                out.add(t)
    types, _ = split_types(filters.get("type", []))
    return out | types


def parse_amount(value: str) -> tuple:
    """'>500' '>=500' '<100' '<=100' '100..200' '500' -> (lo, lo_inclusive, hi, hi_inclusive). None = unbounded."""
    v = str(value).strip().replace(" ", "")
    m = re.fullmatch(r"(>=|<=|>|<|=)?(-?\d[\d.,'’]*)", v) if ".." not in v else None
    if m:
        n = parse_number(m.group(2))
        if n is None:
            raise ValueError(f"amount: '{value}' is not a number")
        op = m.group(1) or "="
        return {"=": (n, True, n, True), ">": (n, False, None, True), ">=": (n, True, None, True),
                "<": (None, True, n, False), "<=": (None, True, n, True)}[op]
    m = re.fullmatch(r"([-\d.,'’]*)\.\.([-\d.,'’]*)", v)
    if m and (m.group(1) or m.group(2)):
        a = parse_number(m.group(1)) if m.group(1) else None
        b = parse_number(m.group(2)) if m.group(2) else None
        if (m.group(1) and a is None) or (m.group(2) and b is None):
            raise ValueError(f"amount: '{value}' is not a range of numbers")
        if a is not None and b is not None and a > b:
            a, b = b, a
        return (a, True, b, True)
    raise ValueError(f"amount: '{value}' - use >500, >=500, <100, <=100, 100..200 or 500")


def amount_ok(amount, spec: tuple) -> bool:
    if amount is None:
        return False
    lo, lo_inc, hi, hi_inc = spec
    if lo is not None and lo == hi and lo_inc and hi_inc:
        return abs(amount - lo) < 0.005
    if lo is not None and (amount < lo or (amount == lo and not lo_inc)):
        return False
    if hi is not None and (amount > hi or (amount == hi and not hi_inc)):
        return False
    return True


def _period_start(s: str) -> str:
    s = s.strip()
    if re.fullmatch(r"\d{4}", s):
        return f"{s}-01-01"
    if re.fullmatch(r"\d{4}-\d{2}", s):
        return f"{s}-01"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    raise ValueError(f"docdate: '{s}' - use 2025, 2025-03, 2025-03-14 or a range 2025-01..2025-06")


def _period_end_excl(s: str) -> str:
    s = s.strip()
    start = date.fromisoformat(_period_start(s))
    if re.fullmatch(r"\d{4}", s):
        return date(start.year + 1, 1, 1).isoformat()
    if re.fullmatch(r"\d{4}-\d{2}", s):
        return (date(start.year + (start.month == 12), start.month % 12 + 1, 1)).isoformat()
    return (start + timedelta(days=1)).isoformat()


def parse_docdate(value: str) -> tuple[str | None, str | None]:
    """'2025' '2025-03' '2025-03-14' '2025-01..2025-06' '2025-01-01..' '..2025-06' -> (from inclusive, to exclusive), ISO days."""
    v = str(value).strip()
    try:
        if ".." in v:
            a, b = v.split("..", 1)
            if not a.strip() and not b.strip():
                raise ValueError
            lo = _period_start(a) if a.strip() else None
            hi = _period_end_excl(b) if b.strip() else None
            if lo and hi and lo >= hi:
                raise ValueError("docdate: the range ends before it starts")
            return lo, hi
        return _period_start(v), _period_end_excl(v)
    except ValueError as e:
        if str(e).startswith("docdate"):
            raise
        raise ValueError(f"docdate: '{value}' - use 2025, 2025-03, 2025-03-14 or a range 2025-01..2025-06") from None


def docdate_ok(doc_date: str | None, spec: tuple) -> bool:
    if not doc_date:
        return False
    eff = doc_date if len(doc_date) == 10 else doc_date + "-01"
    lo, hi = spec
    return (lo is None or eff >= lo) and (hi is None or eff < hi)


def range_to_docdate(after: str | None, before: str | None) -> str:
    """The search box's (after, before-exclusive) dates -> a docdate value, compact when it is a whole month or year."""
    lo = after or ""
    hi_incl = (date.fromisoformat(before) - timedelta(days=1)).isoformat() if before else ""
    if lo and hi_incl:
        a, b = date.fromisoformat(lo), date.fromisoformat(hi_incl)
        if a.month == 1 and a.day == 1 and b.month == 12 and b.day == 31 and a.year == b.year:
            return f"{a.year}"
        if a.day == 1 and (b + timedelta(days=1)).day == 1 and (a.year, a.month) == (b.year, b.month):
            return f"{a.year}-{a.month:02d}"
    return f"{lo}..{hi_incl}"


# ------------------------------------------------------------------ natural language -> filters

_NL_TYPES = [
    (r"bank statements?|account statements?|kontoausz[uü]ge?|kontoauszuege", "bank_statement"),
    (r"payslips?|pay slips?|lohnabrechnung(?:en)?|gehaltsabrechnung(?:en)?", "payslip"),
    (r"tax documents?|steuerbescheide?|steuerdokumente?", "tax_document"),
    (r"invoices?|rechnung(?:en)?", "invoice"),
    (r"receipts?|quittung(?:en)?|kassenbons?", "receipt"),
    (r"contracts?|vertr[aä]ge?|vertraege|agreements?", "contract"),
    (r"quotes?|quotations?|angebote?", "quote"),
    (r"reminders?|mahnung(?:en)?|dunning letters?", "reminder"),
    (r"letters?|briefe?", "letter"),
]
_NL_TYPE_RX = [(re.compile(rf"\b(?:{p})\b", re.I), t) for p, t in _NL_TYPES]
_NL_NUM = r"[\d][\d.,'’ ]*\d|\d"
_NL_CUR = r"€|\$|£|₹|euros?|us[- ]?dollars?|dollars?|pounds?|francs?|yen|" + "|".join(_ISO_CODES)
_NL_CUR_MAP = {**{c: c.upper() for c in _ISO_CODES}, "€": "EUR", "euro": "EUR", "euros": "EUR", "$": "USD", "dollar": "USD", "dollars": "USD",
               "us dollar": "USD", "us dollars": "USD", "us-dollar": "USD", "£": "GBP", "pound": "GBP", "pounds": "GBP", "₹": "INR",
               "franc": "CHF", "francs": "CHF", "yen": "JPY"}
_NL_OPS = [("gt", r"over|above|more than|greater than|exceeding|[uü]ber|mehr als|gr[oö](?:ß|ss)er als|ueber"),
           ("ge", r"at least|minimum|min\.?|mindestens|ab"),
           ("lt", r"under|below|less than|unter|weniger als|kleiner als"),
           ("le", r"at most|up to|maximum|max\.?|h[oö]chstens|bis zu|maximal")]
_NL_AMOUNT = re.compile(
    rf"\b(?P<op>{'|'.join(f'(?P<{k}>{v})' for k, v in _NL_OPS)})\s+(?P<c1>{_NL_CUR})?\s?(?P<num>{_NL_NUM})\s?(?P<c2>{_NL_CUR})?(?![\w])", re.I)
_NL_BETWEEN = re.compile(
    rf"\b(?:between|zwischen)\s+(?P<c0>{_NL_CUR})?\s?(?P<a>{_NL_NUM})\s?(?P<c1>{_NL_CUR})?\s+(?:and|und|to|bis)\s+(?P<c2>{_NL_CUR})?\s?(?P<b>{_NL_NUM})\s?(?P<c3>{_NL_CUR})?(?![\w])", re.I)
_NL_UNIT = re.compile(r"\s*(?:pages?|seiten?|degrees?|grad|km|cm|mm|kg|mb|gb|kb|minutes?|min\b|hours?|stunden?|days?|tage[n]?|years?|jahre[n]?|"
                      r"words?|wort|w[oö]rter|items?|st[uü]ck|people|personen|meters?|metres?|%|percent|prozent)\b", re.I)
_FILLER = {"show", "me", "find", "all", "my", "the", "of", "for", "with", "mit", "alle", "meine", "zeige", "zeig", "von", "für", "fur", "in", "im",
           "a", "an", "any", "list", "please", "bitte", "und", "and", "or", "documents", "docs", "files", "dokumente", "dateien", "und"}


def _nl_num(s: str) -> float | None:
    s = s.strip()
    return parse_number(s)


def nl_map(text: str) -> tuple[str, dict]:
    """A plain-language amount condition in a query -> filters. Conservative: it only fires on an explicit comparator
    (over / under / at least / between / über / unter / zwischen / mehr als ...) followed by a number, AND only when the
    number has a currency ("over 500 EUR") or the query names a kind of document ("invoices over 500"), so "temperature
    over 40 degrees" is never touched. When it fires, a document-type word becomes type:, a currency currency:, the rest
    of the query stays the words to search for. A year-like number without a currency is not an amount.
    Returns (remaining text, {filter key: [value]})."""
    has_type = any(rx.search(text) for rx, _ in _NL_TYPE_RX)
    filters: dict = {}
    t = text

    def cur_code(c: str | None) -> str | None:
        return _NL_CUR_MAP.get(re.sub(r"\s+", " ", c.lower())) if c else None

    m = _NL_BETWEEN.search(t)
    if m:
        a, b = _nl_num(m.group("a")), _nl_num(m.group("b"))
        cur = next((m.group(k) for k in ("c0", "c1", "c2", "c3") if m.group(k)), None)
        if a is not None and b is not None and (cur or (has_type and not (1900 <= a <= 2100 and 1900 <= b <= 2100))):
            filters["amount"] = [f"{min(a, b):g}..{max(a, b):g}"]
            if cur_code(cur):
                filters["currency"] = [cur_code(cur)]
            t = t[:m.start()] + " " + t[m.end():]
    else:
        m = _NL_AMOUNT.search(t)
        if m:
            n = _nl_num(m.group("num"))
            cur = m.group("c1") or m.group("c2")
            unit = None if cur else _NL_UNIT.match(t, m.end())
            if n is not None and not unit and (cur or (has_type and not (1900 <= n <= 2100))):
                op = ">" if m.group("gt") else ">=" if m.group("ge") else "<" if m.group("lt") else "<="
                filters["amount"] = [f"{op}{n:g}"]
                if cur_code(cur):
                    filters["currency"] = [cur_code(cur)]
                t = t[:m.start()] + " " + t[m.end():]
    if not filters:
        return text, {}
    for rx, typ in _NL_TYPE_RX:
        mt = rx.search(t)
        if mt:
            filters.setdefault("type", []).append(typ)
            t = t[:mt.start()] + " " + t[mt.end():]
            break
    return re.sub(r"\s+", " ", t).strip(), filters


def only_filler(text: str) -> bool:
    """True when nothing but filler words is left of a query once its filters were taken out."""
    words = re.findall(r"[\w'’-]+", text or "")
    return all(w.lower() in _FILLER for w in words)


# ------------------------------------------------------------------ views over the table

def present_sql(conn, alias: str = "f.") -> str:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(files)")}
    return f" AND {alias}missing_since IS NULL" if "missing_since" in cols else ""


def _months_between(a: str, b: str) -> list[str]:
    y, m = int(a[:4]), int(a[5:7])
    y2, m2 = int(b[:4]), int(b[5:7])
    out = []
    while (y, m) <= (y2, m2):
        out.append(f"{y:04d}-{m:02d}")
        y, m = y + (m == 12), m % 12 + 1
        if len(out) > 1200:
            raise ValueError("timeline: the range is longer than 100 years")
    return out


def _month_arg(v: str | None, end: bool) -> str | None:
    if not v:
        return None
    v = v.strip()
    if re.fullmatch(r"\d{4}", v):
        return f"{v}-12" if end else f"{v}-01"
    if re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])(-\d{2})?", v):
        return v[:7]
    raise ValueError(f"'{v}' is not a month: use 2025, 2025-03 or 2025-03-14")


def _types_arg(types) -> list[str]:
    if isinstance(types, str):
        types = [types]
    out = []
    for v in types or []:
        for part in str(v).split(","):
            part = part.strip().lower()
            if not part:
                continue
            t = "other" if part == "other" else canon_doctype(part)
            if not t:
                raise ValueError(f"unknown document type '{part}' (use {', '.join(DOCTYPES)})")
            if t not in out:
                out.append(t)
    return out


def facets(collections: list[str], types=None, top: int = 20) -> dict:
    """What the classified documents of these collections are: counts per type, the most frequent counterparties, per
    month, per currency. Collections without the table (an old database no writer has opened) count as empty."""
    from . import store
    tlist = _types_arg(types)
    dtypes: dict = {}
    parties: dict = {}
    months: dict = {}
    currencies: dict = {}
    classified = undated = total_files = 0
    for name in collections:
        try:
            conn = store.connect(name, readonly=True)
        except FileNotFoundError:
            continue
        try:
            total_files += conn.execute("SELECT COUNT(*) FROM files WHERE 1=1" + present_sql(conn, "")).fetchone()[0]
            if not has_table(conn):
                continue
            where = " WHERE dm.doctype IS NOT NULL" + present_sql(conn)
            args: list = []
            if tlist:
                where += f" AND dm.doctype IN ({','.join('?' * len(tlist))})"
                args += tlist
            base = f"FROM doc_meta dm JOIN files f ON f.id = dm.file_id{where}"
            for t, n in conn.execute(f"SELECT dm.doctype, COUNT(*) {base} GROUP BY dm.doctype", args):
                dtypes[t] = dtypes.get(t, 0) + n
                classified += n
            undated += conn.execute(f"SELECT COUNT(*) {base} AND dm.doc_date IS NULL", args).fetchone()[0]
            for p, n in conn.execute(f"SELECT dm.counterparty, COUNT(*) {base} AND dm.counterparty IS NOT NULL"
                                     " GROUP BY dm.counterparty", args):
                key = p.strip().lower()
                spell = parties.setdefault(key, {})
                spell[p] = spell.get(p, 0) + n
            for mth, n in conn.execute(f"SELECT substr(dm.doc_date,1,7), COUNT(*) {base} AND dm.doc_date IS NOT NULL GROUP BY 1", args):
                months[mth] = months.get(mth, 0) + n
            for c, n in conn.execute(f"SELECT dm.currency, COUNT(*) {base} AND dm.currency IS NOT NULL GROUP BY dm.currency", args):
                currencies[c] = currencies.get(c, 0) + n
        finally:
            conn.close()
    plist = sorted(((max(s, key=s.get), sum(s.values())) for s in parties.values()), key=lambda x: (-x[1], x[0].lower()))
    return {"collections": list(collections), "files": total_files, "classified": classified,
            "unclassified": max(0, total_files - classified), "undated": undated,
            "doctypes": [{"doctype": t, "count": n} for t, n in sorted(dtypes.items(), key=lambda kv: (-kv[1], kv[0]))],
            "counterparties": [{"name": n, "count": c} for n, c in plist[:top]],
            "months": [{"month": m, "count": n} for m, n in sorted(months.items())],
            "currencies": [{"currency": c, "count": n} for c, n in sorted(currencies.items(), key=lambda kv: (-kv[1], kv[0]))]}


def timeline(collections: list[str], types=None, date_from: str | None = None, date_to: str | None = None) -> dict:
    """Monthly buckets of the documents (count and the sum of the amounts per currency) AND the months in the range
    with none, so "which months have no receipts" is one call. Documents without a document date are not placed on the
    timeline; they are counted as `undated` (the file's own date is not a substitute: it is often just when it was saved).
    The range defaults to the first and last month that has a document."""
    from . import store
    tlist = _types_arg(types)
    lo, hi = _month_arg(date_from, False), _month_arg(date_to, True)
    counts: dict = {}
    sums: dict = {}
    undated = 0
    noamount: dict = {}
    for name in collections:
        try:
            conn = store.connect(name, readonly=True)
        except FileNotFoundError:
            continue
        try:
            if not has_table(conn):
                continue
            where = " WHERE dm.doctype IS NOT NULL" + present_sql(conn)
            args: list = []
            if tlist:
                where += f" AND dm.doctype IN ({','.join('?' * len(tlist))})"
                args += tlist
            base = f"FROM doc_meta dm JOIN files f ON f.id = dm.file_id{where}"
            undated += conn.execute(f"SELECT COUNT(*) {base} AND dm.doc_date IS NULL", args).fetchone()[0]
            for mth, n in conn.execute(f"SELECT substr(dm.doc_date,1,7), COUNT(*) {base} AND dm.doc_date IS NOT NULL GROUP BY 1", args):
                counts[mth] = counts.get(mth, 0) + n
            for mth, cur, tot, n in conn.execute(
                    f"SELECT substr(dm.doc_date,1,7), dm.currency, SUM(dm.amount), COUNT(dm.amount) {base}"
                    " AND dm.doc_date IS NOT NULL AND dm.amount IS NOT NULL GROUP BY 1, 2", args):
                key = cur or "?"
                sums.setdefault(mth, {})[key] = sums.setdefault(mth, {}).get(key, 0.0) + (tot or 0.0)
                noamount[mth] = noamount.get(mth, 0) + n
        finally:
            conn.close()
    first = min(counts) if counts else None
    last = max(counts) if counts else None
    start, end = lo or first, hi or last
    months = _months_between(start, end) if start and end else []
    buckets, gaps = [], []
    for mth in months:
        n = counts.get(mth, 0)
        qtype = ",".join(tlist)
        buckets.append({"month": mth, "count": n,
                        "amounts": {c: round(v, 2) for c, v in sorted(sums.get(mth, {}).items())},
                        "with_amount": noamount.get(mth, 0),
                        "query": (f"type:{qtype} " if qtype else "") + f"docdate:{mth}"})
        if n == 0:
            gaps.append(mth)
    return {"collections": list(collections), "types": tlist, "granularity": "month", "from": start, "to": end,
            "first": first, "last": last, "buckets": buckets, "gaps": gaps,
            "total": sum(b["count"] for b in buckets), "undated": undated}
