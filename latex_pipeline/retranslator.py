"""
Post-translation scan & retranslate module.

Scans a translated .tex file line-by-line, detects lines that remain
in English (untranslated), and re-translates them using a provided
translator function.  Math environments, LaTeX structural commands,
and bibliography blocks are never touched.

Usage:
    from latex_pipeline.retranslator import scan_and_retranslate

    result = scan_and_retranslate(
        tex_content,
        translator_fn=lambda text: my_translator.translate(text),
    )
    # result.output_tex  -> retranslated content
    # result.retranslated_lines -> list of (line_num, old, new) tuples

Integration point: after autofix, before compile in the pipeline.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result data structure
# ---------------------------------------------------------------------------

@dataclass
class RetranslateResult:
    """Summary of the retranslation pass."""
    total_lines: int = 0
    scanned_lines: int = 0          # Lines actually checked (non-math, non-command)
    english_lines_found: int = 0    # Lines detected as English
    retranslated_lines: int = 0     # Lines successfully retranslated
    failed_lines: int = 0           # Lines where retranslation failed
    skipped_math: int = 0           # Lines skipped (inside math env)
    skipped_command: int = 0        # Lines skipped (pure LaTeX command)
    skipped_short: int = 0          # Lines skipped (too short to judge)
    heading_case_fixes: int = 0     # Headings sentence-cased in final pass
    output_tex: str = ""            # The retranslated content
    changes: List[Tuple[int, str, str]] = field(default_factory=list)
    # Each change: (1-based line number, old_text, new_text)


# ---------------------------------------------------------------------------
# Math environment detection (same patterns as autofix.py -- proven)
# ---------------------------------------------------------------------------

_MATH_ENV_NAMES = (
    r"equation\*?|align\*?|gather\*?|multline\*?|split|"
    r"flalign\*?|alignat\*?|eqnarray\*?"
)
_MATH_ENV_RE = re.compile(
    r"\\begin\{(" + _MATH_ENV_NAMES + r")\}"
    r"(.*?)"
    r"\\end\{\1\}",
    re.DOTALL,
)
# Display matematik ('\[…\]', '$$…$$'): _find_protected_spans TeX okuma
# sırasıyla bulur (core.lang_detect.bracket_display_spans / dollar_math_spans).
_INLINE_MATH_RE = re.compile(r"(?<![\$\\])\$(?!\$)(.+?)(?<![\$\\])\$(?!\$)")

# Code environments
_CODE_ENV_RE = re.compile(
    r"\\begin\{(lstlisting|verbatim|minted)\}"
    r"(.*?)"
    r"\\end\{\1\}",
    re.DOTALL,
)

# Bibliography block
_BIBLIOGRAPHY_ENV_RE = re.compile(
    r"\\begin\{thebibliography\}.*?\\end\{thebibliography\}",
    re.DOTALL,
)

# Tabular environments (tables should not be retranslated)
_TABULAR_ENV_RE = re.compile(
    r"\\begin\{(tabular|tabularx|longtable)\}\s*(?:\{[^}]*\})?"
    r"(.*?)"
    r"\\end\{\1\}",
    re.DOTALL,
)

# ---------------------------------------------------------------------------
# Env-name protection for retranslation (session 5 fix -- 2026-03-03)
# ---------------------------------------------------------------------------
# When retranslating a line, MT may translate \begin{figure} -> \begin{sekil}.
# We stash \begin{...} and \end{...} tokens with safe placeholders so MT
# cannot touch the environment names.

_ENV_CMD_RE = re.compile(r"\\(begin|end)\{([^}]+)\}")
_RT_ENV_PLACEHOLDER = "\uE101RTENV_{}\uE101"


def _stash_env_names(line: str) -> Tuple[str, List[Tuple[str, str]]]:
    """Replace \\begin{name}/\\end{name} with safe placeholders.

    Returns (modified_line, list_of_(placeholder, original)).
    """
    env_stash: List[Tuple[str, str]] = []

    def _repl(m: re.Match) -> str:
        idx = len(env_stash)
        original = m.group(0)
        placeholder = _RT_ENV_PLACEHOLDER.format(idx)
        env_stash.append((placeholder, original))
        return placeholder

    modified = _ENV_CMD_RE.sub(_repl, line)
    return modified, env_stash


def _restore_env_names(line: str, env_stash: List[Tuple[str, str]]) -> str:
    """Restore env name placeholders in the retranslated line."""
    for placeholder, original in env_stash:
        if placeholder in line:
            line = line.replace(placeholder, original, 1)
        else:
            # MT mangled the placeholder -- try to recover
            # Strip the sentinel chars and search for remnants
            bare = placeholder.replace("\uE101", "")
            if bare in line:
                line = line.replace(bare, original, 1)
            else:
                # Placeholder completely lost -- append at end
                logger.warning(
                    "Retranslator: env placeholder %s lost, appending original.",
                    original,
                )
                line = line.rstrip() + " " + original
    return line


# ---------------------------------------------------------------------------
# Post-retranslation prompt leak guard (session 5 fix -- 2026-03-03)
# ---------------------------------------------------------------------------
# When MT retranslates a line, it may inject new prompt leakage.
# We import the prompt leak patterns from autofix and clean the output.

_POST_RT_PROMPT_LEAK_PATTERNS: Optional[List[re.Pattern]] = None


def _get_prompt_leak_patterns() -> List[re.Pattern]:
    """Lazy-load prompt leak patterns from autofix module."""
    global _POST_RT_PROMPT_LEAK_PATTERNS
    if _POST_RT_PROMPT_LEAK_PATTERNS is None:
        try:
            # Retranslator YALNIZ Yol B'de (MT çıktısı) koşar — tam liste
            # ("Çeviri/Çevirmen notu" MT-only kalıpları dahil) doğrudur.
            from latex_pipeline.patterns import ALL_PROMPT_LEAK_PATTERNS as _PROMPT_LEAK_PATTERNS
            _POST_RT_PROMPT_LEAK_PATTERNS = _PROMPT_LEAK_PATTERNS
        except ImportError:
            _POST_RT_PROMPT_LEAK_PATTERNS = []
    return _POST_RT_PROMPT_LEAK_PATTERNS


def _clean_prompt_leaks(text: str, source: Optional[str] = None) -> str:
    """Remove prompt leak patterns from retranslated text.

    G2a-2 (2026-10-05): istem işareti (<<<TRANSLATE_START>>> ve bozuk
    biçimleri, ``translator.strip_prompt_markers``) önce temizlenir; her
    sızıntı günlüğe yazılır (eskiden sessiz siliniyordu).

    FX3 (2026-10-05, V2 Y2): *source* (yeniden çevrilen kaynak satır)
    verilirse kaynakta geçen biçim meşru içeriktir ("<translate>" etiketi
    anlatan satır): kaynaktaki sayısı kadarı korunur, fazlası silinir."""
    from latex_pipeline.translator import strip_prompt_markers, sub_unless_in_source

    text, markers = strip_prompt_markers(text, source)
    if markers:
        logger.warning("Retranslator: İstem işareti sızıntısı temizlendi "
                       "(%d işaret: %s): %.80s", len(markers),
                       ", ".join(markers[:4]), text)
    for pattern in _get_prompt_leak_patterns():
        if pattern.search(text):
            cleaned = sub_unless_in_source(pattern, text, source)
            if cleaned != text:
                logger.warning("Retranslator: istem sızıntısı temizlendi "
                               "(%.60s): %.80s", pattern.pattern, text)
                text = cleaned
    # Remove residual empty content
    text = re.sub(r"^\s*[.,;:!?\-\u2013\u2014]*\s*$", "", text, flags=re.MULTILINE)
    return text.strip()


# EPUB placeholder hallucination pattern (shared with translator.py)
# MT (HY-MT 7B) sometimes hallucinates these into output.
_EPUB_HALLUCINATION_RE = re.compile(
    r"@[A-Za-z]+\d+\b"       # @Bilinmeyen0000, @Unknown123
    r"|@@[A-Z]+(?:\d+@@)?"    # @@KNOWN0001@@, @@NEXT
    r"|[A-Z]+\d+@@"           # KNOWN0001@@ (partial)
)


def _clean_epub_placeholder_hallucinations(text: str, original: str = "") -> str:
    """Remove MT-hallucinated EPUB placeholder artifacts from text.

    HY-MT 7B sometimes hallucinates EPUB-style placeholders like
    @Bilinmeyen0000, @Unknown123, @@KNOWN0001@@, KNOWN0001@@ into its
    output.  These were never in the source text and are pure artifacts.

    B-5 (2026-07-27): *original* verildiyse KAYNAKTA GERÇEKTEN VAR OLAN
    token'lar korunur — desen geniş (@Ad1234 her şeyi yakalar) ve kaynakta
    meşru olarak geçen bir @token (kullanıcı adı, atıf anahtarı) eskiden
    körlemesine siliniyordu.
    """
    if _EPUB_HALLUCINATION_RE.search(text):
        def _repl(m: "re.Match[str]") -> str:
            tok = m.group(0)
            if original and tok in original:
                return tok  # kaynakta var — halüsinasyon değil
            return ""

        cleaned = _EPUB_HALLUCINATION_RE.sub(_repl, text)
        if cleaned == text:
            return text
        # Collapse extra whitespace from removal
        cleaned = re.sub(r"  +", " ", cleaned).strip()
        if cleaned and len(cleaned) > 3:
            logger.warning(
                "Retranslator: removed EPUB placeholder hallucination(s): "
                "%.80s -> %.80s",
                text[:80], cleaned[:80],
            )
            return cleaned
    return text


def _find_protected_spans(text: str) -> List[Tuple[int, int]]:
    """Return sorted, merged (start, end) spans covering all protected regions.

    Protected regions: math environments, display math, code environments,
    bibliography blocks, tabular environments.
    """
    spans: List[Tuple[int, int]] = []
    for pattern in (
        _MATH_ENV_RE,
        _CODE_ENV_RE,
        _BIBLIOGRAPHY_ENV_RE,
        _TABULAR_ENV_RE,
    ):
        for m in pattern.finditer(text):
            spans.append((m.start(), m.end()))
    # 2026-09-29 (F3-01): display matematik TeX okuma sırasıyla (translator
    # stash'i ve QC ile ORTAK okuyucular). Eski '\$\$.*?\$\$' / '\\\[.*?\\\]'
    # bitişik atomun ortasındaki '$$'yi ve Marker atıf kaçışını ('\[ 63}]')
    # açılış sayıp aradaki çevrilmemiş satırları "math protected" diye
    # tarama dışı bırakıyordu.
    # Kod ortamlarının içi TeX gibi okunmaz ('echo $$' bir PID'dir): okuyucu
    # aynı uzunlukta boşluklarla maskelenmiş metinde koşar.
    from core.lang_detect import bracket_display_spans, dollar_math_spans
    parts: List[str] = []
    pos = 0
    for m in _CODE_ENV_RE.finditer(text):
        parts.append(text[pos:m.start()])
        parts.append(re.sub(r"[^\n]", " ", m.group(0)))
        pos = m.end()
    parts.append(text[pos:])
    masked = "".join(parts)
    spans.extend(bracket_display_spans(masked))
    spans.extend((st, en) for st, en, disp in dollar_math_spans(masked) if disp)

    if not spans:
        return []

    spans.sort()
    merged: List[Tuple[int, int]] = [spans[0]]
    for start, end in spans[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


def _offset_to_line(offset: int, line_offsets: List[int]) -> int:
    """Convert a character offset to a 0-based line index."""
    lo, hi = 0, len(line_offsets) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if line_offsets[mid] <= offset:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _build_protected_line_set(
    text: str, lines: List[str]
) -> set:
    """Return set of 0-based line indices that overlap with any protected span."""
    spans = _find_protected_spans(text)
    if not spans:
        return set()

    # Build line start offsets
    line_offsets: List[int] = []
    pos = 0
    for line in lines:
        line_offsets.append(pos)
        pos += len(line) + 1  # +1 for \n

    protected_lines: set = set()
    for span_start, span_end in spans:
        first_line = _offset_to_line(span_start, line_offsets)
        last_line = _offset_to_line(span_end - 1, line_offsets) if span_end > span_start else first_line
        for i in range(first_line, last_line + 1):
            protected_lines.add(i)

    return protected_lines


# ---------------------------------------------------------------------------
# LaTeX command / structural line detection
# ---------------------------------------------------------------------------

# Lines that are purely LaTeX structural commands (not translatable text)
_PURE_COMMAND_RE = re.compile(
    r"^\s*\\("
    r"begin|end|label|ref|eqref|cite|pageref|cref|autoref|"
    r"includegraphics|centering|caption|"
    r"vspace\*?|hspace\*?|vfill|hfill|"
    r"par|noindent|newline|newpage|clearpage|"
    r"bigskip|medskip|smallskip|"
    r"raggedright|raggedleft|maketitle|tableofcontents|"
    r"listoffigures|listoftables|"
    r"frontmatter|mainmatter|backmatter|appendix|"
    r"chapter\*?|section\*?|subsection\*?|subsubsection\*?|paragraph\*?"
    r")\b"
)

# Lines that are empty or only whitespace
_EMPTY_RE = re.compile(r"^\s*$")

# Lines that are LaTeX comments
_COMMENT_RE = re.compile(r"^\s*%")


_HEADING_CMD_RE = re.compile(
    r"^\s*\\(chapter|section|subsection|subsubsection|paragraph)\*?"
    r"\s*(?:\[[^\]]*\])?"   # optional short title [...]
    r"\s*\{(.+)\}\s*$",     # required {title content}
    re.DOTALL,
)


def _is_structural_line(line: str) -> bool:
    """Check if a line is purely structural LaTeX (no translatable text).

    IMPORTANT: heading commands (\\section, \\chapter, etc.) are NOT structural
    if their {title} contains translatable text.  Only headings with purely
    numeric/empty titles (e.g. ``\\section{2.4}``) are treated as structural.
    """
    stripped = line.strip()
    if not stripped:
        return True
    if stripped.startswith("%"):
        return True
    # Pure begin/end environment
    if re.match(r"^\s*\\(begin|end)\{[^}]+\}\s*(\[.*\])?\s*$", stripped):
        return True

    # --- Heading commands: look INSIDE the braces ---
    heading_m = _HEADING_CMD_RE.match(stripped)
    if heading_m:
        title_content = heading_m.group(2)
        # Strip inline math, LaTeX commands, numbers, punctuation
        cleaned_title = re.sub(r"\$[^$]*\$", " ", title_content)             # inline math
        cleaned_title = re.sub(r"\\[a-zA-Z]+\*?(?:\{[^}]*\})*", " ", cleaned_title)  # cmds
        cleaned_title = re.sub(r"[{}]", " ", cleaned_title)                   # braces
        cleaned_title = re.sub(r"[\d.\-:]+", " ", cleaned_title)              # numbers
        cleaned_title = cleaned_title.strip()
        if len(cleaned_title) < 3:
            # Title is just a number or empty -> structural (e.g. \section{2.4})
            return True
        # Title has real text -> NOT structural, let retranslator check it
        return False

    # Pure command with optional argument (non-heading commands)
    if _PURE_COMMAND_RE.match(stripped):
        # For non-heading commands: strip the command and its args,
        # check if anything substantial remains
        after_cmd = re.sub(r"\\[a-zA-Z]+\*?(\{[^}]*\})*(\[[^\]]*\])*", "", stripped)
        if len(after_cmd.strip()) < 5:
            return True
    return False


# ---------------------------------------------------------------------------
# English detection heuristic
# ---------------------------------------------------------------------------

# Common English function words (articles, prepositions, conjunctions, etc.)
# These are words that are virtually never found in Turkish text.
_ENGLISH_FUNCTION_WORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "shall",
    "should", "may", "might", "can", "could", "must",
    "not", "no", "nor",
    "and", "or", "but", "if", "then", "else", "when", "where", "while",
    "as", "at", "by", "for", "from", "in", "into", "of", "on", "to",
    "with", "that", "this", "these", "those", "it", "its",
    "which", "who", "whom", "whose", "what", "how",
    "each", "every", "all", "both", "few", "more", "most",
    "other", "some", "such", "only", "also", "than",
    "very", "just", "about", "above", "after", "before", "between",
    "through", "during", "without", "within",
    "however", "therefore", "thus", "hence", "although", "because",
    "since", "until", "unless", "whereas",
}

# Technical English terms commonly used in Turkish scientific text -- skip these
_TECHNICAL_TERMS = {
    "batch", "epoch", "loss", "bias", "input", "output", "encoder",
    "decoder", "dropout", "pooling", "embedding", "feature", "weight",
    "layer", "kernel", "stride", "padding", "softmax", "sigmoid",
    "relu", "adam", "sgd", "momentum", "norm", "linear", "dense",
    "attention", "transformer", "gradient", "tensor", "scalar",
    "vector", "matrix", "mesh", "grid", "node", "graph", "operator",
    "network", "neural", "model", "deep", "learning",
    "nozzle", "thrust", "mach", "reynolds", "prandtl", "navier",
    "stokes", "euler", "runge", "kutta", "fourier", "laplace",
    "gauss", "lagrange", "taylor", "newton", "jacobian", "hessian",
    "boltzmann", "planck", "maxwell", "bernoulli",
}

# Turkish-specific characters that never appear in English text
_TURKISH_SPECIAL_CHARS = set("\u00e7\u011f\u0131\u00f6\u015f\u00fc\u00c7\u011e\u0130\u00d6\u015e\u00dc")
# c-cedilla, g-breve, dotless-i, o-dieresis, s-cedilla, u-dieresis (and uppercase)

# Turkish morphological suffixes — O(1) set-based lookup (no regex ReDoS risk).
# Includes ASCII-only variants (i for dotless-i, c for c-cedilla, etc.)
_TURKISH_SUFFIX_SET = {
    # Plural + case suffixes
    "lari", "leri", "ları", "lerı", "ları", "leri",
    "inda", "inde", "ında", "inde",
    "inin", "ının", "ınin",
    "daki", "deki",
    "iyla", "iyle", "ıyla",
    # Verb suffixes
    "maktadir", "mektedir", "maktadır", "mektedır",
    "masi", "mesi", "ması", "mesi",
    "arak", "erek",
    "digi", "dığı", "diği",
    "abil", "ebil",
    "iyor", "ıyor",
    "ecek", "acak",
    "mış", "miş",
    # Aorist — NOT: 3-harfli çıplak ASCII aorist ekleri (ser/sur/cur/cer/car/
    # sir/cir/sar) İngilizce kelimeleri Türkçe sanıyordu (user, browser, occur,
    # officer). RT-01: yalnızca Türkçe-özel-karakterli (ş/ç) ya da 4+ harfli
    # aorist ekleri tutuluyor — bunlar İngilizce köke yapışmaz.
    "şir", "şer", "şar", "şur", "çir", "çer", "çar", "çur",
    "lesir", "leşir", "lesır", "leşır", "leser",
    "lanir", "lanır", "lenir",
    "larina", "larine", "lerinde",
    # Copula / be-verb
    "dir", "dır", "tir", "tır", "dur", "dür", "tur", "tür",
    # Common postpositions / conjunctions
    "icin", "için", "veya", "olan", "gibi", "bile",
    "degil", "değil", "ancak",
    "olarak", "sekilde", "şekilde", "uzere", "üzere",
    "kadar", "sonra", "once", "önce",
}


def _has_turkish_suffix(word: str) -> bool:
    """Check if a word ends with a Turkish suffix using O(1) set lookup."""
    w = word.lower()
    for suffix_len in range(3, min(10, len(w)) + 1):
        if w[-suffix_len:] in _TURKISH_SUFFIX_SET:
            return True
    return False


# 2026-09-24: İngilizce'ye ÖZGÜ yazım izleri. Türkçe (yerli + yaygın bilim
# alıntıları dahil) bu harf/dizileri kullanmaz: w/x/q harfleri, th/sh/ch/ph/
# wh/ck/gh ikilileri, ou/ie/au/oa ünlü çiftleri, -tion/-ity/-ing/-ical/-ies/
# -ial/-ous/-ness/-ly/-ed sonekleri. Türkçe alıntılarda görülen ee/oo/ea/ai
# (lineer, koordinat, ideal, daire) BİLEREK dışarıda.
_EN_ORTHO_DIGRAPHS = ("th", "sh", "ch", "ph", "wh", "ck", "gh", "ou", "ie", "au", "oa")
_EN_ORTHO_SUFFIXES = ("tion", "tions", "sion", "sions", "ing", "ness", "ity",
                      "ous", "ical", "ies", "ial", "ly", "ed")
# (sonek, en az harf): Türkçe karşılıkları farklı yazılır (-ence/-ance ->
# -ans, -ic -> -ik, -ive -> -if, -age -> -aj, -ure -> -ür). Uzunluk eşiği
# "bence"/"sence" (5) ve "saate" (5) gibi Türkçe çakışmaları dışarıda tutar.
# 2026-09-24: ("ment", 8) — Türkçe -ment'i -man yazar (apartman, departman,
# fragman, argüman); 8 harf eşiği Türkçede de kullanılan "moment",
# "segment", "element"i dışarıda tutar. Gövde kapısı "Risk management ve
# politika optimizasyonu." gibi yarım çeviriyi bu izle yakalar.
_EN_ORTHO_SUFFIXES_LONG = (
    ("ence", 6), ("ance", 6), ("ic", 5), ("ive", 5), ("age", 5), ("ure", 5),
    ("ate", 6), ("ment", 8),
)
_VOWELS_ASCII = frozenset("aeiou")


def _has_english_orthography(word: str) -> bool:
    """Kelimede İngilizce'ye özgü bir yazım izi var mı? (sözlüksüz)

    Salt-ASCII Türkçe başlığı ("Potansiyel Enerji", "Konik Kesitler")
    İngilizce'den ayırmak için kullanılır: İngilizce başlıkların hemen hepsi
    en az bir iz taşır ("potential" -ial, "sections" -tion, "path" th,
    "frames" çoğul -s, "reference" -ence, "energy" ünsüz+y), ASCII Türkçe
    kelimeler taşımaz (Türkçe'de sözcük sonu ünsüz+y yoktur, -ik/-ans/-if
    yazılır, w/x/q harfleri yoktur).
    """
    w = word.lower().strip(".,;:!?()[]\"'")
    if len(w) < 3 or not w.isascii() or not w.isalpha():
        return False
    if any(c in w for c in "wxq"):
        return True
    if any(d in w for d in _EN_ORTHO_DIGRAPHS):
        return True
    if any(w.endswith(s) for s in _EN_ORTHO_SUFFIXES):
        return True
    if any(len(w) >= n and w.endswith(s) for s, n in _EN_ORTHO_SUFFIXES_LONG):
        return True
    # Sözcük sonu ünsüz + y (energy, theory, trajectory, body): Türkçe'de
    # son "y" hep ünlüden sonra gelir (ay, köy, huy).
    if w.endswith("y") and w[-2] not in _VOWELS_ASCII:
        return True
    # Çoğul -s (frames, orbits, limits): 5+ harf; -is/-us/-os/-as/-ns ile
    # biten Türkçe alıntılar (matris, bonus, kaos, referans, frekans) dışarıda
    # (-tions/-sions çoğulları yukarıdaki sonek listesinde).
    if len(w) >= 5 and w.endswith("s") and w[-2] not in "iuoasn":
        return True
    return False

# Common Turkish words (ASCII-friendly) that reliably indicate Turkish text
# even without special characters.  These words rarely appear in English.
_TURKISH_COMMON_WORDS = {
    "bir", "ve", "ile", "bu", "olan", "icin", "ise", "veya", "ancak",
    "daha", "gibi", "kadar", "sonra", "once", "ama", "fakat", "hem",
    "her", "iki", "arasinda", "olarak", "uzerinde", "altinda",
    "nedeniyle", "acisindan", "boyunca", "dolayi", "sayesinde",
    "tarafindan", "dogrudan", "genellikle", "ozellikle", "dolayisiyla",
    "durumda", "durumunda", "sekilde", "bicimde",
    # Common verbs and verb forms
    "yapilir", "edilir", "olur", "bulunur", "kullanilir", "gorulur",
    "belirlenir", "hesaplanir", "elde", "etmek", "olmak", "yapmak",
    "gerceklesir", "meydana", "gelir", "olusur", "saglar", "gerekir",
    "arttirir", "azaltir", "degisir", "gosterir", "belirtir",
    "neden", "olabilir", "edilebilir", "gerekmektedir",
    # Common Turkish nouns / adjectives (ASCII forms)
    "ortalama", "hata", "girdi", "cikti", "boyutu", "boyut",
    "vektoru", "vektorun", "vektorunun", "matris", "fonksiyon",
    "kare", "mutlak", "toplam", "sonuc", "deger", "degeri",
    "basarili", "basarisiz", "dogru", "yanlis", "kucuk", "buyuk",
    "yuksek", "dusuk", "sabit", "degisken", "parametre",
    # Technical Turkish commonly found in translated scientific text
    "denklem", "denklemleri", "fonksiyon", "fonksiyonu",
    "basinci", "hizi", "sicakligi", "akisi", "orani",
    "bolumde", "bolumunde", "bolumdeki",
    "nozulun", "nozulde", "nozulu",  # nozzle-related
    "arasindaki", "uzerindeki",
}


# ---------------------------------------------------------------------------
# English heading-word detection (module-level -- B-2 / RT-03)
# ---------------------------------------------------------------------------
# Common English content words that appear in untranslated headings.  Hoisted
# to module scope so the set is built once (was rebuilt on every call) and so
# BOTH the Turkish-marker branch and the no-marker branch can use it for
# headings (RT-03: fully-English headings like "DEEP LEARNING" were escaping).
_ENGLISH_HEADING_WORDS = {
    "orthogonal", "vectors", "bases", "matrix", "matrices",
    "linear", "algebra", "spaces", "space", "equations",
    "systems", "values", "decomposition", "product", "products",
    "inner", "outer", "projection", "projections",
    "transformation", "transformations", "independent",
    "dependence", "independence", "symmetric", "definite",
    "least", "squares", "change", "forms", "process",
    "introduction", "conclusion", "summary", "background",
    "results", "discussion", "methods", "approach",
    "overview", "applications", "experiments", "analysis",
    "design", "performance", "properties", "theory",
    "chapter", "section", "appendix", "references",
    "networks", "network", "learning", "training",
    "optimization", "classification", "regression",
    "supervised", "unsupervised", "reinforcement",
    "deep", "neural", "convolutional", "recurrent",
    "generative", "adversarial", "attention", "transformer",
    "gradient", "descent", "backpropagation", "regularization",
    "probability", "statistics", "inference", "bayesian",
    "numerical", "computation", "information",
    "mechanics", "thermodynamics", "propulsion", "transfer",
    "combustion", "waves", "oscillations", "relativity",
    "momentum", "energy", "conservation",
    # Mekanizma ve makine başlık sözcükleri
    "review", "historical", "spiral", "springs", "spring",
    "hairspring", "main", "dynamics", "mechanism",
    "watch", "watches", "clock", "clocks",
    "concluding", "remarks",
    # Genel bilim başlık sözcükleri
    "sensitivity", "averaging", "method", "binary",
    "stars", "mass", "determination", "visual",
    "spectroscopic", "eclipsing",
    # Additional common English heading words
    "model", "models", "function", "functions",
    "algorithm", "algorithms", "structure", "features",
    "estimation", "evaluation", "framework", "general",
    "advanced", "basic", "complex", "simple",
    "experimental", "practical", "abstract",
    # 2026-09-24 (denetim M2#2): yazım izi taşımayan yaygın İngilizce başlık
    # sözcükleri ("Stag Hunt Game", "Multi-agent Team Control", "MLP
    # module"). Türkçe karşılıkları farklı yazılır (oyun, takım, kontrol,
    # modül, cilt, yakıt).
    "game", "games", "team", "control", "module", "volume", "fuel",
}

# Turkish suffixes that attach to English stems via apostrophe, e.g.
# "Springs'nin" -> stem "Springs".  Used to recover the English stem.
_TR_APOSTROPHE_SUFFIXES = {
    "'nin", "’nin",  # possessive
    "'nın", "’nın",
    "'in", "’in",
    "'un", "’un",
    "'ün", "’ün",
    "'sı", "’sı",  # noun suffix
    "'si", "’si",
    "'lar", "’lar", "'ler", "’ler",  # plural
    "'dan", "’dan", "'den", "’den",  # ablative
    "'da", "’da", "'de", "’de",      # locative
    "'na", "’na", "'ne", "’ne",      # dative
    "'nı", "’nı",               # accusative
    "'ni", "’ni",
    "'ya", "’ya", "'ye", "’ye",      # dative alt
}


def _count_known_english_in_heading(words: List[str]) -> int:
    """Count English function/content words in a heading's word list.

    Counts a word if it is an English function word, or if its (apostrophe-
    suffix-stripped) stem is a known English heading content word.  Pure ASCII
    Turkish words (``Dikgen``) are in no English list and are not counted.
    """
    count = 0
    for w in words:
        lower = w.lower().strip(".,;:!?()[]\"'")
        if not lower or len(lower) < 3:
            continue
        if lower in _ENGLISH_FUNCTION_WORDS:
            count += 1
            continue
        # Strip Turkish apostrophe suffixes from an English stem,
        # e.g. "springs'nin" -> "springs".
        stem = lower
        if not lower.isascii():
            for suffix in _TR_APOSTROPHE_SUFFIXES:
                if lower.endswith(suffix):
                    candidate = lower[:len(lower) - len(suffix)]
                    if candidate and candidate.isascii() and candidate.isalpha():
                        stem = candidate
                        break
        if not stem.isascii() or not stem.isalpha():
            continue
        if stem in _ENGLISH_HEADING_WORDS:
            count += 1
    return count


# Başlıkta ek İngilizce kanıtı sayılan işlev sözcükleri.
# _count_known_english_in_heading 3 harften kısa sözcüğü saymadığı için
# "Love is God" kanıtsız kalıyordu. İki harfliler yalnız Türkçe başlıkta
# Türkçe anlamıyla geçmeyenler: "an", "on", "at", "in", "as" Türkçe
# sözcüktür, "no" "numara" kısaltmasıdır. "like" ("Gods Like Men")
# _ENGLISH_FUNCTION_WORDS'te yok; oraya eklemek gövde kurallarını da
# değiştirirdi, bu yüzden yalnız başlık kanıtı.
_EN_HEADING_EXTRA_FUNCTION_WORDS = frozenset({"is", "of", "to", "by", "or", "like"})

# İngilizce listelerde olup salt-ASCII Türkçe başlıkta Türkçe anlamıyla
# geçebilen sözcükler: can (özel ad; ruh), has, had, not ("not almak"),
# spiral, transfer, general. Yazım izi yokken bunlar İngilizce kanıtı
# sayılmaz; "Spiral Yaylar", "Transfer Fonksiyonu", "Can ve Ali"
# İngilizce ilan edilmez. Seçim data/dictionary (EN->TR sözlük) "tr"
# alanında sık geçenlerden yapıldı (24 Eyl 2026). _TECHNICAL_TERMS
# (model, momentum, linear...) de aynı gerekçeyle kanıt sayılmaz.
_TR_ASCII_HOMOGRAPHS = frozenset({
    "can", "has", "had", "not", "spiral", "transfer", "general",
})

# Ek işlev sözcüklerinden salt-ASCII Türkçe başlıkta Türkçe sözcük olarak
# da geçenler: "is" (iş: "Is ve Enerji", "Is Birligi"), "of" (Of ilçesi:
# "Of Tuzlasi"). Başlıkta Türkçe iz varsa bunlar İngilizce kanıtı sayılmaz
# (denetim 2026-09-25, K1b). Gerçek İngilizce başlıklarda ("Love is God",
# "Gods Like Men") Türkçe iz yoktur; output/ ölçümünde ek işlev sözcüğüne
# dayanan dört İngilizce başlığın hiçbiri iz taşımıyor.
_EN_HEADING_TR_HOMOGRAPH_FUNCTION_WORDS = frozenset({"is", "of"})

# Türkçe iz: Türkçe bağlaç/edat ya da ASCII yazılmış iyelik/-lik eki
# ("birligi" <- birliği, "tuzlasi" <- tuzlası, "ilcesi" <- ilçesi).
# Sonekler 5+ harfli sözcükte aranır; output/ altındaki İngilizce
# gövdelerde yalnız "quasi" (q yazım izi zaten İngilizce) ve birkaç özel
# ad ("Lugosi", "Assisi") bu sonekle biter. "ve" İngilizce metinde yalnız
# kesme işaretli biçimde ("I've") geçer; başlık sözcüğü olarak tek başına
# gelmez.
_TR_HEADING_FUNCTION_WORDS = frozenset({
    "ve", "ile", "icin", "için", "bir", "olarak", "gibi", "veya",
    "ama", "fakat", "ancak", "yani",
})
_TR_ASCII_POSSESSIVE_ENDINGS = (
    "ligi", "lugu", "ligu", "lugi", "asi", "esi", "isi", "osi", "usi",
)


def _heading_has_turkish_trace(words: List[str]) -> bool:
    """Salt-ASCII başlıkta Türkçe iz var mı? (bağlaç/edat ya da iyelik eki)"""
    for w in words:
        lower = w.lower().strip(".,;:!?()[]\"'")
        if lower in _TR_HEADING_FUNCTION_WORDS:
            return True
        if (len(lower) >= 5 and lower.isascii() and lower.isalpha()
                and lower.endswith(_TR_ASCII_POSSESSIVE_ENDINGS)):
            return True
    return False


def _heading_has_english_word(words: List[str]) -> bool:
    """Başlıkta bilinen bir İngilizce sözcük var mı? (yazım izi yokken kanıt)

    Bilinen İngilizce işlev/içerik sözcüğü ("and", "also", "above",
    "game") ya da ek işlev sözcüğü ("is", "of", "like"). Türkçede de
    kullanılan sözcükler (_TR_ASCII_HOMOGRAPHS, _TECHNICAL_TERMS) kanıt
    değildir. Salt-ASCII Türkçe başlığın ("Potansiyel Enerji", "Konik
    Kesitler") hiçbir sözcüğü bu listelerde yoktur. Başlıkta Türkçe iz
    varsa "is" (iş) ve "of" (Of) da kanıt değildir ("Is ve Enerji").
    """
    tr_trace = _heading_has_turkish_trace(words)
    candidates = []
    for w in words:
        lower = w.lower().strip(".,;:!?()[]\"'")
        if lower in _TR_ASCII_HOMOGRAPHS or lower in _TECHNICAL_TERMS:
            continue
        if lower in _EN_HEADING_EXTRA_FUNCTION_WORDS:
            if tr_trace and lower in _EN_HEADING_TR_HOMOGRAPH_FUNCTION_WORDS:
                continue
            return True
        candidates.append(w)
    return _count_known_english_in_heading(candidates) >= 1


def _strip_latex_for_language_check(text: str) -> str:
    """Remove LaTeX commands and inline math, leaving only natural language text."""
    # Remove inline math $...$
    clean = _INLINE_MATH_RE.sub("", text)
    # Remove LaTeX commands with arguments: \cmd{...}
    clean = re.sub(r"\\[a-zA-Z]+\*?\s*(?:\{[^}]*\})*(?:\[[^\]]*\])*", " ", clean)
    # Remove remaining backslash commands: \cmd
    clean = re.sub(r"\\[a-zA-Z]+\*?", " ", clean)
    # Remove stash placeholders
    clean = re.sub(r"<<LATEX_\d+>>", " ", clean)
    # Remove braces
    clean = re.sub(r"[{}]", " ", clean)
    # Collapse whitespace
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean


def _clean_for_language_check(text: str, is_heading: bool = False) -> str:
    r"""Natural-language text used for length + English detection.

    For a heading the {title} content is extracted first: the generic command
    remover in :func:`_strip_latex_for_language_check` would otherwise strip a
    bare ``\section{Deep Learning}`` down to an empty string (command eats its
    own argument), causing the scan loop to drop English headings as "too
    short" before they are ever language-checked.  Section numbers ("2.4") are
    stripped too so they don't dilute the word ratio (RT-04).

    Single source of truth so the scan-loop length gate and ``is_english_line``
    never disagree about a heading (RT-C2, within-module).
    """
    if is_heading:
        heading_m = _HEADING_CMD_RE.match(text.strip())
        if heading_m:
            # 2026-09-24: başlık argümanı parantez-dengeli alınır; eski
            # greedy (.+) "\section{X}\label{s}" satırında "\label{s" kuyruğunu
            # başlığa katıp ("s" artığı) dil oranını sulandırıyordu.
            shell = _split_heading_shell(text.strip())
            title_text = shell[1] if shell else heading_m.group(2)
            clean = re.sub(r"\$[^$]*\$", " ", title_text)
            clean = re.sub(r"\\[a-zA-Z]+\*?(?:\{[^}]*\})*", " ", clean)
            clean = re.sub(r"[{}]", " ", clean)
            clean = re.sub(r"[\d.\-:]+", " ", clean)
            clean = re.sub(r"\s+", " ", clean).strip()
            return clean
    return _strip_latex_for_language_check(text)


def _segment_gate_code(text: str, source_lang: str = "en") -> Optional[str]:
    """Çevirmenin MT öncesi segment kapısının kararı (G2a-2, 2026-10-05).

    ``LatexTranslator._foreign_segment_code`` ile AYNI çağrı
    (``core.lang_detect.classify_foreign_segment``): segment kitabın kaynak
    dilinden başka dildeyse (Latince alıntı, Fransızca alıntı, Kiril blok…)
    dil kodu, değilse None. Çevirmen böyle bir segmenti bilerek çevirmeden
    bırakır; retranslator aynı satıra "çevrilmemiş" diyemez (mt okuma §2,
    RAPOR MT-1: bir model testi çıktısında çevirmen Newton/Kepler Latincesine
    "(la), çevrilmeden geçildi" derken retranslator beş satır için
    "English detected" diyordu).
    """
    try:
        from core.lang_detect import classify_foreign_segment
        return classify_foreign_segment(text, source_lang or "en")
    except Exception:
        return None


def is_english_line(
    text: str,
    min_words: int = 4,
    min_length: int = 20,
    is_heading: bool = False,
) -> bool:
    """Determine if a line of text is likely untranslated English.

    G2a-2 (2026-10-05) notu: bu fonksiyon dilin KENDİSİNE bakan ortak
    sezgiseldir; QC (``QCEngine._is_likely_english``) buna delege eder ve
    Latince alıntıyı ancak KAYNAKTA birebir geçiyorsa muaf tutar (model
    uydurması yakalanır; tests/test_fix_F2_20260930.py). Yeniden çeviri
    kararları çevirmenin segment kapısıyla uzlaşan
    :func:`is_untranslated_line` üzerinden verilir.

    Strategy:
    1. Strip LaTeX commands and math -- only look at natural language.
    2. If text is too short, skip (not enough signal).
    3. Check for Turkish-specific characters (s-cedilla, g-breve, dotless-i, etc.)
       If present, text is likely already Turkish.
    4. Check for Turkish morphological suffixes (-lari, -maktadir, -icin, etc.)
       If present, text is likely already Turkish.
    5. Count English function words (the, is, are, which, etc.)
       If ratio is high, text is likely English.

    Parameters
    ----------
    is_heading : bool
        If True, use relaxed thresholds suitable for short heading titles
        (e.g. "The Gram-Schmidt Process" = 4 words but may be shorter
        after LaTeX stripping).

    Returns True if text appears to be untranslated English.
    """
    # Headings are short by nature -- use relaxed thresholds
    if is_heading:
        min_words = min(min_words, 2)
        min_length = min(min_length, 8)

    # For headings, extract title content BEFORE stripping LaTeX commands,
    # because _strip_latex_for_language_check() removes \section{Title} entirely
    # (the heading command eats its own {arg}).  RT-04: section numbers are
    # stripped inside the shared helper.
    clean = _clean_for_language_check(text, is_heading=is_heading)

    if len(clean) < min_length:
        return False

    words = clean.split()
    if len(words) < min_words:
        return False

    # Check for Turkish-specific characters
    has_turkish_char = any(c in _TURKISH_SPECIAL_CHARS for c in clean)

    # Check for Turkish morphological suffixes (O(1) set lookup)
    has_turkish_suffix = any(
        _has_turkish_suffix(w) for w in words if len(w) >= 4
    )

    # Check for common Turkish words (ASCII-friendly)
    turkish_word_count = sum(
        1 for w in words if w.lower().strip(".,;:!?()[]\"'") in _TURKISH_COMMON_WORDS
    )
    has_turkish_words = turkish_word_count >= 2

    # --- Heading English-word detection (RT-03, runs in BOTH branches) ---
    # For headings: count known English words (function + content).  This runs
    # regardless of Turkish markers, so it catches BOTH the mixed heading
    # "Dikgen Vektorler ve Orthogonal Bases" (Turkish markers present) AND the
    # fully-English heading "DEEP LEARNING" / "Attention and Transformer
    # Networks" (no Turkish markers -- previously escaped because technical
    # terms were starved out of the content ratio).  Pure ASCII Turkish words
    # like "Dikgen" are in no English list, so they are not counted.
    if is_heading and _count_known_english_in_heading(words) >= 2:
        return True

    if has_turkish_char or has_turkish_suffix or has_turkish_words:
        # Turkish markers present -- only flag if overwhelmingly English
        # (e.g., one Turkish word in a sea of English)
        en_function_count = sum(
            1 for w in words
            if w.lower() in _ENGLISH_FUNCTION_WORDS
        )
        # 2026-07-11: eski eşik (>=8 + >%50) yalnız NEREDEYSE TAMAMEN
        # İngilizce satırı yakalıyordu; yarısı çevrilmiş karma satır
        # ("...oluşur the properties of the system are preserved")
        # tüm savunma zincirinden görünmez geçiyordu. Eşik: 6+ function
        # word VE >%40 — Türkçe cümle içinde alıntılanan kısa İngilizce
        # kitap adları (2-4 function word) hâlâ muaf.
        if en_function_count >= 6 and en_function_count / len(words) > 0.35:
            return True

        return False

    # No Turkish markers at all -- check English function word ratio
    en_function_count = sum(
        1 for w in words
        if w.lower() in _ENGLISH_FUNCTION_WORDS
    )

    # Also count non-technical English words (alphabetic ASCII, not in technical terms)
    en_content_count = 0
    for w in words:
        lower = w.lower().strip(".,;:!?()[]\"'")
        if not lower:
            continue
        if not lower.isascii() or not lower.isalpha():
            continue
        if lower in _TECHNICAL_TERMS:
            continue
        if len(lower) < 3:
            continue
        en_content_count += 1

    total_words = len(words)

    # Check for author/affiliation lines (should not be retranslated)
    # Pattern: "Name --- Institution, City, State, Country"
    # or just location names like "City, Country"
    # RT-02: a heading is never an author/affiliation line -- skip this guard
    # for headings so Title-Case English titles ("Rocket Propulsion
    # Fundamentals") aren't misread as author names via Pattern 4.
    if not is_heading and _is_author_affiliation_line(clean):
        return False

    # Decision logic:
    # 1. If 3+ English function words AND function words are >25% of total
    #    -> likely English
    if en_function_count >= 3 and en_function_count / total_words > 0.25:
        return True

    # 2026-09-24 (başlık): kural 2/3 "her ASCII kelime İngilizce" sayar;
    # salt-ASCII Türkçe başlık ("Potansiyel Enerji", "Konik Kesitler",
    # "Yorunge Denklemi") İngilizce ilan ediliyor, başlık kabul kapısı
    # doğru MT çevirisini reddedip başlığı İNGİLİZCE bırakıyordu (bir test
    # koşusu). Başlıkta kural 2/3 ancak en az bir kelimede İngilizce'ye özgü
    # yazım izi varsa uygulanır; iz yoksa İngilizce değil.
    # Denetim 2026-09-24 (M2#2): iz şartı tek başına izsiz gerçek İngilizce
    # başlıkları ("Fire and Ice", "Iron and Gold", "Stand Alone")
    # görünmez kılıyordu. İz yoksa bilinen bir İngilizce sözcük de kanıttır.
    if (is_heading
            and not any(_has_english_orthography(w) for w in words)
            and not _heading_has_english_word(words)):
        return False

    # 2026-09-24 (gövde): aynı kusur gövde satırında. Kural 2/3 her ASCII
    # kelimeyi İngilizce sayıyor; Türkçe özel harfi ve tanınan eki olmayan
    # Türkçe cümle ("Birisi bunu elde eder.", "Belirtmekte fayda var ki…")
    # İngilizce ilan edilip yeniden çeviriye gönderiliyor, QC'de
    # "untranslated" oluyordu (bir test koşusunda iki satır). Kural 1'i (işlev
    # sözcükleri) geçemeyen gövde satırında kural 2/3 ancak en az bir
    # İngilizce kanıtı varsa uygulanır: İngilizce'ye özgü yazım izi
    # (_has_english_orthography: w/x/q, th/sh, -tion, -ed, çoğul -s...) ya
    # da iki+ harfli bir İngilizce işlev sözcüğü (tek harfli "a" matematik
    # değişkeni olabilir: "yarıçapı a olan"). Ölçüm (output/ altındaki 13
    # kitabın gövde satırları): kapı 4 TR satırını İngilizce saymaktan
    # çıkardı, dördü de Türkçe ("R, evrensel gaz sabitidir.", "MMD nedir?
    # ..."); EN kaynaklarda kaybedilen tek satır "Area mean diameter =".
    if not is_heading:
        en_evidence = 0
        for w in words:
            lower = w.lower().strip(".,;:!?()[]\"'")
            if (_has_english_orthography(lower)
                    or (len(lower) >= 2 and lower in _ENGLISH_FUNCTION_WORDS)):
                en_evidence += 1
        if en_evidence == 0:
            return False

    if is_heading:
        # 2026-09-24 (başlık): işlev sözcükleri (of/the/an) İngilizce
        # KANITIDIR, kural 2'deki gibi oranı sulandırmaz — "Period of an
        # Elliptical Orbit" 3/5 = 0.6 ile kaçıyordu. İyelik eki de atılır
        # ("Kepler's" -> kepler). Teknik terimler (Laplace, Gauss, vektör)
        # yine kanıt sayılmaz: Türkçe başlıkta da aynen geçerler.
        en_evidence = 0
        for w in words:
            lower = w.lower().strip(".,;:!?()[]\"'")
            lower = re.sub(r"['’]s?$", "", lower)
            if not lower:
                continue
            if lower in _ENGLISH_FUNCTION_WORDS:
                en_evidence += 1
            elif (lower.isascii() and lower.isalpha() and len(lower) >= 3
                    and lower not in _TECHNICAL_TERMS):
                en_evidence += 1
        if en_evidence / max(1, total_words) > 0.6:
            return True

    # 2. If >60% of substantial words are English (non-technical ASCII)
    #    -> likely English
    if en_content_count / max(1, total_words) > 0.6:
        return True

    # 3. Teknik-terim açlığı düzeltmesi (2026-07-11): _TECHNICAL_TERMS'teki
    #    kelimeler content sayacından düşüldüğü için tamamı teknik terimden
    #    oluşan TAM İngilizce cümle ("Orthogonal matrices preserve
    #    eigenvalues and determinants") iki kuraldan da kaçıyordu. Türkçe
    #    işaret ZATEN yok (bu dala ancak öyle gelinir; Türkçe çeviride
    #    teknik kelimeler ek alır: 'matrisler', 'özdeğerleri'). Teknik +
    #    content kelimeler birlikte >%70 ise ve en az 2 teknik terim varsa
    #    İngilizce say.
    en_technical_count = 0
    for w in words:
        lower = w.lower().strip(".,;:!?()[]\"'")
        if lower and lower.isascii() and lower.isalpha() and lower in _TECHNICAL_TERMS:
            en_technical_count += 1
    if (en_technical_count >= 2
            and (en_content_count + en_technical_count) / max(1, total_words) > 0.7):
        return True

    return False


def is_untranslated_line(
    text: str,
    source_lang: str = "en",
    min_words: int = 4,
    min_length: int = 20,
    is_heading: bool = False,
) -> bool:
    """Çeviri çıktısında kaynak dilde kalmış (çevrilmemiş) satır mı?

    Yerel model yolunun kaynak dile göre çalışan dedektörü (K5, KARARLAR 14;
    2026-09-28). Kaynak dil ``core.lang_detect.TEX_SOURCE_LANGS``'ten biridir.

    * ``"en"`` (varsayılan): :func:`is_english_line` AYNEN — İngilizce yol
      birebir aynı kalır (tek karar noktası). Tek istisna (G2a-2,
      2026-10-05): çevirmenin segment kapısının başka bir dile ayırdığı
      gövde satırı (Latince/Fransızca alıntı; :func:`_segment_gate_code`)
      çevrilmemiş sayılmaz — çevirmen onu bilerek çevirmeden bırakmıştır.
    * İngilizce dışı: aynı uzunluk/kelime kapıları (başlıkta gevşek, bkz.
      :func:`is_english_line`), sonra
      ``core.lang_detect.classify_untranslated_line`` (Rusça: Kiril blok ya
      da karma satır; de/fr/es/it: Latin alfabeli Avrupa ailesi — QC ile
      aynı sınıflandırıcılar, kopya yok). Model zaman zaman Türkçe yerine
      İngilizce ürettiği için İngilizce kalmış satır da çevrilmemiş sayılır.
    """
    source = source_lang or "en"
    if source == "en":
        verdict = is_english_line(text, min_words=min_words,
                                  min_length=min_length, is_heading=is_heading)
        # G2a-2: kapı yalnız sezgisel karar True iken sorulur (karar yalnız
        # True→False yönünde değişir; Türkçe satırda ek maliyet yok).
        if verdict and not is_heading and _segment_gate_code(text, "en"):
            return False
        return verdict
    if is_heading:
        min_words = min(min_words, 2)
        min_length = min(min_length, 8)
    clean = _clean_for_language_check(text, is_heading=is_heading)
    if len(clean) < min_length or len(clean.split()) < min_words:
        return False
    # 2026-09-29 (F3-09): stash'in Türkçeleştirdiği atıf etiketi ("(Şek. 1.1)",
    # "Tablo 2") çevrilmemiş cümleye Türkçeye özgü harf katıp sınıflandırıcıyı
    # "Türkçe" kararına düşürüyordu; etiket dil kararına katılmaz.
    clean = _mask_xref_labels(clean)
    from core.lang_detect import classify_untranslated_line

    if classify_untranslated_line(clean, source, heading=is_heading) is not None:
        verdict = True
    else:
        verdict = is_english_line(text, min_words=min_words,
                                  min_length=min_length, is_heading=is_heading)
    # G2a-2 (2026-10-05): çevirmenin segment kapısı (bu kaynak dille) satırı
    # başka bir dile ayırıyorsa çevrilmemiş sayılmaz — Latince alıntıyı
    # Avrupa ailesi sınıflandırıcısı Roman dili sanabiliyordu. "en" istisna:
    # model zaman zaman Türkçe yerine İngilizce ürettiği için İngilizce dışı
    # kaynakta İngilizce satır çevrilmemiş sayılmaya devam eder (kapı
    # İngilizce alıntıyı da "en" diye ayırır; ikisi çıktıdan ayırt edilemez).
    if verdict and not is_heading:
        gate = _segment_gate_code(text, source)
        if gate is not None and gate != "en":
            return False
    return verdict


# ---------------------------------------------------------------------------
# Author / affiliation line detection
# ---------------------------------------------------------------------------

# Keywords that indicate an author/affiliation line (should not be translated)
# Only long-enough keywords to avoid false matches inside regular words.
_AFFILIATION_KEYWORDS = {
    "university", "department", "institute", "laboratory", "lab", "labs",
    "college", "school", "faculty", "center", "centre",
    "usa", "germany", "france", "japan", "china", "india", "canada",
    "new york", "brooklyn", "los angeles", "london", "berlin", "paris",
}
# Short US state abbreviations: matched with word boundary via regex
_US_STATE_ABBREV_RE = re.compile(
    r"\b(?:MD|CA|NY|MA|IL|TX|PA|OH|FL|GA|VA|NC|NJ|CT|WA)\b"
)

_AUTHOR_LINE_RE = re.compile(
    r"^[A-Z][a-z]+(?:\s+[A-Z]\.?)?\s+[A-Z][a-z]+"  # "First [M.] Last"
    r"\s*---\s*"  # separator
)


def _is_author_affiliation_line(text: str) -> bool:
    """Check if a line is an author name + affiliation (should not be translated)."""
    lower = text.lower()

    # Pattern 1: "Name --- Department/University..."
    if _AUTHOR_LINE_RE.match(text):
        return True

    # Pattern 2: Contains 2+ affiliation keywords (or 1 keyword + US state)
    keyword_count = sum(1 for kw in _AFFILIATION_KEYWORDS if kw in lower)
    if _US_STATE_ABBREV_RE.search(text):
        keyword_count += 1
    if keyword_count >= 2:
        return True

    # Pattern 3: Mostly proper nouns (capitalized words) with commas
    # e.g., "Springfield, USA Riverton, USA Lakeside, USA" (sentetik; FX3)
    words = text.split()
    if len(words) >= 3:
        capitalized = sum(1 for w in words if w[0:1].isupper() and w.isalpha())
        if capitalized / len(words) > 0.7 and "," in text:
            # Mostly capitalized words with commas = likely place names
            return True

    # Pattern 4: Line of mostly proper nouns (all capitalized, no function words)
    # e.g., "Lena Brandt Omar Sayed Mira K. Okafor" (sentetik; FX3)
    if len(words) >= 3:
        # Check if it looks like a list of names:
        # - Most words are capitalized
        # - Very few (or zero) English function words
        # - No verbs, no common sentence patterns
        capitalized = sum(
            1 for w in words
            if w[0:1].isupper() and len(w) > 1
        )
        func_words = sum(1 for w in words if w.lower() in _ENGLISH_FUNCTION_WORDS)
        if capitalized / len(words) > 0.8 and func_words == 0 and len(words) <= 15:
            return True

    return False


# ---------------------------------------------------------------------------
# Special region detection (titlepage, references)
# ---------------------------------------------------------------------------

# G2a-2 (2026-10-05): her başlık düzeyi (dönüştürücü bölüm sonu kaynakçasını
# \subsubsection*'a kadar indirebiliyor) ve isteğe bağlı kısa başlık.
_BIB_HEAD_SHELL_RE = re.compile(
    r"\\(?:chapter|section|subsection|subsubsection)\*?\s*"
    r"(?:\[[^\]]*\])?\s*\{([^{}]*)\}")
# Başlık düzeyi (bölge, düzeyi kaynakça başlığınınkine eşit ya da daha üst
# başlıkta biter) — translator.bibliography_regions ile aynı kural.
_HEADING_LEVEL_RE = re.compile(
    r"\\(part|chapter|section|subsection|subsubsection)(?![A-Za-z])")
_HEADING_LEVELS = {"part": -1, "chapter": 0, "section": 1,
                   "subsection": 2, "subsubsection": 3}
_BIB_REGION_STOP_RE = re.compile(
    r"\\(?:appendix|backmatter|end\{document\})(?![A-Za-z])")


def _heading_level(stripped: str) -> Optional[int]:
    """Satır bir başlıkla başlıyorsa düzeyi (``_HEADING_LEVELS``), yoksa None."""
    m = _HEADING_LEVEL_RE.match(stripped)
    return _HEADING_LEVELS[m.group(1)] if m else None


def _is_localized_bib_heading(stripped: str) -> bool:
    """Satır, adı ``patterns.BIBLIOGRAPHY_HEADING_NAMES``'te olan bir başlık
    mı? (küçük harf + aksansız; numara öneki atılır — G2a-2: çevirmenin
    kaynakça kuralıyla aynı ``translator.bibliography_heading_key``)"""
    m = _BIB_HEAD_SHELL_RE.match(stripped)
    if not m:
        return False
    from latex_pipeline.translator import bibliography_heading_key

    return bibliography_heading_key(m.group(1)) is not None


def _find_special_regions(lines: List[str],
                          source_lang: str = "en") -> Tuple[set, set]:
    """Find line indices inside titlepage/frontmatter and references/bibliography sections.

    Returns (titlepage_lines, references_lines) as sets of 0-based indices.
    Titlepage lines cover everything inside \\begin{titlepage}...\\end{titlepage};
    references lines cover a References/Bibliography/Kaynaklar chapter (or
    section) up to the next non-bibliography chapter.

    Kaynakça başlığı ayrıca ``patterns.BIBLIOGRAPHY_HEADING_NAMES`` ile (6
    kaynak dil + Türkçe; çevrilmeden kalmış "Literaturverzeichnis",
    "Литература", numaralı "5 Kaynakça") tanınır. 2026-09-28'de bu yalnız
    İngilizce dışı kaynakta yapılıyordu; G2a-2'den (2026-10-05) beri çevirmen
    kaynakçayı her dilde koruduğu için her kaynakta (*source_lang* artık
    karara katılmaz, imza uyum için duruyor). Düzey: \\chapter …
    \\subsubsection.
    """
    titlepage_lines: set = set()
    references_lines: set = set()

    in_titlepage = False
    in_references = False
    in_references_level = 0  # B-6: bölgenin başlangıç düzeyi (_HEADING_LEVELS)

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Titlepage detection: \begin{titlepage}...\end{titlepage}
        if re.match(r"\\begin\{titlepage\}", stripped):
            in_titlepage = True
        if in_titlepage:
            titlepage_lines.add(i)
        if re.match(r"\\end\{titlepage\}", stripped):
            in_titlepage = False

        # References/Bibliography detection
        # Starts at \chapter*{References}, \chapter*{Bibliography},
        # \chapter*{Kaynaklar}, \chapter*{Kaynakca}
        # or \section*{References} etc.
        _bib_head_re = (
            r"\\(?:chapter|section|subsection|subsubsection)\*?\s*\{"
            r"(?:References|Bibliography|Kaynaklar|Kaynakça|Referanslar)\}"
        )
        _is_bib_head = bool(re.match(_bib_head_re, stripped))
        if not _is_bib_head:
            # G2a-2 (2026-10-05): her kaynak dilde aynı ad kümesi — çevirmen
            # artık kaynakçayı her dilde koruyup başlığı çeviriyor
            # ("Kaynakça"/"Kaynaklar", numaralı "5 Kaynakça"); İngilizce
            # kitabın girdileri İngilizce kalır, burada da korunmalı.
            _is_bib_head = _is_localized_bib_heading(stripped)
        if _is_bib_head:
            in_references = True
            # B-6 (2026-07-27): bölge hangi seviyede başladıysa o seviyede
            # biter — \section*{References} sonrası koruma eskiden yalnız
            # \chapter ile bitiyordu; aynı bölümdeki SONRAKİ \section'lar
            # da 'kaynakça' sanılıp retranslation'dan muaf kalıyordu.
            # G2a-2: \subsection/\subsubsection düzeyi de (sayısal düzey).
            in_references_level = _heading_level(stripped)
        if in_references:
            references_lines.add(i)
            # Region ends at the next non-bib heading of the same-or-higher
            # level (Reviewer R6: kaynakçanın kendi alt başlıkları bölgeyi
            # bölmez) ya da \appendix / \backmatter / \end{document}.
            if i > 0 and not _is_bib_head:
                lvl = _heading_level(stripped)
                _ends = ((lvl is not None and lvl <= in_references_level)
                         or bool(_BIB_REGION_STOP_RE.match(stripped)))
                if _ends:
                    in_references = False
                    references_lines.discard(i)  # Don't include the next heading

    return titlepage_lines, references_lines


# ---------------------------------------------------------------------------
# Main API
# ---------------------------------------------------------------------------

def scan_and_retranslate(
    tex_content: str,
    translator_fn: Callable[[str], str],
    min_line_length: int = 20,
    min_words: int = 4,
    dry_run: bool = False,
    heading_translator_fn: Optional[Callable[[str], str]] = None,
    source_lang: str = "en",
) -> RetranslateResult:
    """Scan translated .tex content and retranslate lines that remain English.

    Parameters
    ----------
    tex_content : str
        The full translated .tex body content.
    translator_fn : callable
        Function that takes English text and returns Turkish translation.
        Signature: translator_fn(english_text: str) -> str
    heading_translator_fn : callable, optional
        2026-09-24: başlık satırları için BAŞLIK OTORİTESİ
        (``LatexTranslator._translate_heading_text``: başlık modu MT + dil
        kapısı + yarım-çeviri kapısı). Verilmezse başlıklar da
        ``translator_fn`` ile (gövde yolu) çevrilir — eski davranış; bu yol
        başlık modunu bilmez, o yüzden çağıranın vermesi beklenir.
    min_line_length : int
        Minimum character length of cleaned text to consider for English check.
    min_words : int
        Minimum word count of cleaned text to consider for English check.
    dry_run : bool
        If True, detect English lines but do not retranslate them.
    source_lang : str
        2026-09-28 (K5): kitabın kaynak dili (``TEX_SOURCE_LANGS``). "en"
        (varsayılan) eski davranıştır (:func:`is_english_line`); başka dilde
        çevrilmemiş satır :func:`is_untranslated_line` ile o dilde aranır.

    Returns
    -------
    RetranslateResult
        Contains the retranslated content and detailed change log.
    """
    result = RetranslateResult()
    lines = tex_content.split("\n")
    result.total_lines = len(lines)

    # Build set of protected line indices (math, code, bibliography)
    protected_lines = _build_protected_line_set(tex_content, lines)

    # Also protect titlepage and references/bibliography sections
    # by scanning for \begin{titlepage}...\end{titlepage} and
    # \chapter*{References} / \chapter*{Bibliography} regions
    titlepage_lines, references_lines = _find_special_regions(lines,
                                                               source_lang)
    protected_lines.update(titlepage_lines)
    protected_lines.update(references_lines)

    logger.info(
        "Retranslator: scanning %d lines (%d protected by math/code/bib, "
        "%d titlepage, %d references)",
        len(lines), len(protected_lines) - len(titlepage_lines) - len(references_lines),
        len(titlepage_lines), len(references_lines),
    )

    output_lines = list(lines)  # Copy for modification

    for i, line in enumerate(lines):
        # Skip protected lines (math, code, bibliography)
        if i in protected_lines:
            result.skipped_math += 1
            continue

        # Altyazı satırı (F1 MT-2/MT-3, 2026-09-30): yapısal sayılmadan önce
        # yalnız METNİ denetlenir ve gerekirse kabuğu korunarak çevrilir.
        caption_shell = _split_caption_shell(line.lstrip())
        if caption_shell is not None:
            result.scanned_lines += 1
            _retranslate_caption_line(i, line, caption_shell, output_lines,
                                      result, translator_fn, source_lang,
                                      dry_run)
            continue

        # Skip structural/empty/comment lines
        if _is_structural_line(line):
            result.skipped_command += 1
            continue

        # Detect if this line is a heading (section/chapter/etc.)
        # Headings that pass _is_structural_line() have translatable content
        line_is_heading = bool(_HEADING_CMD_RE.match(line.strip()))

        # Strip LaTeX for language check.  Heading-aware so a bare
        # \section{Deep Learning} isn't dropped as "too short" before the
        # English check (shared with is_english_line -> no disagreement).
        clean = _clean_for_language_check(line, is_heading=line_is_heading)

        # Use relaxed thresholds for headings (short titles are common)
        effective_min_length = 8 if line_is_heading else min_line_length
        effective_min_words = 2 if line_is_heading else min_words

        if len(clean) < effective_min_length or len(clean.split()) < effective_min_words:
            result.skipped_short += 1
            continue

        result.scanned_lines += 1

        # Check if line is English (pass is_heading for relaxed thresholds).
        # 2026-09-28 (K5): kaynak dilde kalmış satır (İngilizcede AYNI karar).
        if not is_untranslated_line(
            line,
            source_lang,
            min_words=min_words,
            min_length=min_line_length,
            is_heading=line_is_heading,
        ):
            # 2026-09-29 (F3-09): satırın çoğu Türkçe olunca satır kararı
            # kaynak dilde kalan TEK cümleyi görmüyordu (paragrafın %25'i
            # eşiğin altında kalır). Gövde satırı cümlelerine bölünür; kaynak
            # dilde kalan cümle tek başına yeniden çevrilir.
            if not line_is_heading:
                _retranslate_sentences_in_line(
                    i, line, output_lines, result, translator_fn,
                    source_lang, min_words, min_line_length, dry_run,
                    heading_translator_fn=heading_translator_fn)
            continue

        result.english_lines_found += 1
        line_num = i + 1  # 1-based

        logger.info(
            "Retranslator: %s detected at L%d: %.80s",
            "English" if (source_lang or "en") == "en"
            else f"Untranslated ({source_lang})",
            line_num, clean[:80],
        )

        if dry_run:
            result.changes.append((line_num, line.strip(), "[DRY RUN - not translated]"))
            continue

        # Extract translatable text (preserve leading whitespace and LaTeX structure)
        # We translate the cleaned text, then reconstruct the line.
        # Strategy: find contiguous English text spans, translate them,
        # put them back in place.
        try:
            if line_is_heading and heading_translator_fn is not None:
                # 2026-09-24: başlık satırı başlık otoritesiyle çevrilir.
                # Eskiden gövde yolu (_safe_translate) kullanılıyordu: başlık
                # modu yok, NER koruması Title Case başlığı yutuyor, sonuç
                # echo/İngilizce -> "still English" ile başarısız.
                new_line = _retranslate_heading_line(line, heading_translator_fn,
                                                     source_lang=source_lang)
            else:
                new_line = _retranslate_line(line, translator_fn,
                                             source_lang=source_lang)

            if new_line and new_line != line:
                # Verify the translation is actually Turkish now
                if not is_untranslated_line(new_line, source_lang, min_words=min_words,
                                            min_length=min_line_length,
                                            is_heading=line_is_heading):
                    # Apply heading sentence-case if this is a heading
                    if line_is_heading:
                        new_line = _ensure_heading_case(new_line)
                    output_lines[i] = new_line
                    result.retranslated_lines += 1
                    result.changes.append((line_num, line.strip(), new_line.strip()))

                    logger.info(
                        "Retranslator: L%d retranslated: %.60s -> %.60s",
                        line_num, clean[:60], _strip_latex_for_language_check(new_line)[:60],
                    )
                else:
                    # Translation still English -- likely a stubborn passage
                    result.failed_lines += 1
                    logger.warning(
                        "Retranslator: L%d still English after retranslation, keeping original.",
                        line_num,
                    )
            else:
                result.failed_lines += 1
                logger.warning(
                    "Retranslator: L%d translation returned empty or identical.",
                    line_num,
                )

        except Exception as exc:
            result.failed_lines += 1
            logger.error(
                "Retranslator: L%d translation failed: %s",
                line_num, exc,
            )

    # --- Final pass: ensure ALL headings have sentence-case (FIX 5) ---
    # This catches headings that were not retranslated but still have
    # lowercase first letters from the original MT output.
    # RT-F6 (2026-07-28): korumali satirlar (verbatim/lstlisting/math/bib/
    # titlepage) BU gecise de dahil edilmiyordu — kod ornegi icindeki
    # '\section{introduction ...}' satiri kitapta gosterilen listing'i
    # bozacak sekilde degistiriliyordu. Ana dongu ile ayni filtre.
    heading_case_fixes = 0
    for i, line in enumerate(output_lines):
        if i in protected_lines:
            continue
        if _HEADING_CMD_RE.match(line.strip()):
            fixed = _ensure_heading_case(line)
            if fixed != line:
                output_lines[i] = fixed
                heading_case_fixes += 1
    result.heading_case_fixes = heading_case_fixes
    if heading_case_fixes:
        logger.info(
            "Retranslator: fixed sentence-case on %d heading(s).",
            heading_case_fixes,
        )

    result.output_tex = "\n".join(output_lines)

    logger.info(
        "Retranslator complete: %d scanned, %d English found, "
        "%d retranslated, %d failed, %d heading-case, "
        "%d skipped (math=%d, cmd=%d, short=%d)",
        result.scanned_lines,
        result.english_lines_found,
        result.retranslated_lines,
        result.failed_lines,
        result.heading_case_fixes,
        result.skipped_math + result.skipped_command + result.skipped_short,
        result.skipped_math,
        result.skipped_command,
        result.skipped_short,
    )

    return result


# ---------------------------------------------------------------------------
# Cümle düzeyi tarama (2026-09-29, F3-09)
# ---------------------------------------------------------------------------

_XREF_LABEL_RE: Optional["re.Pattern[str]"] = None


def _mask_xref_labels(text: str) -> str:
    """Translator stash'inin Türkçeleştirdiği atıf etiketlerini (etiket +
    numara: "Şekil 1.1", "Şek.~2", "Tablo (3)") boşlukla değiştir. Etiket
    kümesinin tek kaynağı ``translator._FIGURE_LABEL_MAP`` (F3-09)."""
    global _XREF_LABEL_RE
    if _XREF_LABEL_RE is None:
        from latex_pipeline.translator import _FIGURE_LABEL_MAP
        labels = sorted(set(_FIGURE_LABEL_MAP.values()), key=len, reverse=True)
        _XREF_LABEL_RE = re.compile(
            r"(?<![^\W\d_])(?:" + "|".join(re.escape(lb) for lb in labels) + r")"
            r"[\s~\u00a0]*\(?\d+(?:\.\d+)*[a-z]?\)?")
    return _XREF_LABEL_RE.sub(" ", text)


def _sentence_splitter(source_lang: str) -> "re.Pattern[str]":
    """Kaynak dile göre cümle bölücü (translator ile ORTAK desenler)."""
    from latex_pipeline.translator import (LatexTranslator,
                                           _SOURCE_SENTENCE_SPLIT_RES)
    code = source_lang or "en"
    if code == "en":
        return LatexTranslator._SENTENCE_SPLIT_RE
    return _SOURCE_SENTENCE_SPLIT_RES.get(code, LatexTranslator._SENTENCE_SPLIT_RE)


def _line_sentence_spans(line: str, source_lang: str) -> List[Tuple[int, int]]:
    """Satırın cümle aralıkları ``(başlangıç, bitiş)`` — bitişik, boşluklar
    sonraki cümleye yapışmaz. Yalnız süslü parantez derinliği 0 olan ve
    ``$…$`` matematiği DIŞINDAKİ sınırlarda bölünür (``\footnote{A. B}``,
    ``$x. Y$`` bölünmez)."""
    from core.lang_detect import dollar_math_spans
    boundaries = list(_sentence_splitter(source_lang).finditer(line))
    if not boundaries:
        return [(0, len(line))]
    math = [(s, e) for s, e, _d in dollar_math_spans(line)]
    # Her konumdaki süslü parantez derinliği (kaçışlar atlanır), tek geçiş.
    depth_at = [0] * (len(line) + 1)
    depth = 0
    k = 0
    while k < len(line):
        depth_at[k] = depth
        ch = line[k]
        if ch == "\\" and k + 1 < len(line):
            depth_at[k + 1] = depth
            k += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        k += 1
    depth_at[len(line)] = depth
    cuts: List[Tuple[int, int]] = []   # (cümle sonu, sonraki cümle başı)
    for m in boundaries:
        pos = m.start()
        if depth_at[pos] != 0 or any(s <= pos < e for s, e in math):
            continue
        cuts.append((m.start(), m.end()))
    spans: List[Tuple[int, int]] = []
    start = 0
    for end, nxt in cuts:
        spans.append((start, end))
        start = nxt
    spans.append((start, len(line)))
    return spans


# ---------------------------------------------------------------------------
# Cümle başında kaynak dilde kalmış öbek (F1 MT-4 / MT-7, 2026-09-30)
# ---------------------------------------------------------------------------
# Model cümle başını kaynak dilde bırakıp gerisini çevirebiliyor:
# "Dieses Kapitel yalnızca ısı iletimini ele alır", "Das Minuszeichen
# ifadesi…" (hymt-de), kalın etiket "\textbf{First Law:} Her cisim…" ve
# "Third Law: Bir gezegenin…" (bir model testi çıktısı). Cümlenin çoğu Türkçe olduğu
# için satır/cümle kararı ("çevrilmemiş mi?") bunları görmüyordu. Tek
# cümlelik tam kaynak dil kaçağı da kısa kaldığında (5 kelime, 2 işlev
# sözcüğü: "In \textit{a} wird die Wärmeleitfähigkeit behandelt.",
# hymt2-de) eşiklerin altında kalıyordu.
#
# Karar sözlükseldir ve muhafazakârdır:
#   * etiket ("Third Law:", "\textbf{First Law:}") en çok
#     LEADING_LABEL_MAX_WORDS kelime, iki nokta ile biter, arkasında Türkçe
#     vardır ve başlık kurallarıyla kaynak dilde sayılır -> yalnız etiket
#     yeniden çevrilir (başlık otoritesiyle);
#   * cümle başı öbeği en az LEADING_RUN_MIN_WORDS Türkçe olmayan kelimedir,
#     içinde en az bir kaynak dil işlev sözcüğü (core.lang_detect.PROFILES)
#     vardır ve arkasından Türkçe gelir -> cümlenin tamamı yeniden çevrilir;
#   * Türkçe hiç yoksa (cümlenin tamamı öbek) en az LEADING_FULL_MIN_WORDS
#     kelime ve LEADING_FULL_MIN_HITS işlev sözcüğü gerekir.
# Başka dilde alıntı (Latince vb.) ve künye satırı hiç sınıflanmaz. Yeniden
# çeviri hâlâ öbek taşıyorsa ya da kaynak dildeyse özgün hâl korunur.
LEADING_LABEL_MAX_WORDS = 6
LEADING_RUN_MIN_WORDS = 2
LEADING_FULL_MIN_WORDS = 3
LEADING_FULL_MIN_HITS = 2
# Etiket dil kararı için en kısa metin (başlık eşiği 8'den kısa: "Third Law"
# 9, "Solution" 8; "Law"/"Not" gibi 3 harfliler hiç denenmez).
LEADING_LABEL_MIN_CHARS = 4
# Özel ad parçacığı sayılan küçük harfli kelimenin en çok uzunluğu ("de",
# "la", "van", "der", "von", "del"); daha uzunları yalnız kaynak dilin işlev
# sözcüğüyse parçacık sayılır.
NAME_PARTICLE_MAX_CHARS = 3

_LEADING_FMT_LABEL_RE = re.compile(
    r"^\s*(?:\\item\s*(?:\[[^\]]*\])?\s*)?"
    r"\\(?:textbf|textit|emph)\{([^{}]{1,80})\}(\s*:)?")
_LEADING_ITEM_RE = re.compile(r"^\s*\\item\s*(?:\[[^\]]*\])?\s*")
_RUN_TOKEN_RE = re.compile(r"\S+")
# Uzun/orta tire ve tek k\u00f6\u015feli t\u0131rnak da k\u0131rp\u0131l\u0131r (F1, 2026-09-30): diyalog
# tiresi yap\u0131\u015f\u0131k T\u00fcrk\u00e7e kelimeyi ("\u2014\u00c7ok ses mi?", dil_fr) T\u00fcrk\u00e7e
# sayd\u0131rm\u0131yordu.
_RUN_STRIP_CHARS = (".,;:!?()[]{}\"'\u201c\u201d\u201e\u00ab\u00bb\u2018\u2019"
                    "\u2014\u2013\u2039\u203a")
# Kesme i\u015faretli ek aran\u0131rken token sonundan k\u0131rp\u0131lan noktalama (kesme
# i\u015fareti HAR\u0130\u00c7: "\u2019den" ekinin ba\u015f\u0131ndaki i\u015faret korunmal\u0131).
_RUN_TAIL_CHARS = ".,;:!?)]}\"\u201d\u00bb"
# Kesme işaretli Türkçe ek ("Kepler’in", "Güneş’ten", "Newton'un").
_TR_APOSTROPHE_SUFFIX_RE = re.compile(
    r"['\u2019](?:n?[ıiuü]n|[ıiuü]|y?[ae]|[dt][ae]n?|[dt][ae]ki|l[ae]r\w*"
    r"|n?[ıiuü]n?|s[ıiuü]|y[ıiuü]|n[ae])$", re.IGNORECASE)


# Türkçe alfabede olup KAYNAK dilin alfabesinde olmayan harfler: kelimede
# geçmesi Türkçe kanıtıdır (F1, 2026-09-30). İngilizce/Rusça değeri eski
# kuralın aynısıdır ("çöü"); diğer dillerde yalnız o dilde bulunmayanlar
# (Fransızca ç, İspanyolca ü, Almanca ö/ü kendi harfleridir). Eskiden kural
# yalnız en/ru'da işliyordu; Fransızca kaynakta "dünyada ses getirmemesi"
# ("ses" Fransızca işlev sözcüğü) cümle başı öbeği sanılıyordu (dil_fr).
_NON_SOURCE_TR_LETTERS = {
    "en": "çöüÇÖÜ",
    "ru": "çöüÇÖÜ",
    "fr": "öüÖÜ",
    "es": "çöÇÖ",
    "it": "çöüÇÖÜ",
    "de": "çÇ",
}


def _word_is_turkish(word: str, source_lang: str) -> bool:
    """Kelime Türkçe kanıtı mı? (Türkçeye özgü harf, Türkçe işlev sözcüğü,
    kesme işaretli Türkçe ek ya da Türkçe biçim eki)."""
    from core.lang_detect import PROFILES, TURKISH_ONLY_RE
    if TURKISH_ONLY_RE.search(word):
        return True
    letters = _NON_SOURCE_TR_LETTERS.get(source_lang or "en", "")
    if letters and any(c in word for c in letters):
        return True
    lw = word.lower()
    if lw in PROFILES["tr"] or lw in _TURKISH_COMMON_WORDS:
        return True
    if _TR_APOSTROPHE_SUFFIX_RE.search(word):
        return True
    return (len(lw) >= 5 and lw.isalpha() and _has_turkish_suffix(lw)
            and lw not in PROFILES.get(source_lang, ()))


def _label_is_untranslated(label: str, source_lang: str) -> bool:
    """Kısa etiket ("Third Law", "Solution") kaynak dilde mi? Başlık
    kuralları; Türkçe atıf etiketi ("Tablo 1.1") karara katılmaz."""
    clean = _mask_xref_labels(_strip_latex_for_language_check(label))
    clean = re.sub(r"\s+", " ", clean).strip(" :.-\u2013\u2014")
    if len(clean) < LEADING_LABEL_MIN_CHARS:
        return False
    return is_untranslated_line(clean, source_lang, min_words=1,
                                min_length=LEADING_LABEL_MIN_CHARS,
                                is_heading=True)


def _leading_source_run(sentence: str, source_lang: str):
    """Cümle başında kaynak dilde kalmış öbek -> (tür, başlangıç, bitiş).

    tür "label": yalnız [başlangıç, bitiş) aralığındaki etiket metni yeniden
    çevrilir; tür "mixed": cümlenin tamamı. Öbek yoksa None."""
    from core.lang_detect import (PROFILES, classify_foreign_segment,
                                  looks_like_citation)
    lang = source_lang or "en"
    if looks_like_citation(sentence):
        return None
    if classify_foreign_segment(sentence, lang) is not None:
        return None  # başka dilde alıntı (Latince vb.): dokunulmaz
    pos0 = 0
    fm = _LEADING_FMT_LABEL_RE.match(sentence)
    if fm:
        inner = fm.group(1)
        core = inner.rstrip().rstrip(":").rstrip()
        if (inner.rstrip().endswith(":") or fm.group(2)) and core:
            rest = sentence[fm.end():]
            if (any(_word_is_turkish(t.strip(_RUN_STRIP_CHARS), lang)
                    for t in rest.split())
                    and _label_is_untranslated(core, lang)):
                start = fm.start(1) + (len(inner) - len(inner.lstrip()))
                return "label", start, start + len(core.strip())
        pos0 = fm.end()
    else:
        im = _LEADING_ITEM_RE.match(sentence)
        if im:
            pos0 = im.end()
    run = []
    stopped_turkish = False
    label_colon = None
    for tok in _RUN_TOKEN_RE.finditer(sentence, pos0):
        t = tok.group(0)
        if t[:1] in ("\\", "{", "}") or "$" in t or "<<" in t:
            continue  # LaTeX komutu / matematik / yer tutucu: nötr
        word = t.strip(_RUN_STRIP_CHARS)
        if not word or not any(ch.isalpha() for ch in word):
            continue  # sayı / noktalama: nötr
        # Atıftan / yer tutucudan ayrı yazılmış kesme işaretli Türkçe ek
        # ("Gl. (1.5) ’den gelen …", hymt2-de L89) Türkçe kanıtıdır; kesme
        # işareti kırpılınca "den" kaynak dil (Almanca) işlev sözcüğü
        # sanılıyordu (F1, 2026-09-30).
        if (_word_is_turkish(word, lang)
                or _TR_APOSTROPHE_SUFFIX_RE.search(t.rstrip(_RUN_TAIL_CHARS))):
            stopped_turkish = True
            # Özel ad parçacıkları (F1, 2026-09-30): öbek yalnız küçük harfli
            # kısa parçacıklardan / kaynak dil işlev sözcüklerinden oluşuyor
            # ve Türkçe kanıtı olan kelime BÜYÜK harfli, kesme işaretli ekli
            # bir özel adsa ("de la Rue'nün", "van der Waals’ın", "von
            # Kármán’a") öbek adın parçasıdır, kaynak dil kaçağı değildir
            # (dil_fr L462).
            if (run and word[:1].isupper()
                    and _TR_APOSTROPHE_SUFFIX_RE.search(
                        t.rstrip(_RUN_TAIL_CHARS))
                    and all(w.islower()
                            and (len(w) <= NAME_PARTICLE_MAX_CHARS
                                 or w in PROFILES.get(lang, ()))
                            for w, _s, _e in run)):
                return None
            break
        run.append((word, tok.start(), tok.end()))
        if t.rstrip(")]}\"'\u201d\u2019").endswith(":"):
            label_colon = tok.start() + t.rindex(":")
            break
    if not run:
        return None
    if label_colon is not None:
        if len(run) > LEADING_LABEL_MAX_WORDS:
            return None
        rest = sentence[label_colon + 1:]
        if not any(_word_is_turkish(t.strip(_RUN_STRIP_CHARS), lang)
                   for t in rest.split()):
            return None
        start = run[0][1]
        if _label_is_untranslated(sentence[start:label_colon], lang):
            return "label", start, label_colon
        return None
    profile = PROFILES.get(lang, frozenset())
    hits = sum(1 for w, _s, _e in run if w.lower() in profile)
    if stopped_turkish:
        if len(run) >= LEADING_RUN_MIN_WORDS and hits >= 1:
            return "mixed", 0, len(sentence)
        return None
    if len(run) >= LEADING_FULL_MIN_WORDS and hits >= LEADING_FULL_MIN_HITS:
        return "mixed", 0, len(sentence)
    return None


def _retranslate_sentences_in_line(
    i: int, line: str, output_lines: List[str], result: "RetranslateResult",
    translator_fn: Callable[[str], str], source_lang: str,
    min_words: int, min_line_length: int, dry_run: bool,
    heading_translator_fn: Optional[Callable[[str], str]] = None,
) -> None:
    """Satır kaynak dilde değil ama içinde kaynak dilde kalmış cümle varsa
    yalnız o cümleyi yeniden çevir (F3-09). Tek cümlelik satırda cümle kararı
    yok (satır kararı zaten verildi).

    2026-09-30 (F1 MT-4/MT-7): her cümlede ayrıca cümle başı öbeği aranır
    (:func:`_leading_source_run`) — tek cümlelik satırlarda da. Etiket
    öbeğinde yalnız etiket (başlık otoritesiyle, verilmişse), karışık cümlede
    cümlenin tamamı yeniden çevrilir."""
    spans = _line_sentence_spans(line, source_lang)
    jobs: List[Tuple[int, int, str]] = []
    if len(spans) >= 2:
        for s, e in spans:
            if is_untranslated_line(_mask_xref_labels(line[s:e]), source_lang,
                                    min_words=min_words,
                                    min_length=min_line_length):
                jobs.append((s, e, "sentence"))
    covered = {(s, e) for s, e, _k in jobs}
    # Künye satırı (kaynakça maddesi) cümle başı öbeği için taranmaz: cümle
    # bölücü baş harfte ("K. Hartwig:") böldüğü için tek cümlede künye
    # görünmüyor, yazar adı "etiket" sanılıp başlık yoluyla MT'ye gidiyordu
    # (hymt2-de L174, gptoss-de L176; F1, 2026-09-30).
    from core.lang_detect import looks_like_citation
    lead_spans = [] if looks_like_citation(line) else spans
    for s, e in lead_spans:
        if (s, e) in covered:
            continue
        lead = _leading_source_run(line[s:e], source_lang)
        if lead is not None:
            kind, ls, le = lead
            jobs.append((s + ls, s + le, kind))
    if not jobs:
        return
    jobs.sort()
    line_num = i + 1
    result.english_lines_found += 1
    logger.info(
        "Retranslator: L%d içinde %d parça kaynak dilde (%s) kalmış: %.80s",
        line_num, len(jobs), source_lang or "en",
        line[jobs[0][0]:jobs[0][1]],
    )
    if dry_run:
        result.changes.append((line_num, line.strip(),
                               "[DRY RUN - not translated]"))
        return
    new_line = line
    fixed = 0
    # Sağdan sola: önceki aralıklar kaymasın.
    for s, e, kind in reversed(jobs):
        piece = line[s:e]
        try:
            if kind == "label":
                fn = heading_translator_fn or translator_fn
                new_piece = (fn(piece.strip()) or "").strip()
                ok = (bool(new_piece) and new_piece != piece.strip()
                      and _latex_skeleton_kept(piece, new_piece)
                      and not _label_is_untranslated(new_piece, source_lang))
            else:
                new_piece = _retranslate_line(piece, translator_fn,
                                              source_lang=source_lang)
                new_piece = (new_piece or "").strip()
                ok = (bool(new_piece) and new_piece != piece
                      and not is_untranslated_line(
                          _mask_xref_labels(new_piece), source_lang,
                          min_words=min_words, min_length=min_line_length))
                if ok and kind == "mixed":
                    ok = _leading_source_run(new_piece, source_lang) is None
        except Exception as exc:
            logger.error("Retranslator: L%d cümle çevirisi hata: %s",
                         line_num, exc)
            continue
        if ok:
            new_line = new_line[:s] + new_piece + new_line[e:]
            fixed += 1
    if fixed == len(jobs):
        output_lines[i] = new_line
        result.retranslated_lines += 1
        result.changes.append((line_num, line.strip(), new_line.strip()))
        logger.info("Retranslator: L%d cümle düzeyinde yeniden çevrildi "
                    "(%d parça).", line_num, fixed)
    elif fixed:
        # Kısmi başarı da yazılır (kayıp yok: düzelmeyen parça kaynak
        # hâliyle yerinde); satır başarısız sayılır, QC görür.
        output_lines[i] = new_line
        result.failed_lines += 1
        result.changes.append((line_num, line.strip(), new_line.strip()))
        logger.warning("Retranslator: L%d %d/%d parça düzeldi, kalanı kaynak "
                       "dilde kaldı.", line_num, fixed, len(jobs))
    else:
        result.failed_lines += 1
        logger.warning("Retranslator: L%d kaynak dildeki parça yeniden "
                       "çeviride düzelmedi, özgün hâli korundu.", line_num)


# ---------------------------------------------------------------------------
# Line-level retranslation
# ---------------------------------------------------------------------------

# Patterns for extracting translatable text segments from a line
_INLINE_MATH_PLACEHOLDER_RE = re.compile(r"(?<![\$\\])\$(?!\$)(.+?)(?<![\$\\])\$(?!\$)")
_LATEX_CMD_RE = re.compile(r"\\[a-zA-Z]+\*?(?:\s*\{[^}]*\})*(?:\s*\[[^\]]*\])*")
_PLACEHOLDER_TOKEN = "\uE100RTSTASH_{}\uE100"


def _rt_output_problem(sent: str, out: Optional[str], *,
                       skeleton: bool = True) -> Optional[str]:
    """Yeniden çeviri çıktısının yapısal sorunu (G2a-1); yoksa None.

    *sent* modele giden (yer tutuculu) metindir. Denetim
    ``latex_pipeline.translator`` bekçileriyle AYNI ölçütü kullanır: LaTeX
    iskeleti (komut adları ve sırası, ayraç dengesi, metnin hangi grupta
    durduğu), kaynakta olmayan ilişki simgesi, 2+ haneli sayı kaybı.
    ``skeleton=False`` metin korumalı denemenin sonucu içindir (kabuk sırası
    orada ``restore_latex_shells`` ile ayrıca doğrulanır).
    """
    from latex_pipeline.translator import (latex_skeleton_problem,
                                           missing_numbers,
                                           new_relation_symbols)
    if not out or not out.strip():
        return "boş çıktı"
    if skeleton:
        problem = latex_skeleton_problem(sent, out)
        if problem is not None:
            return problem
    rel = new_relation_symbols(sent, out)
    if rel:
        return "kaynakta olmayan ilişki simgesi %s" % dict(rel)
    lost = missing_numbers(sent, out)
    if lost:
        return "kayıp sayı %s" % sorted(lost)
    return None


def _guarded_retranslation(
    safe_work: str, first: str, translator_fn: Callable[[str], str],
    fix: Callable[[str], str],
) -> Optional[str]:
    """Yeniden çeviriyi bekçiden geçir; kabul edilen çeviri ya da None.

    G2a-1 (2026-10-05): eskiden ham ``\\textbf{...}`` gibi komutlar MT'ye
    gidiyor, sonuç denetlenmeden kabul ediliyordu (gptoss-de altyazı kabuğunu
    sildi, hymt2-de altyazı metnini ayraç dışına taşıdı; mt okuma MT-17).
    Sıra: ilk çeviri → aynı metin bir kez daha → metin korumalı çeviri (ham
    komut kabukları ``<<CMD_n>>``, yalnız metin çevrilir) → None (çağıran
    satırı aynen bırakır; QC kaynak dilde kalan satırı görür). *fix* her
    çıktıya restore öncesi uygulanan düzeltmedir (kısa italik kabuk sırası).
    """
    problem = _rt_output_problem(safe_work, first)
    if problem is None:
        return first
    logger.warning("Retranslator: çeviri bekçiye takıldı (%s); yeniden "
                   "deneniyor: %.60s", problem, safe_work)
    try:
        again = translator_fn(safe_work)
    except Exception as exc:  # çeviri hatası satırı bozmasın
        logger.warning("Retranslator: tekrar denemesi hata: %s", exc)
        again = None
    if again and again.strip():
        again = fix(again)
        if _rt_output_problem(safe_work, again) is None:
            return again
    from latex_pipeline.translator import (protect_latex_shells,
                                           restore_latex_shells)
    shelled = protect_latex_shells(safe_work)
    if shelled is not None:
        sent, shells = shelled
        try:
            out = translator_fn(sent)
        except Exception as exc:
            logger.warning("Retranslator: metin korumalı çeviri hata: %s", exc)
            out = None
        if out and out.strip():
            restored = restore_latex_shells(fix(out), sent, shells)
            if (restored is not None
                    and _rt_output_problem(safe_work, restored,
                                           skeleton=False) is None):
                logger.info("Retranslator: metin korumalı çeviri tuttu: %.60s",
                            safe_work)
                return restored
    logger.warning("Retranslator: bekçi tutmadı (%s); satır özgün hâliyle "
                   "korundu: %.60s", problem, safe_work)
    return None


def _retranslate_line(
    line: str,
    translator_fn: Callable[[str], str],
    *,
    is_heading: bool = False,
    source_lang: str = "en",
) -> str:
    r"""Retranslate English text in a single line, preserving LaTeX structure.

    Strategy:
    1. Stash \begin{name}/\end{name} env commands (session 5 fix).
    2. Stash inline math and LaTeX commands with placeholders.
    3. If remaining text is substantial, translate it.
    4. Restore all placeholders.
    5. Clean prompt leaks from MT output (session 5 fix).

    *is_heading* (2026-09-24): başlık METNİ için "yeterli metin" eşiği tarama
    döngüsüyle aynı (8 karakter / 2 kelime) olur; kabuk ayrıştırması bunu
    kendi özyinelemesinde geçirir.
    """
    # Preserve leading whitespace
    leading_ws = line[:len(line) - len(line.lstrip())]
    content = line.lstrip()

    if not content:
        return line

    # --- RT-06: preserve the heading command shell (\section{...}) ---
    # A heading line's ``\section{ ... }`` command must never reach MT raw:
    # MT can drop the command, silently demoting the heading to body text.
    # Retranslate only the inner title text, then reattach the exact shell.
    # Brace-dengeli ayristirma (#36/#80): '\section{X}\label{sec:x}'
    # satirinda yalniz GERCEK baslik metni MT'ye gider; kuyruk (\label vb.)
    # kabugun parcasi olarak aynen korunur.
    heading_shell = _split_heading_shell(content)
    if heading_shell:
        cmd_open, title, tail = heading_shell
        new_title = _retranslate_line(title, translator_fn, is_heading=True,
                                      source_lang=source_lang)
        new_tail = tail
        if tail.strip():
            # Kuyrukta ceviri gerektiren metin varsa ayni yoldan islenir
            # (\label gibi salt-komut kuyruklar kisa-metin esiginde aynen
            # doner).
            new_tail = _retranslate_line(tail, translator_fn,
                                         source_lang=source_lang)
        return leading_ws + cmd_open + new_title.strip() + "}" + new_tail

    # --- Session 5 fix: stash \begin{}/\end{} env names FIRST ---
    # This prevents MT from translating "figure" -> "sekil" etc.
    content, env_stash = _stash_env_names(content)

    # Stash inline math
    stash: List[str] = []

    def _stash(match: re.Match) -> str:
        idx = len(stash)
        stash.append(match.group(0))
        return _PLACEHOLDER_TOKEN.format(idx)

    def _stash_text(fragment: str) -> str:
        idx = len(stash)
        stash.append(fragment)
        return _PLACEHOLDER_TOKEN.format(idx)

    # Stash inline math first
    work = _INLINE_MATH_PLACEHOLDER_RE.sub(_stash, content)
    # Stash LaTeX commands (but NOT their text arguments for headings/captions)
    # Only stash commands that don't contain translatable text
    # Simple approach: stash \texttt{...}, \textit{short}, \ref{...}, \label{...}
    # but leave \textbf{long text} alone

    # Stash non-translatable commands
    for pattern in [
        re.compile(r"\\(?:ref|eqref|cite|pageref|cref|autoref|label)\s*\{[^}]*\}"),
        re.compile(r"\\texttt\{(?:[^{}]|\{[^{}]*\})*\}"),
        _SHORT_TEXTIT_RE,  # Short textit: değişken bütün, sözcükte kabuk (F3-03)
        re.compile(r"\\includegraphics\s*(?:\[[^\]]*\])?\s*\{[^}]*\}"),
        re.compile(r"\\(?:vspace\*?|hspace\*?)\{[^}]*\}"),
        # V2 denetimi (2026-09-29): adres metni çevrilmez (translator ile aynı).
        re.compile(r"\\(?:url|nolinkurl)\s*\{[^{}]*\}"),
    ]:
        if pattern is _SHORT_TEXTIT_RE:
            work = _stash_short_italics(work, _stash_text, source_lang)
        else:
            work = pattern.sub(_stash, work)

    # Check if remaining text (after stashing) has enough content to translate
    remaining = re.sub(r"\uE100RTSTASH_\d+\uE100", " ", work)
    # Also strip env placeholders for length check
    remaining = re.sub(r"\uE101RTENV_\d+\uE101", " ", remaining)
    remaining = re.sub(r"[{}\\]", " ", remaining)
    remaining = re.sub(r"\s+", " ", remaining).strip()

    # 2026-09-24: başlık için tarama döngüsüyle AYNI eşik (8 karakter / 2
    # kelime). Eskiden gövde eşiği (15/3) uygulanıyordu: "Potential Energy"
    # gibi 2 kelimelik başlıklar İngilizce diye bulunuyor ama MT'ye hiç
    # gönderilmeden "returned empty or identical" ile başarısız sayılıyordu
    # (bir test koşusunda 10 başarısızın 6'sı bu).
    min_chars, min_words = (8, 2) if is_heading else (15, 3)
    if len(remaining) < min_chars or len(remaining.split()) < min_words:
        # Not enough text to meaningfully translate
        # But still restore env stash before returning
        restored_content = _restore_env_names(content, env_stash)
        return leading_ws + restored_content

    # Translate the work string (with placeholders)
    # First, make placeholders safer for MT model
    safe_work = work
    placeholder_map: List[Tuple[str, str]] = []
    for idx in range(len(stash)):
        token = _PLACEHOLDER_TOKEN.format(idx)
        safe_token = f"<<RT_{idx}>>"
        safe_work = safe_work.replace(token, safe_token)
        placeholder_map.append((safe_token, stash[idx]))

    # Also make env placeholders safer for MT
    env_safe_map: List[Tuple[str, str, str]] = []
    for placeholder, original in env_stash:
        safe_env_token = f"<<RTENV_{len(env_safe_map)}>>"
        safe_work = safe_work.replace(placeholder, safe_env_token)
        env_safe_map.append((safe_env_token, placeholder, original))

    # Translate
    translated = translator_fn(safe_work)

    if not translated or not translated.strip():
        # Restore env stash before returning original
        restored_content = _restore_env_names(content, env_stash)
        return leading_ws + restored_content

    # V2 denetimi (2026-09-29): ters sıraya konmuş kısa italik kabuğu
    # ('<<RT_b>>sözcük<<RT_a>>') restore'dan ÖNCE düzeltilir (F3-03 kabuğu;
    # aksi hâlde '}sözcük\textit{' satıra girer: sayı dengeli, sıra bozuk).
    from latex_pipeline.translator import repair_italic_shell_order
    translated = repair_italic_shell_order(translated, stash, _RT_SAFE_TOKEN_ID_RE)

    # G2a-1 (2026-10-05, mt okuma MT-7 / MT-17): yer tutucu düzeyinde LaTeX
    # iskeleti, uydurma ilişki simgesi ve sayı kaybı denetlenir; tutmazsa
    # aynı metin bir kez daha → metin korumalı çeviri → satır aynen kalır.
    translated = _guarded_retranslation(
        safe_work, translated, translator_fn,
        lambda out: repair_italic_shell_order(out, stash, _RT_SAFE_TOKEN_ID_RE))
    if translated is None:
        restored_content = _restore_env_names(content, env_stash)
        return leading_ws + restored_content

    # Restore inline math/command placeholders
    for safe_token, original in placeholder_map:
        # Try exact match first
        if safe_token in translated:
            translated = translated.replace(safe_token, original, 1)
        else:
            # Try fuzzy match (MT model might mangle the placeholder)
            # Common mangling: spaces around <<>>, lowercase, etc.
            fuzzy_patterns = [
                re.escape(safe_token),
                re.escape(safe_token).replace("<<", r"<\s*<\s*").replace(">>", r"\s*>\s*>"),
                re.escape(safe_token.replace("<<", "").replace(">>", "")),  # stripped brackets (escaped)
            ]
            restored = False
            for fp in fuzzy_patterns:
                match = re.search(fp, translated)
                if match:
                    translated = translated[:match.start()] + original + translated[match.end():]
                    restored = True
                    break
            if not restored:
                # B-4 (2026-07-27): placeholder MT tarafindan yutulduysa
                # SATIRIN TAMAMINI orijinaliyle geri al — 'satir sonuna
                # ekleme' matematigi/referansi yanlis konuma tasiyordu
                # (translator.py'de ayni desen B-11 ile kaldirilmisti,
                # burasi acik kalmisti). Retranslation bu satir icin iptal.
                logger.warning(
                    "Retranslator: placeholder %s lost during translation — "
                    "line reverted to original (append kaldirildi, B-4).",
                    safe_token,
                )
                restored_content = _restore_env_names(content, env_stash)
                return leading_ws + restored_content

    # Restore env name placeholders
    for safe_env_token, internal_placeholder, original in env_safe_map:
        if safe_env_token in translated:
            translated = translated.replace(safe_env_token, original, 1)
        else:
            # Try fuzzy
            bare = safe_env_token.replace("<<", "").replace(">>", "")
            if bare in translated:
                translated = translated.replace(bare, original, 1)
            elif internal_placeholder in translated:
                translated = translated.replace(internal_placeholder, original, 1)
            else:
                # B-4 (2026-07-27): env placeholder kaybinda da satiri
                # orijinaliyle geri al (sona ekleme \begin/\end dengesini
                # yanlis konumda kuruyordu).
                logger.warning(
                    "Retranslator: env placeholder %s lost — line reverted "
                    "to original (append kaldirildi, B-4).", safe_env_token,
                )
                restored_content = _restore_env_names(content, env_stash)
                return leading_ws + restored_content

    # --- Session 5 fix: clean prompt leaks from retranslated output ---
    translated = _clean_prompt_leaks(translated, source=content)

    # --- EPUB placeholder hallucination cleanup ---
    # HY-MT 7B sometimes hallucinates EPUB-style placeholders like
    # @Bilinmeyen0000, @Unknown123, @@KNOWN0001@@, KNOWN0001@@.
    # B-5: kaynak satirda gercekten var olan @token'lar korunur.
    translated = _clean_epub_placeholder_hallucinations(translated, original=content)

    if not translated or not translated.strip():
        # Prompt leak cleaning emptied the line -- keep original
        restored_content = _restore_env_names(content, env_stash)
        return leading_ws + restored_content

    return leading_ws + translated


# ---------------------------------------------------------------------------
# Heading retranslation via the heading authority (2026-09-24)
# ---------------------------------------------------------------------------

# Başlık metninde MT'ye gitmeyecek parçalar: inline math, \ref/\cite/\label,
# \texttt{...}, kısa \textit{x} (değişken adı). Aynı seçim _retranslate_line'da.
# Kısa italik (≤8 karakter, boşluksuz): değişken/sembol bütün korunur,
# vurgulu gerçek sözcükte yalnız kabuk ('\textit{' + '}') korunur, sözcük
# cümlesiyle çevrilir (F3-03; karar translator._short_italic_is_symbol).
_SHORT_TEXTIT_RE = re.compile(r"\\textit\{([^{}\s]{1,8})\}")


def _stash_short_italics(text: str, push: Callable[[str], str],
                         source_lang: str = "en") -> str:
    """Kısa italikleri ``push(parça) -> yer tutucu`` ile koru (F3-03)."""
    from latex_pipeline.translator import _short_italic_is_symbol

    def _repl(m: re.Match) -> str:
        if _short_italic_is_symbol(m.group(1), source_lang):
            return push(m.group(0))
        return push("\\textit{") + m.group(1) + push("}")
    return _SHORT_TEXTIT_RE.sub(_repl, text)


_HEADING_PROTECT_PATTERNS = [
    re.compile(r"\$(?:[^$\\]|\\.)+\$"),
    re.compile(r"\\(?:ref|eqref|cite|pageref|cref|autoref|label)\s*\{[^}]*\}"),
    re.compile(r"\\texttt\{(?:[^{}]|\{[^{}]*\})*\}"),
    _SHORT_TEXTIT_RE,
]
_LATEX_PH_RE = re.compile(r"<<LATEX_\d+>>")
_LATEX_PH_ID_RE = re.compile(r"<<LATEX_(\d+)>>")
# _retranslate_line'ın MT'ye giden güvenli yer tutucusu (<<RT_n>>), kimlikli.
_RT_SAFE_TOKEN_ID_RE = re.compile(r"<<RT_(\d+)>>")


def _retranslate_heading_line(
    line: str, heading_translator_fn: Callable[[str], str],
    source_lang: str = "en",
) -> str:
    r"""Başlık satırının yalnız başlık METNİNİ başlık otoritesiyle yeniden çevir.

    Kabuk (``\section{ ... }\label{...}``) ve numara öneki ("2.4 ") aynen
    korunur; inline math / \ref / kısa ``\textit{n}`` parçaları
    ``<<LATEX_N>>`` token'ıyla korunur (başlık otoritesi token'ları opak
    geçirir). Token kümesi bozulursa ya da çeviri boşsa satır aynen döner;
    kabul kapısı (İngilizce mi, değişti mi) çağıranda.
    """
    leading_ws = line[:len(line) - len(line.lstrip())]
    content = line.lstrip()
    shell = _split_heading_shell(content)
    if not shell:
        return line
    cmd_open, title, tail = shell
    num_m = _HEADING_NUMBER_PREFIX_RE.match(title)
    prefix = num_m.group(1) if num_m else ""
    bare = title[len(prefix):].strip()
    if not bare:
        return line

    stash: List[str] = []

    def _stash(m: re.Match) -> str:
        stash.append(m.group(0))
        return f"<<LATEX_{len(stash) - 1}>>"

    def _push(fragment: str) -> str:
        stash.append(fragment)
        return f"<<LATEX_{len(stash) - 1}>>"

    work = bare
    for pat in _HEADING_PROTECT_PATTERNS:
        if pat is _SHORT_TEXTIT_RE:
            work = _stash_short_italics(work, _push, source_lang)
        else:
            work = pat.sub(_stash, work)

    translated = heading_translator_fn(work)
    if not translated or not translated.strip():
        return line
    translated = translated.strip()

    ph_in = sorted(_LATEX_PH_RE.findall(work))
    ph_out = sorted(_LATEX_PH_RE.findall(translated))
    if ph_in == ph_out:
        # V2 denetimi (2026-09-29): ters sıraya konmuş kısa italik kabuğu
        # ('<<b>>sözcük<<a>>') düzeltilir; aksi hâlde '}sözcük\textit{'.
        from latex_pipeline.translator import repair_italic_shell_order
        translated = repair_italic_shell_order(translated, stash, _LATEX_PH_ID_RE)
    if ph_in != ph_out:
        logger.warning(
            "Retranslator: heading placeholder set changed (%s -> %s), "
            "keeping original.", ph_in, ph_out,
        )
        return line
    # G2a-1 (2026-10-05, mt okuma MT-7 / MT-17): başlık metninde de LaTeX
    # iskeleti, uydurma ilişki simgesi ve sayı kaybı denetlenir (gövde
    # satırıyla aynı sıra); yeni deneme yer tutucu kümesini yine korumalı.
    from latex_pipeline.translator import repair_italic_shell_order

    def _heading_fn(text: str) -> str:
        return (heading_translator_fn(text) or "").strip()

    guarded = _guarded_retranslation(
        work, translated, _heading_fn,
        lambda out: repair_italic_shell_order(out, stash, _LATEX_PH_ID_RE))
    if guarded is None or sorted(_LATEX_PH_RE.findall(guarded)) != ph_in:
        return line
    translated = guarded
    for idx, original in enumerate(stash):
        translated = translated.replace(f"<<LATEX_{idx}>>", original, 1)
    return leading_ws + cmd_open + prefix + translated + "}" + tail


# ---------------------------------------------------------------------------
# Heading sentence-case enforcement (FIX 5 -- session 10, 2026-03-05)
# ---------------------------------------------------------------------------

# Turkish-aware uppercase map for first-char capitalization.
# Python's str.upper() handles most, but 'i'->'I' is wrong for Turkish
# (should be 'i'->'I' but 'ı'->'I' is also standard -- the critical one
# is that we must NOT miss ö,ş,ç,ğ,ü,ı).
_TR_UPPER_MAP = str.maketrans(
    "abcçdefgğhıijklmnoöprsştuüvyz",
    "ABCÇDEFGĞHIİJKLMNOÖPRSŞTUÜVYZ",
)


def _tr_upper_first(s: str) -> str:
    """Uppercase the first character of *s* using Turkish locale rules."""
    if not s:
        return s
    first = s[0]
    upper_first = first.translate(_TR_UPPER_MAP)
    return upper_first + s[1:]


# İlk kelimeyi kesen sinir: bosluk ve LaTeX ayraclari.
_HEADING_FIRST_WORD_RE = re.compile(r"[^\s{}$\\]*")


def _heading_upper_first(title: str) -> str:
    """Baslik ilk harfini buyut; ilk kelime Ingilizce/kod gorunumluyse
    ASCII ``I`` kullan (RT-F6, 2026-07-28).

    Turkce ``i -> İ`` esleme cevrilememis Ingilizce basligi bozuyordu
    ('introduction' -> 'İntroduction'). Karar yalnizca ilk harf 'i' iken
    verilir; diger tum harflerde davranis aynen korunur.
    """
    if not title or title[0] != "i":
        return _tr_upper_first(title)

    word = _HEADING_FIRST_WORD_RE.match(title).group(0)
    if any(c.isupper() for c in word[1:]):
        # iOS / iPhone gibi marka-kod adi: buyutmek her iki yonde de yanlis.
        return title

    core = word.lower().strip(".,;:!?()[]\"'")
    if any(c in _TURKISH_SPECIAL_CHARS for c in word):
        return _tr_upper_first(title)
    if core in _ENGLISH_HEADING_WORDS or core in _ENGLISH_FUNCTION_WORDS:
        return "I" + title[1:]
    if core in _TURKISH_COMMON_WORDS or _has_turkish_suffix(core):
        return _tr_upper_first(title)
    # Belirsiz: basligin tamamina bak. Turkce isaret yoksa ve bilinen
    # Ingilizce kelime varsa Ingilizce say; aksi halde Turkce (varsayilan).
    if (not any(c in _TURKISH_SPECIAL_CHARS for c in title)
            and _count_known_english_in_heading(title.split()) >= 1):
        return "I" + title[1:]
    return _tr_upper_first(title)


# Regex that matches the heading command OPENING (up to '{'). The title
# argument is then extracted with brace-depth counting — the old greedy
# '(.+)(\}\s*)$' pattern treated the LAST '}' on the line as the closing
# brace, so '\section{X}\label{sec:x}' leaked '}\label{sec:x' into the
# title sent to MT and the \label was silently destroyed (#36/#80).
_HEADING_OPEN_RE = re.compile(
    r"(\\(?:chapter|section|subsection|subsubsection|paragraph)\*?"
    r"\s*(?:\[[^\]]*\])?"      # optional short title
    r"\s*\{)"                  # opening brace
)


def _split_heading_shell(content: str):
    """'\\section{Title}\\label{x}' -> (open, title, tail) ya da None.

    Baslik argumaninin GERCEK kapanis brace'ini depth sayarak bulur;
    kuyruk (\\label vb.) kabugun parcasi olarak aynen korunur.
    """
    m = _HEADING_OPEN_RE.match(content)
    if not m:
        return None
    i = m.end()
    depth = 1
    j = i
    n = len(content)
    while j < n and depth > 0:
        c = content[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        j += 1
    if depth != 0:
        return None
    return m.group(1), content[i:j - 1], content[j:]


# ---------------------------------------------------------------------------
# Altyazı satırı (F1 MT-2/MT-3, 2026-09-30)
# ---------------------------------------------------------------------------
# Eskiden altyazı satırı ya "yapısal" sayılıp atlanıyordu (_PURE_COMMAND_RE
# 'caption' içerir; dil denetimi komutu argümanıyla birlikte siler) ya da —
# kapanış parantezinden sonra metin kalmışsa — satır HAM LaTeX'iyle MT'ye
# gidiyordu: model süslü parantezleri kaydırdı ("\caption*{\textbf{Şekil}
# 1.1:} Durağan…", hymt2-de) ya da komutu tümden sildi (gptoss-de: altyazı
# ve "Abbildung 1.1" etiketi kayboldu). Artık altyazı metni kabuğundan
# ayrılır; yalnız METİN (etiket öneki hariç) dil denetiminden ve yeniden
# çeviriden geçer, kabuk aynen korunur.
_CAPTION_OPEN_RE = re.compile(r"(\\caption\*?\s*(?:\[[^\]]*\])?\s*\{)")


def _split_caption_shell(content: str):
    """'\\caption*{Metin}\\label{x}' -> (açılış, metin, kuyruk) ya da None."""
    m = _CAPTION_OPEN_RE.match(content)
    if not m:
        return None
    i = m.end()
    depth = 1
    j = i
    n = len(content)
    while j < n and depth > 0:
        c = content[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        j += 1
    if depth != 0:
        return None
    return m.group(1), content[i:j - 1], content[j:]


_CAPTION_LABEL_RE: Optional["re.Pattern[str]"] = None


def _caption_label_end(text: str) -> int:
    """Altyazı metninin başındaki Türkçe atıf etiketi önekinin bitişi.

    "Şekil 1.1: …", "\\textbf{Şekil 1.1:} …", "Tablo 2 – …" -> etiketten
    sonraki ilk konum; önek yoksa 0. Etiket kümesinin tek kaynağı
    ``translator._FIGURE_LABEL_MAP`` (``_mask_xref_labels`` ile aynı)."""
    global _CAPTION_LABEL_RE
    if _CAPTION_LABEL_RE is None:
        from latex_pipeline.translator import _FIGURE_LABEL_MAP
        labels = "|".join(re.escape(lb) for lb in sorted(
            set(_FIGURE_LABEL_MAP.values()), key=len, reverse=True))
        num = r"[\s~ ]*\(?\d+(?:\.\d+)*[a-z]?\)?"
        punct = r"\s*[:.–—-]?"
        _CAPTION_LABEL_RE = re.compile(
            r"\s*(?:\\(?:textbf|textit|emph)\{\s*(?:" + labels + r")" + num
            + punct + r"\s*\}" + punct + r"|(?:" + labels + r")" + num
            + punct + r")\s*")
    m = _CAPTION_LABEL_RE.match(text)
    return m.end() if m else 0


_LATEX_CMD_NAME_RE = re.compile(r"\\[A-Za-z]+")


def _latex_skeleton_kept(old: str, new: str) -> bool:
    """Yeniden çeviri LaTeX iskeletini korudu mu? (komut adları + süslü
    parantez dengesi). Model komutu silip/çevirip parantez kaydırdıysa
    (gptoss-de, hymt2-de altyazıları) sonuç reddedilir."""
    if sorted(_LATEX_CMD_NAME_RE.findall(old)) != sorted(
            _LATEX_CMD_NAME_RE.findall(new)):
        return False
    from core.lang_detect import braces_balanced
    return braces_balanced(new) == braces_balanced(old)


def _caption_fragment_untranslated(fragment: str, source_lang: str) -> bool:
    """Altyazı metni (ya da kuyruğu) kaynak dilde mi? Altyazı başlık gibi
    kısadır: başlık eşikleri (8 karakter / 2 kelime) ve başlık kuralları;
    Türkçe atıf etiketi dil kararına katılmaz."""
    clean = _mask_xref_labels(_strip_latex_for_language_check(fragment))
    clean = re.sub(r"\s+", " ", clean).strip(" :.-–—")
    if len(clean) < 8 or len(clean.split()) < 2:
        return False
    return is_untranslated_line(clean, source_lang, min_words=2,
                                min_length=8, is_heading=True)


def _retranslate_caption_line(
    i: int, line: str, shell: tuple, output_lines: List[str],
    result: "RetranslateResult", translator_fn: Callable[[str], str],
    source_lang: str, dry_run: bool,
) -> None:
    """Altyazı satırında kaynak dilde kalmış metni (ve kuyruğu) yeniden çevir.

    Yalnız metin MT'ye gider; kabuk ("\\caption*{", kısa altyazı, "}",
    \\label) ve Türkçe etiket öneki ("Şekil 1.2:") aynen kalır. Sonuç hâlâ
    kaynak dildeyse ya da LaTeX iskeleti bozulduysa parça özgün hâliyle
    korunur (kayıp yok)."""
    leading_ws = line[:len(line) - len(line.lstrip())]
    cmd_open, inner, tail = shell
    lab_end = _caption_label_end(inner)
    parts = {"text": inner[lab_end:], "tail": tail}
    flagged = [k for k, frag in parts.items()
               if _caption_fragment_untranslated(frag, source_lang)]
    if not flagged:
        return
    line_num = i + 1
    result.english_lines_found += 1
    logger.info("Retranslator: L%d altyazısı kaynak dilde (%s) kalmış: %.80s",
                line_num, source_lang or "en", parts[flagged[0]].strip())
    if dry_run:
        result.changes.append((line_num, line.strip(),
                               "[DRY RUN - not translated]"))
        return
    fixed = 0
    for key in flagged:
        frag = parts[key]
        try:
            new = _retranslate_line(frag, translator_fn, is_heading=True,
                                    source_lang=source_lang)
        except Exception as exc:
            logger.error("Retranslator: L%d altyazı çevirisi hata: %s",
                         line_num, exc)
            continue
        if (new and new.strip() and new != frag
                and _latex_skeleton_kept(frag, new)
                and not _caption_fragment_untranslated(new, source_lang)):
            # Kuyruk metni kapanış parantezine yapışmasın.
            if key == "tail" and frag[:1].isspace() and not new[:1].isspace():
                new = " " + new
            parts[key] = new
            fixed += 1
    if not fixed:
        result.failed_lines += 1
        logger.warning("Retranslator: L%d altyazı yeniden çeviride "
                       "düzelmedi, özgün hâli korundu.", line_num)
        return
    new_line = (leading_ws + cmd_open + inner[:lab_end] + parts["text"]
                + "}" + parts["tail"])
    output_lines[i] = new_line
    result.changes.append((line_num, line.strip(), new_line.strip()))
    if fixed == len(flagged):
        result.retranslated_lines += 1
        logger.info("Retranslator: L%d altyazı yeniden çevrildi.", line_num)
    else:
        result.failed_lines += 1
        logger.warning("Retranslator: L%d altyazının %d/%d parçası düzeldi.",
                       line_num, fixed, len(flagged))


# Leading number/label pattern in heading titles: "2.3 ", "A.1 ", "10.2.1 ".
# En az bir rakam ya da noktali etiket sarti var — eski hali HERHANGI bir
# bastaki ASCII kelimeyi ('yapay ') onek sayip IKINCI kelimeyi
# buyutuyordu (#37, eski #78).
_HEADING_NUMBER_PREFIX_RE = re.compile(
    r"^(\s*(?:\d+(?:\.[A-Za-z0-9]+)*\.?|[A-Za-z](?:\.[A-Za-z0-9]+)+\.?)\s+)"
)


def _ensure_heading_case(line: str) -> str:
    """Ensure heading titles start with an uppercase letter (sentence case).

    Applies Turkish-aware capitalization to the first alphabetic character
    in the heading title.  If the title starts with a number prefix
    (e.g. "2.3 ortogonal"), capitalizes the first letter after the number.

    Does NOT convert to Title Case -- only the very first letter is touched.
    """
    shell = _split_heading_shell(line.strip())
    if shell is None:
        return line
    prefix = line[:len(line) - len(line.lstrip())]  # preserve indentation
    cmd_part, title, tail = shell

    # Check for number prefix
    num_m = _HEADING_NUMBER_PREFIX_RE.match(title)
    if num_m:
        num_prefix = num_m.group(1)
        rest = title[len(num_prefix):]
        if rest:
            rest = _heading_upper_first(rest)
        title = num_prefix + rest
    else:
        title = _heading_upper_first(title)

    return prefix + cmd_part + title + "}" + tail


# ---------------------------------------------------------------------------
# CLI interface for standalone testing
# ---------------------------------------------------------------------------

def _cli_main():
    """Command-line interface for testing the retranslator."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Scan translated .tex for English lines and retranslate them.",
    )
    parser.add_argument("tex_file", help="Path to translated .tex file")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only detect English lines, do not retranslate",
    )
    parser.add_argument(
        "--output", "-o",
        help="Output file path (default: overwrite input)",
    )
    parser.add_argument(
        "--min-length",
        type=int,
        default=20,
        help="Minimum cleaned text length to consider (default: 20)",
    )
    parser.add_argument(
        "--min-words",
        type=int,
        default=4,
        help="Minimum word count to consider (default: 4)",
    )
    # 2026-09-25: yeniden çeviri istenen yerel modelle yapılabilir (kimlik
    # biçimleri translate_book --mt-engine ile aynı). Varsayılan çağrı anında
    # registry.DEFAULT_ENGINE'den okunur (tek kaynak).
    from core.mt_engines import registry as mt_registry

    parser.add_argument(
        "--mt-engine",
        default=None,
        metavar="KİMLİK",
        help=f"Çeviri modeli (varsayılan: {mt_registry.DEFAULT_ENGINE}). Adlı motor "
             "ya da önekli kimlik: mlx:<klasör>, gguf:<dosya>, ollama:<model>, "
             "lmstudio:<model>, openai:<adres>|<model> (yalnız bu bilgisayar). "
             "Açıkça verilen model, yüklenmeden önce denetlenir.",
    )
    # 2026-09-28 (K5, KARARLAR 14): kitabın kaynak dili; çevrilmemiş satır o
    # dilde aranır, istem o dilden çevirir. Seçenekler tek kaynaktan.
    from core.lang_detect import TEX_SOURCE_LANGS, language_list_tr

    parser.add_argument(
        "--kaynak-dil",
        dest="kaynak_dil",
        choices=TEX_SOURCE_LANGS,
        default="en",
        metavar="KOD",
        help="Kitabın kaynak dili (varsayılan: en). Seçenekler: "
             f"{', '.join(TEX_SOURCE_LANGS)} "
             f"({language_list_tr(TEX_SOURCE_LANGS)}). Hedef dil her zaman Türkçe.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    from pathlib import Path
    tex_path = Path(args.tex_file)
    if not tex_path.exists():
        logger.error("File not found: %s", tex_path)
        sys.exit(1)

    tex_content = tex_path.read_text(encoding="utf-8")

    if args.dry_run:
        # Dry run: just detect, use a dummy translator
        result = scan_and_retranslate(
            tex_content,
            translator_fn=lambda t: t,  # identity -- won't be called in dry_run
            min_line_length=args.min_length,
            min_words=args.min_words,
            dry_run=True,
            source_lang=args.kaynak_dil,
        )
    else:
        # Real run: need a translator
        # Use LatexTranslator (which wraps BookTranslator internally)
        try:
            from latex_pipeline.config import PipelineConfig
            from latex_pipeline.translator import LatexTranslator

            mt_engine = args.mt_engine or mt_registry.DEFAULT_ENGINE
            if args.mt_engine:
                # Açıkça verilen kimlik model YÜKLENMEDEN denetlenir (kimlik
                # geçerli mi, klasör/dosya var mı, sunucu açık ve model
                # listede mi — translate_book ön kontrolüyle aynı; Türkçe ileti).
                from core.mt_engines import discovery as mt_discovery
                check = mt_discovery.check_engine(mt_engine)
                if not check.ok:
                    logger.error("%s", check.reason)
                    if check.hint:
                        logger.error("İpucu: %s", check.hint)
                    sys.exit(1)

            pipeline_config = PipelineConfig(
                output_dir=str(tex_path.parent.parent),
                source_lang=args.kaynak_dil,
                target_lang="tr",
            )
            lt = LatexTranslator(
                pipeline_config=pipeline_config,
                mt_engine=mt_engine,
                source_lang=args.kaynak_dil,
                target_lang="tr",
                project_name=tex_path.parent.parent.name or "default",
            )

            def translate_fn(text: str) -> str:
                return lt._safe_translate(text)

            result = scan_and_retranslate(
                tex_content,
                translator_fn=translate_fn,
                min_line_length=args.min_length,
                min_words=args.min_words,
                source_lang=args.kaynak_dil,
            )
        except ImportError as exc:
            logger.error("Cannot import LatexTranslator: %s", exc)
            logger.error("Use --dry-run to only detect English lines.")
            sys.exit(1)

    # Print summary
    print(f"\n{'=' * 60}")
    print(f"Retranslator Summary")
    print(f"{'=' * 60}")
    print(f"Total lines:        {result.total_lines}")
    print(f"Scanned lines:      {result.scanned_lines}")
    print(f"English detected:   {result.english_lines_found}")
    print(f"Retranslated:       {result.retranslated_lines}")
    print(f"Failed:             {result.failed_lines}")
    print(f"Skipped (math/bib): {result.skipped_math}")
    print(f"Skipped (command):  {result.skipped_command}")
    print(f"Skipped (short):    {result.skipped_short}")
    print(f"Heading-case fixes: {result.heading_case_fixes}")

    if result.changes:
        print(f"\nChanges:")
        for line_num, old, new in result.changes:
            print(f"  L{line_num}:")
            print(f"    OLD: {old[:100]}")
            print(f"    NEW: {new[:100]}")

    # Save output
    if not args.dry_run and result.retranslated_lines > 0:
        out_path = Path(args.output) if args.output else tex_path
        out_path.write_text(result.output_tex, encoding="utf-8")
        print(f"\nOutput saved: {out_path}")
    elif args.dry_run:
        print(f"\nDry run -- no changes written.")


if __name__ == "__main__":
    _cli_main()
