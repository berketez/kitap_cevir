"""
Yol B onarım sprinti DALGA 2 — chain2 grubu regresyon/kilit testleri.

Kapsanan bulgular:
    GLS-08  — inject_terms KOŞULSUZ devre dışı (her book_type için boş map;
              skip-list'te olmayan tür kazara inject tetikleyemez).
    GLS-D7  — Pass 3 EN-echo taraması TEK birleşik alternation regex'e
              refactor edildi; davranış birebir korunur (longest-first,
              case-preserving, filtreler).
    GLS-14  — curated collision: elle doğrulanmış curated karşılığı otomatik
              terimi ezer; translate_book._load_curated_terms gerçek curated
              set'i yükler. G2c (KARAR 25, 2026-10-05): TerminologyEngine
              (ölü kod) silindi; kilit kitap sözlüğüne
              (kitap_sozlugu.terimleri_cevir, curated=...) taşındı.
    QC-N2   — QCEngine(book_type=...) yapıcı parametresi kablolandı.
    RT-08   — verify_terms idempotens KİLİDİ: verify_terms(verify_terms(x))
              == verify_terms(x) (heading stash + Pass 2 + Pass 3 kapsar).
              Geçtiği için ağır restructure GEREKMEZ.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from latex_pipeline.glossary import (
    GlossaryEngine,
    GlossaryStats,
    TermEntry,
    _SKIP_INJECT_BOOK_TYPES,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GLOSSARY_DIR = PROJECT_ROOT / "glossary"


# ---------------------------------------------------------------------------
# Küçük, veri-bağımsız motor (Pass 2 + Pass 3 + heading kapsar)
# ---------------------------------------------------------------------------

def _mini_engine(book_type: str = "science") -> GlossaryEngine:
    eng = GlossaryEngine.__new__(GlossaryEngine)
    eng.book_type = book_type
    eng.stats = GlossaryStats()
    eng._terms = {}
    # Pass 3: tek-kelime YÜKSEK öncelik
    eng._terms["alpha"] = TermEntry(en="alpha", tr="alfa dalgası", priority="high")
    # Pass 3: tek-kelime ORTA öncelik -> GİRMEZ (daraltma)
    eng._terms["beam"] = TermEntry(en="beam", tr="kiriş", priority="medium")
    # Pass 3: çok-kelime + Pass 2 wrong_alternatives
    eng._terms["gradient descent"] = TermEntry(
        en="gradient descent", tr="gradyan inişi", priority="medium",
        wrong_alternatives=["gradyan azalması", "eğim inişi"],
    )
    # Pass 3: daha UZUN çok-kelime (longest-first testi)
    eng._terms["stochastic gradient descent"] = TermEntry(
        en="stochastic gradient descent", tr="olasılıksal gradyan inişi",
        priority="medium",
    )
    eng._terms["neural network"] = TermEntry(
        en="neural network", tr="sinir ağı", priority="medium",
    )
    eng._finalize()
    return eng


# ---------------------------------------------------------------------------
# GLS-08: inject_terms koşulsuz devre dışı
# ---------------------------------------------------------------------------

class TestInjectTermsUnconditionallyDisabled:
    @pytest.mark.parametrize(
        "book_type",
        ["science", "philosophy", "novel", "general", "medical"],
    )
    def test_returns_empty_for_known_types(self, book_type):
        eng = _mini_engine(book_type)
        text = "The alpha and gradient descent method with neural network."
        out, tmap = eng.inject_terms(text)
        assert out == text
        assert tmap == {}

    def test_returns_empty_for_UNKNOWN_type(self):
        # KRİTİK: skip-list'te OLMAYAN bir book_type bile inject tetiklemez.
        eng = _mini_engine("totally_unknown_genre_xyz")
        assert eng.book_type not in _SKIP_INJECT_BOOK_TYPES
        text = "The alpha and gradient descent method."
        out, tmap = eng.inject_terms(text)
        assert out == text, "Unknown book_type must NOT trigger injection"
        assert tmap == {}

    def test_empty_text(self):
        eng = _mini_engine()
        assert eng.inject_terms("") == ("", {})

    def test_never_produces_placeholder(self):
        eng = _mini_engine()
        out, _ = eng.inject_terms("alpha beam gradient descent")
        assert "<<TERM_" not in out


# ---------------------------------------------------------------------------
# GLS-D7: Pass 3 birleşik regex refactor — davranış birebir korunur
# ---------------------------------------------------------------------------

class TestPass3CombinedRegex:
    def test_longest_first_wins(self):
        eng = _mini_engine()
        out = eng.verify_terms("Use stochastic gradient descent here.", {})
        assert "olasılıksal gradyan inişi" in out
        # Kısa terim ("gradient descent") uzun eşleşmenin içinden ayrıca
        # tetiklenmemeli (çift değişim yok).
        assert "gradyan inişi inişi" not in out
        assert out.count("gradyan inişi") == 1

    def test_multiword_replaced(self):
        eng = _mini_engine()
        assert "gradyan inişi" in eng.verify_terms("Apply gradient descent now.", {})

    def test_single_word_high_priority_replaced(self):
        eng = _mini_engine()
        assert "alfa dalgası" in eng.verify_terms("The alpha value.", {})

    def test_single_word_medium_priority_untouched(self):
        eng = _mini_engine()
        assert eng.verify_terms("The beam is bent.", {}) == "The beam is bent."

    def test_case_preserving_across_occurrences(self):
        eng = _mini_engine()
        out = eng.verify_terms("Alpha and alpha and ALPHA.", {})
        assert "Alfa dalgası and alfa dalgası and ALFA DALGASI" in out

    def test_wrong_alternatives_pass2_still_runs(self):
        eng = _mini_engine()
        out = eng.verify_terms("The eğim inişi and gradyan azalması results.", {})
        assert "eğim inişi" not in out
        assert "gradyan azalması" not in out
        assert out.count("gradyan inişi") == 2

    def test_heading_and_math_untouched(self):
        eng = _mini_engine()
        src = (
            r"\section{Alpha and Gradient Descent}"
            "\n"
            r"Body: alpha and \(\alpha\) and $$beam + alpha$$ end."
        )
        out = eng.verify_terms(src, {})
        # Başlık aynen korunur
        assert r"\section{Alpha and Gradient Descent}" in out
        # Math bölgeleri korunur
        assert r"\(\alpha\)" in out
        assert "$$beam + alpha$$" in out
        # Gövdedeki serbest 'alpha' çevrilir
        assert "Body: alfa dalgası" in out


# ---------------------------------------------------------------------------
# RT-08: verify_terms idempotens KİLİDİ
# ---------------------------------------------------------------------------

class TestVerifyTermsIdempotent:
    IDEMPOTENT_INPUTS = [
        r"The alpha and gradient descent. \section{Alpha Waves} eğim inişi here.",
        r"Use \(\alpha\) and $$alpha+beam$$ then stochastic gradient descent.",
        "ALPHA and alpha and Alpha. Neural network with gradient descent.\n"
        r"\caption{alpha beam gradient descent}",
        r"gradyan azalması was wrong; neural network trains via gradient descent.",
    ]

    @pytest.mark.parametrize("src", IDEMPOTENT_INPUTS)
    def test_mini_engine_idempotent(self, src):
        eng = _mini_engine()
        once = eng.verify_terms(src, {})
        twice = eng.verify_terms(once, {})
        assert once == twice, f"verify_terms not idempotent on: {src!r}"

    @pytest.mark.parametrize("book_type", ["science", "philosophy", "ml"])
    def test_real_engine_idempotent(self, book_type):
        eng = GlossaryEngine(glossary_dir=GLOSSARY_DIR, book_type=book_type)
        body = (
            r"\section{Gradient Descent and Neural Networks}"
            "\n"
            "The gradient descent algorithm trains a neural network. "
            "Alpha waves and beam search.\n"
            r"\begin{equation} \alpha + \beta = \gamma \end{equation}"
            "\n"
            "Stochastic gradient descent uses momentum. "
            r"\caption{Neural network diagram}"
            "\n"
            "Inline $x = alpha$ and display $$ beam + gradient $$ end.\n"
        ) * 20
        once = eng.verify_terms(body, {})
        twice = eng.verify_terms(once, {})
        assert once == twice, f"real engine ({book_type}) not idempotent"


# ---------------------------------------------------------------------------
# GLS-14: curated collision filtresi + _load_curated_terms
# ---------------------------------------------------------------------------

class TestCuratedCollisionFilter:
    def test_curated_wins_over_model_term(self):
        """GLS-14 kilidi kitap sözlüğünde: curated ile çakışan aday modele
        GİTMEZ, curated karşılık kazanır; çakışmayan aday modelden gelir."""
        from latex_pipeline import kitap_sozlugu as ks

        class _Cevirmen:
            def __init__(self):
                self.giden = []

            def translate_batch_raw(self, texts, max_length=512):
                self.giden.extend(texts)
                return [t.rsplit("\n\n", 1)[0] + "\n\nparça" for t in texts]

        cev = _Cevirmen()
        adaylar = [ks.TerimAdayi("nozzle", 5, 1, "A nozzle sentence."),
                   ks.TerimAdayi("widget", 5, 1, "A widget sentence.")]
        out = ks.terimleri_cevir(adaylar, cev, "en",
                                 curated={"nozzle": "lüle"})
        assert {g.kaynak: g.hedef for g in out} == {"nozzle": "lüle",
                                                    "widget": "parça"}
        assert all("nozzle" not in t for t in cev.giden)

    def test_load_curated_terms_reads_real_file(self):
        import translate_book as tb
        curated = tb._load_curated_terms(GLOSSARY_DIR)
        assert isinstance(curated, dict)
        assert len(curated) > 0
        # curated.json'daki bilinen bir giriş
        assert curated.get("nozzle") == "lüle"
        # anahtarlar lower-case
        assert all(k == k.lower() for k in curated)

    def test_load_curated_terms_missing_dir_returns_empty(self, tmp_path):
        import translate_book as tb
        assert tb._load_curated_terms(tmp_path / "nope") == {}


# ---------------------------------------------------------------------------
# QC-N2: QCEngine book_type yapıcı parametresi
# ---------------------------------------------------------------------------

class TestQCEngineBookTypeWiring:
    @pytest.mark.parametrize("book_type", ["novel", "ml", "medical", None])
    def test_constructor_accepts_book_type(self, book_type):
        from latex_pipeline.qc_engine import QCEngine
        q = QCEngine(glossary_dir=None, book_type=book_type)
        assert q._book_type == book_type
