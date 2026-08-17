# -*- coding: utf-8 -*-
"""The OCR quality gate — the thing standing between us and invented news.

Vision models under greedy decoding fall into repetition loops on dense
newsprint. Measured on real pages: Groq's Qwen read Jang's masthead, invented a
date ("پیر 10 جون 2024" for a 17 Aug 2026 edition) and repeated one line 533
times. The output was 21k characters and looked substantial. Any keyword that
appeared in the looping line would have been reported as a genuine print
mention — a fabricated result, which is far worse than finding nothing.

So: degenerate text must never reach the matcher.
"""
from __future__ import annotations

from app.epaper.reader import looks_degenerate


def _run(name, fn):
    try:
        fn()
    except AssertionError as exc:
        print(f"FAIL  {name}: {exc}")
        return 1
    print(f"pass  {name}")
    return 0


def test_line_repetition_loop_is_rejected():
    """The exact failure seen on Jang: one line, hundreds of times."""
    text = "\n".join(["جنگ", "JANG"] + ["پیر 10 جون 2024"] * 533)
    assert looks_degenerate(text), "a 533x repeated line must be rejected"


def test_partial_loop_is_rejected():
    """Dawn's front page: a real first article, then a loop. Still unusable —
    most of the page is missing and the tail is fiction."""
    real = [f"Genuine headline number {i}" for i in range(40)]
    loop = ["demands had been made — a commitment to resolve the remaining two"] * 300
    assert looks_degenerate("\n".join(real + loop))


def test_loop_without_newlines_is_rejected():
    """Some loops never emit a line break, so line-counting alone misses them."""
    assert looks_degenerate("The transporters ended their strike. " * 200)


def test_empty_is_rejected():
    assert looks_degenerate("")
    assert looks_degenerate("   \n  ")


def test_a_real_page_passes():
    """A genuine transcription must survive — the gate has to be safe, not just
    strict, or every page would be thrown away."""
    page = "\n".join([
        "FOUNDED BY QUAID-I-AZAM MOHAMMAD ALI JINNAH",
        "DAWN", "Monday", "August 17, 2026", "KARACHI", "Rs 45.00",
        "Transporters end 9-day strike 'in national interest'",
        "KARACHI: After observing a nine-day strike, the transporters agreed to",
        "resume operations following a marathon meeting hosted by the governor.",
        "Certain matters were addressed immediately while others requiring a",
        "cabinet nod will be taken up in due course, officials said.",
        "The centre sought 15 days to review the demand to withdraw daily fuel",
        "price revisions, a representative confirmed on Sunday evening.",
        "SEPARATE STORY: Rain lashes upper Sindh, disrupting road traffic.",
        "Photo caption: Commuters wade through a flooded underpass on Sunday.",
    ])
    assert not looks_degenerate(page), "a normal page must not be rejected"


def test_repeated_boilerplate_does_not_trip_the_gate():
    """Real pages legitimately repeat short strings (bylines, 'Continued on
    Page 5'). A handful of duplicates is not a loop."""
    body = [f"Distinct sentence {i} carrying actual article content." for i in range(30)]
    body += ["Continued on Page 5"] * 3 + ["Staff Reporter"] * 4
    assert not looks_degenerate("\n".join(body))


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fails += _run(name, fn)
    print(f"\n{'ALL PASS' if not fails else 'FAILURES'} ({fails} failed)")
    raise SystemExit(1 if fails else 0)
