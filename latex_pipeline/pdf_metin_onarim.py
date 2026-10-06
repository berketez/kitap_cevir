# -*- coding: utf-8 -*-
"""Kaynak PDF'in metin katmanıyla Marker satır içi simge onarımı.

Kulvar G1c (2026-09-30). Marker 1.10.2 metinli (dijital) PDF'lerde satır içi
simgeleri pdftext'in kaba sezgisiyle işaretler ve şu hataları üretir
(okuma bulguları MT-2, MT-3, MT-4, MT-5, MT-8, MT-26; 2026-09-30):

- alt simgeyi ``<sup>`` yazar (v_L -> ``v<sup>L</sup>``, m_1 -> ``m<sup>1</sup>``);
  Marker ``has_subscript`` bilgisini hiç basmadığı için alt simgelerin bir
  kısmı da düz metne iner (x_Q -> ``xQ``, v_e -> ``ve``, R_K -> ``*R*K``);
- satır başındaki ya da tuhaf konumdaki TABAN çizgisi sayıları üst simge
  yapar (``<sup>100</sup>W``, ``Bi *<sup>≈</sup>* <sup>0</sup>*,*003``);
- gerçek üst simgeyi düz metne indirir (10^11 -> ``1011``, km^3 -> ``km3``,
  dipnot işareti "seven ferries¹" -> ``seven ferries 1``);
- vektör okunu ``~r`` / ``h~`` yazar (PDF'te U+20D7 birleştirici ok);
- derece işaretini beyaz madde imi ``◦`` (U+25E6) yazar;
- Computer Modern CMSY yazıtipindeki el yazısı büyük harfi düz harf yazar
  (ℰ -> ``E``, 𝒫 -> ``P``);
- tablo hücresinde üst simgeli birimi satırlara böler (``km3<br>−2<br>s``).

Bu modül Marker'ın ham Markdown'ını kaynak PDF'in metin katmanıyla
(PyMuPDF ``rawdict``: karakter karakter taban çizgisi, punto, yazıtipi)
karşılaştırır ve YALNIZ kesin eşleşmelerde düzeltir:

- Her aday yer, PDF'te ve Markdown'da BELGE BOYUNCA BENZERSİZ bir bağlam
  dizgisiyle (boşluksuz, normalleştirilmiş) eşlenir; bağlam iki tarafta da
  tek kez geçmiyorsa dokunulmaz ve "belirsiz" sayılır.
- Simge sınıfı PDF'in geometrisinden gelir: karakter satırın gövde
  puntosundan belirgin küçükse ve taban çizgisi belirgin yukarıdaysa üst,
  aşağıdaysa alt simgedir; aynı taban çizgisindeyse düz metindir.
- Metin eklenmez, silinmez: yalnız ``<sup>``/``<sub>`` etiketleri eklenir,
  kaldırılır ya da değiştirilir; boşluk, PDF'te karşılığı varsa eklenir ya
  da (dipnot işaretinden önce) kaldırılır; ``◦`` -> ``°``, ``~r`` ->
  ``$\\vec{r}$``, ``E`` -> ``$\\mathcal{E}$`` gibi dönüşümler karakteri
  korur (yalnız gösterimi düzeltir). Tablo hücresi yeniden sıralaması
  karakter çoklu kümesi birebir aynıysa yapılır.

Sözleşme: ``onar(markdown, pdf_path) -> str``. PDF yolu yoksa, dosya
açılamıyorsa ya da metin katmanı yoksa (taranmış PDF) Markdown AYNEN döner.
Dönüştürücüde (``MarkdownToLatex.convert``) ``_preprocess_markdown``'dan
ÖNCE çağrılmalıdır: ön işlem ``<sup>``/``<sub>`` etiketlerini LaTeX'e
çevirir ve ``<br>``'leri siler; bu modül etiketleri ham hâliyle görmelidir.
"""
from __future__ import annotations

import difflib
from collections import defaultdict
import html
import itertools
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sabitler (ölçüm: 30 Eyl 2026, üç test kitabının kaynak PDF'leri;
# gerekçeler iç ölçüm notunda)
# ---------------------------------------------------------------------------

# Simge karakterinin puntosu satırın gövde puntosunun en çok bu katı olur.
# Ölçülen: LaTeX 8/10,9 = 0,73; 7/10 = 0,70; Word dipnot işareti 6,6/11 = 0,60.
KUCUK_PUNTO_ORANI = 0.88
# Üst simge: taban çizgisi gövde puntosunun en az bu katı kadar yukarıda.
# Ölçülen: -0,33 ile -0,40 arası.
UST_SIMGE_KAYMA = 0.18
# Alt simge: taban çizgisi gövde puntosunun en az bu katı kadar aşağıda.
# Ölçülen: +0,15 ile +0,16 arası.
ALT_SIMGE_KAYMA = 0.08
# Bundan büyük dikey kayma başka satırdır, simge değildir.
EN_BUYUK_KAYMA = 0.75
# Taban çizgisinde sayılmak için en çok kayma (gövde puntosu katı).
TABAN_TOLERANSI = 0.08
# Simge, satırdaki bir komşu karaktere (taban) yatayda en çok bu kadar
# uzak olabilir (gövde puntosu katı).
BITISIKLIK_ORANI = 0.6
# İki karakter arası yatay boşluk bunu aşarsa (gövde puntosu katı) araya
# sözcük boşluğu konmuş sayılır.
BOSLUK_ORANI = 0.2
# Yalnız küçük karakterlerden oluşan satır (MuPDF'in ayrı satıra böldüğü
# simge) en çok bu kadar karakterse yanındaki gövde satırına bağlanır.
YALNIZ_SIMGE_SATIRI_EN_COK = 6
# Yığılı yapı (satır içi kesir 2EI/ℓR², üst üste alt+üst simge): simgenin
# yatayda örtüştüğü, dikeyde en çok bu kadar (gövde puntosu katı) uzaktaki
# ZIT sınıflı simge varsa dokunulmaz. Komşu satırlar bu kadar MuPDF satırı
# içinde aranır.
YIGIN_DIKEY_ORANI = 1.2
YIGIN_SATIR_PENCERESI = 3

# İki yanlı bağlam pencereleri: (sol, sağ) normalleştirilmiş karakter. Sırayla
# denenir; ilk benzersiz eşleşme kullanılır. İki yan da şarttır: tek yanlı
# pencere öbür yandaki ayrılığı göremez (ölçüm, 30 Eyl: bir test PDF'indeki
# beyaz gizli "x 1" kopyası ve başka birinde paragraf sonundaki sayfa
# numarası tek yanlı pencereyle yanlış karaktere eşlenmişti).
# Son dört pencerede kısa yan tek karakterdir: uzun yan yeri belirler, kısa
# yan içeriğin öbür komşusunun da aynı olduğunu doğrular (Marker'ın gizli
# metinden ürettiği "." gibi tek karakterlik artıkların yanında gerekir).
BAGLAM_PENCERELERI: Tuple[Tuple[int, int], ...] = (
    (24, 24), (16, 16), (12, 12), (16, 4), (4, 16), (12, 4), (4, 12),
    (8, 8), (10, 3), (3, 10), (20, 1), (1, 20), (14, 1), (1, 14))
# İki yanlı pencerede her yanın en az uzunluğu.
TARAF_EN_AZ = 1
# Tek yanlı pencere (yalnız satır sınırında: içerik bir yanda satırı bitirir
# ya da başlatır, bağlam öbür yanda AYNI satırdadır; iki metinde de).
TEK_YANLI_PENCERELER: Tuple[int, ...] = (24, 16, 12)
# Bağlamın (simgenin kendisi hariç) en az uzunluğu.
EN_KISA_BAGLAM = 12
# PDF'in metin katmanı sayılması için gereken en az karakter.
EN_AZ_PDF_METNI = 200
# Tablo hücresi yeniden sıralamasında en çok parça (permütasyon sayısı 4! = 24).
TABLO_EN_COK_PARCA = 4
TABLO_PARCA_EN_UZUN = 16

# Display matematikte tek harf ikamesi (MT-10: Marker'ın görüntüden okuduğu
# "\frac{\mu}{m^3}", PDF'te "µ/r³"). Denklemin PDF bölgesi, $$…$$ bloğunun
# iki yanındaki metnin bu uzunluklardan ilk benzersiz olanıyla bulunur.
DENKLEM_BAGLAM_UZUNLUKLARI: Tuple[int, ...] = (16, 24, 32, 48)
# Bölge (norm karakter) en çok bu kadar uzun olabilir; daha uzunu başka
# metin içeriyordur.
DENKLEM_BOLGE_EN_UZUN = 300
# Karşılaştırmada en az bu kadar harf AYNEN eşleşmeli (bölge doğrulaması).
DENKLEM_EN_AZ_ESLESEN_HARF = 3
# FX2 (2026-10-05): matematik bölgesi (satır içi $…$ ve display $$…$$) için
# bağlam uzunlukları. Satır içi matematiğin önünde/ardında çoğu zaman kısa
# metin vardır; en kısa pencere yine iki metinde de TEK geçmelidir.
MATEMATIK_BAGLAM_UZUNLUKLARI: Tuple[int, ...] = (10, 12, 16, 24, 32, 48)
# Aralarında bu kadardan (norm karakter) kısa metin olan ardışık matematikler
# tek öbek olarak bulunur, PDF bölgesi aradaki metinle bölünür ("$s=0.24$ and
# $\lambda=0.8$": "and" iki bölgeyi ayırır).
MATEMATIK_OBEK_ARASI = 10
# Ok simgeleri (MT-46): PDF karakteri -> anlam; LaTeX komutu -> anlam; anlam
# -> yazılacak komut. TeX'in uzun okları metin katmanında üst üste binen iki
# glif olarak görünür (\iff = ⇐ + ⇒, \Longrightarrow = "=" + ⇒); bunlar
# birleşik okla eşlenir, tek glif sanılmaz.
OK_KARAKTERLERI = frozenset("←→↔⇐⇒⇔⟵⟶⟷⟸⟹⟺↦⟼")
_BIRLESIK_OK = {("⇐", "⇒"): "⟺", ("⇐", "="): "⟸", ("=", "⇒"): "⟹",
                ("←", "−"): "⟵", ("←", "-"): "⟵", ("−", "→"): "⟶",
                ("-", "→"): "⟶", ("←", "→"): "⟷"}
_LATEX_OK = {
    "leftarrow": "←", "gets": "←", "rightarrow": "→", "to": "→",
    "leftrightarrow": "↔", "Leftarrow": "⇐", "Rightarrow": "⇒",
    "Leftrightarrow": "⇔", "longleftarrow": "⟵", "longrightarrow": "⟶",
    "longleftrightarrow": "⟷", "Longleftarrow": "⟸", "impliedby": "⟸",
    "Longrightarrow": "⟹", "implies": "⟹", "Longleftrightarrow": "⟺",
    "iff": "⟺", "mapsto": "↦", "longmapsto": "⟼",
}
_OK_KOMUTU = {
    "←": r"\leftarrow", "→": r"\rightarrow", "↔": r"\leftrightarrow",
    "⇐": r"\Leftarrow", "⇒": r"\Rightarrow", "⇔": r"\Leftrightarrow",
    "⟵": r"\longleftarrow", "⟶": r"\longrightarrow",
    "⟷": r"\longleftrightarrow", "⟸": r"\Longleftarrow",
    "⟹": r"\Longrightarrow", "⟺": r"\Longleftrightarrow", "↦": r"\mapsto",
    "⟼": r"\longmapsto",
}
# Dokunma: iki glif arasındaki yatay boşluk bundan küçükse (pt; örtüşme
# eksi boşluktur) birleşik glif sayılır. TeX ilişki okunun iki yanına kalın
# boşluk (~2,8 pt) koyar; birleşik okun parçaları ise örtüşür ya da bitişir
# (ölçüm: \iff ⇐ ile ⇒ ~5 pt örtüşür; \mapsto'nun sıfır genişlikli "|"
# çizgisiyle "→" arası 0,3 pt).
OK_DOKUNMA_PT = 0.6
# \mapsto / \longmapsto'nun başındaki dikey çizgi (MathTime "|", CM U+21A6
# parçası) — okla birleşir.
DIKEY_CIZGILER = frozenset("|∣│")
# Sayı belirteci: rakam grupları ve aralarındaki tek ayraç ("0.24", "1,5").
_SAYI_RE = re.compile(r"\d+(?:[.,]\d+)*")
# RAPOR MT-8 (FX2): TeX matematik italiği yazı tipleri (Computer Modern /
# Latin Modern math italic). Bu yazı tipindeki harf yalnız matematikte
# kullanılır; Marker onu Markdown italiği ("d*t*", "*w*") yazınca dönüştürücü
# \textit{} üretiyor ve çevirmen sözcük sırasını bozuyordu.
MATEMATIK_ITALIK_FONT_RE = re.compile(
    r"(?:^|\+)(?:CMMIB?\d*|LMMathItalic\w*|MTMIB?\w*)(?:-|$)", re.IGNORECASE)
# Markdown italiğinde 1–3 ASCII harf: matematik değişkeni adayı. Önünde harf/
# rakam (sözcük içi) olmamalı; yalnız tek yıldız.
# FX2: tablo hücresindeki dipnot işareti. PDF'te işaretin solundaki
# sözcük, aynı görsel satırda işaretin en çok bu kadar (pt) solunda biter.
TABLO_ISARET_SOL_ARALIK = 3.0
# Hücre belirteci: <br> ya da boşlukla ayrılmış parça; dipnot adayı 1–3 rakam.
_HUCRE_BELIRTEC_AYRAC_RE = re.compile(r"(<br\s*/?>|\s+)", re.IGNORECASE)
_DIPNOT_ADAYI_RE = re.compile(r"^\d{1,3}$")
_METIN_SIMGE_RE = re.compile(r"([_^])\{([^{}]*[^\x00-\x7f\u0370-\u03ff′][^{}]*)\}")
_MD_ITALIK_HARF_RE = re.compile(
    r"(?<![*\\A-Za-z0-9])(?P<d>d?)\*(?P<x>[A-Za-z]{1,3})\*(?!\*)(?P<nokta>˙?)")
# Metin katmanı Yunan harflerini Latin koduyla veren (ToUnicode'suz) simge
# yazıtipleri: burada Latin harf güvenilmez (ölçüm, 30 Eyl: bir test
# kitabında SymbolMT'de ρ -> "r", θ -> "q"; başka birinde AdvPSMP10'da η -> "g").
GUVENILMEZ_HARF_FONT_RE = re.compile(
    r"Symbol|Math(?:ematical)?Pi|MTSY|MT-?Extra|AdvPS|Universal|Wingding|Dingbat",
    re.IGNORECASE)
# LaTeX komutu -> harf (karşılaştırma için; bu harfler değiştirilmez).
_LATEX_YUNAN = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ϵ",
    "varepsilon": "ε", "zeta": "ζ", "eta": "η", "theta": "θ",
    "vartheta": "ϑ", "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ",
    "nu": "ν", "xi": "ξ", "pi": "π", "varpi": "ϖ", "rho": "ρ",
    "varrho": "ϱ", "sigma": "σ", "varsigma": "ς", "tau": "τ",
    "upsilon": "υ", "phi": "ϕ", "varphi": "φ", "chi": "χ", "psi": "ψ",
    "omega": "ω", "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ",
    "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ",
    "Psi": "Ψ", "Omega": "Ω", "ell": "ℓ",
}
_LATEX_FONKSIYON = frozenset((
    "sin cos tan cot sec csc arcsin arccos arctan sinh cosh tanh coth log "
    "ln lg exp max min lim det arg sup inf deg dim ker gcd Pr").split())
# İçindeki harfler değişken değil sözcüktür: ikame yapılmaz.
_LATEX_METIN_KOMUTU = frozenset(
    ("text", "textrm", "textit", "textbf", "mathrm", "operatorname",
     "mbox", "hbox", "tag", "label", "mathtt", "texttt"))

# Simge içeriği sınırları. Üst simgede birden çok harf (sıra eki "st",
# "TM", "er") dönüştürücüde matematik italiğine döner; dokunulmaz.
UST_ICERIK_EN_UZUN = 8
UST_ICERIK_EN_COK_HARF = 1
ALT_ICERIK_EN_UZUN = 8
ALT_ICERIK_EN_COK_HARF = 4
SIMGE_IZINLI_ISARET = set("+-=,./()′'*†‡§¶∗∞")
DIPNOT_ISARETI_RE = re.compile(r"^(?:\d{1,3}|[*†‡§¶∗]{1,3})$")

# Derece: TeX'in ^\circ'u (küçük, yukarıda "◦"/"∘").
DERECE_KARAKTERLERI = "◦∘"
DERECE = "°"
# Vektör oku (TeX \vec): U+20D7 BİRLEŞTİRİCİ SAĞA OK.
VEKTOR_OKU = "⃗"
# Computer Modern CMSY/CMBSY: büyük harf yuvaları el yazısıdır (\mathcal).
KALIGRAFIK_FONT_RE = re.compile(r"(?:^|\+)CMB?SY\d+", re.IGNORECASE)
# Görünmez metin: beyaz dolgu (MathType'ın beyaz "x 1" kopyası; bir test PDF'inde)
# ya da tam saydam. Marker bunu metne almaz; akışa da alınmaz.
GORUNMEZ_RENK = 0xFFFFFF

# Karşılaştırmada yok sayılan karakterler (iki tarafta da): boşluklar,
# Markdown biçim işaretleri, köşeli ayraç (bağlantı metni), ters bölü (kaçış),
# vektör oku ve Marker'ın ona verdiği "~", aralıklı aksanlar, madde imleri.
_YOK_SAYILAN = set(
    " \t\r\n      ​‌‍﻿­"
    "*_#|\\[]`~∼" + VEKTOR_OKU + "˙¨ˆ˜´`¯˘ˇ˚•▪■●")
_ESDEGER = {
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-", "―": "-",
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"',
    "∗": "*",
}
# Matematik bölgesi yerine Markdown akışına konan engel (PDF'te yok).
_ENGEL = "\x00"

_ETIKET_RE = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9]*)\b[^<>\n]*>")
# Marker'ın kaçışlı etiket artığı ("<sup>&</sup>lt;sup>1</sup>",
# "&lt;sup>1</sup>"): dönüştürücü (R11) bunu kendisi onarır; içine dokunulmaz,
# akışta engel olur.
_KACISLI_ETIKET_RE = re.compile(
    r"<sup>&</sup>(?:lt|gt|amp);(?:/?su[pb]>)?|&lt;/?su[pb](?:&gt;|>)",
    re.IGNORECASE)
_VARLIK_RE = re.compile(r"&(?:#\d{1,6}|#x[0-9A-Fa-f]{1,5}|[A-Za-z]{2,8});")
_GORSEL_RE = re.compile(r"!\[[^\]\n]*\]\([^)\n]*\)")
_TABLO_AYRAC_RE = re.compile(r"^\s*\|?\s*:?-{3,}")
_LISTE_IMI_RE = re.compile(r"^[ \t]*[-+](?=[ \t])")
# İtalik tek matematik simgesi ("0*,*003", "Bi *<* 0"): Marker matematik
# yazıtipindeki noktalamayı italik sanıyor; çevirmen "\textit{,}" yer
# tutucusunu bozuyor (MT-7).
_ITALIK_SIMGE_RE = re.compile(
    r"(?<![*\\])\*([,.;:<>=≈≤≥±×·−+/])\*(?!\*)")
_ITALIK_SIMGE_ONCE = set("0123456789 \t>(")
_ITALIK_SIMGE_SONRA = set("0123456789 \t<)")

# Vektör: "~r", "˙~r", "¨~r" ya da "h~" (ok harfin sağına düşmüş).
_VEKTOR_ONCE_RE = re.compile(
    r"(?<![A-Za-z0-9~])(?P<aks>[˙¨]?)~(?P<x>[A-Za-z])(?![A-Za-z0-9])")
_VEKTOR_SONRA_RE = re.compile(
    r"(?<![A-Za-z0-9~˙¨])(?P<aks>)(?P<x>[A-Za-z])~(?![A-Za-z0-9~])")
_VEKTOR_AKSAN = {"": None, "˙": "dot", "¨": "ddot"}
# Vektörün / el yazısı harfin hemen ardındaki basit simge etiketi tek
# matematiğe katılır ("~v<sub>⊕</sub>" -> "$\vec{v}_{⊕}$"). Katılamazsa
# dokunulmaz: dönüştürücü etiketi ayrı "${}_{⊕}$" yapar ve "$\vec{v}$${}_{⊕}$"
# içindeki "$$" TeX'te display matematik açar (ölçüm, 30 Eyl: bir test
# kitabında 7 yer). İçerikte TeX özel karakteri, boşluk ya da iç içe
# etiket olmamalı.
_ARDISIK_SIMGE_RE = re.compile(r"<(sup|sub)>([^<>$\\{}&%#_^~\s]{1,4})</\1>")
_SIMGE_ETIKETI_ACILIS = ("<sup>", "<sub>")
_SIMGE_ETIKETI_KAPANIS = ("</sup>", "</sub>")

# Derece (Markdown): isteğe bağlı italik/üst simge sarmalı içinde "◦"/"∘".
# Üst simge sarmalı ya iki ucuyla birlikte alınır ya hiç alınmaz: yalnız
# açılışı yutmak ("<sup>◦C</sup>" -> "°C</sup>") yetim kapanış bırakırdı.
_DERECE_MD_RE = re.compile(
    r"<sup>\s*\*?(?P<d>[" + DERECE_KARAKTERLERI + r"])\*?\s*</sup>"
    r"|(?<!<sup>)\*?(?P<d2>[" + DERECE_KARAKTERLERI + r"])\*?")


# ---------------------------------------------------------------------------
# Normalleştirme
# ---------------------------------------------------------------------------

def _norm_karakter(c: str) -> str:
    """Tek kaynak karakterin karşılaştırma biçimi (boş = yok sayılır)."""
    if c in _YOK_SAYILAN:
        return ""
    c = _ESDEGER.get(c, c)
    n = unicodedata.normalize("NFKC", c)
    out = []
    for ch in n:
        if ch in _YOK_SAYILAN or ch.isspace():
            continue
        if unicodedata.category(ch) == "Mn":
            continue
        out.append(_ESDEGER.get(ch, ch))
    return "".join(out)


# ---------------------------------------------------------------------------
# PDF akışı
# ---------------------------------------------------------------------------

@dataclass
class _PdfAkis:
    """PDF metin katmanı: karakter dizileri + normalleştirilmiş dizgi."""
    ch: List[str] = field(default_factory=list)
    sinif: List[str] = field(default_factory=list)   # N / P (üst) / B (alt)
    sayfa: List[int] = field(default_factory=list)
    font: List[str] = field(default_factory=list)
    satir: List[int] = field(default_factory=list)
    x0: List[float] = field(default_factory=list)
    x1: List[float] = field(default_factory=list)
    y0: List[float] = field(default_factory=list)
    y1: List[float] = field(default_factory=list)
    # Karakterin taban çizgisi (origin y) — FX2 dipnot işareti geometrisi.
    oy: List[float] = field(default_factory=list)
    bosluk_once: List[bool] = field(default_factory=list)
    # Karakter satırın taban çizgisinde mi (|kayma| < TABAN_TOLERANSI)?
    # Etiketi kaldırmak (sup -> düz) yalnız bunda yapılır: aynı puntoda
    # yükseltilmiş metin "N" sınıflanır ama tabanda değildir.
    taban: List[bool] = field(default_factory=list)
    norm: str = ""
    kaynak: List[int] = field(default_factory=list)  # norm indeksi -> ch indeksi
    sayfa_sayisi: int = 0
    # MuPDF satırı -> (ilk, son) ham karakter indeksi; satır gövde puntosu.
    satir_araligi: Dict[int, Tuple[int, int]] = field(default_factory=dict)
    satir_puntosu: Dict[int, float] = field(default_factory=dict)


def _satir_referansi(items: Sequence[tuple]) -> Optional[Tuple[float, float]]:
    """Satırın gövde puntosu ve taban çizgisi (en çok karakterli punto)."""
    sayim: Dict[float, int] = {}
    for c, size, *_ in items:
        if c.strip():
            k = round(size, 1)
            sayim[k] = sayim.get(k, 0) + 1
    if not sayim:
        return None
    ref = max(sayim.items(), key=lambda kv: (kv[1], kv[0]))[0]
    tabanlar = sorted(it[3] for it in items
                      if it[0].strip() and abs(round(it[1], 1) - ref) < 0.05)
    return ref, tabanlar[len(tabanlar) // 2]


def _gorunmez_mi(span: dict) -> bool:
    """Span görünmez mi (beyaz dolgu ya da tam saydam)?"""
    try:
        if int(span.get("alpha", 255)) == 0:
            return True
        return int(span.get("color", 0)) == GORUNMEZ_RENK
    except (TypeError, ValueError):
        return False


# 6 Eki 2026: aynı PDF'in akışı bir dönüşümde birden çok kez isteniyor (simge
# onarımı, vurgu onarımı, eş aralıklı parça hizalaması, bağlantı metni; büyük
# bir test kitabında her kurulum ~10 sn). Akış kurulduktan sonra salt okunurdur (yalnız
# _sayfa_ekle/_normlestir yazar); son PDF'in akışı saklanır.
_AKIS_ONBELLEGI: Dict[tuple, Optional["_PdfAkis"]] = {}


def _pdf_akisi_kur(pdf_path: str) -> Optional[_PdfAkis]:
    """PDF'in karakter akışı (önbellekli: yol + değişiklik zamanı + boyut)."""
    import os
    try:
        st = os.stat(pdf_path)
        anahtar = (os.path.abspath(pdf_path), st.st_mtime_ns, st.st_size)
    except OSError:
        return _pdf_akisi_kur_ham(pdf_path)
    if anahtar not in _AKIS_ONBELLEGI:
        _AKIS_ONBELLEGI.clear()
        _AKIS_ONBELLEGI[anahtar] = _pdf_akisi_kur_ham(pdf_path)
    return _AKIS_ONBELLEGI[anahtar]


def _pdf_akisi_kur_ham(pdf_path: str) -> Optional[_PdfAkis]:
    """PDF'in karakter akışını kur; açılamaz ya da metinsizse None."""
    try:
        import fitz  # PyMuPDF
    except ImportError:  # pragma: no cover - venv'de kurulu
        logger.warning("G1c: PyMuPDF yok; PDF metin onarımı atlandı")
        return None
    try:
        doc = fitz.open(str(pdf_path))
    except Exception as exc:  # noqa: BLE001 - bozuk PDF: Markdown aynen
        logger.info("G1c: PDF açılamadı (%s): %s", pdf_path, exc)
        return None
    akis = _PdfAkis()
    try:
        if getattr(doc, "needs_pass", False):
            return None
        akis.sayfa_sayisi = len(doc)
        flags = getattr(fitz, "TEXTFLAGS_RAWDICT", 0) & ~getattr(
            fitz, "TEXT_PRESERVE_IMAGES", 0)
        satir_no = 0
        for pno, page in enumerate(doc):
            try:
                rd = page.get_text("rawdict", flags=flags)
            except Exception:  # noqa: BLE001 - tek sayfa okunamazsa atla
                continue
            satirlar = []
            for blk in rd.get("blocks", []):
                if blk.get("type") != 0:
                    continue
                for ln in blk.get("lines", []):
                    items = []
                    for sp in ln.get("spans", []):
                        if _gorunmez_mi(sp):
                            continue
                        size = float(sp.get("size", 0.0))
                        font = str(sp.get("font", ""))
                        for c in sp.get("chars", []):
                            bx = c.get("bbox", (0, 0, 0, 0))
                            org = c.get("origin", (bx[0], bx[3]))
                            items.append((c.get("c", ""), size, float(org[0]),
                                          float(org[1]), float(bx[0]),
                                          float(bx[2]), font, float(bx[1]),
                                          float(bx[3])))
                    if items:
                        yatay = tuple(round(v, 2) for v in ln.get("dir", (1, 0)))
                        satirlar.append((yatay == (1.0, 0.0),
                                         tuple(ln.get("bbox", (0, 0, 0, 0))),
                                         items))
            _sayfa_ekle(akis, pno, satirlar, satir_no)
            satir_no += len(satirlar)
    finally:
        doc.close()
    _normlestir(akis)
    if len(akis.norm) < EN_AZ_PDF_METNI:
        logger.info("G1c: PDF'te metin katmanı yok (%d karakter): %s",
                    len(akis.norm), pdf_path)
        return None
    return akis


def _sayfa_ekle(akis: _PdfAkis, pno: int, satirlar: list, satir_no: int) -> None:
    """Bir sayfanın satırlarını sınıflandırıp akışa ekle."""
    refs: List[Optional[Tuple[float, float]]] = [
        _satir_referansi(items) if yatay else None
        for yatay, _, items in satirlar]
    # Sayfa gövde puntosu (karakter sayısıyla ağırlıklı en yaygın punto).
    sayim: Dict[float, int] = {}
    for yatay, _, items in satirlar:
        for it in items:
            if it[0].strip():
                k = round(it[1], 1)
                sayim[k] = sayim.get(k, 0) + 1
    govde = max(sayim.items(), key=lambda kv: kv[1])[0] if sayim else 0.0
    # Yalnız küçük karakterli kısa satır: yanındaki gövde satırına bağlan.
    for i, (yatay, bbox, items) in enumerate(satirlar):
        ref = refs[i]
        if ref is None or not yatay:
            continue
        dolu = sum(1 for it in items if it[0].strip())
        if dolu > YALNIZ_SIMGE_SATIRI_EN_COK or ref[0] >= KUCUK_PUNTO_ORANI * govde:
            continue
        en_iyi = None
        for j, (yatay2, bbox2, items2) in enumerate(satirlar):
            ref2 = refs[j]
            if j == i or ref2 is None or not yatay2:
                continue
            if ref[0] > KUCUK_PUNTO_ORANI * ref2[0]:
                continue
            ust = max(bbox[1], bbox2[1])
            alt = min(bbox[3], bbox2[3])
            if alt - ust <= 0:
                continue
            aralik = max(bbox2[0] - bbox[2], bbox[0] - bbox2[2], 0.0)
            if aralik > BITISIKLIK_ORANI * ref2[0]:
                continue
            if en_iyi is None or aralik < en_iyi[0]:
                en_iyi = (aralik, ref2)
        refs[i] = en_iyi[1] if en_iyi else None

    for i, (yatay, bbox, items) in enumerate(satirlar):
        ref = refs[i]
        siniflar = ["N"] * len(items)
        if ref is not None:
            rsize, rbase = ref
            for k, it in enumerate(items):
                c, size, ox, oy = it[0], it[1], it[2], it[3]
                if not c.strip() or "GlyphLess" in it[6]:
                    continue
                if size > KUCUK_PUNTO_ORANI * rsize or rsize <= 0:
                    continue
                dy = (oy - rbase) / rsize
                if abs(dy) > EN_BUYUK_KAYMA:
                    continue
                if dy <= -UST_SIMGE_KAYMA:
                    siniflar[k] = "P"
                elif dy >= ALT_SIMGE_KAYMA:
                    siniflar[k] = "B"
            # Simge, satırdaki bir tam boy komşuya bitişik olmalı.
            _bitisiklik_denetle(items, siniflar, rsize)
        onceki_x1 = None
        if items:
            ilk = len(akis.ch)
            akis.satir_araligi[satir_no + i] = (ilk, ilk + len(items) - 1)
            akis.satir_puntosu[satir_no + i] = ref[0] if ref else govde
        for k, it in enumerate(items):
            c = it[0]
            bosluk = False
            if k == 0:
                bosluk = True
            elif onceki_x1 is not None and ref is not None:
                bosluk = (it[4] - onceki_x1) > BOSLUK_ORANI * ref[0]
            if k > 0 and not items[k - 1][0].strip():
                bosluk = True
            akis.ch.append(c)
            akis.sinif.append(siniflar[k])
            akis.sayfa.append(pno)
            akis.font.append(it[6])
            akis.satir.append(satir_no + i)
            akis.x0.append(it[4])
            akis.x1.append(it[5])
            akis.y0.append(it[7])
            akis.y1.append(it[8])
            akis.oy.append(it[3])
            akis.bosluk_once.append(bosluk)
            akis.taban.append(ref is not None and ref[0] > 0 and abs(
                (it[3] - ref[1]) / ref[0]) < TABAN_TOLERANSI)
            if c.strip():
                onceki_x1 = it[5]


def _bitisiklik_denetle(items: Sequence[tuple], siniflar: List[str],
                        rsize: float) -> None:
    """Simge grubunun iki yanında da tam boy karakter uzaksa sınıfı sil."""
    n = len(items)
    k = 0
    while k < n:
        if siniflar[k] not in ("P", "B"):
            k += 1
            continue
        s = k
        while k < n and (siniflar[k] == siniflar[s] or not items[k][0].strip()):
            k += 1
        e = k  # [s, e)
        while e > s and not items[e - 1][0].strip():
            e -= 1
        sol = s - 1
        while sol >= 0 and not items[sol][0].strip():
            sol -= 1
        sag = e
        while sag < n and not items[sag][0].strip():
            sag += 1
        yakin = False
        if sol >= 0 and siniflar[sol] == "N":
            yakin = items[s][4] - items[sol][5] <= BITISIKLIK_ORANI * rsize
        if not yakin and sag < n and siniflar[sag] == "N":
            yakin = items[sag][4] - items[e - 1][5] <= BITISIKLIK_ORANI * rsize
        if not yakin:
            for t in range(s, e):
                siniflar[t] = "N"


def _yigili_mi(pdf: _PdfAkis, k0: int, k1: int, sinif: str) -> bool:
    """Ham karakter aralığı [k0, k1] yığılı bir yapının parçası mı?

    Yatayda örtüşen, dikeyde yakın ve ZIT sınıflı (P <-> B) bir simge varsa
    evet: satır içi kesirde pay "P", payda "B" sınıflanır; üst üste alt+üst
    simgede de ikisi aynı x'tedir. Bunlar simge değil yapı bilgisidir.
    """
    if sinif not in ("P", "B"):
        return False
    zit = "B" if sinif == "P" else "P"
    x0 = min(pdf.x0[k] for k in range(k0, k1 + 1))
    x1 = max(pdf.x1[k] for k in range(k0, k1 + 1))
    yo = (min(pdf.y0[k] for k in range(k0, k1 + 1))
          + max(pdf.y1[k] for k in range(k0, k1 + 1))) / 2.0
    satir = pdf.satir[k0]
    boy = pdf.satir_puntosu.get(satir, 0.0) or 10.0
    for s2 in range(satir - YIGIN_SATIR_PENCERESI, satir + YIGIN_SATIR_PENCERESI + 1):
        aralik = pdf.satir_araligi.get(s2)
        if aralik is None:
            continue
        for t in range(aralik[0], aralik[1] + 1):
            if pdf.sinif[t] != zit or pdf.sayfa[t] != pdf.sayfa[k0] \
                    or not pdf.ch[t].strip():
                continue
            if pdf.x0[t] < x1 - 0.5 and pdf.x1[t] > x0 + 0.5 and abs(
                    (pdf.y0[t] + pdf.y1[t]) / 2.0 - yo) <= YIGIN_DIKEY_ORANI * boy:
                return True
    return False


def _normlestir(akis: _PdfAkis) -> None:
    """Normalleştirilmiş dizgiyi kur; satır sonu tirelerini yok say."""
    n = len(akis.ch)
    # Satır sonu tiresi + sonraki satır küçük harfle başlıyorsa tire yok
    # sayılır (Marker da aynı kuralla birleştirir).
    tire_yok = set()
    son_dolu: Dict[int, int] = {}
    ilk_dolu: Dict[int, int] = {}
    for i, c in enumerate(akis.ch):
        if c.strip():
            s = akis.satir[i]
            son_dolu[s] = i
            ilk_dolu.setdefault(s, i)
    for s, i in son_dolu.items():
        if akis.ch[i] in ("-", "­", "‐"):
            j = ilk_dolu.get(s + 1)
            if j is not None and akis.ch[j].islower():
                tire_yok.add(i)
    parcalar: List[str] = []
    kaynak: List[int] = []
    for i in range(n):
        if i in tire_yok:
            continue
        t = _norm_karakter(akis.ch[i])
        for ch in t:
            parcalar.append(ch)
            kaynak.append(i)
    akis.norm = "".join(parcalar)
    akis.kaynak = kaynak


# ---------------------------------------------------------------------------
# Markdown akışı
# ---------------------------------------------------------------------------

@dataclass
class _MdAkis:
    norm: str = ""
    bas: List[int] = field(default_factory=list)    # md başlangıç ofseti
    son: List[int] = field(default_factory=list)    # md bitiş ofseti
    sinif: List[str] = field(default_factory=list)  # N / P / B / M (matematik)
    etiket: List[int] = field(default_factory=list)  # etiket no ya da -1
    satir: List[int] = field(default_factory=list)   # Markdown satır numarası
    # etiket no -> (açılış_baş, açılış_son, kapanış_baş, kapanış_son, tür)
    etiketler: List[Tuple[int, int, int, int, str]] = field(default_factory=list)


def _md_akisi_kur(md: str) -> _MdAkis:
    """Markdown'ı karşılaştırma akışına çevir (biçim işaretleri atılır)."""
    ak = _MdAkis()
    parcalar: List[str] = []
    n = len(md)
    i = 0
    yigin: List[Tuple[str, int, int]] = []  # (tür, açılış_baş, açılış_son)
    acik_etiket: List[int] = []             # yığındaki etiketlerin numarası
    satir_basi = True
    kod_blogu = False
    satir_no = [0]

    def ekle(ch: str, bas: int, son: int, sinif: str, etiket: int) -> None:
        parcalar.append(ch)
        ak.bas.append(bas)
        ak.son.append(son)
        ak.sinif.append(sinif)
        ak.etiket.append(etiket)
        ak.satir.append(satir_no[0])

    while i < n:
        c = md[i]
        if c == "\n":
            # Kapanmamış etiketler satır sonunda düşer.
            yigin.clear()
            acik_etiket.clear()
            satir_basi = True
            satir_no[0] += 1
            i += 1
            continue
        if satir_basi:
            satir_basi = False
            son = md.find("\n", i)
            son = n if son < 0 else son
            satir = md[i:son]
            if satir.lstrip().startswith("```"):
                kod_blogu = not kod_blogu
                ekle(_ENGEL, i, son, "M", -1)
                i = son
                continue
            if kod_blogu or _TABLO_AYRAC_RE.match(satir):
                if kod_blogu:
                    ekle(_ENGEL, i, son, "M", -1)
                i = son
                continue
            m = _LISTE_IMI_RE.match(satir)
            if m:
                i += m.end()
                continue
        if c == "$":
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
                ekle(_ENGEL, i, j, "M", -1)
                satir_no[0] += md.count("\n", i, j)
                i = j
                continue
        if c == "!" and md.startswith("![", i):
            m = _GORSEL_RE.match(md, i)
            if m:
                ekle(_ENGEL, i, m.end(), "M", -1)
                i = m.end()
                continue
        if c == "]" and md.startswith("](", i):
            # Bağlantı adresi: kapanan ")" dengesiyle atla.
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
            # Kapanmayan adreste ("(https://…/Ad_(meslek)." kaynakça satırı)
            # satır sonu yutulmaz; yutulunca satır sayacı kayıyordu (5 Eki).
            i = j + 1 if j < n and md[j] == ")" else j
            continue
        if c in "<&":
            m = _KACISLI_ETIKET_RE.match(md, i)
            if m:
                ekle(_ENGEL, i, m.end(), "M", -1)
                i = m.end()
                continue
        if c == "<":
            m = _ETIKET_RE.match(md, i)
            if m:
                ad = m.group(2).lower()
                kapanis = m.group(1) == "/"
                if ad in ("sup", "sub"):
                    if not kapanis:
                        yigin.append((ad, m.start(), m.end()))
                        ak.etiketler.append((m.start(), m.end(), -1, -1, ad))
                        acik_etiket.append(len(ak.etiketler) - 1)
                    elif yigin and yigin[-1][0] == ad:
                        yigin.pop()
                        no = acik_etiket.pop()
                        a0, a1, _, _, tur = ak.etiketler[no]
                        ak.etiketler[no] = (a0, a1, m.start(), m.end(), tur)
                i = m.end()
                continue
        if c == "&":
            m = _VARLIK_RE.match(md, i)
            if m:
                cozum = html.unescape(m.group(0))
                if cozum != m.group(0):
                    sinif = _yigin_sinifi(yigin)
                    et = acik_etiket[-1] if acik_etiket else -1
                    for ch in _norm_karakter(cozum) if len(cozum) == 1 else "".join(
                            _norm_karakter(x) for x in cozum):
                        ekle(ch, i, m.end(), sinif, et)
                    i = m.end()
                    continue
        t = _norm_karakter(c)
        if t:
            sinif = _yigin_sinifi(yigin)
            et = acik_etiket[-1] if acik_etiket else -1
            for ch in t:
                ekle(ch, i, i + 1, sinif, et)
        i += 1
    ak.norm = "".join(parcalar)
    return ak


def _yigin_sinifi(yigin: Sequence[Tuple[str, int, int]]) -> str:
    if not yigin:
        return "N"
    return "P" if yigin[-1][0] == "sup" else "B"


# ---------------------------------------------------------------------------
# Bağlam eşleme
# ---------------------------------------------------------------------------

def _benzersiz_mi(metin: str, w: str, beklenen: int) -> bool:
    return metin.find(w) == beklenen and metin.find(w, beklenen + 1) < 0


def _tek_bul(metin: str, w: str) -> Optional[int]:
    """w metinde tam bir kez geçiyorsa yeri, değilse None."""
    j = metin.find(w)
    if j < 0 or metin.find(w, j + 1) >= 0:
        return None
    return j


def _tek_satir(satir: Callable[[int], int], bas: int, son: int) -> bool:
    """Norm aralığı [bas, son) tek satırda mı?"""
    s0 = satir(bas)
    return all(satir(t) == s0 for t in range(bas + 1, son))


def _satir_sonu_mu(satir: Callable[[int], int], n: int, t: int) -> bool:
    """t, bir satırın ilk karakteri (ya da metnin sonu) mu? (t-1 ile t arası
    satır sınırı)."""
    return t <= 0 or t >= n or satir(t) != satir(t - 1)


def _hizala(src: str, dst: str, a: int, b: int,
            src_satir: Callable[[int], int],
            dst_satir: Callable[[int], int]) -> Optional[int]:
    """src norm aralığı [a, b) için dst'deki başlangıç (benzersiz bağlam).

    1) İki yanlı pencere: her yanda en az TARAF_EN_AZ karakter, bağlam iki
       metinde de tam bir kez geçer. İlk tutan pencere kullanılır.
    2) Olmazsa tek yanlı pencere, yalnız satır sınırında: içerik src'de
       satırı bitiriyorsa sol bağlam, başlatıyorsa sağ bağlam kullanılır;
       bağlam + içerik iki metinde de tek satırdadır ve öbür yan iki metinde
       de satır sınırıdır. İki yön de tutup ayrı yer gösterirse belirsiz.
    """
    ns, nd = len(src), len(dst)
    for sol, sag in BAGLAM_PENCERELERI:
        wa, wb = max(0, a - sol), min(ns, b + sag)
        if a - wa < TARAF_EN_AZ or wb - b < TARAF_EN_AZ \
                or (a - wa) + (wb - b) < EN_KISA_BAGLAM:
            continue
        w = src[wa:wb]
        if _ENGEL in w:
            continue
        j = _tek_bul(dst, w)
        if j is None or not _benzersiz_mi(src, w, wa):
            continue
        return j + (a - wa)
    bulunan = set()
    for uzun in TEK_YANLI_PENCERELER:
        # Sol bağlam: içerik satır sonunda.
        if a - uzun >= 0 and _satir_sonu_mu(src_satir, ns, b) \
                and _tek_satir(src_satir, a - uzun, b):
            w = src[a - uzun:b]
            j = None if _ENGEL in w else _tek_bul(dst, w)
            if j is not None and _benzersiz_mi(src, w, a - uzun) \
                    and _tek_satir(dst_satir, j, j + len(w)) \
                    and _satir_sonu_mu(dst_satir, nd, j + len(w)):
                bulunan.add(j + uzun)
                break
    for uzun in TEK_YANLI_PENCERELER:
        # Sağ bağlam: içerik satır başında.
        if b + uzun <= ns and _satir_sonu_mu(src_satir, ns, a) \
                and _tek_satir(src_satir, a, b + uzun):
            w = src[a:b + uzun]
            j = None if _ENGEL in w else _tek_bul(dst, w)
            if j is not None and _benzersiz_mi(src, w, a) \
                    and _tek_satir(dst_satir, j, j + len(w)) \
                    and _satir_sonu_mu(dst_satir, nd, j):
                bulunan.add(j)
                break
    if len(bulunan) == 1:
        return bulunan.pop()
    return None


def _pdf_satir_fn(pdf: _PdfAkis) -> Callable[[int], int]:
    return lambda i: pdf.satir[pdf.kaynak[i]]


def _md_satir_fn(md: _MdAkis) -> Callable[[int], int]:
    return lambda x: md.satir[x]


def _pdf_den_md_ye(pdf: _PdfAkis, md: _MdAkis, a: int, b: int
                   ) -> Optional[int]:
    """PDF norm aralığı [a, b) için Markdown norm başlangıcı (benzersiz)."""
    return _hizala(pdf.norm, md.norm, a, b, _pdf_satir_fn(pdf),
                   _md_satir_fn(md))


def _md_den_pdf_e(pdf: _PdfAkis, md: _MdAkis, a: int, b: int
                  ) -> Optional[int]:
    """Markdown norm aralığı [a, b) için PDF norm başlangıcı (benzersiz)."""
    return _hizala(md.norm, pdf.norm, a, b, _md_satir_fn(md),
                   _pdf_satir_fn(pdf))


# ---------------------------------------------------------------------------
# Rapor
# ---------------------------------------------------------------------------

@dataclass
class Degisiklik:
    tur: str
    md_bas: int              # o geçişteki Markdown'da ofset
    md_son: int
    once: str
    sonra: str
    sayfa: int = -1          # PDF sayfası (0 tabanlı)
    pdf_idx: int = -1        # PDF karakter indeksi (doğrulama görseli için)
    pdf_son: int = -1
    baglam_sol: str = ""     # düzenlemeden önceki 30 karakter (rapor için)
    baglam_sag: str = ""
    satir: int = 0           # o geçişteki Markdown satır numarası


@dataclass
class OnarimRaporu:
    pdf_var: bool = False
    degisiklikler: List[Degisiklik] = field(default_factory=list)
    atlanan: Dict[str, int] = field(default_factory=dict)

    def atla(self, neden: str) -> None:
        self.atlanan[neden] = self.atlanan.get(neden, 0) + 1

    def sayim(self) -> Dict[str, int]:
        s: Dict[str, int] = {}
        for d in self.degisiklikler:
            s[d.tur] = s.get(d.tur, 0) + 1
        return s


# ---------------------------------------------------------------------------
# Düzenleme uygulama
# ---------------------------------------------------------------------------

def _uygula(md: str, duzenlemeler: List[Tuple[int, int, str, Degisiklik]],
            rapor: OnarimRaporu) -> str:
    """Çakışmayan düzenlemeleri sondan başa uygula (çakışan ikisi de düşer)."""
    duzenlemeler.sort(key=lambda d: (d[0], d[1]))
    secilen: List[Tuple[int, int, str, Degisiklik]] = []
    for d in duzenlemeler:
        if secilen and d[0] < secilen[-1][1]:
            if (d[0], d[1], d[2]) == secilen[-1][:3]:
                continue  # aynı düzenleme iki yoldan bulunmuş
            rapor.atla("cakisan_duzenleme")
            secilen.pop()
            continue
        secilen.append(d)
    parcalar = []
    son = len(md)
    for a, b, yeni, deg in reversed(secilen):
        deg.baglam_sol = md[max(0, a - 30):a]
        deg.baglam_sag = md[b:b + 30]
        deg.satir = md.count("\n", 0, a) + 1
        parcalar.append(md[b:son])
        parcalar.append(yeni)
        son = a
        rapor.degisiklikler.append(deg)
    parcalar.append(md[:son])
    return "".join(reversed(parcalar))


# ---------------------------------------------------------------------------
# 1) İtalik tek matematik simgesi
# ---------------------------------------------------------------------------

def _italik_simge_duzelt(md: str, rapor: OnarimRaporu) -> str:
    duz: List[Tuple[int, int, str, Degisiklik]] = []
    for m in _ITALIK_SIMGE_RE.finditer(md):
        once = md[m.start() - 1] if m.start() > 0 else " "
        sonra = md[m.end()] if m.end() < len(md) else " "
        if once not in _ITALIK_SIMGE_ONCE or sonra not in _ITALIK_SIMGE_SONRA:
            continue
        duz.append((m.start(), m.end(), m.group(1), Degisiklik(
            "italik_simge", m.start(), m.end(), m.group(0), m.group(1))))
    return _uygula(md, duz, rapor) if duz else md


# ---------------------------------------------------------------------------
# 2) Üst / alt simge
# ---------------------------------------------------------------------------

def _icerik_uygun(icerik: str, sinif: str) -> bool:
    """Simge içeriği kurala uygun mu (norm biçim)?"""
    if not icerik:
        return False
    harf = sum(1 for ch in icerik if ch.isalpha())
    for ch in icerik:
        if not (ch.isalnum() or ch in SIMGE_IZINLI_ISARET):
            return False
    if sinif == "P":
        if len(icerik) > UST_ICERIK_EN_UZUN:
            return False
        return harf <= UST_ICERIK_EN_COK_HARF
    if len(icerik) > ALT_ICERIK_EN_UZUN:
        return False
    return harf <= ALT_ICERIK_EN_COK_HARF


def _pdf_simge_kosulari(pdf: _PdfAkis) -> List[Tuple[int, int, str]]:
    """PDF norm dizgisindeki simge koşuları: (norm_baş, norm_son, sınıf)."""
    kosular = []
    P = pdf.norm
    i = 0
    n = len(P)
    while i < n:
        k = pdf.kaynak[i]
        s = pdf.sinif[k]
        if s not in ("P", "B") or pdf.ch[k] in DERECE_KARAKTERLERI:
            i += 1
            continue
        j = i + 1
        while j < n:
            kj = pdf.kaynak[j]
            if pdf.sinif[kj] != s or pdf.satir[kj] != pdf.satir[k] \
                    or pdf.ch[kj] in DERECE_KARAKTERLERI:
                break
            # Arada norm'da yok sayılan ama görünen bir karakter ("]", "|")
            # varsa koşu orada biter: "(ONO₂)₃]ₙ" -> "3" ve "n" ayrı koşular.
            if any(pdf.ch[t].strip() for t in range(pdf.kaynak[j - 1] + 1, kj)):
                break
            j += 1
        kosular.append((i, j, s))
        i = j
    return kosular


def _simge_duzelt(md: str, pdf: _PdfAkis, rapor: OnarimRaporu) -> str:
    ak = _md_akisi_kur(md)
    M = ak.norm
    duz: List[Tuple[int, int, str, Degisiklik]] = []
    islenen_etiket = set()

    def pdf_bosluk(pi: int) -> bool:
        return pdf.bosluk_once[pdf.kaynak[pi]]

    etiket_mi: Dict[int, List[int]] = {}
    for x, no in enumerate(ak.etiket):
        if no >= 0:
            etiket_mi.setdefault(no, []).append(x)

    # --- A) PDF'teki simge koşuları (düz metne inmiş ya da yanlış etiketli) --
    for a, b, s in _pdf_simge_kosulari(pdf):
        icerik = pdf.norm[a:b]
        if not _icerik_uygun(icerik, s):
            rapor.atla("icerik_kurala_uymuyor")
            continue
        # Yan yana zıt sınıflı koşu (kesir, üst üste simge): belirsiz.
        if (a > 0 and pdf.sinif[pdf.kaynak[a - 1]] in ("P", "B")
                and pdf.satir[pdf.kaynak[a - 1]] == pdf.satir[pdf.kaynak[a]]) or \
                (b < len(pdf.norm) and pdf.sinif[pdf.kaynak[b]] in ("P", "B")
                 and pdf.satir[pdf.kaynak[b]] == pdf.satir[pdf.kaynak[a]]):
            rapor.atla("bitisik_zit_simge")
            continue
        if _yigili_mi(pdf, pdf.kaynak[a], pdf.kaynak[b - 1], s):
            rapor.atla("yigili_yapi")
            continue
        j = _pdf_den_md_ye(pdf, ak, a, b)
        if j is None:
            rapor.atla("baglam_bulunamadi")
            continue
        mi = list(range(j, j + (b - a)))
        siniflar = {ak.sinif[x] for x in mi}
        if siniflar == {s}:
            continue  # zaten doğru
        if "M" in siniflar:
            continue  # Marker matematiği: dokunulmaz
        et = {ak.etiket[x] for x in mi}
        if siniflar == {"N"}:
            yeni = _sarma_duzenlemesi(md, ak, mi, s, pdf, a, b, icerik, rapor)
            if yeni is not None:
                duz.append(yeni)
            continue
        if len(et) == 1 and -1 not in et:
            no = et.pop()
            # Etiket içeriğinin tamamı koşuya denk mi?
            if etiket_mi.get(no) == mi:
                a0, a1, k0, _k1, _t = ak.etiketler[no]
                if _unicode_simge_var(md[a1:k0]):
                    rapor.atla("zaten_unicode_simge")
                    continue
                d = _etiket_degistir(md, ak, no, s, pdf, a)
                if d:
                    duz.append(d)
                    islenen_etiket.add(no)
                continue
        yeni = _karma_sarma(md, ak, mi, s, pdf, a, b, icerik, etiket_mi, rapor)
        if yeni is not None:
            duz.append(yeni)
            islenen_etiket.update(ak.etiket[x] for x in mi if ak.etiket[x] >= 0)

    # --- B) Markdown'daki etiketler (PDF'te taban çizgisindeyse aç) ---------
    for no, mi in etiket_mi.items():
        if no in islenen_etiket:
            continue
        a0, a1, k0, k1, tur = ak.etiketler[no]
        if k0 < 0:
            continue
        if any(ak.sinif[x] == "M" for x in mi) or mi != list(range(mi[0], mi[-1] + 1)):
            continue
        pj = _md_den_pdf_e(pdf, ak, mi[0], mi[-1] + 1)
        if pj is None:
            rapor.atla("etiket_baglami_bulunamadi")
            continue
        psin = {pdf.sinif[pdf.kaynak[pj + t]] for t in range(len(mi))}
        hedef = "P" if tur == "sup" else "B"
        if psin == {hedef}:
            continue
        if psin == {"N"}:
            # Derece ◦ taban çizgisinde değil; burada yalnız gerçek düz metin.
            icerik_md = md[a1:k0]
            if not all(pdf.taban[pdf.kaynak[pj + t]] for t in range(len(mi))):
                rapor.atla("taban_cizgisinde_degil")
                continue
            # Dipnot tanımı ("<sup>2</sup> Metin", satır başında): PDF'te
            # numara taban çizgisinde olsa da etiket kalır; dönüştürücü
            # (_pair_page_footnotes) dipnotu bu biçimden eşler.
            satir_bas = md.rfind("\n", 0, a0) + 1
            sonraki = pj + len(mi)
            if tur == "sup" and DIPNOT_ISARETI_RE.match(_norm_birlestir(icerik_md)) \
                    and not md[satir_bas:a0].strip() \
                    and _satir_sonu_mu(_pdf_satir_fn(pdf), len(pdf.norm), pj) \
                    and (sonraki >= len(pdf.norm) or pdf_bosluk(sonraki)):
                rapor.atla("dipnot_tanimi_korundu")
                continue
            yeni = icerik_md
            once = ""
            sonra = ""
            if pdf.bosluk_once[pdf.kaynak[pj]] and a0 > 0 \
                    and not md[a0 - 1].isspace() and md[a0 - 1] not in "*_(":
                once = " "
            sonraki = pj + len(mi)
            if sonraki < len(pdf.norm) and pdf_bosluk(sonraki) \
                    and pdf.satir[pdf.kaynak[sonraki]] == pdf.satir[pdf.kaynak[pj]] \
                    and k1 < len(md) and not md[k1].isspace() \
                    and md[k1] not in "*_).,;:!?":
                sonra = " "
            yeni = once + icerik_md + sonra
            duz.append((a0, k1, yeni, Degisiklik(
                "ust_simge_duz_metin" if tur == "sup" else "alt_simge_duz_metin",
                a0, k1, md[a0:k1], yeni, pdf.sayfa[pdf.kaynak[pj]],
                pdf.kaynak[pj], pdf.kaynak[pj + len(mi) - 1])))
            continue
        if psin in ({"P"}, {"B"}):
            hedef_sinif = next(iter(psin))
            if _yigili_mi(pdf, pdf.kaynak[pj], pdf.kaynak[pj + len(mi) - 1],
                          hedef_sinif):
                rapor.atla("yigili_yapi")
                continue
            d = _etiket_degistir(md, ak, no, hedef_sinif, pdf, pj)
            if d:
                duz.append(d)
            continue
        rapor.atla("etiket_karisik_sinif")
    return _uygula(md, duz, rapor) if duz else md


def _etiket_degistir(md: str, ak: _MdAkis, no: int, hedef_sinif: str,
                     pdf: _PdfAkis, pa: int
                     ) -> Optional[Tuple[int, int, str, Degisiklik]]:
    a0, a1, k0, k1, tur = ak.etiketler[no]
    if k0 < 0:
        return None
    yeni_tur = "sup" if hedef_sinif == "P" else "sub"
    if yeni_tur == tur:
        return None
    yeni = f"<{yeni_tur}>{md[a1:k0]}</{yeni_tur}>"
    kind = "ust_simge_alt_simge" if tur == "sup" else "alt_simge_ust_simge"
    k = pdf.kaynak[pa]
    return (a0, k1, yeni, Degisiklik(kind, a0, k1, md[a0:k1], yeni,
                                     pdf.sayfa[k], k, k))


def _unicode_simge_var(metin: str) -> bool:
    """Metinde Unicode üst/alt simge karakteri (², ³, ¹, ⁰-₉) var mı?
    Bunlar zaten simge gösterimidir; <sup>/<sub> içine alınmaz (çift simge)."""
    for ch in metin:
        d = unicodedata.decomposition(ch)
        if d.startswith("<super>") or d.startswith("<sub>"):
            return True
    return False


def _sarma_duzenlemesi(md: str, ak: _MdAkis, mi: List[int], s: str,
                       pdf: _PdfAkis, a: int, b: int, icerik: str,
                       rapor: OnarimRaporu
                       ) -> Optional[Tuple[int, int, str, Degisiklik]]:
    """Düz metindeki simge karakterlerini <sup>/<sub> içine al."""
    bas = ak.bas[mi[0]]
    son = ak.son[mi[-1]]
    ara = md[bas:son]
    # Arada biçim işareti ya da etiket olmamalı (yalnız karakterler ve boşluk).
    duz_ara = "".join(ch for ch in ara if not ch.isspace())
    if _norm_birlestir(duz_ara) != icerik or "<" in ara \
            or "_" in ara or "$" in ara or "\\" in ara:
        rapor.atla("bicim_isaretli_simge")
        return None
    # Aradaki her görünür karakter içeriğin parçası olmalı (vurgu "*" hariç):
    # yok sayılan "]", "|", "`" simgenin içine alınmaz.
    if any(not _norm_karakter(ch) for ch in duz_ara if ch != "*"):
        rapor.atla("bicim_isaretli_simge")
        return None
    if _unicode_simge_var(ara):
        rapor.atla("zaten_unicode_simge")
        return None
    tag = "sup" if s == "P" else "sub"
    if "*" in ara:
        # Vurgu işareti simgeyi bölmüş ("*Tn*+1", MT-3): her parça ayrı
        # sarılır, işaretler yerinde kalır ("*T<sub>n</sub>*<sub>+1</sub>").
        parcalar = [p for p in re.split(r"(\*+)", ara) if p]
        if any(not p.startswith("*") and p.strip() != p for p in parcalar):
            rapor.atla("bicim_isaretli_simge")
            return None
        govde = "".join(p if p.startswith("*") else f"<{tag}>{p}</{tag}>"
                        for p in parcalar)
    else:
        govde = f"<{tag}>{ara}</{tag}>"
    kind = "duz_metin_ust_simge" if s == "P" else "duz_metin_alt_simge"
    return _sarma_sonu(md, ak, bas, son, govde, kind, s, pdf, a, b, icerik,
                       mi[-1])


def _karma_sarma(md: str, ak: _MdAkis, mi: List[int], s: str,
                 pdf: _PdfAkis, a: int, b: int, icerik: str,
                 etiket_mi: Dict[int, List[int]], rapor: OnarimRaporu
                 ) -> Optional[Tuple[int, int, str, Degisiklik]]:
    """Düz karakter + bütün etiketlerden oluşan koşuyu tek etikete topla:
    PDF'te "n0" alt simge, Markdown'da "fn<sup>0</sup>" -> "f<sub>n0</sub>"."""
    bas, son = ak.bas[mi[0]], ak.son[mi[-1]]
    kume = set(mi)
    for no in sorted({ak.etiket[x] for x in mi if ak.etiket[x] >= 0}):
        a0, _a1, k0, k1, _tur = ak.etiketler[no]
        if k0 < 0 or not set(etiket_mi.get(no, [])) <= kume:
            rapor.atla("kismi_etiket")
            return None
        bas, son = min(bas, a0), max(son, k1)
    ara = md[bas:son]
    ic = _SIMGE_ETIKETI_RE.sub("", ara)
    duz_ic = "".join(ch for ch in ic if not ch.isspace())
    if any(c in ic for c in "<>*_$\\") or _norm_birlestir(duz_ic) != icerik \
            or any(not _norm_karakter(ch) for ch in duz_ic) \
            or _unicode_simge_var(ic):
        rapor.atla("kismi_etiket")
        return None
    tag = "sup" if s == "P" else "sub"
    kind = "karma_ust_simge" if s == "P" else "karma_alt_simge"
    return _sarma_sonu(md, ak, bas, son, f"<{tag}>{ic}</{tag}>", kind, s, pdf,
                       a, b, icerik, mi[-1])


def _sarma_sonu(md: str, ak: _MdAkis, bas: int, son: int, govde: str,
                kind: str, s: str, pdf: _PdfAkis, a: int, b: int,
                icerik: str, son_mi: int
                ) -> Tuple[int, int, str, Degisiklik]:
    """Sarmanın boşluk kuralları (yalnız boşluk; metin değişmez)."""
    dipnot = s == "P" and bool(DIPNOT_ISARETI_RE.match(icerik))
    if dipnot and bas >= 1 and md[bas - 1] == " ":
        kind = "dipnot_isareti"
    # PDF'te simge tabanına bitişikse (önünde boşluk yok) Marker'ın araya
    # koyduğu tek boşluk kaldırılır: "seven ferries 1" -> "seven ferries<sup>1</sup>",
    # "C ′" -> "C<sup>′</sup>".
    if not pdf.bosluk_once[pdf.kaynak[a]] \
            and bas >= 2 and md[bas - 1] == " " and not md[bas - 2].isspace():
        bas -= 1
    # Simgeden sonra PDF'te boşluk var, Markdown'da yoksa (Marker simgenin
    # ardındaki boşluğu yutmuş: "vLisincreased") boşluk eklenir.
    ek = ""
    if b < len(pdf.norm) and pdf.bosluk_once[pdf.kaynak[b]] \
            and pdf.satir[pdf.kaynak[b]] == pdf.satir[pdf.kaynak[a]] \
            and son < len(md) and md[son].isalnum() \
            and son_mi + 1 < len(ak.bas) and ak.bas[son_mi + 1] == son:
        ek = " "
    k = pdf.kaynak[a]
    return (bas, son, govde + ek, Degisiklik(
        kind, bas, son, md[bas:son], govde + ek, pdf.sayfa[k], k,
        pdf.kaynak[b - 1]))


def _norm_birlestir(metin: str) -> str:
    return "".join(_norm_karakter(c) for c in metin)


# ---------------------------------------------------------------------------
# 3) Tablo hücresi: <br> ile bölünmüş üst simgeli birim
# ---------------------------------------------------------------------------

def _tablo_hucre_duzelt(md: str, pdf: _PdfAkis, rapor: OnarimRaporu) -> str:
    duz: List[Tuple[int, int, str, Degisiklik]] = []
    ofset = 0
    for satir in md.split("\n"):
        if satir.lstrip().startswith("|") and "<br" in satir.lower():
            duz.extend(_tablo_satiri(satir, ofset, pdf, rapor))
        ofset += len(satir) + 1
    return _uygula(md, duz, rapor) if duz else md


_HUCRE_AYRAC_RE = re.compile(r"(?<!\\)\|")
_BR_RE = re.compile(r"<br\s*/?\s*>", re.IGNORECASE)


_SIMGE_ETIKETI_RE = re.compile(r"</?su[pb]>", re.IGNORECASE)


def _hucre_duz(metin: str) -> str:
    return _norm_birlestir(_BR_RE.sub("", _SIMGE_ETIKETI_RE.sub("", metin)))


def _tablo_satiri(satir: str, ofset: int, pdf: _PdfAkis,
                  rapor: OnarimRaporu) -> List[Tuple[int, int, str, Degisiklik]]:
    sinirlar = [m.start() for m in _HUCRE_AYRAC_RE.finditer(satir)]
    if len(sinirlar) < 2:
        return []
    hucreler = [(sinirlar[k] + 1, sinirlar[k + 1]) for k in range(len(sinirlar) - 1)]
    sonuc = []
    for h, (hb, hs) in enumerate(hucreler):
        icerik = satir[hb:hs]
        if not _BR_RE.search(icerik) or _unicode_simge_var(icerik):
            continue
        parcalar = [p.strip() for p in _BR_RE.split(icerik) if p.strip()]
        # Parçada yalnız <sup>/<sub> etiketi olabilir (eşleştirmede atılır;
        # yeni hücre PDF'ten kurulur ve karakter kümesi birebir denetlenir).
        if not (2 <= len(parcalar) <= TABLO_EN_COK_PARCA) or any(
                len(_SIMGE_ETIKETI_RE.sub("", p)) > TABLO_PARCA_EN_UZUN
                or re.search(r"[*$\[\]<>`\\]", _SIMGE_ETIKETI_RE.sub("", p))
                for p in parcalar):
            continue
        sol = "".join(_hucre_duz(satir[a:b]) for a, b in hucreler[:h])
        sag = "".join(_hucre_duz(satir[a:b]) for a, b in hucreler[h + 1:])
        adaylar = []
        for perm in itertools.permutations(parcalar):
            govde = _hucre_duz("".join(perm))
            for ctx_sol, ctx_sag in ((sol[-16:], ""), ("", sag[:16])):
                if len(ctx_sol) + len(ctx_sag) < 6:
                    continue
                w = ctx_sol + govde + ctx_sag
                j = pdf.norm.find(w)
                if j < 0 or pdf.norm.find(w, j + 1) >= 0:
                    continue
                pa = j + len(ctx_sol)
                adaylar.append((pa, pa + len(govde), perm))
                break
        adaylar = list({(a, b): (a, b, p) for a, b, p in adaylar}.values())
        if len(adaylar) != 1:
            if adaylar:
                rapor.atla("tablo_hucre_belirsiz")
            continue
        pa, pb, perm = adaylar[0]
        ks = [pdf.kaynak[t] for t in range(pa, pb)]
        if len({pdf.satir[k] for k in ks}) != 1 or not any(
                pdf.sinif[k] in ("P", "B") for k in ks):
            continue
        yeni = _pdf_araligi_md(pdf, ks[0], ks[-1])
        if yeni is None or _hucre_duz(yeni) != _hucre_duz("".join(perm)):
            rapor.atla("tablo_hucre_kayip_riski")
            continue
        if sorted(_hucre_duz(icerik)) != sorted(_hucre_duz(yeni)):
            rapor.atla("tablo_hucre_kayip_riski")
            continue
        bosluk_sol = icerik[:len(icerik) - len(icerik.lstrip())]
        bosluk_sag = icerik[len(icerik.rstrip()):]
        yeni_hucre = bosluk_sol + yeni + bosluk_sag
        a, b = ofset + hb, ofset + hs
        sonuc.append((a, b, yeni_hucre, Degisiklik(
            "tablo_hucre_sirasi", a, b, icerik, yeni_hucre,
            pdf.sayfa[ks[0]], ks[0], ks[-1])))
    return sonuc


def _pdf_araligi_md(pdf: _PdfAkis, k0: int, k1: int) -> Optional[str]:
    """PDF karakter aralığını <sup>/<sub> etiketli Markdown'a çevir."""
    out = []
    acik = None
    for k in range(k0, k1 + 1):
        c = pdf.ch[k]
        if not c.strip():
            continue
        s = pdf.sinif[k]
        tur = {"P": "sup", "B": "sub"}.get(s)
        if tur != acik:
            if acik:
                out.append(f"</{acik}>")
            if pdf.bosluk_once[k] and k > k0 and out:
                out.append(" ")
            if tur:
                out.append(f"<{tur}>")
            acik = tur
        elif pdf.bosluk_once[k] and k > k0:
            out.append(" ")
        if c in "<>&":
            return None
        out.append(c)
    if acik:
        out.append(f"</{acik}>")
    return "".join(out)


# ---------------------------------------------------------------------------
# 4) Vektör oku, derece, el yazısı harf
# ---------------------------------------------------------------------------

def _md_indeksi(ak: _MdAkis, ofset: int) -> Optional[int]:
    """Markdown ofsetindeki karakterin norm indeksi (ikili arama)."""
    import bisect
    x = bisect.bisect_left(ak.bas, ofset)
    if x < len(ak.bas) and ak.bas[x] == ofset:
        return x
    return None


def _vektor_oku_var(pdf: _PdfAkis, k: int) -> bool:
    """PDF karakteri k'nin üstünde vektör oku (U+20D7) var mı?"""
    for t in (k - 1, k - 2, k + 1, k + 2):
        if 0 <= t < len(pdf.ch) and pdf.ch[t] == VEKTOR_OKU \
                and pdf.satir[t] == pdf.satir[k]:
            orta = (pdf.x0[t] + pdf.x1[t]) / 2.0
            genis = max(pdf.x1[k] - pdf.x0[k], 1.0)
            if pdf.x0[k] - genis <= orta <= pdf.x1[k] + genis:
                return True
    return False


def _matematige_al(md: str, bas: int, son: int, govde: str
                   ) -> Optional[Tuple[int, str]]:
    """md[bas:son] yerine "$govde$" yaz; ardındaki basit simge etiketlerini
    (en çok iki) aynı matematiğe kat. Sonuç bitişik bir matematiğe ya da
    simge etiketine değecekse (dönüştürücüde "$…$$…$" = display matematik
    açılışı) None: dokunulmaz."""
    if bas > 0 and md[bas - 1] == "$":
        return None
    if md.endswith(_SIMGE_ETIKETI_KAPANIS, 0, bas):
        return None
    for _ in range(2):
        ard = _ARDISIK_SIMGE_RE.match(md, son)
        if not ard:
            break
        op = "^" if ard.group(1) == "sup" else "_"
        govde += f"{op}{{{ard.group(2).replace('−', '-')}}}"
        son = ard.end()
    if md.startswith(_SIMGE_ETIKETI_ACILIS, son) or md.startswith("$", son):
        return None
    return son, f"${govde}$"


def _simge_ve_harf_duzelt(md: str, pdf: _PdfAkis, rapor: OnarimRaporu) -> str:
    ak = _md_akisi_kur(md)
    duz: List[Tuple[int, int, str, Degisiklik]] = []

    def hizala(x: int) -> Optional[int]:
        return _md_den_pdf_e(pdf, ak, x, x + 1)

    # Vektör oku.
    for rx in (_VEKTOR_ONCE_RE, _VEKTOR_SONRA_RE):
        for m in rx.finditer(md):
            x = _md_indeksi(ak, m.start("x"))
            if x is None or ak.sinif[x] != "N":
                continue
            pj = hizala(x)
            if pj is None:
                rapor.atla("vektor_baglam_bulunamadi")
                continue
            k = pdf.kaynak[pj]
            if not _vektor_oku_var(pdf, k):
                rapor.atla("vektor_pdfte_ok_yok")
                continue
            harf = m.group("x")
            govde = f"\\vec{{{harf}}}"
            aks = _VEKTOR_AKSAN.get(m.group("aks"))
            if aks:
                govde = f"\\{aks}{{{govde}}}"
            sonuc = _matematige_al(md, m.start(), m.end(), govde)
            if sonuc is None:
                rapor.atla("vektor_bitisik_matematik")
                continue
            son, yeni = sonuc
            duz.append((m.start(), son, yeni, Degisiklik(
                "vektor_oku", m.start(), son, md[m.start():son], yeni,
                pdf.sayfa[k], k, k)))

    # Derece (yalnız sayıdan sonra: "20 ◦C", "51◦42'"; satır başındaki "◦"
    # madde imidir, dönüştürücü onu liste sayar).
    for m in _DERECE_MD_RE.finditer(md):
        x = _md_indeksi(ak, m.start("d" if m.group("d") else "d2"))
        if x is None or ak.sinif[x] == "M":
            continue
        onceki = md[max(0, m.start() - 8):m.start()].rstrip(" \t*")
        if onceki.endswith("<sup>"):
            onceki = onceki[:-5].rstrip(" \t*")
        if not onceki or not onceki[-1].isdigit():
            rapor.atla("derece_sayidan_sonra_degil")
            continue
        pj = hizala(x)
        if pj is None:
            rapor.atla("derece_baglam_bulunamadi")
            continue
        k = pdf.kaynak[pj]
        if pdf.ch[k] not in DERECE_KARAKTERLERI or pdf.sinif[k] != "P":
            rapor.atla("derece_pdfte_ust_degil")
            continue
        duz.append((m.start(), m.end(), DERECE, Degisiklik(
            "derece", m.start(), m.end(), m.group(0), DERECE,
            pdf.sayfa[k], k, k)))

    # El yazısı (CMSY) büyük harf.
    P = pdf.norm
    for i, ch in enumerate(P):
        if not ("A" <= ch <= "Z"):
            continue
        k = pdf.kaynak[i]
        if not KALIGRAFIK_FONT_RE.search(pdf.font[k]) or pdf.sinif[k] != "N":
            continue
        j = _pdf_den_md_ye(pdf, ak, i, i + 1)
        if j is None:
            rapor.atla("kaligrafik_baglam_bulunamadi")
            continue
        if ak.sinif[j] != "N" or ak.etiket[j] != -1:
            continue
        bas, son = ak.bas[j], ak.son[j]
        if md[bas:son] != ch:
            continue
        once = md[bas - 1] if bas > 0 else " "
        sonra = md[son] if son < len(md) else " "
        if once.isalnum() or sonra.isalnum() or once in "\\$" or sonra == "$":
            rapor.atla("kaligrafik_sozcuk_icinde")
            continue
        sonuc = _matematige_al(md, bas, son, f"\\mathcal{{{ch}}}")
        if sonuc is None:
            rapor.atla("kaligrafik_bitisik_matematik")
            continue
        son, yeni = sonuc
        duz.append((bas, son, yeni, Degisiklik(
            "kaligrafik_harf", bas, son, md[bas:son], yeni, pdf.sayfa[k], k,
            k)))
    return _uygula(md, duz, rapor) if duz else md


def _pdf_dipnot_isaretleri(pdf: _PdfAkis) -> Dict[str, List[Tuple[str, str, str]]]:
    """Metin içi üst simge rakamlar: rakam -> (önceki sözcük, soldaki sözcük,
    sağdaki sözcük) bağlamlarının listesi (norm biçim; yoksa "").

    İşaret içerik akışında hücre satırlarından SONRA, kendi satırında
    gelebilir (ölçülen bir raporun tabloları); G1c'nin sınıfı (P) o zaman komşusuzluk yüzünden
    düşer. Bu yüzden sol komşu ve üst simgelik GEOMETRİYLE bulunur: işaretin
    soluna TABLO_ISARET_SOL_ARALIK'tan yakın biten, dikeyde örtüşen düz
    karakter; işaretin kutusu ondan belirgin küçük (KUCUK_PUNTO_ORANI) ve
    alt kenarı belirgin yukarıda (UST_SIMGE_KAYMA). Satır başındaki işaret
    (sayfa altı dipnot tanımı) sol komşusuz kalır, sayılmaz."""
    out: Dict[str, List[str]] = defaultdict(list)
    n = len(pdf.ch)
    sayfa_karakter: Dict[int, List[int]] = defaultdict(list)
    for k in range(n):
        if pdf.ch[k].strip() and not pdf.ch[k].isdigit():
            sayfa_karakter[pdf.sayfa[k]].append(k)
    k = 0
    while k < n:
        if not pdf.ch[k].isdigit() or (k > 0 and pdf.ch[k - 1].isdigit()
                                       and pdf.satir[k - 1] == pdf.satir[k]):
            k += 1
            continue
        t = k
        while t + 1 < n and pdf.ch[t + 1].isdigit() and pdf.satir[t + 1] == pdf.satir[k]:
            t += 1
        rakam = "".join(pdf.ch[k:t + 1])
        x0 = pdf.x0[k]
        y0, y1 = min(pdf.y0[k:t + 1]), max(pdf.y1[k:t + 1])
        en_iyi = None
        oy_isaret = max(pdf.oy[k:t + 1])
        for c in sayfa_karakter.get(pdf.sayfa[k], ()):
            if not (x0 - TABLO_ISARET_SOL_ARALIK <= pdf.x1[c] <= x0 + 0.5):
                continue
            # İşaret kutusunun en az yarısı komşunun dikey aralığında olmalı
            # (üst/alt satırın harfi komşu sayılmaz: alt alta iki kimyasal
            # formül).
            ortak = min(pdf.y1[c], y1) - max(pdf.y0[c], y0)
            if ortak < 0.5 * (y1 - y0):
                continue
            if en_iyi is None or (pdf.x1[c], -abs(pdf.oy[c] - oy_isaret)) > \
                    (pdf.x1[en_iyi], -abs(pdf.oy[en_iyi] - oy_isaret)):
                en_iyi = c
        isaret_basi, isaret_sonu = k, t
        k = t + 1
        if en_iyi is None or len(rakam) > 3:
            continue
        # Üst simge: kutusu sol komşudan belirgin küçük ve taban çizgisi
        # belirgin yukarıda (alt simge, ör. kimyasal formüldeki "C₁₀", aşağıda).
        boy = pdf.y1[en_iyi] - pdf.y0[en_iyi]
        punto = pdf.satir_puntosu.get(pdf.satir[en_iyi], 0.0) or boy
        if boy <= 0 or (y1 - y0) > KUCUK_PUNTO_ORANI * boy \
                or (pdf.oy[en_iyi] - max(pdf.oy[isaret_basi:isaret_sonu + 1])) \
                < UST_SIMGE_KAYMA * punto:
            continue
        sol1, b = _sozcuk_geri(pdf, en_iyi)
        if not sol1:
            continue
        # Bir önceki sözcük (aynı satırda boşluktan önce) ve işaretin sağındaki
        # sözcük: hücrede aynı sözcük birden çok geçerse yer belirleyici.
        onceki = b - 1
        while onceki >= 0 and pdf.satir[onceki] == pdf.satir[en_iyi] and not pdf.ch[onceki].strip():
            onceki -= 1
        sol2 = (_sozcuk_geri(pdf, onceki)[0]
                if onceki >= 0 and pdf.satir[onceki] == pdf.satir[en_iyi] else "")
        sag = None
        for c in sayfa_karakter.get(pdf.sayfa[en_iyi], ()):
            if pdf.satir[c] != pdf.satir[en_iyi] or pdf.x0[c] < pdf.x1[isaret_sonu] - 0.5:
                continue
            if sag is None or pdf.x0[c] < pdf.x0[sag]:
                sag = c
        sag1 = ""
        if sag is not None:
            e = sag
            while e + 1 < n and pdf.satir[e + 1] == pdf.satir[sag] \
                    and pdf.ch[e + 1].strip() and not pdf.bosluk_once[e + 1]:
                e += 1
            sag1 = "".join(_norm_karakter(pdf.ch[x]) for x in range(sag, e + 1))
        out[rakam].append((sol2, sol1, sag1))
    return out


def _sozcuk_geri(pdf: _PdfAkis, son: int) -> Tuple[str, int]:
    """son karakterinde biten sözcük (norm) ve başlangıç indeksi (aynı MuPDF
    satırı, araya boşluk girmeden)."""
    b = son
    while b - 1 >= 0 and pdf.satir[b - 1] == pdf.satir[son] \
            and pdf.ch[b - 1].strip() and not pdf.bosluk_once[b]:
        b -= 1
    return "".join(_norm_karakter(pdf.ch[x]) for x in range(b, son + 1)), b


def _tablo_dipnot_duzelt(md: str, pdf: _PdfAkis, rapor: OnarimRaporu) -> str:
    """Tablo hücresinde düz rakam olarak kalmış dipnot işareti (FX2):
    PDF'te aynı rakam bir sözcüğün ardında üst simgeyse ve o sözcük aynı
    hücrede TEK geçiyorsa işaret ``<sup>N</sup>`` olur ve sözcüğün hemen
    ardına konur (Marker işareti hücre başına ya da satır sonuna
    taşıyabiliyor). Rakam ve sözcük birden çok yerde eşleşiyorsa
    dokunulmaz. Satırın karakter çoklu kümesi değişmez (kayıpsızlık ağı)."""
    isaretler = _pdf_dipnot_isaretleri(pdf)
    if not isaretler:
        return md
    satirlar = md.split("\n")
    degisti = False
    for no, satir in enumerate(satirlar):
        if not satir.lstrip().startswith("|") or _TABLO_AYRAC_RE.match(satir):
            continue
        hucreler = re.split(r"(?<!\\)\|", satir)
        yeni_hucreler = []
        for hucre in hucreler:
            parcalar = _HUCRE_BELIRTEC_AYRAC_RE.split(hucre)
            # Çift indeks belirteç, tek indeks ayraç.
            adaylar = [i for i in range(0, len(parcalar), 2)
                       if _DIPNOT_ADAYI_RE.match(parcalar[i]) and parcalar[i] in isaretler]
            for i in adaylar:
                rakam = parcalar[i]
                baglamlar = isaretler[rakam]
                if len(baglamlar) != 1:
                    rapor.atla("tablo_dipnot_belirsiz")
                    continue
                sol2, sol1, sag1 = baglamlar[0]
                # Rakam çıkarılmış belirteç dizisi üzerinde ara.
                sira = [j for j in range(0, len(parcalar), 2) if j != i and parcalar[j]]
                normlar = ["".join(_norm_karakter(c) for c in parcalar[j]) for j in sira]
                eslesen = [x for x, nn in enumerate(normlar) if nn == sol1]
                if len(eslesen) > 1:
                    eslesen = [x for x in eslesen
                               if (sol2 and x > 0 and normlar[x - 1] == sol2)
                               or (sag1 and x + 1 < len(normlar) and normlar[x + 1] == sag1)]
                if len(eslesen) != 1:
                    rapor.atla("tablo_dipnot_belirsiz")
                    continue
                j = sira[eslesen[0]]
                # Rakamı (ve bir komşu ayracı) çıkar, sözcüğün ardına ekle.
                if i + 1 < len(parcalar):
                    del parcalar[i:i + 2]
                else:
                    del parcalar[i - 1:i + 1]
                if j > i:
                    j -= 2
                parcalar[j] = parcalar[j] + "<sup>" + rakam + "</sup>"
                rapor.degisiklikler.append(Degisiklik(
                    "tablo_dipnot", -1, -1, rakam, "<sup>" + rakam + "</sup>"))
                degisti = True
                break   # hücrede bir işaret; gerekirse sonraki geçiş
            yeni_hucreler.append("".join(parcalar))
        satirlar[no] = "|".join(yeni_hucreler)
    return "\n".join(satirlar) if degisti else md


def _metin_simgeleri(tex: str) -> str:
    """Simgedeki Latin/Yunan dışı harf (Kiril alt simge "н") metin kipinde
    kalır: matematik yazı tipinde o glif yok (kaynakta da p_{\\text{н}})."""
    return _METIN_SIMGE_RE.sub(lambda x: x.group(1) + "{\\text{" + x.group(2) + "}}", tex)


def _matematik_italik_duzelt(md: str, pdf: _PdfAkis, rapor: OnarimRaporu) -> str:
    """Marker'ın böldüğü satır içi matematik (RAPOR MT-8): PDF'te TeX
    matematik italiğindeki harf Markdown'da italik ve
    - önünde PDF'te düz yazı tipli, boşluksuz bitişik "d" varsa (diferansiyel)
      "d*t*" -> "$\\mathrm{d}t$";
    - PDF'te üstünde nokta varsa "*m*˙" -> "$\\dot{m}$".
    Tek başına italik harf ("*w*") \\textit olarak doğru basılır; G1c'nin
    kanıt testi (test_kanit_de) bu biçimi sabitler, dokunulmaz. Yer, iki
    yanlı benzersiz bağlamla bulunur; belirsizse dokunulmaz."""
    ak = _md_akisi_kur(md)
    duz: List[Tuple[int, int, str, Degisiklik]] = []
    for m in _MD_ITALIK_HARF_RE.finditer(md):
        harf = m.group("x")
        i0 = _md_indeksi(ak, m.start("x"))
        if i0 is None or ak.sinif[i0] != "N":
            continue
        i1 = i0 + len(harf)
        if ak.norm[i0:i1] != harf:
            continue
        j = _md_den_pdf_e(pdf, ak, i0, i1)
        if j is None:
            rapor.atla("italik_baglam_yok")
            continue
        ks = [pdf.kaynak[t] for t in range(j, j + len(harf))]
        if any(not MATEMATIK_ITALIK_FONT_RE.search(pdf.font[k]) for k in ks):
            continue
        if any(pdf.sinif[k] != "N" for k in ks):
            # "d*CL*" PDF'te dC_L: harflerden biri alt simge; düz "$\\mathrm{d}CL$"
            # yapısı bozar, dokunulmaz.
            rapor.atla("italik_simgeli")
            continue
        if not (m.group("d") or m.group("nokta")):
            continue
        bas, son = m.start() + len(m.group("d")), m.end("x") + 1
        govde = harf
        if m.group("d"):
            # "d" düz yazı tipinde, harfe boşluksuz bitişik olmalı (diferansiyel).
            kd = ks[0] - 1
            while kd >= 0 and not pdf.ch[kd].strip():
                kd -= 1
            if not (kd >= 0 and pdf.ch[kd] == "d" and pdf.satir[kd] == pdf.satir[ks[0]]
                    and not pdf.bosluk_once[ks[0]]
                    and not MATEMATIK_ITALIK_FONT_RE.search(pdf.font[kd])):
                continue
            bas = m.start()
            govde = "\\mathrm{d}" + harf
        if m.group("nokta"):
            if len(harf) != 1:
                continue
            k = ks[0]
            nokta = [t for t in range(max(0, k - 2), min(len(pdf.ch), k + 3))
                     if pdf.ch[t] == "˙" and pdf.satir[t] == pdf.satir[k]
                     and pdf.x0[t] < pdf.x1[k] and pdf.x1[t] > pdf.x0[k]]
            if not nokta:
                continue
            govde = govde.replace(harf, "\\dot{" + harf + "}")
            son = m.end("nokta")
        # Ardındaki basit simge aynı matematiğe katılır ("*p*<sub>a</sub>" ->
        # "$p_{a}$"); bitişik matematiğe değecekse dokunulmaz ("$…$$…$").
        sonuc = _matematige_al(md, bas, son, govde)
        if sonuc is None:
            rapor.atla("italik_bitisik_matematik")
            continue
        son, yeni = sonuc
        yeni = _metin_simgeleri(yeni)
        duz.append((bas, son, yeni, Degisiklik(
            "matematik_italik", bas, son, md[bas:son], yeni, pdf.sayfa[ks[0]], ks[0], ks[-1])))
    return _uygula(md, duz, rapor) if duz else md


# ---------------------------------------------------------------------------
# 5) Display matematikte tek harf ikamesi (MT-10)
# ---------------------------------------------------------------------------

def _latex_harfleri(tex: str, taban: int) -> List[Tuple[int, str]]:
    """LaTeX'teki harfler: (Markdown ofseti ya da -1, harf).

    Düz ASCII harf değişkendir, ofseti verilir (ikame edilebilir). Yunan /
    işlev komutları ve metin komutlarının içindeki harfler karşılaştırmaya
    girer ama ofsetleri -1'dir (değiştirilmez). Komut adları atlanır.
    """
    out: List[Tuple[int, str]] = []
    metin_derinlik: List[int] = []   # metin komutu açıklarının parantez düzeyi
    duzey = 0
    i, n = 0, len(tex)
    while i < n:
        c = tex[i]
        if c == "\\":
            m = re.match(r"\\([A-Za-z]+)", tex[i:])
            if not m:
                i += 2
                continue
            ad = m.group(1)
            i += m.end()
            if ad in _LATEX_YUNAN:
                out.append((-1, _LATEX_YUNAN[ad]))
            elif ad in _LATEX_FONKSIYON:
                out.extend((-1, ch) for ch in ad)
            elif ad in _LATEX_METIN_KOMUTU and i < n and tex[i] == "{":
                metin_derinlik.append(duzey)
            continue
        if c == "{":
            duzey += 1
        elif c == "}":
            duzey -= 1
            if metin_derinlik and metin_derinlik[-1] == duzey:
                metin_derinlik.pop()
        elif c.isalpha():
            ofset = taban + i if (c.isascii() and not metin_derinlik) else -1
            out.append((ofset, _norm_karakter(c) or c))
        i += 1
    return out


def _denklem_bolgesi(pdf: _PdfAkis, ak: _MdAkis, e: int
                     ) -> Optional[Tuple[int, int]]:
    """Markdown norm'undaki display matematik engeli e için PDF norm bölgesi
    [a, b): sol ve sağ bağlamın ilk benzersiz uzunluğu (iki metinde de tek
    geçer). Bulunamazsa, çok uzunsa ya da iki sayfaya yayılıyorsa None."""
    M, P = ak.norm, pdf.norm
    a = b = None
    for uz in DENKLEM_BAGLAM_UZUNLUKLARI:
        if e - uz < 0 or _ENGEL in M[e - uz:e]:
            break
        w = M[e - uz:e]
        j = _tek_bul(P, w)
        if j is not None and _tek_bul(M, w) is not None:
            a = j + uz
            break
    for uz in DENKLEM_BAGLAM_UZUNLUKLARI:
        if e + 1 + uz > len(M) or _ENGEL in M[e + 1:e + 1 + uz]:
            break
        w = M[e + 1:e + 1 + uz]
        j = _tek_bul(P, w)
        if j is not None and _tek_bul(M, w) is not None:
            b = j
            break
    if a is None or b is None or not 0 < b - a <= DENKLEM_BOLGE_EN_UZUN:
        return None
    if pdf.sayfa[pdf.kaynak[a]] != pdf.sayfa[pdf.kaynak[b - 1]]:
        return None
    return a, b


def _denklem_harf_duzelt(md: str, pdf: _PdfAkis, rapor: OnarimRaporu) -> str:
    """$$…$$ bloğunda PDF'le TEK harf farkı varsa o harfi PDF'teki yap.

    Kesinlik koşulları: denklemin PDF bölgesi iki yanlı benzersiz bağlamla
    bulunur; harf dizileri (sırasıyla) tek bir 1'e 1 ikame dışında aynıdır
    ve en az DENKLEM_EN_AZ_ESLESEN_HARF harf aynen eşleşir; iki harf de ASCII
    Latin harftir (Yunan varyantları ϑ/θ, ϖ/π ikame sayılmaz); PDF harfinin
    yazıtipi simge yazıtipi değildir ve aynı yazıtipi bu denklemde en az bir
    harfi daha AYNEN vermiştir (metin katmanı güvenilir).
    """
    ak = _md_akisi_kur(md)
    duz: List[Tuple[int, int, str, Degisiklik]] = []
    for e, ch in enumerate(ak.norm):
        if ch != _ENGEL:
            continue
        bas, son = ak.bas[e], ak.son[e]
        if not md.startswith("$$", bas):
            continue
        bolge = _denklem_bolgesi(pdf, ak, e)
        if bolge is None:
            rapor.atla("denklem_bolgesi_yok")
            continue
        a, b = bolge
        ph = [(t, pdf.norm[t]) for t in range(a, b) if pdf.norm[t].isalpha()]
        lh = _latex_harfleri(md[bas:son], bas)
        sm = difflib.SequenceMatcher(None, [x[1] for x in ph],
                                     [x[1] for x in lh], autojunk=False)
        farklar = [op for op in sm.get_opcodes() if op[0] != "equal"]
        if not farklar:
            continue
        esit = sum(op[2] - op[1] for op in sm.get_opcodes() if op[0] == "equal")
        if len(farklar) != 1 or esit < DENKLEM_EN_AZ_ESLESEN_HARF:
            rapor.atla("denklem_cok_fark")
            continue
        tur, i1, i2, j1, j2 = farklar[0]
        if tur != "replace" or i2 - i1 != 1 or j2 - j1 != 1:
            rapor.atla("denklem_cok_fark")
            continue
        pt, pch = ph[i1]
        ofset, lch = lh[j1]
        if ofset < 0 or not (pch.isascii() and lch.isascii()):
            rapor.atla("denklem_latin_disi")
            continue
        k = pdf.kaynak[pt]
        font = pdf.font[k]
        esit_fontlar = {pdf.font[pdf.kaynak[ph[x][0]]]
                        for op in sm.get_opcodes() if op[0] == "equal"
                        for x in range(op[1], op[2])}
        if GUVENILMEZ_HARF_FONT_RE.search(font) or font not in esit_fontlar:
            rapor.atla("denklem_font_guvenilmez")
            continue
        if md[ofset] != lch:
            continue
        duz.append((ofset, ofset + 1, pch, Degisiklik(
            "denklem_harf", ofset, ofset + 1, lch, pch, pdf.sayfa[k], k, k)))
    return _uygula(md, duz, rapor) if duz else md


# ---------------------------------------------------------------------------
# 6) Matematik bölgesi: ok simgesi (MT-46) ve ondalık virgül (MT-14)
# ---------------------------------------------------------------------------

def _baglam_konumu(P: str, M: str, w: str) -> Optional[int]:
    """w iki metinde de tam bir kez geçiyorsa P'deki yeri."""
    j = _tek_bul(P, w)
    if j is None or _tek_bul(M, w) is None:
        return None
    return j


def _matematik_bolgeleri(pdf: _PdfAkis, ak: _MdAkis, md: str
                         ) -> List[Tuple[int, int, int, int]]:
    """Matematik engelleri için (md_bas, md_son, pdf_a, pdf_b) listesi.

    Ardışık matematikler (aralarında MATEMATIK_OBEK_ARASI'ndan kısa metin)
    tek öbek olarak bulunur: öbeğin solundaki ve sağındaki metin iki metinde
    de tek geçen bir bağlamla PDF'e yerleştirilir, öbeğin PDF bölgesi aradaki
    metinlerle (bölge içinde tek geçmeli) matematiklere bölünür. Bulunamayan,
    çok uzun ya da iki sayfaya yayılan bölge atlanır."""
    M, P = ak.norm, pdf.norm
    engeller = [e for e, ch in enumerate(M)
                if ch == _ENGEL and md.startswith("$", ak.bas[e])]
    out: List[Tuple[int, int, int, int]] = []
    i = 0
    while i < len(engeller):
        obek = [engeller[i]]
        while i + 1 < len(engeller):
            ara = M[obek[-1] + 1:engeller[i + 1]]
            if len(ara) >= MATEMATIK_OBEK_ARASI or _ENGEL in ara:
                break
            obek.append(engeller[i + 1])
            i += 1
        i += 1
        e0, e1 = obek[0], obek[-1]
        a = b = None
        for uz in MATEMATIK_BAGLAM_UZUNLUKLARI:
            if e0 - uz < 0 or _ENGEL in M[e0 - uz:e0]:
                break
            j = _baglam_konumu(P, M, M[e0 - uz:e0])
            if j is not None:
                a = j + uz
                break
        for uz in MATEMATIK_BAGLAM_UZUNLUKLARI:
            if e1 + 1 + uz > len(M) or _ENGEL in M[e1 + 1:e1 + 1 + uz]:
                break
            j = _baglam_konumu(P, M, M[e1 + 1:e1 + 1 + uz])
            if j is not None:
                b = j
                break
        if a is None or b is None or not 0 < b - a <= DENKLEM_BOLGE_EN_UZUN * len(obek):
            continue
        if pdf.sayfa[pdf.kaynak[a]] != pdf.sayfa[pdf.kaynak[b - 1]]:
            continue
        # Öbeği aradaki metinlerle böl.
        sinirlar = [a]
        imlec = a
        bolunemedi = False
        for k in range(len(obek) - 1):
            ara = M[obek[k] + 1:obek[k + 1]]
            if not ara:
                bolunemedi = True
                break
            j = P.find(ara, imlec + 1, b)
            if j < 0 or P.find(ara, j + 1, b) >= 0:
                bolunemedi = True
                break
            sinirlar.extend([j, j + len(ara)])
            imlec = j + len(ara)
        if bolunemedi:
            continue
        sinirlar.append(b)
        for k, e in enumerate(obek):
            pa, pb = sinirlar[2 * k], sinirlar[2 * k + 1]
            if 0 < pb - pa <= DENKLEM_BOLGE_EN_UZUN:
                out.append((ak.bas[e], ak.son[e], pa, pb))
    return out


def _pdf_oklari(pdf: _PdfAkis, a: int, b: int) -> Optional[List[str]]:
    """PDF bölgesindeki ok simgeleri (birleşik oklar tek simge); belirsizse
    None.

    Birleşik oklar: üst üste binen/bitişik ⇐+⇒ (⟺), "="+⇒ (⟹), "−"+→ (⟶),
    "|"+→ (↦), "|"+"−"+→ (⟼) … Okla dokunan başka bir işaret (bilinmeyen
    birleşim) bölgeyi belirsiz yapar: yanlış ikame yerine dokunulmaz."""
    k0, k1 = pdf.kaynak[a], pdf.kaynak[b - 1]

    def sonraki(k: int) -> Optional[int]:
        t = k + 1
        while t <= k1 and not pdf.ch[t].strip():
            t += 1
        if t > k1 or pdf.satir[t] != pdf.satir[k]:
            return None
        return t

    def onceki(k: int) -> Optional[int]:
        t = k - 1
        while t >= k0 and not pdf.ch[t].strip():
            t -= 1
        if t < k0 or pdf.satir[t] != pdf.satir[k]:
            return None
        return t

    def dokunur(sol: Optional[int], sag: Optional[int]) -> bool:
        return sol is not None and sag is not None and pdf.x0[sag] - pdf.x1[sol] < OK_DOKUNMA_PT

    oklar: List[str] = []
    k = k0
    while k <= k1:
        c = pdf.ch[k]
        t = sonraki(k)
        if c in DIKEY_CIZGILER:
            if dokunur(k, t):
                c2 = pdf.ch[t]
                t2 = sonraki(t)
                if c2 in ("→", "⟶"):
                    oklar.append("↦" if c2 == "→" else "⟼")
                    k = t + 1
                    continue
                if c2 in ("−", "-") and dokunur(t, t2) and pdf.ch[t2] == "→":
                    oklar.append("⟼")
                    k = t2 + 1
                    continue
                if c2 in OK_KARAKTERLERI:
                    return None
            k += 1
            continue
        if c in ("=", "−", "-"):
            if dokunur(k, t) and pdf.ch[t] in OK_KARAKTERLERI:
                birlesik = _BIRLESIK_OK.get((c, pdf.ch[t]))
                if birlesik is None:
                    return None
                oklar.append(birlesik)
                k = t + 1
                continue
            k += 1
            continue
        if c in OK_KARAKTERLERI:
            if dokunur(k, t) and (pdf.ch[t] in OK_KARAKTERLERI or pdf.ch[t] in ("=", "−", "-")):
                birlesik = _BIRLESIK_OK.get((c, pdf.ch[t]))
                if birlesik is None:
                    return None
                oklar.append(birlesik)
                k = t + 1
                continue
            o = onceki(k)
            if dokunur(o, k) and not pdf.ch[o].isalnum():
                # Okun önünde ona dokunan tanımadığımız bir işaret var.
                return None
            oklar.append(c)
        k += 1
    return oklar


def _latex_oklari(tex: str, taban: int) -> List[Tuple[int, int, str]]:
    """LaTeX'teki ok komutları: (md başlangıç, md bitiş, anlam)."""
    out = []
    for m in re.finditer(r"\\([A-Za-z]+)", tex):
        anlam = _LATEX_OK.get(m.group(1))
        if anlam:
            out.append((taban + m.start(), taban + m.end(), anlam))
    for m in re.finditer("[" + "".join(sorted(OK_KARAKTERLERI)) + "]", tex):
        out.append((taban + m.start(), taban + m.end(), m.group(0)))
    out.sort()
    return out


def _harfler_tutuyor_mu(pdf: _PdfAkis, a: int, b: int, tex: str) -> bool:
    """Bölge doğrulaması: PDF bölgesinin harfleri LaTeX'inkilerle aynı ya da
    (en az DENKLEM_EN_AZ_ESLESEN_HARF aynı harfle) en çok bir farklı parça."""
    ph = [pdf.norm[t] for t in range(a, b) if pdf.norm[t].isalpha()]
    lh = [x[1] for x in _latex_harfleri(tex, 0)]
    if not ph and not lh:
        return True
    sm = difflib.SequenceMatcher(None, ph, lh, autojunk=False)
    farklar = [op for op in sm.get_opcodes() if op[0] != "equal"]
    esit = sum(op[2] - op[1] for op in sm.get_opcodes() if op[0] == "equal")
    return not farklar or (len(farklar) <= 1 and esit >= DENKLEM_EN_AZ_ESLESEN_HARF)


def _matematik_duzelt(md: str, pdf: _PdfAkis, rapor: OnarimRaporu) -> str:
    """Matematikte PDF'le birebir karşılığı olan ok simgesi ve ondalık ayraç
    farklarını düzelt (Marker'ın formül tanıması: "⇐" -> \\iff, "0,24" ->
    "0.24")."""
    ak = _md_akisi_kur(md)
    duz: List[Tuple[int, int, str, Degisiklik]] = []
    for bas, son, pa, pb in _matematik_bolgeleri(pdf, ak, md):
        tex = md[bas:son]
        k = pdf.kaynak[pa]
        if not _harfler_tutuyor_mu(pdf, pa, pb, tex):
            rapor.atla("matematik_bolgesi_tutmadi")
            continue
        # Ondalık ayraç: sayı belirteçlerinin rakamları birebir aynıysa
        # LaTeX'teki "." PDF'te "," olan yerde "," olur.
        ls = [(m.start(), m.group(0)) for m in _SAYI_RE.finditer(tex)]
        ps = [m.group(0) for m in _SAYI_RE.finditer(pdf.norm[pa:pb])]
        if ls and len(ls) == len(ps) and all(
                re.split(r"[.,]", x) == re.split(r"[.,]", y) for (_, x), y in zip(ls, ps)):
            for (ofs, x), y in zip(ls, ps):
                for t, (cx, cy) in enumerate(zip(x, y)):
                    if cx == "." and cy == ",":
                        o = bas + ofs + t
                        duz.append((o, o + 1, ",", Degisiklik(
                            "ondalik_virgul", o, o + 1, ".", ",", pdf.sayfa[k], k, k)))
        # Ok simgesi: sayıları eşitse farklı olanlar PDF'teki ok olur.
        po = _pdf_oklari(pdf, pa, pb)
        lo = _latex_oklari(tex, bas)
        if po is None:
            rapor.atla("ok_belirsiz")
        elif lo and len(po) == len(lo):
            for (o0, o1, anlam), pdf_ok in zip(lo, po):
                if anlam != pdf_ok:
                    komut = _OK_KOMUTU[pdf_ok]
                    # Ardından harf gelirse komut ona yapışmasın.
                    if o1 < len(md) and md[o1].isalpha():
                        komut += " "
                    duz.append((o0, o1, komut, Degisiklik(
                        "denklem_ok", o0, o1, md[o0:o1], komut, pdf.sayfa[k], k, k)))
        elif lo or po:
            rapor.atla("ok_sayisi_farkli")
    return _uygula(md, duz, rapor) if duz else md


def _yalniz_kayitli_matematik_mi(once: str, sonra: str,
                                 degisiklikler: Sequence[Degisiklik]) -> bool:
    """sonra, once'e kaydedilen matematik değişiklikleri (yalnız "." -> ","
    ve ok komutu -> ok komutu, yalnız matematik içinde) uygulanmış hâli mi?"""
    ak = _md_akisi_kur(once)
    araliklar = [(ak.bas[e], ak.son[e]) for e, ch in enumerate(ak.norm)
                 if ch == _ENGEL and once.startswith("$", ak.bas[e])]
    parcalar = []
    son = 0
    for d in sorted(degisiklikler, key=lambda d: d.md_bas):
        if d.md_bas < son or once[d.md_bas:d.md_son] != d.once:
            return False
        if not any(a <= d.md_bas and d.md_son <= b for a, b in araliklar):
            return False
        if d.tur == "ondalik_virgul":
            if (d.once, d.sonra) != (".", ","):
                return False
        elif d.tur == "denklem_ok":
            if d.once.lstrip("\\") not in _LATEX_OK and d.once not in OK_KARAKTERLERI:
                return False
            if d.sonra.strip() not in _OK_KOMUTU.values():
                return False
        else:
            return False
        parcalar.append(once[son:d.md_bas])
        parcalar.append(d.sonra)
        son = d.md_son
    parcalar.append(once[son:])
    return "".join(parcalar) == sonra


def _yalniz_harf_ikamesi_mi(once: str, sonra: str,
                            degisiklikler: Sequence[Degisiklik]) -> bool:
    """sonra, once'den yalnız kaydedilen tek harf ikameleriyle mi ayrılıyor?"""
    if len(once) != len(sonra):
        return False
    izinli = {(d.md_bas, d.once, d.sonra) for d in degisiklikler}
    for i, (x, y) in enumerate(zip(once, sonra)):
        if x != y and (i, x, y) not in izinli:
            return False
    return True


# ---------------------------------------------------------------------------
# Genel giriş
# ---------------------------------------------------------------------------

# Kayıpsızlık denetiminde görünür içerik: işaretleme (simge etiketi, satır
# kırığı, matematik sınırlayıcısı, \vec/\mathcal/\dot/\ddot sarmalı, vurgu,
# boşluk) atılır; gösterim eşdeğerleri (◦/∘ = °, − = -) birleştirilir.
_KAYIPSIZ_ATILAN_RE = re.compile(
    r"</?su[pb]>|<br\s*/?\s*>|\\(?:vec|mathcal|mathrm|text|dot|ddot)\{|[\s*_$~{}^˙¨]",
    re.IGNORECASE)
_KAYIPSIZ_ESDEGER = str.maketrans({"◦": DERECE, "∘": DERECE, "−": "-"})


def _gorunur(metin: str) -> str:
    return _KAYIPSIZ_ATILAN_RE.sub("", metin).translate(_KAYIPSIZ_ESDEGER)


def _kayipsiz_mi(once: str, sonra: str) -> bool:
    """Görünür içerik satır satır aynı mı? (Tablo satırında yalnız hücre içi
    yeniden sıralama olduğundan karakter çoklu kümesi karşılaştırılır.)"""
    a, b = once.split("\n"), sonra.split("\n")
    if len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if x == y:
            continue
        gx, gy = _gorunur(x), _gorunur(y)
        if gx == gy:
            continue
        if x.lstrip().startswith("|") and sorted(gx) == sorted(gy):
            continue
        return False
    return True


def onar_raporlu(markdown: str, pdf_path: Optional[str]
                 ) -> Tuple[str, OnarimRaporu]:
    """``onar`` + değişiklik raporu (korpus farksalı ve testler için)."""
    rapor = OnarimRaporu()
    if not markdown or not pdf_path:
        return markdown, rapor
    p = Path(str(pdf_path))
    if p.suffix.lower() != ".pdf" or not p.is_file():
        return markdown, rapor
    pdf = _pdf_akisi_kur(str(p))
    if pdf is None:
        return markdown, rapor
    rapor.pdf_var = True
    # Sıra: tablo hücresi önce (hücre içi yeniden sıralama, simge bağlamını
    # PDF'le hizalar: "km3<br>−2<br>s" -> "km<sup>3</sup> s<sup>−2</sup>"
    # olunca aynı satırdaki "1011" de iki yanlı bağlam bulur).
    md = _italik_simge_duzelt(markdown, rapor)
    md = _tablo_hucre_duzelt(md, pdf, rapor)
    md = _simge_duzelt(md, pdf, rapor)
    md = _simge_ve_harf_duzelt(md, pdf, rapor)
    if not _kayipsiz_mi(markdown, md):
        # Güvenlik ağı: görünür içerik değiştiyse hiçbir düzenleme uygulanmaz.
        logger.warning("G1c: kayıpsızlık denetimi tutmadı; Markdown aynen "
                       "bırakıldı (%d düzenleme geri alındı)",
                       len(rapor.degisiklikler))
        rapor.atla("kayipsizlik_denetimi")
        rapor.degisiklikler.clear()
        return markdown, rapor
    # Denklem harfi en son ve AYRI denetimle: görünür içeriği bilerek
    # değiştirir (yanlış OCR harfi -> PDF'teki harf), o yüzden yukarıdaki ağ
    # yerine yalnız kaydedilen ikamelere izin veren denetimden geçer.
    denklem_raporu = OnarimRaporu()
    ara = _denklem_harf_duzelt(md, pdf, denklem_raporu)
    for neden, sayi in denklem_raporu.atlanan.items():
        rapor.atlanan[neden] = rapor.atlanan.get(neden, 0) + sayi
    if ara is not md:
        if _yalniz_harf_ikamesi_mi(md, ara, denklem_raporu.degisiklikler):
            md = ara
            rapor.degisiklikler.extend(denklem_raporu.degisiklikler)
        else:
            rapor.atla("denklem_denetimi")
    if rapor.degisiklikler:
        logger.info("G1c: PDF metin katmanıyla %d simge onarıldı %s",
                    len(rapor.degisiklikler), rapor.sayim())
    return md, rapor


def onar(markdown: str, pdf_path: Optional[str]) -> str:
    """Marker Markdown'ındaki satır içi simge hatalarını kaynak PDF'le onar.

    pdf_path yoksa, PDF açılamıyorsa ya da metin katmanı yoksa (taranmış
    PDF) Markdown aynen döner. Hata durumunda da Markdown aynen döner
    (onarım isteğe bağlı bir iyileştirmedir; boru hattını durdurmaz).
    """
    try:
        return onar_raporlu(markdown, pdf_path)[0]
    except Exception as exc:  # noqa: BLE001 - onarım boru hattını durdurmaz
        logger.warning("G1c: PDF metin onarımı atlandı (%s): %s",
                       type(exc).__name__, exc, exc_info=True)
        return markdown


# ---------------------------------------------------------------------------
# FX2 (2026-10-05): ek geçiş — matematik ve tablo hücresi
# ---------------------------------------------------------------------------

def matematik_tablo_onar_raporlu(markdown: str, pdf_path: Optional[str]
                                 ) -> Tuple[str, OnarimRaporu]:
    """``matematik_tablo_onar`` + rapor.

    ``onar``dan AYRI bir giriştir (G1c'nin kanıt testleri ``onar``ın
    çıktısını sabitler): boru hattında dönüştürücüden ÖNCE, ham Marker
    Markdown'ı üzerinde çağrılır (translate_book.run_pipeline). Adımlar:
    1. Marker'ın böldüğü satır içi matematik (RAPOR MT-8: "d*t*", "*m*˙")
       ve tablo hücresindeki düz rakam dipnot işareti (FX2) — görünür
       içerik aynı kalır (tablo satırında karakter çoklu kümesi), G1c'nin
       kayıpsızlık ağından geçer;
    2. matematikte ok simgesi (MT-46) ve ondalık ayraç (MT-14) — görünür
       içeriği bilerek değiştirir, yalnız kaydedilen matematik
       değişikliklerine izin veren denetimden geçer.
    """
    rapor = OnarimRaporu()
    if not markdown or not pdf_path:
        return markdown, rapor
    p = Path(str(pdf_path))
    if p.suffix.lower() != ".pdf" or not p.is_file():
        return markdown, rapor
    pdf = _pdf_akisi_kur(str(p))
    if pdf is None:
        return markdown, rapor
    rapor.pdf_var = True
    md = _matematik_italik_duzelt(markdown, pdf, rapor)
    md = _tablo_dipnot_duzelt(md, pdf, rapor)
    if not _kayipsiz_mi(markdown, md):
        logger.warning("FX2: matematik/tablo onarımında kayıpsızlık denetimi "
                       "tutmadı; %d düzenleme geri alındı", len(rapor.degisiklikler))
        rapor.atla("kayipsizlik_denetimi")
        rapor.degisiklikler.clear()
        md = markdown
    mat_raporu = OnarimRaporu()
    ara = _matematik_duzelt(md, pdf, mat_raporu)
    for neden, sayi in mat_raporu.atlanan.items():
        rapor.atlanan[neden] = rapor.atlanan.get(neden, 0) + sayi
    if ara is not md:
        if _yalniz_kayitli_matematik_mi(md, ara, mat_raporu.degisiklikler):
            md = ara
            rapor.degisiklikler.extend(mat_raporu.degisiklikler)
        else:
            rapor.atla("matematik_denetimi")
    if rapor.degisiklikler:
        logger.info("FX2: PDF metin katmanıyla %d matematik/tablo onarımı %s",
                    len(rapor.degisiklikler), rapor.sayim())
    return md, rapor


def matematik_tablo_onar(markdown: str, pdf_path: Optional[str]) -> str:
    """Marker'ın matematik ve tablo hücresi hatalarını kaynak PDF'le onar.

    PDF yoksa / açılamıyorsa / metinsizse ya da hata olursa Markdown aynen
    döner (onarım isteğe bağlı bir iyileştirmedir; boru hattını durdurmaz).
    """
    try:
        return matematik_tablo_onar_raporlu(markdown, pdf_path)[0]
    except Exception as exc:  # noqa: BLE001 - onarım boru hattını durdurmaz
        logger.warning("FX2: matematik/tablo onarımı atlandı (%s): %s",
                       type(exc).__name__, exc, exc_info=True)
        return markdown
