"""Tests for heading translation: glossary correctness, Title Case, ASCII fix.

Covers:
- _HEADING_GLOSSARY accuracy (overexpansion vs underexpansion, representation learning)
- _turkish_title_case behaviour (small words, Turkish chars, first word)
- _fix_turkish_ascii coverage (common MT corruptions)
"""

import sys
from pathlib import Path

# Ensure project root on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from latex_pipeline.translator import LatexTranslator


# ---------------------------------------------------------------------------
# Helpers -- access class-level dict / static methods without full init
# ---------------------------------------------------------------------------

_GLOSSARY = LatexTranslator._HEADING_GLOSSARY
_title_case = LatexTranslator._turkish_title_case
_fix_ascii = LatexTranslator._fix_turkish_ascii


# ===========================================================================
# 1. Heading Glossary Correctness
# ===========================================================================

class TestHeadingGlossary:
    """Verify key glossary entries are correct and unambiguous."""

    # -- Overexpansion vs Underexpansion (must NOT be swapped) --
    def test_overexpansion_correct(self):
        assert _GLOSSARY["Overexpansion"] == "Asiri Genisleme".replace(
            "Asiri", "A\u015f\u0131r\u0131"
        ).replace("Genisleme", "Geni\u015fleme")
        # Simpler: direct comparison
        assert _GLOSSARY["Overexpansion"] == "A\u015f\u0131r\u0131 Geni\u015fleme"

    def test_underexpansion_correct(self):
        assert _GLOSSARY["Underexpansion"] == "Yetersiz Geni\u015fleme"

    def test_overexpansion_not_equal_underexpansion(self):
        """The two MUST have different translations."""
        assert _GLOSSARY["Overexpansion"] != _GLOSSARY["Underexpansion"]

    # -- Representation Learning vs Deep Learning --
    def test_representation_learning(self):
        assert _GLOSSARY["Representation Learning"] == "Temsil \u00d6\u011frenimi"

    def test_representation_learning_lowercase(self):
        assert _GLOSSARY["representation learning"] == "Temsil \u00d6\u011frenimi"

    def test_derin_ogrenme_farkli(self):
        assert _GLOSSARY["Deep Learning"] == "Derin \u00d6\u011frenme"
        assert _GLOSSARY["Representation Learning"] != _GLOSSARY["Deep Learning"]

    # -- Weight-Space Symmetries --
    def test_weight_space_hyphen(self):
        assert _GLOSSARY["Weight-Space Symmetries"] == "A\u011f\u0131rl\u0131k Uzay\u0131 Simetrileri"

    def test_weight_space_no_hyphen(self):
        assert _GLOSSARY["Weight Space Symmetries"] == "A\u011f\u0131rl\u0131k Uzay\u0131 Simetrileri"

    def test_weight_space_lower(self):
        assert _GLOSSARY["weight-space symmetries"] == "A\u011f\u0131rl\u0131k Uzay\u0131 Simetrileri"

    # -- UPPERCASE variants --
    def test_uppercase_overexpansion(self):
        assert _GLOSSARY["OVEREXPANSION"] == "A\u015eIRI GEN\u0130\u015eLEME"

    def test_uppercase_underexpansion(self):
        assert _GLOSSARY["UNDEREXPANSION"] == "YETERS\u0130Z GEN\u0130\u015eLEME"


# ===========================================================================
# 2. Turkish Title Case
# ===========================================================================

class TestTurkishTitleCase:
    """Verify _turkish_title_case handles Turkish chars and small words."""

    def test_basic(self):
        assert _title_case("dikkat mekanizmasi") == "Dikkat Mekanizmasi"

    def test_turkish_i(self):
        # 'i' should become 'I' with dot above (Turkish uppercase I)
        result = _title_case("ikinci iterasyon")
        assert result.startswith("\u0130")  # starts with I-dot

    def test_small_words_lowercase(self):
        result = _title_case("sinir aglari ve destek vektor makineleri")
        # "ve" should stay lowercase
        assert " ve " in result

    def test_first_word_always_capitalized(self):
        # Even if first word is a small word, capitalize it
        result = _title_case("ve sonuc")
        assert result[0] == "V"

    def test_empty_string(self):
        assert _title_case("") == ""

    def test_single_word(self):
        result = _title_case("optimizasyon")
        assert result == "Optimizasyon"

    def test_already_correct(self):
        assert _title_case("Derin Aglar") == "Derin Aglar"

    def test_turkish_special_chars(self):
        # ş -> Ş, ö -> Ö, ü -> Ü, ç -> Ç, ğ -> Ğ
        result = _title_case("\u015fekil \u00f6zeti \u00fcretim \u00e7\u0131k\u0131\u015f \u011firi\u015f")
        assert result[0] == "\u015e"  # S-cedilla uppercase

    def test_cd_lulesinde(self):
        result = _title_case("CD l\u00fclesinde a\u015f\u0131r\u0131 geni\u015fleme")
        # CD stays uppercase, "l" -> title case
        assert "CD" in result
        assert result.startswith("CD")


# ===========================================================================
# 3. ASCII Fix
# ===========================================================================

class TestFixTurkishAscii:
    """Verify _fix_turkish_ascii catches common MT corruptions."""

    def test_aglar(self):
        assert _fix_ascii("Derin Aglar") == "Derin A\u011flar"

    def test_ogrenme(self):
        assert _fix_ascii("Derin Ogrenme") == "Derin \u00d6\u011frenme"

    def test_giris(self):
        # "Giris" -> "Giris" (s->s-cedilla), "Bolum" substring in "Bolumu"
        # gets fixed but trailing "u" stays as-is (suffix is not in scope)
        result = _fix_ascii("Giris Bolumu")
        assert "Giri\u015f" in result  # Giris -> Giris with cedilla
        assert "B\u00f6l\u00fcm" in result  # Bolum -> Bolum with umlauts

    def test_genisleme(self):
        assert _fix_ascii("Asiri Genisleme") == "Asiri Geni\u015fleme"

    def test_gosterim(self):
        assert _fix_ascii("Gosterim Ogrenimi") == "G\u00f6sterim \u00d6\u011frenimi"

    def test_isaret(self):
        assert _fix_ascii("Isaret Islemi") == "\u0130\u015faret \u0130\u015flemi"

    def test_istatistik(self):
        assert _fix_ascii("Istatistik Analizi") == "\u0130statistik Analizi"

    def test_no_false_positive(self):
        # Text without corruptions should pass through unchanged
        clean = "Derin A\u011flar ve Sinir A\u011flar\u0131"
        assert _fix_ascii(clean) == clean

    def test_multiple_fixes(self):
        text = "Aglar ve Ogrenme icin Yaklasim"
        result = _fix_ascii(text)
        assert "A\u011flar" in result
        assert "\u00d6\u011frenme" in result
        assert "Yakla\u015f\u0131m" in result

    def test_bolum(self):
        assert _fix_ascii("Bolum 1") == "B\u00f6l\u00fcm 1"

    def test_butunluk(self):
        assert _fix_ascii("Veri Butunluk Kontrolu") == "Veri B\u00fct\u00fcnl\u00fck Kontrolu"


# ===========================================================================
# 4. Integration: Glossary lookup is case-insensitive
# ===========================================================================

class TestGlossaryCaseInsensitive:
    """The _translate_heading_text method does case-insensitive lookup.
    Here we just verify that the glossary has entries for common variants."""

    def test_lowercase_lookup_present(self):
        """At minimum, case-insensitive exact match should work.
        The code does .lower() comparison, so any-case input matches."""
        # Build a lowercase version of all keys
        lower_keys = {k.lower() for k in _GLOSSARY}
        assert "overexpansion" in lower_keys
        assert "underexpansion" in lower_keys
        assert "representation learning" in lower_keys
        assert "weight-space symmetries" in lower_keys
        assert "deep learning" in lower_keys
        assert "attention mechanism" in lower_keys

# ===========================================================================
# 6. Author Name Protection (et al. with \textit{})
# ===========================================================================

class TestAuthorEtalRegex:
    """Verify _AUTHOR_ETAL_RE catches both bare and textit-wrapped et al."""

    def test_bare_et_al(self):
        from latex_pipeline.translator import _AUTHOR_ETAL_RE
        m = _AUTHOR_ETAL_RE.search("Hosterman et al.")
        assert m is not None
        assert m.group(0) == "Hosterman et al."

    def test_textit_et_al(self):
        from latex_pipeline.translator import _AUTHOR_ETAL_RE
        m = _AUTHOR_ETAL_RE.search(r"Hosterman \textit{et al.}")
        assert m is not None
        assert r"\textit{et al.}" in m.group(0)

    def test_textit_et_al_with_year(self):
        from latex_pipeline.translator import _AUTHOR_ETAL_RE
        m = _AUTHOR_ETAL_RE.search(r"Hosterman \textit{et al.} (2021)")
        assert m is not None
        assert "2021" in m.group(0)

    def test_multi_author_textit_et_al(self):
        from latex_pipeline.translator import _AUTHOR_ETAL_RE
        m = _AUTHOR_ETAL_RE.search(r"Kingma and Welling \textit{et al.}")
        assert m is not None
        assert "Kingma" in m.group(0)
        assert "Welling" in m.group(0)


class TestParenRefTextitEtal:
    """Verify _PAREN_REF_RE catches parenthetical refs with textit et al."""

    def test_paren_textit_et_al(self):
        from latex_pipeline.translator import _PAREN_REF_RE
        m = _PAREN_REF_RE.search(r"(Hosterman \textit{et al.}, 2021)")
        assert m is not None
        assert "Hosterman" in m.group(0)
        assert "2021" in m.group(0)

    def test_paren_bare_et_al_still_works(self):
        from latex_pipeline.translator import _PAREN_REF_RE
        m = _PAREN_REF_RE.search("(Goodacre et al., 2016)")
        assert m is not None
        assert "Goodacre" in m.group(0)

    def test_paren_multi_ref_with_textit(self):
        from latex_pipeline.translator import _PAREN_REF_RE
        text = r"(Hosterman \textit{et al.}, 2021; Goodacre et al., 2016)"
        m = _PAREN_REF_RE.search(text)
        assert m is not None
        assert "Hosterman" in m.group(0)
        assert "Goodacre" in m.group(0)
