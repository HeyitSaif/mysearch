"""A development corpus of FICTIONAL documents (English and German) with their expected document facts. Companies, people,
addresses and numbers are invented; the only real-looking values are the public example IBANs of the IBAN standard.

DEV is what the rules were written against. The held-out set (tests/data/docmeta_heldout.json) was written separately,
without sight of the rules, and is what the accuracy numbers in the report are measured on.
`variants(doc)` runs a document through the indexer's own chunking (long paragraphs lose their line breaks).
"""

TODAY = "2026-11-02"


def D(id_, filename, ext, kind, text, doctype, doc_date=None, amount=None, currency=None, counterparty=None, reference=None,
      lang="de"):
    return {"id": id_, "filename": filename, "ext": ext, "kind": kind, "text": text.strip("\n"),
            "expected": {"doctype": doctype, "doc_date": doc_date, "amount": amount, "currency": currency,
                         "counterparty": counterparty, "reference": reference, "lang": lang}}


DEV = [
    D("d01", "Rechnungen/RE-2025-0042_Beispiel.pdf", "pdf", "pdf", """
Beispiel Handels GmbH
Musterstraße 1 · 10115 Musterstadt
Tel. +49 30 1234567 · info@beispiel-handels.example

Rechnung Nr. RE-2025-0042
Rechnungsdatum: 14.03.2025
Lieferdatum: 10.03.2025
Fällig am: 13.04.2025

Pos  Beschreibung      Menge  Einzelpreis  Gesamt
1    Widget Standard   10     12,50 €      125,00 €
2    Montage           2      45,00 €      90,00 €

Nettobetrag          215,00 €
MwSt 19 %             40,85 €
Gesamtbetrag         255,85 €

Bitte überweisen Sie den Betrag bis zum 13.04.2025 auf IBAN DE89 3704 0044 0532 0130 00.
Mit freundlichen Grüßen
Beispiel Handels GmbH
""", "invoice", "2025-03-14", 255.85, "EUR", "Beispiel Handels GmbH", "RE-2025-0042"),

    D("d02", "scan_0231.pdf", "pdf", "pdf", """
Sample Trading Ltd
1 Example Street, Sampleton SA1 1AA

INVOICE
Invoice No: INV-1042
Date: March 14, 2025
Due date: April 13, 2025

Description     Qty   Unit price   Amount
Consulting      10    $120.00      $1,200.00

Subtotal $1,200.00
Tax (10%) $120.00
Total $1,320.00
Amount due $1,320.00
Bank: Example Bank  IBAN GB82 WEST 1234 5698 7654 32
""", "invoice", "2025-03-14", 1320.00, "USD", "Sample Trading Ltd", "INV-1042", "en"),

    D("d03", "Muster_Logistik_Rechnung.pdf", "pdf", "pdf", """
Muster Logistik AG
Beispielweg 12, 80331 Musterhausen

RECHNUNG
Rechnungsnummer 2025-0815       Datum 3. Juli 2025
Kundennummer K-4711

Transportleistung Mai 2025                       1.980,00 EUR
Zwischensumme                                    1.980,00 EUR
zzgl. 19 % MwSt.                                   376,20 EUR
Rechnungsbetrag                                  2.356,20 EUR

Zahlbar innerhalb von 14 Tagen ohne Abzug.
USt-IdNr. DE123456789
""", "invoice", "2025-07-03", 2356.20, "EUR", "Muster Logistik AG", "2025-0815"),

    D("d04", "invoice-118.pdf", "pdf", "pdf", """
Demo Consulting Inc
31 Placeholder Street, Sampleton

TAX INVOICE
Invoice #: 2025/118
Issue date: 14-Mar-2025
Payment terms: 30 days

Strategy workshop                              CAD 4,500.00
Subtotal                                       CAD 4,500.00
VAT 13%                                          CAD 585.00
Total                                          CAD 5,085.00
""", "invoice", "2025-03-14", 5085.0, "CAD", "Demo Consulting Inc", "2025/118", "en"),

    D("d05", "inv_partial.pdf", "pdf", "pdf", """
Foo Widgets Inc
200 Example Avenue

Invoice number: FW-77
Invoice date: 02/17/2025

Widgets (200 pcs)           $1,000.00
Total                       $1,000.00
Paid                         -$400.00
Amount due                    $600.00
""", "invoice", "2025-02-17", 1000.00, "USD", "Foo Widgets Inc", "FW-77", "en"),

    D("d06", "Gutschrift_G-2025-9.pdf", "pdf", "pdf", """
Testwerk KG
Gutschrift Nr. G-2025-9
Datum: 05.05.2025
Zu Rechnung RE-2025-0042

Retoure Widget Standard                          -119,00 €
Gesamtbetrag                                     -119,00 €
""", "invoice", "2025-05-05", -119.00, "EUR", "Testwerk KG", "G-2025-9"),

    D("d07", "kassenbon_0412.jpg", "jpg", "image", """
Musterhausen Supermarkt
Hauptstr. 5
14.03.25 17:42
Brot                    2,49
Milch                   1,19
Äpfel                   3,98
SUMME EUR               7,66
Bar                    10,00
Rückgeld                2,34
Vielen Dank für Ihren Einkauf!
""", "receipt", "2025-03-14", 7.66, "EUR", "Musterhausen Supermarkt", None),

    D("d08", "receipt.pdf", "pdf", "pdf", """
Example Coffee Co
Receipt #4412
03/14/2025 08:15
Latte           $4.50
Muffin          $3.25
TOTAL           $7.75
Card payment VISA ****
Thank you for your visit!
""", "receipt", "2025-03-14", 7.75, "USD", "Example Coffee Co", "4412", "en"),

    D("d09", "Quittung.pdf", "pdf", "pdf", """
Quittung
Betrag dankend erhalten: 85,00 Euro
Für: Gartenarbeiten
Musterstadt, den 21.06.2025
Beispiel Gartenbau e.K.
""", "receipt", "2025-06-21", 85.00, "EUR", "Beispiel Gartenbau e.K.", None),

    D("d10", "Kontoauszug_2025-03.pdf", "pdf", "pdf", """
Beispielbank AG
Kontoauszug
IBAN DE89 3704 0044 0532 0130 00
Auszug Nr. 3/2025        Datum: 31.03.2025

Alter Kontostand                          1.250,00 EUR
03.03.2025  Lastschrift Strom Muster AG        -85,00
05.03.2025  Gutschrift Kunde Beispiel         500,00
10.03.2025  Überweisung Miete                 -650,00
15.03.2025  Lastschrift Versicherung          -42,50
20.03.2025  Gehalt                          2.100,00
28.03.2025  Kartenzahlung Supermarkt          -64,20
Neuer Kontostand                          2.008,30 EUR
""", "bank_statement", "2025-03-31", None, None, "Beispielbank AG", "3/2025"),

    D("d11", "statement_march.pdf", "pdf", "pdf", """
Example Bank plc
ACCOUNT STATEMENT
Statement period: 01.03.2025 - 31.03.2025
Account number 12345678   Sort code 00-00-00

Opening balance                       £1,200.00
03/03/2025  Direct debit Utilities      -£85.00
05/03/2025  Credit Customer            £500.00
10/03/2025  Standing order Rent        -£650.00
15/03/2025  Card payment Shop           -£42.50
20/03/2025  Salary                   £2,100.00
Closing balance                       £3,022.50
""", "bank_statement", "2025-03-31", None, None, "Example Bank plc", None, "en"),

    D("d12", "umsaetze.csv", "csv", "text", """
Buchungstag;Valuta;Verwendungszweck;Betrag;Saldo
03.03.2025;03.03.2025;Lastschrift Strom;-85,00;1165,00
05.03.2025;05.03.2025;Gutschrift Kunde;500,00;1665,00
10.03.2025;10.03.2025;Miete;-650,00;1015,00
15.03.2025;15.03.2025;Versicherung;-42,50;972,50
20.03.2025;20.03.2025;Gehalt;2100,00;3072,50
""", "bank_statement", None, None, None, None, None),

    D("d13", "Dienstleistungsvertrag.pdf", "pdf", "pdf", """
Dienstleistungsvertrag

zwischen der Beispiel Handels GmbH, Musterstraße 1, 10115 Musterstadt
- nachfolgend "Auftraggeber" genannt -
und der Demo Consulting Inc, Sampleton
- nachfolgend "Auftragnehmer" genannt -

§ 1 Vertragsgegenstand
Der Auftragnehmer erbringt Beratungsleistungen.
§ 2 Laufzeit und Kündigung
Der Vertrag beginnt am 01.04.2025 und läuft zwölf Monate.
§ 3 Haftung
§ 4 Gerichtsstand ist Musterstadt.

Musterstadt, den 20.03.2025

______________________        ______________________
Unterschrift Auftraggeber      Unterschrift Auftragnehmer
""", "contract", "2025-03-20", None, None, "Beispiel Handels GmbH", None),

    D("d14", "NDA_Sample.docx", "docx", "office", """
MUTUAL NON-DISCLOSURE AGREEMENT
This Agreement is made on 12 January 2025 between Sample Trading Ltd ("Party A") and Foo Widgets Inc ("Party B"),
hereinafter the parties.
1. Confidential Information
2. Term and Termination. This Agreement shall remain in force for two years.
3. Governing law: the laws of England.
IN WITNESS WHEREOF the parties have executed this Agreement.
Signature: ______________   Signature: ______________
""", "contract", "2025-01-12", None, None, "Sample Trading Ltd", None, "en"),

    D("d15", "Brief_Anfrage.pdf", "pdf", "pdf", """
Muster Logistik AG
Beispielweg 12 · 80331 Musterhausen

Musterhausen, 14. März 2025

Betreff: Rückfrage zu Ihrem Auftrag

Sehr geehrte Damen und Herren,

wir beziehen uns auf Ihr Schreiben vom 3. März und bitten um Bestätigung des Liefertermins.

Mit freundlichen Grüßen
Erika Beispiel
""", "letter", "2025-03-14", None, None, "Muster Logistik AG", None),

    D("d16", "letter_invoice_query.pdf", "pdf", "pdf", """
Foo Widgets Inc
200 Example Avenue

March 20, 2025

Dear Mr Sample,

Regarding your invoice INV-1042 dated March 14, 2025: we believe the quantity is wrong and kindly ask you to check it.

Kind regards,
Alex Example
Foo Widgets Inc
""", "letter", "2025-03-20", None, None, "Foo Widgets Inc", None, "en"),

    D("d17", "Angebot_A-2025-31.pdf", "pdf", "pdf", """
Testwerk KG
Angebot Nr. A-2025-31
Datum: 02.04.2025
Gültig bis: 30.04.2025

Pos  Leistung             Menge  Preis
1    Planung              1      1.500,00 €
2    Umsetzung            1      3.200,00 €

Gesamt netto             4.700,00 €
MwSt 19 %                  893,00 €
Gesamt                   5.593,00 €

Wir freuen uns auf Ihren Auftrag.
""", "quote", "2025-04-02", 5593.00, "EUR", "Testwerk KG", "A-2025-31"),

    D("d18", "quotation_Q-88.pdf", "pdf", "pdf", """
Demo Consulting Inc
QUOTATION
Quote no: Q-88
Date: 10 April 2025
Valid until: 10 May 2025

Workshop (2 days)                  GBP 2,340.00
Total                              GBP 2,340.00
To accept this quote, please sign and return.
""", "quote", "2025-04-10", 2340.00, "GBP", "Demo Consulting Inc", "Q-88", "en"),

    D("d19", "Mahnung_2.pdf", "pdf", "pdf", """
Beispiel Handels GmbH
2. Mahnung
Datum: 20.04.2025

Zu unserer Rechnung Nr. RE-2025-0042 vom 14.03.2025 über 255,85 € haben wir noch keinen Zahlungseingang festgestellt.
Offener Betrag:  255,85 €
Mahngebühr:        5,00 €
Gesamtforderung: 260,85 €
Bitte zahlen Sie bis zum 30.04.2025, sonst leiten wir rechtliche Schritte ein.
""", "reminder", "2025-04-20", 260.85, "EUR", "Beispiel Handels GmbH", None),

    D("d20", "final_notice.pdf", "pdf", "pdf", """
Sample Trading Ltd
FINAL NOTICE
Date: 2 May 2025

Invoice INV-1042 of $1,320.00 is overdue. A late fee of $25.00 applies.
Total amount due: $1,345.00
Please pay immediately.
""", "reminder", "2025-05-02", 1345.00, "USD", "Sample Trading Ltd", None, "en"),

    D("d21", "Lohnabrechnung_2025-03.pdf", "pdf", "pdf", """
Beispiel Handels GmbH
Lohnabrechnung März 2025
Personalnummer 00123   Steuerklasse 1
Geburtsdatum: 01.01.1990
SV-Nummer: 12 010190 A 123
Steuer-ID: 12 345 678 901

Bruttolohn                        3.500,00
Lohnsteuer                         -480,00
Sozialversicherung                 -700,00
Auszahlungsbetrag                 2.320,00 EUR
""", "payslip", "2025-03", 2320.00, "EUR", "Beispiel Handels GmbH", None),

    D("d22", "payslip_june.pdf", "pdf", "pdf", """
Demo Consulting Ltd
PAYSLIP
Pay period: 01.06.2025 - 30.06.2025
Employee ID: E-1042
Date of birth: 02/02/1991
Gross pay  £4,200.00
Income tax  -£800.00
National Insurance  -£350.00
Net pay  £3,050.00
""", "payslip", "2025-06", 3050.00, "GBP", "Demo Consulting Ltd", None, "en"),

    D("d23", "Steuerbescheid_2024.pdf", "pdf", "pdf", """
Steueramt Musterstadt
Einkommensteuerbescheid 2024
Datum: 12.09.2025
Steuernummer 123/456/78901
Steuer-ID 12 345 678 901

Festgesetzte Einkommensteuer         9.800,00 €
Vorauszahlungen                      8.900,00 €
Abschlusszahlung                       900,00 €
Rechtsbehelfsbelehrung: Einspruch innerhalb eines Monats.
""", "tax_document", "2025-09-12", 900.00, "EUR", "Steueramt Musterstadt", None),

    D("d24", "vat_return_ca3_2025-03.pdf", "pdf", "pdf", """
Atelier Exemple SARL
14 Rue de l'Exemple, 00000 Sampleville, France

VAT RETURN (CA3) - MARCH 2025
Filed on 18 April 2025

Taxable turnover at 20 %                EUR 96,500.00
VAT collected                           EUR 19,300.00
Deductible VAT on goods and services    EUR  7,215.40
VAT payable                             EUR 12,084.60
""", "tax_document", "2025-04-18", 12084.60, "EUR", "Atelier Exemple SARL", None, "en"),

    D("d25", "meeting_2025-03-04.md", "md", "text", """
# Weekly sync

Date: 2025-03-04
Attendees: Alex, Erika

- Discussed the roadmap for Q2
- Alex will send the invoice to the customer next week
- Follow up on the contract draft and the quote from the supplier
""", "other", "2025-03-04", None, None, None, None, "en"),

    D("d26", "invoice-process.md", "md", "text", """
# Invoice process

How we handle invoices and receipts: every invoice is checked against the order, then paid within 14 days.
Receipts are stored in the shared folder. Contracts are reviewed by legal. A reminder is sent when an invoice is overdue.
""", "other", None, None, None, None, None, "en"),

    D("d27", "rezept.txt", "txt", "text", """
Pfannkuchen
Zutaten: 250 g Mehl, 2 Eier, 500 ml Milch, eine Prise Salz.
Alles verrühren und in der Pfanne ausbacken.
""", "other", None, None, None, None, None),

    D("d28", "ledger.xlsx", "xlsx", "office", """
Date  Description  Debit  Credit  Balance
2025-01-03  Opening  0.00  0.00  1,000.00
2025-01-10  Office supplies  45.00  0.00  955.00
2025-01-15  Client payment  0.00  1,200.00  2,155.00
2025-01-20  Software  89.00  0.00  2,066.00
2025-01-28  Travel  230.00  0.00  1,836.00
2025-02-02  Client payment  0.00  800.00  2,636.00
""", "other", None, None, None, None, None, "en"),

    D("d29", "mail_invoice.eml", "eml", "email", """
Subject: Your invoice INV-2025-77 from Sample Trading Ltd
From: Sample Trading Billing <billing@sample-trading.example>
To: Alex Example <alex@example.com>
Date: Fri, 14 Mar 2025 09:12:00 +0100
Attachments: INV-2025-77.pdf

Hello,

please find attached invoice INV-2025-77 dated March 14, 2025.
Invoice no: INV-2025-77
Total due: EUR 480.00
Payment terms: 14 days.
""", "invoice", "2025-03-14", 480.00, "EUR", "Sample Trading Billing", "INV-2025-77", "en"),

    D("d30", "mail_reminder.eml", "eml", "email", """
Subject: Zahlungserinnerung zu Rechnung RE-2025-0042
From: "Beispiel Handels GmbH" <buchhaltung@beispiel-handels.example>
To: erika@example.com
Date: Mon, 21 Apr 2025 08:00:00 +0200

Guten Tag,

zu unserer Rechnung RE-2025-0042 über 255,85 EUR ist noch keine Zahlung eingegangen. Bitte begleichen Sie den offenen Betrag bis zum 30.04.2025.
Mahngebühr: 5,00 EUR
Gesamtforderung: 260,85 EUR
""", "reminder", "2025-04-21", 260.85, "EUR", "Beispiel Handels GmbH", None),

    D("d31", "newsletter.eml", "eml", "email", """
Subject: This month at Example Shop - new arrivals
From: Example Shop News <news@example-shop.example>
To: alex@example.com
Date: Wed, 05 Mar 2025 07:30:00 +0000

Hi there, spring is here! See our new arrivals and save 20% on selected items this week.
Unsubscribe at any time.
""", "other", "2025-03-05", None, None, "Example Shop News", None, "en"),

    D("d32", "mail_quote.eml", "eml", "email", """
Subject: Angebot A-2025-31
From: Testwerk KG <vertrieb@testwerk.example>
To: erika@example.com
Date: Wed, 02 Apr 2025 10:00:00 +0200
Attachments: Angebot_A-2025-31.pdf

Guten Tag,

anbei unser Angebot A-2025-31. Gesamt: 5.593,00 EUR, gültig bis 30.04.2025.
""", "quote", "2025-04-02", 5593.00, "EUR", "Testwerk KG", "A-2025-31"),

    D("d33", "id_page.pdf", "pdf", "pdf", """
PASSPORT
Surname: SAMPLE
Given names: ALEX
Nationality: EXAMPLELAND
Date of birth: 01 JAN 1990
Place of birth: SAMPLETON
Passport No: P0000000
P<EXLSAMPLE<<ALEX<<<<<<<<<<<<<<<<<<<<<<<<<<
P00000000<0EXL9001014M3001014<<<<<<<<<<<<<<00
""", "other", None, None, None, None, None, "en"),

    D("d34", "Rechnung_CH.pdf", "pdf", "pdf", """
Muster Handels AG, Beispielstrasse 3, 8000 Beispielstadt
Rechnung 2025-77
Datum 12.02.2025
Total CHF 1'234.50
Zahlbar bis 14.03.2025
""", "invoice", "2025-02-12", 1234.50, "CHF", "Muster Handels AG", "2025-77"),

    D("d35", "invoice_5521.pdf", "pdf", "pdf", """
Muestra Servicios S.A.
Invoice No. MS-5521
Invoice Date: 18 June 2025
Cloud migration support, 6 days         MXN 54,000.00
VAT 16 %                                MXN  8,640.00
Total Amount: MXN 62,640.00
""", "invoice", "2025-06-18", 62640.0, "MXN", "Muestra Servicios S.A.", "MS-5521", "en"),

    D("d36", "Rechnung_Eurowort.pdf", "pdf", "pdf", """
Beispiel Druck GmbH
Rechnung Nr. 20250614
Rechnungsdatum 14.06.2025
Visitenkarten 500 Stück
Summe 1 234,56 Euro
Bankverbindung: IBAN DE89 3704 0044 0532 0130 00 BIC EXAMPLEXXX
""", "invoice", "2025-06-14", 1234.56, "EUR", "Beispiel Druck GmbH", "20250614"),

    D("d37", "Rechnung_Tabelle.pdf", "pdf", "pdf", """
Musterbau GmbH
Rechnung
Rechnungsnummer   Rechnungsdatum   Fällig am
RE-1042           14.03.2025       13.04.2025
Position            Betrag
Bauleistung         1.000,00 €
Summe brutto        1.190,00 €
""", "invoice", "2025-03-14", 1190.00, "EUR", "Musterbau GmbH", "RE-1042"),

    D("d38", "Brief_Ort_Datum.pdf", "pdf", "pdf", """
Erika Beispiel
Musterweg 3
Musterstadt, den 14.03.2025

An die Beispiel Versicherung AG

Betreff: Kündigung meiner Hausratversicherung

Sehr geehrte Damen und Herren,
hiermit kündige ich meine Versicherung zum nächstmöglichen Zeitpunkt.
Mit freundlichen Grüßen
Erika Beispiel
""", "letter", "2025-03-14", None, None, "Erika Beispiel", None),

    D("d39", "Rechnung_zukunft.pdf", "pdf", "pdf", """
Testwerk KG
Rechnung Nr. T-900
Lieferdatum: 01.03.2025
Rechnungsdatum: 05.03.2025
Zahlungsziel: 31.12.2030
Gesamtbetrag: 99,90 €
""", "invoice", "2025-03-05", 99.90, "EUR", "Testwerk KG", "T-900"),

    D("d40", "notes_budget.txt", "txt", "text", """
Budget 2025
Marketing 12.000 €, Sales 8.000 €, Office 3.500 €. Total 23.500 €.
To do: ask for the quote, check the contract, pay the invoice.
""", "other", None, None, None, None, None),

    D("d41", "ramen_receipt.jpg", "jpg", "image", """
Sample Ramen House
2-3-4 Example Dori, Sampletown
2026/03/14 12:41
Shoyu ramen            JPY 980
Gyoza                  JPY 300
TOTAL                  JPY 1,280
Cash                   JPY 1,500
Change                 JPY 220
Thank you!
""", "receipt", "2026-03-14", 1280.0, "JPY", "Sample Ramen House", None, "en"),

    D("d42", "faktura_7781.pdf", "pdf", "pdf", """
Exempel Konsult AB
Provgatan 1, 111 22 Provstad

INVOICE
Invoice no: 7781
Invoice date: 2025-11-03
Due date: 2025-12-03

Consulting, 20 h                      SEK 15 000,00
VAT 25 %                              SEK  3 750,00
Total                                 SEK 18 750,00
""", "invoice", "2025-11-03", 18750.0, "SEK", "Exempel Konsult AB", "7781", "en"),

    D("d43", "coffee_ca.png", "png", "image", """
Example Roasters Inc.
123 Sample St, Exampleton
Receipt #00457
2025-07-19 08:12
Drip coffee large       CAD 3.25
Muffin                  CAD 3.75
Subtotal                CAD 7.00
GST 5%                  CAD 0.35
TOTAL                   CAD 7.35
VISA ************
""", "receipt", "2025-07-19", 7.35, "CAD", "Example Roasters Inc.", "00457", "en"),

    D("d44", "tax_invoice_INR.pdf", "pdf", "pdf", """
Sample Software Private Limited
Unit 5, Example Park, Sampleton

TAX INVOICE
Invoice No. SS/25-26/0412
Invoice Date: 12/09/2025

Annual licence                              ₹ 15,000.00
GST 18%                                      ₹ 2,700.00
Total Amount                                 ₹ 17,700.00
""", "invoice", "2025-09-12", 17700.0, "INR", "Sample Software Private Limited", "SS/25-26/0412", "en"),

    D("d45", "Rechnung_AT_2026-01.pdf", "pdf", "pdf", """
Beispiel Tischlerei GmbH
Holzgasse 4, 1010 Beispielstadt, Österreich

Rechnung Nr. 2026-0018
Rechnungsdatum: 15. Jänner 2026
Zahlungsziel: 14 Tage netto

Tisch Eiche massiv                   1.480,00 €
20 % USt                               296,00 €
Gesamtbetrag                         1.776,00 €
""", "invoice", "2026-01-15", 1776.0, "EUR", "Beispiel Tischlerei GmbH", "2026-0018", "de"),

    D("d46", "regning_2025-044.pdf", "pdf", "pdf", """
Eksempel Handel ApS
Prøvevej 5, 2100 Prøveby

FAKTURA
Fakturanr.: 2025-044
Dato: 2025-10-08

Varer                                DKK 4.200,00
Moms 25 %                            DKK 1.050,00
I alt                                DKK 5.250,00
Invoice total: DKK 5.250,00
""", "invoice", "2025-10-08", 5250.0, "DKK", "Eksempel Handel ApS", None, "en"),      # a label in a language the rules do not read: no number
]


def variants(doc: dict) -> list[tuple[str, str]]:
    """(label, text): the text as written, and as the indexer stores it (chunked, then joined with newlines)."""
    from mse import extract
    chunks = extract.chunk_text(doc["text"])
    return [("raw", doc["text"]), ("chunked", "\n".join(chunks))]
