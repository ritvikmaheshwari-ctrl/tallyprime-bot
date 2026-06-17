import xml.etree.ElementTree as ET
from pathlib import Path


def amount_text(node: ET.Element) -> float:
    raw = (node.findtext("AMOUNT") or "0").strip()
    try:
        return float(raw)
    except ValueError:
        return 0.0


root = ET.fromstring(Path("outputs/tallyprime_bot/runs/tally_purchase_export.xml").read_text(encoding="utf-8-sig"))
vouchers = root.findall(".//VOUCHER")
purchase_total = 0.0
gst_totals = {"Input CGST": 0.0, "Input SGST": 0.0, "Input IGST": 0.0}
gstr2b_count = 0
party_counts: dict[str, int] = {}
party_purchase: dict[str, float] = {}
for voucher in vouchers:
    narration = (voucher.findtext("NARRATION") or "").strip()
    party = (voucher.findtext("PARTYLEDGERNAME") or "").strip()
    is_gstr2b = "GSTR-2B invoice" in narration
    if is_gstr2b:
        gstr2b_count += 1
        party_counts[party] = party_counts.get(party, 0) + 1
    for ledger_entry in voucher.findall("ALLLEDGERENTRIES.LIST"):
        ledger = (ledger_entry.findtext("LEDGERNAME") or "").strip()
        amount = amount_text(ledger_entry)
        debit_value = -amount if amount < 0 else amount
        if ledger == "Purchase Accounts":
            purchase_total += debit_value
            if is_gstr2b:
                party_purchase[party] = party_purchase.get(party, 0.0) + debit_value
        if ledger in gst_totals:
            gst_totals[ledger] += debit_value

print(f"Purchase vouchers exported from Tally: {len(vouchers)}")
print(f"GSTR-2B purchase vouchers found in Tally: {gstr2b_count}")
print(f"Purchase Accounts total in Tally export: {purchase_total:.2f}")
for ledger, total in gst_totals.items():
    print(f"{ledger} total in Tally export: {total:.2f}")
print("GSTR-2B party voucher counts:")
for party, count in sorted(party_counts.items()):
    print(f"  {party}: {count} vouchers, purchase {party_purchase.get(party, 0.0):.2f}")
