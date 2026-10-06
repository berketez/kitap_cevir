"""G kulvarı (6 Ekim 2026): kitap türünün adı ve kitap sözlüğünün önceliği.

1. Felsefe / kişisel gelişim kitaplarının türü ``philosophy`` (tür listesinin
   tek kaynağı ``translate_book.KITAP_TURLERI``).
2. KARAR 25 (kitap sözlüğü önceliklidir): kitabın sözlüğü
   (``glossary_shared.tsv``) bir terimin çevirisini açıkça seçiyorsa genel
   sözlüğün (``glossary/curated.json``) o çeviriyi "yanlış" sayan kuralı o
   kitapta uygulanmaz. Belirti: bir test kitabının elle doğrulanmış
   çevirisi QC 79, çıkış 3; tek CRITICAL bulgu ``wrong_translation``
   "jeton" — oysa kitabın sözlüğü "token" için "jeton"u seçiyor.

Testlerdeki bütün cümleler sentetiktir (KARAR 26: kitap metni yok).
"""

from __future__ import annotations

import json
import sys

import pytest

import translate_book as tb
from latex_pipeline import qc_engine as qc_mod
from latex_pipeline.kitap_sozlugu import KitapSozlugu, SozlukGirdisi
from latex_pipeline.qc_engine import QCEngine

# Modelsiz run_pipeline kurulumu: İngilizce kilit testinin fikstürleri
# (yalnız fikstür / yardımcı; test sınıfları içe aktarılmaz).
from tests.test_kaynak_dil_20260928 import (  # noqa: F401
    make_book,
    pipeline_env,
    restore_root_logger,
)

# ===========================================================================
# 1. Kitap türü: philosophy
# ===========================================================================
class TestTurTespiti:
    def test_felsefe_dosya_adi(self):
        assert tb.detect_book_type("stoicism_guide.epub") == "philosophy"
        assert tb.detect_book_type("Kant ve Ahlak.pdf") == "philosophy"

    def test_kisisel_gelisim_dosya_adi(self):
        assert tb.detect_book_type("The Self Help Handbook.pdf") == "philosophy"
        assert tb.detect_book_type("kişisel gelişim rehberi.pdf") == "philosophy"

    def test_kisisel_gelisim_icerik(self):
        md = ("A practical self-help guide on mindfulness and personal growth "
              "for busy readers who want calmer mornings.")
        assert tb.detect_book_type("adsiz.pdf", md) == "philosophy"

    @pytest.mark.parametrize("ad", [
        "stoicism_guide.epub", "great_novel.epub", "quantum_mechanics.pdf",
        "random_name.epub", "The Self Help Handbook.pdf",
    ])
    def test_tespit_yalniz_gecerli_tur_doner(self, ad):
        tur = tb.detect_book_type(ad)
        assert tur in tb.KITAP_TURLERI


class TestCliSecenekleri:
    def _parse(self, monkeypatch, *ek):
        monkeypatch.setattr(sys, "argv", ["translate_book.py", "kitap.pdf", *ek])
        return tb.parse_args()

    def test_philosophy_kabul(self, monkeypatch):
        assert self._parse(monkeypatch, "--book-type", "philosophy").book_type \
            == "philosophy"

    def test_listede_olmayan_tur_reddedilir(self, monkeypatch, capsys):
        with pytest.raises(SystemExit):
            self._parse(monkeypatch, "--book-type", "medical")
        hata = capsys.readouterr().err
        assert "philosophy" in hata

    def test_secenekler_tur_listesiyle_ayni(self, monkeypatch, capsys):
        with pytest.raises(SystemExit):
            self._parse(monkeypatch, "--help")
        yardim = capsys.readouterr().out
        assert "{" + ",".join(tb.KITAP_TURLERI) + "}" in yardim


# ===========================================================================
# 2. Kitap sözlüğü genel sözlüğün "yanlış karşılık" kuralını geçersiz kılar
# ===========================================================================
_GENEL_SOZLUK = {
    "terms": {
        "token": {"tr": "belirteç", "domain": "ml", "priority": "high",
                  "wrong_alternatives": ["jeton", "simge"]},
        "tokens": {"tr": "belirteçler", "domain": "ml", "priority": "high",
                   "wrong_alternatives": ["jetonlar"]},
        "subsonic": {"tr": "ses altı", "domain": "physics",
                     "wrong_alternatives": ["alt ses"]},
        "nozzle": {"tr": "lüle", "domain": "physics", "priority": "high",
                   "wrong_alternatives": ["nozul", "nozulun"]},
    }
}

_EN_JETON = (
    "\\chapter{Access}\n\n"
    "A personal token is required before the archive opens. Each token "
    "works only once, and expired tokens are removed every night.\n"
)
_TR_JETON = (
    "\\chapter{Erişim}\n\n"
    "Arşiv açılmadan önce kişisel bir jeton (token) gerekir. Her jeton "
    "yalnız bir kez çalışır; süresi dolan jetonlar her gece silinir.\n"
)


@pytest.fixture
def genel_sozluk_klasoru(tmp_path):
    gdir = tmp_path / "glossary"
    gdir.mkdir()
    (gdir / "curated.json").write_text(
        json.dumps(_GENEL_SOZLUK, ensure_ascii=False), encoding="utf-8")
    return str(gdir)


def _sozluk(*ciftler) -> KitapSozlugu:
    return KitapSozlugu([SozlukGirdisi(k, h) for k, h in ciftler], "en")


def _bulgular(rapor, denetim="wrong_translation"):
    return [i for i in rapor.issues if i.check_name == denetim]


def _yanlis_sozcukler(rapor):
    return {i.message.split("'")[1] for i in _bulgular(rapor)}


class TestYanlisKarsilikMuafiyeti:
    def test_sozluksuz_kural_isler(self, genel_sozluk_klasoru):
        """Kontrol: kitap sözlüğü yokken genel kural eskisi gibi uygulanır."""
        rapor = QCEngine(glossary_dir=genel_sozluk_klasoru,
                         book_type="science").analyze(_TR_JETON, _EN_JETON)
        assert {"jeton", "jetonlar"} <= _yanlis_sozcukler(rapor)

    def test_kitap_secimi_kurali_kaldirir(self, genel_sozluk_klasoru):
        rapor = QCEngine(glossary_dir=genel_sozluk_klasoru, book_type="science",
                         kitap_sozlugu=_sozluk(("token", "jeton (token)"))
                         ).analyze(_TR_JETON, _EN_JETON)
        assert _bulgular(rapor) == []

    def test_bilesik_secim_tek_sozcugu_muaf_kilmaz(self, genel_sozluk_klasoru):
        """"access token -> erişim jetonu" seçimi tek başına "jeton"u
        kapsamaz: metindeki "erişim jetonu" kurala zaten takılmaz, tek başına
        "jeton" kitabın seçimi değildir (kural onda işler)."""
        tr = ("\\chapter{Erişim}\n\n"
              "Kapı erişim jetonunu okur ve erişim jetonu geçerliyse açılır. "
              "Arşiv için bir jeton daha gerekir.\n")
        en = ("\\chapter{Access}\n\n"
              "The door reads the access token and opens if the access token "
              "is valid. The archive needs one more token.\n")
        rapor = QCEngine(glossary_dir=genel_sozluk_klasoru, book_type="science",
                         kitap_sozlugu=_sozluk(("access token", "erişim jetonu"))
                         ).analyze(tr, en)
        assert _yanlis_sozcukler(rapor) == {"jeton"}

    def test_secilmeyen_yanlis_karsilik_hala_bulunur(self, genel_sozluk_klasoru):
        """Muafiyet yalnız kitabın seçtiği karşılığa: "simge" seçilmedi."""
        tr = _TR_JETON + "\nKapıdaki simge de aynı işi görür.\n"
        rapor = QCEngine(glossary_dir=genel_sozluk_klasoru, book_type="science",
                         kitap_sozlugu=_sozluk(("token", "jeton (token)"))
                         ).analyze(tr, _EN_JETON)
        assert _yanlis_sozcukler(rapor) == {"simge"}

    def test_kisa_secim_uzun_ifadeyi_kapsamaz(self, genel_sozluk_klasoru):
        """"sound -> ses" seçimi "alt ses" kuralını kaldırmaz."""
        tr = "\\chapter{Uçuş}\n\nUçak alt ses hızında süzüldü ve sessizce indi.\n"
        en = "\\chapter{Flight}\n\nThe plane glided at subsonic speed and landed.\n"
        rapor = QCEngine(glossary_dir=genel_sozluk_klasoru, book_type="science",
                         kitap_sozlugu=_sozluk(("sound", "ses"))
                         ).analyze(tr, en)
        assert "alt ses" in _yanlis_sozcukler(rapor)

    def test_secimin_cekimli_yanlis_bicimi_muaf(self, genel_sozluk_klasoru):
        """Kitap "nozul" seçtiyse kuralın çekimli "nozulun" biçimi de muaf."""
        tr = "\\chapter{Motor}\n\nNozulun ağzı daraldıkça akış hızlanır.\n"
        en = "\\chapter{Engine}\n\nThe flow speeds up as the nozzle mouth narrows.\n"
        kural = QCEngine(glossary_dir=genel_sozluk_klasoru,
                         book_type="science").analyze(tr, en)
        assert "nozulun" in _yanlis_sozcukler(kural)
        rapor = QCEngine(glossary_dir=genel_sozluk_klasoru, book_type="science",
                         kitap_sozlugu=_sozluk(("nozzle", "nozul"))
                         ).analyze(tr, en)
        assert _bulgular(rapor) == []

    def test_sabit_desen_de_kitap_secimine_uyar(self):
        """Kod içindeki "bilinen yanlış çeviri" deseni de kitap seçimine uyar."""
        tr = "\\chapter{Ağlar}\n\nVeriler bilgisayar ağı üzerinden taşınır.\n"
        en = "\\chapter{Networks}\n\nData travels over the computer network.\n"
        kural = QCEngine().analyze(tr, en)
        assert _bulgular(kural), "kontrol: desen bu metinde bulgu üretmeli"
        rapor = QCEngine(kitap_sozlugu=_sozluk(
            ("computer network", "bilgisayar ağı"))).analyze(tr, en)
        assert _bulgular(rapor) == []


class TestBeklenenKarsilikCelismesi:
    """Genel sözlüğün "şu terim şöyle çevrilmeli" kuralı da kitap sözlüğü
    aynı terim için başka karşılık seçtiyse o kitapta uygulanmaz."""

    _EN = "\\chapter{Shop}\n\nThe token opens the gate.\n"
    _TR = "\\chapter{Dükkân}\n\nJeton (token) kapıyı açar.\n"

    def test_sozluksuz_kural_isler(self, genel_sozluk_klasoru):
        rapor = QCEngine(glossary_dir=genel_sozluk_klasoru,
                         book_type="science").analyze(self._TR, self._EN)
        assert _bulgular(rapor, "glossary_compliance")

    def test_kitap_baska_karsilik_secti(self, genel_sozluk_klasoru):
        rapor = QCEngine(glossary_dir=genel_sozluk_klasoru, book_type="science",
                         kitap_sozlugu=_sozluk(("token", "jeton (token)"))
                         ).analyze(self._TR, self._EN)
        assert _bulgular(rapor, "glossary_compliance") == []

    def test_kitap_ayni_karsiligi_secti_kural_kalir(self, genel_sozluk_klasoru):
        """Çelişki yoksa genel kural (çevrilmemiş terim) çalışmaya devam eder."""
        rapor = QCEngine(glossary_dir=genel_sozluk_klasoru, book_type="science",
                         kitap_sozlugu=_sozluk(("token", "belirteç"))
                         ).analyze(self._TR, self._EN)
        assert _bulgular(rapor, "glossary_compliance")


class TestTexYoluTutarlilikKapali:
    """TeX yolunda sözlük QC'ye yalnız muafiyet için verilir: terim
    tutarlılığı denetimi (G2c) kapalı kalır."""

    _EN = "\n\n".join([
        "The anomaly appeared in the first sensor run.",
        "Engineers traced the anomaly to a loose cable.",
        "A second anomaly showed up after the repair.",
        "The final report lists every anomaly by date.",
    ])
    _TR = "\n\n".join([
        "Anormallik ilk algılayıcı koşusunda göründü.",
        "Mühendisler anormalliği gevşek bir kabloya bağladı.",
        "Onarımdan sonra ikinci bir anomali ortaya çıktı.",
        "Son rapor her anormalliği tarihe göre listeler.",
    ])

    def test_tutarlilik_varsayilan_acik(self):
        rapor = QCEngine(kitap_sozlugu=_sozluk(("anomaly", "anomali"))
                         ).analyze(self._TR, self._EN)
        assert _bulgular(rapor, "term_consistency")

    def test_tutarlilik_kapatilabilir(self):
        rapor = QCEngine(kitap_sozlugu=_sozluk(("anomaly", "anomali")),
                         tutarlilik_denetimi=False).analyze(self._TR, self._EN)
        assert _bulgular(rapor, "term_consistency") == []


# ===========================================================================
# 3. Okuma noktası: run_pipeline (2_derle kitap.json / META türünü buraya verir)
# ===========================================================================
_EN_GOVDE = (
    "\\chapter{Gatekeeping}\n\n"
    "Every visitor receives a paper token at the entrance of the museum. "
    "The token lets the visitor enter one gallery, and the guard keeps the "
    "used token in a small box until the evening count.\n\n"
    "Visitors who lose the token must wait at the desk while a new one is "
    "printed for them by the staff.\n"
)
_TR_GOVDE = (
    "\\chapter{Kapı Denetimi}\n\n"
    "Her ziyaretçi müzenin girişinde kâğıt bir jeton (token) alır. Jeton "
    "ziyaretçinin bir galeriye girmesini sağlar; görevli, kullanılan jetonu "
    "akşam sayımına kadar küçük bir kutuda saklar.\n\n"
    "Jetonunu kaybeden ziyaretçi, görevliler yenisini basana kadar masada "
    "bekler.\n"
)


@pytest.fixture
def qc_kaydi(monkeypatch):
    """run_pipeline'ın kurduğu QCEngine'lerin yapıcı argümanları ve raporları."""
    kayit = {"kwargs": [], "raporlar": []}

    class _KayitliQC(QCEngine):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            kayit["kwargs"].append(k)

        def analyze(self, *a, **k):
            rapor = super().analyze(*a, **k)
            kayit["raporlar"].append(rapor)
            return rapor

    monkeypatch.setattr(qc_mod, "QCEngine", _KayitliQC)
    return kayit


def _tex_yolu_kitabi(root, ad, sozluk_satiri=None):
    out = make_book(root, _EN_GOVDE, ad)
    (out / "source" / "translated_body.tex").write_text(_TR_GOVDE,
                                                       encoding="utf-8")
    if sozluk_satiri is not None:
        (out / "glossary_shared.tsv").write_text(sozluk_satiri + "\n",
                                                 encoding="utf-8")
    return out


def _tex_yolu_kos(out, book_type="science"):
    return tb.run_pipeline(str(out), "Sözlük Kitabı", skip_translate=True,
                           skip_compile=True, skip_qc=False, resume=True,
                           manual_translation=True, book_type=book_type,
                           pdf_path=None)


class TestOkumaNoktasi:
    def test_tur_okunurken_aynen_islenir(self, pipeline_env, qc_kaydi):
        """2_derle kitap.json / META'daki türü run_pipeline'a verir."""
        out = _tex_yolu_kitabi(pipeline_env, "felsefe")
        assert _tex_yolu_kos(out, book_type="philosophy") in (0, 3)
        assert qc_kaydi["kwargs"], "QCEngine kurulmadı"
        assert {k.get("book_type") for k in qc_kaydi["kwargs"]} == {"philosophy"}
        gunluk = (out / "pipeline.log").read_text(encoding="utf-8")
        assert "Book type: philosophy" in gunluk

    def test_tex_yolunda_kitap_sozlugu_jeton_bulgusunu_kaldirir(
            self, pipeline_env, qc_kaydi):
        # Kontrol: sözlüksüz kitapta gerçek genel sözlüğün kuralı işler.
        kontrol = _tex_yolu_kitabi(pipeline_env, "sozluksuz")
        assert _tex_yolu_kos(kontrol) in (0, 3)
        kontrol_bulgu = {s for r in qc_kaydi["raporlar"]
                         for s in _yanlis_sozcukler(r)}
        assert "jeton" in kontrol_bulgu, (
            "glossary/curated.json 'token' için 'jeton'u artık yanlış "
            "saymıyor: test verisini güncelle")

        qc_kaydi["raporlar"].clear()
        qc_kaydi["kwargs"].clear()
        out = _tex_yolu_kitabi(pipeline_env, "sozluklu",
                               "token\tjeton (token)\terişim jetonu")
        assert _tex_yolu_kos(out) in (0, 3)
        assert qc_kaydi["raporlar"]
        bulgu = {s for r in qc_kaydi["raporlar"] for s in _yanlis_sozcukler(r)}
        assert "jeton" not in bulgu
        # TeX yolunda tutarlılık denetimi kapalı (G2c), sözlük yalnız muafiyet.
        assert all(k.get("tutarlilik_denetimi") is False
                   for k in qc_kaydi["kwargs"])
        assert all(k.get("kitap_sozlugu") is not None
                   for k in qc_kaydi["kwargs"])
        assert not [i for r in qc_kaydi["raporlar"] for i in r.issues
                    if i.check_name == "term_consistency"]
        # Sözlük dosyası değiştirilmez.
        assert (out / "glossary_shared.tsv").read_text(encoding="utf-8") == \
            "token\tjeton (token)\terişim jetonu\n"
