"""B21: TOC tabular column width fix.

TOC tablolarında ``\\begin{tabular}{l l l l}`` gibi sabit genişliği olmayan
sütunlar, uzun başlıklar olduğunda sayfayı taşırıp metnin kesilmesine yol
açıyordu. Sanitizer'ın ``_fix_toc_tabular_widths`` metodu penultimate
sütunu ``>{\\raggedright\\arraybackslash}p{70mm}``, son sütunu ``r`` ile
sınırlandırır. ``\\raggedright`` injection overfull \\hbox'ı (clip) önler.
"""
import pytest


class TestTocTabularWidth:
    def _get_sanitizer(self):
        from latex_pipeline.sanitizer import LatexSanitizer
        return LatexSanitizer(source_lang="en", target_lang="tr")

    # 2026-09-24: B21 artık TOC kanıtını tablo GÖVDESİNDEN okur (son sütun
    # artan sayfa no + başlık metni); yalnız spec satırı TOC kanıtı değildir
    # (veri tablosu Tablo 2.1 bozuluyordu). Bu iki test bu yüzden içindekiler
    # satırı içeren gövdeyle besleniyor; beklenen dönüşüm aynı.
    def test_llll_converted_to_pwidth(self):
        san = self._get_sanitizer()
        if not hasattr(san, "_fix_toc_tabular_widths"):
            pytest.skip("_fix_toc_tabular_widths henüz eklenmedi")
        body = (r"\begin{tabular}{l l l l}" "\n"
                r"1 & Bölüm & Giriş ve Temel Kavramlar & 11 \\" "\n"
                r"\end{tabular}")
        result = san._fix_toc_tabular_widths(body)
        assert "p{" in result, f"p{{}} yok: {result!r}"
        assert result.splitlines()[0].rstrip().endswith("r}")

    def test_lll_three_cols_converted(self):
        san = self._get_sanitizer()
        if not hasattr(san, "_fix_toc_tabular_widths"):
            pytest.skip("...")
        body = (r"\begin{tabular}{l l l}" "\n"
                r"1.1 & Giriş & 3 \\" "\n"
                r"\end{tabular}")
        result = san._fix_toc_tabular_widths(body)
        assert "p{70mm}" in result and "raggedright" in result
        assert result.splitlines()[0].rstrip().endswith("r}")

    def test_already_pwidth_unchanged(self):
        san = self._get_sanitizer()
        if not hasattr(san, "_fix_toc_tabular_widths"):
            pytest.skip("...")
        body = r"\begin{tabular}{l p{5cm} r}"
        result = san._fix_toc_tabular_widths(body)
        assert "p{5cm}" in result

    def test_2_col_unchanged(self):
        """2 sütunlu tabular dokunulmasın."""
        san = self._get_sanitizer()
        if not hasattr(san, "_fix_toc_tabular_widths"):
            pytest.skip("...")
        body = r"\begin{tabular}{l r}"
        result = san._fix_toc_tabular_widths(body)
        assert result == body

    def test_7_col_unchanged(self):
        """Çok geniş (>6 sütun) spec dokunulmasın."""
        san = self._get_sanitizer()
        if not hasattr(san, "_fix_toc_tabular_widths"):
            pytest.skip("...")
        body = r"\begin{tabular}{l l l l l l l}"
        result = san._fix_toc_tabular_widths(body)
        assert result == body

    def test_column_separators_unchanged(self):
        """@{} veya || içeren spec dokunulmasın."""
        san = self._get_sanitizer()
        if not hasattr(san, "_fix_toc_tabular_widths"):
            pytest.skip("...")
        body = r"\begin{tabular}{l||l|l|l}"
        result = san._fix_toc_tabular_widths(body)
        assert result == body

    def test_real_toc_example(self):
        san = self._get_sanitizer()
        if not hasattr(san, "_fix_toc_tabular_widths"):
            pytest.skip("...")
        body = (
            r"\begin{tabular}{l l l l}" "\n"
            r"\toprule Bölüm & 2 & - Aerothermochemistry Roket Motorları & 21 \\" "\n"
            r"\midrule 2.1 & & Giriş & 21 \\" "\n"
            r"\end{tabular}"
        )
        result = san._fix_toc_tabular_widths(body)
        assert "p{70mm}" in result and "raggedright" in result

    def test_idempotent(self):
        """İkinci kez çalıştırmak değiştirmemeli (spec p{} içerdiği için).

        Denetim 2026-09-24 (M3#9): yalnız spec satırıyla beslenen eski hâli
        gövdeye dayalı sezgide hiç dönüşmüyordu (first == second boşa
        doğruydu); gerçek İçindekiler gövdesiyle ilk geçişin p{70mm}
        ürettiği de sınanır.
        """
        san = self._get_sanitizer()
        if not hasattr(san, "_fix_toc_tabular_widths"):
            pytest.skip("...")
        body = (
            r"\begin{tabular}{l l l l}" "\n"
            r"1 & Bölüm & Giriş ve Temel Kavramlar & 11 \\" "\n"
            r"1.1 & & Tarihçe & 12 \\" "\n"
            r"1.2 & & Temel Tanımlar & 17 \\" "\n"
            r"\end{tabular}"
        )
        first = san._fix_toc_tabular_widths(body)
        assert first != body
        assert first.splitlines()[0] == (
            r"\begin{tabular}{l l >{\raggedright\arraybackslash}p{70mm} r}")
        second = san._fix_toc_tabular_widths(first)
        assert first == second

    def test_sanitize_pipeline_applies_fix(self):
        """sanitize() çağrısı TOC genişlik düzeltmesini de uygulamalı."""
        san = self._get_sanitizer()
        body = (
            r"\begin{tabular}{l l l l}" "\n"
            r"1 & Bölüm & Giriş ve Temel Kavramlar & 11 \\" "\n"
            r"\end{tabular}"
        )
        cleaned, _report = san.sanitize(body)
        assert "p{70mm}" in cleaned and "raggedright" in cleaned
