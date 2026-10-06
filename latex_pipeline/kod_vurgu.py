# -*- coding: utf-8 -*-
"""Kod çitindeki düzyazı alıntının kalın/italik vurgusu.

6 Ekim 2026. Marker kaynaktaki eş aralıklı DÜZYAZI alıntıları (ajan akıl
yürütmesi, CoT) kod çiti (```) olarak yazar ve kutudaki vurguyu düşürür:
eş aralıklı alıntı kutusunda kalın dizilmiş kesitler (yazı tipinin kalın
biçimi, ör. "...Mono-Bold") ve tümüyle italik dizilmiş kutular (italik
biçimi) düz metne iner. Dönüştürücü çiti ``lstlisting`` yapıyordu; şablonda
lstlisting fancyvrb Verbatim'idir, içinde ``\\textbf`` çalışmaz. Sanitizer
(G3a-2) düzyazı kutusunu ancak derlemede alıntıya çeviriyordu; vurgu bilgisi
orada yoktu.
``vurgu_onarim`` eş aralıklı yazı tiplerini ve kod bloklarını bilerek atlar.

Bu modül:

- kod çitinin HAM içeriğini kaynak PDF'in karakter akışıyla
  (``pdf_metin_onarim``) hizalar; içerik PDF'te benzersiz (ya da her
  geçtiği yerde aynı vurguyla) bulunmalıdır;
- PDF yazı tipi adından (``vurgu_onarim``'ın kalın/italik ölçütleriyle;
  eş aralıklı yazı tipleri DAHİL) kalın/italik koşuları çıkarır;
- düzyazı çiti için (dil seçeneksiz + ``sanitizer.listing_is_prose``)
  doğrudan sanitizer'ın alıntı biçimini (``listing_to_quote``) üretir,
  koşular ``\\textbf{}`` / ``\\textit{}`` olur; kutu önceki paragrafın yarım
  cümlesini sürdürüyorsa (``listing_continues_paragraph``)
  paragrafa eklenecek metni üretir.

Böylece EXPORT.tex'te alıntı temiz LaTeX'tir: çevirmen (yapay zekâ ya da
yerel model) metni çevirir, vurgu komutunu korur; derlemede sanitizer'ın
dönüştüreceği lstlisting kalmaz.

Kayıpsızlık: metin eklenmez, silinmez, sırası değişmez; yalnız vurgu komutu
eklenir. Her çıktının vurgu komutları çıkarılmış hâli, sanitizer'ın aynı
içerikten ürettiği metinle (``listing_to_quote`` / paragrafa bağlama metni)
BİREBİR karşılaştırılır; tutmazsa vurgusuz biçim döner. Belirsiz her
durumda (içerik PDF'te yok, farklı vurguyla birden çok yerde, eşleşme oranı
düşük) vurgu eklenmez. Düzyazı olmayan gerçek kod Verbatim kalır; onun
vurgusu gösterilmez (sınırlama).

Sözleşme: ``duzyazi_kod_blogu(kod, pdf_yolu, onceki, rapor)`` ->
``None`` (düzyazı değil: dönüştürücü lstlisting basar) ya da
``(latex, paragrafa_bagla)``.
"""
from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from latex_pipeline import pdf_metin_onarim as pmo
from latex_pipeline import vurgu_onarim as vo
from latex_pipeline.sanitizer import (
    _escape_quote_text,
    listing_continues_paragraph,
    listing_is_prose,
    listing_to_quote,
)

logger = logging.getLogger(__name__)

# Koşu en az bu kadar harf taşır (vurgu onarımıyla aynı ölçüt): tek başına
# kalın üç nokta ya da tırnak vurgu sayılmaz.
KOSU_EN_AZ_HARF = vo.KOSU_EN_AZ_HARF
# İçeriğin normalleştirilmiş hâli en az bu uzunlukta olmalı (bağlam
# eşlemesinin en kısa bağlamı): daha kısası PDF'te benzersiz bulunamaz.
EN_KISA_ICERIK = pmo.EN_KISA_BAGLAM
# Tam eşleşme yoksa (kutu sayfa sonunda bölünmüş — akışta araya dipnot,
# sayfa numarası, üst bilgi girer — ya da Marker bir karakteri farklı
# okumuş) içerik, PDF'te benzersiz geçen çapa parçalarıyla bulunan pencerede
# karakter karakter hizalanır. Çapa uzunluğu: bağlam eşlemesinin en geniş
# penceresi (pdf_metin_onarim.BAGLAM_PENCERELERI, 24 + 24) düzeyinde.
CAPA_UZUNLUGU = 24
# Hizalamada içerik karakterlerinin en az bu oranı PDF'te karşılık bulmalı.
ESLESME_ORANI = 0.9
# Bundan kısa ortak bloklar hizalamada sayılmaz (pencerenin araya giren
# kısmındaki tek tük harf rastlantısı vurgu taşımasın).
HIZALAMA_EN_KISA_BLOK = 3
# Pencere: çapaların kapsadığı aralık + iki yanda bu kadar karakter pay.
PENCERE_PAYI = 64
# Pencere en çok içeriğin bu katı (+ paylar) olabilir: sayfa sonundaki
# dipnotlar araya girebilir, ama sınırsız pencere yanlış yere hizalar.
PENCERE_EN_COK_KAT = 4
# Aynı içerik PDF'te bundan çok yerde geçiyorsa belirsiz sayılır.
EN_COK_TEKRAR = 4
# Yaklaşık hizalama bundan uzun içerikte denenmez (karakter karakter
# hizalama içerik x pencere ölçeğinde büyür; ölçüm: 6000 normalleştirilmiş
# karakter + dört katı pencere ~0,8 sn). Düzyazı kod çitleri kısa
# alıntılardır; uzun içerikte yalnız tam eşleşme aranır.
YAKLASIK_EN_UZUN = 6000
# Koşunun uçlarındaki tırnak işaretleri koşunun dışında kalır ('“\textbf{X}'):
# elle onarılmış altın referansların biçimi; dönüştürücünün eş aralıklı
# parça kuralıyla aynı küme (converter._MONO_EDGE_QUOTES).
UC_TIRNAKLARI = "\"'“”‘’«»„"

ITALIK, KALIN = 1, 2
_ACILIS = {ITALIK: "\\textit{", KALIN: "\\textbf{", ITALIK | KALIN: "\\textbf{\\textit{"}
_KAPANIS = {ITALIK: "}", KALIN: "}", ITALIK | KALIN: "}}"}
_TUR = {ITALIK: "italik", KALIN: "kalin", ITALIK | KALIN: "kalin_italik"}


@dataclass
class KodVurguRaporu:
    """Dönüştürücü başına sayaç (tanı ve korpus denetimi için)."""
    sayac: Dict[str, int] = field(default_factory=dict)
    ornekler: List[str] = field(default_factory=list)

    def say(self, ad: str, ornek: str = "") -> None:
        self.sayac[ad] = self.sayac.get(ad, 0) + 1
        if ornek and len(self.ornekler) < 40:
            self.ornekler.append(f"{ad}: {ornek[:70]}")


# ---------------------------------------------------------------------------
# PDF yazı tipi -> vurgu
# ---------------------------------------------------------------------------

def pdf_stili(font: str) -> int:
    """Yazı tipi adından vurgu: 0 düz, 1 italik, 2 kalın, 3 ikisi; -1
    matematik/simge ya da adsız yazı tipi.

    ``vurgu_onarim.font_stili`` ile aynı ad ölçütü (aynı düzenli ifadeler);
    tek fark eş aralıklı yazı tiplerinin DAHİL olması: kod kutusunun yazı
    tipi zaten eş aralıklıdır (ör. Courier-Bold, ...Mono-Bold).
    """
    f = (font or "").rsplit("+", 1)[-1]
    f = re.sub(r"-Identity-[HV]$", "", f)
    f = re.sub(r"\d+$", "", f)
    if not f or vo._MATEMATIK_FONT_RE.search(f):
        return -1
    f = re.sub(r"(?:MT|PS|Std|LT)$", "", f)
    stil = 0
    if vo._ITALIK_FONT_RE.search(f):
        stil |= ITALIK
    if vo._KALIN_FONT_RE.search(f):
        stil |= KALIN
    return stil


# ---------------------------------------------------------------------------
# Hizalama
# ---------------------------------------------------------------------------

def _kod_normu(kod: str) -> Tuple[str, List[int]]:
    """İçeriğin karşılaştırma biçimi (pdf_metin_onarim ile aynı
    normalleştirme) ve her norm karakterinin içerikteki ofseti."""
    norm: List[str] = []
    ofset: List[int] = []
    for o, c in enumerate(kod):
        for ch in pmo._norm_karakter(c):
            norm.append(ch)
            ofset.append(o)
    return "".join(norm), ofset


def _stil_dizisi(pdf: "pmo._PdfAkis", konumlar: Sequence[Optional[int]],
                 onbellek: Dict[str, int]) -> List[int]:
    out: List[int] = []
    for p in konumlar:
        if p is None:
            out.append(-1)
            continue
        font = pdf.font[pdf.kaynak[p]]
        st = onbellek.get(font)
        if st is None:
            st = onbellek[font] = pdf_stili(font)
        out.append(st)
    return out


def _tam_eslesme(cn: str, pdf: "pmo._PdfAkis", onbellek: Dict[str, int]
                 ) -> Tuple[Optional[List[int]], str]:
    """İçerik PDF akışında aynen geçiyorsa her norm karakterinin stili."""
    yerler: List[int] = []
    j = pdf.norm.find(cn)
    while j >= 0 and len(yerler) <= EN_COK_TEKRAR:
        yerler.append(j)
        j = pdf.norm.find(cn, j + 1)
    if not yerler:
        return None, "tam_yok"
    if len(yerler) > EN_COK_TEKRAR:
        return None, "belirsiz"
    n = len(cn)
    stiller = [_stil_dizisi(pdf, range(y, y + n), onbellek) for y in yerler]
    if any(s != stiller[0] for s in stiller[1:]):
        return None, "belirsiz"   # aynı metin farklı vurguyla birden çok yerde
    return stiller[0], "tam"


def _yaklasik_eslesme(cn: str, pdf: "pmo._PdfAkis", onbellek: Dict[str, int]
                      ) -> Tuple[Optional[List[int]], str]:
    """Benzersiz çapalarla pencere bul, pencerede karakter karakter hizala."""
    n = len(cn)
    if n > YAKLASIK_EN_UZUN:
        return None, "uzun"
    k = min(CAPA_UZUNLUGU, n)
    capalar: List[Tuple[int, int]] = []   # (içerik ofseti, PDF norm konumu)
    for bas in sorted({0, n // 4, n // 2, (3 * n) // 4, n - k}):
        if bas < 0 or bas + k > n:
            continue
        parca = cn[bas:bas + k]
        p = pdf.norm.find(parca)
        if p < 0 or pdf.norm.find(parca, p + 1) >= 0:
            continue
        capalar.append((bas, p))
    if not capalar:
        return None, "bulunamadi"
    # Çapalar içerik sırasıyla PDF'te de ilerlemeli (yoksa farklı yerler).
    if any(b[1] <= a[1] for a, b in zip(capalar, capalar[1:])):
        return None, "belirsiz"
    w0 = max(0, capalar[0][1] - capalar[0][0] - PENCERE_PAYI)
    w1 = min(len(pdf.norm), capalar[-1][1] + (n - capalar[-1][0]) + PENCERE_PAYI)
    if w1 - w0 > PENCERE_EN_COK_KAT * n + 2 * PENCERE_PAYI:
        return None, "pencere_genis"
    sm = difflib.SequenceMatcher(None, cn, pdf.norm[w0:w1], autojunk=False)
    konum: List[Optional[int]] = [None] * n
    toplam = 0
    for a, b, boy in sm.get_matching_blocks():
        if boy < HIZALAMA_EN_KISA_BLOK:
            continue
        for t in range(boy):
            konum[a + t] = w0 + b + t
        toplam += boy
    if toplam < ESLESME_ORANI * n:
        return None, "eslesme_dusuk"
    return _stil_dizisi(pdf, konum, onbellek), "yaklasik"


def vurgu_kosulari(kod: str, pdf: Optional["pmo._PdfAkis"]
                   ) -> Tuple[List[Tuple[int, int, int]], str]:
    """İçerikteki vurgu koşuları [(baş, son, stil)] (içerik ofseti, son
    hariç) ve eşleşme durumu (tam / yaklasik / pdf_yok / kisa / uzun /
    bulunamadi / belirsiz / pencere_genis / eslesme_dusuk)."""
    if pdf is None:
        return [], "pdf_yok"
    cn, ofset = _kod_normu(kod)
    if len(cn) < EN_KISA_ICERIK:
        return [], "kisa"
    onbellek: Dict[str, int] = {}
    stiller, durum = _tam_eslesme(cn, pdf, onbellek)
    if stiller is None and durum == "tam_yok":
        stiller, durum = _yaklasik_eslesme(cn, pdf, onbellek)
    if stiller is None:
        return [], durum
    # Karakter stili: içerik karakterinin bütün norm karakterleri aynı
    # stilde olmalı ("…" -> "..."); karşılıksız norm karakteri koşuyu keser.
    kar_stil: Dict[int, int] = {}
    for i, st in enumerate(stiller):
        o = ofset[i]
        once = kar_stil.get(o)
        kar_stil[o] = st if once is None or once == st else -1
    # Koşu: aynı pozitif stildeki ardışık (normu olan) karakterler; normu
    # olmayanlar (boşluk, satır sonu, '[', ']', '_', '*') iki yanı aynı
    # koşudaysa içeride kalır.
    kosular: List[Tuple[int, int, int]] = []
    sirali = sorted(kar_stil.items())
    i = 0
    while i < len(sirali):
        o, st = sirali[i]
        if st <= 0:
            i += 1
            continue
        k = i
        while k + 1 < len(sirali) and sirali[k + 1][1] == st:
            k += 1
        bas, son = o, sirali[k][0] + 1
        while bas < son and (kod[bas] in UC_TIRNAKLARI or kod[bas].isspace()):
            bas += 1
        while son > bas and (kod[son - 1] in UC_TIRNAKLARI or kod[son - 1].isspace()):
            son -= 1
        if sum(1 for c in kod[bas:son] if c.isalpha()) >= KOSU_EN_AZ_HARF:
            kosular.append((bas, son, st))
        i = k + 1
    return kosular, durum


# ---------------------------------------------------------------------------
# LaTeX üretimi (sanitizer'ın biçimiyle birebir)
# ---------------------------------------------------------------------------

def _paragraflar(kod: str) -> List[List[Tuple[str, int]]]:
    """``listing_to_quote`` ile aynı bölme (satırlar kırpılır, tek boşlukla
    birleşir, boş satır paragraf ayırır); her karakterin içerik ofseti.
    Birleştirme boşluğu önceki satırın satır sonu ofsetini taşır (o satır
    sonu bir koşunun içindeyse boşluk da içindedir)."""
    paras: List[List[Tuple[str, int]]] = []
    cur: List[Tuple[str, int]] = []
    pos = 0
    for ln in kod.split("\n"):
        kirpik = ln.strip()
        if kirpik:
            bas = pos + len(ln) - len(ln.lstrip())
            if cur:
                cur.append((" ", pos - 1))
            cur.extend((c, bas + k) for k, c in enumerate(kirpik))
        elif cur:
            paras.append(cur)
            cur = []
        pos += len(ln) + 1
    if cur:
        paras.append(cur)
    return paras


def _yaz(parca: Sequence[Tuple[str, int]], stil: Sequence[int]) -> Tuple[str, str]:
    """(vurgulu, vurgusuz) kaçışlı metin."""
    out: List[str] = []
    duz: List[str] = []
    aktif = 0
    for c, o in parca:
        st = stil[o]
        if st != aktif:
            if aktif:
                out.append(_KAPANIS[aktif])
            if st:
                out.append(_ACILIS[st])
            aktif = st
        e = _escape_quote_text(c)
        out.append(e)
        duz.append(e)
    if aktif:
        out.append(_KAPANIS[aktif])
    return "".join(out), "".join(duz)


def _stil_tablosu(kod: str, kosular: Sequence[Tuple[int, int, int]]) -> List[int]:
    stil = [0] * len(kod)
    for bas, son, st in kosular:
        for o in range(bas, son):
            stil[o] = st
    return stil


def alinti_latex(kod: str, kosular: Sequence[Tuple[int, int, int]]) -> Tuple[str, str]:
    """Alıntı ortamı: (vurgulu, vurgusuz). Vurgusuz hâl
    ``listing_to_quote(kod)`` ile aynı olmalıdır."""
    stil = _stil_tablosu(kod, kosular)
    yazili = [_yaz(p, stil) for p in _paragraflar(kod)]
    bas, son = "\\begin{quote}\n\\raggedright\\ttfamily ", "\n\\end{quote}"
    return (bas + "\n\n".join(v for v, _ in yazili) + son,
            bas + "\n\n".join(d for _, d in yazili) + son)


def baglama_metni(kod: str, kosular: Sequence[Tuple[int, int, int]]) -> Tuple[str, str]:
    """Yarım cümleyi sürdüren kutunun önceki paragrafa eklenecek tek parça
    metni: (vurgulu, vurgusuz)."""
    # Paragraflar arası boşluk (çitin sonundaki ofset) vurgusuz. Bağlanan
    # kutuda iç boş satır yoktur (listing_continues_paragraph); yine de
    # sanitizer gibi bütün dolu satırlar tek boşlukla birleşir.
    stil = _stil_tablosu(kod, kosular) + [0]
    tek: List[Tuple[str, int]] = []
    for p in _paragraflar(kod):
        if tek:
            tek.append((" ", len(kod)))
        tek.extend(p)
    return _yaz(tek, stil)


def _sanitizer_baglama_metni(kod: str) -> str:
    """sanitizer._prose_listings_to_quote'un paragrafa eklediği metin."""
    return _escape_quote_text(
        " ".join(ln.strip() for ln in kod.split("\n") if ln.strip()))


# ---------------------------------------------------------------------------
# Dönüştürücü giriş noktası
# ---------------------------------------------------------------------------

def _pdf_akisi(pdf_yolu: str) -> Optional["pmo._PdfAkis"]:
    """PDF akışı (pdf_metin_onarim'ın önbelleğinden: dönüştürücü aynı PDF'in
    akışını simge ve vurgu onarımında az önce kurmuştur)."""
    return pmo._pdf_akisi_kur(pdf_yolu)


def duzyazi_kod_blogu(kod: str, pdf_yolu: Optional[str], onceki: str = "",
                      rapor: Optional[KodVurguRaporu] = None,
                      ) -> Optional[Tuple[str, bool]]:
    """Dil seçeneksiz kod çiti içeriği için LaTeX.

    Düzyazı değilse (``listing_is_prose``) None: dönüştürücü lstlisting
    basar. Düzyazıysa ``(latex, paragrafa_bagla)``: ``paragrafa_bagla``
    False ise ``latex`` alıntı ortamıdır; True ise önceki paragrafın sonuna
    bir boşlukla eklenecek metindir (*onceki*: çitten önceki gövde metni,
    çitle ayıran satır sonu dahil; ``listing_continues_paragraph``).
    """
    if rapor is None:
        rapor = KodVurguRaporu()
    if not listing_is_prose(kod):
        return None
    kosular: List[Tuple[int, int, int]] = []
    if pdf_yolu and str(pdf_yolu).lower().endswith(".pdf"):
        kosular, durum = vurgu_kosulari(kod, _pdf_akisi(str(pdf_yolu)))
    else:
        durum = "pdf_yok"
    # Eşleşme durumu da sayılır (tam / yaklasik / pdf_yok / bulunamadi ...).
    rapor.say(durum, "" if durum in ("tam", "yaklasik") else kod.strip().split("\n", 1)[0])
    bagla = listing_continues_paragraph(onceki, kod)
    if bagla:
        latex, duz = baglama_metni(kod, kosular)
        beklenen = _sanitizer_baglama_metni(kod)
    else:
        latex, duz = alinti_latex(kod, kosular)
        beklenen = listing_to_quote(kod)
    if duz != beklenen:   # kayıpsızlık denetimi: biçim sanitizer'la aynı değil
        rapor.say("denetim_tutmadi", kod.strip().split("\n", 1)[0])
        logger.warning("Kod çiti vurgusu: kayıpsızlık denetimi tutmadı; vurgusuz biçim")
        latex = beklenen
        kosular = []
    rapor.say("paragrafa_baglandi" if bagla else "duzyazi_alinti")
    for bas, son, st in kosular:
        rapor.say(_TUR[st], kod[bas:son].replace("\n", " "))
    return latex, bagla
