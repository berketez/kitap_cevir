"""Paket adımları: venv, ``tekerlek_kur`` (çevrimdışı grup kurulumu), sınama, kaldırma.

TASARIM D.1: tekerlekleri yöneticinin indiricisi indirir; uv yalnız
çevrimdışı kurar::

    uv pip install --offline --no-index --find-links <kök>/indirilen/tekerlek
                   --no-deps --require-hashes -r <etkin kilit>

Etkin kilit, grubun kilit dosyasındaki (``packaging/requirements/<grup>.txt``)
her ``ad==sürüm`` satırıdır; özetleri kilittekilerle manifestteki tekerlek
öğesinin SHA-256'sının birleşimidir. Bu, sürüm çıkarken derlenen kendi
tekerleğimizin (llama-cpp-python; kilitte yalnız kaynak paketinin özeti var)
aynı sürüm için kabul edilmesini sağlar; güven kökü iki dosyada da aynıdır
(ikisi de DMG'deki uygulama arşivinin içindedir). Manifestteki bir tekerlek
kilitte olmayan bir ad/sürüm taşıyorsa kurulum durur.

GGUF: "hazır tekerlek ya da derleme". Adımın ``kaynaktan`` listesindeki bir
paketin tekerleği manifestte yoksa ya da indirilemezse (ör. sürüm deposu
özelken), paket kaynaktan derlenir. Derleme Xcode Komut Satırı Araçları ister
ve çevrimdışı DEĞİLDİR: yapı bağımlılıkları (cmake, scikit-build-core, ninja)
uv'nin yapı yalıtımıyla PyPI'dan iner; kaynak paketi kilitteki özetle doğrulanır.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

try:
    from ..durum import (KilitPaketi, KokDuzeni, calisma_zamani_etiketi, eksik_paketler,
                         kilit_oku, kurulu_dagitimlar, normal_ad, sha256_dosya)
except ImportError:  # düz betik olarak yüklendi
    from durum import (KilitPaketi, KokDuzeni, calisma_zamani_etiketi,  # type: ignore
                       eksik_paketler, kilit_oku, kurulu_dagitimlar, normal_ad, sha256_dosya)

from .ortak import (KOD_CALISMA_ZAMANI, KOD_DOGRULAMA, KOD_DOSYA_YOK, KOD_KILIT, KOD_KOMUT,
                    KOD_KSA_YOK, KOD_MANIFEST, AdimHatasi, AdimSonucu, Baglam, IptalEdildi,
                    logger, temel_ortam)

# ---------------------------------------------------------------------------
# Sabitler (yalnız bu modülde)
# ---------------------------------------------------------------------------
KILIT_KLASORU = "packaging/requirements"        # uygulama köküne göre
ETKIN_KILIT_SABLONU = "kilit-{grup}.txt"        # indirilen/ altında (kurulum sonunda silinir)
XCODE_SELECT = "/usr/bin/xcode-select"
KSA_DERLEYICI = "usr/bin/clang"                 # xcode-select -p klasörüne göre
KSA_ZAMAN_ASIMI_SN = 10
_MODUL_ADI = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")

MSG_KSA_YOK = ("GGUF desteği için Xcode Komut Satırı Araçları gerekir: Terminal'de "
               "'xcode-select --install' komutunu çalıştır, kurulum bitince yeniden dene.")


# ---------------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------------
def tekerlek_adi(dosya_adi: str) -> Optional[Tuple[str, str]]:
    """Tekerlek dosya adından ``(normal ad, sürüm)`` (PEP 427); değilse None."""
    if not dosya_adi.endswith(".whl"):
        return None
    parcalar = dosya_adi[: -len(".whl")].split("-")
    if len(parcalar) < 5:
        return None
    return normal_ad(parcalar[0]), parcalar[1]


def ksa_var(komut: str = XCODE_SELECT) -> bool:
    """Xcode Komut Satırı Araçları gerçekten kurulu mu (``clang`` bir kabuk değil)?

    Temiz Mac'te ``/usr/bin/clang`` yalnız kurulum penceresi açan bir kabuktur
    (prototip); bu yüzden ``xcode-select -p``'nin gösterdiği klasöre bakılır.
    """
    try:
        sonuc = subprocess.run([komut, "-p"], capture_output=True, text=True,
                               timeout=KSA_ZAMAN_ASIMI_SN, env=temel_ortam())
    except (OSError, subprocess.SubprocessError):
        return False
    if sonuc.returncode != 0 or not sonuc.stdout.strip():
        return False
    return os.access(Path(sonuc.stdout.strip()) / KSA_DERLEYICI, os.X_OK)


def _python_yolu(b: Baglam) -> Path:
    uyg = b.manifest.get("uygulama") if isinstance(b.manifest, Mapping) else None
    try:
        etiket = calisma_zamani_etiketi((uyg or {}).get("calisma_zamani") or {})
    except ValueError as exc:
        raise AdimHatasi(KOD_MANIFEST, f"Manifestte çalışma zamanı yok: {exc}") from exc
    python = b.duzen.calisma_zamani_python(etiket)
    if not python.exists():
        raise AdimHatasi(KOD_CALISMA_ZAMANI, f"Python çalışma zamanı bulunamadı: {python}")
    return python


def venv_uygun(duzen: KokDuzeni, python: Path) -> bool:
    """venv var ve bu çalışma zamanından kurulmuş mu (``pyvenv.cfg`` ``home``)?"""
    cfg = duzen.venv / "pyvenv.cfg"
    if not duzen.venv_python.exists():
        return False
    try:
        satirlar = cfg.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for satir in satirlar:
        anahtar, _, deger = satir.partition("=")
        if anahtar.strip() == "home":
            try:
                return Path(deger.strip()).resolve() == python.parent.resolve()
            except OSError:
                return False
    return False


def venv_hazirla(b: Baglam) -> Path:
    """``<kök>/venv``'i manifestteki çalışma zamanından kurar (varsa ve uygunsa dokunmaz).

    Çalışma zamanı değişmişse (güncelleme, TASARIM G.4-4) eski venv silinip
    yeniden kurulur; paket grupları son koşul denetiminde eksik çıkar ve
    yeniden kurulur.
    """
    python = _python_yolu(b)
    if venv_uygun(b.duzen, python):
        return b.duzen.venv_python
    uv = b.duzen.uv
    if not os.access(uv, os.X_OK):
        raise AdimHatasi(KOD_DOSYA_YOK, f"uv kurulu değil: {uv}")
    if b.duzen.venv.exists():
        logger.info("venv bu çalışma zamanına ait değil, yeniden kuruluyor: %s", b.duzen.venv)
        shutil.rmtree(b.duzen.venv)
    sonuc = b.komut([uv, "venv", "--no-config", "--python", python, b.duzen.venv],
                    cwd=b.duzen.kok)
    if sonuc.rc != 0 or not b.duzen.venv_python.exists():
        raise AdimHatasi(KOD_KOMUT, "Python ortamı (venv) kurulamadı.", sonuc.ayrinti)
    return b.duzen.venv_python


def _kilit_yolu(b: Baglam, adim: Mapping[str, Any], grup: str) -> Path:
    goreli = str(adim.get("kilit") or f"{KILIT_KLASORU}/{grup}.txt")
    yol = b.uygulama_yolu(goreli)
    if not yol.is_file():
        raise AdimHatasi(KOD_DOSYA_YOK, f"Kilit dosyası yok: {goreli}")
    return yol


def etkin_kilit_yaz(hedef: Path, paketler: Sequence[KilitPaketi],
                    tekerlekler: Sequence[Mapping[str, Any]],
                    dislanan: Optional[Set[str]] = None) -> List[KilitPaketi]:
    """Kilit + manifest tekerlek özetlerinden uv'ye verilecek kilit dosyasını yazar.

    ``dislanan`` (normal adlar) dosyaya yazılmaz (kaynaktan derlenecekler).
    Manifestteki tekerleğin ad/sürümü kilitte yoksa ``AdimHatasi(KOD_KILIT)``.
    """
    ek: Dict[str, List[str]] = {}
    kilit_surumu = {p.normal: p.surum for p in paketler}
    for oge in tekerlekler:
        ad_surum = tekerlek_adi(PurePosixPath(str(oge.get("hedef", ""))).name)
        if ad_surum is None:
            raise AdimHatasi(KOD_MANIFEST, f"Tekerlek öğesinin adı çözülemedi: {oge.get('hedef')}")
        ad, surum = ad_surum
        if kilit_surumu.get(ad) != surum:
            raise AdimHatasi(KOD_KILIT,
                             f"Manifestteki tekerlek kilitle uyuşmuyor: {ad}=={surum} "
                             f"(kilitte: {kilit_surumu.get(ad) or 'yok'}).")
        ek.setdefault(ad, []).append(str(oge.get("sha256", "")).lower())
    dislanan = dislanan or set()
    yazilan: List[KilitPaketi] = []
    satirlar = []
    for p in paketler:
        if p.normal in dislanan:
            continue
        ozetler = list(p.ozetler) + [o for o in ek.get(p.normal, []) if o not in p.ozetler]
        yazilan.append(KilitPaketi(p.ad, p.surum, tuple(ozetler)))
        satirlar.append(f"{p.ad}=={p.surum} " + " ".join(f"--hash=sha256:{o}" for o in ozetler))
    hedef.parent.mkdir(parents=True, exist_ok=True)
    hedef.write_text("\n".join(satirlar) + "\n", encoding="utf-8")
    return yazilan


def _grup_bilgisi(kilit_sha: str, paketler: Sequence[KilitPaketi]) -> Dict[str, Any]:
    return {"kilit_sha256": kilit_sha, "paketler": [[p.ad, p.surum] for p in paketler]}


# ---------------------------------------------------------------------------
# tekerlek_kur
# ---------------------------------------------------------------------------
def tekerlek_kur(b: Baglam, adim: Mapping[str, Any], kayit: Optional[dict]) -> AdimSonucu:
    """``{"tur": "tekerlek_kur", "grup": <ad>, "kilit": <uygulamaya göre yol>?,
    "kaynaktan": [<paket>]?, "derleme_ortami": {<değişken>: <değer>}?}``.

    Tükettiği öğeler: bileşenin ``tur == "tekerlek"`` öğeleri. Son koşul:
    kilitteki her paket kilitteki sürümüyle venv'de kurulu.
    """
    grup = str(adim.get("grup") or "")
    if not grup:
        raise AdimHatasi(KOD_MANIFEST, "tekerlek_kur adımında 'grup' yok.")
    kilit = _kilit_yolu(b, adim, grup)
    kilit_sha = sha256_dosya(kilit)
    try:
        paketler = kilit_oku(kilit)
    except ValueError as exc:
        raise AdimHatasi(KOD_KILIT, str(exc)) from exc
    python = venv_hazirla(b)
    site = b.duzen.site_packages
    ciftler = [(p.ad, p.surum) for p in paketler]
    eksik = eksik_paketler(site, ciftler)
    tekerlekler = [o for o in b.ogeler if o.get("tur") == "tekerlek"]
    tuketilen = [str(o["kimlik"]) for o in tekerlekler if b.indirilen_altinda_mi(o)]
    if (not b.yeniden and not eksik and kayit
            and kayit.get("kilit_sha256") == kilit_sha):
        return AdimSonucu.kayittan(kayit)

    kurulu = kurulu_dagitimlar(site)

    def _gerekli(oge: Mapping[str, Any]) -> bool:
        ad_surum = tekerlek_adi(PurePosixPath(str(oge.get("hedef", ""))).name)
        return b.yeniden or ad_surum is None or kurulu.get(ad_surum[0]) != ad_surum[1]

    gerekli = [o for o in tekerlekler if _gerekli(o)]
    kaynaktan = {normal_ad(str(a)) for a in (adim.get("kaynaktan") or [])}
    eksik_adlar = {normal_ad(e.split("==", 1)[0]) for e in eksik}

    def _ad(oge: Mapping[str, Any]) -> str:
        ad_surum = tekerlek_adi(PurePosixPath(str(oge.get("hedef", ""))).name)
        return ad_surum[0] if ad_surum else ""

    b.indir([o for o in gerekli if _ad(o) not in kaynaktan])
    derlenecek: Set[str] = set()
    for oge in [o for o in gerekli if _ad(o) in kaynaktan]:
        try:
            b.indir([oge])
        except IptalEdildi:
            raise
        except Exception as exc:  # noqa: BLE001 — hazır tekerlek yoksa derleme yoluna geçilir
            logger.warning("hazır tekerlek indirilemedi (%s), kaynaktan derlenecek: %s",
                           oge.get("kimlik"), exc)
            derlenecek.add(_ad(oge))
    tekerlegi_olan = {_ad(o) for o in tekerlekler}
    derlenecek |= {a for a in kaynaktan if a in eksik_adlar and a not in tekerlegi_olan}
    if not b.yeniden:
        derlenecek &= eksik_adlar   # zaten kurulu olanı derleme
    b.iptal_denetle()

    uv = b.duzen.uv
    depo = b.duzen.tekerlek_deposu
    depo.mkdir(parents=True, exist_ok=True)
    kullanilan_tekerlekler = [o for o in tekerlekler if _ad(o) not in derlenecek]
    etkin = b.duzen.indirilen / ETKIN_KILIT_SABLONU.format(grup=grup)
    etkin_kilit_yaz(etkin, paketler, kullanilan_tekerlekler, dislanan=derlenecek)
    argv: List[Any] = [uv, "pip", "install", "--no-config", "--python", python,
                       "--offline", "--no-index", "--find-links", depo,
                       "--no-deps", "--require-hashes", "-r", etkin]
    if b.yeniden:
        argv.append("--reinstall")
    sonuc = b.komut(argv, cwd=b.duzen.kok)
    if sonuc.rc != 0:
        raise AdimHatasi(KOD_KOMUT, f"'{grup}' paketleri kurulamadı.", sonuc.ayrinti)

    if derlenecek:
        if not ksa_var():
            raise AdimHatasi(KOD_KSA_YOK, MSG_KSA_YOK)
        derleme_kilidi = b.duzen.indirilen / ETKIN_KILIT_SABLONU.format(grup=f"{grup}-derleme")
        etkin_kilit_yaz(derleme_kilidi, [p for p in paketler if p.normal in derlenecek], [])
        ortam = {str(k): str(v) for k, v in (adim.get("derleme_ortami") or {}).items()}
        if b.min_macos:
            ortam.setdefault("MACOSX_DEPLOYMENT_TARGET", b.min_macos)
        argv = [uv, "pip", "install", "--no-config", "--python", python,
                "--find-links", depo, "--no-deps", "--require-hashes"]
        for ad in sorted(derlenecek):
            argv += ["--no-binary", ad]
        argv += ["-r", derleme_kilidi]
        if b.yeniden:
            argv.append("--reinstall")
        sonuc = b.komut(argv, ortam=ortam, cwd=b.duzen.kok)
        if sonuc.rc != 0:
            raise AdimHatasi(KOD_KOMUT, f"'{', '.join(sorted(derlenecek))}' kaynaktan "
                                        "derlenemedi.", sonuc.ayrinti)

    kalan = eksik_paketler(site, ciftler)
    if kalan:
        raise AdimHatasi(KOD_DOGRULAMA, f"'{grup}' kurulumundan sonra eksik paket var: "
                                        f"{kalan[0]}", "\n".join(kalan))
    return AdimSonucu(gruplar={grup: _grup_bilgisi(kilit_sha, paketler)},
                      tuketilen=tuketilen,
                      kayit={"kilit_sha256": kilit_sha, "paket_sayisi": len(paketler),
                             "derlenen": sorted(derlenecek)})


# ---------------------------------------------------------------------------
# Doğrulama ve kaldırma
# ---------------------------------------------------------------------------
def python_ice_aktar(b: Baglam, moduller: Sequence[str],
                     ortam: Optional[Mapping[str, str]] = None) -> None:
    """venv Python'unda modülleri içe aktarır (model YÜKLEMEZ); olmazsa ``AdimHatasi``."""
    adlar = [str(m) for m in moduller]
    if not adlar or not all(_MODUL_ADI.match(m) for m in adlar):
        raise AdimHatasi(KOD_MANIFEST, f"Geçersiz modül listesi: {adlar!r}")
    kod = "; ".join(f"import {m}" for m in adlar)
    sonuc = b.komut([b.duzen.venv_python, "-I", "-c", kod], ortam=ortam, cwd=b.duzen.kok)
    if sonuc.rc != 0:
        raise AdimHatasi(KOD_DOGRULAMA, f"İçe aktarma sınaması başarısız: {', '.join(adlar)}",
                         sonuc.ayrinti)


def paketleri_kaldir(b: Baglam, adlar: Sequence[str]) -> None:
    """``uv pip uninstall`` (yalnız verilen adlar; boşsa bir şey yapılmaz)."""
    if not adlar:
        return
    sonuc = b.komut([b.duzen.uv, "pip", "uninstall", "--no-config", "--python",
                     b.duzen.venv_python, *adlar], cwd=b.duzen.kok)
    if sonuc.rc != 0:
        raise AdimHatasi(KOD_KOMUT, "Paketler kaldırılamadı.", sonuc.ayrinti)
