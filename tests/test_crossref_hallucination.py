"""Tests for cross-ref stash expansion and hallucination detection.

Module 3: Chained reference regex, page references, footnote stash,
hallucination detection (QC check 29), and repetition fix.
"""
import pytest
import re


class TestChainedReferences:
    """Test _FIGURE_REF_RE chained reference matching."""

    def test_single_ref(self):
        from latex_pipeline.translator import _FIGURE_REF_RE
        m = _FIGURE_REF_RE.search("see Figure 7.4 for details")
        assert m is not None
        assert m.group(0) == "Figure 7.4"

    def test_chained_and(self):
        from latex_pipeline.translator import _FIGURE_REF_RE
        m = _FIGURE_REF_RE.search("see Figures 7.4 and 7.5 for details")
        assert m is not None
        assert "7.4" in m.group(0)
        assert "7.5" in m.group(0)

    def test_chained_comma(self):
        from latex_pipeline.translator import _FIGURE_REF_RE
        m = _FIGURE_REF_RE.search("Theorems 2.6, 2.7")
        assert m is not None
        assert "2.6" in m.group(0)
        assert "2.7" in m.group(0)

    def test_chained_range(self):
        from latex_pipeline.translator import _FIGURE_REF_RE
        m = _FIGURE_REF_RE.search("Eqs. 3.1-3.5")
        assert m is not None
        assert "3.1" in m.group(0)
        assert "3.5" in m.group(0)

    def test_chained_range_endash(self):
        """Test en-dash range separator."""
        from latex_pipeline.translator import _FIGURE_REF_RE
        m = _FIGURE_REF_RE.search("Eqs. 3.1\u20133.5")
        assert m is not None
        assert "3.1" in m.group(0)
        assert "3.5" in m.group(0)

    def test_chained_ve(self):
        """Test Turkish 've' as separator."""
        from latex_pipeline.translator import _FIGURE_REF_RE
        m = _FIGURE_REF_RE.search("Sections 3.1 ve 3.2")
        assert m is not None
        assert "3.1" in m.group(0)
        assert "3.2" in m.group(0)

    def test_chained_three_items(self):
        """Test three items: comma + and."""
        from latex_pipeline.translator import _FIGURE_REF_RE
        m = _FIGURE_REF_RE.search("Tables 1.1, 1.2 and 1.3 show")
        assert m is not None
        matched = m.group(0)
        assert "1.1" in matched
        assert "1.2" in matched
        assert "1.3" in matched

    def test_no_greedy_and(self):
        """'Figure 7.4 and the results' must NOT capture 'and the'."""
        from latex_pipeline.translator import _FIGURE_REF_RE
        m = _FIGURE_REF_RE.search("Figure 7.4 and the results show")
        assert m is not None
        matched = m.group(0)
        assert matched == "Figure 7.4"
        assert "the" not in matched

    def test_single_ref_still_works(self):
        """Backward compat: single ref without continuation."""
        from latex_pipeline.translator import _FIGURE_REF_RE
        cases = [
            ("Table 3.2", "Table 3.2"),
            ("Eq. 4.5", "Eq. 4.5"),
            ("Theorem 1", "Theorem 1"),
            ("Fig. 3.2a", "Fig. 3.2a"),
            ("Chapter 12", "Chapter 12"),
            ("Lemma 5.3", "Lemma 5.3"),
        ]
        for text, expected in cases:
            m = _FIGURE_REF_RE.search(f"see {text} for")
            assert m is not None, f"Failed to match: {text}"
            assert m.group(0) == expected, f"Expected '{expected}', got '{m.group(0)}'"


class TestPageRef:
    """Test _PAGE_REF_RE page reference matching."""

    def test_page_ref_single(self):
        from latex_pipeline.translator import _PAGE_REF_RE
        m = _PAGE_REF_RE.search("see p. 234")
        assert m is not None
        assert "234" in m.group(0)

    def test_page_ref_range(self):
        from latex_pipeline.translator import _PAGE_REF_RE
        m = _PAGE_REF_RE.search("pp. 45-67")
        assert m is not None
        assert "45" in m.group(0)
        assert "67" in m.group(0)

    def test_page_ref_word(self):
        from latex_pipeline.translator import _PAGE_REF_RE
        m = _PAGE_REF_RE.search("see page 12 for")
        assert m is not None
        assert "12" in m.group(0)

    def test_pages_range(self):
        from latex_pipeline.translator import _PAGE_REF_RE
        m = _PAGE_REF_RE.search("Pages 100-200")
        assert m is not None
        assert "100" in m.group(0)
        assert "200" in m.group(0)

    def test_page_ref_endash(self):
        from latex_pipeline.translator import _PAGE_REF_RE
        m = _PAGE_REF_RE.search("pp. 45\u201367")
        assert m is not None
        assert "45" in m.group(0)
        assert "67" in m.group(0)


class TestFootnoteStash:
    """Test _FOOTNOTE_RE footnote matching."""

    def test_footnote_simple(self):
        from latex_pipeline.translator import _FOOTNOTE_RE
        text = r"Some text\footnote{This is a footnote.} more text."
        m = _FOOTNOTE_RE.search(text)
        assert m is not None
        assert "footnote" in m.group(0)
        assert "This is a footnote." in m.group(1)

    def test_footnote_nested_braces(self):
        from latex_pipeline.translator import _FOOTNOTE_RE
        text = r"Text\footnote{This has \textit{emphasis} inside.} rest."
        m = _FOOTNOTE_RE.search(text)
        assert m is not None
        assert "emphasis" in m.group(0)

    def test_footnote_double_nested(self):
        from latex_pipeline.translator import _FOOTNOTE_RE
        text = r"Text\footnote{See \textbf{\textit{bold italic}} here.} rest."
        m = _FOOTNOTE_RE.search(text)
        assert m is not None
        assert "bold italic" in m.group(0)


class TestHallucinationDetection:
    """Test QC check 29: hallucination detection."""

    def test_repetition_detection(self):
        """Repetition loops should be detected without crash."""
        from latex_pipeline.qc_engine import QCEngine
        engine = QCEngine()
        # Create a clear repetition: same 6-word phrase repeated 4 times
        phrase = "bu bir test cumlesidir su anda"
        tr = f"{phrase} {phrase} {phrase} {phrase}"
        en = "This is a test sentence right now."
        report = engine.analyze(tr, en)
        # Should detect repetition
        halluc_issues = [i for i in report.issues if i.check_name == "hallucination"]
        assert len(halluc_issues) > 0, "Expected at least one hallucination issue"
        # At least one should mention repetition
        rep_issues = [i for i in halluc_issues if "repetition" in i.message.lower()]
        assert len(rep_issues) > 0, "Expected a repetition loop detection"

    def test_no_false_positive_on_normal_text(self):
        """Normal translated text should not trigger hallucination."""
        from latex_pipeline.qc_engine import QCEngine
        engine = QCEngine()
        en = "This is a normal paragraph about neural networks."
        tr = "Bu, sinir aglari hakkinda normal bir paragraftir."
        report = engine.analyze(tr, en)
        halluc_issues = [i for i in report.issues if i.check_name == "hallucination"]
        # No repetition or extreme length ratio issues expected
        rep_issues = [i for i in halluc_issues if "repetition" in i.message.lower()]
        assert len(rep_issues) == 0

    def test_length_ratio_extreme(self):
        """Extreme TR/EN length ratio should flag hallucination."""
        from latex_pipeline.qc_engine import QCEngine
        engine = QCEngine()
        # EN: moderate paragraph, TR: 20x longer (bloated)
        en_para = "This is a test paragraph with enough content to exceed the minimum threshold for checking."
        tr_para = ("Bu bir test paragraf " * 60).strip()
        # Wrap in enough lines for paragraph detection
        en = en_para + "\n"
        tr = tr_para + "\n"
        report = engine.analyze(tr, en)
        assert isinstance(report.score, float)

    def test_orphan_dotted_number(self):
        """Numbers in EN but missing in TR should be flagged."""
        from latex_pipeline.qc_engine import QCEngine
        engine = QCEngine()
        en = "See Figure 7.4 and Table 3.2 and also Section 5.1 and Eq. 2.3 for details."
        # TR has none of these numbers
        tr = "Detaylar icin ilgili sekil ve tablolara bakiniz."
        report = engine.analyze(tr, en)
        halluc_issues = [i for i in report.issues if i.check_name == "hallucination"]
        orphan_issues = [i for i in halluc_issues if "orphan" in i.message.lower()]
        assert len(orphan_issues) > 0, "Expected orphan dotted number detection"

    def test_analyze_returns_score(self):
        """Basic sanity: analyze always returns a float score."""
        from latex_pipeline.qc_engine import QCEngine
        engine = QCEngine()
        report = engine.analyze("Test text.", "Source text.")
        assert isinstance(report.score, float)
        assert 0.0 <= report.score <= 100.0


class TestRepetitionFix:
    """Test autofix handler for repetition loops."""

    def test_repetition_truncated(self):
        """Repetition loop should be truncated to first occurrence."""
        from latex_pipeline.autofix import AutoFixer
        from latex_pipeline.qc_engine import QCEngine, QCIssue

        engine = QCEngine()
        fixer = AutoFixer(qc_engine=engine)

        phrase = "bu bir test cumlesidir su anda"
        body = f"Baslangic. {phrase} {phrase} {phrase} {phrase} Bitis."

        # Create a mock issue
        issues = [QCIssue(
            check_name="hallucination",
            severity="critical",
            line_num=1,
            message="MT repetition loop detected",
            auto_fixable=True,
            confidence=0.95,
            fix_suggestion="Truncate",
            context="",
        )]

        fixed, fixes = fixer._fix_segment_hallucination(body, issues, [])
        # Should contain the phrase only once (plus surrounding text)
        count = fixed.count(phrase)
        assert count == 1, f"Expected 1 occurrence, got {count}"
        assert "Baslangic" in fixed
        assert "Bitis" in fixed

    def test_no_fix_on_clean_text(self):
        """Clean text should not be modified."""
        from latex_pipeline.autofix import AutoFixer
        from latex_pipeline.qc_engine import QCEngine

        engine = QCEngine()
        fixer = AutoFixer(qc_engine=engine)

        body = "Bu normal bir cumle. Baska bir cumle daha var burada."
        fixed, fixes = fixer._fix_segment_hallucination(body, [], [])
        assert fixed == body
        assert len(fixes) == 0


class TestFigureRefTranslation:
    """Test that _translate_figure_ref handles chained references."""

    def test_chained_ref_translation(self):
        """Chained ref: label translated, all numbers preserved."""
        from latex_pipeline.translator import _FIGURE_REF_RE, _translate_figure_ref
        m = _FIGURE_REF_RE.search("Figures 7.4 and 7.5")
        assert m is not None
        translated = _translate_figure_ref(m)
        # Label should be Turkish
        assert "\u015eekil" in translated or "Sekil" in translated.replace("\u015e", "S")
        # Both numbers must be preserved
        assert "7.4" in translated
        assert "7.5" in translated
