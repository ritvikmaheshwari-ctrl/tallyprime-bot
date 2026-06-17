import re
from pathlib import Path
from urllib.request import Request, urlopen


RUN_DIR = Path("outputs/tallyprime_bot/runs/20260610_164259_857541")
XML_PATH = RUN_DIR / "2_import_vouchers.xml"
TARGET_INVOICE = "AJM252600015"


text = XML_PATH.read_text(encoding="utf-8")
messages = re.findall(r"<TALLYMESSAGE[\s\S]*?</TALLYMESSAGE>", text)
target = next((message for message in messages if TARGET_INVOICE in message), "")
if not target:
    raise SystemExit(f"Could not find invoice {TARGET_INVOICE}")

envelope = f"""<ENVELOPE>
  <HEADER>
    <TALLYREQUEST>Import Data</TALLYREQUEST>
  </HEADER>
  <BODY>
    <IMPORTDATA>
      <REQUESTDESC>
        <REPORTNAME>Vouchers</REPORTNAME>
      </REQUESTDESC>
      <REQUESTDATA>
{target}
      </REQUESTDATA>
    </IMPORTDATA>
  </BODY>
</ENVELOPE>"""

request = Request("http://localhost:9000", data=envelope.encode("utf-8"), method="POST")
response = urlopen(request, timeout=20).read().decode("utf-8", errors="replace")
(RUN_DIR / "single_voucher_import_response.xml").write_text(response, encoding="utf-8")
print(response)
