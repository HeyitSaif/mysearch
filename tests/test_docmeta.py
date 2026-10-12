"""Document understanding (mse/docmeta.py): the type of a document, its date, amount, counterparty and number, in English and
German, with fictional documents only. No database, no model, no network.
Run: .venv/bin/python -m unittest -v tests.test_docmeta"""

import json
import os
import re
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

os.environ.setdefault("MSE_HOME", tempfile.mkdtemp(prefix="mse-test-"))

from mse import docmeta, extract  # noqa: E402
from mse.redact import redact  # noqa: E402
from tests.docmeta_corpus import DEV, TODAY  # noqa: E402

T = date.fromisoformat(TODAY)
HELDOUT = Path(__file__).parent / "data" / "docmeta_heldout.json"
IBAN = "DE89 3704 0044 0532 0130 00"           # the public example IBAN of the standard


def fields(text, doctype, **kw):
    return docmeta.extract_fields(text, doctype, kw.pop("lang", None), today=kw.pop("today", T), **kw)


def analyse(text, filename="", ext="pdf", meta=None, **kw):
    return docmeta.analyse(text, filename, ext, meta, today=kw.pop("today", T), **kw)


class Numbers(unittest.TestCase):
    def test_german_and_english_formats(self):
        for s, want in (("1.234,56", 1234.56), ("1,234.56", 1234.56), ("1'234.50", 1234.5), ("1 234,56", 1234.56),
                        ("125,000", 125000.0), ("12,5", 12.5), ("0,99", 0.99), ("1.5", 1.5), ("1.234", 1234.0),
                        ("12.345.678,90", 12345678.9), ("1,234,567.89", 1234567.89), ("7", 7.0)):
            with self.subTest(s=s):
                self.assertEqual(docmeta.parse_number(s), want)

    def test_not_a_number(self):
        for s in ("", "abc", "1.2.3a", "--"):
            self.assertIsNone(docmeta.parse_number(s))

    def test_many_currencies_not_only_a_few(self):
        for code in ("JPY", "CAD", "SEK", "DKK", "NOK", "INR", "BRL", "AUD", "KRW", "MXN", "ZAR", "PLN"):
            self.assertEqual(docmeta.canon_currency(code), code)
            self.assertEqual(docmeta.canon_currency(code.lower()), code)
        self.assertEqual(docmeta.canon_currency("₹"), "INR")
        for word in ("and", "all", "try", "for", "the", "mad"):                      # ordinary words are never currencies
            self.assertIsNone(docmeta.canon_currency(word), word)
        f = docmeta.extract_fields("Invoice\nTotal SEK 18 750,00", "invoice", today=T)
        self.assertEqual((f["amount"], f["currency"]), (18750.0, "SEK"))
        f = docmeta.extract_fields("Receipt\nTOTAL CAD 7.35", "receipt", today=T)
        self.assertEqual((f["amount"], f["currency"]), (7.35, "CAD"))
        f = docmeta.extract_fields("Invoice\nTotal Amount ₹ 17,700.00", "invoice", today=T)
        self.assertEqual((f["amount"], f["currency"]), (17700.0, "INR"))
        self.assertEqual(docmeta.nl_map("invoices over 5000 JPY")[1], {"amount": [">5000"], "currency": ["JPY"], "type": ["invoice"]})
        self.assertEqual(docmeta.nl_map("receipts under 20 yen")[1]["currency"], ["JPY"])

    def test_currencies(self):
        for s, want in (("€", "EUR"), ("Euro", "EUR"), ("$", "USD"), ("US$", "USD"), ("£", "GBP"), ("SEK", "SEK"), ("chf", "CHF"),
                        ("CHF", "CHF"), ("₹", "INR"), ("yen", None)):
            self.assertEqual(docmeta.canon_currency(s), want, s)


class Amounts(unittest.TestCase):
    def amount(self, text, doctype="invoice", **kw):
        f = fields(text, doctype, **kw)
        return f["amount"], f["currency"]

    def test_german_gross_total_not_net_or_vat(self):
        t = "Nettobetrag 215,00 €\nMwSt 19 % 40,85 €\nGesamtbetrag 255,85 €"
        self.assertEqual(self.amount(t), (255.85, "EUR"))

    def test_english_total_not_subtotal(self):
        t = "Subtotal $1,200.00\nTax (10%) $120.00\nTotal $1,320.00"
        self.assertEqual(self.amount(t), (1320.0, "USD"))

    def test_total_with_label_and_value_far_apart_in_a_column_layout(self):
        t = "Subtotal" + " " * 40 + "SGD 4,500.00\nTotal" + " " * 42 + "SGD 4,725.00"
        self.assertEqual(self.amount(t), (4725.0, "SGD"))

    def test_partial_payment_keeps_the_invoice_total(self):
        t = "Total $1,000.00\nPaid -$400.00\nAmount due $600.00"
        self.assertEqual(self.amount(t), (1000.0, "USD"))

    def test_credit_note_keeps_its_sign(self):
        self.assertEqual(self.amount("Gesamtbetrag -119,00 €"), (-119.0, "EUR"))
        self.assertEqual(self.amount("Total credit -£2,340.00"), (-2340.0, "GBP"))

    def test_swiss_and_japanese_and_word_currencies(self):
        self.assertEqual(self.amount("Total CHF 1'234.50"), (1234.5, "CHF"))
        self.assertEqual(self.amount("Total Amount: JPY 125,000"), (125000.0, "JPY"))
        self.assertEqual(self.amount("Summe 1 234,56 Euro"), (1234.56, "EUR"))
        self.assertEqual(self.amount("TOTAL £7.80", "receipt"), (7.8, "GBP"))

    def test_a_bare_number_is_not_an_amount(self):
        # a date, a postcode, a phone number, a quantity and an invoice number: none of them is the total
        t = "Rechnung Nr. 20250614\n10115 Musterstadt\nTel. +49 30 1234567\nMenge 12\nDatum 14.03.2025"
        self.assertEqual(self.amount(t), (None, None))

    def test_receipt_total_not_the_cash_given(self):
        t = "Brot 2,49\nMilch 1,19\nSUMME EUR 3,68\nBar 10,00\nRückgeld 6,32"
        self.assertEqual(self.amount(t, "receipt"), (3.68, "EUR"))

    def test_reminder_takes_the_claim_with_fees(self):
        t = "Offener Betrag: 255,85 €\nMahngebühr: 5,00 €\nGesamtforderung: 260,85 €"
        self.assertEqual(self.amount(t, "reminder"), (260.85, "EUR"))

    def test_payslip_is_the_payout_not_the_gross(self):
        t = "Bruttolohn 3.500,00\nLohnsteuer -480,00\nAuszahlungsbetrag 2.320,00 EUR"
        self.assertEqual(self.amount(t, "payslip"), (2320.0, "EUR"))

    def test_bank_statement_letter_and_other_have_no_amount(self):
        for typ in ("bank_statement", "letter", "other"):
            self.assertEqual(self.amount("Neuer Kontostand 2.008,30 EUR\nTotal 55,00 EUR", typ), (None, None))

    def test_german_vat_without_any_currency_sign_is_euro(self):
        t = "Zwischensumme 807,50\nUSt 19 % 153,43\nGesamtbetrag 960,93"
        f = fields(t, "invoice", lang="de")
        self.assertEqual((f["amount"], f["currency"]), (960.93, "EUR"))
        self.assertIsNone(fields(t, "invoice", lang="en")["currency"])


class Dates(unittest.TestCase):
    def date(self, text, doctype="invoice", **kw):
        return fields(text, doctype, **kw)["doc_date"]

    def test_formats_english_and_german(self):
        for text, want in (("Invoice date: 2025-03-14", "2025-03-14"), ("Rechnungsdatum: 14.03.2025", "2025-03-14"),
                           ("Date: March 14, 2025", "2025-03-14"), ("Datum: 14. März 2025", "2025-03-14"),
                           ("Issue date: 14-Mar-2025", "2025-03-14"), ("Date: 14 March 2025", "2025-03-14"),
                           ("Datum: 14.3.25", "2025-03-14"), ("Date: 1st of March 2025", "2025-03-01")):
            with self.subTest(text=text):
                self.assertEqual(self.date(text), want)

    def test_slash_dates_by_the_day_that_cannot_be_a_month(self):
        self.assertEqual(self.date("Date: 03/14/2025"), "2025-03-14")
        self.assertEqual(self.date("Date: 14/03/2025"), "2025-03-14")

    def test_the_invoice_date_beats_the_due_and_delivery_dates(self):
        t = "Lieferdatum: 01.03.2025\nZahlungsziel: 31.03.2025\nRechnungsdatum: 05.03.2025"
        self.assertEqual(self.date(t), "2025-03-05")
        t = "Due date: April 13, 2025\nService date: March 1, 2025\nInvoice Date: March 14, 2025"
        self.assertEqual(self.date(t), "2025-03-14")

    def test_never_in_the_future(self):
        self.assertEqual(self.date("Rechnungsdatum: 05.03.2025\nZahlungsziel: 31.12.2030"), "2025-03-05")
        self.assertIsNone(self.date("Datum: 05.03.2030"))
        self.assertEqual(self.date("Datum: 05.03.2030\nRechnungsdatum 01.02.2025"), "2025-02-01")
        # `today` moves the line
        self.assertIsNone(self.date("Datum: 05.03.2025", today=date(2025, 1, 1)))

    def test_birth_dates_and_validity_dates_are_never_the_document_date(self):
        t = "Geburtsdatum: 01.01.1990\nGültig bis: 01.01.2030\nAusgestellt am: 14.03.2025"
        self.assertEqual(self.date(t, "letter"), "2025-03-14")
        self.assertIsNone(self.date("Date of birth: 02/02/1991", "letter"))
        self.assertIsNone(self.date("DOB: 1990-01-01\nValid until: 2027-05-01", "contract"))

    def test_place_and_date_line_of_a_letter(self):
        t = "Erika Beispiel\nMusterweg 3\nMusterstadt, den 14.03.2025\nSehr geehrte Damen und Herren,"
        self.assertEqual(self.date(t, "letter"), "2025-03-14")
        self.assertEqual(self.date("Dear Sir,\nSampleton, March 14, 2025\nbody", "letter"), "2025-03-14")

    def test_a_letter_that_cites_an_invoice_date_keeps_its_own(self):
        t = "March 20, 2025\n\nDear Mr Sample,\nRegarding your invoice INV-1042 dated March 14, 2025: please check."
        self.assertEqual(self.date(t, "letter"), "2025-03-20")

    def test_payslip_is_a_month_unless_an_issue_date_is_printed(self):
        self.assertEqual(self.date("Lohnabrechnung März 2025\nBrutto 3.000,00", "payslip"), "2025-03")
        self.assertEqual(self.date("Pay period: 01.06.2025 - 30.06.2025", "payslip"), "2025-06")
        self.assertEqual(self.date("GEHALTSABRECHNUNG November 2025\nAbrechnung erstellt am: 27.11.2025", "payslip"), "2025-11-27")

    def test_bank_statement_uses_its_date_or_period_end(self):
        self.assertEqual(self.date("Auszugsdatum: 31.03.2025\n03.03.2025 Lastschrift -5,00", "bank_statement"), "2025-03-31")
        self.assertEqual(self.date("Statement period: 01.03.2025 - 31.03.2025\n05/03/2025 Credit 5.00", "bank_statement"), "2025-03-31")
        self.assertEqual(self.date('"Period","2025-05-01","2025-05-31"\n"2025-05-02","x","5.00"', "bank_statement"), "2025-05-31")

    def test_a_data_export_without_statement_date_has_none(self):
        text = "Date;Details;Amount\n2025-02-03;Rent;-950,00\n2025-02-10;Salary;2000,00"
        self.assertIsNone(fields(text, "bank_statement", ext="csv")["doc_date"])

    def test_email_date_header(self):
        t = "Subject: x\nFrom: A B <a@mail.example.com>\nDate: Fri, 14 Mar 2025 09:12:00 +0100\n\nHello"
        self.assertEqual(fields(t, "other", ext="eml")["doc_date"], "2025-03-14")

    def test_other_documents_only_take_a_labelled_date(self):
        self.assertIsNone(self.date("We met on 2019-05-02 and again later.", "other"))
        self.assertEqual(self.date("Last updated: 2025-04-10\nnotes", "other"), "2025-04-10")


class References(unittest.TestCase):
    def ref(self, text, doctype="invoice", **kw):
        return fields(text, doctype, **kw)["reference"]

    def test_labelled_numbers(self):
        for text, want in (("Rechnungsnummer: RE-2025-0042", "RE-2025-0042"), ("Rechnungs-Nr.:   RE-2025-0042", "RE-2025-0042"),
                           ("Invoice No.: INV-1042", "INV-1042"), ("Invoice #: 2025/118", "2025/118"),
                           ("Rechnung Nr. 4711 vom 3. Mai", "4711")):
            with self.subTest(text=text):
                self.assertEqual(self.ref(text), want)

    def test_a_reminder_gets_its_own_number_not_the_invoice_it_cites(self):
        t = "Zahlungserinnerung Nr. ZE-2025-0066\nUnsere Rechnung Nr. RE-2025-0042 vom 14.03.2025 ist noch offen."
        self.assertEqual(self.ref(t, "reminder"), "ZE-2025-0066")
        self.assertIsNone(self.ref("FINAL NOTICE\nInvoice INV-4410 is overdue.", "reminder"))

    def test_name_followed_by_number_in_the_heading(self):
        self.assertEqual(self.ref("OFFER Q-2025-322\nDate: 2025-05-14", "quote"), "Q-2025-322")
        self.assertEqual(self.ref("QUOTATION DC-Q-2025-044\nDate: 12 March 2025", "quote"), "DC-Q-2025-044")
        self.assertEqual(self.ref("Muster AG\nRechnung 2025-77\nDatum 12.02.2025", "invoice"), "2025-77")

    def test_header_row_above_a_value_row(self):
        t = "Rechnungsnummer   Rechnungsdatum   Fällig am\nRE-1042           14.03.2025       13.04.2025"
        self.assertEqual(self.ref(t), "RE-1042")

    def test_an_iban_is_never_a_reference(self):
        for text in (f"Referenz: {IBAN}", f"Rechnungsnummer: {IBAN.replace(' ', '')}", "Ref: GB82WEST12345698765432",
                     "Reference: GB82 WEST 1234 5698 7654 32", "Invoice no. DE89370400440532013000"):
            with self.subTest(text=text):
                self.assertIsNone(self.ref(text))

    def test_personal_and_tax_ids_and_card_numbers_are_never_a_reference(self):
        for text in ("Ref: 4111111111111111", "Rechnungsnummer: 4111 1111 1111 1111", "Referenz: Steuer-ID 12345678901",
                     "Reference: passport P1234567", "Ref: Aufenthaltstitel 123456789",
                     "Invoice number: 2025", "Rechnungsnummer: 14.03.2025"):
            with self.subTest(text=text):
                self.assertIsNone(self.ref(text))

    def test_whatever_the_redactor_masks_is_never_a_reference(self):
        # the rule is the redactor's verdict, not a list of shapes: a stand-in redactor that masks a made-up identifier
        # format (QX-123456) shows that a masked value is refused, and that the same value is an ordinary number without it
        rx = re.compile(r"\bQX-\d{6}\b")

        def stand_in(text):
            return rx.sub("[redacted]", text), len(rx.findall(text))

        self.assertEqual(self.ref("Ref: QX-123456"), "QX-123456")
        with mock.patch("mse.redact.redact_count", stand_in):
            self.assertIsNone(self.ref("Ref: QX-123456"))

    def test_payslips_and_tax_documents_carry_no_reference_at_all(self):
        for typ in ("payslip", "tax_document", "other"):
            self.assertIsNone(self.ref("Referenz: AB-1234\nAktenzeichen: 99/99999", typ))

    def test_a_number_that_only_looks_like_an_iban_is_kept(self):
        self.assertEqual(self.ref("Belegnummer: ZB-20250717-0042", "receipt"), "ZB-20250717-0042")

    def test_statement_month_is_not_a_number(self):
        self.assertIsNone(self.ref("Kontoauszug 08/2025\nAuszugsdatum 29.08.2025", "bank_statement"))


class Counterparties(unittest.TestCase):
    def party(self, text, doctype="invoice", **kw):
        return fields(text, doctype, **kw)["counterparty"]

    def test_email_display_name_not_the_address(self):
        t = 'Subject: Hi\nFrom: "Example Shop News" <news@mail.example.com>\nDate: Wed, 05 Mar 2025 07:30:00 +0000\n\nbody'
        self.assertEqual(self.party(t, "other", ext="eml"), "Example Shop News")
        t2 = "Subject: Hi\nFrom: <billing@acme-parts.example>\nDate: Wed, 05 Mar 2025 07:30:00 +0000\n\nbody"
        self.assertEqual(self.party(t2, "other", ext="eml"), "Acme Parts")
        t3 = "Subject: Hi\nFrom: someone@example.com\nDate: Wed, 05 Mar 2025 07:30:00 +0000\n\nbody"
        self.assertIsNone(self.party(t3, "other", ext="eml"))
        for stem in sorted(docmeta._FREE_MAIL):                       # a private mailbox says nothing about a company
            with self.subTest(free_mail=stem):
                t4 = f"Subject: Hi\nFrom: someone@{stem}.com\nDate: Wed, 05 Mar 2025 07:30:00 +0000\n\nbody"
                self.assertIsNone(self.party(t4, "other", ext="eml"))

    def test_the_letterhead_not_the_customer(self):
        t = "Muster AG\nMusterweg 1\n\nRechnung an:\nKunde Beispiel GmbH\nHauptstr. 2\n\nRechnung Nr. 5"
        self.assertEqual(self.party(t), "Muster AG")
        t = "Bill to:\nSample Customer Ltd\n\nFrom:\nExample Supplies LLC\n12 Sample Avenue"
        self.assertEqual(self.party(t), "Example Supplies LLC")

    def test_a_broad_list_of_legal_forms(self):
        for name in ("Acme GmbH", "Muster AG", "Example Ltd", "Sample PLC", "Demo LLC", "Foo Inc", "Sample Corp.", "Beispiel KG",
                     "Muster OHG", "Exemple SARL", "Exemple SAS", "Voorbeeld BV", "Voorbeeld NV", "Exempel AB", "Eksempel AS",
                     "Esimerkki Oy", "Esempio S.p.A.", "Ejemplo S.L.", "Demo Pty Ltd", "Sample Pvt Ltd", "Muster e.K.", "Sample LLP",
                     "Exemple SA"):
            with self.subTest(name=name):
                self.assertEqual(self.party(f"{name}\nMain Street 1\nInvoice 5"), name)

    def test_the_built_in_list_is_broad_and_international(self):
        self.assertGreaterEqual(len(docmeta._ORG_SUFFIXES), 25)

    def test_extra_legal_forms_come_from_the_configuration(self):
        line = "Exemplo Comercio Ltda, Rua Principal 1"
        self.assertIsNone(docmeta._org_in_line(line, docmeta._org_suffix_rx(())))
        self.assertEqual(docmeta._org_in_line(line, docmeta._org_suffix_rx(("Ltda",))), "Exemplo Comercio Ltda")
        t = "Invoice\nPlease note: delivered by Exemplo Comercio Ltda today\nTotal 5 EUR"
        self.assertEqual(self.party(t, extra_org_suffixes=("Ltda",)), "Exemplo Comercio Ltda")

    def test_own_names_are_skipped(self):
        t = "Muster AG\nMusterweg 1\n\nBeispiel GmbH\nWeg 2\nRechnung"
        self.assertEqual(self.party(t), "Muster AG")
        self.assertEqual(self.party(t, own_names=("Muster AG",)), "Beispiel GmbH")

    def test_ambiguous_short_forms_need_to_end_the_name(self):
        self.assertIsNone(self.party("BILLING AS OF MARCH\nSome text here", "letter"))

    def test_private_sender_before_the_recipient_block(self):
        t = ("Erika Beispiel\nLindenweg 3\n10117 Musterstadt\n\nMuster Mobilfunk GmbH\nKundenservice\nPostfach 000000\n\n"
             "Musterstadt, 12.12.2024\nSehr geehrte Damen und Herren,\nMit freundlichen Grüßen")
        self.assertEqual(self.party(t, "letter"), "Erika Beispiel")

    def test_bank_is_the_sender_of_its_statement(self):
        t = "Test Software Solutions LLC\nAccount holder\n\nSAMPLE CREDIT UNION\nAccount statement\nOpening balance 100.00"
        self.assertEqual(self.party(t, "bank_statement"), "SAMPLE CREDIT UNION")

    def test_a_note_has_no_counterparty(self):
        self.assertIsNone(self.party("Meeting notes\nSample Trading Ltd was mentioned twice.", "other"))

    def test_the_value_is_never_an_identifier(self):
        for bad in (f"Rechnung\nDE89370400440532013000", "Rechnung\nAcme GmbH 12345678901"):
            p = self.party(bad)
            self.assertTrue(p is None or redact(p) == p)


class Privacy(unittest.TestCase):
    def test_identity_documents_get_nothing(self):
        text = ("PASSPORT\nSurname: SAMPLE\nGiven names: ALEX\nNationality: EXAMPLELAND\nDate of birth: 01 JAN 1990\nPassport No: P0000000\n"
                "P<EXLSAMPLE<<ALEX<<<<<<<<<<<<<<<<<<<<<<<<<<\nP00000000<0EXL9001014M3001014<<<<<<<<<<<<<<00")
        row = analyse(text, "id_page.pdf")
        self.assertEqual(row["doctype"], "other")
        self.assertTrue(all(row[k] is None for k in ("doc_date", "amount", "currency", "counterparty", "reference")))

    def test_no_field_of_any_corpus_document_holds_an_identifier(self):
        docs = DEV + (json.loads(HELDOUT.read_text())["docs"] if HELDOUT.exists() else [])
        ibans = re.compile(r"[A-Z]{2}\d{2}\s?(?:[A-Z0-9]{4}\s?){3,}")
        for d in docs:
            row = analyse(d["text"], d.get("filename", ""), d.get("ext", ""), {"kind": d.get("kind")})
            for k in ("counterparty", "reference"):
                v = row[k]
                if v is None:
                    continue
                self.assertEqual(redact(v), v, (d["id"], k, v))
                self.assertIsNone(ibans.search(v), (d["id"], k, v))
                self.assertNotRegex(v, r"\b\d{9,}\b", (d["id"], k))
            self.assertNotRegex(str(row["doc_date"] or ""), r"^19[0-8]\d", d["id"])

    def test_birth_date_lines_never_leak_into_the_date_of_a_payslip(self):
        text = "Lohnabrechnung\nGeburtsdatum: 15.07.1985\nSV-Nummer: 00 150785 X 000\nSteuer-ID: 00987654321\nAuszahlungsbetrag 1.000,00 EUR"
        row = analyse(text, "payslip.pdf")
        self.assertEqual(row["doctype"], "payslip")
        self.assertIsNone(row["reference"])
        self.assertNotEqual(row["doc_date"], "1985-07-15")


class Classification(unittest.TestCase):
    def cls(self, text, filename="", ext="pdf", **kw):
        return docmeta.classify(text, filename, ext, kw.pop("meta", None), **kw)

    def test_each_type_in_both_languages(self):
        cases = {
            "invoice": ("Rechnung Nr. 5\nRechnungsdatum 14.03.2025\nGesamtbetrag 100,00 €\nZahlungsziel 14 Tage", "Invoice\nInvoice No: 5\nTotal due $100\nPayment terms 14 days"),
            "receipt": ("Kassenbon\nBrot 2,49\nSUMME 2,49\nBar 5,00\nRückgeld 2,51", "Receipt\nLatte 4.50\nTOTAL 4.50\nCard payment\nThank you for your visit"),
            "bank_statement": ("Kontoauszug\nAlter Kontostand 100,00\nNeuer Kontostand 200,00\nBuchungstag Valuta", "Account statement\nOpening balance 100\nClosing balance 200"),
            "quote": ("Angebot Nr. A-1\nGültig bis 30.04.2025\nGesamt 500,00 €", "Quotation\nQuote no: Q-1\nValid until May 10"),
            "reminder": ("2. Mahnung\nMahngebühr 5,00 €\nBitte begleichen Sie den offenen Betrag", "Payment reminder\nOverdue invoice\nLate fee applies\nPlease pay immediately"),
            "payslip": ("Lohnabrechnung\nBrutto 3.000\nLohnsteuer\nSozialversicherung\nAuszahlungsbetrag", "Payslip\nGross pay\nNet pay\nIncome tax\nNational Insurance"),
            "tax_document": ("Einkommensteuerbescheid 2024\nFestgesetzte Einkommensteuer\nRechtsbehelfsbelehrung", "Tax assessment\nTaxable income\nTax due\nObjection"),
            "contract": ("Dienstleistungsvertrag\nzwischen A und B\n§ 1 Gegenstand\n§ 2 Haftung\nUnterschrift", "Service Agreement\nbetween A and B\n1. Term and termination\n2. Liability\nGoverning law\nSignature"),
            "letter": ("Sehr geehrte Damen und Herren,\nBetreff: Anfrage\nwir bitten um Rückmeldung.\nMit freundlichen Grüßen", "Dear Sir or Madam,\nWe are writing to ask about delivery.\nKind regards"),
        }
        for want, texts in cases.items():
            for t in texts:
                with self.subTest(want=want, text=t[:25]):
                    self.assertEqual(self.cls(t)[0], want)

    def test_unclear_is_other(self):
        for t in ("Pfannkuchen\nZutaten: Mehl, Eier, Milch", "def f(x):\n    return x + 1\n", "hello", ""):
            self.assertEqual(self.cls(t)[0], "other")

    def test_a_note_about_invoices_is_not_an_invoice(self):
        note = "# Invoice process\n\nEvery invoice is checked against the order and paid within 14 days. A reminder is sent when it is overdue."
        self.assertEqual(self.cls(note, "invoice-process.md", "md")[0], "other")

    def test_a_reminder_that_cites_an_invoice_is_a_reminder(self):
        t = "Mahnung\nzu unserer Rechnung Nr. RE-1 vom 14.03.2025 über 100,00 €\nRechnungsnummer RE-1\nMahngebühr 5,00 €\nGesamtforderung 105,00 €"
        self.assertEqual(self.cls(t)[0], "reminder")

    def test_a_letter_about_an_invoice_is_a_letter(self):
        t = "Re: your invoice INV-3391\n\nDear Ms Sample,\nThank you for your prompt payment of invoice INV-3391.\n\nWith kind regards,"
        self.assertEqual(self.cls(t, "ThankYou_INV-3391.docx", "docx")[0], "letter")

    def test_a_quote_with_a_total_is_a_quote_and_pro_forma_too(self):
        self.assertEqual(self.cls("Angebot Nr. A-1\nGültig bis 30.04.2025\nGesamt 5.593,00 €\nMwSt 19 %")[0], "quote")
        self.assertEqual(self.cls("PRO FORMA INVOICE\nNo. PI-5532\nValid for 30 days\nTotal value $12,450.00")[0], "quote")
        self.assertEqual(self.cls("PROFORMA-RECHNUNG Nr. PF-1\nWarenwert gesamt CHF 4'870.00")[0], "quote")

    def test_order_confirmations_and_delivery_notes_are_other(self):
        self.assertEqual(self.cls("AUFTRAGSBESTÄTIGUNG\nAuftrags-Nr.: AB-1\nAuftragssumme brutto: 2.751,28 €")[0], "other")
        self.assertEqual(self.cls("DELIVERY NOTE\nDelivery note no.: DN-1\nQty ordered 6 Qty delivered 6")[0], "other")

    def test_ledger_spreadsheet_is_not_a_statement(self):
        rows = "\n".join(f"2025-01-{d:02d}  Item {d}  {d}.00  0.00  {1000 - d}.00" for d in range(3, 20))
        self.assertEqual(self.cls("Date  Description  Debit  Credit  Balance\n" + rows, "ledger.xlsx", "xlsx")[0], "other")

    def test_file_name_hints_help_a_little_but_do_not_decide(self):
        self.assertEqual(self.cls("Seite 1\nRechnungsdatum 14.03.2025\nSumme 55,00 EUR", "Rechnung_2025.pdf")[0], "invoice")
        self.assertEqual(self.cls("Pfannkuchen\nMehl, Eier", "Rechnung_2025.pdf")[0], "other")

    def test_transliterated_umlauts(self):
        self.assertEqual(self.cls("KASSENBON\nBrot 2,49\nSUMME 2,49\nBar 5,00\nRueckgeld 2,51")[0], "receipt")
        self.assertEqual(self.cls("Zahlungserinnerung\nMahngebuehr 5,00 EUR\nBitte begleichen Sie den offenen Betrag")[0], "reminder")

    def test_low_confidence_is_other_and_confidence_is_a_fraction(self):
        typ, conf = self.cls("Total 5.00")
        self.assertEqual(typ, "other")
        self.assertTrue(0.0 <= conf <= 1.0)
        typ, conf = self.cls("Rechnung Nr. 5\nRechnungsdatum 14.03.2025\nGesamtbetrag 100,00 €\nZahlungsziel 14 Tage")
        self.assertEqual(typ, "invoice")
        self.assertGreater(conf, 0.5)

    def test_prior_from_embeddings_is_a_hook_that_never_decides_alone(self):
        weak = "Sample text with nothing typical"
        self.assertEqual(self.cls(weak, prior={"invoice": 1.0})[0], "other")
        borderline = "Rechnung\nSumme 5,00 EUR"
        base = self.cls(borderline)[1]
        self.assertGreaterEqual(self.cls(borderline, prior={"invoice": 1.0})[1], base)

    def test_extra_keywords_from_the_configuration(self):
        t = "Hundesteuer 2025\nHundemarke Nr. 55\nSteuerzeichen für Hundehalter"
        self.assertEqual(self.cls(t)[0], "other")
        extra = {"tax_document": ["hundesteuer", "hundemarke", "steuerzeichen", "hundehalter"]}
        self.assertEqual(self.cls(t, extra_keywords=extra)[0], "tax_document")
        self.assertEqual(self.cls(t, extra_keywords={"receipt": ["hundesteuer"]})[0], "other")

    def test_classification_does_not_depend_on_how_the_indexer_chunks_the_text(self):
        for d in DEV:
            chunked = "\n".join(extract.chunk_text(d["text"]))
            a = docmeta.classify(d["text"], d["filename"], d["ext"], {"kind": d["kind"]})[0]
            b = docmeta.classify(chunked, d["filename"], d["ext"], {"kind": d["kind"]})[0]
            self.assertEqual(a, b, d["id"])


class Corpus(unittest.TestCase):
    """The accuracy floor on the fictional corpora. The development corpus is what the rules were written against, the
    held-out set was written separately; both must stay high (the full table: python -m tests.docmeta_report)."""

    FIELDS = ("doctype", "doc_date", "amount", "currency", "counterparty", "reference")

    def score(self, docs, chunked=False):
        from tests.docmeta_report import evaluate
        stats, _ = evaluate(docs, chunked)
        return {f: s["ok"] / s["n"] for f, s in stats.items()}

    def test_development_corpus(self):
        for chunked in (False, True):
            acc = self.score(DEV, chunked)
            for f in self.FIELDS:
                self.assertGreaterEqual(acc[f], 0.95, (f, chunked, acc[f]))

    @unittest.skipUnless(HELDOUT.exists(), "no held-out set")
    def test_held_out_corpus(self):
        docs = json.loads(HELDOUT.read_text())["docs"]
        self.assertGreaterEqual(len(docs), 100)
        acc = self.score(docs)
        for f in self.FIELDS:
            self.assertGreaterEqual(acc[f], 0.9, (f, acc[f]))

    HELDOUT2 = Path(__file__).parent / "data" / "docmeta_heldout2.json"

    @unittest.skipUnless(HELDOUT2.exists(), "no second held-out set")
    def test_second_held_out_corpus(self):
        docs = json.loads(self.HELDOUT2.read_text())["docs"]
        self.assertGreaterEqual(len(docs), 70)
        for chunked in (False, True):
            acc = self.score(docs, chunked)
            for f in self.FIELDS:
                self.assertGreaterEqual(acc[f], 0.9, (f, chunked, acc[f]))

    def test_speed(self):
        import time
        docs = (DEV * 25)[:1000]
        t = time.perf_counter()
        for d in docs:
            analyse(d["text"], d["filename"], d["ext"], meta={"kind": d["kind"]})
        per_doc_ms = (time.perf_counter() - t) / len(docs) * 1000
        self.assertLess(per_doc_ms, 15.0, per_doc_ms)       # about 1 ms here; a loose bound for slow machines


class FilterSyntax(unittest.TestCase):
    def test_amount(self):
        P = docmeta.parse_amount
        self.assertEqual(P(">500"), (500.0, False, None, True))
        self.assertEqual(P(">=500"), (500.0, True, None, True))
        self.assertEqual(P("<100"), (None, True, 100.0, False))
        self.assertEqual(P("<=100"), (None, True, 100.0, True))
        self.assertEqual(P("100..200"), (100.0, True, 200.0, True))
        self.assertEqual(P("200..100"), (100.0, True, 200.0, True))
        self.assertEqual(P("1.000,50"), (1000.5, True, 1000.5, True))
        self.assertEqual(P(">1,000"), (1000.0, False, None, True))
        for bad in ("", "abc", ">", "5..x", "<>5"):
            with self.assertRaises(ValueError, msg=bad):
                P(bad)
        ok = docmeta.amount_ok
        self.assertTrue(ok(500.01, P(">500")))
        self.assertFalse(ok(500.0, P(">500")))
        self.assertTrue(ok(500.0, P(">=500")))
        self.assertTrue(ok(150.0, P("100..200")))
        self.assertFalse(ok(None, P("<100")))
        self.assertTrue(ok(-5.0, P("<0")))
        self.assertTrue(ok(119.0, P("119")))

    def test_docdate(self):
        P = docmeta.parse_docdate
        self.assertEqual(P("2025"), ("2025-01-01", "2026-01-01"))
        self.assertEqual(P("2025-03"), ("2025-03-01", "2025-04-01"))
        self.assertEqual(P("2025-12"), ("2025-12-01", "2026-01-01"))
        self.assertEqual(P("2025-03-14"), ("2025-03-14", "2025-03-15"))
        self.assertEqual(P("2025-01..2025-06"), ("2025-01-01", "2025-07-01"))
        self.assertEqual(P("2025-01-01.."), ("2025-01-01", None))
        self.assertEqual(P("..2025-06"), (None, "2025-07-01"))
        for bad in ("", "..", "2025-13", "march", "2025-06..2025-01", "25"):
            with self.assertRaises(ValueError, msg=bad):
                P(bad)
        ok = docmeta.docdate_ok
        self.assertTrue(ok("2025-03-14", P("2025-03")))
        self.assertFalse(ok("2025-04-01", P("2025-03")))
        self.assertTrue(ok("2025-03", P("2025-03")))            # a month-only date (a payslip)
        self.assertFalse(ok(None, P("2025")))

    def test_doctype_names(self):
        self.assertEqual(docmeta.canon_doctype("Invoices"), "invoice")
        self.assertEqual(docmeta.canon_doctype("Rechnung"), "invoice")
        self.assertEqual(docmeta.canon_doctype("bank-statement"), "bank_statement")
        self.assertEqual(docmeta.canon_doctype("Kontoauszüge"), "bank_statement")
        self.assertEqual(docmeta.canon_doctype("tax_document"), "tax_document")
        self.assertIsNone(docmeta.canon_doctype("pdf"))
        self.assertIsNone(docmeta.canon_doctype("other"))        # type:other stays the file kind
        types, rest = docmeta.split_types(["receipt,invoice", "pdf", "Quittung"])
        self.assertEqual((types, rest), ({"receipt", "invoice"}, ["pdf"]))
        self.assertEqual(docmeta.doctype_values({"doctype": ["other"]}), {"other"})
        with self.assertRaises(ValueError):
            docmeta.doctype_values({"doctype": ["spaceship"]})

    def test_natural_language(self):
        nl = docmeta.nl_map
        self.assertEqual(nl("invoices over 500 EUR"), ("", {"amount": [">500"], "currency": ["EUR"], "type": ["invoice"]}))
        self.assertEqual(nl("Rechnungen über 500 Euro"), ("", {"amount": [">500"], "currency": ["EUR"], "type": ["invoice"]}))
        self.assertEqual(nl("receipts under 20 €")[1], {"amount": ["<20"], "currency": ["EUR"], "type": ["receipt"]})
        self.assertEqual(nl("contracts mehr als 1.000 $")[1]["amount"], [">1000"])
        self.assertEqual(nl("documents between 100 and 200 EUR")[1], {"amount": ["100..200"], "currency": ["EUR"]})
        self.assertEqual(nl("Verträge zwischen 100 und 200 Euro")[1]["amount"], ["100..200"])
        self.assertEqual(nl("invoices at least 100")[1], {"amount": [">=100"], "type": ["invoice"]})
        self.assertEqual(nl("invoices from Example Supplies over 500 EUR")[0], "from Example Supplies")
        # conservative: nothing without a comparator, nothing without a currency or a kind of document, no years, no units
        for q in ("invoices from March", "Rechnung 119,00 EUR", "temperature over 40 degrees", "something at least 5 pages",
                  "rent over 2025", "budget over 500", "shipments under 20 kg", "over 500"):
            with self.subTest(q=q):
                self.assertEqual(nl(q), (q, {}))


if __name__ == "__main__":
    unittest.main()
