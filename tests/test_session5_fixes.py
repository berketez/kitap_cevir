"""Tests for Session 5 fixes (2026-03-03):
- Aggressive prompt leak patterns (lecture-notes book 17/100 fix)
- Env name protection in retranslator (textbook 77/100 fix)
- Post-retranslation prompt leak guard
- Segment retry env name protection

Tests cover:
1. New prompt leak patterns in sanitizer, autofix, and qc_engine
2. Retranslator env name stash/restore
3. Post-retranslation prompt leak cleaning
4. Residual line cleanup after prompt leak removal
"""

import re
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ======================================================================
# 1. New Prompt Leak Pattern Tests
# ======================================================================

# New patterns added in session 5 that MUST be matched
SESSION5_SHOULD_MATCH = [
    # HY-MT 7B actual system prompt echoes
    "Translate from English to Turkish.",
    "Translate from English to Turkish. Preserve math notation.",
    "Output only the translation.",
    "Preserve math notation.",
    "Preserve the mathematical notation and formulas.",
    "Preserve mathematical formulas intact.",
    # Turkish echoes of system prompt
    "Sadece ceviriyi cikti olarak verin.",
    "Sadece ceviriyi ver.",
    "Matematiksel notasyonlari koruyun.",
    "Matematik notasyonu degistirmeyin.",
    "Baglami korumalisiniz.",
    "Baglami koru.",
    "Sadece cevrilmis metni dondur.",
    "Sadece ceviriyi dondur.",
    "Aciklama yapma, sadece cevir.",
    "Yorum ekleme.",
    "Ceviri notu: Bu metin...",
    "Cevirmen notu: Dikkat...",
    "\u00c7eviri notu: Bu metin...",  # with c-cedilla
    "\u00c7evirmen notu: Dikkat...",  # with c-cedilla
    "Please translate this section.",
    "Here is the translation:",
    "Here is the translated text:",
    "Ingilizce'den Turkce'ye ceviri yapilmistir.",
]

# These must NOT be matched (false positives for new patterns)
SESSION5_SHOULD_NOT_MATCH = [
    "Bu denklem matematiksel bir ifadedir.",  # "matematik" in context of math content
    r"\begin{equation*}",
    r"\end{figure}",
    "$E = mc^2$",
    "Sinir aglari derin ogrenme alaninda kullanilir.",
    "Bu bolumde sonuclari tartisacagiz.",
    "Sekil 3.2'de gosterilen grafik onemlidir.",
    # "orijinal metin" should match but "orijinal" alone should not
    "Bu yaklasim orijinal kalmistir.",
]


def test_sanitizer_session5_patterns():
    """Sanitizer catches new session 5 prompt leak patterns."""
    from latex_pipeline.sanitizer import _PROMPT_LEAK_PATTERNS

    passed = 0
    failed = 0
    for text in SESSION5_SHOULD_MATCH:
        matched = any(p.search(text) for p in _PROMPT_LEAK_PATTERNS)
        if not matched:
            failed += 1
            print(f"  [FAIL] Sanitizer should match: {text!r}")
        else:
            passed += 1

    for text in SESSION5_SHOULD_NOT_MATCH:
        matched = any(p.search(text) for p in _PROMPT_LEAK_PATTERNS)
        if matched:
            failed += 1
            for p in _PROMPT_LEAK_PATTERNS:
                m = p.search(text)
                if m:
                    print(f"  [FAIL] Sanitizer false positive on {text!r} by {p.pattern!r}")
                    break
        else:
            passed += 1

    print(f"Sanitizer session5: {passed} passed, {failed} failed")
    assert failed == 0, f"{failed} sanitizer session5 test(s) failed"


def test_autofix_session5_patterns():
    """AutoFix catches new session 5 prompt leak patterns."""
    from latex_pipeline.autofix import _PROMPT_LEAK_PATTERNS

    passed = 0
    failed = 0
    for text in SESSION5_SHOULD_MATCH:
        matched = any(p.search(text) for p in _PROMPT_LEAK_PATTERNS)
        if not matched:
            failed += 1
            print(f"  [FAIL] AutoFix should match: {text!r}")
        else:
            passed += 1

    for text in SESSION5_SHOULD_NOT_MATCH:
        matched = any(p.search(text) for p in _PROMPT_LEAK_PATTERNS)
        if matched:
            failed += 1
            for p in _PROMPT_LEAK_PATTERNS:
                m = p.search(text)
                if m:
                    print(f"  [FAIL] AutoFix false positive on {text!r} by {p.pattern!r}")
                    break
        else:
            passed += 1

    print(f"AutoFix session5: {passed} passed, {failed} failed")
    assert failed == 0, f"{failed} autofix session5 test(s) failed"


def test_qc_engine_session5_patterns():
    """QC Engine catches new session 5 prompt leak patterns."""
    from latex_pipeline.qc_engine import QCEngine

    engine = QCEngine()

    passed = 0
    failed = 0

    # QC engine uses both string patterns and regex patterns
    all_patterns = []
    for pattern_str in engine._prompt_patterns:
        all_patterns.append(re.compile(re.escape(pattern_str), re.IGNORECASE))
    all_patterns.extend(engine._prompt_leak_regexes)

    for text in SESSION5_SHOULD_MATCH:
        matched = any(p.search(text) for p in all_patterns)
        if not matched:
            failed += 1
            print(f"  [FAIL] QC should match: {text!r}")
        else:
            passed += 1

    for text in SESSION5_SHOULD_NOT_MATCH:
        matched = any(p.search(text) for p in all_patterns)
        if matched:
            failed += 1
            for p in all_patterns:
                m = p.search(text)
                if m:
                    print(f"  [FAIL] QC false positive on {text!r} by {p.pattern!r}")
                    break
        else:
            passed += 1

    print(f"QC Engine session5: {passed} passed, {failed} failed")
    assert failed == 0, f"{failed} QC engine session5 test(s) failed"


# ======================================================================
# 2. Retranslator Env Name Protection Tests
# ======================================================================

def test_env_stash_restore_basic():
    r"""Stash and restore \begin{figure}/\end{figure}."""
    from latex_pipeline.retranslator import _stash_env_names, _restore_env_names

    line = r"\begin{figure}[h] Some caption text \end{figure}"
    stashed, env_stash = _stash_env_names(line)

    # Env commands should be replaced with placeholders
    assert r"\begin{figure}" not in stashed
    assert r"\end{figure}" not in stashed
    assert len(env_stash) == 2

    # Restore should bring them back
    restored = _restore_env_names(stashed, env_stash)
    assert r"\begin{figure}" in restored
    assert r"\end{figure}" in restored
    print("PASS: test_env_stash_restore_basic")


def test_env_stash_multiple_envs():
    r"""Stash handles multiple different env names."""
    from latex_pipeline.retranslator import _stash_env_names, _restore_env_names

    line = r"\begin{figure}\end{figure} text \begin{table}\end{table}"
    stashed, env_stash = _stash_env_names(line)

    assert len(env_stash) == 4  # 2 begin + 2 end
    assert r"\begin{figure}" not in stashed
    assert r"\begin{table}" not in stashed

    restored = _restore_env_names(stashed, env_stash)
    assert r"\begin{figure}" in restored
    assert r"\end{figure}" in restored
    assert r"\begin{table}" in restored
    assert r"\end{table}" in restored
    print("PASS: test_env_stash_multiple_envs")


def test_env_stash_star_env():
    r"""Stash handles starred environments like \begin{figure*}."""
    from latex_pipeline.retranslator import _stash_env_names, _restore_env_names

    line = r"\begin{figure*} wide figure \end{figure*}"
    stashed, env_stash = _stash_env_names(line)

    assert r"\begin{figure*}" not in stashed
    assert r"\end{figure*}" not in stashed

    restored = _restore_env_names(stashed, env_stash)
    assert r"\begin{figure*}" in restored
    assert r"\end{figure*}" in restored
    print("PASS: test_env_stash_star_env")


def test_env_stash_no_envs():
    r"""Stash on line without env commands returns line unchanged."""
    from latex_pipeline.retranslator import _stash_env_names, _restore_env_names

    line = "This is a regular text line without any environments."
    stashed, env_stash = _stash_env_names(line)

    assert stashed == line
    assert len(env_stash) == 0
    print("PASS: test_env_stash_no_envs")


def test_env_stash_math_env():
    r"""Math environments like \begin{equation} are also stashed."""
    from latex_pipeline.retranslator import _stash_env_names, _restore_env_names

    line = r"\begin{equation} E = mc^2 \end{equation}"
    stashed, env_stash = _stash_env_names(line)

    assert r"\begin{equation}" not in stashed
    assert r"\end{equation}" not in stashed
    assert len(env_stash) == 2

    restored = _restore_env_names(stashed, env_stash)
    assert r"\begin{equation}" in restored
    assert r"\end{equation}" in restored
    print("PASS: test_env_stash_math_env")


# ======================================================================
# 3. Post-Retranslation Prompt Leak Cleaning Tests
# ======================================================================

def test_clean_prompt_leaks():
    """Prompt leak cleaning removes known patterns from text."""
    from latex_pipeline.retranslator import _clean_prompt_leaks

    # Should clean prompt leaks
    dirty = "Bu bir test cumlesidir. Translate from English to Turkish."
    cleaned = _clean_prompt_leaks(dirty)
    assert "Translate from" not in cleaned
    assert "test cumlesidir" in cleaned

    # Should not damage normal text
    normal = "Bu normal bir Turkce cumle."
    cleaned_normal = _clean_prompt_leaks(normal)
    assert cleaned_normal == normal
    print("PASS: test_clean_prompt_leaks")


def test_clean_prompt_leaks_turkish():
    """Turkish prompt leaks are cleaned (multi-line input)."""
    from latex_pipeline.retranslator import _clean_prompt_leaks

    # Prompt leak on its own line, followed by real content on next line
    dirty = "Baglami korumalisiniz.\nSonuc olarak enerji korunur."
    cleaned = _clean_prompt_leaks(dirty)
    assert "Baglami koru" not in cleaned
    # Real content on a separate line should survive
    assert "enerji korunur" in cleaned
    print("PASS: test_clean_prompt_leaks_turkish")


def test_clean_prompt_leaks_empty_result():
    """If prompt leak cleaning empties the text, return empty string."""
    from latex_pipeline.retranslator import _clean_prompt_leaks

    only_leak = "Translate from English to Turkish."
    cleaned = _clean_prompt_leaks(only_leak)
    # Should be empty or very short after cleaning
    assert len(cleaned) < 5
    print("PASS: test_clean_prompt_leaks_empty_result")


# ======================================================================
# 4. Residual Line Cleanup Tests
# ======================================================================

def test_sanitizer_residual_cleanup():
    """After prompt leak removal, residual punctuation lines are cleaned."""
    from latex_pipeline.sanitizer import LatexSanitizer

    san = LatexSanitizer()

    # Line that becomes just punctuation after prompt leak removal
    body = "Translate from English to Turkish.\nBu normal satir.\n"
    cleaned, report = san.sanitize(body)
    assert "Translate from" not in cleaned
    assert "Bu normal satir." in cleaned
    # Should not leave orphan empty lines with just punctuation
    lines = [l for l in cleaned.split("\n") if l.strip()]
    for line in lines:
        stripped = line.strip()
        if stripped:
            assert not re.match(r"^[.,;:!?\-]+$", stripped), \
                f"Orphan punctuation line found: {stripped!r}"
    print("PASS: test_sanitizer_residual_cleanup")


def test_sanitizer_preserves_math_after_prompt_leak():
    """Prompt leak removal does not damage math environments."""
    from latex_pipeline.sanitizer import LatexSanitizer

    san = LatexSanitizer()

    body = (
        "Output only the translation.\n"
        "$E = mc^2$ enerji formulu.\n"
        r"\begin{equation}" + "\n"
        r"F = ma" + "\n"
        r"\end{equation}" + "\n"
    )
    cleaned, report = san.sanitize(body)
    assert "Output only" not in cleaned
    assert "$E = mc^2$" in cleaned
    assert r"\begin{equation}" in cleaned
    assert r"\end{equation}" in cleaned
    print("PASS: test_sanitizer_preserves_math_after_prompt_leak")


# ======================================================================
# 5. Integration Test: QC Detects -> AutoFix Resolves Session 5 Patterns
# ======================================================================

def test_qc_detects_session5_prompt_leakage():
    """QC engine detects new session 5 prompt leak patterns."""
    from latex_pipeline.qc_engine import QCEngine

    engine = QCEngine()

    tr_body = (
        "Translate from English to Turkish. Preserve math notation.\n"
        "Bu bir test cumlesidir.\n"
        "Sadece ceviriyi ver.\n"
    )

    report = engine.analyze(tr_body, "")
    prompt_issues = [i for i in report.issues if i.check_name == "prompt_leakage"]
    assert len(prompt_issues) >= 2, (
        f"Expected at least 2 prompt_leakage issues, got {len(prompt_issues)}: "
        f"{[i.message for i in prompt_issues]}"
    )
    print("PASS: test_qc_detects_session5_prompt_leakage")


def test_autofix_resolves_session5_prompt_leakage():
    """AutoFix removes new session 5 prompt leak patterns."""
    from latex_pipeline.autofix import AutoFixer
    from latex_pipeline.qc_engine import QCEngine

    engine = QCEngine()
    fixer = AutoFixer(qc_engine=engine)

    tr_body = (
        "Translate from English to Turkish.\n"
        "Bu bir test cumlesidir.\n"
        "Preserve math notation.\n"
        "$E = mc^2$ onemli formullerden biridir.\n"
    )

    fixed, report = fixer.run(tr_body, source_body="")
    assert "Translate from" not in fixed
    assert "Preserve math notation" not in fixed
    assert "test cumlesidir" in fixed
    assert "$E = mc^2$" in fixed
    print("PASS: test_autofix_resolves_session5_prompt_leakage")


# ======================================================================
# 6. Integration: Env Name Fix Persists After Simulated Retranslation
# ======================================================================

def test_env_name_fix_persists():
    r"""AutoFix env name fix should persist even after retranslation guard."""
    from latex_pipeline.autofix import AutoFixer
    from latex_pipeline.qc_engine import QCEngine

    engine = QCEngine()
    fixer = AutoFixer(qc_engine=engine)

    # Simulated textbook-style problem: translated env names
    tr_body = r"""
\begin{sekil}[h]
\centering
\includegraphics{img.png}
\caption{Test sekil}
\end{sekil}

Bu bir test metnidir.

\begin{tablo}
\centering
Test tablo verisi
\end{tablo}
"""

    fixed, report = fixer.run(tr_body, source_body="")
    assert r"\begin{figure}" in fixed
    assert r"\end{figure}" in fixed
    assert r"\begin{table}" in fixed
    assert r"\end{table}" in fixed
    assert r"\begin{sekil}" not in fixed
    assert r"\begin{tablo}" not in fixed
    print("PASS: test_env_name_fix_persists")


# ======================================================================
# Main
# ======================================================================

if __name__ == "__main__":
    test_sanitizer_session5_patterns()
    test_autofix_session5_patterns()
    test_qc_engine_session5_patterns()
    test_env_stash_restore_basic()
    test_env_stash_multiple_envs()
    test_env_stash_star_env()
    test_env_stash_no_envs()
    test_env_stash_math_env()
    test_clean_prompt_leaks()
    test_clean_prompt_leaks_turkish()
    test_clean_prompt_leaks_empty_result()
    test_sanitizer_residual_cleanup()
    test_sanitizer_preserves_math_after_prompt_leak()
    test_qc_detects_session5_prompt_leakage()
    test_autofix_resolves_session5_prompt_leakage()
    test_env_name_fix_persists()
    print("\nALL SESSION 5 TESTS PASSED")
