"""QC: çevrilmemiş/karışık dilli satırdaki artikel ikinci kez sayılmaz (5 Ekim 2026).

Okuma denetiminde çevrilmemiş bir paragraf hem "untranslated" hem de
içindeki her "the X" / "a X" için ayrı "Ingilizce artikel" bulgusu
üretiyordu. Metinler sentetiktir.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from latex_pipeline.qc_engine import QCEngine  # noqa: E402

TR = ("Bu bölümde modelin eğitim sürecini ve kullanılan veri kümesini "
      "ayrıntılı olarak açıklıyoruz.")
EN = ("We then describe the training procedure in detail, and we report "
      "the results of a careful evaluation on the held-out split, which "
      "confirms that the method generalises to a wide range of inputs.")
# Türkçe cümle + çevrilmemiş İngilizce cümle (Check 2: mixed_language_line).
KARISIK = ("Bu bölümde sonuçları ayrıntılı biçimde özetliyoruz. The evaluation "
           "was carried out on a large held-out split with several different "
           "random seeds and the results were stable across all of them.")


def _bul(rep, check, needle=""):
    return [i for i in rep.issues if i.check_name == check and needle in i.message]


@pytest.fixture
def engine():
    return QCEngine()


def test_cevrilmemis_paragrafta_artikel_sayilmaz(engine):
    rep = engine.analyze(TR + "\n\n" + EN + "\n")
    assert _bul(rep, "untranslated"), "çevrilmemiş paragraf yine raporlanmalı"
    assert _bul(rep, "turkish_naturalness", "artikel") == []


def test_karisik_satirda_artikel_sayilmaz(engine):
    rep = engine.analyze(TR + "\n\n" + KARISIK + "\n")
    assert _bul(rep, "mixed_language_line")
    assert _bul(rep, "turkish_naturalness", "artikel") == []


def test_turkce_cumledeki_artikel_hala_yakalanir(engine):
    rep = engine.analyze(TR + "\n\nBu görevde a spacecraft ve bir füze kullanıldı.\n")
    assert _bul(rep, "turkish_naturalness", "'a spacecraft'")


def test_kayit_her_analizde_sifirlanir(engine):
    engine.analyze(TR + "\n\n" + KARISIK + "\n")
    rep = engine.analyze(TR + "\n\nBu görevde a spacecraft ve bir füze kullanıldı.\n")
    assert _bul(rep, "turkish_naturalness", "'a spacecraft'")


def test_karisik_satir_onemi_cevrilmemis_miktarla_olceklenir(engine):
    """Artikel artık çift sayılmadığı için yarısı çevrilmemiş paragraf eşiğin
    altında kalmayı önem derecesinden almalı (çevrilmemiş satırla aynı eşikler)."""
    rep_k = engine.analyze(TR + "\n\n" + KARISIK + "\n")   # ~140 karakter İngilizce
    rep_u = engine.analyze(TR + "\n\n" + TR + " " + EN + " " + EN + "\n")
    sev_k = [i.severity for i in _bul(rep_k, "mixed_language_line")]
    sev_u = [i.severity for i in _bul(rep_u, "mixed_language_line")]
    assert sev_k == ["high"], sev_k
    assert sev_u == ["critical"], sev_u
    assert rep_u.score < 90  # min_qc_score: yarısı çevrilmemiş paragraf eşiği geçmez
