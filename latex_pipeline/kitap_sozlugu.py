"""Kitap sözlüğü (KARAR 25, 5 Ekim 2026).

"Tek veya birkaç kitaba göre sözlük olmaz, tüm kitaplar için düşünmeliyiz":
sabit genel alan sözlüğü YOKTUR. Her kitap kendi terim sözlüğünü KAYNAK
DİLDE (en/de/fr/es/it/ru) çıkarır; dosya kitap klasöründe
``glossary_shared.tsv``'dir. Biçim "PDF'den TeX çıkar" yolunun
(CEVIRI_TALIMATI.md Adım 3) biçimiyle birebir aynıdır: UTF-8 (BOM'suz),
satır başına ``kaynak terim<TAB>Türkçe`` (isteğe bağlı üçüncü sütun: not),
başlık satırı yok, LF satır sonu. İki yol aynı dosyayı okur/yazar.

Bölümler:

1. Aday terim çıkarımı (:func:`aday_terimler`) — LaTeX gövdeden (matematik,
   komut ve atıf maskelenir) kaynak dilde; durak kelimeler
   ``core.lang_detect.frequent_words(dil)``; 1–4 sözcüklü, kenarı durak
   kelime olmayan öbekler + cümle içi büyük harfli terimler; sıklık eşiği;
   en çok :data:`ADAY_EN_COK` aday. Deterministik.
2. Terim çevirisi (:func:`terimleri_cevir`) — her aday BİR kez, ilk geçtiği
   cümleyle bağlamlı, seçili yerel motorun genel çeviri çağrısıyla
   (``BookTranslator.translate_batch_raw``; bütün arka uçlarda çalışır,
   koruma zinciri ve çeviri belleği YOK). İngilizce kitapta
   ``glossary/curated.json`` (elle doğrulanmış) başlangıçtır ve modelin
   önüne geçer.
3. Uygulama — (a) istem düzeyi: :meth:`KitapSozlugu.istem_bloku` segmentte
   geçen terimlerin karşılıklarını talimata ekler (HY-MT'nin resmî
   "terminology intervention" biçimi; VARSAYILAN KAPALI, bkz.
   :data:`ISTEM_TERIMLERI_VARSAYILAN`); (b) :meth:`KitapSozlugu.verify_terms`
   yalnız çıktıda ÖZGÜN dilde kalmış terimi tam eşleşmeyle karşılığına
   çevirir; (c) :func:`tutarlilik_bulgulari` QC'ye "aynı kaynak terime
   farklı karşılık" UYARILARINI verir (otomatik değiştirme yok).

Modül yalnız standart kütüphaneye ve ``core.lang_detect``'e dayanır; model
yüklemez. Çevirmen nesnesi dışarıdan verilir (ördek tipleme:
``translate_batch_raw(metinler, max_length)``).
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sabitler — eşikler 6 dilli test korpusunda ölçüldü (G2c, 2026-10-05; ölçüm
# tablosu ve betiği iç denetim notunda).
# ---------------------------------------------------------------------------

#: Kitap klasöründeki sözlük dosyasının adı (TeX yoluyla ortak).
SOZLUK_DOSYA_ADI = "glossary_shared.tsv"

#: Desteklenen kaynak diller (TeX yolu ile aynı altı dil).
KAYNAK_DILLER: Tuple[str, ...] = ("en", "de", "fr", "es", "it", "ru")

#: En çok aday terim sayısı (K). CEVIRI_TALIMATI.md Adım 3: "50–100 terim
#: yeter"; her aday bir model çağrısı demek (çeviri süresi).
ADAY_EN_COK = 100

#: Bir öbekteki en çok sözcük.
OBEK_EN_COK_SOZCUK = 4

#: Çok sözcüklü öbeğin aday olması için en az geçiş sayısı.
OBEK_EN_AZ_SIKLIK = 2

#: Tek sözcüklü adayın en az geçiş sayısı (tek sözcükte genel söz
#: gürültüsü öbekten fazla; eşik daha yüksek).
TEK_SOZCUK_EN_AZ_SIKLIK = 3

#: Küçük harfle yazılan tek sözcüklü adayın en az harf sayısı. Genel dil
#: sözcükleri (time, way, people; tiempo, cosa) kısa; teknik terimler
#: (eccentricity, anomaly, perihelion) uzun olma eğiliminde.
TEK_SOZCUK_EN_AZ_HARF = 7

#: Almancada her ad büyük harfle başlar: tek sözcüklü Almanca adın aday
#: olması için en az harf (Zeit, Jahr, Teil gibi genel adlar elenir;
#: Wärmeleitung, Grenzschicht gibi bileşik terimler kalır).
ALMANCA_TEK_AD_EN_AZ_HARF = 9

#: Öbek kenarındaki (ilk/son) sözcüğün en az harf sayısı.
KENAR_SOZCUK_EN_AZ_HARF = 3

#: İlk geçtiği cümlenin (bağlamın) en çok karakteri; uzun cümlede terimin
#: çevresinden bu kadarlık pencere alınır.
BAGLAM_EN_COK_KARAKTER = 320

# ---------------------------------------------------------------------------
# Belirteçleme
# ---------------------------------------------------------------------------

# Harf dizisi; iç kısa çizgi ve kesme işareti sözcüğün parçası
# ("Gun-Club", "semi-latus", "Kepler's", "l'orbite" -> aşağıda bölünür).
_SOZCUK_RE = re.compile(r"[^\W\d_]+(?:[-‐'’][^\W\d_]+)*")

# Fransızca / İtalyanca eksiltme önekleri: "l'orbite" -> "l'" + "orbite".
_ELISION_RE = re.compile(
    r"^(l|d|j|m|n|s|t|c|qu|jusqu|lorsqu|puisqu|quoiqu|dell|all|nell|sull"
    r"|dall|coll|un|quell|bell|sant|tutt|po)['’](.+)$",
    re.IGNORECASE,
)

# Öbek sınırı: noktalama, parantez, süslü/köşeli ayraç, tırnaklar, tireler.
_OBEK_SINIRI_RE = re.compile(r"[,;:()\[\]{}\"“”„«»‹›?!.…–—/\\|<>=+*&%#~^_$]")

# Cümle sonu: nokta/ünlem/soru (+ kapanış tırnağı/parantezi) ve boşluk.
_CUMLE_SONU_RE = re.compile(r"(?<=[.!?…])[\"”»’)]*\s+")

# Paragraf ayırıcı (boş satır).
_PARAGRAF_RE = re.compile(r"\n[ \t]*\n")


# Başlık komutları (aday çıkarımında atlanır): başlıklar Title Case yazılır
# ("Energy of the Orbit", "Circular Orbits"); büyük harf orada özel ad
# kanıtı değildir ve başlık cümle değildir (bağlam olamaz). Başlıktaki terim
# gövdede de geçer, sayımı oradan gelir.
_BASLIK_RE = re.compile(
    r"\\(?:part|chapter|section|subsection|subsubsection|paragraph"
    r"|subparagraph)\*?\s*(?:\[[^\]\n]*\])?\s*\{[^{}\n]*\}"
)

# Satır içi biçim komutları (argümanı düzyazıdır; süslüsü öbek sınırı değil).
_VURGU_RE = re.compile(
    r"\\(?:emph|textbf|textit|textsl|textsc|textup|textmd|textrm|textsf"
    r"|texttt|underline|uline)\*?\{([^{}\n]*)\}"
)


def _durak_kelimeler(dil: str) -> frozenset:
    """Dilin durak kelimeleri: ``core.lang_detect.frequent_words(dil)``."""
    try:
        from core.lang_detect import frequent_words
    except Exception:  # pragma: no cover - core her zaman var
        return frozenset()
    return frozenset(w.lower() for w in frequent_words(dil))


def govdeyi_temizle(latex: str) -> str:
    """LaTeX gövdeden düzyazıyı ayıkla (satır hizası korunur).

    ``core.lang_detect.clean_latex_for_detection`` kullanılır: matematik,
    kod, yorum, URL ve argümanı düzyazı olmayan komutlar (``\\label``,
    ``\\ref``, ``\\cite``, ``\\includegraphics`` …) silinir; komut adları
    silinir, metin argümanları (``\\textbf{…}``, ``\\footnote{…}``,
    ``\\section{…}``) kalır.
    """
    if not latex:
        return ""
    from core.lang_detect import clean_latex_for_detection
    # Vurgu/biçim komutları açılır (GLS-13): "\\emph{gradient descent}"
    # süslüsü öbek sınırı sayılırsa önündeki belirleyici ("The") kaybolur.
    latex = _VURGU_RE.sub(r"\1", latex)
    temiz = clean_latex_for_detection(latex)
    # Marker'ın HTML kalıntıları (<sup>, <span ...>) sözcük sayılmasın.
    temiz = re.sub(r"<[^>\n]{0,80}>", " ", temiz)
    return temiz


def _cumleler(temiz: str) -> List[str]:
    """Temiz metni cümlelere böl (paragraf sınırı cümle sınırıdır)."""
    out: List[str] = []
    for paragraf in _PARAGRAF_RE.split(temiz):
        tek = re.sub(r"\s+", " ", paragraf).strip()
        if not tek:
            continue
        for cumle in _CUMLE_SONU_RE.split(tek):
            cumle = cumle.strip()
            if cumle:
                out.append(cumle)
    return out


def _belirtec_araliklari(parca: str) -> List[Tuple[str, int]]:
    """Sözcükler ve parça içindeki BİTİŞ konumları (eksiltme önekleri ayrılır)."""
    out: List[Tuple[str, int]] = []
    for m in _SOZCUK_RE.finditer(parca):
        tok = m.group(0)
        em = _ELISION_RE.match(tok)
        if em:
            out.append((em.group(1) + "'", m.start() + len(em.group(1)) + 1))
            tok = em.group(2)
        out.append((tok, m.end()))
    return out


def _belirtecler(parca: str) -> List[str]:
    """Bir öbek parçasının sözcükleri (eksiltme önekleri ayrılır)."""
    return [t for t, _son in _belirtec_araliklari(parca)]


# ---------------------------------------------------------------------------
# Hafif biçim birleştirme (yalnız SAYIM için; yüzey biçim korunur)
# ---------------------------------------------------------------------------
# Aynı terimin çekimli biçimleri (Almanca çoğul, Rusça hâl ekleri, İtalyanca
# tekil/çoğul) sıklığı bölmesin diye sayım anahtarı kaba bir kök kullanır.
# Sözlüğe yazılan biçim kitapta EN SIK geçen yüzey biçimdir (kök değil).

_RU_EKLER = (
    "ами", "ями", "ого", "его", "ому", "ему", "ыми", "ими", "иях", "ах",
    "ях", "ой", "ей", "ом", "ем", "ую", "юю", "ая", "яя", "ое", "ее", "ые",
    "ие", "ый", "ий", "ов", "ев", "ам", "ям", "а", "я", "у", "ю", "е", "ы",
    "и", "о", "ь", "й",
)

#: Kök kesmede sözcükte en az kalacak harf (kısa sözcük kesilmez).
_KOK_EN_AZ_HARF = 4


def _sayim_koku(sozcuk: str, dil: str) -> str:
    """Sayım anahtarı için kaba kök (küçük harf)."""
    w = sozcuk.lower()
    if len(w) <= _KOK_EN_AZ_HARF:
        return w
    if dil == "ru":
        for ek in _RU_EKLER:
            if w.endswith(ek) and len(w) - len(ek) >= _KOK_EN_AZ_HARF:
                return w[: -len(ek)]
        return w
    if dil == "de":
        for ek in ("en", "es", "er", "n", "s", "e"):
            if w.endswith(ek) and len(w) - len(ek) >= _KOK_EN_AZ_HARF:
                return w[: -len(ek)]
        return w
    if dil == "it":
        if w[-1] in "aeio":
            return w[:-1]
        return w
    if dil in ("en", "fr", "es"):
        if w.endswith("ies") and dil == "en":
            return w[:-3] + "y"
        if w.endswith("es") and dil == "es" and len(w) - 2 >= _KOK_EN_AZ_HARF:
            return w[:-2]
        if w.endswith("s") and not w.endswith("ss"):
            return w[:-1]
        return w
    return w


# ---------------------------------------------------------------------------
# Aday terim çıkarımı
# ---------------------------------------------------------------------------
# Dilbilgisi sabitleri (sözlük DEĞİL — terim karşılığı taşımazlar): ad
# öbeğini işlev sözcüğü dizisinden ayırmak için belirleyiciler (tanımlık,
# iyelik, gösterme) ve öbek içinde izin verilen bağlayıcı edatlar.
# Korpus ölçümü (RAPOR §1): yalnız durak kelime listesiyle "quelques",
# "nuestro", "tuttavia", "obtained" gibi işlev/genel sözcükler ilk 30'a
# giriyordu; belirleyici oranı bunları eler, adları ("the velocity",
# "le mortier", "il telescopio") tutar.

_BELIRLEYICILER: Dict[str, frozenset] = {
    "en": frozenset({
        "the", "a", "an", "this", "that", "these", "those", "its", "their",
        "his", "her", "our", "each", "every", "any", "no", "whose",
    }),
    "fr": frozenset({
        "le", "la", "les", "l'", "un", "une", "des", "du", "au", "aux", "ce",
        "cet", "cette", "ces", "son", "sa", "ses", "leur", "leurs", "notre",
        "nos", "votre", "vos", "mon", "ma", "mes", "d'",
    }),
    "es": frozenset({
        "el", "la", "los", "las", "un", "una", "unos", "unas", "del", "al",
        "este", "esta", "estos", "estas", "ese", "esa", "esos", "esas",
        "aquel", "aquella", "su", "sus", "nuestro", "nuestra", "nuestros",
        "nuestras", "lo",
    }),
    "it": frozenset({
        "il", "lo", "la", "i", "gli", "le", "l'", "un", "uno", "una", "un'",
        "del", "dello", "della", "dei", "degli", "delle", "dell'", "al",
        "allo", "alla", "ai", "agli", "alle", "all'", "nel", "nello",
        "nella", "nei", "negli", "nelle", "nell'", "sul", "sulla", "sui",
        "questo", "questa", "questi", "queste", "quel", "quella", "suo",
        "sua", "suoi", "sue", "loro", "dal", "dalla", "dai", "dalle",
    }),
    "de": frozenset({
        "der", "die", "das", "den", "dem", "des", "ein", "eine", "einen",
        "einem", "einer", "eines", "dieser", "diese", "dieses", "diesen",
        "diesem", "sein", "seine", "seinen", "seinem", "seiner", "ihr",
        "ihre", "ihren", "ihrem", "ihrer", "unser", "unsere", "jeder",
        "jede", "jedes", "zum", "zur", "im", "am", "vom", "beim",
    }),
    "ru": frozenset(),  # Rusçada tanımlık yok: aşağıdaki sıfat eki kuralı
}

#: Öbeğin İÇİNDE (kenarda değil) izin verilen bağlayıcı edatlar (en çok
#: bir tane): "equation of motion", "armes à feu", "velocidad de escape",
#: "pluralità dei mondi". Bağlaç ("und", "and", "et") öbek kurmaz.
_IC_BAGLAYICILAR: Dict[str, frozenset] = {
    "en": frozenset({"of"}),
    "fr": frozenset({"de", "du", "des", "d'", "à", "en"}),
    "es": frozenset({"de", "del", "en"}),
    "it": frozenset({"di", "del", "dei", "degli", "della", "delle", "dello",
                     "d'", "dell'", "a", "da"}),
    "de": frozenset(),
    "ru": frozenset(),
}

#: Rusça sıfat ekleri: tek başına sıfat ("удельный", "рабочего") terim
#: değil, çok sözcüklü terimin parçasıdır ("удельный импульс").
_RU_SIFAT_EKLERI: Tuple[str, ...] = (
    "ый", "ий", "ой", "ая", "яя", "ое", "ее", "ые", "ие", "ого", "его",
    "ому", "ему", "ым", "им", "ых", "их", "ую", "юю", "ыми", "ими",
)

#: Rusça dönüşlü fiil/ortaç ekleri: terim adı öbeğinde fiil olmaz
#: ("является", "меняется", "остаётся" cümle parçası kurar).
_RU_DONUSLU_EKLER: Tuple[str, ...] = ("ся", "сь")

#: Dönüştürme artığı belirteçler (EPUB/HTML kalıntısı); aday olamaz.
_ARTIK_BELIRTECLER = frozenset({"xhtml", "html", "htm", "http", "https", "www"}
                               | {
    # Şekil/tablo/denklem atıf etiketleri (V2 bulgusu Y5, 5 Ekim: saat
    # kitabında "Fig" 1. aday çıkıyordu). Altı kaynak dil, kısaltmalar dahil.
    "fig", "figs", "figure", "figures", "tab", "table", "tables", "eq", "eqs",
    "equation", "equations", "abb", "abbildung", "tabelle", "gl", "gleichung",
    "bild", "tableau", "équation", "tabla", "ecuación", "tabella", "equazione",
    "рис", "рисунок", "табл", "таблица", "ур",
})

#: Belirleyici oranı eşiği: büyük harfli olmayan adayın geçişlerinin en az
#: bu kadarında hemen önünde bir belirleyici bulunmalı (ad öbeği sinyali).
#: Ölçüm: terimlerde 0,35–0,90; işlev/genel sözcüklerde 0,00–0,15.
BELIRLEYICI_EN_AZ_ORAN = 0.25

#: Ad öbeğinin sonunda bitme oranı: küçük harfli tek sözcüklü adayın
#: geçişlerinin en az bu kadarında ardından içerik sözcüğü GELMEMELİ (öbek
#: sonu, durak kelime ya da noktalama). Ad önü sıfatlar ("specific",
#: "initial", "gravitational", "nouveau", "grandes", "semplice") hemen
#: hep bir adın önünde durur: ölçümde 0,00–0,17; adlar 0,25–1,00
#: (RAPOR §1). Almanca (ad büyük harfli) ve Rusça (sıfat eki kuralı) dışı.
OBEK_SONU_EN_AZ_ORAN = 0.25

#: Geçişlerinin bu oranından fazlasında hemen ardından sayı gelen tek
#: sözcük atıf etiketidir ("Figure 3", "Abbildung 2", "Таблица 1"); aday
#: olamaz (başlık/altyazı çevirisi kendi boru hattında).
RAKAM_ONU_EN_COK_ORAN = 0.5


@dataclass
class TerimAdayi:
    """Kitaptan çıkarılan aday terim (kaynak dilde)."""

    terim: str                  # kitapta en sık geçen yüzey biçimi
    siklik: int                 # (kök anahtarıyla) geçiş sayısı
    sozcuk_sayisi: int
    ilk_cumle: str              # ilk geçtiği cümle (bağlam)
    buyuk_harfli: bool = False  # cümle içinde büyük harfle geçiyor
    puan: float = 0.0           # sıralama puanı (C-değeri benzeri)


def _buyuk_harf(sozcuk: str) -> bool:
    return bool(sozcuk) and sozcuk[0].isupper()


def _baglam_penceresi(cumle: str, terim: str) -> str:
    """Uzun cümlede terimin çevresinden :data:`BAGLAM_EN_COK_KARAKTER`."""
    if len(cumle) <= BAGLAM_EN_COK_KARAKTER:
        return cumle
    i = cumle.lower().find(terim.lower())
    if i < 0:
        return cumle[:BAGLAM_EN_COK_KARAKTER].rstrip()
    yari = (BAGLAM_EN_COK_KARAKTER - len(terim)) // 2
    bas = max(0, i - yari)
    son = min(len(cumle), bas + BAGLAM_EN_COK_KARAKTER)
    bas = max(0, son - BAGLAM_EN_COK_KARAKTER)
    # Sözcük ortasından başlamasın / bitmesin.
    if bas > 0:
        bosluk = cumle.find(" ", bas)
        if 0 <= bosluk < i:
            bas = bosluk + 1
    if son < len(cumle):
        bosluk = cumle.rfind(" ", i + len(terim), son)
        if bosluk > 0:
            son = bosluk
    return cumle[bas:son].strip()


def _obek_gecerli(toks: Sequence[str], alt: Sequence[str], i: int, j: int,
                  durak: frozenset, baglayici: frozenset, dil: str) -> bool:
    """``toks[i:j]`` aday öbek olabilir mi (kenar + iç sözcük kuralları)."""
    ilk, son = alt[i], alt[j - 1]
    if ilk in durak or son in durak:
        return False
    if (len(toks[i]) < KENAR_SOZCUK_EN_AZ_HARF
            or len(toks[j - 1]) < KENAR_SOZCUK_EN_AZ_HARF
            or toks[i].endswith("'") or toks[j - 1].endswith("'")):
        return False
    if any(t in _ARTIK_BELIRTECLER for t in alt[i:j]):
        return False
    ic_durak = [t for t in alt[i + 1:j - 1] if t in durak or t.endswith("'")]
    if ic_durak and (len(ic_durak) > 1 or ic_durak[0] not in baglayici):
        return False
    if dil == "de" and j - i > 1 and not _buyuk_harf(toks[j - 1]):
        # Almanca ad öbeğinin başı (son sözcük) addır: büyük harf.
        return False
    if dil == "ru" and any(t.endswith(_RU_DONUSLU_EKLER) for t in alt[i:j]):
        return False
    return True


def aday_terimler(latex_govde: str, kaynak_dil: str,
                  en_cok: int = ADAY_EN_COK) -> List[TerimAdayi]:
    """LaTeX gövdeden kaynak dilde aday terimleri çıkar (deterministik).

    Kurallar (eşikler modül başındaki adlı sabitler):

    * Gövde :func:`govdeyi_temizle` ile düzyazıya indirgenir (matematik,
      komut, atıf maskelenir; başlık komutları atlanır — Title Case özel ad
      kanıtı değildir); cümlelere, cümleler noktalamada öbek parçalarına
      bölünür.
    * 1–:data:`OBEK_EN_COK_SOZCUK` sözcüklü n-gram'lar; ilk ve son sözcük
      durak kelime (``core.lang_detect.frequent_words``) olamaz ve en az
      :data:`KENAR_SOZCUK_EN_AZ_HARF` harflidir; içeride en çok bir
      bağlayıcı edat olabilir ("speed of light", "armes à feu").
    * Çok sözcüklü öbek :data:`OBEK_EN_AZ_SIKLIK`, tek sözcük
      :data:`TEK_SOZCUK_EN_AZ_SIKLIK` kez geçmeli.
    * Cümle içinde büyük harfle geçen aday (özel ad, adlandırılmış terim,
      kısaltma) doğrudan kabul edilir. Küçük harfli adayda (en/fr/es/it/de)
      geçişlerin en az :data:`BELIRLEYICI_EN_AZ_ORAN` kadarının önünde
      belirleyici olmalı (ad öbeği sinyali); Almancada tek sözcüklü ad en
      az :data:`ALMANCA_TEK_AD_EN_AZ_HARF` harfli, öbeğin son sözcüğü
      büyük harfli; Rusçada tek başına sıfat aday olamaz. Küçük harfli tek
      sözcük en az :data:`TEK_SOZCUK_EN_AZ_HARF` harflidir ve (en/fr/es/
      it) geçişlerinin en az :data:`OBEK_SONU_EN_AZ_ORAN` kadarında ad
      öbeğini bitirir (ad önü sıfat elenir).
    * Ardından çoğunlukla sayı gelen tek sözcük atıf etiketidir ("Figure
      3"), aday olamaz.
    * Puan C-değeri benzeridir: ``log2(n+1) * (sıklık - kapsayan uzun
      öbeklerin ortalama sıklığı)``.
    * Sıralama ``(-puan, -sıklık, anahtar)``: aynı girdi her zaman aynı
      listeyi verir.
    """
    dil = (kaynak_dil or "en").lower()
    if dil not in KAYNAK_DILLER:
        # Durak kelime / dilbilgisi listesi yok: aday listesi çöp olurdu.
        logger.warning("Kitap sözlüğü: '%s' desteklenen kaynak dil değil; "
                       "aday çıkarılmadı.", dil)
        return []
    durak = _durak_kelimeler(dil)
    belirleyici = _BELIRLEYICILER.get(dil, frozenset())
    baglayici = _IC_BAGLAYICILAR.get(dil, frozenset())
    temiz = govdeyi_temizle(_BASLIK_RE.sub(" ", latex_govde or ""))
    if not temiz.strip():
        return []

    siklik: Counter = Counter()                       # anahtar -> geçiş
    yuzeyler: Dict[Tuple[str, ...], Counter] = defaultdict(Counter)
    ilk_cumle: Dict[Tuple[str, ...], str] = {}
    ici_buyuk: Counter = Counter()                    # cümle içi büyük harf
    ici_toplam: Counter = Counter()                   # cümle içi geçiş
    belirleyicili: Counter = Counter()                # önünde belirleyici
    rakam_onu: Counter = Counter()                    # ardından sayı
    obek_sonu: Counter = Counter()                    # ardından içerik yok

    for cumle in _cumleler(temiz):
        cumle_basi = True
        for pm in re.finditer(r"[^" + _OBEK_SINIRI_RE.pattern[1:-1] + r"]+",
                              cumle):
            parca = pm.group(0)
            araliklar = _belirtec_araliklari(parca)
            if not araliklar:
                continue
            toks = [t for t, _son in araliklar]
            kokler = [_sayim_koku(t, dil) for t in toks]
            alt = [t.lower() for t in toks]
            # Sözcüğün hemen ardından sayı mı geliyor? ("Figure 3", "Abb. 2")
            rakam_sonra = [
                bool(cumle[pm.start() + son:pm.start() + son + 4]
                     .lstrip(" .~\u00a0")[:1].isdigit())
                for _t, son in araliklar
            ]
            for i in range(len(toks)):
                for n in range(1, OBEK_EN_COK_SOZCUK + 1):
                    j = i + n
                    if j > len(toks):
                        break
                    if not _obek_gecerli(toks, alt, i, j, durak, baglayici,
                                         dil):
                        continue
                    anahtar = tuple(kokler[i:j])
                    yuzey = " ".join(toks[i:j])
                    siklik[anahtar] += 1
                    yuzeyler[anahtar][yuzey] += 1
                    if anahtar not in ilk_cumle:
                        ilk_cumle[anahtar] = cumle
                    if i > 0 and alt[i - 1] in belirleyici:
                        belirleyicili[anahtar] += 1
                    if (j == len(toks) or alt[j] in durak
                            or toks[j].endswith("'")):
                        obek_sonu[anahtar] += 1
                    if rakam_sonra[j - 1]:
                        rakam_onu[anahtar] += 1
                    if not (cumle_basi and i == 0):
                        ici_toplam[anahtar] += 1
                        if all(_buyuk_harf(t) for t in toks[i:j]
                               if t.lower() not in durak):
                            ici_buyuk[anahtar] += 1
            cumle_basi = False

    # --- Aday süzgeci ---
    adaylar: Dict[Tuple[str, ...], TerimAdayi] = {}
    for anahtar, f in siklik.items():
        n = len(anahtar)
        yuzey = _en_sik_yuzey(yuzeyler[anahtar])
        buyuk = (ici_toplam[anahtar] > 0
                 and ici_buyuk[anahtar] * 2 >= ici_toplam[anahtar])
        if f < (TEK_SOZCUK_EN_AZ_SIKLIK if n == 1 else OBEK_EN_AZ_SIKLIK):
            continue
        if n == 1 and rakam_onu[anahtar] > RAKAM_ONU_EN_COK_ORAN * f:
            continue
        if n == 1 and dil == "en" and yuzey.lower().endswith(("'s", "’s")):
            continue  # iyelik tek başına terim değil ("Kepler's"); öbek kalır
        oran = belirleyicili[anahtar] / f
        if dil == "de":
            if n == 1:
                if not buyuk or len(yuzey) < ALMANCA_TEK_AD_EN_AZ_HARF:
                    continue
            elif not buyuk and oran < BELIRLEYICI_EN_AZ_ORAN:
                continue
        elif dil == "ru":
            if n == 1 and not buyuk:
                if len(yuzey) < TEK_SOZCUK_EN_AZ_HARF:
                    continue
                if yuzey.lower().endswith(_RU_SIFAT_EKLERI):
                    continue
        elif not buyuk:
            if n == 1 and len(yuzey) < TEK_SOZCUK_EN_AZ_HARF:
                continue
            if belirleyici and oran < BELIRLEYICI_EN_AZ_ORAN:
                continue
            if n == 1 and obek_sonu[anahtar] < OBEK_SONU_EN_AZ_ORAN * f:
                continue
        adaylar[anahtar] = TerimAdayi(
            terim=yuzey, siklik=f, sozcuk_sayisi=n,
            ilk_cumle=_baglam_penceresi(ilk_cumle[anahtar], yuzey),
            buyuk_harfli=buyuk,
        )

    # --- C-değeri benzeri puan (iç içe öbek düzeltmesi) ---
    kapsayanlar: Dict[Tuple[str, ...], List[int]] = defaultdict(list)
    for uzun, aday in adaylar.items():
        if len(uzun) < 2:
            continue
        for n in range(1, len(uzun)):
            for i in range(len(uzun) - n + 1):
                kisa = uzun[i:i + n]
                if kisa in adaylar:
                    kapsayanlar[kisa].append(aday.siklik)
    for anahtar, aday in adaylar.items():
        agirlik = math.log2(len(anahtar) + 1)
        ic = kapsayanlar.get(anahtar)
        if ic:
            aday.puan = agirlik * (aday.siklik - sum(ic) / len(ic))
        else:
            aday.puan = agirlik * aday.siklik

    sirali = sorted(
        adaylar.items(),
        key=lambda kv: (-kv[1].puan, -kv[1].siklik, kv[0]),
    )
    out: List[TerimAdayi] = []
    for _anahtar, aday in sirali:
        if aday.puan <= 0:
            continue
        out.append(aday)
        if len(out) >= max(0, int(en_cok)):
            break
    return out


def _tumu_buyuk(yuzey: str) -> bool:
    harfler = [c for c in yuzey if c.isalpha()]
    return len(harfler) > 1 and all(c.isupper() for c in harfler)


def _en_sik_yuzey(sayac: Counter) -> str:
    """En sık yüzey biçimi (deterministik).

    Tamamı büyük harfli biçim (başlıktaki "PIANETA MARTE") yalnız başka
    biçim yoksa seçilir (kısaltmalar: "ГОСТ", "NASA"); eşitlikte sözlük
    sırasında ilk.
    """
    adaylar = [kv for kv in sayac.items() if not _tumu_buyuk(kv[0])] \
        or list(sayac.items())
    return sorted(adaylar, key=lambda kv: (-kv[1], kv[0]))[0][0]


# ---------------------------------------------------------------------------
# Sözlük girdileri ve dosya biçimi (TeX yoluyla ortak)
# ---------------------------------------------------------------------------

#: Girdinin kaynağı: yerel motorun bağlamlı çevirisi.
KAYNAK_MODEL = "model"
#: Girdinin kaynağı: ``glossary/curated.json`` (elle doğrulanmış; yalnız
#: İngilizce kitapta).
KAYNAK_CURATED = "curated"
#: Girdinin kaynağı: ``glossary_shared.tsv``'den okundu (kullanıcı, TeX
#: yolu çevirmeni ya da önceki koşu).
KAYNAK_DOSYA = "dosya"


@dataclass
class SozlukGirdisi:
    """Kitap sözlüğünün bir satırı (kaynak dilde terim -> Türkçe)."""

    kaynak: str
    hedef: str
    not_: str = ""
    kaynak_turu: str = KAYNAK_DOSYA


def _tsv_alani(metin: str) -> str:
    """TSV alanı: sekme / satır sonu boşluğa iner (biçim bütünlüğü)."""
    return re.sub(r"[\t\r\n]+", " ", metin or "").strip()


def tsv_oku(yol) -> List[SozlukGirdisi]:
    """``glossary_shared.tsv`` oku (TeX yolu biçimi; yoksa boş liste).

    Satır: ``kaynak<TAB>Türkçe[<TAB>not]``. Boş satır ve ``#`` ile başlayan
    satır atlanır; sekmesiz ya da alanı boş satır atlanır (uyarıyla). UTF-8
    BOM ve CRLF kabul edilir. Aynı kaynak terim ikinci kez gelirse ilki
    geçerlidir.
    """
    p = Path(yol)
    if not p.is_file():
        return []
    try:
        metin = p.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("Kitap sözlüğü okunamadı (%s): %s", p, exc)
        return []
    out: List[SozlukGirdisi] = []
    gorulen: Set[str] = set()
    atlanan = 0
    for satir in metin.splitlines():
        if not satir.strip() or satir.lstrip().startswith("#"):
            continue
        parca = satir.split("\t")
        if len(parca) < 2:
            atlanan += 1
            continue
        kaynak, hedef = parca[0].strip(), parca[1].strip()
        if not kaynak or not hedef:
            atlanan += 1
            continue
        if kaynak.lower() in gorulen:
            continue
        gorulen.add(kaynak.lower())
        out.append(SozlukGirdisi(
            kaynak=kaynak, hedef=hedef,
            not_=parca[2].strip() if len(parca) > 2 else "",
            kaynak_turu=KAYNAK_DOSYA,
        ))
    if atlanan:
        logger.warning("Kitap sözlüğü %s: %d satır biçim dışı (sekme ya da "
                       "alan eksik), atlandı.", p.name, atlanan)
    return out


def tsv_yaz(yol, girdiler: Iterable[SozlukGirdisi]) -> None:
    """``glossary_shared.tsv`` yaz (TeX yolu biçimiyle birebir).

    UTF-8 (BOM'suz), LF, başlık satırı yok; satır ``kaynak<TAB>Türkçe``
    (not varsa üçüncü sütun). Yazım geçici dosya + ``replace`` ile atomiktir
    (yarıda kalan yazım eski dosyayı bozmaz).
    """
    p = Path(yol)
    satirlar = []
    for g in girdiler:
        alanlar = [_tsv_alani(g.kaynak), _tsv_alani(g.hedef)]
        if not alanlar[0] or not alanlar[1]:
            continue
        if _tsv_alani(g.not_):
            alanlar.append(_tsv_alani(g.not_))
        satirlar.append("\t".join(alanlar))
    p.parent.mkdir(parents=True, exist_ok=True)
    gecici = p.with_name(p.name + ".yaziliyor")
    gecici.write_text("".join(s + "\n" for s in satirlar), encoding="utf-8")
    gecici.replace(p)


# ---------------------------------------------------------------------------
# Türkçe harf büyüklüğü
# ---------------------------------------------------------------------------

def _tr_kucuk(metin: str) -> str:
    """Türkçe küçük harf: I -> ı, İ -> i (Python ``lower`` İ'yi bozar)."""
    return (metin or "").replace("I", "ı").replace("İ", "i").lower()


#: Kamu adı (arayüz araması vb.): Türkçe kurallı küçük harf.
turkce_kucuk = _tr_kucuk


def _tr_bas_buyuk(metin: str) -> str:
    """Türkçe ilk harf büyük: i -> İ, ı -> I."""
    if not metin:
        return metin
    ilk = {"i": "İ", "ı": "I"}.get(metin[0], metin[0].upper())
    return ilk + metin[1:]


# ---------------------------------------------------------------------------
# İstem düzeyi uygulama (3a) — HY-MT "terminology intervention"
# ---------------------------------------------------------------------------
# Resmî biçim (doğrulandı 2026-10-05; Hugging Face tencent/HY-MT1.5-7B model
# kartı ve github.com/Tencent-Hunyuan/HY-MT README'si — ikisi aynı):
#
#     参考下面的翻译：
#     {source_term} 翻译成 {target_term}
#
#     将以下文本翻译为{target_language}，注意只需要输出翻译后的结果，不要额外解释：
#     {source_text}
#
# Resmî kaynak tek terim çifti gösterir; birden çok çiftin nasıl verileceği
# BELGELENMEMİŞTİR — burada her çift kendi satırında aynı kalıpla verilir
# (RAPOR'da risk olarak yazıldı). Blok mevcut talimatın ÖNÜNE eklenir
# (resmî sıra: terim bloğu, boş satır, çeviri talimatı).

#: HY-MT resmî terim bloğu başlığı.
HYMT_TERIM_BASLIGI = "参考下面的翻译："
#: HY-MT resmî terim satırı kalıbı.
HYMT_TERIM_KALIBI = "{kaynak} 翻译成 {hedef}"
#: Sohbet biçimli (HY-MT dışı) motorlar için aynı anlamda İngilizce başlık.
GENEL_TERIM_BASLIGI = "Use these term translations:"
#: Sohbet biçimli motorlar için terim satırı kalıbı.
GENEL_TERIM_KALIBI = "{kaynak} = {hedef}"

#: Bir segmentin talimatına eklenecek en çok terim çifti (uzun istem =
#: sızıntı riski; core/translator.py kısa istem ilkesi).
ISTEM_EN_COK_TERIM = 6

#: İstem düzeyi terim desteği (3a) VARSAYILAN KAPALI. Gerekçe: (1) gerçek
#: modelle (HY-MT / sohbet modelleri) etkisi ölçülmedi — model yükleme bu
#: kulvarda yasak (kural 4; "model testleri Berke başlatır"); (2) İngilizce
#: kilit dosyası tests/test_kaynak_dil_20260928.py (TestIstemler) Almanca/
#: Rusça istemin talimat başını BİREBİR denetler, blok onu değiştirir (kural
#: 6). Açmak için ortam değişkeni ``KITAPCEVIR_SOZLUK_ISTEMI=1`` ya da bu
#: sabit; ölçümden sonra varsayılan açılırsa kilit bilinçli güncellenmeli.
ISTEM_TERIMLERI_VARSAYILAN = False

#: İstem desteğini koşu başına açan/kapatan ortam değişkeni ("1" / "0").
ISTEM_ORTAM_DEGISKENI = "KITAPCEVIR_SOZLUK_ISTEMI"


def istem_terimleri_acik() -> bool:
    """İstem düzeyi terim desteği açık mı? (ortam değişkeni > sabit)"""
    deger = os.environ.get(ISTEM_ORTAM_DEGISKENI, "").strip().lower()
    if deger in ("1", "true", "evet", "açık", "acik", "on"):
        return True
    if deger in ("0", "false", "hayır", "hayir", "kapalı", "kapali", "off"):
        return False
    return ISTEM_TERIMLERI_VARSAYILAN

# Modelin terim bloğunu çıktıya yansıtması (sızıntı) — yalnız TAM satır.
_SIZINTI_SATIR_RE = re.compile(
    r"^(?:" + re.escape(HYMT_TERIM_BASLIGI.rstrip("：")) + r"[:：]?"
    r"|" + re.escape(GENEL_TERIM_BASLIGI) +
    r"|[^\n]{1,160}? 翻译成 [^\n]{1,160})$"
)


# Sohbet biçimi terim satırı ("X = Y"); yalnız blok başlığının ARDINDA silinir.
_GENEL_CIFT_RE = re.compile(r"^[^\n=]{1,160} = [^\n]{1,160}$")


def istem_sizintisini_temizle(cikti: str) -> str:
    """Model çıktısına yansımış terim bloğu satırlarını sil.

    Yalnız bloğun kendi satırları (başlık ya da ``X 翻译成 Y``) silinir;
    Türkçe çeviri satırlarına dokunulmaz. Blok izi yoksa metin AYNEN döner.
    """
    if not cikti or ("翻译成" not in cikti and "参考下面" not in cikti
                     and GENEL_TERIM_BASLIGI not in cikti):
        return cikti
    satirlar = cikti.split("\n")
    kalan: List[str] = []
    blokta = False
    for s in satirlar:
        t = s.strip()
        if _SIZINTI_SATIR_RE.match(t):
            blokta = True                    # başlık ya da "X 翻译成 Y"
            continue
        if blokta and _GENEL_CIFT_RE.match(t):
            continue                         # başlığın ardındaki "X = Y"
        blokta = False
        kalan.append(s)
    if len(kalan) == len(satirlar):
        return cikti
    return "\n".join(kalan).strip("\n")


# ---------------------------------------------------------------------------
# Doğrulama geçişi (3b) sabitleri
# ---------------------------------------------------------------------------

#: verify_terms'in dokunacağı en kısa kaynak terim (harf). Çok kısa
#: dizeler Türkçe metindeki rastlantısal eşleşmeye açık.
VERIFY_EN_AZ_HARF = 4

#: Eşleşmenin cümlesini ararken iki yana bakılan en çok karakter.
VERIFY_CUMLE_PENCERESI = 300

# Eşleşmenin hemen ardından Türkçe ek (kesme + harf): "Wärmeleitung'u".
# Kökü değiştirmek ünlü uyumunu bozar -> dokunulmaz (QC raporlar).
_TURKCE_EK_RE = re.compile(r"['’][^\W\d_]")

# Yalnız Türkçede bulunan harfler (ş ğ ı İ; ö/ü/ç Almanca/Fransızcada da
# var) — dil kararı için güçlü işaret (core.lang_detect.TURKISH_ONLY_LETTERS).
_TURKCE_HARF_RE = re.compile(r"[şğıŞĞİ]")


# ---------------------------------------------------------------------------
# Tutarlılık denetimi (3c) sabitleri
# ---------------------------------------------------------------------------

#: QC'ye verilecek en çok tutarlılık bulgusu. Bulgu UYARIDIR (model terim
#: seçimi; otomatik düzeltme yok); her biri QC puanından 1 düşer, puan
#: etkisi bu sayıyla sınırlı kalır. Fazlası son bulgunun iletisinde sayılır.
TUTARLILIK_EN_COK_BULGU = 5

#: Benzer sözcük önerisi için en az benzerlik oranı (difflib).
BENZER_EN_AZ_ORAN = 0.6

#: Paragraf sayıları tutmadığında oranla eşlenen paragrafın iki yanından
#: bakılan komşu paragraf sayısı.
HIZALAMA_PENCERESI = 1


# ---------------------------------------------------------------------------
# KitapSozlugu
# ---------------------------------------------------------------------------

class KitapSozlugu:
    """Bir kitabın terim sözlüğü (kaynak dilde terim -> Türkçe).

    ``glossary_shared.tsv``'den yüklenir ya da :func:`kitap_sozlugu_hazirla`
    ile çıkarılır. Genel alan sözlüğü YÜKLEMEZ (KARAR 25).
    """

    def __init__(self, girdiler: Iterable[SozlukGirdisi] = (),
                 kaynak_dil: str = "en", istem_acik: Optional[bool] = None):
        self.kaynak_dil = (kaynak_dil or "en").lower()
        #: İstem düzeyi terim desteği (3a); None -> istem_terimleri_acik().
        self.istem_acik = (istem_terimleri_acik() if istem_acik is None
                           else bool(istem_acik))
        self._girdiler: List[SozlukGirdisi] = []
        gorulen: Set[str] = set()
        for g in girdiler:
            kaynak, hedef = _tsv_alani(g.kaynak), _tsv_alani(g.hedef)
            if not kaynak or not hedef or kaynak.lower() in gorulen:
                continue
            gorulen.add(kaynak.lower())
            self._girdiler.append(SozlukGirdisi(
                kaynak, hedef, _tsv_alani(g.not_), g.kaynak_turu))
        self._kok_dizini: Optional[Dict[str, List[Tuple[Tuple[str, ...],
                                                        SozlukGirdisi]]]] = None
        self._verify: Optional[Tuple[Optional["re.Pattern[str]"],
                                     Dict[str, SozlukGirdisi]]] = None
        #: Son verify_terms çağrısında değiştirilen / atlanan geçiş sayısı.
        self.son_verify_sayisi = 0
        self.son_verify_atlanan = 0

    # -- temel -------------------------------------------------------------
    def __len__(self) -> int:
        return len(self._girdiler)

    def __iter__(self):
        return iter(list(self._girdiler))

    @property
    def girdiler(self) -> List[SozlukGirdisi]:
        return list(self._girdiler)

    def sozluk(self) -> Dict[str, str]:
        """``{kaynak: hedef}`` (dosya sırasıyla)."""
        return {g.kaynak: g.hedef for g in self._girdiler}

    @classmethod
    def dosyadan_yukle(cls, yol, kaynak_dil: str = "en") -> "KitapSozlugu":
        """``glossary_shared.tsv``'den yükle; dosya yoksa boş sözlük."""
        return cls(tsv_oku(yol), kaynak_dil)

    def tsv_yaz(self, yol) -> None:
        """Sözlüğü ``glossary_shared.tsv`` biçiminde yaz."""
        tsv_yaz(yol, self._girdiler)

    def ara(self, sorgu: str) -> List[SozlukGirdisi]:
        """Kaynak ya da Türkçe tarafta *sorgu* geçen girdiler (arayüz araması)."""
        q = _tr_kucuk((sorgu or "").strip())
        if not q:
            return self.girdiler
        return [g for g in self._girdiler
                if q in _tr_kucuk(g.kaynak) or q in _tr_kucuk(g.hedef)]

    # -- segmentte geçen terimler (istem için; kök toleranslı) ---------------
    def _dizin(self) -> Dict[str, List[Tuple[Tuple[str, ...], SozlukGirdisi]]]:
        if self._kok_dizini is None:
            dizin: Dict[str, List[Tuple[Tuple[str, ...], SozlukGirdisi]]] = \
                defaultdict(list)
            for g in self._girdiler:
                kokler = tuple(_sayim_koku(t, self.kaynak_dil)
                               for t in _belirtecler(g.kaynak))
                if kokler:
                    dizin[kokler[0]].append((kokler, g))
            for liste in dizin.values():
                liste.sort(key=lambda kv: (-len(kv[0]), kv[1].kaynak))
            self._kok_dizini = dict(dizin)
        return self._kok_dizini

    def _gecen_girdiler(self, metin: str) -> List[SozlukGirdisi]:
        """Metinde geçen girdiler (geçiş sırasıyla, tekrarsız; en uzun eşleşme)."""
        if not metin or not self._girdiler:
            return []
        dizin = self._dizin()
        out: List[SozlukGirdisi] = []
        gorulen: Set[int] = set()
        for parca in _OBEK_SINIRI_RE.split(metin):
            toks = _belirtecler(parca)
            if not toks:
                continue
            kokler = [_sayim_koku(t, self.kaynak_dil) for t in toks]
            i = 0
            while i < len(kokler):
                eslesen = None
                for anahtar, g in dizin.get(kokler[i], ()):
                    if tuple(kokler[i:i + len(anahtar)]) == anahtar:
                        eslesen = (anahtar, g)
                        break
                if eslesen is None:
                    i += 1
                    continue
                anahtar, g = eslesen
                if id(g) not in gorulen:
                    gorulen.add(id(g))
                    out.append(g)
                i += len(anahtar)
        return out

    def segment_terimleri(self, metin: str,
                          en_cok: int = ISTEM_EN_COK_TERIM
                          ) -> List[Tuple[str, str]]:
        """Segmentte geçen terimlerin ``(kaynak, hedef)`` çiftleri.

        Eşleşme sayım köküyle yapılır (çekimli biçim de bulunur: Rusça
        "двигателя" -> "двигатель"); en uzun terim önce, örtüşme yok.
        """
        return [(g.kaynak, g.hedef)
                for g in self._gecen_girdiler(metin)[:max(0, int(en_cok))]]

    def istem_bloku(self, metin: str, bicim: str = "hymt",
                    en_cok: int = ISTEM_EN_COK_TERIM) -> str:
        """Segment için terim bloğu (talimatın ÖNÜNE eklenir); terim yoksa ya
        da istem desteği kapalıysa (:data:`ISTEM_TERIMLERI_VARSAYILAN`) "".

        ``bicim="hymt"``: HY-MT resmî "terminology intervention" biçimi.
        Başka biçim (sohbet modelleri): aynı anlamda İngilizce blok.
        HY-MT bloğu boş satırla, sohbet bloğu tek satır sonuyla biter.
        """
        if not self.istem_acik:
            return ""
        ciftler = self.segment_terimleri(metin, en_cok=en_cok)
        if not ciftler:
            return ""
        # HY-MT: resmî sıra (blok, BOŞ satır, talimat); talimat metinden
        # <|extra_4|> ile ayrılır. Sohbet biçimi: talimat ile metin tek user
        # iletisinde "talimat\n\nmetin" diye birleşir — blok tek satır sonuyla
        # biter ki talimat+metin ayrımı (ilk boş satır) bozulmasın.
        if (bicim or "").lower() == "hymt":
            baslik, kalip, son = HYMT_TERIM_BASLIGI, HYMT_TERIM_KALIBI, "\n\n"
        else:
            baslik, kalip, son = GENEL_TERIM_BASLIGI, GENEL_TERIM_KALIBI, "\n"
        satirlar = [kalip.format(kaynak=k, hedef=h) for k, h in ciftler]
        return baslik + "\n" + "\n".join(satirlar) + son

    # -- doğrulama geçişi (3b) --------------------------------------------
    def _verify_deseni(self) -> Tuple[Optional["re.Pattern[str]"],
                                      Dict[str, SozlukGirdisi]]:
        if self._verify is None:
            harita: Dict[str, SozlukGirdisi] = {}
            for g in self._girdiler:
                if g.kaynak.lower() == g.hedef.lower():
                    continue  # çevrilmeyen terim (özel ad): değişecek bir şey yok
                if sum(1 for c in g.kaynak if c.isalpha()) < VERIFY_EN_AZ_HARF:
                    continue
                harita.setdefault(g.kaynak, g)
                bas_buyuk = g.kaynak[:1].upper() + g.kaynak[1:]
                harita.setdefault(bas_buyuk, g)
            if not harita:
                self._verify = (None, {})
            else:
                alternatifler = sorted(harita, key=lambda f: (-len(f), f))
                desen = re.compile(
                    r"(?<![\w'’\-])(?:"
                    + "|".join(re.escape(f) for f in alternatifler)
                    + r")(?![\w\-])"
                )
                self._verify = (desen, harita)
        return self._verify

    def verify_terms(self, metin: str, term_map=None) -> str:
        """Çıktıda ÖZGÜN dilde kalmış terimi karşılığına çevir (3b).

        ``GlossaryEngine.verify_terms(metin, term_map)`` ile aynı imza (Phase
        5.75'te yerine geçer); ``term_map`` kullanılmaz. Kurallar:

        * Yalnız tam eşleşme: sözlükteki yazılış ya da (yalnız CÜMLE BAŞINDA)
          ilk harfi büyük biçimi; sözcük sınırlı (kök/ek eşleşmesi YOK).
          Cümle ortasındaki büyük harfli biçim başlık/özel addır, dokunulmaz.
        * Başlık/altyazı, matematik, kod, atıf ve LaTeX komutları korunur
          (``glossary._protect_math_zones`` — eski geçişle aynı koruma).
        * Terimin ardından Türkçe ek geliyorsa ("Wärmeleitung'u") dokunulmaz
          (kökü değiştirmek ünlü uyumunu bozar; QC raporlar).
        * Terim, karşılığından hemen sonra parantez içinde özgün dilde
          verilmişse ("ısı iletimi (Wärmeleitung)") dokunulmaz.
        * Cümlesi Türkçe olmayan (MT'nin çevirmediği ya da yarı çevirdiği)
          yerde dokunulmaz: Türkçe işaretleri kaynak dilin işlev
          sözcüklerinden KESİN fazla olmalı; tek terimi değiştirmek karışık
          dilli cümle üretir, o cümle QC'nin "çevrilmemiş" denetimine kalır.
        * Tablo/dizin satırında (kaçışsız ``&``) dokunulmaz.
        * Genel alan sözlükleri YÜKLENMEZ; yalnız bu kitabın sözlüğü.
        """
        self.son_verify_sayisi = 0
        self.son_verify_atlanan = 0
        if not metin or not self._girdiler:
            return metin
        desen, harita = self._verify_deseni()
        if desen is None:
            return metin
        from latex_pipeline.glossary import (
            _protect_math_zones, _restore_math_zones,
        )
        korunmus, stash = _protect_math_zones(metin)
        # İki dilde ortak işlev sözcüğü (en "an" ~ tr "an") karara katılmaz.
        tr_tum, kaynak_tum = _durak_kelimeler("tr"), _durak_kelimeler(self.kaynak_dil)
        tr_durak, kaynak_durak = tr_tum - kaynak_tum, kaynak_tum - tr_tum

        def _degistir(m: "re.Match[str]") -> str:
            bulunan = m.group(0)
            g = harita.get(bulunan)
            if g is None:
                return bulunan
            bas, son = m.start(), m.end()
            if bulunan != g.kaynak and not _cumle_basi_mi(korunmus, bas):
                # Küçük harfli terimin büyük harfli biçimi yalnız cümle başında
                # aranır; cümle ortasında büyük harf başlık/özel ad işaretidir
                # ("\textit{Experimental Combustion}", tablo başlığı).
                self.son_verify_atlanan += 1
                return bulunan
            if _TURKCE_EK_RE.match(korunmus, son):
                self.son_verify_atlanan += 1
                return bulunan
            if _parantez_icinde_ozgun(korunmus, bas, g.hedef):
                self.son_verify_atlanan += 1
                return bulunan
            if _ardindan_parantezde_hedef(korunmus, son, g.hedef):
                # "terim (karşılık)" kalıbı: model terimi bırakıp karşılığını
                # parantezde vermiş; değiştirmek "karşılık (karşılık)" yapar
                # (V2 bulgusu Y4).
                self.son_verify_atlanan += 1
                return bulunan
            if _tablo_satiri_mi(korunmus, bas):
                # Tablo/dizin satırı (çıplak '&'): hücre çevirisi kendi boru
                # hattında; dizinlerde iki dil yan yana durabilir.
                self.son_verify_atlanan += 1
                return bulunan
            cevre = _cumle_araligi(korunmus, bas, son)
            cevre = cevre.replace(bulunan, " ")
            if not _turkce_cevre(cevre, tr_durak, kaynak_durak):
                self.son_verify_atlanan += 1
                return bulunan
            self.son_verify_sayisi += 1
            hedef = g.hedef
            if bulunan[:1].isupper() and not g.kaynak[:1].isupper():
                hedef = _tr_bas_buyuk(hedef)
            return hedef

        sonuc = desen.sub(_degistir, korunmus)
        if self.son_verify_sayisi:
            logger.info("Kitap sözlüğü: özgün dilde kalmış %d terim geçişi "
                        "karşılığına çevrildi (%d geçişe dokunulmadı).",
                        self.son_verify_sayisi, self.son_verify_atlanan)
        return _restore_math_zones(sonuc, stash)

    # Geriye uyum / okunurluk: GlossaryEngine ile ortak ad.
    @property
    def term_count(self) -> int:
        return len(self._girdiler)


# Cümle sonu işaretleri (ardından gelen sözcük cümle başıdır).
_CUMLE_SONU_ISARETLERI = ".!?:…"


def _cumle_basi_mi(metin: str, bas: int) -> bool:
    """*bas* konumu cümle başı mı? (metin/paragraf başı ya da öncesinde,
    boşluk ve açılış tırnağı/parantezi atlanınca, cümle sonu işareti)."""
    i = bas - 1
    while i >= 0 and metin[i] in " \t\"“«‘'(":
        i -= 1
    if i < 0:
        return True
    if metin[i] == "\n":
        j = i - 1
        while j >= 0 and metin[j] in " \t":
            j -= 1
        return j < 0 or metin[j] == "\n"  # boş satır: paragraf başı
    return metin[i] in _CUMLE_SONU_ISARETLERI


_CIPLAK_VE_RE = re.compile(r"(?<!\\)&")


def _tablo_satiri_mi(metin: str, bas: int) -> bool:
    """Eşleşmenin satırı tablo satırı mı? (kaçışsız '&' içerir)"""
    sol = metin.rfind("\n", 0, bas) + 1
    sag = metin.find("\n", bas)
    return bool(_CIPLAK_VE_RE.search(metin, sol, sag if sag >= 0 else len(metin)))


def _parantez_icinde_ozgun(metin: str, bas: int, hedef: str) -> bool:
    """Terim "karşılık (terim)" kalıbında mı? (çevirmenin bilinçli notu)."""
    onu = metin[max(0, bas - 3):bas]
    if "(" not in onu:
        return False
    pencere = metin[max(0, bas - len(hedef) - 40):bas]
    return _tr_kucuk(hedef) in _tr_kucuk(pencere)


def _ardindan_parantezde_hedef(metin: str, son: int, hedef: str) -> bool:
    """Terim "terim (karşılık)" kalıbında mı? (Ardından boşluk + "(" +
    karşılık; Türkçe küçük harf karşılaştırması, çekim eki serbest.)"""
    m = re.match(r"\s*\(\s*", metin[son:son + 4])
    if not m:
        return False
    bas = son + m.end()
    parca = metin[bas:bas + len(hedef) + 8]
    return _tr_kucuk(parca).startswith(_tr_kucuk(hedef))


# Cümle sınırı: cümle sonu işareti + boşluk ya da satır sonu.
_CUMLE_SINIRI_RE = re.compile(r"[.!?…](?=\s)|\n")


def _cumle_araligi(metin: str, bas: int, son: int) -> str:
    """Eşleşmenin cümlesi (cümle sonu işareti ya da satır sonu sınırlı;
    iki yana en çok :data:`VERIFY_CUMLE_PENCERESI` karakter)."""
    sol = max(0, bas - VERIFY_CUMLE_PENCERESI)
    sag = min(len(metin), son + VERIFY_CUMLE_PENCERESI)
    onceki = [m.end() for m in _CUMLE_SINIRI_RE.finditer(metin, sol, bas)]
    sonraki = _CUMLE_SINIRI_RE.search(metin, son, sag)
    return metin[(onceki[-1] if onceki else sol):(sonraki.start() if sonraki else sag)]


def _turkce_cevre(cevre: str, tr_durak: frozenset,
                  kaynak_durak: frozenset) -> bool:
    """Cümle Türkçe mi? Türkçe işaretleri (işlev sözcüğü + Türkçeye özgü
    harf) kaynak dilin işlev sözcüklerinden KESİN fazla olmalı: yarı
    çevrilmiş karışık cümlede ("Hot gas flows through the lüle boğazı
    before the exit.") terime dokunulmaz — o cümle QC'nin işidir."""
    sozcukler = [_tr_kucuk(w) for w in re.findall(r"[^\W\d_]+", cevre)]
    tr = sum(1 for w in sozcukler if w in tr_durak)
    if _TURKCE_HARF_RE.search(cevre):
        tr += 1
    kaynak = sum(1 for w in sozcukler if w in kaynak_durak)
    return tr >= 1 and tr > kaynak


# ---------------------------------------------------------------------------
# Terim çevirisi (2)
# ---------------------------------------------------------------------------

#: ``translate_batch_raw`` çağrısı başına aday sayısı.
TERIM_TOPLU_BOYUT = 8

#: Karşılık bir terim olmalı, cümle değil: en çok
#: ``kaynak sözcük sayısı * katsayı + pay`` sözcük.
KARSILIK_SOZCUK_KATSAYISI = 3
KARSILIK_SOZCUK_PAYI = 2

# Çıktıda kalmamalı: kaynak alfabesi (Kiril), yer tutucu, istem kalıntısı.
_KIRIL_RE = re.compile(r"[Ѐ-ӿ]")
_KALINTI_RE = re.compile(r"<<|>>|@@|\[MT_FAIL\]|翻译|参考")


def curated_yolu() -> Path:
    """``glossary/curated.json`` (proje kökü; CWD'den bağımsız)."""
    return Path(__file__).resolve().parent.parent / "glossary" / "curated.json"


def curated_terimleri(yol=None) -> Dict[str, str]:
    """Elle doğrulanmış İngilizce->Türkçe terimler: ``{terim: Türkçe}``.

    ``keep_original`` girdisi Türkçe tarafta özgün yazılışı taşır. Dosya
    yoksa / bozuksa boş sözlük (boru hattı durmaz).
    """
    p = Path(yol) if yol else curated_yolu()
    try:
        veri = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("curated.json okunamadı (%s): %s", p, exc)
        return {}
    out: Dict[str, str] = {}
    terimler = veri.get("terms", {}) if isinstance(veri, dict) else {}
    if isinstance(terimler, dict):
        for en, bilgi in terimler.items():
            if isinstance(bilgi, dict):
                tr = str(bilgi.get("tr", "") or "").strip()
                if bilgi.get("keep_original") and not tr:
                    tr = en
            elif isinstance(bilgi, str):
                tr = bilgi.strip()
            else:
                continue
            if en.strip() and tr:
                out[en.strip()] = tr
    return out


def kitaptaki_curated(latex_govde: str,
                      curated: Dict[str, str]) -> List[SozlukGirdisi]:
    """Kitapta (düzyazıda, sözcük sınırlı) geçen curated terimleri.

    İngilizce kitapta sözlüğün başlangıcıdır (elle doğrulanmış karşılık).
    Sıra: kitapta ilk geçiş sırası (deterministik).
    """
    if not curated or not latex_govde:
        return []
    temiz = govdeyi_temizle(latex_govde)
    anahtarlar = sorted(curated, key=lambda t: (-len(t), t))
    desen = re.compile(
        r"(?<![\w\-])(?:" + "|".join(re.escape(t) for t in anahtarlar)
        + r")(?![\w\-])", re.IGNORECASE)
    kucuk = {t.lower(): t for t in anahtarlar}
    out: List[SozlukGirdisi] = []
    gorulen: Set[str] = set()
    for m in desen.finditer(temiz):
        t = kucuk.get(m.group(0).lower())
        if t is None or t.lower() in gorulen:
            continue
        gorulen.add(t.lower())
        out.append(SozlukGirdisi(t, curated[t], kaynak_turu=KAYNAK_CURATED))
    return out


def _baglamli_metin(aday: TerimAdayi) -> str:
    """Bağlamlı istek: ilk geçtiği cümle + boş satır + terim.

    Model iki paragrafı birlikte çevirir; terimin karşılığı cümlenin
    çevirisiyle aynı anlamda seçilir ("true anomaly" cümlesindeki
    "anomaly" -> "anomali"). Son paragraf terimin karşılığıdır.
    """
    return f"{aday.ilk_cumle}\n\n{aday.terim}"


def _latin_mi(metin: str) -> bool:
    return not _KIRIL_RE.search(metin or "")


def karsilik_ayikla(cikti, aday: TerimAdayi, baglamli: bool) -> Optional[str]:
    """Model çıktısından terimin Türkçe karşılığını çıkar; geçersizse None.

    Geçersiz: boş / ``[MT_FAIL]`` / bağlamlı istekte ikinci paragraf yok /
    çok satırlı / terim değil cümle (sözcük sınırı) / yer tutucu ya da
    istem kalıntısı / Kiril harfi kalmış / yankı (kaynakla aynı; yalnız
    cümle içinde büyük harfle geçen Latin alfabeli özel adda kabul).
    """
    if not isinstance(cikti, str):
        return None
    c = cikti.strip()
    if not c or c.startswith("[MT_FAIL]"):
        return None
    if baglamli:
        paragraflar = [p.strip() for p in re.split(r"\n[ \t]*\n", c)
                       if p.strip()]
        if len(paragraflar) < 2:
            return None
        c = paragraflar[-1]
    if "\n" in c:
        return None
    c = c.strip(" \t\"'“”„«»‹›`").rstrip(".;:,!?").strip()
    if not c:
        return None
    sinir = aday.sozcuk_sayisi * KARSILIK_SOZCUK_KATSAYISI + KARSILIK_SOZCUK_PAYI
    if len(c.split()) > sinir:
        return None
    if _KALINTI_RE.search(c) or _KIRIL_RE.search(c):
        return None
    if c.lower() == aday.terim.lower():
        if not (aday.buyuk_harfli and _latin_mi(aday.terim)):
            return None
        return aday.terim
    return c


def _parcalar(liste: Sequence, boyut: int):
    boyut = max(1, int(boyut))
    for i in range(0, len(liste), boyut):
        yield liste[i:i + boyut]


def terimleri_cevir(adaylar: Sequence[TerimAdayi], cevirmen,
                    kaynak_dil: str = "en", *,
                    curated: Optional[Dict[str, str]] = None,
                    toplu_boyut: int = TERIM_TOPLU_BOYUT,
                    ) -> List[SozlukGirdisi]:
    """Adayları BİR kez, bağlamlı çevir (seçili yerel motorla).

    *cevirmen*: ``translate_batch_raw(metinler, max_length=...)`` sunan
    nesne (``core.translator.BookTranslator``: bütün arka uçlarda —
    MLX, Ollama, LM Studio, OpenAI uyumlu, GGUF — çalışan genel çağrı;
    koruma zinciri ve çeviri belleği YOK, motor kodu değişmez). Önce
    bağlamlı istek (:func:`_baglamli_metin`); karşılığı ayıklanamayan aday
    için tek başına terim istenir; o da geçersizse aday sözlüğe girmez
    (uydurma karşılık yazılmaz).

    *curated* (İngilizce kitap): eşleşen aday modele GİTMEZ, curated
    karşılık kullanılır (modelin önüne geçer).

    ``BackendUnavailable`` (model kullanılamıyor) yukarı çıkar; çağıran
    sözlüğü yazmaz.
    """
    curated_k = {k.lower(): v for k, v in (curated or {}).items()}
    girdiler: List[SozlukGirdisi] = []
    model_adaylari: List[TerimAdayi] = []
    for a in adaylar:
        c = curated_k.get(a.terim.lower())
        if c:
            girdiler.append(SozlukGirdisi(a.terim, c, kaynak_turu=KAYNAK_CURATED))
        else:
            model_adaylari.append(a)
    if not model_adaylari or cevirmen is None:
        return girdiler

    def _cagir(metinler: List[str]) -> List[Optional[str]]:
        try:
            out = cevirmen.translate_batch_raw(metinler)
        except Exception as exc:
            if type(exc).__name__ == "BackendUnavailable":
                raise
            logger.warning("Kitap sözlüğü: terim çevirisi çağrısı başarısız "
                           "(%s); bu parti atlandı.", exc)
            return [None] * len(metinler)
        if not isinstance(out, list) or len(out) != len(metinler):
            return [None] * len(metinler)
        return out

    sonuc: Dict[int, str] = {}
    toplam = len(model_adaylari)
    for parti_no, parti in enumerate(_parcalar(model_adaylari, toplu_boyut)):
        ciktilar = _cagir([_baglamli_metin(a) for a in parti])
        for a, c in zip(parti, ciktilar):
            h = karsilik_ayikla(c, a, baglamli=True)
            if h:
                sonuc[id(a)] = h
        logger.info("Kitap sözlüğü: %d/%d terim çevrildi.",
                    min(toplam, (parti_no + 1) * max(1, toplu_boyut)), toplam)
    eksik = [a for a in model_adaylari if id(a) not in sonuc]
    if eksik:
        for parti in _parcalar(eksik, toplu_boyut):
            ciktilar = _cagir([a.terim for a in parti])
            for a, c in zip(parti, ciktilar):
                h = karsilik_ayikla(c, a, baglamli=False)
                if h:
                    sonuc[id(a)] = h
    dusen = [a.terim for a in model_adaylari if id(a) not in sonuc]
    if dusen:
        logger.info("Kitap sözlüğü: %d adayın karşılığı alınamadı, sözlüğe "
                    "girmedi (%s).", len(dusen), ", ".join(dusen[:8]))
    for a in model_adaylari:
        if id(a) in sonuc:
            girdiler.append(SozlukGirdisi(a.terim, sonuc[id(a)],
                                          kaynak_turu=KAYNAK_MODEL))
    return girdiler


def kitap_sozlugu_hazirla(latex_govde: str, kaynak_dil: str, kitap_klasoru,
                          cevirmen_saglayici: Optional[Callable[[], object]] = None,
                          *, curated: Optional[Dict[str, str]] = None,
                          en_cok: int = ADAY_EN_COK) -> KitapSozlugu:
    """Kitabın sözlüğünü hazırla: klasörde ``glossary_shared.tsv`` varsa
    onu YÜKLE (kullanıcının / TeX yolu çevirmeninin / önceki koşunun
    sözlüğü önceliklidir, üzerine yazılmaz); yoksa çıkar, çevir, yaz.

    *cevirmen_saglayici*: çevirmeni döndüren çağrılabilir (tembel model
    yükleme; aday yoksa model hiç yüklenmez). Model kullanılamazsa ya da
    çeviri yarıda kalırsa dosya YAZILMAZ (sonraki koşu yeniden dener);
    o koşu için eldeki (curated) girdilerle devam edilir.
    """
    dil = (kaynak_dil or "en").lower()
    yol = Path(kitap_klasoru) / SOZLUK_DOSYA_ADI
    if yol.is_file():
        sozluk = KitapSozlugu.dosyadan_yukle(yol, dil)
        logger.info("Kitap sözlüğü dosyadan yüklendi: %s (%d terim).",
                    yol, len(sozluk))
        return sozluk

    adaylar = aday_terimler(latex_govde, dil, en_cok=en_cok)
    girdiler: List[SozlukGirdisi] = []
    cur: Dict[str, str] = {}
    if dil == "en":
        cur = curated if curated is not None else curated_terimleri()
        girdiler.extend(kitaptaki_curated(latex_govde, cur))
        alinan = {g.kaynak.lower() for g in girdiler}
        adaylar = [a for a in adaylar if a.terim.lower() not in alinan]
    logger.info("Kitap sözlüğü: %d aday terim (%s), %d curated terim.",
                len(adaylar), dil, len(girdiler))

    cevirmen = None
    if adaylar and cevirmen_saglayici is not None:
        try:
            cevirmen = cevirmen_saglayici()
        except Exception as exc:
            logger.warning("Kitap sözlüğü: çeviri modeli açılamadı (%s); "
                           "sözlük bu koşuda yazılmadı.", exc)
            return KitapSozlugu(girdiler, dil)
    try:
        girdiler.extend(terimleri_cevir(adaylar, cevirmen, dil, curated=cur))
    except Exception as exc:
        logger.warning("Kitap sözlüğü: terim çevirisi yarıda kaldı (%s); "
                       "sözlük bu koşuda yazılmadı.", exc)
        return KitapSozlugu(girdiler, dil)
    sozluk = KitapSozlugu(girdiler, dil)
    if len(sozluk):
        try:
            sozluk.tsv_yaz(yol)
            logger.info("Kitap sözlüğü yazıldı: %s (%d terim).", yol, len(sozluk))
        except OSError as exc:
            logger.warning("Kitap sözlüğü yazılamadı (%s): %s", yol, exc)
    return sozluk


# ---------------------------------------------------------------------------
# Tutarlılık denetimi (3c) — QC uyarısı, otomatik değiştirme YOK
# ---------------------------------------------------------------------------

@dataclass
class TutarlilikBulgusu:
    """Bir sözlük teriminin çeviride farklı karşılıklarla geçmesi."""

    kaynak: str
    hedef: str
    kaynak_paragraf: int     # kaynakta terimin geçtiği (hizalanan) paragraf
    uyumlu: int              # çeviride sözlük karşılığının geçtiği
    farkli: int              # karşılığın da, özgün terimin de geçmediği
    ilk_satir: int           # çeviride ilk "farklı" paragrafın ilk satırı
    ornek: str = ""          # o paragraftan kısa kesit
    benzer: str = ""         # karşılığa benzeyen sözcük (olası farklı karşılık)


def _paragraf_araliklari(satirlar: Sequence[str]) -> List[Tuple[int, int]]:
    """Boş satırla ayrılmış paragrafların ``(ilk, son+1)`` satır dizinleri."""
    out: List[Tuple[int, int]] = []
    bas = None
    for i, s in enumerate(satirlar):
        if s.strip():
            if bas is None:
                bas = i
        elif bas is not None:
            out.append((bas, i))
            bas = None
    if bas is not None:
        out.append((bas, len(satirlar)))
    return out


def _temiz_paragraflar(govde: str) -> Tuple[List[Tuple[int, int]], List[str],
                                            List[str]]:
    """``(aralıklar, ham satırlar, temiz satırlar)`` — temizlik satır hizalı."""
    ham = (govde or "").split("\n")
    temiz = govdeyi_temizle(govde or "").split("\n")
    if len(temiz) != len(ham):  # pragma: no cover - temizlik hizayı korur
        temiz = [govdeyi_temizle(s) for s in ham]
    return _paragraf_araliklari(ham), ham, temiz


_YUMUSAMA = {"p": "b", "ç": "c", "t": "d", "k": "ğ"}


def _hedef_gecer_mi(tr_tokens: Sequence[str], hedef_tokens: Sequence[str]) -> bool:
    """Türkçe sözcük dizisinde sözlük karşılığı geçiyor mu? (çekim toleranslı)

    Son sözcük ekli olabilir ("anomali" -> "anomalisi", "kitap" ->
    "kitabı": ünsüz yumuşaması); önceki sözcükler birebir.
    """
    n = len(hedef_tokens)
    if not n or len(tr_tokens) < n:
        return False
    son = hedef_tokens[-1]
    kokler = [son]
    if son and son[-1] in _YUMUSAMA:
        kokler.append(son[:-1] + _YUMUSAMA[son[-1]])
        if son[-1] == "k":
            kokler.append(son[:-1] + "g")  # "renk" -> "rengi"
    bas = list(hedef_tokens[:-1])
    for i in range(len(tr_tokens) - n + 1):
        if list(tr_tokens[i:i + n - 1]) != bas:
            continue
        if tr_tokens[i + n - 1].startswith(tuple(kokler)):
            return True
    return False


def _tr_belirtecler(metin: str) -> List[str]:
    return [_tr_kucuk(t) for t in _SOZCUK_RE.findall(metin or "")]


def _benzer_sozcuk(tokens: Sequence[str], hedef: str) -> str:
    """Paragrafta karşılığa en çok benzeyen sözcük (oran eşiği üstünde)."""
    import difflib
    en_iyi, oran_iyi = "", 0.0
    h = _tr_kucuk(hedef)
    for w in dict.fromkeys(tokens):
        if len(w) < 3 or w.startswith(h):
            continue
        oran = difflib.SequenceMatcher(None, h, w[:len(h) + 4]).ratio()
        if oran > oran_iyi:
            en_iyi, oran_iyi = w, oran
    return en_iyi if oran_iyi >= BENZER_EN_AZ_ORAN else ""


def tutarlilik_bulgulari(sozluk: "KitapSozlugu", kaynak_govde: str,
                         ceviri_govde: str) -> List[TutarlilikBulgusu]:
    """Kitap sözlüğüne göre terim tutarlılığı (QC uyarısı; değiştirme YOK).

    Kaynakta terimin geçtiği her paragraf, çevirideki karşılık paragrafla
    eşlenir (paragraf sayıları eşitse sırayla; değilse oranla ve
    ±:data:`HIZALAMA_PENCERESI` komşuyla). Eşlenen paragrafta sözlük
    karşılığı (Türkçe çekim toleranslı) varsa "uyumlu"; ne karşılık ne de
    özgün terim varsa "farklı" sayılır (özgün dilde kalan terim "çevrilmemiş"
    denetimlerinin ve verify_terms'in işidir, burada sayılmaz).

    Bulgu: terim en az iki paragrafta geçiyor ve en az bir "farklı" var —
    ya bazı yerlerde sözlük karşılığı bazı yerlerde başka karşılık (mt okuma
    MT-36: "anomaly" -> "anormallik" ×6, "anomali" ×1) ya da karşılık hiç
    kullanılmamış (en az iki farklı geçiş). Sıra: farklı sayısı azalan.
    """
    if sozluk is None or not len(sozluk) or not kaynak_govde or not ceviri_govde:
        return []
    girdiler = [g for g in sozluk.girdiler
                if _tr_kucuk(g.kaynak) != _tr_kucuk(g.hedef)]
    if not girdiler:
        return []
    k_ara, _k_ham, k_temiz = _temiz_paragraflar(kaynak_govde)
    t_ara, t_ham, t_temiz = _temiz_paragraflar(ceviri_govde)
    if not k_ara or not t_ara:
        return []
    alt = KitapSozlugu(girdiler, sozluk.kaynak_dil)
    hedef_tok = {id(g): _tr_belirtecler(g.hedef) for g in alt.girdiler}
    kaynak_tok = {id(g): _tr_belirtecler(g.kaynak) for g in alt.girdiler}

    def _tr_paragraf(j: int) -> str:
        a, b = t_ara[j]
        return "\n".join(t_temiz[a:b])

    t_tok_onbellek: Dict[int, List[str]] = {}

    def _t_tok(j: int) -> List[str]:
        if j not in t_tok_onbellek:
            t_tok_onbellek[j] = _tr_belirtecler(_tr_paragraf(j))
        return t_tok_onbellek[j]

    esit = len(k_ara) == len(t_ara)
    oran = len(t_ara) / len(k_ara)
    sayac: Dict[int, List[int]] = {}      # id -> [paragraf, uyumlu, farklı]
    ilk_farkli: Dict[int, int] = {}       # id -> çeviri paragraf dizini
    for i, (a, b) in enumerate(k_ara):
        k_metin = "\n".join(k_temiz[a:b])
        gecen = alt._gecen_girdiler(k_metin)
        if not gecen:
            continue
        if esit:
            adaylar_j = [i]
            merkez = i
        else:
            merkez = min(len(t_ara) - 1, int(round(i * oran)))
            adaylar_j = [j for j in range(merkez - HIZALAMA_PENCERESI,
                                          merkez + HIZALAMA_PENCERESI + 1)
                         if 0 <= j < len(t_ara)]
        toks = [w for j in adaylar_j for w in _t_tok(j)]
        for g in gecen:
            s = sayac.setdefault(id(g), [0, 0, 0])
            s[0] += 1
            if _hedef_gecer_mi(toks, hedef_tok[id(g)]):
                s[1] += 1
            elif _hedef_gecer_mi(toks, kaynak_tok[id(g)]):
                continue  # özgün dilde kalmış: başka denetimin işi
            else:
                s[2] += 1
                ilk_farkli.setdefault(id(g), merkez)

    bulgular: List[TutarlilikBulgusu] = []
    for g in alt.girdiler:
        s = sayac.get(id(g))
        if not s:
            continue
        paragraf, uyumlu, farkli = s
        if paragraf < 2 or farkli < 1 or (uyumlu == 0 and farkli < 2):
            continue
        j = ilk_farkli[id(g)]
        a, b = t_ara[j]
        ornek = " ".join(" ".join(t_ham[a:b]).split())[:120]
        benzer = ""
        if len(hedef_tok[id(g)]) == 1:
            benzer = _benzer_sozcuk(_t_tok(j), g.hedef)
        bulgular.append(TutarlilikBulgusu(
            kaynak=g.kaynak, hedef=g.hedef, kaynak_paragraf=paragraf,
            uyumlu=uyumlu, farkli=farkli, ilk_satir=a + 1, ornek=ornek,
            benzer=benzer,
        ))
    bulgular.sort(key=lambda b: (-b.farkli, b.kaynak.lower()))
    return bulgular


def bulgu_iletisi(b: TutarlilikBulgusu) -> str:
    """QC raporu için Türkçe ileti."""
    ileti = (f"Terim tutarsızlığı (kitap sözlüğü): '{b.kaynak}' sözlükte "
             f"'{b.hedef}'; {b.farkli} paragrafta farklı karşılık")
    if b.uyumlu:
        ileti += f", {b.uyumlu} paragrafta sözlükteki karşılık"
    if b.benzer:
        ileti += f" (ör. '{b.benzer}')"
    return ileti


# ---------------------------------------------------------------------------
# Genel sözlükle çelişki: kitap sözlüğü önceliklidir (KARAR 25)
# ---------------------------------------------------------------------------
# glossary/curated.json'un QC kuralları (yanlış karşılık listesi, beklenen
# karşılık) kaynaktan bağımsız tarar: Türkçe metinde "jeton" geçtiği her
# yerde "token -> belirteç" kuralı ihlal sayılır. Kitabın sözlüğü o terim
# için başka bir karşılık seçmişse (ör. "token -> jeton (token)") genel
# kural o kitapta uygulanmaz. Yalnız ÇELİŞKİ kalkar: kitabın
# seçmediği yanlış karşılık bulunmaya devam eder.

_PARANTEZ_RE = re.compile(r"\(([^()]*)\)")


def _alan_bicimleri(alan: str) -> List[str]:
    """Sözlük alanının ayrı biçimleri: parantezsiz hâl, parantez içi ve "/"
    ile ayrılan seçenekler ("jeton (token)" -> "jeton", "token";
    "sahteleme / sahtelemek" -> "sahteleme", "sahtelemek")."""
    out: List[str] = []
    for parca in [_PARANTEZ_RE.sub(" ", alan or ""),
                  *_PARANTEZ_RE.findall(alan or "")]:
        for secenek in parca.split("/"):
            secenek = " ".join(secenek.split())
            if secenek and secenek not in out:
                out.append(secenek)
    return out


class KarsilikSecimleri:
    """Kitap sözlüğünün seçtiği Türkçe karşılıklar (QC: genel sözlük
    kuralıyla çelişki denetimi; sözlüğe dokunmaz).

    Eşleşme Türkçe küçük harfle ve çekim toleransıyla yapılır
    (:func:`_hedef_gecer_mi`: son sözcük ek alabilir, ünsüz yumuşaması).
    """

    def __init__(self, sozluk: Iterable[SozlukGirdisi],
                 kaynak_dil: Optional[str] = None):
        self.kaynak_dil = (kaynak_dil or getattr(sozluk, "kaynak_dil", "en")
                           or "en").lower()
        #: Bütün karşılık biçimleri (belirteç dizileri, tekrarsız).
        self._hedefler: List[Tuple[str, ...]] = []
        #: Kaynak terimin sayım kökü -> o girdinin karşılık biçimleri.
        self._terimler: Dict[Tuple[str, ...], List[Tuple[str, ...]]] = {}
        for g in sozluk:
            bicimler = [b for b in (tuple(_tr_belirtecler(h))
                                    for h in _alan_bicimleri(g.hedef)) if b]
            for b in bicimler:
                if b not in self._hedefler:
                    self._hedefler.append(b)
            for k in _alan_bicimleri(g.kaynak):
                kok = self._kok(k)
                if kok:
                    self._terimler.setdefault(kok, []).extend(bicimler)

    def __bool__(self) -> bool:
        return bool(self._hedefler)

    def _kok(self, terim: str) -> Tuple[str, ...]:
        return tuple(_sayim_koku(t, self.kaynak_dil) for t in _belirtecler(terim))

    @staticmethod
    def _eslesir(ifade: Sequence[str], bicim: Sequence[str],
                 ekli_icerik: bool = False) -> bool:
        # (a) ifade biçimin içinde TAM sözcüklerle geçer ("jeton" <- "jeton
        #     (token)"); ekli_icerik=True ise son sözcüğü ekli de olabilir
        #     ("belirteç" <- "erişim belirteci").
        # (b) ifade biçimin çekimli hâlidir: aynı sözcük sayısı, son sözcük
        #     ek alır ("jetonlar" <- "jeton", "nozulun" <- "nozul").
        # Daha uzun ifade, içindeki tek sözcük seçildi diye eşleşmez ("ses"
        # seçimi "alt ses"i kapsamaz).
        n = len(ifade)
        if ekli_icerik:
            if _hedef_gecer_mi(bicim, ifade):
                return True
        elif any(tuple(bicim[i:i + n]) == tuple(ifade)
                 for i in range(len(bicim) - n + 1)):
            return True
        return n == len(bicim) and _hedef_gecer_mi(ifade, bicim)

    def secili_mi(self, ifade: str) -> bool:
        """Kitap *ifade*yi (Türkçe) bir terimin karşılığı olarak seçmiş mi?

        Seçim: karşılık biçiminde tam sözcüklerle geçmesi ya da biçimin
        çekimli hâli olması. "access token -> erişim jetonu" seçimi tek
        başına "jeton"u kapsamaz (metindeki "erişim jetonu" zaten "jeton"
        kuralına takılmaz; tek başına "jeton" kitabın seçimi değildir).
        """
        toks = _tr_belirtecler(ifade)
        return bool(toks) and any(self._eslesir(toks, b) for b in self._hedefler)

    def desen_secili_mi(self, desen: "re.Pattern[str]") -> bool:
        """Kitabın seçtiği bir karşılık *desen*e uyuyor mu? (koddaki sabit
        "bilinen yanlış çeviri" desenleri için)"""
        return any(desen.search(" ".join(b)) for b in self._hedefler)

    def celisir_mi(self, terim: str, beklenen: str) -> bool:
        """Kitap kaynak dildeki *terim*i tanımlıyor ve seçtiği karşılık genel
        sözlüğün *beklenen* karşılığı DEĞİL mi? (çelişki yoksa False)"""
        secilenler = self._terimler.get(self._kok(terim))
        bek = _tr_belirtecler(beklenen)
        if not secilenler or not bek:
            return False
        return not any(self._eslesir(bek, b, ekli_icerik=True)
                       for b in secilenler)
