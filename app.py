"""app.py — the Track 2 chatbot front-end (Streamlit), deck demo script's stage.

Layout (a judge reads it left to right):

  * **Sidebar (LHS)** — everything the machine is doing: the live booking
    step (state / attempts / outcome), the captured fields masked exactly
    like the trace, the latest trace record, plus the controls (FAQ
    backend, router-model toggle, reset) and the preset replay C01–C14.
    This backend panel is HIDDEN by default: a **Show backend info** button
    above the chat reveals a login form (Admin credentials), and showing
    the panel requires it; **Hide backend info** locks it again.
  * **Main (chat)** — the real orchestrator behind `handle_turn()`, in
    standard chat order (newest at the tail, right above the pinned input),
    with a Clear-messages button next to the send box.

  A second page, **Trace & metrics**, is the README-7 cost meter: tokens,
  estimated cost and seconds **per conversation** (live trace, falling back
  to the acceptance-run receipt), the per-turn masked table, and the
  measured **before vs after** of one improvement (retrieval TF-IDF →
  GraphRAG). Rules-first spends 0 model calls against a ceiling of 2, so
  the money column reads $0.00 until the opt-in model path is switched on.

Run:  streamlit run app.py --server.port 8601
"""

from __future__ import annotations

import hmac
import json
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

from booking.state_machine import BookingMachine, Session
from core import config
from core.orchestrator import Orchestrator
from faq.answer import FaqEngine
from guards import redact
from tracking.trace import TraceRecorder

st.set_page_config(page_title="Kallang Auto Care — Track 2 chatbot", layout="wide")

# ---- workshop theme (DARK) ----------------------------------------------------
# Injected per-page (not .streamlit/config.toml) so the GraphRAG demo sharing
# this project directory on :8600 keeps its own look. Why dark: the judges'
# browsers run OS dark mode, so Streamlit's "auto" theme already renders the
# header, widgets and input bar dark with near-white text — a previous light
# palette then laid that white text on our light cards and it vanished
# (user screenshot error1.png). Rule kept from that lesson: EVERY custom
# surface pins its own text colour; never recolour a whole container `*`.

_CSS = """
<style>
:root {
  --kak-bg: #10151f;         /* night-steel canvas */
  --kak-panel: #1a2230;      /* sidebar panel */
  --kak-card: #222d40;       /* cards, bubbles, metric chips */
  --kak-line: #2e3a52;       /* borders on dark */
  --kak-text: #f2f5fa;       /* primary text — white */
  --kak-dim: #9fb0cc;        /* secondary text — steel grey-blue */
  --kak-accent: #ff7a45;     /* hi-vis orange (pops on dark) */
  --kak-amber: #f2a104;      /* safety yellow — the accent the eye follows */
}

html, body, [class*="css"] {
  font-family: "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  color: var(--kak-text);
}
header[data-testid="stHeader"], footer { visibility: hidden; }
/* EXCEPT the sidebar re-expand control: the header is hidden for looks, but
   hiding its control made a collapsed sidebar unrecoverable — the collapse
   state persists in the browser, so an admin who logged in then saw an empty
   left panel forever (error3.png). Pin the toggle top-left, above everything. */
header [data-testid="stSidebarCollapsedControl"],
header .stSidebarCollapsedControl,
header [data-testid="stExpandSidebarButton"] {
  visibility: visible;
  position: fixed; left: 12px; top: 10px; z-index: 999;
  background: var(--kak-card); color: var(--kak-text);
  border: 1px solid var(--kak-line); border-radius: 8px; padding: 6px 10px;
  cursor: pointer;
}
header [data-testid="stSidebarCollapsedControl"] svg,
header .stSidebarCollapsedControl svg,
header [data-testid="stExpandSidebarButton"] svg { fill: var(--kak-text); }
.block-container { padding-top: 1.4rem; padding-bottom: 6rem; max-width: 980px; }

/* ---------- canvas + sidebar: dark panels, white text ---------- */
[data-testid="stAppViewContainer"], [data-testid="stMainBlockContainer"], [data-testid="stMain"] {
  background: var(--kak-bg);
}
[data-testid="stSidebar"] { background: var(--kak-panel); border-right: 1px solid var(--kak-line); }
[data-testid="stSidebar"] h1 { color: #ffffff; font-size: 1.45rem; letter-spacing: .2px; }
[data-testid="stSidebar"] h2, [data-testid="stSidebar"] h3 {
  color: var(--kak-amber); font-size: 1.02rem; letter-spacing: .3px;
}
[data-testid="stSidebar"] hr { border-color: var(--kak-line); }
/* the texts that were white-on-white in the screenshot, pinned explicitly */
[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p,
[data-testid="stSidebar"] li,
[data-testid="stSidebar"] [data-testid="stCaptionContainer"],
[data-testid="stSidebar"] [role="radio"] p { color: var(--kak-text); }
[data-testid="stSidebar"] .stButton > button {
  background: var(--kak-card); color: var(--kak-text);
  border: 1px solid var(--kak-line); border-radius: 10px; font-weight: 600;
}
[data-testid="stSidebar"] .stButton > button:hover {
  border-color: var(--kak-amber); color: #ffffff;
}

/* ---------- metric chips: background + text pinned together ---------- */
[data-testid="stMetric"] {
  background: var(--kak-card); border: 1px solid var(--kak-line);
  border-left: 4px solid var(--kak-accent); border-radius: 10px; padding: 8px 12px;
}
[data-testid="stMetric"] [data-testid="stMetricLabel"] p { color: var(--kak-dim); }
[data-testid="stMetricValue"] div, [data-testid="stMetricValue"] { color: var(--kak-text) !important; }

/* ---------- chat ---------- */
.kak-hero {
  background: var(--kak-card); color: #ffffff; padding: 12px 18px;
  border-radius: 12px; border-left: 5px solid var(--kak-amber);
  font-weight: 600; letter-spacing: .2px; margin-bottom: 8px;
}
[data-testid="stChatMessage"] {
  background: var(--kak-card); border: 1px solid var(--kak-line);
  border-left: 4px solid var(--kak-amber);
  border-radius: 14px; padding: 12px 16px; margin-bottom: 10px;
}
[data-testid="stChatMessage"] .stMarkdown, [data-testid="stChatMessage"] p { color: var(--kak-text); }
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p { color: var(--kak-dim); }

/* ---------- alerts (empty-state hint) and headings on dark ---------- */
[data-testid="stAlert"] { background: var(--kak-card); border: 1px solid var(--kak-line); border-radius: 10px; }
[data-testid="stAlert"] p { color: var(--kak-text); }
[data-testid="stMain"] h1, [data-testid="stMain"] h2, [data-testid="stMain"] h3 { color: #ffffff; }
[data-testid="stMain"] .stMarkdown p { color: var(--kak-text); }

pre, code { font-family: Consolas, "Cascadia Mono", monospace; }

/* main-area buttons (Clear messages) — card style, amber hover */
[data-testid="stMain"] .stButton > button {
  background: var(--kak-card); color: var(--kak-text);
  border: 1px solid var(--kak-line); border-radius: 10px; font-weight: 600;
}
[data-testid="stMain"] .stButton > button:hover {
  border-color: var(--kak-accent); color: #ffffff;
}

/* admin-login form — pinned surface (lesson: never inherit, always pin) */
[data-testid="stForm"] {
  background: var(--kak-panel); border: 1px solid var(--kak-line);
  border-radius: 12px; padding: 14px 16px;
}
[data-testid="stForm"] label, [data-testid="stForm"] label * { color: var(--kak-text); }
[data-testid="stForm"] input {
  background: var(--kak-card) !important; color: var(--kak-text) !important;
  border: 1px solid var(--kak-line) !important; border-radius: 8px;
}
[data-testid="stForm"] [data-testid="stInputTypeSelectorContainer"] button { color: var(--kak-dim); }
[data-testid="stFormSubmitButton"] > button {
  background: var(--kak-accent); color: #ffffff;
  border: none; border-radius: 10px; font-weight: 600;
}
[data-testid="stFormSubmitButton"] > button:hover { background: #ff8f60; color: #ffffff; }
</style>
"""
st.markdown(_CSS, unsafe_allow_html=True)


@st.cache_resource
def shared_engines():
    return BookingMachine(), FaqEngine(backend="tfidf")


@st.cache_resource
def graph_engine():
    """Built once — a fresh FaqEngine(backend='graphrag') takes ~2.7 s and
    the sidebar swap re-ran on EVERY rerun, taxing each message."""
    return FaqEngine(backend="graphrag")


def fresh_orchestrator() -> Orchestrator:
    machine, faq = shared_engines()
    recorder = TraceRecorder()
    return Orchestrator(machine=machine, faq_engine=faq, recorder=recorder)


def init_state() -> None:
    if "chat" not in st.session_state:
        st.session_state.chat = []           # list of {"role","text","meta"}
        st.session_state.booking = Session()
        st.session_state.orch = fresh_orchestrator()
        st.session_state.totals = {"turns": 0, "calls": 0, "latency_ms": 0.0}
        # backend-panel gate: hidden by default, shown only after admin login
        st.session_state.admin_authed = False
        st.session_state.login_open = False


init_state()

# ---- left sidebar: live session (the machine's dashboard) + controls --------

def masked_view(session: Session) -> str:
    lines = []
    for field, value in session.filled.items():
        if not value:
            continue
        shown = redact.mask_for_trace(value) if field in ("mobile", "email", "vehicle_plate") else value
        lines.append(f"- **{field}**: {shown}")
    return "\n".join(lines) or "*(no fields captured yet)*"


BOOKING_STEPS = ["service", "vehicle_plate", "date", "time_slot", "name",
                 "mobile", "email", "consent", "confirm"]


def render_progress(session: Session) -> None:
    """README "Then add" §6: the page must show chat + the current booking
    step + a masked trace — that duty is NOT gated by the admin panel (the
    gate hides internals: attempts, raw trace JSON, controls, replay). This
    strip is the customer-level view of the same truth, always visible."""
    state = session.state
    if state == "idle":
        st.markdown("**Progress:** no booking in progress — ask “Can I make "
                    "a booking?” to start one.")
        return
    if state == "done":
        st.markdown(f"**Progress:** booked ✓ — reference `{session.booking_ref}`.")
        return
    parts = []
    for s in BOOKING_STEPS:
        label = s.replace("_", " ")
        if s == state:
            parts.append(f"**{label} ◀**")
        elif (bool(session.consent_given_at) if s == "consent"
              else bool(session.filled.get(s))):
            parts.append(f"~~{label}~~")
        else:
            parts.append(label)
    line = "**Progress:** " + " → ".join(parts)
    captured = []
    for fld, value in session.filled.items():
        if value:
            captured.append(f"{fld.replace('_', ' ')} "
                            + (redact.mask_for_trace(value)
                               if fld in ("mobile", "email", "vehicle_plate") else value))
    if captured:
        line += "\n\n**Captured (masked):** " + " · ".join(captured)
    st.markdown(line)


with st.sidebar:
    st.title("Kallang Auto Care")
    st.caption("Track 2 · rules-first workshop assistant")
    page = st.radio("Page", ["Chat", "Trace & metrics"], label_visibility="collapsed")

    if not st.session_state.admin_authed:
        # Backend info (live session, trace, controls, replay) is hidden until
        # the admin unlocks it with the button above the chat. Navigation and
        # the chat itself stay customer-facing — only this panel is gated.
        st.divider()
        st.caption("🔒 Backend panel hidden — press **Show backend info** above the chat and log in to reveal it.")
    else:
        st.subheader("Live session")
        st.metric("State", st.session_state.booking.state)
        st.metric("Attempts", st.session_state.booking.attempts.get(st.session_state.booking.state, 0))
        st.markdown("Captured (masked):")
        st.markdown(masked_view(st.session_state.booking))
        if st.session_state.booking.outcome:
            st.metric("Outcome", st.session_state.booking.outcome)
        with st.expander("Latest trace record"):
            rec = st.session_state.orch.recorder
            path = rec.path
            if path.exists():
                lines = path.read_text(encoding="utf-8").strip().splitlines()
                if lines:
                    st.json(json.loads(lines[-1]))

        st.divider()

        machine, faq_engine = shared_engines()
        backend = st.selectbox("FAQ backend", ["tfidf (baseline)", "graphrag (P2 upgrade)"], index=0)
        if backend.startswith("graphrag"):
            st.session_state.setdefault("_faq_swap", None)
            st.info("Swapping to the graph engine (lexical backstop behind it); G8 measured hit@1 44.4% → 77.8% on the judged questions.")
            st.session_state.orch.faq = graph_engine()
        elif st.session_state.orch.faq is not faq_engine:
            st.session_state.orch.faq = faq_engine

        use_model = st.toggle("Router model path (optional)", value=False,
                              help="Rules handle every judged turn with 0 calls; this adds a schema-gated routing suggestion.")
        if use_model != st.session_state.orch.gateway.use_model:
            from core.gateway import Gateway
            st.session_state.orch.gateway = Gateway(use_model=use_model)

        if st.button("Reset conversation"):
            st.session_state.chat = []
            st.session_state.booking = Session()
            st.session_state.orch.recorder = TraceRecorder()

        st.divider()
        st.subheader("Preset replay (test set)")
        convs = [json.loads(l) for l in
                 Path("test_set/conversations.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        chosen = st.selectbox("Conversation", [f"{c['id']} — {c['title']}" for c in convs], key="preset_select")
        if st.button("Replay into chat"):
            conv = convs[[c["id"] for c in convs].index(chosen.split(" ")[0])]
            st.session_state.booking = Session()
            st.session_state.chat = []
            for turn in conv["turns"]:
                res = st.session_state.orch.handle_turn(st.session_state.booking, turn["user"])
                st.session_state.chat.append({"role": "user", "text": turn["user"], "meta": {}})
                st.session_state.chat.append({
                    "role": "assistant", "text": res.reply,
                    "meta": {"route": res.route, "state": f"{res.state_before} → {res.state_after}"},
                })
            st.session_state.totals["turns"] += len(conv["turns"])
            st.rerun()

# ---- main ---------------------------------------------------------------------

# ---- backend-panel gate --------------------------------------------------------
# The sidebar dashboard is the machine's internals (state, trace, controls,
# replay) — hidden from the customer-facing view by default. This button
# shows/hides it; showing requires the admin login for this browser session.
# Demo-only constants: move to config/env (and, for anything beyond a judged
# sandbox, a real session store) before production.
ADMIN_USER = "Admin"
ADMIN_PASS = "Admin123"

gate_status, gate_btn = st.columns([7, 3])
with gate_status:
    if st.session_state.admin_authed:
        st.caption("Backend panel: **shown** (admin session) — if nothing appears "
                   "on the left, the panel is collapsed: click the **≫** control "
                   "at the top-left of the page to open it.")
    else:
        st.caption("Backend panel: **hidden**")
with gate_btn:
    if st.session_state.admin_authed:
        if st.button("🙈 Hide backend info", key="gate_hide", use_container_width=True):
            st.session_state.admin_authed = False
            st.session_state.login_open = False
            st.rerun()
    else:
        if st.button("🔓 Show backend info", key="gate_show", use_container_width=True):
            st.session_state.login_open = True
        if st.session_state.login_open:
            with st.form("admin_login", clear_on_submit=True):
                entered_user = st.text_input("Username", autocomplete="off")
                entered_pass = st.text_input("Password", type="password", autocomplete="off")
                submitted = st.form_submit_button("Unlock", use_container_width=True)
            if submitted:
                if (hmac.compare_digest(entered_user, ADMIN_USER)
                        and hmac.compare_digest(entered_pass, ADMIN_PASS)):
                    st.session_state.admin_authed = True
                    st.session_state.login_open = False
                    st.rerun()
                else:
                    st.error("Invalid username or password.")

st.divider()

if page == "Chat":
    st.markdown(
        '<div class="kak-hero">Service concierge — bookings &amp; workshop FAQ.</div>',
        unsafe_allow_html=True,
    )
    # always-visible README §6 surface: step + masked captures (see render_progress)
    render_progress(st.session_state.booking)
    # chat_input renders pinned to the page bottom wherever it is
    # called; read it BEFORE rendering so the new turn shows in the
    # same script run (no reliance on an implicit rerun).
    user_text = st.chat_input("Type your message…")
    if user_text:
        st.session_state.chat.append({"role": "user", "text": user_text, "meta": {}})
        res = st.session_state.orch.handle_turn(st.session_state.booking, user_text)
        st.session_state.chat.append({
            "role": "assistant", "text": res.reply,
            "meta": {"route": res.route, "state": f"{res.state_before} → {res.state_after}"},
        })
        t = st.session_state.totals
        t["turns"] += 1
        t["calls"] += res.model_calls
        t["latency_ms"] += res.latency_ms
        if res.verify_flags:
            st.session_state["verify_warn"] = res.verify_flags
        # The sidebar dashboard rendered BEFORE this turn was processed
        # (Streamlit runs the sidebar block first), so its State/Attempts
        # would lag a turn. chat_input is one-shot — rerun is safe and
        # shows the fresh state.
        st.rerun()
    if st.session_state.get("verify_warn"):
        st.warning(f"Verify flagged: {st.session_state.pop('verify_warn')}")
    if not st.session_state.chat:
        st.info(
            "**Start with “Can I make a booking?”** — then just answer what "
            "the bot asks, one step at a time:\n\n"
            "1. **Service** — e.g. “basic servicing”, “tyre rotation”, “EV health check”\n"
            "2. **Car plate** — e.g. SBA1234K\n"
            "3. **Date** — Mon–Sat, 26 Oct – 7 Nov 2026\n"
            "4. **Time slot** — pick one of the free times shown (or say “morning”)\n"
            "5. **Your details** — name, 8-digit mobile, email — everything is "
            "shown *masked* in the Live session panel\n"
            "6. **Consent** — nothing is saved until you agree\n\n"
            "Prefer to ask first? Try “what are your opening hours?”. "
            "And “try again” or “cancel” work at any step."
        )
    # Newest at the BOTTOM — standard chat ordering. (Top-first ordering was
    # tried and rejected in live use, error2.png: chat_input pins the viewport
    # to the page bottom, so as messages stacked, the newest drifted up AWAY
    # from what the user could see.) The tail of the conversation now sits
    # right above the input, where the pinned view already is.
    for msg in st.session_state.chat:
        with st.chat_message(msg["role"], avatar="👤" if msg["role"] == "user" else "🛠️"):
            st.markdown(msg["text"])
            if msg["meta"]:
                st.caption(f"route `{msg['meta']['route']}` · state `{msg['meta']['state']}`")
    # The clear button rides at the END of the thread — which the pinned
    # layout puts immediately above the chat_input row, next to the send
    # arrow (chat_input itself cannot host sibling widgets).
    col_stat, col_act = st.columns([5, 2])
    with col_stat:
        t = st.session_state.totals
        st.caption(f"turns {t['turns']} · model calls {t['calls']} (ceiling 2/turn) · "
                   f"{t['latency_ms']:.0f} ms total")
    with col_act:
        if st.button("✖ Clear messages", key="clear_chat", use_container_width=True):
            st.session_state.chat = []
            st.rerun()

else:
    st.subheader("Trace & metrics")
    # README "Then add" 7 — the cost meter: tokens, cost and seconds per
    # conversation, before and after one improvement. Money is a list-price
    # ESTIMATE (config.USD_PER_MTOK_*); the deck's point is that rules-first
    # spends 0 model calls against a ceiling of 2, so the meter reads $0.00
    # until the opt-in model path is switched on.
    def _cost(recs):
        tin = sum(int(r.get("tokens_in") or 0) for r in recs)
        tout = sum(int(r.get("tokens_out") or 0) for r in recs)
        return tin, tout, (tin * config.USD_PER_MTOK_IN + tout * config.USD_PER_MTOK_OUT) / 1e6

    def _conversations(recs):
        # A TraceRecorder restarts its turn counter at 1 (Reset conversation,
        # replay, fresh server run) — so a turn that does not increase marks
        # the start of a new conversation.
        convs, cur = [], []
        for r in recs:
            if cur and int(r.get("turn", 0)) <= int(cur[-1].get("turn", 0)):
                convs.append(cur)
                cur = []
            cur.append(r)
        if cur:
            convs.append(cur)
        return convs

    path = config.TRACE_PATH
    records = []
    if path.exists():
        records = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]

    if records:
        st.markdown("**Per conversation** — this machine's live trace (`logs/trace.jsonl`)")
        st.dataframe(
            [{
                "conversation": f"#{i}",
                "started": str(r0.get("ts", ""))[:19],
                "turns": len(conv),
                "model calls": sum(int(r.get("model_calls_this_turn") or 0) for r in conv),
                "tokens in": tin, "tokens out": tout,
                "est cost (USD)": f"{usd:.4f}",
                "seconds": round(sum(float(r.get("latency_ms") or 0) for r in conv) / 1000, 2),
            } for i, conv in enumerate(_conversations(records), 1)
             for r0 in [conv[0]]
             for tin, tout, usd in [_cost(conv)]],
            width="stretch", hide_index=True,
        )
        tin, tout, usd = _cost(records)
        calls = sum(int(r.get("model_calls_this_turn") or 0) for r in records)
        tot_s = sum(float(r.get("latency_ms") or 0) for r in records) / 1000
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Turns", len(records))
        c2.metric("Model calls", calls, help="Ceiling 2 per turn; rules-first spends 0.")
        c3.metric("Tokens in/out", f"{tin:,} / {tout:,}")
        c4.metric("Est cost", f"${usd:.4f}", help="List-price estimate: $4/M in, $20/M out.")
        c5.metric("Seconds", f"{tot_s:.2f}")
    else:
        art = config.PROJECT_ROOT / "artifacts" / "acceptance.json"
        if art.exists():
            receipt = json.loads(art.read_text(encoding="utf-8"))
            rows = [{
                "conversation": c["id"], "turns": c["turns"], "outcome": c["outcome"],
                "model calls": c["model_calls"],
                "tokens in": 0, "tokens out": 0, "est cost (USD)": "0.0000",
                "seconds": round(c["latency_ms"] / 1000, 2),
            } for c in receipt["conversations"]]
            st.markdown("**Per conversation** — no live trace yet, so this is the "
                        "acceptance-run receipt (`artifacts/acceptance.json`): "
                        f"{len(rows)} conversations, {receipt['pass']} passed, 0 model calls.")
            st.dataframe(rows, width="stretch", hide_index=True)
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Conversations", len(rows))
            c2.metric("Model calls", sum(r["model calls"] for r in rows))
            c3.metric("Est cost", "$0.0000")
            c4.metric("Seconds", f"{sum(r['seconds'] for r in rows):.2f}")
        else:
            st.info("No trace yet — chat or replay a preset first.")

    st.markdown("**Per turn** — the masked trace, newest at the bottom")
    if records:
        st.dataframe(
            [{
                "turn": r["turn"], "route": r["route"],
                "state": f"{r['state_before']}→{r['state_after']}",
                "faq_section": r.get("faq_section_cited"),
                "score": r.get("retrieval_top_score"),
                "validation": r.get("validation"),
                "calls": r.get("model_calls_this_turn"),
                "tok in": r.get("tokens_in", 0),
                "tok out": r.get("tokens_out", 0),
                "est $ (USD)": f"{_cost([r])[2]:.6f}",
                "ms": r.get("latency_ms"),
            } for r in records],
            width="stretch", hide_index=True,
        )
    else:
        st.caption("Nothing recorded in this session yet — the table fills as you chat or replay.")

    with st.expander("Before vs after — the one measured improvement: retrieval TF-IDF → GraphRAG",
                     expanded=True):
        ev_p = config.PROJECT_ROOT / "graphrag_demo" / "artifacts" / "m6_eval.json"
        if ev_p.exists():
            ev = json.loads(ev_p.read_text(encoding="utf-8")).get("summary", {})
            st.markdown(
                f"Measured by `graphrag_demo/evaluate.py` over the {int(ev.get('questions', 9))} "
                f"judged questions — the receipt is `graphrag_demo/artifacts/m6_eval.json`. "
                f"Retrieval-only, so no model is called either side: **+"
                f"{(ev['graphrag_hit1'] - ev['baseline_hit1']) * 100:.1f} pts hit@1 and "
                f"+{(ev['graphrag_abstain_correct'] - ev['baseline_abstain_correct']) * 100:.1f} pts "
                f"abstention bought for +{ev['graphrag_avg_ms'] - ev['baseline_avg_ms']:.2f} ms "
                "of local retrieval — money spent: unchanged at $0.00.**")
            st.dataframe(
                [
                    {"metric": "hit@1", "before (TF-IDF)": f"{ev['baseline_hit1']:.1%}",
                     "after (GraphRAG)": f"{ev['graphrag_hit1']:.1%}"},
                    {"metric": "correct abstention", "before (TF-IDF)": f"{ev['baseline_abstain_correct']:.1%}",
                     "after (GraphRAG)": f"{ev['graphrag_abstain_correct']:.1%}"},
                    {"metric": "global-question coverage (sections/question)",
                     "before (TF-IDF)": f"{ev['baseline_global_coverage']:.2f}",
                     "after (GraphRAG)": f"{ev['graphrag_global_coverage']:.2f}"},
                    {"metric": "avg retrieval time (ms)", "before (TF-IDF)": f"{ev['baseline_avg_ms']:.2f}",
                     "after (GraphRAG)": f"{ev['graphrag_avg_ms']:.2f}"},
                    {"metric": "model calls / est cost per question", "before (TF-IDF)": "0 · $0.0000",
                     "after (GraphRAG)": "0 · $0.0000"},
                ],
                width="stretch", hide_index=True,
            )
        else:
            st.info("Run `python graphrag_demo/evaluate.py` to write the before/after receipt.")

    with st.expander("Full JSONL (last 20)"):
        for r in records[-20:]:
            st.json(r)
