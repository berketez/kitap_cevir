"""TOC hücre koruması testleri (2026-07-06 sutton smoke).

İki gerçek bug:
1. "xvii" gibi Roma sayfa numaraları MT'ye gidip söze çevriliyordu
   ("xvii" → "on yedi"). Artık `_is_non_translatable` bunları atlar.
2. "Required Mass of Pressurizing Gas / 214" kalıbındaki sayfa ayracını
   MT bölme işlemi sanıyordu ("... 214 ile böl"). Artık `/ 214` parçası
   `[[PGREF_n]]` olarak stash'lenir, MT'ye gitmez.
"""

import pytest
from unittest.mock import MagicMock

from core.translator import BookTranslator


@pytest.fixture()
def bt():
    mock = MagicMock(spec=BookTranslator)
    # Sınıf sabiti gerçek regex olmalı (MagicMock attr'ı truthy döner)
    mock._ROMAN_PAGE_RE = BookTranslator._ROMAN_PAGE_RE
    return mock


def _non_translatable(bt, text):
    return BookTranslator._is_non_translatable(bt, text)


class TestRomanPageNumerals:
    @pytest.mark.parametrize(
        "token", ["xvii", "vi", "ix", "iv", "xl", "xxxix", "lxxxviii", "xii"]
    )
    def test_roman_page_numbers_skipped(self, bt, token):
        assert _non_translatable(bt, token) is True

    @pytest.mark.parametrize(
        "token",
        [
            "mix",     # geçerli Roma grameri ama alfabe [ivxl] dışı (m)
            "civil",   # gerçek sözcük, gramer geçersiz
            "lid",     # gramer geçersiz
            "vip",     # gramer geçersiz
            "il",      # gramer geçersiz (i, l'den önce gelemez)
            "Mix",     # büyük harf — yalnız küçük harf sayfa numarası
            "XVII",    # büyük harf Roma — dokunma (başlık olabilir)
            "xi.",     # noktalama içeriyor
        ],
    )
    def test_words_and_uppercase_not_skipped(self, bt, token):
        assert _non_translatable(bt, token) is False

    def test_prose_containing_roman_not_skipped(self, bt):
        assert _non_translatable(bt, "vi editor is great") is False


class TestPageRefProtection:
    def test_protect_and_restore_roundtrip(self, bt):
        text = "Duct Jet Propulsion / 2"
        protected, refs = BookTranslator._protect_page_refs(bt, text)
        assert len(refs) == 1
        assert "[[PGREF_0]]" in protected
        assert "/ 2" not in protected
        restored = BookTranslator._restore_page_refs(bt, protected, refs)
        assert restored == text

    def test_multiple_refs_in_single_cell(self, bt):
        # gerçek vaka biçimi: tek hücreye sıkışmış çok girişli TOC satırı
        text = (
            "Factors Affecting the Required Volume of Cooling Water / 214 "
            "Simplified Method for the Volume of Cooling Water / 215"
        )
        protected, refs = BookTranslator._protect_page_refs(bt, text)
        assert len(refs) == 2
        assert "214" not in protected
        assert "215" not in protected
        assert BookTranslator._restore_page_refs(bt, protected, refs) == text

    def test_ref_at_line_end_with_punctuation(self, bt):
        text = "Water Pumping / 4, Centrifugal Water Pumping / 5"
        protected, refs = BookTranslator._protect_page_refs(bt, text)
        assert len(refs) == 2
        assert BookTranslator._restore_page_refs(bt, protected, refs) == text

    def test_prose_slash_roundtrip_lossless(self, bt):
        # Düz metindeki "3 / 4" da stash'lenir ama kayıpsız geri döner
        text = "oran 3 / 4 olarak verilir"
        protected, refs = BookTranslator._protect_page_refs(bt, text)
        assert BookTranslator._restore_page_refs(bt, protected, refs) == text

    def test_no_space_slash_untouched(self, bt):
        # km/h, URL path gibi bitişik / desenleri korunmaz (dokunulmaz)
        text = "hız km/h cinsinden, bkz http://example.com/404"
        protected, refs = BookTranslator._protect_page_refs(bt, text)
        assert protected == text
        assert refs == []
