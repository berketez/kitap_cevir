# -*- coding: utf-8 -*-
"""Yol B sözlük veri onarımı (grup: glossary_data) regresyon testleri.

Bu testler glossary/*.json'daki onarımların kalıcılığını doğrular:
  * ASCII-bozuk TR değerleri düzeltildi (bağlam-farkında, kör deasciify değil).
  * Toplu sözlük-import çöpü (science_engineering / physics / math / cs) silindi.
  * do_not_translate.json hijyeni yapıldı, estrogen->östrojen taşındı.
  * KRITIK: 'sinir ağı' (neural network) korundu, 'sınır ağı' yapılmadı.
  * GlossaryEngine her book_type için hatasız yükleniyor.
Ayrıca onarım scriptinin idempotent olduğu doğrulanır.

G2c (KARAR 25, 2026-10-05): genel alan sözlükleri (science_engineering,
physics, math, cs, default, technical, felsefe türü) SİLİNDİ — sabit genel sözlük
yok, her kitap kendi sözlüğünü çıkarır (latex_pipeline/kitap_sozlugu.py).
O dosyaların veri onarım testleri ve onarım scriptinin idempotentlik testi
(script yalnız silinen dosyaları işliyordu) kaldırıldı; kalan iki dosyanın
(curated.json, do_not_translate.json) testleri durur.
"""
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GD = ROOT / "glossary"



def _terms(fname):
    return json.loads((GD / fname).read_text(encoding="utf-8"))["terms"]


# ---------------------------------------------------------------------------
# DATA-09: do_not_translate.json hijyeni
# ---------------------------------------------------------------------------
def test_estrogen_not_in_dnt():
    # G2c: taşındığı default.json silindi; do_not_translate.json hijyeni durur.
    dnt = _terms("do_not_translate.json")
    assert "estrogen" not in dnt


def test_dnt_only_keep_original_remains():
    """do_not_translate.json'da yalnızca keep_original=true girişler kalmalı."""
    dnt = _terms("do_not_translate.json")
    for en, info in dnt.items():
        assert info.get("keep_original") is True, f"{en} keep_original değil"


def test_serotonin_flagged_keep_original():
    ser = _terms("do_not_translate.json").get("serotonin")
    assert ser is not None and ser.get("keep_original") is True


# ---------------------------------------------------------------------------
# GlossaryEngine: her book_type için hatasız yükleme + bozuk değer kalmadı
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("book_type", [
    "science", "engineering", "technical", "computer", "ml",
    "physics", "philosophy", "novel", "general",
])
def test_glossary_engine_loads(book_type):
    from latex_pipeline.glossary import GlossaryEngine
    eng = GlossaryEngine(GD, book_type=book_type)
    assert len(eng._terms) > 0


def test_no_capital_i_corruption_in_turkish_values():
    """Yüklenen TR değerlerinde ı->I bozulması kalmadı (İng. özel adlar hariç)."""
    import re
    from latex_pipeline.glossary import GlossaryEngine
    cap_i = re.compile(r"[a-zçğıöşü]I|I[a-zçğıöşü]")
    eng = GlossaryEngine(GD, book_type="science")
    # keep_original İngilizce özel adları (Kinsey Institute, Washington Post)
    # ele; yalnızca çeviri değerlerini denetle.
    bad = [
        (t.en, t.tr) for t in eng._terms.values()
        if cap_i.search(t.tr) and not t.keep_original
        and t.tr != t.en  # çevrilmemiş İngilizce değil
    ]
    assert bad == [], f"Bozuk TR değerleri: {bad}"
