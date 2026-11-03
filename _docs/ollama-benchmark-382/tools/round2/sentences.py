"""Abbreviation-aware sentence counter (fix of the 'Dr.' artifact in mech.py's TLDR sentence count).

count_sentences(text) counts boundaries at . ! ? (plus closing quotes/brackets) followed by whitespace and a capital letter,
digit, quote or bracket, except when the period ends an abbreviation (Dr., Mr., Mrs., Ms., Prof., St., vs., etc.,
e.g., i.e., No., Inc., Jr., Sr.), a single-letter initial ('J. Smith') or a decimal number (the digit case needs no
space, so decimals never match). Self-check: python3 sentences.py
"""
import re

_cand = re.compile(r"([.!?])([\"')\]]*)\s+(?=[A-Z0-9\"'(\[])")
_abbr = {"dr", "mr", "mrs", "ms", "prof", "st", "vs", "e.g", "i.e", "no", "inc", "jr", "sr", "mt", "fig", "approx", "dept", "co"}


def count_sentences(text: str) -> int:
    text = text.strip()
    if not text:
        return 0
    n = 1
    for m in _cand.finditer(text):
        if m.group(1) == ".":
            w = re.search(r"([A-Za-z.]+)$", text[: m.start()])
            word = (w.group(1) if w else "").lower().rstrip(".")
            before = text[: m.start()]
            initial = len(word) == 1 and word.isalpha() and (len(before) < 2 or before[-2] in " (")
            if word in _abbr or initial:
                continue
        n += 1
    return n


if __name__ == "__main__":
    cases = [
        ("Dr. Eric Berg explains PSA. It is not a cancer marker. Screening gains days.", 3),
        ("Mr. J. Smith saw 3.5 percent. He left, i.e. he quit. Yes!", 3),
        ("One. Two? Three! Four.", 4),
        ("Dr. Berg says vs. the others etc. They agree.", 2),
        ("It cost $4.50 per share. Prof. Lee agreed.", 2),
    ]
    for t, want in cases:
        got = count_sentences(t)
        assert got == want, (t, got, want)
    print("sentence counter self-check ok")
