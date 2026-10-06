"""B13: Tabular hücresi glossary-aware olmalı.

TOC marker içeriği `\\begin{tabular}` olarak yeniden yapılandırır. Hücre
çevirisi doğrudan MT'ye giderse `_HEADING_GLOSSARY` atlanır ve kısa TOC
başlıkları echo'lanır. Bu test `_translate_cell` helper'ının glossary-first
davranışını doğrular.
"""
import pytest


class TestTabularCellGlossary:
    """TOC entry'leri glossary ile çevrilmeli."""

    def _get_translator(self):
        """Minimal LatexTranslator — MT olmadan glossary test için."""
        from latex_pipeline.translator import LatexTranslator
        lt = LatexTranslator.__new__(LatexTranslator)
        return lt

    def test_basic_definitions_from_heading_glossary(self):
        lt = self._get_translator()
        if not hasattr(lt, '_translate_cell'):
            pytest.skip("_translate_cell henüz eklenmedi")
        result = lt._translate_cell("Basic Definitions")
        assert "Temel Tanımlar" in result, f"Got: {result!r}"

    def test_ideal_gas_mixture(self):
        lt = self._get_translator()
        if not hasattr(lt, '_translate_cell'):
            pytest.skip("_translate_cell henüz eklenmedi")
        result = lt._translate_cell("Ideal Gas Mixture")
        assert "İdeal Gaz Karışımı" in result, f"Got: {result!r}"

    def test_normal_shocks(self):
        lt = self._get_translator()
        if not hasattr(lt, '_translate_cell'):
            pytest.skip("_translate_cell henüz eklenmedi")
        result = lt._translate_cell("Normal Shocks")
        assert "Normal Şoklar" in result, f"Got: {result!r}"

    def test_numbered_entry_title_translated(self):
        lt = self._get_translator()
        if not hasattr(lt, '_translate_cell'):
            pytest.skip("_translate_cell henüz eklenmedi")
        result = lt._translate_cell("2.2.1 Basic Definitions")
        # Numara korunmalı, başlık çevrilmeli
        assert "2.2.1" in result, f"Numara kaybı: {result!r}"
        assert "Temel Tanımlar" in result, f"Başlık çevrilmedi: {result!r}"

    def test_numbered_entry_with_dot(self):
        lt = self._get_translator()
        if not hasattr(lt, '_translate_cell'):
            pytest.skip("_translate_cell henüz eklenmedi")
        result = lt._translate_cell("2.5.5 Normal Shocks")
        assert "2.5.5" in result
        assert "Normal Şoklar" in result, f"Got: {result!r}"

    def test_multi_word_partial_match(self):
        lt = self._get_translator()
        if not hasattr(lt, '_translate_cell'):
            pytest.skip("_translate_cell henüz eklenmedi")
        result = lt._translate_cell("Effective Exhaust Velocity")
        # Glossary'de "Effective Exhaust Velocity": "Etkin Egzoz Hızı" var
        assert "Etkin Egzoz Hızı" in result or "Egzoz Hızı" in result, (
            f"Got: {result!r}"
        )

    def test_preserves_whitespace(self):
        lt = self._get_translator()
        if not hasattr(lt, '_translate_cell'):
            pytest.skip("_translate_cell henüz eklenmedi")
        result = lt._translate_cell("  Basic Definitions  ")
        # leading/trailing whitespace korunmalı
        assert result.startswith("  "), f"Leading WS kayboldu: {result!r}"
        assert result.endswith("  "), f"Trailing WS kayboldu: {result!r}"
        assert "Temel Tanımlar" in result

    def test_empty_cell(self):
        lt = self._get_translator()
        if not hasattr(lt, '_translate_cell'):
            pytest.skip("_translate_cell henüz eklenmedi")
        assert lt._translate_cell("") == ""
        assert lt._translate_cell("   ") == "   "

    def test_unknown_cell_no_mt_available(self):
        """MT yoksa ve glossary'de de yoksa, cell_text döner (hata vermez)."""
        lt = self._get_translator()
        if not hasattr(lt, '_translate_cell'):
            pytest.skip("_translate_cell henüz eklenmedi")
        # "Quantum Tunneling Amplitude" glossary'de olmaması muhtemel
        result = lt._translate_cell("Quantum Tunneling Amplitude XYZ")
        # Exception yükseltmemeli; ya çevirmeli ya da aynen döndürmeli
        assert isinstance(result, str)
