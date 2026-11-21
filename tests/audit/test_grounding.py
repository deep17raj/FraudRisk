"""Citation grounding: every cited clause must be verbatim, and fabrications must not survive.

This is the file that defends the quality claim. The invariant asserted throughout is the
strongest one available: each returned clause is `in` the source string, character for
character - not similar to it, not a normalised match of it.
"""
from __future__ import annotations

from audit.grounding import (GroundedSource, ground_quotes, normalize)

SRC = """SUPPLY AGREEMENT

4.1  The Supplier shall deliver the Goods to the Buyer's
warehouse by 1 March 2026, in accordance with the deliv-
ery timetable.

5.2  The Buyer shall pay £10,000 within “thirty (30) days” of invoice.

6.1  The Supplier shall maintain insurance of £2,000,000 — at its own cost — for the term.
"""


def g() -> GroundedSource:
    return GroundedSource.build(SRC)


# --- the offset map ---------------------------------------------------------------------

def test_offsets_always_index_the_original_text():
    norm, offs = normalize(SRC)
    assert len(norm) == len(offs)
    assert all(0 <= o < len(SRC) for o in offs)
    assert offs == sorted(offs), "offsets must advance monotonically through the source"


def test_normalisation_collapses_whitespace_and_folds_typography():
    norm, _ = normalize("The  Supplier shall “deliver” — soon—today")
    assert norm == "The Supplier shall deliver - soon-today"


def test_normalisation_joins_a_hyphen_broken_word():
    norm, _ = normalize("the deliv-\nery timetable")
    assert norm == "the delivery timetable"


# --- locating a real quote --------------------------------------------------------------

def test_an_exact_quote_is_returned_as_a_source_slice():
    hit = g().locate("The Supplier shall deliver the Goods")
    assert hit == "The Supplier shall deliver the Goods" and hit in SRC


def test_a_quote_across_a_line_break_is_located_and_stays_verbatim():
    """The model renders the clause on one line; the document wraps it. Both must agree."""
    quote = "deliver the Goods to the Buyer's warehouse by 1 March 2026"
    hit = g().locate(quote)
    assert hit in SRC
    assert "\n" in hit, "the returned slice keeps the document's own line break"
    # Compared in the normalised view on both sides: the slice carries the document's line
    # break, and normalisation folds apostrophes out of the matching view by design.
    assert normalize(hit)[0] == normalize(quote)[0]


def test_a_quote_with_hyphenation_joined_is_located():
    hit = g().locate("in accordance with the delivery timetable")
    assert hit in SRC and "deliv-\nery" in hit


def test_curly_quotes_in_the_source_do_not_defeat_a_straight_quoted_claim():
    hit = g().locate('The Buyer shall pay £10,000 within "thirty (30) days" of invoice')
    assert hit in SRC and "“thirty (30) days”" in hit


def test_inline_quote_marks_dropped_by_the_model_are_tolerated():
    """A model routinely loses the document's inline quotes around a defined term. That
    difference sits mid-quote, where edge trimming cannot reach it."""
    hit = g().locate("The Buyer shall pay £10,000 within thirty (30) days of invoice")
    assert hit in SRC and "thirty (30) days" in hit


def test_an_em_dash_aside_is_located():
    hit = g().locate("The Supplier shall maintain insurance of £2,000,000 - at its own cost")
    assert hit in SRC


def test_a_preamble_the_model_glued_on_is_trimmed_away():
    hit = g().locate("Clause 4.1 states that The Supplier shall deliver the Goods")
    assert hit in SRC and not hit.lower().startswith("clause")


def test_an_invented_tail_is_trimmed_leaving_the_real_clause():
    hit = g().locate("The Buyer shall pay £10,000 within thirty (30) days of invoice "
                     "and interest at 8% per annum")
    assert hit in SRC and "interest" not in hit


# --- refusing a fabrication -------------------------------------------------------------

def test_a_wholly_fabricated_clause_is_dropped():
    assert g().locate("The Supplier shall indemnify the Buyer against all losses") is None


def test_a_fabrication_is_not_laundered_into_a_generic_fragment():
    """Without a coverage floor this would be salvaged down to "The Supplier shall" - three
    words that occur in nearly every contract and are evidence of nothing."""
    hit = g().locate("The Supplier shall indemnify the Buyer against all losses howsoever "
                     "arising under this Agreement")
    assert hit is None


def test_a_paraphrase_is_dropped():
    assert g().locate("The seller must ship the products to the purchaser's depot") is None


def test_a_quote_from_a_different_contract_is_dropped():
    assert g().locate("The Tenant shall keep the Premises in good repair") is None


def test_too_short_a_quote_is_dropped():
    assert g().locate("shall") is None
    assert g().locate("the") is None


def test_an_empty_quote_is_dropped():
    assert g().locate("") is None and g().locate(None) is None


# --- clause label verification ----------------------------------------------------------

def test_a_real_clause_label_is_recognised():
    src = g()
    assert src.has_clause_label("Clause 4.1")
    assert src.has_clause_label("4.1")
    assert src.has_clause_label("Clause 5.2")


def test_an_invented_clause_label_is_rejected():
    src = g()
    assert not src.has_clause_label("Clause 9.9")
    assert not src.has_clause_label("12.4")
    assert not src.has_clause_label("")


# --- the citation list ------------------------------------------------------------------

def test_every_clause_returned_is_a_verbatim_substring_of_the_source():
    quotes = [
        "The Supplier shall deliver the Goods",
        "The Buyer shall pay £10,000 within thirty (30) days of invoice",
        "The Supplier shall indemnify the Buyer against all losses",   # fabricated
        "in accordance with the delivery timetable",
    ]
    clauses, dropped = ground_quotes(g(), quotes, max_items=12, max_chars=600)
    assert dropped == 1, "the fabricated quote must be counted out"
    assert clauses, "the real quotes must survive"
    for c in clauses:
        assert c in SRC, f"not verbatim: {c!r}"


def test_duplicates_and_contained_quotes_are_collapsed():
    quotes = ["The Supplier shall deliver the Goods",
              "The Supplier shall deliver the Goods",
              "The Supplier shall deliver the Goods to the Buyer's warehouse"]
    clauses, _ = ground_quotes(g(), quotes, max_items=12, max_chars=600)
    assert len(clauses) == 1
    assert "warehouse" in clauses[0], "the longest citation should win"
    assert clauses[0] in SRC


def test_the_citation_list_is_capped():
    quotes = ["The Supplier shall deliver the Goods",
              "The Buyer shall pay £10,000 within thirty (30) days of invoice",
              "The Supplier shall maintain insurance of £2,000,000"]
    clauses, _ = ground_quotes(g(), quotes, max_items=2, max_chars=600)
    assert len(clauses) == 2


def test_a_long_citation_is_trimmed_and_stays_verbatim():
    long_src = "1.1 " + ". ".join(f"The Supplier shall do thing number {i}" for i in range(40))
    src = GroundedSource.build(long_src)
    clauses, _ = ground_quotes(src, [long_src], max_items=4, max_chars=200)
    assert clauses and len(clauses[0]) <= 200
    assert clauses[0] in long_src, "trimming must not break the verbatim guarantee"


def test_all_fabrications_yields_an_empty_list_rather_than_a_guess():
    clauses, dropped = ground_quotes(
        g(), ["The Supplier shall indemnify the Buyer against every conceivable loss",
              "The Tenant shall keep the Premises in good repair at all times"],
        max_items=12, max_chars=600)
    assert clauses == [] and dropped == 2


def test_the_slice_invariant_holds_for_random_inputs():
    """A property test for the one rule that must never break.

    Whatever `locate` returns, for any quote against any source, it is a contiguous slice of
    that source. This is checked over random punctuation-heavy text because that is where a
    normalisation or offset-mapping bug would hide: the offset table is what maps a match in
    the normalised view back to real coordinates, and an off-by-one there would produce text
    that never appeared in the document.
    """
    import random

    alphabet = "abc def.,;'\"\n-’“ ­"
    rnd = random.Random(1234)
    for _ in range(2000):
        src = "".join(rnd.choice(alphabet) for _ in range(300))
        quote = "".join(rnd.choice(alphabet) for _ in range(40))
        hit = GroundedSource.build(src).locate(quote)
        assert hit is None or hit in src, f"not a slice: {hit!r} of {src!r}"


def test_offsets_round_trip_on_text_full_of_normalisation_traps():
    traps = ("A B\tC   D\n\nE-\nF­G​H’I“J”K—L…M "
             "N\r\nO　P")
    src = GroundedSource.build(traps)
    for length in range(12, len(src.norm) + 1):
        for start in range(0, len(src.norm) - length + 1, 3):
            needle = src.norm[start:start + length]
            hit = src.locate(needle)
            assert hit is None or hit in traps, f"not a slice: {hit!r}"


def test_grounding_never_invents_text_for_an_empty_source():
    empty = GroundedSource.build("")
    assert empty.locate("The Supplier shall deliver the Goods") is None
    assert ground_quotes(empty, ["anything at all here"], max_items=4, max_chars=600) == ([], 1)
