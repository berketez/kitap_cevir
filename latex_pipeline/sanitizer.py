"""
Pre-translation LaTeX sanitizer.

Cleans the LaTeX body BEFORE sending it to the translation engine.
Targets known issues found during Book 1 (Deep Learning & Computational
Physics) translation: publisher artifacts, PDF header remnants, broken
hrefs, MT prompt leakage, and normalizes EN cross-reference labels to TR.

Also handles POST-translation cleanup:
- HTML tag artifacts from HY-MT 7B repetition loops
- Mis-translated LaTeX commands (e.g. \\frontmatter -> \\on tarafmatter)
- Dangerous LaTeX commands (\\write18, \\input, etc.) for security

All operations are IDEMPOTENT -- running sanitize() twice on the same
input produces the same output.

INVARIANT: Content inside math environments, \\label{}, \\ref{}, \\cite{}
is NEVER modified.

Usage:
    from latex_pipeline.sanitizer import LatexSanitizer

    sanitizer = LatexSanitizer(source_lang="en", target_lang="tr")
    cleaned_body, report = sanitizer.sanitize(latex_body)
    print(report)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

# Modül sabiti TAM listedir (Yol B sözleşmesi — testler/dış çağıranlar
# bunu doğrular); manuel modda MT-only alt küme çalışma anında ÇIKARILIR.
from latex_pipeline.patterns import ALL_PROMPT_LEAK_PATTERNS as PROMPT_LEAK_PATTERNS
from latex_pipeline.patterns import CHAPTER_LABEL_WORDS
from latex_pipeline.patterns import XREF_LABEL_WORDS
from latex_pipeline.patterns import FLOAT_CAPTION_MAX_CHARS
from latex_pipeline.patterns import FRONT_MATTER_HEADING_NAMES
from latex_pipeline.patterns import fold_accents
from latex_pipeline.kutu_onarim import ORTAM as _KUTU_ORTAMI

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

@dataclass
class SanitizeReport:
    """Counts of each fix type applied during sanitization."""
    artifacts_removed: int = 0
    captions_normalized: int = 0
    headers_cleaned: int = 0
    prompt_leaks_fixed: int = 0
    hrefs_fixed: int = 0
    cjk_cleaned: int = 0
    html_tags_cleaned: int = 0
    commands_fixed: int = 0
    dangerous_filtered: int = 0
    invisible_chars_cleaned: int = 0
    epub_artifacts_cleaned: int = 0
    markdown_links_cleaned: int = 0
    float_placeholders_cleaned: int = 0
    translated_env_names_fixed: int = 0
    foreign_words_cleaned: int = 0
    stuck_words_fixed: int = 0
    figure_heights_capped: int = 0
    total_fixes: int = 0
    details: List[str] = field(default_factory=list)

    def _recount(self) -> None:
        self.total_fixes = (
            self.artifacts_removed
            + self.captions_normalized
            + self.headers_cleaned
            + self.prompt_leaks_fixed
            + self.hrefs_fixed
            + self.cjk_cleaned
            + self.html_tags_cleaned
            + self.commands_fixed
            + self.dangerous_filtered
            + self.invisible_chars_cleaned
            + self.epub_artifacts_cleaned
            + self.markdown_links_cleaned
            + self.float_placeholders_cleaned
            + self.translated_env_names_fixed
            + self.foreign_words_cleaned
            + self.stuck_words_fixed
            + self.figure_heights_capped
        )

    def __str__(self) -> str:
        self._recount()
        lines = [
            f"SanitizeReport: {self.total_fixes} total fixes",
            f"  artifacts_removed  : {self.artifacts_removed}",
            f"  captions_normalized: {self.captions_normalized}",
            f"  headers_cleaned    : {self.headers_cleaned}",
            f"  prompt_leaks_fixed : {self.prompt_leaks_fixed}",
            f"  hrefs_fixed        : {self.hrefs_fixed}",
            f"  cjk_cleaned        : {self.cjk_cleaned}",
            f"  html_tags_cleaned  : {self.html_tags_cleaned}",
            f"  commands_fixed     : {self.commands_fixed}",
            f"  dangerous_filtered : {self.dangerous_filtered}",
            f"  invisible_chars    : {self.invisible_chars_cleaned}",
            f"  epub_artifacts     : {self.epub_artifacts_cleaned}",
            f"  markdown_links     : {self.markdown_links_cleaned}",
            f"  float_placeholders : {self.float_placeholders_cleaned}",
            f"  translated_envs    : {self.translated_env_names_fixed}",
            f"  foreign_words      : {self.foreign_words_cleaned}",
            f"  stuck_words        : {self.stuck_words_fixed}",
            f"  figure_heights     : {self.figure_heights_capped}",
        ]
        if self.details:
            lines.append("  details:")
            for d in self.details:
                lines.append(f"    - {d}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Protected-zone helpers
# ---------------------------------------------------------------------------

# Math environments we must never touch.
_MATH_ENV_NAMES = (
    r"equation\*?|align\*?|gather\*?|multline\*?|split"
    r"|flalign\*?|alignat\*?|cases|pmatrix|bmatrix|vmatrix"
)
# 2026-07-11 (#6): \end tarafi backreference'a cevrildi (autofix.py'deki
# muadiliyle ayni). Eski hali begin/end adlarini BAGIMSIZ esliyordu:
# \begin{align}...\begin{cases}...\end{cases} ilk \end'de kesiyor,
# ortamin kuyrugu (...\end{align}) korumasiz kalip bozuluyordu.
_MATH_ENV_RE = re.compile(
    r"\\begin\{(" + _MATH_ENV_NAMES + r")\}"
    r".*?"
    r"\\end\{\1\}",
    re.DOTALL,
)
_DISPLAY_MATH_RE = re.compile(r"\\\[.*?\\\]", re.DOTALL)
# Inline math satir sonu icerebilir (Marker cikti kirar): (?s) benzeri
# genisletme, ANCAK paragraf siniri (bos satir) asilmaz — yoksa kacak tek
# '$' tum paragraflari yutar. Eski satir-ici hali cok satirli '$...$'i
# stash'lemiyordu; korumasiz math icinde _fix_text_mode_greek ic ice '$'
# uretiyordu (audit 2026-07-16 #32).
_INLINE_MATH_RE = re.compile(
    r"(?<!\\)\$(?!\$)((?:(?!\n\s*\n)(?!(?<!\\)\$).)+)(?<!\\)\$(?!\$)",
    re.DOTALL,
)

# Code / verbatim environments -- must be stashed before sanitization.
_CODE_ENV_RE = re.compile(
    r"\\begin\{(lstlisting|verbatim|minted)\}.*?\\end\{\1\}",
    re.DOTALL,
)

# Denetim 2026-09-24 (M1#5): stash'in görmediği matematik/kod bağlamları.
# Metin modu sarmalamaları (Yunan harfi, G3 sembolleri) bunların içine '$'
# yazıyordu: '$$a ≤ b$$' -> '$$a $\leq$ b$$' ("Display math should end
# with $$"), '\verb|a ≤ b|' PDF'ye '$\leq$' diye basılıyordu.
# \verb<c>...<c>: tek satır; ayraç harf, boşluk ya da '*' olamaz.
_VERB_RE = re.compile(r"\\verb\*?([^\sA-Za-z*])[^\n]*?\1")
# \(...\): en içteki çift; araya başka \( / \) ya da paragraf sınırı
# girmez. Genel stash'e DEĞİL yalnız sarmalama stash'ine girer: Marker'ın
# eşleşmeyen '\(2006)' ... '1989\)' artefaktları paragraf içinde birbirine
# bağlanıp 1600 karakterlik düzyazı aralığı oluşturabiliyor (bir İngilizce bilim kitabı); o
# aralıklar yüzde kaçışı gibi diğer geçişleri eskisi gibi alır.
_PAREN_MATH_RE = re.compile(
    r"(?<!\\)\\\((?:(?!\\[()]|\n[ \t]*\n).)*?\\\)",
    re.DOTALL,
)
# \ensuremath{...}: argüman her zaman matematiktir (3 düzey iç içe parantez).
_ENSUREMATH_RE = re.compile(
    r"\\ensuremath\s*\{(?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*\}"
)
# '$' / '$$' belirteçleri (kaçışlı karakter ve paragraf sınırı dahil):
# _dollar_display_spans TeX'in okuma sırasını izler.
_DOLLAR_TOKEN_RE = re.compile(r"\\.|\$\$|\$|\n[ \t]*\n", re.DOTALL)
# Yalnız metin modu sarmalamalarından korunan bölgeler (_fix_text_mode_wraps):
# \url{...}, \href'in YALNIZ URL argümanı (bağlantı metni metindir),
# \texorpdfstring{..}{..} (ikinci argüman PDF yer imi metnidir, '$' almaz)
# ve \(...\) (_PAREN_MATH_RE).
# Genel stash'e alınmazlar: yüzde kaçışı (\href URL'sinde \% hyperref'te
# doğru çözülür) ve URL onarımları eskisi gibi çalışır. '#page-N-N' iç
# bağlantıları _fix_broken_hrefs'indir.
_WRAP_PROTECTED_RE = re.compile(
    r"\\url\s*\{[^{}\n]*\}"
    r"|\\href\s*(?:\[[^\]\n]*\])?\s*\{(?!\s*#)[^{}\n]*\}"
    r"|\\texorpdfstring\s*\{(?:[^{}]|\{[^{}]*\})*\}\s*\{(?:[^{}]|\{[^{}]*\})*\}"
)
# Argümanı PDF yer imine de yazılan başlık komutları. Yer imi dönüşümü
# (hyperref + unicode-math) '$\alpha$', '$\rightarrow$' gibi sembol
# komutlarında "Improper alphabetic constant" x100 ile derlemeyi PDF'siz
# durduruyor; başlıkta sarmalama \texorpdfstring ile yapılır.
_HEADING_CMD_OPEN_RE = re.compile(
    r"\\(?:part|chapter|section|subsection|subsubsection|paragraph"
    r"|subparagraph)\*?\s*"
)

# P2: section başlığı başındaki numara öneki ("\section{1.1 Başlık}" ->
# "\section{Başlık}"; LaTeX kendisi numaralar). TEK KAYNAK: sanitizer bu
# öneki çeviriden siler, QC section_number_consistency karşılaştırmadan önce
# kaynağa da aynı kuralı uygular (2026-09-24; bir test kitabında 23 sahte
# CRITICAL).
# \chapter bilerek kapsam dışı — sanitizer chapter numarasına dokunmaz.
# Yıldızlı başlık da kapsam dışı: LaTeX onu numaralamaz, silinen önek
# başlığın TEK numarasıdır ("\subsubsection*{§ 1.1. Terminoloji}" numarasız
# kalıyordu — denetim 2026-09-24 M1#8); orada numara metnin parçasıdır.
SECTION_NUM_PREFIX_RE = re.compile(
    # "§ 1.1. Başlık" (Rusça/Almanca ders kitapları, 2026-09-24) de önektir.
    r"(\\(?:sub)*section\{)\s*(?:§\s*)?\d+(?:\.\d+)*\.?\s+"
)

# ---------------------------------------------------------------------------
# P1: Marker bölüm ETİKETİ artefaktları (2026-06-14, 2026-09-24). TEK KAYNAK:
# sanitizer bu kuralları çeviriye uygular (_fix_marker_chapter_artifacts),
# QC (information_loss, section_number_consistency, number_hallucination)
# karşılaştırmadan önce iki tarafa da uygular (apply_chapter_label_rules).
# Eskiden QC kaynağı dönüştürmüyordu: eski body.tex/EXPORT.tex'teki
# "\section{Chapter 3}" çeviride silinince kusursuz Yol A çevirisi sahte
# "bölüm kaybı" CRITICAL'ı alıp 2_derle'den çıkış 3 ile dönüyordu
# (denetim 2026-09-24 M1#1).
# ---------------------------------------------------------------------------

# Sahte chapter satırı: tam olarak "Chapter N" / "Bölüm N" / "Kısım N"
# başlıklı \chapter. Bölüm etiketi kelimeleri patterns.py'de tek kaynak;
# "Kısım" yalnız bu kuralda (2026-06-14 davranışı).
_SPURIOUS_CHAPTER_RE = re.compile(
    r"^[ \t]*\\chapter\*?\{\s*(?:" + CHAPTER_LABEL_WORDS + r"|K[ıi]s[ıi]m)"
    r"\s+\d+\s*\}[ \t]*\r?\n",
    re.MULTILINE,
)
# Bölüm etiketi başlığının "{Chapter 2}" kısmı + satır sonu. "Kısım/Part"
# BİLEREK yok: kitap içi kısım adıdır (CEVIRI_TALIMATI "Kısım" kullanır),
# birleştirilmez ve silinmez (denetim M1#2: "Kısım I" + başlık \chapter
# yapılıyor, bölüm numaraları kayıyordu).
_CHAPTER_LABEL_ARG = (
    r"\{\s*" + CHAPTER_LABEL_WORDS + r"\s+(?:\d+|[IVXLC]+)\s*\}[ \t]*\r?\n"
)
# Etiket biçimli başlık argümanı (bölüm ya da kısım etiketi + numara):
# birleştirmede "başlık" olarak alınmaz — art arda iki etiket (cevap
# ekinde boş "Bölüm 1" + "Bölüm 2") \chapter{Bölüm 2} olup sonra sahte
# chapter diye siliniyor, iki başlık birden kayboluyordu.
_LABEL_LIKE_ARG = (
    r"\s*(?:" + CHAPTER_LABEL_WORDS + r"|K[ıi]s[ıi]m|(?i:Part))"
    r"\s+(?:\d+|[IVXLC]+)\s*\}"
)
# (0a) "\section{Chapter 2}" + ardından AYNI (section) düzeyde numarasız,
# etiket biçimli olmayan başlık -> tek \chapter{Başlık} (Marker'ın aynı-düzey
# örüntüsü; converter bunu kökte düzeltir, kural eski body.tex/EXPORT.tex
# dosyaları için savunma hattıdır). Yalnız \section: daha derin düzeydeki
# etiket doğrudan \chapter'a çıkarılmaz (denetim M1#2c).
# Gruplar: 1=etiketin yıldızı, 2=başlığın yıldızı, 3=başlık.
_LABEL_SECTION_THEN_TITLE_RE = re.compile(
    r"^[ \t]*\\section(\*?)" + _CHAPTER_LABEL_ARG
    + r"(?:[ \t]*\r?\n)*"
    r"[ \t]*\\section(\*?)\{(?!\s*\d+(?:\.\d+)*\s)(?!" + _LABEL_LIKE_ARG + r")"
    r"((?:[^{}\n]|\{(?:[^{}\n]|\{[^{}\n]*\})*\})*)\}",
    re.MULTILINE,
)
# (0b) Etiket (sub)section + ardından \chapter{Başlık} (farklı-düzey
# örüntü): etiket satırı (ardındaki boş satırlarla) silinir, chapter kalır.
# Aradaki başka etiket satırları da aynı gruptadır (tek geçişte silinir ->
# kural idempotent; boş satır birikip ikinci geçişte daralmaz).
_LABEL_SECTION_BEFORE_CHAPTER_RE = re.compile(
    r"^[ \t]*\\(?:sub)*section\*?" + _CHAPTER_LABEL_ARG + r"(?:[ \t]*\r?\n)*"
    r"(?=(?:[ \t]*\\(?:sub)*section\*?" + _CHAPTER_LABEL_ARG + r"(?:[ \t]*\r?\n)*)*"
    r"[ \t]*\\chapter\*?\{)",
    re.MULTILINE,
)


def _apply_chapter_label_rules(text: str, merged_mark: str = "") -> Tuple[str, int]:
    """Bölüm etiketi kuralları (0a), (0b), (1); ``(metin, değişiklik sayısı)``.

    *merged_mark* verilirse (0a)'nın ürettiği her \\chapter satırının
    kapanış parantezinden sonra bu işaret bırakılır: sanitizer'ın başlık-
    chapter sezgisi (adım 2) birleştirmeyle yaratılan chapter'ları bu
    işaretle tanıyıp saymaz, sonra işareti siler.
    """
    has_chapter = bool(re.search(r"\\chapter\*?\{", text))

    def _merge(m: "re.Match[str]") -> str:
        title_star, title = m.group(2), m.group(3)
        if has_chapter:
            return "\\chapter" + title_star + "{" + title + "}" + merged_mark
        # Bölüm kavramı olmayan belgede (article) \chapter derlenmez:
        # etiket düşer, başlık kendi seviyesinde kalır.
        return "\\section" + title_star + "{" + title + "}"

    text, n_merge = _LABEL_SECTION_THEN_TITLE_RE.subn(_merge, text)
    text, n_drop = _LABEL_SECTION_BEFORE_CHAPTER_RE.subn("", text)
    text, n_spurious = _SPURIOUS_CHAPTER_RE.subn("", text)
    return text, n_merge + n_drop + n_spurious


def apply_chapter_label_rules(text: str) -> str:
    """Sanitizer'ın bölüm etiketi kurallarını (sahte "Chapter N" satırları,
    etiket-section birleştirme/silme) metne uygular; idempotent.

    QC yapı sayımları (information_loss, section_number_consistency,
    number_hallucination) bunu HER İKİ tarafa uygular: sanitizer yalnız
    çeviriyi dönüştürdüğü için eski kaynaktaki etiket başlıkları çeviride
    "kayıp" sayılıyordu.
    """
    if not text:
        return text or ""
    return _apply_chapter_label_rules(text)[0]


# ---------------------------------------------------------------------------
# F1-06 / F2-09 (2026-09-29): başlık-chapter sezgisinin KANIT şartları.
# ---------------------------------------------------------------------------
# Eski sezgi yalnız yapıya bakıyordu (ilk chapter kısa ve \section'sız):
# kısa gerçek 1. bölüm (roman, EPUB, "Kapitel 1 Grundlagen") \chapter* olup
# numarasını ve İçindekiler girdisini yitiriyor, sonraki bölümler bir numara
# kayıyordu. "Kısa ilk chapter" hem künye sayfası hem kısa gerçek bölüm
# olabilir; artık yıldızlamak için ayrıca şu kanıtlardan biri gerekir:
#   (a) ilk chapter'dan sonraki ilk başlık "Chapter 1" / "Bölüm I" gibi 1
#       numaralı bölüm etiketidir (kitabın 1. bölümü ikinci chapter'dır;
#       Marker künye örüntüsü, ders notu derlemi);
#   (b) sonraki chapter bir ön madde başlığıdır (Önsöz, Preface, Vorwort,
#       İçindekiler ...): ondan önce gelen bölüm de ön maddedir (ithaf);
#   (c) ilk chapter'ın düz metni yalnız bir iki satırlık ad/yazar bilgisidir
#       (_TITLE_PAGE_MAX_TEXT_CHARS): gerçek bölüm en az bir paragraftır.
# EPUB kaynağında kural hiç uygulanmaz: EPUB'un bölüm yapısını yazar kurar,
# Marker künye artefaktı yoktur (dönüştürücü ön maddeyi zaten ayırır).
# İlk chapter'ın kendisi etiketliyse ("Kapitel 1" satırı önünde) gerçektir.
_TITLE_PAGE_MAX_TEXT_CHARS = 160
# Ön madde başlıkları: patterns.FRONT_MATTER_HEADING_NAMES (kaynak diller,
# aksansız küçük harf) eksi GİRİŞ adları (Introduction gerçek 1. bölüm
# olabilir) artı Türkçe karşılıklar (çevrilmiş gövde).
_INTRO_HEADING_NAMES = frozenset({
    "introduction", "einleitung", "введение", "introduccion", "introduzione",
})
_TITLE_PAGE_FOLLOWER_NAMES = (FRONT_MATTER_HEADING_NAMES - _INTRO_HEADING_NAMES) | frozenset({
    "onsoz", "on soz", "sunus", "tesekkur", "tesekkurler", "ithaf",
    "icindekiler", "yazar hakkinda", "yazarlar hakkinda", "prolog",
})
# Bölüm etiketi başlığı (her düzey): "\chapter{Chapter 1}", "\section{Bölüm I}".
_LABEL_HEADING_RE = re.compile(
    r"\\(?:chapter|(?:sub)*section)\*?\{\s*" + CHAPTER_LABEL_WORDS
    + r"\s+(\d+|[IVXLC]+)\s*\}")
# Herhangi bir bölümleme başlığı (başlık argümanı tek düzey iç içe grup).
_ANY_HEADING_RE = re.compile(
    r"\\(?:chapter|(?:sub)*section)\*?\{((?:[^{}]|\{[^{}]*\})*)\}")
_LATEX_CMD_RE = re.compile(r"\\[A-Za-z@]+\*?(?:\[[^\]]*\])?|[{}]")
_EPUB_SOURCE = "epub"


def _heading_key(title: str) -> str:
    """Ön madde karşılaştırması: aksansız, küçük harf, tek boşluk."""
    folded = fold_accents(_LATEX_CMD_RE.sub(" ", title)).casefold()
    return re.sub(r"\s+", " ", folded).strip(" .:;")


def _plain_text_len(segment: str) -> int:
    """Segmentin düz metin uzunluğu (komutlar, parantezler, yorumlar ve
    stash yer tutucuları hariç, boşluk sıkıştırılmış)."""
    text = re.sub(r"(?<!\\)%[^\n]*", " ", segment)
    text = re.sub(r"__SANSTASH_\d+__", " ", text)
    text = _LATEX_CMD_RE.sub(" ", text)
    return len(re.sub(r"\s+", " ", text).strip())


def _title_chapter_evidence(body: str) -> Tuple[Optional[str], str]:
    """İlk (etiket olmayan) \\chapter künye/ön madde bölümü mü?

    ``(başlık_argümanı, kanıt)`` döner; kanıt "label-1",
    "front-matter-next", "title-page-text" ya da kanıt yoksa ``""``.
    Etiket kuralları (``_apply_chapter_label_rules``) etiket satırlarını
    sildiği için UYGULANMADAN önceki gövdeye bakılır."""
    first = None
    for m in _ANY_HEADING_RE.finditer(body):
        if not m.group(0).startswith("\\chapter"):
            continue
        if _LABEL_HEADING_RE.fullmatch(m.group(0)):
            continue
        first = m
        break
    if first is None:
        return None, ""
    title = first.group(1)
    before = body[:first.start()].rstrip()
    prev = None
    for prev in _ANY_HEADING_RE.finditer(before):
        pass
    if (prev is not None and prev.end() == len(before)
            and _LABEL_HEADING_RE.fullmatch(prev.group(0))):
        return title, ""  # ilk chapter'ın kendisi etiketli: gerçek bölüm
    nxt = _ANY_HEADING_RE.search(body, first.end())
    lab = _LABEL_HEADING_RE.fullmatch(nxt.group(0)) if nxt else None
    if lab and lab.group(1) in ("1", "I"):
        return title, "label-1"
    for ch in _ANY_HEADING_RE.finditer(body, first.end()):
        if not ch.group(0).startswith("\\chapter"):
            continue
        if _LABEL_HEADING_RE.fullmatch(ch.group(0)):
            continue
        if _heading_key(ch.group(1)) in _TITLE_PAGE_FOLLOWER_NAMES:
            return title, "front-matter-next"
        break
    seg_end = nxt.start() if nxt else len(body)
    if _plain_text_len(body[first.end():seg_end]) <= _TITLE_PAGE_MAX_TEXT_CHARS:
        return title, "title-page-text"
    return title, ""


def _meta_fields(text: str) -> Dict[str, str]:
    """Gövdedeki ``% KITAPCEVIR-META`` başlığının alanları (yoksa boş).

    Biçimin tek kaynağı ``yol_a_ortak`` (META_BEGIN/META_END/_META_FIELDS);
    burada dosya değil METİN okunur (2_derle gövdesi başlığı taşır; Phase 6
    tam belgesinde başlık \\begin{document} ardındadır)."""
    try:
        from yol_a_ortak import META_BEGIN, META_END, _META_FIELDS
    except Exception:  # pragma: no cover - paket dışı kullanım
        return {}
    start = text.find(META_BEGIN)
    if start < 0:
        return {}
    end = text.find(META_END, start + len(META_BEGIN))
    if end < 0:
        return {}
    meta: Dict[str, str] = {}
    for line in text[start + len(META_BEGIN):end].splitlines():
        line = line.strip()
        if not line.startswith("%"):
            continue
        key, sep, val = line[1:].strip().partition(":")
        if sep and key.strip() in _META_FIELDS:
            meta[key.strip()] = val.strip()
    return meta


# Commands whose braced arguments must not be touched.
_PROTECTED_CMD_RE = re.compile(
    r"\\(?:label|ref|eqref|cite|pageref|cref|autoref)\s*\{[^}]*\}"
)

# B21: TOC-style tabular column spec — used by _fix_toc_tabular_widths
_TOC_TABULAR_RE = re.compile(
    r"\\begin\{tabular\}\s*\{([^}]+)\}",
    re.IGNORECASE,
)
_TABULAR_END_RE = re.compile(r"\\end\{tabular\}")
# B21 (2026-09-24): "içindekiler tablosu mu?" kanıtı SPEC'ten değil tablo
# GÖVDESİNDEN okunur. Spec-only sezgi ("3-6 sütun, yalnız l/r/c") veri
# tablolarını da TOC sanıp sondan ikinci sütuna 70 mm yazıyordu (bir veri
# tablosunda "e" sütunu 70 mm oldu, "şekil" sütunu sayfanın sağına savruldu).
# TOC imzası: son sütun sayfa numarasıdır (1-4 haneli tam sayı ya da küçük
# Roma rakamı; Marker'ın birleştirdiği "343 346" dahil), numaralar aşağı
# doğru ARTAR (OCR hatası payı: azalan çift oranı sınırlı) ve aynı satırda
# başlık metni vardır (sayısal veri ızgarası elenir).
# Roma dalı yalnız i/v/x (ön söz sayfaları küçüktür): m, mm, cm, l, ml, d
# de geçerli Roma sayısıdır ve birim sütunlu veri tablosu (Uzunluk & 2.5 &
# m) İçindekiler sanılıyordu (denetim 2026-09-24 M3#1). Ayrıca en az bir
# sayfa no rakamla başlamalı ve Roma değerleri de artmalı.
_TOC_PAGE_CELL_RE = re.compile(r"^(?:\d{1,4}(?:\s+\d{1,4})*|[ivx]{1,7})$")
_ROMAN_PAGE_VALUES = {"i": 1, "v": 5, "x": 10}
_TABLE_RULE_CMD_RE = re.compile(
    r"\\(?:toprule|midrule|bottomrule|hline)\b|\\cline\{[^}]*\}"
)
_TOC_TEXT_RUN_RE = re.compile(r"[^\W\d_]{3,}")
_TOC_MIN_PAGE_RATIO = 0.8        # dolu son hücrelerin en az bu kadarı sayfa no
_TOC_MAX_DECREASE_RATIO = 0.2    # ardışık sayfa no çiftlerinde azalan payı
_TOC_MIN_TEXT_RATIO = 0.5        # sayfa no'lu satırların bu kadarında metin

# V1 denetimi (2026-09-29): sayfadan taşan tablo (_fit_wide_tables).
# Gövde puntosunda metin genişliğine sığan yaklaşık karakter sayısı; bir
# tablonun satır başına doğal genişliği bunu aşarsa uzun sütunlar kırılır.
# config/defaults.py'ye taşınmalı (şimdilik yalnız burada kullanılıyor).
_TABLE_FIT_MAX_CHARS = 75
# En uzun hücresi bu kadar karakterden kısa sütun kırılmaz (sayı, birim).
_TABLE_WRAP_MIN_CHARS = 20
# Sütun başına ara boşluk (2\tabcolsep ≈ 12 pt) karakter karşılığı.
_TABLE_COLSEP_CHARS = 3
# Kırılan sütunun en dar genişliği (\linewidth oranı).
_TABLE_WRAP_MIN_FRAC = 0.15
_CELL_CMD_RE = re.compile(r"\\[A-Za-z@]+\*?(?:\[[^\]]*\])?")
_CELL_MATH_RE = re.compile(r"\$[^$]*\$")


def _cell_display_len(cell: str) -> int:
    """Tablo hücresinin basılı uzunluğu (karakter, kaba kestirim): komut
    adları ve ayraçlar sayılmaz, satır içi matematik yarım sayılır."""
    text = _CELL_MATH_RE.sub(
        lambda m: "x" * max(1, (len(m.group(0)) - 2) // 2), cell)
    text = _CELL_CMD_RE.sub("", text)
    text = re.sub(r"[{}\\]", "", text)
    return len(" ".join(text.split()))


def _looks_like_toc_table(table_body: str) -> bool:
    """Tablo gövdesi (``\\begin{tabular}{..}`` ile ``\\end{tabular}``
    arası) bir içindekiler sayfası gibi mi görünüyor? Bkz. B21 notu."""
    last_cells: List[str] = []
    has_text: List[bool] = []
    for row in re.split(r"\\\\", table_body):
        row = _TABLE_RULE_CMD_RE.sub("", row)
        if "&" not in row:
            continue
        cells = [c.strip() for c in re.split(r"(?<!\\)&", row)]
        last_cells.append(cells[-1])
        has_text.append(any(_TOC_TEXT_RUN_RE.search(c) for c in cells[:-1]))
    filled = [i for i, c in enumerate(last_cells) if c]
    if not filled:
        return False
    paged = [i for i in filled if _TOC_PAGE_CELL_RE.match(last_cells[i])]
    if len(paged) < _TOC_MIN_PAGE_RATIO * len(filled):
        return False
    arabic = [int(last_cells[i].split()[0]) for i in paged
              if last_cells[i][0].isdigit()]
    if not arabic:
        # Yalnız i/v/x harfli son sütun birim/etiket sütunu olabilir.
        return False
    roman = [_roman_page_value(last_cells[i]) for i in paged
             if not last_cells[i][0].isdigit()]
    # Ön söz (Roma) ve gövde (Arap) sayfaları ayrı dizilerdir; ikisi de artar.
    for numbers in (arabic, roman):
        decreases = sum(1 for a, b in zip(numbers, numbers[1:]) if b < a)
        if decreases > _TOC_MAX_DECREASE_RATIO * max(1, len(numbers) - 1):
            return False
    return sum(has_text[i] for i in paged) >= _TOC_MIN_TEXT_RATIO * len(paged)


def _roman_page_value(cell: str) -> int:
    """Küçük harfli i/v/x Roma sayısının değeri ("xiv" -> 14)."""
    total = prev = 0
    for ch in reversed(cell):
        value = _ROMAN_PAGE_VALUES[ch]
        if value < prev:
            total -= value
        else:
            total += value
            prev = value
    return total


# ---------------------------------------------------------------------------
# G1 (2026-09-25): görsel yüksekliğine üst sınır. TEK SABİT.
# ---------------------------------------------------------------------------
# Kanıt (Rusça taranmış bir bölüm): 0.8\textwidth genişlikteki
# uzun-dar şemalar \textheight'tan 46-423 pt uzun çıkıyordu. [!htb] şekil
# hiçbir sayfaya sığmayınca ("Float too large for page") \clearpage'e kadar
# ertelenir; şekiller FIFO sırayla yerleştiği için ARKASINDAKİ bütün şekiller
# de bölüm sonuna itilir (38 sayfalık kaynak 86 sayfa, son 20 sayfanın her
# birinde tek şekil; altyazılar gövde metni olduğu için yerinde kalıyordu).
#
# Neden 0,7\textheight:
#   * book.cls'nin \topfraction varsayılanı 0,7'dir: bu yükseklikteki bir
#     şekil "!" olmadan da ([t], [tp], [htbp]) sayfa başına yerleşebilir,
#     yani hiçbir yerleşim belirteciyle "yerleştirilemez" hâle gelmez.
#   * Sayfanın en az 0,3'ü (\textfraction = 0,2'den fazla) şeklin altyazısına
#     ve çevresindeki metne kalır; şekil ile altyazısı aynı sayfada kalabilir.
#   * Olağan sayfa oranında (\textwidth ≈ 0,65\textheight) 0.8\textwidth
#     genişlikteki bir görsel ancak yükseklik/genişlik oranı ~1,35'i aşınca
#     sınıra takılır: yalnız gerçekten sığmayan uzun-dar görseller küçülür.
FIGURE_MAX_HEIGHT = r"0.7\textheight"

# Genişlik verilmiş \includegraphics'in seçenek listesi (eski iki köşeli
# parantezli bounding-box sözdizimi ``[..][..]{..}`` eşleşmez: ilk listeden
# sonra '{' gelmesi şart). İç içe köşeli parantezli liste de eşleşmez.
_INCLUDEGRAPHICS_OPTS_RE = re.compile(
    r"(\\includegraphics\*?\s*)\[([^\[\]]*)\](?=\s*\{)"
)
# Bu anahtarlardan biri varsa görsele dokunulmaz: yükseklik zaten
# belirlenmiştir (height/totalheight), ölçek genişlikle birlikte belirsizdir
# (scale) ya da döndürmede yükseklik ekleme sırasına göre anlam değiştirir
# (angle/origin).
_GRAPHICS_SKIP_KEYS = frozenset(
    {"height", "totalheight", "scale", "angle", "origin"}
)


def _split_keyval(options: str) -> List[str]:
    """Anahtar=değer listesini süslü parantez dışındaki virgüllerden böl."""
    parts: List[str] = []
    depth = 0
    current: List[str] = []
    for ch in options:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


def cap_includegraphics_height(text: str) -> Tuple[str, int]:
    r"""Genişlik verilmiş her ``\includegraphics``'e yükseklik üst sınırı ekle.

    ``[width=0.8\textwidth]`` -> ``[width=0.8\textwidth,height=0.7\textheight,
    keepaspectratio]``. graphicx ``keepaspectratio`` ile görseli iki sınırın
    İÇİNE sığdırır: görsel verilen genişlikte zaten sınırın altındaysa
    boyutu değişmez (graphicx'in ölçek aritmetiğinden gelen <0,1 pt
    yuvarlama dışında; sayfa düzenini etkilemez); uzunsa en-boy oranı
    korunarak küçülür. Hiçbir görsel büyümez, genişlik davranışı değişmez.

    Dokunulmayanlar: seçeneksiz ``\includegraphics{..}`` (yalnız yükseklik
    vermek küçük görseli BÜYÜTÜRDÜ), ``_GRAPHICS_SKIP_KEYS`` anahtarlarından
    birini taşıyanlar, ``keepaspectratio=false``, geçersiz genişlik
    (boş ya da ``!``). Yükseklik taşıyan çıktıya ikinci kez uygulanınca
    değişmez (idempotent). Döner: ``(metin, eklenen sınır sayısı)``.
    """
    count = 0

    def _repl(m: "re.Match[str]") -> str:
        nonlocal count
        options = m.group(2)
        values = {}
        for item in _split_keyval(options):
            if not item.strip():
                continue
            key, _, value = item.partition("=")
            values[key.strip()] = value.strip()
        if "width" not in values or values["width"] in ("", "!"):
            return m.group(0)
        if _GRAPHICS_SKIP_KEYS.intersection(values):
            return m.group(0)
        additions = ["height=" + FIGURE_MAX_HEIGHT]
        if "keepaspectratio" in values:
            if values["keepaspectratio"].lower() not in ("", "true"):
                return m.group(0)
        else:
            additions.append("keepaspectratio")
        base = options.rstrip()
        sep = "" if base.endswith(",") else ","
        count += 1
        return f"{m.group(1)}[{base}{sep}{','.join(additions)}]"

    return _INCLUDEGRAPHICS_OPTS_RE.sub(_repl, text), count


def _stash_protected(text: str) -> Tuple[str, List[str]]:
    """Replace math environments, display/inline math, and ref commands
    with ``__SANSTASH_N__`` placeholders.  Returns (modified_text, stash).
    """
    stash: List[str] = []

    def _push(zone: str) -> str:
        stash.append(zone)
        return f"__SANSTASH_{len(stash) - 1}__"

    def _repl(m: re.Match) -> str:
        return _push(m.group(0))

    # Order: broader patterns first. Kod (ortam ve \verb) matematikten
    # önce: içindeki '$' matematik sayılmasın.
    text = _MATH_ENV_RE.sub(_repl, text)
    text = _CODE_ENV_RE.sub(_repl, text)
    text = _VERB_RE.sub(_repl, text)
    text = _stash_dollar_display(text, _push)
    text = _DISPLAY_MATH_RE.sub(_repl, text)
    text = _INLINE_MATH_RE.sub(lambda m: _repl(m), text)
    text = _ENSUREMATH_RE.sub(_repl, text)
    text = _PROTECTED_CMD_RE.sub(_repl, text)
    return text, stash


def dollar_math_spans(text: str) -> List[Tuple[int, int, bool]]:
    """``$...$`` ve ``$$...$$`` bölgeleri: ``(başlangıç, bitiş, display_mı)``.

    Aralık ayraçları kapsar (``text[başlangıç:bitiş]`` ``$`` ile başlayıp
    biter). Düz regex yetmez: gerçek gövdelerde bitişik iki inline math
    ('$\\Theta$${}^{*}$') çok yaygındır ve ortadaki '$$' display değildir.
    TeX gibi soldan okunur: metin modunda '$$' display açar, '$' inline
    açar; inline içinde '$' kapatır ('$$' = kapat + yeni inline aç);
    display yalnız '$$' ile kapanır. Paragraf sınırı her durumu sıfırlar
    (kapanmayan bölge kaydedilmez, paragrafları yutmaz).

    Autofix'in satır içi matematik onarımları da bunu kullanır: iki
    matematik bölgesinin ARASINDAKİ düzyazı ('$$...$$ metin $x$') eskiden
    '$...$' sanılıp birleştiriliyordu (kalite 2026-09-25, R6a).
    """
    spans: List[Tuple[int, int, bool]] = []
    state = "text"   # "text" | "inline" | "display"
    start = 0
    for m in _DOLLAR_TOKEN_RE.finditer(text):
        tok = m.group(0)
        if tok[0] == "\\":
            continue
        if tok[0] == "\n":
            state = "text"
        elif state == "text":
            if tok == "$$":
                state, start = "display", m.start()
            else:
                state, start = "inline", m.start()
        elif state == "inline":
            # '$$' inline'ı kapatıp yenisini açar; '$' yalnız kapatır.
            spans.append((start, m.start() + 1, False))
            if tok == "$$":
                start = m.start() + 1
            else:
                state = "text"
        elif tok == "$$":
            spans.append((start, m.end(), True))
            state = "text"
    return spans


def _dollar_display_spans(text: str) -> List[Tuple[int, int]]:
    """``$$...$$`` display matematiğinin ``(başlangıç, bitiş)`` aralıkları
    (okuma kuralı: ``dollar_math_spans``)."""
    return [(s, e) for s, e, display in dollar_math_spans(text) if display]


def _stash_dollar_display(text: str, push) -> str:
    """``$$...$$`` aralıklarını ``push(bölge) -> yer tutucu`` ile değiştirir."""
    spans = _dollar_display_spans(text)
    if not spans:
        return text
    out: List[str] = []
    pos = 0
    for start, end in spans:
        out.append(text[pos:start])
        out.append(push(text[start:end]))
        pos = end
    out.append(text[pos:])
    return "".join(out)


def _restore_stash(text: str, stash: List[str]) -> str:
    """Restore ``__SANSTASH_N__`` placeholders in reverse order."""
    for idx in range(len(stash) - 1, -1, -1):
        text = text.replace(f"__SANSTASH_{idx}__", stash[idx])
    return text


def _is_code_zone(zone: str) -> bool:
    """Stash bölgesi kod mu (verbatim/lstlisting/minted ortamı ya da \\verb)?"""
    return bool(_CODE_ENV_RE.match(zone) or _VERB_RE.match(zone))


# ---------------------------------------------------------------------------
# F2-05 (2026-09-29): matematikte liste virgülü ondalık virgülden ayrılır.
# ---------------------------------------------------------------------------
# Şablonun icomma eşdeğeri (turkish_book.tex, R15) virgülden hemen sonra
# rakam geliyorsa virgülü sayı parçası (\mathord) dizer: Türkçe "998,2"
# doğru çıkar, ama aralık/koordinat/dizi listesi ("[−1,1]", "i=1,2,…,n")
# da ondalık gibi sıkışır. Şablon yalnız bir sonraki simgeyi görür; bağlam
# burada okunur ve LİSTE virgülünün ardına boşluk konur (TeX matematikte
# boşluğu yok sayar; yalnız şablonun virgül makrosu onu "rakam değil" görür
# ve virgülü noktalama olarak dizer). Kurallar:
#   (1) boşluksuz virgülle bağlı üç ya da daha çok sayı ("1,2,3") — binlik
#       ayracı ("1,000,000") hariç; ondalık sayıda tek virgül olur. Boşluklu
#       virgül ("0,1, 0,2") üst düzey ayraçtır, öğeler ondalık sayılabilir,
#   (2) boşluksuz, üç nokta içeren liste ("1,2,\ldots,n"),
#   (3) köşeli ayraçlı iki uçlu aralık ("[−1,1]", "[0,1)", "(0,1]"),
#   (4) YALNIZ kaynak dil açıkça İngilizce verildiyse: kalan her rakam-virgül-
#       rakam (İngilizcede ondalık ayraç noktadır; "(0,0)" koordinattır),
#       binlik ayracı hariç.
# Yalnız derlenecek tam belgede (Phase 6) uygulanır: QC ve EPUB gövdesi
# değişmez. \text{}, \tag{}, \label{} gibi metin kipi argümanlarına dokunulmaz.
_MATH_NUM = r"[-−+]?\d+(?:\.\d+)?"
_MATH_ELLIPSIS = r"(?:\\(?:ldots|cdots|dotsc|dotsb|dotsm|dotso|dots)(?![A-Za-z])|\.\.\.|…)"
_MATH_ITEM = r"(?:" + _MATH_NUM + r"|" + _MATH_ELLIPSIS + r")"
# Boşluksuz virgülle bağlı öğe dizisi: boşluklu virgül ("1,5, 2,5") üst
# düzey ayraçtır, öğeleri ondalık olabilir; ayrı diziler sayılır.
_MATH_LIST_RUN_RE = re.compile(
    r"(?<![\w.,])" + _MATH_ITEM + r"(?:," + _MATH_ITEM + r")+(?![\w.])")
_MATH_ELLIPSIS_RE = re.compile(_MATH_ELLIPSIS)
_MATH_INTERVAL_RE = re.compile(
    r"(?:\\left\s*)?([\[(])\s*" + _MATH_NUM + r"\s*,\s*" + _MATH_NUM
    + r"\s*(?:\\right\s*)?([\])])")
_MATH_THOUSANDS_RE = re.compile(r"[1-9]\d{0,2}(?:,\d{3})+(?:\.\d+)?")
_MATH_DIGIT_COMMA_RE = re.compile(r"(?<=\d),(?=\d)")
_MATH_TEXT_ARG_RE = re.compile(
    r"\\(?:text[a-z]*|mbox|hbox|fbox|tag|label|ref|eqref|intertext)\*?\s*"
    r"\{(?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*\}")


def _is_math_zone(zone: str) -> bool:
    """Stash bölgesi matematik mi ($, $$, \\[, matematik ortamı, \\ensuremath)?"""
    return (zone.startswith("$") or zone.startswith("\\[")
            or zone.startswith("\\ensuremath")
            or bool(_MATH_ENV_RE.match(zone)))


def space_list_commas(math: str, english_source: bool = False) -> Tuple[str, int]:
    """Matematik metninde LİSTE virgüllerinin (rakam,rakam) ardına boşluk
    koy; ``(metin, değişen virgül sayısı)``. Kurallar yukarıda (F2-05)."""
    masks: List[str] = []

    def _mask(m: "re.Match[str]") -> str:
        masks.append(m.group(0))
        return f"\x00M{len(masks) - 1}\x00"

    work = _MATH_TEXT_ARG_RE.sub(_mask, math)
    spaced: Set[int] = set()

    def _mark(span_start: int, span_text: str) -> None:
        for c in _MATH_DIGIT_COMMA_RE.finditer(span_text):
            spaced.add(span_start + c.start())

    for m in _MATH_LIST_RUN_RE.finditer(work):
        run = m.group(0)
        items = [x.strip() for x in run.split(",")]
        numeric = [x for x in items if not _MATH_ELLIPSIS_RE.fullmatch(x)]
        thousands = bool(_MATH_THOUSANDS_RE.fullmatch(run))
        if len(numeric) < len(items):              # (2) üç nokta
            _mark(m.start(), run)
        elif len(numeric) >= 3 and not thousands:  # (1) uzun liste
            _mark(m.start(), run)
        elif english_source and not thousands:     # (4) İngilizce kaynak
            _mark(m.start(), run)
    for m in _MATH_INTERVAL_RE.finditer(work):     # (3) aralık
        if "[" in (m.group(1), m.group(2)) or "]" in (m.group(1), m.group(2)):
            _mark(m.start(), m.group(0))
        elif english_source and not _MATH_THOUSANDS_RE.search(m.group(0)):
            _mark(m.start(), m.group(0))
    if not spaced:
        return math, 0
    out = []
    prev = 0
    for pos in sorted(spaced):
        out.append(work[prev:pos + 1])
        out.append(" ")
        prev = pos + 1
    out.append(work[prev:])
    work = "".join(out)
    for i, zone in enumerate(masks):
        work = work.replace(f"\x00M{i}\x00", zone)
    return work, len(spaced)


# ---------------------------------------------------------------------------
# G3a-6 (2026-10-05, mt okuma MT-22): \substack satır ayırıcısı.
# ---------------------------------------------------------------------------
# Marker çok satırlı alt sınırı ``\substack{j=1\ j \neq i}`` yazıyor: satır
# ayırıcı ``\\`` yerine kontrol boşluğu ``\ `` (aynı kökten tek sütunlu
# ``\begin{array}{c} j=1 \ j \neq i \end{array}``). PDF'te koşullar Σ altında
# alt alta değil yan yana basılıyordu (derlemde 50+ kez).
# Kural muhafazakârdır: içerikte hiç ``\\`` yoksa ve EN DIŞ düzeydeki her
# ``\ `` ile ayrılan HER parça bir bağıntı içeriyorsa (her parça bir koşul:
# "j=1", "j \neq i", "(x_i, y_i) \in S") ``\ `` -> ``\\``. Bir yanı bağıntısız
# boşluk ("\text{tüm}\ i") bilinçli boşluk olabilir, dokunulmaz. array yalnız
# tek sütunluysa ve alt/üst simge konumundaysa ele alınır (çok sütunlu
# array'de ``\ `` hücre içi boşluktur).
_SUBSTACK_OPEN_RE = re.compile(r"\\substack\s*\{")
_SCRIPT_ARRAY_OPEN_RE = re.compile(
    r"[_^]\s*\{\s*\\begin\{array\}\s*\{\s*[lcr]\s*\}")
_ARRAY_END_RE = re.compile(r"\\(begin|end)\{array\}")
_SUBSTACK_REL_RE = re.compile(
    r"[=<>≤≥≠∈∉⊂⊆⊃⊇≪≫→]"
    r"|\\(?:neq?|leq?|geq?|leqslant|geqslant|in|notin|ni|subset|subseteq"
    r"|supset|supseteq|ll|gg|to|sim|approx|equiv|mid)(?![A-Za-z])")


def _top_level_control_spaces(content: str) -> Optional[List[int]]:
    """*content* içinde en dış düzeydeki ``\\ `` konumları; ``\\\\`` varsa
    (satırlar zaten ayrılmış) ``None``."""
    spots: List[int] = []
    depth = 0
    i = 0
    n = len(content)
    while i < n:
        ch = content[i]
        if ch == "\\" and i + 1 < n:
            nxt = content[i + 1]
            if nxt == "\\":
                return None
            if nxt in " \t\n" and depth == 0:
                spots.append(i)
            i += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    return spots


def _rows_from_control_spaces(content: str) -> Optional[str]:
    """Kontrol boşluklarını satır ayırıcıya çevrilmiş içerik ya da ``None``."""
    spots = _top_level_control_spaces(content)
    if not spots:
        return None
    parts, prev = [], 0
    for pos in spots:
        parts.append(content[prev:pos])
        prev = pos + 2
    parts.append(content[prev:])
    if not all(_SUBSTACK_REL_RE.search(p) for p in parts):
        return None
    out, prev = [], 0
    for pos in spots:
        out.append(content[prev:pos])
        out.append("\\\\" + content[pos + 1])
        prev = pos + 2
    out.append(content[prev:])
    return "".join(out)


def _brace_group_end(text: str, open_pos: int) -> int:
    """``text[open_pos] == '{'`` grubunun kapanış ``}`` konumu (yoksa -1).
    Kaçışlı ``\\{`` / ``\\}`` sayılmaz."""
    depth = 0
    i = open_pos
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


def fix_substack_rows(math: str) -> Tuple[str, int]:
    """Matematik metninde ``\\substack`` / alt-üst simgedeki tek sütunlu
    ``array`` satırlarını ayır (bkz. G3a-6 açıklaması). ``(yeni, sayı)``."""
    if "\\substack" not in math and "array" not in math:
        return math, 0
    edits: List[Tuple[int, int, str]] = []
    for m in _SUBSTACK_OPEN_RE.finditer(math):
        end = _brace_group_end(math, m.end() - 1)
        if end < 0:
            continue
        fixed = _rows_from_control_spaces(math[m.end():end])
        if fixed is not None:
            edits.append((m.end(), end, fixed))
    for m in _SCRIPT_ARRAY_OPEN_RE.finditer(math):
        depth, end = 1, -1
        for e in _ARRAY_END_RE.finditer(math, m.end()):
            depth += 1 if e.group(1) == "begin" else -1
            if depth == 0:
                end = e.start()
                break
        if end < 0:
            continue
        fixed = _rows_from_control_spaces(math[m.end():end])
        if fixed is not None:
            edits.append((m.end(), end, fixed))
    if not edits:
        return math, 0
    out, prev, applied = [], 0, 0
    for start, end, fixed in sorted(edits):
        if start < prev:  # iç içe eşleşme: dıştaki zaten değişti
            continue
        out.append(math[prev:start])
        out.append(fixed)
        prev = end
        applied += 1
    out.append(math[prev:])
    return "".join(out), applied


# ---------------------------------------------------------------------------
# G3a-5 (2026-10-05, mt okuma MT-21): formül/sayıdan sonra ek önünde boşluk.
# ---------------------------------------------------------------------------
# Marker '$…$' çevresine boşluk koyuyor; yer tutucu geri yazılınca boşluk
# kalıyor: "$t^{1/3}$ ’nin", "Şekil 2.1 ’dan", "Denk. (1.3) ’ın". Türkçede
# kesme işaretiyle ayrılan ekin önünde boşluk olmaz (TDK). Yalnız boşluk
# silinir; ekin ünlü uyumu modelin işidir, dokunulmaz. Muhafazakâr kural:
#   * önceki öğe satır içi formül / atıf (stash: '$…$', \ensuremath, \ref,
#     \eqref, \cite …), rakam ya da kapanış ayracı (')' — '\)' dahil — ']');
#   * kesmeden (’ ya da ') hemen sonra küçük harfle başlayan, en çok
#     _SUFFIX_MAX_LETTERS harflik ek; ekten sonra harf/rakam ya da yeniden
#     kesme gelmez ("'nin'" tırnak içindeki sözcüktür, ek değil).
# Harften sonraki boşluk ("Ankara ’da") kapsam dışıdır (MT-21 formül/sayı).
# Türkçe ekin en uzun biçimi ("’lerindekilerden") 15 harf; 12 harf sınırı
# bunların neredeyse tümünü kapsar, sözcük sanılacak uzun dizgiyi dışarıda
# bırakır.
_SUFFIX_MAX_LETTERS = 12
_SUFFIX_AFTER_SPACE_RE = re.compile(
    r"(__SANSTASH_(\d+)__|[0-9)\]])[ \t]+"
    r"(?=[’'][a-zçğıöşüâîû]{1," + str(_SUFFIX_MAX_LETTERS) + r"}(?![’'\w]))")
# Stash bölgesi satır içi formül ya da atıf mı (display matematik değil)?
_SUFFIX_ZONE_RE = re.compile(
    r"\$(?!\$)|\\ensuremath\b"
    r"|\\(?:ref|eqref|cite|pageref|cref|autoref)\s*\{")


def join_suffix_after_formula(body: str, stash: List[str]) -> Tuple[str, int]:
    """Stash'li gövdede formül/atıf/sayı ile kesmeli ek arasındaki boşluğu
    sil (bkz. G3a-5). ``(yeni gövde, sayı)``."""
    count = 0

    def _repl(m: "re.Match[str]") -> str:
        nonlocal count
        idx = m.group(2)
        if idx is not None:
            i = int(idx)
            if i >= len(stash) or not _SUFFIX_ZONE_RE.match(stash[i]):
                return m.group(0)
        count += 1
        return m.group(1)

    return _SUFFIX_AFTER_SPACE_RE.sub(_repl, body), count


def _math_wrap(name: str, char: str) -> str:
    """Metin modu sarmalaması: ``θ`` -> ``$\\theta$``."""
    return "$\\" + name + "$"


def _push_wrap_protected(zones: List[str], zone: str) -> str:
    zones.append(zone)
    return f"__SANWRAP_{len(zones) - 1}__"


def _stash_wrap_protected(text: str) -> Tuple[str, List[str]]:
    """_WRAP_PROTECTED_RE ve \\(...\\) bölgelerini ``__SANWRAP_N__`` ile
    değiştirir."""
    zones: List[str] = []

    def _push(m: "re.Match[str]") -> str:
        return _push_wrap_protected(zones, m.group(0))

    text = _WRAP_PROTECTED_RE.sub(_push, text)
    text = _PAREN_MATH_RE.sub(_push, text)
    return text, zones


_WRAP_PLACEHOLDER_RE = re.compile(r"__SANWRAP_(\d+)__")


def _restore_wrap_protected(text: str, zones: List[str]) -> str:
    """Tek geçişli geri yükleme (bölge başına tam metin taraması, 400
    bağlantılı kitapta +0,3 sn tutuyordu). Bir bölge başka bir bölgenin
    yer tutucusunu içerebilir ('\\(\\url{..}\\)'): o bir tur daha ister."""
    for _ in range(3):
        text, n = _WRAP_PLACEHOLDER_RE.subn(
            lambda m: zones[int(m.group(1))], text)
        if not n:
            break
    return text


def _heading_arg_spans(text: str) -> List[Tuple[int, int]]:
    """Başlık komutlarının ``[kısa]`` ve ``{başlık}`` argüman içerikleri
    (``(başlangıç, bitiş)``; parantez-dengeli, kaçışlı karakter atlanır)."""
    spans: List[Tuple[int, int]] = []
    n = len(text)
    for m in _HEADING_CMD_OPEN_RE.finditer(text):
        i = m.end()
        if i < n and text[i] == "[":
            close = text.find("]", i + 1)
            if close == -1:
                continue
            spans.append((i + 1, close))
            i = close + 1
            while i < n and text[i] in " \t":
                i += 1
        if i >= n or text[i] != "{":
            continue
        depth, j = 1, i + 1
        while j < n and depth:
            c = text[j]
            if c == "\\":
                j += 2
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            j += 1
        if depth == 0:
            spans.append((i + 1, j - 1))
    return spans


# ---------------------------------------------------------------------------
# Invisible / zero-width character map (EPUB artifacts + PDF remnants)
# ---------------------------------------------------------------------------
# These must be cleaned BEFORE stash to prevent regex mismatch.
# Map: Unicode codepoint -> replacement string.

_INVISIBLE_CHAR_MAP: Dict[str, str] = {
    "\ufeff": "",       # BOM (zero-width no-break space)
    "\u200b": "",       # ZWSP (zero-width space)
    "\u200c": "",       # ZWNJ (zero-width non-joiner)
    "\u200d": "",       # ZWJ (zero-width joiner)
    "\u00a0": " ",      # NBSP -> normal space
    "\u00ad": "",       # Soft hyphen -> remove
    "\u200e": "",       # Left-to-right mark
    "\u200f": "",       # Right-to-left mark
    "\u2060": "",       # Word joiner
    "\u2028": "\n",     # Line separator -> newline
    "\u2029": "\n",     # Paragraph separator -> newline
}

# Single regex matching any of the invisible characters above.
_INVISIBLE_CHAR_RE = re.compile(
    "[" + "".join(re.escape(c) for c in _INVISIBLE_CHAR_MAP.keys()) + "]"
)

# C0 control chars (except tab \x09 / newline \x0a / CR \x0d), DEL, and C1
# range. Marker occasionally embeds these (notably NULL \x00) around numbers
# in math, breaking XeLaTeX. Stripped outright. (Yol A fix, 2026-06-19)
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

# Text-mode Greek -> math. Marker emits Greek letters as \textit{θ} or bare θ
# in prose; the text font (Latin Modern Roman) lacks Greek glyphs -> BLANK in
# the PDF. Wrap in math so the math font renders them. (Yol A fix, 2026-06-19)
_GREEK_NAME_MAP: Dict[str, str] = {
    "θ": "theta", "ϑ": "vartheta", "Θ": "Theta", "π": "pi", "ϖ": "varpi",
    "Π": "Pi", "σ": "sigma", "ς": "varsigma", "Σ": "Sigma", "φ": "phi",
    "ϕ": "varphi", "Φ": "Phi", "λ": "lambda", "Λ": "Lambda", "μ": "mu",
    "ν": "nu", "γ": "gamma", "Γ": "Gamma", "α": "alpha", "β": "beta",
    "δ": "delta", "Δ": "Delta", "ε": "epsilon", "ϵ": "epsilon", "Ω": "Omega",
    "ω": "omega", "ρ": "rho", "τ": "tau", "ψ": "psi", "Ψ": "Psi", "ξ": "xi",
    "Ξ": "Xi", "ζ": "zeta", "η": "eta", "κ": "kappa", "χ": "chi", "υ": "upsilon",
}
_GREEK_CHARS = "".join(_GREEK_NAME_MAP.keys())
_GREEK_TEXTCMD_RE = re.compile(
    r"\\(?:textit|textbf|textrm|textsf|emph)\{([" + _GREEK_CHARS + r"])\}"
)
_GREEK_BARE_RE = re.compile(r"[" + _GREEK_CHARS + r"]")
# R6d (kalite 2026-09-25): Yunanca SÖZCÜK matematik değildir. Eskiden
# "σελἡνη" (Verne'in Gutenberg notu, Fransızca test metni) harf harf
# '$\sigma$$\epsilon$$\lambda$ἡ$\nu$$\eta$' oluyor, PDF'te italik ve aralıklı
# basılıyordu. Yunan yazısı bloğu: Yunanca ve Kıpti (U+0370-03FF) ile
# Genişletilmiş Yunanca (U+1F00-1FFF, politonik). Kesintisiz bir Yunan
# harf dizisi şu durumda sözcüktür ve metinde kalır (yazı tipi şablonun
# işidir): en az _GREEK_WORD_MIN_LEN harf, ya da haritada olmayan (vurgulu,
# politonik, ο/ι gibi Latin benzeri) bir harf içerir. Tek harf ("ΔT", "μm")
# ve iki harflik harita dizisi ("Δσ", "αβ") eskisi gibi sarılır.
_GREEK_SCRIPT_RE = re.compile(r"[Ͱ-Ͽἀ-῿]")
_GREEK_WORD_MIN_LEN = 3


def _is_greek_word_char(text: str, pos: int) -> bool:
    """``text[pos]`` bir Yunanca sözcüğün harfi mi (bkz. ``_GREEK_WORD_MIN_LEN``)."""
    start = pos
    while start > 0 and _GREEK_SCRIPT_RE.match(text, start - 1):
        start -= 1
    end = pos + 1
    while end < len(text) and _GREEK_SCRIPT_RE.match(text, end):
        end += 1
    run = text[start:end]
    return (len(run) >= _GREEK_WORD_MIN_LEN
            or any(c not in _GREEK_NAME_MAP for c in run))

# G3 (2026-09-24): metin fontunda glifi OLMAYAN bağıntı/ok sembolleri. Aynı
# Yunan harfi sorunu: prose'daki çıplak "M ≫ m" PDF'de sembolsüz basılıyordu
# (bir test koşusunun book_tr.log'u: "Missing character: There is no ≫
# (U+226B) in font lmroman10-regular"). Liste yalnız derleme günlüklerinde
# eksik glif olarak GÖRÜLMÜŞ sembollerdir (üç test kitabında): ≫ ≪ ≤ ≥
# (lmroman), → ⇔ (STIX Two Text). ∞ bu fontlarda var (aynı kitabın
# tablosunda uyarı yok) — bilerek dışarıda.
_TEXT_MATH_SYMBOL_MAP: Dict[str, str] = {
    "≫": "gg", "≪": "ll", "≤": "leq", "≥": "geq",
    "→": "rightarrow", "⇔": "Leftrightarrow",
}
_TEXT_MATH_SYMBOL_RE = re.compile(
    r"[" + "".join(_TEXT_MATH_SYMBOL_MAP.keys()) + r"]"
)
# G3'ün ürettiği tek komutluk math içerikleri ("\gg", "\leq" ...). TEK
# KAYNAK: QC broken_inline_math bunları parça saymaz — '$m$ ≪ M' ->
# '$m$ $\ll$ M' geçerli ve tam bir ifadedir, MT parçalanması değildir.
TEXT_MODE_SYMBOL_COMMANDS = frozenset(
    "\\" + name for name in _TEXT_MATH_SYMBOL_MAP.values()
)
# Ters eşlem (komut adı -> Unicode karakter). EPUB çıktısı ('$\alpha$',
# '$m \ll M$' gibi basit matematiği düz metne çevirirken) kullanır; tek
# kaynak yukarıdaki iki tablodur. Aynı ada iki karakter düşerse (epsilon:
# ε, ϵ) tablodaki ilki seçilir.
LATEX_SYMBOL_TO_UNICODE: Dict[str, str] = {}
for _char, _name in (list(_GREEK_NAME_MAP.items())
                     + list(_TEXT_MATH_SYMBOL_MAP.items())):
    LATEX_SYMBOL_TO_UNICODE.setdefault(_name, _char)
del _char, _name

# ---------------------------------------------------------------------------
# EPUB artifact patterns (conversion remnants from EPUB -> Markdown -> LaTeX)
# ---------------------------------------------------------------------------

# @@NEXT, @@UNKNOWN0001@@, @@PAGEBREAK etc. -- EPUB converter placeholders
_EPUB_AT_PLACEHOLDER_RE = re.compile(r"@@[A-Z]+(?:\d+@@)?")

# @Bilinmeyen0000, @Unknown123 -- EPUB unknown reference placeholders
_EPUB_NAMED_PLACEHOLDER_RE = re.compile(r"@[A-Za-z]+\d+\b")

# kindle:embed:XXXX -- Kindle-specific embed URLs
_EPUB_KINDLE_EMBED_RE = re.compile(
    r"kindle:embed:[A-Za-z0-9?=&#%._/-]+",
)

# part0042.html#ch7-sec3 -- EPUB internal cross-references (standalone only)
# Must NOT match inside \href{...} -- we check context in the method.
_EPUB_INTERNAL_LINK_RE = re.compile(
    r"(?<![\{])part\d+\.x?html?#[A-Za-z0-9._-]+",
)

# Duplicate section/chapter: \section{Title}\n\chapter{Title} (same title)
# Keep \chapter, remove \section.  We use a backreference to match identical
# titles. Yildiz YAKALANIR ve korunur: eskiden \chapter*'in yildizi
# replacement'ta dusuyor, numarasiz onmadde numarali bolume donusuyordu (#56).
_EPUB_DUPLICATE_HEADING_RE = re.compile(
    r"\\section\*?\{([^}]+)\}\s*\n\s*\\chapter(\*?)\{\1\}",
)

# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------

# --- Publisher artifacts (Springer, Creative Commons) ---

_PUBLISHER_PATTERNS: List[re.Pattern] = [
    # Springer CrossMark / "Check for updates"
    re.compile(
        r"\\includegraphics[^}]*\{[^}]*crossmark[^}]*\}",
        re.IGNORECASE,
    ),
    re.compile(
        r"\\includegraphics[^}]*\{[^}]*check[-_]?for[-_]?updates[^}]*\}",
        re.IGNORECASE,
    ),
    # Textual "Check for updates" (standalone line)
    re.compile(r"^\s*Check\s+for\s+updates\s*$", re.MULTILINE | re.IGNORECASE),
    # Creative Commons boilerplate (multi-line)
    re.compile(
        r"This\s+(chapter|article|work)\s+is\s+licensed\s+under\s+"
        r"(a\s+)?Creative\s+Commons.*?(?=\n\n|\Z)",
        re.DOTALL | re.IGNORECASE,
    ),
    # Open Access notice
    re.compile(
        r"Open\s+Access\s+This\s+(chapter|article|book).*?(?=\n\n|\Z)",
        re.DOTALL | re.IGNORECASE,
    ),
    # Copyright line: "(c) The Author(s) YYYY" or "Copyright (c) YYYY Springer"
    re.compile(
        r"^\s*(?:\\textcopyright|\(c\)|©)\s+(?:The\s+Author|Springer|"
        r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*).*?\d{4}.*$",
        re.MULTILINE | re.IGNORECASE,
    ),
    # Springer Nature link (bare DOI boilerplate). NOT \href{}/\url{} icindeki
    # DOI'lere dokunma (kaynakca citation'lari mesru) ve { } sinirini ASLA asma
    # (yoksa \href{doi}{text} yapisinin kapanis parantezleri yenir -> brace
    # dengesizligi -> XeLaTeX belgeyi yutar). Sadece bare/standalone DOI silinir.
    re.compile(
        r"(?<![{/=])https?://doi\.org/10\.\d{4,5}/[^\s}{]+",
    ),
]

# --- PDF page header artifacts ---
# Patterns like "2 1 Introduction", "88 6 Operator Networks", "xvi Contents"
# These are running headers/footers that Marker picks up from the PDF.

_HEADER_PATTERNS: List[re.Pattern] = [
    # "xvi Contents", "xiv About the Authors", "xviii Preface" etc.
    re.compile(
        r"^\s*(?:x{0,3}(?:i{1,3}|iv|v|vi{0,3}|ix))\s+"
        r"(?:Contents|About\s+the\s+Authors?|Preface|Acknowledgment|"
        r"Yazarlar\s+Hakk[ıi]nda|[İI][çc]indekiler)\s*$",
        re.MULTILINE | re.IGNORECASE,
    ),
    # Stray page number + chapter number + title on a single line
    # e.g. "2 1 Introduction", "88 6 Operator Networks"
    # Must be a standalone line (no other content), min 2 tokens.
    re.compile(
        r"^\s*\d{1,4}\s+\d{1,2}\s+[A-Z][A-Za-z\s,:-]{3,60}\s*$",
        re.MULTILINE,
    ),
    # Turkish version: "6.2 Operatorler 89"
    re.compile(
        r"^\s*\d{1,2}(?:\.\d{1,2})?\s+[A-Za-z\u00C0-\u024F\u0130\u0131]{2,}"
        r"(?:\s+[A-Za-z\u00C0-\u024F\u0130\u0131]+)*\s+\d{1,4}\s*$",
        re.MULTILINE,
    ),
]

# F2-11 (2026-09-29): tek başına duran tarih satırı ("12 Nisan 1961",
# "3 March 1998", "12 de abril de 1961") üst bilgi DEĞİLDİR: mektup, günlük,
# önsöz ve ithaf tarihidir. Yukarıdaki Türkçe desen ("6.2 Operatorler 89")
# bunları siliyordu. Ay adları: Türkçe + TeX yolunun kaynak dilleri
# (İngilizce, Almanca, Fransızca, İspanyolca, İtalyanca; Rusça ay adları
# Kiril olduğu için üst bilgi desenine zaten uymaz).
_MONTH_NAMES = (
    "ocak|şubat|mart|nisan|mayıs|haziran|temmuz|ağustos|eylül|ekim|kasım|aralık"
    "|january|february|march|april|may|june|july|august|september|october"
    "|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
    "|januar|jänner|februar|märz|mai|juni|juli|oktober|dezember"
    "|janvier|février|mars|avril|juin|juillet|août|septembre|octobre"
    "|novembre|décembre"
    "|enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre"
    "|octubre|noviembre|diciembre"
    "|gennaio|febbraio|aprile|maggio|giugno|luglio|settembre|ottobre|dicembre"
)
_DATE_LINE_RE = re.compile(
    r"\s*\d{1,2}\.?\s+(?:de\s+)?(?:" + _MONTH_NAMES + r")\.?"
    r"(?:\s+(?:de|del))?\s+\d{2,4}\s*",
    re.IGNORECASE,
)

# --- MT prompt leakage patterns ---
# Imported from patterns.py (DRY -- single source of truth)
_PROMPT_LEAK_PATTERNS = PROMPT_LEAK_PATTERNS

# --- CJK character artifact patterns ---
# HY-MT 7B training data artifacts: Chinese/Japanese/Korean characters
# that leak into Turkish translation output.
_CJK_ARTIFACT_RE = re.compile(
    r"[\u4e00-\u9fff"        # CJK Unified Ideographs (Chinese)
    r"\u3000-\u303f"         # CJK Punctuation
    r"\u3040-\u309f"         # Hiragana (Japanese)
    r"\u30a0-\u30ff"         # Katakana (Japanese)
    r"\uac00-\ud7af"         # Korean Hangul Syllables
    r"\uff00-\uffef"         # Halfwidth/Fullwidth Forms (CJK punctuation)
    r"]+"
)

# --- Broken href patterns ---
# \href{#page-XXX-0}{\[ NNN}] from Marker PDF extraction.
# These are malformed citation-style references where the display text
# is just a number wrapped in broken brackets: \[ NNN}] or [NNN}
# IMPORTANT: the pattern must NOT match valid hrefs like
# \href{#page-9-0}{4.3 CD NOZZLE GEOMETRY} or \href{#page-112-0}{Figure 4.1.}
# We require the display text to start with \[ or [ immediately after the
# opening brace, with only digits (and optional commas) as content.
_BROKEN_HREF_RE = re.compile(
    r"\\href\{#page-\d+-\d+\}\{\\?\[\s*(\d+(?:\s*,\s*\d+)*)\s*\\?\}?\]?"
)

# G3a-4 (2026-10-05, iç onarım notu): iç bağlantılar. Marker'ın
# '\\href{#page-N-M}{…}' bağlantılarının hedefi yok (dönüştürücü Marker'ın
# '<span id="page-…">' çapalarını atıyor; bir test raporunda 158 bağlantının
# hiçbiri bir yere gitmiyordu). Ad hedefi: \\hypertarget{ad} ya da \\label{ad}.
_INTERNAL_HREF_RE = re.compile(
    r"\\href\s*(?:\[[^\]\n]*\])?\s*\{\s*#([^{}\s]+)\s*\}\s*\{")
_HYPERTARGET_NAME_RE = re.compile(r"\\hypertarget\s*\{([^{}]+)\}")
_LABEL_NAME_RE = re.compile(r"\\label\s*\{([^{}]+)\}")
# hyperref'in kendi ürettiği hedef adları (hypertexnames): bunlara giden
# bağlantı geçerlidir ("page.3", "chapter.2", "section.1.2", "Item.5" …).
_HYPERREF_AUTO_DEST_RE = re.compile(
    r"(?:page|part|chapter|section|subsection|subsubsection|paragraph"
    r"|subparagraph|appendix|figure|table|subfigure|subtable|equation|Item"
    r"|Hfootnote|cite|lstlisting|theorem|AMS)\.\S+$|doc-start$")

# --- Batch processing comments ---
_BATCH_COMMENT_RE = re.compile(
    r"^\s*%+\s*-{2,}\s*(?:Batch|BATCH|batch|PAGES?|Pages?)\s+.*$",
    re.MULTILINE,
)

# --- Springer "Check for updates" logo ---
_CHECK_UPDATES_LOGO_RE = re.compile(
    r"\\begin\{wrapfigure\}.*?check[-_]?for[-_]?updates.*?\\end\{wrapfigure\}",
    re.DOTALL | re.IGNORECASE,
)

# Yol A fix (2026-06-19): Springer "Check for updates" crossmark rozeti her
# bolum basinda Marker tarafindan caption'siz bir \begin{figure} olarak
# cikariliyor (wrapfigure DEGIL -> ustteki regex kacirdi). Bu rozetler kitap
# icerigi degil yayinci metadata'si. Sezgi: \chapter hemen ardindan gelen,
# CAPTION'SIZ, tek-includegraphics figur = rozet. Gercek bolum-acilis figurleri
# neredeyse her zaman caption'lidir. (negatif lookahead \caption ile guvene alindi)
_CHAPTER_BADGE_FIGURE_RE = re.compile(
    r"(\\chapter(?:\[[^\]]*\])?\{[^}]*\}\s*)"
    r"\\begin\{figure\}(?:(?!\\end\{figure\}|\\caption).)*?"
    r"\\includegraphics(?:(?!\\end\{figure\}|\\caption).)*?\\end\{figure\}\s*",
    re.DOTALL,
)
# F2-10 (2026-09-29): yukarıdaki sezgi her altyazısız bölüm açılış görselini
# siliyordu (resimli kitapta bölüm gravürü, EPUB süs görseli, ithaf
# fotoğrafı; ölçülen: bir kitapta ithaf fotoğrafı 500x142 px, bir başkasında
# 10 süs görseli). Artık yalnız DOĞRULANMIŞ rozet silinir: tek görsel, adı
# Marker'ın PDF görsel adı (_page_N_Picture_M), dosyası bulunup küçük ve
# kareye yakın ölçülür (Springer "Check for updates": derlemde 87x101 ile
# 95x95 px arası) ve ardından altyazı paragrafı gelmez. Dosya bulunamazsa
# görsel KALIR (kayıpsızlık) ve raporda uyarı olur.
_MARKER_IMAGE_NAME_RE = re.compile(
    r"(?:^|/)_page_\d+_(?:Picture|Figure)_\d+(?:\.[A-Za-z0-9]+)?$")
_INCLUDEGRAPHICS_NAME_RE = re.compile(
    r"\\includegraphics\s*(?:\[[^\]]*\])?\s*\{([^{}]+)\}")
# Rozetin en uzun kenarı (px) ve en/boy oranı üst sınırı.
_BADGE_MAX_SIDE_PX = 160
_BADGE_MAX_ASPECT = 1.3
# Uzantısız \includegraphics adı için denenecek uzantılar (graphicx sırası).
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".PNG", ".JPG", ".JPEG")

# ---------------------------------------------------------------------------
# HTML artifact patterns (NEW -- session 6, 2026-03-03)
# ---------------------------------------------------------------------------
# HY-MT 7B enters repetition loops when it sees patterns like "as follows:"
# and generates thousands of HTML tags (colgroup, tbody, etc.).
# These patterns clean ALL HTML artifacts from translated LaTeX output.

# Phase 1: Convert meaningful HTML to LaTeX equivalents BEFORE stripping.
# <sup>X</sup> -> \textsuperscript{X}
_HTML_SUP_RE = re.compile(r"<sup>(.*?)</sup>", re.IGNORECASE | re.DOTALL)
# <sub>X</sub> -> \textsubscript{X}
_HTML_SUB_RE = re.compile(r"<sub>(.*?)</sub>", re.IGNORECASE | re.DOTALL)
# <a href="...">text</a> -> text (keep link text, drop tag)
_HTML_ANCHOR_RE = re.compile(
    r'<a\s+[^>]*href=["\'][^"\']*["\'][^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
# <em>text</em> -> \emph{text}
_HTML_EM_RE = re.compile(r"<em>(.*?)</em>", re.IGNORECASE | re.DOTALL)
# <strong>text</strong> -> \textbf{text}
_HTML_STRONG_RE = re.compile(r"<strong>(.*?)</strong>", re.IGNORECASE | re.DOTALL)

# Phase 2: Strip all remaining HTML tags.
# This single pattern catches ALL HTML open/close/self-closing tags.
# Handles tags with attributes: <div class="foo">, <col style="width:10%"/>, etc.
# The + quantifier on the outer group ensures we catch repetition bursts
# (e.g. 1400 consecutive </colgroup> tags) in one pass.
_HTML_TAG_STRIP_RE = re.compile(
    r"(?:"
    r"</?(?:colgroup|tbody|thead|tfoot|tr|td|th|table|div|span|p|br|hr|ul|ol|li"
    r"|dd|dt|dl|nav|header|footer|section|article|aside|main|figure|figcaption"
    r"|blockquote|pre|code|img|input|button|form|select|option|textarea"
    r"|label|fieldset|legend|details|summary|dialog|template|slot|canvas"
    r"|video|audio|source|track|embed|object|param|iframe|link|meta|style"
    r"|script|noscript|col)\b[^>]*/?>)"
    r"+",  # Greedy + to eat repetition bursts in one match
    re.IGNORECASE,
)

# Phase 3: HTML entities -> proper characters or LaTeX.
_HTML_ENTITY_MAP: Dict[str, str] = {
    "&amp;": r"\&",      # & is special in LaTeX
    "&lt;": "<",
    "&gt;": ">",
    "&nbsp;": "~",       # Non-breaking space in LaTeX
    "&ndash;": "--",
    "&mdash;": "---",
    "&laquo;": "<<",
    "&raquo;": ">>",
    "&quot;": '"',
    "&apos;": "'",
    "&times;": r"$\times$",
    "&divide;": r"$\div$",
    "&plusmn;": r"$\pm$",
    "&deg;": r"$^\circ$",
    "&hellip;": r"\ldots{}",
    "&copy;": r"\textcopyright{}",
    "&reg;": r"\textregistered{}",
    "&trade;": r"\texttrademark{}",
}

# Build a single regex for all known HTML entities.
# 2026-10-05: önündeki ters bölüler de alınır. Dönüştürücü "&"yi "\&" yazar;
# "\&quot;" eskiden "\"" (umlaut aksanı) ve "\&amp;" "\\&" (satır sonu +
# hizalama hatası) oluyordu. Tek sayıdaki ters bölünün sonuncusu "&"nin
# kaçışıdır, düşer (bkz. _html_entity_repl).
_HTML_ENTITY_RE = re.compile(
    r"(\\*)(" + "|".join(re.escape(k) for k in _HTML_ENTITY_MAP.keys()) + ")",
    re.IGNORECASE,
)

# Catch-all for numeric HTML entities: &#123; or &#x1F; etc. Dönüştürücü
# çıktısında "#" de kaçışlıdır: "\&\#39;".
_HTML_NUMERIC_ENTITY_RE = re.compile(r"(\\*)&(?:\\(?=#))?#(x?)([0-9a-fA-F]+);",
                                     re.IGNORECASE)


def _html_entity_repl(m: "re.Match[str]") -> str:
    bs = m.group(1)
    if len(bs) % 2:
        bs = bs[:-1]
    ent = m.group(2)
    return bs + _HTML_ENTITY_MAP.get(ent.lower(), _HTML_ENTITY_MAP.get(ent, ent))


def _html_numeric_entity_repl(m: "re.Match[str]") -> str:
    """Sayısal varlık -> karakter; TeX özel karakteri kaçışlanır ("&#37;" ham
    "%" kalsa satırın kalanını yorum yapar). Çözülemeyen varlık aynen kalır."""
    bs = m.group(1)
    if len(bs) % 2:
        bs = bs[:-1]
    try:
        cp = int(m.group(3), 16 if m.group(2) else 10)
        ch = chr(cp)
    except (ValueError, OverflowError):
        return m.group(0)
    if cp < 32 or 0xD800 <= cp <= 0xDFFF:
        return m.group(0)
    return bs + _QUOTE_TEX_ESCAPES.get(ch, ch)

# Phase 4: Detect lines that are PURELY HTML (no real text content).
# After tag stripping, if a line is empty or only whitespace, it was
# pure HTML.  We handle this in the method, not via a single regex.


# ---------------------------------------------------------------------------
# Mis-translated LaTeX command patterns (NEW -- session 6, 2026-03-03)
# ---------------------------------------------------------------------------
# HY-MT 7B sometimes translates LaTeX command names into Turkish.
# Each entry: regex pattern -> correct LaTeX replacement.

_LATEX_COMMAND_FIXES: List[Tuple[re.Pattern, str]] = [
    # \frontmatter variants
    (re.compile(r"\\[öo]n\s*tarafmatter\b"), r"\\frontmatter"),
    (re.compile(r"\\[öo]n\s*madde\b"), r"\\frontmatter"),
    # \tableofcontents variants
    (re.compile(r"\\tableof[iı][çc]eriks?\b"), r"\\tableofcontents"),
    (re.compile(r"\\[iı][çc]indekiler\s*tablosu\b"), r"\\tableofcontents"),
    # \bottomrule variants
    (re.compile(r"\\[şs]i[şs]e\s*dibirule\b"), r"\\bottomrule"),
    (re.compile(r"\\alt\s*[çc]izgi\b"), r"\\bottomrule"),
    # \backmatter variants
    (re.compile(r"\\arka\s*tarafmatter\b"), r"\\backmatter"),
    (re.compile(r"\\arka\s*madde\b"), r"\\backmatter"),
    # \mainmatter variants
    (re.compile(r"\\ana\s*(?:metin|k[ıi]s[ıi]m)matter\b"), r"\\mainmatter"),
    (re.compile(r"\\ana\s*madde\b"), r"\\mainmatter"),
    # \toprule
    (re.compile(r"\\[üu]st\s*kural\b"), r"\\toprule"),
    (re.compile(r"\\[üu]st\s*[çc]izgi\b"), r"\\toprule"),
    # \midrule
    (re.compile(r"\\orta\s*kural\b"), r"\\midrule"),
    (re.compile(r"\\orta\s*[çc]izgi\b"), r"\\midrule"),
    # \includegraphics
    (re.compile(r"\\resim\s*ekle\b"), r"\\includegraphics"),
    (re.compile(r"\\grafik\s*ekle\b"), r"\\includegraphics"),
    # \textbf / \textit
    (re.compile(r"\\metin\s*kal[ıi]n\b"), r"\\textbf"),
    (re.compile(r"\\metin\s*italik\b"), r"\\textit"),
    # \newpage / \clearpage
    (re.compile(r"\\yeni\s*sayfa\b"), r"\\newpage"),
    (re.compile(r"\\sayfa\s*temizle\b"), r"\\clearpage"),
    # \appendix
    (re.compile(r"\\ek(?:ler)?\s*b[öo]l[üu]m\b"), r"\\appendix"),
    # \bibliography
    (re.compile(r"\\kaynak[çc]a\b"), r"\\bibliography"),
    # \listoffigures / \listoftables
    (re.compile(r"\\[şs]ekil\s*listesi\b"), r"\\listoffigures"),
    (re.compile(r"\\tablo\s*listesi\b"), r"\\listoftables"),
]

# Catch-all: detect LaTeX commands containing Turkish-specific characters.
# These are LOGGED but NOT auto-corrected (false positive risk).
# Turkish chars: ş, ğ, ı, ö, ü, ç, İ, Ş, Ğ, Ö, Ü, Ç
_TURKISH_CHAR_COMMAND_RE = re.compile(
    r"\\[a-zA-Z]*[şğıöüçİŞĞÖÜÇ][a-zA-Z]*\b"
)

# Tam belgede preamble ile gövdenin sınırı. Satır başında aranır: şablonun
# yorumlarında geçen bir "\begin{document}" sınır sayılmasın.
_DOCUMENT_BEGIN_RE = re.compile(r"^[ \t]*\\begin\{document\}", re.MULTILINE)
# Ayrım yalnız TAM belgede yapılır: ilk yorum/boş olmayan satır belge sınıfı
# (ya da ondan önce gelebilen \RequirePackage vb.). Phase 5.5'in yalnız-gövde
# girdisinde verbatim içindeki bir LaTeX örneğinin "\begin{document}" satırı
# sınır sanılıyor, ondan önceki bütün gövde temizlenmeden kalıyordu
# (denetim 2026-09-24 M1#7).
_FULL_DOCUMENT_START_RE = re.compile(
    r"\A\ufeff?(?:[ \t]*(?:%[^\n]*)?\r?\n)*[ \t]*"
    r"\\(?:documentclass|RequirePackage|PassOptionsToPackage|DocumentMetadata)\b"
)


# ---------------------------------------------------------------------------
# Dangerous LaTeX command patterns (NEW -- session 6, 2026-03-03)
# ---------------------------------------------------------------------------
# Defense-in-depth: even though XeLaTeX runs with -no-shell-escape,
# we strip these from the source to prevent any possible exploitation.

_DANGEROUS_LATEX_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # TeX kontrol sözcüğü ilk harf-dışı karakterde biter: '\openin5' iki
    # belirteçtir (\openin + 5). Bu yüzden komut sınırı \b ile değil
    # _CS_END ile yazılır; \b rakamdan önce eşleşmediği için '\openin5=...'
    # ve '\catcode64=11' filtreyi atlıyordu (denetim 25 Eylül 2026).
    # Shell execution
    (re.compile(r"\\immediate\s*\\write\s*18\s*\{[^}]*\}"), "shell exec (\\immediate\\write18)"),
    (re.compile(r"\\write\s*18\s*\{[^}]*\}"), "shell exec (\\write18)"),
    # File inclusion (could read sensitive files)
    # NOTE: \input and \include are ALSO used legitimately in LaTeX.
    # We only filter them when they reference suspicious paths.
    (re.compile(
        r"\\input\s*\{[^}]*(?:/etc/|/proc/|/dev/|~|\.\.)[^}]*\}"
    ), "suspicious \\input path"),
    (re.compile(
        r"\\include\s*\{[^}]*(?:/etc/|/proc/|/dev/|~|\.\.)[^}]*\}"
    ), "suspicious \\include path"),
    # Absolute path inclusion (path traversal)
    (re.compile(
        r"\\(?:input|include)\s*\{/[^}]*\}"
    ), "absolute path \\input blocked"),
    # Ayraçsız TeX sözdizimi: '\input /etc/passwd', '\input~/x', '\input../x'.
    # Göreli '\input bolum2' meşru kalır.
    (re.compile(
        r"\\input" + r"(?![A-Za-z@])" + r"\s*(?:/|~|\.\.)[^\s{}]*"
    ), "suspicious \\input path"),
    # Dosya içeriğini PDF'ye basan komutlar (listings, verbatim, fancyvrb)
    # ve koşullu okuma: mutlak / üst dizin / ev dizini yolu.
    (re.compile(
        r"\\(?:lstinputlisting|verbatiminput|VerbatimInput|InputIfFileExists"
        r"|IfFileExists)\s*(?:\[[^\]]*\]\s*)?\{\s*(?:/|~|[^}]*\.\.)[^}]*\}"
    ), "suspicious file listing path"),
    # File I/O
    (re.compile(r"\\openin(?![A-Za-z@])[^\n]*"), "file read (\\openin)"),
    (re.compile(r"\\openout(?![A-Za-z@])[^\n]*"), "file write (\\openout)"),
    (re.compile(r"\\read(?:line)?(?![A-Za-z@])\s*-?\d+\s*to[^\n]*"), "file read (\\read)"),
    (re.compile(r"\\closein(?![A-Za-z@])"), "file close (\\closein)"),
    (re.compile(r"\\closeout(?![A-Za-z@])"), "file close (\\closeout)"),
    # Category code manipulation (can redefine any character):
    # '\catcode`\@=11', '\catcode64=11', '\catcode"40=11', "\catcode'100 11"
    (re.compile(
        r"\\catcode\s*(?:`\\?.|\d+|\"[0-9A-Fa-f]+|'[0-7]+)\s*=?\s*\d+"
    ), "catcode manipulation"),
    # ^^ gösterimiyle gizlenmiş karakter ('^^5cinput' = '\input')
    (re.compile(r"\^\^[0-9a-f]{2}"), "hex character escape (^^)"),
    # Dynamic command creation via csname -- only filter if contains
    # dangerous payloads (write, input, catcode). Normal csname usage
    # like \csname theequation\endcsname is common and legitimate.
    (re.compile(
        r"\\csname\b[^\\]*(?:write|input|include|catcode|openin|openout)"
        r"[^\\]*\\endcsname"
    ), "dangerous dynamic command (\\csname)"),
    # TeX primitives for arbitrary code execution
    (re.compile(r"\\directlua\b\s*\{[^}]*\}"), "Lua code exec (\\directlua)"),
    (re.compile(r"\\luaexec\b\s*\{[^}]*\}"), "Lua code exec (\\luaexec)"),
    # Shell escape via other engines
    (re.compile(r"\\ShellEscape\b\s*\{[^}]*\}"), "shell exec (\\ShellEscape)"),
    (re.compile(r"\\@@input\b"), "internal input (\\@@input)"),
]


# --- EN -> TR caption/label normalization ---
# Each tuple: (regex_pattern, replacement_template, label_for_report)
# The replacement uses \N backreferences and literal Turkish text.
# We use ~ (non-breaking space) between label and number per Turkish typesetting.

_LABEL_NORMALIZATIONS: List[Tuple[re.Pattern, str, str]] = [
    # NOTE: All replacements use UTF-8 Turkish characters directly.
    # XeLaTeX + UTF-8 source files handle these natively.
    # Legacy LaTeX encodings (B\"{o}l\"{u}m, Tan{\i}m, etc.) are WRONG
    # in this context and would conflict with autofix.py which also
    # produces UTF-8 output.

    # "Fig. N" or "Figure N" -> "Sekil~N"
    (re.compile(r"\bFig(?:ure)?\.?\s*(\d[\d.]*\w?)", re.IGNORECASE),
     r"Şekil~\1", "Fig->Şekil"),

    # "Table N" -> "Tablo~N"
    (re.compile(r"\bTable\s+(\d[\d.]*\w?)", re.IGNORECASE),
     r"Tablo~\1", "Table->Tablo"),

    # "Algorithm N" -> "Algoritma~N"
    (re.compile(r"\bAlgorithm\s+(\d[\d.]*\w?)", re.IGNORECASE),
     r"Algoritma~\1", "Algorithm->Algoritma"),

    # "Chapter N" or "Chap. N" -> "Bolum~N"
    (re.compile(r"\bChap(?:ter)?\.?\s+(\d[\d.]*\w?)", re.IGNORECASE),
     "Bölüm~\\1", "Chapter->Bölüm"),

    # "Eq. N" or "Equation N" -> "Denklem~N"
    (re.compile(r"\bEq(?:uation)?\.?\s*\(?(\d[\d.]*\w?)\)?", re.IGNORECASE),
     r"Denklem~\1", "Eq->Denklem"),

    # "Theorem N" -> "Teorem~N"
    (re.compile(r"\bTheorem\s+(\d[\d.]*\w?)", re.IGNORECASE),
     r"Teorem~\1", "Theorem->Teorem"),

    # "Definition N" -> "Tanim~N"
    (re.compile(r"\bDefinition\s+(\d[\d.]*\w?)", re.IGNORECASE),
     "Tanım~\\1", "Definition->Tanım"),

    # "Lemma N" -> "Onerme~N"
    (re.compile(r"\bLemma\s+(\d[\d.]*\w?)", re.IGNORECASE),
     "Önerme~\\1", "Lemma->Önerme"),

    # "Proof" (standalone, no number) -> "Ispat"
    (re.compile(r"\bProof\b(?!\s*\d)", re.IGNORECASE),
     "İspat", "Proof->İspat"),

    # "Remark N" -> "Aciklama~N"
    (re.compile(r"\bRemark\s+(\d[\d.]*\w?)", re.IGNORECASE),
     "Açıklama~\\1", "Remark->Açıklama"),
]

# Pre-compiled for the idempotency guard: if Turkish labels already exist,
# skip normalization to avoid double-replacement.
_TR_LABELS_PRESENT = re.compile(
    r"(?:Şekil|Tablo|Algoritma|Bölüm|Denklem|Teorem|Tanım|Önerme|İspat|Açıklama)~",
)


# R17 (kalite 2026-09-25): '{' ya da '}' hemen ardından gelip satır sonuna
# kadar yalnız boşluk bırakan '%' LaTeX'in satır sonu yorumudur (makro
# yazımındaki boşluk yutucu: '\resizebox{\textwidth}{!}{%'). Türkçe yüzde
# ('%36') sayıdan önce gelir, '{'/'}' ile satır sonu arasına düşmez.
_EOL_BRACE_COMMENT_RE = re.compile(r"(?<=[{}])%[ \t]*$")
# F2-06 (2026-09-29): '}' + '%' satır sonunda her zaman yorum değildir.
# Biçim komutunun ya da çıplak grubun rakamla biten içeriğinden sonra gelen
# '%' düzyazı yüzdesidir ('\textbf{50}%', '\emph{yüzde 12}%', '{12}%');
# yorum sayılınca yüzde işareti ve satır sonu boşluğu kayboluyordu
# ("verim 50olarak"). Çok argümanlı komutun son argümanı ('\setcounter{page}{1}%')
# ve makro yazımı ('\end{minipage}%', '\resizebox{..}{!}{%') yorum kalır.
_PROSE_GROUP_CMDS = frozenset({
    "textbf", "textit", "emph", "textsl", "textsc", "textup", "textmd",
    "textnormal", "textrm", "textsf", "texttt", "underline", "uline", "mbox",
    "text", "textcolor",
})
_CMD_BEFORE_RE = re.compile(r"\\([A-Za-z]+)\s*$")


def _group_open(line: str, close: int) -> int:
    """``line[close]`` ``}`` ise aynı satırdaki eşi ``{``'in konumu; yoksa -1
    (kaçışlı ``\\{``/``\\}`` sayılmaz)."""
    depth = 0
    i = close
    while i >= 0:
        ch = line[i]
        escaped = i > 0 and line[i - 1] == "\\"
        if ch == "}" and not escaped:
            depth += 1
        elif ch == "{" and not escaped:
            depth -= 1
            if depth == 0:
                return i
        i -= 1
    return -1


def _eol_percent_is_prose(line: str, pct: int) -> bool:
    """``line[pct]`` satır sonu ``%``'si ve ``}``'den sonra geliyor: grup
    rakamla biten düzyazı içeriğiyse (biçim komutu argümanı ya da çıplak
    grup) ``%`` yüzdedir (True); aksi hâlde satır sonu yorumu (False)."""
    close = pct - 1
    if close < 0 or line[close] != "}":
        return False
    open_ = _group_open(line, close)
    if open_ < 0:
        return False
    content = line[open_ + 1:close].rstrip()
    if not content or not content[-1].isdigit():
        return False
    before = line[:open_].rstrip()
    m = _CMD_BEFORE_RE.search(before)
    if m:
        return m.group(1) in _PROSE_GROUP_CMDS
    if before.endswith("}"):
        # Önceki argüman grubu: yalnız \textcolor{renk}{50}% düzyazıdır.
        prev_open = _group_open(before, len(before) - 1)
        m = _CMD_BEFORE_RE.search(before[:prev_open]) if prev_open >= 0 else None
        return bool(m) and m.group(1) == "textcolor"
    # Çıplak grup ('{12}%'): önünde komut, ']' ya da '}' yok.
    return not before.endswith("]")

# R13 (kalite 2026-09-25): float DIŞINDA kalmış altyazı paragrafı. Marker
# ve dönüştürücü altyazıyı çoğu zaman \caption değil ayrı bir paragraf
# ("Şekil 1.1. ...", "\textbf{Tablo 1.1:} ...") olarak verir. Etiket
# sözcükleri: Türkçe + kaynak dillerin tek kaynağı patterns.XREF_LABEL_WORDS.
_TR_FLOAT_LABEL_WORDS: Dict[str, str] = {
    # "Şek." kısaltması İngilizce derlemde de altyazı başıdır ("Şek. 2.10 ...").
    "figure": r"Şekil|Şek\.|Resim|Görsel",
    "table": r"Tablo|Çizelge",
}


def _float_caption_para_re(kind: str) -> "re.Pattern[str]":
    words = [_TR_FLOAT_LABEL_WORDS[kind]] + list(XREF_LABEL_WORDS[kind].values())
    return re.compile(
        r"\A\s*(?:\\(?:textbf|textit|emph|textsc|textsl)\{\s*)?"
        r"(?:" + "|".join(words) + r")[\s~]*\d+(?:[.\-–]\d+)*",
    )


_FLOAT_CAPTION_PARA_RE: Dict[str, "re.Pattern[str]"] = {
    kind: _float_caption_para_re(kind) for kind in ("figure", "table")
}
# Numaradan sonra kesme eki ya da virgül: "Şekil 1.1'de ...", "Şekil 1.1 ve
# 1.2'de ...", "Tablo 2, 3 ve 4'te ...", "Şekil 2.3, ağın ..." atıf ya da
# gövde cümlesidir, altyazı değil.
_CAPTION_REF_SUFFIX_RE = re.compile(
    r"(?:\s*(?:,|ve|ile|-|–)\s*\d+(?:[.\-–]\d+)*)*['’]|\s*,")
# Alt şekil harfi (numaraya bitişik tek küçük harf: "3a", "2b)") ve
# numaradan sonraki ilk anlamlı karakter (F2-07).
_CAPTION_SUBFIG_RE = re.compile(r"[a-z](?![^\W\d_])")
_CAPTION_NEXT_CHAR_RE = re.compile(r"[\s~]*(\S)(\S?)")
# Numaradan sonra bu kadar ya da daha az sözcük: devam/etiket satırı.
_CAPTION_LABEL_ONLY_WORDS = 2
# Paragraf sınırı (boş satır).
_PARA_BREAK_RE = re.compile(r"\n[ \t]*\n")
# Her boyutta şekil/tablo bloğu (yerleşim belirteci ne olursa olsun).
_FLOAT_BLOCK_RE = re.compile(
    r"\\begin\{(figure|table)\}(?:\[[^\]]*\])?(.*?)\\end\{\1\}", re.DOTALL)


def _caption_like_para(para: str, kind: str) -> bool:
    """Paragraf float DIŞINDA kalmış bir ``kind`` altyazısı gibi mi başlıyor?

    Etiket + numara ile başlar ("Şekil 1.4. ...", "\\textbf{Tablo 1.1:}
    ...") ve numaradan sonra kesme eki ya da virgül gelmez (atıf/gövde
    cümlesi değil). ``_relax_figure_placement`` ile
    ``_glue_float_captions`` aynı ölçütü kullanır: ``[H]`` yalnız birlikte
    taşınacak altyazı için kalır.
    """
    m = _FLOAT_CAPTION_PARA_RE[kind].match(para)
    if not m or _CAPTION_REF_SUFFIX_RE.match(para, m.end()):
        return False
    # F2-07 (2026-09-29): numaradan sonra küçük harfle süren paragraf gövde
    # cümlesidir ("Şekil 3 incelendiğinde ...", "Şekil 3 bu durumu gösterir:",
    # "Figure 3 shows ...", "Abb. 3 zeigt ..."); float'a gömülünce okuma
    # sırası değişiyordu. Altyazı numaradan sonra ayraçla (. : — ( ) ya da
    # büyük harfle sürer. Alt şekil harfi ("Şekil 3a") atlanır.
    pos = m.end()
    sub = _CAPTION_SUBFIG_RE.match(para, pos)
    if sub:
        pos = sub.end()
    # Yalnız etiket + numara + en çok iki sözcük ("Şekil 2.23 continued",
    # "Şekil 3 devamı"): gövde cümlesi değil, devam/etiket satırıdır.
    if len(para[pos:].split()) <= _CAPTION_LABEL_ONLY_WORDS:
        return True
    nxt = _CAPTION_NEXT_CHAR_RE.match(para, pos)
    if not nxt or not nxt.group(1).islower():
        return True
    # "Şekil 3 a) üst görünüm": ayraçlı alt şekil etiketi altyazıdır.
    return nxt.group(2) in (")", ".")


def _para_after_at(text: str, pos: int) -> Tuple[int, int]:
    """``pos``'tan sonraki ilk paragrafın ``(başlangıç, bitiş)`` aralığı."""
    start = pos
    while start < len(text) and text[start].isspace():
        start += 1
    end_m = _PARA_BREAK_RE.search(text, start)
    return start, (end_m.start() if end_m else len(text))


def _para_before_at(text: str, pos: int, floor: int = 0) -> Tuple[int, int]:
    """``pos``'tan önceki son paragrafın aralığı (``floor``'un gerisine inmez).

    Geriye doğru büyüyen pencereyle aranır: büyük gövdede her float için
    belgenin başına kadar taramak gereksiz (float başına O(paragraf))."""
    end = pos
    while end > floor and text[end - 1].isspace():
        end -= 1
    lo, win = end, 2048
    while True:
        lo = max(floor, lo - win)
        cuts = [b.end() for b in _PARA_BREAK_RE.finditer(text, lo, end)]
        if cuts:
            return cuts[-1], end
        if lo == floor:
            return floor, end
        win *= 2


def _outside_caption(text: str, m: "re.Match[str]", sides: Dict[str, str],
                     floor: int = 0) -> Optional[Tuple[int, int]]:
    """Float bloğu *m*'nin dış altyazı paragrafının aralığı; yoksa ya da
    eşleme belirsizse ``None``.

    Aday, belgenin o tür için altyazı yönündeki (``sides``, bkz.
    ``_float_caption_sides``) bitişik paragraftır ve ``_caption_like_para``
    olmalıdır. Float dizisinde eşleme yapılmaz (eski davranış): float'un öbür
    yanında aynı türden ``\\caption``'sız bir float varsa en yakın altyazı
    dizinin İLK float'una aittir. Örnek (taranmış bir bölüm): iki görsel art arda,
    ardından "Şekil 1.13" ve "Şekil 1.14"; ikinci görsel "Şekil 1.13"e
    yapıştırılıyordu. Altyazı dizisi ("Şekil 1.2" + "Şekil 1.3" art arda)
    tek başına belirsizlik değildir: en yakın altyazı bitişik float'undur
    (iki sütunlu bir Almanca kitapta: Şekil 1.2).
    """
    kind = m.group(1)
    if sides[kind] == "before":
        c0, c1 = _para_before_at(text, m.start(), floor)
        other = _para_after_at(text, m.end())
    else:
        c0, c1 = _para_after_at(text, m.end())
        other = _para_before_at(text, m.start())
    if not _caption_like_para(text[c0:c1], kind):
        return None
    other_para = text[other[0]:other[1]]
    if (re.match(r"\s*\\begin\{" + kind + r"\}", other_para)
            and "\\caption" not in other_para):
        return None
    return c0, c1


# Eşitlikte varsayılan altyazı yönü: şekilde altta, tabloda üstte (yaygın dizgi).
_DEFAULT_CAPTION_SIDE: Dict[str, str] = {"figure": "after", "table": "before"}


def _float_caption_sides(text: str) -> Dict[str, str]:
    """Belgede dış altyazı float'un önünde mi (``"before"``) arkasında mı
    (``"after"``)? Tür başına (``figure``/``table``) çoğunluk kararı.

    Yalnız bir yanında altyazı olan (öteki yanında olmayan) ``\\caption``'sız
    bloklar oy verir; eşitlikte ``_DEFAULT_CAPTION_SIDE``. Gerekçe: ardışık
    float'larda ("Tablo 7.2 ... [tablo] Tablo 7.3 ... [tablo]") iki yöne de
    bakmak bir float'un altyazısını öncekine yapıştırıyordu
    (ders notu derlemi, farksal 2026-09-28).
    """
    votes = {kind: {"before": 0, "after": 0} for kind in _DEFAULT_CAPTION_SIDE}
    for m in _FLOAT_BLOCK_RE.finditer(text):
        kind = m.group(1)
        if "\\caption" in m.group(2):
            continue
        b0, b1 = _para_before_at(text, m.start())
        a0, a1 = _para_after_at(text, m.end())
        b = _caption_like_para(text[b0:b1], kind)
        a = _caption_like_para(text[a0:a1], kind)
        if b != a:
            votes[kind]["before" if b else "after"] += 1
    sides = {}
    for kind, v in votes.items():
        if v["before"] == v["after"]:
            sides[kind] = _DEFAULT_CAPTION_SIDE[kind]
        else:
            sides[kind] = "before" if v["before"] > v["after"] else "after"
    return sides


# \begin{figure|table}[H] ... \end{...} bloğu (iç içe aynı ortam olmaz).
_FLOAT_H_BLOCK_RE = re.compile(
    r"\\begin\{(figure|table)\}\[H\](.*?)\\end\{\1\}", re.DOTALL)
# G3a-1: ölçülü yerinde birimin açılışı (ikinci geçiş aynı bloğu yeniden
# sarmasın diye tanınır).
_INPLACE_WRAP_OPEN = "\\par\\setbox0\\vbox{"
# Birimin hemen ardındaki dipnot metni: \footnotetext[N]{...} (+ isteğe bağlı
# \setcounter{footnote}{N}); yalnız boşlukla ayrılmış ardışık olanlar.
_FOOTNOTETEXT_OPEN_RE = re.compile(r"\s*\\footnotetext\s*(?:\[[^\]\n]*\])?\s*\{")
_SETCOUNTER_FOOTNOTE_RE = re.compile(
    r"[ \t]*\\setcounter\s*\{footnote\}\s*\{\s*\d+\s*\}%?")


def _code_spans(text: str) -> List[Tuple[int, int]]:
    """Kod örneklerinin (verbatim/lstlisting/minted ortamı, \\verb) aralıkları."""
    return [(c.start(), c.end()) for rx in (_CODE_ENV_RE, _VERB_RE)
            for c in rx.finditer(text)]


def _in_spans(start: int, end: int, spans: List[Tuple[int, int]]) -> bool:
    return any(s < end and start < e for s, e in spans)


def _is_wrapped_float(text: str, m: "re.Match[str]") -> bool:
    """Blok zaten ölçülü yerinde birimin içinde mi (G3a-1)?"""
    return text.endswith(_INPLACE_WRAP_OPEN, 0, m.start())


# ---------------------------------------------------------------------------
# G3a-2 (2026-10-05): kod kutusundaki düzyazı alıntı.
# ---------------------------------------------------------------------------
# Marker kaynaktaki eş aralıklı DÜZYAZI alıntıları (ajan akıl yürütmesi,
# CoT) kod bloğu (```) yazıyor; dönüştürücü bunları lstlisting yapıyor ve
# şablon satır numaralı, çerçeveli kutu basıyordu; Türkçe satır uzayınca kutu
# satırı "↪" ile kırılıyordu (bir test raporunda). Kaynakta bunlar
# girintili, eş aralıklı, numarasız alıntıdır. Dil seçeneksiz lstlisting'in
# içeriği düzyazıysa (cümle yapısı var, kod belirtisi yok) alıntı ortamına
# alınır: quote + \raggedright\ttfamily (elle onarımla aynı görünüm).
# Gerçek kod, düzenli ifade, belirteç/mesaj dizgisi kutuda kalır.
# Sınıflandırıcı muhafazakârdır (yanlış "kod" eski davranıştır; yanlış
# "düzyazı" kodun satır yapısını bozar). Eşikler bir test raporunun 29
# bloğuyla (18 düzyazı, 11 kod; EN+TR 58 blok) ve derlemdeki öteki 16
# lstlisting bloğuyla ayarlandı: 34/36 düzyazı bulundu, kod blokların
# hiçbiri düzyazı sayılmadı (kaçan 2 blok ters tırnaklı komut içeriyor).
# En az sözcük: kısa komut satırı ("pip install torch") düzyazı sayılmaz.
_PROSE_MIN_WORDS = 6
# Sözcük oranı: yalnız harften (iç kesme/tire/eğik çizgiyle bağlı) oluşan
# parçaların tüm parçalara oranı. Ölçülen düzyazı bloklarında en düşük 0,89;
# belirteç bloklarında en yüksek 0,69 (karma Türkçe + belirteç).
_PROSE_MIN_WORD_RATIO = 0.85
# Küçük harfle başlayan sözcük oranı: büyük harfli belirteç/SQL dizgisi elenir.
_PROSE_MIN_LOWER_RATIO = 0.5
# Ortalama satır uzunluğu: kısa satırlı blok (şiir, liste, tablo, kod)
# düzyazı sarması değildir. Ölçülen düzyazı bloklarında en kısa ortalama 36.
_PROSE_MIN_AVG_LINE = 30
# İç büyük harfli (CamelCase) sözcük ve alt çizgili belirteç payı: düzyazıda
# marka adı (GitHub, WebDAV) ya da alıntılanan tek belirteç
# ("QQ_SON_ADIM_SIMDI") olabilir; payları küçük kalmalı.
_PROSE_MAX_CAMEL_RATIO = 0.1
_PROSE_MAX_TOKEN_RATIO = 0.1
# Kesin kod belirtileri: ters bölü, URL, karşılaştırma/atama işleçleri, kabuk
# ve şablon karakterleri, satır başı yorum, satır sonu ';', sekme.
_PROSE_CODE_SIGNAL_RE = re.compile(
    r"\\|://|www\.|==|!=|<=|>=|->|=>|::|&&|\|\||\+\+|\+=|[=$`^~|@]"
    r"|^#|;$|\t", re.MULTILINE)
# Karartma yer tutucusu ("<gizli>", "<adı verilmeyen bir araç …>") düzyazıdır.
_PROSE_REDACTION_RE = re.compile(r"<[^\W\d_][^<>{}\\]*>")
_PROSE_EDGE_PUNCT = "“”\"'‘’«»„()[]{}.,;:!?…-–—*"
_PROSE_SENTENCE_MARK_RE = re.compile(r"[.!?…,;:](?:\s|$|[”’\"')\]}])")
_PROSE_WORD_SPLIT_RE = re.compile(r"['’\-/]")
_PROSE_CAMEL_RE = re.compile(r"[a-zçğıöşü][A-ZÇĞİÖŞÜ]")
# Dil seçeneksiz lstlisting bloğu (seçenekli blok — language, caption,
# label — bilerek kod olarak işaretlenmiştir, dokunulmaz).
_BARE_LSTLISTING_RE = re.compile(
    r"\\begin\{lstlisting\}[ \t]*\n(.*?)\n?[ \t]*\\end\{lstlisting\}", re.DOTALL)
# Alıntı metninde TeX özel karakterleri (sınıflandırıcı \ $ ^ ~ | = karakterli
# bloğu zaten düzyazı saymaz; kaçış yine de eksiksiz).
_QUOTE_TEX_ESCAPES = {
    "\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "#": r"\#", "%": r"\%",
    "&": r"\&", "_": r"\_", "$": r"\$", "^": r"\^{}", "~": r"\textasciitilde{}",
}


def _prose_word(core: str) -> bool:
    parts = [p for p in _PROSE_WORD_SPLIT_RE.split(core) if p]
    return bool(parts) and all(p.isalpha() for p in parts)


def listing_is_prose(content: str) -> bool:
    """lstlisting içeriği düzyazı mı (bkz. G3a-2)? Muhafazakâr."""
    lines = [ln.rstrip() for ln in content.split("\n")]
    nonempty = [ln for ln in lines if ln.strip()]
    if not nonempty:
        return False
    # Girinti (kod yapısı) ya da kesin kod belirtisi: düzyazı değil.
    if any(ln[:1] in (" ", "\t") for ln in nonempty):
        return False
    text = " ".join(ln.strip() for ln in nonempty)
    # Dış süslü ayraç: yeniden ifade edilmiş alıntı ("{[Heyecan] …}").
    inner = text[1:-1] if text.startswith("{") and text.endswith("}") else text
    inner = _PROSE_REDACTION_RE.sub(" ", inner)
    if any(ch in inner for ch in "{}<>"):
        return False
    if _PROSE_CODE_SIGNAL_RE.search("\n".join(ln.strip() for ln in nonempty)):
        return False
    tokens = [t for t in inner.split() if t.strip(_PROSE_EDGE_PUNCT)]
    words = [t.strip(_PROSE_EDGE_PUNCT) for t in tokens
             if _prose_word(t.strip(_PROSE_EDGE_PUNCT))]
    if len(words) < _PROSE_MIN_WORDS:
        return False
    if len(words) < _PROSE_MIN_WORD_RATIO * len(tokens):
        return False
    if sum(1 for t in tokens if "_" in t) > _PROSE_MAX_TOKEN_RATIO * len(tokens):
        return False
    if (sum(1 for w in words if _PROSE_CAMEL_RE.search(w))
            > _PROSE_MAX_CAMEL_RATIO * len(words)):
        return False
    if sum(1 for w in words if w[0].islower()) < _PROSE_MIN_LOWER_RATIO * len(words):
        return False
    if not _PROSE_SENTENCE_MARK_RE.search(inner):
        return False
    if (len(nonempty) > 1
            and sum(len(ln) for ln in nonempty) / len(nonempty) < _PROSE_MIN_AVG_LINE):
        return False
    return True


# 2026-10-05: kutu, önceki paragrafın yarım kalan cümlesini sürdürüyorsa (önceki
# metin harf ya da virgülle bitiyor, kutu küçük harfle başlıyor) kutu kalkar,
# metin paragrafın devamı olur. Önceki satırda yorum ('%') varsa ya da satır
# komutla başlıyorsa bağlanmaz (metin yoruma düşerdi / yapı satırıdır).
_PROSE_CONTINUES_RE = re.compile(r"[^\W\d_,]|,", re.UNICODE)
_UNESCAPED_PERCENT_RE = re.compile(r"(?<!\\)%")


def listing_continues_paragraph(before: str, content: str) -> bool:
    """Düzyazı kutusu önceki paragrafın yarım cümlesinin devamı mı?"""
    paras = [ln for ln in content.split("\n")]
    if any(not ln.strip() for ln in paras[1:-1]):
        return False  # birden çok paragraf: alıntı kalır
    first = content.lstrip()[:1]
    if not first or not first.islower():
        return False
    tail = before.rstrip()
    if not tail or before[len(tail):].count("\n") < 1:
        return False
    last_line = tail.rsplit("\n", 1)[-1]
    if last_line.lstrip().startswith("\\") or _UNESCAPED_PERCENT_RE.search(last_line):
        return False
    return bool(_PROSE_CONTINUES_RE.fullmatch(tail[-1]))


def _escape_quote_text(text: str) -> str:
    return "".join(_QUOTE_TEX_ESCAPES.get(ch, ch) for ch in text)


def listing_to_quote(content: str) -> str:
    """Düzyazı lstlisting içeriğini alıntı ortamına çevir (kayıpsız: satır
    sonları boşluk olur, boş satır paragraf ayırır, TeX özel karakterleri
    kaçışlanır)."""
    paras: List[str] = []
    cur: List[str] = []
    for ln in content.split("\n"):
        if ln.strip():
            cur.append(ln.strip())
        elif cur:
            paras.append(" ".join(cur))
            cur = []
    if cur:
        paras.append(" ".join(cur))
    esc = [_escape_quote_text(p) for p in paras]
    return ("\\begin{quote}\n\\raggedright\\ttfamily " + "\n\n".join(esc)
            + "\n\\end{quote}")


def _trailing_footnotetexts(text: str, pos: int) -> int:
    """*pos*'tan sonra yalnız boşlukla ayrılan ``\\footnotetext`` dizisinin
    bittiği konum (yoksa *pos*)."""
    end = pos
    while True:
        m = _FOOTNOTETEXT_OPEN_RE.match(text, end)
        if not m:
            return end
        close = _brace_group_end(text, m.end() - 1)
        if close < 0:
            return end
        end = close + 1
        c = _SETCOUNTER_FOOTNOTE_RE.match(text, end)
        if c:
            end = c.end()


# ---------------------------------------------------------------------------
# G3a-3 (2026-10-05, RAPOR MT-6): display matematiğin GÖRÜNÜR genişliği.
# ---------------------------------------------------------------------------
# B22 eskiden LaTeX kaynağının uzunluğuna bakıyordu (180 karakter). Kaynak
# uzunluğu basılı genişliği göstermez: derlemdeki 243 tek satırlık display
# denklemde em/karakter oranı 0,06-0,47 (7 kat). "\vec{\boldsymbol{r}}"
# 20 karakter, basılı tek harf. Sonuç (bir test kitabında): kaynakta tek satıra
# sığan denklem (17,8-20,5 em; satır 41,6 em) multline* ile sola ve sağa
# yaslı iki parçaya bölünüyordu; tersine 102 karakterlik ama 45 em genişlikteki
# denklem (bir İngilizce bilim kitabı) bölünmeyip taşıyordu.
# Tahmin: komut adları sayılmaz; görünür simge sayılır (em cinsinden):
# harf/rakam, işlem ve bağıntı (+ yanlarındaki matematik boşluğu), ayraç;
# kesirde pay/payda en genişi, üst/alt simge küçültülmüş ve üst üste,
# \vec \mathrm \boldsymbol gibi sarmalayıcılarda yalnız argüman. Ölçekler
# derlemde XeLaTeX ile ölçülen genişliklerle ayarlandı (iç ölçüm notu):
# tahmin/gerçek oranı 0,88-1,25, ortanca 1,04.
_MW_GLYPH = 0.52        # ortalama küçük harf / rakam
_MW_UPPER = 0.68        # büyük harf
_MW_BIN = 1.22          # ikili işlem (+ − ×) ve iki yanındaki orta boşluk
_MW_CDOT = 0.78         # \cdot: dar işlem
_MW_REL = 1.33          # bağıntı (= < ≤ →) ve iki yanındaki kalın boşluk
_MW_LONG_REL = 2.25     # \implies, \iff, \Longrightarrow + kalın boşluklar
_MW_DELIM = 0.39        # ( ) [ ] |
_MW_PUNCT = 0.45        # , ; ve ardındaki ince boşluk
_MW_DOT = 0.28          # . : ' !
_MW_BIGOP = 1.45        # \sum \prod \int (display boyutu)
_MW_FRAC_PAD = 0.24     # kesir çizgisinin iki yandaki taşması
_MW_SQRT = 0.85         # kök işareti
_MW_FUNC_GAP = 0.17     # \sin x: işlev adından sonraki ince boşluk
_MW_TEXT_CHAR = 0.48    # \text{...} içindeki karakter
_MW_SYMBOL = 0.6        # tanınmayan simge komutu (Yunan harfi vb.)
_MW_TAG_GAP = 1.0       # \tag ile denklem arasındaki en az boşluk
_MW_MATRIX_COLSEP = 0.9  # matris/array sütun arası
_MW_SCRIPT = 0.72       # üst/alt simge ölçeği
_MW_SCRIPT2 = 0.55      # iç içe simge ölçeği (scriptscript)
_MW_SPACE = {",": 0.17, ":": 0.22, ">": 0.22, ";": 0.28, "!": -0.17, " ": 0.33,
             "quad": 1.0, "qquad": 2.0, "enspace": 0.5, "thinspace": 0.17,
             "medspace": 0.22, "thickspace": 0.28, "negthinspace": -0.17}
_MW_ZERO = frozenset({
    "left", "right", "big", "Big", "bigg", "Bigg", "bigl", "bigr", "Bigl", "Bigr",
    "biggl", "biggr", "Biggl", "Biggr", "bigm", "Bigm", "middle", "displaystyle",
    "textstyle", "scriptstyle", "scriptscriptstyle", "limits", "nolimits",
    "nonumber", "notag", "mathstrut", "strut", "allowbreak", "relax", "nobreak",
    "boldmath", "unboldmath"})
_MW_SKIP_ARG = frozenset({"label", "phantom", "vphantom", "hphantom", "color",
                          "hspace", "kern", "mkern", "mspace", "hskip"})
_MW_ARG_ONLY = frozenset({
    "mathrm", "mathbf", "mathit", "mathsf", "mathtt", "mathcal", "mathbb",
    "mathfrak", "mathscr", "boldsymbol", "bm", "symbf", "symrm", "symit",
    "symcal", "symbb", "pmb", "vec", "hat", "bar", "tilde", "dot", "ddot",
    "dddot", "overline", "underline", "widehat", "widetilde", "overrightarrow",
    "overleftarrow", "mathring", "check", "breve", "acute", "grave",
    "operatorname", "mathop", "mathbin", "mathrel", "mathord", "mathopen",
    "mathclose", "mathpunct", "overbrace", "underbrace", "boxed", "cancel",
    "ensuremath"})
_MW_TEXT_ARG = frozenset({"text", "textrm", "textit", "textbf", "textsf",
                          "texttt", "mbox", "textnormal", "textup", "emph"})
_MW_BIN_CS = frozenset({
    "times", "pm", "mp", "cup", "cap", "circ", "wedge", "vee", "oplus",
    "otimes", "ominus", "odot", "ast", "star", "bullet", "div", "setminus",
    "land", "lor", "sqcup", "sqcap", "uplus", "amalg"})
_MW_REL_CS = frozenset({
    "leq", "le", "geq", "ge", "neq", "ne", "approx", "equiv", "sim", "simeq",
    "cong", "propto", "in", "notin", "ni", "subset", "subseteq", "supset",
    "supseteq", "to", "rightarrow", "leftarrow", "mapsto", "Rightarrow",
    "Leftarrow", "Leftrightarrow", "leftrightarrow", "ll", "gg", "perp",
    "parallel", "mid", "leqslant", "geqslant", "prec", "succ", "preceq",
    "succeq", "doteq", "asymp", "vdash", "dashv", "models", "coloneqq",
    "eqqcolon", "triangleq", "lesssim", "gtrsim", "nleq", "ngeq", "uparrow",
    "downarrow", "nearrow", "searrow", "gets", "colon"})
_MW_LONG_REL_CS = frozenset({
    "implies", "impliedby", "iff", "Longrightarrow", "Longleftarrow",
    "Longleftrightarrow", "longrightarrow", "longleftarrow",
    "longleftrightarrow", "longmapsto", "xrightarrow", "xleftarrow"})
_MW_BIGOP_CS = frozenset({"sum", "prod", "coprod", "bigcup", "bigcap",
                          "bigoplus", "bigotimes", "bigvee", "bigwedge",
                          "bigsqcup"})
_MW_INTOP_CS = frozenset({"int", "iint", "iiint", "oint"})
_MW_LIMOP_CS = frozenset({"lim", "max", "min", "sup", "inf", "limsup",
                          "liminf", "det", "Pr", "gcd", "argmax", "argmin"})
_MW_FUNC_CS = frozenset({
    "sin", "cos", "tan", "cot", "sec", "csc", "arcsin", "arccos", "arctan",
    "sinh", "cosh", "tanh", "coth", "log", "ln", "lg", "exp", "arg", "deg",
    "dim", "ker", "hom", "Re", "Im", "sgn", "tr", "mod", "bmod", "pmod"})
_MW_SYMBOL_CS = {
    "infty": 0.85, "nabla": 0.8, "partial": 0.56, "ldots": 1.2, "cdots": 1.2,
    "dots": 1.2, "dotsc": 1.2, "dotsb": 1.2, "vdots": 0.4, "ddots": 1.0,
    "prime": 0.3, "ell": 0.42, "hbar": 0.55, "forall": 0.56, "exists": 0.56,
    "emptyset": 0.5, "angle": 0.7, "triangle": 0.78, "square": 0.7,
    "langle": 0.39, "rangle": 0.39, "lvert": 0.28, "rvert": 0.28,
    "vert": 0.28, "lVert": 0.5, "rVert": 0.5, "Vert": 0.5, "lfloor": 0.45,
    "rfloor": 0.45, "lceil": 0.45, "rceil": 0.45, "backslash": 0.5}
_MW_SYMBOL_ESC = {"|": 0.5, "{": 0.5, "}": 0.5, "%": 0.83, "$": 0.5,
                  "&": 0.78, "#": 0.83, "_": 0.5}
_MW_FRAC_CS = frozenset({"frac", "dfrac", "cfrac", "tfrac", "binom", "dbinom",
                         "tbinom"})
_MW_OVERUNDER_CS = frozenset({"overset", "underset", "stackrel"})
_MW_MATRIX_ENVS = frozenset({"matrix", "pmatrix", "bmatrix", "Bmatrix",
                             "vmatrix", "Vmatrix", "smallmatrix", "array",
                             "cases", "aligned", "gathered", "split",
                             "alignedat"})
_MW_REL_CHARS = frozenset("=<>≤≥≠≈≡∼→←⇒⇐⇔↦∈∉⊂⊆⊃⊇∝≪≫")
_MW_BIN_CHARS = frozenset("+-−×·±∓∪∩∘⊕⊗÷*")
_MW_TOKEN_RE = re.compile(r"\\([A-Za-z]+)\*?|\\(.)|(\s+)|(.)", re.DOTALL)
# Tahmin edilemeyen (aşırı iç içe) girdide eski kaynak-uzunluğu yaklaşımı:
# derlemde ortalama 0,19 em / kaynak karakteri.
_MW_FALLBACK_PER_CHAR = 0.19


class _MathWidth:
    """Belirteç listesi üzerinde özyinelemeli genişlik tahmini (em)."""

    def __init__(self, toks: List[Tuple[str, str]]):
        self.t = toks
        self.i = 0

    def peek(self) -> Optional[Tuple[str, str]]:
        return self.t[self.i] if self.i < len(self.t) else None

    def take(self) -> Optional[Tuple[str, str]]:
        tok = self.peek()
        self.i += 1
        return tok

    def _until_close(self, open_tok, close_tok) -> List[Tuple[str, str]]:
        depth, start = 1, self.i
        while self.i < len(self.t):
            k = self.t[self.i]
            if k == open_tok:
                depth += 1
            elif k == close_tok:
                depth -= 1
                if depth == 0:
                    inner = self.t[start:self.i]
                    self.i += 1
                    return inner
            self.i += 1
        return self.t[start:]

    def arg(self) -> List[Tuple[str, str]]:
        """Sonraki argüman: {…} grubunun ya da tek belirtecin belirteçleri."""
        tok = self.take()
        if tok is None:
            return []
        if tok == ("ch", "{"):
            return self._until_close(("ch", "{"), ("ch", "}"))
        return [tok]

    def opt(self) -> Optional[List[Tuple[str, str]]]:
        if self.peek() == ("ch", "["):
            self.i += 1
            return self._until_close(("ch", "["), ("ch", "]"))
        return None

    def env_body(self) -> List[Tuple[str, str]]:
        return self._until_close(("cs", "begin"), ("cs", "end"))


def _mw_tokens(math: str) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for m in _MW_TOKEN_RE.finditer(math):
        if m.group(1) is not None:
            out.append(("cs", m.group(1)))
        elif m.group(2) is not None:
            out.append(("sym", m.group(2)))
        elif m.group(4) is not None:
            out.append(("ch", m.group(4)))
        # boşluk (group 3) matematikte dizgiye girmez
    return out


def _mw_list(toks: List[Tuple[str, str]], scale: float) -> float:
    return _mw_seq(_MathWidth(toks), scale)


def _mw_rows(name: str, toks: List[Tuple[str, str]], scale: float) -> float:
    """Matris benzeri ortam / \\substack: sütun genişliği en geniş hücre."""
    rows: List[List[List[Tuple[str, str]]]] = []
    row: List[List[Tuple[str, str]]] = []
    cell: List[Tuple[str, str]] = []
    depth = 0
    for tok in toks:
        if tok in (("ch", "{"), ("cs", "begin")):
            depth += 1
        elif tok in (("ch", "}"), ("cs", "end")):
            depth -= 1
        if depth == 0 and tok == ("sym", "\\"):
            row.append(cell)
            rows.append(row)
            row, cell = [], []
            continue
        if depth == 0 and tok == ("ch", "&"):
            row.append(cell)
            cell = []
            continue
        cell.append(tok)
    row.append(cell)
    rows.append(row)
    ncol = max(len(r) for r in rows)
    cols = [0.0] * ncol
    for r in rows:
        for j, c in enumerate(r):
            cols[j] = max(cols[j], _mw_list(c, scale))
    sep = 0.0 if name in ("aligned", "split", "alignedat", "substack") \
        else _MW_MATRIX_COLSEP * scale
    width = sum(cols) + sep * (ncol - 1)
    if name in ("pmatrix", "bmatrix", "Bmatrix", "vmatrix", "Vmatrix"):
        width += 2 * _MW_DELIM * scale
    elif name == "cases":
        width += 2 * _MW_MATRIX_COLSEP * scale  # sol ayraç + sütun arası
    return width


def _mw_scripts(p: _MathWidth, scale: float) -> float:
    """Tabandan sonraki ^/_ simgeleri: alt ve üst üst üste, en genişi."""
    sup = sub = 0.0
    inner = _MW_SCRIPT if scale >= 1.0 else _MW_SCRIPT2 / _MW_SCRIPT
    while p.peek() in (("ch", "^"), ("ch", "_"), ("cs", "limits"),
                       ("cs", "nolimits")):
        tok = p.take()
        if tok[0] == "cs":
            continue
        width = _mw_list(p.arg(), scale * inner)
        if tok[1] == "^":
            sup = max(sup, width)
        else:
            sub = max(sub, width)
    return max(sup, sub)


def _mw_char(val: str, scale: float) -> float:
    if val == "~":
        return _MW_SPACE[" "] * scale
    if val in _MW_REL_CHARS:
        return _MW_REL * scale
    if val in _MW_BIN_CHARS:
        return _MW_BIN * scale
    if val in "()[]|":
        return _MW_DELIM * scale
    if val == "/":
        return _MW_GLYPH * scale
    if val in ",;":
        return _MW_PUNCT * scale
    if val in ".:'!?":
        return _MW_DOT * scale
    return (_MW_UPPER if val.isupper() else _MW_GLYPH) * scale


def _mw_cs(p: _MathWidth, val: str, scale: float) -> Tuple[float, bool]:
    """Kontrol sözcüğünün genişliği; ikinci değer: sınırlar alta/üste mi."""
    if val in _MW_SPACE:
        return _MW_SPACE[val] * scale, False
    if val in _MW_ZERO:
        return 0.0, False
    if val in _MW_SKIP_ARG:
        p.arg()
        return 0.0, False
    if val == "tag":
        return _mw_list(p.arg(), scale) + _MW_TAG_GAP * scale, False
    if val in _MW_ARG_ONLY:
        return _mw_list(p.arg(), scale), False
    if val in _MW_TEXT_ARG:
        txt = p.arg()
        chars = sum(len(v) if k == "cs" else 1 for k, v in txt
                    if (k, v) not in (("ch", "{"), ("ch", "}")))
        return chars * _MW_TEXT_CHAR * scale, False
    if val in _MW_FRAC_CS:
        part = scale * (_MW_SCRIPT if val.startswith("t") else 1.0)
        width = max(_mw_list(p.arg(), part), _mw_list(p.arg(), part))
        width += _MW_FRAC_PAD * scale
        if "binom" in val:
            width += 2 * _MW_DELIM * scale
        return width, False
    if val == "sqrt":
        index = p.opt()
        width = _mw_list(p.arg(), scale) + _MW_SQRT * scale
        if index:
            width += _mw_list(index, scale * _MW_SCRIPT2) / 2
        return width, False
    if val in _MW_OVERUNDER_CS:
        top = _mw_list(p.arg(), scale * _MW_SCRIPT)
        return max(top, _mw_list(p.arg(), scale)), False
    if val == "substack":
        return _mw_rows("substack", p.arg(), scale), False
    if val == "begin":
        name = "".join(v for _, v in p.arg())
        if name == "array":
            p.arg()  # sütun belirtimi
        body = p.env_body()
        p.arg()  # \end{ad}
        return _mw_rows(name if name in _MW_MATRIX_ENVS else "matrix",
                        body, scale), False
    if val == "end":
        p.arg()
        return 0.0, False
    if val == "cdot":
        return _MW_CDOT * scale, False
    if val in _MW_BIN_CS:
        return _MW_BIN * scale, False
    if val in _MW_REL_CS:
        return _MW_REL * scale, False
    if val in _MW_LONG_REL_CS:
        width = _MW_LONG_REL * scale
        if val.startswith("x"):
            p.opt()
            width = max(width, _mw_list(p.arg(), scale * _MW_SCRIPT)
                        + _MW_REL * scale)
        return width, False
    if val in _MW_BIGOP_CS:
        return _MW_BIGOP * scale, True
    if val in _MW_INTOP_CS:
        return _MW_BIGOP * scale, False
    if val in _MW_LIMOP_CS or val in _MW_FUNC_CS:
        return ((len(val) * _MW_GLYPH + _MW_FUNC_GAP) * scale,
                val in _MW_LIMOP_CS)
    if val in _MW_SYMBOL_CS:
        return _MW_SYMBOL_CS[val] * scale, False
    return _MW_SYMBOL * scale, False


def _mw_seq(p: _MathWidth, scale: float) -> float:
    total = 0.0
    while True:
        tok = p.take()
        if tok is None:
            return total
        kind, val = tok
        limits = False
        if kind == "ch":
            if val == "{":
                p.i -= 1
                base = _mw_list(p.arg(), scale)
            elif val in "}&":
                continue
            elif val in "^_":
                p.i -= 1
                total += _mw_scripts(p, scale)
                continue
            else:
                base = _mw_char(val, scale)
        elif kind == "sym":
            if val == "\\":
                continue
            base = (_MW_SPACE[val] if val in _MW_SPACE
                    else _MW_SYMBOL_ESC.get(val, _MW_GLYPH)) * scale
        else:
            base, limits = _mw_cs(p, val, scale)
        if p.peek() in (("ch", "^"), ("ch", "_")):
            scripts = _mw_scripts(p, scale)
            # Display'de \sum, \lim sınırları alta/üste: genişlik en genişi.
            total += (max(base, scripts) if limits and scale >= 1.0
                      else base + scripts)
        else:
            total += base


def display_math_width_em(math: str) -> float:
    """Display matematiğin basılı genişliğinin tahmini (em; bkz. G3a-3)."""
    try:
        return _mw_list(_mw_tokens(math), 1.0)
    except RecursionError:
        return len(math) * _MW_FALLBACK_PER_CHAR


def _greedy_line_count(widths: List[float], limit: float) -> int:
    lines, cur = 1, 0.0
    for w in widths:
        if cur and cur + w > limit:
            lines += 1
            cur = w
        else:
            cur += w
    return lines


# Dengeli bölmede ikili aramanın adım sayısı (2^-30 göreli kesinlik yeter).
_CHUNK_SEARCH_STEPS = 30


def _balanced_chunks(math: str, cuts: List[int],
                     limit: float) -> Optional[List[str]]:
    """*math*'i *cuts* konumlarından en az satıra, eşit genişlikte böl.

    Satır sayısı *limit* (em) ile açgözlü bölmenin verdiği en az sayıdır;
    o sayıyı aşmayan en küçük satır genişliği ikili aramayla bulunur (son
    satırın tek başına "= 0" kalmaması için)."""
    bounds = [0] + cuts + [len(math)]
    segs = [math[a:b] for a, b in zip(bounds, bounds[1:])]
    widths = [display_math_width_em(seg) for seg in segs]
    lines = _greedy_line_count(widths, limit)
    if lines < 2:
        return None
    lo = max(max(widths), sum(widths) / lines)
    hi = max(lo, limit)
    for _ in range(_CHUNK_SEARCH_STEPS):
        mid = (lo + hi) / 2
        if _greedy_line_count(widths, mid) <= lines:
            hi = mid
        else:
            lo = mid
    chunks: List[str] = []
    cur, cur_w = "", 0.0
    for seg, w in zip(segs, widths):
        if cur and cur_w + w > hi:
            chunks.append(cur.strip())
            cur, cur_w = seg, w
        else:
            cur += seg
            cur_w += w
    chunks.append(cur.strip())
    return [c for c in chunks if c]


# Satır genişliği (em) ve bölme eşiği.
# Tahmin gerçeğin en çok %12 altında kaldı (oran ≥ 0,88): tahmin
# 0,88 × satır genişliğini aşmıyorsa denklem gerçekten sığar. Eşiği aşan
# denklem bölünmeye adaydır (sığabilecek az sayıda denklem de bölünebilir;
# taşan denklem bölünmeden kalmaz).
_MATH_WIDTH_SAFETY = 0.88
# Satır genişliği bilinmezken (Phase 5.5: yalnız gövde, önsöz yok) varsayılan:
# derlemdeki EN GENİŞ satır (472 pt / 10 pt = 47,2 em). Bu evre yalnız her
# düzende taşacak denklemi böler; gerçek düzen Phase 6'da önsözden okunur ve
# o düzende taşan denklem orada bölünür (bölünmüş denklem geri birleştirilemez,
# bu yüzden gövde evresi düzenden bağımsız en cömert sınırı kullanır).
_MATH_LINE_EM_UNKNOWN = 47.0
# Gövde evresinde bölünmesi GEREKEN (en geniş düzende bile taşan) denklemin
# satırları ise en DAR düzene göre kurulur: bölünmüş denklem Phase 6'da gerçek
# düzene göre yeniden bölünemez. Derlemdeki en dar satır bir test kitabında
# (347,98 pt / 11 pt = 31,6 em; bir başkasında 317,8 pt / 10 pt = 31,8 em).
_MATH_LINE_EM_NARROW = 31.6
# Önsözden satır genişliği: belge sınıfı punto seçeneği + kâğıt + geometry.
_PT_PER_UNIT = {"pt": 1.0, "mm": 72.27 / 25.4, "cm": 72.27 / 2.54,
                "in": 72.27, "bp": 72.27 / 72.0, "pc": 12.0}
_PAPER_WIDTH_PT = {
    "a4paper": 210 * 72.27 / 25.4, "a5paper": 148 * 72.27 / 25.4,
    "b5paper": 176 * 72.27 / 25.4, "letterpaper": 8.5 * 72.27,
    "legalpaper": 8.5 * 72.27, "executivepaper": 7.25 * 72.27}
_DOCCLASS_OPTIONS_RE = re.compile(r"\\documentclass\s*\[([^\]]*)\]")
_GEOMETRY_ARGS_RE = re.compile(
    r"\\(?:geometry|usepackage\s*\[((?:[^\[\]]|\[[^\]]*\])*)\]\s*\{geometry\})"
    r"(?:\s*\{((?:[^{}]|\{[^{}]*\})*)\})?")
_LENGTH_RE = re.compile(r"^\s*(\d+(?:\.\d+)?|\.\d+)\s*(pt|mm|cm|in|bp|pc)\s*$")
# geometry varsayılanı (yalnız paket yüklü, seçenek yok): metin genişliği
# kâğıdın 0,7'si (geometry belgesi, "hscale").
_GEOMETRY_DEFAULT_HSCALE = 0.7
# Belge sınıfı punto seçeneği yoksa standart sınıfların varsayılanı.
_DEFAULT_FONT_PT = 10.0


def _length_pt(value: str) -> Optional[float]:
    m = _LENGTH_RE.match(value)
    return float(m.group(1)) * _PT_PER_UNIT[m.group(2)] if m else None


def math_line_capacity_em(preamble: str) -> Optional[float]:
    """Önsözden metin satırının em cinsinden genişliği; okunamazsa ``None``.

    Yalnız şablonun ürettiği biçimler okunur: ``\\documentclass[...,11pt,
    a4paper]``, ``\\geometry{margin=..}`` ya da ``left/right/paperwidth/
    textwidth/width``. Son ``\\geometry`` geçerlidir (preamble_ozel.tex
    şablondan sonra gelir)."""
    m = _DOCCLASS_OPTIONS_RE.search(preamble)
    options = [o.strip() for o in (m.group(1).split(",") if m else [])]
    font_pt = _DEFAULT_FONT_PT
    paper = None
    for opt in options:
        fm = re.fullmatch(r"(\d+(?:\.\d+)?)pt", opt)
        if fm:
            font_pt = float(fm.group(1))
        elif opt in _PAPER_WIDTH_PT:
            paper = _PAPER_WIDTH_PT[opt]
    keys: Dict[str, str] = {}
    geometry_seen = False
    for g in _GEOMETRY_ARGS_RE.finditer(preamble):
        geometry_seen = True
        for part in _split_keyval(",".join(x for x in g.groups() if x)):
            if "=" in part:
                k, v = part.split("=", 1)
                keys[k.strip()] = v.strip()
            elif part.strip() in _PAPER_WIDTH_PT:
                paper = _PAPER_WIDTH_PT[part.strip()]
    if "paperwidth" in keys:
        paper = _length_pt(keys["paperwidth"]) or paper
    width = None
    for k in ("textwidth", "width"):
        if k in keys:
            width = _length_pt(keys[k])
    if width is None and paper is not None:
        margin = _length_pt(keys.get("margin", "") or keys.get("hmargin", ""))
        left = _length_pt(keys.get("left", "") or keys.get("lmargin", "")
                          or keys.get("inner", ""))
        right = _length_pt(keys.get("right", "") or keys.get("rmargin", "")
                           or keys.get("outer", ""))
        left = left if left is not None else margin
        right = right if right is not None else margin
        if left is not None and right is not None:
            width = paper - left - right
        elif left is not None or right is not None:
            width = paper - 2 * (left if left is not None else right)
        elif geometry_seen:
            width = paper * _GEOMETRY_DEFAULT_HSCALE
    if width is None or width <= 0:
        return None
    if "twocolumn" in options:
        width /= 2
    return width / font_pt


# ---------------------------------------------------------------------------
# Main sanitizer class
# ---------------------------------------------------------------------------

class LatexSanitizer:
    """Pre-translation LaTeX body sanitizer.

    Parameters
    ----------
    source_lang : str
        Source language code (default ``"en"``).
    target_lang : str
        Target language code (default ``"tr"``).
    """

    # Sınıf düzeyi varsayılanlar: __init__'siz kurulan örnekler (testler
    # LatexSanitizer.__new__ ile tek geçişi çağırır) da çalışır.
    source_lang_known: bool = False
    source_format: Optional[str] = None
    image_dirs: Tuple[Path, ...] = ()
    _meta: Dict[str, str] = {}
    # G3a-3: sanitize() çağrısı başına metin satırının em genişliği (yalnız
    # tam belgede önsözden okunur; None = bilinmiyor).
    _math_line_em: Optional[float] = None

    def __init__(self, source_lang: Optional[str] = None,
                 target_lang: str = "tr",
                 manual_translation: bool = False,
                 user_defined_envs=None,
                 source_format: Optional[str] = None,
                 image_dirs: Optional[Iterable] = None):
        # source_lang verilmezse eski varsayım ("en") sürer; YALNIZ açıkça
        # verilen dil "bilinen" sayılır (F2-05: İngilizce kaynağa özgü dizgi
        # kuralı tahminle uygulanmaz — Yol A'da kaynak dil ölçülmez).
        self.source_lang = source_lang or "en"
        self.source_lang_known = source_lang is not None
        self.target_lang = target_lang
        # F1-06 (2026-09-29): kaynak biçimi ("pdf" / "epub"). Verilmezse
        # gövdedeki "% KITAPCEVIR-META" başlığının source_format alanı okunur.
        self.source_format = (source_format or "").strip().lower() or None
        # F2-10 (2026-09-29): \includegraphics adlarının çözüleceği dizinler
        # (rozet doğrulaması görselin piksel ölçüsüne bakar). Verilmezse
        # META başlığındaki output_dir'in source/images ve source dizinleri.
        self.image_dirs: Tuple[Path, ...] = tuple(
            Path(d) for d in (image_dirs or ()))
        # sanitize() çağrısı başına META alanları (yalnız okunur).
        self._meta: Dict[str, str] = {}
        # F2 (2026-07-28): preamble_ozel.tex gibi gövde DIŞINDA tanımlanan
        # ortam adları — A-9 guard'ının gövde taraması bunları göremez.
        self.user_defined_envs: frozenset = frozenset(user_defined_envs or ())
        # 2026-07-11 (#2): Yol A (ajan/manuel ceviri) modunda MT-artefakti
        # odakli agresif dil fix'leri atlanir — ajan ciktisi otoritedir;
        # 'and/or/to' degisimleri ve yabanci-kelime/yapisik-kelime kurallari
        # ajanin BILINCLI biraktigi Ingilizceyi (alinti, kaynakca) bozuyordu.
        # Gercek cevrilmemis kalinti QC raporunda gorunur, sessizce ezilmez.
        self.manual_translation = manual_translation

    def sanitize(self, latex_body: str) -> Tuple[str, SanitizeReport]:
        """Main entry point.  Returns ``(cleaned_body, report)``.

        All cleaning steps respect math environments and protected commands
        -- their content is stashed before cleaning and restored after.

        Tam belge verilirse (Phase 6: şablona sarılmış kitap) preamble LaTeX
        KODUDUR, metin değil: gövde kuralları yalnız ``\\begin{document}``
        sonrasına uygulanır, preamble'a sadece güvenlik filtresi
        (``preamble_ozel.tex`` için). Eskiden ``_escape_stray_percent``
        şablonun satır sonu yorumlarını (``{%``) ``\\%`` yapıyor, 17 Tem
        2026'dan beri her PDF'nin ilk sayfasına ``% % % %`` basılıyordu.
        Tam belge = ``\\documentclass`` ile başlayan metin; yalnız gövdede
        (Phase 5.5) ayrım yapılmaz (bkz. ``_FULL_DOCUMENT_START_RE``).
        """
        if not latex_body or not latex_body.strip():
            return latex_body or "", SanitizeReport()

        report = SanitizeReport()
        self._meta = _meta_fields(latex_body)
        begin = (_DOCUMENT_BEGIN_RE.search(latex_body)
                 if _FULL_DOCUMENT_START_RE.match(latex_body) else None)
        self._math_line_em = (math_line_capacity_em(latex_body[:begin.start()])
                              if begin else None)
        if begin:
            preamble = self._filter_dangerous_commands(
                latex_body[:begin.start()], report)
            body = preamble + self._sanitize_text(
                latex_body[begin.start():], report, full_document=True)
            # Yalnız tam belgede (şablon aşaması): Faz 5.5 gövdesi QC'de
            # kaynakla karşılaştırılır, kaynakta olmayan \allowbreak{} oraya
            # girmez.
            body = uzun_belirtec_kirilimi(body)
        else:
            body = self._sanitize_text(latex_body, report)

        report._recount()
        logger.info("Sanitization complete: %s", report)
        return body, report

    def _sanitize_text(self, latex_body: str, report: SanitizeReport,
                       full_document: bool = False) -> str:
        """Gövde metnine tüm temizlik geçişlerini uygula (sıra önemli).

        *full_document*: metin şablona sarılmış tam belgenin gövdesidir
        (Phase 6, derlemeden hemen önce). Dizgi geçişleri (G1 görsel
        yüksekliği sınırı) yalnız bu durumda uygulanır.
        """
        # --- Phase 0: Clean invisible characters BEFORE stashing ---
        # Zero-width chars can break stash regex matching (e.g. \be\u200bgin{eq})
        latex_body = self._clean_invisible_chars(latex_body, report)

        # --- Phase 0.5: Marker artifact: \textit{X}word \u2192 $X_{\mathrm{word}}$
        # Marker often outputs subscripted variables like e_{free} as
        # ``\textit{e}free``. The trailing word stays in plain text where
        # glossary apply corrupts it (e.g. "free"\u2192"kullan\u0131ma a\u00e7\u0131k"). By
        # converting to proper math, we fix rendering AND get math protection.
        latex_body = self._fix_textit_subscripts(latex_body, report)

        # --- Phase 0.6: \bar{X} \u2192 \overline{X} for STIX Two Math compat
        # STIX Two Math + unicode-math renders \bar accent on italic vars
        # in a way that visually resembles \u2260. \overline is more robust.
        latex_body = self._bar_to_overline(latex_body, report)

        # Stash protected zones so we never touch math / refs.
        body, stash = _stash_protected(latex_body)
        logger.debug("Stashed %d protected zones for sanitization.", len(stash))

        # Apply cleaning passes (order matters).
        # --- Pre-translation cleanup ---
        body = self._clean_epub_artifacts(body, report)    # EPUB placeholders
        body = self._remove_publisher_artifacts(body, report)
        body = self._remove_check_updates_logo(body, report)
        body = self._fix_double_backslash_artifacts(body, report)
        body = self._fix_unbalanced_inline_math_parens(body, report)
        body = self._escape_stray_percent(body, report)
        body = self._fix_text_mode_wraps(body, report)   # Yunan + G3 (≫ ≪ ≤ ≥ → ⇔)
        body = self._clean_headers(body, report)
        body = self._fix_broken_hrefs(body, report)
        body = self._fix_prompt_leaks(body, report)

        # --- Post-translation artifact cleanup ---
        body = self._clean_html_artifacts(body, report)     # NEW: HTML tags
        # A-4 (2026-07-27): CJK temizliği YALNIZ Yol B'de — HY-MT'nin CJK
        # halüsinasyonu artefakttır; manuel çeviride Çince/Japonca/Korece
        # alıntı çevirmenin bilinçli içeriğidir, silinmez.
        if not self.manual_translation:
            body = self._clean_cjk_artifacts(body, report)
        body = self._clean_batch_comments(body, report)
        body = self._fix_translated_commands(body, report)   # NEW: LaTeX cmd fix
        body = self._clean_markdown_link_remnants(body, report)  # FIX 1: md links
        body = self._clean_float_placeholder_remnants(body, report)  # float [H] cleanup
        body = self._fix_translated_env_names(body, report)      # FIX 2: env names
        if not self.manual_translation:
            # Yol B (MT) artefakt fix'leri — Yol A'da atlanir (#2, 2026-07-11)
            body = self._clean_foreign_word_leaks(body, report)   # FIX 3: foreign word leaks
            body = self._fix_stuck_words(body, report)            # FIX 4: stuck words
        body = self._fix_translated_latex_commands(body, report)  # FIX 5: translated LaTeX commands
        if not self.manual_translation:
            body = self._fix_english_leaks(body, report)          # FIX 6: English word leaks
            body = self._fix_text_mode_superscript(body, report)  # F1 MT-10: metinde '^{1}'
        body = self._remove_stray_toc(body, report)               # FIX 7: stray \tableofcontents
        body = self._fix_marker_chapter_artifacts(body, report)   # P1: spurious + title chapters
        body = self._strip_section_number_prefix(body, report)    # P2: section number prefix
        # A-8 (2026-07-27): tablo terim çevirisi YALNIZ Yol B'de — birleşik
        # özel adları eziyordu (ör. "Mercury-X 6" -> "Merkür-X 6"); manuel
        # çeviride tablo hücresi çevirmenin tercihidir.
        if not self.manual_translation:
            body = self._translate_table_terms(body, report)     # T5: tablo gök cismi adları

        # A-5 (2026-07-27): EN->TR etiket normalizasyonu YALNIZ Yol B'de —
        # bilinçli İngilizce alıntıların içini bozuyordu ("The proof" ->
        # "The İspat"); manuel çeviride etiketleri çevirmen yönetir.
        if (self.source_lang == "en" and self.target_lang == "tr"
                and not self.manual_translation):
            body = self._normalize_labels_en_to_tr(body, report)

        # --- Security ---
        body = self._filter_dangerous_commands(body, report)  # NEW: security
        # Stash'teki matematik bölgeleri de ($...$, $$...$$, \[...\],
        # \ensuremath, math ortamları) filtreden geçer: '$$\input{/etc/passwd}$$'
        # ve '\ensuremath{...}' stash'e alınınca filtreyi atlıyordu (denetim
        # 2026-09-24, M1#5 düzeltmesinin yan etkisi). Kod bölgeleri
        # (verbatim/lstlisting/minted, \verb) harfiyen dizilir: dokunulmaz.
        stash = [zone if _is_code_zone(zone)
                 else self._filter_math_zone(zone, report)
                 for zone in stash]
        # G3a-6 (mt okuma MT-22): \substack koşulları alt alta (kod bölgesi
        # hariç; \(...\) genel stash'te olmadığı için gövdeye de uygulanır).
        stash, body = self._fix_substack_rows(stash, body, report)
        # G3a-5 (mt okuma MT-21): "$x$ ’nin" -> "$x$’nin" (yalnız boşluk).
        body, n_suffix = join_suffix_after_formula(body, stash)
        if n_suffix:
            report.details.append(
                f"Space before apostrophe suffix removed ({n_suffix}x)")

        # --- B7: Math balance preflight (WARN-only, autofix YOK) ---
        # Stash ÖNCESİ restore yok — legitimate math zaten __SANSTASH_N__
        # placeholder'larda saklı, geriye kalan $'lar "rogue" demektir.
        math_issues = self._check_math_balance(body)
        if math_issues:
            logger.warning(
                "B7 math balance: %d paragraph(s) with unclosed $. First: %s",
                len(math_issues),
                math_issues[0]["context"][:100],
            )
            for i, issue in enumerate(math_issues[:5]):
                logger.warning(
                    "  [%d] line~%d: %s",
                    i + 1,
                    issue["line_start"],
                    issue["context"][:120],
                )
            if len(math_issues) > 5:
                logger.warning("  ... ve %d tane daha", len(math_issues) - 5)
            # Feature flag: autofix_math_balance (default False) — kapalı.
            if getattr(self, "autofix_math_balance", False):
                logger.warning(
                    "B7 autofix_math_balance aktif ama uygulanmadı "
                    "(autofix devre dışı, sadece raporlama)."
                )

        # --- G1: görsel yüksekliği sınırı (2026-09-25) ---
        # Stash açılmadan: verbatim/lstlisting/\verb içindeki örnek
        # \includegraphics satırları harfiyen kalır. YALNIZ tam belgede
        # (Phase 6): Phase 5.5'te gövdeye eklenseydi '\textheight', QC'nin
        # broken_command_braces bulgusu veren her koşuda autofix'in '\text'
        # onarımıyla '\text{height}' olur, derleme bozulurdu (autofix
        # 'textheight'i meşru komut saymıyor; 2026-09-25'te denendi; QC ve
        # autofix Phase 5.5'teki gövdeyle çalışır). Phase 6 her iki yolda
        # (Yol A 2_derle, Yol B translate_book) derlemeden hemen önce koşar
        # ve eski EXPORT_TR.tex'ler de buradan geçer.
        if full_document:
            body = self._cap_figure_heights(body, report)
            # F2-05: matematikte liste virgülü (yalnız dizgi; Phase 6).
            stash = self._space_math_list_commas(stash, report)
            # E (2026-10-06, mt okuma MT-24): kutu ortamı dengesi (kod
            # bölgeleri yer tutucudayken: kitaptaki LaTeX örneği sayılmaz).
            body = self._balance_box_env(body, report)

        # Restore protected zones.
        body = _restore_stash(body, stash)

        # T7: Bozuk footnote / HTML sup-sub (restore sonrası, math fragment görünür)
        body = self._clean_sup_sub_markup(body, report)

        # --- B21: TOC tabular sütun genişliği (compile öncesi son adım) ---
        body = self._fix_toc_tabular_widths(body)
        # --- V1 denetimi (2026-09-29): sayfadan taşan tablo (yalnız tam
        # belge; QC/EPUB Phase 5.5 gövdesini okur). Uzun metinli sütunlar
        # kırılan p{} sütunu olur; eskiden 'l' sütunu sayfa kenarından taşıp
        # hücre metni kesiliyordu (EPUB anlatı tabloları, F1-01).
        if full_document:
            body = self._fit_wide_tables(body, report)
            # G3a-4 (iç onarım notu): hedefi olmayan iç bağlantı kalkar,
            # metni kalır; hedef \label ise \hyperref (yalnız tam belge:
            # QC'nin \href sayımı Phase 5.5 gövdesini okur).
            body = self._resolve_internal_links(body, report)

        # --- B24: Multi-line equation* → align* (2026-05-09) ---
        # equation* is single-line; multi-line content gets shrunk to
        # microscopic size by B22 resizebox. Detect multi-line equation*
        # blocks and convert them to align*.
        body = self._multiline_equation_to_align(body, report)

        # --- B22: Overfull display-math wrap (2026-05-09) ---
        # Long single-line equations from converter overflow page margin.
        # Wrap them in \resizebox{\linewidth}{!}{$\displaystyle ...$} so they
        # always fit. Short equations are left untouched.
        body = self._wrap_overfull_displaymath(body, report)

        # --- B23: Figure float placement (2026-05-09) ---
        # ``\begin{figure}[H]`` (from float package) forces "here exactly"
        # placement which bunches captions into the surrounding paragraph
        # text. Relax to ``[!htb]`` so LaTeX picks a sensible spot.
        body = self._relax_figure_placement(body, report)
        # R13: [H] kalan float + dış altyazı tek kutuda (yalnız derlenecek
        # tam belge; QC/EPUB Phase 5.5 gövdesini okur, ona dokunulmaz).
        # G3a-1: kalan [H] bloklar ölçülü yerinde birim (taşma koruması).
        if full_document:
            body = self._glue_float_captions(body, report)
            body = self._place_floats_inplace(body, report)

        # --- B22b: \left\right delimiter balance after split (Yol A, 2026-06-19) ---
        # B22'nin overfull-split'i \\ ile satira bolerken \left...\right cifti
        # iki satira dagilabilir -> "Extra \right" / "Missing \right" hatasi ve
        # norm/mutlak-deger cubuklarinin kaybi. Her satiri \right./\left. ile
        # dengele.
        body = self._balance_split_delimiters(body, report)

        # --- Yol A (2026-06-19): ham URL'leri \url{} ile sar + uzun chapter
        # basliklarina kisa header. Ham uzun URL'ler dar metin sutununda margin
        # disina tasiyor; \url{} (template'te xurl ile) her yerden kirilabilir.
        body = self._wrap_raw_urls(body, report)
        body = self._add_short_chapter_headers(body, report)
        # G3a-2 (2026-10-05): düzyazı kod kutusu -> alıntı (yalnız
        # tam belge; en son: alıntı metnine bu koşuda başka geçiş dokunmaz).
        if full_document:
            body = self._prose_listings_to_quote(body, report)
        return body

    def _escape_stray_percent(
        self, body: str, report: SanitizeReport
    ) -> str:
        r"""Kacissiz ``%`` -> ``\%``.

        Marker URL yuzde-kodlari (``%20``, ``%3D``) ve ceviri yuzdeleri
        (``%36``, ``%100``) kacissiz kalirsa ``%`` LaTeX yorumu baslatir ve
        satirin geri kalanini (icindeki ``}`` dahil) yutar -> brace
        dengesizligi + ICERIK KAYBI (bir İngilizce bilim kitabı: \\end{document}
        grup icinde kaldi, 'no legal \\end' -> kesik derleme).

        Korunanlar: gercek yorum satirlari (``%`` ile baslayan), ``\\url{...}``
        (verbatim, ``%`` ham kalmali), ``{``/``}`` hemen ardindan satir sonuna
        kadar giden yorum (``\\resizebox{..}{!}{%``; R17, 2026-09-25).
        ``$...$``/display/code zaten stash'li.
        ``\\href`` URL'lerinde ``\\%`` hyperref tarafindan dogru cozulur.
        """
        url_re = re.compile(r"\\url\{[^}]*\}")
        # '% FILTERED [' isaretcisi _filter_dangerous_commands'in kendi
        # yorumudur; kacislanirsa PDF'de gorunur hale gelir (#29) — muaf.
        bare_re = re.compile(r"(?<!\\)%(?!\s*FILTERED\s*\[)")
        out = []
        n = 0
        for line in body.split("\n"):
            if line.lstrip().startswith("%"):  # gercek yorum satiri
                out.append(line)
                continue
            urls: List[str] = []

            def _stash_url(m: "re.Match") -> str:
                urls.append(m.group(0))
                return f"\x00U{len(urls) - 1}\x00"

            tmp = url_re.sub(_stash_url, line)
            # R17: satır sonu yorumu ('...{%', '}%') makro yazımının boşluk
            # yutucusudur, yüzde değil. Kaçışlanırsa görünür '%' basılıyordu
            # (taranmış bir bölümde 6 tablo parçasında '\resizebox{..}{!}{\%').
            eol = _EOL_BRACE_COMMENT_RE.search(tmp)
            tail = ""
            # F2-06: '\textbf{50}%' satır sonunda yüzdedir, yorum değil.
            if eol and not _eol_percent_is_prose(tmp, eol.start()):
                tmp, tail = tmp[:eol.start()], tmp[eol.start():]
            tmp, c = bare_re.subn(r"\\%", tmp)
            tmp += tail
            for i, u in enumerate(urls):
                tmp = tmp.replace(f"\x00U{i}\x00", u)
            n += c
            out.append(tmp)
        if n:
            report.artifacts_removed += n
            report.details.append(f"Stray % escaped ({n}x)")
        return "\n".join(out)

    def _fix_unbalanced_inline_math_parens(
        self, body: str, report: SanitizeReport
    ) -> str:
        r"""Marker'in citation/yil parantezlerini \\( \\) (inline math) yapan
        artefaktini onar.

        Marker PDF cikarmada '(2015)' -> '\\(2015)' veya '(98\\)' uretebilir;
        eslesmeyen \\( / \\) inline-math moduna girip belge sonuna kadar metni
        yutar -> XeLaTeX 'Incomplete \\ifmmode' + Emergency stop -> kesik PDF
        (bir İngilizce bilim kitabı: 7 eslesmeyen \\(, PDF 443->364 sayfa).

        Stack ile esle: yalniz ESLESMEYEN \\( / \\) duz parantize cevrilir.
        Dengeli \\(...\\) (gercek inline math olabilir) DOKUNULMAZ.
        """
        toks = [(m.start(), m.group()) for m in re.finditer(r"\\[()]", body)]
        stack: List[int] = []
        unmatched = set()
        for pos, t in toks:
            if t == "\\(":
                stack.append(pos)
            else:  # \)
                if stack:
                    stack.pop()
                else:
                    unmatched.add(pos)
        unmatched.update(stack)  # kapanmamis \(
        if not unmatched:
            return body
        chars = list(body)
        for pos in sorted(unmatched, reverse=True):
            chars[pos:pos + 2] = ["(" if body[pos + 1] == "(" else ")"]
        report.artifacts_removed += len(unmatched)
        report.details.append(
            f"Unbalanced inline-math parens fixed ({len(unmatched)}x)"
        )
        return "".join(chars)

    def _wrap_raw_urls(self, body: str, report: SanitizeReport) -> str:
        """Ham http(s) URL'leri \\url{} ile sar (tasma onleme).

        Zaten \\href{/\\url{ icinde olanlara dokunma (onunde {, /, = varsa atla).
        Marker'in \\_ kacisli URL'lerini \\url icin sadelestir.
        """
        # URL karakterleri VEYA escaped-ozel (\_ \% \# \&) eslesir; ama \ + HARF
        # (komut, or. \href) URL'yi BITIRIR. Yoksa Marker'in bare-URL + bitisik
        # \href artefakti (http://x/fi\href{...}{...}) \url{...\href} enkazina
        # donusup brace/iffalse kilidi yaratir.
        url_re = re.compile(r"(?<![{/=])(https?://(?:[^\s}{$\\]|\\[_%#&~])+)")
        # V1 denetimi (2026-09-29): bu adım stash açıldıktan sonra koşar; kod
        # örneklerindeki (verbatim/lstlisting/minted, \verb) URL harfiyen
        # kalır. Eskiden kod bloğuna "\url{" ve "}" düz metin olarak
        # basılıyordu (bir test kitabında: 'read.csv(url("\url{https://…}"))').
        code_spans = [(c.start(), c.end()) for rx in (_CODE_ENV_RE, _VERB_RE)
                      for c in rx.finditer(body)]

        wrapped = 0

        def _wrap(m: "re.Match") -> str:
            nonlocal wrapped
            if any(s <= m.start() < e for s, e in code_spans):
                return m.group(0)
            wrapped += 1
            u = m.group(1)
            # URL icindeki LaTeX kacislarini sadelestir (\url verbatim ister)
            for esc, plain in (("\\_", "_"), ("\\&", "&"), ("\\#", "#"),
                               ("\\%", "%"), ("\\~", "~")):
                u = u.replace(esc, plain)
            stripped = u.rstrip(".,);:")
            # Kirpilan cumle noktalamasi metne GERI eklenir — eskiden
            # eslesme tuketildigi icin nokta/parantez kayboluyordu (#42).
            trail = u[len(stripped):]
            return "\\url{" + stripped + "}" + trail

        new = url_re.sub(_wrap, body)
        if wrapped:
            report.details.append(f"Raw URLs wrapped in \\url ({wrapped}x)")
            return new
        return body

    def _add_short_chapter_headers(
        self, body: str, report: SanitizeReport
    ) -> str:
        """Parantezli uzun chapter basliklarina kisa header ekle.

        '\\chapter{X (Y)}' -> '\\chapter[X]{X (Y)}' : running header'da parantezli
        uzun baslik dar sutunu tasiriyordu; kisa form (parantez atilmis) header'da
        kullanilir, tam form basligin kendisinde kalir.
        """
        n = 0

        def _short(m: "re.Match") -> str:
            nonlocal n
            full = m.group(1)
            s = re.sub(r"\s*\([^)]*\)", "", full).strip()
            # Kisa form brace-dengesizse (ic komutun '}'i parantezle
            # birlikte silinmis olabilir) donusumu uygulama: eskiden
            # '\chapter{X (Y) \emph{Z}}' dengeli dosyayi dengesiz
            # yapiyordu (#1/#41).
            if s.count("{") != s.count("}"):
                return m.group(0)
            if s and s != full:
                n += 1
                return "\\chapter[" + s + "]{" + full + "}"
            return m.group(0)

        # sadece optional-arg'i OLMAYAN \chapter{...}; baslik bir seviye
        # ic ice brace ('\emph{...}') icerebilir — '[^}]*' ILK '}'de
        # kesilip basligi bozuyordu (#1/#41).
        body = re.sub(
            r"\\chapter\{((?:[^{}]|\{[^{}]*\})*)\}", _short, body
        )
        if n:
            report.details.append(f"Short chapter headers added ({n}x)")
        return body

    def _balance_split_delimiters(
        self, body: str, report: SanitizeReport
    ) -> str:
        """multline/align/gather satirlarinda \\left/\\right dengesini kur.

        Bir \\\\-ayrilmis satirda kapanmamis \\left varsa sonuna \\right. eklenir;
        sonraki satir basina ayni sayida \\left. konur (gorunmez delimiter).
        """
        env_re = re.compile(
            r"(\\begin\{(?:multline\*?|align\*?|gather\*?)\})(.*?)"
            r"(\\end\{(?:multline\*?|align\*?|gather\*?)\})",
            re.DOTALL,
        )
        left_re = re.compile(r"\\left\b")
        right_re = re.compile(r"\\right\b")
        fixed = 0

        def _balance(body_text: str) -> str:
            nonlocal fixed
            rows = re.split(r"(\\\\)", body_text)
            out = []
            carry = 0
            for part in rows:
                if part == "\\\\":
                    out.append(part)
                    continue
                seg = part
                if carry > 0:
                    seg = ("\\left. " * carry) + seg
                    carry = 0
                nl = len(left_re.findall(seg))
                nr = len(right_re.findall(seg))
                if nl > nr:
                    seg = seg.rstrip() + (" \\right." * (nl - nr))
                    carry = nl - nr
                    fixed += 1
                out.append(seg)
            return "".join(out)

        body = env_re.sub(
            lambda m: m.group(1) + _balance(m.group(2)) + m.group(3), body
        )
        if fixed:
            report.details.append(
                f"Split \\left\\right delimiters balanced ({fixed}x)"
            )
        return body

    # ------------------------------------------------------------------
    # P1/P2: Marker bölüm/kısım numaralandırma artefaktları (2026-06-14)
    # ------------------------------------------------------------------

    # Modül düzeyindeki tek kaynaklı kuralların takma adları (QC aynı
    # kuralları kaynağa da uygular: SECTION_NUM_PREFIX_RE,
    # apply_chapter_label_rules).
    _SPURIOUS_CHAPTER_RE = _SPURIOUS_CHAPTER_RE
    # Section başlığı başındaki numara prefix'i: "1.1 ", "2 ", "3.4.5 "
    _SECTION_NUM_PREFIX_RE = SECTION_NUM_PREFIX_RE
    _LABEL_SECTION_THEN_TITLE_RE = _LABEL_SECTION_THEN_TITLE_RE
    _LABEL_SECTION_BEFORE_CHAPTER_RE = _LABEL_SECTION_BEFORE_CHAPTER_RE
    # (0a)'nın ürettiği chapter satırının geçici işareti (bkz. adım 2).
    _MERGED_CHAPTER_MARK = "\x00merged-chapter\x00"

    def _fix_marker_chapter_artifacts(
        self, body: str, report: SanitizeReport,
    ) -> str:
        """Marker artefaktları: (0) section seviyesinde kalmış 'Chapter N'
        etiketi + ardındaki aynı-düzey başlığı tek \\chapter{Başlık} yap
        (belgede \\chapter yoksa yalnız etiketi düşür), \\chapter'dan hemen
        önceki etiket satırını sil; (1) boş 'Chapter N'/'Bölüm N' sahte
        chapter satırlarını sil; (2) ilk \\chapter başlık-chapter ise
        (ardından \\section gelmeden başka \\chapter geliyorsa) numarasız
        (\\chapter*) yap. (0) ve (1) modül düzeyinde tek kaynaktır
        (``apply_chapter_label_rules``; QC kaynağa da uygular).

        Böylece gerçek bölümler 1'den numaralanır. Marker, kitap başlığını ve her
        gerçek bölüm öncesinde boş bir 'Chapter N' başlığını chapter olarak üretir.
        """
        mark = self._MERGED_CHAPTER_MARK
        # F1-06/F2-09: kanıt, etiket satırları silinmeden önce okunur.
        epub_source = (self.source_format
                       or self._meta.get("source_format", "").lower()) == _EPUB_SOURCE
        ev_title, evidence = ((None, "") if epub_source
                              else _title_chapter_evidence(body))
        body, count = _apply_chapter_label_rules(body, merged_mark=mark)

        # Title-chapter -> \chapter* SADECE Marker title-page örüntüsünde:
        # birden fazla \chapter VAR ve ilk ikisi arasında \section YOK
        # (yani ilk chapter sadece başlık/künye, ardından gerçek 1. bölüm gelir).
        # Tek chapter'lı veya section'lı ilk chapter -> DOKUNMA (false positive).
        # A-11 (2026-07-27): section'sız kitaplarda (roman/deneme — hiçbir
        # bölümde \section yok) bu sezgi GERÇEK 1. bölümü numarasızlaştırıyordu.
        # Ek şart: ilk chapter'ın gövdesi kısa olmalı (başlık/künye sayfası
        # ~1-2 KB'yi geçmez; gerçek bir bölüm binlerce karakterdir).
        # 2026-09-24 (denetim M1#2, M3#2): (0)'ın birleştirmeyle yarattığı
        # chapter'lar sayılmaz — yapı kararı kaynağın kendi chapter'larına
        # göre verilir; yoksa kısa ilk gerçek bölüm \chapter* oluyordu.
        # Belge birleşmiş (etiketli, yani gerçek) bir bölümle başlıyorsa
        # başlık sayfası yoktur: sonraki bir bölüm "ilk" sayılmaz.
        def _line_from(pos: int) -> str:
            end = body.find("\n", pos)
            return body[pos:] if end == -1 else body[pos:end]

        all_chapters = list(re.finditer(r"\\chapter(\*?)\{", body))
        chapters = [m for m in all_chapters if mark not in _line_from(m.start())]
        if (len(chapters) > 1 and chapters[0].group(1) == ""
                and chapters[0].start() == all_chapters[0].start()):
            first = chapters[0]
            segment = body[first.end():chapters[1].start()]
            title_m = _ANY_HEADING_RE.match(body, first.start())
            same_chapter = bool(title_m) and title_m.group(1) == ev_title
            if (not re.search(r"\\(?:sub)*section\*?\{", segment)
                    and len(segment.strip()) < 2000):
                if evidence and same_chapter:
                    body = (body[:first.start()] + r"\chapter*{"
                            + body[first.end():])
                    count += 1
                    report.details.append(
                        f"Title chapter unnumbered (evidence: {evidence})")
                else:
                    # Kısa ilk bölüm ama künye kanıtı yok: gerçek bölüm
                    # sayılır, numarası ve İçindekiler girdisi korunur.
                    reason = ("EPUB source" if epub_source
                              else "no title-page evidence")
                    report.details.append(
                        f"Short first chapter kept numbered ({reason})")
        body = body.replace(mark, "")

        if count:
            report.headers_cleaned += count
            report.details.append(f"Marker chapter artifacts fixed ({count}x)")
        return body

    def _strip_section_number_prefix(
        self, body: str, report: SanitizeReport,
    ) -> str:
        """'\\section{1.1 Başlık}' -> '\\section{Başlık}'.

        Marker section başlıklarına numarayı gömer; LaTeX otomatik numaralandırma
        ile çift numara ('3.5  1.5 ...') oluşur. Baştaki numara prefix'ini siler.
        """
        body, n = self._SECTION_NUM_PREFIX_RE.subn(r"\1", body)
        if n:
            report.headers_cleaned += n
            report.details.append(f"Section number prefix stripped ({n}x)")
        return body

    # ------------------------------------------------------------------
    # T5: Tablo gök cismi adlarını çevir (2026-06-14)
    # ------------------------------------------------------------------

    # Gök cismi / gezegen adları — deterministik, tüm kitaplar için güvenli.
    # Tablo HÜCRE METNİ olarak İngilizce kalır (ajan tabular'ı "math" sanıp atlar);
    # bu map tablo bloğu içinde plain-text satır/başlık etiketlerini Türkçeleştirir.
    # Math ortamları stash'li olduğundan ($...array...$ içindeki adlar) DOKUNULMAZ.
    _CELESTIAL_MAP = {
        "Celestial Body": "Gök Cismi",
        "Sun": "Güneş", "Earth": "Dünya", "Moon": "Ay",
        "Mercury": "Merkür", "Venus": "Venüs", "Jupiter": "Jüpiter",
        "Saturn": "Satürn", "Uranus": "Uranüs", "Neptune": "Neptün",
        "Pluto": "Plüton", "Planet": "Gezegen",
        # Mars -> Mars (değişmez), bilinçli atlandı
    }
    _TABULAR_BLOCK_RE = re.compile(
        r"\\begin\{tabular\}.*?\\end\{tabular\}", re.DOTALL
    )

    def _translate_table_terms(
        self, body: str, report: SanitizeReport,
    ) -> str:
        """Tablo (tabular) bloklarındaki İngilizce gök cismi/gezegen adlarını
        Türkçeleştir. Sadece plain-text hücreler; stash'li math'e dokunmaz."""
        stats = {"n": 0}

        def _repl_block(m: "re.Match[str]") -> str:
            block = m.group(0)
            for en, tr in self._CELESTIAL_MAP.items():
                pat = re.compile(
                    r"(?<![A-Za-zçğıöşüÇĞİÖŞÜ])" + re.escape(en)
                    + r"(?![A-Za-zçğıöşüÇĞİÖŞÜ])"
                )
                block, c = pat.subn(tr, block)
                stats["n"] += c
            return block

        body = self._TABULAR_BLOCK_RE.sub(_repl_block, body)
        if stats["n"]:
            report.headers_cleaned += stats["n"]
            report.details.append(
                f"Table celestial terms translated ({stats['n']}x)"
            )
        return body

    # ------------------------------------------------------------------
    # T7: Bozuk footnote / HTML sup-sub markup temizliği (2026-06-14)
    # ------------------------------------------------------------------

    def _clean_sup_sub_markup(
        self, body: str, report: SanitizeReport,
    ) -> str:
        """Marker'ın bozduğu footnote işaretleri ve HTML <sup>/<sub> kalıntıları.
        ``${}^{&}$lt;sup>2</sup>`` -> ``\\textsuperscript{2}``. Stash RESTORE
        SONRASI çağrılır (math fragment'i tam görünür)."""
        count = 0
        # Marker mangled footnote: ${}^{&}$lt;sup>N</sup>
        body, n = re.subn(
            r"\$\{\}\^\{&\}\$lt;sup>([^<]*)</sup>",
            r"\\textsuperscript{\1}", body,
        )
        count += n
        # HTML sup/sub (düz veya encode edilmiş)
        for pat, repl in (
            (r"<sup>([^<]*)</sup>", r"\\textsuperscript{\1}"),
            (r"<sub>([^<]*)</sub>", r"\\textsubscript{\1}"),
            (r"&lt;sup&gt;([^&]*)&lt;/sup&gt;", r"\\textsuperscript{\1}"),
            (r"&lt;sub&gt;([^&]*)&lt;/sub&gt;", r"\\textsubscript{\1}"),
        ):
            body, n = re.subn(pat, repl, body)
            count += n
        if count:
            report.html_tags_cleaned += count
            report.details.append(f"sup/sub markup cleaned ({count}x)")
        return body

    # ------------------------------------------------------------------
    # Phase 0.5: Fix Marker \textit{X}word → $X_{\mathrm{word}}$ (2026-05-09)
    # ------------------------------------------------------------------

    # Match \textit{<single letter>}<lowercase word>
    # Word must be lowercase to avoid catching sentence boundaries like
    # "\\textit{e}'ler" or "\\textit{m} kütleli". The \b anchor ensures
    # we don't grab subsequent letters from a longer identifier.
    _TEXTIT_SUBSCRIPT_RE = re.compile(
        r"\\textit\{([A-Za-z])\}([a-z][a-z0-9]{1,15})\b"
    )

    # Whitelist of common subscript words used in physics/math papers.
    # If the word is not on this list, leave the \\textit alone (could be a
    # legitimate emphasized phrase like "\\textit{e} = 0").
    _SUBSCRIPT_WHITELIST: Set[str] = {
        "free", "forced", "true", "ref", "in", "out", "up", "down",
        "max", "min", "avg", "mean", "rms", "tot", "total", "eff",
        "effective", "obs", "calc", "exp", "theory", "init", "final",
        "old", "new", "left", "right", "top", "bottom", "front", "back",
        "rad", "tan", "tang", "tangential", "radial", "normal", "axial",
        "ext", "int", "internal", "external",
    }

    def _fix_textit_subscripts(
        self, body: str, report: SanitizeReport,
    ) -> str:
        """Convert ``\\textit{X}word`` to ``$X_{\\mathrm{word}}$`` for
        whitelisted physics/math subscripts.

        This is a Marker OCR artifact: subscripted variables
        ``e_{\\mathrm{free}}`` get flattened to ``\\textit{e}free`` in the
        markdown source. Without conversion, the trailing word lands in
        plain text where glossary apply corrupts it.
        """
        count = 0

        def _repl(m: "re.Match[str]") -> str:
            nonlocal count
            var = m.group(1)
            word = m.group(2)
            if word.lower() not in self._SUBSCRIPT_WHITELIST:
                return m.group(0)
            count += 1
            return f"${var}_{{\\mathrm{{{word}}}}}$"

        body = self._TEXTIT_SUBSCRIPT_RE.sub(_repl, body)

        if count:
            report.details.append(
                f"Marker subscript artifact \\textit{{X}}word fixed ({count}x)"
            )
            logger.info(
                "Phase 0.5: Fixed %d \\textit{X}word subscript artifact(s).",
                count,
            )
        return body

    # ------------------------------------------------------------------
    # Phase 0.6: \bar{X} → \overline{X} (2026-05-09)
    # ------------------------------------------------------------------

    _BAR_ACCENT_RE = re.compile(r"\\bar\s*\{([^{}]+)\}")

    def _bar_to_overline(
        self, body: str, report: SanitizeReport,
    ) -> str:
        """Replace ``\\bar{X}`` with ``\\overline{X}``.

        STIX Two Math + unicode-math sometimes renders the ``\\bar``
        accent on italic variables in a way that visually resembles
        the not-equal glyph (≠). ``\\overline`` produces a wider, more
        legible bar and renders correctly across the whole STIX family.
        """
        count = 0

        def _repl(m: "re.Match[str]") -> str:
            nonlocal count
            count += 1
            return f"\\overline{{{m.group(1)}}}"

        body = self._BAR_ACCENT_RE.sub(_repl, body)

        if count:
            report.details.append(
                f"\\bar{{X}} → \\overline{{X}} ({count}x)"
            )
            logger.info("Phase 0.6: Replaced %d \\bar with \\overline.", count)
        return body

    # ------------------------------------------------------------------
    # B23: Figure/table placement — G3a-1 (2026-10-05): [H] KORUNUR
    # ------------------------------------------------------------------
    # B23 (2026-05-09) [H]'yi [!htb]'ye gevşetiyordu, R13 (2026-09-25)
    # yalnız dış altyazılı floatlarda [H] bırakıyordu; o birimler de
    # [!htb] float olarak kayıyordu. Sonuç (bir test raporunda): beş şekil
    # ve dört tablo anıldıkları metinden sonraya kaydı; grafiği tanıtan,
    # "…:" ile biten cümle grafiğinden iki sayfa önce kaldı; kayan şekildeki
    # dipnotun metni işaretinden ayrı sayfaya düştü (üç dipnotta). Artık
    # her şekil/tablo kaynak sırasında, yerinde dizilir ([H]). Taşmaya karşı
    # güvenli yöntem (yalnız derlenecek tam belge, Phase 6): birim önce
    # ölçülür (\setbox0\vbox); metin alanına sığıyorsa yerinde bölünmez
    # kutudur ([H] ile aynı), sığmıyorsa sayfadan
    # taşmaz: dış altyazılı birim yerinde bölünebilir açılır (\unvbox0),
    # float.sty kutusu (iç altyazılı) metin alanı yüksekliğine küçültülür
    # (kayıpsız; eskiden "Float too large" ile bölüm sonuna erteleniyordu).
    # Birimin hemen ardındaki \footnotetext[...]{...} dizisi kutunun hemen
    # arkasına, kapanış aralığından ÖNCE alınır: işaret ile metin arasında
    # sayfa kesme noktası kalmaz.

    _FIGURE_H_RE = re.compile(r"\\begin\{(figure|table)\}\[H\]")

    def _relax_figure_placement(
        self, body: str, report: SanitizeReport,
    ) -> str:
        """``[H]`` şekil/tablo yerleşimi KORUNUR (G3a-1; eskiden [!htb]).

        Gövde değişmez; yalnız sayı raporlanır. Taşma koruması ve tek
        birim kurulumu tam belgede ``_glue_float_captions`` ile
        ``_place_floats_inplace``'tedir.
        """
        kept = len(self._FIGURE_H_RE.findall(body))
        if kept:
            report.details.append(
                f"Figure/table [H] kept in source order ({kept}x)")
        return body

    # R13 (yalnız derlenecek tam belge, Phase 6): altyazısı float DIŞINDA
    # kalan şekil/tablo, altyazı paragrafıyla TEK birim olarak dizilir.
    # Neden: float.sty [H] kutusunu '\box ... \vskip\intextsep' ile bırakır,
    # aradaki yapıştırıcı yasal sayfa kesme noktasıdır (sentetik fr: görsel
    # s6, altyazı s7). Birim önce ölçülür (\setbox0\vbox): metin alanına
    # sığıyorsa yerinde bölünmez kutu ('\vskip\intextsep\box0', float.sty
    # [H] aralığıyla aynı); sığmıyorsa (kısa \textheight'lı taranmış kitap)
    # '\unvbox0' ile yerinde, bölünebilir kalır. Altyazı paragrafının
    # dizgisi değişmez (kutu float dışında kurulur). G3a-1: eskiden sığan
    # birim '[!htb]' float olarak kayıyordu.
    _GLUE_UNSAFE_RE = re.compile(r"\\(?:footnote|marginpar|begin|end)\b")
    # Altyazı paragrafı üst sınırı (karakter): uzun paragraf altyazı değil,
    # şekle atıfla başlayan gövde metnidir; birlikte kaydırılmaz. Derlemdeki
    # en uzun gerçek altyazı ~600 karakter (bir İngilizce test kitabında).
    # Tek kaynak: dönüştürücünün float altyazı sınırıyla aynı ölçüt
    # (patterns.FLOAT_CAPTION_MAX_CHARS; Vq 2026-09-28, ikinci literal kaldırıldı).
    _GLUE_MAX_CAPTION_CHARS = FLOAT_CAPTION_MAX_CHARS
    # Birim sonunun ortak kuyruğu: (dipnot metinleri) + float.sty [H]'nin
    # kapanış aralığı.
    _UNIT_TAIL = "\\vskip\\intextsep\\par"

    @classmethod
    def _unit_tail(cls, footnotes: str) -> str:
        return ("\n" + footnotes.strip() + "\n" if footnotes.strip() else "\n") \
            + cls._UNIT_TAIL

    @classmethod
    def _glued_unit(cls, kind: str, parts: List[str],
                    footnotes: str = "") -> str:
        """*parts*: sırayla birim parçaları (float içeriği ``{..\\par}`` grubu
        ve altyazı paragrafı). Çıktı ölçülüp yerinde dizilir (bkz. R13)."""
        # G3b önerisi (MT-25): parçalar arasında altyazı aralığı;
        # eskiden yalnız \lineskip (1 pt) kalıyor, altyazı çizgiye değiyordu.
        return ("\\par\\setbox0\\vbox{%\n"
                + "\\par\\vskip\\abovecaptionskip\n".join(parts) + "\\par}%\n"
                "\\ifdim\\dimexpr\\ht0+\\dp0\\relax>\\textheight"
                "\\unvbox0 \\else\\vskip\\intextsep\\box0 \\fi"
                + cls._unit_tail(footnotes))

    @staticmethod
    def _caption_par(para: str) -> str:
        """G3b önerisi (MT-25): dış altyazı paragrafı birimde altyazı gibi
        dizilir — girintisiz ve ortalı (eskiden girintili, iki yana yaslı,
        tablonun altında sola kaymış görünüyordu). Metin değişmez."""
        return "{\\noindent\\centering " + para.strip() + "\\par}"

    @classmethod
    def _inplace_unit(cls, block: str, footnotes: str = "") -> str:
        """``\\begin{figure}[H]…\\end{figure}`` bloğu (iç altyazılı ya da
        altyazısız): float.sty kutusu ölçülür, sığarsa yerinde (float.sty
        [H] ile aynı), sığmazsa metin alanı yüksekliğine küçültülür."""
        # Küçültülen kutu sayfayı doldurur: ardındaki aralık sayfa kesme
        # noktası kalır (dipnot metni sığmazsa sonraki sayfaya geçer, kutu
        # ile birlikte sayfadan taşmaz).
        return ("\\par\\setbox0\\vbox{" + block + "}%\n"
                "\\ifdim\\dimexpr\\ht0+\\dp0\\relax>\\textheight"
                "{\\centering\\resizebox*{!}{\\textheight}{\\box0}\\par}"
                "\\vskip\\intextsep"
                "\\else\\setbox0\\vbox{\\unvbox0\\unskip}\\unvbox0 \\fi"
                + cls._unit_tail(footnotes))

    def _glue_float_captions(
        self, body: str, report: SanitizeReport,
    ) -> str:
        """``[H]`` şekil/tablo + dış altyazı paragrafını tek birim yap.

        Altyazısı float dışında ve bitişik bloklar. Şekilde altyazı sonraki,
        tabloda belgenin altyazı yönündeki paragraftır
        (``_float_caption_sides``). Dipnotlu, ortam içeren ya da uzun
        paragraf birime alınmaz (``_place_floats_inplace``'e kalır). Birimde
        ``[H]`` float kalmadığı için ikinci geçiş aynı bloğu yeniden
        işlemez.
        """
        out: List[str] = []
        pos = 0
        n = 0
        # Kod örneklerindeki (verbatim/lstlisting/minted, \verb) float
        # metni harfiyen kalır: bu adım stash açıldıktan sonra koşar.
        code_spans = _code_spans(body)
        sides = _float_caption_sides(body)
        for m in _FLOAT_H_BLOCK_RE.finditer(body):
            if m.start() < pos or "\\caption" in m.group(2):
                continue
            if _in_spans(m.start(), m.end(), code_spans) or _is_wrapped_float(body, m):
                continue
            kind = m.group(1)
            content = "{" + m.group(2).strip("\n") + "\\par}"
            span = _outside_caption(body, m, sides, floor=pos)
            if span is None:
                continue
            c0, c1 = span
            para = body[c0:c1].strip()
            if not self._is_glueable_caption(para, kind):
                continue
            if c0 < m.start():
                start, end, parts = c0, m.end(), [self._caption_par(para), content]
            else:
                start, end, parts = m.start(), c1, [content, self._caption_par(para)]
            fn_end = _trailing_footnotetexts(body, end)
            out.append(body[pos:start])
            out.append(self._glued_unit(kind, parts, body[end:fn_end]))
            pos = fn_end
            n += 1
        if not n:
            return body
        out.append(body[pos:])
        report.details.append(
            f"Figure/table and outside caption set as one float unit, in place "
            f"({n}x)")
        return "".join(out)

    def _place_floats_inplace(
        self, body: str, report: SanitizeReport,
    ) -> str:
        """Kalan ``[H]`` şekil/tablo bloklarını ölçülü yerinde birime al
        (G3a-1; yalnız tam belge, ``_glue_float_captions``'tan sonra)."""
        out: List[str] = []
        pos = 0
        n = 0
        code_spans = _code_spans(body)
        for m in _FLOAT_H_BLOCK_RE.finditer(body):
            if m.start() < pos or _in_spans(m.start(), m.end(), code_spans):
                continue
            if _is_wrapped_float(body, m) or m.group(2).strip() == "\\box0":
                continue
            fn_end = _trailing_footnotetexts(body, m.end())
            out.append(body[pos:m.start()])
            out.append(self._inplace_unit(m.group(0), body[m.end():fn_end]))
            pos = fn_end
            n += 1
        if not n:
            return body
        out.append(body[pos:])
        report.details.append(
            f"Figure/table set in place with overflow guard ({n}x)")
        return "".join(out)

    def _prose_listings_to_quote(
        self, body: str, report: SanitizeReport,
    ) -> str:
        """G3a-2: düzyazı içerikli (dil seçeneksiz) lstlisting -> alıntı.

        Yalnız derlenecek tam belgede, gövde geçişlerinin EN SONUNDA: alıntı
        metnine bu koşuda başka geçiş dokunmaz (QC/EPUB Phase 5.5 gövdesini
        okur, ona dokunulmaz). verbatim/minted içindeki örneklere dokunulmaz.
        """
        if "\\begin{lstlisting}" not in body:
            return body
        others = [(c.start(), c.end()) for c in _CODE_ENV_RE.finditer(body)
                  if not c.group(0).startswith("\\begin{lstlisting}")]
        others += [(c.start(), c.end()) for c in _VERB_RE.finditer(body)]
        out: List[str] = []
        pos = 0
        n = merged = 0
        for m in _BARE_LSTLISTING_RE.finditer(body):
            if _in_spans(m.start(), m.end(), others):
                continue
            if not listing_is_prose(m.group(1)):
                continue
            if listing_continues_paragraph(body[pos:m.start()], m.group(1)):
                # Yarım cümlenin devamı -> önceki paragrafa bağlanır.
                tail_end = len(body[:m.start()].rstrip())
                out.append(body[pos:tail_end])
                out.append(" " + _escape_quote_text(
                    " ".join(ln.strip() for ln in m.group(1).split("\n")
                             if ln.strip())))
                pos = m.end()
                merged += 1
                continue
            out.append(body[pos:m.start()])
            out.append(listing_to_quote(m.group(1)))
            pos = m.end()
            n += 1
        if not n and not merged:
            return body
        out.append(body[pos:])
        report.details.append(
            f"Code block of prose set as quote (no line numbers/frame) ({n}x), "
            f"joined to unfinished paragraph ({merged}x)")
        return "".join(out)

    def _is_glueable_caption(self, para: str, kind: str) -> bool:
        """Paragraf, float'a yapıştırılacak bir altyazı mı (bkz. _GLUE_*).

        Altyazı ölçütü ``_caption_like_para``; ek olarak kısa ve kutuya
        alınması güvenli olmalı (dipnot/kenar notu/ortam içermez)."""
        para = para.strip()
        if not para or len(para) > self._GLUE_MAX_CAPTION_CHARS:
            return False
        if self._GLUE_UNSAFE_RE.search(para):
            return False
        return _caption_like_para(para, kind)

    def _fix_substack_rows(
        self, stash: List[str], body: str, report: SanitizeReport,
    ) -> Tuple[List[str], str]:
        """G3a-6: matematik bölgelerinde ve stash'li gövdede (kod bölgeleri
        yer tutucudadır) ``fix_substack_rows``."""
        total = 0
        out: List[str] = []
        for zone in stash:
            if _is_code_zone(zone) or not _is_math_zone(zone):
                out.append(zone)
                continue
            zone, n = fix_substack_rows(zone)
            total += n
            out.append(zone)
        body, n = fix_substack_rows(body)
        total += n
        if total:
            report.details.append(
                f"Substack rows split at control space ({total}x)")
        return out, body

    def _space_math_list_commas(
        self, stash: List[str], report: SanitizeReport,
    ) -> List[str]:
        """F2-05: stash'teki matematik bölgelerinde liste virgülüne boşluk
        (bkz. ``space_list_commas``). İngilizceye özgü kural yalnız kaynak
        dil AÇIKÇA "en" verildiyse uygulanır."""
        english = self.source_lang_known and self.source_lang == "en"
        total = 0
        out: List[str] = []
        for zone in stash:
            if _is_code_zone(zone) or not _is_math_zone(zone):
                out.append(zone)
                continue
            zone, n = space_list_commas(zone, english_source=english)
            total += n
            out.append(zone)
        if total:
            report.details.append(
                f"Math list comma spaced (not decimal) ({total}x)")
        return out

    # ------------------------------------------------------------------
    # E (2026-10-06, mt okuma MT-24): kaynak PDF kutusu ortamının dengesi
    # ------------------------------------------------------------------

    _BOX_ENV_RE = re.compile(r"\\(begin|end)\{" + _KUTU_ORTAMI + r"\}")

    def _balance_box_env(self, body: str, report: SanitizeReport) -> str:
        """Eşi olmayan ``\\begin{kckutu}`` / ``\\end{kckutu}`` satırını kaldır.

        Dönüştürücü kutuyu (latex_pipeline/kutu_onarim.py) iki ayrı satırda
        açıp kapatır. Çevirmen (insan, yapay zekâ ya da yerel model) satırın
        birini düşürür ya da sırayı bozarsa derleme "\\begin{kckutu} ...
        ended by ..." hatasıyla duruyordu. Eşi olmayan satır (ya da satırda
        başka metin varsa yalnız komut) silinir: içerik kutusuz, aynen basılır
        (kayıpsız). İç içe kutu dönüştürücüde hiç üretilmez; içteki açılış
        eşsiz sayılır.
        """
        if _KUTU_ORTAMI not in body:
            return body
        matches = list(self._BOX_ENV_RE.finditer(body))
        if not matches:
            return body
        unmatched: List[re.Match] = []
        open_m: Optional[re.Match] = None
        for m in matches:
            if m.group(1) == "begin":
                if open_m is not None:
                    unmatched.append(open_m)
                open_m = m
            elif open_m is not None:
                open_m = None
            else:
                unmatched.append(m)
        if open_m is not None:
            unmatched.append(open_m)
        if not unmatched:
            return body
        for m in sorted(unmatched, key=lambda x: x.start(), reverse=True):
            ls = body.rfind("\n", 0, m.start()) + 1
            le = body.find("\n", m.end())
            le = len(body) if le < 0 else le
            if body[ls:le].strip() == m.group(0):
                body = body[:ls] + body[min(le + 1, len(body)):]
            else:
                body = body[:m.start()] + body[m.end():]
        report.commands_fixed += len(unmatched)
        report.details.append(
            f"Unmatched box environment line removed ({len(unmatched)}x)")
        logger.warning("Eşi olmayan kutu ortamı satırı kaldırıldı (%dx); "
                       "içerik kutusuz basılır.", len(unmatched))
        return body

    # ------------------------------------------------------------------
    # G1: Görsel yüksekliği üst sınırı (2026-09-25)
    # ------------------------------------------------------------------

    def _cap_figure_heights(
        self, body: str, report: SanitizeReport,
    ) -> str:
        """Genişlik verilmiş görsellere ``FIGURE_MAX_HEIGHT`` sınırı ekle.

        Ayrıntı ve sabitin gerekçesi: ``cap_includegraphics_height`` ve
        ``FIGURE_MAX_HEIGHT`` notu. Görsel yalnız sınırı aşarsa küçülür.
        """
        body, count = cap_includegraphics_height(body)
        if count:
            report.figure_heights_capped += count
            report.details.append(
                f"Figure height capped at {FIGURE_MAX_HEIGHT} ({count}x)"
            )
            logger.info(
                "G1: %d \\includegraphics height-capped at %s "
                "(keepaspectratio, shrink-only).",
                count, FIGURE_MAX_HEIGHT,
            )
        return body

    # ------------------------------------------------------------------
    # B24: Multi-line equation* → align* (2026-05-09)
    # ------------------------------------------------------------------

    def _multiline_equation_to_align(
        self, body: str, report: SanitizeReport,
    ) -> str:
        """Convert multi-line ``\\begin{equation*}...\\end{equation*}``
        blocks (where each line is a separate equation with ``=``) into
        ``\\begin{align*}...\\end{align*}``.

        Reason: ``equation*`` is single-line; if Marker emitted 4 separate
        equations inside one ``equation*`` block (separated by newlines),
        XeLaTeX joins them onto one line which then either overflows the
        margin or (after B22 resizebox) becomes unreadably small.
        ``align*`` properly stacks them with ``\\\\`` line breaks and ``&``
        alignment.

        Heuristic: if the block contains 2+ non-empty lines where each
        contains an ``=`` sign, convert to align* and:
          - prepend ``&`` before the first ``=`` on each line
          - append ``\\\\`` at end of each line (except the last)
        """
        count = 0

        def _convert(m: "re.Match[str]") -> str:
            nonlocal count
            inner = m.group(1)
            # Split into "logical" lines, ignoring blank lines and \tag
            raw_lines = inner.split("\n")
            content_lines: List[str] = []
            tag_line = None
            for ln in raw_lines:
                stripped = ln.strip()
                if not stripped:
                    continue
                if stripped.startswith(r"\tag"):
                    tag_line = stripped
                    continue
                content_lines.append(ln)

            # Need 2+ equation lines and every line must contain '='
            if len(content_lines) < 2:
                return m.group(0)
            if not all("=" in ln for ln in content_lines):
                return m.group(0)
            # Skip if already has \\ line breaks (already aligned-style)
            if any(r"\\" in ln for ln in content_lines):
                return m.group(0)

            # Build align* version: insert & before first = on each line,
            # join with \\ + newline.
            converted: List[str] = []
            for ln in content_lines:
                # Strip trailing comma so we don't double up
                ln_rstrip = ln.rstrip()
                # Prepend & before the first TOP-LEVEL = (subscript ici
                # '='e & girmez, #33)
                eq_pos = self._first_toplevel_eq(ln_rstrip)
                if eq_pos > 0 and ln_rstrip[eq_pos - 1] != "&":
                    ln_rstrip = ln_rstrip[:eq_pos] + "&" + ln_rstrip[eq_pos:]
                converted.append(ln_rstrip)

            # Join with \\ line break
            inner_new = " \\\\\n".join(converted)
            if tag_line:
                inner_new += "\n" + tag_line
            count += 1
            return f"\\begin{{align*}}\n{inner_new}\n\\end{{align*}}"

        # Use a local pattern (B22 defines a class-level one further below
        # but we run before that block in the class body).
        _eq_pat = re.compile(
            r"\\begin\{equation\*\}(.*?)\\end\{equation\*\}",
            re.DOTALL,
        )
        body = _eq_pat.sub(_convert, body)

        if count:
            report.details.append(
                f"Multi-line equation* converted to align* ({count}x)"
            )
            logger.info(
                "B24: Converted %d multi-line equation* block(s) to align*.",
                count,
            )
        return body

    # ------------------------------------------------------------------
    # B22: Auto-split overfull display math into multline* (2026-05-09)
    # ------------------------------------------------------------------
    #
    # Goal: Keep all equations at the SAME font size (academic standard).
    # Long single-line equations are split at operator boundaries (+, -, =)
    # using ``\\\\`` line breaks, then wrapped in ``multline*`` so LaTeX
    # naturally typesets them across multiple lines without scaling.

    # G3a-3 (2026-10-05, RAPOR MT-6): bölme ölçütü artık GÖRÜNÜR genişlik
    # (display_math_width_em). Eski ölçüt kaynak uzunluğuydu (180
    # karakter): tek satıra sığan denklemler bölünüyor, kısa kaynaklı geniş
    # denklemler taşıyordu. Eşik: _MATH_WIDTH_SAFETY × satır genişliği
    # (Phase 6'da önsözden, gövde evresinde _MATH_LINE_EM_UNKNOWN).

    def _math_split_threshold_em(self) -> float:
        """Bu genişliği (em) aşan display denklem bölünmeye adaydır."""
        line = self._math_line_em or _MATH_LINE_EM_UNKNOWN
        return _MATH_WIDTH_SAFETY * line

    def _math_chunk_limit_em(self) -> float:
        """Bölünen denklemin bir satırının en çok genişliği (em)."""
        line = self._math_line_em or _MATH_LINE_EM_NARROW
        return _MATH_WIDTH_SAFETY * line

    # Regex: \[ ... \] (display math, dotall)
    _DISPLAY_MATH_BLOCK_RE = re.compile(r"\\\[(.*?)\\\]", re.DOTALL)
    # Regex: \begin{equation*} ... \end{equation*}
    _EQUATION_STAR_BLOCK_RE = re.compile(
        r"\\begin\{equation\*\}(.*?)\\end\{equation\*\}",
        re.DOTALL,
    )
    # Top-level operators where we can introduce \\ line breaks.
    # Match only when at brace depth 0 (handled in _split_at_operators).
    _SPLIT_OPERATOR_RE = re.compile(r"(?<!\\)([+\-=]|\\cdot\b|\\times\b)")

    # Top-level multi-equation separators on one line:
    # 1. \qquad / \quad
    # 2. ", " followed by a fresh "var =" definition (Marker often
    #    drops \qquad and leaves bare comma between definitions).
    _QQUAD_SEP_RE = re.compile(
        r",?\s*\\q?quad\s+"
        r"|"
        r",\s+(?=[a-zA-Z](?:_\{[^}]+\}|\^\{[^}]+\})?\s*=)"
    )

    def _wrap_overfull_displaymath(
        self, body: str, report: SanitizeReport,
    ) -> str:
        """Auto-split overfull display-math blocks.

        Strategy (in order):
        1. If the block contains 2+ ``\\qquad``-separated independent
           equations (each with its own ``=``), split into ``align*`` so
           every equation gets its own line with proper alignment.
        2. Otherwise, split at top-level operator boundaries (``+``,
           ``-``, ``=``) and wrap in ``multline*``.
        3. If neither strategy finds safe break points, leave the block
           untouched.

        All sub-lines render at the SAME font size — this matches academic
        typography conventions (no scaling).
        """
        align_count = 0
        multline_count = 0
        threshold = self._math_split_threshold_em()

        def _wrap_repl(m: "re.Match[str]", env_kind: str) -> str:
            nonlocal align_count, multline_count
            inner = m.group(1)
            inner_stripped = inner.strip()
            if display_math_width_em(inner_stripped) <= threshold:
                return m.group(0)
            # Strip inner matrix-like environments first; their internal
            # \\\\ (row separators) must NOT count as outer line breaks.
            inner_outer = re.sub(
                r"\\begin\{[bpv]?matrix\}.*?\\end\{[bpv]?matrix\}",
                "", inner, flags=re.DOTALL,
            )
            inner_outer = re.sub(
                r"\\begin\{(?:smallmatrix|array)\}.*?\\end\{(?:smallmatrix|array)\}",
                "", inner_outer, flags=re.DOTALL,
            )
            # Skip if outer scope is already structured / split
            if any(marker in inner_outer for marker in (
                r"\begin{aligned}", r"\begin{split}", r"\begin{cases}",
                r"\begin{multline}", r"\begin{align}", "\\\\",
            )):
                return m.group(0)
            # Strategy 1: \qquad-separated multi-equation → align*
            aligned = self._split_at_qquad(inner_stripped)
            if aligned is not None:
                align_count += 1
                return f"\\begin{{align*}}\n{aligned}\n\\end{{align*}}"
            # Strategy 2: operator-boundary split → multline*
            split = self._split_at_operators(inner_stripped)
            if split is None:
                return m.group(0)
            multline_count += 1
            return f"\\begin{{multline*}}\n{split}\n\\end{{multline*}}"

        body = self._DISPLAY_MATH_BLOCK_RE.sub(
            lambda m: _wrap_repl(m, "display"), body,
        )
        body = self._EQUATION_STAR_BLOCK_RE.sub(
            lambda m: _wrap_repl(m, "equation*"), body,
        )

        total = align_count + multline_count
        if total:
            report.details.append(
                f"Overfull display-math split: {align_count}× align*, "
                f"{multline_count}× multline*"
            )
            logger.info(
                "B22: Split %d overfull display-math block(s): "
                "%d→align*, %d→multline* (threshold=%.1f em).",
                total, align_count, multline_count, threshold,
            )
        return body

    def _split_at_qquad(self, math: str) -> Optional[str]:
        """If the equation contains 2+ ``\\qquad``-separated parts, each
        with its own ``=``, return an ``align*``-formatted body.

        Each part gets ``&`` before its first ``=`` and is joined with
        ``\\\\``. A trailing ``\\tag{...}`` is preserved on the last line.
        """
        # Detach trailing \tag{...}
        tag_match = re.search(r"\s*\\tag\{[^}]+\}\s*$", math)
        tag_str = ""
        if tag_match:
            tag_str = tag_match.group(0).strip()
            math = math[:tag_match.start()].rstrip()

        # Split at top-level (depth 0) \qquad / \quad
        parts = self._split_top_level(math, self._QQUAD_SEP_RE)
        # Each part must contain '='
        if len(parts) < 2 or not all("=" in p for p in parts):
            return None
        # Add & before first TOP-LEVEL = on each part (subscript ici '='e
        # & girmez, #78)
        aligned_parts: List[str] = []
        for p in parts:
            p = p.strip().rstrip(",")
            eq = self._first_toplevel_eq(p)
            if eq > 0 and p[eq - 1] != "&":
                p = p[:eq] + "&" + p[eq:]
            aligned_parts.append(p)
        body = " \\\\\n".join(aligned_parts)
        if tag_str:
            body += " " + tag_str
        return body

    @staticmethod
    def _first_toplevel_eq(s: str) -> int:
        """Brace/paren derinligi 0'daki ILK '=' pozisyonu; yoksa -1.

        B22/B24 hizalama '&'i eskiden naif find('=') ile yerlestiriliyordu;
        '\\sum_{i=1}' gibi alt-indis icindeki '='e '&' enjekte edilip
        'Misplaced alignment tab' derleme hatasi uretiyordu (#33/#78).
        """
        depth = 0
        i = 0
        n = len(s)
        while i < n:
            c = s[i]
            if c == "\\":
                i += 2
                continue
            if c in "({":
                depth += 1
            elif c in ")}":
                depth -= 1
            elif c == "=" and depth == 0:
                return i
            i += 1
        return -1

    def _split_top_level(
        self, math: str, sep_re: "re.Pattern[str]",
    ) -> List[str]:
        """Split ``math`` at occurrences of ``sep_re`` that are at brace
        depth 0 (i.e., not inside ``{...}`` groups).

        Two-pass: (1) find all sep_re matches with finditer, (2) track
        brace depth as we walk the string and accept a match only if
        depth==0 at its start position.
        """
        all_matches = list(sep_re.finditer(math))
        if not all_matches:
            return [math.strip()] if math.strip() else []

        # Compute depth at every char position (small math strings)
        depth_at: List[int] = [0] * (len(math) + 1)
        depth = 0
        i = 0
        n = len(math)
        while i < n:
            c = math[i]
            if c == "\\":
                # Skip escape sequence
                depth_at[i] = depth
                if i + 1 < n:
                    depth_at[i + 1] = depth
                i += 2
                continue
            if c in "({":
                depth += 1
            elif c in ")}":
                depth -= 1
            depth_at[i] = depth if c not in "({)}" else (
                depth if c in ")}" else depth - 1
            )
            # Set the actual depth_at this position before any open/close
            # (cleaner: record depth BEFORE applying):
            i += 1

        # Simpler re-walk to record depth at start of each char
        depth_at = [0] * (n + 1)
        depth = 0
        i = 0
        while i < n:
            depth_at[i] = depth
            c = math[i]
            if c == "\\":
                if i + 1 < n:
                    depth_at[i + 1] = depth
                i += 2
                continue
            if c in "({":
                depth += 1
            elif c in ")}":
                depth -= 1
            i += 1
        depth_at[n] = depth

        parts: List[str] = []
        last = 0
        for m in all_matches:
            if depth_at[m.start()] == 0:
                parts.append(math[last:m.start()])
                last = m.end()
        parts.append(math[last:])
        return [p.strip() for p in parts if p.strip()]

    def _split_at_operators(self, math: str) -> Optional[str]:
        """Insert ``\\\\`` line breaks at top-level (brace-depth-0)
        operator boundaries to split a long equation across multiple lines.

        Returns the rewritten math string, or ``None`` if no safe split
        points were found (caller should leave the equation untouched).

        Strategy:
        - Walk the string tracking brace depth.
        - Mark candidate split points right before a top-level ``+``,
          ``-``, or ``=``.
        - Aim for chunks of ~150 chars; if one segment would be too short
          (<60 chars), skip that split point and continue.
        - Preserve any trailing ``\\tag{...}`` on the last line.
        """
        # Detach trailing \tag{...} so it stays on the final line
        tag_match = re.search(r"\s*\\tag\{[^}]+\}\s*$", math)
        tag_str = ""
        if tag_match:
            tag_str = tag_match.group(0).strip()
            math = math[:tag_match.start()].rstrip()

        depth = 0
        candidates: List[int] = []
        i = 0
        n = len(math)
        while i < n:
            c = math[i]
            if c == "\\":
                # Skip escape sequence (\command or \{ \})
                i += 2
                continue
            # Track parens and braces but NOT square brackets: [...]
            # is the typical "outermost group" of a long equation
            # (\left[ ... \right]) whose interior operators we DO want
            # as break candidates. Parens are tracked to avoid splitting
            # inside \cos(\varpi - \varpi)-style function arguments.
            if c in "({":
                depth += 1
            elif c in ")}":
                depth -= 1
            elif depth == 0 and c in "+-=" and i > 0:
                # Don't split at unary minus (preceded by operator/start).
                # Walk back past whitespace to find the actual prev char.
                p = i - 1
                while p > 0 and math[p].isspace():
                    p -= 1
                prev = math[p] if p >= 0 else ""
                is_unary = (
                    c == "-" and (
                        p < 0 or
                        (p == 0 and math[0].isspace()) or
                        prev in "+-=*/([{,"
                    )
                )
                if is_unary:
                    i += 1
                    continue
                candidates.append(i)
            i += 1

        if not candidates:
            return None

        # G3a-3 (2026-10-05): parçalar GÖRÜNÜR genişliğe göre kurulur (eski
        # kural kaynak uzunluğuydu: 110-160 karakter; kısa kaynaklı geniş
        # denklemde hiç parça çıkmıyor, denklem taşıyordu). Satır sayısı en
        # az olacak, satırlar olabildiğince eşit genişlikte olacak.
        chunks = _balanced_chunks(math, candidates,
                                  self._math_chunk_limit_em())
        if chunks is None or len(chunks) < 2:
            return None

        # Append trailing \tag to the last line
        if tag_str:
            chunks[-1] = chunks[-1] + " " + tag_str

        return " \\\\\n".join(chunks)

    # ------------------------------------------------------------------
    # Phase 0: Invisible character cleanup (runs BEFORE stash)
    # ------------------------------------------------------------------

    def _clean_invisible_chars(
        self, body: str, report: SanitizeReport
    ) -> str:
        """Remove invisible Unicode characters that break stash regex matching.

        EPUB and PDF sources often contain zero-width characters (BOM, ZWSP,
        ZWNJ, ZWJ), non-breaking spaces, soft hyphens, and bidi marks.
        These are invisible in editors but can split LaTeX commands mid-token,
        causing ``_stash_protected()`` regexes to fail silently.

        This method runs on the RAW input BEFORE any stashing occurs.
        Replacements:
        - BOM, ZWSP, ZWNJ, ZWJ, soft hyphen, bidi marks, word joiner -> delete
        - NBSP (U+00A0) -> normal space
        - Line/paragraph separator (U+2028, U+2029) -> newline
        """
        def _replace_invisible(m: re.Match) -> str:
            return _INVISIBLE_CHAR_MAP[m.group(0)]

        matches = _INVISIBLE_CHAR_RE.findall(body)
        if matches:
            count = len(matches)
            body = _INVISIBLE_CHAR_RE.sub(_replace_invisible, body)
            report.invisible_chars_cleaned += count
            report.details.append(
                f"Invisible characters cleaned ({count}x): "
                f"BOM/ZWSP/NBSP/soft-hyphen/bidi-marks"
            )
            logger.debug(
                "Cleaned %d invisible character(s) before stash.", count,
            )

        # Yol A fix (2026-06-19): Marker bazi sayilari/markup'i C0 kontrol
        # karakterleriyle (ozellikle NULL \x00) sariyor: "W = \x007\x00".
        # Bunlar XeLaTeX'te "Text line contains an invalid character" fatal
        # hatasi veriyor (-halt-on-error) veya missing-char uyarisi. Tab/newline
        # disindaki tum C0 + DEL + C1 kontrol karakterlerini sil.
        ctrl_matches = _CONTROL_CHAR_RE.findall(body)
        if ctrl_matches:
            body = _CONTROL_CHAR_RE.sub("", body)
            report.invisible_chars_cleaned += len(ctrl_matches)
            report.details.append(
                f"Control characters stripped ({len(ctrl_matches)}x): "
                f"NULL/C0/DEL/C1 (Marker artifact)"
            )
        return body

    # ------------------------------------------------------------------
    # EPUB artifact cleanup
    # ------------------------------------------------------------------

    def _clean_epub_artifacts(
        self, body: str, report: SanitizeReport
    ) -> str:
        """Remove EPUB conversion artifacts from LaTeX body.

        EPUB-to-Markdown-to-LaTeX conversion leaves behind several types
        of artifacts:
        - ``@@NEXT``, ``@@PAGEBREAK`` etc. -- converter placeholders
        - ``@Bilinmeyen0000``, ``@Unknown123`` -- unknown reference markers
        - ``kindle:embed:XXXX`` -- Kindle embed URLs (replaced with note)
        - ``part0042.html#ch7-sec3`` -- EPUB internal links (standalone only)
        - Duplicate ``\\section{Title}`` + ``\\chapter{Title}`` pairs

        All operations are idempotent.
        """
        total_cleaned = 0

        # --- @@PLACEHOLDER patterns ---
        at_matches = _EPUB_AT_PLACEHOLDER_RE.findall(body)
        if at_matches:
            body = _EPUB_AT_PLACEHOLDER_RE.sub("", body)
            total_cleaned += len(at_matches)
            logger.debug("Removed %d @@PLACEHOLDER(s).", len(at_matches))

        # --- @Named0000 patterns ---
        named_matches = _EPUB_NAMED_PLACEHOLDER_RE.findall(body)
        if named_matches:
            body = _EPUB_NAMED_PLACEHOLDER_RE.sub("", body)
            total_cleaned += len(named_matches)
            logger.debug("Removed %d @Named placeholder(s).", len(named_matches))

        # --- kindle:embed:XXXX ---
        kindle_matches = _EPUB_KINDLE_EMBED_RE.findall(body)
        if kindle_matches:
            body = _EPUB_KINDLE_EMBED_RE.sub(r"\\textit{[resim]}", body)
            total_cleaned += len(kindle_matches)
            logger.debug(
                "Replaced %d kindle:embed URL(s) with [resim].",
                len(kindle_matches),
            )

        # --- EPUB internal links (standalone only, not inside \href{}) ---
        link_matches = _EPUB_INTERNAL_LINK_RE.findall(body)
        if link_matches:
            body = _EPUB_INTERNAL_LINK_RE.sub("", body)
            total_cleaned += len(link_matches)
            logger.debug(
                "Removed %d standalone EPUB internal link(s).",
                len(link_matches),
            )

        # --- Duplicate section+chapter headings ---
        dup_matches = _EPUB_DUPLICATE_HEADING_RE.findall(body)
        if dup_matches:
            # Keep \chapter{Title} (yildiziyla), remove the preceding
            # \section{Title}
            body = _EPUB_DUPLICATE_HEADING_RE.sub(
                lambda m: r"\chapter" + m.group(2) + "{" + m.group(1) + "}",
                body,
            )
            total_cleaned += len(dup_matches)
            logger.debug(
                "Removed %d duplicate section/chapter heading(s).",
                len(dup_matches),
            )

        if total_cleaned > 0:
            report.epub_artifacts_cleaned += total_cleaned
            report.details.append(
                f"EPUB artifacts cleaned ({total_cleaned}x): "
                f"placeholders, kindle URLs, internal links, duplicate headings"
            )
            logger.info(
                "Cleaned %d EPUB artifact(s).", total_cleaned,
            )

        # Clean up orphan whitespace from removed artifacts
        if total_cleaned > 0:
            body = re.sub(r"\n{3,}", "\n\n", body)

        return body

    # ------------------------------------------------------------------
    # Cleaning passes
    # ------------------------------------------------------------------

    def _remove_publisher_artifacts(
        self, body: str, report: SanitizeReport
    ) -> str:
        """Remove Springer CrossMark, copyright, Creative Commons, DOI."""
        for pat in _PUBLISHER_PATTERNS:
            matches = pat.findall(body)
            if matches:
                count = len(matches)
                body = pat.sub("", body)
                report.artifacts_removed += count
                report.details.append(
                    f"Publisher artifact removed ({count}x): "
                    f"{pat.pattern[:60]}"
                )
                logger.debug(
                    "Removed %d publisher artifact(s) matching: %s",
                    count, pat.pattern[:60],
                )
        return body

    def _remove_check_updates_logo(
        self, body: str, report: SanitizeReport
    ) -> str:
        """Remove Springer 'Check for updates' crossmark badge.

        Iki bicim: (1) eski wrapfigure formu (metin etiketli),
        (2) Marker'in urettigi caption'siz, chapter-acilis \\begin{figure} formu.
        """
        matches = _CHECK_UPDATES_LOGO_RE.findall(body)
        if matches:
            body = _CHECK_UPDATES_LOGO_RE.sub("", body)
            report.artifacts_removed += len(matches)
            report.details.append(
                f"Check-for-updates logo block removed ({len(matches)}x)"
            )
        # Caption'siz, chapter-acilis figur rozetleri (chapter korunur, figur
        # silinir). F2-10 (2026-09-29): yalniz DOGRULANMIS rozet silinir
        # (bkz. _MARKER_IMAGE_NAME_RE); geri kalan her gorsel kalir.
        removed = 0
        kept: Dict[str, List[str]] = {}

        def _badge(m: "re.Match[str]") -> str:
            nonlocal removed
            reason = self._badge_keep_reason(body, m)
            if reason:
                kept.setdefault(reason, []).append(
                    (_INCLUDEGRAPHICS_NAME_RE.findall(m.group(0)) or ["?"])[0])
                return m.group(0)
            removed += 1
            return m.group(1)

        body = _CHAPTER_BADGE_FIGURE_RE.sub(_badge, body)
        if removed:
            report.artifacts_removed += removed
            report.details.append(
                f"Chapter-opening badge figure removed ({removed}x)"
            )
        for reason, names in kept.items():
            report.details.append(
                f"Chapter-opening figure kept, {reason} ({len(names)}x): "
                + ", ".join(names[:3]))
            if reason == "image not found to verify badge":
                logger.warning(
                    "Bölüm açılışında altyazısız %d görsel korundu: rozet olup "
                    "olmadığı doğrulanamadı (görsel dosyası bulunamadı): %s",
                    len(names), ", ".join(names[:3]))
        return body

    def _badge_keep_reason(self, body: str, m: "re.Match[str]") -> str:
        """Bölüm açılış figürü *m* doğrulanmış yayıncı rozeti değilse
        korunma nedeni; rozetse ``""`` (F2-10)."""
        names = _INCLUDEGRAPHICS_NAME_RE.findall(m.group(0))
        if len(names) != 1:
            return "not a single image"
        p0, p1 = _para_after_at(body, m.end())
        if _caption_like_para(body[p0:p1], "figure"):
            return "caption follows"
        name = names[0].strip()
        if not _MARKER_IMAGE_NAME_RE.search(name):
            return "not a PDF badge image"
        size = self._image_size(name)
        if size is None:
            return "image not found to verify badge"
        w, h = size
        if (max(w, h) > _BADGE_MAX_SIDE_PX or min(w, h) <= 0
                or max(w, h) / min(w, h) > _BADGE_MAX_ASPECT):
            return f"not badge-sized ({w}x{h} px)"
        return ""

    def _image_dirs(self) -> List[Path]:
        """Rozet doğrulamasında görsellerin aranacağı dizinler."""
        dirs = list(self.image_dirs)
        out_dir = self._meta.get("output_dir", "")
        if out_dir:
            dirs += [Path(out_dir) / "source" / "images",
                     Path(out_dir) / "source", Path(out_dir) / "images"]
        return dirs

    def _image_size(self, name: str) -> Optional[Tuple[int, int]]:
        """``\\includegraphics`` adının piksel ölçüsü; bulunamazsa None."""
        rel = Path(name)
        if rel.is_absolute() or ".." in rel.parts:
            return None
        cands = [rel] if rel.suffix else [rel.with_suffix(e) for e in _IMAGE_EXTS]
        for d in self._image_dirs():
            for c in cands:
                path = d / c
                if not path.is_file():
                    continue
                try:
                    from PIL import Image
                    with Image.open(path) as im:
                        return im.size
                except Exception:  # bozuk / desteklenmeyen dosya
                    return None
        return None

    def _fix_double_backslash_artifacts(
        self, body: str, report: SanitizeReport
    ) -> str:
        """Marker'in cift-backslash + bozuk Python dunder artefaktlari (Yol A, 2026-06-19).

        Marker bazen text komutlarini cift backslash ile uretir
        ('\\\\textit{...}') -> XeLaTeX bunu satir-sonu (\\\\) + 'textit' metni
        sanir, "There's no line here to end" hatasi verir. Ayrica Python dunder
        metod adlarini bozar: '\\textit{\\_init\\}\\_' -> kapanmayan brace ('\\}')
        "Missing }" hatasi. URL'lerdeki '\\\\_' -> '_' subscript "Missing $".
        Math stash'lendigi icin guvenli (sadece prose/komut metni).
        """
        n = 0
        # 1) Cift-backslash + text-format komutu: \\textit -> \textit
        body, c = re.subn(
            r"\\\\(textit|textbf|texttt|textrm|textsf|emph|section|subsection)\b",
            r"\\\1", body,
        )
        n += c
        # 2) Bozuk dunder yapisi: \textit{\_NAME\}\_ -> \texttt{\_\_NAME\_\_}
        body, c = re.subn(
            r"\\text(?:it|tt|rm)\{\\_(\w+)\\\}\\_",
            r"\\texttt{\\_\\_\1\\_\\_}", body,
        )
        n += c
        # 3) Cift-backslash escaped underscore (URL artefakti): \\_ -> \_
        body, c = re.subn(r"\\\\_", r"\\_", body)
        n += c
        if n:
            report.artifacts_removed += n
            report.details.append(
                f"Double-backslash / dunder artifacts fixed ({n}x)"
            )
        return body

    def _fix_text_mode_wraps(
        self, body: str, report: SanitizeReport
    ) -> str:
        """Metin modu sarmalamaları (Yunan harfi + G3 sembolleri); tek giriş.

        Math/kod bölgeleri çağırandan stash'li gelir. Bağlantı hedefleri
        (\\url, \\href URL'si), \\(...\\) ve \\texorpdfstring yalnız bu
        iki geçiş için ayrıca korunur (_WRAP_PROTECTED_RE): URL'ye ve
        \\(...\\) içine '$\\leq$' yazılıyordu (denetim 2026-09-24 M1#5). Başlık
        argümanında sembol ``\\texorpdfstring{$\\alpha$}{α}`` olur (bkz.
        _HEADING_CMD_OPEN_RE). QC paritesi (apply_text_mode_math_wraps) aynı
        yöntemi çağırır.
        """
        body, zones = _stash_wrap_protected(body)

        def _pdf_safe(name: str, char: str) -> str:
            # Üretilen \texorpdfstring de korunur: gövde geçişi ikinci
            # argümandaki karakteri yeniden sarmasın (idempotent).
            return _push_wrap_protected(
                zones, "\\texorpdfstring{$\\" + name + "$}{" + char + "}")

        spans = _heading_arg_spans(body)
        if spans:
            parts: List[str] = []
            pos = 0
            for start, end in spans:
                if start < pos:   # iç içe başlık komutu: dış argüman işlendi
                    continue
                arg = self._fix_text_mode_greek(
                    body[start:end], report, wrap=_pdf_safe)
                arg = self._fix_text_mode_math_symbols(arg, report, wrap=_pdf_safe)
                parts.append(body[pos:start])
                parts.append(arg)
                pos = end
            parts.append(body[pos:])
            body = "".join(parts)
        body = self._fix_text_mode_greek(body, report)
        body = self._fix_text_mode_math_symbols(body, report)
        return _restore_wrap_protected(body, zones)

    def _fix_text_mode_greek(
        self, body: str, report: SanitizeReport,
        wrap: Optional[Callable[[str, str], str]] = None,
    ) -> str:
        """Text-mode Greek -> math mode (Yol A, 2026-06-19).

        Marker Yunan harflerini prose'da \\textit{θ} veya ciplak θ olarak
        biraktiginda metin fontu (Latin Modern Roman) Yunan glyph icermedigi
        icin PDF'de BOS render olur. Math moduna ($\\theta$) alarak math fontuna
        yonlendiririz. Math zaten stash'lendigi icin sadece prose'a dokunur.
        *wrap(ad, karakter)* verilirse sarmalamayı o üretir (başlık argümanı).
        """
        n = 0
        wrap = wrap or _math_wrap

        def _cmd(m: "re.Match") -> str:
            nonlocal n
            n += 1
            return wrap(_GREEK_NAME_MAP[m.group(1)], m.group(1))

        body = _GREEK_TEXTCMD_RE.sub(_cmd, body)

        def _bare(m: "re.Match") -> str:
            nonlocal n
            # Parite guard'i: satirda eslesmeden ONCE tek sayida kacissiz
            # '$' varsa bu harf muhtemelen stash'lenememis bir math'in
            # ICINDEDIR — sarmalama ic ice '$' uretir (audit #32). Atla.
            line_start = m.string.rfind("\n", 0, m.start()) + 1
            prefix = m.string[line_start:m.start()]
            if len(re.findall(r"(?<!\\)\$", prefix)) % 2 == 1:
                return m.group(0)
            # R6d: Yunanca sözcüğün harfi matematik sembolü değildir.
            if _is_greek_word_char(m.string, m.start()):
                return m.group(0)
            n += 1
            return wrap(_GREEK_NAME_MAP[m.group(0)], m.group(0))

        body = _GREEK_BARE_RE.sub(_bare, body)

        if n:
            report.details.append(
                f"Text-mode Greek wrapped in math ({n}x)"
            )
        return body

    # 2026-09-30 (F1, MT-10): metin kipinde LaTeX üst simge kalıntısı. Model,
    # MT'ye ham giden '\textsuperscript{1}' dipnot işaretini düz metne
    # '^{1}' olarak yazabiliyordu (hymt-de 1. tur: "Wert.^{1} Das …").
    # Metin kipinde '^' TeX hatasıdır ("Missing $ inserted"): paragrafın
    # kalanı matematik kipine düşer ya da işaret PDF'e bozuk basılır. Kök
    # dönüştürücüde (F1-10, 29 Eylül: sayfa altı dipnotu gerçek \footnote)
    # ve çeviride (F1: kısa \textsuperscript yer tutucuya alınır) kapandı;
    # bu geçiş son emniyettir: YALNIZ basit içerik (rakam / harf / * † ‡)
    # \textsuperscript{..} olur, içerik aynen korunur. '\^{o}' (aksan) ve
    # '^^' dokunulmaz; satırda stash'lenememiş açık '$' ya da '\(' varsa
    # işaret bir matematiğin içindedir, dokunulmaz.
    _TEXT_MODE_CARET_RE = re.compile(
        r"(?<![\\^])\^(?:\{([0-9A-Za-z*\u2020\u2021,+\-]{1,8})\}"
        r"|([0-9]{1,3})(?![0-9]))")

    def _fix_text_mode_superscript(
        self, body: str, report: SanitizeReport,
    ) -> str:
        """Metin kipindeki '^{1}' / '^1' -> '\\textsuperscript{1}' (F1 MT-10)."""
        n = 0

        def _repl(m: "re.Match") -> str:
            nonlocal n
            line_start = m.string.rfind("\n", 0, m.start()) + 1
            prefix = m.string[line_start:m.start()]
            if len(re.findall(r"(?<!\\)\$", prefix)) % 2 == 1:
                return m.group(0)
            if prefix.count("\\(") > prefix.count("\\)"):
                return m.group(0)
            if re.search(r"\\(?:url|href)\{[^}]*$", prefix):
                return m.group(0)
            n += 1
            return "\\textsuperscript{" + (m.group(1) or m.group(2)) + "}"

        body = self._TEXT_MODE_CARET_RE.sub(_repl, body)
        if n:
            report.details.append(
                f"Text-mode superscript '^{{..}}' -> \\textsuperscript ({n}x)")
            logger.info("Metin kipindeki %d üst simge kalıntısı "
                        "\\textsuperscript yapıldı.", n)
        return body

    def _fix_text_mode_math_symbols(
        self, body: str, report: SanitizeReport,
        wrap: Optional[Callable[[str, str], str]] = None,
    ) -> str:
        """G3 (2026-09-24): metin modundaki ≫ ≪ ≤ ≥ → ⇔ -> ``$\\gg$`` vb.

        ``_fix_text_mode_greek`` ile aynı gerekçe ve aynı parite guard'ı:
        metin fontunda bu glifler yok, math fontunda var. Math zaten
        stash'li olduğundan yalnız prose'a dokunur; satırda eşleşmeden önce
        tek sayıda kaçışsız ``$`` varsa sembol stash'lenememiş bir math'in
        içindedir, sarmalanmaz. Yalnız sembol sarılır ("M $\\gg$ m"):
        işlenenlerin sınırını metinden güvenle çıkarmak mümkün değil.
        *wrap* için bkz. ``_fix_text_mode_greek``.
        """
        n = 0
        wrap = wrap or _math_wrap

        def _wrap(m: "re.Match") -> str:
            nonlocal n
            line_start = m.string.rfind("\n", 0, m.start()) + 1
            prefix = m.string[line_start:m.start()]
            if len(re.findall(r"(?<!\\)\$", prefix)) % 2 == 1:
                return m.group(0)
            n += 1
            return wrap(_TEXT_MATH_SYMBOL_MAP[m.group(0)], m.group(0))

        body = _TEXT_MATH_SYMBOL_RE.sub(_wrap, body)
        if n:
            report.details.append(
                f"Text-mode math symbols wrapped in math ({n}x)"
            )
        return body

    def _clean_headers(self, body: str, report: SanitizeReport) -> str:
        """Remove PDF page header/footer artifacts.

        F2-11 (2026-09-29): tarih satırları ("12 Nisan 1961") üst bilgi
        sayılmaz (``_DATE_LINE_RE``). Manuel çeviride (kullanıcı ya da
        yapay zekâ asistanı) hiçbir satır SİLİNMEZ — çevirmenin metni
        otoritedir (kayıpsızlık); olası üst bilgi satırları raporda ve
        günlükte uyarı olarak görünür, çevirmen karar verir.
        """
        for pat in _HEADER_PATTERNS:
            found = [m.group(0) for m in pat.finditer(body)
                     if not _DATE_LINE_RE.fullmatch(m.group(0))]
            if not found:
                continue
            count = len(found)
            shown = ", ".join(repr(f.strip()[:50]) for f in found[:3])
            if self.manual_translation:
                report.details.append(
                    f"Possible running header kept, manual translation "
                    f"({count}x): {shown}")
                logger.warning(
                    "Olası sayfa üst bilgisi satırı korundu (manuel çeviri, "
                    "silinmedi) %dx: %s", count, shown)
                continue
            body = pat.sub(
                lambda m: m.group(0) if _DATE_LINE_RE.fullmatch(m.group(0))
                else "", body)
            report.headers_cleaned += count
            report.details.append(
                f"Header artifact removed ({count}x): "
                f"{pat.pattern[:60]}"
            )
            logger.debug(
                "Removed %d header artifact(s) matching: %s",
                count, pat.pattern[:60],
            )
        return body

    def _resolve_internal_links(
        self, body: str, report: SanitizeReport,
    ) -> str:
        """G3a-4: ``\\href{#ad}{metin}`` iç bağlantısını hedefine bağla.

        Hedef ``\\hypertarget{ad}`` ya da hyperref'in kendi adlarından biri
        (``page.3``, ``chapter.2`` …) ise bağlantı kalır; ``\\label{ad}``
        ise ``\\hyperref[ad]{metin}`` olur; hiçbiri yoksa bağlantı kalkar,
        metin olduğu gibi kalır (kayıpsız). Kod örneklerine dokunulmaz.
        """
        if "\\href" not in body:
            return body
        code = [(c.start(), c.end()) for rx in (_CODE_ENV_RE, _VERB_RE)
                for c in rx.finditer(body)]

        def _in_code(pos: int) -> bool:
            return any(a <= pos < b for a, b in code)

        targets = {m.group(1).strip() for m in _HYPERTARGET_NAME_RE.finditer(body)
                   if not _in_code(m.start())}
        labels = {m.group(1).strip() for m in _LABEL_NAME_RE.finditer(body)
                  if not _in_code(m.start())}
        counts = {"unlinked": 0, "hyperref": 0}

        def _resolve(text: str, offset: int) -> str:
            out: List[str] = []
            pos = 0
            for m in _INTERNAL_HREF_RE.finditer(text):
                if m.start() < pos or _in_code(offset + m.start()):
                    continue
                name = m.group(1)
                if name in targets or _HYPERREF_AUTO_DEST_RE.match(name):
                    continue
                close = _brace_group_end(text, m.end() - 1)
                if close < 0:
                    continue
                inner = _resolve(text[m.end():close], offset + m.end())
                out.append(text[pos:m.start()])
                if name in labels:
                    out.append(f"\\hyperref[{name}]{{{inner}}}")
                    counts["hyperref"] += 1
                else:
                    out.append(inner)
                    counts["unlinked"] += 1
                pos = close + 1
            out.append(text[pos:])
            return "".join(out)

        body = _resolve(body, 0)
        if counts["unlinked"] or counts["hyperref"]:
            report.details.append(
                f"Dead internal links: {counts['unlinked']} unlinked (text "
                f"kept), {counts['hyperref']} -> \\hyperref")
        return body

    def _fix_broken_hrefs(self, body: str, report: SanitizeReport) -> str:
        """Fix broken \\href{#page-XXX-0}{\\[ NNN}] patterns.

        Replaces with plain ``[N]`` citation-style reference.
        """
        matches = _BROKEN_HREF_RE.findall(body)
        if matches:
            body = _BROKEN_HREF_RE.sub(r"[\1]", body)
            report.hrefs_fixed += len(matches)
            report.details.append(
                f"Broken href fixed ({len(matches)}x): "
                f"\\href{{#page-...}} -> [N]"
            )
            logger.debug("Fixed %d broken href(s).", len(matches))
        return body

    def _fix_prompt_leaks(self, body: str, report: SanitizeReport) -> str:
        """Remove MT prompt leakage fragments.

        After pattern removal, lines that are left empty or contain only
        punctuation/whitespace are also removed (residual cleanup).

        A-1/A-4 (2026-07-27): MT-only kalıplar ("Çeviri/Çevirmen notu",
        CJK artefakt regex'i) manuel modda UYGULANMAZ — çevirmenin bilinçli
        notu/alıntısı silinmez. Modül sabiti tam listedir (Yol B sözleşmesi).
        """
        from latex_pipeline.patterns import PROMPT_LEAK_PATTERNS_MT_ONLY
        _patterns = list(_PROMPT_LEAK_PATTERNS)
        if self.manual_translation:
            # Karşılaştırma pattern STRING'i ile (reviewer R2): id() kıyası
            # modül yeniden yüklenirse sessizce kırılıyordu (iki liste ayrı
            # nesnelere ayrışınca MT-only kalıplar manuel modda geri geliyordu).
            _mt_only = {p.pattern for p in PROMPT_LEAK_PATTERNS_MT_ONLY}
            _patterns = [p for p in _patterns if p.pattern not in _mt_only]
        for pat in _patterns:
            matches = pat.findall(body)
            if matches:
                count = len(matches)
                body = pat.sub("", body)
                report.prompt_leaks_fixed += count
                report.details.append(
                    f"Prompt leak removed ({count}x): "
                    f"{pat.pattern[:60]}"
                )
                logger.debug(
                    "Removed %d prompt leak(s) matching: %s",
                    count, pat.pattern[:60],
                )

        # Residual cleanup: lines that became empty or trivial after
        # prompt leak removal (only whitespace, commas, periods, colons).
        # This prevents orphan lines like ", ." or "  " from remaining.
        body = re.sub(
            r"^\s*[.,;:!?\-\u2013\u2014]*\s*$",
            "",
            body,
            flags=re.MULTILINE,
        )
        # Collapse 3+ consecutive blank lines into 2
        body = re.sub(r"\n{3,}", "\n\n", body)
        return body

    # ------------------------------------------------------------------
    # NEW: HTML artifact cleanup (session 6, 2026-03-03)
    # ------------------------------------------------------------------

    def _clean_html_artifacts(
        self, body: str, report: SanitizeReport
    ) -> str:
        """Remove HTML tags and entities from HY-MT 7B repetition loops.

        The HY-MT 7B model enters degenerate repetition when it encounters
        patterns like "as follows:" and produces thousands of HTML table
        tags (colgroup, tbody, etc.).  This method:

        1. Converts meaningful HTML to LaTeX equivalents (<sup> -> \\textsuperscript)
        2. Strips all remaining HTML tags (handles repetition bursts)
        3. Replaces HTML entities with proper LaTeX/Unicode
        4. Removes lines that were entirely HTML (no real text content)

        All operations are idempotent -- running on clean LaTeX is a no-op.
        """
        total_cleaned = 0

        # --- Phase 1: Convert meaningful HTML to LaTeX ---
        # <sup>X</sup> -> \textsuperscript{X}
        sup_matches = _HTML_SUP_RE.findall(body)
        if sup_matches:
            body = _HTML_SUP_RE.sub(r"\\textsuperscript{\1}", body)
            total_cleaned += len(sup_matches)
            logger.debug("Converted %d <sup> tags to \\textsuperscript.", len(sup_matches))

        # <sub>X</sub> -> \textsubscript{X}
        sub_matches = _HTML_SUB_RE.findall(body)
        if sub_matches:
            body = _HTML_SUB_RE.sub(r"\\textsubscript{\1}", body)
            total_cleaned += len(sub_matches)
            logger.debug("Converted %d <sub> tags to \\textsubscript.", len(sub_matches))

        # <em>X</em> -> \emph{X}
        em_matches = _HTML_EM_RE.findall(body)
        if em_matches:
            body = _HTML_EM_RE.sub(r"\\emph{\1}", body)
            total_cleaned += len(em_matches)
            logger.debug("Converted %d <em> tags to \\emph.", len(em_matches))

        # <strong>X</strong> -> \textbf{X}
        strong_matches = _HTML_STRONG_RE.findall(body)
        if strong_matches:
            body = _HTML_STRONG_RE.sub(r"\\textbf{\1}", body)
            total_cleaned += len(strong_matches)
            logger.debug("Converted %d <strong> tags to \\textbf.", len(strong_matches))

        # <a href="...">text</a> -> text
        anchor_matches = _HTML_ANCHOR_RE.findall(body)
        if anchor_matches:
            body = _HTML_ANCHOR_RE.sub(r"\1", body)
            total_cleaned += len(anchor_matches)
            logger.debug("Stripped %d <a> tags, kept link text.", len(anchor_matches))

        # --- Phase 2: Strip all remaining HTML tags ---
        # Count individual tags before stripping (the + quantifier groups
        # consecutive tags into one match, but we want the real count).
        tag_count = len(re.findall(
            r"</?(?:colgroup|tbody|thead|tfoot|tr|td|th|table|div|span|p|br|hr"
            r"|ul|ol|li|dd|dt|dl|nav|header|footer|section|article|aside|main"
            r"|figure|figcaption|blockquote|pre|code|img|input|button|form"
            r"|select|option|textarea|label|fieldset|legend|details|summary"
            r"|dialog|template|slot|canvas|video|audio|source|track|embed"
            r"|object|param|iframe|link|meta|style|script|noscript|col"
            r")\b[^>]*/?>",
            body, re.IGNORECASE
        ))
        if tag_count > 0:
            body = _HTML_TAG_STRIP_RE.sub("", body)
            total_cleaned += tag_count
            logger.debug("Stripped %d HTML tags.", tag_count)

        # --- Phase 3: HTML entities ---
        entity_count = len(_HTML_ENTITY_RE.findall(body))
        if entity_count > 0:
            body = _HTML_ENTITY_RE.sub(_html_entity_repl, body)
            total_cleaned += entity_count
            logger.debug("Replaced %d HTML entities.", entity_count)

        # Numeric entities: &#123; &#x1F; etc. -> try to decode
        numeric_matches = _HTML_NUMERIC_ENTITY_RE.findall(body)
        if numeric_matches:
            body = _HTML_NUMERIC_ENTITY_RE.sub(_html_numeric_entity_repl, body)
            total_cleaned += len(numeric_matches)
            logger.debug("Decoded %d numeric HTML entities.", len(numeric_matches))

        # --- Phase 4: Remove lines that are now empty after HTML stripping ---
        # A line that was e.g. "<tr><td></td></tr>" is now "".
        # But we don't want to remove lines with stash placeholders.
        if tag_count > 0:
            cleaned_lines = []
            for line in body.split("\n"):
                stripped = line.strip()
                # Keep the line if it has real content or stash placeholder
                if stripped and not re.match(r"^\s*$", stripped):
                    cleaned_lines.append(line)
                elif "__SANSTASH_" in line:
                    cleaned_lines.append(line)
                else:
                    cleaned_lines.append("")  # Preserve as blank line
            body = "\n".join(cleaned_lines)
            # Collapse excessive blank lines (from stripped HTML blocks)
            body = re.sub(r"\n{4,}", "\n\n\n", body)

        if total_cleaned > 0:
            report.html_tags_cleaned += total_cleaned
            report.details.append(
                f"HTML artifacts cleaned ({total_cleaned}x): "
                f"tags stripped, entities replaced, meaningful tags -> LaTeX"
            )
            logger.info(
                "Cleaned %d HTML artifacts (tags + entities).", total_cleaned
            )
        return body

    # ------------------------------------------------------------------
    # NEW: Mis-translated LaTeX command fix (session 6, 2026-03-03)
    # ------------------------------------------------------------------

    def _fix_translated_commands(
        self, body: str, report: SanitizeReport
    ) -> str:
        """Fix LaTeX commands that were mistakenly translated to Turkish.

        HY-MT 7B sometimes translates standard LaTeX command names:
        - ``\\frontmatter`` -> ``\\on tarafmatter``
        - ``\\tableofcontents`` -> ``\\tableofiçeriks``
        - ``\\bottomrule`` -> ``\\şişe dibirule``

        This method applies known fixes and logs unknown Turkish-character
        commands for investigation.
        """
        total_fixed = 0

        for pat, replacement in _LATEX_COMMAND_FIXES:
            matches = pat.findall(body)
            if matches:
                count = len(matches)
                body = pat.sub(replacement, body)
                total_fixed += count
                logger.debug(
                    "Fixed %d translated command(s): %s -> %s",
                    count, pat.pattern[:40], replacement,
                )

        if total_fixed > 0:
            report.commands_fixed += total_fixed
            report.details.append(
                f"Translated LaTeX commands fixed ({total_fixed}x)"
            )

        # --- Catch-all: log unknown Turkish-character commands ---
        # These are NOT auto-corrected (false positive risk: e.g. user-defined
        # commands with Turkish names), but we log them for investigation.
        suspicious = _TURKISH_CHAR_COMMAND_RE.findall(body)
        if suspicious:
            # Deduplicate for logging
            unique_suspicious = sorted(set(suspicious))
            # Filter out known-good Turkish commands that users might define
            _KNOWN_GOOD_TR_COMMANDS = {
                r"\şekil", r"\bölüm", r"\başlık", r"\açıklama",
                r"\önerme", r"\tanım", r"\teorem", r"\ispat",
                r"\içindekiler", r"\kaynakça",
            }
            unknown = [
                cmd for cmd in unique_suspicious
                if cmd.lower() not in _KNOWN_GOOD_TR_COMMANDS
            ]
            if unknown:
                logger.warning(
                    "Found %d potentially mis-translated LaTeX command(s) "
                    "containing Turkish characters (NOT auto-fixed): %s",
                    len(unknown),
                    ", ".join(unknown[:10]),  # Log max 10
                )
                report.details.append(
                    f"Suspicious Turkish-char commands logged ({len(unknown)}x, "
                    f"not auto-fixed): {', '.join(unknown[:5])}"
                )

        return body

    # ------------------------------------------------------------------
    # FIX 1: Markdown link remnant cleanup (session 9, 2026-03-05)
    # ------------------------------------------------------------------

    def _clean_markdown_link_remnants(
        self, body: str, report: SanitizeReport
    ) -> str:
        """Remove markdown link remnants that survive OCR/EPUB conversion.

        Patterns cleaned:
        - ``[7](#sayfa-251-0)`` -> ``7`` (numbered internal links)
        - ``[text](#anchor)`` -> ``text`` (internal anchor links)
        - ``[text](url)`` -> ``text`` (external links, keep text only)
        - Does NOT touch image links ``![alt](src)`` (handled elsewhere)
        - Does NOT touch stash placeholders ``__SANSTASH_N__``
        """
        total_cleaned = 0

        # Pattern 1: [number](#anchor) -> number
        # e.g. [7](#sayfa-251-0), [8](#sayfa-251-1), [12](#page-42)
        p1 = re.compile(r"\[(\d+(?:,\s*\d+)*)\]\(#[^)]+\)")
        matches = p1.findall(body)
        if matches:
            body = p1.sub(r"\1", body)
            total_cleaned += len(matches)

        # Pattern 2: [text](#internal-anchor) -> text
        # e.g. [Chapter 3](#chapter-3), [bir sonraki bolum](#bolum-5)
        # But NOT ![alt](src) which starts with !
        p2 = re.compile(r"(?<!!)\[([^\]]{1,120})\]\(#[^)]+\)")
        matches = p2.findall(body)
        if matches:
            body = p2.sub(r"\1", body)
            total_cleaned += len(matches)

        # Pattern 3: [text](http...) or [text](relative-url) -> text
        # External links -- keep text, drop URL.  Skip stash placeholders.
        # Add trailing space only if URL is immediately followed by an
        # alphanumeric character; skip space before punctuation.
        _PUNCT_AFTER = frozenset(".,;:!?)]}'\"\u2019\u201D")
        p3 = re.compile(r"(?<!!)\[([^\]]{1,120})\]\([^)]+\)")

        def _p3_replace(m: re.Match) -> str:
            text = m.group(1)
            end = m.end()
            # Check what character follows the match in the original body.
            # Add space only if an alphanumeric char follows immediately;
            # skip space before punctuation, whitespace, or end-of-string.
            if end < len(body):
                next_ch = body[end]
                if next_ch not in _PUNCT_AFTER and not next_ch.isspace():
                    return text + " "
            return text

        count3 = len(p3.findall(body))
        if count3:
            body = p3.sub(_p3_replace, body)
            total_cleaned += count3

        # Clean up: collapse any accidental double+ spaces.
        if total_cleaned > 0:
            body = re.sub(r"  +", " ", body)
            report.markdown_links_cleaned += total_cleaned
            report.details.append(
                f"Markdown link remnants cleaned ({total_cleaned}x)"
            )
            logger.info(
                "Cleaned %d markdown link remnant(s).", total_cleaned
            )
        return body

    # ------------------------------------------------------------------
    # Float placeholder remnant cleanup (e.g. orphan [H] from \begin{figure}[H])
    # ------------------------------------------------------------------

    # Pattern matches standalone float specifier lines like [H], [h], [H!],
    # [htbp], [!ht] etc. that remain after Marker OCR strips \begin{figure}[H].
    _FLOAT_SPEC_RE = re.compile(
        r"^\s*\[(H|h|H!|htbp|htb|htp|ht|bp|tb|!htbp|!ht)\]\s*$",
        re.MULTILINE,
    )

    def _clean_float_placeholder_remnants(
        self, body: str, report: SanitizeReport
    ) -> str:
        """Remove orphan float-specifier lines left by Marker OCR.

        When Marker OCR converts PDF figures, ``\\begin{figure}[H]`` is
        sometimes split so that ``[H]`` ends up on its own line.  After
        translation, these bare ``[H]`` tokens appear as visible text in
        the output PDF.

        This method removes lines that consist *only* of a float specifier
        (e.g. ``[H]``, ``[htbp]``, ``[!ht]``).  Lines where the specifier
        is part of a ``\\begin{...}[...]`` command are NOT touched because
        the stash mechanism already protects them.
        """
        matches = self._FLOAT_SPEC_RE.findall(body)
        if matches:
            body = self._FLOAT_SPEC_RE.sub("", body)
            # Collapse any resulting double blank lines
            body = re.sub(r"\n{3,}", "\n\n", body)
            count = len(matches)
            report.float_placeholders_cleaned += count
            report.details.append(
                f"Float placeholder remnants cleaned ({count}x)"
            )
            logger.info(
                "Cleaned %d float placeholder remnant(s).", count
            )
        return body

    # ------------------------------------------------------------------
    # FIX 2: Translated LaTeX environment name fix (session 9, 2026-03-05)
    # ------------------------------------------------------------------

    # Map of Turkish (or other language) environment names to their correct
    # English LaTeX equivalents.  HY-MT 7B sometimes translates
    # \begin{figure} -> \begin{sekil}, \begin{table} -> \begin{tablo}, etc.
    _TRANSLATED_ENV_NAMES = {
        "sekil": "figure", "\u015fekil": "figure",  # sekil, sekil
        "tablo": "table",
        "denklem": "equation", "e\u015fitlik": "equation", "esitlik": "equation",
        "ispat": "proof", "kan\u0131t": "proof", "kanit": "proof",
        "teorem": "theorem",
        "tanim": "definition", "tan\u0131m": "definition",
        "ornek": "example", "\u00f6rnek": "example",
        "onerme": "proposition", "\u00f6nerme": "proposition",
        "sonuc": "corollary", "sonu\u00e7": "corollary",
        "varsayim": "assumption", "varsay\u0131m": "assumption",
        "algoritma": "algorithm",
        "liste": "itemize",
        "numarali": "enumerate", "numaral\u0131": "enumerate",
        "alinti": "quote", "al\u0131nt\u0131": "quote",
        "madde": "enumerate",
        "oge": "itemize", "\u00f6ge": "itemize", "\u00f6\u011fe": "itemize",
        "resim": "figure", "cizim": "figure", "\u00e7izim": "figure",
        "merkez": "center",
        "ozet": "abstract", "\u00f6zet": "abstract",
        "aciklama": "remark", "a\u00e7\u0131klama": "remark",
        "ozellik": "property", "\u00f6zellik": "property",
        "matris": "matrix",
        "hizalama": "align",
        "dizi": "array",
        # German/Spanish MT leakage
        "gleichung": "equation", "abbildung": "figure",
        "ecuacion": "equation", "figura": "figure",
    }

    def _fix_translated_env_names(
        self, body: str, report: SanitizeReport
    ) -> str:
        r"""Fix translated LaTeX environment names back to English.

        Replaces e.g. ``\begin{sekil}`` -> ``\begin{figure}`` and
        ``\end{sekil}`` -> ``\end{figure}``.

        This runs in the sanitizer (post-translation) to catch env name
        translations BEFORE they reach autofix/compile, preventing
        XeLaTeX compilation failures from unknown environments.

        A-9 (2026-07-27): gövdede \newenvironment/\newtheorem ile TANIMLI
        adlara dokunulmaz — kullanıcı kendi Türkçe ortamını tanımladıysa
        (örn. \newtheorem{tanim}{Tanım}) yeniden adlandırma tanım-kullanım
        tutarlılığını bozup derlemeyi KIRAR (koruma her iki yol için doğru).
        """
        total_fixed = 0

        defined_envs = set(re.findall(
            r"\\(?:newenvironment|renewenvironment|newtheorem\*?)\s*\{([^}]+)\}",
            body,
        )) | self.user_defined_envs

        for tr_name, en_name in self._TRANSLATED_ENV_NAMES.items():
            if tr_name in defined_envs:
                logger.debug(
                    "Env '%s' gövdede tanımlı (\\newenvironment/\\newtheorem) "
                    "— yeniden adlandırma atlandı.", tr_name,
                )
                continue
            # Match \begin{tr_name} and \end{tr_name}, with optional star
            pattern = re.compile(
                r"(\\(?:begin|end))\{"
                + re.escape(tr_name)
                + r"(\*?)\}"
            )
            matches = pattern.findall(body)
            if matches:
                count = len(matches)

                def _replacer(m: re.Match, correct=en_name) -> str:
                    return f"{m.group(1)}{{{correct}{m.group(2)}}}"

                body = pattern.sub(_replacer, body)
                total_fixed += count
                logger.debug(
                    "Fixed %d translated env name(s): %s -> %s",
                    count, tr_name, en_name,
                )

        if total_fixed > 0:
            report.translated_env_names_fixed += total_fixed
            report.details.append(
                f"Translated env names fixed ({total_fixed}x)"
            )
            logger.info(
                "Fixed %d translated environment name(s).", total_fixed
            )
        return body

    # ------------------------------------------------------------------
    # NEW: Dangerous LaTeX command filter (session 6, 2026-03-03)
    # ------------------------------------------------------------------

    def _filter_dangerous_commands(
        self, body: str, report: SanitizeReport
    ) -> str:
        """Filter potentially dangerous LaTeX commands for security.

        Defense-in-depth: XeLaTeX already runs with ``-no-shell-escape``,
        but we also strip dangerous commands at the sanitizer level.
        Commands are replaced with a LaTeX comment showing what was filtered.

        Filtered commands include:
        - ``\\write18`` / ``\\immediate\\write18`` (shell execution)
        - ``\\openin`` / ``\\openout`` (file I/O)
        - ``\\catcode`` (category code manipulation)
        - ``\\csname...\\endcsname`` (dynamic command creation)
        - ``\\directlua`` / ``\\luaexec`` (Lua code execution)
        - Suspicious ``\\input`` / ``\\include`` paths (path traversal)
        """
        total_filtered = 0

        for pat, description in _DANGEROUS_LATEX_PATTERNS:
            replaced = 0

            def _make_comment(m: re.Match, _desc: str = description) -> str:
                nonlocal replaced
                # Idempotentlik (#29/#87): satir zaten yorumsa dokunma —
                # eski kod kendi '% FILTERED' yorumunu her geciste yeniden
                # sarip satiri buyutuyordu.
                line_start = m.string.rfind("\n", 0, m.start()) + 1
                if m.string[line_start:m.start()].lstrip().startswith("%"):
                    return m.group(0)
                replaced += 1
                logger.warning(
                    "SECURITY: Filtered dangerous command: %s",
                    m.group(0).replace("\n", " ")[:120],
                )
                # Orijinal komut YORUMA GOMULMEZ (ikinci geciste yeniden
                # eslesiyordu); tamamen silinir, iz olarak komutsuz marker
                # kalir. Description'daki '\' temizlenir ki kacis
                # senaryolarinda aktif LaTeX'e donusemesin. Satir kuyrugu
                # \n ile yeni satira tasinir (eskiden yorumda kayboluyordu).
                return "% FILTERED [" + _desc.replace("\\", "") + "]\n"

            body = pat.sub(_make_comment, body)
            if replaced:
                total_filtered += replaced
                logger.warning(
                    "SECURITY: Filtered %d dangerous command(s): %s",
                    replaced, description,
                )

        if total_filtered > 0:
            report.dangerous_filtered += total_filtered
            report.details.append(
                f"Dangerous LaTeX commands filtered ({total_filtered}x)"
            )
            logger.warning(
                "SECURITY: Total %d dangerous command(s) filtered in this document.",
                total_filtered,
            )

        return body

    def _filter_math_zone(self, zone: str, report: SanitizeReport) -> str:
        """Stash'teki bir matematik bölgesine güvenlik filtresi.

        Filtre izi ("% FILTERED [...]" + satır sonu) ardındaki satır sonuyla
        boş satır oluşturabilir; matematikte boş satır derleme hatasıdır,
        değişen bölgede boş satırlar teke indirilir.
        """
        filtered = self._filter_dangerous_commands(zone, report)
        if filtered != zone:
            filtered = re.sub(r"\n(?:[ \t]*\n)+", "\n", filtered)
        return filtered

    # ------------------------------------------------------------------
    # Existing cleaning passes (unchanged)
    # ------------------------------------------------------------------

    def _clean_cjk_artifacts(
        self, body: str, report: SanitizeReport
    ) -> str:
        """Remove CJK character artifacts from HY-MT 7B training data.

        The HY-MT model occasionally leaks Chinese/Japanese/Korean
        characters into Turkish output (e.g. ``如图de``).  These are
        training data artifacts and never belong in a Turkish LaTeX
        document.
        """
        matches = _CJK_ARTIFACT_RE.findall(body)
        if matches:
            count = len(matches)
            body = _CJK_ARTIFACT_RE.sub("", body)
            report.cjk_cleaned += count
            report.details.append(
                f"CJK artifact removed ({count}x): "
                f"Chinese/Japanese/Korean characters"
            )
            logger.debug(
                "Removed %d CJK artifact(s).", count,
            )
        return body

    def _clean_batch_comments(
        self, body: str, report: SanitizeReport
    ) -> str:
        """Remove batch processing comments like ``% --- Batch N ---``."""
        matches = _BATCH_COMMENT_RE.findall(body)
        if matches:
            body = _BATCH_COMMENT_RE.sub("", body)
            report.artifacts_removed += len(matches)
            report.details.append(
                f"Batch comment removed ({len(matches)}x)"
            )
            logger.debug("Removed %d batch comment(s).", len(matches))
        return body

    def _normalize_labels_en_to_tr(
        self, body: str, report: SanitizeReport
    ) -> str:
        """Normalize English cross-reference labels to Turkish.

        Idempotent: skips if Turkish labels are already present.
        """
        # Idempotency guard -- if we already see Turkish labels, skip.
        if _TR_LABELS_PRESENT.search(body):
            logger.debug(
                "Turkish labels already present; skipping normalization."
            )
            return body

        total_normalized = 0
        for pat, replacement, label in _LABEL_NORMALIZATIONS:
            matches = pat.findall(body)
            if matches:
                count = len(matches)
                body = pat.sub(replacement, body)
                total_normalized += count
                logger.debug(
                    "Normalized %d label(s): %s", count, label,
                )

        if total_normalized > 0:
            report.captions_normalized += total_normalized
            report.details.append(
                f"EN->TR label normalization ({total_normalized}x)"
            )
        return body

    # ------------------------------------------------------------------
    # FIX 3: Foreign language word leak cleanup (2026-03-13)
    # ------------------------------------------------------------------

    # Spanish/Portuguese/French words that leak into Turkish translations.
    # Each entry: (compiled regex, replacement string).
    # These patterns match foreign words ONLY in Turkish prose context
    # (not inside LaTeX commands).
    _FOREIGN_WORD_LEAKS: List[Tuple[re.Pattern, str]] = [
        # "como gösterildiği" -> "gösterildiği"
        (re.compile(r"\bcomo\s+gösterildiği", re.IGNORECASE), "gösterildiği"),
        # "como belirtildiği" -> "belirtildiği"
        (re.compile(r"\bcomo\s+belirtildiği", re.IGNORECASE), "belirtildiği"),
        # "como açıklandığı" -> "açıklandığı"
        (re.compile(r"\bcomo\s+açıklandığı", re.IGNORECASE), "açıklandığı"),
        # "como tanımlandığı" -> "tanımlandığı"
        (re.compile(r"\bcomo\s+tanımlandığı", re.IGNORECASE), "tanımlandığı"),
        # "como" followed by any Turkish word (lowercase Turkish letter after space)
        # This is the general catch-all for "como + Turkish continuation".
        # Only matches when "como" is followed by a Turkish-specific character word.
        (re.compile(
            r"\bcomo\s+(?=[a-zA-ZçğıöşüÇĞİÖŞÜ]{3,})"
        ), ""),
        # Standalone "como" between Turkish words (not inside LaTeX commands)
        # e.g. "değerler como büyüktür" -> "değerler büyüktür"
        # Lookbehind: Turkish letter + space; lookahead: Turkish letter.
        (re.compile(
            r"(?<=[çğıöşüÇĞİÖŞÜa-zA-Z]\s)como\s+"
            r"(?=[a-zA-ZçğıöşüÇĞİÖŞÜ])"
        ), ""),
    ]

    def _clean_foreign_word_leaks(
        self, body: str, report: SanitizeReport
    ) -> str:
        """Remove foreign language word leaks from Turkish translation output.

        MT models (especially HY-MT 7B) sometimes leak Spanish, Portuguese,
        or French words into the Turkish output.  The most common leak is
        the Spanish word "como" (= "as/like") appearing before Turkish verbs:
        e.g. "como gösterildiği gibi" instead of "gösterildiği gibi".

        This method removes known foreign word patterns from prose text.
        LaTeX commands and math zones are already stashed at this point,
        so only prose content is affected.

        All operations are idempotent.
        """
        total_cleaned = 0

        for pat, replacement in self._FOREIGN_WORD_LEAKS:
            matches = pat.findall(body)
            if matches:
                count = len(matches)
                body = pat.sub(replacement, body)
                total_cleaned += count
                logger.debug(
                    "Cleaned %d foreign word leak(s): %s",
                    count, pat.pattern[:50],
                )

        # Clean up: collapse double spaces left after removal.
        if total_cleaned > 0:
            body = re.sub(r"  +", " ", body)
            report.foreign_words_cleaned += total_cleaned
            report.details.append(
                f"Foreign word leaks cleaned ({total_cleaned}x): "
                f"como and similar ES/PT/FR artifacts"
            )
            logger.info(
                "Cleaned %d foreign word leak(s).", total_cleaned
            )
        return body

    # ------------------------------------------------------------------
    # FIX 4: Stuck (concatenated) word fix (2026-03-13)
    # ------------------------------------------------------------------

    # Turkish "hiç" + verb patterns where a space is missing.
    # "hiçbir" is a valid compound word and must be EXCLUDED.
    # Pattern: "hiç" immediately followed by a lowercase Turkish letter
    # that is NOT "bir" (to protect "hiçbir", "hiçbirimiz", etc.).
    # 2026-07-16 (#40): 'hiçlik/hiçliğe/hiçe/hiçten/hiçleş-' mesru Turkce
    # kelimelerdir, bolunmez — lookahead genisletildi.
    _STUCK_HIC_RE = re.compile(
        r"\bhiç(?!bir|lik|liğ|e\b|ten\b|le[şs])([a-zA-ZçğıöşüÇĞİÖŞÜ])",
        re.UNICODE,
    )

    def _fix_stuck_words(
        self, body: str, report: SanitizeReport
    ) -> str:
        r"""Fix concatenated words where a space is missing.

        MT models sometimes produce stuck-together words like
        "hiçetkilemeyecektir" instead of "hiç etkilemeyecektir".

        Rules:
        - ``hiç`` + verb/adjective -> ``hiç `` + verb/adjective
        - ``hiçbir`` is a valid Turkish word and is NOT split
        - ``hiçbirimiz``, ``hiçbirisi``, ``hiçbirine`` etc. are NOT split

        Only ``hiç`` immediately followed by a non-``bir`` continuation
        is considered stuck and gets a space inserted.

        All operations are idempotent (already-spaced text is not matched).
        """
        matches = self._STUCK_HIC_RE.findall(body)
        if matches:
            count = len(matches)
            body = self._STUCK_HIC_RE.sub(r"hiç \1", body)
            report.stuck_words_fixed += count
            report.details.append(
                f"Stuck words fixed ({count}x): "
                f"'hiç' + verb separated"
            )
            logger.info(
                "Fixed %d stuck word(s) (hiç+verb).", count
            )
        return body

    # ------------------------------------------------------------------
    # FIX 5: Translated LaTeX commands back to original
    # ------------------------------------------------------------------
    # NOT (audit 2026-07-16 #31): kelime siniri ZORUNLU — duz str.replace
    # '\bölü' ⊂ '\bölüm' ve '\çarpı' ⊂ '\çarpım' carpismalarinda '\fracm',
    # '\timesm' gibi tanimsiz komut uretiyordu. Self-map girisler
    # ('\beta'->'\beta', '\sigma'->'\sigma') sahte sayac sisiriyordu, silindi.
    _TRANSLATED_LATEX_MAP = {
        r"\çarpı": r"\times",
        r"\alfa": r"\alpha",
        r"\gama": r"\gamma",
        r"\teta": r"\theta",
        r"\lamda": r"\lambda",
        r"\düşüncel": "ideal",
        r"\bölü": r"\frac",
    }

    def _fix_translated_latex_commands(
        self, body: str, report: "SanitizeReport"
    ) -> str:
        """Restore LaTeX commands that got translated to Turkish."""
        import re
        count = 0
        # Direct replacements — kelime sinirli (\bölü, \bölüm'e dokunmaz)
        for wrong, correct in self._TRANSLATED_LATEX_MAP.items():
            wrong_re = re.compile(
                re.escape(wrong) + r"(?![a-zA-ZçğıöşüÇĞİÖŞÜ])"
            )
            n = len(wrong_re.findall(body))
            if n:
                body = wrong_re.sub(correct.replace("\\", "\\\\"), body)
                count += n

        # \alfa → \alpha (when followed by space, brace, or letter)
        # Catch patterns like "\alfa dalgası" → "\alpha dalgası"
        alfa_re = re.compile(r"\\alfa\b")
        n = len(alfa_re.findall(body))
        if n:
            body = alfa_re.sub(r"\\alpha", body)
            count += n

        if count > 0:
            report.total_fixes += count
            report.details.append(
                f"Translated LaTeX commands restored ({count}x)"
            )
        return body

    # ------------------------------------------------------------------
    # FIX 6: English word leaks in Turkish text
    # ------------------------------------------------------------------
    # 2026-07-11 (#62): 'and'/'or' kurallari listeden cikarilip satir-bagi
    # kapili _fix_and_or_leak()'e tasindi — lookbehind tek karaktere bakiyor,
    # tamamen Ingilizce satirda ('The History of Rivers and the ...',
    # kaynakca girisleri) 'r and t' gibi dizilimlerde ' ve ' enjekte edip
    # Ingilizce birakildigi BILINCLI metni bozuyordu (HEAD-11 ile ayni sinif).
    _ENGLISH_LEAK_PATTERNS = [
        # NOT: "to" -> "'den" kurali listeden cikarildi; satir-baglami
        # gerektirdigi icin _fix_to_leak() ile ayri ele aliniyor (HEAD-11).
        # "on" before math/variable
        (re.compile(r"\bon\s+(\$[^$]+\$)"), r"üzerindeki \1"),
        # "In Bölüm" → "Bölüm...de"
        (re.compile(r"\bIn\s+(Bölüm|Şekil|Tablo|Denklem)\s+"), r"\1 "),
        # "düşüncel" → "ideal"
        (re.compile(r"\bdüşüncel\b"), "ideal"),
        # "bell" (as in bell nozzle) in Turkish context
        (re.compile(r"\bbell\s+şeklindeki\b"), "çan şeklindeki"),
    ]

    # HEAD-11: " to " -> "'den " yalnizca satirda Turkce baglam (ozel karakter)
    # varsa uygulanir. Tamamen-Ingilizce satirlarda (cevrilmemis baslik/kaynakca,
    # ör. "A Short Guide to Garden Ponds") bu kural garbage uretiyordu.
    _TO_LEAK_RE = re.compile(r"(?<=[A-Za-z}])\s+to\s+(?=[A-Z\\$])")
    _TR_MARKER_CHARS = frozenset("çğıöşüÇĞİÖŞÜ")

    # 2026-07-11 (#62): and/or degisimi yalnizca Turkce-isaretli satirlarda.
    _AND_OR_LEAK_PATTERNS = [
        (re.compile(r"(?<=[a-zçğıöşü])\s+and\s+(?=[A-ZÇĞİÖŞÜa-zçğıöşü0-9\\])"), " ve "),
        (re.compile(r"(?<=[a-zçğıöşü])\s+or\s+(?=[A-ZÇĞİÖŞÜa-zçğıöşü])"), " veya "),
    ]

    # 2026-09-30 (F1, MT-3): Türkçe işaret CÜMLE düzeyinde ve atıf etiketi
    # maskelenerek aranır. Eskiden satırın herhangi bir yerindeki Türkçe harf
    # yetiyordu; çevrilmemiş altyazıda stash'in Türkçeleştirdiği etiket
    # ("\caption*{Şekil 1.2: Sketch of the two orbital rules.}")
    # ya da aynı satırdaki Türkçe başka cümle, İngilizce cümleye ' ve '
    # sokuyordu ("first ve second laws"; bir model denemesinde). Kaynak
    # dilde kalmış cümleye Türkçe sözcük değişimi uygulanmaz; o cümle
    # yeniden çevirinin (retranslator) işidir.
    _LEAK_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

    def _tr_marked_ranges(self, line: str) -> List[Tuple[int, int]]:
        """Satırın Türkçe işaretli CÜMLE aralıkları [(başlangıç, bitiş)].

        Atıf etiketi ("Şekil 1.2", "Tablo 3", "Bölüm 2") Türkçe kanıtı
        sayılmaz (etiket kümesinin tek kaynağı retranslator._mask_xref_labels
        -> translator._FIGURE_LABEL_MAP)."""
        from latex_pipeline.retranslator import _mask_xref_labels
        ranges: List[Tuple[int, int]] = []
        start = 0
        cuts = [(m.start(), m.end())
                for m in self._LEAK_SENTENCE_SPLIT_RE.finditer(line)]
        for end, nxt in cuts + [(len(line), len(line))]:
            if set(_mask_xref_labels(line[start:end])) & self._TR_MARKER_CHARS:
                ranges.append((start, end))
            start = nxt
        return ranges

    def _sub_in_tr_ranges(self, line: str, subs) -> Tuple[str, int]:
        """*subs* ([(desen, yerine)]) yalnız Türkçe işaretli cümlelerde."""
        if not (set(line) & self._TR_MARKER_CHARS):
            return line, 0
        total = 0
        # Sağdan sola: önceki aralıkların konumu kaymaz.
        for s, e in reversed(self._tr_marked_ranges(line)):
            seg = line[s:e]
            for pat, repl in subs:
                seg, n = pat.subn(repl, seg)
                total += n
            line = line[:s] + seg + line[e:]
        return line, total

    def _fix_and_or_leak(self, body: str) -> Tuple[str, int]:
        """' and '/' or ' -> ' ve '/' veya ' YALNIZCA Turkce-isaretli cumlelerde.

        Tamamen Ingilizce satirlar (kaynakca, cevrilmeden birakilmis alinti
        basliklar) ve Turkce satirdaki Ingilizce kalmis cumle dokunulmadan
        kalir; onlar untranslated olarak retranslator/QC'nin isi.
        """
        counter = 0
        lines = body.split("\n")
        for i, ln in enumerate(lines):
            new, line_n = self._sub_in_tr_ranges(ln, self._AND_OR_LEAK_PATTERNS)
            if line_n:
                lines[i] = new
                counter += line_n
        return "\n".join(lines), counter

    def _fix_to_leak(self, body: str) -> Tuple[str, int]:
        """Convert ' to ' -> "'den " ONLY in sentences with Turkish markers.

        Returns (new_body, replacements_made).
        """
        counter = 0
        lines = body.split("\n")
        for i, ln in enumerate(lines):
            new, line_n = self._sub_in_tr_ranges(
                ln, [(self._TO_LEAK_RE, "'den ")])
            if line_n:
                lines[i] = new
                counter += line_n
        return "\n".join(lines), counter

    def _fix_english_leaks(
        self, body: str, report: "SanitizeReport"
    ) -> str:
        """Fix English words leaked into Turkish translation."""
        count = 0
        for pattern, replacement in self._ENGLISH_LEAK_PATTERNS:
            matches = len(pattern.findall(body))
            if matches:
                body = pattern.sub(replacement, body)
                count += matches
        # HEAD-11: context-gated " to " rule (see _fix_to_leak)
        body, to_count = self._fix_to_leak(body)
        count += to_count
        # 2026-07-11 (#62): satir-kapili and/or degisimi
        body, ao_count = self._fix_and_or_leak(body)
        count += ao_count
        if count > 0:
            report.total_fixes += count
            report.details.append(
                f"English word leaks fixed ({count}x)"
            )
        return body

    # ------------------------------------------------------------------
    # FIX 7: Remove stray \tableofcontents from body
    # ------------------------------------------------------------------
    def _remove_stray_toc(
        self, body: str, report: "SanitizeReport"
    ) -> str:
        """Remove \tableofcontents that shouldn't be in the body.

        The converter sometimes leaves a stray \\tableofcontents at the
        end of translated_body.tex.  This produces an empty/wrong TOC
        page in the compiled PDF.  We strip it here.

        ÖNEMLİ (2026-07-06 sutton smoke bulgusu): converter, ön madde →
        ana madde geçişinde BİLİNÇLİ olarak ``\\tableofcontents`` +
        ``\\mainmatter`` çifti üretir (converter.py) ve template'te ayrıca
        TOC yoktur. Eski koşulsuz silme, kitabın otomatik İçindekiler'ini
        tamamen yok ediyordu. Artık yalnızca ``\\mainmatter`` izlemeyen
        (gerçekten başıboş) olanlar silinir.
        """
        import re
        toc_re = re.compile(
            r"\\tableofcontents(?![a-zA-Z])(?!\s*\\mainmatter)\s*"
        )
        count = len(toc_re.findall(body))
        if count:
            body = toc_re.sub("", body)
            report.total_fixes += count
            report.details.append(
                f"Stray \\tableofcontents removed ({count}x)"
            )
        return body

    # ------------------------------------------------------------------
    # B21: TOC tabular sütun genişliği koruması
    # ------------------------------------------------------------------
    def _fix_toc_tabular_widths(self, body: str) -> str:
        """B21: TOC gibi 4-5 sütunlu ``{l l l l}`` tabular'ların uzun başlık
        sütunu sayfayı taşmasın diye ``p{80mm}`` ile sınırlandırılır.

        Heuristik:
        - Spec yalnızca ``l`` / ``r`` / ``c`` karakterlerinden oluşmalı
          (``p{}``, ``||``, ``@{...}`` varsa dokunulmaz).
        - Sütun sayısı 3-6 arasında olmalı (küçük tablolar ve geniş grid'ler
          dokunulmaz).
        - Penultimate sütun (tipik başlık) ``p{80mm}``, son sütun (tipik
          sayfa no) ``r`` yapılır.
        - 2026-09-24: tablo gövdesi gerçekten içindekiler olmalı
          (``_looks_like_toc_table``: son sütun artan sayfa numaraları +
          satırda başlık metni). Yalnız spec'e bakan eski hali veri
          tablolarını da genişletip yerleşimi bozuyordu (bir veri tablosu).
        """
        def _replace(match: re.Match) -> str:
            spec = match.group(1).strip()
            cols = spec.split()
            if not cols:
                return match.group(0)
            if not all(c in ("l", "r", "c") for c in cols):
                return match.group(0)
            if len(cols) < 3 or len(cols) > 6:
                return match.group(0)
            end = _TABULAR_END_RE.search(match.string, match.end())
            table_body = match.string[match.end():end.start() if end else None]
            if not _looks_like_toc_table(table_body):
                return match.group(0)
            new_cols = list(cols)
            # B21+: \raggedright + arraybackslash kritik — onsuz p{} kolonu
            # justify modunda kalır, uzun TR başlıklar overfull \hbox üretip
            # marjın dışına taşar (Berke "clip" olarak gözlemlemişti).
            # 80mm → 70mm: 4-5 kolonlu TOC'ta toplam textwidth aşımını önler.
            new_cols[-2] = ">{\\raggedright\\arraybackslash}p{70mm}"
            new_cols[-1] = "r"
            return "\\begin{tabular}{" + " ".join(new_cols) + "}"

        return _TOC_TABULAR_RE.sub(_replace, body)

    # ------------------------------------------------------------------
    # V1 denetimi (2026-09-29): sayfadan taşan tablo
    # ------------------------------------------------------------------
    def _fit_wide_tables(self, body: str, report: SanitizeReport) -> str:
        """Doğal genişliği metin genişliğini aşan ``{l l …}`` tabloyu sayfaya
        sığdır.

        ``l``/``r``/``c`` sütunu kırılmaz: EPUB'un anlatı tablosu (Verne:
        ~110 karakterlik hücre) sayfanın sağından ~290 pt taşıyor, sağdaki
        sütunlar PDF'te hiç görünmüyordu (kayıpsızlık). Genişlik karakter
        sayısıyla kestirilir (_TABLE_FIT_MAX_CHARS). Aday tabloda
        (1) en uzun hücresi _TABLE_WRAP_MIN_CHARS'ı aşan sütun kırılan
        ``p{…\\linewidth}`` olur (sayısal/kısa sütun olduğu gibi kalır);
        (2) tablo şablonun ``kcfit`` ortamına alınır: TeX'in ölçtüğü genişlik
        yine \\linewidth'i aşıyorsa (çok sütunlu sayı ızgarası) tablo sığacak
        kadar küçültülür, aşmıyorsa dokunulmaz. Spec'i yalnız l/r/c olmayan
        (p{}, |, @{}) ya da kod örneği içindeki tabloya dokunulmaz (B21
        İçindekiler tablosunu önceden p{70mm} yapar).
        """
        code_spans = [(c.start(), c.end()) for rx in (_CODE_ENV_RE, _VERB_RE)
                      for c in rx.finditer(body)]
        out: List[str] = []
        pos = 0
        fixed = 0
        for match in _TOC_TABULAR_RE.finditer(body):
            if match.start() < pos:
                continue
            if any(s <= match.start() < e for s, e in code_spans):
                continue
            spec = re.sub(r"\s+", "", match.group(1))
            if not re.fullmatch(r"[lrc]+", spec):
                continue
            end = _TABULAR_END_RE.search(body, match.end())
            if end is None:
                continue
            table_body = body[match.end():end.start()]
            if "\\begin{tabular" in table_body:
                continue
            ncols = len(spec)
            widths = [0] * ncols
            for row in re.split(r"\\\\", table_body):
                row = _TABLE_RULE_CMD_RE.sub("", row)
                if not row.strip():
                    continue
                cells = re.split(r"(?<!\\)&", row)
                for j, cell in enumerate(cells[:ncols]):
                    widths[j] = max(widths[j], _cell_display_len(cell))
            if sum(widths) + _TABLE_COLSEP_CHARS * ncols <= _TABLE_FIT_MAX_CHARS:
                continue
            cols = list(spec)
            wrap = [j for j in range(ncols)
                    if widths[j] >= _TABLE_WRAP_MIN_CHARS]
            if wrap:
                keep = sum(widths[j] + _TABLE_COLSEP_CHARS
                           for j in range(ncols) if j not in wrap)
                avail = max(
                    _TABLE_FIT_MAX_CHARS - keep - _TABLE_COLSEP_CHARS * len(wrap),
                    _TABLE_WRAP_MIN_CHARS * len(wrap))
                total = sum(widths[j] for j in wrap)
                for j in wrap:
                    frac = max(avail * widths[j] / total / _TABLE_FIT_MAX_CHARS,
                               _TABLE_WRAP_MIN_FRAC)
                    cols[j] = (">{\\raggedright\\arraybackslash}"
                               f"p{{{frac:.2f}\\linewidth}}")
            out.append(body[pos:match.start()])
            out.append("\\begin{kcfit}\\begin{tabular}{" + " ".join(cols) + "}")
            out.append(body[match.end():end.end()])
            out.append("\\end{kcfit}")
            pos = end.end()
            fixed += 1
        if not fixed:
            return body
        out.append(body[pos:])
        report.details.append(
            f"Wide table fitted to page width ({fixed}x)")
        logger.info("Sayfadan taşan %d tablo sayfa genişliğine sığdırıldı "
                    "(uzun sütun p{}, gerekirse küçültme).", fixed)
        return "".join(out)

    # ------------------------------------------------------------------
    # B7: Math balance check (WARN-only, autofix YOK)
    # ------------------------------------------------------------------
    def _check_math_balance(self, body: str) -> List[Dict]:
        """B7: Açılmamış/kapanmamış ``$`` tespit et. Autofix YOK — raporlar.

        Paragraf bazlı (çift ``\\n`` arası) tek ``$`` sayısını kontrol eder.
        Legitimate math bloklarının ``_stash_protected`` ile __SANSTASH_N__
        placeholder'larına çevrildiği varsayılır; bu yüzden geriye kalan
        ``$`` karakterleri "rogue" (açılmamış / kapanmamış) demektir.

        Kurallar:
        - ``\\$`` (escape edilmiş dolar) sayılmaz.
        - ``$$...$$`` display math paragraf dengesini bozmaz (çift olarak çıkarılır).
        - Geriye kalan tek ``$`` sayısı tek ise paragraf dengesizdir → WARN.

        Parameters
        ----------
        body : str
            Sanitize edilmiş (stash edilmiş) LaTeX gövdesi.

        Returns
        -------
        list of dict
            Her sorun için ``{"line_start", "context", "single_count",
            "paragraph_preview"}`` alanlarını içeren sözlük listesi.
        """
        issues: List[Dict] = []
        paragraphs = re.split(r"\n\s*\n", body)
        cumulative_line = 0
        for para in paragraphs:
            # Escape edilmiş \$'ları önce maskele
            masked = re.sub(r"\\\$", "\x00", para)
            # $$ display math — çift olarak çıkar
            masked_no_dd = re.sub(r"\$\$", "", masked)
            single_count = masked_no_dd.count("$")
            if single_count % 2 != 0:
                first_dollar_idx = masked_no_dd.find("$")
                start = max(0, first_dollar_idx - 30)
                end = first_dollar_idx + 100
                context = masked_no_dd[start:end].replace("\x00", r"\$")
                issues.append({
                    "line_start": cumulative_line,
                    "context": context[:200],
                    "single_count": single_count,
                    "paragraph_preview": para[:100],
                })
            cumulative_line += para.count("\n") + 2
        return issues

    def preflight_math_check(self, body: str) -> bool:
        """B7: Compile öncesi math balance preflight (WARN-only).

        Sadece raporlama yapar, autofix uygulamaz. Her zaman ``True``
        döner — hatalı olsa dahi pipeline durdurulmaz, sadece log'a
        düşer ki operatör görebilsin.
        """
        issues = self._check_math_balance(body)
        if issues:
            logger.warning(
                "=== B7 math balance preflight: %d sorun ===",
                len(issues),
            )
            for i, issue in enumerate(issues[:5]):
                logger.warning(
                    "  [%d] line~%d: %s",
                    i + 1,
                    issue["line_start"],
                    issue["context"][:120],
                )
            if len(issues) > 5:
                logger.warning("  ... ve %d tane daha", len(issues) - 5)
        return True


# ---------------------------------------------------------------------------
# QC paritesi (2026-09-24): sanitizer'ın metne eklediği yapıların QC'de
# "kayıp/fazlalık" sayılmaması için QC, karşılaştırmadan önce kaynak ve
# çeviriye AYNI dönüşümü uygular. Kural tanımları burada tek yerde durur.
# ---------------------------------------------------------------------------

def apply_text_mode_math_wraps(text: str) -> str:
    """Metin modundaki Yunan harfi ve eksik-glif sembollerini sanitizer'ın
    yaptığı gibi ``$...$`` içine alır (math/ref korumalı, idempotent).

    QC ``inline_math_loss`` sayımı bunu HER İKİ tarafa uygular: sanitizer
    yalnız çeviriyi sarmaladığı için kaynaktaki çıplak "θ" / "≫" eskiden
    çeviride "fazladan inline math" diye raporlanıyordu (bir test kitabında
    +10).
    """
    if not text:
        return text or ""
    body, stash = _stash_protected(text)
    body = LatexSanitizer()._fix_text_mode_wraps(body, SanitizeReport())
    return _restore_stash(body, stash)


# ---------------------------------------------------------------------------
# Uzun belirteçlerde satır kırılma noktası (5 Ekim 2026; V1 bulgusu)
# ---------------------------------------------------------------------------
# Alt çizgiyle bağlanmış uzun belirteçler ("qq\_ADIM\_YASAK\_KUTU\_…"),
# düzenli ifadeler ("\textasciicircum{}qq(?:ALFA|BETA|…)") ve yollar LaTeX'te
# kırılamadığı için sayfa dışına taşıyordu (makine dizgisi alıntılayan bir
# raporun gerçek akış EXPORT'unda 37 Overfull \hbox, en çok 341 pt). Boşluksuz
# uzun belirteçte her "\_", "|" ve "/" ardına "\allowbreak{}" konur; basılan
# metin değişmez.
# Kapsam dışı: matematik (satır içi ve ortamlar), kod ortamları, \verb,
# \href/\url adresleri, etiket/atıf/dosya argümanları, tablo sütun tanımları
# ("{|l|c|}") ve preamble.
UZUN_BELIRTEC_EN_AZ = 24   # karakter; daha kısa belirteç satıra sığar
UZUN_BELIRTEC_EN_AZ_KIRILMA = 2
_UZUN_BELIRTEC_RE = re.compile(
    r"(?:[A-Za-z0-9\[\].:#@?*+()=,/|-]|\\[_{}$&%#]"
    r"|\\text(?:asciicircum|backslash|asciitilde)\{\})+")
_BELIRTEC_KIRILMA_RE = re.compile(r"\\_|\||/")
# Argümanı metin olan komutlar: içlerindeki belirteç kırılabilir. Öteki her
# komutun argümanları korunur (dosya yolu: \includepdf, \input; etiket/atıf;
# tablo sütun tanımı \begin{tabular}{|l|c|}; başlık/altyazı gibi taşınan
# argümanlar).
_METIN_ARGUMANLI_KOMUTLAR = (
    "textbf|textit|texttt|textsf|textrm|textsc|textup|textsl|textmd|textnormal"
    "|emph|underline|uline|sout|footnote|footnotetext|mbox|text|textsuperscript"
    "|textsubscript|textasciicircum|textbackslash|textasciitilde|textbar")
_BELIRTEC_KORUMA_RE = re.compile(
    r"\\begin\{(?P<kod>verbatim|Verbatim|lstlisting|minted|comment"
    r"|equation|align|alignat|gather|multline|flalign|eqnarray|displaymath|math)"
    r"(?P<yildiz>\*?)\}.*?\\end\{(?P=kod)(?P=yildiz)\}"
    r"|\\verb\*?(?P<vd>[^A-Za-z\s*]).*?(?P=vd)"
    r"|\\lstinline(?:\[[^\]]*\])?(?:\{[^{}]*\}|(?P<ld>[^A-Za-z\s{\[]).*?(?P=ld))"
    r"|\\(?!(?:" + _METIN_ARGUMANLI_KOMUTLAR + r")(?![A-Za-z]))[A-Za-z@]+\*?"
    r"(?:\s*\[[^\]]*\])*(?:\s*\{(?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*\})+"
    r"|\\\[.*?\\\]|\\\(.*?\\\)|(?<!\\)\$\$.*?(?<!\\)\$\$|(?<!\\)\$[^$]*(?<!\\)\$",
    re.DOTALL)


def uzun_belirtec_kirilimi(metin: str) -> str:
    r"""Uzun boşluksuz belirteçlere kırılma noktası koyar (idempotent; bkz. yukarı)."""
    if "\\_" not in metin and "|" not in metin and "/" not in metin:
        return metin
    bas = metin.find("\\begin{document}")
    on, govde = (metin[:bas], metin[bas:]) if bas >= 0 else ("", metin)

    def _kir(m: "re.Match[str]") -> str:
        b = m.group(0)
        if (len(b) < UZUN_BELIRTEC_EN_AZ
                or len(_BELIRTEC_KIRILMA_RE.findall(b)) < UZUN_BELIRTEC_EN_AZ_KIRILMA):
            return b
        return _BELIRTEC_KIRILMA_RE.sub(lambda k: k.group(0) + "\\allowbreak{}", b)

    parcalar, son = [], 0
    for k in _BELIRTEC_KORUMA_RE.finditer(govde):
        parcalar.append(_UZUN_BELIRTEC_RE.sub(_kir, govde[son:k.start()]))
        parcalar.append(k.group(0))
        son = k.end()
    parcalar.append(_UZUN_BELIRTEC_RE.sub(_kir, govde[son:]))
    yeni = "".join(parcalar)
    # İdempotent: önceki geçişin koyduğu kırılma noktası yinelenmez.
    while "\\allowbreak{}\\allowbreak{}" in yeni:
        yeni = yeni.replace("\\allowbreak{}\\allowbreak{}", "\\allowbreak{}")
    return on + yeni
