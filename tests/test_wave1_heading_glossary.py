"""Tests for Wave 1: Heading glossary expansion, placeholder leak fix.

Covers:
- Book-generic heading entries (Forward, Preface, Epilogue, etc.)
- EPUB placeholder hallucination detection (_validate_stash_item)
- EPUB placeholder cleanup in _post_process_translation
- Retranslator EPUB placeholder cleanup function
"""

import sys
from pathlib import Path

# Ensure project root on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from latex_pipeline.translator import LatexTranslator


# ---------------------------------------------------------------------------
# Helpers -- access class-level dict / static methods without full init
# ---------------------------------------------------------------------------

_GLOSSARY = LatexTranslator._HEADING_GLOSSARY
_title_case = LatexTranslator._turkish_title_case
_validate = LatexTranslator._validate_stash_item
_EPUB_RE = LatexTranslator._EPUB_HALLUCINATION_RE

# ===========================================================================
# 2. Book-Generic Headings
# ===========================================================================

class TestBookGenericHeadings:
    """Verify book-generic heading entries (Forward, Preface, Epilogue, etc.)."""

    def test_foreword(self):
        assert _GLOSSARY["Foreword"] == "\u00d6ns\u00f6z"

    def test_preface(self):
        assert _GLOSSARY["Preface"] == "\u00d6ns\u00f6z"

    def test_acknowledgments(self):
        assert _GLOSSARY["Acknowledgments"] == "Te\u015fekk\u00fcr"

    def test_acknowledgements_british(self):
        assert _GLOSSARY["Acknowledgements"] == "Te\u015fekk\u00fcr"

    def test_epilogue(self):
        assert _GLOSSARY["Epilogue"] == "Sons\u00f6z"

    def test_about_the_author(self):
        assert _GLOSSARY["About the Author"] == "Yazar Hakk\u0131nda"

    def test_table_of_contents(self):
        assert _GLOSSARY["Table of Contents"] == "\u0130\u00e7indekiler"

    def test_glossary_heading(self):
        assert _GLOSSARY["Glossary"] == "S\u00f6zl\u00fck"

    def test_index(self):
        assert _GLOSSARY["Index"] == "Dizin"

    def test_dedication(self):
        assert _GLOSSARY["Dedication"] == "\u0130thaf"

# ===========================================================================
# 4. EPUB Placeholder Hallucination Detection
# ===========================================================================

class TestEpubPlaceholderDetection:
    """Test the _EPUB_HALLUCINATION_RE regex."""

    def test_bilinmeyen_pattern(self):
        assert _EPUB_RE.search("@Bilinmeyen0000")

    def test_unknown_pattern(self):
        assert _EPUB_RE.search("@Unknown123")

    def test_known_at_pattern(self):
        assert _EPUB_RE.search("@@KNOWN0001@@")

    def test_known_partial_pattern(self):
        assert _EPUB_RE.search("KNOWN0001@@")

    def test_at_next_pattern(self):
        assert _EPUB_RE.search("@@NEXT")

    def test_clean_text_no_match(self):
        assert not _EPUB_RE.search("Normal Turkish text here")

    def test_email_no_match(self):
        """Email addresses should not be caught as EPUB placeholders."""
        # user@domain.com has no digits right after alpha, so it's safe
        assert not _EPUB_RE.search("user@domain.com")

    def test_latex_command_no_match(self):
        assert not _EPUB_RE.search("\\textit{some text}")


# ===========================================================================
# 5. Stash Item Validation with EPUB Hallucinations
# ===========================================================================

class TestValidateStashItemEpub:
    """_validate_stash_item must reject MT-hallucinated EPUB placeholders."""

    def test_clean_textit_passes(self):
        assert _validate(
            "\\textit{First Set of Books}",
            "\\textit{Birinci Kitap Seti}",
        )

    def test_bilinmeyen_rejected(self):
        """Stash item with @Bilinmeyen0000 must be rejected."""
        assert not _validate(
            "\\textit{First Set of Books}",
            "\\textit{@Bilinmeyen0000 Kitaplar}",
        )

    def test_known_rejected(self):
        """Stash item with KNOWN0001@@ must be rejected."""
        assert not _validate(
            "\\textit{The End of Men}",
            "\\textit{KNOWN0001@@ Erkekler}",
        )

    def test_at_at_known_rejected(self):
        """Stash item with @@KNOWN0001@@ must be rejected."""
        assert not _validate(
            "\\textit{Some Book Title}",
            "\\textit{@@KNOWN0001@@ Baslik}",
        )

    def test_original_has_pattern_passes(self):
        """If original also has the pattern, it should pass (not hallucinated)."""
        assert _validate(
            "\\textit{@Legacy0001 Reference}",
            "\\textit{@Legacy0001 Referans}",
        )


# ===========================================================================
# 6. Felsefe türü sözlük dosyası (silindi)
# ===========================================================================

# G2c (KARAR 25, 2026-10-05): felsefe türünün sözlük dosyası SİLİNDİ — sabit genel
# alan sözlüğü yok; her kitap kendi sözlüğünü çıkarır
# (latex_pipeline/kitap_sozlugu.py, glossary_shared.tsv). Dosyanın varlık ve
# yapı testleri (TestRedPillGlossaryFile) kaldırıldı; translator'ın başlık
# sözlüğü (_HEADING_GLOSSARY) testleri durur.


# ===========================================================================
# 7. Retranslator EPUB Cleanup Function
# ===========================================================================

class TestRetranslatorEpubCleanup:
    """Test _clean_epub_placeholder_hallucinations from retranslator."""

    def test_removes_bilinmeyen(self):
        from latex_pipeline.retranslator import _clean_epub_placeholder_hallucinations
        result = _clean_epub_placeholder_hallucinations(
            "Bu bir @Bilinmeyen0000 Kitaplar testidir"
        )
        assert "@Bilinmeyen0000" not in result
        assert "testidir" in result

    def test_removes_known_at(self):
        from latex_pipeline.retranslator import _clean_epub_placeholder_hallucinations
        result = _clean_epub_placeholder_hallucinations(
            "Metin @@KNOWN0001@@ burada"
        )
        assert "@@KNOWN0001@@" not in result
        assert "burada" in result

    def test_clean_text_unchanged(self):
        from latex_pipeline.retranslator import _clean_epub_placeholder_hallucinations
        text = "Normal Turkce metin burada"
        assert _clean_epub_placeholder_hallucinations(text) == text

    def test_short_result_returns_original(self):
        from latex_pipeline.retranslator import _clean_epub_placeholder_hallucinations
        # If removing the placeholder leaves too little text, return original
        text = "@Bilinmeyen0000"
        result = _clean_epub_placeholder_hallucinations(text)
        # Should return original since cleaned would be empty
        assert result == text
