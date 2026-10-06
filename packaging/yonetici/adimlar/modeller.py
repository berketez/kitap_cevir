"""Dosya ve model adımları: ``dosya_yerlestir``, ``surya_manifest_yaz``, ``lisans_yaz``.

- Marker/Surya (TASARIM D.3, lisans K5): 48 dosya ``modeller/datalab/<ad>/<tarih>/``
  altına indiricinin öğeleri olarak iner. Surya'nın ``check_manifest``'i bir
  klasörü, ``manifest.json``'undaki her dosya yerindeyse hazır sayar ve kendi
  (sürmesiz, doğrulamasız) indiricisini çalıştırmaz. ``surya_manifest_yaz``
  her model klasöründe bu koşulu sağlar: ``manifest.json`` öğe olarak indiyse
  listesindeki dosyaları denetler, inmediyse öğelerden yazar
  (``{"files": [...]}``, Surya'nın biçimi).
- HY-MT (lisans K6): ``LICENSE-Tencent-HY.txt`` (indirilen ``License.txt``'nin
  birebir kopyası), ``NOTICE`` (lisansın 3(d) cümlesi, birebir) ve
  ``KAYNAK.txt`` (depo, revizyon, indirme tarihi) modelin yanına; ayrıca
  ``<kök>/LICENSES/<lisans_klasoru>/``'na. Onay kaydı olmadan yazılmaz.
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Mapping, Optional

try:
    from .. import lisans
    from ..durum import MODELLER_DATALAB, atomik_yaz, sha256_dosya, simdi
except ImportError:  # düz betik olarak yüklendi
    import lisans  # type: ignore[no-redef]
    from durum import (MODELLER_DATALAB, atomik_yaz,  # type: ignore[no-redef]
                       sha256_dosya, simdi)

from .ortak import (KOD_DOSYA_YOK, KOD_LISANS, KOD_MANIFEST, AdimHatasi, AdimSonucu, Baglam)

# ---------------------------------------------------------------------------
# Sabitler (yalnız bu modülde)
# ---------------------------------------------------------------------------
SURYA_KOKU = MODELLER_DATALAB             # köke göre (KokDuzeni.modeller_datalab); tek kaynak durum.py
SURYA_MANIFESTI = "manifest.json"
KAYNAK_ONEKI_UYGULAMA = "uygulama:"
KAYNAK_ONEKI_KOK = "kok:"
LISANS_DOSYA_ADI = "LICENSE-Tencent-HY.txt"
NOTICE_ADI = "NOTICE"
KAYNAK_ADI = "KAYNAK.txt"
_VARSAYILAN_KIP = 0o644


# ---------------------------------------------------------------------------
# dosya_yerlestir
# ---------------------------------------------------------------------------
def _kaynak_yolu(b: Baglam, kaynak: str) -> Path:
    if kaynak.startswith(KAYNAK_ONEKI_UYGULAMA):
        return b.uygulama_yolu(kaynak[len(KAYNAK_ONEKI_UYGULAMA):])
    if kaynak.startswith(KAYNAK_ONEKI_KOK):
        try:
            return b.duzen.kok_yolu(kaynak[len(KAYNAK_ONEKI_KOK):])
        except ValueError as exc:
            raise AdimHatasi(KOD_MANIFEST, f"dosya_yerlestir kaynağı geçersiz: {exc}") from exc
    raise AdimHatasi(KOD_MANIFEST, f"dosya_yerlestir kaynağı '{KAYNAK_ONEKI_UYGULAMA}' ya da "
                                   f"'{KAYNAK_ONEKI_KOK}' ile başlamalı: {kaynak!r}")


def dosya_yerlestir(b: Baglam, adim: Mapping[str, Any], kayit: Optional[dict]) -> AdimSonucu:
    """``{"tur": "dosya_yerlestir", "kaynak": "uygulama:<yol>" | "kok:<yol>",
    "hedef": <köke göre yol>, "kip": "755"?}`` (ör. xelatex sarmalayıcısı)."""
    kaynak = _kaynak_yolu(b, str(adim.get("kaynak") or ""))
    if not kaynak.is_file():
        raise AdimHatasi(KOD_DOSYA_YOK, f"Yerleştirilecek dosya yok: {kaynak}")
    try:
        hedef = b.duzen.kok_yolu(str(adim.get("hedef") or ""))
    except ValueError as exc:
        raise AdimHatasi(KOD_MANIFEST, f"dosya_yerlestir hedefi geçersiz: {exc}") from exc
    try:
        kip = int(str(adim.get("kip") or ""), 8) if adim.get("kip") else _VARSAYILAN_KIP
    except ValueError as exc:
        raise AdimHatasi(KOD_MANIFEST, f"Geçersiz dosya kipi: {adim.get('kip')!r}") from exc
    kaynak_sha = sha256_dosya(kaynak)
    goreli = b.duzen.goreli(hedef)
    if (not b.yeniden and hedef.is_file() and (hedef.stat().st_mode & 0o777) == kip
            and sha256_dosya(hedef) == kaynak_sha):
        return AdimSonucu(atlandi=True, dosyalar=[goreli], kayit={"sha256": kaynak_sha})
    atomik_yaz(hedef, kaynak.read_bytes(), kip)
    return AdimSonucu(dosyalar=[goreli], kayit={"sha256": kaynak_sha})


# ---------------------------------------------------------------------------
# surya_manifest_yaz
# ---------------------------------------------------------------------------
def surya_klasorleri(ogeler: List[Mapping[str, Any]], kok: str = SURYA_KOKU
                     ) -> Dict[str, List[str]]:
    """Öğelerden ``{<kök>/<ad>/<tarih>: [klasöre göre dosya yolları]}``.

    Surya klasörü kökün iki düzey altıdır (``layout/2025_09_23``); daha sığ
    öğeler (``fonts/GoNotoCurrent-Regular.ttf``) model klasörü sayılmaz.
    """
    kok_parca = PurePosixPath(kok).parts
    klasorler: Dict[str, List[str]] = {}
    for oge in ogeler:
        parca = PurePosixPath(str(oge.get("hedef", ""))).parts
        if parca[:len(kok_parca)] != kok_parca or len(parca) < len(kok_parca) + 3:
            continue
        klasor = "/".join(parca[:len(kok_parca) + 2])
        klasorler.setdefault(klasor, []).append("/".join(parca[len(kok_parca) + 2:]))
    return {k: sorted(v) for k, v in sorted(klasorler.items())}


def surya_manifest_yaz(b: Baglam, adim: Mapping[str, Any], kayit: Optional[dict]
                       ) -> AdimSonucu:
    """``{"tur": "surya_manifest_yaz", "kok": "modeller/datalab"?}``."""
    kok = str(adim.get("kok") or SURYA_KOKU)
    klasorler = surya_klasorleri(b.ogeler, kok)
    if not klasorler:
        raise AdimHatasi(KOD_MANIFEST, f"'{kok}' altında model öğesi yok.")
    yazilan: List[str] = []
    for klasor, dosyalar in klasorler.items():
        dizin = b.duzen.kok_yolu(klasor)
        manifest = dizin / SURYA_MANIFESTI
        if SURYA_MANIFESTI in dosyalar:
            try:
                liste = json.loads(manifest.read_text(encoding="utf-8")).get("files")
            except (OSError, ValueError, AttributeError) as exc:
                raise AdimHatasi(KOD_DOSYA_YOK, f"Surya manifesti okunamadı: {klasor}") from exc
            if not isinstance(liste, list):
                raise AdimHatasi(KOD_MANIFEST, f"Surya manifestinde 'files' yok: {klasor}")
            eksik = [d for d in liste if not (dizin / str(d)).is_file()]
            if eksik:
                raise AdimHatasi(KOD_DOSYA_YOK,
                                 f"Model dosyası eksik: {klasor}/{eksik[0]}", "\n".join(eksik))
            continue
        eksik = [d for d in dosyalar if not (dizin / d).is_file()]
        if eksik:
            raise AdimHatasi(KOD_DOSYA_YOK, f"Model dosyası eksik: {klasor}/{eksik[0]}")
        atomik_yaz(manifest, (json.dumps({"files": dosyalar}, ensure_ascii=False) + "\n")
                   .encode("utf-8"))
        yazilan.append(b.duzen.goreli(manifest))
    return AdimSonucu(dosyalar=yazilan, yollar=[kok], kayit={"klasorler": list(klasorler)})


# ---------------------------------------------------------------------------
# lisans_yaz
# ---------------------------------------------------------------------------
def lisans_yaz(b: Baglam, adim: Mapping[str, Any], kayit: Optional[dict]) -> AdimSonucu:
    """``{"tur": "lisans_yaz", "oge": <lisans metni öğesi>, "dizin": <köke göre model
    klasörü>, "lisans_klasoru": <LICENSES altı ad>?, "dosya_adi": ?}``."""
    oge = b.oge(str(adim.get("oge")))
    alan = b.bilesen.get("lisans") if isinstance(b.bilesen.get("lisans"), Mapping) else {}
    if not alan.get("metin_sha256") or alan.get("metin_sha256") != oge.get("sha256"):
        raise AdimHatasi(KOD_LISANS, "Lisans metni öğesi manifestteki lisans özetiyle uyuşmuyor.")
    onay = lisans.onay_durumu(b.duzen, b.bilesen)
    if not onay["onaylandi"]:
        raise AdimHatasi(KOD_LISANS, onay["neden"] or "Lisans onayı yok.")
    try:
        dizin = b.duzen.kok_yolu(str(adim.get("dizin") or ""))
    except ValueError as exc:
        raise AdimHatasi(KOD_MANIFEST, f"lisans_yaz dizini geçersiz: {exc}") from exc

    dosya_adi = str(adim.get("dosya_adi") or LISANS_DOSYA_ADI)
    if kayit and not b.yeniden and kayit.get("metin_sha256") == oge.get("sha256"):
        sonuc = AdimSonucu.kayittan(kayit)
        if all(b.duzen.kok_yolu(d).is_file() for d in sonuc.dosyalar):
            return sonuc

    b.indir([oge])
    kaynak = b.oge_yolu(oge)
    metin_bayt = kaynak.read_bytes()
    notice = lisans.tencent_notice(metin_bayt.decode("utf-8")) + "\n"
    kaynaklar = lisans.hymt_kaynaklari(b.bilesen)   # depo + revizyon manifestin URL'lerinden
    kaynak_metni = (
        f"Depo: https://huggingface.co/{kaynaklar['depo']}\n"
        f"Revizyon: {kaynaklar['revizyon_tam']}\n"
        f"Lisans: https://huggingface.co/{kaynaklar['lisans_depo']}/blob/"
        f"{kaynaklar['lisans_revizyon']}/License.txt\n"
        f"İndirme tarihi: {simdi()}\n"
    ).encode("utf-8")

    yazilacak = {dizin / dosya_adi: metin_bayt, dizin / NOTICE_ADI: notice.encode("utf-8"),
                 dizin / KAYNAK_ADI: kaynak_metni}
    klasor_adi = adim.get("lisans_klasoru")
    if klasor_adi:
        try:
            lisans_dizini = b.duzen.kok_yolu(f"{b.duzen.goreli(b.duzen.lisanslar)}/{klasor_adi}")
        except ValueError as exc:
            raise AdimHatasi(KOD_MANIFEST, f"lisans_klasoru geçersiz: {exc}") from exc
        yazilacak[lisans_dizini / dosya_adi] = metin_bayt
        yazilacak[lisans_dizini / NOTICE_ADI] = notice.encode("utf-8")
    for yol, icerik in yazilacak.items():
        atomik_yaz(yol, icerik, _VARSAYILAN_KIP)
    return AdimSonucu(dosyalar=[b.duzen.goreli(y) for y in yazilacak],
                      yollar=[b.duzen.goreli(dizin)],
                      tuketilen=[str(oge["kimlik"])] if b.indirilen_altinda_mi(oge) else [],
                      kayit={"metin_sha256": oge.get("sha256")})


# ---------------------------------------------------------------------------
# Kaldırma yardımcısı
# ---------------------------------------------------------------------------
def ortak_klasor(ogeler: List[Mapping[str, Any]], alt_kok: str) -> Optional[str]:
    """Öğelerin ``alt_kok`` altındaki ortak klasörü (ör. ``veri/models/hymt-7b-mlx``).

    Klasör ``alt_kok``'un kendisi olamaz (``veri/models``'ı silmek kullanıcının
    öteki modellerini de siler); en az bir düzey altı olmalı.
    """
    kok_parca = PurePosixPath(alt_kok).parts
    ortak: Optional[tuple] = None
    for oge in ogeler:
        parca = PurePosixPath(str(oge.get("hedef", ""))).parts[:-1]
        if parca[:len(kok_parca)] != kok_parca:
            continue
        ortak = parca if ortak is None else tuple(os.path.commonprefix([list(ortak), list(parca)]))
    if not ortak or len(ortak) <= len(kok_parca):
        return None
    return "/".join(ortak)

