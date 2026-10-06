"""Yol B fix sprint — latex_pipeline/translator.py hedefli testleri.

Bu dosya SADECE latex_translator grubunun düzeltmelerini test eder.
Ağır MT modeli yüklenmez: saf/statik fonksiyonlar doğrudan, instance
gereken yerlerde ``LatexTranslator.__new__`` ile bare instance + sahte
translator enjekte edilir (``_get_translator`` model yüklemez çünkü
``_translator`` önceden set edilir).

Kapsanan bulgular:
  A1        — batch ana yolu translate_batch_protected'a bağlanır (hasattr)
  A2/B-1    — başlık MT'si is_heading=True + dil kapısı
  HEAD-01   — kelime-sınırlı heading glossary (substring garbage yok)
  HEAD-04   — akronim koruma kuralı (CNN/RNN/LSTM)
  HEAD-05   — _fix_turkish_ascii word-boundary
  HEAD-06   — Türkçe-bilinçli title case (İLE -> ile)
  HEAD-10   — heading glossary veri onarımı
  HEAD-12   — kelime-kelime salata fallback kaldırıldı
  B-11      — placeholder kaybında orijinale geri dönüş (tek yardımcı)
  BATCH-06  — hard_split placeholder'ı ikiye bölmez
  BATCH-11  — _PLACEHOLDER_RE hem LATEX hem CMD yakalar
  A5        — hard-split tek merkezi sabit
  C2        — _is_english_sentence kanonik is_english_line'a delege
"""

import re
import types

import pytest

from config.defaults import MAX_SEGMENT_LENGTH
from latex_pipeline.translator import (
    HARD_SPLIT_CHARS,
    LatexTranslator,
    TranslationStats,
    _looks_like_acronym,
    _smart_title_case,
)


# ---------------------------------------------------------------------------
# Test yardımcıları
# ---------------------------------------------------------------------------

def _bare() -> LatexTranslator:
    """Model/glossary yüklemeden bare LatexTranslator instance."""
    t = LatexTranslator.__new__(LatexTranslator)
    t._glossary_term_map = {}
    t.stats = TranslationStats()
    t.target_lang = "tr"
    return t


class _FakeTranslator:
    """translate_text(text, is_heading=False) — davranışı callback belirler."""

    def __init__(self, fn):
        self._fn = fn
        self.last_is_heading = None

    def translate_text(self, text, is_heading=False):
        self.last_is_heading = is_heading
        return self._fn(text, is_heading)


def _heading_translator(fn) -> LatexTranslator:
    t = _bare()
    t._translator = _FakeTranslator(fn)
    return t


# ---------------------------------------------------------------------------
# HEAD-04: akronim koruma kuralı
# ---------------------------------------------------------------------------

def test_head04_acronym_rule_preserves_ml_acronyms():
    out = _smart_title_case("TRAINING CNN AND RNN MODELS WITH LSTM")
    assert "CNN" in out and "RNN" in out and "LSTM" in out
    # Whack-a-mole olmadan 'Cnn'/'Rnn'/'Lstm' üretilmemeli
    assert "Cnn" not in out and "Rnn" not in out and "Lstm" not in out
    # Normal kelimeler title-case olmalı
    assert "Training" in out and "Models" in out


def test_head04_looks_like_acronym():
    assert _looks_like_acronym("CNN")
    assert _looks_like_acronym("LSTM")
    assert _looks_like_acronym("SGD")
    # Sesli harf içeren -> akronim sayılmaz (kural), listeye kalır
    assert not _looks_like_acronym("GAN")   # A sesli
    assert not _looks_like_acronym("MODELS")  # >5 ve sesli
    assert not _looks_like_acronym("the")   # küçük harf


# NOT: HEAD-05 (_fix_turkish_ascii word-boundary) ATLANDI — Türkçe eklemeli
# dil olduğu için iki yanlı \b eşleşme asıl kullanımı (kök+ek düzeltme)
# bozuyordu; mevcut substring davranışı korundu. Gerekçe structured output'ta.


# ---------------------------------------------------------------------------
# HEAD-06: Türkçe-bilinçli title case
# ---------------------------------------------------------------------------

def test_head06_turkish_title_case_ile():
    out = LatexTranslator._turkish_title_case("ısı VE kütle transferi İLE analiz")
    # 'VE' -> 've' ve 'İLE' -> 'ile' (bağlaç, ilk kelime değil)
    assert " ile " in out
    assert " ve " in out
    assert "İLE" not in out and "VE" not in out


# ---------------------------------------------------------------------------
# HEAD-01 + HEAD-12: kelime-sınırlı heading glossary, garbage yok
# ---------------------------------------------------------------------------

def test_head01_word_boundary_no_substring_garbage():
    t = _bare()
    # 'Section' -> 'Kısım' 'Sectional' içinde tetiklenmemeli
    out = t._apply_heading_glossary_wb("Sectional Introduction")
    assert "Kısımal" not in out
    # 'Introduction' tam kelime -> çevrilmeli
    assert "Giriş" in out
    assert "Sectional" in out  # dokunulmadı


def test_head01_chapters_plural_not_mangled():
    t = _bare()
    # 'Chapter' \b 'Chapters'a eşleşmez -> 'Bölüms' garbage üretilmez
    out = t._apply_heading_glossary_wb("Chapters and Sections")
    assert "Bölüms" not in out and "Kısıms" not in out


def test_head_full_flow_echo_produces_no_garbage():
    # MT echo eder (çeviremez) -> son çare orijinali korur, garbage yok
    t = _heading_translator(lambda text, is_heading: text)
    out = t._translate_heading_text("Sectional Analysis of Rockets")
    assert "Kısımal" not in out
    assert "Bölüms" not in out


def test_head12_no_word_salad():
    # Eski davranış 'Derin Ağlar ve Training' salatası üretirdi; artık
    # eşleşme yoksa orijinal korunur (yarı-İngilizce salata YOK).
    t = _heading_translator(lambda text, is_heading: text)  # echo
    out = t._translate_heading_text("Deep Networks and Training")
    # Kısmi/karışık çıktı olmamalı: ya tam Türkçe ya orijinal
    assert out == "Deep Networks and Training"


# ---------------------------------------------------------------------------
# A2 + B-1: başlık modu MT + dil kapısı
# ---------------------------------------------------------------------------

def test_a2_is_heading_flag_passed():
    fake = _FakeTranslator(lambda text, is_heading: "Derin Öğrenmeye Giriş")
    t = _bare()
    t._translator = fake
    out = t._translate_heading_text("Introduction to Deep Learning Zzz")
    assert fake.last_is_heading is True
    assert out == "Derin Öğrenmeye Giriş"


def test_a2_typeerror_fallback_to_old_signature():
    # Eski imzalı translator (is_heading kabul etmez) -> TypeError -> fallback
    class _OldSig:
        def translate_text(self, text):
            return "Türkçe Başlık"

    t = _bare()
    t._translator = _OldSig()
    out = t._translate_heading_text("Some English Heading Text Zzz")
    assert out == "Türkçe Başlık"


def test_b1_language_gate_rejects_english_mt_result():
    # MT echo DEĞİL ama yine İngilizce döndürürse kabul edilmemeli.
    t = _heading_translator(
        lambda text, is_heading: "Introduction to Deep Neural Systems"
    )
    out = t._translate_heading_text("Foundations of Machine Perception Zzz")
    # İngilizce MT çıktısı reddedilmeli (kabul edilseydi title-case'i dönerdi)
    assert out != "Introduction To Deep Neural Systems"
    assert "Introduction to Deep Neural Systems" not in out


def test_b1_heading_is_english_gate():
    t = _bare()
    assert t._heading_is_english("Introduction to Deep Neural Networks") is True
    assert t._heading_is_english("Derin Öğrenmeye Giriş") is False


def test_head_positive_turkish_mt_accepted():
    t = _heading_translator(lambda text, is_heading: "Sinir Ağlarına Giriş")
    out = t._translate_heading_text("Introduction to Neural Systems Zzz")
    assert out == "Sinir Ağlarına Giriş"


# ---------------------------------------------------------------------------
# HEAD-10: glossary veri onarımı
# ---------------------------------------------------------------------------

def test_head10_glossary_data_repair():
    g = LatexTranslator._HEADING_GLOSSARY
    # Yarı çevrilmiş (Türkçe sözcük karışmış) anahtar kalmamalı.
    assert not any("Yöntemler" in k or "Bir-Dimensional" in k for k in g)
    assert g["Problems"] == "Problemler"


# ---------------------------------------------------------------------------
# B-11: placeholder kaybında orijinale geri dönüş
# ---------------------------------------------------------------------------

def test_b11_revert_on_placeholder_loss():
    t = _bare()
    original = "See <<LATEX_3>> for details."
    lost = t._revert_on_placeholder_loss("Ayrıntılar için bakınız.", original)
    assert lost == original  # segment orijinaline geri alındı
    assert t.stats.placeholders_lost == 1


def test_b11_no_revert_when_preserved():
    t = _bare()
    original = "See <<LATEX_3>> for details."
    good = "Ayrıntılar için <<LATEX_3>> bakınız."
    out = t._revert_on_placeholder_loss(good, original)
    assert out == good
    assert t.stats.placeholders_lost == 0


def test_b11_no_tail_append():
    # Eski davranış placeholder'ı sona ekliyordu; artık ASLA sona-ekleme yok.
    t = _bare()
    original = "Orta <<LATEX_5>> cümle."
    out = t._revert_on_placeholder_loss("Ortada cümle var.", original)
    # Çıktı ya orijinal ya korunmuş — sona eklenmiş placeholder içermez
    assert not out.rstrip().endswith("<<LATEX_5>>") or out == original
    assert out == original


# ---------------------------------------------------------------------------
# BATCH-11: _PLACEHOLDER_RE hem LATEX hem CMD
# ---------------------------------------------------------------------------

def test_batch11_placeholder_re_matches_cmd_and_latex():
    found = LatexTranslator._PLACEHOLDER_RE.findall("x <<CMD_2>> y <<LATEX_5>> z")
    assert found == ["<<CMD_2>>", "<<LATEX_5>>"]


# ---------------------------------------------------------------------------
# BATCH-06: hard_split placeholder'ı bölmez
# ---------------------------------------------------------------------------

def test_batch06_hard_split_keeps_placeholder_intact():
    text = "a" * 490 + "<<LATEX_7>>" + "b" * 490
    chunks = LatexTranslator._hard_split(text, max_chars=500)
    joined = " ".join(chunks)
    # Placeholder tam olarak korunmalı (ikiye bölünmemeli)
    assert "<<LATEX_7>>" in joined
    for c in chunks:
        # Hiçbir chunk yarım placeholder içermemeli
        assert c.count("<<LATEX_") == c.count(">>") or "<<LATEX_7>>" in c


# ---------------------------------------------------------------------------
# A5: hard-split tek merkezi sabit
# ---------------------------------------------------------------------------

def test_a5_hard_split_central_constant():
    assert HARD_SPLIT_CHARS == MAX_SEGMENT_LENGTH // 4
    # _hard_split default'u merkezi sabiti kullanır
    import inspect
    sig = inspect.signature(LatexTranslator._hard_split)
    assert sig.parameters["max_chars"].default == HARD_SPLIT_CHARS


# ---------------------------------------------------------------------------
# C2: _is_english_sentence kanonik dedektöre delege
# ---------------------------------------------------------------------------

def test_c2_is_english_sentence_delegates():
    t = _bare()
    assert t._is_english_sentence(
        "This is a fully English sentence about the model and its results."
    ) is True
    assert t._is_english_sentence(
        "Bu tamamen Türkçe bir cümledir ve modelin sonuçları hakkındadır."
    ) is False


# ---------------------------------------------------------------------------
# A1: batch ana yolu translate_batch_protected'a bağlanır (hasattr)
# ---------------------------------------------------------------------------

class _ProtectedBatch:
    def __init__(self):
        self.protected_called = False

    def translate_batch_protected(self, batch):
        self.protected_called = True
        return ["P:" + x for x in batch]

    def _translate_batch_hymt(self, batch):
        return ["H:" + x for x in batch]


class _RawOnlyBatch:
    def _translate_batch_hymt(self, batch):
        return ["H:" + x for x in batch]


def _batch_instance(fake):
    t = _bare()
    t._translator = fake
    t.pipeline_config = types.SimpleNamespace(segment_timeout=60)
    return t


def test_a1_batch_prefers_protected():
    fake = _ProtectedBatch()
    t = _batch_instance(fake)
    out = t._batch_mt_call(["alpha", "beta"])
    assert fake.protected_called is True
    assert out == ["P:alpha", "P:beta"]


def test_a1_batch_falls_back_to_raw_when_no_protected():
    fake = _RawOnlyBatch()
    t = _batch_instance(fake)
    out = t._batch_mt_call(["alpha", "beta"])
    assert out == ["H:alpha", "H:beta"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
