"""Kilitteki her paket için macOS sürümüne göre doğru tekerleği seçer (TASARIM C.1-4).

Kurulum çevrimdışıdır: yönetici yalnız manifestteki tekerleği indirir ve uv onu
``--find-links`` deposundan kurar. Bu yüzden her desteklenen macOS ana sürümü
için o sürümde GERÇEKTEN kurulabilen tekerlek seçilmelidir. Kural, uv/pip'in
etiket önceliğinin CPython 3.12 + macOS arm64 için sadeleştirilmiş hâlidir:

1. platforma özgü tekerlek, ``any`` tekerlekten önce gelir;
2. yorumlayıcı/ABI: ``cp312-cp312`` > ``cp3X-abi3`` (X büyükten küçüğe) >
   ``cp312-none`` > ``py312``/``py3``/``py3X``;
3. platform sürümü: ``macosx_A_B`` (A.B ≤ hedef macOS) büyükten küçüğe; aynı
   sürümde ``arm64``, ``universal2``'den önce gelir;
4. etiketler aynıysa büyük yapı numarası (PEP 427) önce gelir.

Seçimler macOS'a göre aynıysa öğe koşulsuzdur; değilse her farklı dosya için
``kosul.macos`` yazılır (son sürüm ``"26+"``).
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

_AD_NORMAL = re.compile(r"[-_.]+")
_MACOS_PLATFORM = re.compile(r"^macosx_(\d+)_(\d+)_(arm64|universal2)$")
_PY = re.compile(r"^(cp|py)(\d)(\d*)$")


def normal_ad(ad: str) -> str:
    """PEP 503 adı (``Jinja2`` → ``jinja2``, ``typing_extensions`` → ``typing-extensions``)."""
    return _AD_NORMAL.sub("-", ad).lower()


def ayristir(dosya_adi: str) -> Optional[Tuple[str, str, List[Tuple[str, str, str]]]]:
    """``ad-sürüm(-yapı)?-py-abi-platform.whl`` → (normal ad, sürüm, etiket üçlüleri)."""
    if not dosya_adi.endswith(".whl"):
        return None
    parcalar = dosya_adi[:-4].split("-")
    if len(parcalar) not in (5, 6):
        return None
    ad, surum = parcalar[0], parcalar[1]
    py, abi, plat = parcalar[-3], parcalar[-2], parcalar[-1]
    uclu = [(p, a, pl) for p in py.split(".") for a in abi.split(".") for pl in plat.split(".")]
    return normal_ad(ad), surum, uclu


def yapi_numarasi(dosya_adi: str) -> int:
    """PEP 427 yapı etiketi (``torch-2.10.0-2-cp312-…`` → 2); yoksa 0. Aynı etiketli
    iki tekerlekte uv/pip büyük yapı numarasını seçer."""
    parcalar = dosya_adi[:-4].split("-") if dosya_adi.endswith(".whl") else []
    if len(parcalar) != 6:
        return 0
    m = re.match(r"^(\d+)", parcalar[2])
    return int(m.group(1)) if m else 0


def _yorumlayici_sirasi(py: str, abi: str, python: Tuple[int, int]) -> Optional[Tuple[int, int]]:
    """Küçük = öncelikli. Uyumsuzsa None."""
    m = _PY.match(py)
    if not m:
        return None
    tur, ana, alt = m.group(1), int(m.group(2)), m.group(3)
    if ana != python[0]:
        return None
    alt_sayi = int(alt) if alt else None
    if tur == "cp":
        if alt_sayi is None:
            return None
        if abi == f"cp{python[0]}{python[1]}" and alt_sayi == python[1]:
            return (0, 0)
        if abi == "abi3" and alt_sayi <= python[1]:
            return (1, python[1] - alt_sayi)
        if abi == "none" and alt_sayi == python[1]:
            return (2, 0)
        return None
    # py etiketi yalnız "none" ABI ile
    if abi != "none":
        return None
    if alt_sayi is None:                       # py3
        return (3, 1)
    if alt_sayi == python[1]:                  # py312
        return (3, 0)
    if alt_sayi < python[1]:                   # py311, py38 …
        return (3, 2 + (python[1] - alt_sayi))
    return None


def _platform_sirasi(plat: str, macos: Tuple[int, int]) -> Optional[Tuple[int, int, int]]:
    """(any mi, -sürüm, mimari) — küçük = öncelikli. Uyumsuzsa None."""
    if plat == "any":
        return (1, 0, 0)
    m = _MACOS_PLATFORM.match(plat)
    if not m:
        return None
    surum = (int(m.group(1)), int(m.group(2)))
    if surum > macos:
        return None
    return (0, -(surum[0] * 100 + surum[1]), 0 if m.group(3) == "arm64" else 1)


def oncelik(dosya_adi: str, python: Tuple[int, int], macos: Tuple[int, int]
            ) -> Optional[Tuple[int, ...]]:
    """Tekerleğin bu Python + macOS'ta önceliği (küçük = iyi); kurulamıyorsa None."""
    bilgi = ayristir(dosya_adi)
    if bilgi is None:
        return None
    en_iyi: Optional[Tuple[int, ...]] = None
    for py, abi, plat in bilgi[2]:
        y = _yorumlayici_sirasi(py, abi, python)
        p = _platform_sirasi(plat, macos)
        if y is None or p is None:
            continue
        anahtar = (p[0], y[0], y[1], p[1], p[2], -yapi_numarasi(dosya_adi))
        if en_iyi is None or anahtar < en_iyi:
            en_iyi = anahtar
    return en_iyi


def sec(dosyalar: Iterable[Mapping], python: Tuple[int, int],
        macos_surumleri: Sequence[int]) -> Dict[int, Mapping]:
    """Her macOS ana sürümü için en uygun tekerlek kaydı (``{"filename", …}``).

    Bir sürümde kurulabilen tekerlek yoksa ``ValueError``.
    """
    adaylar = [d for d in dosyalar if str(d.get("filename", "")).endswith(".whl")]
    secim: Dict[int, Mapping] = {}
    for ana in macos_surumleri:
        sirali = sorted(((oncelik(str(d["filename"]), python, (ana, 0)), str(d["filename"]), d)
                         for d in adaylar), key=lambda x: (x[0] is None, x[0] or (), x[1]))
        if not sirali or sirali[0][0] is None:
            raise ValueError(f"macOS {ana} + CPython {python[0]}.{python[1]} için kurulabilen "
                             f"tekerlek yok ({', '.join(sorted(str(d['filename']) for d in adaylar)) or 'tekerlek yok'})")
        secim[ana] = sirali[0][2]
    return secim


def kosullar(secim: Mapping[int, Mapping]) -> List[Tuple[Mapping, Optional[List[str]]]]:
    """Seçimi öğelere çevirir: ``[(kayıt, None)]`` (koşulsuz) ya da her dosya için
    ``kosul.macos`` listesi. En yüksek sürüm ``"<n>+"`` yazılır (n ve sonrası)."""
    surumler = sorted(secim)
    dosyaya_gore: Dict[str, List[int]] = {}
    kayitlar: Dict[str, Mapping] = {}
    for ana in surumler:
        ad = str(secim[ana]["filename"])
        dosyaya_gore.setdefault(ad, []).append(ana)
        kayitlar[ad] = secim[ana]
    if len(dosyaya_gore) == 1:
        ad = next(iter(dosyaya_gore))
        return [(kayitlar[ad], None)]
    en_yuksek = surumler[-1]
    sonuc = []
    for ad, anlar in sorted(dosyaya_gore.items(), key=lambda kv: kv[1][0]):
        sonuc.append((kayitlar[ad], [f"{a}+" if a == en_yuksek else str(a) for a in anlar]))
    return sonuc
