# TallyPrime Entry Prep Bot

A small local bot for preparing TallyPrime 7.0 voucher imports from bills and bank statements.

It does three things:

1. Accepts bills and bank statements in CSV, XLSX, TXT, and text-based PDF files.
2. Extracts likely dates, amounts, parties, voucher types, and narration.
3. Exports reviewable CSV, JSON, and a basic Tally XML voucher file.

Important: review all entries before importing them into TallyPrime. This bot prepares data; it does not directly post entries into your company books.

## Start

On a normal Windows PC, no VS Code is needed.

First install:

```text
install_bot.bat
```

Then start:

```text
run_bot.bat
```

Then open:

```text
http://127.0.0.1:8765
```

Full local setup notes are in `LOCAL_SETUP.md`.

## Use On Another PC

You can move this bot to another Windows PC without VS Code.

1. Copy the full `tallyprime_bot` folder to the other PC.
2. Install Python 3.11 or 3.12.
3. Run `install_bot.bat`.
4. Run `run_bot.bat`.

If you update code on this PC and want the other PC to match:

- best option: keep the folder in Git and run `update_bot.bat` on the other PC
- simple option: copy the updated full folder and run `update_bot.bat` or `install_bot.bat` again

Do not copy only `app.py`. Keep the batch files, requirements files, docs, and export folders together.

## Clear Old History

If the bot feels heavy and you want a fresh local state, run:

```text
clear_history.bat
```

This clears old `runs`, `uploads`, `tmp`, `latest_export`, and cache files without touching your code.

For automatic ledger setup, open:

```text
http://127.0.0.1:8765/setup
```

## TallyPrime 7.0 Workflow

- For bank statements, TallyPrime 7.0 already supports importing bank statements and auto-creating vouchers for supported banks.
- Use this bot when you want one review screen for bills plus bank statement rows, or when your input files need cleaning/classification first.
- Use the generated XML/JSON only after checking ledgers, voucher types, GST treatment, and narration.
- For first-time setup, use the Company Setup page to generate `tallyprime_masters.xml`, then import it in TallyPrime using `O: Import > Masters`.

## What Can Be Automated

- Ledger creation can be automated by importing `tallyprime_masters.xml`.
- Voucher preparation can be automated by uploading bills/statements and importing the reviewed XML/JSON.
- Company creation still needs to be confirmed in TallyPrime once, because Tally requires company-level details and acceptance inside the product.

## Supported Inputs

- `.csv`
- `.xlsx`
- `.xls`
- `.txt`
- `.pdf` with selectable text
- `.jpg`, `.jpeg`, `.png`, `.bmp`, `.tif`, `.tiff`, `.webp` when OCR is installed

Scanned bills/photos need Tesseract OCR and Poppler installed on Windows. See `LOCAL_SETUP.md`.

For 8 GB RAM PCs, keep the default lightweight `requirements.txt` install. Do not install `requirements-ocr-optional.txt` unless you really need the extra EasyOCR fallback.

## Outputs

Each run creates a dated folder under `runs/` containing:

- `review_entries.csv`
- `tallyprime_import.json`
- `tallyprime_import.xml`
- `1_import_masters.xml`
- `2_import_vouchers.xml`
- `source_vs_xml_check.csv`
- `import_match_summary.txt`
- `raw_extracts.json`

## Date Formats

Before processing a file, choose the source date order if the file uses one fixed format.

Supported options:

- `DD-MM-YYYY`
- `MM-DD-YYYY`
- `YYYY-MM-DD`
- `DD-YYYY-MM`
- `MM-YYYY-DD`
- `YYYY-DD-MM`

If you are unsure, leave it on Auto detect.
