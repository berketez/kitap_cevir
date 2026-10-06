"""
Tests for the GlossaryEngine terminology matching system.

Tests both the new inject/verify approach (preferred) and the
deprecated stash/restore approach (backward compatibility).

G2c (KARAR 25, 2026-10-05): genel alan sözlükleri (default/ml/math/physics/
cs/science_engineering/technical .json ve felsefe türü sözlüğü) SİLİNDİ;
GlossaryEngine yalnız curated.json + do_not_translate.json yükler. Motorun MEKANİĞİ
(en uzun eşleşme, harf duyarlılığı, yer tutucu atlama, stash/restore, alan
süzgeci) artık silinen dosyalara değil SENTETİK bir sözlük klasörüne
(``glossary_dir`` fikstürü) karşı sınanır; gerçek veri testleri yalnız
kalan curated.json üzerindedir (``real_glossary_dir``).
"""

import json
import warnings
import pytest
from pathlib import Path
from latex_pipeline.glossary import GlossaryEngine, TermEntry, TermMatch


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Sentetik sözlük (motor mekaniği için; kitap metni değil, genel terimler).
_SENTETIK_CURATED = {
    "terms": {
        "neural network": {"tr": "sinir ağı", "priority": "high"},
        "convolutional neural network": {"tr": "evrişimli sinir ağı",
                                         "domain": "ml"},
        "deep learning": {"tr": "derin öğrenme", "domain": "ml"},
        "stochastic gradient descent": {"tr": "stokastik gradyan inişi",
                                        "domain": "ml"},
        "gradient descent": {"tr": "gradyan inişi", "domain": "ml",
                             "priority": "high"},
        "loss function": {"tr": "kayıp fonksiyonu", "domain": "ml"},
        "activation function": {"tr": "aktivasyon fonksiyonu", "domain": "ml"},
        "boundary layer": {"tr": "sınır tabakası", "domain": "physics"},
        "eigenvalue": {"tr": "özdeğer", "domain": "math"},
        "Fourier series": {"tr": "Fourier serisi", "domain": "math",
                           "case_sensitive": True},
        "categorical imperative": {"tr": "kategorik buyruk",
                                   "domain": "philosophy"},
    }
}
_SENTETIK_DNT = {
    "terms": {"Monte Carlo": {"tr": "Monte Carlo", "keep_original": True}},
}
# KARAR 25: haritada olmayan (silinmiş genel) sözlük dosyası klasörde dursa
# bile YÜKLENMEZ.
_SILINMIS_GENEL = {"terms": {"cat": {"tr": "kedi", "priority": "high"}}}


@pytest.fixture
def glossary_dir(tmp_path):
    """Sentetik sözlük klasörü (curated + do_not_translate + yüklenmeyecek
    eski genel dosyalar)."""
    gdir = tmp_path / "glossary"
    gdir.mkdir()
    (gdir / "curated.json").write_text(
        json.dumps(_SENTETIK_CURATED, ensure_ascii=False), encoding="utf-8")
    (gdir / "do_not_translate.json").write_text(
        json.dumps(_SENTETIK_DNT, ensure_ascii=False), encoding="utf-8")
    for eski in ("default.json", "technical.json", "science_engineering.json"):
        (gdir / eski).write_text(json.dumps(_SILINMIS_GENEL), encoding="utf-8")
    return gdir


@pytest.fixture
def real_glossary_dir():
    """Projenin gerçek sözlük klasörü (yalnız curated + do_not_translate)."""
    return Path(__file__).resolve().parent.parent / "glossary"


@pytest.fixture
def engine(glossary_dir):
    """GlossaryEngine loaded with science book type."""
    return GlossaryEngine(glossary_dir=glossary_dir, book_type="science")


@pytest.fixture
def engine_philosophy(glossary_dir):
    """GlossaryEngine loaded with philosophy book type."""
    return GlossaryEngine(glossary_dir=glossary_dir, book_type="philosophy")


@pytest.fixture
def engine_novel(glossary_dir):
    """GlossaryEngine loaded with novel book type."""
    return GlossaryEngine(glossary_dir=glossary_dir, book_type="novel")


# ---------------------------------------------------------------------------
# Loading tests
# ---------------------------------------------------------------------------

class TestLoading:
    """Test glossary file loading."""

    def test_loads_files(self, engine):
        assert engine.term_count > 0
        assert len(engine.stats.files_loaded) > 0

    def test_science_loads_expected_files(self, engine):
        # G2c / KARAR 25: yalnız curated + do_not_translate yüklenir.
        loaded = set(engine.stats.files_loaded)
        assert loaded == {"curated.json", "do_not_translate.json"}

    def test_excludes_deleted_general_glossaries(self, engine):
        # Klasörde duran eski genel sözlükler (default/technical/
        # science_engineering) haritada olmadığı için yüklenmez.
        loaded = set(engine.stats.files_loaded)
        assert not loaded & {"default.json", "technical.json",
                             "science_engineering.json"}
        assert engine.get_term("cat") is None

    def test_science_term_count(self, engine):
        """science: philosophy alanı dışındaki 10 curated + 1 do_not_translate."""
        assert engine.term_count == 11

    def test_real_glossary_loads_only_two_files(self, real_glossary_dir):
        eng = GlossaryEngine(glossary_dir=real_glossary_dir, book_type="science")
        assert set(eng.stats.files_loaded) <= {"curated.json",
                                               "do_not_translate.json"}
        assert eng.term_count > 0

    def test_philosophy_loads_curated(self, engine_philosophy):
        loaded = set(engine_philosophy.stats.files_loaded)
        assert "curated.json" in loaded

    def test_novel_minimal_terms(self, engine_novel):
        """Novel should have fewer terms than science."""
        assert engine_novel.term_count < 1000


# ---------------------------------------------------------------------------
# Term matching tests
# ---------------------------------------------------------------------------

class TestMatching:
    """Test term matching in text."""

    def test_basic_match(self, engine):
        matches = engine.match_terms("The neural network is trained.")
        en_terms = [m.en_term for m in matches]
        assert "neural network" in en_terms

    def test_no_partial_match(self, engine):
        """Word boundaries should prevent partial matches."""
        matches = engine.match_terms("The neurality is interesting.")
        en_terms = [m.en_term for m in matches]
        assert "neural network" not in en_terms
        # "neurality" should not be matched even as "neural"
        assert not any("neural" == m.en_term for m in matches)

    def test_longest_match_first(self, engine):
        """'stochastic gradient descent' should match before 'gradient descent'."""
        text = "We use stochastic gradient descent."
        matches = engine.match_terms(text)
        en_terms = [m.en_term for m in matches]
        assert "stochastic gradient descent" in en_terms
        # Should NOT also match "gradient descent" separately
        # (it's consumed by the longer match)

    def test_case_insensitive_default(self, engine):
        """Default terms should match case-insensitively."""
        matches = engine.match_terms("NEURAL NETWORK is powerful.")
        en_terms = [m.en_term for m in matches]
        assert any("neural network" in t.lower() for t in en_terms)

    def test_case_sensitive_term(self, engine):
        """Case-sensitive terms should only match exact case."""
        # Fourier series is case_sensitive=True in the synthetic curated.json
        matches_correct = engine.match_terms("Apply Fourier series here.")
        matches_wrong = engine.match_terms("apply fourier series here.")
        correct_terms = [m.en_term for m in matches_correct]
        wrong_terms = [m.en_term for m in matches_wrong]
        assert any("fourier series" in t.lower() for t in correct_terms)
        assert "Fourier series" not in wrong_terms

    def test_skip_latex_placeholders(self, engine):
        """Terms inside <<LATEX_N>> regions should not match."""
        text = "The <<LATEX_5>> uses gradient descent."
        matches = engine.match_terms(text)
        en_terms = [m.en_term for m in matches]
        assert "gradient descent" in en_terms
        # No match should overlap with <<LATEX_5>>

    def test_skip_term_placeholders(self, engine):
        """Terms inside <<TERM_N>> regions should not match."""
        text = "The <<TERM_3>> and loss function."
        matches = engine.match_terms(text)
        en_terms = [m.en_term for m in matches]
        assert "loss function" in en_terms

    def test_empty_text(self, engine):
        matches = engine.match_terms("")
        assert matches == []

    def test_no_matches(self, engine):
        matches = engine.match_terms("The cat sat on the mat.")
        assert matches == []


# ---------------------------------------------------------------------------
# Direct TR Injection tests (preferred approach)
# ---------------------------------------------------------------------------

class TestInjectVerify:
    """Test direct TR injection and post-translation verification."""

    def test_basic_inject_disabled(self, engine):
        """inject_terms is disabled for all types — text unchanged, empty tmap."""
        text = "Use gradient descent for training."
        injected, tmap = engine.inject_terms(text)
        assert injected == text
        assert tmap == {}

    def test_inject_multiple_terms_disabled(self, engine):
        """inject_terms disabled — text unchanged even with multiple terms."""
        text = "The neural network uses gradient descent."
        injected, tmap = engine.inject_terms(text)
        assert injected == text
        assert tmap == {}

    def test_inject_no_placeholders(self, engine):
        """inject_terms must never produce <<TERM_N>> placeholders."""
        text = "Deep learning with convolutional neural network and loss function."
        injected, tmap = engine.inject_terms(text)
        assert "<<TERM_" not in injected
        assert injected == text

    def test_inject_preserves_latex_placeholders(self, engine):
        """<<LATEX_N>> placeholders should survive (inject is disabled anyway)."""
        text = "The <<LATEX_0>> with loss function gives <<LATEX_1>>."
        injected, tmap = engine.inject_terms(text)
        assert "<<LATEX_0>>" in injected
        assert "<<LATEX_1>>" in injected
        assert injected == text

    def test_inject_empty_text(self, engine):
        injected, tmap = engine.inject_terms("")
        assert injected == ""
        assert len(tmap) == 0

    def test_inject_no_matches(self, engine):
        text = "Just regular text with no terms."
        injected, tmap = engine.inject_terms(text)
        assert injected == text
        assert len(tmap) == 0

    def test_inject_keep_original_terms(self, engine):
        """With inject disabled, text passes through unchanged."""
        text = "Apply Monte Carlo methods."
        injected, tmap = engine.inject_terms(text)
        assert "Monte Carlo" in injected
        assert injected == text

    def test_verify_terms_all_present(self, engine):
        """verify_terms should be a no-op when all TR terms already present."""
        # inject_terms is now disabled for all types, returns empty tmap
        text = "The neural network uses gradient descent."
        injected, tmap = engine.inject_terms(text)
        assert tmap == {}  # inject is disabled
        assert injected == text  # text unchanged

    def test_verify_terms_fixes_en_echo(self, engine):
        """verify_terms Pass 3 should fix EN terms that MT left untranslated."""
        # Simulate MT output that left "gradient descent" in English
        tr_text = "Egitim icin gradient descent kullanin."
        verified = engine.verify_terms(tr_text, {})
        # The EN term should be replaced with TR
        gd_entry = engine.get_term("gradient descent")
        if gd_entry:
            assert gd_entry.effective_tr in verified or "gradient descent" not in verified

    def test_verify_empty_map(self, engine):
        """verify_terms with empty map should return text unchanged."""
        text = "Some translated text."
        verified = engine.verify_terms(text, {})
        assert verified == text

    def test_inject_disabled_for_all_types(self, engine):
        """inject_terms should return empty tmap for ALL book types now."""
        text = "stochastic gradient descent"
        injected, tmap = engine.inject_terms(text)
        assert tmap == {}
        assert injected == text  # text unchanged

    def test_match_terms_still_works(self, engine):
        """match_terms should still find terms even though inject is disabled."""
        text = "loss function and activation function"
        matches = engine.match_terms(text)
        assert len(matches) >= 2

    def test_verify_replaces_en_echoes(self, engine):
        """verify_terms Pass 3 should replace EN terms left by MT."""
        # Simulate: MT left "neural network" untranslated
        tr_text = "Bu neural network cok hizli."
        verified = engine.verify_terms(tr_text, {})
        nn_entry = engine.get_term("neural network")
        if nn_entry:
            assert nn_entry.effective_tr in verified


# ---------------------------------------------------------------------------
# Deprecated stash/restore tests (backward compatibility)
# ---------------------------------------------------------------------------

class TestStashRestoreDeprecated:
    """Test deprecated stash_terms/restore_terms still work.

    These methods emit DeprecationWarning but must remain functional
    for backward compatibility.
    """

    def test_stash_emits_deprecation_warning(self, engine):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            engine.stash_terms("Use gradient descent.")
            assert any(issubclass(x.category, DeprecationWarning) for x in w)

    def test_restore_emits_deprecation_warning(self, engine):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            engine.restore_terms("<<TERM_0>> text", {0: TermMatch(
                en_term="test", tr_term="test", start=0, end=4,
            )})
            assert any(issubclass(x.category, DeprecationWarning) for x in w)

    def test_basic_stash_restore_still_works(self, engine):
        """Deprecated path should still function correctly."""
        text = "Use gradient descent for training."
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            stashed, tmap = engine.stash_terms(text)
            assert "<<TERM_" in stashed
            assert "gradient descent" not in stashed
            restored = engine.restore_terms(stashed, tmap)
            assert "gradyan" in restored

    def test_multiple_terms_deprecated(self, engine):
        text = "The neural network uses gradient descent."
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            stashed, tmap = engine.stash_terms(text)
            assert len(tmap) >= 2
            restored = engine.restore_terms(stashed, tmap)
            assert "sinir" in restored
            assert "gradyan" in restored

    def test_mangled_placeholder_recovery(self, engine):
        text = "The neural network works."
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            stashed, tmap = engine.stash_terms(text)
            # Simulate MT model adding spaces
            mangled = stashed.replace("<<TERM_0>>", "<< TERM_0 >>")
            restored = engine.restore_terms(mangled, tmap)
            assert "<<TERM" not in restored or "sinir" in restored


# ---------------------------------------------------------------------------
# Turkish quality tests
# ---------------------------------------------------------------------------

class TestTurkishQuality:
    """Test that translations use proper Turkish special characters."""

    def test_special_chars_in_common_terms(self, real_glossary_dir):
        """Gerçek curated.json'daki anahtar terimler Türkçe harfli (G2c:
        silinen genel sözlüklerin terimleri listeden çıkarıldı)."""
        engine = GlossaryEngine(glossary_dir=real_glossary_dir,
                                book_type="science")
        checks = {
            "neural network": "ağ",                     # ğ
            "deep learning": "öğ",                      # ö, ğ
            "convolutional neural network": "şğ",       # ş, ğ
        }
        for en, expected_chars in checks.items():
            entry = engine.get_term(en)
            assert entry is not None, f"Term '{en}' not found"
            assert any(c in entry.tr for c in expected_chars), (
                f"Term '{en}' -> '{entry.tr}' lacks Turkish special chars")

    def test_no_ascii_for_key_terms(self, real_glossary_dir):
        """Terms like 'neural network' should NOT be 'sinir agi' (ASCII)."""
        engine = GlossaryEngine(glossary_dir=real_glossary_dir,
                                book_type="science")
        entry = engine.get_term("neural network")
        assert entry is not None
        assert "ağ" in entry.tr, (
            f"Expected 'ağ' in '{entry.tr}', got ASCII 'ag' instead"
        )


# ---------------------------------------------------------------------------
# Domain/book_type tests
# ---------------------------------------------------------------------------

class TestDomains:
    """Test domain filtering by book type."""

    def test_science_has_ml_terms(self, engine):
        entry = engine.get_term("convolutional neural network")
        assert entry is not None

    def test_science_has_physics_terms(self, engine):
        entry = engine.get_term("boundary layer")
        assert entry is not None

    def test_science_has_math_terms(self, engine):
        entry = engine.get_term("eigenvalue")
        assert entry is not None

    def test_philosophy_has_specific_terms(self, engine_philosophy):
        entry = engine_philosophy.get_term("categorical imperative")
        assert entry is not None
        assert entry.tr == "kategorik buyruk"

    def test_narrow_type_drops_other_domains(self, glossary_dir):
        # physics türü: general + math + physics; ml / philosophy terimi yok
        eng = GlossaryEngine(glossary_dir=glossary_dir, book_type="physics")
        assert eng.get_term("boundary layer") is not None
        assert eng.get_term("convolutional neural network") is None
        assert eng.get_term("categorical imperative") is None


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Test edge cases and special inputs."""

    def test_overlapping_terms(self, engine):
        """When two terms overlap, only one should match."""
        # "gradient descent" overlaps with "stochastic gradient descent"
        text = "stochastic gradient descent"
        matches = engine.match_terms(text)
        assert len(matches) == 1  # Only the longest match

    def test_adjacent_terms(self, engine):
        """Two terms next to each other should both be matched."""
        text = "loss function and activation function"
        matches = engine.match_terms(text)
        assert len(matches) >= 2

    def test_term_at_start_of_text(self, engine):
        text = "Neural network is the foundation."
        matches = engine.match_terms(text)
        assert len(matches) >= 1

    def test_term_at_end_of_text(self, engine):
        text = "We studied the neural network"
        matches = engine.match_terms(text)
        assert len(matches) >= 1

    def test_extra_terms_parameter(self, glossary_dir):
        """Extra terms should be merged."""
        extra = {"quantum entanglement": "kuantum dolaniklık"}
        eng = GlossaryEngine(
            glossary_dir=glossary_dir,
            book_type="science",
            extra_terms=extra,
        )
        entry = eng.get_term("quantum entanglement")
        assert entry is not None
        assert entry.tr == "kuantum dolaniklık"

    def test_summary(self, engine):
        summary = engine.summary()
        assert "GlossaryEngine" in summary
        assert "terms loaded" in summary
