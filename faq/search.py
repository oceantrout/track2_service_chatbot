"""faq/search.py — synonym map + BM25 over the 12 sections; τ and code decide.

Slide 7's exact synonym map (maintenance → servicing package, polish →
detailing/coating, …) is *code*: the query is expanded before scoring, and
the expanded tokens carry weight 2 — the map is a deliberate semantic
contribution, not a hint. C03 "How much is a maintenance?" reaches
Servicing packages without a model.

Two gates, both deterministic:

  * **unknown-token guard** — every content word of the question must be
    either in the corpus vocabulary or covered by the synonym map. "COE"
    and "insurance" are neither: the KB cannot answer, so we abstain
    before any score is believed (C14's expected outcome);
  * **τ gate** — the BM25 top score must clear FAQ_TAU.

Recorded deviation (workplan §11): scoring is BM25 (k1=1.2, b=0.75), the
TF-IDF family with document-length normalisation. Plain sklearn cosine
systematically short-changed long sections — "Do you do car polish?" put
the 4-line Warranty above the Services-and-prices table because cosine
penalises length; that is exactly the failure the deck's FAQ path must not
have. backend="graphrag" is the measured P2 swap (G8: hit@1 44.4% → 77.8%).
"""

from __future__ import annotations

from collections import Counter

import math
import re

from core import config
from faq import index
from guards import typo

# The listing rule, named so SYNONYMS can define it and expand_tokens can
# gate it (see LISTING_NEUTRAL below).
OFFER_RULE = r"\boffer(s|ed|ing)?\b|\bprovide[sd]?\b"

# The deck's synonym map, verbatim, plus a weight per rule (calibrated).
# The how-much rule expands to price/cost deliberately weakly: a question
# naming a package ("how much is the full servicing package?") must stay
# with its section, not get dragged to the generic price table.
SYNONYMS: dict[str, tuple[list[str], float]] = {
    r"\bmaintenance\b": (["servicing package", "servicing"], 2.0),
    r"\bpolish\b|\bwax\b": (["detailing", "coating"], 2.0),
    r"\bcar wash\b": (["detailing"], 2.0),
    r"\baircon\b|\bair con\b": (["air conditioning"], 2.0),
    r"\btyre\b|\btire\b": (["wheel", "balancing"], 2.0),
    r"\bhow much\b|\bquote\b|\bprice it\b": (["price", "cost"], 0.5),
    r"\bappointment\b": (["booking", "slot"], 2.0),
    # "what services do you offer?" is the global listing question — it
    # must reach the listing sections ("Services and prices" / "Servicing
    # packages"), not the cancellation clause that happens to contain the
    # word "offer". Found in live UI testing. Order matters: "prices" first
    # (the deck's most-specific-first rule) — "package" at full weight
    # dragged "do you offer warranty?" to Servicing packages via that
    # title's x3 boost (that row is now gated by LISTING_NEUTRAL below).
    OFFER_RULE: (["prices", "package"], 2.0),
    # "what is your service?" IS the bare listing question even without
    # "offer" — one token can't clear τ alone, so it borrows the listing
    # expansion at slightly gentler weight than the offer rule (a named
    # package in the question should still outvote this hint).
    r"\bservice\b|\bservices\b": (["prices", "package"], 1.5),
}

# The offer/provide expansion is pure listing help, so it may only fire on
# BARE listing questions — every content token must be one of these neutral
# words. "do you offer warranty?" names a topic (warranty): the question's
# own word must win, not the expansion. Live-UI probe on tfidf put the
# price table above Warranty until this gate existed.
LISTING_NEUTRAL = {"servic", "offer", "provid", "provide", "price", "cost",
                   "packag", "package", "slot", "visit"}

# Irregular folds the suffix rules can't reach, so the pricing-table
# section title ("Services and prices") and "servicing" share a token.
IRREGULAR = {"service": "servic", "services": "servic", "serviced": "servic"}

STOP = {
    "the", "a", "an", "and", "or", "to", "of", "is", "are", "for", "on",
    "in", "at", "you", "your", "do", "does", "can", "i", "we", "it", "be",
    "what", "how", "when", "where", "much", "many", "please", "me", "my",
    "this", "that", "there", "here", "with", "about",
    # conversational fillers the corpus never mentions (calibrated):
    # "off"/"fine" were BM25 artefacts — high idf, zero meaning.
    "off", "ok", "okay", "fine", "by", "way", "get", "got",
    # conversational scaffolding (live-UI found): "may I KNOW the service"
    # — verbs of asking are never the topic; they tripped the unknown-token
    # guard and the whole question died before scoring. "may" also broke
    # the bare-listing test in LISTING_NEUTRAL (below) for nothing.
    "know", "want", "need", "tell", "say", "says", "said", "think",
    "maybe", "probably", "perhaps", "may",
}


def _stem(word: str) -> str:
    """Tiny deterministic suffix fold: parking→park, packages→package.

    The synonym map handles vocabulary; this handles morphology so BM25
    matches what humans mean. Deliberately dumb — no nltk, no surprises.
    """
    if word in IRREGULAR:
        return IRREGULAR[word]
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("ing") and len(word) > 5:
        return word[:-3]
    if word.endswith("ed") and len(word) > 4:
        return word[:-2]  # offered→offer, booked→book (docs fold the same way)
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def tokenize(text: str) -> list[str]:
    cleaned = re.sub(r"['’]s\b", "", text.lower())
    return [
        _stem(w)
        for w in re.findall(r"[a-z0-9][a-z0-9%\-]*", cleaned)
        if w not in STOP
    ]


def expand_tokens(question: str) -> list[tuple[str, float]]:
    """(token, weight): raw question ×2, synonym expansions ×rule-weight.

    A rule is skipped entirely when its expansion words are already raw
    tokens of the question: "how much does a maintenance COST" already
    says cost — adding the price/cost vote again only drags every question
    toward the price table, which is exactly the trap in C03/C11.
    """
    weights: Counter = Counter()
    raw_tokens = set()
    for tok in tokenize(question):
        weights[tok] += 2.0
        raw_tokens.add(tok)
    lowered = question.lower()
    for pattern, (phrases, rule_w) in SYNONYMS.items():
        if re.search(pattern, lowered):
            if pattern == OFFER_RULE and not raw_tokens <= LISTING_NEUTRAL:
                continue  # names a topic — its own words must win, not ours
            expansion_tokens = set()
            for phrase in phrases:
                expansion_tokens.update(tokenize(phrase))
            if expansion_tokens & raw_tokens:
                continue  # the question already speaks this rule's language
            # Order is meaning: the deck lists map targets most-specific
            # first ("polish → detailing, coating"). Phrase 1 carries the
            # rule's full weight; later phrases carry half — related words,
            # not the answer itself. This is what puts "car polish" on the
            # Services-and-prices detailing row instead of the 2-line
            # coating warranty in Warranty.
            for rank, phrase in enumerate(phrases):
                w = rule_w if rank == 0 else rule_w / 2.0
                for tok in tokenize(phrase):
                    weights[tok] += w
    return list(weights.items())


def unknown_unmapped(question: str, vocab: set[str]) -> list[str]:
    """Content words neither in the KB nor explained by the synonym map.

    "Covered" is judged on the QUESTION side: if a synonym rule fired on
    this word in this question (maintenance, polish, …), the word needs no
    KB vocabulary — the map already translated it. COE and insurance match
    no rule and appear nowhere: the KB cannot answer (C14).
    """
    lowered = question.lower()
    rule_words: set[str] = set()
    for pattern, (phrases, _w) in SYNONYMS.items():
        m = re.search(pattern, lowered)
        if m:
            rule_words.update(_stem(w) for w in tokenize(m.group(0)))
            for phrase in phrases:
                rule_words.update(_stem(w) for w in tokenize(phrase))
    bad: list[str] = []
    for tok in tokenize(question):
        if len(tok) < 3:
            continue
        if tok not in vocab and _stem(tok) not in rule_words:
            bad.append(tok)
    return bad


class FaqSearch:
    # k1=2.0 (vs textbook 1.2): "detailing" appears 8× in Services and
    # prices and 2× in Warranty — saturation at 1.2 flattened that signal
    # and let the shorter section win C03's polish question.
    # b=0.5 (vs 0.75): these twelve "documents" range from 86 to 189
    # tokens; 0.75 over-penalised the price table — the section with real
    # substance about polish/detailing — against the 4-line Warranty.
    K1 = 2.0
    B = 0.5

    def __init__(self, sections=None):
        self.sections = sections if sections is not None else index.load_sections()
        # Title ×3 before the body: a cheap two-field BM25F. The section
        # name is the strongest signal that the section is *about* the word
        # ("Location and parking" must win on "park" against a body-length
        # accident in "What to bring").
        self.docs_tokens = [tokenize(f"{s.title} {s.title} {s.title}\n{s.text}") for s in self.sections]
        self.docs_tf = [Counter(toks) for toks in self.docs_tokens]
        self.doc_len = [len(toks) for toks in self.docs_tokens]
        self.avg_len = sum(self.doc_len) / max(1, len(self.doc_len))
        self.df: Counter = Counter()
        for tf in self.docs_tf:
            for tok in tf:
                self.df[tok] += 1
        self.n = len(self.sections)
        self.vocab = set(self.df)
        # RAW (pre-stem) words for the spellfix, plus STOP as "known as-is"
        # — scaffolding words must never be autocorrected into content.
        raw: set[str] = set()
        for s in self.sections:
            raw.update(re.findall(r"[a-z0-9][a-z0-9%\-]*",
                                  f"{s.title}\n{s.text}".lower()))
        self.raw_vocab = raw | STOP

    def _idf(self, tok: str) -> float:
        df = self.df.get(tok, 0)
        return math.log(1 + (self.n - df + 0.5) / (df + 0.5))

    def scores(self, question: str) -> list[tuple[float, index.Section]]:
        weighted = expand_tokens(question)
        out: list[tuple[float, index.Section]] = []
        for i, section in enumerate(self.sections):
            tf, ln = self.docs_tf[i], self.doc_len[i]
            score = 0.0
            for tok, w in weighted:
                f = tf.get(tok, 0)
                if not f:
                    continue
                denom = f + self.K1 * (1 - self.B + self.B * ln / self.avg_len)
                score += w * self._idf(tok) * (f * (self.K1 + 1)) / denom
            out.append((score, section))
        out.sort(key=lambda p: p[0], reverse=True)
        return out

    def top(self, question: str) -> tuple[float, index.Section]:
        return self.scores(question)[0]

    def _fix_typos(self, question: str) -> str:
        """Deterministic one-edit spellfix (guards/typo.py), content words only.

        Guards the guard: "servie" and "srevice" are one keystroke (insert,
        delete, substitute or TRANSPOSE) from the shop's "service", so they
        should reach the KB — but "COE" is 3 letters (never fixed) and
        "insurance" has no unique one-edit neighbour, so C14's abstain
        survives the mercy. A word is replaced only when EVERY one-edit
        neighbour in the raw KB vocabulary folds to the same stem —
        "servicee" touches "service" and "services", and both mean servic,
        so it resolves; ambiguity means hands off.
        """
        return typo.correct(question, self.raw_vocab, fold=_stem)

    def hit(self, question: str, tau: float = config.FAQ_TAU) -> tuple[float, index.Section] | None:
        """The only entry the answer path uses. Below τ, or if the KB simply
        has no vocabulary for the question, there is no hit."""
        question = self._fix_typos(question)
        if unknown_unmapped(question, self.vocab):
            return None
        score, section = self.top(question)
        if score < tau:
            return None
        return score, section


def build(backend: str = "tfidf"):
    if backend in ("tfidf", "bm25"):
        return FaqSearch()
    if backend == "graphrag":
        # The P2 upgrade: same interface, graph-backed scores.
        return GraphRagSearch()
    raise ValueError(f"unknown faq backend {backend!r}")


class GraphRagSearch:
    """Adapter over graphrag_demo's QueryEngine, kept thin and optional.

    Wiring note (fixed at the P2 sanity check): two things silently
    disabled this backend before anyone clicked the toggle —
    (1) the evidence key is `section_title`, not `section`/`title`;
    (2) the engine scores on a 0..1 cosine-ish scale, so comparing
    `best_score` against the BM25-scaled `FAQ_TAU` (3.0) rejected every
    hit. The graph pipeline carries its own calibrated abstention rule
    (`result["abstain"]`, graphrag config.ABSTAIN_THRESHOLD) — that is
    the gate here; the chatbot's τ is a backstop for the lexical backend
    only.

    Lexical backstop (live UI, 22 Oct): the graph's local mode answers
    entity-naming questions ("do you offer warranty?" → Warranty) but
    abstains on GLOBAL listing phrasings ("what services do you offer?")
    that the tuned BM25 handles. So: graph first, BM25 catches what the
    graph abstains on, and the turn only abstains if BOTH fail — the
    toggle is graph-primary-with-backstop, not graph-only. C14 holds:
    the lexical side refuses COE/insurance too.

    Sweep wiring (24 Oct): the adapter used to force mode="local", so the
    graph's global community path (the one thing it has over TF-IDF) never
    ran from the chatbot at all — "summarise all your policies" fell
    through to the lexical backstop and answered with one price chunk.
    The mode now comes from the graph's own rules-based Router, and a
    global-mode hit merges the sections the sweep touched into one
    citable Section: the Source line lists every section, and each quoted
    line is still verbatim from the KB (citation.grounded holds per line).
    """

    def __init__(self):
        from graphrag_demo.query import QueryEngine  # lazy: chatbot runs without it too

        self.engine = QueryEngine()
        self._lexical = FaqSearch()
        from graphrag_demo.router import Router

        self.router = Router(self.engine)  # shares this engine; no second graph build

    def hit(self, question: str, tau=None):
        mode = self.router.route(question)["mode"]
        result = self.engine.query(question, mode=mode)
        from faq import index as faq_index

        sections = {s.title: s for s in faq_index.load_sections()}
        if not (result["abstain"] or not result["evidence"]):
            hits = [
                sections[e["section_title"]]
                for e in result["evidence"]
                if e.get("section_title") in sections
            ]
            if hits:
                if mode == "global" and len({h.title for h in hits}) > 1:
                    return result["best_score"], self._merge_sections(hits)
                return result["best_score"], hits[0]
        return self._lexical.hit(question)  # graph abstained or cited nothing real

    @staticmethod
    def _merge_sections(hits):
        """A sweep answer may cite several sections; keep the contract of
        one Section by concatenating the top five it touched, in evidence
        order. Titles are joined with ';' so callers can still test
        membership of any single section."""
        from faq.index import Section

        seen_titles: list[str] = []
        merged = []
        for section in hits:
            if section.title not in seen_titles:
                seen_titles.append(section.title)
                merged.append(section)
            if len(merged) >= 5:
                break
        return Section(
            "; ".join(seen_titles),
            "\n\n".join(s.text for s in merged),
            min(s.start_line for s in merged),
            max(s.end_line for s in merged),
        )
