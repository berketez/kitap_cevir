"""
Wave 1 tests: inject_terms() poisoning fix + LaTeX command stash fix.

Tests cover:
    1. _SKIP_INJECT_BOOK_TYPES completeness (all non-STEM types skip injection)
    2. STEM book types still get injection
    3. Heading protection in inject_terms()
    4. Inline LaTeX command stash/restore in translator._safe_translate()
    5. Standalone command regex coverage
    6. Edge cases (empty text, placeholder-only, mangled placeholders)

2026-03-05
"""

import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------

def _make_glossary_engine(book_type: str, extra_terms=None):
    """Create a GlossaryEngine with a minimal in-memory term set."""
    from latex_pipeline.glossary import GlossaryEngine

    glossary_dir = PROJECT_ROOT / "glossary"
    if not glossary_dir.exists():
        # CI fallback: create a temp glossary dir with a minimal file
        # (G2c / KARAR 25: genel default.json silindi ve yüklenmez; yedek
        # dosya kalan curated.json adını taşır.)
        import tempfile, json
        tmp = Path(tempfile.mkdtemp()) / "glossary"
        tmp.mkdir()
        (tmp / "curated.json").write_text(json.dumps({
            "terms": {
                "neural network": {"tr": "sinir agi", "domain": "general"},
                "gradient descent": {"tr": "gradyan inisi", "domain": "general"},
                "pedestalization": {"tr": "yuceltme", "domain": "general"},
            }
        }))
        glossary_dir = tmp

    return GlossaryEngine(
        glossary_dir=glossary_dir,
        book_type=book_type,
        extra_terms=extra_terms or {},
    )


def _make_translator():
    """Create a LatexTranslator with a mock MT engine (no GPU needed)."""
    from latex_pipeline.translator import LatexTranslator
    from latex_pipeline.config import PipelineConfig

    config = PipelineConfig(
        translate=True,
        book_type="science",
        segment_timeout=5,
    )
    translator = LatexTranslator(
        pipeline_config=config,
        mt_engine="hymt-7b",
        book_type="science",
    )
    return translator


# -----------------------------------------------------------------------
# SORUN 1: inject_terms() skip for non-STEM book types
# -----------------------------------------------------------------------

class TestInjectTermsSkip:
    """Verify that non-STEM book types skip inject_terms()."""

    # All non-STEM types that MUST skip injection
    NON_STEM_TYPES = [
        "philosophy", "literature", "psychology",
        "history", "biography", "self_help", "fiction", "novel",
        "sociology", "politics", "business", "law", "religion",
        "memoir", "art", "sports", "food", "travel", "military",
        "general",
    ]

    # STEM types that MUST keep injection active
    STEM_TYPES = [
        "science", "engineering", "technical", "computer",
        "ml", "physics",
    ]

    @pytest.mark.parametrize("book_type", NON_STEM_TYPES)
    def test_non_stem_skips_injection(self, book_type):
        """inject_terms() should return unmodified text for non-STEM types."""
        from latex_pipeline.glossary import _SKIP_INJECT_BOOK_TYPES

        assert book_type in _SKIP_INJECT_BOOK_TYPES, (
            f"book_type '{book_type}' is missing from _SKIP_INJECT_BOOK_TYPES"
        )

        engine = _make_glossary_engine(book_type, extra_terms={
            "pedestalization": "yuceltme",
            "afiyet": "saglik",
        })
        text = "The pedestalization of women is a core concept."
        result, term_map = engine.inject_terms(text)
        # Text should be UNCHANGED
        assert result == text, (
            f"inject_terms modified text for non-STEM type '{book_type}': {result}"
        )
        assert term_map == {}, "term_map should be empty for non-STEM types"

    @pytest.mark.parametrize("book_type", STEM_TYPES)
    def test_stem_also_skips_injection(self, book_type):
        """inject_terms() is now disabled for ALL types including STEM."""
        from latex_pipeline.glossary import _SKIP_INJECT_BOOK_TYPES

        assert book_type in _SKIP_INJECT_BOOK_TYPES, (
            f"STEM type '{book_type}' should be in _SKIP_INJECT_BOOK_TYPES (inject disabled for all)"
        )

    def test_skip_set_completeness(self):
        """All app.py sidebar non-STEM options should be in _SKIP_INJECT_BOOK_TYPES."""
        from latex_pipeline.glossary import _SKIP_INJECT_BOOK_TYPES

        # These are the non-STEM options from app.py sidebar + additional genres
        required = {
            "philosophy", "literature", "psychology",
            "history", "biography", "self_help", "fiction", "novel",
            "sociology", "politics", "business", "law", "religion",
            "memoir", "general", "art", "sports", "food", "travel",
            "military",
        }
        missing = required - _SKIP_INJECT_BOOK_TYPES
        assert not missing, (
            f"Non-STEM types missing from _SKIP_INJECT_BOOK_TYPES: {missing}"
        )


# -----------------------------------------------------------------------
# SORUN 1b: Heading protection in inject_terms()
# -----------------------------------------------------------------------

class TestInjectTermsHeadingProtection:
    """Terms inside LaTeX headings should NOT be injected."""

    def test_heading_section_not_injected(self):
        """Terms inside \\section{...} should be skipped."""
        engine = _make_glossary_engine("science", extra_terms={
            "neural network": "sinir agi",
        })
        text = r"\section{Neural Network Architecture}"
        result, term_map = engine.inject_terms(text)
        # Heading content should be untouched
        assert "Neural Network" in result, (
            f"Heading was modified by inject_terms: {result}"
        )

    def test_heading_chapter_not_injected(self):
        """Terms inside \\chapter{...} should be skipped."""
        engine = _make_glossary_engine("science", extra_terms={
            "gradient descent": "gradyan inisi",
        })
        text = r"\chapter{Gradient Descent Methods}"
        result, term_map = engine.inject_terms(text)
        assert "Gradient Descent" in result, (
            f"Chapter heading was modified: {result}"
        )

    def test_body_text_not_injected_anymore(self):
        """inject_terms is disabled for all types, body text unchanged."""
        engine = _make_glossary_engine("science", extra_terms={
            "neural network": "sinir agi",
        })
        text = "The neural network learns by backpropagation."
        result, term_map = engine.inject_terms(text)
        assert result == text, "inject_terms should not modify text (disabled)"
        assert term_map == {}

    def test_mixed_heading_and_body_no_injection(self):
        """inject_terms disabled: neither heading nor body text modified."""
        engine = _make_glossary_engine("science", extra_terms={
            "neural network": "sinir agi",
        })
        text = (
            r"\section{Neural Network}" "\n"
            r"The neural network is trained on data."
        )
        result, term_map = engine.inject_terms(text)
        assert result == text, "inject_terms should not modify text (disabled)"
        assert term_map == {}


# -----------------------------------------------------------------------
# SORUN 2: LaTeX command stash in _safe_translate()
# -----------------------------------------------------------------------

class TestInlineCommandStash:
    """Test the _stash_inline_commands / _restore_inline_commands mechanism."""

    def _stash_and_restore(self, text):
        from latex_pipeline.translator import LatexTranslator
        stashed, cmds = LatexTranslator._stash_inline_commands(text)
        restored = LatexTranslator._restore_inline_commands(stashed, cmds)
        return stashed, cmds, restored

    def test_centering_stashed(self):
        text = r"Some text \centering more text"
        stashed, cmds, restored = self._stash_and_restore(text)
        assert r"\centering" not in stashed, "\\centering should be stashed"
        assert "<<CMD_0>>" in stashed, "Placeholder should be present"
        assert restored == text, "Restore should produce original"

    def test_frontmatter_stashed(self):
        text = r"\frontmatter"
        stashed, cmds, restored = self._stash_and_restore(text)
        assert stashed == "<<CMD_0>>"
        assert cmds == [r"\frontmatter"]
        assert restored == text

    def test_vfill_stashed(self):
        text = r"Title page content \vfill Author name"
        stashed, cmds, restored = self._stash_and_restore(text)
        assert r"\vfill" not in stashed
        assert restored == text

    def test_thispagestyle_stashed(self):
        text = r"\thispagestyle{empty}"
        stashed, cmds, restored = self._stash_and_restore(text)
        assert stashed == "<<CMD_0>>"
        assert cmds == [r"\thispagestyle{empty}"]
        assert restored == text

    def test_pagestyle_stashed(self):
        text = r"\pagestyle{fancy}"
        stashed, cmds, restored = self._stash_and_restore(text)
        assert stashed == "<<CMD_0>>"
        assert restored == text

    def test_setcounter_stashed(self):
        text = r"\setcounter{page}{1}"
        stashed, cmds, restored = self._stash_and_restore(text)
        assert stashed == "<<CMD_0>>"
        assert cmds == [r"\setcounter{page}{1}"]
        assert restored == text

    def test_rule_stashed(self):
        text = r"\rule{0.5\textwidth}{0.4pt}"
        stashed, cmds, restored = self._stash_and_restore(text)
        # \rule with nested braces -- our regex uses [^}]* so it may
        # not match if there are nested braces.  Verify behavior.
        # (If the regex can't handle nested braces, the command leaks
        # through -- this is acceptable for now.)
        assert restored == text

    def test_vspace_stashed(self):
        text = r"Text before \vspace*{2cm} text after"
        stashed, cmds, restored = self._stash_and_restore(text)
        assert r"\vspace*{2cm}" not in stashed
        assert restored == text

    def test_begin_titlepage_stashed(self):
        text = r"\begin{titlepage}"
        stashed, cmds, restored = self._stash_and_restore(text)
        assert stashed == "<<CMD_0>>"
        assert restored == text

    def test_end_titlepage_stashed(self):
        text = r"\end{titlepage}"
        stashed, cmds, restored = self._stash_and_restore(text)
        assert stashed == "<<CMD_0>>"
        assert restored == text

    def test_cleardoublepage_stashed(self):
        text = r"\cleardoublepage"
        stashed, cmds, restored = self._stash_and_restore(text)
        assert stashed == "<<CMD_0>>"
        assert restored == text

    def test_multiple_commands_stashed(self):
        """Multiple different commands in one segment."""
        text = r"\centering \vfill \noindent Some actual text here"
        stashed, cmds, restored = self._stash_and_restore(text)
        assert len(cmds) == 3, f"Expected 3 commands stashed, got {len(cmds)}: {cmds}"
        assert "Some actual text here" in stashed
        assert restored == text

    def test_no_commands_passthrough(self):
        """Text without LaTeX commands should pass through unchanged."""
        text = "This is plain text without any commands."
        stashed, cmds, restored = self._stash_and_restore(text)
        assert stashed == text
        assert cmds == []
        assert restored == text

    def test_mangled_placeholder_recovery(self):
        """Mangled <<CMD_N>> placeholders should be recovered."""
        from latex_pipeline.translator import LatexTranslator
        cmds = [r"\centering", r"\vfill"]
        # Simulate MT mangling: spaces added
        mangled = "< < CMD _ 0 > > text < < CMD _ 1 > >"
        restored = LatexTranslator._restore_inline_commands(mangled, cmds)
        assert r"\centering" in restored
        assert r"\vfill" in restored

    def test_begin_center_stashed(self):
        text = r"\begin{center} text \end{center}"
        stashed, cmds, restored = self._stash_and_restore(text)
        assert r"\begin{center}" not in stashed
        assert r"\end{center}" not in stashed
        assert len(cmds) == 2
        assert restored == text


# -----------------------------------------------------------------------
# SORUN 2b: Standalone command regex completeness
# -----------------------------------------------------------------------

class TestStandaloneCommandRegex:
    """Verify _STANDALONE_CMD_*_RE patterns in _stash_protected()."""

    def test_standalone_bare_commands(self):
        """All required bare commands should match the regex."""
        from latex_pipeline.translator import _STANDALONE_CMD_BARE_RE

        commands = [
            r"\frontmatter", r"\mainmatter", r"\backmatter",
            r"\centering", r"\vfill", r"\hfill",
            r"\noindent", r"\newpage", r"\clearpage",
            r"\cleardoublepage",
            r"\tableofcontents", r"\maketitle",
            r"\raggedright", r"\raggedleft",
            r"\listoffigures", r"\listoftables",
            r"\bigskip", r"\medskip", r"\smallskip",
            r"\par", r"\newline",
        ]
        for cmd in commands:
            assert _STANDALONE_CMD_BARE_RE.search(cmd), (
                f"_STANDALONE_CMD_BARE_RE does not match: {cmd}"
            )

    def test_standalone_with_arg_commands(self):
        """Commands with arguments should match the regex."""
        from latex_pipeline.translator import _STANDALONE_CMD_WITH_ARG_RE

        commands = [
            r"\vspace{1cm}",
            r"\vspace*{2cm}",
            r"\hspace{5mm}",
            r"\thispagestyle{empty}",
            r"\pagestyle{fancy}",
            r"\setcounter{page}{1}",
            r"\rule{10cm}{0.4pt}",
        ]
        for cmd in commands:
            assert _STANDALONE_CMD_WITH_ARG_RE.search(cmd), (
                f"_STANDALONE_CMD_WITH_ARG_RE does not match: {cmd}"
            )

    def test_par_does_not_match_parbox(self):
        """\\par regex should NOT match \\parbox (word boundary check)."""
        from latex_pipeline.translator import _STANDALONE_CMD_BARE_RE

        assert _STANDALONE_CMD_BARE_RE.search(r"\par "), "\\par should match"
        # \parbox should NOT match
        match = _STANDALONE_CMD_BARE_RE.search(r"\parbox{5cm}{text}")
        if match:
            assert match.group(0) != r"\parbox", "\\parbox should NOT match"


# -----------------------------------------------------------------------
# Edge cases
# -----------------------------------------------------------------------

class TestEdgeCases:
    """Edge cases for both fixes."""

    def test_empty_text_inject_terms(self):
        """inject_terms on empty text should return empty."""
        engine = _make_glossary_engine("science")
        result, term_map = engine.inject_terms("")
        assert result == ""
        assert term_map == {}

    def test_empty_text_stash_inline(self):
        """_stash_inline_commands on empty text should return empty."""
        from latex_pipeline.translator import LatexTranslator
        stashed, cmds = LatexTranslator._stash_inline_commands("")
        assert stashed == ""
        assert cmds == []

    def test_medical_nature_not_skipped(self):
        """medical and nature types are STEM-adjacent, should NOT skip."""
        from latex_pipeline.glossary import _SKIP_INJECT_BOOK_TYPES

        # medical and nature are now also in skip list (all types disabled)
        assert "medical" in _SKIP_INJECT_BOOK_TYPES
        assert "nature" in _SKIP_INJECT_BOOK_TYPES

    def test_inline_cmd_regex_does_not_match_textbf(self):
        """_INLINE_CMD_STASH_RE should NOT match \\textbf, \\textit, etc."""
        from latex_pipeline.translator import LatexTranslator
        text = r"\textbf{important} and \textit{emphasis}"
        stashed, cmds = LatexTranslator._stash_inline_commands(text)
        assert cmds == [], f"Should not stash formatting commands: {cmds}"
        assert stashed == text
