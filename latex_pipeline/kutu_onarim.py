# -*- coding: utf-8 -*-
"""Kaynak PDF'teki çerçeveli / zeminli kutuların geri getirilmesi.

6 Ekim 2026. Marker kutuları düşürür: bir gök mekaniği kitabında Kepler ve
Newton yasaları kaynakta çerçeveli kutudadır, sentetik
Almanca ısı aktarımı belgesinde "Exkurs 1.1" gri zeminli çerçevededir; ikisi
de Türkçede sıradan paragraf olarak basılıyordu. Bu modül kaynak PDF'in çizim
katmanından (``page.get_drawings()``) kutu dikdörtgenlerini bulur, içine TAM
düşen metin satırlarını Markdown bloklarıyla eşler ve eşleşen bloklar
BÜTÜNÜYLE kutudaysa onları ayrı satırlardaki yapı işaretleriyle sarar::

    <!-- kc:kutu-bas -->

    …bloklar…

    <!-- kc:kutu-son -->

Dönüştürücü işaretleri satırlar işlenmeden önce ayıklar
(:func:`isaretleri_ayikla`; yapı planı işaretleri hiç görmez) ve kutuyu
``\\begin{kckutu}`` … ``\\end{kckutu}`` olarak basar (ortam şablonda).

Temkin ("emin değilsen dokunma"); aday dikdörtgen şu durumlarda kutu sayılmaz:

- sayfa kenarına değiyor (taşma, çift sayfa yayılımı), sayfanın büyük
  kısmını kaplıyor (sayfa kenarlığı) ya da aynı dikdörtgen birçok sayfada
  yineleniyor (üst/alt bilgi çubuğu, metin alanı çerçevesi);
- başka bir adaya bitişik (renkli tablo satırları/hücreleri) ya da iç içe;
- içinde görsel ya da (kendi çerçeve/zemin katmanları dışında) birkaç
  çizimden fazlası var (şekil çerçevesi, grafik, tablo çizgileri);
- kenarı bir metin satırını kesiyor;
- içindeki metin Markdown'da tek bir yerde, blok sınırından başlayıp blok
  sınırında biten ardışık bloklarla eşleşmiyor (paragraf ortasından
  bölünmez), bloklar arasında tablo / görsel / kod bloğu var, ilk blok bir
  önceki cümlenin devamı gibi başlıyor ya da son blok cümle ortasında
  bitiyor (sayfa sınırında bölünen kutunun yarısı tek başına sarılmaz).

Sayfa sınırında bölünmüş kutu (sayfa N'nin altında, N+1'in üstünde aynı
genişlikte iki parça; arada yalnız "(continued)" gibi kısa artık) tek kutu
olarak birleştirilir.

Tek çizgiler (dipnot ayırıcısı, booktabs kuralları) kutu oluşturmaz: kutu ya
dolu/çizgili kapalı bir dikdörtgendir ya da dört kenarı çizgi parçalarından
(LaTeX ``framed``) eksiksiz kurulur.

Metin değişmez: yalnız işaret satırları eklenir (kayıpsızlık). PDF yoksa,
açılamıyorsa ya da kutu bulunamazsa Markdown AYNEN döner.

Sözleşme: ``onar_raporlu(markdown, pdf_yolu) -> (markdown, KutuRaporu)``.
Dönüştürücüde ``vurgu_onarim``'dan sonra, ``_wrap_pdf_monospace``'ten önce,
yalnız kaynak PDF varken çağrılır.
"""
from __future__ import annotations

import bisect
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# NOT: marker_onarim (cümle sonu / devam ölçütleri) işlev içinde yüklenir:
# sanitizer, çevirmen ve QC yalnız ORTAM adını almak için bu modülü içe
# aktarır; import maliyetleri artmasın.

# Yapı işaretleri (ayrı satırlarda, önünde/ardında boş satır). Dönüştürücü
# bunları isaretleri_ayikla ile çıkarır; hiçbir çıktıya metin olarak geçmez.
KUTU_BAS = "<!-- kc:kutu-bas -->"
KUTU_SON = "<!-- kc:kutu-son -->"
# Şablondaki LaTeX ortamının adı (latex_pipeline/templates/turkish_book.tex).
ORTAM = "kckutu"

# --- Çizim sınıflandırması ---------------------------------------------------
# Çizgi (kural) sayılan çizimin ince kenarı en çok bu kadar (pt). \fbox /
# framed / tcolorbox kenarları 0,4–1,4 pt; dolu zemin bundan çok kalındır.
CIZGI_KALINLIK_AZAMI = 2.0
# Çizgi sayılan çizimin uzun kenarı en az bu kadar (pt); daha kısası nokta,
# ok ucu, imdir.
CIZGI_EN_AZ_UZUNLUK = 4.0
# Kenar parçalarının eşlenme toleransı (pt): framed çerçeveyi sayfa akışına
# göre parça parça çizer, parçalar 0,1–0,5 pt kayık ya da üst üstedir.
KENAR_TOLERANSI = 2.0
# Bu açıklığa (bileşen başına, 0–1) kadar beyaz/beyaza yakın dolgu ya da
# çizgi görünmez sayılır (şekil arka planı). tcolorbox'un "black!5" zemini
# (0,95) görünür kalır.
BEYAZ_ESIGI = 0.98

# --- Aday dikdörtgen süzgeçleri ----------------------------------------------
# Kutunun en az yüksekliği (pt): iki satır (10–11 pt punto, ~12 pt satır
# aralığı); tek satırlık çerçeve çoğunlukla denklem ya da etikettir.
KUTU_EN_AZ_YUKSEKLIK = 18.0
# Kutunun en az genişliği, sayfa genişliğine oranla: dar çerçeveler (şekil
# içi etiket, sayfa kenarı sekmesi) kutu değildir.
KUTU_EN_AZ_GENISLIK_ORANI = 0.3
# Sayfa kenarına bu kadar (pt) yakın dikdörtgen taşma, çift sayfa yayılımı
# ya da sayfa kenarlığıdır.
SAYFA_KENAR_PAYI = 2.0
# Sayfa alanının bu oranından büyük dikdörtgen sayfa çerçevesidir.
SAYFA_ALAN_ORANI_AZAMI = 0.6
# Yüksekliği sayfa yüksekliğinin bu oranını aşan dikdörtgen metin alanı
# çerçevesidir (geniş kenar boşluklu sayfada alan oranı süzgecinden kaçar).
# Gerçek uzun kutu çeviride sayfaya sığmazsa şablon onu bölünebilir basar.
KUTU_YUKSEKLIK_ORANI_AZAMI = 0.75
# Aynı dikdörtgen (KENAR_TOLERANSI içinde) bu kadar ya da daha çok sayfada
# varsa sayfa süsüdür (üst/alt bilgi çubuğu, metin alanı çerçevesi).
YINELENEN_SAYFA_ESIGI = 3
# İç içe iki adaydan içteki, dört kenarından en az üçü dıştakinin kenarına
# en çok bu kadar (pt) yakınsa aynı kutunun katmanıdır (tcolorbox: koyu dış
# dolgu + açık iç dolgu 0,6 pt içeride; başlık şeridi üst/sol/sağ kenarda;
# zemin + çizgi).
KATMAN_PAYI = 4.0
# Kutunun içinde (kendi çerçeve/zemin katmanları dışında) en çok bu kadar
# başka çizim olabilir (başlık ayırıcı çizgi, alt çizgi); fazlası şekil,
# grafik ya da tablodur.
IC_CIZIM_AZAMI = 2
# Kutuya taşan ya da içindeki görsel kutuyu geçersiz kılar (şekil çerçevesi);
# yalnız alanı kutunun bu oranından küçük görsel (kutu simgesi, madde imi
# resmi) yok sayılır.
SIMGE_ALAN_ORANI = 0.03
# Çok sayıda çizimli sayfa (harita, grafik) taranmaz; çizgiden çerçeve
# kurma da çizgisi çok sayfada atlanır (tablo/grafik sayfası).
SAYFA_CIZIM_AZAMI = 4000
SAYFA_CIZGI_AZAMI = 400

# --- Metin eşleme ------------------------------------------------------------
# Satır alanının en az bu oranı içerideyse satır kutudadır; en çok
# DISARIDA_ORANI'ysa dışındadır; arası belirsizdir (kenar satırı kesiyor) ve
# aday atılır.
ICERIDE_ORANI = 0.9
DISARIDA_ORANI = 0.35
# Kutudaki normalleştirilmiş harf/rakam sayısı en az bu kadar olmalı.
KUTU_EN_AZ_HARF = 40
# Uç çapası uzunluğu (normalleştirilmiş karakter) ve uçtan içe kaydırılarak
# en çok kaç çapa denenir (uçta Marker'ın farklı yazdığı matematik ya da
# metinde sık yinelenen kalıp olabilir).
CAPA_UZUNLUGU = 24
CAPA_DENEME = 4
# Bir çapanın Markdown'da en çok bu kadar geçişi incelenir; daha çoksa çapa
# yinelenen kalıptır, bir içteki çapa denenir.
CAPA_GECIS_AZAMI = 20
# Eşleşme bölgesinin uçları blok sınırına en çok bu kadar karakter uzak
# olabilir (dipnot işareti, madde imi, matematik yazım farkı).
SINIR_TOLERANSI = 6
# Kutu metni ile eşleşen blokların normalleştirilmiş metni arasındaki
# benzerlik (difflib oranı) en az bu kadar olmalı.
BENZERLIK_ESIGI = 0.9
# Bölge uzunluğu kutu metninden en çok bu oranda sapabilir.
UZUNLUK_SAPMA_ORANI = 0.12

# --- Sayfa sınırında bölünen kutu ---------------------------------------------
# İki parçanın sol/sağ kenarı en çok bu kadar (pt) farklı olabilir.
ZINCIR_X_TOLERANSI = 4.0
# Parçaların blokları arasında en çok bu kadar kısa artık blok olabilir
# ("(continued)", sızmış sayfa numarası / üst bilgi): tek satır, en çok
# ARA_BLOK_EN_UZUN karakter.
ARA_BLOK_AZAMI = 3
ARA_BLOK_EN_UZUN = 100

_CIT_RE = re.compile(r"^\s*(```|~~~)")
_GORSEL_RE = re.compile(r"^\s*!\[[^\]\n]*\]\([^)\n]*\)")
_TABLO_RE = re.compile(r"^\s*\|")
_HTML_BLOK_RE = re.compile(r"^\s*<(?:table|tr|td|th|img|figure|div)\b", re.I)
_BASLIK_RE = re.compile(r"^\s*#{1,6}\s")
_LISTE_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")
_YORUM_RE = re.compile(r"^\s*<!--")
_KOMUT_RE = re.compile(r"\\[A-Za-z]+")
_ETIKET_RE = re.compile(r"<[^<>\n]{1,200}>")
_BAG_HEDEF_RE = re.compile(r"\]\([^)\s]*\)")
_GORSEL_SATIR_ICI_RE = re.compile(r"!\[[^\]\n]*\]\([^)\n]*\)")

# Kutuya giremeyen blok türleri (şablon kutusu bölünemez \vbox'tır: kayan
# nesne, uzun tablo, kod bloğu içinde dizilemez).
_YASAK_TURLER = frozenset({"kod", "tablo", "gorsel", "html", "isaret"})
# Bu nedenle eşlenemeyen sayfa parçası, komşu sayfa parçasıyla birlikte
# yeniden denenir (Marker iki sayfaya bölünen paragrafı tek blok yazar).
_BIRLESIK_DENE = frozenset({"blok_siniri_yok", "md_bulunamadi"})


@dataclass
class KutuRaporu:
    """Onarım özeti: sayaçlar + kabul edilen kutular (sayfa, satır aralığı)."""

    sayac: Dict[str, int] = field(default_factory=dict)
    kutular: List[Dict[str, object]] = field(default_factory=list)
    atlananlar: List[Dict[str, object]] = field(default_factory=list)

    def say(self, anahtar: str, n: int = 1) -> None:
        self.sayac[anahtar] = self.sayac.get(anahtar, 0) + n

    def atla(self, neden: str, sayfa: int, rect, ornek: str = "") -> None:
        self.say("ret_" + neden)
        self.atlananlar.append({"neden": neden, "sayfa": sayfa,
                                "rect": tuple(round(v, 1) for v in rect),
                                "ornek": ornek[:80]})


# ---------------------------------------------------------------------------
# Normalleştirme
# ---------------------------------------------------------------------------

def norm(metin: str) -> str:
    """Eşleme anahtarı: NFKC + casefold, yalnız harf/rakam (bitişik).

    PDF ve Markdown tarafına aynı uygulanır; satır sonu tirelemesi, bağlaç
    (ﬁ), tırnak, vurgu işaretleri ve boşluk farkı eşlemeyi bozmaz."""
    t = unicodedata.normalize("NFKC", metin).casefold()
    return "".join(c for c in t if c.isalnum())


def _md_norm(metin: str) -> str:
    """Markdown bloğunun eşleme anahtarı: görsel, bağlantı hedefi, HTML
    etiketi ve LaTeX komut adları atılır (komut argümanları kalır:
    ``$\\vec{F}$`` -> "F", PDF'te "⃗F" -> "F")."""
    t = _GORSEL_SATIR_ICI_RE.sub(" ", metin)
    t = _BAG_HEDEF_RE.sub("]", t)
    t = _ETIKET_RE.sub(" ", t)
    t = _KOMUT_RE.sub(" ", t)
    return norm(t)


# ---------------------------------------------------------------------------
# Markdown blokları
# ---------------------------------------------------------------------------

@dataclass
class _Blok:
    bas: int          # ilk satır (dahil)
    son: int          # son satır (dahil)
    tur: str          # paragraf, baslik, liste, denklem, kod, tablo, gorsel, html, isaret
    anahtar: str      # _md_norm


def _blok_turu(satirlar: Sequence[str]) -> str:
    ilk = satirlar[0]
    if _CIT_RE.match(ilk):
        return "kod"
    if any(_TABLO_RE.match(s) for s in satirlar):
        return "tablo"
    if any(_GORSEL_RE.match(s) for s in satirlar):
        return "gorsel"
    if any(_HTML_BLOK_RE.match(s) for s in satirlar):
        return "html"
    if _YORUM_RE.match(ilk):
        return "isaret"
    if ilk.strip().startswith("$$"):
        return "denklem"
    if _BASLIK_RE.match(ilk):
        return "baslik"
    if _LISTE_RE.match(ilk):
        return "liste"
    return "paragraf"


def bloklar(satirlar: Sequence[str]) -> List[_Blok]:
    """Markdown'ı boş satırla ayrılmış bloklara böl.

    Kod çiti (```) ve çok satırlı ``$$`` denklemi içindeki boş satır blok
    sınırı DEĞİLDİR: işaret ancak blokların arasına girer, hiçbir zaman kod
    bloğunun ya da denklemin içine düşmez."""
    out: List[_Blok] = []
    n = len(satirlar)
    i = 0
    while i < n:
        if not satirlar[i].strip():
            i += 1
            continue
        j = i
        s = satirlar[i]
        cit = _CIT_RE.match(s)
        if cit:
            j = i + 1
            while j < n and not satirlar[j].strip().startswith(cit.group(1)):
                j += 1
            j = min(j, n - 1)
        elif s.strip().startswith("$$") and s.strip().count("$$") == 1:
            j = i + 1
            while j < n and "$$" not in satirlar[j]:
                j += 1
            if j >= n:          # kapanmayan denklem: tek satır say
                j = i
        else:
            while j + 1 < n and satirlar[j + 1].strip() \
                    and not _CIT_RE.match(satirlar[j + 1]):
                j += 1
        parca = satirlar[i:j + 1]
        out.append(_Blok(i, j, _blok_turu(parca), _md_norm("\n".join(parca))))
        i = j + 1
    return out


# ---------------------------------------------------------------------------
# PDF çizimlerinden aday dikdörtgenler
# ---------------------------------------------------------------------------

Rect = Tuple[float, float, float, float]


def _beyaz(renk) -> bool:
    return renk is None or all(c >= BEYAZ_ESIGI for c in renk)


def _uc_noktalar(items) -> List[Tuple[float, float]]:
    pts: List[Tuple[float, float]] = []
    for it in items:
        if it[0] == "l":
            pts += [(it[1].x, it[1].y), (it[2].x, it[2].y)]
        elif it[0] == "c":
            pts += [(it[1].x, it[1].y), (it[4].x, it[4].y)]
    return pts


def _dikdortgen_yolu_mu(items, r: Rect) -> bool:
    """l/c ögelerinden oluşan yol r'nin dört kenarı üzerinde kapalı bir
    (köşeleri yuvarlatılmış olabilir) dikdörtgen mi? Her uç nokta bir kenarın
    üzerindedir ve dört kenarın her birinin en az yarısı düz çizgidir (daire,
    elips, çokgen elenir)."""
    x0, y0, x1, y1 = r
    tol = KENAR_TOLERANSI
    for px, py in _uc_noktalar(items):
        if min(abs(px - x0), abs(px - x1), abs(py - y0), abs(py - y1)) > tol:
            return False
    kenar = {"ust": 0.0, "alt": 0.0, "sol": 0.0, "sag": 0.0}
    for it in items:
        if it[0] != "l":
            continue
        a, b = it[1], it[2]
        if abs(a.y - b.y) <= tol:
            if abs(a.y - y0) <= tol:
                kenar["ust"] += abs(a.x - b.x)
            elif abs(a.y - y1) <= tol:
                kenar["alt"] += abs(a.x - b.x)
        elif abs(a.x - b.x) <= tol:
            if abs(a.x - x0) <= tol:
                kenar["sol"] += abs(a.y - b.y)
            elif abs(a.x - x1) <= tol:
                kenar["sag"] += abs(a.y - b.y)
    w, h = x1 - x0, y1 - y0
    return (kenar["ust"] >= 0.5 * w and kenar["alt"] >= 0.5 * w
            and kenar["sol"] >= 0.5 * h and kenar["sag"] >= 0.5 * h)


@dataclass
class _Ilkel:
    rect: Rect
    tur: str          # "yatay", "dikey", "alan", "diger"


def _ilkeller(cizimler) -> List[_Ilkel]:
    """Görünür çizim yollarını çizgi / alan / diğer ilkellerine ayır."""
    out: List[_Ilkel] = []
    for d in cizimler:
        tip = d.get("type") or ""
        dolgu = d.get("fill") if "f" in tip else None
        cizgi = d.get("color") if "s" in tip else None
        opak = d.get("fill_opacity")
        if dolgu is not None and opak is not None and opak < 0.1:
            dolgu = None
        if _beyaz(dolgu) and _beyaz(cizgi):
            continue
        items = d.get("items") or []
        r = d["rect"]
        rr: Rect = (r.x0, r.y0, r.x1, r.y1)
        turler = {it[0] for it in items}
        parcalar: List[Rect] = []
        if turler and turler <= {"re", "qu"}:
            for it in items:
                q = it[1].rect if it[0] == "qu" else it[1]
                parcalar.append((q.x0, q.y0, q.x1, q.y1))
        elif turler == {"l"} and len(items) > 1 \
                and not _dikdortgen_yolu_mu(items, rr):
            # Ayrı çizgi parçalarından oluşan yol (tablo çizgileri tek yolda).
            for it in items:
                a, b = it[1], it[2]
                parcalar.append((min(a.x, b.x), min(a.y, b.y),
                                 max(a.x, b.x), max(a.y, b.y)))
        else:
            if min(rr[2] - rr[0], rr[3] - rr[1]) > CIZGI_KALINLIK_AZAMI \
                    and not _dikdortgen_yolu_mu(items, rr):
                out.append(_Ilkel(rr, "diger"))     # eğri, çokgen, grafik
                continue
            parcalar.append(rr)
        for p in parcalar:
            w, h = p[2] - p[0], p[3] - p[1]
            if min(w, h) <= CIZGI_KALINLIK_AZAMI:
                if max(w, h) >= CIZGI_EN_AZ_UZUNLUK:
                    out.append(_Ilkel(p, "yatay" if w >= h else "dikey"))
                else:
                    out.append(_Ilkel(p, "diger"))
            else:
                out.append(_Ilkel(p, "alan"))
    return out


def _araliklar_kaplar(araliklar: List[Tuple[float, float]], a: float, b: float) -> bool:
    """[a, b] aralığı (KENAR_TOLERANSI boşluklarla) birleşik aralıklarca
    kaplanıyor mu?"""
    tol = KENAR_TOLERANSI
    konum = a + tol
    for s, e in sorted(araliklar):
        if s > konum + tol:
            break
        konum = max(konum, e)
    return konum >= b - tol


def _cizgilerden_cerceveler(ilkeller: List[_Ilkel]) -> List[Rect]:
    """Dört kenarı çizgi parçalarından kurulan çerçeveler (LaTeX framed,
    \\fbox'un parça parça çizimi)."""
    tol = KENAR_TOLERANSI
    yataylar = sorted((p.rect for p in ilkeller if p.tur == "yatay"),
                      key=lambda r: (r[1], r[0]))
    dikeyler = [p.rect for p in ilkeller if p.tur == "dikey"]
    if len(yataylar) < 2 or len(dikeyler) < 2 or \
            len(yataylar) + len(dikeyler) > SAYFA_CIZGI_AZAMI:
        return []
    out: List[Rect] = []
    for i, a in enumerate(yataylar):
        for b in yataylar[i + 1:]:
            if abs(a[0] - b[0]) > tol or abs(a[2] - b[2]) > tol:
                continue
            if b[1] - a[3] < KUTU_EN_AZ_YUKSEKLIK - 2 * tol:
                continue
            ust, alt = (a[1] + a[3]) / 2, (b[1] + b[3]) / 2
            x0, x1 = min(a[0], b[0]), max(a[2], b[2])
            sol = [(d[1], d[3]) for d in dikeyler if abs((d[0] + d[2]) / 2 - x0) <= tol]
            sag = [(d[1], d[3]) for d in dikeyler if abs((d[0] + d[2]) / 2 - x1) <= tol]
            if sol and sag and _araliklar_kaplar(sol, ust, alt) \
                    and _araliklar_kaplar(sag, ust, alt):
                out.append((x0, min(a[1], b[1]), x1, max(a[3], b[3])))
                break           # a için en yakın alt kenar
    return out


def _icinde(ic: Rect, dis: Rect, pay: float) -> bool:
    return (ic[0] >= dis[0] - pay and ic[1] >= dis[1] - pay
            and ic[2] <= dis[2] + pay and ic[3] <= dis[3] + pay)


def _katman_mi(ic: Rect, dis: Rect) -> bool:
    """ic dis'in içinde ve dört kenarından en az üçü dis'in kenarına en çok
    KATMAN_PAYI uzak mı (aynı kutunun zemini, çerçevesi, başlık şeridi)?"""
    if not _icinde(ic, dis, 0.5):
        return False
    yakin = sum(1 for f in (ic[0] - dis[0], ic[1] - dis[1],
                            dis[2] - ic[2], dis[3] - ic[3]) if f <= KATMAN_PAYI)
    return yakin >= 3


def _bitisik_mi(a: Rect, b: Rect) -> bool:
    """İki dikdörtgen kenar kenara mı (renkli tablo satırı/hücresi)?"""
    tol = KENAR_TOLERANSI
    x_ortak = min(a[2], b[2]) - max(a[0], b[0])
    y_ortak = min(a[3], b[3]) - max(a[1], b[1])
    if x_ortak > 0.5 * min(a[2] - a[0], b[2] - b[0]) and \
            (abs(a[3] - b[1]) <= tol or abs(b[3] - a[1]) <= tol):
        return True
    return y_ortak > 0.5 * min(a[3] - a[1], b[3] - b[1]) and \
        (abs(a[2] - b[0]) <= tol or abs(b[2] - a[0]) <= tol)


def _alan(r: Rect) -> float:
    return max(0.0, r[2] - r[0]) * max(0.0, r[3] - r[1])


def _kesisim(a: Rect, b: Rect) -> float:
    return _alan((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))


def _ayni_rect(a: Rect, b: Rect, tol: float = KENAR_TOLERANSI) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b))


@dataclass
class _Aday:
    sayfa: int        # 0 tabanlı
    rect: Rect
    katmanlar: List[Rect]


def sayfa_adaylari(sayfa_no: int, cizimler, genislik: float,
                   yukseklik: float) -> Tuple[List[_Aday], List[_Ilkel], List[Tuple[str, Rect]]]:
    """Bir sayfanın aday kutuları, bütün ilkelleri ve geometri retleri."""
    ilk = _ilkeller(cizimler)
    hamlar: List[Rect] = [p.rect for p in ilk if p.tur == "alan"]
    hamlar += _cizgilerden_cerceveler(ilk)
    retler: List[Tuple[str, Rect]] = []
    uygun: List[Rect] = []
    for r in hamlar:
        w, h = r[2] - r[0], r[3] - r[1]
        if h < KUTU_EN_AZ_YUKSEKLIK or w < KUTU_EN_AZ_GENISLIK_ORANI * genislik:
            continue                    # küçük: sessizce geç (çok sayıda)
        if (r[0] < SAYFA_KENAR_PAYI or r[1] < SAYFA_KENAR_PAYI
                or r[2] > genislik - SAYFA_KENAR_PAYI
                or r[3] > yukseklik - SAYFA_KENAR_PAYI):
            retler.append(("sayfa_kenari", r))
            continue
        if _alan(r) > SAYFA_ALAN_ORANI_AZAMI * genislik * yukseklik:
            retler.append(("sayfa_cercevesi", r))
            continue
        uygun.append(r)
    # Katmanları birleştir: büyükten küçüğe; içteki, dıştakinin katmanıysa
    # ona katılır.
    uygun.sort(key=_alan, reverse=True)
    adaylar: List[_Aday] = []
    for r in uygun:
        for a in adaylar:
            if _ayni_rect(r, a.rect) or _katman_mi(r, a.rect):
                a.katmanlar.append(r)
                break
        else:
            adaylar.append(_Aday(sayfa_no, r, [r]))
    return adaylar, ilk, retler


# ---------------------------------------------------------------------------
# Kutu metni
# ---------------------------------------------------------------------------

def _satirlar(sayfa) -> List[Tuple[Rect, str]]:
    out: List[Tuple[Rect, str]] = []
    for b in sayfa.get_text("dict").get("blocks", []):
        if b.get("type") != 0:
            continue
        for ln in b.get("lines", []):
            metin = "".join(s.get("text", "") for s in ln.get("spans", []))
            if metin.strip():
                x0, y0, x1, y1 = ln["bbox"]
                out.append(((x0, y0, x1, y1), metin))
    return out


def _icerde_orani(satir: Rect, r: Rect) -> float:
    a = _alan(satir)
    if a <= 0:
        cx, cy = (satir[0] + satir[2]) / 2, (satir[1] + satir[3]) / 2
        return 1.0 if r[0] <= cx <= r[2] and r[1] <= cy <= r[3] else 0.0
    return _kesisim(satir, r) / a


def kutu_metni(aday: _Aday, satirlar: List[Tuple[Rect, str]]) -> Optional[str]:
    """Kutuya TAM düşen satırların metni; kenar bir satırı kesiyorsa None."""
    parcalar: List[str] = []
    for bbox, metin in satirlar:
        oran = _icerde_orani(bbox, aday.rect)
        if oran >= ICERIDE_ORANI:
            parcalar.append(metin)
        elif oran > DISARIDA_ORANI:
            return None
    return "\n".join(parcalar)


def _ic_cizim_sayisi(aday: _Aday, ilkeller: List[_Ilkel]) -> int:
    """Kutunun içindeki, kendi çerçeve/zemin katmanı olmayan çizim sayısı."""
    r = aday.rect
    tol = KENAR_TOLERANSI
    n = 0
    for p in ilkeller:
        q = p.rect
        if not _icinde(q, r, 1.0):
            continue
        if any(_ayni_rect(q, k) or _katman_mi(q, k) for k in aday.katmanlar):
            continue
        if p.tur == "yatay" and (abs(q[1] - r[1]) <= tol or abs(q[3] - r[3]) <= tol
                                 or abs(q[1] - r[3]) <= tol or abs(q[3] - r[1]) <= tol):
            continue            # kendi üst/alt kenarı
        if p.tur == "dikey" and (abs(q[0] - r[0]) <= tol or abs(q[2] - r[2]) <= tol
                                 or abs(q[0] - r[2]) <= tol or abs(q[2] - r[0]) <= tol):
            continue            # kendi sol/sağ kenarı
        n += 1
    return n


# ---------------------------------------------------------------------------
# Markdown eşleme
# ---------------------------------------------------------------------------

class _MdDizin:
    """Blokların normalleştirilmiş metinleri art arda (g) + blok uçları."""

    def __init__(self, satirlar: Sequence[str]):
        self.satirlar = satirlar
        self.bloklar = bloklar(satirlar)
        parcalar: List[str] = []
        self.bas: List[int] = []
        self.son: List[int] = []
        konum = 0
        for b in self.bloklar:
            self.bas.append(konum)
            parcalar.append(b.anahtar)
            konum += len(b.anahtar)
            self.son.append(konum)
        self.g = "".join(parcalar)

    def _gecisler(self, capa: str) -> List[int]:
        out: List[int] = []
        k = self.g.find(capa)
        while k >= 0 and len(out) <= CAPA_GECIS_AZAMI:
            out.append(k)
            k = self.g.find(capa, k + 1)
        return out

    def _sinir(self, konum: int, uclar: List[int], bas_mi: bool,
               kesin: bool) -> Optional[int]:
        """konum'a SINIR_TOLERANSI içinde blok ucu (boş anahtarlı bloklar
        hariç). Birden çok uç varsa: konum tam bir uçsa ve çapa kutu
        metninin tam ucundan alındıysa (*kesin*) o, değilse belirsiz."""
        lo = bisect.bisect_left(uclar, konum - SINIR_TOLERANSI)
        hi = bisect.bisect_right(uclar, konum + SINIR_TOLERANSI)
        adaylar = [k for k in range(lo, hi) if self.bloklar[k].anahtar]
        if not adaylar:
            return None
        konumlar = sorted({uclar[k] for k in adaylar})
        if len(konumlar) > 1:
            if kesin and konum in konumlar:
                konumlar = [konum]
            else:
                return None
        sec = [k for k in adaylar if uclar[k] == konumlar[0]]
        return sec[0] if bas_mi else sec[-1]

    def bolge(self, b_metni: str) -> Tuple[Optional[Tuple[int, int]], str]:
        """Kutu metninin (norm) Markdown'daki blok aralığı ve (yoksa) neden."""
        L = len(b_metni)
        ciftler = set()
        sik = False
        for d in range(CAPA_DENEME):
            k = d * CAPA_UZUNLUGU
            # İçe kaydırılan çapalar ikiye bölünmüş metnin kendi yarısında
            # kalır; uçtaki çapa (d = 0) kısa metinde de denenir.
            if d and k + CAPA_UZUNLUGU > L // 2 + 1:
                break
            bas_g = self._gecisler(b_metni[k:k + CAPA_UZUNLUGU])
            son_g = self._gecisler(b_metni[L - k - CAPA_UZUNLUGU:L - k])
            if len(bas_g) > CAPA_GECIS_AZAMI or len(son_g) > CAPA_GECIS_AZAMI:
                sik = True
                continue
            for s in bas_g:
                for e in son_g:
                    s0, e1 = s - k, e + CAPA_UZUNLUGU + k
                    if e1 <= s0:
                        continue
                    if abs((e1 - s0) - L) > max(2 * SINIR_TOLERANSI,
                                                UZUNLUK_SAPMA_ORANI * L):
                        continue
                    ciftler.add((s0, e1, k == 0))
            if ciftler:
                break
        if not ciftler:
            return None, ("capa_belirsiz" if sik else "md_bulunamadi")
        sonuc = set()
        for s0, e1, kesin in ciftler:
            k0 = self._sinir(s0, self.bas, True, kesin)
            k1 = self._sinir(e1, self.son, False, kesin)
            if k0 is None or k1 is None or k1 < k0:
                continue
            a_metni = self.g[self.bas[k0]:self.son[k1]]
            if SequenceMatcher(None, a_metni, b_metni,
                               autojunk=False).ratio() < BENZERLIK_ESIGI:
                continue
            sonuc.add((k0, k1))
        if not sonuc:
            return None, "blok_siniri_yok"
        if len(sonuc) > 1:
            return None, "md_belirsiz"
        return sonuc.pop(), ""

    def ara_artik_mi(self, k_a: int, k_b: int) -> bool:
        """k_a ile k_b arasındaki (ikisi hariç) bloklar yalnız kısa artık mı
        (tek satırlık paragraf, en çok ARA_BLOK_EN_UZUN karakter)?"""
        ara = self.bloklar[k_a + 1:k_b]
        if len(ara) > ARA_BLOK_AZAMI:
            return False
        for b in ara:
            if b.tur != "paragraf" or b.son != b.bas \
                    or len(self.satirlar[b.bas].strip()) > ARA_BLOK_EN_UZUN:
                return False
        return True

    def devam_etiketi_arada(self, k_a: int, k_b: int) -> bool:
        """k_a ile k_b arasında sayfa sonu devam etiketi ("(continued)",
        "(Fortsetzung)"; marker_onarim._DEVAM_ETIKET_RE) var mı?"""
        from latex_pipeline import marker_onarim
        return any(marker_onarim._DEVAM_ETIKET_RE.match(
                       self.satirlar[b.bas].strip().strip("*_").strip())
                   for b in self.bloklar[k_a + 1:k_b])

    def acik_uclu(self, k: int) -> bool:
        """k. blok cümle ortasında mı bitiyor? Ölçüt, sayfa kırılımı
        onarımınınkiyle aynıdır (marker_onarim._acik_uclu): kutu sınırı onun
        birleştireceği iki yarıyı ayırmamalı."""
        from latex_pipeline import marker_onarim
        b = self.bloklar[k]
        return b.tur in ("paragraf", "liste") and \
            marker_onarim._acik_uclu(self.satirlar[b.son])

    def devam_bicimli(self, k: int) -> bool:
        """k. blok bir önceki cümlenin devamı gibi mi başlıyor
        (marker_onarim._devam_bicimli)?"""
        from latex_pipeline import marker_onarim
        b = self.bloklar[k]
        return b.tur == "paragraf" and \
            marker_onarim._devam_bicimli(self.satirlar[b.bas])


# ---------------------------------------------------------------------------
# Ana giriş
# ---------------------------------------------------------------------------

@dataclass
class _Eslesme:
    parcalar: List[_Aday]             # sayfa parçaları (sırayla)
    anahtar: str                      # norm(kutu metni), parçalar art arda
    ornek: str
    aralik: Optional[Tuple[int, int]] = None
    neden: str = ""

    @property
    def ilk(self) -> _Aday:
        return self.parcalar[0]

    @property
    def son(self) -> _Aday:
        return self.parcalar[-1]


def kutulari_bul(markdown: str, pdf_yolu: str,
                 rapor: Optional[KutuRaporu] = None) -> List[Tuple[int, int, Dict[str, object]]]:
    """Kabul edilen kutular: (ilk satır, son satır, bilgi) — satırlar
    *markdown*'ın satır indeksleri (dahil)."""
    rapor = rapor if rapor is not None else KutuRaporu()
    try:
        import fitz  # PyMuPDF
    except ImportError:            # pragma: no cover - ortamda hep var
        rapor.say("fitz_yok")
        return []
    try:
        doc = fitz.open(str(pdf_yolu))
    except Exception as exc:  # noqa: BLE001 - bozuk PDF onarımı durdurmaz
        logger.debug("Kutu onarımı: PDF açılamadı (%s)", exc)
        rapor.say("pdf_acilamadi")
        return []
    try:
        return _kutulari_bul(doc, markdown, rapor)
    finally:
        doc.close()


def _geometri_adaylari(doc, rapor: KutuRaporu) -> List[Tuple[_Aday, List[_Ilkel]]]:
    out: List[Tuple[_Aday, List[_Ilkel]]] = []
    for pno in range(doc.page_count):
        sayfa = doc[pno]
        try:
            cizimler = sayfa.get_drawings()
        except Exception:  # noqa: BLE001 - tek sayfanın çizimi okunamazsa
            rapor.say("cizim_okunamadi")
            continue
        if not cizimler:
            continue
        if len(cizimler) > SAYFA_CIZIM_AZAMI:
            rapor.say("sayfa_cok_cizimli")
            continue
        g, y = sayfa.rect.width, sayfa.rect.height
        adaylar, ilkeller, retler = sayfa_adaylari(pno, cizimler, g, y)
        for neden, r in retler:
            rapor.atla(neden, pno + 1, r)
        out.extend((a, ilkeller) for a in adaylar)
    return out


def _gecerli_adaylar(doc, adaylar: List[Tuple[_Aday, List[_Ilkel]]],
                     rapor: KutuRaporu) -> List[_Eslesme]:
    """Geometri ve metin süzgeçlerinden geçen adaylar (sayfa, y sırasıyla)."""
    out: List[_Eslesme] = []
    sayfa_satirlari: Dict[int, List[Tuple[Rect, str]]] = {}
    for a, ilkeller in adaylar:
        sno = a.sayfa + 1
        sayfa = doc[a.sayfa]
        tekrar = len({b.sayfa for b, _ in adaylar if _ayni_rect(a.rect, b.rect)})
        if tekrar >= YINELENEN_SAYFA_ESIGI:
            rapor.atla("yinelenen", sno, a.rect)
            continue
        if (a.rect[3] - a.rect[1]) > KUTU_YUKSEKLIK_ORANI_AZAMI * sayfa.rect.height:
            rapor.atla("uzun", sno, a.rect)
            continue
        komsular = [b for b, _ in adaylar if b is not a and b.sayfa == a.sayfa]
        if any(_icinde(b.rect, a.rect, 0.5) or _icinde(a.rect, b.rect, 0.5)
               for b in komsular):
            rapor.atla("ic_ice", sno, a.rect)
            continue
        if any(_bitisik_mi(a.rect, b.rect) for b in komsular):
            rapor.atla("bitisik", sno, a.rect)
            continue
        try:
            gorseller = sayfa.get_image_info()
        except Exception:  # noqa: BLE001
            gorseller = []
        if any(_alan(tuple(g["bbox"])) >= SIMGE_ALAN_ORANI * _alan(a.rect)
               and _kesisim(tuple(g["bbox"]), a.rect) > 0.1 * _alan(tuple(g["bbox"]))
               for g in gorseller):
            rapor.atla("gorsel", sno, a.rect)
            continue
        if _ic_cizim_sayisi(a, ilkeller) > IC_CIZIM_AZAMI:
            rapor.atla("ic_cizim", sno, a.rect)
            continue
        if a.sayfa not in sayfa_satirlari:
            sayfa_satirlari[a.sayfa] = _satirlar(sayfa)
        metin = kutu_metni(a, sayfa_satirlari[a.sayfa])
        if metin is None:
            rapor.atla("kenar_metni_kesiyor", sno, a.rect)
            continue
        anahtar = norm(metin)
        if len(anahtar) < KUTU_EN_AZ_HARF:
            rapor.atla("az_metin", sno, a.rect, metin)
            continue
        out.append(_Eslesme([a], anahtar, metin))
    out.sort(key=lambda e: (e.ilk.sayfa, e.ilk.rect[1]))
    return out


def _zincirlenebilir(onceki: _Eslesme, e: _Eslesme,
                     gecerli: List[_Eslesme]) -> bool:
    """onceki'nin son parçası sayfanın en alttaki adayı, e sonraki sayfanın
    en üstteki adayı ve ikisi aynı genişlikte mi?"""
    a, b = onceki.son, e.ilk
    if b.sayfa != a.sayfa + 1:
        return False
    if abs(a.rect[0] - b.rect[0]) > ZINCIR_X_TOLERANSI or \
            abs(a.rect[2] - b.rect[2]) > ZINCIR_X_TOLERANSI:
        return False
    alttaki = max((x.son.rect[3] for x in gecerli if x.son.sayfa == a.sayfa),
                  default=a.rect[3])
    ustteki = min((x.ilk.rect[1] for x in gecerli if x.ilk.sayfa == b.sayfa),
                  default=b.rect[1])
    return a.rect[3] >= alttaki and b.rect[1] <= ustteki


def _zincirle(onceki: _Eslesme, e: _Eslesme, dizin: _MdDizin) -> Optional[_Eslesme]:
    """Sayfa sınırında bölünmüş kutunun iki parçasını birleştir; olmazsa None.

    İkisi de ayrı ayrı eşlendiyse: aralarında yalnız kısa artık bloklar var
    ve kutunun sürdüğüne kanıt var: paragraf sınırı aşıyor (önceki cümle
    ortasında bitiyor ya da sonraki devam gibi başlıyor) ya da arada sayfa
    sonu devam etiketi ("(continued)") duruyor. Biri eşlenemediyse (Marker
    bölünen paragrafı tek blok yazdı): iki parçanın metni birlikte eşlenir."""
    parcalar = onceki.parcalar + e.parcalar
    anahtar = onceki.anahtar + e.anahtar
    if onceki.aralik and e.aralik:
        (a0, a1), (b0, b1) = onceki.aralik, e.aralik
        if b0 <= a1 or not dizin.ara_artik_mi(a1, b0):
            return None
        if not (dizin.acik_uclu(a1) or dizin.devam_bicimli(b0)
                or dizin.devam_etiketi_arada(a1, b0)):
            return None             # iki ayrı, tam kutu
        return _Eslesme(parcalar, anahtar, onceki.ornek, (a0, b1))
    if (onceki.aralik is None and onceki.neden not in _BIRLESIK_DENE) or \
            (e.aralik is None and e.neden not in _BIRLESIK_DENE):
        return None
    aralik, _neden = dizin.bolge(anahtar)
    if aralik is None:
        return None
    return _Eslesme(parcalar, anahtar, onceki.ornek, aralik)


def _kutulari_bul(doc, markdown: str, rapor: KutuRaporu):
    adaylar = _geometri_adaylari(doc, rapor)
    if not adaylar:
        return []
    rapor.say("aday", len(adaylar))
    gecerli = _gecerli_adaylar(doc, adaylar, rapor)
    if not gecerli:
        return []
    satirlar_md = markdown.split("\n")
    dizin = _MdDizin(satirlar_md)
    for e in gecerli:
        e.aralik, e.neden = dizin.bolge(e.anahtar)

    # Sayfa sınırında bölünen kutular.
    zincir: List[_Eslesme] = []
    for e in gecerli:
        if zincir and _zincirlenebilir(zincir[-1], e, gecerli):
            birlesik = _zincirle(zincir[-1], e, dizin)
            if birlesik is not None:
                rapor.say("zincir")
                zincir[-1] = birlesik
                continue
        zincir.append(e)

    kabul: List[Tuple[int, int, Dict[str, object]]] = []
    for e in zincir:
        sno, rect = e.ilk.sayfa + 1, e.ilk.rect
        if e.aralik is None:
            rapor.atla(e.neden, sno, rect, e.ornek)
            continue
        k0, k1 = e.aralik
        if {dizin.bloklar[k].tur for k in range(k0, k1 + 1)} & _YASAK_TURLER:
            rapor.atla("yasak_blok", sno, rect, e.ornek)
            continue
        if dizin.bloklar[k1].tur == "baslik":
            rapor.atla("baslikla_bitiyor", sno, rect, e.ornek)
            continue
        if dizin.devam_bicimli(k0):
            rapor.atla("devam_ile_basliyor", sno, rect, e.ornek)
            continue
        if dizin.acik_uclu(k1):
            rapor.atla("acik_uclu_bitiyor", sno, rect, e.ornek)
            continue
        bas, son = dizin.bloklar[k0].bas, dizin.bloklar[k1].son
        kabul.append((bas, son, {
            "sayfa": sno,
            "sayfalar": [p.sayfa + 1 for p in e.parcalar],
            "rect": tuple(round(v, 1) for v in rect),
            "blok": k1 - k0 + 1,
            "ilk": satirlar_md[bas][:80],
            "son": satirlar_md[son][-80:]}))

    # Çakışan kutular (iki aday aynı ya da örtüşen bloklara düştü): ikisi de
    # atılır — hangisinin doğru olduğu bilinemez.
    kabul.sort(key=lambda t: (t[0], t[1]))
    atilan: set = set()
    for i, (_b1, s1, _) in enumerate(kabul):
        for j in range(i + 1, len(kabul)):
            if kabul[j][0] > s1:
                break
            atilan.update((i, j))
    temiz: List[Tuple[int, int, Dict[str, object]]] = []
    for i, k in enumerate(kabul):
        if i in atilan:
            rapor.atla("cakisan", int(k[2]["sayfa"]), k[2]["rect"])
        else:
            temiz.append(k)
    return temiz


def onar_raporlu(markdown: str, pdf_yolu: Optional[str]) -> Tuple[str, KutuRaporu]:
    """Kutuya TAM düşen bütün blokları KUTU_BAS / KUTU_SON satırlarıyla sar.

    PDF yoksa ya da kutu bulunamazsa Markdown AYNEN döner."""
    rapor = KutuRaporu()
    if not markdown or not pdf_yolu or not str(pdf_yolu).lower().endswith(".pdf"):
        return markdown, rapor
    if KUTU_BAS in markdown or KUTU_SON in markdown:
        rapor.say("zaten_isaretli")
        return markdown, rapor
    kutular = kutulari_bul(markdown, str(pdf_yolu), rapor)
    if not kutular:
        return markdown, rapor
    satirlar = markdown.split("\n")
    # Sondan başa ekle: önceki satır indeksleri kaymaz.
    for bas, son, bilgi in sorted(kutular, key=lambda t: t[0], reverse=True):
        satirlar[son + 1:son + 1] = ["", KUTU_SON]
        satirlar[bas:bas] = [KUTU_BAS, ""]
        rapor.kutular.append(bilgi)
    rapor.kutular.reverse()
    rapor.say("kutu", len(kutular))
    return "\n".join(satirlar), rapor


def onar(markdown: str, pdf_yolu: Optional[str]) -> str:
    return onar_raporlu(markdown, pdf_yolu)[0]


# ---------------------------------------------------------------------------
# Dönüştürücü tarafı
# ---------------------------------------------------------------------------

def isaretleri_ayikla(satirlar: Sequence[str]) -> Tuple[List[str], List[Tuple[int, str]]]:
    """İşaret satırlarını çıkar; olaylar: (satır indeksi, "bas" | "son").

    "bas": kutunun ilk satırı (ayıklanmış listede); "son": kutunun son
    bloğundan sonraki satır. onar_raporlu'nun eklediği boş satır da
    alınır: ayıklanmış liste işaretsiz Markdown'la satır satır AYNIDIR.
    Aynı indekse düşen "son" ve "bas" (bitişik kutular) sırası: önce "son"."""
    out: List[str] = []
    olaylar: List[Tuple[int, str]] = []
    atla_bos = False
    for s in satirlar:
        t = s.strip()
        if t == KUTU_BAS:
            olaylar.append((len(out), "bas"))
            atla_bos = True
            continue
        if t == KUTU_SON:
            if out and not out[-1].strip():
                out.pop()
            olaylar.append((len(out), "son"))
            atla_bos = False
            continue
        if atla_bos:
            atla_bos = False
            if not t:
                continue
        out.append(s)
    olaylar.sort(key=lambda o: (o[0], 0 if o[1] == "son" else 1))
    return out, olaylar
