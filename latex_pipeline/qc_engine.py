"""
Post-translation quality control engine.

Analyzes translated LaTeX output against the English source and detects
issues automatically. Runs 35 independent checks:

    1. Formula Integrity     -- equation count parity, \\tag{} matching
    2. Untranslated Blocks   -- source text that wasn't translated (English,
                                German/French/Spanish/Italian/Portuguese,
                                Cyrillic/Greek/Arabic/Hebrew/CJK script)
    3. Prompt Leakage        -- MT system prompt fragments in output
    4. Name/Ref Corruption   -- proper nouns mangled, \\ref/\\cite mismatch
    5. Caption Consistency   -- mixed Fig./Sekil, Table/Tablo terminology
    6. Placeholder Orphans   -- leftover <<LATEX_N>>, [[UNIT_N]], @...@@
    7. Broken LaTeX Syntax   -- \\textit>, markdown images, Asian brackets
    8. Brace Balance         -- unmatched { } count
    9. Markdown Link Remnant -- [[N](#page)], ![](src) patterns
   10. Table Column Mismatch -- tabular column vs & count
   11. Untranslated Headings -- \\chapter/\\section still in the source language
   12. Broken Inline Math    -- fragmented $^{N}$ ) $^{T}$ patterns
   13. LaTeX-Encoded Turkish -- \\c{S}ekil, B\\"{o}l\\"{u}m instead of UTF-8
   14. Information Loss       -- section/chapter count mismatch EN vs TR
   15. Equation Count         -- total equation environment count comparison
   16. Figure Reference Integrity -- \\includegraphics / \\ref{fig:} count
   17. Section Number Consistency -- section/chapter numbering (e.g. 4.4.2)
   18. Href Preservation      -- \\href count parity EN vs TR
   19. Foreign Language Leakage -- Spanish/French/German words in Turkish text
   20. Antonym Guard           -- opposite-meaning term substitution detection
   21. First Person Academic   -- 1st person pronouns in academic text
   22. Number Hallucination Guard -- cross-ref numbers EN vs TR comparison
   23. Sentence Length Alignment  -- EN/TR sentence length ratio check
   24. Translated Env Name       -- \\begin{sekil} etc. (env names translated)
   25. Broken Command Braces     -- \\textbfXxx missing braces after command
   26. Stash Placeholder Orphan  -- <<REF_N>>, <<TERM_N>> etc. unrestored
   27. Word Concatenation        -- lost spaces: "fonksiyonlarıHer" etc.
   28. HTML Artifact Detection   -- colgroup/tbody/div/span tags in LaTeX
   29. Hallucination Detection   -- length ratio, repetition loops, orphan numbers
   30. ASCII Degradation         -- Turkish chars degraded to ASCII (ö→o, ş→s, etc.)
   31. Glossary Compliance       -- high-priority glossary terms left untranslated
   32. Wrong Translation         -- known wrong translations from glossary/hardcoded
   33. Hallucination Patterns    -- repeated words, fabricated refs, long words
   34. Turkish Naturalness       -- unnatural Turkish sentence patterns
   35. Index Preservation        -- \\index{} count parity EN vs TR

Usage:
    engine = QCEngine(glossary_dir="glossary/")
    report = engine.analyze(translated_body, source_body)
    report.print_summary()

No external dependencies beyond stdlib + re + json (source-language
detection comes from the project's own dependency-free core.lang_detect).
"""

from __future__ import annotations

import functools
import json
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from latex_pipeline.patterns import (
    TRANSLATOR_NOTE_FOOTNOTE_OPEN_RE,
    XREF_FABRICATION_CATEGORIES,
    xref_label_alternation,
)
from latex_pipeline.kutu_onarim import ORTAM as _KUTU_ORTAMI

logger = logging.getLogger(__name__)


def _lang_detect():
    """``core.lang_detect``'i tembel yükle.

    Alfabe tanımları ve kaynak-dil eşikleri orada tek yerde durur (Yol B dil
    kapısı ile ortak). Modül düzeyinde import edilmez: ``core`` paketinin
    ``__init__``'i ağır modüller yüklüyor, QC'nin import maliyeti artmasın.
    """
    from core import lang_detect
    return lang_detect


_STRAY_DOLLAR_RE = re.compile(r"(?<!\\)\$")


def _clean_for_language(text: str) -> str:
    """Dil ölçümü için temizlik: önce '$' matematiği TeX gibi okunup
    boşaltılır, sonra ``lang_detect.clean_latex_for_detection``.

    2026-09-28 (Vq): temizleyicinin display deseni ('$$.*?$$', DOTALL)
    bitişik iki satır içi atomun ortasındaki '$$'yi ('$\\alpha$${}^{0}$';
    sanitizer'ın metin kipi sarmalaması bunu çok üretir) display açılışı
    sanıp belgedeki bir sonraki '$$'ye kadar her satırı boşaltıyordu:
    aradaki çevrilmemiş Almanca/Fransızca paragraf Avrupa dili
    sınıflandırıcısına hiç ulaşmıyordu (çevrilmemiş paragraf kaçıyordu).
    ``sanitizer.dollar_math_spans`` paragraf sınırında sıfırlanır; kalan
    eşsiz '$' da atılır. Satır sayısı ve hizası korunur.
    """
    if not text or "$" not in text:
        return _lang_detect().clean_latex_for_detection(text)
    from latex_pipeline.sanitizer import dollar_math_spans
    chars = list(text)
    for start, end, _display in dollar_math_spans(text):
        for k in range(start, end):
            if chars[k] != "\n":
                chars[k] = " "
    blanked = _STRAY_DOLLAR_RE.sub(" ", "".join(chars))
    return _lang_detect().clean_latex_for_detection(blanked)


# ---------------------------------------------------------------------------
# Kaynak dil taraması: bağlantılar ve komut argümanları (kulvar Q, 2026-09-29)
# ---------------------------------------------------------------------------

# Tek satırlık çevrilmemiş birimin önem eşikleri (karakter): bu uzunlukta
# satır/argüman "paragraf" (critical) ya da "cümle(ler)" (high) sayılır;
# daha kısası medium. Check 2'nin satır ve argüman bulguları aynı eşikleri
# kullanır.
UNTRANSLATED_PARAGRAPH_CHARS = 200
UNTRANSLATED_SENTENCE_CHARS = 100

# F2-03: bağlantı belirteçleri — eskiden satırı dil denetiminden BÜTÜNÜYLE
# muaf tutan desenin (https?://, doi.org/, DOI:, arXiv:) AYNISI; artık
# yalnız belirtecin kendisi maskelenir. Süslü ayraç belirtece dahil
# edilmez: "\url{https://x}}" maskelenince dipnotun kapanışı yerinde
# kalmalı. ("www." ile başlayan adresler eskiden de muaf değildi; onları
# lang_detect temizliği zaten siler.)
_URL_TOKEN_RE = re.compile(
    r"https?://[^\s{}]*|doi\.org/[^\s{}]*|DOI:\s*[^\s{}]*|arXiv:\s*[^\s{}]*"
)

# Künye ipuçları (kaynakça bölümü dışında kalan kaynak girdileri; dipnottaki
# künye). Künye çevrilmez, dil denetimi onu "çevrilmemiş" saymaz.
#   * yıl + yayın işareti (eski ``\item`` künye kuralının sözcükleri, sözcük
#     sınırlı: Almanca "Presse", İngilizce "pressure" sayılmaz);
#   * yazar listesiyle başlayıp parantezli yıl: "Roe JQ (2012)",
#     "\item Doe AB (2021b)", "Poe CD, Moe EF (2019 Mar 29)" — yıldan
#     önceki kelimelerin hepsi büyük harfle başlar (ya da "and/et al/van"
#     gibi ad parçasıdır); "As shown by Smith (2019)" künye değildir;
#   * cilt(sayı):sayfa: "12(3):45–67", "7(2): 101".
_REFERENCE_HINT_RE = re.compile(
    r"\b\d{4}\b.*(?:\bpp\.|\bpages\b|\bvol\.|\bjournal\b|\bproceedings\b|"
    r"\bspringer\b|\bwiley\b|\belsevier\b|\bieee\b|\bacm\b|\bpress\b|"
    r"\bpublisher\b)",
    re.IGNORECASE,
)
_REFERENCE_YEAR_PAREN_RE = re.compile(
    r"\((?:1[5-9]|20)\d{2}[a-z]?\b[^()]{0,12}\)")
_REFERENCE_VOL_ISSUE_RE = re.compile(r"\b\d+\s*\(\d+\)\s*:\s*\d+")
_REFERENCE_ITEM_PREFIX_RE = re.compile(
    r"^\s*\\(?:bib)?item\b(?:\s*\[[^\]]*\])?")
# Yazar listesinde küçük harfle yazılabilen ad parçaları ve bağlaçlar.
_REFERENCE_NAME_PARTICLES = frozenset({
    "and", "et", "al", "van", "von", "de", "der", "den", "da", "di", "du",
    "le", "la", "ve", "und", "y", "e",
})
# Künye girdisinin uzunluk sınırı ve parantezli yılın aranacağı baş kısım
# (yazar listesi + yıl). Daha uzun metin künye değil düzyazıdır.
REFERENCE_MAX_CHARS = 600
REFERENCE_YEAR_HEAD_CHARS = 200


def _looks_like_reference(text: str) -> bool:
    """Metin (satır ya da argüman) bir künye girdisine mi benziyor?

    ``lang_detect.looks_like_citation``'ı tamamlar (o, dergi adını ve
    yazar-yıl biçimini işaret saymaz). Uzun düzyazı paragrafı
    (``REFERENCE_MAX_CHARS``'tan uzun) künye sayılmaz.
    """
    if len(text) > REFERENCE_MAX_CHARS:
        return False
    if len(text) <= _lang_detect().CITATION_MAX_CHARS and \
            _REFERENCE_HINT_RE.search(text):
        return True
    if _REFERENCE_VOL_ISSUE_RE.search(text):
        return True
    m = _REFERENCE_YEAR_PAREN_RE.search(text[:REFERENCE_YEAR_HEAD_CHARS])
    if m is None:
        return False
    words = _LETTER_RUN_RE.findall(
        _REFERENCE_ITEM_PREFIX_RE.sub(" ", text[:m.start()]))
    return bool(words) and all(
        w[0].isupper() or w.lower() in _REFERENCE_NAME_PARTICLES
        for w in words)


# Web/gazete künyesi (sonnot ve dipnotta): yıl + başlık işareti (tırnak ya da
# italik) ya da alan yapısı (en az bu kadar virgül). Sentetik örnek: "The
# Daily Courier, “City Opens New Library ...” Jane Doe, March 3, 2019, <URL>".
_REFERENCE_YEAR_RE = re.compile(r"\b(?:1[5-9]|20)\d{2}\b")
_REFERENCE_TITLE_MARK_RE = re.compile(r"[“”«»„\"]|\\(?:textit|emph)\s*\{")
REFERENCE_MIN_FIELDS = 3
# Bağlantılı satırın (ya da dipnotun) bağlantı dışında kalan düzyazısı bundan
# kısaysa bağlantı satırı sayılır ("Web sitemizi <URL> adresinden ziyaret
# edin."); tek satırlık çevrilmemiş birimin "cümle" eşiğiyle aynı.
LINK_LINE_MIN_PROSE_CHARS = UNTRANSLATED_SENTENCE_CHARS


def _looks_like_web_reference(text: str) -> bool:
    """Bağlantılı satır ya da dipnot bir web/gazete künyesi mi?

    Yalnız bağlantı içeren satırlarda ve dipnot/altyazı argümanlarında
    kullanılır (``_looks_like_reference``'tan geniştir): yıl VE (başlık
    işareti ya da en az ``REFERENCE_MIN_FIELDS`` virgül), en fazla
    ``REFERENCE_MAX_CHARS`` karakter.
    """
    return (len(text) <= REFERENCE_MAX_CHARS
            and _REFERENCE_YEAR_RE.search(text) is not None
            and (_REFERENCE_TITLE_MARK_RE.search(text) is not None
                 or text.count(",") >= REFERENCE_MIN_FIELDS))


# F2-02: argümanı DÜZYAZI olan komutlar. _LATEX_CMD_RE komutu argümanıyla
# birlikte sildiği için kaynak dilde kalmış dipnot, italik paragraf ya da
# altyazı dil denetimine hiç ulaşmıyordu. Argümanlar ayrı birim olarak
# sınıflanır.
#   * Birim komutları: metin akışından ayrı basılan metin (dipnot, altyazı,
#     kenar notu). Çevrilmemiş argüman kendi başına "untranslated" bulgusu.
#   * Satır içi biçim komutları: cümle içinde terim, kitap adı, alıntı
#     taşır. Satırda başka düzyazı yoksa satırın kendisidir
#     ("\textit{<paragraf>}"); Türkçe cümlenin içindeyse denetlenmez —
#     özgün dilinde bırakılan kitap adı ve alıntı meşrudur (bir test
#     kitabında ölçüldü; sentetik örnek: "\textit{The Counting Craft: A
#     Practical Guide to ...}" 17 kelime).
#   * ``\href{url}{metin}``: bağlantı metni satır içi düzyazıdır.
PROSE_UNIT_COMMANDS = frozenset({
    "footnote", "footnotetext", "caption", "marginpar",
})
PROSE_INLINE_COMMANDS = frozenset({
    "textit", "textbf", "emph", "textsl", "textsc", "textup", "textnormal",
    "textrm", "textsf", "underline", "uline", "enquote", "href",
})
# Komuttan önceki çift ters bölü satır sonudur: "metin\\\textit{...}" = satır
# sonu + \textit. Ters bölüsü kaçışlı "\\textit" komut değildir.
_PROSE_ARG_CMD_RE = re.compile(
    r"(?<!\\)(?:\\\\)*\\("
    + "|".join(sorted(PROSE_UNIT_COMMANDS | PROSE_INLINE_COMMANDS,
                      key=len, reverse=True))
    + r")(?![A-Za-z])\*?"
)
# İç içe argüman (dipnot içinde italik paragraf) en fazla bu derinliğe
# kadar ayrıştırılır.
PROSE_ARG_MAX_DEPTH = 2


def prose_command_args(text: str) -> List[Tuple[str, str, int, int]]:
    r"""Metindeki EN DIŞ düzeydeki düzyazı komutları.

    Dönüş: ``(komut, argüman, başlangıç, bitiş)``; ``text[başlangıç:bitiş]``
    komutu argümanıyla kapsar. İsteğe bağlı ``[...]`` argümanları atlanır
    (``\caption[kısa]{uzun}``: uzun altyazı sınıflanır); ``\href``'in ilk
    (adres) argümanı atlanır, ikincisi (bağlantı metni) alınır. Satır
    sonuna kadar kapanmayan argüman (bir sonraki satıra taşan dipnot)
    satırın sonuna kadar alınır — metin kaybolmaz, denetimden kaçmaz.
    İç içe komutlar dıştakinin argümanında kalır.
    """
    out: List[Tuple[str, str, int, int]] = []
    n = len(text)
    consumed = 0
    for m in _PROSE_ARG_CMD_RE.finditer(text):
        cmd_start = m.start(1) - 1          # komutun ters bölüsü
        if cmd_start < consumed:
            continue
        cmd = m.group(1)
        pos = m.end()
        while True:
            k = pos
            while k < n and text[k] in " \t":
                k += 1
            if k < n and text[k] == "[":
                close = text.find("]", k)
                if close == -1:
                    break
                pos = close + 1
            else:
                break
        if cmd == "href":
            first = _read_brace_group(text, pos)
            if first is None:
                continue
            pos = first[1]
        group = _read_brace_group(text, pos)
        if group is None:
            k = pos
            while k < n and text[k] in " \t":
                k += 1
            if k < n and text[k] == "{":
                out.append((cmd, text[k + 1:], cmd_start, n))
                consumed = n
            continue
        out.append((cmd, group[0], cmd_start, group[1]))
        consumed = group[1]
    return out


def _cut_spans(text: str, spans: Iterable[Tuple[int, int]]) -> str:
    """*text*'ten verilen aralıkları boşlukla değiştirerek çıkar."""
    out: List[str] = []
    pos = 0
    for start, end in sorted(spans):
        if start < pos:
            continue
        out.append(text[pos:start])
        out.append(" ")
        pos = end
    out.append(text[pos:])
    return "".join(out)


def mask_for_language_check(text: str) -> str:
    """Dil kararı öncesi LaTeX komutlarını (argümanlarıyla), matematiği ve
    yer tutucuları at; kalan harfleri tek boşlukla döndür.

    QC'nin satır düzeyi İngilizce taraması, yabancı dil sızıntısı ve birinci
    şahıs denetimleri bunu kullanır (eskiden ``QCEngine`` metodu; davranış
    aynı).
    """
    masked = _LATEX_PLACEHOLDER_RE.sub(" ", text)
    masked = _INLINE_BLOCK_RE.sub(" ", masked)
    masked = _DISPLAY_BLOCK_RE.sub(" ", masked)
    masked = _LATEX_CMD_RE.sub(" ", masked)
    masked = masked.replace("{", " ").replace("}", " ")
    masked = re.sub(r"[0-9]+", " ", masked)
    return re.sub(r"\s+", " ", masked).strip()


def unit_source_language(text: str, web_ref: bool = False) -> Optional[str]:
    """Tek bir metin biriminin (satırın dış metni, dipnot/altyazı/italik
    argümanı, EPUB bloğu) çevrilmemiş kaynak dili.

    Dönüş: ``"en"``, Avrupa dili (``de/fr/es/it/pt``) ya da Latin dışı
    alfabe kodu (``ru/el/...``); Türkçe, karar için yetersiz (kısa terim,
    kitap adı) ya da künye ise None. QC Check 2'nin satır düzeyiyle AYNI
    ölçütler: Latin dışı alfabede ``classify_script_line`` "block", Latin
    alfabeli Avrupa dillerinde ``classify_latin_line``, İngilizcede cümle
    bazında ``retranslator.is_english_line`` (Türkçe harfli birim İngilizce
    sayılmaz; o karışık satırdır). Künye biçimli birim muaftır; *web_ref*
    (bağlantılı satır, dipnot/altyazı argümanı) web/gazete künyesini de muaf
    tutar. Metin ham LaTeX olabilir: düzyazı komutlarının argümanları
    korunur, diğer komutlar ve matematik atılır.

    Kaynağın kendisinde aynen geçen üçüncü dil alıntısı muafiyeti kaynak
    bağlamı ister; o ``QCEngine._flagged_unit_language``'dadır.
    """
    if not text or not text.strip():
        return None
    ld = _lang_detect()
    if ld.looks_like_citation(text) or _looks_like_reference(text):
        return None
    if web_ref and _looks_like_web_reference(text):
        return None
    clean = _clean_for_language(text)
    if ld.NON_LATIN_CHAR_RE.search(clean):
        verdict = ld.classify_script_line(clean)
        if verdict is not None and verdict.kind == "block":
            return verdict.code
    eu_lang = ld.classify_latin_line(clean)
    if eu_lang is not None:
        return eu_lang
    if any(c in text for c in _TURKISH_CHARS):
        return None
    from latex_pipeline.retranslator import is_english_line
    masked = mask_for_language_check(_PROSE_ARG_CMD_RE.sub(" ", text))
    for sent in re.split(r"(?<=[.?!])\s+", masked):
        sent = sent.strip()
        if len(sent) >= 20 and is_english_line(sent, min_words=4,
                                               min_length=20):
            return "en"
    return None


@functools.lru_cache(maxsize=4)
def _chapter_label_parity(text: str) -> str:
    """Sanitizer'ın bölüm etiketi kurallarını (tek kaynak:
    ``sanitizer.apply_chapter_label_rules``) metne uygular.

    Yapı sayan üç check (information_loss, section_number_consistency,
    number_hallucination) bunu kaynağa ve çeviriye uygular; aynı gövde için
    bir analizde altı kez çağrıldığından sonuç önbelleklenir. Sanitizer
    çeviride "\\section{Bölüm 3}" etiketini silip/birleştiriyor, kaynağı
    dönüştürmeyen QC eski EXPORT.tex'li kusursuz çeviriye sahte "bölüm
    kaybı" CRITICAL'ı veriyordu (denetim 2026-09-24 M1#1).
    """
    from latex_pipeline.sanitizer import apply_chapter_label_rules
    return apply_chapter_label_rules(text)


def _build_xref_num_re(label_alternation: str) -> "re.Pattern":
    """Atıf numarası deseni: ``<etiket>[\\s~]*<numara>``.

    *label_alternation* yabancı/İngilizce etiket sözcükleridir
    (``patterns.xref_label_alternation``). Türkçe etiketler ve Türkçe ek
    almış etiket dalı her iki tarafta aynıdır. İki yakalama grubu: çıplak
    etiket numarası ya da ek almış etiketin noktalı numarası.
    """
    return re.compile(
        r"\b(?:"
        r"(?:" + label_alternation +
        # Turkish labels, bare or plural ("Bölümler 10–12") form
        r"|(?:Şekil|Tablo|Tanım|Teorem|Lemma|Önerme|Sonuç|Örnek"
        r"|Alıştırma|Algoritma|Bölüm|Denklem|İspat|Özellik)(?:ler|lar)?"
        # translator abbreviations (_FIGURE_LABEL_MAP); büyük/küçük harf
        # DUYARLI: küçük "denk." Türkçe sıfattır ("birbirine denk. 3 ...").
        # Küçük "şek." kısaltması çeviri tarafında ayrı desenle eklenir
        # (_XREF_TR_LOWER_ABBREV_RE); bu dizge 2026-09-24 deseniyle
        # birebir aynı kalır (test_dalga_q: test_tr_side_pattern_unchanged).
        r"|(?-i:Şek\.|Denk\.)"
        r")[\s~]*(\d+(?:\.\d+)*)"
        # Turkish labels with a case suffix: dotted numbers only
        r"|(?:Teorem|Lemma|Önerme|Sonuç|Örne[kğ]|Alıştırma|Algoritma"
        r"|Bölüm|Denklem|İspat|Özellik"
        r")\w+[\s~]*(\d+(?:\.\d+)+)"
        r")",
        re.IGNORECASE,
    )


# Kaynakta PARANTEZLİ denklem atfı: "Eq. (1.3)", "формулы (1.3)",
# "Gl. (3)". number_hallucination'ın numara sayımı parantezi atlamaz (çeviri
# tarafı deseni de atlamaz, simetrik kalsın); bu desen yalnız "çeviride var,
# kaynakta yok" (uydurma) yönünde muafiyet için kullanılır: çevirmen
# "Denklem 1.3" yazınca numara kaynakta vardır.
_XREF_PAREN_EQ_SRC_RE = re.compile(
    r"\b(?:" + xref_label_alternation(("equation",)) + r")"
    r"[\s~]*\([\s~]*(\d+(?:\.\d+)*)[\s~]*\)",
    re.IGNORECASE,
)


# OCR nokta kaybı uzlaştırması (QCEngine._reconcile_ocr_dot_loss): kaynakta
# noktası düşmüş numara ("11" = 1.1) en az iki hanelidir, çevirideki aday
# noktalıdır; ikisi de sıfırla başlamaz.
_OCR_DOTLESS_REF_RE = re.compile(r"[1-9]\d+")
_OCR_DOTTED_REF_RE = re.compile(r"[1-9]\d*(?:\.\d+)+")

# OCR'ın noktayı BOŞLUĞA çevirdiği kaynak atıf ("Рис. 1 2." = 1.2,
# "Рис 1 13" = 1.13): etiketli çıplak numarayı izleyen boşluk + 1-3 hane.
# Ardında ondalık/nokta-rakam varsa ("1 2.5") eşleşmez.
_OCR_SPACED_TAIL_RE = re.compile(r"[ \t]+([1-9]\d{0,2})\b(?![.,]\d)")

# Baskı/OCR ön ek hatası uzlaştırması (QCEngine._ocr_prefix_variants):
# kaynaktaki noktalı atıf numaralarının en az bu payı tek bir ön eki
# (bölüm numarasını) taşıyorsa belge tek bölümlü sayılır.
OCR_PREFIX_DOMINANT_SHARE = 0.8


# "Uydurulmus referans" denetiminin KAYNAK tarafı: Şekil/Tablo/Denklem
# karşılığı olan etiketler, altı kaynak dilde (patterns.XREF_LABEL_WORDS),
# noktalı numara. Denklem numarası parantezli olabilir ("формулы (1.1)",
# "Gl. (2.3)"). Başta \b YOK (2026-09-24 deseni gibi): kaynak kümesi eski
# kümenin üst kümesi kalsın — bu denetimde kaynak tarafı genişlemesi yalnız
# sahte "uydurma" bulgusunu azaltabilir.
_XREF_FABRICATION_SRC_RE = re.compile(
    r"(?:" + xref_label_alternation(XREF_FABRICATION_CATEGORIES) + r")"
    r"[\s~]*\(?[\s~]*(\d+(?:\.\d+)+)",
    re.IGNORECASE,
)


# 2026-10-05: grafik içi adların eşlemesi (Berke kararı, 30 Eylül; elle
# onarım) altyazının sonuna parantez içinde çevirmen notuyla veriliyor:
# "(Çevirmen notu: grafikteki adlar — …)". Kaynakta karşılığı olmayan meşru
# ektir; dipnot biçimi gibi uzunluk hesaplarına sayılmaz.
_TRANSLATOR_NOTE_PAREN_OPEN_RE = re.compile(
    r"\(\s*(?:\\(?:textit|emph|textbf|textsl)\s*\{\s*)?"
    r"(?:Çevirmen|Çeviri|Çevirenin|Çevirmenin)\s+notu\b",
    re.IGNORECASE,
)


def _strip_balanced(text: str, open_re: "re.Pattern[str]",
                    opener: str, closer: str) -> str:
    """``open_re`` ile başlayıp ``opener``/``closer`` dengesiyle kapanan
    parçaları çıkarır. Ters bölüyle kaçışlı karakterler (``\\{``, ``\\(``)
    sayılmaz; kapanışı bulunamayan (bozuk) parça olduğu gibi bırakılır.
    Hiçbir şey çıkarılmazsa aynı nesneyi döndürür (çağıranlar ``is`` ile
    bakıyor)."""
    out: List[str] = []
    pos = 0
    for m in open_re.finditer(text):
        if m.start() < pos:
            continue  # önceki notun içinde kalan eşleşme
        i = text.find(opener, m.start())
        depth = 0
        end = -1
        while i < len(text):
            ch = text[i]
            if ch == "\\":
                i += 2
                continue
            if ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
            i += 1
        if end < 0:
            continue
        out.append(text[pos:m.start()])
        pos = end
    if not out:
        return text
    out.append(text[pos:])
    return "".join(out)


def strip_translator_notes(text: str) -> str:
    """Çevirmen notlarını metinden çıkarır: ``\\footnote{Çevirmen notu: ...}``
    dipnotları ve altyazı içi ``(Çevirmen notu: ...)`` parantezleri.

    Dipnot açılışı ``patterns.TRANSLATOR_NOTE_FOOTNOTE_OPEN_RE``, parantez
    açılışı ``_TRANSLATOR_NOTE_PAREN_OPEN_RE`` ile bulunur; kapanış dengeli
    sayılır (kaçışlı ayraçlar sayılmaz). Kapanışı bulunamayan (bozuk) not
    olduğu gibi bırakılır. Diğer dipnotlara ve parantezlere dokunulmaz.
    Yalnız QC'nin uzunluk hesaplarında kullanılır; derlenen metin değişmez.
    """
    if "\\footnote" in text:  # hızlı eleme
        text = _strip_balanced(text, TRANSLATOR_NOTE_FOOTNOTE_OPEN_RE, "{", "}")
    if "notu" in text.lower():  # hızlı eleme
        text = _strip_balanced(text, _TRANSLATOR_NOTE_PAREN_OPEN_RE, "(", ")")
    return text


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class QCIssue:
    """Single quality issue found during analysis."""

    check_name: str       # "formula_integrity", "untranslated", etc.
    severity: str         # "critical", "high", "medium", "low"
    line_num: int         # 1-based line number in translated text
    message: str          # Human-readable description
    auto_fixable: bool    # Whether the issue can be fixed automatically
    confidence: float     # 0.0 - 1.0 (how sure we are this is a real issue)
    fix_suggestion: str   # What to fix (regex pattern or replacement text)
    context: str          # Surrounding text for debugging

    @property
    def category(self) -> str:
        """Compatibility alias for check_name.

        core/qa_checker.py's QAIssue uses 'category', and app.py accesses
        issue.category on both QAIssue and QCIssue objects.  This property
        ensures QCIssue works wherever 'category' is expected.
        """
        return self.check_name


@dataclass
class QCReport:
    """Aggregated quality control report."""

    issues: List[QCIssue] = field(default_factory=list)
    score: float = 100.0  # 0-100

    @property
    def critical_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "critical")

    @property
    def high_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "high")

    @property
    def medium_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "medium")

    @property
    def low_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "low")

    @property
    def auto_fixable_count(self) -> int:
        return sum(1 for i in self.issues if i.auto_fixable)

    def print_summary(self) -> None:
        """Print a concise summary of the QC report."""
        print("=" * 60)
        print("QC ENGINE REPORT")
        print("=" * 60)

        grade = (
            "A" if self.score >= 90 else
            "B" if self.score >= 75 else
            "C" if self.score >= 60 else
            "D" if self.score >= 40 else
            "F"
        )
        print(f"\n  Score: {self.score:.0f}/100 ({grade})")
        print(f"  Total issues: {len(self.issues)}")
        print(f"    CRITICAL: {self.critical_count}")
        print(f"    HIGH:     {self.high_count}")
        print(f"    MEDIUM:   {self.medium_count}")
        print(f"    LOW:      {self.low_count}")
        print(f"  Auto-fixable: {self.auto_fixable_count}")

        if self.issues:
            print("\n  Issues by check:")
            by_check: Dict[str, int] = {}
            for issue in self.issues:
                by_check[issue.check_name] = by_check.get(issue.check_name, 0) + 1
            for check, count in sorted(by_check.items(), key=lambda x: -x[1]):
                print(f"    {check}: {count}")

        if self.issues:
            print("\n  Details:")
            for issue in sorted(self.issues, key=lambda i: (
                {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(i.severity, 4),
                i.line_num,
            )):
                sev_tag = issue.severity.upper()
                fixable = " [auto-fixable]" if issue.auto_fixable else ""
                print(
                    f"    [{sev_tag}] L{issue.line_num}: "
                    f"{issue.message}{fixable}"
                )
                if issue.context:
                    ctx = issue.context[:100]
                    if len(issue.context) > 100:
                        ctx += "..."
                    print(f"           -> {ctx}")

        print("=" * 60)


# print_summary biçiminin okuyucusu (2026-09-25, dalga Q): 2_derle --json
# qc_score / qc_top alanlarını pipeline'ın yazdığı qc_report.txt'ten okur.
# Biçim yukarıdaki print_summary ile BİRLİKTE değişmelidir (test: gidiş-dönüş).
_SUMMARY_SCORE_RE = re.compile(r"^\s*Score:\s*(\d+(?:\.\d+)?)/100\b", re.MULTILINE)
_SUMMARY_BY_CHECK_HEAD = "Issues by check:"
_SUMMARY_BY_CHECK_LINE_RE = re.compile(r"^\s{4}([A-Za-z0-9_]+):\s*(\d+)\s*$")


def parse_qc_report_summary(text: str, top: int = 3) -> Optional[dict]:
    """``QCReport.print_summary`` çıktısından skor ve en çok bulgu veren
    kontrolleri okur.

    Döner: ``{"score": float, "top": [{"check": str, "count": int}, ...]}``
    (``top`` en çok bulgu veren ilk *top* kontrol, print_summary sırasıyla
    — sayıya göre azalan). Skor satırı yoksa ``None``.
    """
    m = _SUMMARY_SCORE_RE.search(text or "")
    if not m:
        return None
    checks: List[dict] = []
    lines = text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines)
                     if ln.strip() == _SUMMARY_BY_CHECK_HEAD)
    except StopIteration:
        start = None
    if start is not None:
        for ln in lines[start + 1:]:
            cm = _SUMMARY_BY_CHECK_LINE_RE.match(ln)
            if not cm:
                break
            checks.append({"check": cm.group(1), "count": int(cm.group(2))})
    checks.sort(key=lambda c: -c["count"])  # kararlı: eşitlikte rapor sırası
    return {"score": float(m.group(1)), "top": checks[:top]}


# ---------------------------------------------------------------------------
# Regex patterns used across checks
# ---------------------------------------------------------------------------

# Math environments: equation, equation*, align, align*, gather, gather*,
# multline, multline*, split, flalign, flalign*, alignat, alignat*
_MATH_ENV_RE = re.compile(
    r"\\begin\{(equation\*?|align\*?|gather\*?|multline\*?|split|"
    r"flalign\*?|alignat\*?)\}",
)

# Display math \[...\]
_DISPLAY_MATH_RE = re.compile(r"\\\[.*?\\\]", re.DOTALL)

# Inline math $...$ (no escaped \$, no display $$)
_INLINE_MATH_RE = re.compile(r"(?<![\$\\])\$(?!\$)(.+?)(?<![\$\\])\$(?!\$)")

# \tag{...} values
_TAG_RE = re.compile(r"\\tag\{([^}]+)\}")

# LaTeX commands (for masking before English detection)
_LATEX_CMD_RE = re.compile(r"\\[a-zA-Z]+(?:\*?\{[^}]*\}|\*?\[[^\]]*\])*")

# Math environment content (for masking)
_MATH_BLOCK_RE = re.compile(
    r"\\begin\{(equation\*?|align\*?|gather\*?|multline\*?|split|"
    r"flalign\*?|alignat\*?|lstlisting|verbatim|minted)\}"
    r".*?"
    r"\\end\{\1\}",
    re.DOTALL,
)

# Display math blocks for masking
_DISPLAY_BLOCK_RE = re.compile(r"\\\[.*?\\\]", re.DOTALL)

# Inline math for masking
_INLINE_BLOCK_RE = re.compile(
    r"(?<![\$\\])\$(?!\$).+?(?<![\$\\])\$(?!\$)"
)

# Reference commands: \ref{}, \cite{}, \label{}, \eqref{}, \pageref{},
# \cref{}, \autoref{}, \href{}{}
_REF_CMD_RE = re.compile(
    r"\\(?:ref|cite|label|eqref|pageref|cref|autoref)\s*\{([^}]*)\}"
)

# Başlık komutuyla başlayan satır (çevrilmemiş başlıklar Check 11'in işi;
# Check 2'nin kaynak-dil taraması bunları çift raporlamaz).
_HEADING_CMD_LINE_RE = re.compile(
    r"\\(?:part|chapter|section|subsection|subsubsection|paragraph)\*?\s*[\[{]"
)

# \includegraphics
_INCLUDEGRAPHICS_RE = re.compile(
    r"\\includegraphics\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}"
)

# Kullanıcı tanımlı ortam/teorem adları (F2, 2026-07-28).
# sanitizer._fix_translated_env_names A-9 guard'ı ile AYNI desen —
# davranış paritesi bozulmasın diye burada tek yerde tanımlı; autofix de
# buradan import eder.
_DEFINED_ENV_RE = re.compile(
    r"\\(?:newenvironment|renewenvironment|newtheorem\*?)\s*\{([^}]+)\}"
)


# LaTeX-kodlu Türkçe harfler (\c{s}, {\i}, \u{g}, \"{o}, \.{I}) -> UTF-8.
# Check 13 (latex_encoded_turkish) bunları ayrıca raporlar; dil kararı
# veren kontroller ise ÇÖZÜLMÜŞ metne bakmalı: "Duyarl{\i}l{\i}k Analizi"
# Türkçedir (2026-09-24, başlık regex'i parantez-dengeli olunca görüldü).
_LATEX_TR_CHAR_MAP: Dict[str, str] = {
    r"\c{S}": "Ş", r"\c{s}": "ş", r"\c{C}": "Ç", r"\c{c}": "ç",
    r'\"{O}': "Ö", r'\"{o}': "ö", r'\"{U}': "Ü", r'\"{u}': "ü",
    r"\u{G}": "Ğ", r"\u{g}": "ğ", r"\.{I}": "İ", r"{\i}": "ı",
}
_LATEX_TR_CHAR_RE = re.compile(
    "|".join(re.escape(k) for k in _LATEX_TR_CHAR_MAP)
    + r"|\\i(?![a-zA-Z])"
)


def _decode_latex_turkish(text: str) -> str:
    """LaTeX-kodlu Türkçe harfleri UTF-8'e çevir (yalnız dil analizi için)."""
    return _LATEX_TR_CHAR_RE.sub(
        lambda m: _LATEX_TR_CHAR_MAP.get(m.group(0), "ı"), text)


# _count_inline_math belirteçleri: kaçışlı karakter, "$$", "$", satır sonu.
_MATH_DOLLAR_TOKEN_RE = re.compile(r"\\.|\$\$|\$|\n", re.DOTALL)


def _read_brace_group(text: str, pos: int) -> Optional[Tuple[str, int]]:
    """``text[pos:]`` (baştaki boşluk atlanır) ``{...}`` ile başlıyorsa
    PARANTEZ-DENGELİ içeriği ve kapanıştan sonraki konumu döndürür."""
    n = len(text)
    while pos < n and text[pos].isspace():
        pos += 1
    if pos >= n or text[pos] != "{":
        return None
    depth = 0
    j = pos
    while j < n:
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[pos + 1:j], j + 1
        j += 1
    return None


# ---------------------------------------------------------------------------
# Makine belirteci / tanımlayıcı (kulvar F2, 2026-09-30)
# ---------------------------------------------------------------------------
# Belirteç dizgileri (ajan mesajı, zaman damgası etiketi, kod tanımlayıcısı:
# "qqSAYAC_YENI/BASLA_T1700000000_U12345") doğal dil değildir:
# çevrilmez, cümle de değildir. Makine dizgisi alıntılayan bir raporda başlık
# denetimi ve "uzun noktalamasız cümle" denetimi bunları dil metni sanıyordu.

# Sıfır genişlikli satır kırma komutları: dönüştürücü uzun belirtecin
# içine ekler ("SINY\allowbreak{}AL"); kelime sınırı DEĞİLDİR.
_ZERO_WIDTH_BREAK_RE = re.compile(
    r"\\(?:allowbreak|linebreak|nolinebreak|hspace\*?\s*\{0pt\})"
    r"(?![A-Za-z])(?:\s*\{\})?|\\-"
)
_ESCAPED_SPECIAL_RE = re.compile(r"\\([_&%#$])")
_BARE_CMD_NAME_RE = re.compile(r"\\[A-Za-z@]+\*?")
# Doğal dil kelimesi: yalnız harflerden oluşan (kesme/tire ile bağlı
# parçalar olabilir) belirteç. Rakam, altçizgi, eğik çizgi içeren belirteç
# tanımlayıcıdır.
_NATURAL_WORD_RE = re.compile(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*")
_TOKEN_EDGE_PUNCT = "\"'“”‘’«»()[]<>.,;:!?"
# İçeriği kod/belirteç olan komutlar (tek bağımsız değişken).
_CODE_ARG_CMD_RE = re.compile(r"\\(?:texttt|url|path|nolinkurl)(?![A-Za-z])")
# Kod/belirteç yerine konan yer tutucu (tek kelime sayılır, noktalama yok).
MACHINE_TOKEN_PLACEHOLDER = "KOD"
# Altçizgi içeren tanımlayıcı dizisi (harf/rakam ve düz ya da kaçışlı
# altçizgi): "zZR_ARVO36861_KAM1196", "part0025\_split\_001". Yalnız dizinin
# kendisi değişir; çevresindeki noktalama (cümle sonu) korunur.
_UNDERSCORE_IDENT_RE = re.compile(
    r"(?<![\w\\])(?:[^\W_]|\\?_)*\\?_(?:[^\W_]|\\?_)*")


def _unlatex_tokens(text: str) -> str:
    """Belirteç sınaması için sade metin: sıfır genişlikli kırmalar ve
    komut adları silinir (bağımsız değişkenler kalır), kaçışlı özel
    karakterler (``\\_``) çözülür, süslü ayraçlar atılır."""
    t = _ZERO_WIDTH_BREAK_RE.sub("", text)
    t = _ESCAPED_SPECIAL_RE.sub(r"\1", t)
    t = _BARE_CMD_NAME_RE.sub("", t)
    return t.replace("{", "").replace("}", "")


def natural_language_words(text: str) -> List[str]:
    """LaTeX metnindeki doğal dil kelimeleri: boşlukla ayrılmış ve
    kenar noktalaması atılınca yalnız harften oluşan belirteçler.

    Boş liste = metin yalnız makine belirteci/tanımlayıcı, sayı ya da
    işaretten oluşuyor (çevrilecek dil yok).
    """
    out: List[str] = []
    for tok in _unlatex_tokens(text).split():
        tok = tok.strip(_TOKEN_EDGE_PUNCT)
        if tok and _NATURAL_WORD_RE.fullmatch(tok):
            out.append(tok)
    return out


def mask_machine_tokens(text: str) -> str:
    """Kod/belirteç parçalarını tek kelimelik yer tutucuyla değiştir.

    * ``\\texttt{...}``, ``\\url{...}``, ``\\path{...}`` bağımsız
      değişkenleri (parantez dengeli; satırda kapanmıyorsa satır sonuna
      kadar);
    * sıfır genişlikli kırmalar silindikten sonra altçizgi içeren
      tanımlayıcı dizileri ("zZR_ARVO36861_KAM1196"); çevresindeki
      noktalama korunur ("roles.\\href{part0025_split_001.html}" cümle
      sonunu kaybetmez).
    Uzunluk/cümle ölçen denetimler (Check 34b) içindir; satır sayısı
    korunur (girdi tek satırdır).
    """
    out: List[str] = []
    pos = 0
    n = len(text)
    for m in _CODE_ARG_CMD_RE.finditer(text):
        if m.start() < pos:
            continue
        group = _read_brace_group(text, m.end())
        end = group[1] if group is not None else n
        out.append(text[pos:m.start()])
        out.append(" " + MACHINE_TOKEN_PLACEHOLDER + " ")
        pos = end
    out.append(text[pos:])
    t = _ZERO_WIDTH_BREAK_RE.sub("", "".join(out))
    return _UNDERSCORE_IDENT_RE.sub(MACHINE_TOKEN_PLACEHOLDER, t)


def _skip_bracket_group(text: str, pos: int) -> int:
    """``[...]`` isteğe bağlı argümanını atla (yoksa konum değişmez)."""
    j = pos
    while j < len(text) and text[j].isspace():
        j += 1
    if j < len(text) and text[j] == "[":
        close = text.find("]", j)
        if close != -1:
            return close + 1
    return pos


def _read_tabular_spec(line: str, pos: int, env_name: str) -> Optional[str]:
    r"""``\begin{<env>}`` sonrasından sütun spec'ini oku (parantez-dengeli).

    tabular*/tabularx önce genişlik argümanı alır; konum argümanı
    ``[t]`` her ortamda atlanır. Spec bu satırda yoksa None.
    """
    pos = _skip_bracket_group(line, pos)
    if env_name in ("tabular*", "tabularx"):
        width = _read_brace_group(line, pos)
        if width is None:
            return None
        pos = _skip_bracket_group(line, width[1])
    group = _read_brace_group(line, pos)
    return group[0] if group else None


def _count_tabular_spec_columns(spec: str) -> int:
    r"""Tabular sütun spec'indeki GERÇEK sütun sayısı.

    Sütun türleri (l c r X S, p/m/b{genişlik}, \newcolumntype harfleri) birer
    sütundur; ``>{..}`` ``<{..}`` ``@{..}`` ``!{..}`` ve ``|`` sütun değildir,
    ``*{n}{spec}`` n kez tekrarlanır. Argümanların İÇİNDEKİ harfler
    (``\raggedright``, ``70mm``) sayılmaz.
    """
    count = 0
    i = 0
    n = len(spec)
    while i < n:
        c = spec[i]
        if c in "><@!":
            group = _read_brace_group(spec, i + 1)
            i = group[1] if group else i + 1
        elif c == "*":
            reps = _read_brace_group(spec, i + 1)
            body = _read_brace_group(spec, reps[1]) if reps else None
            if reps and body and reps[0].strip().isdigit():
                count += int(reps[0].strip()) * _count_tabular_spec_columns(
                    body[0])
                i = body[1]
            else:
                i += 1
        elif c == "\\":
            # \vline gibi komut: adı atla
            j = i + 1
            while j < n and spec[j].isalpha():
                j += 1
            i = max(j, i + 2)
        elif c == "{":
            group = _read_brace_group(spec, i)
            i = group[1] if group else i + 1
        elif c.isascii() and c.isalpha():
            count += 1
            i = _skip_bracket_group(spec, i + 1)
            # p{..}/m{..}/b{..} ve özel sütun türlerinin argümanları
            group = _read_brace_group(spec, i)
            while group is not None:
                i = group[1]
                group = _read_brace_group(spec, i)
        else:
            i += 1
    return count


def collect_defined_env_names(text: str) -> frozenset:
    r"""Metinde \newenvironment/\renewenvironment/\newtheorem ile
    tanımlanan ortam adlarını döndürür.

    Çağıran, preamble_ozel.tex gibi gövde dışı kaynakları da tarayıp
    sonucu QCEngine(user_defined_envs=...) / AutoFixer(user_defined_envs=...)
    ile geçirebilir.
    """
    if not text:
        return frozenset()
    return frozenset(_DEFINED_ENV_RE.findall(text))

# Tabular environment boundaries (for skipping tables in untranslated check)
_TABULAR_BEGIN_RE = re.compile(r"\\begin\{(tabular|tabularx|longtable)\}")
_TABULAR_END_RE = re.compile(r"\\end\{(tabular|tabularx|longtable)\}")

# Proper noun heuristic: capitalized multi-word sequences (2+ words)
# Matches things like "Dr. John Smith", "Fourier Transform", "Reynolds Number"
_PROPER_NOUN_RE = re.compile(
    r"(?:(?:Dr|Prof|Mr|Mrs|Ms)\.\s+)?"  # optional title
    r"(?:[A-Z][a-z]+(?:\s+(?:of|the|and|in|for|de|von|van|al)\s+)?){2,}"
)

# <<LATEX_N>> placeholder
_LATEX_PLACEHOLDER_RE = re.compile(r"<<LATEX_\d+>>")

# [[UNIT_N]], [[BIRIM_N]], [[BİRİM_N]]
# 2026-07-11: dar UNIT/BIRIM listesi generic'e cevrildi. Eski hali
# [[REF_N]] ve [[PGREF_N]]'i (core/translator'in gercek formatlari) hic
# yakalamiyordu; [Ii] siniflari Turkce 'İ' (U+0130) ile de eslesmiyordu
# ([[BİRİM_0]] QC'den 100 puanla geciyordu). Unicode harf sinifi + _N
# kuyrugu tum pipeline placeholder ailesini kapsar.
_UNIT_PLACEHOLDER_RE = re.compile(
    r"\[\[[^\W\d]{1,24}_\d+\]\]"
)

# [[FORMULA_N]] — generic pattern kapsiyor; ayri check'te cifte rapor
# olmamasi icin placeholder_checks listesinden cikarildi (asagida).
_FORMULA_PLACEHOLDER_RE = re.compile(r"\[\[FORMULA_\d+\]\]")

# @...@@ proper noun markers
_PROPER_NOUN_MARKER_RE = re.compile(r"@[^@]+@@")

# ASCII-corruption signature in a Turkish glossary value: an uppercase ASCII
# 'I' sitting mid-word after a lowercase letter (e.g. "isinIm" for "ışınım",
# "kararlIlik" for "kararlılık").  Legitimate Turkish never capitalises 'I'
# in the middle of a lowercase word, so this reliably marks corrupt data.
_MIDWORD_CAP_I_RE = re.compile(r"[a-zçğıöşü]I")


# ---------------------------------------------------------------------------
# Case-insensitive katlama (F27/F38 ön-elemeleri)
# ---------------------------------------------------------------------------

# ``str.lower()`` tek başına ``re.IGNORECASE``'i karşılamaz:
#   * 'İ'.lower() -> 'i' + U+0307 (iki karakter),
#   * Python'un re motoru {I, i, ı, İ} ve {s, ſ} kümelerini eşdeğer sayar.
# Ön-elemeler regex kararından DAHA GENİŞ olmak zorunda (daralırsa bulgu
# sessizce kaçar), bu yüzden katlama bu sınıfları tek temsilciye indirir.
# tests/test_fixav20260729_qcaf.py denkliği kalıp kümesi üzerinde kilitler.
_CI_FOLD_MAP = str.maketrans({
    "İ": "i", "I": "i", "ı": "i", "ſ": "s", "̇": "",
})


def _fold_ci(text: str) -> str:
    """Case-insensitive karşılaştırma için metni katla.

    R6 (2026-07-29): bu katlama yalnız LATİN + TÜRKÇE kalıplar için
    re.IGNORECASE'in üst kümesidir. IGNORECASE'in eşdeğer saydığı bazı
    Yunan/sembol çiftleri (mikro/mu, final sigma vb.) burada ayrık kalır —
    yeni bir desen bu karakterleri içerirse tabloyu genişletmek gerekir
    (denklik testi tests/test_fixav20260729_qcaf.py'de kilitli).
    """
    return text.lower().translate(_CI_FOLD_MAP)


# ---------------------------------------------------------------------------
# MT tekrar-döngüsü ön-elemesi (F38)
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"\w+")

# ``_REPETITION_LOOP_RE`` / ``_REPETITION_FIX_RE`` eşiği: 5+ kelimelik fraz,
# 3+ ardışık kopya (fraz + 2 tekrar).
_REPETITION_MIN_PHRASE = 5
_REPETITION_MIN_COPIES = 3
# Aday çifti bütçesi: patolojik satırda ön-eleme kendisi pahalıya gelmesin;
# bütçe biterse "olabilir" denir ve kararı regex verir (eski davranış).
_REPETITION_PREFILTER_BUDGET = 4000


def may_have_repetition_loop(text: str) -> bool:
    """5+ kelimelik bir frazın 3+ ardışık tekrarı OLABİLİR mi? (O(W))

    ``(\\b\\w+(?:\\s+\\w+){4,}?)(?:\\s+\\1){2,}`` geri-referanslı tembel
    regex'i satır uzunluğunda süperlineerdir (W=400 kelimede ~20 ms; kitap
    gövdesinde geçiş başına saniyeler). Bu fonksiyon aynı kararın gevşek
    (üst-küme) hâlini kelime tabanlı, doğrusal maliyetle verir: ``False``
    dönerse regex'in eşleşmesi İMKÂNSIZDIR, ``True`` dönerse kararı yine
    regex verir. Böylece bulgu kümesi birebir korunur.

    Üst-küme olma gerekçesi: regex eşleşmesindeki fraz saf ``\\w+`` token
    dizisidir, dolayısıyla ``\\w+`` tokenlarında da i, i+L, i+2L konumlarında
    aynı 5-gram tekrar eder. Karşılaştırma ``_fold_ci`` ile yapılır (regex'in
    IGNORECASE denkliğinden daha geniş).
    """
    n_min = _REPETITION_MIN_PHRASE * _REPETITION_MIN_COPIES
    # H2 (2026-07-29): 15 tokenin asgari uzunluğu 15 harf + 14 ayraç = 29
    # karakterdir; '< 2*n_min' (30) tam 29 karakterlik gerçek döngüyü
    # kaçırıyordu ('a b c d e' x3). Üst-küme garantisi için -1.
    if len(text) < 2 * n_min - 1:
        return False
    words = _WORD_RE.findall(_fold_ci(text))
    n = len(words)
    if n < n_min:
        return False
    # 3 kopya => en az 2*L (>= 10) tekrarlı token bulunmak zorunda.
    if n - len(set(words)) < 2 * _REPETITION_MIN_PHRASE:
        return False

    k = _REPETITION_MIN_PHRASE
    positions: Dict[Tuple[str, ...], List[int]] = {}
    for i in range(n - k + 1):
        positions.setdefault(tuple(words[i:i + k]), []).append(i)

    budget = _REPETITION_PREFILTER_BUDGET
    for ps in positions.values():
        if len(ps) < _REPETITION_MIN_COPIES:
            continue
        pset = set(ps)
        for a_idx, start in enumerate(ps):
            for nxt in ps[a_idx + 1:]:
                period = nxt - start
                if period < k:
                    continue
                budget -= 1
                if budget < 0:
                    return True  # pahalı satır: kararı regex versin
                if start + 2 * period in pset:
                    return True
    return False


# ---------------------------------------------------------------------------
# Turkish character set for language detection
# ---------------------------------------------------------------------------

# Characters unique to Turkish that don't appear in English
_TURKISH_CHARS = set("şğıöüçŞĞİÖÜÇ")

# Check 19 (foreign_language_leakage) için: Türkçede OLMAYAN Latin aksanları
# ASCII'ye katlanır (küçük harf metne uygulanır). Türkçe harfler (ö, ü, ç,
# ş, ğ, ı) ve Türkçede de kullanılan şapkalılar (â, î, û) dışarıda kalır.
_FOREIGN_ACCENT_FOLD = str.maketrans({
    "á": "a", "à": "a", "ã": "a", "ä": "a", "å": "a",
    "é": "e", "è": "e", "ê": "e", "ë": "e",
    "í": "i", "ì": "i", "ï": "i",
    "ó": "o", "ò": "o", "õ": "o", "ô": "o",
    "ú": "u", "ù": "u",
    "ñ": "n",
})

# Common Turkish words (ASCII-only forms) that indicate translated text
# even when Turkish-specific characters are missing or LaTeX-encoded.
# R2 (2026-07-29): broken_command_braces için "daha uzun meşru komut"
# ayrımı. F1 fix'i küçük harfle devam eden HER eşleşmeyi meşru sayıyordu;
# bu \footnotesize'ı kurtardı ama \textbfdeger gibi tamamı-küçük yapışık
# kırıkları da kör etti. Doğru kural: devam eden ASCII harflerle oluşan TAM
# ad bu kümede gerçekten varsa komut meşrudur, yoksa kırıktır. Küme,
# _BRACE_REQUIRED_CMDS/_BRACE_FIX_CMDS öneklerini uzatan gerçek LaTeX
# komutlarını kapsar; autofix aynı kümeyi import eder (kopya yok).
KNOWN_LONGER_COMMANDS = frozenset({
    # \footnote*
    "footnotesize", "footnotemark", "footnotetext", "footnoterule",
    # \caption*
    "captionof", "captionsetup", "captionabove", "captionbelow",
    # \chapter* / \section* aile işaretleri
    "chaptermark", "chaptername", "sectionmark",
    "subsectionmark", "subsubsectionmark",
    # \text* (autofix'in 'text' önekini uzatan gerçek komutlar)
    "textbf", "textit", "texttt", "textsc", "textsf", "textrm", "textmd",
    "textup", "textsl", "textnormal", "textwidth", "textcolor",
    "textsuperscript", "textsubscript", "textbackslash", "textbullet",
    "textemdash", "textendash", "texttrademark", "textregistered",
    "textdegree", "textcelsius", "textasciitilde", "textasciicircum",
    "textvisiblespace", "textellipsis", "textgreater", "textless",
    "textbar", "textunderscore", "textquotedblleft", "textquotedblright",
    # 2026-09-25 (kulvar Q): sayfa/kayan nesne parametreleri ve textcomp
    # simgeleri. Autofix'in '\text' onarımı bunları tanımadığı için
    # '\textheight' -> '\text{height}' yapıyordu (sanitizer G1'in görsel
    # yüksekliği sınırı '0.7\textheight' kullanır; derleme bozulur).
    "textheight", "textfraction", "textfloatsep", "textstyle",
    "textquoteleft", "textquoteright", "textquotesingle", "textcircled",
    "textperiodcentered", "textdagger", "textdaggerdbl", "textsection",
    "textparagraph", "textcopyright", "texttimes", "textdiv", "textpm",
    "textmu", "textminus", "textonehalf", "textperthousand",
    "textexclamdown", "textquestiondown",
    # \footnote* uzunluk parametresi
    "footnotesep",
    # \fbox*
    "fboxsep", "fboxrule",
})

# Kırık komut adayında komut adını uzatan ASCII harf kuyruğu.
_CMD_TAIL_RE = re.compile(r"[A-Za-z]+")


_TURKISH_COMMON_WORDS = {
    # Ultra-common function words
    "bir", "ve", "bu", "ile", "olan", "olarak", "gibi", "daha",
    "sonra", "ancak", "ama", "hem", "ya", "ne", "ki", "da",
    "de", "mi", "mu", "dir", "dır", "tir", "tur", "ler", "lar",
    # Common verbs/forms without special chars
    "icin", "uzerinde", "uzerine", "olarak", "yapilir", "edilir",
    "kullanilir", "verilir", "alinir", "belirlenir", "tanimlanir",
    "gosterilir", "hesaplanir", "bulunur", "yazilir", "calisir",
    "baslatilir", "durdurulur", "gonderilir", "olusturulur",
    # Common nouns without special chars
    "sinif", "katman", "fonksiyon", "denklem", "tanim", "teorem",
    "boyut", "deger", "ornek", "sekil", "tablo", "bolum", "algoritma",
    "matris", "vektor", "uzay", "cozum", "sonuc", "veri", "model",
    # Common adjectives/adverbs
    "buyuk", "kucuk", "yeni", "eski", "ilk", "son", "her", "tum",
    "ayni", "baska", "diger", "asagidaki", "yukaridaki",
    # Turkish sentence patterns (LaTeX-encoded forms)
    "olarak", "durumunda", "sayesinde", "araciligiyla",
    "bakimdan", "dolayi", "nedeniyle", "acidan",
}


# ---------------------------------------------------------------------------
# Kaynak bağlamı (2026-09-25, kulvar Q): yanlış alarm muafiyetleri
# ---------------------------------------------------------------------------
# Yol A'da kaynak altı dilden biri olabilir (en/de/ru/fr/es/it). Birkaç
# denetim, kaynakta AYNEN geçen içeriği (özel ad, künye, özgün dilinde
# bırakılmış alıntı) çeviri hatası sanıyordu. Kaynaktan bir kez çıkarılan
# bağlam (baskın dil, harf dizileri, normalleştirilmiş kelime dizisi,
# birinci tekil şahıs zamiri yoğunluğu) bu muafiyetlerin tek kaynağıdır.

# Harf dizisi (rakam ve altçizgi ayırıcı): "Partsch", "MacClellan", "xhtml".
_LETTER_RUN_RE = re.compile(r"[^\W\d_]+")

# Kaynak dil tespiti bu güvenin altındaysa dil "bilinmiyor" sayılır ve dile
# bağlı muafiyetler uygulanmaz (eski davranış).
SOURCE_LANG_MIN_CONFIDENCE = 0.5

# Özgün dilinde bırakılmış alıntı: çeviri satırının kelimelerinin en az bu
# payı, kaynakta en az SOURCE_QUOTE_MIN_RUN kelimelik ARDIŞIK (birebir)
# dizilerle örtülmeli.
SOURCE_QUOTE_MIN_COVERAGE = 0.6
SOURCE_QUOTE_MIN_RUN = 3
# Satırın dili, kaynağın dilini işlev kelimelerinde en az bu kat geçmeli
# (akraba dillerde satır sınıflandırıcısının eşitlik hatasına karşı).
SOURCE_QUOTE_LANG_DOMINANCE = 2

# Kaynak birinci tekil şahısla yazılmışsa (yazar sesi, roman diyaloğu)
# çevirideki birinci şahıs sadakattir. Eşik: en az bu kadar zamir VE 1000
# kelimede en az bu yoğunluk. Ölçüm (2026-09-25): İspanyolca deneme 6/5531
# (1,1‰), Fransızca roman 31/5915 (5,2‰), İtalyanca deneme 3/5000 (0,6‰);
# iki İngilizce bilim kitabı 24/188370 (0,13‰) ve 6/71742 (0,08‰) — ikincisi
# alıntılardaki rastlantısal zamirdir, uyarı sürer.
FIRST_PERSON_SOURCE_MIN = 3
FIRST_PERSON_SOURCE_PER_1000 = 0.5

# Kaynak dilde birinci tekil şahıs zamirleri (küçük harf; clean +
# tokenize_words sonrası). İngilizce "I" sayılmaz: Roma rakamı/değişken
# ("Part I", "Type I") ile karışır. Fransızca "j'" elizyonu "j" kalır.
_SOURCE_FIRST_PERSON_WORDS: Dict[str, frozenset] = {
    "en": frozenset({"my", "me", "myself", "mine"}),
    "de": frozenset({"ich", "mich", "mir", "mein", "meine", "meinen",
                     "meinem", "meiner", "meines"}),
    "fr": frozenset({"je", "j", "moi", "me", "mon", "ma", "mes"}),
    "es": frozenset({"yo", "me", "mí", "mi", "mis", "conmigo"}),
    "it": frozenset({"io", "me", "mi", "mio", "mia", "miei", "mie"}),
    "ru": frozenset({"я", "меня", "мне", "мной", "мой", "моя", "моё",
                     "мое", "мои", "моего", "моей", "моим", "моих",
                     "моему", "мою"}),
}

# Latince "a ..." kalıpları İngilizce artikel değildir ("a priori").
_LATIN_A_LOCUTIONS = frozenset({
    "priori", "posteriori", "fortiori", "contrario", "minore", "maiore",
    "majore", "minori", "maiori", "majori",
})

# ---------------------------------------------------------------------------
# Kaynakta aynen geçen Latince alıntı (kulvar F2, 2026-09-30; MT-1)
# ---------------------------------------------------------------------------
# Kelime kümeleri ve eşik 2026-10-05'te ``core.lang_detect``'e taşındı
# (bütün dil eşiklerinin tek kaynağı; yerel model zinciri de oradan okur).
# Aşağıdaki ``latin_quote_hits`` / ``is_latin_text`` oraya iletir.

# Check 33e (tekrarlayan fraz) kaynak karşılaştırması (kulvar F2,
# 2026-09-30): "tekrar kaynakta da varsa bulgu değil". Kaynaktaki tekrarın
# ÇEVİRİDEKİ tekrarla aynı şey olduğu şöyle sınanır:
#   1. Hizalama: çeviri paragrafının satır aralığı kaynağa satır oranıyla
#      eşlenir (± pencere: REPEAT_SOURCE_WINDOW_LINES ya da kaynak satır
#      sayısının REPEAT_SOURCE_WINDOW_SHARE payı, hangisi büyükse);
#      penceredeki kaynak paragraflarından çeviriyle en çok DEĞİŞMEZ
#      belirteci (iki+ haneli ya da ondalıklı sayı, ASCII büyük harfli ad,
#      "#page-…" hedefi) paylaşan seçilir. Eşleşme en az
#      REPEAT_SOURCE_MIN_ANCHORS ortak belirteç VE Dice benzerliği en az
#      REPEAT_SOURCE_MIN_ANCHOR_DICE olmalı; yoksa hizalama yok sayılır.
#      Ölçüm (derlem): doğru eşleşmeler 0,59–0,97; yanlışlar 0,11–0,38
#      (bir test kitabında doğru paragraf 115 satır öteydi; tek haneli
#      sayılarla yanlış paragrafa 0,38).
#   2. Tekrar boyu: çevirideki frazın tüm geçişlerinde ortak kalan en uzun
#      dizi (ör. "değerleri tek tek ölçtü" = 4 kelime) ile hizalanan kaynak
#      paragrafında en az aynı sayıda tekrar eden içerikli en uzun dizi
#      ("the value was measured" = 4) karşılaştırılır: kaynaktaki boy en az
#      max(REPEAT_SOURCE_NGRAM, ⌈tr_boy × REPEAT_SOURCE_SPAN_RATIO⌉)
#      olmalı. Makine çevirisinin bir alıştırmayı üç kez yazması (bir test
#      kitabında) kaynaktaki kısa bir kalıpla ("is cooled down" x3) muaf TUTULMAZ.
# Koşullardan biri sağlanmazsa bulgu eskisi gibi raporlanır (güvenli yön).
REPEAT_SOURCE_WINDOW_LINES = 30
REPEAT_SOURCE_WINDOW_SHARE = 0.03
REPEAT_SOURCE_NGRAM = 3
REPEAT_SOURCE_SPAN_RATIO = 0.75
# İçerikli dizi: en az bir kelimesi işlev kelimesi değil ve en az bu kadar
# harfli ("one of the", "in order to" gibi salt kalıplar sayılmaz).
REPEAT_SOURCE_CONTENT_MIN_CHARS = 4
REPEAT_SOURCE_MIN_ANCHORS = 3
REPEAT_SOURCE_MIN_ANCHOR_DICE = 0.5
# Tek haneli sayılar (1, 2, 3) her paragrafta geçer, ayırt edici değildir.
_REPEAT_ANCHOR_RE = re.compile(
    r"#page-[\w-]+|\d+(?:[.,]\d+)+|\d{2,}|(?<!\w)[A-Z][A-Za-z]{2,}(?!\w)")


@functools.lru_cache(maxsize=1)
def _all_function_words() -> frozenset:
    """Profil dillerinin (en/tr/de/fr/es/it/pt/ru) ham sık kelime
    listelerinin birleşimi (``lang_detect.frequent_words``; tek kaynak)."""
    ld = _lang_detect()
    out: Set[str] = set()
    for lang in ld.LATIN_PROFILE_LANGS + ("ru",):
        out |= ld.frequent_words(lang)
    return frozenset(out)


def latin_quote_hits(words: Iterable[str]) -> Tuple[int, int]:
    """(Latince kanıt kelimesi sayısı, en güçlü rakip profil dilinin işlev
    kelimesi sayısı) — ``core.lang_detect.latin_quote_hits``'e iletir."""
    return _lang_detect().latin_quote_hits(words)


def is_latin_text(words: Iterable[str]) -> bool:
    """Kelime dizisi Latince mi? ``core.lang_detect.is_latin_text``'e
    ``SOURCE_QUOTE_LANG_DOMINANCE`` baskınlık katıyla iletir."""
    return _lang_detect().is_latin_text(
        words, dominance=SOURCE_QUOTE_LANG_DOMINANCE)


def _same_language(a: Optional[str], b: Optional[str]) -> bool:
    """İki dil kodu aynı dil mi? Kiril alfabesi düzeyindeki "cyrillic"
    kodu Rusça ("ru") ile aynı sayılır (satır sınıflandırıcısı kısa
    satırda dili ayırt edemeyip alfabe kodunu dönebiliyor)."""
    def _norm(code: Optional[str]) -> Optional[str]:
        return "ru" if code == "cyrillic" else code
    return _norm(a) == _norm(b)


# ---------------------------------------------------------------------------
# Kulvar G4 (2026-09-30): okuma ajanlarının bulduğu, QC'nin görmediği hatalar
# ---------------------------------------------------------------------------
# Kanıt: iç okuma bulguları ve G4 düzeltme raporu (iç denetim notu).
# Yeni denetimler:
#   fragmented_line          parçalı yol çıktısı (MT-1)
#   number_corruption        sayı/karşılaştırma bozulması (MT-7)
#   caption_structure        altyazı sayısı, boş/etiket-yalnız altyazı,
#                            altyazıya yapışmış gövde, float içi başıboş metin
#                            (MT-17, gpt-oss altyazı kaybı)
#   footnote_count           \footnote sayı farkı
#   source_language_sentence Türkçe satırda kaynak dilde cümle (MT-9)
#   footnote_def_in_body, footnote_body_merge, plain_footnote_marker,
#   page_number_leak, visible_html_tag, markdown_remnant,
#   unescaped_ampersand      boru hattı artıkları (MT-16/18)
#   placeholder_orphans      (mevcut ad) açılı ayraçları düşmüş "LATEX_N"
# Hepsi rapor-only (auto_fixable=False): AutoFixer'da karşılıkları yok.
# Boru hattı artıkları kaynakta da olsa raporlanır: PDF'teki kusur gerçektir
# (Marker/dönüştürücü kökenli olduğu iletide belirtilir).

# Satır bazlı yeni denetimlerde bir denetimin en çok bu kadar satırı ayrı
# bulgu olur; kalanı tek özet bulguda sayılır (rapor boğulmasın).
G4_LINE_ISSUE_CAP = 20

# --- Parçalı yol (MT-1) ---
# Yer tutucu yutulunca satır, yer tutucu sınırlarından parçalanıp her parça
# ayrı çevriliyor (translator: "eski parçalı yol"). Model her parçayı ayrı
# cümle sanıp büyük harfle başlatıyor: "$m_1$ ve $m_2$ Sırasıyla." Denetim
# satır içi çapaları (``$…$``/``\(…\)``, kısa ``\textit{…}`` = Marker'ın italik
# değişkeni, noktalı denklem atfı "(1.1)") kaynaktaki aynı çapalarla hizalar.
# Çevirideki çapanın hemen ardından büyük harfli sıradan bir kelime gelirken
# kaynakta aynı çapanın ardından küçük harf ya da noktalama geliyorsa bu bir
# belirtidir. Özel adlar sayılmaz: kaynakta aynen geçen kelime, kesme işaretli
# ek alan kelime, belgede cümle ortasında büyük harfle geçip hiç küçük harfle
# geçmeyen kelime, etiket kelimeleri (Şekil, Denklem…).
FRAGMENT_ITALIC_ANCHOR_MAX_CHARS = 12
# Tek başına yeterli belirti sayısı; daha azı ancak ek belirtiyle (satır başı
# boşluk, "…" artığı, satır sonunda çevrilmemiş işlev kelimesi, kaynağa göre
# ayraç dengesizliği) bulgu olur.
FRAGMENT_MIN_CAPS = 2
# Belirti ağırlığı bu değere ulaşan satır HIGH, altı MEDIUM (bkz.
# _check_fragmented_lines).
FRAGMENT_HIGH_WEIGHT = 3
_FRAGMENT_PAREN_REF_RE = re.compile(r"\(\s*\d+\.\d+(?:\.\d+)*\s*\)")
_FRAGMENT_ITALIC_RE = re.compile(
    r"\\(?:textit|emph)\{([^{}]{1,%d})\}" % FRAGMENT_ITALIC_ANCHOR_MAX_CHARS)
_FRAGMENT_PAREN_MATH_RE = re.compile(r"\\\((.+?)\\\)")
_FRAGMENT_FOLLOW_RE = re.compile(r"[ \t]*(?:([^\W\d_]+)|(\S))?")
_WS_RE = re.compile(r"\s+")
_TR_UPPER = "A-ZÇĞİÖŞÜÂÎÛ"
_TR_LOWER = "a-zçğıöşüâîû"
# Cümle ortasında (küçük harfli kelimeden sonra) büyük harfle geçen kelime.
_FRAGMENT_MIDCAP_RE = re.compile(
    rf"(?<=[{_TR_LOWER}]) ([{_TR_UPPER}][{_TR_LOWER}]+)(?![{_TR_LOWER}'’])")
_FRAGMENT_LOWER_WORD_RE = re.compile(
    rf"(?<![{_TR_UPPER}{_TR_LOWER}])[{_TR_LOWER}]{{2,}}")
_FRAGMENT_LABEL_WORDS = frozenset({
    "Şekil", "Şek", "Denklem", "Denk", "Tablo", "Bölüm", "Kısım", "Ek",
    "Örnek", "Teorem", "Tanım", "Önerme", "Lemma", "Sonuç", "Alıştırma",
    "Problem", "Soru", "Not", "Uyarı", "Eşitlik", "Formül", "Madde",
    "Resim", "Grafik", "Algoritma", "Kanıt", "İspat", "Çözüm",
})
_ELLIPSIS_RE = re.compile(r"…|\.\.\.|\\l?dots(?![A-Za-z])")
# "…’nin konumu", "…ye göre": üç nokta bir adın yerine geçip ek almış.
_ELLIPSIS_SUFFIX_RE = re.compile(
    rf"…[ \t]?['’]?(?:[yn]?[ıiuü]n|[yn]?[ae]|d[ae]n|t[ae]n|d[ae]|t[ae]"
    rf"|y?[ıiuü])(?![{_TR_LOWER}])")
# Satır sonunda çevrilmeden kalmış kısa kaynak dili işlev kelimesi ("$m_1$
# is"). Türkçe sözcüklerle çakışanlar (de, da, en, o, a, e) listede yok.
_FRAGMENT_TAIL_WORDS: Dict[str, Tuple[str, ...]] = {
    "en": ("is", "are", "at", "where", "the", "of", "and", "was", "were",
           "to", "by", "with", "which", "that"),
    "de": ("ist", "sind", "und", "der", "die", "das", "mit", "von", "wird",
           "bei", "zu", "für", "wie", "auf", "im", "dem", "den"),
    "fr": ("est", "sont", "et", "le", "la", "les", "du", "des", "où", "que",
           "qui", "avec", "dans", "pour", "sur", "par"),
    "es": ("es", "son", "el", "la", "los", "las", "del", "que", "con",
           "por", "para", "donde"),
    "it": ("è", "sono", "il", "la", "le", "di", "del", "che", "con", "per",
           "dove"),
    "pt": ("é", "são", "os", "as", "do", "que", "com", "em", "por", "para",
           "onde"),
}


@functools.lru_cache(maxsize=None)
def _fragment_tail_re(lang: Optional[str]) -> "re.Pattern":
    words = _FRAGMENT_TAIL_WORDS.get(lang or "en", _FRAGMENT_TAIL_WORDS["en"])
    return re.compile(
        r"(?:^|[\s$])(" + "|".join(re.escape(w) for w in words) + r")\s*$")


def fragment_anchors(line: str) -> List[Tuple[int, int, str]]:
    """Satırdaki hizalanabilir çapalar: ``(başlangıç, bitiş, anahtar)``.

    Anahtar boşluksuz içeriktir; ``$…$`` ile ``\\(…\\)`` aynı anahtarı alır
    (sanitizer biçimi değiştirebilir). Yalnız işleç/ok olan matematik
    (``$\\to$``, ``$\\times$``) çapa değildir: ardından gelen büyük harf bir
    şema etiketidir ("Demand $\\leftarrow$ Price signal").
    """
    out: List[Tuple[int, int, str]] = []
    if "$" in line:
        for start, end, display in _lang_detect().dollar_math_spans(line):
            if display:
                continue
            inner = line[start:end].strip("$")
            if not re.search(r"[A-Za-z0-9]", _BARE_CMD_NAME_RE.sub("", inner)):
                continue
            out.append((start, end, "m:" + _WS_RE.sub("", inner)))
    if "\\(" in line:
        for m in _FRAGMENT_PAREN_MATH_RE.finditer(line):
            inner = m.group(1)
            if re.search(r"[A-Za-z0-9]", _BARE_CMD_NAME_RE.sub("", inner)):
                out.append((m.start(), m.end(), "m:" + _WS_RE.sub("", inner)))
    if "\\textit" in line or "\\emph" in line:
        for m in _FRAGMENT_ITALIC_RE.finditer(line):
            out.append((m.start(), m.end(), "i:" + _WS_RE.sub("", m.group(1))))
    if "(" in line:
        for m in _FRAGMENT_PAREN_REF_RE.finditer(line):
            out.append((m.start(), m.end(), "r:" + _WS_RE.sub("", m.group(0))))
    out.sort()
    return out


def anchor_follow(line: str, end: int) -> Tuple[str, Optional[str], bool]:
    """Çapadan sonra ne geliyor? ``(tür, kelime, kesme_işareti_var_mı)``.

    tür: "cap" (büyük harfle başlayan, gerisi küçük harfli kelime), "low"
    (küçük harfle başlayan kelime), "punct" (başka karakter) ya da "eol".
    """
    m = _FRAGMENT_FOLLOW_RE.match(line, end)
    word = m.group(1)
    if word:
        apos = line[m.end():m.end() + 1] in ("'", "’")
        if word[0].isupper() and len(word) > 1 and word[1:].islower():
            return "cap", word, apos
        if word[0].islower():
            return "low", word, apos
        return "punct", None, False
    if m.group(2):
        return "punct", None, False
    return "eol", None, False


def _blank_math_spans(line: str) -> str:
    """``$…$`` matematiğini (TeX okuma sırasıyla) boşlukla doldur."""
    if "$" not in line:
        return line
    chars = list(line)
    for start, end, _display in _lang_detect().dollar_math_spans(line):
        for k in range(start, end):
            chars[k] = " "
    return "".join(chars)


def paren_balance(line: str) -> Tuple[int, int]:
    """Matematik ve komut seçenekleri dışında ``(``/``)`` ve ``[``/``]`` farkı."""
    text = _blank_math_spans(line)
    text = re.sub(r"\\\\\[[^\]]*\]", " ", text)        # \\[2pt]
    text = re.sub(r"\\[\[\]()]", " ", text)             # \[ \] \( \)
    text = re.sub(r"\\[A-Za-z@]+\*?(?:\[[^\]]*\])?", " ", text)
    return (text.count("(") - text.count(")"),
            text.count("[") - text.count("]"))


class _FragmentSourceIndex:
    """Kaynak satırlarının çapa dizini (parçalı yol hizalaması için)."""

    __slots__ = ("lines", "by_key", "follow")

    def __init__(self, lines: List[str]) -> None:
        self.lines = lines
        self.by_key: Dict[str, List[int]] = {}
        # satır -> {çapa anahtarı: {ardından gelen türler}}
        self.follow: Dict[int, Dict[str, Set[str]]] = {}
        for i, line in enumerate(lines):
            if ("$" not in line and "\\(" not in line and "(" not in line
                    and "\\textit" not in line and "\\emph" not in line):
                continue
            kinds: Dict[str, Set[str]] = {}
            for _s, end, key in fragment_anchors(line):
                kinds.setdefault(key, set()).add(anchor_follow(line, end)[0])
            if kinds:
                self.follow[i] = kinds
                for key in kinds:
                    self.by_key.setdefault(key, []).append(i)

    def best_line(self, keys: Iterable[str], expect: int,
                  window: int) -> Optional[int]:
        """Pencere içinde çapaların en çoğunu paylaşan kaynak satırı."""
        score: Dict[int, int] = {}
        lo, hi = expect - window, expect + window
        for key in set(keys):
            for i in self.by_key.get(key, ()):
                if lo <= i <= hi:
                    score[i] = score.get(i, 0) + 1
        if not score:
            return None
        return max(score, key=lambda i: (score[i], -abs(i - expect)))


# --- Sayı bozulması (MT-7) ---
# Karşılaştırma işaretleri tek biçime indirilir (``$\leq$``, ``\leq``,
# ``\textless`` → ≤ / <), biçim sarmalayıcıları (\textit{<}) ve '$' atılır.
_REL_NORMALIZE: Tuple[Tuple["re.Pattern", str], ...] = (
    (re.compile(r"\$\s*\\(?:leqslant|leq|le)\s*\$"), "≤"),
    (re.compile(r"\$\s*\\(?:geqslant|geq|ge)\s*\$"), "≥"),
    (re.compile(r"\$\s*<\s*\$"), "<"),
    (re.compile(r"\$\s*>\s*\$"), ">"),
    (re.compile(r"\\textless(?:\{\})?"), "<"),
    (re.compile(r"\\textgreater(?:\{\})?"), ">"),
    (re.compile(r"\\(?:leqslant|leq|le)(?![A-Za-z])"), "≤"),
    (re.compile(r"\\(?:geqslant|geq|ge)(?![A-Za-z])"), "≥"),
    (re.compile(r"\\lt(?![A-Za-z])"), "<"),
    (re.compile(r"\\gt(?![A-Za-z])"), ">"),
)
_REL_WRAP_RE = re.compile(
    r"\\(?:textit|textbf|textrm|textup|textsl|emph|mathrm|mathit|text)"
    r"\{([^{}]*)\}")
# Arka arkaya iki karşılaştırma işareti: "Bi < ≤ 0" (kaynakta "Bi < 0,1").
_DOUBLE_REL_RE = re.compile(
    r"(?<![<>≤≥=\-])([<>≤≥])[ \t]+([<>≤≥])(?![<>≤≥=\-])")
# Bölünmüş ondalık: virgülden önce boşluk ("0 , 1") ya da virgülle rakam
# arasına işaret girmiş ("0 , ≤ 1"). Virgülden SONRA boşluk tek başına
# sayılmaz: "1, 2 ve 3" Türkçe sıralamadır.
_SPLIT_DECIMAL_RE = re.compile(
    r"(?<![\d.,])(\d{1,6})[ \t]+,[ \t]*[<>≤≥]?[ \t]*(\d{1,6})(?![\d])"
    r"|(?<![\d.,])(\d{1,6})[ \t]*,[ \t]*[<>≤≥][ \t]*(\d{1,6})(?![\d])")
# İki sayı arasında karşılaştırma işareti: "0 ≤ 1" (kaynakta "0,1").
_REL_BETWEEN_NUMS_RE = re.compile(
    r"(?<![\d.,])(\d{1,6})[ \t]*([<>≤≥])[ \t]*(\d{1,6})(?![\d.,])")
_REL_NUM_RE = re.compile(r"([<>≤≥])[ \t]*(-?\d+(?:[.,]\d+)?)")


def rel_normalize(text: str) -> str:
    """Karşılaştırma işaretlerini tek biçime indir, sarmalayıcıları at."""
    for rx, rep in _REL_NORMALIZE:
        text = rx.sub(rep, text)
    for _ in range(2):
        text = _REL_WRAP_RE.sub(r"\1", text)
    return text.replace("$", " ").replace("~", " ")


# --- Altyazı / float / dipnot iskeleti (MT-17, gpt-oss) ---
_CAPTION_OPEN_RE = re.compile(r"\\caption(?:of)?\*?\s*(?:\[[^\]]*\])?\s*(?=\{)")
_FOOTNOTE_OPEN_RE = re.compile(r"\\footnote\s*(?:\[[^\]]*\])?\s*\{")
_FLOAT_BEGIN_RE = re.compile(
    r"\\begin\{(figure\*?|table\*?|wrapfigure|SCfigure)\}")
_CAPTION_LABEL_WORDS = (
    r"Şekil|Şek\.|Tablo|Resim|Grafik|Görsel|Figure|Fig\.|Table|Abbildung"
    r"|Abb\.|Tabelle|Tab\.|Figura|Tabla|Tabella|Рис\.?|Рисунок|Таблица")
# Yalnız etiket ("Şekil 1.1:") ya da boş altyazı argümanı.
_CAPTION_LABEL_ONLY_RE = re.compile(
    rf"^\s*(?:(?:{_CAPTION_LABEL_WORDS})\s*[\dA-Za-z.\-–]*\s*[:.]?)?\s*$")
_CAPTION_LABEL_START_RE = re.compile(rf"^\s*(?:{_CAPTION_LABEL_WORDS})")
# Numaralı altyazı etiketiyle başlayan düz metin ("Şekil 1.1: …").
_CAPTION_LABEL_NUM_RE = re.compile(
    rf"^\s*(?:{_CAPTION_LABEL_WORDS})\s*\d+(?:[.\-–]\d+)*\s*[:.]?")
_TEXT_FMT_OPEN_RE = re.compile(
    r"\\(?:text(?:bf|it|rm|sf|sc|up|sl|tt)?|emph|underline)\s*(?=\{)")
_PROSE_WORD_RE = re.compile(r"[^\W\d_]{2,}")
# Etiketi ve açıklamasını taşıyan biçim grubundan sonra altyazı ayracı içinde
# (ya da ayraçtan sonra) süren metin: "\caption*{\textit{Şekil 8: …}} Ardından
# gelen …". Görev tanımı: altyazıda 60+ sözcük ve cümle sonundan
# sonra yeni cümleler.
CAPTION_GLUE_MIN_WORDS = 60
CAPTION_GLUE_TAIL_MIN_WORDS = 8
CAPTION_LABEL_GROUP_MIN_WORDS = 12
# Float ortamında altyazı dışında bu kadar sözcük düz metin varsa (altyazı
# ayraçtan çıkmış ya da gövde paragrafı float'a girmiş) bulgu olur.
FLOAT_PROSE_MIN_WORDS = 5

# --- Kaynak dilde cümle (MT-9) ---
# Türkçe satırın içindeki tek cümle kaynak dilde (de/fr/es/it/pt) kalmış ya
# da model onu kaynak dilde YENİDEN yazmış. Satır düzeyi sınıflandırıcı
# (lang_detect.classify_latin_line) Türkçeye özgü harf (ş, ğ, ı) gördüğü
# satırı hiç sınıflandırmaz; kısa cümlede de 3 işlev kelimesi ister. Cümle
# düzeyinde: Türkçeye özgü harf yok, en az SOURCE_SENTENCE_MIN_HITS kaynak
# dili işlev kelimesi, Türkçe işlev kelimesi yok, kaynak dili kelimeleri
# İngilizceninkinin SOURCE_QUOTE_LANG_DOMINANCE katı ve cümlenin en az
# SOURCE_SENTENCE_MIN_SHARE payı. Cümle kaynakta birebir geçiyorsa
# (SOURCE_QUOTE_MIN_COVERAGE) "çevrilmemiş", geçmiyorsa "uydurma/yeniden
# yazılmış" sayılır.
SOURCE_SENTENCE_MIN_HITS = 2
SOURCE_SENTENCE_MIN_SHARE = 0.25
# Satırın kaynak dilde cümle dışındaki kısmı en az bu kadar kelime ve Türkçe
# işaret (Türkçeye özgü harf ya da Türkçe işlev kelimesi) taşımalı.
SOURCE_SENTENCE_TR_REST_MIN_WORDS = 3
# Bu kadar ya da daha az kelimelik ayraç grubu ({…}) eser adı sayılır ve
# cümle değerlendirmesinden düşülür.
SOURCE_SENTENCE_TITLE_MAX_WORDS = 12
_BRACE_GROUP_RE = re.compile(r"\{([^{}]*)\}")
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?;:])\s+")

# --- Boru hattı artıkları ---
FOOTNOTE_DEF_MIN_WORDS = 3
# Gövdede bu kadar ya da daha çok dipnot tanımı paragrafı varsa kusur
# sistematiktir (bir test raporunda 125 dipnot; okuma bulgusu "yüksek") → MEDIUM;
# daha azı → LOW.
FOOTNOTE_DEF_SYSTEMATIC = 3
# Satır başında "\textsuperscript{N} metin": PDF dipnotu \footnote'a
# eşlenmemiş, gövdede paragraf olarak duruyor (bilinçli karar olsa da).
# Gövdeye gömülmüş dipnot: "[* Ay anlamına gelen …]" (çevirmen dipnotu
# satır içi nota çevirmiş).
_INLINE_NOTE_RE = re.compile(r"\[\*+\s*\S")
_FOOTNOTE_DEF_LINE_RE = re.compile(
    r"^\s*\\textsuperscript\{\s*(\d{1,3})\s*\}\s*(\S.*)$")
# Cümle sonundan sonra satır sonunda düz rakam: dipnot işareti üst simge
# olmamış ("… ödeme almadık.} 3"). Yalnız belgede o numaralı dipnot tanımı
# varsa sayılır.
_PLAIN_FOOTNOTE_MARK_RE = re.compile(r"[.!?:;”\"’)\]]\}?[ \t]+(\d{1,3})\s*$")
# Tek başına sayıdan oluşan paragraf: kaynağın sayfa numarası metne sızmış.
# En çok 3 hane: dört haneli tek sayı çoğunlukla yıldır ("1923", başlık
# sayfası; derlem ölçümü).
_PAGE_NUMBER_LINE_RE = re.compile(r"^\s*\d{1,3}\s*$")
# Sayıdan önceki/sonraki kod bloğu sınırı: "Cevap:" listelemesinden sonra
# gelen "38" ya da numaralı kod örnekleri ("1" + \begin{lstlisting}) sayfa
# numarası değil, içeriktir (derlem ölçümü, bir İngilizce test kitabı).
_CODE_BLOCK_EDGE_RE = re.compile(
    r"^\s*\\(?:begin|end)\{(?:lstlisting|verbatim|Verbatim|minted|alltt"
    r"|comment|tcolorbox)\*?\}")
# Yalnız kapanışı olan ya da tek başına anlamlı HTML öğeleri; açılış
# etiketi (<u>, <s>, <a …>) aynı satırda kapanışıyla birlikte sayılır:
# "<s>[INST] <<SYS>>" LLM istem biçimidir, HTML artığı değil.
_HTML_VOID_TAGS = frozenset({"br", "hr", "img"})
# Bir önceki paragrafı bitiren/yeni blok açan komutlar (gövde metni sayılmaz).
_BLOCK_CMD_LINE_RE = re.compile(
    r"^\s*\\(?:chapter|section|subsection|subsubsection|paragraph"
    r"|subparagraph|part|begin|end|item|caption|label|includegraphics"
    r"|centering|par|bigskip|medskip|smallskip|vspace|newpage|clearpage"
    r"|cleardoublepage|pagebreak|hline|toprule|midrule|bottomrule"
    r"|bibitem|printbibliography|tableofcontents|maketitle)(?![A-Za-z])"
    r"|^\s*\\[\[\]]")
_VISIBLE_HTML_TAG_RE = re.compile(
    r"</?(?:u|i|b|s|em|strong|sup|sub|span|div|p|br|a|font|mark|small|big"
    r"|code|tt|ins|del|strike|center|li|ul|ol|table|tr|td|th|h[1-6]|img|hr"
    r"|blockquote)(?:\s[^<>\n]*)?/?>", re.IGNORECASE)
_HTML_ENTITY_RE = re.compile(
    r"&(?:amp|lt|gt|nbsp|quot|apos|#\d{2,5}|#x[0-9a-fA-F]{2,4});")
_MARKDOWN_BOLD_RE = re.compile(r"\*\*")
_MARKDOWN_UNDERSCORE_RE = re.compile(
    r"(?<![\w\\])__(?=[^\W_])|(?<=[^\W_])__(?![\w])")
# Kod/URL argümanları (içlerinde **, __, &, <b> meşru olabilir).
_CODE_LIKE_ARG_RE = re.compile(
    r"\\(?:texttt|url|path|nolinkurl|lstinline|href)\s*\{[^{}]*\}")
_VERB_INLINE_RE = re.compile(r"\\verb\*?(\S).*?\1")
# '&' hizalama sekmesidir; yalnız bu ortamların içinde meşrudur.
_AMP_ALIGN_ENVS = frozenset({
    "tabular", "tabular*", "tabularx", "tabulary", "longtable",
    "supertabular", "xtabular", "xltabular", "array", "align", "align*",
    "alignat", "alignat*", "aligned", "alignedat", "eqnarray", "eqnarray*",
    "flalign", "flalign*", "split", "cases", "dcases", "rcases", "matrix",
    "pmatrix", "bmatrix", "Bmatrix", "vmatrix", "Vmatrix", "smallmatrix",
    "subarray", "IEEEeqnarray", "tabu", "NiceTabular", "NiceArray",
    "gathered", "multlined", "tblr", "longtblr",
})
_AMP_VERBATIM_ENVS = frozenset({
    "verbatim", "Verbatim", "lstlisting", "minted", "comment", "alltt",
})
_AMP_TOKEN_RE = re.compile(
    r"\\begin\{([^{}]+)\}|\\end\{([^{}]+)\}|\\[&%$#_{}]|\\\\"
    r"|\\verb\*?(\S)|\\(?:href|url|includegraphics|input|include|label"
    r"|ref|eqref|pageref|autoref|cref|Cref)\*?\s*(?:\[[^\]]*\]\s*)?(?=\{)"
    r"|%|&")
# Kaçışsız '&' ve iki yanındaki birer kelime (kaynakta aynen var mı?):
# "Rehfeld & Söhne", "Doe&Roe-1959-Exp&".
_AMP_CONTEXT_RE = re.compile(r"(?:[^\s&]+[ \t]+)?[^\s&]*(?<!\\)&[^\s&]*(?:[ \t]+[^\s&]+)?")
# Tek başına kalmış yer tutucu gövdesi: "“LATEX_1941”" (açılı ayraçlar
# düşmüş; Check 6 yalnız <<…>> / < < … > > biçimlerini arar).
_BARE_LATEX_PLACEHOLDER_RE = re.compile(r"(?<![A-Za-z<])LATEX\s*_\s*\d+(?!\d)")
_ANY_LATEX_PLACEHOLDER_RE = re.compile(r"<\s*<\s*LATEX\s*_?\s*\d+\s*>\s*>")


class _SourceContext:
    """Bir kaynak gövdeden bir kez çıkarılan QC bağlamı (önbelleklenir)."""

    __slots__ = ("lang", "letter_runs", "norm_words", "word_count",
                 "first_person")

    def __init__(self, source_body: str) -> None:
        ld = _lang_detect()
        clean = _clean_for_language(source_body) if source_body else ""
        lang: Optional[str] = None
        if clean.strip():
            est = ld.analyze_language(clean, already_clean=True)
            if (est.code != "und"
                    and est.confidence >= SOURCE_LANG_MIN_CONFIDENCE):
                lang = est.code
        self.lang = lang
        # Ham kaynaktaki harf dizileri (büyük/küçük harf korunur): bağlantı
        # ve komut argümanlarındaki diziler de dahil ("…h-18.htm.xhtml").
        self.letter_runs: frozenset = frozenset(
            _LETTER_RUN_RE.findall(source_body or ""))
        words = ld.tokenize_words(clean)
        self.word_count = len(words)
        # Birebir alıntı araması için: " w1 w2 ... wn " (kelime sınırlı).
        self.norm_words = " " + " ".join(words) + " "
        fp_words = _SOURCE_FIRST_PERSON_WORDS.get(
            "ru" if lang == "cyrillic" else lang or "")
        self.first_person = (sum(1 for w in words if w in fp_words)
                             if fp_words else 0)

    def is_first_person_voice(self) -> bool:
        """Kaynak birinci tekil şahısla mı yazılmış (yazar/anlatıcı sesi)?"""
        if self.first_person < FIRST_PERSON_SOURCE_MIN or not self.word_count:
            return False
        return (self.first_person * 1000.0 / self.word_count
                >= FIRST_PERSON_SOURCE_PER_1000)

    def verbatim_coverage(self, text: str) -> float:
        """*text*'in kelimelerinin kaynakta birebir (ardışık) geçen
        ``SOURCE_QUOTE_MIN_RUN``+ kelimelik dizilerle örtülen payı.

        Kelime sınırlı arama: " a b c " kaynak dizisinde aranır. Açgözlü:
        her konumdan en uzun eşleşen dizi alınır.
        """
        words = _lang_detect().tokenize_words(text)
        n = len(words)
        if n < SOURCE_QUOTE_MIN_RUN:
            return 0.0
        covered = 0
        i = 0
        while i < n:
            best = 0
            j = i + SOURCE_QUOTE_MIN_RUN
            while j <= n and (" " + " ".join(words[i:j]) + " ") in self.norm_words:
                best = j - i
                j += 1
            if best:
                covered += best
                i += best
            else:
                i += 1
        return covered / n


# ---------------------------------------------------------------------------
# QCEngine
# ---------------------------------------------------------------------------

class QCEngine:
    """Post-translation quality control engine.

    Runs 34 independent checks on translated LaTeX output and produces
    a scored QCReport.

    All checks use simple heuristics -- no external NLP/language
    detection libraries required.
    """

    def __init__(self, glossary_dir: Optional[str] = None,
                 book_type: Optional[str] = None,
                 user_defined_envs: Optional[Iterable[str]] = None,
                 kitap_sozlugu=None,
                 tutarlilik_denetimi: bool = True) -> None:
        # Book type for context-aware checks (e.g. skip first_person_academic
        # for non-academic genres like 'philosophy', 'novel')
        self._book_type: Optional[str] = book_type

        # G2c (KARAR 25, 2026-10-05): kitabın kendi sözlüğü
        # (latex_pipeline.kitap_sozlugu.KitapSozlugu; glossary_shared.tsv).
        # Verilirse term_consistency denetimi koşar (UYARI, değiştirme yok).
        # 2026-10-06 (KARAR 25: kitap sözlüğü önceliklidir): genel sözlüğün
        # (glossary/curated.json + sabit desenler) kitap sözlüğüyle ÇELİŞEN
        # kuralı o kitapta uygulanmaz (bkz. _kitap_secimleri).
        # tutarlilik_denetimi=False: sözlük yalnız bu muafiyet için verilir
        # (TeX yolu; G2c'nin kararı: o yolda tutarlılık denetimi koşmaz).
        self._kitap_sozlugu = kitap_sozlugu
        self._tutarlilik_denetimi: bool = bool(tutarlilik_denetimi)
        self._secimler = None
        self._secimler_hazir = False

        # F2 (2026-07-28): gövde DIŞINDA (örn. preamble_ozel.tex) tanımlanan
        # ortam adları — translated_env_name check'i bunları issue saymaz.
        # Gövde içi tanımlar her analyze'da ayrıca taranır.
        self._user_defined_envs: frozenset = frozenset(user_defined_envs or ())

        # Known MT system prompt fragments that might leak into output
        # NOT (audit 2026-07-16 #28): fiilsiz genis ifadeler ("Orijinal
        # metin", "Matematiksel notasyon") mesru kitap cumlelerini leak
        # sayiyordu; talimat fiili zorunlu hale getirildi.
        self._prompt_patterns: List[str] = [
            "Tüm matematiksel notasyonları koru",
            "Standart Türk bilimsel",
            "LaTeX komutlarını koruyun",
            "çeviri yaparken",
            "You are a professional",
            "Translate the following",
            "Please translate",
            "Keep all LaTeX commands",
            "Do not translate mathematical",
            "Preserve the original formatting",
            "bilimsel terimleri kullanılmalıdır",
            "matematiksel ifadeleri değiştirmeyin",
            "formülleri olduğu gibi bırakın",
            # HY-MT 7B specific leakage (Turkish translation of instruction)
            "denklemleri, değişken adlarını",
            "birim kısaltmalarını",
            "formülleri değiştirmeden kullanın",
            "terimlerini kullanın",
            "Başka hiçbir şey kullanmayın",
            # English instruction leakage
            "variable names (v, T, p, rho",
            "unit abbreviations (m/s, kg",
            "Keep all mathematical notation",
            "Use standard Turkish scientific",
            "Only output the translation",
            # ----- NEW: Codex findings 2026-03-02 -----
            "Matematiksel gösterimleri koru",
            "Matematiksel ifadeleri koru",
            "Yalnızca bu kısmı çevir",
            "Sadece bu kısmı çevir",
            "Lütfen çevirin",
            "Aşağıdaki metni çevirin",
            "Çeviri talimatları",
            "Bu bir çeviridir",
            "Orijinal metni koru",
            "Orijinal metni değiştirme",
            "Metni Türkçeye çevir",
            # ----- NEW: Session 5 additions (2026-03-03) -----
            # HY-MT 7B actual system prompt fragments
            "Translate from English to Turkish",
            "Translate from",
            "Output only the translation",
            "Preserve math notation",
            "Preserve mathematical notation",
            # Turkish variants of system prompt
            "Sadece çeviriyi çıktı",
            "Sadece çeviriyi ver",
            "Sadece çeviriyi döndür",
            "Matematiksel notasyonu koru",
            "Matematik notasyonu koru",
            "Bağlamı koru",
            "Sadece çevrilmiş metni",
            "Açıklama yapma",
            "Yorum ekleme",
            "Çeviri notu",
            "Çevirmen notu",
            "İngilizce'den Türkçe'ye",
        ]

        # A-1 (2026-07-27): "Çeviri/Çevirmen notu" YALNIZ Yol B'de leak'tir
        # (HY-MT halüsinasyonu). Yol A'da çevirmenin bilinçli notu — manuel
        # modda bu alt küme _check_prompt_leakage'da ATLANIR (ana liste Yol B
        # sözleşmesi olarak tam kalır; testler onu doğrular).
        self._prompt_patterns_mt_only: frozenset = frozenset({
            "Çeviri notu",
            "Çevirmen notu",
        })
        self._prompt_leak_regexes_mt_only: frozenset = frozenset({
            r"(?i)[ÇçCc]eviri\s+notu",
            r"(?i)[ÇçCc]evirmen\s+notu",
        })

        # Regex-based prompt leak patterns (for angle-bracket markers etc.)
        # These are compiled re.Pattern objects, searched WITHOUT re.escape.
        self._prompt_leak_regexes: List[re.Pattern] = [
            # Turkish angle-bracket instruction markers (HY-MT 7B leakage)
            re.compile(r"<{2,3}[^>]*ÇEVİR[İI][^>]*>{2,3}", re.IGNORECASE),
            re.compile(r"<{2,3}[^>]*BAŞLA[^>]*>{2,3}", re.IGNORECASE),
            re.compile(r"<{2,3}[^>]*SON[^>]*>{2,3}", re.IGNORECASE),
            re.compile(r"<{2,3}[^>]*TRANSLATE[^>]*>{2,3}", re.IGNORECASE),
            re.compile(r"<{2,3}[^>]*BEGIN[^>]*>{2,3}", re.IGNORECASE),
            re.compile(r"<{2,3}[^>]*END[^>]*>{2,3}", re.IGNORECASE),
            # Turkish instruction sentences that leak
            re.compile(
                r"(?i)(?:aşağıdaki|yukarıdaki)\s+(?:metni|paragrafı|bölümü)\s+"
                r"(?:türkçeye?\s+)?çevir"
            ),
            re.compile(
                r"(?i)çeviri(?:ye|nin|yi)?\s+(?:başla|sonu?|bitir)"
            ),
            # ----- NEW: Codex findings 2026-03-02 -----
            re.compile(
                r"(?i)matematiksel\s+g[öo]sterimleri\s+koru"
            ),
            re.compile(
                r"(?i)yaln[ıi]zca\s+bu\s+k[ıi]sm[ıi]\s+[çc]evir"
            ),
            re.compile(
                r"(?i)l[üu]tfen\s+[çc]evirin"
            ),
            re.compile(
                r"(?i)a[şs]a[ğg][ıi]daki\s+metni\s+[çc]evirin"
            ),
            re.compile(
                r"(?i)[çc]eviri\s+talimatlar[ıi]"
            ),
            re.compile(
                r"(?i)orijinal\s+metn?i?n?(?:deki|in)?\s+"
                r"(?:format[ıi]n?[ıi]?\s+)?(?:koru|aynen|de[ğg]i[şs]tirme)"
            ),
            re.compile(
                r"(?i)metni\s+t[üu]rk[çc]eye\s+[çc]evir"
            ),
            # ----- NEW: Additional patterns (session 4) -----
            re.compile(r"(?i)kaynak\s+dil[iı]n?[iı]?\s+koru"),
            re.compile(r"(?im)^\s*[çc]eviriyi\s+yap[.!]?\s*$"),
            re.compile(r"(?i)keep\s+(?:all\s+)?mathematical\s+notations?"),
            re.compile(r"(?i)only\s+translate\s+this\s+(?:part|section|segment)"),
            re.compile(r"(?i)do\s+not\s+translate"),
            re.compile(r"(?i)preserve\s+(?:the\s+)?original\s+formatting"),
            re.compile(r"(?i)form[üu]lleri\s+de[ğg]i[şs]tirmeden"),
            re.compile(r"(?i)LaTeX\s+komutlar[ıi]n[ıi]\s+de[ğg]i[şs]tirme"),
            # ----- NEW: Session 5 patterns (2026-03-03) -----
            re.compile(r"(?i)translate\s+from\s+\w+\s+to\s+\w+"),
            re.compile(r"(?i)output\s+only\s+the\s+translation"),
            re.compile(r"(?i)preserve\s+math\s+notation"),
            re.compile(r"(?i)preserve\s+(?:the\s+)?(?:mathematical|math)\s+(?:notation|formulas?)"),
            re.compile(r"(?i)sadece\s+[çc]eviriyi\s+[çc][ıi]kt[ıi]"),
            re.compile(r"(?i)sadece\s+[çc]eviri(?:yi)?\s+ver"),
            re.compile(
                r"(?i)matematik(?:sel)?\s+notasyon\w*\s+"
                r"(?:koru|aynen|oldu[ğg]u\s+gibi|de[ğg]i[şs]tirme)"
            ),
            re.compile(
                r"(?i)ba[ğg]lam[ıi]\s+"
                r"koru(?:yun|n|mal[ıi]s[ıi]n[ıi]z|mal[ıi]s[ıi]n)?\b"
            ),
            re.compile(r"(?i)sadece\s+[çc]evrilmi[şs]\s+metni"),
            re.compile(r"(?i)sadece\s+[çc]eviriyi\s+d[öo]nd[üu]r"),
            re.compile(r"(?i)a[çc][ıi]klama\s+yapma"),
            re.compile(r"(?i)yorum\s+ekleme"),
            re.compile(r"(?i)[ÇçCc]eviri\s+notu"),
            re.compile(r"(?i)[ÇçCc]evirmen\s+notu"),
            re.compile(r"(?i)please\s+translate"),
            re.compile(r"(?i)I\s+will\s+translate"),
            re.compile(r"(?i)here\s+is\s+the\s+translat(?:ion|ed)"),
            re.compile(r"(?i)[İIi]ngilizce'?den\s+T[üu]rk[çc]e'?ye"),
        ]

        # F27 (2026-07-29): literal kalıp indeksi bir kez kurulur.
        self._build_prompt_literal_index()

        # Caption/reference consistency pairs: (English form, Turkish form)
        # If both forms appear in the same document, that's inconsistent.
        self._consistency_pairs: List[Tuple[str, str]] = [
            ("Fig.", "Şekil"),
            ("Figure", "Şekil"),
            ("Table", "Tablo"),
            ("Eq.", "Denklem"),
            ("Equation", "Denklem"),
            ("Chapter", "Bölüm"),
            ("Section", "Bölüm"),
            ("Theorem", "Teorem"),
            ("Lemma", "Önerme"),
            ("Proof", "İspat"),
            ("Definition", "Tanım"),
            ("Algorithm", "Algoritma"),
        ]

        # Glossary for semantic checks (31-34)
        self._glossary_dir: Optional[str] = glossary_dir
        self._glossary_terms: Optional[Dict] = None  # lazy loaded
        self._glossary_loaded: bool = False

        # 2026-09-25 (kulvar Q): analiz başına kaynak bağlamı ve denetimler
        # arası paylaşılan durum. analyze() her çağrıda yeniler; denetimler
        # doğrudan çağrılırsa (testler) boş varsayılanlarla eski davranış.
        self._src_ctx: Optional[_SourceContext] = None
        self._src_ctx_key: Optional[Tuple[int, int]] = None
        # Check 2'nin çevrilmemiş saydığı satırlar: {satır: dil kodu}.
        self._untranslated_lines: Dict[int, str] = {}
        # Check 2'nin kaynakta aynen geçen yabancı dil alıntısı diye muaf
        # tuttuğu satırlar (kulvar F2): Check 34 bu satırlarda İngilizce
        # artikel aramaz ("a principiis" Latincedir).
        self._foreign_quote_lines: Set[int] = set()
        # Check 2'nin karışık dilli (Türkçe + İngilizce cümle) diye raporladığı
        # satırlar: Check 34 bunlarda artikeli ikinci kez saymaz.
        self._mixed_language_lines: Set[int] = set()
        # Kulvar G4: kaynak dilde cümle bulunan satırlar ve parçalı yol
        # denetiminin kaynak çapa dizini (kaynak anahtarıyla önbellek).
        self._src_sentence_lines: Set[int] = set()
        self._frag_src_cache = None
        # Check 33e için kaynak paragrafları: (kaynak özeti, [(ilk satır,
        # son satır, kelimeler, değişmez belirteçler)]) (tembel, kaynak
        # başına bir kez; kulvar F2).
        self._src_paragraphs: Optional[
            Tuple[int, List[Tuple[int, int, List[str], Counter]]]] = None
        self._ocr_corrected_src_nums: Set[str] = set()

    # ------------------------------------------------------------------
    # Glossary lazy loader (for checks 31-32)
    # ------------------------------------------------------------------

    def _load_glossary_for_qc(self) -> Dict:
        """Load glossary terms from JSON files (lazy, fault-tolerant).

        Returns a dict: {
            "high_terms": {en_lower: tr_str, ...},
            "wrong_alternatives": {wrong_tr: (correct_tr, en_term), ...},
        }
        """
        if self._glossary_loaded:
            return self._glossary_terms or {}

        self._glossary_loaded = True

        if not self._glossary_dir:
            self._glossary_terms = {}
            return {}

        glossary_path = Path(self._glossary_dir)
        if not glossary_path.is_dir():
            logger.debug("Glossary dir not found: %s", self._glossary_dir)
            self._glossary_terms = {}
            return {}

        high_terms: Dict[str, str] = {}
        wrong_alts: Dict[str, Tuple[str, str]] = {}

        # 2026-07-11 (#7): book_type domain filtresi — eskiden TUM
        # glossary/*.json yukleniyordu; (silinen) felsefe turu sozlugunun
        # wrong_alternatives girisleri bilim kitabinda da uygulanip
        # ('matris' -> 'The Matrix' duzeltme onerisi gibi)
        # yanlis-pozitif/yanlis-fix uretiyordu.
        # Ceviri tarafiyla AYNI harita kullanilir (tek otorite).
        _allowed_files = None
        if self._book_type:
            try:
                from latex_pipeline.glossary import _DOMAIN_MAP, _FILE_DOMAIN_MAP
                _active = set(_DOMAIN_MAP.get(self._book_type, ["general"]))
                _allowed_files = {
                    name for name, domains in _FILE_DOMAIN_MAP.items()
                    if domains & _active
                }
            except Exception as exc:
                logger.debug("QC glossary domain filtresi kurulamadi: %s", exc)
                _allowed_files = None

        # sorted() makes the "last writer wins" resolution deterministic when
        # the same EN term appears in multiple files (QC-N5); glob() order is
        # otherwise filesystem-dependent and non-reproducible.
        for json_file in sorted(glossary_path.glob("*.json")):
            if _allowed_files is not None and json_file.name not in _allowed_files:
                logger.debug(
                    "QC glossary: %s atlandi (book_type=%s domain disi)",
                    json_file.name, self._book_type,
                )
                continue
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
                terms = data.get("terms", {})
                for en_term, info in terms.items():
                    if not isinstance(info, dict):
                        continue
                    tr_val = info.get("tr", "")
                    priority = info.get("priority", "low")

                    # Collect high-priority terms (min 4 chars EN)
                    if priority == "high" and len(en_term) >= 4 and tr_val:
                        # Gate out ASCII-corrupted TR values (QC-N4): a
                        # mid-word capital 'I' means the entry is garbage
                        # (e.g. "isinIm") and would inject broken text.
                        if _MIDWORD_CAP_I_RE.search(tr_val):
                            logger.debug(
                                "Skipping corrupt glossary value: %s -> %s (%s)",
                                en_term, tr_val, json_file.name,
                            )
                            continue
                        key = en_term.lower()
                        prev = high_terms.get(key)
                        if prev is not None and prev != tr_val:
                            logger.debug(
                                "Glossary conflict for '%s': '%s' -> '%s' (%s)",
                                key, prev, tr_val, json_file.name,
                            )
                        high_terms[key] = tr_val

                    # Collect wrong alternatives
                    for wrong in info.get("wrong_alternatives", []):
                        if wrong and len(wrong) >= 3:
                            wrong_alts[wrong.lower()] = (tr_val, en_term)
            except (json.JSONDecodeError, OSError, KeyError) as exc:
                logger.debug(
                    "Glossary load error for %s: %s", json_file, exc,
                )
                continue

        self._glossary_terms = {
            "high_terms": high_terms,
            "wrong_alternatives": wrong_alts,
        }
        logger.info(
            "Glossary loaded: %d high-priority terms, %d wrong alternatives.",
            len(high_terms), len(wrong_alts),
        )
        return self._glossary_terms

    # ------------------------------------------------------------------
    # Protected content masking helper (for checks 31-34)
    # ------------------------------------------------------------------

    @staticmethod
    def _mask_protected_content(text: str) -> str:
        """Replace math, verbatim, lstlisting, minted content with spaces.

        This prevents false positives inside environments that should
        remain in English/code.  Uses module-level _MATH_BLOCK_RE and
        _DISPLAY_BLOCK_RE plus additional patterns for code blocks.

        Newlines inside multi-line blocks are preserved (blanked to spaces
        only for non-newline characters) so that callers computing a line
        number via ``masked[:pos].count("\\n")`` stay aligned with the
        original text (QC-N6).
        """
        # Blank every character except newlines, keeping length + line breaks.
        def _blank(m: "re.Match") -> str:
            return re.sub(r"[^\n]", " ", m.group(0))

        masked = _MATH_BLOCK_RE.sub(_blank, text)
        masked = _DISPLAY_BLOCK_RE.sub(_blank, masked)
        masked = _INLINE_BLOCK_RE.sub(_blank, masked)
        # Also mask \verb|...|
        masked = re.sub(r"\\verb\|[^|]*\|", _blank, masked)
        return masked

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    # Checks whose auto-fix path re-invokes the (weak) HY-MT model or
    # rewrites text the agent deliberately produced.  In manual/agent
    # translation mode (Yol A) the agent is authoritative, so these are
    # downgraded to report-only (auto_fixable=False) -- see analyze().
    _MT_FEEDBACK_CHECKS: frozenset = frozenset({
        "untranslated",
        "untranslated_heading",
        "mixed_language_line",
        "glossary_compliance",
        "wrong_translation",
        "first_person_academic",
        # A-2/A-3 (2026-07-27): manuel modda rapor-only — antonym flip
        # satır-pozisyonel hizalamayla doğru çeviriyi bozabiliyor,
        # word_concatenation CamelCase özel adları bölüyordu.
        "antonym_guard",
        "word_concatenation",
        # F35 (2026-07-29): AutoFixer'ın _MANUAL_TRANSLATION_SKIP listesiyle
        # SENKRON tutulur (iki liste ayrışırsa sessiz delik açılır, A-17).
        # hallucination -> tekrarı budar, foreign_language_leakage -> yabancı
        # kelimeyi siler, number_hallucination -> sayıyı değiştirir; üçü de
        # Yol A'da çevirmenin bilinçli metnini ezebilir, rapor-only kalırlar.
        "hallucination",
        "foreign_language_leakage",
        "number_hallucination",
    })

    def _source_context(self, source_body: str) -> Optional[_SourceContext]:
        """Kaynak bağlamı; kaynak yoksa None. (uzunluk, hash) ile önbellek."""
        if not source_body or not source_body.strip():
            self._src_ctx_key = None
            return None
        key = (len(source_body), hash(source_body))
        if self._src_ctx is not None and self._src_ctx_key == key:
            return self._src_ctx
        self._src_ctx_key = key
        return _SourceContext(source_body)

    def _in_source_verbatim(self, word: str) -> bool:
        """Harf dizisi kaynakta AYNEN (büyük/küçük harf dahil) geçiyor mu?"""
        ctx = self._src_ctx
        return ctx is not None and word in ctx.letter_runs

    # Başlık kelimelerinin en az bu payı kaynakta geçmeli (kulvar Q).
    _HEADING_SOURCE_MIN_SHARE = 0.5

    def _heading_words_in_source(self, heading_clean: str) -> bool:
        """Başlığın 3+ harfli kelimelerinin en az yarısı kaynakta geçiyor mu?"""
        ctx = self._src_ctx
        if ctx is None:
            return True
        words = [w for w in _lang_detect().tokenize_words(heading_clean)
                 if len(w) >= 3]
        if not words:
            return True
        hits = sum(1 for w in words if f" {w} " in ctx.norm_words)
        return hits / len(words) >= self._HEADING_SOURCE_MIN_SHARE

    def _is_untranslated_source_line(self, line_idx: int,
                                     line_lang: Optional[str]) -> bool:
        """Satır Check 2'de "untranslated" sayıldı VE kaynağın KENDİ
        dilinde mi? (foreign_language_leakage'ın çift sayım muafiyeti)

        Kural (2026-09-28, kulvar Q; R5 "aynı pasaj üç kez sayılıyor"):
        kaynağın dilinde kalmış satır çevrilmemiş kaynak metnidir; bunu
        Check 2 raporlar, Check 19 aynı satırı "yanlış dile sızıntı" diye
        ikinci kez saymaz. Dil, Check 2'nin satır kararından ya da Check
        19'un işlev kelimelerinden (*line_lang*) gelir; biri kaynağın
        diliyle aynıysa yeter. Kaynak dili bilinmiyorsa (kaynak yok ya da
        güven düşük) ya da satır kaynaktan FARKLI bir dildeyse (İngilizce
        kaynaktan İspanyolca çıktı: gerçek sızıntı) eski davranış sürer:
        iki denetim de raporlar.
        """
        ctx = self._src_ctx
        if ctx is None or ctx.lang is None:
            return False
        code = self._untranslated_lines.get(line_idx)
        if code is None:
            return False
        return (_same_language(code, ctx.lang)
                or (line_lang is not None
                    and _same_language(line_lang, ctx.lang)))

    def _is_source_foreign_quote(self, raw_line: str,
                                 line_lang: Optional[str]) -> bool:
        """Çeviri satırı, kaynakta aynı dilde AYNEN geçen üçüncü dil alıntısı mı?

        Kural (2026-09-25, kulvar Q; İtalyanca kitapta Flammarion alıntısı): satırın
        dili kaynağın baskın dilinden FARKLI (İtalyanca kitapta Fransızca
        alıntı) ve satır kelimelerinin en az ``SOURCE_QUOTE_MIN_COVERAGE``
        payı kaynakta birebir geçiyorsa, çevirmen alıntıyı özgün dilinde
        bırakmıştır — çevrilmemiş kaynak metni değildir. Satır kaynağın
        KENDİ dilindeyse (İtalyanca kitapta İtalyanca satır) muafiyet yok:
        birebir kopya tam da çevrilmemiş metindir. Satırdaki çevirmen notu
        dipnotu (``\\footnote{Çevirmen notu: ...``) örtüye sayılmaz.
        """
        ctx = self._src_ctx
        if ctx is None or ctx.lang is None or not line_lang:
            return False
        if _same_language(line_lang, ctx.lang):
            return False
        ld = _lang_detect()
        m = TRANSLATOR_NOTE_FOOTNOTE_OPEN_RE.search(raw_line)
        if m:
            raw_line = raw_line[:m.start()]
        text = _clean_for_language(raw_line)
        # Satır dili sınıflandırıcısı akraba dillerde eşitlikte yanılır
        # (İtalyanca satır "fr"/"es" diye etiketlenebilir: it=3, fr=3).
        # İki dil de Latin profilliyse satırın dili kaynağın dilini işlev
        # kelimelerinde en az SOURCE_QUOTE_LANG_DOMINANCE kat geçmeli;
        # yoksa satır kaynağın kendi dilinde sayılır (muafiyet yok).
        if (line_lang in ld.LATIN_PROFILE_LANGS
                and ctx.lang in ld.LATIN_PROFILE_LANGS):
            hits, _ = ld.profile_hits(ld.tokenize_words(text))
            if (hits.get(line_lang, 0)
                    < SOURCE_QUOTE_LANG_DOMINANCE
                    * max(1, hits.get(ctx.lang, 0))):
                return False
        return ctx.verbatim_coverage(text) >= SOURCE_QUOTE_MIN_COVERAGE

    def _is_source_latin_quote(self, raw_line: str) -> bool:
        """Satır kaynakta AYNEN geçen Latince alıntı mı? (kulvar F2, MT-1)

        Latincenin dil profili yok; ``is_english_line`` Latince satırı
        İngilizce sanıyor. Karar iki koşula bağlı:

        * dil tespiti: ``is_latin_text`` (Latince işlev kelimeleri İngilizce,
          Türkçe ve Avrupa dillerinin işlev kelimelerini açıkça geçer).
          Kaynağın dili Avrupa diliyse (Fransızca kitapta "est", "qui")
          satır o dilin TAM profil sayımını da ``SOURCE_QUOTE_LANG_DOMINANCE``
          kat geçmeli: kaynağın kendi dilindeki çevrilmemiş satır muaf
          olmaz;
        * kaynakta birebir: kelimelerin en az ``SOURCE_QUOTE_MIN_COVERAGE``
          payı kaynakta ardışık dizilerle geçer.

        Çevrilmemiş İngilizce satır da kaynakta birebir geçer; onu ayıran
        dil tespitidir (İngilizce satırda Latince kanıt kelimesi yok).
        Satırdaki çevirmen notu dipnotu örtüye sayılmaz.
        """
        ctx = self._src_ctx
        if ctx is None:
            return False
        ld = _lang_detect()
        m = TRANSLATOR_NOTE_FOOTNOTE_OPEN_RE.search(raw_line)
        if m:
            raw_line = raw_line[:m.start()]
        text = _clean_for_language(raw_line)
        words = ld.tokenize_words(text)
        if not is_latin_text(words):
            return False
        if ctx.lang in ld.EUROPEAN_DETECT_LANGS:
            latin, _rival = latin_quote_hits(words)
            hits, _family = ld.profile_hits(words)
            if latin < SOURCE_QUOTE_LANG_DOMINANCE * max(
                    1, hits.get(ctx.lang, 0)):
                return False
        return ctx.verbatim_coverage(text) >= SOURCE_QUOTE_MIN_COVERAGE

    def analyze(
        self,
        translated_body: str,
        source_body: str = "",
        manual_translation: bool = False,
    ) -> QCReport:
        """Run all checks and return a scored QCReport.

        Parameters
        ----------
        translated_body : str
            The translated LaTeX body (Turkish).
        source_body : str, optional
            The original English LaTeX body. Some checks are skipped
            if this is not provided.
        manual_translation : bool, optional
            When True (Yol A / agent translation), checks that would feed
            text back into the weak HY-MT model or overwrite the agent's
            deliberate output are downgraded to report-only.  They are
            still detected and scored, but ``auto_fixable`` is forced to
            False so the AutoFixer never re-translates agent output.
            Backward compatible (default False = Yol B / HY-MT pipeline).

        Returns
        -------
        QCReport
            Report with all detected issues and a 0-100 score.
        """
        report = QCReport()
        self._in_verbatim = False  # Reset verbatim tracking state

        if not translated_body or not translated_body.strip():
            logger.warning("Empty translated body -- nothing to check.")
            report.score = 0.0
            return report

        tr_lines = translated_body.split("\n")
        en_lines = source_body.split("\n") if source_body else []

        # A-1/A-2 (2026-07-27): bazı check'ler manuel modda farklı davranır
        # (Çevirmen notu leak sayılmaz, antonym_guard rapor-only olur).
        self._manual_translation = manual_translation

        # 2026-09-25 (kulvar Q): kaynak bağlamı (baskın dil, birebir
        # alıntı dizisi, birinci şahıs yoğunluğu). Autofix döngüsü aynı
        # kaynağı defalarca verir; bağlam kaynak metne göre önbelleklenir.
        self._src_ctx = self._source_context(source_body)
        self._untranslated_lines = {}
        self._foreign_quote_lines = set()
        self._mixed_language_lines = set()
        # Kulvar G4: kaynak dilde cümle bulunan satırlar (Check 19 aynı
        # satırı ikinci kez saymasın).
        self._src_sentence_lines = set()
        self._src_paragraphs = None
        self._ocr_corrected_src_nums = set()

        logger.info(
            "QC Engine: analyzing %d TR lines, %d EN lines.",
            len(tr_lines), len(en_lines),
        )

        # Run all 35 checks
        self._check_formula_integrity(
            translated_body, source_body, tr_lines, en_lines, report,
        )
        self._check_untranslated_blocks(
            translated_body, tr_lines, report,
        )
        self._check_prompt_leakage(
            translated_body, tr_lines, report,
        )
        self._check_name_ref_corruption(
            translated_body, source_body, tr_lines, en_lines, report,
        )
        self._check_caption_consistency(
            translated_body, tr_lines, report,
        )
        self._check_placeholder_orphans(
            translated_body, tr_lines, report,
        )
        self._check_broken_latex_syntax(
            translated_body, tr_lines, report,
        )
        self._check_brace_balance(
            translated_body, tr_lines, report,
        )
        self._check_markdown_link_remnants(
            translated_body, tr_lines, report,
        )
        self._check_table_column_mismatch(
            translated_body, tr_lines, report,
        )
        self._check_untranslated_headings(
            translated_body, source_body, tr_lines, en_lines, report,
        )
        self._check_broken_inline_math(
            translated_body, tr_lines, report, en_lines=en_lines,
        )
        self._check_latex_encoded_turkish(
            translated_body, tr_lines, report,
        )
        self._check_information_loss(
            translated_body, source_body, tr_lines, en_lines, report,
        )
        self._check_equation_count(
            translated_body, source_body, tr_lines, en_lines, report,
        )
        self._check_figure_reference_integrity(
            translated_body, source_body, tr_lines, en_lines, report,
        )
        self._check_section_number_consistency(
            translated_body, source_body, tr_lines, en_lines, report,
        )
        self._check_href_preservation(
            translated_body, source_body, tr_lines, en_lines, report,
        )
        # Kulvar G4: cümle düzeyi kaynak dili denetimi Check 19'dan ÖNCE
        # (Check 19 bu satırları ikinci kez saymaz).
        self._check_source_language_sentences(
            translated_body, tr_lines, report,
        )
        self._check_foreign_language_leakage(
            translated_body, tr_lines, report,
        )
        self._check_antonym_guard(
            translated_body, source_body, tr_lines, en_lines, report,
        )
        self._check_first_person_academic(
            translated_body, tr_lines, report,
        )
        self._check_number_hallucination(
            translated_body, source_body, tr_lines, en_lines, report,
        )
        self._check_sentence_length_alignment(
            translated_body, source_body, tr_lines, en_lines, report,
        )
        self._check_translated_env_name(
            translated_body, tr_lines, report,
        )
        self._check_broken_command_braces(
            translated_body, tr_lines, report,
        )
        self._check_stash_placeholder_orphan(
            translated_body, tr_lines, report,
        )
        self._check_word_concatenation(
            translated_body, tr_lines, report,
        )
        self._check_html_artifacts(
            translated_body, tr_lines, report,
        )
        self._check_hallucination(
            translated_body, source_body, tr_lines, en_lines, report,
        )
        self._check_ascii_degradation(
            translated_body, tr_lines, report,
        )
        self._check_glossary_compliance(
            translated_body, source_body, tr_lines, en_lines, report,
        )
        self._check_wrong_translation(
            translated_body, tr_lines, report,
        )
        # G2c (KARAR 25): kitap sözlüğüne göre terim tutarlılığı (UYARI).
        self._check_term_consistency(
            translated_body, source_body, report,
        )
        self._check_hallucination_patterns(
            translated_body, source_body, tr_lines, en_lines, report,
        )
        self._check_turkish_naturalness(
            translated_body, tr_lines, report,
        )
        self._check_index_preservation(
            translated_body, source_body, tr_lines, en_lines, report,
        )
        # Kulvar G4 (2026-09-30): okuma bulgularından gelen denetimler.
        self._check_fragmented_lines(
            translated_body, source_body, tr_lines, en_lines, report,
        )
        self._check_number_corruption(
            translated_body, source_body, tr_lines, en_lines, report,
        )
        self._check_caption_structure(
            translated_body, source_body, tr_lines, en_lines, report,
        )
        self._check_footnote_count(
            translated_body, source_body, tr_lines, en_lines, report,
        )
        self._check_pipeline_artifacts(
            translated_body, source_body, tr_lines, en_lines, report,
        )

        # Manual/agent translation (Yol A): downgrade MT-feedback checks to
        # report-only so the AutoFixer never re-translates agent output with
        # the weak HY-MT model.  Issues stay visible + scored.
        if manual_translation:
            for issue in report.issues:
                if issue.check_name in self._MT_FEEDBACK_CHECKS:
                    issue.auto_fixable = False

        # Calculate score
        report.score = self._calculate_score(report)

        logger.info(
            "QC Engine: %d issues found, score %.0f/100.",
            len(report.issues), report.score,
        )

        return report

    # Alias: some callers use engine.run() instead of engine.analyze().
    # Both names do the same thing.
    run = analyze

    # ------------------------------------------------------------------
    # Score calculation
    # ------------------------------------------------------------------

    def _calculate_score(self, report: QCReport) -> float:
        """Calculate overall quality score from issue counts.

        Deduction formula:
            critical: -15 each
            high:     -8 each
            medium:   -3 each
            low:      -1 each
        """
        score = 100.0
        score -= report.critical_count * 15
        score -= report.high_count * 8
        score -= report.medium_count * 3
        score -= report.low_count * 1
        return max(0.0, score)

    # ------------------------------------------------------------------
    # Check 1: Formula Integrity
    # ------------------------------------------------------------------

    def _check_formula_integrity(
        self,
        tr_text: str,
        en_text: str,
        tr_lines: List[str],
        en_lines: List[str],
        report: QCReport,
    ) -> None:
        """Compare equation counts and \\tag{} values between EN and TR."""
        # 2026-09-25 (kulvar Q): paragraf sonuna kadar kapanmayan '$$'
        # display (sanitizer "$${}^{^1}$$ Türkçe..." -> "$$^{^1} Türkçe...")
        # TeX hatasıdır ve paragrafın metnini matematik kipine düşürür
        # (dil_sent_de: dipnot metni boşluksuz basıldı). Eskiden yalnız
        # inline sayımını çökertip yanıltıcı "EN=47 TR=16" veriyordu.
        _, unterminated = self._scan_dollar_math(tr_text, keep_lines=True)
        for ln in unterminated[:5]:
            report.issues.append(QCIssue(
                check_name="broken_latex_syntax",
                severity="high",
                line_num=ln,
                message=(
                    f"Unterminated $$ display math at L{ln}: paragraph ends "
                    f"inside display math (TeX: 'Display math should end "
                    f"with $$')"
                ),
                auto_fixable=False,
                confidence=0.95,
                fix_suggestion="Close the $$ display or make it inline math",
                context=(tr_lines[ln - 1].strip()[:120]
                         if 0 < ln <= len(tr_lines) else ""),
            ))

        # F2-12 (2026-09-29, kulvar Q; Vq-5): cümle ortasına yazılmış
        # '$$…$$' TeX'te display matematiktir — paragraf bölünür, ifade
        # ortalanmış ayrı satıra düşer ("Toplam parçacık sayısı $$n_{tot}$$
        # değeri..."). MT ve el çevirisi satır içi '$…$'yi böyle
        # bozabiliyordu; hiçbir denetim görmüyordu. Kaynakta da AYNI ifade
        # cümle ortasında '$$' ise (yazarın tercihi) raporlanmaz.
        self._check_mid_sentence_display_math(
            tr_text, en_text, tr_lines, report)

        if not en_text:
            logger.debug("No source body -- skipping formula integrity check.")
            return

        # 2026-09-24 (G2): display-math blokları ORTAM ADINDAN BAĞIMSIZ,
        # toplam olarak karşılaştırılır. Sanitizer B22 uzun \[...\]'yi
        # multline*/align*'a, B24 çok satırlı equation*'ı align*'a BİLEREK
        # çevirir (1 blok -> 1 blok); ortam adı bazlı sayım bu dönüşümleri
        # "equation eksik / multline fazla" diye raporluyordu (bir kitapta 4 sahte
        # HIGH). Gerçek kayıp/fazlalık toplamı değiştirir ve yakalanır;
        # dökümü mesajda kalır.
        en_blocks = self._count_display_blocks(en_text)
        tr_blocks = self._count_display_blocks(tr_text)
        en_total = sum(en_blocks.values())
        tr_total = sum(tr_blocks.values())
        if en_total != tr_total:
            diff = en_total - tr_total
            severity = "critical" if abs(diff) > 3 else "high"
            # Sayısı değişen ilk ortamın ilk satırı (yaklaşık konum)
            line_num = 1
            for env_name in self._DISPLAY_ENV_NAMES:
                if en_blocks[env_name] != tr_blocks[env_name]:
                    line_num = self._find_first_env_line(
                        tr_lines, env_name,
                    ) or 1
                    break

            def _fmt(blocks: Dict[str, int]) -> str:
                return ", ".join(
                    f"{k}={v}" for k, v in blocks.items() if v
                ) or "0"

            report.issues.append(QCIssue(
                check_name="formula_integrity",
                severity=severity,
                line_num=line_num,
                message=(
                    f"Display math block count mismatch: "
                    f"EN={en_total}, TR={tr_total} (diff={diff:+d})"
                ),
                auto_fixable=False,
                confidence=0.95,
                fix_suggestion=(
                    "Check for missing/extra display-math blocks"
                ),
                context=f"EN: {_fmt(en_blocks)} | TR: {_fmt(tr_blocks)}",
            ))

        # Count inline math $...$
        # 2026-09-24: sanitizer metin modundaki Yunan harfini/eksik-glif
        # sembolünü ($\theta$, $\gg$) YALNIZ çeviride sarar; kaynağa aynı
        # dönüşüm uygulanmadan sayılınca bu sarmalamalar "fazladan inline
        # math" görünüyordu (bir kitapta EN=118, TR=128). İki tarafa aynı kural.
        from latex_pipeline.sanitizer import apply_text_mode_math_wraps
        en_dollars = self._count_inline_math(
            apply_text_mode_math_wraps(en_text))
        tr_dollars = self._count_inline_math(
            apply_text_mode_math_wraps(tr_text))
        if en_dollars != tr_dollars:
            diff = en_dollars - tr_dollars
            # Inline math differences are less severe (MT often restructures).
            # A difference of 1-2 is within noise (MT merging/splitting
            # adjacent math expressions), so we use LOW severity.
            severity = (
                "critical" if abs(diff) > 20 else
                "high" if abs(diff) > 10 else
                "medium" if abs(diff) > 2 else
                "low"
            )
            report.issues.append(QCIssue(
                check_name="inline_math_loss",
                severity=severity,
                line_num=1,
                message=(
                    f"Inline math $ count mismatch: "
                    f"EN={en_dollars}, TR={tr_dollars} (diff={diff:+d})"
                ),
                auto_fixable=True,
                confidence=0.90,
                fix_suggestion=(
                    "Restore lost inline math from EN source by "
                    "line-by-line comparison"
                ),
                context=f"en={en_dollars},tr={tr_dollars}",
            ))

        # Compare \tag{} values
        en_tags = set(re.findall(r"\\tag\{([^}]+)\}", en_text))
        tr_tags = set(re.findall(r"\\tag\{([^}]+)\}", tr_text))

        missing_tags = en_tags - tr_tags
        extra_tags = tr_tags - en_tags

        if missing_tags:
            report.issues.append(QCIssue(
                check_name="formula_integrity",
                severity="critical",
                line_num=1,
                message=(
                    f"Missing equation tags in TR: "
                    f"{', '.join(sorted(missing_tags)[:10])}"
                    + (f" (+{len(missing_tags)-10} more)"
                       if len(missing_tags) > 10 else "")
                ),
                auto_fixable=False,
                confidence=0.95,
                fix_suggestion="Restore missing \\tag{} from EN source",
                context="",
            ))

        if extra_tags:
            report.issues.append(QCIssue(
                check_name="formula_integrity",
                severity="medium",
                line_num=1,
                message=(
                    f"Extra equation tags in TR not in EN: "
                    f"{', '.join(sorted(extra_tags)[:10])}"
                ),
                auto_fixable=False,
                confidence=0.80,
                fix_suggestion="Verify extra \\tag{} values are intentional",
                context="",
            ))

    # Satırda düzyazı kelimesi (en az iki harf; tek harf değişken sayılmaz).
    _PROSE_WORD_RE = re.compile(r"[^\W\d_]{2,}")
    # Cümle ortasındaki display matematik bulgusu en fazla bu kadar satır
    # için ayrı yazılır (unterminated '$$' bulgusuyla aynı sınır).
    _MID_DISPLAY_MAX_ISSUES = 5

    @classmethod
    def _mid_sentence_display_math(cls, text: str) -> List[Tuple[int, str]]:
        """Aynı satırda iki yanında düzyazı olan tek satırlık ``$$…$$``
        bölgeleri: ``(satır, boşluksuz içerik)``.

        Bölgeler ``sanitizer.dollar_math_spans`` ile TeX gibi okunur
        (bitişik iki inline atomun ortasındaki '$$' display sayılmaz).
        Yanlarda yalnız komut, satır içi matematik ya da noktalama varsa
        (``$$a$$ \\label{…}``, satır sonu) cümle ortası sayılmaz.
        """
        if not text or "$$" not in text:
            return []
        from latex_pipeline.sanitizer import dollar_math_spans

        def _prose(part: str) -> bool:
            part = _INLINE_BLOCK_RE.sub(" ", part)
            part = _LATEX_CMD_RE.sub(" ", part)
            return bool(cls._PROSE_WORD_RE.search(part))

        out: List[Tuple[int, str]] = []
        for start, end, display in dollar_math_spans(text):
            if not display:
                continue
            body = text[start:end]
            if "\n" in body:
                continue
            ls = text.rfind("\n", 0, start) + 1
            le = text.find("\n", end)
            le = len(text) if le < 0 else le
            before = text[ls:start]
            if before.lstrip().startswith("%"):
                continue
            after = re.sub(r"(?<!\\)%.*", "", text[end:le])
            if _prose(before) and _prose(after):
                out.append((text.count("\n", 0, start) + 1,
                            re.sub(r"\s+", "", body[2:-2])))
        return out

    def _check_mid_sentence_display_math(
        self,
        tr_text: str,
        en_text: str,
        tr_lines: List[str],
        report: QCReport,
    ) -> None:
        """Cümle ortasındaki ``$$…$$`` (satır içinde display) — F2-12."""
        hits = self._mid_sentence_display_math(tr_text)
        if not hits:
            return
        code_lines = self._find_code_env_lines(tr_lines)
        src_exprs = {expr for _, expr in
                     self._mid_sentence_display_math(en_text or "")}
        hits = [(ln, expr) for ln, expr in hits
                if ln not in code_lines and expr not in src_exprs]
        total = len(hits)
        for ln, _expr in hits[:self._MID_DISPLAY_MAX_ISSUES]:
            more = (f" ({total} occurrences in total)"
                    if total > self._MID_DISPLAY_MAX_ISSUES else "")
            report.issues.append(QCIssue(
                check_name="formula_integrity",
                severity="medium",
                line_num=ln,
                message=(
                    f"Display math $$...$$ inside a sentence at L{ln}: the "
                    f"paragraph breaks around a centered equation{more}"
                ),
                auto_fixable=False,
                confidence=0.90,
                fix_suggestion=(
                    "Write the expression as inline math $...$ (as in the "
                    "source) or move it to its own display line"
                ),
                context=(tr_lines[ln - 1].strip()[:120]
                         if 0 < ln <= len(tr_lines) else ""),
            ))

    def _count_inline_math(self, text: str) -> int:
        """Count inline math $...$ occurrences, excluding display math."""
        return self._scan_dollar_math(text)[0]

    # Sayım dışı bırakılan display blokları. Silinen blok yerine "\x00"
    # konur (boşluk DEĞİL): blok çevresinde sahte "boş satır" (paragraf
    # sonu) ya da sahte bitişiklik oluşmasın (kulvar Q).
    _MATH_ENV_BLOCK_RE = re.compile(
        r"\\begin\{(?:equation|align|gather|multline|split|"
        r"flalign|alignat)\*?\}.*?\\end\{(?:equation|align|gather|"
        r"multline|split|flalign|alignat)\*?\}",
        re.DOTALL,
    )
    _BRACKET_BLOCK_RE = re.compile(r"\\\[.+?\\\]", re.DOTALL)
    # Boş tabanlı üst/alt simge atomu: "$^{x}$", "${}^{x}$", "$_{i}$".
    _SCRIPT_ATOM_START_RE = re.compile(r"[ \t]*(?:\{[ \t]*\}[ \t]*)?[\^_]")

    @classmethod
    def _script_atom_joins(cls, temp: str, open_pos: int,
                           last_close: int) -> bool:
        """``open_pos``'taki '$' ile açılan atom, bir önceki inline atoma
        bitişik (arada yalnız boşluk/sekme) boş tabanlı simge atomu mu?"""
        if last_close < 0 or open_pos < last_close:
            return False
        if temp[last_close:open_pos].strip(" \t"):
            return False
        return bool(cls._SCRIPT_ATOM_START_RE.match(temp, open_pos + 1))

    @classmethod
    def _scan_dollar_math(cls, text: str,
                          keep_lines: bool = False) -> Tuple[int, List[int]]:
        """'$' matematiğini TeX gibi soldan tara: (inline sayısı, kapanmamış
        '$$' display bloklarının açıldığı satırlar).

        2026-09-24: '$' eşlemesi TeX gibi SOLDAN TARANIR. Eski desen
        ("$$...$$" DOTALL silme + bağımsız '$...$' eşleme) bitişik inline
        math'te ("$x$$\\theta$" — sanitizer'ın metin-modu Yunan harfi
        sarmalaması bunu üretir) ortadaki "$$"yi display açılışı sanıp
        belgenin ilerisindeki ilk "$$"ye kadar her şeyi siliyordu
        (bir test kitabında sarmalama sonrası kaynak sayımı 650 -> 449 düşüyordu).
        TeX'te inline math içindeki '$' HER ZAMAN kapatır; "$$" yalnız
        metin kipinde display açar. Kapanmayan inline satır sonunda
        bırakılır (eski desen de tek satırla sınırlıydı).

        2026-09-25 (kulvar Q), iki kural daha (dil_sent_de EN=47 TR=16,
        dil_sent_it EN=55 TR=48; çeviri 47=47 ve 55=55 korumuştu):
          * Paragraf sonu (boş satır) kapanmamış '$$' display'i bitirir —
            TeX de orada "Display math should end with $$" verip kurtarır.
            Eskiden sanitizer'ın bozduğu TEK bir "$$^{^1}" belgenin geri
            kalanındaki bütün inline math'i yutuyordu. Kapanmamış blok
            ayrıca raporlanır (``keep_lines=True`` ile satır numarası).
          * Önceki inline atoma bitişik boş tabanlı simge atomu ("$\\zeta$
            ${}^{2}$", "$\\omega$${}^{n}$") o atomla BİR ifade sayılır.
            Autofix bu parçaları birleştiriyor ("$\\zeta^{2}$"), kaynak
            birleşmiyordu: iki tarafa aynı kural uygulanmazsa her birleştirme
            sahte "inline math kaybı" oluyordu.
        """
        if keep_lines:
            def _blank(m: "re.Match") -> str:
                return re.sub(r"[^\n]", "\x00", m.group(0))
        else:
            def _blank(m: "re.Match") -> str:
                return "\x00"
        temp = cls._BRACKET_BLOCK_RE.sub(_blank, text)
        temp = cls._MATH_ENV_BLOCK_RE.sub(_blank, temp)

        count = 0
        mode = ""          # "" metin, "i" inline, "d" display
        last_close = -1    # son kapanan inline '$'ın hemen sonrası
        merge = False      # açık atom öncekiyle birleşik mi (sayılmaz)
        prev_nl = -1
        d_open = -1
        unterminated: List[int] = []
        for m in _MATH_DOLLAR_TOKEN_RE.finditer(temp):
            tok = m.group(0)
            pos = m.start()
            if tok[0] == "\\":
                continue                     # kaçışlı karakter (\$ dahil)
            if tok == "\n":
                if mode == "i":
                    mode = ""
                    merge = False
                elif (mode == "d" and prev_nl >= 0
                        and not temp[prev_nl + 1:pos].strip()):
                    unterminated.append(temp.count("\n", 0, d_open) + 1)
                    mode = ""
                prev_nl = pos
            elif mode == "i":
                if not merge:
                    count += 1               # '$' ya da '$$'nin ilk '$'ı kapatır
                merge = False
                last_close = pos + 1
                # '$$'nin ikinci '$'ı yeni inline açar ("$x$$\theta$")
                if tok == "$$":
                    mode = "i"
                    merge = cls._script_atom_joins(temp, pos + 1, last_close)
                else:
                    mode = ""
            elif tok == "$$":
                if mode == "d":
                    mode = ""
                else:
                    mode = "d"
                    d_open = pos
            elif mode == "":
                mode = "i"
                merge = cls._script_atom_joins(temp, pos, last_close)
        if mode == "d":
            unterminated.append(temp.count("\n", 0, d_open) + 1)
        return count, unterminated

    # Display-math blok türleri (formula_integrity toplam sayımı). "display"
    # = \[...\]; kaçışlı referans "\[4\]" sayılmaz (yalnız rakam içerik).
    _DISPLAY_ENV_NAMES: Tuple[str, ...] = (
        "equation", "align", "gather", "multline", "flalign", "alignat",
        "eqnarray", "display",
    )
    _DISPLAY_BRACKET_RE = re.compile(r"\\\[(?!\s*\d+\s*\\\])")

    def _count_display_blocks(self, text: str) -> Dict[str, int]:
        """Display-math blok sayıları, tür bazında (``*``'lı biçimler dahil)."""
        counts: Dict[str, int] = {}
        for env_name in self._DISPLAY_ENV_NAMES:
            if env_name == "display":
                counts[env_name] = len(self._DISPLAY_BRACKET_RE.findall(text))
            else:
                counts[env_name] = len(re.findall(
                    r"\\begin\{" + env_name + r"\*?\}", text))
        return counts

    def _find_first_env_line(
        self, lines: List[str], env_name: str,
    ) -> Optional[int]:
        """Find the first line containing a given environment."""
        if env_name == "display":
            for i, line in enumerate(lines, 1):
                if self._DISPLAY_BRACKET_RE.search(line):
                    return i
            return None
        pattern = re.compile(rf"\\begin\{{{env_name}")
        for i, line in enumerate(lines, 1):
            if pattern.search(line):
                return i
        return None

    # ------------------------------------------------------------------
    # Check 2: Untranslated Block Detection
    # ------------------------------------------------------------------

    def _check_untranslated_blocks(
        self,
        tr_text: str,
        tr_lines: List[str],
        report: QCReport,
    ) -> None:
        """Find blocks of source-language text that weren't translated.

        Algorithm:
        1. Detect bibliography/references sections and skip them entirely.
        2. Mask LaTeX commands and math environments (they stay English).
        3. Split remaining text into sentences.
        4. For each sentence > 20 chars: check if it's English using
           a character heuristic -- Turkish has unique chars (ş, ğ, ı, ö, ü, ç)
           that English doesn't; if a sentence has 0 Turkish chars and >70%
           ASCII letters, it's likely untranslated.
        5. 2+ consecutive English sentences = CRITICAL
           Single English sentence = MEDIUM

        Çok dilli kaynak (2026-09-24): kaynak dil İngilizce dışında da
        olabildiği için iki tespit daha aynı "untranslated" akışına girer
        (aynı ardışık-blok/uzunluk önem dereceleriyle):
        6. Latin dışı alfabe (Kiril, Yunan, Arap, İbrani, CJK): satırda en az
           4 kelime ve harflerin en az yarısı o alfabede ise satır çevrilmemiş
           sayılır; pay düşük ama 8+ kelime varsa "mixed_language_line".
           Tek Yunan harfi (α), tek kısaltma (ЖРД), kaynakça/künye satırı ve
           başlıklar (Check 11) muaftır. Matematik tüm metin üzerinde
           maskelenir (çok satırlı denklemlerdeki \\text{пост} gibi Kiril
           indisler sayılmaz).
        7. Latin alfabeli Avrupa dilleri (de/fr/es/it/pt): İngilizce sayılmayan
           satırda Avrupa işlev kelimeleri baskınsa satır çevrilmemiş sayılır.
           Almanca ü/ö ve Fransızca ç Türkçe harf sayıldığı için bu satırlar
           eskiden "saf Türkçe" diye atlanıyordu.
        Eşikler ve alfabe tanımları ``core.lang_detect``'te (tek kaynak).
        """
        ld = _lang_detect()

        # Build a set of line indices that are inside bibliography/references
        bib_lines = self._find_bibliography_lines(tr_lines)

        # Build a set of line indices inside code environments
        code_lines = self._find_code_env_lines(tr_lines)

        # Build a set of line indices inside tabular environments
        # Tables must stay untranslated (proje kuralı: tablo içeriği korunur)
        tabular_lines = self._find_tabular_lines(tr_lines)

        # Kaynak-dil taraması için metin BİR KEZ temizlenir: denklem
        # ortamları ve \[ ... \] blokları çok satırlı olabildiğinden satır
        # satır maskeleme yetmez (\[ ... T_{ad} ... \] içindeki "ad"
        # İtalyanca sanılıyordu). Temizlik satır hizasını korur (~70 ms /
        # 1,5 MB gövde). F2-03: bağlantı belirteçleri (URL/DOI/arXiv) önce
        # maskelenir (satır sonlarına dokunulmaz).
        src_lines: Optional[List[str]] = _clean_for_language(
            _URL_TOKEN_RE.sub(" ", tr_text)).split("\n")
        if len(src_lines) != len(tr_lines):  # pragma: no cover - hiza korunur
            logger.debug("Kaynak-dil maskesi satır hizasını bozdu; "
                         "çok dilli kaynak taraması atlandı.")
            src_lines = None
        has_non_latin = bool(ld.NON_LATIN_CHAR_RE.search(tr_text))

        # Process line by line, tracking consecutive untranslated lines.
        # Öğe: (line_num, text) = İngilizce, (line_num, text, dil_kodu) =
        # başka kaynak dil.
        consecutive_english: List[Tuple] = []

        for line_idx, raw_line in enumerate(tr_lines, 1):
            # Skip bibliography/references lines entirely
            if line_idx in bib_lines:
                self._flush_english_streak(consecutive_english, report)
                consecutive_english = []
                continue

            # Skip code environment lines (lstlisting, verbatim, minted)
            if line_idx in code_lines:
                self._flush_english_streak(consecutive_english, report)
                consecutive_english = []
                continue

            # Skip tabular environment lines (tables stay in English)
            if line_idx in tabular_lines:
                self._flush_english_streak(consecutive_english, report)
                consecutive_english = []
                continue

            # Skip empty lines, comments, pure LaTeX commands
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("%"):
                self._flush_english_streak(consecutive_english, report)
                consecutive_english = []
                continue

            # F2-03 (2026-09-29, kulvar Q): bağlantı (URL, DOI, arXiv) satırı
            # artık dil denetiminden MUAF TUTMAZ. Eskiden bağlantılı dipnotu
            # ya da "(arXiv:…)" atfı olan çevrilmemiş paragraf hiç
            # görünmüyordu. Yalnız belirteç maskelenir, satırın geri kalanı
            # denetlenir. Eskisi gibi muaf kalanlar: künye satırı (yazar-yıl,
            # cilt(sayı):sayfa, web/gazete künyesi) ve bağlantı dışındaki
            # düzyazısı kısa bağlantı satırı ("Web sitemizi <URL> adresinden
            # ziyaret edin.", boşlukla bölünmüş adres parçası).
            work = stripped
            had_url = _URL_TOKEN_RE.search(stripped) is not None
            if had_url:
                work = _URL_TOKEN_RE.sub(" ", stripped)
                if (ld.looks_like_citation(work)
                        or _looks_like_reference(work)
                        or _looks_like_web_reference(work)
                        or len(self._mask_for_language_check(
                            _PROSE_ARG_CMD_RE.sub(" ", work)))
                        < LINK_LINE_MIN_PROSE_CHARS):
                    self._flush_english_streak(consecutive_english, report)
                    consecutive_english = []
                    continue

            # Skip lines that are known-English content (künye, e-posta...)
            # (2026-09-24: latex-only kontrolünden ÖNCEYE alındı — ikisi de
            # akışı kesip atladığı için sıra İngilizce davranışını
            # değiştirmez; Latin dışı alfabe taraması künye/e-posta
            # satırlarını da atlasın diye bu kontrolden sonra gelir.)
            if self._is_known_english_content(work):
                self._flush_english_streak(consecutive_english, report)
                consecutive_english = []
                continue

            # F2-02 (2026-09-29, kulvar Q): satırın dış metni ve düzyazı
            # komutlarının argümanları (dipnot, altyazı, italik...) satır
            # düzeyi karar "çevrilmemiş" demediğinde AYRI birim olarak
            # sınıflanır (bkz. _prose_unit_verdict). Başlıklar Check 11'in.
            units_ok = (not _HEADING_CMD_LINE_RE.match(work)
                        and _PROSE_ARG_CMD_RE.search(work) is not None)

            # Latin dışı alfabe (Kiril, Yunan, Arap, İbrani, CJK). Metin
            # argümanları (\textbf{...}, \caption{...}, \footnote{...})
            # temizlikte korunduğu için latex-only elemesinden ÖNCE bakılır;
            # başlık satırları Check 11'e bırakılır.
            if (src_lines is not None and has_non_latin
                    and ld.NON_LATIN_CHAR_RE.search(work)
                    and not _HEADING_CMD_LINE_RE.match(work)
                    and not ld.looks_like_citation(work)):
                verdict = ld.classify_script_line(src_lines[line_idx - 1])
                if verdict is not None and verdict.kind == "block":
                    # Özgün dilinde bırakılmış üçüncü dil alıntısı (kulvar Q).
                    if self._is_source_foreign_quote(raw_line, verdict.code):
                        self._foreign_quote_lines.add(line_idx)
                        self._flush_english_streak(consecutive_english, report)
                        consecutive_english = []
                        continue
                    consecutive_english.append(
                        (line_idx, stripped, verdict.code))
                    continue
                if verdict is not None:  # kind == "mixed"
                    unit = (self._prose_unit_verdict(work, web_ref=had_url)
                            if units_ok else None)
                    if unit is not None and unit[0] == "line":
                        self._apply_prose_unit(unit, line_idx, stripped,
                                               consecutive_english, report)
                        continue
                    self._flush_english_streak(consecutive_english, report)
                    consecutive_english = []
                    self._report_mixed_script_line(
                        line_idx, stripped, verdict, report)
                    self._apply_prose_unit(unit, line_idx, stripped,
                                           consecutive_english, report)
                    continue

            # Skip lines that are only LaTeX structure (F2-02: düzyazı
            # komutunun argümanı yine de ayrı birim olarak sınıflanır —
            # "\textit{<paragraf>}", "\caption{<altyazı>}" satırları).
            if self._is_latex_only_line(work):
                unit = (self._prose_unit_verdict(work, web_ref=had_url)
                        if units_ok else None)
                self._apply_prose_unit(unit, line_idx, stripped,
                                       consecutive_english, report)
                continue

            # Check for Turkish chars (line-level and LaTeX-encoded)
            has_turkish = (
                any(c in work for c in _TURKISH_CHARS)
                or bool(re.search(
                    r'\\c\{[csCsS]\}|\\"\{[oOuU]\}|\{\\i\}|\\i\b',
                    work,
                ))
            )

            # Mask protected content
            masked = self._mask_for_language_check(work)

            # Split into sentences (rough: split on . ? ! followed by space)
            sentences = re.split(r"(?<=[.?!])\s+", masked)

            line_has_english = False
            for sent in sentences:
                sent = sent.strip()
                if len(sent) < 20:
                    continue

                if self._is_likely_english(sent):
                    line_has_english = True
                    break

            # Latin alfabeli Avrupa dili (de/fr/es/it/pt). İngilizce
            # tespitinden SONRA ve yalnız İngilizce değilse bakılır — mevcut
            # İngilizce akışı aynen kalır. ü/ö/ç içeren Almanca/Fransızca
            # satır has_turkish=True olduğundan eskiden "saf Türkçe" sayılıp
            # atlanıyordu; Türkçeye özgü harf (ş, ğ, ı, İ) varsa
            # classify_latin_line zaten None döner.
            eu_lang = None
            if src_lines is not None and not ld.looks_like_citation(work):
                eu_lang = ld.classify_latin_line(src_lines[line_idx - 1])
            # Sınıflandırıcı satırı bir Avrupa diline atfettiyse (işlev
            # kelimeleri İngilizce+Türkçeye baskın) satır çevrilmemiştir;
            # "karışık Türkçe/İngilizce" dalına düşmemeli. Almanca satırda
            # ü/ö has_turkish'i, umlautsuz bir cümle de _is_likely_english'i
            # tetikliyordu: bütün Almanca paragraf yalnız MEDIUM
            # mixed_language_line alıyordu (dil teyidi, 25 Eylül 2026).
            if eu_lang is not None:
                # 2026-09-25 (kulvar Q): kaynakta da bu dilde AYNEN geçen
                # alıntı (İtalyanca kitapta Fransızca alıntı) çevrilmemiş
                # sayılmaz; bkz. _is_source_foreign_quote. 2026-09-30
                # (kulvar F2): sınıflandırıcı Latince satırı Roman diline
                # atfedebilir ("se", "non"); kaynakta aynen geçen Latince
                # alıntı da muaftır (bkz. _is_source_latin_quote).
                if (self._is_source_foreign_quote(raw_line, eu_lang)
                        or self._is_source_latin_quote(raw_line)):
                    self._foreign_quote_lines.add(line_idx)
                    self._flush_english_streak(consecutive_english, report)
                    consecutive_english = []
                    continue
                consecutive_english.append((line_idx, stripped, eu_lang))
                continue

            # If line has Turkish chars but also long English blocks,
            # flag as mixed-language (MEDIUM severity)
            if has_turkish and line_has_english:
                # F2-02: Türkçe harfler yalnız argümandaysa (dipnottaki
                # çevirmen notu, bağlantı metni) satırın dış metni bütünüyle
                # çevrilmemiş olabilir: o zaman karışık değil, çevrilmemiş.
                unit = (self._prose_unit_verdict(work, web_ref=had_url)
                        if units_ok else None)
                if unit is not None and unit[0] == "line":
                    self._apply_prose_unit(unit, line_idx, stripped,
                                           consecutive_english, report)
                    continue
                # Count English words vs total words to assess severity
                en_words = sum(
                    1 for w in masked.split()
                    if len(w) > 2 and w.isascii() and w.isalpha()
                )
                total_words = max(1, len(masked.split()))
                en_ratio = en_words / total_words

                if en_ratio > 0.3 and en_words > 8:
                    self._mixed_language_lines.add(line_idx)
                    # 5 Ekim 2026: önem derecesi satırdaki çevrilmemiş
                    # İngilizce cümle miktarıyla ölçeklenir (çevrilmemiş
                    # satırla aynı eşikler). Eskiden hep MEDIUM'du; yarısı
                    # çevrilmemiş paragrafı eşiğin altına indiren, aynı
                    # cümlelerdeki her artikelin ayrıca sayılmasıydı (Check 34
                    # raporlanmış satırda artık saymıyor).
                    _en_chars = sum(
                        len(s.strip()) for s in sentences
                        if len(s.strip()) >= 20
                        and self._is_likely_english(s.strip()))
                    if _en_chars >= UNTRANSLATED_PARAGRAPH_CHARS:
                        _sev = "critical"
                    elif _en_chars >= UNTRANSLATED_SENTENCE_CHARS:
                        _sev = "high"
                    else:
                        _sev = "medium"
                    report.issues.append(QCIssue(
                        check_name="mixed_language_line",
                        severity=_sev,
                        line_num=line_idx,
                        message=(
                            f"Line {line_idx}: Mixed Turkish/English text "
                            f"({en_words} English words, {en_ratio:.0%} ratio). "
                            f"Partial translation suspected."
                        ),
                        auto_fixable=True,
                        confidence=0.90,
                        fix_suggestion="Re-translate English sentences in this line",
                        context=stripped[:100],
                    ))
                # Don't count as consecutive English streak
                self._apply_prose_unit(unit, line_idx, stripped,
                                       consecutive_english, report)
            # 2026-09-28 (kulvar Q): İngilizce satır için alıntı muafiyeti
            # YOK. _is_likely_english karma satırda zayıf: İtalyanca kaynağın
            # çevrilmemiş bir satırı içindeki kısa İngilizce eser adı ("...e
            # Davide Brewster (More Worlds than one) i quali collocarono")
            # yüzünden satırı İngilizce sayıyor, satır da kaynakta birebir
            # geçtiği için muaf tutuluyordu (iki arayüz denemesinde:
            # çevrilmemiş İtalyanca satır bloktan düşüyordu). Derlemde bu
            # dalın yararlı tek kullanımı yoktu.
            # 2026-09-30 (kulvar F2, MT-1): TEK istisna dil tespitiyle
            # İngilizce OLMADIĞI gösterilen satır: profilsiz Latince alıntı
            # (Kepler/Newton), kaynakta aynen geçiyorsa. "Kaynakta birebir"
            # tek başına yetmez (çevrilmemiş İngilizce satır da birebirdir);
            # İtalyanca satır is_latin_text'ten geçmez (rakip dil baskın).
            elif line_has_english and self._is_source_latin_quote(raw_line):
                self._foreign_quote_lines.add(line_idx)
                self._flush_english_streak(consecutive_english, report)
                consecutive_english = []
            elif line_has_english:
                # İngilizce sanılan satır aslında aksansız bir Avrupa dili
                # olabilir (İtalyanca/İspanyolca ASCII kelimeler); tespit
                # AYNI kalır, yalnız mesajdaki dil adı düzeltilir.
                consecutive_english.append(
                    (line_idx, stripped, eu_lang) if eu_lang
                    else (line_idx, stripped))
            else:
                # Türkçe satır (ya da karar için yetersiz): F2-02 argüman
                # birimleri (Türkçe cümlenin dipnotunda kalan paragraf).
                unit = (self._prose_unit_verdict(work, web_ref=had_url)
                        if units_ok else None)
                self._apply_prose_unit(unit, line_idx, stripped,
                                       consecutive_english, report)

        # Flush any remaining streak at end of file
        self._flush_english_streak(consecutive_english, report)

    def _find_bibliography_lines(self, lines: List[str]) -> Set[int]:
        """Find all line numbers (1-based) inside bibliography/references sections.

        Detects:
        - \\begin{thebibliography} ... \\end{thebibliography}
        - \\chapter*{References} / \\section*{References} / \\chapter*{Bibliography}
          until end of file or next \\chapter/\\section
        - \\bibitem entries anywhere
        """
        bib_lines: Set[int] = set()
        in_bib_env = False
        in_bib_section = False
        bib_level = 0
        # G2a-2 (2026-10-05): çevirmen kaynakçayı her başlık düzeyinde
        # (dönüştürücü bölüm sonu kaynakçasını \subsubsection*'a indirebiliyor)
        # ve numaralı başlıkta ("5 Kaynakça") korur, başlığı çevirir; girdiler
        # kaynak dilde kalır. Aynı kural: translator.bibliography_heading_key.
        from latex_pipeline.translator import bibliography_heading_key
        heading_re = re.compile(
            r"\\(part|chapter|section|subsection|subsubsection)\*?\s*"
            r"(?:\[[^\]]*\])?\s*\{([^{}]*)\}")
        levels = {"part": -1, "chapter": 0, "section": 1, "subsection": 2,
                  "subsubsection": 3}

        for idx, line in enumerate(lines, 1):
            stripped = line.strip()

            # thebibliography environment
            if r"\begin{thebibliography}" in stripped:
                in_bib_env = True
            if in_bib_env:
                bib_lines.add(idx)
                if r"\end{thebibliography}" in stripped:
                    in_bib_env = False
                continue

            # References/Bibliography chapter/section heading.
            # 2026-09-24: kaynak dil İngilizce dışında da olabildiğinden
            # (Yol A) çevrilmeden bırakılmış kaynakça başlıkları da tanınır —
            # Rusça/Almanca kaynakça künyeleri meşrudur, "çevrilmemiş" değil.
            if re.search(
                r"\\(?:chapter|section)\*?\{(?:References|Bibliography|"
                r"Kaynakça|Kaynaklar|Referanslar|"
                r"Литература|Список\s+литературы|Библиография|"
                r"Библиографический\s+список|"
                r"Literatur|Literaturverzeichnis|Bibliographie|Références|"
                r"Bibliografía|Referencias|Bibliografia|"
                r"Riferimenti\s+bibliografici|Referências)\}",
                stripped, re.IGNORECASE,
            ):
                in_bib_section = True
                hm = heading_re.match(stripped)
                bib_level = levels[hm.group(1)] if hm else 0
                bib_lines.add(idx)
                continue
            hm = heading_re.match(stripped)
            if hm and bibliography_heading_key(hm.group(2)):
                in_bib_section = True
                bib_level = levels[hm.group(1)]
                bib_lines.add(idx)
                continue

            # End of bibliography section: next heading of the same or a
            # higher level (G2a-2: düzeye duyarlı; kaynakçanın kendi alt
            # başlıkları bölgeyi bölmez)
            if in_bib_section:
                if ((hm and levels[hm.group(1)] <= bib_level)
                        or re.match(r"\\(?:appendix|backmatter)\b", stripped)):
                    in_bib_section = False
                else:
                    bib_lines.add(idx)
                    continue

            # Individual \bibitem entries (even outside formal bib env)
            if stripped.startswith(r"\bibitem"):
                bib_lines.add(idx)

        return bib_lines

    def _find_code_env_lines(self, lines: List[str]) -> Set[int]:
        """Find all line numbers (1-based) inside code environments.

        Detects lstlisting, verbatim, minted, and algorithmic environments.
        Code should always stay in English.
        """
        code_lines: Set[int] = set()
        in_code = False
        end_pattern = ""

        for idx, line in enumerate(lines, 1):
            stripped = line.strip()

            if not in_code:
                m = re.match(
                    r"\\begin\{(lstlisting|verbatim|minted|"
                    r"algorithmic|algorithm|Verbatim)\}",
                    stripped,
                )
                if m:
                    in_code = True
                    end_pattern = r"\end{" + m.group(1) + "}"
                    code_lines.add(idx)
                    continue
            else:
                code_lines.add(idx)
                if end_pattern in stripped:
                    in_code = False
                    end_pattern = ""

        return code_lines

    @staticmethod
    def _find_tabular_lines(lines: List[str]) -> Set[int]:
        """Find all line numbers (1-based) inside tabular environments.

        Tables (tabular, tabularx, longtable) should not be translated
        per the project rule (tables are preserved), so their content must be excluded from the
        untranslated-text check.
        """
        tabular_lines: Set[int] = set()
        in_tabular = False
        for idx, line in enumerate(lines, 1):
            stripped = line.strip()
            if not in_tabular:
                if _TABULAR_BEGIN_RE.search(stripped):
                    in_tabular = True
                    tabular_lines.add(idx)
            else:
                tabular_lines.add(idx)
                if _TABULAR_END_RE.search(stripped):
                    in_tabular = False
        return tabular_lines

    def _is_known_english_content(self, line: str) -> bool:
        """Check if a line contains content that should stay in English.

        Returns True for:
        - \\bibitem and citation entries
        - Lines that are primarily proper nouns (author names in headers)
        - Code/verbatim environments
        - \\item entries that look like bibliography references

        F2-03 (2026-09-29, kulvar Q): URL/DOI/arXiv geçen satır artık
        BÜTÜNÜYLE muaf değildir; çağıran belirteci maskeler
        (``_URL_TOKEN_RE``) ve yalnız bağlantılı künye satırını atlar.
        """
        stripped = line.strip()

        # \bibitem entries
        if stripped.startswith(r"\bibitem"):
            return True

        # \item with citation-like content: author names, years, journal refs
        if stripped.startswith(r"\item") and re.search(
            r"\b\d{4}\b.*(?:pp\.|pages|vol\.|journal|proceedings|"
            r"springer|wiley|elsevier|ieee|acm|press|publisher)",
            stripped, re.IGNORECASE,
        ):
            return True

        # Lines inside lstlisting/verbatim/minted (code stays English)
        if re.match(r"\\begin\{(?:lstlisting|verbatim|minted)\}", stripped):
            return True

        # Index entries
        if stripped.startswith(r"\index{"):
            return True

        # Copyright / publisher notices.
        # 2026-07-11 (#34): tek zayif kelime muafiyet icin YETERSIZ — govde
        # paragrafinda 'license'/'Springer' gecmesi tum satiri untranslated
        # taramasindan muaf birakiyordu. Guclu isaret tek basina yeter;
        # zayif kelime yil ya da ikinci kelimeyle desteklenmeli.
        if re.search(
            r"©|\ball rights reserved\b|\bisbn[\s:0-9-]|\bissn[\s:0-9-]",
            stripped, re.IGNORECASE,
        ):
            return True
        _weak_pub = re.findall(
            r"\b(?:copyright|springer|elsevier|wiley|open access|"
            r"creative commons|license)\b",
            stripped, re.IGNORECASE,
        )
        if _weak_pub and (
            len(_weak_pub) >= 2 or re.search(r"\b(?:19|20)\d{2}\b", stripped)
        ):
            return True

        # Author/affiliation lines (near beginning of book, contain
        # institution names). 2026-07-11 (#34): yalniz KISA satirlar —
        # 'The regional health survey found...' gibi govde paragrafi
        # bu kaliba takilip muaf kalmasin.
        if len(stripped) < 120 and re.search(
            r"(?:Department|University|Institute|Laboratory|School|College|"
            r"Faculty|Center|Centre)\s+of\b",
            stripped,
        ):
            return True

        # Email addresses
        if re.search(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", stripped):
            return True

        return False

    @staticmethod
    def _report_mixed_script_line(
        line_idx: int,
        stripped: str,
        verdict,
        report: QCReport,
    ) -> None:
        """Türkçe satır içinde Latin dışı alfabeyle kalmış çevrilmemiş cümle.

        İngilizce karma satırla (mixed_language_line) aynı ad ve önem
        derecesi (medium) — Yol A'da rapor-only kalır.
        """
        _ld = _lang_detect()
        _label = _ld.language_name_en(verdict.code)
        _unit = "characters" if verdict.code == "cjk" else "words"
        report.issues.append(QCIssue(
            check_name="mixed_language_line",
            severity="medium",
            line_num=line_idx,
            message=(
                f"Line {line_idx}: Mixed Turkish/{_label} text "
                f"({verdict.words} {_label} {_unit}, {verdict.share:.0%} of "
                f"letters). Partial translation suspected."
            ),
            auto_fixable=True,
            confidence=0.85,
            fix_suggestion=(
                f"Translate the remaining {_label} sentence(s) in this line"
            ),
            context=stripped[:100],
        ))

    def _flush_english_streak(
        self,
        streak: List[Tuple],
        report: QCReport,
    ) -> None:
        """Convert a streak of untranslated lines into QCIssues.

        Öğeler ``(satır, metin)`` (İngilizce) ya da ``(satır, metin,
        dil_kodu)`` (başka kaynak dil, 2026-09-24). Önem dereceleri dilden
        bağımsızdır; İngilizce-dışı bir satır içeren blokta mesaj o dilin
        adını taşır (tamamı İngilizce blokta mesajlar eskisiyle BİREBİR).
        """
        if not streak:
            return

        # 2026-09-25 (kulvar Q): çevrilmemiş sayılan satırlar dil koduyla
        # kaydedilir; foreign_language_leakage kaynağın KENDİ dilindeki
        # aynı satırları ikinci kez saymaz (bkz. _is_untranslated_source_line).
        for item in streak:
            self._untranslated_lines[item[0]] = (
                item[2] if len(item) > 2 else "en")

        first_line = streak[0][0]
        context_text = streak[0][1][:120]

        _foreign = [item[2] for item in streak
                    if len(item) > 2 and item[2] != "en"]
        if _foreign:
            _lang = Counter(_foreign).most_common(1)[0][0]
            _label = _lang_detect().language_name_en(_lang)
            _possibly = f"Possibly untranslated {_label} sentence"
        else:
            _label = "English"
            _possibly = "Possibly untranslated sentence"

        if len(streak) >= 2:
            # 2+ consecutive English lines = CRITICAL
            report.issues.append(QCIssue(
                check_name="untranslated",
                severity="critical",
                line_num=first_line,
                message=(
                    f"Untranslated {_label} block: {len(streak)} consecutive "
                    f"lines starting at L{first_line}"
                ),
                auto_fixable=True,
                confidence=0.90,
                fix_suggestion=(
                    f"Re-translate lines {first_line}-{streak[-1][0]}"
                ),
                context=context_text,
            ))
        else:
            # Tek satirlik streak: MT ciktisinda bir PARAGRAF genelde tek
            # satirdir — 2026-07-11'e kadar koca Ingilizce paragraf yalniz
            # 'medium' aliyordu (critical esigi 2+ ARDISIK satir istiyordu,
            # paragraf-basina-satir formatinda o esik hic tetiklenmiyordu).
            # Uzunluga gore eskale edilir.
            _line_text = streak[0][1]
            if len(_line_text) >= UNTRANSLATED_PARAGRAPH_CHARS:
                _sev = "critical"
                _msg = (f"Untranslated {_label} paragraph (single line, "
                        f"{len(_line_text)} chars) at L{first_line}")
            elif len(_line_text) >= UNTRANSLATED_SENTENCE_CHARS:
                _sev = "high"
                _msg = f"Untranslated {_label} sentence(s) at L{first_line}"
            else:
                _sev = "medium"
                _msg = f"{_possibly} at L{first_line}"
            report.issues.append(QCIssue(
                check_name="untranslated",
                severity=_sev,
                line_num=first_line,
                message=_msg,
                auto_fixable=True,
                confidence=0.85,
                fix_suggestion=f"Re-translate line {first_line}",
                context=context_text,
            ))

    # ------------------------------------------------------------------
    # F2-02 (2026-09-29, kulvar Q): komut argümanlarının dil denetimi
    # ------------------------------------------------------------------

    def _has_prose(self, text: str) -> bool:
        """Metinde (düzyazı komutlarının argümanları DAHİL) düzyazı var mı?

        ``_is_latex_only_line`` komutu argümanıyla sildiğinden önce düzyazı
        komutlarının yalnız ADLARI atılır.
        """
        return not self._is_latex_only_line(_PROSE_ARG_CMD_RE.sub(" ", text))

    def _unit_language(self, text: str,
                       web_ref: bool = False) -> Optional[str]:
        """Tek birimin kaynak dili (tek kaynak: ``unit_source_language``)."""
        return unit_source_language(text, web_ref)

    def _flagged_unit_language(self, text: str,
                               web_ref: bool = False) -> Optional[str]:
        """``_unit_language`` + özgün dilinde bırakılmış üçüncü dil alıntısı
        muafiyeti (satır düzeyindeki ``_is_source_foreign_quote`` kuralı)."""
        lang = self._unit_language(text, web_ref)
        if lang is not None and lang != "en" and \
                self._is_source_foreign_quote(text, lang):
            return None
        return lang

    def _prose_unit_verdict(
        self, text: str, depth: int = 0, web_ref: bool = False,
    ) -> Optional[Tuple[str, str, str, str]]:
        r"""Satırın (ya da bir argümanın) çevrilmemiş birimini bul.

        Satır düzeyi karar "çevrilmemiş" demediğinde çağrılır. Birimler:
        satırın DIŞ metni (düzyazı komutları çıkarılmış) ve her düzyazı
        komutunun argümanı (``prose_command_args``). Dönüş
        ``(tür, komut, metin, dil)`` ya da None:

        * ``"line"``: satırın asıl metni çevrilmemiş — dış metin (Türkçe
          harfleri yalnız bağlantı metninde/çevirmen notunda olan
          Almanca paragraf) ya da satırda başka düzyazı olmayan argüman
          (``\textit{<paragraf>}``, ``\caption{<altyazı>}``). Satır gibi
          sayılır, ardışık blok kuralına girer.
        * ``"arg"``: düzyazılı satırda çevrilmemiş dipnot/altyazı/kenar
          notu argümanı — kendi bulgusu.

        Türkçe cümle içindeki biçim komutu (kitap adı, alıntı) aday
        değildir. Birden çok aday varsa öncelik line > arg, aynı türde en
        uzun metin. Dipnot/altyazı argümanında ve *web_ref* (satırda bağlantı
        vardı) iken web/gazete künyesi muaftır.
        """
        args = prose_command_args(text)
        if not args:
            return None
        found: List[Tuple[str, str, str, str]] = []
        outer = _cut_spans(text, [(a[2], a[3]) for a in args])
        if self._has_prose(outer):
            lang = self._flagged_unit_language(outer, web_ref)
            if lang is not None:
                found.append(("line", "", text, lang))
        for cmd, arg, start, end in args:
            arg_web_ref = web_ref or cmd in PROSE_UNIT_COMMANDS
            lang = self._flagged_unit_language(arg, arg_web_ref)
            seg = arg
            if lang is None:
                if depth + 1 >= PROSE_ARG_MAX_DEPTH:
                    continue
                inner = self._prose_unit_verdict(arg, depth + 1, arg_web_ref)
                if inner is None:
                    continue
                if inner[0] != "line":
                    found.append(inner)
                    continue
                # Argümanın asıl metni çevrilmemiş: argüman bayraklı sayılır.
                lang = inner[3]
            standalone = not self._has_prose(
                text[:start] + " " + text[end:])
            if standalone:
                found.append(("line", cmd, seg, lang))
            elif cmd in PROSE_UNIT_COMMANDS:
                found.append(("arg", cmd, seg, lang))
        if not found:
            return None
        rank = {"line": 0, "arg": 1}
        found.sort(key=lambda f: (rank[f[0]], -len(f[2])))
        return found[0]

    def _apply_prose_unit(
        self,
        unit: Optional[Tuple[str, str, str, str]],
        line_idx: int,
        stripped: str,
        streak: List[Tuple],
        report: QCReport,
    ) -> None:
        """``_prose_unit_verdict`` kararını uygula (Check 2 döngüsü).

        ``"line"`` satırı ardışık bloğa ekler; diğer her durumda blok
        kapatılır (``streak`` yerinde boşaltılır), ``"arg"`` kendi
        "untranslated" bulgusunu üretir.
        """
        if unit is not None and unit[0] == "line":
            lang = unit[3]
            streak.append((line_idx, stripped) if lang == "en"
                          else (line_idx, stripped, lang))
            return
        self._flush_english_streak(streak, report)
        streak.clear()
        if unit is None:
            return
        _kind, cmd, seg, lang = unit
        self._report_untranslated_argument(line_idx, cmd, seg, lang, report)

    def _report_untranslated_argument(
        self, line_idx: int, cmd: str, seg: str, lang: str,
        report: QCReport,
    ) -> None:
        """Düzyazılı satırdaki çevrilmemiş dipnot/altyazı argümanı.

        Önem, tek satırlık çevrilmemiş birimle AYNI uzunluk eşikleriyle.
        Otomatik düzeltme yok: yeniden çeviri bütün satırı (Türkçe kısmıyla
        birlikte) modele gönderirdi.
        """
        code = self._prefer_source_language(lang, seg)
        self._untranslated_lines[line_idx] = code
        label = ("English" if code == "en"
                 else _lang_detect().language_name_en(code))
        text = seg.strip()
        if len(text) >= UNTRANSLATED_PARAGRAPH_CHARS:
            sev = "critical"
            what = f"{label} paragraph"
        elif len(text) >= UNTRANSLATED_SENTENCE_CHARS:
            sev = "high"
            what = f"{label} sentence(s)"
        else:
            sev = "medium"
            what = f"{label} text"
        report.issues.append(QCIssue(
            check_name="untranslated",
            severity=sev,
            line_num=line_idx,
            message=(f"Untranslated {what} in \\{cmd} argument "
                     f"({len(text)} chars) at L{line_idx}"),
            auto_fixable=False,
            confidence=0.85,
            fix_suggestion=(f"Translate the \\{cmd} argument at "
                            f"line {line_idx}"),
            context=text[:120],
        ))

    def _prefer_source_language(self, code: str, text: str) -> str:
        """Akraba Latin dillerinde dil adını kaynağın diline göre düzelt.

        F2-14 (2026-09-29, kulvar Q): satır/başlık sınıflandırıcısı akraba
        dillerde eşitlikte yanılır ("La presión en un fluido en reposo":
        fr=es=it=2 -> "fr"; İspanyolca kitapta başlık "French" diye
        raporlanıyordu). Kaynağın dili de Latin profilliyse ve metnin dili
        kaynağın dilini işlev kelimelerinde ``SOURCE_QUOTE_LANG_DOMINANCE``
        kat geçmiyorsa (güven düşük) kaynağın dili kullanılır; baskınsa
        (İtalyanca kitapta açıkça Fransızca başlık) sınıflandırıcının dili
        kalır. Kaynak dili bilinmiyorsa değişiklik yok.
        """
        ctx = self._src_ctx
        if (not code or ctx is None or ctx.lang is None
                or _same_language(code, ctx.lang)):
            return code
        ld = _lang_detect()
        if (code not in ld.LATIN_PROFILE_LANGS
                or ctx.lang not in ld.LATIN_PROFILE_LANGS
                or code in ("en", "tr") or ctx.lang in ("en", "tr")):
            return code
        hits, _ = ld.profile_hits(ld.tokenize_words(text))
        if (hits.get(code, 0)
                >= SOURCE_QUOTE_LANG_DOMINANCE
                * max(1, hits.get(ctx.lang, 0))):
            return code
        return ctx.lang

    def _mask_for_language_check(self, text: str) -> str:
        """Remove LaTeX commands, math, and placeholders before checking
        the language of the remaining text (tek kaynak:
        ``mask_for_language_check``)."""
        return mask_for_language_check(text)

    def _is_latex_only_line(self, line: str) -> bool:
        """Check if a line contains only LaTeX commands, no prose."""
        # Remove all LaTeX commands, math, braces, whitespace
        stripped = _LATEX_CMD_RE.sub("", line)
        stripped = _INLINE_BLOCK_RE.sub("", stripped)
        stripped = _DISPLAY_BLOCK_RE.sub("", stripped)
        stripped = _LATEX_PLACEHOLDER_RE.sub("", stripped)
        stripped = re.sub(r"[{}\[\]\\$%&]", "", stripped)
        stripped = stripped.strip()
        return len(stripped) < 10

    def _is_likely_english(self, text: str) -> bool:
        """Heuristic: is this text likely untranslated English (not Turkish)?

        Delegates to the canonical detector ``retranslator.is_english_line``
        (SOZLESME 4) so the three former copies of this logic agree on the
        same thresholds.  A local copy previously used an ALL-CAPS "title"
        guard that silently exempted capitalised untranslated English lines
        (QC-C3); the canonical detector has no such hole.

        Thresholds (min 4 words, min 20 chars) preserve the old body-check
        behaviour; ``is_heading`` is left False because callers pass already
        sentence-split prose.
        """
        # Lazy import to avoid a circular import at module load time.
        from latex_pipeline.retranslator import is_english_line
        return is_english_line(text, min_words=4, min_length=20)

    # ------------------------------------------------------------------
    # Check 3: Prompt Leakage Detection
    # ------------------------------------------------------------------

    # F27 (2026-07-29): case-insensitive katlama modül düzeyindedir
    # (``_fold_ci``); ön-eleme regex kararından daha geniş olmak zorunda.
    _fold_ci = staticmethod(_fold_ci)

    def _build_prompt_literal_index(self) -> None:
        """(kalıp, katlanmış kalıp, derlenmiş regex) üçlülerini hazırla.

        F27: eskiden her ``analyze`` çağrısında ~50 literal yeniden
        ``re.compile`` ediliyordu; derleme artık bir kez yapılır.
        """
        self._prompt_literal_res: List[Tuple[str, str, re.Pattern]] = [
            (p, self._fold_ci(p), re.compile(re.escape(p), re.IGNORECASE))
            for p in self._prompt_patterns
        ]

    def _check_prompt_leakage(
        self,
        tr_text: str,
        tr_lines: List[str],
        report: QCReport,
    ) -> None:
        """Find MT system prompt fragments leaked into translation output.

        A-1 (2026-07-27): manuel modda "Çeviri/Çevirmen notu" kalıpları
        uygulanmaz — Yol A'da çevirmenin bilinçli notudur, leak değildir.
        (Ana listeler Yol B sözleşmesi olarak tamdır; alt küme burada atlanır.)

        ``tr_lines`` ``tr_text``ten türemiş olmalıdır (analyze böyle çağırır):
        F27 ön-elemesi tüm gövde üzerinde çalışır.
        """
        _manual = getattr(self, "_manual_translation", False)
        # F27: eskiden desen x satır ikili döngüsü vardı (O(P x L)) ve her
        # desen yeniden derleniyordu. Artık gövdenin TEK katlanmış kopyası
        # üstünde ucuz `in` ön-elemesi yapılır; yalnız gövdede geçen desen
        # satır döngüsüne girer. Eşleşme kararını hâlâ AYNI regex verir,
        # issue kümesi ve sırası değişmez.
        if len(getattr(self, "_prompt_literal_res", ())) != len(
                self._prompt_patterns):
            # Kalıp listesi çalışma anında değiştirilmişse indeksi tazele.
            self._build_prompt_literal_index()
        folded_text = self._fold_ci(tr_text)
        for pattern_str, folded_pattern, pattern in self._prompt_literal_res:
            if _manual and pattern_str in self._prompt_patterns_mt_only:
                continue
            if folded_pattern not in folded_text:
                continue
            for i, line in enumerate(tr_lines, 1):
                if pattern.search(line):
                    # Skip LaTeX comments
                    stripped = line.strip()
                    if stripped.startswith("%"):
                        continue

                    report.issues.append(QCIssue(
                        check_name="prompt_leakage",
                        severity="critical",
                        line_num=i,
                        message=(
                            f"MT system prompt leak detected: "
                            f"\"{pattern_str}\""
                        ),
                        auto_fixable=True,
                        confidence=0.95,
                        fix_suggestion=(
                            f"Remove the leaked prompt text at line {i}"
                        ),
                        context=stripped[:120],
                    ))

        # --- Regex-based prompt leak patterns (angle-bracket markers etc.) ---
        for rx in self._prompt_leak_regexes:
            # A-1: MT-only regex'ler manuel modda atlanır
            if _manual and rx.pattern in self._prompt_leak_regexes_mt_only:
                continue
            for i, line in enumerate(tr_lines, 1):
                if rx.search(line):
                    stripped = line.strip()
                    if stripped.startswith("%"):
                        continue

                    report.issues.append(QCIssue(
                        check_name="prompt_leakage",
                        severity="critical",
                        line_num=i,
                        message=(
                            f"MT system prompt leak detected (regex): "
                            f"\"{rx.pattern}\""
                        ),
                        auto_fixable=True,
                        confidence=0.95,
                        fix_suggestion=(
                            f"Remove the leaked prompt text at line {i}"
                        ),
                        context=stripped[:120],
                    ))

    # ------------------------------------------------------------------
    # Check 4: Name/Reference Corruption
    # ------------------------------------------------------------------

    def _check_name_ref_corruption(
        self,
        tr_text: str,
        en_text: str,
        tr_lines: List[str],
        en_lines: List[str],
        report: QCReport,
    ) -> None:
        """Check that proper nouns and \\ref/\\cite values survive translation."""
        if not en_text:
            logger.debug("No source body -- skipping name/ref check.")
            return

        # --- Part A: \ref, \cite, \label value matching ---
        en_refs = self._extract_ref_values(en_text)
        tr_refs = self._extract_ref_values(tr_text)

        missing_refs = en_refs - tr_refs
        if missing_refs:
            # Group by type for clearer reporting
            severity = "high" if len(missing_refs) > 5 else "medium"
            report.issues.append(QCIssue(
                check_name="name_ref_corruption",
                severity=severity,
                line_num=1,
                message=(
                    f"Missing reference values in TR: "
                    f"{', '.join(sorted(missing_refs)[:15])}"
                    + (f" (+{len(missing_refs)-15} more)"
                       if len(missing_refs) > 15 else "")
                ),
                auto_fixable=False,
                confidence=0.90,
                fix_suggestion="Restore missing \\ref/\\cite/\\label values",
                context="",
            ))

        # --- Part B: \includegraphics path matching ---
        en_images = set(re.findall(
            r"\\includegraphics\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}", en_text,
        ))
        tr_images = set(re.findall(
            r"\\includegraphics\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}", tr_text,
        ))
        missing_images = en_images - tr_images
        if missing_images:
            report.issues.append(QCIssue(
                check_name="missing_images",
                severity="high",
                line_num=1,
                message=(
                    f"Missing \\includegraphics in TR: "
                    f"{', '.join(sorted(missing_images)[:5])}"
                ),
                auto_fixable=True,
                confidence=0.95,
                fix_suggestion="Restore missing image references from EN source",
                context=",".join(sorted(missing_images)),
            ))

        # --- Part C: Proper noun preservation ---
        en_nouns = self._extract_proper_nouns(en_text)

        # Only check nouns that appear multiple times (more reliable)
        # or are clearly personal names (contain a title prefix)
        significant_nouns: Set[str] = set()
        for noun in en_nouns:
            # Personal names with titles
            if re.match(r"(?:Dr|Prof|Mr|Mrs|Ms)\.", noun):
                significant_nouns.add(noun)
            # Multi-word proper nouns that appear at least twice
            elif en_text.count(noun) >= 2 and len(noun.split()) >= 2:
                significant_nouns.add(noun)

        for noun in significant_nouns:
            if noun not in tr_text:
                # Check partial matches (first/last name separately)
                words = noun.split()
                partial_found = any(w in tr_text for w in words if len(w) > 3)

                if not partial_found:
                    # Find approximate line in TR
                    line_num = 1
                    for idx, line in enumerate(tr_lines, 1):
                        # Look for any word from the noun
                        if any(w.lower() in line.lower() for w in words if len(w) > 2):
                            line_num = idx
                            break

                    report.issues.append(QCIssue(
                        check_name="name_ref_corruption",
                        severity="high",
                        line_num=line_num,
                        message=(
                            f"Proper noun possibly corrupted or missing: "
                            f"\"{noun}\""
                        ),
                        auto_fixable=False,
                        confidence=0.70,
                        fix_suggestion=(
                            f"Verify \"{noun}\" appears correctly in TR"
                        ),
                        context="",
                    ))

    def _extract_ref_values(self, text: str) -> Set[str]:
        """Extract all \\ref, \\cite, \\label, \\eqref values."""
        values: Set[str] = set()
        for m in _REF_CMD_RE.finditer(text):
            # \cite can have comma-separated keys
            raw = m.group(1)
            for key in raw.split(","):
                key = key.strip()
                if key:
                    values.add(key)
        return values

    def _extract_proper_nouns(self, text: str) -> Set[str]:
        """Extract proper noun candidates from English text.

        Uses a simple heuristic: capitalized multi-word sequences
        that aren't at the start of a sentence and aren't LaTeX commands.
        """
        # Remove LaTeX commands and math
        # E (2026-10-06, mt okuma MT-24): kaynak PDF kutusu (kckutu) düzyazı
        # sarar; ortam satırları önce atılır, yoksa aşağıdaki ortam silme
        # kutudaki bütün metni (özel adlarıyla) denetimden düşürürdü.
        cleaned = re.sub(r"\\(?:begin|end)\{" + _KUTU_ORTAMI + r"\}", " ", text)
        cleaned = re.sub(
            r"\\begin\{.+?\}.*?\\end\{.+?\}", " ", cleaned, flags=re.DOTALL,
        )
        cleaned = re.sub(r"\\\[.*?\\\]", " ", cleaned, flags=re.DOTALL)
        cleaned = re.sub(r"(?<![\$\\])\$(?!\$).+?(?<![\$\\])\$(?!\$)", " ", cleaned)
        cleaned = _LATEX_CMD_RE.sub(" ", cleaned)
        cleaned = re.sub(r"[{}]", " ", cleaned)

        nouns: Set[str] = set()
        for m in _PROPER_NOUN_RE.finditer(cleaned):
            noun = m.group(0).strip()
            # Filter out common false positives
            if noun in (
                "In This", "The The", "We The", "It The", "For The",
                "In The", "On The", "At The", "By The", "Of The",
                "As The", "To The", "If The", "Let The",
            ):
                continue
            # Skip if it's very short (just 2 short words)
            words = noun.split()
            if len(words) == 2 and all(len(w) <= 3 for w in words):
                continue
            nouns.add(noun)

        return nouns

    # ------------------------------------------------------------------
    # Check 5: Caption/Reference Consistency
    # ------------------------------------------------------------------

    def _check_caption_consistency(
        self,
        tr_text: str,
        tr_lines: List[str],
        report: QCReport,
    ) -> None:
        """Check for mixed English/Turkish terminology in the translation.

        If both "Fig." and "Sekil" appear, that's inconsistent -- the
        translator should use one or the other.
        """
        for en_form, tr_form in self._consistency_pairs:
            # Count occurrences (case sensitive for abbreviations,
            # word-boundary aware for full words)
            if len(en_form) <= 4:
                # Short form like "Fig." or "Eq." -- literal match
                en_count = tr_text.count(en_form)
            else:
                # Full word -- word boundary match
                en_count = len(re.findall(
                    r"\b" + re.escape(en_form) + r"\b", tr_text,
                ))

            tr_count = len(re.findall(
                re.escape(tr_form), tr_text,
            ))

            if en_count > 0 and tr_count > 0:
                # Both forms present -- inconsistency
                total = en_count + tr_count
                # If English form is dominant, it's worse
                en_ratio = en_count / total
                severity = "medium" if en_ratio > 0.3 else "low"

                # Find first line with English form
                line_num = 1
                for idx, line in enumerate(tr_lines, 1):
                    if en_form in line:
                        line_num = idx
                        break

                report.issues.append(QCIssue(
                    check_name="caption_consistency",
                    severity=severity,
                    line_num=line_num,
                    message=(
                        f"Inconsistent terminology: both \"{en_form}\" "
                        f"({en_count}x) and \"{tr_form}\" ({tr_count}x) used"
                    ),
                    auto_fixable=True,
                    confidence=0.85,
                    fix_suggestion=(
                        f"Replace all \"{en_form}\" with \"{tr_form}\" "
                        f"(or vice versa)"
                    ),
                    context="",
                ))

    # ------------------------------------------------------------------
    # Check 6: Placeholder Orphans
    # ------------------------------------------------------------------

    def _check_placeholder_orphans(
        self,
        tr_text: str,
        tr_lines: List[str],
        report: QCReport,
    ) -> None:
        """Find leftover placeholders that weren't restored or cleaned up.

        Searches for:
        - <<LATEX_N>> placeholders that weren't restored
        - [[UNIT_N]] / [[BIRIM_N]] / [[BİRİM_N]] artifacts
        - [[FORMULA_N]] artifacts
        - @...@@ proper noun markers
        """
        placeholder_checks = [
            (
                _LATEX_PLACEHOLDER_RE,
                "<<LATEX_N>>",
                "critical",
                "Unrestored LaTeX placeholder",
            ),
            (
                _UNIT_PLACEHOLDER_RE,
                "[[TYPE_N]]",
                "high",
                "Leftover pipeline placeholder artifact "
                "(UNIT/BIRIM/BİRİM/REF/PGREF/FORMULA...)",
            ),
            (
                _PROPER_NOUN_MARKER_RE,
                "@...@@",
                "medium",
                "Leftover proper noun marker",
            ),
        ]

        for pattern, label, severity, description in placeholder_checks:
            for i, line in enumerate(tr_lines, 1):
                for m in pattern.finditer(line):
                    matched = m.group(0)

                    # For @...@@, skip if inside a LaTeX comment
                    if label == "@...@@" and line.strip().startswith("%"):
                        continue

                    # For @...@@, skip email addresses (user@domain@@)
                    if label == "@...@@":
                        content = matched[1:-2]  # strip @ and @@
                        if "@" in content or "." in content:
                            continue

                    report.issues.append(QCIssue(
                        check_name="placeholder_orphans",
                        severity=severity,
                        line_num=i,
                        message=f"{description}: {matched}",
                        auto_fixable=True,
                        confidence=0.95,
                        fix_suggestion=f"Remove or restore: {matched}",
                        context=line.strip()[:120],
                    ))

        # Also check for mangled placeholders:
        # < < LATEX_42 > >, << LATEX_42>>, etc.
        mangled_re = re.compile(r"<\s*<\s*LATEX\s*_?\s*\d+\s*>\s*>")
        for i, line in enumerate(tr_lines, 1):
            for m in mangled_re.finditer(line):
                matched = m.group(0)
                # Skip if already caught by exact match above
                if _LATEX_PLACEHOLDER_RE.search(matched):
                    continue
                report.issues.append(QCIssue(
                    check_name="placeholder_orphans",
                    severity="critical",
                    line_num=i,
                    message=f"Mangled LaTeX placeholder: {matched}",
                    auto_fixable=True,
                    confidence=0.90,
                    fix_suggestion=(
                        f"Fix mangled placeholder at line {i}: {matched}"
                    ),
                    context=line.strip()[:120],
                ))


    # ------------------------------------------------------------------
    # Check 7: Broken LaTeX syntax
    # ------------------------------------------------------------------

    def _check_broken_latex_syntax(
        self,
        translated_body: str,
        tr_lines: List[str],
        report: QCReport,
    ) -> None:
        """Detect broken LaTeX syntax patterns in translated output."""

        # Pattern 1: \textit> instead of \textit{
        for i, line in enumerate(tr_lines, 1):
            if r"\textit>" in line:
                report.issues.append(QCIssue(
                    check_name="broken_latex_syntax",
                    severity="high",
                    line_num=i,
                    message=f"Line {i}: Broken \\textit> should be \\textit{{}}",
                    auto_fixable=True,
                    confidence=1.0,
                    fix_suggestion=r"Replace \textit> with \textit{",
                    context=line.strip()[:120],
                ))

        # Pattern 2: Markdown image syntax in LaTeX
        md_img_re = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
        for i, line in enumerate(tr_lines, 1):
            if md_img_re.search(line):
                report.issues.append(QCIssue(
                    check_name="broken_latex_syntax",
                    severity="medium",
                    line_num=i,
                    message=f"Line {i}: Markdown image syntax ![](src) in LaTeX output",
                    auto_fixable=True,
                    confidence=0.95,
                    fix_suggestion="Convert ![](src) to \\includegraphics",
                    context=line.strip()[:120],
                ))

        # Pattern 3: Japanese/Asian brackets instead of braces
        for i, line in enumerate(tr_lines, 1):
            for bad_char in ["』", "》", "《"]:
                if bad_char in line:
                    report.issues.append(QCIssue(
                        check_name="broken_latex_syntax",
                        severity="medium",
                        line_num=i,
                        message=f"Line {i}: Asian bracket {bad_char} should be brace",
                        auto_fixable=True,
                        confidence=1.0,
                        fix_suggestion=f"Replace {bad_char} with brace",
                        context=line.strip()[:120],
                    ))

        # Pattern 4: Markdown links [text](#page-xxx) in LaTeX
        md_link_re = re.compile(r"\[[^\]]*\]\(\\?#page-\d+-\d+\)")
        for i, line in enumerate(tr_lines, 1):
            if md_link_re.search(line):
                report.issues.append(QCIssue(
                    check_name="broken_latex_syntax",
                    severity="high",
                    line_num=i,
                    message=f"Line {i}: Markdown link [text](#page) in LaTeX output",
                    auto_fixable=True,
                    confidence=0.95,
                    fix_suggestion="Convert to \\href or remove link",
                    context=line.strip()[:120],
                ))

        # Pattern 5: Unclosed titlepage braces -- {\Huge or {\Large without }
        _tp_unclosed_re = re.compile(
            r"^\s*\{\\(?:Huge|Large)\b"
        )
        for i, line in enumerate(tr_lines, 1):
            if _tp_unclosed_re.match(line):
                # Line starts with {\Huge or {\Large -- check brace balance
                if line.count("{") > line.count("}"):
                    report.issues.append(QCIssue(
                        check_name="broken_latex_syntax",
                        severity="high",
                        line_num=i,
                        message=f"Line {i}: Unclosed brace in titlepage sizing command",
                        auto_fixable=True,
                        confidence=1.0,
                        fix_suggestion="Add closing brace before \\par",
                        context=line.strip()[:120],
                    ))

        # Pattern 6: LaTeX-encoded Turkish chars (should be UTF-8 for XeLaTeX)
        for i, line in enumerate(tr_lines, 1):
            if r"\c{S}ekil" in line or r'\"' in line:
                report.issues.append(QCIssue(
                    check_name="broken_latex_syntax",
                    severity="low",
                    line_num=i,
                    message=f"Line {i}: LaTeX-encoded Turkish char should be UTF-8",
                    auto_fixable=True,
                    confidence=0.9,
                    fix_suggestion="Use UTF-8 Şekil instead of \\c{{S}}ekil",
                    context=line.strip()[:120],
                ))

        # Pattern 7: [[BIRIM_N]] placeholder remnants
        birim_re = re.compile(r"\[\[(?:BIRIM|BİRİM|UNIT)_\d+\]\]")
        for i, line in enumerate(tr_lines, 1):
            if birim_re.search(line):
                report.issues.append(QCIssue(
                    check_name="broken_latex_syntax",
                    severity="high",
                    line_num=i,
                    message=f"Line {i}: Pipeline placeholder [[BIRIM_N]] not cleaned",
                    auto_fixable=True,
                    confidence=1.0,
                    fix_suggestion="Remove [[BIRIM_N]] placeholder",
                    context=line.strip()[:120],
                ))

    # ------------------------------------------------------------------
    # Check 8: Brace Balance
    # ------------------------------------------------------------------

    def _check_brace_balance(
        self,
        tr_text: str,
        tr_lines: List[str],
        report: QCReport,
    ) -> None:
        """Check that { and } counts are balanced globally and per region.

        An imbalanced document will not compile. Titlepage regions are
        checked separately because MT models frequently inject extra
        braces there.
        """
        # --- Global brace balance ---
        # Strip verbatim/lstlisting/minted environments
        cleaned = self._strip_verbatim_envs(tr_text)
        # Do not count escaped braces \{ \}
        cleaned_no_esc = re.sub(r"\\[{}]", "", cleaned)

        open_count = cleaned_no_esc.count("{")
        close_count = cleaned_no_esc.count("}")
        diff = open_count - close_count

        if diff != 0:
            severity = "critical" if abs(diff) > 5 else "high"
            problem_line = self._find_brace_imbalance_line(
                tr_lines, diff,
            )

            report.issues.append(QCIssue(
                check_name="brace_balance",
                severity=severity,
                line_num=problem_line,
                message=(
                    f"Brace imbalance: {open_count} opening '{{' vs "
                    f"{close_count} closing '}}' (diff={diff:+d})"
                ),
                auto_fixable=abs(diff) <= 3,
                confidence=0.95,
                fix_suggestion=(
                    f"{'Remove' if diff < 0 else 'Add'} "
                    f"{abs(diff)} {'closing' if diff > 0 else 'opening'} "
                    f"brace(s)"
                ),
                context="",
            ))

        # --- Titlepage region brace balance ---
        tp_start = None
        tp_end = None
        for i, line in enumerate(tr_lines, 1):
            if r"\begin{titlepage}" in line:
                tp_start = i
            if r"\end{titlepage}" in line:
                tp_end = i
                break

        if tp_start is not None and tp_end is not None:
            tp_text = "\n".join(tr_lines[tp_start - 1:tp_end])
            tp_cleaned = re.sub(r"\\[{}]", "", tp_text)
            tp_open = tp_cleaned.count("{")
            tp_close = tp_cleaned.count("}")
            tp_diff = tp_open - tp_close
            if tp_diff != 0:
                report.issues.append(QCIssue(
                    check_name="brace_balance",
                    severity="high",
                    line_num=tp_start,
                    message=(
                        f"Titlepage brace imbalance: "
                        f"{tp_open} '{{' vs {tp_close} '}}' "
                        f"(diff={tp_diff:+d})"
                    ),
                    auto_fixable=True,
                    confidence=0.95,
                    fix_suggestion=(
                        f"Fix brace balance in titlepage region "
                        f"(lines {tp_start}-{tp_end})"
                    ),
                    context=tp_text[:100],
                ))

    def _find_brace_imbalance_line(
        self, lines: List[str], global_diff: int,
    ) -> int:
        """Find the line where brace balance first goes wrong."""
        balance = 0
        for i, line in enumerate(lines, 1):
            cleaned = re.sub(r"\\[{}]", "", line)
            balance += cleaned.count("{") - cleaned.count("}")
            if global_diff > 0 and balance > 10:
                return i
            if global_diff < 0 and balance < -1:
                return i
        return 1

    @staticmethod
    def _strip_verbatim_envs(text: str) -> str:
        """Remove content inside verbatim/lstlisting/minted envs."""
        return re.sub(
            r"\\begin\{(?:verbatim|lstlisting|minted|Verbatim)\}"
            r".*?"
            r"\\end\{(?:verbatim|lstlisting|minted|Verbatim)\}",
            "",
            text,
            flags=re.DOTALL,
        )

    # ------------------------------------------------------------------
    # Check 9: Markdown Link Remnants
    # ------------------------------------------------------------------

    def _check_markdown_link_remnants(
        self,
        tr_text: str,
        tr_lines: List[str],
        report: QCReport,
    ) -> None:
        """Detect markdown link/image syntax that should not be in LaTeX.

        Catches patterns the existing broken_latex_syntax check misses:
        - [[N](#page-xxx)] with double brackets
        - [[N\\]](#page-xxx) with escaped bracket
        - ![alt](path) markdown images
        - [text](url) generic markdown links
        """
        # Pattern A: [[N](#page-...)] -- double-bracketed reference links
        double_bracket_link = re.compile(
            r"\[\[(\d+(?:,\s*\d+)*)"
            r"(?:\\?\])?"
            r"\]\(\\?#[^)]*\)\]?"
        )

        # Pattern B: [[N\]](#page) with escaped bracket inside
        escaped_bracket_link = re.compile(
            r"\[\[(\d+(?:,\s*\d+)*)"
            r"\\?\]\]"
            r"\(\\?#[^)]*\)"
        )

        # Pattern C: [N\](#page-...) with backslash before ]
        backslash_bracket = re.compile(
            r"\[(\d+(?:,\s*\d+)*)\\?\]\(\\?#page[^)]*\)"
        )

        # Pattern D: generic markdown link [text](url) not already caught
        generic_md_link = re.compile(
            r"\[([^\]]{1,80})\]\((?:https?://|\\?#)[^)]+\)"
        )

        # Pattern E: markdown image ![...](...)
        md_image = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")

        # Pattern F (2026-09-25, kulvar Q): EPUB dipnot bağlantısı, hedef
        # başka XHTML dosyasında: "[[1]](…h-18.htm.xhtml\#Footnote\_1)".
        # A/B yalnız "(#…)" çapasını tanıyordu; bu kalıntı PDF'e ham metin
        # basılıyor (bir kitapta 4 kez, bir satır 28,8 pt taşıyor) ama QC'de
        # yalnız dolaylı "xhtml ünsüz kümesi" uyarısı olarak görünüyordu.
        # Autofix'te karşılığı yok: rapor-only (auto_fixable=False).
        file_anchor_link = re.compile(
            r"\[\[(\d+(?:,\s*\d+)*)\\?\]\]"
            r"\((?![^)]*https?://)[^()\s]+?\\?#[^)\s]*\)"
        )

        patterns = [
            (double_bracket_link, "Double-bracket markdown link [[N](#page)]"),
            (escaped_bracket_link, "Escaped bracket markdown link [[N\\]](#page)"),
            (backslash_bracket, "Backslash bracket markdown link [N\\](#page)"),
            (generic_md_link, "Generic markdown link [text](url)"),
            (md_image, "Markdown image ![alt](src)"),
            (file_anchor_link,
             "File-anchored markdown link [[N]](file#anchor)"),
        ]

        for pattern, description in patterns:
            for i, line in enumerate(tr_lines, 1):
                if line.strip().startswith("%"):
                    continue
                for m in pattern.finditer(line):
                    matched = m.group(0)
                    report.issues.append(QCIssue(
                        check_name="markdown_link_remnant",
                        severity="high",
                        line_num=i,
                        message=(
                            f"{description}: {matched[:60]}"
                        ),
                        auto_fixable=pattern is not file_anchor_link,
                        confidence=0.95,
                        fix_suggestion=(
                            f"Convert to LaTeX or remove: {matched[:40]}"
                        ),
                        context=line.strip()[:120],
                    ))

    # ------------------------------------------------------------------
    # Check 10: Table Column Mismatch
    # ------------------------------------------------------------------

    def _check_table_column_mismatch(
        self,
        tr_text: str,
        tr_lines: List[str],
        report: QCReport,
    ) -> None:
        r"""Check tabular column definitions vs data row & counts.

        If \begin{tabular}{l l l} says 3 columns but a data row has
        4 ampersands, the table will not compile correctly.
        """
        # 2026-09-24 (I4): spec PARANTEZ-DENGELİ okunur ve sütun olarak yalnız
        # sütun türleri sayılır. Eski `\{([^}]+)\}` + `[lcrpmbXS]` sayacı
        # "l l >{\raggedright\arraybackslash}p{70mm} r" spec'ini ilk '}'te
        # kesip \raggedright/\arraybackslash içindeki harfleri sütun sanıyordu
        # ("9 cols") -> satır başına sahte HIGH (bir kitapta 7).
        tabular_re = re.compile(
            r"\\begin\{(tabular\*?|longtable|tabularx)\}"
        )

        in_tabular = False
        expected_cols = 0
        tabular_start_line = 0
        env_name = ""
        col_spec = ""

        for i, line in enumerate(tr_lines, 1):
            stripped = line.strip()

            if not in_tabular:
                m = tabular_re.search(stripped)
                if m:
                    env_name = m.group(1)
                    spec = _read_tabular_spec(stripped, m.end(), env_name)
                    if spec is None:
                        continue
                    in_tabular = True
                    col_spec = spec
                    expected_cols = _count_tabular_spec_columns(col_spec)
                    tabular_start_line = i
                continue

            end_pattern = f"\\end{{{env_name}}}"
            if end_pattern in stripped:
                in_tabular = False
                continue

            if (not stripped
                    or stripped.startswith("%")
                    or stripped == r"\hline"
                    or stripped == r"\toprule"
                    or stripped == r"\midrule"
                    or stripped == r"\bottomrule"
                    or stripped.startswith(r"\cline")):
                continue

            if r"\\" in stripped or "&" in stripped:
                amp_count = stripped.count("&")
                row_cols = amp_count + 1

                multi_re = re.compile(r"\\multicolumn\{(\d+)\}")
                multi_spans = multi_re.findall(stripped)
                if multi_spans:
                    extra_cols = sum(int(n) - 1 for n in multi_spans)
                    row_cols += extra_cols

                if (expected_cols > 0
                        and row_cols != expected_cols
                        and amp_count > 0):
                    report.issues.append(QCIssue(
                        check_name="table_column_mismatch",
                        severity="high",
                        line_num=i,
                        message=(
                            f"Table column mismatch at L{i}: "
                            f"spec says {expected_cols} cols "
                            f"({col_spec}), row has {row_cols} "
                            f"({amp_count} ampersands)"
                        ),
                        auto_fixable=False,
                        confidence=0.90,
                        fix_suggestion=(
                            f"Fix row at L{i} to have "
                            f"{expected_cols - 1} ampersands "
                            f"(tabular started at L{tabular_start_line})"
                        ),
                        context=stripped[:120],
                    ))

    # ------------------------------------------------------------------
    # Check 11: Untranslated Headings
    # ------------------------------------------------------------------

    def _check_untranslated_headings(
        self,
        tr_text: str,
        en_text: str,
        tr_lines: List[str],
        en_lines: List[str],
        report: QCReport,
    ) -> None:
        r"""Detect chapter/section headings still in source language.

        Looks for \chapter{English Text}, \section{English Text} etc.
        Türkçe harfsiz Latin başlıkta İngilizce kararı
        ``retranslator.is_english_line``'ındır (tek kaynak); Avrupa dili,
        Latin dışı alfabe ve Türkçe harfli yarım çeviri ayrıca denetlenir.
        """
        # 2026-09-24: başlık argümanı PARANTEZ-DENGELİ okunur (retranslator
        # ile tek kaynak). Eski `([^}]+)` "\section{The \textit{x}-Axis
        # Rule}" başlığını "The \textit{x" diye kesiyordu: rapor yanlış
        # metni gösteriyor, autofix'e kırık başlık gidiyordu.
        from latex_pipeline.retranslator import (
            _clean_for_language_check,
            _split_heading_shell,
            is_english_line,
        )
        heading_re = re.compile(
            r"\\(chapter|section|subsection|subsubsection|paragraph)"
            r"\*?\s*(?:\[[^\]]*\])?\s*\{"
        )

        def _heading_title(line: str):
            m = heading_re.search(line)
            if not m:
                return None, None
            shell = _split_heading_shell(line[m.start():])
            if shell is None:
                return m, None
            return m, _decode_latex_turkish(shell[1].strip())

        # Kaynaktaki başlık metinleri (birebir yankı kanıtı için). Numara
        # öneki sanitizer'daki gibi iki tarafta da atılır: çeviride
        # "\section{1.2 Machine Learning}" -> "\section{Machine Learning}".
        from latex_pipeline.sanitizer import SECTION_NUM_PREFIX_RE

        def _echo_key(line: str):
            return _heading_title(SECTION_NUM_PREFIX_RE.sub(r"\1", line))[1]

        en_headings = set()
        for en_line in (en_lines or []):
            en_title = _echo_key(en_line)
            if en_title:
                en_headings.add(en_title)

        for i, line in enumerate(tr_lines, 1):
            m, heading_text = _heading_title(line)
            if m is None:
                continue

            cmd = m.group(1)
            if not heading_text:
                continue

            if len(heading_text) < 5:
                continue

            # 2026-09-30 (kulvar F2): doğal dil kelimesi olmayan
            # başlık (makine belirteci/tanımlayıcı: "qqSAYAC\_YENI/
            # BASLA\_T1700000000\_U12345", salt numara) çevrilmez; kaynakta
            # başlık olarak birebir geçmesi "çevrilmemiş" kanıtı değildir.
            if not natural_language_words(heading_text):
                continue

            # Dil kontrolü metni retranslator ile TEK KAYNAK: komut ve
            # argümanı boşlukla ayrılır ("\textit{n}-Cisim" -> "Cisim");
            # eski boşluksuz silme komşu kelimeleri yapıştırıyordu.
            probe = "\\" + cmd + "{" + heading_text + "}"
            cleaned = _clean_for_language_check(probe, is_heading=True)
            if len(cleaned) < 4:
                continue

            has_turkish = any(c in heading_text for c in _TURKISH_CHARS)

            # --- Mixed-language heading detection ---
            # Even if heading has Turkish chars, check for English function
            # words that indicate a half-translated heading like
            # "Doğrusal Uzaylar And The Temel Kavramlar".
            _en_function_words = {
                'the', 'and', 'of', 'in', 'for', 'with', 'or', 'a', 'an',
                'by', 'to', 'is', 'are', 'on', 'at', 'not', 'but',
            }
            heading_words = heading_text.split()
            en_func_count = sum(
                1 for w in heading_words if w.lower() in _en_function_words
            )
            if has_turkish and en_func_count >= 2 and len(heading_words) >= 3:
                severity = "high" if cmd in ("chapter", "section") else "medium"
                report.issues.append(QCIssue(
                    check_name="untranslated_heading",
                    severity=severity,
                    line_num=i,
                    message=(
                        f"\\{cmd} heading is half-translated "
                        f"({en_func_count} English function words): "
                        f"\"{heading_text[:60]}\""
                    ),
                    auto_fixable=True,
                    confidence=0.90,
                    fix_suggestion=(
                        f"Fully translate the \\{cmd} heading at L{i} -- "
                        f"English words remain: "
                        + ", ".join(
                            w for w in heading_words
                            if w.lower() in _en_function_words
                        )
                    ),
                    context=line.strip()[:120],
                ))
                continue  # Already reported; skip the fully-untranslated check

            # 2026-09-24: ö/ü/ç Almanca/Fransızca ile ORTAK — "Über die
            # Düsenströmung" gibi çevrilmemiş başlık Türkçe sanılıp
            # atlanıyordu. Türkçeye özgü harf (ş, ğ, ı, İ) yoksa Avrupa
            # işlev kelimelerine bakılır (eşikler core.lang_detect'te).
            label = None
            if has_turkish:
                eu_lang = _lang_detect().classify_latin_line(
                    cleaned, heading=True)
                if eu_lang is None:
                    continue
                # F2-14 (2026-09-29): akraba dillerde eşitlik -> kaynağın dili.
                label = _lang_detect().language_name_en(
                    self._prefer_source_language(eu_lang, cleaned))
            else:
                counts = _lang_detect().script_letter_counts(cleaned)
                non_latin = sum(v for k, v in counts.items() if k != "latin")
                # Latin dışı alfabeli başlık (Kiril, Yunan, CJK...) eskisi
                # gibi çevrilmemiş sayılır; Check 2 onu buraya bırakır.
                if non_latin <= counts["latin"]:
                    eu_lang = _lang_detect().classify_latin_line(
                        cleaned, heading=True)
                    if eu_lang is not None:
                        # F2-14 (2026-09-29): İspanyolca başlık "French"
                        # diye etiketleniyordu (fr=es=it eşitliği).
                        label = _lang_detect().language_name_en(
                            self._prefer_source_language(eu_lang, cleaned))
                    # Latin harfli, Türkçe harfsiz başlık: İngilizce kararı
                    # retranslator.is_english_line'ın (TEK KAYNAK;
                    # retranslator başlığı aynı kararla geri çevirir). Eski
                    # yerel sezgi (Türkçe sık kelime/ek yoksa çevrilmemiş)
                    # ASCII Türkçe başlıkları ("Potansiyel Enerji",
                    # "\textit{n}-Cisim Problemi") HIGH işaretliyordu
                    # (denetim 2026-09-24 M1#6). Uzunluk kapısı QC'nin
                    # eskisi (tek kelimelik "Introduction" da denetlenir);
                    # karar mantığı aynıdır. 2026-07-11 (#35): başlık EN
                    # kaynakta başlık olarak BİREBİR geçiyorsa da
                    # çevrilmemiştir ('Scalar Products' sözde Türkçe ekle
                    # dil kapısından geçer; ASCII Türkçe başlık EN kaynakta
                    # başlık olmaz).
                    elif (not is_english_line(probe, min_words=1,
                                              min_length=4, is_heading=True)
                            and _echo_key(line) not in en_headings):
                        continue
                    # 2026-09-25 (kulvar Q): kaynak İngilizce DEĞİLSE
                    # "İngilizce başlık" kararı kaynaktan kanıt ister.
                    # is_english_line salt-ASCII Türkçe başlığı ("Kutu 1.1.
                    # Hidrostatik paradoks": 'paradoks' İngilizce çoğul -s
                    # izi) İngilizce sayabiliyor; İspanyolca kaynaktan
                    # İngilizce başlık ancak kaynakta da geçiyorsa gelmiş
                    # olabilir (başlık yankısı ya da kelimeleri kaynakta).
                    elif (label is None
                            and self._src_ctx is not None
                            and self._src_ctx.lang is not None
                            and not _same_language(self._src_ctx.lang, "en")
                            and _echo_key(line) not in en_headings
                            and not self._heading_words_in_source(
                                cleaned)):
                        continue

            # NOTE: no ALL-CAPS skip here.  An untranslated English heading
            # rendered in capitals (e.g. \chapter{DEEP LEARNING FUNDAMENTALS})
            # is exactly what must be flagged; genuine Turkish headings are
            # exempted above (Turkish chars / is_english_line).
            in_source = bool(en_text) and heading_text in en_text
            if label is not None:
                confidence = 0.95 if in_source else 0.80
                message = (f"\\{cmd} heading appears untranslated "
                           f"({label}): \"{heading_text[:50]}\"")
            else:
                confidence = 0.95 if in_source else 0.75
                message = (f"\\{cmd} heading appears untranslated: "
                           f"\"{heading_text[:50]}\"")

            report.issues.append(QCIssue(
                check_name="untranslated_heading",
                severity="high" if cmd in ("chapter", "section") else "medium",
                line_num=i,
                message=message,
                auto_fixable=True,
                confidence=confidence,
                fix_suggestion=(
                    f"Translate the \\{cmd} heading at L{i}"
                ),
                context=line.strip()[:120],
            ))

    # ------------------------------------------------------------------
    # Check 12: Broken Inline Math
    # ------------------------------------------------------------------

    @staticmethod
    def _leading_comment_lines(lines: List[str]) -> int:
        """Baştaki yorum (``%``) ya da boş satır sayısı.

        Yol A çevirisi (EXPORT_TR.tex) 12 satırlık KITAPCEVIR-META
        başlığıyla başlar, kaynak gövde başlamaz; TR→kaynak satır eşlemesi
        bu bloğu atlamazsa her satır ~11 satır kayar (kulvar Q, 2026-09-28).
        """
        n = 0
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("%"):
                break
            n += 1
        return n

    def _check_broken_inline_math(
        self,
        tr_text: str,
        tr_lines: List[str],
        report: QCReport,
        en_lines: List[str] = None,
    ) -> None:
        r"""Detect fragmented inline math that should be a single expression.

        MT models sometimes split $x^{N}_{T}$ into $x^{N}$ $_{T}$
        or $^{N}$ ) $^{T}$ etc.

        If ``en_lines`` is provided, the check compares each flagged
        pattern against the corresponding EN source line.  When the
        exact same fragmented pattern exists in the EN source, the
        issue is suppressed (the fragmentation originates from the
        converter, not from the MT model).

        Patterns:
        - $expr$ $expr$ (consecutive inline math with no/little text)
        - $^{...}$ standalone superscript without base
        - $_{...}$ standalone subscript without base
        """
        from latex_pipeline.sanitizer import (TEXT_MODE_SYMBOL_COMMANDS,
                                              apply_text_mode_math_wraps,
                                              dollar_math_spans)

        # Kaynak satırın sanitizer sarmalamalı biçimi (satır no -> metin);
        # yalnız bir aday parça denetlenirken hesaplanır.
        en_wrapped: Dict[int, str] = {}

        # Consecutive inline math with 0-3 chars between
        consecutive_math = re.compile(
            r"\$([^$]+)\$"
            r"\s{0,3}"
            r"([)(\[\],;:\s]{0,3})"
            r"\s{0,3}"
            r"\$([^$]+)\$"
        )

        # Standalone superscript/subscript (no base variable)
        standalone_script = re.compile(
            r"(?<!\$)\$\s*([_^]\{[^}]+\})\s*\$(?!\$)"
        )

        def _normalize_math_expr(expr: str) -> str:
            """Normalize a math expression for comparison.

            Strips empty brace groups ``{}`` that the converter inserts
            as empty bases before superscript/subscript.  This allows
            ``${}^{K}$`` to match ``$^{K}$``.
            """
            return re.sub(r"\{\}", "", re.sub(r"\s+", " ", expr.strip()))

        def _pattern_exists_in_en(pattern_text: str, tr_line_idx: int) -> bool:
            """Check if the same fragmented math pattern exists in the EN source.

            Uses a window of lines around the corresponding position to
            account for line-count differences between EN and TR.
            """
            if not en_lines:
                return False

            # Map TR line index to EN line range (proportional + window).
            # 2026-09-28 (kulvar Q): baştaki yorum/boş satır bloğu (Yol A
            # çevirisinin 12 satırlık KITAPCEVIR-META başlığı) orana
            # katılmaz; eskiden TR satırı ~11 satır geriye düşüyor, kaynakta
            # aynı yerde duran "${}^{1}$" dipnot işareti ±5 pencereye
            # girmiyordu (dil_sent_it: "Standalone superscript" MEDIUM x2).
            tr_off = self._leading_comment_lines(tr_lines)
            en_off = self._leading_comment_lines(en_lines)
            ratio = ((len(en_lines) - en_off)
                     / max(len(tr_lines) - tr_off, 1))
            en_center = en_off + int(max(tr_line_idx - tr_off, 0) * ratio)
            window = 5
            start = max(0, en_center - window)
            end = min(len(en_lines), en_center + window + 1)

            # Normalize whitespace and empty braces for comparison
            normalized = _normalize_math_expr(pattern_text)
            math_parts = re.findall(r"\$[^$]+\$", normalized)

            for en_idx in range(start, end):
                en_stripped = en_lines[en_idx].strip()
                # 2026-09-28 (Vq): kaynak satır sanitizer'ın metin kipi
                # sarmalamasından geçirilmiş biçimiyle de karşılaştırılır
                # (inline sayımındaki "iki tarafa aynı kural" paritesi,
                # 2026-09-24). Çeviri sanitize edilmiş gövdedir; kaynakta
                # aynen duran '\textit{δ ≈}' çeviride '$\delta$ $\approx$'
                # olur ve MT kaynaklı parçalanma sanılıyordu (dil_sent_it
                # HIGH x3, dil_sent_de HIGH x1; çeviri kaynakla aynıydı).
                if en_idx not in en_wrapped:
                    en_wrapped[en_idx] = _normalize_math_expr(
                        apply_text_mode_math_wraps(en_stripped))
                for en_normalized in (_normalize_math_expr(en_stripped),
                                      en_wrapped[en_idx]):
                    if normalized in en_normalized:
                        return True

                    # Also check if the individual math fragments exist
                    # in the same EN line (not necessarily identical
                    # spacing). Extract the $...$ parts from the pattern
                    if len(math_parts) >= 2:
                        all_found = all(
                            _normalize_math_expr(mp) in en_normalized
                            for mp in math_parts
                        )
                        if all_found:
                            return True

                    # Check single standalone script: $^{X}$ should match
                    # ${}^{X}$ in EN (empty-brace base form)
                    if len(math_parts) == 1:
                        mp_norm = _normalize_math_expr(math_parts[0])
                        if mp_norm in en_normalized:
                            return True

            return False

        for i, line in enumerate(tr_lines, 1):
            stripped = line.strip()
            if stripped.startswith("%"):
                continue

            if re.match(
                r"\s*\\(?:begin|end)\{(?:equation|align|gather|"
                r"multline|split|flalign|alignat)",
                stripped,
            ):
                continue

            # 2026-09-28 (Vq): parça adayları TeX'in okuduğu satır içi
            # matematik bölgelerinden kurulur (sanitizer.dollar_math_spans:
            # '$$' metin kipinde display açar). Düz desen display'in içindeki
            # '${}^{^1}$'ı ve display'den sonraki düzyazıyı ('$$...$$
            # İngilizce literatürde ... $\lambda$') iki inline parça sanıyor
            # (dil_sent_de: dipnot satırına sahte HIGH), bu sahte eşleşme
            # satırdaki gerçek bitişik parçayı da yutuyordu. Bitişik iki
            # bölge aynı desenle (arada en çok 3 boşluk/ayraç) sınanır;
            # eşleşen çift atlanır (eski finditer'in örtüşmezliği).
            spans = [(s, e) for s, e, disp in dollar_math_spans(stripped)
                     if not disp]
            pairs = []
            k = 0
            while k + 1 < len(spans):
                pm = consecutive_math.fullmatch(
                    stripped, spans[k][0], spans[k + 1][1])
                if pm:
                    pairs.append(pm)
                    k += 2
                else:
                    k += 1
            for m in pairs:
                first = m.group(1).strip()
                sep = m.group(2).strip()
                second = m.group(3).strip()

                is_fragment = False
                if first.endswith("^") or first.endswith("_"):
                    is_fragment = True
                if second.startswith("^") or second.startswith("_"):
                    is_fragment = True
                if second.startswith("{") and first.endswith("}"):
                    is_fragment = True
                if re.match(r"^[_^]\{", first) or re.match(r"^[_^]\{", second):
                    is_fragment = True
                # Yakınlık sezgisi: sanitizer'ın G3 sembol sarmalaması
                # ('$m$ ≪ M' -> '$m$ $\ll$ M') tek başına tam bir ifadedir,
                # parça sayılmaz (denetim 2026-09-24 M1#4). Yapısal
                # kurallar (yukarıdaki ^/_ ve {} bağları) aynen geçerli.
                if sep in (")", "(", "") and (
                    len(first) <= 8 or len(second) <= 8
                ) and not (first in TEXT_MODE_SYMBOL_COMMANDS
                           or second in TEXT_MODE_SYMBOL_COMMANDS):
                    is_fragment = True

                if is_fragment:
                    matched = m.group(0)
                    # Skip if EN source has the same fragmented pattern
                    if _pattern_exists_in_en(matched, i - 1):
                        logger.debug(
                            "Skipping broken_inline_math at L%d: "
                            "same pattern exists in EN source: %s",
                            i, matched[:60],
                        )
                        continue

                    report.issues.append(QCIssue(
                        check_name="broken_inline_math",
                        severity="high",
                        line_num=i,
                        message=(
                            f"Fragmented inline math at L{i}: "
                            f"{matched[:60]}"
                        ),
                        auto_fixable=True,
                        confidence=0.85,
                        fix_suggestion="Merge into single $...$ expression",
                        context=stripped[:120],
                    ))

            for m in standalone_script.finditer(stripped):
                matched = m.group(0)
                pos = m.start()
                before = stripped[:pos]
                dollar_count = before.count("$") - before.count(r"\$")
                if dollar_count % 2 == 0:
                    # Skip if EN source has the same standalone pattern
                    if _pattern_exists_in_en(matched, i - 1):
                        logger.debug(
                            "Skipping standalone script at L%d: "
                            "same pattern exists in EN source: %s",
                            i, matched,
                        )
                        continue

                    report.issues.append(QCIssue(
                        check_name="broken_inline_math",
                        severity="medium",
                        line_num=i,
                        message=(
                            f"Standalone "
                            f"{'superscript' if '^' in matched else 'subscript'} "
                            f"without base: {matched}"
                        ),
                        auto_fixable=True,
                        confidence=0.85,
                        fix_suggestion=(
                            "Attach to preceding variable or merge "
                            "with adjacent math"
                        ),
                        context=stripped[:120],
                    ))

    # ------------------------------------------------------------------
    # Check 13: LaTeX-Encoded Turkish Characters
    # ------------------------------------------------------------------

    def _check_latex_encoded_turkish(
        self,
        tr_text: str,
        tr_lines: List[str],
        report: QCReport,
    ) -> None:
        r"""Detect LaTeX-encoded Turkish characters that should be UTF-8.

        In XeLaTeX with UTF-8 encoding, characters like S-cedilla,
        o-umlaut etc. should be written directly rather than using
        legacy encodings like \c{S}, \"{o}, {\i}, \u{g}, etc.
        """
        combined_pattern = re.compile(
            r'\\c\{[SsCc]\}'
            r'|\\"\{[OoUu]\}'
            r'|\{\\i\}'
            r"|\\i(?=[^a-zA-Z])"
            r"|\\u\{[Gg]\}"
            r"|\\\.\{I\}"
        )

        for i, line in enumerate(tr_lines, 1):
            stripped = line.strip()
            if stripped.startswith("%"):
                continue

            if re.match(
                r"\s*\\(?:begin|end)\{(?:equation|align|gather|"
                r"multline|split|flalign|alignat)",
                stripped,
            ):
                continue

            for m in combined_pattern.finditer(stripped):
                matched = m.group(0)

                # Skip if inside inline math ($...$)
                pos = m.start()
                before = stripped[:pos]
                dollar_count = before.count("$") - before.count(r"\$")
                if dollar_count % 2 != 0:
                    continue

                report.issues.append(QCIssue(
                    check_name="latex_encoded_turkish",
                    severity="low",
                    line_num=i,
                    message=(
                        f"LaTeX-encoded Turkish char at L{i}: "
                        f"\"{matched}\" -- use UTF-8 in XeLaTeX"
                    ),
                    auto_fixable=True,
                    confidence=0.90,
                    fix_suggestion=(
                        f"Replace \"{matched}\" with UTF-8 equivalent"
                    ),
                    context=stripped[:120],
                ))


    # ------------------------------------------------------------------
    # Check 14: Information Loss (section/chapter count parity)
    # ------------------------------------------------------------------

    def _check_information_loss(
        self,
        tr_text: str,
        en_text: str,
        tr_lines: List[str],
        en_lines: List[str],
        report: QCReport,
    ) -> None:
        """Compare section/chapter counts between EN and TR.

        If the translated document has fewer structural elements than
        the original, content may have been silently dropped.
        """
        if not en_text:
            logger.debug("No source body -- skipping information_loss check.")
            return

        # Sanitizer'ın bölüm etiketi dönüşümü iki tarafa da (tek kaynak).
        en_text = _chapter_label_parity(en_text)
        tr_text = _chapter_label_parity(tr_text)

        # Count structural elements: \chapter, \section, \subsection, \subsubsection
        struct_cmds = [
            ("chapter", r"\\chapter\b"),
            ("section", r"\\section\b"),
            ("subsection", r"\\subsection\b"),
            ("subsubsection", r"\\subsubsection\b"),
        ]

        for name, pattern in struct_cmds:
            en_count = len(re.findall(pattern, en_text))
            tr_count = len(re.findall(pattern, tr_text))

            if en_count == 0:
                continue  # Nothing to compare

            diff = en_count - tr_count
            if diff == 0:
                continue

            # Missing sections are more severe than extra ones
            if diff > 0:
                # TR has fewer -- content loss
                severity = "critical" if abs(diff) >= 2 else "high"
                message = (
                    f"Information loss: {name} count EN={en_count}, "
                    f"TR={tr_count} ({diff} missing in translation)"
                )
            else:
                # TR has more -- possible spurious splits
                severity = "medium"
                message = (
                    f"Information gain: {name} count EN={en_count}, "
                    f"TR={tr_count} ({abs(diff)} extra in translation)"
                )

            report.issues.append(QCIssue(
                check_name="information_loss",
                severity=severity,
                line_num=1,
                message=message,
                auto_fixable=False,
                confidence=0.90,
                fix_suggestion=(
                    f"Verify that all {name} elements are present "
                    f"in the translation"
                ),
                context=f"en_{name}={en_count},tr_{name}={tr_count}",
            ))

    # ------------------------------------------------------------------
    # Check 15: Equation Count (total equation environments)
    # ------------------------------------------------------------------

    def _check_equation_count(
        self,
        tr_text: str,
        en_text: str,
        tr_lines: List[str],
        en_lines: List[str],
        report: QCReport,
    ) -> None:
        """Compare total equation environment count between EN and TR.

        This is a broader check than formula_integrity -- it counts ALL
        equation-like environments as a single aggregate number.
        Any difference indicates potential formula loss or duplication.
        """
        if not en_text:
            logger.debug("No source body -- skipping equation_count check.")
            return

        # Count all equation-like environments
        eq_envs = [
            r"\\begin\{equation\*?\}",
            r"\\begin\{align\*?\}",
            r"\\begin\{gather\*?\}",
            r"\\begin\{multline\*?\}",
            r"\\begin\{flalign\*?\}",
            r"\\begin\{alignat\*?\}",
            r"\\begin\{eqnarray\*?\}",
        ]

        en_total = sum(len(re.findall(p, en_text)) for p in eq_envs)
        tr_total = sum(len(re.findall(p, tr_text)) for p in eq_envs)

        # Also count display math \[...\] — exclude escaped references like \[4\]
        _display_math_re = re.compile(
            r"\\\[(?!\s*\d+\s*\\\])"  # \[ NOT followed by just digits then \]
        )
        en_display = len(_display_math_re.findall(en_text))
        tr_display = len(_display_math_re.findall(tr_text))

        en_all = en_total + en_display
        tr_all = tr_total + tr_display

        if en_all == 0:
            return

        diff = en_all - tr_all
        if diff == 0:
            return

        pct = abs(diff) / max(1, en_all) * 100

        if pct > 10:
            severity = "high"
        elif pct > 5:
            severity = "medium"
        else:
            severity = "low"

        report.issues.append(QCIssue(
            check_name="equation_count",
            severity=severity,
            line_num=1,
            message=(
                f"Total equation count mismatch: EN={en_all} "
                f"(envs={en_total}, display={en_display}), "
                f"TR={tr_all} (envs={tr_total}, display={tr_display}), "
                f"diff={diff:+d} ({pct:.1f}%)"
            ),
            auto_fixable=False,
            confidence=0.85,
            fix_suggestion=(
                "Compare equation environments line-by-line to find "
                "missing or duplicated equations"
            ),
            context=f"en_total={en_all},tr_total={tr_all}",
        ))

    # ------------------------------------------------------------------
    # Check 16: Figure Reference Integrity
    # ------------------------------------------------------------------

    def _check_figure_reference_integrity(
        self,
        tr_text: str,
        en_text: str,
        tr_lines: List[str],
        en_lines: List[str],
        report: QCReport,
    ) -> None:
        r"""Compare \includegraphics and figure references between EN and TR.

        Checks:
        1. \includegraphics count parity
        2. \includegraphics file paths match
        3. \ref{fig:...} reference count
        """
        if not en_text:
            logger.debug(
                "No source body -- skipping figure_reference_integrity check."
            )
            return

        # 1. \includegraphics count
        en_graphics = re.findall(
            r"\\includegraphics\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}", en_text,
        )
        tr_graphics = re.findall(
            r"\\includegraphics\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}", tr_text,
        )

        en_gfx_count = len(en_graphics)
        tr_gfx_count = len(tr_graphics)

        if en_gfx_count != tr_gfx_count:
            diff = en_gfx_count - tr_gfx_count
            severity = "critical" if abs(diff) >= 3 else "high"
            report.issues.append(QCIssue(
                check_name="figure_reference_integrity",
                severity=severity,
                line_num=1,
                message=(
                    f"\\includegraphics count mismatch: "
                    f"EN={en_gfx_count}, TR={tr_gfx_count} (diff={diff:+d})"
                ),
                auto_fixable=False,
                confidence=0.95,
                fix_suggestion=(
                    "Restore missing \\includegraphics from EN source"
                ),
                context="",
            ))

        # 2. Compare file paths (normalized)
        en_paths = set(p.strip() for p in en_graphics)
        tr_paths = set(p.strip() for p in tr_graphics)
        missing_paths = en_paths - tr_paths

        if missing_paths:
            # Report up to 5 missing paths
            sample = sorted(missing_paths)[:5]
            report.issues.append(QCIssue(
                check_name="figure_reference_integrity",
                severity="high",
                line_num=1,
                message=(
                    f"{len(missing_paths)} image path(s) in EN but not in TR: "
                    f"{', '.join(sample)}"
                    + (f" (+{len(missing_paths)-5} more)"
                       if len(missing_paths) > 5 else "")
                ),
                auto_fixable=False,
                confidence=0.90,
                fix_suggestion=(
                    "Ensure all image file references are preserved"
                ),
                context="",
            ))

        # 3. \ref{fig:...} count
        en_fig_refs = len(re.findall(r"\\ref\{fig:[^}]*\}", en_text))
        tr_fig_refs = len(re.findall(r"\\ref\{fig:[^}]*\}", tr_text))

        if en_fig_refs != tr_fig_refs and en_fig_refs > 0:
            diff = en_fig_refs - tr_fig_refs
            severity = "medium" if abs(diff) <= 3 else "high"
            report.issues.append(QCIssue(
                check_name="figure_reference_integrity",
                severity=severity,
                line_num=1,
                message=(
                    f"Figure \\ref{{fig:...}} count mismatch: "
                    f"EN={en_fig_refs}, TR={tr_fig_refs} (diff={diff:+d})"
                ),
                auto_fixable=False,
                confidence=0.85,
                fix_suggestion=(
                    "Verify that all \\ref{fig:...} references are "
                    "preserved in translation"
                ),
                context="",
            ))

    # ------------------------------------------------------------------
    # Check 17: Section Number Consistency
    # ------------------------------------------------------------------

    # Regex to extract section/chapter numbers from headings.
    # Matches patterns like: \section{4.4.2 Title} or \subsection*{3.1 Foo}
    _SECTION_NUM_RE = re.compile(
        r"\\(?:chapter|section|subsection|subsubsection)\*?\s*\{"
        r"\s*(\d+(?:\.\d+)*)\s+"
    )

    def _check_section_number_consistency(
        self,
        tr_text: str,
        en_text: str,
        tr_lines: List[str],
        en_lines: List[str],
        report: QCReport,
    ) -> None:
        """Compare section/chapter numbering between EN and TR.

        Extracts numeric prefixes from heading commands (e.g. ``4.4.2``
        from ``\\section{4.4.2 Overexpansion}``) and checks that the
        same numbers appear in the translation in the same order.
        Any mismatch is CRITICAL because it breaks the document
        structure.
        """
        if not en_text:
            logger.debug(
                "No source body -- skipping section_number_consistency check."
            )
            return

        # 2026-09-24 (I2): sanitizer P2 section önekini çeviriden BİLEREK
        # siler (LaTeX kendisi numaralar; çift numara önlenir). QC kaynağı
        # olduğu gibi karşılaştırınca her numaralı başlık "<missing>" diye
        # CRITICAL oluyordu (bir kitapta 22 + 1 sayım = 23 sahte CRITICAL, skor
        # 0/100'ün asıl nedeni). Çeviride silinebilir önek kalmamışsa
        # sanitizer çalışmıştır: AYNI kural (tek kaynak) kaynağa da
        # uygulanır, kalan numaralar (\chapter) karşılaştırılır. Önek hâlâ
        # duruyorsa (sanitize edilmemiş gövde) iki taraf ham karşılaştırılır
        # — MT'nin değiştirdiği numara ("4.4.2" -> "4.4.1") yakalanmaya devam
        # eder. Yıldızlı başlıktaki numara metnin parçasıdır, iki tarafta da
        # kalır ve karşılaştırılır. Bölüm etiketi kuralı da iki tarafa.
        from latex_pipeline.sanitizer import SECTION_NUM_PREFIX_RE
        en_text = _chapter_label_parity(en_text)
        tr_text = _chapter_label_parity(tr_text)
        if not SECTION_NUM_PREFIX_RE.search(tr_text):
            en_text = SECTION_NUM_PREFIX_RE.sub(r"\1", en_text)

        en_nums = self._SECTION_NUM_RE.findall(en_text)
        tr_nums = self._SECTION_NUM_RE.findall(tr_text)

        if not en_nums:
            return  # No numbered sections in source

        # Build ordered lists and compare element-by-element
        max_len = max(len(en_nums), len(tr_nums))
        for idx in range(max_len):
            en_val = en_nums[idx] if idx < len(en_nums) else "<missing>"
            tr_val = tr_nums[idx] if idx < len(tr_nums) else "<missing>"

            if en_val != tr_val:
                # Find approximate line number in TR
                line_num = 1
                if idx < len(tr_nums):
                    # Search for this heading in TR text
                    for li, line in enumerate(tr_lines, 1):
                        if re.search(
                            r"\\(?:chapter|section|subsection|subsubsection)"
                            r"\*?\s*\{\s*" + re.escape(tr_val),
                            line,
                        ):
                            line_num = li
                            break

                report.issues.append(QCIssue(
                    check_name="section_number_consistency",
                    severity="critical",
                    line_num=line_num,
                    message=(
                        f"Section number mismatch at position {idx+1}: "
                        f"EN='{en_val}', TR='{tr_val}'"
                    ),
                    # Report-only: the AutoFixer has no handler for
                    # 'section_number_consistency', so leaving this True kept
                    # convergence from ever being reached (QC-N7).
                    auto_fixable=False,
                    confidence=0.95,
                    fix_suggestion=(
                        f"Replace section number '{tr_val}' with '{en_val}' "
                        f"to match original"
                    ),
                    context=f"en_nums={en_nums}, tr_nums={tr_nums}",
                ))

        # Also flag if total count differs
        if len(en_nums) != len(tr_nums):
            diff = len(en_nums) - len(tr_nums)
            report.issues.append(QCIssue(
                check_name="section_number_consistency",
                severity="critical",
                line_num=1,
                message=(
                    f"Numbered section count mismatch: "
                    f"EN={len(en_nums)}, TR={len(tr_nums)} (diff={diff:+d})"
                ),
                auto_fixable=False,
                confidence=0.90,
                fix_suggestion=(
                    "Check for missing or extra numbered sections"
                ),
                context="",
            ))

    # ------------------------------------------------------------------
    # Check 18: Href Preservation
    # ------------------------------------------------------------------

    _HREF_RE = re.compile(r"\\href\s*\{")
    # Marker OCR artifact: \href{#page-N-0}{...} -- internal PDF page refs,
    # not real hyperlinks.  Counts vary between EN/TR so we exclude them.
    _HREF_PAGE_ARTIFACT_RE = re.compile(r"\\href\s*\{#page-\d+-\d+\}")

    def _check_href_preservation(
        self,
        tr_text: str,
        en_text: str,
        tr_lines: List[str],
        en_lines: List[str],
        report: QCReport,
    ) -> None:
        r"""Compare \href count between EN and TR.

        If the original has N ``\href`` commands and the translation
        has M, and ``|N - M| > 2``, flag as HIGH severity.
        A difference of 1-2 may be intentional (e.g. translator note
        links), but larger differences indicate dropped hyperlinks.

        ``#page-N-0`` refs (Marker OCR artifacts) are excluded from
        both counts since they are internal PDF page references, not
        real hyperlinks.
        """
        if not en_text:
            logger.debug(
                "No source body -- skipping href_preservation check."
            )
            return

        # Total href counts minus #page- artifacts
        en_count = (
            len(self._HREF_RE.findall(en_text))
            - len(self._HREF_PAGE_ARTIFACT_RE.findall(en_text))
        )
        tr_count = (
            len(self._HREF_RE.findall(tr_text))
            - len(self._HREF_PAGE_ARTIFACT_RE.findall(tr_text))
        )

        if en_count == 0 and tr_count == 0:
            return  # No hrefs in either

        diff = en_count - tr_count
        abs_diff = abs(diff)

        if abs_diff > 2:
            severity = "critical" if abs_diff > 5 else "high"
            report.issues.append(QCIssue(
                check_name="href_preservation",
                severity=severity,
                line_num=1,
                message=(
                    f"\\href count mismatch: "
                    f"EN={en_count}, TR={tr_count} (diff={diff:+d})"
                ),
                auto_fixable=False,
                confidence=0.90,
                fix_suggestion=(
                    "Restore missing \\href commands from EN source"
                ),
                context=f"en_href={en_count}, tr_href={tr_count}",
            ))
        elif abs_diff == 2:
            # Difference of 2 -- informational, low severity
            report.issues.append(QCIssue(
                check_name="href_preservation",
                severity="low",
                line_num=1,
                message=(
                    f"\\href count minor difference: "
                    f"EN={en_count}, TR={tr_count} (diff={diff:+d})"
                ),
                auto_fixable=False,
                confidence=0.70,
                fix_suggestion="Verify href difference is intentional",
                context="",
            ))
        # abs_diff == 1 is within noise (MT may restructure
        # parenthetical links or translator notes); not flagged.

    # ------------------------------------------------------------------
    # Check 19: Foreign Language Leakage
    # ------------------------------------------------------------------

    # Common foreign-language words (not English, not Turkish) that
    # indicate the MT model hallucinated in the wrong language.
    # Grouped by source language for clarity.
    # _FOREIGN_WORDS anahtarlarının core.lang_detect dil kodları (kulvar Q).
    _FOREIGN_LANG_CODES: Dict[str, str] = {
        "spanish": "es", "french": "fr", "german": "de",
        "italian": "it", "portuguese": "pt",
    }

    _FOREIGN_WORDS: Dict[str, List[str]] = {
        "spanish": [
            "como", "pero", "para", "donde", "cuando", "porque",
            "tiene", "puede", "desde", "hasta", "entre", "sobre",
            "tambien", "siempre", "nunca", "ahora", "entonces",
            "mismo", "cada", "otro", "esta", "estos", "estas",
            # 2026-09-24 (çok dilli kaynak): gerçekçi çevrilmemiş cümleyi
            # yakalamak için. "los/las" BİLEREK yok (Los Angeles, Las Vegas
            # özel adları Türkçe metinde meşru).
            "muy",
        ],
        "french": [
            "comme", "mais", "pour", "dans", "avec", "cette",
            "sont", "nous", "vous", "leur", "tout", "peut",
            "donc", "aussi", "bien", "fait", "encore", "entre",
            "sous", "chez", "selon", "apres", "avant",
            # 2026-09-24: "les/des" BİLEREK yok (kaynakça künyeleri, özel
            # adlar ve kısaltmalar: LES-6, DES).
            "une", "qui",
        ],
        "german": [
            "das", "und", "ist", "nicht", "auch", "noch",
            "wird", "kann", "oder", "aber", "nach", "wenn",
            "sich", "sind", "eine", "einem", "einen", "dieser",
            "diese", "durch", "werden", "haben", "wurde",
        ],
        "italian": [
            "della", "nella", "questo", "questa", "sono",
            "come", "anche", "essere", "stato", "molto",
            "quando", "perche", "sempre", "ancora", "senza",
            # 2026-09-24: İtalyanca cümlenin en sık işlev kelimeleri — eski
            # liste gerçekçi bir çevrilmemiş cümlede tek eşleşme veriyordu
            # (eşik 2). "che" BİLEREK yok (Che Guevara, tablo kısaltmaları).
            "gli", "sia", "degli", "delle", "nel", "sul", "dei", "alla",
            "questi",
        ],
        "portuguese": [
            "como", "para", "mais", "quando", "muito",
            "tambem", "ainda", "desde", "porque", "pode",
            "entre", "sobre", "depois", "antes", "sempre",
        ],
    }

    # Words that exist in multiple languages and should not trigger
    # false positives.  "de", "en", "et" etc. are common in Turkish
    # (de/da = too/also, en = most), English, and other languages.
    _FOREIGN_WHITELIST = {
        "de", "da", "en", "et", "le", "la", "un", "a", "o",
        "ne", "se", "son", "son", "su", "hat", "bin", "an",
        "was", "her", "die", "den", "dem", "van", "von",
        "can", "hat", "nur", "um", "am", "mal", "dir",
        # Common Turkish words that collide with Spanish/Portuguese lists:
        # "para" = money / "for each"; "hasta" = patient / "until".
        # Guaranteed false positives in finance and medical books (QC-C8).
        "para", "hasta",
    }

    # CJK character regex for foreign language leakage check.
    # Detects Chinese, Japanese, Korean characters that are HY-MT 7B
    # training data artifacts (e.g. "如图de").
    _CJK_RE = re.compile(
        r"[\u4e00-\u9fff"        # CJK Unified Ideographs (Chinese)
        r"\u3000-\u303f"         # CJK Punctuation
        r"\u3040-\u309f"         # Hiragana (Japanese)
        r"\u30a0-\u30ff"         # Katakana (Japanese)
        r"\uac00-\ud7af"         # Korean Hangul Syllables
        r"]+"
    )

    def _check_foreign_language_leakage(
        self,
        tr_text: str,
        tr_lines: List[str],
        report: QCReport,
    ) -> None:
        """Detect non-English, non-Turkish foreign words in translation.

        Scans the Turkish translation for common Spanish, French, German,
        Italian, and Portuguese words, as well as CJK (Chinese/Japanese/
        Korean) character artifacts from HY-MT 7B training data.  These
        indicate the MT model switched to the wrong target language or
        leaked training data.
        """
        # --- CJK character artifact check ---
        cjk_hits: List[Tuple[int, str]] = []
        for line_idx, raw_line in enumerate(tr_lines, 1):
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("%"):
                continue
            cjk_matches = self._CJK_RE.findall(stripped)
            for cjk_frag in cjk_matches:
                cjk_hits.append((line_idx, cjk_frag))

        if cjk_hits:
            unique_frags = sorted(set(f for _, f in cjk_hits))
            first_line = cjk_hits[0][0]
            report.issues.append(QCIssue(
                check_name="foreign_language_leakage",
                severity="high",
                line_num=first_line,
                message=(
                    f"CJK character artifact detected: "
                    f"{len(cjk_hits)} occurrence(s) of "
                    f"{', '.join(unique_frags[:8])}"
                    + (f" (+{len(unique_frags)-8} more)"
                       if len(unique_frags) > 8 else "")
                ),
                auto_fixable=True,
                confidence=0.99,
                fix_suggestion=(
                    "Remove CJK characters (HY-MT training data artifact)"
                ),
                context=f"lines: {[h[0] for h in cjk_hits[:10]]}",
            ))

        # --- European language word check ---
        # Build lookup: word -> language
        word_to_lang: Dict[str, str] = {}
        for lang, words in self._FOREIGN_WORDS.items():
            for w in words:
                if w not in self._FOREIGN_WHITELIST:
                    word_to_lang[w] = lang

        # Track findings per language to avoid flooding the report
        findings: Dict[str, List[Tuple[int, str]]] = {}  # lang -> [(line, word)]

        for line_idx, raw_line in enumerate(tr_lines, 1):
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("%"):
                continue
            # Skip math environments, code blocks, URLs, commands
            masked = self._mask_for_language_check(stripped)
            if len(masked) < 10:
                continue

            # 2026-09-24: kelime listeleri aksansız (tambien, perche, apres);
            # Türkçede olmayan aksanlar katlanır ki "también", "perché",
            # "está" eşleşsin. ö/ü/ç/ş/ğ/ı KATLANMAZ (Türkçe "öder" ->
            # Almanca "oder" gibi sahte eşleşme olmasın).
            words_in_line = re.findall(
                r"\b([a-z]{3,})\b",
                masked.lower().translate(_FOREIGN_ACCENT_FOLD),
            )
            line_hits = [(word, word_to_lang[word]) for word in words_in_line
                         if word in word_to_lang]
            if not line_hits:
                continue
            # 2026-09-25 (kulvar Q): kaynakta da bu dilde AYNEN geçen alıntı
            # satırı sızıntı değildir (Check 2 ile aynı kural).
            line_lang = Counter(l for _, l in line_hits).most_common(1)[0][0]
            line_code = self._FOREIGN_LANG_CODES.get(line_lang)
            if self._is_source_foreign_quote(raw_line, line_code):
                continue
            # 2026-09-28 (kulvar Q): kaynağın kendi dilinde kalmış ve Check
            # 2'nin zaten "untranslated" saydığı satır burada ikinci kez
            # sayılmaz (aynı bulgu: çevrilmemiş kaynak metni).
            if self._is_untranslated_source_line(line_idx, line_code):
                continue
            # Kulvar G4: satırdaki kaynak dilde cümleyi
            # source_language_sentence zaten raporladı (aynı bulgu).
            if (line_idx in self._src_sentence_lines
                    and self._src_ctx is not None
                    and _same_language(line_code, self._src_ctx.lang)):
                continue
            for word, lang in line_hits:
                findings.setdefault(lang, []).append((line_idx, word))

        # Report per language
        for lang, hits in findings.items():
            if len(hits) < 2:
                continue  # Single hit is likely false positive

            # Deduplicate by unique words
            unique_words = sorted(set(w for _, w in hits))
            first_line = hits[0][0]

            # Foreign language leakage is a high-confidence check;
            # even a single repeated foreign word is reliable evidence.
            conf = 0.90

            report.issues.append(QCIssue(
                check_name="foreign_language_leakage",
                severity="medium",
                line_num=first_line,
                message=(
                    f"{lang.title()} language leakage detected: "
                    f"{len(hits)} occurrence(s) of "
                    f"{', '.join(unique_words[:8])}"
                    + (f" (+{len(unique_words)-8} more)"
                       if len(unique_words) > 8 else "")
                ),
                auto_fixable=True,
                confidence=conf,
                fix_suggestion=(
                    f"Re-translate lines containing {lang} words"
                ),
                context=f"lines: {[h[0] for h in hits[:10]]}",
            ))

    # ------------------------------------------------------------------
    # Check 20: Antonym Guard
    # ------------------------------------------------------------------

    # Antonym pairs: (english_term, wrong_turkish, correct_turkish)
    # If EN source contains english_term and TR contains wrong_turkish,
    # that's a critical antonym swap error.
    # QC-N3 veri onarimi (3 Temmuz 2026):
    # - wrong_tr eslesmesi .lower() substring oldugu icin hem ASCII hem
    #   Turkce-karakterli varyantlar ayri satir olarak listelenir (MT ciktisi
    #   iki formda da gelebilir; ASCII-only satirlar gercek Turkce metni
    #   yakalayamiyordu).
    # - correct_tr dokumana YAZILAN deger -> duzgun Turkce olmali
    #   (eski "ses alti"/"onyargi"/"turbulansl" ASCII-bozuk/kesikti).
    # - ("bias","error","hata","onyargi") cifti SILINDI: "hata" asiri-genel,
    #   ML baglaminda bias->"önyargı" zorlamasi yanlis (bkz. YOL_B raporu).
    # - Olu _ANTONYM_PAIRS_TURKISH listesi kaldirildi (hicbir yerden
    #   okunmuyordu).
    _ANTONYM_PAIRS: List[Tuple[str, str, str, str]] = [
        # (en_term, en_antonym, wrong_tr, correct_tr)
        # Subsonic vs Supersonic
        ("subsonic", "supersonic", "supersonik", "ses altı"),
        ("subsonic", "supersonic", "ses ustu", "ses altı"),
        ("subsonic", "supersonic", "ses üstü", "ses altı"),
        ("supersonic", "subsonic", "ses alti", "ses üstü"),
        ("supersonic", "subsonic", "ses altı", "ses üstü"),
        ("supersonic", "subsonic", "subsonik", "ses üstü"),
        # Over/Under expansion
        ("overexpansion", "underexpansion",
         "yetersiz genisleme", "aşırı genişleme"),
        ("overexpansion", "underexpansion",
         "yetersiz genişleme", "aşırı genişleme"),
        ("underexpansion", "overexpansion",
         "asiri genisleme", "yetersiz genişleme"),
        ("underexpansion", "overexpansion",
         "aşırı genişleme", "yetersiz genişleme"),
        # Convergent vs Divergent
        ("convergent", "divergent", "genisleyen", "daralan"),
        ("convergent", "divergent", "genişleyen", "daralan"),
        ("convergent", "divergent", "iraksak", "yakınsak"),
        ("convergent", "divergent", "ıraksak", "yakınsak"),
        ("divergent", "convergent", "daralan", "genişleyen"),
        ("divergent", "convergent", "yakinsak", "ıraksak"),
        ("divergent", "convergent", "yakınsak", "ıraksak"),
        # Laminar vs Turbulent
        ("laminar", "turbulent", "turbulansl", "laminer"),
        ("laminar", "turbulent", "türbülanslı", "laminer"),
        ("turbulent", "laminar", "laminer", "türbülanslı"),
        # Endothermic vs Exothermic
        ("endothermic", "exothermic", "ekzotermik", "endotermik"),
        ("exothermic", "endothermic", "endotermik", "ekzotermik"),
    ]

    # 2026-09-25 (kulvar Q): kaynak terimin öteki beş kaynak dildeki
    # biçimleri. İngilizce biçim eskisi gibi TAM kelimedir (desen İngilizce
    # metinde birebir aynı eşleşir); ek biçimler İngilizcede geçmez.
    _ANTONYM_SOURCE_FORMS: Dict[str, str] = {
        "subsonic": (r"subsonic|subsoniques?|unterschall\w*|subsónic[oa]s?"
                     r"|subsonic[oaie]|дозвуков\w*"),
        "supersonic": (r"supersonic|supersoniques?|überschall\w*"
                       r"|supersónic[oa]s?|supersonic[oaie]|сверхзвуков\w*"),
        "overexpansion": (r"overexpansion|surdétente|überexpansion"
                          r"|sobreexpansión|sovraespansione"
                          r"|перерасширени\w*"),
        "underexpansion": (r"underexpansion|sous-détente|unterexpansion"
                           r"|subexpansión|sottoespansione"
                           r"|недорасширени\w*"),
        "convergent": (r"convergent|convergentes?|konvergente?[nrms]?"
                       r"|convergent[ei]|сужающ\w*"),
        "divergent": (r"divergent|divergentes?|divergente[nrms]?"
                      r"|divergent[ei]|расширяющ\w*"),
        "laminar": (r"laminar|laminaires?|laminare[ns]?|laminares"
                    r"|laminar[ei]|ламинарн\w*"),
        "turbulent": (r"turbulent|turbulentes?|turbulente[nrm]"
                      r"|turbulent[oa]s?|turbolent[oiae]|турбулентн\w*"),
        "endothermic": (r"endothermic|endothermiques?|endotherme?[nrms]?"
                        r"|endotérmic[oa]s?|endotermic[oaie]"
                        r"|эндотермическ\w*"),
        "exothermic": (r"exothermic|exothermiques?|exotherme?[nrms]?"
                       r"|exotérmic[oa]s?|esotermic[oaie]"
                       r"|экзотермическ\w*"),
    }

    @classmethod
    @functools.lru_cache(maxsize=None)
    def _antonym_source_re(cls, term: str) -> "re.Pattern":
        """Kaynak terim deseni (altı kaynak dil); listede yoksa tam kelime."""
        alt = cls._ANTONYM_SOURCE_FORMS.get(term, re.escape(term))
        return re.compile(r"\b(?:" + alt + r")\b", re.IGNORECASE)

    def _check_antonym_guard(
        self,
        tr_text: str,
        en_text: str,
        tr_lines: List[str],
        en_lines: List[str],
        report: QCReport,
    ) -> None:
        """Detect antonym substitution errors in translation.

        For each known antonym pair, if the English source contains
        term A but the Turkish translation contains the Turkish
        translation of term B (the opposite), flag as CRITICAL.

        Example: EN has "subsonic" but TR says "supersonik" instead
        of "ses alti" -- this is a meaning-reversing error.
        """
        if not en_text:
            logger.debug(
                "No source body -- skipping antonym_guard check."
            )
            return

        # Line-level antonym detection: only flag when the EN line
        # contains en_term AND the corresponding TR line contains wrong_tr.
        # This prevents false positives (e.g. "hata" for "error" flagged
        # because "bias" exists elsewhere in the document).
        n_en = len(en_lines)
        n_tr = len(tr_lines)
        # 2026-09-28 (kulvar Q): baştaki yorum/boş satır bloğu (Yol A
        # KITAPCEVIR-META başlığı) satır eşlemesini kaydırmasın.
        line_shift = (self._leading_comment_lines(en_lines)
                      - self._leading_comment_lines(tr_lines))
        tr_lower_all = tr_text.lower()
        src_ant_counts: Dict[str, int] = {}

        for en_term, en_antonym, wrong_tr, correct_tr in self._ANTONYM_PAIRS:
            wrong_count = tr_lower_all.count(wrong_tr)
            if not wrong_count:
                continue  # hiçbir satırda yok: eski döngü de bulgu üretmezdi
            en_pat = self._antonym_source_re(en_term)
            en_ant_pat = self._antonym_source_re(en_antonym)

            # 2026-09-25 (kulvar Q): çevirideki "yanlış" terimin her geçişi
            # kaynaktaki karşıt terimin bir geçişiyle açıklanabiliyorsa
            # (TR 'türbülanslı' sayısı <= kaynak 'turbulent/turbulento'
            # sayısı) takas kanıtı yoktur. Satır-konumlu ±2 pencere,
            # dipnotların yer değiştirdiği belgede başka cümleye düşüyordu
            # (dil_sent_es: "si el flujo es laminar" <-> "türbülanslı
            # rejimde"); kaynaktaki 'turbulento' İngilizce desene de
            # uymadığından "ikisi de var" istisnası da çalışmıyordu.
            #
            # 2026-09-28 (kulvar Q): muafiyet ek olarak kaynaktaki terimin
            # HER geçişinin çeviride doğru karşılıkla (correct_tr) durmasını
            # ister. Yalnız sayı karşılaştırması gerçek bir takası
            # gizleyebiliyordu: kaynak 1 'laminar' + 2 'turbulent'; çeviri
            # 'laminar'ı 'türbülanslı', bir 'turbulent'i 'türbülans' yazınca
            # TR 'türbülanslı' sayısı (2) kaynak 'turbulent' sayısını (2)
            # aşmıyordu. Doğru karşılık eksikse satır düzeyi denetime geçilir.
            for _term, _pat in ((en_antonym, en_ant_pat), (en_term, en_pat)):
                if _term not in src_ant_counts:
                    src_ant_counts[_term] = len(_pat.findall(en_text))
            if (wrong_count <= src_ant_counts[en_antonym]
                    and tr_lower_all.count(correct_tr)
                    >= src_ant_counts[en_term]):
                continue

            # Find all TR lines containing the wrong translation
            for tr_idx, tr_line in enumerate(tr_lines):
                tr_line_lower = tr_line.lower()
                if wrong_tr not in tr_line_lower:
                    continue

                # Check corresponding EN lines (same index +/- 2 window)
                found_en_term = False
                both_terms_present = False
                for offset in range(0, 3):
                    for sign in (0, -1, 1) if offset == 0 else (-1, 1):
                        en_idx = tr_idx + line_shift + offset * sign
                        if 0 <= en_idx < n_en:
                            en_line = en_lines[en_idx]
                            if en_pat.search(en_line):
                                found_en_term = True
                                # If BOTH term and antonym exist in same EN line,
                                # both translations are expected -- not an error
                                if en_ant_pat.search(en_line):
                                    both_terms_present = True
                                break
                    if found_en_term:
                        break

                if found_en_term and not both_terms_present:
                    report.issues.append(QCIssue(
                        check_name="antonym_guard",
                        severity="critical",
                        line_num=tr_idx + 1,
                        message=(
                            f"Antonym swap: EN '{en_term}' translated as "
                            f"'{wrong_tr}' (antonym of '{en_antonym}'). "
                            f"Should be '{correct_tr}'"
                        ),
                        auto_fixable=True,
                        confidence=0.95,
                        fix_suggestion=(
                            f"Replace '{wrong_tr}' with '{correct_tr}'"
                        ),
                        context=f"EN term: {en_term}",
                    ))

    # ------------------------------------------------------------------
    # Check 21: First Person Academic
    # ------------------------------------------------------------------

    # Turkish first-person singular verb forms and pronouns that
    # should not appear in academic/scientific text.
    _FIRST_PERSON_PATTERNS: List[str] = [
        # 1st person singular pronouns/suffixes
        r"\bben\b",
        r"\bbenim\b",
        r"\bbana\b",
        r"\bbeni\b",
        r"\bbende\b",
        r"\bbenden\b",
        # 1st person singular verb endings (safe generic patterns)
        # -iyorum/-uyorum/-ıyorum/-üyorum (always verb, never noun)
        r"\b\w+[iuıü]yorum\b",
        # -iyordum/-uyordum (1. tekil) ve -iyorduk (1. çoğul) hikâye
        # kipi. 2026-09-25 (kulvar Q): eski desen "yordun?" idi; 3. tekil
        # "-yordu"yu (alıyordu, bulunuyordu) ve 2. tekil "-yordun"u
        # yakalıyor, "-yordum"u ise HİÇ yakalamıyordu (\b 'm'den önce
        # düşmüyor). Rusça/Almanca/Fransızca kaynaklı anlatıda her
        # "-yordu" sahte birinci şahıs uyarısı oluyordu.
        r"\b\w+[iuıü]yordu[mk]\b",
        # -ecegim/-acagim (future 1st person -- always verb)
        r"\b\w+[ae]ca[gğ][iıİ]m\b",
        # -ebilirim/-abilirim (ability 1st person -- always verb)
        r"\b\w+[ae]bilir[iı]m\b",
        # -maliyim/-meliyim (obligation 1st person -- always verb)
        r"\b\w+mal[iı]y[iı]m\b",
        # Specific 1st person past tense verbs (academic context)
        # NOTE: The old generic r"\b\w+[dt][iuae]m\b" was removed because
        # it matched Turkish nouns like "egitim", "yontem", "sistem",
        # "adim", "tanim", "terim" causing ~90% false positives.
        r"\byapt[iı]m\b",
        r"\bg[oö]rd[uü]m\b",
        r"\byazd[iı]m\b",
        r"\bokudum\b",
        r"\bbuldum\b",
        r"\bdenedim\b",
        r"\bkulland[iı]m\b",
        r"\binceledim\b",
        r"\banaliz\s+ettim\b",
        r"\btest\s+ettim\b",
        r"\bgeli[sş]tirdim\b",
        r"\btasarlad[iı]m\b",
        r"\bg[oö]zlemledim\b",
        r"\b[oö]l[cç]t[uü]m\b",
        r"\bhesaplad[iı]m\b",
        r"\belde\s+ettim\b",
        r"\banlad[iı]m\b",
        # Specific high-frequency academic violations (ASCII forms)
        r"\bd[uü][sş][uü]n[uü]yorum\b",
        r"\bsan[iı]yorum\b",
        r"\bg[oö]r[uü]yorum\b",
        r"\bs[oö]yleyebilirim\b",
        r"\bbelirtmeliyim\b",
        # Turkish special char forms
        r"\banl[iı]yorum\b",
        r"\bbiliyorum\b",
    ]

    # Compile once
    _FIRST_PERSON_RE = re.compile(
        "|".join(_FIRST_PERSON_PATTERNS), re.IGNORECASE
    )

    def _check_first_person_academic(
        self,
        tr_text: str,
        tr_lines: List[str],
        report: QCReport,
    ) -> None:
        """Detect first-person singular expressions in academic text.

        Academic/scientific texts should use impersonal or third-person
        constructions.  First-person forms like "anliyorum" (I understand),
        "yapacagim" (I will do), "biliyorum" (I know) indicate poor
        translation quality for academic registers.

        Skipped for non-academic book types where first person is natural
        (e.g. philosophy, novel, self_help, memoir, biography).
        """
        # Non-academic genres: first person is natural and expected
        _NON_ACADEMIC_TYPES = {
            "novel", "philosophy", "self_help",
            "memoir", "biography", "literary", "psychology",
        }
        if self._book_type and self._book_type in _NON_ACADEMIC_TYPES:
            return

        # 2026-09-25 (kulvar Q): kaynak birinci tekil şahısla yazılmışsa
        # (İspanyolca deneme yazarı: "supongo"/"me"; Fransızca roman diyaloğu)
        # çevirideki birinci şahıs kaynağa sadakattir, MT sapması değildir.
        # book_type tespiti türü ıskalayabildiği için kanıt kaynaktan
        # (zamir yoğunluğu, bkz. FIRST_PERSON_SOURCE_*) alınır.
        ctx = self._src_ctx
        if ctx is not None and ctx.is_first_person_voice():
            return

        findings: List[Tuple[int, str]] = []

        for line_idx, raw_line in enumerate(tr_lines, 1):
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("%"):
                continue

            # Skip math, code, commands
            masked = self._mask_for_language_check(stripped)
            if len(masked) < 5:
                continue

            matches = self._FIRST_PERSON_RE.findall(masked.lower())
            for m in matches:
                findings.append((line_idx, m))

        if not findings:
            return

        # Group and report
        unique_forms = sorted(set(w for _, w in findings))
        first_line = findings[0][0]

        report.issues.append(QCIssue(
            check_name="first_person_academic",
            severity="medium",
            line_num=first_line,
            message=(
                f"First-person expressions in academic text: "
                f"{len(findings)} occurrence(s) -- "
                f"{', '.join(unique_forms[:6])}"
                + (f" (+{len(unique_forms)-6} more)"
                   if len(unique_forms) > 6 else "")
            ),
            auto_fixable=True,
            confidence=0.90,
            fix_suggestion=(
                "Replace first-person forms with impersonal/passive "
                "constructions (e.g. 'anliyorum' -> 'anlasilmaktadir')"
            ),
            context=f"lines: {[h[0] for h in findings[:10]]}",
        ))

    # ------------------------------------------------------------------
    # Check 22: Number Hallucination Guard
    # ------------------------------------------------------------------

    # Cross-reference labels and their Turkish translations
    _XREF_LABEL_MAP: Dict[str, str] = {
        "figure": "şekil", "fig": "şekil", "fig.": "şekil",
        "table": "tablo", "tab": "tablo", "tab.": "tablo",
        "definition": "tanım", "def": "tanım", "def.": "tanım",
        "theorem": "teorem", "thm": "teorem", "thm.": "teorem",
        "lemma": "lemma",
        "proposition": "önerme", "prop": "önerme",
        "corollary": "sonuç",
        "example": "örnek", "ex": "örnek", "ex.": "örnek",
        "exercise": "alıştırma",
        "algorithm": "algoritma",
        "section": "bölüm", "sec": "bölüm", "sec.": "bölüm",
        "chapter": "bölüm", "ch": "bölüm", "ch.": "bölüm",
        "equation": "denklem", "eq": "denklem", "eq.": "denklem",
        "remark": "not",
        "proof": "ispat",
        "property": "özellik",
    }

    # Regex to extract cross-ref labels + numbers from text
    # Pattern 1: Label + Number (e.g. "Lemma 2.6", "Teorem 2.25")
    # NOTE: [\s~] is used instead of \s because HY-MT 7B produces tilde
    # non-breaking spaces in cross-refs (e.g. "Önerme~2.17") and Python's
    # \s does NOT match ~ (U+007E).  Tilde is legal LaTeX (non-breaking
    # space) so the QC engine must treat it as whitespace in xref context.
    #
    # 2026-09-24 (M): iki düzeltme (bir test kitabında 5 sahte bulgu).
    # (1) "Şek." / "Denk." kısaltmaları: translator._FIGURE_LABEL_MAP
    #     "Fig." -> "Şek.", "Eq." -> "Denk." üretiyor; QC bunları tanımadığı
    #     için "Şek. 2.3" gibi doğru korunmuş referanslar "missing" CRITICAL
    #     oluyordu (4 adet).
    # (2) Türkçe kelime sırası: ek almış etiket (Teoremi, Bölümü, Denklemi)
    #     ardından gelen sayıya bağlı değildir — Türkçede referansın eki
    #     sayıdan SONRA gelir ("Teorem 2.5'te", "2.5. Teoremde"). "Yazar bu
    #     yöntemi 1887 yılında..." cümlesindeki yıl "halüsinasyon" diye HIGH
    #     alıyordu. Ek almış etiketten sonra yalnız NOKTALI numara ("Teoremde
    #     2.6" — MT'nin ürettiği bozuk ama referans taşıyan sıra) referans
    #     sayılır; çıplak etiket her numarayı alır ("Teorem 3", "Şekil 1.1").
    #     Çoğul eki hâl eki değildir: "Bölümler 10–12" her numarayı alır.
    #     Ek alabilen etiket aileleri eski desendekiyle AYNI (Şekil/Tablo/
    #     Tanım ek almaz: "şeklinde 2.5 kat" referans değildir).
    # İki yakalama grubu vardır; numara hangisi doluysa odur
    # (bkz. _xref_numbers).
    #
    # 2026-09-25 (dalga Q): etiket sözcükleri tek kaynaktan
    # (patterns.XREF_LABEL_WORDS). ÇEVİRİ tarafı deseni yalnız "en"
    # girdileriyle kurulur — 2026-09-24 deseniyle birebir aynı dizgedir.
    # KAYNAK tarafı deseni altı kaynak dilin (en, de, ru, fr, es, it)
    # etiketlerini de tanır: "Рис. 1.1", "Abb. 2", "Tabla 3.1" kaynakta
    # atıf sayılmıyordu; Rusça bölümde kontrol hiç çalışmıyordu.
    _XREF_NUM_RE = _build_xref_num_re(xref_label_alternation(langs=("en",)))
    _XREF_NUM_SRC_RE = _build_xref_num_re(xref_label_alternation())

    # 2026-09-28 (kulvar Q): çeviri tarafında KÜÇÜK harfli "şek." kısaltması
    # ("basınç artar (şek. 1.1)"). Çevirmen kaynaktaki "(fig. 1.1)"i böyle
    # yazınca atıf sayılmıyor, "1.1 eksik" CRITICAL çıkıyordu (dil_sent_es,
    # dil_sent_it). Küçük "şek." Türkçe bir kelime değildir (küçük "denk."
    # sıfat olduğu için onun karşılığı EKLENMEZ). Ayrı desen: _XREF_NUM_RE
    # dizgesi 2026-09-24 deseniyle birebir aynı kalmalı.
    _XREF_TR_LOWER_ABBREV_RE = re.compile(r"(?<!\w)şek\.[\s~]*(\d+(?:\.\d+)*)")

    @classmethod
    def _xref_numbers(cls, text: str, source: bool = False) -> List[str]:
        """Referans numaraları (iki grup birleşik), metindeki sırayla.

        ``source=False``: çeviri (Türkçe) tarafı, ``_XREF_NUM_RE`` +
        küçük "şek." kısaltması (``_XREF_TR_LOWER_ABBREV_RE``).
        ``source=True``: kaynak metin, ``_XREF_NUM_SRC_RE`` (altı kaynak dil).
        """
        if source:
            return [bare or suffixed
                    for bare, suffixed in cls._XREF_NUM_SRC_RE.findall(text)]
        return [num for _, num in cls._xref_number_spans(text)]

    # 2026-09-30 (kulvar F2): "örneğin" Türkçede çoğunlukla "for example"
    # bağlacıdır. İleri desenin ek almış etiket dalı ("Örne[kğ]\w+ 2.5")
    # "Örneğin 2.5 kat daha hızlı" cümlesindeki değeri atıf sayıyordu. Bu
    # biçim atıf olmaz ("Örnek 2.5'te" doğrusudur); çeviri tarafında atlanır.
    # (_XREF_NUM_RE dizgesi 2026-09-24 deseniyle birebir aynı kalmalı —
    # eleme eşleşmeden SONRA yapılır.)
    _XREF_TR_CONJ_LABEL_RE = re.compile(r"(?i:örneğin)(?![\w])")

    @classmethod
    def _xref_number_spans(cls, text: str) -> List[Tuple[int, str]]:
        """Çeviri tarafı ileri atıf numaraları: ``(konum, numara)``."""
        found: List[Tuple[int, str]] = []
        for m in cls._XREF_NUM_RE.finditer(text):
            if (m.group(2) is not None
                    and cls._XREF_TR_CONJ_LABEL_RE.match(text, m.start())):
                continue  # "Örneğin 2.5 ..." bağlaç + değer
            found.append((m.start(), m.group(1) or m.group(2)))
        found += [(m.start(), m.group(1))
                  for m in cls._XREF_TR_LOWER_ABBREV_RE.finditer(text)]
        return sorted(found)

    # Pattern 2: Number + Label (Turkish style: "2.6. Teoremde", "2.6 nolu Teorem")
    # Captures the number when it precedes a Turkish label
    # 2026-09-30 (kulvar F2): gruplar adlandırıldı; sayma ifadesi ve
    # "örneğin" bağlacı ayrımı için bkz. _reverse_xref_numbers.
    _XREF_NUM_REVERSE_RE = re.compile(
        r"(?P<num>\d+(?:\.\d+)*)"
        r"(?P<ord>\.)?[\s~]*(?P<nolu>(?:nolu|numaralı)[\s~]+)?"
        r"(?P<label>Şekil|Tablo|Tanım|Teorem\w*|Lemma\w*|Önerme\w*|Sonuç\w*"
        r"|Örne[kğ]\w*"
        r"|Alıştırma\w*|Algoritma\w*|Bölüm\w*|Denklem\w*|İspat\w*|Özellik\w*)",
        re.IGNORECASE,
    )

    @classmethod
    def _reverse_xref_numbers(
        cls, text: str,
    ) -> Tuple[List[Tuple[int, str]], List[Tuple[int, str]]]:
        """Türkçe ters sıralı atıflar ("2.6. Teoremde", "52. örnek",
        "3 nolu Şekil"): ``(güçlü, zayıf)`` listeleri, öğe ``(konum, numara)``.

        2026-09-30 (kulvar F2): Türkçede atıf numarası
        etiketin önünde SIRA SAYISIdır ("52. örnek", "2.6. Teoremde") ya da
        "nolu/numaralı" ile gelir. Çıplak tam sayı + ad bir SAYMA
        ifadesidir: "bu 40 örneği" (these 40 samples), "12 bölümden
        oluşur". "örneğin" ise bağlaçtır: "17 örneğin en az bir..."
        (kaynakta "17 for example": 17 düz metinde kalmış dipnot işareti).
        Eskiden üçü de atıf sayılıp "kaynakta yok" HIGH alıyordu.

        * Güçlü (atıf): sıra noktası, "nolu/numaralı" ya da noktalı numara
          ("2.6 Teoremde"). Hem eksik hem uydurma yönünde sayılır.
        * Zayıf: çıplak tam sayı + etiket (sayma ifadesi olabilir). Eksik
          yönünde çevirinin kanıtı sayılır (eski davranış), uydurma
          yönünde sayılmaz.
        * "örneğin" bağlacı (sıra noktası ve "nolu" yoksa, ya da ardından
          virgül geliyorsa: "... 52. Örneğin, ...") hiç sayılmaz. Sıra
          noktalı "52. örneğin çözümü" (örneğin = örnek + ilgi eki) atıftır.
        """
        strong: List[Tuple[int, str]] = []
        weak: List[Tuple[int, str]] = []
        for m in cls._XREF_NUM_REVERSE_RE.finditer(text):
            num = m.group("num")
            marked = bool(m.group("ord") or m.group("nolu"))
            if cls._XREF_TR_CONJ_LABEL_RE.fullmatch(m.group("label")):
                nxt = text[m.end():m.end() + 1]
                if not marked or nxt == ",":
                    continue
            if marked or "." in num:
                strong.append((m.start(), num))
            else:
                weak.append((m.start(), num))
        return strong, weak

    @staticmethod
    def _reconcile_ocr_dot_loss(missing_nums, hallucinated_nums,
                                en_counter, tr_counter):
        """OCR nokta kaybını eksik/uydurma numara listelerinden düşer.

        2026-09-25 (dalga Q): OCR "Рис. 1.1"i "Рис 11", "Рис. 1.11"i
        "Рис 111" okuyabiliyor; çevirmen doğru numarayı ("Şekil 1.1")
        yazınca kaynaktaki noktasız "11" eksik, çevirideki "1.1" fazla
        görünüyordu. Kural: kaynakta EKSİK kalan NOKTASIZ bir numara,
        çeviride FAZLA kalan ve noktaları atılınca ona eşit olan NOKTALI
        numara(lar)la adet adet eşleşir; eşleşen adet iki listeden de düşer.
        Noktalı kaynak numarası ("4.18" -> "1.18") uzlaştırılmaz — rakam
        değişimi gerçek fark olabilir. Sıfırla başlayan numaralar
        ("ch005" DOI kalıntısı, "0.05" p-değeri) atıf numarası değildir,
        eşleştirilmez.
        """
        surplus = {num: cnt - en_counter.get(num, 0)
                   for num, cnt in tr_counter.items()
                   if cnt > en_counter.get(num, 0)
                   and _OCR_DOTTED_REF_RE.fullmatch(num)}
        if not surplus:
            return missing_nums, hallucinated_nums
        by_digits: Dict[str, List[str]] = {}
        for num in sorted(surplus):
            by_digits.setdefault(num.replace(".", ""), []).append(num)
        kept_missing = []
        for num, en_count, tr_count, diff in missing_nums:
            remaining = diff
            if _OCR_DOTLESS_REF_RE.fullmatch(num):
                for cand in by_digits.get(num, ()):
                    take = min(surplus[cand], remaining)
                    surplus[cand] -= take
                    remaining -= take
                    if remaining == 0:
                        break
            if remaining > 0:
                kept_missing.append((num, en_count, tr_count, remaining))
        kept_halluc = []
        for num, tr_count in hallucinated_nums:
            left = surplus.get(num, tr_count)
            if left > 0:
                kept_halluc.append((num, left))
        return kept_missing, kept_halluc

    @classmethod
    def _ocr_spaced_variants(cls, en_text: str) -> Dict[str, Counter]:
        """Kaynakta noktası BOŞLUĞA dönmüş atıflar: ``{"1": {"1.13": 1}}``.

        2026-09-25 (kulvar Q): taranmış Rusça bir kitapta "Рис 1 13" ve
        "Рис. 1 2." gibi altyazılarda OCR noktayı boşluk okumuş; kaynak
        deseni çıplak "1"i yakalıyor, çevirmen "Şekil 1.13" yazınca "1"
        eksik (CRITICAL) görünüyordu. Yalnız ETİKETLİ çıplak numaranın
        hemen ardındaki 1-3 haneli sayı aday olur.
        """
        variants: Dict[str, Counter] = {}
        for m in cls._XREF_NUM_SRC_RE.finditer(en_text):
            bare = m.group(1)
            if not bare or "." in bare:
                continue
            tail = _OCR_SPACED_TAIL_RE.match(en_text, m.end())
            if tail:
                variants.setdefault(bare, Counter())[
                    f"{bare}.{tail.group(1)}"] += 1
        return variants

    @staticmethod
    def _ocr_prefix_variants(missing_nums, en_counter,
                             tr_counter) -> Dict[str, Counter]:
        """Baskı/OCR ön ek hatası adayları: ``{"4.18": {"1.18": 1}}``.

        2026-09-25 (kulvar Q): tek bölümlük belgede bütün atıflar
        "1.x" iken kaynak altyazı "Рис. 4.18" (metin içi atıf "рис. 1.18")
        basılmış; çevirmen "Şekil 1.18" yazınca "4.18 eksik" CRITICAL ve
        "4.18 öksüz" HIGH çıkıyordu. Kural (hepsi birden):
          * belge tek bölümlü: noktalı kaynak atıflarının en az
            ``OCR_PREFIX_DOMINANT_SHARE`` payı aynı ön eki (Q) taşır;
          * eksik numara P.S'nin ön eki P kaynakta YALNIZ bu numarada geçer
            (başka "P.x" atfı yok — gerçek bir bölüme atıf değil);
          * çeviride Q.S kaynaktakinden fazla geçer (fazlalık = düzeltme).
        Rakam değişimi (2.14 -> 2.12) ya da çok bölümlü belgede önek
        değişimi (4.18 -> 1.18, 4.x atıfları varken) uzlaştırılmaz.
        """
        dotted = Counter()
        for num, cnt in en_counter.items():
            if _OCR_DOTTED_REF_RE.fullmatch(num):
                dotted[num.split(".", 1)[0]] += cnt
        total = sum(dotted.values())
        if not total:
            return {}
        dominant, dom_n = dotted.most_common(1)[0]
        if dom_n / total < OCR_PREFIX_DOMINANT_SHARE:
            return {}
        variants: Dict[str, Counter] = {}
        for num, en_count, _tr_count, diff in missing_nums:
            if not _OCR_DOTTED_REF_RE.fullmatch(num):
                continue
            prefix, rest = num.split(".", 1)
            if prefix == dominant or dotted[prefix] != en_count:
                continue
            cand = f"{dominant}.{rest}"
            if tr_counter.get(cand, 0) > en_counter.get(cand, 0):
                variants[num] = Counter({cand: diff})
        return variants

    @staticmethod
    def _reconcile_ocr_variants(missing_nums, hallucinated_nums,
                                en_counter, tr_counter, variants):
        """Eksik kaynak numarasını çevirideki FAZLA aday numarayla adet
        adet eşleştirip iki listeden düşer (``_reconcile_ocr_dot_loss``
        ile aynı fazlalık muhasebesi; *variants* ``{eksik: {aday: üst
        sınır}}``). Dönüş: (kalan eksikler, kalan uydurmalar, uzlaşan
        kaynak numaraları)."""
        if not variants:
            return missing_nums, hallucinated_nums, set()
        surplus = {num: cnt - en_counter.get(num, 0)
                   for num, cnt in tr_counter.items()
                   if cnt > en_counter.get(num, 0)}
        reconciled: Set[str] = set()
        kept_missing = []
        for num, en_count, tr_count, diff in missing_nums:
            remaining = diff
            for cand, cap in sorted(variants.get(num, Counter()).items()):
                take = min(surplus.get(cand, 0), cap, remaining)
                if take > 0:
                    surplus[cand] -= take
                    remaining -= take
                    reconciled.add(num)
                if remaining == 0:
                    break
            if remaining > 0:
                kept_missing.append((num, en_count, tr_count, remaining))
        kept_halluc = []
        for num, tr_count in hallucinated_nums:
            left = surplus.get(num, tr_count)
            if left > 0:
                kept_halluc.append((num, min(left, tr_count)))
        return kept_missing, kept_halluc, reconciled

    def _check_number_hallucination(
        self,
        tr_text: str,
        en_text: str,
        tr_lines: List[str],
        en_lines: List[str],
        report: QCReport,
    ) -> None:
        """Compare cross-reference numbers between EN source and TR translation.

        Extracts all Figure/Table/Definition/Theorem/etc. + number patterns
        from both texts and checks that the same numbers appear. If the
        translation changed a number (e.g. Definition 2.14 → Tanım 2.12),
        this is flagged as CRITICAL because it corrupts academic references.
        """
        if not en_text:
            return

        # Sanitizer'ın sildiği "\section{Chapter 3}" etiketi kaynakta "3"
        # referansı sayılmasın: etiket kuralı iki tarafa da (tek kaynak).
        en_text = _chapter_label_parity(en_text)
        tr_text = _chapter_label_parity(tr_text)

        # Extract all cross-ref numbers from EN and TR
        # Use both label+number and number+label patterns for TR
        en_nums = self._xref_numbers(en_text, source=True)
        tr_spans_fwd = self._xref_number_spans(tr_text)
        tr_rev_strong, tr_rev_weak = self._reverse_xref_numbers(tr_text)
        # Uydurma yönü yalnız GÜÇLÜ atıflarla (kulvar F2): sayma ifadesi
        # ("bu 40 örneği") kaynakta yok diye uydurma sayılmaz.
        tr_strong_spans = sorted(tr_spans_fwd + tr_rev_strong)
        tr_nums = ([num for _, num in tr_strong_spans]
                   + [num for _, num in tr_rev_weak])
        tr_first_pos: Dict[str, int] = {}
        for pos, num in tr_strong_spans:
            tr_first_pos.setdefault(num, pos)

        if not en_nums:
            return  # No cross-references in source

        # Count occurrences of each number in EN and TR
        from collections import Counter
        en_counter = Counter(en_nums)
        tr_counter = Counter(tr_nums)

        # Find numbers that exist in EN but are missing or changed in TR
        missing_nums = []
        for num, en_count in en_counter.items():
            tr_count = tr_counter.get(num, 0)
            if tr_count < en_count:
                diff = en_count - tr_count
                missing_nums.append((num, en_count, tr_count, diff))

        # Find numbers in TR that don't exist in EN (hallucinated)
        # 2026-09-25 (dalga Q): kaynakta parantezli denklem atfı olarak geçen
        # numara ("Eq. (1.3)", "формулы (1.3)") çeviride "Denklem 1.3"
        # yazılınca uydurma sayılmaz.
        en_paren_eq = set(_XREF_PAREN_EQ_SRC_RE.findall(en_text))
        tr_strong_counter = Counter(num for _, num in tr_strong_spans)
        hallucinated_nums = []
        for num, tr_count in tr_strong_counter.items():
            if num not in en_counter and num not in en_paren_eq:
                hallucinated_nums.append((num, tr_count))

        missing_nums, hallucinated_nums = self._reconcile_ocr_dot_loss(
            missing_nums, hallucinated_nums, en_counter, tr_counter)

        # 2026-09-25 (kulvar Q): OCR'ın noktayı boşluğa çevirdiği ("Рис 1
        # 13") ve baskının ön eki bozduğu ("Рис. 4.18" tek bölümlü belgede)
        # kaynak atıflar, çevirmenin doğru numarasıyla uzlaştırılır.
        # Uzlaşan noktalı numaralar Check 29c'nin (öksüz noktalı numara)
        # aynı numarayı yeniden raporlamaması için kaydedilir.
        # Anahtarlar ayrık (boşluklu: çıplak numara, ön ek: noktalı);
        # tek çağrı, fazlalık muhasebesini ortak tutar.
        # Kaynak taraması yalnız eksik çıplak numara varsa (maliyet).
        _variants = (self._ocr_spaced_variants(en_text)
                     if any("." not in n for n, *_ in missing_nums) else {})
        _variants.update(self._ocr_prefix_variants(
            missing_nums, en_counter, tr_counter))
        missing_nums, hallucinated_nums, _ocr_ok = (
            self._reconcile_ocr_variants(
                missing_nums, hallucinated_nums, en_counter, tr_counter,
                _variants))
        self._ocr_corrected_src_nums.update(
            n for n in _ocr_ok if _OCR_DOTTED_REF_RE.fullmatch(n))

        if not missing_nums and not hallucinated_nums:
            return  # All numbers match

        # Report missing numbers
        for num, en_count, tr_count, diff in missing_nums[:5]:
            # Find line in TR where this number should appear
            line_num = 1
            for li, line in enumerate(tr_lines, 1):
                if num in line:
                    line_num = li
                    break

            report.issues.append(QCIssue(
                check_name="number_hallucination",
                severity="critical",
                line_num=line_num,
                message=(
                    f"Cross-ref number '{num}' appears {en_count}x in EN "
                    f"but only {tr_count}x in TR (missing {diff})"
                ),
                auto_fixable=False,
                confidence=0.90,
                fix_suggestion=(
                    f"Check translation for corrupted reference number "
                    f"'{num}' -- MT may have changed it"
                ),
                context=f"EN has {len(en_counter)} unique ref numbers, "
                        f"TR has {len(tr_counter)}",
            ))

        # Report hallucinated numbers (new numbers not in source)
        for num, tr_count in hallucinated_nums[:3]:
            # 2026-09-30 (kulvar F2): satır, atfın GEÇTİĞİ yerden (eskiden
            # numaranın alt dizge olarak geçtiği ilk satır: bir test raporunda
            # "52" için çok önceki bir satır gösteriliyordu).
            # Bölüm etiketi kuralı satır sayısını değiştirebildiği için satır,
            # atfın geçtiği satırın METNİ özgün satırlarda aranarak bulunur;
            # bulunamazsa eski yol (numaranın geçtiği ilk satır).
            line_num = 0
            if num in tr_first_pos:
                pos = tr_first_pos[num]
                start = tr_text.rfind("\n", 0, pos) + 1
                end = tr_text.find("\n", pos)
                snippet = tr_text[start:end if end != -1 else len(tr_text)]
                line_num = next((li for li, line in enumerate(tr_lines, 1)
                                 if line == snippet), 0)
            if not line_num:
                line_num = 1
                for li, line in enumerate(tr_lines, 1):
                    if num in line:
                        line_num = li
                        break

            report.issues.append(QCIssue(
                check_name="number_hallucination",
                severity="high",
                line_num=line_num,
                message=(
                    f"Cross-ref number '{num}' appears {tr_count}x in TR "
                    f"but NOT in EN source -- possible hallucination"
                ),
                auto_fixable=False,
                confidence=0.80,
                fix_suggestion=(
                    f"Number '{num}' may be hallucinated by MT model -- "
                    f"verify against source"
                ),
                context="",
            ))

    # ------------------------------------------------------------------
    # Check 23: Sentence Length Alignment
    # ------------------------------------------------------------------

    @staticmethod
    def _split_sentences(text: str) -> List[str]:
        """Split text into sentences, ignoring LaTeX commands and math."""
        # Remove math environments
        cleaned = re.sub(r'\$[^$]+\$', ' MATH ', text)
        cleaned = re.sub(r'\\begin\{equation\}.*?\\end\{equation\}',
                        ' MATH ', cleaned, flags=re.DOTALL)
        # Remove LaTeX commands
        cleaned = re.sub(r'\\[a-zA-Z]+\{[^}]*\}', ' ', cleaned)
        cleaned = re.sub(r'\\[a-zA-Z]+', ' ', cleaned)
        # Split on sentence boundaries
        sentences = re.split(r'(?<=[.!?])\s+(?=[A-ZÇĞİÖŞÜa-zçğıöşü])', cleaned)
        return [s.strip() for s in sentences if len(s.strip()) > 10]

    def _check_sentence_length_alignment(
        self,
        tr_text: str,
        en_text: str,
        tr_lines: List[str],
        en_lines: List[str],
        report: QCReport,
    ) -> None:
        """Check overall EN/TR length ratio for information loss/gain.

        This is a document-level (not paragraph-aligned) heuristic:

        * Sums the character length of all substantial (>30 char) paragraphs
          on each side and compares the totals.  TR below 50% of EN total
          flags possible information loss (high); above 200% flags possible
          duplication/hallucination (medium).
        * Separately compares the *count* of substantial paragraphs and flags
          when it differs by more than max(3, 20%).

        All issues here are report-only (``auto_fixable=False``); nothing is
        fed back to the MT model.  Turkish is normally 10-30% longer than
        English due to agglutinative morphology, so the 0.5/2.0 bounds are
        deliberately wide to only catch gross mismatches.
        """
        if not en_text:
            return

        # 2026-09-25 (dalga Q): Yol A'da çevirmen notu dipnotları kaynakta
        # karşılığı olmayan meşru ektir; uzunluk oranına sayılmaz.
        if getattr(self, "_manual_translation", False):
            tr_text = strip_translator_notes(tr_text)

        # Split into paragraphs (double newline separated)
        en_paras = [p.strip() for p in re.split(r'\n\s*\n', en_text) if p.strip()]
        tr_paras = [p.strip() for p in re.split(r'\n\s*\n', tr_text) if p.strip()]

        # Filter out very short paragraphs (LaTeX commands only)
        en_paras = [p for p in en_paras if len(p) > 30]
        tr_paras = [p for p in tr_paras if len(p) > 30]

        if not en_paras or not tr_paras:
            return

        # Overall length comparison
        en_total = sum(len(p) for p in en_paras)
        tr_total = sum(len(p) for p in tr_paras)

        if en_total == 0:
            return

        ratio = tr_total / en_total

        # Turkish text is typically 10-30% longer than English due to
        # agglutinative morphology. Ratio 0.8-1.5 is normal.
        if ratio < 0.5:
            report.issues.append(QCIssue(
                check_name="sentence_length_alignment",
                severity="high",
                line_num=1,
                message=(
                    f"TR text is {ratio:.0%} of EN length "
                    f"(EN={en_total} chars, TR={tr_total} chars) -- "
                    f"possible information loss"
                ),
                auto_fixable=False,
                confidence=0.85,
                fix_suggestion=(
                    "Check for missing paragraphs or truncated translations"
                ),
                context=f"EN paragraphs: {len(en_paras)}, "
                        f"TR paragraphs: {len(tr_paras)}",
            ))
        elif ratio > 2.0:
            report.issues.append(QCIssue(
                check_name="sentence_length_alignment",
                severity="medium",
                line_num=1,
                message=(
                    f"TR text is {ratio:.0%} of EN length "
                    f"(EN={en_total} chars, TR={tr_total} chars) -- "
                    f"possible duplication or hallucination"
                ),
                auto_fixable=False,
                confidence=0.75,
                fix_suggestion=(
                    "Check for duplicated paragraphs or MT hallucination"
                ),
                context=f"EN paragraphs: {len(en_paras)}, "
                        f"TR paragraphs: {len(tr_paras)}",
            ))

        # Paragraph count comparison
        if len(en_paras) > 0 and len(tr_paras) > 0:
            para_diff = abs(len(en_paras) - len(tr_paras))
            # Allow +-20% tolerance
            tolerance = max(3, int(len(en_paras) * 0.2))
            if para_diff > tolerance:
                report.issues.append(QCIssue(
                    check_name="sentence_length_alignment",
                    severity="medium",
                    line_num=1,
                    message=(
                        f"Paragraph count mismatch: "
                        f"EN={len(en_paras)}, TR={len(tr_paras)} "
                        f"(diff={para_diff})"
                    ),
                    auto_fixable=False,
                    confidence=0.70,
                    fix_suggestion=(
                        "Check for merged or split paragraphs in translation"
                    ),
                    context="",
                ))


    # ------------------------------------------------------------------
    # Check 24: Translated Environment Names
    # ------------------------------------------------------------------

    # Valid LaTeX environment names that must NEVER be translated.
    _VALID_ENV_NAMES = frozenset({
        # Standard document environments
        "document", "abstract", "center", "flushleft", "flushright",
        "minipage", "verbatim", "quote", "quotation", "verse",
        "description", "list",
        # Math environments
        "equation", "equation*", "align", "align*", "gather", "gather*",
        "multline", "multline*", "split", "flalign", "flalign*",
        "alignat", "alignat*", "eqnarray", "eqnarray*",
        "aligned", "gathered", "cases", "matrix", "pmatrix", "bmatrix",
        "vmatrix", "Vmatrix", "Bmatrix",
        # Floats and lists
        "figure", "figure*", "table", "table*",
        "itemize", "enumerate", "tabular", "tabularx", "longtable",
        "array",
        # Theorem-like environments
        "theorem", "proof", "lemma", "definition", "example",
        "remark", "corollary", "proposition", "property",
        # TikZ / code / algorithm
        "tikzpicture", "tikzcd", "lstlisting", "algorithm", "algorithmic",
        # Page structure
        "titlepage", "thebibliography", "appendix",
        # AMS
        "subequations",
    })

    # Known Turkish (or other language) translations of env names.
    # Maps wrong -> correct.
    _TRANSLATED_ENV_MAP = {
        # Turkish translations
        "sekil": "figure", "şekil": "figure", "resim": "figure",
        "tablo": "table", "cizim": "figure",
        "denklem": "equation", "esitlik": "equation",
        "oge": "itemize", "öge": "itemize", "öğe": "itemize",
        "madde": "enumerate",
        "kanit": "proof", "kanıt": "proof", "ispat": "proof",
        "teorem": "theorem",
        "tanim": "definition", "tanım": "definition",
        "ornek": "example", "örnek": "example",
        "aciklama": "remark", "açıklama": "remark",
        "onerme": "proposition", "önerme": "proposition",
        "sonuc": "corollary", "sonuç": "corollary",
        "ozellik": "property", "özellik": "property",
        "ozet": "abstract", "özet": "abstract",
        "merkez": "center",
        "alinti": "quote", "alıntı": "quote",
        "matris": "matrix",
        "hizalama": "align",
        "dizi": "array",
        # German/French/Spanish (for foreign_language_leakage overlap)
        "gleichung": "equation",  # German
        "abbildung": "figure",    # German
        "ecuacion": "equation",   # Spanish
        "figura": "figure",       # Spanish/Italian
    }

    def _check_translated_env_name(
        self,
        tr_text: str,
        tr_lines: List[str],
        report: QCReport,
    ) -> None:
        r"""Detect LaTeX environment names that were translated.

        Scans for \begin{...} and \end{...} where the environment name
        is NOT in the known valid set.  If the name matches a known
        translated form, it is flagged as CRITICAL (will break XeLaTeX).
        Unknown names that are not in the valid set are flagged as HIGH
        (may be custom environments or errors).
        """
        env_re = re.compile(r"\\(?:begin|end)\{([^}]+)\}")

        # F2 (2026-07-28, A-9 eşdeğeri): kullanıcının \newenvironment/
        # \newtheorem ile tanımladığı adlar issue DEĞİLDİR — sanitizer
        # guard'ı vardı ama QC/autofix yolu tanımlı adı critical sayıp
        # yeniden adlandırıyor, derlemeyi kırıyordu.
        defined_envs = (
            collect_defined_env_names(tr_text) | self._user_defined_envs
        )

        for line_idx, line in enumerate(tr_lines):
            # Skip lines inside math environments (they may use custom envs)
            stripped = line.strip()
            if not stripped:
                continue

            for m in env_re.finditer(line):
                env_name = m.group(1).strip()
                # Remove star suffix for matching (e.g. "sekil*" -> "sekil")
                env_base = env_name.rstrip("*")

                # Valid environment -- OK
                if env_name in self._VALID_ENV_NAMES:
                    continue

                # Kullanıcı tanımlı ortam -- OK (F2)
                if env_base in defined_envs or env_name in defined_envs:
                    continue

                # Check if it is a known translated name
                correct_name = self._TRANSLATED_ENV_MAP.get(env_base.lower())
                if correct_name:
                    report.issues.append(QCIssue(
                        check_name="translated_env_name",
                        severity="critical",
                        line_num=line_idx + 1,
                        message=(
                            f"Environment name translated: "
                            f"\\begin{{{env_name}}} should be "
                            f"\\begin{{{correct_name}}}"
                        ),
                        auto_fixable=True,
                        confidence=0.95,
                        fix_suggestion=f"{env_name} -> {correct_name}",
                        context=stripped[:120],
                    ))

    # ------------------------------------------------------------------
    # Check 25: Broken Command Braces
    # ------------------------------------------------------------------

    # LaTeX commands that MUST be followed by {.
    # Listed from longest to shortest to avoid prefix matching issues
    # (e.g. \textbf must not match as \text + bf).
    _BRACE_REQUIRED_CMDS = (
        r"\\subsubsection\*?",
        r"\\subsection\*?",
        r"\\underline",
        r"\\footnote",
        r"\\section\*?",
        r"\\chapter\*?",
        r"\\caption",
        r"\\textbf",
        r"\\textit",
        r"\\emph",
        r"\\mbox",
        r"\\fbox",
    )

    # Build combined regex: command NOT followed by { or [.
    # F1 (2026-07-28) + R2 (2026-07-29): harf devamı toptan dışlanmaz —
    # eşleşme sonrası KNOWN_LONGER_COMMANDS kontrolü yapılır. Böylece
    # \footnotesize/\captionsetup meşru kalır (F1), \textbfdeger gibi
    # tamamı-küçük yapışık kırıklar da yakalanır (R2; F1'in toptan a-z
    # dışlaması bunları kör etmişti).
    # Eski not: F1, lookahead'e a-z eklemişti — LaTeX komut
    # adları yalnız ASCII harften oluşur, küçük harfle devam eden eşleşme
    # daha uzun MEŞRU bir komuttur (\footnotesize, \captionsetup,
    # \captionof); eskiden bunlar "eksik brace" sayılıp autofix'te
    # \footnote{size} gibi sahte komutlara dönüşüyordu.  Kırık vaka
    # (\textbfSpektrum: kopan brace içeriği büyük harfle başlar) yakalanmaya
    # devam eder.
    _BROKEN_CMD_RE = re.compile(
        r"("
        + r"|".join(_BRACE_REQUIRED_CMDS)
        + r")"
        r"(?![{*\[\\])"  # not followed by { * [ or \
        r"([A-Za-z\u00C0-\u024F])"  # followed by a letter (Latin+extended)
    )

    def _check_broken_command_braces(
        self,
        tr_text: str,
        tr_lines: List[str],
        report: QCReport,
    ) -> None:
        r"""Detect LaTeX commands missing required braces.

        For commands like \textbf, \textit, \emph, etc. that MUST be
        followed by {content}, detects cases where the brace is missing
        (e.g. \textbfSpektrum instead of \textbf{Spektrum}).
        """
        for line_idx, line in enumerate(tr_lines):
            stripped = line.strip()
            if not stripped:
                continue

            # Skip lines that are purely environment markers
            if stripped.startswith("\\begin") or stripped.startswith("\\end"):
                continue

            for m in self._BROKEN_CMD_RE.finditer(line):
                full_match = m.group(0)
                cmd_name = m.group(1)       # e.g. \textbf
                first_char = m.group(2)     # e.g. S (from Spektrum)

                # R2: devam eden ASCII harflerle oluşan tam ad bilinen bir
                # komutsa (\footnotesize, \captionsetup) bu kırık değildir.
                tail_m = _CMD_TAIL_RE.match(line, m.start(2))
                if tail_m and (cmd_name.lstrip("\\").rstrip("*")
                               + tail_m.group(0)) in KNOWN_LONGER_COMMANDS:
                    continue

                report.issues.append(QCIssue(
                    check_name="broken_command_braces",
                    severity="high",
                    line_num=line_idx + 1,
                    message=(
                        f"Command {cmd_name} missing braces: "
                        f"'{full_match}' should be "
                        f"'{cmd_name}{{{first_char}...}}'..."
                    ),
                    auto_fixable=True,
                    confidence=0.90,
                    fix_suggestion=(
                        f"Add braces: {cmd_name}{{...}}"
                    ),
                    context=stripped[:120],
                ))

    # ------------------------------------------------------------------
    # Check 26: Stash Placeholder Orphan
    # ------------------------------------------------------------------

    # Generic stash placeholder pattern: <<ANYNAME_digits>>
    # Excludes <<LATEX_N>> which is already caught by check 6.
    _STASH_PLACEHOLDER_RE = re.compile(r"<<([A-Z]+)_(\d+)>>")

    # NER placeholder KALINTISI: MT, @@UNKNOWN0000@@ gibi placeholder'ların
    # sözcük kısmını çevirebiliyor (UNKNOWN → BİLİNMİYOR) veya @@ işaretlerini
    # kısmen düşürebiliyor. Kanıt (sutton smoke, 2026-07-06):
    #   \subsubsection{MİYOR0000@@}  ve  "Yerçekimi-MİYOR0000-MİYOR0001".
    # Kanonik desenler ([A-Z_]+) Türkçe 'İ' içeren bozuk varyantı kaçırıyordu.
    _NER_RESIDUE_RE = re.compile(
        r"@@[^\s@]{0,24}?\d{1,6}@@"          # @@<bozuk-sözcük>0001@@
        r"|[^\W\d_]{2,24}\d{4}@@"            # MİYOR0001@@ (baş @@ düşmüş)
        r"|@@[^\W\d_]{2,24}\d{4}(?!\d)"      # @@MİYOR0001 (son @@ düşmüş)
        r"|\b(?:UNKNOWN|B[İI]L[İI]NMEYEN|B[İI]L[İI]NM[İI]YOR|M[İI]YOR|"
        r"PERSON|K[İI][ŞS][İI]|ORG|KURUM|WORK|ESER|TERM|TER[İI]M)"
        r"[-_]?\d{2,4}(?!\d)\b"              # işaretsiz kalıntı (MİYOR0000)
    )

    def _check_stash_placeholder_orphan(
        self,
        tr_text: str,
        tr_lines: List[str],
        report: QCReport,
    ) -> None:
        """Detect unrestored stash placeholders in translated output.

        The translator pipeline uses placeholders like <<REF_N>>,
        <<TERM_N>>, <<AUTHOR_N>>, etc. to protect content during
        translation.  If _restore_stash() fails or is buggy, these
        remain in the final output.  This check catches ALL such
        patterns that check 6 (placeholder_orphans) does not cover.

        <<LATEX_N>> is handled by check 6 -- we skip it here to
        avoid duplicate issues.
        """
        # Types already handled by check 6
        _SKIP_TYPES = {"LATEX"}

        for line_idx, line in enumerate(tr_lines):
            stripped = line.strip()
            if not stripped:
                continue
            # Skip LaTeX comments
            if stripped.startswith("%"):
                continue

            for m in self._STASH_PLACEHOLDER_RE.finditer(line):
                placeholder_type = m.group(1)  # e.g. "REF", "TERM"
                placeholder_num = m.group(2)    # e.g. "42"
                full_match = m.group(0)         # e.g. "<<REF_42>>"

                # Skip types already caught by check 6
                if placeholder_type in _SKIP_TYPES:
                    continue

                report.issues.append(QCIssue(
                    check_name="stash_placeholder_orphan",
                    severity="critical",
                    line_num=line_idx + 1,
                    message=(
                        f"Unrestored stash placeholder: {full_match} "
                        f"(type={placeholder_type}, id={placeholder_num})"
                    ),
                    auto_fixable=True,
                    confidence=0.95,
                    fix_suggestion=f"Remove orphan placeholder: {full_match}",
                    context=stripped[:120],
                ))

            # Bozulmuş NER placeholder kalıntıları (MİYOR0000@@ vb.).
            # auto_fixable=False: silmek içeriği (örn. yazar adını) yok eder;
            # doğru aksiyon kaynaktan geri yükleme/yeniden çeviridir.
            for m in self._NER_RESIDUE_RE.finditer(line):
                report.issues.append(QCIssue(
                    check_name="ner_placeholder_residue",
                    severity="critical",
                    line_num=line_idx + 1,
                    message=(
                        f"Mangled NER placeholder residue: {m.group(0)!r} "
                        f"(MT placeholder'ı bozmuş, restore başarısız)"
                    ),
                    auto_fixable=False,
                    confidence=0.9,
                    fix_suggestion=(
                        "Kaynak metinden orijinal içeriği geri yükle veya "
                        "segmenti yeniden çevir — kalıntıyı SİLME"
                    ),
                    context=stripped[:120],
                ))

    # ------------------------------------------------------------------
    # Check 28: HTML Artifact Detection
    # ------------------------------------------------------------------

    # Common HTML tags that indicate OCR/conversion artifacts in LaTeX
    _HTML_ARTIFACT_RE = re.compile(
        r"</?(?:colgroup|col|tbody|thead|tr|td|th|table|div|span|"
        r"sup|sub|br|p|em|strong|a|ul|ol|li|img|hr|section|article|"
        r"header|footer|nav|main|aside|figure|figcaption|blockquote"
        r")\b[^>]*>"
    )

    # Verbatim-benzeri ortamlar (içlerinde HTML meşrudur). F42 (2026-07-29):
    # ortam adı yakalanır ki begin ile aynı satırdaki end eşleştirilebilsin.
    _VERBATIM_BEGIN_RE = re.compile(r"\\begin\{(verbatim|lstlisting|minted)\}")
    _VERBATIM_END_RE = re.compile(r"\\end\{(verbatim|lstlisting|minted)\}")

    def _check_html_artifacts(
        self,
        tr_text: str,
        tr_lines: List[str],
        report: QCReport,
    ) -> None:
        """Detect HTML tag artifacts in translated LaTeX output.

        Marker OCR or MT models sometimes produce HTML tags (colgroup,
        tbody, div, span, etc.) that have no place in LaTeX output.
        A single stray tag might be noise, but 3+ tags on a single
        line is a definitive artifact.

        One test book had 1400+ colgroup tags that went undetected
        because this check did not exist.
        """
        total_html_tags = 0

        for line_idx, line in enumerate(tr_lines):
            stripped = line.strip()
            if not stripped:
                continue
            # Skip LaTeX comments
            if stripped.startswith("%"):
                continue
            # Track verbatim / lstlisting / minted environments
            # (HTML is legal inside these)
            if not hasattr(self, '_in_verbatim'):
                self._in_verbatim = False
            if self._in_verbatim:
                # F42: kapanış satırın BAŞINDA olmak zorunda değil —
                # "kod \end{verbatim}" da ortamı kapatır. Eskiden bayrak
                # açık kalıp belgenin kalanını taramadan muaf tutuyordu.
                if self._VERBATIM_END_RE.search(stripped):
                    self._in_verbatim = False
                continue
            m_begin = self._VERBATIM_BEGIN_RE.match(stripped)
            if m_begin:
                # F42: tek satırlık ortam (\begin{verbatim}x\end{verbatim})
                # bayrağı AÇIK BIRAKMAMALI; eskiden belgenin geri kalanı
                # html_artifact taramasından tamamen düşüyordu.
                m_end = self._VERBATIM_END_RE.search(stripped)
                if not (m_end and m_end.group(1) == m_begin.group(1)):
                    self._in_verbatim = True
                continue
            if self._VERBATIM_END_RE.match(stripped):
                self._in_verbatim = False
                continue

            matches = self._HTML_ARTIFACT_RE.findall(line)
            if len(matches) >= 3:
                total_html_tags += len(matches)
                report.issues.append(QCIssue(
                    check_name="html_artifact",
                    severity="critical",
                    line_num=line_idx + 1,
                    message=(
                        f"HTML artifacts detected: {len(matches)} tags "
                        f"on this line ({matches[0]}, ...)"
                    ),
                    # Report-only: no AutoFixer handler for 'html_artifact'
                    # (QC-N7).  HTML stripping belongs to the sanitizer stage.
                    auto_fixable=False,
                    confidence=0.95,
                    fix_suggestion=(
                        "Sanitizer should strip HTML tags from this line"
                    ),
                    context=stripped[:120],
                ))
            elif len(matches) == 1 or len(matches) == 2:
                # Low-count HTML tags: still suspicious but lower severity
                total_html_tags += len(matches)

        # If the document has many scattered HTML tags (even 1-2 per line),
        # that's also an artifact problem. Kapi yalniz KENDI check'inin
        # bulgularina bakar — eski 'not report.issues' kosulu, onceki 27
        # check'ten HERHANGI bir bulgu varken bu dali olu koda ceviriyordu
        # (audit 2026-07-16 #38).
        if total_html_tags >= 10 and not any(
            i.check_name == "html_artifact" for i in report.issues
        ):
            report.issues.append(QCIssue(
                check_name="html_artifact",
                severity="high",
                line_num=1,
                message=(
                    f"Document contains {total_html_tags} HTML tags "
                    f"scattered across lines -- likely conversion artifact"
                ),
                # Report-only: no AutoFixer handler for 'html_artifact' (QC-N7).
                auto_fixable=False,
                confidence=0.85,
                fix_suggestion=(
                    "Sanitizer should clean all HTML tags from the document"
                ),
                context="",
            ))

    # ------------------------------------------------------------------
    # Check 27: Word Concatenation (lost space detection)
    # ------------------------------------------------------------------

    # Pattern: Turkish lowercase letter followed by uppercase letter
    # followed by lowercase -- indicates lost space between words.
    # e.g. "fonksiyonlarıHer" -> "fonksiyonları Her"
    _WORD_CONCAT_RE = re.compile(
        r"[a-z\u00e7\u011f\u0131\u00f6\u015f\u00fc]"  # çğıöşü
        r"[A-Z\u00c7\u011e\u0130\u00d6\u015e\u00dc]"  # ÇĞIÖŞÜ
        r"[a-z\u00e7\u011f\u0131\u00f6\u015f\u00fc]"  # çğıöşü
    )

    # Common physics/engineering unit abbreviations that use mixed case
    # but are NOT concatenation errors (e.g. "kPa", "MHz", "mW").
    # Checked against the full word (lowercased) surrounding the match.
    _UNIT_EXCEPTIONS = {
        "kpa", "mpa", "gpa", "hpa", "tpa",         # pascal
        "kv", "mv",                                  # volt
        "ka", "ma",                                  # ampere
        "kw", "mw", "gw", "tw",                     # watt
        "km", "cm", "mm", "nm", "pm", "fm",         # metre
        "kg", "mg", "ng", "pg",                      # gram
        "khz", "mhz", "ghz", "thz",                 # hertz
        "kb", "mb", "gb", "tb", "pb",               # byte
        "ms", "ns", "ps", "fs",                      # second
        "kj", "mj", "gj",                            # joule
        "kn",                                         # knot / kilonewton
        "dl", "ml", "cl",                             # litre
        "kwh", "mwh", "gwh",                          # watt-hour
        "rpm",                                         # revolutions per minute
        "db", "dba",                                   # decibel
    }

    # Known legitimate camelCase / mixed-case words that should NOT
    # be flagged.  All comparisons are done on the full word containing
    # the match.
    _CAMELCASE_WHITELIST = {
        "LaTeX", "BibTeX", "XeLaTeX", "LuaTeX", "pdfTeX", "ConTeXt",
        "XeTeX", "BibLaTeX",
        "iPhone", "iPad", "iPod", "iTunes", "iMac", "macOS", "iOS",
        "GitHub", "JavaScript", "TypeScript", "CamelCase", "GitHub",
        "YouTube", "LinkedIn", "TensorFlow", "PyTorch", "NumPy",
        "SciPy", "DataFrame", "PowerPoint", "WordPress", "PostgreSQL",
        "MongoDB", "GraphQL", "DevOps", "IntelliJ", "PhpStorm",
        "WebSocket", "OpenAI", "ChatGPT", "AutoML", "ResNet", "VGGNet",
        "ImageNet", "arXiv", "ResNet", "DenseNet", "EfficientNet",
        "MobileNet",
    }

    # LaTeX command pattern -- used to skip matches inside commands
    _LATEX_CMD_SKIP_RE = re.compile(
        r"\\[a-zA-Z]+\s*(?:\*?\{[^}]*\}|\*?\[[^\]]*\])*"
    )

    def _check_word_concatenation(
        self,
        tr_text: str,
        tr_lines: List[str],
        report: QCReport,
    ) -> None:
        """Detect lost spaces between Turkish words.

        When _restore_stash() or MT output drops a space, adjacent
        words get concatenated.  This shows up as a lowercase Turkish
        letter immediately followed by an uppercase letter mid-word,
        e.g. "fonksiyonlarıHer", "yöntemBu".

        Excludes:
        - Content inside LaTeX commands (\\textbf{}, \\section{}, etc.)
        - Known camelCase words (LaTeX, iPhone, etc.)
        - Content inside math environments
        """
        for line_idx, line in enumerate(tr_lines):
            stripped = line.strip()
            if not stripped:
                continue
            # Skip LaTeX comments
            if stripped.startswith("%"):
                continue
            # Skip lines that are purely LaTeX commands
            if stripped.startswith("\\begin") or stripped.startswith("\\end"):
                continue

            # Build a set of character positions that are inside LaTeX
            # commands, so we can skip matches there.
            cmd_positions: Set[int] = set()
            for m in self._LATEX_CMD_SKIP_RE.finditer(line):
                for pos in range(m.start(), m.end()):
                    cmd_positions.add(pos)

            # Also skip positions inside inline math $...$
            for m in _INLINE_BLOCK_RE.finditer(line):
                for pos in range(m.start(), m.end()):
                    cmd_positions.add(pos)

            for m in self._WORD_CONCAT_RE.finditer(line):
                match_start = m.start()

                # Skip if inside a LaTeX command span
                if match_start in cmd_positions or (match_start + 1) in cmd_positions:
                    continue

                # Extract the full "word" surrounding the match to check
                # against the whitelist.
                # Walk left and right to find word boundaries.
                left = match_start
                while left > 0 and (line[left - 1].isalnum() or line[left - 1] in "_."):
                    left -= 1
                right = m.end()
                while right < len(line) and (line[right].isalnum() or line[right] in "_."):
                    right += 1
                full_word = line[left:right]

                # Skip physics/engineering unit abbreviations (e.g. kPa, MHz)
                if full_word.lower() in self._UNIT_EXCEPTIONS:
                    continue

                # Check whitelist
                if full_word in self._CAMELCASE_WHITELIST:
                    continue

                # Also skip if any whitelist entry is a substring
                # (handles e.g. "kullanılanLaTeX" -> "LaTeX" is in whitelist)
                skip = False
                for wl_word in self._CAMELCASE_WHITELIST:
                    if wl_word in full_word:
                        skip = True
                        break
                if skip:
                    continue

                # 2026-09-25 (kulvar Q): kaynakta AYNEN geçen harf dizisi
                # kayıp boşluk olamaz — özel ad ("MacClellan", "McCulloch")
                # ya da kod adı ("DataLoader"); çeviri onu kaynaktan taşıdı.
                l_start, l_end = match_start, m.end()
                while l_start > 0 and line[l_start - 1].isalpha():
                    l_start -= 1
                while l_end < len(line) and line[l_end].isalpha():
                    l_end += 1
                if self._in_source_verbatim(line[l_start:l_end]):
                    continue

                matched_text = m.group(0)
                # Suggest inserting a space between the lowercase and
                # uppercase character
                fix_text = matched_text[0] + " " + matched_text[1:]

                report.issues.append(QCIssue(
                    check_name="word_concatenation",
                    severity="high",
                    line_num=line_idx + 1,
                    message=(
                        f"Lost space detected: '{matched_text}' in "
                        f"'{full_word}' -- likely "
                        f"'{fix_text}'"
                    ),
                    auto_fixable=True,
                    confidence=0.85,
                    fix_suggestion=f"Insert space: '{matched_text}' -> '{fix_text}'",
                    context=stripped[:120],
                ))

    # ------------------------------------------------------------------
    # Check 29: Hallucination and Information Loss Detection
    # ------------------------------------------------------------------

    # Regex for finding dotted numbers in body text (e.g. 3.14, 7.4, 2.6)
    # Excludes numbers inside LaTeX commands and math environments.
    _DOTTED_NUMBER_RE = re.compile(r"(?<!\w)\d+\.\d+(?:\.\d+)*(?!\w)")
    # 2026-09-30 (kulvar F2): Türkçede ondalık ayırıcı VİRGÜLDÜR: kaynaktaki
    # "1.09", "18.8", "7.5%" çeviride "1,09", "18,8", "%7,5" yazılır (bir test
    # raporunda 7 HIGH "Orphan dotted numbers" + 2 LOW, hepsi yanlış alarm).
    # Çeviri tarafında virgüllü ondalık da noktalı karşılığıyla sayılır.
    # Binlik ayraçlı ("1.234,5") ve virgülle sıralanmış ("1,2,3") sayılar
    # dahil edilmez: tek virgüllü, iki yanı rakam, öncesinde/ardında nokta,
    # virgül ya da harf olmayan dizi.
    _TR_DECIMAL_COMMA_RE = re.compile(
        r"(?<![\w.,])\d+,\d+(?![\w]|[.,]\d)")

    # Common decimal numbers that often appear as contextual values rather
    # than cross-references. When these are "orphaned" (EN has them, TR
    # doesn't), it's usually a paraphrase difference, not a real
    # hallucination. We downgrade their severity from "high" to "low".
    _COMMON_DECIMAL_NUMBERS = {
        "0.5", "1.0", "1.5", "2.0", "2.5", "3.0", "3.5", "4.0", "4.5",
        "5.0", "5.5", "6.0", "6.5", "7.0", "7.5", "8.0", "8.5", "9.0",
        "9.5", "10.0", "0.1", "0.2", "0.25", "0.3", "0.4", "0.6", "0.7",
        "0.75", "0.8", "0.9", "1.25", "1.75", "2.25", "2.75", "3.14",
        "5.75", "12.0", "15.0", "20.0", "25.0", "50.0", "100.0",
    }

    # Regex for detecting MT repetition loops: a phrase of 5+ words
    # repeated 3+ times consecutively.
    _REPETITION_LOOP_RE = re.compile(
        r"(\b\w+(?:\s+\w+){4,}?)(?:\s+\1){2,}",
        re.IGNORECASE,
    )

    # F8 (2026-07-28): 29a senkron geri kurma penceresi. Birleşmiş
    # paragraf hipotezi ancak birleşik oran bu DAR penceredeyse kabul
    # edilir (normal çeviri oranı ~1.0); 0.3-3.0 ana penceresi kullanılsa
    # gerçek halüsinasyon da "birleşme" sanılıp susturulurdu.
    _PARA_RESYNC_RATIO_LOW = 0.5
    _PARA_RESYNC_RATIO_HIGH = 2.0
    _PARA_RESYNC_MAX_MERGE = 3
    # 29a bu boydan kısa EN paragrafını yargılamaz (başlık, etiket, kısa
    # bağlaç parçası); FX3 çapa hipotezi de yalnız bu boydan kısa paragrafı
    # atlar — 29a'nın zaten görmediği boy, tespit gücü azalmaz.
    _PARA_SHORT_CHARS = 50
    # FX3 (2026-10-05): çapaya ulaşmak için atlanabilecek en çok kısa
    # paragraf (bağlam grubunda display başına en çok bir parça kayar).
    _PARA_ANCHOR_MAX_SKIP = 2
    _INCLUDEGRAPHICS_RE = re.compile(
        r"\\includegraphics\s*(?:\[[^\]]*\])?\s*\{[^}]*\}")

    @staticmethod
    def _para_anchor_key(para: str) -> Optional[str]:
        """FX3 (2026-10-05): paragraf bir ÇAPA mı (iki dilde birebir duran
        içerik)? Display matematik (\\[…\\] / $$…$$; çeviride yer tutucudan
        AYNEN geri yazılır) ya da \\includegraphics içeren şekil bloğu
        (altyazı çevrilir, görsel yolu değişmez). Anahtar: display bloğunun
        kendisi (ardından gelen Türkçe devam satırları anahtara girmez) ya da
        görsel komutları; çapa değilse None."""
        s = para.strip()
        if s.startswith(("\\[", "$$")):
            kept: List[str] = []
            in_display = False
            for line in s.split("\n"):
                if not in_display:
                    if not line.startswith(("\\[", "$$")):
                        break
                    in_display = True
                kept.append(line)
                if line.endswith("\\]") or (
                        line.endswith("$$")
                        and (len(kept) > 1 or line.count("$$") >= 2)):
                    in_display = False
            key = " ".join("".join(kept).split())
            return key or None
        if "\\includegraphics" in s:
            found = QCEngine._INCLUDEGRAPHICS_RE.findall(s)
            return " ".join(" ".join(f.split()) for f in found) or None
        return None

    @classmethod
    def _para_anchor_resync(cls, en_paras: List[str], tr_paras: List[str],
                            en_keys: List[Optional[str]],
                            tr_keys: List[Optional[str]],
                            ei: int, tj: int) -> Optional[Tuple[int, int]]:
        """FX3 (2026-10-05): çapa yeniden hizalaması. Bir yanda çapa varken
        öbür yandaki paragraf aynı çapa değilse, öbür yanda yalnız KISA ve
        çapa olmayan paragraflar (en çok ``_PARA_ANCHOR_MAX_SKIP``)
        atlanınca aynı çapaya ulaşılıyorsa yeni (ei, tj) döner; yoksa None.

        Olgu (bir model denemesi, 5 Eki): bağlam grubunda Türkçe söz dizimi
        metni display'in öbür yanına taşıyor — kaynakta karşılığı olmayan
        kısa devam paragrafı ("… elde edilir.") ya da eriyen kısa parça
        paragrafı hizayı yerel olarak ±1 kaydırıyor, paragraf sayısı eşit
        kaldığı için birleşme/bölünme hipotezleri denenmiyordu → 30 sahte
        CRITICAL, puan 0. Kanıt: iki yanda BİREBİR aynı çapa; atlanan
        paragraf 29a'nın zaten yargılamadığı boyda (``_PARA_SHORT_CHARS``)."""
        ka, kb = en_keys[ei], tr_keys[tj]
        if ka is not None and ka == kb:
            return None

        def skippable(paras: List[str], keys: List[Optional[str]],
                      k: int) -> bool:
            return keys[k] is None and len(paras[k]) < cls._PARA_SHORT_CHARS

        if ka is not None:      # TR'de fazladan kısa paragraf(lar)
            for d in range(1, cls._PARA_ANCHOR_MAX_SKIP + 1):
                j = tj + d
                if j >= len(tr_paras) or not skippable(tr_paras, tr_keys, j - 1):
                    break
                if tr_keys[j] == ka:
                    return ei, j
        if kb is not None:      # TR'de eriyen kısa paragraf(lar)
            for d in range(1, cls._PARA_ANCHOR_MAX_SKIP + 1):
                i = ei + d
                if i >= len(en_paras) or not skippable(en_paras, en_keys, i - 1):
                    break
                if en_keys[i] == kb:
                    return i, tj
        return None

    @classmethod
    def _anchor_boundary_shift(cls, en_paras: List[str], tr_paras: List[str],
                               en_keys: List[Optional[str]],
                               tr_keys: List[Optional[str]],
                               ei: int, tj: int, doc_ratio: float) -> bool:
        """FX3 (2026-10-05): çift (ei, tj) ile AYNI ÇAPANIN öbür yanındaki
        düz metin çifti arasında yalnız metin yer değiştirmiş mi? Türkçe söz
        dizimi kaynakta display'den sonra gelen tanımı display'den önceki
        paragrafa taşıyabilir (sentetik kalıp: "… açı şöyle tanımlanınca
        <display> şeklindedir."). Hepsi gerekir: araya düşen paragraf iki
        yanda birebir aynı çapa; iki çiftin birleşik oranı belge oranının
        ``_PARA_SHIFT_TOLERANCE`` katı içinde; komşu çift TEK BAŞINA o bandın
        DIŞINDA ve ters yönde (eksiği/fazlayı gerçekten o taşıyor — yapısal
        devam işareti olmadığı için ``_paragraph_boundary_shift``'ten sıkı:
        olağan orandaki komşu "telafi" sayılmaz, gerçek kayıp görünür kalır)."""
        if doc_ratio <= 0:
            return False
        tol = cls._PARA_SHIFT_TOLERANCE
        cur = len(tr_paras[tj]) / max(len(en_paras[ei]), 1)
        for step in (-1, 1):
            a_e, a_t = ei + step, tj + step
            n_e, n_t = ei + 2 * step, tj + 2 * step
            if not (0 <= n_e < len(en_paras) and 0 <= n_t < len(tr_paras)):
                continue
            if en_keys[a_e] is None or en_keys[a_e] != tr_keys[a_t]:
                continue
            if en_keys[n_e] is not None or tr_keys[n_t] is not None:
                continue
            en_sum = len(en_paras[ei]) + len(en_paras[n_e])
            tr_sum = len(tr_paras[tj]) + len(tr_paras[n_t])
            rel = (tr_sum / max(en_sum, 1)) / doc_ratio
            if not (1 / tol <= rel <= tol):
                continue
            nb = len(tr_paras[n_t]) / max(len(en_paras[n_e]), 1)
            if ((cur < doc_ratio and nb > doc_ratio * tol)
                    or (cur > doc_ratio and nb < doc_ratio / tol)):
                return True
        return False

    @staticmethod
    def _is_split_continuation(prev: str, para: str) -> bool:
        """*para*, *prev*'de yarıda kalan cümlenin devamı mı (sayfa sonu
        bölünmesi)? *prev* cümle ortasında biter (son karakter harf, tire
        ya da virgül; kapanış noktalaması/ayraç değil) VE *para* doğrudan
        küçük harfle başlar (komut, matematik, ayraç değil)."""
        head = para.lstrip()
        tail = prev.rstrip()
        if not head or not tail:
            return False
        return (head[0].isalpha() and head[0].islower()
                and (tail[-1].isalpha() or tail[-1] in "-,‐"))

    # Paragraf sınırı kayması (kulvar Q): iki çiftin birleşik TR/EN oranı
    # belgenin genel oranının bu çarpanı içinde kalmalı.
    _PARA_SHIFT_TOLERANCE = 1.33

    @classmethod
    def _paragraph_boundary_shift(cls, en_paras: List[str],
                                  tr_paras: List[str], ei: int, tj: int,
                                  doc_ratio: float) -> bool:
        """Çift (ei, tj) ile bir komşu çift arasında yalnız paragraf sınırı
        mı kaymış? Hepsi gerekir:
          * HEM EN HEM TR tarafında iki çiftten SONRAKİNİN paragrafı,
            öncekinde yarıda kalan cümlenin devamıdır
            (``_is_split_continuation``: sayfa sonu bölünmesi; iki tarafta
            birden istenir ki kaymış/hizasız çiftler rastlantıyla geçmesin);
          * iki çiftin birleşik TR/EN oranı belge oranının
            ``_PARA_SHIFT_TOLERANCE`` katı içindedir (içerik korunmuş);
          * komşu çift eksiği/fazlayı telafi eder: bu çift kısaysa komşu
            TR paragrafı belge oranından UZUN, bu çift uzunsa KISA.
        Komşu kendi başına normal orandaysa (gerçek kayıp) kabul edilmez.
        """
        if doc_ratio <= 0:
            return False
        cur = len(tr_paras[tj]) / max(len(en_paras[ei]), 1)
        for (a_e, a_t, b_e, b_t), (n_e, n_t) in (
                ((ei - 1, tj - 1, ei, tj), (ei - 1, tj - 1)),
                ((ei, tj, ei + 1, tj + 1), (ei + 1, tj + 1))):
            if a_e < 0 or a_t < 0 or b_e >= len(en_paras) \
                    or b_t >= len(tr_paras):
                continue
            if not (cls._is_split_continuation(en_paras[a_e], en_paras[b_e])
                    and cls._is_split_continuation(tr_paras[a_t],
                                                   tr_paras[b_t])):
                continue
            en_sum = len(en_paras[a_e]) + len(en_paras[b_e])
            tr_sum = len(tr_paras[a_t]) + len(tr_paras[b_t])
            rel = (tr_sum / max(en_sum, 1)) / doc_ratio
            if not (1 / cls._PARA_SHIFT_TOLERANCE <= rel
                    <= cls._PARA_SHIFT_TOLERANCE):
                continue
            nb = len(tr_paras[n_t]) / max(len(en_paras[n_e]), 1)
            if (cur < doc_ratio < nb) or (nb < doc_ratio < cur):
                return True
        return False

    def _check_hallucination(
        self,
        tr_text: str,
        en_text: str,
        tr_lines: List[str],
        en_lines: List[str],
        report: QCReport,
    ) -> None:
        """Check 29: Hallucination and information loss detection.

        Sub-checks:
            29a. Paragraph length ratio -- flags extreme shrinkage (< 0.3)
                 or bloat (> 3.0) between EN and TR paragraphs.
            29b. MT repetition loop -- detects 5+ word phrases repeated
                 3+ times consecutively (a common MT failure mode).
            29c. Orphan dotted number -- EN has dotted numbers (3.14, 7.4)
                 that are missing in TR, suggesting number loss.
        """
        # 29a. Paragraph length ratio
        # 2026-09-25 (dalga Q): Yol A'da çevirmen notu dipnotları
        # (\footnote{Çevirmen notu: ...}) paragraf uzunluğuna sayılmaz —
        # kaynakta karşılığı olmayan meşru ektir; "halüsinasyon" / "bilgi
        # kaybı" oranını bozuyordu. Yol B'de (MT) not zaten sızıntıdır.
        # (Yalnız 29a; 29b/29c özgün satırlarla çalışır.)
        len_tr_lines = tr_lines
        if getattr(self, "_manual_translation", False):
            _notes_free = strip_translator_notes(tr_text)
            if _notes_free is not tr_text:
                len_tr_lines = _notes_free.split("\n")
        if en_lines and len_tr_lines:
            # Split into paragraphs (groups of non-empty lines)
            def _get_paragraphs(lines: List[str]) -> List[str]:
                paragraphs: List[str] = []
                current: List[str] = []
                for line in lines:
                    stripped = line.strip()
                    if not stripped:
                        if current:
                            paragraphs.append("\n".join(current))
                            current = []
                    else:
                        # Skip pure LaTeX structural lines for comparison
                        if stripped.startswith("\\begin") or stripped.startswith("\\end"):
                            continue
                        if stripped.startswith("%"):
                            continue
                        current.append(stripped)
                if current:
                    paragraphs.append("\n".join(current))
                return paragraphs

            en_paras = _get_paragraphs(en_lines)
            tr_paras = _get_paragraphs(len_tr_lines)
            # Belge genel TR/EN uzunluk oranı (sınır kayması testi, kulvar Q).
            _doc_ratio = (sum(len(p) for p in tr_paras)
                          / max(sum(len(p) for p in en_paras), 1))

            # F8 (2026-07-28): salt indeks hizalaması, MT'nin iki paragrafı
            # birleştirmesi (çok yaygın) sonraki TÜM çiftleri kaydırıp
            # zincirleme sahte critical üretiyordu (her biri -15, skor 0'a
            # çakılıyor). Oran pencere dışına çıkınca önce "birleşme"
            # hipotezi denenir: karşı taraftan 2-3 paragraf birleştirilince
            # oran dar pencereye dönüyor VE ardından gelen çift de uyumluysa
            # senkron geri kurulur, issue üretilmez.
            def _next_pair_ok(ei_n: int, tj_n: int) -> bool:
                if ei_n >= len(en_paras) or tj_n >= len(tr_paras):
                    return True  # liste bitti, çelişen kanıt yok
                nxt_en = len(en_paras[ei_n])
                if nxt_en < self._PARA_SHORT_CHARS:
                    return True
                nxt_ratio = len(tr_paras[tj_n]) / max(nxt_en, 1)
                return 0.3 <= nxt_ratio <= 3.0

            # FX3 (2026-10-05): çapa (display/şekil) anahtarları bir kez.
            en_keys = [self._para_anchor_key(p) for p in en_paras]
            tr_keys = [self._para_anchor_key(p) for p in tr_paras]

            ei = tj = 0
            while ei < len(en_paras) and tj < len(tr_paras):
                # FX3: çapada yerel kaymayı (kısa devam / eriyen kısa parça)
                # geri kur; sonra (ei, tj) aynı çapadır, olağan yoldan geçer.
                moved = self._para_anchor_resync(en_paras, tr_paras, en_keys,
                                                 tr_keys, ei, tj)
                if moved is not None:
                    ei, tj = moved
                    continue
                en_len = len(en_paras[ei])
                tr_len = len(tr_paras[tj])
                if en_len < self._PARA_SHORT_CHARS:
                    # Skip very short paragraphs (headings, labels)
                    ei += 1
                    tj += 1
                    continue
                ratio = tr_len / max(en_len, 1)
                if 0.3 <= ratio <= 3.0:
                    ei += 1
                    tj += 1
                    continue

                resynced = False
                # Hipotezler global paragraf sayısıyla kapılanır: gerçek
                # birleşme TR paragraf sayısını AZALTIR, bölünme ARTIRIR.
                # Sayılar eşitken hipotez denenmez — yoksa tekdüze belgede
                # gerçek bloat/bilgi kaybı da "birleşme" sanılıp susardı.
                if ratio > 3.0 and len(tr_paras) < len(en_paras):
                    # TR[tj], EN[ei..ei+k-1] paragraflarının birleşimi mi?
                    combined = en_len
                    for k in range(2, self._PARA_RESYNC_MAX_MERGE + 1):
                        if ei + k - 1 >= len(en_paras):
                            break
                        combined += len(en_paras[ei + k - 1]) + 2
                        merged_ratio = tr_len / max(combined, 1)
                        if (self._PARA_RESYNC_RATIO_LOW <= merged_ratio
                                <= self._PARA_RESYNC_RATIO_HIGH
                                and _next_pair_ok(ei + k, tj + 1)):
                            ei += k
                            tj += 1
                            resynced = True
                            break
                elif ratio < 0.3 and len(tr_paras) > len(en_paras):
                    # EN[ei], TR[tj..tj+k-1] paragraflarına mı bölünmüş?
                    combined = tr_len
                    for k in range(2, self._PARA_RESYNC_MAX_MERGE + 1):
                        if tj + k - 1 >= len(tr_paras):
                            break
                        combined += len(tr_paras[tj + k - 1]) + 2
                        merged_ratio = combined / max(en_len, 1)
                        if (self._PARA_RESYNC_RATIO_LOW <= merged_ratio
                                <= self._PARA_RESYNC_RATIO_HIGH
                                and _next_pair_ok(ei + 1, tj + k)):
                            ei += 1
                            tj += k
                            resynced = True
                            break
                if resynced:
                    continue

                # 2026-09-25 (kulvar Q): paragraf SINIRI KAYMASI. Sayfa
                # sonunda bölünmüş cümlenin devamı ayrı paragraf olur
                # (küçük harfle başlar: "и далее ... и др."); çevirmen
                # kelimeleri sınırın öte yanına taşıyınca paragraf sayısı
                # AYNI kalır, birleşme/bölünme hipotezi denenmez ve kısa
                # parça "bilgi kaybı" CRITICAL alırdı (bir kitapta 15/76 = %19,7).
                # Devam işareti var, komşu çift eksiği telafi ediyor ve iki
                # çiftin birleşik oranı belge oranına yakınsa içerik
                # kaybolmamış, yalnız yer değiştirmiştir.
                if self._paragraph_boundary_shift(en_paras, tr_paras, ei, tj,
                                                  _doc_ratio):
                    ei += 1
                    tj += 1
                    continue
                # FX3 (2026-10-05): metin AYNI ÇAPANIN öbür yanına taşınmış
                # (Türkçe söz dizimi; sentetik kalıp: "… açı şöyle
                # tanımlanınca <display> şeklindedir."): içerik korunmuş,
                # yalnız yer değiştirmiş.
                if self._anchor_boundary_shift(en_paras, tr_paras, en_keys,
                                               tr_keys, ei, tj, _doc_ratio):
                    ei += 1
                    tj += 1
                    continue

                if ratio < 0.3:
                    report.issues.append(QCIssue(
                        check_name="hallucination",
                        severity="critical",
                        line_num=1,  # paragraph-level, not line-level
                        message=(
                            f"Information loss: TR paragraph {tj + 1} "
                            f"is {ratio:.1%} of EN length "
                            f"({tr_len} vs {en_len} chars)"
                        ),
                        auto_fixable=False,
                        confidence=0.8,
                        fix_suggestion="Re-translate this paragraph",
                        context=tr_paras[tj][:120],
                    ))
                else:
                    report.issues.append(QCIssue(
                        check_name="hallucination",
                        severity="critical",
                        line_num=1,
                        message=(
                            f"Possible hallucination/repetition: TR paragraph "
                            f"{tj + 1} is {ratio:.1f}x EN length "
                            f"({tr_len} vs {en_len} chars)"
                        ),
                        auto_fixable=True,
                        confidence=0.8,
                        fix_suggestion="Check for repetition loops",
                        context=tr_paras[tj][:120],
                    ))
                ei += 1
                tj += 1

        # 29b. MT repetition loop detection
        for line_idx, line in enumerate(tr_lines):
            stripped = line.strip()
            if not stripped or len(stripped) < 30:
                continue
            if stripped.startswith("%") or stripped.startswith("\\begin") or stripped.startswith("\\end"):
                continue

            # F38 (2026-07-29): geri-referanslı tembel regex satır uzunluğunda
            # süperlineer (W=400 kelimede ~20 ms, kitapta geçiş başına
            # saniyeler). O(W) ön-eleme eşleşmesi imkânsız satırları eler;
            # kararı ve mesajdaki frazı hâlâ regex verir.
            if not may_have_repetition_loop(stripped):
                continue

            m = self._REPETITION_LOOP_RE.search(stripped)
            if m:
                repeated_phrase = m.group(1)
                report.issues.append(QCIssue(
                    check_name="hallucination",
                    severity="critical",
                    line_num=line_idx + 1,
                    message=(
                        f"MT repetition loop detected: "
                        f"'{repeated_phrase[:50]}' repeated 3+ times"
                    ),
                    auto_fixable=True,
                    confidence=0.95,
                    fix_suggestion=(
                        f"Truncate to first occurrence of "
                        f"'{repeated_phrase[:30]}'"
                    ),
                    context=stripped[:120],
                ))

        # 29c. Orphan dotted number detection
        if en_text and tr_text:
            # Extract dotted numbers from EN, excluding those inside
            # LaTeX commands (already protected by stash)
            en_clean = _MATH_BLOCK_RE.sub("", en_text)
            en_clean = _DISPLAY_BLOCK_RE.sub("", en_clean)
            en_clean = _INLINE_BLOCK_RE.sub("", en_clean)
            en_clean = _LATEX_CMD_RE.sub("", en_clean)

            tr_clean = _MATH_BLOCK_RE.sub("", tr_text)
            tr_clean = _DISPLAY_BLOCK_RE.sub("", tr_clean)
            tr_clean = _INLINE_BLOCK_RE.sub("", tr_clean)
            tr_clean = _LATEX_CMD_RE.sub("", tr_clean)

            en_nums = set(self._DOTTED_NUMBER_RE.findall(en_clean))
            tr_nums = set(self._DOTTED_NUMBER_RE.findall(tr_clean))
            # Türkçe ondalık virgül: "1,09" / "%7,5" = kaynaktaki "1.09" /
            # "7.5" (kulvar F2). Çeviride noktalı yazılmışsa zaten sayılır.
            tr_nums |= {num.replace(",", ".") for num in
                        self._TR_DECIMAL_COMMA_RE.findall(tr_clean)}

            # Numbers in EN but missing from TR
            # 2026-09-25 (kulvar Q): Check 22'nin baskı/OCR hatası diye
            # uzlaştırdığı kaynak numarası ("4.18" -> "1.18") öksüz değildir.
            missing_nums = en_nums - tr_nums - self._ocr_corrected_src_nums
            # Filter out very common numbers that might just be decimal
            # values (like 0.5, 1.0) -- focus on cross-ref style numbers
            missing_refs = {
                n for n in missing_nums
                if not n.startswith("0.") and "." in n
                and int(n.split(".")[0]) > 0
            }

            # Separate common decimals (low severity) from likely
            # cross-references (high severity).
            common_decimals = missing_refs & self._COMMON_DECIMAL_NUMBERS
            real_missing = missing_refs - common_decimals

            if len(real_missing) > 3:
                # Report as a batch to avoid flooding the report
                sample = sorted(real_missing)[:5]
                report.issues.append(QCIssue(
                    check_name="hallucination",
                    severity="high",
                    line_num=1,
                    message=(
                        f"Orphan dotted numbers: {len(real_missing)} numbers "
                        f"present in EN but missing in TR "
                        f"(e.g. {', '.join(sample)})"
                    ),
                    auto_fixable=False,
                    confidence=0.6,
                    fix_suggestion=(
                        "Cross-ref stash may have failed for these numbers"
                    ),
                    context="",
                ))
            elif real_missing:
                for num in sorted(real_missing):
                    report.issues.append(QCIssue(
                        check_name="hallucination",
                        severity="high",
                        line_num=1,
                        message=(
                            f"Orphan dotted number: '{num}' present in EN "
                            f"but missing in TR"
                        ),
                        auto_fixable=False,
                        confidence=0.6,
                        fix_suggestion=(
                            f"Verify cross-ref number '{num}' was preserved"
                        ),
                        context="",
                    ))

            # Report common decimal orphans at low severity -- these are
            # usually contextual value differences, not real hallucinations.
            if common_decimals:
                for num in sorted(common_decimals):
                    report.issues.append(QCIssue(
                        check_name="hallucination",
                        severity="low",
                        line_num=1,
                        message=(
                            f"Common decimal orphan: '{num}' in EN but not "
                            f"in TR (likely contextual difference)"
                        ),
                        auto_fixable=False,
                        confidence=0.3,
                        fix_suggestion=(
                            f"Verify '{num}' -- common decimal, may be "
                            f"paraphrased differently in Turkish"
                        ),
                        context="",
                    ))


    # ------------------------------------------------------------------
    # Check 30: ASCII Degradation Detection
    # ------------------------------------------------------------------

    # Turkish words that MUST contain special characters.
    # Maps: ASCII-degraded form -> correct Turkish form.
    # None means the word is already valid ASCII Turkish (skip).
    _ASCII_DEGRADATION_MAP: Dict[str, Optional[str]] = {
        "icin": "i\u00e7in",           # icin -> icin
        "onemli": "\u00f6nemli",       # onemli -> onemli
        "gosterir": "g\u00f6sterir",   # gosterir -> gosterir
        "ustunde": "\u00fcst\u00fcnde",  # ustunde -> ustunde
        "altinda": "alt\u0131nda",     # altinda -> altinda
        "uzerinde": "\u00fczerinde",   # uzerinde -> uzerinde
        "degil": "de\u011fil",         # degil -> degil
        "buyuk": "b\u00fcy\u00fck",    # buyuk -> buyuk
        "kucuk": "k\u00fc\u00e7\u00fck",  # kucuk -> kucuk
        "oldugu": "oldu\u011fu",       # oldugu -> oldugu
        "gercek": "ger\u00e7ek",       # gercek -> gercek
        "turkce": "t\u00fcrk\u00e7e",  # turkce -> turkce
        "sonuc": "sonu\u00e7",         # sonuc -> sonuc
        "baslangic": "ba\u015flang\u0131\u00e7",  # baslangic -> baslangic
        "sekil": "\u015fekil",         # sekil -> sekil
        "ornek": "\u00f6rnek",         # ornek -> ornek
        "ozellik": "\u00f6zellik",     # ozellik -> ozellik
        "islem": "i\u015flem",         # islem -> islem
        "cozum": "\u00e7\u00f6z\u00fcm",  # cozum -> cozum
        "donus": "d\u00f6n\u00fc\u015f",   # donus -> donus
        "cesitli": "\u00e7e\u015fitli",    # cesitli -> cesitli
        "guclu": "g\u00fc\u00e7l\u00fc",   # guclu -> guclu
        "bolum": "b\u00f6l\u00fcm",        # bolum -> bolum
        "denklem": None,                    # already ASCII-safe
        "fonksiyon": None,                  # already ASCII-safe
        "tablo": None,                      # already ASCII-safe
        "grafik": None,                     # already ASCII-safe
        "bicimde": "bi\u00e7imde",          # bicimde -> bicimde
        "gosterge": "g\u00f6sterge",        # gosterge -> gosterge
        "ustel": "\u00fcstel",              # ustel -> ustel
        "dogrusal": "do\u011frusal",        # dogrusal -> dogrusal
        "esleme": "e\u015fleme",            # esleme -> esleme
        "ozellikle": "\u00f6zellikle",      # ozellikle -> ozellikle
        "uygulama": None,                   # already ASCII-safe
        "tanimla": "tan\u0131mla",          # tanimla -> tanimla
        "degisken": "de\u011fi\u015fken",   # degisken -> degisken
        "donusum": "d\u00f6n\u00fc\u015f\u00fcm",  # donusum -> donusum
        "olcum": "\u00f6l\u00e7\u00fcm",    # olcum -> olcum
        "isaret": "i\u015faret",            # isaret -> isaret
        "cikti": "\u00e7\u0131kt\u0131",    # cikti -> cikti
        "giris": "giri\u015f",              # giris -> giris
        "calis": "\u00e7al\u0131\u015f",    # calis -> calis (stem)
        "icerik": "i\u00e7erik",            # icerik -> icerik
        "ucuncu": "\u00fc\u00e7\u00fcnc\u00fc",  # ucuncu -> ucuncu
        "birinci": None,                    # already ASCII-safe
        "ikinci": None,                     # already ASCII-safe
        "iliskin": "ili\u015fkin",          # iliskin -> iliskin
        "aciklama": "a\u00e7\u0131klama",  # aciklama -> aciklama
    }

    # Build a compiled regex that matches any of the degraded forms
    # (word boundary on both sides).
    _ASCII_DEGRADATION_RE: re.Pattern = re.compile(
        r"\b(?:" + "|".join(
            k for k, v in _ASCII_DEGRADATION_MAP.items() if v is not None
        ) + r")\b",
        re.IGNORECASE,
    )

    def _check_ascii_degradation(
        self,
        tr_text: str,
        tr_lines: List[str],
        report: QCReport,
    ) -> None:
        """Check 30: Detect ASCII degradation of Turkish characters.

        Turkish text MUST use proper Unicode characters (Türkçe karakter kuralı).
        This check finds words where Turkish special characters have been
        degraded to their ASCII equivalents:
            o -> o, s -> s, i -> i, c -> c, g -> g, u -> u

        Each detected word is a critical issue because ASCII degradation
        renders the Turkish text incorrect and unprofessional.
        """
        # Kod/matematik ICERIGI ve komut argumanlari maskelenir (#39):
        # eski satir-basi kontrolu lstlisting'in IC satirlarini tariyordu;
        # 'sonuc = hesapla(girdi)' gibi kod critical sayilip autofix
        # tarafinda hicbir zaman duzeltilemedigi icin skor kalici cokuyordu.
        # \url/\label/\includegraphics argumanlarinda ise fix uygulanirsa
        # icerik bozulur — onlar da maskelenir. Maske newline'lari korur,
        # satir numaralari degismez.
        def _blank(m: "re.Match") -> str:
            return re.sub(r"[^\n]", " ", m.group(0))

        masked = self._mask_protected_content(tr_text)
        masked = re.sub(
            r"\\(?:url|href|includegraphics|label|ref|eqref|pageref|cref|"
            r"autoref|cite|citep|citet|input|include|bibliography)"
            r"(?:\[[^\]]*\])?\s*\{[^{}]*\}",
            _blank,
            masked,
        )
        masked_lines = masked.split("\n")

        for line_idx, line in enumerate(masked_lines):
            # Baglam/atlamalar icin ORIJINAL satir kullanilir (maske
            # hizali: ayni satir sayisi).
            orig_line = tr_lines[line_idx] if line_idx < len(tr_lines) else line
            stripped = orig_line.strip()
            if not stripped:
                continue
            # Skip LaTeX comments
            if stripped.startswith("%"):
                continue
            # Skip pure LaTeX structural lines
            if stripped.startswith("\\begin") or stripped.startswith("\\end"):
                continue
            # Skip lines that are likely code/verbatim
            if stripped.startswith("\\verb") or stripped.startswith("\\lstinline"):
                continue

            for m in self._ASCII_DEGRADATION_RE.finditer(line):
                found_word = m.group(0).lower()
                correct = self._ASCII_DEGRADATION_MAP.get(found_word)
                if correct is None:
                    continue

                # Extra guard: verify this is actually the ASCII-degraded
                # form and not a false positive.  If the matched text
                # already contains any Turkish special character, skip.
                _TR_SPECIAL = set("\u00e7\u011f\u0131\u00f6\u015f\u00fc"
                                  "\u00c7\u011e\u0130\u00d6\u015e\u00dc")
                if any(ch in _TR_SPECIAL for ch in m.group(0)):
                    continue

                report.issues.append(QCIssue(
                    check_name="ascii_degradation",
                    severity="critical",
                    line_num=line_idx + 1,
                    message=(
                        f"ASCII bozulma: '{m.group(0)}' -> "
                        f"'{correct}' olmali (Türkçe karakter kuralı)"
                    ),
                    auto_fixable=True,
                    confidence=0.95,
                    fix_suggestion=f"Replace '{m.group(0)}' with '{correct}'",
                    context=stripped[:120],
                ))

    # ------------------------------------------------------------------
    # Check 31: Glossary Compliance
    # ------------------------------------------------------------------

    def _check_glossary_compliance(
        self,
        tr_text: str,
        en_text: str,
        tr_lines: List[str],
        en_lines: List[str],
        report: QCReport,
    ) -> None:
        """Check 31: Verify high-priority glossary terms are translated.

        Loads glossary JSON files (lazy) and checks whether English terms
        that have a high-priority Turkish translation still appear
        untranslated in the TR text.  Skips math/verbatim/code blocks.
        """
        glossary = self._load_glossary_for_qc()
        high_terms = glossary.get("high_terms", {})
        if not high_terms:
            return
        # KARAR 25: kitabın sözlüğü bir terim için başka karşılık seçtiyse
        # genel sözlüğün beklediği karşılık o kitapta aranmaz.
        secimler = self._kitap_secimleri()

        # Mask protected content so we don't flag terms inside math/code
        masked_tr = self._mask_protected_content(tr_text)
        masked_tr_lower = masked_tr.lower()

        # Also need to check if the Turkish equivalent IS present
        tr_lower = tr_text.lower()

        already_reported: Set[str] = set()

        # F9/F15 (2026-07-28): ~900 terim x tam-gövde regex taraması yerine
        # kelime->terim indeksi. Metnin \w+ kelime kümesi BİR kez çıkarılır;
        # tüm kelimeleri kümede olmayan terim için regex hiç çalışmaz.
        # Semantik birebir: tek kelimelik terimde küme üyeliği \b...\b
        # eşleşmesiyle özdeş, çok kelimeli terimde gerekli koşul (aday
        # yine regex ile doğrulanır). Metin/desen zaten lowercase olduğu
        # için IGNORECASE ek eşleşme üretmez.
        word_set = set(re.findall(r"\w+", masked_tr_lower))

        for en_term_lower, tr_expected in high_terms.items():
            if len(en_term_lower) < 4:
                continue

            term_words = re.findall(r"\w+", en_term_lower)
            if term_words:
                if any(w not in word_set for w in term_words):
                    continue
            elif en_term_lower not in masked_tr_lower:
                # \w içermeyen egzotik terim: ucuz alt-dize ön filtresi
                continue

            # Check if the English term appears in masked TR text
            # Use word boundary search to avoid substring matches
            try:
                pattern = re.compile(
                    r"\b" + re.escape(en_term_lower) + r"\b",
                    re.IGNORECASE,
                )
            except re.error:
                continue

            matches = list(pattern.finditer(masked_tr_lower))
            if not matches:
                continue

            # If Turkish equivalent is already present, skip
            tr_expected_lower = tr_expected.lower()
            if tr_expected_lower in tr_lower:
                continue

            # Avoid duplicate reports for same term
            if en_term_lower in already_reported:
                continue
            already_reported.add(en_term_lower)
            if (secimler is not None
                    and secimler.celisir_mi(en_term_lower, tr_expected)):
                continue

            # Find the line number of first occurrence
            first_match = matches[0]
            line_num = masked_tr[:first_match.start()].count("\n") + 1

            report.issues.append(QCIssue(
                check_name="glossary_compliance",
                severity="high",
                line_num=line_num,
                message=(
                    f"Glossary ihlali: '{en_term_lower}' cevirilmemis, "
                    f"'{tr_expected}' olmali ({len(matches)} yerde)"
                ),
                auto_fixable=True,
                confidence=0.85,
                fix_suggestion=(
                    f"Replace '{en_term_lower}' with '{tr_expected}'"
                ),
                context="",
            ))

    # ------------------------------------------------------------------
    # Check 31b: Kitap sözlüğüne göre terim tutarlılığı (G2c, KARAR 25)
    # ------------------------------------------------------------------

    def _kitap_secimleri(self):
        """Kitap sözlüğünün seçtiği karşılıklar (genel sözlük kuralıyla
        çelişki denetimi için) ya da None (sözlük yok / boş).

        KARAR 25 (kitap sözlüğü önceliklidir): kitabın sözlüğü bir terimin
        çevirisini açıkça seçiyorsa genel sözlüğün o çeviriyi "yanlış" sayan
        ya da başka karşılık bekleyen kuralı o kitapta uygulanmaz. Tembel;
        örnek başına bir kez kurulur.
        """
        if getattr(self, "_secimler_hazir", False):
            return getattr(self, "_secimler", None)
        self._secimler_hazir = True
        self._secimler = None
        sozluk = getattr(self, "_kitap_sozlugu", None)
        if sozluk is None:
            return None
        try:
            from latex_pipeline.kitap_sozlugu import KarsilikSecimleri
            secimler = KarsilikSecimleri(sozluk)
        except Exception as exc:  # muafiyet hatası raporu düşürmez
            logger.warning("Kitap sözlüğü seçimleri okunamadı: %s", exc)
            return None
        self._secimler = secimler if secimler else None
        return self._secimler

    def _check_term_consistency(
        self,
        tr_text: str,
        en_text: str,
        report: QCReport,
    ) -> None:
        """Aynı kaynak terime farklı karşılıklar (kitap sözlüğüne göre).

        mt okuma MT-36/MT-37: "anomaly" altı yerde "anormallik", bir yerde
        "anomali". Kaynak ve çeviri paragrafları eşlenir; terimin geçtiği
        paragrafta sözlük karşılığı yoksa "farklı karşılık" sayılır
        (``latex_pipeline.kitap_sozlugu.tutarlilik_bulgulari``).

        UYARIDIR: ``severity="low"``, ``auto_fixable=False`` — modelin
        terim seçimi otomatik değiştirilmez. En çok
        ``TUTARLILIK_EN_COK_BULGU`` bulgu (puan etkisi sınırlı); fazlası son
        bulgunun iletisinde sayılır. Sözlük ya da kaynak yoksa denetim
        koşmaz.
        """
        sozluk = getattr(self, "_kitap_sozlugu", None)
        if (sozluk is None or not en_text
                or not getattr(self, "_tutarlilik_denetimi", True)):
            return
        try:
            from latex_pipeline import kitap_sozlugu as _ks
            bulgular = _ks.tutarlilik_bulgulari(sozluk, en_text, tr_text)
        except Exception as exc:  # denetim hatası raporu düşürmez
            logger.warning("term_consistency denetimi koşamadı: %s", exc)
            return
        if not bulgular:
            return
        sinir = _ks.TUTARLILIK_EN_COK_BULGU
        for k, b in enumerate(bulgular[:sinir]):
            ileti = _ks.bulgu_iletisi(b)
            if k == sinir - 1 and len(bulgular) > sinir:
                ileti += f" (+{len(bulgular) - sinir} terim daha)"
            report.issues.append(QCIssue(
                check_name="term_consistency",
                severity="low",
                line_num=b.ilk_satir,
                message=ileti,
                auto_fixable=False,
                confidence=0.6,
                fix_suggestion="",
                context=b.ornek,
            ))

    # ------------------------------------------------------------------
    # Check 32: Wrong Translation Detection
    # ------------------------------------------------------------------

    # Hardcoded known-wrong translation patterns
    _KNOWN_WRONG_TRANSLATIONS: List[Tuple[str, str, str]] = [
        # (regex_pattern, correct_tr, description)
        (r"bilgisayar\s+a\u011f[ıi]", "yapay sinir a\u011f\u0131",
         "neural network yanl\u0131\u015f \u00e7eviri"),
        (r"(?:sak\u0131z|ak\u00e7aa\u011fa\u00e7)\s+(?:a\u011fac\u0131\s+)?re\u00e7ine",
         "", "MT hal\u00fcinasyon: re\u00e7ine"),
        # NOTE: single-book hallucination signatures (a few phrases tied to
        # one book's translation) were removed (QC-N8): they matched
        # legitimate prose in other books and, being
        # auto_fixable, corrupted valid Yol B output.  Only domain-general
        # errors remain above.
    ]

    def _check_wrong_translation(
        self,
        tr_text: str,
        tr_lines: List[str],
        report: QCReport,
    ) -> None:
        """Check 32: Detect known wrong translations and glossary violations.

        Uses two sources:
          1. Glossary 'wrong_alternatives' field
          2. Hardcoded _KNOWN_WRONG_TRANSLATIONS patterns

        Each match is critical because it's a CONFIRMED wrong translation.

        KARAR 25 (2026-10-06): kitabın sözlüğü (glossary_shared.tsv) bir
        karşılığı açıkça seçmişse ("token -> jeton (token)") o karşılığı
        "yanlış" sayan genel kural bu kitapta uygulanmaz; seçilmeyen yanlış
        karşılıklar bulunmaya devam eder (``_kitap_secimleri``).
        """
        masked_tr = self._mask_protected_content(tr_text)
        masked_tr_lower = masked_tr.lower()
        masked_lines = masked_tr.split("\n")

        # --- Part A: Glossary wrong_alternatives ---
        glossary = self._load_glossary_for_qc()
        wrong_alts = glossary.get("wrong_alternatives", {})
        already_reported_wrong: Set[str] = set()
        # KARAR 25: kitabın sözlüğünün seçtiği karşılık "yanlış" sayılmaz.
        secimler = self._kitap_secimleri()
        muaf: List[str] = []

        # F9/F15 (2026-07-28): glossary_compliance ile aynı kelime-indeksi
        # ön filtresi — terim başına tam-gövde taraması ancak adaylarda.
        word_set = set(re.findall(r"\w+", masked_tr_lower))

        for wrong_lower, (correct_tr, en_term) in wrong_alts.items():
            if wrong_lower in already_reported_wrong:
                continue
            term_words = re.findall(r"\w+", wrong_lower)
            if term_words:
                if any(w not in word_set for w in term_words):
                    continue
            elif wrong_lower not in masked_tr_lower:
                continue
            # Word boundary match
            try:
                pat = re.compile(
                    r"\b" + re.escape(wrong_lower) + r"\b",
                    re.IGNORECASE,
                )
            except re.error:
                continue

            match = pat.search(masked_tr_lower)
            if not match:
                continue

            already_reported_wrong.add(wrong_lower)
            if secimler is not None and secimler.secili_mi(wrong_lower):
                muaf.append(wrong_lower)
                continue
            line_num = masked_tr[:match.start()].count("\n") + 1

            report.issues.append(QCIssue(
                check_name="wrong_translation",
                severity="critical",
                line_num=line_num,
                message=(
                    f"Yanlis ceviri: '{wrong_lower}' bulundu, "
                    f"dogrusu '{correct_tr}' "
                    f"(EN: '{en_term}')"
                ),
                auto_fixable=True,
                confidence=0.95,
                fix_suggestion=(
                    f"Replace '{wrong_lower}' with '{correct_tr}'"
                ),
                context="",
            ))

        # --- Part B: Hardcoded wrong patterns ---
        for regex_pat, correct_tr, description in self._KNOWN_WRONG_TRANSLATIONS:
            try:
                compiled = re.compile(regex_pat, re.IGNORECASE)
            except re.error:
                continue

            for line_idx, line in enumerate(masked_lines):
                m = compiled.search(line)
                if m:
                    if (secimler is not None
                            and secimler.desen_secili_mi(compiled)):
                        muaf.append(m.group(0).lower())
                        break
                    fix = (
                        f"Replace '{m.group(0)}' with '{correct_tr}'"
                        if correct_tr
                        else f"Remove hallucinated text: '{m.group(0)}'"
                    )
                    report.issues.append(QCIssue(
                        check_name="wrong_translation",
                        severity="critical",
                        line_num=line_idx + 1,
                        message=(
                            f"Bilinen yanlis ceviri: '{m.group(0)}' -- "
                            f"{description}"
                        ),
                        auto_fixable=bool(correct_tr),
                        confidence=0.95,
                        fix_suggestion=fix,
                        context=line.strip()[:120],
                    ))
                    break  # one report per pattern is enough

        if muaf:
            logger.info(
                "QC wrong_translation: kitap sözlüğünün seçtiği %d karşılık "
                "genel sözlük kuralından muaf tutuldu (%s).",
                len(muaf), ", ".join(sorted(set(muaf))[:5]),
            )

    # ------------------------------------------------------------------
    # Check 33: Hallucination Patterns
    # ------------------------------------------------------------------

    # Repeated word: same word 3+ times consecutively
    _REPEATED_WORD_RE = re.compile(
        r"\b(\w{3,})\s+(?:\1\s+){2,}\1?\b", re.IGNORECASE,
    )

    # Repeated phrase: same 4+ word phrase appearing 2+ times
    _REPEATED_PHRASE_RE = re.compile(
        r"(\b\w+(?:\s+\w+){3,}?)\s+(?:.*?\s+)?(\1)", re.IGNORECASE,
    )

    # Overly long word (30+ chars) - likely concatenation or hallucination
    _LONG_WORD_RE = re.compile(r"\b[a-zA-Z\u00c0-\u024f]{30,}\b")

    # Turkish consonant cluster: 5+ consonants in a row (not normal in Turkish)
    _CONSONANT_CLUSTER_RE = re.compile(
        r"[bcdfghjklmnpqrstvwxyz\u00e7\u011f\u015f]{5,}", re.IGNORECASE,
    )

    @staticmethod
    def _repeat_anchors(text: str) -> Counter:
        """Çeviriyle kaynak arasında değişmeyen belirteçler (sayı; Türkçe
        ondalık virgül noktaya çevrilir, ASCII büyük harfli ad, sayfa
        hedefi)."""
        return Counter(tok.replace(",", ".")
                       for tok in _REPEAT_ANCHOR_RE.findall(text))

    def _source_paragraphs(
        self, en_lines: List[str],
    ) -> List[Tuple[int, int, List[str], Counter]]:
        """Kaynak paragrafları (boş satırla ayrılmış, tablo satırları hariç):
        ``(ilk satır, son satır, kelimeler, değişmez belirteçler)`` —
        1 tabanlı; analiz başına bir kez (kulvar F2). Kelimeler
        ``lang_detect`` temizliğinden sonra (komut ve matematik atılmış).
        """
        # Önbellek anahtarı kaynağın kendisi: denetim analyze() dışında
        # (testlerde) farklı kaynaklarla doğrudan çağrılabilir.
        key = hash(tuple(en_lines))
        if (self._src_paragraphs is not None
                and self._src_paragraphs[0] == key):
            return self._src_paragraphs[1]
        ld = _lang_detect()
        clean_lines = _clean_for_language("\n".join(en_lines)).split("\n")
        out: List[Tuple[int, int, List[str], Counter]] = []
        self._src_paragraphs = (key, out)
        if len(clean_lines) != len(en_lines):  # pragma: no cover - hiza korunur
            return out
        tabular = self._find_tabular_lines(en_lines)
        start = 0
        for i in range(1, len(en_lines) + 2):
            raw = en_lines[i - 1] if i <= len(en_lines) else ""
            if raw.strip() and i not in tabular:
                if not start:
                    start = i
                continue
            if start:
                words: List[str] = []
                for k in range(start, i):
                    words.extend(ld.tokenize_words(clean_lines[k - 1]))
                out.append((start, i - 1, words, self._repeat_anchors(
                    "\n".join(en_lines[start - 1:i - 1]))))
                start = 0
        return out

    @staticmethod
    def _longest_repeated_span(words: List[str], count: int,
                               content_ok) -> int:
        """*words* içinde en az *count* kez geçen ve *content_ok* koşulunu
        sağlayan en uzun kelime dizisinin boyu (``REPEAT_SOURCE_NGRAM``'dan
        kısa ise 0)."""
        best = 0
        n = REPEAT_SOURCE_NGRAM
        while n <= len(words):
            grams = Counter(tuple(words[k:k + n])
                            for k in range(len(words) - n + 1))
            if not any(c >= count and content_ok(g)
                       for g, c in grams.items()):
                break
            best = n
            n += 1
        return best

    @staticmethod
    def _phrase_span(words: List[str], phrase: str) -> int:
        """Çeviri frazının (4 kelime) tüm geçişlerinde ORTAK kalan en uzun
        kesintisiz dizinin boyu: geçişler sağa/sola aynı kelimelerle
        uzadıkça büyür ("… ölçüm değerleri" üç geçişte farklı ekle biter)."""
        low = [w.lower() for w in words]
        target = phrase.split()
        size = len(target)
        occ = [k for k in range(len(low) - size + 1)
               if low[k:k + size] == target]
        if len(occ) < 2:
            return size
        right = 0
        while all(o + size + right < len(low) for o in occ) and len(
                {low[o + size + right] for o in occ}) == 1:
            right += 1
        left = 0
        while all(o - left - 1 >= 0 for o in occ) and len(
                {low[o - left - 1] for o in occ}) == 1:
            left += 1
        return size + left + right

    def _source_mirrors_repeat(
        self, tr_start: int, tr_end: int, tr_lines: List[str],
        en_lines: List[str], para_words: List[str], phrase: str, count: int,
    ) -> bool:
        """Çevirideki fraz tekrarı kaynakta da var mı? (kulvar F2; kurallar
        ``REPEAT_SOURCE_*`` sabitlerinin açıklamasında.) Kaynak yoksa ya da
        hizalama kanıtı yetersizse False (eski davranış)."""
        if not en_lines or not tr_lines:
            return False
        ratio = len(en_lines) / len(tr_lines)
        window = max(REPEAT_SOURCE_WINDOW_LINES,
                     REPEAT_SOURCE_WINDOW_SHARE * len(en_lines))
        lo = tr_start * ratio - window
        hi = tr_end * ratio + window
        tr_anchors = self._repeat_anchors(
            "\n".join(tr_lines[tr_start - 1:tr_end]))
        tr_total = sum(tr_anchors.values())
        best = None
        best_score = 0
        best_dice = 0.0
        for para in self._source_paragraphs(en_lines):
            p_start, p_end, _words, anchors = para
            if p_end < lo or p_start > hi:
                continue
            score = sum((tr_anchors & anchors).values())
            dice = 2.0 * score / max(1, tr_total + sum(anchors.values()))
            if dice > best_dice:
                best, best_score, best_dice = para, score, dice
        if (best is None or best_score < REPEAT_SOURCE_MIN_ANCHORS
                or best_dice < REPEAT_SOURCE_MIN_ANCHOR_DICE):
            return False
        function_words = _all_function_words()

        def _content(gram) -> bool:
            return any(len(w) >= REPEAT_SOURCE_CONTENT_MIN_CHARS
                       and w not in function_words for w in gram)

        need = max(REPEAT_SOURCE_NGRAM,
                   math.ceil(self._phrase_span(para_words, phrase)
                             * REPEAT_SOURCE_SPAN_RATIO))
        return self._longest_repeated_span(best[2], count, _content) >= need

    def _check_hallucination_patterns(
        self,
        tr_text: str,
        en_text: str,
        tr_lines: List[str],
        en_lines: List[str],
        report: QCReport,
    ) -> None:
        """Check 33: Detect hallucination patterns in translated text.

        Sub-checks:
            33a. Repeated word (3+ consecutive occurrences)
            33b. Consonant cluster (5+ consonants, not normal Turkish)
            33c. Fabricated reference (Sekil/Denklem refs not in source)
            33d. Overly long word (30+ chars)
            33e. Repeated phrase (same 4+ word phrase in same paragraph)
        """
        masked_tr = self._mask_protected_content(tr_text)
        masked_lines = masked_tr.split("\n")
        # 33b muafiyeti için kaynakça satırları (kulvar Q).
        bib_lines = self._find_bibliography_lines(masked_lines)

        for line_idx, line in enumerate(masked_lines):
            stripped = line.strip()
            if not stripped or len(stripped) < 10:
                continue
            if stripped.startswith("%"):
                continue
            if stripped.startswith("\\begin") or stripped.startswith("\\end"):
                continue

            # 33a. Repeated word detection
            for m in self._REPEATED_WORD_RE.finditer(stripped):
                repeated_word = m.group(1)
                # Skip very short or numeric
                if len(repeated_word) < 3 or repeated_word.isdigit():
                    continue
                report.issues.append(QCIssue(
                    check_name="hallucination_pattern",
                    severity="critical",
                    line_num=line_idx + 1,
                    message=(
                        f"Tekrarlayan kelime: '{repeated_word}' "
                        f"3+ kez ust uste"
                    ),
                    # Report-only: no AutoFixer handler for
                    # 'hallucination_pattern' (registry has 'hallucination'
                    # only), so True blocked convergence (QC-N7).
                    auto_fixable=False,
                    confidence=0.90,
                    fix_suggestion=(
                        f"Reduce repetition of '{repeated_word}'"
                    ),
                    context=stripped[:120],
                ))

            # 33b. Consonant cluster (unusual in Turkish)
            for m in self._CONSONANT_CLUSTER_RE.finditer(stripped):
                cluster = m.group(0)
                # Skip if inside what looks like a LaTeX command
                pos = m.start()
                if pos > 0 and stripped[pos - 1] == "\\":
                    continue
                # Skip common abbreviations/acronyms
                if cluster.isupper() and len(cluster) <= 6:
                    continue
                # 2026-09-25 (kulvar Q): yabancı özel ad ve künye ünsüz
                # kümesi taşır ("Partsch", "Armstrong", "Gettysburg",
                # "Zeitschrift"). Muaf: (1) büyük harfle başlayan kelime
                # (özel ad), (2) kaynakça/künye satırı, (3) kaynakta AYNEN
                # geçen kelime (bağlantı kalıntısı "xhtml" dahil — o kusur
                # markdown_link_remnant'ın işidir).
                w_start, w_end = m.start(), m.end()
                while w_start > 0 and stripped[w_start - 1].isalpha():
                    w_start -= 1
                while w_end < len(stripped) and stripped[w_end].isalpha():
                    w_end += 1
                word = stripped[w_start:w_end]
                if word[:1].isupper():
                    continue
                if ((line_idx + 1) in bib_lines
                        or _lang_detect().looks_like_citation(stripped)):
                    continue
                if self._in_source_verbatim(word):
                    continue
                report.issues.append(QCIssue(
                    check_name="hallucination_pattern",
                    severity="medium",
                    line_num=line_idx + 1,
                    message=(
                        f"Anormal unsuz kumesi: '{cluster}' "
                        f"({len(cluster)} unsuz yan yana)"
                    ),
                    auto_fixable=False,
                    confidence=0.90,
                    fix_suggestion="Check for garbled text or OCR error",
                    context=stripped[:120],
                ))

            # 33d. Overly long word
            for m in self._LONG_WORD_RE.finditer(stripped):
                long_word = m.group(0)
                report.issues.append(QCIssue(
                    check_name="hallucination_pattern",
                    severity="high",
                    line_num=line_idx + 1,
                    message=(
                        f"Asiri uzun kelime ({len(long_word)} karakter): "
                        f"'{long_word[:40]}...'"
                    ),
                    auto_fixable=False,
                    confidence=0.90,
                    fix_suggestion="Check for concatenated words or hallucination",
                    context=stripped[:120],
                ))

        # 33c. Fabricated reference detection
        if en_text:
            # Extract reference numbers from TR and EN
            tr_ref_re = re.compile(
                r"(?:\u015eekil|Sekil|Denklem|Tablo)\s+(\d+(?:\.\d+)+)",
                re.IGNORECASE,
            )
            # 2026-09-25 (dalga Q): kaynak tarafı altı kaynak dilin
            # Şekil/Tablo/Denklem karşılıklarını tanır ("Рис. 1.1",
            # "Таблица 1.1", "Abb. 1.1", "Gl. (1.4)"); eskiden yalnız
            # Figure/Fig./Equation/Eq./Table vardı, Rusça bölümdeki her
            # doğru şekil numarası "uydurma" CRITICAL alıyordu.
            en_refs = set(_XREF_FABRICATION_SRC_RE.findall(en_text))
            tr_refs = set(tr_ref_re.findall(masked_tr))

            fabricated = tr_refs - en_refs
            for ref_num in sorted(fabricated):
                report.issues.append(QCIssue(
                    check_name="hallucination_pattern",
                    severity="critical",
                    line_num=1,
                    message=(
                        f"Uydurulmus referans: '{ref_num}' "
                        f"TR'de var ama EN'de yok"
                    ),
                    auto_fixable=False,
                    confidence=0.90,
                    fix_suggestion=(
                        f"Verify reference '{ref_num}' exists in source"
                    ),
                    context="",
                ))

        # 33e. Repeated phrase detection (paragraph level)
        # Split into paragraphs and check each
        # 2026-09-24 (M): tablo satırları paragraf metni değildir; hücre
        # değerlerinin tekrarı ("& > 0 &" bir tabloda 3 kez) doğal
        # tablo yapısıdır, halüsinasyon değil. Tabular satırları atlanır.
        tabular_line_nums = self._find_tabular_lines(masked_lines)
        # Öğe: (ilk satır, son satır, metin) — son satır kaynak eşlemesi
        # için (kulvar F2).
        paragraphs: List[Tuple[int, int, str]] = []
        current_para: List[str] = []
        para_start = 0
        for i, line in enumerate(masked_lines):
            stripped = line.strip()
            if (i + 1) in tabular_line_nums:
                stripped = ""
            if not stripped:
                if current_para:
                    paragraphs.append((para_start, i, " ".join(current_para)))
                    current_para = []
            else:
                if not current_para:
                    para_start = i + 1
                current_para.append(stripped)
        if current_para:
            paragraphs.append((para_start, len(masked_lines),
                               " ".join(current_para)))

        for para_line, para_end, para_text in paragraphs:
            if len(para_text) < 100:
                continue
            # Extract 4-word sequences and find duplicates
            words = para_text.split()
            seen_phrases: Dict[str, int] = {}
            for wi in range(len(words) - 3):
                phrase = " ".join(words[wi:wi + 4]).lower()
                # Skip phrases that are mostly LaTeX commands
                if "\\" in phrase:
                    continue
                seen_phrases[phrase] = seen_phrases.get(phrase, 0) + 1

            for phrase, count in seen_phrases.items():
                if count >= 3:
                    # 2026-09-30 (kulvar F2): tekrar
                    # kaynakta da (aynı ya da fazla sayıda) varsa çevirinin
                    # sadakatidir, halüsinasyon değil ("The value was
                    # measured" x9 -> "değerleri tek tek ölçtü" x5).
                    if self._source_mirrors_repeat(
                            para_line, para_end, tr_lines, en_lines,
                            words, phrase, count):
                        continue
                    report.issues.append(QCIssue(
                        check_name="hallucination_pattern",
                        severity="high",
                        line_num=para_line,
                        message=(
                            f"Tekrarlayan fraz: '{phrase}' "
                            f"ayni paragrafta {count} kez"
                        ),
                        # Report-only: no AutoFixer handler for
                        # 'hallucination_pattern' (QC-N7).
                        auto_fixable=False,
                        confidence=0.90,
                        fix_suggestion=f"Reduce repetition of '{phrase}'",
                        context=para_text[:120],
                    ))

    # ------------------------------------------------------------------
    # Check 34: Turkish Naturalness
    # ------------------------------------------------------------------

    # English articles/prepositions that shouldn't appear in Turkish text.
    # "the" and "a" are always flagged when followed by a word.
    # "an" is only flagged when followed by a vowel-initial word, because
    # Turkish "an" (meaning "moment") followed by a consonant-initial word
    # is natural Turkish (e.g. "an icin", "an bile").
    # In English, "an" precedes vowel sounds, so "an + vowel" is a strong
    # signal for an English article false positive.
    @staticmethod
    def _looks_turkish_word(word: str, line_is_turkish: bool) -> bool:
        """Kelime Türkçe mi? Türkçe harf taşıyorsa evet; yaygın ASCII Türkçe
        kelime listesi ("olan", "ve") yalnız satırda Türkçe harf varsa
        kullanılır — listede İngilizce ile ortak kelimeler var ("model",
        "son", "her"); tamamen İngilizce satırda "a model" artikel kalır."""
        w = word.strip(".,;:!?()[]\"'’")
        if not w:
            return False
        if any(c in _TURKISH_CHARS for c in w):
            return True
        return line_is_turkish and w.lower() in _TURKISH_COMMON_WORDS

    # 2026-09-25 (dalga Q): kesme işaretiyle eklenmiş Türkçe ek artikel
    # değildir: "Mars'a giden", "Ay'a indirip", "NASA'ya" (düz ' ve
    # tipografik ’ ‘ ʼ). Eskiden "'a giden" İngilizce artikel sayılıyordu.
    _SUFFIX_APOSTROPHES = "'\u2019\u2018\u02bc"
    _ENGLISH_ARTICLE_RE = re.compile(
        r"(?<![" + _SUFFIX_APOSTROPHES + r"])"
        r"\b(?:the|a)\s+[a-zA-Z\u00c0-\u024f]+"
        r"|(?<![" + _SUFFIX_APOSTROPHES + r"])"
        r"\ban\s+[aeiouAEIOU\u00e0-\u00f6\u00f8-\u00ff][a-zA-Z\u00c0-\u024f]*",
        re.IGNORECASE,
    )

    # Very long sentence without punctuation (300+ chars, no period/comma)
    _GOOGLE_TRANSLATE_RE = re.compile(
        r"[^\.\,\;\:\!\?\n]{300,}",
    )

    # "tarafindan" + AKTIF fiil deseni (bozuk edilgen:
    # 'tarafından yapıyor'). 2026-07-17 (smoke bulgusu): eski desen
    # MESRU edilgenleri de yakaliyordu — 'yapılmıştır' ('ır' ile
    # bitiyor), 'yapılır', 'okunur'. Edilgen cati isaretleri (-ıl
    # ailesi ve -nır ailesi) tasiyan fiiller DISLANIR.
    _BROKEN_PASSIVE_RE = re.compile(
        r"taraf\u0131ndan\s+(?!\w*[ıiuü]l)(?!\w+n[ıiuü]r\b)"
        r"\w+(?:yor|[ıiuü]r|mak|mek|[ae]r)\b",
        re.IGNORECASE,
    )

    def _check_turkish_naturalness(
        self,
        tr_text: str,
        tr_lines: List[str],
        report: QCReport,
    ) -> None:
        """Check 34: Detect unnatural Turkish patterns in translation.

        Sub-checks:
            34a. English articles mixed into Turkish text
            34b. Google-Translate-style long unpunctuated sentences
            34c. Broken passive constructions
        Only flags high-confidence patterns to minimize false positives.
        Severity is medium since these are naturalness issues, not errors.
        """
        masked_tr = self._mask_protected_content(tr_text)
        masked_lines = masked_tr.split("\n")

        for line_idx, line in enumerate(masked_lines):
            stripped = line.strip()
            if not stripped or len(stripped) < 20:
                continue
            if stripped.startswith("%"):
                continue
            if stripped.startswith("\\begin") or stripped.startswith("\\end"):
                continue
            # Skip lines that are primarily LaTeX commands
            if stripped.startswith("\\"):
                continue

            # 34a. English articles in Turkish text
            # Only flag if the article is surrounded by Turkish words
            # 5 Ekim 2026: Check 2 satırı çevrilmemiş ya da karışık dilli diye
            # zaten raporladıysa içindeki "the X" / "a X" o bulgunun
            # parçasıdır; her artikel için ikinci kez sayılmaz (çevrilmemiş
            # bir paragraf N tane artikel bulgusu daha üretiyordu).
            _satir_raporlu = (
                (line_idx + 1) in getattr(self, "_untranslated_lines", {})
                or (line_idx + 1) in getattr(self, "_mixed_language_lines", ()))
            for m in (() if _satir_raporlu
                      else self._ENGLISH_ARTICLE_RE.finditer(stripped)):
                article_phrase = m.group(0)
                pos = m.start()

                # Skip if at line start (could be a heading reference)
                if pos == 0:
                    continue

                # Check context: is there Turkish text before it?
                before = stripped[:pos].strip()
                if not before:
                    continue

                # Check that this isn't part of a known English proper noun
                # or citation
                after_word = article_phrase.split()[-1] if article_phrase.split() else ""
                if after_word and after_word[0].isupper():
                    # Likely a proper noun like "the Reynolds", skip
                    continue
                # 2026-09-24 (M): tek harfli "a" matematik metninde çoğu kez
                # değişkendir ("yarıçapı a olan", "a doğrusu"; bir kitapta 3 sahte
                # MEDIUM). İngilizce artikel İngilizce ismin önünde durur;
                # ardındaki kelime TÜRKÇE ise (Türkçe harf / yaygın Türkçe
                # kelime) "a" artikel değildir.
                if (article_phrase.split()[0].lower() == "a"
                        and self._looks_turkish_word(
                            after_word,
                            any(c in _TURKISH_CHARS for c in stripped))):
                    continue
                # 2026-09-25 (kulvar Q): Latince kalıp ("a priori",
                # "a posteriori", "a fortiori") İngilizce artikel değildir.
                if (article_phrase.split()[0].lower() == "a"
                        and after_word.lower() in _LATIN_A_LOCUTIONS):
                    continue
                # 2026-09-30 (kulvar F2, MT-1): kaynakta aynen geçen yabancı
                # dil alıntısı Türkçe metin değildir; "a principiis", "a
                # viribus" Latincedir. Check 2'nin muaf tuttuğu satırlar ve
                # (Check 2'nin İngilizce saymadığı, ör. "Lex I - Corpus
                # omne ...") kaynakta aynen geçen Latince satır.
                if ((line_idx + 1) in getattr(self, "_foreign_quote_lines", ())
                        or (line_idx < len(tr_lines)
                            and self._is_source_latin_quote(
                                tr_lines[line_idx]))):
                    continue

                report.issues.append(QCIssue(
                    check_name="turkish_naturalness",
                    severity="medium",
                    line_num=line_idx + 1,
                    message=(
                        f"Ingilizce artikel Turkce icinde: "
                        f"'{article_phrase}'"
                    ),
                    # 2026-09-25 (dalga Q): rapor-only. AutoFixer'da
                    # 'turkish_naturalness' için düzeltici YOK (kayıt
                    # defterinde yok; güven 0.70 < 0.75 eşiği zaten
                    # dağıtımı engelliyordu): "[auto-fixable]" etiketi
                    # kullanıcıyı yanıltıyordu. İleride düzeltici eklenirse
                    # kesme işaretli Türkçe eklere ("Mars'a") dokunmamalı.
                    auto_fixable=False,
                    confidence=0.70,
                    fix_suggestion=(
                        f"Remove English article from '{article_phrase}'"
                    ),
                    context=stripped[:120],
                ))

            # 34b. Google Translate marker: very long sentence without punctuation
            # 2026-09-30 (kulvar F2): kod/belirteç alıntısı
            # ("\texttt{SINY\allowbreak{}AL\_ornek...}") noktalama içermez
            # ama cümle değildir; tek kelimelik yer tutucuya indirgenir.
            for m in self._GOOGLE_TRANSLATE_RE.finditer(
                    mask_machine_tokens(stripped)):
                long_chunk = m.group(0).strip()
                # Only flag if it contains actual words (not LaTeX)
                word_count = len(re.findall(r"\b\w+\b", long_chunk))
                if word_count < 20:
                    continue
                report.issues.append(QCIssue(
                    check_name="turkish_naturalness",
                    severity="medium",
                    line_num=line_idx + 1,
                    message=(
                        f"Cok uzun noktalama olmadan cumle "
                        f"({len(long_chunk)} karakter, {word_count} kelime)"
                    ),
                    auto_fixable=False,
                    confidence=0.70,
                    fix_suggestion="Break into shorter sentences with punctuation",
                    context=long_chunk[:120],
                ))

            # 34c. Broken passive construction
            for m in self._BROKEN_PASSIVE_RE.finditer(stripped):
                report.issues.append(QCIssue(
                    check_name="turkish_naturalness",
                    severity="medium",
                    line_num=line_idx + 1,
                    message=(
                        f"Bozuk edilgen yapi: '{m.group(0)}'"
                    ),
                    auto_fixable=False,
                    confidence=0.70,
                    fix_suggestion=(
                        f"Fix passive construction: '{m.group(0)}'"
                    ),
                    context=stripped[:120],
                ))


    # ------------------------------------------------------------------
    # Check 35: Index Preservation
    # ------------------------------------------------------------------

    _INDEX_RE = re.compile(r"\\index\s*\{")

    def _check_index_preservation(
        self,
        tr_text: str,
        en_text: str,
        tr_lines: List[str],
        en_lines: List[str],
        report: QCReport,
    ) -> None:
        r"""Check 35: Compare \\index{...} counts between EN and TR.

        \\index commands are structural bookmarks that should survive
        translation unchanged.  Any count difference indicates lost or
        duplicated index entries.
        """
        if not en_text:
            return

        en_count = len(self._INDEX_RE.findall(en_text))
        tr_count = len(self._INDEX_RE.findall(tr_text))

        if en_count == 0:
            # Source has no index entries -- nothing to check
            return

        diff = en_count - tr_count
        if diff == 0:
            return

        if diff > 0:
            msg = (
                f"\\index kaybi: EN'de {en_count}, TR'de {tr_count} "
                f"({diff} adet eksik)"
            )
        else:
            msg = (
                f"\\index fazlaligi: EN'de {en_count}, TR'de {tr_count} "
                f"({-diff} adet fazla)"
            )

        report.issues.append(QCIssue(
            check_name="index_preservation",
            severity="high",
            line_num=1,
            message=msg,
            auto_fixable=False,
            confidence=0.95,
            fix_suggestion=(
                "Restore missing \\index{} entries from EN source"
                if diff > 0
                else "Remove duplicate \\index{} entries"
            ),
            context=f"EN: {en_count} index entries, TR: {tr_count}",
        ))

    # ------------------------------------------------------------------
    # Kulvar G4 (2026-09-30): okuma bulgularından gelen denetimler
    # (sabitler ve gerekçe: modül başındaki "Kulvar G4" bölümü)
    # ------------------------------------------------------------------

    @staticmethod
    def _emit_capped(report: QCReport, issues: List[QCIssue]) -> None:
        """Satır bazlı bulguları ekle; ``G4_LINE_ISSUE_CAP``'ten fazlası
        tek özet bulguda sayılır (toplam ve satır listesi iletide)."""
        if len(issues) <= G4_LINE_ISSUE_CAP:
            report.issues.extend(issues)
            return
        head, rest = issues[:G4_LINE_ISSUE_CAP], issues[G4_LINE_ISSUE_CAP:]
        report.issues.extend(head)
        first = rest[0]
        shown = ", ".join(str(i.line_num) for i in rest[:40])
        rank = {"critical": 3, "high": 2, "medium": 1, "low": 0}
        report.issues.append(QCIssue(
            check_name=first.check_name,
            severity=max((i.severity for i in rest),
                         key=lambda s: rank.get(s, 0)),
            line_num=first.line_num,
            message=(
                f"Aynı türden {len(rest)} satır daha (toplam "
                f"{len(issues)}; ilk {G4_LINE_ISSUE_CAP} tanesi ayrı "
                f"listelendi)"
            ),
            auto_fixable=False,
            confidence=min(i.confidence for i in rest),
            fix_suggestion=first.fix_suggestion,
            context="satırlar: " + shown + (" …" if len(rest) > 40 else ""),
        ))

    def _g4_skip_lines(self, tr_lines: List[str]) -> Set[int]:
        """Düzyazı denetimlerinin atladığı satırlar (1 tabanlı): boş satır,
        yorum, kod ortamı ve çok satırlı matematik (equation/align/… ve
        ``\\[ … \\]``)."""
        skip: Set[int] = set(self._find_code_env_lines(tr_lines))
        closing: Optional[str] = None
        for idx, line in enumerate(tr_lines, 1):
            st = line.strip()
            if not st or st.startswith("%"):
                skip.add(idx)
                continue
            if closing is not None:
                skip.add(idx)
                if closing in st:
                    closing = None
                continue
            m = _MATH_ENV_RE.search(st)
            if m and f"\\end{{{m.group(1)}}}" not in st[m.end():]:
                closing = f"\\end{{{m.group(1)}}}"
                skip.add(idx)
            elif st.startswith("\\[") and "\\]" not in st[2:]:
                closing = "\\]"
                skip.add(idx)
        return skip

    def _fragment_source_index(
        self, en_lines: List[str],
    ) -> "_FragmentSourceIndex":
        """Kaynak çapa dizini; autofix döngüsü aynı kaynağı defalarca verir,
        dizin kaynak anahtarıyla önbelleklenir."""
        key = getattr(self, "_src_ctx_key", None)
        cached = getattr(self, "_frag_src_cache", None)
        if cached is not None and key is not None and cached[0] == key:
            return cached[1]
        index = _FragmentSourceIndex(en_lines)
        self._frag_src_cache = (key, index)
        return index

    def _check_fragmented_lines(
        self,
        tr_text: str,
        en_text: str,
        tr_lines: List[str],
        en_lines: List[str],
        report: QCReport,
    ) -> None:
        """Parçalı yol çıktısı: formül/yer tutucu sınırlarında bölünüp her
        parçası ayrı çevrilmiş, okunamayan satır (okuma bulgusu MT-1).

        Belirtiler (satır kaynaktaki karşılığıyla çapa üzerinden hizalanır):

        * çapadan sonra cümle ortasında büyük harfle başlayan sıradan kelime
          ("$m_2$ Sırasıyla"), kaynakta aynı çapadan sonra küçük harf ya da
          noktalama gelirken;
        * ek belirtiler: satır başında boşluk (parçalı yolun birleştirme
          imzası; kaynak satırda yoksa), kaynakta olmayan "…", ek almış üç
          nokta ("…’nin", tek başına da yeterli), satır sonunda çevrilmemiş
          kaynak dili işlev kelimesi ("$m_1$ is"), kaynağa göre dengesiz
          ayraç.

        ``FRAGMENT_MIN_CAPS`` belirti ya da bir belirti + bir ek belirti
        bulgu olur; toplam ağırlık ``FRAGMENT_HIGH_WEIGHT``'e ulaşırsa HIGH,
        altındaysa MEDIUM. Kaynak yoksa denetim çalışmaz (hizalama gerekir).
        """
        if not en_text:
            return
        src_index = self._fragment_source_index(en_lines)
        lowers = set(_FRAGMENT_LOWER_WORD_RE.findall(tr_text))
        proper = {w for w in _FRAGMENT_MIDCAP_RE.findall(tr_text)
                  if w.lower() not in lowers}
        ctx = self._src_ctx
        src_runs = ctx.letter_runs if ctx is not None else frozenset()
        tail_re = _fragment_tail_re(ctx.lang if ctx is not None else None)
        skip = self._g4_skip_lines(tr_lines)
        n_src, n_tr = len(en_lines), len(tr_lines)
        window = max(REPEAT_SOURCE_WINDOW_LINES,
                     int(REPEAT_SOURCE_WINDOW_SHARE * n_src))
        found: List[QCIssue] = []

        for idx, line in enumerate(tr_lines):
            if (idx + 1) in skip:
                continue
            anchors = fragment_anchors(line)
            ell_suffix = _ELLIPSIS_SUFFIX_RE.search(line)
            if not anchors and not ell_suffix:
                continue
            caps: List[Tuple[str, str]] = []
            for _start, end, key in anchors:
                kind, word, apos = anchor_follow(line, end)
                if kind != "cap" or apos:
                    continue
                if (word in _FRAGMENT_LABEL_WORDS or word in proper
                        or word in src_runs):
                    continue
                caps.append((key, word))
            if not caps and not ell_suffix:
                continue

            src_line: Optional[str] = None
            symptoms: List[str] = []
            if caps:
                expect = int(idx * n_src / max(n_tr, 1))
                best = src_index.best_line(
                    [k for _s, _e, k in anchors], expect, window)
                if best is not None:
                    src_line = en_lines[best]
                    follow = src_index.follow.get(best, {})
                    for key, word in caps:
                        kinds = follow.get(key)
                        # Kaynakta bu çapa yok ya da kaynak da aynı yerde
                        # büyük harfle sürüyor (başlık, özel ad): belirti değil.
                        if kinds is None or "cap" in kinds:
                            continue
                        symptoms.append(word)
            if not symptoms and not ell_suffix:
                continue

            extras: List[str] = []
            ref = src_line or ""
            if re.match(r"^ [^\s\\]", line) and not ref.startswith(" "):
                extras.append("satır başında boşluk (parçalı yol imzası)")
            if (src_line is not None
                    and len(_ELLIPSIS_RE.findall(line))
                    > len(_ELLIPSIS_RE.findall(src_line))):
                extras.append("kaynakta olmayan '…'")
            if ell_suffix:
                extras.append(
                    f"ek almış üç nokta '{ell_suffix.group(0).strip()}'")
            tail = tail_re.search(line.rstrip())
            if tail:
                extras.append(
                    f"satır sonunda çevrilmemiş '{tail.group(1)}'")
            if (src_line is not None
                    and paren_balance(line) != paren_balance(src_line)):
                extras.append("kaynağa göre dengesiz ayraç")

            if not (len(symptoms) >= FRAGMENT_MIN_CAPS
                    or (symptoms and extras) or ell_suffix):
                continue
            # Ağırlık: her büyük harfli parça ve her ek belirti 1, ek almış
            # üç nokta 2. Yalnız iki belirti (ör. "$q$ Birim kütleye…" iki
            # kez) anlamı çoğunlukla korur → MEDIUM; daha fazlası okunamaz
            # satırdır → HIGH (okuma bulgusu MT-1: 15 satırın hepsi bozuk).
            weight = len(symptoms) + len(extras) + (1 if ell_suffix else 0)
            severity = ("high" if weight >= FRAGMENT_HIGH_WEIGHT
                        else "medium")
            parts: List[str] = []
            if symptoms:
                shown = ", ".join(symptoms[:5])
                if len(symptoms) > 5:
                    shown += f" (+{len(symptoms) - 5})"
                parts.append(
                    "formülden sonra cümle ortasında büyük harfle başlayan "
                    f"parça: {shown}")
            parts.extend(extras)
            found.append(QCIssue(
                check_name="fragmented_line",
                severity=severity,
                line_num=idx + 1,
                message=(
                    "Parçalı çeviri belirtisi (satır formül sınırlarında "
                    "bölünüp parça parça çevrilmiş"
                    + (", okunamıyor): " if severity == "high"
                       else "; cümle sınırları bozuk): ")
                    + "; ".join(parts)
                ),
                auto_fixable=False,
                confidence=0.85 if len(symptoms) >= FRAGMENT_MIN_CAPS else 0.75,
                fix_suggestion=(
                    "Satırı kaynaktan bütün olarak yeniden çevirin (yer "
                    "tutucu kaybında parçalı yol çıktısı kullanılmamalı)"
                ),
                context=line.strip()[:120],
            ))
        self._emit_capped(report, found)

    def _check_number_corruption(
        self,
        tr_text: str,
        en_text: str,
        tr_lines: List[str],
        en_lines: List[str],
        report: QCReport,
    ) -> None:
        """Sayı/karşılaştırma bozulması (okuma bulgusu MT-7: kaynakta
        "Bi < 0,1", çeviride "Bi < ≤ 0 , ≤ 1").

        Satır başına üç belirti, her biri kaynağa karşı doğrulanır:

        * art arda iki karşılaştırma işareti ("< ≤") ve kaynakta bu dizi yok;
        * bölünmüş ondalık ("0 , 1", "0 , ≤ 1") ve kaynakta bitişik "0,1" ya
          da "0.1" var ama aynı bölünmüş biçim yok;
        * iki sayı arasına işaret girmiş ("0 ≤ 1") ve kaynakta bitişik ondalık
          var ama bu karşılaştırma yok.

        Kaynaktaki karşılaştırma ifadesinin ("< 0,1") çeviride kaybolduğu
        iletide ayrıca belirtilir. Kaynak yoksa denetim çalışmaz.
        """
        if not en_text:
            return
        src_norm = rel_normalize(en_text)
        src_compact = _WS_RE.sub("", src_norm)
        src_pairs = Counter(
            (rel, num.replace(",", "."))
            for rel, num in _REL_NUM_RE.findall(src_norm))
        skip = self._g4_skip_lines(tr_lines)
        found: List[QCIssue] = []

        def _src_has_decimal(a: str, b: str) -> bool:
            return re.search(
                rf"(?<![\d.,]){a}[.,]{b}(?!\d)", src_norm) is not None

        for idx, line in enumerate(tr_lines, 1):
            if idx in skip or not any(ch.isdigit() for ch in line):
                continue
            norm = rel_normalize(line)
            problems: List[str] = []
            lost: List[str] = []
            for m in _DOUBLE_REL_RE.finditer(norm):
                if _WS_RE.sub("", m.group(0)) in src_compact:
                    continue
                if "LATEX" in norm[m.end():m.end() + 12]:
                    continue  # bozuk yer tutucu: placeholder_orphans'ın işi
                problems.append(
                    "art arda iki karşılaştırma işareti "
                    f"'{m.group(1)} {m.group(2)}'")
            for m in _SPLIT_DECIMAL_RE.finditer(norm):
                a = m.group(1) or m.group(3)
                b = m.group(2) or m.group(4)
                if not _src_has_decimal(a, b):
                    continue
                if m.group(1):
                    same_in_src = re.search(
                        rf"(?<![\d.,]){a}[ \t]+,", src_norm)
                else:
                    same_in_src = re.search(
                        rf"(?<![\d.,]){a}[ \t]*,[ \t]*[<>≤≥]", src_norm)
                if same_in_src:
                    continue
                problems.append(
                    f"bölünmüş ondalık '{_WS_RE.sub(' ', m.group(0)).strip()}' "
                    f"(kaynakta {a},{b})")
                tr_pairs = {(r, n.replace(",", "."))
                            for r, n in _REL_NUM_RE.findall(norm)}
                for (rel, num) in src_pairs:
                    if num == f"{a}.{b}" and (rel, num) not in tr_pairs:
                        lost.append(f"{rel} {a},{b}")
            for m in _REL_BETWEEN_NUMS_RE.finditer(norm):
                a, rel, b = m.group(1), m.group(2), m.group(3)
                if not _src_has_decimal(a, b):
                    continue
                if re.search(rf"(?<![\d.,]){a}[ \t]*{re.escape(rel)}[ \t]*{b}"
                             rf"(?![\d.,])", src_norm):
                    continue
                problems.append(
                    f"sayıların arasına kaynakta olmayan '{rel}' girmiş "
                    f"('{a} {rel} {b}', kaynakta {a},{b})")
            if not problems:
                continue
            msg = "Sayı bozulması: " + "; ".join(dict.fromkeys(problems))
            if lost:
                msg += ("; kaynaktaki karşılaştırma çeviride yok: "
                        + ", ".join(f"'{x}'" for x in dict.fromkeys(lost)))
            found.append(QCIssue(
                check_name="number_corruption",
                severity="high",
                line_num=idx,
                message=msg,
                auto_fixable=False,
                confidence=0.85,
                fix_suggestion=(
                    "Sayı ve karşılaştırma ifadesini kaynaktan aynen geri "
                    "yükleyin (ondalık virgülle, araya işaret girmeden)"
                ),
                context=line.strip()[:120],
            ))
        self._emit_capped(report, found)

    @staticmethod
    def _caption_plain(text: str) -> str:
        """Altyazı metninden düz yazı: biçim komutlarının içeriği kalır,
        öteki komutlar (argümanlarıyla) ve ayraçlar atılır."""
        text = _TEXT_FMT_OPEN_RE.sub("", text)
        text = re.sub(r"\\(?:href|url)\s*\{[^{}]*\}", " ", text)
        text = re.sub(
            r"\\(?:label|ref|eqref|cite\w*|includegraphics|footnote"
            r"|textsuperscript)\*?(?:\s*\[[^\]]*\])?\s*\{[^{}]*\}", " ", text)
        text = _BARE_CMD_NAME_RE.sub(" ", text)
        text = re.sub(r"\\.", " ", text)
        return text.replace("{", "").replace("}", "")

    def _check_caption_structure(
        self,
        tr_text: str,
        en_text: str,
        tr_lines: List[str],
        en_lines: List[str],
        report: QCReport,
    ) -> None:
        r"""Altyazı iskeleti (okuma bulguları MT-17, gpt-oss altyazı
        kaybı):

        * ``\caption`` sayısı kaynaktan az: altyazı ve numaralı etiket
          kaybolmuş → HIGH; metin etiketiyle birlikte float içinde ya da
          hemen ardında düz paragraf olarak duruyor → MEDIUM; fazla → LOW;
        * ayraç yalnız etiketi taşıyor, metin dışarıda
          (``\caption*{\textbf{Şekil} 1.1:} Durağan …``) → MEDIUM;
        * altyazıya gövde paragrafı yapışmış: etiketi ve açıklamasını taşıyan
          biçim grubundan sonra ayraç içinde (ya da ayraçtan sonra) süren uzun
          metin → MEDIUM;
        * float ortamında altyazı dışında düz metin (altyazı ayracı düşmüş ya
          da gövde paragrafı float'a girmiş) → MEDIUM.
        """
        # Altyazı kaybı raporlandıysa altyazısız float'lardaki başıboş metin
        # aynı kusurun sonucudur, ikinci kez sayılmaz.
        caption_lost = False
        if en_text:
            cap_src = len(_CAPTION_OPEN_RE.findall(en_text))
            cap_tr = len(_CAPTION_OPEN_RE.findall(tr_text))
            if cap_tr != cap_src:
                missing = cap_tr < cap_src
                caption_lost = missing
                n_missing = cap_src - cap_tr
                # Altyazı metni etiketiyle ("Şekil 1.1: …") float içinde ya
                # da hemen ardında düz metin olarak duruyorsa içerik yerinde,
                # yalnız \\caption yapısı gitmiştir → MEDIUM (biçim). Etiket
                # de yoksa (gpt-oss: yalnız açıklama metni kaldı) numara ve
                # altyazı kaybolmuştur → HIGH.
                # Kaynakta da aynı biçimde (altyazısız float + etiketli düz
                # paragraf) duran float'lar sayılmaz.
                traced = (max(0, self._captionless_float_label_traces(tr_lines)
                              - self._captionless_float_label_traces(en_lines))
                          if missing else 0)
                if not missing:
                    severity = "low"
                    message = (f"Fazla altyazı: kaynakta {cap_src}, çeviride "
                               f"{cap_tr} \\caption")
                elif traced >= n_missing:
                    severity = "medium"
                    message = (
                        f"Altyazı yapısı kayboldu: kaynakta {cap_src}, "
                        f"çeviride {cap_tr} \\caption; altyazı metni "
                        "etiketiyle birlikte float içinde ya da hemen "
                        "ardında düz paragraf olarak duruyor")
                else:
                    severity = "high"
                    message = (
                        f"Altyazı kaybı: kaynakta {cap_src}, çeviride "
                        f"{cap_tr} \\caption ({n_missing} eksik); numaralı "
                        "etiket ve altyazı biçimi kayboluyor")
                report.issues.append(QCIssue(
                    check_name="caption_structure",
                    severity=severity,
                    line_num=self._first_float_without_caption(tr_lines),
                    message=message,
                    auto_fixable=False,
                    confidence=0.9,
                    fix_suggestion=(
                        "Kaynaktaki \\caption{…} yapısını geri yükleyin"
                    ),
                    context=f"kaynak={cap_src}, çeviri={cap_tr}",
                ))

        label_only_lines: Set[int] = set()
        found: List[QCIssue] = []
        for m in _CAPTION_OPEN_RE.finditer(tr_text):
            grp = _read_brace_group(tr_text, m.end())
            if grp is None:
                continue
            arg, end = grp
            line_no = tr_text.count("\n", 0, m.start()) + 1
            if tr_lines[line_no - 1].lstrip().startswith("%"):
                continue
            eol = tr_text.find("\n", end)
            after = tr_text[end:eol if eol >= 0 else len(tr_text)]
            after_words = _PROSE_WORD_RE.findall(self._caption_plain(after))
            plain = self._caption_plain(arg)
            if _CAPTION_LABEL_ONLY_RE.match(plain):
                if len(after_words) >= 2:
                    label_only_lines.add(line_no)
                    found.append(QCIssue(
                        check_name="caption_structure",
                        severity="medium",
                        line_num=line_no,
                        message=(
                            "Altyazı metni \\caption ayracının dışında: ayraç "
                            f"yalnız '{_WS_RE.sub(' ', plain).strip() or '(boş)'}' "
                            "içeriyor, altyazı metni float içinde serbest "
                            "paragraf olarak basılıyor"
                        ),
                        auto_fixable=False,
                        confidence=0.9,
                        fix_suggestion=(
                            "Kapanış ayracını altyazı metninin sonuna taşıyın"
                        ),
                        context=tr_lines[line_no - 1].strip()[:120],
                    ))
                continue
            # Altyazıya yapışmış gövde paragrafı.
            body = arg.lstrip()
            fm = _TEXT_FMT_OPEN_RE.match(body)
            if not fm:
                continue
            inner_grp = _read_brace_group(body, fm.end())
            if inner_grp is None:
                continue
            inner, inner_end = inner_grp
            inner_plain = self._caption_plain(inner).strip()
            tail_words = (_PROSE_WORD_RE.findall(
                self._caption_plain(body[inner_end:])) + after_words)
            inner_words = _PROSE_WORD_RE.findall(inner_plain)
            if (_CAPTION_LABEL_START_RE.match(inner_plain)
                    and len(inner_words) > CAPTION_LABEL_GROUP_MIN_WORDS
                    and re.search(r"[.!?]\s*$", inner_plain)
                    and len(tail_words) >= CAPTION_GLUE_TAIL_MIN_WORDS
                    and len(inner_words) + len(tail_words)
                    >= CAPTION_GLUE_MIN_WORDS):
                found.append(QCIssue(
                    check_name="caption_structure",
                    severity="medium",
                    line_num=line_no,
                    message=(
                        "Altyazıya gövde paragrafı yapışmış: altyazı "
                        f"'{inner_plain[:40]}…' bittikten sonra aynı altyazıda "
                        f"{len(tail_words)} sözcük daha sürüyor "
                        f"('{' '.join(tail_words[:6])} …'); paragraf şekille "
                        "birlikte taşınıyor"
                    ),
                    auto_fixable=False,
                    confidence=0.8,
                    fix_suggestion=(
                        "Altyazıdan sonraki metni float dışına, kaynaktaki "
                        "yerine taşıyın"
                    ),
                    context=tr_lines[line_no - 1].strip()[:120],
                ))

        # Float içinde altyazı dışı düz metin.
        i = 0
        while i < len(tr_lines):
            m = _FLOAT_BEGIN_RE.search(tr_lines[i])
            if not m or tr_lines[i].lstrip().startswith("%"):
                i += 1
                continue
            env = m.group(1)
            end_tag = f"\\end{{{env}}}"
            j = i
            while j < len(tr_lines) and end_tag not in tr_lines[j]:
                j += 1
            block_lines = range(i + 1, min(j, len(tr_lines) - 1) + 2)
            block = "\n".join(tr_lines[i:j + 1])
            if (any(n in label_only_lines for n in block_lines)
                    or (caption_lost and not _CAPTION_OPEN_RE.search(block))):
                i = j + 1
                continue
            words = self._float_prose_words(block, env)
            if len(words) >= FLOAT_PROSE_MIN_WORDS:
                found.append(QCIssue(
                    check_name="caption_structure",
                    severity="medium",
                    line_num=i + 1,
                    message=(
                        f"Float ({env}) içinde altyazı dışında düz metin: "
                        f"{len(words)} sözcük ('{' '.join(words[:8])} …'); "
                        "altyazı ayracı düşmüş ya da gövde paragrafı "
                        "float'a girmiş"
                    ),
                    auto_fixable=False,
                    confidence=0.8,
                    fix_suggestion=(
                        "Metni \\caption{…} içine alın ya da float dışına, "
                        "kaynaktaki yerine taşıyın"
                    ),
                    context=" ".join(words[:20])[:120],
                ))
            i = j + 1
        self._emit_capped(report, found)

    def _captionless_float_label_traces(self, tr_lines: List[str]) -> int:
        r"""Altyazısız float'lardan kaçında altyazı metni etiketiyle
        ("Şekil 1.1: …", ``\textbf{Şekil 1.1:} …``) float içinde ya da
        float'tan sonraki ilk dolu satırda düz metin olarak duruyor."""
        count = 0
        i = 0
        n = len(tr_lines)
        while i < n:
            m = _FLOAT_BEGIN_RE.search(tr_lines[i])
            if not m or tr_lines[i].lstrip().startswith("%"):
                i += 1
                continue
            env = m.group(1)
            end_tag = f"\\end{{{env}}}"
            j = i
            while j < n and end_tag not in tr_lines[j]:
                j += 1
            block = "\n".join(tr_lines[i:j + 1])
            if not _CAPTION_OPEN_RE.search(block):
                inside = (self._float_prose_text(block, env)
                          .replace("{", "").replace("}", ""))
                k = j + 1
                while k < n and not tr_lines[k].strip():
                    k += 1
                after = tr_lines[k] if k < n else ""
                if (_CAPTION_LABEL_NUM_RE.match(inside)
                        or _CAPTION_LABEL_NUM_RE.match(
                            self._caption_plain(after))):
                    count += 1
            i = j + 1
        return count

    @staticmethod
    def _first_float_without_caption(tr_lines: List[str]) -> int:
        """Altyazısız ilk float'un satırı (yoksa 1)."""
        start: Optional[int] = None
        env = ""
        has_cap = False
        for idx, line in enumerate(tr_lines, 1):
            if start is None:
                m = _FLOAT_BEGIN_RE.search(line)
                if m:
                    start, env, has_cap = idx, m.group(1), False
                    has_cap = bool(_CAPTION_OPEN_RE.search(line))
                    if f"\\end{{{env}}}" in line[m.end():]:
                        if not has_cap:
                            return idx
                        start = None
                continue
            if _CAPTION_OPEN_RE.search(line):
                has_cap = True
            if f"\\end{{{env}}}" in line:
                if not has_cap:
                    return start
                start = None
        return 1

    def _float_prose_words(self, block: str, env: str) -> List[str]:
        """Float bloğunda altyazı ve iç ortamlar dışında kalan sözcükler."""
        return _PROSE_WORD_RE.findall(self._float_prose_text(block, env))

    def _float_prose_text(self, block: str, env: str) -> str:
        """Float bloğunda altyazı ve iç ortamlar dışında kalan düz metin."""
        body = re.sub(r"^.*?\\begin\{" + re.escape(env) + r"\}(?:\s*\[[^\]]*\])?",
                      "", block, count=1, flags=re.DOTALL)
        body = body.replace(f"\\end{{{env}}}", " ")
        # İç ortamlar (tabular, minipage, subfigure, tikzpicture…) bütünüyle.
        for _ in range(3):
            body = re.sub(r"\\begin\{([^{}]+)\}.*?\\end\{\1\}", " ", body,
                          flags=re.DOTALL)
        out: List[str] = []
        pos = 0
        for m in _CAPTION_OPEN_RE.finditer(body):
            if m.start() < pos:
                continue
            out.append(body[pos:m.start()])
            grp = _read_brace_group(body, m.end())
            pos = grp[1] if grp else m.end()
        out.append(body[pos:])
        text = " ".join(out)
        text = re.sub(r"(?<!\\)%[^\n]*", " ", text)
        text = _TEXT_FMT_OPEN_RE.sub("", text)
        text = re.sub(
            r"\\[A-Za-z@]+\*?(?:\s*\[[^\]]*\])*(?:\s*\{[^{}]*\})*", " ", text)
        text = re.sub(r"\\.", " ", text)
        return _WS_RE.sub(" ", text).strip()

    def _check_footnote_count(
        self,
        tr_text: str,
        en_text: str,
        tr_lines: List[str],
        en_lines: List[str],
        report: QCReport,
    ) -> None:
        r"""``\footnote`` sayısı kaynağa göre (çevirmen notu dipnotları
        hariç): eksik → HIGH (dipnot içeriği kayıp); not metni gövdeye
        gömülmüşse (satır içi "[* …]" ya da dipnot paragrafı) → MEDIUM;
        fazla → LOW."""
        if not en_text:
            return
        src_n = (len(_FOOTNOTE_OPEN_RE.findall(en_text))
                 - len(TRANSLATOR_NOTE_FOOTNOTE_OPEN_RE.findall(en_text)))
        tr_n = (len(_FOOTNOTE_OPEN_RE.findall(tr_text))
                - len(TRANSLATOR_NOTE_FOOTNOTE_OPEN_RE.findall(tr_text)))
        if src_n == tr_n:
            return
        missing = tr_n < src_n
        # Dipnot metni gövdeye gömülmüşse ("[* …]" satır içi not ya da
        # "\textsuperscript{N} metin" paragrafı; kaynaktakinden fazlası)
        # içerik yerinde, yalnız dipnot yapısı gitmiştir → MEDIUM (biçim).
        traces = 0
        if missing:
            def _traces(text: str) -> int:
                return (len(_INLINE_NOTE_RE.findall(text))
                        + sum(1 for ln in text.split("\n")
                              if _FOOTNOTE_DEF_LINE_RE.match(ln)))
            traces = max(0, _traces(tr_text) - _traces(en_text))
        if not missing:
            severity = "low"
            message = (f"Fazla dipnot: kaynakta {src_n}, çeviride {tr_n} "
                       "\\footnote (çevirmen notları hariç)")
        elif traces >= src_n - tr_n:
            severity = "medium"
            message = (f"Dipnot yapısı kayboldu: kaynakta {src_n}, çeviride "
                       f"{tr_n} \\footnote; not metni gövdeye gömülmüş "
                       "(satır içi '[* …]' ya da ayrı paragraf)")
        else:
            severity = "high"
            message = (f"Dipnot kaybı: kaynakta {src_n}, çeviride {tr_n} "
                       f"\\footnote ({src_n - tr_n} eksik; çevirmen notları "
                       "hariç)")
        report.issues.append(QCIssue(
            check_name="footnote_count",
            severity=severity,
            line_num=1,
            message=message,
            auto_fixable=False,
            confidence=0.9,
            fix_suggestion=(
                "Eksik dipnotları kaynaktan geri yükleyin" if missing
                else "Fazladan dipnotun kaynağını denetleyin"
            ),
            context=f"kaynak={src_n}, çeviri={tr_n}",
        ))

    def _check_source_language_sentences(
        self,
        tr_text: str,
        tr_lines: List[str],
        report: QCReport,
    ) -> None:
        """Türkçe satırın içinde kaynak dilde (de/fr/es/it/pt) cümle
        (okuma bulgusu MT-9: "In a wird die Wärmeleitfähigkeit behandelt.
        Bu problemin …").

        Neden ayrı denetim: Check 2'nin satır sınıflandırıcısı Türkçeye özgü
        harf (ş, ğ, ı) gördüğü satırı hiç sınıflandırmaz ve kısa satırda 3
        işlev kelimesi ister; Check 19 (foreign_language_leakage) belge
        başına aynı dilden en az 2 kelime ister ve "die"yi İngilizce
        sayıp beyaz listede tutar. MT-9'da ikisi de sessiz kaldı.

        Cümle kaynakta birebir geçiyorsa (``SOURCE_QUOTE_MIN_COVERAGE``)
        "çevrilmemiş cümle" (Check 2'nin uzunluk eşikleriyle HIGH/MEDIUM),
        geçmiyorsa model cümleyi kaynak dilde uydurmuş/yeniden yazmıştır
        (HIGH). Satırın geri kalanı Türkçe olmalıdır (tamamı kaynak dilde
        kalmış satır Check 2'nin işidir). Check 2'nin çevrilmemiş ya da
        alıntı saydığı satırlar, kaynakça, kod ve tablo satırları atlanır.
        İngilizce kaynakta çalışmaz (orada Check 2 cümle düzeyinde bakıyor).
        """
        ctx = self._src_ctx
        ld = _lang_detect()
        if ctx is None or ctx.lang not in ld.EUROPEAN_DETECT_LANGS:
            return
        lang = ctx.lang
        lang_name = ld.language_name_tr(lang)
        clean_lines = _clean_for_language(tr_text).split("\n")
        excluded = (self._find_bibliography_lines(tr_lines)
                    | self._find_code_env_lines(tr_lines)
                    | self._find_tabular_lines(tr_lines))
        found: List[QCIssue] = []
        for idx, clean in enumerate(clean_lines, 1):
            if (idx in excluded or idx in self._untranslated_lines
                    or idx in self._foreign_quote_lines
                    or idx > len(tr_lines)):
                continue
            raw = tr_lines[idx - 1].strip()
            if not raw or raw.startswith("%") or not clean.strip():
                continue
            if ld.looks_like_citation(clean):
                continue
            for sent in _SENTENCE_BOUNDARY_RE.split(clean):
                if ld.TURKISH_ONLY_RE.search(sent):
                    continue
                # Kısa biçimli grup ({…}: \textit/\emph argümanı) eser adıdır;
                # özgün dilinde bırakılması olağandır ("\textit{Observaciones
                # sobre la enseñanza de la Medicina}", derlem ölçümü).
                bare = _BRACE_GROUP_RE.sub(
                    lambda m: (m.group(0) if len(m.group(1).split())
                               > SOURCE_SENTENCE_TITLE_MAX_WORDS else " "),
                    sent)
                words = [w.lower() for w in ld.tokenize_words(bare)
                         if len(w) >= 2]
                if len(words) < ld.LINE_LATIN_MIN_WORDS:
                    continue
                hits, _family = ld.profile_hits(words)
                n_src = hits.get(lang, 0)
                if (n_src < SOURCE_SENTENCE_MIN_HITS
                        or hits.get("tr", 0) > 0
                        or n_src < SOURCE_QUOTE_LANG_DOMINANCE
                        * hits.get("en", 0)
                        or n_src / len(words) < SOURCE_SENTENCE_MIN_SHARE):
                    continue
                if is_latin_text(words):
                    continue
                # Yalnız TÜRKÇE satırın içindeki kaynak dilde cümle: satırın
                # geri kalanında Türkçe olmalı. Tamamı kaynak dilde kalmış
                # satır Check 2'nin (untranslated) ve başlıksa Check 11'in
                # alanıdır; burada ikinci kez sayılmaz (derlem ölçümü:
                # tümüyle çevrilmemiş İspanyolca/İtalyanca satırlar).
                rest = clean.replace(sent, " ", 1)
                rest_words = [w.lower() for w in ld.tokenize_words(rest)
                              if len(w) >= 2]
                if (len(rest_words) < SOURCE_SENTENCE_TR_REST_MIN_WORDS
                        or not (ld.TURKISH_ONLY_RE.search(rest)
                                or ld.profile_hits(rest_words)[0].get("tr", 0))):
                    continue
                sent_txt = _WS_RE.sub(" ", sent).strip()
                verbatim = (ctx.verbatim_coverage(sent)
                            >= SOURCE_QUOTE_MIN_COVERAGE)
                if verbatim:
                    severity = ("high" if len(sent_txt)
                                >= UNTRANSLATED_SENTENCE_CHARS else "medium")
                    msg = (f"Türkçe satırda çevrilmemiş {lang_name} cümle "
                           f"(kaynakta birebir var): '{sent_txt[:80]}'")
                else:
                    severity = "high"
                    msg = (f"Türkçe satırda {lang_name} cümle, kaynakta "
                           "birebir yok: model cümleyi kaynak dilde uydurmuş "
                           f"ya da yeniden yazmış: '{sent_txt[:80]}'")
                self._src_sentence_lines.add(idx)
                found.append(QCIssue(
                    check_name="source_language_sentence",
                    severity=severity,
                    line_num=idx,
                    message=msg,
                    auto_fixable=False,
                    confidence=0.85,
                    fix_suggestion=(
                        "Cümleyi kaynaktaki karşılığından yeniden çevirin"
                    ),
                    context=raw[:120],
                ))
                break  # satır başına bir bulgu
        self._emit_capped(report, found)

    def _check_pipeline_artifacts(
        self,
        tr_text: str,
        en_text: str,
        tr_lines: List[str],
        en_lines: List[str],
        report: QCReport,
    ) -> None:
        """Boru hattı artıkları (okuma bulguları MT-16/18).

        Kaynakta da olsalar raporlanır: PDF'teki kusur gerçektir. Biçim
        artıkları belge başına tek bulgu (sayı + satırlar), derleme hatası
        veren kaçışsız '&' satır başına HIGH, açılı ayraçları düşmüş
        "LATEX_N" yer tutucusu satır başına CRITICAL (Check 6 ile aynı
        ölçek).
        """
        code = self._find_code_env_lines(tr_lines)
        skip = self._g4_skip_lines(tr_lines) | self._find_tabular_lines(tr_lines)

        def _lines(nums: List[int]) -> str:
            shown = ", ".join(str(n) for n in nums[:15])
            return "satırlar: " + shown + (" …" if len(nums) > 15 else "")

        def _aggregate(*, check_name: str, severity: str, nums: List[int],
                       message: str, fix: str) -> None:
            if not nums:
                return
            report.issues.append(QCIssue(
                check_name=check_name,
                severity=severity,
                line_num=nums[0],
                message=message,
                auto_fixable=False,
                confidence=0.9,
                fix_suggestion=fix,
                context=_lines(nums),
            ))

        # 1) Gövdede dipnot tanımı paragrafı ve ona yapışan metin.
        def _defs(lines: List[str]) -> List[Tuple[int, str]]:
            out = []
            for n, ln in enumerate(lines, 1):
                m = _FOOTNOTE_DEF_LINE_RE.match(ln)
                if (m and len(_PROSE_WORD_RE.findall(m.group(2)))
                        >= FOOTNOTE_DEF_MIN_WORDS):
                    out.append((n, m.group(1)))
            return out

        defs = [(n, num) for n, num in _defs(tr_lines) if n not in code]
        def_lines = {n for n, _ in defs}
        src_defs = len(_defs(en_lines)) if en_lines else 0
        _aggregate(
            check_name="footnote_def_in_body",
            severity=("medium" if len(defs) >= FOOTNOTE_DEF_SYSTEMATIC
                      else "low"),
            nums=[n for n, _ in defs],
            message=f"PDF dipnotu \\footnote'a eşlenmemiş: {len(defs)} dipnot tanımı "
            "gövdede '\\textsuperscript{N} metin' paragrafı olarak duruyor"
            + (f" (kaynakta {src_defs}; Marker/dönüştürücü artığı)"
               if src_defs else ""),
            fix="Dipnot tanımlarını \\footnote{…} olarak işaretin yerine taşıyın",
        )
        merged_body: List[int] = []
        merged_note: List[int] = []
        for n, _num in defs:
            if n >= len(tr_lines):
                continue
            nxt = tr_lines[n]  # sonraki satır (0 tabanlı n = 1 tabanlı n+1)
            if not nxt.strip() or nxt.lstrip().startswith("%"):
                continue
            if _BLOCK_CMD_LINE_RE.match(nxt):
                continue
            if (n + 1) in def_lines:
                merged_note.append(n)
            elif len(_PROSE_WORD_RE.findall(nxt)) >= FOOTNOTE_DEF_MIN_WORDS:
                merged_body.append(n)
        _aggregate(
            check_name="footnote_body_merge", severity="medium",
            nums=merged_body,
            message=f"Gövde metni dipnot paragrafına yapışmış: {len(merged_body)} "
            "dipnot tanımından sonra boş satır yok, devam eden gövde "
            "paragrafı dipnotun parçası olarak basılıyor",
            fix="Dipnot tanımı ile sonraki gövde paragrafı arasına boş satır "
            "koyun (ya da dipnotu \\footnote{…} yapın)",
        )
        _aggregate(
            check_name="footnote_body_merge", severity="low",
            nums=merged_note,
            message=f"Ardışık dipnot tanımları tek paragrafta birleşmiş: "
            f"{len(merged_note)} yerde iki dipnot arasında boş satır yok",
            fix="Dipnot tanımlarını boş satırla ayırın",
        )

        # 2) Düz rakam dipnot işareti ("… almadık.} 3").
        def_nums = {num for _n, num in defs}
        plain_marks: List[int] = []
        if def_nums:
            for n, ln in enumerate(tr_lines, 1):
                if n in code or n in def_lines or ln.lstrip().startswith("%"):
                    continue
                m = _PLAIN_FOOTNOTE_MARK_RE.search(ln)
                if m and m.group(1) in def_nums:
                    plain_marks.append(n)
        _aggregate(
            check_name="plain_footnote_marker", severity="low",
            nums=plain_marks,
            message=f"Dipnot işareti üst simge değil, düz rakam: {len(plain_marks)} "
            "satırın sonunda cümle sonundan sonra dipnot numarası duruyor "
            "(sayı gibi okunuyor)",
            fix="İşareti \\textsuperscript{N} (ya da \\footnote) yapın",
        )

        # 3) Tek başına sayı paragrafı: sızmış sayfa numarası.
        page_nums: List[int] = []
        def _neighbour(n: int, step: int) -> str:
            k = n - 1 + step
            while 0 <= k < len(tr_lines) and not tr_lines[k].strip():
                k += step
            return tr_lines[k] if 0 <= k < len(tr_lines) else ""

        for n, ln in enumerate(tr_lines, 1):
            if n in skip or not _PAGE_NUMBER_LINE_RE.match(ln):
                continue
            prev = tr_lines[n - 2] if n >= 2 else ""
            nxt = tr_lines[n] if n < len(tr_lines) else ""
            if prev.strip() and not _BLOCK_CMD_LINE_RE.match(prev):
                continue
            if nxt.strip() and not _BLOCK_CMD_LINE_RE.match(nxt):
                continue
            before, after = _neighbour(n, -1), _neighbour(n, 1)
            # Kod örneğinin çıktısı / numarası ya da "…:" ile tanıtılan
            # değer: içerik, sayfa numarası değil.
            if (_CODE_BLOCK_EDGE_RE.match(before)
                    or _CODE_BLOCK_EDGE_RE.match(after)
                    or before.rstrip().endswith(":")):
                continue
            page_nums.append(n)
        _aggregate(
            check_name="page_number_leak", severity="medium",
            nums=page_nums,
            message="Metne sızmış sayfa ya da bölüm numarası: "
            f"{len(page_nums)} yerde tek başına sayıdan oluşan paragraf ("
            + ", ".join(f"'{tr_lines[n - 1].strip()}'" for n in page_nums[:5])
            + ")",
            fix="Sayfa numarası satırlarını silin",
        )

        # 4) Görünen HTML etiketi/varlığı ve 5) markdown artığı.
        artifact_doc_level = any(
            i.check_name == "html_artifact" and i.line_num == 1
            and not i.context for i in report.issues)
        artifact_lines = {i.line_num for i in report.issues
                          if i.check_name == "html_artifact"}
        html_hits: List[Tuple[int, str]] = []
        md_hits: List[Tuple[int, str]] = []
        for n, ln in enumerate(tr_lines, 1):
            if n in code or ln.lstrip().startswith("%"):
                continue
            masked = _VERB_INLINE_RE.sub(" ", _CODE_LIKE_ARG_RE.sub(" ", ln))
            if "$" in masked:
                masked = _blank_math_spans(masked)
            if n not in artifact_lines:
                for m in _VISIBLE_HTML_TAG_RE.finditer(masked):
                    if (artifact_doc_level
                            and self._HTML_ARTIFACT_RE.fullmatch(m.group(0))):
                        continue
                    tag = m.group(0)
                    name = re.match(r"</?\s*([A-Za-z][A-Za-z0-9]*)", tag)
                    name = name.group(1).lower() if name else ""
                    # Açılış etiketi yalnız aynı satırda kapanışı varsa
                    # artıktır ("<s>[INST] <<SYS>>" LLM istem biçimi).
                    if (not tag.startswith("</")
                            and name not in _HTML_VOID_TAGS
                            and not re.search(rf"</\s*{name}\s*>",
                                              masked[m.end():], re.I)):
                        continue
                    html_hits.append((n, tag))
                for m in _HTML_ENTITY_RE.finditer(masked):
                    html_hits.append((n, m.group(0)))
            for rx in (_MARKDOWN_BOLD_RE, _MARKDOWN_UNDERSCORE_RE):
                for m in rx.finditer(masked):
                    md_hits.append((n, m.group(0)))
        if html_hits:
            kinds = ", ".join(dict.fromkeys(t for _n, t in html_hits[:40]))
            _aggregate(
                check_name="visible_html_tag", severity="medium",
                nums=list(dict.fromkeys(n for n, _t in html_hits)),
                message=f"PDF'te görünen HTML etiketi/varlığı: {len(html_hits)} adet "
                f"({kinds[:80]})",
                fix="Etiketleri LaTeX karşılığına çevirin (<u>…</u> → "
                "\\underline{…}, <sup> → \\textsuperscript) ya da silin",
            )
        if md_hits:
            kinds = ", ".join(dict.fromkeys(t for _n, t in md_hits))
            _aggregate(
                check_name="markdown_remnant", severity="medium",
                nums=list(dict.fromkeys(n for n, _t in md_hits)),
                message=f"Markdown artığı: {len(md_hits)} adet '{kinds}' "
                "(PDF'te metin olarak basılıyor)",
                fix="Markdown işaretlerini silin ya da \\textbf{…} yapın",
            )

        # 6) Kaçışsız '&': derleme hatası, karakter düşer. Kök: kaynakta da
        # aynen kaçışsızsa Marker/dönüştürücü, kaynakta '\\&' ise çeviri.
        found: List[QCIssue] = []
        for n in self._unescaped_ampersand_lines(tr_lines):
            origin = ""
            for tok in _AMP_CONTEXT_RE.findall(tr_lines[n - 1]):
                tok = tok.strip("{}()[],.;:")
                if len(tok) < 3 or not en_text:
                    continue
                if tok in en_text:
                    origin = " (kaynakta da aynen kaçışsız: Marker/dönüştürücü artığı)"
                    break
                if tok.replace("&", "\\&") in en_text:
                    origin = (" (kaynakta '\\&' kaçışlı: kaçış çeviri adımında "
                              "kaybolmuş)")
                    break
            found.append(QCIssue(
                check_name="unescaped_ampersand",
                severity="high",
                line_num=n,
                message=(
                    "Hizalama ortamı dışında kaçışsız '&': derleme hatası "
                    "('Misplaced alignment tab character'), karakter PDF'ten "
                    "düşer" + origin
                ),
                auto_fixable=False,
                confidence=0.9,
                fix_suggestion="'&' yerine '\\&' yazın",
                context=tr_lines[n - 1].strip()[:120],
            ))
        self._emit_capped(report, found)

        # 7) Açılı ayraçları düşmüş yer tutucu: "“LATEX_1941”" (korunan
        # içerik — burada bir eser adı — geri konmamış). Check 6 yalnız
        # <<LATEX_N>> ve < < LATEX_N > > biçimlerini arar.
        bare: List[QCIssue] = []
        for n, ln in enumerate(tr_lines, 1):
            if n in code or "LATEX" not in ln or ln.lstrip().startswith("%"):
                continue
            covered = [m.span() for m in _ANY_LATEX_PLACEHOLDER_RE.finditer(ln)]
            masked = _VERB_INLINE_RE.sub(" ", _CODE_LIKE_ARG_RE.sub(" ", ln))
            for m in _BARE_LATEX_PLACEHOLDER_RE.finditer(masked):
                if any(a <= m.start() < b for a, b in covered):
                    continue
                bare.append(QCIssue(
                    check_name="placeholder_orphans",
                    severity="critical",
                    line_num=n,
                    message=(
                        "Açılı ayraçları düşmüş yer tutucu: "
                        f"'{m.group(0)}' (korunan içerik geri konmamış, "
                        "PDF'te kod olarak basılıyor)"
                    ),
                    auto_fixable=False,
                    confidence=0.9,
                    fix_suggestion=(
                        "Yer tutucunun yerine kaynaktaki içeriği geri yükleyin"
                    ),
                    context=ln.strip()[:120],
                ))
        self._emit_capped(report, bare)

    @staticmethod
    def _unescaped_ampersand_lines(tr_lines: List[str]) -> List[int]:
        """Hizalama ortamı (tabular, align, array, matris…) dışında kaçışsız
        '&' içeren satırlar (1 tabanlı; satır başına bir kez)."""
        stack: List[str] = []
        out: List[int] = []
        for n, line in enumerate(tr_lines, 1):
            pos = 0
            hit = False
            while True:
                m = _AMP_TOKEN_RE.search(line, pos)
                if not m:
                    break
                tok = m.group(0)
                pos = m.end()
                in_verbatim = any(e in _AMP_VERBATIM_ENVS for e in stack)
                if m.group(1):
                    if not in_verbatim:
                        stack.append(m.group(1).strip())
                elif m.group(2):
                    name = m.group(2).strip()
                    if name in stack:
                        while stack and stack.pop() != name:
                            pass
                elif in_verbatim:
                    continue
                elif tok.startswith("\\verb"):
                    close = line.find(m.group(3), pos)
                    pos = len(line) if close < 0 else close + 1
                elif tok.startswith("\\") and tok[1:2].isalpha():
                    # \href/\url adresi, \includegraphics dosya adı,
                    # \label/\ref anahtarı: '&' orada meşru (derlem ölçümü:
                    # "fig&5.png" hatasız derlendi). \href'in bağlantı METNİ
                    # taranmaya devam eder.
                    grp = _read_brace_group(line, pos)
                    if grp is not None:
                        pos = grp[1]
                elif tok == "%":
                    break
                elif tok == "&":
                    if not any(e in _AMP_ALIGN_ENVS for e in stack):
                        hit = True
            if hit:
                out.append(n)
        return out


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI: run QC on a translated .tex file, optionally with EN source."""
    import sys

    if len(sys.argv) < 2:
        print(
            "Usage: python -m latex_pipeline.qc_engine "
            "<translated.tex> [source.tex]"
        )
        sys.exit(1)

    from pathlib import Path

    tr_path = Path(sys.argv[1])
    en_path = Path(sys.argv[2]) if len(sys.argv) > 2 else None

    if not tr_path.exists():
        print(f"File not found: {tr_path}")
        sys.exit(1)

    tr_text = tr_path.read_text(encoding="utf-8", errors="replace")
    en_text = ""
    if en_path and en_path.exists():
        en_text = en_path.read_text(encoding="utf-8", errors="replace")

    engine = QCEngine()
    report = engine.analyze(tr_text, en_text)
    report.print_summary()


if __name__ == "__main__":
    import sys

    # If called with --test-first-person, run inline verification
    if len(sys.argv) > 1 and sys.argv[1] == "--test-first-person":
        print("=== first_person_academic pattern verification ===\n")
        regex = QCEngine._FIRST_PERSON_RE

        # These MUST NOT match (false positives in old code)
        false_positive_words = [
            "adim", "adım",       # step (noun)
            "egitim", "eğitim",   # training (noun)
            "yontem", "yöntem",   # method (noun)
            "sistem",             # system (noun)
            "algoritma",          # algorithm (noun)
            "tanim", "tanım",     # definition (noun)
            "terim",              # term (noun)
            "iletisim",           # communication (noun)
            "bildirim",           # notification (noun)
            "duzeltim",           # correction (noun)
        ]

        # These MUST match (true positives)
        true_positive_words = [
            "anlıyorum",          # I understand
            "anliyorum",          # I understand (ASCII)
            "yaptım",            # I did
            "yaptim",            # I did (ASCII)
            "biliyorum",          # I know
            "ben",                # I (pronoun)
            "benim",              # my (pronoun)
            "bana",               # to me
            "düşünüyorum",        # I think
            "dusunuyorum",        # I think (ASCII)
            "görüyorum",          # I see
            "goruyorum",          # I see (ASCII)
            "sanıyorum",          # I suppose
            "saniyorum",          # I suppose (ASCII)
            "kullandım",          # I used
            "buldum",             # I found
            "gördüm",            # I saw
            "hesapladım",         # I calculated
            "geliştirdim",        # I developed
            "okudum",             # I read
            "denedim",            # I tried
            "inceledim",          # I examined
            "gözlemledim",        # I observed
            "ölçtüm",            # I measured
            "söyleyebilirim",     # I can say
            "belirtmeliyim",      # I must state
        ]

        all_pass = True

        print("FALSE POSITIVE checks (must NOT match):")
        for word in false_positive_words:
            match = regex.search(word)
            status = "FAIL (matched!)" if match else "OK"
            if match:
                all_pass = False
            print(f"  {word:20s} -> {status}")

        print("\nTRUE POSITIVE checks (must match):")
        for word in true_positive_words:
            match = regex.search(word)
            status = "OK" if match else "FAIL (not matched!)"
            if not match:
                all_pass = False
            print(f"  {word:20s} -> {status}")

        print(f"\n{'='*50}")
        if all_pass:
            print("ALL TESTS PASSED")
        else:
            print("SOME TESTS FAILED")
        sys.exit(0 if all_pass else 1)

    main()
