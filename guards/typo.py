"""guards/typo.py — one-edit spellfix: deterministic, vocabulary-aware, conservative.

The deck's doctrine is *code for rules*, and typo tolerance is a rule, not
a guess: a word is replaced only when EVERY one-edit neighbour (Damerau:
insert / delete / substitute / transpose) in the target vocabulary folds to
the SAME stem. That is what makes it safe:

    "srevice"   -> "service"   (one transposition, unique meaning)
    "servicee"  -> "service"   (one deletion; "service"/"services" are one
                                answer, not two — folded before deciding)
    "booooking" -> "booking"   (held-key repeat: collapsing consecutive
                                duplicate letters lands on exactly ONE
                                vocabulary word — second rule, same
                                uniqueness requirement as the first)
    "coee"      -> untouched   (ambiguous or absent — the abstain keeps its
                                teeth: C14's COE/insurance never resolve)

Used at three places, and deliberately NOT a fourth:
  * faq/search.py   — corrects against the KB vocabulary before the
    unknown-token guard and BM25 scoring;
  * router/router.py — corrects against the intent keyword vocabulary so
    "can i make a boooking" still reaches the booking flow (and the model
    path, if enabled, is fed the corrected text too);
  * booking/state_machine.py — corrects the DETECTION copy of a turn only,
    so command/intent regexes fire for "booooking" while extract_* and
    validate_* keep reading the raw text;
  * never the booking machine's VALUE fields — a customer's name, plate or
    email must arrive byte-for-byte, autocorrect has no business there.
"""

from __future__ import annotations

import re

WORD_RE = re.compile(r"[a-z0-9][a-z0-9%\-]*")


def damerau_1(a: str, b: str) -> bool:
    """True when b is exactly one insert/delete/substitute/transpose from a."""
    if a == b:
        return False
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    # single transposition
    if la == lb:
        for i in range(la - 1):
            if a[i] == b[i + 1] and a[i + 1] == b[i]:
                if a[:i] == b[:i] and a[i + 2:] == b[i + 2:]:
                    return True
    i = j = diff = 0
    while i < la and j < lb:
        if a[i] != b[j]:
            diff += 1
            if diff > 1:
                return False
            if la > lb:
                i += 1          # deletion from a
            elif lb > la:
                j += 1          # insertion into a
            else:
                i += 1
                j += 1          # substitution
        else:
            i += 1
            j += 1
    diff += (la - i) + (lb - j)
    return diff <= 1


def collapse_repeats(w: str) -> str:
    """Fold runs of one letter: 'booooking' -> 'booking'."""
    out = []
    for ch in w:
        if not out or out[-1] != ch:
            out.append(ch)
    return "".join(out)


def correct(text: str, vocab: set[str], fold=None, min_len: int = 4) -> str:
    """Replace unknown words with their unique one-edit meaning.

    fold(token) defines "same meaning" for ambiguity purposes (identity by
    default; the FAQ passes its stemmer so service/services count once).
    Words of length < min_len are never touched — short words are where
    typos and real domain terms collide ("COE", "PIN").

    Second rule (only when the first finds nothing): a held-key typo —
    the word collapses to exactly ONE vocabulary word (unique after fold),
    and the collapsed form is not itself ambiguous against the vocabulary.
    """
    if fold is None:
        fold = lambda w: w

    def repl(m):
        w = m.group(0)
        low = w.lower()
        if len(low) < min_len or low in vocab:
            return w
        cands = sorted(v for v in vocab if damerau_1(low, v))
        if not cands:
            cl = collapse_repeats(low)
            if cl != low:
                cands = sorted(v for v in vocab if collapse_repeats(v) == cl)
        if cands and len({fold(c) for c in cands}) == 1:
            return cands[0]
        return w

    return WORD_RE.sub(repl, text.lower())
