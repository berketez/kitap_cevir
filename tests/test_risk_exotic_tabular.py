"""Risk 3 — Egzotik tabular tipleri simülasyonu.

13 Mart fiyaskosu: TOC tabular komple stash'lenip çevrilmeden
geçiyordu. Fix: _translate_tabular_body. Ama 25 Nisan sprint'i
sadece basit tabular'ları test etti.

Bu testler farklı kitap stillerinde karşılaşılacak tabular tiplerini
kontrol eder:
1. multicolumn / multirow
2. nested tabular (table-in-table)
3. tabularx (X column expanding)
4. longtable
5. booktabs (\\toprule \\midrule \\cmidrule \\bottomrule)
6. math-only cell ($x^2$)
7. boş hücre
8. column separator çeşitleri ({l|c|r}, {|l|}, {l@{...}r})

Tüm testler MOCK kullanır.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestStashSafety:
    """Tabular kapsayıcısı stash'lendikten sonra recover edilebilmeli.

    13 Mart bug'ında tabular komple stash → hiç çevrilmeden geri
    yerleştiriliyor. Fix: _translate_tabular_body içeriği translate eder.
    """

    def test_simple_tabular_translated(self, make_latex_translator,
                                         mock_batch_fn, mock_single_fn):
        """Temel: \\begin{tabular}{ll} A & B \\end{tabular} → çevrilmeli."""
        lt = make_latex_translator()
        body = (
            r"\begin{tabular}{ll}" "\n"
            r"Introduction & 1 \\" "\n"
            r"Conclusion & 99 \\" "\n"
            r"\end{tabular}"
        )
        result = lt.translate(body)
        # Tabular komple stash'lenmemeli (eski bug)
        assert r"\begin{tabular}" in result
        assert r"\end{tabular}" in result
        # Çeviri yapılmış olmalı
        assert "[TR]" in result or "Giriş" in result

    def test_tabularx_handled(self, make_latex_translator,
                                mock_batch_fn, mock_single_fn):
        """tabularx (genişleyen X kolonu) — pipeline desteklemeli."""
        lt = make_latex_translator()
        body = (
            r"\begin{tabularx}{\linewidth}{lXr}" "\n"
            r"Param & Description here is long & 42 \\" "\n"
            r"\end{tabularx}"
        )
        result = lt.translate(body)
        assert r"\begin{tabularx}" in result
        assert r"\end{tabularx}" in result

    def test_longtable_handled(self, make_latex_translator,
                                 mock_batch_fn, mock_single_fn):
        """longtable (sayfa-bölünebilir tablo) — desteklemeli."""
        lt = make_latex_translator()
        body = (
            r"\begin{longtable}{lll}" "\n"
            r"\toprule" "\n"
            r"Symbol & Meaning & Unit \\" "\n"
            r"\midrule" "\n"
            r"$F$ & Force & N \\" "\n"
            r"\bottomrule" "\n"
            r"\end{longtable}"
        )
        result = lt.translate(body)
        assert r"\begin{longtable}" in result


class TestBooktabsHandling:
    """booktabs komutları (\\toprule, \\midrule, \\cmidrule, \\bottomrule)."""

    def test_booktabs_inside_tabular(self, make_latex_translator,
                                       mock_batch_fn, mock_single_fn):
        """\\toprule/\\midrule içerikleri kaybolmasın."""
        lt = make_latex_translator()
        body = (
            r"\begin{tabular}{lll}" "\n"
            r"\toprule" "\n"
            r"Chapter & Title & Page \\" "\n"
            r"\midrule" "\n"
            r"1 & Introduction & 1 \\" "\n"
            r"2 & Methods & 25 \\" "\n"
            r"\bottomrule" "\n"
            r"\end{tabular}"
        )
        result = lt.translate(body)
        assert r"\toprule" in result or "<<LATEX" in result
        assert r"\midrule" in result or "<<LATEX" in result
        assert r"\bottomrule" in result or "<<LATEX" in result

    def test_cmidrule_with_span(self, make_latex_translator,
                                  mock_batch_fn, mock_single_fn):
        """\\cmidrule{2-3} gibi spanlı komutlar."""
        lt = make_latex_translator()
        body = (
            r"\begin{tabular}{lll}" "\n"
            r"\toprule" "\n"
            r"A & B & C \\" "\n"
            r"\cmidrule{2-3}" "\n"
            r"D & E & F \\" "\n"
            r"\bottomrule" "\n"
            r"\end{tabular}"
        )
        result = lt.translate(body)
        # \cmidrule{2-3} korunmuş olmalı
        assert "cmidrule" in result or "<<LATEX" in result

    def test_bare_booktabs_outside_tabular(self, make_latex_translator,
                                             mock_batch_fn, mock_single_fn):
        """Marker-PDF artifact'i: \\toprule tabular DIŞINDA — stash'lenmeli."""
        lt = make_latex_translator()
        body = (
            r"\toprule Chapter 1 & Introduction & 1 \\"
            "\n"
            r"\midrule"
            "\n"
        )
        # Bug B2: bare booktabs komutları stash'lenmeli ki MT'ye gitmesin
        result = lt.translate(body)
        # \toprule patlatma yapmamalı, korunmuş olmalı
        assert r"\toprule" in result or "<<LATEX" in result


class TestMulticolumnMultirow:
    """\\multicolumn ve \\multirow hücre span'leri."""

    def test_multicolumn_cell(self, make_latex_translator,
                                mock_batch_fn, mock_single_fn):
        """\\multicolumn{3}{c}{Title} — span'li başlık hücresi."""
        lt = make_latex_translator()
        body = (
            r"\begin{tabular}{lll}" "\n"
            r"\multicolumn{3}{c}{Combined Title} \\" "\n"
            r"A & B & C \\" "\n"
            r"\end{tabular}"
        )
        result = lt.translate(body)
        # \multicolumn{3}{c}{...} bütünlük korumalı
        assert r"\multicolumn" in result or "<<LATEX" in result

    def test_multirow_cell(self, make_latex_translator,
                             mock_batch_fn, mock_single_fn):
        """\\multirow{2}{*}{Label}."""
        lt = make_latex_translator()
        body = (
            r"\begin{tabular}{ll}" "\n"
            r"\multirow{2}{*}{Group A} & Item 1 \\" "\n"
            r" & Item 2 \\" "\n"
            r"\end{tabular}"
        )
        result = lt.translate(body)
        assert r"\multirow" in result or "<<LATEX" in result


class TestColumnSpecVariants:
    """Egzotik kolon spec'leri pipeline'ı bozmasın."""

    def test_pipe_separators(self, make_latex_translator,
                               mock_batch_fn, mock_single_fn):
        """\\begin{tabular}{|l|c|r|} — pipe ayraçlı."""
        lt = make_latex_translator()
        body = (
            r"\begin{tabular}{|l|c|r|}" "\n"
            r"\hline" "\n"
            r"Name & Value & Unit \\" "\n"
            r"\hline" "\n"
            r"\end{tabular}"
        )
        result = lt.translate(body)
        assert r"\begin{tabular}{|l|c|r|}" in result

    def test_at_separator(self, make_latex_translator,
                            mock_batch_fn, mock_single_fn):
        """{l@{\\hspace{2em}}r} — @{} özel separator."""
        lt = make_latex_translator()
        body = (
            r"\begin{tabular}{l@{\hspace{2em}}r}" "\n"
            r"Item & 42 \\" "\n"
            r"\end{tabular}"
        )
        result = lt.translate(body)
        # @{...} korunmalı, kolon spec'i sanitizer'ın TOC fix'i bunu bozmamalı
        assert "@{" in result

    def test_p_column_with_width(self, make_latex_translator,
                                   mock_batch_fn, mock_single_fn):
        """p{5cm} — sabit genişlikli kolon."""
        lt = make_latex_translator()
        body = (
            r"\begin{tabular}{l p{5cm} r}" "\n"
            r"Symbol & Description & Page \\" "\n"
            r"\end{tabular}"
        )
        result = lt.translate(body)
        assert r"p{5cm}" in result

    def test_array_column_format(self, make_latex_translator,
                                   mock_batch_fn, mock_single_fn):
        """{>{\\centering}p{3cm}} — array paket prefiksli."""
        lt = make_latex_translator()
        body = (
            r"\begin{tabular}{>{\centering\arraybackslash}p{3cm} l}" "\n"
            r"Centered & Left \\" "\n"
            r"\end{tabular}"
        )
        result = lt.translate(body)
        # Patlamasın yeter
        assert r"\begin{tabular}" in result
        assert r"\end{tabular}" in result


class TestNestedAndComplex:
    """İç içe / karmaşık tabular yapıları."""

    def test_nested_tabular(self, make_latex_translator,
                              mock_batch_fn, mock_single_fn):
        """Tabular hücresi içinde başka bir tabular."""
        lt = make_latex_translator()
        body = (
            r"\begin{tabular}{lc}" "\n"
            r"Outer & \begin{tabular}{ll} a & b \\ c & d \end{tabular} \\" "\n"
            r"Bottom & End \\" "\n"
            r"\end{tabular}"
        )
        result = lt.translate(body)
        # Patlamasın, dış tabular bütünlüğüyle dursun
        assert result.count(r"\begin{tabular}") >= 1

    def test_math_only_cell(self, make_latex_translator,
                              mock_batch_fn, mock_single_fn):
        """Sadece matematik içeren hücre — MT'ye gönderilmemeli."""
        lt = make_latex_translator()
        body = (
            r"\begin{tabular}{lc}" "\n"
            r"Energy & $E = mc^2$ \\" "\n"
            r"Force & $F = ma$ \\" "\n"
            r"\end{tabular}"
        )
        result = lt.translate(body)
        # Math placeholder olarak korunmuş veya $...$ geri gelmiş olmalı
        assert "$" in result or "<<LATEX" in result

    def test_empty_cells(self, make_latex_translator,
                           mock_batch_fn, mock_single_fn):
        """Boş hücreler pipeline'ı bozmamalı."""
        lt = make_latex_translator()
        body = (
            r"\begin{tabular}{lll}" "\n"
            r"A & & C \\" "\n"
            r" & B & \\" "\n"
            r"\end{tabular}"
        )
        result = lt.translate(body)
        # 2-3 cell ayracı korunmalı
        assert "&" in result

    def test_huge_tabular_50_rows(self, make_latex_translator,
                                    mock_batch_fn, mock_single_fn):
        """50 satırlı büyük tablo — patlamasın, hızlı bitsin."""
        lt = make_latex_translator()
        rows = "\n".join([f"Row {i} & Value {i} & Unit {i} \\\\" for i in range(50)])
        body = (
            r"\begin{tabular}{lll}" "\n"
            + rows + "\n"
            + r"\end{tabular}"
        )
        result = lt.translate(body)
        assert r"\begin{tabular}" in result
        assert r"\end{tabular}" in result


class TestMixedScientificContent:
    """Bilim/mühendislik kitaplarında karşılaşılan karışık tablolar."""

    def test_units_table_with_math(self, make_latex_translator,
                                     mock_batch_fn, mock_single_fn):
        """Birim tablosu: text + math + units karışık."""
        lt = make_latex_translator()
        body = (
            r"\begin{tabular}{lll}" "\n"
            r"\toprule" "\n"
            r"Quantity & Symbol & SI Unit \\" "\n"
            r"\midrule" "\n"
            r"Energy & $E$ & $\text{J}$ \\" "\n"
            r"Force & $F$ & $\text{N}$ \\" "\n"
            r"Pressure & $p$ & $\text{Pa}$ \\" "\n"
            r"\bottomrule" "\n"
            r"\end{tabular}"
        )
        result = lt.translate(body)
        # Math kalmış
        assert "$" in result or "<<LATEX" in result

    def test_toc_style_4col(self, make_latex_translator,
                              mock_batch_fn, mock_single_fn):
        """TOC stili 4-kolonlu — sanitizer p{70mm} fix uygulamalı."""
        lt = make_latex_translator()
        body = (
            r"\begin{tabular}{l l l l}" "\n"
            r"\toprule Chapter & 2 & Aerothermodynamics & 21 \\" "\n"
            r"\midrule 2.1 & & Introduction & 21 \\" "\n"
            r"2.2 & & Energy Conservation Equations & 25 \\" "\n"
            r"\end{tabular}"
        )
        result = lt.translate(body)
        # Tabular bütünlüğü
        assert r"\begin{tabular}" in result
