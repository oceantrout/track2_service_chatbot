"""Unit test: the FAQ path end-to-end (index → search → answer → citation).

Run:  PYTHONPATH=. python _t_faq.py

All nine judged FAQ questions from conversations.jsonl, plus the shape
checks the acceptance matrix needs: every non-abstaining answer must cite
a real section title on a "Source:" line, and the quoted text must be
grounded in that section (citation.grounded). C14's two questions must
abstain with the exact deck text.
"""

from __future__ import annotations

import json
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from faq.answer import ABSTAIN_TEXT, FaqEngine
from faq.citation import grounded
from faq.index import load_sections

PASS = 0
FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
    else:
        FAIL += 1
        print(f"FAIL {name}\n  got : {got!r}\n  want: {want!r}")


engine = FaqEngine(backend="tfidf")
titles = {s.title for s in load_sections()}

rows = [json.loads(l) for l in open("test_set/conversations.jsonl", encoding="utf-8")]
seen = []
for conv in rows:
    for t in conv.get("turns", []):
        q = t.get("user") or t.get("message") or ""
        if (t.get("expected_route") == "faq" or (t.get("expected_section") is not None)) and q:
            seen.append((conv["id"], q, t.get("expected_section")))

for cid, q, want in seen:
    result = engine.answer(q)
    if want is None:
        check(f"{cid} abstain :: {q[:35]}", result["answer"], ABSTAIN_TEXT)
    else:
        check(f"{cid} section :: {q[:35]}", result["section"], want)
        body = result["answer"].rsplit("\nSource:", 1)[0]
        section = [s for s in engine.search.sections if s.title == want][0]
        check(f"{cid} grounded :: {q[:35]}", grounded(body, section), True)
        check(f"{cid} source line :: {q[:35]}", result["answer"].endswith(f"Source: {want}"), True)

# structural: every cited title is a real heading in the KB
for cid, q, want in seen:
    r = engine.answer(q)
    if r["section"] is not None:
        check(f"{cid} title in KB", r["section"] in titles, True)

# spell-fix feature (guards/typo.py, live-UI request): one-edit typos of
# content words resolve against the KB vocabulary — insert, delete,
# substitute AND transpose — while C14's genuine unknowns keep abstaining.
from guards.typo import correct, damerau_1

for a, b, want in [("servie", "service", True),        # insert
                   ("srevice", "service", True),       # transpose
                   ("servicee", "service", True),      # delete
                   ("servcie", "service", True),       # transpose
                   ("srevcie", "service", False),      # two edits: hands off
                   ("book", "books", True)]:
    check(f"damerau_1({a!r},{b!r})", damerau_1(a, b), want)
# fold-uniqueness: a fold that maps service/services to ONE meaning makes
# "servicee" unambiguous; with identity fold it is two candidate meanings.
check("correct servicee (folded)", correct("what is your servicee",
                                          {"service", "services"},
                                          fold=lambda w: w[:7]),
      "what is your service")
# "toook" is TWO edits from book but one from took — with took in the
# vocab the nearest neighbour is unique and the fix is simply right;
# without it, "took" itself would be rewritten to "book". That is exactly
# why the router's COMMON_NEIGHBOURS exists. reound touches round AND
# refund → ambiguous → hands off.
check("correct toook→took (vocab has took)", correct("i toook my car",
                                                    {"book", "took"}, fold=str),
      "i took my car")
check("correct took→book WITHOUT shield vocab", correct("i took my car",
                                                        {"book"}, fold=str),
      "i book my car")
check("correct ambiguous reound", correct("got a good reound",
                                          {"round", "refund"}, fold=str),
      "got a good reound")
check("correct short word untouched", correct("what is my coe",
                                              {"code"}, fold=str),
      "what is my coe")

for q, want in [
    ("may I know the servie you offered", "Services and prices"),
    ("what is your srevice", "Services and prices"),
    ("what is your servicee", "Services and prices"),
    ("what is youur servcie", "Services and prices"),
    ("Do you cover COE renewal?", None),
    ("do you handle car insurance claims?", None),
]:
    r = engine.answer(q)
    if want is None:
        check(f"spellfix abstain :: {q[:35]}", r["answer"], ABSTAIN_TEXT)
    else:
        check(f"spellfix section :: {q[:35]}", r["section"], want)

print(f"\n_t_faq: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
