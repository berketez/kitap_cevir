# -*- coding: utf-8 -*-
"""Marker'ın kestiği şekil görsellerini kaynak PDF'ten yeniden kırpma.

Kulvar FX2 (2026-10-05; okuma bulguları, 2026-09-30). Marker şekil
görsellerini 192 dpi'lık sayfa görüntüsünden, düzen modelinin verdiği kutuyla
kırpar (``_page_<sayfa>_Figure_<n>.png``). Kutu dar kalınca grafiğin başlığı,
alt başlığı, lejantı ya da logosu görselin DIŞINDA kalır; bu yazılar PDF'te
çoğu zaman metin değil görselin pikselleridir, yani hiçbir yerde
kurtarılmazlar (kayıp).

Yöntem (her görsel için, yalnız kanıtlı durumda):

1. Görselin sayfadaki yeri şablon eşlemeyle bulunur: sayfa PyMuPDF ile aynı
   çözünürlükte (192 dpi) çizilir, Marker kırpımı bu görüntüde aranır. En iyi
   eşleşme hem çok yakın olmalı (piksel başına ortalama kare fark
   ``ESLESME_EN_COK_MSD``'den küçük) hem de ikinci en iyi yerden belirgin
   biçimde iyi olmalı (``ESLESME_AYIRT_ORANI``); değilse dokunulmaz.
2. Kırpıma büyük ölçüde giren PDF nesneleri (gömülü raster görsel, vektör
   çizim) kırpımın dışına taşıyorsa, nesnenin taşan şeridinde gerçek mürekkep
   varsa (``MUREKKEP_EN_AZ_PIKSEL``; raster için daha düşük eşik) o taraf
   nesnenin kenarına dek genişletilmeye ADAY olur. Kutu dikdörtgen olduğundan
   genişleme o taraftaki bütün bandı görsele katar; bant denetlenir ve bant
   şu satırlardan birine değiyorsa daha yakın adaya inilir ya da taraf
   olduğu gibi kalır: Markdown'da bulunan satır, şekil etiketi olamayacak
   kadar uzun satır (altyazı, gövde metni), yeni kutunun sınırında kesilecek
   satır. Böylece beyaz kenar, kenar yumuşatma ve gövde metni görsele
   alınmaz. Kutu adım adım büyür (üst/alt genişleyince köşedeki logo yan
   şeride girer).
3. Yeni kırpım eskisini KAPSAR (eski kutu yalnız genişler, hiç daralmaz):
   Marker'ın görselde tuttuğu hiçbir şey kaybolmaz.
4. Aynı raster görselin parçası olup genişleyen kırpımın içinde kalan ikinci
   Marker görseli (ör. grafiğin altındaki logo ayrı kırpılmış) artık yinelenen
   parçadır: Markdown'daki satırı kaldırılır (görsel kaybolmaz, büyük
   görselin içindedir).

Değiştirilen her görselin özgünü ``<images>/marker_ozgun/`` altına bir kez
yedeklenir; ``<images>/sekil_kirpma.json`` her değişikliği (eski/yeni kutu,
yeni dosyanın SHA-1'i) kaydeder. Aynı görsele ikinci koşuda dokunulmaz.

Sözleşme: ``onar(markdown, pdf_path, images_dir) -> str``. PDF ya da görsel
klasörü yoksa, PyMuPDF/NumPy/OpenCV yoksa ya da beklenmeyen bir hata olursa
Markdown aynen döner ve hiçbir dosya değişmez (onarım isteğe bağlı bir
iyileştirmedir; boru hattını durdurmaz).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from latex_pipeline import pdf_metin_onarim as _pmo

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sabitler (ölçüm: 5 Eki 2026, korpustaki 18 kitabın 569 Marker görseli;
# gerekçeler FX2/RAPOR.md'de)
# ---------------------------------------------------------------------------

# Marker sayfa görsellerini bu çözünürlükte çizip kırpar (marker
# DocumentBuilder.highres_image_dpi). Dönüştürücünün görsel boyu hesabı da
# aynı değeri kullanır (converter._MARKER_IMAGE_DPI); tek kaynak orası.
from latex_pipeline.converter import _MARKER_IMAGE_DPI as MARKER_DPI  # noqa: E402

# Marker görsel adı: _page_<0 tabanlı sayfa>_<Figure|Picture>_<n>.<uzantı>
# (converter._MARKER_IMAGE_NAME_RE ile aynı biçim; burada sayfa numarası da
# gerekli).
MARKER_AD_RE = re.compile(
    r"^_page_(?P<sayfa>\d+)_(?:Figure|Picture)_\d+\.(?:png|jpe?g)$",
    re.IGNORECASE)
# Markdown'da tek başına satırdaki görsel; önünde Marker'ın bağlantı hedefi
# (<span id="page-23-0"></span>) olabilir (ölçüm: bir test kitabında 117
# görselin 30'u böyle).
GORSEL_SATIRI_RE = re.compile(
    r"^(?P<on>(?:<span\b[^<>\n]*>\s*</span>\s*)*)"
    r"(?P<gorsel>!\[[^\]\n]*\]\((?P<yol>[^)\n]+)\))[ \t]*$", re.MULTILINE)
# Marker bazen ".jpeg" yazıp ".png" kaydeder (converter._resolve_image).
DENENEN_UZANTILAR: Tuple[str, ...] = (".png", ".jpg", ".jpeg")

# Şablon eşleme kabulü. Ölçülen en iyi eşleşme (PyMuPDF çizimi, Marker
# pdfium çizimi): piksel başına ortalama kare fark 1,4–108 (gri düzey²);
# ikinci en iyi yer 500–5700. Eşik bunun rahat üstünde, ikinci yerin rahat
# altında.
ESLESME_EN_COK_MSD = 400.0
# En iyi / ikinci en iyi (en az ESLESME_KOMSU_PX uzakta) oranı en çok bu.
ESLESME_AYIRT_ORANI = 0.35
ESLESME_KOMSU_PX = 6
# Bundan küçük görsel (piksel) aranmaz: tek düze küçük görsel her yere uyar.
EN_KUCUK_GORSEL_PX = 12

# Raster/çizim kırpıma "büyük ölçüde giriyor" sayılır: nesne alanının en az bu
# kadarı kırpımın içinde (ölçülen: 0,69–0,995; ayrı kırpılmış logo parçasını
# taşıyan raster 0,014; taranmış sayfa 0,04–0,2).
NESNE_ICERIDE_EN_AZ = 0.5
# Raster kırpımın da büyük kısmı olmalı (kırpım alanının en az bu kadarı).
KIRPIM_NESNE_EN_AZ = 0.5
# Sayfanın bu kadarını kaplayan nesne sayfa zemini ya da taranmış sayfadır.
SAYFA_ZEMINI_ORANI = 0.8
# Nesne kırpımın dışına en az bu kadar (pt) taşmalı.
TASMA_EN_AZ_PT = 1.0
# Mürekkep: gri düzeyi bundan koyu piksel. Şeritte en az bu kadar mürekkep
# pikseli yoksa genişletilmez. Ölçülen: gerçek kesik (başlık, logo)
# 305–27000 piksel; kenar yumuşatma ve boş kenar 0–44 piksel. Raster
# şeridi rasterin kendi alanıdır (genişleme en kötü ihtimalle rasterin beyaz
# kenarını katar), eşik orada düşüktür: ölçülen bir grafik başlığının ilk harfinin
# kesik 2,5 pt'lik ucu 192 dpi'da ~60 piksel. Vektör çizimde (kırpma yolu
# dışında kalan görünmez parçalar olabilir) yüksek eşik.
MUREKKEP_GRI_ESIGI = 200
MUREKKEP_EN_AZ_PIKSEL = 100
MUREKKEP_EN_AZ_PIKSEL_RASTER = 30
# Banda giren metin satırı: satır yüksekliğinin en az bu kadarı bantta.
# Ölçülen: bir test kitabında 1,5 pt'lik alt bant 12 pt'lik madde satırının
# üstünü (%12,5) kesiyordu; satırın kutu payı (iniş boşluğu) %5'in altında.
BANT_SATIR_ORANI = 0.1
# Satırın Markdown'da aranması için en az normalleştirilmiş uzunluk.
SATIR_EN_AZ_NORM = 3
# Şekil içi etiket kısadır ("Valve", "τ2", "Inlet pipe"); bundan uzun
# satır gövde metni ya da altyazıdır, Markdown'da bulunamasa da (satır sonu
# tiresi, farklı bölünme) şeride giriyorsa genişletilmez. Ölçülen: bir
# Almanca kitapta harita altyazısının ilk satırı 95, en uzun şekil etiketi
# 18 karakter.
ETIKET_EN_UZUN_NORM = 24
# Yinelenen parça: kırpımı genişleyen kırpımın içinde (bu kadar pt pay).
PARCA_PAYI_PT = 1.0

# Kutu genişletme adımı en çok bu kadar yinelenir (her adım kutuyu yalnız
# büyütür; ölçülen olguda en çok 3 adım gerekti).
YINELEME_EN_COK = 6

YEDEK_KLASORU = "marker_ozgun"
KAYIT_DOSYASI = "sekil_kirpma.json"


# ---------------------------------------------------------------------------
# Rapor
# ---------------------------------------------------------------------------

@dataclass
class GorselKaydi:
    """Bir Marker görselinin sayfadaki yeri ve (varsa) yeni kırpımı."""
    ad: str
    sayfa: int
    kirpim: Tuple[float, float, float, float]          # PDF pt (eski)
    yeni: Optional[Tuple[float, float, float, float]] = None
    nedenler: List[str] = field(default_factory=list)  # genişleten nesneler
    parca_kaldirildi: bool = False
    rasterler: List[int] = field(default_factory=list)  # genişleten raster xref'leri


@dataclass
class KirpmaRaporu:
    pdf_var: bool = False
    gorseller: List[GorselKaydi] = field(default_factory=list)
    atlanan: Dict[str, int] = field(default_factory=dict)

    def atla(self, neden: str) -> None:
        self.atlanan[neden] = self.atlanan.get(neden, 0) + 1

    @property
    def genisletilen(self) -> List[GorselKaydi]:
        """Genişlemiş bütün görseller (önceki koşuda genişleyenler dahil)."""
        return [g for g in self.gorseller if g.yeni is not None]

    @property
    def bu_kosuda_genisletilen(self) -> List[GorselKaydi]:
        return [g for g in self.genisletilen if g.nedenler != ["onceki_kosu"]]

    @property
    def kaldirilan_parcalar(self) -> List[GorselKaydi]:
        return [g for g in self.gorseller if g.parca_kaldirildi]

    def sekil_bolgeleri(self) -> Dict[int, List[Tuple[float, float, float, float]]]:
        """Sayfa -> şekil kutuları (PDF pt): görsele çizilmiş alanlar.

        Kayıp metin kurtarma bu alanlardaki metni şekil içi yazı sayar."""
        out: Dict[int, List[Tuple[float, float, float, float]]] = {}
        for g in self.gorseller:
            out.setdefault(g.sayfa, []).append(g.yeni or g.kirpim)
        return out


# ---------------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------------

def _gorsel_dosyasi(images_dir: Path, ad: str) -> Optional[Path]:
    """Markdown'daki ad için diskteki dosya (uzantı farkı tolere edilir)."""
    yol = images_dir / ad
    if yol.is_file():
        return yol
    kok = Path(ad).stem
    for uz in DENENEN_UZANTILAR:
        aday = images_dir / (kok + uz)
        if aday.is_file():
            return aday
    return None


def _sha1(yol: Path) -> str:
    return hashlib.sha1(yol.read_bytes()).hexdigest()


def _kayit_oku(images_dir: Path) -> Dict[str, dict]:
    yol = images_dir / KAYIT_DOSYASI
    try:
        veri = json.loads(yol.read_text(encoding="utf-8"))
        return veri if isinstance(veri, dict) else {}
    except (OSError, ValueError):
        return {}


def _alan(r) -> float:
    return max(0.0, r[2] - r[0]) * max(0.0, r[3] - r[1])


def _kesisim(a, b) -> Tuple[float, float, float, float]:
    return (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))


def _beyaz_mi(renk) -> bool:
    return renk is not None and len(renk) >= 3 and all(v > 0.95 for v in renk[:3])


def _sayfa_cizimi(page, olcek: float):
    """Sayfanın gri ve RGB görüntüsü (NumPy) — 192 dpi."""
    import numpy as np
    import fitz  # PyMuPDF

    pix = page.get_pixmap(matrix=fitz.Matrix(olcek, olcek), alpha=False)
    rgb = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
        pix.height, pix.width, pix.n)[:, :, :3]
    gri = (0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1]
           + 0.114 * rgb[:, :, 2]).astype(np.uint8)
    return gri, rgb


def _yerini_bul(sayfa_gri, gorsel_gri) -> Optional[Tuple[int, int, float]]:
    """Kırpımın sayfa görüntüsündeki sol üst köşesi (px) ve MSD; belirsizse None."""
    import cv2
    import numpy as np

    h, w = gorsel_gri.shape
    H, W = sayfa_gri.shape
    if h < EN_KUCUK_GORSEL_PX or w < EN_KUCUK_GORSEL_PX or h > H or w > W:
        return None
    sonuc = cv2.matchTemplate(sayfa_gri, gorsel_gri, cv2.TM_SQDIFF)
    en_az, _, yer, _ = cv2.minMaxLoc(sonuc)
    x, y = yer
    msd = en_az / float(w * h)
    if msd > ESLESME_EN_COK_MSD:
        return None
    ikinci = sonuc.copy()
    k = ESLESME_KOMSU_PX
    ikinci[max(0, y - k):y + k + 1, max(0, x - k):x + k + 1] = np.inf
    if ikinci.size and np.isfinite(ikinci).any():
        msd2 = float(ikinci.min()) / float(w * h)
        if msd2 <= 0 or msd > ESLESME_AYIRT_ORANI * msd2:
            return None
    return x, y, msd


def _metin_satirlari(page) -> List[Tuple[Tuple[float, float, float, float], str]]:
    satirlar = []
    for blk in page.get_text("dict").get("blocks", []):
        if blk.get("type") != 0:
            continue
        for ln in blk.get("lines", []):
            metin = "".join(sp.get("text", "") for sp in ln.get("spans", []))
            if metin.strip():
                satirlar.append((tuple(ln.get("bbox", (0, 0, 0, 0))), metin))
    return satirlar


class _TembelNorm:
    """Markdown'ın karşılaştırma dizgisi; yalnız gerekince kurulur (büyük
    kitapta birkaç saniye sürer, çoğu kitapta hiç gerekmez)."""

    def __init__(self, markdown: str) -> None:
        self._md = markdown
        self._norm: Optional[str] = None

    def __contains__(self, parca: str) -> bool:
        if self._norm is None:
            self._norm = _pmo._md_akisi_kur(self._md).norm
        return parca in self._norm


# ---------------------------------------------------------------------------
# Genişletme kararı
# ---------------------------------------------------------------------------

def _adaylar(page, kirpim) -> List[Tuple[str, Tuple[float, float, float, float], int]]:
    """Kırpıma büyük ölçüde giren nesneler: (tür, kutu, raster xref ya da -1)."""
    sayfa = tuple(page.rect)
    sayfa_alani = _alan(sayfa)
    out = []
    for info in page.get_image_info(xrefs=True):
        r = _kesisim(tuple(info.get("bbox", (0, 0, 0, 0))), sayfa)
        a = _alan(r)
        if a <= 0 or a >= SAYFA_ZEMINI_ORANI * sayfa_alani:
            continue
        ic = _alan(_kesisim(r, kirpim))
        if ic / a >= NESNE_ICERIDE_EN_AZ and ic / max(_alan(kirpim), 1e-6) >= KIRPIM_NESNE_EN_AZ:
            out.append(("raster", r, int(info.get("xref", 0) or 0)))
    try:
        cizimler = page.get_drawings()
    except Exception:  # noqa: BLE001 - bozuk çizim akışı: yalnız rasterler
        cizimler = []
    for d in cizimler:
        r = _kesisim(tuple(d.get("rect", (0, 0, 0, 0))), sayfa)
        # Sıfır kalınlıklı çizgi: alan hesabı için yarım puan pay.
        rp = (r[0] - 0.5, r[1] - 0.5, r[2] + 0.5, r[3] + 0.5)
        a = _alan(rp)
        if r[2] < r[0] or r[3] < r[1] or a >= SAYFA_ZEMINI_ORANI * sayfa_alani:
            continue
        dolgu, cizgi = d.get("fill"), d.get("color")
        if (dolgu is None or _beyaz_mi(dolgu)) and (cizgi is None or _beyaz_mi(cizgi)):
            continue
        if _alan(_kesisim(rp, kirpim)) / a >= NESNE_ICERIDE_EN_AZ:
            out.append(("cizim", r, -1))
    return out


def _seritler(nesne, kirpim) -> List[Tuple[str, Tuple[float, float, float, float]]]:
    """Nesnenin kırpım dışında kalan şeritleri (taraf, kutu)."""
    x0, y0, x1, y1 = kirpim
    n0, m0, n1, m1 = nesne
    out = []
    if x0 - n0 > TASMA_EN_AZ_PT:
        out.append(("sol", (n0, max(m0, y0), x0, min(m1, y1))))
    if n1 - x1 > TASMA_EN_AZ_PT:
        out.append(("sag", (x1, max(m0, y0), n1, min(m1, y1))))
    if y0 - m0 > TASMA_EN_AZ_PT:
        out.append(("ust", (n0, m0, n1, y0)))
    if m1 - y1 > TASMA_EN_AZ_PT:
        out.append(("alt", (n0, y1, n1, m1)))
    return [(t, s) for t, s in out if s[2] > s[0] and s[3] > s[1]]


def _murekkep(sayfa_gri, serit, olcek: float) -> int:
    H, W = sayfa_gri.shape
    x0 = max(0, int(round(serit[0] * olcek)))
    y0 = max(0, int(round(serit[1] * olcek)))
    x1 = min(W, int(round(serit[2] * olcek)))
    y1 = min(H, int(round(serit[3] * olcek)))
    if x1 <= x0 or y1 <= y0:
        return 0
    return int((sayfa_gri[y0:y1, x0:x1] < MUREKKEP_GRI_ESIGI).sum())


def _bant_engeli(bant, kutu, satirlar, md_norm) -> bool:
    """Görsele eklenecek bantta gövde metni var mı?

    Bant, yeni kutunun eski kırpım dışında kalan kısmıdır (kutu bir
    dikdörtgen olduğundan bir tarafı genişletmek o taraftaki bütün şeridi
    görsele katar). Banda giren her satır incelenir; şunlar engeldir:
    Markdown'da bulunan, şekil etiketi olamayacak kadar uzun ya da yeni
    kutunun sınırında kesilecek olan (kutu satırın tamamını almıyor)."""
    if bant[2] <= bant[0] or bant[3] <= bant[1]:
        return False
    pay = (kutu[0] - PARCA_PAYI_PT, kutu[1] - PARCA_PAYI_PT,
           kutu[2] + PARCA_PAYI_PT, kutu[3] + PARCA_PAYI_PT)
    for satir, metin in satirlar:
        a = _alan(satir)
        if a <= 0:
            continue
        k = _kesisim(satir, bant)
        if k[2] - k[0] <= 0.5 or (k[3] - k[1]) < BANT_SATIR_ORANI * (satir[3] - satir[1]):
            continue
        norm = "".join(_pmo._norm_karakter(c) for c in metin)
        if len(norm) >= ETIKET_EN_UZUN_NORM:
            return True
        if _alan(_kesisim(satir, pay)) < 0.999 * a:
            return True
        if len(norm) >= SATIR_EN_AZ_NORM and norm in md_norm:
            return True
    return False


def _bantlar(eski, yeni) -> Dict[str, Tuple[float, float, float, float]]:
    """Yeni kutunun eski kutu dışında kalan dört bandı (köşeler üst/alt
    bantta)."""
    x0, y0, x1, y1 = eski
    X0, Y0, X1, Y1 = yeni
    return {"ust": (X0, Y0, X1, y0), "alt": (X0, y1, X1, Y1),
            "sol": (X0, y0, x0, y1), "sag": (x1, y0, X1, y1)}


def _kenar(kutu, taraf: str) -> float:
    return {"sol": kutu[0], "ust": kutu[1], "sag": kutu[2], "alt": kutu[3]}[taraf]


def _kenari_uygula(kutu, taraf: str, kenar: float):
    x0, y0, x1, y1 = kutu
    if taraf == "sol":
        x0 = min(x0, kenar)
    elif taraf == "sag":
        x1 = max(x1, kenar)
    elif taraf == "ust":
        y0 = min(y0, kenar)
    else:
        y1 = max(y1, kenar)
    return (x0, y0, x1, y1)


def _yeni_kirpim(page, kirpim, sayfa_gri, olcek, satirlar, md_norm, rapor
                 ) -> Tuple[Tuple[float, float, float, float], List[str], set]:
    """Genişletilmiş kırpım, nedenler ve genişleten rasterlerin xref'leri.

    Kutu adım adım büyür: her adımda adayların O ANKİ kutu dışında kalan
    şeritleri mürekkep kanıtıyla öneri olur (üst/alt genişleyince köşedeki
    logo gibi içerik yan şeride girer, bir sonraki adımda görülür). Her
    tarafta en uzak öneriden başlanır; yeni kutunun eski kırpım dışında
    kalan bantları gövde metnine çarpıyorsa daha yakın öneriye inilir,
    hiçbiri olmazsa o taraf olduğu gibi kalır."""
    adaylar = _adaylar(page, kirpim)
    kutu = kirpim
    nedenler = set()
    rasterler = set()
    reddedilen = set()
    for _ in range(YINELEME_EN_COK):
        oneriler: Dict[str, List[Tuple[float, str, int]]] = {
            "sol": [], "sag": [], "ust": [], "alt": []}
        for tur, nesne, xref in adaylar:
            for taraf, serit in _seritler(nesne, kutu):
                kenar = _kenar(nesne, taraf)
                if (taraf, round(kenar, 2)) in reddedilen:
                    continue
                esik = (MUREKKEP_EN_AZ_PIKSEL_RASTER if tur == "raster"
                        else MUREKKEP_EN_AZ_PIKSEL)
                if _murekkep(sayfa_gri, serit, olcek) < esik:
                    rapor.atla("serit_bos")
                    continue
                oneriler[taraf].append((kenar, tur, xref))
        yeni = kutu
        for taraf, liste in oneriler.items():
            liste.sort(key=lambda o: o[0], reverse=taraf in ("sag", "alt"))
            for i, (kenar, _tur, _xref) in enumerate(liste):
                aday = _kenari_uygula(yeni, taraf, kenar)
                if any(_bant_engeli(b, aday, satirlar, md_norm)
                       for b in _bantlar(kirpim, aday).values()):
                    reddedilen.add((taraf, round(kenar, 2)))
                    rapor.atla("serit_govde_metni")
                    continue
                yeni = aday
                # Seçilen kenarın içinde kalan bütün öneriler tamamlandı.
                for _k, tur, xref in liste[i:]:
                    nedenler.add(f"{tur}:{taraf}")
                    if tur == "raster":
                        rasterler.add(xref)
                break
        if yeni == kutu:
            break
        kutu = yeni
    return kutu, sorted(nedenler), rasterler


# ---------------------------------------------------------------------------
# Genel giriş
# ---------------------------------------------------------------------------

def onar_raporlu(markdown: str, pdf_path: Optional[str],
                 images_dir: Optional[str], yaz: bool = True
                 ) -> Tuple[str, KirpmaRaporu]:
    """``onar`` + rapor. ``yaz=False``: dosyalara dokunmadan yalnız ölç."""
    rapor = KirpmaRaporu()
    if not markdown or not pdf_path or not images_dir:
        return markdown, rapor
    pdf = Path(str(pdf_path))
    img_dir = Path(str(images_dir))
    if pdf.suffix.lower() != ".pdf" or not pdf.is_file() or not img_dir.is_dir():
        return markdown, rapor
    try:
        import cv2  # noqa: F401
        import fitz  # PyMuPDF
        import numpy as np  # noqa: F401
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - venv'de kurulu
        logger.info("FX2 şekil kırpma atlandı (kütüphane yok): %s", exc)
        return markdown, rapor

    # Markdown'daki Marker görselleri: ad -> satır başlangıç ofsetleri.
    satir_ofsetleri: Dict[str, List[Tuple[int, int]]] = {}
    for m in GORSEL_SATIRI_RE.finditer(markdown):
        ad = Path(m.group("yol").strip()).name
        if MARKER_AD_RE.match(ad):
            satir_ofsetleri.setdefault(ad, []).append((m.start(), m.end()))
    if not satir_ofsetleri:
        return markdown, rapor

    try:
        doc = fitz.open(str(pdf))
    except Exception as exc:  # noqa: BLE001 - bozuk PDF: Markdown aynen
        logger.info("FX2: PDF açılamadı (%s): %s", pdf, exc)
        return markdown, rapor
    kayit = _kayit_oku(img_dir)
    olcek = MARKER_DPI / 72.0
    md_norm = _TembelNorm(markdown)
    kaldirilacak: List[str] = []
    yazilacak: List[Tuple[GorselKaydi, Path, "object"]] = []
    try:
        if getattr(doc, "needs_pass", False):
            return markdown, rapor
        rapor.pdf_var = True
        sayfaya_gore: Dict[int, List[Tuple[str, Path]]] = {}
        gorulen = set()
        for ad in satir_ofsetleri:
            dosya = _gorsel_dosyasi(img_dir, ad)
            if dosya is None:
                rapor.atla("dosya_yok")
                continue
            if dosya.name in gorulen:      # aynı dosyaya iki ad (.jpeg/.png)
                continue
            gorulen.add(dosya.name)
            sayfa = int(MARKER_AD_RE.match(ad).group("sayfa"))
            if sayfa >= len(doc):
                rapor.atla("sayfa_yok")
                continue
            sayfaya_gore.setdefault(sayfa, []).append((ad, dosya))
        for sayfa in sorted(sayfaya_gore):
            page = doc[sayfa]
            if page.rotation:
                rapor.atla("donuk_sayfa")
                continue
            sayfa_gri, sayfa_rgb = _sayfa_cizimi(page, olcek)
            satirlar = _metin_satirlari(page)
            kayitlar: List[Tuple[GorselKaydi, Path, Optional[Tuple[int, int, int, int]], set]] = []
            for ad, dosya in sayfaya_gore[sayfa]:
                onceki = kayit.get(dosya.name)
                if onceki and onceki.get("sha1") == _sha1(dosya):
                    # Önceki koşu genişletti: dosyaya dokunulmaz; bölge ve
                    # genişleten rasterler kayıttan gelir (yinelenen parça
                    # denetimi her koşuda yeniden yapılır: Markdown her
                    # dönüşümde full_book.md'den taze okunur).
                    g = GorselKaydi(dosya.name, sayfa, tuple(onceki.get("eski", (0, 0, 0, 0))),
                                    tuple(onceki.get("yeni", (0, 0, 0, 0))),
                                    ["onceki_kosu"])
                    kayitlar.append((g, dosya, None, set(onceki.get("rasterler", ()))))
                    rapor.gorseller.append(g)
                    rapor.atla("zaten_genisletilmis")
                    continue
                try:
                    with Image.open(dosya) as im:
                        gorsel_gri = np.asarray(im.convert("L"))
                except Exception:  # noqa: BLE001 - okunamayan görsel
                    rapor.atla("gorsel_okunamadi")
                    continue
                yer = _yerini_bul(sayfa_gri, gorsel_gri)
                if yer is None:
                    rapor.atla("yer_bulunamadi")
                    continue
                px, py, _msd = yer
                h, w = gorsel_gri.shape
                kirpim = (px / olcek, py / olcek, (px + w) / olcek, (py + h) / olcek)
                yeni, nedenler, rasterler = _yeni_kirpim(
                    page, kirpim, sayfa_gri, olcek, satirlar, md_norm, rapor)
                g = GorselKaydi(dosya.name, sayfa, kirpim)
                if yeni != kirpim:
                    g.yeni = yeni
                    g.nedenler = nedenler
                kayitlar.append((g, dosya, (px, py, w, h), rasterler))
                rapor.gorseller.append(g)
            # Yinelenen parça: aynı rasterin parçası olan ve bütün mürekkebi
            # genişleyen kırpımın içinde kalan ikinci görsel.
            for g, dosya, _px, _r in kayitlar:
                if g.yeni is not None:
                    continue
                for g2, _d2, _p2, rasterler2 in kayitlar:
                    if g2 is g or g2.yeni is None or not rasterler2:
                        continue
                    if _parca_mi(g.kirpim, g2.yeni, page, rasterler2,
                                 sayfa_gri, olcek):
                        g.parca_kaldirildi = True
                        kaldirilacak.append(g.ad)
                        break
            for g, dosya, piksel, rasterler in kayitlar:
                if g.yeni is None or piksel is None:
                    continue
                px, py, w, h = piksel
                g.rasterler = sorted(rasterler)
                H, W = sayfa_gri.shape
                nx0 = max(0, min(px, int(round(g.yeni[0] * olcek))))
                ny0 = max(0, min(py, int(round(g.yeni[1] * olcek))))
                nx1 = min(W, max(px + w, int(round(g.yeni[2] * olcek))))
                ny1 = min(H, max(py + h, int(round(g.yeni[3] * olcek))))
                g.yeni = (nx0 / olcek, ny0 / olcek, nx1 / olcek, ny1 / olcek)
                kesit = sayfa_rgb[ny0:ny1, nx0:nx1].copy()
                yazilacak.append((g, dosya, kesit))
    finally:
        doc.close()

    md = markdown
    if kaldirilacak:
        # Parça görselin satırı (ve ardından gelen boş satır) kaldırılır.
        kaldir = set(kaldirilacak)
        parcalar = []
        son = 0
        for m in GORSEL_SATIRI_RE.finditer(markdown):
            ad = Path(m.group("yol").strip()).name
            dosya = _gorsel_dosyasi(img_dir, ad)
            if dosya is None or dosya.name not in kaldir:
                continue
            if m.group("on"):
                # Bağlantı hedefi kalır, yalnız görsel çıkar.
                parcalar.append(markdown[son:m.start("gorsel")])
                son = m.end()
                continue
            bitis = m.end()
            if markdown.startswith("\n\n", bitis):
                bitis += 2
            elif markdown.startswith("\n", bitis):
                bitis += 1
            parcalar.append(markdown[son:m.start()])
            son = bitis
        parcalar.append(markdown[son:])
        md = "".join(parcalar)
    if yaz and yazilacak:
        from PIL import Image as _Image

        yedek_dir = img_dir / YEDEK_KLASORU
        for g, dosya, kesit in yazilacak:
            try:
                yedek_dir.mkdir(exist_ok=True)
                yedek = yedek_dir / dosya.name
                if not yedek.exists():
                    shutil.copy2(dosya, yedek)
                _Image.fromarray(kesit).save(str(dosya))
                kayit[dosya.name] = {
                    "sayfa": g.sayfa,
                    "eski": [round(v, 2) for v in g.kirpim],
                    "yeni": [round(v, 2) for v in g.yeni],
                    "nedenler": g.nedenler,
                    "rasterler": g.rasterler,
                    "sha1": _sha1(dosya),
                }
            except OSError as exc:
                logger.warning("FX2: %s yazılamadı: %s", dosya.name, exc)
                rapor.atla("yazilamadi")
        try:
            (img_dir / KAYIT_DOSYASI).write_text(
                json.dumps(kayit, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError as exc:
            logger.warning("FX2: şekil kırpma kaydı yazılamadı: %s", exc)
    if rapor.genisletilen or rapor.kaldirilan_parcalar:
        logger.info("FX2: %d şekil görseli kaynak PDF'ten genişletildi (%d önceki "
                    "koşuda), %d yinelenen parça görsel kaldırıldı",
                    len(rapor.genisletilen),
                    len(rapor.genisletilen) - len(rapor.bu_kosuda_genisletilen),
                    len(rapor.kaldirilan_parcalar))
    return md, rapor


def _parca_mi(kirpim, buyuk, page, rasterler, sayfa_gri, olcek) -> bool:
    """kirpim, genişlemiş ``buyuk`` kırpımın yinelenen parçası mı?

    Koşullar: kırpım, ``buyuk``u genişleten rasterlerden birinin içinde
    (alanının en az %95'i) ve kırpımın ``buyuk`` dışında kalan kısmında
    mürekkep yok (içeriğinin tamamı büyük görselde zaten var)."""
    if not any(_alan(_kesisim(kirpim, r_kutu)) >= 0.95 * _alan(kirpim)
               for _tur, r_kutu, xref in _adaylar_raster(page) if xref in rasterler):
        return False
    disari = 0
    x0, y0, x1, y1 = kirpim
    b0, c0, b1, c1 = buyuk
    for serit in ((x0, y0, min(x1, b0), y1), (max(x0, b1), y0, x1, y1),
                  (max(x0, b0), y0, min(x1, b1), min(y1, c0)),
                  (max(x0, b0), max(y0, c1), min(x1, b1), y1)):
        if serit[2] > serit[0] and serit[3] > serit[1]:
            disari += _murekkep(sayfa_gri, serit, olcek)
    return disari < MUREKKEP_EN_AZ_PIKSEL


def _adaylar_raster(page):
    """Sayfadaki raster yerleşimleri (tür, kutu, xref) — parça denetimi için."""
    sayfa = tuple(page.rect)
    for info in page.get_image_info(xrefs=True):
        r = _kesisim(tuple(info.get("bbox", (0, 0, 0, 0))), sayfa)
        if _alan(r) > 0:
            yield ("raster", r, int(info.get("xref", 0) or 0))


def onar(markdown: str, pdf_path: Optional[str], images_dir: Optional[str]) -> str:
    """Kesilmiş şekil görsellerini kaynak PDF'ten genişlet; Markdown'ı döndür.

    Dönen Markdown'dan yalnız yinelenen parça görsellerin satırları çıkmış
    olabilir. Hata durumunda Markdown aynen döner.
    """
    try:
        return onar_raporlu(markdown, pdf_path, images_dir)[0]
    except Exception as exc:  # noqa: BLE001 - onarım boru hattını durdurmaz
        logger.warning("FX2: şekil kırpma atlandı (%s): %s",
                       type(exc).__name__, exc, exc_info=True)
        return markdown
