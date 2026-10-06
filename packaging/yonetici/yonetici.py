"""Kitapçevir yöneticisi: sihirbaz sunucusu, kurulum işinin sahibi, süreç yöneticisi.

Dayanak: TASARIM B.3–B.6 (süreç düzeni, kabuk sözleşmesi, API, kapanış),
C (manifest), E (ekranlar). Yalnız stdlib: DMG'deki taşınabilir Python'la
çalışır (TASARIM A.2)::

    <kök>/calisma_zamani/python-<sürüm>/bin/python3 -I -X utf8 \\
        <kök>/app/guncel/packaging/yonetici/yonetici.py --kok <kök> --port 0

Standart çıktının İLK satırı kabuğa tek satır JSON'dur (B.4-6)::

    {"url": "http://127.0.0.1:<P>/?anahtar=<tok>", "kapanis_suresi_sn": 25}

Kilit başka örnekteyse çıkış kodu ``ayarlar.KILIT_DOLU_CIKIS_KODU``; çalışan
örneğin adresi aynı biçimde, ``"zaten_calisiyor": true`` ile basılır (B.6).

Katmanlar (kulvarlar arası sözleşme):

- Kurulum mantığı K3'ün ``kurulum.Kurulum``'undadır: bileşen durumu, plan,
  lisans onayı, iş, iptal, kaldırma. Bu modül onu çağırır; plan, boyut ya da
  TeX hükmü burada yeniden hesaplanmaz.
- İndirme K2'nin ``indirici.Indirici``'sidir. ``IndiriciUyarlayici`` onu
  K3'ün ``indirici(ogeler, istek)`` sözleşmesine bağlar (iş başına tek
  ``Indirici``: genel hız bütün işin gerçek baytından ölçülür). Sınıf tektir
  ve ``kurulum.py``'de durur; bu modüldeki ad onun kendisidir.
- ``IsGorunumu``: kurulum olaylarından ekran görünümü. Her sayı olaylardan
  ya da diskten gelir (gerçek bayt, gerçek süre); tahmin "tahmini" yazılır.
- ``Yonetici``: ``sunucu.Sunucu``'nun çağırdığı API yöntemleri (B.5),
  Streamlit ve kapanış (``surecler``).
"""

from __future__ import annotations

import sys
from pathlib import Path

if __name__ == "__main__" and not __package__:
    # Düz betik olarak çalıştırıldı (``python3 -I …/yonetici.py``): ``-I`` betik
    # klasörünü sys.path'e koymaz. ``packaging/`` eklenir ve modül paket olarak
    # yeniden yüklenir (öteki modüller göreli içe aktarıyor). Betik klasörü
    # sys.path'te kalırsa bu dosya ``yonetici`` paketini gölgelerdi.
    _BETIK_DIZINI = Path(__file__).resolve().parent
    sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != _BETIK_DIZINI]
    sys.path.insert(0, str(_BETIK_DIZINI.parent))
    import importlib as _importlib

    sys.exit(_importlib.import_module("yonetici.yonetici").main())

import argparse  # noqa: E402 (betik önyüklemesinden sonra)
import collections  # noqa: E402
import csv  # noqa: E402
import datetime as _dt  # noqa: E402
import errno  # noqa: E402
import fcntl  # noqa: E402
import hashlib  # noqa: E402
import importlib  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import logging.handlers  # noqa: E402
import os  # noqa: E402
import platform  # noqa: E402
import plistlib  # noqa: E402
import queue  # noqa: E402
import re  # noqa: E402
import secrets  # noqa: E402
import shutil  # noqa: E402
import signal  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import tomllib  # noqa: E402
import unicodedata  # noqa: E402
import urllib.parse  # noqa: E402
from http import HTTPStatus  # noqa: E402
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple  # noqa: E402

from . import ayarlar, disk, donanim, durum, indirici, kurulum, lisans, manifest, sunucu, surecler  # noqa: E402,E501
from .adimlar import tex as tex_adimi  # noqa: E402
from .adimlar.ortak import IptalEdildi  # noqa: E402,F401 (testler yonetici.IptalEdildi adını kullanır)
from .sunucu import ApiHatasi, bicim_boyut, bicim_boyut_hassas, bicim_hiz, bicim_sure  # noqa: E402

logger = logging.getLogger("kitapcevir.yonetici")

# ---------------------------------------------------------------------------
# Sabitler (yalnız bu modülde; paylaşılanlar ayarlar.py'de)
# ---------------------------------------------------------------------------
UYGULAMA_KLASOR_ADI = "Kitapçevir"                  # ~/Library/Logs/<bu> (TASARIM B.6)
GUNLUK_ORTAM_DEGISKENI = "KITAPCEVIR_GUNLUK_DIZINI"  # kabuk (K1) verir
MANIFEST_GORELI = "packaging/manifest.json"          # uygulama koduna göre (TASARIM C.1)
TEMA_DOSYASI = ".streamlit/config.toml"              # renklerin tek kaynağı (TASARIM A.2)
TEX_GRUBU = kurulum.TEX_GRUBU
ALGILA_TEX_KURULU = kurulum.ON_KOSUL_TEX_KURULU
ZORUNLU, ISTEGE_BAGLI = kurulum.ZORUNLU, kurulum.ISTEGE_BAGLI
PLAN_SAKLAMA_EN_COK = 32
# Hoş geldin ekranındaki "bileşenler yayıncılarının sunucularından iner"
# satırı ve istek sınırı iletisi manifestteki gerçek adreslerden üretilir.
KAYNAK_ADLARI = {
    "github.com": "GitHub", "objects.githubusercontent.com": "GitHub",
    "release-assets.githubusercontent.com": "GitHub",
    "files.pythonhosted.org": "PyPI", "pypi.org": "PyPI",
    "huggingface.co": "Hugging Face", "models.datalab.to": "datalab.to",
    "mirror.ctan.org": "CTAN",
}
LISANS_METNI_EN_COK_BAYT = 2_000_000
EBEVEYN_YOKLAMA_SN = 2
IS_BEKLEME_KAPANISTA_SN = 5
# Başlıksız kipte olay kuyruğunu bekleme aralığı (kesme sinyali bu aralıkla işlenir).
BASLIKSIZ_YOKLAMA_SN = 0.5
# /api/durum iki kez (özet + algılanan liste) aynı TeX algılamasını istemesin.
TEX_ALGILAMA_ONBELLEK_SN = 2.0
TEMA_ESLEMESI = (("primaryColor", "--kc-vurgu"), ("backgroundColor", "--kc-zemin"),
                 ("secondaryBackgroundColor", "--kc-zemin-2"), ("textColor", "--kc-metin"))
_RENK = re.compile(r"^#[0-9a-fA-F]{3,8}$")

# "Kitapçevir'i kaldır…" (TASARIM G.5)
# Yalnız bu adı taşıyan kök silinir: yanlış --kok (ör. ev dizini) verilse de
# kaldırma reddedilir.
KALDIRMA_KOK_ADI = UYGULAMA_KLASOR_ADI
# Çalışan yöneticinin kodu, Python'u ve kilidi: kaldırmada çıkışa bırakılır.
KALDIRMADA_SONA_KALANLAR = ("app", "calisma_zamani", "durum")
# Günlük klasöründe yalnız bizim dosyalarımız silinir (TASARIM B.6 adları).
GUNLUK_DOSYA_DESENLERI = ("yonetici.log*", "streamlit.log*", "kabuk.log*", "kurulum-*.log")
# Paket kimliğine bağlı macOS klasörleri ve dosyaları (ev dizinine göre; G.5).
# Kimlik kabuğun verdiği paketin Info.plist'inden okunur (tek kaynak: Info.plist).
# Tercih dosyası ve kayıtlı pencere durumu uçtan uca sınamada oluşmadı ama
# AppKit/WebKit gerçek kullanımda yazabilir (K9 D9); varsa silinir.
PAKET_YOLU_ORTAM_DEGISKENI = "KITAPCEVIR_PAKET_YOLU"
PAKET_KLASOR_SABLONLARI = ("Library/WebKit/{kimlik}", "Library/Caches/{kimlik}",
                           "Library/HTTPStorages/{kimlik}",
                           "Library/Preferences/{kimlik}.plist",
                           "Library/Saved Application State/{kimlik}.savedState")
# Paket kimliği (CFBundleIdentifier) ters alan adı biçiminde olmalı: en az iki
# parça, her parça harf/rakamla başlar ve yalnız harf/rakam/tire içerir. "." ya
# da ".." gibi bir değer kabul edilseydi yukarıdaki şablonlar ~/Library/WebKit'in
# kendisini ya da ~/Library'yi gösterirdi (K9 D4; Info.plist bozulursa).
_PAKET_KIMLIGI = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*(?:\.[A-Za-z0-9][A-Za-z0-9-]*)+")
TEX_TAM = "tex-tam"                                  # sistem geneline kurulur, kaldırılmaz
# Kurulum ekranında inen dosyanın adı: hedef yolunun en çok bu kadar son parçası
# ("layout/2025_09_23/model.safetensors"; TASARIM E.5). indirilen/ altındakiler
# (tekerlek, arşiv) geçici olduğu için yalnız dosya adıyla gösterilir.
OGE_ADI_PARCA_EN_COK = 3

# Bileşen ve adım durum metinleri (ekranda)
DURUM_METINLERI = {
    durum.DURUM_KURULU: "kurulu",
    durum.DURUM_EKSIK: "kurulu değil",
    durum.DURUM_BOZUK: "bozuk",
    durum.DURUM_KURULUYOR: "kuruluyor",
    durum.DURUM_GUNCELLENECEK: "güncellenecek",
}
ADIM_BEKLIYOR, ADIM_SURUYOR, ADIM_BITTI = "bekliyor", "suruyor", "bitti"
ADIM_HATA, ADIM_IPTAL = "hata", "iptal"
ADIM_METINLERI = {ADIM_BEKLIYOR: "bekliyor", ADIM_SURUYOR: "sürüyor", ADIM_BITTI: "tamam",
                  ADIM_HATA: "hata", ADIM_IPTAL: "durduruldu"}
MSG_ADIM_ATLANDI = "önceden yapılmış"
SONUC_TAMAM, SONUC_IPTAL, SONUC_HATA = (kurulum.SONUC_TAMAM, kurulum.SONUC_IPTAL,
                                        kurulum.SONUC_HATA)
# Adım türünün ekrandaki sınıfı (sayfa "kuruluyor…" / "sürüyor…" önekini buna göre seçer)
ADIM_INDIR, ADIM_DOGRULAMA = "indir", "dogrulama"

# Olay türleri (TASARIM B.5; K3 ve K2 üretir, ``is_basladi`` yöneticinindir)
OLAY_IS_BASLADI = "is_basladi"
OLAY_ADIM_BASLADI = "adim_basladi"
OLAY_ILERLEME = "ilerleme"
OLAY_OGE_DURUMU = "oge_durumu"
OLAY_SAYAC = "sayac"
OLAY_BEKLEME = "bekleme"
OLAY_ADIM_BITTI = "adim_bitti"
OLAY_BILESEN_BITTI = "bilesen_bitti"
OLAY_HATA = "hata"
OLAY_BITTI = "bitti"
KURULUM_OLAYLARI = (OLAY_ADIM_BASLADI, OLAY_ILERLEME, OLAY_OGE_DURUMU, OLAY_SAYAC,
                    OLAY_BEKLEME, OLAY_ADIM_BITTI, OLAY_BILESEN_BITTI, OLAY_HATA, OLAY_BITTI)
# Sayfaya gönderilmeyen (yalnız görünüme işlenen) olaylar
SESSIZ_OLAYLAR = (OLAY_OGE_DURUMU,)

# Arayüz metinleri (yönetici tarafında üretilenler)
MSG_MANIFEST_YOK = "Bileşen listesi (manifest) okunamadı: {neden}"
MSG_TEX_SINANMADI = "Şablon sınaması henüz yapılmadı."
MSG_HAZIR_DEGIL = "Zorunlu bileşen hazır değil: {ad} ({durum})."
MSG_TEX_ETKIN_DEGIL = "Etkin bir TeX dağıtımı yok."
MSG_IS_SURUYOR = kurulum.MSG_IS_SURUYOR
MSG_IS_YOK = "Süren bir kurulum yok."
MSG_KAPANIYOR = "Yönetici kapanıyor."
MSG_UYGULAMA_ISI = ("Kitapçevir'de süren bir iş var ({n}). Önce işi bitir ya da iptal et; "
                    "bileşenler iş sürerken değiştirilemez.")
MSG_GUNCELLEME_YOK = "Bu sürüm güncel ({surum})."
MSG_GUNCELLEME_VAR = "Yeni sürüm var: {surum}."
MSG_GUNCELLEME_DENETLENEMEDI = "Güncelleme denetlenemedi."
MSG_SAYAC_OLCULEN = "{i} {birim} indi (sürümde ölçülen: {n})"
MSG_SAYAC_YALIN = "{i} {birim} indi"
MSG_TAHMINI = "~{sure} (tahmini)"
MSG_OLCUM_NOTU = "Boyutlar sürüm {surum} çıkarken ölçüldü ({tarih}, {makine}, macOS {macos})."
MSG_BEKLEME_SINIR = "{kaynak} istek sınırı"
MSG_BEKLEME_SINIR_GENEL = "Sunucu istek sınırı"
MSG_BEKLEME_DENEME = "Bağlantı sorunu (deneme {deneme}/{en_cok})"
MSG_LISANS_ONAY_GEREKMEZ = "{ad} lisans onayı gerektirmiyor."
MSG_LISANS_GERI_ALINAMAZ = "Bu bileşen kurulurken lisans onayı geri alınamaz."
MSG_LISANS_YEREL_YOK = "Yerel kopya yok."
MSG_LISANS_YEREL_BULUNAMADI = "Yerel kopya bulunamadı."
MSG_LISANS_YEREL_UYUSMUYOR = "Yerel kopya manifestteki özetle eşleşmiyor."
MSG_TEX_ALGILANMADI = "Bu TeX algılanmadı."
MSG_BILINMEYEN_HEDEF = "Bilinmeyen hedef."
MSG_GUNLUK_YOK = "Kurulum günlüğü yok."
MSG_ADRES_YOK = "Lisansın bağlantısı yok."
MSG_SECIM_GECERSIZ = "Seçim listesi geçersiz."
MSG_KALDIRILDI = "Kitapçevir bu Mac'ten kaldırıldı; uygulamayı kapat."
MSG_KALDIRMA_KOK = ("Kurulum kökü tanınmadı ({kok}); güvenlik için kaldırma yapılmıyor. "
                    "Klasörleri elle silebilirsin.")
MSG_KALDIRMA_ONAY = "Kaldırma için açık onay gerekiyor."

# KurulumHatasi kodu → HTTP durumu (PlanHatasi hep 400)
_KURULUM_HTTP = {
    kurulum.KOD_IS_SURUYOR: HTTPStatus.CONFLICT,
    kurulum.KOD_PLAN_YOK: HTTPStatus.CONFLICT,
    kurulum.KOD_PLAN_ESKIDI: HTTPStatus.CONFLICT,
    kurulum.KOD_ONAY_EKSIK: HTTPStatus.CONFLICT,
    kurulum.KOD_DISK: HTTPStatus.CONFLICT,
    kurulum.KOD_KALDIRILAMAZ: HTTPStatus.CONFLICT,
}


def _tamsayi(deger: Any) -> Optional[int]:
    if isinstance(deger, bool) or not isinstance(deger, int) or deger < 0:
        return None
    return deger


def _ev_kisalt(yol: Path) -> str:
    """Ekranda gösterilen yol: ev dizini "~" ile kısaltılır."""
    metin = str(yol)
    ev = str(Path.home())
    if ev and ev != "/" and (metin == ev or metin.startswith(ev + os.sep)):
        return "~" + metin[len(ev):]
    return metin


def macos_surumu() -> str:
    return platform.mac_ver()[0] or ""


def varsayilan_gunluk_dizini() -> Path:
    """``KITAPCEVIR_GUNLUK_DIZINI`` (kabuk verir) ya da ``~/Library/Logs/Kitapçevir``."""
    ortam = os.environ.get(GUNLUK_ORTAM_DEGISKENI)
    if ortam:
        return Path(ortam).expanduser()
    return Path.home() / "Library" / "Logs" / UYGULAMA_KLASOR_ADI


def paket_kimligi(paket_yolu: Optional[Path]) -> Optional[str]:
    """Kabuğun paketindeki ``Contents/Info.plist`` → ``CFBundleIdentifier``; yoksa None."""
    if paket_yolu is None:
        return None
    try:
        with open(Path(paket_yolu) / "Contents" / "Info.plist", "rb") as dosya:
            kimlik = plistlib.load(dosya).get("CFBundleIdentifier")
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    return kimlik if isinstance(kimlik, str) and _PAKET_KIMLIGI.fullmatch(kimlik) else None


def _yolu_sil(yol: Path) -> None:
    """Klasörü ağacıyla, dosyayı ya da bağı siler (bağın hedefine gidilmez)."""
    if yol.is_symlink() or not yol.is_dir():
        yol.unlink(missing_ok=True)
    else:
        shutil.rmtree(yol)


def _kurulum_hatasi(exc: kurulum.KurulumHatasi) -> ApiHatasi:
    if isinstance(exc, kurulum.PlanHatasi):
        return ApiHatasi(HTTPStatus.BAD_REQUEST, exc.kod, exc.ileti)
    return ApiHatasi(_KURULUM_HTTP.get(exc.kod, HTTPStatus.CONFLICT), exc.kod, exc.ileti)


def _plan_adi(b: Mapping[str, Any]) -> str:
    """Özetteki ad: TeX seçeneği adında "TeX" geçmiyorsa önüne "TeX: " eklenir
    ("Kurulu olanı kullan" tek başına ne olduğunu söylemez)."""
    ad = str(b.get("ad") or b.get("kimlik") or "")
    return f"TeX: {ad}" if b.get("grup") == TEX_GRUBU and "TeX" not in ad else ad


def oge_gorunen_adi(hedef: str, kimlik: str = "") -> str:
    """Öğenin kurulum ekranındaki adı, manifestteki hedef yolundan (E.5)."""
    parcalar = [p for p in str(hedef or "").split("/") if p and p != "."]
    if not parcalar:
        return kimlik
    if parcalar[0] == ayarlar.INDIRILEN_DIZINI:
        return parcalar[-1]
    return "/".join(parcalar[-OGE_ADI_PARCA_EN_COK:])


def _sunucu_adi(url: str) -> str:
    ad = urllib.parse.urlsplit(str(url)).hostname or ""
    return KAYNAK_ADLARI.get(ad, ad)


# ---------------------------------------------------------------------------
# K3 kurulumunun yönetici kipi
# ---------------------------------------------------------------------------
class YoneticiKurulumu(kurulum.Kurulum):
    """K3 ``Kurulum``'u; yalnız kurulu TeX algılaması ayrı yapılır.

    ``Kurulum.tex_algilanan`` şablonu her yeni xelatex için gerçekten derler
    (bu Mac'te ~3 sn). ``GET /api/durum`` bunu beklemesin diye durum
    sorgusunda sınamasız okunur: önceki sınamanın sonucu
    ``durum/tex_algilama.json`` önbelleğinden gelir, yoksa ``sinama`` None
    olur ve sayfa ``POST /api/tex_sina`` ile sınamayı ister (``tex_sina``).
    Hüküm yine K3'ündür (``_tex_kurulu_hukmu``); sınanmamış TeX seçilemez.
    """

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._tex_kilidi = threading.Lock()
        self._tex_onbellek: Optional[Tuple[float, List[Dict[str, Any]]]] = None

    def tex_algilanan(self) -> List[Dict[str, Any]]:
        with self._tex_kilidi:
            simdi = time.monotonic()
            if self._tex_onbellek and simdi - self._tex_onbellek[0] < TEX_ALGILAMA_ONBELLEK_SN:
                return [dict(a) for a in self._tex_onbellek[1]]
            liste = tex_adimi.tex_algila(self.duzen, self.uygulama, sina=False)
            self._tex_onbellek = (simdi, liste)
            return [dict(a) for a in liste]

    def tex_sina(self) -> List[Dict[str, Any]]:
        """Algılanan her xelatex'i şablonla sınar (önbellekte olan yeniden derlenmez)."""
        with self._tex_kilidi:
            liste = tex_adimi.tex_algila(self.duzen, self.uygulama, sina=True)
            self._tex_onbellek = (time.monotonic(), liste)
            return [dict(a) for a in liste]


# ---------------------------------------------------------------------------
# K2 indiricisi → K3 sözleşmesi: TEK uyarlayıcı K3'ün ``kurulum.py``'sinde
# durur (``indirici`` verilmeyen ``Kurulum`` da onu kullanır). Buradaki ad
# aynı sınıftır; yönetici ve testleri bu adla çağırır.
# ---------------------------------------------------------------------------
IndiriciUyarlayici = kurulum.IndiriciUyarlayici


# ---------------------------------------------------------------------------
# Kurulum işinin ekran görünümü
# ---------------------------------------------------------------------------
class IsGorunumu:
    """Kurulum olaylarından ekran görünümü (E.5). Bütün sayılar gerçektir.

    - Genel ilerleme = öğe başına diskteki gerçek bayt. Başlangıçta öğenin
      diskte duran kısmı (tam dosya ya da ``.part``; ``manifest.oge_mevcut_bayt``),
      sonra indiricinin ``ilerleme`` olayları. Bileşenin doğrulaması bitince
      bütün öğeleri tamam sayılır (önceki işte inip kurulumda tüketilen
      tekerlekler dahil). Payda = planın öğe baytları toplamı.
    - Hız = indiricinin son ``ayarlar.HIZ_PENCERESI_SN`` içindeki ölçümü;
      bu süreden eski ya da yoksa "—". Kalan süre yalnız hız varken ve
      "tahmini" diye verilir.
    """

    def __init__(self, plan_kimligi: str, bilesenler: Sequence[Tuple[str, str, bool]],
                 ogeler: Mapping[Tuple[str, str], Mapping[str, Any]], *,
                 kalici: Optional[Mapping[str, Sequence[str]]] = None,
                 saat: Callable[[], float] = time.time,
                 kaynak_adi: Callable[[str, str], str] = lambda bilesen, oge: "",
                 oge_adi: Optional[Callable[[str, str], str]] = None):
        self._saat = saat
        self._oge_adi = oge_adi or (lambda bilesen, oge: oge)
        # Bileşenin başında "indir" adımında inen öğeler (K3 ``kalici_ogeler``)
        self._kalici = {str(k): [str(o) for o in v] for k, v in (kalici or {}).items()}
        self._kaynak_adi = kaynak_adi
        self.plan_kimligi = str(plan_kimligi)
        self.baslangic = saat()
        self.bitis: Optional[float] = None
        self.sonuc: Optional[str] = None
        self.hata: Optional[Dict[str, Any]] = None
        self.gunluk: str = ""
        self._beklemeler: "collections.OrderedDict[Tuple[str, str], Dict[str, Any]]" = \
            collections.OrderedDict()
        self._hiz: Optional[float] = None
        self._hiz_zamani = 0.0
        self.bilesenler: "collections.OrderedDict[str, Dict[str, Any]]" = collections.OrderedDict()
        for kimlik, ad, onar in bilesenler:
            self._bilesen(str(kimlik), str(ad or kimlik), bool(onar))
        self._ogeler: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for (bilesen, oge), kayit in ogeler.items():
            toplam = _tamsayi(kayit.get("toplam")) or 0
            self._ogeler[(str(bilesen), str(oge))] = {
                "bayt": min(_tamsayi(kayit.get("bayt")) or 0, toplam), "toplam": toplam}
        self.toplam_bayt = sum(k["toplam"] for k in self._ogeler.values())

    # -- iç kayıtlar -----------------------------------------------------------
    def _bilesen(self, kimlik: str, ad: Optional[str] = None, onar: bool = False) -> Dict[str, Any]:
        b = self.bilesenler.get(kimlik)
        if b is None:
            b = {"kimlik": kimlik, "ad": ad or kimlik, "onar": onar, "durum": ADIM_BEKLIYOR,
                 "adimlar": collections.OrderedDict(), "son_adim": None}
            self.bilesenler[kimlik] = b
        return b

    @staticmethod
    def _son_adim(b: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        anahtar = b.get("son_adim")
        return b["adimlar"].get(anahtar) if anahtar is not None else None

    def _bileseni_tamamla(self, kimlik: str) -> None:
        for (bilesen, _), kayit in self._ogeler.items():
            if bilesen == kimlik:
                kayit["bayt"] = kayit["toplam"]

    def _bekleme_metni(self, veri: Mapping[str, Any]) -> str:
        neden = str(veri.get("neden") or "")
        if neden == indirici.NEDEN_HIZ_SINIRI:
            kaynak = self._kaynak_adi(str(veri.get("bilesen") or ""), str(veri.get("oge") or ""))
            return MSG_BEKLEME_SINIR.format(kaynak=kaynak) if kaynak else MSG_BEKLEME_SINIR_GENEL
        if neden == indirici.NEDEN_YENIDEN_DENEME:
            return MSG_BEKLEME_DENEME.format(deneme=veri.get("deneme", "?"),
                                             en_cok=ayarlar.YENIDEN_DENEME)
        return neden   # K3'ün kendi Türkçe iletisi (ör. MacTeX kurulum programı)

    @property
    def calisiyor(self) -> bool:
        return self.sonuc is None

    # -- olay uygulama -------------------------------------------------------
    def uygula(self, tur: str, veri: Mapping[str, Any]) -> Dict[str, Any]:
        """Olayı duruma işler; etkilenen görünümü döndürür (olayla birlikte yayılır)."""
        simdi = self._saat()
        kimlik = veri.get("bilesen")
        b = self._bilesen(str(kimlik)) if kimlik and kimlik in self.bilesenler else None
        if tur == OLAY_ADIM_BASLADI and b is not None:
            self._beklemeler.clear()
            for onceki in self.bilesenler.values():
                if onceki is b:
                    break
                if onceki["durum"] == ADIM_SURUYOR:
                    onceki["durum"] = ADIM_BITTI
            anahtar = str(veri.get("sira", veri.get("adim", len(b["adimlar"]))))
            tur_adi = str(veri.get("adim") or "")
            baslangic = veri.get("baslangic")
            ogeler = list(self._kalici.get(b["kimlik"], [])) if tur_adi == ADIM_INDIR else []
            b["adimlar"][anahtar] = {
                "anahtar": anahtar, "adim": tur_adi, "ad": str(veri.get("ad") or tur_adi),
                "durum": ADIM_SURUYOR, "atlandi": False,
                "baslangic": float(baslangic) if isinstance(baslangic, (int, float)) else simdi,
                "bitis": None, "ogeler": ogeler, "son_oge": None, "sayac": None,
            }
            b["son_adim"] = anahtar
            b["durum"] = ADIM_SURUYOR
        elif tur == OLAY_ILERLEME and b is not None:
            oge = str(veri.get("oge") or "")
            kayit = self._ogeler.setdefault((b["kimlik"], oge), {"bayt": 0, "toplam": 0})
            bayt = _tamsayi(veri.get("bayt"))
            toplam = _tamsayi(veri.get("toplam_bayt"))
            if toplam is not None and toplam != kayit["toplam"]:
                # Olayın taşıdığı toplam (öğenin gerçek boyu) plandakinden farklıysa
                self.toplam_bayt += toplam - kayit["toplam"]
                kayit["toplam"] = toplam
            if bayt is not None:
                kayit["bayt"] = min(bayt, kayit["toplam"]) if kayit["toplam"] else bayt
            hiz = veri.get("hiz_bayt_sn")
            self._hiz = float(hiz) if isinstance(hiz, (int, float)) and hiz > 0 else None
            self._hiz_zamani = simdi
            self._beklemeler.pop((b["kimlik"], oge), None)
            adim = self._son_adim(b)
            if adim is not None:
                if oge not in adim["ogeler"]:
                    adim["ogeler"].append(oge)
                adim["son_oge"] = oge
        elif tur == OLAY_OGE_DURUMU and b is not None:
            kayit = self._ogeler.get((b["kimlik"], str(veri.get("oge") or "")))
            if kayit is not None and veri.get("durum") in (indirici.DOGRULANDI,
                                                           indirici.KLONLANDI):
                kayit["bayt"] = kayit["toplam"]
        elif tur == OLAY_SAYAC and b is not None:
            adim = self._son_adim(b)
            if adim is not None:
                adim["sayac"] = {"i": _tamsayi(veri.get("i")), "n": _tamsayi(veri.get("n")),
                                 "birim": str(veri.get("birim") or "")}
        elif tur == OLAY_BEKLEME:
            kalan = veri.get("kalan_sn")
            anahtar = (str(kimlik or ""), str(veri.get("oge") or ""))
            self._beklemeler.pop(anahtar, None)
            self._beklemeler[anahtar] = {
                "neden": self._bekleme_metni(veri),
                "kalan_sn": kalan if isinstance(kalan, (int, float)) else None,
                "zaman": simdi, "bilesen": kimlik}
        elif tur == OLAY_ADIM_BITTI and b is not None:
            self._beklemeler.clear()
            adim = self._son_adim(b)
            if adim is not None:
                adim["durum"] = ADIM_BITTI
                adim["bitis"] = simdi
                adim["atlandi"] = bool(veri.get("atlandi"))
                if adim["adim"] == ADIM_INDIR:
                    for oge in adim["ogeler"]:
                        kayit = self._ogeler.get((b["kimlik"], oge))
                        if kayit is not None:
                            kayit["bayt"] = kayit["toplam"]
            if str(veri.get("adim")) == ADIM_DOGRULAMA:
                b["durum"] = ADIM_BITTI
                self._bileseni_tamamla(b["kimlik"])
        elif tur == OLAY_BILESEN_BITTI and b is not None:
            # K3: bileşen kaydı yazıldı (doğrulama dahil her adım bitti)
            self._beklemeler.clear()
            b["durum"] = ADIM_BITTI
            self._bileseni_tamamla(b["kimlik"])
        elif tur == OLAY_HATA:
            self.hata = {"kod": str(veri.get("kod") or ""), "ileti": str(veri.get("ileti") or ""),
                         "ayrinti_gunluk": str(veri.get("ayrinti_gunluk") or self.gunluk or "")}
            for bil in self.bilesenler.values():
                for adim in bil["adimlar"].values():
                    if adim["durum"] == ADIM_SURUYOR:
                        adim["durum"] = ADIM_HATA
                        adim["bitis"] = simdi
                if bil["durum"] == ADIM_SURUYOR:
                    bil["durum"] = ADIM_HATA
        elif tur == OLAY_BITTI:
            self.sonuc = str(veri.get("sonuc") or SONUC_HATA)
            self.bitis = simdi
            self._beklemeler.clear()
            for bil in self.bilesenler.values():
                for adim in bil["adimlar"].values():
                    if adim["durum"] == ADIM_SURUYOR:
                        adim["durum"] = ADIM_BITTI if self.sonuc == SONUC_TAMAM else ADIM_IPTAL
                        adim["bitis"] = simdi
                if self.sonuc == SONUC_TAMAM:
                    bil["durum"] = ADIM_BITTI
                    self._bileseni_tamamla(bil["kimlik"])
                elif bil["durum"] == ADIM_SURUYOR:
                    bil["durum"] = ADIM_IPTAL if self.sonuc == SONUC_IPTAL else ADIM_HATA
        gorunum: Dict[str, Any] = {"genel": self.genel_gorunumu(),
                                   "bekleme": self._bekleme_gorunumu()}
        if b is not None:
            gorunum["bilesen"] = self.bilesen_gorunumu(b["kimlik"])
        if tur in (OLAY_IS_BASLADI, OLAY_HATA, OLAY_BITTI, OLAY_ADIM_BASLADI):
            gorunum["bilesenler"] = [self.bilesen_gorunumu(k) for k in self.bilesenler]
        if tur in (OLAY_IS_BASLADI, OLAY_HATA, OLAY_BITTI):
            gorunum["is"] = self._is_alanlari()
        return gorunum

    # -- görünümler ----------------------------------------------------------
    def adim_gorunumu(self, bilesen_kimligi: str, adim: Mapping[str, Any]) -> Dict[str, Any]:
        # "indir" adımının öğeleri baştan bellidir (diskte duranlar dahil);
        # öteki adımlarınki ilerleme olayı geldikçe eklenir.
        bayt, toplam = 0, 0
        for oge in adim["ogeler"]:
            kayit = self._ogeler.get((bilesen_kimligi, oge))
            if kayit is not None:
                bayt += kayit["bayt"]
                toplam += kayit["toplam"]
        sure = (adim["bitis"] - adim["baslangic"]) if adim["bitis"] else None
        durum_metni = MSG_ADIM_ATLANDI if adim["atlandi"] else \
            ADIM_METINLERI.get(adim["durum"], adim["durum"])
        g: Dict[str, Any] = {
            "adim": adim["adim"], "ad": adim["ad"], "durum": adim["durum"],
            "tur": ("indirme" if adim["adim"] == ADIM_INDIR else
                    ADIM_DOGRULAMA if adim["adim"] == ADIM_DOGRULAMA else "kurulum"),
            "durum_metni": durum_metni, "baslangic": adim["baslangic"], "bitis": adim["bitis"],
            "sure_sn": sure, "sure_metni": bicim_sure(sure) if sure is not None else "",
            "bayt": bayt if adim["ogeler"] else None,
            "toplam_bayt": toplam if adim["ogeler"] else None,
            "bayt_metni": "", "oran": None, "oge": None, "sayac_metni": "",
        }
        if adim["ogeler"] and toplam:
            g["bayt_metni"] = f"{bicim_boyut(bayt)} / {bicim_boyut(toplam)}"
            g["oran"] = min(1.0, bayt / toplam)
        elif adim["ogeler"]:
            g["bayt_metni"] = bicim_boyut(bayt)
        son = adim.get("son_oge")
        kayit = self._ogeler.get((bilesen_kimligi, son)) if son else None
        if kayit is not None and adim["durum"] == ADIM_SURUYOR and len(adim["ogeler"]) > 1:
            metin = bicim_boyut(kayit["bayt"])
            if kayit["toplam"]:
                metin += f" / {bicim_boyut(kayit['toplam'])}"
            g["oge"] = {"ad": self._oge_adi(bilesen_kimligi, son) or son, "metin": metin}
        sayac = adim.get("sayac")
        if sayac and sayac.get("i") is not None:
            birim = sayac.get("birim") or ""
            if sayac.get("n"):
                g["sayac_metni"] = MSG_SAYAC_OLCULEN.format(i=sayac["i"], n=sayac["n"], birim=birim)
            else:
                g["sayac_metni"] = MSG_SAYAC_YALIN.format(i=sayac["i"], birim=birim)
        return g

    def bilesen_gorunumu(self, kimlik: str) -> Dict[str, Any]:
        b = self.bilesenler[kimlik]
        return {"kimlik": kimlik, "ad": b["ad"], "onar": b["onar"], "durum": b["durum"],
                "durum_metni": ADIM_METINLERI.get(b["durum"], b["durum"]),
                "adimlar": [self.adim_gorunumu(kimlik, a) for a in b["adimlar"].values()]}

    def genel_gorunumu(self) -> Dict[str, Any]:
        simdi = self._saat()
        bayt = sum(k["bayt"] for k in self._ogeler.values())
        toplam = self.toplam_bayt
        hiz = self._hiz if (self.calisiyor and self._hiz
                            and simdi - self._hiz_zamani <= ayarlar.HIZ_PENCERESI_SN) else None
        g: Dict[str, Any] = {"bayt": bayt, "toplam_bayt": toplam, "oran": None,
                             "metin": bicim_boyut(bayt), "hiz_bayt_sn": hiz,
                             "hiz_metni": bicim_hiz(hiz), "kalan_sn": None, "kalan_metni": ""}
        if toplam:
            g["oran"] = min(1.0, bayt / toplam)
            g["metin"] = f"{bicim_boyut(bayt)} / {bicim_boyut(toplam)}"
            if hiz and bayt < toplam:
                kalan = (toplam - bayt) / hiz
                g["kalan_sn"] = kalan
                g["kalan_metni"] = MSG_TAHMINI.format(sure=_tahmini_sure(kalan))
        return g

    def _bekleme_gorunumu(self) -> Optional[Dict[str, Any]]:
        simdi = self._saat()
        for anahtar in list(self._beklemeler):
            bk = self._beklemeler[anahtar]
            if bk["kalan_sn"] is not None and bk["zaman"] + bk["kalan_sn"] < simdi - 1:
                del self._beklemeler[anahtar]
        if not self._beklemeler:
            return None
        return dict(next(reversed(self._beklemeler.values())))

    def _is_alanlari(self) -> Dict[str, Any]:
        return {"plan_kimligi": self.plan_kimligi, "baslangic": self.baslangic,
                "bitis": self.bitis, "sonuc": self.sonuc, "calisiyor": self.calisiyor,
                "hata": self.hata, "gunluk": self.gunluk,
                "gunluk_metni": _ev_kisalt(Path(self.gunluk)) if self.gunluk else "",
                "gecen_metni": bicim_sure((self.bitis or self._saat()) - self.baslangic)}

    def ozet(self) -> Dict[str, Any]:
        return {"is": self._is_alanlari(), "genel": self.genel_gorunumu(),
                "bekleme": self._bekleme_gorunumu(),
                "bilesenler": [self.bilesen_gorunumu(k) for k in self.bilesenler]}


def _tahmini_sure(saniye: float) -> str:
    """Tahmin dakikaya yuvarlanır (bir dakikanın altı saniyeyle)."""
    if saniye >= 60:
        return bicim_sure(round(saniye / 60) * 60)
    return bicim_sure(saniye)


# ---------------------------------------------------------------------------
# "Diskte" (E.7): kurulu bileşenin gerçek disk kullanımı
# ---------------------------------------------------------------------------
def _alt_yollari_ayikla(yollar: Iterable[Path]) -> List[Path]:
    """Başka bir yolun içinde kalan yolları atar (klasör ve içindeki dosya iki kez sayılmasın)."""
    sirali = sorted({Path(y) for y in yollar}, key=lambda y: len(y.parts))
    secilen: List[Path] = []
    for yol in sirali:
        if not any(yol == s or s in yol.parents for s in secilen):
            secilen.append(yol)
    return secilen


def _dist_info(site: Path, ad: str, surum: str) -> Optional[Path]:
    hedef = (durum.normal_ad(ad), surum)
    try:
        girdiler = list(os.scandir(site))
    except OSError:
        return None
    for girdi in girdiler:
        if not girdi.name.endswith(".dist-info") or "-" not in girdi.name:
            continue
        paket, paket_surum = girdi.name[: -len(".dist-info")].rsplit("-", 1)
        if (durum.normal_ad(paket), paket_surum) == hedef:
            return Path(girdi.path)
    return None


def _paket_disk_bayt(site: Path, ad: str, surum: str, sayilan: set) -> int:
    """Paketin RECORD'undaki üst düzey girdilerin (``__pycache__`` dahil) disk kullanımı.

    ``sayilan`` önceki paketlerin saydığı girdileri taşır; ortak ad alanı
    klasörü iki kez sayılmaz.
    """
    dist = _dist_info(site, ad, surum)
    if dist is None:
        return 0
    ustler = {dist.name}
    disari: List[Path] = []
    try:
        satirlar = (dist / "RECORD").read_text(encoding="utf-8").splitlines()
    except OSError:
        satirlar = []
    for satir in csv.reader(satirlar):
        if not satir or not satir[0]:
            continue
        goreli = satir[0]
        if goreli.startswith("../") or goreli.startswith("/"):
            disari.append(Path(os.path.normpath(site / goreli)))
            continue
        ustler.add(goreli.split("/", 1)[0])
    toplam = 0
    for ust in sorted(ustler):
        if ("site", ust) not in sayilan:
            sayilan.add(("site", ust))
            toplam += disk.disk_kullanimi_bayt(site / ust)
    for yol in disari:
        if ("dis", str(yol)) not in sayilan:
            sayilan.add(("dis", str(yol)))
            toplam += disk.disk_kullanimi_bayt(yol)
    return toplam


def diskte_olc(duzen: durum.KokDuzeni, bilesenler: Sequence[Mapping[str, Any]],
               kayitlar: Mapping[str, Any]) -> Dict[str, Optional[int]]:
    """Kurulu bileşenlerin ``du`` gibi ölçülen disk kullanımı; kaydı olmayan None.

    Kaynak ``kurulum.json`` kaydıdır (K3): ``dosyalar`` (göreli yol → bayt),
    ``gruplar[*].paketler`` (venv'e kurulan ``[ad, sürüm]``). ``tectonic_isit``
    adımlı bileşene ``<kök>/tex`` (tectonic + önbellek) sayılır. Birden çok
    bileşenin kilidindeki paket manifest sırasında ilk bileşene sayılır
    (sürüm ölçümüyle aynı kural; TASARIM C.1-3).
    """
    sayilan: set = set()
    sonuc: Dict[str, Optional[int]] = {}
    for bilesen in bilesenler:
        kimlik = str(bilesen.get("kimlik"))
        kayit = kayitlar.get(kimlik)
        if not isinstance(kayit, Mapping):
            sonuc[kimlik] = None
            continue
        yollar: List[Path] = []
        dosyalar = kayit.get("dosyalar")
        for goreli in (dosyalar.keys() if isinstance(dosyalar, Mapping) else []):
            try:
                yollar.append(disk.guvenli_yol(duzen.kok, str(goreli)))
            except disk.GecersizYol:
                continue
        if any(isinstance(a, Mapping) and a.get("tur") == "tectonic_isit"
               for a in bilesen.get("adimlar") or []):
            yollar.append(duzen.tex)
        toplam = 0
        for yol in _alt_yollari_ayikla(yollar):
            if ("dis", str(yol)) in sayilan:
                continue
            sayilan.add(("dis", str(yol)))
            toplam += disk.disk_kullanimi_bayt(yol)
        gruplar = kayit.get("gruplar")
        if isinstance(gruplar, Mapping):
            for bilgi in gruplar.values():
                paketler = bilgi.get("paketler") if isinstance(bilgi, Mapping) else None
                for cift in paketler or []:
                    if isinstance(cift, (list, tuple)) and len(cift) == 2:
                        toplam += _paket_disk_bayt(duzen.site_packages, str(cift[0]),
                                                   str(cift[1]), sayilan)
        sonuc[kimlik] = toplam
    return sonuc


def tema_css(uygulama: Path) -> str:
    """``.streamlit/config.toml [theme]`` renkleri CSS değişkeni olarak (renkler kopyalanmaz)."""
    try:
        tema = tomllib.loads((Path(uygulama) / TEMA_DOSYASI).read_text(encoding="utf-8"))
        tema = tema.get("theme") or {}
    except (OSError, ValueError):
        tema = {}
    satirlar = []
    for anahtar, degisken in TEMA_ESLEMESI:
        deger = tema.get(anahtar)
        if isinstance(deger, str) and _RENK.match(deger):
            satirlar.append(f"  {degisken}: {deger};")
    if tema.get("base") in ("dark", "light"):
        satirlar.append(f"  color-scheme: {tema['base']};")
    return ":root {\n" + "\n".join(satirlar) + "\n}\n"


def _tarih_tr(iso: str) -> str:
    """"2026-09-29" → "29.09.2026" (E.3 maketi); tanınmazsa olduğu gibi."""
    try:
        return _dt.date.fromisoformat(iso).strftime("%d.%m.%Y")
    except ValueError:
        return iso


# ---------------------------------------------------------------------------
# Denetleyici: API yöntemleri
# ---------------------------------------------------------------------------
class Yonetici:
    """Sunucunun çağırdığı API (TASARIM B.5), kurulum işinin ve Streamlit'in sahibi.

    ``kur`` bir ``kurulum.Kurulum`` (paketli kipte ``YoneticiKurulumu``) ya da
    manifest okunamadıysa None'dır; o durumda sayfa sorunu gösterir.
    """

    def __init__(self, duzen: durum.KokDuzeni, uygulama: Path, kur: Optional[Any], *,
                 gunluk_dizini: Path, manifest_hatasi: str = "",
                 yayin: Optional[sunucu.OlayYayini] = None,
                 streamlit: Optional[surecler.StreamlitSureci] = None,
                 acici: Callable[[str], None] = surecler.ac,
                 guncelleme_denetle: Callable[[str], Dict[str, Any]] = indirici.guncelleme_denetle,
                 internet_denetle: Callable[[str], str] = indirici.internet_durumu,
                 onceki_calisan: Optional[Mapping[str, Any]] = None,
                 surec_tablosu: Callable[[], List[Dict[str, Any]]] = surecler.surec_tablosu,
                 macos: Optional[str] = None, ev: Optional[Path] = None,
                 paket_yolu: Optional[Path] = None):
        self.duzen = duzen
        self.uygulama = Path(uygulama)
        self.kur = kur
        self.gunluk_dizini = Path(gunluk_dizini)
        self.manifest_hatasi = manifest_hatasi
        self.yayin = yayin or sunucu.OlayYayini()
        self.streamlit = streamlit or surecler.StreamlitSureci(duzen, self.uygulama)
        self._acici = acici
        self._guncelleme_denetle = guncelleme_denetle
        self._internet_denetle_islevi = internet_denetle
        self._surec_tablosu = surec_tablosu
        self._macos = macos if macos is not None else macos_surumu()
        self._kilit = threading.RLock()
        self._planlar: "collections.OrderedDict[str, Dict[str, Any]]" = collections.OrderedDict()
        self._is: Optional[IsGorunumu] = None
        self._is_ek: Dict[str, Any] = {}
        self._kapaniyor = threading.Event()
        self._kapanis_bitti = threading.Event()
        self._internet = indirici.INTERNET_BILINMIYOR
        self._internet_hazir = threading.Event()
        self._internet_basladi = False
        self._adlar: Optional[Dict[str, str]] = None
        self._diskte_onbellek: Optional[Tuple[float, Dict[str, Optional[int]]]] = None
        self.url = ""
        self._sunucu_kapat: Callable[[], None] = lambda: None
        self._son_calisan: Optional[Dict[str, Any]] = None
        self._yetimler: List[Dict[str, Any]] = []
        self._ev = Path(ev) if ev is not None else Path.home()
        ortam_paketi = os.environ.get(PAKET_YOLU_ORTAM_DEGISKENI)
        self._paket_yolu = (Path(paket_yolu) if paket_yolu is not None
                            else Path(ortam_paketi) if ortam_paketi else None)
        # "Kitapçevir'i kaldır…" yapıldıysa: {"veri_de_sil": bool}; kalanlar çıkışta silinir
        self._kaldirildi: Optional[Dict[str, Any]] = None
        if onceki_calisan:
            try:
                self._yetimler = surecler.yetimleri_bul(duzen, onceki_calisan,
                                                        tablo=self._surec_tablosu())
            except Exception:  # noqa: BLE001 — denetim yapılamazsa liste boş kalır
                logger.exception("yetim süreç denetimi yapılamadı")
            if self._yetimler:
                logger.warning("önceki oturumdan kalan %d süreç var", len(self._yetimler))

    # ======================================================================
    # Yaşam döngüsü
    # ======================================================================
    def baglan(self, url: str, sunucu_kapat: Callable[[], None]) -> None:
        """Sunucu açıldıktan sonra: adres (Streamlit ortamı için) ve kapatıcı."""
        self.url = url
        self._sunucu_kapat = sunucu_kapat

    def arka_isleri_baslat(self, ebeveyn_pid: Optional[int] = None) -> None:
        """İnternet denetimi, ``calisan.json`` güncellemesi ve kabuk bekçisi."""
        with self._kilit:
            self._internet_basladi = True
        threading.Thread(target=self._internet_denetle, name="internet", daemon=True).start()
        threading.Thread(target=self._calisan_guncelle_dongusu, name="calisan",
                         daemon=True).start()
        threading.Thread(target=self._ebeveyn_izle, args=(ebeveyn_pid,), name="ebeveyn",
                         daemon=True).start()

    def _ilk_url(self) -> Optional[str]:
        if self.kur is None:
            return None
        for b in self.kur.bilesenler:
            for oge in b.get("ogeler") or []:
                adres = str(oge.get("url", "")) if isinstance(oge, Mapping) else ""
                if adres.startswith("https://"):
                    return adres
        return None

    def _internet_hazirla(self, yenile: bool = False) -> None:
        """İlk denetim arka planda başlamadıysa (ya da yenileme istendiyse) burada
        yapılır; başladıysa sonucu en çok zaman aşımı kadar beklenir."""
        with self._kilit:
            basla = yenile or not self._internet_basladi
            if basla:
                self._internet_basladi = True
                self._internet_hazir.clear()
        if basla:
            self._internet_denetle()
        else:
            self._internet_hazir.wait(ayarlar.CEVRIMDISI_ZAMAN_ASIMI_SN + 1)

    def _internet_denetle(self) -> None:
        try:
            adres = self._ilk_url()
            self._internet = (self._internet_denetle_islevi(adres) if adres
                              else indirici.INTERNET_BILINMIYOR)
        except Exception:  # noqa: BLE001 — denetlenemedi "bilinmiyor"dur
            logger.exception("internet denetimi yapılamadı")
            self._internet = indirici.INTERNET_BILINMIYOR
        finally:
            self._internet_hazir.set()

    def calisan_kaydi(self) -> Dict[str, Any]:
        calisiyor = self.streamlit.calisiyor()
        return {"pid": os.getpid(), "url": self.url, "kok": str(self.duzen.kok),
                "zaman": durum.simdi(), "streamlit": self.streamlit.kayit(),
                "is_gruplari": self.streamlit.torun_gruplari() if calisiyor else []}

    def calisan_yaz(self) -> None:
        kayit = self.calisan_kaydi()
        karsilastirma = {k: v for k, v in kayit.items() if k != "zaman"}
        if karsilastirma != self._son_calisan:
            surecler.calisan_yaz(self.duzen, kayit)
            self._son_calisan = karsilastirma

    def _calisan_guncelle_dongusu(self) -> None:
        while not self._kapaniyor.wait(surecler.TORUN_YOKLAMA_SN):
            try:
                self.calisan_yaz()
            except Exception:  # noqa: BLE001
                logger.exception("calisan.json güncellenemedi")

    def _ebeveyn_izle(self, pid: Optional[int]) -> None:
        """Kabuk ölürse (``kill -9``) yönetici de kapanır.

        Kabuk PID vermezse ebeveyn değişimine bakılır: ebeveyn ölünce süreç
        launchd'ye (1) bağlanır (TASARIM B.6).

        stdin OKUNMAZ (kabuğun borusu yalnız açık tutulur). Bir gün kabuk
        ölümünü stdin EOF'uyla anlamak gerekirse ``os.read(0, …)`` kullanılmalı:
        daemon iş parçacığında ``sys.stdin.buffer.read()`` çıkışta "could not
        acquire lock for <stdin>" ölümcül hatası ve SIGABRT veriyor (K1 ölçümü;
        ``test_sunucu.test_yonetici_stdin_okumaz`` bekçisi).
        """
        ilk_ebeveyn = os.getppid()
        while not self._kapaniyor.wait(EBEVEYN_YOKLAMA_SN):
            if pid:
                yasiyor = surecler.surec_yasiyor(pid)
            else:
                yasiyor = os.getppid() == ilk_ebeveyn
            if not yasiyor:
                logger.warning("kabuk sonlandı; yönetici kapanıyor")
                self.kapanis_baslat()
                return

    def kapanis_baslat(self) -> None:
        """Kapanışı arka planda başlatır (istek ya da sinyal işleyicisinden)."""
        if self._kapaniyor.is_set():
            return
        threading.Thread(target=self.kapanis, name="kapanis", daemon=False).start()

    def kapanis(self) -> None:
        """İşi iptal eder, Streamlit'i durdurur, calisan.json'u siler, sunucuyu kapatır."""
        with self._kilit:
            ilk = not self._kapaniyor.is_set()
            self._kapaniyor.set()
        if not ilk:
            self._kapanis_bitti.wait(ayarlar.KAPANIS_SURESI_SN)
            return
        # Kabuk en çok KAPANIS_SURESI_SN bekler (B.6-3); yönetici kendi işini
        # bu süreden KAPANIS_PAYI_SN önce bitirir ki kendisi de çıkabilsin.
        son = time.monotonic() + ayarlar.KAPANIS_SURESI_SN - surecler.KAPANIS_PAYI_SN
        try:
            logger.info("kapanış başladı")
            if self.kur is not None and self.kur.calisiyor:
                self.kur.iptal()
                self.kur.bekle(max(0.0, min(IS_BEKLEME_KAPANISTA_SN, son - time.monotonic())))
            self.streamlit.durdur(max(1.0, son - time.monotonic()))
            surecler.calisan_sil(self.duzen)
            if self._kaldirildi is not None:
                self._kaldirma_son_temizlik()
        except Exception:  # noqa: BLE001
            logger.exception("kapanışta hata")
        finally:
            self._kapanis_bitti.set()
            self._sunucu_kapat()

    # ======================================================================
    # Yardımcılar
    # ======================================================================
    def _kur_gerekli(self) -> Any:
        if self._kaldirildi is not None:
            raise ApiHatasi(HTTPStatus.CONFLICT, "kaldirildi", MSG_KALDIRILDI)
        if self.kur is None:
            raise ApiHatasi(HTTPStatus.SERVICE_UNAVAILABLE, "manifest_yok",
                            MSG_MANIFEST_YOK.format(neden=self.manifest_hatasi))
        return self.kur

    def _bilesen_bul(self, kimlik: Any) -> Mapping[str, Any]:
        kur = self._kur_gerekli()
        try:
            return kur.bilesen(str(kimlik))
        except kurulum.PlanHatasi as exc:
            raise ApiHatasi(HTTPStatus.NOT_FOUND, "bilesen_yok", exc.ileti)

    def _surum(self) -> str:
        return str(self.kur.uygulama_surumu) if self.kur is not None else ""

    def _manifest_alani(self, alan: str) -> Mapping[str, Any]:
        deger = (self.kur.manifest or {}).get(alan) if self.kur is not None else None
        return deger if isinstance(deger, Mapping) else {}

    def _is_suruyor(self) -> bool:
        return self.kur is not None and bool(self.kur.calisiyor)

    def _adlar_oku(self) -> Dict[str, str]:
        """Ekrandaki adlar ``ui.branding``'den (tek kaynak); okunamazsa sayfanın kendi başlığı."""
        if self._adlar is None:
            try:
                donanim.core_hardware(self.uygulama)   # ui.branding core'u ister: kabuk kurulsun
                branding = importlib.import_module("ui.branding")
                self._adlar = {"uygulama": branding.APP_NAME, "alt_baslik": branding.APP_TAGLINE,
                               "tex_yolu": branding.TEX_TITLE, "model_yolu": branding.MODEL_TITLE}
            except Exception as exc:  # noqa: BLE001 — sayfa kendi başlığıyla açılır
                logger.warning("ui.branding okunamadı: %s", exc)
                self._adlar = {}
        return self._adlar

    def _kaynaklar(self) -> List[str]:
        """Öğelerin indiği sunucuların adları (manifest sırasıyla, yinelenmeden)."""
        adlar: List[str] = []
        for b in (self.kur.bilesenler if self.kur is not None else []):
            for oge in b.get("ogeler") or []:
                if not isinstance(oge, Mapping):
                    continue
                ad = _sunucu_adi(str(oge.get("url", "")))
                if ad and ad not in adlar:
                    adlar.append(ad)
        return adlar

    def _boyut_gorunumu(self, boyut: Mapping[str, Any]) -> Dict[str, Any]:
        indirme, disk_bayt = boyut.get("indirme_bayt"), boyut.get("disk_bayt")
        return {"indirme_bayt": indirme, "disk_bayt": disk_bayt,
                "olcum_notu": boyut.get("olcum_notu", ""),
                "indirme_metni": bicim_boyut_hassas(indirme),
                "disk_metni": bicim_boyut_hassas(disk_bayt) if disk_bayt is not None
                else "ölçülmedi"}

    def _lisans_gorunumu(self, b: Mapping[str, Any],
                         durumu: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        d = dict(durumu) if durumu is not None else lisans.onay_durumu(self.duzen, b)
        g = {"onay_gerekli": bool(d.get("onay_gerekli")), "onaylandi": bool(d.get("onaylandi")),
             "neden": d.get("neden", ""), "metin": None}
        if g["onay_gerekli"]:
            try:
                g["metin"] = self.kur.lisans_metni(str(b.get("kimlik")))
            except (lisans.LisansHatasi, kurulum.KurulumHatasi) as exc:
                g["neden"] = str(exc)
        return g

    def _hazir_degil_nedeni(self, gorunumler: Sequence[Mapping[str, Any]]) -> str:
        for g in gorunumler:
            if g.get("zorunluluk") == ZORUNLU and g.get("durum") != durum.DURUM_KURULU:
                return MSG_HAZIR_DEGIL.format(ad=g.get("ad", g.get("kimlik")),
                                              durum=g.get("durum_metni", ""))
        return MSG_TEX_ETKIN_DEGIL

    def _kurulu_kayitlar(self) -> Mapping[str, Any]:
        defter = getattr(self.kur, "defter", None)
        if defter is None:
            return {}
        try:
            return defter.oku().get("bilesenler") or {}
        except Exception:  # noqa: BLE001 — okunamayan kayıt: sürüm "—"
            logger.exception("kurulum.json okunamadı")
            return {}

    def _diskte(self) -> Dict[str, Optional[int]]:
        try:
            damga = self.duzen.kurulum_dosyasi.stat().st_mtime_ns
        except OSError:
            damga = 0
        if self._diskte_onbellek and self._diskte_onbellek[0] == damga:
            return self._diskte_onbellek[1]
        try:
            olcum = diskte_olc(self.duzen, self.kur.bilesenler, self._kurulu_kayitlar())
        except Exception:  # noqa: BLE001 — ölçülemeyen "—" gösterilir
            logger.exception("disk kullanımı ölçülemedi")
            olcum = {}
        self._diskte_onbellek = (damga, olcum)
        return olcum

    # ======================================================================
    # GET /api/durum
    # ======================================================================
    def durum(self, sorgu: Mapping[str, str]) -> Dict[str, Any]:
        if self._kaldirildi is not None:
            return {"kaldirildi": True, "sorun": None, "adlar": self._adlar or {},
                    "bilesenler": [], "is": {"calisiyor": False}, "uygulama_hazir": False}
        self._internet_hazirla(yenile=sorgu.get("internet") == "yenile")
        bos = disk.bos_alan_bayt(self.duzen.kok)
        uygulama_alani = self._manifest_alani("uygulama")
        yanit: Dict[str, Any] = {
            "surum": self._surum(), "yapi": uygulama_alani.get("yapi", ""),
            "macos": self._macos,
            "kok": str(self.duzen.kok), "kok_metni": _ev_kisalt(self.duzen.kok),
            "bos_disk_bayt": bos, "bos_disk_metni": bicim_boyut(bos),
            "internet": self._internet,
            "donanim": donanim.durum_ozeti(self.uygulama),
            "gunluk_dizini": str(self.gunluk_dizini),
            "gunluk_metni": _ev_kisalt(self.gunluk_dizini),
            "kaynak_kodu_url": ayarlar.KAYNAK_KODU_URL,
            "kaynak_kodu_metni": ayarlar.KAYNAK_KODU_URL.split("://", 1)[-1],
            "adlar": self._adlar_oku(),
            "is": {"calisiyor": self._is_suruyor(),
                   "plan_kimligi": self._is.plan_kimligi if self._is else None,
                   "sonuc": self._is.sonuc if self._is else None},
            "streamlit": {"calisiyor": self.streamlit.calisiyor(), "url": self.streamlit.url},
            "yetimler": [{"pid": y["pid"], "komut": y["komut"]} for y in self._yetimler],
            "sorun": None, "bilesenler": [], "tex_algilanan": [], "tex_etkin": None,
            "uygulama_hazir": False, "hazir_degil_nedeni": "", "kip_onerisi": "sihirbaz",
            "olcum_metni": "", "kaynaklar": [], "tex_varsayilan": None, "kaldirildi": False,
        }
        if self.kur is None:
            yanit["sorun"] = {"kod": "manifest_yok",
                              "ileti": MSG_MANIFEST_YOK.format(neden=self.manifest_hatasi)}
            return yanit
        olcum = self._manifest_alani("olcum")
        if olcum:
            yanit["olcum_metni"] = MSG_OLCUM_NOTU.format(
                surum=self._surum(), tarih=_tarih_tr(str(olcum.get("tarih", ""))),
                makine=olcum.get("makine", ""), macos=olcum.get("macos", ""))
        ozetler = self.kur.bilesen_ozetleri()
        algilanan = self.kur.tex_algilanan()
        yanit["tex_algilanan"] = algilanan
        yanit["kaynaklar"] = self._kaynaklar()
        for g in self.kur.gruplar:
            if g.get("kimlik") == TEX_GRUBU:
                yanit["tex_varsayilan"] = g.get("varsayilan")
        diskte = self._diskte() if sorgu.get("diskte") == "1" else {}
        kayitlar = self._kurulu_kayitlar()
        gorunumler = []
        for o in ozetler:
            kimlik = o["kimlik"]
            b = self.kur.bilesen(kimlik)
            g = dict(o)
            kayit = kayitlar.get(kimlik) if isinstance(kayitlar.get(kimlik), Mapping) else {}
            g["surum"] = kayit.get("surum") or o.get("surum", "")
            g["durum_metni"] = DURUM_METINLERI.get(o["durum"], o["durum"])
            g["boyut"] = self._boyut_gorunumu(o.get("boyut") or {})
            g["lisans"] = self._lisans_gorunumu(b, o.get("lisans"))
            g["bagimliliklar"] = list(b.get("bagimliliklar") or [])
            g["diskte_bayt"] = diskte.get(kimlik)
            g["diskte_metni"] = (bicim_boyut_hassas(diskte[kimlik])
                                 if diskte.get(kimlik) is not None else "—")
            on_kosul = b.get("on_kosul") if isinstance(b.get("on_kosul"), Mapping) else {}
            if on_kosul.get("algila") == ALGILA_TEX_KURULU and algilanan:
                gecen = next((a for a in algilanan if (a.get("sinama") or {}).get("gecti")), None)
                g["algilanan"] = gecen or algilanan[0]
                if gecen is None and all(a.get("sinama") is None for a in algilanan):
                    # Sınama henüz yapılmadı: K3'ün "derleyemedi" hükmü değil, bekleniyor.
                    g["secilebilir"] = False
                    g["neden"] = ""
                    g["sinama_bekleniyor"] = True
            if o.get("etkin"):
                yanit["tex_etkin"] = kimlik
            gorunumler.append(g)
        yanit["bilesenler"] = gorunumler
        hazir = bool(self.kur.uygulama_hazir())
        yanit["uygulama_hazir"] = hazir
        yanit["hazir_degil_nedeni"] = "" if hazir else self._hazir_degil_nedeni(gorunumler)
        zorunlu_eksik = any(g["durum"] in (durum.DURUM_EKSIK, durum.DURUM_KURULUYOR)
                            for g in gorunumler if g.get("zorunluluk") == ZORUNLU)
        tex_yok = yanit["tex_varsayilan"] is not None and yanit["tex_etkin"] is None
        yanit["kip_onerisi"] = "sihirbaz" if (zorunlu_eksik or tex_yok) else "bilesenler"
        return yanit

    # ======================================================================
    # Lisans
    # ======================================================================
    def lisans(self, govde: Mapping[str, Any]) -> Dict[str, Any]:
        kur = self._kur_gerekli()
        b = self._bilesen_bul(govde.get("bilesen"))
        kimlik = str(b.get("kimlik"))
        kabul = govde.get("kabul")
        if not isinstance(kabul, bool):
            raise ApiHatasi(HTTPStatus.BAD_REQUEST, "kabul", "'kabul' true ya da false olmalı.")
        if not lisans.onay_gerekli(b):
            raise ApiHatasi(HTTPStatus.BAD_REQUEST, "onay_gerekmez",
                            MSG_LISANS_ONAY_GEREKMEZ.format(ad=b.get("ad", kimlik)))
        if kabul:
            try:
                kur.lisans_kabul(kimlik)
            except (lisans.LisansHatasi, kurulum.KurulumHatasi) as exc:
                raise ApiHatasi(HTTPStatus.CONFLICT, "lisans", str(exc))
            logger.info("lisans kabul edildi: %s", kimlik)
        else:
            if self._is is not None and self._is.calisiyor and kimlik in self._is.bilesenler:
                raise ApiHatasi(HTTPStatus.CONFLICT, "is_suruyor", MSG_LISANS_GERI_ALINAMAZ)
            kur.lisans_geri_al(kimlik)
            logger.info("lisans onayı geri alındı: %s", kimlik)
        return {"bilesen": kimlik, "lisans": self._lisans_gorunumu(b)}

    def lisans_metni(self, sorgu: Mapping[str, str]) -> Dict[str, Any]:
        """Lisansın tam metni: yerel kopyanın SHA-256'sı manifestle eşleşirse metin,
        değilse yalnız bağlantı (doğrulanmamış metin lisans diye gösterilmez).

        Yerel kopya ``lisans.metin_dosyasi``: önce köke (``<kök>/LICENSES/…``),
        sonra uygulama koduna göre aranır.
        """
        b = self._bilesen_bul(sorgu.get("bilesen"))
        alan = b.get("lisans") if isinstance(b.get("lisans"), Mapping) else {}
        beklenen = str(alan.get("metin_sha256") or "")
        dosya = str(alan.get("metin_dosyasi") or "")
        yanit = {"bilesen": b.get("kimlik"), "metin": None, "dogrulandi": False,
                 "url": str(alan.get("metin_url") or ""), "neden": ""}
        if not dosya:
            yanit["neden"] = MSG_LISANS_YEREL_YOK
            return yanit
        for kok in (self.duzen.kok, self.uygulama):
            try:
                yol = disk.guvenli_yol(kok, dosya)
                if yol.stat().st_size > LISANS_METNI_EN_COK_BAYT:
                    continue
                ham = yol.read_bytes()
            except (disk.GecersizYol, OSError):
                continue
            if beklenen and hashlib.sha256(ham).hexdigest() == beklenen:
                yanit.update(metin=ham.decode("utf-8", "replace"), dogrulandi=True)
                return yanit
            yanit["neden"] = MSG_LISANS_YEREL_UYUSMUYOR
        if not yanit["neden"]:
            yanit["neden"] = MSG_LISANS_YEREL_BULUNAMADI
        return yanit

    # ======================================================================
    # Plan
    # ======================================================================
    def _plan_gorunumu(self, p: Mapping[str, Any], secim: Mapping[str, Any]) -> Dict[str, Any]:
        kur = self.kur
        onar = set(p.get("onar") or [])
        satirlar = []
        for kimlik in p.get("bilesenler") or []:
            b = kur.bilesen(kimlik)
            indirme, disk_bayt = kur.boyut(b, "indirme_bayt"), kur.boyut(b, "disk_bayt")
            satirlar.append({
                "bilesen": kimlik, "ad": _plan_adi(b), "onar": kimlik in onar,
                "indirme_bayt": indirme, "disk_bayt": disk_bayt,
                "indirme_metni": bicim_boyut_hassas(indirme),
                "disk_metni": bicim_boyut_hassas(disk_bayt) if disk_bayt is not None
                else "ölçülmedi"})
        eksik_onaylar = []
        for kimlik in p.get("eksik_onaylar") or []:
            b = kur.bilesen(kimlik)
            try:
                ileti = kur.lisans_metni(kimlik).get("red_iletisi", "")
            except (lisans.LisansHatasi, kurulum.KurulumHatasi) as exc:
                ileti = str(exc)
            eksik_onaylar.append({"bilesen": kimlik, "ad": b.get("ad", kimlik), "ileti": ileti})
        eksik = p.get("eksik_bayt") or 0
        tex = (p.get("secimler") or {}).get(TEX_GRUBU)
        etkin = (durum.tex_oku(self.duzen) or {}).get("tur")
        # Kurulu bir TeX türüne geri dönüş: kurulacak bileşen yok ama etkin TeX
        # değişecek. K3 boş planı yine koşar ve durum/tex.json'u çevirir.
        yalniz_tex = bool(not satirlar and tex and tex != etkin)
        return {
            "plan_kimligi": p["plan_kimligi"], "tex": tex, "tex_etkin": etkin,
            "yalniz_tex": yalniz_tex,
            "tex_ad": kur.bilesen(tex).get("ad", tex) if tex else "",
            "secim": dict(secim), "satirlar": satirlar, "bos": not satirlar,
            "kurulacaklar_metni": ", ".join(s["ad"] for s in satirlar),
            "indirme_bayt": p.get("indirme_bayt"),
            "indirme_metni": bicim_boyut_hassas(p.get("indirme_bayt")),
            "disk_bayt": p.get("disk_bayt"), "disk_metni": bicim_boyut_hassas(p.get("disk_bayt")),
            "disk_olculmeyen": [kur.bilesen(k).get("ad", k)
                                for k in p.get("boyutu_bilinmeyen") or []],
            "gecici_bayt": p.get("gecici_bayt"),
            "bos_disk_bayt": p.get("bos_disk_bayt"),
            "bos_disk_metni": bicim_boyut(p.get("bos_disk_bayt")),
            "gerekli_bayt": p.get("gerekli_bayt"),
            "gerekli_metni": bicim_boyut_hassas(p.get("gerekli_bayt")),
            "yeter": bool(p.get("yeter")), "eksik_bayt": eksik,
            "eksik_metni": bicim_boyut_hassas(eksik) if eksik else "",
            "eksik_onaylar": eksik_onaylar, "internet": self._internet,
            "kok_metni": _ev_kisalt(self.duzen.kok),
        }

    def plan(self, govde: Mapping[str, Any]) -> Dict[str, Any]:
        kur = self._kur_gerekli()
        tex = govde.get("tex")
        istege = govde.get("istege_bagli") or []
        onar = govde.get("onar") or []
        if (tex is not None and not isinstance(tex, str)) or not isinstance(istege, list) \
                or not isinstance(onar, list) \
                or not all(isinstance(k, str) for k in [*istege, *onar]):
            raise ApiHatasi(HTTPStatus.BAD_REQUEST, "secim", MSG_SECIM_GECERSIZ)
        try:
            p = kur.plan(tex=tex, istege_bagli=istege, onar=onar)
        except kurulum.KurulumHatasi as exc:
            raise _kurulum_hatasi(exc)
        secim = {"tex": tex, "istege_bagli": list(istege), "onar": list(onar)}
        gorunum = self._plan_gorunumu(p, secim)
        with self._kilit:
            self._planlar[p["plan_kimligi"]] = {"plan": dict(p), "secim": secim}
            while len(self._planlar) > PLAN_SAKLAMA_EN_COK:
                self._planlar.popitem(last=False)
        return gorunum

    # ======================================================================
    # Kurulum işi
    # ======================================================================
    def _is_ogeleri(self, bilesenler: Sequence[str]
                    ) -> Tuple[Dict[Tuple[str, str], Dict[str, int]],
                               Dict[Tuple[str, str], str], Dict[Tuple[str, str], str]]:
        """Planın öğeleri: ``{(bileşen, öğe): {"bayt": diskte duran, "toplam"}}``,
        adresleri ve ekrandaki adları (hedef yolundan)."""
        ogeler: Dict[Tuple[str, str], Dict[str, int]] = {}
        adresler: Dict[Tuple[str, str], str] = {}
        adlar: Dict[Tuple[str, str], str] = {}
        for kimlik in bilesenler:
            for oge in self.kur.uygun_ogeler(self.kur.bilesen(kimlik)):
                anahtar = (kimlik, str(oge.get("kimlik")))
                try:
                    mevcut = manifest.oge_mevcut_bayt(self.duzen.kok, oge)
                except (KeyError, TypeError, ValueError):
                    mevcut = 0
                ogeler[anahtar] = {"bayt": mevcut, "toplam": int(oge.get("bayt") or 0)}
                adresler[anahtar] = str(oge.get("url", ""))
                adlar[anahtar] = oge_gorunen_adi(str(oge.get("hedef", "")), anahtar[1])
        return ogeler, adresler, adlar

    def kur_baslat(self, plan_kimligi: str) -> Dict[str, Any]:
        """``POST /api/kur``'un gövdesi (başlıksız kip de kullanır)."""
        kur = self._kur_gerekli()
        with self._kilit:
            if self._kapaniyor.is_set():
                raise ApiHatasi(HTTPStatus.SERVICE_UNAVAILABLE, "kapaniyor", MSG_KAPANIYOR)
            if kur.calisiyor:
                raise ApiHatasi(HTTPStatus.CONFLICT, kurulum.KOD_IS_SURUYOR, MSG_IS_SURUYOR)
            kayit = self._planlar.get(plan_kimligi)
            if kayit is None:
                raise ApiHatasi(HTTPStatus.CONFLICT, kurulum.KOD_PLAN_YOK, kurulum.MSG_PLAN_YOK)
            isler = self._calisan_is_sayisi()
            if isler:
                raise ApiHatasi(HTTPStatus.CONFLICT, "uygulama_isi_suruyor",
                                MSG_UYGULAMA_ISI.format(n=isler))
            plan = kayit["plan"]
            onar = set(plan.get("onar") or [])
            bilesenler = [(k, _plan_adi(kur.bilesen(k)), k in onar)
                          for k in plan.get("bilesenler") or []]
            ogeler, adresler, adlar = self._is_ogeleri([k for k, _, _ in bilesenler])
            kalici = {k: [str(o.get("kimlik")) for o in kur.kalici_ogeler(kur.bilesen(k))]
                      for k, _, _ in bilesenler}
            gorunum = IsGorunumu(plan_kimligi, bilesenler, ogeler, kalici=kalici,
                                 kaynak_adi=lambda bil, oge: _sunucu_adi(adresler.get((bil, oge),
                                                                                      "")),
                                 oge_adi=lambda bil, oge: adlar.get((bil, oge), oge))
            damga = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            gunluk_yolu = self.gunluk_dizini / f"kurulum-{damga}.log"
            isleyici: Optional[logging.Handler] = None
            try:
                self.gunluk_dizini.mkdir(parents=True, exist_ok=True)
                isleyici = logging.FileHandler(gunluk_yolu, encoding="utf-8")
                isleyici.setFormatter(logging.Formatter(_GUNLUK_BICIMI))
                logging.getLogger("kitapcevir").addHandler(isleyici)
                gorunum.gunluk = str(gunluk_yolu)
                kur.gunluk_yolu = str(gunluk_yolu)
            except OSError as exc:
                logger.warning("kurulum günlüğü açılamadı: %s", exc)
            streamlit_acikti = self.streamlit.calisiyor()
            if streamlit_acikti:
                self.streamlit.durdur()
            onceki_is, onceki_ek = self._is, self._is_ek
            self._is = gorunum
            self._is_ek = {"gunluk_isleyici": isleyici, "streamlit_acikti": streamlit_acikti}
            try:
                kur.baslat(plan_kimligi, self._kurulum_olayi)
            except kurulum.KurulumHatasi as exc:
                self._is, self._is_ek = onceki_is, onceki_ek
                self._gunluk_kapat(isleyici)
                if exc.kod in (kurulum.KOD_PLAN_ESKIDI, kurulum.KOD_PLAN_YOK):
                    self._planlar.pop(plan_kimligi, None)
                raise _kurulum_hatasi(exc)
            logger.info("kurulum başladı: plan %s, bileşenler %s", plan_kimligi,
                        [k for k, _, _ in bilesenler])
            self._olay_yayinla(OLAY_IS_BASLADI, {"plan_kimligi": plan_kimligi,
                                                 "bilesenler": [k for k, _, _ in bilesenler]})
            return {"plan_kimligi": plan_kimligi, "baslangic": gorunum.baslangic}

    def kur_istegi(self, govde: Mapping[str, Any]) -> Tuple[int, Dict[str, Any]]:
        return HTTPStatus.ACCEPTED, self.kur_baslat(str(govde.get("plan_kimligi") or ""))

    @staticmethod
    def _gunluk_kapat(isleyici: Optional[logging.Handler]) -> None:
        if isleyici is not None:
            logging.getLogger("kitapcevir").removeHandler(isleyici)
            isleyici.close()

    def _olay_yayinla(self, tur: str, veri: Mapping[str, Any], *, yayinla: bool = True) -> None:
        temiz = json.loads(json.dumps(dict(veri), ensure_ascii=False, default=str))
        with self._kilit:
            # Görünüm ve yayın aynı kilit altında: eşzamanlı indirme iş
            # parçacıklarının olayları ekrana uygulandıkları sırayla gider.
            gorunum = self._is.uygula(tur, temiz) if self._is is not None else {}
            if yayinla:
                temiz["gorunum"] = gorunum
                temiz["sunucu_zamani"] = sunucu.zaman_damgasi()
                self.yayin.yayinla(tur, temiz)

    def _kurulum_olayi(self, tur: str, veri: Mapping[str, Any]) -> None:
        """K3/K2'nin olay geri çağırımı (iş ve indirme iş parçacıklarından)."""
        if tur not in KURULUM_OLAYLARI:
            logger.warning("bilinmeyen kurulum olayı yok sayıldı: %s", tur)
            return
        if tur in (OLAY_ADIM_BASLADI, OLAY_ADIM_BITTI):
            logger.info("%s: %s %s", tur, veri.get("bilesen"), veri.get("ad") or veri.get("adim"))
        elif tur == OLAY_HATA:
            logger.error("kurulum hatası: %s: %s (%s)", veri.get("kod"), veri.get("ileti"),
                         veri.get("ayrinti"))
        self._olay_yayinla(tur, veri, yayinla=tur not in SESSIZ_OLAYLAR)
        if tur == OLAY_BITTI:
            self._is_bitti(str(veri.get("sonuc") or SONUC_HATA))

    def _is_bitti(self, sonuc: str) -> None:
        with self._kilit:
            ek, self._is_ek = self._is_ek, {}
            self._planlar.clear()
            self._diskte_onbellek = None
        logger.info("kurulum bitti: %s", sonuc)
        self._gunluk_kapat(ek.get("gunluk_isleyici"))
        if ek.get("streamlit_acikti") and sonuc == SONUC_TAMAM and not self._kapaniyor.is_set():
            threading.Thread(target=self._streamlit_yeniden, name="streamlit-yeniden",
                             daemon=True).start()

    def _streamlit_yeniden(self) -> None:
        try:
            self.streamlit.baslat(self._streamlit_icin_url())
        except surecler.StreamlitHatasi as exc:
            logger.error("Streamlit yeniden başlatılamadı: %s", exc)

    def is_ozeti(self, sorgu: Mapping[str, str]) -> Dict[str, Any]:
        with self._kilit:
            if self._is is None:
                return {"is": None, "genel": None, "bekleme": None, "bilesenler": []}
            return self._is.ozet()

    def iptal(self, govde: Mapping[str, Any]) -> Tuple[int, Dict[str, Any]]:
        if not self._is_suruyor() or not self.kur.iptal():
            raise ApiHatasi(HTTPStatus.CONFLICT, "is_yok", MSG_IS_YOK)
        logger.info("kurulum için durdurma istendi")
        return HTTPStatus.ACCEPTED, {"iptal": True}

    def _calisan_is_sayisi(self) -> int:
        """Streamlit'in torunlarında koşan uygulama işleri (iş başına bir süreç grubu)."""
        return len({s["pgid"] for s in self.streamlit.calisan_isler()})

    def is_durumu(self, sorgu: Mapping[str, str]) -> Dict[str, Any]:
        return {"calisan_is": self._calisan_is_sayisi(), "kurulum_suruyor": self._is_suruyor()}

    def kaldir(self, govde: Mapping[str, Any]) -> Dict[str, Any]:
        kur = self._kur_gerekli()
        b = self._bilesen_bul(govde.get("bilesen"))
        kimlik = str(b.get("kimlik"))
        with self._kilit:
            if kur.calisiyor:
                raise ApiHatasi(HTTPStatus.CONFLICT, kurulum.KOD_IS_SURUYOR, MSG_IS_SURUYOR)
            isler = self._calisan_is_sayisi()
            if isler:
                raise ApiHatasi(HTTPStatus.CONFLICT, "uygulama_isi_suruyor",
                                MSG_UYGULAMA_ISI.format(n=isler))
            streamlit_acikti = self.streamlit.calisiyor()
            if streamlit_acikti:
                self.streamlit.durdur()
            try:
                sonuc = kur.kaldir(kimlik)
            except kurulum.KurulumHatasi as exc:
                raise _kurulum_hatasi(exc)
            finally:
                self._diskte_onbellek = None
                self._planlar.clear()
                if streamlit_acikti and not self._kapaniyor.is_set():
                    threading.Thread(target=self._streamlit_yeniden, name="streamlit-yeniden",
                                     daemon=True).start()
            logger.info("bileşen kaldırıldı: %s (%s bayt)", kimlik, sonuc.get("silinen_bayt"))
        silinen = _tamsayi(sonuc.get("silinen_bayt"))
        return {"bilesen": kimlik, "kaldirildi": True, "silinen_bayt": silinen,
                "silinen_metni": bicim_boyut_hassas(silinen)}

    # ======================================================================
    # Uygulama (Streamlit) ve kapanış
    # ======================================================================
    def _streamlit_icin_url(self) -> str:
        """``KITAPCEVIR_YONETICI_URL``: kenar çubuğundaki "Bileşenler" bağlantısı (F4)."""
        return f"{self.url}&kip=bilesenler" if self.url else ""

    def uygulama_baslat(self, govde: Mapping[str, Any]) -> Dict[str, Any]:
        """``POST /api/uygulama``: zorunlular kuruluysa Streamlit'i başlatır, adresini döner.

        (Yöntem adı ``uygulama`` DEĞİL: ``self.uygulama`` uygulama kodunun
        klasörüdür ve örnek özniteliği yöntemi gölgelerdi.)
        """
        kur = self._kur_gerekli()
        if kur.calisiyor:
            raise ApiHatasi(HTTPStatus.CONFLICT, kurulum.KOD_IS_SURUYOR, MSG_IS_SURUYOR)
        if not kur.uygulama_hazir():
            gorunum = self.durum({})
            raise ApiHatasi(HTTPStatus.CONFLICT, "hazir_degil", gorunum["hazir_degil_nedeni"])
        try:
            url = self.streamlit.baslat(self._streamlit_icin_url())
        except surecler.StreamlitHatasi as exc:
            raise ApiHatasi(HTTPStatus.SERVICE_UNAVAILABLE, "streamlit", str(exc),
                            gunluk=str(self.gunluk_dizini / "streamlit.log"))
        try:
            self.calisan_yaz()
        except OSError:
            logger.exception("calisan.json yazılamadı")
        return {"url": url}

    def kapat(self, govde: Mapping[str, Any]) -> Tuple[int, Dict[str, Any]]:
        self.kapanis_baslat()
        return HTTPStatus.ACCEPTED, {"kapaniyor": True,
                                     "kapanis_suresi_sn": ayarlar.KAPANIS_SURESI_SN}

    # ======================================================================
    # Dış açıcı, güncelleme, TeX sınaması, yetimler, tema
    # ======================================================================
    def ac(self, govde: Mapping[str, Any]) -> Dict[str, Any]:
        """Yalnız bilinen hedefler açılır; sayfa adres ya da yol veremez."""
        hedef = govde.get("hedef")
        if hedef == "kaynak_kodu":
            yer = ayarlar.KAYNAK_KODU_URL
        elif hedef == "surumler":
            yer = ayarlar.SURUMLER_SAYFASI_URL
        elif hedef == "gunlukler":
            self.gunluk_dizini.mkdir(parents=True, exist_ok=True)
            yer = str(self.gunluk_dizini)
        elif hedef == "gunluk":
            yer = self._is.gunluk if self._is is not None else ""
            if not yer:
                raise ApiHatasi(HTTPStatus.NOT_FOUND, "gunluk_yok", MSG_GUNLUK_YOK)
        elif hedef == "lisanslar":
            # Ana lisans metinleri uygulama arşivinin LICENSES/'inde (GPL, KAYNAK_KODU.txt,
            # Datalab, Python ve üçüncü taraf bildirimleri, 00_OKU.txt). Kökteki LICENSES/
            # yalnız HY-MT kurulunca oluşur ve Tencent kopyasını taşır (K9 Y2).
            adaylar = (self.uygulama / durum.LISANSLAR_DIZINI, self.duzen.lisanslar)
            yer = str(next((a for a in adaylar if a.is_dir()), self.uygulama))
        elif hedef == "lisans_tam_metin":
            b = self._bilesen_bul(govde.get("bilesen"))
            alan = b.get("lisans") if isinstance(b.get("lisans"), Mapping) else {}
            yer = str(alan.get("metin_url") or "")
            if not yer.startswith("https://"):
                raise ApiHatasi(HTTPStatus.NOT_FOUND, "adres_yok", MSG_ADRES_YOK)
        else:
            raise ApiHatasi(HTTPStatus.BAD_REQUEST, "hedef", MSG_BILINMEYEN_HEDEF)
        self._acici(yer)
        return {"acildi": True}

    def guncelleme(self, govde: Mapping[str, Any]) -> Dict[str, Any]:
        """Yalnız "Denetle" düğmesiyle (G.4); erişilemezse sessizce "denetlenemedi"."""
        surum = self._surum()
        try:
            sonuc = self._guncelleme_denetle(surum)
        except Exception:  # noqa: BLE001 — denetim hata sayılmaz
            logger.exception("güncelleme denetimi")
            sonuc = {"durum": "denetlenemedi"}
        durum_ = sonuc.get("durum")
        if durum_ == "yeni":
            return {"durum": "yeni", "surum": sonuc.get("son_surum"),
                    "baslik": sonuc.get("baslik") or "",
                    "ileti": MSG_GUNCELLEME_VAR.format(surum=sonuc.get("son_surum"))}
        if durum_ == "guncel":
            return {"durum": "guncel", "surum": surum,
                    "ileti": MSG_GUNCELLEME_YOK.format(surum=surum)}
        return {"durum": "denetlenemedi", "ileti": MSG_GUNCELLEME_DENETLENEMEDI}

    def tex_sina(self, govde: Mapping[str, Any]) -> Dict[str, Any]:
        """Kurulu TeX'i şablonla sınar (sayfa "şablon sınanıyor…" gösterirken)."""
        kur = self._kur_gerekli()
        yol = govde.get("yol")
        if not any(a.get("yol") == yol for a in kur.tex_algilanan()):
            raise ApiHatasi(HTTPStatus.BAD_REQUEST, "tex_yolu", MSG_TEX_ALGILANMADI)
        sina = getattr(kur, "tex_sina", None)
        liste = sina() if callable(sina) else kur.tex_algilanan()
        aday = next((a for a in liste if a.get("yol") == yol), {})
        logger.info("kurulu TeX sınaması (%s): %s", yol, aday.get("sinama"))
        return {"yol": yol, "sinama": aday.get("sinama")}

    def yetimleri_durdur(self, govde: Mapping[str, Any]) -> Dict[str, Any]:
        istenen = govde.get("pidler")
        if not isinstance(istenen, list):
            raise ApiHatasi(HTTPStatus.BAD_REQUEST, "pidler", "'pidler' liste olmalı.")
        bilinen = {y["pid"] for y in self._yetimler}
        secilen = [p for p in istenen if isinstance(p, int) and p in bilinen]
        kalan = surecler.surecleri_durdur(secilen)
        self._yetimler = [y for y in self._yetimler
                          if y["pid"] in kalan or y["pid"] not in secilen]
        logger.info("önceki oturumdan kalan süreçler durduruldu: %s (kalan %s)", secilen, kalan)
        return {"durdurulan": [p for p in secilen if p not in kalan], "kalan": kalan}

    def tema_css(self) -> str:
        return tema_css(self.uygulama)

    # ======================================================================
    # "Kitapçevir'i kaldır…" (TASARIM G.5)
    # ======================================================================
    def _kaldirma_koku_gecerli(self) -> bool:
        """Kök bizim kökümüz mü: adı "Kitapçevir", içinde durum/ var, ev ya da / değil."""
        kok = self.duzen.kok
        ad = unicodedata.normalize("NFC", kok.name)
        return (ad == unicodedata.normalize("NFC", KALDIRMA_KOK_ADI)
                and self.duzen.durum_dizini.is_dir()
                and kok not in (Path("/"), self._ev) and len(kok.parts) > 2)

    def _paket_klasorleri(self) -> List[Path]:
        kimlik = paket_kimligi(self._paket_yolu)
        if not kimlik:
            return []
        return [self._ev / sablon.format(kimlik=kimlik) for sablon in PAKET_KLASOR_SABLONLARI]

    def _gunluk_dosyalari(self) -> List[Path]:
        dosyalar: List[Path] = []
        for desen in GUNLUK_DOSYA_DESENLERI:
            dosyalar.extend(p for p in self.gunluk_dizini.glob(desen) if p.is_file())
        return sorted(set(dosyalar))

    def _kaldirma_engeli(self) -> str:
        if self._kaldirildi is not None:
            return MSG_KALDIRILDI
        if not self._kaldirma_koku_gecerli():
            return MSG_KALDIRMA_KOK.format(kok=_ev_kisalt(self.duzen.kok))
        if self._is_suruyor():
            return MSG_IS_SURUYOR
        isler = self._calisan_is_sayisi()
        if isler:
            return MSG_UYGULAMA_ISI.format(n=isler)
        return ""

    def _veride_bilesenler(self) -> List[Dict[str, Any]]:
        """``veri/`` altına kurulmuş sihirbaz bileşenleri (HY-MT ``veri/models``'e iner).

        Varsayılan kaldırma ``veri/``'yi korur; bu bileşenler de onunla kalır (K9 O1).
        Kurulum kaydındaki klasör ve dosyalardan (``yollar``, ``dosyalar``) yalnız
        ``veri/`` altında olup diskte duranlar sayılır; boyut gerçek ``du`` ölçümüdür.
        """
        veri = self.duzen.veri
        sonuc: List[Dict[str, Any]] = []
        for kimlik, kayit in self._kurulu_kayitlar().items():
            if not isinstance(kayit, Mapping):
                continue
            yollar = []
            for goreli in list(kayit.get("yollar") or []) + list(kayit.get("dosyalar") or {}):
                try:
                    yol = self.duzen.kok_yolu(str(goreli))
                except ValueError:
                    continue
                if veri in yol.parents and yol.exists() and yol not in yollar:
                    yollar.append(yol)
            if not yollar:
                continue
            try:
                ad = str(self._bilesen_bul(kimlik).get("ad") or kimlik)
            except ApiHatasi:
                ad = str(kimlik)
            bayt = sum(disk.disk_kullanimi_bayt(y) for y in _alt_yollari_ayikla(yollar))
            ortak = Path(os.path.commonpath([str(y) for y in yollar]))
            sonuc.append({"kimlik": str(kimlik), "ad": ad, "bayt": bayt,
                          "metin": bicim_boyut_hassas(bayt), "yol_metni": _ev_kisalt(ortak)})
        return sorted(sonuc, key=lambda b: b["kimlik"])

    def kaldirma_plani(self, sorgu: Mapping[str, str]) -> Dict[str, Any]:
        """``GET /api/kaldirma_plani``: silinecek her klasör gerçek ``du`` boyutuyla."""
        ogeler: List[Dict[str, Any]] = []

        def ekle(tur: str, yol: Path, bayt: int) -> None:
            ogeler.append({"tur": tur, "yol_metni": _ev_kisalt(yol), "bayt": bayt,
                           "metin": bicim_boyut_hassas(bayt)})

        kok = self.duzen.kok
        veri = self.duzen.veri
        if kok.is_dir():
            for cocuk in sorted(kok.iterdir(), key=lambda p: p.name):
                if cocuk == veri:
                    continue
                ekle("kok", cocuk, disk.disk_kullanimi_bayt(cocuk))
        gunlukler = self._gunluk_dosyalari()
        if gunlukler:
            ekle("gunluk", self.gunluk_dizini,
                 sum(disk.disk_kullanimi_bayt(p) for p in gunlukler))
        for yol in self._paket_klasorleri():
            if yol.exists():
                ekle("paket", yol, disk.disk_kullanimi_bayt(yol))
        veri_bayt = disk.disk_kullanimi_bayt(veri) if veri.exists() else 0
        tex = (durum.tex_oku(self.duzen) or {}).get("tur")
        kayitlar = self._kurulu_kayitlar() if self.kur is not None else {}
        engel = self._kaldirma_engeli()
        return {
            "izin": not engel, "neden": engel, "ogeler": ogeler,
            "silinecek_bayt": sum(o["bayt"] for o in ogeler),
            "silinecek_metni": bicim_boyut_hassas(sum(o["bayt"] for o in ogeler)),
            "veri": {"var": veri.exists(), "yol_metni": _ev_kisalt(veri), "bayt": veri_bayt,
                     "metin": bicim_boyut_hassas(veri_bayt)},
            # veri/ ile korunan sihirbaz bileşenleri (HY-MT); sayfa ayrı kutuyla sorar.
            "veride_bilesenler": self._veride_bilesenler() if self.kur is not None else [],
            "mactex": tex == TEX_TAM or TEX_TAM in kayitlar,
        }

    def tumunu_kaldir(self, govde: Mapping[str, Any]) -> Dict[str, Any]:
        """``POST /api/tumunu_kaldir {onay: true, veri_de_sil: bool, bilesenleri_de_sil?: [..]}``.

        Kök altındaki her şey silinir; ``veri/`` (kitaplar, çeviri belleği, modeller)
        yalnız ``veri_de_sil`` ile. ``veri/`` korunurken içindeki sihirbaz bileşenleri
        (HY-MT; planın ``veride_bilesenler``'i) ``bilesenleri_de_sil`` listesiyle ayrıca
        silinebilir (K9 O1). Çalışan kod, Python ve kilit (``KALDIRMADA_SONA_KALANLAR``),
        günlükler ve paket klasörleri çıkışta silinir. MacTeX sistem geneline kurulu
        olduğu için kaldırılmaz.
        """
        if govde.get("onay") is not True:
            raise ApiHatasi(HTTPStatus.BAD_REQUEST, "onay", MSG_KALDIRMA_ONAY)
        veri_de_sil = govde.get("veri_de_sil")
        if not isinstance(veri_de_sil, bool):
            raise ApiHatasi(HTTPStatus.BAD_REQUEST, "veri_de_sil",
                            "'veri_de_sil' true ya da false olmalı.")
        istenen = govde.get("bilesenleri_de_sil", [])
        if not isinstance(istenen, list) or not all(isinstance(k, str) for k in istenen):
            raise ApiHatasi(HTTPStatus.BAD_REQUEST, "bilesenleri_de_sil",
                            "'bilesenleri_de_sil' bileşen kimliklerinin listesi olmalı.")
        with self._kilit:
            engel = self._kaldirma_engeli()
            if engel:
                raise ApiHatasi(HTTPStatus.CONFLICT, "kaldirilamaz", engel)
            veride = {b["kimlik"]: b for b in
                      (self._veride_bilesenler() if self.kur is not None else [])}
            yabanci = sorted(set(istenen) - set(veride))
            if yabanci:
                raise ApiHatasi(HTTPStatus.BAD_REQUEST, "bilesenleri_de_sil",
                                "Veri klasöründe kurulu olmayan bileşen: " + ", ".join(yabanci))
            if self.streamlit.calisiyor():
                self.streamlit.durdur()
            kok = self.duzen.kok
            once = disk.disk_kullanimi_bayt(kok)
            silinmeyen: List[str] = []
            veride_silinenler: List[str] = []
            if not veri_de_sil:
                for kimlik in sorted(set(istenen)):
                    try:
                        self.kur.kaldir(kimlik)
                        veride_silinenler.append(kimlik)
                    except (kurulum.KurulumHatasi, OSError) as exc:
                        logger.error("kaldırmada silinemedi: %s (%s)", kimlik, exc)
                        silinmeyen.append(veride[kimlik]["yol_metni"])
            for cocuk in sorted(kok.iterdir(), key=lambda p: p.name):
                if cocuk.name in KALDIRMADA_SONA_KALANLAR:
                    continue
                if cocuk == self.duzen.veri and not veri_de_sil:
                    continue
                try:
                    _yolu_sil(cocuk)
                except OSError as exc:
                    logger.error("kaldırmada silinemedi: %s (%s)", cocuk, exc)
                    silinmeyen.append(_ev_kisalt(cocuk))
            sonra = disk.disk_kullanimi_bayt(kok)
            self._kaldirildi = {"veri_de_sil": veri_de_sil}
            self._planlar.clear()
            logger.warning("Kitapçevir kaldırıldı (veri %s); kalanlar çıkışta silinecek",
                           "silindi" if veri_de_sil else "korundu")
        veri_var = self.duzen.veri.exists()
        kalanlar = [b for k, b in sorted(veride.items())
                    if veri_var and k not in veride_silinenler]
        return {"kaldirildi": True, "silinen_bayt": max(0, once - sonra),
                "silinen_metni": bicim_boyut_hassas(max(0, once - sonra)),
                "veri_korundu": veri_var, "veri_yolu_metni": _ev_kisalt(self.duzen.veri),
                "veride_silinenler": veride_silinenler, "veride_kalanlar": kalanlar,
                "silinmeyen": silinmeyen}

    def _kaldirma_son_temizlik(self) -> None:
        """Kapanışın sonu: çalışan kod, kilit, günlükler ve paket klasörleri silinir."""
        kok = self.duzen.kok
        for ad in KALDIRMADA_SONA_KALANLAR:
            try:
                _yolu_sil(kok / ad)
            except OSError as exc:
                logger.error("kaldırmada silinemedi: %s (%s)", kok / ad, exc)
        for yol in self._paket_klasorleri():
            try:
                if yol.exists() or yol.is_symlink():
                    _yolu_sil(yol)
            except OSError as exc:
                logger.error("kaldırmada silinemedi: %s (%s)", yol, exc)
        # Günlük işleyicileri kapatılır, sonra yalnız bizim günlük dosyalarımız silinir.
        for ad in ("kitapcevir", "kitapcevir.yonetici", "kitapcevir.streamlit"):
            gunlukcu = logging.getLogger(ad)
            for isleyici in list(gunlukcu.handlers):
                if isinstance(isleyici, logging.FileHandler):
                    gunlukcu.removeHandler(isleyici)
                    isleyici.close()
        for dosya in self._gunluk_dosyalari():
            dosya.unlink(missing_ok=True)
        for klasor in (self.gunluk_dizini, kok):
            try:
                klasor.rmdir()      # yalnız boşsa (veri korunduysa kök kalır)
            except OSError:
                pass

    # ======================================================================
    # Başlıksız kip (sürüm betiğinin ölçüm koşusu; TASARIM G.1-7)
    # ======================================================================
    def basliksiz_kur(self, kur: str, lisans_kabul: str,
                      yaz: Callable[[str], None] = print) -> int:
        """``--kur zorunlu,tex-hafif,gguf``: planı kurar, olayları JSON satırı basar.

        Çıkış: 0 tamam, 1 hata, 2 bilinmeyen bileşen, 3 lisans onayı eksik,
        4 disk yetersiz, 5 manifest, 130 iptal. ``--lisans-kabul`` komut
        satırı kullanıcısının açık onayıdır (sürüm betiği).
        """
        def _json(veri: Mapping[str, Any]) -> None:
            yaz(json.dumps(dict(veri), ensure_ascii=False))

        if self.kur is None:
            _json({"hata": MSG_MANIFEST_YOK.format(neden=self.manifest_hatasi)})
            return 5
        tex, istege = None, []
        for parca in [p.strip() for p in kur.split(",") if p.strip()]:
            if parca == ZORUNLU:
                continue
            try:
                b = self.kur.bilesen(parca)
            except kurulum.PlanHatasi as exc:
                _json({"hata": exc.ileti})
                return 2
            if b.get("grup") == TEX_GRUBU:
                tex = parca
            elif b.get("zorunluluk") == ISTEGE_BAGLI:
                istege.append(parca)
        for kimlik in [p.strip() for p in (lisans_kabul or "").split(",") if p.strip()]:
            try:
                self.lisans({"bilesen": kimlik, "kabul": True})
            except ApiHatasi as exc:
                _json(exc.sozluk())
                return 3
        try:
            plan = self.plan({"tex": tex, "istege_bagli": istege})
        except ApiHatasi as exc:
            _json(exc.sozluk())
            return 1
        if plan["eksik_onaylar"]:
            _json({"hata": "lisans onayı eksik",
                   "bilesenler": [e["bilesen"] for e in plan["eksik_onaylar"]]})
            return 3
        if not plan["yeter"]:
            _json({"hata": kurulum.MSG_DISK.format(eksik=plan["eksik_bayt"])})
            return 4
        if plan["bos"] and not plan["yalniz_tex"]:
            _json({"bilgi": "Kurulacak bileşen yok: seçilenlerin hepsi kurulu."})
            return 0
        kuyruk = self.yayin.abone_ol()
        sonuc = SONUC_HATA
        try:
            try:
                self.kur_baslat(plan["plan_kimligi"])
            except ApiHatasi as exc:
                _json(exc.sozluk())
                return 1
            while True:
                # Zaman aşımlı bekleme: Ctrl+C / SIGTERM olay gelmeyen uzun adımda
                # (ör. uv kurulumu) da en geç BASLIKSIZ_YOKLAMA_SN içinde işlenir.
                try:
                    olay = kuyruk.get(timeout=BASLIKSIZ_YOKLAMA_SN)
                except queue.Empty:
                    if self.kur.calisiyor or not kuyruk.empty():
                        continue
                    logger.error("kurulum işi 'bitti' olayı yaymadan sona erdi")
                    break
                if olay is None:
                    break
                _, tur, veri = olay
                _json({"olay": tur, **{k: v for k, v in veri.items() if k != "gorunum"}})
                if tur == OLAY_BITTI:
                    sonuc = veri.get("sonuc", SONUC_HATA)
                    break
        except KeyboardInterrupt:
            # Ctrl+C ya da SIGTERM (main() başlıksız kipte ikisini de buraya getirir):
            # iş iptal edilir, .part dosyaları sürdürülmek üzere kalır.
            self.kur.iptal()
            sonuc = SONUC_IPTAL
            _json({"bilgi": kurulum.MSG_IPTAL})
        finally:
            self.yayin.ayril(kuyruk)
        self.kur.bekle(IS_BEKLEME_KAPANISTA_SN if sonuc == SONUC_IPTAL else None)
        return {SONUC_TAMAM: 0, SONUC_IPTAL: 130}.get(sonuc, 1)


# ---------------------------------------------------------------------------
# Günlük ve kilit
# ---------------------------------------------------------------------------
_GUNLUK_BICIMI = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def gunlukleri_kur(gunluk_dizini: Path) -> List[logging.Handler]:
    """``yonetici.log`` ve ``streamlit.log``: RotatingFileHandler (TASARIM B.6)."""
    gunluk_dizini.mkdir(parents=True, exist_ok=True)
    bicim = logging.Formatter(_GUNLUK_BICIMI)
    isleyiciler = []
    for ad, dosya in (("kitapcevir.yonetici", "yonetici.log"),
                      ("kitapcevir.streamlit", "streamlit.log")):
        isleyici = logging.handlers.RotatingFileHandler(
            gunluk_dizini / dosya, maxBytes=ayarlar.GUNLUK_BOYUT_BAYT,
            backupCount=max(0, ayarlar.GUNLUK_ADET - 1), encoding="utf-8")
        isleyici.setFormatter(bicim)
        gunlukcu = logging.getLogger(ad)
        gunlukcu.addHandler(isleyici)
        gunlukcu.setLevel(logging.INFO)
        isleyiciler.append(isleyici)
    logging.getLogger("kitapcevir").setLevel(logging.INFO)
    logging.getLogger("kitapcevir.streamlit").propagate = False
    return isleyiciler


def kilit_al(duzen: durum.KokDuzeni) -> Optional[int]:
    """``durum/kilit`` üstünde ``flock(LOCK_EX | LOCK_NB)``; alınamazsa None (B.6)."""
    duzen.durum_dizini.mkdir(parents=True, exist_ok=True)
    fd = os.open(duzen.kilit_dosyasi, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            return None
        raise
    return fd


def kurulum_hazirla(duzen: durum.KokDuzeni, uygulama: Path, manifest_yolu: Path,
                    gunluk_dizini: Path) -> Tuple[Optional[YoneticiKurulumu], str]:
    """Manifesti (K2 doğrulayıcısıyla) okur, K3 kurulumunu K2 indiricisiyle kurar.

    Manifest okunamazsa ``(None, neden)``: sayfa "Kurulum başlatılamadı" der.
    """
    try:
        belge = manifest.yukle(manifest_yolu)
    except manifest.ManifestHatasi as exc:
        # İleti ilk on sorunu zaten içerir (K2); bütün liste yalnız günlüğe yazılır.
        neden = str(exc)
        logger.error("manifest yüklenemedi: %s", neden)
        for sorun in getattr(exc, "sorunlar", None) or []:
            logger.error("manifest sorunu: %s", sorun)
        return None, neden
    uyarlayici = IndiriciUyarlayici(belge.surum)
    kur = YoneticiKurulumu(duzen.kok, uygulama, belge.veri, indirici=uyarlayici,
                           gunluk_yolu=gunluk_dizini)
    return kur, ""


# ---------------------------------------------------------------------------
# Giriş noktası
# ---------------------------------------------------------------------------
def _arguman_ayristir(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    ay = argparse.ArgumentParser(prog="yonetici.py", description="Kitapçevir yöneticisi")
    ay.add_argument("--kok", required=True, help="kurulum kökü (mutlak yol)")
    ay.add_argument("--port", type=int, default=0, help="dinlenecek kapı (0: boş kapı)")
    ay.add_argument("--uygulama", help="uygulama kodu (varsayılan: bu dosyanın deposu)")
    ay.add_argument("--manifest", help="manifest.json (varsayılan: <uygulama>/packaging/)")
    ay.add_argument("--gunluk", help="günlük klasörü (varsayılan: KITAPCEVIR_GUNLUK_DIZINI "
                                     "ya da ~/Library/Logs/Kitapçevir)")
    ay.add_argument("--ebeveyn-pid", type=int, help="bu süreç ölünce yönetici kapanır")
    ay.add_argument("--basliksiz", action="store_true", help="sunucusuz kurulum (sürüm ölçümü)")
    ay.add_argument("--kur", default="", help="başlıksız kipte: zorunlu,tex-hafif,gguf …")
    ay.add_argument("--lisans-kabul", default="", help="başlıksız kipte kabul edilen lisanslar")
    return ay.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _arguman_ayristir(argv)
    kok = Path(args.kok).expanduser()
    if not kok.is_absolute():
        print(json.dumps({"hata": "--kok mutlak yol olmalı"}, ensure_ascii=False), file=sys.stderr)
        return 2
    duzen = durum.KokDuzeni(kok)
    kilit = kilit_al(duzen)
    if kilit is None:
        onceki = surecler.calisan_oku(duzen) or {}
        print(json.dumps({"url": onceki.get("url"), "kapanis_suresi_sn": ayarlar.KAPANIS_SURESI_SN,
                          "zaten_calisiyor": True}, ensure_ascii=False), flush=True)
        return ayarlar.KILIT_DOLU_CIKIS_KODU
    try:
        gunluk_dizini = Path(args.gunluk).expanduser() if args.gunluk else varsayilan_gunluk_dizini()
        gunlukleri_kur(gunluk_dizini)
        uygulama = (Path(args.uygulama).expanduser() if args.uygulama
                    else Path(__file__).resolve().parents[2])
        manifest_yolu = (Path(args.manifest).expanduser() if args.manifest
                         else uygulama / MANIFEST_GORELI)
        kur, neden = kurulum_hazirla(duzen, uygulama, manifest_yolu, gunluk_dizini)
        yonetici = Yonetici(duzen, uygulama, kur, gunluk_dizini=gunluk_dizini,
                            manifest_hatasi=neden, onceki_calisan=surecler.calisan_oku(duzen))
        logger.info("yönetici açıldı: kök %s, uygulama %s, pid %s", kok, uygulama, os.getpid())
        if args.basliksiz:
            # SIGTERM de Ctrl+C gibi işi iptal etsin (varsayılan eylem süreci öldürürdü).
            signal.signal(signal.SIGTERM, signal.default_int_handler)
            return yonetici.basliksiz_kur(args.kur, args.lisans_kabul)
        anahtar = secrets.token_hex(ayarlar.ANAHTAR_BAYT)
        sunucu_ = sunucu.Sunucu(yonetici, anahtar, kapi=args.port, yayin=yonetici.yayin)
        url = sunucu_.adres()
        yonetici.baglan(url, lambda: threading.Thread(target=sunucu_.kapat, name="sunucu-kapat",
                                                      daemon=True).start())
        yonetici.calisan_yaz()
        for sinyal in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sinyal, lambda *_: yonetici.kapanis_baslat())
        print(json.dumps({"url": url, "kapanis_suresi_sn": ayarlar.KAPANIS_SURESI_SN},
                         ensure_ascii=False), flush=True)
        yonetici.arka_isleri_baslat(args.ebeveyn_pid)
        try:
            sunucu_.calistir()
        finally:
            yonetici.kapanis()
            sunucu_.kapat()
        logger.info("yönetici kapandı")
        return 0
    finally:
        os.close(kilit)


__all__ = ["IndiriciUyarlayici", "IsGorunumu", "Yonetici", "YoneticiKurulumu", "diskte_olc",
           "gunlukleri_kur", "kilit_al", "kurulum_hazirla", "main", "tema_css"]

if __name__ == "__main__":  # python -m yonetici.yonetici
    sys.exit(main())
