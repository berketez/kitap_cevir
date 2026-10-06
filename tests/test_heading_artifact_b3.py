"""B3 fix testleri: Heading içinde kalan glossary placeholder artifact'leri.

Kanıt (bir test kitabının çevirisinde):
    7.7 Katı-MİYOR0000@@

Beklenen: @@TERMnnnn@@ / <<TERM_N>> / TERM_N fragment'leri heading
çıktısında görünmemeli.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

# Proje kökü path'e
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from latex_pipeline.translator import (  # noqa: E402
    _ARTIFACT_TERM_RE,
    _ARTIFACT_PLACEHOLDER_RE,
    LatexTranslator,
)


# =============================================================================
# 1. Module-level regex testleri
# =============================================================================

class TestArtifactTermRegex:
    """_ARTIFACT_TERM_RE üç formatı da yakalamalı."""

    def test_regex_catches_at_format(self):
        assert _ARTIFACT_TERM_RE.search("Text @@TERM0000@@ here")

    def test_regex_catches_at_format_multi_digit(self):
        assert _ARTIFACT_TERM_RE.search("Test @@TERM12345@@ content")

    def test_regex_catches_angle_format(self):
        assert _ARTIFACT_TERM_RE.search("Text <<TERM_5>> here")

    def test_regex_catches_bare_format(self):
        assert _ARTIFACT_TERM_RE.search("Text TERM_3 here")

    def test_regex_no_false_positive_plain(self):
        assert not _ARTIFACT_TERM_RE.search("Normal heading without artifacts")

    def test_regex_no_false_positive_similar_words(self):
        # "Terminal" / "Term" gibi normal sözcükler yakalanmamalı
        assert not _ARTIFACT_TERM_RE.search("Terminal velocity")
        assert not _ARTIFACT_TERM_RE.search("Term and condition")

    def test_regex_catches_mixed_in_heading(self):
        # Gerçek dünya kanıt: "Katı-MİYOR0000@@" benzeri
        # Bu tam olarak yakalanmaz (MİYOR ≠ TERM) ama @@TERM0000@@ yakalanır.
        assert _ARTIFACT_TERM_RE.search("Katı-@@TERM0000@@ Yakıtlar")


class TestArtifactPlaceholderRegex:
    """_ARTIFACT_PLACEHOLDER_RE body için LATEX/CMD/TERM hepsini yakalamalı."""

    def test_latex_placeholder(self):
        assert _ARTIFACT_PLACEHOLDER_RE.search("body <<LATEX_42>> text")

    def test_cmd_placeholder(self):
        assert _ARTIFACT_PLACEHOLDER_RE.search("body <<CMD_7>> text")

    def test_term_angle(self):
        assert _ARTIFACT_PLACEHOLDER_RE.search("body <<TERM_3>> text")

    def test_term_at_format(self):
        assert _ARTIFACT_PLACEHOLDER_RE.search("body @@TERM0000@@ text")

    def test_bracket_format(self):
        assert _ARTIFACT_PLACEHOLDER_RE.search("body [[UNIT_5]] text")


# =============================================================================
# 2. _resolve_term_placeholders_for_heading — MT hallüsinasyon formatı
# =============================================================================

def _make_lt_with_map(term_map=None):
    """LatexTranslator init'ini bypass ederek sadece gerekli attr'leri set
    eden hafif fake oluşturur.  _resolve_term_placeholders_for_heading
    sadece self._glossary_term_map ve self.logger kullanıyor, full init
    gerekmez — unbound call'la test ediyoruz.
    """
    fake = SimpleNamespace()
    fake._glossary_term_map = term_map or {}
    # Method'u bound gibi çağır
    return fake


class TestResolveTermPlaceholders:
    """_resolve_term_placeholders_for_heading: tüm formatları yakalamalı."""

    def test_resolves_angle_format(self):
        tm = SimpleNamespace(en_term="thrust", tr_term="itki")
        fake = _make_lt_with_map({5: tm})
        result = LatexTranslator._resolve_term_placeholders_for_heading(
            fake, "Heading with <<TERM_5>> here"
        )
        assert "itki" in result
        assert "<<TERM_" not in result

    def test_resolves_at_format(self):
        """B3 kritik: @@TERM0000@@ formatı resolve edilmeli."""
        tm = SimpleNamespace(en_term="solid-propellant", tr_term="Katı-Yakıt")
        fake = _make_lt_with_map({0: tm})
        result = LatexTranslator._resolve_term_placeholders_for_heading(
            fake, "Section @@TERM0000@@ operation"
        )
        assert "Katı-Yakıt" in result
        assert "@@TERM" not in result

    def test_resolves_bare_format(self):
        tm = SimpleNamespace(en_term="nozzle", tr_term="lüle")
        fake = _make_lt_with_map({3: tm})
        result = LatexTranslator._resolve_term_placeholders_for_heading(
            fake, "Title TERM_3 continue"
        )
        assert "lüle" in result

    def test_strips_unresolved_artifact(self):
        """Map'te olmayan placeholder silinmeli (eskiden as-is bırakılıyordu)."""
        fake = _make_lt_with_map({})  # boş map
        result = LatexTranslator._resolve_term_placeholders_for_heading(
            fake, "Section @@TERM9999@@ text"
        )
        assert "@@TERM" not in result
        assert "TERM_" not in result

    def test_no_artifact_passthrough(self):
        fake = _make_lt_with_map({})
        text = "Normal heading text"
        result = LatexTranslator._resolve_term_placeholders_for_heading(fake, text)
        assert result == text

    def test_empty_input(self):
        fake = _make_lt_with_map({})
        assert LatexTranslator._resolve_term_placeholders_for_heading(fake, "") == ""

    def test_real_world_broken_heading(self):
        """Gerçek kanıt: 'Katı-MİYOR0000@@' benzeri bir girdi.

        MİYOR kısmı TERM değil (MT hallüsinasyonu sonucu başka bir kelime).
        Bu durumda @@TERM0000@@ formatı YOKSA regex hiçbir şey yakalamaz —
        doğru davranış.  Fakat @@TERM0000@@ olarak gelmişse resolve edilmeli.
        """
        tm = SimpleNamespace(en_term="solid-propellant", tr_term="Katı-Yakıt")
        fake = _make_lt_with_map({0: tm})
        # B3 scenario: MT'nin ürettiği bozuk format
        result = LatexTranslator._resolve_term_placeholders_for_heading(
            fake, "Katı-@@TERM0000@@ Yakıtlar"
        )
        # Term resolve edildiyse @@TERM kalmamalı
        assert "@@TERM" not in result
        # Resolve sonucu Türkçe term burada olmalı
        assert "Katı-Yakıt" in result


# =============================================================================
# 3. _sanitize_heading_artifacts — output guard
# =============================================================================

class TestSanitizeHeadingArtifacts:
    """Son-savunma katmanı: output'ta artifact kaldıysa temizlenmeli."""

    def test_empty_input(self):
        fake = SimpleNamespace()
        assert LatexTranslator._sanitize_heading_artifacts(fake, "") == ""

    def test_passthrough_clean(self):
        fake = SimpleNamespace()
        text = "Katı-Yakıt Roket Motoru"
        assert LatexTranslator._sanitize_heading_artifacts(fake, text) == text

    def test_strips_at_artifact(self):
        fake = SimpleNamespace()
        result = LatexTranslator._sanitize_heading_artifacts(
            fake, "Katı-@@TERM0000@@ Yakıtlar"
        )
        assert "@@TERM" not in result
        # "Katı" ve "Yakıtlar" kalmalı
        assert "Katı" in result
        assert "Yakıtlar" in result

    def test_strips_angle_artifact(self):
        fake = SimpleNamespace()
        result = LatexTranslator._sanitize_heading_artifacts(
            fake, "Test <<TERM_5>> heading"
        )
        assert "<<TERM_" not in result

    def test_strips_bare_artifact(self):
        fake = SimpleNamespace()
        result = LatexTranslator._sanitize_heading_artifacts(
            fake, "Title TERM_3 continue"
        )
        assert "TERM_3" not in result

    def test_cleans_double_spaces(self):
        fake = SimpleNamespace()
        result = LatexTranslator._sanitize_heading_artifacts(
            fake, "A @@TERM0001@@ B"
        )
        assert "  " not in result  # çift boşluk olmamalı

    def test_cleans_dangling_dash(self):
        fake = SimpleNamespace()
        result = LatexTranslator._sanitize_heading_artifacts(
            fake, "Katı-@@TERM0000@@"
        )
        # Başta/sonda tire kalmamalı
        assert not result.endswith("-")
        assert "@@TERM" not in result
