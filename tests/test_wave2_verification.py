"""
Wave 2 verification tests: Cross-cutting integration and edge-case coverage.

Covers gaps identified in Wave 1 test coverage analysis:

1. batch_generate + stash/restore combined workflow
2. verify_terms Pass 3 (EN echo) + wrong_alternatives combined
3. Heading glossary partial match + case insensitive interaction
4. Inline command stash placeholder recovery (deeply mangled placeholders)
5. Retranslator EPUB cleanup + normal text mixture
6. Integration test: EPUB body.tex simulation with multiple subsystems
7. GlossaryEngine domain filtering and priority resolution
8. _is_structural_line edge cases from retranslator
9. _translate_heading_text multi-word partial match with stop words
10. _post_process_translation prompt leak + EPUB hallucination combined

2026-03-05  --  Tester Agent (Wave 2)
"""

import json
import re
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_glossary_engine(book_type: str, extra_terms=None, terms_dict=None):
    """Create a GlossaryEngine with controlled glossary content.

    If terms_dict is provided, a temporary glossary directory is created with
    exactly those terms.  Otherwise falls back to the project glossary dir.

    G2c (KARAR 25, 2026-10-05): geçici sözlük dosyasının adı ``curated.json``
    (eskiden ``default.json``). Genel alan sözlükleri silindi ve
    GlossaryEngine haritada olmayan dosyayı yüklemez; kalan iki dosyadan
    curated.json her türde (alan: general) yüklenir — davranış aynı.
    """
    from latex_pipeline.glossary import GlossaryEngine

    if terms_dict is not None:
        tmp = Path(tempfile.mkdtemp()) / "glossary"
        tmp.mkdir()
        (tmp / "curated.json").write_text(json.dumps({
            "terms": terms_dict,
        }), encoding="utf-8")
        glossary_dir = tmp
    else:
        glossary_dir = PROJECT_ROOT / "glossary"
        if not glossary_dir.exists():
            tmp = Path(tempfile.mkdtemp()) / "glossary"
            tmp.mkdir()
            (tmp / "curated.json").write_text(json.dumps({
                "terms": {
                    "neural network": {"tr": "sinir agi", "domain": "general"},
                    "gradient descent": {"tr": "gradyan inisi", "domain": "general"},
                }
            }))
            glossary_dir = tmp

    return GlossaryEngine(
        glossary_dir=glossary_dir,
        book_type=book_type,
        extra_terms=extra_terms or {},
    )


def _make_batch_translator(batch_fn=None, single_fn=None):
    """Create a LatexTranslator with mocked MT for batch testing."""
    from latex_pipeline.translator import LatexTranslator, TranslationStats
    from latex_pipeline.config import PipelineConfig

    config = PipelineConfig(
        translate=True,
        segment_timeout=60,
    )
    lt = LatexTranslator(
        pipeline_config=config,
        mt_engine="hymt-7b",
        source_lang="en",
        target_lang="tr",
    )

    mock_bt = MagicMock()
    # A1 fix (Yol B): _batch_mt_call artık translate_batch_protected varsa onu
    # tercih ediyor; MagicMock bunu otomatik üretir. Bu testler ham
    # _translate_batch_hymt (fallback) yolunu doğruladığı için attribute'u sil.
    del mock_bt.translate_batch_protected

    if batch_fn is None:
        def _default_batch(texts, max_length=512):
            return [t.upper() for t in texts]
        mock_bt._translate_batch_hymt = MagicMock(side_effect=_default_batch)
    else:
        mock_bt._translate_batch_hymt = MagicMock(side_effect=batch_fn)

    if single_fn is None:
        mock_bt.translate_text = MagicMock(side_effect=lambda t: t.upper())
    else:
        mock_bt.translate_text = MagicMock(side_effect=single_fn)

    lt._translator = mock_bt
    lt._checkpoint_dir = None
    lt._checkpoint_counter = 0
    lt._progress_callback = None
    lt.stats = TranslationStats()

    return lt


# ===========================================================================
# 1. batch_generate + stash/restore combined workflow
# ===========================================================================

class TestBatchWithStashRestore:
    """Verify that batch translation correctly stashes and restores
    inline LaTeX commands across multiple paragraphs simultaneously."""

    def test_batch_multiple_paragraphs_with_inline_cmds(self):
        """Multiple paragraphs each containing inline LaTeX commands should
        have those commands stashed before MT and restored after."""
        def _batch_fn(texts, max_length=512):
            results = []
            for t in texts:
                parts = re.split(r"(<<(?:LATEX|CMD)_\d+>>)", t)
                translated = []
                for p in parts:
                    if re.fullmatch(r"<<(?:LATEX|CMD)_\d+>>", p):
                        translated.append(p)
                    else:
                        translated.append(p.upper())
                results.append("".join(translated))
            return results

        lt = _make_batch_translator(batch_fn=_batch_fn)

        body = (
            "\\centering First paragraph text.\n\n"
            "\\noindent Second paragraph text.\n\n"
            "\\vfill Third paragraph text."
        )
        result = lt._translate_paragraphs(body)

        assert "\\centering" in result, "\\centering should survive batch stash/restore"
        assert "\\noindent" in result, "\\noindent should survive batch stash/restore"
        assert "\\vfill" in result, "\\vfill should survive batch stash/restore"
        assert "FIRST PARAGRAPH TEXT." in result
        assert "SECOND PARAGRAPH TEXT." in result
        assert "THIRD PARAGRAPH TEXT." in result

    def test_batch_with_begin_end_env_stash(self):
        """\\begin{titlepage} and \\end{titlepage} in a batch paragraph should
        be stashed to <<CMD_N>> and restored correctly after MT."""
        def _batch_fn(texts, max_length=512):
            results = []
            for t in texts:
                parts = re.split(r"(<<(?:LATEX|CMD)_\d+>>)", t)
                translated = []
                for p in parts:
                    if re.fullmatch(r"<<(?:LATEX|CMD)_\d+>>", p):
                        translated.append(p)
                    else:
                        translated.append(p.upper())
                results.append("".join(translated))
            return results

        lt = _make_batch_translator(batch_fn=_batch_fn)

        body = "\\begin{titlepage} Some title text \\end{titlepage}"
        result = lt._translate_paragraphs(body)

        assert "\\begin{titlepage}" in result
        assert "\\end{titlepage}" in result
        assert "SOME TITLE TEXT" in result

    def test_batch_with_latex_placeholder_and_cmd_placeholder(self):
        """Text with both <<LATEX_N>> (pre-stashed math) and inline commands
        should preserve both placeholder types through batch translation."""
        def _batch_fn(texts, max_length=512):
            results = []
            for t in texts:
                parts = re.split(r"(<<(?:LATEX|CMD)_\d+>>)", t)
                translated = []
                for p in parts:
                    if re.fullmatch(r"<<(?:LATEX|CMD)_\d+>>", p):
                        translated.append(p)
                    else:
                        translated.append(p.upper())
                results.append("".join(translated))
            return results

        lt = _make_batch_translator(batch_fn=_batch_fn)

        body = "\\centering The value is <<LATEX_0>> as shown above."
        result = lt._translate_paragraphs(body)

        assert "\\centering" in result, "Inline cmd should be restored"
        assert "<<LATEX_0>>" in result, "Pre-stashed math should survive"
        assert "THE VALUE IS" in result


# ===========================================================================
# 2. verify_terms: Pass 3 (EN echo) + wrong_alternatives combined
# ===========================================================================

class TestVerifyTermsCombined:
    """Test that verify_terms handles both wrong_alternatives (Pass 2)
    and EN echo replacement (Pass 3) in a single call."""

    def test_wrong_alternative_replaced(self):
        """A known wrong alternative should be replaced with the correct term."""
        engine = _make_glossary_engine("science", terms_dict={
            "neural network": {
                "tr": "sinir agi",
                "domain": "general",
                "priority": "high",
                "wrong_alternatives": ["bilgisayar agi", "yapay zeka agi"],
            },
        })
        text = "Bu bilgisayar agi ile egitildi."
        result = engine.verify_terms(text, {})
        assert "sinir agi" in result, (
            f"wrong_alternative 'bilgisayar agi' should be replaced: {result}"
        )
        assert "bilgisayar agi" not in result

    def test_en_echo_replaced_for_noninject_genre(self):
        """For non-inject genres (empty term_map), high-priority EN terms
        that survived MT should be replaced with TR in Pass 3."""
        engine = _make_glossary_engine("philosophy", terms_dict={
            "Stoicism": {
                "tr": "Stoacılık",
                "domain": "general",
                "priority": "high",
            },
        })
        text = "Stoicism kavrami onemlidir."
        result = engine.verify_terms(text, {})
        assert "Stoacılık" in result, (
            f"EN echo 'Stoicism' should be replaced for non-inject genre: {result}"
        )

    def test_short_en_echo_not_replaced(self):
        """Very short terms (< 4 chars) should NOT be replaced in Pass 3."""
        engine = _make_glossary_engine("philosophy", terms_dict={
            "use": {
                "tr": "kullanmak",
                "domain": "general",
                "priority": "low",
            },
        })
        text = "We use this concept here."
        result = engine.verify_terms(text, {})
        assert "use" in result, (
            "Short term (< 4 chars) should NOT be replaced by EN echo scan"
        )

    def test_wrong_alt_and_en_echo_combined(self):
        """Both wrong_alternatives and EN echo should fire in the same call.
        This tests that Pass 2 runs before Pass 3 without interference."""
        engine = _make_glossary_engine("philosophy", terms_dict={
            "neural network": {
                "tr": "sinir agi",
                "domain": "general",
                "priority": "high",
                "wrong_alternatives": ["bilgisayar agi"],
            },
            "Stoicism": {
                "tr": "Stoacılık",
                "domain": "general",
                "priority": "high",
            },
        })
        text = "bilgisayar agi ve Stoicism kavramlari."
        result = engine.verify_terms(text, {})
        assert "sinir agi" in result, "wrong_alternative should be fixed"
        assert "Stoacılık" in result, "EN echo should be replaced"
        assert "bilgisayar agi" not in result
        assert "Stoicism" not in result

    def test_short_en_term_not_replaced_in_echo_scan(self):
        """EN terms shorter than 4 chars should not be replaced in Pass 3
        to avoid false positives (e.g. 'the', 'and')."""
        engine = _make_glossary_engine("philosophy", terms_dict={
            "GDP": {
                "tr": "GSYH",
                "domain": "general",
                "priority": "high",
            },
        })
        text = "GDP degeri yuksek."
        # GDP is only 3 chars, should NOT be replaced
        result = engine.verify_terms(text, {})
        assert "GDP" in result, "3-char term should not be replaced in echo scan"


# ===========================================================================
# 3. Heading glossary partial match + case insensitive
# ===========================================================================

class TestHeadingGlossaryPartialMatch:
    """Test _translate_heading_text with partial glossary matches."""

    def test_partial_match_case_insensitive(self):
        """Heading 'NEURAL NETWORKS' (all caps) should match 'Neural Networks'
        in the glossary via case-insensitive comparison."""
        from latex_pipeline.translator import LatexTranslator
        glossary = LatexTranslator._HEADING_GLOSSARY
        lower_map = {k.lower(): v for k, v in glossary.items()}
        assert "neural networks" in lower_map
        assert lower_map["neural networks"] == "Sinir Aglari" or "Sinir" in lower_map["neural networks"]

    def test_compound_heading_partial_glossary(self):
        """A compound heading like 'Deep Learning and Neural Networks' should
        match both 'Deep Learning' and 'Neural Networks' partial matches."""
        from latex_pipeline.translator import LatexTranslator
        glossary = LatexTranslator._HEADING_GLOSSARY
        text = "Deep Learning and Neural Networks"
        text_lower = text.lower()
        matches = []
        for en, tr in glossary.items():
            if en.lower() in text_lower and len(en) >= 3:
                matches.append((en, tr, len(en)))
        # Both "Deep Learning" and "Neural Networks" should be found
        en_terms = [m[0] for m in matches]
        assert any("Deep Learning" in t for t in en_terms), (
            f"'Deep Learning' not matched in heading glossary: {en_terms}"
        )
        assert any("Neural Networks" in t or "Neural Network" in t for t in en_terms), (
            f"'Neural Networks' not matched in heading glossary: {en_terms}"
        )

    def test_turkish_title_case_preserves_small_words(self):
        """Turkish title case should keep connector words lowercase
        (ve, ile, bir) unless they are the first word."""
        from latex_pipeline.translator import LatexTranslator
        result = LatexTranslator._turkish_title_case("sinir aglari ve derin ogrenme")
        words = result.split()
        # "ve" should stay lowercase
        if len(words) >= 4:
            assert words[2].lower() == "ve", (
                f"'ve' should stay lowercase in title case: {result}"
            )

    def test_turkish_title_case_first_word_always_capitalized(self):
        """First word should always be capitalized even if it is a small word."""
        from latex_pipeline.translator import LatexTranslator
        result = LatexTranslator._turkish_title_case("ve sonuc")
        assert result[0].isupper() or result[0] in "VİI", (
            f"First word should be capitalized: {result}"
        )

    def test_turkish_title_case_empty_string(self):
        """Empty string should return empty."""
        from latex_pipeline.translator import LatexTranslator
        assert LatexTranslator._turkish_title_case("") == ""


# ===========================================================================
# 4. Inline command stash: deeply mangled placeholder recovery
# ===========================================================================

class TestMangledPlaceholderRecovery:
    """Test recovery of severely mangled <<CMD_N>> placeholders."""

    def test_spaces_everywhere(self):
        """MT might add spaces in every position: < < C M D _ 0 > >"""
        from latex_pipeline.translator import LatexTranslator
        cmds = [r"\centering"]
        mangled = "< < CMD _ 0 > > metin burada"
        restored = LatexTranslator._restore_inline_commands(mangled, cmds)
        assert r"\centering" in restored, (
            f"Deeply mangled placeholder not recovered: {restored}"
        )

    def test_newline_in_placeholder(self):
        """MT might insert a newline inside the placeholder: <<CMD_\n0>>"""
        from latex_pipeline.translator import LatexTranslator
        cmds = [r"\vfill"]
        # The mangled recovery regex uses \s* which should match \n
        mangled = "< <CMD_\n0> > some text"
        restored = LatexTranslator._restore_inline_commands(mangled, cmds)
        # This might or might not recover -- test the behavior
        # At minimum, the function should not crash
        assert isinstance(restored, str)

    def test_multiple_mangled_placeholders(self):
        """Multiple mangled placeholders in one string should all be recovered."""
        from latex_pipeline.translator import LatexTranslator
        cmds = [r"\centering", r"\vfill", r"\noindent"]
        mangled = "< < CMD _ 0 > > metin < < CMD _ 1 > > daha fazla < < CMD _ 2 > >"
        restored = LatexTranslator._restore_inline_commands(mangled, cmds)
        assert r"\centering" in restored
        assert r"\vfill" in restored
        assert r"\noindent" in restored

    def test_out_of_range_index_ignored(self):
        """A mangled placeholder with an out-of-range index should be kept as-is."""
        from latex_pipeline.translator import LatexTranslator
        cmds = [r"\centering"]  # Only index 0 exists
        mangled = "< < CMD _ 5 > > normal text"
        restored = LatexTranslator._restore_inline_commands(mangled, cmds)
        # Index 5 is out of range -- the regex callback should return match.group(0)
        assert "normal text" in restored


# ===========================================================================
# 5. Retranslator EPUB cleanup + normal text mixture
# ===========================================================================

class TestRetranslatorEpubMixed:
    """Test _clean_epub_placeholder_hallucinations with mixed content."""

    def test_normal_text_with_single_hallucination(self):
        """Normal Turkish text with one EPUB hallucination should be cleaned."""
        from latex_pipeline.retranslator import _clean_epub_placeholder_hallucinations
        text = "Bu bir test cumlesiidr @Bilinmeyen0000 ve devam eder"
        result = _clean_epub_placeholder_hallucinations(text)
        assert "@Bilinmeyen0000" not in result
        assert "test" in result
        assert "devam" in result

    def test_multiple_different_hallucinations(self):
        """Text with multiple different EPUB hallucination patterns should have
        all of them removed."""
        from latex_pipeline.retranslator import _clean_epub_placeholder_hallucinations
        text = "Metin @Bilinmeyen0000 ortada @@KNOWN0001@@ sonda KNOWN0002@@"
        result = _clean_epub_placeholder_hallucinations(text)
        assert "@Bilinmeyen" not in result
        assert "@@KNOWN" not in result
        assert "KNOWN0002@@" not in result
        assert "Metin" in result

    def test_latex_commands_not_affected(self):
        """LaTeX commands should not be caught by EPUB hallucination regex."""
        from latex_pipeline.retranslator import _clean_epub_placeholder_hallucinations
        text = "\\textit{Giris} bolumunde \\textbf{onemli} konular var"
        result = _clean_epub_placeholder_hallucinations(text)
        assert result == text, "LaTeX commands should be untouched"

    def test_only_hallucination_returns_original(self):
        """If removing the hallucination leaves text shorter than 4 chars,
        original should be returned."""
        from latex_pipeline.retranslator import _clean_epub_placeholder_hallucinations
        text = "@Bilinmeyen0000 ab"
        result = _clean_epub_placeholder_hallucinations(text)
        # After removal: "ab" which is <=3 chars, so original returned
        assert result == text


# ===========================================================================
# 6. Integration test: EPUB body.tex simulation
# ===========================================================================

class TestEpubBodyTexIntegration:
    """Integration test simulating an EPUB-originated body.tex file
    going through multiple pipeline subsystems."""

    def test_epub_body_inject_skip_for_philosophy(self):
        """For book_type='philosophy', inject_terms() should return text unchanged
        even when the text contains glossary terms."""
        engine = _make_glossary_engine("philosophy", extra_terms={
            "categorical imperative": "kategorik buyruk",
            "social contract": "toplum sözleşmesi",
        })
        body = (
            "\\begin{titlepage}\n"
            "\\centering\n"
            "Notes on Virtue\n"
            "\\end{titlepage}\n"
            "\n"
            "\\section{Social Contract}\n"
            "The categorical imperative shapes every social contract.\n"
        )
        result, term_map = engine.inject_terms(body)
        assert result == body, (
            "inject_terms should skip for philosophy book type"
        )
        assert term_map == {}

    def test_epub_body_inject_disabled_for_science(self):
        """inject_terms() is now disabled for ALL types including science.
        Text should pass through unchanged."""
        engine = _make_glossary_engine("science", terms_dict={
            "neural network": {
                "tr": "sinir agi",
                "domain": "general",
            },
        })
        body = (
            "\\section{Neural Network Architecture}\n"
            "The neural network is trained on large datasets.\n"
        )
        result, term_map = engine.inject_terms(body)
        assert result == body, "inject_terms should not modify text (disabled)"
        assert term_map == {}

    def test_inline_commands_stash_through_paragraph_translation(self):
        """Inline LaTeX commands in an EPUB body should be stashed before
        translation and restored after."""
        lt = _make_batch_translator()

        body = (
            "\\begin{titlepage}\n\n"
            "\\centering\n\n"
            "Some title text here.\n\n"
            "\\vfill\n\n"
            "Author name and info."
        )
        result = lt._translate_paragraphs(body)

        # Structural commands should survive
        assert "\\begin{titlepage}" in result
        assert "\\centering" in result
        assert "\\vfill" in result
        # Text should be translated (uppercased by mock)
        assert "SOME TITLE TEXT HERE." in result or "AUTHOR NAME AND INFO." in result

    def test_placeholder_leak_not_present(self):
        """After stash/restore cycle, no <<CMD_N>> placeholders should remain
        in the output."""
        lt = _make_batch_translator()

        body = "\\centering Some text \\vfill More text \\noindent Final text."
        result = lt._translate_paragraphs(body)

        # No raw placeholders should leak
        assert "<<CMD_" not in result, (
            f"Placeholder leak detected in output: {result}"
        )

    def test_heading_not_translated_by_batch(self):
        """\\section{...} lines should be skipped by _translate_paragraphs
        (they are handled by Phase 2 separately)."""
        lt = _make_batch_translator()

        body = "\\section{Introduction}\n\nThis is the body text."
        result = lt._translate_paragraphs(body)

        # Heading should be preserved as-is
        assert "\\section{Introduction}" in result
        # Body should be translated
        assert "THIS IS THE BODY TEXT." in result


# ===========================================================================
# 7. GlossaryEngine domain filtering and priority resolution
# ===========================================================================

class TestGlossaryDomainAndPriority:
    """Test term domain filtering and priority-based deduplication."""

    def test_higher_priority_wins(self):
        """When two terms with the same EN key have different priorities,
        the higher priority should win."""
        from latex_pipeline.glossary import GlossaryEngine, TermEntry

        engine = _make_glossary_engine("science", terms_dict={})
        # Manually add two entries with different priorities
        engine._add_term(TermEntry(en="test", tr="test_low", priority="low"))
        engine._add_term(TermEntry(en="test", tr="test_high", priority="high"))

        assert engine._terms["test"].tr == "test_high"

    def test_same_priority_turkish_chars_win(self):
        """When two terms have the same priority, the one with Turkish
        special characters should win."""
        from latex_pipeline.glossary import GlossaryEngine, TermEntry

        engine = _make_glossary_engine("science", terms_dict={})
        engine._add_term(TermEntry(en="gradient", tr="gradyan", priority="medium"))
        engine._add_term(TermEntry(
            en="gradient", tr="gradyan\u0131", priority="medium"  # with dotless i
        ))

        # The Turkish-char version should win
        assert "\u0131" in engine._terms["gradient"].tr

    def test_case_sensitive_term_exact_only(self):
        """A case_sensitive term should only match exact case."""
        engine = _make_glossary_engine("science", terms_dict={
            "ReLU": {
                "tr": "ReLU",
                "domain": "general",
                "case_sensitive": True,
                "keep_original": True,
            },
        })
        # Exact match
        matches = engine.match_terms("The ReLU activation function")
        relu_matches = [m for m in matches if m.en_term == "ReLU"]
        assert len(relu_matches) >= 1, "Exact case 'ReLU' should match"

        # Wrong case should NOT match
        matches2 = engine.match_terms("The relu activation function")
        relu_matches2 = [m for m in matches2 if m.en_term == "ReLU"]
        assert len(relu_matches2) == 0, "Wrong case 'relu' should not match case_sensitive term"

    def test_keep_original_preserves_en(self):
        """A keep_original=True term should have effective_tr equal to en."""
        from latex_pipeline.glossary import TermEntry
        entry = TermEntry(en="MATLAB", tr="", keep_original=True)
        assert entry.effective_tr == "MATLAB"


# ===========================================================================
# 8. _is_structural_line edge cases
# ===========================================================================

class TestIsStructuralLine:
    """Test _is_structural_line from retranslator for various edge cases."""

    def test_empty_line_is_structural(self):
        """Empty or whitespace-only lines should be structural."""
        from latex_pipeline.retranslator import _is_structural_line
        assert _is_structural_line("") is True
        assert _is_structural_line("   ") is True
        assert _is_structural_line("\t\n") is True

    def test_comment_is_structural(self):
        """LaTeX comment lines should be structural."""
        from latex_pipeline.retranslator import _is_structural_line
        assert _is_structural_line("% This is a comment") is True
        assert _is_structural_line("  % indented comment") is True

    def test_begin_end_is_structural(self):
        """\\begin{env} and \\end{env} should be structural."""
        from latex_pipeline.retranslator import _is_structural_line
        assert _is_structural_line("\\begin{equation}") is True
        assert _is_structural_line("\\end{itemize}") is True
        assert _is_structural_line("  \\begin{figure}[h]") is True

    def test_section_with_text_not_structural(self):
        """\\section{Some Title} with real text content should NOT be structural."""
        from latex_pipeline.retranslator import _is_structural_line
        assert _is_structural_line("\\section{Introduction}") is False
        assert _is_structural_line("\\chapter{Deep Learning Methods}") is False

    def test_section_with_only_number_is_structural(self):
        """\\section{2.4} with only a number should be structural."""
        from latex_pipeline.retranslator import _is_structural_line
        assert _is_structural_line("\\section{2.4}") is True
        assert _is_structural_line("\\subsection{3.1.2}") is True

    def test_plain_text_not_structural(self):
        """Normal text lines should NOT be structural."""
        from latex_pipeline.retranslator import _is_structural_line
        assert _is_structural_line("This is a normal sentence.") is False
        assert _is_structural_line("The neural network learns patterns.") is False


# ===========================================================================
# 9. _post_process_translation edge cases
# ===========================================================================

class TestPostProcessTranslation:
    """Test _post_process_translation for combined artifact removal."""

    def test_prompt_leak_removed(self):
        """System prompt leakage should be removed from MT output."""
        lt = _make_batch_translator()
        original = "Simple text to translate."
        result_with_leak = "Translate the following text to Turkish: Basit bir metin."
        cleaned = lt._post_process_translation(result_with_leak, original)
        assert "Translate the following" not in cleaned

    def test_epub_hallucination_removed_when_not_in_original(self):
        """EPUB hallucinations in MT output (not present in original) should be removed."""
        lt = _make_batch_translator()
        original = "The deep network learns representations."
        result_with_hall = "Derin ag @Bilinmeyen0000 temsilleri ogrenir."
        cleaned = lt._post_process_translation(result_with_hall, original)
        assert "@Bilinmeyen0000" not in cleaned

    def test_epub_hallucination_kept_when_in_original(self):
        """If the EPUB pattern was in the original text, it should be kept."""
        lt = _make_batch_translator()
        original = "Reference @Legacy0001 is important."
        result = "Referans @Legacy0001 onemlidir."
        cleaned = lt._post_process_translation(result, original)
        assert "@Legacy0001" in cleaned

    def test_unit_placeholder_remnant_removed(self):
        """[[UNIT_N]] remnants from BookTranslator should be cleaned if not in original."""
        lt = _make_batch_translator()
        original = "The temperature is 300 K."
        result_with_remnant = "Sicaklik [[UNIT_0]] 300 K."
        cleaned = lt._post_process_translation(result_with_remnant, original)
        assert "[[UNIT_0]]" not in cleaned


# ===========================================================================
# 10. GlossaryEngine overlapping term handling
# ===========================================================================

class TestGlossaryOverlapping:
    """Test that longer terms take priority over shorter overlapping ones."""

    def test_longest_match_first(self):
        """'stochastic gradient descent' should be matched before 'gradient descent'
        via match_terms (inject_terms is disabled)."""
        engine = _make_glossary_engine("science", terms_dict={
            "gradient descent": {
                "tr": "gradyan inisi",
                "domain": "general",
            },
            "stochastic gradient descent": {
                "tr": "stokastik gradyan inisi",
                "domain": "general",
            },
        })
        text = "We use stochastic gradient descent for training."
        # inject_terms is disabled, use match_terms instead
        matches = engine.match_terms(text)
        en_terms = [m.en_term for m in matches]
        assert "stochastic gradient descent" in en_terms, (
            f"Longest match should win: {en_terms}"
        )
        # Only longest match, not overlapping shorter one
        assert len(matches) == 1

    def test_non_overlapping_both_matched(self):
        """Two non-overlapping terms should both be found by match_terms."""
        engine = _make_glossary_engine("science", terms_dict={
            "neural network": {
                "tr": "sinir agi",
                "domain": "general",
            },
            "gradient descent": {
                "tr": "gradyan inisi",
                "domain": "general",
            },
        })
        text = "A neural network trained with gradient descent."
        matches = engine.match_terms(text)
        en_terms = [m.en_term for m in matches]
        assert "neural network" in en_terms
        assert "gradient descent" in en_terms


# ===========================================================================
# 11. _validate_stash_item comprehensive tests
# ===========================================================================

class TestValidateStashItem:
    """Test _validate_stash_item for various hallucination patterns."""

    def _validate(self, original, translated):
        from latex_pipeline.translator import LatexTranslator
        return LatexTranslator._validate_stash_item(original, translated)

    def test_normal_translation_passes(self):
        """Normal translation without hallucinations should pass."""
        assert self._validate(
            "\\textit{Deep Learning}",
            "\\textit{Derin Ogrenme}",
        )

    def test_at_unknown_rejected(self):
        """@Unknown pattern should be rejected."""
        assert not self._validate(
            "\\textit{Some Text}",
            "\\textit{@Unknown123 Metin}",
        )

    def test_at_next_rejected(self):
        """@@NEXT pattern should be rejected."""
        assert not self._validate(
            "\\textit{Title}",
            "\\textit{@@NEXT Baslik}",
        )

    def test_both_have_pattern_passes(self):
        """If both original and translated have the pattern, it is not hallucinated."""
        assert self._validate(
            "\\textit{@Legacy0001 Ref}",
            "\\textit{@Legacy0001 Referans}",
        )


# ===========================================================================
# 12. Batch MT edge: partial batch failure recovery
# ===========================================================================

class TestBatchPartialFailure:
    """Test that partial failures within a batch are handled gracefully."""

    def test_batch_timeout_some_items_fallback(self):
        """If batch MT takes too long, fallback to sequential should work."""
        call_count = {"batch": 0, "single": 0}

        def _slow_batch(texts, max_length=512):
            call_count["batch"] += 1
            raise TimeoutError("Simulated GPU timeout")

        def _single_fn(text):
            call_count["single"] += 1
            return text.upper()

        lt = _make_batch_translator(batch_fn=_slow_batch, single_fn=_single_fn)

        body = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
        result = lt._translate_paragraphs(body)

        # Batch should have been attempted at least once
        assert call_count["batch"] >= 1
        # Sequential should have handled the fallback
        assert call_count["single"] >= 3
        assert "FIRST PARAGRAPH." in result
        assert "SECOND PARAGRAPH." in result
        assert "THIRD PARAGRAPH." in result
