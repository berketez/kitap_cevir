"""
Tests for two systematic translation quality issues:

1. Line-end hyphenation rejoining in converter.py
   - OCR / PDF extraction breaks words across lines with hyphens
   - "Fur-\\nthermore" must become "Furthermore"
   - Real compound words like "self-attention" must NOT be touched

2. Academic citation / author name stashing in translator.py
   - Inline citations must be stashed so MT model does not translate names
   - "Hosterman et al." must survive translation intact
   - "(LeMarc, 2015)" must survive translation intact
"""

import re
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from latex_pipeline.converter import MarkdownToLatex
from latex_pipeline.config import PipelineConfig
from latex_pipeline.translator import (
    _AUTHOR_ETAL_RE,
    _AUTHOR_YEAR_RE,
    _PAREN_REF_RE,
)


# ---------------------------------------------------------------------------
# 1. Hyphenation tests (converter._preprocess_markdown)
# ---------------------------------------------------------------------------

class TestHyphenationFix:
    """Verify that line-end hyphenation is rejoined in preprocessing."""

    def _preprocess(self, text: str) -> str:
        """Helper: run _preprocess_markdown on text."""
        config = PipelineConfig()
        converter = MarkdownToLatex(config)
        return converter._preprocess_markdown(text)

    def test_furthermore(self):
        result = self._preprocess("Fur-\nthermore, this is important.")
        assert "Furthermore" in result, f"Expected 'Furthermore', got: {result}"

    def test_approximation(self):
        result = self._preprocess("The approxi-\nmation error is small.")
        assert "approximation" in result, f"Expected 'approximation', got: {result}"

    def test_convolutional(self):
        result = self._preprocess("A convo-\nlutional neural network.")
        assert "convolutional" in result, f"Expected 'convolutional', got: {result}"

    def test_pretraining(self):
        result = self._preprocess("The pre-\ntraining phase is long.")
        assert "pretraining" in result, f"Expected 'pretraining', got: {result}"

    def test_self_attention_preserved(self):
        """Compound word with no newline must NOT be altered."""
        result = self._preprocess("The self-attention mechanism works well.")
        assert "self-attention" in result, (
            f"'self-attention' should be preserved, got: {result}"
        )

    def test_pre_training_preserved(self):
        """Compound word without newline must NOT be altered."""
        result = self._preprocess("We use pre-training for this model.")
        assert "pre-training" in result, (
            f"'pre-training' should be preserved, got: {result}"
        )

    def test_state_of_the_art_preserved(self):
        """Multi-hyphen compound without newline must NOT be altered."""
        result = self._preprocess("This is a state-of-the-art method.")
        assert "state-of-the-art" in result, (
            f"'state-of-the-art' should be preserved, got: {result}"
        )

    def test_uppercase_hyphen_preserved(self):
        """Uppercase letter before hyphen+newline should NOT be rejoined.
        This avoids false merges like acronyms: 'A-\\nB' should stay."""
        result = self._preprocess("Use the A-\nB method.")
        assert "A-" in result, (
            f"Uppercase hyphenation should not be rejoined, got: {result}"
        )

    def test_multi_space_hyphenation(self):
        """Hyphen followed by multiple spaces (OCR artifact) should be rejoined."""
        result = self._preprocess("The approxi-   mation is good.")
        assert "approximation" in result, f"Expected 'approximation', got: {result}"

    def test_real_sentence_with_hyphenation(self):
        """Full sentence with multiple hyphenation breaks."""
        text = (
            "Fur-\nthermore, convo-\nlutional neural networks\n"
            "have self-attention mechanisms."
        )
        result = self._preprocess(text)
        assert "Furthermore" in result
        assert "convolutional" in result
        assert "self-attention" in result


# ---------------------------------------------------------------------------
# 2. Citation / author name stashing tests (translator patterns)
# ---------------------------------------------------------------------------

class TestCitationStash:
    """Verify that academic citation patterns are matched for stashing."""

    # --- _AUTHOR_ETAL_RE ---

    def test_hospedales_et_al(self):
        m = _AUTHOR_ETAL_RE.search("Hosterman et al.")
        assert m is not None, "Should match 'Hosterman et al.'"
        assert m.group(0) == "Hosterman et al."

    def test_lecun_et_al(self):
        """CamelCase surname: LeMarc should be matched."""
        m = _AUTHOR_ETAL_RE.search("LeMarc et al.")
        assert m is not None, "Should match 'LeMarc et al.'"
        assert m.group(0) == "LeMarc et al."

    def test_goodfellow_et_al_with_year(self):
        """'Author et al. (YYYY)' should be captured as one unit."""
        m = _AUTHOR_ETAL_RE.search("Goodacre et al. (2016)")
        assert m is not None, "Should match 'Goodacre et al. (2016)'"
        assert "(2016)" in m.group(0)

    def test_hospedales_et_al_with_year(self):
        m = _AUTHOR_ETAL_RE.search("Hosterman et al. (2020)")
        assert m is not None
        assert "(2020)" in m.group(0)

    def test_two_authors_et_al(self):
        m = _AUTHOR_ETAL_RE.search("Kingma and Welling et al.")
        assert m is not None
        assert "Kingma and Welling et al." == m.group(0)

    # --- _AUTHOR_YEAR_RE ---

    def test_lecun_year(self):
        """Bare 'Author (YYYY)' without et al."""
        m = _AUTHOR_YEAR_RE.search("LeMarc (2015)")
        assert m is not None, "Should match 'LeMarc (2015)'"
        assert m.group(0) == "LeMarc (2015)"

    def test_krizhevsky_year(self):
        m = _AUTHOR_YEAR_RE.search("Kovarsky (2012)")
        assert m is not None
        assert m.group(0) == "Kovarsky (2012)"

    # --- _PAREN_REF_RE ---

    def test_paren_author_year(self):
        m = _PAREN_REF_RE.search("(Smith, 2020)")
        assert m is not None
        assert m.group(0) == "(Smith, 2020)"

    def test_paren_lecun_year(self):
        """CamelCase inside parenthetical ref."""
        m = _PAREN_REF_RE.search("(LeMarc, 2015)")
        assert m is not None
        assert m.group(0) == "(LeMarc, 2015)"

    def test_paren_et_al(self):
        m = _PAREN_REF_RE.search("(Hosterman et al., 2020)")
        assert m is not None
        assert m.group(0) == "(Hosterman et al., 2020)"

    def test_paren_two_authors(self):
        m = _PAREN_REF_RE.search("(Kovarsky and Hartwell, 2012)")
        assert m is not None
        assert m.group(0) == "(Kovarsky and Hartwell, 2012)"

    def test_paren_multi_ref(self):
        """Multiple refs separated by semicolons."""
        text = "(Smith et al., 2020; Jones, 2019)"
        m = _PAREN_REF_RE.search(text)
        assert m is not None
        assert m.group(0) == text

    def test_paren_degroot_obrien(self):
        """Unusual surnames: DeVaren, O'Brien."""
        m = _PAREN_REF_RE.search("(DeVaren and O'Brien, 2018)")
        assert m is not None
        assert "DeVaren" in m.group(0)

    # --- Negative tests ---

    def test_no_match_normal_text(self):
        """Normal text should not be matched."""
        assert _AUTHOR_ETAL_RE.search("self-attention is important") is None
        assert _PAREN_REF_RE.search("self-attention is important") is None
        assert _AUTHOR_YEAR_RE.search("self-attention is important") is None

    def test_no_match_lowercase(self):
        """Lowercase words should not trigger author matching."""
        assert _AUTHOR_ETAL_RE.search("the hospital et al.") is None
        assert _PAREN_REF_RE.search("(the hospital, 2020)") is None


# ---------------------------------------------------------------------------
# 3. Integration: stash round-trip test
# ---------------------------------------------------------------------------

class TestStashRoundtrip:
    """Verify that citations survive the stash -> (translate) -> restore cycle."""

    def _make_translator(self):
        """Create a LatexTranslator with no actual MT engine."""
        from latex_pipeline.translator import LatexTranslator
        config = PipelineConfig()
        return LatexTranslator(config)

    def test_hospedales_stashed(self):
        """'Hosterman et al.' should be replaced with a placeholder."""
        translator = self._make_translator()
        text = "As shown by Hosterman et al. the method works."
        result, stash = translator._stash_protected(text)
        # "Hosterman et al." should be in the stash
        assert any("Hosterman et al." in s for s in stash), (
            f"'Hosterman et al.' not found in stash: {stash}"
        )
        # The placeholder should be in the result
        assert "<<LATEX_" in result, f"No placeholder in result: {result}"

    def test_lecun_paren_stashed(self):
        """'(LeMarc, 2015)' should be replaced with a placeholder."""
        translator = self._make_translator()
        text = "The method was proposed by (LeMarc, 2015)."
        result, stash = translator._stash_protected(text)
        assert any("LeMarc" in s for s in stash), (
            f"'(LeMarc, 2015)' not found in stash: {stash}"
        )

    def test_lecun_bare_year_stashed(self):
        """'LeMarc (2015)' should be replaced with a placeholder."""
        translator = self._make_translator()
        text = "As LeMarc (2015) showed, deep learning works."
        result, stash = translator._stash_protected(text)
        assert any("LeMarc" in s for s in stash), (
            f"'LeMarc (2015)' not found in stash: {stash}"
        )

    def test_restore_preserves_citation(self):
        """Full round-trip: stash + restore should preserve citations exactly."""
        translator = self._make_translator()
        text = "Hosterman et al. showed that (LeMarc, 2015) is important."
        stashed, stash_list = translator._stash_protected(text)
        # Simulate identity translation (no actual MT)
        restored = stashed
        for idx in range(len(stash_list) - 1, -1, -1):
            restored = restored.replace(f"<<LATEX_{idx}>>", stash_list[idx])
        assert "Hosterman et al." in restored
        assert "(LeMarc, 2015)" in restored
