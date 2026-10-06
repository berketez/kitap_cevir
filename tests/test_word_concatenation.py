"""Tests for word concatenation fixes in autofix.py.

Tests the three patterns:
  A) lowerUpper boundary: "fonksiyonlariHer" -> "fonksiyonlari Her"
  B) paren/bracket + upper: ").Boyle" -> "). Boyle"
  C) letter+punct + upper: "sonuctur.Bu" -> "sonuctur. Bu"
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from latex_pipeline.autofix import AutoFixer
from latex_pipeline.qc_engine import QCEngine


def _run_fix(body: str) -> str:
    """Run word_concatenation fix on body and return fixed text."""
    fixer = AutoFixer.__new__(AutoFixer)
    # Initialize just the patterns we need
    fixer.__class__ = AutoFixer
    import re
    from latex_pipeline.autofix import _find_math_spans
    math_spans = _find_math_spans(body)
    fixed, fixes = fixer._fix_word_concatenation(body, [], math_spans)
    return fixed


# ------------------------------------------------------------------
# Pattern A: lowerUpper boundary
# ------------------------------------------------------------------

def test_lower_upper_boundary_basic():
    """Basic lowerUpper boundary: insert space."""
    result = _run_fix("fonksiyonlarıHer zaman")
    assert "fonksiyonları Her" in result


def test_lower_upper_boundary_turkish_chars():
    """Turkish special chars: oOoO boundary."""
    result = _run_fix("değerlerÖzellikle önemlidir")
    assert "değerler Özellikle" in result


def test_lower_upper_camelcase_whitelist():
    """CamelCase whitelist: LaTeX should NOT be split."""
    result = _run_fix("Bu LaTeX komutudur")
    assert "La TeX" not in result
    assert "LaTeX" in result


def test_lower_upper_in_math_skip():
    """Inside inline math $...$ -> no splitting."""
    result = _run_fix("$xY$ normal metin")
    assert "$xY$" in result


# ------------------------------------------------------------------
# Pattern B: paren/bracket + uppercase
# ------------------------------------------------------------------

def test_paren_upper_basic():
    """)B -> ) B"""
    result = _run_fix("(sonuc).Boyle bir")
    assert "). Boyle" in result or "). B" in result


def test_bracket_upper():
    """]T -> ] T"""
    result = _run_fix("[12]Teleskop")
    assert "] Teleskop" in result or "]T" not in result


def test_period_upper_after_paren():
    """).Bu -> ). Bu"""
    result = _run_fix("hesaplanir).Bu durumda")
    assert "). Bu" in result


# ------------------------------------------------------------------
# Pattern C: letter+punct + uppercase (sentence boundary)
# ------------------------------------------------------------------

def test_punct_upper_period():
    """Period + upper: sonuctur.Bu -> sonuctur. Bu"""
    result = _run_fix("sonuctur.Bu nedenle")
    assert "sonuctur. Bu" in result


def test_punct_upper_comma():
    """Comma + upper: olmaktadir,Ayrica -> olmaktadir, Ayrica"""
    result = _run_fix("olmaktadir,Ayrica baska")
    # Comma + uppercase -> space inserted
    assert ", Ayrica" in result or ",Ayrica" not in result


def test_punct_upper_skip_in_latex_cmd():
    r"""LaTeX command like \textbf{Sonuc} should not be affected."""
    result = _run_fix(r"\textbf{Sonuc}")
    # Should remain unchanged
    assert r"\textbf{Sonuc}" in result


def test_no_false_positive_decimal():
    """Decimal number like 3.14 should not get a space."""
    result = _run_fix("yaklaşık 3.14 değerinde")
    # Pattern C requires letter before punct, so 3.14 (digit before .) won't match
    assert "3.14" in result


def test_no_false_positive_abbreviation():
    """Normal sentence should not be affected."""
    result = _run_fix("Bu bir cümledir.")
    assert "Bu bir cümledir." in result


# ------------------------------------------------------------------
# Combined patterns
# ------------------------------------------------------------------

def test_combined_all_three():
    """A line with all three pattern types."""
    body = "teleskoplarHer).Böyle bir sonuçtur.Bu"
    result = _run_fix(body)
    # Pattern A: teleskoplarHer -> teleskoplar Her
    assert "teleskoplar Her" in result
    # Pattern B: ).Böyle -> ). Böyle
    assert "). Böyle" in result or "). B" in result
    # Pattern C: sonuçtur.Bu -> sonuçtur. Bu
    assert "sonuçtur. Bu" in result


def test_empty_line():
    """Empty string should return empty."""
    result = _run_fix("")
    assert result == ""


def test_comment_line_skipped():
    """Comment lines should not be modified."""
    result = _run_fix("% fonksiyonlarıHer zaman")
    assert "fonksiyonlarıHer" in result  # unchanged
