"""Özgün kapak kararı (G3b, 2026-10-05; Berke kararı).

Kaynak PDF'in 1. sayfası **görsel ağırlıklıysa** (kitap kapağı) çevirinin
başına aynen basılır (``\\includepdf``); **metin sayfasıysa** (dizgiyle
kurulmuş başlık sayfası, logolu künye/başlık sayfası, bölüm açılışı) ya da
**boşsa** basılmaz. Basılmayan sayfanın içeriği zaten çevrilmiş gövdededir
(dönüştürücünün kurduğu Türkçe başlık sayfası korunur): kayıp yoktur.

Eski kural (R1, 2026-09-25) sözcük sayısına bakıyordu: 40 ya da daha az
sözcüklü her 1. sayfa kapak sayılıyordu. Sonuç: bir raporun metinli başlık
sayfası (44 sözcük + küçük logo), görselsiz başlık sayfaları (12–35 sözcük)
ve boş 1. sayfalar (5 PDF) İngilizce/boş sayfa olarak basılıyordu.

Ölçü: **metin dışı mürekkep oranı** — 1. sayfa düşük çözünürlükte gri
düzeyde çizilir; sözcük kutularının (metin katmanı; taranmış sayfada OCR)
DIŞINDA kalan mürekkepli piksellerin sayfa alanına oranı. Raster görsel,
vektör çizim, gölgeleme (shading) hepsi okurun gördüğü gibi sayılır;
kâğıt beyazı taranmış sayfa ise mürekkep sayılmaz.

Ölçülen (2026-10-05, test korpusundaki 31 benzersiz kaynak PDF; tablo iç
ölçüm notunda):
  - kapaklar (7 PDF):                    0,70 – 0,94
  - metin sayfaları (1. sayfa):          0,00 – 0,07
  - görselli gövde sayfaları (en çok):   0,37
  - boş sayfa:                           0,000 (toplam mürekkep de 0)
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Eşikler (ölçüm gerekçeli)
# ---------------------------------------------------------------------------
# Çizim ölçeği: 1/5 (612 pt'lık sayfa ~122 piksel). Oran ölçümü için yeterli,
# sayfa başına birkaç milisaniye.
_RENDER_ZOOM: float = 0.2
# Gri düzey (0–255) bunun altındaysa piksel mürekkeptir. Kâğıt beyazı ve
# taranmış kâğıt bunun üstünde kalır (taranmış bir metin sayfasında metin
# dışı mürekkep 0,04).
_INK_GRAY_MAX: int = 240
# 1. sayfanın metin dışı mürekkep oranı en az bu kadarsa görsel ağırlıklıdır.
# Kapaklar >= 0,70, metin sayfaları <= 0,07, görselli gövde sayfası <= 0,37
# ölçüldü; eşik iki küme arasında, iki yana da geniş pay bırakır.
_VISUAL_MIN: float = 0.5
# 1. sayfa, gövde sayfalarının medyanından en az bu kadar daha görsel
# olmalı; değilse (bütün sayfalar koyu/görselli: taranmış gri kâğıt,
# resimli kitap) görsel ayırt etmez, sözcük sayısı karar verir. Ölçülen
# kapak–gövde farkı >= 0,66.
_VISUAL_MARGIN: float = 0.25
# Görsel ayırt etmediğinde: 1. sayfa sözcüğü / gövde sözcük medyanı bu oranın
# altındaysa kapak. Ölçülen (R1, 2026-09-25): kapaklar <= 0,10, metin
# sayfaları >= 0,71.
_TEXT_RATIO: float = 0.5
# Gövde ölçümü için 1. sayfadan sonra bakılan en çok sayfa ve sözcük
# karşılaştırmasına girecek gövde sayfasının en az sözcüğü.
_SAMPLE_PAGES: int = 10
_MIN_BODY_WORDS: int = 20
# Sözcüksüz ve toplam mürekkebi bu oranın altında olan sayfa boştur
# (ölçülen boş sayfalar: 0,000; en seyrek metin sayfası: 0,018).
_BLANK_INK_MAX: float = 0.005


@dataclass(frozen=True)
class KapakKarari:
    """``basilir``: True (kapak, basılır), False (basılmaz), None (ölçülemedi)."""
    basilir: Optional[bool]
    neden: str
    ilk_gorsel: float = 0.0
    ilk_sozcuk: int = 0
    govde_gorsel: Optional[float] = None
    govde_sozcuk: Optional[float] = None


def sayfa_olcusu(page, fitz) -> Tuple[float, float, int]:
    """(metin dışı mürekkep oranı, toplam mürekkep oranı, sözcük sayısı)."""
    words = page.get_text("words")
    pix = page.get_pixmap(matrix=fitz.Matrix(_RENDER_ZOOM, _RENDER_ZOOM),
                          colorspace=fitz.csGRAY, alpha=False)
    w, h, stride = pix.width, pix.height, pix.stride
    if w <= 0 or h <= 0:
        return 0.0, 0.0, len(words)
    buf = bytearray(pix.samples)
    ink_table = bytes(1 if v < _INK_GRAY_MAX else 0 for v in range(256))
    total_ink = sum(buf[y * stride:y * stride + w].translate(ink_table).count(1)
                    for y in range(h))
    # Sözcük kutuları beyaza boyanır: kalan mürekkep metin dışıdır.
    rot = page.rotation_matrix if page.rotation else None
    for wd in words:
        r = fitz.Rect(wd[:4])
        if rot is not None:
            r = r * rot
        x0 = max(0, int(r.x0 * _RENDER_ZOOM))
        x1 = min(w, int(r.x1 * _RENDER_ZOOM + 0.999))
        y0 = max(0, int(r.y0 * _RENDER_ZOOM))
        y1 = min(h, int(r.y1 * _RENDER_ZOOM + 0.999))
        if x1 <= x0:
            continue
        white = b"\xff" * (x1 - x0)
        for y in range(y0, y1):
            off = y * stride
            buf[off + x0:off + x1] = white
    out_ink = sum(buf[y * stride:y * stride + w].translate(ink_table).count(1)
                  for y in range(h))
    area = float(w * h)
    return out_ink / area, total_ink / area, len(words)


def kapak_karari(pdf_path: str) -> KapakKarari:
    """Kaynak PDF'in 1. sayfası kapak olarak basılsın mı?"""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return KapakKarari(None, "PyMuPDF yok")
    try:
        doc = fitz.open(pdf_path)
    except Exception:  # bozuk / PDF olmayan dosya
        return KapakKarari(None, "PDF açılamadı")
    try:
        if doc.page_count == 0:
            return KapakKarari(None, "PDF'te sayfa yok")
        try:
            g1, ink1, w1 = sayfa_olcusu(doc[0], fitz)
            govde: List[Tuple[float, float, int]] = [
                sayfa_olcusu(doc[i], fitz)
                for i in range(1, min(doc.page_count, 1 + _SAMPLE_PAGES))]
        except Exception as exc:  # çizim/metin çıkarımı başarısız
            return KapakKarari(None, f"ölçülemedi ({exc})")
    finally:
        doc.close()
    return _karar(g1, ink1, w1, govde)


def _karar(g1: float, ink1: float, w1: int,
           govde: List[Tuple[float, float, int]]) -> KapakKarari:
    def sonuc(basilir: Optional[bool], neden: str,
              gg: Optional[float] = None,
              gw: Optional[float] = None) -> KapakKarari:
        return KapakKarari(basilir, neden, round(g1, 3), w1,
                           None if gg is None else round(gg, 3), gw)

    if w1 == 0 and ink1 < _BLANK_INK_MAX:
        return sonuc(False, "boş sayfa")
    if g1 < _VISUAL_MIN:
        return sonuc(False, f"metin sayfası (görsel oranı {g1:.2f})")
    if not govde:
        return sonuc(True, f"görsel ağırlıklı (görsel oranı {g1:.2f}, tek sayfa)")
    gg = statistics.median(g for g, _, _ in govde)
    if g1 - gg >= _VISUAL_MARGIN:
        return sonuc(True, f"görsel ağırlıklı (görsel oranı {g1:.2f}, "
                           f"gövde {gg:.2f})", gg)
    # Gövde de görsel ağırlıklı: görsel ayırt etmez, metin miktarı bakılır.
    body_words = [w for _, _, w in govde if w >= _MIN_BODY_WORDS]
    if not body_words:
        return sonuc(True, "görsel ağırlıklı; karşılaştıracak metin yok", gg)
    med = statistics.median(body_words)
    if w1 < _TEXT_RATIO * med:
        return sonuc(True, f"görsel ve az metinli ({w1} sözcük, gövde "
                           f"medyanı {med:g})", gg, med)
    return sonuc(False, f"görselli metin sayfası ({w1} sözcük, gövde "
                        f"medyanı {med:g})", gg, med)
