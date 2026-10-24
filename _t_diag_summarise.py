# Diagnostic: why "Summarise all your policies and rules for customers"
# answers like TF-IDF even with the graphrag toggle on.
from graphrag_demo.query import QueryEngine
from graphrag_demo.router import Router

q = "Summarise all your policies and rules for customers"

r = Router()
route = r.route(q)
print("DEMO ROUTER ->", route["mode"])
print("  reasons:", route.get("reasons"))

eng = r.engine
for mode in ("local", "global", "hybrid"):
    res = eng.query(q, mode=mode)
    print(
        f"{mode:7s} | best_score {res['best_score']:.4f} | abstain {res['abstain']} "
        f"| n_evidence {len(res['evidence'])}"
    )
    secs = sorted({e.get("section_title") for e in res["evidence"]})
    print("   sections:", secs[:8])
    for e in res["evidence"][:3]:
        print(f"   ev: {e['score']:.3f} [{e.get('section_title')}] {e['text'][:70]}")

# what the CHATBOT actually gets from its adapter:
from faq.search import GraphRagSearch
hit = GraphRagSearch().hit(q)
print("\nCHATBOT GraphRagSearch.hit ->", (hit[0], hit[1].title if hit else None))

from faq.answer import FaqEngine
ans = FaqEngine(backend="graphrag").answer(q)
print("answer section:", ans["section"], "| score:", ans["score"])
print("answer:\n" + ans["answer"])

for neg in [
    "What's the COE price this month?",
    "Do you handle insurance claims for accident repairs?",
]:
    a = FaqEngine(backend="graphrag").answer(neg)
    print(f"\nNEGATIVE {neg!r} -> abstained={a['abstained']} section={a['section']}")
