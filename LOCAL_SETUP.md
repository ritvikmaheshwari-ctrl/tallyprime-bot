# Local Setup On Another PC

This bot does not need VS Code. It runs locally in your browser.

Main pages:

- `http://127.0.0.1:8765/` for bank statements and entry prep
- `http://127.0.0.1:8765/bills` for bill parsing and bill XML export
- `http://127.0.0.1:8765/setup` for company setup / masters helper

## Fast Setup

1. Copy the full `tallyprime_bot` folder to the other PC.
2. Install Python 3.11 or 3.12 from:

```text
https://www.python.org/downloads/windows/
```

3. During Python install, tick:

```text
Add python.exe to PATH
```

4. Double-click:

```text
install_bot.bat
```

5. Double-click:

```text
run_bot.bat
```

The bot opens in your browser locally on:

```text
http://127.0.0.1:8765
```

## What `install_bot.bat` does

- creates `.venv`
- upgrades `pip`
- installs everything from `requirements.txt`

Manual install command:

```powershell
python -m pip install -r requirements.txt
```

## Python Libraries Included

`requirements.txt` now includes the main Python-side libraries needed by the bot, including:

- pandas
- openpyxl
- xlrd
- pillow
- pdf2image
- pypdf
- easyocr

## Extra Windows Programs Still Needed

Some OCR/PDF features need Windows tools outside Python.

Install these on the other PC too:

```powershell
winget install UB-Mannheim.TesseractOCR
winget install oschwartz10612.Poppler
```

Then restart the bot.

## Old `.xls` File Support

If Microsoft Excel is installed, the bot can convert old `.xls` files to `.xlsx` automatically.

If Excel is not installed, use `.xlsx` or `.csv` when possible.

## Tally Import Order

For bill XML:

1. Import `1_import_masters.xml` as `Masters`
2. Import `2_import_vouchers.xml` as `Transactions / Vouchers`

For bank statement XML:

1. Import `required_masters.xml` once if needed
2. Import `tallyprime_import.xml` as `Transactions / Vouchers`

Always use the files from:

```text
latest_export
```

## How To Keep Another PC Updated

There are 3 realistic ways.

### Best way: Git

Use this if you want changes from this PC to be easy to move to the other PC.

1. Put this folder in a Git repository
2. Install Git on both PCs
3. Clone the same repository on both PCs
4. On the other PC, run:

```text
update_bot.bat
```

That will:

- pull latest code
- reinstall Python requirements if needed
- keep both PCs on the same version more reliably

### Simple way: shared folder / pen drive / OneDrive

If you do not want Git:

1. Keep this `tallyprime_bot` folder as your master copy
2. When you change code on this PC, copy the updated folder to the other PC
3. On the other PC, run:

```text
update_bot.bat
```

If that is the first time on the other PC, run:

```text
install_bot.bat
```

If needed, you can also reinstall libraries manually:

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Date Format Setting

Before processing a file, choose the matching date format from the dropdown inside the bot.

Supported options:

- `DD-MM-YYYY`
- `MM-DD-YYYY`
- `YYYY-MM-DD`
- `DD-YYYY-MM`
- `MM-YYYY-DD`
- `YYYY-DD-MM`

If the file mixes formats or you are unsure, use `Auto detect`.

## Clear Old Bot Data

If you want to delete old saved runs and start fresh:

```text
clear_history.bat
```

This removes old run/output/cache data only. It does not delete your source code.

### Most polished way later

If you want this to behave like a proper Windows app, the next step would be packaging it with PyInstaller into an `.exe`.

That is better when:

- many users will run it
- you want one-click launch
- you want controlled upgrades

## Recommended Folder To Copy

Copy this whole folder:

```text
tallyprime_bot
```

Do not copy only `app.py`. The batch files, docs, requirements, and export folders matter too.
