# -*- coding: utf-8 -*-
"""Yol B DALGA 2 — sözlük veri onarımı (grup: data2) regresyon testleri.

Kapsanan bulgular:
  * GLS-07 : wrong_alternatives'ten MEŞRU eş-anlamlı Türkçe çeviriler çıkarıldı
             (science_engineering.json + curated.json). Pass 2 artık doğru
             çevirileri (yapay sinir ağı, dikgen, nokta çarpımı) ezmez.
  * GLS-09 : curated.json'daki genel girişler ETIKETSIZ (alan filtresine
             takılmaz).
  * DATA-07: science_engineering.json 'context' -> domain (muhafazakâr); net
             tek-alanlı girişler etiketli, belirsizler etiketsiz.
  * TASK4  : physics/math/cs.json düşük-öncelik alan-dışı absürt girişler silindi
             (INERT olduklarından davranış değişmez; doğru-niş girişler korundu).

Ayrıca GlossaryEngine'in her book_type için hatasız yüklendiği ve domain
filtrelemesinin doğru çalıştığı (dar tür = çapraz-alan terimi düşer; 'science' =
tüm domainler aktif = filtreleme yok) davranışsal olarak doğrulanır.

G2c (KARAR 25, 2026-10-05): science_engineering/physics/math/cs .json ve
felsefe türü sözlüğü SİLİNDİ (sabit genel sözlük yok; kitap sözlüğü:
latex_pipeline/kitap_sozlugu.py). O dosyaların veri testleri (GLS-07
science_engineering kısmı, DATA-07, TASK4, felsefe türü sözlüğü satırı)
kaldırıldı; curated.json testleri ve domain filtresi testleri durur.

FX5b (KARAR 35, 2026-10-05): tek bir kitaba özgü girişler (felsefe türü alanı)
curated.json'dan çıkarıldı (açık yayın temizliği; bekçi:
tests/test_FX5b_g.py). GLS-09'un o girişleri sınayan etiket ve kanonik biçim
testleri kaldırıldı; genel girişlerin etiketsizliği ve her kitap türünde
hatasız yükleme testleri durur.
"""
import json
from pathlib import Path

import pytest

from latex_pipeline.glossary import GlossaryEngine

ROOT = Path(__file__).resolve().parent.parent
GD = ROOT / "glossary"


def _terms(fname):
    return json.loads((GD / fname).read_text(encoding="utf-8"))["terms"]


def _eng(book_type):
    return GlossaryEngine(GD, book_type=book_type)


# ---------------------------------------------------------------------------
# GLS-07: meşru eş-anlamlı wrong_alternatives çıkarıldı
# ---------------------------------------------------------------------------
def test_gls07_effective_orthogonal_after_collision():
    """curated.json (yükleme sırasında kazanan) da temizlendiğinden, motorda
    yüklenen 'orthogonal' girişinin wrong_alternatives'i boştur (yoksa
    science_engineering fix'i nötrlenirdi)."""
    eng = _eng("science")
    for k in ["orthogonal", "orthogonal basis", "orthogonal projection",
              "orthonormal basis"]:
        e = eng._terms.get(k)
        assert e is not None and e.wrong_alternatives == [], k


def test_gls07_curated_inner_product_and_activation():
    cur = _terms("curated.json")
    assert cur["inner product"].get("wrong_alternatives") == ["çarpım işareti"]
    # 'etkinleştirme fonksiyonu' meşru -> çıkarıldı; 'eylem fonksiyonu' yanlış -> kaldı
    assert cur["activation function"].get("wrong_alternatives") == ["eylem fonksiyonu"]


def test_gls07_curated_keeps_deliberate_standardization():
    """Bağlam-direktifli bilinçli standardizasyon (nozzle: 'nozul değil lüle',
    token) ve gerçekten-yanlış alternatifler (subsonic) DOKUNULMADAN kalır."""
    cur = _terms("curated.json")
    assert "nozul" in cur["nozzle"].get("wrong_alternatives", [])
    assert "alt ses" in cur["subsonic"].get("wrong_alternatives", [])
    assert "jeton" in cur["token"].get("wrong_alternatives", [])


# ---------------------------------------------------------------------------
# GLS-09: genel girişler etiketsiz
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("en", [
    # genel/biyoloji/psikoloji/ekonomi/AI -> ETIKETSIZ kalmalı
    "machine learning", "artificial intelligence", "neural network",
    "cognitive dissonance", "supply and demand", "pair bonding",
    "prefrontal cortex", "praxeology",
])
def test_gls09_general_curated_untagged(en):
    info = _terms("curated.json").get(en)
    assert info is not None
    assert "domain" not in info, f"{en} yanlışlıkla etiketlendi"


# ---------------------------------------------------------------------------
# GlossaryEngine her book_type için hatasız yüklenir (regresyon kapısı)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("book_type", [
    "science", "engineering", "technical", "computer", "ml", "physics",
    "philosophy", "novel", "general", "psychology", "math", "cs",
])
def test_engine_loads_all_book_types(book_type):
    assert len(_eng(book_type)._terms) > 0
