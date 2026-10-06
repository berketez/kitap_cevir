# -*- coding: utf-8 -*-
"""Marker'ın düşürdüğü metni kaynak PDF'in metin katmanından kurtarma.

Kulvar FX2 (2026-10-05; okuma bulgusu, 2026-09-30): bir ara başlık ne Marker
Markdown'ında ne yer iminde vardı, PDF'in metin katmanında ise sayfanın son
gövde satırıydı. Kayıpsızlık kuralı gereği PDF'te olup Markdown'da HİÇ
bulunmayan satırlar doğru yere geri konur.

Yöntem:

1. PDF satırları (PyMuPDF "dict", görünmez metin hariç) okunur, her satır
   G1c'nin karşılaştırma biçimine (``pdf_metin_onarim._norm_karakter``)
   çevrilir. Markdown da aynı biçime çevrilir; burada kod blokları ve HTML
   etiketine benzeyen düz metin ("<bir not>") METİN sayılır (G1c onları
   yok sayar), gerçek etiketler (``<sup>``, ``<span id=…>``) atlanır,
   matematik, görsel ve bağlantı adresi engel olur.
2. PDF satırları sırayla Markdown'da aranır (kayan pencere; bulunamayan uzun
   satır için bütün belge). Satır Markdown'da hiç yoksa ve 16 karakterlik
   hiçbir parçası da yoksa "kayıp aday"dır.
3. Aday ancak şu koşullarla eklenir (muhafazakâr; emin olunmayan dokunulmaz):
   - sayfa üst/alt bilgisi, sayfa numarası (KARAR 32), sayfa kenarındaki
     satır, dikey yazı ve şekil içi yazı (görsel ya da Marker şekil kırpımı
     içinde) DEĞİL;
   - PDF'te adaydan önceki ve sonraki güvenilir satırlar (uzun, Markdown'da
     tek kez geçen) Markdown'da arada BAŞKA HİÇBİR ŞEY olmadan bitişik:
     aday tam olarak o boşluktan düşmüştür;
   - boşluk paragraf sınırıysa aday ayrı paragraf (stili Markdown'daki aynı
     yazı tipli bir başlıkla eşleşirse o düzeyde başlık) olur; paragraf
     içiyse satır içine sözcük boşluğuyla girer; ikisi de değilse dokunulmaz.
4. Güvenlik ağı: çıktının karşılaştırma dizgisi, girdininkine yalnız eklenen
   satırların PDF'teki metni eklenmiş hâli olmalıdır; değilse hiçbir ekleme
   yapılmaz.

Sözleşme: ``onar(markdown, pdf_path, sekil_bolgeleri=None) -> str``. PDF
yoksa, açılamazsa, metin katmanı yoksa ya da beklenmeyen bir hata olursa
Markdown aynen döner. ``sekil_bolgeleri`` (sayfa -> PDF pt kutuları) Marker'ın
şekil görsellerinin sayfadaki yerleridir (``sekil_kirpma`` raporu); bu
alanlardaki yazı görselin içindedir, metin olarak eklenmez.
"""
from __future__ import annotations

import html
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from latex_pipeline import pdf_metin_onarim as _pmo

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sabitler (ölçüm: 5 Eki 2026, korpustaki 18 kitap; gerekçeler FX2/RAPOR.md)
# ---------------------------------------------------------------------------

# Arama penceresi (karşılaştırma karakteri): Marker sırası PDF'e yakındır;
# dipnot/altyazı gibi yer değiştiren parçalar pencere dışında bütün belgede
# aranır.
PENCERE_GERI = 2000
PENCERE_ILERI = 8000
# İmleci ilerletecek ve çapa olabilecek en kısa satır (karakter).
CAPA_EN_KISA = 12
# Bundan kısa satır aday olmaz ve yer belirlemede kullanılmaz ("1", "a)").
ADAY_EN_KISA = 4
# Kısmi varlık: satırın bu uzunluktaki bir parçası imlecin hemen yakınında
# (önceki satırın bittiği yer) bulunursa satır yerindedir, kayıp değildir
# (içinde matematik, farklı bölünme vb. var). Yakınlık dar ve İLERİ yönlü
# tutulur: başlık, hemen önceki paragrafın bir ifadesini yineleyebilir.
PARCA_PENCERE = 12
PARCA_ADIM_PENCERE = 6
PARCA_YAKIN_GERI = 20
PARCA_YAKIN_ILERI = 1500
# Uzun satırın 16 karakterlik parçalarının en az yarısı belgenin başka bir
# yerinde varsa satır kayıp sayılmaz (Marker taşıyıp değiştirmiş olabilir;
# yinelenme tehlikesi). Kısa satır için tam eşleşme denetimi yeter: taşınmış
# ya da paragrafa yapışmış kısa satır zaten aynen bulunur.
PARCA_BELGE = 16
PARCA_ADIM_BELGE = 8
PARCA_BELGE_EN_KISA_SATIR = 40
PARCA_BELGE_ORANI = 0.5
# Ekleme yerinin bu kadar (karşılaştırma karakteri) yakınında adayın en az
# bu oranı (12 karakterlik pencerelerle örtülen) varsa eklenmez: yer
# değiştirmiş biçimde duruyor. Ölçülen: bir test kitabında tablo hücresi (dipnot
# işareti hücre başına taşınmış) %92; önceki cümleyle ifade paylaşan başlık
# %71.
YAKIN_PARCA_PAYI = 400
YAKIN_PARCA_ORTUSU = 0.8
# Kayıp öbeği ile çapalar arasında en çok bu kadar PDF satırı olabilir.
OBEK_EN_UZUN = 12
# Üst/alt bilgi: rakamları maskelenmiş aynı satır en az bu kadar sayfanın ilk
# ya da son SAYFA_UC_SATIR satırında geçiyorsa sayfalama artığıdır.
UST_ALT_EN_AZ_SAYFA = 3
SAYFA_UC_SATIR = 2
# Sayfa numarası bandı: sayfanın en üst/en alt satırı olan sayfa numaraları
# (en az KENAR_BANDI_EN_AZ tane) üst ve alt bilgi bandını belirler; bandın
# içindeki satır sayfalama artığıdır. Band sınırı, numaraların dikey
# konumlarının uç yüzdeliğinden (KENAR_BANDI_YUZDELIK) KENAR_BANDI_PAYI
# (sayfa yüksekliği oranı) kadar içeridedir. Ölçülen: bir test kitabında numaralar 742 pt
# (0,937), sayfa sonundaki kayıp başlık 707 pt (0,893).
KENAR_BANDI_EN_AZ = 3
KENAR_BANDI_YUZDELIK = 0.1
KENAR_BANDI_PAYI = 0.01
# Gövde puntosu sayılan punto aralığı (en yaygın puntoya göre oran). Dar:
# 10 pt üst bilgi 11 pt gövdenin çerçevesine sayılmamalı (ölçüm: bir test
# kitabında %12'lik pay üst bilgiyi gövde çerçevesine katıyordu).
GOVDE_PUNTO_PAYI = 0.05
# Başlık adayı: tek satır, en çok bu kadar karakter. Düzeyi, aynı yazı
# tipi/punto/kalınlıktaki satırların Markdown'daki başlık düzeyinden gelir;
# eşleşme yoksa kalın satır kalın paragraf olur.
BASLIK_EN_UZUN = 150
# Başlık stili eşleştirmede punto toleransı (pt).
STIL_PUNTO_TOLERANSI = 0.25
# Satır başındaki madde imleri karşılaştırmada yok sayılır (Markdown'da liste
# imi "-" olur).
MADDE_IMLERI = frozenset("○◦●•▪■□◆◇►▸‣⁃∙·–")
# Gerçek HTML etiketi: bilinen ad + yalnız ad=değer öznitelikleri. "<a command
# that …>" ya da "<the same issue>" düz metindir.
_HTML_ADLARI = ("sup|sub|br|span|a|i|b|u|em|strong|p|div|small|s|del|ins|mark|"
                "code|pre|img|table|tr|td|th|thead|tbody|ol|ul|li|hr|math|"
                "blockquote|h[1-6]")
_GERCEK_ETIKET_RE = re.compile(
    r"</?(?:" + _HTML_ADLARI + r")(?:\s+[\w:-]+\s*=\s*(?:\"[^\"\n]*\"|'[^'\n]*'"
    r"|[^\s\"'>]+))*\s*/?>", re.IGNORECASE)
_VARLIK_RE = re.compile(r"&(?:#\d{1,6}|#x[0-9A-Fa-f]{1,5}|[A-Za-z]{2,8});")
_GORSEL_RE = re.compile(r"!\[[^\]\n]*\]\([^)\n]*\)")
_TABLO_AYRAC_RE = re.compile(r"^\s*\|?\s*:?-{3,}")
_LISTE_IMI_RE = re.compile(r"^[ \t]*(?:[-+*]|\d{1,3}[.)])(?=[ \t])")
_BASLIK_IMI_RE = re.compile(r"^[ \t]*(#{1,6})[ \t]+")
_ALINTI_IMI_RE = re.compile(r"^[ \t]*(?:>[ \t]?)+")
_SAYFA_NO_RE = re.compile(r"^[\s\divxlcdmIVXLCDM.,:;\-–—()\[\]/|]+$")
_SAYFA_NO_KESIN_RE = re.compile(r"^\s*(?:\d{1,4}|[ivxlcdm]{1,7}|[IVXLCDM]{1,7})\s*$")
_ENGEL = "\x00"
# Eklenen metinde Markdown/TeX anlamı olan işaretler Marker gibi kaçışlanır
# (dönüştürücü bunları geri alır: _MD_ESCAPED_SPECIALS, kaçışlı yıldız).
_KACISLANAN = "\\*_$#"


# ---------------------------------------------------------------------------
# Veri
# ---------------------------------------------------------------------------

@dataclass
class _Satir:
    sayfa: int
    blok: int
    metin: str
    norm: str
    kutu: Tuple[float, float, float, float]
    yatay: bool
    punto: float
    kalin: bool
    font: str
    yukseklik: float                 # sayfa yüksekliği
    durum: str = ""                  # var / kismi / yok / kisa / var_coklu
    md: Tuple[int, int] = (-1, -1)   # Markdown norm aralığı
    neden: str = ""                  # dışlanma nedeni


@dataclass
class Ekleme:
    sayfa: int                        # 0 tabanlı PDF sayfası
    satirlar: List[str]               # PDF satırları (özgün metin)
    bicim: str                        # baslik / paragraf / satir_ici
    eklenen: str                      # Markdown'a giren metin
    md_ofset: int = -1                # girdideki ekleme ofseti
    md_satir: int = 0                 # girdideki satır numarası (1 tabanlı)
    once: str = ""                    # ekleme yerinden önceki 40 karakter
    sonra: str = ""


@dataclass
class KurtarmaRaporu:
    pdf_var: bool = False
    eklemeler: List[Ekleme] = field(default_factory=list)
    atlanan: Dict[str, int] = field(default_factory=dict)
    # Eklenmeyen adaylar (sayfa, metin, neden) — doğrulama ve rapor için.
    eklenmeyen: List[Tuple[int, str, str]] = field(default_factory=list)

    def atla(self, neden: str) -> None:
        self.atlanan[neden] = self.atlanan.get(neden, 0) + 1


# ---------------------------------------------------------------------------
# Normalleştirme
# ---------------------------------------------------------------------------

def _satir_normu(metin: str) -> str:
    t = metin.lstrip(" \t​ ")
    while t and t[0] in MADDE_IMLERI:
        t = t[1:].lstrip(" \t​ ")
    return "".join(_pmo._norm_karakter(c) for c in t)


def _md_akisi(md: str) -> Tuple[str, List[int], List[int]]:
    """Markdown -> (norm, başlangıç ofsetleri, bitiş ofsetleri).

    Kod blokları ve etikete benzeyen düz metin METİNDİR; gerçek HTML
    etiketi, kod çiti, tablo ayracı, satır başı işaretleri (liste, başlık,
    alıntı), bağlantı adresi atlanır; matematik ve görsel engeldir."""
    parcalar: List[str] = []
    bas: List[int] = []
    son: List[int] = []
    n = len(md)
    i = 0
    satir_basi = True

    def ekle(ch: str, a: int, b: int) -> None:
        parcalar.append(ch)
        bas.append(a)
        son.append(b)

    while i < n:
        c = md[i]
        if c == "\n":
            satir_basi = True
            i += 1
            continue
        if satir_basi:
            satir_basi = False
            e = md.find("\n", i)
            e = n if e < 0 else e
            satir = md[i:e]
            if satir.lstrip().startswith("```") or _TABLO_AYRAC_RE.match(satir):
                i = e
                continue
            m = _ALINTI_IMI_RE.match(satir)
            if m and m.end() > 0:
                i += m.end()
                satir = md[i:e]
            m = _BASLIK_IMI_RE.match(satir) or _LISTE_IMI_RE.match(satir)
            if m:
                i += m.end()
                continue
        if c == "$" and not (i > 0 and md[i - 1] == "\\"):
            if md.startswith("$$", i):
                kap = md.find("$$", i + 2)
                j = n if kap < 0 else kap + 2
            else:
                kap = i + 1
                while True:
                    kap = md.find("$", kap)
                    if kap < 0 or md[kap - 1] != "\\":
                        break
                    kap += 1
                satir_sonu = md.find("\n", i)
                if kap < 0 or (0 <= satir_sonu < kap):
                    kap = -1
                j = i + 1 if kap < 0 else kap + 1
            if j > i + 1:
                ekle(_ENGEL, i, j)
                i = j
                continue
        if c == "!" and md.startswith("![", i):
            m = _GORSEL_RE.match(md, i)
            if m:
                ekle(_ENGEL, i, m.end())
                i = m.end()
                continue
        if c == "]" and md.startswith("](", i):
            derinlik = 0
            j = i + 1
            while j < n and md[j] != "\n":
                if md[j] == "(":
                    derinlik += 1
                elif md[j] == ")":
                    derinlik -= 1
                    if derinlik == 0:
                        break
                j += 1
            i = j + 1
            continue
        if c == "<":
            m = _GERCEK_ETIKET_RE.match(md, i)
            if m:
                i = m.end()
                continue
        if c == "&":
            m = _VARLIK_RE.match(md, i)
            if m:
                cozum = html.unescape(m.group(0))
                if cozum != m.group(0):
                    for ch in "".join(_pmo._norm_karakter(x) for x in cozum):
                        ekle(ch, i, m.end())
                    i = m.end()
                    continue
        for ch in _pmo._norm_karakter(c):
            ekle(ch, i, i + 1)
        i += 1
    return "".join(parcalar), bas, son


# ---------------------------------------------------------------------------
# PDF satırları
# ---------------------------------------------------------------------------

def _gorunmez(span: dict) -> bool:
    try:
        return int(span.get("alpha", 255)) == 0 or int(span.get("color", 0)) == 0xFFFFFF
    except (TypeError, ValueError):
        return False


def _pdf_satirlari(doc) -> List[_Satir]:
    out: List[_Satir] = []
    for pno, page in enumerate(doc):
        try:
            d = page.get_text("dict")
        except Exception:  # noqa: BLE001 - okunamayan sayfa atlanır
            continue
        yuk = float(page.rect.height) or 1.0
        for bno, blk in enumerate(d.get("blocks", [])):
            if blk.get("type") != 0:
                continue
            for ln in blk.get("lines", []):
                spans = [s for s in ln.get("spans", []) if not _gorunmez(s)]
                metin = "".join(s.get("text", "") for s in spans)
                if not metin.strip():
                    continue
                yazili = [s for s in spans if s.get("text", "").strip()]
                fontlar = Counter()
                for s in yazili:
                    fontlar[(str(s.get("font", "")), round(float(s.get("size", 0)), 1))] += \
                        len(s.get("text", "").strip())
                (font, punto), _ = fontlar.most_common(1)[0]
                kalin = all((int(s.get("flags", 0)) & 16) or "bold" in str(s.get("font", "")).lower()
                            for s in yazili)
                yon = tuple(round(v, 2) for v in ln.get("dir", (1, 0)))
                out.append(_Satir(pno, bno, metin, _satir_normu(metin),
                                  tuple(ln.get("bbox", (0, 0, 0, 0))), yon == (1.0, 0.0),
                                  punto, kalin, font, yuk))
    return out


def _kesisim_alani(a, b) -> float:
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(0.0, min(a[3], b[3]) - max(a[1], b[1]))


def _govde_puntosu(satirlar: Sequence[_Satir]) -> float:
    sayim: Counter = Counter()
    for s in satirlar:
        if s.yatay:
            sayim[s.punto] += len(s.norm)
    return sayim.most_common(1)[0][0] if sayim else 0.0


def _kenar_bantlari(satirlar: Sequence[_Satir]) -> Tuple[float, float]:
    """(üst bandın alt sınırı, alt bandın üst sınırı) — sayfa yüksekliği
    oranı. Sayfanın en üst ya da en alt satırı olan sayfa numaralarından
    (yalnız rakam/Roma rakamı) bulunur; yeterli numara yoksa band yoktur
    (0, 1)."""
    en_ust: Dict[int, _Satir] = {}
    en_alt: Dict[int, _Satir] = {}
    for s in satirlar:
        if not s.yatay:
            continue
        if s.sayfa not in en_ust or s.kutu[1] < en_ust[s.sayfa].kutu[1]:
            en_ust[s.sayfa] = s
        if s.sayfa not in en_alt or s.kutu[3] > en_alt[s.sayfa].kutu[3]:
            en_alt[s.sayfa] = s
    # Yalnız gerçek numara (rakam ya da Roma rakamı), sayfanın kendi yarısında
    # (yalnız üst bilgisi olan boş sayfada en alt satır da üsttedir).
    ustler = sorted(s.kutu[3] / s.yukseklik for s in en_ust.values()
                    if _SAYFA_NO_KESIN_RE.match(s.metin) and s.kutu[3] < 0.5 * s.yukseklik)
    altlar = sorted(s.kutu[1] / s.yukseklik for s in en_alt.values()
                    if _SAYFA_NO_KESIN_RE.match(s.metin) and s.kutu[1] > 0.5 * s.yukseklik)
    ust = alt = None
    if len(ustler) >= KENAR_BANDI_EN_AZ:
        ust = ustler[len(ustler) - 1 - int(KENAR_BANDI_YUZDELIK * len(ustler))] + KENAR_BANDI_PAYI
    if len(altlar) >= KENAR_BANDI_EN_AZ:
        alt = altlar[int(KENAR_BANDI_YUZDELIK * len(altlar))] - KENAR_BANDI_PAYI
    return (ust if ust is not None else 0.0), (alt if alt is not None else 1.0)


def _sayfa_govde_araliklari(satirlar: Sequence[_Satir], govde: float
                            ) -> Dict[int, Tuple[float, float]]:
    """Sayfa -> gövde satırlarının (gövde puntolu, uzun) dikey aralığı."""
    out: Dict[int, Tuple[float, float]] = {}
    for s in satirlar:
        if s.yatay and govde and abs(s.punto - govde) <= GOVDE_PUNTO_PAYI * govde \
                and len(s.norm) >= CAPA_EN_KISA:
            a, b = out.get(s.sayfa, (s.kutu[1], s.kutu[3]))
            out[s.sayfa] = (min(a, s.kutu[1]), max(b, s.kutu[3]))
    return out


def _govde_disinda_kucuk(s: _Satir, aralik: Optional[Tuple[float, float]],
                         govde: float) -> bool:
    """Satır, sayfasındaki bütün gövde satırlarının üstünde ya da altında ve
    gövdeden küçük puntolu mu? (Üst/alt bilgi; aynı ya da büyük puntolu
    başlık sayfa sonunda da olabilir, ona dokunulmaz.)"""
    if aralik is None or not govde or s.punto >= govde * (1 - GOVDE_PUNTO_PAYI):
        return False
    return s.kutu[3] <= aralik[0] + 0.5 or s.kutu[1] >= aralik[1] - 0.5


_SAYI_BELIRTECI_RE = re.compile(r"\d+|\b[ivxlcdm]+\b|\b[IVXLCDM]+\b")


def _maske(s: _Satir) -> str:
    """Sayfa numarası (Arap ya da Roma rakamı) maskelenmiş karşılaştırma
    biçimi ("xiv ◾ Harbour Notes" ile "xv ◾ Harbour Notes" aynı olur)."""
    return _satir_normu(_SAYI_BELIRTECI_RE.sub("#", s.metin))


def _ust_alt_bilgi(satirlar: Sequence[_Satir]) -> set:
    """Sayfanın ilk/son satırlarında, sayıları maskeli aynı metinle en az
    UST_ALT_EN_AZ_SAYFA sayfada geçen satırların indeksleri."""
    sayfa_satir: Dict[int, List[int]] = defaultdict(list)
    for i, s in enumerate(satirlar):
        sayfa_satir[s.sayfa].append(i)
    uclar: Dict[int, List[int]] = {}
    gecis: Dict[str, set] = defaultdict(set)
    for p, idx in sayfa_satir.items():
        u = idx[:SAYFA_UC_SATIR] + idx[-SAYFA_UC_SATIR:]
        uclar[p] = u
        for i in u:
            gecis[_maske(satirlar[i])].add(p)
    out = set()
    for p, u in uclar.items():
        for i in u:
            if len(gecis[_maske(satirlar[i])]) >= UST_ALT_EN_AZ_SAYFA:
                out.add(i)
    return out


# ---------------------------------------------------------------------------
# Eşleme
# ---------------------------------------------------------------------------

def _tek_mi(M: str, w: str) -> bool:
    j = M.find(w)
    return j >= 0 and M.find(w, j + 1) < 0


def _eslestir(satirlar: Sequence[_Satir], M: str) -> None:
    imlec = 0
    for s in satirlar:
        n = s.norm
        if len(n) < ADAY_EN_KISA:
            s.durum = "kisa"
            continue
        lo = max(0, imlec - PENCERE_GERI)
        hi = min(len(M), imlec + PENCERE_ILERI)
        j = M.find(n, lo, hi)
        if j >= 0:
            s.durum, s.md = "var", (j, j + len(n))
            if len(n) >= CAPA_EN_KISA:
                imlec = j + len(n)
            continue
        kismi = False
        y_lo = max(0, imlec - PARCA_YAKIN_GERI)
        y_hi = min(len(M), imlec + PARCA_YAKIN_ILERI)
        for k in range(0, max(1, len(n) - PARCA_PENCERE + 1), PARCA_ADIM_PENCERE):
            parca = n[k:k + PARCA_PENCERE]
            if len(parca) == PARCA_PENCERE and M.find(parca, y_lo, y_hi) >= 0:
                kismi = True
                break
        if kismi:
            s.durum = "kismi"
            continue
        if len(n) >= CAPA_EN_KISA:
            j = M.find(n)
            if j >= 0:
                if M.find(n, j + 1) < 0:
                    s.durum, s.md = "var", (j, j + len(n))
                    imlec = j + len(n)
                else:
                    s.durum = "var_coklu"
                continue
        s.durum = "yok"


def _baska_yerde(n: str, M: str) -> Optional[str]:
    if M.find(n) >= 0:
        return "baska_yerde"
    if len(n) <= PARCA_BELGE_EN_KISA_SATIR:
        return None
    parcalar = [n[k:k + PARCA_BELGE]
                for k in range(0, len(n) - PARCA_BELGE + 1, PARCA_ADIM_BELGE)]
    bulunan = sum(1 for p in parcalar if M.find(p) >= 0)
    if parcalar and bulunan >= PARCA_BELGE_ORANI * len(parcalar):
        return "parca_baska_yerde"
    return None


# ---------------------------------------------------------------------------
# Ekleme metni
# ---------------------------------------------------------------------------

def _kacisla(metin: str) -> str:
    out = []
    for c in metin:
        if c in _KACISLANAN:
            out.append("\\" + c)
        else:
            out.append(c)
    return "".join(out)


def _satirlari_birlestir(satirlar: Sequence[_Satir]) -> str:
    """PDF satırlarını tek paragraf metnine çevir (satır sonu tiresi + küçük
    harfle başlayan sonraki satır birleşir; madde imi korunur)."""
    out = ""
    for s in satirlar:
        t = " ".join(s.metin.replace("​", " ").split())
        if not out:
            out = t
        elif out.endswith("-") and t[:1].islower():
            out = out[:-1] + t
        else:
            out = out + " " + t
    return out


def _baslik_duzeyi(satir: _Satir, satirlar: Sequence[_Satir], basliklar: Dict[str, int]
                   ) -> Optional[int]:
    """Aynı yazı tipi/puntodaki PDF satırlarından Markdown'da başlık olanların
    en yaygın düzeyi."""
    sayim: Counter = Counter()
    for s in satirlar:
        if s is satir or s.font != satir.font or s.kalin != satir.kalin \
                or abs(s.punto - satir.punto) > STIL_PUNTO_TOLERANSI:
            continue
        duzey = basliklar.get(s.norm)
        if duzey:
            sayim[duzey] += 1
    return sayim.most_common(1)[0][0] if sayim else None


def _md_basliklari(md: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for ln in md.split("\n"):
        m = _BASLIK_IMI_RE.match(ln)
        if not m:
            continue
        govde = ln[m.end():]
        norm, _, _ = _md_akisi(govde)
        if norm and _ENGEL not in norm:
            out.setdefault(norm, len(m.group(1)))
    return out


# ---------------------------------------------------------------------------
# Genel giriş
# ---------------------------------------------------------------------------

def onar_raporlu(markdown: str, pdf_path: Optional[str],
                 sekil_bolgeleri: Optional[Dict[int, Sequence[Sequence[float]]]] = None
                 ) -> Tuple[str, KurtarmaRaporu]:
    """``onar`` + rapor."""
    rapor = KurtarmaRaporu()
    if not markdown or not pdf_path:
        return markdown, rapor
    p = Path(str(pdf_path))
    if p.suffix.lower() != ".pdf" or not p.is_file():
        return markdown, rapor
    try:
        import fitz  # PyMuPDF
    except ImportError:  # pragma: no cover - venv'de kurulu
        return markdown, rapor
    try:
        doc = fitz.open(str(p))
    except Exception as exc:  # noqa: BLE001 - bozuk PDF: Markdown aynen
        logger.info("FX2: PDF açılamadı (%s): %s", p, exc)
        return markdown, rapor
    try:
        if getattr(doc, "needs_pass", False):
            return markdown, rapor
        satirlar = _pdf_satirlari(doc)
        gorseller: Dict[int, List[Tuple[float, float, float, float]]] = defaultdict(list)
        for pno, page in enumerate(doc):
            try:
                for info in page.get_image_info():
                    gorseller[pno].append(tuple(info.get("bbox", (0, 0, 0, 0))))
            except Exception:  # noqa: BLE001
                continue
    finally:
        doc.close()
    if sum(len(s.norm) for s in satirlar) < _pmo.EN_AZ_PDF_METNI:
        return markdown, rapor
    rapor.pdf_var = True
    for pno, kutular in (sekil_bolgeleri or {}).items():
        gorseller[int(pno)].extend(tuple(k) for k in kutular)

    M, md_bas, md_son = _md_akisi(markdown)
    _eslestir(satirlar, M)
    govde = _govde_puntosu(satirlar)
    ust_sinir, alt_sinir = _kenar_bantlari(satirlar)
    ust_alt = _ust_alt_bilgi(satirlar)
    sayfa_govde = _sayfa_govde_araliklari(satirlar, govde)

    # Dışlama ve aday belirleme.
    for i, s in enumerate(satirlar):
        if s.durum != "yok":
            continue
        if not s.yatay:
            s.neden = "dikey"
        elif _SAYFA_NO_RE.match(s.metin):
            s.neden = "sayfa_no"
        elif i in ust_alt:
            s.neden = "ust_alt_bilgi"
        elif s.kutu[3] / s.yukseklik <= ust_sinir or s.kutu[1] / s.yukseklik >= alt_sinir:
            s.neden = "sayfa_kenari"
        elif _govde_disinda_kucuk(s, sayfa_govde.get(s.sayfa), govde):
            s.neden = "sayfa_kenari"
        elif any(_kesisim_alani(s.kutu, g) >= 0.5 * max(
                (s.kutu[2] - s.kutu[0]) * (s.kutu[3] - s.kutu[1]), 1e-6)
                for g in gorseller.get(s.sayfa, ())):
            s.neden = "sekil_ici"
        else:
            s.neden = _baska_yerde(s.norm, M) or ""
        if s.neden:
            rapor.atla(s.neden)

    artik = ("sayfa_no", "ust_alt_bilgi", "sayfa_kenari", "dikey", "sekil_ici")
    basliklar = _md_basliklari(markdown)
    eklemeler: List[Tuple[int, int, str, Ekleme, str]] = []   # (norm_konum, sıra, metin, kayıt, eklenen_norm)
    i = 0
    n_sat = len(satirlar)
    while i < n_sat:
        s = satirlar[i]
        if not (s.durum == "yok" and not s.neden):
            i += 1
            continue
        # Öbek: adaylar + araya giren sayfalama artıkları.
        j = i
        while j + 1 < n_sat and (
                (satirlar[j + 1].durum == "yok" and satirlar[j + 1].neden in ("",) + artik)):
            j += 1
        obek = list(range(i, j + 1))
        adaylar = [k for k in obek if not satirlar[k].neden]
        i = j + 1
        # Çapalar: önce/sonra güvenilir satır (Markdown'da bulunan uzun
        # satır). Aradakiler yalnız kısa satır (Markdown'da olabilir) ya da
        # sayfalama artığı (Markdown'da yok) olabilir.
        P, ara_once = _capa(satirlar, obek[0] - 1, -1, artik)
        N, ara_sonra = _capa(satirlar, obek[-1] + 1, +1, artik)
        metinler = [satirlar[k].metin for k in adaylar]
        if P is None or N is None:
            for k in adaylar:
                rapor.eklenmeyen.append((satirlar[k].sayfa, satirlar[k].metin, "capa_yok"))
            rapor.atla("capa_yok")
            continue
        sp, sn = satirlar[P], satirlar[N]
        if not (_tek_mi(M, sp.norm) and _tek_mi(M, sn.norm)):
            for k in adaylar:
                rapor.eklenmeyen.append((satirlar[k].sayfa, satirlar[k].metin, "capa_tek_degil"))
            rapor.atla("capa_tek_degil")
            continue
        arada = M[sp.md[1]:sn.md[0]] if sp.md[1] <= sn.md[0] else None
        # Aradaki bulunmuş kısa satırlar da çapaların arasında olmalı.
        ara_tutarli = arada is not None and all(
            sp.md[1] <= satirlar[k].md[0] and satirlar[k].md[1] <= sn.md[0]
            for k in ara_once + ara_sonra
            if satirlar[k].durum == "var" and satirlar[k].md[0] >= 0)
        konum = _ara_konumu(arada, [satirlar[k] for k in reversed(ara_once)],
                            [satirlar[k] for k in ara_sonra]) if ara_tutarli else None
        if konum is not None:
            konum += sp.md[1]
            if _yakinda_parca(M, konum, [satirlar[k].norm for k in adaylar]):
                for k in adaylar:
                    rapor.eklenmeyen.append((satirlar[k].sayfa, satirlar[k].metin, "yakinda_parca"))
                rapor.atla("yakinda_parca")
                continue
        else:
            for k in adaylar:
                rapor.eklenmeyen.append((satirlar[k].sayfa, satirlar[k].metin, "bitisik_degil"))
            rapor.atla("bitisik_degil")
            continue
        if konum <= 0 or konum >= len(M):
            rapor.atla("konum_yok")
            continue
        a, b = md_son[konum - 1], md_bas[konum]
        bosluk = markdown[a:b]
        aday_sat = [satirlar[k] for k in adaylar]
        gruplar: List[List[_Satir]] = []
        for st in aday_sat:
            if gruplar and (gruplar[-1][-1].sayfa, gruplar[-1][-1].blok) == (st.sayfa, st.blok):
                gruplar[-1].append(st)
            else:
                gruplar.append([st])
        p_blok = (sp.sayfa, sp.blok)
        n_blok = (sn.sayfa, sn.blok)
        ilk_blok = (gruplar[0][0].sayfa, gruplar[0][0].blok)
        son_blok = (gruplar[-1][-1].sayfa, gruplar[-1][-1].blok)
        bosluk_var = any(ch.isspace() for ch in bosluk)
        if len(gruplar) == 1 and ilk_blok == p_blok and bosluk_var:
            # Önceki çapanın paragrafının devamı (ör. paragrafın son satırı
            # düşmüş): paragrafın sonuna, ilk boşluk karakterinden önce.
            bicim = "satir_ici"
            k = next(x for x, ch in enumerate(bosluk) if ch.isspace())
            ofset = a + k
            eklenen = " " + _kacisla(_satirlari_birlestir(aday_sat))
        elif len(gruplar) == 1 and son_blok == n_blok and bosluk_var:
            # Sonraki çapanın paragrafının başı: boşluktaki son boşluk
            # karakterinden sonra (satır başı işaretlerinden sonra, açılan
            # vurgu işaretinden önce).
            bicim = "satir_ici"
            k = max(x for x, ch in enumerate(bosluk) if ch.isspace())
            ofset = a + k + 1
            eklenen = _kacisla(_satirlari_birlestir(aday_sat)) + " "
        elif "\n\n" in bosluk and ilk_blok != p_blok and son_blok != n_blok:
            # Ayrı blok(lar): paragraf ya da başlık olarak paragraf sınırına.
            paragraflar = []
            bicim = "paragraf"
            for g in gruplar:
                metin = _satirlari_birlestir(g)
                tek = len(g) == 1 and len(metin) <= BASLIK_EN_UZUN
                duzey = _baslik_duzeyi(g[0], satirlar, basliklar) if tek else None
                if duzey:
                    # Aynı yazı tipi/punto/kalınlıktaki satırlar Markdown'da
                    # bu düzeyde başlık: kayıp satır da başlıktır.
                    paragraflar.append("#" * duzey + " " + _kacisla(metin))
                    bicim = "baslik"
                    continue
                if tek and g[0].kalin and not (govde and g[0].punto < govde * (1 - GOVDE_PUNTO_PAYI)):
                    paragraflar.append("**" + _kacisla(metin) + "**")
                    continue
                paragraflar.append(_kacisla(metin))
            k = bosluk.index("\n\n")
            ofset = a + k
            eklenen = "\n\n" + "\n\n".join(paragraflar)
        else:
            for st in aday_sat:
                rapor.eklenmeyen.append((st.sayfa, st.metin, "yer_belirsiz"))
            rapor.atla("yer_belirsiz")
            continue
        kayit = Ekleme(aday_sat[0].sayfa, metinler, bicim, eklenen, ofset,
                       markdown.count("\n", 0, ofset) + 1,
                       markdown[max(0, ofset - 40):ofset], markdown[ofset:ofset + 40])
        eklemeler.append((ofset, len(eklemeler), eklenen, kayit,
                          "".join(st.norm for st in aday_sat)))

    if not eklemeler:
        return markdown, rapor
    # Sondan başa uygula.
    md = markdown
    for ofset, _sira, eklenen, _kayit, _n in sorted(eklemeler, key=lambda e: (e[0], e[1]), reverse=True):
        md = md[:ofset] + eklenen + md[ofset:]
    # Güvenlik ağı: çıktının normu = girdinin normu + eklenenlerin PDF normu.
    beklenen = M
    for ofset, _sira, _eklenen, _kayit, n_ek in sorted(eklemeler, key=lambda e: (e[0], e[1]), reverse=True):
        # ofset, M'de hangi konuma düşüyor: ofsetten önce biten karakter sayısı.
        konum = _konum(md_son, ofset)
        beklenen = beklenen[:konum] + n_ek + beklenen[konum:]
    cikti_norm, _, _ = _md_akisi(md)
    if cikti_norm != beklenen:
        fark = next((x for x, (u, v) in enumerate(zip(cikti_norm, beklenen)) if u != v),
                    min(len(cikti_norm), len(beklenen)))
        logger.warning("FX2: kayıp metin güvenlik ağı tutmadı; %d ekleme geri alındı "
                       "(ilk fark %d: %r / %r)", len(eklemeler), fark,
                       cikti_norm[max(0, fark - 30):fark + 30],
                       beklenen[max(0, fark - 30):fark + 30])
        rapor.atla("guvenlik_agi")
        return markdown, rapor
    rapor.eklemeler = [e[3] for e in sorted(eklemeler, key=lambda e: (e[0], e[1]))]
    logger.info("FX2: PDF metin katmanından %d kayıp parça Markdown'a eklendi",
                len(rapor.eklemeler))
    return md, rapor


def _capa(satirlar: Sequence[_Satir], k: int, yon: int, artik: Sequence[str]
          ) -> Tuple[Optional[int], List[int]]:
    """k'dan yon yönünde ilk güvenilir satır ve aradaki satırlar.

    Aradaki satır kısa (Markdown'da bulunsun bulunmasın) ya da sayfalama
    artığı olmalı; başka bir satıra (kısmi, çok geçen, başka yerde bulunan)
    rastlanırsa çapa yoktur. En çok OBEK_EN_UZUN satır geriye/ileriye bakılır."""
    ara: List[int] = []
    adim = 0
    while 0 <= k < len(satirlar) and adim <= OBEK_EN_UZUN:
        s = satirlar[k]
        if s.durum == "var" and len(s.norm) >= CAPA_EN_KISA:
            return k, ara
        if s.durum == "kisa" or (s.durum == "var" and len(s.norm) < CAPA_EN_KISA) \
                or (s.durum == "yok" and s.neden in artik):
            ara.append(k)
            k += yon
            adim += 1
            continue
        return None, ara
    return None, ara


def _yakinda_parca(M: str, konum: int, normlar: Sequence[str]) -> bool:
    """Adayın büyük kısmı ekleme yerinin yakınında zaten var mı?

    Yakın bölgede bulunan 12 karakterlik pencerelerin örttüğü karakterler
    sayılır; bir adayın en az YAKIN_PARCA_ORTUSU kadarı örtülüyorsa içerik
    Markdown'da başka biçimde (yer değiştirmiş dipnot işareti, farklı
    bölünmüş hücre) duruyordur: eklenmez. Başlığın, hemen önceki cümledeki
    bir ifadeyi yinelemesi ("… from the old harbour") bu oranın altında kalır."""
    bolge = M[max(0, konum - YAKIN_PARCA_PAYI):konum + YAKIN_PARCA_PAYI]
    for n in normlar:
        if len(n) < PARCA_PENCERE:
            continue
        ortulu = [False] * len(n)
        for k in range(0, len(n) - PARCA_PENCERE + 1):
            if n[k:k + PARCA_PENCERE] in bolge:
                for t in range(k, k + PARCA_PENCERE):
                    ortulu[t] = True
        if sum(ortulu) >= YAKIN_PARCA_ORTUSU * len(n):
            return True
    return False


def _ara_konumu(arada: Optional[str], once: Sequence[_Satir], sonra: Sequence[_Satir]
                ) -> Optional[int]:
    """Çapalar arasındaki Markdown dizgisi ``arada``, aradaki kısa/artık
    satırların (PDF sırasıyla) bir alt dizisinin birleşimi mi? Öyleyse
    ekleme konumu (``arada`` içinde, öbekten önceki satırların sonu); değilse
    None. Kısa satır Markdown'da bulunmayabilir (sayfa numarası "86"), tablo
    hücresi "1" ise bulunur; açgözlü eşleme yeter (aradaki satır sayısı az)."""
    if arada is None or _ENGEL in arada:
        return None
    i = 0
    for s in once:
        if s.norm and arada.startswith(s.norm, i):
            i += len(s.norm)
    konum = i
    for s in sonra:
        if s.norm and arada.startswith(s.norm, i):
            i += len(s.norm)
    return konum if i == len(arada) else None


def _konum(md_son: Sequence[int], ofset: int) -> int:
    """Ham ofsetten önce biten norm karakteri sayısı (ikili arama)."""
    lo, hi = 0, len(md_son)
    while lo < hi:
        mid = (lo + hi) // 2
        if md_son[mid] <= ofset:
            lo = mid + 1
        else:
            hi = mid
    return lo


def onar(markdown: str, pdf_path: Optional[str],
         sekil_bolgeleri: Optional[Dict[int, Sequence[Sequence[float]]]] = None) -> str:
    """Kaynak PDF'te olup Markdown'da bulunmayan satırları geri koy.

    PDF yoksa / açılamıyorsa / metinsizse ya da hata olursa Markdown aynen
    döner (onarım isteğe bağlı bir iyileştirmedir; boru hattını durdurmaz).
    """
    try:
        return onar_raporlu(markdown, pdf_path, sekil_bolgeleri)[0]
    except Exception as exc:  # noqa: BLE001 - onarım boru hattını durdurmaz
        logger.warning("FX2: kayıp metin kurtarma atlandı (%s): %s",
                       type(exc).__name__, exc, exc_info=True)
        return markdown
