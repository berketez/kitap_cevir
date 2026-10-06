"""G2c (5 Ekim 2026) — KİTAP SÖZLÜĞÜ (Berke KARAR 25).

"Tek veya birkaç kitaba göre sözlük olmaz, tüm kitaplar için düşünmeliyiz":
sabit genel alan sözlüğü yok; her kitap kendi sözlüğünü KAYNAK DİLDE çıkarır
(``latex_pipeline/kitap_sozlugu.py``, kitap klasöründe ``glossary_shared.tsv``
— "PDF'den TeX çıkar" yolunun biçimiyle birebir).

Kapsam:
  (1) aday terim çıkarımı (6 dil; matematik/komut/atıf maskeli; deterministik)
  (2) terim çevirisi: bağlamlı, seçili motorun genel çağrısıyla
      (``BookTranslator.translate_batch_raw``); curated.json İngilizcede önce
  (3a) istem bloğu: HY-MT resmî "terminology intervention" biçimi
  (3b) verify_terms: yalnız özgün dilde kalmış terim, tam eşleşme
  (3c) QC term_consistency UYARISI (otomatik değiştirme yok)
  (4) temizlik: genel alan sözlükleri ve ölü modüller silindi
  Uçtan uca: Almanca ve İngilizce SENTETİK gövde -> sözlük -> sahte arka uç
  (tests/fakes) ile çeviri -> tsv -> verify_terms -> QC uyarısı.

Bütün metinler SENTETİKTİR (kural 9; kitap metni kopyalanmadı). Model
yüklenmez, gerçek sunucuya bağlanılmaz.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from latex_pipeline import kitap_sozlugu as ks
from latex_pipeline.kitap_sozlugu import (
    KitapSozlugu,
    SozlukGirdisi,
    TerimAdayi,
)

PROJE = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Sentetik gövdeler (kendi yazdığımız cümleler)
# ---------------------------------------------------------------------------

ALMANCA_GOVDE = r"""\chapter{Wärmeleitung in Platten}

Der Wärmestrom durch eine Platte hängt von der Temperaturdifferenz ab. Wir betrachten zuerst den stationären Fall, in dem sich nichts ändert \cite{muster2001}.

Die thermische Leitfähigkeit des Materials bestimmt, wie groß der Wärmestrom bei gegebener Temperaturdifferenz wird. Für Kupfer ist die thermische Leitfähigkeit sehr hoch.

Es gilt $\dot{Q} = \lambda A \Delta T / d$, wobei $\lambda$ die Leitfähigkeit bezeichnet. Der Wärmestrom steigt also linear mit der Temperaturdifferenz.

Die Biot-Zahl vergleicht den Widerstand im Inneren mit dem Widerstand an der Oberfläche. Ist die Biot-Zahl klein, darf man die Temperatur im Körper als gleichmäßig ansehen. Eine kleine Biot-Zahl vereinfacht die Rechnung, und der Wärmestrom lässt sich leicht abschätzen.
"""

INGILIZCE_GOVDE = r"""\section{Orbital Elements}

The true anomaly $\nu$ gives the angle between the periapsis and the current position of the satellite. We measure the true anomaly in the orbital plane.

From the true anomaly we can compute the eccentric anomaly $E$ with a simple relation. The eccentric anomaly is an auxiliary angle defined on a reference circle \cite{vallado}.

The semi-latus rectum $p$ fixes the size of the orbit. For a circle, the semi-latus rectum equals the radius, and the eccentric anomaly coincides with the true anomaly.

Kepler's equation links the mean anomaly to the eccentric anomaly. Solving this equation numerically is a standard exercise; the mean anomaly grows linearly with time.
"""


def _terimler(adaylar):
    return [a.terim for a in adaylar]


# ===========================================================================
# (1) Aday terim çıkarımı
# ===========================================================================

class TestAdayCikarimi:
    def test_almanca_bilesik_ad_ve_obek(self):
        t = _terimler(ks.aday_terimler(ALMANCA_GOVDE, "de"))
        assert "Wärmestrom" in t
        assert "Temperaturdifferenz" in t
        assert "Biot-Zahl" in t
        assert "thermische Leitfähigkeit" in t

    def test_ingilizce_cok_sozcuklu_terimler(self):
        t = _terimler(ks.aday_terimler(INGILIZCE_GOVDE, "en"))
        for terim in ("true anomaly", "eccentric anomaly", "semi-latus rectum",
                      "mean anomaly"):
            assert terim in t, (terim, t)

    def test_matematik_komut_atif_maskelenir(self):
        t = " ".join(_terimler(ks.aday_terimler(
            ALMANCA_GOVDE + INGILIZCE_GOVDE, "en")))
        for cop in ("lambda", "dot", "cite", "muster", "vallado", "nu"):
            assert cop not in t.split(), (cop, t)

    def test_kenar_durak_kelime_olamaz(self):
        for dil, govde in (("de", ALMANCA_GOVDE), ("en", INGILIZCE_GOVDE)):
            durak = ks._durak_kelimeler(dil)
            for a in ks.aday_terimler(govde, dil):
                kelimeler = a.terim.lower().split()
                assert kelimeler[0] not in durak and kelimeler[-1] not in durak

    def test_deterministik(self):
        a1 = ks.aday_terimler(INGILIZCE_GOVDE, "en")
        a2 = ks.aday_terimler(INGILIZCE_GOVDE, "en")
        assert [(a.terim, a.siklik, a.puan) for a in a1] == \
            [(a.terim, a.siklik, a.puan) for a in a2]

    def test_en_cok_k_aday(self):
        assert len(ks.aday_terimler(INGILIZCE_GOVDE, "en", en_cok=2)) == 2
        assert ks.aday_terimler("", "en") == []
        # desteklenmeyen dilde (durak listesi yok) aday çıkarılmaz
        assert ks.aday_terimler(INGILIZCE_GOVDE, "zh") == []

    def test_ilk_cumle_baglami(self):
        a = {x.terim: x for x in ks.aday_terimler(INGILIZCE_GOVDE, "en")}
        assert "true anomaly" in a["eccentric anomaly"].ilk_cumle  # ilk geçiş
        assert a["eccentric anomaly"].ilk_cumle.startswith("From the true")
        assert "$" not in a["eccentric anomaly"].ilk_cumle  # matematik maskeli

    def test_ad_onu_sifat_ve_islev_sozcugu_elenir(self):
        govde = ("\n\n".join(
            f"The specific impulse of stage {i} is high. Moreover, the "
            f"specific impulse matters. The engine burns fuel." for i in range(4)))
        t = _terimler(ks.aday_terimler(govde, "en"))
        assert "specific impulse" in t
        assert "specific" not in t       # ad önü sıfat (öbek sonu oranı 0)
        assert "Moreover" not in t       # cümle başı büyük harf, özel ad değil

    def test_atif_etiketi_aday_degil(self):
        govde = "\n\n".join(
            f"As Figure {i} shows, the nozzle expands the gas." for i in range(5))
        assert "Figure" not in _terimler(ks.aday_terimler(govde, "en"))

    def test_rusca_sifat_tek_basina_aday_degil(self):
        govde = "\n\n".join([
            "Удельный импульс двигателя растёт с давлением.",
            "Мы оцениваем удельный импульс для разных топлив.",
            "Высокий удельный импульс нужен ракете.",
        ])
        t = _terimler(ks.aday_terimler(govde, "ru"))
        assert any("импульс" in x for x in t)
        assert "удельный" not in [x.lower() for x in t]

    def test_fransizca_eksiltme_ve_baglayici(self):
        govde = "\n\n".join([
            "La vitesse de libération dépend de la masse.",
            "On calcule la vitesse de libération pour chaque planète.",
            "L'orbite circulaire est simple, et l'orbite reste stable.",
        ])
        t = _terimler(ks.aday_terimler(govde, "fr"))
        assert "vitesse de libération" in t
        assert all(not x.startswith("l'") for x in t)


# ===========================================================================
# Dosya biçimi (TeX yoluyla birebir)
# ===========================================================================

class TestTsvBicimi:
    def test_yazilan_bayt_bicimi(self, tmp_path):
        p = tmp_path / ks.SOZLUK_DOSYA_ADI
        ks.tsv_yaz(p, [SozlukGirdisi("Wärmestrom", "ısı akısı"),
                       SozlukGirdisi("Biot-Zahl", "Biot sayısı", not_="boyutsuz")])
        b = p.read_bytes()
        assert not b.startswith(b"\xef\xbb\xbf")         # BOM yok
        assert b"\r" not in b                              # LF
        assert b.decode("utf-8") == (
            "Wärmestrom\tısı akısı\nBiot-Zahl\tBiot sayısı\tboyutsuz\n")

    def test_okuma_dayanikli(self, tmp_path):
        p = tmp_path / ks.SOZLUK_DOSYA_ADI
        p.write_bytes(("\ufeff# yorum\r\nSchub\titki\r\n\r\nbozuk satır\r\n"
                       "\tboş kaynak\r\nSchub\tikinci\r\nnozzle\tlüle\tnot\r\n"
                       ).encode("utf-8"))
        g = ks.tsv_oku(p)
        assert [(x.kaynak, x.hedef, x.not_) for x in g] == [
            ("Schub", "itki", ""), ("nozzle", "lüle", "not")]
        assert ks.tsv_oku(tmp_path / "yok.tsv") == []

    def test_gidis_donus_ve_sekme_temizligi(self, tmp_path):
        p = tmp_path / "g.tsv"
        s = KitapSozlugu([SozlukGirdisi("a\tb", "c\nd")], "en")
        s.tsv_yaz(p)
        assert p.read_text(encoding="utf-8") == "a b\tc d\n"
        assert KitapSozlugu.dosyadan_yukle(p).sozluk() == {"a b": "c d"}

    def test_mevcut_tex_yolu_sozlugu_okunur(self, tmp_path):
        # TeX yolundaki üç sütunlu biçim (not sütunu) aynı okuyucudan geçer.
        p = tmp_path / "g.tsv"
        p.write_text("agent\tajan\ttemel terim\nsubagent\talt ajan\n",
                     encoding="utf-8")
        assert KitapSozlugu.dosyadan_yukle(p).sozluk() == {
            "agent": "ajan", "subagent": "alt ajan"}


# ===========================================================================
# (2) Terim çevirisi — sahte çevirmen (ördek tipi) ile birim düzeyi
# ===========================================================================

class _SozlukCevirmen:
    """translate_batch_raw sözleşmesi: her paragrafı sözlükten çevirir."""

    def __init__(self, tablo, cumle="Kısa bir açıklama cümlesi."):
        self.tablo = tablo
        self.cumle = cumle
        self.cagrilar = []

    def translate_batch_raw(self, texts, max_length=512):
        self.cagrilar.append(list(texts))
        out = []
        for t in texts:
            parcalar = [p.strip() for p in t.split("\n\n")]
            out.append("\n\n".join(self.tablo.get(p, self.cumle)
                                   if p in self.tablo or len(parcalar) > 1
                                   else self.tablo.get(p, p)
                                   for p in parcalar))
        return out


def _aday(terim, cumle="Ein Satz.", buyuk=False, n=None):
    return TerimAdayi(terim=terim, siklik=3,
                      sozcuk_sayisi=n or len(terim.split()),
                      ilk_cumle=cumle, buyuk_harfli=buyuk)


class TestTerimCevirisi:
    def test_baglamli_istek_ve_son_paragraf(self):
        cev = _SozlukCevirmen({"Wärmestrom": "ısı akısı"})
        g = ks.terimleri_cevir([_aday("Wärmestrom", "Der Wärmestrom ist groß.")],
                               cev, "de")
        assert [(x.kaynak, x.hedef, x.kaynak_turu) for x in g] == [
            ("Wärmestrom", "ısı akısı", ks.KAYNAK_MODEL)]
        # Her aday BİR kez, ilk cümlesiyle birlikte gönderildi.
        assert cev.cagrilar == [["Der Wärmestrom ist groß.\n\nWärmestrom"]]

    def test_baglam_bozulursa_tek_basina_yedek(self):
        class Tek:
            def __init__(self):
                self.cagrilar = []

            def translate_batch_raw(self, texts, max_length=512):
                self.cagrilar.append(list(texts))
                # bağlamlı istekte tek paragraf döner (ayıklanamaz)
                return ["ısı akısı büyüktür" if "\n\n" in t else "ısı akısı"
                        for t in texts]
        cev = Tek()
        g = ks.terimleri_cevir([_aday("Wärmestrom")], cev, "de")
        assert g[0].hedef == "ısı akısı"
        assert len(cev.cagrilar) == 2 and cev.cagrilar[1] == ["Wärmestrom"]

    def test_yanki_ve_kiril_ve_cumle_reddedilir(self):
        class Kotu:
            def translate_batch_raw(self, texts, max_length=512):
                out = []
                for t in texts:
                    son = t.split("\n\n")[-1]
                    if son == "thrust":
                        out.append(t)                    # yankı (arka uç hatası)
                    elif son == "тяга":
                        out.append("cümle.\n\nтяга")     # Kiril kaldı
                    else:
                        out.append("cümle.\n\n" + " ".join(["çok"] * 12))
                return out
        adaylar = [_aday("thrust"), _aday("тяга"), _aday("Raum")]
        assert ks.terimleri_cevir(adaylar, Kotu(), "en") == []

    def test_ozel_ad_yankisi_kabul(self):
        class Yanki:
            def translate_batch_raw(self, texts, max_length=512):
                return ["Bir cümle.\n\nBarbicane" for _ in texts]
        g = ks.terimleri_cevir([_aday("Barbicane", buyuk=True)], Yanki(), "fr")
        assert g[0].hedef == "Barbicane"

    def test_mt_fail_ve_hata_partiyi_atlar(self):
        class Hata:
            def translate_batch_raw(self, texts, max_length=512):
                raise RuntimeError("geçici")
        assert ks.terimleri_cevir([_aday("Schub")], Hata(), "de") == []

    def test_backend_unavailable_yukari_cikar(self):
        from core.mt_engines.contracts import BackendUnavailable

        class Kapali:
            def translate_batch_raw(self, texts, max_length=512):
                raise BackendUnavailable("sunucu kapalı")
        with pytest.raises(BackendUnavailable):
            ks.terimleri_cevir([_aday("Schub")], Kapali(), "de")

    def test_curated_modelin_onune_gecer(self):
        cev = _SozlukCevirmen({"nozzle": "MODEL"})
        g = ks.terimleri_cevir([_aday("nozzle"), _aday("chamber")], cev, "en",
                               curated={"Nozzle": "lüle"})
        assert g[0].kaynak == "nozzle" and g[0].hedef == "lüle"
        assert g[0].kaynak_turu == ks.KAYNAK_CURATED
        assert all("nozzle" not in t for c in cev.cagrilar for t in c)

    def test_curated_json_okunur(self, tmp_path):
        p = tmp_path / "c.json"
        p.write_text('{"terms": {"blue lantern": {"tr": "", "keep_original": true},'
                     ' "nozzle": {"tr": "lüle"}, "x": 3}}', encoding="utf-8")
        assert ks.curated_terimleri(p) == {"blue lantern": "blue lantern",
                                           "nozzle": "lüle"}
        assert ks.curated_terimleri(tmp_path / "yok.json") == {}
        # gerçek dosya duruyor ve elle doğrulanmış girdileri var
        assert len(ks.curated_terimleri()) > 0

    def test_kitaptaki_curated(self):
        govde = r"The \textbf{nozzle} and the Throat Area matter. $nozzle$"
        g = ks.kitaptaki_curated(govde, {"nozzle": "lüle", "throat area":
                                         "boğaz alanı", "plume": "duman"})
        assert [(x.kaynak, x.hedef) for x in g] == [
            ("nozzle", "lüle"), ("throat area", "boğaz alanı")]


# ===========================================================================
# kitap_sozlugu_hazirla — dosya önceliği, yazma, hata
# ===========================================================================

class TestHazirla:
    def test_mevcut_dosya_yuklenir_uzerine_yazilmaz(self, tmp_path):
        p = tmp_path / ks.SOZLUK_DOSYA_ADI
        p.write_text("Wärmestrom\tısı akımı\n", encoding="utf-8")
        cagrildi = []
        s = ks.kitap_sozlugu_hazirla(ALMANCA_GOVDE, "de", tmp_path,
                                     lambda: cagrildi.append(1))
        assert s.sozluk() == {"Wärmestrom": "ısı akımı"}
        assert cagrildi == []                      # model hiç açılmadı
        assert p.read_text(encoding="utf-8") == "Wärmestrom\tısı akımı\n"

    def test_cikar_cevir_yaz(self, tmp_path):
        tablo = {"Wärmestrom": "ısı akısı", "Temperaturdifferenz":
                 "sıcaklık farkı", "Biot-Zahl": "Biot sayısı",
                 "thermische Leitfähigkeit": "ısıl iletkenlik"}
        s = ks.kitap_sozlugu_hazirla(ALMANCA_GOVDE, "de", tmp_path,
                                     lambda: _SozlukCevirmen(tablo))
        d = s.sozluk()
        for k, v in tablo.items():
            assert d.get(k) == v
        satirlar = (tmp_path / ks.SOZLUK_DOSYA_ADI).read_text(
            encoding="utf-8").splitlines()
        assert "Wärmestrom\tısı akısı" in satirlar

    def test_model_acilamazsa_yazilmaz(self, tmp_path):
        def _patla():
            raise RuntimeError("model yok")
        s = ks.kitap_sozlugu_hazirla(ALMANCA_GOVDE, "de", tmp_path, _patla)
        assert len(s) == 0
        assert not (tmp_path / ks.SOZLUK_DOSYA_ADI).exists()

    def test_ingilizcede_curated_baslangic(self, tmp_path):
        govde = INGILIZCE_GOVDE + "\n\nThe nozzle is short.\n"
        s = ks.kitap_sozlugu_hazirla(govde, "en", tmp_path, lambda: None,
                                     curated={"nozzle": "lüle"})
        assert s.girdiler[0].kaynak == "nozzle"
        assert s.girdiler[0].kaynak_turu == ks.KAYNAK_CURATED


# ===========================================================================
# (3a) İstem bloğu
# ===========================================================================

class TestIstemBloku:
    S = KitapSozlugu([SozlukGirdisi("Wärmestrom", "ısı akısı"),
                      SozlukGirdisi("thermische Leitfähigkeit", "ısıl iletkenlik"),
                      SozlukGirdisi("Leitfähigkeit", "iletkenlik")], "de",
                     istem_acik=True)

    def test_varsayilan_kapali_ve_ortam_degiskeni(self, monkeypatch):
        # İstem desteği ölçülene kadar varsayılan KAPALI (kural 4 + 6).
        monkeypatch.delenv(ks.ISTEM_ORTAM_DEGISKENI, raising=False)
        assert ks.ISTEM_TERIMLERI_VARSAYILAN is False
        kapali = KitapSozlugu(self.S.girdiler, "de")
        assert kapali.istem_bloku("Der Wärmestrom ist groß.") == ""
        monkeypatch.setenv(ks.ISTEM_ORTAM_DEGISKENI, "1")
        acik = KitapSozlugu(self.S.girdiler, "de")
        assert acik.istem_bloku("Der Wärmestrom ist groß.") != ""
        monkeypatch.setenv(ks.ISTEM_ORTAM_DEGISKENI, "0")
        assert not KitapSozlugu(self.S.girdiler, "de").istem_acik

    def test_hymt_resmi_bicim(self):
        b = self.S.istem_bloku("Der Wärmestrom ist groß.")
        assert b == "参考下面的翻译：\nWärmestrom 翻译成 ısı akısı\n\n"

    def test_en_uzun_terim_ve_kok_toleransi(self):
        b = self.S.istem_bloku(
            "Die thermische Leitfähigkeit bestimmt die Wärmeströme.")
        # çoğul 'Wärmeströme' kök anahtarıyla bulunmaz (ö); tekil kök bulunur
        assert "thermische Leitfähigkeit 翻译成 ısıl iletkenlik" in b
        assert "\nLeitfähigkeit 翻译成" not in b      # örtüşen kısa terim yok

    def test_cekimli_bicim_bulunur(self):
        s = KitapSozlugu([SozlukGirdisi("двигатель", "motor")], "ru")
        assert s.segment_terimleri("Тяга двигателя растёт.") == [
            ("двигатель", "motor")]

    def test_sohbet_bicimi_ve_bos(self):
        assert self.S.istem_bloku("Der Wärmestrom.", bicim="chat") == \
            "Use these term translations:\nWärmestrom = ısı akısı\n"
        assert self.S.istem_bloku("Nichts hier.") == ""
        assert KitapSozlugu(istem_acik=True).istem_bloku("Der Wärmestrom.") == ""

    def test_en_cok_terim(self):
        # Rakam sözcük ayırıcıdır: ayrı terimler harf sonekiyle kurulur.
        ekler = "abcdfghijk"
        s = KitapSozlugu([SozlukGirdisi(f"Terminus{e}", f"terim{e}")
                          for e in ekler], "de", istem_acik=True)
        metin = " ".join(f"Terminus{e}" for e in ekler)
        assert len(s.segment_terimleri(metin)) == ks.ISTEM_EN_COK_TERIM

    def test_sizinti_temizligi(self):
        cikti = "参考下面的翻译：\nWärmestrom 翻译成 ısı akısı\n\nIsı akısı büyüktür."
        assert ks.istem_sizintisini_temizle(cikti) == "Isı akısı büyüktür."
        assert ks.istem_sizintisini_temizle("Düz metin.") == "Düz metin."
        assert ks.istem_sizintisini_temizle(
            "Use these term translations:\nOrbit büyüktür.") == "Orbit büyüktür."
        # sohbet biçimi: başlığın ardındaki "X = Y" satırları da gider;
        # başlıktan bağımsız "x = y" içerikli satır kalır
        assert ks.istem_sizintisini_temizle(
            "Use these term translations:\nWärmestrom = ısı akısı\n"
            "Isı akısı büyüktür.\nx = y burada kalır.") == \
            "Isı akısı büyüktür.\nx = y burada kalır."


# ===========================================================================
# (3b) verify_terms — yalnız özgün dilde kalmış terim, tam eşleşme
# ===========================================================================

class TestVerifyTerms:
    S = KitapSozlugu([SozlukGirdisi("Wärmestrom", "ısı akısı"),
                      SozlukGirdisi("Raumerfahrung", "mekân deneyimi"),
                      SozlukGirdisi("Gun-Club", "Gun-Club"),
                      SozlukGirdisi("alpha", "alfa dalgası")], "de")

    def test_turkce_cumlede_degisir(self):
        out = self.S.verify_terms("Bu bölümde Wärmestrom kavramını ele alıyoruz.")
        assert out == "Bu bölümde ısı akısı kavramını ele alıyoruz."
        assert self.S.son_verify_sayisi == 1

    def test_matematik_ve_komut_korunur(self):
        metin = r"Bu $\alpha$ değeri ve \alpha komutu için alpha sözcüğü de var."
        out = self.S.verify_terms(metin)
        assert r"$\alpha$" in out and r"\alpha komutu" in out
        assert "alfa dalgası sözcüğü" in out

    def test_turkce_ek_bozulmaz(self):
        metin = "Bu bölümde Raumerfahrung'u ve Wärmestrom'un etkisini inceliyoruz."
        assert self.S.verify_terms(metin) == metin

    def test_parantez_icindeki_ozgun_terim_kalir(self):
        metin = "Burada mekân deneyimi (Raumerfahrung) için bir örnek veriyoruz."
        assert self.S.verify_terms(metin) == metin

    def test_cevrilmemis_cumleye_dokunulmaz(self):
        metin = "Die Raumerfahrung ist wichtig und der Wärmestrom auch."
        assert self.S.verify_terms(metin) == metin

    def test_baslik_korunur(self):
        metin = "\\section{Wärmestrom}\n\nBu bölümde Wärmestrom var."
        out = self.S.verify_terms(metin)
        assert out.startswith("\\section{Wärmestrom}")
        assert out.endswith("Bu bölümde ısı akısı var.")

    def test_cumle_basi_turkce_buyuk_harf(self):
        s = KitapSozlugu([SozlukGirdisi("impulse", "itki"),
                          SozlukGirdisi("increment", "ıraksak artış")], "en")
        out = s.verify_terms(
            "Impulse bu durumda çok büyüktür. Increment ise daha küçüktür.")
        assert out == ("İtki bu durumda çok büyüktür. Iraksak artış ise daha "
                       "küçüktür.")

    def test_cumle_ortasi_buyuk_harf_baslik_dokunulmaz(self):
        s = KitapSozlugu([SozlukGirdisi("combustion", "yanma")], "en")
        metin = r"Yazar ayrıca \textit{Experimental Combustion} kitabını da yazdı."
        assert s.verify_terms(metin) == metin
        # aynı cümlede küçük harfli geçiş değişir
        assert s.verify_terms("Bu combustion süreci için bir örnek de var.") == \
            "Bu yanma süreci için bir örnek de var."

    def test_yari_cevrilmis_cumle_ve_tablo_dokunulmaz(self):
        s = KitapSozlugu([SozlukGirdisi("axial direction", "eksenel yön"),
                          SozlukGirdisi("mass flow", "kütle akışı")], "en")
        karisik = "Gas leaves lüle çıkışı along axial direction only."
        assert s.verify_terms(karisik) == karisik
        tablo = r"velocity ve mass flow için & 346 \\"
        assert s.verify_terms(tablo) == tablo

    def test_alt_dize_ve_kucuk_harf_varyanti_eslesmez(self):
        metin = "Bu Wärmestromdichte değeri ve wärmestrom yazımı burada."
        assert self.S.verify_terms(metin) == metin

    def test_kimlik_girdisi_ve_bos(self):
        metin = "Gun-Club burada toplandı."
        assert self.S.verify_terms(metin) == metin
        assert KitapSozlugu().verify_terms(metin) == metin
        assert self.S.verify_terms("") == ""

    def test_glossaryengine_imzasiyla_uyumlu(self):
        # Phase 5.75'te GlossaryEngine.verify_terms(metin, term_map={}) yerine
        out = self.S.verify_terms("Bu Wärmestrom büyük.", term_map={})
        assert out == "Bu ısı akısı büyük."


# ===========================================================================
# (3c) QC — term_consistency UYARISI
# ===========================================================================

KAYNAK_ANOMALI = "\n\n".join([
    r"The true anomaly $\theta$ measures the angle along the orbit.",
    "We compute the mean anomaly from the elapsed time.",
    r"The eccentric anomaly $E$ is an auxiliary angle.",
    "A hyperbolic anomaly appears for open orbits.",
])
CEVIRI_ANOMALI = "\n\n".join([
    r"Gerçek anormallik $\theta$ yörünge boyunca açıyı ölçer.",
    "Geçen zamandan ortalama anormalliği hesaplarız.",
    r"Eksantrik anomalisi $E$ yardımcı bir açıdır.",
    "Açık yörüngeler için hiperbolik anormallik ortaya çıkar.",
])
SOZLUK_ANOMALI = KitapSozlugu([SozlukGirdisi("anomaly", "anomali"),
                               SozlukGirdisi("orbit", "yörünge")], "en")


class TestTutarlilik:
    def test_bulgu(self):
        b = ks.tutarlilik_bulgulari(SOZLUK_ANOMALI, KAYNAK_ANOMALI, CEVIRI_ANOMALI)
        assert len(b) == 1
        assert (b[0].kaynak, b[0].hedef, b[0].uyumlu, b[0].farkli) == (
            "anomaly", "anomali", 1, 3)
        assert b[0].ilk_satir == 1 and b[0].benzer == "anormallik"
        ileti = ks.bulgu_iletisi(b[0])
        assert "'anomaly'" in ileti and "'anomali'" in ileti

    def test_ceki_eki_uyumlu_sayilir(self):
        tr = CEVIRI_ANOMALI.replace("anormalliği", "anomaliyi").replace(
            "anormallik", "anomali")
        assert ks.tutarlilik_bulgulari(SOZLUK_ANOMALI, KAYNAK_ANOMALI, tr) == []

    def test_ozgun_dilde_kalan_sayilmaz(self):
        tr = CEVIRI_ANOMALI.replace("anormallik", "anomaly").replace(
            "anormalliği", "anomaly'yi")
        assert ks.tutarlilik_bulgulari(SOZLUK_ANOMALI, KAYNAK_ANOMALI, tr) == []

    def test_paragraf_sayisi_farkli_pencere(self):
        tr = CEVIRI_ANOMALI + "\n\nEk bir paragraf burada duruyor."
        b = ks.tutarlilik_bulgulari(SOZLUK_ANOMALI, KAYNAK_ANOMALI, tr)
        assert b and b[0].kaynak == "anomaly"

    def test_qcengine_uyari_uretir(self):
        from latex_pipeline.qc_engine import QCEngine
        rep = QCEngine(kitap_sozlugu=SOZLUK_ANOMALI).analyze(
            CEVIRI_ANOMALI, KAYNAK_ANOMALI)
        tc = [i for i in rep.issues if i.check_name == "term_consistency"]
        assert len(tc) == 1
        assert tc[0].severity == "low" and tc[0].auto_fixable is False
        assert tc[0].line_num == 1 and "anormallik" in tc[0].message

    def test_sozluksuz_qc_denetim_kosmaz(self):
        from latex_pipeline.qc_engine import QCEngine
        rep = QCEngine().analyze(CEVIRI_ANOMALI, KAYNAK_ANOMALI)
        assert not [i for i in rep.issues if i.check_name == "term_consistency"]

    def test_bulgu_siniri(self):
        from latex_pipeline.qc_engine import QCEngine
        n = ks.TUTARLILIK_EN_COK_BULGU + 2
        ekler = "abcdefghijklmnopqr"[:n]
        sozluk = KitapSozlugu([SozlukGirdisi(f"widget{e}", f"aygıt{e}")
                               for e in ekler], "en")
        src = "\n\n".join(" ".join(f"widget{e}" for e in ekler)
                          for _ in range(3))
        tr = "\n\n".join("Burada başka sözcükler var." for _ in range(3))
        rep = QCEngine(kitap_sozlugu=sozluk).analyze(tr, src)
        tc = [i for i in rep.issues if i.check_name == "term_consistency"]
        assert len(tc) == ks.TUTARLILIK_EN_COK_BULGU
        assert "(+2 terim daha)" in tc[-1].message


# ===========================================================================
# (4) Temizlik — genel alan sözlükleri ve ölü modüller
# ===========================================================================

class TestTemizlik:
    def test_genel_alan_sozlukleri_silindi(self):
        kalan = sorted(p.name for p in (PROJE / "glossary").glob("*.json"))
        assert kalan == ["curated.json", "do_not_translate.json"]

    @pytest.mark.parametrize("modul", ["latex_pipeline.terminology_engine",
                                       "latex_pipeline.glossary_bootstrapper"])
    def test_olu_moduller_silindi(self, modul):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(modul)

    def test_glossaryengine_geriye_uyumlu(self):
        from latex_pipeline.glossary import GlossaryEngine, create_glossary_engine
        eng = GlossaryEngine(glossary_dir=PROJE / "glossary", book_type="science")
        assert set(eng.stats.files_loaded) <= {"curated.json",
                                               "do_not_translate.json"}
        assert eng.term_count > 0
        assert create_glossary_engine(PROJE).term_count == eng.term_count

    def test_glossarymanager_geriye_uyumlu(self):
        from core.glossary import GlossaryManager
        gm = GlossaryManager(book_type="science")
        assert len(gm.terms) > 0
        assert all(t.get("source") == "curated" for t in gm.terms.values())


# ===========================================================================
# Türkçe harf büyüklüğü
# ===========================================================================

def test_turkce_harf_buyuklugu():
    assert ks._tr_bas_buyuk("ivme") == "İvme"
    assert ks._tr_bas_buyuk("ısı") == "Isı"
    assert ks._tr_kucuk("IŞIK İVME") == "ışık ivme"


# ===========================================================================
# Uçtan uca: sentetik gövde -> sözlük -> sahte arka uç (tests/fakes) ->
# tsv -> verify_terms -> QC uyarısı
# ===========================================================================

_OFF = dict(use_memory=False, glossary_mode="off", run_qa=False,
            check_consistency=False)

TR_ALMANCA = {
    "Wärmestrom": "ısı akısı",
    "Temperaturdifferenz": "sıcaklık farkı",
    "Biot-Zahl": "Biot sayısı",
    "thermische Leitfähigkeit": "ısıl iletkenlik",
}
TR_INGILIZCE = {
    "true anomaly": "gerçek anomali",
    "eccentric anomaly": "eksantrik anomali",
    "semi-latus rectum": "yarı parametre",
    "mean anomaly": "ortalama anomali",
}


def _sahte_ceviri(tablo):
    """Sahte sunucunun yanıtı: paragraf paragraf tablodan (bilinmeyen cümle
    için sabit Türkçe cümle)."""
    def _yanit(text):
        return "\n\n".join(tablo.get(p.strip(), "Kısa bir açıklama cümlesi.")
                           for p in text.split("\n\n"))
    return _yanit


@pytest.fixture
def bt_fabrikasi(monkeypatch, fake_ollama):
    import core.mt_engines as cme
    from core.mt_engines.base import EngineSpec
    from core.mt_engines.chat import ChatTemplateEngine
    from core.translator import BookTranslator, TranslationConfig

    spec = EngineSpec(name="ollama:qwen3:8b", backend="ollama", model="qwen3:8b",
                      base_url=fake_ollama.url, source="adhoc", format="chat")
    monkeypatch.setattr(cme, "create_engine",
                        lambda name, *a, **k: ChatTemplateEngine(spec))

    def _yap(kaynak_dil, tablo):
        fake_ollama.reply = _sahte_ceviri(tablo)
        return BookTranslator(TranslationConfig(
            mt_engine=spec.name, source_lang=kaynak_dil, **_OFF))
    return _yap


class TestUctanUca:
    @pytest.mark.parametrize("dil, govde, tablo", [
        ("de", ALMANCA_GOVDE, TR_ALMANCA),
        ("en", INGILIZCE_GOVDE, TR_INGILIZCE),
    ])
    def test_sozluk_cikar_cevir_yaz(self, tmp_path, bt_fabrikasi, dil, govde,
                                    tablo):
        bt = bt_fabrikasi(dil, tablo)
        s = ks.kitap_sozlugu_hazirla(govde, dil, tmp_path, lambda: bt,
                                     curated={})
        d = s.sozluk()
        for k, v in tablo.items():
            assert d.get(k) == v, (k, d)
        tsv = (tmp_path / ks.SOZLUK_DOSYA_ADI).read_text(encoding="utf-8")
        for k, v in tablo.items():
            assert f"{k}\t{v}\n" in tsv
        # yeniden açılış: aynı sözlük dosyadan (model çağrısı yok)
        s2 = ks.kitap_sozlugu_hazirla(govde, dil, tmp_path, None)
        assert s2.sozluk() == d

    def test_almanca_verify_ve_qc(self, tmp_path, bt_fabrikasi):
        from latex_pipeline.qc_engine import QCEngine
        bt = bt_fabrikasi("de", TR_ALMANCA)
        s = ks.kitap_sozlugu_hazirla(ALMANCA_GOVDE, "de", tmp_path, lambda: bt)
        # Modelin çıktısı: bir yerde terim özgün dilde kalmış, bir yerde
        # sözlük dışı karşılık ("ısı akımı") seçilmiş.
        paragraflar = ALMANCA_GOVDE.split("\n\n")
        ceviri = "\n\n".join([
            r"\chapter{Plakalarda ısı iletimi}",
            "Bir plakadan geçen Wärmestrom sıcaklık farkına bağlıdır. Önce "
            "durağan hâli ele alıyoruz.",
            "Malzemenin ısıl iletkenliği, verilen sıcaklık farkında ısı "
            "akımının ne kadar büyük olacağını belirler. Bakırda ısıl "
            "iletkenlik çok yüksektir.",
            r"$\dot{Q} = \lambda A \Delta T / d$ geçerlidir; ısı akısı "
            "sıcaklık farkıyla doğrusal artar.",
            "Biot sayısı içteki direnci yüzeydeki dirençle karşılaştırır. "
            "Biot sayısı küçükse sıcaklık düzgün sayılabilir; küçük bir Biot "
            "sayısı hesabı kolaylaştırır ve ısı akımı kolayca kestirilir.",
        ]) + "\n"
        assert len(paragraflar) == len(ceviri.split("\n\n"))
        duzeltilmis = s.verify_terms(ceviri)
        assert "geçen ısı akısı sıcaklık" in duzeltilmis
        assert "Wärmestrom" not in duzeltilmis
        rep = QCEngine(kitap_sozlugu=s).analyze(duzeltilmis, ALMANCA_GOVDE)
        tc = [i for i in rep.issues if i.check_name == "term_consistency"]
        assert tc and "'Wärmestrom'" in tc[0].message
        assert all(i.auto_fixable is False for i in tc)
        # QC metni DEĞİŞTİRMEZ (uyarı)
        assert "ısı akımı" in duzeltilmis

    def test_istem_blogu_genel_cagriyla_calisir(self, bt_fabrikasi):
        # Terim çevirisi BookTranslator.translate_batch_raw üzerinden (genel
        # çağrı) gider; sahte sunucuya bağlamlı iki paragraflı istek ulaşır.
        bt = bt_fabrikasi("en", TR_INGILIZCE)
        out = bt.translate_batch_raw(["We see it.\n\ntrue anomaly"])
        assert out[0].split("\n\n")[-1] == "gerçek anomali"
