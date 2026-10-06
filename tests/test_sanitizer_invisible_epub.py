"""Tests for sanitizer invisible character cleanup and EPUB artifact removal.

Session 8 (2026-03-04):
- Invisible/zero-width Unicode character cleanup (runs BEFORE stash)
- EPUB conversion artifact removal (@@placeholders, kindle URLs, etc.)

These features prevent stash regex breakage and improve translation quality
for EPUB-sourced LaTeX documents.
"""

import sys
import os
import re

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from latex_pipeline.sanitizer import LatexSanitizer, SanitizeReport


# ======================================================================
# 1. Invisible Character Cleanup Tests
# ======================================================================


class TestInvisibleCharCleanup:
    """Tests for _clean_invisible_chars (Phase 0, before stash)."""

    def _sanitize(self, text: str) -> tuple:
        san = LatexSanitizer()
        return san.sanitize(text)

    def test_bom_removed(self):
        """BOM (U+FEFF) at start of document should be removed."""
        body = "\ufeffThis is a test document."
        cleaned, report = self._sanitize(body)
        assert "\ufeff" not in cleaned
        assert "This is a test document." in cleaned
        assert report.invisible_chars_cleaned >= 1

    def test_zwsp_removed(self):
        """Zero-width space (U+200B) should be removed."""
        body = "Hello\u200Bworld"
        cleaned, report = self._sanitize(body)
        assert "\u200b" not in cleaned
        assert "Helloworld" in cleaned
        assert report.invisible_chars_cleaned >= 1

    def test_zwnj_removed(self):
        """Zero-width non-joiner (U+200C) should be removed."""
        body = "test\u200ctext"
        cleaned, report = self._sanitize(body)
        assert "\u200c" not in cleaned
        assert "testtext" in cleaned

    def test_zwj_removed(self):
        """Zero-width joiner (U+200D) should be removed."""
        body = "test\u200dtext"
        cleaned, report = self._sanitize(body)
        assert "\u200d" not in cleaned
        assert "testtext" in cleaned

    def test_nbsp_to_space(self):
        """NBSP (U+00A0) should become normal space."""
        body = "word1\u00a0word2"
        cleaned, report = self._sanitize(body)
        assert "\u00a0" not in cleaned
        assert "word1 word2" in cleaned
        assert report.invisible_chars_cleaned >= 1

    def test_soft_hyphen_removed(self):
        """Soft hyphen (U+00AD) should be removed."""
        body = "hyphe\u00adnation"
        cleaned, report = self._sanitize(body)
        assert "\u00ad" not in cleaned
        assert "hyphenation" in cleaned

    def test_bidi_marks_removed(self):
        """Left-to-right and right-to-left marks should be removed."""
        body = "text\u200ewith\u200fmarks"
        cleaned, report = self._sanitize(body)
        assert "\u200e" not in cleaned
        assert "\u200f" not in cleaned
        assert "textwithmarks" in cleaned
        assert report.invisible_chars_cleaned >= 2

    def test_word_joiner_removed(self):
        """Word joiner (U+2060) should be removed."""
        body = "no\u2060break"
        cleaned, report = self._sanitize(body)
        assert "\u2060" not in cleaned
        assert "nobreak" in cleaned

    def test_line_separator_to_newline(self):
        """Line separator (U+2028) should become newline."""
        body = "line1\u2028line2"
        cleaned, report = self._sanitize(body)
        assert "\u2028" not in cleaned
        assert "line1\nline2" in cleaned

    def test_paragraph_separator_to_newline(self):
        """Paragraph separator (U+2029) should become newline."""
        body = "para1\u2029para2"
        cleaned, report = self._sanitize(body)
        assert "\u2029" not in cleaned
        assert "para1\npara2" in cleaned

    def test_multiple_invisible_chars(self):
        """Multiple different invisible chars in one document."""
        body = "\ufeff\u200bHello\u00a0World\u200e\u200f\u00adEnd"
        cleaned, report = self._sanitize(body)
        assert "\ufeff" not in cleaned
        assert "\u200b" not in cleaned
        assert "\u00a0" not in cleaned
        assert "\u200e" not in cleaned
        assert "\u200f" not in cleaned
        assert "\u00ad" not in cleaned
        assert "Hello World" in cleaned
        assert "End" in cleaned
        assert report.invisible_chars_cleaned >= 6

    def test_no_invisible_chars_noop(self):
        """Clean text should pass through unchanged, zero count."""
        body = "This is clean text with $E=mc^2$ and normal spaces."
        cleaned, report = self._sanitize(body)
        assert report.invisible_chars_cleaned == 0

    def test_invisible_char_inside_latex_command(self):
        r"""Invisible char splitting \begin should be cleaned BEFORE stash.

        This is the critical case: U+200B inside \\begin{equation} would
        prevent the stash regex from matching the math environment.
        """
        # \be<ZWSP>gin{equation} -- would break _MATH_ENV_RE
        body = "\\be\u200bgin{equation}\nE = mc^2\n\\end{equation}"
        cleaned, report = self._sanitize(body)
        assert "\u200b" not in cleaned
        # After invisible char removal, stash should work and math should be preserved
        assert "E = mc^2" in cleaned
        assert "\\begin{equation}" in cleaned
        assert "\\end{equation}" in cleaned

    def test_nbsp_in_latex_ref(self):
        r"""NBSP inside \ref{} should become normal space (then stash works)."""
        body = "See \\ref{fig:\u00a0test} for details."
        cleaned, report = self._sanitize(body)
        assert "\u00a0" not in cleaned
        assert report.invisible_chars_cleaned >= 1

    def test_idempotent(self):
        """Running cleanup twice produces same result."""
        san = LatexSanitizer()
        body = "\ufeff\u200bHello\u00a0World\u200e"
        cleaned1, _ = san.sanitize(body)
        cleaned2, report2 = san.sanitize(cleaned1)
        assert cleaned1 == cleaned2
        assert report2.invisible_chars_cleaned == 0


# ======================================================================
# 2. EPUB Artifact Cleanup Tests
# ======================================================================


class TestEpubArtifactCleanup:
    """Tests for _clean_epub_artifacts."""

    def _sanitize(self, text: str) -> tuple:
        san = LatexSanitizer()
        return san.sanitize(text)

    def test_at_placeholder_next(self):
        """@@NEXT placeholder should be removed."""
        body = "Some text @@NEXT more text"
        cleaned, report = self._sanitize(body)
        assert "@@NEXT" not in cleaned
        assert "Some text" in cleaned
        assert "more text" in cleaned
        assert report.epub_artifacts_cleaned >= 1

    def test_at_placeholder_pagebreak(self):
        """@@PAGEBREAK placeholder should be removed."""
        body = "Line before\n@@PAGEBREAK\nLine after"
        cleaned, report = self._sanitize(body)
        assert "@@PAGEBREAK" not in cleaned
        assert "Line before" in cleaned
        assert "Line after" in cleaned

    def test_at_placeholder_unknown_with_digits(self):
        """@@UNKNOWN0001@@ should be removed."""
        body = "Text @@UNKNOWN0001@@ more text"
        cleaned, report = self._sanitize(body)
        assert "@@UNKNOWN0001@@" not in cleaned
        assert "Text" in cleaned

    def test_named_placeholder_bilinmeyen(self):
        """@Bilinmeyen0000 should be removed."""
        body = "Reference @Bilinmeyen0000 here"
        cleaned, report = self._sanitize(body)
        assert "@Bilinmeyen0000" not in cleaned
        assert "Reference" in cleaned
        assert "here" in cleaned
        assert report.epub_artifacts_cleaned >= 1

    def test_named_placeholder_unknown(self):
        """@Unknown123 should be removed."""
        body = "See @Unknown123 for details"
        cleaned, report = self._sanitize(body)
        assert "@Unknown123" not in cleaned

    def test_kindle_embed_url(self):
        """kindle:embed:XXXX should be replaced with [resim]."""
        body = "Image here: kindle:embed:0001?mime=image/jpg in text"
        cleaned, report = self._sanitize(body)
        assert "kindle:embed" not in cleaned
        assert "\\textit{[resim]}" in cleaned
        assert report.epub_artifacts_cleaned >= 1

    def test_epub_internal_link_standalone(self):
        """Standalone EPUB internal links should be removed."""
        body = "See part0042.html#ch7-sec3 for details"
        cleaned, report = self._sanitize(body)
        assert "part0042.html#ch7-sec3" not in cleaned
        assert "See" in cleaned
        assert "for details" in cleaned

    def test_epub_internal_link_inside_href_preserved(self):
        r"""EPUB links inside \href{} should NOT be removed."""
        body = r"\href{part0042.html#ch7-sec3}{Chapter 7}"
        cleaned, report = self._sanitize(body)
        # The link is inside \href{...}, the negative lookbehind should protect it.
        # Note: "Chapter 7" may become "Bolum~7" due to label normalization --
        # that is expected.  The key assertion is that \href structure is intact.
        assert r"\href{part0042.html#ch7-sec3}" in cleaned

    def test_duplicate_section_chapter(self):
        r"""Duplicate \section{Title}\n\chapter{Title} should keep only \chapter."""
        body = "\\section{Introduction}\n\\chapter{Introduction}\nContent here."
        cleaned, report = self._sanitize(body)
        assert "\\chapter{Introduction}" in cleaned
        assert "\\section{Introduction}" not in cleaned
        assert "Content here." in cleaned
        assert report.epub_artifacts_cleaned >= 1

    def test_duplicate_section_chapter_star(self):
        r"""Duplicate \section*{Title}\n\chapter*{Title} should keep only \chapter*."""
        body = "\\section*{Preface}\n\\chapter*{Preface}\nSome text."
        cleaned, report = self._sanitize(body)
        # The regex handles starred variants
        assert "\\chapter{Preface}" in cleaned or "\\chapter*{Preface}" in cleaned
        assert "Some text." in cleaned

    def test_no_epub_artifacts_noop(self):
        """Clean LaTeX with no EPUB artifacts should pass through unchanged."""
        body = (
            "\\chapter{Introduction}\n"
            "This is clean LaTeX with $E=mc^2$ and no EPUB junk.\n"
        )
        cleaned, report = self._sanitize(body)
        assert report.epub_artifacts_cleaned == 0
        assert "\\chapter{Introduction}" in cleaned

    def test_multiple_epub_artifacts(self):
        """Multiple different EPUB artifacts in one document."""
        body = (
            "@@NEXT\n"
            "Some text @Bilinmeyen0000 more text\n"
            "Image: kindle:embed:0001?mime=image/png\n"
            "See part0010.html#sec2 for details\n"
            "Real content here.\n"
        )
        cleaned, report = self._sanitize(body)
        assert "@@NEXT" not in cleaned
        assert "@Bilinmeyen0000" not in cleaned
        assert "kindle:embed" not in cleaned
        assert "part0010.html#sec2" not in cleaned
        assert "Real content here." in cleaned
        assert report.epub_artifacts_cleaned >= 4

    def test_idempotent_epub(self):
        """Running EPUB cleanup twice produces same result."""
        san = LatexSanitizer()
        body = "@@NEXT @Unknown123 kindle:embed:test123 part0001.html#ch1"
        cleaned1, _ = san.sanitize(body)
        cleaned2, report2 = san.sanitize(cleaned1)
        assert cleaned1 == cleaned2
        assert report2.epub_artifacts_cleaned == 0


# ======================================================================
# 3. Integration: Invisible + EPUB + Existing Features
# ======================================================================


class TestIntegration:
    """Integration tests: invisible chars + EPUB artifacts + math preservation."""

    def _sanitize(self, text: str) -> tuple:
        san = LatexSanitizer()
        return san.sanitize(text)

    def test_invisible_chars_dont_break_math_stash(self):
        r"""Invisible chars near math environments must be cleaned first.

        If ZWSP sits between \begin and {equation}, the stash regex fails
        and the equation content gets modified by later passes.
        """
        body = (
            "\ufeff\\begin{equation}\n"
            "F = ma\n"
            "\\end{equation}\n"
            "Text with Figure 1 reference.\n"
        )
        cleaned, report = self._sanitize(body)
        assert "F = ma" in cleaned
        assert "\\begin{equation}" in cleaned
        # BOM should be removed
        assert "\ufeff" not in cleaned
        assert report.invisible_chars_cleaned >= 1

    def test_epub_artifacts_with_html_tags(self):
        """EPUB artifacts combined with HTML tag artifacts."""
        body = (
            "@@NEXT\n"
            "<div>Some HTML</div>\n"
            "@Unknown999\n"
            "Real content.\n"
        )
        cleaned, report = self._sanitize(body)
        assert "@@NEXT" not in cleaned
        assert "@Unknown999" not in cleaned
        assert "<div>" not in cleaned
        assert "Real content." in cleaned

    def test_epub_artifacts_with_prompt_leaks(self):
        """EPUB artifacts combined with prompt leak patterns."""
        body = (
            "@@PAGEBREAK\n"
            "Translate from English to Turkish.\n"
            "This is real content.\n"
        )
        cleaned, report = self._sanitize(body)
        assert "@@PAGEBREAK" not in cleaned
        assert "Translate from" not in cleaned
        assert "real content" in cleaned

    def test_full_epub_document_simulation(self):
        """Simulated EPUB-sourced LaTeX with all artifact types."""
        body = (
            "\ufeff"  # BOM at start
            "\\chapter{Introduction}\n"
            "\n"
            "\\section{Introduction}\n"
            "\\chapter{Introduction}\n"  # duplicate heading
            "\n"
            "This\u00a0is\u200b the\u00ad introduction.\n"  # invisible chars
            "\n"
            "@@NEXT\n"  # EPUB placeholder
            "\n"
            "See @Bilinmeyen0000 for the image.\n"  # unknown ref
            "\n"
            "\\begin{equation}\n"
            "E = mc^2\n"
            "\\end{equation}\n"
            "\n"
            "Image: kindle:embed:img0001?mime=image/jpeg\n"  # Kindle URL
            "\n"
            "Cross-ref: part0005.html#sec-results\n"  # EPUB link
            "\n"
            "End of chapter.\n"
        )
        cleaned, report = self._sanitize(body)

        # Invisible chars cleaned
        assert "\ufeff" not in cleaned
        assert "\u200b" not in cleaned
        assert "\u00a0" not in cleaned
        assert "\u00ad" not in cleaned

        # EPUB artifacts cleaned
        assert "@@NEXT" not in cleaned
        assert "@Bilinmeyen0000" not in cleaned
        assert "kindle:embed" not in cleaned
        assert "part0005.html#sec-results" not in cleaned

        # Math preserved
        assert "E = mc^2" in cleaned
        assert "\\begin{equation}" in cleaned

        # Real content preserved
        assert "This is the introduction." in cleaned
        assert "End of chapter." in cleaned

        # Report should show both types of cleanup
        assert report.invisible_chars_cleaned > 0
        assert report.epub_artifacts_cleaned > 0

    def test_protected_zones_still_work_after_cleanup(self):
        r"""Math, \ref, \cite content must survive invisible char + EPUB cleanup."""
        body = (
            "\ufeff"
            "$\\alpha + \\beta$\n"
            "See \\ref{fig:test} and \\cite{author2024}.\n"
            "@@NEXT\n"
            "\\begin{align}\n"
            "x &= y + z\n"
            "\\end{align}\n"
        )
        cleaned, report = self._sanitize(body)
        assert "$\\alpha + \\beta$" in cleaned
        assert "\\ref{fig:test}" in cleaned
        assert "\\cite{author2024}" in cleaned
        assert "x &= y + z" in cleaned
        assert "@@NEXT" not in cleaned

    def test_report_counts_accurate(self):
        """Report should accurately count invisible chars and EPUB artifacts."""
        body = (
            "\ufeff\u200b\u00a0text\n"  # 3 invisible chars
            "@@NEXT @@PAGEBREAK\n"      # 2 EPUB placeholders
        )
        cleaned, report = self._sanitize(body)
        assert report.invisible_chars_cleaned == 3
        assert report.epub_artifacts_cleaned == 2
        report._recount()
        assert report.total_fixes >= 5


# ======================================================================
# Main
# ======================================================================

if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
