"""Yol B orchestrator fix sprint — regression tests.

Covers the orchestrator-group findings:
  RT-01  short aorist suffixes no longer flag English words as Turkish
  RT-02  Title-Case English headings aren't misread as author lines
  RT-03  fully-English headings are detected in the no-marker branch too
  RT-04  numbered English headings ("2.4 The Laplace Transform") are detected
  RT-05  Phase 5.8 positional fallback removed + acceptance gate added
  RT-06  retranslator preserves the heading command shell (\\section{...})
  RT-07  heading sentence-case fixes are adopted even at 0 retranslations
  RT-09  dead code removed (skipped_bibliography, _TURKISH_CHARS)
  RT-11  --batch + --import-tex is a hard error
  HEAD-11 sanitizer " to " rule gated on Turkish line context
  QC      _qc_analyze forwards manual_translation with TypeError fallback

All tests are Yol-B focused; where Yol-A (manual_translation) behaviour is
relevant it is asserted to remain unchanged.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from latex_pipeline import retranslator as RT
from latex_pipeline.retranslator import (
    is_english_line,
    _has_turkish_suffix,
    _retranslate_line,
    scan_and_retranslate,
    RetranslateResult,
)
from latex_pipeline.sanitizer import LatexSanitizer

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# RT-01 — short aorist suffixes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("word", ["user", "browser", "occur", "officer", "laser"])
def test_rt01_english_words_not_turkish_suffix(word):
    assert _has_turkish_suffix(word) is False


def test_rt01_english_sentences_detected():
    assert is_english_line(
        "The user opens the browser and selects the correct option from the menu."
    ) is True
    assert is_english_line(
        "This error may occur when the officer submits the form to the server."
    ) is True


def test_rt01_turkish_still_detected_as_turkish():
    # Turkish sentences must NOT be flagged as English.
    assert is_english_line(
        "Bu denklem basınç ve hız arasındaki ilişkiyi gösterir ve önemlidir."
    ) is False


# ---------------------------------------------------------------------------
# RT-02 — Title-Case English headings aren't author lines
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title", [
    r"\section{Rocket Propulsion Fundamentals}",
    r"\section{Neural Network Training Strategies}",
    r"\chapter{Deep Learning Fundamentals}",
])
def test_rt02_titlecase_heading_detected(title):
    assert is_english_line(title, is_heading=True) is True


def test_rt02_author_line_still_skipped_in_body():
    # A real author/affiliation body line must still be skipped (is_heading=False).
    line = "Rose Field Marco Velli Samir K. Brook"
    assert is_english_line(line, is_heading=False) is False


# ---------------------------------------------------------------------------
# RT-03 — fully-English headings in the no-marker branch
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title", [
    r"\chapter{DEEP LEARNING}",
    r"\section{Attention and Transformer Networks}",
    r"\section{Introduction to Machine Learning}",
])
def test_rt03_fully_english_heading_detected(title):
    assert is_english_line(title, is_heading=True) is True


@pytest.mark.parametrize("title", [
    r"\section{Yörünge Mekaniğine Giriş}",
    r"\section{Fourier Dönüşümü}",
    r"\section{Transformer Mimarisi}",      # single English loanword -> keep
])
def test_rt03_turkish_heading_not_flagged(title):
    assert is_english_line(title, is_heading=True) is False


def test_rt03_scan_reaches_english_headings():
    # Regression lock: the scan-loop length pre-filter must use the
    # heading-aware cleaner, otherwise \chapter{DEEP LEARNING} is dropped as
    # "too short" before is_english_line ever runs.
    doc = (
        r"\chapter{DEEP LEARNING}" "\n\n"
        "Bu bölüm derin öğrenmeyi Türkçe olarak anlatır ve yeterince uzundur burada.\n\n"
        r"\section{Attention and Transformer Networks}" "\n"
    )
    res = scan_and_retranslate(doc, translator_fn=lambda t: "Türkçe çeviri metni yeterince uzun.")
    # Both English headings should be counted as English (not skipped_short).
    assert res.english_lines_found >= 2


def test_rt03_scan_leaves_turkish_heading_alone():
    doc = (
        r"\section{Yörünge Mekaniğine Giriş}" "\n\n"
        "Türkçe paragraf yeterince uzun ve doğru şekilde çevrilmiştir burada.\n"
    )
    res = scan_and_retranslate(doc, translator_fn=lambda t: "XXX")
    assert res.english_lines_found == 0


# ---------------------------------------------------------------------------
# RT-04 — numbered English headings
# ---------------------------------------------------------------------------

def test_rt04_numbered_heading_detected():
    assert is_english_line(r"\section{2.4 The Laplace Transform}", is_heading=True) is True
    assert is_english_line(r"\section{10.2.1 Introduction to Deep Networks}", is_heading=True) is True
    # Unnumbered variant already worked -> stays True (no regression).
    assert is_english_line(r"\section{The Laplace Transform}", is_heading=True) is True


# ---------------------------------------------------------------------------
# RT-06 — heading command shell preserved through retranslation
# ---------------------------------------------------------------------------

def test_rt06_heading_command_preserved():
    def eat_command(_text):
        # Simulate MT dropping the LaTeX command entirely.
        return "Roket lülelerinde ısı transfer süreci"

    out = _retranslate_line(
        r"\section{The Heat Transfer Process in Rocket Nozzles}",
        eat_command,
    )
    assert out.lstrip().startswith(r"\section{")
    assert out.rstrip().endswith("}")
    assert r"\section{" in out


def test_rt06_body_line_unaffected():
    # Non-heading line must still go through the normal path.
    def tr(_t):
        return "Bu bir Türkçe cümledir ve yeterince uzundur burada."
    out = _retranslate_line(
        "This is an English sentence that is long enough to translate here.",
        tr,
    )
    assert "çe" in out or "Türk" in out  # got the Turkish output


# ---------------------------------------------------------------------------
# RT-07 — heading-case fixes adopted at 0 retranslations
# ---------------------------------------------------------------------------

def test_rt07_heading_case_fix_flag_populated():
    body = (
        r"\section{yörünge mekaniğine giriş}" + "\n\n"
        "Bu bir Türkçe paragraftır ve içinde yeterince Türkçe kelime bulunur "
        "ve tamamen çevrilmiştir.\n"
    )
    result = scan_and_retranslate(body, translator_fn=lambda t: t)
    assert result.retranslated_lines == 0
    assert result.heading_case_fixes >= 1
    assert result.output_tex != body
    assert "Yörünge" in result.output_tex


# ---------------------------------------------------------------------------
# RT-09 — dead code removed
# ---------------------------------------------------------------------------

def test_rt09_dead_symbols_removed():
    assert not hasattr(RetranslateResult(), "skipped_bibliography")
    assert hasattr(RetranslateResult(), "heading_case_fixes")
    assert not hasattr(RT, "_TURKISH_CHARS")


# ---------------------------------------------------------------------------
# HEAD-11 — sanitizer " to " rule gated on Turkish context
# ---------------------------------------------------------------------------

def test_head11_english_line_not_corrupted():
    s = LatexSanitizer()
    english = (
        "An Introduction to Statistical Learning\n"
        "Introduction to Probability Models\n"
        "What You Need to Know to Make It\n"
    )
    body, n = s._fix_to_leak(english)
    assert body == english
    assert n == 0
    assert "'den" not in body


def test_head11_turkish_context_still_converted():
    s = LatexSanitizer()
    body, n = s._fix_to_leak("Basınç oranı Pe to Pa olarak hesaplanır")
    assert n == 1
    assert "Pe'den Pa" in body


def test_head11_full_sanitize_preserves_english_bibliography():
    s = LatexSanitizer()
    body = "An Introduction to Statistical Learning by James et al.\n"
    out, _report = s.sanitize(body)
    assert "'den" not in out


# ---------------------------------------------------------------------------
# RT-05 — positional fallback removed + acceptance gate present (source lock)
# ---------------------------------------------------------------------------

def test_rt05_no_positional_fallback_in_source():
    src = (PROJECT_ROOT / "translate_book.py").read_text(encoding="utf-8")
    # The blind positional fallback must be gone.
    assert "clamped = min(tr_idx, len(en_lines) - 1)" not in src
    # The Phase 5.8 acceptance gate must be present. K5 (2026-09-28): kapı
    # kaynak dile göre çalışır (is_untranslated_line; İngilizcede
    # is_english_line'ın AYNISI — retranslator'daki tek karar noktası).
    assert "not is_untranslated_line(" in src
    assert "new_tr, config.source_lang)" in src


# ---------------------------------------------------------------------------
# RT-11 — --batch + --import-tex mutually exclusive
# ---------------------------------------------------------------------------

def test_rt11_batch_and_import_tex_rejected(tmp_path):
    imp = tmp_path / "import.tex"
    imp.write_text("x" * 300, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "translate_book.py", "dummy.epub",
         "--batch", "--import-tex", str(imp)],
        cwd=str(PROJECT_ROOT),
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 1
    combined = (proc.stdout + proc.stderr).lower()
    assert "batch" in combined and "import-tex" in combined


# ---------------------------------------------------------------------------
# QC — _qc_analyze forwards manual_translation with TypeError fallback
# ---------------------------------------------------------------------------

def test_qc_analyze_forwards_manual_translation():
    import translate_book as TB

    captured = {}

    class NewQC:
        def analyze(self, tb, sb="", manual_translation=False):
            captured["manual"] = manual_translation
            return "ok"

    assert TB._qc_analyze(NewQC(), "body", "src", manual_translation=True) == "ok"
    assert captured["manual"] is True


def test_qc_analyze_no_silent_legacy_fallback():
    """2026-07-17 (#12): TypeError fallback'i KALDIRILDI.

    Eski davranis, analyze ICINDEKI gercek bir TypeError'i imza uyusmazligi
    sanip cagriyi manual_translation OLMADAN tekrarliyordu — Yol A kitabi
    sessizce Yol B modunda QC'leniyor, asil hata yutuluyordu. Artik imza
    kalici (qc_engine.analyze manual_translation destekliyor); legacy imza
    sessiz mod dususu yerine YUKSEK SESLE patlamali.
    """
    import pytest

    import translate_book as TB

    class LegacyQC:
        def analyze(self, tb, sb=""):
            return "legacy"

    with pytest.raises(TypeError):
        TB._qc_analyze(LegacyQC(), "body", "src", manual_translation=True)
