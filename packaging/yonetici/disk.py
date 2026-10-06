"""Disk yardımcıları: güvenli yol, boş alan ön denetimi, du ölçümü, APFS klonu.

TASARIM D.2-8 (disk ön denetimi), C.1-3 (disk_bayt), C.3 (yeniden_kullan →
APFS klonu), E.7 ("Diskte" alanı gerçek ``du`` ölçümüdür). Yalnız stdlib.
"""
from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import re
import stat
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Optional, Union

try:
    from . import ayarlar
except ImportError:  # düz betik olarak yüklendi (sys.path[0] = packaging/yonetici)
    import ayarlar  # type: ignore[no-redef]

YolTuru = Union[str, "os.PathLike[str]"]

# Disk dolu sayılan hata kodları: yer yok ya da kota aşıldı.
_DISK_DOLU_KODLARI = frozenset({errno.ENOSPC, errno.EDQUOT})


class GecersizYol(ValueError):
    """Manifestteki köke göre yol güvenli değil (mutlak, '..' ya da boş)."""


class IslemDurduruldu(Exception):
    """Uzun disk işlemi (özet hesabı, kopya) durdurma bayrağıyla kesildi."""


# .part adında öğe kimliğinden izin verilen karakterler dışındakiler '_' olur.
_PART_AD_DISI = re.compile(r"[^A-Za-z0-9._+-]")


# ---------------------------------------------------------------------------
# Köke göre yol güvenliği (şema: "hedef: köke göre yol; '..' yasak")
# ---------------------------------------------------------------------------
def goreli_yol_sorunu(goreli: object) -> Optional[str]:
    """Köke göre yol kuralını denetler; sorun yoksa None, varsa Türkçe neden.

    Kural: boş olmayan metin; mutlak değil; ``~`` ile başlamaz; hiçbir parçası
    ``..`` ya da ``.`` değil; NUL ve ters eğik çizgi içermez; boş parça yok
    (``a//b`` ya da sonda ``/``).
    """
    if not isinstance(goreli, str) or not goreli:
        return "yol boş ya da metin değil"
    if "\x00" in goreli:
        return "yol NUL karakteri içeriyor"
    if "\\" in goreli:
        return "yol ters eğik çizgi içeriyor"
    if goreli.startswith("/"):
        return "yol mutlak (köke göre olmalı)"
    if goreli.startswith("~"):
        return "yol '~' ile başlıyor (köke göre olmalı)"
    parcalar = goreli.split("/")
    for parca in parcalar:
        if parca == "":
            return "yolda boş parça var"
        if parca in (".", ".."):
            return "yolda '.' ya da '..' parçası var"
    return None


def guvenli_yol(kok: YolTuru, goreli: str) -> Path:
    """``<kök>/<göreli>`` yolunu döndürür; kökün dışına çıkan yolu reddeder."""
    sorun = goreli_yol_sorunu(goreli)
    if sorun:
        raise GecersizYol(f"Geçersiz hedef yolu {goreli!r}: {sorun}.")
    kok_yolu = Path(os.path.abspath(os.fspath(kok)))
    tam = Path(os.path.normpath(kok_yolu.joinpath(*PurePosixPath(goreli).parts)))
    if tam == kok_yolu or kok_yolu not in tam.parents:
        raise GecersizYol(f"Geçersiz hedef yolu {goreli!r}: kökün dışına çıkıyor.")
    return tam


# ---------------------------------------------------------------------------
# Boş alan ön denetimi (TASARIM D.2-8)
# ---------------------------------------------------------------------------
def _var_olan_ata(yol: Path) -> Path:
    """Henüz yoksa (ilk kurulum) yolun var olan en yakın atasını bulur."""
    aday = Path(os.path.abspath(os.fspath(yol)))
    while not aday.exists():
        if aday.parent == aday:
            break
        aday = aday.parent
    return aday


def bos_alan_bayt(yol: YolTuru) -> int:
    """Yolun bulunduğu birimde bu kullanıcının kullanabileceği boş bayt.

    ``os.statvfs`` ile ``f_bavail × f_frsize`` (TASARIM D.2-8). APFS'in
    "temizlenebilir" alanı buna dahil değildir; yani ölçüm tutucudur.
    """
    sonuc = os.statvfs(_var_olan_ata(Path(os.fspath(yol))))
    return int(sonuc.f_bavail) * int(sonuc.f_frsize)


@dataclass(frozen=True)
class DiskDenetimi:
    """Plan için disk ön denetiminin sonucu (sayıların hepsi gerçek ölçüm/manifest)."""
    bos_bayt: int
    ihtiyac_bayt: int      # plan: disk_bayt toplamı + geçici (indirilen/) tepe
    pay_bayt: int          # ayarlar.DISK_PAYI_BAYT
    gerekli_bayt: int      # ihtiyac + pay
    yeter: bool
    eksik_bayt: int        # yetmiyorsa ne kadar eksik; yetiyorsa 0

    def sozluk(self) -> dict:
        return {
            "bos_bayt": self.bos_bayt,
            "ihtiyac_bayt": self.ihtiyac_bayt,
            "pay_bayt": self.pay_bayt,
            "gerekli_bayt": self.gerekli_bayt,
            "yeter": self.yeter,
            "eksik_bayt": self.eksik_bayt,
        }


def denetle(kok: YolTuru, ihtiyac_bayt: int, *, pay_bayt: Optional[int] = None,
            bos_bayt: Optional[int] = None) -> DiskDenetimi:
    """Boş alan ihtiyaç + payı karşılıyor mu?

    ``ihtiyac_bayt`` = kurulum boyunca gereken en büyük ek alan; tek tanımı
    ``manifest.ek_alan_tepesi``'dir (K3'ün planı ve ``Manifest.plan_toplami``
    onu kullanır). ``bos_bayt`` verilmezse ``kok``'un birimi ölçülür.
    """
    if ihtiyac_bayt < 0:
        raise ValueError("ihtiyac_bayt negatif olamaz")
    pay = ayarlar.DISK_PAYI_BAYT if pay_bayt is None else int(pay_bayt)
    bos = bos_alan_bayt(kok) if bos_bayt is None else int(bos_bayt)
    gerekli = int(ihtiyac_bayt) + pay
    eksik = max(0, gerekli - bos)
    return DiskDenetimi(bos_bayt=bos, ihtiyac_bayt=int(ihtiyac_bayt), pay_bayt=pay,
                        gerekli_bayt=gerekli, yeter=eksik == 0, eksik_bayt=eksik)


def disk_dolu_hatasi_mi(hata: BaseException) -> bool:
    """ENOSPC / EDQUOT: yazma diskte yer kalmadığı için başarısız oldu."""
    return isinstance(hata, OSError) and hata.errno in _DISK_DOLU_KODLARI


# ---------------------------------------------------------------------------
# Gerçek disk kullanımı (``du -sk`` karşılığı; TASARIM E.7 "Diskte")
# ---------------------------------------------------------------------------
def disk_kullanimi_bayt(yol: YolTuru) -> int:
    """Yolun diskte kapladığı gerçek alan (ayrılmış bloklar), ``du`` gibi.

    Sembolik bağlar izlenmez; aynı inode (sabit bağ) bir kez sayılır. Yol
    yoksa 0. APFS klonları ``du`` gibi tam boyutuyla sayılır (paylaşılan
    bloklar ayrıştırılmaz).
    """
    kok = Path(os.fspath(yol))
    try:
        ilk = os.lstat(kok)
    except FileNotFoundError:
        return 0
    gorulen: set = set()

    def _bloklar(st: os.stat_result) -> int:
        anahtar = (st.st_dev, st.st_ino)
        if anahtar in gorulen:
            return 0
        gorulen.add(anahtar)
        return int(st.st_blocks) * 512

    toplam = _bloklar(ilk)
    if not stat.S_ISDIR(ilk.st_mode):
        return toplam
    for dizin, altlar, dosyalar in os.walk(kok, followlinks=False):
        for ad in altlar + dosyalar:
            try:
                toplam += _bloklar(os.lstat(os.path.join(dizin, ad)))
            except FileNotFoundError:
                continue
    return toplam


# ---------------------------------------------------------------------------
# APFS klonu (TASARIM C.3: yeniden_kullan; ölçüm: 1,4 GB'ta 0,028 sn)
# ---------------------------------------------------------------------------
def apfs_klonla(kaynak: YolTuru, hedef: YolTuru) -> bool:
    """``clonefile(2)`` ile kopyasız klon; başarılıysa True.

    Hedef var olmamalı. Farklı birim (EXDEV), APFS olmayan birim (ENOTSUP) ya
    da ctypes'ın işlevi bulamaması durumunda False döner; çağıran düz
    kopyaya düşer. Disk harcamaz (bloklar paylaşılır).
    """
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        islev = libc.clonefile
    except (OSError, AttributeError):
        return False
    islev.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
    islev.restype = ctypes.c_int
    sonuc = islev(os.fsencode(os.fspath(kaynak)), os.fsencode(os.fspath(hedef)), 0)
    return sonuc == 0


# ---------------------------------------------------------------------------
# .part dosyasının yeri (TASARIM D.2-1): indirici ve plan toplamı aynı adı
# kullanır; ad yalnız burada kurulur.
# ---------------------------------------------------------------------------
def part_yolu(indirilen_dizini: YolTuru, kimlik: str, sha256: str) -> Path:
    """``<indirilen>/<güvenli kimlik>.<sha256 öneki>.part``.

    Ad SHA-256 önekini taşır: manifest bir öğenin içeriğini değiştirirse
    (aynı kimlik, yeni SHA) eski yarım dosyaya yeni baytlar eklenmez.
    """
    guvenli = _PART_AD_DISI.sub("_", str(kimlik))[: ayarlar.PART_AD_EN_COK] or "oge"
    onek = str(sha256)[: ayarlar.PART_OZET_ONEKI] or "ozetsiz"
    return Path(os.fspath(indirilen_dizini)) / f"{guvenli}.{onek}{ayarlar.PART_UZANTISI}"


# ---------------------------------------------------------------------------
# Akışlı özet (SHA-256 / SHA-512): durdurulabilir
# ---------------------------------------------------------------------------
def ozete_ekle(ozet, yol: YolTuru, *, parca: Optional[int] = None,
               durdur: Optional[threading.Event] = None,
               ilerleme: Optional[Callable[[int], None]] = None) -> int:
    """Dosyanın baytlarını ``ozet`` nesnesine (hashlib) ekler; okunan bayt sayısı.

    Her parçadan önce ``durdur`` bayrağına bakılır; kalkmışsa
    ``IslemDurduruldu``. ``ilerleme`` her parçada okunan bayt sayısıyla çağrılır.
    """
    boy = ayarlar.PARCA_BAYT if parca is None else int(parca)
    toplam = 0
    with open(os.fspath(yol), "rb") as dosya:
        while True:
            if durdur is not None and durdur.is_set():
                raise IslemDurduruldu(os.fspath(yol))
            veri = dosya.read(boy)
            if not veri:
                return toplam
            ozet.update(veri)
            toplam += len(veri)
            if ilerleme is not None:
                ilerleme(len(veri))


def ozet_hesapla(yol: YolTuru, algoritma: str = "sha256", *, parca: Optional[int] = None,
                 durdur: Optional[threading.Event] = None) -> str:
    """Dosyanın onaltılık özeti (küçük harf)."""
    ozet = hashlib.new(algoritma)
    ozete_ekle(ozet, yol, parca=parca, durdur=durdur)
    return ozet.hexdigest()
