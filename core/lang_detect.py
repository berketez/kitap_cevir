"""Bağımlılıksız kaynak dili tespiti (alfabe oranları + sık kelime profilleri).

İki tüketicisi var:

1. ``translate_book.py`` — "Yerel model ile çevir" (Yol B) kapısı. Yerel
   modelin kaynak dilleri ``TEX_SOURCE_LANGS``'tir (KARARLAR 14, 28 Eylül
   2026); kitabın dili bunlardan biriyse zincire o dil akar, desteklenmeyen
   bir dilde olduğu güvenle ölçülürse MT başlamadan iş durdurulur (bkz.
   :func:`detect_language`, :func:`should_stop_for_source_language`).
   Çıkarımdan önce de PDF metin katmanı / EPUB metni üzerinde daha sıkı bir
   ön kapı koşar (bkz. :func:`should_stop_before_extraction`).
2. ``latex_pipeline/qc_engine.py`` — çevrilmemiş kaynak metin kontrolü. Türkçe
   çıktıda Latin dışı alfabeyle (Kiril, Yunan, Arap, İbrani, CJK) ya da Latin
   alfabeli bir Avrupa diliyle (de/fr/es/it/pt) yazılmış, çevrilmeden kalmış
   satırları yakalar (bkz. :func:`classify_script_line`,
   :func:`classify_latin_line`).

Alfabe tanımları ve BÜTÜN sayısal eşikler bu modülde tanımlıdır (tek kaynak);
QC ile Yol B kapısı aynı tanımları paylaşır. Harici kütüphane (langdetect,
fasttext vb.) kullanılmaz: yöntem, harf başına alfabe sayımı ile her dilin en
sık işlev kelimelerinden kurulmuş profillerin (İngilizce/Türkçe ile ortak
kelimeleri ayıklanmış) eşleşme oranıdır. Matematik, kod ve LaTeX komutları
ölçümden önce maskelenir; bol formüllü İngilizce kitap bu yüzden "belirsiz"e
düşmez.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Alfabe tanımları (QC ile ORTAK — tek kaynak)
# ---------------------------------------------------------------------------

# Latin dışı alfabeler: ad -> Unicode aralıkları. Aralıklar harf dışı işaret
# de içerebilir; sayımda yalnız ``str.isalpha()`` olan karakterler kullanılır.
SCRIPT_RANGES: Dict[str, Tuple[Tuple[int, int], ...]] = {
    "cyrillic": ((0x0400, 0x04FF), (0x0500, 0x052F)),
    "greek": ((0x0370, 0x03FF), (0x1F00, 0x1FFF)),
    "arabic": ((0x0600, 0x06FF), (0x0750, 0x077F), (0x08A0, 0x08FF),
               (0xFB50, 0xFDFF), (0xFE70, 0xFEFF)),
    "hebrew": ((0x0590, 0x05FF), (0xFB1D, 0xFB4F)),
    # Çince ideogramlar + Japon kana + Kore hangıl (CJK noktalama hariç —
    # harf sayılmaz).
    "cjk": ((0x3040, 0x309F), (0x30A0, 0x30FF), (0x3400, 0x4DBF),
            (0x4E00, 0x9FFF), (0xAC00, 0xD7AF), (0xF900, 0xFAFF)),
}

# Latin harfler: ASCII + Latin-1 ek + Latin Genişletilmiş-A/B + Latin
# Genişletilmiş Ek (Vietnamca vb.). Türkçe harflerin hepsi burada.
_LATIN_CLASS = (
    "A-Za-z\u00C0-\u00D6\u00D8-\u00F6\u00F8-\u024F\u1E00-\u1EFF"
)


def _char_class(ranges: Iterable[Tuple[int, int]]) -> str:
    return "".join(f"\\u{lo:04X}-\\u{hi:04X}" for lo, hi in ranges)


SCRIPT_CHAR_RES: Dict[str, "re.Pattern[str]"] = {
    name: re.compile(f"[{_char_class(rngs)}]")
    for name, rngs in SCRIPT_RANGES.items()
}
# Bir satırda "iki+ harflik kelime" (CJK hariç; orada kelime arası boşluk yok).
SCRIPT_WORD_RES: Dict[str, "re.Pattern[str]"] = {
    name: re.compile(f"[{_char_class(rngs)}]{{2,}}")
    for name, rngs in SCRIPT_RANGES.items()
}
# Herhangi bir Latin dışı alfabe karakteri — hızlı ön eleme (C hızında search).
NON_LATIN_CHAR_RE = re.compile(
    "[" + "".join(_char_class(r) for r in SCRIPT_RANGES.values()) + "]"
)
LATIN_LETTER_RE = re.compile(f"[{_LATIN_CLASS}]")
# Kiril kelime (tek harfli dahil) — Rusça profil kapsaması için.
_CYRILLIC_TOKEN_RE = re.compile(f"[{_char_class(SCRIPT_RANGES['cyrillic'])}]+")

# Yalnız Türkçede geçen harfler. ö/ü/ç Almanca/Fransızca ile ORTAKTIR —
# "Türkçe harf var, demek ki Türkçe" çıkarımı o yüzden yalnız bu altkümeyle
# yapılabilir (â/î/û Fransızcada da var).
TURKISH_ONLY_LETTERS = "şğıŞĞİ"
TURKISH_ONLY_RE = re.compile(f"[{TURKISH_ONLY_LETTERS}]")

# Harf dizisi (rakam/altçizgi hariç Unicode harfleri) — kelime profilleri için.
_WORD_RE = re.compile(r"[^\W\d_]+")

# ---------------------------------------------------------------------------
# Eşikler (TEK YER — QC ve Yol B kapısı buradan okur)
# ---------------------------------------------------------------------------

# -- Kitap düzeyi (Yol B kapısı) --
# Karar için gereken asgari harf/kelime; altında sonuç "und" (ölçülemedi).
# ~1000 harf ≈ 170 kelime: işlev kelimesi oranı bu örneklemde kararlıdır;
# gerçek bir kitap bunun yüzlerce katıdır (eşik yalnız neredeyse boş/yalnız
# formülden oluşan gövdeyi "belirsiz"e düşürür).
DETECT_MIN_LETTERS = 1000
DETECT_MIN_WORDS = 150
# Ölçüm örneği üst sınırı (karakter). Uzun kitapta eşit aralıklı pencereler
# alınır; 400k temizlenmiş karakter ~60k kelime eder, karar için bol bol yeter.
DETECT_SAMPLE_CHARS = 400_000
DETECT_SAMPLE_WINDOWS = 20
# Latin dışı bir alfabe harflerin en az bu payına sahipse kitabın ana alfabesi
# odur (güven = o pay).
SCRIPT_DOMINANCE_SHARE = 0.50
# Kelime profili: kazanan grubun (İngilizce / Türkçe / Avrupa ailesi)
# eşleşen kelime payı bu orana ulaşınca "kapsama" tam sayılır. Ölçüm
# (output/ altındaki 13 İngilizce gövde): İngilizce işlev kelimesi payı
# %34-50; eşik bilerek çok daha aşağıda.
PROFILE_FULL_COVERAGE = 0.12
# Kiril metin Rusça sayılmak için Rusça profilde en az bu kapsama gerekli;
# yoksa kod "cyrillic" (Kiril alfabeli, dili belirsiz: Ukraynaca, Bulgarca...).
RUSSIAN_MIN_COVERAGE = 0.05
# Yol B kapısı: tespit edilen dil desteklenmiyor (TEX_SOURCE_LANGS dışında)
# VE güven >= bu eşik ise iş DURUR; desteklenen İngilizce dışı bir dil ancak
# bu eşikte kaynak dil olarak kabul edilir (altında İngilizce varsayılır).
# Eşiğin altındaki her İngilizce-dışı tespit yalnız UYARI loglar. Yanlış
# pozitifin maliyeti yüksek (kitap haksız yere reddedilir ya da yanlış dille
# çevrilir); ölçülen gerçek kitaplarda doğru tespitin güveni 0,97-1,00;
# İngilizce ile başka dilin karıştığı metinlerde 0,5-0,76 — eşik ikisinin
# arasında.
GATE_STOP_CONFIDENCE = 0.80

# -- Çıkarım öncesi ön kapı (Yol B; PDF metin katmanı / EPUB metni) --
# Marker OCR'dan ÖNCE, kitabın gömülü metninden ucuz bir örnek alınır:
# belgeye eşit aralıkla yayılmış en çok PREGATE_MAX_PAGES sayfa, sayfa başına
# en çok PREGATE_MAX_CHARS / PREGATE_MAX_PAGES karakter (tek bir yoğun sayfa,
# ör. dizin, örneği ele geçiremez). EPUB'da aynı toplam sınır geçerlidir.
PREGATE_MAX_PAGES = 20
PREGATE_MAX_CHARS = 60_000
# Ön kapı tam kapıdan daha SIKIDIR: metin katmanı taramalarda bozuk olabilir
# ve arayüz --dil-kontrolu-yok bayrağını geçemez; yanlış pozitif İngilizce bir
# kitabı çıkış yolu olmadan reddeder. Bu yüzden (1) en az bu kadar harf
# (~2,5 sayfa düzyazı; DETECT_MIN_LETTERS'ın 5 katı) ister, (2) kazanan dilin
# işlev kelimesi kapsaması en az PROFILE_FULL_COVERAGE olmalıdır — Kiril/Latin
# dışı dalda güven yalnız alfabe payıdır, kelime kanıtı taşımaz; yanlış OCR
# diliyle üretilmiş "Kiril görünümlü" çöp katman bu şarta takılır. Kelime
# profili olmayan alfabeler (CJK, Yunan, Arap, İbrani) ön kapıda hiç
# durdurulmaz; onları çıkarım sonrasındaki tam kapı yakalar.
PREGATE_MIN_LETTERS = 5 * DETECT_MIN_LETTERS

# -- Satır düzeyi (QC, Latin dışı alfabe) --
# Satır "çevrilmemiş" sayılır: en az 4 kelime (2+ harf) VE harflerin en az
# yarısı o alfabede. Tek Yunan harfi (α), tek kısaltma (ГОСТ, ПС) ya da
# Türkçe cümle içindeki kısa bir alıntı bu eşiğe takılmaz.
LINE_SCRIPT_MIN_WORDS = 4
LINE_SCRIPT_MIN_SHARE = 0.50
# CJK'da kelime arası boşluk yok: kelime yerine karakter sayılır. HY-MT'nin
# "如图" gibi 2-3 karakterlik artefaktları foreign_language_leakage'ın işi.
LINE_CJK_MIN_CHARS = 10
# Karma satır (Türkçe + çevrilmemiş kaynak cümle): daha çok kelime ister,
# pay düşük olabilir. Kısa kitap adı alıntıları (≤7 kelime) muaf kalır.
LINE_SCRIPT_MIXED_MIN_WORDS = 8
LINE_CJK_MIXED_MIN_CHARS = 20

# -- Künye muafiyeti (QC) --
# "Yıl + yayın işareti" künye kuralı yalnız bu uzunluğa kadar olan satırlara
# uygulanır: uzun bir düzyazı paragrafında "1968" ile "С. 5" tesadüfen
# birlikte geçebilir, paragraf künye diye atlanmasın.
CITATION_MAX_CHARS = 400

# -- Satır düzeyi (QC, Latin alfabeli Avrupa dilleri) --
# Avrupa ailesi eşleşmesi: en az 3 işlev kelimesi, kelimelerin en az %20'si,
# ve İngilizce+Türkçe eşleşmelerin en az 2 katı (baskınlık).
LINE_LATIN_MIN_WORDS = 4
LINE_LATIN_MIN_HITS = 3
LINE_LATIN_MIN_COVERAGE = 0.20
LINE_LATIN_DOMINANCE = 2.0
# Başlıklar kısadır: 2 eşleşme ve %25 kapsama yeterli.
HEADING_LATIN_MIN_WORDS = 2
HEADING_LATIN_MIN_HITS = 2
HEADING_LATIN_MIN_COVERAGE = 0.25
# Latin dışı alfabeli BAŞLIK (yerel model yolu, Rusça kaynak; 2026-09-28):
# en az bu kadar 2+ harflik kelime ve harflerin LINE_SCRIPT_MIN_SHARE payı
# yeter (Latin başlık eşiğiyle aynı sayı).
HEADING_SCRIPT_MIN_WORDS = HEADING_LATIN_MIN_WORDS

# ---------------------------------------------------------------------------
# Dil adları
# ---------------------------------------------------------------------------

LANGUAGE_NAMES_TR: Dict[str, str] = {
    "en": "İngilizce", "de": "Almanca", "fr": "Fransızca",
    "es": "İspanyolca", "it": "İtalyanca", "pt": "Portekizce",
    "ru": "Rusça", "tr": "Türkçe",
    "cyrillic": "Kiril alfabeli bir dil",
    "el": "Yunanca",
    "arabic": "Arap alfabeli bir dil",
    "hebrew": "İbranice",
    "cjk": "Çince/Japonca/Korece",
    "und": "belirsiz",
}

# QC mesajları İngilizce yazılıyor (mevcut kontrollerle tutarlı).
LANGUAGE_NAMES_EN: Dict[str, str] = {
    "en": "English", "de": "German", "fr": "French", "es": "Spanish",
    "it": "Italian", "pt": "Portuguese", "ru": "Russian", "tr": "Turkish",
    "cyrillic": "Cyrillic-script", "el": "Greek",
    "arabic": "Arabic-script", "hebrew": "Hebrew", "cjk": "CJK",
    "und": "unknown-language",
}

# Latin dışı alfabe -> dil kodu (Kiril hariç: orada Rusça profil ayrıca bakılır).
_SCRIPT_TO_CODE: Dict[str, str] = {
    "cyrillic": "cyrillic", "greek": "el", "arabic": "arabic",
    "hebrew": "hebrew", "cjk": "cjk",
}


# Uygulamanın desteklediği kaynak diller: "PDF'den TeX çıkar" (Yol A) ve
# 28 Eylül 2026'dan beri (KARARLAR 14) "Yerel model ile çevir" (Yol B, TÜM
# yerel modellerde; hedef dil yine yalnız Türkçe). Kullanıcıya gösterilen
# düzyazı dil listelerinin (ui/branding.py, 1_cikar.py) TEK kaynağı; README,
# CEVIRI_TALIMATI.md ve docs/ altındaki listeler testle bu sabite bağlıdır
# (tests/test_denetim_f_dil_20260924.py). Çince/Japonca/Korece (CJK) kapsam
# dışıdır — tasarım kararı. Yerel model kapısı (translate_book) bu listeyi
# kullanır: listede olmayan dil güvenle ölçülürse çıkış 4.
TEX_SOURCE_LANGS: Tuple[str, ...] = ("en", "de", "ru", "fr", "es", "it")


def language_name_tr(code: str) -> str:
    """Dil kodunun kullanıcıya gösterilecek Türkçe adı."""
    return LANGUAGE_NAMES_TR.get(code, code)


def language_list_tr(codes: Iterable[str], last_sep: str = ", ") -> str:
    """Dil kodlarını Türkçe düzyazı listeye çevir.

    ``language_list_tr(("en", "de", "ru"), " ya da ")`` →
    ``"İngilizce, Almanca ya da Rusça"``. *last_sep* son iki ad arasına girer
    (varsayılan virgül).
    """
    names = [language_name_tr(code) for code in codes]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + last_sep + names[-1]


def language_name_en(code: str) -> str:
    """Dil kodunun (QC mesajları için) İngilizce adı."""
    return LANGUAGE_NAMES_EN.get(code, code)


# ---------------------------------------------------------------------------
# Kelime profilleri
# ---------------------------------------------------------------------------
# Her dil için yüksek frekanslı işlev kelimeleri. Profiller modül yüklenirken
# iki kurala göre ayrıştırılır (bkz. _build_profiles):
#   * İngilizce ve Türkçe profiller: başka HERHANGİ bir profilde de geçen
#     kelime atılır (yalnız o dile işaret eden kelimeler kalır).
#   * Avrupa profilleri (de/fr/es/it/pt): İngilizce ya da Türkçe listede
#     geçen kelime atılır; Avrupa dilleri ARASINDA ortak kelimeler (que, il,
#     se, la...) kalır — onlar "çevrilmemiş Avrupa dili" ailesinin kanıtıdır,
#     hangi dil olduğu ayırt edici kelimelerden çıkar.
# Türkçe liste bu yüzden aynı zamanda bir dışlama listesidir: Türkçe metinde
# de sık geçen kısa kelimeler (de, da, en, ne, son, su, para, hasta, ya, mi)
# Avrupa kanıtı SAYILMAZ. İngilizce listedeki "in", "per", "come", "man",
# "also", "do", "no", "plus", "on" da aynı nedenle oradadır.

_RAW_PROFILES: Dict[str, Tuple[str, ...]] = {
    "en": (
        "the", "of", "and", "to", "is", "that", "for", "it", "with", "as",
        "on", "are", "by", "this", "be", "which", "from", "or", "at", "not",
        "we", "these", "their", "has", "have", "its", "where", "when",
        "also", "but", "been", "into", "than", "such", "may", "between",
        "each", "other", "if", "there", "they", "would", "should", "will",
        "were", "what", "who", "all", "one", "more", "only", "then", "must",
        "through", "both", "any", "how", "most", "those", "some", "our",
        "them", "was", "an", "can", "do", "no", "so", "his", "her", "he",
        "she", "you", "my", "about", "over", "under", "after", "before",
        "because", "while", "however", "therefore", "thus", "given",
        "since", "could", "does", "shown", "used", "same", "very", "per",
        "come", "man", "plus", "in", "a",
        # "et al." İngilizce/Türkçe akademik metinde de kalıptır; Fransızca
        # "et" ve İspanyolca/İtalyanca "al" bu yüzden Avrupa kanıtı sayılmaz.
        "et", "al",
    ),
    "de": (
        "der", "die", "und", "den", "von", "zu", "das", "mit", "sich",
        "des", "auf", "für", "ist", "im", "dem", "nicht", "ein", "eine",
        "als", "auch", "es", "werden", "aus", "er", "hat", "dass", "daß",
        "sie", "nach", "wird", "bei", "einer", "um", "zum", "zur", "sind",
        "noch", "wie", "einem", "einen", "über", "haben", "nur", "oder",
        "aber", "vor", "bis", "mehr", "durch", "sein", "wurde", "sei",
        "kann", "können", "wenn", "diese", "dieser", "dieses", "zwischen",
        "unter", "wir", "ihre", "ihr", "beim", "sowie", "daher", "jedoch",
        "welche", "welcher", "wobei", "damit", "gibt", "muss", "muß",
        "soll", "dabei", "hier", "ob", "dann", "weil", "sondern", "schon",
        "ebenfalls", "werden", "wurden", "worden", "man", "also", "in",
    ),
    "fr": (
        "le", "la", "les", "des", "du", "une", "un", "et", "est", "qui",
        "dans", "pour", "pas", "sur", "par", "au", "aux", "ce", "cette",
        "ces", "il", "elle", "ils", "elles", "sont", "avec", "ou", "mais",
        "comme", "leur", "leurs", "nous", "vous", "être", "été", "peut",
        "fait", "sous", "sans", "aussi", "donc", "lorsque", "ainsi",
        "dont", "où", "très", "même", "tout", "tous", "toute", "chaque",
        "selon", "cet", "était", "ont", "sa", "ses", "son", "ne", "que",
        "de", "en", "se", "on", "plus", "entre", "qu", "à", "lors",
        "puis", "chez", "après", "avant", "encore", "bien",
    ),
    "es": (
        "el", "la", "los", "las", "de", "del", "que", "es", "en", "por",
        "con", "para", "se", "lo", "como", "más", "pero", "sus", "su",
        "al", "este", "esta", "estos", "estas", "cuando", "muy", "sobre",
        "también", "hasta", "desde", "donde", "puede", "ser", "fue", "son",
        "hay", "cada", "otro", "otra", "porque", "mismo", "tiene",
        "pueden", "entre", "una", "un", "entonces", "según", "sino",
        "aunque", "así", "ella", "ellos", "nos", "les", "durante", "tanto",
        "bien", "sin", "mi", "ya", "no", "le",
    ),
    "it": (
        "il", "lo", "la", "gli", "le", "di", "del", "della", "dei",
        "delle", "degli", "che", "in", "un", "una", "uno", "per", "con",
        "non", "si", "da", "al", "alla", "nel", "nella", "nei", "sono",
        "come", "anche", "più", "ma", "questo", "questa", "questi",
        "quello", "quella", "essere", "stato", "può", "tra", "fra", "sul",
        "sulla", "dal", "dalla", "ogni", "molto", "perché", "quando",
        "ancora", "senza", "cui", "sia", "loro", "hanno", "viene", "dove",
        "ed", "è", "ha", "se", "sempre", "stesso", "essa", "esso",
        "all", "dello", "allo", "agli", "alle", "nelle", "negli", "sui",
    ),
    "pt": (
        "os", "dos", "das", "em", "no", "nos", "nas", "um", "uma", "que",
        "para", "com", "não", "por", "mais", "ao", "aos", "é", "são",
        "foi", "ser", "pelo", "pela", "sua", "seu", "suas", "seus",
        "também", "quando", "muito", "ainda", "onde", "pode", "isso",
        "isto", "este", "esta", "essa", "esse", "há", "já", "sobre",
        "depois", "antes", "sempre", "porque", "mas", "entre", "cada",
        "desde", "até", "apenas", "então", "da", "do", "na", "de", "se",
        "ou", "como", "à", "às", "pelos", "pelas", "num", "numa", "lhe",
    ),
    "ru": (
        "и", "в", "не", "на", "что", "с", "по", "это", "как", "а", "к",
        "из", "для", "от", "при", "о", "до", "или", "так", "же", "то",
        "все", "его", "их", "быть", "был", "была", "было", "были",
        "может", "также", "если", "этого", "этот", "эта", "эти", "который",
        "которые", "которая", "которого", "между", "только", "более",
        "чем", "где", "когда", "после", "через", "под", "над", "без",
        "есть", "будет", "всех", "этой", "том", "того", "этом", "можно",
        "следовательно", "поэтому", "однако", "т", "е",
    ),
    "tr": (
        "ve", "bir", "bu", "ile", "için", "icin", "olarak", "da", "de",
        "olan", "daha", "gibi", "çok", "cok", "en", "ancak", "veya", "ise",
        "kadar", "sonra", "her", "göre", "gore", "değil", "degil",
        "olduğu", "oldugu", "arasında", "arasinda", "üzerinde", "uzerinde",
        "şekilde", "nedeniyle", "tüm", "tum", "bazı", "bazi", "aynı",
        "ayni", "ya", "hem", "ki", "mi", "ne", "son", "su", "para",
        "hasta", "şu", "ama", "çünkü", "cunku", "yani", "bile", "diye",
        "eğer", "eger", "olur", "oldu", "olup", "olmak", "üzere", "uzere",
        "önce", "once", "sadece", "yalnız", "birlikte", "böylece", "o",
        "an", "sen", "ben", "biz", "siz", "var", "yok", "dir", "dır",
        "tir", "tır", "iki", "üç", "dört",
    ),
}


def frequent_words(lang: str) -> FrozenSet[str]:
    """Dilin ham sık kelime listesi (profil ayıklaması ÖNCESİ; ``"en"``,
    ``"de"``… — bilinmeyen dilde boş küme). Kısa bir sözcüğün o dilde
    gerçek (işlev) sözcük olup olmadığını sormak için (ör. italik ``is``,
    ``no``, ``не``; translator F3-03)."""
    return _FREQUENT_WORDS.get(lang, frozenset())


def is_signal_word(word: str) -> bool:
    """Profil eşleşmesine girebilecek kelime mi?

    Tek harfli ASCII kelimeler (a, e, i, x...) matematik değişkeniyle
    karışır, sayılmaz; tek harfli ASCII DIŞI kelimeler (è, é, и, в) ise
    güçlü dil işaretidir, sayılır.
    """
    return len(word) >= 2 or not word.isascii()


# Latin alfabeli profil dilleri (Rusça Kiril tarafında ayrıca kullanılır).
LATIN_PROFILE_LANGS: Tuple[str, ...] = ("en", "de", "fr", "es", "it", "pt", "tr")
# TESPİT edilen Latin alfabeli Avrupa dilleri ("aile"). Desteklenen kaynak
# dil listesi DEĞİLDİR (o TEX_SOURCE_LANGS): Portekizce yalnız tanınmak için
# burada — QC çevrilmemiş Portekizce satırı, kapı Portekizce kitabı yakalasın.
EUROPEAN_DETECT_LANGS: Tuple[str, ...] = ("de", "fr", "es", "it", "pt")
# Ayırt edici profili (başka listeyle ortak kelimesi atılmış) olan diller.
_EXCLUSIVE_LANGS: Tuple[str, ...] = ("en", "tr", "ru")


def _build_profiles(
    raw: Dict[str, Tuple[str, ...]],
) -> Dict[str, FrozenSet[str]]:
    """Ham listelerden ölçümde kullanılan profilleri kur.

    * en / tr / ru: başka herhangi bir listede de geçen kelime atılır.
    * de / fr / es / it / pt: İngilizce ya da Türkçe listede geçen kelime
      atılır; Avrupa dilleri arasındaki ortaklık korunur.
    * Tek harfli ASCII kelimeler hiçbir profile girmez (is_signal_word).
    """
    sets = {lang: {w for w in words if is_signal_word(w)}
            for lang, words in raw.items()}
    out: Dict[str, FrozenSet[str]] = {}
    for lang, words in sets.items():
        if lang in _EXCLUSIVE_LANGS:
            others = set().union(*(v for k, v in sets.items() if k != lang))
            out[lang] = frozenset(words - others)
        else:
            out[lang] = frozenset(words - sets["en"] - sets["tr"])
    return out


PROFILES: Dict[str, FrozenSet[str]] = _build_profiles(_RAW_PROFILES)
_FREQUENT_WORDS: Dict[str, FrozenSet[str]] = {
    lang: frozenset(words) for lang, words in _RAW_PROFILES.items()
}
# Avrupa ailesinin birleşik kelime kümesi (satır başına "aile kanıtı").
EUROPEAN_FAMILY_WORDS: FrozenSet[str] = frozenset().union(
    *(PROFILES[lang] for lang in EUROPEAN_DETECT_LANGS)
)


# ---------------------------------------------------------------------------
# LaTeX temizliği (ölçüm öncesi)
# ---------------------------------------------------------------------------

_COMMENT_RE = re.compile(r"(?<!\\)%[^\n]*")
_VERBATIM_ENV_RE = re.compile(
    r"\\begin\{((?:verbatim|Verbatim|lstlisting|minted|comment)\*?)\}"
    r".*?\\end\{\1\}",
    re.DOTALL,
)
_MATH_ENV_RE = re.compile(
    r"\\begin\{((?:equation|align|alignat|gather|multline|flalign|eqnarray"
    r"|displaymath|math|split|dmath|IEEEeqnarray)\*?)\}"
    r".*?\\end\{\1\}",
    re.DOTALL,
)
# '$…$', '$$…$$' ve '\[…\]' düz regexle değil TeX okuma sırasıyla bulunur
# (dollar_math_spans / bracket_display_spans, aşağıda).
_PAREN_INLINE_RE = re.compile(r"\\\(.*?\\\)", re.DOTALL)
# Argümanı düzyazı OLMAYAN komutlar: argümanıyla birlikte silinir.
_NONPROSE_CMD_RE = re.compile(
    r"\\(?:label|ref|eqref|pageref|autoref|cref|Cref|nameref|cite[a-zA-Z]*"
    r"|url|href|includegraphics|input|include|bibliographystyle|bibliography"
    r"|begin|end|documentclass|usepackage|hypersetup|setlength|setcounter"
    r"|addtocounter|vspace|hspace|newcommand|renewcommand|index|hypertarget"
    r"|hyperlink|pdfbookmark|color|textcolor|definecolor|fontsize|graphicspath)"
    r"(?![A-Za-z])\*?(?:[ \t]*\[[^\]\n]*\])*(?:[ \t]*\{[^{}\n]*\})?"
)
_CMD_NAME_RE = re.compile(r"\\(?:[A-Za-z@]+\*?|.)")
_URL_RE = re.compile(r"(?:https?://|www\.)\S+")


def _blank_keep_newlines(match: "re.Match[str]") -> str:
    """Eşleşmeyi boşluğa çevir; satır sonlarını koru (satır hizası bozulmasın)."""
    return re.sub(r"[^\n]+", " ", match.group(0))


# ---------------------------------------------------------------------------
# Matematik aralıkları: TeX'in okuma sırası (2026-09-29, F3-01 / Vq-1)
# ---------------------------------------------------------------------------
# Düz regex ('\$\$.*?\$\$', '\\\[.*?\\\]', DOTALL) iki gerçek kalıpta
# düzyazı yutuyordu: bitişik iki satır içi atomun ortasındaki '$$'
# ('$C_{4}$$H_{10}$' display açılışı sanılıyordu) ve Marker'ın atıf kaçışı
# ('\href{#page-157-0}{\[ 63}]': '\[' display açılışı sanılıyordu). İkisinde
# de belgedeki bir sonraki kapanışa kadar her şey — paragraflar boyunca —
# "matematik" oluyordu (bir test kitabında gövdenin %40'ı). Artık iki okuyucu da
# TeX gibi soldan okur ve paragraf sınırında sıfırlanır.

# '\[' / '\]' belirteçleri; '\\' (satır sonu; '\\[2pt]' seçeneği açılış
# DEĞİLDİR) ve öteki kaçışlar tek belirteç olarak atlanır.
_BRACKET_TOKEN_RE = re.compile(r"\\\[|\\\]|\\.|\n[ \t]*\n", re.DOTALL)


def braces_balanced(text: str) -> bool:
    """Kaçışsız süslü parantezler dengeli mi (derinlik hiç eksiye inmeden)?"""
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                return False
        i += 1
    return depth == 0


def bracket_display_spans(text: str) -> List[Tuple[int, int]]:
    r"""``\[ … \]`` display matematiğinin ``(başlangıç, bitiş)`` aralıkları.

    TeX gibi soldan okunur: ``\[`` açar, ``\]`` kapatır; paragraf sınırı
    (boş satır) açık adayı düşürür (TeX'te display içinde ``\par`` hatadır).
    Aday ancak içindeki süslü parantezler dengeliyse matematiktir: Marker'ın
    atıf kaçışı ``\href{#page-157-0}{\[ 63}]`` içinde ``\[``'dan sonra
    kapanan ``}`` gelir — bu bir display değildir. Aynı paragrafta birden
    çok açılış varsa ``\]``'yi, içi dengeli olan EN ERKEN açılış alır
    (atıf kaçışının arkasından gelen gerçek ``\[ x \]`` kaybolmaz).
    """
    spans: List[Tuple[int, int]] = []
    if "\\[" not in text:
        return spans
    openers: List[int] = []
    for m in _BRACKET_TOKEN_RE.finditer(text):
        tok = m.group(0)
        if tok[0] == "\n":
            openers = []
        elif tok == "\\[":
            openers.append(m.start())
        elif tok == "\\]":
            for start in openers:
                if braces_balanced(text[start + 2:m.start()]):
                    spans.append((start, m.end()))
                    break
            openers = []
    return spans


# '\\$' (çift ters bölü + dolar): eski dönüştürücü Markdown'daki kaçışlı
# doları ('\$1.10') böyle yazıyordu (iki test kitabının gövdesinde).
# TeX bunu satır sonu + matematik açılışı okur; yazarın niyeti düz dolardır.
_ESCAPED_BACKSLASH_DOLLAR_RE = re.compile(r"(?<!\\)(?:\\\\)+(\$)")


def literal_dollar_positions(text: str) -> FrozenSet[int]:
    """Çift ters bölüden sonra gelen (düz dolar sayılan) ``$`` konumları."""
    if "\\\\$" not in text:
        return frozenset()
    return frozenset(m.start(1) for m in _ESCAPED_BACKSLASH_DOLLAR_RE.finditer(text))


def dollar_math_spans(text: str) -> List[Tuple[int, int, bool]]:
    """``$…$`` / ``$$…$$`` aralıkları ``(başlangıç, bitiş, display_mı)``.

    Okuyucu ``latex_pipeline.sanitizer.dollar_math_spans`` (TeX okuma
    sırası, bitişik atom ``$a$$b$`` iki satır içi matematiktir, paragraf
    sınırında sıfırlanır; QC de bunu kullanır). Tek fark: çift ters bölüden
    sonra gelen ``$`` (``\\$4/day``) düz dolar sayılır — eski translator
    deseni de öyle sayıyordu; aksi hâlde iki fiyat arasındaki düzyazı
    "matematik" olup çevrilmeden kalırdı. Katman sırası için yerel içe
    aktarma: ``core`` modül yüklenirken ``latex_pipeline``'a bağlanmaz.
    """
    if "$" not in text:
        return []
    from latex_pipeline.sanitizer import dollar_math_spans as _spans
    literal = literal_dollar_positions(text)
    if literal:
        chars = list(text)
        for pos in literal:
            chars[pos] = " "
        text = "".join(chars)
    return _spans(text)


def _blank_spans(text: str, spans: Iterable[Tuple[int, int, bool]]) -> str:
    """Aralıkları boşalt: tek satırlık satır içi matematik tek boşluk olur,
    display ve çok satırlı aralıklarda satır sonları korunur (eski regex
    temizliğiyle aynı çıktı biçimi)."""
    out: List[str] = []
    pos = 0
    for start, end, display in sorted(spans):
        if start < pos:
            continue
        out.append(text[pos:start])
        seg = text[start:end]
        if display or "\n" in seg:
            out.append(re.sub(r"[^\n]+", " ", seg))
        else:
            out.append(" ")
        pos = end
    out.append(text[pos:])
    return "".join(out)


def clean_latex_for_detection(text: str) -> str:
    r"""Dil ölçümü için LaTeX'ten düzyazıyı ayıkla.

    Matematik (``$…$``, ``$$…$$``, ``\[…\]``, ``\(…\)``, equation/align/…
    ortamları), kod (verbatim/lstlisting/minted), yorumlar, URL'ler ve
    argümanı düzyazı olmayan komutlar (``\label``, ``\ref``, ``\cite``,
    ``\includegraphics``…) silinir; kalan komut ADLARI silinir ama metin
    argümanları (``\textbf{…}``, ``\footnote{…}``, ``\section{…}``) kalır.

    Satır sayısı KORUNUR (çok satırlı bloklar boşluklarla doldurulur) —
    QC tüm metni bir kez temizleyip satır satır okuyabilsin diye.
    """
    if not text:
        return ""
    out = _COMMENT_RE.sub(" ", text)
    out = _VERBATIM_ENV_RE.sub(_blank_keep_newlines, out)
    out = _MATH_ENV_RE.sub(_blank_keep_newlines, out)
    # 2026-09-29 (Vq-1 / F3-01): '$' ve '\[' matematiği TeX okuma sırasıyla
    # (bitişik atom ve Marker atıf kaçışı düzyazıyı yutmaz).
    out = _blank_spans(out, dollar_math_spans(out))
    out = _blank_spans(out, [(s, e, True) for s, e in bracket_display_spans(out)])
    out = _PAREN_INLINE_RE.sub(_blank_keep_newlines, out)
    out = _URL_RE.sub(" ", out)
    out = _NONPROSE_CMD_RE.sub(" ", out)
    out = _CMD_NAME_RE.sub(" ", out)
    return out


# ---------------------------------------------------------------------------
# Sayım yardımcıları
# ---------------------------------------------------------------------------

def script_letter_counts(text: str) -> Dict[str, int]:
    """Metindeki harfleri alfabeye göre say: ``{"latin": n, "cyrillic": n, …}``.

    Yalnız harfler (``str.isalpha``) sayılır; rakam, noktalama ve alfabe
    aralığındaki işaretler (Arapça rakamlar vb.) sayılmaz.
    """
    counts: Dict[str, int] = {"latin": len(LATIN_LETTER_RE.findall(text))}
    if not NON_LATIN_CHAR_RE.search(text):
        for name in SCRIPT_RANGES:
            counts[name] = 0
        return counts
    for name, rx in SCRIPT_CHAR_RES.items():
        counts[name] = sum(1 for ch in rx.findall(text) if ch.isalpha())
    return counts


def tokenize_words(text: str) -> List[str]:
    """Küçük harfe çevrilmiş harf dizileri (rakam/altçizgi ayırıcıdır)."""
    return [w.lower() for w in _WORD_RE.findall(text)]


def profile_hits(words: Iterable[str]) -> Tuple[Dict[str, int], int]:
    """Kelimeleri profillere eşle: ``(dil başına eşleşme, Avrupa aile sayısı)``.

    Avrupa dilleri arasında ortak bir kelime (que, il, se...) içerdiği her
    Avrupa diline sayılır; aile sayısı ise her kelimeyi bir kez sayar. en/tr
    profilleri ayrıktır (ortak kelimeleri atılmış).
    """
    hits = {lang: 0 for lang in LATIN_PROFILE_LANGS}
    family = 0
    for w in words:
        if w in EUROPEAN_FAMILY_WORDS:
            family += 1
            for lang in EUROPEAN_DETECT_LANGS:
                if w in PROFILES[lang]:
                    hits[lang] += 1
        elif w in PROFILES["en"]:
            hits["en"] += 1
        elif w in PROFILES["tr"]:
            hits["tr"] += 1
    return hits, family


def _best_european(hits: Dict[str, int]) -> str:
    """En çok eşleşen Avrupa dili (eşitlikte EUROPEAN_DETECT_LANGS sırası)."""
    return max(EUROPEAN_DETECT_LANGS, key=lambda lang: hits[lang])


# ---------------------------------------------------------------------------
# Kitap düzeyi tespit (Yol B kapısı)
# ---------------------------------------------------------------------------

@dataclass
class LanguageEstimate:
    """Kitap/metin düzeyi dil tahmini ve ayrıntıları (loglama/test için)."""

    code: str                 # "en", "de", "ru", "cyrillic", "cjk", "und"...
    confidence: float         # 0.0 - 1.0
    letters: int = 0          # ölçülen harf sayısı
    words: int = 0            # ölçülen kelime sayısı (Latin dalı)
    script_shares: Dict[str, float] = field(default_factory=dict)
    profile_hits: Dict[str, int] = field(default_factory=dict)
    family_hits: int = 0      # Avrupa ailesi (de/fr/es/it/pt) kelime sayısı
    # Kazanan dilin (Latin dalında kazanan grubun) işlev kelimesi kapsaması:
    # eşleşen kelime / ölçülen kelime. Kiril dalında Rusça profil kapsaması;
    # profili olmayan alfabelerde (CJK, Yunan, Arap, İbrani) 0.
    coverage: float = 0.0

    @property
    def name_tr(self) -> str:
        return language_name_tr(self.code)


def sample_text(text: str, max_chars: int = DETECT_SAMPLE_CHARS,
                windows: int = DETECT_SAMPLE_WINDOWS) -> str:
    """Uzun metinden eşit aralıklı pencerelerle örnek al (baş+orta+son)."""
    if len(text) <= max_chars:
        return text
    win = max_chars // windows
    step = (len(text) - win) / max(1, windows - 1)
    parts = [text[int(i * step): int(i * step) + win] for i in range(windows)]
    return "\n".join(parts)


def _russian_coverage(words: List[str]) -> float:
    """Kiril kelimelerin (tek harfliler dahil) Rusça profildeki payı."""
    if not words:
        return 0.0
    return sum(1 for w in words if w in PROFILES["ru"]) / len(words)


def _cyrillic_code(words: List[str]) -> str:
    """Kiril kelimelerde Rusça profil kapsaması yeterliyse 'ru'."""
    if _russian_coverage(words) >= RUSSIAN_MIN_COVERAGE:
        return "ru"
    return "cyrillic"


def analyze_language(text: str, *, already_clean: bool = False) -> LanguageEstimate:
    """Metnin (kitap gövdesi) baskın dilini ayrıntılarıyla tahmin et.

    Karar sırası:
      1. Harf sayısı ``DETECT_MIN_LETTERS`` altındaysa ``und`` (güven 0).
      2. Latin dışı bir alfabe harflerin ``SCRIPT_DOMINANCE_SHARE`` kadarını
         tutuyorsa o alfabenin dili; güven = alfabenin harf payı.
      3. Aksi halde Latin kelime profilleri üç grupta yarışır: İngilizce,
         Türkçe ve Avrupa ailesi (de/fr/es/it/pt). Güven = (kazanan grubun
         tüm eşleşmeler içindeki payı) × (kapsama doygunluğu) × (Latin harf
         payı). Avrupa ailesi kazanırsa dil, en çok eşleşen Avrupa dilidir.
    """
    clean = text if already_clean else clean_latex_for_detection(text)
    sample = sample_text(clean)
    counts = script_letter_counts(sample)
    total = sum(counts.values())
    shares = {k: (v / total if total else 0.0) for k, v in counts.items()}
    if total < DETECT_MIN_LETTERS:
        return LanguageEstimate("und", 0.0, letters=total, script_shares=shares)

    non_latin = {k: v for k, v in counts.items() if k != "latin"}
    top_script = max(non_latin, key=non_latin.get)
    top_share = shares[top_script]
    if top_share >= SCRIPT_DOMINANCE_SHARE:
        coverage = 0.0
        if top_script == "cyrillic":
            cyr_words = [w.lower() for w in _CYRILLIC_TOKEN_RE.findall(sample)]
            code = _cyrillic_code(cyr_words)
            coverage = _russian_coverage(cyr_words)
        else:
            code = _SCRIPT_TO_CODE[top_script]
        return LanguageEstimate(code, round(top_share, 3), letters=total,
                                script_shares=shares,
                                coverage=round(coverage, 3))

    words = [w for w in tokenize_words(sample) if LATIN_LETTER_RE.match(w)]
    if len(words) < DETECT_MIN_WORDS:
        return LanguageEstimate("und", 0.0, letters=total, words=len(words),
                                script_shares=shares)
    hits, family = profile_hits(words)
    groups = {"en": hits["en"], "tr": hits["tr"], "eu": family}
    group_total = sum(groups.values())
    if group_total == 0:
        return LanguageEstimate("und", 0.0, letters=total, words=len(words),
                                script_shares=shares, profile_hits=hits)
    top = max(groups, key=groups.get)
    code = _best_european(hits) if top == "eu" else top
    dominance = groups[top] / group_total
    coverage = groups[top] / len(words)
    conf = (dominance
            * min(1.0, coverage / PROFILE_FULL_COVERAGE)
            * shares["latin"])
    return LanguageEstimate(code, round(conf, 3), letters=total,
                            words=len(words), script_shares=shares,
                            profile_hits=hits, family_hits=family,
                            coverage=round(coverage, 3))


def detect_language(text: str) -> Tuple[str, float]:
    """Metnin baskın dilini ``(dil_kodu, güven)`` olarak döndür.

    Kodlar: ``en de fr es it pt ru tr`` (dil), ``cyrillic el arabic hebrew
    cjk`` (alfabe düzeyi), ``und`` (ölçülemedi). Güven 0-1 arasıdır; Yol B
    kapısı yalnız ``GATE_STOP_CONFIDENCE`` üstündeki İngilizce-dışı
    tespitlerde durdurur.
    """
    est = analyze_language(text)
    return est.code, est.confidence


def should_stop_for_source_language(
        est: LanguageEstimate,
        supported: Iterable[str] = TEX_SOURCE_LANGS) -> bool:
    """Yol B kapısı kararı: dil güvenle ölçüldü VE desteklenmiyorsa True.

    *supported* kaynak dil listesidir (varsayılan ``TEX_SOURCE_LANGS``;
    KARARLAR 14). "und" (ölçülemedi) hiçbir zaman durdurmaz. 28 Eylül 2026
    öncesinde liste yalnız İngilizceydi (çıkış 4 = "İngilizce değil").
    """
    return (est.code != "und"
            and est.code not in tuple(supported)
            and est.confidence >= GATE_STOP_CONFIDENCE)


def should_stop_before_extraction(
        est: LanguageEstimate,
        supported: Iterable[str] = TEX_SOURCE_LANGS) -> bool:
    """Yol B ÖN kapısı (çıkarım öncesi, gömülü metin) kararı.

    Tam kapının (:func:`should_stop_for_source_language`) şartlarına ek
    olarak en az ``PREGATE_MIN_LETTERS`` harf ve kazanan dilde en az
    ``PROFILE_FULL_COVERAGE`` işlev kelimesi kapsaması ister (bkz. eşiklerin
    açıklaması). Şartlardan biri tutmazsa ön kapı SESSİZCE geçer; karar
    çıkarım sonrasındaki tam kapıya kalır.
    """
    return (should_stop_for_source_language(est, supported)
            and est.letters >= PREGATE_MIN_LETTERS
            and est.coverage >= PROFILE_FULL_COVERAGE)


def clean_plain_text_for_detection(text: str) -> str:
    """Düz metni (PDF metin katmanı, EPUB metni) dil ölçümüne hazırla.

    :func:`clean_latex_for_detection` düz metne UYGULANMAZ: "%50" gibi bir
    ifade orada yorum sayılıp satırın geri kalanı silinirdi. Burada yalnız
    URL'ler atılır; sonuç ``analyze_language(..., already_clean=True)`` ile
    ölçülür.
    """
    return _URL_RE.sub(" ", text or "")


# ---------------------------------------------------------------------------
# Satır düzeyi sınıflandırma (QC)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LineVerdict:
    """Bir satırın çevrilmemiş kaynak dil tespiti."""

    code: str      # dil/alfabe kodu ("ru", "cyrillic", "el", "de"...)
    kind: str      # "block" (satır kaynak dilde) | "mixed" (Türkçe + kaynak)
    words: int     # kaynak dildeki kelime (CJK'da karakter) sayısı
    share: float   # satırdaki harflerin kaynak alfabedeki payı (Latin dalında 1.0)


# Satır kaynakça/künye gibi görünüyor mu (Rusça GOST künyesi dahil)?
# Kaynakça bölümü dışında kalan dipnot künyeleri ve \item listesine dökülmüş
# kaynakçalar için; künyedeki yabancı başlık meşrudur, "çevrilmemiş" değil.
_CITATION_YEAR_RE = re.compile(r"\b(?:1[5-9]|20)\d{2}\b")
_CITATION_MARK_RE = re.compile(
    r"(?:\b(?:М|Л|СПб|Киев|Минск)\.?\s*:|//|№\s*\d|—\s*\d+\s*с\.|\bС\.\s*\d|"
    r"\bстр\.|\bт\.\s*\d|\bИзд|\bизд\.|\bpp?\.\s*\d|\bvol\.|\bS\.\s*\d|"
    r"\bVerlag\b|\bHrsg\.|\b[ÉéEe]d\.|\bISBN\b)"
)
_CITATION_ITEM_RE = re.compile(r"^\s*\\(?:bib)?item\b")


def looks_like_citation(text: str) -> bool:
    r"""Kaba künye tespiti (ham LaTeX satırı ya da temizlenmiş metin).

    * ``\item``/``\bibitem`` ile başlayıp yıl içeren satır (listeye dökülmüş
      kaynakça: "\item de Laplace PS (1814) Essai philosophique ..."), ya da
    * ``CITATION_MAX_CHARS``'tan kısa, yıl VE yayın işareti (sayfa,
      yayınevi, //, №, ISBN) içeren satır ("М.: Машиностроение, 1968. — 396 с.").
    """
    if not _CITATION_YEAR_RE.search(text):
        return False
    if _CITATION_ITEM_RE.match(text):
        return True
    return (len(text) <= CITATION_MAX_CHARS
            and bool(_CITATION_MARK_RE.search(text)))


def classify_script_line(text: str) -> Optional[LineVerdict]:
    """Temizlenmiş bir satırda Latin dışı alfabeyle çevrilmemiş metin var mı?

    ``text`` :func:`clean_latex_for_detection` çıktısından bir satır olmalı
    (matematik/komutlar zaten maskeli). Dönüş:
      * ``kind="block"``: en az ``LINE_SCRIPT_MIN_WORDS`` kelime ve harflerin
        en az ``LINE_SCRIPT_MIN_SHARE`` payı o alfabede — satır çevrilmemiş.
      * ``kind="mixed"``: pay düşük ama en az ``LINE_SCRIPT_MIXED_MIN_WORDS``
        kelime — Türkçe satırın içinde çevrilmemiş cümle.
      * ``None``: tek harf/kısaltma/kısa alıntı ya da künye.
    """
    if not text or not NON_LATIN_CHAR_RE.search(text):
        return None
    counts = script_letter_counts(text)
    total = sum(counts.values())
    if not total:
        return None
    non_latin = {k: v for k, v in counts.items() if k != "latin"}
    script = max(non_latin, key=non_latin.get)
    if not non_latin[script]:
        return None
    share = non_latin[script] / total

    if script == "cjk":
        amount = non_latin[script]
        block_min, mixed_min = LINE_CJK_MIN_CHARS, LINE_CJK_MIXED_MIN_CHARS
        code = "cjk"
    else:
        amount = len(SCRIPT_WORD_RES[script].findall(text))
        block_min, mixed_min = LINE_SCRIPT_MIN_WORDS, LINE_SCRIPT_MIXED_MIN_WORDS
        if script == "cyrillic":
            # Dil adı için tek harfli Rusça işlev kelimeleri (в, и, с) de
            # sayılır; eşik kararı yukarıdaki 2+ harflik kelimelerledir.
            code = _cyrillic_code(
                [w.lower() for w in _CYRILLIC_TOKEN_RE.findall(text)])
        else:
            code = _SCRIPT_TO_CODE[script]

    if amount < block_min:
        return None
    if looks_like_citation(text):
        return None
    if share >= LINE_SCRIPT_MIN_SHARE:
        return LineVerdict(code, "block", amount, round(share, 3))
    if amount >= mixed_min:
        return LineVerdict(code, "mixed", amount, round(share, 3))
    return None


def classify_latin_line(text: str, *, heading: bool = False) -> Optional[str]:
    """Maskelenmiş bir satır/başlık çevrilmemiş Almanca/Fransızca/İspanyolca/
    İtalyanca/Portekizce mi? Evetse dil kodu, değilse None.

    ö/ü/ç Türkçe ile ORTAK olduğundan "Türkçe harf var" kısayolu yalnız
    Türkçeye özgü harflerle (ş, ğ, ı, İ) yapılır; bunlardan biri varsa satır
    Türkçe sayılır. Karar Avrupa ailesi işlev kelimesi eşleşmesine dayanır:
    en az ``LINE_LATIN_MIN_HITS`` eşleşme, ``LINE_LATIN_MIN_COVERAGE``
    kapsama ve İngilizce+Türkçe eşleşmelerin ``LINE_LATIN_DOMINANCE`` katı.
    """
    if not text or TURKISH_ONLY_RE.search(text):
        return None
    if heading:
        words = [w for w in tokenize_words(text) if is_signal_word(w)]
    else:
        # Gövde satırında 1-3 harflik TAMAMI BÜYÜK kelimeler yazar
        # baş harfleri ve kısaltmalardır (MA, JM, DES, LES) — dil kanıtı
        # sayılmaz. (Başlıklar tamamen büyük harfle yazılabildiği için
        # orada atılmaz.)
        words = [w.lower() for w in _WORD_RE.findall(text)
                 if not (w.isupper() and len(w) <= 3)
                 and is_signal_word(w.lower())]
    if heading:
        min_words, min_hits, min_cov = (HEADING_LATIN_MIN_WORDS,
                                        HEADING_LATIN_MIN_HITS,
                                        HEADING_LATIN_MIN_COVERAGE)
    else:
        min_words, min_hits, min_cov = (LINE_LATIN_MIN_WORDS,
                                        LINE_LATIN_MIN_HITS,
                                        LINE_LATIN_MIN_COVERAGE)
    if len(words) < min_words:
        return None
    hits, family = profile_hits(words)
    if family < min_hits or family / len(words) < min_cov:
        return None
    if family < LINE_LATIN_DOMINANCE * (hits["en"] + hits["tr"]):
        return None
    return _best_european(hits)


# ---------------------------------------------------------------------------
# Segment düzeyi kaynak dil kapısı (Yol B, MT öncesi) — 2026-09-24 eklendi
# ---------------------------------------------------------------------------
# Bir test koşusu (24 Eyl): İngilizce kitabın içindeki 6 Latince alıntı (Kepler
# Astronomia Nova, Newton Lex I–III) MT'ye gitti; model Türkçeye çevirip iki
# yerde ağır halüsinasyon üretti ("sesquialtera" = 3/2 oranı → "2:1").
# Yol A çevirmeni 6/6'sını Latince bırakmıştı. Aşağıdaki yardımcı bir MT
# SEGMENTİNİN (satır/paragraf) kaynak dilden (İngilizce) farklı bir dilde
# yazıldığını tespit eder; çağıran segmenti çevirmeden geçirir ve loglar.
#
# Yalnız EKLEME: yukarıdaki profiller, eşikler ve fonksiyonlar değişmedi.
# Latince profili bilerek PROFILES'a KATILMADI — _build_profiles en/tr
# ayrık kümelerini "başka profilde geçen kelimeyi at" kuralıyla kuruyor;
# Latince eklenirse "in", "per", "et" gibi kelimeler İngilizce profilinden
# düşer ve kitap düzeyi tespit değişir. O yüzden ayrı küme + ayrı eşleme.

# Latince işlev kelimeleri / sık biçimler. İngilizce ile ORTAK yazılışlar
# (in, per, sub, ad, id, re, via, de, non, post, super, a) bilerek dışarıda:
# İngilizce teknik metinde bunlar Latince kanıtı sayılmasın.
_LATIN_WORDS_RAW: Tuple[str, ...] = (
    "et", "ex", "cum", "sive", "seu", "vel", "aut", "nisi", "quod", "quae",
    "qui", "quam", "quia", "ut", "est", "sunt", "esse", "sicut", "itaque",
    "ergo", "enim", "autem", "vero", "sed", "nam", "hoc", "haec", "hic",
    "illa", "ille", "illud", "ipsa", "ipse", "ipsum", "ipsorum", "omne",
    "omnis", "omnia", "omnes", "omnium", "semper", "etiam", "iam", "tam",
    "ita", "sic", "ubi", "unde", "quare", "quo", "qua", "quibus", "huius",
    "eius", "cuius", "suo", "sua", "suum", "atque", "ac", "nec", "neque",
    "res", "ab", "contra", "ante", "sine", "inter", "praeter", "praeterquam",
    "propter", "propterea", "quatenus", "quorum", "quarum", "quibusdam",
    "dicitur", "dicatur", "fieri", "fiat", "fit", "habet", "habent",
    "potest", "possunt", "erat", "erant", "esset", "fuit", "sit", "sint",
    "idem", "eadem", "vis", "corpus", "corpora", "corporum", "motus",
    "tempus", "tempora", "temporis", "lex", "leges", "id", "se",
)
# Türkçe ve İngilizce profilleriyle çakışan kelimeler yine de atılır (ör.
# "se" es/fr/it/pt ile ortak ama en/tr'de yok → kalır; "id" İngilizce
# değil ama teknik metinde "id" geçebilir — kısa olduğu için tek harf
# kuralı değil, aşağıdaki ≥3 eşleşme + baskınlık kuralı korur).
LATIN_WORDS: FrozenSet[str] = frozenset(
    w for w in _LATIN_WORDS_RAW
    if w not in PROFILES["en"] and w not in PROFILES["tr"]
)
# Latince çekim/çekimli fiil sonları — yalnız ≥ LATIN_SUFFIX_MIN_LEN harfli
# kelimelerde sayılır ("forum", "quorum" gibi İngilizceye geçmiş kısa
# kelimeler ve "-que"li İngilizce "torque/unique" bilerek DIŞARIDA: -que
# listede yok).
_LATIN_SUFFIX_RE = re.compile(
    r"(?:ibus|orum|arum|ntur|atur|etur|itur|erunt|isse|issim[aeu][ms]?"
    r"|æ|ae|orum|abitur|abuntur)$"
)
LATIN_SUFFIX_MIN_LEN = 6
# Segment Latince sayılır: en az LINE_LATIN_MIN_WORDS kelime, en az
# LINE_LATIN_MIN_HITS Latince eşleşme, kapsama LINE_LATIN_MIN_COVERAGE ve
# İngilizce+Türkçe eşleşmelerin LINE_LATIN_DOMINANCE katı (satır düzeyi
# Avrupa dili kuralıyla AYNI eşikler — yeni eşik tanımlanmadı).


def latin_word_hits(words: Iterable[str]) -> int:
    """Kelimelerden kaçı Latince kanıtı (profil kelimesi ya da uzun Latince ek)?"""
    n = 0
    for w in words:
        if w in LATIN_WORDS:
            n += 1
        elif len(w) >= LATIN_SUFFIX_MIN_LEN and _LATIN_SUFFIX_RE.search(w):
            n += 1
    return n


# Çeviri katmanının yer tutucuları (<<LATEX_3>>, <<CMD_0>>, [[UNIT_1]],
# @@PERSON0001@@) ölçümden önce silinir — sayı/harf karışımı kelime sayılmasın.
_SEGMENT_PLACEHOLDER_RE = re.compile(
    r"<<[A-Z]+_\d+>>|\[\[[A-Za-z]+_\d+\]\]|@@[A-Za-z]+\d+@@"
)


def _segment_signal_words(clean: str) -> List[str]:
    """Segment kararında sayılan kelimeler (kısa büyük harf kısaltmalar hariç)."""
    return [w.lower() for w in _WORD_RE.findall(clean)
            if not (w.isupper() and len(w) <= 3) and is_signal_word(w.lower())]


def _is_latin_quote(words: List[str]) -> bool:
    """Kelimeler Latince alıntı mı (profil + ek; İngilizce/Türkçe baskınlığı)?"""
    la = latin_word_hits(words)
    hits, _family = profile_hits(words)
    return (la >= LINE_LATIN_MIN_HITS
            and la / len(words) >= LINE_LATIN_MIN_COVERAGE
            and la >= LINE_LATIN_DOMINANCE * (hits["en"] + hits["tr"]))


# Latince alıntı kararının GÜVENLİ olduğu İngilizce dışı kaynak diller. Roman
# dillerinde (fr/es/it) Latince profil kelimeleri kaynağın kendi işlev
# kelimeleriyle çakışır ("est", "se", "qui", "qua", "contra", "ante"): bir
# Fransızca paragraf "Latince" sayılıp ÇEVRİLMEDEN kalırdı. Orada karar
# verilmez, segment MT'ye gider.
_LATIN_QUOTE_SAFE_SOURCES: Tuple[str, ...] = ("de", "ru")


def classify_foreign_segment(text: str, source_lang: str = "en") -> Optional[str]:
    """MT'ye gidecek bir segment kaynak dilden BAŞKA bir dildeyse kodunu döndür.

    *source_lang* ``"en"`` (varsayılan; 2026-09-24 davranışı AYNEN): dönüş
    ``"la"`` (Latince), Avrupa dili kodu (``de/fr/es/it/pt``,
    :func:`classify_latin_line` kuralıyla), Latin dışı alfabe kodu
    (``ru/el/...``, :func:`classify_script_line` ``block`` kararıyla) ya da
    ``None`` (İngilizce / karar için yetersiz). Karar muhafazakârdır: yanlış
    pozitifin bedeli çevrilmemiş paragraf olduğundan kısa segmentler
    (< ``LINE_LATIN_MIN_WORDS`` kelime) hiç sınıflanmaz ve Latince için
    İngilizce eşleşmelere karşı baskınlık şartı aranır.

    İngilizce dışı kaynak (yerel model yolu, KARARLAR 14; 2026-09-28): yalnız
    kaynağın ALFABESİNDEN farklı yazılmış segment yabancı sayılır — Latin
    alfabeli kaynakta (de/fr/es/it) Kiril/Yunan/Arap… blok; Rusça kaynakta
    Kiril dışı blok ya da harflerinin çoğu Latin olan ve dili tanınan
    (Latince, Avrupa dili, İngilizce) segment. Latince alıntı ayrıca yalnız
    ``_LATIN_QUOTE_SAFE_SOURCES``'ta aranır. Aynı alfabedeki diller arası
    ayrım (Almanca kitapta Fransızca alıntı) yapılmaz: yanlış pozitif
    kaynağın kendi paragrafını çevrilmeden bırakırdı.
    """
    if not text:
        return None
    clean = clean_latex_for_detection(_SEGMENT_PLACEHOLDER_RE.sub(" ", text))
    if not clean.strip():
        return None
    source = source_lang or "en"
    verdict = classify_script_line(clean)
    if source != "en":
        return _foreign_segment_non_english(clean, source, verdict)
    if verdict is not None and verdict.kind == "block":
        return verdict.code
    if TURKISH_ONLY_RE.search(clean):
        return None
    words = _segment_signal_words(clean)
    if len(words) < LINE_LATIN_MIN_WORDS:
        return None
    if _is_latin_quote(words):
        return "la"
    return classify_latin_line(clean)


def _foreign_segment_non_english(clean: str, source: str,
                                 verdict: Optional[LineVerdict]) -> Optional[str]:
    """:func:`classify_foreign_segment`'in İngilizce dışı kaynak dalı."""
    source_is_cyrillic = source == "ru"
    if verdict is not None and verdict.kind == "block":
        if source_is_cyrillic and verdict.code in ("ru", "cyrillic"):
            return None
        return verdict.code
    if TURKISH_ONLY_RE.search(clean):
        return None
    if source_is_cyrillic:
        counts = script_letter_counts(clean)
        total = sum(counts.values())
        if not total or counts["latin"] / total < LINE_SCRIPT_MIN_SHARE:
            return None
    words = _segment_signal_words(clean)
    if len(words) < LINE_LATIN_MIN_WORDS:
        return None
    if source in _LATIN_QUOTE_SAFE_SOURCES and _is_latin_quote(words):
        return "la"
    if not source_is_cyrillic:
        return None
    european = classify_latin_line(clean)
    if european is not None:
        return european
    hits, _family = profile_hits(words)
    if (hits["en"] >= LINE_LATIN_MIN_HITS
            and hits["en"] / len(words) >= LINE_LATIN_MIN_COVERAGE):
        return "en"
    return None


def classify_untranslated_line(text: str, source_lang: str, *,
                               heading: bool = False) -> Optional[str]:
    """Türkçe çıktıda İngilizce DIŞI kaynak dilde çevrilmeden kalmış satır mı?

    Yerel model yolunun (KARARLAR 14) çevrilmemiş satır dedektörü; İngilizce
    kaynakta KULLANILMAZ (orada tek karar ``retranslator.is_english_line``).
    ``text`` maskelenmiş metin olmalıdır (LaTeX komutları/matematik atılmış).

    * ``ru``: Kiril alfabesiyle blok ya da karma satır
      (:func:`classify_script_line`); başlıkta en az
      ``HEADING_SCRIPT_MIN_WORDS`` Kiril kelime ve harflerin
      ``LINE_SCRIPT_MIN_SHARE`` payı.
    * ``de/fr/es/it``: Latin alfabeli Avrupa ailesi
      (:func:`classify_latin_line`). Aile içindeki diller kısa satırda
      karışabildiği için (es/it/pt) dil EŞİTLİĞİ aranmaz: çıktı Türkçe
      olmalıdır, Avrupa dilinde kalmış satır çevrilmemiştir (QC Check 2 ile
      aynı ölçüt).

    Dönüş: satırın dil kodu ya da ``None``.
    """
    if not text:
        return None
    if source_lang == "ru":
        if heading:
            counts = script_letter_counts(text)
            total = sum(counts.values())
            if not total or counts["cyrillic"] / total < LINE_SCRIPT_MIN_SHARE:
                return None
            if (len(SCRIPT_WORD_RES["cyrillic"].findall(text))
                    < HEADING_SCRIPT_MIN_WORDS):
                return None
            return _cyrillic_code(
                [w.lower() for w in _CYRILLIC_TOKEN_RE.findall(text)])
        verdict = classify_script_line(text)
        if verdict is not None and verdict.code in ("ru", "cyrillic"):
            return verdict.code
        return None
    if source_lang in EUROPEAN_DETECT_LANGS:
        return classify_latin_line(text, heading=heading)
    return None


# ---------------------------------------------------------------------------
# QC: kaynakta aynen geçen Latince alıntı (kulvar F2, 2026-09-30; MT-1)
# 2026-10-05: latex_pipeline/qc_engine.py'den taşındı (davranış aynen).
# Yukarıdaki segment kapısının LATIN_WORDS / _is_latin_quote'u AYRI bir
# karardır (MT segmenti, kapsama + en/tr baskınlığı); bu küme QC'nin satır
# kararıdır (bütün profil dillerine baskınlık).
# ---------------------------------------------------------------------------
# Kepler/Newton Latincesi kaynakta da Latincedir; model onu doğru olarak
# özgün dilinde bırakır. Latincenin bu modülde profili yok:
# satır Avrupa dili sayılmıyor, ``is_english_line``'ın içerik kuralı (her
# ASCII kelime İngilizce, "-s" çoğul izi) onu "Untranslated English
# paragraph" CRITICAL yapıyordu (bir model testi çıktısında üç CRITICAL + iki HIGH).
# Karar dil TESPİTİNE dayanır, yalnız "kaynakta birebir geçiyor"a değil:
# çevrilmemiş İngilizce satır da kaynakta birebir geçer. Satır ancak
# Latince işlev kelimeleri İngilizce (ve öteki profil dillerin) işlev
# kelimelerini açıkça geçiyorsa alıntı sayılır.
#
# İki küme:
#   * ``LATIN_FUNCTION_WORDS``: Latince işlev kelimeleri, Roman dilleriyle
#     ORTAK olanlar dahil ("et", "est", "non", "se", "qui", "esse").
#     Bu kelimeler rakip dil sayımından düşülür: "est" Fransızca profilde,
#     "esse" Portekizce profilde; Latince satırı Fransızca/Portekizce
#     saydırmamalı.
#   * Kanıt kelimeleri (``_latin_quote_words``): yukarıdakilerin İngilizce
#     ya da Türkçe metinde sözcük olarak geçebilenleri ÇIKARILMIŞ hâli.
#     "ad", "id", "hoc", "sic", "pro", "ex", "sub", "per", "in", "ut"
#     (UT = evrensel zaman), "sit", "vis", "quo", "ergo", "non" ("non-
#     linear" tokenleşince "non"), "se" ("per se") kanıt SAYILMAZ; ayrıca
#     İngilizce/Türkçe ham sık kelime listelerindekiler ("et") çalışma
#     anında atılır.
LATIN_FUNCTION_WORDS: frozenset = frozenset({
    # Latinceye özgü
    "quod", "quae", "quam", "quia", "quoniam", "quidem", "quis", "quibus",
    "cuius", "sed", "cum", "sive", "seu", "atque", "vel", "nisi", "ubi",
    "etiam", "enim", "autem", "igitur", "itaque", "quare", "quoque",
    "tamen", "nec", "neque", "aut", "tum", "ita", "haec", "huius", "huic",
    "hanc", "hunc", "sunt", "erit", "fuit", "eius", "eorum", "earum",
    "eam", "eum", "eas", "eos", "omnis", "omne", "omnes", "omnia", "apud",
    "secundum", "semper", "idem", "ipse", "ipsa", "ipsum", "ipsorum",
    "illa", "ille", "illud", "illum", "sicut", "velut", "unde", "inde",
    "ibi", "deinde", "simul", "quatenus", "quemadmodum", "vero", "esse",
    "est",
    # Roman dilleri / İngilizce ile ortak (rakip sayımdan düşülür, kanıt
    # sayılmaz ya da yalnız İngilizce/Türkçede sözcük değilse sayılır)
    "et", "non", "se", "qui", "de", "per", "in", "ad", "ex", "pro", "sub",
    "id", "hoc", "sic", "ut", "sit", "vis", "quo", "qua", "ergo", "a",
    "e", "si", "ne", "nam", "iam", "tam", "dum", "inter", "contra",
    "sine", "post", "ante", "super", "me", "te", "tu", "nos", "vos",
    "cui", "ab",
})
# İngilizce metinde sözcük (ya da tokenleşme parçası) olarak geçebilen
# Latince işlev kelimeleri: kanıt sayılmaz.
LATIN_EN_AMBIGUOUS: frozenset = frozenset({
    "ad", "id", "hoc", "sic", "pro", "ex", "sub", "ut", "sit", "vis", "quo",
    "qua", "ergo", "in", "a", "e", "per", "non", "se", "si", "ne", "iam",
    "tam", "dum", "inter", "contra", "post", "ante", "super", "me", "te",
    "tu", "nos", "ab", "nam", "sine", "vero", "cum", "ita",
})
# Latince alıntı kararı: satırda en az bu kadar Latince kanıt kelimesi VE
# bu sayı her profil dilinin (en/tr/de/fr/es/it/pt; Latinceyle ortak
# kelimeler hariç) işlev kelimesi sayısının SOURCE_QUOTE_LANG_DOMINANCE
# katı. Ölçüm (2026-09-30): output/ derlemindeki 45 297 satırda (EN, DE,
# FR, ES, IT, RU kaynaklar + Türkçe çeviriler) Latince sayılan satırlar
# yalnız bir test kitabının 6 Kepler/Newton satırı (kanıt 3–7, rakip 0); en yakın
# yabancı satırlar Fransızca ("est", "qui": kanıt 2–3, rakip 13–22).
LATIN_QUOTE_MIN_HITS = 3
# Baskınlık katı: satırın Latince kanıtı, en güçlü rakip profil dilinin
# işlev kelimesi sayısının en az bu katı. QC'deki
# ``qc_engine.SOURCE_QUOTE_LANG_DOMINANCE`` ile AYNI ilke ve değer (QC bu
# modülü tembel yüklediği için sabiti içe aktaramıyor; eşitlik
# tests/test_qc_cevirmen_notu_20261005.py'de korunur).
LATIN_QUOTE_DOMINANCE = 2


@functools.lru_cache(maxsize=1)
def _latin_quote_words() -> FrozenSet[str]:
    """Latince kanıt kelimeleri (bkz. ``LATIN_FUNCTION_WORDS``)."""
    taken = set(frequent_words("en")) | set(frequent_words("tr"))
    return frozenset(LATIN_FUNCTION_WORDS - LATIN_EN_AMBIGUOUS - taken)


def latin_quote_hits(words: Iterable[str]) -> Tuple[int, int]:
    """(Latince kanıt kelimesi sayısı, en güçlü rakip profil dilinin işlev
    kelimesi sayısı). *words* :func:`tokenize_words` çıktısıdır; Latinceyle
    ortak kelimeler rakip sayıma girmez."""
    words = list(words)
    marks = _latin_quote_words()
    latin = sum(1 for w in words if w in marks)
    hits, _family = profile_hits(
        w for w in words if w not in LATIN_FUNCTION_WORDS)
    return latin, max(hits.values()) if hits else 0


def is_latin_text(words: Iterable[str], *,
                  dominance: int = LATIN_QUOTE_DOMINANCE) -> bool:
    """Kelime dizisi Latince mi? (İngilizce/Türkçe/Avrupa dili DEĞİL.)

    En az ``LATIN_QUOTE_MIN_HITS`` Latince kanıt kelimesi VE bu sayı her
    profil dilinin işlev kelimesi sayısının en az *dominance* katı.
    """
    latin, rival = latin_quote_hits(words)
    return latin >= LATIN_QUOTE_MIN_HITS and latin >= dominance * max(1, rival)
