"""Smoke test: the chatbot front-end (app.py) via Streamlit AppTest.

Run:  PYTHONPATH=. python _t_chatapp.py

Proves a judge can: open the page, book a servicing end-to-end through the
chat box, see the step panel move, keep PII masked in the panel while the
chat itself may show raw values, switch to the trace page, and replay a
preset — all with no exceptions.

Housekeeping, because this test writes REAL rows through the REAL pipeline:

  * data/bookings.csv and logs/trace.jsonl are archived before the run and
    restored after (same discipline as run_acceptance.py) — otherwise a
    test booking occupies its slot and the next run sees "09:00 already
    taken" (the live availability check working exactly as specified);
  * bookings start header-only, so the first booking on 27 Oct is
    deterministically KAC-261027-001.

AppTest quirks handled here:
  * a widget's set_value STICKS across runs (unlike the browser, which
    clears chat_input after sending) — so every scripted send is followed
    by an explicit clear before any run that isn't a send;
  * the sidebar has two selectboxes ("FAQ backend" first, then the
    preset "Conversation") — address the preset by its options, not by 0;
  * the backend panel is LOGIN-GATED (demo admin): the test exercises the
    gate (hidden by default, wrong credentials rejected, login reveals,
    hide re-locks) and then runs the judge journey unlocked.
"""

from __future__ import annotations

import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from streamlit.testing.v1 import AppTest

from core import config

BOOKINGS_HEADER = ("booking_ref,created_at,service_code,vehicle_plate,"
                  "appointment_date,appointment_time,name,mobile,email,"
                  "consent_given_at\n")

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"FAIL {name}")


def main() -> None:
    global PASS, FAIL
    bk, tr = config.BOOKINGS_PATH, config.TRACE_PATH
    bk_backup = bk.read_text(encoding="utf-8") if bk.exists() else None
    tr_backup = tr.read_text(encoding="utf-8") if tr.exists() else None
    bk.parent.mkdir(parents=True, exist_ok=True)
    bk.write_text(BOOKINGS_HEADER, encoding="utf-8")
    try:
        run_checks()
    finally:
        if bk_backup is not None:
            bk.write_text(bk_backup, encoding="utf-8")
        else:
            bk.write_text(BOOKINGS_HEADER, encoding="utf-8")
        if tr_backup is not None:
            tr.write_text(tr_backup, encoding="utf-8")
        elif tr.exists():
            tr.unlink()


def run_checks() -> None:
    at = AppTest.from_file("app.py", default_timeout=30)
    at.run()
    check("page loads", not at.exception)

    # ---- backend-panel gate -------------------------------------------------
    def gate(label_part):
        return next(b for b in at.button if label_part in b.label)

    def login(user: str, pwd: str):
        gate("Show backend info").click()
        at.run()
        at.text_input[0].set_value(user)
        at.text_input[1].set_value(pwd)
        gate("Unlock").click()
        at.run()

    check("panel hidden by default", len(at.sidebar.metric) == 0)
    check("README §6 while locked: step strip + masked trace visible",
          any("Progress:" in m.value for m in at.markdown))
    login("Admin", "wrong-pass")
    check("wrong password rejected", bool(at.error) and len(at.sidebar.metric) == 0)
    login("Admin", "Admin123")
    check("admin login reveals panel", len(at.sidebar.metric) > 0)
    gate("Hide backend info").click()
    at.run()
    check("hide re-locks the panel", len(at.sidebar.metric) == 0)
    login("Admin", "Admin123")  # leave unlocked for the judge journey below

    def send(text: str):
        at.chat_input[0].set_value(text)
        at.run()

    def clear_input():
        at.chat_input[0].set_value("")
        at.run()

    def mds() -> list[str]:
        return [m.value for m in at.markdown]

    send("Hi, I'd like to book a basic servicing")
    check("no exception after turn 1", not at.exception)
    check("asks plate", any("vehicle plate" in m for m in mds()))
    check("step strip tracks the current step",
          any("Progress:" in m and "vehicle plate" in m for m in mds()))
    metrics = {mt.label: str(mt.value) for mt in at.sidebar.metric}
    check("state moved to vehicle_plate", metrics.get("State") == "vehicle_plate")

    send("SBA 1234 K")
    check("asks date", any("Which date" in m for m in mds()))

    send("next Tuesday")
    offer = next((m for m in mds() if "Tue 27 Oct 2026" in m and "Which time" in m), "")
    check("offers 27 Oct times", bool(offer))
    free = re.findall(r"\b\d{2}:\d{2}\b", offer)
    check("free times listed", len(free) >= 1)

    # send whatever the app actually says is free — never hardcode a slot
    send(free[0])
    check("asks name", any("name" in m.lower() for m in mds()))

    for msg in ["Tan Wei Ming", "9123 4567", "weiming.tan@example.com"]:
        send(msg)
    check("asks consent", any("Do you agree" in m for m in mds()))
    check("step strip masks PII too",
          any("Captured (masked)" in m and "9*** 4567" in m
              and "w***@example.com" in m and "weiming.tan@example.com" not in m
              for m in mds()))

    send("yes")
    send("yes")
    check("no exception through full booking", not at.exception)
    check("reference issued", any("KAC-261027-001" in m for m in mds()))
    check("step strip shows booked",
          any("Progress:" in m and "booked" in m and "KAC-261027-001" in m for m in mds()))

    # the captured-fields panel must be masked even though the chat shows raw
    # (the panel lives in the sidebar now — the "Live session" dashboard)
    clear_input()
    panel = ""
    all_mds = [m.value for m in at.sidebar.markdown]
    for i, m in enumerate(all_mds):
        if m.startswith("Captured (masked):"):
            panel = all_mds[i + 1] if i + 1 < len(all_mds) else ""
    check("panel found", bool(panel))
    check("panel masks mobile", "91234567" not in panel and "9*** 4567" in panel)
    check("panel masks email", "weiming.tan@example.com" not in panel and "w***@example.com" in panel)
    check("panel masks plate", "SBA1234K" not in panel and "S****K" in panel)

    # trace page
    at.sidebar.radio[0].set_value("Trace & metrics")
    at.run()
    check("trace page loads", not at.exception)
    check("trace has rows", len(at.dataframe) > 0)
    # README "Then add" 7: tokens, cost and seconds per conversation,
    # before and after one improvement — the page must show all of it.
    cols = [str(c) for df in at.dataframe for c in df.value.columns]
    check("§7 meter: tokens/cost/seconds columns",
          any("tokens in" in c for c in cols) and any("est cost" in c for c in cols)
          and any("seconds" in c for c in cols))
    check("§7 before/after table present",
          any("before (TF-IDF)" in c for c in cols))
    mlabels = [mt.label for mt in at.metric]
    check("§7 summary chips include Est cost", "Est cost" in mlabels)

    # preset replay: C12 (out of scope), addressed by options not by index
    at.sidebar.radio[0].set_value("Chat")
    at.run()
    preset = next(s for s in at.sidebar.selectbox if any(o.startswith("C01") for o in s.options))
    c12 = next(o for o in preset.options if o.startswith("C12"))
    preset.set_value(c12)
    at.run()
    replay = [b for b in at.sidebar.button if "Replay" in getattr(b, "label", "")]
    (replay[0] if replay else at.sidebar.button[1]).click()
    at.run()
    check("preset replay no exception", not at.exception)
    check("C12 redirected",
          any("I can only help with this workshop's services" in m for m in mds()))

    gate("Hide backend info").click()
    at.run()
    check("panel hides again after use", len(at.sidebar.metric) == 0)

    # error4.png reproduction, fresh app (chat page works with panel locked):
    # mid-flow "no. I want to change from the begining" must restart at step 1
    # with an acknowledgement — not be charged as a bad email value.
    at2 = AppTest.from_file("app.py", default_timeout=30)
    at2.run()

    def send2(text: str):
        at2.chat_input[0].set_value(text)
        at2.run()

    for t in ["book a basic servicing", "SBA1234K", "29 Oct", "1pm",
              "Tan Wei Ming", "9123 4567"]:
        send2(t)
    check("go-back journey reaches the email step",
          any("email" in m.lower() for m in (x.value for x in at2.markdown)))
    send2("no. I want to change from the begining")
    mds2 = [m.value for m in at2.markdown]
    check("error4: go-back acknowledged + service list shown",
          any("go back to the beginning and select a service" in m and "We offer" in m
              for m in mds2))
    check("error4: step strip back at service",
          any("Progress:" in m and "service ◀" in m for m in mds2))

    # error6.png reproduction: "can I change service" two steps into a fresh
    # booking — must reset to step 1 with the canned priced list. Before the
    # fix it went to the FAQ (question shape won), which dumped the raw KB
    # pricing section ("Source: Services and prices") and the state stayed
    # at vehicle_plate.
    send2("basic servicing")
    send2("SBA9999Z")
    send2("can I change service")
    mds3 = [m.value for m in at2.markdown]
    check("error6: change-service answers with priced list",
          any("pick the service again" in m and "SGD 128" in m and "Which service" in m
              for m in mds3))
    check("error6: no FAQ dump (no Source: citation)",
          not any("Source: Services and prices" in m for m in mds3))
    check("error6: state reset to service",
          any("Progress:" in m and "service ◀" in m for m in mds3))


if __name__ == "__main__":
    main()
    print(f"\n_t_chatapp: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
