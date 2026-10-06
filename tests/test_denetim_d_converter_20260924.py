"""Denetim 24 Eylül 2026, Mercek 1 #3 — dönüştürücü bölüm etiketi adaylığı.

Yalın "Chapter N" etiketinden sonra gelen başlık artık yalnız şu koşulda
etiketle birleşip \\chapter olur: etiketten DAHA DERİN düzeyde değil ve
numaralı bir soru/alıştırma/örnek başlığı ("Problem 1.3", "Übung 2") değil.
Eskiden ders kitaplarının cevap eklerindeki "## Chapter 1" + "### Problem 1.3"
örüntüsünde "Problem 1.3" \\chapter'a yükseltiliyor, "Chapter 1/2" etiketleri
kayboluyordu (Yol A Adım 1 / EXPORT.tex).

Girdiler sentetiktir (kitap metni kopyalanmadı).
"""

from __future__ import annotations

import re

import pytest

from latex_pipeline.config import PipelineConfig
from latex_pipeline.converter import NUMBERED_EXERCISE_WORDS, MarkdownToLatex


def _converter() -> MarkdownToLatex:
    return MarkdownToLatex(PipelineConfig(document_class="book", output_dir="/tmp/x"))


def _convert(markdown: str) -> str:
    return _converter().convert(markdown)


def _headings(tex: str) -> list[str]:
    return [
        ln for ln in tex.split("\n")
        if re.match(r"\\(chapter|section|subsection|subsubsection)\*?\{", ln)
    ]


def _unstarred(tex: str) -> list[str]:
    """Ön madde yıldızını (\\section*) at: burada yalnız DÜZEY sınanır."""
    return [h.replace("*{", "{", 1) for h in _headings(tex)]


# Denetimdeki conv_edge.py durumu: cevap eki, etiket ## + problem ###.
_MD_CEVAP_EKI = (
    "# Orbital Mechanics\n\n"
    "Some intro text for the book that is long enough to be a paragraph.\n\n"
    "# Introduction\n\n"
    "Text of chapter one about orbits and conic sections in space.\n\n"
    "## 1.1 Kepler\n\n"
    "Text.\n\n"
    "# Answers to Selected Problems\n\n"
    "## Chapter 1\n\n"
    "### Problem 1.3\n\n"
    "The answer is 42 km/s.\n\n"
    "### Problem 1.5\n\n"
    "The answer is 7 days.\n\n"
    "## Chapter 2\n\n"
    "### Problem 2.1\n\n"
    "The answer is 3 hours.\n"
)


class TestCevapEki:
    def test_etiketler_korunur_problemler_dogru_duzeyde(self):
        heads = _headings(_convert(_MD_CEVAP_EKI))
        start = heads.index("\\chapter{Answers to Selected Problems}")
        assert heads[start:] == [
            "\\chapter{Answers to Selected Problems}",
            "\\section{Chapter 1}",
            "\\subsection{Problem 1.3}",
            "\\subsection{Problem 1.5}",
            "\\section{Chapter 2}",
            "\\subsection{Problem 2.1}",
        ], heads

    def test_problem_hicbir_zaman_chapter_olmaz(self):
        tex = _convert(_MD_CEVAP_EKI)
        assert not re.search(r"\\chapter\*?\{Problem", tex), _headings(tex)

    def test_ayni_duzeyde_numarali_problem_de_aday_degil(self):
        # Etiketle AYNI düzeyde numaralı problem: düzey kuralı yakalamaz,
        # kelime kuralı yakalar.
        heads = _unstarred(_convert(
            "# Answers\n\nIntro text.\n\n## Chapter 1\n\n## Problem 1.3\n\nThe answer is 5.\n"))
        assert "\\section{Chapter 1}" in heads, heads
        assert "\\section{Problem 1.3}" in heads, heads
        assert not any(h.startswith("\\chapter{Problem") for h in heads), heads


@pytest.mark.parametrize("title", [
    "Problem 1.3",
    "PROBLEM 2",
    "Problems 4.1",
    "Exercise 2.4",
    "Example 3: Hohmann transfer",
    "Exercise A.2",
    "Problem No. 7",
    "Soru 1",
    "Alıştırma 3.2",
    "ALIŞTIRMA 3",
    "Örnek 2",
    "Задача 4.1",
    "Упражнение 1",
    "Пример 5",
    "Задача № 6",
    "Aufgabe 2.1",
    "Übung 4",
    "Beispiel 1",
    "Exercice 2",
    "Exemple 3",
    "Problème 1",
    "Ejercicio 1.2",
    "Ejemplo 3",
    "Problema 4",
    "Esercizio 5",
    "Esempio 2",
])
def test_numarali_soru_basligi_etiketle_birlesmez(title):
    heads = _headings(_convert(f"## Chapter 2\n\n## {title}\n\nMetin satırı.\n"))
    assert not any(h.startswith("\\chapter") and title in h for h in heads), heads
    assert any("Chapter 2" in h for h in heads), heads


@pytest.mark.parametrize("title", [
    "Problems and Solutions",
    "Example Applications",
    "Exercises",
    "Problem Set Overview",
])
def test_numarasiz_soru_kelimeli_baslik_yine_bolum_olur(title):
    # Kelime kuralı yalnız NUMARALI başlığı dışlar; numarasız bölüm adı
    # etiketle birleşmeye devam eder.
    heads = _headings(_convert(f"## Chapter 5\n\n## {title}\n\nMetin satırı.\n"))
    assert f"\\chapter{{{title}}}" in heads, heads
    assert not any("Chapter 5" in h for h in heads), heads


class TestDuzeyKurali:
    def test_daha_derin_numarasiz_baslik_birlesmez(self):
        heads = _unstarred(_convert(
            "## Chapter 3\n\n### Orbit Determination\n\nMetin satırı.\n"))
        assert "\\chapter{Orbit Determination}" not in heads, heads
        assert any("Chapter 3" in h for h in heads), heads
        assert "\\subsection{Orbit Determination}" in heads, heads

    def test_daha_sig_baslik_birlesir(self):
        # Bir test kitabının 3. bölüm örüntüsü: "## Chapter 3" + "# Başlık" -> tek \chapter
        heads = _headings(_convert(
            "## Chapter 3\n\n# Three-Dimensional Motion\n\n## 3.1 Frames\n\nMetin.\n"))
        assert "\\chapter{Three-Dimensional Motion}" in heads, heads
        assert not any("Chapter 3" in h for h in heads), heads

    def test_ayni_duzey_birlesir(self):
        heads = _headings(_convert(
            "## Chapter 2\n\n## Planar Motion\n\n#### 2.1 Frames\n\nMetin.\n"))
        assert "\\chapter{Planar Motion}" in heads, heads

    def test_rusca_derin_etiket_sig_baslik_birlesir(self):
        # Rusça bir kitabın örüntüsü: "#### Глава 1" + "## BAŞLIK" (etiket daha derin)
        heads = _headings(_convert(
            "#### Глава 1\n\n## ОБЩИЕ СВЕДЕНИЯ\n\n## § 1.1. ТЕРМИНОЛОГИЯ\n\nТекст.\n"))
        assert heads[0] == "\\chapter{ОБЩИЕ СВЕДЕНИЯ}", heads
        assert "\\section{§ 1.1. ТЕРМИНОЛОГИЯ}" in heads, heads

    def test_aday_fonksiyonu_duzey_siniri(self):
        c = _converter()
        assert c._is_chapter_title_candidate("# Title", 2)
        assert c._is_chapter_title_candidate("## Title", 2)
        assert not c._is_chapter_title_candidate("### Title", 2)
        assert c._is_chapter_title_candidate("## Title", 4)
        assert not c._is_chapter_title_candidate("## Problem 1.3", 2)
        assert not c._is_chapter_title_candidate("## **Übung 2**", 2)
        assert not c._is_chapter_title_candidate("## 2.1 Title", 2)
        assert not c._is_chapter_title_candidate("## Chapter 3", 2)

    def test_etiket_durumu_sizmaz(self):
        # Birleşmeyen etiket bekleyen etiket durumu bırakmaz: sonraki
        # bağımsız başlık bölüme zorlanmaz.
        c = _converter()
        tex = c.convert("## Chapter 1\n\n### Problem 1.3\n\nCevap.\n\n### Remarks\n\nNot.\n")
        assert c._pending_chapter_label is None
        heads = _unstarred(tex)
        assert "\\subsection{Remarks}" in heads, heads


def test_kelime_listesi_tek_sabit():
    # Liste tek kaynaktan: sınıf regex'i modül sabitinden kurulur.
    assert NUMBERED_EXERCISE_WORDS in MarkdownToLatex._NUMBERED_EXERCISE_RE.pattern
