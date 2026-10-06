"""Tests for semantic QC checks 31-34.

Check 31: Glossary Compliance
Check 32: Wrong Translation Detection
Check 33: Hallucination Patterns
Check 34: Turkish Naturalness
"""

import json
import os
import tempfile

import pytest

from latex_pipeline.qc_engine import QCEngine, QCReport


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def glossary_dir(tmp_path):
    """Create a temp glossary directory with test data."""
    terms = {
        "terms": {
            "neural network": {
                "tr": "yapay sinir agi",
                "priority": "high",
                "wrong_alternatives": ["bilgisayar agi", "bilgisayar aglari"],
            },
            "Reynolds number": {
                "tr": "Reynolds sayisi",
                "priority": "high",
            },
            "nozzle": {
                "tr": "lule",
                "priority": "high",
                "wrong_alternatives": ["nozul", "nozul"],
            },
            "subsonic": {
                "tr": "ses alti",
                "priority": "high",
                "wrong_alternatives": ["alt ses", "alt sonik"],
            },
            "Game": {
                "tr": "Oyun",
                "priority": "high",
                "wrong_alternatives": ["kumar oynamak", "kumar"],
            },
            "ab": {
                "tr": "kisa",
                "priority": "high",
            },
        }
    }
    (tmp_path / "test_glossary.json").write_text(
        json.dumps(terms, ensure_ascii=False), encoding="utf-8",
    )
    return str(tmp_path)


@pytest.fixture
def engine(glossary_dir):
    """QCEngine with glossary loaded."""
    return QCEngine(glossary_dir=glossary_dir)


@pytest.fixture
def engine_no_glossary():
    """QCEngine without glossary."""
    return QCEngine()


# ===========================================================================
# Check 31: Glossary Compliance
# ===========================================================================

class TestGlossaryCompliance:
    """Tests for _check_glossary_compliance (check 31)."""

    def test_untranslated_high_priority_term_flagged(self, engine):
        """English high-priority term left untranslated should be flagged."""
        tr = "Bu neural network modeli iyi calisiyor."
        report = engine.analyze(tr)
        issues = [i for i in report.issues if i.check_name == "glossary_compliance"]
        assert len(issues) >= 1
        assert any("neural network" in i.message.lower() for i in issues)

    def test_correctly_translated_term_not_flagged(self, engine):
        """When Turkish equivalent is present, no issue should be raised."""
        tr = "Bu yapay sinir agi modeli iyi calisiyor."
        report = engine.analyze(tr)
        issues = [i for i in report.issues if i.check_name == "glossary_compliance"]
        # "neural network" not in TR, but "yapay sinir agi" is -> no flag
        assert len(issues) == 0 or not any(
            "neural network" in i.message.lower() for i in issues
        )

    def test_math_env_term_skipped(self, engine):
        """Terms inside math environments should NOT be flagged."""
        tr = "\\begin{equation}\n neural network = 5 \n\\end{equation}\nBu yapay sinir agi modeli."
        report = engine.analyze(tr)
        issues = [i for i in report.issues if i.check_name == "glossary_compliance"]
        # The "neural network" is inside equation, so should be skipped
        nn_issues = [i for i in issues if "neural network" in i.message.lower()]
        assert len(nn_issues) == 0

    def test_short_terms_skipped(self, engine):
        """Terms shorter than 4 chars should not be checked (false positive)."""
        tr = "ab cd ef gh ab ab ab"
        report = engine.analyze(tr)
        issues = [i for i in report.issues if i.check_name == "glossary_compliance"]
        # "ab" is only 2 chars, should be skipped
        ab_issues = [i for i in issues if "'ab'" in i.message.lower()]
        assert len(ab_issues) == 0

    def test_no_glossary_dir_skips_check(self, engine_no_glossary):
        """Without glossary_dir, check should silently skip."""
        tr = "Bu neural network modeli iyi calisiyor."
        report = engine_no_glossary.analyze(tr)
        issues = [i for i in report.issues if i.check_name == "glossary_compliance"]
        assert len(issues) == 0

    def test_multiple_terms_flagged(self, engine):
        """Multiple untranslated terms should each be reported."""
        tr = "Bu neural network ve Reynolds number arastirmasi."
        report = engine.analyze(tr)
        issues = [i for i in report.issues if i.check_name == "glossary_compliance"]
        assert len(issues) >= 2

    def test_verbatim_env_skipped(self, engine):
        """Terms inside verbatim should not be flagged."""
        tr = "\\begin{verbatim}\nnozzle = nozzle\n\\end{verbatim}\nBu lule iyi."
        report = engine.analyze(tr)
        issues = [i for i in report.issues if i.check_name == "glossary_compliance"]
        nozzle_issues = [i for i in issues if "nozzle" in i.message.lower()]
        assert len(nozzle_issues) == 0

    def test_inline_math_skipped(self, engine):
        """Terms inside inline math $...$ should not be flagged."""
        tr = "Formulde $subsonic$ kullanilir. Bu ses alti akistir."
        report = engine.analyze(tr)
        issues = [i for i in report.issues if i.check_name == "glossary_compliance"]
        sub_issues = [i for i in issues if "subsonic" in i.message.lower()]
        assert len(sub_issues) == 0


# ===========================================================================
# Check 32: Wrong Translation Detection
# ===========================================================================

class TestWrongTranslation:
    """Tests for _check_wrong_translation (check 32)."""

    def test_glossary_wrong_alternative_flagged(self, engine):
        """Known wrong alternative from glossary should be flagged."""
        tr = "Bu nozul cok iyi calisiyor."
        report = engine.analyze(tr)
        issues = [i for i in report.issues if i.check_name == "wrong_translation"]
        assert len(issues) >= 1
        assert any("nozul" in i.message.lower() for i in issues)

    def test_hardcoded_wrong_bilgisayar_agi(self, engine):
        """'bilgisayar agi' should be flagged as wrong for neural network."""
        tr = "Bu bilgisayar a\u011f\u0131 modeli kullanilir."
        report = engine.analyze(tr)
        issues = [i for i in report.issues if i.check_name == "wrong_translation"]
        assert len(issues) >= 1

    def test_hardcoded_hallucination_recine(self, engine):
        """Hallucination pattern 'sakiz agaci recine' should be flagged."""
        tr = "Bu sak\u0131z a\u011fac\u0131 re\u00e7ine ile yapildi."
        report = engine.analyze(tr)
        issues = [i for i in report.issues if i.check_name == "wrong_translation"]
        assert len(issues) >= 1

    def test_kilise_legit_prose_not_flagged(self, engine):
        """QC-N8: 'kilise uyelerinin' is legitimate prose (e.g. a religion
        book) and must NOT be flagged.  The book-specific hallucination
        signature was removed from _KNOWN_WRONG_TRANSLATIONS."""
        tr = "Kilise \u00fcyelerinin \u00e7o\u011fu bu g\u00f6r\u00fc\u015fe kat\u0131lmaz."
        report = engine.analyze(tr)
        issues = [i for i in report.issues if i.check_name == "wrong_translation"]
        assert len(issues) == 0

    def test_hardcoded_kumar(self, engine):
        """'kumar oynamak' is still flagged here because this fixture's
        glossary maps Game -> wrong_alternatives ['kumar oynamak', 'kumar'].
        (The hardcoded single-book pattern was removed in QC-N8, but the
        glossary-driven Part A path is independent and unchanged.)"""
        tr = "Bu kumar oynamak becerisidir."
        report = engine.analyze(tr)
        issues = [i for i in report.issues if i.check_name == "wrong_translation"]
        assert len(issues) >= 1

    def test_correct_translation_not_flagged(self, engine):
        """Correct translations should NOT trigger wrong_translation."""
        tr = "Bu l\u00fcle iyi calisiyor. Yapay sinir a\u011f\u0131 modeli."
        report = engine.analyze(tr)
        issues = [i for i in report.issues if i.check_name == "wrong_translation"]
        assert len(issues) == 0

    def test_math_env_wrong_alt_skipped(self, engine):
        """Wrong alternatives inside math env should not be flagged."""
        tr = "\\begin{equation}\n bilgisayar a\u011f\u0131 = x \n\\end{equation}\n Normal metin."
        report = engine.analyze(tr)
        issues = [i for i in report.issues if i.check_name == "wrong_translation"]
        bilgisayar_issues = [i for i in issues if "bilgisayar" in i.message.lower()]
        assert len(bilgisayar_issues) == 0

    def test_severity_is_critical(self, engine):
        """Wrong translation issues should always be critical."""
        tr = "Bu nozul calisiyor."
        report = engine.analyze(tr)
        issues = [i for i in report.issues if i.check_name == "wrong_translation"]
        for issue in issues:
            assert issue.severity == "critical"


# ===========================================================================
# Check 33: Hallucination Patterns
# ===========================================================================

class TestHallucinationPatterns:
    """Tests for _check_hallucination_patterns (check 33)."""

    def test_repeated_word_flagged(self, engine):
        """Same word 3+ times consecutively should be flagged."""
        tr = "Bu metin metin metin metin ile devam eder."
        report = engine.analyze(tr)
        issues = [i for i in report.issues if i.check_name == "hallucination_pattern"]
        assert any("tekrarlayan kelime" in i.message.lower() for i in issues)

    def test_no_repetition_clean(self, engine):
        """Normal text should not trigger repeated word check."""
        tr = "Bu bir normal metin satiri olmalidir ve dogru calisir."
        report = engine.analyze(tr)
        issues = [i for i in report.issues if i.check_name == "hallucination_pattern"
                  and "tekrarlayan" in i.message.lower()]
        assert len(issues) == 0

    def test_long_word_flagged(self, engine):
        """Word with 30+ chars should be flagged."""
        long_word = "a" * 35
        tr = f"Bu {long_word} kelimesi cok uzun."
        report = engine.analyze(tr)
        issues = [i for i in report.issues if i.check_name == "hallucination_pattern"
                  and "uzun kelime" in i.message.lower()]
        assert len(issues) >= 1

    def test_normal_length_word_clean(self, engine):
        """Normal-length words should not trigger long word check."""
        tr = "Bu normal bir kelime ve cumle yapisinda sorun yoktur."
        report = engine.analyze(tr)
        issues = [i for i in report.issues if i.check_name == "hallucination_pattern"
                  and "uzun kelime" in i.message.lower()]
        assert len(issues) == 0

    def test_fabricated_reference(self, engine):
        """Reference in TR but not in EN source should be flagged."""
        tr = "\u015eekil 99.99 gosterilmistir."
        en = "The result is shown in Figure 1.1."
        report = engine.analyze(tr, en)
        issues = [i for i in report.issues if i.check_name == "hallucination_pattern"
                  and "uydurulmus" in i.message.lower()]
        assert len(issues) >= 1

    def test_valid_reference_not_flagged(self, engine):
        """Reference that exists in EN should NOT be flagged."""
        tr = "\u015eekil 1.1 gosterilmistir."
        en = "The result is shown in Figure 1.1."
        report = engine.analyze(tr, en)
        issues = [i for i in report.issues if i.check_name == "hallucination_pattern"
                  and "uydurulmus" in i.message.lower()
                  and "1.1" in i.message]
        assert len(issues) == 0

    def test_consonant_cluster_flagged(self, engine):
        """5+ consonants in a row should be flagged."""
        tr = "Bu xzqwrtp kelimesi bozuk."
        report = engine.analyze(tr)
        issues = [i for i in report.issues if i.check_name == "hallucination_pattern"
                  and "unsuz" in i.message.lower()]
        assert len(issues) >= 1

    def test_math_env_hallucination_skipped(self, engine):
        """Hallucination patterns inside math env should be skipped."""
        long_word = "x" * 35
        tr = f"\\begin{{equation}}\n {long_word} = 5 \n\\end{{equation}}\nNormal metin."
        report = engine.analyze(tr)
        issues = [i for i in report.issues if i.check_name == "hallucination_pattern"
                  and "uzun kelime" in i.message.lower()]
        assert len(issues) == 0

    def test_repeated_phrase_in_paragraph(self, engine):
        """Same 4-word phrase 3+ times in a paragraph should be flagged."""
        phrase = "bu onemli bir sonuc"
        tr = f"{phrase} olarak {phrase} gorulmektedir {phrase} seklindedir {phrase} bulduk."
        report = engine.analyze(tr)
        issues = [i for i in report.issues if i.check_name == "hallucination_pattern"
                  and "tekrarlayan fraz" in i.message.lower()]
        assert len(issues) >= 1

    def test_comment_lines_skipped(self, engine):
        """Lines starting with % should be skipped."""
        tr = "% metin metin metin metin metin metin"
        report = engine.analyze(tr)
        issues = [i for i in report.issues if i.check_name == "hallucination_pattern"
                  and "tekrarlayan" in i.message.lower()]
        assert len(issues) == 0


# ===========================================================================
# Check 34: Turkish Naturalness
# ===========================================================================

class TestTurkishNaturalness:
    """Tests for _check_turkish_naturalness (check 34)."""

    def test_english_article_in_turkish_flagged(self, engine):
        """English article 'the' mixed into Turkish text should be flagged."""
        tr = "Bu formulde the sayi cok buyuk olmalidir."
        report = engine.analyze(tr)
        issues = [i for i in report.issues if i.check_name == "turkish_naturalness"
                  and "artikel" in i.message.lower()]
        assert len(issues) >= 1

    def test_no_english_article_clean(self, engine):
        """Clean Turkish text should not trigger article check."""
        tr = "Bu formulde sayi cok buyuk olmalidir ve dogru calisir."
        report = engine.analyze(tr)
        issues = [i for i in report.issues if i.check_name == "turkish_naturalness"
                  and "artikel" in i.message.lower()]
        assert len(issues) == 0

    def test_proper_noun_with_article_skipped(self, engine):
        """'the Reynolds' should be skipped (proper noun)."""
        tr = "Bu formulde the Reynolds sayisi kullanilir."
        report = engine.analyze(tr)
        issues = [i for i in report.issues if i.check_name == "turkish_naturalness"
                  and "artikel" in i.message.lower()
                  and "the Reynolds" in i.message]
        assert len(issues) == 0

    def test_long_unpunctuated_sentence_flagged(self, engine):
        """300+ char sentence without punctuation should be flagged."""
        words = ["kelime"] * 60  # ~420 chars with spaces
        tr = " ".join(words)
        report = engine.analyze(tr)
        issues = [i for i in report.issues if i.check_name == "turkish_naturalness"
                  and "uzun" in i.message.lower()]
        assert len(issues) >= 1

    def test_normal_sentence_not_flagged(self, engine):
        """Normal sentences with punctuation should not be flagged."""
        tr = "Bu bir cumle. Bu da ikinci cumle. Uc ve dort."
        report = engine.analyze(tr)
        issues = [i for i in report.issues if i.check_name == "turkish_naturalness"
                  and "uzun" in i.message.lower()]
        assert len(issues) == 0

    def test_math_env_skipped_for_naturalness(self, engine):
        """Content inside math environments should be skipped."""
        long_math = "x " * 200  # Would be 400+ chars
        tr = f"\\begin{{equation}}\n{long_math}\n\\end{{equation}}\nNormal metin."
        report = engine.analyze(tr)
        issues = [i for i in report.issues if i.check_name == "turkish_naturalness"]
        # Should not flag the long math content
        long_issues = [i for i in issues if "uzun" in i.message.lower()]
        assert len(long_issues) == 0

    def test_severity_is_medium(self, engine):
        """Naturalness issues should be medium severity."""
        tr = "Bu formulde the sayi cok buyuk olmalidir."
        report = engine.analyze(tr)
        issues = [i for i in report.issues if i.check_name == "turkish_naturalness"]
        for issue in issues:
            assert issue.severity == "medium"

    def test_latex_command_lines_skipped(self, engine):
        """Lines starting with \\ should be skipped."""
        tr = "\\section{the big number}\nNormal Turkce metin."
        report = engine.analyze(tr)
        issues = [i for i in report.issues if i.check_name == "turkish_naturalness"
                  and "artikel" in i.message.lower()]
        assert len(issues) == 0

    def test_broken_passive_flagged(self, engine):
        """'tarafindan' + active verb should be flagged."""
        tr = "Bu islem taraf\u0131ndan yapar seklinde gerceklesir."
        report = engine.analyze(tr)
        issues = [i for i in report.issues if i.check_name == "turkish_naturalness"
                  and "edilgen" in i.message.lower()]
        assert len(issues) >= 1


# ===========================================================================
# Cross-cutting: mask_protected_content tests
# ===========================================================================

class TestMaskProtectedContent:
    """Tests for _mask_protected_content helper."""

    def test_math_block_masked(self, engine):
        text = "before \\begin{equation}x=1\\end{equation} after"
        masked = engine._mask_protected_content(text)
        assert "x=1" not in masked
        assert "before" in masked
        assert "after" in masked

    def test_inline_math_masked(self, engine):
        text = "before $x+y$ after"
        masked = engine._mask_protected_content(text)
        assert "x+y" not in masked
        assert "before" in masked
        assert "after" in masked

    def test_display_math_masked(self, engine):
        text = "before \\[x = y\\] after"
        masked = engine._mask_protected_content(text)
        assert "x = y" not in masked

    def test_verb_masked(self, engine):
        text = "before \\verb|code here| after"
        masked = engine._mask_protected_content(text)
        assert "code here" not in masked

    def test_plain_text_preserved(self, engine):
        text = "Bu tamamen normal bir metin."
        masked = engine._mask_protected_content(text)
        assert masked == text


# ===========================================================================
# Integration: QCEngine initialization
# ===========================================================================

class TestQCEngineInit:
    """Tests for QCEngine initialization with glossary."""

    def test_init_without_glossary(self):
        engine = QCEngine()
        assert engine._glossary_dir is None
        assert engine._glossary_terms is None

    def test_init_with_glossary(self, glossary_dir):
        engine = QCEngine(glossary_dir=glossary_dir)
        assert engine._glossary_dir == glossary_dir

    def test_lazy_load_on_first_analyze(self, glossary_dir):
        engine = QCEngine(glossary_dir=glossary_dir)
        assert engine._glossary_loaded is False
        engine.analyze("Test metin")
        assert engine._glossary_loaded is True

    def test_invalid_glossary_dir_no_crash(self):
        engine = QCEngine(glossary_dir="/nonexistent/path/to/nowhere")
        report = engine.analyze("Test metin")
        # Should not crash, just skip glossary checks
        assert isinstance(report, QCReport)

    def test_corrupt_json_no_crash(self, tmp_path):
        (tmp_path / "bad.json").write_text("NOT VALID JSON {{{")
        engine = QCEngine(glossary_dir=str(tmp_path))
        report = engine.analyze("Test metin")
        assert isinstance(report, QCReport)
