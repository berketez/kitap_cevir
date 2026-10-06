"""Denetim 25 Eylül 2026 — denetçi R: K1/K3/K5 doğrulamasında bulunan kusurlar.

  K1d  epub_output._extract_text_from_tex: basit matematik Unicode'a
       çevrilince kısa satır eleği ('len > 2') tek başına duran '$\\pi$.',
       '\\(\\theta\\)' gibi satırları düşürüyordu; paragraf sayısı eski
       koddan farklılaşıyor, pozisyonel EPUB eşlemesi o noktadan kayıyordu.
       Elek artık dönüşümden önceki soyma uzunluğuna bakar.
  K5d  Küçük harfli dosya adındaki ek ('source_book.pdf'i') K5'in büyük
       harfe duyarlı bekçisinden kaçmıştı.

Metinler sentetiktir; model ya da XeLaTeX gerekmez.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from latex_pipeline.epub_output import _extract_text_from_tex  # noqa: E402


# ---------------------------------------------------------------------------
# K1d — kısa satır eleği dönüşümden önceki uzunluğa bakar
# ---------------------------------------------------------------------------

class TestK1dShortLineFilterKeepsParagraphCount:
    @pytest.mark.parametrize("middle, expected", [
        ("$\\pi$.", "π."),                              # eskiden '$$.'
        ("\\(\\theta\\)", "θ"),                         # eskiden '\(\)'
        ("$\\alpha$,", "α,"),                           # eskiden '$$,'
        ("\\texorpdfstring{$\\alpha$}{α}", "α"),        # eskiden '$${α}'
        ("\\textbf{$\\beta$}.", "β."),                  # eskiden '$$.'
        ("$\\Delta$ t", "Δ t"),                         # zaten geçiyordu
    ])
    def test_short_math_paragraph_not_dropped(self, middle, expected):
        tex = f"Birinci paragraf.\n\n{middle}\n\nSon paragraf.\n"
        assert _extract_text_from_tex(tex) == [
            "Birinci paragraf.", expected, "Son paragraf."]

    @pytest.mark.parametrize("middle", [
        "$\\pi$",       # eskiden de '$$' (2 harf) olarak eleniyordu
        "{\\i}",        # eskiden '{}'
        "ab",
    ])
    def test_lines_old_code_dropped_stay_dropped(self, middle):
        tex = f"Birinci paragraf.\n\n{middle}\n\nSon paragraf.\n"
        assert _extract_text_from_tex(tex) == [
            "Birinci paragraf.", "Son paragraf."]

    def test_short_line_inside_paragraph_kept(self):
        tex = "Uzun bir satır.\n$\\pi$.\nDevamı.\n"
        assert _extract_text_from_tex(tex) == ["Uzun bir satır. π. Devamı."]


# ---------------------------------------------------------------------------
# K5d — küçük harfli dosya adı + eski ek
# ---------------------------------------------------------------------------

# Dosya adının uzantısı okunuşla ('pe-de-fe') ek alır: .pdf'yi, .pdf'si ...
_OLD_LOWER_PDF_SUFFIX_RE = re.compile(
    r"\.pdf'(?:i|in|e|te|ten|tir)(?![^\W\d_])")
_SKIP_DIRS = {"venv", "output", "kutuphane", "cevrilenkitaplar", "models",
              "data", ".git", "__pycache__", ".pytest_cache", "fonts"}


def _source_files():
    for dirpath, dirnames, filenames in os.walk(PROJECT_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            if name.endswith((".py", ".md", ".tex")):
                path = Path(dirpath) / name
                if path.resolve() != Path(__file__).resolve():
                    yield path


class TestK5dLowercaseFilenameSuffix:
    def test_pattern(self):
        assert _OLD_LOWER_PDF_SUFFIX_RE.search("source_book.pdf'i yerinde")
        assert not _OLD_LOWER_PDF_SUFFIX_RE.search("source_book.pdf'si yerinde")
        assert not _OLD_LOWER_PDF_SUFFIX_RE.search("book_tr.pdf'yi aç")

    def test_no_old_suffix_on_pdf_filenames(self):
        hits = []
        for path in _source_files():
            text = path.read_text(encoding="utf-8", errors="replace")
            for n, line in enumerate(text.splitlines(), 1):
                if _OLD_LOWER_PDF_SUFFIX_RE.search(line):
                    hits.append(f"{path.relative_to(PROJECT_ROOT)}:{n}")
        assert hits == []
