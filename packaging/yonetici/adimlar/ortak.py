"""Kurulum adımlarının ortak altyapısı: bağlam, sonuç, hatalar, alt süreç çalıştırıcı.

Yalnız stdlib (TASARIM A.2). Adım işlevlerinin imzası tektir::

    def adim(b: Baglam, adim: Mapping, kayit: Optional[dict]) -> AdimSonucu

- ``adim``: manifestteki adım kaydı (``{"tur": ..., <alanlar>}``; alanlar
  ``kurulum.py`` belgesindeki tabloda).
- ``kayit``: bu adımın önceki başarılı koşusunun kaydı (``durum/kurulum.json``
  ``adimlar``); yoksa None. Adım, kayıt geçerliyse VE son koşulu hâlâ
  sağlanıyorsa işi yapmadan ``AdimSonucu(atlandi=True, ...)`` döner
  (idempotent, yarıda kalınca sürdürülebilir). ``b.yeniden`` (Onar) kayda
  bakmadan yeniden yaptırır.
- Adım kendi tükettiği öğeleri (tekerlek, arşiv, paket) ``b.indir`` ile
  kendisi indirir; atlanan adım indirmez.
"""

from __future__ import annotations

import collections
import logging
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Sequence

try:
    from .. import ayarlar
    from ..durum import KokDuzeni, KurulumDurumu
except ImportError:  # düz betik olarak yüklendi (sys.path[0] = packaging/yonetici)
    import ayarlar  # type: ignore[no-redef]
    from durum import KokDuzeni, KurulumDurumu  # type: ignore[no-redef]

logger = logging.getLogger("kitapcevir.yonetici.kurulum")

# ---------------------------------------------------------------------------
# Sabitler (yalnız adımlar)
# ---------------------------------------------------------------------------
# Finder'dan açılan uygulamanın PATH'i (prototip K2). Alt süreçler aynı
# ortamı görsün diye geliştirici kipinde de bu kullanılır.
SISTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
# Kullanıcının kabuğundan sızıp kurulumu etkileyebilecek ortam değişkenleri
# (ör. PYTHONPATH, VIRTUAL_ENV, UV_INDEX_URL, PIP_INDEX_URL, CONDA_*).
_SIZINTI_ONEKLERI = ("PYTHON", "VIRTUAL_ENV", "CONDA", "PIP_", "UV_", "__PYVENV")
# İptalde SIGTERM ile SIGKILL arasındaki bekleme.
DURDURMA_SURESI_SN = 5.0
# Hata iletisinin "ayrıntı" alanına giden son çıktı satırı sayısı.
HATA_SATIR_SAYISI = 20

# Hata kodları (hata olayının "kod" alanı; arayüz iletiyi olduğu gibi gösterir)
KOD_KOMUT = "komut_basarisiz"
KOD_IPTAL = "iptal"
KOD_MANIFEST = "manifest"
KOD_KILIT = "kilit_uyumsuz"
KOD_ARSIV = "arsiv_bozuk"
KOD_DOSYA_YOK = "dosya_yok"
KOD_CALISMA_ZAMANI = "calisma_zamani"
KOD_KSA_YOK = "ksa_yok"
KOD_TEX = "tex_sinama"
KOD_LISANS = "lisans"
KOD_DOGRULAMA = "dogrulama"
KOD_INDIRME = "indirme"


class AdimHatasi(Exception):
    """Kullanıcıya gösterilebilir Türkçe iletili adım hatası."""

    def __init__(self, kod: str, ileti: str, ayrinti: str = ""):
        super().__init__(ileti)
        self.kod = kod
        self.ileti = ileti
        self.ayrinti = ayrinti


class IptalEdildi(Exception):
    """Kullanıcı işi iptal etti (``iptal`` olayı kuruldu)."""


# ---------------------------------------------------------------------------
# Adım sonucu
# ---------------------------------------------------------------------------
@dataclass
class AdimSonucu:
    """Bir adımın bileşen kaydına katkısı.

    - ``dosyalar``: köke göre yollar; bileşen kaydına boyutlarıyla yazılır
      (``durum.bilesen_durumu`` hızlı denetimi).
    - ``harici``: kökün dışındaki, bileşenin dayandığı mutlak yollar (MacTeX
      / kurulu TeX'in xelatex'i).
    - ``yollar``: köke göre klasörler (bileşenin sahibi olduğu; "Diskte" ölçümü
      ve "Kaldır" bunları bütün olarak ele alır; ör. ``tex``, model klasörü).
    - ``gruplar``: ``{grup: {"kilit_sha256", "paketler": [[ad, sürüm], ...]}}``.
    - ``tex``: TeX adımlarında ``{"xelatex", "surum", "sinama"}`` → ``durum/tex.json``.
    - ``tuketilen``: kurulumdan sonra ``indirilen/`` altından silinebilecek öğe kimlikleri.
    - ``kayit``: adım kaydına eklenecek ek alanlar (ör. kilit SHA'sı).
    """
    atlandi: bool = False
    dosyalar: List[str] = field(default_factory=list)
    harici: List[str] = field(default_factory=list)
    yollar: List[str] = field(default_factory=list)
    gruplar: Dict[str, Any] = field(default_factory=dict)
    tex: Optional[Dict[str, Any]] = None
    tuketilen: List[str] = field(default_factory=list)
    kayit: Dict[str, Any] = field(default_factory=dict)

    def kayda(self) -> Dict[str, Any]:
        """Adım kaydına yazılacak sözlük (atlanınca aynı sonuç bundan kurulur)."""
        veri = dict(self.kayit)
        veri["sonuc"] = {"dosyalar": list(self.dosyalar), "harici": list(self.harici),
                         "yollar": list(self.yollar), "gruplar": dict(self.gruplar),
                         "tex": self.tex}
        return veri

    @classmethod
    def kayittan(cls, kayit: Mapping[str, Any]) -> "AdimSonucu":
        sonuc = kayit.get("sonuc") if isinstance(kayit.get("sonuc"), Mapping) else {}
        return cls(atlandi=True, dosyalar=list(sonuc.get("dosyalar") or []),
                   harici=list(sonuc.get("harici") or []),
                   yollar=list(sonuc.get("yollar") or []),
                   gruplar=dict(sonuc.get("gruplar") or {}), tex=sonuc.get("tex"),
                   kayit={k: v for k, v in kayit.items() if k not in ("sonuc", "zaman")})


# ---------------------------------------------------------------------------
# Bağlam
# ---------------------------------------------------------------------------
Olay = Callable[[str, Dict[str, Any]], None]
IndirIslevi = Callable[[Sequence[Mapping[str, Any]]], None]


@dataclass
class Baglam:
    """Bir bileşenin adımlarına verilen bağlam.

    ``indir``: öğeleri ``<kök>/<hedef>``'e SHA-256 doğrulamasıyla indirir
    (``kurulum.py``'deki indirici sözleşmesi); iptalde ``IptalEdildi``,
    hatada ``AdimHatasi(KOD_INDIRME, ...)`` yükseltir.
    """
    duzen: KokDuzeni
    kayit_defteri: KurulumDurumu
    uygulama: Path
    manifest: Mapping[str, Any]
    bilesen: Mapping[str, Any]
    olay: Olay
    iptal: threading.Event
    indir: IndirIslevi
    ogeler: List[Mapping[str, Any]] = field(default_factory=list)   # bu macOS'a uyan öğeler
    yeniden: bool = False
    macos_ana: Optional[int] = None
    # Plandan gelen seçimler (ör. K4 planındaki tex-kurulu için {"xelatex": <yol>})
    tercihler: Dict[str, Any] = field(default_factory=dict)
    # Şu an koşan adımın kimliği (sayaç/bekleme olaylarına eklenir)
    adim_kimligi: Optional[str] = None

    @property
    def kimlik(self) -> str:
        return str(self.bilesen.get("kimlik"))

    # -- öğeler ---------------------------------------------------------------
    def oge(self, kimlik: str) -> Mapping[str, Any]:
        for oge in self.ogeler:
            if oge.get("kimlik") == kimlik:
                return oge
        raise AdimHatasi(KOD_MANIFEST, f"{self.kimlik} bileşeninde '{kimlik}' öğesi yok "
                                       "(ya da bu macOS sürümüne uygun değil).")

    def oge_yolu(self, oge: Mapping[str, Any]) -> Path:
        try:
            return self.duzen.kok_yolu(str(oge.get("hedef", "")))
        except ValueError as exc:
            raise AdimHatasi(KOD_MANIFEST, f"Geçersiz öğe hedefi: {exc}") from exc

    def indirilen_altinda_mi(self, oge: Mapping[str, Any]) -> bool:
        """Öğe geçici indirme deposuna mı iniyor (tüketilince silinir)?"""
        return PurePosixPath(str(oge.get("hedef", ""))).parts[:1] == \
            (ayarlar.INDIRILEN_DIZINI,)

    def uygulama_yolu(self, goreli: str) -> Path:
        """Uygulama koduna göre yol (``packaging/tex/isitma.tex`` gibi); '..' yasak."""
        parca = PurePosixPath(goreli)
        if not goreli or parca.is_absolute() or ".." in parca.parts:
            raise AdimHatasi(KOD_MANIFEST, f"Geçersiz uygulama yolu: {goreli!r}")
        return self.uygulama.joinpath(*parca.parts)

    @property
    def min_macos(self) -> str:
        uyg = self.manifest.get("uygulama") if isinstance(self.manifest, Mapping) else None
        return str((uyg or {}).get("min_macos") or "")

    # -- olay ve iptal --------------------------------------------------------
    def bildir(self, olay_turu: str, /, **veri: Any) -> None:
        veri.setdefault("bilesen", self.kimlik)
        if self.adim_kimligi is not None:
            veri.setdefault("adim", self.adim_kimligi)
        try:
            self.olay(olay_turu, veri)
        except Exception:  # noqa: BLE001 — olay dinleyicisinin hatası kurulumu durdurmaz
            logger.exception("olay dinleyicisi hata verdi (%s)", olay_turu)

    def iptal_denetle(self) -> None:
        if self.iptal.is_set():
            raise IptalEdildi()

    def bekle(self, saniye: float) -> None:
        """İptale duyarlı bekleme."""
        if self.iptal.wait(max(0.0, saniye)):
            raise IptalEdildi()

    # -- alt süreç -------------------------------------------------------------
    def komut(self, argv: Sequence[Any], *, ortam: Optional[Mapping[str, str]] = None,
              cwd: Optional[Path] = None, satir: Optional[Callable[[str], None]] = None
              ) -> "KomutSonucu":
        return komut_calistir(argv, iptal=self.iptal, env=temel_ortam(ortam), cwd=cwd,
                              satir=satir)


# ---------------------------------------------------------------------------
# Alt süreç
# ---------------------------------------------------------------------------
def temel_ortam(ek: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """Alt süreç ortamı: sızıntı değişkenleri atılır, sabit ve tekrarlanabilir değerler.

    ``UV_NO_CACHE``: uv önbelleği kökte birikmesin (prototip: 420 MB);
    ``UV_PYTHON_DOWNLOADS=never``: uv kendi Python'unu indirmesin.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith(_SIZINTI_ONEKLERI)}
    env.update({
        "PATH": SISTEM_PATH,
        "PYTHONNOUSERSITE": "1",
        "PYTHONUTF8": "1",
        "UV_NO_CACHE": "1",
        "UV_PYTHON_DOWNLOADS": "never",
        "HF_HUB_DISABLE_TELEMETRY": "1",
    })
    env.setdefault("LANG", "en_US.UTF-8")
    if ek:
        env.update({str(k): str(v) for k, v in ek.items()})
    return env


@dataclass
class KomutSonucu:
    rc: int
    son_satirlar: List[str]

    @property
    def ayrinti(self) -> str:
        return "\n".join(self.son_satirlar)


def _surec_grubunu_durdur(surec: subprocess.Popen) -> None:
    for sinyal in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(surec.pid, sinyal)
        except (ProcessLookupError, PermissionError):
            return
        try:
            surec.wait(timeout=DURDURMA_SURESI_SN)
            return
        except subprocess.TimeoutExpired:
            continue


def komut_calistir(argv: Sequence[Any], *, iptal: threading.Event,
                   env: Optional[Mapping[str, str]] = None, cwd: Optional[Path] = None,
                   satir: Optional[Callable[[str], None]] = None) -> KomutSonucu:
    """Komutu çalıştırır; çıktıyı satır satır günlüğe ve ``satir``'a verir.

    Süreç kendi süreç grubunda başlar; ``iptal`` kurulursa gruba SIGTERM,
    ``DURDURMA_SURESI_SN`` sonra SIGKILL gönderilir ve ``IptalEdildi``
    yükseltilir. Standart hata standart çıktıyla birleşir.
    """
    args = [os.fspath(a) for a in argv]
    logger.info("komut: %s", " ".join(args))
    try:
        surec = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, env=dict(env) if env else None,
                                 cwd=os.fspath(cwd) if cwd else None, start_new_session=True)
    except OSError as exc:
        raise AdimHatasi(KOD_KOMUT, f"Komut başlatılamadı: {args[0]} ({exc.strerror})") from exc

    son: Deque[str] = collections.deque(maxlen=HATA_SATIR_SAYISI)

    def _oku() -> None:
        assert surec.stdout is not None
        for ham in surec.stdout:
            metin = ham.decode("utf-8", "replace").rstrip("\r\n")
            son.append(metin)
            logger.debug("  %s", metin)
            if satir is not None:
                try:
                    satir(metin)
                except Exception:  # noqa: BLE001 — sayaç hatası komutu bozmasın
                    logger.exception("satır dinleyicisi hata verdi")

    okuyucu = threading.Thread(target=_oku, name="komut-cikti", daemon=True)
    okuyucu.start()
    try:
        while surec.poll() is None:
            if iptal.wait(ayarlar.IPTAL_YOKLAMA_SN):
                _surec_grubunu_durdur(surec)
                raise IptalEdildi()
    finally:
        if surec.poll() is None:
            _surec_grubunu_durdur(surec)
        okuyucu.join(timeout=DURDURMA_SURESI_SN)
    rc = int(surec.returncode)
    logger.info("komut bitti: rc=%d", rc)
    return KomutSonucu(rc, list(son))


def gecen(t0: float) -> float:
    """Gerçek geçen süre (sn, bir ondalık)."""
    return round(time.monotonic() - t0, 1)
