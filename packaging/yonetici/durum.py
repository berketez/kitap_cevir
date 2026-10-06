"""Kurulum kökünün yerleşimi ve kalıcı kurulum durumu (TASARIM B.8, D, E.7).

Yalnız stdlib: yönetici DMG'deki taşınabilir Python'la çalışır (TASARIM A.2).

Bu modül üç şeyin TEK kaynağıdır:

1. **Kök yerleşimi** (``KokDuzeni``): ``<kök>`` altındaki klasör ve dosya
   adları. Yönetici (K4: ``surecler.py`` Streamlit ortamı, ``sunucu.py``) ve
   kurulum adımları yolları buradan okur; yol kodda ikinci kez yazılmaz.
2. **Durum dosyaları**: ``durum/kurulum.json`` (bileşen ve adım kayıtları),
   ``durum/tex.json`` (etkin TeX), ``durum/olcumler.jsonl`` (adım ölçümleri).
   Lisans onayları ``lisans.py``'dedir ama aynı atomik yazıcıyı kullanır.
3. **Bileşen durumu hükmü** (``bilesen_durumu``): "kurulu" / "eksik" /
   "bozuk" / "guncellenecek" (B.5 ``GET /api/durum``). "kuruluyor" hükmü
   süren işi bilen ``kurulum.Kurulum``'dan gelir.

Yazma kuralı: her JSON dosyası geçici dosyaya yazılıp ``fsync`` edilir ve
``os.replace`` ile atomik olarak yerine konur. Yarıda kesilen yazma eski
dosyayı bozmaz; bu, kurulumun "yarıda kalınca sürdürülebilir" olmasının
dayanağıdır.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import tempfile
import threading
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import unquote

try:
    from . import ayarlar, disk
except ImportError:  # düz betik olarak yüklendi (sys.path[0] = packaging/yonetici)
    import ayarlar  # type: ignore[no-redef]
    import disk  # type: ignore[no-redef]

# ---------------------------------------------------------------------------
# Sabitler (yalnız bu modülde; yerleşim adları B.8'den)
# ---------------------------------------------------------------------------
SEMA_SURUMU = 1

DURUM_DIZINI = "durum"
KURULUM_DOSYASI = "kurulum.json"
TEX_DOSYASI = "tex.json"
OLCUM_DOSYASI = "olcumler.jsonl"
LISANS_ONAY_DOSYASI = "lisans_onaylari.json"
TEX_ALGILAMA_DOSYASI = "tex_algilama.json"   # kurulu TeX sınamasının önbelleği
KILIT_DOSYASI = "kilit"                      # yöneticinin flock'u (TASARIM B.6)
CALISAN_DOSYASI = "calisan.json"             # çalışan örnek + torun süreçler (B.6)

CALISMA_ZAMANI_DIZINI = "calisma_zamani"
CALISMA_ZAMANI_ONEKI = "python-"
TAMAM_ISARETI = ".tamam"          # çalışma zamanı klasöründe: arşivin SHA-256'sı
BIN_DIZINI = "bin"
UV_ADI = "uv"
APP_DIZINI = "app"
GUNCEL_BAGI = "guncel"
VENV_DIZINI = "venv"
TEX_DIZINI = "tex"
MODELLER_DATALAB = "modeller/datalab"
# marker'ın FONT_PATH'i (TASARIM B.8): yazıtipi site-packages'a değil buraya.
FONT_GORELI = "modeller/datalab/fonts/GoNotoCurrent-Regular.ttf"
VERI_DIZINI = "veri"
INDIRILEN_DIZINI = ayarlar.INDIRILEN_DIZINI   # tek kaynak: ayarlar.py
TEKERLEK_DIZINI = "tekerlek"                  # indirilen/tekerlek: uv --find-links deposu
LISANSLAR_DIZINI = "LICENSES"
# venv/lib/<bu>/site-packages. Kilitler CPython 3.12 için üretildi; venv'de
# tek bir python3.* klasörü varsa o kullanılır (KokDuzeni.site_packages).
PYTHON_SURUM_DIZINI = "python3.12"

# Bileşen durumları (B.5)
DURUM_KURULU = "kurulu"
DURUM_EKSIK = "eksik"
DURUM_BOZUK = "bozuk"
DURUM_KURULUYOR = "kuruluyor"
DURUM_GUNCELLENECEK = "guncellenecek"

# python-build-standalone arşiv adı: cpython-<sürüm>+<yapı>-<hedef>-install_only.tar.gz
_PBS_ADI = re.compile(r"cpython-(?P<etiket>[0-9][0-9.]*\+[0-9]+)-")
# Kilit satırı: "ad==sürüm \" (uv pip compile --generate-hashes çıktısı)
_KILIT_SATIRI = re.compile(r"^(?P<ad>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<surum>[^\s;\\]+)")
_KILIT_OZETI = re.compile(r"--hash=sha256:(?P<ozet>[0-9a-fA-F]{64})")
_AD_NORMAL = re.compile(r"[-_.]+")


# ---------------------------------------------------------------------------
# Kök yerleşimi
# ---------------------------------------------------------------------------
class KokDuzeni:
    """``<kök>`` altındaki yolların tek kaynağı (TASARIM B.8).

    Yollar yalnız hesaplanır; klasör oluşturmaz. ``kok`` mutlak olmalıdır.
    """

    def __init__(self, kok: os.PathLike | str):
        kok = Path(kok)
        if not kok.is_absolute():
            raise ValueError(f"kurulum kökü mutlak yol olmalı: {kok}")
        self.kok = kok

    # -- durum ---------------------------------------------------------------
    @property
    def durum_dizini(self) -> Path:
        return self.kok / DURUM_DIZINI

    @property
    def kurulum_dosyasi(self) -> Path:
        return self.durum_dizini / KURULUM_DOSYASI

    @property
    def tex_dosyasi(self) -> Path:
        return self.durum_dizini / TEX_DOSYASI

    @property
    def olcum_dosyasi(self) -> Path:
        return self.durum_dizini / OLCUM_DOSYASI

    @property
    def lisans_onay_dosyasi(self) -> Path:
        return self.durum_dizini / LISANS_ONAY_DOSYASI

    @property
    def tex_algilama_dosyasi(self) -> Path:
        return self.durum_dizini / TEX_ALGILAMA_DOSYASI

    @property
    def kilit_dosyasi(self) -> Path:
        return self.durum_dizini / KILIT_DOSYASI

    @property
    def calisan_dosyasi(self) -> Path:
        return self.durum_dizini / CALISAN_DOSYASI

    # -- çalışma zamanı ve araçlar ------------------------------------------
    @property
    def calisma_zamani_dizini(self) -> Path:
        return self.kok / CALISMA_ZAMANI_DIZINI

    def calisma_zamani(self, etiket: str) -> Path:
        """``calisma_zamani/python-<etiket>`` (ör. etiket ``3.12.14+20260924``)."""
        return self.calisma_zamani_dizini / f"{CALISMA_ZAMANI_ONEKI}{etiket}"

    def calisma_zamani_python(self, etiket: str) -> Path:
        return self.calisma_zamani(etiket) / "bin" / "python3"

    @property
    def uv(self) -> Path:
        return self.kok / BIN_DIZINI / UV_ADI

    @property
    def app_guncel(self) -> Path:
        return self.kok / APP_DIZINI / GUNCEL_BAGI

    # -- venv -----------------------------------------------------------------
    @property
    def venv(self) -> Path:
        return self.kok / VENV_DIZINI

    @property
    def venv_python(self) -> Path:
        return self.venv / "bin" / "python"

    @property
    def site_packages(self) -> Path:
        """``venv/lib/python3.X/site-packages``: venv'de tek sürüm klasörü varsa o."""
        lib = self.venv / "lib"
        try:
            adaylar = sorted(p for p in lib.glob("python3.*/site-packages") if p.is_dir())
        except OSError:
            adaylar = []
        if len(adaylar) == 1:
            return adaylar[0]
        return lib / PYTHON_SURUM_DIZINI / "site-packages"

    # -- TeX ------------------------------------------------------------------
    @property
    def tex(self) -> Path:
        return self.kok / TEX_DIZINI

    @property
    def tex_bin(self) -> Path:
        return self.tex / "bin"

    @property
    def tectonic(self) -> Path:
        return self.tex_bin / "tectonic"

    @property
    def tex_shim(self) -> Path:
        return self.tex / "shim"

    @property
    def tex_cache(self) -> Path:
        return self.tex / "cache"

    # -- modeller ve veri -----------------------------------------------------
    @property
    def modeller_datalab(self) -> Path:
        """``MODEL_CACHE_DIR`` (Surya/Marker; lisans K5)."""
        return self.kok / MODELLER_DATALAB

    @property
    def font_yolu(self) -> Path:
        """``FONT_PATH`` (marker yazıtipi; TASARIM B.8)."""
        return self.kok / FONT_GORELI

    @property
    def veri(self) -> Path:
        """``KITAPCEVIR_VERI_KOKU`` (TASARIM F1)."""
        return self.kok / VERI_DIZINI

    @property
    def indirilen(self) -> Path:
        return self.kok / INDIRILEN_DIZINI

    @property
    def tekerlek_deposu(self) -> Path:
        """``indirilen/tekerlek``: grubun tekerlekleri kurulana kadar burada durur."""
        return self.indirilen / TEKERLEK_DIZINI

    @property
    def lisanslar(self) -> Path:
        return self.kok / LISANSLAR_DIZINI

    def kok_yolu(self, goreli: str) -> Path:
        """Manifestteki köke göre yolu (``hedef``) mutlak yola çevirir.

        Kural tek yerdedir: ``disk.guvenli_yol`` (``..``, mutlak yol, ``~``,
        boş parça reddedilir; hata ``disk.GecersizYol``, bir ``ValueError``).
        """
        return disk.guvenli_yol(self.kok, goreli)

    def goreli(self, yol: os.PathLike | str) -> str:
        """Kök altındaki mutlak yolu köke göre ``/``'lı yola çevirir."""
        return Path(yol).relative_to(self.kok).as_posix()


def calisma_zamani_etiketi(oge: Mapping[str, Any]) -> str:
    """Manifestin ``uygulama.calisma_zamani`` öğesinden sürüm etiketi.

    Ad python-build-standalone'unkidir:
    ``cpython-3.12.14+20260924-aarch64-apple-darwin-install_only.tar.gz``
    -> ``3.12.14+20260924``. URL'deki ``%2B`` çözülür.
    """
    for alan in ("url", "hedef", "kimlik"):
        deger = oge.get(alan)
        if isinstance(deger, str):
            bulunan = _PBS_ADI.search(unquote(deger.rsplit("/", 1)[-1]))
            if bulunan:
                return bulunan.group("etiket")
    raise ValueError("çalışma zamanı öğesinden sürüm etiketi çıkarılamadı")


# ---------------------------------------------------------------------------
# JSON: okuma ve atomik yazma
# ---------------------------------------------------------------------------
def simdi() -> str:
    """Yerel saat dilimiyle ISO 8601 zaman (saniye)."""
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def json_oku(yol: Path, varsayilan: Any = None) -> Any:
    """JSON dosyasını okur; yoksa ya da bozuksa ``varsayilan``'ı döndürür."""
    try:
        with open(yol, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return varsayilan
    except (OSError, ValueError):
        return varsayilan


def _dizini_esitle(dizin: Path) -> None:
    try:
        fd = os.open(dizin, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomik_yaz(yol: Path, icerik: bytes, kip: Optional[int] = None) -> None:
    """``icerik``'i geçici dosyaya yazar, ``fsync`` eder, ``os.replace`` ile yerine koyar."""
    yol = Path(yol)
    yol.parent.mkdir(parents=True, exist_ok=True)
    fd, gecici = tempfile.mkstemp(prefix=f".{yol.name}.", suffix=".yeni", dir=yol.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(icerik)
            f.flush()
            os.fsync(f.fileno())
        if kip is not None:
            os.chmod(gecici, kip)
        os.replace(gecici, yol)
    except BaseException:
        try:
            os.unlink(gecici)
        except OSError:
            pass
        raise
    _dizini_esitle(yol.parent)


def json_yaz_atomik(yol: Path, veri: Any) -> None:
    metin = json.dumps(veri, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    atomik_yaz(Path(yol), metin.encode("utf-8"))


def satir_ekle(yol: Path, veri: Mapping[str, Any]) -> None:
    """JSONL dosyasına tek satır ekler (ölçüm kaydı)."""
    yol = Path(yol)
    yol.parent.mkdir(parents=True, exist_ok=True)
    with open(yol, "a", encoding="utf-8") as f:
        f.write(json.dumps(dict(veri), ensure_ascii=False, sort_keys=True) + "\n")


def sha256_dosya(yol: Path, parca: Optional[int] = None) -> str:
    """Dosyanın SHA-256'sı (küçük harf onaltılık).

    Akışlı okuma ``disk.ozet_hesapla``'dır; parça boyu verilmezse
    ``ayarlar.PARCA_BAYT`` (tek kaynak; sayı burada yazılmaz).
    """
    return disk.ozet_hesapla(yol, "sha256", parca=parca)


def kanonik_sha256(veri: Any) -> str:
    """JSON değerinin kanonik (sıralı anahtar, boşluksuz) biçiminin SHA-256'sı."""
    metin = json.dumps(veri, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(metin.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# kurulum.json
# ---------------------------------------------------------------------------
def _bos_kurulum() -> Dict[str, Any]:
    return {"sema_surumu": SEMA_SURUMU, "bilesenler": {}, "adimlar": {}, "son_is": None}


class KurulumDurumu:
    """``durum/kurulum.json`` üstünde kilitli oku-değiştir-yaz.

    Yapı::

        {"sema_surumu": 1,
         "bilesenler": {<kimlik>: {"imza", "surum", "zaman", "dosyalar": {<göreli yol>: bayt},
                                   "gruplar": {<grup>: <kilit SHA-256>}, ...}},
         "adimlar":    {<bileşen>/<adım anahtarı>: {"zaman", ...adımın kaydı}},
         "son_is":     {"plan_kimligi", "bilesenler", "sonuc", "zaman", "hata"?}}

    Süreç içi eşzamanlılık kilitle korunur; süreçler arası tek yazıcıyı
    yöneticinin ``durum/kilit`` ``flock``'u sağlar (TASARIM B.6).
    """

    def __init__(self, duzen: KokDuzeni):
        self.duzen = duzen
        self._kilit = threading.RLock()

    # -- ham okuma/yazma ------------------------------------------------------
    def oku(self) -> Dict[str, Any]:
        with self._kilit:
            veri = json_oku(self.duzen.kurulum_dosyasi, None)
            if not isinstance(veri, dict) or veri.get("sema_surumu") != SEMA_SURUMU:
                return _bos_kurulum()
            for anahtar, bos in (("bilesenler", {}), ("adimlar", {})):
                if not isinstance(veri.get(anahtar), dict):
                    veri[anahtar] = bos
            return veri

    def _yaz(self, veri: Mapping[str, Any]) -> None:
        json_yaz_atomik(self.duzen.kurulum_dosyasi, veri)

    # -- bileşen kayıtları ----------------------------------------------------
    def bilesen(self, kimlik: str) -> Optional[Dict[str, Any]]:
        kayit = self.oku()["bilesenler"].get(kimlik)
        return kayit if isinstance(kayit, dict) else None

    def bilesen_kaydet(self, kimlik: str, kayit: Mapping[str, Any]) -> None:
        with self._kilit:
            veri = self.oku()
            veri["bilesenler"][kimlik] = dict(kayit)
            self._yaz(veri)

    def bilesen_sil(self, kimlik: str) -> None:
        """Bileşen kaydını ve ona ait adım kayıtlarını siler."""
        with self._kilit:
            veri = self.oku()
            veri["bilesenler"].pop(kimlik, None)
            onek = f"{kimlik}/"
            for anahtar in [a for a in veri["adimlar"] if a.startswith(onek)]:
                del veri["adimlar"][anahtar]
            self._yaz(veri)

    # -- adım kayıtları (idempotent adımların "yapıldı" işareti) ---------------
    def adim(self, bilesen: str, anahtar: str) -> Optional[Dict[str, Any]]:
        kayit = self.oku()["adimlar"].get(f"{bilesen}/{anahtar}")
        return kayit if isinstance(kayit, dict) else None

    def adim_kaydet(self, bilesen: str, anahtar: str, kayit: Mapping[str, Any]) -> None:
        with self._kilit:
            veri = self.oku()
            yeni = dict(kayit)
            yeni.setdefault("zaman", simdi())
            veri["adimlar"][f"{bilesen}/{anahtar}"] = yeni
            self._yaz(veri)

    def adim_sil(self, bilesen: str, anahtar: str) -> None:
        with self._kilit:
            veri = self.oku()
            if veri["adimlar"].pop(f"{bilesen}/{anahtar}", None) is not None:
                self._yaz(veri)

    # -- son iş -------------------------------------------------------------------
    def son_is(self) -> Optional[Dict[str, Any]]:
        kayit = self.oku().get("son_is")
        return kayit if isinstance(kayit, dict) else None

    def son_is_kaydet(self, kayit: Mapping[str, Any]) -> None:
        with self._kilit:
            veri = self.oku()
            yeni = dict(kayit)
            yeni.setdefault("zaman", simdi())
            veri["son_is"] = yeni
            self._yaz(veri)


# ---------------------------------------------------------------------------
# tex.json (etkin TeX; K4 surecler.py PATH'i buradan kurar)
# ---------------------------------------------------------------------------
def tex_oku(duzen: KokDuzeni) -> Optional[Dict[str, Any]]:
    """``durum/tex.json``: ``{"tur", "xelatex", "path_dizini", "surum", "sinama", "zaman"}``.

    ``path_dizini`` Streamlit ortamının PATH'ine ilk sırada eklenecek klasördür
    (``<kök>/tex/shim`` ya da ``/Library/TeX/texbin``; TASARIM B.8).
    """
    veri = json_oku(duzen.tex_dosyasi, None)
    return veri if isinstance(veri, dict) and veri.get("xelatex") else None


def tex_yaz(duzen: KokDuzeni, tur: str, xelatex: Path, *, surum: str = "",
            sinama: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    veri = {
        "tur": tur,
        "xelatex": str(xelatex),
        "path_dizini": str(Path(xelatex).parent),
        "surum": surum,
        "sinama": dict(sinama or {}),
        "zaman": simdi(),
    }
    json_yaz_atomik(duzen.tex_dosyasi, veri)
    return veri


def tex_sil(duzen: KokDuzeni) -> None:
    try:
        duzen.tex_dosyasi.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Kilit dosyası ve kurulu paketler (venv'i içe aktarma yapmadan denetleme)
# ---------------------------------------------------------------------------
def normal_ad(ad: str) -> str:
    """PEP 503 benzeri ad normalleştirme (küçük harf, ``-_.`` → ``_``)."""
    return _AD_NORMAL.sub("_", ad).lower()


@dataclass(frozen=True)
class KilitPaketi:
    """Kilit dosyasındaki tek paket: ad, sürüm ve izinli SHA-256 özetleri."""
    ad: str
    surum: str
    ozetler: Tuple[str, ...]

    @property
    def normal(self) -> str:
        return normal_ad(self.ad)


def kilit_oku(kilit: Path) -> List[KilitPaketi]:
    """``uv pip compile --generate-hashes`` kilidini okur (ters bölü devamları birleşir).

    Yorum satırları (``#``) atlanır. Her ``ad==sürüm`` girdisinin
    ``--hash=sha256:`` özetleri toplanır. Aynı paket iki kez geçerse
    ``ValueError`` (kilit bozuk).
    """
    with open(kilit, "r", encoding="utf-8") as f:
        ham = f.read()
    mantiksal: List[str] = []
    birikim = ""
    for satir in ham.splitlines():
        govde = satir.split(" #", 1)[0] if not satir.lstrip().startswith("#") else ""
        govde = govde.rstrip()
        if govde.endswith("\\"):
            birikim += govde[:-1] + " "
            continue
        birikim += govde
        if birikim.strip():
            mantiksal.append(birikim.strip())
        birikim = ""
    if birikim.strip():
        mantiksal.append(birikim.strip())

    paketler: List[KilitPaketi] = []
    gorulen: set = set()
    for satir in mantiksal:
        bulunan = _KILIT_SATIRI.match(satir)
        if not bulunan:
            continue
        ad, surum = bulunan.group("ad"), bulunan.group("surum")
        if normal_ad(ad) in gorulen:
            raise ValueError(f"kilitte {ad} iki kez geçiyor: {kilit}")
        gorulen.add(normal_ad(ad))
        ozetler = tuple(o.group("ozet").lower() for o in _KILIT_OZETI.finditer(satir))
        paketler.append(KilitPaketi(ad, surum, ozetler))
    return paketler


def kilit_paketleri(kilit: Path) -> List[Tuple[str, str]]:
    """``uv pip compile --generate-hashes`` kilidindeki ``(ad, sürüm)`` çiftleri."""
    return [(p.ad, p.surum) for p in kilit_oku(kilit)]


def kurulu_dagitimlar(site_packages: Path) -> Dict[str, str]:
    """``site-packages`` içindeki ``*.dist-info`` klasörlerinden ``{normal ad: sürüm}``."""
    sonuc: Dict[str, str] = {}
    try:
        girdiler = list(os.scandir(site_packages))
    except OSError:
        return sonuc
    for girdi in girdiler:
        if not girdi.name.endswith(".dist-info") or not girdi.is_dir():
            continue
        govde = girdi.name[: -len(".dist-info")]
        if "-" not in govde:
            continue
        ad, surum = govde.rsplit("-", 1)
        sonuc[normal_ad(ad)] = surum
    return sonuc


def eksik_paketler(site_packages: Path, paketler: Iterable[Tuple[str, str]]) -> List[str]:
    """Kilitteki sürümüyle kurulu OLMAYAN paketler (``ad==sürüm`` listesi)."""
    kurulu = kurulu_dagitimlar(site_packages)
    eksik = []
    for ad, surum in paketler:
        if kurulu.get(normal_ad(ad)) != surum:
            eksik.append(f"{ad}=={surum}")
    return eksik


# ---------------------------------------------------------------------------
# Bileşen durumu hükmü
# ---------------------------------------------------------------------------
def bilesen_imzasi(bilesen: Mapping[str, Any]) -> str:
    """Manifestteki bileşen kaydının kanonik SHA-256'sı.

    Kilit dosyası, öğe SHA'ları ya da adımlar değişince imza değişir; kurulu
    kaydın imzası farklıysa bileşen "guncellenecek" sayılır (TASARIM G.2, G.4).
    ``boyut`` ve metin alanları imzaya girmez: yalnız ölçüm notu değişen sürüm
    bileşeni yeniden kurdurmaz.
    """
    cekirdek = {k: bilesen.get(k) for k in ("kimlik", "surum", "ogeler", "adimlar",
                                             "dogrulama", "bagimliliklar")}
    return kanonik_sha256(cekirdek)


def dosyalar_yerinde(duzen: KokDuzeni, dosyalar: Mapping[str, Any]) -> List[str]:
    """Kayıttaki ``{göreli yol: bayt}`` dosyalarından eksik ya da boyu farklı olanlar.

    Hızlı denetimdir (SHA okumaz); tam doğrulama "Onar"da yapılır (E.7).
    """
    sorunlu = []
    for goreli, bayt in dosyalar.items():
        try:
            yol = duzen.kok_yolu(goreli)
            boy = yol.stat().st_size
        except (OSError, ValueError):
            sorunlu.append(goreli)
            continue
        if isinstance(bayt, int) and boy != bayt:
            sorunlu.append(goreli)
    return sorunlu


def bilesen_durumu(duzen: KokDuzeni, kurulum: KurulumDurumu,
                   bilesen: Mapping[str, Any]) -> Tuple[str, str]:
    """``(durum, neden)``: "kurulu" / "eksik" / "bozuk" / "guncellenecek".

    - Kayıt yoksa: eksik.
    - Kayıttaki imza manifestinkinden farklıysa: güncellenecek.
    - Kayıttaki dosyalar eksik/boyu farklıysa ya da kayıtlı paket grubunun
      kilitteki bir paketi venv'de yoksa: bozuk (neden ilk sorunu söyler).
    """
    kimlik = str(bilesen.get("kimlik"))
    kayit = kurulum.bilesen(kimlik)
    if not kayit:
        return DURUM_EKSIK, ""
    if kayit.get("imza") != bilesen_imzasi(bilesen):
        return DURUM_GUNCELLENECEK, "bileşen bu sürümde değişti"
    dosyalar = kayit.get("dosyalar")
    if isinstance(dosyalar, dict):
        sorunlu = dosyalar_yerinde(duzen, dosyalar)
        if sorunlu:
            return DURUM_BOZUK, f"eksik ya da bozuk dosya: {sorunlu[0]}"
    # Kökün dışındaki, bizim kurmadığımız ama bileşenin dayandığı dosyalar
    # (MacTeX / kurulu TeX'in xelatex'i). Kaldırıldıysa bileşen bozuktur.
    harici = kayit.get("harici")
    if isinstance(harici, list):
        for yol in harici:
            if not (isinstance(yol, str) and os.path.isfile(yol) and os.access(yol, os.X_OK)):
                return DURUM_BOZUK, f"dosya bulunamadı: {yol}"
    gruplar = kayit.get("gruplar")
    if isinstance(gruplar, dict) and gruplar:
        if not duzen.venv_python.exists():
            return DURUM_BOZUK, "Python ortamı (venv) yok"
        for grup, bilgi in gruplar.items():
            paketler = bilgi.get("paketler") if isinstance(bilgi, dict) else None
            if not isinstance(paketler, list):
                continue
            eksik = eksik_paketler(duzen.site_packages, [tuple(p) for p in paketler])
            if eksik:
                return DURUM_BOZUK, f"{grup} paketi eksik: {eksik[0]}"
    return DURUM_KURULU, ""


def fazla_paketler(site_packages: Path, izinli: Iterable[str]) -> List[str]:
    """venv'de kurulu olup izinli adlar (kurulu grupların kilitleri) dışında kalanlar.

    ``izinli`` normal adlardır (``normal_ad``). Güncellemede (TASARIM G.4)
    kilitten çıkan paketleri kaldırmak için kullanılır. Sıralı döner.
    """
    izin = {normal_ad(a) for a in izinli}
    return sorted(ad for ad in kurulu_dagitimlar(site_packages) if ad not in izin)
