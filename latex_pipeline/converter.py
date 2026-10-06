"""
Markdown-to-LaTeX converter.

Converts Marker-produced Markdown (with preserved LaTeX formulas) into
clean LaTeX body text.  The output is NOT a full document -- it must be
wrapped by TemplateEngine before compilation.

Design principles:
  - Formulas ($..$ and $$..$$) are NEVER modified.
  - Images are referenced via \\includegraphics with correct relative paths.
  - Tables are converted to booktabs format.
  - Headings, bold, italic, lists, code blocks are mapped 1:1.
  - Front matter (preface, acknowledgements, TOC) handled for book class.
  - Marker-specific artifacts are cleaned in preprocessing.

Usage:
    converter = MarkdownToLatex(config)
    latex_body = converter.convert(markdown_text, images_dict)
"""

from __future__ import annotations

import html
import logging
import bisect
import re
import unicodedata
import urllib.parse
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Set, Tuple

from latex_pipeline import kod_vurgu, marker_onarim, pdf_metin_onarim, vurgu_onarim
from latex_pipeline import kutu_onarim
from latex_pipeline.config import PipelineConfig
from latex_pipeline.patterns import (
    ALTAD_MARKER,
    APPENDIX_LABEL_WORDS,
    BACK_MATTER_HEADING_NAMES,
    BIBLIOGRAPHY_HEADING_NAMES,
    BOX_LABEL_WORDS,
    CHAPTER_LABEL_WORDS,
    CONTENTS_HEADING_NAMES,
    ENGLISH_NUMBER_WORDS,
    FLOAT_CAPTION_MAX_CHARS,
    FRONT_MATTER_HEADING_NAMES,
    NUMBER_WORDS_PATTERN,
    PART_LABEL_WORDS,
    ROMAN_NUMERAL_PATTERN,
    fold_accents,
    number_word_value,
    roman_to_int,
    xref_label_alternation,
)
# EPUB dipnot sözleşmesinin kimlik öneki (E1 ↔ C2; tek kaynak ayrıştırıcıda).
from parsers.epub_parser import NOTE_LABEL_PREFIX

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers -- TeX escaping (applied ONLY to plain text, never to formulas)
# ---------------------------------------------------------------------------

_TEX_SPECIAL = {
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}

# Regex: $ … $ (inline) or $$ … $$ (display).  We use a sentinel approach:
# 1.  Find all math spans and replace with <<MATH_n>> placeholders.
# 2.  Escape TeX specials in the remaining plain text.
# 3.  Restore the math spans verbatim.

_DISPLAY_MATH_RE = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)
# Opening $: must NOT be preceded by \ (escaped dollar) or $ (display math)
# Closing $: must NOT be preceded by $ (display math).
# NOTE: The closing lookbehind intentionally omits \\ to avoid breaking on
# TeX spacing commands like \, \; \! \: that end just before the closing $.
# Previously (?<![$\\]) on the closing side would reject $\alpha\,$ because
# the lookbehind saw the \ of \, -- but \, is a spacing command, not \$.
_INLINE_MATH_RE = re.compile(r"(?<![\$\\])\$(?!\$)(.+?)(?<!\$)\$(?!\$)")

# Front matter heading names (case-insensitive, after stripping bold markers)
_FRONT_MATTER_NAMES = {
    "preface", "foreword", "acknowledgements", "acknowledgments",
    "dedication", "contents", "table of contents", "about the authors",
    "about the author", "about this book", "notation", "list of figures",
    "list of tables", "list of symbols", "list of abbreviations",
}

# Page header artifact patterns (e.g., "xii Contents", "Contents xiii")
_PAGE_HEADER_RE = re.compile(
    r"^(?:[xivlcdm]+\s+(?:Contents|Preface|Foreword)|(?:Contents|Preface|Foreword)\s+[xivlcdm]+)$",
    re.IGNORECASE,
)

# Body page header artifacts (e.g., "2.2 Sample Methods 11")
# Pattern: section_number title page_number (short line, <60 chars)
# Guards:
#   - page_number >= 2 digits (10+) to avoid matching "Theorem 1"
#   - title text is ONLY letters/spaces/hyphens (NO digits) -- prevents
#     false positives like "2.1 Convergence Theorem 1"
#   - title max 45 chars (~5-6 words) -- real content lines are longer
#   - 1+ spaces before page_number
#   - total line length checked separately (< 60 chars)
_BODY_PAGE_HEADER_RE = re.compile(
    r"^\d+(?:\.\d+)+\s+[A-Z][A-Za-z\s(),-]{1,45}\s+\d{2,4}$"
)

# B4 fix: Booktabs / tablo kural komutları. Bir satır bu komutlardan
# birini içeriyorsa markdown başlığı olarak parse edilmemeli. Marker,
# PDF'den TOC'u bazen booktabs tablosuna dönüştürüyor ve bu komutlar
# başlık satırına sızabiliyor. Bu regex'i _try_heading() guard'ı
# kullanıyor (converter.py) ve translator.py'deki _BOOKTABS_BARE_RE
# ile eşleşir (tabular dışında kalan çıplak komutları yakalar).
_BOOKTABS_CMD_RE = re.compile(
    r"\\(?:toprule|midrule|bottomrule|hline|cmidrule|addlinespace|cline)\b"
)

# 2026-09-24: soru / alıştırma / örnek kelimeleri — "Problem 1.3", "Übung 2",
# "Задача 4.1" gibi NUMARALI başlıklar bölüm başlığı değildir (bkz.
# MarkdownToLatex._is_chapter_title_candidate). Kapsam CHAPTER_LABEL_WORDS ile
# aynı: TeX yolunun kaynak dilleri (İngilizce, Almanca, Rusça, Fransızca,
# İspanyolca, İtalyanca) + Türkçe. Büyük/küçük harf duyarsız.
NUMBERED_EXERCISE_WORDS = (
    r"(?i:Problem[aes]?|Probl[èe]me|Exercises?|Examples?"
    r"|Soru|Al[ıIi][şs]t[ıIi]rma|[ÖO]rnek"
    r"|Задача|Упражнение|Пример"
    r"|Aufgabe|[ÜU]bung|Beispiel"
    r"|Exercice|Exemple"
    r"|Ejercicio|Ejemplo|Esercizio|Esempio)"
)

# ---------------------------------------------------------------------------
# 2026-09-25 (Y1): çok dilli bölüm / kısım ETİKETİ çözümleyicisi
# ---------------------------------------------------------------------------
# Sözcük listeleri latex_pipeline/patterns.py'de tek kaynaktır. Biçimler:
#   ileri sıra : "Chapter 2", "Kapitel 1 Grundlagen – …", "CHAPITRE III. Le Gun-Club",
#                "Глава первая", "Capítulo primero", "Part Two", "Livre premier"
#   ters sıra  : "Zweites Kapitel", "Première partie", "3. Kapitel: Titel"
# Numara: Arap rakamı (≤3 hane), sayı sözcüğü (1-20, altı dil), Roma rakamı.
# Sözcük Roma rakamından ÖNCE denenir: "Chapitre dix" = 10 (Roma "DIX" = 509).
_LABEL_WORD_RE = (
    r"(?P<word>" + CHAPTER_LABEL_WORDS + r"|" + PART_LABEL_WORDS + r")"
)
_LABEL_NUM_RE = (
    r"(?P<num>\d{1,3}(?![\w\-])|" + NUMBER_WORDS_PATTERN
    + r"|(?i:" + ROMAN_NUMERAL_PATTERN + r")(?![\w\-]))"
)
_LABEL_FORWARD_RE = re.compile(
    r"^" + _LABEL_WORD_RE + r"\s+(?:(?:No|Nr|N[°º]|№)\.?\s*)?"
    + _LABEL_NUM_RE + r"(?P<rest>.*)$",
    re.DOTALL,
)
_LABEL_REVERSE_RE = re.compile(
    r"^(?P<num>(?:\d{1,3}|(?i:" + ROMAN_NUMERAL_PATTERN + r"))\.|"
    + NUMBER_WORDS_PATTERN + r")\s+" + _LABEL_WORD_RE
    + r"(?![\w\-])(?P<rest>.*)$",
    re.DOTALL,
)
# Etiketten sonra: bitiş, ya da ayraç (. : – — -) / boşluk + başlık.
_LABEL_REST_RE = re.compile(r"^(?:\s*[.:–—\-]+\s*|\s+)(?P<title>.*?)\s*$", re.DOTALL)
_PART_WORD_RE = re.compile(r"^" + PART_LABEL_WORDS + r"$")
# Ters sırada Arap/Roma rakamı + nokta yalnız Almancada deyimseldir
# ("3. Kapitel", "II. Teil"); İngilizce "1. Book reviews" etiket değildir.
_REVERSE_DIGIT_WORDS = frozenset({"kapitel", "teil", "buch"})
_ENGLISH_LABEL_WORDS = frozenset({"chapter", "part", "book"})
_APPENDIX_RE = re.compile(r"^" + APPENDIX_LABEL_WORDS + r"(?![\w\-])")
# Roma rakamıyla başlayan başlık: "I", "II.", "III — Titre", "IV. Titre",
# "V LE TITRE". Yalnız BÜYÜK harf (küçük "i) ii)" numaralandırma listesidir).
_ROMAN_HEADING_RE = re.compile(
    r"^(?P<num>" + ROMAN_NUMERAL_PATTERN + r")(?![\w\-])(?P<rest>.*)$", re.DOTALL,
)
# Etiket / Roma rakamı değer sınırı ("MIX" = 1009 gibi sözcükleri eler).
_MAX_LABEL_VALUE = 200

# Bölüm olmaması gereken kısa başlıklar: "# References" \section'a iner
# (_try_heading; R8c yetim hesabı da aynı kuralı bilir).
_NON_CHAPTER_NAMES = frozenset({
    "remarks", "remark", "note", "notes", "summary",
    "references", "bibliography", "appendix",
})

# 2026-09-25 (R8): kutu (yan metin) etiketi — "Exkurs 1.1", "Encadré 1.1 —
# Le décrochage", "Recuadro 1.1. …", "Врезка 1.1". Numara zorunludur
# ("Box" tek başına sıradan sözcük olabilir). Sözcükler patterns.py'de.
_BOX_LABEL_RE = re.compile(
    r"^" + BOX_LABEL_WORDS + r"\s+(?P<num>(?:\d+|[A-Z])(?:\.\d+)*)(?![\w])"
    r"(?P<rest>.*)$", re.DOTALL,
)
# KARAR 21 (2026-09-29): harfli başlık — "(a) The first phase", "a) X",
# "*a*) OBSERVACIÓN" (Marker harfi italik yazar), "2.1 a) X", "(iv) X".
# Harf/etiket kaynaktaki gibi metinde kalır, başlık numarasız + İçindekiler'de
# (bkz. MarkdownToLatex._try_heading). Tek harf ya da küçük Roma rakamı
# (ii-xviii); ardından boşluk ya da başlık sonu.
_LETTER_LABEL_HEADING_RE = re.compile(
    r"^(?:\d+(?:\.\d+)*\.?\s+)?"
    r"(?:\(\s*(?:[A-Za-z]|[ivx]{2,4})\s*\)"
    r"|(?:\*{1,2}|_)?(?:[A-Za-z]|[ivx]{2,4})(?:\*{1,2}|_)?\))"
    r"(?=\s|$)")
# F1-15 (2026-09-29): iki punto bu kadardan (pt) az farklıysa aynı sayılır
# (PDF'te aynı başlık stili 14,9 / 14,94 gibi yuvarlama farkı taşır; bir Almanca
# test kitabında ayrılması gereken başlıklar 14,9 ve 12,8 pt).
_HEADING_SIZE_TOL = 0.5

# KARAR 20/21: numarasız başlığın İçindekiler girdisi, başlık komutunun hemen
# ardında. Başlık metni belgede BİR kez yazılır (iki kez çevrilmesin):
# girdi metni, başlığın hyperref/nameref'in (şablonda yüklü) tuttuğu adıdır
# (\@currentlabelname; "@" harfi için \csname biçimi). Etiket ya da ikinci
# bir metin kopyası gerekmez; .toc'a ilk geçişte yazılır.
# Örnek: \section*{Kaynakça}\addcontentsline{toc}{section}{\csname @currentlabelname\endcsname}
_TOC_ENTRY_TEMPLATE = (
    "\\addcontentsline{{toc}}{{{level}}}{{\\csname @currentlabelname\\endcsname}}")
# KARAR 20: numarasız kaynakçanın İçindekiler girdisi en derin bu düzeyde
# yazılır (3 = subsection; book sınıfının öntanımlı tocdepth'i 2 = subsection,
# daha derin girdi İçindekiler'de görünmez). Şablon tocdepth'i değişirse
# birlikte gözden geçirilmeli.
_BIB_TOC_MAX_LEVEL = 3

# Birleştirilen başlık parçaları arasındaki ayraç (tek kaynak): iki satırlı
# kutuda etiket + kutu başlığı, EPUB'da başlık + alt ad (R8).
_TITLE_JOIN_SEP = " — "

# 2026-09-25 (R8, E1 sözleşmesi): EPUB'da bölüm başlığının alt adı
# (p.subh2) epub_parser tarafından başlığın HEMEN ardından bu işaretle
# başlayan tek satırlık paragraf olarak verilir. Converter onu bölüm
# başlığına katar: yalın etiket ("CAPÍTULO VII", "II.") yerine alt ad bölüm
# adı olur, aksi halde "başlık — alt ad". Başlığa bağlanamayan işaretli satır
# işaretsiz düz paragraf olarak basılır. İşaretin tek kaynağı
# patterns.ALTAD_MARKER (buradan da içe aktarılabilir).
# Bundan uzun alt ad başlığa katılmaz (düz paragraf kalır): başlık/İçindekiler
# satırı olarak okunaksızlaşır. 2026-09-28: 200 -> 120; 120-200 karakterlik
# alt ad üst bilgi satırını taşırıyordu (bir İspanyolca EPUB'da, 657 pt overfull).
_ALTAD_MAX_CHARS = 120
# 2026-09-28 (R8b): konu özeti biçimindeki alt ad ("Consideraciones sobre los
# métodos generales. — Infecundidad de las reglas abstractas. — ...") bir ad
# değil, konu listesidir: başlığa katılmaz, düz paragraf kalır (özgünde de
# başlığın altında basılıdır). Yalın etiketin ("CAPÍTULO PRIMERO") adı olarak
# yalnız İLK konu alınır; etiket "Bölüm 1" önekiyle yinelenmez. Ayraç
# NOKTA + tire'dir (". — "): "La ciencia – una introducción" gibi tireli
# alt başlıklı ad özet sayılmaz, bütünüyle ad olur.
_ALTAD_SEGMENT_SEP_RE = re.compile(r"(?<=\.)\s+[—–]\s+")

# ---------------------------------------------------------------------------
# 2026-10-05 (G1a-1): Marker yapı hataları — satır düzeyinde onarım
# (bkz. MarkdownToLatex._repair_marker_structure)
# ---------------------------------------------------------------------------
# Başlığa dönmüş madde imli satır (2026-10-05). Marker madde imini vurgu
# işaretinin içine ya da dışına yazar: "#### - **Note:**",
# "### **- Summary:**", "### - *first sample item:*". İmden sonra boşluk
# şarttır ("## -Minus sign rules" gerçek başlıktır).
_BULLET_HEADING_RE = re.compile(
    r"^#{1,6}\s+(?P<open>\*{1,3}|_{1,2})?\s*[-•●▪◦]\s+(?P<rest>\S.*?)\s*$")
# Tamamı kalın, iki nokta üst üsteyle biten kısa etiket başlığı
# ("#### **Result:**", "#### **Sample output lines:**"): ardındaki içeriği
# tanıtan kalın etikettir, başlık değil. Yalnız "###" ve daha derin düzeyde
# (bölüm/kısım başlığı değil), rakamsız (numaralı soru/örnek başlığı ayrı
# kuraldır).
_BOLD_LABEL_HEADING_RE = re.compile(
    r"^#{3,6}\s+(?P<label>\*\*(?P<text>[^*\n]+?)(?::\*\*|\*\*:))\s*$")
_LABEL_HEADING_MAX_WORDS = 6   # "Sorting unlabeled sample records:" = 4
# Başlık satırının sonuna yapışmış italik soru ("# X *What was X?*").
# Kaynakta soru başlığın altındaki ayrı satırdır. Soru olmayan italik kuyruk
# ("Methods *(revised)*") ya da tamamı italik başlık bu kurala girmez.
# 2026-10-05: Marker'ın başlık yaptığı iki satır türü başlık değildir (bkz.
# _demote_non_headings): tek boşluksuz makine belirteci (girintili kod satırı
# "#### qqSAYAC\_YENI/BASLA\_T1700000000\_U12345") ve tamamı vurgulu soru
# ("#### *Bu yöntem sonucu etkiledi mi?*"; aynı biçimdeki öteki sorular
# gövde satırı).
_TOKEN_HEADING_RE = re.compile(r"^#{1,6}\s+(?P<text>\S+)\s*$")
_TOKEN_HEADING_MIN_CHARS = 12
# Yalnız italik ("*…*") ya da kalın italik ("***…***") soru: Marker gerçek
# başlığı sık sık "**…**" kalın verir ("### **Gelecek nereye gidiyor?**" gibi
# bölüm başlığı; korpus denetimi 5 Eki).
_QUESTION_HEADING_RE = re.compile(
    r"^#{1,6}\s+(?P<text>(?P<m>\*\*\*|\*(?!\*))(?=[^\s*])[^*\n]*\?(?P=m))\s*$")
_HEADING_TRAILING_QUESTION_RE = re.compile(
    r"^(?P<head>#{1,6}\s+(?P<title>.*?[^\s*]))\s+"
    r"(?P<q>(?P<m>\*{1,3})(?=[^\s*])[^*\n]*?\?(?P=m))\s*$")
# PDF yer imine (outline) yapışık başlığı gövde satırının başından
# ayırmak için yer imi başlığı en az bu kadar sözcük içermeli. Kısa yer imi
# ("Results", "Short tag") sıradan bir cümlenin ilk sözcükleri olabilir
# ("Results of the survey show ..."); ölçülen bir raporda yapışan üç
# başlık 8-10 sözcük.
_GLUED_HEADING_MIN_WORDS = 4
# Şekil altyazısı satırına boş satırsız yapışan satır en az bu kadar
# karakterse gövde paragrafıdır, ayrı paragraf olur. Marker her paragrafı tek
# satır yazar; altyazının kısa devam satırı (kaynak notu "Source: …", panel
# açıklaması) altyazıda kalır. Korpusta yapışan gövde satırları 148–430
# karakter, kısa devam satırları < 40 karakter.
_CAPTION_TAIL_MIN_CHARS = 80
# mt okuma MT-23: ek başlığının çözümlenmesi — ek sözcüğü, isteğe
# bağlı harf/numara etiketi (Latin ya da Kiril tek harf, 1-2 haneli sayı) ve
# ayraçtan sonraki ad: "Appendix: X", "Appendix A - X", "APPENDIX D: X",
# "Anhang 1: X", "Appendix Sample Tables" (etiketsiz ad).
_APPENDIX_TITLE_RE = re.compile(
    r"^(?P<word>" + APPENDIX_LABEL_WORDS + r")(?![\w\-])"
    r"(?:\s+(?P<label>[A-ZА-ЯЁ]|\d{1,2})(?![\w\-]))?"
    r"(?P<rest>.*)$", re.DOTALL)


def parse_box_label(title: str) -> Optional[tuple]:
    """Kutu etiketi -> (etiket, kutu_başlığı); değilse None.

    "Exkurs 1.1" -> ("Exkurs 1.1", ""); "Encadré 1.1 — Le décrochage" ->
    ("Encadré 1.1", "Le décrochage"); "Recuadro 1.1. La paradoja" ->
    ("Recuadro 1.1", "La paradoja"). "Box office", "Boxen" -> None.
    """
    text = title.strip()
    m = _BOX_LABEL_RE.match(text)
    if not m:
        return None
    label = text[:m.end("num")]
    rest = m.group("rest")
    if not rest.strip(" .:–—-"):
        return label, ""
    sep = re.match(r"^\s*[.:–—\-]+\s*(?P<t>\S.*)$", rest, re.DOTALL)
    if sep:
        return label, sep.group("t").strip()
    plain = re.match(r"^\s+(?P<t>\S.*)$", rest, re.DOTALL)
    if plain:
        return label, plain.group("t").strip()
    return None


# 2026-09-28 (R18): basılı içindekiler devam sayfasının üst bilgisi: sayfa
# numarası (Roma ya da Arap) + ayraç ya da boşluk + içindekiler adı, ya da
# tersi ("viii ◾ Contents", "Contents | ix"). _norm_name biçiminde (küçük
# harf, aksansız) eşlenir; ad CONTENTS_HEADING_NAMES'ten gelir. Roma
# rakamı geçerli ve _MAX_LABEL_VALUE'dan küçük olmalı ("mix" = 1009 elenir).
_TOC_HEAD_SEP = r"(?:\s*[◾■▪|•·—–-]\s*|\s+)"
_TOC_HEAD_PREFIX_RE = re.compile(
    r"^(?P<page>[ivxlcdm]+|\d+)" + _TOC_HEAD_SEP + r"(?P<rest>\S.*)$")
_TOC_HEAD_SUFFIX_RE = re.compile(
    r"^(?P<rest>\S.*?)" + _TOC_HEAD_SEP + r"(?P<page>[ivxlcdm]+|\d+)$")


def _is_page_token(token: str) -> bool:
    if token.isdigit():
        return True
    return 1 <= roman_to_int(token) <= _MAX_LABEL_VALUE


def _is_contents_heading(title: str) -> bool:
    """Başlık bir içindekiler başlığı ya da onun sayfa üst bilgisi mi?

    "Inhaltsverzeichnis", "Оглавление", "viii ◾ Contents" -> True;
    "Summary of Contents", "Contents of the Box" -> False.
    """
    name = _norm_name(title)
    if name in CONTENTS_HEADING_NAMES:
        return True
    for rx in (_TOC_HEAD_PREFIX_RE, _TOC_HEAD_SUFFIX_RE):
        m = rx.match(name)
        if (m and _is_page_token(m.group("page"))
                and _norm_name(m.group("rest")) in CONTENTS_HEADING_NAMES):
            return True
    return False


def _clean_altad(text: str) -> str:
    """Alt ad metni: tek sondaki nokta atılır ("Marcha de la investigación
    científica." -> "... científica"); üç nokta / soru / ünlem kalır."""
    text = " ".join(text.split())
    if text.endswith(".") and not text.endswith(".."):
        text = text[:-1].rstrip()
    return text


class _Label(NamedTuple):
    """Çözümlenmiş bölüm/kısım etiketi."""
    kind: str      # "chapter" | "part"
    value: int
    title: str     # etiketten sonraki başlık metni ("" = yalın etiket)
    digit: bool    # numara Arap rakamıyla mı yazılmış


def parse_heading_label(title: str) -> Optional[_Label]:
    """Başlık metni bir bölüm/kısım etiketiyle mi başlıyor? Değilse None.

    "Kapitel 1 Grundlagen – X" -> ("chapter", 1, "Grundlagen – X", True)
    "CAPÍTULO VII"         -> ("chapter", 7, "", False)
    "Zweites Kapitel"      -> ("chapter", 2, "", False)
    "Part I First Principles" -> ("part", 1, "First Principles", False)
    "Kapitel und Vers", "Глава государства", "Chapter 1.2 X" -> None
    """
    text = title.strip()
    if not text:
        return None
    m = _LABEL_FORWARD_RE.match(text)
    reverse = False
    if not m:
        m = _LABEL_REVERSE_RE.match(text)
        reverse = True
    if not m:
        return None
    word = m.group("word")
    num = m.group("num")
    rest = m.group("rest")
    if reverse:
        folded_word = fold_accents(word).lower()
        if num.endswith("."):
            if folded_word not in _REVERSE_DIGIT_WORDS:
                return None
            num = num[:-1]
        elif (folded_word in _ENGLISH_LABEL_WORDS
              or num.lower() in ENGLISH_NUMBER_WORDS):
            return None
    if rest and re.match(r"^\s*[.,]\d", rest):
        return None  # "Chapter 1.2 X": bölüm değil, alt bölüm göndermesi
    rest_m = _LABEL_REST_RE.match(rest) if rest else None
    if rest and not rest_m:
        return None  # "Chapter 1)" gibi ayraçsız devam
    rest_title = rest_m.group("title") if rest_m else ""
    if num.isdigit():
        value, digit = int(num), True
    else:
        value = number_word_value(num) or roman_to_int(num)
        digit = False
        if not 1 <= value <= _MAX_LABEL_VALUE:
            return None
    kind = "part" if _PART_WORD_RE.match(word) else "chapter"
    return _Label(kind, value, rest_title, digit)


def parse_roman_heading(title: str) -> Optional[tuple]:
    """Roma rakamıyla başlayan başlık -> (değer, başlık, güçlü_mü); değilse None.

    Güçlü: yalın rakam ("II", "II.") ya da ayraçlı başlık ("III — X",
    "IV. X"). Zayıf: yalnız boşlukla ayrılmış başlık ("I LE GUN-CLUB");
    zayıf biçim ancak en az iki üyeli ardışık dizide kabul edilir
    ("I Am Legend" tek başına bölüm sayılmaz).
    """
    text = title.strip()
    m = _ROMAN_HEADING_RE.match(text)
    if not m or not m.group("num").isupper():
        return None
    value = roman_to_int(m.group("num"))
    if not 1 <= value <= _MAX_LABEL_VALUE:
        return None
    rest = m.group("rest")
    if not rest.strip(" .:–—-"):
        return value, "", True
    sep = re.match(r"^\s*[.:–—\-]+\s*(?P<t>\S.*)$", rest, re.DOTALL)
    if sep:
        return value, sep.group("t").strip(), True
    weak = re.match(r"^\s+(?P<t>\S.*)$", rest, re.DOTALL)
    if weak and weak.group("t")[0].isupper():
        return value, weak.group("t").strip(), False
    return None


def _norm_key(text: str) -> str:
    """Karşılaştırma anahtarı: aksansız, küçük harf, yalnız harf/rakam."""
    return "".join(c for c in fold_accents(text).casefold() if c.isalnum())


def _norm_name(text: str) -> str:
    """Ön/arka madde adı karşılaştırması: aksansız, küçük harf, tek boşluk."""
    folded = fold_accents(text).casefold()
    return re.sub(r"\s+", " ", folded).strip(" .:;")


class _Heading:
    """Yapı çözümlemesindeki bir markdown başlık satırı."""
    __slots__ = ("idx", "level", "title", "label", "roman", "box")

    def __init__(self, idx: int, level: int, title: str):
        self.idx = idx
        self.level = level
        self.title = title
        self.label: Optional[_Label] = parse_heading_label(title)
        self.roman = None if self.label else parse_roman_heading(title)
        self.box = (None if self.label or (self.roman and self.roman[2])
                    else parse_box_label(title))


class _HeadingDecision(NamedTuple):
    """Yapı planının bir başlık satırı için kararı (bkz. _plan_structure)."""
    drop: bool = False           # satır başlık olarak basılmaz
    level: int = 0               # 0=\part, 1=\chapter, 2=\section ...
    title: Optional[str] = None  # görünen başlık (None: satırdaki metin)
    anchor: bool = False         # bölüm/kısım çapası (zorlanmış düzey)
    unnumbered: bool = False     # yıldızlı basılır (kutu başlığı, R8)
    # 2026-10-05 (G1a-1, MT-23): numarasız başlık İçindekiler'e girer
    # (KARAR 20/21 biçimi; ek başlıkları).
    toc: bool = False

# F32 (2026-07-29): Markdown kacisli TeX ozel karakterleri. Escape adimindan
# ONCE geri alinir, yoksa '\X' -> '\' + '\X' = '\\X' (cift-escape) olur:
# LaTeX'te '\\' zorunlu satir kirimi demektir. '$' listeye simdi eklendi —
# eskiden '\$100' ciktida '\\$100' oluyordu (satir kirimi + math modu acilisi,
# sonraki '$'a kadar metin formule yutuluyordu; sanitizer yalnizca WARN basar).
# Liste TEK yerde tutulur: uc cagri yeri (_protect_and_escape,
# _escape_tex_keep_tokens, _convert_inline Step 2.5) ayni sozlesmeyi paylasir,
# boylece 2026-07-11 (#5) ve F32'deki gibi listeler arasi kayma olusmaz.
# NOT: gercek matematik bu noktada zaten stash'lenmis olmalidir (\x00MATH_n\x00
# veya \x00PROT_n\x00), yani buradaki '$' her zaman duz metin dolaridir.
_MD_ESCAPED_SPECIALS = ("_", "#", "&", "%", "$")


def _unescape_markdown_specials(text: str) -> str:
    """Markdown kacisli TeX ozel karakterlerini sadelestirir ('\\$' -> '$')."""
    for ch in _MD_ESCAPED_SPECIALS:
        text = text.replace("\\" + ch, ch)
    return text


# F1-04 / F2-04 (2026-09-29): Markdown kaçışlı yıldız ve alt çizgi. Marker düz
# yıldızı "\*" (çarpma "0.5\*0.5", "C\*", "ABC\*'21", "*A\**"), düz alt çizgiyi
# "\_" yazar. İkisi de biçim kalıplarından (kalın/italik) ÖNCE düz karaktere
# çözülür: eskiden iki "\*" italik sanılıp "0.5\\textit{0.5 … 0.2\}" dengesiz
# grubu derlemeyi "File ended while scanning use of \textit" ile durduruyordu;
# tek "\*" ise LaTeX'te kesme noktası komutudur (\discretionary): yıldız
# basılmıyor, satır sonunda U+0002 eksik glifi çıkıyordu. Yıldız metin
# kipinde "*" olarak basılır. "\\*" (kaçışlı ters bölü + yıldız) dokunulmaz.
_MD_ESCAPED_STAR_RE = re.compile(r"(?<!\\)\\\*")
_MD_ESCAPED_UNDERSCORE_RE = re.compile(r"(?<!\\)\\_")

# F1-05 (2026-09-29): matematikte "\\" ardından (boşluk +) "[": LaTeX köşeli
# ayracı satır aralığı argümanı okur ("\\[2pt]"). Marker'ın "\\ [\text{AU}]"
# (bir tablo başlığı), "\\ [0, 1, 0]^{\top}" (cases ortamı) satırında
# içerik uzunluk değildir -> "Illegal unit of measure". Uzunluk olmayan köşeli
# ayraçtan önce "{}" konur ("\\{} ["); gerçek uzunluk ("\\[2pt]", "\\[-1ex]",
# "\\[0.5\baselineskip]", "\\[\jot]") dokunulmaz.
_MATH_BREAK_BRACKET_RE = re.compile(
    r"(?<!\\)(\\\\\*?)(\s*)\[(?!\s*(?:-?(?:\d+(?:\.\d*)?|\.\d+)\s*"
    r"(?:pt|em|ex|mm|cm|in|bp|pc|dd|cc|sp|mu)"
    r"|-?(?:\d+(?:\.\d*)?|\.\d+)?\s*\\[A-Za-z]+)\s*\])")


def _fix_math_linebreak_bracket(math: str) -> str:
    """Matematik parçasında uzunluk olmayan '\\\\ [' -> '\\\\{} [' (F1-05)."""
    if "\\\\" not in math:
        return math
    return _MATH_BREAK_BRACKET_RE.sub(r"\1{}\2[", math)


# F1-05 (2026-09-29): tablo hücresindeki satır içi matematikte ortam dışı
# "\\": Marker iki satırlık hücreyi "$2.71828182 \\ (0.00001234)$" yazar;
# tabular içinde "\\" satırı bitirir, matematik açık kalır ("Missing $
# inserted"). Hücre matematiği tek sütunlu array'e alınır: iki satır
# kaynaktaki gibi alt alta kalır, glifler değişmez.
_ENV_SPAN_RE = re.compile(r"\\begin\{([^}]*)\}.*?\\end\{\1\}", re.DOTALL)


def _wrap_cell_math_linebreaks(cell: str) -> str:
    """Tablo hücresinde ortam dışı '\\\\' içeren satır içi matematiği
    ``\\begin{array}{@{}l@{}} … \\end{array}`` içine al (F1-05)."""
    if "\\\\" not in cell or "$" not in cell:
        return cell

    def _wrap(m: "re.Match[str]") -> str:
        inner = m.group(1)
        if "\\\\" not in _ENV_SPAN_RE.sub("", inner):
            return m.group(0)
        return ("$\\begin{array}{@{}l@{}}" + inner.strip()
                + "\\end{array}$")

    return _INLINE_MATH_RE.sub(_wrap, cell)


# F1-05 (2026-09-29): Marker kaçışlı bağ metni. Metin en az bir Marker
# kaçışı içerir ("\(", "\)", "\[", "\]", "\\_" ya da "\\*"); adres dengeli tek düzey
# parantez içerebilir ("…/Sample_Topic_(physics)"). Görsel ("![…](…)")
# hariç. Bkz. MarkdownToLatex._stash_marker_escaped_links.
# Metinde çıplak "[" de aynı yoldan geçer: Marker köşeli ayraçlı atıfı
# "[[1,2](#page-12-0)]" (ayraç bağın dışında) yazar; eski ön işlem (7. adım)
# "[[4\]](#page-…)", "[105\]](#page-…)", "[\[1](#page-…)" biçimlerini
# "[4](#page-…)"ya indirip atıf ayraçlarını SİLİYORDU ("[101, 105]" ->
# "[101, 105", "[1,2]" -> "1,2]"). Artık ayraçlar bağın dışında korunur.
_MARKER_ESCAPED_LINK_RE = re.compile(
    r"(?<!!)\[(?P<text>[^\]\n]*?(?:\\[()\[\]]|\\\\[_*]|\[)[^\]\n]*)\]"
    r"\((?P<url>(?:[^()\s]|\([^()\s]*\))+|[^)\s]+)\)")
_MARKER_LINK_ESCAPE_RE = re.compile(r"(?<!\\)\\([()\[\]])")
# Marker bağ metninde çift kaçış: "Sample\\_Topic", "NH2OH\\*NO3".
_MARKER_LINK_DOUBLE_ESCAPE_RE = re.compile(r"\\\\([_*])")


def _unescape_marker_link_text(text: str) -> str:
    """Marker bağ metni kaçışları (F1-05): "\\(" "\\)" "\\[" "\\]" düz
    karakter olur; çift kaçışlı "\\\\_" / "\\\\*" tek kaçışa ("\\_", "\\*")
    iner (sonra satır içi dönüşüm düz alt çizgi / yıldız yazar)."""
    text = _MARKER_LINK_DOUBLE_ESCAPE_RE.sub(r"\\\1", text)
    return _MARKER_LINK_ESCAPE_RE.sub(r"\1", text)


_CODE_SPAN_RE = re.compile(r"`[^`]+`")
_LINK_EDGE_PAIRS = {"(": ")", "[": "]"}


def _split_link_edge_brackets(label: str) -> Tuple[str, str, str]:
    """Bağ metninin ucundaki ayraçları dışarı al (F1-05).

    Eşleşmemiş uç ayraç: "(2009" -> ("(", "2009", ""); "2009)" -> ("",
    "2009", ")"); "[ 63" -> ("[", "63", ""). Metni bütünüyle saran köşeli
    ayraç da dışarı alınır: "[4]" -> ("[", "4", "]") — "\\href{…}{[4]}"
    2_derle sanitizer'ının bozuk bağ onarımında ayraç grubunu bozuyordu.
    Dengeli parantez içeride kalır: "(6.3)" -> ("", "(6.3)", ""). Metin
    yalnız ayraçtan ibaretse dokunulmaz (bağ boş kalmasın).
    """
    pre, body, post = "", label, ""
    changed = True
    while changed and body:
        changed = False
        first = body[0]
        if (first in _LINK_EDGE_PAIRS
                and body.count(first) > body.count(_LINK_EDGE_PAIRS[first])):
            pre += first
            body = body[1:].lstrip()
            changed = True
            continue
        last = body[-1]
        opener = {")": "(", "]": "["}.get(last)
        if opener and body.count(last) > body.count(opener):
            post = last + post
            body = body[:-1].rstrip()
            changed = True
            continue
        if (len(body) > 2 and first == "[" and last == "]"
                and "[" not in body[1:-1] and "]" not in body[1:-1]):
            pre += "["
            post = "]" + post
            body = body[1:-1].strip()
            changed = True
    if not body.strip():
        return "", label, ""
    return pre, body, post


def _literal_asterisks(text: str) -> str:
    """Kaçışlı yıldız -> '*' (F1-04/F2-04, _protect_and_escape yolu)."""
    return _MD_ESCAPED_STAR_RE.sub("*", text)


# F1-09 (V1 denetimi, 2026-09-29): EPUB düz metin kaçışlarının kalanı.
# parsers.epub_parser.md_escape düz metindeki ters bölüyü "\\", ters tırnağı
# "\`", "(" önündeki kapanış köşeli ayracını "\]" yazar (sözleşme orada).
# Dönüştürücü "\*" / "\_" / "\$" kaçışlarını çözüyordu, bu üçünü çözmüyordu:
# "C:\\Users" TeX satır sonu olup ters bölüler kayboluyordu ("C:" / "Users");
# "[see here\](not a link)" "\href{not a link}{see here\}" olup derlemeyi
# "Paragraph ended before \href@split" ile durduruyor, paragrafın kalanı
# PDF'ten düşüyordu; "\`x\`" kod parçası sanılıp "\texttt{x\}" oluyordu.
# Kod parçası (`...`) içine dokunulmaz (CommonMark'ta orada kaçış yoktur).
# Marker'ın "\\_" / "\\*" çift kaçışı (bağ metni dışında da görülür) EPUB
# yolunda hiç üretilmez; eski anlamıyla kalır (ters bölü kuralına girmez).
# 2026-10-05: Marker ters bölüyü kaçışlamaz: düz metindeki tek "\" + harf
# ("\d{2,6}" regex, "C:\Users" yolu) PDF'teki ters bölüdür, TeX komutu değil
# (Marker matematiği $…$ içinde verir; matematik önceden stash'lenir). Eskiden
# komut olarak geçiyordu: "\d" alt nokta aksanı ("\d+" -> "+̣"), "\Users"
# tanımsız komut hatası. EPUB yolu ters bölüyü "\\" yazar (bs grubu). İki
# istisna (bkz. _literal_escape_tex): matematik dışına yazılmış bilinen
# matematik simgesi ve sözcüğe yapışık "\n" satır sonu artığı.
_MD_LITERAL_ESCAPE_RE = re.compile(
    r"`[^`\n]+`"
    r"|(?<!\\)(?P<bs>(?:\\\\)+)(?![_*])"
    r"|(?<!\\)\\(?P<ch>`|\](?=\())"
    r"|(?<!\\)\\(?P<tek>[A-Za-z]+)")
# Marker'ın matematik dışına yazdığı simge komutu ("(2 g) \rightarrow (3 g)"):
# matematik kipinde basılır.
_METIN_ICI_MATEMATIK = frozenset({
    "rightarrow", "leftarrow", "Rightarrow", "Leftarrow", "leftrightarrow",
    "Leftrightarrow", "longrightarrow", "Longrightarrow", "to", "mapsto",
    "uparrow", "downarrow", "leq", "geq", "le", "ge", "neq", "ne", "approx",
    "equiv", "sim", "simeq", "cong", "propto", "ll", "gg", "times", "pm", "mp",
    "cdot", "div", "circ", "infty", "partial", "nabla", "ldots", "cdots",
    "alpha", "beta", "gamma", "delta", "epsilon", "varepsilon", "zeta", "eta",
    "theta", "vartheta", "iota", "kappa", "lambda", "mu", "nu", "xi", "pi",
    "rho", "sigma", "tau", "upsilon", "phi", "varphi", "chi", "psi", "omega",
    "Gamma", "Delta", "Theta", "Lambda", "Xi", "Pi", "Sigma", "Upsilon", "Phi",
    "Psi", "Omega"})


# V1 denetimi (2026-09-29): tablo hücresinde markdown biçimi (kaçışsız "*…*",
# "**…**", sözcük sınırında "_…_", "[metin](adres)"); bkz.
# MarkdownToLatex._table_cell_tex. Matematik önceden maskelenir.
_TABLE_CELL_MARKUP_RE = re.compile(
    r"(?<![\\*])\*{1,3}[^*\s][^*]*?(?<![\\\s])\*{1,3}(?!\*)"
    r"|(?<![\w\\])_{1,3}[^_\s][^_]*?(?<![\\\s])_{1,3}(?!\w)"
    r"|(?<!!)\[[^\]\n]+\]\([^)\s]+\)"
    # 2026-10-05 (G1a-2): <u>…</u> ve kod parçası da satır içi dönüşüme
    # gider (eskiden hücrede düz basılıyordu: "<u>97776</u>", "`x`").
    r"|<u[\s>]|`[^`\n]+`")


# 2026-10-05 (G1a-2): satır içi HTML etiketleri. Etiket ancak
# HTML sözdizimine uyuyorsa etikettir: ad + yalnız "ad=değer" öznitelikleri.
# fd74ff2'nin "(?=[\s/>])" şartı adın ardından BOŞLUĞA izin veriyordu: "<I
# think it is fine …>" ve "<I will tell the team …>" düzyazısı i etiketi
# sanılıp siliniyordu. "<a command that …>",
# "<b and c>" gibi açılı ayraçlı düzyazı da artık korunur.
_HTML_ATTRS = (r"""(?:\s+[A-Za-z_:][-A-Za-z0-9_:.]*\s*=\s*"""
               r"""(?:"[^"]*"|'[^']*'|[^\s"'=<>`]+))*\s*""")
_HTML_STRIP_TAG_RE = re.compile(
    r"</?(?:span|div|p|pre|br|em|strong|b|i|a|ul|ol|li)" + _HTML_ATTRS + r"/?>",
    re.IGNORECASE)
# <b>/<strong> -> **…**, <i>/<em> -> *…*. İçinde etiket, satır sonu ya da
# tablo ayracı olan çift çevrilmez; etiketi yalnız silinir (eski davranış).
_HTML_EMPH_RE = re.compile(
    r"<(b|strong|i|em)" + _HTML_ATTRS + r">([^<>\n|]*?)</\1\s*>", re.IGNORECASE)
_HTML_ANCHOR_RE = re.compile(
    r"<a\s(?:[^<>]*?\s)?href\s*=\s*(?:\"([^\"]*)\"|'([^']*)')[^<>]*>"
    r"([^<>\n]*?)</a\s*>", re.IGNORECASE)
# Tablo hücresindeki liste (<ul><li>12</li><li>13</li></ul>): maddeler
# boşlukla ayrılır (hücredeki <br> gibi).
_HTML_LIST_ITEM_RE = re.compile(r"<li" + _HTML_ATTRS + r">", re.IGNORECASE)
# <u>…</u>: Marker kaynakta altı çizili metni böyle verir (bir raporda mavi
# alt çizgili iç bağlantılar, bir kitapta gerçek alt çizgi). _convert_inline'da
# kaynak PDF'te bağlantıysa \href, değilse \uline (ulem, şablonda yüklü;
# \underline satır kıramaz) olur.
_HTML_UNDERLINE_RE = re.compile(r"<u" + _HTML_ATTRS + r">(.*?)</u\s*>",
                                re.IGNORECASE)
_HTML_UNDERLINE_TAG_RE = re.compile(r"</?u" + _HTML_ATTRS + r">", re.IGNORECASE)
# <u> metninin PDF sayfasını bulmak için Markdown'daki çevresi (_norm_key
# karakteri): önce iki yanlı uzun, sonra kısa pencere; tek yanlı bağlam en az
# _ULINK_CONTEXT_MIN karakter (daha kısası sayfayı ayırt etmez).
_ULINK_CONTEXT_WINDOWS = (24, 12)
_ULINK_CONTEXT_MIN = 8
# Bağlantı dikdörtgeni metni okunurken üstten ve alttan bu oranda daraltılır
# (dikdörtgen komşu satıra taşabiliyor; ölçüldü).
_LINK_RECT_INSET = 0.25
_SUPSUB_TAG_RE = re.compile(r"</?su[bp]>", re.IGNORECASE)


def _html_anchor_repl(m: "re.Match[str]") -> str:
    """<a href="adres">metin</a> -> [metin](adres); olmazsa yalnız metin."""
    url = html.unescape(m.group(1) if m.group(1) is not None else m.group(2)).strip()
    label = m.group(3)
    if (label.strip() and url and not re.search(r"[\s()]", url)
            and not re.search(r"[\[\]]", label)):
        return f"[{label}]({url})"
    return label


def _html_emph_repl(m: "re.Match[str]") -> str:
    """<b>/<strong>/<i>/<em> çifti -> Markdown vurgusu (boşluklar dışarıda)."""
    inner = m.group(2)
    core = inner.strip()
    s = m.string
    if (not core or "*" in core or s[m.start() - 1:m.start()] == "*"
            or s[m.end():m.end() + 1] == "*"):
        return inner
    mark = "**" if m.group(1).lower() in ("b", "strong") else "*"
    lead = inner[:len(inner) - len(inner.lstrip())]
    trail = inner[len(inner.rstrip()):]
    return f"{lead}{mark}{core}{mark}{trail}"


# 2026-10-05 (G1a-2): Marker kaynakta tek olan bağlantıyı satır
# sonunda ya da sözcük sözcük böler: "[shared](#page-12-0)
# [tools](#page-12-0)", "[http://www.x.org/](U)  [abc/def.](U)". Aynı
# hedefe giden, arada yalnız boşluk olan ardışık bağlantılar birleşir.
_MD_LINK_RE = re.compile(
    r"(?<!!)\[((?:\\.|[^\[\]\\\n])+)\]\(((?:[^()\s]|\([^()\s]*\))+)\)")
# Atıf numarası ("\[101,", "105\]", "12"): iki numara ayrı atıftır,
# birleşmez (aynı kaynakça sayfasına gitse de).
_CITATION_LABEL_RE = re.compile(r"^[\s\[\]()\\,;.–-]*\d+[a-z]?[\s\[\]()\\,;.–-]*$")
# Adres metni gibi görünen bağlantı metni (satır sonunda bölünmüş adres).
_URLISH_LABEL_RE = re.compile(r"[/.:]|^www", re.IGNORECASE)
_MD_ESCAPE_RE = re.compile(r"\\([!-/:-@\[-`{-~])")
_MD_DOUBLE_ESCAPE_RE = re.compile(r"\\\\([_*])")
# Dar bağlantı genişletmesi (bkz. MarkdownToLatex._widen_narrow_links).
_MD_LINK_URL_RE = re.compile(r"\]\((?:[^()\s]|\([^()\s]*\))*\)")
# Bağlantıya taşınamayan sözcük: vurgu işareti, bağlantı/etiket, matematik,
# kod, yer tutucu ya da nöbetçi taşıyan.
_LINK_WORD_BLOCK_RE = re.compile(r"[*\[\]<>$`\x00\x02\\]|(?<!\w)_|_(?!\w)")
_LINK_TRAIL_PUNCT = ".,;:!?)]\"'”’»"
_LINK_LEAD_PUNCT = "([\"'“‘«"


def _same_link_target(pdf_target: str, md_url: str) -> bool:
    """PDF bağlantı hedefi Markdown adresiyle aynı mı (iç bağlantıda sayfa)?"""
    a, b = re.match(r"#page-(\d+)", pdf_target), re.match(r"#page-(\d+)", md_url)
    if a or b:
        return bool(a and b and a.group(1) == b.group(1))
    return pdf_target.rstrip("/") == md_url.rstrip("/")


def _take_link_words(segment: str, target: str, from_end: bool) -> Optional[int]:
    """segment'in ucundaki, _norm_key birleşimi tam olarak target olan
    sözcükler: taşınacak karakter sayısı (soldan: sondan sayılır; sağdan:
    baştan). target boşsa 0, eşleşme yoksa None. Uçtaki noktalama dışarıda
    kalır."""
    if not target:
        return 0
    words = list(re.finditer(r"\S+", segment))
    acc = ""
    for w in (reversed(words) if from_end else words):
        token = w.group(0)
        if _LINK_WORD_BLOCK_RE.search(token):
            return None
        nk = _norm_key(token)
        acc = nk + acc if from_end else acc + nk
        if acc == target:
            if from_end:
                return len(segment) - (w.start() + len(token) - len(token.lstrip(_LINK_LEAD_PUNCT)))
            return w.end() - (len(token) - len(token.rstrip(_LINK_TRAIL_PUNCT)))
        if not (target.endswith(acc) if from_end else target.startswith(acc)):
            return None
    return None


def _md_unescape(text: str) -> str:
    """Markdown kaçışlarını düz karaktere çevir (Marker'ın "\\\\_" çift
    kaçışı dahil); kod parçası içeriği ve karşılaştırma için."""
    return _MD_ESCAPE_RE.sub(r"\1", _MD_DOUBLE_ESCAPE_RE.sub(r"\1", text))


def _split_link_joiner(a: str, b: str, url: str, sep: str) -> str:
    """Birleşen iki bağlantı metninin arasına gelecek dizgi.

    Adres metni satır sonunda bölünmüşse parçalar adresle doğrulanarak
    boşluksuz ("www.example." + "com") ya da tireyle ("rule-of" +
    "thumb…": Marker satır sonu tiresini düşürmüş) birleşir; rakamla bölünmüş
    sayı da ("978-0-000-1" + "2345-6", ISBN) adresle doğrulanınca boşluksuz.
    Öteki metinler tek boşlukla: adresi sözcüklerden kurulu bağlantıda
    ("sample-words-of-…") düzyazı sözcükleri tireyle birleşmez. Arada hiç
    boşluk yoksa görünen metin olduğu gibi kalır.
    """
    va, vb = _md_unescape(a), _md_unescape(b)
    # Yüzde kodu çözülmüş, büyük/küçük harf duyarsız adres: bağlantı metni
    # "…pdf. last accessed 1/2/2020", adres "…pdf.%20Last%20accessed%20…".
    u = urllib.parse.unquote(html.unescape(url)).casefold()

    def in_url(s: str) -> bool:
        # Cümlenin noktalaması/ayracı atılır; etiket de yüzde kodlu olabilir
        # ("Ada" + "%27s_lemma").
        s = urllib.parse.unquote(s.rstrip(".,;:)]")).casefold()
        return bool(s) and s in u

    urlish = not sep or bool(_URLISH_LABEL_RE.search(va))
    split_number = va[-1:].isdigit() and vb[:1].isdigit()
    if (urlish or split_number) and in_url(va + vb):
        return ""
    if urlish and in_url(va + "-" + vb):
        return "-"
    return " " if sep else ""


# 2026-10-05 (G1a-2, mt okuma MT-18'in dönüştürücü payı): CommonMark vurgu
# algoritması ('*' için). Düzenli ifade yolu ("***(.+?)***" ...) aynı
# satırda birden çok üçlü yıldızı ya da "***X**, … **Y.***" dizisini yanlış
# eşleyip "**"/"*" artığı bırakıyordu (iki test kitabında, birinde 6
# satır). Yalnız artık bırakılan satırda kullanılır; eşi
# olmayan işaret düz karakter kalır (Marker düz yıldızı her zaman
# kaçışlamaz: "Mean value*" dipnot simgesi).
class _EmDelim:
    __slots__ = ("count", "orig", "can_open", "can_close")

    def __init__(self, count: int, can_open: bool, can_close: bool):
        self.count = count
        self.orig = count
        self.can_open = can_open
        self.can_close = can_close


def _cm_char_class(ch: str) -> str:
    """CommonMark sınıfı: 'w' boşluk (metin başı/sonu dahil), 'p'
    noktalama/simge, 'a' öteki. Stash belirteci (\\x00…) 'a' sayılır."""
    if not ch or ch.isspace():
        return "w"
    if ch == "\x00":
        return "a"
    return "p" if unicodedata.category(ch)[0] in "PS" else "a"


def _cm_delim_runs(text: str):
    """(başlangıç, bitiş, açabilir, kapatabilir) — her '*' dizisi için.

    Marker lehçesi: CommonMark'ın noktalama koşulu uygulanmaz; ardı boşluk
    olmayan dizi açabilir, önü boşluk olmayan dizi kapatabilir. Marker
    kapanış işaretini noktalamadan sonra boşluksuz harfe bitiştirir
    ("“*tired”*so", "***First Rule*.**This"); katı kural bunları
    eşleyemeyip düz yıldız bırakıyordu. Boşluklar arasındaki yıldız ("a * b")
    ne açar ne kapatır.
    """
    for m in re.finditer(r"\*+", text):
        before = _cm_char_class(text[m.start() - 1] if m.start() else "")
        after = _cm_char_class(text[m.end()] if m.end() < len(text) else "")
        yield m.start(), m.end(), after != "w", before != "w"


def _cm_emphasis_tree(text: str) -> list:
    """Metni düğümlere ayır: str, _EmDelim ya da ("strong"/"em", çocuklar)."""
    items: list = []
    pos = 0
    for start, end, left, right in _cm_delim_runs(text):
        if start > pos:
            items.append(text[pos:start])
        items.append(_EmDelim(end - start, left, right))
        pos = end
    if pos < len(text):
        items.append(text[pos:])
    delims = [x for x in items if isinstance(x, _EmDelim)]
    k = 0
    while k < len(delims):
        closer = delims[k]
        if closer.count == 0 or not closer.can_close:
            k += 1
            continue
        found = None
        for j in range(k - 1, -1, -1):
            op = delims[j]
            if op.count == 0 or not op.can_open:
                continue
            # "Üç kuralı" (CommonMark 6.2, kural 9-10).
            if ((op.can_close or closer.can_open)
                    and (op.orig + closer.orig) % 3 == 0
                    and not (op.orig % 3 == 0 and closer.orig % 3 == 0)):
                continue
            found = j
            break
        if found is None:
            k += 1
            continue
        op = delims[found]
        use = 2 if op.count >= 2 and closer.count >= 2 else 1
        a = next(i for i, x in enumerate(items) if x is op)
        b = next(i for i, x in enumerate(items) if x is closer)
        items[a + 1:b] = [("strong" if use == 2 else "em", items[a + 1:b])]
        op.count -= use
        closer.count -= use
        del delims[found + 1:k]
        k = found + 1
    return items


def _cm_render_tex(nodes: list, stash) -> str:
    """Ağacı LaTeX'e çevir: vurgu düğümü stash'lenir (içerik kaçışlı, düzenli
    ifade yoluyla aynı sözleşme), üst düzey metin ham kalır (Adım 3 kaçışlar).
    em(strong(X)) düzenli ifade yolundaki gibi \\textbf{\\textit{X}} yazılır."""
    parts: List[str] = []
    for nd in nodes:
        if isinstance(nd, str):
            parts.append(nd)
        elif isinstance(nd, _EmDelim):
            parts.append("*" * nd.count)
        else:
            kind, children = nd
            if (kind == "em" and len(children) == 1 and isinstance(children[0], tuple)
                    and children[0][0] == "strong"):
                inner = _cm_render_tex(children[0][1], stash)
                parts.append(stash("\\textbf{\\textit{"
                                   + _escape_tex_keep_tokens(inner) + "}}"))
                continue
            inner = _cm_render_tex(children, stash)
            cmd = "\\textbf{" if kind == "strong" else "\\textit{"
            parts.append(stash(cmd + _escape_tex_keep_tokens(inner) + "}"))
    return "".join(parts)


def _cm_render_markdown(nodes: list) -> str:
    """Ağacı Markdown'a geri yaz; satır sonunu aşan vurgu her satırda ayrı
    kapanıp açılır ("*a\\nb*" -> "*a*\\n*b*"). Satırlara bölünmüş (EPUB/
    Gutenberg) paragrafta satır satır dönüşüm çifti görebilsin diye."""
    parts: List[str] = []
    for nd in nodes:
        if isinstance(nd, str):
            parts.append(nd)
        elif isinstance(nd, _EmDelim):
            parts.append("*" * nd.count)
        else:
            kind, children = nd
            mark = "**" if kind == "strong" else "*"
            pieces = []
            for piece in _cm_render_markdown(children).split("\n"):
                core = piece.strip()
                if not core:
                    pieces.append(piece)
                    continue
                lead = piece[:len(piece) - len(piece.lstrip())]
                trail = piece[len(piece.rstrip()):]
                pieces.append(f"{lead}{mark}{core}{mark}{trail}")
            parts.append("\n".join(pieces))
    return "".join(parts)


def _cm_has_open_delims(text: str) -> bool:
    """Düzenli ifade geçişinden sonra yanaşık (vurgu işareti olabilecek)
    '*' dizisi kaldı mı? Boşluklar arasındaki yıldız ("a * b") sayılmaz."""
    return any(left or right for _s, _e, left, right in _cm_delim_runs(text))


# 2026-10-05 (G1a-2): kaynak PDF'te eş aralıklı yazı tipiyle dizilmiş
# satır içi parçalar (özel adlar, makine dizgileri, commit/PR metinleri)
# Marker'da düz metne iniyordu. Yazı tipi adı ölçütü (PDF'in kendi bilgisi).
_MONO_FONT_RE = re.compile(
    r"mono|courier|consol|menlo|inconsolata|typewriter|cmtt|lmtt|"
    r"sourcecode|firacode|cascadiacode|lucidaconsole|andale|fixedsys",
    re.IGNORECASE)
# Düzyazı parçası: en az bu kadar sözcük ve bu kadar küçük harfli sözcük
# ("{I will need to look at this again tomorrow.}"). Ölçüm (makine dizgisi
# alıntılayan bir raporda): bir eş aralıklı ailenin satır içi parçalarının
# 29/37'si düzyazı (alıntı), ötekinin 2/240'ı (commit iletisi).
_MONO_PROSE_MIN_WORDS = 4
_MONO_PROSE_MIN_LOWER = 2
# Bir eş aralıklı yazı tipi ailesinin parçalarının en az bu oranı düzyazıysa
# aile ALINTI yazı tipidir: düzyazı parçaları sarılmaz (yerel model yolu
# \texttt içeriğini çevirmez; elle onarılmış referans da alıntıyı \texttt
# yapmamış), yalnız rakam/alt çizgi taşıyan tek belirteçler ("TOKEN12345")
# sarılır.
_MONO_QUOTE_FAMILY_SHARE = 0.5
# Tırnak ve cümle sonu noktalaması \texttt dışında kalır ('"\texttt{X}".'),
# altın EXPORT.tex biçimi.
_MONO_EDGE_QUOTES = "\"'“”‘’«»„"
_MONO_TAIL_PUNCT = ".,;!?"
# Bu uzunluktan uzun kod parçasında "\_" önünde satır kırılabilir
# (\allowbreak; elle onarılmış referansla aynı eşik): uzun makine dizgileri
# satırdan taşıyordu.
_TT_BREAK_MIN_CHARS = 20
# Ön denetimde aranan eş aralıklı parçanın en kısa uzunluğu (boşluksuz):
# daha kısası ("a", "x1") her metinde geçer, ayırt etmez.
_MONO_PROBE_MIN_CHARS = 4
_CODE_FENCE_BLOCK_RE = re.compile(r"^\s*```.*?^\s*```[^\n]*$", re.MULTILINE | re.DOTALL)


def _code_span_tex(content: str) -> str:
    """Kod parçası içeriğinin TeX'i (\\texttt, uzunsa '\\_' önünde kırılır)."""
    # 2026-10-05: kod parçasında ters bölü düz karakterdir (CommonMark; Marker
    # kod parçalarında kaçış yok, _wrap_pdf_monospace içeriği çözer): eskiden
    # "\d+" alt nokta aksanı, "a\\b" satır sonu oluyordu.
    tex = "".join("\\textbackslash{}" if ch == "\\" else _TEX_SPECIAL.get(ch, ch)
                  for ch in content)
    if len(content) > _TT_BREAK_MIN_CHARS:
        tex = tex.replace("\\_", "\\allowbreak{}\\_")
    return "\\texttt{" + tex + "}"


def _mono_font_family(font: str) -> str:
    """"ABCDEF+RobotoMono-Regular" -> "RobotoMono" (biçem eki atılır)."""
    name = re.sub(r"^[A-Z]{6}\+", "", font or "")
    return re.split(r"[-,]", name, maxsplit=1)[0]


def _mono_is_prose(text: str) -> bool:
    words = text.split()
    lower = [w for w in re.findall(r"[^\W\d_]+", text) if w.islower() and len(w) >= 2]
    return len(words) >= _MONO_PROSE_MIN_WORDS and len(lower) >= _MONO_PROSE_MIN_LOWER


def _mono_is_token(text: str) -> bool:
    """Tek belirteç ve kod gibi (rakam, alt çizgi, eğik çizgi, ayraç, uzantı)."""
    core = text.strip().strip(_MONO_EDGE_QUOTES).rstrip(_MONO_TAIL_PUNCT)
    return (bool(core) and not re.search(r"\s", core)
            and bool(re.search(r"[\d_/\\\[\]{}<>=#@$%^&|~]|\.[A-Za-z]", core)))


def _literal_escape_tex(m: "re.Match[str]") -> Optional[str]:
    """_MD_LITERAL_ESCAPE_RE eşleşmesinin TeX karşılığı; kod parçasında None."""
    if m.group("bs"):
        return "\\textbackslash{}" * (len(m.group("bs")) // 2)
    if m.group("tek"):
        ad = m.group("tek")
        if ad in _METIN_ICI_MATEMATIK:
            return "$\\" + ad + "$"
        once = m.string[m.start() - 1] if m.start() else ""
        if (ad[0] == "n" and ad[1:].isalpha() and ad[1:].islower()
                and (once.isalpha() or once in ".,;:)]")):
            # Marker'ın satır sonu artığı: sözcüğe yapışık "\n"
            # ("to\nfurther", "problem\nis") boşluktur.
            return " " + ad[1:]
        return "\\textbackslash{}" + ad
    if m.group("ch") == "]":
        # "{]}": basılan karakter "]"; 2_derle sanitizer'ının markdown bağ
        # kalıntısı temizliği ("[metin](adres)" -> "metin") düz metni
        # bağ sanıp "(adres)" kısmını silmesin (kayıpsızlık).
        return "{]}"
    if m.group("ch"):
        return m.group("ch")
    return None


def _stash_literal_escapes(text: str, stash) -> str:
    """Kaçışlı ters bölü / ters tırnak / köşeli ayracı düz karakter olarak
    stash'le (F1-09; bkz. _MD_LITERAL_ESCAPE_RE). Matematik önceden
    stash'lenmiş olmalı."""
    if "\\" not in text:
        return text

    def _repl(m: "re.Match[str]") -> str:
        tex = _literal_escape_tex(m)
        return m.group(0) if tex is None else stash(tex)

    return _MD_LITERAL_ESCAPE_RE.sub(_repl, text)


# Marker'ın üst simgeyle böldüğü HTML varlığı: "<sup>&</sup>quot;".
_BOLUNMUS_VARLIK_RE = re.compile(
    r"<sup>&</sup>(quot|amp|lt|gt|apos|#\d+|#x[0-9a-fA-F]+);")
# 2026-10-05: Marker metnindeki HTML varlığı ("&quot;", "&#39;", "&amp;")
# kaçıştan ÖNCE düz karaktere çözülür. Eskiden "\&quot;" olarak çıkıyor,
# 2_derle sanitizer'ı onu "\"" (umlaut aksanı: "\"\textbf{…}" derleme
# hatası) ya da "\\&" (satır sonu + hizalama hatası) yapıyordu; sayısal
# varlık "&#39;" düz basılıyordu. "&lt;"/"&gt;" çözülmez: HTML etiketi
# adımları (R11 "&lt;sup>", etiket sanılan metin koruması) onları ayrıca işler.
_HTML_VARLIK_RE = re.compile(r"&(?:[A-Za-z][A-Za-z0-9]{1,31}|#\d{1,7}|#[xX][0-9a-fA-F]{1,6});")
_COZULMEYEN_VARLIK = frozenset({"<", ">"})
# Bağlantı hedefi olamayacak "(…)": "?" ile başlar ya da adreste geçmeyen
# karakter taşır (korpusta yalnız regex parçaları: "(?P<ad>…)", "(?:…)").
_BAG_DISI_HEDEF_RE = re.compile(r"^\?|[\\<>{}|^`]")


def _html_varliklarini_coz(markdown: str) -> str:
    """Kod çiti ve kod parçası dışındaki HTML varlıklarını çöz (bkz. yukarı)."""
    if "&" not in markdown:
        return markdown

    def _coz(m: "re.Match[str]") -> str:
        ch = html.unescape(m.group(0))
        if ch == m.group(0) or ch in _COZULMEYEN_VARLIK or any(
                ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in ch):
            return m.group(0)
        return ch

    parcalar = re.split(r"(```.*?```|`[^`\n]+`)", markdown, flags=re.DOTALL)
    for k in range(0, len(parcalar), 2):
        parcalar[k] = _HTML_VARLIK_RE.sub(_coz, parcalar[k])
    return "".join(parcalar)


def _protect_and_escape(text: str) -> str:
    """
    Escape TeX-special characters in *text* while leaving math spans intact.
    """
    math_spans: List[str] = []

    def _stash(m: re.Match) -> str:
        idx = len(math_spans)
        math_spans.append(_fix_math_linebreak_bracket(m.group(0)))
        return f"\x00MATH{idx}\x00"

    out = _DISPLAY_MATH_RE.sub(_stash, text)
    out = _INLINE_MATH_RE.sub(_stash, out)

    # F1-09: kaçışlı ters bölü / ters tırnak / köşeli ayraç düz karakter
    # (tablo hücresinde "\\" eskiden tabular satırını bitiriyordu).
    def _stash_literal(tex: str) -> str:
        idx = len(math_spans)
        math_spans.append(tex)
        return f"\x00MATH{idx}\x00"

    # 2026-10-05 (G1a-2): kod parçası \texttt olur (eskiden ters tırnaklar
    # düz basılıyordu); <u>/</u> etiketi görünmez (biçim bu yolda düşer).
    out = _CODE_SPAN_RE.sub(lambda m: _stash_literal(_code_span_tex(m.group(0)[1:-1])), out)
    out = _HTML_UNDERLINE_TAG_RE.sub("", out)
    out = _stash_literal_escapes(out, _stash_literal)

    # Unescape markdown-escaped TeX specials to prevent double-escaping
    # E.g. markdown \_  →  _  →  (TeX escape below) →  \_
    # Without this: \_  →  \ + \_  =  \\_  (line break + subscript = ERROR)
    out = _literal_asterisks(out)
    out = _unescape_markdown_specials(out)

    # Escape TeX specials only in non-placeholder parts
    parts = re.split(r"(\x00MATH\d+\x00)", out)
    for k, part in enumerate(parts):
        if not part.startswith("\x00"):
            escaped = []
            for ch in part:
                escaped.append(_TEX_SPECIAL.get(ch, ch))
            parts[k] = "".join(escaped)
    out = "".join(parts)

    for idx in range(len(math_spans) - 1, -1, -1):
        out = out.replace(f"\x00MATH{idx}\x00", math_spans[idx])

    # Tablo hücresi / başlık sayfası satırı önceki "\\" satır sonunun
    # ardından gelir: baştaki yıldız "\\*" (yıldızlı satır sonu) okunup
    # yutulmasın.
    if out.startswith("*"):
        out = "{}" + out
    return out


def _escape_tex(text: str) -> str:
    """Escape TeX specials -- thin wrapper for contexts where we KNOW there is no math."""
    return "".join(_TEX_SPECIAL.get(ch, ch) for ch in text)


def _escape_tex_keep_tokens(text: str) -> str:
    """TeX specials'i escape et; \x00PROT_N\x00 stash token'larina dokunma.

    2026-07-11 (#5): bold/italik/link icerigi _convert_inline'da stash'lendigi
    icin Step 4'un genel escape'inden KACIYORDU — '**%50 indirim & dahasi**'
    '\\textbf{%50 indirim & dahasi}' olarak cikiyor, % satirin kalanini yorumlayip
    icerik siliyor, & derlemeyi kiriyordu. Icerik sarilirken burada escape edilir.
    Once markdown-escape'leri geri al (cift-escape onlemi, _protect_and_escape
    ile ayni sozlesme).
    """
    text = _unescape_markdown_specials(text)
    parts = re.split(r"(\x00PROT_\d+\x00)", text)
    for k, part in enumerate(parts):
        if not part.startswith("\x00"):
            parts[k] = "".join(_TEX_SPECIAL.get(ch, ch) for ch in part)
    return "".join(parts)


# ---------------------------------------------------------------------------
# Kulvar C2 (2026-09-28): üst/alt simge, dipnot, şekil altyazısı, süs görseli
# ---------------------------------------------------------------------------

# Satır içi işaret nöbetçisi. Ön işlemde üretilen \textsuperscript ve
# \footnote yerleri metinde "\x02S<n>\x02" / "\x02F<n>\x02" olarak durur:
# bu karakterler TeX kaçışından (_TEX_SPECIAL), markdown biçim düzenlerinden
# ve matematik stash'inden etkilenmez; hangi yoldan geçerse geçsin (paragraf,
# başlık, liste, tablo hücresi, başlık sayfası) convert() sonunda gerçek
# komuta çevrilir (_resolve_inline_marks).
_MARK_SENTINEL = "\x02"
_MARK_RE = re.compile("\x02([SF])(\\d+)\x02")

# R11: Marker'ın HTML kalıntıları. "<sup>&</sup>lt;sup>1</sup>" = kaçışlı
# "&lt;sup>1</sup>" metninin '&'ı üst simgeye düşmüş hâli; varlık önce
# birleştirilir, sonra kaçışlı <sup>/<sub> etiketi çözülür.
_SPLIT_ENTITY_RE = re.compile(
    r"<sup>&</sup>((?:lt|gt|amp|quot|apos|#\d{1,5}|#x[0-9A-Fa-f]{1,4});)",
    re.IGNORECASE)
_ESCAPED_TAG_RE = re.compile(r"&lt;(/?)(sup|sub)(?:&gt;|>)", re.IGNORECASE)
# R11: "$<sup>^{1}</sup>$" (Marker üst simgeyi bir de matematiğe sarmış):
# eskiden "$${}^{^{1}}$$" olup satırı display matematiğe çeviriyor, dipnot
# metnini matematik kipine yutuyordu (sent_de, sent_ru).
_MATH_WRAPPED_TAG_RE = re.compile(
    r"\$\s*<(sup|sub)>\s*[\^_]?\s*\{?\s*([^<{}$]*?)\s*\}?\s*</\1>\s*\$",
    re.IGNORECASE)
_SUPSUB_RE = re.compile(
    r"(?:\*(?P<iv>[A-Za-z])\*|(?P<base>[A-Za-z0-9]))?"
    r"<(?P<tag>sup|sub)>(?P<c>[^<]+)</(?P=tag)>",
    re.IGNORECASE)
# R11: bitişik üst/alt simge dizisi ("<sup>x</sup><sup>i</sup>", Marker'ın
# satır içi matematik çorbası). Tek tek çevrilince "${}^{x}$${}^{i}$" olur;
# AutoFix bitişik iki matematiği "$^{x}^{i}$" diye birleştirip "Double
# superscript" derleme hatası üretiyordu (sent_it). Dizi TEK matematik olur:
# "${}^{x}{}^{i}$" (görünüm aynı).
_SUPSUB_RUN_RE = re.compile(
    r"(?:\*(?P<iv>[A-Za-z])\*|(?P<base>[A-Za-z0-9]))?"
    r"(?P<run>(?:<(?:sup|sub)>[^<]+</(?:sup|sub)>){2,})",
    re.IGNORECASE)
_SUPSUB_ONE_RE = re.compile(r"<(sup|sub)>([^<]+)</\1>", re.IGNORECASE)
# Dipnot/işaret simgesi: 1-3 rakam ya da *, †, ‡, §, ¶ (1-3 tane).
_SUP_MARK_RE = re.compile(r"^(?:\d{1,3}|[*†‡§¶]{1,3})$")
# İşaretten önce metin bağlamı gösteren karakterler (noktalama, kapanan
# tırnak, boşluk). ')' ve ']' yok: "(a+b)<sup>2</sup>" matematiktir.
_SUP_TEXT_CONTEXT = set(".,;:!?»”\"'’…")
# R4: Kiril indis (kısaltma): "н", "а", "раб", "пост".
_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]+")
_CYRILLIC_INDEX_RE = re.compile(r"^[А-Яа-яЁё]{1,6}\.?$")
# R4: italik Latin değişkene yapışık Kiril indis ("*< p*н", "*p*н"): Marker
# alt simgeyi düz metne indirmiş. Italik kapanışının hemen ardından 1-3 küçük
# Kiril harf ve harf olmayan sınır.
_ITALIC_CYR_INDEX_RE = re.compile(
    r"(?<!\*)\*(?!\*)(?P<pre>[^*\n]*?)(?P<var>[A-Za-z])\*(?!\*)"
    r"(?P<idx>[а-яё]{1,3})(?![А-Яа-яЁёA-Za-z0-9])")

# F1-10 (2026-09-29): PDF (Marker) sayfa altı dipnot tanımı — satır başında
# üst simge nöbetçisi + metin (bkz. MarkdownToLatex._pair_page_footnotes).
# Pencere: tanım ile gövde işareti arasında en çok bu kadar dolu satır
# (ölçüm, 29 Eyl: sentetik kitaplarda ≤14, gerçek bir raporda ≤23; bir
# sayfada ~30-40 blok olur).
_PAGE_FOOTNOTE_DEF_RE = re.compile("^[ \t]*\x02S(\\d+)\x02[ \t]*(\\S.*)$")
_SUP_SENTINEL_RE = re.compile("\x02S(\\d+)\x02")
_PAGE_FOOTNOTE_WINDOW = 40
_FOOTNOTE_REF_PUNCT = set(".,;:!?)]»”\"'’…")
# FX1 (V1 yeni hata 3): Marker'ın tek paragrafta birleştirdiği ardışık
# tanımlarda bölme noktası: cümle sonu (+ kapayıcı) + boşluk + üst simge
# nöbetçisi + harf/tırnak/ayraç/vurgu/kod ile başlayan metin.
_MERGED_DEF_CUT_RE = re.compile(
    "(?P<end>[.!?…][\"'”’»)\\]*_`]*)(?P<ws>[ \t]+)(?P<mark>\x02S(?P<k>\\d+)\x02)"
    "[ \t]*(?=[^\\W\\d_]|[\"'“‘«(\\[*_`])")
# FX1: kapanan ayraçtan sonra önişlemde matematiğe alınmış
# üst simge rakamı ("value)${}^{12}$ is"), ardından düzyazı (boşluk + harf),
# noktalama + boşluk ya da satır sonu. "(a+b)${}^{2}$ = c" (işleç) aday değil.
_PAREN_MATH_SUP_RE = re.compile(
    r"(?<=[)\]])\$\{\}\^\{(\d{1,3})\}\$(?=[ \t]+[^\W\d_]|[.,;:][ \t]|[ \t]*$)")
# Ayraç içinin düzyazı denetimi için açılış ayracı en çok bu kadar karakter
# geride aranır (ölçülen en uzun örnekte ayraç ~110 karakter).
_PAREN_PROSE_MAX = 300
# FX1: işaretten hemen önce kapanan HTML metin etiketi ("</u>³"):
# Marker'ın altı çizili/italik metni dönüştürücüde sonradan komuta döner.
_CLOSING_TEXT_TAG_RE = re.compile(
    r"</(?:u|i|b|em|strong|span|a|s|small|mark)\s*>$", re.IGNORECASE)
# FX1: çok paragraflı dipnotun PDF kanıtı. Eşleme imzası metnin
# normalleştirilmiş ilk _FN_SIG_CHARS harf-rakamı; en az _FN_CONT_MIN_CHARS
# olmalı (kısa satırın tek eşleşmesi kanıt sayılmaz). Punto karşılaştırma
# payı _FN_SIZE_TOL pt; dipnot puntosu gövdeden en az _FN_BODY_GAP pt küçük
# (ölçülen bir raporda: gövde 11 pt, dipnot 10 pt, dipnot numarası 6 pt).
_FN_SIG_CHARS = 40
_FN_CONT_MIN_CHARS = 30
_FN_SIZE_TOL = 0.25
_FN_BODY_GAP = 0.5
# Paragraf boşluğu: yeni paragrafın ilk satırı öncekinin son satırından en az
# bu kat × punto aşağıda (ölçülen raporda: satır aralığı 11,5 pt = 1,15 × 10 pt;
# çok paragraflı bir dipnotun paragrafları arasında boş satırla 23 pt = 2,3 ×).
_FN_PARA_GAP_RATIO = 1.6
_FN_NORM_DROP_RE = re.compile(r"\x02[SF]\d+\x02|\]\([^)\s]*\)|<[^>\n]+>")


def _fn_norm(text: str) -> str:
    """Dipnot kanıtı için metin: nöbetçi, bağ hedefi ve HTML etiketi
    atılmış, NFKC, yalnız küçük harf-rakam (PDF ve Markdown aynı biçim)."""
    out: List[str] = []
    for ch in unicodedata.normalize("NFKC", html.unescape(_FN_NORM_DROP_RE.sub("", text))):
        if ch.isalnum():
            out.extend(c for c in ch.lower() if c.isalnum())
    return "".join(out)


# Birleşik tanımdaki komşu numaraların en büyük farkı: aynı sayfanın
# dipnotları. Ölçülen raporda 7 birleşik tanımın hepsinde fark 1 (ters ya
# da düz sıra; "12 11 10" gibi zincir); 3 sayfada birkaç tanım atlanmasına pay.
_MERGED_DEF_MAX_GAP = 3


def _is_footnote_ref_context(line: str, start: int, end: int) -> bool:
    """Gövdedeki üst simge nöbetçisi dipnot işareti bağlamında mı (F1-10)?

    İşaret sözcüğe ya da noktalamaya bitişik (ya da aralarında tek boşluk)
    olmalı; ardından boşluk, satır sonu, noktalama ya da başka bir işaret
    gelmeli. Marker'ın üst simgeye böldüğü sayı ("<sup>1</sup>*,*<sup>5</sup>"
    = 1,5; "1,5" / "3.2" gibi ardından rakam gelen) işaret SAYILMAZ: eskiden
    dipnot o sayının basamağını silerdi.
    """
    after = line[end:end + 1]
    if after and not after.isspace():
        if after == _MARK_SENTINEL:
            if not line.startswith(_MARK_SENTINEL + "S", end):
                return False
        elif after == "\\" and line.startswith("\\_", end):
            # FX1: işaret kod/ileti adının ortasında ("ALPHA¹⁰⁵_REPLY",
            # Marker'da "ALPHA <sup>105</sup>\_REPLY"): ad alt çizgiyle sürer.
            pass
        elif after in "*_" and not line[end:].lstrip("*_")[:1].strip():
            # FX1: vurgunun kapanışı + boşluk/satır sonu ("…sonu.¹⁸*"). Ardından
            # noktalama/rakam gelen vurgu ("¹*,*⁵" = 1,5) işaret sayılmaz.
            pass
        elif after not in _FOOTNOTE_REF_PUNCT:
            return False
        elif line[end + 1:end + 2].isdigit():
            return False
    head = line[:start]
    before = head[-1:]
    # FX1: yüzde ("30%³") ve kapanan HTML metin etiketi ("</u>³")
    # sözcük sonu sayılır; tablo satırında hücre başı ("| ⁹ Metin…": Marker
    # hücredeki işareti hücrenin başına kaydırabiliyor) da.
    if (before == "%" or _CLOSING_TEXT_TAG_RE.search(head.rstrip())
            or (head.rstrip().endswith("|") and line.lstrip().startswith("|"))):
        return True
    # 2026-10-05 (G1a-2): kod parçasının kapanış ters tırnağı sözcük sonu
    # sayılır ("`23619B`<sup>81</sup>": eş aralıklı ad, dipnot işareti).
    if before.isspace():
        before = head.rstrip()[-1:]
        return bool(before) and (before.isalpha() or before in _FOOTNOTE_REF_PUNCT
                                 or before in ("*", "`"))
    return (not before or before.isalpha() or before in _FOOTNOTE_REF_PUNCT
            or before in ("*", "_", "`", _MARK_SENTINEL))


# EPUB dipnot sözleşmesi (E1, onaylı): işaret "[^kc-N]", tanım satır başında
# tek satır "[^kc-N]: metin".
_FOOTNOTE_ID = re.escape(NOTE_LABEL_PREFIX) + r"\d+"
_FOOTNOTE_DEF_RE = re.compile(r"^\[\^(" + _FOOTNOTE_ID + r")\]:[ \t]?(.*)$")
_FOOTNOTE_REF_RE = re.compile(r"\[\^(" + _FOOTNOTE_ID + r")\]")
_HEADING_CMD_RE = re.compile(
    r"^(\\(?:part|chapter|section|subsection|subsubsection|paragraph"
    r"|subparagraph))(\*?)\{")
# Kayan ortam (tablo, şekil): içindeki \footnote metnini kaybeder (kutu);
# işaret \footnotemark, metin ortamın ardında \footnotetext olur.
_FLOAT_ENV_RE = re.compile(r"\\begin\{(table|figure)\}.*?\\end\{\1\}", re.DOTALL)

# R13: şekil altyazısı paragrafı. Etiket sözcükleri tek kaynaktan
# (patterns.XREF_LABEL_WORDS["figure"]: en/de/ru/fr/es/it). Numara OCR
# biçimleriyle: "1.1", "1-2", "1 2" (nokta düşmüş), "11".
# 2026-10-05: baştaki vurgu en çok üç işaret ("***Figure 1:** …*": PDF vurgu
# onarımı kalın italik etiketi italik altyazının içine kalın olarak ekler).
_FIGURE_CAPTION_RE = re.compile(
    r"^\s*(?:\*{1,3}|_{1,3})?\s*(?:" + xref_label_alternation(["figure"])
    + r")\s*(?P<num>\d+(?:\s?[.\-–]\s?\d+|\s\d+)*[a-z]?)(?P<after>.*)$",
    re.IGNORECASE | re.DOTALL)
# Numaradan sonra altyazı ayracı ("Рис. 1.3. X", "Figura 1.1: X",
# "**Abb. 1.1** X"): noktalama ya da kapanan vurgu.
_CAPTION_SEP_RE = re.compile(r"^\s*(?:\*{1,3}|_{1,3})?\s*[.:–—\-]|^(?:\*{1,3}|_{1,3})(?:\s|$)")
_IMAGE_LINE_RE = re.compile(r"^!\[([^\]]*)\]\(([^)]+)\)\s*(?:\\\\)?\s*$")

# R14: küçük (süs) görseller. Marker sayfa görsellerini 192 dpi'lık sayfa
# görüntüsünden kırpar (ölçüldü: bir Almanca test kitabındaki kutu simgesi
# PDF'te 40,8 pt = 109 piksel). En uzun kenarı bu sınırı (kaynakta ~34 mm)
# aşmayan görsel 0.8\textwidth'e büyütülmez, kaynaktaki boyutunda basılır:
# kutu/bölüm simgeleri, logolar, süs çiçekleri yarım sayfa oluyordu.
_MARKER_IMAGE_DPI = 192
_SMALL_IMAGE_MAX_PX = 256
_DEFAULT_IMAGE_WIDTH = r"0.8\textwidth"
# G3b önerisi (mt okuma MT-46): Marker sayfa görseli kaynaktaki boyutundan
# BÜYÜK basılmaz. Eskiden 256 pikselden büyük her görsel 0.8\textwidth'e
# büyütülüyordu: kaynakta sayfanın üçte biri olan şekiller ~3 kat büyüyor,
# etiketleri metinden iri, taranmış görseller pikselli çıkıyordu (iki test
# kitabında Şekil 1.1: 365 px = kaynakta 1,9 in; 401 px = 2,1 in, PDF'teki
# kutusu 136 pt = 1,9 in). Yalnız Marker adlı görseller (192 dpi
# kırpım; çözünürlüğü bilinmeyen öteki görseller eskisi gibi).
_MARKER_IMAGE_NAME_RE = re.compile(r"^_page_\d+_(?:Figure|Picture)_\d+\.")
_DEFAULT_IMAGE_FRAC = 0.8          # _DEFAULT_IMAGE_WIDTH'in \textwidth oranı
_MM_PER_INCH = 25.4
# Yerleşim ölçülmediyse şablonun yedek geometrisi: A4 (210 mm), kenar 2,5 cm.
_FALLBACK_TEXT_WIDTH_MM = 210.0 - 2 * 25.0
# R14: sayfa kenarı sekme numarası: tek başına kalın 1-3 rakam ("**1**").
_TAB_NUMBER_RE = re.compile(r"^(?:\*\*|__)\d{1,3}(?:\*\*|__)$")


def _wrap_cyrillic(content: str) -> str:
    """Matematik içeriğindeki Kiril harf dizilerini \\text{} içine al
    (matematik fontunda Kiril glifi yok: "p = p" basılıyordu)."""
    return _CYRILLIC_RE.sub(lambda m: "\\text{" + m.group(0) + "}", content)


def _normalize_script_content(raw: str) -> str:
    """<sup>/<sub> içeriği: HTML varlıkları çözülür ("&amp;" -> "&"),
    Markdown kaçışlı "\\*" yıldız olur (matematikte "\\*" görünmez kesme
    noktasıdır), iç içe üst simge açılır ("^{1}" -> "1")."""
    content = html.unescape(raw)
    content = (content.replace("\u2217", "*").replace("\u2032", "'")
               .replace("\\*", "*"))
    if content.lstrip()[:1] in ("^", "_"):
        content = re.sub(r"^\s*[\^_]\s*\{?\s*(.*?)\s*\}?\s*$", r"\1", content)
    return content


def _math_script_content(content: str) -> str:
    """<sup>/<sub> içeriğini matematik indisi olarak güvenli yaz."""
    content = content.replace("$", "")
    content = re.sub(r"(?<!\\)([&%#])", r"\\\1", content)
    return _wrap_cyrillic(content)


_HREF_URL_ARG_RE = re.compile(r"(\\href\{)([^{}]*)(\})")


def _protect_href_urls(tex: str) -> str:
    """Başka bir komutun argümanına (\\footnote, \\caption*) giren
    ``\\href`` adresinde '#' ve '%' kaçışlanır: argüman içinde hyperref
    adresi kelimesi kelimesine okuyamaz; '#' "Illegal parameter number",
    '%' dipnotun geri kalanını yorum yapıp "File ended while scanning use
    of \\@footnotetext" hatası veriyordu (bir kitabın dipnotlarında
    "ch07.html#ch07"; bir başkasının altyazılarında "#page-51-0")."""
    return _HREF_URL_ARG_RE.sub(
        lambda m: m.group(1) + re.sub(r"(?<!\\)([#%])", r"\\\1", m.group(2))
        + m.group(3), tex)


def _tex_balanced(tex: str) -> bool:
    """Kaçışsız '{'/'}' dengeli ve '$' sayısı çift mi? Komut argümanına
    (\\caption*, \\footnote) giren metin dengesizse argüman okuması
    belgenin sonuna kadar kaçar ("File ended while scanning"); düz
    paragrafta aynı kusur yerel ve onarılabilir bir hatadır (Marker'ın
    "*A\\**" italiği: "\\textit{A\\}...")."""
    depth = dollars = 0
    i = 0
    while i < len(tex):
        ch = tex[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                return False
        elif ch == "$":
            dollars += 1
        i += 1
    return depth == 0 and dollars % 2 == 0


def _braced_end(text: str, open_idx: int) -> int:
    """text[open_idx] == '{' için eşleşen '}' indeksi; yoksa -1."""
    depth = 0
    i = open_idx
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _code_block_preceding(output: List[str]) -> str:
    """Kod çitinden önceki gövde metninin sonu (2026-10-06, kutunun yarım
    cümleyi sürdürüp sürdürmediği kararı için): son dolu blok + ardındaki boş
    satırlar + çiti ayıran satır sonu.
    Gövde ``"\\n".join(output)`` ile kurulur; karar yalnız sondaki satıra
    bakar (sanitizer.listing_continues_paragraph)."""
    k = len(output)
    while k > 0 and not output[k - 1].strip():
        k -= 1
    return "\n".join(output[max(k - 1, 0):]) + "\n"


def _join_to_previous_paragraph(output: List[str], text: str) -> None:
    """Yarım cümleyi sürdüren kutunun metnini önceki paragrafa ekle
    (sanitizer._prose_listings_to_quote'un bağlamasıyla aynı: aradaki boşluk
    ve boş satırlar kalkar, tek boşlukla birleşir)."""
    while output and not output[-1].strip():
        output.pop()
    if output:
        output[-1] = output[-1].rstrip() + " " + text
    else:   # pragma: no cover - bağlama kararı önceki metin ister
        output.append(text)


# E (2026-10-06, mt okuma MT-24): şablonun kckutu ortamı içeriği bölünemez
# bir \vbox'ta dizer. Aşağıdakiler kutuda dizilemez ya da kutuda anlamını
# yitirir; kutu içeriğinde görülürse kutu basılmaz, metin kutusuz AYNEN kalır:
# bölüm ve üstü başlıklar (\chapter sayfa keser; \section/\subsection üst
# bilgi işareti \vbox'tan çıkmaz), kayan nesne ve tablolar (\vbox içinde
# "Not in outer par mode"; uzun tablo bölünemez), kod blokları, görsel,
# ön/ana/arka madde geçişleri, sayfa kesmeleri, iç içe kutu ve dipnot
# (\vbox içindeki dipnot ekleme düğümü kutu basılınca kaybolur). Dipnot
# işareti bu noktada henüz nöbetçidir (\x02F<n>\x02, _resolve_inline_marks).
_KUTU_YASAK_RE = re.compile(
    r"\\(?:part|chapter|section|subsection)\*?\s*[\[{]"
    r"|\\begin\{(?:figure|table|longtable|tabular|tabularx|lstlisting"
    r"|verbatim|Verbatim|minted|titlepage|kcfit|" + kutu_onarim.ORTAM
    + r")\*?\}"
    r"|\\(?:includegraphics|includepdf|mainmatter|frontmatter|backmatter"
    r"|appendix|clearpage|cleardoublepage|newpage|pagebreak|maketitle"
    r"|tableofcontents|footnote|footnotetext|marginpar)(?![A-Za-z])"
    "|\x02F\\d+\x02")


# ---------------------------------------------------------------------------
# MarkdownToLatex
# ---------------------------------------------------------------------------

class MarkdownToLatex:
    """
    Convert Markdown (with LaTeX math) into LaTeX body text.

    The converter works line-by-line, accumulating paragraphs and detecting
    block-level structures (headings, lists, code blocks, tables, images,
    display math).  Handles Marker-specific artifacts and book front matter.
    """

    def __init__(self, config: PipelineConfig):
        self.config = config
        # 2026-09-24: yalın "Chapter N" etiketi görüldü, sonraki başlık
        # bölüm seviyesine zorlanacak (bkz. _try_heading).
        self._pending_chapter_label: Optional[str] = None
        # 2026-09-25 (Y1): convert() başında kurulan yapı planı (bkz.
        # _plan_structure). Plan yoksa (_try_heading doğrudan çağrılırsa)
        # eski satır-satır davranış geçerlidir.
        self._heading_plan: Optional[Dict[int, _HeadingDecision]] = None
        self._first_anchor_idx: Optional[int] = None
        self._skip_lines: Set[int] = set()
        self._title_authors: List[str] = []
        # 2026-09-25 (R8): başlık satırı -> başlığa " — " ile eklenecek alt ad
        # (EPUB p.subh2, bkz. ALTAD_MARKER).
        self._heading_suffix: Dict[int, str] = {}
        self._roman_altad: Set[int] = set()
        # 2026-09-28 (C2): satır içi işaret nöbetçilerinin içerikleri
        # (\x02S<n>\x02 -> \textsuperscript, \x02F<n>\x02 -> \footnote).
        self._sup_marks: List[str] = []
        self._footnote_marks: List[str] = []      # nöbetçi -> dipnot kimliği
        self._footnote_defs: Dict[str, str] = {}  # kimlik -> tanım metni
        # R13: görsel satırı -> (altyazı metni, "before"/"after").
        self._figure_captions: Dict[int, Tuple[str, str]] = {}
        # F1-15: kaynak PDF'teki başlık puntoları (convert() başına bir kez).
        self._pdf_heading_sizes_cache: Optional[Dict[str, float]] = None
        # 2026-10-05 (G1a-1): kaynak PDF'in yer imleri (outline), (düzey, ad).
        self._pdf_outline_cache: Optional[List[Tuple[int, str]]] = None
        self._transition_idx: Optional[int] = None
        self._appendix_titles: Dict[int, str] = {}
        self._appendix_started = False
        self._text_before_first_heading = False
        self._titleless_title_idx: Optional[int] = None
        # E (2026-10-06, mt okuma MT-24): kaynak PDF kutuları. Olaylar
        # (satır, "bas"/"son") kutu_onarim.isaretleri_ayikla'dan; açık
        # kutunun \begin{kckutu} satırının output'taki yeri; sayaçlar.
        self.kutu_raporu = kutu_onarim.KutuRaporu()
        self._kutu_olaylari: List[Tuple[int, str]] = []
        self._kutu_acik: Optional[int] = None
        self.kutu_sayaci: Dict[str, int] = {}
        # 2026-10-05 (G1a-2): kaynak PDF'in sayfa metinleri ve okunan
        # sayfaların bağlantı dizinleri (<u>…</u> için, bkz. _pdf_page_links).
        self._pdf_link_index_cache: Optional[Dict[str, object]] = None
        # 2026-10-06: düzyazı kod çiti -> vurgulu alıntı sayaçları (kod_vurgu).
        self.kod_vurgu_raporu = kod_vurgu.KodVurguRaporu()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _kaynak_pdf_var(self) -> bool:
        """Kaynak PDF (Marker yolu) gerçekten var mı? EPUB girdisinde
        config.input_pdf'in gösterdiği source_book.pdf yoktur."""
        yol = getattr(self.config, "input_pdf", None)
        return bool(yol) and str(yol).lower().endswith(".pdf") and Path(yol).is_file()

    def convert(self, markdown: str, images: Optional[Dict] = None) -> str:
        """
        Convert *markdown* to LaTeX body text.

        Parameters
        ----------
        markdown : Marker-produced Markdown string.
        images   : dict mapping image filenames to paths (used to verify existence).

        Returns
        -------
        LaTeX body text (str) -- no preamble, no \\begin{document}.
        """
        images = images or {}
        # 2026-09-28 (C2): satır içi işaretler çağrılar arasında sızmasın.
        self._sup_marks = []
        self._footnote_marks = []
        self._footnote_defs = {}
        self._figure_captions = {}
        self._pdf_heading_sizes_cache = None
        self._pdf_link_index_cache = None
        self.marker_onarim_sayaci: Dict[str, int] = {}
        self.kod_vurgu_raporu = kod_vurgu.KodVurguRaporu()

        # 2026-10-05: Marker HTML varlığını üst simgeyle bölebiliyor
        # ("<sup>&</sup>quot;"); bölünmüş
        # varlık birleştirilip çözülür, yoksa çıktıya "\\&quot;" sızıyordu.
        markdown = _BOLUNMUS_VARLIK_RE.sub(
            lambda m: html.unescape("&" + m.group(1) + ";"), markdown)
        markdown = _html_varliklarini_coz(markdown)

        # 2026-10-05 (bağlama, G1c — 30 Eylül modülü): Marker'ın satır içi
        # simge hataları (alt/üst simge, vektör oku, r³→m³, derece, tablo
        # sayıları) kaynak PDF'in metin katmanıyla onarılır. Ham <sup>/<sub>/
        # <br> etiketlerini görmeli: ön işlemden ve _wrap_pdf_monospace'ten
        # ÖNCE (G1a-2 notu). PDF yoksa (EPUB) Markdown aynen döner.
        markdown = pdf_metin_onarim.onar(
            markdown, getattr(self.config, "input_pdf", None))
        # 2026-10-05: Marker'ın düşürdüğü kalın/italik vurgu PDF yazı
        # tiplerinden geri gelir (bkz. vurgu_onarim); ham Markdown
        # üzerinde, kod sarmalamadan önce. PDF yoksa Markdown aynen döner.
        try:
            markdown, self.vurgu_raporu = vurgu_onarim.onar_raporlu(
                markdown, getattr(self.config, "input_pdf", None))
        except Exception as exc:  # noqa: BLE001 - onarım dönüşümü durdurmaz
            logger.warning("PDF vurgu onarımı atlandı: %s", exc)
            self.vurgu_raporu = vurgu_onarim.VurguRaporu()
        if self.vurgu_raporu.sayac:
            logger.info("PDF vurgu onarımı: %s", self.vurgu_raporu.sayac)

        # E (2026-10-06, mt okuma MT-24): Marker kaynak PDF'teki çerçeveli /
        # renkli zeminli kutuları düşürüyordu. Kutuya TAM düşen bütün bloklar
        # kutu işaretleriyle sarılır (kutu_onarim); işaretler satırlar
        # işlenmeden önce ayıklanır (yapı planı onları görmez) ve kutu
        # \begin{kckutu}…\end{kckutu} olarak basılır. Yalnız PDF yolu.
        self.kutu_raporu = kutu_onarim.KutuRaporu()
        if self._kaynak_pdf_var():
            try:
                markdown, self.kutu_raporu = kutu_onarim.onar_raporlu(
                    markdown, getattr(self.config, "input_pdf", None))
            except Exception as exc:  # noqa: BLE001 - onarım dönüşümü durdurmaz
                logger.warning("PDF kutu onarımı atlandı: %s", exc)
                self.kutu_raporu = kutu_onarim.KutuRaporu()
            if self.kutu_raporu.kutular:
                logger.info("PDF kutu onarımı: %d kutu",
                            len(self.kutu_raporu.kutular))

        # 2026-10-05 (G1a-2): kaynak PDF'te eş aralıklı (kod) yazı
        # tipindeki satır içi parçalar Markdown kod parçası olur (`…` ->
        # \texttt). Ham Marker Markdown'ı üzerinde, ön işlemden ÖNCE çalışır
        # (hizalama <sup> etiketli ham metni bekler; bkz. _wrap_pdf_monospace).
        markdown = self._wrap_pdf_monospace(markdown)

        # --- Preprocessing: clean Marker artifacts ---
        markdown = self._preprocess_markdown(markdown)
        # 2026-10-05 (bağlama, G1b): sayfa kırılımı onarımı — bölünmüş
        # paragraf/madde/liste/tablo, sızan sayfa numarası, denklem devamı,
        # başlığı yinelenen devam tablosu (KARAR 30). Dipnot işlemlerinden
        # ÖNCE (dipnot tanımları ayrı paragraf kalır; araya giren tanımlar
        # birleşen paragrafın ardına taşınır). Yalnız PDF (Marker) yolu:
        # EPUB'da sayfa kırılımı yoktur.
        if self._kaynak_pdf_var():
            markdown, self.marker_onarim_sayaci = marker_onarim.onar_rapor(markdown)
            degisen = {k: v for k, v in self.marker_onarim_sayaci.items() if v}
            if degisen:
                logger.info("Marker sayfa kırılımı onarımı: %s", degisen)
        # EPUB dipnotları (E1 sözleşmesi): tanımlar bütün metinden toplanır,
        # işaretler nöbetçiye döner (bkz. _extract_footnotes).
        markdown = self._extract_footnotes(markdown)
        # F1-10: PDF (Marker) sayfa altı dipnotları ("<sup>N</sup>Metin"
        # paragrafı) gövdedeki işaretlerine \footnote olarak taşınır.
        markdown = self._pair_page_footnotes(markdown)
        # 2026-10-05 (G1a-1): Marker'ın yapı hataları (başlığa dönmüş madde,
        # başlığa yapışık soru, gövdeye dönmüş/yapışık yer imi başlığı,
        # altyazıya yapışık gövde) satır düzeyinde onarılır; yapı planı ve
        # dönüşüm onarılmış satırları görür.
        self._pdf_outline_cache = None
        markdown = self._repair_marker_structure(markdown)

        lines = markdown.split("\n")
        # E (2026-10-06): kutu işaretleri ayıklanır (satırlar işaretsiz
        # Markdown'la birebir aynı kalır); sınırlar satır indeksi olarak
        # tutulur ve döngüde basılır (_kutu_olaylarini_isle).
        self._kutu_olaylari = []
        self._kutu_acik = None
        self.kutu_sayaci = {}
        if self.kutu_raporu.kutular:
            lines, self._kutu_olaylari = kutu_onarim.isaretleri_ayikla(lines)
        output: List[str] = []
        # R12: liste maddesinin devam satırı kuralı belge türüne bağlı.
        self._hard_wrapped = self._is_hard_wrapped(lines)
        # 2026-10-05 (G1a-2): satırlara bölünmüş paragrafta satır sonunu aşan
        # vurgu satır satır kapanıp açılır (bkz. _split_wrapped_emphasis).
        if self._hard_wrapped:
            lines = self._split_wrapped_emphasis(lines)

        is_book = self.config.document_class == "book"

        # Determine if this is a "start of book" extraction
        # Only build title page + frontmatter when extracting from the beginning
        pr = self.config.page_range
        is_book_start = is_book and (pr is None or (pr and min(pr) <= 1))

        self._in_frontmatter = is_book_start
        self._frontmatter_ended = False
        self._seen_headings: set = set()
        self._last_heading_key = None  # (#46) convert() cagrilari arasinda sizmasin
        self._book_title = ""
        self._title_page_emitted = False
        self._title_page_done = False
        self._pending_chapter_label = None
        self._heading_plan = None
        self._first_anchor_idx = None
        self._skip_lines = set()
        self._title_authors = []
        self._heading_suffix = {}
        # 2026-10-05 (G1a-1): başlıksız başlayan belge (ilk başlıktan önce
        # düzyazı; bkz. _build_title_page), başlıksız belgede taşınan geçiş
        # noktası (_plan_titleless_transition) ve kitap sonu ekleri
        # (_plan_appendices: satır -> "Appendix:" öneksiz ad).
        self._text_before_first_heading = False
        self._titleless_title_idx: Optional[int] = None
        self._transition_idx: Optional[int] = None
        self._appendix_titles: Dict[int, str] = {}
        self._appendix_started = False

        # For book class: collect pre-heading content for title page (only at start)
        start_line = 0
        if is_book_start:
            title_page_tex, consumed = self._build_title_page(lines)
            if title_page_tex:
                output.append(title_page_tex)
                output.append("")
                start_line = consumed
                # Mark the title heading as seen so it won't be duplicated
                for line in lines[:consumed]:
                    m = re.match(r"^(#{1,6})\s+(.*)", line)
                    if m:
                        clean = re.sub(r"\*\*(.+?)\*\*", r"\1", m.group(2)).strip()
                        self._book_title = clean
                        self._seen_headings.add(f"{len(m.group(1))}:{clean}")
                        break

            # NOT: \frontmatter kaldirildi -- sayfa numaralari her zaman
            # arap rakami (1, 2, 3) olsun.  Kisa test cevirilerinde roman
            # rakami (i, ii, iii) ve bos ToC sayfasi sorununa yol aciyordu.

        # 2026-09-25 (Y1): çok dilli bölüm yapısı planı — başlık satırları
        # için düzey/başlık kararları, ön madde -> ana madde geçiş noktası ve
        # bölüm başlığına katılan gövde satırları (bkz. _plan_structure).
        self._heading_plan = self._plan_structure(
            lines, start_line, is_book_start=is_book_start)
        # 2026-10-05 (G1a-1): başlıksız ve çapasız belgede ana madde,
        # PDF puntosu bölüm düzeyinde gösteren öndeki başlıklardan başlar.
        if is_book_start and self._first_anchor_idx is None:
            self._plan_titleless_transition(lines, start_line)
        # 2026-10-05 (G1a-1, MT-23): ek başlıkları.
        self._plan_appendices(lines, start_line)
        # 2026-09-28 (C2): sayfa üst bilgisi satırları (R14) ve şeklin
        # bitişiğindeki altyazı paragrafları (R13) gövdeye ayrıca basılmaz.
        self._plan_running_headers(lines, start_line)
        self._plan_figure_captions(lines, start_line)

        i = start_line
        while i < len(lines):
            line = lines[i]

            # E (2026-10-06): kutu sınırı bu satırdaysa \begin/\end{kckutu}.
            if self._kutu_olaylari:
                self._kutu_olaylarini_isle(lines, i, output)

            # Bölüm başlığına katılmış satır (Roma rakamlı bölüm + BÜYÜK
            # HARFLİ tek satırlık ad): gövdede ikinci kez basılmaz.
            if i in self._skip_lines:
                i += 1
                continue

            # Ardisik-baslik dedup sozlesmesi (#46): baslik OLMAYAN dolu bir
            # satir gorulunce _last_heading_key sifirlanir — araya govde
            # metni giren mesru tekrar basliklar ('### Proof' ... '### Proof')
            # eskiden 'bitisik dup' sanilip siliniyordu.
            if line.strip() and not re.match(r"^\s*#{1,6}\s+", line):
                self._last_heading_key = None

            # --- Başlığa katılamayan alt ad satırı (R8, E1 sözleşmesi) -----
            # İşaret metin olarak basılmaz ("<!– kc:altad –>" çıkıyordu);
            # alt ad düz paragraf kalır.
            if line.lstrip().startswith(ALTAD_MARKER):
                rest = line.lstrip()[len(ALTAD_MARKER):].strip()
                if rest:
                    output.append(self._convert_inline(rest))
                i += 1
                continue

            # --- Display math block ($$...$$) spanning multiple lines ------
            if line.strip().startswith("$$"):
                block, consumed = self._consume_display_math(lines, i)
                output.append(block)
                i += consumed
                continue

            # --- Fenced code block -----------------------------------------
            if line.strip().startswith("```"):
                block, consumed = self._consume_code_block(lines, i, output)
                if block is not None:   # None: önceki paragrafa bağlandı
                    output.append(block)
                i += consumed
                continue

            # --- Table (lines starting with |) -----------------------------
            if line.strip().startswith("|"):
                block, consumed = self._consume_table(lines, i)
                output.append(block)
                i += consumed
                continue

            # --- Heading ---------------------------------------------------
            # 2026-09-24: yalın "Chapter N" etiketi için bir sonraki dolu
            # satıra bakılır (etiket + başlık = tek \chapter, bkz. _try_heading).
            next_nonblank = None
            if re.match(r"^\s*#{1,6}\s+", line):
                next_nonblank = self._next_nonblank_line(lines, i + 1)
            heading = self._try_heading(
                line, next_nonblank=next_nonblank, line_idx=i)
            if heading is not None:
                output.append(heading)
                i += 1
                continue

            # --- Image (![alt](src)) on its own line -----------------------
            img = self._try_image(line.strip(), line_idx=i)
            if img is not None:
                output.append(img)
                i += 1
                continue

            # --- Unordered list item (- or *) ------------------------------
            if re.match(r"^\s*[-*]\s+", line):
                block, consumed = self._consume_unordered_list(lines, i)
                output.append(block)
                i += consumed
                continue

            # --- Ordered list item (1. 2. ...) -----------------------------
            if re.match(r"^\s*\d+\.\s+", line):
                block, consumed = self._consume_ordered_list(lines, i)
                output.append(block)
                i += consumed
                continue

            # --- Horizontal rule -------------------------------------------
            if re.match(r"^\s*[-*_]{3,}\s*$", line):
                output.append(r"\bigskip\noindent\rule{\textwidth}{0.4pt}\bigskip")
                i += 1
                continue

            # --- Blank line (paragraph separator) --------------------------
            if line.strip() == "":
                output.append("")
                i += 1
                continue

            # --- Plain paragraph line --------------------------------------
            output.append(self._convert_inline(line))
            i += 1

        # E (2026-10-06): belge sonundaki kutu "son" olayı (kutu son satırda
        # biter) ya da kapanmayan kutu.
        if self._kutu_olaylari:
            self._kutu_olaylarini_isle(lines, len(lines), output)
        if self._kutu_acik is not None:
            self._kutu_geri_al(output, "kapanmadi")
        if self.kutu_sayaci:
            logger.info("PDF kutuları: %s", self.kutu_sayaci)

        # For book class: frontmatter artik kullanilmadigi icin
        # burada ToC eklemeye gerek yok.  Eskiden \frontmatter modunda
        # kalinirsa yedek olarak ToC ekleniyordu.

        body = "\n".join(output)
        body = self._resolve_inline_marks(body)
        if self.kod_vurgu_raporu.sayac:
            logger.info("Düzyazı kod çiti (vurgulu alıntı): %s", self.kod_vurgu_raporu.sayac)
        logger.info("Converted %d markdown lines -> %d chars LaTeX", len(lines), len(body))
        return body

    # ------------------------------------------------------------------
    # E (2026-10-06, mt okuma MT-24): kaynak PDF kutuları
    # ------------------------------------------------------------------

    def _kutu_say(self, anahtar: str) -> None:
        self.kutu_sayaci[anahtar] = self.kutu_sayaci.get(anahtar, 0) + 1

    def _kutu_olaylarini_isle(self, lines: List[str], i: int,
                              output: List[str]) -> None:
        """Döngü i. satıra geldi: indeksi i'yi geçmeyen kutu olaylarını uygula.

        "bas" kutuyu yalnız tam kendi satırında açar: bir blok tüketicisi
        (liste, tablo, denklem) kutunun ilk satırını yuttuysa kutu basılmaz.
        "son" kendi satırında ya da tüketici aradaki yalnız BOŞ satırları
        yuttuysa kapatır; dolu satır yutulduysa (kutu dışındaki metin kutuya
        girerdi) kutu geri alınır."""
        while self._kutu_olaylari and self._kutu_olaylari[0][0] <= i:
            idx, tur = self._kutu_olaylari.pop(0)
            if tur == "bas":
                if idx == i and self._kutu_acik is None:
                    self._kutu_acik = len(output)
                    output.append(self._kutu_acilis())
                else:
                    self._kutu_say("bas_yutuldu")
                continue
            if self._kutu_acik is None:
                continue
            if idx == i or not any(lines[k].strip()
                                   for k in range(idx, min(i, len(lines)))):
                self._kutu_kapat(output)
            else:
                self._kutu_geri_al(output, "son_yutuldu")

    @staticmethod
    def _kutu_acilis() -> str:
        return "\\begin{%s}" % kutu_onarim.ORTAM

    def _kutu_kapat(self, output: List[str]) -> None:
        """Açık kutuyu kapat; içerik kutuya uygun değilse geri al."""
        bas = self._kutu_acik
        icerik = "\n".join([output[bas].replace(self._kutu_acilis(), "", 1)]
                           + output[bas + 1:])
        if not icerik.strip():
            self._kutu_geri_al(output, "bos")
        elif _KUTU_YASAK_RE.search(icerik):
            self._kutu_geri_al(output, "yasak_icerik")
        else:
            self._kutu_acik = None
            output.append("\\end{%s}" % kutu_onarim.ORTAM)
            self._kutu_say("basildi")

    def _kutu_geri_al(self, output: List[str], neden: str) -> None:
        """Açık kutunun \\begin satırını sil: içerik kutusuz, aynen kalır.
        (Başka bir dönüşüm o satıra metin eklediyse yalnız komut silinir.)"""
        bas = self._kutu_acik
        if output[bas] == self._kutu_acilis():
            del output[bas]
        else:
            output[bas] = output[bas].replace(self._kutu_acilis(), "", 1).lstrip()
        self._kutu_acik = None
        self._kutu_say(neden)

    # ------------------------------------------------------------------
    # Cover page from original PDF (pdfpages)
    # ------------------------------------------------------------------

    @staticmethod
    def _build_cover_from_pdf(pdf_path: str, cover_pages: Optional[List[int]] = None) -> str:
        """
        Generate LaTeX code to include original PDF cover page(s) via pdfpages.

        Uses ``\\includepdf`` so the original cover is pixel-perfect preserved
        (fonts, colours, background, layout) -- no reconstruction needed.

        Parameters
        ----------
        pdf_path    : Absolute path to the original PDF file.
        cover_pages : 1-based page numbers to include (default: [1]).

        Returns
        -------
        LaTeX string with ``\\includepdf`` command(s).  The ``pdfpages``
        package must be loaded in the preamble (handled by TemplateEngine).
        """
        if cover_pages is None:
            cover_pages = [1]

        # Validate path exists
        from pathlib import Path as _Path
        if not _Path(pdf_path).exists():
            logger.warning("Cover PDF not found: %s -- falling back to titlepage", pdf_path)
            return ""

        # Build comma-separated page list for pdfpages
        pages_str = ",".join(str(p) for p in cover_pages)

        # Use absolute path so XeLaTeX can find it regardless of cwd
        abs_path = str(_Path(pdf_path).resolve())

        lines_out = [
            f"% -- Original cover page(s) from source PDF --",
            f"\\includepdf[pages={{{pages_str}}}]{{{abs_path}}}",
        ]

        logger.info("Cover from PDF: pages=%s, path=%s", pages_str, abs_path)
        return "\n".join(lines_out)

    # ------------------------------------------------------------------
    # Title page builder (for book class)
    # ------------------------------------------------------------------

    def _build_title_page(self, lines: List[str]) -> tuple[str, int]:
        """
        Scan the first lines to build a title page for book class.

        Returns (latex_str, lines_consumed).  Lines consumed = number of
        lines from the start that were absorbed into the title page.
        """
        title = ""
        authors: List[str] = []
        cover_images: List[str] = []
        lines_consumed = 0
        found_title = False
        self._text_before_first_heading = False

        for idx, line in enumerate(lines):
            stripped = line.strip()

            # Skip blank lines (count them as consumed if before/near title)
            if not stripped:
                if not found_title:
                    lines_consumed = idx + 1
                continue

            # Check for heading
            m = re.match(r"^(#{1,6})\s+(.*)", stripped)
            if m:
                if not found_title:
                    # First heading = book title
                    raw = m.group(2).strip()
                    title = re.sub(r"\*\*(.+?)\*\*", r"\1", raw).strip()
                    found_title = True
                    lines_consumed = idx + 1
                    continue
                else:
                    # Second heading = stop
                    break

            # Check for image (only consume first image after title as cover)
            img_m = re.match(r"^!\[([^\]]*)\]\(([^)]+)\)\s*$", stripped)
            if img_m and found_title and len(cover_images) == 0:
                src = Path(img_m.group(2)).name
                # Resolve actual extension (Marker may reference .jpeg but file is .png)
                src = self._resolve_image(src)
                cover_images.append(src)
                lines_consumed = idx + 1
                continue

            # Plain text before first heading = author names
            # Only accept short, clean lines as authors (no math, no images,
            # no markdown links, no display math).
            if not found_title:
                if (len(stripped) < 150
                        and "$$" not in stripped
                        and "![" not in stripped
                        and "$" not in stripped
                        and "\\[" not in stripped
                        and not re.search(r'\[.*\]\(', stripped)):
                    authors.append(stripped)
                    lines_consumed = idx + 1
                    continue
                elif re.match(r"^!\[[^\]]*\]\([^)]+\)\s*$", stripped):
                    # Başlıktan önce tek başına görsel (kapak): eskisi gibi
                    # tüketilir; kapağı şablon kaynak PDF'in 1. sayfasından basar.
                    lines_consumed = idx + 1
                    continue
                else:
                    # 2026-09-28: ilk başlıktan önce düzyazı / matematik /
                    # bağlantı var → belge başlıkla başlamıyor (rapor, makale,
                    # kitap kesiti) ve ilk başlık kitap adı değil. Eskiden bu
                    # satırlar tüketilip sessizce ATILIYORDU (bir raporda
                    # 4 sayfa / 9 400 karakter, bir test kitabında 6 300
                    # karakter). Başlık sayfası kurulmaz, hiçbir satır atılmaz.
                    logger.info(
                        "Body text before the first heading — no title page, "
                        "nothing consumed",
                    )
                    # 2026-10-05 (G1a-1): ilk "#" başlık da kitap adı
                    # sayılmaz (_try_heading); eskiden ana madde ikinci "#"de
                    # başlıyor, İçindekiler bölüm ortasına düşüyordu.
                    self._text_before_first_heading = True
                    return "", 0

            # Any other content after title+image: stop consuming
            break

        if not title:
            return "", 0

        # Validate title: reject section numbers and generic labels
        _bad_title_re = re.compile(
            r"^(?:\d+(?:\.\d+)+\s|Example\s+\d|Figure\s+\d|Table\s+\d|NOTE\b|FIGURE\s+\d)",
            re.IGNORECASE,
        )
        if _bad_title_re.match(title) or len(title) < 3:
            logger.warning("Rejected invalid title '%s' — skipping titlepage", title)
            return "", 0

        # 2026-09-24: yalın "Chapter N"/"Bölüm N" etiketi kitap adı DEĞİLDİR.
        # Bir test kitabında "## Chapter 1" başlık sayfası oldu ("Chapter 1" adlı
        # kitap); etiket yutulduğu için 1. bölüm tesadüfen doğru çıktı, 2.
        # bölümde aynı örüntü \section{Chapter 2} verdi. Etiket gövdeye
        # bırakılır; _try_heading onu ardındaki başlıkla tek \chapter yapar.
        # 2026-09-25 (Y1): etiket + başlık aynı satırda da kitap adı değildir
        # ("# Kapitel 1 Grundlagen – …" başlık sayfasına gidip bölüm kayboluyordu);
        # etiket altı dilde ve kısım etiketleri için de tanınır.
        # Roma rakamlı bölüm başlığı ("I. Le départ", "II.") da kitap adı
        # değildir; yalnız boşlukla ayrılan zayıf biçim ("V for Vendetta")
        # kitap adı olabilir.
        clean = self._clean_heading_title(title)
        roman = parse_roman_heading(clean)
        if parse_heading_label(clean) is not None or (roman is not None and roman[2]):
            logger.info(
                "Chapter/part label '%s' is not a book title — skipping titlepage",
                title,
            )
            return "", 0

        # Build titlepage LaTeX
        parts = [
            "\\begin{titlepage}",
            "  \\centering",
            "  \\vspace*{3cm}",
            f"  {{\\Huge\\bfseries {_protect_and_escape(title)}}}\\par",
            "  \\vspace{1.5cm}",
        ]

        if cover_images:
            parts.append(f"  \\includegraphics[width=0.5\\textwidth]{{{cover_images[0]}}}")
            parts.append("  \\vfill")

        if authors:
            author_str = " \\\\\n  ".join(
                _protect_and_escape(a) for a in authors
            )
            parts.append(f"  {{\\Large {author_str}}}\\par")
        else:
            parts.append("  \\vfill")

        parts.append("  \\vfill")
        parts.append("\\end{titlepage}")

        logger.info("Built title page: '%s' by %s (%d lines consumed)",
                     title, ", ".join(authors), lines_consumed)
        # Yazar adları yapı planında "yazar adı başlığı" tanımak için tutulur.
        self._title_authors = list(authors)
        return "\n".join(parts), lines_consumed

    # ------------------------------------------------------------------
    # Markdown preprocessing -- clean Marker artifacts
    # ------------------------------------------------------------------

    def _preprocess_markdown(self, text: str) -> str:
        """
        Clean Marker-produced artifacts from markdown before conversion.

        Handles: line-end hyphenation, HTML tags, auto-links, page headers,
        duplicate headings, manual TOC sections, excess blank lines.
        """
        # 0. Rejoin line-end hyphenation from OCR / PDF extraction.
        #    PDF renderers break long words across lines with a hyphen:
        #      "Fur-\nthermore" should become "Furthermore"
        #      "approxi-\nmation" should become "approximation"
        #    Pattern: lowercase letter + hyphen + newline + lowercase letter.
        #    This does NOT touch real compound words like "self-attention"
        #    because those do not have a newline after the hyphen.
        text = re.sub(r'([a-z])-\n([a-z])', r'\1\2', text)
        # Also handle hyphen + optional whitespace (multiple spaces / \r\n)
        # that OCR sometimes produces instead of a clean newline.
        # Guard: only rejoin if the total matched span is short (<20 chars)
        # to avoid merging unrelated lines.
        text = re.sub(
            r'([a-z])-\s{2,}([a-z])',
            lambda m: m.group(1) + m.group(2) if len(m.group(0)) < 20 else m.group(0),
            text,
        )

        # 0.5  Fix concatenated words inside italic spans from OCR.
        #   Marker OCR sometimes loses spaces inside italic blocks, producing
        #   *Burada,Fuygulanankuvvettir,kyaysabitidirvexyerdeitirmedir.*
        #   We detect italic spans with 30+ chars and no spaces, then insert
        #   spaces at camelCase boundaries, after commas, and after periods.
        #   Inline math ($...$) is stashed first so formulas are never touched.
        text = self._fix_italic_concatenation(text)

        # 1. Strip HTML tags: <br>, <br/>, <br />, etc.
        text = re.sub(r"<br\s*/?\s*>", " ", text, flags=re.IGNORECASE)

        # 2. Convert <sup>/<sub> to LaTeX math before stripping.
        #    If preceded by a letter/digit (e.g. "x<sup>0</sup>"), merge the
        #    base into math mode: "x<sup>0</sup>" -> "$x^{0}$".
        #    If standalone (no preceding alphanumeric), use empty base:
        #    "<sup>T</sup>" -> "${}^{T}$".
        #    2026-09-28 (C2): HTML kalıntıları (R11), Kiril indisler (R4) ve
        #    metin içi dipnot işaretleri (\textsuperscript) — bkz.
        #    _convert_sup_sub.
        text = self._convert_sup_sub(text)

        # 3. Strip other common HTML tags
        # 2026-09-29: etiket adından sonra sınır ŞART ((?=[\s/>])). Eskiden
        # "<p…>"/"<i…>" ile başlayan her şey içeriğiyle siliniyordu: "<pending
        # review>", "<payload>", "<prefix>", IGNORECASE ile "<I think…>" (bir
        # raporda 8 düzyazı parçası + 6 düzenli ifade grubu sessizce kayboldu).
        # 2026-10-05 (G1a-2): sınır şartı boşluğa izin verdiği için "<I think
        # it is fine…>" yine siliniyordu; etiket artık HTML sözdizimiyle
        # tanınır ve biçim etiketleri Markdown'a çevrilir (bkz.
        # _convert_inline_html; <u> _convert_inline'da).
        text = self._convert_inline_html(text)

        # 3. Convert auto-links <https://url> to plain text
        text = re.sub(r"<(https?://[^>]+)>", r"\1", text)

        # 4. Remove page header artifacts ("xii Contents", "Contents xiii", "2.2 Title 11")
        lines = text.split("\n")
        cleaned = []
        for line in lines:
            stripped = line.strip()
            if _PAGE_HEADER_RE.match(stripped):
                logger.debug("Removed page header: %s", stripped)
                continue
            if _BODY_PAGE_HEADER_RE.match(stripped) and len(stripped) < 60:
                logger.debug("Removed body page header: %s", stripped)
                continue
            # 2026-09-28 (R14): sayfa kenarındaki sekme numarası ("**1**")
            # gövdeye "\textbf{1}" paragrafı olarak giriyordu (bir Almanca
            # test kitabında).
            if _TAB_NUMBER_RE.match(stripped):
                logger.debug("Removed thumb-tab number: %s", stripped)
                continue
            cleaned.append(line)

        # 5. Remove duplicate consecutive # headings (same level+text)
        deduped = []
        prev_heading_key = None
        for line in cleaned:
            m = re.match(r"^(#{1,6})\s+(.*)", line)
            if m:
                level = len(m.group(1))
                title_text = m.group(2).strip()
                # Normalize: strip bold markers for comparison
                normalized = re.sub(r"\*\*(.+?)\*\*", r"\1", title_text).strip()
                heading_key = f"{level}:{normalized}"
                if heading_key == prev_heading_key:
                    logger.debug("Removed duplicate heading: %s", line.strip())
                    continue
                prev_heading_key = heading_key
            else:
                if line.strip():
                    prev_heading_key = None
            deduped.append(line)

        # 6. Detect and remove manual TOC section
        #    Pattern: ### Contents (or similar) followed by pipe tables
        #    2026-09-25 (R18): başka dillerin içindekiler başlıkları
        #    ("Inhaltsverzeichnis", "Оглавление", "Table des matières",
        #    "Índice general", "Indice"; patterns.CONTENTS_HEADING_NAMES) de
        #    basılı tabloyla birlikte atılır. Gerekçe: kitap \tableofcontents
        #    ile Türkçe sayfa numaralı içindekiler üretir; Marker'ın tabloya
        #    çevirdiği basılı içindekiler ÖZGÜN (bayat) sayfa numaralarını
        #    taşıyıp ikinci bir içindekiler olarak basılıyordu (5/5 sentetik
        #    belge). İngilizce "Contents" davranışı aynen korunur; diğer
        #    adlarda ("Sommario", "Inhalt" özet/içerik de olabilir) başlık
        #    ancak hemen ardından tablo geliyorsa atılır.
        #    2026-09-28 (R18): basılı içindekilerin devam sayfalarındaki üst
        #    bilgi başlığı ("viii ◾ Contents", "Inhalt IX") da aynı kurala
        #    girer (_is_contents_heading); eskiden ilk sayfa atılıyor, devam
        #    sayfaları "\subsection{viii ◾ Contents}" + bayat tablo olarak
        #    gövdede kalıyordu (büyük bir İngilizce kitapta 5 sayfa).
        final = []
        in_toc = False
        for k, line in enumerate(deduped):
            stripped = line.strip()

            # Detect "Contents" heading at any level
            if re.match(r"^#{1,6}\s+\**Contents\**\s*$", stripped):
                in_toc = True
                logger.debug("Detected manual TOC section -- removing")
                continue
            head_m = re.match(r"^#{1,6}\s+(.*)$", stripped)
            if head_m and _is_contents_heading(
                    self._clean_heading_title(head_m.group(1))):
                nxt = next((ln.strip() for ln in deduped[k + 1:] if ln.strip()), "")
                if nxt.startswith("|"):
                    in_toc = True
                    logger.debug("Detected printed TOC table (%s) -- removing",
                                 stripped)
                    continue

            if in_toc:
                # Skip pipe table lines and blank lines within TOC
                if stripped.startswith("|") or stripped == "":
                    continue
                # Non-table, non-blank line: TOC section is over
                in_toc = False

            final.append(line)

        text = "\n".join(final)

        # 7. (KALDIRILDI 2026-09-29, F1-05) Marker'ın köşeli ayraçlı atıf
        #    bağları ("[[4\]](#page-12-0)", "[\[4\]](#page-…)",
        #    "[105\]](#page-…)", "[[1,2](#page-…)]") eskiden burada
        #    "[4](#page-…)"ya indiriliyor, atıf ayraçları siliniyordu.
        #    Artık _convert_inline'ın 0. adımı (_stash_marker_escaped_links)
        #    ayraçları bağın dışında korur: "[\href{#page-12-0}{4}]".

        # 8. Collapse 3+ consecutive blank lines into 2
        text = re.sub(r"\n{3,}", "\n\n", text)

        return text

    def _convert_inline_html(self, text: str) -> str:
        """Satır içi HTML etiketleri (G1a-2).

        - ``<a href="adres">metin</a>`` -> ``[metin](adres)`` (eskiden etiket
          görünür kalıyordu; iki test kitabında);
        - ``<b>``/``<strong>`` -> ``**…**``, ``<i>``/``<em>`` -> ``*…*``
          (eskiden yalnız silinip biçim düşüyordu; tablo hücrelerinde kaynakta
          kalın/italik: CMBX8 ya da ``.B`` yazı tipi);
        - tablo hücresindeki ``<ul><li>…</li></ul>`` maddeleri boşlukla
          ayrılır;
        - span/div/p/pre/br ve çifti bulunamayan biçim etiketleri silinir.

        Etiket ancak HTML sözdizimine uyuyorsa (ad + "ad=değer" öznitelikleri)
        etikettir: "<I think …>", "<a command …>" düzyazısı korunur. ``<u>``
        burada değil, _convert_inline'da çevrilir (PDF bağlantı denetimi).
        """
        if "<" not in text:
            return text
        text = _HTML_ANCHOR_RE.sub(_html_anchor_repl, text)
        text = _HTML_EMPH_RE.sub(_html_emph_repl, text)
        text = _HTML_LIST_ITEM_RE.sub(" ", text)
        text = _HTML_STRIP_TAG_RE.sub("", text)
        # İçinde başka etiket olduğu için _convert_sup_sub'ın çeviremediği
        # üst/alt simge ("<sup><b>1</b></sup>") iç etiket silinince çevrilir.
        if _SUPSUB_TAG_RE.search(text):
            text = self._convert_sup_sub(text)
            # Eşi olmayan üst/alt simge etiketi görünmesin (içerik kalır).
            text = _SUPSUB_TAG_RE.sub("", text)
        return text

    # ------------------------------------------------------------------
    # C2 (2026-09-28): üst/alt simge, Kiril indis, dipnot işaretleri
    # ------------------------------------------------------------------

    def _sup_mark(self, content: str) -> str:
        """``\\textsuperscript{content}`` yerine geçen nöbetçi (bkz. _MARK_RE)."""
        return f"{_MARK_SENTINEL}S{self._sup_mark_index(content)}{_MARK_SENTINEL}"

    def _sup_mark_index(self, content: str) -> int:
        """Yeni üst simge nöbetçisinin numarası (içerik _sup_marks'a eklenir)."""
        self._sup_marks.append(content)
        return len(self._sup_marks) - 1

    def _convert_sup_sub(self, text: str) -> str:
        """Marker'ın ``<sup>``/``<sub>`` etiketlerini LaTeX'e çevir.

        Eski kural aynen (yalnız aşağıdaki durumlar değişir): önünde tek
        harf/rakam taban varsa ``$x^{0}$``, yoksa ``${}^{T}$``.

        - R11: bölünmüş HTML varlığı ("<sup>&</sup>lt;sup>1</sup>") ve
          matematiğe sarılmış iç içe üst simge ("$<sup>^{1}</sup>$") önce
          onarılır; içerikteki ``&``/``%``/``#`` kaçışlanır (eskiden
          "${}^{&}$" derlemeyi kırıyordu), HTML varlıkları çözülür.
        - R4: içerik Kiril kısaltma indisiyse ("<sup>н</sup>") konum ALT
          simgeye düzeltilir (Marker Kiril alt indisi üst simge okuyor; Rus
          teknik dizgisinde Kiril harfli indis alt indistir) ve harfler
          ``\\text{}`` içine alınır: ``*p*<sup>н</sup>`` -> ``$p_{\\text{н}}$``.
          Matematik fontunda Kiril glifi olmadığı için eskiden "p = p"
          basılıyordu. Italik değişkene yapışık Kiril indis ("*< p*н") de
          alt indise döner.
        - Metin içi işaret (dipnot / not simgesi, EPUB ``<sup>N</sup>``
          sözleşmesi): içerik 1-3 rakam ya da ``*†‡§¶`` ve önünde boşluk,
          noktalama ya da ≥2 harfli sözcük sonu varsa
          ``\\textsuperscript{N}``. Eskiden sözcüğün son harfi matematiğe
          alınıyordu ("filosofía<sup>1</sup>" -> "filosofí$a^{1}$"); Markdown
          kaçışlı ``\\*`` matematikte görünmez (``\\*`` = kesme noktası)
          olduğu için "*" dipnot işareti hiç basılmıyordu.
        """
        text = _SPLIT_ENTITY_RE.sub(lambda m: "&" + m.group(1), text)
        text = _ESCAPED_TAG_RE.sub(
            lambda m: f"<{m.group(1)}{m.group(2).lower()}>", text)
        text = _MATH_WRAPPED_TAG_RE.sub(
            lambda m: (f"<{m.group(1).lower()}>{m.group(2)}"
                       f"</{m.group(1).lower()}>"), text)
        text = _SUPSUB_RUN_RE.sub(self._supsub_run_repl, text)
        text = _SUPSUB_RE.sub(self._supsub_repl, text)
        text = _ITALIC_CYR_INDEX_RE.sub(self._italic_cyr_index_repl, text)
        return text

    def _supsub_run_repl(self, m: "re.Match[str]") -> str:
        """Bitişik etiket dizisini tek matematik yap (bkz. _SUPSUB_RUN_RE).

        Dizide Kiril indis varsa ya da etiketler düzgün eşleşmiyorsa dizi
        tek tek kurala bırakılır; metin bağlamında yalnız işaret
        simgelerinden oluşan dizi ("sözcük<sup>1</sup><sup>2</sup>") art
        arda \\textsuperscript olur."""
        tags = _SUPSUB_ONE_RE.findall(m.group("run"))
        if "".join(f"<{t}>{c}</{t}>" for t, c in tags).lower() != \
                m.group("run").lower():
            return m.group(0)
        ivar, base = m.group("iv"), m.group("base") or ""
        contents = [_normalize_script_content(raw).strip() for _, raw in tags]
        if any(_CYRILLIC_INDEX_RE.match(c) for c in contents):
            return m.group(0)
        if not ivar and all(t.lower() == "sup" and _SUP_MARK_RE.match(c)
                            for (t, _), c in zip(tags, contents)):
            if self._sup_mark_context(m.string, m.start() + len(base)):
                return base + "".join(self._sup_mark(c) for c in contents)
        parts = []
        for (tag, _), c in zip(tags, contents):
            op = "^" if tag.lower() == "sup" else "_"
            parts.append(f"{op}{{{_math_script_content(c)}}}")
        prefix = f"*{ivar}*" if ivar else ""
        return prefix + "$" + base + ("" if base else "{}") + "{}".join(parts) + "$"

    def _sup_mark_context(self, src: str, at: int) -> bool:
        """``src[at:]``'daki işaret simgeli ``<sup>`` (1-3 rakam ya da
        ``*†‡§¶``) metin içi işaret (dipnot) bağlamında mı?

        Önünde boşluk, noktalama ya da ≥2 harfli sözcük sonu (eski kural);
        FX1: yüzde işareti ("30%³"), kapanan vurgu/kod ("*…*³",
        "`kod`³": tek harfli italik değişken "*x*²" _SUPSUB_RE'nin
        ``iv`` grubuyla ayrıca matematik kalır) ve kapanan HTML metin etiketi
        ("</u>³"). Bunlar eskiden "${}^{N}$" oluyor, dipnot eşlenmiyordu."""
        before = src[at - 1] if at > 0 else ""
        before2 = src[at - 2] if at > 1 else ""
        if (not before or before.isspace() or before in _SUP_TEXT_CONTEXT
                or (self._is_word_letter(before) and self._is_word_letter(before2))):
            return True
        if before == "%":
            return True
        if before in "*_`" and before2 and not before2.isspace():
            # Kapanan vurgu/kod: içerik en az iki harf-rakamsa metin ("*bir
            # alıntı*³", "`ABC123`³"); tek harfli vurgu ("**v**¹": kalın
            # vektör) matematik kalır.
            run = re.search(r"[*_`]+$", src[:at]).group(0)
            acilis = src.rfind(run, 0, at - len(run))
            icerik = src[acilis + len(run):at - len(run)] if acilis >= 0 else ""
            return len(re.sub(r"[\W_]", "", icerik)) >= 2
        return bool(_CLOSING_TEXT_TAG_RE.search(src, max(0, at - 12), at))

    @staticmethod
    def _is_word_letter(ch: str) -> bool:
        """Sözcük harfi mi? Yunan harfi matematik değişkenidir, değiştirici
        harf (U+02C6 "ˆ": Marker'ın şapkalı vektörü "gˆ<sup>1</sup>") da
        sözcük harfi sayılmaz."""
        return (bool(ch) and ch.isalpha()
                and unicodedata.category(ch) != "Lm"
                and not ("\u0370" <= ch <= "\u03ff"))

    def _supsub_repl(self, m: "re.Match[str]") -> str:
        tag = m.group("tag").lower()
        ivar = m.group("iv")
        base = m.group("base") or ""
        content = _normalize_script_content(m.group("c"))
        stripped = content.strip()

        # R4: Kiril kısaltma indisi -> alt indis, \text{} içinde.
        if _CYRILLIC_INDEX_RE.match(stripped):
            var = ivar or base
            return f"${var}_{{\\text{{{stripped}}}}}$" if var else \
                "${}_{\\text{" + stripped + "}}$"

        prefix = f"*{ivar}*" if ivar else ""
        src = m.string
        tag_start = m.start() + len(prefix) + len(base)
        if tag == "sup" and not ivar and _SUP_MARK_RE.match(stripped):
            if self._sup_mark_context(src, tag_start):
                return base + self._sup_mark(stripped)

        c = _math_script_content(content)
        op = "^" if tag == "sup" else "_"
        if base:
            return f"{prefix}${base}{op}{{{c}}}$"
        return f"{prefix}${{}}{op}{{{c}}}$"

    @staticmethod
    def _italic_cyr_index_repl(m: "re.Match[str]") -> str:
        """"*< p*н" -> "< $p_{\\text{н}}$"; "*a b*н" -> "*a b*${}_{\\text{н}}$"."""
        pre, var, idx = m.group("pre"), m.group("var"), m.group("idx")
        if not re.search(r"[^\W\d_]", pre):
            return f"{pre}${var}_{{\\text{{{idx}}}}}$"
        return f"*{pre}{var}*" + "${}_{\\text{" + idx + "}}$"

    def _extract_footnotes(self, text: str) -> str:
        """EPUB dipnot sözleşmesi (E1, onaylı): ``[^kc-N]`` işaretleri ve
        ``[^kc-N]: metin`` tanım satırları.

        Tanımlar BÜTÜN metinden toplanır (tek dosyalı Gutenberg kitabında
        hepsi kitabın sonunda, birden çok bölüm başlığından sonra gelir);
        işaretli tanım satırı gövdeden çıkar, işaret nöbetçiye döner ve
        convert() sonunda işaretin yerinde ``\\footnote{metin}`` olur
        (metin normal satır içi dönüşümden geçer). Tanımı olmayan işaret
        uyarıyla ``\\textsuperscript{N}`` olarak kalır; işaretsiz (ya da
        yinelenen) tanım metni kaybolmasın diye düz paragraf kalır.
        """
        if "[^" not in text:
            return text
        lines = text.split("\n")
        defs: Dict[str, str] = {}
        def_at: Dict[int, "re.Match[str]"] = {}
        for i, line in enumerate(lines):
            m = _FOOTNOTE_DEF_RE.match(line)
            if m:
                def_at[i] = m
                defs.setdefault(m.group(1), m.group(2).strip())
        if not def_at and not _FOOTNOTE_REF_RE.search(text):
            return text
        refs: Set[str] = set()
        for i, line in enumerate(lines):
            if i not in def_at:
                refs.update(_FOOTNOTE_REF_RE.findall(line))
        for body in defs.values():
            refs.update(_FOOTNOTE_REF_RE.findall(body))
        self._footnote_defs = {k: v for k, v in defs.items() if k in refs}

        out: List[str] = []
        taken: Set[str] = set()
        for i, line in enumerate(lines):
            m = def_at.get(i)
            if m is None:
                out.append(_FOOTNOTE_REF_RE.sub(self._footnote_ref_repl, line))
                continue
            fid = m.group(1)
            if fid in refs and fid not in taken:
                taken.add(fid)
                continue
            logger.warning(
                "Dipnot tanımı %s: işaretsiz ya da yinelenen tanım gövdede "
                "düz paragraf olarak kalıyor", fid)
            out.append(m.group(2).strip())
        return re.sub(r"\n{3,}", "\n\n", "\n".join(out))

    def _pair_page_footnotes(self, text: str) -> str:
        """F1-10: PDF (Marker) sayfa altı dipnotlarını gerçek \\footnote yap.

        Marker sayfa altındaki dipnotu ayrı paragraf olarak "<sup>N</sup>Metin"
        (ya da "$<sup>^N</sup>$ Metin") yazar; gövdedeki işaret "…sözcük.
        <sup>N</sup>" biçimindedir. İkisi de ön işlemde üst simge nöbetçisine
        döner (_convert_sup_sub). Eskiden tanım paragrafı sayfa sonunun denk
        geldiği yerde gövdede kalıyordu ("\\textsuperscript{2}Die …" tablodan
        hemen önce, dil_sent_de).

        Tanım: tek başına paragraf (öncesi/sonrası boş satır, başka tanım ya
        da tanımın devamı olamayacak blok: bkz. _alone), satır başında işaret
        (1-3 rakam ya da *†‡§¶) ve ardından en az iki harfli sözcük içeren
        metin. Marker'ın tek paragrafta birleştirdiği ardışık tanımlar önce
        numara sınırlarından bölünür (_split_merged_footnote_defs; FX1).
        Eşleme: tanımdan GERİYE doğru aynı işaretli, henüz eşlenmemiş en yakın
        gövde nöbetçisi; en çok _PAGE_FOOTNOTE_WINDOW dolu satır, aynı numaralı
        başka bir tanımda ya da numaralandırmanın yeniden başladığı "# "
        (bölüm) başlığında arama durur (_footnote_restart_rows). Eşleşen işaret
        yerinde \\footnote[N]{metin} olur (KARAR 31: kaynak numarası), tanım
        paragrafı gövdeden çıkar (metin dipnota taşınır; kayıp yok).
        Eşleşmeyen tanım olduğu gibi paragraf kalır, eşleşmeyen işaret
        \\textsuperscript olarak görünür kalır; sayılar uyarıyla günlüğe
        yazılır.
        """
        # KARAR 31: eşlenen dipnotun kaynak numarası (kimlik -> "N").
        self._footnote_nums: Dict[str, str] = {}
        self._fn_pdf_cache = None    # FX1 PDF kanıt dizini (bu çağrıya özgü)
        if _MARK_SENTINEL not in text or self._footnote_marks:
            # EPUB dipnot sözleşmesi ([^kc-N]) etkinse bu belge EPUB'dur:
            # notlar orada; EPUB çıktısının not sırası eşlemesi bozulmasın.
            return text
        lines = text.split("\n")
        # V1 (2026-09-29): çitli kod bloğu (```) satırları taranmaz. Kod
        # yorumu "# ..." bölüm başlığı sanılıp geriye aramayı durduruyordu
        # (Python kodu içeren sayfada dipnot eşlenmiyordu).
        code_rows = self._code_fence_rows(lines)
        # FX1 (V1 yeni hata 3): "38 … 37 …" tek paragraf.
        lines, split_of = self._split_merged_footnote_defs(lines, code_rows)
        if split_of:
            code_rows = self._code_fence_rows(lines)
        n = len(lines)
        defs: List[Tuple[int, int]] = []   # (satır, nöbetçi no)
        for i, line in enumerate(lines):
            if i in code_rows:
                continue
            m = _PAGE_FOOTNOTE_DEF_RE.match(line)
            if not m or not re.search(r"[^\W\d_]{2}", m.group(2)):
                continue
            defs.append((i, int(m.group(1))))
        if not defs:
            return text
        def_rows = {i for i, _ in defs}
        barrier = self._footnote_restart_rows(lines, defs, code_rows)

        def _alone(i: int) -> bool:
            before = i == 0 or not lines[i - 1].strip() or (i - 1) in def_rows
            # FX1 (V1 yeni hata 4): ardından boş satırsız gelen görsel,
            # başlık, kod çiti, madde ya da (kesin kapanmış tanımdan sonra)
            # yeni gövde cümlesi tanımın devamı değildir (bkz.
            # marker_onarim.tanim_ardi_sinir_mi; Marker'ın PDF'siz yolunda
            # da geçerli).
            after = (i + 1 >= n or not lines[i + 1].strip() or (i + 1) in def_rows
                     or marker_onarim.tanim_ardi_sinir_mi(lines[i], lines[i + 1]))
            return before and after

        used: Set[int] = set()
        drop: Dict[int, str] = {}          # eşlenen tanım satırı -> dipnot kimliği
        marker_row: Dict[str, int] = {}    # dipnot kimliği -> işaret satırı
        for i, k in defs:
            content = self._sup_marks[k]
            if not _alone(i):
                continue
            found: Optional[Tuple[int, int]] = None
            nonblank = 0
            j = i - 1
            while j >= 0 and nonblank <= _PAGE_FOOTNOTE_WINDOW:
                line = lines[j]
                if line.strip():
                    nonblank += 1
                if j in code_rows:
                    j -= 1
                    continue
                if j in def_rows:
                    dm = _PAGE_FOOTNOTE_DEF_RE.match(line)
                    if dm and self._sup_marks[int(dm.group(1))] == content:
                        break
                    j -= 1
                    continue
                for mm in reversed(list(_SUP_SENTINEL_RE.finditer(line))):
                    kk = int(mm.group(1))
                    if (kk not in used and self._sup_marks[kk] == content
                            and _is_footnote_ref_context(line, mm.start(), mm.end())):
                        found = (j, kk)
                        break
                # FX1: kapanan ayraçtan sonraki üst simge
                # önişlemde matematik olmuştu ("value)${}^{12}$ is"); aynı
                # numaralı tanım için, ardından düzyazı sürüyorsa işarettir.
                # Daha sağdaysa (tanıma daha yakın) nöbetçi adayından önce gelir.
                pm = [x for x in _PAREN_MATH_SUP_RE.finditer(line)
                      if x.group(1) == content.strip()
                      and self._prose_parenthesis_before(line, x.start())]
                if pm and (found is None or pm[-1].start() > mm.start()):
                    x = pm[-1]
                    kk = self._sup_mark_index(x.group(1))
                    lines[j] = (line[:x.start()] + f"{_MARK_SENTINEL}S{kk}{_MARK_SENTINEL}"
                                + line[x.end():])
                    found = (j, kk)
                if found or j in barrier:
                    break   # numaralandırmanın yeniden başladığı bölüm başlığı
                j -= 1
            if found is None:
                continue
            j, kk = found
            logger.debug("PDF dipnotu %s: işaret satır %d <- tanım satır %d",
                         content, j + 1, i + 1)
            used.add(kk)
            fid = f"pdf-{len(self._footnote_marks)}"
            self._footnote_defs[fid] = _PAGE_FOOTNOTE_DEF_RE.match(
                lines[i]).group(2).strip()
            self._footnote_nums[fid] = content.strip()
            self._footnote_marks.append(fid)
            # İşaretin önündeki boşluk atılır ("Schicht *i*. <sup>2</sup>
            # Praktisch" -> "…i.\footnote{…} Praktisch"): dipnot işareti
            # sözcüğe/noktalamaya bitişik basılır.
            lines[j] = re.sub(
                "[ \t]*" + re.escape(f"{_MARK_SENTINEL}S{kk}{_MARK_SENTINEL}"),
                lambda _m: (f"{_MARK_SENTINEL}F{len(self._footnote_marks) - 1}"
                            f"{_MARK_SENTINEL}"),
                lines[j], count=1)
            drop[i] = fid
            marker_row[fid] = j
        # FX1 (muhafazakâr): bölünen parçalardan işaretine eşlenemeyen, önceki
        # parçasına aynen geri katılır (önceki eşlendiyse onun dipnot
        # metnine): bölme yalnız eşlemeyle kanıtlanınca kalır.
        merged_back: Set[int] = set()
        for p in sorted(split_of, reverse=True):
            if p in drop:
                continue
            q, ws = split_of[p]
            if q in drop:
                fid = drop[q]
                self._footnote_defs[fid] = self._footnote_defs[fid] + ws + lines[p].strip()
            else:
                lines[q] = lines[q] + ws + lines[p]
            merged_back.add(p)
        # FX1: çok paragraflı dipnotun gövdede kalan devam
        # paragrafları PDF kanıtıyla dipnota katılır (boş satırla ayrılı;
        # _footnote_text \par ile birleştirir).
        moved: Set[int] = set()
        for fid, rows in self._footnote_continuations(
                lines, drop, code_rows | def_rows | merged_back, marker_row).items():
            self._footnote_defs[fid] += "".join("\n\n" + lines[r].strip() for r in rows)
            moved.update(rows)
        if moved:
            logger.info("PDF dipnotu: %d devam paragrafı çok paragraflı dipnota katıldı",
                        len(moved))
        paired = len(drop)
        unpaired = len(defs) - paired - len(merged_back)
        if len(split_of) > len(merged_back):
            logger.info("PDF dipnotu: birleşik tanım paragrafından %d tanım ayrıldı",
                        len(split_of) - len(merged_back))
        if unpaired:
            logger.warning(
                "PDF dipnotu: %d tanım \\footnote'a taşındı, %d tanım eşlenemedi "
                "(gövde işareti bulunamadı ya da tanım tek paragraf değil; "
                "\\textsuperscript'li paragraf olarak kaldı)", paired, unpaired)
        elif paired:
            logger.info("PDF dipnotu: %d tanım \\footnote'a taşındı", paired)
        if not drop:
            return text
        out = [ln for i, ln in enumerate(lines)
               if i not in drop and i not in merged_back and i not in moved]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(out))

    def _pdf_footnote_index(self) -> Optional[Dict[str, object]]:
        """Kaynak PDF'in dipnot kanıtı dizini (convert() başına bir kez; FX1).

        Her sayfa için normalleştirilmiş harf-rakam akışı (_fn_norm; PyMuPDF
        içerik akışı sırası), her karakterin puntosu ve satırının üst kenarı
        (y); belgenin gövde
        puntosu (bütün sayfalarda karakter sayısına göre en sık punto; uzun
        dipnotlu tek bir sayfada dipnot puntosu çoğunlukta olabilir). PDF
        yoksa ya da okunamazsa None."""
        cache = getattr(self, "_fn_pdf_cache", None)
        if cache is not None:
            return cache or None
        self._fn_pdf_cache = {}
        pdf = Path(getattr(self.config, "input_pdf", "") or "")
        if pdf.suffix.lower() != ".pdf" or not pdf.is_file():
            return None
        try:
            import fitz  # PyMuPDF
            doc = fitz.open(str(pdf))
        except Exception as e:  # noqa: BLE001 - kanıt yoksa dokunulmaz
            logger.debug("Dipnot PDF dizini kurulamadı: %s", e)
            return None
        pages: List[Tuple[str, List[float], List[float]]] = []
        count: Dict[float, int] = {}
        try:
            for page in doc:
                chars: List[str] = []
                sizes: List[float] = []
                ys: List[float] = []
                for blk in page.get_text("dict").get("blocks", []):
                    for ln in blk.get("lines", []):
                        y = float(ln["bbox"][1])
                        for sp in ln.get("spans", []):
                            sz = round(float(sp.get("size", 0.0)), 2)
                            for c in _fn_norm(sp.get("text", "")):
                                chars.append(c)
                                sizes.append(sz)
                                ys.append(y)
                                count[sz] = count.get(sz, 0) + 1
                pages.append(("".join(chars), sizes, ys))
        except Exception as e:  # noqa: BLE001
            logger.debug("Dipnot PDF dizini okunamadı: %s", e)
            return None
        finally:
            doc.close()
        self._fn_pdf_cache = {"pages": pages,
                              "body": max(count, key=count.get) if count else 0.0}
        return self._fn_pdf_cache

    @staticmethod
    def _fn_locate(idx: Dict[str, list], text: str) -> Optional[Tuple[int, int]]:
        """Metnin PDF akışındaki TEK yeri (sayfa, akış konumu); imza kısa ya
        da birden çok yerde geçiyorsa None (kanıt yok)."""
        sig = _fn_norm(text)[:_FN_SIG_CHARS]
        if len(sig) < _FN_CONT_MIN_CHARS:
            return None
        hit: Optional[Tuple[int, int]] = None
        for pno, page in enumerate(idx["pages"]):
            k = page[0].find(sig)
            while k >= 0:
                if hit is not None:
                    return None
                hit = (pno, k)
                k = page[0].find(sig, k + 1)
        return hit

    @staticmethod
    def _prose_parenthesis_before(line: str, close_end: int) -> bool:
        """``line[close_end-1]`` kapanan ayraçsa ve ayraç içi DÜZYAZIysa
        (en az iki sözcük, işleç/komut yok: "(a simple test value)") True;
        matematik ("(a+b)", "(x)") ya da açılışı bulunamayan ayraç False."""
        kapanis = line[close_end - 1:close_end]
        acilis = {")": "(", "]": "["}.get(kapanis)
        if not acilis:
            return False
        derin, i = 0, close_end - 1
        while i >= 0 and close_end - i <= _PAREN_PROSE_MAX:
            if line[i] == kapanis:
                derin += 1
            elif line[i] == acilis:
                derin -= 1
                if derin == 0:
                    ic = line[i + 1:close_end - 1]
                    return (not re.search(r"[=+\-−×·/^*<>|$]|\\[a-zA-Z]", ic)
                            and bool(re.search(r"[^\W\d_]{2,}\s+[^\W\d_]{2,}", ic)))
            i -= 1
        return False

    @staticmethod
    def _par_safe_marker(lines: List[str], row: Optional[int], mark_no: int) -> bool:
        """İşaret (\\x02F<no>\\x02) satırında \\par'a dayanıklı yerde mi?
        Başlık ve tablo satırı değil; önündeki metinde açık vurgu (tek sayıda
        ``**``/``__``/``*``/sözcük sınırında ``_``), açık bağ metni ``[``,
        açık kod (tek sayıda ters tırnak) ya da kapanmamış HTML metin etiketi
        yok."""
        if row is None:
            return False
        line = lines[row]
        pos = line.find(f"{_MARK_SENTINEL}F{mark_no}{_MARK_SENTINEL}")
        if pos < 0 or re.match(r"\s*(?:#|\|)", line):
            return False
        pre = re.sub(r"\\.", "", line[:pos])
        if pre.count("`") % 2 or pre.count("[") > pre.count("]"):
            return False
        for tag in ("u", "i", "b", "em", "strong", "span"):
            if len(re.findall(r"<%s[\s>]" % tag, pre, re.I)) > len(
                    re.findall(r"</%s\s*>" % tag, pre, re.I)):
                return False
        if (pre.count("**") % 2 or pre.count("__") % 2):
            return False
        tek = pre.replace("**", "").replace("__", "")
        return not (tek.count("*") % 2
                    or len(re.findall(r"(?<![^\W_])_|_(?![^\W_])", tek)) % 2)

    @staticmethod
    def _fn_cont_candidate(lines: List[str], r: int) -> bool:
        """Satır, çok paragraflı dipnotun devamı olabilecek düz, tek satırlık
        paragraf mı (başlık/madde/tablo/görsel/kod/denklem/tanım değil,
        işaret içermiyor)?"""
        s = lines[r].strip()
        if not s or (r > 0 and lines[r - 1].strip()) \
                or (r + 1 < len(lines) and lines[r + 1].strip()):
            return False
        if _MARK_SENTINEL in s or _PAGE_FOOTNOTE_DEF_RE.match(lines[r]):
            return False
        return not re.match(r"(?:#|\||!\[|```|\$\$|>|[-*+]\s|\d+[.)]\s)", s)

    def _footnote_continuations(self, lines: List[str], paired: Dict[int, str],
                                skip: Set[int], marker_row: Dict[str, int]
                                ) -> Dict[str, List[int]]:
        """Çok paragraflı dipnotun gövdede kalan devam paragrafları (FX1).

        Marker dipnotun 2. ve sonraki paragraflarını ayrı gövde paragrafı
        olarak, çoğu kez tanımdan ÖNCE yazar. Kanıt (hepsi, kaynak PDF'ten):
        eşlenen tanım D'nin metni PDF'te TEK yerde ve harf-rakam akışında
        kesintisiz; devam paragrafı P'nin metni akışta D'nin (ya da bir
        önceki devamın) BİTTİĞİ yerden başlıyor (bitişiklik: arada başka
        metin yok); P'nin ilk harfinin puntosu D'ninkiyle aynı (±_FN_SIZE_TOL)
        ve D'nin puntosu belgenin gövde puntosundan en az _FN_BODY_GAP
        küçük (dipnot alanı); P yeni paragraf: ilk satırı öncekinin son
        satırından en az _FN_PARA_GAP_RATIO × punto aşağıda (paragraf
        boşluğu; boşluksuz süren parça aynı paragrafın Marker'ca bölünmüş
        devamıdır, \\par ile katılmaz). Aday: D'nin ±_PAGE_FOOTNOTE_WINDOW satır
        çevresindeki düz, tek satırlık, en az _FN_CONT_MIN_CHARS harf-rakamlı
        paragraflar. Zincir koptuğu yerde durur (kısmi kanıtla taşınmaz).
        İşaret \\par'a dayanmayan bir bağımsız değişkenin içindeyse (vurgu,
        bağ metni, başlık, tablo hücresi: _par_safe_marker) katılmaz: \\par
        "\\textit" içinde derleme hatasıdır.
        Dönüş: dipnot kimliği -> PDF sırasıyla devam satırları. PDF yoksa boş
        (dokunulmaz)."""
        if not paired:
            return {}
        idx = self._pdf_footnote_index()
        if not idx:
            return {}
        result: Dict[str, List[int]] = {}
        taken: Set[int] = set()
        norm_cache: Dict[int, str] = {}
        for i, fid in sorted(paired.items()):
            if not self._par_safe_marker(lines, marker_row.get(fid),
                                         self._footnote_marks.index(fid)):
                continue
            loc = self._fn_locate(idx, self._footnote_defs[fid])
            if not loc:
                continue
            pno, k = loc
            stream, sizes, ys = idx["pages"][pno]
            size_d = sizes[k]
            dn = _fn_norm(self._footnote_defs[fid])
            if size_d > idx["body"] - _FN_BODY_GAP or not stream.startswith(dn, k):
                continue
            e = k + len(dn)
            lo = max(0, i - _PAGE_FOOTNOTE_WINDOW)
            cands: Dict[int, str] = {}
            for r in range(lo, min(len(lines), i + _PAGE_FOOTNOTE_WINDOW + 1)):
                if r == i or r in skip or r in taken or r in paired \
                        or not self._fn_cont_candidate(lines, r):
                    continue
                if r not in norm_cache:
                    norm_cache[r] = _fn_norm(lines[r])
                if len(norm_cache[r]) >= _FN_CONT_MIN_CHARS:
                    cands[r] = norm_cache[r]
            chain: List[int] = []
            while e < len(stream):
                nxt = [r for r, t in cands.items()
                       if r not in chain and stream.startswith(t, e)]
                if (len(nxt) != 1 or abs(sizes[e] - size_d) > _FN_SIZE_TOL
                        or ys[e] - ys[e - 1] < _FN_PARA_GAP_RATIO * size_d):
                    break
                chain.append(nxt[0])
                e += len(cands[nxt[0]])
            if chain:
                result[fid] = chain
                taken.update(chain)
        return result

    @staticmethod
    def _code_fence_rows(lines: List[str]) -> Set[int]:
        """Çitli kod bloğunun (```) satırları (çit satırları dahil)."""
        rows: Set[int] = set()
        in_code = False
        for i, line in enumerate(lines):
            if line.strip().startswith("```"):
                rows.add(i)
                in_code = not in_code
            elif in_code:
                rows.add(i)
        return rows

    def _footnote_restart_rows(self, lines: List[str], defs: List[Tuple[int, int]],
                               code_rows: Set[int]) -> Set[int]:
        """Geriye aramanın aşamayacağı "# " başlık satırları (FX1).

        Bölüm başlığı yalnız dipnot numaralandırması orada yeniden başlıyorsa
        (ya da bilinemiyorsa) engeldir: önceki bölümün eşlenmemiş "1"i yeni
        bölümün "1" tanımına eşlenmesin. Başlıktan önceki son rakamlı tanımın
        numarası, başlıktan sonraki ilk rakamlı tanımınkinden küçükse
        numaralar sürüyordur (ör. kaynakta baştan sona kesintisiz; Marker sayfa
        ortasındaki "# " başlığını sayfanın dipnotlarından önce yazar) ve
        başlık aşılır."""
        rows: List[int] = []
        vals: List[int] = []
        for i, k in defs:
            c = self._sup_marks[k].strip()
            if c.isdigit():
                rows.append(i)
                vals.append(int(c))
        barrier: Set[int] = set()
        p = 0                       # rows[p] = başlıktan sonraki ilk tanım
        for h, line in enumerate(lines):
            while p < len(rows) and rows[p] < h:
                p += 1
            if h in code_rows or not re.match(r"^#\s", line):
                continue
            if not (0 < p < len(rows) and vals[p] > vals[p - 1]):
                barrier.add(h)
        return barrier

    def _split_merged_footnote_defs(
            self, lines: List[str], code_rows: Set[int]
    ) -> Tuple[List[str], Dict[int, Tuple[int, str]]]:
        """Marker'ın tek paragrafta birleştirdiği ardışık dipnot tanımlarını
        ("<sup>38</sup> … sonu. <sup>37</sup> Yeni not …") ayrı tanım
        satırlarına böl (V1 yeni hata 3: içteki tanım ilk
        dipnotun İÇİNE giriyor, gövdedeki işareti öksüz kalıyordu).

        Bölme noktası (hepsi): cümle sonu noktalaması (+ kapayıcı) + boşluk +
        rakamlı üst simge + harf/tırnak/ayraç/vurgu ile başlayan metin; numara
        tanımınkinden (zincirde bir öncekinden) farklı ve en çok
        _MERGED_DEF_MAX_GAP uzak (aynı sayfanın dipnotları); o numaranın
        pencere içinde AYRI bir tanımı yok (varsa içteki işaret o dipnota
        atıftır); her parça en az iki harfli sözcük içerir. Dönüş: (yeni
        satırlar, ayrılan parça satırı -> (önceki parça satırı, aradaki
        boşluk)); eşlenemeyen parça _pair_page_footnotes'ta geri katılır."""
        defs_at: Dict[int, "re.Match[str]"] = {}
        for i, line in enumerate(lines):
            if i in code_rows:
                continue
            m = _PAGE_FOOTNOTE_DEF_RE.match(line)
            if m and re.search(r"[^\W\d_]{2}", m.group(2)):
                defs_at[i] = m
        if not defs_at:
            return lines, {}
        standalone: Dict[str, List[int]] = {}
        for i, m in defs_at.items():
            standalone.setdefault(self._sup_marks[int(m.group(1))].strip(), []).append(i)
        out: List[str] = []
        split_of: Dict[int, Tuple[int, str]] = {}
        for i, line in enumerate(lines):
            m = defs_at.get(i)
            parts = self._merged_def_parts(line, m, i, standalone) if m else None
            if not parts:
                out.append(line)
                continue
            out.append(parts[0][1])
            for ws, part in parts[1:]:
                split_of[len(out)] = (len(out) - 1, ws)
                out.append(part)
        return out, split_of

    def _merged_def_parts(self, line: str, m: "re.Match[str]", i: int,
                          standalone: Dict[str, List[int]]
                          ) -> Optional[List[Tuple[str, str]]]:
        """Birleşik tanım satırının parçaları [(önceki boşluk, parça)] ya da
        None (bölme yok; koşullar _split_merged_footnote_defs'te)."""
        prev = self._sup_marks[int(m.group(1))].strip()
        if not prev.isdigit():
            return None
        parts: List[Tuple[str, str]] = []
        start, ws_prev = 0, ""
        for mm in _MERGED_DEF_CUT_RE.finditer(line, m.start(2)):
            num = self._sup_marks[int(mm.group("k"))].strip()
            if (not num.isdigit() or num == prev
                    or abs(int(num) - int(prev)) > _MERGED_DEF_MAX_GAP
                    or any(abs(j - i) <= _PAGE_FOOTNOTE_WINDOW
                           for j in standalone.get(num, ()))):
                continue
            parts.append((ws_prev, line[start:mm.end("end")]))
            ws_prev, start, prev = mm.group("ws"), mm.start("mark"), num
        if not parts:
            return None
        parts.append((ws_prev, line[start:]))
        for _, part in parts:
            pm = _PAGE_FOOTNOTE_DEF_RE.match(part)
            if not pm or not re.search(r"[^\W\d_]{2}", pm.group(2)):
                return None
        return parts

    def _footnote_ref_repl(self, m: "re.Match[str]") -> str:
        fid = m.group(1)
        if fid not in self._footnote_defs:
            logger.warning(
                "Dipnot işareti [^%s] için tanım yok: işaret üst simge olarak "
                "kalıyor", fid)
            return self._sup_mark(fid[len(NOTE_LABEL_PREFIX):])
        self._footnote_marks.append(fid)
        return f"{_MARK_SENTINEL}F{len(self._footnote_marks) - 1}{_MARK_SENTINEL}"

    def _footnote_text(self, fid: str) -> str:
        """Dipnot metni LaTeX'e. Çok paragraflı dipnotun (FX1)
        paragrafları boş satırla ayrılı durur; ``\\par`` ile birleşir (boş
        satır \\footnote bağımsız değişkenini satır temelli araçlarda
        bölerdi)."""
        raw = self._footnote_defs.get(fid, "")
        return "\\par ".join(self._footnote_paragraph_tex(fid, part)
                             for part in raw.split("\n\n"))

    def _footnote_paragraph_tex(self, fid: str, raw: str) -> str:
        raw = _FOOTNOTE_REF_RE.sub(self._footnote_ref_repl, raw)
        tex = _protect_href_urls(self._convert_inline(raw).strip())
        if not _tex_balanced(tex):
            # Biçim kaybı, metin kaybından iyidir: düz (kaçışlı) metin.
            logger.warning("Dipnot %s satır içi biçimi dengesiz: düz metin "
                           "olarak yazıldı", fid)
            tex = _protect_and_escape(raw.strip())
            if not _tex_balanced(tex):
                tex = _escape_tex(raw.strip())
        return tex

    def _resolve_inline_marks(self, body: str) -> str:
        """Nöbetçileri gerçek komutlara çevir (convert() sonu).

        - Kayan ortam (``table``, ``figure``) içindeki dipnot: ``\\footnote``
          kutu içinde metni kaybeder; işaret ``\\footnotemark``, metin
          ortamın hemen ardında ``\\footnotetext``.
        - KARAR 31: PDF dipnotu kaynaktaki numarasıyla basılır
          (``\\footnote[N]``, ``\\footnotemark[N]``/``\\footnotetext[N]``);
          EPUB dipnotu (E1) LaTeX sayacıyla.
        - Numaralı başlıktaki dipnot: içindekilere/üst bilgiye gitmesin diye
          kısa başlık dipnotsuz verilir (``\\section[{kısa}]{uzun}``).
        - Aynı dipnota ikinci işaret: ``\\footref`` (ilk dipnota ``\\label``).
        """
        if _MARK_SENTINEL not in body:
            return body
        self._fn_seen: Set[str] = set()
        counts: Dict[str, int] = {}
        for fid in self._footnote_marks:
            counts[fid] = counts.get(fid, 0) + 1
        self._fn_counts = counts
        body = _FLOAT_ENV_RE.sub(self._table_footnotes, body)
        lines = body.split("\n")
        for k, line in enumerate(lines):
            if _MARK_SENTINEL + "F" not in line:
                continue
            hm = _HEADING_CMD_RE.match(line)
            if hm and not hm.group(2):
                lines[k] = self._heading_footnotes(line, hm)
        body = "\n".join(lines)
        for _ in range(4):   # dipnot metnindeki iç içe işaretler
            if _MARK_SENTINEL not in body:
                break
            body = _MARK_RE.sub(self._mark_repl, body)
        if _MARK_SENTINEL in body:
            logger.warning("Çözülemeyen iç içe dipnot işareti atlandı")
            body = _MARK_RE.sub("", body)
        return body

    def _mark_repl(self, m: "re.Match[str]") -> str:
        kind, n = m.group(1), int(m.group(2))
        if kind == "S":
            return "\\textsuperscript{" + _escape_tex(self._sup_marks[n]) + "}"
        fid = self._footnote_marks[n]
        label = "fn:" + fid
        if fid in self._fn_seen:
            return "\\footref{" + label + "}"
        self._fn_seen.add(fid)
        text = self._footnote_text(fid)
        if self._fn_counts.get(fid, 0) > 1:
            text = "\\label{" + label + "}" + text
        return "\\footnote" + self._footnote_number_opt(fid) + "{" + text + "}"

    def _footnote_number_opt(self, fid: str) -> str:
        """KARAR 31: PDF dipnotunun kaynak numarası "[N]" (yoksa "").

        Yalnız rakamlı işaret: \\footnote[*] geçersizdir (isteğe bağlı
        bağımsız değişken sayı ister); simge işaretli ve EPUB dipnotu
        sayaçla numaralanır."""
        num = getattr(self, "_footnote_nums", {}).get(fid, "")
        return f"[{num}]" if num.isdigit() else ""

    def _table_footnotes(self, m: "re.Match[str]") -> str:
        """Kayan ortamdaki (tablo/şekil) dipnot: işaret \\footnotemark,
        metin ortamın ardında \\footnotetext. Kaynak numaralı dipnot
        (KARAR 31) numarasını taşır ve sayaca dokunmaz; numarasız (EPUB)
        dipnot sayaçla eşlenir (\\addtocounter + \\stepcounter)."""
        block = m.group(0)
        if _MARK_SENTINEL + "F" not in block:
            return block
        fids: List[str] = []

        def _mark(mm: "re.Match[str]") -> str:
            if mm.group(1) != "F":
                return mm.group(0)
            fid = self._footnote_marks[int(mm.group(2))]
            fids.append(fid)
            return "\\footnotemark" + (self._footnote_number_opt(fid) or "{}")

        block = _MARK_RE.sub(_mark, block)
        counted = [fid for fid in fids if not self._footnote_number_opt(fid)]
        tail = ["\\addtocounter{footnote}{-%d}" % len(counted)] if counted else []
        for fid in fids:
            self._fn_seen.add(fid)
            opt = self._footnote_number_opt(fid)
            tail.append(("\\footnotetext" + opt if opt
                         else "\\stepcounter{footnote}\\footnotetext")
                        + "{" + self._footnote_text(fid) + "}")
        return block + "\n" + "\n".join(tail)

    @staticmethod
    def _heading_footnotes(line: str, hm: "re.Match[str]") -> str:
        open_idx = hm.end() - 1
        close = _braced_end(line, open_idx)
        if close < 0:
            return line
        full = line[open_idx + 1:close]
        short = _MARK_RE.sub(
            lambda mm: "" if mm.group(1) == "F" else mm.group(0), full).strip()
        return f"{hm.group(1)}[{{{short}}}]{{{full}}}{line[close + 1:]}"

    # ------------------------------------------------------------------
    # C2 (2026-09-28): sayfa üst bilgisi (R14), şekil altyazısı (R13)
    # ------------------------------------------------------------------

    def _plan_running_headers(self, lines: List[str], start: int) -> None:
        """Sayfa üst bilgisi satırlarını (kitap adı + bölüm adı) atla.

        Marker bazen üst bilgiyi gövdeye paragraf olarak koyar: sent_it
        "*Meccanica delle vibrazioni Oscillazioni libere*" = kitap adı +
        bölüm adı. Kural: tek başına duran (öncesi/sonrası boş) kısa satır,
        sayfa numarası atılınca, kitap adı ile bir başlık adının (etiketsiz
        biçimi de: "Capitolo 1. X" -> "X") art arda yazımıysa (iki sırada da)
        üst bilgidir. Kitap adı bilinmiyorsa (başlık sayfası yok) kural
        çalışmaz.
        """
        # Kitap adı adayları: başlık sayfasından okunan ad ve belgenin ilk
        # 40 satırındaki ilk "# " başlığı (başlık sayfası yazar satırıyla
        # başlıyorsa _book_title yazar adı olabiliyor: sent_it).
        title_keys: Set[str] = set()
        for cand in [self._book_title or ""] + [
                self._clean_heading_title(m.group(1))
                for m in (re.match(r"^#\s+(.*)$", ln) for ln in lines[:40])
                if m][:1]:
            key = _norm_key(cand)
            if len(key) >= 8:
                title_keys.add(key)
        if not title_keys:
            return
        tails: Set[str] = set()
        for line in lines:
            hm = re.match(r"^\s*#{1,6}\s+(.*)$", line)
            if not hm:
                continue
            title = self._clean_heading_title(hm.group(1))
            label = parse_heading_label(title)
            for cand in (title, label.title if label else ""):
                key = _norm_key(cand)
                if len(key) >= 4 and key not in title_keys:
                    tails.add(key)
        if not tails:
            return
        for i in range(start, len(lines)):
            s = lines[i].strip()
            if (not s or len(s) > 200 or i in self._skip_lines
                    or self._BLOCK_START_RE.match(s)
                    or (i > 0 and lines[i - 1].strip())
                    or (i + 1 < len(lines) and lines[i + 1].strip())):
                continue
            key = _norm_key(re.sub(r"^\d{1,4}\s+|\s+\d{1,4}$", "", s))
            for title_key in title_keys:
                if key.startswith(title_key):
                    rest = key[len(title_key):]
                elif key.endswith(title_key):
                    rest = key[:-len(title_key)]
                else:
                    continue
                if rest in tails:
                    logger.debug("Removed running header: %s", s)
                    self._skip_lines.add(i)
                    break

    @staticmethod
    def _is_caption_text(text: str) -> bool:
        """Paragraf bir şekil altyazısı mı ("Рис. 1.3. X", "**Abb. 1.1** X",
        "Figura 1.1: X", OCR'de "Рис 11 X")? Atıfla başlayan gövde cümlesi
        ("Figure 3.4 shows ...", "Abb. 1.2 zeigt ...") değildir: numaradan
        sonra ayraç ya da kapanan vurgu, yoksa büyük harfle başlayan ad gelir.
        """
        if len(text) > FLOAT_CAPTION_MAX_CHARS or _MARK_SENTINEL + "F" in text:
            return False
        m = _FIGURE_CAPTION_RE.match(text)
        if not m:
            return False
        after = m.group("after")
        if not after.strip() or _CAPTION_SEP_RE.match(after):
            return True
        if not after[:1].isspace():
            return False
        first = after.lstrip().lstrip("*_\"'«„“‘(")[:1]
        return first.isupper() or first.isdigit()

    def _plan_figure_captions(self, lines: List[str], start: int) -> None:
        """Görselin bitişiğindeki altyazı paragrafını şeklin içine al (R13).

        Marker altyazıyı ayrı paragraf verir; \\begin{figure} dışında kalan
        altyazı ayrı sayfaya düşüyordu (iki test kitabında 12/20 ve 2/5).
        Altyazı ``\\caption*`` olur: numara kaynaktaki gibi metinde kalır (LaTeX
        ikinci bir "Şekil N:" eklemez, numaralama kaymaz).

        Eşleme blok (boş satırla ayrılan paragraf; görsel satırı tek başına
        blok) düzeyindedir. Belgenin altyazı yönü (görselin ardı/önü)
        yalnız bir yanında altyazı olan görsellerin oyuyla belirlenir;
        eşitlikte "ardı". Sıra: (1) art arda k görsel + aynı yönde tam k
        altyazı sırayla eşlenir (yan yana şekiller: Рис. 1.13/1.14 gibi);
        (2) tercih edilen yandaki bitişik altyazı (art arda görsel dizisinin
        o yandaki ucunda sayı tutmuyorsa belirsiz, eşlenmez); (3) öbür yandaki
        bitişik altyazı, o altyazının öbür komşusu görsel değilse (ör.
        Abb. 1.3 görselden önce). Eşlenmeyen altyazı eskisi gibi paragraf
        kalır.
        """
        self._figure_captions = {}
        blocks: List[list] = []   # [tür, ilk satır, son satır]
        cur: Optional[list] = None
        for i in range(start, len(lines)):
            s = lines[i].strip()
            if not s:
                cur = None
                continue
            if i in self._skip_lines:
                cur = None
                blocks.append(["other", i, i])
                continue
            if _IMAGE_LINE_RE.match(s.replace(r"\_", "_")):
                cur = None
                blocks.append(["image", i, i])
                continue
            if self._BLOCK_START_RE.match(s):
                cur = ["other", i, i]
                blocks.append(cur)
            elif cur is None:
                cur = ["text", i, i]
                blocks.append(cur)
            else:
                cur[2] = i
        texts: Dict[int, str] = {}
        for p, b in enumerate(blocks):
            if b[0] != "text":
                continue
            t = " ".join(lines[k].strip() for k in range(b[1], b[2] + 1))
            if self._is_caption_text(t) and _tex_balanced(self._convert_inline(t)):
                b[0] = "caption"
                texts[p] = t
        imgs = [p for p, b in enumerate(blocks) if b[0] == "image"]
        if not imgs or not texts:
            return

        def kind(p: int) -> Optional[str]:
            return blocks[p][0] if 0 <= p < len(blocks) else None

        votes = {"after": 0, "before": 0}
        for p in imgs:
            a, b = kind(p + 1) == "caption", kind(p - 1) == "caption"
            if a != b:
                votes["after" if a else "before"] += 1
        side = "before" if votes["before"] > votes["after"] else "after"
        other = "before" if side == "after" else "after"
        step = 1 if side == "after" else -1

        runs: List[List[int]] = []
        for p in imgs:
            if runs and runs[-1][-1] == p - 1:
                runs[-1].append(p)
            else:
                runs.append([p])
        run_of = {p: run for run in runs for p in run}
        pairs: Dict[int, Tuple[int, str]] = {}
        claimed: Set[int] = set()
        # (1) görsel dizisi + aynı sayıda altyazı dizisi
        for run in runs:
            k = len(run)
            if k < 2:
                continue
            if side == "after":
                caps = list(range(run[-1] + 1, run[-1] + 1 + k))
                beyond = run[-1] + 1 + k
            else:
                caps = list(range(run[0] - k, run[0]))
                beyond = run[0] - k - 1
            if (all(kind(c) == "caption" for c in caps)
                    and kind(beyond) != "caption"):
                for p, c in zip(run, caps):
                    pairs[p] = (c, side)
                    claimed.add(c)
        # (2) tercih edilen yan
        for p in imgs:
            if p in pairs:
                continue
            c = p + step
            run = run_of[p]
            at_run_end = len(run) > 1 and p == (run[-1] if step > 0 else run[0])
            if kind(c) == "caption" and c not in claimed and not at_run_end:
                pairs[p] = (c, side)
                claimed.add(c)
        # (3) öbür yan
        for p in imgs:
            if p in pairs:
                continue
            c = p - step
            if (kind(c) == "caption" and c not in claimed
                    and kind(c - step) != "image"):
                pairs[p] = (c, other)
                claimed.add(c)
        # (4) dizinin ucundaki görsel: dizinin öbür görselleri altyazısını
        # aldıysa uçtaki altyazı artık belirsiz değildir (ör. Fig. 2.21
        # görselden önce, Fig. 2.22 sonraki görselin ardında).
        for p in imgs:
            c = p + step
            if (p not in pairs and kind(c) == "caption" and c not in claimed
                    and all(q in pairs for q in run_of[p] if q != p)):
                pairs[p] = (c, side)
                claimed.add(c)

        for p, (c, where) in pairs.items():
            b = blocks[c]
            self._figure_captions[blocks[p][1]] = (texts[c], where)
            self._skip_lines.update(range(b[1], b[2] + 1))

    # ------------------------------------------------------------------
    # Italic concatenation fix (OCR artifact)
    # ------------------------------------------------------------------

    # Regex to find italic spans in Markdown: *text* or _text_
    # Excludes ** (bold) and __ (bold) via negative lookahead/lookbehind.
    _ITALIC_STAR_RE = re.compile(
        r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", re.DOTALL
    )
    _ITALIC_UNDER_RE = re.compile(
        r"(?<!\w)_([^_]+)_(?!\w)", re.DOTALL
    )

    def _fix_italic_concatenation(self, text: str) -> str:
        """Fix missing spaces inside italic blocks from Marker OCR.

        Marker OCR sometimes strips all spaces inside italic spans, producing
        text like ``*Burada,Fkuvvettir,kyaysabitidirvexyerdeitirmedir.*``

        Strategy:
        1. Stash inline math ``$...$`` so formulas are never touched.
        2. Find italic spans (``*...*`` or ``_..._``) with 30+ chars and
           no spaces -- these are almost certainly concatenated.
        3. Insert spaces at:
           a) camelCase boundaries: ``(?<=[a-z])(?=[A-Z])``
           b) after comma + letter: ``(?<=,)(?=[A-Za-z])``
           c) after period + uppercase: ``(?<=\\.)(?=[A-Z])``
        4. Restore math stash.

        Returns the (possibly fixed) text.
        """
        # Quick bail: no italic markers at all
        if "*" not in text and "_" not in text:
            return text

        # Stash inline math to protect formulas
        math_stash: list = []

        def _stash_math(m: re.Match) -> str:
            idx = len(math_stash)
            math_stash.append(m.group(0))
            return f"<<ITAL_MATH_{idx}>>"

        work = re.sub(r"\$\$.*?\$\$", _stash_math, text, flags=re.DOTALL)
        work = re.sub(r"(?<!\$)\$(?!\$)(.+?)(?<!\$)\$(?!\$)", _stash_math, work)

        def _fix_concat_inner(content: str) -> str:
            """Insert spaces in a concatenated italic content string."""
            # Only fix if 30+ chars and contains no space
            if len(content) < 30 or " " in content:
                return content
            # a) camelCase: lowercase followed by uppercase
            fixed = re.sub(r"(?<=[a-z\u00e7\u011f\u0131\u00f6\u015f\u00fc])(?=[A-Z\u00c7\u011e\u0130\u00d6\u015e\u00dc])", " ", content)
            # b) comma + letter (no space)
            fixed = re.sub(r"(?<=,)(?=[A-Za-z\u00c7\u011e\u0130\u00d6\u015e\u00dc\u00e7\u011f\u0131\u00f6\u015f\u00fc])", " ", fixed)
            # c) period + uppercase (sentence boundary)
            fixed = re.sub(r"(?<=\.)(?=[A-Z\u00c7\u011e\u0130\u00d6\u015e\u00dc])", " ", fixed)
            if fixed != content:
                logger.debug(
                    "Fixed italic concatenation: '%s' -> '%s'",
                    content[:60], fixed[:60],
                )
            return fixed

        def _fix_star(m: re.Match) -> str:
            inner = m.group(1)
            return f"*{_fix_concat_inner(inner)}*"

        def _fix_under(m: re.Match) -> str:
            inner = m.group(1)
            return f"_{_fix_concat_inner(inner)}_"

        work = self._ITALIC_STAR_RE.sub(_fix_star, work)
        work = self._ITALIC_UNDER_RE.sub(_fix_under, work)

        # Restore math stash
        for idx in range(len(math_stash) - 1, -1, -1):
            work = work.replace(f"<<ITAL_MATH_{idx}>>", math_stash[idx])

        return work

    # ------------------------------------------------------------------
    # Front matter helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_front_matter_heading(title: str) -> bool:
        """Check if heading title is a front matter section name."""
        clean = title.strip().lower()
        return clean in _FRONT_MATTER_NAMES

    def _maybe_transition_to_mainmatter(
        self, clean_title: str, level: int, line_idx: Optional[int] = None,
    ) -> str:
        """
        If this heading signals the start of real content (not front matter),
        return the LaTeX transition commands (tableofcontents + mainmatter).

        2026-09-25 (Y1): yapı planı bölüm/kısım çapası bulduysa geçiş YALNIZ
        ilk çapada olur (*line_idx* ile); öncesindeki etiketsiz başlıklar
        (kitap adı, yazar adı, künye, önsöz) ön maddede numarasız kalır.
        """
        if not self._in_frontmatter or self._frontmatter_ended:
            return ""

        anchor_idx = getattr(self, "_first_anchor_idx", None)
        if anchor_idx is not None:
            # ">=": çapa satırı bitişik tekrar diye elenirse geçiş ondan
            # sonraki ilk başlıkta yapılır (belge ön maddede takılı kalmaz).
            if line_idx is None or line_idx < anchor_idx:
                return ""
            self._in_frontmatter = False
            self._frontmatter_ended = True
            logger.info("Front matter -> main matter transition at first "
                        "chapter/part anchor: %s", clean_title)
            return "\\tableofcontents\n\n\\mainmatter\n\n"

        # 2026-10-05 (G1a-1): başlıksız belgede plan geçişi öndeki
        # bölüm düzeyli başlığa taşıdıysa (bkz. _plan_titleless_transition)
        # geçiş o satırdadır; düzey koşulu aranmaz.
        tidx = getattr(self, "_transition_idx", None)
        if tidx is not None:
            if line_idx is None or line_idx < tidx:
                return ""
            self._in_frontmatter = False
            self._frontmatter_ended = True
            logger.info("Front matter -> main matter transition at first "
                        "chapter-size heading of an untitled document: %s",
                        clean_title)
            return "\\tableofcontents\n\n\\mainmatter\n\n"

        # Only # (chapter) level triggers transition
        if level != 1:
            return ""

        # Book title repeats are still front matter
        if clean_title == self._book_title:
            return ""

        # Known front matter names stay in front matter
        if self._is_front_matter_heading(clean_title):
            return ""

        # This is a real chapter -- transition!
        self._in_frontmatter = False
        self._frontmatter_ended = True
        logger.info("Front matter -> main matter transition at: %s", clean_title)
        return "\\tableofcontents\n\n\\mainmatter\n\n"

    # ------------------------------------------------------------------
    # Block-level consumers (return LaTeX string + number of lines eaten)
    # ------------------------------------------------------------------

    # Regex for standalone equation numbers like "(2.1)" or "(2.4a)"
    _EQ_NUM_RE = re.compile(r"^\s*\((\d+(?:\.\d+)*[a-zA-Z]?)\)\s*$")

    # R16: display matematiğin sonundaki düz metin denklem numarası. Önünde
    # en az bir boşluk/aralık komutu olmalı ("f(2)" işlev uygulamasıdır).
    _EQ_NUM_IN_MATH_RE = re.compile(
        r"(?P<sp>(?:\s|~|\\q?quad\b|\\hfill\b|\\[,;:!])+)"
        r"\((?P<num>\d+(?:\.\d+)*[a-zA-Z]?)\)\s*$")
    _EQ_NUM_OPERATOR_END_RE = re.compile(
        r"(?:[+\-=*/^_(<>|&]|\\(?:cdot|times|div|pm|mp|left))\s*$")

    def _split_inner_eq_number(self, inner: str) -> tuple:
        """``inner``'ın sonundaki "(1.5)" denklem numarasını ayır.

        Döner: ``(numarasız_içerik, numara)``; numara değilse
        ``(inner, None)``. Noktalı numara ("1.5") işleçten sonra gelmedikçe
        numaradır ("2 \\cdot (1.5)" çarpımdır). Noktasız numara ("(7)")
        yalnız ``\\quad``/``\\qquad``/``\\hfill`` ya da noktalama
        (".", ",", ";") ardından numara sayılır. Çok satırlı içerik (ortam
        dışı ``\\\\``) olduğu gibi kalır.
        """
        m = self._EQ_NUM_IN_MATH_RE.search(inner)
        if not m:
            return inner, None
        head = inner[:m.start()].rstrip()
        if not head:
            return inner, None
        if "\\\\" in head and "\\begin{" not in head:
            return inner, None
        num = m.group("num")
        if "." in num:
            if self._EQ_NUM_OPERATOR_END_RE.search(head):
                return inner, None
        elif not (re.search(r"\\(?:q?quad|hfill)", m.group("sp"))
                  or head[-1] in ".,;"):
            return inner, None
        return head, num

    def _consume_display_math(self, lines: List[str], start: int) -> tuple[str, int]:
        """
        Consume a $$ … $$ block.  The math content is left UNTOUCHED,
        except a plain-text equation number at its very end ("... (1.5)$$",
        R16) which becomes \\tag{} (see _split_inner_eq_number).

        Also peeks ahead for standalone equation numbers like "(2.1)" on
        the next non-blank line and absorbs them as \\tag{}.
        """
        first = lines[start].strip()
        trailing_text = ""  # kapanış $$'dan sonra aynı satırda kalan metin
        if first.startswith("$$") and first.endswith("$$") and len(first) > 4:
            inner = first[2:-2].strip()
            j = start + 1
        else:
            # Use slicing [2:] not lstrip("$") to avoid over-stripping
            collected = [first[2:].strip() if first.startswith("$$") else first]
            j = start + 1
            closed = False
            # Kapanmamış $$ guard'ı: kapanış hiç gelmezse eski kod dosyanın
            # SONUNA kadar her şeyi (başlıklar dahil) math içine yutuyordu.
            # Yeni kural: başlık satırı ya da makul satır sınırı aşımı ->
            # blok kapanmamış kabul edilir ve ilk satır düz metne geri döner.
            _MAX_MATH_LINES = 30
            while j < len(lines):
                ln = lines[j]
                ln_stripped = ln.strip()
                if "$$" in ln_stripped:
                    before, _, after = ln_stripped.partition("$$")
                    collected.append(before.strip())
                    trailing_text = after.strip()
                    j += 1
                    closed = True
                    break
                if ln_stripped.startswith("#") or (j - start) > _MAX_MATH_LINES:
                    break  # başlığa çarptık / sınır aşıldı: kapanış yok
                collected.append(ln)
                j += 1
            if not closed:
                logger.warning(
                    "Unclosed $$ at line %d: treating as plain text "
                    "(not swallowing the rest of the document)", start + 1,
                )
                return _protect_and_escape(first), 1
            inner = "\n".join(collected).strip()

        # F1-05: uzunluk olmayan "\\ [" satır aralığı sanılmasın
        # ("\\ [0, 1, 0]^{\top}" -> "Illegal unit of measure").
        inner = _fix_math_linebreak_bracket(inner)

        # R16 (2026-09-28): numara matematiğin İÇİNDE, sonda düz metin
        # ("$$m\ddot{x} + kx = 0. (1.1)$$", "... \qquad (1.2)$$"): \tag'e
        # alınır, yoksa satır içinde basılıp sağa yaslanmıyordu (sentetik
        # fr/es/it ve bir Rusça test kitabı).
        eq_num = None
        if r"\tag{" not in inner:
            inner, eq_num = self._split_inner_eq_number(inner)

        # Kapanış $$ ile aynı satırda kalan kuyruk: "(2.3)" ise denklem
        # numarasıdır; başka metinse math'ten SONRA düz metin olarak korunur.
        tail_latex = ""
        if trailing_text:
            m_tail = self._EQ_NUM_RE.match(trailing_text)
            if m_tail and eq_num is None:
                eq_num = m_tail.group(1)
            else:
                tail_latex = "\n\n" + _protect_and_escape(trailing_text)

        # Peek ahead: search up to 3 lines for standalone equation number "(N.N)"
        # Marker sometimes puts 1-2 blank lines between $$ and the eq number.
        peek = j
        lines_checked = 0
        while eq_num is None and peek < len(lines) and lines_checked < 3:
            stripped_peek = lines[peek].strip()
            if not stripped_peek:
                # Blank line -- skip but count toward the 3-line window
                peek += 1
                lines_checked += 1
                continue
            m = self._EQ_NUM_RE.match(stripped_peek)
            if m:
                eq_num = m.group(1)
                j = peek + 1  # consume the equation number line
            break  # Stop at first non-blank line (whether matched or not)

        # Check if \tag{} is already inside the math
        has_tag = r"\tag{" in inner

        # Use equation* environment when we have an equation number
        if eq_num or has_tag:
            if eq_num and not has_tag:
                inner += f"\n\\tag{{{eq_num}}}"
            return (f"\\begin{{equation*}}\n{inner}\n\\end{{equation*}}"
                    f"{tail_latex}", j - start)
        else:
            return f"\\[\n{inner}\n\\]{tail_latex}", j - start

    def _consume_code_block(self, lines: List[str], start: int,
                            output: Optional[List[str]] = None,
                            ) -> tuple[Optional[str], int]:
        """
        Consume a fenced code block (```lang ... ```).

        2026-10-06: kaynak PDF varken dil seçeneksiz DÜZYAZI çiti
        (sanitizer.listing_is_prose) lstlisting olmaz: kod_vurgu doğrudan
        sanitizer'ın alıntı biçimini basar, PDF'te kalın/italik dizilmiş
        koşular \\textbf{} / \\textit{} olur (Verbatim'de vurgu yoktu). Kutu
        önceki paragrafın yarım cümlesini sürdürüyorsa metin o paragrafa
        eklenir ve None döner (*output* yerinde değişir). Gerçek kod, dil
        seçenekli çit ve PDF'siz kaynak (EPUB) eskisi gibi lstlisting'dir
        (düzyazı kutuyu derlemede sanitizer çevirir).
        """
        first = lines[start].strip()
        lang = first.lstrip("`").strip() or ""
        collected: List[str] = []
        j = start + 1
        while j < len(lines):
            if lines[j].strip().startswith("```"):
                j += 1
                break
            collected.append(lines[j])
            j += 1

        code = "\n".join(collected)
        if not lang and output is not None and self._kaynak_pdf_var():
            try:
                sonuc = kod_vurgu.duzyazi_kod_blogu(
                    code, self.config.input_pdf, _code_block_preceding(output),
                    self.kod_vurgu_raporu)
            except Exception as exc:  # noqa: BLE001 - vurgu dönüşümü durdurmaz
                logger.warning("Kod çiti vurgusu atlandı (lstlisting kalır): %s", exc)
                sonuc = None
            if sonuc is not None:
                latex, bagla = sonuc
                if not bagla:
                    return latex, j - start
                _join_to_previous_paragraph(output, latex)
                return None, j - start
        lang_opt = f"[language={lang}]" if lang else ""
        return f"\\begin{{lstlisting}}{lang_opt}\n{code}\n\\end{{lstlisting}}", j - start

    def _consume_table(self, lines: List[str], start: int) -> tuple[str, int]:
        """
        Consume a Markdown pipe table and convert to booktabs.
        """
        table_lines: List[str] = []
        j = start
        while j < len(lines) and lines[j].strip().startswith("|"):
            table_lines.append(lines[j].strip())
            j += 1

        if len(table_lines) < 2:
            # Not a real table, treat as text
            return _protect_and_escape(lines[start]), 1

        # Parse header
        header_cells = self._parse_table_row(table_lines[0])
        ncols = len(header_cells)

        # Skip separator row (|---|---|)
        data_start = 1
        if data_start < len(table_lines) and re.match(r"^\|[\s\-:|]+\|$", table_lines[data_start]):
            data_start = 2

        # Parse data rows
        data_rows: List[List[str]] = []
        for row_line in table_lines[data_start:]:
            cells = self._parse_table_row(row_line)
            data_rows.append(cells)

        # Build LaTeX
        col_spec = " ".join(["l"] * ncols)
        parts = [
            f"\\begin{{table}}[H]",
            f"\\centering",
            f"\\begin{{tabular}}{{{col_spec}}}",
            "\\toprule",
        ]

        # F1-05: hücre matematiğindeki ortam dışı "\\" tabular satırını
        # bitirmesin (bkz. _wrap_cell_math_linebreaks).
        # V1 denetimi (2026-09-29, F1-01 tablo sözleşmesi): EPUB tablosunun
        # başlık satırı yoksa ayrıştırıcı BOŞ başlık satırı yazar; boş satır
        # ve altındaki \midrule basılmaz (kaynakta başlık yok).
        if any(c.strip() for c in header_cells):
            header_escaped = [self._table_cell_tex(c) for c in header_cells]
            parts.append(" & ".join(header_escaped) + " \\\\")
            parts.append("\\midrule")

        for row in data_rows:
            while len(row) < ncols:
                row.append("")
            row_escaped = [self._table_cell_tex(c) for c in row[:ncols]]
            parts.append(" & ".join(row_escaped) + " \\\\")

        parts.append("\\bottomrule")
        parts.append("\\end{tabular}")
        parts.append("\\end{table}")

        return "\n".join(parts), j - start

    def _table_cell_tex(self, cell: str) -> str:
        """Tablo hücresinin TeX'i.

        V1 denetimi (2026-09-29, F1-01 tablo sözleşmesi): EPUB tabloları
        artık markdown tablosu; hücre satır içi biçim taşır ("*italik*",
        "**kalın**", "[bağ](adres)"). Eskiden her hücre yalnız TeX
        kaçışından geçiyordu: yıldızlar düz basılıyor, bağ "[metin](adres)"
        kalıp 2_derle sanitizer'ının bağ kalıntısı temizliğinde adresi
        siliniyordu. Biçim işareti taşıyan hücre satır içi dönüşümden geçer;
        öteki hücreler (Marker tablolarının hemen hepsi) eskisi gibi.
        """
        cell = _wrap_cell_math_linebreaks(cell)
        plain = _INLINE_MATH_RE.sub(" ", _DISPLAY_MATH_RE.sub(" ", cell))
        if _TABLE_CELL_MARKUP_RE.search(plain):
            return self._convert_inline(cell)
        return _protect_and_escape(cell)

    @staticmethod
    def _parse_table_row(row: str) -> List[str]:
        """Split a markdown table row into cells."""
        row = row.strip().strip("|")
        return [cell.strip() for cell in row.split("|")]

    # 2026-09-25 (R12): liste yapısı.
    #  - Madde metni tembel devam satırlarını da alır (CommonMark): EPUB
    #    paragrafı "1. *Başlık\nsürer.*— metin…" satırlara bölünmüş gelir;
    #    eskiden yalnız ilk satır \item oluyor, kalanı listenin DIŞINA düşüp
    #    italik "*…*" literal kalıyor, cümle \end{enumerate} ile bölünüyordu.
    #  - Boş satırla ayrılmış ardışık numaralı maddeler (1., 2., 3.) TEK
    #    liste olur (eskiden her biri tek \item'lı ayrı enumerate: "1., 1.,
    #    1."; bir İspanyolca EPUB'da).
    #  - "- 1. …", "- 2. …" (Marker, numaralı listeyi madde imli verir) 1'den
    #    başlayıp kesintisiz artıyorsa numaralı listedir ("• 1." çift im).
    #  - Madde başındaki ikinci im ("- --- …", "- $\bullet$ …") atılır
    #    ("• —", "• •" çift im; Rus dizgisinde madde imi tiredir).
    #  C (2026-10-06, V1 bulgusu 8): numaralı liste 1'den başlamıyorsa (araya
    #  denklem/paragraf/şekil girmiş: "1." … şekil … "2.", "3.") enumerate
    #  kaynak numarasıyla başlar: \begin{enumerate}[start=N], seçenek \begin
    #  ile AYNI satırda (enumitem; şablonda yüklü). Eskiden numara verilmiyor,
    #  liste LaTeX'te yeniden 1'den sayıyordu (korpusta iki kitapta 28
    #  madde). Bu biçim seçildi, çünkü:
    #    - EPUB hizalaması (epub_output._tex_units) \begin{…} ardındaki […]
    #      seçeneğini zaten atar; \setcounter{enumi}{N} ise ayrı satırda da,
    #      \begin ile aynı satırda da metin birimi olarak hizalamaya sızar.
    #    - Yerel model çevirmeni liste ortamı satırını seçeneğiyle birlikte
    #      yer tutucuya alır (translator._LIST_ENV_RE); seçenek modele gitmez.
    #  Madde metni değişmez; yalnız numara LaTeX sayacına taşınır. 1'den
    #  başlayan listenin çıktısı bayt bayt aynıdır. "- N." (madde imli
    #  numaralı) liste 1'den başlamıyorsa eski davranış sürer (madde imli,
    #  numara metinde; numara zaten kaynaktaki gibi basılır).
    _OL_ITEM_RE = re.compile(r"^\s*(\d+)\.\s+(.*)")
    _UL_ITEM_RE = re.compile(r"^\s*[-*]\s+(.*)")
    _ITEM_NUMBER_RE = re.compile(r"^(\d+)\.\s+")
    _BULLET_GLYPH_RE = re.compile(
        r"^(?:-{2,3}|[—–•◦▪■●·]|\$\\(?:bullet|circ|cdot)\$)\s+")
    # Madde metnini bitiren (devam satırı OLMAYAN) blok başlangıçları.
    _BLOCK_START_RE = re.compile(
        r"^(?:#{1,6}\s|\||\$\$|\\\[|```|!\[|[-*+]\s|\d+\.\s|>|<!--"
        r"|[-*_]{3,}\s*$)")

    # Belge satır kırpılmış mı (EPUB/Gutenberg: paragraf birden çok satır)?
    # Düz metin satırlarının bu oranı ardından yine düz metin satırıyla
    # gelir. Ölçüm (25 Eyl): EPUB kesitleri 0,83-0,94; Marker çıktıları
    # 0-0,25 (Marker her paragrafı tek satır yazar).
    _HARD_WRAP_RATIO = 0.5

    def _is_hard_wrapped(self, lines: List[str]) -> bool:
        plain = [i for i, ln in enumerate(lines)
                 if ln.strip() and not self._BLOCK_START_RE.match(ln.strip())]
        if not plain:
            return False
        plain_set = set(plain)
        cont = sum(1 for i in plain if i + 1 in plain_set)
        return cont / len(plain) >= self._HARD_WRAP_RATIO

    def _item_text(self, lines: List[str], j: int, first: str) -> tuple[str, int]:
        """*j* satırındaki maddenin metni (devam satırlarıyla) + sonraki satır.

        Satır kırpılmış belgede (EPUB) her dolu satır maddenin devamıdır
        (blok başlangıcına kadar). Marker çıktısında paragraf tek satırdır;
        orada yalnız girintili satır (Marker maddenin devamını girintiyle
        yazar) ya da küçük harfle başlayan satır devam sayılır (sayfa/sütun
        kırılmasında bölünmüş madde, sentetik: "...the check must run on" +
        "every single part"); girintisiz, büyük harfle başlayan satır yeni paragraf
        ya da altyazı olabilir ("Fig. 2.1 …", "The following …") ve listeye
        katılmaz.
        """
        wrapped = getattr(self, "_hard_wrapped", False)
        parts = [first]
        k = j + 1
        while k < len(lines):
            s = lines[k].strip()
            if not s or self._BLOCK_START_RE.match(s):
                break
            if not wrapped:
                indented = len(lines[k]) - len(lines[k].lstrip(" \t")) >= 2
                bare = re.sub(r"<[^>]+>", "", s)
                if not (indented or (bare[:1].isalpha() and bare[:1].islower())):
                    break
                if parts[-1].rstrip().endswith("$$"):
                    break
            parts.append(s)
            k += 1
        if len(parts) == 1:
            return first, k   # tek satır: metin eskisi gibi (sondaki boşluk dahil)
        return " ".join([parts[0].rstrip()] + parts[1:]), k

    def _next_item_after_blank(self, lines: List[str], j: int,
                               item_re: "re.Pattern") -> Optional[tuple]:
        """*j*'den başlayan boş satırların ardındaki madde: (satır, eşleşme)."""
        k = j
        while k < len(lines) and not lines[k].strip():
            k += 1
        if k == j or k >= len(lines):
            return None
        m = item_re.match(lines[k])
        return (k, m) if m else None

    @staticmethod
    def _list_block(env: str, items: List[str], start: int = 1) -> str:
        # start: enumerate'in ilk madde numarası (kaynaktaki numara). 1 ise
        # seçenek yazılmaz (eski çıktı); değilse enumitem [start=N] aynı
        # satırda (bkz. _OL_ITEM_RE üstündeki açıklama).
        option = f"[start={start}]" if env == "enumerate" and start != 1 else ""
        parts = [f"\\begin{{{env}}}{option}"]
        for item in items:
            parts.append(f"  \\item {item}")
        parts.append(f"\\end{{{env}}}")
        return "\n".join(parts)

    # G1b-2 (2026-10-05): iç içe liste. Marker alt
    # maddeyi girintiyle verir ("  - …"); eskiden _UL_ITEM_RE/_OL_ITEM_RE
    # girintiyi yok sayıyor, alt maddeler üst maddenin kardeşi oluyordu
    # (düzleşme). Listenin ilk maddesinden daha girintili madde (ve onun daha
    # girintili devam satırları) bir önceki maddenin içinde iç liste olarak
    # basılır. LaTeX liste derinliği sınırı aşılmaz: _LIST_NEST_MAX düzeyde
    # eski düz davranış sürer.
    _LIST_NEST_MAX = 4

    @staticmethod
    def _indent_width(line: str) -> int:
        return len(line) - len(line.lstrip(" \t"))

    def _nested_at(self, lines: List[str], j: int, base: int) -> bool:
        """*j* satırı *base*'ten daha girintili bir madde mi (iç liste başı)?"""
        return (getattr(self, "_list_depth", 0) + 1 < self._LIST_NEST_MAX
                and self._indent_width(lines[j]) > base
                and bool(self._UL_ITEM_RE.match(lines[j])
                         or self._OL_ITEM_RE.match(lines[j])))

    def _nested_list_end(self, lines: List[str], j: int, base: int) -> int:
        """*j*'de başlayan iç listenin bittiği satır (dahil değil): *base*'ten
        daha girintili madde ve devam satırları; boş satırda, daha az
        girintili satırda ya da madde dışı blok başlangıcında biter."""
        k = j
        while k < len(lines):
            s = lines[k]
            if not s.strip() or self._indent_width(s) <= base:
                break
            if (self._BLOCK_START_RE.match(s.strip())
                    and not (self._UL_ITEM_RE.match(s) or self._OL_ITEM_RE.match(s))):
                break
            k += 1
        return k

    def _render_nested_list(self, lines: List[str]) -> str:
        """İç listenin satırlarını (bir ya da ardışık birkaç liste) çevir."""
        out: List[str] = []
        self._list_depth = getattr(self, "_list_depth", 0) + 1
        try:
            i = 0
            while i < len(lines):
                if self._OL_ITEM_RE.match(lines[i]):
                    block, n = self._consume_ordered_list(lines, i)
                elif self._UL_ITEM_RE.match(lines[i]):
                    block, n = self._consume_unordered_list(lines, i)
                else:
                    block, n = self._convert_inline(lines[i].strip()), 1
                out.append(block)
                i += max(n, 1)
        finally:
            self._list_depth -= 1
        return "\n".join(out)

    @staticmethod
    def _with_nested(items: List[str], nested: List[str]) -> List[str]:
        return [it + ("\n" + inner if inner else "") for it, inner in zip(items, nested)]

    def _consume_unordered_list(self, lines: List[str], start: int) -> tuple[str, int]:
        """Consume contiguous unordered list items."""
        raws: List[str] = []
        nested: List[str] = []
        numbers: List[Optional[int]] = []
        base = self._indent_width(lines[start])
        j = start
        while j < len(lines):
            if raws and self._nested_at(lines, j, base):
                k = self._nested_list_end(lines, j, base)
                inner = self._render_nested_list(lines[j:k])
                nested[-1] = (nested[-1] + "\n" + inner) if nested[-1] else inner
                j = k
                continue
            m = self._UL_ITEM_RE.match(lines[j])
            if not m:
                # "- 1." numaralı liste boş satırla bölünmüşse devam et.
                nxt = self._next_item_after_blank(lines, j, self._UL_ITEM_RE)
                if (nxt is not None and numbers and numbers[0] == 1
                        and all(n is not None for n in numbers)):
                    num_m = self._ITEM_NUMBER_RE.match(nxt[1].group(1).strip())
                    if num_m and int(num_m.group(1)) == numbers[-1] + 1:
                        j = nxt[0]
                        continue
                break
            text, j = self._item_text(lines, j, m.group(1))
            raws.append(text)
            nested.append("")
            num_m = self._ITEM_NUMBER_RE.match(text)
            numbers.append(int(num_m.group(1)) if num_m else None)

        if (numbers and numbers[0] == 1
                and all(n is not None for n in numbers)
                and all(b == a + 1 for a, b in zip(numbers, numbers[1:]))):
            items = [self._convert_inline(self._ITEM_NUMBER_RE.sub("", r, count=1))
                     for r in raws]
            return (self._list_block("enumerate", self._with_nested(items, nested)),
                    j - start)

        items = [self._convert_inline(self._BULLET_GLYPH_RE.sub("", r, count=1))
                 for r in raws]
        return self._list_block("itemize", self._with_nested(items, nested)), j - start

    def _consume_ordered_list(self, lines: List[str], start: int) -> tuple[str, int]:
        """Consume contiguous ordered list items."""
        items: List[str] = []
        nested: List[str] = []
        last_num: Optional[int] = None
        first_num: Optional[int] = None
        base = self._indent_width(lines[start])
        j = start
        while j < len(lines):
            if items and self._nested_at(lines, j, base):
                k = self._nested_list_end(lines, j, base)
                inner = self._render_nested_list(lines[j:k])
                nested[-1] = (nested[-1] + "\n" + inner) if nested[-1] else inner
                j = k
                continue
            m = self._OL_ITEM_RE.match(lines[j])
            if not m:
                # Boş satırla ayrılmış bir sonraki numara (N+1.) aynı listedir.
                nxt = self._next_item_after_blank(lines, j, self._OL_ITEM_RE)
                if (nxt is not None and last_num is not None
                        and int(nxt[1].group(1)) == last_num + 1):
                    j = nxt[0]
                    continue
                break
            text, j = self._item_text(lines, j, m.group(2))
            items.append(self._convert_inline(text))
            nested.append("")
            last_num = int(m.group(1))
            if first_num is None:
                first_num = last_num

        return (self._list_block("enumerate", self._with_nested(items, nested),
                                 start=first_num if first_num is not None else 1),
                j - start)

    # ------------------------------------------------------------------
    # Inline conversions
    # ------------------------------------------------------------------

    # Regex to detect section numbers like "2", "2.2", "2.2.3", "A.1"
    _SECTION_NUM_RE = re.compile(r"^(\d+(?:\.\d+)*|[A-Z](?:\.\d+)*)\s+(.+)")
    # 2026-09-24: paragraf işaretli numara "§ 1.3. Başlık" (Rusça/Almanca ders
    # kitapları); numaranın ardındaki nokta isteğe bağlı. Taranmış Rusça kitapta
    # Marker başlık düzeylerini sayfadan sayfaya rastgele veriyor (§ 1.2 ###,
    # § 1.3 #): numara tanınmazsa § 1.3 \chapter oluyordu.
    _PARAGRAPH_SIGN_NUM_RE = re.compile(r"^§\s*(\d+(?:\.\d+)*)\.?\s+(.+)")
    # 2026-09-25 (R10): Rus (ve Alman) dizgisinde numaranın ardında nokta
    # olur: "2.1. Течение газа в сопле", "1.2.1. Составляющие тяги".
    # _SECTION_NUM_RE sondaki noktayı kabul etmediği için başlık Marker'ın
    # "####" düzeyine düşüyor, numarasız \subsubsection olup sonraki bölüm
    # numaralarını kaydırıyordu (sent_ru: 2.2 -> "2.1"). Nokta YALNIZ çok
    # parçalı numarada kabul edilir: "1. Einleitung" / "A. Smith" /
    # "I. Titre" liste maddesi, harfli ya da Roma rakamlı başlık olabilir,
    # eski davranışları değişmez.
    _SECTION_NUM_DOT_RE = re.compile(r"^((?:\d+|[A-Z])(?:\.\d+)+)\.\s+(.+)")

    def _section_number(self, title: str) -> Optional[str]:
        """Başlığın başındaki bölüm numarası ("2.1", "A.1", "2.1." → "2.1",
        "§ 1.3." → "1.3"); yoksa None."""
        m = (self._SECTION_NUM_RE.match(title)
             or self._SECTION_NUM_DOT_RE.match(title)
             or self._PARAGRAPH_SIGN_NUM_RE.match(title))
        return m.group(1) if m else None

    # 2026-09-24: yalın bölüm ETİKETİ — "Chapter 2", "Bölüm 2", "Chapter II"
    # (başlık metni yok). Marker bölüm numarasını ve bölüm adını ayrı başlık
    # satırları olarak verebilir; bkz. _try_heading.
    # 2026-09-25 (Y1): etiket tanıma modül düzeyindeki parse_heading_label'a
    # taşındı (altı dil, sıra sözcükleri, ters sıra, kısım etiketleri).

    @staticmethod
    def _bare_chapter_label(title: str) -> Optional[_Label]:
        """Başlık yalın bölüm etiketi mi ("Chapter 2", "CAPÍTULO VII")?"""
        label = parse_heading_label(title)
        if label is not None and label.kind == "chapter" and not label.title:
            return label
        return None

    # 2026-09-24 (denetim Mercek 1 #3): numaralı soru/alıştırma/örnek başlığı
    # ("Problem 1.3", "Übung 2", "Задача 4.1") bölüm başlığı DEĞİLDİR. Cevap
    # eklerindeki "## Chapter 1" + "### Problem 1.3" örüntüsünde Problem 1.3
    # \chapter'a zorlanıyor, etiket yutuluyordu. Kelimeler NUMBERED_EXERCISE_WORDS'te.
    _NUMBERED_EXERCISE_RE = re.compile(
        r"^" + NUMBERED_EXERCISE_WORDS + r"\s+(?:No\.?\s*|№\s*)?(?:[A-Z]\.?)?\d"
    )

    @staticmethod
    def _clean_heading_title(raw_title: str) -> str:
        """Markdown başlık metnini sadeleştir (span, sayfa linki, bold, italik)."""
        raw_title = raw_title.strip()
        # Strip HTML span tags (Marker inserts <span id="page-X-0"></span>)
        raw_title = re.sub(r"<span[^>]*></span>", "", raw_title).strip()
        # Strip Marker page-link wrappers from headings.
        # Marker produces headings like: # [4.3 Title](#page-9-0)
        # The [text](#page-N-0) link is meaningless in LaTeX; extract just the text.
        # F1-05: bağ metnindeki Marker kaçışları çözülür ("6.4.2.1 Solid Fuel
        # \(Binder\)" başlıkta matematik açıyordu; "NH2OH\\*NO3" satır sonu).
        raw_title = re.sub(r"\[([^\]]+)\]\(#[^)]*\)",
                           lambda m: _unescape_marker_link_text(m.group(1)),
                           raw_title).strip()
        # Strip bold markers from title (**Preface** -> Preface)
        clean_title = re.sub(r"\*\*(.+?)\*\*", r"\1", raw_title).strip()
        # Strip wrapping italic markers from heading titles (*Remarks* -> Remarks)
        clean_title = re.sub(r"^\*(.+)\*$", r"\1", clean_title).strip()
        return clean_title

    @staticmethod
    def _next_nonblank_line(lines: List[str], start: int) -> Optional[str]:
        """*start* indeksinden itibaren ilk dolu satır (yoksa None)."""
        for j in range(start, len(lines)):
            if lines[j].strip():
                return lines[j]
        return None

    # ------------------------------------------------------------------
    # 2026-09-25 (Y1): çok dilli bölüm yapısı planı
    # ------------------------------------------------------------------
    # İngilizce dışı kitaplarda bölüm yapısı zayıftı (1_cikar ölçümü, 25 Eyl):
    # es "## CAPÍTULO VII" -> \section*, fr "### I" -> \subsection, it
    # yazar adı -> \chapter, de "# Kapitel 1 Grundlagen – …" başlık sayfasına
    # gidip kutu başlıkları \chapter oluyordu; EPUB'da "## X" + "# X"
    # tekrarı çift başlık veriyordu. Plan, satır-satır dönüşümden ÖNCE tüm
    # başlıklara bakıp şu kararları verir (kurallar tek tek test edilir):
    #   Y1d  bitişik çapraz düzey tekrar ("## X" + "# X") -> tek (sığ) başlık
    #   Y1a  bölüm etiketi (altı dil): etiket + başlık -> \chapter{başlık};
    #        yalın etiket + ardından başlık -> tek \chapter; ardından başlık
    #        yoksa ve etiket üst düzeydeyse (ebeveyni yok ya da kitap/yazar/
    #        ön madde başlığı) \chapter{etiket} — Arap rakamlı yalın etiket
    #        HARİÇ: sanitizer "\chapter{Chapter 3}" satırını sahte bölüm diye
    #        siler (_SPURIOUS_CHAPTER_RE), orada eski davranış korunur.
    #        Kısım etiketleri geçerli bir dizi oluşturuyorsa \part olur.
    #   Y1b  etiket yoksa: bir düzeyde ardışık artan Roma rakamlı başlıklar
    #        (I, II., III — X) bölüm dizisidir -> \chapter.
    #   Y1c  çapa (etiket/dizi/kısım) varsa ön madde -> ana madde geçişi ilk
    #        çapadadır; öncesindeki kitap adı/yazar adı başlıkları \section*
    #        olur. İlk çapadan sonra etiketsiz \chapter adayı başlıklar
    #        (kutu/Exkurs başlığı) \section'a iner; çapalar tek tip md
    #        düzeyindeyse (L≥2, EPUB h2/h3) alt başlıklar göreli düzeye iner.
    #   R8a  (2026-09-25) kutu etiketi ("Exkurs 1.1", "Encadré 1.1 — X",
    #        "Врезка 1.1"): numarasız \subsubsection*; etiket yalınsa hemen
    #        ardından gelen numarasız başlık kutu başlığıdır ("Exkurs 1.1 —
    #        Başlık", tek başlık). Kutu başlıkları yapı çözümlemesine girmez
    #        (bölüm/alt bölüm numaralamasını kaydırmaz, düzey boşluğunu
    #        belirlemez).
    #   R8b  (2026-09-25) EPUB alt adı (ALTAD_MARKER satırı) başlığa katılır:
    #        yalın etiket / yalın Roma rakamı yerine bölüm adı olur, aksi
    #        halde "başlık — alt ad".
    #   R8c  (2026-09-25) numaralı başlığı olmayan bölümlerde yetim alt
    #        başlık: bölümün doğrudan altındaki başlık \subsection ya da daha
    #        derin kalmaz ("1.0.1" numarası, numarasız \subsubsection);
    #        md düzey hiyerarşisi korunarak boşluk kapatılır (yalnız terfi).

    @staticmethod
    def _display_math_span(lines: List[str], start: int) -> int:
        """_consume_display_math'ın tükettiği satır sayısı (denklem no hariç)."""
        first = lines[start].strip()
        if first.startswith("$$") and first.endswith("$$") and len(first) > 4:
            return 1
        j = start + 1
        while j < len(lines):
            ln = lines[j].strip()
            if "$$" in ln:
                return j + 1 - start
            if ln.startswith("#") or (j - start) > 30:
                return 1  # kapanmamış $$: tek satır düz metin
            j += 1
        return 1

    def _scan_headings(self, lines: List[str], start: int) -> List[_Heading]:
        """convert() döngüsünün başlık olarak işleyeceği satırlar."""
        heads: List[_Heading] = []
        i = start
        n = len(lines)
        while i < n:
            stripped = lines[i].strip()
            if stripped.startswith("$$"):
                i += self._display_math_span(lines, i)
                continue
            if stripped.startswith("```"):
                j = i + 1
                while j < n and not lines[j].strip().startswith("```"):
                    j += 1
                i = min(j + 1, n)
                continue
            m = re.match(r"^(#{1,6})\s+(.*)", lines[i])
            if m and not _BOOKTABS_CMD_RE.search(lines[i]):
                title = self._clean_heading_title(m.group(2))
                if title:
                    heads.append(_Heading(i, len(m.group(1)), title))
            i += 1
        return heads

    @staticmethod
    def _lines_between_ok(lines: List[str], a: int, b: int,
                          dropped: Set[int]) -> bool:
        """a ile b arasındaki satırlar boş (ya da düşürülmüş başlık) mı?"""
        return all(not lines[j].strip() or j in dropped for j in range(a + 1, b))

    @staticmethod
    def _has_body_text(lines: List[str], a: int, b: int) -> bool:
        """a ile b arasında başlık olmayan dolu satır var mı?"""
        return any(lines[j].strip() and not re.match(r"^\s*#{1,6}\s", lines[j])
                   for j in range(a + 1, b))

    @staticmethod
    def _caps_title_line(lines: List[str], idx: int) -> Optional[int]:
        """Başlığın hemen ardındaki BÜYÜK HARFLİ tek satırlık ad ("LE GUN-CLUB").

        EPUB'da "<h3>I<br/>LE GUN-CLUB</h3>" Roma rakamı ile bölüm adını
        ayrı satırlara bölebiliyor. Satır tek başına bir paragraf olmalı
        (ardından boş satır), en çok 100 karakter, küçük harf içermemeli;
        başlık/tablo/liste/görsel/matematik değil.
        """
        j = idx + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        if j >= len(lines):
            return None
        text = lines[j].strip()
        if len(text) > 100 or re.match(r"^(?:#|\||\$\$|```|!\[|[-*+]\s|\d+\.\s|>)", text):
            return None
        if j + 1 < len(lines) and lines[j + 1].strip():
            return None
        letters = [c for c in text if c.isalpha()]
        if len(letters) < 2 or any(c.islower() for c in letters):
            return None
        return j

    def _author_tokens(self, lines: List[str], start: int) -> Set[str]:
        """Yazar adı sözcükleri: başlık sayfasındaki yazarlar + EPUB künyesi.

        epub_parser kitap adından hemen sonra "*Yazar*" satırı yazar; ilk
        başlığa ya da "---" ayracına kadar en çok üç dolu satıra bakılır.
        """
        names = list(getattr(self, "_title_authors", []) or [])
        seen = 0
        for j in range(start, len(lines)):
            text = lines[j].strip()
            if not text:
                continue
            if text.startswith("#") or text in ("---", "***", "___"):
                break
            seen += 1
            m = re.match(r"^\*{1,3}([^*]+?)\*{1,3}$", text)
            if m:
                names.append(m.group(1))
            if seen >= 3:
                break
        tokens: Set[str] = set()
        for name in names:
            for word in re.findall(r"\w+", fold_accents(name).casefold()):
                if len(word) >= 3 and not word.isdigit():
                    tokens.add(word)
        return tokens

    def _attach_altad(self, lines: List[str], heads: List[_Heading],
                      dropped: Set[int]) -> None:
        """R8b: ALTAD_MARKER satırını hemen önündeki başlığa bağla.

        Yalın etiket ("CAPÍTULO VII") ya da yalın Roma rakamı ("II.") alt adı
        başlık olarak alır (etiket LaTeX'in "Bölüm N" önekiyle yinelenmez;
        "Bölüm 2 — Bölüm VII" çelişkisi); diğer başlıklara " — alt ad"
        eklenir (_heading_suffix). Bağlanan satır gövdede basılmaz.
        Konu özeti ("A. — B. — C.") ya da _ALTAD_MAX_CHARS'tan uzun alt ad
        başlığa katılmaz, düz paragraf kalır; yalın etiket ise özetin ilk
        konusunu ad olarak alır.
        """
        self._roman_altad: Set[int] = set()
        by_idx = {h.idx: h for h in heads}
        first = heads[0].idx if heads else len(lines)
        for j in range(first + 1, len(lines)):
            s = lines[j].lstrip()
            if not s.startswith(ALTAD_MARKER):
                continue
            alt = _clean_altad(s[len(ALTAD_MARKER):])
            if not alt:
                continue
            k = j - 1
            while k >= 0 and (not lines[k].strip() or k in dropped):
                k -= 1
            h = by_idx.get(k)
            if h is None:
                continue
            bare_label = h.label is not None and not h.label.title
            bare_roman = (not bare_label and h.roman is not None
                          and not h.roman[1])
            topics = [t for t in (_clean_altad(x) for x in
                                  _ALTAD_SEGMENT_SEP_RE.split(alt)) if t]
            if len(topics) > 1 or len(alt) > _ALTAD_MAX_CHARS:
                # Ad değil: satır düz paragraf kalır (_skip_lines'a girmez).
                name = topics[0] if len(topics) > 1 else ""
                if not name or len(name) > _ALTAD_MAX_CHARS:
                    continue
                if bare_label:
                    h.label = h.label._replace(title=name)
                elif bare_roman:
                    h.roman = (h.roman[0], name, h.roman[2])
                    self._roman_altad.add(h.idx)
                continue
            if bare_label:
                h.label = h.label._replace(title=alt)
            elif bare_roman:
                h.roman = (h.roman[0], alt, h.roman[2])
                self._roman_altad.add(h.idx)
            else:
                self._heading_suffix[h.idx] = alt
            self._skip_lines.add(j)

    def _is_box_title(self, lines: List[str], h: _Heading, nxt: _Heading,
                      dropped: Set[int]) -> bool:
        """Yalın kutu etiketinin ("Exkurs 1.1") ardındaki başlık kutu başlığı mı?

        Arada yalnız boş satır olmalı; numaralı başlık, etiket (bölüm/kısım/
        kutu/ek), numaralı alıştırma ve ön/arka madde adı kutu başlığı
        değildir. Düzeyi önemsizdir: Marker kutu başlığını çoğu kez etiketten
        SIĞ verir ("## Exkurs 1.1" + "# Reisen …", "#### Врезка 1.1" + "## …").
        """
        if not self._lines_between_ok(lines, h.idx, nxt.idx, dropped):
            return False
        if nxt.label is not None or nxt.box is not None:
            return False
        if nxt.roman is not None and nxt.roman[2]:
            return False
        if _APPENDIX_RE.match(nxt.title) or self._section_number(nxt.title):
            return False
        if self._NUMBERED_EXERCISE_RE.match(nxt.title):
            return False
        name = _norm_name(nxt.title)
        return not (name in FRONT_MATTER_HEADING_NAMES
                    or name in BACK_MATTER_HEADING_NAMES
                    or name in BIBLIOGRAPHY_HEADING_NAMES)

    def _plan_boxes(self, lines: List[str], heads: List[_Heading],
                    plan: Dict[int, _HeadingDecision],
                    dropped: Set[int]) -> List[_Heading]:
        """R8a: kutu başlıklarını numarasız \\subsubsection* olarak planla.

        Döner: kutu başlıkları çıkarılmış başlık listesi (yapı çözümlemesinin
        geri kalanı kutuları görmez: bölüm adayı, ebeveyn ya da düzey boşluğu
        hesabına girmezler).
        """
        kept: List[_Heading] = []
        k = 0
        while k < len(heads):
            h = heads[k]
            if h.box is None:
                kept.append(h)
                k += 1
                continue
            label, box_title = h.box
            title = h.title
            if not box_title and k + 1 < len(heads):
                nxt = heads[k + 1]
                if self._is_box_title(lines, h, nxt, dropped):
                    title = label + _TITLE_JOIN_SEP + nxt.title
                    plan[nxt.idx] = _HeadingDecision(drop=True)
                    dropped.add(nxt.idx)
                    k += 1
            plan[h.idx] = _HeadingDecision(level=4, title=title, unnumbered=True)
            k += 1
        return kept

    def _plan_structure(
        self, lines: List[str], start: int, is_book_start: bool = True,
    ) -> Dict[int, _HeadingDecision]:
        """Başlık satırı indeksi -> karar. Ayrıca _first_anchor_idx ve
        _skip_lines'ı kurar. Kurallar için bkz. bölüm başındaki açıklama."""
        plan: Dict[int, _HeadingDecision] = {}
        self._first_anchor_idx = None
        self._skip_lines = set()
        self._heading_suffix = {}
        heads = self._scan_headings(lines, start)
        if not heads:
            return plan

        # --- Y1d: bitişik çapraz düzey tekrar ("## X" + "# X") ----------
        # Aynı düzeydeki bitişik tekrarı _preprocess_markdown zaten eliyor;
        # burada düzeyi farklı olan (EPUB: parser'ın "## belge adı" + belgenin
        # kendi "# X" başlığı) tek başlığa iner, sığ olan kalır.
        kept: List[_Heading] = []
        dropped: Set[int] = set()
        for h in heads:
            prev = kept[-1] if kept else None
            if (prev is not None and prev.level != h.level
                    and _norm_name(prev.title) == _norm_name(h.title)
                    and self._lines_between_ok(lines, prev.idx, h.idx, dropped)):
                loser = prev if h.level < prev.level else h
                plan[loser.idx] = _HeadingDecision(drop=True)
                dropped.add(loser.idx)
                if loser is prev:
                    kept[-1] = h
                continue
            kept.append(h)
        heads = kept

        # --- R8b: EPUB alt adı (bölüm başlığının p.subh2'si) --------------
        self._attach_altad(lines, heads, dropped)

        # --- R8a: kutu başlıkları -----------------------------------------
        heads = self._plan_boxes(lines, heads, plan, dropped)
        if not heads:
            return plan
        pos_of = {h.idx: k for k, h in enumerate(heads)}

        # Ebeveyn: kendisinden önceki en yakın DAHA SIĞ başlık.
        parent_of: Dict[int, Optional[_Heading]] = {}
        stack: List[_Heading] = []
        for h in heads:
            while stack and stack[-1].level >= h.level:
                stack.pop()
            parent_of[h.idx] = stack[-1] if stack else None
            stack.append(h)

        book_key = _norm_key(getattr(self, "_book_title", "") or "")
        author_tokens = self._author_tokens(lines, start)

        def title_like(h: _Heading) -> bool:
            key = _norm_key(h.title)
            if not book_key or len(key) < 4:
                return False
            return key == book_key or (len(key) >= 6 and (
                book_key.startswith(key) or key.startswith(book_key)))

        def author_like(h: _Heading) -> bool:
            words = [w for w in re.findall(r"\w+", fold_accents(h.title).casefold())
                     if not w.isdigit()]
            return bool(words) and len(words) <= 5 and words[-1] in author_tokens

        def is_part(h: _Heading) -> bool:
            return h.label is not None and h.label.kind == "part"

        def exempt_parent(h: _Heading) -> bool:
            # Bölüm dizisinin ebeveyni olabilecek "yapı dışı" başlıklar.
            name = _norm_name(h.title)
            return (title_like(h) or author_like(h) or is_part(h)
                    or name in FRONT_MATTER_HEADING_NAMES)

        def exempt_after(h: _Heading) -> bool:
            # İlk çapadan sonra \chapter düzeyinde kalabilen etiketsiz başlıklar.
            return (is_part(h) or _norm_name(h.title) in BACK_MATTER_HEADING_NAMES
                    or bool(_APPENDIX_RE.match(h.title)))

        def toc_echo(h: _Heading) -> bool:
            # İçindekiler girdisi: etiket başlığının ardından tablo gelir
            # ("### Part I First Principles" + "| … | 3 |").
            nxt = self._next_nonblank_line(lines, h.idx + 1)
            return nxt is not None and nxt.strip().startswith("|")

        # Aynı etiket (tür + numara + başlık) birden çok kez geçiyorsa yalnız
        # içindekiler girdisi OLMAYAN ilk geçiş çapadır; sonrakiler üst bilgi
        # yankısıdır (bölüm yine basılır, ama yapıyı belirlemez).
        first_label_idx: Dict[tuple, int] = {}
        for h in heads:
            if h.label is not None and not toc_echo(h):
                key = (h.label.kind, h.label.value, _norm_key(h.label.title))
                first_label_idx.setdefault(key, h.idx)

        def real_label(h: _Heading) -> bool:
            key = (h.label.kind, h.label.value, _norm_key(h.label.title))
            return first_label_idx.get(key) == h.idx

        anchor_level: Dict[int, int] = {}
        chapter_anchors: List[int] = []
        label_values: List[int] = []

        # --- Y1a: bölüm etiketleri --------------------------------------
        for k, h in enumerate(heads):
            label = h.label
            if label is None or label.kind != "chapter":
                continue
            real = real_label(h)
            if label.title:
                # Etiket + başlık aynı satırda: etiket LaTeX'in "Bölüm N"
                # önekiyle yinelenmesin diye atılır (İngilizce "Chapter N X"
                # davranışının altı dile genişletilmişi).
                plan[h.idx] = _HeadingDecision(level=1, title=label.title, anchor=real)
                if real:
                    chapter_anchors.append(h.idx)
                    anchor_level[h.idx] = h.level
                    label_values.append(label.value)
                continue
            nxt = heads[k + 1] if k + 1 < len(heads) else None
            if (nxt is not None and nxt.idx not in plan
                    and self._lines_between_ok(lines, h.idx, nxt.idx, dropped)
                    and self._is_chapter_title_candidate(lines[nxt.idx], h.level)):
                # Yalın etiket + ardından başlık = tek \chapter{başlık}.
                plan[h.idx] = _HeadingDecision(drop=True)
                plan[nxt.idx] = _HeadingDecision(level=1, anchor=real)
                if real:
                    chapter_anchors.append(nxt.idx)
                    anchor_level[nxt.idx] = min(h.level, nxt.level)
                    label_values.append(label.value)
                continue
            if not real:
                continue
            parent = parent_of.get(h.idx)
            if h.level == 1:
                # Eskiden de \chapter{etiket} idi; artık çapa sayılır.
                plan[h.idx] = _HeadingDecision(level=1, anchor=True)
            elif not label.digit and (parent is None or exempt_parent(parent)):
                # Başlıksız üst düzey etiket ("## CAPÍTULO VII" + gövde).
                plan[h.idx] = _HeadingDecision(level=1, anchor=True)
            else:
                continue
            chapter_anchors.append(h.idx)
            anchor_level[h.idx] = h.level
            label_values.append(label.value)

        # --- Kısım etiketleri -> \part (geçerli dizi ise) ---------------
        parts = [h for h in heads if is_part(h) and h.idx not in plan and real_label(h)]
        part_values = [h.label.value for h in parts]
        parts_ok = (len(parts) >= 2
                    and all(b == a + 1 for a, b in zip(part_values, part_values[1:])))
        if parts_ok:
            # Kısımlar bölüm içerir: ardışık iki kısım arasında en az bir
            # bölüm çapası ya da kısımla aynı/daha sığ düzeyde başlık olmalı
            # (bir alıştırmadaki "Part 1 / Part 2" alt başlıkları elenir).
            anchor_set = set(chapter_anchors)
            for a, b in zip(parts, parts[1:]):
                between = heads[pos_of[a.idx] + 1:pos_of[b.idx]]
                if not any(c.idx in anchor_set or c.level <= a.level for c in between):
                    parts_ok = False
                    break
        part_anchors: List[int] = []
        if parts_ok:
            for h in parts:
                plan[h.idx] = _HeadingDecision(
                    level=0, title=h.label.title or h.title, anchor=True)
                part_anchors.append(h.idx)

        # --- Y1b: Roma rakamlı bölüm dizisi (etiket yoksa) ---------------
        roman_anchors: List[int] = []
        if not chapter_anchors:
            by_level: Dict[int, List[_Heading]] = {}
            for h in heads:
                if h.roman is not None and h.idx not in plan:
                    by_level.setdefault(h.level, []).append(h)
            for lvl in sorted(by_level):
                members = by_level[lvl]
                values = [h.roman[0] for h in members]
                if any(b != a + 1 for a, b in zip(values, values[1:])):
                    continue  # ardışık değil (yeniden başlayan alt numaralar)
                if len(members) == 1 and (not members[0].roman[2] or values[0] > 2):
                    # Tek üye ancak güçlü biçimde ve I/II ise dizi sayılır
                    # ("I Am Legend" ya da tek başına "## X" dizi değildir).
                    continue
                first_pos = pos_of[members[0].idx]
                # Dizi II'den başlıyorsa I. kısmın başlığı yazılmamıştır
                # (Gutenberg: yazı adı + gövde + "II."): dizinin hemen
                # önündeki, yapı dışı olmayan ve ardında gövde metni bulunan
                # başlık 1. bölümdür; dizinin ebeveyni olabilir.
                implicit: Optional[_Heading] = None
                if values[0] == 2 and first_pos > 0:
                    prev = heads[first_pos - 1]
                    if (prev.idx not in plan and prev.label is None
                            and prev.roman is None and not exempt_parent(prev)
                            and self._has_body_text(lines, prev.idx, members[0].idx)):
                        implicit = prev

                def parent_ok(h: _Heading) -> bool:
                    par = parent_of.get(h.idx)
                    return par is None or exempt_parent(par) or par is implicit

                if not all(parent_ok(h) for h in members):
                    continue  # dizi başka bir bölümün alt başlıklarıdır
                if any(c.level < lvl and not exempt_after(c)
                       for c in heads[first_pos + 1:]):
                    continue  # diziden sonra daha sığ yapı var: dizi alt düzey
                for h in members:
                    title = h.roman[1]
                    if not title:
                        caps = self._caps_title_line(lines, h.idx)
                        if caps is not None:
                            title = lines[caps].strip()
                            self._skip_lines.add(caps)
                        else:
                            title = h.title
                    plan[h.idx] = _HeadingDecision(level=1, title=title, anchor=True)
                    roman_anchors.append(h.idx)
                    anchor_level[h.idx] = h.level
                if implicit is not None:
                    plan[implicit.idx] = _HeadingDecision(level=1, anchor=True)
                    roman_anchors.append(implicit.idx)
                    anchor_level[implicit.idx] = implicit.level
                break

        # R8b: alt adı olan ama bölüm dizisine girmeyen yalın Roma rakamlı
        # başlık da alt adı başlık olarak alır (düzeyi değişmez).
        for h in heads:
            if (h.idx not in plan and h.roman is not None
                    and h.idx in self._roman_altad):
                plan[h.idx] = _HeadingDecision(level=h.level, title=h.roman[1])

        chapter_anchors = chapter_anchors + roman_anchors
        all_anchors = chapter_anchors + part_anchors
        if not all_anchors:
            return plan
        first_anchor = min(all_anchors)
        if is_book_start:
            self._first_anchor_idx = first_anchor

            # --- Y1c: ön maddedeki kitap adı / yazar adı başlıkları -------
            # Başlık sayfasındaki adın AYNI düzeyde birebir tekrarı zaten
            # _seen_headings ile basılmıyor; ona dokunulmaz.
            seen = getattr(self, "_seen_headings", set())
            for h in heads:
                if h.idx >= first_anchor:
                    break
                bold_free = re.sub(r"\*\*(.+?)\*\*", r"\1", h.title).strip()
                if (h.idx not in plan and h.level == 1
                        and f"{h.level}:{bold_free}" not in seen
                        and (title_like(h) or author_like(h))):
                    plan[h.idx] = _HeadingDecision(level=2)

        if not chapter_anchors:
            return plan

        # --- Y1c: ilk çapadan sonra etiketsiz \chapter adayları ----------
        after = [h for h in heads if h.idx > first_anchor and h.idx not in plan]
        levels = {anchor_level[i] for i in chapter_anchors}
        uniform = next(iter(levels)) if len(levels) == 1 else None
        # Etiket numaraları kesintisiz bir dizi değilse (Marker bazı
        # bölümlerin etiketini düşürmüş: 1, 2, 5, 9) etiketsiz başlıklar
        # gerçek bölüm olabilir; o zaman hiçbiri indirilmez.
        values = sorted(set(label_values))
        systematic = all(b == a + 1 for a, b in zip(values, values[1:]))
        if (systematic and uniform is not None and uniform >= 2
                and not any(h.level < uniform and not exempt_after(h) for h in after)):
            # Çapalar tek tip düzeyde (EPUB h2/h3) ve üstlerinde yapı yok:
            # alt başlıklar çapaya göre göreli düzeye iner ("###" -> \section).
            shift = uniform - 1
            for h in after:
                if is_part(h):
                    continue
                if h.level <= uniform:
                    new_level = 1 if exempt_after(h) else 2
                else:
                    new_level = h.level - shift
                if new_level != h.level:
                    plan[h.idx] = _HeadingDecision(level=new_level)
        else:
            if systematic:
                for h in after:
                    if h.level == 1 and not exempt_after(h):
                        plan[h.idx] = _HeadingDecision(level=2)
            # Düzey boşluğu: bölümün altında hiç "##" yoksa (EPUB'da bölüm
            # "#", alt başlık "###") alt başlıklar boşluğu kapatacak kadar
            # yükselir; yoksa \chapter altında \subsection kalıp "1.0.1"
            # numarası çıkar. Numaralı başlıklar ("2.1 X") düzeyini
            # numarasından aldığı için hesaba katılmaz.
            def md_driven(h: _Heading) -> bool:
                num = self._section_number(h.title)
                return not is_part(h) and (num is None or "." not in num)

            def current(h: _Heading) -> int:
                d = plan.get(h.idx)
                return d.level if d is not None else h.level

            driven = [h for h in after if md_driven(h) and current(h) >= 2]
            if driven:
                gap = min(current(h) for h in driven) - 2
                if gap > 0:
                    for h in driven:
                        plan[h.idx] = _HeadingDecision(level=current(h) - gap)

        # --- R8c: yetim alt başlıklar (numarasız bölümlerde) ---------------
        self._close_orphan_headings(heads, plan, first_anchor)
        return plan

    def _close_orphan_headings(self, heads: List[_Heading],
                               plan: Dict[int, _HeadingDecision],
                               first_anchor: int) -> None:
        """R8c: bölümün doğrudan altındaki yetim başlıkları terfi ettir.

        Genel düzey boşluğu (Y1c) kitap geneli en sığ alt başlığa göre
        hesaplanır; tek bir bölümde alt başlıklar daha derinse (bir Almanca
        test kitabında gerçek h2 başlıkları Marker'da "####") \\subsection
        ("1.0.1") ya da
        numarasız, İçindekiler'de görünmeyen \\subsubsection kalıyordu.
        Her bölüm kendi içinde md düzey hiyerarşisine göre yeniden
        düzeylenir: bölümün altındaki ilk kat \\section, onun altı
        \\subsection ... (aynı md düzeyindeki kardeşler aynı düzeyi alır).
        Yalnız TERFİ (düzey sayısı küçülür), indirme yok. Çok parçalı
        numaralı başlık ("2.1 X") içeren bölümlere dokunulmaz: orada yapıyı
        numaralar belirler, numarasız başlıkların yeri belirsizdir.
        """
        def level_of(h: _Heading) -> Optional[int]:
            d = plan.get(h.idx)
            if d is not None:
                return None if d.drop else d.level
            if h.level == 1 and h.title.lower() in _NON_CHAPTER_NAMES:
                return 2  # _try_heading bunu \section'a indirir
            return h.level

        def numbered(h: _Heading) -> bool:
            num = self._section_number(h.title)
            return num is not None and "." in num

        segments: List[List[_Heading]] = []
        cur: Optional[List[_Heading]] = None
        for h in heads:
            if h.idx < first_anchor:
                continue
            lvl = level_of(h)
            if lvl is None:
                continue
            if lvl <= 1:
                cur = []
                segments.append(cur)
            elif cur is not None:
                cur.append(h)

        for seg in segments:
            if not seg or any(numbered(h) for h in seg):
                continue
            # F1-15: kaynak PDF'in puntosu aynı md düzeyindeki başlıkları
            # ayırıyorsa düzey puntoya göre kurulur (terfi ve indirme).
            keys = self._size_refined_keys(seg)
            if keys is not None:
                size_stack: List[tuple] = []
                for h in seg:
                    key = keys[h.idx]
                    while size_stack and size_stack[-1] >= key:
                        size_stack.pop()
                    target = 2 + len(size_stack)
                    size_stack.append(key)
                    if h.label is not None or target == level_of(h):
                        continue
                    d = plan.get(h.idx)
                    plan[h.idx] = (d._replace(level=target) if d is not None
                                   else _HeadingDecision(level=target))
                continue
            stack: List[int] = []   # açık ataların md düzeyleri
            for h in seg:
                while stack and stack[-1] >= h.level:
                    stack.pop()
                target = 2 + len(stack)
                stack.append(h.level)
                # Etiketli başlık (kısım dizisine girmeyen "Part 1: Setup",
                # çapa olmayan "Chapter 3") Y1 kurallarına tabidir; düzeyine
                # dokunulmaz (Y1c düzey boşluğu hesabı da onları dışlar).
                if h.label is not None:
                    continue
                if target < level_of(h):
                    d = plan.get(h.idx)
                    plan[h.idx] = (d._replace(level=target) if d is not None
                                   else _HeadingDecision(level=target))

    def _size_refined_keys(self, seg: List[_Heading]) -> Optional[Dict[int, tuple]]:
        """F1-15: bölüm içi başlıkların düzey anahtarı (md düzeyi, -punto).

        Marker başlık düzeyini satır yüksekliği kümelemesinden verir; aynı
        "####" düzeyine 14,9 pt ve 12,8 pt başlıklar düşebiliyor (ölçüldü):
        R8c md düzeyine göre kardeş sayıp bir kısmını \\section, "###"
        sonrasındakini \\subsection yapıyordu. Kaynak PDF'teki punto aynı md
        düzeyinin başlıklarını ayırıyorsa büyük punto üst düzey olur.

        Güvenlik (biri tutmazsa None — eski md kuralı geçerli):
        - bölümdeki HER başlık PDF'te tek anlamlı bir puntoyla eşleşir
          (_pdf_heading_sizes; içindekiler/üst bilgi yinelemeleri elenir);
        - en az bir md düzeyinde birbirinden > _HEADING_SIZE_TOL farklı iki
          punto vardır (yoksa md düzeyi zaten tutarlıdır, PDF okunmaz);
        - punto md düzeyiyle ÇELİŞMEZ: md'de daha sığ başlık daha küçük
          puntolu olamaz (çelişkide eşleme ya da dizgi belirsizdir).
        Punto yalnız aynı md düzeyindeki başlıkları sıralar; anahtar
        (md düzeyi, -punto kümesi).
        """
        by_level: Dict[int, int] = {}
        for h in seg:
            by_level[h.level] = by_level.get(h.level, 0) + 1
        if max(by_level.values(), default=0) < 2:
            return None
        sizes = self._pdf_heading_sizes()
        if not sizes:
            return None
        size_of: Dict[int, float] = {}
        for h in seg:
            s = sizes.get(_norm_key(h.title))
            if s is None:
                return None
            size_of[h.idx] = s
        split = False
        for lvl in by_level:
            vals = [size_of[h.idx] for h in seg if h.level == lvl]
            if vals and max(vals) - min(vals) > _HEADING_SIZE_TOL:
                split = True
        if not split:
            return None
        for a in seg:
            for b in seg:
                if a.level < b.level and size_of[a.idx] < size_of[b.idx] - _HEADING_SIZE_TOL:
                    logger.debug("F1-15: punto md düzeyiyle çelişiyor (%s / %s) — "
                                 "md düzeyi korunur", a.title, b.title)
                    return None
        # Punto kümeleri: aralarında <= _HEADING_SIZE_TOL fark olanlar aynı küme.
        clusters: List[List[float]] = []
        for s in sorted(set(size_of.values()), reverse=True):
            if clusters and clusters[-1][-1] - s <= _HEADING_SIZE_TOL:
                clusters[-1].append(s)
            else:
                clusters.append([s])
        bucket = {s: c[0] for c in clusters for s in c}
        logger.info("F1-15: bölüm içi başlık düzeyi kaynak PDF puntosuyla "
                    "kuruldu (%d başlık)", len(seg))
        return {h.idx: (h.level, -bucket[size_of[h.idx]]) for h in seg}

    def _pdf_heading_sizes(self) -> Dict[str, float]:
        """Kaynak PDF'teki başlık adayı satırların puntosu (F1-15).

        Anahtar: _norm_key(satır ya da blok metni). Başlık adayı: gövde
        puntosundan büyük ya da kalın satır (içindekiler girdisi, üst bilgi
        gövde puntosunda/küçük, kalın olmayan satırlardır ve elenir). Aynı
        metin farklı puntolarda başlık adayıysa (belirsiz) anahtar yazılmaz.
        PDF yok/okunamıyorsa boş sözlük. convert() başına bir kez okunur.
        """
        if self._pdf_heading_sizes_cache is not None:
            return self._pdf_heading_sizes_cache
        self._pdf_heading_sizes_cache = {}
        pdf = Path(getattr(self.config, "input_pdf", "") or "")
        if pdf.suffix.lower() != ".pdf" or not pdf.is_file():
            return self._pdf_heading_sizes_cache
        try:
            import fitz  # PyMuPDF
            doc = fitz.open(str(pdf))
        except Exception as exc:  # noqa: BLE001 - PDF okunamazsa md kuralı
            logger.debug("F1-15: PDF okunamadı (%s): %s", pdf, exc)
            return self._pdf_heading_sizes_cache
        chars: Dict[float, int] = {}
        occ: Dict[str, List[Tuple[float, bool]]] = {}
        try:
            flags = getattr(fitz, "TEXTFLAGS_DICT", 0) & ~getattr(
                fitz, "TEXT_PRESERVE_IMAGES", 0)
            for page in doc:
                for block in page.get_text("dict", flags=flags).get("blocks", []):
                    rows: List[Tuple[str, float, bool]] = []
                    for line in block.get("lines", []):
                        spans = [s for s in line.get("spans", [])
                                 if s.get("text", "").strip()]
                        if not spans:
                            continue
                        for s in spans:
                            sz = round(float(s["size"]), 1)
                            chars[sz] = chars.get(sz, 0) + len(s["text"])
                        rows.append((
                            "".join(s["text"] for s in spans),
                            round(max(float(s["size"]) for s in spans), 1),
                            any((int(s.get("flags", 0)) & 16)
                                or "bold" in str(s.get("font", "")).lower()
                                for s in spans)))
                    entries = list(rows)
                    if len(rows) > 1:
                        entries.append((" ".join(r[0] for r in rows),
                                        max(r[1] for r in rows),
                                        any(r[2] for r in rows)))
                    for text, size, bold in entries:
                        if len(text) > 300:
                            continue
                        key = _norm_key(text)
                        if len(key) >= 4:
                            occ.setdefault(key, []).append((size, bold))
        except Exception as exc:  # noqa: BLE001
            logger.debug("F1-15: PDF taranamadı: %s", exc)
            return self._pdf_heading_sizes_cache
        finally:
            doc.close()
        if not chars:
            return self._pdf_heading_sizes_cache
        body = max(chars.items(), key=lambda kv: kv[1])[0]
        result: Dict[str, float] = {}
        for key, seen in occ.items():
            heads = sorted({s for s, bold in seen
                            if s > body + _HEADING_SIZE_TOL or (bold and s >= body)})
            if heads and heads[-1] - heads[0] <= _HEADING_SIZE_TOL:
                result[key] = heads[-1]
        self._pdf_heading_sizes_cache = result
        return result

    # ------------------------------------------------------------------
    # 2026-10-05 (G1a-1): Marker yapı hataları (satır düzeyinde onarım),
    # başlıksız belgede ana madde geçişi, ek başlıkları
    # ------------------------------------------------------------------

    def _repair_marker_structure(self, markdown: str) -> str:
        """Yapı planından ÖNCE satır düzeyindeki Marker yapı hataları.

        Sıra: (1) başlığa dönmüş madde / kalın etiket;
        (2) başlığa yapışık italik soru; (3) PDF yer imi başlığı olan
        gövde satırı, sonraki cümleye yapışık başlık;
        (4) şekil altyazısına yapışık gövde satırı. Hiçbir metin
        atılmaz: satırlar yalnız yeniden etiketlenir ya da bölünür.
        """
        lines = markdown.split("\n")
        lines = self._demote_bullet_headings(lines)
        lines = self._demote_non_headings(lines)
        lines = self._split_heading_questions(lines)
        lines = self._promote_outline_headings(lines)
        lines = self._split_caption_tails(lines)
        return "\n".join(lines)

    def _demote_bullet_headings(self, lines: List[str]) -> List[str]:
        """Başlığa dönmüş madde imli satır liste maddesi; kalın iki noktalı
        kısa etiket başlığı kalın paragraf olur.

        "#### - **Note:**" -> "- **Note:**". Etiket iki noktayla
        bitiyor ve ardından tek satırlık düz paragraf geliyorsa paragraf
        maddenin içeriğidir (kaynakta aynı satır: "● Note: abc…";
        Marker'ın doğru verdiği kardeş madde de böyle). Komşu maddelerle
        arasındaki boş satırlar kalkar (tek liste). İç içe liste dönüştürücüde
        desteklenmez: alt maddeler kardeş madde olur. Eskiden madde başlık
        oluyor, "### **- Summary:**" İçindekiler'e "3.3.2 - Özet:" diye girip
        sonraki alt bölüm numaralarını kaydırıyordu.
        "#### **Result:**" -> "**Result:**" (bkz. _BOLD_LABEL_HEADING_RE).
        """
        out: List[str] = []
        n = len(lines)
        i = 0
        in_code = False
        while i < n:
            ln = lines[i]
            s = ln.strip()
            if s.startswith("```"):
                in_code = not in_code
            if in_code or not s.startswith("#") or _BOOKTABS_CMD_RE.search(s):
                out.append(ln)
                i += 1
                continue
            m = _BULLET_HEADING_RE.match(s)
            if m:
                item = (m.group("open") or "") + m.group("rest")
                j = i + 1
                while j < n and not lines[j].strip():
                    j += 1
                nxt = lines[j].strip() if j < n else ""
                after = lines[j + 1].strip() if j + 1 < n else ""
                if (re.sub(r"[*_\s]+$", "", item).endswith(":") and nxt
                        and not self._BLOCK_START_RE.match(nxt)
                        and not nxt.startswith(_MARK_SENTINEL)
                        and (not after or self._BLOCK_START_RE.match(after))):
                    item = f"{item} {nxt}"
                    j += 1
                else:
                    j = i + 1
                k = len(out)
                while k > 0 and not out[k - 1].strip():
                    k -= 1
                if 0 < k < len(out) and self._UL_ITEM_RE.match(out[k - 1]):
                    del out[k:]
                out.append("- " + item)
                logger.debug("G1a-1: başlığa dönmüş madde liste maddesi oldu: %s", s)
                k = j
                while k < n and not lines[k].strip():
                    k += 1
                if k > j and k < n and self._UL_ITEM_RE.match(lines[k]):
                    j = k
                i = j
                continue
            m = _BOLD_LABEL_HEADING_RE.match(s)
            if (m and not re.search(r"\d", m.group("text"))
                    and len(m.group("text").split()) <= _LABEL_HEADING_MAX_WORDS):
                out.append(m.group("label"))
                logger.debug("G1a-1: kalın etiket başlığı paragraf oldu: %s", s)
                i += 1
                continue
            out.append(ln)
            i += 1
        return out

    def _demote_non_headings(self, lines: List[str]) -> List[str]:
        """Makine belirteci ve vurgulu soru başlığı paragraf olur (bkz.
        _TOKEN_HEADING_RE). Yalnız PDF yolu; PDF yer iminde
        (outline) geçen satır başlık kalır. Belirteç ayrıca PDF'te kalın ya
        da büyük puntolu (başlık adayı) olmamalı. Metin aynen kalır."""
        if not self._kaynak_pdf_var():
            return lines
        outline = {self._outline_key(t) for _, t in self._pdf_outline()}
        out: List[str] = []
        in_code = False
        for ln in lines:
            s = ln.strip()
            if s.startswith("```"):
                in_code = not in_code
            if not in_code and s.startswith("#"):
                m = _QUESTION_HEADING_RE.match(s)
                if m and self._outline_key(m.group("text")) not in outline:
                    out.append(m.group("text"))
                    logger.debug("Vurgulu soru başlığı paragraf oldu: %s", s)
                    continue
                m = _TOKEN_HEADING_RE.match(s)
                if m:
                    core = _md_unescape(m.group("text").strip("`"))
                    if (len(core) >= _TOKEN_HEADING_MIN_CHARS
                            and re.search(r"[_/\\]", core)
                            and self._outline_key(m.group("text")) not in outline
                            and _norm_key(core) not in self._pdf_heading_sizes()):
                        out.append(m.group("text"))
                        logger.debug("Belirteç başlığı paragraf oldu: %s", s)
                        continue
            out.append(ln)
        return out

    @staticmethod
    def _split_heading_questions(lines: List[str]) -> List[str]:
        """Başlığın sonuna yapışmış italik soru ayrı satıra iner.

        "# X *What was X?*" -> "# X" + boş satır + "*What was X?*". Kaynakta
        soru başlığın altındaki ayrı satırdır; başlıkta kalınca İçindekiler'e
        de giriyor, başlık metni iki kez okunuyordu.
        """
        out: List[str] = []
        in_code = False
        for ln in lines:
            s = ln.strip()
            if s.startswith("```"):
                in_code = not in_code
            m = (None if in_code or not s.startswith("#")
                 else _HEADING_TRAILING_QUESTION_RE.match(s))
            if m and re.search(r"\w", m.group("title")):
                out.extend([m.group("head"), "", m.group("q")])
                continue
            out.append(ln)
        return out

    def _pdf_outline(self) -> List[Tuple[int, str]]:
        """Kaynak PDF'in yer imleri [(düzey, ad)]; PDF/yer imi yoksa boş."""
        if self._pdf_outline_cache is not None:
            return self._pdf_outline_cache
        self._pdf_outline_cache = []
        pdf = Path(getattr(self.config, "input_pdf", "") or "")
        if pdf.suffix.lower() != ".pdf" or not pdf.is_file():
            return self._pdf_outline_cache
        try:
            import fitz  # PyMuPDF
            doc = fitz.open(str(pdf))
            try:
                toc = doc.get_toc(simple=True)
            finally:
                doc.close()
        except Exception as exc:  # noqa: BLE001 - yer imi okunamazsa onarım yok
            logger.debug("G1a-1: PDF yer imleri okunamadı (%s): %s", pdf, exc)
            return self._pdf_outline_cache
        self._pdf_outline_cache = [(int(e[0]), str(e[1])) for e in toc
                                   if len(e) >= 2 and str(e[1]).strip()]
        return self._pdf_outline_cache

    @staticmethod
    def _outline_key(text: str) -> str:
        """Yer imi eşleme anahtarı: nöbetçi, bağ ve vurgu atılmış _norm_key."""
        return _norm_key(MarkdownToLatex._clean_heading_title(_MARK_RE.sub("", text)))

    def _promote_outline_headings(self, lines: List[str]) -> List[str]:
        """PDF yer imi başlığı olan gövde satırı başlık olur.

        Marker bazı başlıkları "#" işaretsiz düz paragraf verir (yalnız sayfa
        çapasıyla ya da hiç işaretsiz); yer imi (outline) kaynağın kendi
        başlık listesidir. Kurallar:
        - yalnız ilk markdown başlığından SONRAKİ, paragraf başındaki düz
          satırlar (kapak/ad bloğuna dokunulmaz);
        - her yer imi bir kez kullanılır; mevcut başlıklar kendi yer imini
          önce tüketir (aynı ad iki kez başlık olmaz);
        - tam eşleşme (yer iminde dipnot işareti rakam olarak kalmış olabilir:
          "…values92"); ya da satır yer imi başlığıyla başlıyor ve ardından
          büyük harfle yeni cümle geliyorsa (yapışık başlık) satır ikiye
          bölünür — yer imi en az _GLUED_HEADING_MIN_WORDS sözcük olmalı;
        - düzey yer imi düzeyinden (en sığ yer imi = "#"); gerçek boru
          hattında toc_releveler başlıkları sonra yer imine göre düzeyler.
        PDF ya da yer imi yoksa satırlar aynen döner.
        """
        outline = self._pdf_outline()
        if not outline:
            return lines
        top = min(lvl for lvl, _t in outline)
        remaining: Dict[str, List[int]] = {}
        words: Dict[str, int] = {}
        for lvl, title in outline:
            key = _norm_key(title)
            if len(key) >= 4:
                remaining.setdefault(key, []).append(lvl)
                words[key] = len(title.split())
        heads = self._scan_headings(lines, 0)   # kod/matematik blokları hariç
        if not heads:
            return lines
        first_head = heads[0].idx
        for h in heads:
            key = _norm_key(_MARK_RE.sub("", h.title))
            if remaining.get(key):
                remaining[key].pop(0)

        out: List[str] = list(lines[:first_head])
        promoted: List[int] = []   # kurtarılan başlıkların out içindeki yeri
        in_code = False
        math_until = -1
        for i in range(first_head, len(lines)):
            ln = lines[i]
            s = ln.strip()
            if s.startswith("```"):
                in_code = not in_code
                out.append(ln)
                continue
            if in_code or i < math_until:
                out.append(ln)
                continue
            if s.startswith("$$"):
                math_until = i + self._display_math_span(lines, i)
                out.append(ln)
                continue
            if (not s or self._BLOCK_START_RE.match(s) or s.startswith(_MARK_SENTINEL)
                    or lines[i - 1].strip()):
                out.append(ln)
                continue
            hit = self._outline_hit(s, remaining, words)
            if hit is None:
                out.append(ln)
                continue
            key, cut = hit
            lvl = remaining[key].pop(0)
            hashes = "#" * max(1, min(6, lvl - top + 1))
            promoted.append(len(out))
            if cut is None:
                out.append(f"{hashes} {s}")
                logger.info("G1a-1: PDF yer imi başlığı gövdeden kurtarıldı: %s", s[:80])
            else:
                out.extend([f"{hashes} {s[:cut].rstrip()}", "", s[cut:].strip()])
                logger.info("G1a-1: yapışık yer imi başlığı ayrıldı: %s", s[:cut])
        self._nest_under_promoted(out, promoted)
        return out

    def _nest_under_promoted(self, out: List[str], promoted: List[int]) -> None:
        """Kurtarılan başlığın altındaki Marker başlıkları bir düzey iner.

        Kurtarılan başlık yer imi düzeyini alır; Marker onun alt başlıklarını
        aynı "#" düzeyinde vermiş olabilir (ör. "First synthetic heading…"
        12 pt yer imi başlığı, altındaki "Second synthetic heading…" 11 pt
        kalın başlık, ikisi de "####"; altında
        \\paragraph olmalı). Kanıt PDF puntosu: aynı "#" düzeyinde ve kurtarılan
        başlıktan belirgin küçük (_HEADING_SIZE_TOL) puntolu başlık çocuktur.
        Daha sığ başlıkta, puntosu bilinmeyen ya da büyük/eşit puntolu
        kardeşte, ya da sonraki kurtarılan başlıkta durulur.
        """
        sizes = self._pdf_heading_sizes()
        if not sizes or not promoted:
            return
        marks = set(promoted)
        for p in promoted:
            m = re.match(r"^(#{1,6})\s+(.*)", out[p])
            parent = sizes.get(self._outline_key(m.group(2))) if m else None
            if parent is None:
                continue
            level = len(m.group(1))
            for q in range(p + 1, len(out)):
                if q in marks:
                    break
                mq = re.match(r"^(#{1,6})\s+(.*)", out[q])
                if not mq:
                    continue
                lq = len(mq.group(1))
                if lq < level:
                    break
                if lq > level:
                    continue
                child = sizes.get(self._outline_key(mq.group(2)))
                if child is None or child >= parent - _HEADING_SIZE_TOL or level >= 6:
                    break
                out[q] = "#" * (level + 1) + " " + mq.group(2)
                logger.debug("G1a-1: kurtarılan başlığın alt başlığı bir düzey indi: %s",
                             mq.group(2)[:60])

    def _outline_hit(self, s: str, remaining: Dict[str, List[int]],
                     words: Dict[str, int]) -> Optional[Tuple[str, Optional[int]]]:
        """Satırın yer imi eşleşmesi: (anahtar, None) tam; (anahtar, bölme
        yeri) yapışık başlık; eşleşme yoksa None."""
        key = self._outline_key(s)
        if remaining.get(key):
            return key, None
        if _MARK_SENTINEL in s and len(key) >= 4:
            for k2, lv in remaining.items():
                if lv and k2.startswith(key) and k2[len(key):].isdigit():
                    return k2, None
        # Yapışık başlık: satırın normalleştirilmiş karakterleri ve özgün yerleri.
        norm: List[str] = []
        where: List[int] = []
        p = 0
        while p < len(s):
            mm = _MARK_RE.match(s, p)
            if mm:
                p = mm.end()
                continue
            for c in fold_accents(s[p]).casefold():
                if c.isalnum():
                    norm.append(c)
                    where.append(p)
            p += 1
        flat = "".join(norm)
        best: Optional[Tuple[str, int]] = None
        for k2, lv in remaining.items():
            if (not lv or words.get(k2, 0) < _GLUED_HEADING_MIN_WORDS
                    or len(k2) >= len(flat) or not flat.startswith(k2)):
                continue
            cut = where[len(k2) - 1] + 1
            # Başlığın sonundaki kapanan tırnak/ayraç/iki nokta başlıkta kalır.
            while cut < len(s) and not s[cut].isspace() and not s[cut].isalnum():
                cut += 1
            rest = s[cut:]
            # 2026-10-05 (G1a-2): yeni cümle kod parçasıyla başlayabilir
            # ("`ORNEKAD[big]`, the …": eş aralıklı ad).
            nxt = rest.lstrip().lstrip("`")
            if not rest[:1].isspace() or not nxt[:1].isupper():
                continue
            if best is None or len(k2) > len(best[0]):
                best = (k2, cut)
        return best

    def _split_caption_tails(self, lines: List[str]) -> List[str]:
        """Şekil altyazısı satırına yapışık gövde satırı ayrı paragraf.

        Marker her paragrafı tek satır yazar; görselin bitişiğindeki altyazı
        satırının altına boş satırsız gelen uzun düz satır altyazının devamı
        değil, sonraki gövde paragrafıdır ("*Figure 8: …*" + "Of the 120
        samples …"). Eskiden ikisi tek blok sayılıyordu: altyazı şekle
        bağlanınca gövde \\caption* içine giriyor, bağlanmayınca (blokta
        dipnot varsa) altyazı ile gövde aynı paragrafta basılıyordu. Araya
        boş satır konur; metin aynen kalır. Kısa devam satırı
        (_CAPTION_TAIL_MIN_CHARS), vurgulu devam ve altyazı cümlesi bitmeden
        küçük harfle süren satır altyazıda kalır. Satır kırpılmış belgede
        (EPUB/Gutenberg: paragraf çok satırlı) uygulanmaz.
        """
        if self._is_hard_wrapped(lines):
            return lines
        out: List[str] = []
        prev: Optional[str] = None   # bir önceki dolu satır
        for i, ln in enumerate(lines):
            out.append(ln)
            s = ln.strip()
            if not s:
                continue
            before, prev = prev, s
            if i + 1 >= len(lines) or (i > 0 and lines[i - 1].strip()):
                continue   # paragrafın ilk satırı değil
            nxt = lines[i + 1].strip()
            if (len(nxt) < _CAPTION_TAIL_MIN_CHARS or nxt[0] in "*_"
                    or self._BLOCK_START_RE.match(nxt)):
                continue
            if before is None or not _IMAGE_LINE_RE.match(before.replace(r"\_", "_")):
                continue
            if not self._is_caption_text(s):
                continue
            complete = bool(re.search(r"[.!?…][\s*_\"'”’»)\]]*$", s))
            first = nxt.lstrip("\"'“‘«„(`")[:1]   # ` : kod parçası (G1a-2)
            if not complete and not (first.isupper() or first.isdigit()):
                continue   # altyazı cümlesi alt satırda sürüyor
            out.append("")
            logger.debug("G1a-1: altyazıya yapışık gövde satırı ayrıldı: %s", nxt[:60])
        return out

    def _planned_level(self, h: _Heading) -> Optional[int]:
        """_try_heading'in bu başlığa vereceği LaTeX düzeyi (düşürülürse None).

        Sıra _try_heading ile aynı: plan kararı (çapa düzeyi aynen), yoksa
        başlıklı bölüm etiketi (1); "# References" türü kısa adın inmesi;
        numaradan düzey.
        """
        d = (self._heading_plan or {}).get(h.idx)
        if d is not None:
            if d.drop:
                return None
            level, title = d.level, d.title or h.title
            if d.anchor:
                return level
        else:
            level, title = h.level, h.title
            if h.label is not None and h.label.kind == "chapter" and h.label.title:
                level, title = 1, h.label.title
        if level == 1 and title.lower() in _NON_CHAPTER_NAMES:
            level = 2
        return self._infer_heading_level(title, level)[0]

    def _plan_titleless_transition(self, lines: List[str], start: int) -> None:
        """Başlıksız (ilk başlıktan önce düzyazı) ve çapasız belgede ön madde
        -> ana madde geçişi. Üç sonuç:

        1. İlk "#" başlığın ardından ön madde başlığı geliyorsa (İçindekiler,
           Önsöz…) o başlık kitap adıdır: eski kural (_titleless_title_idx).
        2. Eski kural ilk bölüm düzeyli başlıkta (T) geçer; öncesindeki
           başlıklar ön maddede numarasız kalır. Rapor gibi belgede Marker
           bölüm başlıklarını "##", alt bölümleri "#" verdiyse T bölüm
           ortasındadır: İçindekiler ve \\mainmatter 2. bölümün ortasına
           düşüyordu (bir raporda: toc_releveler öndeki "##"leri sonra \\chapter
           yapıyor). Kaynak PDF puntosu öndeki ilk başlığı T kadar ya da daha
           büyük gösteriyorsa geçiş o başlığa taşınır (_transition_idx).
           Öndeki başlıklardan biri numaralıysa ("7.2 X": önceki bölümün
           devamı), etiketliyse ya da ön madde adıysa taşınmaz.
        3. Taşınmadıysa: ilk "#" belgenin ilk başlığıysa ilk bölümdür (kitap
           adı sanılmaz; eskiden geçiş ikinci "#"e kayıyordu), değilse eski
           kural (kitap adı) geçerli.
        """
        if not getattr(self, "_text_before_first_heading", False):
            return
        heads = [h for h in self._scan_headings(lines, start)
                 if self._planned_level(h) is not None]

        def front_name(h: _Heading) -> bool:
            return (self._is_front_matter_heading(h.title)
                    or _norm_name(h.title) in FRONT_MATTER_HEADING_NAMES)

        # Düzyazıdan sonra gelen ilk "#" başlığın ardından (sonraki "#" dahil)
        # ön madde başlığı geliyorsa ("# Kitap Adı" + "## Contents" ya da
        # "# Preface": kapakta tanıtım yazısı olan kitap) o başlık kitap
        # adıdır; eski kural geçerli (_try_heading).
        def md_level(h: _Heading) -> int:
            d = (self._heading_plan or {}).get(h.idx)
            return d.level if d is not None else h.level

        first_h1 = next((k for k, h in enumerate(heads) if md_level(h) == 1), None)
        if first_h1 is None:
            return
        for h in heads[first_h1 + 1:]:
            if front_name(h) or _is_contents_heading(h.title):
                self._titleless_title_idx = heads[first_h1].idx
                return
            if md_level(h) == 1:
                break
        if self._move_titleless_transition(heads, front_name):
            return
        # Kitap adı mı, ilk bölüm mü? Kanıt PDF puntosu: ilk "#" sonraki "#"
        # başlıktan büyükse kitap adı, değilse (aynı bölüm puntosu) bölümdür.
        # Punto yoksa: ilk "#" belgenin ilk başlığıysa bölümdür; öncesinde alt
        # başlıklar varsa (kitap kesiti ya da Marker'ın düzeyleri karışık) eski
        # kural (kitap adı) — yoksa İçindekiler belgenin ortasına, ön madde
        # sanılan alt bölümlerin ardına düşerdi (bir kitap kesitinde ölçüldü).
        sizes = self._pdf_heading_sizes()
        nxt = next((h for h in heads[first_h1 + 1:] if md_level(h) == 1), None)
        s1 = sizes.get(_norm_key(heads[first_h1].title))
        s2 = sizes.get(_norm_key(nxt.title)) if nxt is not None else None
        if s1 is not None and s2 is not None:
            is_title = s1 > s2 + _HEADING_SIZE_TOL
        else:
            is_title = first_h1 > 0
        if is_title:
            self._titleless_title_idx = heads[first_h1].idx

    def _move_titleless_transition(self, heads: List[_Heading], front_name) -> bool:
        """Geçişi öndeki bölüm puntolu başlığa taşı (bkz.
        _plan_titleless_transition); taşındıysa True."""
        trigger = next((h for h in heads if self._planned_level(h) == 1
                        and not self._is_front_matter_heading(h.title)), None)
        if trigger is None:
            return False
        before = [h for h in heads if h.idx < trigger.idx]
        if not before:
            return False
        for h in before:
            num = self._section_number(h.title)
            if (num is not None and "." in num) or h.label is not None or front_name(h):
                return False
        sizes = self._pdf_heading_sizes()
        first_size = sizes.get(_norm_key(before[0].title))
        trigger_size = sizes.get(_norm_key(trigger.title))
        if (first_size is None or trigger_size is None
                or first_size < trigger_size - _HEADING_SIZE_TOL):
            return False
        self._transition_idx = before[0].idx
        logger.info("G1a-1: başlıksız belgede ana madde geçişi öne taşındı: %s "
                    "(eski geçiş: %s)", before[0].title, trigger.title)
        return True

    def _plan_appendices(self, lines: List[str], start: int) -> None:
        """Ek başlıkları (mt okuma MT-23).

        - Kitabın sonundaki bölüm düzeyli ekler (ardından yalnız ek ya da
          arka/ön madde adı gelir) \\appendix ile "Ek A/B/C" olur; ad
          "Appendix:" / "Appendix A -" öneksiz yazılır (etiketi LaTeX basar;
          başlıklı bölüm etiketindeki "Chapter N X" kuralı gibi, bkz.
          _try_heading). Koşul: her ekin adı var (ayraç "Appendices" ya da
          yalın "APPENDIX A" değil) ve harf etiketleri varsa A'dan sırayla
          gider (yoksa LaTeX'in harfi kaynaktakini tutmaz: rakamlı ek, eksik
          harf).
        - Öteki ek başlıkları (bölüm sonu eki, iki bölüm arasındaki ek, ayraç,
          rakamlı ek) numarasız basılır ama İçindekiler'de kalır (KARAR 20/21
          biçimi); ad aynen. Eskiden "1.10.1 Ek: …", "2.11 Ek: …", "Bölüm 4 ·
          Ek: …" çıkıyor, arada kalan ek sonraki bölümlerin numarasını
          kaydırıyordu.
        - Bölüm içi ek, kaynak PDF puntosu bölümün numaralı \\section
          başlıklarıyla aynıysa \\section düzeyine çıkar (Marker "###" verse
          de kaynakta numaralı alt bölümlerin kardeşidir).
        """
        plan = self._heading_plan
        if plan is None:
            return
        heads = [h for h in self._scan_headings(lines, start)
                 if self._planned_level(h) is not None]
        apps = [h for h in heads if _APPENDIX_RE.match(h.title)]
        if not apps:
            return
        level = {h.idx: self._planned_level(h) for h in heads}
        app_ids = {h.idx for h in apps}

        def tail_ok(h: _Heading) -> bool:
            name = _norm_name(h.title)
            return (h.idx in app_ids or name in BACK_MATTER_HEADING_NAMES
                    or name in BIBLIOGRAPHY_HEADING_NAMES
                    or name in FRONT_MATTER_HEADING_NAMES
                    or h.title.lower() in _NON_CHAPTER_NAMES)

        chapters = [h for h in heads if level[h.idx] == 1]
        block: List[_Heading] = []
        for k, h in enumerate(chapters):
            if h.idx in app_ids and all(tail_ok(c) for c in chapters[k:]):
                block = [c for c in chapters[k:] if c.idx in app_ids]
                break
        names: Dict[int, Tuple[Optional[str], str]] = {}
        for h in block:
            m = _APPENDIX_TITLE_RE.match(h.title)
            rest = m.group("rest") if m else ""
            names[h.idx] = (m.group("label") if m else None,
                            re.sub(r"^[\s.:–—\-]+", "", rest).strip())
        labels = [names[h.idx][0] for h in block]
        lettered = (all(lb is not None and len(lb) == 1 and "A" <= lb <= "Z"
                        for lb in labels)
                    and [ord(lb) - ord("A") for lb in labels] == list(range(len(labels))))
        done: Set[int] = set()
        if (block and all(names[h.idx][1] for h in block)
                and (all(lb is None for lb in labels) or lettered)):
            for h in block:
                self._appendix_titles[h.idx] = names[h.idx][1]
                d = plan.get(h.idx)
                plan[h.idx] = (d._replace(level=1) if d is not None
                               else _HeadingDecision(level=1))
                done.add(h.idx)
            logger.info("G1a-1: %d kitap sonu eki \\appendix ile dizilecek", len(block))
        sizes: Optional[Dict[str, float]] = None
        for h in apps:
            lvl = level[h.idx]
            if h.idx in done or lvl is None or lvl < 1:
                continue   # \part düzeyindeki ayraca dokunulmaz
            new = lvl
            if lvl > 2:
                if sizes is None:
                    sizes = self._pdf_heading_sizes()
                if self._appendix_size_is_section(h, heads, level, sizes):
                    new = 2
            d = plan.get(h.idx)
            plan[h.idx] = (d._replace(level=new, unnumbered=True, toc=True)
                           if d is not None else
                           _HeadingDecision(level=new, unnumbered=True, toc=True))

    def _appendix_size_is_section(self, h: _Heading, heads: List[_Heading],
                                  level: Dict[int, Optional[int]],
                                  sizes: Dict[str, float]) -> bool:
        """Bölüm içi ekin PDF puntosu, aynı bölümdeki numaralı \\section
        başlıklarınınkiyle aynı mı (F1-15 toleransı)?"""
        size = sizes.get(_norm_key(h.title)) if sizes else None
        if size is None:
            return False
        k = next(p for p, c in enumerate(heads) if c.idx == h.idx)
        sec: List[float] = []
        for c in reversed(heads[:k]):
            lv = level.get(c.idx)
            if lv is not None and lv <= 1:
                break
            num = self._section_number(c.title)
            if lv == 2 and num is not None and "." in num:
                s = sizes.get(_norm_key(c.title))
                if s is not None:
                    sec.append(s)
        return bool(sec) and all(abs(s - size) <= _HEADING_SIZE_TOL for s in sec)

    def _is_chapter_title_candidate(self, line: str, label_level: int) -> bool:
        """Satır, yalın bölüm etiketinin ardına gelebilecek BÖLÜM BAŞLIĞI mı?

        Numarasız bir markdown başlığı olmalı: numaralı başlık ("2.1 X") alt
        bölümdür, ikinci bir etiket ("Chapter 3") ya da booktabs sızıntısı
        bölüm başlığı değildir.

        *label_level*: etiketin markdown düzeyi (## → 2). Etiketten DAHA DERİN
        başlık ("## Chapter 1" + "### Problem 1.3") etiketin alt başlığıdır;
        aynı ya da daha sığ düzey aday olabilir ("## Chapter 3" + "# Başlık",
        "#### Глава 1" + "## Başlık"). Numaralı soru/alıştırma/örnek başlığı
        ("Problem 1.3", "Übung 2") hiçbir düzeyde aday değildir.
        """
        m = re.match(r"^(#{1,6})\s+(.*)", line.strip())
        if not m or _BOOKTABS_CMD_RE.search(line):
            return False
        if len(m.group(1)) > label_level:
            return False
        title = self._clean_heading_title(m.group(2))
        # Hiçbir etiket (yalın ya da başlıklı bölüm/kısım etiketi) başka bir
        # etiketin başlığı olamaz: "Chapter 3 Foo" kendi bölümüdür.
        if not title or parse_heading_label(title) is not None:
            return False
        if self._section_number(title) is not None:
            return False
        if self._NUMBERED_EXERCISE_RE.match(title):
            return False
        return True

    def _infer_heading_level(self, clean_title: str, md_level: int) -> tuple[int, str]:
        """
        Infer the correct LaTeX heading level from section numbers in the title.

        If the title starts with a section number (e.g., "2.2.3 Flow Rates"),
        use the number depth to determine the level:
          "2"     -> chapter (1)
          "2.2"   -> section (2)
          "2.2.3" -> subsection (3)
          "2.2.3.1" -> subsubsection (4)

        Returns (level, display_title) where display_title keeps the number.
        """
        num_str = self._section_number(clean_title)
        if num_str is not None:
            depth = num_str.count(".") + 1  # "2.2.3" -> 3 dots-1 = 3 parts
            # Only override level for multi-part numbers (X.Y, X.Y.Z)
            # Single numbers like "5." are ambiguous (could be list item)
            if depth == 1 and "." not in num_str:
                return md_level, clean_title  # Keep markdown level
            # Map depth to heading level (capped at 6)
            inferred = min(depth, 6)
            return inferred, clean_title
        # No section number -- trust markdown level
        return md_level, clean_title

    def _try_heading(
        self, line: str, next_nonblank: Optional[str] = None,
        line_idx: Optional[int] = None,
    ) -> Optional[str]:
        """Return LaTeX heading command or None.

        *next_nonblank*: bir sonraki dolu markdown satırı (convert() verir).
        Yalın "Chapter N" etiketinin ardından bölüm başlığı geliyor mu diye
        bakılır; None ise etiket eski davranışla kendi seviyesinde kalır.

        *line_idx*: satırın convert() içindeki indeksi. Yapı planında bu
        satır için karar varsa (bkz. _plan_structure) düzey/başlık oradan
        gelir; plan yoksa eski satır-satır kurallar uygulanır.
        """
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if not m:
            return None

        # B4 fix: Marker PDF'den TOC'u booktabs tabular'a dönüştürdüğünde
        # \toprule / \midrule / \bottomrule gibi komutlar markdown başlık
        # satırına sızabiliyor. Bu satırları heading olarak parse etmek,
        # MT'nin "toprule Chapter" gibi saçmalıkları çevirmesine yol açıyor.
        # Booktabs/hline komutu içeren satır heading DEĞİLDİR — None dön.
        if _BOOKTABS_CMD_RE.search(line):
            return None

        md_level = len(m.group(1))
        clean_title = self._clean_heading_title(m.group(2))

        plan = getattr(self, "_heading_plan", None)
        decision = None
        if plan is not None and line_idx is not None:
            decision = plan.get(line_idx)

        forced_chapter = False
        anchor = False
        if decision is not None:
            # 2026-09-25 (Y1): yapı planının kararı (bölüm/kısım çapası,
            # etiket birleştirme, bitişik çapraz düzey tekrar, ön madde
            # kitap/yazar adı, etiketsiz üst başlığın indirilmesi).
            if decision.drop:
                return ""
            md_level = decision.level
            if decision.title is not None:
                clean_title = decision.title
            anchor = decision.anchor
            forced_chapter = anchor and md_level <= 1
        elif plan is None:
            # 2026-09-24: yalın bölüm ETİKETİ ("## Chapter 2") + hemen ardından
            # gelen numarasız başlık ("## Motion in a Plane")
            # = TEK \chapter{Başlık}. Marker ikisini AYNI seviyede verdiğinde eski
            # kod \section{Chapter 2} + \section{Başlık} üretiyor, 2. bölüm
            # içindekilerde "1.11 Bölüm 2" olarak 1. bölümün altına düşüyordu
            # (bir test kitabının koşusunda ölçüldü). Etiket
            # yutulur, sonraki başlık bölüm seviyesine zorlanır. Ardından başlık
            # gelmiyorsa (numaralı alt bölüm / gövde metni / daha derin düzeyde
            # başlık / numaralı soru-alıştırma) eski davranış korunur.
            # (Plan varken aynı kural _plan_structure'da uygulanır.)
            bare = self._bare_chapter_label(clean_title)
            if (bare is not None and next_nonblank is not None
                    and self._is_chapter_title_candidate(next_nonblank, md_level)):
                self._pending_chapter_label = str(bare.value)
                return ""

            if getattr(self, "_pending_chapter_label", None) is not None:
                # Önceki başlık satırı yalın etiketti: bu başlık gerçek bölümdür.
                forced_chapter = True
                md_level = 1
                self._pending_chapter_label = None

        if decision is None:
            # Strip "Chapter N" prefix (LaTeX adds it automatically).
            # 2026-09-25 (Y1): altı dilde etiket + başlık ("Kapitel 1 Grundlagen",
            # "CHAPITRE III. Le Gun-Club"); plan varken karar plandan gelir.
            label = parse_heading_label(clean_title)
            if label is not None and label.kind == "chapter" and label.title:
                clean_title = label.title
                md_level = 1  # Force chapter level

        # 2026-09-25 (R18): kaynakça başlığı numarasız basılır ("2.4 Kaynakça"
        # çıkıyordu); ad eşlemesi alt ad eklenmeden önceki başlıkla yapılır.
        # KARAR 20 (2026-09-29): kaynakça KAYNAKTAKİ GİBİ — kaynakta numarasız
        # ("References") numarasız ama İçindekiler'de (toc_entry); kaynakta
        # numaralı ("2.4 References": ad eşlemesine girmez) eskisi gibi
        # numaralı başlıktır, İçindekiler'e zaten girer.
        # KARAR 21 (2026-09-29): harfli başlık ("(a) The first phase",
        # "*a*) OBSERVACIÓN", "2.1 a) X"): kaynaktaki harf/etiket metinde
        # kalır, LaTeX numarası kalkar (numarasız), İçindekiler'de kalır;
        # eskiden "1.2.1 (a) The first phase" çift numara basılıyordu.
        force_star = bool(decision is not None and decision.unnumbered)
        # 2026-10-05 (G1a-1, MT-23): planın numarasız + İçindekiler kararı
        # (ek başlıkları, bkz. _plan_appendices).
        toc_entry = bool(decision is not None and decision.toc)
        toc_max_level = 6
        if _norm_name(clean_title) in BIBLIOGRAPHY_HEADING_NAMES:
            force_star = True
            toc_entry = True
            # "Her durumda İçindekiler'de": Marker'ın \subsubsection'a
            # indirdiği bölüm sonu kaynakçası kitabın içindekiler
            # derinliğinde (subsection) görünsün.
            toc_max_level = _BIB_TOC_MAX_LEVEL
        elif not anchor and _LETTER_LABEL_HEADING_RE.match(clean_title):
            force_star = True
            toc_entry = True

        # 2026-09-25 (R8b): EPUB bölüm alt adı ("başlık — alt ad").
        suffix = (self._heading_suffix.get(line_idx)
                  if line_idx is not None else None)
        if suffix:
            clean_title = f"{clean_title}{_TITLE_JOIN_SEP}{suffix}"

        # Non-structural headings: short notes/remarks shouldn't be chapters
        # (etiketle zorlanmış bölüm başlığı bu kuraldan muaftır).
        if (clean_title.lower() in _NON_CHAPTER_NAMES and md_level == 1
                and not forced_chapter and not anchor):
            md_level = 2  # Demote to section

        # Dedup (2026-07-11, #4): kitap-KURESEL dedup ARDIŞIK dedup'a
        # daraltildi. Eski hali ayni baslik metnini kitap boyunca bir kez
        # kabul ediyordu — her bolumde tekrarlayan mesru basliklar
        # ('Exercises', 'Ozet', 'Problems') ikinci bolumden itibaren
        # SESSIZCE siliniyordu. Marker'in gercek dup deseni BITISIK tekrar
        # oldugu icin yalniz ardisik ayni-seviye tekrar elenir; capraz-seviye
        # dup (\section{X}+\chapter{X}) sanitizer P1'in isi.
        # _seen_headings yalniz baslik-sayfasi girisini tutar (kitap adinin
        # govdede tekrar basilmamasi icin) — o kontrol korunur.
        norm_title = re.sub(r"\*\*(.+?)\*\*", r"\1", clean_title).strip()
        heading_key = f"{md_level}:{norm_title}"
        if heading_key in self._seen_headings:
            return ""  # baslik sayfasinda zaten islendi
        if heading_key == getattr(self, "_last_heading_key", None):
            return ""  # bitisik Marker dup'i
        self._last_heading_key = heading_key

        # Store book title from first # heading
        # (etiketin ardındaki başlık kitap adı DEĞİL, gerçek bölümdür; kitap
        # adı sanılırsa mainmatter geçişi tetiklenmez -> bölüm numarasız
        # kalır ve içindekiler basılmaz.)
        # 2026-10-05 (G1a-1): ilk başlıktan önce düzyazı varsa belge
        # başlıkla başlamıyor (rapor, makale): ilk "#" başlık kitap adı DEĞİL,
        # ilk bölümdür (bkz. _build_title_page) — ardından ön madde başlıkları
        # (İçindekiler, Önsöz…) gelmiyorsa (bkz. _plan_titleless_transition).
        if (md_level == 1 and not self._book_title and not forced_chapter
                and (not getattr(self, "_text_before_first_heading", False)
                     or line_idx == getattr(self, "_titleless_title_idx", None))):
            self._book_title = clean_title

        # Infer correct level from section number (çapa düzeyi plandan gelir;
        # kısım \part'tır, numara çıkarımına girmez).
        if anchor:
            level, display_title = md_level, clean_title
        else:
            level, display_title = self._infer_heading_level(clean_title, md_level)

        # Convert inline formatting in title
        title_tex = self._convert_inline(display_title)

        commands = {
            0: "part",
            1: "chapter",
            2: "section",
            3: "subsection",
            4: "subsubsection",
            5: "paragraph",
            6: "subparagraph",
        }
        cmd = commands.get(level, "subparagraph")

        # For mid-book extractions: always use starred (unnumbered) since
        # the original section numbers are already in the heading text
        is_mid_book = not self._in_frontmatter and self._frontmatter_ended is False
        use_star = is_mid_book or self._in_frontmatter

        # Front matter handling for book class
        result_prefix = ""

        if self._in_frontmatter:
            # Check if this heading triggers mainmatter transition
            transition = self._maybe_transition_to_mainmatter(
                clean_title, level, line_idx=line_idx)
            if transition:
                result_prefix = transition
                use_star = False  # Real chapters get numbering

            # Still in frontmatter? Use starred (unnumbered) version
            if self._in_frontmatter and level <= 2:
                return f"{result_prefix}\\{cmd}*{{{title_tex}}}"

        # 2026-10-05 (G1a-1): kitap sonu eki — ilkinden önce
        # \appendix (LaTeX "Ek A/B" etiketini basar), ad "Appendix:" öneksiz.
        # Ön maddede ya da kitap ortası kesitte (numarasız başlıklar) önek
        # atılmaz: etiket metni kaybolmasın.
        appendix_prefix = ""
        app_title = (self._appendix_titles.get(line_idx)
                     if line_idx is not None else None)
        if app_title is not None and not use_star and not force_star and level <= 1:
            title_tex = self._convert_inline(app_title)
            if not self._appendix_started:
                appendix_prefix = "\\appendix\n\n"
                self._appendix_started = True

        star = "*" if (use_star or force_star) else ""
        toc_tex = ""
        if toc_entry and not use_star and level >= 1:
            # KARAR 20/21: numarasız başlık İçindekiler'de (bkz.
            # _TOC_ENTRY_TEMPLATE). Aynı satırda: başlığı satır başı
            # deseniyle okuyan araçlar (EPUB birimi, çevirmen başlık deseni)
            # ilk ayraç argümanını alır, kuyruğu görmez.
            toc_cmd = commands.get(min(level, toc_max_level), cmd)
            toc_tex = _TOC_ENTRY_TEMPLATE.format(level=toc_cmd)
        return f"{result_prefix}{appendix_prefix}\\{cmd}{star}{{{title_tex}}}{toc_tex}"

    def _resolve_image(self, filename: str) -> str:
        """
        Resolve actual image filename by checking the images directory.

        Marker sometimes references .jpeg but saves .png (or vice versa).
        Try the exact filename first, then alternative extensions.
        """
        img_dir = self.config.images_dir
        if not img_dir.exists():
            return filename

        # Exact match
        if (img_dir / filename).exists():
            return filename

        # Try alternative extensions
        stem = Path(filename).stem
        for ext in [".png", ".jpg", ".jpeg", ".pdf", ".svg"]:
            candidate = stem + ext
            if (img_dir / candidate).exists():
                logger.debug("Image resolved: %s -> %s", filename, candidate)
                return candidate

        # No match found -- return original and let LaTeX handle the error
        logger.warning("Image not found: %s (checked %s)", filename, img_dir)
        return filename

    def _image_width(self, img_ref: str) -> str:
        """R14: küçük (süs) görsel kaynaktaki boyutunda, öbürleri
        ``0.8\\textwidth`` (bkz. _SMALL_IMAGE_MAX_PX). Boyut okunamazsa
        (dosya yok, SVG/PDF) eski genişlik."""
        path = self.config.images_dir / img_ref
        try:
            if not path.is_file():
                return _DEFAULT_IMAGE_WIDTH
            from PIL import Image
            with Image.open(path) as im:
                w, h = im.size
        except Exception:  # noqa: BLE001 - boyut okunamazsa eski genişlik
            return _DEFAULT_IMAGE_WIDTH
        if 0 < max(w, h) <= _SMALL_IMAGE_MAX_PX:
            return f"{w / _MARKER_IMAGE_DPI:.2f}in"
        if w > 0 and _MARKER_IMAGE_NAME_RE.match(img_ref):
            source_in = w / _MARKER_IMAGE_DPI
            if source_in < _DEFAULT_IMAGE_FRAC * self._text_width_in():
                return f"{source_in:.2f}in"
        return _DEFAULT_IMAGE_WIDTH

    def _text_width_in(self) -> float:
        """Metin genişliği (inç): ölçülen yerleşim varsa kâğıt - kenarlar
        (şablon geometrisiyle aynı değerler), yoksa yedek geometri."""
        cfg = self.config

        def _mm(value) -> float:
            m = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*mm\s*$", str(value or ""))
            return float(m.group(1)) if m else 0.0

        if getattr(cfg, "layout_detected", False):
            width = (_mm(cfg.page_width) - _mm(cfg.margin_left)
                     - _mm(cfg.margin_right))
            if width > 0:
                return width / _MM_PER_INCH
        return _FALLBACK_TEXT_WIDTH_MM / _MM_PER_INCH

    def _try_image(self, line: str, line_idx: Optional[int] = None) -> Optional[str]:
        """Convert ![alt](src) to \\begin{figure}...

        *line_idx*: satırın markdown indeksi; _plan_figure_captions bu
        görsele bir altyazı paragrafı bağladıysa altyazı ``\\caption*``
        olarak kaynaktaki yanına (görselin önü/ardı) yazılır (R13).
        """
        # Also match lines ending with \\ (LaTeX line break) or trailing whitespace
        m = re.match(r"^!\[([^\]]*)\]\(([^)]+)\)\s*(?:\\\\)?[\s]*$", line)
        if not m:
            # Try with escaped underscores (\_) that TeX escaping may have introduced
            m = re.match(r"^!\[([^\]]*)\]\(([^)]+)\)\s*(?:\\\\)?[\s]*$",
                         line.replace(r"\_", "_"))
            if not m:
                return None
        alt = m.group(1)
        src = m.group(2)

        # Use just the filename -- \graphicspath handles the directory
        src_path = Path(src)
        img_ref = self._resolve_image(src_path.name)
        width = self._image_width(img_ref)

        glued = (self._figure_captions.get(line_idx)
                 if line_idx is not None else None)
        caption = (f"  \\caption*{{{_protect_href_urls(self._convert_inline(glued[0]))}}}"
                   if glued else None)
        parts = [
            "\\begin{figure}[H]",
            "  \\centering",
        ]
        if glued and glued[1] == "before":
            parts.append(caption)
        parts.append(f"  \\includegraphics[width={width}]{{{img_ref}}}")
        if glued:
            if glued[1] == "after":
                parts.append(caption)
        elif alt:
            parts.append(f"  \\caption{{{_escape_tex(alt)}}}")
        parts.append("\\end{figure}")
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # 2026-10-05 (G1a-2): satır içi biçim — <u>, bölünmüş bağlantılar,
    # vurgu dizileri, eş aralıklı (kod) parçalar
    # ------------------------------------------------------------------

    def _pdf_link_cache(self) -> Dict[str, object]:
        """Bağlantı önbelleği: "pages" -> sayfa metinlerinin _norm_key'i,
        "joined"/"starts" -> aramalık birleşik metin, sayfa no -> o sayfanın
        bağlantı dizini. convert() başına bir kez, PDF bir kez açılarak kurulur
        (6 Eki: sayfa başına yeniden açmak büyük bir kitapta ~33 sn sürüyordu).
        Bağlantı metni karakter akışından okunur (akış yoksa get_textbox)."""
        cache = self._pdf_link_index_cache
        if cache is None:
            cache = self._pdf_link_index_cache = {"pages": [], "joined": "", "starts": []}
            pdf = Path(getattr(self.config, "input_pdf", "") or "")
            if pdf.suffix.lower() == ".pdf" and pdf.is_file():
                try:
                    import fitz  # PyMuPDF
                    doc = fitz.open(str(pdf))
                    try:
                        cache["pages"] = [_norm_key(p.get_text("text")) for p in doc]
                        chars = self._pdf_page_chars(str(pdf))
                        for pno, page in enumerate(doc):
                            if page.first_link is not None:
                                cache[pno] = self._page_link_dict(
                                    fitz, page, chars.get(pno) if chars is not None else None)
                    finally:
                        doc.close()
                except Exception as exc:  # noqa: BLE001 - PDF okunamazsa \uline
                    logger.debug("G1a-2: PDF metni okunamadı (%s): %s", pdf, exc)
                starts, pos = [], 0
                for pk in cache["pages"]:
                    starts.append(pos)
                    pos += len(pk) + 1
                cache["joined"] = "\x00".join(cache["pages"])
                cache["starts"] = starts
        return cache

    @staticmethod
    def _pdf_page_chars(pdf: str) -> Optional[Dict[int, List[Tuple[float, float, str]]]]:
        """Karakter akışından sayfa -> [(merkez x, merkez y, karakter)]; akış
        kurulamazsa None (önbellekli akış: pdf_metin_onarim._pdf_akisi_kur)."""
        try:
            ak = pdf_metin_onarim._pdf_akisi_kur(pdf)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Bağlantı metni için karakter akışı kurulamadı: %s", exc)
            return None
        if ak is None:
            return None
        out: Dict[int, List[Tuple[float, float, str]]] = {}
        for i, c in enumerate(ak.ch):
            out.setdefault(ak.sayfa[i], []).append(
                ((ak.x0[i] + ak.x1[i]) / 2, (ak.y0[i] + ak.y1[i]) / 2, c))
        return out

    def _pdf_page_link_dict(self, pno: int) -> Dict[str, Set[str]]:
        """Bir sayfanın bağlantı dizini (önbellekli; bkz. _page_link_dict)."""
        return self._pdf_link_cache().get(pno, {})

    @staticmethod
    def _page_link_dict(fitz, page, chars: Optional[List[Tuple[float, float, str]]] = None
                        ) -> Dict[str, Set[str]]:
        """Bir sayfanın bağlantıları: _norm_key(metin) -> hedefler.

        Bağlantı metni dikdörtgenin dikeyde daraltılmış hâlinden okunur
        (_LINK_RECT_INSET): tam dikdörtgen üst satıra değip onu da alıyordu
        (üst satırın bir parçası metne giriyordu); sözcük ölçütü ise bitişik
        dipnot üst simgesini sözcüğe katıyordu ("sample27")."""
        def link_text(rect) -> str:
            inset = rect.height * _LINK_RECT_INSET
            r = fitz.Rect(rect.x0, rect.y0 + inset, rect.x1, rect.y1 - inset)
            if chars is not None:
                # Akış karakterleri: merkezi daraltılmış dikdörtgende olan
                # (anahtar _norm_key'dir; boşluk ve sıra dışı ayrıntı önemsiz).
                return "".join(c for x, y, c in chars
                               if r.x0 <= x <= r.x1 and r.y0 <= y <= r.y1)
            return page.get_textbox(r)

        index: Dict[str, Set[str]] = {}
        prev_target, prev_text = None, ""
        for ln in page.get_links():
            kind = ln.get("kind")
            if kind == fitz.LINK_URI and ln.get("uri"):
                target = str(ln["uri"])
            elif kind in (fitz.LINK_GOTO, fitz.LINK_NAMED) and int(ln.get("page", -1)) >= 0:
                target = f"#page-{int(ln['page'])}-0"
            else:
                prev_target = None
                continue
            text = link_text(fitz.Rect(ln["from"]))
            index.setdefault(_norm_key(text), set()).add(target)
            if target == prev_target:
                prev_text = prev_text + " " + text
                index.setdefault(_norm_key(prev_text), set()).add(target)
            else:
                prev_text = text
            prev_target = target
        index.pop("", None)
        return index

    def _pdf_link_target(self, text: str, before: str = "",
                         after: str = "") -> Optional[str]:
        """Altı çizili metin kaynak PDF'te TEK hedefli bir bağlantıysa hedefi.

        Metnin geçtiği sayfa, Markdown'daki çevresiyle (before/after) bulunur:
        bağlam + metin PDF'te TEK sayfada geçmeli (en uzun bağlamdan kısaya;
        birden çok sayfada geçen bağlam belirsizdir, bağlantı kurulmaz). Aynı
        sözcük başka sayfada bağlantı olabilir (bir kitapta aynı sözcük bir
        sayfada bağlantı, üç sayfada düz alt çizgi) — yalnız bu geçişin
        sayfasına bakılır. Yalnız o sayfanın bağlantıları okunur (bütün
        belgenin bağlantı metinlerini okumak büyük bir İngilizce kitapta
        (~2700 bağlantı) 33 sn sürüyordu).
        """
        key = _norm_key(_MARK_RE.sub("", text))
        if not key:
            return None
        pages = self._pdf_link_cache()["pages"]
        if not pages:
            return None
        lk = _norm_key(_MARK_RE.sub("", before))
        rk = _norm_key(_MARK_RE.sub("", after))
        contexts = [lk[-w:] + key + rk[:w] for w in _ULINK_CONTEXT_WINDOWS if lk or rk]
        for w in _ULINK_CONTEXT_WINDOWS:
            if len(lk[-w:]) >= _ULINK_CONTEXT_MIN:
                contexts.append(lk[-w:] + key)
            if len(rk[:w]) >= _ULINK_CONTEXT_MIN:
                contexts.append(key + rk[:w])
        if not lk and not rk:
            contexts.append(key)   # metin satırın tamamı
        for ctx in dict.fromkeys(contexts):
            hits = [p for p, pk in enumerate(pages) if ctx in pk]
            if len(hits) > 1:
                return None        # belirsiz
            if len(hits) == 1:
                targets = self._pdf_page_link_dict(hits[0]).get(key, set())
                return next(iter(targets)) if len(targets) == 1 else None
        return None

    def _stash_underlines(self, text: str, stash) -> str:
        """<u>…</u> -> \\href (kaynak PDF'te bağlantı) ya da \\uline.
        Eskiden etiket metinde görünüyordu (dönüştürücüde <u> hiç
        yoktu). Kod parçası/matematik içindeki etikete dokunulmaz. İçinde
        dipnot işareti varsa yalnız etiket düşer (\\uline içinde \\footnote
        olmaz); eşi olmayan <u>/</u> görünmez."""
        def _spans(s: str):
            return [m.span() for rx in (_DISPLAY_MATH_RE, _INLINE_MATH_RE, _CODE_SPAN_RE)
                    for m in rx.finditer(s)]

        spans = _spans(text)

        def _repl(m: "re.Match[str]") -> str:
            if any(a <= m.start() < b for a, b in spans):
                return m.group(0)
            inner = m.group(1)
            if not inner.strip() or _MARK_SENTINEL + "F" in inner:
                return inner
            body = self._convert_inline(inner)
            target = None
            if "](" not in inner:
                before = _HTML_UNDERLINE_TAG_RE.sub("", text[:m.start()])
                after = _HTML_UNDERLINE_TAG_RE.sub("", text[m.end():])
                target = self._pdf_link_target(inner, before, after)
            if target:
                return stash("\\href{" + target + "}{" + body + "}")
            return stash("\\uline{" + body + "}")

        out = _HTML_UNDERLINE_RE.sub(_repl, text)
        spans = _spans(out)
        return _HTML_UNDERLINE_TAG_RE.sub(
            lambda m: m.group(0) if any(a <= m.start() < b for a, b in spans) else "",
            out)

    @staticmethod
    def _merge_split_links(text: str) -> str:
        """Aynı hedefe giden, arada yalnız boşluk olan ardışık bağlantılar tek
        bağlantı olur (bkz. _MD_LINK_RE, _split_link_joiner). İki atıf
        numarası, farklı hedef, virgülle ayrılmış bağlantılar ayrı kalır.
        Kod parçası/matematik içindeki eşleşmeye dokunulmaz."""
        if text.count("](") < 2:
            return text
        changed = True
        while changed:
            changed = False
            spans = [m.span() for rx in (_DISPLAY_MATH_RE, _INLINE_MATH_RE, _CODE_SPAN_RE)
                     for m in rx.finditer(text)]
            links = list(_MD_LINK_RE.finditer(text))
            for m1, m2 in zip(links, links[1:]):
                url = m1.group(2)
                sep = text[m1.end():m2.start()]
                if url != m2.group(2) or sep.strip(" \t"):
                    continue
                if any(a <= m1.start() < b or a <= m2.start() < b for a, b in spans):
                    continue
                a, b = m1.group(1), m2.group(1)
                if _CITATION_LABEL_RE.match(a) and _CITATION_LABEL_RE.match(b):
                    continue
                joiner = _split_link_joiner(a, b, url, sep)
                text = (text[:m1.start()] + "[" + a + joiner + b + "](" + url + ")"
                        + text[m2.end():])
                changed = True
                break
        return text

    def _widen_narrow_links(self, text: str) -> str:
        """Dar bağlantı PDF'teki bağlantı metnine genişler (2026-10-05).

        Marker bağlantıyı tek sözcüğe indirir: "see the [summary](#page-8-0)
        table of all results" (kaynakta bütün öbek bağlantı; bir test
        kitabında 222 bağlantının 152'si dar). Bağlantının sayfası bağlamla
        bulunur (tek sayfada geçmeli); o sayfada aynı hedefe giden bir PDF
        bağlantı metni etiketi kapsıyorsa ve etiketin solundaki/sağındaki
        sözcükler onun eksik kalan iki ucuyla birebir eşleşiyorsa (_norm_key)
        bu sözcükler bağlantının içine alınır. Uçtaki noktalama dışarıda
        kalır; vurgu işareti, başka bağlantı, etiket ya da matematik taşıyan
        sözcük taşınmaz. Metin değişmez, yalnız bağlantının kapsamı."""
        if not self._kaynak_pdf_var() or "](" not in text:
            return text
        if not self._pdf_link_cache()["pages"]:
            return text
        spans = [m.span() for rx in (_DISPLAY_MATH_RE, _INLINE_MATH_RE, _CODE_SPAN_RE)
                 for m in rx.finditer(text)]
        pos = 0
        while True:
            links = [m for m in _MD_LINK_RE.finditer(text, pos)
                     if not any(a <= m.start() < b for a, b in spans)]
            if not links:
                return text
            m = links[0]
            prev_end = max([e for s, e in (l.span() for l in _MD_LINK_RE.finditer(text))
                            if e <= m.start()], default=0)
            nxt = links[1].start() if len(links) > 1 else len(text)
            new = self._widen_one_link(m.group(1), m.group(2), text[prev_end:m.start()],
                                       text[m.end():nxt])
            if new is None:
                pos = m.end()
                continue
            nb, na = new
            before, after = text[prev_end:m.start()], text[m.end():nxt]
            label = before[len(before) - nb:] + m.group(1) + after[:na]
            link = "[" + label + "](" + m.group(2) + ")"
            text = (text[:prev_end] + before[:len(before) - nb] + link + after[na:]
                    + text[nxt:])
            spans = [s.span() for rx in (_DISPLAY_MATH_RE, _INLINE_MATH_RE, _CODE_SPAN_RE)
                     for s in rx.finditer(text)]
            pos = prev_end + len(before) - nb + len(link)

    def _widen_one_link(self, label: str, url: str, before: str,
                        after: str) -> Optional[Tuple[int, int]]:
        """(soldan taşınacak karakter, sağdan taşınacak karakter) ya da None."""
        key = _norm_key(label)
        if not key:
            return None
        page = self._link_page(key, before, after)
        if page is None:
            return None
        cands = [k for k, targets in self._pdf_page_link_dict(page).items()
                 if key in k and k != key and k.count(key) == 1
                 and any(_same_link_target(t, url) for t in targets)]
        for cand in sorted(cands, key=len, reverse=True):
            left, right = cand.split(key)
            nb = _take_link_words(before, left, from_end=True)
            na = _take_link_words(after, right, from_end=False)
            if nb is not None and na is not None and (nb or na):
                return nb, na
        return None

    def _link_page(self, key: str, before: str, after: str) -> Optional[int]:
        """Bağlantının PDF sayfası: bağlam + etiket TEK sayfada geçmeli
        (en uzun bağlamdan kısaya; bkz. _pdf_link_target)."""
        lk = _norm_key(_MD_LINK_URL_RE.sub("]", before))
        rk = _norm_key(_MD_LINK_URL_RE.sub("]", after))
        contexts = [lk[-w:] + key + rk[:w] for w in _ULINK_CONTEXT_WINDOWS if lk or rk]
        for w in _ULINK_CONTEXT_WINDOWS:
            if len(lk[-w:]) >= _ULINK_CONTEXT_MIN:
                contexts.append(lk[-w:] + key)
            if len(rk[:w]) >= _ULINK_CONTEXT_MIN:
                contexts.append(key + rk[:w])
        cache = self._pdf_link_cache()
        joined, starts = cache["joined"], cache["starts"]
        for ctx in dict.fromkeys(contexts):
            i = joined.find(ctx)
            if i < 0:
                continue
            page = bisect.bisect_right(starts, i) - 1
            nxt = starts[page + 1] if page + 1 < len(starts) else len(joined)
            if joined.find(ctx, nxt) >= 0:
                return None        # başka sayfada da geçiyor: belirsiz
            return page
        return None

    @staticmethod
    def _pdf_mono_candidates(pdf: str, markdown: str) -> bool:
        """Hızlı ön denetim: eş aralıklı yazı tipindeki bir PDF parçası
        Markdown'ın kod bloğu dışındaki metninde geçiyor mu? Yalnız yazı tipi
        listesinde eş aralıklı yazı tipi olan sayfaların metni okunur. Yoksa
        karakter akışı hiç kurulmaz (büyük bir test kitabında eş aralıklı
        satırların hepsi kod bloğunda; akış 5 sn sürüyordu)."""
        import fitz  # PyMuPDF
        plain = re.sub(r"\s+|\\", "", _CODE_FENCE_BLOCK_RE.sub(" ", markdown))
        doc = fitz.open(pdf)
        try:
            for page in doc:
                if not any(_MONO_FONT_RE.search(f"{f[3]} {f[4]}") for f in page.get_fonts()):
                    continue
                for block in page.get_text("dict").get("blocks", []):
                    for line in block.get("lines", []):
                        for span in line.get("spans", []):
                            key = re.sub(r"\s+", "", span.get("text", ""))
                            if (len(key) >= _MONO_PROBE_MIN_CHARS
                                    and _MONO_FONT_RE.search(str(span.get("font", "")))
                                    and key in plain):
                                return True
        finally:
            doc.close()
        return False

    def _wrap_pdf_monospace(self, markdown: str) -> str:
        """Kaynak PDF'te eş aralıklı yazı tipindeki satır içi parçaları
        Markdown kod parçasına (`…`) al.

        Marker yazı tipini bilmez; özel adlar, makine dizgileri ve commit/PR
        metinleri düz yazıya iniyordu (makine dizgisi alıntılayan bir raporda
        kaynakta 262 satır içi eş aralıklı parça; elle onarımda 66 ad + 32
        satır düzeltildi). Yöntem:
        - PDF karakter akışı ve Markdown akışı G1c'nin onarım modülüyle
          (pdf_metin_onarim) kurulur; parça Markdown'a BENZERSİZ bağlamla
          eşlenir (iki metinde de tek geçen bağlam); eşlenemeyen dokunulmaz.
        - Eş aralıklı yazı tipi ailesi düzyazı ağırlıklıysa (düzyazı alıntısı)
          alıntı yazı tipidir: yalnız kod gibi tek belirteçler sarılır
          (_MONO_QUOTE_FAMILY_SHARE).
        - Tırnak ve sondaki noktalama dışarıda kalır; parça tek satırda,
          biçim işareti (vurgu, bağlantı adresi, etiket, matematik) taşımayan
          düz metin olmalıdır. Kod bloğu ve mevcut kod parçası dışlanır.
        PDF yoksa, eş aralıklı yazı tipi yoksa ya da metin katmanı yoksa
        Markdown aynen döner. Metin eklenmez, silinmez (yalnız ` işareti).
        """
        pdf = Path(getattr(self.config, "input_pdf", "") or "")
        if pdf.suffix.lower() != ".pdf" or not pdf.is_file():
            return markdown
        try:
            from latex_pipeline import pdf_metin_onarim as pmo
            if not self._pdf_mono_candidates(str(pdf), markdown):
                return markdown
            ak = pmo._pdf_akisi_kur(str(pdf))
            if ak is None:
                return markdown
            mk = pmo._md_akisi_kur(markdown)
            norm_char = pmo._norm_karakter
        except Exception as exc:  # noqa: BLE001 - tarama yoksa Markdown aynen
            logger.debug("G1a-2: eş aralıklı yazı tipi taraması atlandı: %s", exc)
            return markdown

        mono_cache: Dict[str, bool] = {}

        def is_mono(raw_idx: int) -> bool:
            font = ak.font[raw_idx]
            if font not in mono_cache:
                mono_cache[font] = bool(_MONO_FONT_RE.search(font))
            return mono_cache[font]

        # Eş aralıklı parçalar (normalleştirilmiş akışta, aynı sayfada).
        runs: List[Tuple[int, int]] = []
        n = len(ak.norm)
        i = 0
        while i < n:
            if not is_mono(ak.kaynak[i]):
                i += 1
                continue
            j = i
            page = ak.sayfa[ak.kaynak[i]]
            while j < n and is_mono(ak.kaynak[j]) and ak.sayfa[ak.kaynak[j]] == page:
                j += 1
            runs.append((i, j))
            i = j

        def raw_of(a: int, b: int) -> str:
            return "".join(ak.ch[ak.kaynak[a]:ak.kaynak[b - 1] + 1])

        families: Dict[str, List[int]] = {}
        for a, b in runs:
            fam = _mono_font_family(ak.font[ak.kaynak[a]])
            st = families.setdefault(fam, [0, 0])
            st[0] += 1
            st[1] += _mono_is_prose(raw_of(a, b))
        quote = {f for f, (cnt, prose) in families.items()
                 if prose >= _MONO_QUOTE_FAMILY_SHARE * cnt}

        def edge_chars(start: int, step: int) -> str:
            """Parçanın ucundaki, akışta yok sayılan eş aralıklı karakterler
            ("ORNEKAD[big]" -> "]"); Markdown aralığına katılır."""
            out = []
            r = start
            while 0 <= r < len(ak.ch):
                c = ak.ch[r]
                if c.isspace() or norm_char(c) or not is_mono(r):
                    break
                out.append(c)
                r += step
            return "".join(out)

        edits: List[Tuple[int, int]] = []
        skipped = 0
        for a, b in runs:
            raw = raw_of(a, b)
            if (_mono_font_family(ak.font[ak.kaynak[a]]) in quote
                    and not _mono_is_token(raw)):
                continue
            try:
                j = pmo._pdf_den_md_ye(ak, mk, a, b)
            except Exception:  # noqa: BLE001
                j = None
            if j is None or mk.norm[j:j + (b - a)] != ak.norm[a:b]:
                skipped += 1
                continue
            mb, me = mk.bas[j], mk.son[j + (b - a) - 1]
            lead = edge_chars(ak.kaynak[a] - 1, -1)
            trail = edge_chars(ak.kaynak[b - 1] + 1, 1)
            mb, me = self._mono_extend(markdown, mb, me, lead, trail)
            rng = self._mono_trim(markdown, mb, me)
            if rng is None or not self._mono_range_ok(markdown, *rng):
                skipped += 1
                continue
            edits.append(rng)

        if not edits:
            return markdown
        out = markdown
        last = len(out) + 1
        done = 0
        for mb, me in sorted(set(edits), reverse=True):
            if me > last:
                continue   # çakışan aralık
            out = out[:mb] + "`" + _md_unescape(out[mb:me]) + "`" + out[me:]
            last = mb
            done += 1
        logger.info("G1a-2: eş aralıklı satır içi parça \\texttt: %d (eşlenemeyen %d)",
                    done, skipped)
        return out

    @staticmethod
    def _mono_extend(md: str, mb: int, me: int, lead: str, trail: str) -> Tuple[int, int]:
        """Aralığı PDF parçasının uçtaki yok sayılan karakterleriyle genişlet
        (Markdown'da kaçışlı olabilir: "\\[")."""
        for c in reversed(lead):
            if mb >= 1 and md[mb - 1] == c:
                mb -= 1
            elif mb >= 2 and md[mb - 2:mb] == "\\" + c:
                mb -= 2
            else:
                break
        for c in trail:
            if md[me:me + 1] == c:
                me += 1
            elif md[me:me + 2] == "\\" + c:
                me += 2
            else:
                break
        return mb, me

    @staticmethod
    def _mono_trim(md: str, mb: int, me: int) -> Optional[Tuple[int, int]]:
        """Tırnak, boşluk, sondaki noktalama ve eşsiz uç ayraç dışarıda kalır."""
        changed = True
        while changed and mb < me:
            changed = False
            if md[mb].isspace() or md[mb] in _MONO_EDGE_QUOTES:
                mb += 1
                changed = True
                continue
            if md[me - 1].isspace() or md[me - 1] in _MONO_EDGE_QUOTES + _MONO_TAIL_PUNCT:
                me -= 1
                if me > mb and md[me - 1] == "\\":
                    me -= 1     # kaçışlı noktalama ("\.") bütün dışarıda
                changed = True
                continue
            seg = md[mb:me]
            for op, cl in (("(", ")"), ("[", "]")):
                if seg.startswith(op) and seg.count(op) > seg.count(cl):
                    mb += 1
                    changed = True
                    break
                if seg.startswith("\\" + op) and seg.count(op) > seg.count(cl):
                    mb += 2
                    changed = True
                    break
                if seg.endswith(cl) and seg.count(cl) > seg.count(op):
                    me -= 2 if seg.endswith("\\" + cl) else 1
                    changed = True
                    break
        if mb >= me or not any(c.isalnum() for c in md[mb:me]):
            return None
        return mb, me

    @staticmethod
    def _mono_range_ok(md: str, mb: int, me: int) -> bool:
        """Aralık tek satırda, biçim işareti taşımayan düz metin mi?"""
        seg = md[mb:me]
        if not seg or "\n" in seg or "`" in seg:
            return False
        if re.search(r"[$<\x00-\x08]|\]\(|!\[", seg):
            return False
        if re.search(r"(?<!\\)[*_]", seg):
            return False   # vurgu işareti (Marker düz '_' ve '*'yı kaçışlar)
        if md[mb - 1:mb] in ("\\", "`") or md[me:me + 1] == "`":
            return False
        line_start = md.rfind("\n", 0, mb) + 1
        return md.count("`", line_start, mb) % 2 == 0   # mevcut kod parçası içinde değil

    def _split_wrapped_emphasis(self, lines: List[str]) -> List[str]:
        """Satırlara bölünmüş paragrafta (EPUB/Gutenberg) satır sonunu aşan
        vurgu her satırda kapanıp açılır: "*First part of" / "the phrase and"
        / "ends here*," -> "*First part of*" / "*the phrase and*" / "*ends
        here*,". Satırlar tek tek dönüştürüldüğü için eskiden "*" işaretleri
        düz basılıyordu (üç EPUB test kitabında). Eşleşme CommonMark
        kuralıyla bütün paragrafta aranır; satır sayısı ve metin değişmez.
        """
        out = list(lines)
        i = 0
        n = len(lines)
        while i < n:
            if not self._plain_paragraph_line(lines[i]):
                i += 1
                continue
            j = i
            while j < n and self._plain_paragraph_line(lines[j]):
                j += 1
            if j - i >= 2 and any("*" in ln for ln in lines[i:j]):
                block = "\n".join(lines[i:j])
                tree = _cm_emphasis_tree(block)
                if any(isinstance(nd, tuple) and "\n" in _cm_render_markdown([nd])
                       for nd in tree):
                    new = _cm_render_markdown(tree).split("\n")
                    if len(new) == j - i:
                        out[i:j] = new
            i = j
        return out

    def _plain_paragraph_line(self, line: str) -> bool:
        """convert() döngüsünde düz paragraf satırı olarak işlenecek satır mı?"""
        s = line.strip()
        return bool(s) and not self._BLOCK_START_RE.match(s) and not s.startswith(
            (_MARK_SENTINEL, ALTAD_MARKER)) and not _IMAGE_LINE_RE.match(s)

    def _stash_marker_escaped_links(self, text: str, stash) -> str:
        """F1-05: Marker kaçışlı bağ metnini \\href'e çevir ve stash'le.

        Marker bağ metnindeki parantez/köşeli ayracı kaçışlar ("\\(", "\\)",
        "\\[", "\\]"), alt çizgiyi "\\\\_" yazar. Eskiden metin olduğu gibi
        \\href'e giriyordu: "\\href{#page-12-0}{\\(2009}" matematik açıyor
        ("Missing $ inserted"), "{2009\\)}" "Bad math environment delimiter",
        "{\\[101,}" sanitizer'ın bozuk bağ onarımında "Too many }'s"
        veriyordu; "\\\\_" ise italik kalıbına takılıyordu. Kaçışlar düz
        karaktere çözülür; metnin ucundaki EŞLEŞMEMİŞ ayraç bağın dışına
        alınır ("(\\href{…}{2009}", "\\href{…}{2009})", "[\\href{…}{63}]"),
        dengeli olan içeride kalır ("{(6.3)}"). Metin normal satır içi
        dönüşümden geçer; adres eskisi gibi olduğu gibi yazılır (dengeli
        parantezli adres, "…Sample_Topic_(physics)", bütün alınır).
        Matematik ya da kod içindeki eşleşmeye dokunulmaz.
        """
        spans = [m.span() for rx in (_DISPLAY_MATH_RE, _INLINE_MATH_RE,
                                     _CODE_SPAN_RE)
                 for m in rx.finditer(text)]

        def _inside(pos: int) -> bool:
            return any(a <= pos < b for a, b in spans)

        def _repl(m: "re.Match[str]") -> str:
            if _inside(m.start()) or _inside(m.end() - 1):
                return m.group(0)
            label = _unescape_marker_link_text(m.group("text"))
            pre, label, post = _split_link_edge_brackets(label)
            url = m.group("url")
            if url.startswith("\\#"):
                url = url[1:]   # Marker "(\#page-…)" (eski 7. adımdaki gibi)
            # Dışarı alınan ayraçlar da stash'lenir: sonraki bağ kalıbı
            # "[" + bağ + "[105](…)" dizisini tek bağ sanmasın.
            return ((stash(pre) if pre else "")
                    + stash("\\href{" + url + "}{"
                            + self._convert_inline(label) + "}")
                    + (stash(post) if post else ""))

        return _MARKER_ESCAPED_LINK_RE.sub(_repl, text)

    def _convert_inline(self, text: str) -> str:
        """
        Convert inline Markdown formatting to LaTeX.

        Strategy:
        1. Stash all "protected" spans (math, code) as placeholders.
        2. Convert Markdown formatting (bold, italic, etc.) to LaTeX.
        3. Stash the LaTeX commands as placeholders too.
        4. Escape TeX specials in the remaining plain text.
        5. Restore all placeholders in reverse order.
        """
        # Accumulator for ALL protected spans
        protected: List[str] = []

        def _stash(content: str) -> str:
            idx = len(protected)
            protected.append(content)
            return f"\x00PROT_{idx}\x00"

        out = text
        # Step 0a (G1a-2): aynı hedefe ardışık (bölünmüş) bağlantılar
        # tek bağlantı olur (bkz. _merge_split_links); dar bağlantı PDF'teki
        # bağlantı metnine genişler (bkz. _widen_narrow_links).
        if "](" in out:
            out = self._merge_split_links(out)
            widened = self._widen_narrow_links(out)
            if widened != out:
                # Genişleyen iki satırlık bağlantının parçaları artık bitişik
                # ("[replace the](#p) [sample target](#p)").
                out = self._merge_split_links(widened)
        # Step 0b (G1a-2): <u>…</u> -> \href / \uline.
        if "<" in out:
            out = self._stash_underlines(out, _stash)

        # Step 0 (F1-05): Marker kaçışlı bağ metni ("[\(2009](#page-12-0))",
        # "[2009\)](#p)", "[\[ 63](#p)]", "[…Sample\\_Topic\\_\(x\)](url)")
        # biçim kalıplarından ÖNCE \href'e çevrilir (bkz.
        # _stash_marker_escaped_links).
        if "](" in out:
            out = self._stash_marker_escaped_links(out, _stash)

        # Step 1a: Stash display math ($$...$$)
        # F1-05: uzunluk olmayan "\\ [" -> "\\{} [" (bkz. _fix_math_linebreak_bracket)
        out = _DISPLAY_MATH_RE.sub(
            lambda m: _stash(_fix_math_linebreak_bracket(m.group(0))), out)

        # Step 1b: Stash inline math ($...$)
        out = _INLINE_MATH_RE.sub(
            lambda m: _stash(_fix_math_linebreak_bracket(m.group(0))), out)

        # Step 1b' (F1-09): EPUB kaçışlı ters bölü ("\\"), ters tırnak ("\`")
        # ve "(" önündeki köşeli ayraç ("\]") düz karakter; kod parçası ve
        # bağlantı kalıpları onları görmesin (bkz. _MD_LITERAL_ESCAPE_RE).
        out = _stash_literal_escapes(out, _stash)

        # Step 1c: Stash inline code (`...`)
        # 2026-10-05 (G1a-2): uzun kod parçasında "\_" önünde satır
        # kırılabilir (bkz. _code_span_tex).
        out = re.sub(
            r"`([^`]+)`",
            lambda m: _stash(_code_span_tex(m.group(1))),
            out,
        )

        # Step 1d: Replace Unicode math symbols BEFORE formatting (bold/italic)
        # Critical: must run before Step 2 so symbols inside *italic* get converted
        _MATH_UNICODE = {
            "∗": "$*$", "∏": "$\\prod$", "∑": "$\\sum$", "∫": "$\\int$",
            "∂": "$\\partial$", "∇": "$\\nabla$", "∞": "$\\infty$",
            "≤": "$\\le$", "≥": "$\\ge$", "≈": "$\\approx$", "≡": "$\\equiv$",
            "≠": "$\\ne$", "±": "$\\pm$", "∓": "$\\mp$", "×": "$\\times$",
            "÷": "$\\div$", "∈": "$\\in$", "∉": "$\\notin$",
            "⊂": "$\\subset$", "⊃": "$\\supset$", "∪": "$\\cup$", "∩": "$\\cap$",
            "∀": "$\\forall$", "∃": "$\\exists$", "↦": "$\\mapsto$",
            "→": "$\\to$", "←": "$\\leftarrow$",
            "⋅": "$\\cdot$", "·": "$\\cdot$",
            # Greek letters that Marker sometimes outputs as Unicode
            "𝛩": "$\\Theta$", "𝛥": "$\\Delta$", "𝛺": "$\\Omega$",
            "𝛼": "$\\alpha$", "𝛽": "$\\beta$", "𝛾": "$\\gamma$",
            "𝛿": "$\\delta$", "𝜀": "$\\epsilon$", "𝜃": "$\\theta$",
            "𝜅": "$\\kappa$", "𝜆": "$\\lambda$", "𝜇": "$\\mu$",
            "𝜎": "$\\sigma$", "𝜔": "$\\omega$", "𝜙": "$\\phi$",
            "𝜋": "$\\pi$", "𝜏": "$\\tau$",
            "κ": "$\\kappa$", "δ": "$\\delta$",
        }
        for uchar, replacement in _MATH_UNICODE.items():
            if uchar in out:
                out = out.replace(uchar, _stash(replacement))

        # Step 2: Convert Markdown formatting to LaTeX and stash results.

        # Inline images first (before links, since ![...] starts with !)
        out = re.sub(
            r"!\[([^\]]*)\]\(([^)]+)\)",
            lambda m: _stash(f"\\includegraphics[height=1em]{{{self._resolve_image(Path(m.group(2)).name)}}}"),
            out,
        )

        # F1-04 / F2-04: kaçışlı yıldız ve alt çizgi düz karakterdir; biçim
        # kalıpları onları italik/kalın işareti sanmasın ("0.5\*0.5 … 0.2\*0.8"
        # eskiden "\textit{…\}" oluyordu). Görsel yolundan SONRA: dosya adındaki
        # "\_" _resolve_image'a eskisi gibi gider.
        out = _MD_ESCAPED_STAR_RE.sub(lambda m: _stash("*"), out)
        out = _MD_ESCAPED_UNDERSCORE_RE.sub(lambda m: _stash("\\_"), out)

        # 2026-10-05 (G1a-2): düzenli ifade geçişi yıldız artığı bırakırsa
        # (yanaşık '*' dizisi kaldı ya da yakalanan içerikte '*' var: iç içe
        # ya da yanlış eşlenmiş vurgu) yıldızlı vurgu CommonMark kuralıyla
        # baştan çözülür (bkz. _cm_emphasis_tree). Öteki satırlar aynen.
        pre_emphasis = out
        star_leftover = [False]

        def _star(cmd_open: str, cmd_close: str):
            def _repl(m: "re.Match[str]") -> str:
                if "*" in m.group(1):
                    star_leftover[0] = True
                return _stash(cmd_open + _escape_tex_keep_tokens(m.group(1)) + cmd_close)
            return _repl

        def _under(cmd_open: str, cmd_close: str):
            # 2026-10-05 (G1a-2): yalnız alt çizgiden oluşan içerik ("____"
            # boşluk doldurma çizgisi) vurgu değildir (CommonMark); eskiden
            # "\textit{\_\_}" olup iki alt çizgi kayboluyordu.
            def _repl(m: "re.Match[str]") -> str:
                if not m.group(1).strip("_"):
                    return m.group(0)
                return _stash(cmd_open + _escape_tex_keep_tokens(m.group(1)) + cmd_close)
            return _repl

        def _underscore_emphasis(s: str) -> str:
            s = re.sub(r"___(.+?)___", _under("\\textbf{\\textit{", "}}"), s)
            s = re.sub(r"__(.+?)__", _under("\\textbf{", "}"), s)
            return re.sub(r"(?<!\w)_(.+?)_(?!\w)", _under("\\textit{", "}"), s)

        # Bold+Italic: ***text*** or ___text___ (MUST come before bold/italic)
        out = re.sub(r"\*\*\*(.+?)\*\*\*", _star("\\textbf{\\textit{", "}}"), out)
        out = re.sub(r"___(.+?)___", _under("\\textbf{\\textit{", "}}"), out)

        # Bold: **text** or __text__
        out = re.sub(r"\*\*(.+?)\*\*", _star("\\textbf{", "}"), out)
        out = re.sub(r"__(.+?)__", _under("\\textbf{", "}"), out)

        # Italic: *text* or _text_
        out = re.sub(r"\*(.+?)\*", _star("\\textit{", "}"), out)
        out = re.sub(r"(?<!\w)_(.+?)_(?!\w)", _under("\\textit{", "}"), out)

        if star_leftover[0] or ("*" in out and _cm_has_open_delims(out)):
            out = _underscore_emphasis(_cm_render_tex(_cm_emphasis_tree(pre_emphasis), _stash))

        # Links: [text](url)
        # 2026-10-05: hedefi adres olmayan "[…](…)" bağlantı değildir (metindeki
        # regex "[\_-](?P<ad>[A-Z0-9]{8,})" \href{?P<…>}{\_-} oluyor, kalıbın
        # yarısı PDF'ten düşüyordu). Metin olarak kalır; "]"
        # "{]}" yazılır ki 2_derle sanitizer'ının bağ kalıntısı temizliği de
        # "(…)" kısmını silmesin (bkz. _literal_escape_tex).
        def _link(m: "re.Match[str]") -> str:
            if _BAG_DISI_HEDEF_RE.search(m.group(2)):
                return "[" + m.group(1) + _stash("{]}") + "(" + m.group(2) + ")"
            return _stash(f"\\href{{{m.group(2)}}}{{{_escape_tex_keep_tokens(m.group(1))}}}")

        out = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _link, out)

        # Strikethrough: ~~text~~ (#52: icerik escape edilir — ham % satiri
        # yutuyor, & derlemeyi kiriyordu; \sout icin ulem paketi template'e
        # eklendi)
        out = re.sub(
            r"~~(.+?)~~",
            lambda m: _stash(f"\\sout{{{_escape_tex_keep_tokens(m.group(1))}}}"),
            out,
        )

        # Step 2.5: Unescape markdown-escaped TeX specials to prevent double-escaping
        # Markdown \_  →  _  →  (Step 3 will re-escape to \_)
        # Without this: \_  →  \ + \_  =  \\_  (\\=linebreak + _=subscript = ERROR)
        # Gercek math Step 1a/1b'de stash'lendi, buradaki '$' duz metindir.
        out = _unescape_markdown_specials(out)

        # Step 3: Escape TeX specials in remaining plain text segments
        parts = re.split(r"(\x00PROT_\d+\x00)", out)
        for k, part in enumerate(parts):
            if not part.startswith("\x00"):
                escaped = []
                for ch in part:
                    escaped.append(_TEX_SPECIAL.get(ch, ch))
                parts[k] = "".join(escaped)
        out = "".join(parts)

        # Step 4: Restore all protected spans (reverse order for nested stashes)
        # Outer stashes (higher index) may contain inner placeholders (lower index).
        # Restoring outer first exposes inner placeholders for subsequent passes.
        for idx in range(len(protected) - 1, -1, -1):
            out = out.replace(f"\x00PROT_{idx}\x00", protected[idx])

        return out
