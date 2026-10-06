"""Bileşen manifesti: yükleme, doğrulama, seçim çözümü ve plan toplamı.

TASARIM C (şema ``packaging/manifest.schema.json``), D.2-8 (disk ön denetimi).
Yalnız stdlib: ``jsonschema`` DMG'deki Python'da yoktur, bu yüzden şemanın
yapısal kuralları burada elle de denetlenir (``dogrula``). Şema dosyası ile
bu doğrulayıcının aynı kararı verdiği ``packaging/testler/test_manifest.py``
içinde gerçek ``jsonschema`` ile sınanır (venv'de).

Şemanın anlatamadığı anlamsal kurallar yalnız buradadır:

- bileşen, grup ve öğe kimlikleri tektir; iki öğe aynı ``hedef``e yazmaz
  (macOS koşulları kesişmiyorsa yazabilir);
- bağımlılıklar vardır, döngü yoktur, ``secim`` bileşenine bağımlılık
  yoktur, zorunlu bileşen yalnız zorunlu bileşene bağımlıdır;
- grubun varsayılanı o grubun seçeneğidir;
- ``boyut.indirme_bayt`` her macOS anahtarında o macOS'a uyan öğelerin
  ``bayt`` toplamına eşittir (TASARIM C.1-3: "indirme = öğe baytlarının
  toplamı, kesin"); ``hepsi`` anahtarı koşullu öğeyle birlikte olmaz.

Bileşenler düz sözlük olarak dolaşır (K3'ün ``lisans`` / ``donanim`` /
``durum`` modülleri ``Mapping`` bekler); bu modül yalnız okur, değiştirmez.

Tek kaynak olduğu kurallar (K3 ``kurulum.py`` ve K4 bunları çağırır, yeniden
yazmaz): macOS koşulu (``oge_uygun_mu``: ``"14"``, ``"26+"``), macOS'a göre
boyut seçimi (``bayt_sec``), geçici öğe (``gecici_mi``) ve disk ön denetimi
(``gecici_tepe``, ``ek_alan_tepesi``).
"""
from __future__ import annotations

import json
import os
import platform
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:
    from . import ayarlar, disk
except ImportError:  # düz betik olarak yüklendi (sys.path[0] = packaging/yonetici)
    import ayarlar  # type: ignore[no-redef]
    import disk  # type: ignore[no-redef]

# ---------------------------------------------------------------------------
# Şemadaki sabit kümeler (manifest.schema.json ile aynı; test ikisini karşılaştırır)
# ---------------------------------------------------------------------------
ZORUNLU = "zorunlu"
ISTEGE_BAGLI = "istege_bagli"
SECIM = "secim"
ZORUNLULUKLAR = (ZORUNLU, ISTEGE_BAGLI, SECIM)
GRUP_TURU = "tek_secim"
MIMARI = "arm64"
OGE_TURLERI = ("dosya", "tekerlek", "arsiv", "pkg")
ADIM_TURLERI = ("uv_kur", "tekerlek_kur", "arsiv_ac", "dosya_yerlestir", "tectonic_isit",
                "mactex_pkg_ac", "tex_algila_sina", "lisans_yaz", "surya_manifest_yaz")
DOGRULAMA_TURLERI = ("python_ice_aktar", "dosyalar_sha", "tex_derle", "komut")
DAGITIMLAR = ("dmg", "indirilir", "harici_kurulum")
KOSUL_GENEL_KAPI = "yerel_model_genel_kapi"
KOSUL_MODEL_SIGAR = "model_sigar"
KV_ALANLARI = ("katman", "kv_basligi", "head_dim", "oge_bayt")

_KOK_ALANLAR = {"sema_surumu", "uygulama", "olcum", "gruplar", "bilesenler"}
_UYGULAMA_ALANLAR = {"surum", "yapi", "min_macos", "mimari", "calisma_zamani"}
_OLCUM_ALANLAR = {"tarih", "makine", "macos", "yontem"}
_GRUP_ALANLAR = {"kimlik", "ad", "tur", "varsayilan", "zorunlu"}
_GRUP_ZORUNLU = ("kimlik", "tur", "varsayilan", "zorunlu")
_OGE_ALANLAR = {"kimlik", "url", "yedek_urller", "bayt", "sha256", "hedef",
                "tur", "kosul", "yeniden_kullan"}
_OGE_ZORUNLU = ("kimlik", "url", "bayt", "sha256", "hedef")
_BILESEN_ALANLAR = {"kimlik", "ad", "aciklama", "zorunluluk", "grup", "surum", "bagimliliklar",
                    "donanim_kosulu", "on_kosul", "lisans", "boyut", "ogeler", "adimlar",
                    "dogrulama", "kaldirilabilir", "sayac_beklenen"}
_BILESEN_ZORUNLU = ("kimlik", "ad", "aciklama", "zorunluluk", "boyut", "ogeler", "adimlar",
                    "dogrulama")
_LISANS_ALANLAR = {"spdx", "metin_dosyasi", "metin_sha256", "metin_url", "onay_gerekli",
                   "dagitim", "kaynak_kodu_url"}
_BOYUT_ALANLAR = {"indirme_bayt", "disk_bayt", "ek_ag_bayt_olculen", "olcum_notu"}

_KIMLIK = re.compile(r"^[a-z][a-z0-9-]*$")
_OGE_KIMLIK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_OGE_KIMLIK_EN_COK = 200
_SURUM = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_MACOS_SURUMU = re.compile(r"^[0-9]+(\.[0-9]+)*$")
_TARIH = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_HTTPS = re.compile(r"^https://[^\s/?#@]+(/\S*)?$")
_KOSUL_MACOS = re.compile(r"^([0-9]+)(\+?)$")
_BAYT_ANAHTARI = re.compile(r"^(hepsi|[0-9]+)$")


class ManifestHatasi(ValueError):
    """Manifest okunamadı, geçersiz ya da istenen seçim manifestle uyuşmuyor.

    ``sorunlar`` doğrulamada bulunan bütün sorunları (Türkçe) taşır.
    """

    def __init__(self, ileti: str, sorunlar: Sequence[str] = ()):
        self.sorunlar = list(sorunlar)
        if self.sorunlar:
            ileti = ileti + ": " + "; ".join(self.sorunlar[:10])
            if len(self.sorunlar) > 10:
                ileti += f" (+{len(self.sorunlar) - 10} sorun daha)"
        super().__init__(ileti)


# ---------------------------------------------------------------------------
# macOS sürümü ve koşullar
# ---------------------------------------------------------------------------
def macos_ana_surum(surum_metni: Optional[str] = None) -> Optional[str]:
    """``"26.7"`` → ``"26"``. Verilmezse bu Mac'in sürümü; ölçülemezse None."""
    metin = platform.mac_ver()[0] if surum_metni is None else str(surum_metni)
    bulunan = re.match(r"^([0-9]+)", metin or "")
    return bulunan.group(1) if bulunan else None


def _macos_gerekli(macos: Optional[str]) -> int:
    ana = macos_ana_surum(macos) if macos is not None else macos_ana_surum()
    if ana is None:
        raise ManifestHatasi("macOS sürümü belirlenemedi")
    return int(ana)


def _kosul_uyar(ifade: str, ana: int) -> bool:
    bulunan = _KOSUL_MACOS.match(ifade)
    if not bulunan:
        return False
    sayi = int(bulunan.group(1))
    return ana >= sayi if bulunan.group(2) else ana == sayi


def oge_kosullu_mu(oge: Mapping[str, Any]) -> bool:
    kosul = oge.get("kosul")
    return isinstance(kosul, Mapping) and bool(kosul.get("macos"))


def oge_uygun_mu(oge: Mapping[str, Any], macos: Optional[str] = None) -> bool:
    """Öğe bu macOS'ta iner mi? Koşulsuz öğe her zaman iner.

    ``kosul.macos`` öğeleri: ``"14"`` yalnız macOS 14, ``"26+"`` 26 ve sonrası.
    """
    if not oge_kosullu_mu(oge):
        return True
    ana = _macos_gerekli(macos)
    return any(_kosul_uyar(str(ifade), ana) for ifade in oge["kosul"]["macos"])


def bayt_sec(sozluk: Mapping[str, Any], macos: Optional[str] = None) -> int:
    """Boyut sözlüğünden bu macOS'un değeri.

    ``hepsi`` varsa o; yoksa tam ana sürüm; yoksa ana sürümden küçük ya da
    eşit en büyük anahtar (ör. macOS 27'de ``26`` ölçümü); o da yoksa hata.
    """
    if ayarlar.BOYUT_ORTAK_ANAHTAR in sozluk:
        return int(sozluk[ayarlar.BOYUT_ORTAK_ANAHTAR])
    ana = _macos_gerekli(macos)
    sayisal = sorted(int(k) for k in sozluk if str(k).isdigit())
    uygun = [k for k in sayisal if k <= ana]
    if not uygun:
        raise ManifestHatasi(f"macOS {ana} için boyut ölçümü yok (manifestte: "
                             f"{', '.join(str(k) for k in sayisal) or 'hiçbiri'})")
    return int(sozluk[str(uygun[-1])])


def gecici_mi(oge: Mapping[str, Any]) -> bool:
    """Hedefi ``indirilen/`` altında olan öğe geçicidir (kurulumdan sonra silinir).

    Geçicinin TEK tanımı budur: kurulum da tüketilen öğeyi yalnız ``indirilen/``
    altındaysa siler (``adimlar.ortak.Baglam.indirilen_altinda_mi``); öteki
    öğeler kalıcıdır ve bileşenin ``disk_bayt`` ölçümünün içindedir.
    """
    hedef = str(oge.get("hedef", ""))
    return hedef == ayarlar.INDIRILEN_DIZINI or hedef.startswith(ayarlar.INDIRILEN_DIZINI + "/")


def gecici_toplami(ogeler: Iterable[Mapping[str, Any]]) -> int:
    """Öğelerden geçici olanların (``gecici_mi``) bayt toplamı."""
    return sum(int(o.get("bayt") or 0) for o in ogeler if gecici_mi(o))


# ---------------------------------------------------------------------------
# Doğrulama (stdlib; şemanın yapısal kuralları + anlamsal kurallar)
# ---------------------------------------------------------------------------
def _tamsayi_mi(deger: Any) -> bool:
    return isinstance(deger, int) and not isinstance(deger, bool)


class _Denetci:
    def __init__(self) -> None:
        self.sorunlar: List[str] = []

    def ekle(self, yer: str, ileti: str) -> None:
        self.sorunlar.append(f"{yer}: {ileti}")

    # -- küçük denetimler -----------------------------------------------------
    def nesne(self, deger: Any, yer: str) -> bool:
        if not isinstance(deger, dict):
            self.ekle(yer, "nesne (sözlük) olmalı")
            return False
        return True

    def alanlar(self, deger: Mapping[str, Any], yer: str, izinli: set,
                zorunlu: Iterable[str]) -> None:
        for alan in zorunlu:
            if alan not in deger:
                self.ekle(yer, f"'{alan}' alanı eksik")
        for alan in deger:
            if alan not in izinli:
                self.ekle(yer, f"bilinmeyen alan '{alan}'")

    def metin(self, deger: Any, yer: str, desen: Optional[re.Pattern] = None,
              bos_olabilir: bool = False) -> bool:
        if not isinstance(deger, str) or (not bos_olabilir and not deger):
            self.ekle(yer, "boş olmayan metin olmalı" if not bos_olabilir else "metin olmalı")
            return False
        if desen is not None and not desen.match(deger):
            self.ekle(yer, f"biçim geçersiz ({deger!r})")
            return False
        return True

    def url(self, deger: Any, yer: str) -> None:
        if not isinstance(deger, str) or not _HTTPS.match(deger):
            self.ekle(yer, f"https:// ile başlayan geçerli adres olmalı ({deger!r})")

    def bayt_sozlugu(self, deger: Any, yer: str) -> None:
        if not self.nesne(deger, yer):
            return
        if not deger:
            self.ekle(yer, "en az bir anahtar olmalı")
            return
        for anahtar, bayt in deger.items():
            if not _BAYT_ANAHTARI.match(str(anahtar)):
                self.ekle(yer, f"anahtar 'hepsi' ya da macOS ana sürümü olmalı ({anahtar!r})")
            if not _tamsayi_mi(bayt) or bayt < 0:
                self.ekle(f"{yer}.{anahtar}", "negatif olmayan tamsayı olmalı")
        if ayarlar.BOYUT_ORTAK_ANAHTAR in deger and len(deger) > 1:
            self.ekle(yer, "'hepsi' anahtarı macOS anahtarlarıyla birlikte kullanılamaz")

    # -- öğe -----------------------------------------------------------------
    def oge(self, oge: Any, yer: str) -> None:
        if not self.nesne(oge, yer):
            return
        self.alanlar(oge, yer, _OGE_ALANLAR, _OGE_ZORUNLU)
        if "kimlik" in oge and self.metin(oge["kimlik"], f"{yer}.kimlik", _OGE_KIMLIK):
            if len(oge["kimlik"]) > _OGE_KIMLIK_EN_COK:
                self.ekle(f"{yer}.kimlik", f"en çok {_OGE_KIMLIK_EN_COK} karakter olmalı")
        if "url" in oge:
            self.url(oge["url"], f"{yer}.url")
        if "yedek_urller" in oge:
            if not isinstance(oge["yedek_urller"], list):
                self.ekle(f"{yer}.yedek_urller", "liste olmalı")
            else:
                for i, adres in enumerate(oge["yedek_urller"]):
                    self.url(adres, f"{yer}.yedek_urller[{i}]")
        if "bayt" in oge and (not _tamsayi_mi(oge["bayt"]) or oge["bayt"] < 0):
            self.ekle(f"{yer}.bayt", "negatif olmayan tamsayı olmalı")
        if "sha256" in oge:
            self.metin(oge["sha256"], f"{yer}.sha256", _SHA256)
        if "hedef" in oge:
            sorun = disk.goreli_yol_sorunu(oge["hedef"])
            if sorun:
                self.ekle(f"{yer}.hedef", sorun)
        if "tur" in oge and oge["tur"] not in OGE_TURLERI:
            self.ekle(f"{yer}.tur", f"şunlardan biri olmalı: {', '.join(OGE_TURLERI)}")
        if "kosul" in oge:
            kosul = oge["kosul"]
            if self.nesne(kosul, f"{yer}.kosul"):
                self.alanlar(kosul, f"{yer}.kosul", {"macos"}, ("macos",))
                liste = kosul.get("macos")
                if "macos" in kosul and (not isinstance(liste, list) or not liste):
                    self.ekle(f"{yer}.kosul.macos", "boş olmayan liste olmalı")
                elif isinstance(liste, list):
                    for i, ifade in enumerate(liste):
                        if not isinstance(ifade, str) or not _KOSUL_MACOS.match(ifade):
                            self.ekle(f"{yer}.kosul.macos[{i}]",
                                      f"'14' ya da '26+' biçiminde olmalı ({ifade!r})")
        if "yeniden_kullan" in oge:
            liste = oge["yeniden_kullan"]
            if not isinstance(liste, list):
                self.ekle(f"{yer}.yeniden_kullan", "liste olmalı")
            else:
                for i, aday in enumerate(liste):
                    if not isinstance(aday, str) or not aday.startswith(("~", "/")):
                        self.ekle(f"{yer}.yeniden_kullan[{i}]",
                                  "mutlak ya da '~' ile başlayan yol olmalı")

    # -- donanım koşulu ------------------------------------------------------
    def donanim_tek(self, kosul: Any, yer: str) -> None:
        if not self.nesne(kosul, yer):
            return
        tur = kosul.get("tur")
        if tur == KOSUL_GENEL_KAPI:
            self.alanlar(kosul, yer, {"tur"}, ("tur",))
        elif tur == KOSUL_MODEL_SIGAR:
            self.alanlar(kosul, yer, {"tur", "agirlik_bayt", "kv", "config"},
                         ("tur", "agirlik_bayt", "kv"))
            if "config" in kosul:
                self.nesne(kosul["config"], f"{yer}.config")
            agirlik = kosul.get("agirlik_bayt")
            if "agirlik_bayt" in kosul and (not _tamsayi_mi(agirlik) or agirlik < 1):
                self.ekle(f"{yer}.agirlik_bayt", "pozitif tamsayı olmalı")
            kv = kosul.get("kv")
            if "kv" in kosul and self.nesne(kv, f"{yer}.kv"):
                self.alanlar(kv, f"{yer}.kv", set(KV_ALANLARI), KV_ALANLARI)
                for alan in KV_ALANLARI:
                    if alan in kv and (not _tamsayi_mi(kv[alan]) or kv[alan] < 1):
                        self.ekle(f"{yer}.kv.{alan}", "pozitif tamsayı olmalı")
        else:
            self.ekle(f"{yer}.tur", f"'{KOSUL_GENEL_KAPI}' ya da '{KOSUL_MODEL_SIGAR}' olmalı")

    # -- bileşen -------------------------------------------------------------
    def bilesen(self, b: Any, yer: str) -> None:
        if not self.nesne(b, yer):
            return
        self.alanlar(b, yer, _BILESEN_ALANLAR, _BILESEN_ZORUNLU)
        if "kimlik" in b:
            self.metin(b["kimlik"], f"{yer}.kimlik", _KIMLIK)
        if "ad" in b:
            self.metin(b["ad"], f"{yer}.ad")
        if "aciklama" in b:
            self.metin(b["aciklama"], f"{yer}.aciklama", bos_olabilir=True)
        zorunluluk = b.get("zorunluluk")
        if "zorunluluk" in b and zorunluluk not in ZORUNLULUKLAR:
            self.ekle(f"{yer}.zorunluluk", f"şunlardan biri olmalı: {', '.join(ZORUNLULUKLAR)}")
        grup = b.get("grup")
        if zorunluluk == SECIM:
            if grup is None:
                self.ekle(f"{yer}.grup", "seçim bileşeni bir gruba bağlı olmalı")
            else:
                self.metin(grup, f"{yer}.grup", _KIMLIK)
        elif grup is not None:
            self.ekle(f"{yer}.grup", "yalnız 'secim' bileşeni gruba bağlanır (null olmalı)")
        if "surum" in b:
            self.metin(b["surum"], f"{yer}.surum", bos_olabilir=True)
        if "bagimliliklar" in b:
            liste = b["bagimliliklar"]
            if not isinstance(liste, list):
                self.ekle(f"{yer}.bagimliliklar", "liste olmalı")
            else:
                for i, kimlik in enumerate(liste):
                    self.metin(kimlik, f"{yer}.bagimliliklar[{i}]", _KIMLIK)
                if len(set(map(str, liste))) != len(liste):
                    self.ekle(f"{yer}.bagimliliklar", "aynı bağımlılık iki kez yazılmış")
        if "donanim_kosulu" in b and b["donanim_kosulu"] is not None:
            kosul = b["donanim_kosulu"]
            if isinstance(kosul, list):
                if not kosul:
                    self.ekle(f"{yer}.donanim_kosulu", "liste boş olamaz (koşul yoksa null)")
                for i, tek in enumerate(kosul):
                    self.donanim_tek(tek, f"{yer}.donanim_kosulu[{i}]")
            else:
                self.donanim_tek(kosul, f"{yer}.donanim_kosulu")
        if "on_kosul" in b:
            self.nesne(b["on_kosul"], f"{yer}.on_kosul")
        if "lisans" in b:
            self.lisans(b["lisans"], f"{yer}.lisans")
        if "boyut" in b and self.nesne(b["boyut"], f"{yer}.boyut"):
            boyut = b["boyut"]
            self.alanlar(boyut, f"{yer}.boyut", _BOYUT_ALANLAR,
                         ("indirme_bayt", "disk_bayt", "olcum_notu"))
            for alan in ("indirme_bayt", "disk_bayt"):
                if alan in boyut:
                    self.bayt_sozlugu(boyut[alan], f"{yer}.boyut.{alan}")
            ek = boyut.get("ek_ag_bayt_olculen")
            if "ek_ag_bayt_olculen" in boyut and (not _tamsayi_mi(ek) or ek < 0):
                self.ekle(f"{yer}.boyut.ek_ag_bayt_olculen", "negatif olmayan tamsayı olmalı")
            if "olcum_notu" in boyut:
                self.metin(boyut["olcum_notu"], f"{yer}.boyut.olcum_notu")
        if "ogeler" in b:
            if not isinstance(b["ogeler"], list):
                self.ekle(f"{yer}.ogeler", "liste olmalı")
            else:
                for i, oge in enumerate(b["ogeler"]):
                    self.oge(oge, f"{yer}.ogeler[{i}]")
        if "adimlar" in b:
            if not isinstance(b["adimlar"], list):
                self.ekle(f"{yer}.adimlar", "liste olmalı")
            else:
                for i, adim in enumerate(b["adimlar"]):
                    if not self.nesne(adim, f"{yer}.adimlar[{i}]"):
                        continue
                    if adim.get("tur") not in ADIM_TURLERI:
                        self.ekle(f"{yer}.adimlar[{i}].tur", f"bilinmeyen adım türü {adim.get('tur')!r}")
        if "dogrulama" in b:
            dogrulama = b["dogrulama"]
            if isinstance(dogrulama, list):
                if not dogrulama:
                    self.ekle(f"{yer}.dogrulama", "liste boş olamaz")
                for i, tek in enumerate(dogrulama):
                    self.dogrulama_tek(tek, f"{yer}.dogrulama[{i}]")
            else:
                self.dogrulama_tek(dogrulama, f"{yer}.dogrulama")
        if "kaldirilabilir" in b and not isinstance(b["kaldirilabilir"], bool):
            self.ekle(f"{yer}.kaldirilabilir", "true/false olmalı")
        if "sayac_beklenen" in b and self.nesne(b["sayac_beklenen"], f"{yer}.sayac_beklenen"):
            sayac = b["sayac_beklenen"]
            self.alanlar(sayac, f"{yer}.sayac_beklenen", {"birim", "n"}, ("birim", "n"))
            if "birim" in sayac:
                self.metin(sayac["birim"], f"{yer}.sayac_beklenen.birim")
            if "n" in sayac and (not _tamsayi_mi(sayac["n"]) or sayac["n"] < 0):
                self.ekle(f"{yer}.sayac_beklenen.n", "negatif olmayan tamsayı olmalı")

    def dogrulama_tek(self, dogrulama: Any, yer: str) -> None:
        if not self.nesne(dogrulama, yer):
            return
        if dogrulama.get("tur") not in DOGRULAMA_TURLERI:
            self.ekle(f"{yer}.tur", f"bilinmeyen doğrulama türü {dogrulama.get('tur')!r}")
        if "moduller" in dogrulama:
            moduller = dogrulama["moduller"]
            if not isinstance(moduller, list) or not all(isinstance(m, str) for m in moduller):
                self.ekle(f"{yer}.moduller", "metin listesi olmalı")

    def lisans(self, lisans: Any, yer: str) -> None:
        if not self.nesne(lisans, yer):
            return
        self.alanlar(lisans, yer, _LISANS_ALANLAR, ("spdx", "onay_gerekli"))
        if "spdx" in lisans:
            self.metin(lisans["spdx"], f"{yer}.spdx")
        if "metin_dosyasi" in lisans:
            self.metin(lisans["metin_dosyasi"], f"{yer}.metin_dosyasi")
        if "metin_sha256" in lisans:
            self.metin(lisans["metin_sha256"], f"{yer}.metin_sha256", _SHA256)
        for alan in ("metin_url", "kaynak_kodu_url"):
            if alan in lisans:
                self.url(lisans[alan], f"{yer}.{alan}")
        if "onay_gerekli" in lisans and not isinstance(lisans["onay_gerekli"], bool):
            self.ekle(f"{yer}.onay_gerekli", "true/false olmalı")
        if lisans.get("onay_gerekli") is True:
            for alan in ("metin_sha256", "metin_url"):
                if alan not in lisans:
                    self.ekle(yer, f"onay gerektiren lisansta '{alan}' zorunlu (onay metne bağlanır, "
                                   "tam metin bağlantısı gösterilir)")
        if "dagitim" in lisans and lisans["dagitim"] not in DAGITIMLAR:
            self.ekle(f"{yer}.dagitim", f"şunlardan biri olmalı: {', '.join(DAGITIMLAR)}")


def _kosullar_kesisir(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """İki öğe aynı macOS'ta birlikte inebilir mi?"""
    if not oge_kosullu_mu(a) or not oge_kosullu_mu(b):
        return True
    for x in a["kosul"]["macos"]:
        for y in b["kosul"]["macos"]:
            mx, my = _KOSUL_MACOS.match(str(x)), _KOSUL_MACOS.match(str(y))
            if not mx or not my:
                continue
            nx, ny = int(mx.group(1)), int(my.group(1))
            ax, ay = bool(mx.group(2)), bool(my.group(2))
            if (ax and ay) or (ax and ny >= nx) or (ay and nx >= ny) or (nx == ny):
                return True
    return False


def _anlamsal(veri: Mapping[str, Any], d: _Denetci) -> None:
    bilesenler = [b for b in veri.get("bilesenler") or [] if isinstance(b, dict)]
    gruplar = [g for g in veri.get("gruplar") or [] if isinstance(g, dict)]

    # Kimlik tekliği
    kimlikler: Dict[str, Mapping[str, Any]] = {}
    for i, b in enumerate(bilesenler):
        kimlik = b.get("kimlik")
        if isinstance(kimlik, str):
            if kimlik in kimlikler:
                d.ekle(f"bilesenler[{i}].kimlik", f"'{kimlik}' iki kez tanımlı")
            kimlikler.setdefault(kimlik, b)
    grup_kimlikleri: Dict[str, Mapping[str, Any]] = {}
    for i, g in enumerate(gruplar):
        kimlik = g.get("kimlik")
        if isinstance(kimlik, str):
            if kimlik in grup_kimlikleri:
                d.ekle(f"gruplar[{i}].kimlik", f"'{kimlik}' iki kez tanımlı")
            if kimlik in kimlikler:
                d.ekle(f"gruplar[{i}].kimlik", f"'{kimlik}' bir bileşen kimliğiyle aynı")
            grup_kimlikleri.setdefault(kimlik, g)

    # Öğe kimliği ve hedef tekliği
    tum_ogeler: List[Tuple[str, Mapping[str, Any]]] = []
    for i, b in enumerate(bilesenler):
        for j, oge in enumerate(b.get("ogeler") or []):
            if isinstance(oge, dict):
                tum_ogeler.append((f"bilesenler[{i}].ogeler[{j}]", oge))
    goruldu_kimlik: Dict[str, str] = {}
    for yer, oge in tum_ogeler:
        kimlik = oge.get("kimlik")
        if isinstance(kimlik, str):
            if kimlik in goruldu_kimlik:
                d.ekle(f"{yer}.kimlik", f"'{kimlik}' öğe kimliği {goruldu_kimlik[kimlik]} ile aynı")
            else:
                goruldu_kimlik[kimlik] = yer
    for x in range(len(tum_ogeler)):
        for y in range(x + 1, len(tum_ogeler)):
            (yx, ox), (yy, oy) = tum_ogeler[x], tum_ogeler[y]
            if ox.get("hedef") and ox.get("hedef") == oy.get("hedef") and _kosullar_kesisir(ox, oy):
                d.ekle(f"{yy}.hedef", f"'{oy.get('hedef')}' hedefine {yx} de yazıyor")

    # Gruplar ve seçim bileşenleri
    for i, b in enumerate(bilesenler):
        grup = b.get("grup")
        if b.get("zorunluluk") == SECIM and isinstance(grup, str) and grup not in grup_kimlikleri:
            d.ekle(f"bilesenler[{i}].grup", f"tanımsız grup '{grup}'")
    for i, g in enumerate(gruplar):
        kimlik = g.get("kimlik")
        secenekler = [b.get("kimlik") for b in bilesenler
                      if b.get("zorunluluk") == SECIM and b.get("grup") == kimlik]
        if not secenekler:
            d.ekle(f"gruplar[{i}]", f"'{kimlik}' grubunun seçeneği yok")
        varsayilan = g.get("varsayilan")
        if isinstance(varsayilan, str) and secenekler and varsayilan not in secenekler:
            d.ekle(f"gruplar[{i}].varsayilan", f"'{varsayilan}' bu grubun seçeneği değil")

    # Bağımlılıklar
    for i, b in enumerate(bilesenler):
        for bag in b.get("bagimliliklar") or []:
            if not isinstance(bag, str):
                continue
            if bag == b.get("kimlik"):
                d.ekle(f"bilesenler[{i}].bagimliliklar", "bileşen kendisine bağımlı olamaz")
                continue
            hedef = kimlikler.get(bag)
            if hedef is None:
                d.ekle(f"bilesenler[{i}].bagimliliklar", f"tanımsız bileşen '{bag}'")
                continue
            if hedef.get("zorunluluk") == SECIM:
                d.ekle(f"bilesenler[{i}].bagimliliklar",
                       f"'{bag}' bir seçim bileşeni; seçime bağımlılık kurulamaz")
            if b.get("zorunluluk") == ZORUNLU and hedef.get("zorunluluk") != ZORUNLU:
                d.ekle(f"bilesenler[{i}].bagimliliklar",
                       f"zorunlu bileşen isteğe bağlı '{bag}' bileşenine bağımlı olamaz")
    # Döngü (üç renkli DFS)
    renk: Dict[str, int] = {}

    def gez(kimlik: str, yol: List[str]) -> None:
        renk[kimlik] = 1
        for bag in kimlikler[kimlik].get("bagimliliklar") or []:
            if bag not in kimlikler or bag == kimlik:
                continue
            if renk.get(bag) == 1:
                d.ekle("bilesenler", "bağımlılık döngüsü: " + " → ".join(yol + [kimlik, bag]))
            elif renk.get(bag) is None:
                gez(bag, yol + [kimlik])
        renk[kimlik] = 2

    for kimlik in kimlikler:
        if renk.get(kimlik) is None:
            gez(kimlik, [])

    # Boyut sözlükleri en düşük desteklenen macOS'u da karşılamalı (bayt_sec
    # "ana sürümden küçük ya da eşit en büyük anahtarı" alır): yoksa o Mac'te
    # boyut gösterilemez ve plan kurulamaz. Sürüm çıkarken yakalansın.
    uygulama = veri.get("uygulama") if isinstance(veri.get("uygulama"), dict) else {}
    en_dusuk = macos_ana_surum(str(uygulama.get("min_macos", ""))) if uygulama else None
    if en_dusuk is not None:
        for i, b in enumerate(bilesenler):
            boyut = b.get("boyut")
            if not isinstance(boyut, dict):
                continue
            for alan in ("indirme_bayt", "disk_bayt"):
                sozluk = boyut.get(alan)
                if not isinstance(sozluk, dict) or ayarlar.BOYUT_ORTAK_ANAHTAR in sozluk:
                    continue
                anahtarlar = sorted(int(k) for k in sozluk if str(k).isdigit())
                if anahtarlar and anahtarlar[0] > int(en_dusuk):
                    d.ekle(f"bilesenler[{i}].boyut.{alan}",
                           f"en düşük desteklenen macOS {en_dusuk} için değer yok "
                           f"(en küçük anahtar {anahtarlar[0]}; 'hepsi' ya da ≤ {en_dusuk} gerekli)")

    # Boyut: indirme_bayt = öğe baytları toplamı (her macOS anahtarında)
    for i, b in enumerate(bilesenler):
        boyut = b.get("boyut")
        ogeler = [o for o in b.get("ogeler") or [] if isinstance(o, dict)]
        if not isinstance(boyut, dict) or not isinstance(boyut.get("indirme_bayt"), dict):
            continue
        indirme = boyut["indirme_bayt"]
        if not all(_tamsayi_mi(o.get("bayt")) for o in ogeler):
            continue
        yer = f"bilesenler[{i}].boyut.indirme_bayt"
        if ayarlar.BOYUT_ORTAK_ANAHTAR in indirme:
            if any(oge_kosullu_mu(o) for o in ogeler):
                d.ekle(yer, "koşullu öğe var; indirme_bayt macOS'a göre verilmeli ('hepsi' değil)")
            elif _tamsayi_mi(indirme[ayarlar.BOYUT_ORTAK_ANAHTAR]):
                toplam = sum(o["bayt"] for o in ogeler)
                if toplam != indirme[ayarlar.BOYUT_ORTAK_ANAHTAR]:
                    d.ekle(yer, f"öğe baytları toplamı {toplam}, manifestte "
                                f"{indirme[ayarlar.BOYUT_ORTAK_ANAHTAR]}")
            continue
        for anahtar, deger in indirme.items():
            if not str(anahtar).isdigit() or not _tamsayi_mi(deger):
                continue
            ana = int(anahtar)
            toplam = sum(o["bayt"] for o in ogeler
                         if not oge_kosullu_mu(o)
                         or any(_kosul_uyar(str(x), ana) for x in o["kosul"]["macos"]))
            if toplam != deger:
                d.ekle(f"{yer}.{anahtar}", f"macOS {anahtar} öğe baytları toplamı {toplam}, "
                                           f"manifestte {deger}")
        anahtarlar = [int(k) for k in indirme if str(k).isdigit()]
        for j, o in enumerate(ogeler):
            if oge_kosullu_mu(o) and not any(
                    _kosul_uyar(str(x), ana) for x in o["kosul"]["macos"] for ana in anahtarlar):
                d.ekle(f"bilesenler[{i}].ogeler[{j}].kosul",
                       "koşulu indirme_bayt anahtarlarının hiçbirine uymuyor")


def dogrula(veri: Any) -> List[str]:
    """Manifesti denetler; sorun yoksa boş liste, varsa Türkçe sorun listesi."""
    d = _Denetci()
    if not d.nesne(veri, "manifest"):
        return d.sorunlar
    d.alanlar(veri, "manifest", _KOK_ALANLAR, sorted(_KOK_ALANLAR))
    if "sema_surumu" in veri and (veri["sema_surumu"] != ayarlar.MANIFEST_SEMA_SURUMU
                                  or isinstance(veri["sema_surumu"], bool)):
        d.ekle("sema_surumu", f"{ayarlar.MANIFEST_SEMA_SURUMU} olmalı "
                              f"(bu yönetici yalnız bu şema sürümünü okur)")
    uygulama = veri.get("uygulama")
    if "uygulama" in veri and d.nesne(uygulama, "uygulama"):
        d.alanlar(uygulama, "uygulama", _UYGULAMA_ALANLAR, sorted(_UYGULAMA_ALANLAR))
        if "surum" in uygulama:
            d.metin(uygulama["surum"], "uygulama.surum", _SURUM)
        if "yapi" in uygulama:
            d.metin(uygulama["yapi"], "uygulama.yapi")
        if "min_macos" in uygulama:
            d.metin(uygulama["min_macos"], "uygulama.min_macos", _MACOS_SURUMU)
        if "mimari" in uygulama and uygulama["mimari"] != MIMARI:
            d.ekle("uygulama.mimari", f"'{MIMARI}' olmalı")
        if "calisma_zamani" in uygulama:
            d.oge(uygulama["calisma_zamani"], "uygulama.calisma_zamani")
    olcum = veri.get("olcum")
    if "olcum" in veri and d.nesne(olcum, "olcum"):
        d.alanlar(olcum, "olcum", _OLCUM_ALANLAR, sorted(_OLCUM_ALANLAR))
        if "tarih" in olcum:
            d.metin(olcum["tarih"], "olcum.tarih", _TARIH)
        for alan in ("makine", "macos", "yontem"):
            if alan in olcum:
                d.metin(olcum[alan], f"olcum.{alan}")
    if "gruplar" in veri:
        if not isinstance(veri["gruplar"], list):
            d.ekle("gruplar", "liste olmalı")
        else:
            for i, g in enumerate(veri["gruplar"]):
                yer = f"gruplar[{i}]"
                if not d.nesne(g, yer):
                    continue
                d.alanlar(g, yer, _GRUP_ALANLAR, _GRUP_ZORUNLU)
                if "kimlik" in g:
                    d.metin(g["kimlik"], f"{yer}.kimlik", _KIMLIK)
                if "ad" in g:
                    d.metin(g["ad"], f"{yer}.ad")
                if "tur" in g and g["tur"] != GRUP_TURU:
                    d.ekle(f"{yer}.tur", f"'{GRUP_TURU}' olmalı")
                if "varsayilan" in g:
                    d.metin(g["varsayilan"], f"{yer}.varsayilan", _KIMLIK)
                if "zorunlu" in g and not isinstance(g["zorunlu"], bool):
                    d.ekle(f"{yer}.zorunlu", "true/false olmalı")
    if "bilesenler" in veri:
        if not isinstance(veri["bilesenler"], list) or not veri["bilesenler"]:
            d.ekle("bilesenler", "boş olmayan liste olmalı")
        else:
            for i, b in enumerate(veri["bilesenler"]):
                d.bilesen(b, f"bilesenler[{i}]")
    if not d.sorunlar:
        _anlamsal(veri, d)
    return d.sorunlar


def _cift_anahtar_yasak(ciftler: List[Tuple[str, Any]]) -> Dict[str, Any]:
    sonuc: Dict[str, Any] = {}
    for anahtar, deger in ciftler:
        if anahtar in sonuc:
            raise ManifestHatasi(f"manifestte '{anahtar}' anahtarı aynı nesnede iki kez var")
        sonuc[anahtar] = deger
    return sonuc


def yukle(yol: "os.PathLike[str] | str") -> "Manifest":
    """Manifest dosyasını okur ve doğrular; sorun varsa ``ManifestHatasi``."""
    try:
        with open(os.fspath(yol), "r", encoding="utf-8") as dosya:
            veri = json.load(dosya, object_pairs_hook=_cift_anahtar_yasak)
    except FileNotFoundError as exc:
        raise ManifestHatasi(f"manifest bulunamadı: {yol}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ManifestHatasi(f"manifest okunamadı (geçersiz JSON): {exc}") from exc
    return Manifest(veri)


# ---------------------------------------------------------------------------
# Disk ön denetimi: geçici tepe ve ek alan (TASARIM D.2-8). TEK TANIM: K3'ün
# planı (kurulum.Kurulum.plan) ve Manifest.plan_toplami bu işlevleri kullanır.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DiskKalemi:
    """Bir bileşenin disk ön denetimine girdisi. Kalemler KURULUM SIRASIYLA verilir."""
    kimlik: str
    disk_bayt: int                 # kurulu ayak izi (sürümde ölçülen; bilinmiyorsa 0)
    gecici_bayt: int               # indirilen/ altına inen öğeler (gecici_mi)
    mevcut_kalici_bayt: int = 0    # kalıcı öğelerden diskte zaten duran bayt
    mevcut_gecici_bayt: int = 0    # geçici öğelerden diskte zaten duran bayt (.part dahil)

    @property
    def mevcut_bayt(self) -> int:
        return self.mevcut_kalici_bayt + self.mevcut_gecici_bayt


def _mevcut_bayt(kok: "os.PathLike[str] | str", oge: Mapping[str, Any],
                 indirilen_dizini: "os.PathLike[str] | str | None") -> int:
    try:
        return oge_mevcut_bayt(kok, oge, indirilen_dizini)
    except (KeyError, TypeError, ValueError):   # eksik alanlı öğe: diskte yok sayılır
        return 0


def disk_kalemi(kimlik: str, ogeler: Iterable[Mapping[str, Any]], disk_bayt: int, *,
                kok: "os.PathLike[str] | str | None" = None,
                indirilen_dizini: "os.PathLike[str] | str | None" = None) -> DiskKalemi:
    """Bileşenin ``DiskKalemi``'si. ``ogeler``: bu macOS'ta inecek öğeler (``oge_uygun_mu``).

    ``kok`` verilirse diskte duran baytlar (yerindeki doğru boylu dosya ya da
    yarım ``.part``) sayılır; verilmezse 0.
    """
    gecici = mevcut_kalici = mevcut_gecici = 0
    for oge in ogeler:
        var = _mevcut_bayt(kok, oge, indirilen_dizini) if kok is not None else 0
        if gecici_mi(oge):
            gecici += int(oge.get("bayt") or 0)
            mevcut_gecici += var
        else:
            mevcut_kalici += var
    return DiskKalemi(kimlik=kimlik, disk_bayt=int(disk_bayt or 0), gecici_bayt=gecici,
                      mevcut_kalici_bayt=mevcut_kalici, mevcut_gecici_bayt=mevcut_gecici)


def gecici_tepe(kalemler: Iterable[DiskKalemi]) -> int:
    """Geçici tepe: bir bileşenin geçici öğelerinin toplamının en büyüğü.

    Bileşenler sırayla kurulur ve geçici öğe onu tüketen adım bitince silinir;
    bu yüzden iki bileşenin geçici dosyaları aynı anda diskte durmaz. Bir
    bileşenin geçicileri adım adım silinse de hepsi birden sayılır (üst sınır).
    """
    return max((k.gecici_bayt for k in kalemler), default=0)


def ek_alan_tepesi(kalemler: Iterable[DiskKalemi]) -> int:
    """Kurulum boyunca gereken en büyük EK disk alanı (bayt; güvenlik payı HARİÇ).

    j. bileşen kurulurken, şimdiki duruma göre fazladan

        Σ_{i≤j} (disk_i − mevcut_kalici_i − mevcut_gecici_i) + gecici_j

    bayt dolu olur: önceki bileşenler kurulmuş ve geçicileri (önceden inmiş
    olanlar dahil) silinmiştir; j'nin geçicileri hepsi birden diskte sayılır.
    Sonraki bileşenlerin diskte duran yarım dosyaları zaten dolu alandır (ne
    eklenir ne düşülür). Sonuç bütün j'lerin en büyüğüdür (≥ 0). Yarıda kalan
    15 GB'lık indirme sürdürülürken yeniden 15 GB boş alan istenmez.
    Disk ön denetimi: ``disk.denetle(kok, ek_alan_tepesi(kalemler))``.
    """
    tepe = birikmis = 0
    for k in kalemler:
        birikmis += k.disk_bayt - k.mevcut_kalici_bayt - k.mevcut_gecici_bayt
        tepe = max(tepe, birikmis + k.gecici_bayt)
    return tepe


# ---------------------------------------------------------------------------
# Plan toplamı (TASARIM D.2-8, E.4)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PlanToplami:
    """Seçili bileşenlerin boyutları. Sayılar manifestten (sürümde ölçülen) ve diskten."""
    bilesenler: Tuple[str, ...]
    indirme_bayt: int          # manifestteki indirme toplamı (bu macOS için)
    kalan_indirme_bayt: int    # diskte (yarım .part ya da yerinde) olanlar düşülmüş
    disk_bayt: int             # manifestteki disk_bayt toplamı
    gecici_bayt: int           # geçici tepe (gecici_tepe)
    mevcut_bayt: int           # öğelerin diskte zaten duran baytları
    ihtiyac_bayt: int          # ek_alan_tepesi (≥ 0); güvenlik payı HARİÇ

    def sozluk(self) -> Dict[str, Any]:
        return {"bilesenler": list(self.bilesenler), "indirme_bayt": self.indirme_bayt,
                "kalan_indirme_bayt": self.kalan_indirme_bayt, "disk_bayt": self.disk_bayt,
                "gecici_bayt": self.gecici_bayt, "mevcut_bayt": self.mevcut_bayt,
                "ihtiyac_bayt": self.ihtiyac_bayt}


def oge_mevcut_bayt(kok: "os.PathLike[str] | str", oge: Mapping[str, Any],
                    indirilen_dizini: "os.PathLike[str] | str | None" = None) -> int:
    """Öğenin diskte zaten duran baytı: yerindeyse (boyu doğru) tamamı, değilse .part boyu.

    SHA okunmaz (hızlı sayım); doğrulamayı indirici yapar.
    """
    bayt = int(oge["bayt"])
    try:
        hedef = disk.guvenli_yol(kok, str(oge["hedef"]))
        if hedef.is_file() and hedef.stat().st_size == bayt:
            return bayt
    except (OSError, disk.GecersizYol):
        pass
    dizin = indirilen_dizini if indirilen_dizini is not None else \
        Path(os.fspath(kok)) / ayarlar.INDIRILEN_DIZINI
    try:
        boy = disk.part_yolu(dizin, str(oge["kimlik"]), str(oge["sha256"])).stat().st_size
    except OSError:
        return 0
    return min(boy, bayt)


# ---------------------------------------------------------------------------
# Manifest nesnesi
# ---------------------------------------------------------------------------
class Manifest:
    """Doğrulanmış manifest üzerinde salt okunur sorgular."""

    def __init__(self, veri: Mapping[str, Any], *, dogrulansin: bool = True):
        if dogrulansin:
            sorunlar = dogrula(veri)
            if sorunlar:
                raise ManifestHatasi("manifest geçersiz", sorunlar)
        self.veri = veri
        self._bilesenler: Dict[str, Mapping[str, Any]] = {b["kimlik"]: b for b in veri["bilesenler"]}
        self._gruplar: Dict[str, Mapping[str, Any]] = {g["kimlik"]: g for g in veri["gruplar"]}

    # -- temel ---------------------------------------------------------------
    @property
    def surum(self) -> str:
        return str(self.veri["uygulama"]["surum"])

    @property
    def yapi(self) -> str:
        return str(self.veri["uygulama"]["yapi"])

    @property
    def min_macos(self) -> str:
        return str(self.veri["uygulama"]["min_macos"])

    @property
    def olcum(self) -> Mapping[str, Any]:
        return self.veri["olcum"]

    @property
    def calisma_zamani(self) -> Mapping[str, Any]:
        return self.veri["uygulama"]["calisma_zamani"]

    @property
    def bilesenler(self) -> List[Mapping[str, Any]]:
        """Manifest sırasıyla (TASARIM C.1-3 ölçüm sırası)."""
        return list(self.veri["bilesenler"])

    @property
    def kimlikler(self) -> List[str]:
        return [b["kimlik"] for b in self.veri["bilesenler"]]

    def bilesen(self, kimlik: str) -> Mapping[str, Any]:
        try:
            return self._bilesenler[kimlik]
        except KeyError:
            raise ManifestHatasi(f"manifestte '{kimlik}' bileşeni yok") from None

    @property
    def gruplar(self) -> List[Mapping[str, Any]]:
        return list(self.veri["gruplar"])

    def grup(self, kimlik: str) -> Mapping[str, Any]:
        try:
            return self._gruplar[kimlik]
        except KeyError:
            raise ManifestHatasi(f"manifestte '{kimlik}' grubu yok") from None

    def grup_secenekleri(self, grup: str) -> List[str]:
        self.grup(grup)
        return [b["kimlik"] for b in self.veri["bilesenler"]
                if b["zorunluluk"] == SECIM and b.get("grup") == grup]

    def zorunlular(self) -> List[str]:
        return [b["kimlik"] for b in self.veri["bilesenler"] if b["zorunluluk"] == ZORUNLU]

    def istege_baglilar(self) -> List[str]:
        return [b["kimlik"] for b in self.veri["bilesenler"] if b["zorunluluk"] == ISTEGE_BAGLI]

    def ilk_url(self) -> str:
        """Çevrimdışı algılamanın HEAD atacağı adres (TASARIM D.2-9)."""
        for b in self.veri["bilesenler"]:
            for oge in b["ogeler"]:
                return str(oge["url"])
        return str(self.calisma_zamani["url"])

    # -- öğeler ve boyutlar --------------------------------------------------
    def ogeler(self, kimlik: str, macos: Optional[str] = None) -> List[Mapping[str, Any]]:
        """Bileşenin bu macOS'ta inecek öğeleri."""
        return [o for o in self.bilesen(kimlik)["ogeler"] if oge_uygun_mu(o, macos)]

    def gecici_bayt(self, kimlik: str, macos: Optional[str] = None) -> int:
        """Bileşenin ``indirilen/`` altına inen (kurulum sonunda silinen) öğelerinin toplamı."""
        return gecici_toplami(self.ogeler(kimlik, macos))

    def boyut(self, kimlik: str, macos: Optional[str] = None) -> Dict[str, int]:
        boyut = self.bilesen(kimlik)["boyut"]
        return {"indirme_bayt": bayt_sec(boyut["indirme_bayt"], macos),
                "disk_bayt": bayt_sec(boyut["disk_bayt"], macos)}

    # -- seçim ---------------------------------------------------------------
    def bagimliliklarla(self, kimlikler: Iterable[str]) -> List[str]:
        """Kimlikler + bütün bağımlılıkları; manifest sırası, bağımlılık önce."""
        istenen: set = set()
        bekleyen = list(kimlikler)
        while bekleyen:
            kimlik = bekleyen.pop()
            if kimlik in istenen:
                continue
            istenen.add(kimlik)
            bekleyen.extend(self.bilesen(kimlik).get("bagimliliklar") or [])
        sira: List[str] = []
        yerlesti: set = set()

        def yerlestir(kimlik: str) -> None:
            if kimlik in yerlesti:
                return
            for bag in self.bilesen(kimlik).get("bagimliliklar") or []:
                yerlestir(bag)
            yerlesti.add(kimlik)
            sira.append(kimlik)

        for kimlik in self.kimlikler:
            if kimlik in istenen:
                yerlestir(kimlik)
        return sira

    def kurulacaklar(self, grup_secimleri: Optional[Mapping[str, str]] = None,
                     istege_bagli: Iterable[str] = ()) -> List[str]:
        """Zorunlular + her grubun seçimi (yoksa varsayılanı) + isteğe bağlılar + bağımlılıklar.

        Geçersiz seçim (gruba ait olmayan, isteğe bağlı olmayan, zorunlu grupta
        seçimsiz) ``ManifestHatasi`` verir. Kurulu olanları çıkarmak çağıranın işidir.
        """
        secimler = dict(grup_secimleri or {})
        for grup in secimler:
            self.grup(grup)
        secilen = list(self.zorunlular())
        for grup in self.gruplar:
            gk = grup["kimlik"]
            secim = secimler.get(gk)
            if secim is None and grup["zorunlu"]:
                secim = grup["varsayilan"]
            if secim is None:
                continue
            if secim not in self.grup_secenekleri(gk):
                raise ManifestHatasi(f"'{secim}' '{gk}' grubunun seçeneği değil")
            secilen.append(secim)
        for kimlik in istege_bagli:
            if self.bilesen(kimlik)["zorunluluk"] != ISTEGE_BAGLI:
                raise ManifestHatasi(f"'{kimlik}' isteğe bağlı bir bileşen değil")
            secilen.append(kimlik)
        return self.bagimliliklarla(secilen)

    # -- plan ----------------------------------------------------------------
    def plan_toplami(self, kimlikler: Iterable[str], macos: Optional[str] = None, *,
                     kok: "os.PathLike[str] | str | None" = None,
                     indirilen_dizini: "os.PathLike[str] | str | None" = None) -> PlanToplami:
        """Seçilen (ve eksik) bileşenlerin toplamları; ``kimlikler`` KURULUM SIRASIYLA.

        ``kok`` verilirse yarım ``.part`` dosyaları ve yerindeki öğeler
        ``mevcut_bayt``'a sayılır. Geçici tepe ve ek alan, K3'ün planıyla aynı
        tanımdır (``gecici_tepe``, ``ek_alan_tepesi``). Disk ön denetimi:
        ``disk.denetle(kok, plan.ihtiyac_bayt)`` (pay ``ayarlar.DISK_PAYI_BAYT``).
        """
        liste = list(dict.fromkeys(kimlikler))
        kalemler: List[DiskKalemi] = []
        indirme = kalan = 0
        for kimlik in liste:
            boyut = self.boyut(kimlik, macos)
            ogeler = self.ogeler(kimlik, macos)
            kalem = disk_kalemi(kimlik, ogeler, boyut["disk_bayt"], kok=kok,
                                indirilen_dizini=indirilen_dizini)
            kalemler.append(kalem)
            indirme += boyut["indirme_bayt"]
            kalan += sum(int(o["bayt"]) for o in ogeler) - kalem.mevcut_bayt
        return PlanToplami(bilesenler=tuple(liste), indirme_bayt=indirme,
                           kalan_indirme_bayt=kalan,
                           disk_bayt=sum(k.disk_bayt for k in kalemler),
                           gecici_bayt=gecici_tepe(kalemler),
                           mevcut_bayt=sum(k.mevcut_bayt for k in kalemler),
                           ihtiyac_bayt=ek_alan_tepesi(kalemler))
