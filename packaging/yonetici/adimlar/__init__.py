"""Kurulum adımları (TASARIM C.2 ``adimlar[].tur`` ve ``dogrulama.tur``).

Adım türü → işlev eşlemesinin TEK yeri burasıdır; manifest şemasındaki
listeyle birebir aynıdır (``packaging/manifest.schema.json``, C.2).
Alanların tablosu ``kurulum.py`` belgesindedir.
"""

from __future__ import annotations

from typing import Callable, Dict

from . import arsiv, modeller, paketler, tex
from .ortak import AdimHatasi, AdimSonucu, Baglam, IptalEdildi

AdimIslevi = Callable[..., AdimSonucu]

ADIMLAR: Dict[str, AdimIslevi] = {
    "uv_kur": arsiv.uv_kur,
    "tekerlek_kur": paketler.tekerlek_kur,
    "arsiv_ac": arsiv.arsiv_ac,
    "dosya_yerlestir": modeller.dosya_yerlestir,
    "tectonic_isit": tex.tectonic_isit,
    "mactex_pkg_ac": tex.mactex_pkg_ac,
    "tex_algila_sina": tex.tex_algila_sina,
    "lisans_yaz": modeller.lisans_yaz,
    "surya_manifest_yaz": modeller.surya_manifest_yaz,
}

# Kendi öğesini ``adim["oge"]`` alanıyla tüketen adımlar (öteki öğeler bileşenin
# başında "kalıcı" olarak iner; tekerlekler tekerlek_kur'un).
OGE_TUKETEN_ADIMLAR = frozenset({"uv_kur", "arsiv_ac", "mactex_pkg_ac", "lisans_yaz"})
TEKERLEK_ADIMI = "tekerlek_kur"
TEKERLEK_TURU = "tekerlek"

__all__ = ["ADIMLAR", "OGE_TUKETEN_ADIMLAR", "TEKERLEK_ADIMI", "TEKERLEK_TURU",
           "AdimHatasi", "AdimSonucu", "Baglam", "IptalEdildi"]
