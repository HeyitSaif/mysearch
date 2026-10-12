"""Redaction of national ID numbers across countries, the cases the module always masked, and the user-defined formats.

Every row of mse.redact.NATIONAL_IDS has a valid example that is masked and a near-miss that is not. The valid examples
are the numbers a published rule uses in its own documentation, or numbers built here with that rule; none is anyone's
real number. A near-miss changes one check digit (or the date, or the range) and is written without a label, because the
labelled rules of the module mask a labelled value whatever its check. A row that is labelled is therefore checked with
its label in front. The legacy cases are the ones the module masked before the table existed.
Run: .venv/bin/python -m unittest -v tests.test_redact_intl"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("MSE_HOME", tempfile.mkdtemp(prefix="mse-test-"))

from mse import config, paths  # noqa: E402
from mse import redact  # noqa: E402
from mse.redact import MASK, NATIONAL_IDS, redact as red, redact_count, redact_path  # noqa: E402

# (country, row name as in NATIONAL_IDS, valid example, near-miss). One entry per row.
VECTORS = [
    ("AE", "national ID, 784 series", "784-1970-1234567-1", "784-1970-123456-1"),                 # shape only, as before
    ("AT", "social security number, spaced", "1237 010180", "1237 010181"),            # official example; weighted mod 11
    ("AT", "social security number, labelled", "SV-Nr 1237010180", "SV-Nr 1237010181"),
    ("BE", "national register number", "85.07.30-033.28", "85.07.30-033.29"),          # mod 97 check, documented example
    ("BE", "national register number, labelled", "RRN 85073003328", "RRN 85073003329"),
    ("BR", "CPF", "111.444.777-35", "111.444.777-36"),                                 # the two check digits, classic example
    ("BR", "CPF, labelled", "CPF 11144477735", "CPF 11144477736"),
    ("CA", "social insurance number, labelled", "SIN 123-456-782", "SIN 123-456-783"),  # Luhn, documented example
    ("CH", "AHV number", "756.9217.0769.85", "756.9217.0769.86"),                      # EAN-13 check, documented example
    ("CH", "AHV number, labelled", "AHV 7569217076985", "AHV 7569217076986"),
    ("DE", "tax ID (Steuer-ID)", "65 929 970 489", "65 929 970 480"),                  # BZSt documented example
    ("DE", "ID card (Personalausweis), labelled", "Personalausweis T220001293", "T220001294"),  # MRZ specimen, ICAO check
    ("DK", "CPR number, labelled", "CPR 211062-5629", "CPR 511062-5629"),              # embedded date; no check digit
    ("ES", "DNI, labelled", "DNI 12345678Z", "DNI 12345678A"),                         # letter from mod 23
    ("ES", "NIE", "X1234567L", "X1234567M"),                                           # letter from mod 23, X=0
    ("FI", "henkilötunnus", "131052-308T", "131052-308U"),                             # control character, documented
    ("FR", "NIR", "1 85 05 78 006 084 91", "1 85 05 78 006 084 92"),                  # key = 97 - (number mod 97)
    ("GB", "National Insurance number", "AB 12 34 56 C", "QQ 12 34 56 C"),             # prefix exclusions
    ("IN", "Aadhaar, labelled", "Aadhaar 2345 6789 0124", "Aadhaar 2345 6789 0125"),   # Verhoeff check
    ("IN", "PAN", "ABCPE1234F", "ABCXE1234F"),                                         # holder-type letter
    ("IT", "codice fiscale", "RCCMNL83S18D969H", "RCCMNL83S18D969J"),                  # check letter, documented
    ("NL", "BSN, labelled", "BSN 111222333", "BSN 111222334"),                         # eleven-proof, documented
    ("NO", "fødselsnummer, grouped", "151086 95088", "151086 95077"),                  # two mod-11 checks, documented
    ("NO", "fødselsnummer, labelled", "fødselsnummer 15108695088", "fødselsnummer 15108695077"),
    ("PK", "CNIC", "12345-1234567-1", "12345-123456-1"),                               # shape only, as before
    ("PL", "PESEL, labelled", "PESEL 44051401359", "PESEL 44051401358"),               # check digit, documented
    ("SE", "personnummer", "811218-9876", "811218-9877"),                              # Luhn and date
    ("SE", "personnummer, labelled", "personnummer 8112189876", "personnummer 8112189877"),
    ("TR", "TC kimlik number, labelled", "TC kimlik 10000000146", "TC kimlik 10000000147"),  # two check digits
    ("US", "social security number, labelled", "SSN 536-90-4399", "SSN 666-90-4399"),  # area 666 is never issued
]

# Ordinary text. None of it may change: invoice and order numbers, phone numbers, amounts, dates, codes, and digit groups
# that are only a slice of a longer number.
ORDINARY = [
    "Invoice 1234567001", "Order ID 1234567890", "Kundennummer 1234567890", "RE-2025-0042 vom 14.03.2025",
    "+49 30 1234567", "0664 1234567", "+1 (555) 123-4567", "Betrag 1.234,56 €", "$1,234.56", "2025-03-14",
    "Nr. 218 036 242, paid", "Booking 555-12-3456 confirmed", "Ref 352 137 491 571 533 EUR",
    "Invoice 2025 031412 for hosting", "Ticket 0123-4567-8901", "Serial 358240051111110", "4111 1111 1111 1111",
    "ISBN 978-3-16-148410-0", "EAN 4006381333931", "Tracking 1Z999AA10123456784", "Konto 1234567890, BLZ 10010010",
    "Gesamt 255,85 EUR, Pos. 3 12,50 €", "v1.2.3 and 2.0.1-beta", "10115 Berlin", "Reg 123456 B", "USt-IdNr. DE123456789",
]


class Table(unittest.TestCase):
    def test_table_covers_more_than_twenty_countries(self):
        self.assertGreaterEqual(len({row[0] for row in NATIONAL_IDS}), 20)

    def test_table_is_sorted_by_country_code(self):
        codes = [row[0] for row in NATIONAL_IDS]
        self.assertEqual(codes, sorted(codes))

    def test_every_row_has_a_vector_and_every_vector_a_row(self):
        rows = {(row[0], row[1]) for row in NATIONAL_IDS}
        vectors = {(c, n) for c, n, _valid, _near in VECTORS}
        self.assertEqual(rows, vectors)

    def test_every_check_is_a_function_or_none(self):
        for _country, _name, rx, check in NATIONAL_IDS:
            self.assertIn("id", rx.groupindex)
            self.assertTrue(check is None or callable(check))


class PerCountry(unittest.TestCase):
    def test_valid_example_is_masked(self):
        for country, name, valid, _near in VECTORS:
            with self.subTest(country=country, row=name):
                out, n = redact_count(valid)
                self.assertNotEqual(out, valid)
                self.assertIn(MASK, out)
                self.assertGreaterEqual(n, 1)

    def test_near_miss_is_not_masked(self):
        for country, name, _valid, near in VECTORS:
            with self.subTest(country=country, row=name):
                self.assertEqual(red(near), near)

    def test_valid_example_is_masked_once_and_the_label_stays(self):
        self.assertEqual(redact_count("BSN 111222333"), ("BSN [redacted]", 1))
        self.assertEqual(redact_count("UAE ID 784-1970-1234567-1"), ("UAE ID [redacted]", 1))
        self.assertEqual(redact_count("TC kimlik 10000000146"), ("TC kimlik [redacted]", 1))

    def test_a_label_after_digits_still_matches(self):
        # a label right after a number or an IBAN is still a label; the digit rule applies to the number only
        self.assertEqual(red("IBAN DE89 3704 0044 0532 0130 00 BSN 111222333"), "IBAN DE•••• 3000 BSN [redacted]")
        self.assertEqual(red("Nr 3000 SSN 536-90-4399"), "Nr 3000 SSN [redacted]")

    def test_each_number_on_a_line_is_decided_by_its_own_check(self):
        self.assertEqual(red("65 929 970 489 und 65 929 970 480"), f"{MASK} und 65 929 970 480")


class LegacyCases(unittest.TestCase):
    """Everything the module masked before the table existed, from tests/test_core.py and tests/test_mcp_redaction.py."""

    def test_masked_as_before(self):
        cases = [
            ("Passport No.: AB1234567", "AB1234567"),
            ("Aufenthaltstitel Nr. Y12A3B4C5", "Y12A3B4C5"),
            ("UAE ID 784-1970-1234567-1", "784-1970-1234567-1"),
            ("Geburtsdatum: 01.01.1970", "01.01.1970"),
            ("Steuer-ID: 65 929 970 489", "65 929 970 489"),
            ("Sozialversicherungsnummer 12 345678 A 901", "12 345678 A 901"),
            ("date of birth 1 March 1990", "1 March 1990"),
            ("P<UTOERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<<<<< L898902C36UTO7408122F1204159ZE184226B<<<<<", "P<UTO"),
        ]
        for text, raw in cases:
            with self.subTest(text=text[:40]):
                out = red(text)
                self.assertIn(MASK, out)
                self.assertNotIn(raw, out)

    def test_company_identifiers_stay(self):
        for text in ("Reg 123456 B", "Steuer-Nr. 12/345/67890", "USt-IdNr. DE123456789", "Invoice 1234567001"):
            self.assertEqual(red(text), text)

    def test_iban_keeps_country_and_last_four(self):
        self.assertEqual(red("IBAN DE89 3704 0044 0532 0130 00"), "IBAN DE•••• 3000")
        self.assertEqual(red("Pay DE89370400440532013000 now"), "Pay DE•••• 3000 now")

    def test_date_of_birth_and_machine_readable_zone_unchanged(self):
        self.assertEqual(red("Geburtsdatum: 01.01.1970"), f"Geburtsdatum: {MASK}")
        mrz = "P<UTOERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<<<<< L898902C36UTO7408122F1204159ZE184226B<<<<<"
        self.assertEqual(red(mrz), f"{MASK} {MASK}")      # the line has a space in it: each half is masked, as before

    def test_identity_file_names_as_before(self):
        self.assertEqual(redact_path("01_Records/Passport_AB1234567.pdf"), "01_Records/Passport_[id].pdf")

    def test_identifier_shaped_token_without_a_valid_check_is_left_alone_by_the_table(self):
        # the NIE near-miss of tests/test_mcp_redaction: the table does not mask it, and _guard masks it as an ID
        self.assertEqual(red("X1234567Y"), "X1234567Y")


class Ordinary(unittest.TestCase):
    def test_ordinary_numbers_are_unchanged(self):
        for text in ORDINARY:
            with self.subTest(text=text):
                self.assertEqual(redact_count(text), (text, 0))

    def test_a_digit_group_inside_a_longer_run_is_not_masked(self):
        self.assertEqual(red("Ref 352 137 491 571 533"), "Ref 352 137 491 571 533")
        self.assertEqual(red("Nr. 9 218 036 242"), "Nr. 9 218 036 242")


class ExtraPatterns(unittest.TestCase):
    """config agents.extra_id_patterns: a list of {name, regex}, read again whenever the config file changes.
    The config here is a private temporary file (paths.CONFIG is patched), so the shared test configuration is never
    touched and the other tests see the same keys as before."""

    def setUp(self):
        folder = Path(tempfile.mkdtemp(prefix="redact-extra-"))
        self.path = folder / "config.json"
        patcher = mock.patch.object(paths, "CONFIG", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def use(self, patterns):
        """Write the file the way config does: a new file that replaces the old one."""
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"agents": {"extra_id_patterns": patterns}}), encoding="utf-8")
        os.replace(tmp, self.path)

    def test_user_pattern_is_masked_and_counted(self):
        self.use([{"name": "Staff badge", "regex": "EMP-[0-9]{6}"}])
        self.assertEqual(redact_count("Badge EMP-123456 ok"), ("Badge [redacted] ok", 1))

    def test_config_module_keeps_the_list(self):
        config.update_settings({"agents": {"extra_id_patterns": [{"name": "Staff badge", "regex": "EMP-[0-9]{6}"}]}})
        self.assertEqual(config.load()["agents"]["extra_id_patterns"], [{"name": "Staff badge", "regex": "EMP-[0-9]{6}"}])
        self.assertEqual(red("Badge EMP-123456"), "Badge [redacted]")

    def test_built_in_rules_still_run_with_user_patterns(self):
        self.use([{"name": "Staff badge", "regex": "EMP-[0-9]{6}"}])
        self.assertEqual(red("IBAN DE89 3704 0044 0532 0130 00 EMP-123456"), "IBAN DE•••• 3000 [redacted]")

    def test_refused_patterns_are_skipped_and_the_rest_work(self):
        refused = [
            {"name": "nested", "regex": "(a+)+b"},
            {"name": "nested class", "regex": "([0-9]*)*x"},
            {"name": "backref", "regex": r"(\d)\1"},
            {"name": "empty", "regex": "x*"},
            {"name": "too long", "regex": "a" * 201},
            {"name": "bad", "regex": "["},
            {"name": "not text", "regex": 42},
            "not an object",
        ]
        self.use(refused + [{"name": "ok", "regex": "EMP-[0-9]{6}"}])
        self.assertEqual(redact_count("EMP-123456 and 65 929 970 489"), (f"{MASK} and {MASK}", 2))

    def test_too_many_patterns_are_capped(self):
        self.use([{"name": f"p{i}", "regex": f"Q{i:03d}-[0-9]{{4}}"} for i in range(40)])
        self.assertEqual(red("Q031-1234"), MASK)                 # the 32nd entry is applied
        self.assertEqual(red("Q039-1234"), "Q039-1234")          # beyond the cap of 32: not applied

    def test_changed_patterns_are_picked_up(self):
        self.use([{"name": "a", "regex": "AAA-[0-9]{3}"}])
        self.assertEqual(red("AAA-123 BBBBB-456"), f"{MASK} BBBBB-456")
        self.use([{"name": "b", "regex": "BBBBB-[0-9]{3}"}])
        self.assertEqual(red("AAA-123 BBBBB-456"), f"AAA-123 {MASK}")

    def test_unreadable_config_gives_no_extra_patterns(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(redact_count("EMP-123456"), ("EMP-123456", 0))
        self.assertEqual(red("BSN 111222333"), "BSN [redacted]")      # the built-in rules still run

    def test_no_patterns_means_no_extra_masks(self):
        self.use([])
        self.assertEqual(redact_count("EMP-123456"), ("EMP-123456", 0))


if __name__ == "__main__":
    unittest.main()
