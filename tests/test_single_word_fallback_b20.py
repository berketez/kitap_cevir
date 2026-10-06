"""B20 / FAZ F1: Tek kelime fallback — partial match sonrası leftover cleanup.

`_translate_cell` tabular TOC hücrelerinde kısmi glossary eşleşmesi sonrası
ya da kısmen Türkçe metinlerde kalan tek İngilizce kelimeleri
``_SINGLE_WORD_FALLBACK`` ile çevirir. Tamamen İngilizce metinler MT yoluna
düşmeli, tek kelime temizliğine değil.
"""

from __future__ import annotations

import pytest


class TestSingleWordFallbackSweep:
    """``_sweep_single_word_fallbacks`` helper davranışı."""

    def _get_translator(self):
        from latex_pipeline.translator import LatexTranslator
        return LatexTranslator.__new__(LatexTranslator)

    def test_helper_exists(self):
        lt = self._get_translator()
        assert hasattr(lt, "_sweep_single_word_fallbacks"), (
            "_sweep_single_word_fallbacks helper bulunamadı"
        )
        assert hasattr(lt, "_SINGLE_WORD_FALLBACK"), (
            "_SINGLE_WORD_FALLBACK sözlüğü tanımsız"
        )

    def test_fallback_dict_has_expected_keys(self):
        lt = self._get_translator()
        d = lt._SINGLE_WORD_FALLBACK
        # FAZ F1 TOC bulgularında geçen leftover'lar
        for key in [
            "Reaction", "Equation", "Variation", "Effect",
            "Elements", "Aerothermochemistry",
            "CHEMICAL", "GAS", "DYNAMICS", "EQUATION",
            "GEOMETRY", "BASICS",
        ]:
            assert key in d, f"_SINGLE_WORD_FALLBACK'te {key!r} yok"

    def test_sweep_pure_turkish_unchanged(self):
        lt = self._get_translator()
        assert lt._sweep_single_word_fallbacks("Temel İlkeler") == "Temel İlkeler"

    def test_sweep_single_capitalized_english(self):
        lt = self._get_translator()
        result = lt._sweep_single_word_fallbacks("Oluşum Isıları ve Reaction")
        assert "Reaction" not in result
        assert "Tepkime" in result

    def test_sweep_all_caps_english(self):
        lt = self._get_translator()
        result = lt._sweep_single_word_fallbacks("İtme Kuvveti EQUATION")
        assert "EQUATION" not in result
        assert "Denklem" in result

    def test_sweep_preserves_punctuation(self):
        lt = self._get_translator()
        # Trailing comma must survive the replacement
        result = lt._sweep_single_word_fallbacks("İtme, Reaction,")
        assert "Reaction" not in result
        assert "," in result

    def test_sweep_unknown_english_untouched(self):
        lt = self._get_translator()
        # "Foobar" dict'te yok; dokunulmamalı
        result = lt._sweep_single_word_fallbacks("Temel Foobar")
        assert "Foobar" in result

    def test_sweep_numbers_untouched(self):
        lt = self._get_translator()
        result = lt._sweep_single_word_fallbacks("Bölüm 2.3 Reaction")
        assert "2.3" in result
        assert "Reaction" not in result


class TestTranslateCellB20:
    """``_translate_cell`` FAZ F1 cleanup davranışı."""

    def _get_translator(self):
        from latex_pipeline.translator import LatexTranslator
        return LatexTranslator.__new__(LatexTranslator)

    def test_leftover_reaction_translated(self):
        lt = self._get_translator()
        if not hasattr(lt, "_translate_cell"):
            pytest.skip("_translate_cell yok")
        # Heading-glossary'de "Reaction" tek başına yok; partial match varsa
        # stop-word + single-word sweep çalışır, yoksa adım 3.5 yakalar.
        result = lt._translate_cell("Oluşum Isıları ve Reaction")
        assert "Reaction" not in result, f"Got: {result!r}"
        assert "Tepkime" in result

    def test_leftover_equation(self):
        lt = self._get_translator()
        if not hasattr(lt, "_translate_cell"):
            pytest.skip("_translate_cell yok")
        result = lt._translate_cell("İtme Kuvveti EQUATION")
        assert "EQUATION" not in result, f"Got: {result!r}"

    def test_partial_turkish_all_caps_cleanup(self):
        lt = self._get_translator()
        if not hasattr(lt, "_translate_cell"):
            pytest.skip("_translate_cell yok")
        result = lt._translate_cell("Temel İlkeler CHEMICAL TERMODİNAMİK")
        assert "CHEMICAL" not in result, f"Got: {result!r}"

    def test_leftover_variation(self):
        lt = self._get_translator()
        if not hasattr(lt, "_translate_cell"):
            pytest.skip("_translate_cell yok")
        result = lt._translate_cell("Variation İtme Kuvveti ile")
        assert "Variation" not in result, f"Got: {result!r}"

    def test_leftover_effect(self):
        lt = self._get_translator()
        if not hasattr(lt, "_translate_cell"):
            pytest.skip("_translate_cell yok")
        result = lt._translate_cell("Effect Iraksama Açısı İtme")
        # Effect -> Etki/Etkisi beklenir
        assert "Effect" not in result or "Etki" in result, f"Got: {result!r}"

    def test_leftover_elements(self):
        lt = self._get_translator()
        if not hasattr(lt, "_translate_cell"):
            pytest.skip("_translate_cell yok")
        result = lt._translate_cell("Elements Roket İtişi")
        assert "Elements" not in result, f"Got: {result!r}"

    def test_leftover_aerothermochemistry(self):
        lt = self._get_translator()
        if not hasattr(lt, "_translate_cell"):
            pytest.skip("_translate_cell yok")
        result = lt._translate_cell("Aerothermochemistry Roket Motorları")
        assert "Aerothermochemistry" not in result, f"Got: {result!r}"

    def test_pure_english_does_not_crash(self):
        """Tamamen İngilizce hücre MT'ye düşmeli; single-word sweep
        tetiklenmeyecek çünkü Türkçe karakter yok. MT yoksa orijinali
        döndürebilir — en azından crash olmamalı."""
        lt = self._get_translator()
        if not hasattr(lt, "_translate_cell"):
            pytest.skip("_translate_cell yok")
        result = lt._translate_cell("Some Random Technical Title")
        assert isinstance(result, str)

    def test_turkish_only_cell_unchanged_or_valid(self):
        lt = self._get_translator()
        if not hasattr(lt, "_translate_cell"):
            pytest.skip("_translate_cell yok")
        # Zaten Türkçe, dokunulmamalı (ya da sadece minor)
        result = lt._translate_cell("Temel Tanımlar")
        assert isinstance(result, str)
        assert result.strip(), "Boş sonuç dönmemeli"

    def test_whitespace_preserved(self):
        lt = self._get_translator()
        if not hasattr(lt, "_translate_cell"):
            pytest.skip("_translate_cell yok")
        result = lt._translate_cell("  Variation İtme Kuvveti ile  ")
        assert result.startswith("  ")
        assert result.endswith("  ")
        assert "Variation" not in result

    def test_empty_cell(self):
        lt = self._get_translator()
        if not hasattr(lt, "_translate_cell"):
            pytest.skip("_translate_cell yok")
        assert lt._translate_cell("") == ""
        assert lt._translate_cell("   ") == "   "
