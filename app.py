from __future__ import annotations

import csv
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, urlparse

import pandas as pd
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
from pdf2image import convert_from_path
from pypdf import PdfReader


BASE_DIR = Path(__file__).resolve().parent
RUNS_DIR = BASE_DIR / "runs"
UPLOADS_DIR = BASE_DIR / "uploads"
CLEANUP_DIR = BASE_DIR / "cleanup"
TMP_DIR = BASE_DIR / "tmp"
LATEST_EXPORT_DIR = BASE_DIR / "latest_export"
RUNS_DIR.mkdir(exist_ok=True)
UPLOADS_DIR.mkdir(exist_ok=True)
CLEANUP_DIR.mkdir(exist_ok=True)
TMP_DIR.mkdir(exist_ok=True)
LATEST_EXPORT_DIR.mkdir(exist_ok=True)

HOST = "127.0.0.1"
PORT = 8765
DEFAULT_BANK_LEDGER = "Bank"
DEFAULT_SUSPENSE_LEDGER = "Suspense"
DEFAULT_SUSPENSE_GROUP = "Suspense A/c"
MATCH_BANK_STATEMENT_COLUMNS = False
ACTIVE_FILES: dict[str, dict] = {}
LAST_RUN_DIR: Path | None = None
BILL_FILES: dict[str, dict] = {}
BILL_LAST_RUN_DIR: Path | None = None
BANK_LEDGER_NAME = DEFAULT_BANK_LEDGER
EASYOCR_READER = None
DATE_PARSE_MODE = "auto"


def stop_other_servers_on_port() -> None:
    current_pid = os.getpid()
    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except Exception:
        return
    pids: set[int] = set()
    for line in result.stdout.splitlines():
        if f":{PORT}" not in line or "LISTENING" not in line.upper():
            continue
        parts = line.split()
        if not parts:
            continue
        try:
            pid = int(parts[-1])
        except ValueError:
            continue
        if pid and pid != current_pid:
            pids.add(pid)
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, check=False, timeout=5)
        except Exception:
            pass


def xml_text(value: object) -> str:
    return html.escape(str(value), quote=False)


def xml_attr(value: object) -> str:
    return html.escape(str(value), quote=False).replace('"', "&quot;")


def current_date_parse_mode() -> str:
    return DATE_PARSE_MODE


def set_date_parse_mode(mode: str) -> None:
    global DATE_PARSE_MODE
    clean = (mode or "auto").strip().lower()
    if clean not in {"auto", "dmy", "mdy", "ymd", "dym", "myd", "ydm"}:
        clean = "auto"
    DATE_PARSE_MODE = clean


def date_format_options(selected: str = "auto") -> str:
    options = [
        ("auto", "Auto detect"),
        ("dmy", "DD-MM-YYYY / DD/MM/YYYY"),
        ("mdy", "MM-DD-YYYY / MM/DD/YYYY"),
        ("ymd", "YYYY-MM-DD / YYYY/MM/DD"),
        ("dym", "DD-YYYY-MM / DD/YYYY/MM"),
        ("myd", "MM-YYYY-DD / MM/YYYY/DD"),
        ("ydm", "YYYY-DD-MM / YYYY/DD/MM"),
    ]
    return "".join(
        f'<option value="{value}"{" selected" if value == selected else ""}>{label}</option>'
        for value, label in options
    )


def date_format_placeholder(mode: str = "auto") -> str:
    clean = (mode or "auto").strip().lower()
    mapping = {
        "auto": "dd-mm-yyyy / mm-dd-yyyy / yyyy-mm-dd",
        "dmy": "dd-mm-yyyy",
        "mdy": "mm-dd-yyyy",
        "ymd": "yyyy-mm-dd",
        "dym": "dd-yyyy-mm",
        "myd": "mm-yyyy-dd",
        "ydm": "yyyy-dd-mm",
    }
    return mapping.get(clean, mapping["auto"])


@dataclass
class Entry:
    source_file: str
    source_kind: str
    voucher_type: str
    date: str
    party_ledger: str
    debit_ledger: str
    credit_ledger: str
    amount: float
    narration: str
    confidence: str
    needs_review: str
    cgst_amount: float = 0.0
    sgst_amount: float = 0.0
    igst_amount: float = 0.0
    total_amount: float = 0.0
    inventory_items: list[dict] = field(default_factory=list)
    charge_lines: list[dict] = field(default_factory=list)
    voucher_number: str = ""


DATE_PATTERNS = [
    re.compile(r"\b(\d{1,2})[-/\.](\d{1,2})[-/\.](\d{2,4})\b"),
    re.compile(r"\b(\d{4})[-/\.](\d{1,2})[-/\.](\d{1,2})\b"),
]
MONTH_DATE_RE = re.compile(r"\b(\d{1,2})[-\s]([A-Za-z]{3,9})[-\s,](\d{2,4})\b", re.I)
AMOUNT_RE = re.compile(r"(?<!\w)(?:INR|Rs\.?|₹)?\s*([0-9]{1,3}(?:,[0-9]{2,3})*(?:\.[0-9]{1,2})?|[0-9]+(?:\.[0-9]{1,2})?)(?!\w)", re.I)
GSTIN_RE = re.compile(r"\b[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b")
BANK_HEADER_RE = re.compile(
    r"transaction\s+date|value\s+date|description/narration|withdrawal|deposit|debit|credit|"
    r"particulars|narration|closing\s+balance|opening\s+balance|account\s+statement",
    re.I,
)
BANK_TXN_START_RE = re.compile(r"^\s*\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}\s+\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}\s*$")
BANK_TXN_INLINE_RE = re.compile(r"\b\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}\s+\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}\b")
NUMBERED_TXN_START_RE = re.compile(r"^\s*(\d+)\s+(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})\s+(.*)$")
GENERIC_DATE_RE = re.compile(
    r"\b(?:\d{1,2}[-/\.]\d{1,2}[-/\.]\d{2,4}|\d{4}[-/\.]\d{1,2}[-/\.]\d{1,2}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4})\b"
)
GENERIC_TXN_START_RE = re.compile(
    r"^\s*(?:\d+\s+)?(?:\d{1,2}[-/\.]\d{1,2}[-/\.]\d{2,4}|\d{4}[-/\.]\d{1,2}[-/\.]\d{1,2}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4})\b"
)
BANK_NON_AMOUNT_RE = re.compile(
    r"reference\s+no|customer|account\s+type|account\s+number|statement|opening\s+balance|closing\s+balance|"
    r"address|ifsc|nominee|website|email|call\s+us|write\s+to\s+us|follow\s+us|page\s+\d+|toll-free|reg\.",
    re.I,
)


def _normalize_year_part(value: str) -> int:
    year = int(value)
    if year < 100:
        year += 2000
    return year


def parse_numeric_date_by_mode(text: str, mode: str = "auto", month_first: bool = False) -> str:
    raw = str(text).strip()
    match = re.fullmatch(r"(\d{1,4})[-/\.](\d{1,4})[-/\.](\d{1,4})", raw)
    if not match:
        return ""
    a, b, c = match.groups()
    try:
        if mode == "dmy":
            day, month, year = int(a), int(b), _normalize_year_part(c)
        elif mode == "mdy":
            month, day, year = int(a), int(b), _normalize_year_part(c)
        elif mode == "ymd":
            year, month, day = _normalize_year_part(a), int(b), int(c)
        elif mode == "dym":
            day, year, month = int(a), _normalize_year_part(b), int(c)
        elif mode == "myd":
            month, year, day = int(a), _normalize_year_part(b), int(c)
        elif mode == "ydm":
            year, day, month = _normalize_year_part(a), int(b), int(c)
        else:
            if len(a) == 4:
                year, month, day = _normalize_year_part(a), int(b), int(c)
            elif len(c) == 4 or len(c) <= 2:
                year = _normalize_year_part(c)
                month = int(a) if month_first else int(b)
                day = int(b) if month_first else int(a)
            else:
                return ""
        return datetime(year, month, day).strftime("%Y%m%d")
    except ValueError:
        return ""


def normalize_date(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y%m%d")
    text = str(value).strip()
    if not text or text.lower() in {"nan", "nat"}:
        return ""
    eight_digit = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", text)
    if eight_digit:
        return f"{eight_digit.group(1)}{eight_digit.group(2)}{eight_digit.group(3)}"
    explicit_numeric = parse_numeric_date_by_mode(text, current_date_parse_mode())
    if explicit_numeric:
        return explicit_numeric
    mode = current_date_parse_mode()
    if mode == "ymd":
        parsed = pd.to_datetime(text, yearfirst=True, dayfirst=False, errors="coerce")
    elif mode in {"mdy", "myd"}:
        parsed = pd.to_datetime(text, dayfirst=False, errors="coerce")
    else:
        parsed = pd.to_datetime(text, dayfirst=True, errors="coerce")
    if not pd.isna(parsed):
        return parsed.strftime("%Y%m%d")
    return ""


def normalize_numeric_date_text(text: str, month_first: bool = False) -> str:
    explicit = parse_numeric_date_by_mode(str(text).strip(), current_date_parse_mode(), month_first=month_first)
    if explicit:
        return explicit
    return parse_numeric_date_by_mode(str(text).strip(), "auto", month_first=month_first) or normalize_date(text)


def extract_report_period(text: str) -> tuple[str, str]:
    compact = re.sub(r"\s+", " ", text)
    patterns = [
        r"(\d{1,2}[-/\.]\d{1,2}[-/\.]\d{2,4})\s*(?:to|-)\s*(\d{1,2}[-/\.]\d{1,2}[-/\.]\d{2,4})",
        r"(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4})\s*(?:to|-)\s*(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4})",
    ]
    for pattern in patterns:
        match = re.search(pattern, compact, re.I)
        if not match:
            continue
        start = normalize_date(match.group(1))
        end = normalize_date(match.group(2))
        if start and end:
            return start, end
    return "", ""


def date_in_period(date_text: str, period_start: str, period_end: str) -> bool:
    return bool(date_text and period_start and period_end and period_start <= date_text <= period_end)


def choose_month_first_dates(samples: list[tuple[int, int, str]], period_start: str = "", period_end: str = "") -> bool:
    mode = current_date_parse_mode()
    if mode == "mdy":
        return True
    if mode == "dmy":
        return False
    if not samples:
        return False
    if period_start and period_end:
        month_first_hits = 0
        day_first_hits = 0
        for first, second, year_text in samples:
            raw = f"{first}/{second}/{year_text}"
            month_first_hits += 1 if date_in_period(normalize_numeric_date_text(raw, month_first=True), period_start, period_end) else 0
            day_first_hits += 1 if date_in_period(normalize_numeric_date_text(raw, month_first=False), period_start, period_end) else 0
        if month_first_hits != day_first_hits:
            return month_first_hits > day_first_hits
    first_values = {part[0] for part in samples}
    second_values = {part[1] for part in samples}
    return len(first_values) <= 2 and len(second_values) > len(first_values)


def money(value: object) -> float:
    if value is None:
        return 0.0
    text = str(value).replace(",", "").replace("₹", "").replace("INR", "").replace("Rs.", "").strip()
    try:
        return round(abs(float(text)), 2)
    except ValueError:
        return 0.0


def first_date_in_text(text: str) -> str:
    for pattern in DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        groups = match.groups()
        if len(groups[0]) == 4:
            raw = f"{groups[2]}/{groups[1]}/{groups[0]}"
        else:
            raw = f"{groups[0]}/{groups[1]}/{groups[2]}"
        date = normalize_date(raw)
        if date:
            return date
    month_match = MONTH_DATE_RE.search(text)
    if month_match:
        date = normalize_date(" ".join(month_match.groups()))
        if date:
            return date
    return ""


def largest_amount_in_text(text: str) -> float:
    amounts = []
    for match in AMOUNT_RE.finditer(text):
        raw = match.group(1).replace(",", "")
        if len(raw.split(".", 1)[0]) > 10:
            continue
        amounts.append(money(match.group(1)))
    amounts = [a for a in amounts if a > 0]
    return max(amounts) if amounts else 0.0


def amounts_in_text(text: str) -> list[float]:
    values: list[float] = []
    for match in AMOUNT_RE.finditer(text):
        digits = re.sub(r"\D", "", match.group(1))
        if len(digits) > 10:
            continue
        value = money(match.group(1))
        if value > 0:
            values.append(value)
    return values


def labeled_amount(text: str, labels: Iterable[str]) -> float:
    compact_text = re.sub(r"(?i)total\\s*amount", "total amount", text)
    compact_text = re.sub(r"(?i)tax\\s*amount", "tax amount", compact_text)
    compact_text = re.sub(r"(?i)atter", "after", compact_text)
    for label in labels:
        pattern = re.compile(
            rf"{label}[^\d]{{0,30}}((?:\d{{1,3}}(?:,\d{{2,3}})+|\d+)(?:\.\d{{1,2}})?)",
            re.I,
        )
        matches = pattern.findall(compact_text)
        if matches:
            return ocr_money(matches[-1], label)
    return 0.0


def ocr_money(value: object, label: str = "") -> float:
    amount = money(value)
    text = str(value).replace(",", "").strip()
    digits = re.sub(r"\D", "", text)
    if "." not in text and len(digits) in {5, 6} and re.search(r"tax|gst|cgst|sgst|igst", label, re.I):
        return round(float(digits) / 100, 2)
    return amount


def tax_label_amount(text: str, label: str) -> float:
    values = [
        ocr_money(value, label)
        for value in re.findall(
            rf"\b{label}\b[^\d]{{0,25}}((?:\d{{1,3}}(?:,\d{{2,3}})+|\d+)(?:\.\d{{1,2}})?)",
            text,
            re.I,
        )
    ]
    values = [value for value in values if value > 0]
    return round(max(values), 2) if values else 0.0


def bill_summary_taxable_amount(text: str) -> float:
    match = re.search(
        r"(?im)^Total:\s*\n\s*((?:\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d{1,2})?)",
        text,
    )
    return money(match.group(1)) if match else 0.0


def clean_item_name(name: str) -> str:
    name = name.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    name = re.sub(r"^\s*\d+\s+", "", name.strip())
    name = re.sub(r"\s+", " ", name)
    return name.strip(" -:|[]")[:120]


def parse_bill_inventory(text: str) -> tuple[list[dict], list[dict]]:
    lines = [line.strip(" []|") for line in text.splitlines() if line.strip(" []|")]
    items: list[dict] = []
    charges: list[dict] = []
    item_name_re = re.compile(r"^\d+\s*[|/]\s*(.+?)\s*$")
    hsn_re = re.compile(r"^\d{4,8}$")
    qty_re = re.compile(r"^(\d+(?:,\d+)*(?:\.\d+)?)\s*([A-Za-z]+)$")
    rate_re = re.compile(r"^\d+(?:,\d+)*(?:\.\d{1,2})?$")
    item_end_indexes: list[int] = []

    index = 0
    while index < len(lines):
        line = lines[index]
        name_match = item_name_re.match(line)
        if not name_match:
            index += 1
            continue
        name = clean_item_name(name_match.group(1))
        if not name or len(name) < 3:
            index += 1
            continue
        next_index = len(lines)
        for probe in range(index + 1, len(lines)):
            if item_name_re.match(lines[probe]) or re.search(r"loading|fright|freight|cgst|sgst|igst|total", lines[probe], re.I):
                next_index = probe
                break
        normal_window = lines[index + 1:min(next_index, index + 6)]
        split_window = lines[max(0, index - 4):index] + lines[index + 1:min(next_index, index + 4)]
        hsn = ""
        quantity = ""
        unit = ""
        rate = 0.0
        amount = 0.0

        def read_parts(parts: list[str]) -> tuple[str, str, str, float, float, int]:
            found_hsn = ""
            found_quantity = ""
            found_unit = ""
            found_rate = 0.0
            found_amount = 0.0
            numbers: list[tuple[int, float]] = []
            for pos, part in enumerate(parts):
                compact = part.replace(" ", "")
                if not found_hsn and hsn_re.fullmatch(compact):
                    found_hsn = compact
                    continue
                if hsn_re.fullmatch(compact):
                    continue
                if not found_quantity:
                    qty_match = qty_re.match(compact)
                    if qty_match:
                        found_quantity = qty_match.group(1).replace(",", "")
                        found_unit = qty_match.group(2)
                        continue
                if rate_re.fullmatch(part.replace(",", "")):
                    value = money(part)
                    if value:
                        numbers.append((pos, value))
            if len(numbers) >= 2:
                found_rate = numbers[-2][1]
                found_amount = numbers[-1][1]
            elif numbers:
                found_amount = numbers[-1][1]
            consumed = numbers[-1][0] + 1 if numbers else len(parts)
            return found_hsn, found_quantity, found_unit, found_rate, found_amount, consumed

        hsn, quantity, unit, rate, amount, consumed = read_parts(normal_window)
        if not (hsn and amount):
            hsn, quantity, unit, rate, amount, _split_consumed = read_parts(split_window)
            consumed = len(normal_window)
        if hsn and amount:
            item_end_indexes.append(min(next_index, index + 1 + consumed))
            items.append({
                "name": name,
                "hsn": hsn,
                "quantity": quantity,
                "unit": unit,
                "rate": rate,
                "amount": amount,
            })
            index += 1
        else:
            index += 1

    def add_charge(ledger: str, hsn: str, amount: float) -> None:
        if not ledger or not amount:
            return
        clean_ledger = normalize_charge_ledger(ledger)
        for charge in charges:
            if str(charge.get("ledger", "")).strip().lower() == clean_ledger.lower():
                charge["amount"] = amount
                if hsn and not charge.get("hsn"):
                    charge["hsn"] = hsn
                return
        charges.append({"ledger": clean_ledger, "hsn": hsn, "amount": amount})

    charge_start = max(item_end_indexes) if item_end_indexes else 0
    charge_end = len(lines)
    for probe in range(charge_start, len(lines)):
        if re.search(r"amount\s+chargeable|tax\s+amount|company|declaration|authorised|e-way|eway", lines[probe], re.I):
            charge_end = probe
            break

    for index, line in enumerate(lines[charge_start:charge_end], start=charge_start):
        lower = line.lower()
        if "loading" in lower and "cutting" in lower:
            amount = next((money(part) for part in lines[index + 1:index + 5] if money(part) and not hsn_re.fullmatch(part.replace(" ", ""))), 0.0)
            if amount:
                add_charge("Loading & Cutting Charges", "8428", amount)
        elif "fright" in lower or "freight" in lower:
            amount = next((money(part) for part in lines[index + 1:index + 5] if money(part) and not hsn_re.fullmatch(part.replace(" ", ""))), 0.0)
            if amount:
                add_charge("Freight", "9965", amount)
        elif re.search(r"r/?off|round\s*off|rioff|roff", lower):
            for part in lines[index + 1:index + 4]:
                raw = part.replace("(", "-").replace(")", "")
                amount = money(raw)
                if amount:
                    add_charge("Round Off", "", -amount if "-" in raw else amount)
                    break
        elif is_possible_charge_label(line):
            amount = 0.0
            hsn = ""
            for part in lines[index + 1:index + 5]:
                compact = part.replace(" ", "")
                if not hsn and hsn_re.fullmatch(compact):
                    hsn = compact
                    continue
                if money(part):
                    amount = money(part)
                    break
            if amount:
                if is_negative_charge_context(lines, index):
                    amount = -amount
                add_charge(line, hsn, amount)
    return items, charges


def normalize_charge_ledger(label: str) -> str:
    clean = re.sub(r"\([^)]*\)", "", label)
    clean = clean.replace("&amp;", "&")
    clean = re.sub(r"[^A-Za-z0-9&/ +.-]", " ", clean)
    clean = re.sub(r"\bFRIGHT\b", "Freight", clean, flags=re.I)
    clean = re.sub(r"\bR/?OFF\b|\bRIOFF\b|\bROFF\b", "Round Off", clean, flags=re.I)
    clean = re.sub(r"\s+", " ", clean).strip(" -:|")
    if re.search(r"round off", clean, re.I):
        return "Round Off"
    if re.search(r"freight", clean, re.I):
        return "Freight"
    if re.search(r"loading.*cutting|cutting.*loading", clean, re.I):
        return "Loading & Cutting Charges"
    return clean[:80] or "Other Charges"


def is_possible_charge_label(line: str) -> bool:
    lowered = line.lower()
    if len(line) < 3 or len(line) > 90:
        return False
    if re.search(r"cgst|sgst|igst|taxable|total|amount|quantity|rate|goods|services|description|hsn|sac|less|state|gstin|invoice|dated", lowered):
        return False
    if re.match(r"^\d+\s*[|/]", line):
        return False
    if AMOUNT_RE.fullmatch(line) or re.fullmatch(r"\d{4,8}", line.replace(" ", "")):
        return False
    return bool(re.search(r"[A-Za-z]", line))


def is_negative_charge_context(lines: list[str], index: int) -> bool:
    context = " ".join(lines[max(0, index - 3):index + 2]).lower()
    return "less" in context or "discount" in lines[index].lower()


def extract_bill_amounts(text: str) -> tuple[float, float, float, float, float]:
    total = labeled_amount(text, [
        "total\\s*amount\\s*after\\s*tax",
        "totl\\s*amount\\s*after\\s*tax",
        "d?totl\\s*amount\\s*after\\s*tax",
        "toal\\s*amount\\s*after\\s*tar",
        "grand\\s+total",
        "total\\s+amount",
        "net\\s+amount",
        "invoice\\s+value",
        "\\btotal\\b",
    ])
    if total and total < 100:
        total = 0.0
    if not total:
        after_tax_match = re.search(r"(?i)(?:totl|total|toal|dtotl)\s*amount\s*(?:after|ater)?\s*(?:tax|tar)?[^\d]{0,20}(\d{4,6})", text)
        if after_tax_match:
            total = money(after_tax_match.group(1))
    cgst = tax_label_amount(text, "CGST")
    sgst = tax_label_amount(text, "SGST")
    igst = tax_label_amount(text, "IGST")
    gst = round(cgst + sgst + igst, 2)
    if not gst:
        gst = labeled_amount(text, ["\\bGST\\b", "tax\\s+amount", "\\btax\\b"])
        if gst and not (cgst or sgst or igst):
            cgst = round(gst / 2, 2)
            sgst = round(gst / 2, 2)
    all_amounts = amounts_in_text(text)
    if not total and all_amounts:
        total = max(amount for amount in all_amounts if amount < 10000000) if any(amount < 10000000 for amount in all_amounts) else 0.0
    taxable = labeled_amount(text, ["taxable\\s+value", "basic\\s+amount", "sub\\s*total", "amount"])
    summary_taxable = bill_summary_taxable_amount(text)
    if summary_taxable:
        taxable = summary_taxable
    if not taxable and total and gst:
        taxable = round(total - gst, 2)
    if not taxable:
        taxable = total
    if (taxable == total or taxable < 100 or abs(total - (taxable + gst)) > 1) and total and gst and not summary_taxable:
        taxable = round(total - gst, 2)
    return taxable, cgst, sgst, igst, total or taxable


def guess_party_from_text(text: str, fallback: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for label in ("Supplier", "Vendor", "Bill From", "From", "Paid to", "Received from"):
        for line in lines:
            if label.lower() in line.lower() and ":" in line:
                return line.split(":", 1)[1].strip()[:80] or fallback
    for line in lines[:10]:
        if len(line) > 4 and not GSTIN_RE.search(line) and not AMOUNT_RE.search(line):
            return line[:80]
    return fallback


def current_bank_ledger() -> str:
    return BANK_LEDGER_NAME or DEFAULT_BANK_LEDGER


def set_bank_ledger_name(name: str) -> None:
    global BANK_LEDGER_NAME
    clean = name.strip()
    BANK_LEDGER_NAME = clean or DEFAULT_BANK_LEDGER


def classify_bank_row(row: dict, filename: str, bank_ledger: str | None = None) -> Entry:
    lower = {str(k).lower(): k for k in row.keys()}

    def pick(names: Iterable[str]) -> object:
        for name in names:
            for key_lower, original in lower.items():
                if name in key_lower:
                    return row.get(original)
        return ""

    date = normalize_date(pick(["date", "txn date", "value date"]))
    desc = str(pick(["description", "narration", "particular", "details", "remarks"])).strip()
    debit = money(pick(["withdrawal", "debit", "paid", "dr"]))
    credit = money(pick(["deposit", "credit", "received", "cr"]))
    amount_value = credit or debit or money(pick(["amount"]))
    raw_amount = str(pick(["amount"])).strip()
    direction = str(pick(["dr/cr", "cr/dr", "type", "transaction type", "debit/credit"])).lower()
    if not credit and not debit and raw_amount.startswith("-"):
        debit = amount_value
    if not credit and not debit and amount_value:
        if "cr" in direction or "credit" in direction or "deposit" in direction:
            credit = amount_value
        elif "dr" in direction or "debit" in direction or "withdraw" in direction:
            debit = amount_value
    voucher_type = "Receipt" if credit and not debit else "Payment"
    party = desc[:80] or "Unknown Party"
    needs_review = "No" if date and amount_value and desc else "Yes"
    confidence = "Medium" if needs_review == "No" else "Low"
    return Entry(
        source_file=filename,
        source_kind="Bank Statement",
        voucher_type=voucher_type,
        date=date,
        party_ledger=DEFAULT_SUSPENSE_LEDGER,
        debit_ledger=(bank_ledger or current_bank_ledger()) if voucher_type == "Receipt" else DEFAULT_SUSPENSE_LEDGER,
        credit_ledger=DEFAULT_SUSPENSE_LEDGER if voucher_type == "Receipt" else (bank_ledger or current_bank_ledger()),
        amount=amount_value,
        narration=desc or f"Imported from {filename}",
        confidence=confidence,
        needs_review=needs_review,
    )


def is_bank_statement_text(text: str) -> bool:
    lower = text.lower()
    has_bank_words = len(BANK_HEADER_RE.findall(text)) >= 2 and "balance" in lower
    has_transaction_shape = len(GENERIC_TXN_START_RE.findall(text)) >= 3 and bool(re.search(r"\b(debit|credit|withdrawal|deposit|dr\.?|cr\.?)\b", lower))
    return has_bank_words or has_transaction_shape


def infer_bank_ledger_name(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        if line.lower().startswith("au current account"):
            return line
    for idx, line in enumerate(lines):
        if line.lower() == "account type" and idx + 1 < len(lines):
            candidate = lines[idx + 1].strip()
            if candidate and not candidate.startswith(":") and "propert" not in candidate.lower():
                return candidate
    if "aubank.in" in text.lower() or "au current account" in text.lower():
        return "AU Bank"
    return DEFAULT_BANK_LEDGER


def parse_bank_amount_columns(line: str) -> tuple[float, float, float]:
    if BANK_NON_AMOUNT_RE.search(line):
        return 0.0, 0.0, 0.0
    normalized = line.replace("\u00a0", " ").replace("₹", " ")
    amount_token = r"(?:\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d{1,2})?"
    column_matches = re.findall(rf"(?<![\w])(-|{amount_token})(?![\w])", normalized)
    money_tokens = [token for token in column_matches if token == "-" or len(re.sub(r"\D", "", token)) <= 10]
    if len(money_tokens) < 3:
        return 0.0, 0.0, 0.0
    debit_token, credit_token, _balance_token = money_tokens[-3:]
    debit = 0.0 if debit_token == "-" else money(debit_token)
    credit = 0.0 if credit_token == "-" else money(credit_token)
    balance = 0.0 if _balance_token == "-" else money(_balance_token)
    return debit, credit, balance


def parse_bank_amount_line(line: str) -> tuple[float, float]:
    debit, credit, _balance = parse_bank_amount_columns(line)
    return debit, credit


def parse_numbered_amount_columns(line: str, previous_balance: float = 0.0) -> tuple[float, float, float]:
    normalized = line.replace("\u00a0", " ")
    for marker in (" Account No.", " Account Statement ", " End of Statement", " Any discrepancy", " Account Summary "):
        if marker in normalized:
            normalized = normalized.split(marker, 1)[0]
    amount_token = r"(?:\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d{1,2})"
    amounts = re.findall(amount_token, normalized)
    amounts = [a for a in amounts if len(re.sub(r"\D", "", a)) <= 10]
    if len(amounts) < 2:
        return 0.0, 0.0, 0.0
    balance = money(amounts[-1])
    amount_value = money(amounts[-2])
    if previous_balance:
        if balance > previous_balance:
            return 0.0, amount_value, balance
        if balance < previous_balance:
            return amount_value, 0.0, balance
    elif balance == amount_value:
        return 0.0, amount_value, balance
    desc_part = normalized[: normalized.rfind(amounts[-2])].upper()
    if "RECD:" in desc_part or " DEPOSIT" in desc_part or "/CR/" in desc_part:
        return 0.0, amount_value, balance
    if "DIRECT DEBIT" in desc_part or "NACH" in desc_part or "/DR/" in desc_part or "-DR-" in desc_part:
        return amount_value, 0.0, balance
    previous_balance_hint = 0.0
    return amount_value, 0.0, balance if balance or previous_balance_hint else balance


def trim_statement_footer(text: str) -> str:
    cleaned = text
    for marker in (" SUNIL SINGH SOLANKI Account No.", " Account No.", " Account Statement ", " End of Statement", " Any discrepancy", " Account Summary "):
        if marker in cleaned:
            cleaned = cleaned.split(marker, 1)[0]
    return cleaned.strip()


def likely_money_tokens(text: str) -> list[str]:
    normalized = text.replace("\u00a0", " ").replace("â‚¹", " ").replace("₹", " ")
    amount_token = r"(?:\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d{1,2})?"
    tokens = re.findall(rf"(?<![\w])({amount_token})(?![\w])", normalized)
    clean_tokens: list[str] = []
    for token in tokens:
        digits = re.sub(r"\D", "", token)
        if len(digits) > 10:
            continue
        if "." not in token and "," not in token and len(digits) > 6:
            continue
        clean_tokens.append(token)
    return clean_tokens


def amount_from_balance_change(amount_value: float, balance: float, previous_balance: float, text: str) -> tuple[float, float]:
    upper = text.upper()
    if previous_balance:
        if balance > previous_balance:
            return 0.0, amount_value
        if balance < previous_balance:
            return amount_value, 0.0
    if re.search(r"\b(CR|CREDIT|DEPOSIT|RECEIVED|RECD)\b|/CR/", upper):
        return 0.0, amount_value
    if re.search(r"\b(DR|DEBIT|WITHDRAWAL|PAID|PAYMENT|SENT)\b|/DR/|-DR-", upper):
        return amount_value, 0.0
    return amount_value, 0.0


def balance_from_label(text: str, label: str) -> float:
    pattern = re.compile(rf"{label}[^0-9-]*((?:\d{{1,3}}(?:,\d{{2,3}})+|\d+)(?:\.\d{{1,2}})?)", re.I)
    matches = pattern.findall(text)
    return money(matches[-1]) if matches else 0.0


def parse_generic_bank_amounts(text: str, previous_balance: float = 0.0) -> tuple[float, float, float]:
    if BANK_NON_AMOUNT_RE.search(text):
        return 0.0, 0.0, 0.0
    tokens = likely_money_tokens(text)
    if not tokens:
        return 0.0, 0.0, 0.0
    upper = text.upper()
    if len(tokens) >= 3:
        debit = money(tokens[-3])
        credit = money(tokens[-2])
        balance = money(tokens[-1])
        if debit and credit:
            debit, credit = amount_from_balance_change(credit or debit, balance, previous_balance, text)
        return debit, credit, balance
    if len(tokens) == 2:
        amount_value = money(tokens[-2])
        balance = money(tokens[-1])
        debit, credit = amount_from_balance_change(amount_value, balance, previous_balance, text)
        return debit, credit, balance
    amount_value = money(tokens[-1])
    debit, credit = amount_from_balance_change(amount_value, 0.0, previous_balance, text)
    if re.search(r"\bBAL(?:ANCE)?\b", upper):
        return 0.0, 0.0, amount_value
    return debit, credit, 0.0


def clean_generic_narration(text: str) -> str:
    narration = GENERIC_DATE_RE.sub(" ", text, count=1)
    tokens = likely_money_tokens(narration)
    for token in reversed(tokens[-4:]):
        narration = re.sub(rf"\s*{re.escape(token)}\s*$", "", narration).strip()
    narration = re.sub(r"\s+", " ", narration).strip(" -|:")
    return trim_statement_footer(narration)[:220]


def parse_generic_bank_statement_text(path: Path, text: str, bank_ledger_override: str = "") -> tuple[list[Entry], dict]:
    bank_ledger_name = bank_ledger_override.strip() or current_bank_ledger()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if GENERIC_TXN_START_RE.match(line) and likely_money_tokens(line) and not BANK_NON_AMOUNT_RE.search(line):
            if current:
                blocks.append(current)
            current = [line]
        elif current:
            if re.search(r"statement generated|end of statement|opening\s+balance|closing\s+balance|page\s+\d+", line, re.I):
                continue
            current.append(line)
    if current:
        blocks.append(current)

    opening_balance = balance_from_label(text, "opening\\s+balance")
    explicit_closing = balance_from_label(text, "closing\\s+balance")
    previous_balance = opening_balance
    closing_balance = opening_balance
    entries: list[Entry] = []

    for block in blocks:
        combined = " ".join(block)
        date_match = GENERIC_DATE_RE.search(combined)
        date = normalize_date(date_match.group(0)) if date_match else ""
        debit, credit, balance = parse_generic_bank_amounts(combined, previous_balance)
        if not (debit or credit):
            continue
        amount_value = credit or debit
        voucher_type = "Receipt" if credit else "Payment"
        narration = clean_generic_narration(combined) or f"Imported from {path.name}"
        entries.append(Entry(
            source_file=path.name,
            source_kind="Bank Statement",
            voucher_type=voucher_type,
            date=date,
            party_ledger=DEFAULT_SUSPENSE_LEDGER,
            debit_ledger=bank_ledger_name if voucher_type == "Receipt" else DEFAULT_SUSPENSE_LEDGER,
            credit_ledger=DEFAULT_SUSPENSE_LEDGER if voucher_type == "Receipt" else bank_ledger_name,
            amount=amount_value,
            narration=narration,
            confidence="Medium" if date and balance else "Low",
            needs_review="No" if date and amount_value else "Yes",
        ))
        if balance:
            previous_balance = balance
            closing_balance = balance

    return entries, {
        "file": path.name,
        "kind": "generic_bank_statement_text",
        "transaction_headers": len(blocks),
        "transactions": len(entries),
        "opening_balance": opening_balance,
        "closing_balance": explicit_closing or closing_balance,
        "bank_ledger_name": bank_ledger_name,
        "preview": text[:1000],
    }


def parse_numbered_bank_statement_text(path: Path, text: str, bank_ledger_override: str = "") -> tuple[list[Entry], dict]:
    bank_ledger_name = bank_ledger_override.strip() or current_bank_ledger()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    opening_balance = 0.0
    for line in lines:
        if "Opening Balance" in line:
            amounts = re.findall(r"(?:\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d{1,2})", line)
            if amounts:
                opening_balance = money(amounts[-1])
                break

    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if NUMBERED_TXN_START_RE.match(line):
            if current:
                blocks.append(current)
            current = [line]
        elif current:
            if line.startswith("Statement Generated") or line.startswith("Savings Account Transactions") or line.startswith("# Date"):
                continue
            current.append(line)
    if current:
        blocks.append(current)

    entries: list[Entry] = []
    closing_balance = opening_balance
    previous_balance = opening_balance
    for block in blocks:
        combined = " ".join(block)
        match = NUMBERED_TXN_START_RE.match(block[0])
        if not match:
            continue
        date = normalize_date(match.group(2))
        debit, credit, balance = parse_numbered_amount_columns(combined, previous_balance)
        if not (debit or credit):
            continue
        if debit and credit:
            if balance > previous_balance:
                debit, credit = 0.0, credit or debit
            else:
                debit, credit = debit or credit, 0.0
        amount_value = credit or debit
        voucher_type = "Receipt" if credit else "Payment"
        narration = trim_statement_footer(re.sub(r"^\s*\d+\s+\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}\s+", "", combined))
        narration = re.sub(r"\s+(?:\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d{1,2})(?:\s+(?:\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d{1,2})){1,2}\s*$", "", narration).strip()[:220]
        entries.append(Entry(
            source_file=path.name,
            source_kind="Bank Statement",
            voucher_type=voucher_type,
            date=date,
            party_ledger=DEFAULT_SUSPENSE_LEDGER,
            debit_ledger=bank_ledger_name if voucher_type == "Receipt" else DEFAULT_SUSPENSE_LEDGER,
            credit_ledger=DEFAULT_SUSPENSE_LEDGER if voucher_type == "Receipt" else bank_ledger_name,
            amount=amount_value,
            narration=narration or f"Imported from {path.name}",
            confidence="Medium" if date else "Low",
            needs_review="No" if date else "Yes",
        ))
        previous_balance = balance or previous_balance
        closing_balance = previous_balance

    return entries, {
        "file": path.name,
        "kind": "numbered_bank_statement_text",
        "transaction_headers": len(blocks),
        "transactions": len(entries),
        "opening_balance": opening_balance,
        "closing_balance": closing_balance,
        "bank_ledger_name": bank_ledger_name,
        "preview": text[:1000],
    }


def split_inline_transaction_lines(lines: list[str]) -> list[str]:
    split_lines: list[str] = []
    for line in lines:
        matches = list(BANK_TXN_INLINE_RE.finditer(line))
        if not matches or BANK_TXN_START_RE.match(line):
            split_lines.append(line)
            continue
        cursor = 0
        for match in matches:
            prefix = line[cursor:match.start()].strip()
            if prefix:
                split_lines.append(prefix)
            split_lines.append(match.group(0))
            cursor = match.end()
        suffix = line[cursor:].strip()
        if suffix:
            split_lines.append(suffix)
    return split_lines


def parse_bank_statement_text(path: Path, text: str, bank_ledger_override: str = "") -> tuple[list[Entry], dict]:
    if "Withdrawal (Dr.)" in text and "Deposit (Cr.)" in text:
        return parse_numbered_bank_statement_text(path, text, bank_ledger_override)
    lines = split_inline_transaction_lines([line.strip() for line in text.splitlines() if line.strip()])
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if BANK_TXN_START_RE.match(line):
            if current:
                blocks.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append(current)

    bank_ledger_name = bank_ledger_override.strip() or current_bank_ledger()
    entries: list[Entry] = []
    opening_balance = 0.0
    closing_balance = 0.0
    for block in blocks:
        date = normalize_date(" ".join(block[0].split()[:3]))
        amount_line = ""
        amount_index = -1
        debit = 0.0
        credit = 0.0
        for idx in range(len(block) - 1, -1, -1):
            candidate = block[idx]
            if not ("₹" in candidate or re.search(r"\d{1,3}(?:,\d{2,3})+\.\d{2}", candidate) or re.search(r"(?<!\d)\d+\.\d{2}(?!\d)", candidate)):
                continue
            candidate_debit, candidate_credit = parse_bank_amount_line(candidate)
            if candidate_debit or candidate_credit:
                amount_line = candidate
                amount_index = idx
                debit, credit = candidate_debit, candidate_credit
                if not opening_balance:
                    _debit, _credit, balance = parse_bank_amount_columns(candidate)
                    if balance:
                        opening_balance = round(balance + _debit - _credit, 2)
                _debit, _credit, balance = parse_bank_amount_columns(candidate)
                if balance:
                    closing_balance = balance
                break
        amount_value = credit or debit
        if not amount_value:
            continue
        narration_source = block[1:amount_index] if amount_index > 1 else block[1:]
        narration_lines = [line for line in narration_source if line != amount_line and not BANK_NON_AMOUNT_RE.search(line)]
        if not narration_lines and amount_line:
            narration_lines = [re.sub(r"\s+(?:-|\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d{1,2})?(?:\s+(?:-|\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d{1,2})?){1,2}\s*$", "", amount_line).strip()]
        narration = " ".join(line for line in narration_lines if line).strip()[:220] or f"Imported from {path.name}"
        party = narration[:80] or "Unknown Party"
        voucher_type = "Receipt" if credit and not debit else "Payment"
        entries.append(Entry(
            source_file=path.name,
            source_kind="Bank Statement",
            voucher_type=voucher_type,
            date=date,
            party_ledger=DEFAULT_SUSPENSE_LEDGER,
            debit_ledger=bank_ledger_name if voucher_type == "Receipt" else DEFAULT_SUSPENSE_LEDGER,
            credit_ledger=DEFAULT_SUSPENSE_LEDGER if voucher_type == "Receipt" else bank_ledger_name,
            amount=amount_value,
            narration=narration,
            confidence="Medium" if date else "Low",
            needs_review="No" if date else "Yes",
        ))

    raw = {
        "file": path.name,
        "kind": "bank_statement_text",
        "transaction_headers": len(blocks),
        "transactions": len(entries),
        "opening_balance": opening_balance,
        "closing_balance": closing_balance,
        "bank_ledger_name": bank_ledger_name,
        "preview": text[:1000],
    }
    if entries:
        return entries, raw
    return parse_generic_bank_statement_text(path, text, bank_ledger_override)


def extract_spreadsheet(path: Path, bank_ledger_override: str = "") -> tuple[list[Entry], dict]:
    if path.suffix.lower() == ".csv":
        df = pd.read_csv(path, dtype=str)
    else:
        df = pd.read_excel(path, dtype=str)
    bank_ledger_name = bank_ledger_override.strip() or current_bank_ledger()
    header_words = re.compile(r"date|particular|narration|description|withdrawal|deposit|debit|credit|amount|balance", re.I)
    recognized = sum(1 for column in df.columns if header_words.search(str(column)))
    if recognized < 2:
        probe = pd.read_csv(path, header=None, dtype=str) if path.suffix.lower() == ".csv" else pd.read_excel(path, header=None, dtype=str)
        for idx, row in probe.head(15).iterrows():
            values = [str(value).strip() for value in row.tolist()]
            if sum(1 for value in values if header_words.search(value)) >= 2:
                df = probe.iloc[idx + 1:].copy()
                df.columns = values
                break
    df = df.dropna(how="all")

    entries: list[Entry] = []
    balances: list[float] = []
    for _, row in df.iterrows():
        row_dict = {str(key).strip(): value for key, value in row.dropna().to_dict().items() if str(key).strip()}
        if not row_dict:
            continue
        entry = classify_bank_row(row_dict, path.name, bank_ledger_name)
        lower = {str(k).lower(): k for k in row_dict.keys()}
        balance_value = 0.0
        for key_lower, original in lower.items():
            if "balance" in key_lower:
                balance_value = money(row_dict.get(original))
                break
        if balance_value:
            balances.append(balance_value)
        if entry.amount > 0 and entry.date:
            entries.append(entry)

    opening_balance = 0.0
    closing_balance = balances[-1] if balances else 0.0
    if entries and balances:
        first = entries[0]
        first_balance = balances[0]
        opening_balance = round(first_balance + first.amount, 2) if first.voucher_type == "Payment" else round(first_balance - first.amount, 2)
    return entries, {
        "file": path.name,
        "kind": "table",
        "rows": len(entries),
        "columns": list(df.columns),
        "opening_balance": opening_balance,
        "closing_balance": closing_balance,
        "bank_ledger_name": bank_ledger_name,
    }


def extract_text_pdf(path: Path) -> str:
    reader = PdfReader(str(path))
    chunks = []
    for page in reader.pages:
        chunks.append(page.extract_text() or "")
    return "\n\f\n".join(chunks).strip()


def text_is_readable(text: str) -> bool:
    if not text.strip():
        return False
    if text.count("\ufffd") > max(10, len(text) * 0.03):
        return False
    alnum = len(re.findall(r"[A-Za-z0-9]", text))
    return alnum >= 40 and alnum / max(len(text), 1) >= 0.20


def tesseract_command() -> str:
    configured = shutil.which("tesseract")
    if configured:
        return configured
    common = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
    return str(common) if common.exists() else ""


def poppler_bin_path() -> str:
    pdftoppm = shutil.which("pdftoppm")
    if pdftoppm:
        return str(Path(pdftoppm).parent)
    package_root = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
    matches = list(package_root.glob("oschwartz10612.Poppler_*\\poppler-*\\Library\\bin\\pdftoppm.exe"))
    return str(matches[0].parent) if matches else ""


def tesseract_languages() -> str:
    command = tesseract_command()
    if not command:
        return "eng"
    try:
        result = subprocess.run([command, "--list-langs"], capture_output=True, text=True, check=False)
        langs = set(result.stdout.lower().split())
    except Exception:
        langs = set()
    return "eng+hin" if "hin" in langs else "eng"


def crop_non_white(image: Image.Image) -> Image.Image:
    gray = image.convert("L")
    mask = gray.point(lambda pixel: 255 if pixel < 245 else 0)
    bbox = mask.getbbox()
    if not bbox:
        return image
    left, top, right, bottom = bbox
    pad = 20
    left = max(left - pad, 0)
    top = max(top - pad, 0)
    right = min(right + pad, image.width)
    bottom = min(bottom + pad, image.height)
    return image.crop((left, top, right, bottom))


def prepare_ocr_image(image: Image.Image, angle: int) -> Image.Image:
    candidate = crop_non_white(image)
    candidate = candidate.rotate(angle, expand=True) if angle else candidate
    candidate = ImageOps.autocontrast(candidate.convert("L"))
    if candidate.width < 2200:
        scale = 2200 / max(candidate.width, 1)
        candidate = candidate.resize((int(candidate.width * scale), int(candidate.height * scale)), Image.Resampling.LANCZOS)
    candidate = candidate.filter(ImageFilter.SHARPEN)
    return candidate.point(lambda pixel: 255 if pixel > 170 else 0)


def easyocr_reader():
    global EASYOCR_READER
    if EASYOCR_READER is not None:
        return EASYOCR_READER
    try:
        import easyocr
        EASYOCR_READER = easyocr.Reader(["en"], gpu=False, verbose=False)
    except Exception:
        EASYOCR_READER = False
    return EASYOCR_READER or None


def easyocr_image(image: Image.Image) -> str:
    reader = easyocr_reader()
    if not reader:
        return ""
    base = crop_non_white(image.convert("RGB"))
    best_text = ""
    best_score = -1
    for angle in (0, 90, 180, 270):
        rotated = base.rotate(angle, expand=True) if angle else base
        if rotated.width < 2500:
            scale = 2500 / max(rotated.width, 1)
            rotated = rotated.resize((int(rotated.width * scale), int(rotated.height * scale)), Image.Resampling.LANCZOS)
        variants = [rotated]
        for variant in variants:
            token = uuid.uuid4().hex
            image_path = TMP_DIR / f"{token}_easyocr.png"
            try:
                variant.save(image_path)
                lines = reader.readtext(
                    str(image_path),
                    detail=0,
                    paragraph=False,
                    text_threshold=0.3,
                    low_text=0.2,
                    link_threshold=0.2,
                )
                text = "\n".join(str(line) for line in lines)
                score = len(re.findall(r"[A-Za-z0-9]", text))
                score += 100 * sum(word in text.lower() for word in ("total", "amount", "tax", "cgst", "sgst", "igst", "samsung", "oppo", "vivo", "bhawani"))
                if score > best_score:
                    best_score = score
                    best_text = text
            finally:
                try:
                    if image_path.exists():
                        image_path.unlink()
                except OSError:
                    pass
    return best_text


def ocr_image(image: Image.Image) -> str:
    command = tesseract_command()
    if not command:
        return easyocr_image(image)
    language = tesseract_languages()
    best_text = ""
    base_image = image.convert("RGB")
    for angle in (0, 90, 180, 270):
        token = uuid.uuid4().hex
        image_path = TMP_DIR / f"{token}_page.png"
        output_base = TMP_DIR / f"{token}_ocr"
        output_file = output_base.with_suffix(".txt")
        try:
            candidate = prepare_ocr_image(base_image, angle)
            candidate.save(image_path)
            subprocess.run(
                [command, str(image_path), str(output_base), "-l", language, "--psm", "11"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            text = output_file.read_text(encoding="utf-8", errors="ignore") if output_file.exists() else ""
            if len(text.strip()) > len(best_text.strip()):
                best_text = text
        finally:
            for path in (image_path, output_file):
                try:
                    if path.exists():
                        path.unlink()
                except OSError:
                    pass
    invoice_words = ("total", "amount", "tax", "cgst", "sgst", "igst", "invoice")
    if text_is_readable(best_text) and len(best_text.strip()) > 250 and any(word in best_text.lower() for word in invoice_words):
        return best_text
    easy_text = easyocr_image(image)
    if len(easy_text.strip()) > len(best_text.strip()) or any(word in easy_text.lower() for word in invoice_words):
        return easy_text
    return best_text


def extract_text_image(path: Path) -> str:
    try:
        with Image.open(path) as image:
            return ocr_image(image)
    except Exception:
        return ""


def extract_text_scanned_pdf(path: Path) -> str:
    if not tesseract_command():
        return ""
    poppler_path = poppler_bin_path()
    try:
        kwargs = {"dpi": 240, "first_page": 1, "last_page": 25}
        if poppler_path:
            kwargs["poppler_path"] = poppler_path
        pages = convert_from_path(str(path), **kwargs)
    except Exception:
        return ""
    return "\n\f\n".join(ocr_image(page) for page in pages).strip()


def extract_text_file(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        text = extract_text_pdf(path)
        return text if text_is_readable(text) else extract_text_scanned_pdf(path)
    if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}:
        return extract_text_image(path)
    return path.read_text(encoding="utf-8", errors="ignore")


def bill_entry_ledgers(entry_type: str, party: str) -> tuple[str, str, str]:
    kind = entry_type.strip().lower()
    party_ledger = party.strip() or DEFAULT_SUSPENSE_LEDGER
    if kind == "sale":
        return "Sales", party_ledger, "Sales Accounts"
    if kind == "expense":
        return "Payment", "Expense", party_ledger
    if kind == "asset":
        return "Purchase", "Fixed Assets", party_ledger
    return "Purchase", "Purchase Accounts", party_ledger


def bill_main_ledger_parent(voucher_type: str, ledger_name: str) -> str:
    clean = ledger_name.strip().lower()
    if clean in {"fixed assets", "asset", "assets"}:
        return "Fixed Assets"
    if clean in {"expense", "expenses"} or "expense" in clean:
        return "Purchase Accounts" if voucher_type == "Purchase" else "Indirect Expenses"
    if voucher_type == "Sales":
        return "Sales Accounts"
    return "Purchase Accounts"


def split_bill_texts(text: str) -> list[str]:
    def has_invoice_body(part: str) -> bool:
        item_rows = len(re.findall(r"(?m)^\s*\d+\s*[|/]\s*.+", part))
        return item_rows >= 1 and bool(re.search(r"(?i)\b(hsn|description|goods|amount chargeable|cgst|sgst|igst|total)\b", part))

    raw_parts = [part.strip() for part in re.split(r"(?i)(?=\bTax\s+Invoice\b)", text) if part.strip()]
    parts: list[str] = []
    for part in raw_parts:
        if has_invoice_body(part):
            parts.append(part)
        elif parts:
            parts[-1] = f"{parts[-1]}\n{part}"
    if len(parts) > 1:
        return parts
    page_parts = [part.strip() for part in text.split("\f") if part.strip()]
    good_pages = [part for part in page_parts if has_invoice_body(part)]
    return good_pages if len(good_pages) > 1 else [text]


def entry_from_bill_text(path: Path, text: str, entry_type: str = "purchase", source_name: str = "") -> Entry:
    amount, cgst_amount, sgst_amount, igst_amount, total_amount = extract_bill_amounts(text)
    inventory_items, charge_lines = parse_bill_inventory(text)
    if inventory_items:
        item_total = round(sum(float(item.get("amount", 0) or 0) for item in inventory_items), 2)
        taxable_charges = round(
            sum(float(charge.get("amount", 0) or 0) for charge in charge_lines if str(charge.get("ledger", "")).strip().lower() != "round off"),
            2,
        )
        if item_total:
            amount = round(item_total + taxable_charges, 2)
        known_total = round(
            item_total
            + sum(float(charge.get("amount", 0) or 0) for charge in charge_lines)
            + cgst_amount
            + sgst_amount
            + igst_amount,
            2,
        )
        has_round_off = any(str(charge.get("ledger", "")).strip().lower() == "round off" for charge in charge_lines)
        difference = round((total_amount or known_total) - known_total, 2)
        if not has_round_off and difference:
            charge_lines.append({"ledger": "Round Off", "hsn": "", "amount": difference})
    date = first_date_in_text(text)
    if not date and not re.search(r"total|amount|taxable|cgst|sgst|igst|invoice", text, re.I):
        amount = cgst_amount = sgst_amount = igst_amount = total_amount = 0.0
    if not date and total_amount and total_amount < 100:
        amount = cgst_amount = sgst_amount = igst_amount = total_amount = 0.0
    party = guess_party_from_text(text, path.stem)
    needs_review = "No" if total_amount and date and text.strip() else "Yes"
    confidence = "Medium" if needs_review == "No" else "Low"
    ocr_note = "OCR engine is not installed. Install Tesseract OCR, then upload this scanned/photo bill again."
    narration = " ".join(text.split())[:220] or ocr_note
    voucher_type, debit_ledger, credit_ledger = bill_entry_ledgers(entry_type, DEFAULT_SUSPENSE_LEDGER)
    return Entry(
        source_file=source_name or path.name,
        source_kind="Bill",
        voucher_type=voucher_type,
        date=date,
        party_ledger=DEFAULT_SUSPENSE_LEDGER,
        debit_ledger=debit_ledger,
        credit_ledger=credit_ledger,
        amount=amount,
        narration=narration,
        confidence=confidence,
        needs_review=needs_review,
        cgst_amount=cgst_amount,
        sgst_amount=sgst_amount,
        igst_amount=igst_amount,
        total_amount=total_amount,
        inventory_items=inventory_items,
        charge_lines=charge_lines,
    )


def extract_bill_spreadsheet(path: Path, entry_type: str = "purchase", sheet_name: str | int | None = None) -> tuple[list[Entry], dict]:
    def clean_cell(value: object) -> str:
        if value is None:
            return ""
        text = str(value).strip()
        return "" if text.lower() in {"", "nan", "nat", "none"} else text

    def looks_like_gstin(value: object) -> bool:
        return bool(GSTIN_RE.fullmatch(clean_cell(value).upper()))

    def usable_ledger_name(value: object) -> str:
        text = clean_cell(value)
        if not text or looks_like_gstin(text):
            return ""
        if normalize_date(text):
            return ""
        if re.fullmatch(r"[\d\s./\\-]+", text):
            return ""
        if re.search(r"\b(gstin|gst\s*no|bill\s*no|invoice\s*no|hsn|sac)\b", text, re.I):
            return ""
        return text

    def spreadsheet_frame(header: int | None = 0) -> pd.DataFrame:
        if path.suffix.lower() == ".csv":
            return pd.read_csv(path, header=header, dtype=str)
        return pd.read_excel(path, header=header, dtype=str, sheet_name=sheet_name)

    if path.suffix.lower() in {".xlsx", ".xls"} and sheet_name is None:
        try:
            sheet_names = pd.ExcelFile(path).sheet_names
        except Exception:
            sheet_names = []
        all_entries: list[Entry] = []
        raw_sheets: list[dict] = []
        for one_sheet in sheet_names:
            entries, raw = extract_bill_spreadsheet(path, entry_type, one_sheet)
            useful_entries = [entry for entry in entries if entry.amount or entry.total_amount or entry.date]
            if useful_entries:
                all_entries.extend(useful_entries)
                raw["sheet"] = one_sheet
                raw_sheets.append(raw)
        if all_entries:
            return all_entries, {
                "file": path.name,
                "kind": "multi_sheet_bill_table",
                "entry_type": entry_type,
                "rows": len(all_entries),
                "source_rows": sum(int(raw.get("source_rows", raw.get("rows", 0)) or 0) for raw in raw_sheets),
                "source_total": round(sum(float(raw.get("source_total", 0) or 0) for raw in raw_sheets), 2),
                "generated_total": round(sum((entry.total_amount or entry.amount) for entry in all_entries), 2),
                "sheets": raw_sheets,
            }
        if sheet_names:
            sheet_name = sheet_names[0]

    def gstr2b_entries(probe: pd.DataFrame) -> tuple[list[Entry], dict] | None:
        for header_idx, row in probe.head(30).iterrows():
            header_values = [clean_cell(value).lower() for value in row.tolist()]
            if "gstin of supplier" not in header_values or "trade/legal name" not in header_values:
                continue

            def header_index(names: Iterable[str]) -> int | None:
                for idx, value in enumerate(header_values):
                    text = value.strip()
                    if any(text == name or name in text for name in names):
                        return idx
                return None

            gstin_idx = header_index(["gstin of supplier"])
            party_idx = header_index(["trade/legal name", "trade name", "legal name"])
            invoice_idx = header_index(["invoice number", "invoice no", "bill no", "bill number"])
            date_idx = header_index(["invoice date", "bill date", "date"])
            invoice_value_idx = header_index(["invoice value", "total amount", "invoice amount"])
            taxable_value_idx = header_index(["taxable value", "taxable amount", "basic amount"])
            igst_idx = header_index(["integrated tax", "igst"])
            cgst_idx = header_index(["central tax", "cgst"])
            sgst_idx = header_index(["state/ut tax", "sgst", "utgst", "state tax"])

            entries: list[Entry] = []
            for _, data_row in probe.iloc[int(header_idx) + 2:].iterrows():
                values = data_row.tolist()
                supplier_gstin = clean_cell(values[gstin_idx] if gstin_idx is not None and gstin_idx < len(values) else "")
                party = usable_ledger_name(values[party_idx] if party_idx is not None and party_idx < len(values) else "") or DEFAULT_SUSPENSE_LEDGER
                invoice_no = clean_cell(values[invoice_idx] if invoice_idx is not None and invoice_idx < len(values) else "")
                date = normalize_date(values[date_idx] if date_idx is not None and date_idx < len(values) else "")
                total_amount = money(values[invoice_value_idx] if invoice_value_idx is not None and invoice_value_idx < len(values) else "")
                amount = money(values[taxable_value_idx] if taxable_value_idx is not None and taxable_value_idx < len(values) else "")
                igst_amount = money(values[igst_idx] if igst_idx is not None and igst_idx < len(values) else "")
                cgst_amount = money(values[cgst_idx] if cgst_idx is not None and cgst_idx < len(values) else "")
                sgst_amount = money(values[sgst_idx] if sgst_idx is not None and sgst_idx < len(values) else "")
                gst_amount = round(cgst_amount + sgst_amount + igst_amount, 2)
                if not amount and total_amount:
                    amount = round(total_amount - gst_amount, 2) if gst_amount else total_amount
                if not total_amount and (amount or gst_amount):
                    total_amount = round(amount + gst_amount, 2)
                if not (date or total_amount or amount):
                    continue
                voucher_type, debit_ledger, credit_ledger = bill_entry_ledgers(entry_type, party)
                narration_bits = [f"GSTR-2B invoice {invoice_no}" if invoice_no else "", f"GSTIN {supplier_gstin}" if supplier_gstin else ""]
                entries.append(Entry(
                    source_file=path.name,
                    source_kind="Bill",
                    voucher_type=voucher_type,
                    date=date,
                    party_ledger=party,
                    debit_ledger=debit_ledger,
                    credit_ledger=credit_ledger,
                    amount=amount,
                    narration=" ".join(bit for bit in narration_bits if bit)[:220] or f"Imported from {path.name}",
                    confidence="Medium" if date and total_amount else "Low",
                    needs_review="No" if date and total_amount else "Yes",
                    cgst_amount=cgst_amount,
                    sgst_amount=sgst_amount,
                    igst_amount=igst_amount,
                    total_amount=total_amount,
                ))
            return entries, {
                "file": path.name,
                "kind": "gstr2b_bill_table",
                "entry_type": entry_type,
                "rows": len(entries),
                "source_rows": len(entries),
                "source_total": round(sum((entry.total_amount or entry.amount) for entry in entries), 2),
                "generated_total": round(sum((entry.total_amount or entry.amount) for entry in entries), 2),
                "columns": [
                    "GSTIN of supplier",
                    "Trade/Legal name",
                    "Invoice number",
                    "Invoice type",
                    "Invoice Date",
                    "Invoice Value",
                    "Taxable Value",
                    "Integrated Tax",
                    "Central Tax",
                    "State/UT Tax",
                ],
            }
        return None

    def ledger_register_entries(probe: pd.DataFrame) -> tuple[list[Entry], dict] | None:
        def header_index(values: list[str], names: Iterable[str]) -> int | None:
            for idx, value in enumerate(values):
                text = value.lower().strip()
                if any(text == name or name in text for name in names):
                    return idx
            return None

        def party_from_particulars(text: str) -> str:
            clean = re.sub(r"\s+", " ", text).strip()
            match = re.search(r"\b(?:bill|invoice)\s*no\.?\s*[:#-]?\s*\S+\s+(.+)$", clean, re.I)
            if match:
                clean = match.group(1).strip()
            clean = re.sub(r"^\S*[/\\-]?\d+\S*\s+", "", clean).strip()
            return usable_ledger_name(clean) or DEFAULT_SUSPENSE_LEDGER

        for header_idx, row in probe.head(40).iterrows():
            values = [clean_cell(value) for value in row.tolist()]
            date_idx = header_index(values, ["date"])
            type_idx = header_index(values, ["type", "voucher type", "vch type"])
            particulars_idx = header_index(values, ["particulars", "particular", "narration", "description"])
            debit_idx = header_index(values, ["debit", "dr"])
            credit_idx = header_index(values, ["credit", "cr"])
            balance_idx = header_index(values, ["balance"])
            if date_idx is None or particulars_idx is None or debit_idx is None:
                continue
            if credit_idx is None and balance_idx is None:
                continue

            numeric_dates: list[tuple[int, int, int | str]] = []
            period_start, period_end = extract_report_period(" ".join(str(clean_cell(value)) for value in probe.head(20).fillna("").to_numpy().flatten()))
            for _, data_row in probe.iloc[int(header_idx) + 1:int(header_idx) + 60].iterrows():
                cells = data_row.tolist()
                raw_date = clean_cell(cells[date_idx] if date_idx < len(cells) else "")
                match = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", raw_date)
                if match:
                    numeric_dates.append((int(match.group(1)), int(match.group(2)), match.group(3)))
            month_first_dates = choose_month_first_dates(numeric_dates, period_start, period_end) if len(numeric_dates) >= 3 else False

            def register_date(value: object) -> str:
                text = clean_cell(value)
                if re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", text):
                    parsed = normalize_numeric_date_text(text, month_first=month_first_dates)
                    if parsed:
                        return parsed
                return normalize_date(value)

            entries: list[Entry] = []
            for _, data_row in probe.iloc[int(header_idx) + 1:].iterrows():
                cells = data_row.tolist()
                date = register_date(cells[date_idx] if date_idx < len(cells) else "")
                particulars = clean_cell(cells[particulars_idx] if particulars_idx < len(cells) else "")
                row_type = clean_cell(cells[type_idx] if type_idx is not None and type_idx < len(cells) else "").lower()
                debit = money(cells[debit_idx] if debit_idx < len(cells) else "")
                credit = money(cells[credit_idx] if credit_idx is not None and credit_idx < len(cells) else "")
                if not date or re.search(r"\b(opening|closing)\s+balance\b", particulars, re.I):
                    continue
                if not (debit or credit):
                    continue

                if "sale" in row_type:
                    voucher_type = "Sales"
                    party = party_from_particulars(particulars)
                    amount = debit or credit
                    debit_ledger = party
                    credit_ledger = "Sales Accounts"
                elif "purchase" in row_type:
                    voucher_type = "Purchase"
                    party = party_from_particulars(particulars)
                    amount = debit or credit
                    debit_ledger = "Purchase Accounts"
                    credit_ledger = party
                else:
                    row_entry_type = "sale" if entry_type.strip().lower() == "sale" else "purchase"
                    party = party_from_particulars(particulars)
                    voucher_type, debit_ledger, credit_ledger = bill_entry_ledgers(row_entry_type, party)
                    amount = debit or credit

                entries.append(Entry(
                    source_file=path.name,
                    source_kind="Bill",
                    voucher_type=voucher_type,
                    date=date,
                    party_ledger=party,
                    debit_ledger=debit_ledger,
                    credit_ledger=credit_ledger,
                    amount=amount,
                    narration=particulars[:220] or f"Imported from {path.name}",
                    confidence="Medium",
                    needs_review="No",
                    cgst_amount=0.0,
                    sgst_amount=0.0,
                    igst_amount=0.0,
                    total_amount=amount,
                ))
            if entries:
                return entries, {
                    "file": path.name,
                    "kind": "ledger_register_bill_table",
                    "entry_type": entry_type,
                    "rows": len(entries),
                    "source_rows": len(entries),
                    "source_total": round(sum((entry.total_amount or entry.amount) for entry in entries), 2),
                    "generated_total": round(sum((entry.total_amount or entry.amount) for entry in entries), 2),
                    "columns": values,
                }
        return None

    probe = spreadsheet_frame(header=None)
    gstr2b_result = gstr2b_entries(probe)
    if gstr2b_result is not None:
        return gstr2b_result
    ledger_register_result = ledger_register_entries(probe)
    if ledger_register_result is not None:
        return ledger_register_result

    df = spreadsheet_frame()
    header_words = re.compile(r"date|type|particular|debit|credit|balance|party|supplier|customer|vendor|ledger|amount|total|cgst|sgst|igst|central|state|integrated|trade|legal|narration|description|remarks|voucher", re.I)
    recognized = sum(1 for column in df.columns if header_words.search(str(column)))
    if recognized < 2:
        for idx, row in probe.head(15).iterrows():
            values = [str(value).strip() for value in row.tolist()]
            if sum(1 for value in values if header_words.search(value)) >= 2:
                df = probe.iloc[idx + 1:].copy()
                df.columns = values
                break
    df = df.dropna(how="all")
    entries: list[Entry] = []
    for _, row in df.iterrows():
        row_dict = {str(key).strip(): value for key, value in row.dropna().to_dict().items() if str(key).strip()}
        lower = {key.lower(): key for key in row_dict}

        def pick(names: Iterable[str], exclude: Iterable[str] = ()) -> object:
            excluded = tuple(term.lower() for term in exclude)
            for name in names:
                for key_lower, original in lower.items():
                    if name in key_lower and not any(term in key_lower for term in excluded):
                        return row_dict.get(original)
            return ""

        def pick_ledger(names: Iterable[str]) -> str:
            for name in names:
                for key_lower, original in lower.items():
                    if name not in key_lower:
                        continue
                    if any(term in key_lower for term in ("gst", "gstin", "uin", "pan", "tin", "bill", "invoice", "hsn", "sac", "state", "rate", "period", "source", "irn", "number", "no.")):
                        continue
                    ledger = usable_ledger_name(row_dict.get(original))
                    if ledger:
                        return ledger
            return ""

        def pick_money(names: Iterable[str], exclude: Iterable[str] = ()) -> float:
            value = pick(names, exclude)
            return money(value)

        def pick_tax(names: Iterable[str]) -> float:
            amount_words = ("amount", "amt", "tax", "value")
            for name in names:
                for key_lower, original in lower.items():
                    if name in key_lower and any(word in key_lower for word in amount_words) and not any(term in key_lower for term in ("rate", "%", "percent", "eligible", "available")):
                        amount = money(row_dict.get(original))
                        if amount:
                            return amount
            for name in names:
                for key_lower, original in lower.items():
                    if name in key_lower and not any(term in key_lower for term in ("rate", "%", "percent", "eligible", "available")):
                        amount = money(row_dict.get(original))
                        if amount:
                            return amount
            return 0.0

        date = normalize_date(pick(["date", "invoice date", "bill date", "voucher date"]))
        party = pick_ledger([
            "party ledger",
            "supplier ledger",
            "vendor ledger",
            "customer ledger",
            "party name",
            "supplier name",
            "vendor name",
            "customer name",
            "trade/legal name",
            "trade name",
            "legal name",
            "company name",
            "name of supplier",
            "name of vendor",
            "name of customer",
            "party",
            "supplier",
            "customer",
            "vendor",
            "name",
        ]) or DEFAULT_SUSPENSE_LEDGER
        amount = pick_money(["taxable value", "taxable amount", "basic amount", "sub total", "amount"], ["total", "tax", "cgst", "sgst", "igst", "cess", "rate", "%"])
        cgst_amount = pick_tax(["cgst", "central tax", "central gst"])
        sgst_amount = pick_tax(["sgst", "state/ut tax", "state tax", "utgst", "state gst"])
        igst_amount = pick_tax(["igst", "integrated tax", "integrated gst"])
        if not (cgst_amount or sgst_amount or igst_amount):
            gst_guess = pick_money(["gst amount", "tax amount", "tax"], ["rate", "%", "taxable"])
            if gst_guess:
                cgst_amount = round(gst_guess / 2, 2)
                sgst_amount = round(gst_guess - cgst_amount, 2)
        gst_amount = round(cgst_amount + sgst_amount + igst_amount, 2)
        total_amount = pick_money(["total amount", "grand total", "invoice value", "net amount", "bill amount", "total"], ["taxable", "tax amount", "cgst", "sgst", "igst"])
        if not total_amount:
            total_amount = round(amount + gst_amount, 2) if gst_amount else amount
        if not amount:
            amount = round(total_amount - gst_amount, 2) if gst_amount else total_amount
        narration = str(pick(["narration", "description", "remarks", "particular"])).strip() or f"Imported from {path.name}"
        row_entry_type = str(pick(["entry type", "voucher type", "type"])).strip().lower() or entry_type
        voucher_type, debit_ledger, credit_ledger = bill_entry_ledgers(row_entry_type, party)
        voucher_override = str(pick(["voucher type", "voucher"])).strip()
        debit_override = str(pick(["debit ledger", "debit account", "dr ledger", "dr account"])).strip()
        credit_override = str(pick(["credit ledger", "credit account", "cr ledger", "cr account"])).strip()
        if voucher_override:
            voucher_type = voucher_override
        if debit_override:
            debit_ledger = debit_override
        if usable_ledger_name(credit_override):
            credit_ledger = credit_override
        entries.append(Entry(
            source_file=path.name,
            source_kind="Bill",
            voucher_type=voucher_type,
            date=date,
            party_ledger=party,
            debit_ledger=debit_ledger,
            credit_ledger=credit_ledger,
            amount=amount,
            narration=narration[:220],
            confidence="Medium" if date and total_amount else "Low",
            needs_review="No" if date and total_amount else "Yes",
            cgst_amount=cgst_amount,
            sgst_amount=sgst_amount,
            igst_amount=igst_amount,
            total_amount=total_amount,
        ))
    entries = [entry for entry in entries if entry.amount > 0 or entry.date or entry.narration]
    return entries, {
        "file": path.name,
        "kind": "bill_table",
        "entry_type": entry_type,
        "rows": len(entries),
        "source_rows": len(entries),
        "source_total": round(sum((entry.total_amount or entry.amount) for entry in entries), 2),
        "generated_total": round(sum((entry.total_amount or entry.amount) for entry in entries), 2),
        "columns": list(df.columns),
    }


def extract_ledger_register_text(path: Path, text: str, entry_type: str = "purchase") -> tuple[list[Entry], dict] | None:
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    header_text = " ".join(lines[:30]).lower()
    if not all(word in header_text for word in ("date", "particular")):
        return None
    if not any(word in header_text for word in ("debit", "credit", "balance")):
        return None

    date_rows: list[tuple[int, int, str]] = []
    for line in lines[:80]:
        match = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b", line)
        if match:
            date_rows.append((int(match.group(1)), int(match.group(2)), match.group(3)))
    period_start, period_end = extract_report_period(" ".join(lines[:40]))
    month_first_dates = choose_month_first_dates(date_rows, period_start, period_end)

    def parse_register_date(raw: str) -> str:
        if re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", raw):
            parsed = normalize_numeric_date_text(raw, month_first=month_first_dates)
            if parsed:
                return parsed
        return normalize_date(raw)

    def party_from_particulars(particulars: str) -> str:
        clean = re.sub(r"\s+", " ", particulars).strip()
        match = re.search(r"\b(?:bill|invoice)\s*no\.?\s*[:#-]?\s*\S+\s+(.+)$", clean, re.I)
        if match:
            clean = match.group(1).strip()
        clean = re.sub(r"^\S*[/\\-]?\d+\S*\s+", "", clean).strip()
        if not clean or GSTIN_RE.fullmatch(clean.upper()) or AMOUNT_RE.search(clean):
            return DEFAULT_SUSPENSE_LEDGER
        return clean[:80]

    entries: list[Entry] = []
    row_re = re.compile(r"^(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\s+(\w+)\s+(.+?)\s+((?:\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d{1,2})?)(?:\s+((?:\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d{1,2})?)){0,2}\s*$")
    for line in lines:
        if re.search(r"\b(opening|closing)\s+balance\b", line, re.I):
            continue
        match = row_re.match(line)
        if not match:
            continue
        raw_date, raw_type, particulars, first_amount = match.group(1), match.group(2), match.group(3), match.group(4)
        date = parse_register_date(raw_date)
        amount = money(first_amount)
        if not date or not amount:
            continue
        row_type = raw_type.lower()
        party = party_from_particulars(particulars)
        if "sale" in row_type:
            voucher_type = "Sales"
            debit_ledger = party
            credit_ledger = "Sales Accounts"
        elif "purchase" in row_type:
            voucher_type = "Purchase"
            debit_ledger = "Purchase Accounts"
            credit_ledger = party
        else:
            voucher_type, debit_ledger, credit_ledger = bill_entry_ledgers(entry_type, party)
        entries.append(Entry(
            source_file=path.name,
            source_kind="Bill",
            voucher_type=voucher_type,
            date=date,
            party_ledger=party,
            debit_ledger=debit_ledger,
            credit_ledger=credit_ledger,
            amount=amount,
            narration=particulars[:220] or f"Imported from {path.name}",
            confidence="Low" if party == DEFAULT_SUSPENSE_LEDGER else "Medium",
            needs_review="Yes" if party == DEFAULT_SUSPENSE_LEDGER else "No",
            total_amount=amount,
        ))

    if not entries:
        return None
    return entries, {
        "file": path.name,
        "kind": "ledger_register_bill_text",
        "entry_type": entry_type,
        "rows": len(entries),
        "preview": text[:1000],
    }


def convert_xls_to_xlsx(path: Path) -> Path | None:
    excel_candidates = [
        Path(r"C:\Program Files\Microsoft Office\root\Office16\EXCEL.EXE"),
        Path(r"C:\Program Files (x86)\Microsoft Office\root\Office16\EXCEL.EXE"),
    ]
    if not any(candidate.exists() for candidate in excel_candidates):
        return None
    output = TMP_DIR / f"{path.stem}_{uuid.uuid4().hex}.xlsx"

    def ps_quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    script = f"""
$src = {ps_quote(str(path.resolve()))}
$dst = {ps_quote(str(output.resolve()))}
$excel = New-Object -ComObject Excel.Application
$excel.Visible = $false
$excel.DisplayAlerts = $false
try {{
  $workbook = $excel.Workbooks.Open($src)
  $workbook.SaveAs($dst, 51)
  $workbook.Close($false)
}} finally {{
  $excel.Quit()
  [System.Runtime.InteropServices.Marshal]::ReleaseComObject($excel) | Out-Null
}}
"""
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            check=True,
            timeout=90,
            capture_output=True,
            text=True,
        )
    except Exception:
        return None
    return output if output.exists() else None


def process_bill_file(path: Path, entry_type: str = "purchase") -> tuple[list[Entry], dict]:
    suffix = path.suffix.lower()
    if suffix in {".pdf", ".txt", ".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}:
        text = extract_text_file(path)
        ledger_register = extract_ledger_register_text(path, text, entry_type)
        if ledger_register is not None:
            return ledger_register
        bill_texts = split_bill_texts(text)
        entries = [
            entry_from_bill_text(
                path,
                bill_text,
                entry_type,
                source_name=path.name if len(bill_texts) == 1 else f"{path.name} / Bill {index + 1}",
            )
            for index, bill_text in enumerate(bill_texts)
        ]
        return entries, {
            "file": path.name,
            "kind": "bill_text",
            "entry_type": entry_type,
            "chars": len(text),
            "bills_detected": len(entries),
            "preview": text[:1000],
        }
    if suffix == ".xls":
        converted = convert_xls_to_xlsx(path)
        if converted:
            entries, raw = extract_bill_spreadsheet(converted, entry_type)
            raw["file"] = path.name
            raw["converted_from"] = ".xls"
            raw["converted_to"] = converted.name
            return entries, raw
    if suffix in {".csv", ".xlsx", ".xls"}:
        entries, raw = extract_bill_spreadsheet(path, entry_type)
        return entries, raw
    return [Entry(path.name, "Unsupported", "Journal", "", DEFAULT_SUSPENSE_LEDGER, DEFAULT_SUSPENSE_LEDGER, DEFAULT_SUSPENSE_LEDGER, 0, "Unsupported bill file type", "Low", "Yes")], {"file": path.name, "kind": "unsupported_bill"}


def process_manual_bill_file(path: Path, entry_type: str = "purchase") -> tuple[list[Entry], dict]:
    if path.suffix.lower() not in {".csv", ".xlsx", ".xls"}:
        return [Entry(path.name, "Unsupported", "Journal", "", DEFAULT_SUSPENSE_LEDGER, DEFAULT_SUSPENSE_LEDGER, DEFAULT_SUSPENSE_LEDGER, 0, "Manual bill upload accepts only CSV, XLSX, or XLS files.", "Low", "Yes")], {"file": path.name, "kind": "unsupported_manual_bill"}
    return process_bill_file(path, entry_type)


def process_generated_bill_file(path: Path, entry_type: str = "purchase") -> tuple[list[Entry], dict]:
    if path.suffix.lower() in {".csv", ".xlsx", ".xls"}:
        return [Entry(path.name, "Unsupported", "Journal", "", DEFAULT_SUSPENSE_LEDGER, DEFAULT_SUSPENSE_LEDGER, DEFAULT_SUSPENSE_LEDGER, 0, "Use the Manual bills Excel upload for spreadsheet files.", "Low", "Yes")], {"file": path.name, "kind": "unsupported_generated_bill"}
    return process_bill_file(path, entry_type)


def process_file(path: Path, bank_ledger_override: str = "") -> tuple[list[Entry], dict]:
    suffix = path.suffix.lower()
    if suffix in {".csv", ".xlsx", ".xls"}:
        return extract_spreadsheet(path, bank_ledger_override)
    if suffix in {".pdf", ".txt"}:
        text = extract_text_file(path)
        if is_bank_statement_text(text):
            return parse_bank_statement_text(path, text, bank_ledger_override)
        generic_entries, generic_raw = parse_generic_bank_statement_text(path, text, bank_ledger_override)
        if len(generic_entries) >= 2:
            return generic_entries, generic_raw
        return [entry_from_bill_text(path, text)], {"file": path.name, "kind": "text", "chars": len(text), "preview": text[:1000]}
    return [Entry(path.name, "Unsupported", "Journal", "", "Unknown Party", "Suspense", "Suspense", 0, "Unsupported file type", "Low", "Yes")], {"file": path.name, "kind": "unsupported"}


def tally_date(date_text: str) -> str:
    return date_text if re.fullmatch(r"\d{8}", date_text or "") else datetime.now().strftime("%Y%m%d")


def voucher_guid(entry: Entry) -> str:
    key = "|".join([
        "tallyprime-entry-prep-v2",
        entry.voucher_type,
        entry.date,
        entry.debit_ledger,
        entry.credit_ledger,
        f"{entry.amount:.2f}",
        f"{entry.cgst_amount:.2f}",
        f"{entry.sgst_amount:.2f}",
        f"{entry.igst_amount:.2f}",
        f"{entry.total_amount:.2f}",
        json.dumps(entry.inventory_items, sort_keys=True),
        json.dumps(entry.charge_lines, sort_keys=True),
        entry.voucher_number,
        entry.narration,
    ])
    return hashlib.md5(key.encode("utf-8")).hexdigest()


def voucher_amount(entry: Entry) -> float:
    return entry.total_amount if entry.source_kind == "Bill" and entry.total_amount else entry.amount


def signed_tally_amount(amount: float) -> str:
    return f"{amount:.2f}"


def export_entry(entry: Entry) -> Entry:
    if entry.source_kind == "Bank Statement":
        voucher_type = entry.voucher_type
        party_ledger = (entry.party_ledger or DEFAULT_SUSPENSE_LEDGER).strip() or DEFAULT_SUSPENSE_LEDGER
        debit_clean = (entry.debit_ledger or "").strip()
        credit_clean = (entry.credit_ledger or "").strip()
        bank_ledger = ""
        for candidate in (debit_clean, credit_clean):
            if candidate and candidate.lower() not in {DEFAULT_SUSPENSE_LEDGER.lower(), party_ledger.lower()}:
                bank_ledger = candidate
                break
        if not bank_ledger:
            bank_ledger = current_bank_ledger()
        if MATCH_BANK_STATEMENT_COLUMNS:
            debit_ledger = bank_ledger if entry.voucher_type == "Payment" else party_ledger
            credit_ledger = party_ledger if entry.voucher_type == "Payment" else bank_ledger
        else:
            debit_ledger = bank_ledger if entry.voucher_type == "Receipt" else party_ledger
            credit_ledger = party_ledger if entry.voucher_type == "Receipt" else bank_ledger
    else:
        voucher_type = entry.voucher_type
        debit_ledger = entry.debit_ledger
        credit_ledger = entry.credit_ledger
        party_ledger = (entry.party_ledger or DEFAULT_SUSPENSE_LEDGER).strip() or DEFAULT_SUSPENSE_LEDGER
    return Entry(
        source_file=entry.source_file,
        source_kind=entry.source_kind,
        voucher_type=voucher_type,
        date=entry.date,
        party_ledger=party_ledger,
        debit_ledger=debit_ledger,
        credit_ledger=credit_ledger,
        amount=entry.amount,
        narration=entry.narration,
        confidence=entry.confidence,
        needs_review=entry.needs_review,
        cgst_amount=entry.cgst_amount,
        sgst_amount=entry.sgst_amount,
        igst_amount=entry.igst_amount,
        total_amount=entry.total_amount,
        inventory_items=list(entry.inventory_items or []),
        charge_lines=list(entry.charge_lines or []),
        voucher_number=(bill_number_from_entry(entry) if entry.source_kind == "Bill" else (entry.voucher_number or "")).strip(),
    )


def item_unit(item: dict) -> str:
    raw = str(item.get("unit", "")).strip()
    key = re.sub(r"[^a-z0-9]", "", raw.lower())
    if key in {"kg", "kgs", "kgm", "kgram", "kilogram", "kilograms"}:
        return "KGS"
    if key in {"pc", "pcs", "piece", "pieces", "nos", "no", "number", "numbers"}:
        return "NOS"
    if key in {"box", "boxes"}:
        return "BOXES"
    return raw.upper() if raw else "NOS"


def item_quantity(item: dict) -> float:
    return float(str(item.get("quantity", "") or "0").replace(",", "") or 0)


def bill_tally_party(entry: Entry) -> str:
    party = (entry.party_ledger or DEFAULT_SUSPENSE_LEDGER).strip() or DEFAULT_SUSPENSE_LEDGER
    if party.lower() == DEFAULT_SUSPENSE_LEDGER.lower():
        return "Suspense Customer" if entry.voucher_type == "Sales" else "Suspense Supplier"
    return party


def inventory_line_xml(entry: Entry, item: dict) -> str:
    name = xml_text(str(item.get("name", "")).strip() or "Unknown Item")
    amount = float(item.get("amount", 0) or 0)
    unit = xml_text(item_unit(item))
    qty = item_quantity(item)
    rate = float(item.get("rate", 0) or 0)
    is_sale = entry.voucher_type == "Sales"
    is_deemed_positive = "No" if is_sale else "Yes"
    amount_value = amount if is_sale else -amount
    qty_value = -qty if is_sale else qty
    qty_xml = f"""
      <ACTUALQTY>{qty_value:.4f} {unit}</ACTUALQTY>
      <BILLEDQTY>{qty_value:.4f} {unit}</BILLEDQTY>""" if qty else ""
    rate_xml = f"\n      <RATE>{rate:.2f}/{unit}</RATE>" if rate else ""
    allocation_ledger = entry.credit_ledger if is_sale else entry.debit_ledger
    return f"""
    <ALLINVENTORYENTRIES.LIST>
      <STOCKITEMNAME>{name}</STOCKITEMNAME>
      <ISDEEMEDPOSITIVE>{is_deemed_positive}</ISDEEMEDPOSITIVE>{qty_xml}{rate_xml}
      <AMOUNT>{amount_value:.2f}</AMOUNT>
      <ACCOUNTINGALLOCATIONS.LIST>
        <LEDGERNAME>{xml_text(allocation_ledger)}</LEDGERNAME>
        <ISDEEMEDPOSITIVE>{is_deemed_positive}</ISDEEMEDPOSITIVE>
        <AMOUNT>{amount_value:.2f}</AMOUNT>
      </ACCOUNTINGALLOCATIONS.LIST>
    </ALLINVENTORYENTRIES.LIST>""".rstrip()


def ledger_entry_xml(ledger: str, amount: float) -> str:
    is_deemed_positive = "Yes" if amount < 0 else "No"
    return f"""
    <ALLLEDGERENTRIES.LIST>
      <LEDGERNAME>{xml_text(ledger)}</LEDGERNAME>
      <ISDEEMEDPOSITIVE>{is_deemed_positive}</ISDEEMEDPOSITIVE>
      <AMOUNT>{amount:.2f}</AMOUNT>
    </ALLLEDGERENTRIES.LIST>""".rstrip()


def accounting_ledger_entry_xml(ledger: str, amount: float, is_party: bool = False, bill_ref: str = "") -> str:
    is_deemed_positive = "Yes" if amount < 0 else "No"
    party_xml = "\n      <ISPARTYLEDGER>Yes</ISPARTYLEDGER>" if is_party else "\n      <ISPARTYLEDGER>No</ISPARTYLEDGER>"
    bill_xml = ""
    if is_party and bill_ref:
        bill_xml = f"""
      <BILLALLOCATIONS.LIST>
        <NAME>{xml_text(bill_ref)}</NAME>
        <BILLTYPE>New Ref</BILLTYPE>
        <AMOUNT>{amount:.2f}</AMOUNT>
      </BILLALLOCATIONS.LIST>"""
    return f"""
    <ALLLEDGERENTRIES.LIST>
      <LEDGERNAME>{xml_text(ledger)}</LEDGERNAME>
      <LEDGERFROMITEM>No</LEDGERFROMITEM>
      <REMOVEZEROENTRIES>No</REMOVEZEROENTRIES>
      <ISDEEMEDPOSITIVE>{is_deemed_positive}</ISDEEMEDPOSITIVE>
      <ISLASTDEEMEDPOSITIVE>{is_deemed_positive}</ISLASTDEEMEDPOSITIVE>{party_xml}
      <AMOUNT>{amount:.2f}</AMOUNT>
{bill_xml}
    </ALLLEDGERENTRIES.LIST>""".rstrip()


def bill_number_from_entry(entry: Entry) -> str:
    existing = (getattr(entry, "voucher_number", "") or "").strip()
    if existing:
        return existing[:80]
    text = entry.narration or ""
    patterns = [
        r"\bbill\s*no\.?\s*[:#-]?\s*([A-Za-z0-9][A-Za-z0-9/_-]*)",
        r"\binvoice\s*no\.?\s*[:#-]?\s*([A-Za-z0-9][A-Za-z0-9/_-]*)",
        r"\binv\.?\s*no\.?\s*[:#-]?\s*([A-Za-z0-9][A-Za-z0-9/_-]*)",
        r"\binvoice\s+([A-Za-z0-9][A-Za-z0-9/_-]*)(?:\s+GSTIN\b|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip()[:80]
    return ""


def bill_reference(entry: Entry) -> str:
    bill_no = bill_number_from_entry(entry)
    if bill_no:
        return bill_no
    match = re.search(r"\binvoice\s+(.+?)(?:\s+GSTIN\b|$)", entry.narration or "", re.IGNORECASE)
    if match:
        return match.group(1).strip()[:80]
    return hashlib.md5(voucher_guid(entry).encode("utf-8")).hexdigest()[:12].upper()


def bill_voucher_number(entry: Entry, run_id: str) -> str:
    key = f"{run_id}|{voucher_guid(entry)}"
    return "BILL-" + hashlib.md5(key.encode("utf-8")).hexdigest()[:12].upper()


def inventory_voucher_xml(entry: Entry, run_id: str = "") -> str:
    entry = export_entry(entry)
    voucher_no = bill_number_from_entry(entry) or bill_voucher_number(entry, run_id)
    voucher_type = entry.voucher_type
    narration = entry.narration
    party = bill_tally_party(entry)
    is_sale = voucher_type == "Sales"
    inventory_xml = "\n".join(inventory_line_xml(entry, item) for item in entry.inventory_items if float(item.get("amount", 0) or 0) > 0)
    party_amount = -voucher_amount(entry) if is_sale else voucher_amount(entry)
    party_xml = accounting_ledger_entry_xml(party, party_amount, is_party=True)
    ledger_parts: list[str] = []
    for charge in entry.charge_lines:
        ledger = str(charge.get("ledger", "")).strip()
        amount = float(charge.get("amount", 0) or 0)
        if ledger and amount:
            signed = abs(amount) if is_sale else -abs(amount)
            if amount < 0:
                signed = -signed
            ledger_parts.append(ledger_entry_xml(ledger, signed))
    if entry.cgst_amount:
        ledger_parts.append(ledger_entry_xml("Output CGST" if is_sale else "Input CGST", entry.cgst_amount if is_sale else -entry.cgst_amount))
    if entry.sgst_amount:
        ledger_parts.append(ledger_entry_xml("Output SGST" if is_sale else "Input SGST", entry.sgst_amount if is_sale else -entry.sgst_amount))
    if entry.igst_amount:
        ledger_parts.append(ledger_entry_xml("Output IGST" if is_sale else "Input IGST", entry.igst_amount if is_sale else -entry.igst_amount))
    ledger_xml_body = "\n".join(ledger_parts)
    return f"""
<TALLYMESSAGE xmlns:UDF="TallyUDF">
  <VOUCHER VCHTYPE="{xml_attr(voucher_type)}" ACTION="Create" OBJVIEW="Invoice Voucher View">
    <DATE>{tally_date(entry.date)}</DATE>
    <EFFECTIVEDATE>{tally_date(entry.date)}</EFFECTIVEDATE>
    <VOUCHERTYPENAME>{xml_text(voucher_type)}</VOUCHERTYPENAME>
    <VOUCHERNUMBER>{xml_text(voucher_no)}</VOUCHERNUMBER>
    <REFERENCE>{xml_text(voucher_no)}</REFERENCE>
    <REFERENCEDATE>{tally_date(entry.date)}</REFERENCEDATE>
    <PARTYLEDGERNAME>{xml_text(party)}</PARTYLEDGERNAME>
    <PERSISTEDVIEW>Invoice Voucher View</PERSISTEDVIEW>
    <ISINVOICE>Yes</ISINVOICE>
    <NARRATION>{xml_text(narration)}</NARRATION>
{party_xml}
{inventory_xml}
{ledger_xml_body}
  </VOUCHER>
</TALLYMESSAGE>""".strip()


def accounting_bill_voucher_xml(entry: Entry, run_id: str = "") -> str:
    entry = export_entry(entry)
    party_name = (entry.party_ledger or DEFAULT_SUSPENSE_LEDGER).strip() or DEFAULT_SUSPENSE_LEDGER
    voucher_type = "Sales" if entry.voucher_type == "Sales" else "Purchase"
    is_sale = voucher_type == "Sales"
    ref_name = bill_reference(entry)
    voucher_number = bill_number_from_entry(entry)
    voucher_number_xml = ""
    if voucher_number:
        voucher_number_xml = (
            f"\n    <VOUCHERNUMBER>{xml_text(voucher_number)}</VOUCHERNUMBER>"
            f"\n    <REFERENCE>{xml_text(voucher_number)}</REFERENCE>"
            f"\n    <REFERENCEDATE>{tally_date(entry.date)}</REFERENCEDATE>"
        )
    narration_parts = [entry.narration]
    for item in entry.inventory_items:
        item_bits = [
            str(item.get("name", "")).strip(),
            f"HSN {item.get('hsn', '')}".strip(),
            f"Qty {item.get('quantity', '')}".strip(),
            f"Rate {item.get('rate', '')}".strip(),
            f"Amount {float(item.get('amount', 0) or 0):.2f}",
        ]
        narration_parts.append(" | ".join(bit for bit in item_bits if bit and bit not in {"HSN", "Qty", "Rate"}))
    narration = " ; ".join(part for part in narration_parts if part).strip()[:900]
    item_total = round(sum(float(item.get("amount", 0) or 0) for item in entry.inventory_items), 2)
    tax_total = round(entry.cgst_amount + entry.sgst_amount + entry.igst_amount, 2)
    charge_total = round(sum(float(charge.get("amount", 0) or 0) for charge in entry.charge_lines), 2)
    if item_total:
        base_amount = item_total
    elif entry.amount:
        base_amount = entry.amount
    else:
        total_amount = voucher_amount(entry)
        base_amount = round(abs(total_amount) - abs(tax_total) - abs(charge_total), 2) if total_amount and tax_total else entry.amount
    total_amount = round(abs(base_amount) + abs(charge_total) + abs(tax_total), 2)
    ledger_amounts: list[tuple[str, float, bool]] = []
    if is_sale:
        ledger_amounts.append((party_name, -abs(total_amount), True))
        ledger_amounts.append((entry.credit_ledger, abs(base_amount), False))
    else:
        ledger_amounts.append((entry.debit_ledger, -abs(base_amount), False))
    for charge in entry.charge_lines:
        ledger = str(charge.get("ledger", "")).strip()
        amount = float(charge.get("amount", 0) or 0)
        if ledger and amount:
            signed = abs(amount) if is_sale else -abs(amount)
            if amount < 0:
                signed = -signed
            ledger_amounts.append((ledger, signed, False))
    if entry.cgst_amount:
        ledger_amounts.append(("Output CGST" if is_sale else "Input CGST", entry.cgst_amount if is_sale else -entry.cgst_amount, False))
    if entry.sgst_amount:
        ledger_amounts.append(("Output SGST" if is_sale else "Input SGST", entry.sgst_amount if is_sale else -entry.sgst_amount, False))
    if entry.igst_amount:
        ledger_amounts.append(("Output IGST" if is_sale else "Input IGST", entry.igst_amount if is_sale else -entry.igst_amount, False))
    if not is_sale:
        ledger_amounts.append((party_name, abs(total_amount), True))
    ledger_xml_body = "\n".join(
        accounting_ledger_entry_xml(ledger, amount, is_party=is_party, bill_ref=ref_name if is_party else "")
        for ledger, amount, is_party in ledger_amounts
        if ledger and abs(amount) >= 0.01
    )
    return f"""
<TALLYMESSAGE xmlns:UDF="TallyUDF">
  <VOUCHER VCHTYPE="{xml_attr(voucher_type)}" ACTION="Create" OBJVIEW="Accounting Voucher View">
    <DATE>{tally_date(entry.date)}</DATE>
    <EFFECTIVEDATE>{tally_date(entry.date)}</EFFECTIVEDATE>
    <VOUCHERTYPENAME>{xml_text(voucher_type)}</VOUCHERTYPENAME>{voucher_number_xml}
    <PARTYLEDGERNAME>{xml_text(party_name)}</PARTYLEDGERNAME>
    <PERSISTEDVIEW>Accounting Invoice View</PERSISTEDVIEW>
    <ISINVOICE>Yes</ISINVOICE>
    <NARRATION>{xml_text(narration)}</NARRATION>
{ledger_xml_body}
  </VOUCHER>
</TALLYMESSAGE>""".strip()


def voucher_xml(entry: Entry, run_id: str = "") -> str:
    entry = export_entry(entry)
    if entry.source_kind == "Bill":
        if entry.inventory_items:
            return inventory_voucher_xml(entry, run_id)
        return accounting_bill_voucher_xml(entry, run_id)
    guid = hashlib.md5(("voucher|" + run_id + "|" + voucher_guid(entry)).encode("utf-8")).hexdigest()
    voucher_type = entry.voucher_type
    narration = entry.narration
    amount_value = voucher_amount(entry)
    debit_amount = f"-{amount_value:.2f}"
    credit_amount = f"{amount_value:.2f}"
    return f"""
<TALLYMESSAGE xmlns:UDF="TallyUDF">
  <VOUCHER VCHTYPE="{html.escape(voucher_type)}" ACTION="Create" OBJVIEW="Accounting Voucher View">
    <DATE>{tally_date(entry.date)}</DATE>
    <GUID>{guid}</GUID>
    <VOUCHERTYPENAME>{html.escape(voucher_type)}</VOUCHERTYPENAME>
    <PARTYLEDGERNAME>{html.escape(entry.party_ledger or DEFAULT_SUSPENSE_LEDGER)}</PARTYLEDGERNAME>
    <NARRATION>{html.escape(narration)}</NARRATION>
    <ALLLEDGERENTRIES.LIST>
      <LEDGERNAME>{html.escape(entry.debit_ledger)}</LEDGERNAME>
      <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
      <AMOUNT>{debit_amount}</AMOUNT>
    </ALLLEDGERENTRIES.LIST>
    <ALLLEDGERENTRIES.LIST>
      <LEDGERNAME>{html.escape(entry.credit_ledger)}</LEDGERNAME>
      <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
      <AMOUNT>{credit_amount}</AMOUNT>
    </ALLLEDGERENTRIES.LIST>
  </VOUCHER>
</TALLYMESSAGE>""".strip()


def ledger_xml(name: str, parent: str, billwise: bool = False, opening_balance: float = 0.0, action: str = "Create") -> str:
    clean_name = html.escape(name.strip())
    clean_parent = html.escape(parent.strip())
    billwise_text = "Yes" if billwise else "No"
    opening_xml = f"\n    <OPENINGBALANCE>-{opening_balance:.2f}</OPENINGBALANCE>" if opening_balance else ""
    return f"""
<TALLYMESSAGE xmlns:UDF="TallyUDF">
  <LEDGER NAME="{clean_name}" ACTION="{html.escape(action)}">
    <NAME>{clean_name}</NAME>
    <PARENT>{clean_parent}</PARENT>
    <ISBILLWISEON>{billwise_text}</ISBILLWISEON>
    <AFFECTSSTOCK>No</AFFECTSSTOCK>
    <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>{opening_xml}
  </LEDGER>
</TALLYMESSAGE>""".strip()


def unit_xml(symbol: str) -> str:
    clean = xml_attr(symbol.strip())
    clean_text = xml_text(symbol.strip())
    return f"""
<TALLYMESSAGE xmlns:UDF="TallyUDF">
  <UNIT NAME="{clean}" RESERVEDNAME="" ACTION="Create">
    <NAME>{clean_text}</NAME>
    <ISSIMPLEUNIT>Yes</ISSIMPLEUNIT>
    <DECIMALPLACES>4</DECIMALPLACES>
  </UNIT>
</TALLYMESSAGE>""".strip()


def stock_group_xml(name: str = "Imported Stock Items") -> str:
    clean = xml_attr(name.strip())
    clean_text = xml_text(name.strip())
    return f"""
<TALLYMESSAGE xmlns:UDF="TallyUDF">
  <STOCKGROUP NAME="{clean}" ACTION="Create">
    <NAME>{clean_text}</NAME>
    <PARENT></PARENT>
    <ISSUBLEDGER>No</ISSUBLEDGER>
    <ISADDABLE>Yes</ISADDABLE>
  </STOCKGROUP>
</TALLYMESSAGE>""".strip()


def stock_item_xml(item: dict) -> str:
    name_attr = xml_attr(str(item.get("name", "")).strip())
    name = xml_text(str(item.get("name", "")).strip())
    hsn = xml_text(str(item.get("hsn", "")).strip())
    unit = xml_text(item_unit(item))
    gst_xml = f"\n    <GSTAPPLICABLE>&#4; Applicable</GSTAPPLICABLE>" if hsn else ""
    hsn_xml = f"\n    <HSNCODE>{hsn}</HSNCODE>" if hsn else ""
    return f"""
<TALLYMESSAGE xmlns:UDF="TallyUDF">
  <STOCKITEM NAME="{name_attr}" ACTION="Create">
    <NAME>{name}</NAME>
    <PARENT>Imported Stock Items</PARENT>
    <BASEUNITS>{unit}</BASEUNITS>{gst_xml}{hsn_xml}
  </STOCKITEM>
</TALLYMESSAGE>""".strip()


def voucher_type_manual_xml(name: str) -> str:
    clean_attr = xml_attr(name.strip())
    clean_text = xml_text(name.strip())
    return f"""
<TALLYMESSAGE xmlns:UDF="TallyUDF">
  <VOUCHERTYPE NAME="{clean_attr}" RESERVEDNAME="{clean_attr}" ACTION="Alter">
    <NAME>{clean_text}</NAME>
    <PARENT>{clean_text}</PARENT>
    <NUMBERINGMETHOD>Manual</NUMBERINGMETHOD>
    <USEZEROENTRIES>No</USEZEROENTRIES>
    <ISACTIVE>Yes</ISACTIVE>
  </VOUCHERTYPE>
</TALLYMESSAGE>""".strip()


def write_setup_outputs(form: dict[str, list[str]]) -> Path:
    run_dir = RUNS_DIR / f"setup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=True)

    def field(name: str, default: str = "") -> str:
        return (form.get(name, [default])[0] or default).strip()

    def names(name: str) -> list[str]:
        text = field(name)
        return [part.strip() for part in text.split(",") if part.strip()]

    company = field("company_name", "My Company")
    bank = field("bank_ledger", "Bank")
    suppliers = names("supplier_ledgers")
    customers = names("customer_ledgers")

    ledgers = [
        {"name": bank, "parent": "Bank Accounts", "billwise": False},
        {"name": "Purchase Accounts", "parent": "Purchase Accounts", "billwise": False},
        {"name": "Purchase Expenses", "parent": "Purchase Accounts", "billwise": False},
    ]
    ledgers.extend({"name": name, "parent": "Sundry Creditors", "billwise": True} for name in suppliers)
    ledgers.extend({"name": name, "parent": "Sundry Debtors", "billwise": True} for name in customers)

    profile = {
        "company_name": company,
        "note": "Create/select this company in TallyPrime first, then import tallyprime_masters.xml as Masters.",
        "ledgers": ledgers,
    }
    (run_dir / "company_setup.json").write_text(json.dumps(profile, indent=2), encoding="utf-8")
    xml_body = "\n".join(ledger_xml(item["name"], item["parent"], bool(item["billwise"])) for item in ledgers)
    xml = f"""<ENVELOPE>
  <HEADER>
    <TALLYREQUEST>Import Data</TALLYREQUEST>
  </HEADER>
  <BODY>
    <IMPORTDATA>
      <REQUESTDESC>
        <REPORTNAME>All Masters</REPORTNAME>
      </REQUESTDESC>
      <REQUESTDATA>
{xml_body}
      </REQUESTDATA>
    </IMPORTDATA>
  </BODY>
</ENVELOPE>
"""
    (run_dir / "tallyprime_masters.xml").write_text(xml, encoding="utf-8")
    return run_dir


def raw_metric(raw: dict, key: str) -> float:
    value = raw.get(key, 0) or 0
    try:
        total = float(value)
    except (TypeError, ValueError):
        total = 0.0
    if total:
        return total
    for sheet in raw.get("sheets", []) or []:
        if isinstance(sheet, dict):
            total += raw_metric(sheet, key)
    return total


def write_outputs(entries: list[Entry], raw_extracts: list[dict]) -> Path:
    run_dir = RUNS_DIR / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    run_dir.mkdir(parents=True, exist_ok=True)
    run_id = f"{run_dir.name}-{uuid.uuid4().hex}"
    entries = [export_entry(entry) for entry in entries]
    rows = [asdict(e) for e in entries]
    with (run_dir / "review_entries.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else list(Entry.__dataclass_fields__.keys()))
        writer.writeheader()
        writer.writerows(rows)
    (run_dir / "tallyprime_import.json").write_text(json.dumps({"vouchers": rows}, indent=2), encoding="utf-8")
    (run_dir / "raw_extracts.json").write_text(json.dumps(raw_extracts, indent=2), encoding="utf-8")
    bill_entries = [entry for entry in entries if entry.source_kind == "Bill"]
    if bill_entries:
        taxable_total = round(sum(entry.amount for entry in bill_entries), 2)
        cgst_total = round(sum(entry.cgst_amount for entry in bill_entries), 2)
        sgst_total = round(sum(entry.sgst_amount for entry in bill_entries), 2)
        igst_total = round(sum(entry.igst_amount for entry in bill_entries), 2)
        tax_total = round(cgst_total + sgst_total + igst_total, 2)
        invoice_total = round(taxable_total + tax_total, 2)
        party_totals: dict[str, float] = {}
        voucher_counts: dict[str, int] = {}
        month_totals: dict[str, float] = {}
        month_counts: dict[str, int] = {}
        dates = []
        for entry in bill_entries:
            party = bill_tally_party(entry) or entry.party_ledger or "Suspense"
            party_totals[party] = round(party_totals.get(party, 0.0) + voucher_amount(entry), 2)
            voucher_counts[entry.voucher_type] = voucher_counts.get(entry.voucher_type, 0) + 1
            if entry.date:
                dates.append(entry.date)
                month_key = entry.date[:6]
                month_totals[month_key] = round(month_totals.get(month_key, 0.0) + (entry.total_amount or entry.amount), 2)
                month_counts[month_key] = month_counts.get(month_key, 0) + 1
        primary_voucher_type = "Sales" if voucher_counts.get("Sales", 0) >= voucher_counts.get("Purchase", 0) else "Purchase"
        main_ledger_label = "Sales Accounts" if primary_voucher_type == "Sales" else "Purchase Accounts"
        tax_prefix = "Output" if primary_voucher_type == "Sales" else "Input"
        party_total_label = "Customer ledger total" if primary_voucher_type == "Sales" else "Supplier ledger total"
        match_summary = {
            "bill_vouchers_generated": len(bill_entries),
            "voucher_counts": voucher_counts,
            "date_from": min(dates) if dates else "",
            "date_to": max(dates) if dates else "",
            "source_rows": int(sum(raw_metric(raw, "source_rows") for raw in raw_extracts)),
            "source_total": round(sum(raw_metric(raw, "source_total") for raw in raw_extracts), 2),
            "xml_rows": len(bill_entries),
            "xml_total": round(sum((entry.total_amount or entry.amount) for entry in bill_entries), 2),
            "month_counts": dict(sorted(month_counts.items())),
            "month_totals": dict(sorted(month_totals.items())),
            "main_ledger_label": main_ledger_label,
            "tax_prefix": tax_prefix,
            "party_total_label": party_total_label,
            "main_ledger_total": taxable_total,
            "cgst_total": cgst_total,
            "sgst_total": sgst_total,
            "igst_total": igst_total,
            "gst_total": tax_total,
            "party_ledger_total": invoice_total,
            "party_totals": dict(sorted(party_totals.items())),
            "tally_check_note": (
                f"{main_ledger_label} ledger shows the uploaded taxable total. "
                f"{party_total_label} is {main_ledger_label} + {tax_prefix} CGST + {tax_prefix} SGST + {tax_prefix} IGST."
            ),
        }
        match_summary["source_row_difference"] = match_summary["xml_rows"] - match_summary["source_rows"]
        match_summary["source_total_difference"] = round(match_summary["xml_total"] - match_summary["source_total"], 2)
        (run_dir / "import_match_summary.json").write_text(json.dumps(match_summary, indent=2), encoding="utf-8")
        with (run_dir / "source_vs_xml_check.csv").open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["check", "source", "xml", "difference", "status"])
            writer.writeheader()
            writer.writerow({
                "check": "rows",
                "source": match_summary["source_rows"],
                "xml": match_summary["xml_rows"],
                "difference": match_summary["source_row_difference"],
                "status": "OK" if match_summary["source_row_difference"] == 0 else "MISMATCH",
            })
            writer.writerow({
                "check": "total_amount",
                "source": f"{match_summary['source_total']:.2f}",
                "xml": f"{match_summary['xml_total']:.2f}",
                "difference": f"{match_summary['source_total_difference']:.2f}",
                "status": "OK" if abs(match_summary["source_total_difference"]) < 0.01 else "MISMATCH",
            })
        summary_lines = [
            "IMPORT MATCH SUMMARY",
            f"Bill vouchers generated: {len(bill_entries)}",
            f"Voucher counts: {', '.join(f'{k}={v}' for k, v in sorted(voucher_counts.items()))}",
            f"Date range: {match_summary['date_from']} to {match_summary['date_to']}",
            "",
            "Source vs generated XML:",
            f"Source rows: {match_summary['source_rows']} | XML rows: {match_summary['xml_rows']} | Difference: {match_summary['source_row_difference']}",
            f"Source total: {match_summary['source_total']:.2f} | XML total: {match_summary['xml_total']:.2f} | Difference: {match_summary['source_total_difference']:.2f}",
            "",
            "Month totals generated:",
        ]
        summary_lines.extend(
            f"{month}: rows={month_counts.get(month, 0)} total={amount:.2f}"
            for month, amount in sorted(month_totals.items())
        )
        summary_lines.extend([
            "",
            "Tally ledger totals to verify:",
            f"{main_ledger_label} total: {taxable_total:.2f}",
            f"{tax_prefix} CGST total: {cgst_total:.2f}",
            f"{tax_prefix} SGST total: {sgst_total:.2f}",
            f"{tax_prefix} IGST total: {igst_total:.2f}",
            f"GST total: {tax_total:.2f}",
            f"{party_total_label}: {invoice_total:.2f}",
            "",
            "Party totals:",
        ])
        summary_lines.extend(f"{name}: {amount:.2f}" for name, amount in sorted(party_totals.items()))
        summary_lines.extend([
            "",
            f"Note: {main_ledger_label} does not include GST. Compare the uploaded taxable value with {main_ledger_label}.",
        ])
        (run_dir / "import_match_summary.txt").write_text("\n".join(summary_lines), encoding="utf-8")
    opening_balance = next((float(raw.get("opening_balance", 0) or 0) for raw in raw_extracts if raw.get("opening_balance")), 0.0)
    opening_bank_ledger = next((raw.get("bank_ledger_name") for raw in raw_extracts if raw.get("opening_balance") and raw.get("bank_ledger_name")), current_bank_ledger())
    pdf_closing_balance = next((float(raw.get("closing_balance", 0) or 0) for raw in raw_extracts if raw.get("closing_balance")), 0.0)
    transaction_headers = sum(int(raw.get("transaction_headers", 0) or 0) for raw in raw_extracts)
    payment_total = round(sum(entry.amount for entry in entries if entry.voucher_type == "Payment"), 2)
    receipt_total = round(sum(entry.amount for entry in entries if entry.voucher_type == "Receipt"), 2)
    computed_closing = round(opening_balance - payment_total + receipt_total, 2)
    reconciliation = {
        "entries_generated": len(entries),
        "pdf_transaction_headers": transaction_headers,
        "payments_total": payment_total,
        "receipts_total": receipt_total,
        "opening_balance": opening_balance,
        "computed_closing_balance": computed_closing,
        "pdf_last_closing_balance": pdf_closing_balance,
        "difference": round(computed_closing - pdf_closing_balance, 2) if pdf_closing_balance else "",
    }
    with (run_dir / "reconciliation_summary.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(reconciliation.keys()))
        writer.writeheader()
        writer.writerow(reconciliation)
    (run_dir / "reconciliation_summary.json").write_text(json.dumps(reconciliation, indent=2), encoding="utf-8")
    ledger_names: dict[str, str] = {}
    stock_items: dict[str, dict] = {}
    units: set[str] = set()
    bank_statement_ledgers = {
        ledger.strip()
        for entry in entries
        if entry.source_kind == "Bank Statement"
        for ledger in (entry.debit_ledger, entry.credit_ledger)
        if ledger.strip() and ledger.strip().lower() != (entry.party_ledger or DEFAULT_SUSPENSE_LEDGER).strip().lower()
    }
    for entry in entries:
        for ledger_name in (entry.debit_ledger, entry.credit_ledger, entry.party_ledger):
            clean = ledger_name.strip()
            if not clean:
                continue
            if clean.lower() == DEFAULT_SUSPENSE_LEDGER.lower():
                ledger_names[clean] = DEFAULT_SUSPENSE_GROUP
            elif clean in bank_statement_ledgers or clean.lower() in {"bank", "cash"}:
                ledger_names[clean] = "Bank Accounts" if clean.lower() != "cash" else "Cash-in-Hand"
            elif entry.source_kind == "Bank Statement" and clean.lower() == (entry.party_ledger or "").strip().lower():
                ledger_names[clean] = DEFAULT_SUSPENSE_GROUP
            elif clean.lower() in {"purchase accounts", "purchase expenses"}:
                ledger_names[clean] = "Purchase Accounts"
            elif clean.lower() == "sales accounts":
                ledger_names[clean] = "Sales Accounts"
            elif clean.lower() == "expense":
                ledger_names[clean] = "Indirect Expenses"
            elif clean.lower() == "fixed assets":
                ledger_names[clean] = "Fixed Assets"
            elif entry.source_kind == "Bill" and clean.lower() == (entry.party_ledger or "").strip().lower() and entry.voucher_type == "Sales":
                ledger_names[clean] = "Sundry Debtors"
            elif entry.source_kind == "Bill" and clean.lower() == (entry.party_ledger or "").strip().lower():
                ledger_names[clean] = "Sundry Creditors"
            elif entry.source_kind == "Bill" and clean.lower() == (entry.debit_ledger or "").strip().lower() and entry.voucher_type == "Purchase":
                ledger_names[clean] = bill_main_ledger_parent(entry.voucher_type, clean)
            elif entry.source_kind == "Bill" and clean.lower() == (entry.credit_ledger or "").strip().lower() and entry.voucher_type == "Sales":
                ledger_names[clean] = bill_main_ledger_parent(entry.voucher_type, clean)
            elif entry.voucher_type == "Receipt":
                ledger_names[clean] = "Sundry Debtors"
            elif entry.voucher_type == "Purchase":
                ledger_names[clean] = "Sundry Creditors"
            else:
                ledger_names[clean] = "Sundry Creditors"
        if entry.source_kind == "Bill":
            mapped_party = bill_tally_party(entry)
            if mapped_party:
                ledger_names[mapped_party] = "Sundry Debtors" if entry.voucher_type == "Sales" else "Sundry Creditors"
            if entry.cgst_amount:
                ledger_names["Output CGST" if entry.voucher_type == "Sales" else "Input CGST"] = "Duties & Taxes"
            if entry.sgst_amount:
                ledger_names["Output SGST" if entry.voucher_type == "Sales" else "Input SGST"] = "Duties & Taxes"
            if entry.igst_amount:
                ledger_names["Output IGST" if entry.voucher_type == "Sales" else "Input IGST"] = "Duties & Taxes"
            for charge in entry.charge_lines:
                ledger = str(charge.get("ledger", "")).strip()
                if ledger:
                    ledger_names[ledger] = "Indirect Expenses" if ledger.lower() != "round off" else "Indirect Expenses"
            for item in entry.inventory_items:
                name = str(item.get("name", "")).strip()
                if name:
                    stock_items[name] = item
                unit = item_unit(item)
                if unit:
                    units.add(unit)
    ledger_masters_body = "\n".join(
        ledger_xml(
            name,
            parent,
            billwise=parent in {"Sundry Creditors", "Sundry Debtors"},
            opening_balance=opening_balance if name == opening_bank_ledger else 0.0,
        )
        for name, parent in sorted(ledger_names.items())
    )
    inventory_masters_body = "\n".join(
        [unit_xml(unit) for unit in sorted(units)] +
        ([stock_group_xml()] if stock_items else []) +
        [stock_item_xml(item) for _, item in sorted(stock_items.items())]
    )
    voucher_type_masters_body = "\n".join(
        voucher_type_manual_xml(voucher_type)
        for voucher_type in sorted({entry.voucher_type for entry in entries if entry.source_kind == "Bill" and entry.voucher_type in {"Sales", "Purchase"}})
    )
    masters_body = "\n".join(part for part in (ledger_masters_body, inventory_masters_body, voucher_type_masters_body) if part)
    masters_xml = f"""<ENVELOPE>
  <HEADER>
    <TALLYREQUEST>Import Data</TALLYREQUEST>
  </HEADER>
  <BODY>
    <IMPORTDATA>
      <REQUESTDESC>
        <REPORTNAME>All Masters</REPORTNAME>
      </REQUESTDESC>
      <REQUESTDATA>
{masters_body}
      </REQUESTDATA>
    </IMPORTDATA>
  </BODY>
</ENVELOPE>
    """
    (run_dir / "required_masters.xml").write_text(masters_xml, encoding="utf-8")
    (run_dir / "1_import_masters.xml").write_text(masters_xml, encoding="utf-8")
    if opening_balance:
        update_bank_xml = f"""<ENVELOPE>
  <HEADER>
    <TALLYREQUEST>Import Data</TALLYREQUEST>
  </HEADER>
  <BODY>
    <IMPORTDATA>
      <REQUESTDESC>
        <REPORTNAME>All Masters</REPORTNAME>
      </REQUESTDESC>
      <REQUESTDATA>
{ledger_xml(next((raw.get("bank_ledger_name") for raw in raw_extracts if raw.get("opening_balance") and raw.get("bank_ledger_name")), current_bank_ledger()), "Bank Accounts", opening_balance=opening_balance, action="Alter")}
      </REQUESTDATA>
    </IMPORTDATA>
  </BODY>
</ENVELOPE>
"""
        (run_dir / "update_bank_opening.xml").write_text(update_bank_xml, encoding="utf-8")
    opening_alter_body = ""
    if opening_balance:
        opening_alter_body = ledger_xml(opening_bank_ledger, "Bank Accounts", opening_balance=opening_balance, action="Alter")
    xml_body = "\n".join(
        voucher_xml(e, run_id)
        for e in entries
        if e.amount > 0
    )
    import_body = "\n".join(part for part in (opening_alter_body, xml_body) if part)
    xml = f"""<ENVELOPE>
  <HEADER>
    <TALLYREQUEST>Import Data</TALLYREQUEST>
  </HEADER>
  <BODY>
    <IMPORTDATA>
      <REQUESTDESC>
        <REPORTNAME>Vouchers</REPORTNAME>
      </REQUESTDESC>
      <REQUESTDATA>
{import_body}
      </REQUESTDATA>
    </IMPORTDATA>
  </BODY>
</ENVELOPE>
"""
    (run_dir / "tallyprime_import.xml").write_text(xml, encoding="utf-8")
    (run_dir / "2_import_vouchers.xml").write_text(xml, encoding="utf-8")
    dated_entries = [entry for entry in entries if normalize_date(entry.date)]
    period_line = ""
    if dated_entries:
        from_date = min(normalize_date(entry.date) for entry in dated_entries)
        to_date = max(normalize_date(entry.date) for entry in dated_entries)
        period_line = (
            f"After import, in Tally press F2 and set Period From {human_date(from_date)} "
            f"To {human_date(to_date)} so the imported vouchers are visible.\n"
        )
    steps_text = (
        "Step 1: Import 1_import_masters.xml with Tally import type set to Masters.\n"
        "Step 2: Import 2_import_vouchers.xml with Tally import type set to Transactions/Vouchers.\n"
        "Bill vouchers are exported as Purchase/Sales invoice vouchers with stock item name, quantity, unit, and rate.\n"
        f"{period_line}"
        "Always import from the latest_export folder to avoid using an older downloaded XML by mistake.\n"
    )
    (run_dir / "import_steps.txt").write_text(steps_text, encoding="utf-8")
    refresh_latest_export(run_dir)
    return run_dir


def filter_entries_by_date(entries: list[Entry], date_from: str = "", date_to: str = "") -> list[Entry]:
    start = normalize_date(date_from)
    end = normalize_date(date_to)
    if not start and not end:
        return entries
    filtered = []
    for entry in entries:
        if start and entry.date < start:
            continue
        if end and entry.date > end:
            continue
        filtered.append(entry)
    return filtered


def active_entries() -> list[Entry]:
    entries: list[Entry] = []
    for item in ACTIVE_FILES.values():
        entries.extend(item["entries"])
    return entries


def active_raw_extracts() -> list[dict]:
    extracts: list[dict] = []
    for item in ACTIVE_FILES.values():
        extracts.extend(item["raw_extracts"])
    return extracts


def rebuild_active_outputs() -> Path | None:
    global LAST_RUN_DIR
    entries = active_entries()
    if not entries:
        LAST_RUN_DIR = None
        return None
    LAST_RUN_DIR = write_outputs(entries, active_raw_extracts())
    return LAST_RUN_DIR


def active_bill_entries() -> list[Entry]:
    entries: list[Entry] = []
    for item in BILL_FILES.values():
        entries.extend(item["entries"])
    return entries


def active_bill_raw_extracts() -> list[dict]:
    extracts: list[dict] = []
    for item in BILL_FILES.values():
        extracts.extend(item["raw_extracts"])
    return extracts


def rebuild_bill_outputs() -> Path | None:
    global BILL_LAST_RUN_DIR
    entries = active_bill_entries()
    if not entries:
        BILL_LAST_RUN_DIR = None
        return None
    BILL_LAST_RUN_DIR = write_outputs(entries, active_bill_raw_extracts())
    return BILL_LAST_RUN_DIR


def refresh_latest_export(run_dir: Path) -> None:
    LATEST_EXPORT_DIR.mkdir(exist_ok=True)
    keep_names = {
        "1_import_masters.xml",
        "2_import_vouchers.xml",
        "required_masters.xml",
        "tallyprime_import.xml",
        "review_entries.csv",
        "import_match_summary.json",
        "import_match_summary.txt",
        "source_vs_xml_check.csv",
        "raw_extracts.json",
        "import_steps.txt",
    }
    for old in LATEST_EXPORT_DIR.iterdir():
        if old.is_file():
            old.unlink()
    for name in keep_names:
        src = run_dir / name
        if src.exists():
            shutil.copy2(src, LATEST_EXPORT_DIR / name)


def format_bill_items(entry: Entry) -> str:
    lines: list[str] = []
    for item in entry.inventory_items or []:
        qty = f"{item.get('quantity', '')} {item.get('unit', '')}".strip()
        lines.append(
            " | ".join([
                str(item.get("name", "")).strip(),
                str(item.get("hsn", "")).strip(),
                qty,
                f"{float(item.get('rate', 0) or 0):.2f}",
                f"{float(item.get('amount', 0) or 0):.2f}",
            ])
        )
    return "\n".join(line for line in lines if line.strip(" |"))


def format_item_column(entry: Entry, key: str) -> str:
    values: list[str] = []
    for item in entry.inventory_items or []:
        if key == "quantity":
            values.append(f"{item.get('quantity', '')} {item.get('unit', '')}".strip())
        elif key in {"rate", "amount"}:
            values.append(f"{float(item.get(key, 0) or 0):.2f}")
        else:
            values.append(str(item.get(key, "")).strip())
    return "\n".join(values)


def display_date(date_text: str) -> str:
    normalized = normalize_date(date_text)
    if re.fullmatch(r"\d{8}", normalized):
        return f"{normalized[0:4]}-{normalized[4:6]}-{normalized[6:8]}"
    return date_text


def human_date(date_text: str) -> str:
    normalized = normalize_date(date_text)
    if re.fullmatch(r"\d{8}", normalized):
        return f"{normalized[6:8]}-{normalized[4:6]}-{normalized[0:4]}"
    return date_text


def charge_amount(entry: Entry, ledger: str) -> float:
    target = ledger.strip().lower()
    for charge in entry.charge_lines or []:
        if str(charge.get("ledger", "")).strip().lower() == target:
            return float(charge.get("amount", 0) or 0)
    return 0.0


def charge_hsn(entry: Entry, ledger: str) -> str:
    target = ledger.strip().lower()
    for charge in entry.charge_lines or []:
        if str(charge.get("ledger", "")).strip().lower() == target:
            return str(charge.get("hsn", "") or "").strip()
    if target == "loading & cutting charges":
        return "8428"
    if target == "freight":
        return "9965"
    return ""


def all_bill_charge_ledgers() -> list[str]:
    ledgers: dict[str, str] = {}
    for item in BILL_FILES.values():
        for entry in item.get("entries", []):
            for charge in entry.charge_lines or []:
                ledger = str(charge.get("ledger", "")).strip()
                if ledger:
                    ledgers[ledger.lower()] = ledger
    preferred = ["Loading & Cutting Charges", "Freight", "Round Off"]
    ordered = [ledger for ledger in preferred if ledger.lower() in ledgers]
    ordered.extend(ledger for key, ledger in sorted(ledgers.items()) if ledger not in ordered)
    return ordered


def build_charge_lines_from_values(values: dict[str, float], existing: list[dict]) -> list[dict]:
    existing_hsns = {str(charge.get("ledger", "")).strip().lower(): str(charge.get("hsn", "") or "").strip() for charge in existing or []}
    lines: list[dict] = []
    for ledger, amount in values.items():
        clean = normalize_charge_ledger(ledger)
        if amount:
            lines.append({"ledger": clean, "hsn": existing_hsns.get(clean.lower(), ""), "amount": amount})
    return lines


def parse_bill_items_text(text: str, existing: list[dict]) -> list[dict]:
    parsed: list[dict] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 5:
            continue
        qty_text = parts[2]
        qty_match = re.match(r"([0-9,.]+)\s*(.*)", qty_text)
        quantity = qty_match.group(1).replace(",", "") if qty_match else ""
        unit = qty_match.group(2).strip() if qty_match else ""
        parsed.append({
            "name": clean_item_name(parts[0]),
            "hsn": parts[1],
            "quantity": quantity,
            "unit": unit,
            "rate": money(parts[3]),
            "amount": money(parts[4]),
        })
    return parsed or list(existing or [])


def parse_bill_items_columns(
    names_text: str,
    hsns_text: str,
    quantities_text: str,
    rates_text: str,
    amounts_text: str,
    existing: list[dict],
) -> list[dict]:
    names = [line.strip() for line in names_text.splitlines()]
    hsns = [line.strip() for line in hsns_text.splitlines()]
    quantities = [line.strip() for line in quantities_text.splitlines()]
    rates = [line.strip() for line in rates_text.splitlines()]
    amounts = [line.strip() for line in amounts_text.splitlines()]
    count = max(len(names), len(hsns), len(quantities), len(rates), len(amounts))
    parsed: list[dict] = []
    for idx in range(count):
        name = names[idx] if idx < len(names) else ""
        if not name:
            continue
        qty_text = quantities[idx] if idx < len(quantities) else ""
        qty_match = re.match(r"([0-9,.]+)\s*(.*)", qty_text)
        quantity = qty_match.group(1).replace(",", "") if qty_match else ""
        unit = qty_match.group(2).strip() if qty_match else ""
        parsed.append({
            "name": clean_item_name(name),
            "hsn": hsns[idx] if idx < len(hsns) else "",
            "quantity": quantity,
            "unit": unit,
            "rate": money(rates[idx] if idx < len(rates) else ""),
            "amount": money(amounts[idx] if idx < len(amounts) else ""),
        })
    return parsed or list(existing or [])


def render_page(message: str = "", run_dir: Path | None = None, entries: list[Entry] | None = None) -> bytes:
    if entries is None:
        entries = active_entries()
    if run_dir is None:
        run_dir = LAST_RUN_DIR
    date_options = date_format_options(current_date_parse_mode())
    date_placeholder = date_format_placeholder(current_date_parse_mode())
    rows_html = ""
    if ACTIVE_FILES:
        for file_id, item in ACTIVE_FILES.items():
            for index, e in enumerate(item["entries"]):
                input_name = f"party_ledger:{file_id}:{index}"
                rows_html += f"""
                <tr>
                  <td>{html.escape(e.source_file)}</td>
                  <td>{html.escape(e.voucher_type)}</td>
                  <td>{html.escape(e.date)}</td>
                  <td><input class="ledger-input" name="{html.escape(input_name)}" value="{html.escape(e.party_ledger)}"></td>
                  <td class="num">{e.amount:.2f}</td>
                  <td>{html.escape(e.confidence)}</td>
                  <td>{html.escape(e.needs_review)}</td>
                </tr>"""
    elif entries:
        for e in entries:
            rows_html += f"""
            <tr>
              <td>{html.escape(e.source_file)}</td>
              <td>{html.escape(e.voucher_type)}</td>
              <td>{html.escape(e.date)}</td>
              <td>{html.escape(e.party_ledger)}</td>
              <td class="num">{e.amount:.2f}</td>
              <td>{html.escape(e.confidence)}</td>
              <td>{html.escape(e.needs_review)}</td>
            </tr>"""
    update_button = """
      <div class="table-actions">
        <button type="submit">Update XML with edited party ledgers</button>
      </div>""" if ACTIVE_FILES else ""
    links = ""
    if run_dir:
        rel = run_dir.name
        links = f"""
        <div class="downloads">
          <a href="/latest_export/review_entries.csv">Review CSV</a>
          <a href="/latest_export/required_masters.xml">Required Masters XML</a>
          <a href="/runs/{rel}/update_bank_opening.xml">Update Bank Opening</a>
          <a href="/runs/{rel}/tallyprime_import.json">Tally JSON</a>
          <a href="/latest_export/tallyprime_import.xml">Tally XML</a>
          <a href="/runs/{rel}/reconciliation_summary.csv">Reconciliation</a>
          <a href="/latest_export/raw_extracts.json">Raw extracts</a>
        </div>"""
    file_rows = ""
    for file_id, item in ACTIVE_FILES.items():
        file_rows += f"""
        <tr>
          <td>{html.escape(item["filename"])}</td>
          <td class="num">{len(item["entries"])}</td>
          <td>
            <form action="/delete" method="post" class="inline-form">
              <input type="hidden" name="file_id" value="{html.escape(file_id)}">
              <button class="danger" type="submit">Delete</button>
            </form>
          </td>
        </tr>"""
    clear_form = ""
    if ACTIVE_FILES:
        clear_form = """
        <form action="/clear" method="post" class="inline-form">
          <button class="danger" type="submit">Clear batch</button>
        </form>"""
    body = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>TallyPrime Entry Prep Bot</title>
  <style>
    :root {{ color-scheme: light; font-family: "Segoe UI", Arial, sans-serif; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: #f4f6f8; color: #1f2937; }}
    main {{ max-width: 1200px; margin: 0 auto; padding: 32px 24px 40px; }}
    header {{ display: flex; justify-content: space-between; gap: 18px; align-items: flex-start; margin-bottom: 24px; }}
    nav {{ display: flex; gap: 10px; flex-wrap: wrap; }}
    nav a {{ color: #174ea6; text-decoration: none; font-weight: 700; padding: 10px 14px; border-radius: 999px; background: #e8f0fe; }}
    nav a.active {{ background: #174ea6; color: #fff; }}
    h1 {{ font-size: 32px; margin: 0 0 6px; letter-spacing: 0; }}
    h2 {{ margin: 0 0 14px; font-size: 18px; }}
    p {{ margin: 0; color: #5f6368; line-height: 1.5; }}
    .panel {{ background: #fff; border: 1px solid #d9e0e7; border-radius: 16px; padding: 20px; margin-bottom: 20px; box-shadow: 0 10px 28px rgba(15, 23, 42, 0.06); }}
    form {{ display: grid; gap: 16px; }}
    .filters {{ display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); align-items: end; }}
    label {{ display: grid; gap: 7px; font-weight: 700; color: #243447; }}
    input, select, textarea {{ width: 100%; border: 1px solid #c9d2de; border-radius: 10px; padding: 11px 12px; font: inherit; background: #fff; color: #1f2937; }}
    input:focus, select:focus, textarea:focus {{ outline: 2px solid #d2e3fc; outline-offset: 0; border-color: #174ea6; }}
    input[type=file] {{ padding: 14px; border: 1px dashed #aab7c7; border-radius: 12px; background: #fafcff; }}
    .ledger-input {{ min-width: 150px; padding: 9px 10px; }}
    .date-text-input {{ min-width: 180px; }}
    .checkline {{ display: flex; align-items: center; gap: 8px; font-weight: 700; }}
    .checkline input {{ width: auto; }}
    button {{ width: fit-content; border: 0; border-radius: 10px; background: #1967d2; color: #fff; padding: 11px 18px; font-weight: 700; cursor: pointer; box-shadow: 0 6px 16px rgba(25, 103, 210, 0.22); }}
    button:hover {{ background: #1557b0; }}
    .danger {{ background: #c5221f; padding: 9px 13px; box-shadow: none; }}
    .danger:hover {{ background: #a50e0e; }}
    .inline-form {{ display: inline; }}
    .table-actions {{ display: flex; justify-content: flex-end; margin: 0 0 12px; }}
    .message {{ color: #0b6b35; font-weight: 700; margin-bottom: 14px; padding: 12px 14px; background: #e6f4ea; border: 1px solid #b7dfc3; border-radius: 12px; }}
    .downloads {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 14px; }}
    .downloads a {{ color: #174ea6; background: #eef3fe; text-decoration: none; padding: 10px 12px; border-radius: 999px; font-weight: 600; }}
    .table-wrap {{ overflow: auto; border: 1px solid #e3e7ed; border-radius: 14px; background: #fff; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; font-size: 14px; }}
    th, td {{ border-bottom: 1px solid #e8edf3; padding: 11px 12px; text-align: left; vertical-align: top; }}
    th {{ background: #f7f9fc; font-size: 12px; text-transform: uppercase; color: #5f6368; letter-spacing: 0.04em; }}
    .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
    .note {{ font-size: 13px; color: #5f6368; margin-top: 10px; }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>TallyPrime Entry Prep Bot</h1>
        <p>Upload bills and bank statements, review extracted entries, then import the checked file into TallyPrime.</p>
      </div>
      <nav><a href="/bills">Bills</a><a class="active" href="/">Entries</a><a href="/setup">Company setup</a></nav>
    </header>
    <section class="panel">
      {"<div class='message'>" + html.escape(message) + "</div>" if message else ""}
      <form action="/upload" method="post" enctype="multipart/form-data">
        <input type="file" name="files" multiple accept=".csv,.xlsx,.xls,.txt,.pdf,.jpg,.jpeg,.png,.bmp,.tif,.tiff,.webp">
        <div class="filters">
          <label>Bank ledger name
            <input name="bank_ledger" placeholder="Exact Tally bank ledger name" required>
          </label>
          <label>Date format
            <select name="date_format" id="entryDateFormat">{date_options}</select>
          </label>
          <label>From date
            <input class="date-text-input" type="text" name="date_from" id="dateFromInput" placeholder="{date_placeholder}" inputmode="numeric">
          </label>
          <label>To date
            <input class="date-text-input" type="text" name="date_to" id="dateToInput" placeholder="{date_placeholder}" inputmode="numeric">
          </label>
        </div>
        <label class="checkline">
          <input type="checkbox" name="append_batch" value="yes">
          Add to current batch
        </label>
        <button type="submit">Process files</button>
      </form>
      <p class="note">Best inputs: bank CSV/XLSX and text-based PDF bills. Scanned PDFs/photos need OCR and will be marked for review.</p>
      <p class="note">Choose the date format before processing if the source file uses DD-MM-YYYY, MM-DD-YYYY, YYYY-MM-DD, DD-YYYY-MM, MM-YYYY-DD, or YYYY-DD-MM consistently.</p>
      <p class="note">Use the same selected format in From date and To date filters.</p>
      <p class="note">After editing party ledgers, click Update XML. If a typed party ledger is new in Tally, import Required Masters XML once before importing Tally XML.</p>
      {links}
      <p class="note"><a href="/cleanup/cleanup_bad_ledgers.xml">Download cleanup for old bad ledgers</a></p>
    </section>
    <section class="panel">
      <h2>Uploaded files</h2>
      {clear_form}
      <div class="table-wrap">
        <table>
          <thead><tr><th>File</th><th>Entries</th><th>Action</th></tr></thead>
          <tbody>{file_rows or "<tr><td colspan='3'>No files in the current batch.</td></tr>"}</tbody>
        </table>
      </div>
    </section>
    <section>
      <form action="/update_entries" method="post">
        {update_button}
        <div class="table-wrap">
          <table>
            <thead>
              <tr><th>Source</th><th>Voucher</th><th>Date</th><th>Party ledger</th><th>Amount</th><th>Confidence</th><th>Review</th></tr>
            </thead>
            <tbody>{rows_html or "<tr><td colspan='7'>No entries processed yet.</td></tr>"}</tbody>
          </table>
        </div>
      </form>
    </section>
  </main>
  <script>
    (() => {{
      const formatSelect = document.getElementById("entryDateFormat");
      const fromInput = document.getElementById("dateFromInput");
      const toInput = document.getElementById("dateToInput");
      const placeholders = {{
        auto: "dd-mm-yyyy / mm-dd-yyyy / yyyy-mm-dd",
        dmy: "dd-mm-yyyy",
        mdy: "mm-dd-yyyy",
        ymd: "yyyy-mm-dd",
        dym: "dd-yyyy-mm",
        myd: "mm-yyyy-dd",
        ydm: "yyyy-dd-mm"
      }};
      const syncDateFilterPlaceholders = () => {{
        const selected = formatSelect ? formatSelect.value : "auto";
        const placeholder = placeholders[selected] || placeholders.auto;
        if (fromInput) fromInput.placeholder = placeholder;
        if (toInput) toInput.placeholder = placeholder;
      }};
      if (formatSelect) {{
        formatSelect.addEventListener("change", syncDateFilterPlaceholders);
      }}
      syncDateFilterPlaceholders();
    }})();
  </script>
</body>
</html>"""
    return body.encode("utf-8")


def render_bill_page(message: str = "", run_dir: Path | None = None) -> bytes:
    if run_dir is None:
        run_dir = BILL_LAST_RUN_DIR
    date_options = date_format_options(current_date_parse_mode())
    charge_ledgers = all_bill_charge_ledgers()
    charge_headers = "".join(f"<th>{html.escape(ledger)}</th>" for ledger in charge_ledgers)
    rows_html = ""
    for file_id, item in BILL_FILES.items():
        for index, e in enumerate(item["entries"]):
            prefix = f"bill:{file_id}:{index}"
            item_amount_total = round(sum(float(row.get("amount", 0) or 0) for row in e.inventory_items), 2)
            taxable_charge_total = round(sum(charge_amount(e, ledger) for ledger in charge_ledgers if ledger.strip().lower() != "round off"), 2)
            amount_display = round(item_amount_total + taxable_charge_total, 2) if item_amount_total else e.amount
            charge_inputs = ""
            for charge_index, ledger in enumerate(charge_ledgers):
                charge_inputs += f"""
              <td><input class="amount-input" name="{prefix}:charge_amount:{charge_index}" value="{charge_amount(e, ledger):.2f}"><input type="hidden" name="{prefix}:charge_ledger:{charge_index}" value="{html.escape(ledger)}"><input type="hidden" name="{prefix}:charge_hsn:{charge_index}" value="{html.escape(charge_hsn(e, ledger))}"></td>"""
            rows_html += f"""
            <tr class="bill-entry-row">
              <td><input type="checkbox" class="row-check" aria-label="Select row"></td>
              <td>{html.escape(e.source_file)}</td>
              <td><textarea class="item-name-input" name="{prefix}:item_names" spellcheck="false">{html.escape(format_item_column(e, "name"))}</textarea></td>
              <td><textarea class="item-small-input" name="{prefix}:item_hsns" spellcheck="false">{html.escape(format_item_column(e, "hsn"))}</textarea></td>
              <td><textarea class="item-small-input" name="{prefix}:item_quantities" spellcheck="false">{html.escape(format_item_column(e, "quantity"))}</textarea></td>
              <td><textarea class="item-small-input" name="{prefix}:item_rates" spellcheck="false">{html.escape(format_item_column(e, "rate"))}</textarea></td>
              <td><textarea class="item-small-input num-textarea" name="{prefix}:item_amounts" spellcheck="false">{html.escape(format_item_column(e, "amount"))}</textarea></td>
{charge_inputs}
              <td><input class="small-input" name="{prefix}:voucher_number" value="{html.escape(bill_number_from_entry(e))}"></td>
              <td><input class="small-input" name="{prefix}:voucher_type" value="{html.escape(e.voucher_type)}"></td>
              <td><input class="small-input" type="date" name="{prefix}:date" value="{html.escape(display_date(e.date))}"></td>
              <td><input class="ledger-input" name="{prefix}:party_ledger" value="{html.escape(e.party_ledger)}"></td>
              <td><input class="ledger-input" name="{prefix}:debit_ledger" value="{html.escape(e.debit_ledger)}"></td>
              <td><input class="ledger-input" name="{prefix}:credit_ledger" value="{html.escape(e.credit_ledger)}"></td>
              <td><input class="amount-input" name="{prefix}:amount" value="{amount_display:.2f}"></td>
              <td><input class="amount-input" name="{prefix}:cgst_amount" value="{e.cgst_amount:.2f}"></td>
              <td><input class="amount-input" name="{prefix}:sgst_amount" value="{e.sgst_amount:.2f}"></td>
              <td><input class="amount-input" name="{prefix}:igst_amount" value="{e.igst_amount:.2f}"></td>
              <td><input class="amount-input" name="{prefix}:total_amount" value="{(e.total_amount or e.amount):.2f}"></td>
              <td><input class="ledger-input" name="{prefix}:narration" value="{html.escape(e.narration)}"></td>
              <td>{html.escape(e.needs_review)}</td>
            </tr>"""
    file_rows = ""
    for file_id, item in BILL_FILES.items():
        file_rows += f"""
        <tr>
          <td>{html.escape(item["filename"])}</td>
          <td class="num">{len(item["entries"])}</td>
          <td>
            <form action="/bill_delete" method="post" class="inline-form">
              <input type="hidden" name="file_id" value="{html.escape(file_id)}">
              <button class="danger" type="submit">Delete</button>
            </form>
          </td>
        </tr>"""
    clear_form = """
      <form action="/bill_clear" method="post" class="inline-form">
        <button class="danger" type="submit">Clear bills</button>
      </form>""" if BILL_FILES else ""
    update_button = """
      <div class="table-actions">
        <button type="submit">Update XML with edited bill entries</button>
      </div>""" if BILL_FILES else ""
    links = ""
    if run_dir:
        links = """
        <div class="downloads">
          <a href="/latest_export/review_entries.csv">Review CSV</a>
          <a href="/latest_export/1_import_masters.xml">1. Import Masters XML</a>
          <a href="/runs/{rel}/tallyprime_import.json">Tally JSON</a>
          <a href="/latest_export/2_import_vouchers.xml">2. Import Vouchers XML</a>
          <a href="/latest_export/import_match_summary.txt">Import match summary</a>
          <a href="/latest_export/source_vs_xml_check.csv">Source vs XML check</a>
          <a href="/latest_export/raw_extracts.json">Raw extracts</a>
          <a href="/latest_export/import_steps.txt">Import steps</a>
        </div>""".replace("{rel}", run_dir.name)
    body = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>TallyPrime Bill Parser</title>
  <style>
    :root {{ color-scheme: light; font-family: "Segoe UI", Arial, sans-serif; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: #f4f6f8; color: #1f2937; }}
    main {{ max-width: 1440px; margin: 0 auto; padding: 32px 24px 40px; }}
    header {{ display: flex; justify-content: space-between; gap: 18px; align-items: flex-start; margin-bottom: 24px; }}
    nav {{ display: flex; gap: 10px; flex-wrap: wrap; }}
    nav a {{ color: #174ea6; text-decoration: none; font-weight: 700; padding: 10px 14px; border-radius: 999px; background: #e8f0fe; }}
    nav a.active {{ background: #174ea6; color: #fff; }}
    h1 {{ font-size: 32px; margin: 0 0 6px; letter-spacing: 0; }}
    h2 {{ margin: 0 0 14px; font-size: 18px; }}
    p {{ margin: 0; color: #5f6368; line-height: 1.5; }}
    .panel {{ background: #fff; border: 1px solid #d9e0e7; border-radius: 16px; padding: 20px; margin-bottom: 20px; box-shadow: 0 10px 28px rgba(15, 23, 42, 0.06); }}
    .split-panels {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 18px; align-items: start; }}
    .subpanel {{ display: grid; gap: 14px; padding: 18px; border: 1px solid #e3e7ed; border-radius: 16px; background: linear-gradient(180deg, #fbfdff 0%, #ffffff 100%); min-height: 100%; }}
    .subpanel h2 {{ margin: 0; font-size: 18px; }}
    form {{ display: grid; gap: 16px; }}
    .filters {{ display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }}
    label {{ display: grid; gap: 7px; font-weight: 700; color: #243447; }}
    input, select, textarea {{ width: 100%; border: 1px solid #c9d2de; border-radius: 10px; padding: 11px 12px; font: inherit; background: #fff; color: #1f2937; }}
    input:focus, select:focus, textarea:focus {{ outline: 2px solid #d2e3fc; outline-offset: 0; border-color: #174ea6; }}
    input[type=file] {{ padding: 14px; border: 1px dashed #aab7c7; border-radius: 12px; background: #fafcff; }}
    button {{ width: fit-content; border: 0; border-radius: 10px; background: #1967d2; color: #fff; padding: 11px 18px; font-weight: 700; cursor: pointer; box-shadow: 0 6px 16px rgba(25, 103, 210, 0.22); }}
    button:hover {{ background: #1557b0; }}
    .danger {{ background: #c5221f; padding: 9px 13px; box-shadow: none; }}
    .danger:hover {{ background: #a50e0e; }}
    .inline-form {{ display: inline; }}
    .message {{ color: #0b6b35; font-weight: 700; margin-bottom: 14px; padding: 12px 14px; background: #e6f4ea; border: 1px solid #b7dfc3; border-radius: 12px; }}
    .downloads {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 14px; }}
    .downloads a {{ color: #174ea6; background: #eef3fe; text-decoration: none; padding: 10px 12px; border-radius: 999px; font-weight: 600; }}
    .table-actions {{ display: flex; justify-content: flex-end; margin: 0 0 12px; }}
    .bulk-actions, .search-actions {{ display: flex; flex-wrap: wrap; align-items: end; gap: 12px; background: #f8fafc; border: 1px solid #e3e7ed; border-radius: 14px; padding: 14px; margin: 0 0 12px; }}
    .bulk-actions label, .search-actions label {{ display: grid; gap: 4px; font-size: 12px; color: #5f6368; text-transform: uppercase; }}
    .bulk-actions input, .bulk-actions select {{ min-width: 180px; padding: 9px 10px; }}
    .search-actions input {{ min-width: 320px; padding: 9px 10px; }}
    .bulk-count, .search-count {{ font-size: 13px; color: #5f6368; padding: 0 4px 8px; }}
    .table-wrap {{ overflow: auto; border: 1px solid #e3e7ed; border-radius: 14px; background: #fff; }}
    table {{ width: 100%; min-width: {2400 + (160 * len(charge_ledgers))}px; border-collapse: collapse; background: #fff; font-size: 14px; }}
    th, td {{ border-bottom: 1px solid #e8edf3; padding: 9px 10px; text-align: left; vertical-align: top; }}
    th {{ background: #f7f9fc; font-size: 12px; text-transform: uppercase; color: #5f6368; letter-spacing: 0.04em; position: sticky; top: 0; z-index: 1; }}
    .ledger-input {{ min-width: 150px; padding: 9px 10px; }}
    .small-input {{ width: 120px; min-width: 120px; padding: 9px 10px; }}
    .amount-input {{ width: 120px; min-width: 120px; padding: 9px 10px; text-align: right; }}
    .item-name-input {{ width: 280px; min-height: 116px; padding: 8px 9px; font: 13px/1.55 Consolas, monospace; resize: vertical; overflow: auto; white-space: pre; }}
    .item-small-input {{ width: 150px; min-height: 116px; padding: 8px 9px; font: 13px/1.55 Consolas, monospace; resize: vertical; overflow: auto; white-space: pre; }}
    .num-textarea {{ text-align: right; }}
    .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
    .note {{ font-size: 13px; color: #5f6368; margin-top: 10px; }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>TallyPrime Bill Parser</h1>
        <p>Upload bills, edit voucher and ledger details, then import the generated XML into TallyPrime.</p>
      </div>
      <nav><a class="active" href="/bills">Bills</a><a href="/">Entries</a><a href="/setup">Company setup</a></nav>
    </header>
    <section class="panel">
      {"<div class='message'>" + html.escape(message) + "</div>" if message else ""}
      <div class="split-panels">
        <div class="subpanel">
          <h2>Computer-generated bills</h2>
          <form action="/bill_upload" method="post" enctype="multipart/form-data">
            <input type="hidden" name="bill_source" value="generated">
            <input type="file" name="files" multiple accept=".txt,.pdf,.jpg,.jpeg,.png,.bmp,.tif,.tiff,.webp">
            <div class="filters">
              <label>Default entry type
                <select name="entry_type">
                  <option value="purchase">Purchase bill</option>
                  <option value="sale">Sale bill</option>
                  <option value="expense">Expense</option>
                  <option value="asset">Asset purchase</option>
                </select>
              </label>
              <label>Date format
                <select name="date_format">{date_options}</select>
              </label>
            </div>
            <button type="submit">Parse bill files</button>
          </form>
          <p class="note">Use this for PDF, photo, PNG/JPG, or text bill files. OCR values marked Review should be checked before XML import.</p>
          <p class="note">Choose the date format before processing if the bill uses DD-MM-YYYY, MM-DD-YYYY, YYYY-MM-DD, DD-YYYY-MM, MM-YYYY-DD, or YYYY-DD-MM consistently.</p>
          <p class="note">In Tally, import 1. Import Masters XML as Masters, then import 2. Import Vouchers XML as Transactions/Vouchers.</p>
        </div>
        <div class="subpanel">
          <h2>Manual bills Excel</h2>
          <form action="/bill_upload" method="post" enctype="multipart/form-data">
            <input type="hidden" name="bill_source" value="excel">
            <input type="file" name="files" multiple accept=".csv,.xlsx,.xls">
            <div class="filters">
              <label>Default entry type
                <select name="entry_type">
                  <option value="purchase">Purchase bill</option>
                  <option value="sale">Sale bill</option>
                  <option value="expense">Expense</option>
                  <option value="asset">Asset purchase</option>
                </select>
              </label>
              <label>Date format
                <select name="date_format">{date_options}</select>
              </label>
            </div>
            <button type="submit">Import Excel bills</button>
          </form>
          <p class="note">Use this for handwritten/manual bills. Columns can include Date, Party Ledger, Entry Type, Debit Ledger, Credit Ledger, Amount, CGST, SGST, IGST, Total Amount, and Narration.</p>
          <p class="note"><a href="/bill_excel_template.csv">Download Excel template CSV</a></p>
        </div>
      </div>
      {links}
    </section>
    <section class="panel">
      <h2>Uploaded bills</h2>
      {clear_form}
      <div class="table-wrap">
        <table>
          <thead><tr><th>File</th><th>Entries</th><th>Action</th></tr></thead>
          <tbody>{file_rows or "<tr><td colspan='3'>No bills in the current batch.</td></tr>"}</tbody>
        </table>
      </div>
    </section>
    <section>
      <form action="/update_bills" method="post">
        {update_button}
        <div class="search-actions">
          <label>Search entries
            <input id="billSearch" placeholder="Search by source, party, item, bill no., amount, narration, HSN, quantity">
          </label>
          <div id="searchCount" class="search-count">Showing all rows</div>
        </div>
        <div class="bulk-actions">
          <label>Bulk column
            <select id="bulkColumn">
              <option value="item_names">Item name</option>
              <option value="item_hsns">HSN/SAC</option>
              <option value="item_quantities">Quantity</option>
              <option value="item_rates">Rate</option>
              <option value="item_amounts">Item amount</option>
              <option value="party_ledger">Party ledger</option>
              <option value="debit_ledger">Debit ledger</option>
              <option value="credit_ledger">Credit ledger</option>
              <option value="voucher_type">Voucher type</option>
              <option value="voucher_number">Voucher no.</option>
              <option value="date">Date</option>
              <option value="amount">Amount</option>
              <option value="cgst_amount">CGST</option>
              <option value="sgst_amount">SGST</option>
              <option value="igst_amount">IGST</option>
              <option value="total_amount">Total amount</option>
              <option value="narration">Narration</option>
            </select>
          </label>
          <label>New value
            <input id="bulkValue" placeholder="Value for selected rows">
          </label>
          <button type="button" id="applyBulk">Apply to selected</button>
          <div id="selectedCount" class="bulk-count">0 rows selected</div>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr><th><input type="checkbox" id="selectAllRows" aria-label="Select all rows"></th><th>Source</th><th>Item Name</th><th>HSN/SAC</th><th>Quantity</th><th>Rate</th><th>Item Amount</th>{charge_headers}<th>Voucher No.</th><th>Voucher</th><th>Date</th><th>Party ledger</th><th>Debit ledger</th><th>Credit ledger</th><th>Amount</th><th>CGST</th><th>SGST</th><th>IGST</th><th>Total Amount</th><th>Narration</th><th>Review</th></tr>
            </thead>
            <tbody>{rows_html or f"<tr><td colspan='{20 + len(charge_ledgers)}'>No bill entries processed yet.</td></tr>"}</tbody>
          </table>
        </div>
      </form>
    </section>
  </main>
  <script>
    const billSearch = document.getElementById("billSearch");
    const searchCount = document.getElementById("searchCount");
    const selectAllRows = document.getElementById("selectAllRows");
    const rowChecks = () => Array.from(document.querySelectorAll(".row-check"));
    const entryRows = () => Array.from(document.querySelectorAll(".bill-entry-row"));
    const selectedCount = document.getElementById("selectedCount");
    const syncSearchState = () => {{
      const query = (billSearch && billSearch.value ? billSearch.value : "").trim().toLowerCase();
      const rows = entryRows();
      let visible = 0;
      rows.forEach((row) => {{
        const text = (row.innerText || row.textContent || "").toLowerCase();
        const match = !query || text.includes(query);
        row.style.display = match ? "" : "none";
        if (match) visible += 1;
      }});
      if (searchCount) {{
        searchCount.textContent = query
          ? `${{visible}} matching row${{visible === 1 ? "" : "s"}}`
          : `${{visible}} row${{visible === 1 ? "" : "s"}} shown`;
      }}
    }};
    const syncSelectionState = () => {{
      const checks = rowChecks();
      const selected = checks.filter((check) => check.checked).length;
      if (selectedCount) {{
        selectedCount.textContent = `${{selected}} row${{selected === 1 ? "" : "s"}} selected`;
      }}
      if (selectAllRows) {{
        selectAllRows.checked = checks.length > 0 && selected === checks.length;
        selectAllRows.indeterminate = selected > 0 && selected < checks.length;
      }}
    }};
    if (selectAllRows) {{
      selectAllRows.addEventListener("change", () => {{
        rowChecks().forEach((check) => {{ check.checked = selectAllRows.checked; }});
        syncSelectionState();
      }});
    }}
    if (billSearch) {{
      billSearch.addEventListener("input", syncSearchState);
    }}
    rowChecks().forEach((check) => check.addEventListener("change", syncSelectionState));
    const applyBulk = document.getElementById("applyBulk");
    if (applyBulk) {{
      applyBulk.addEventListener("click", () => {{
        const column = document.getElementById("bulkColumn").value;
        const value = document.getElementById("bulkValue").value;
        const selected = rowChecks().filter((check) => check.checked);
        selected.forEach((check) => {{
          const row = check.closest("tr");
          const input = row ? row.querySelector(`[name$=":${{column}}"]`) : null;
          if (input) input.value = value;
        }});
        syncSelectionState();
      }});
    }}
    syncSearchState();
    syncSelectionState();
  </script>
</body>
</html>"""
    return body.encode("utf-8")


def render_setup_page(message: str = "", run_dir: Path | None = None) -> bytes:
    links = ""
    if run_dir:
        rel = run_dir.name
        links = f"""
        <div class="downloads">
          <a href="/runs/{rel}/tallyprime_masters.xml">Masters XML</a>
          <a href="/runs/{rel}/company_setup.json">Setup JSON</a>
        </div>"""
    body = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>TallyPrime Company Setup</title>
  <style>
    :root {{ color-scheme: light; font-family: "Segoe UI", Arial, sans-serif; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: #f4f6f8; color: #1f2937; }}
    main {{ max-width: 980px; margin: 0 auto; padding: 32px 24px 40px; }}
    header {{ display: flex; justify-content: space-between; gap: 18px; align-items: flex-start; margin-bottom: 24px; }}
    nav {{ display: flex; gap: 10px; flex-wrap: wrap; }}
    nav a {{ color: #174ea6; text-decoration: none; font-weight: 700; padding: 10px 14px; border-radius: 999px; background: #e8f0fe; }}
    nav a.active {{ background: #174ea6; color: #fff; }}
    h1 {{ font-size: 32px; margin: 0 0 6px; letter-spacing: 0; }}
    p {{ margin: 0; color: #5f6368; line-height: 1.5; }}
    .panel {{ background: #fff; border: 1px solid #d9e0e7; border-radius: 16px; padding: 20px; margin-bottom: 20px; box-shadow: 0 10px 28px rgba(15, 23, 42, 0.06); }}
    form {{ display: grid; gap: 16px; }}
    label {{ display: grid; gap: 7px; font-weight: 700; color: #243447; }}
    input, textarea {{ width: 100%; border: 1px solid #c9d2de; border-radius: 10px; padding: 11px 12px; font: inherit; background: #fff; color: #1f2937; }}
    input:focus, textarea:focus {{ outline: 2px solid #d2e3fc; outline-offset: 0; border-color: #174ea6; }}
    textarea {{ min-height: 84px; resize: vertical; }}
    button {{ width: fit-content; border: 0; border-radius: 10px; background: #1967d2; color: #fff; padding: 11px 18px; font-weight: 700; cursor: pointer; box-shadow: 0 6px 16px rgba(25, 103, 210, 0.22); }}
    button:hover {{ background: #1557b0; }}
    .message {{ color: #0b6b35; font-weight: 700; margin-bottom: 14px; padding: 12px 14px; background: #e6f4ea; border: 1px solid #b7dfc3; border-radius: 12px; }}
    .downloads {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 14px; }}
    .downloads a {{ color: #174ea6; background: #eef3fe; text-decoration: none; padding: 10px 12px; border-radius: 999px; font-weight: 600; }}
    .note {{ font-size: 13px; color: #5f6368; margin-top: 10px; }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>TallyPrime Company Setup</h1>
        <p>Create the basic ledger masters file, then import it into the company opened in TallyPrime.</p>
      </div>
      <nav><a href="/bills">Bills</a><a href="/">Entries</a><a class="active" href="/setup">Company setup</a></nav>
    </header>
    <section class="panel">
      {"<div class='message'>" + html.escape(message) + "</div>" if message else ""}
      <form action="/setup" method="post">
        <label>Company name
          <input name="company_name" value="My Company" required>
        </label>
        <label>Bank ledger name
          <input name="bank_ledger" value="HDFC Bank" required>
        </label>
        <label>Supplier ledgers, comma separated
          <textarea name="supplier_ledgers" placeholder="ABC Traders, XYZ Suppliers"></textarea>
        </label>
        <label>Customer ledgers, comma separated
          <textarea name="customer_ledgers" placeholder="Customer One, Customer Two"></textarea>
        </label>
        <button type="submit">Create masters file</button>
      </form>
      <p class="note">TallyPrime still needs one company open before importing masters. This file creates Bank, Purchase, Sundry Creditor, and Sundry Debtor ledgers automatically.</p>
      {links}
    </section>
  </main>
</body>
</html>"""
    return body.encode("utf-8")


def parse_multipart(body: bytes, content_type: str) -> tuple[list[tuple[str, bytes]], dict[str, str]]:
    marker = "boundary="
    if marker not in content_type:
        return [], {}
    boundary = ("--" + content_type.split(marker, 1)[1]).encode()
    files = []
    fields: dict[str, str] = {}
    for part in body.split(boundary):
        if b"Content-Disposition" not in part:
            continue
        header, _, data = part.partition(b"\r\n\r\n")
        disposition = header.decode("utf-8", errors="ignore")
        name_match = re.search(r'name="([^"]*)"', disposition)
        if not name_match:
            continue
        payload = data.rstrip(b"\r\n-")
        filename_match = re.search(r'filename="([^"]*)"', disposition)
        if filename_match is not None:
            if not filename_match.group(1):
                continue
            filename = Path(filename_match.group(1)).name
            files.append((filename, payload))
        else:
            fields[name_match.group(1)] = payload.decode("utf-8", errors="ignore").strip()
    return files, fields


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/latest_export/"):
            target = (LATEST_EXPORT_DIR / parsed.path.removeprefix("/latest_export/")).resolve()
            if not str(target).startswith(str(LATEST_EXPORT_DIR.resolve())) or not target.is_file():
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", f'attachment; filename="{target.name}"')
            self.end_headers()
            self.wfile.write(target.read_bytes())
            return
        if parsed.path.startswith("/runs/"):
            target = (RUNS_DIR / parsed.path.removeprefix("/runs/")).resolve()
            if not str(target).startswith(str(RUNS_DIR.resolve())) or not target.is_file():
                self.send_error(404)
                return
            run_name = target.parent.name
            download_name = f"{run_name}_{target.name}"
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
            self.end_headers()
            self.wfile.write(target.read_bytes())
            return
        if parsed.path.startswith("/cleanup/"):
            target = (CLEANUP_DIR / parsed.path.removeprefix("/cleanup/")).resolve()
            if not str(target).startswith(str(CLEANUP_DIR.resolve())) or not target.is_file():
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", f'attachment; filename="{target.name}"')
            self.end_headers()
            self.wfile.write(target.read_bytes())
            return
        if parsed.path == "/setup":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(render_setup_page())
            return
        if parsed.path == "/bills":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(render_bill_page())
            return
        if parsed.path == "/bill_excel_template.csv":
            template = (
                "Date,Party Ledger,Entry Type,Voucher Type,Debit Ledger,Credit Ledger,"
                "Amount,CGST,SGST,IGST,Total Amount,Narration\n"
                "03-06-2026,Suspense,purchase,Purchase,Purchase Accounts,Suspense,"
                "10000,900,900,0,11800,Manual bill example\n"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="manual_bill_template.csv"')
            self.end_headers()
            self.wfile.write(template.encode("utf-8"))
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(render_page())

    def do_POST(self) -> None:
        if self.path == "/setup":
            length = int(self.headers.get("Content-Length", "0"))
            form = parse_qs(self.rfile.read(length).decode("utf-8", errors="ignore"))
            run_dir = write_setup_outputs(form)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(render_setup_page("Created TallyPrime masters import files.", run_dir))
            return
        if self.path == "/delete":
            length = int(self.headers.get("Content-Length", "0"))
            form = parse_qs(self.rfile.read(length).decode("utf-8", errors="ignore"))
            file_id = form.get("file_id", [""])[0]
            deleted = ACTIVE_FILES.pop(file_id, None)
            run_dir = rebuild_active_outputs()
            message = "Deleted file from current batch." if deleted else "File was already removed."
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(render_page(message, run_dir))
            return
        if self.path == "/clear":
            ACTIVE_FILES.clear()
            rebuild_active_outputs()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(render_page("Cleared current batch. Upload a fresh file now."))
            return
        if self.path == "/bill_delete":
            length = int(self.headers.get("Content-Length", "0"))
            form = parse_qs(self.rfile.read(length).decode("utf-8", errors="ignore"))
            file_id = form.get("file_id", [""])[0]
            deleted = BILL_FILES.pop(file_id, None)
            run_dir = rebuild_bill_outputs()
            message = "Deleted bill file from current batch." if deleted else "Bill file was already removed."
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(render_bill_page(message, run_dir))
            return
        if self.path == "/bill_clear":
            BILL_FILES.clear()
            rebuild_bill_outputs()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(render_bill_page("Cleared bill batch. Upload fresh bills now."))
            return
        if self.path == "/update_bills":
            length = int(self.headers.get("Content-Length", "0"))
            form = parse_qs(self.rfile.read(length).decode("utf-8", errors="ignore"))
            updated = 0
            for file_id, item in BILL_FILES.items():
                for index, entry in enumerate(item["entries"]):
                    prefix = f"bill:{file_id}:{index}:"
                    voucher_type = (form.get(prefix + "voucher_type", [entry.voucher_type])[0] or entry.voucher_type).strip()
                    date_text = (form.get(prefix + "date", [entry.date])[0] or entry.date).strip()
                    date = date_text if re.fullmatch(r"\d{8}", date_text) else normalize_date(date_text)
                    party_ledger = (form.get(prefix + "party_ledger", [entry.party_ledger])[0] or DEFAULT_SUSPENSE_LEDGER).strip()
                    debit_ledger = (form.get(prefix + "debit_ledger", [entry.debit_ledger])[0] or DEFAULT_SUSPENSE_LEDGER).strip()
                    credit_ledger = (form.get(prefix + "credit_ledger", [entry.credit_ledger])[0] or DEFAULT_SUSPENSE_LEDGER).strip()
                    voucher_number = (form.get(prefix + "voucher_number", [bill_number_from_entry(entry)])[0] or "").strip()
                    amount_value = money(form.get(prefix + "amount", [entry.amount])[0]) or entry.amount
                    cgst_amount = money(form.get(prefix + "cgst_amount", [entry.cgst_amount])[0])
                    sgst_amount = money(form.get(prefix + "sgst_amount", [entry.sgst_amount])[0])
                    igst_amount = money(form.get(prefix + "igst_amount", [entry.igst_amount])[0])
                    tax_total = round(cgst_amount + sgst_amount + igst_amount, 2)
                    total_amount = money(form.get(prefix + "total_amount", [entry.total_amount or entry.amount])[0])
                    if not total_amount:
                        total_amount = round(amount_value + tax_total, 2) if tax_total else amount_value
                    narration = (form.get(prefix + "narration", [entry.narration])[0] or entry.narration).strip()
                    charge_values: dict[str, float] = {}
                    charge_index = 0
                    while True:
                        ledger_key = prefix + f"charge_ledger:{charge_index}"
                        amount_key = prefix + f"charge_amount:{charge_index}"
                        if ledger_key not in form and amount_key not in form:
                            break
                        ledger_name = (form.get(ledger_key, [""])[0] or "").strip()
                        amount_text = str(form.get(amount_key, ["0"])[0]).strip()
                        charge_value = money(amount_text)
                        if "-" in amount_text or "(" in amount_text:
                            charge_value = -charge_value
                        if ledger_name:
                            charge_values[ledger_name] = charge_value
                        charge_index += 1
                    inventory_items = parse_bill_items_columns(
                        form.get(prefix + "item_names", [format_item_column(entry, "name")])[0],
                        form.get(prefix + "item_hsns", [format_item_column(entry, "hsn")])[0],
                        form.get(prefix + "item_quantities", [format_item_column(entry, "quantity")])[0],
                        form.get(prefix + "item_rates", [format_item_column(entry, "rate")])[0],
                        form.get(prefix + "item_amounts", [format_item_column(entry, "amount")])[0],
                        list(entry.inventory_items or []),
                    )
                    if inventory_items:
                        item_total = round(sum(float(item.get("amount", 0) or 0) for item in inventory_items), 2)
                        taxable_charge_total = round(sum(value for ledger, value in charge_values.items() if ledger.strip().lower() != "round off"), 2)
                        amount_value = round(item_total + taxable_charge_total, 2)
                    charge_lines = build_charge_lines_from_values(charge_values, list(entry.charge_lines or []))
                    charge_total = round(sum(float(charge.get("amount", 0) or 0) for charge in charge_lines), 2)
                    computed_total = round(amount_value + tax_total + charge_total, 2)
                    if not total_amount or abs(total_amount - computed_total) > 0.01:
                        total_amount = computed_total
                    item["entries"][index] = Entry(
                        source_file=entry.source_file,
                        source_kind=entry.source_kind,
                        voucher_type=voucher_type,
                        date=date,
                        party_ledger=party_ledger,
                        debit_ledger=debit_ledger,
                        credit_ledger=credit_ledger,
                        amount=amount_value,
                        narration=narration[:220],
                        confidence="Medium" if date and total_amount else "Low",
                        needs_review="No" if date and total_amount and debit_ledger and credit_ledger else "Yes",
                        cgst_amount=cgst_amount,
                        sgst_amount=sgst_amount,
                        igst_amount=igst_amount,
                        total_amount=total_amount,
                        inventory_items=inventory_items,
                        charge_lines=charge_lines,
                        voucher_number=voucher_number[:80],
                    )
                    updated += 1
            run_dir = rebuild_bill_outputs()
            message = f"Updated {updated} bill entr{'y' if updated == 1 else 'ies'} and regenerated the XML."
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(render_bill_page(message, run_dir))
            return
        if self.path == "/bill_upload":
            length = int(self.headers.get("Content-Length", "0"))
            files, fields = parse_multipart(self.rfile.read(length), self.headers.get("Content-Type", ""))
            global BILL_LAST_RUN_DIR
            set_date_parse_mode(fields.get("date_format", "auto"))
            entry_type = fields.get("entry_type", "purchase").strip() or "purchase"
            bill_source = fields.get("bill_source", "generated").strip() or "generated"
            for filename, data in files:
                save_path = UPLOADS_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{filename}"
                save_path.write_bytes(data)
                try:
                    if bill_source == "excel":
                        new_entries, raw = process_manual_bill_file(save_path, entry_type)
                    else:
                        new_entries, raw = process_generated_bill_file(save_path, entry_type)
                    file_id = uuid.uuid4().hex
                    BILL_FILES[file_id] = {"filename": filename, "entries": new_entries, "raw_extracts": [raw]}
                except Exception as exc:
                    error_entry = Entry(filename, "Error", "Journal", "", DEFAULT_SUSPENSE_LEDGER, DEFAULT_SUSPENSE_LEDGER, DEFAULT_SUSPENSE_LEDGER, 0, str(exc), "Low", "Yes")
                    file_id = uuid.uuid4().hex
                    BILL_FILES[file_id] = {"filename": filename, "entries": [error_entry], "raw_extracts": [{"file": filename, "error": str(exc)}]}
            run_dir = rebuild_bill_outputs()
            total_entries = len(active_bill_entries())
            source_label = "manual Excel" if bill_source == "excel" else "computer-generated/OCR"
            message = f"Added {len(files)} {source_label} bill file(s). Current bill batch has {total_entries} entr{'y' if total_entries == 1 else 'ies'}."
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(render_bill_page(message, run_dir))
            return
        if self.path == "/update_entries":
            length = int(self.headers.get("Content-Length", "0"))
            form = parse_qs(self.rfile.read(length).decode("utf-8", errors="ignore"))
            updated = 0
            for key, values in form.items():
                if not key.startswith("party_ledger:"):
                    continue
                _prefix, file_id, index_text = key.split(":", 2)
                item = ACTIVE_FILES.get(file_id)
                if not item:
                    continue
                try:
                    index = int(index_text)
                    entry = item["entries"][index]
                except (ValueError, IndexError):
                    continue
                new_party = (values[0] if values else "").strip() or DEFAULT_SUSPENSE_LEDGER
                if entry.party_ledger != new_party:
                    item["entries"][index] = Entry(
                        source_file=entry.source_file,
                        source_kind=entry.source_kind,
                        voucher_type=entry.voucher_type,
                        date=entry.date,
                        party_ledger=new_party,
                        debit_ledger=entry.debit_ledger,
                        credit_ledger=entry.credit_ledger,
                        amount=entry.amount,
                        narration=entry.narration,
                        confidence=entry.confidence,
                        needs_review=entry.needs_review,
                    )
                    updated += 1
            run_dir = rebuild_active_outputs()
            message = f"Updated {updated} entr{'y' if updated == 1 else 'ies'} and regenerated the XML."
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(render_page(message, run_dir))
            return
        if self.path != "/upload":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        files, fields = parse_multipart(self.rfile.read(length), self.headers.get("Content-Type", ""))
        if fields.get("append_batch") != "yes":
            ACTIVE_FILES.clear()
            global LAST_RUN_DIR
            LAST_RUN_DIR = None
        set_date_parse_mode(fields.get("date_format", "auto"))
        bank_ledger_override = fields.get("bank_ledger", "").strip()
        if not bank_ledger_override:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(render_page("Enter the exact Tally bank ledger name before processing."))
            return
        set_bank_ledger_name(bank_ledger_override)
        entries: list[Entry] = []
        raw_extracts: list[dict] = []
        for filename, data in files:
            save_path = UPLOADS_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{filename}"
            save_path.write_bytes(data)
            try:
                new_entries, raw = process_file(save_path, bank_ledger_override)
                new_entries = filter_entries_by_date(new_entries, fields.get("date_from", ""), fields.get("date_to", ""))
                file_id = uuid.uuid4().hex
                ACTIVE_FILES[file_id] = {"filename": filename, "entries": new_entries, "raw_extracts": [raw]}
                entries.extend(new_entries)
                raw_extracts.append(raw)
            except Exception as exc:
                error_entry = Entry(filename, "Error", "Journal", "", DEFAULT_SUSPENSE_LEDGER, DEFAULT_SUSPENSE_LEDGER, DEFAULT_SUSPENSE_LEDGER, 0, str(exc), "Low", "Yes")
                file_id = uuid.uuid4().hex
                ACTIVE_FILES[file_id] = {"filename": filename, "entries": [error_entry], "raw_extracts": [{"file": filename, "error": str(exc)}]}
                entries.append(error_entry)
                raw_extracts.append({"file": filename, "error": str(exc)})
        run_dir = rebuild_active_outputs()
        total_entries = len(active_entries())
        message = f"Added {len(files)} file(s). Current batch has {total_entries} entr{'y' if total_entries == 1 else 'ies'}."
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(render_page(message, run_dir))

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} - {fmt % args}")


class SingleBotServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


if __name__ == "__main__":
    stop_other_servers_on_port()
    server = SingleBotServer((HOST, PORT), Handler)
    print(f"TallyPrime Entry Prep Bot running at http://{HOST}:{PORT}")
    print("Press Ctrl+C to stop.")
    server.serve_forever()
