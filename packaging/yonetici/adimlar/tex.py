"""TeX adımları: tectonic ısıtması, kurulu TeX algılama + şablon sınaması, MacTeX.

TASARIM D.3:

- ``tectonic_isit``: ``packaging/tex/isitma.tex`` gerçek derlemeyle
  çalıştırılır; ilerleme tectonic'in her ``note: downloading`` satırıdır
  (bayt yok, tectonic bayt bildirmiyor). Sürümde ölçülen sayı
  ``bilesen.sayac_beklenen.n``'dir ("214 dosya indi (sürümde ölçülen: 352)").
- Kurulu TeX: önce ``/Library/TeX/texbin/xelatex``, sonra
  ``/usr/local/texlive/<yıl>/bin/universal-darwin/xelatex`` (yıllar azalan).
  PATH'e bakılmaz (Finder ortamının PATH'i yok). Algılanan xelatex ile
  ``isitma.tex`` derlenir; sonuç ``durum/tex_algilama.json``'da xelatex'in
  gerçek yolu + boyutu + değişme zamanı + belgenin SHA-256'sıyla saklanır,
  yani ``/api/durum`` her açılışta yeniden derlemez.
- MacTeX: ``open <pkg>`` ile Installer.app açılır, ``MACTEX_YOKLAMA_SN``'de
  bir xelatex dosyasına bakılır (gerçek denetim). "Vazgeç" = iş iptali.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

try:
    from .. import ayarlar
    from ..durum import KokDuzeni, json_oku, json_yaz_atomik, sha256_dosya, simdi
except ImportError:  # düz betik olarak yüklendi
    import ayarlar  # type: ignore[no-redef]
    from durum import (KokDuzeni, json_oku, json_yaz_atomik,  # type: ignore[no-redef]
                       sha256_dosya, simdi)

from .ortak import (KOD_DOSYA_YOK, KOD_KOMUT, KOD_TEX, SISTEM_PATH, AdimHatasi,
                    AdimSonucu, Baglam, gecen, komut_calistir, temel_ortam)

# ---------------------------------------------------------------------------
# Sabitler (yalnız bu modülde)
# ---------------------------------------------------------------------------
TEXBIN_XELATEX = "/Library/TeX/texbin/xelatex"
TEXLIVE_DESENI = "/usr/local/texlive/*/bin/universal-darwin/xelatex"
_TEXLIVE_YILI = re.compile(r"/texlive/(\d{4})/")
ISITMA_BELGESI = "packaging/tex/isitma.tex"          # uygulama köküne göre
ISITMA_ADI = "isitma"
TECTONIC_INDIRME_ISARETI = "note: downloading"
MACOS_ACICI = "/usr/bin/open"
# latex_pipeline/compiler.py'nin çağrısıyla aynı seçenekler + ilk hatada dur.
_XELATEX_SECENEKLERI = ("-interaction=nonstopmode", "-halt-on-error", "-file-line-error",
                        "-no-shell-escape")
_TECTONIC_SECENEKLERI = ("-X", "compile", "--untrusted", "-Z", "continue-on-errors")
_ILK_HATA = re.compile(r"^(! .+|\S+:\d+: .+)$")

MSG_TEX_YOK = "Bu Mac'te kurulu TeX (TeX Live ya da MacTeX) bulunamadı."
MSG_TEX_SINAMA = "Kurulu TeX bu şablonu derleyemedi: {hata}"
MSG_TEX_HATA_BILINMIYOR = "hata satırı bulunamadı (günlüğe bak)"
MSG_MACTEX_BEKLE = "macOS kurulum programı açıldı; bitince bu ekran kendiliğinden devam eder."
MSG_ISITMA = "tectonic ısıtma derlemesi başarısız."


# ---------------------------------------------------------------------------
# Algılama
# ---------------------------------------------------------------------------
def _yil(yol: str) -> int:
    bulunan = _TEXLIVE_YILI.search(yol)
    return int(bulunan.group(1)) if bulunan else 0


def xelatex_adaylari() -> List[str]:
    """Çalıştırılabilir xelatex adayları (aynı gerçek dosya bir kez), öncelik sırasıyla."""
    adaylar = [TEXBIN_XELATEX] + sorted(glob.glob(TEXLIVE_DESENI), key=_yil, reverse=True)
    sonuc: List[str] = []
    gorulen = set()
    for aday in adaylar:
        if not (os.path.isfile(aday) and os.access(aday, os.X_OK)):
            continue
        gercek = os.path.realpath(aday)
        if gercek in gorulen:
            continue
        gorulen.add(gercek)
        sonuc.append(aday)
    return sonuc


def xelatex_surumu(xelatex: str, iptal: Optional[threading.Event] = None) -> str:
    """``xelatex --version`` çıktısının ilk satırı (ör. "XeTeX 3.141592653-2.6-0.999997 (TeX Live 2025)")."""
    sonuc = komut_calistir([xelatex, "--version"], iptal=iptal or threading.Event(),
                           env=temel_ortam({"PATH": f"{os.path.dirname(xelatex)}:{SISTEM_PATH}"}))
    return sonuc.son_satirlar[0].strip() if sonuc.rc == 0 and sonuc.son_satirlar else ""


def _ilk_hata(gunluk: Path, son_satirlar: List[str]) -> str:
    try:
        satirlar = gunluk.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        satirlar = []
    for satir in satirlar + list(son_satirlar):
        if _ILK_HATA.match(satir.strip()):
            return satir.strip()
    return MSG_TEX_HATA_BILINMIYOR


def tex_sina(duzen: KokDuzeni, xelatex: str, belge: Path,
             iptal: Optional[threading.Event] = None) -> Dict[str, Any]:
    """``belge``'yi ``xelatex`` ile geçici klasörde derler: ``{"gecti", "hata", "sure_sn"}``."""
    t0 = time.monotonic()
    duzen.indirilen.mkdir(parents=True, exist_ok=True)
    klasor = Path(tempfile.mkdtemp(prefix="tex-sinama-", dir=duzen.indirilen))
    try:
        shutil.copyfile(belge, klasor / f"{ISITMA_ADI}.tex")
        sonuc = komut_calistir(
            [xelatex, *_XELATEX_SECENEKLERI, f"{ISITMA_ADI}.tex"],
            iptal=iptal or threading.Event(),
            env=temel_ortam({"PATH": f"{os.path.dirname(xelatex)}:{SISTEM_PATH}"}), cwd=klasor)
        pdf = klasor / f"{ISITMA_ADI}.pdf"
        gecti = sonuc.rc == 0 and pdf.is_file() and pdf.stat().st_size > 0
        hata = "" if gecti else _ilk_hata(klasor / f"{ISITMA_ADI}.log", sonuc.son_satirlar)
    finally:
        shutil.rmtree(klasor, ignore_errors=True)
    return {"gecti": gecti, "hata": hata, "sure_sn": gecen(t0)}


def _dosya_imzasi(yol: str) -> Optional[str]:
    try:
        gercek = os.path.realpath(yol)
        st = os.stat(gercek)
    except OSError:
        return None
    return f"{gercek}:{st.st_size}:{st.st_mtime_ns}"


def _onbellek_oku(duzen: KokDuzeni) -> Dict[str, Any]:
    onbellek = json_oku(duzen.tex_algilama_dosyasi, None)
    kayitlar = onbellek.get("kayitlar") if isinstance(onbellek, dict) else None
    return dict(kayitlar) if isinstance(kayitlar, dict) else {}


def _bilgi(duzen: KokDuzeni, uygulama: Path, xelatex: str, sina: bool,
           iptal: Optional[threading.Event], kayitlar: Dict[str, Any]
           ) -> Tuple[Dict[str, Any], bool]:
    """Tek aday için bilgi; ``kayitlar`` (önbellek) yerinde güncellenir, değiştiyse True."""
    belge = Path(uygulama) / ISITMA_BELGESI
    belge_sha = sha256_dosya(belge) if belge.is_file() else ""
    gercek = os.path.realpath(xelatex)
    imza = f"{_dosya_imzasi(xelatex)}|{belge_sha}"
    kayit = kayitlar.get(gercek)
    degisti = False
    if not (isinstance(kayit, dict) and kayit.get("imza") == imza
            and (kayit.get("sinama") is not None or not sina)):
        sinama = tex_sina(duzen, xelatex, belge, iptal) if (sina and belge_sha) else None
        kayit = {"imza": imza, "surum": xelatex_surumu(xelatex, iptal), "sinama": sinama,
                 "zaman": simdi()}
        if sinama is not None:
            kayitlar[gercek] = kayit
            degisti = True
    return ({"yol": xelatex, "gercek_yol": gercek, "surum": kayit.get("surum", ""),
             "sinama": kayit.get("sinama")}, degisti)


def xelatex_bilgisi(duzen: KokDuzeni, uygulama: Path, xelatex: str, *, sina: bool = True,
                    iptal: Optional[threading.Event] = None) -> Dict[str, Any]:
    """Tek xelatex için ``{"yol", "gercek_yol", "surum", "sinama"}`` (önbellekli).

    Önbellek anahtarı: xelatex'in gerçek yolu + boyutu + değişme zamanı +
    ``isitma.tex``'in SHA-256'sı. ``sina=False`` iken önbellekte yoksa
    ``sinama`` None'dır (derleme yapılmaz).
    """
    kayitlar = _onbellek_oku(duzen)
    sonuc, degisti = _bilgi(duzen, uygulama, xelatex, sina, iptal, kayitlar)
    if degisti:
        json_yaz_atomik(duzen.tex_algilama_dosyasi, {"kayitlar": kayitlar})
    return sonuc


def tex_algila(duzen: KokDuzeni, uygulama: Path, *, sina: bool = True,
               iptal: Optional[threading.Event] = None) -> List[Dict[str, Any]]:
    """Bu Mac'teki TeX'ler: ``[{"yol", "gercek_yol", "surum", "sinama"}]`` (B.5 ``tex_algilanan``).

    ``sinama`` = ``{"gecti", "hata", "sure_sn"}`` ya da ``sina=False`` ile ve
    önbellekte yoksa None. Sınama sonucu önbellektedir (``durum/tex_algilama.json``);
    xelatex ya da şablon değişince yeniden derlenir.
    """
    kayitlar = _onbellek_oku(duzen)
    sonuc: List[Dict[str, Any]] = []
    degisti = False
    for yol in xelatex_adaylari():
        bilgi, d = _bilgi(duzen, uygulama, yol, sina, iptal, kayitlar)
        sonuc.append(bilgi)
        degisti = degisti or d
    if degisti:
        json_yaz_atomik(duzen.tex_algilama_dosyasi, {"kayitlar": kayitlar})
    return sonuc


# ---------------------------------------------------------------------------
# Adımlar
# ---------------------------------------------------------------------------
def tex_algila_sina(b: Baglam, adim: Mapping[str, Any], kayit: Optional[dict]) -> AdimSonucu:
    """``{"tur": "tex_algila_sina", "yol": <xelatex mutlak yolu>?}``.

    ``yol`` verilirse (MacTeX: ``/Library/TeX/texbin/xelatex``) yalnız o aday
    sınanır; verilmezse sınamayı geçen ilk aday seçilir.
    """
    istenen = adim.get("yol") or b.tercihler.get("xelatex")
    if not b.yeniden and kayit:
        xelatex = kayit.get("xelatex")
        if (isinstance(xelatex, str) and kayit.get("imza") == _dosya_imzasi(xelatex)
                and (not istenen or istenen == xelatex)):
            return AdimSonucu.kayittan(kayit)
    adaylar = tex_algila(b.duzen, b.uygulama, sina=True, iptal=b.iptal)
    if istenen:
        adaylar = [a for a in adaylar
                   if a["yol"] == istenen or a["gercek_yol"] == os.path.realpath(istenen)]
    if not adaylar:
        raise AdimHatasi(KOD_TEX, MSG_TEX_YOK)
    for aday in adaylar:
        sinama = aday.get("sinama") or {}
        if sinama.get("gecti"):
            return AdimSonucu(harici=[aday["yol"]],
                              tex={"xelatex": aday["yol"], "surum": aday.get("surum", ""),
                                   "sinama": sinama},
                              kayit={"xelatex": aday["yol"],
                                     "imza": _dosya_imzasi(aday["yol"])})
    ilk = adaylar[0].get("sinama") or {}
    raise AdimHatasi(KOD_TEX, MSG_TEX_SINAMA.format(hata=ilk.get("hata") or
                                                    MSG_TEX_HATA_BILINMIYOR))


def _tectonic_surumu(b: Baglam) -> str:
    sonuc = b.komut([b.duzen.tectonic, "--version"])
    return sonuc.son_satirlar[-1].strip() if sonuc.rc == 0 and sonuc.son_satirlar else ""


def _onbellek_dolu(duzen: KokDuzeni) -> bool:
    bicimler = duzen.tex_cache / "formats"
    try:
        return any(bicimler.iterdir())
    except OSError:
        return False


def tectonic_isit(b: Baglam, adim: Mapping[str, Any], kayit: Optional[dict]) -> AdimSonucu:
    """``{"tur": "tectonic_isit", "belge": <uygulamaya göre yol>?}``.

    Ön koşul: ``tex/bin/tectonic`` (``arsiv_ac``) ve ``tex/shim/xelatex``
    (``dosya_yerlestir``) yerinde. ``TECTONIC_CACHE_DIR = <kök>/tex/cache``.
    """
    tectonic = b.duzen.tectonic
    shim = b.duzen.tex_shim / "xelatex"
    for gerekli in (tectonic, shim):
        if not os.access(gerekli, os.X_OK):
            raise AdimHatasi(KOD_DOSYA_YOK, f"tectonic ısıtması için gereken dosya yok: {gerekli}")
    belge = b.uygulama_yolu(str(adim.get("belge") or ISITMA_BELGESI))
    if not belge.is_file():
        raise AdimHatasi(KOD_DOSYA_YOK, f"Isıtma belgesi yok: {belge}")
    belge_sha = sha256_dosya(belge)
    tectonic_boyut = tectonic.stat().st_size
    if (not b.yeniden and kayit and kayit.get("belge_sha256") == belge_sha
            and kayit.get("tectonic_boyut") == tectonic_boyut and _onbellek_dolu(b.duzen)):
        return AdimSonucu.kayittan(kayit)

    beklenen = b.bilesen.get("sayac_beklenen")
    beklenen = beklenen if isinstance(beklenen, Mapping) else {}
    n = beklenen.get("n") if isinstance(beklenen.get("n"), int) else None
    birim = str(beklenen.get("birim") or "dosya")
    sayac = [0]

    def _satir(metin: str) -> None:
        if TECTONIC_INDIRME_ISARETI in metin:
            sayac[0] += 1
            # n sürümde ölçülen sayıdır (manifest); bu kurulumun sayısı değil
            b.bildir("sayac", i=sayac[0], n=n, birim=birim, olculen=n is not None)

    t0 = time.monotonic()
    b.duzen.tex_cache.mkdir(parents=True, exist_ok=True)
    b.duzen.indirilen.mkdir(parents=True, exist_ok=True)
    klasor = Path(tempfile.mkdtemp(prefix="isitma-", dir=b.duzen.indirilen))
    try:
        shutil.copyfile(belge, klasor / f"{ISITMA_ADI}.tex")
        sonuc = b.komut([tectonic, *_TECTONIC_SECENEKLERI, f"{ISITMA_ADI}.tex"],
                        ortam={"TECTONIC_CACHE_DIR": str(b.duzen.tex_cache)}, cwd=klasor,
                        satir=_satir)
        pdf = klasor / f"{ISITMA_ADI}.pdf"
        gecti = sonuc.rc == 0 and pdf.is_file() and pdf.stat().st_size > 0
    finally:
        shutil.rmtree(klasor, ignore_errors=True)
    if not gecti:
        raise AdimHatasi(KOD_TEX, MSG_ISITMA, sonuc.ayrinti)
    sinama = {"gecti": True, "hata": "", "sure_sn": gecen(t0), "indirilen_dosya": sayac[0]}
    return AdimSonucu(tex={"xelatex": str(shim), "surum": _tectonic_surumu(b), "sinama": sinama},
                      yollar=[b.duzen.goreli(b.duzen.tex)],
                      kayit={"belge_sha256": belge_sha, "tectonic_boyut": tectonic_boyut,
                             "indirilen_dosya": sayac[0]})


def _xelatex_imzasi(yol: str) -> Optional[Tuple[str, int]]:
    try:
        gercek = os.path.realpath(yol)
        return gercek, os.stat(gercek).st_mtime_ns
    except OSError:
        return None


def mactex_pkg_ac(b: Baglam, adim: Mapping[str, Any], kayit: Optional[dict]) -> AdimSonucu:
    """``{"tur": "mactex_pkg_ac", "oge": <MacTeX.pkg öğesi>, "yol": <beklenen xelatex>?}``.

    Paket indirilir (manifestteki sabit SHA-256 ile doğrulama indiricinin işidir;
    yansıdaki .sha512'ye bakılmaz, K9 O2), Installer.app
    açılır ve xelatex dosyası belirene (ya da değişene) kadar beklenir.
    Bekleme iptal edilebilir ("Vazgeç").
    """
    oge = b.oge(str(adim.get("oge")))
    xelatex = str(adim.get("yol") or TEXBIN_XELATEX)
    if not b.yeniden and kayit and os.access(xelatex, os.X_OK):
        return AdimSonucu.kayittan(kayit)
    b.indir([oge])
    b.iptal_denetle()
    once = _xelatex_imzasi(xelatex)
    acildi = b.komut([MACOS_ACICI, b.oge_yolu(oge)])
    if acildi.rc != 0:
        raise AdimHatasi(KOD_KOMUT, "macOS kurulum programı açılamadı.", acildi.ayrinti)
    b.bildir("bekleme", neden=MSG_MACTEX_BEKLE, kalan_sn=None)
    while True:
        b.bekle(ayarlar.MACTEX_YOKLAMA_SN)
        simdiki = _xelatex_imzasi(xelatex)
        if simdiki is not None and simdiki != once and os.access(xelatex, os.X_OK):
            break
    return AdimSonucu(harici=[xelatex],
                      tuketilen=[str(oge["kimlik"])] if b.indirilen_altinda_mi(oge) else [],
                      kayit={"xelatex": xelatex})
