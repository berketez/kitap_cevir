"""B2 fix testleri: ALL-CAPS heading tespiti num-prefix sonrasında çalışsın.

Kanıt biçimi (sentetik):
    \\section*{7.6 FLOW MODEL FOR HIGH-PRESSURE PUMPING}
    \\section*{7.8 INTERNAL DYNAMICS OF HPRE}

Beklenen: Num-prefix ayrıldıktan sonra title_text ALL-CAPS ise
title case'e dönüşüp MT'ye öyle gönderilmeli.  Strict `text==upper()`
yerine %80 büyük harf oranı kullanılmalı — "FLOW MODEL for X"
gibi minor lowercase kelimelerde de tetiklenmeli.
"""

import sys
from pathlib import Path

# Proje kökü path'e
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from latex_pipeline.translator import (  # noqa: E402
    _smart_title_case,
    _is_effectively_all_caps,
)


# =============================================================================
# 1. _is_effectively_all_caps helper testleri
# =============================================================================

class TestIsEffectivelyAllCaps:
    """B2: ALL-CAPS algilama >=%80 buyuk harf oranina dayanmali."""

    def test_pure_all_caps(self):
        assert _is_effectively_all_caps("FLOW MODEL FOR PUMPING") is True

    def test_minor_lowercase_word(self):
        # "for" lowercase olsa da cogunluk buyuk harf
        assert _is_effectively_all_caps("FLOW MODEL for PUMPING") is True

    def test_title_case_false(self):
        assert _is_effectively_all_caps("Thermal Model For Burning") is False

    def test_all_lowercase_false(self):
        assert _is_effectively_all_caps("thermal model for burning") is False

    def test_too_short_returns_false(self):
        # 3'ten az harf
        assert _is_effectively_all_caps("AB") is False
        assert _is_effectively_all_caps("A") is False
        assert _is_effectively_all_caps("") is False

    def test_numbers_and_punct_ignored(self):
        # Sayi ve noktalama oran hesabina girmemeli
        assert _is_effectively_all_caps("7.6 FLOW MODEL") is True

    def test_exactly_80_percent(self):
        # 5 harfin 4'u upper = %80 -> True
        # "ABCDe" -> 4 upper / 5 letter = 0.8
        assert _is_effectively_all_caps("ABCDe") is True

    def test_below_80_percent(self):
        # 5 harfin 3'u upper = %60 -> False
        assert _is_effectively_all_caps("ABCde") is False

    def test_mixed_with_hyphen(self):
        # Hyphen alfabetik degil, sayilmaz; harflerin hepsi upper
        assert _is_effectively_all_caps("HIGH-PRESSURE PUMPING") is True


# =============================================================================
# 2. _smart_title_case hyphen-aware testleri
# =============================================================================

class TestSmartTitleCaseHyphen:
    """B2: str.title() hyphen-aware olmali (Python default davranisi)."""

    def test_single_hyphen_word(self):
        assert _smart_title_case("HIGH-PRESSURE") == "High-Pressure"

    def test_hyphen_in_phrase(self):
        expected = "Flow Model For High-Pressure Pumping"
        assert _smart_title_case("FLOW MODEL FOR HIGH-PRESSURE PUMPING") == expected

    def test_multi_hyphen(self):
        # "STATE-OF-THE-ART" -> "State-Of-The-Art"
        assert _smart_title_case("STATE-OF-THE-ART") == "State-Of-The-Art"

    def test_acronym_preserved(self):
        # GPU acronym listede -> upper kalir
        assert _smart_title_case("GPU PERFORMANCE") == "GPU Performance"

    def test_internal_dynamics_of_hpre(self):
        # Kanit metni: "INTERNAL DYNAMICS OF HPRE"
        # HPRE acronym listede degil -> title case
        result = _smart_title_case("INTERNAL DYNAMICS OF HPRE")
        assert result == "Internal Dynamics Of Hpre", f"Got: {result!r}"


# =============================================================================
# 3. Integration: num-prefix + ALL-CAPS beraber davranislari
# =============================================================================

class TestNumPrefixAllCapsIntegration:
    """Num-prefix ayrildiktan sonra title_text uzerinde helper'lar calismali.

    _translate_heading_text icinde:
      text_clean = title_text  (num-prefix siyirildigindan sonra)
      if _is_effectively_all_caps(text_clean) and len>5:
          text_for_mt = _smart_title_case(text_clean)

    Boylece MT'ye title case input gider; MT title case echo yaparsa
    bile sonuc okunabilir (ve echo tespit `text_for_mt` karsilastirmasi
    ile yakalanir).
    """

    def test_title_text_after_num_prefix_is_detected(self):
        # Sadece title_text ALL-CAPS check'i simule et
        title_text = "FLOW MODEL FOR HIGH-PRESSURE PUMPING"
        assert _is_effectively_all_caps(title_text) is True
        assert len(title_text) > 5

        text_for_mt = _smart_title_case(title_text)
        assert text_for_mt == "Flow Model For High-Pressure Pumping"

    def test_already_title_case_not_modified(self):
        # Zaten title case olan basliklar dokunulmamali
        title_text = "Thermal Model For Burning"
        assert _is_effectively_all_caps(title_text) is False

    def test_internal_ballistics_spre(self):
        title_text = "INTERNAL DYNAMICS OF HPRE"
        assert _is_effectively_all_caps(title_text) is True
        assert len(title_text) > 5
        text_for_mt = _smart_title_case(title_text)
        # Tum kelimeler title case olmali
        for word in text_for_mt.split():
            # Acronym degilse ilk harf upper, digerleri lower olmali
            if word.upper() not in {
                "GPU", "CPU", "API", "IEEE", "NASA", "ASME", "AIAA",
            }:
                assert word[0].isupper(), f"Ilk harf upper degil: {word!r}"
                if len(word) > 1:
                    # Hyphen-split ile ilk harf upper geri kalan lower
                    for part in word.split("-"):
                        if part:
                            assert part[0].isupper(), \
                                f"Hyphen-part ilk harf upper degil: {part!r}"
