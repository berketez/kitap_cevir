"""Tests for Turkish angle-bracket prompt leakage patterns.

Tests both autofix._PROMPT_LEAK_PATTERNS and qc_engine._prompt_leak_regexes
to ensure they catch HY-MT 7B Turkish instruction markers without false
positives on normal Turkish text.
"""

import re
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------

# These MUST be matched (true positives)
SHOULD_MATCH = [
    "<<<ÇEVİRİYE BAŞLA>>>",
    "<<<BAŞLA>>>",
    "<<<SON>>>",
    "<<<TRANSLATE>>>",
    "<<BEGIN>>",
    "<<END>>",
    "<<<ÇEVİRİ SONU>>>",
    "<<TRANSLATE THIS>>",
    "<<<BEGIN TRANSLATION>>>",
    "<<<END TRANSLATION>>>",
    "Aşağıdaki metni Türkçeye çevirin.",
    "Aşağıdaki paragrafı çevir",
    "Yukarıdaki bölümü Türkçeye çevirin lütfen",
    "Çeviriye başla",
    "Çevirinin sonu",
    "Çeviriyi bitir",
    # Session 4 additions
    "Kaynak dili koru",
    "Kaynak dilini koruyun",
    "Çeviriyi yap",
    "Keep mathematical notations unchanged",
    "keep all mathematical notations",
    "Only translate this part",
    "only translate this section please",
    "Do not translate the equations",
    "Preserve the original formatting",
    "formülleri değiştirmeden kullanın",
    "LaTeX komutlarını değiştirme",
]

# These must NOT be matched (false positives)
SHOULD_NOT_MATCH = [
    "Normal Türkçe metin bu pattern'e takılmamalı",
    "Bu bir deneme cümlesidir.",
    "Sinir ağları derin öğrenme alanında kullanılır.",
    r"\begin{equation*}",
    r"\end{equation*}",
    "Bölüm 3: Sonuçlar ve Tartışma",
    "$E = mc^2$",
    "Başlangıç koşulları tanımlanmalıdır.",  # "Başla" icermiyor, "Başlangıç" var
    "Bu denklemin sonucu şudur:",  # "son" kelimesi ama angle bracket yok
]


def test_autofix_patterns():
    """Test autofix._PROMPT_LEAK_PATTERNS against test cases."""
    from latex_pipeline.autofix import _PROMPT_LEAK_PATTERNS

    # Extract only the Turkish angle-bracket patterns (last 8 in the list)
    # We test ALL patterns to make sure nothing regressed
    print("=== AUTOFIX PATTERN TESTS ===\n")

    # True positive tests
    passed = 0
    failed = 0
    for text in SHOULD_MATCH:
        matched = any(p.search(text) for p in _PROMPT_LEAK_PATTERNS)
        status = "PASS" if matched else "FAIL"
        if not matched:
            failed += 1
            print(f"  [{status}] Should match but didn't: {text!r}")
        else:
            passed += 1
            print(f"  [{status}] {text!r}")

    print()

    # False positive tests
    for text in SHOULD_NOT_MATCH:
        matched = any(p.search(text) for p in _PROMPT_LEAK_PATTERNS)
        status = "PASS" if not matched else "FAIL"
        if matched:
            failed += 1
            # Show which pattern matched
            for p in _PROMPT_LEAK_PATTERNS:
                m = p.search(text)
                if m:
                    print(f"  [{status}] False positive on {text!r} -- matched by: {p.pattern!r} -> {m.group()!r}")
                    break
        else:
            passed += 1
            print(f"  [{status}] No false positive: {text!r}")

    print(f"\nAutofix: {passed} passed, {failed} failed out of {passed + failed}\n")
    assert failed == 0, f"{failed} autofix pattern test(s) failed"


def test_qc_engine_patterns():
    """Test qc_engine._prompt_leak_regexes against test cases."""
    from latex_pipeline.qc_engine import QCEngine

    engine = QCEngine()

    print("=== QC ENGINE REGEX PATTERN TESTS ===\n")

    passed = 0
    failed = 0
    for text in SHOULD_MATCH:
        matched = any(rx.search(text) for rx in engine._prompt_leak_regexes)
        status = "PASS" if matched else "FAIL"
        if not matched:
            failed += 1
            print(f"  [{status}] Should match but didn't: {text!r}")
        else:
            passed += 1
            print(f"  [{status}] {text!r}")

    print()

    for text in SHOULD_NOT_MATCH:
        matched = any(rx.search(text) for rx in engine._prompt_leak_regexes)
        status = "PASS" if not matched else "FAIL"
        if matched:
            failed += 1
            for rx in engine._prompt_leak_regexes:
                m = rx.search(text)
                if m:
                    print(f"  [{status}] False positive on {text!r} -- matched by: {rx.pattern!r} -> {m.group()!r}")
                    break
        else:
            passed += 1
            print(f"  [{status}] No false positive: {text!r}")

    print(f"\nQC Engine: {passed} passed, {failed} failed out of {passed + failed}\n")
    assert failed == 0, f"{failed} QC engine pattern test(s) failed"


def test_sanitizer_prompt_leak_patterns():
    """Test sanitizer._PROMPT_LEAK_PATTERNS against test cases."""
    from latex_pipeline.sanitizer import _PROMPT_LEAK_PATTERNS

    print("=== SANITIZER PATTERN TESTS ===\n")

    passed = 0
    failed = 0
    for text in SHOULD_MATCH:
        matched = any(p.search(text) for p in _PROMPT_LEAK_PATTERNS)
        status = "PASS" if matched else "FAIL"
        if not matched:
            failed += 1
            print(f"  [{status}] Should match but didn't: {text!r}")
        else:
            passed += 1
            print(f"  [{status}] {text!r}")

    print()

    for text in SHOULD_NOT_MATCH:
        matched = any(p.search(text) for p in _PROMPT_LEAK_PATTERNS)
        status = "PASS" if not matched else "FAIL"
        if matched:
            failed += 1
            for p in _PROMPT_LEAK_PATTERNS:
                m = p.search(text)
                if m:
                    print(f"  [{status}] False positive on {text!r} -- matched by: {p.pattern!r} -> {m.group()!r}")
                    break
        else:
            passed += 1
            print(f"  [{status}] No false positive: {text!r}")

    print(f"\nSanitizer: {passed} passed, {failed} failed out of {passed + failed}\n")
    assert failed == 0, f"{failed} sanitizer pattern test(s) failed"


def test_sanitizer_line_level_cleaning():
    """Test that sanitizer removes full lines when line is only a prompt,
    but removes only the pattern when it appears mid-line."""
    from latex_pipeline.sanitizer import LatexSanitizer

    san = LatexSanitizer()

    # Full line = prompt -> entire line removed
    body_full = "Matematiksel gösterimleri koru\nBu normal satir.\n"
    cleaned, report = san.sanitize(body_full)
    assert "Matematiksel" not in cleaned
    assert "Bu normal satir." in cleaned

    # Mid-line prompt -> only pattern removed, rest preserved
    body_mid = "Sonuc olarak keep mathematical notations seklinde devam eder.\n"
    cleaned, report = san.sanitize(body_mid)
    assert "keep mathematical notations" not in cleaned
    assert "Sonuc olarak" in cleaned


if __name__ == "__main__":
    test_autofix_patterns()
    test_qc_engine_patterns()
    test_sanitizer_prompt_leak_patterns()
    test_sanitizer_line_level_cleaning()
    print("ALL TESTS PASSED")
