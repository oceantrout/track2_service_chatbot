import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from streamlit.testing.v1 import AppTest

PAGES = [
    "Overview", "G1 Ingest", "G2 Extract", "G3 Graph", "G4 Communities",
    "G5 Retrieve", "G6 Answer", "G9 Workflow A", "G8 Evaluate",
]

fail = 0
for page in PAGES:
    at = AppTest.from_file("app.py", default_timeout=60)
    at.run()
    if at.exception:
        print(f"[BOOT-FAIL] {page}: {at.exception[0].value}")
        fail += 1
        continue
    # switch sidebar radio to this page
    at.sidebar.radio[0].set_value(page)
    at.run()
    if at.exception:
        print(f"[EXC] {page}: {at.exception[0].value}")
        fail += 1
    else:
        warns = [w.value for w in at.warning]
        extras = f" warnings={warns}" if warns else ""
        print(f"[OK ] {page}  elements={len(at.main)}{extras}")

# exercise the interactive paths
at = AppTest.from_file("app.py", default_timeout=60)
at.run()
at.sidebar.radio[0].set_value("G9 Workflow A")
at.run()
at.button[0].click()
at.run()
if at.exception:
    print(f"[EXC] Workflow A run: {at.exception[0].value}")
    fail += 1
else:
    print(f"[OK ] Workflow A button pressed, metrics={len(at.main.get('metric')) if hasattr(at.main,'get') else 'n/a'}")

at = AppTest.from_file("app.py", default_timeout=60)
at.run()
at.sidebar.radio[0].set_value("G6 Answer")
at.run()
at.button[0].click()
at.run()
if at.exception:
    print(f"[EXC] Answer run: {at.exception[0].value}")
    fail += 1
else:
    print("[OK ] Answer button pressed")

at = AppTest.from_file("app.py", default_timeout=60)
at.run()
at.sidebar.radio[0].set_value("G5 Retrieve")
at.run()
if at.exception:
    print(f"[EXC] Retrieve: {at.exception[0].value}")
    fail += 1
else:
    print("[OK ] Retrieve page rendered")

print("FAILURES:", fail)
sys.exit(1 if fail else 0)
