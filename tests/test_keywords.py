# -*- coding: utf-8 -*-
"""Keyword-matching precision suite (exact whole-word / phrase only).

Run:  python -m tests.test_keywords
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


# --- Must not substring-match ------------------------------------------------
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

# --- Exact whole word only (no inflection, no fuzzy) -------------------------
check("rape == rape", has("a rape case was reported", "rape"), True)
check("rape !~ raped", has("the victim was raped", "rape"), False)
check("rape !~ rapes", has("reports of rapes rose", "rape"), False)
check("rape + punct", has("the charge was rape.", "rape"), True)

check("war !~ ward", has("the ward was full", "war"), False)
check("war !~ toward", has("moving toward peace", "war"), False)
check("war !~ warning", has("a stern warning", "war"), False)
check("war == war", has("the war continues", "war"), True)

check("Modi !~ modify", has("we must modify the plan", "Modi"), False)
check("Modi == Modi", has("PM Modi spoke", "Modi"), True)
check("coal !~ coalition", has("the coalition government", "coal"), False)
check("protest !~ protestant", has("a protestant church", "protest"), False)
check("protest !~ protesting", has("crowds protesting downtown", "protest"), False)
check("protest == protest", has("a protest downtown", "protest"), True)

check("Pakistan !~ Pakistani", has("a Pakistani official said", "Pakistan"), False)
check("Pakistan == Pakistan", has("Pakistan today", "Pakistan"), True)
check("BLA == BLA", has("BLA claimed responsibility", "BLA"), True)
check("BLA !~ black", has("a black car", "BLA"), False)
check("aleema khan ==", has("Senator Aleema Khan spoke today", "aleema khan"), True)
check("aleema khan !~ partial", has("Aleema spoke today", "aleema khan"), False)
check("budget scaling", [fuzzy_budget(k) for k in ("war", "rape", "biden", "pakistan")],
      [0, 0, 0, 0])

# --- Multi-word ---------------------------------------------------------------
check("phrase match", has("the foreign affairs ministry", "foreign affairs"), True)
check("phrase !~ prefix", has("foreigners arrived", "foreign affairs"), False)

# --- Urdu: code-point + harakat unification (still exact after normalize) ----
check("ur codepoint kaf", has("حکومت پاكستان نے کہا", "پاکستان", "ur"), True)
check("ur harakat", has("پاکِسْتان کا اعلان", "پاکستان", "ur"), True)
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
