"""Arşiv adımları: ``arsiv_ac``, ``uv_kur`` ve taşınabilir Python'u açma.

Arşivler Python ``tarfile`` ile ``filter="data"`` süzgeciyle açılır: kök
dışına yazan, mutlak yollu ya da aygıt dosyalı üyeler reddedilir. ``tarfile``
``/usr/bin/tar``'ın aksine karantina özniteliğini açılan dosyalara taşımaz
(TASARIM B.7-2), bu yüzden ek ``xattr`` işlemi gerekmez.
"""

from __future__ import annotations

import os
import shutil
import tarfile
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

try:
    from .. import disk
    from ..durum import TAMAM_ISARETI, KokDuzeni, calisma_zamani_etiketi, sha256_dosya
except ImportError:  # düz betik olarak yüklendi
    import disk  # type: ignore[no-redef]
    from durum import (TAMAM_ISARETI, KokDuzeni,  # type: ignore[no-redef]
                       calisma_zamani_etiketi, sha256_dosya)

from .ortak import (KOD_ARSIV, KOD_CALISMA_ZAMANI, KOD_KOMUT, KOD_MANIFEST, AdimHatasi,
                    AdimSonucu, Baglam, komut_calistir, temel_ortam)

# uv'nin resmî arm64 arşivindeki ikili (astral-sh/uv sürüm varlığı; prototip).
UV_UYESI = "uv-aarch64-apple-darwin/uv"
# python-build-standalone install_only arşivinin kök klasörü.
PBS_KOK_KLASORU = "python"
# Çalışma zamanı sınaması (TASARIM B.4-4).
PYTHON_SINAMASI = "import ssl, sqlite3"
# Çıkarılan dosyada grup/başkası yazma izni kaldırılır.
_YAZMA_MASKESI = 0o022


def _uye_kipi(bilgi: tarfile.TarInfo) -> int:
    return (bilgi.mode & 0o777) & ~_YAZMA_MASKESI


def arsivden_cikar(arsiv: Path, hedef: Path,
                   uyeler: Optional[Mapping[str, str]] = None) -> List[Path]:
    """Arşivi ``hedef`` klasörüne açar; yazılan dosyaları döndürür.

    ``uyeler`` verilirse yalnız ``{arşivdeki yol: hedefe göre ad}`` dosyaları
    çıkarılır (ör. ``{"tectonic": "tectonic"}``); her biri geçici adla yazılıp
    ``os.replace`` ile yerine konur. Verilmezse bütün arşiv ``filter="data"``
    ile geçici klasöre açılır ve üst düzey girdiler hedefe taşınır.
    """
    hedef.mkdir(parents=True, exist_ok=True)
    yazilan: List[Path] = []
    try:
        with tarfile.open(arsiv, "r:*") as tf:
            if uyeler:
                for uye_adi, ad in uyeler.items():
                    sorun = disk.goreli_yol_sorunu(ad)
                    if sorun:
                        raise AdimHatasi(KOD_MANIFEST, f"Geçersiz hedef adı {ad!r}: {sorun}.")
                    try:
                        bilgi = tf.getmember(uye_adi)
                    except KeyError:
                        raise AdimHatasi(KOD_ARSIV,
                                         f"Arşivde '{uye_adi}' yok: {arsiv.name}") from None
                    if not bilgi.isfile():
                        raise AdimHatasi(KOD_ARSIV, f"'{uye_adi}' düz dosya değil: {arsiv.name}")
                    kaynak = tf.extractfile(bilgi)
                    if kaynak is None:
                        raise AdimHatasi(KOD_ARSIV, f"'{uye_adi}' okunamadı: {arsiv.name}")
                    yol = disk.guvenli_yol(hedef, ad)
                    yol.parent.mkdir(parents=True, exist_ok=True)
                    fd, gecici = tempfile.mkstemp(prefix=f".{yol.name}.", suffix=".yeni",
                                                  dir=yol.parent)
                    try:
                        with os.fdopen(fd, "wb") as f:
                            shutil.copyfileobj(kaynak, f)
                        os.chmod(gecici, _uye_kipi(bilgi))
                        os.replace(gecici, yol)
                    except BaseException:
                        Path(gecici).unlink(missing_ok=True)
                        raise
                    yazilan.append(yol)
                return yazilan
            gecici_dizin = Path(tempfile.mkdtemp(prefix=f".{hedef.name}.", dir=hedef.parent))
            try:
                tf.extractall(gecici_dizin, filter="data")
                for girdi in sorted(gecici_dizin.iterdir()):
                    yer = hedef / girdi.name
                    if yer.is_dir() and not yer.is_symlink():
                        shutil.rmtree(yer)
                    elif yer.exists() or yer.is_symlink():
                        yer.unlink()
                    os.replace(girdi, yer)
                    yazilan.append(yer)
            finally:
                shutil.rmtree(gecici_dizin, ignore_errors=True)
    except (tarfile.TarError, EOFError) as exc:
        raise AdimHatasi(KOD_ARSIV, f"Arşiv açılamadı: {arsiv.name} ({exc})") from exc
    return yazilan


def _dosya_listesi(duzen: KokDuzeni, yollar: List[Path]) -> List[str]:
    sonuc: List[str] = []
    for yol in yollar:
        if yol.is_dir() and not yol.is_symlink():
            for alt in sorted(yol.rglob("*")):
                if alt.is_file() and not alt.is_symlink():
                    sonuc.append(duzen.goreli(alt))
        elif yol.is_file():
            sonuc.append(duzen.goreli(yol))
    return sonuc


def _kayit_gecerli(b: Baglam, kayit: Optional[Mapping[str, Any]], oge: Mapping[str, Any],
                   dosyalar_alan: str = "boyutlar") -> bool:
    if b.yeniden or not kayit or kayit.get("oge_sha256") != oge.get("sha256"):
        return False
    boyutlar = kayit.get(dosyalar_alan)
    if not isinstance(boyutlar, Mapping) or not boyutlar:
        return False
    for goreli, bayt in boyutlar.items():
        try:
            if b.duzen.kok_yolu(goreli).stat().st_size != bayt:
                return False
        except (OSError, ValueError):
            return False
    return True


def _boyutlar(b: Baglam, dosyalar: List[str]) -> Dict[str, int]:
    return {g: b.duzen.kok_yolu(g).stat().st_size for g in dosyalar}


def arsiv_ac(b: Baglam, adim: Mapping[str, Any], kayit: Optional[dict]) -> AdimSonucu:
    """``{"tur": "arsiv_ac", "oge": <kimlik>, "hedef": <köke göre klasör>,
    "uyeler": {<arşivdeki yol>: <hedefe göre ad>}?}``."""
    oge = b.oge(str(adim.get("oge")))
    hedef_goreli = str(adim.get("hedef") or "")
    try:
        hedef = b.duzen.kok_yolu(hedef_goreli)
    except ValueError as exc:
        raise AdimHatasi(KOD_MANIFEST, f"arsiv_ac hedefi geçersiz: {exc}") from exc
    if _kayit_gecerli(b, kayit, oge):
        return AdimSonucu.kayittan(kayit or {})
    b.indir([oge])
    b.iptal_denetle()
    uyeler = adim.get("uyeler")
    yazilan = arsivden_cikar(b.oge_yolu(oge), hedef,
                             uyeler if isinstance(uyeler, Mapping) else None)
    dosyalar = _dosya_listesi(b.duzen, yazilan)
    return AdimSonucu(dosyalar=dosyalar,
                      tuketilen=[str(oge["kimlik"])] if b.indirilen_altinda_mi(oge) else [],
                      kayit={"oge_sha256": oge.get("sha256"),
                             "boyutlar": _boyutlar(b, dosyalar)})


def uv_surumu(uv: Path, b: Optional[Baglam] = None) -> str:
    """``uv --version`` çıktısındaki sürüm (``uv 0.12.20 (...)`` → ``0.12.20``)."""
    if b is not None:
        sonuc = b.komut([uv, "--version"])
    else:
        sonuc = komut_calistir([uv, "--version"], iptal=threading.Event(), env=temel_ortam())
    if sonuc.rc != 0 or not sonuc.son_satirlar:
        raise AdimHatasi(KOD_KOMUT, f"uv çalışmadı: {uv}", sonuc.ayrinti)
    parcalar = sonuc.son_satirlar[-1].split()
    return parcalar[1] if len(parcalar) > 1 else ""


def uv_kur(b: Baglam, adim: Mapping[str, Any], kayit: Optional[dict]) -> AdimSonucu:
    """``{"tur": "uv_kur", "oge": <kimlik>, "uye": "uv-aarch64-apple-darwin/uv"?}``.

    uv ikilisi ``<kök>/bin/uv``'ye konur (``KokDuzeni.uv``) ve ``--version``
    ile çalıştığı sınanır.
    """
    oge = b.oge(str(adim.get("oge")))
    uv = b.duzen.uv
    if _kayit_gecerli(b, kayit, oge) and os.access(uv, os.X_OK):
        return AdimSonucu.kayittan(kayit or {})
    b.indir([oge])
    b.iptal_denetle()
    arsivden_cikar(b.oge_yolu(oge), uv.parent, {str(adim.get("uye") or UV_UYESI): uv.name})
    surum = uv_surumu(uv, b)
    dosyalar = [b.duzen.goreli(uv)]
    return AdimSonucu(dosyalar=dosyalar,
                      tuketilen=[str(oge["kimlik"])] if b.indirilen_altinda_mi(oge) else [],
                      kayit={"oge_sha256": oge.get("sha256"), "surum": surum,
                             "boyutlar": _boyutlar(b, dosyalar)})


# ---------------------------------------------------------------------------
# Taşınabilir Python (manifest adımı değildir: kabuk ya da başlıksız kip çağırır)
# ---------------------------------------------------------------------------
def calisma_zamani_hazirla(duzen: KokDuzeni, arsiv: Path, sha256: str, *,
                           etiket: Optional[str] = None) -> Path:
    """python-build-standalone arşivini ``calisma_zamani/python-<etiket>/``'e açar.

    TASARIM B.4-4'ün Python karşılığı (sürüm betiği ve başlıksız kip için;
    kabuk aynı işi kendisi yapar). ``.tamam`` (``durum.TAMAM_ISARETI``) işaretinde arşivin SHA-256'sı
    durur; aynıysa hiçbir şey yapılmaz. Sıra: SHA denetimi → ``.yeni``'ye
    açma → ``import ssl, sqlite3`` sınaması → atomik yeniden adlandırma →
    ``.tamam``. Dönüş: ``bin/python3`` yolu.
    """
    arsiv = Path(arsiv)
    etiket = etiket or calisma_zamani_etiketi({"url": arsiv.name})
    hedef = duzen.calisma_zamani(etiket)
    python = duzen.calisma_zamani_python(etiket)
    tamam = hedef / TAMAM_ISARETI
    try:
        if tamam.read_text(encoding="utf-8").strip() == sha256 and python.exists():
            return python
    except OSError:
        pass
    gercek = sha256_dosya(arsiv)
    if gercek != sha256:
        raise AdimHatasi(KOD_CALISMA_ZAMANI,
                         f"Python arşivinin SHA-256'sı uyuşmuyor: {arsiv.name}",
                         f"beklenen {sha256}, bulunan {gercek}")
    yeni = hedef.with_name(hedef.name + ".yeni")
    shutil.rmtree(yeni, ignore_errors=True)
    yeni.mkdir(parents=True)
    try:
        arsivden_cikar(arsiv, yeni)
        kok = yeni / PBS_KOK_KLASORU
        aday = kok / "bin" / "python3"
        if not aday.exists():
            raise AdimHatasi(KOD_CALISMA_ZAMANI, "Python arşivinde bin/python3 yok.")
        sinama = komut_calistir([aday, "-I", "-c", PYTHON_SINAMASI], iptal=threading.Event(),
                                env=temel_ortam())
        if sinama.rc != 0:
            raise AdimHatasi(KOD_CALISMA_ZAMANI, "Açılan Python çalışmadı.", sinama.ayrinti)
        if hedef.exists():
            eski = hedef.with_name(hedef.name + ".eski")
            shutil.rmtree(eski, ignore_errors=True)
            os.replace(hedef, eski)
            shutil.rmtree(eski, ignore_errors=True)
        os.replace(kok, hedef)
        (hedef / TAMAM_ISARETI).write_text(sha256 + "\n", encoding="utf-8")
    finally:
        shutil.rmtree(yeni, ignore_errors=True)
    return python
