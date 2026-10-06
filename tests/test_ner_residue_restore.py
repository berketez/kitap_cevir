"""NER placeholder kalıntısı geri yükleme testleri (2026-07-06 sutton smoke).

Bug: MT modeli @@UNKNOWN0000@@ placeholder'ının sözcük kısmını çeviriyor
(UNKNOWN → BİLİNMİYOR) veya @@ işaretlerini kısmen düşürüyor. Eski post-pass
deseni ([A-Z_]+) Türkçe 'İ' harfini yakalayamadığı için "MİYOR0000@@"
kalıntısı final çıktıya sızdı ve yazar adı ("OSCAR BIBLARZ") PDF'den silindi.

Gerçek vakalar:
  \\subsubsection{OSCAR BIBLARZ}  →  \\subsubsection{MİYOR0000@@}
  "Gravity-Free Drag-Free ..."    →  "Yerçekimi-MİYOR0000-MİYOR0001 / 99"
"""

import pytest

from core.ner_protector import NERProtector


@pytest.fixture()
def ner():
    return NERProtector()


def _mapping(*originals):
    """index → orijinal eşlemeli sahte protect() çıktısı üret."""
    return {
        f"@@UNKNOWN{i:04d}@@": {"original": o, "category": "UNKNOWN", "index": i}
        for i, o in enumerate(originals)
    }


class TestMangledResidueRestore:
    def test_full_wrap_translated_word(self, ner):
        # MT, UNKNOWN sözcüğünü BİLİNMİYOR diye çevirmiş
        m = _mapping("OSCAR BIBLARZ")
        out = ner.restore("@@BİLİNMİYOR0000@@", m)
        assert out == "OSCAR BIBLARZ"

    def test_leading_marks_dropped(self, ner):
        # sutton gerçek vakası: baştaki @@ düşmüş
        m = _mapping("OSCAR BIBLARZ")
        out = ner.restore("MİYOR0000@@", m)
        assert "OSCAR BIBLARZ" in out
        assert "MİYOR" not in out
        assert "@@" not in out

    def test_trailing_marks_dropped(self, ner):
        m = _mapping("OSCAR BIBLARZ")
        out = ner.restore("@@MİYOR0000", m)
        assert "OSCAR BIBLARZ" in out
        assert "@@" not in out

    def test_bare_residue_two_entities(self, ner):
        # sutton TOC vakası: hiç @@ işareti kalmamış, iki kalıntı yan yana
        m = _mapping("Gravity-Free", "Free Space Flight")
        out = ner.restore("Yerçekimi-MİYOR0000-MİYOR0001 / 99", m)
        assert "Gravity-Free" in out
        assert "Free Space Flight" in out
        assert "MİYOR" not in out

    def test_canonical_placeholder_still_restored(self, ner):
        # Bozulmamış placeholder'ın normal yolu bozulmamalı
        m = _mapping("OSCAR BIBLARZ")
        out = ner.restore("Yazar: @@UNKNOWN0000@@", m)
        assert out == "Yazar: OSCAR BIBLARZ"


class TestFalsePositiveGuard:
    def test_unknown_index_untouched(self, ner):
        # Yakalanan index mapping'de yoksa metne DOKUNULMAZ
        m = _mapping("OSCAR BIBLARZ")  # yalnızca index 0 var
        text = "MODEL2024@@ referans kodu"
        assert ner.restore(text, m) == text

    def test_clean_text_untouched(self, ner):
        m = _mapping("OSCAR BIBLARZ")
        text = "Bu temiz bir Türkçe cümledir, yıl 2024."
        assert ner.restore(text, m) == text

    def test_empty_mapping_untouched(self, ner):
        text = "MİYOR0000@@ kalıntısı"
        assert ner.restore(text, {}) == text
