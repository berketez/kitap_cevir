"""Bölüm etiketi + aynı düzeyde başlık = tek \\chapter — TeX yolunun tüm kaynak dillerinde.

24 Eylül 2026: Yol B testinde "## Chapter 2" + "## Two-Dimensional Analysis
of Motion" (aynı düzey) \\section'a düşüyordu; düzeltme yalnız Chapter/Bölüm'ü
tanıyordu. Rusça tarama kitabında Marker aynı örüntüyü veriyor
("## Глава 1" + "## <başlık>"); etiket kelimeleri artık
latex_pipeline/patterns.py CHAPTER_LABEL_WORDS'te tek kaynak.
"""

from __future__ import annotations

import re

import pytest

from latex_pipeline.config import PipelineConfig
from latex_pipeline.converter import MarkdownToLatex
from latex_pipeline.sanitizer import LatexSanitizer, SanitizeReport

# Başlık ve cümle SENTETİKTİR (25 Eylül 2026, telif: kitaptan metin alınmaz);
# biçim, Rusça tarama bir ders kitabının Marker çıktısını taklit eder: etiket
# satırı, aynı düzeyde büyük harfli bölüm adı, "§" numaralı alt başlık.
_MD_RUSCA = (
    "## Глава 1\n\n"
    "## ДВИЖЕНИЕ ГАЗА В УЧЕБНОЙ МОДЕЛИ СОПЛА\n\n"
    "## § 1.1. ИСХОДНЫЕ ДОПУЩЕНИЯ УЧЕБНОЙ МОДЕЛИ\n\n"
    "Чтобы учебная ракета оторвалась от стартового стола, двигатель должен "
    "развить силу, которую называют *тягой*.\n"
)


def _convert(markdown: str) -> str:
    return MarkdownToLatex(PipelineConfig(document_class="book", output_dir="/tmp/x")).convert(markdown)


def _headings(tex: str) -> list[str]:
    return [line for line in tex.split("\n")
            if re.match(r"\\(chapter|section|subsection|subsubsection)\*?\{", line)]


def test_rusca_glava_etiketi_bolum_olur():
    headings = _headings(_convert(_MD_RUSCA))
    assert "\\chapter{ДВИЖЕНИЕ ГАЗА В УЧЕБНОЙ МОДЕЛИ СОПЛА}" in headings
    assert not any("Глава" in h for h in headings)


@pytest.mark.parametrize("label, title", [
    ("Kapitel 2", "Die Düse"),
    ("Chapitre 3", "La tuyère"),
    ("Capítulo 4", "La tobera"),
    ("Capitulo 4", "La tobera"),
    ("Capitolo 5", "L'ugello"),
    ("ГЛАВА 6", "СОПЛА"),
    ("Chapter II", "Reference Frames"),
])
def test_diger_dillerin_etiketleri(label, title):
    markdown = f"## {label}\n\n## {title}\n\n#### 2.1 Alt başlık\n\nMetin.\n"
    headings = _headings(_convert(markdown))
    assert f"\\chapter{{{title}}}" in headings
    assert not any(label in h for h in headings)


# Düzey dizisi gerçek bir taramanın full_book.md'sinden (Rusça ders kitabı, 1.
# bölüm): Marker'ın verdiği düzeyler sayfadan sayfaya rastgele (####, ##, ####,
# ###, #, ##, ##). Başlık metinleri sentetiktir.
_MD_RUSCA_BOLUM = (
    "#### Глава 1\n\n"
    "## ДВИЖЕНИЕ ГАЗА В УЧЕБНОЙ МОДЕЛИ СОПЛА\n\n"
    "#### § 1.1. ИСХОДНЫЕ ДОПУЩЕНИЯ УЧЕБНОЙ МОДЕЛИ\n\nТекст.\n\n"
    "### § 1.2. ПОДАЧА ТОПЛИВА В КАМЕРУ\n\nТекст.\n\n"
    "# § 1.3. ТЕМПЕРАТУРА, ДАВЛЕНИЕ И СКОРОСТЬ НА ВХОДЕ В СОПЛО\n\nТекст.\n\n"
    "## § 1.4. РАСЧЕТ ТЯГИ ДЛЯ ТРЕХ РЕЖИМОВ\n\nТекст.\n\n"
    "## § 1.5. СРАВНЕНИЕ С ОПЫТНЫМИ ДАННЫМИ\n\nТекст.\n"
)


def test_paragraf_isaretli_numara_duzeyi_belirler():
    headings = _headings(_convert(_MD_RUSCA_BOLUM))
    chapters = [h for h in headings if h.startswith("\\chapter")]
    assert chapters == ["\\chapter{ДВИЖЕНИЕ ГАЗА В УЧЕБНОЙ МОДЕЛИ СОПЛА}"]
    paragraphs = [h for h in headings if "§ 1." in h]
    assert len(paragraphs) == 5
    assert all(h.startswith("\\section{§ 1.") for h in paragraphs), paragraphs


def test_numarasiz_etiket_olmayan_baslik_dokunulmaz():
    headings = _headings(_convert("## Kapitel und Vers\n\n## Die Düse\n\nText.\n"))
    assert not any(h.startswith("\\chapter{Die Düse}") for h in headings)


def _sanitize(body: str) -> str:
    sanitizer = LatexSanitizer.__new__(LatexSanitizer)
    return sanitizer._fix_marker_chapter_artifacts(body, SanitizeReport())


def test_sanitizer_rusca_etiket_section_birlesir():
    out = _sanitize("\\chapter{A}\n\nMetin.\n\n\\section{Глава 2}\n\n\\section{СОПЛА}\n\nМетин.\n")
    assert "\\chapter{СОПЛА}" in out
    assert "Глава 2" not in out


def test_sanitizer_sahte_bos_chapter_silinir():
    out = _sanitize("\\chapter{Kapitel 3}\n\\chapter{Die Düse}\n\nText.\n")
    assert "Kapitel 3" not in out
    assert "\\chapter{Die Düse}" in out


def test_paragraf_isaretli_numara_oneki_silinir():
    """LaTeX kendi numarasını basar; "§ 1.1." kalırsa PDF'de "1.1 § 1.1." çıkar."""
    sanitizer = LatexSanitizer.__new__(LatexSanitizer)
    out = sanitizer._strip_section_number_prefix(
        "\\section{§ 1.1. ТЕРМИНОЛОГИЯ}\n\\section{§1.2 Sınıflandırma}\n"
        "\\subsection{2.3 Başlık}\n\\section{§ yalnız işaret}\n",
        SanitizeReport())
    assert "\\section{ТЕРМИНОЛОГИЯ}" in out
    assert "\\section{Sınıflandırma}" in out
    assert "\\subsection{Başlık}" in out
    assert "\\section{§ yalnız işaret}" in out     # numarasız § dokunulmaz
