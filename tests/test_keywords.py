# -*- coding: utf-8 -*-
"""Keyword-matching precision suite.

Run:  python -m tests.test_keywords          (no test framework needed)

These cases encode the matcher's contract — including regressions that shipped
once and must never return:
- "rape" matched rate/rare/race/rope/grape/scrape and polluted every scan
  (fuzzy budget now scales with keyword length; matching is word-boundary).
- Urdu code-point variants (Arabic kaf/yeh vs Urdu keheh/yeh) and harakat must
  unify so Whisper/vision output matches user-typed keywords.
"""
from __future__ import annotations

from app.core.keywords import find_matches, fuzzy_budget, normalize

FAILURES: list[str] = []


def check(name: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{name}: got={got!r} want={want!r}")
        print(f"FAIL  {name}: got={got!r} want={want!r}")
    else:
        print(f"pass  {name}")


def has(text: str, kw: str, lang: str = "en") -> bool:
    return bool(find_matches(text, [(kw, lang)]))


# --- Short keywords must not fuzzy-drift or substring-match -----------------
for word, body in {
    "grape": "the grape harvest was good",
    "scrape": "he got a scrape on his knee",
    "draped": "the flag was draped over it",
    "rate": "the interest rate rose today",
    "rare": "a rare event occurred",
    "race": "the race began at noon",
    "rope": "they used a rope",
    "ripe": "the fruit is ripe",
    "trade": "bilateral trade ties",
    "shape": "in good shape",
    "rage": "road rage incident",
    "range": "a wide range of options",
}.items():
    check(f"rape !~ {word}", has(body, "rape"), False)

# --- Genuine mentions + light inflection still match ------------------------
check("rape == rape", has("a rape case was reported", "rape"), True)
check("rape ~ raped", has("the victim was raped", "rape"), True)
check("rape ~ rapes", has("reports of rapes rose", "rape"), True)
check("rape + punct", has("the charge was rape.", "rape"), True)

# --- <=3-char keywords: whole word only --------------------------------------
check("war !~ ward", has("the ward was full", "war"), False)
check("war !~ toward", has("moving toward peace", "war"), False)
check("war !~ warning", has("a stern warning", "war"), False)
check("war == war", has("the war continues", "war"), True)

# --- Word boundaries beat substrings -----------------------------------------
check("Modi !~ modify", has("we must modify the plan", "Modi"), False)
check("Modi == Modi", has("PM Modi spoke", "Modi"), True)
check("coal !~ coalition", has("the coalition government", "coal"), False)
check("protest !~ protestant", has("a protestant church", "protest"), False)
check("protest ~ protesting", has("crowds protesting downtown", "protest"), True)

# --- Longer keywords keep useful fuzz ----------------------------------------
check("Pakistan ~ Pakistani", has("a Pakistani official said", "Pakistan"), True)
check("Pakistan == Pakistan", has("Pakistan today", "Pakistan"), True)
check("budget scaling", [fuzzy_budget(k) for k in ("war", "rape", "biden", "pakistan")],
      [0, 0, 1, 2])

# --- Multi-word ---------------------------------------------------------------
check("phrase match", has("the foreign affairs ministry", "foreign affairs"), True)
check("phrase !~ prefix", has("foreigners arrived", "foreign affairs"), False)

# --- Urdu: code-point + harakat unification ----------------------------------
check("ur codepoint kaf", has("حکومت پاكستان نے کہا", "پاکستان", "ur"), True)   # Arabic kaf in text
check("ur harakat", has("پاکِسْتان کا اعلان", "پاکستان", "ur"), True)             # diacritics in text
check("ur multiword", has("ایران کے رہنما آیت اللہ خامنہ ای نے", "خامنہ ای", "ur"), True)
check("ur absent", has("یہ ایک عام جملہ ہے", "خامنہ ای", "ur"), False)
check("ur normalize", normalize("پاكستان", "ur"), normalize("پاکستان", "ur"))

# --- Empty / degenerate --------------------------------------------------------
check("empty keyword", find_matches("anything", [("", "en")]), [])
check("spaces keyword", find_matches("anything", [("   ", "en")]), [])

print(f"\n{'ALL PASS' if not FAILURES else f'{len(FAILURES)} FAILURE(S)'} "
      f"({len(FAILURES)} failed)")
if FAILURES:
    raise SystemExit(1)
