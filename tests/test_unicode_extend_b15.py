"""B15: Extended unicode artifact strip.

Marker PDF extraction bazı sayfalarda `U+FFFF` non-character, Arabic non-character
(U+FDD0-U+FDEF), private-use-supplementary (U+F0000-U+10FFFD) ve geometric emoji
(🟥 vs.) üretebiliyor. Bu test suite bu glyph'lerin `_normalize_unicode`
tarafından doğru strip/map edildiğini doğrular.
"""
import pytest


class TestExtendedUnicode:
    def _get_extractor(self):
        from latex_pipeline.extractor import MarkerExtractor
        from latex_pipeline.config import PipelineConfig
        cfg = PipelineConfig(input_pdf="/tmp/x.pdf")
        return MarkerExtractor(cfg)

    def test_ffff_stripped(self):
        ext = self._get_extractor()
        text = "Test" + chr(0xFFFF) + "End"
        out, stats = ext._normalize_unicode(text)
        assert out == "TestEnd"
        assert stats.get("noncharacter_stripped", 0) == 1

    def test_fffe_stripped(self):
        ext = self._get_extractor()
        text = "Test" + chr(0xFFFE) + "End"
        out, stats = ext._normalize_unicode(text)
        assert out == "TestEnd"
        assert stats.get("noncharacter_stripped", 0) == 1

    def test_fdd0_arabic_noncharacter(self):
        ext = self._get_extractor()
        text = "Test" + chr(0xFDD0) + "End"
        out, stats = ext._normalize_unicode(text)
        assert out == "TestEnd"
        assert stats.get("noncharacter_stripped", 0) == 1

    def test_fdef_arabic_noncharacter_upper_bound(self):
        ext = self._get_extractor()
        text = "Test" + chr(0xFDEF) + "End"
        out, stats = ext._normalize_unicode(text)
        assert out == "TestEnd"
        assert stats.get("noncharacter_stripped", 0) == 1

    def test_fdcf_just_before_arabic_noncharacter_preserved(self):
        """U+FDCF Arabic non-character range'den ÖNCE, silinmemeli."""
        ext = self._get_extractor()
        text = "Test" + chr(0xFDCF) + "End"
        out, stats = ext._normalize_unicode(text)
        assert chr(0xFDCF) in out
        assert stats.get("noncharacter_stripped", 0) == 0

    def test_fdf0_just_after_arabic_noncharacter_preserved(self):
        """U+FDF0 Arabic non-character range'den SONRA, silinmemeli."""
        ext = self._get_extractor()
        text = "Test" + chr(0xFDF0) + "End"
        out, stats = ext._normalize_unicode(text)
        assert chr(0xFDF0) in out

    def test_plane1_noncharacter(self):
        ext = self._get_extractor()
        text = "Test" + chr(0x1FFFE) + "End"
        out, stats = ext._normalize_unicode(text)
        assert out == "TestEnd"
        assert stats.get("noncharacter_stripped", 0) == 1

    def test_plane1_noncharacter_ffff(self):
        ext = self._get_extractor()
        text = "Test" + chr(0x1FFFF) + "End"
        out, stats = ext._normalize_unicode(text)
        assert out == "TestEnd"

    def test_plane10_noncharacter(self):
        ext = self._get_extractor()
        text = "Test" + chr(0x10FFFF) + "End"
        out, stats = ext._normalize_unicode(text)
        assert out == "TestEnd"

    def test_private_use_supp_a_stripped(self):
        ext = self._get_extractor()
        text = "Test" + chr(0xF0000) + "End"
        out, stats = ext._normalize_unicode(text)
        assert out == "TestEnd"
        assert stats.get("private_use_supp_stripped", 0) == 1

    def test_private_use_supp_b_stripped(self):
        ext = self._get_extractor()
        text = "Test" + chr(0x100000) + "End"
        out, stats = ext._normalize_unicode(text)
        assert out == "TestEnd"
        assert stats.get("private_use_supp_stripped", 0) == 1

    def test_red_square_emoji_mapped(self):
        """Gerçek bug senaryosu: TOC entry'de 🟥 geldi."""
        ext = self._get_extractor()
        text = "Bölüm 2 🟥 Rocket Engines"
        out, stats = ext._normalize_unicode(text)
        assert "🟥" not in out
        assert "-" in out
        assert stats.get("bullets_mapped", 0) >= 1

    def test_geometric_emoji_squares_mapped(self):
        ext = self._get_extractor()
        text = "🟥🟦🟩🟨🟧🟪🟫"
        out, stats = ext._normalize_unicode(text)
        assert all(e not in out for e in ["🟥", "🟦", "🟩", "🟨", "🟧", "🟪", "🟫"])
        assert stats.get("bullets_mapped", 0) == 7

    def test_geometric_emoji_circles_mapped(self):
        ext = self._get_extractor()
        text = "🟢🔴🔵"
        out, stats = ext._normalize_unicode(text)
        assert all(e not in out for e in ["🟢", "🔴", "🔵"])
        assert stats.get("bullets_mapped", 0) == 3

    def test_white_square_and_diamonds_mapped(self):
        ext = self._get_extractor()
        text = "□▫▪◆◇★☆❖"
        out, stats = ext._normalize_unicode(text)
        assert all(c not in out for c in "□▫▪◆◇★☆❖")
        assert stats.get("bullets_mapped", 0) == 8

    def test_regular_turkish_preserved(self):
        """KRİTİK: Türkçe harfler hiçbir şekilde etkilenmemeli."""
        ext = self._get_extractor()
        text = "Şekil çözüm öğeleri ığüÖÇŞĞÜİ"
        out, stats = ext._normalize_unicode(text)
        for ch in "ŞçözümöğeleriığüÖÇŞĞÜİ":
            assert ch in out, f"Türkçe karakter kayboldu: {ch!r}"
        assert stats.get("pua_stripped", 0) == 0
        assert stats.get("noncharacter_stripped", 0) == 0

    def test_mixed_turkish_with_noncharacter(self):
        """Gerçek TOC satırı simulasyonu."""
        ext = self._get_extractor()
        text = f"Bölüm 2 {chr(0xFFFF)} Rocket Engines'nın Aerotermokimyası"
        out, stats = ext._normalize_unicode(text)
        assert chr(0xFFFF) not in out
        assert "Bölüm" in out
        assert "Aerotermokimyası" in out
        assert stats.get("noncharacter_stripped", 0) == 1

    def test_preserve_tab_newline_cr(self):
        ext = self._get_extractor()
        text = "a\tb\nc\rd"
        out, stats = ext._normalize_unicode(text)
        assert out == "a\tb\nc\rd"
        assert stats.get("control_stripped", 0) == 0

    def test_stats_keys_present(self):
        ext = self._get_extractor()
        _, stats = ext._normalize_unicode("abc")
        for key in (
            "pua_stripped",
            "fffd_stripped",
            "bullets_mapped",
            "control_stripped",
            "noncharacter_stripped",
            "private_use_supp_stripped",
        ):
            assert key in stats, f"Eksik stat: {key}"
