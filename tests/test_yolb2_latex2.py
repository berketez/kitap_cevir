"""Yol B onarım DALGA 2 — latex2 grubu hedefli testleri.

Kapsanan bulgular:
  GLS-D5   — translator.py'deki âtıl glossary inject/verify çifti kaldırıldı.
  BATCH-14 — _BATCH_SIZE magic number -> PipelineConfig.mlx_batch_size.
  BATCH-13 — batched yolda âtıl ara checkpoint yazımı kaldırıldı,
             sequential fallback checkpoint'i korundu.

Ağır MT modeli yüklenmez; bare instance (``__new__``) + saf yardımcılar.
"""

import inspect
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from latex_pipeline.config import PipelineConfig
from latex_pipeline.translator import LatexTranslator, TranslationStats
from latex_pipeline.glossary import (
    GlossaryEngine,
    _DOMAIN_MAP,
    _SKIP_INJECT_BOOK_TYPES,
)
from config.defaults import DEFAULT_BATCH_SIZE_HYMT_LOCAL


def _bare() -> LatexTranslator:
    t = LatexTranslator.__new__(LatexTranslator)
    t._glossary_term_map = {}
    t.stats = TranslationStats()
    t.target_lang = "tr"
    return t


# ---------------------------------------------------------------------------
# GLS-D5 — inject/verify çifti âtıldı (premise + davranış)
# ---------------------------------------------------------------------------

def test_glsd5_inject_empty_for_all_registered_booktypes():
    """inject_terms tüm kayıtlı book_type'larda boş map döndürür.

    Çiftin kaldırılmasının gerekçesini kilitler: her book_type
    _SKIP_INJECT_BOOK_TYPES kapsamındadır, dolayısıyla map her zaman boştur.
    """
    gdir = Path("glossary")
    if not gdir.exists():
        pytest.skip("glossary/ dizini yok")
    sample = (
        "The gradient descent algorithm computes the derivative of the loss "
        "function with respect to torque and velocity in the boundary layer."
    )
    for bt in _DOMAIN_MAP:
        assert bt in _SKIP_INJECT_BOOK_TYPES, f"{bt} skip setinde değil"
        eng = GlossaryEngine(glossary_dir=gdir, book_type=bt)
        text, tmap = eng.inject_terms(sample)
        assert tmap == {}, f"{bt}: inject map boş değil"
        assert text == sample, f"{bt}: metin değişti"


def test_glsd5_translate_does_not_call_inject_or_verify():
    """translate() artık glossary inject_terms/verify_terms ÇAĞIRMAZ.

    Çağrılırsa Mock AssertionError fırlatır; hata yoksa çift gerçekten
    kaldırılmıştır.
    """
    t = _bare()
    t._checkpoint_dir = None
    t._checkpoint_counter = 0
    t._progress_callback = None

    eng = MagicMock()
    eng.inject_terms.side_effect = AssertionError("inject_terms çağrılmamalı")
    eng.verify_terms.side_effect = AssertionError("verify_terms çağrılmamalı")
    t._glossary_engine = eng

    # Ağır alt-adımları kimliğe indirge (MT modeli yüklenmesin).
    t._stash_protected = lambda b: (b, [])
    t._translate_structured = lambda b: b
    t._translate_paragraphs = lambda b: b
    t._restore_stash = lambda b, s: b
    t._fix_line_brace_balance = lambda b: b
    t._save_checkpoint = lambda *a, **k: None

    out = t.translate("Bu bir gövde metnidir.")
    assert out == "Bu bir gövde metnidir."
    assert not eng.inject_terms.called
    assert not eng.verify_terms.called


def test_glsd5_heading_term_resolver_still_strips_artifacts():
    """Yaşayan kullanım korundu: _resolve_term_placeholders_for_heading,
    boş term_map ile hallüsinasyon TERM artefaktlarını siler."""
    t = _bare()
    t._glossary_term_map = {}  # inject yok -> map boş
    out = t._resolve_term_placeholders_for_heading("Giriş <<TERM_3>> Bölümü")
    assert "TERM" not in out
    assert "Giriş" in out and "Bölümü" in out


# ---------------------------------------------------------------------------
# BATCH-14 — mlx_batch_size config alanı
# ---------------------------------------------------------------------------

def test_batch14_config_field_default_and_distinct():
    c = PipelineConfig()
    assert c.mlx_batch_size == 8
    # DEFAULT_BATCH_SIZE_HYMT_LOCAL (core MT batch) ile AYRI kavram.
    assert c.mlx_batch_size != DEFAULT_BATCH_SIZE_HYMT_LOCAL


def test_batch14_get_batch_size_reads_config():
    t = LatexTranslator.__new__(LatexTranslator)
    t.pipeline_config = PipelineConfig(mlx_batch_size=16)
    assert t._get_batch_size() == 16


def test_batch14_get_batch_size_falls_back_on_default_and_none():
    t = LatexTranslator.__new__(LatexTranslator)
    t.pipeline_config = PipelineConfig()  # default 8
    assert t._get_batch_size() == 8
    t.pipeline_config = None  # config yok -> sınıf sabiti
    assert t._get_batch_size() == LatexTranslator._BATCH_SIZE == 8
    t.pipeline_config = PipelineConfig(mlx_batch_size=0)  # 0 -> falsy fallback
    assert t._get_batch_size() == 8


# ---------------------------------------------------------------------------
# BATCH-13 — ara checkpoint yazımı (batched kaldırıldı, sequential korundu)
# ---------------------------------------------------------------------------

def test_batch13_batched_path_has_no_intermediate_checkpoint():
    src = inspect.getsource(LatexTranslator._translate_paragraphs_batched)
    assert "_save_checkpoint(partial + remaining)" not in src
    assert "_save_checkpoint(partial" not in src


def test_batch13_sequential_fallback_keeps_intermediate_checkpoint():
    src = inspect.getsource(LatexTranslator._translate_paragraphs_sequential)
    assert "_save_checkpoint(partial + remaining)" in src


def test_batch13_resume_returns_final_checkpoint(tmp_path):
    t = LatexTranslator.__new__(LatexTranslator)
    t._checkpoint_dir = tmp_path
    (tmp_path / "translated_body_final.tex").write_text(
        "SON GÖVDE", encoding="utf-8"
    )
    assert t._try_resume_from_checkpoint("kaynak") == "SON GÖVDE"


def test_batch13_resume_rejects_intermediate_only(tmp_path):
    t = LatexTranslator.__new__(LatexTranslator)
    t._checkpoint_dir = tmp_path
    ckpt = tmp_path / "translated_body_checkpoint.tex"
    ckpt.write_text("kısmi <<LATEX_0>> gövde", encoding="utf-8")
    # Final yok -> intermediate reddedilir, None döner, .unusable'a taşınır.
    assert t._try_resume_from_checkpoint("kaynak") is None
    assert not ckpt.exists()
    assert (tmp_path / "translated_body_checkpoint.tex.unusable").exists()
