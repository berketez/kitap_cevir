"""İndirme yöneticisi: .part + Range ile sürdürme, akışta özet, gerçek bayt ilerlemesi.

TASARIM D (D.2 öğe durum makinesi, D.3 MacTeX), lisans raporu §8.1 K2–K4.
Yalnız stdlib: DMG'deki taşınabilir Python'la çalışır.

Öğe başına akış (D.2)::

    BEKLIYOR → (hedef yerinde + özet doğru) → DOGRULANDI            kaynak "yerinde"
    BEKLIYOR → (yeniden_kullan eşleşti) → KLONLANDI → DOGRULANDI    kaynak "klon"/"kopya"
    BEKLIYOR → INIYOR ⇄ YENIDEN_DENE → DOGRULANIYOR → DOGRULANDI    kaynak "ag"
                  └→ IPTAL (.part kalır)   └→ SHA_UYUSMADI (.part silinir, 1 kez baştan) → HATA

Kurallar:

- ``.part`` yeri ``disk.part_yolu``: ``<kök>/indirilen/<kimlik>.<sha öneki>.part``.
  Bitince ``os.replace`` ile ``hedef``e taşınır.
- Sürdürme: ``Range: bytes=<boy>-``. 206 + doğru ``Content-Range`` → eklenir;
  200 → sunucu Range'i yok saydı, dosya baştan yazılır; 416 → ``.part``
  silinip baştan.
- Özet akış içinde hesaplanır; sürdürmede önce var olan ``.part`` okunur.
- Yönlendirmeler elle izlenir: her adımda ``Range`` korunur, HF ``resolve``
  yanıtındaki ``X-Linked-Size`` / ``X-Linked-ETag`` (LFS'te SHA-256)
  manifestle karşılaştırılır; uyuşmazsa indirme başlamadan "kaynak değişmiş".
  Çözümlenen (imzalı CDN) adres önbelleğe alınır; 4xx (süresi dolmuş imza:
  403/410) gelince manifest adresi yeniden çözümlenir ve Range ile sürülür.
  https'ten http'ye düşen yönlendirme reddedilir.
- Yeniden deneme: bağlantı hatası, 5xx, erken kopma → üstel bekleme; 429 →
  ``Retry-After`` ya da ``RateLimit`` başlığındaki ``t`` kadar (gerçek geri
  sayım olayı). Gerçek ilerleme olan her denemeden sonra sayaç sıfırlanır.
  Deneme hakkı biterse iş "duraklatıldı" olur (bütün iş durur, ``.part``
  kalır): ağ yoksa her öğe ayrı ayrı beklemesin.
- İptal: parça (``PARCA_BAYT``) başına bayrağa bakılır; ayrıca açık yanıtlar
  kapatılır ki takılmış okuma beklenmesin. ``.part`` kalır.
- Disk: indirme başlamadan boş alan ≥ kalan bayt + ``DISK_PAYI_BAYT`` denetlenir
  (``disk_yetersiz``; iş hiç başlamaz). Yine de ENOSPC/EDQUOT gelirse bütün iş
  durur, ``.part`` kalır (``disk_dolu``).
- Yedek adresler: 404/403 gibi öğe hatasında ya da sunucuya özgü iş durduran
  hatada (5xx, istek sınırı, bağlantı hakkı bitti) sıradaki ``yedek_urller``
  denenir; ``.part`` aynı içeriğe ait olduğundan yedekte de sürdürülür.
- Eşzamanlılık: ``BUYUK_OGE_ESIGI_BAYT`` altı öğeler ``IS_PARCACIGI_KUCUK``,
  üstü ``IS_PARCACIGI_BUYUK`` iş parçacığıyla, iki havuz aynı anda.
- İlerleme yalnız gerçek baytla: öğe için diskteki ``.part`` boyu; genel
  için öğelerin toplamı. Hız son ``HIZ_PENCERESI_SN`` saniyede ağdan gelen
  bayttan ölçülür; ölçülemiyorsa ``None`` ("—"). Kalan süre yalnız hız
  ölçülünce verilir (arayüz "tahmini" diye yazar).

Olaylar (``olay(ad, veri)``; çağrılar tek kilitle sıralanır, alıcı iş
parçacığı güvenli olmak zorunda değil, ama hızlı dönmeli):

- ``ilerleme``: ``{bilesen, oge, bayt, toplam_bayt, hiz_bayt_sn, genel_bayt,
  genel_toplam_bayt, kalan_sn_tahmini}`` (``hiz_bayt_sn`` genel hızdır)
- ``oge_durumu``: ``{bilesen, oge, durum, kaynak?, hata?}``
- ``bekleme``: ``{bilesen, oge, neden: "yeniden_deneme"|"hiz_siniri", kalan_sn, deneme}``

``hata``/``adim_*``/``bitti`` olaylarını kurulum düzenleyicisi (K3) üretir;
indirici hatayı ``IndirmeSonucu`` içinde döndürür. K3'ün ``kurulum.Kurulum``'u
çağrılabilir bir indirici bekler (``indirici(ogeler, istek) -> None``); tek
uyarlayıcı ``kurulum.IndiriciUyarlayici``'dir (iş başına tek ``Indirici``;
yönetici aynı sınıfı ``yonetici.IndiriciUyarlayici`` adıyla kullanır).
"""
from __future__ import annotations

import errno
import hashlib
import http.client
import json
import logging
import math
import os
import re
import shutil
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlsplit, urlunsplit

try:
    from . import ayarlar, disk
except ImportError:  # düz betik olarak yüklendi (sys.path[0] = packaging/yonetici)
    import ayarlar  # type: ignore[no-redef]
    import disk  # type: ignore[no-redef]

logger = logging.getLogger("kitapcevir.yonetici.indirici")

# ---------------------------------------------------------------------------
# Durumlar ve hata kodları (sözleşme: K3 kurulum, K4 arayüz)
# ---------------------------------------------------------------------------
BEKLIYOR = "BEKLIYOR"
INIYOR = "INIYOR"
YENIDEN_DENE = "YENIDEN_DENE"
DOGRULANIYOR = "DOGRULANIYOR"
DOGRULANDI = "DOGRULANDI"
KLONLANDI = "KLONLANDI"
IPTAL = "IPTAL"
SHA_UYUSMADI = "SHA_UYUSMADI"
HATA = "HATA"

KAYNAK_AG = "ag"
KAYNAK_KLON = "klon"
KAYNAK_KOPYA = "kopya"
KAYNAK_YERINDE = "yerinde"

IS_TAMAM = "tamam"
IS_IPTAL = "iptal"
IS_DURAKLATILDI = "duraklatildi"
IS_HATA = "hata"

HATA_IPTAL = "iptal"
HATA_DISK_DOLU = "disk_dolu"
HATA_DISK_YETERSIZ = "disk_yetersiz"      # ön denetim: indirme hiç başlamadı (K3 KOD_DISK ile aynı)
HATA_BAGLANTI = "baglanti"
HATA_SUNUCU = "sunucu"
HATA_HIZ_SINIRI = "hiz_siniri"
HATA_TLS = "tls"
HATA_SHA = "sha_uyusmadi"
HATA_KAYNAK_DEGISTI = "kaynak_degisti"
HATA_ERISIM = "erisim_reddedildi"
HATA_BULUNAMADI = "bulunamadi"
HATA_YANIT = "gecersiz_yanit"
HATA_HEDEF = "gecersiz_hedef"
HATA_YAZMA = "yazma"
HATA_BEKLENMEYEN = "beklenmeyen"

NEDEN_YENIDEN_DENEME = "yeniden_deneme"
NEDEN_HIZ_SINIRI = "hiz_siniri"

# Bütün işi durduran ama yalnız o sunucuya özgü olabilen hatalar: öğenin yedek
# adresi varsa önce o denenir.
_ADRESE_OZGU_HATALAR = frozenset({HATA_SUNUCU, HATA_HIZ_SINIRI, HATA_BAGLANTI})

_OZET_ADI = {"sha256": "SHA-256"}

_HF_ALANI = "huggingface.co"
_YONLENDIRME_KODLARI = (301, 302, 303, 307, 308)
_CONTENT_RANGE = re.compile(r"^\s*bytes\s+(\d+)-(\d+)/(\d+|\*)\s*$", re.I)
_RATELIMIT_T = re.compile(r"(?:^|[;,\s])t=(\d+)")
_RATELIMIT_RESET = re.compile(r"(?:^|[;,\s])reset=(\d+)")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_AG_YOK_ERRNO = frozenset({errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN,
                           errno.ECONNREFUSED, errno.ETIMEDOUT, errno.EHOSTDOWN})
# Sistem anahtar zinciri (TASARIM D.2-10): kurumsal kökler + Apple kökleri.
_ANAHTAR_ZINCIRLERI = ("/Library/Keychains/System.keychain",
                       "/System/Library/Keychains/SystemRootCertificates.keychain")
_SECURITY = "/usr/bin/security"


# ---------------------------------------------------------------------------
# Hata ve sonuç türleri
# ---------------------------------------------------------------------------
class IndirmeHatasi(Exception):
    """Kullanıcıya gösterilebilir indirme hatası (``ileti`` Türkçe).

    ``tum_isi_durdur``: bu hata yalnız öğeyi değil bütün işi durdurur (disk
    dolu, bağlantı yok, sunucu/istek sınırı tükendi, TLS, iptal).
    """

    def __init__(self, kod: str, ileti: str, *, ayrinti: str = "",
                 tum_isi_durdur: bool = False, veri: Optional[Mapping[str, Any]] = None):
        super().__init__(ileti)
        self.kod = kod
        self.ileti = ileti
        self.ayrinti = ayrinti
        self.tum_isi_durdur = tum_isi_durdur
        # Arayüzün kendi biçimleyeceği sayılar (ör. disk ön denetiminde eksik_bayt).
        self.veri: Dict[str, Any] = dict(veri or {})

    def sozluk(self) -> Dict[str, Any]:
        return {"kod": self.kod, "ileti": self.ileti, "ayrinti": self.ayrinti,
                "tum_isi_durdur": self.tum_isi_durdur, "veri": dict(self.veri)}


class _Durduruldu(Exception):
    """İç işaret: iş durdurma bayrağı kalktı (iptal ya da başka öğenin iş durduran hatası)."""


class _HttpHatasi(Exception):
    def __init__(self, kod: int, url: str, basliklar: Mapping[str, str], yonlendirildi: bool):
        super().__init__(f"HTTP {kod}")
        self.kod = kod
        self.url = url
        self.basliklar = basliklar
        self.yonlendirildi = yonlendirildi


@dataclass(frozen=True)
class Oge:
    """İndirilecek tek dosya (manifestteki ``oge`` + ait olduğu bileşen)."""
    bilesen: str
    kimlik: str
    url: str
    bayt: int
    sha256: str
    hedef: str
    yedek_urller: Tuple[str, ...] = ()
    tur: str = "dosya"
    yeniden_kullan: Tuple[str, ...] = ()

    @classmethod
    def manifestten(cls, oge: Mapping[str, Any], bilesen: str) -> "Oge":
        return cls(bilesen=str(bilesen), kimlik=str(oge["kimlik"]), url=str(oge["url"]),
                   bayt=int(oge["bayt"]), sha256=str(oge["sha256"]), hedef=str(oge["hedef"]),
                   yedek_urller=tuple(str(u) for u in oge.get("yedek_urller") or ()),
                   tur=str(oge.get("tur") or "dosya"),
                   yeniden_kullan=tuple(str(y) for y in oge.get("yeniden_kullan") or ()))

    @property
    def ad(self) -> str:
        """İletilerde gösterilen dosya adı."""
        return PurePosixPath(self.hedef).name or self.kimlik


def ogeler_listesi(manifest: Any, kimlikler: Iterable[str],
                   macos: Optional[str] = None) -> List[Oge]:
    """``manifest.Manifest``'ten bileşenlerin bu macOS'a uyan öğeleri, sırayla."""
    return [Oge.manifestten(o, k) for k in kimlikler for o in manifest.ogeler(k, macos)]


@dataclass
class OgeSonucu:
    bilesen: str
    kimlik: str
    durum: str = BEKLIYOR
    hedef: Optional[Path] = None
    kaynak: Optional[str] = None
    agdan_bayt: int = 0
    hata: Optional[IndirmeHatasi] = None

    def sozluk(self) -> Dict[str, Any]:
        return {"bilesen": self.bilesen, "oge": self.kimlik, "durum": self.durum,
                "hedef": str(self.hedef) if self.hedef else None, "kaynak": self.kaynak,
                "agdan_bayt": self.agdan_bayt,
                "hata": self.hata.sozluk() if self.hata else None}


@dataclass
class IndirmeSonucu:
    """``durum``: "tamam" | "iptal" | "duraklatildi" (iş durduran hata) | "hata" (öğe hatası)."""
    durum: str
    ogeler: List[OgeSonucu] = field(default_factory=list)
    hata: Optional[IndirmeHatasi] = None

    @property
    def basarili(self) -> bool:
        return self.durum == IS_TAMAM

    def hatalar(self) -> List[OgeSonucu]:
        return [o for o in self.ogeler if o.durum == HATA]

    def sozluk(self) -> Dict[str, Any]:
        return {"durum": self.durum, "ogeler": [o.sozluk() for o in self.ogeler],
                "hata": self.hata.sozluk() if self.hata else None}


# ---------------------------------------------------------------------------
# Hız ölçümü (kayan pencere)
# ---------------------------------------------------------------------------
class HizOlcer:
    """Son ``pencere_sn`` saniyede eklenen gerçek baytlardan hız (bayt/sn).

    Ölçüm ``en_az_sn``'den kısaysa ``None`` (bilinmiyor) döner; bayt gelmeyen
    dönemde hız gerçekten düşer (sahte sabit hız yok).
    """

    def __init__(self, pencere_sn: float = ayarlar.HIZ_PENCERESI_SN,
                 en_az_sn: float = ayarlar.HIZ_OLCUM_EN_AZ_SN,
                 saat: Callable[[], float] = time.monotonic):
        self._pencere = float(pencere_sn)
        self._en_az = float(en_az_sn)
        self._saat = saat
        self._ornekler: deque = deque()
        self._toplam = 0
        self._ilk: Optional[float] = None
        self._kilit = threading.Lock()

    def _buda(self, simdi: float) -> None:
        sinir = simdi - self._pencere
        while self._ornekler and self._ornekler[0][0] < sinir:
            self._toplam -= self._ornekler.popleft()[1]

    def ekle(self, bayt: int) -> None:
        simdi = self._saat()
        with self._kilit:
            if self._ilk is None:
                self._ilk = simdi
            self._ornekler.append((simdi, int(bayt)))
            self._toplam += int(bayt)
            self._buda(simdi)

    def hiz(self) -> Optional[float]:
        simdi = self._saat()
        with self._kilit:
            if self._ilk is None:
                return None
            gecen = simdi - self._ilk
            if gecen < self._en_az:
                return None
            self._buda(simdi)
            return self._toplam / min(self._pencere, gecen)


# ---------------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------------
def _sorgusuz(url: str) -> str:
    """Günlük için: imzalı bağlantının sorgu kısmı yazılmaz."""
    parca = urlsplit(url)
    return urlunsplit((parca.scheme, parca.netloc, parca.path, "", ""))


def _hf_mi(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == _HF_ALANI or host.endswith("." + _HF_ALANI)


def yonlendirme_guvenli_mi(eski: str, yeni: str) -> bool:
    """https'ten başka şemaya düşen ya da http(s) dışı yönlendirme reddedilir."""
    eski_sema = urlsplit(eski).scheme.lower()
    yeni_sema = urlsplit(yeni).scheme.lower()
    if yeni_sema not in ("http", "https"):
        return False
    return not (eski_sema == "https" and yeni_sema != "https")


def bekleme_suresi(deneme: int, taban_sn: float = ayarlar.BEKLEME_TABANI_SN,
                   tavan_sn: float = ayarlar.BEKLEME_TAVANI_SN) -> float:
    """Üstel bekleme: taban × 2^(deneme−1), en çok tavan."""
    return min(float(tavan_sn), float(taban_sn) * (2 ** max(0, deneme - 1)))


def hiz_siniri_suresi(basliklar: Mapping[str, str], simdi: Optional[float] = None) -> Optional[float]:
    """429 yanıtından beklenecek saniye: ``Retry-After`` (sn ya da tarih), yoksa ``RateLimit`` t=.

    Başlık adları büyük/küçük harfe duyarsızdır (HF ``ratelimit`` diye gönderir).
    """
    basliklar = {str(k).lower(): v for k, v in basliklar.items()}
    deger = basliklar.get("retry-after")
    if deger:
        deger = deger.strip()
        if deger.isdigit():
            return float(deger)
        try:
            tarih = parsedate_to_datetime(deger)
            return max(0.0, tarih.timestamp() - (time.time() if simdi is None else simdi))
        except (TypeError, ValueError, IndexError, OverflowError):
            pass
    oran = basliklar.get("ratelimit") or ""
    bulunan = _RATELIMIT_T.search(oran) or _RATELIMIT_RESET.search(oran)
    if bulunan:
        return float(bulunan.group(1))
    return None


def _baglik_sha(deger: Optional[str]) -> Optional[str]:
    """``X-Linked-ETag: "abc…"`` → SHA-256 (64 onaltılık); değilse None (Xet/blob özeti)."""
    if not deger:
        return None
    temiz = deger.strip()
    if temiz.startswith("W/"):
        temiz = temiz[2:]
    temiz = temiz.strip().strip('"').lower()
    return temiz if _SHA256_HEX.match(temiz) else None


def _tls_hatasi_mi(hata: BaseException) -> bool:
    neden = getattr(hata, "reason", None)
    return isinstance(hata, ssl.SSLError) or isinstance(neden, ssl.SSLError)


def _ag_yok_mu(hata: BaseException) -> bool:
    neden = getattr(hata, "reason", hata)
    if isinstance(neden, (socket.gaierror, socket.timeout, TimeoutError, ConnectionRefusedError)):
        return True
    return isinstance(neden, OSError) and neden.errno in _AG_YOK_ERRNO


def _ssl_baglami(ca_dosyasi: Optional[os.PathLike] = None) -> ssl.SSLContext:
    if ca_dosyasi is not None:
        return ssl.create_default_context(cafile=os.fspath(ca_dosyasi))
    return ssl.create_default_context()


class _YonlendirmeIzleme(urllib.request.HTTPRedirectHandler):
    """Yönlendirmeyi urllib izlemesin: 30x HTTPError olarak döner, elle izlenir."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


class _GuvenliYonlendirme(urllib.request.HTTPRedirectHandler):
    """Yan istekler (güncelleme denetimi) yönlendirmeyi izler ama https'ten düşmez."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        if not yonlendirme_guvenli_mi(req.full_url, newurl):
            raise urllib.error.HTTPError(newurl, code, "güvenli olmayan yönlendirme", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _acici(baglam: ssl.SSLContext) -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_YonlendirmeIzleme(),
                                       urllib.request.HTTPSHandler(context=baglam))


def _izleyen_acici(baglam: ssl.SSLContext) -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_GuvenliYonlendirme(),
                                       urllib.request.HTTPSHandler(context=baglam))


def _bayt_metni(bayt: int) -> str:
    """İletide tam bayt sayısı (Türkçe binlik ayırıcı): 3412000000 → "3.412.000.000 bayt".

    Birimli gösterim (GB) arayüzün işidir; hata nesnesi sayıyı ``veri``de de taşır.
    """
    return f"{int(bayt):,}".replace(",", ".") + " bayt"


# ---------------------------------------------------------------------------
# İndirici
# ---------------------------------------------------------------------------
@dataclass
class _Calisma:
    """Tek öğenin bir indirme koşusundaki durumu."""
    oge: Oge
    part: Path
    algoritma: str
    ozet: Any = None
    yazilan: int = 0                 # .part'taki bayt (özete girmiş)
    beklenen_bayt: int = 0
    beklenen_hex: Optional[str] = None
    cozulmus: Optional[str] = None   # önbellekteki yönlendirilmiş adres
    agdan: int = 0


class Indirici:
    """Bir kurulum işinin indiricisi.

    Kullanım (K3)::

        ind = Indirici(kok, surum=manifest.surum, olay=gonder,
                       genel_toplam_bayt=plan.indirme_bayt)
        sonuc = ind.indir(ogeler_listesi(manifest, ["cekirdek"], macos))
        ...                          # aynı nesneyle sonraki bileşenler
        ind.iptal()                  # başka iş parçacığından; .part'lar kalır

    Bir nesne bir iş içindir: ``iptal()``'den ya da iş durduran bir hatadan
    sonra ``indir()`` hemen döner; "Sürdür" yeni bir ``Indirici`` kurar.
    Sayısal ayarların varsayılanı ``ayarlar.py``'dir; parametreler yalnız
    test ve sürüm betiği içindir.
    """

    def __init__(self, kok: "os.PathLike[str] | str", *, surum: str = "0.0.0",
                 olay: Optional[Callable[[str, Dict[str, Any]], None]] = None,
                 indirilen_dizini: "os.PathLike[str] | str | None" = None,
                 genel_toplam_bayt: Optional[int] = None,
                 ca_dosyasi: "os.PathLike[str] | str | None" = None,
                 var_olani_dogrula: bool = True,
                 yeniden_deneme: Optional[int] = None,
                 bekleme_tabani_sn: Optional[float] = None,
                 bekleme_tavani_sn: Optional[float] = None,
                 hiz_siniri_tavani_sn: Optional[float] = None,
                 zaman_asimi_sn: Optional[float] = None,
                 parca_bayt: Optional[int] = None,
                 is_parcacigi_kucuk: Optional[int] = None,
                 is_parcacigi_buyuk: Optional[int] = None,
                 buyuk_oge_esigi_bayt: Optional[int] = None,
                 disk_on_denetimi: bool = True,
                 saat: Callable[[], float] = time.monotonic):
        self.kok = Path(os.path.abspath(os.fspath(kok)))
        self.indirilen = Path(os.fspath(indirilen_dizini)) if indirilen_dizini is not None \
            else self.kok / ayarlar.INDIRILEN_DIZINI
        self.kullanici_aracisi = ayarlar.KULLANICI_ARACISI_SABLONU.format(surum=surum)
        self._olay_alici = olay
        self._var_olani_dogrula = var_olani_dogrula
        self._deneme = ayarlar.YENIDEN_DENEME if yeniden_deneme is None else int(yeniden_deneme)
        self._taban = ayarlar.BEKLEME_TABANI_SN if bekleme_tabani_sn is None else float(bekleme_tabani_sn)
        self._tavan = ayarlar.BEKLEME_TAVANI_SN if bekleme_tavani_sn is None else float(bekleme_tavani_sn)
        self._hiz_tavan = (ayarlar.HIZ_SINIRI_BEKLEME_TAVANI_SN if hiz_siniri_tavani_sn is None
                           else float(hiz_siniri_tavani_sn))
        self._zaman_asimi = (ayarlar.BAGLANTI_ZAMAN_ASIMI_SN if zaman_asimi_sn is None
                             else float(zaman_asimi_sn))
        self._parca = ayarlar.PARCA_BAYT if parca_bayt is None else int(parca_bayt)
        self._kucuk = ayarlar.IS_PARCACIGI_KUCUK if is_parcacigi_kucuk is None else int(is_parcacigi_kucuk)
        self._buyuk = ayarlar.IS_PARCACIGI_BUYUK if is_parcacigi_buyuk is None else int(is_parcacigi_buyuk)
        self._esik = (ayarlar.BUYUK_OGE_ESIGI_BAYT if buyuk_oge_esigi_bayt is None
                      else int(buyuk_oge_esigi_bayt))
        self._saat = saat
        self._disk_on_denetimi = disk_on_denetimi
        baglam = _ssl_baglami(ca_dosyasi)
        self._acici = _acici(baglam)

        self._durdur = threading.Event()
        self._durdurma_nedeni: Optional[IndirmeHatasi] = None
        self._kilit = threading.Lock()
        self._olay_kilidi = threading.Lock()
        self._aktif_yanitlar: set = set()
        self._hiz = HizOlcer(saat=saat)
        self._oge_bayt: Dict[Tuple[str, str], int] = {}
        self._oge_toplam: Dict[Tuple[str, str], int] = {}
        self._son_olay: Dict[Tuple[str, str], float] = {}
        self._sabit_genel_toplam = genel_toplam_bayt

    # -- dış API -------------------------------------------------------------
    def iptal(self) -> None:
        """İşi durdurur; süren okumalar kesilir, ``.part`` dosyaları kalır."""
        self._durdur_isaretle(IndirmeHatasi(
            HATA_IPTAL, "İndirme iptal edildi; indirilen kısım saklandı, kaldığı yerden sürer.",
            tum_isi_durdur=True))

    @property
    def durduruldu(self) -> bool:
        return self._durdur.is_set()

    def part_yolu(self, oge: Oge) -> Path:
        return disk.part_yolu(self.indirilen, oge.kimlik, oge.sha256)

    def indir(self, ogeler: Iterable[Oge]) -> IndirmeSonucu:
        """Öğeleri indirir (ya da yerinde/klon olarak doğrular). Hata fırlatmaz; sonuç döner."""
        liste = list(ogeler)
        sonuclar = {(o.bilesen, o.kimlik): OgeSonucu(o.bilesen, o.kimlik) for o in liste}
        with self._kilit:
            for o in liste:
                anahtar = (o.bilesen, o.kimlik)
                self._oge_toplam[anahtar] = o.bayt
                self._oge_bayt.setdefault(anahtar, 0)
        if liste and self._disk_on_denetimi and not self._durdur.is_set():
            yetersiz = self._disk_on_denetimi_yap(liste)
            if yetersiz is not None:
                self._durdur_isaretle(yetersiz)
        if not self._durdur.is_set():
            kucukler = [o for o in liste if o.bayt < self._esik]
            buyukler = [o for o in liste if o.bayt >= self._esik]
            havuzlar = []
            for grup, sayi in ((kucukler, self._kucuk), (buyukler, self._buyuk)):
                if grup:
                    havuz = ThreadPoolExecutor(max_workers=max(1, sayi),
                                               thread_name_prefix="indirici")
                    havuzlar.append((havuz, [havuz.submit(self._oge_guvenli, o) for o in grup]))
            for havuz, isler in havuzlar:
                for is_ in isler:
                    s = is_.result()
                    sonuclar[(s.bilesen, s.kimlik)] = s
                havuz.shutdown(wait=True)
        for anahtar, s in sonuclar.items():
            if s.durum == BEKLIYOR and self._durdur.is_set():
                s.durum = IPTAL
        return self._is_sonucu([sonuclar[(o.bilesen, o.kimlik)] for o in liste])

    def kalan_bayt(self, ogeler: Iterable[Oge]) -> int:
        """Diske yazılacak kalan bayt (hızlı sayım; özet okunmaz, ağa çıkılmaz).

        Hedefte boyu doğru dosya ya da ``yeniden_kullan`` yolunda boyu doğru aday
        olan öğe 0 sayılır (APFS klonu disk harcamaz); ``.part``'ı olanın yalnız
        kalanı sayılır.
        """
        kalan = 0
        for oge in ogeler:
            try:
                hedef = disk.guvenli_yol(self.kok, oge.hedef)
                if hedef.is_file() and hedef.stat().st_size == oge.bayt:
                    continue
            except (OSError, disk.GecersizYol):
                pass
            if self._aday_var_mi(oge):
                continue
            try:
                var = self.part_yolu(oge).stat().st_size
            except OSError:
                var = 0
            kalan += max(0, oge.bayt - var)
        return kalan

    @staticmethod
    def _aday_var_mi(oge: Oge) -> bool:
        for aday_metni in oge.yeniden_kullan:
            aday = Path(os.path.expanduser(aday_metni))
            try:
                if aday.is_dir():
                    aday = aday / PurePosixPath(oge.hedef).name
                if aday.is_file() and aday.stat().st_size == oge.bayt:
                    return True
            except OSError:
                continue
        return False

    def _disk_on_denetimi_yap(self, liste: Sequence[Oge]) -> Optional[IndirmeHatasi]:
        """İndirme başlamadan boş alan ≥ kalan bayt + ``DISK_PAYI_BAYT`` mı? (D.2-8)

        Diski tümüyle doldurmadan durmak için; asıl ön denetim plan anındadır
        (``disk.denetle(kök, plan.ihtiyac_bayt)``). Yetmezse iş hiç başlamaz.
        """
        try:
            denetim = disk.denetle(self.kok, self.kalan_bayt(liste))
        except OSError as exc:
            logger.warning("boş alan ölçülemedi, ön denetim atlandı: %r", exc)
            return None
        if denetim.yeter:
            return None
        return IndirmeHatasi(
            HATA_DISK_YETERSIZ,
            f"Diskte yeterli boş alan yok: indirme için {_bayt_metni(denetim.eksik_bayt)} daha "
            "gerekiyor (güvenlik payı dahil). İndirme başlamadı; yer açıp yeniden dene.",
            ayrinti=json.dumps(denetim.sozluk()), tum_isi_durdur=True, veri=denetim.sozluk())

    # -- iç: durum ve olaylar -----------------------------------------------
    def _durdur_isaretle(self, hata: IndirmeHatasi) -> None:
        with self._kilit:
            if self._durdurma_nedeni is None:
                self._durdurma_nedeni = hata
            self._durdur.set()
            yanitlar = list(self._aktif_yanitlar)
        for yanit in yanitlar:
            # Önce soket kapatılır: takılmış recv hemen döner. yanit.close()
            # burada ÇAĞRILMAZ; okuyan iş parçacığı tamponun kilidini tuttuğu
            # için close() okuma bitene kadar bekler (zaman aşımına dek takılır).
            # Yanıtı okuyan iş parçacığı kendi finally'sinde kapatır.
            sock = getattr(getattr(getattr(yanit, "fp", None), "raw", None), "_sock", None)
            if sock is None:
                continue
            try:
                # SSLSocket.shutdown _sslobj'yi sıfırlar; taban sınıfınki yalnız soketi keser.
                socket.socket.shutdown(sock, socket.SHUT_RDWR)
            except OSError:
                pass

    def _is_sonucu(self, ogeler: List[OgeSonucu]) -> IndirmeSonucu:
        neden = self._durdurma_nedeni
        if neden is not None:
            durum = IS_IPTAL if neden.kod == HATA_IPTAL else IS_DURAKLATILDI
            return IndirmeSonucu(durum=durum, ogeler=ogeler, hata=neden)
        hatali = [o for o in ogeler if o.durum == HATA]
        if hatali:
            return IndirmeSonucu(durum=IS_HATA, ogeler=ogeler, hata=hatali[0].hata)
        return IndirmeSonucu(durum=IS_TAMAM, ogeler=ogeler)

    def _yay(self, ad: str, veri: Dict[str, Any]) -> None:
        if self._olay_alici is None:
            return
        with self._olay_kilidi:
            try:
                self._olay_alici(ad, veri)
            except Exception:  # noqa: BLE001 — alıcı hatası indirmeyi bozmasın
                logger.exception("olay alıcısı hata verdi (%s)", ad)

    def _durum_yay(self, oge: Oge, durum: str, **ek: Any) -> None:
        self._yay("oge_durumu", {"bilesen": oge.bilesen, "oge": oge.kimlik, "durum": durum, **ek})

    def _ilerleme(self, oge: Oge, bayt: int, *, agdan: int = 0, zorla: bool = False,
                  toplam: Optional[int] = None) -> None:
        anahtar = (oge.bilesen, oge.kimlik)
        if agdan:
            self._hiz.ekle(agdan)
        simdi = self._saat()
        with self._kilit:
            self._oge_bayt[anahtar] = bayt
            if toplam is not None:
                self._oge_toplam[anahtar] = toplam
            oge_toplam = self._oge_toplam.get(anahtar, oge.bayt)
            son = self._son_olay.get(anahtar)
            if not zorla and son is not None and simdi - son < ayarlar.ILERLEME_OLAY_ARALIGI_SN:
                return
            self._son_olay[anahtar] = simdi
            genel = sum(self._oge_bayt.values())
            genel_toplam = self._sabit_genel_toplam if self._sabit_genel_toplam is not None \
                else sum(self._oge_toplam.values())
            genel_toplam = max(genel_toplam, genel)
        hiz = self._hiz.hiz()
        kalan = None
        if hiz:
            kalan = math.ceil((genel_toplam - genel) / hiz)
        self._yay("ilerleme", {"bilesen": oge.bilesen, "oge": oge.kimlik, "bayt": bayt,
                               "toplam_bayt": oge_toplam, "hiz_bayt_sn": hiz,
                               "genel_bayt": genel, "genel_toplam_bayt": genel_toplam,
                               "kalan_sn_tahmini": kalan})

    def _denetle(self) -> None:
        if self._durdur.is_set():
            raise _Durduruldu()

    def _bekle(self, sure: float, oge: Oge, neden: str, deneme: int) -> None:
        """Gerçek geri sayım: her tam saniyede bir ``bekleme`` olayı; iptalde hemen çıkar."""
        bitis = self._saat() + max(0.0, sure)
        son_saniye = None
        while True:
            kalan = bitis - self._saat()
            if kalan <= 0:
                return
            saniye = math.ceil(kalan)
            if saniye != son_saniye:
                son_saniye = saniye
                self._yay("bekleme", {"bilesen": oge.bilesen, "oge": oge.kimlik,
                                      "neden": neden, "kalan_sn": saniye, "deneme": deneme})
            if self._durdur.wait(min(ayarlar.IPTAL_YOKLAMA_SN, kalan)):
                raise _Durduruldu()

    # -- iç: öğe -------------------------------------------------------------
    def _oge_guvenli(self, oge: Oge) -> OgeSonucu:
        sonuc = OgeSonucu(oge.bilesen, oge.kimlik)
        try:
            self._oge_isle(oge, sonuc)
        except _Durduruldu:
            sonuc.durum = IPTAL
            self._durum_yay(oge, IPTAL)
        except IndirmeHatasi as hata:
            sonuc.durum = HATA
            sonuc.hata = hata
            if hata.tum_isi_durdur:
                self._durdur_isaretle(hata)
            logger.warning("indirme hatası %s/%s: %s (%s)", oge.bilesen, oge.kimlik, hata.kod,
                           hata.ayrinti)
            self._durum_yay(oge, HATA, hata=hata.sozluk())
        except Exception as exc:  # noqa: BLE001 — iş parçacığı sessizce ölmesin
            logger.exception("beklenmeyen indirme hatası %s/%s", oge.bilesen, oge.kimlik)
            sonuc.durum = HATA
            sonuc.hata = IndirmeHatasi(HATA_BEKLENMEYEN,
                                       f"Beklenmeyen hata: {oge.ad} indirilemedi.",
                                       ayrinti=repr(exc))
            self._durum_yay(oge, HATA, hata=sonuc.hata.sozluk())
        return sonuc

    def _oge_isle(self, oge: Oge, sonuc: OgeSonucu) -> None:
        self._denetle()
        try:
            hedef = disk.guvenli_yol(self.kok, oge.hedef)
        except disk.GecersizYol as exc:
            raise IndirmeHatasi(HATA_HEDEF, f"Geçersiz hedef yolu: {oge.hedef}",
                                ayrinti=str(exc)) from exc
        sonuc.hedef = hedef
        self._durum_yay(oge, BEKLIYOR)

        # 1. Hedef yerinde mi?
        if hedef.is_file():
            if self._yerinde_dogru_mu(oge, hedef):
                self._bitir(oge, sonuc, KAYNAK_YERINDE)
                return
            logger.info("yerindeki dosya doğrulanamadı, yeniden inecek: %s", oge.hedef)
            hedef.unlink()

        # 2. Bilinen yerde aynı dosya var mı? (APFS klonu)
        if oge.yeniden_kullan:
            kaynak = self._yeniden_kullan(oge, hedef)
            if kaynak:
                self._bitir(oge, sonuc, kaynak)
                return

        # 3. Ağ: birincil adres, sonra yedekler. Sunucuya özgü hatada (5xx / istek
        # sınırı / bağlantı hakkı bitti) sıradaki adres denenir; disk, TLS ve iptal
        # her adreste aynı olduğundan hemen bütün işi durdurur.
        adresler = [oge.url, *oge.yedek_urller]
        son_hata: Optional[IndirmeHatasi] = None
        for sira, adres in enumerate(adresler):
            try:
                sonuc.agdan_bayt += self._agdan_indir(oge, adres, hedef)
            except IndirmeHatasi as hata:
                sonuncu = sira == len(adresler) - 1
                if hata.tum_isi_durdur and (sonuncu or hata.kod not in _ADRESE_OZGU_HATALAR):
                    raise
                son_hata = hata
                logger.info("adres başarısız (%s), sıradaki denenecek: %s",
                            hata.kod, _sorgusuz(adres))
                continue
            self._bitir(oge, sonuc, KAYNAK_AG)
            return
        assert son_hata is not None
        raise son_hata

    def _bitir(self, oge: Oge, sonuc: OgeSonucu, kaynak: str) -> None:
        sonuc.durum = DOGRULANDI
        sonuc.kaynak = kaynak
        boy = sonuc.hedef.stat().st_size if sonuc.hedef else oge.bayt
        self._ilerleme(oge, boy, zorla=True, toplam=boy)
        self._durum_yay(oge, DOGRULANDI, kaynak=kaynak)

    def _yerinde_dogru_mu(self, oge: Oge, hedef: Path) -> bool:
        """Hedefteki dosya bu öğe mi? Boyu tutmuyorsa hayır; tutuyorsa özet (istenirse)."""
        if hedef.stat().st_size != oge.bayt:
            return False
        if not self._var_olani_dogrula:
            return True
        beklenen, algoritma = oge.sha256, "sha256"
        self._durum_yay(oge, DOGRULANIYOR)
        try:
            return disk.ozet_hesapla(hedef, algoritma, parca=self._parca,
                                     durdur=self._durdur) == beklenen
        except disk.IslemDurduruldu:
            raise _Durduruldu() from None

    def _yeniden_kullan(self, oge: Oge, hedef: Path) -> Optional[str]:
        """Bilinen yollarda aynı SHA'lı dosyayı arar; bulursa klonlar (olmazsa kopyalar)."""
        for aday_metni in oge.yeniden_kullan:
            aday = Path(os.path.expanduser(aday_metni))
            if aday.is_dir():
                aday = aday / PurePosixPath(oge.hedef).name
            try:
                if not aday.is_file() or aday.stat().st_size != oge.bayt:
                    continue
            except OSError:
                continue
            hedef.parent.mkdir(parents=True, exist_ok=True)
            gecici = hedef.with_name(f"{hedef.name}.{os.getpid()}.klon")
            gecici.unlink(missing_ok=True)
            try:
                if disk.apfs_klonla(aday, gecici):
                    kaynak = KAYNAK_KLON
                    self._durum_yay(oge, KLONLANDI, kaynak=kaynak)
                    self._durum_yay(oge, DOGRULANIYOR)
                    esit = disk.ozet_hesapla(gecici, "sha256", parca=self._parca,
                                             durdur=self._durdur) == oge.sha256
                else:
                    kaynak = KAYNAK_KOPYA
                    esit = self._kopyala_ozetle(oge, aday, gecici)
            except disk.IslemDurduruldu:
                gecici.unlink(missing_ok=True)
                raise _Durduruldu() from None
            except OSError as exc:
                gecici.unlink(missing_ok=True)
                if disk.disk_dolu_hatasi_mi(exc):
                    raise self._disk_dolu(exc) from exc
                logger.info("yeniden kullanım başarısız (%s): %s", aday, exc)
                continue
            except BaseException:
                gecici.unlink(missing_ok=True)
                raise
            if not esit:
                gecici.unlink(missing_ok=True)
                logger.info("yeniden kullanım adayı özeti uyuşmadı: %s", aday)
                continue
            os.replace(gecici, hedef)
            logger.info("yeniden kullanıldı (%s): %s → %s", kaynak, aday, oge.hedef)
            return kaynak
        return None

    def _kopyala_ozetle(self, oge: Oge, kaynak: Path, hedef: Path) -> bool:
        """Klon olmayınca (başka birim) parça parça kopyalar; özet aynı geçişte."""
        ozet = hashlib.sha256()
        yazilan = 0
        fd = os.open(hedef, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            with open(kaynak, "rb") as giris:
                while True:
                    if self._durdur.is_set():
                        raise disk.IslemDurduruldu(str(kaynak))
                    veri = giris.read(self._parca)
                    if not veri:
                        break
                    self._hepsini_yaz(fd, veri, ozet)
                    yazilan += len(veri)
                    self._ilerleme(oge, yazilan)
            os.fsync(fd)
        finally:
            os.close(fd)
        return ozet.hexdigest() == oge.sha256

    # -- iç: ağ --------------------------------------------------------------
    def _istek(self, url: str, *, yontem: str = "GET", bas: int = 0) -> urllib.request.Request:
        basliklar = {"User-Agent": self.kullanici_aracisi, "Accept-Encoding": "identity",
                     "Accept": "*/*"}
        if bas > 0:
            basliklar["Range"] = f"bytes={bas}-"
        return urllib.request.Request(url, headers=basliklar, method=yontem)

    def _ac(self, cal: _Calisma, url: str, bas: int, *, yontem: str = "GET"):
        """Yönlendirmeleri elle izleyerek açar; (yanıt, son adres) döner.

        Her yönlendirme yanıtındaki ``X-Linked-*`` başlıkları manifestle
        karşılaştırılır. 2xx dışı yanıt ``_HttpHatasi`` olur.
        """
        mevcut = url
        for _ in range(ayarlar.YONLENDIRME_EN_COK + 1):
            self._denetle()
            try:
                yanit = self._acici.open(self._istek(mevcut, yontem=yontem, bas=bas),
                                         timeout=self._zaman_asimi)
            except urllib.error.HTTPError as hata:
                basliklar = hata.headers
                try:
                    if hata.code in _YONLENDIRME_KODLARI and basliklar.get("Location"):
                        self._bagli_basliklari_denetle(cal, basliklar)
                        yeni = urljoin(mevcut, basliklar["Location"])
                        if not yonlendirme_guvenli_mi(mevcut, yeni):
                            raise IndirmeHatasi(HATA_YANIT,
                                                f"Güvenli olmayan yönlendirme reddedildi: {cal.oge.ad}",
                                                ayrinti=f"{_sorgusuz(mevcut)} → {_sorgusuz(yeni)}")
                        mevcut = yeni
                        continue
                    # Sözlüğe çevrilmez: HTTPMessage'in get'i büyük/küçük harfe duyarsız.
                    raise _HttpHatasi(hata.code, mevcut, basliklar,
                                      yonlendirildi=(mevcut != url)) from None
                finally:
                    try:
                        hata.close()
                    except Exception:  # noqa: BLE001
                        pass
            return yanit, mevcut
        raise IndirmeHatasi(HATA_YANIT, f"Çok fazla yönlendirme: {cal.oge.ad}",
                            ayrinti=_sorgusuz(url))

    def _bagli_basliklari_denetle(self, cal: _Calisma, basliklar: Mapping[str, str]) -> None:
        """HF ``resolve`` yanıtı: X-Linked-Size / X-Linked-ETag manifestle aynı mı? (D.2-4)"""
        boyut = basliklar.get("X-Linked-Size")
        if boyut and boyut.strip().isdigit() and int(boyut) != cal.oge.bayt:
            raise self._kaynak_degisti(cal.oge, f"sunucudaki boyut {int(boyut)}, "
                                                f"manifestte {cal.oge.bayt}")
        sha = _baglik_sha(basliklar.get("X-Linked-ETag"))
        if sha and sha != cal.oge.sha256:
            raise self._kaynak_degisti(cal.oge, "sunucudaki SHA-256 manifesttekinden farklı")

    def _kaynak_degisti(self, oge: Oge, neden: str) -> IndirmeHatasi:
        return IndirmeHatasi(HATA_KAYNAK_DEGISTI,
                             f"Kaynak değişmiş: {oge.ad} sunucuda bu sürümün beklediği dosya "
                             f"değil ({neden}). İndirme durduruldu.", ayrinti=neden)

    def _disk_dolu(self, exc: BaseException) -> IndirmeHatasi:
        return IndirmeHatasi(HATA_DISK_DOLU,
                             "Diskte yer kalmadı. İndirilen kısım saklandı; yer açıp "
                             "kurulumu sürdürebilirsin.", ayrinti=str(exc), tum_isi_durdur=True)

    def _ag_hatasi_ileti(self, oge: Oge, exc: BaseException) -> IndirmeHatasi:
        if _tls_hatasi_mi(exc):
            host = urlsplit(oge.url).hostname or ""
            # Sistem sertifikalarıyla yedek yol (sistem_sertifikalari_disa_aktar) arayüze
            # bağlı değil; ileti yalnız kullanıcının yapabileceğini söyler (K9).
            return IndirmeHatasi(HATA_TLS,
                                 f"Güvenli bağlantı (TLS) doğrulanamadı: {host}. Mac'in tarih "
                                 "ve saatini denetle; herkese açık bir ağdaysan ağın giriş "
                                 "sayfasını tamamla ya da başka bir ağdan yeniden dene. "
                                 "İndirilen kısım saklandı.", ayrinti=repr(exc),
                                 tum_isi_durdur=True)
        return IndirmeHatasi(HATA_BAGLANTI,
                             f"Bağlantı kurulamadı ya da koptu ({self._deneme} deneme). "
                             "İndirilen kısım saklandı; bağlantı gelince kurulum kaldığı "
                             "yerden sürer.", ayrinti=repr(exc), tum_isi_durdur=True)

    def _part_hazirla(self, cal: _Calisma) -> None:
        """Var olan ``.part``'ı özete okur (sürdürme); boyu beklenenden büyükse siler."""
        cal.part.parent.mkdir(parents=True, exist_ok=True)
        cal.ozet = hashlib.new(cal.algoritma)
        cal.yazilan = 0
        if not cal.part.exists():
            return
        boy = cal.part.stat().st_size
        if boy > cal.oge.bayt:
            logger.info(".part beklenenden büyük, siliniyor: %s", cal.part.name)
            cal.part.unlink()
            return
        try:
            cal.yazilan = disk.ozete_ekle(cal.ozet, cal.part, parca=self._parca, durdur=self._durdur)
        except disk.IslemDurduruldu:
            raise _Durduruldu() from None
        self._ilerleme(cal.oge, cal.yazilan, zorla=True)

    def _part_sifirla(self, cal: _Calisma, fd: int) -> None:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        cal.ozet = hashlib.new(cal.algoritma)
        cal.yazilan = 0
        self._ilerleme(cal.oge, 0, zorla=True)

    def _hepsini_yaz(self, fd: int, veri: bytes, ozet: Any) -> None:
        gorunum = memoryview(veri)
        while gorunum:
            try:
                n = os.write(fd, gorunum)
            except OSError as exc:
                if disk.disk_dolu_hatasi_mi(exc):
                    raise self._disk_dolu(exc) from exc
                raise IndirmeHatasi(HATA_YAZMA, "İndirilen dosya diske yazılamadı.",
                                    ayrinti=repr(exc)) from exc
            ozet.update(gorunum[:n])
            gorunum = gorunum[n:]

    def _agdan_indir(self, oge: Oge, url: str, hedef: Path) -> int:
        """Tek adresten indirir, doğrular, hedefe taşır; ağdan gelen baytı döndürür."""
        cal = _Calisma(oge=oge, part=self.part_yolu(oge), algoritma="sha256",
                       beklenen_bayt=oge.bayt, beklenen_hex=oge.sha256)
        self._part_hazirla(cal)
        fd = os.open(cal.part, os.O_WRONLY | os.O_CREAT, 0o644)
        try:
            os.lseek(fd, 0, os.SEEK_END)
            self._dongu(cal, url, fd)
            try:
                os.fsync(fd)
            except OSError as exc:
                if disk.disk_dolu_hatasi_mi(exc):
                    raise self._disk_dolu(exc) from exc
                raise
        except BaseException:
            # Hiç bayt inmeden biten deneme boş .part bırakmasın (dolu .part sürdürme için kalır).
            try:
                if os.fstat(fd).st_size == 0:
                    cal.part.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        finally:
            os.close(fd)
        hedef.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(cal.part, hedef)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            shutil.move(os.fspath(cal.part), os.fspath(hedef))
        return cal.agdan

    def _dongu(self, cal: _Calisma, url: str, fd: int) -> None:
        """Yeniden deneme / yeniden çözümleme / doğrulama döngüsü (D.2-2…5)."""
        oge = cal.oge
        deneme = 0
        cozumleme = 0
        bastan = 0
        while True:
            self._denetle()
            ag_once = cal.agdan
            try:
                if cal.yazilan > cal.beklenen_bayt:
                    self._part_sifirla(cal, fd)
                if cal.yazilan < cal.beklenen_bayt:
                    ilerledi = self._bir_deneme(cal, url, fd)
                    if ilerledi:
                        deneme = 0
                        cozumleme = 0
                    if cal.yazilan < cal.beklenen_bayt:
                        # Sunucu erken kapattı (urllib eksik gövdede sessizce b"" döner).
                        raise ConnectionError(f"erken kopma {cal.yazilan}/{cal.beklenen_bayt}")
            except _HttpHatasi as hata:
                deneme, cozumleme = self._http_hatasi(cal, url, fd, hata, deneme, cozumleme)
                continue
            except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
                self._denetle()
                if isinstance(exc, OSError) and disk.disk_dolu_hatasi_mi(exc):
                    raise self._disk_dolu(exc) from exc
                deneme = self._ag_denemesi(cal, exc, deneme, ilerledi=cal.agdan > ag_once)
                continue
            # Tamamlandı: doğrula (özet akış içinde hesaplandı; ikinci okuma yok)
            self._durum_yay(oge, DOGRULANIYOR)
            if cal.ozet.hexdigest() == cal.beklenen_hex:
                return
            bastan += 1
            logger.warning("özet uyuşmadı (%s), .part silindi (baştan %d)", oge.kimlik, bastan)
            self._durum_yay(oge, SHA_UYUSMADI)
            self._part_sifirla(cal, fd)
            if bastan > ayarlar.SHA_UYUSMADI_BASTAN_DENEME:
                cal.part.unlink(missing_ok=True)
                raise IndirmeHatasi(
                    HATA_SHA,
                    f"İndirilen dosya doğrulanamadı ({_OZET_ADI[cal.algoritma]} uyuşmuyor): "
                    f"{oge.ad}. Bozuk dosya silindi; yeniden deneyebilirsin.",
                    ayrinti=f"{bastan} kez baştan indirildi")

    def _ag_denemesi(self, cal: _Calisma, exc: BaseException, deneme: int, *,
                     ilerledi: bool) -> int:
        """Bağlantı hatası: deneme sayar, üstel bekler; hak bitince işi durdurur."""
        self._denetle()
        if _tls_hatasi_mi(exc):
            raise self._ag_hatasi_ileti(cal.oge, exc)
        deneme = 1 if ilerledi else deneme + 1
        if deneme > self._deneme:
            raise self._ag_hatasi_ileti(cal.oge, exc)
        logger.info("bağlantı hatası (%s), %d. deneme: %r", cal.oge.kimlik, deneme, exc)
        self._durum_yay(cal.oge, YENIDEN_DENE, deneme=deneme)
        self._bekle(bekleme_suresi(deneme, self._taban, self._tavan), cal.oge,
                    NEDEN_YENIDEN_DENEME, deneme)
        return deneme

    def _http_hatasi(self, cal: _Calisma, url: str, fd: int, hata: _HttpHatasi,
                     deneme: int, cozumleme: int) -> Tuple[int, int]:
        oge = cal.oge
        kod = hata.kod
        if kod == 416:
            # İstenen aralık dosyada yok: .part güvenilmez, baştan.
            logger.info("416 (%s): .part baştan", oge.kimlik)
            self._part_sifirla(cal, fd)
            deneme += 1
            if deneme > self._deneme:
                raise IndirmeHatasi(HATA_YANIT, f"Sunucu indirme aralığını kabul etmedi: {oge.ad}",
                                    ayrinti="416")
            return deneme, cozumleme
        if kod == 429:
            deneme += 1
            if deneme > self._deneme:
                raise IndirmeHatasi(HATA_HIZ_SINIRI,
                                    "Sunucunun istek sınırı aşıldı ve beklemeler yetmedi. "
                                    "İndirilen kısım saklandı; biraz sonra sürdür.",
                                    ayrinti=str(hata.basliklar.get("RateLimit", "")),
                                    tum_isi_durdur=True)
            sure = hiz_siniri_suresi(hata.basliklar)
            sure = bekleme_suresi(deneme, self._taban, self._tavan) if sure is None \
                else min(sure, self._hiz_tavan)
            self._durum_yay(oge, YENIDEN_DENE, deneme=deneme)
            self._bekle(sure, oge, NEDEN_HIZ_SINIRI, deneme)
            return deneme, cozumleme
        if 500 <= kod < 600:
            deneme += 1
            if deneme > self._deneme:
                raise IndirmeHatasi(HATA_SUNUCU,
                                    f"Sunucu yanıt vermiyor (HTTP {kod}, {self._deneme} deneme). "
                                    "İndirilen kısım saklandı; sonra sürdürebilirsin.",
                                    ayrinti=_sorgusuz(hata.url), tum_isi_durdur=True)
            self._durum_yay(oge, YENIDEN_DENE, deneme=deneme)
            self._bekle(bekleme_suresi(deneme, self._taban, self._tavan), oge,
                        NEDEN_YENIDEN_DENEME, deneme)
            return deneme, cozumleme
        # Diğer 4xx: önbellekteki ya da yönlendirilmiş (imzalı CDN) adres → yeniden çözümle.
        if cal.cozulmus is not None or hata.yonlendirildi:
            cozumleme += 1
            if cozumleme <= ayarlar.YENIDEN_COZUMLEME_EN_COK:
                logger.info("HTTP %d yönlendirilmiş adreste (%s): yeniden çözümleniyor", kod,
                            oge.kimlik)
                cal.cozulmus = None
                return deneme, cozumleme
        if kod in (401, 403):
            if _hf_mi(url):
                ileti = (f"Hugging Face'te oturum/onay gerekiyor: {oge.ad} (HTTP {kod}). "
                         "Model kapılı hâle gelmiş olabilir.")
            else:
                ileti = f"Sunucu erişimi reddetti: {oge.ad} (HTTP {kod})."
            raise IndirmeHatasi(HATA_ERISIM, ileti, ayrinti=_sorgusuz(hata.url))
        if kod in (404, 410):
            raise IndirmeHatasi(HATA_BULUNAMADI, f"Dosya sunucuda bulunamadı: {oge.ad} (HTTP {kod}).",
                                ayrinti=_sorgusuz(hata.url))
        raise IndirmeHatasi(HATA_YANIT, f"Sunucudan beklenmeyen yanıt: {oge.ad} (HTTP {kod}).",
                            ayrinti=_sorgusuz(hata.url))

    def _bir_deneme(self, cal: _Calisma, url: str, fd: int) -> bool:
        """Tek istek: açar, yanıtı denetler, gövdeyi akıtır. Bayt geldiyse True."""
        oge = cal.oge
        adres = cal.cozulmus or url
        yanit, son = self._ac(cal, adres, cal.yazilan)
        if son != url:
            cal.cozulmus = son
        with self._kilit:
            self._aktif_yanitlar.add(yanit)
        ilerledi = False
        try:
            if self._durdur.is_set():
                raise _Durduruldu()
            durum = yanit.status
            if durum == 206:
                aralik = _CONTENT_RANGE.match(yanit.headers.get("Content-Range", "") or "")
                if not aralik or int(aralik.group(1)) != cal.yazilan:
                    logger.info("Content-Range beklenmedik (%s): .part baştan", oge.kimlik)
                    self._part_sifirla(cal, fd)
                    raise ConnectionError("Content-Range uyuşmadı; baştan istenecek")
                toplam = aralik.group(3)
                if toplam != "*":
                    self._toplami_denetle(cal, int(toplam))
            elif durum == 200:
                if cal.yazilan:
                    logger.info("sunucu Range'i yok saydı (%s): .part baştan yazılıyor", oge.kimlik)
                    self._part_sifirla(cal, fd)
                uzunluk = yanit.headers.get("Content-Length")
                if uzunluk and uzunluk.strip().isdigit():
                    self._toplami_denetle(cal, int(uzunluk))
            else:
                raise IndirmeHatasi(HATA_YANIT,
                                    f"Sunucudan beklenmeyen yanıt: {oge.ad} (HTTP {durum}).",
                                    ayrinti=_sorgusuz(son))
            self._durum_yay(oge, INIYOR)
            self._ilerleme(oge, cal.yazilan, zorla=True, toplam=cal.beklenen_bayt)
            while True:
                if self._durdur.is_set():
                    raise _Durduruldu()
                try:
                    parca = yanit.read(self._parca)
                except Exception:
                    # İptalde yanıt başka iş parçacığından kapatılır: okuma
                    # ValueError/AttributeError/OSError verebilir; bu iptaldir.
                    if self._durdur.is_set():
                        raise _Durduruldu() from None
                    raise
                if not parca:
                    break
                if cal.yazilan + len(parca) > cal.beklenen_bayt:
                    self._part_sifirla(cal, fd)
                    raise self._kaynak_degisti(oge, "sunucu beklenenden fazla bayt gönderdi")
                self._hepsini_yaz(fd, parca, cal.ozet)
                cal.yazilan += len(parca)
                cal.agdan += len(parca)
                ilerledi = True
                self._ilerleme(oge, cal.yazilan, agdan=len(parca))
            self._ilerleme(oge, cal.yazilan, zorla=True)
            return ilerledi
        finally:
            with self._kilit:
                self._aktif_yanitlar.discard(yanit)
            try:
                yanit.close()
            except Exception:  # noqa: BLE001
                pass

    def _toplami_denetle(self, cal: _Calisma, toplam: int) -> None:
        if toplam != cal.oge.bayt:
            raise self._kaynak_degisti(cal.oge, f"sunucudaki boyut {toplam}, manifestte {cal.oge.bayt}")


# ---------------------------------------------------------------------------
# Çevrimdışı algılama (TASARIM D.2-9) ve güncelleme denetimi (G.4)
# ---------------------------------------------------------------------------
INTERNET_VAR = "var"
INTERNET_YOK = "yok"
INTERNET_BILINMIYOR = "bilinmiyor"


def internet_durumu(url: str, *, zaman_asimi_sn: Optional[float] = None,
                    surum: str = "0.0.0") -> str:
    """Adrese HEAD atar: herhangi bir HTTP yanıtı "var"; ad çözülemez / ulaşılamaz /
    zaman aşımı "yok"; başka (ör. TLS) "bilinmiyor". Yönlendirme izlenmez."""
    sure = ayarlar.CEVRIMDISI_ZAMAN_ASIMI_SN if zaman_asimi_sn is None else zaman_asimi_sn
    istek = urllib.request.Request(url, method="HEAD", headers={
        "User-Agent": ayarlar.KULLANICI_ARACISI_SABLONU.format(surum=surum)})
    try:
        with _acici(_ssl_baglami()).open(istek, timeout=sure):
            return INTERNET_VAR
    except urllib.error.HTTPError as hata:
        hata.close()
        return INTERNET_VAR
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        if _tls_hatasi_mi(exc):
            return INTERNET_BILINMIYOR
        if _ag_yok_mu(exc):
            return INTERNET_YOK
        return INTERNET_BILINMIYOR


def _surum_demeti(metin: str) -> Optional[Tuple[int, int, int]]:
    bulunan = _SEMVER.match(str(metin).strip())
    return tuple(int(x) for x in bulunan.groups()) if bulunan else None  # type: ignore[return-value]


def guncelleme_denetle(mevcut_surum: str, *, url: Optional[str] = None,
                       zaman_asimi_sn: Optional[float] = None) -> Dict[str, Any]:
    """Yalnız "Denetle" düğmesiyle çağrılır (G.4). Hiç hata fırlatmaz.

    Dönen ``durum``: "yeni" | "guncel" | "denetlenemedi". Erişilemezse (depo
    özel → 404, ağ yok, bozuk yanıt) sessizce "denetlenemedi". Adresler
    ``ayarlar.py``'den (tek yer).
    """
    adres = url or ayarlar.GUNCELLEME_API_URL
    sure = ayarlar.GUNCELLEME_ZAMAN_ASIMI_SN if zaman_asimi_sn is None else zaman_asimi_sn
    sonuc: Dict[str, Any] = {"durum": "denetlenemedi", "mevcut_surum": mevcut_surum,
                             "son_surum": None, "baslik": None,
                             "sayfa_url": ayarlar.SURUMLER_SAYFASI_URL}
    istek = urllib.request.Request(adres, headers={
        "User-Agent": ayarlar.KULLANICI_ARACISI_SABLONU.format(surum=mevcut_surum),
        "Accept": "application/vnd.github+json"})
    try:
        with _izleyen_acici(_ssl_baglami()).open(istek, timeout=sure) as yanit:
            veri = json.loads(yanit.read(ayarlar.GUNCELLEME_YANIT_EN_COK_BAYT).decode("utf-8"))
        etiket = str(veri["tag_name"])
    except Exception as exc:  # noqa: BLE001 — sessizce "denetlenemedi"
        logger.info("güncelleme denetlenemedi: %r", exc)
        return sonuc
    son, mevcut = _surum_demeti(etiket), _surum_demeti(mevcut_surum)
    if son is None or mevcut is None:
        logger.info("güncelleme denetlenemedi: sürüm biçimi (%s / %s)", etiket, mevcut_surum)
        return sonuc
    sayfa = veri.get("html_url")
    if isinstance(sayfa, str) and sayfa.startswith(ayarlar.KAYNAK_KODU_URL + "/"):
        sonuc["sayfa_url"] = sayfa
    sonuc.update(durum="yeni" if son > mevcut else "guncel",
                 son_surum=".".join(str(x) for x in son),
                 baslik=str(veri.get("name") or etiket))
    return sonuc


# ---------------------------------------------------------------------------
# TLS yedek yolu (TASARIM D.2-10): yalnız TLS hatasında ve kullanıcı onayıyla
# ---------------------------------------------------------------------------
def sistem_sertifikalari_disa_aktar(hedef: "os.PathLike[str] | str", *,
                                    calistir: Callable[..., Any] = subprocess.run) -> Path:
    """Sistem anahtar zincirindeki kök sertifikaları PEM dosyasına yazar.

    Dönen yol ``Indirici(ca_dosyasi=...)``'ya verilir. Otomatik çağrılmaz:
    kullanıcı onayını arayüz (K4) alır.
    """
    hedef_yolu = Path(os.fspath(hedef))
    cikti = calistir([_SECURITY, "find-certificate", "-a", "-p", *_ANAHTAR_ZINCIRLERI],
                     capture_output=True, timeout=ayarlar.CA_DISA_AKTARMA_ZAMAN_ASIMI_SN,
                     check=False)
    veri = cikti.stdout or b""
    if cikti.returncode != 0 or b"BEGIN CERTIFICATE" not in veri:
        raise IndirmeHatasi(HATA_TLS, "Sistem anahtar zincirinden sertifika alınamadı.",
                            ayrinti=(cikti.stderr or b"")[:500].decode("utf-8", "replace"))
    hedef_yolu.parent.mkdir(parents=True, exist_ok=True)
    gecici = hedef_yolu.with_name(hedef_yolu.name + ".yeni")
    gecici.write_bytes(veri)
    os.replace(gecici, hedef_yolu)
    return hedef_yolu
