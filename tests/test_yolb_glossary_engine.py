"""
Yol B (HY-MT otomatik çeviri) glossary_engine fix sprinti regresyon testleri.

Kapsanan bulgular:
    D3/GLS-06  — verify_terms başlık/caption satırlarına DOKUNMAZ.
    GLS-03     — verbatim/lstlisting/minted ortamları korunur.
    GLS-D1     — Pass 3 EN echo taraması daraltıldı (çok-kelime VEYA yüksek
                 öncelik tek-kelime); bağlamsız tek-kelime replace kapalı.
    GLS-02     — case-preserving replacement (cümle başı / ALL-CAPS korunur).
    GLS-04     — $$...$$ display math korunur.
    GLS-05     — iç içe matematik ortamı backreference ile tam korunur.
    DATA-D6    — _add_term keep_original girişini korur (kitap adı ezilmez).
    GLS-13     — \\emph{}/\\textbf{} içindeki terim adayı kaybolmaz. G2c
                 (KARAR 25, 2026-10-05): TerminologyEngine ve
                 GlossaryBootstrapper (ölü kod) SİLİNDİ; aday çıkarımı artık
                 latex_pipeline/kitap_sozlugu.py'de, test onu sınar. D4
                 (Bootstrapper translator=None) modülle birlikte kalktı.
    Focus 5    — Yükleme aşamasında TR-kalite (mojibake) uyarı kapısı.

NOT: Yol A koruması (manual_translation=True) bu motorun Phase 5.75 çağrısında
zaten `not manual_translation` guard'ı ile atlanır; burada verify_terms'in
başlık/kod bölgelerine dokunmaması Yol A'nın da (ajan çevirisi) korunmasını
güçlendirir — bkz. test_manual_mode_headings_never_touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from latex_pipeline.glossary import (
    GlossaryEngine,
    TermEntry,
    _match_case,
    _protect_math_zones,
    _restore_math_zones,
    _stash_headings,
    _TR_MOJIBAKE_MARKERS,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GLOSSARY_DIR = PROJECT_ROOT / "glossary"


@pytest.fixture(scope="module")
def science_engine():
    return GlossaryEngine(glossary_dir=GLOSSARY_DIR, book_type="science")


@pytest.fixture(scope="module")
def philosophy_engine():
    return GlossaryEngine(glossary_dir=GLOSSARY_DIR, book_type="philosophy")


# ---------------------------------------------------------------------------
# Küçük, veri-bağımsız motor: davranışı deterministik test etmek için
# ---------------------------------------------------------------------------

def _mini_engine():
    eng = GlossaryEngine.__new__(GlossaryEngine)
    eng.book_type = "science"
    from latex_pipeline.glossary import GlossaryStats
    eng.stats = GlossaryStats()
    eng._terms = {}
    # alpha: tek-kelime, YÜKSEK öncelik -> Pass 3'e girer
    eng._terms["alpha"] = TermEntry(en="alpha", tr="alfa dalgası", priority="high")
    # beam: tek-kelime, ORTA öncelik -> Pass 3'e GİRMEZ (daraltma)
    eng._terms["beam"] = TermEntry(en="beam", tr="kiriş", priority="medium")
    # gradient descent: çok-kelime -> Pass 3'e girer
    eng._terms["gradient descent"] = TermEntry(
        en="gradient descent", tr="gradyan inişi", priority="medium",
    )
    eng._finalize()
    return eng


# ---------------------------------------------------------------------------
# D3 / GLS-06: başlık koruması
# ---------------------------------------------------------------------------

class TestHeadingProtection:
    def test_section_untouched(self):
        eng = _mini_engine()
        src = r"\section{Alpha and Gradient Descent}"
        assert eng.verify_terms(src, {}) == src

    def test_caption_untouched(self):
        eng = _mini_engine()
        src = r"\caption{Alpha beam gradient descent}"
        assert eng.verify_terms(src, {}) == src

    def test_chapter_with_nested_braces_untouched(self):
        eng = _mini_engine()
        src = r"\chapter{Alpha \textbf{Gradient Descent} Theory}"
        assert eng.verify_terms(src, {}) == src

    def test_body_outside_heading_still_processed(self):
        eng = _mini_engine()
        src = r"\section{Alpha}" + "\nThe gradient descent method."
        out = eng.verify_terms(src, {})
        # Başlık korunur, gövdedeki çok-kelime terim çevrilir
        assert r"\section{Alpha}" in out
        assert "gradyan inişi" in out

    def test_stash_headings_helper_balances_braces(self):
        stash = []
        text = r"pre \section{A {B} C} post"
        out = _stash_headings(text, stash)
        assert out == "pre __GLOSSSTASH_0__ post"
        assert stash == [r"\section{A {B} C}"]

    def test_manual_mode_headings_never_touched(self, science_engine):
        # Yol A güçlendirme: gerçek sözlükle bile başlık dokunulmaz kalmalı.
        src = r"\section{Alpha Waves and Beam Design}"
        assert science_engine.verify_terms(src, {}) == src


# ---------------------------------------------------------------------------
# GLS-03: kod ortamları
# ---------------------------------------------------------------------------

class TestCodeEnvProtection:
    @pytest.mark.parametrize("env", ["verbatim", "lstlisting", "minted", "Verbatim"])
    def test_code_env_content_untouched(self, science_engine, env):
        src = (
            "Metin.\n"
            f"\\begin{{{env}}}\n"
            "import gradient\nvector = alpha * beam\n"
            f"\\end{{{env}}}\n"
            "Son."
        )
        out = science_engine.verify_terms(src, {})
        assert "import gradient" in out
        assert "alpha * beam" in out
        assert "gradyan" not in out


# ---------------------------------------------------------------------------
# GLS-D1 / GLS-D7: Pass 3 daraltma
# ---------------------------------------------------------------------------

class TestPass3Narrowing:
    def test_single_word_medium_priority_not_replaced(self):
        eng = _mini_engine()
        # 'beam' tek-kelime orta öncelik -> DEĞİŞMEZ
        assert eng.verify_terms("The beam is bent.", {}) == "The beam is bent."

    def test_single_word_high_priority_replaced(self):
        eng = _mini_engine()
        out = eng.verify_terms("The alpha value.", {})
        assert "alfa dalgası" in out

    def test_multiword_replaced(self):
        eng = _mini_engine()
        out = eng.verify_terms("Use gradient descent here.", {})
        assert "gradyan inişi" in out


# ---------------------------------------------------------------------------
# GLS-02: case-preserving
# ---------------------------------------------------------------------------

class TestCasePreserving:
    def test_match_case_variants(self):
        assert _match_case("Alpha", "alfa dalgası") == "Alfa dalgası"
        assert _match_case("ALPHA", "alfa dalgası") == "ALFA DALGASI"
        assert _match_case("alpha", "alfa dalgası") == "alfa dalgası"
        # Sözlükteki büyük harfli TR, küçük eşleşmede küçülür
        assert _match_case("game", "Oyun") == "oyun"

    def test_sentence_start_capital_preserved(self):
        eng = _mini_engine()
        out = eng.verify_terms("Alpha starts a sentence.", {})
        assert out.startswith("Alfa dalgası")

    def test_allcaps_preserved(self):
        eng = _mini_engine()
        out = eng.verify_terms("The ALPHA constant.", {})
        assert "ALFA DALGASI" in out

    def test_no_constant_case_across_occurrences(self):
        eng = _mini_engine()
        out = eng.verify_terms("Alpha and alpha and ALPHA.", {})
        assert "Alfa dalgası and alfa dalgası and ALFA DALGASI" in out


# ---------------------------------------------------------------------------
# GLS-04 / GLS-05: $$ ve iç içe math
# ---------------------------------------------------------------------------

class TestMathProtection:
    def test_dollar_display_math_untouched(self, science_engine):
        src = r"Text $$ alpha + beam $$ end."
        assert science_engine.verify_terms(src, {}) == src

    def test_nested_equation_tail_protected(self, science_engine):
        src = (
            r"\begin{equation} M = \begin{pmatrix} a \end{pmatrix}, "
            r"\quad alpha + beam = 0 \end{equation}"
        )
        # İç içe ortamda kuyruk (alpha + beam) da korunmalı
        assert science_engine.verify_terms(src, {}) == src

    def test_protect_restore_roundtrip(self):
        text = r"a \alpha b $$x$$ c \section{H} d"
        stashed, stash = _protect_math_zones(text)
        assert _restore_math_zones(stashed, stash) == text


# ---------------------------------------------------------------------------
# DATA-D6: keep_original korunur
# ---------------------------------------------------------------------------

class TestKeepOriginalGuard:
    def test_add_term_keep_original_not_overwritten(self):
        eng = GlossaryEngine.__new__(GlossaryEngine)
        eng._terms = {}
        eng._add_term(TermEntry(en="Preventive Medicine", tr="Preventive Medicine",
                                keep_original=True, priority="high"))
        # TR-karakterli, keep_original'sız giriş EZEMEZ
        eng._add_term(TermEntry(en="Preventive Medicine", tr="önleyici tıp",
                                keep_original=False, priority="high"))
        t = eng._terms["preventive medicine"]
        assert t.keep_original is True
        assert t.tr == "Preventive Medicine"

    def test_new_keep_original_upgrades_plain(self):
        eng = GlossaryEngine.__new__(GlossaryEngine)
        eng._terms = {}
        eng._add_term(TermEntry(en="Bell Labs", tr="Bell Laboratuvarları", keep_original=False))
        eng._add_term(TermEntry(en="Bell Labs", tr="Bell Labs", keep_original=True))
        t = eng._terms["bell labs"]
        assert t.keep_original is True

    def test_real_glossary_keep_original_kept(self, philosophy_engine):
        # glossary/do_not_translate.json'daki keep_original girişi (kurum adı;
        # tek kitaba özgü adlar 5 Ekim 2026'da çıkarıldı, FX5b).
        t = philosophy_engine.get_term("kinsey institute")
        assert t is not None
        assert t.keep_original is True
        assert t.effective_tr == "Kinsey Institute"


# ---------------------------------------------------------------------------
# Focus 5: TR-kalite (mojibake) uyarı kapısı
# ---------------------------------------------------------------------------

class TestTRQualityGate:
    def test_mojibake_triggers_warning(self, tmp_path, caplog):
        gdir = tmp_path / "glossary"
        gdir.mkdir()
        # Bozuk kodlama: 'ÅŸ' mojibake işareti
        (gdir / "curated.json").write_text(
            json.dumps({"terms": {"foo": {"tr": "baÅŸlik", "priority": "high"}}},
                       ensure_ascii=False),
            encoding="utf-8",
        )
        import logging
        with caplog.at_level(logging.WARNING):
            GlossaryEngine(glossary_dir=gdir, book_type="general")
        assert any("TR kalite" in r.message for r in caplog.records)

    def test_clean_file_no_warning(self, tmp_path, caplog):
        gdir = tmp_path / "glossary"
        gdir.mkdir()
        (gdir / "curated.json").write_text(
            json.dumps({"terms": {"foo": {"tr": "başlık", "priority": "high"}}},
                       ensure_ascii=False),
            encoding="utf-8",
        )
        import logging
        with caplog.at_level(logging.WARNING):
            GlossaryEngine(glossary_dir=gdir, book_type="general")
        assert not any("TR kalite" in r.message for r in caplog.records)

    def test_markers_are_nonempty(self):
        assert len(_TR_MOJIBAKE_MARKERS) > 0
        assert "�" in _TR_MOJIBAKE_MARKERS


# ---------------------------------------------------------------------------
# GLS-13: \emph{}/\textbf{} unwrap (terim adayları kaybolmaz)
# ---------------------------------------------------------------------------

class TestEmphasisUnwrap:
    """G2c: aday çıkarımı (kitap_sozlugu) vurgu komutlarının argümanını
    düzyazı sayar; vurgulu ve düz geçiş birlikte sayılır."""

    def test_vurgulu_terim_aday_sayilir(self):
        from latex_pipeline import kitap_sozlugu as ks
        govde = "\n\n".join([
            r"The \emph{gradient descent} method converges slowly.",
            r"We apply \textbf{gradient descent} to the loss.",
            "Plain gradient descent needs a step size.",
        ])
        adaylar = {a.terim: a for a in ks.aday_terimler(govde, "en")}
        assert "gradient descent" in adaylar
        assert adaylar["gradient descent"].siklik == 3

    def test_temizlik_vurgu_argumanini_korur(self):
        from latex_pipeline import kitap_sozlugu as ks
        temiz = ks.govdeyi_temizle(r"x \textbf{gradient} y \emph{momentum}")
        assert "gradient" in temiz and "momentum" in temiz
        assert "textbf" not in temiz and "emph" not in temiz
