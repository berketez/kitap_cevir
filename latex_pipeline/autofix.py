"""
Automatic fix module for LaTeX translation QC issues.

Takes QC issues from qc_engine.py and applies fixes automatically.
Runs in a convergence loop (max 3 iterations) until no more fixable
issues remain or score gains become negligible.

Fix registry:
    prompt_leakage      - Remove known prompt patterns (regex delete)
    caption_inconsistency - Normalize English captions to Turkish equivalents
    placeholder_orphan  - Remove <<LATEX_N>>, [[BIRIM_N]], @...@@ artifacts
    untranslated        - Mark for re-translate (if translator provided)

Safety:
    - NEVER modifies content inside math environments
    - Uses word boundaries for replacements to avoid partial matches
    - Tracks every fix applied (old_text -> new_text) for auditability
"""

from __future__ import annotations

import bisect
import copy
import difflib
import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

# Modül sabiti TAM listedir (Yol B sözleşmesi — testler/dış çağıranlar
# bunu doğrular); manuel modda MT-only alt küme çalışma anında ÇIKARILIR.
from latex_pipeline.patterns import ALL_PROMPT_LEAK_PATTERNS as PROMPT_LEAK_PATTERNS
from latex_pipeline.qc_engine import (
    QCEngine,
    QCReport,
    QCIssue,
    collect_defined_env_names,
    may_have_repetition_loop,
    KNOWN_LONGER_COMMANDS,
)
# F25: kısa ASCII taban filtresinden geçen Türkçe işlev sözcüklerini
# ("bir", "bu", "ile", ...) elemek için tek kaynak — kopyalanmaz.
from latex_pipeline.qc_engine import _TURKISH_COMMON_WORDS
# R6a: '$' / '$$' okuma kuralının tek kaynağı (sanitizer stash'i de bunu kullanır).
from latex_pipeline.sanitizer import dollar_math_spans

logger = logging.getLogger(__name__)


def _inline_math_zones(line: str) -> "set[Tuple[int, int]]":
    """Satırdaki GERÇEK tek-dolar matematik bölgeleri: ``{(açılış, bitiş)}``.

    ``line[açılış:bitiş]`` ``$...$`` bölgesinin tamamıdır. R6a (kalite
    2026-09-25): autofix'in ``\\$...\\$`` desenleri TeX okuma sırasını
    bilmiyordu; '$${}^{^1}$$ Türkçe dipnot ... ise  $\\lambda$' satırında
    iki bölgenin ARASINDAKİ düzyazıyı matematik sanıp birleştiriyor, dipnot
    metni display matematiğe düşüyordu (sentetik de: "Display math should
    end with $$", harfleri eksik boşluksuz metin). Bir onarım yalnız bu
    kümedeki bölgelere uygulanır.
    """
    return {(s, e) for s, e, display in dollar_math_spans(line) if not display}


# Üst/alt simge türü: '^{..}', '^x', "'" (üs), '_{..}', '_x'.
def _trailing_script_kinds(expr: str) -> "set[str]":
    """İfadenin SON çekirdeğine bağlı simge türleri (``{'^'}``, ``{'_'}``...).

    ``m^{2}`` -> ``{'^'}``; ``x_{i}^{2}`` -> ``{'_', '^'}``; ``f'`` -> ``{'^'}``.
    Aynı türden ikinci simge TeX'te "Double superscript/subscript" hatasıdır.
    """
    kinds: set = set()
    s = expr.rstrip()
    while s:
        if s.endswith("'"):
            kinds.add("^")
            s = s[:-1]
            continue
        if s.endswith("}"):
            depth, i = 0, len(s) - 1
            while i >= 0:
                c = s[i]
                if c == "}" and (i == 0 or s[i - 1] != "\\"):
                    depth += 1
                elif c == "{" and (i == 0 or s[i - 1] != "\\"):
                    depth -= 1
                    if depth == 0:
                        break
                i -= 1
            head = s[:i].rstrip() if i > 0 else ""
            if head.endswith(("^", "_")) and not head.endswith(("\\^", "\\_")):
                kinds.add(head[-1])
                s = head[:-1]
                continue
            break
        m = _TRAILING_BARE_SCRIPT_RE.search(s)
        if m:
            kinds.add(m.group(1))
            s = s[:m.start()]
            continue
        break
    return kinds


# Son çekirdeğe ayraçsız bağlı simge: '^2', '_i', '^\alpha' (kaçışlı '\^'/'\_' değil).
_TRAILING_BARE_SCRIPT_RE = re.compile(
    r"(?<!\\)([\^_])\s*(?:\\[A-Za-z]+|[^\s{}\\])$")


def _leading_script_kinds(expr: str) -> "set[str]":
    """İfadenin BAŞINDAKİ (tabansız) simge türleri: ``^{K}_{i}`` -> ``{'^', '_'}``."""
    kinds: set = set()
    s = expr.lstrip()
    while s and s[0] in "^_":
        kinds.add(s[0])
        rest = s[1:].lstrip()
        if rest.startswith("{"):
            depth = 0
            for i, c in enumerate(rest):
                if c == "{" and (i == 0 or rest[i - 1] != "\\"):
                    depth += 1
                elif c == "}" and rest[i - 1] != "\\":
                    depth -= 1
                    if depth == 0:
                        break
            s = rest[i + 1:].lstrip()
        elif rest.startswith("\\"):
            m = re.match(r"\\[A-Za-z]+|\\.", rest)
            s = rest[m.end():].lstrip() if m else ""
        else:
            s = rest[1:].lstrip()
    return kinds


def _join_math_scripts(first: str, second: str) -> str:
    """``first`` + ``second`` birleşimi; ikinci ifadenin baştaki simgesi
    birincinin son çekirdeğinde zaten varsa araya boş çekirdek ``{}`` konur.

    R6b (kalite 2026-09-25): '$m^{2}$ ${}^{K}$' önce '{}' temizliğiyle
    '$m^{2}$ $^{K}$', sonra birleştirmeyle '$m^{2}^{K}$' oluyordu ->
    "Double superscript" (sentetik de book_tr.tex:315, sentetik it:224).
    """
    if _trailing_script_kinds(first) & _leading_script_kinds(second):
        return first + "{}" + second
    return first + second

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_MAX_ITERATIONS = 5
MIN_SCORE_GAIN = 0.5
DEFAULT_MIN_QC_SCORE = 90.0  # Minimum acceptable score from config

# ---------------------------------------------------------------------------
# Math environment detection -- used to protect math from edits
# ---------------------------------------------------------------------------

# Named math environments
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

# Display math: \[ ... \]
_DISPLAY_MATH_RE = re.compile(r"\\\[.*?\\\]", re.DOTALL)

# Inline math: $...$ (not \$ or $$)
_INLINE_MATH_RE = re.compile(r"(?<![\$\\])\$(?!\$)(.+?)(?<![\$\\])\$(?!\$)")

# Display math: $$...$$
_DISPLAY_MATH_DOLLAR_RE = re.compile(r"\$\$.*?\$\$", re.DOTALL)

# Code / verbatim environments -- must be protected from edits like math
_CODE_ENV_RE = re.compile(
    r"\\begin\{(lstlisting|verbatim|minted)\}"
    r"(.*?)"
    r"\\end\{\1\}",
    re.DOTALL,
)

# F4/F5 (2026-07-28): gövde-geneli temizlikler (satır silme, boşluk/boş
# satır collapse) verbatim/lstlisting/minted İÇİNE sızmamalı — kod
# girintisi ve '...' gibi satırlar sessizce bozuluyordu. 2026-07-11 #15
# düzeltmesindeki \x00VERB stash deseni ortak yardımcıya alındı.
_VERBATIM_STASH_RE = re.compile(
    r"\\begin\{(verbatim|lstlisting|minted)\}.*?\\end\{\1\}",
    re.DOTALL,
)


def _cleanup_outside_verbatim(body: str, cleanup: Callable[[str], str]) -> str:
    """Temizliği verbatim/lstlisting/minted blokları stash'lenmiş uygular."""
    stash: List[str] = []

    def _save(m: "re.Match") -> str:
        stash.append(m.group(0))
        return f"\x00VERB{len(stash) - 1}\x00"

    body = _VERBATIM_STASH_RE.sub(_save, body)
    body = cleanup(body)
    for i, chunk in enumerate(stash):
        body = body.replace(f"\x00VERB{i}\x00", chunk)
    return body


def _find_math_spans(text: str) -> List[Tuple[int, int]]:
    """Return a sorted list of (start, end) spans covering all math regions."""
    spans: List[Tuple[int, int]] = []
    for pattern in (
        _MATH_ENV_RE,
        _DISPLAY_MATH_RE,
        _DISPLAY_MATH_DOLLAR_RE,
        _INLINE_MATH_RE,
        _CODE_ENV_RE,
    ):
        for m in pattern.finditer(text):
            spans.append((m.start(), m.end()))
    # Sort and merge overlapping spans
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


# Turkish-aware case mapping for the FIRST letter of a word.
# Python's str.upper() is ASCII-locale: 'i'.upper() == 'I' (wrong for Turkish,
# should be 'İ') and 'ı'.upper() == 'I' as well. These maps fix the dotted/
# dotless-i pair so sentence-initial capitalization stays correct.
_TR_UPPER_MAP = {"i": "İ", "ı": "I"}
_TR_LOWER_MAP = {"İ": "i", "I": "ı"}


def _tr_upper_first(word: str) -> str:
    """Uppercase the first character using Turkish İ/ı rules, keep the rest."""
    if not word:
        return word
    first = word[0]
    up = _TR_UPPER_MAP.get(first) or first.upper()
    return up + word[1:]


def _tr_upper(s: str) -> str:
    """Uppercase a whole string with Turkish İ/ı rules ('için' -> 'İÇİN')."""
    return s.replace("i", "İ").replace("ı", "I").upper()


def _make_case_preserving_repl(repl: str):
    """Return a re.sub replacer that keeps the matched term's leading case.

    If the matched text starts uppercase but the replacement starts
    lowercase, the replacement's first letter is uppercased using Turkish
    İ/ı rules (so 'için' -> 'İçin', not 'Için'). Prevents a lowercase map
    value from destroying a sentence-initial capital (AF-C6/AF-03).
    """
    def _repl(m: "re.Match") -> str:
        matched = m.group(0)
        if matched and matched[0].isupper() and repl and repl[0].islower():
            return _tr_upper_first(repl)
        return repl
    return _repl


def _is_inside_math(pos: int, math_spans: List[Tuple[int, int]]) -> bool:
    """Check if a position falls inside any math span (binary search)."""
    lo, hi = 0, len(math_spans) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        start, end = math_spans[mid]
        if pos < start:
            hi = mid - 1
        elif pos >= end:
            lo = mid + 1
        else:
            return True
    return False


# ---------------------------------------------------------------------------
# Tabular environment detection -- used to protect tables from edits
# ---------------------------------------------------------------------------

_TABULAR_ENV_BEGIN_RE = re.compile(
    r"\\begin\{(tabular|tabularx|longtable)\}"
)
_TABULAR_ENV_END_RE = re.compile(
    r"\\end\{(tabular|tabularx|longtable)\}"
)


def _restore_blocked_lines(lines: List[str]) -> set:
    """Satır içi matematiğin geri EKLENEMEYECEĞİ satırlar (0 tabanlı).

    R6c (kalite 2026-09-25): tablo satırları (``_build_tabular_line_set``),
    matematik ortamları (equation*, align ...), ``\\[...\\]``, ``$$...$$``
    ve kod ortamları. Eklenen ``$..$`` display matematik içinde derleme
    hatasıdır (bir test kitabında equation* içine '$h^2$'), tablo satırında
    hücre yapısını bozar (örnek: '\\toprule $K_m$ ...').
    """
    blocked = set(_build_tabular_line_set(lines))
    text = "\n".join(lines)
    spans = [(m.start(), m.end()) for pat in (_MATH_ENV_RE, _DISPLAY_MATH_RE,
                                             _CODE_ENV_RE)
             for m in pat.finditer(text)]
    spans += [(st, en) for st, en, display in dollar_math_spans(text) if display]
    if not spans:
        return blocked
    starts = []
    pos = 0
    for line in lines:
        starts.append(pos)
        pos += len(line) + 1
    for st, en in spans:
        first = bisect.bisect_right(starts, st) - 1
        last = bisect.bisect_right(starts, max(st, en - 1)) - 1
        blocked.update(range(max(first, 0), last + 1))
    return blocked


def _build_tabular_line_set(lines: List[str]) -> set:
    """Return a set of 0-based line indices that fall inside tabular environments.

    Scans *lines* for ``\\begin{tabular}``, ``\\begin{tabularx}``, and
    ``\\begin{longtable}`` and marks every line between the opening and
    closing tags (inclusive) as belonging to a tabular environment.
    """
    inside: set = set()
    in_tabular = False
    for idx, line in enumerate(lines):
        if not in_tabular:
            if _TABULAR_ENV_BEGIN_RE.search(line):
                in_tabular = True
                inside.add(idx)
        else:
            inside.add(idx)
            if _TABULAR_ENV_END_RE.search(line):
                in_tabular = False
    return inside


def _safe_sub(
    pattern: re.Pattern,
    repl,
    text: str,
    math_spans: List[Tuple[int, int]],
) -> Tuple[str, List[Tuple[str, str, int]]]:
    """Regex substitution that skips matches inside math environments.

    Returns (new_text, list_of_(old, new, original_pos) tuples).
    """
    changes: List[Tuple[str, str, int]] = []

    # Collect all matches, filter out those inside math, then rebuild the
    # string by applying the remaining replacements in REVERSE order so that
    # earlier positions are not shifted by later edits. (A single-pass
    # re.sub cannot skip math spans reliably, hence this manual approach.)
    matches = list(pattern.finditer(text))
    if not matches:
        return text, []

    # Filter out matches inside math
    safe_matches = [
        m for m in matches
        if not _is_inside_math(m.start(), math_spans)
    ]
    if not safe_matches:
        return text, []

    # Apply replacements in reverse order to preserve positions
    result = text
    for m in reversed(safe_matches):
        old = m.group(0)
        if callable(repl):
            new = repl(m)
        else:
            new = m.expand(repl)
        if old != new:
            result = result[:m.start()] + new + result[m.end():]
            changes.append((old, new, m.start()))

    # Reverse changes list so it's in forward order
    changes.reverse()
    return result, changes


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class FixResult:
    """Record of a single fix applied."""
    check_name: str
    line_number: int
    old_text: str
    new_text: str
    method: str  # "regex", "replace", "re-translate"


@dataclass
class AutoFixReport:
    """Summary of the autofix run."""
    iterations: int = 0
    total_fixes: int = 0
    fixes_per_iteration: List[int] = field(default_factory=list)
    converged: bool = False
    initial_score: float = 0.0
    final_score: float = 0.0
    min_qc_score: float = DEFAULT_MIN_QC_SCORE
    met_threshold: bool = False  # True if final_score >= min_qc_score
    fixes: List[FixResult] = field(default_factory=list)
    remaining_issues: List[QCIssue] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Prompt leakage patterns -- imported from patterns.py (DRY)
# ---------------------------------------------------------------------------
_PROMPT_LEAK_PATTERNS = PROMPT_LEAK_PATTERNS


# ---------------------------------------------------------------------------
# Caption normalization mapping (English -> Turkish)
# ---------------------------------------------------------------------------

# Each entry: (regex_pattern, replacement_string)
# Uses word boundary \b to avoid partial matches.
_CAPTION_NORMALIZATIONS: List[Tuple[re.Pattern, str]] = [
    # "Fig. N" / "Figure N" -> "Sekil~N"
    (re.compile(r"\bFig(?:ure)?\.?\s*(\d+(?:\.\d+)*)"), r"Sekil~\1"),
    # "Table N" -> "Tablo~N"
    (re.compile(r"\bTable\s+(\d+(?:\.\d+)*)"), r"Tablo~\1"),
    # "Eq. N" / "Equation N" / "Eq. (N)" -> "Denklem~N" / "Denklem~(N)"
    (re.compile(r"\bEq(?:uation)?\.?\s*(\(?\d+(?:\.\d+)*\)?)"), r"Denklem~\1"),
    # "Using Eq." -> "Denklem ... kullanılarak" (remove "Using")
    (re.compile(r"\bUsing\s+Denklem"), "Denklem"),
    # "Chapter N" -> "Bolum~N"
    (re.compile(r"\bChapter\s+(\d+(?:\.\d+)*)"), r"Bolum~\1"),
    # "Algorithm N" -> "Algoritma~N"
    (re.compile(r"\bAlgorithm\s+(\d+(?:\.\d+)*)"), r"Algoritma~\1"),
    # "Section N" -> "Bolum~N"
    (re.compile(r"\bSection\s+(\d+(?:\.\d+)*)"), r"Bolum~\1"),
    # "Theorem N" / "Theorem (N)" -> "Teorem~N"
    (re.compile(r"\bTheorem\s+(\(?\d+(?:\.\d+)*\)?)"), r"Teorem~\1"),
    # "Lemma N" -> "Lemma~N" (stays same but normalize spacing)
    (re.compile(r"\bLemma\s+(\d+(?:\.\d+)*)"), r"Lemma~\1"),
    # "Definition N" -> "Tanim~N"
    (re.compile(r"\bDefinition\s+(\d+(?:\.\d+)*)"), r"Tanim~\1"),
    # "Proposition N" -> "Onerme~N"
    (re.compile(r"\bProposition\s+(\d+(?:\.\d+)*)"), r"Onerme~\1"),
    # "Corollary N" -> "Sonuc~N"
    (re.compile(r"\bCorollary\s+(\d+(?:\.\d+)*)"), r"Sonuc~\1"),
    # "Proof" -> "Ispat" (standalone)
    (re.compile(r"\bProof\b"), "Ispat"),
    # "Example N" -> "Ornek~N"
    (re.compile(r"\bExample\s+(\d+(?:\.\d+)*)"), r"Ornek~\1"),
    # "Remark N" -> "Not~N"
    (re.compile(r"\bRemark\s+(\d+(?:\.\d+)*)"), r"Not~\1"),
    # "Appendix N" / "Appendix A" -> "Ek~N"
    (re.compile(r"\bAppendix\s+([A-Z0-9]+(?:\.\d+)?)"), r"Ek~\1"),
]

# Second pass: fix Turkish special characters that LaTeX needs
# These map ASCII approximations to proper LaTeX Turkish chars
_TURKISH_CHAR_FIXES: List[Tuple[re.Pattern, str]] = [
    # XeLaTeX + UTF-8: use native Turkish chars directly
    # Sekil -> Şekil
    (re.compile(r"\bSekil(~\d)"), r"Şekil\1"),
    # Bolum -> Bölüm
    (re.compile(r"\bBolum(~\d)"), r"Bölüm\1"),
    # Tanim -> Tanım
    (re.compile(r"\bTanim(~\d)"), r"Tanım\1"),
    # Onerme -> Önerme
    (re.compile(r"\bOnerme(~\d)"), r"Önerme\1"),
    # Sonuc -> Sonuç
    (re.compile(r"\bSonuc(~\d)"), r"Sonuç\1"),
    # Ispat -> İspat
    (re.compile(r"\bIspat\b"), r"İspat"),
    # Ornek -> Örnek
    (re.compile(r"\bOrnek(~\d)"), r"Örnek\1"),
    # Fix legacy LaTeX encoding back to UTF-8 (from previous runs)
    (re.compile(r"\\c\{S\}ekil"), "Şekil"),
    (re.compile(r'B\\"\{o\}l\\"\{u\}m'), "Bölüm"),
    (re.compile(r"Tan\{\\i\}m"), "Tanım"),
    (re.compile(r'\\"\{O\}nerme'), "Önerme"),
    (re.compile(r"Sonu\\c\{c\}"), "Sonuç"),
    (re.compile(r'\\"\{O\}rnek'), "Örnek"),
]


# ---------------------------------------------------------------------------
# Placeholder / artifact patterns
# ---------------------------------------------------------------------------

_PLACEHOLDER_ORPHAN_PATTERNS: List[re.Pattern] = [
    # <<LATEX_N>> leftover placeholders
    re.compile(r"<<LATEX_\d+>>"),
    # [[BIRIM_N]] / [[UNIT_N]] / [[FORMULA_N]] BookTranslator artifacts
    re.compile(r"\[\[(?:UNIT|B[Ii]R[Ii]M|FORMULA)_\d+\]\]"),
    # @...@@ proper-noun markers from BookTranslator
    re.compile(r"@([^@\n]{1,60})@@"),
    # Mangled LATEX placeholders: < < LATEX_42 > >, << LATEX_42>>, etc.
    re.compile(r"<\s*<\s*LATEX[_\s]*\d+\s*>\s*>"),
    # Guillemet format: «LATEX₁₂₇ >> (MT subscript digit bozulmasi)
    re.compile(r"\u00ab[A-Z]+[\u2080-\u2089]+\s*>{1,2}"),
]

# For @...@@ we keep the inner text, for others we delete entirely
_ATAT_PATTERN = re.compile(r"@([^@\n]{1,60})@@")


# ---------------------------------------------------------------------------
# Untranslated text detection helpers
# ---------------------------------------------------------------------------

def _extract_context_window(
    body: str, line_number: int, context_lines: int = 1
) -> Tuple[str, str, str]:
    """Extract context around a line: (before, target_line, after).

    Returns up to `context_lines` lines before and after the target line.
    """
    lines = body.split("\n")
    if line_number < 1 or line_number > len(lines):
        return "", "", ""
    idx = line_number - 1  # 0-based
    before_start = max(0, idx - context_lines)
    after_end = min(len(lines), idx + context_lines + 1)
    before = "\n".join(lines[before_start:idx])
    target = lines[idx]
    after = "\n".join(lines[idx + 1:after_end])
    return before, target, after


# ---------------------------------------------------------------------------
# AutoFixer
# ---------------------------------------------------------------------------

class AutoFixer:
    """Automatic fixer for LaTeX translation QC issues.

    Parameters
    ----------
    qc_engine : QCEngine, optional
        QC engine instance.  If None, a default one is created.
    translator : LatexTranslator, optional
        If provided, untranslated blocks can be surgically re-translated.
        If None, untranslated issues are flagged but not fixed.
    """

    def __init__(
        self,
        qc_engine: QCEngine = None,
        translator=None,
        min_qc_score: float = DEFAULT_MIN_QC_SCORE,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        manual_translation: bool = False,
        user_defined_envs=None,
    ):
        if qc_engine is None:
            qc_engine = QCEngine()
        self.qc_engine = qc_engine
        self.translator = translator
        self.min_qc_score = min_qc_score
        self.max_iterations = max_iterations
        self._source_body: Optional[str] = None
        # F2 (2026-07-28): gövde DIŞINDA (örn. preamble_ozel.tex) tanımlı
        # ortam adları — _fix_translated_env_name bunları yeniden adlandırmaz.
        # Gövde içi \newenvironment/\newtheorem tanımları ayrıca taranır.
        self.user_defined_envs: frozenset = frozenset(user_defined_envs or ())
        # Manual translation mode (e.g. --import-tex Claude-translated body):
        # skip glossary_compliance / wrong_translation handlers that were
        # designed for HY-MT output. They produce half-translations like
        # "hipergami Knows Best" or "Design ve layout" on clean human output.
        self.manual_translation = manual_translation

        # Fix registry: check_name -> fix method
        # Note: QC engine uses "caption_consistency" and "placeholder_orphans"
        # (plural), we register both forms for safety.
        self._fix_registry: Dict[str, Callable] = {
            "prompt_leakage": self._fix_prompt_leakage,
            "caption_consistency": self._fix_caption_inconsistency,
            "caption_inconsistency": self._fix_caption_inconsistency,
            "placeholder_orphans": self._fix_placeholder_orphan,
            "placeholder_orphan": self._fix_placeholder_orphan,
            "untranslated": self._fix_untranslated,
            "mixed_language_line": self._fix_mixed_language_line,
            "missing_images": self._fix_missing_images,
            "inline_math_loss": self._fix_inline_math_loss,
            "broken_latex_syntax": self._fix_broken_latex_syntax,
            # New checks (v2)
            "brace_balance": self._fix_brace_balance,
            "markdown_link_remnant": self._fix_markdown_link_remnant,
            "table_column_mismatch": self._fix_table_column_mismatch,
            "untranslated_heading": self._fix_untranslated_heading,
            "broken_inline_math": self._fix_broken_inline_math,
            "latex_encoded_turkish": self._fix_latex_encoded_turkish,
            "foreign_language_leakage": self._fix_foreign_language_leakage,
            "first_person_academic": self._fix_first_person_academic,
            "antonym_guard": self._fix_antonym_guard,
            "number_hallucination": self._fix_number_hallucination,
            # New checks (v3) -- translated env names and broken braces
            "translated_env_name": self._fix_translated_env_name,
            "broken_command_braces": self._fix_broken_command_braces,
            # New checks (v4) -- stash restore quality
            "stash_placeholder_orphan": self._fix_stash_placeholder_orphan,
            "word_concatenation": self._fix_word_concatenation,
            # New checks (v5) -- hallucination / repetition detection
            "hallucination": self._fix_segment_hallucination,
            # New checks (v6) -- ASCII degradation (Türkçe karakter kuralı)
            "ascii_degradation": self._fix_ascii_degradation,
            # New checks (v7) -- glossary compliance (session 9, 2026-03-05)
            "glossary_compliance": self._fix_glossary_compliance,
            # New checks (v8) -- wrong_translation uses same fix logic as
            # glossary_compliance (QC check 32 reports "Replace 'X' with 'Y'"
            # in fix_suggestion, which _fix_glossary_compliance parses).
            # Additionally has a hardcoded fallback map for known MT errors.
            "wrong_translation": self._fix_wrong_translation,
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        translated_body: str,
        source_body: str = None,
        progress_callback=None,
        initial_report: QCReport = None,
    ) -> Tuple[str, AutoFixReport]:
        """Run the autofix convergence loop.

        Parameters
        ----------
        translated_body : str
            The translated LaTeX body to fix.
        source_body : str, optional
            The original (source language) LaTeX body for comparison.
        progress_callback : callable, optional
            Called as ``progress_callback(fraction, description)``
            where fraction is 0.0-1.0.
        initial_report : QCReport, optional
            F17 (2026-07-28): AYNI ``translated_body`` + ``source_body`` +
            manuel bayrakla ÜRETİLMİŞ taze QC raporu. Verilirse baştaki
            analiz atlanır (çağıran zaten analiz ettiyse aynı gövde art
            arda 3 kez analiz edilmesin diye). Farklı gövde/parametreyle
            üretilmiş rapor GEÇİRİLMEMELİDİR.

        Returns
        -------
        (fixed_body, report) : Tuple[str, AutoFixReport]
        """
        report = AutoFixReport()
        current = translated_body
        prev_fix_hash: str | None = None
        self._source_body = source_body

        # Get initial score
        # A-17 (2026-07-27): iç QC çağrıları manuel bayrağı DÜŞÜRMESİN —
        # eskiden bayraksız analyze MT-feedback issue'larını auto_fixable
        # bırakıyor, koruma tek başına _MANUAL_TRANSLATION_SKIP listesine
        # kalıyordu (iki liste ayrışırsa sessiz delik).
        # F17 (2026-07-28): "current gövdesinin taze raporu" tek değişkende
        # (current_qc) taşınır; gövde değişmeden tekrar analyze edilmez.
        # analyze deterministik olduğundan davranış birebir aynı, tam
        # analiz sayısı ~2N+2'den ~N+1'e iner.
        if initial_report is not None:
            # H1 (2026-07-29): _apply_fixes satır-sayısı değişen kategoriden
            # sonra kalan issue'ların line_num'ını YERİNDE remap eder.
            # Çağıranın raporu (ve translate_book'un F36 QC cache'i)
            # kirlenmesin diye kopya üzerinde çalışılır.
            current_qc = copy.copy(initial_report)
            current_qc.issues = [copy.copy(i) for i in initial_report.issues]
        else:
            current_qc = self.qc_engine.analyze(current, source_body, manual_translation=self.manual_translation)
        report.initial_score = current_qc.score

        logger.info(
            "AutoFix starting. Initial QC score: %.1f, issues: %d",
            current_qc.score, len(current_qc.issues),
        )

        for iteration in range(self.max_iterations):
            report.iterations = iteration + 1

            if progress_callback:
                progress_callback(
                    iteration / self.max_iterations,
                    f"autofix: iteration {iteration + 1}/{self.max_iterations}",
                )

            # Analyze current state
            # F17: current_qc her zaman `current` gövdesinin raporu —
            # ilk iterasyonda baştaki analiz, sonrakilerde önceki
            # iterasyonun post_qc'si. Gövde arada değişmedi, yeniden
            # analiz gereksizdi.
            qc_report = current_qc

            # Filter to auto-fixable issues with high confidence
            fixable = [
                issue for issue in qc_report.issues
                if issue.auto_fixable and issue.confidence >= 0.75
            ]

            if not fixable:
                logger.info(
                    "Iteration %d: No fixable issues. Converged.",
                    iteration + 1,
                )
                report.converged = True
                break

            # Oscillation detection: hash the set of (check_name, line_num)
            # to detect if we keep finding the SAME issues, not just same count.
            fix_sig = "|".join(
                sorted(f"{i.check_name}:{i.line_num}" for i in fixable)
            )
            current_fix_hash = hashlib.md5(fix_sig.encode()).hexdigest()

            if current_fix_hash == prev_fix_hash:
                logger.info(
                    "Iteration %d: Same issues detected (hash %s). "
                    "Stopping to prevent oscillation.",
                    iteration + 1, current_fix_hash[:8],
                )
                break

            # --- Score regression guard ---
            # Snapshot BEFORE applying fixes so we can revert if score drops.
            snapshot = current

            # Apply fixes
            # H1 (2026-07-29): _apply_fixes AF-09 remap'i issue'ları YERİNDE
            # değiştirir. Kopyalarla çağrılır ki qc_report (dolayısıyla
            # initial_report ve translate_book'un F36 QC cache'i) hiçbir
            # yolda kirlenmesin; revert dalında rapor snapshot gövdesiyle
            # tutarlı kalır.
            current, iteration_fixes = self._apply_fixes(
                current, [copy.copy(i) for i in fixable])
            iteration_fix_count = len(iteration_fixes)

            logger.info(
                "Iteration %d: Applied %d fixes.",
                iteration + 1, iteration_fix_count,
            )

            # Check score gain (applies to ALL iterations including the first)
            post_qc = self.qc_engine.analyze(current, source_body, manual_translation=self.manual_translation)
            score_gain = post_qc.score - qc_report.score

            if score_gain < 0:
                # Score DROPPED -- revert to snapshot, discard this iteration
                logger.warning(
                    "Iteration %d: Score DROPPED by %.1f (%.1f -> %.1f). "
                    "Reverting %d fixes to protect quality.",
                    iteration + 1, abs(score_gain),
                    qc_report.score, post_qc.score,
                    iteration_fix_count,
                )
                current = snapshot
                # F17: gövde snapshot'a döndü — qc_report o gövdenin raporu
                current_qc = qc_report
                # Do NOT add these fixes to the report -- they were reverted
                report.fixes_per_iteration.append(0)
                break

            # F17: bundan sonra `current` gövdesinin taze raporu post_qc
            current_qc = post_qc

            if score_gain < MIN_SCORE_GAIN:
                logger.info(
                    "Iteration %d: Score gain %.1f < %.1f minimum. "
                    "Diminishing returns, stopping.",
                    iteration + 1, score_gain, MIN_SCORE_GAIN,
                )
                # Score did not drop, so keep the fixes but stop iterating
                report.fixes.extend(iteration_fixes)
                report.fixes_per_iteration.append(iteration_fix_count)
                report.total_fixes += iteration_fix_count
                break

            # Score improved sufficiently -- commit the fixes
            report.fixes.extend(iteration_fixes)
            report.fixes_per_iteration.append(iteration_fix_count)
            report.total_fixes += iteration_fix_count
            prev_fix_hash = current_fix_hash

        # Final analysis for remaining issues
        # F17: current_qc her çıkış yolunda `current` gövdesinin raporudur
        # (converge/oscillation: gövde değişmedi; revert: snapshot'a dönüldü;
        # commit/diminishing: post_qc atandı) — yeniden analyze gereksiz.
        final_qc = current_qc
        report.final_score = final_qc.score
        report.remaining_issues = [
            issue for issue in final_qc.issues
            if not issue.auto_fixable or issue.confidence < 0.75
        ]

        # Check convergence: no more fixable issues
        still_fixable = [
            issue for issue in final_qc.issues
            if issue.auto_fixable and issue.confidence >= 0.75
        ]
        if not still_fixable:
            report.converged = True

        # --- Post-convergence structural fixes (always run) ---
        pre_struct = current
        current, struct_fixes = self._apply_structural_fixes(current)
        report.fixes.extend(struct_fixes)
        report.total_fixes += len(struct_fixes)

        if struct_fixes and current != pre_struct:
            # Recompute final score after structural fixes.
            # F26 (2026-07-29): gövde gerçekten değiştiyse. Bir yapısal
            # düzeltici FixResult üretip metni aynı bırakırsa (idempotent
            # yeniden yazım) tam analiz tekrarı bedavaya gidiyordu.
            final_qc2 = self.qc_engine.analyze(current, source_body, manual_translation=self.manual_translation)
            report.final_score = final_qc2.score

        # Determine if minimum QC score threshold is met
        report.min_qc_score = self.min_qc_score
        report.met_threshold = report.final_score >= self.min_qc_score

        if progress_callback:
            progress_callback(1.0, "autofix: complete")

        logger.info(
            "AutoFix complete. Iterations: %d, Fixes: %d, "
            "Score: %.1f -> %.1f, Converged: %s, "
            "Threshold: %.0f (met=%s)",
            report.iterations, report.total_fixes,
            report.initial_score, report.final_score,
            report.converged,
            self.min_qc_score, report.met_threshold,
        )

        return current, report

    # ------------------------------------------------------------------
    # Structural fixes (QC-independent, always run)
    # ------------------------------------------------------------------

    # Turkish deasciification map for headings
    # Words that MT model commonly outputs in ASCII form
    _TURKISH_DEASCIIFY: List[Tuple[re.Pattern, str]] = [
        # Common word fixes (case-sensitive, word boundary)
        (re.compile(r"\bAlishtirmasi\b"), "Alıştırması"),
        (re.compile(r"\bAlistirmasi\b"), "Alıştırması"),
        (re.compile(r"\bFonksiyonlari\b"), "Fonksiyonları"),
        (re.compile(r"\bDogrusal\b"), "Doğrusal"),
        (re.compile(r"\bYakinlasma\b"), "Yakınlaşma"),
        (re.compile(r"\bInisi\b"), "İnişi"),
        (re.compile(r"\bIleri\b"), "İleri"),
        (re.compile(r"\bYontemleri\b"), "Yöntemleri"),
        (re.compile(r"\bSiniflandirma\b"), "Sınıflandırma"),
        (re.compile(r"\bKonvolusyonlar\b"), "Konvolüsyonlar"),
        (re.compile(r"\bKonvolusyon\b"), "Konvolüsyon"),
        (re.compile(r"\bKatmanlari\b"), "Katmanları"),
        # DATA-05: removed ~12 identity/no-op entries that mapped a word to
        # itself (Regresyon, Aktivasyon, Optimizasyon, Diferansiyel,
        # Denklemler, Transpoze, Teoremleri, Hesaplama, Stokastik, Ortalama,
        # Notlar, Saf), a duplicate '\bInisi\b', and the hidden translation
        # 'Pooling' -> 'Havuzlama' (a term translation, not a deasciify; the
        # canonical heading glossary already carries it).
        (re.compile(r"\bAyrik\b"), "Ayrık"),
        (re.compile(r"\bGiris\b"), "Giriş"),
        (re.compile(r"\bCikis\b"), "Çıkış"),
        (re.compile(r"\bOgrenme\b"), "Öğrenme"),
        (re.compile(r"\bOgretme\b"), "Öğretme"),
        (re.compile(r"\bOrnek\b"), "Örnek"),
        (re.compile(r"\bOrnekler\b"), "Örnekler"),
        (re.compile(r"\bOzet\b"), "Özet"),
        (re.compile(r"\bUretken\b"), "Üretken"),
        (re.compile(r"\bUretici\b"), "Üretici"),
        (re.compile(r"\bSonuclar\b"), "Sonuçlar"),
        (re.compile(r"\bSonuclari\b"), "Sonuçları"),
        (re.compile(r"\bBolum\b"), "Bölüm"),
        (re.compile(r"\bTanim\b"), "Tanım"),
        (re.compile(r"\bIslemleri\b"), "İşlemleri"),
        (re.compile(r"\bIslem\b"), "İşlem"),
        (re.compile(r"\bKosullu\b"), "Koşullu"),
    ]

    # Suffix-based Turkish deasciification (for unknown words in headings)
    _TURKISH_SUFFIX_FIXES: List[Tuple[re.Pattern, str]] = [
        # -lari -> -ları
        (re.compile(r"(\w)lari\b"), r"\1ları"),
        # -leri stays -leri (already correct)
        # -si -> -sı (when preceded by consonant without Turkish)
        (re.compile(r"(\w)masi\b"), r"\1ması"),
        # DATA-05: removed no-op rule '(\w)mesi\b' -> '\1mesi' (replacement
        # was identical to the match -- '-mesi' is already correct Turkish).
    ]

    # ------------------------------------------------------------------
    # Fix: number_hallucination
    # ------------------------------------------------------------------

    # Cross-ref labels for EN->TR number fixing
    _XREF_FIX_RE = re.compile(
        r"(?:Figure|Fig\.?|Table|Tab\.?|Definition|Def\.?|Theorem|Thm\.?"
        r"|Lemma|Proposition|Prop\.?|Corollary|Example|Ex\.?"
        r"|Exercise|Algorithm|Section|Sec\.?|Chapter|Ch\.?"
        r"|Equation|Eq\.?|Remark|Proof|Property"
        r"|Şekil|Tablo|Tanım|Teorem|Önerme|Sonuç|Örnek"
        r"|Alıştırma|Algoritma|Bölüm|Denklem|Not|İspat|Özellik"
        r")\s*"
        r"(\d+(?:\.\d+)*)",
        re.IGNORECASE,
    )

    def _fix_number_hallucination(
        self,
        body: str,
        issues: List[QCIssue],
        math_spans: List[Tuple[int, int]],
    ) -> Tuple[str, List[FixResult]]:
        """Fix cross-reference numbers that MT model hallucinated.

        Strategy: For each missing number reported by QC, search the EN
        source for the correct number and the TR text for the wrong number.
        If the wrong number appears near a cross-ref label, replace it.
        """
        fixes: List[FixResult] = []
        if not self._source_body:
            return body, fixes

        # Extract all ref numbers from EN source
        from collections import Counter
        en_nums = Counter(self._XREF_FIX_RE.findall(self._source_body))
        tr_nums = Counter(self._XREF_FIX_RE.findall(body))

        # Find hallucinated numbers (in TR but not in EN)
        hallucinated = set(tr_nums.keys()) - set(en_nums.keys())
        # Find missing numbers (in EN but fewer in TR)
        missing = {n for n, c in en_nums.items() if tr_nums.get(n, 0) < c}

        if not hallucinated or not missing:
            return body, fixes

        # Try to map hallucinated -> missing numbers
        # Match by prefix similarity (e.g., 2.12 is close to 2.14)
        for wrong_num in sorted(hallucinated):
            # Find the most likely correct number
            best_match = None
            best_score = 0
            wrong_parts = wrong_num.split(".")

            for correct_num in missing:
                correct_parts = correct_num.split(".")
                # Same section prefix? (e.g., both start with "2.")
                if len(wrong_parts) == len(correct_parts):
                    matching = sum(
                        1 for a, b in zip(wrong_parts, correct_parts) if a == b
                    )
                    if matching > best_score:
                        best_score = matching
                        best_match = correct_num

            if best_match and best_score >= len(wrong_parts) - 1:
                # Replace wrong number with correct one (near cross-ref labels)
                label_pattern = re.compile(
                    r"((?:Şekil|Tablo|Tanım|Teorem|Lemma|Önerme|Sonuç|Örnek"
                    r"|Alıştırma|Algoritma|Bölüm|Denklem|Not|İspat|Özellik"
                    r"|Figure|Table|Definition|Theorem|Example|Section"
                    r"|Chapter|Equation)\s*)"
                    + re.escape(wrong_num),
                    re.IGNORECASE,
                )

                new_body = label_pattern.sub(r"\g<1>" + best_match, body)
                if new_body != body:
                    fixes.append(FixResult(
                        check_name="number_hallucination",
                        line_number=0,
                        old_text=f"...{wrong_num}...",
                        new_text=f"...{best_match}...",
                        method="number_correction",
                    ))
                    body = new_body
                    missing.discard(best_match)
                    logger.info(
                        "Fixed hallucinated number: %s -> %s",
                        wrong_num, best_match,
                    )

        return body, fixes

    def _apply_structural_fixes(
        self, body: str,
    ) -> Tuple[str, List[FixResult]]:
        """Apply QC-independent structural fixes.

        These run after the QC convergence loop and fix issues that
        the QC engine doesn't detect (formatting, charset, etc.).
        """
        fixes: List[FixResult] = []

        # 1. Deasciify Turkish chars in section headings
        # A-7 (2026-07-27): manuel modda KAPALI — suffix sezgisi yabancı
        # özel adları bozuyordu (Tomasi -> Toması); Yol A'da başlığı çevirmen
        # yazdı, ASCII bırakıldıysa bilinçlidir.
        heading_fixes: List[FixResult] = []
        if not self.manual_translation:
            body, heading_fixes = self._fix_heading_turkish_chars(body)
            fixes.extend(heading_fixes)

        # 2. Remove stray page header artifacts
        # A-6 (2026-07-27): manuel modda KAPALI — 'sayıyla başlayıp sayıyla
        # biten satır' sezgisi kullanıcının meşru satırını (örn. yıl ... sayı)
        # komple siliyordu; Yol B OCR artefaktı manuel çeviride zaten
        # çevirmenin gözünden geçmiştir.
        header_fixes: List[FixResult] = []
        if not self.manual_translation:
            body, header_fixes = self._fix_page_header_artifacts(body)
            fixes.extend(header_fixes)

        # 3. Clean markdown reference remnants and dead PDF page hrefs
        body, md_fixes = self._fix_markdown_and_href_remnants(body)
        fixes.extend(md_fixes)

        # 4. Fix tabular column count mismatches
        body, tab_fixes = self._fix_tabular_column_count(body)
        fixes.extend(tab_fixes)

        # 5. Apply hardcoded wrong-translation map (FIX 6, session 10).
        # This was designed for HY-MT (Yol B) output. In manual/agent mode
        # (Yol A, --import-tex) the translation is authoritative: this map has
        # no context awareness and silently rewrites legitimate Turkish
        # ('dikgen', 'bilgisayar ağı', ...). The QC-issue loop already skips
        # wrong_translation in manual mode via _MANUAL_TRANSLATION_SKIP; this
        # structural pass bypassed that guard (DATA-01/AF-02/QC-N1). Gate it.
        wt_fixes: List[FixResult] = []
        if not self.manual_translation:
            body, wt_fixes = self._apply_wrong_translation_map_always(body)
            fixes.extend(wt_fixes)

        if fixes:
            logger.info(
                "Structural fixes: %d heading charset + %d header artifacts "
                "+ %d markdown/href + %d tabular + %d wrong_translation.",
                len(heading_fixes), len(header_fixes),
                len(md_fixes), len(tab_fixes), len(wt_fixes),
            )

        return body, fixes

    # ------------------------------------------------------------------
    # Structural: markdown / href remnants
    # ------------------------------------------------------------------

    # Compiled patterns for markdown/href cleanup (class-level for reuse)
    _MD_BRACKET_REF = re.compile(
        r"\[\[(\d+(?:,\d+)*)\\?\]\]\s*\(\\?#page-\d+-\d+\)"
    )
    _ESC_BRACKET_REF = re.compile(
        r"\\\[(\d+(?:,?\d+)*)\\\]\s*\(\\?#?page-\d+-\d+\)"
    )
    _HREF_PAGE_REF = re.compile(
        r"\\href\{#page-\d+-\d+\}\{([^}]*)\}"
    )
    _TEXTIT_URL = re.compile(
        r"\\textit\{([^}]+)\}\((https?://[^)]+)\)"
    )
    _BROKEN_HREF_PAREN = re.compile(
        r"\\href\{https?://[^}]+\}\{\\[\(]([^}]*)\}"
    )

    def _fix_markdown_and_href_remnants(
        self, body: str,
    ) -> Tuple[str, List[FixResult]]:
        """Clean up markdown reference remnants and dead PDF page hrefs.

        Fixes these patterns (produced by PDF-to-LaTeX conversion):
          - [[N\\]](#page-xxx-0) -> [N]
          - \\[N\\] (#page-xxx-0) -> [N]
          - \\href{#page-N-0}{text} -> text  (dead internal page refs)
          - \\textit{text}(url) -> \\href{url}{\\textit{text}}
          - \\href{url}{\\(Author} -> (Author  (broken href with paren)
        """
        fixes: List[FixResult] = []

        # 1. [[N\]](#page-xxx-0) -> [N]
        def _count_and_sub(pattern, repl_or_fn, text, fix_name):
            nonlocal fixes
            matches = list(pattern.finditer(text))
            if not matches:
                return text
            for m in reversed(matches):
                old = m.group(0)
                if callable(repl_or_fn):
                    new = repl_or_fn(m)
                else:
                    new = m.expand(repl_or_fn)
                if old != new:
                    line_num = text[:m.start()].count("\n") + 1
                    text = text[:m.start()] + new + text[m.end():]
                    fixes.append(FixResult(
                        check_name=fix_name,
                        line_number=line_num,
                        old_text=old[:60],
                        new_text=new[:60],
                        method="regex",
                    ))
            return text

        body = _count_and_sub(
            self._MD_BRACKET_REF, r"[\1]", body, "markdown_bracket_ref"
        )
        body = _count_and_sub(
            self._ESC_BRACKET_REF, r"[\1]", body, "markdown_bracket_ref"
        )

        # 2. \href{#page-N-0}{text} -- REMOVED here: autofix works on the
        # Phase 5.5 body that QC also reads (the QC href check excludes
        # #page- artifacts from its count). G3a-4 (2026-10-05, ONARIM2
        # R2.4-3): bu bağlantıların hedefi yoktur (dönüştürücü \hypertarget
        # üretmiyor); derlenecek tam belgede sanitizer
        # (_resolve_internal_links) hedefsiz olanı kaldırır, metni bırakır,
        # hedefi \label olanı \hyperref yapar.

        # 3. \textit{text}(url) -> \href{url}{\textit{text}}
        body = _count_and_sub(
            self._TEXTIT_URL,
            r"\\href{\2}{\\textit{\1}}",
            body,
            "textit_url_to_href",
        )

        # 4. \href{url}{\(Author} -> (Author
        body = _count_and_sub(
            self._BROKEN_HREF_PAREN,
            lambda m: "(" + m.group(1),
            body,
            "broken_href_paren",
        )

        return body, fixes

    # ------------------------------------------------------------------
    # Structural: tabular column count
    # ------------------------------------------------------------------

    _TABULAR_BEGIN = re.compile(
        r"\\begin\{tabular\}\{([^}]*)\}"
    )

    def _fix_tabular_column_count(
        self, body: str,
    ) -> Tuple[str, List[FixResult]]:
        """Log tabular environments where column spec doesn't match data.

        Previously this method attempted to rewrite the column spec, but
        that often *broke* tables (e.g. replacing ``|l|c|r|`` with plain
        ``l l l l``).  Now it only logs mismatches for manual review and
        returns the body unchanged.
        """
        fixes: List[FixResult] = []
        lines = body.split("\n")

        i = 0
        while i < len(lines):
            m = self._TABULAR_BEGIN.search(lines[i])
            if not m:
                i += 1
                continue

            col_spec = m.group(1).strip()
            # Count declared columns: each l, c, r, p{...} counts as one
            declared_cols = len(re.findall(r"[lcr]|p\{[^}]*\}", col_spec))

            # Scan table body for max column count
            max_cols = 0
            j = i + 1
            while j < len(lines):
                if "\\end{tabular}" in lines[j]:
                    break
                # Skip toprule, midrule, bottomrule, hline
                stripped = lines[j].strip()
                if stripped and not re.match(
                    r"\\(?:toprule|midrule|bottomrule|hline|cline)",
                    stripped,
                ):
                    col_count = stripped.count("&") + 1
                    max_cols = max(max_cols, col_count)
                j += 1

            if max_cols > declared_cols and max_cols > 0:
                # Log the mismatch but do NOT modify the table.
                # Automatic column-spec rewriting was destroying tables
                # by stripping vertical rules, alignment, and p{} widths.
                logger.warning(
                    "Tabular column mismatch at line %d: spec has %d cols "
                    "but data has %d cols. Skipping auto-fix to preserve "
                    "table structure (col_spec=%r).",
                    i + 1, declared_cols, max_cols, col_spec,
                )

            i = j + 1 if j < len(lines) else i + 1

        # Body is returned unchanged -- no structural edits to tables.
        return body, fixes

    def _fix_heading_turkish_chars(
        self, body: str,
    ) -> Tuple[str, List[FixResult]]:
        """Deasciify Turkish characters in \\section/\\subsection headings."""
        fixes: List[FixResult] = []
        lines = body.split("\n")

        heading_re = re.compile(
            r"(\\(?:section|subsection|chapter)\*?\{)(.+?)(\})\s*$"
        )

        for i, line in enumerate(lines):
            m = heading_re.match(line.strip())
            if not m:
                continue

            cmd = m.group(1)
            inner = m.group(2)
            close = m.group(3)

            # Check if heading already has proper Turkish chars
            if any(c in inner for c in "ışöüğçİŞÖÜĞÇ"):
                continue

            # Apply word-level fixes
            new_inner = inner
            for pat, repl in self._TURKISH_DEASCIIFY:
                new_inner = pat.sub(repl, new_inner)

            # Apply suffix-level fixes (only on words not already fixed)
            for pat, repl in self._TURKISH_SUFFIX_FIXES:
                new_inner = pat.sub(repl, new_inner)

            if new_inner != inner:
                # Preserve original whitespace
                ws = line[:len(line) - len(line.lstrip())]
                lines[i] = f"{ws}{cmd}{new_inner}{close}"
                fixes.append(FixResult(
                    check_name="heading_charset",
                    line_number=i + 1,
                    old_text=inner[:60],
                    new_text=new_inner[:60],
                    method="deasciify",
                ))

        if fixes:
            body = "\n".join(lines)

        return body, fixes

    _PAGE_HEADER_RE = re.compile(
        r"^\s*\d{1,2}(?:\.\d{1,2})?\s+[A-Za-z][\w\s,:-]{3,40}\s+\d{1,4}\s*$"
    )

    def _fix_page_header_artifacts(
        self, body: str,
    ) -> Tuple[str, List[FixResult]]:
        """Remove stray page header lines like '3.2 ResNets 43'."""
        fixes: List[FixResult] = []
        lines = body.split("\n")
        new_lines = []

        for i, line in enumerate(lines):
            if self._PAGE_HEADER_RE.match(line):
                # Make sure it's not inside an environment
                fixes.append(FixResult(
                    check_name="page_header_artifact",
                    line_number=i + 1,
                    old_text=line.strip()[:60],
                    new_text="",
                    method="delete",
                ))
                continue
            new_lines.append(line)

        if fixes:
            body = "\n".join(new_lines)

        return body, fixes

    # ------------------------------------------------------------------
    # Fix dispatcher
    # ------------------------------------------------------------------

    def _apply_fixes(
        self,
        body: str,
        issues: List[QCIssue],
    ) -> Tuple[str, List[FixResult]]:
        """Apply all fixable issues to body with per-category rollback.

        For each fix category (check_name), applies the fix and then
        runs a lightweight structural check.  If the fix introduces
        new structural damage (brace imbalance worsens or new orphan
        placeholders appear), the fix is rolled back for that category.

        Returns (fixed_body, list_of_fix_results).
        """
        all_fixes: List[FixResult] = []

        # Compute math spans once for the whole body
        math_spans = _find_math_spans(body)

        # Group issues by check_name to batch similar fixes
        by_check: Dict[str, List[QCIssue]] = {}
        for issue in issues:
            by_check.setdefault(issue.check_name, []).append(issue)

        # Handlers that turn clean human/agent translations into half-broken
        # text (e.g. "Patience Wins Again" -> "sabır Wins Again") veya
        # İngilizce sanılan satırı HY-MT'ye verip garbage üretenler
        # (örn. ders notu başlığında "Development" -> "yıkama"). Bu check'ler HY-MT (Yol B) için
        # tasarlandı; manual/ajan çevirisinde (Yol A) ajan otoriterdir, bilinçli
        # bırakılan İngilizce (akronim, özel ad, terim) HY-MT ile YENİDEN
        # çevrilmemeli. İki-kol ayrımı: Yol A'yı koru, Yol B etkilenmesin.
        # untranslated/untranslated_heading/mixed_language_line MT translator
        # çağırır -> manual modda skip (2026-06-14).
        # first_person_academic assumes academic register and rewrites first
        # person singular into passive. In agent/manual translations (esp.
        # narrative / philosophy books) first person is deliberate and correct;
        # the fix strands accusative objects onto passive verbs and produces
        # ungrammatical Turkish (QC-N2). The book_type guard in QCEngine is a
        # no-op because callers don't pass book_type, so skip it in Yol A here.
        _MANUAL_TRANSLATION_SKIP = {
            "glossary_compliance", "wrong_translation",
            "untranslated", "untranslated_heading", "mixed_language_line",
            "first_person_academic",
            # A-2 (2026-07-27): satır-pozisyonel hizalamalı antonym flip —
            # yapı değişince DOĞRU çeviriyi tersine çeviriyordu; manuel modda
            # çevirmen otoriterdir, rapor-only.
            "antonym_guard",
            # A-3 (2026-07-27): CamelCase özel ad bölme (LeCun -> Le Cun) —
            # MT yapıştırması manuel çeviride olmaz, yanlış bölme riski baskın.
            "word_concatenation",
            # F35 (2026-07-29): kalan MT-artefakt düzelticileri. Yol A'da
            # çevirmen otoriterdir, bunlar kullanıcı metnini SİLİYOR:
            #   hallucination -> _fix_segment_hallucination 5+ kelimelik
            #     tekrarı ilk geçişe budar (retorik/şiir tekrarı kaybolur),
            #   foreign_language_leakage -> _fix_foreign_language_leakage
            #     bayraklı satırdan yabancı kelimeyi siler (bilinçli alıntı;
            #     sanitizer A-4'te CJK zaten manuel modda korunuyor),
            #   number_hallucination -> _fix_number_hallucination sayıyı "en
            #     yakın" kaynak sayısıyla değiştirir. QC bugün bu issue'yu
            #     her iki dalda auto_fixable=False üretiyor (dispatch'e
            #     ulaşmıyor), listede olması ileride o bayrak açılırsa
            #     Yol A'yı sessizce açıkta bırakmasın diye.
            "hallucination",
            "foreign_language_leakage",
            "number_hallucination",
        }

        # Apply fixes in a stable order with per-category rollback
        sorted_checks = sorted(by_check.keys())
        for ci, check_name in enumerate(sorted_checks):
            if self.manual_translation and check_name in _MANUAL_TRANSLATION_SKIP:
                logger.info(
                    "AutoFix: skipping '%s' (manual translation mode).",
                    check_name,
                )
                continue
            fix_fn = self._fix_registry.get(check_name)
            if fix_fn is None:
                logger.warning(
                    "No fix function registered for check '%s'. Skipping.",
                    check_name,
                )
                continue

            # Snapshot before this category's fixes
            snapshot = body
            snapshot_brace_delta = self._brace_delta(body)
            # Newline count before the fix -- used to detect line-count changes
            # that would stale the QCIssue.line_num of later categories (AF-09).
            snapshot_nl = snapshot.count("\n")

            check_issues = by_check[check_name]
            body, fixes = fix_fn(body, check_issues, math_spans)

            if fixes:
                # Lightweight structural regression check:
                # 1. Brace balance should not worsen
                # Tolerans 0: kendisi brace dengesi bozan 'fix' tanimi geregi
                # hatalidir. Eski +2 toleransi kategoriler ve iterasyonlar
                # boyunca birikip Phase 6 kaba brace onarimina is birakiyordu
                # (audit 2026-07-16 #85, eski #50).
                new_brace_delta = self._brace_delta(body)
                brace_worsened = abs(new_brace_delta) > abs(snapshot_brace_delta)

                # 2. No new orphan placeholders
                new_orphans = len(re.findall(r"<<LATEX_\d+>>", body))
                old_orphans = len(re.findall(r"<<LATEX_\d+>>", snapshot))
                orphan_worsened = new_orphans > old_orphans

                if brace_worsened or orphan_worsened:
                    reason = []
                    if brace_worsened:
                        reason.append(
                            f"brace delta {snapshot_brace_delta}->{new_brace_delta}"
                        )
                    if orphan_worsened:
                        reason.append(
                            f"orphans {old_orphans}->{new_orphans}"
                        )
                    logger.warning(
                        "Per-category rollback: reverting %d fix(es) for '%s' "
                        "due to structural regression (%s).",
                        len(fixes), check_name, ", ".join(reason),
                    )
                    body = snapshot
                    # Do not add these fixes to the result
                else:
                    all_fixes.extend(fixes)
                    # Recompute math spans after successful fix category
                    math_spans = _find_math_spans(body)
                    # AF-09: if this category added/removed lines, the
                    # QCIssue.line_num values for not-yet-processed categories
                    # (computed against the pre-fix body) now point at the
                    # wrong lines. Remap them so later line-based handlers
                    # (untranslated_heading, mixed_language_line, ...) target
                    # the correct line instead of silently missing or hitting
                    # an unrelated line. Only runs when the line count changed
                    # (the common no-change case pays nothing).
                    if body.count("\n") != snapshot_nl:
                        remaining = [
                            iss
                            for later in sorted_checks[ci + 1:]
                            for iss in by_check[later]
                        ]
                        if remaining:
                            self._remap_issue_line_nums(
                                snapshot, body, remaining,
                            )

        return body, all_fixes

    @staticmethod
    def _remap_issue_line_nums(
        old_body: str,
        new_body: str,
        issues: List[QCIssue],
    ) -> None:
        """Shift each issue's ``line_num`` from *old_body* to *new_body* coords.

        When a fix category inserts or deletes lines, the 1-based
        ``QCIssue.line_num`` values that a single QC pass computed against the
        pre-fix body become stale for every category processed afterwards
        (AF-09). This rebuilds an old->new line map with :mod:`difflib` and
        updates each issue *in place*.

        Lines that survive unchanged are mapped exactly; a line that fell
        inside a replaced/deleted region is collapsed onto the start of that
        region (best effort -- such an issue was likely already handled, and
        the downstream acceptance gate rejects any garbage). Out-of-range
        line numbers are left untouched (handlers bound-check them).
        """
        old_lines = old_body.split("\n")
        new_lines = new_body.split("\n")
        sm = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
        line_map: Dict[int, int] = {}
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                for off in range(i2 - i1):
                    line_map[i1 + off] = j1 + off
            else:
                # replace / delete / insert: collapse old lines onto j1
                for k in range(i1, i2):
                    line_map[k] = j1
        for iss in issues:
            new_idx = line_map.get(iss.line_num - 1)
            if new_idx is not None:
                iss.line_num = new_idx + 1

    # F39 (2026-07-29): "\\" + herhangi bir karakter (satır sonu dahil)
    # ikilisi -- eski saf-Python döngüsünün "kaçışlıysa 2 karakter atla"
    # semantiğinin birebir karşılığı. Soldan sağa, örtüşmesiz eşleşme aynı
    # eşleştirmeyi üretir; metin sonundaki tek "\\" hiç eşleşmez ve zaten
    # brace olmadığı için sayımı etkilemez.
    _ESCAPED_PAIR_RE = re.compile(r"\\.", re.DOTALL)

    @staticmethod
    def _brace_delta(text: str) -> int:
        """Return open_braces - close_braces (ignoring escaped braces)."""
        # F39: kaçışlı ikilileri C hızında düşür, sonra say. Ölçüm: 1.5 MB
        # gövdede saf Python karakter döngüsü 82 ms, bu 1.3 ms; kaçış yoğun
        # metinde (1 MB, her 5 karakterde bir "\") 46 ms -> 8 ms.
        # _apply_fixes kategori başına 2 kez çağırıyor.
        stripped = AutoFixer._ESCAPED_PAIR_RE.sub("", text)
        return stripped.count("{") - stripped.count("}")

    # ------------------------------------------------------------------
    # Fix: prompt_leakage
    # ------------------------------------------------------------------

    def _fix_prompt_leakage(
        self,
        body: str,
        issues: List[QCIssue],
        math_spans: List[Tuple[int, int]],
    ) -> Tuple[str, List[FixResult]]:
        """Remove known prompt leakage patterns.

        A-1/A-4 (2026-07-27): MT-only kalıplar ("Çeviri/Çevirmen notu",
        CJK artefakt regex'i) manuel modda UYGULANMAZ — çevirmenin bilinçli
        notu/alıntısı silinmez. Modül sabiti tam listedir (Yol B sözleşmesi).
        """
        fixes: List[FixResult] = []

        from latex_pipeline.patterns import PROMPT_LEAK_PATTERNS_MT_ONLY
        _patterns = list(_PROMPT_LEAK_PATTERNS)
        if self.manual_translation:
            # Karşılaştırma pattern STRING'i ile (reviewer R2): id() kıyası
            # modül yeniden yüklenirse sessizce kırılıyordu.
            _mt_only = {p.pattern for p in PROMPT_LEAK_PATTERNS_MT_ONLY}
            _patterns = [p for p in _patterns if p.pattern not in _mt_only]
        for pattern in _patterns:
            body, changes = _safe_sub(pattern, "", body, math_spans)
            if changes:
                # F7 (2026-07-28): govde degisti, eski span ofsetleri bayat
                # kaldi -- sonraki desenler math icine yazabiliyordu.
                math_spans = _find_math_spans(body)
            for old, new, pos in changes:
                line_num = body[:pos].count("\n") + 1
                fixes.append(FixResult(
                    check_name="prompt_leakage",
                    line_number=line_num,
                    old_text=old.strip(),
                    new_text=new.strip(),
                    method="regex",
                ))
                logger.debug(
                    "Removed prompt leakage at line %d: %.80s",
                    line_num, old.strip(),
                )

        # Residual cleanup: lines that became trivial after prompt leak
        # removal (only whitespace, punctuation).
        # F4 (2026-07-28): yalniz gercekten leak silindiyse calisir ve
        # verbatim/lstlisting/minted stash'lenir -- eski hali kod
        # blogundaki '...' satirlarini ve kasitli cift bos satirlari
        # kosulsuz eziyordu.
        if fixes:
            def _residual_cleanup(b: str) -> str:
                b = re.sub(
                    r"^\s*[.,;:!?\-\u2013\u2014]*\s*$",
                    "",
                    b,
                    flags=re.MULTILINE,
                )
                # Clean up blank lines left by deletions (3+ -> 2)
                return re.sub(r"\n{3,}", "\n\n", b)

            body = _cleanup_outside_verbatim(body, _residual_cleanup)

        return body, fixes

    # ------------------------------------------------------------------
    # Fix: caption_inconsistency
    # ------------------------------------------------------------------

    def _fix_caption_inconsistency(
        self,
        body: str,
        issues: List[QCIssue],
        math_spans: List[Tuple[int, int]],
    ) -> Tuple[str, List[FixResult]]:
        """Normalize English reference labels to Turkish equivalents."""
        fixes: List[FixResult] = []

        # Apply each normalization pattern
        for pattern, replacement in _CAPTION_NORMALIZATIONS:
            body, changes = _safe_sub(pattern, replacement, body, math_spans)
            if changes:
                # F7 (2026-07-28): uzunluk degisimi sonraki desenlerin
                # ofsetlerini kaydiriyordu ('Fig. N' -> 'Sekil~N' +1 char),
                # math ici eslesmeler span disina dusup yeniden yaziliyordu.
                math_spans = _find_math_spans(body)
            for old, new, pos in changes:
                line_num = body[:pos].count("\n") + 1
                fixes.append(FixResult(
                    check_name="caption_inconsistency",
                    line_number=line_num,
                    old_text=old,
                    new_text=new,
                    method="replace",
                ))

        # Second pass: fix Turkish special characters in our replacements
        for pattern, replacement in _TURKISH_CHAR_FIXES:
            body, changes = _safe_sub(pattern, replacement, body, math_spans)
            if changes:
                math_spans = _find_math_spans(body)  # F7
            for old, new, pos in changes:
                line_num = body[:pos].count("\n") + 1
                fixes.append(FixResult(
                    check_name="caption_inconsistency",
                    line_number=line_num,
                    old_text=old,
                    new_text=new,
                    method="replace",
                ))

        if fixes:
            logger.info(
                "Caption normalization: %d replacements.", len(fixes),
            )

        return body, fixes

    # ------------------------------------------------------------------
    # Fix: placeholder_orphan
    # ------------------------------------------------------------------

    def _fix_placeholder_orphan(
        self,
        body: str,
        issues: List[QCIssue],
        math_spans: List[Tuple[int, int]],
    ) -> Tuple[str, List[FixResult]]:
        """Remove orphaned placeholder artifacts."""
        fixes: List[FixResult] = []

        # Handle @...@@ specially: keep the inner text
        body, changes = _safe_sub(
            _ATAT_PATTERN,
            lambda m: m.group(1),  # keep text, remove markers
            body,
            math_spans,
        )
        for old, new, pos in changes:
            line_num = body[:pos].count("\n") + 1
            fixes.append(FixResult(
                check_name="placeholder_orphan",
                line_number=line_num,
                old_text=old,
                new_text=new,
                method="regex",
            ))

        # Remove all other placeholder patterns (delete entirely)
        for pattern in _PLACEHOLDER_ORPHAN_PATTERNS:
            # Skip the @...@@ pattern since we already handled it
            if pattern is _ATAT_PATTERN:
                continue
            # Skip patterns that would match inside our already-fixed text
            body, changes = _safe_sub(pattern, "", body, math_spans)
            for old, new, pos in changes:
                line_num = body[:pos].count("\n") + 1
                fixes.append(FixResult(
                    check_name="placeholder_orphan",
                    line_number=line_num,
                    old_text=old,
                    new_text="",
                    method="regex",
                ))

        # Clean up extra whitespace from placeholder removal.
        # 2026-07-11 (#15): temizlik verbatim/lstlisting/minted DISINDA
        # uygulanir — eski hali kod bloklarindaki girintiyi ('    x = 1')
        # tek bosluga indirip kodu sessizce bozuyordu, kabul kapisi bunu
        # olcmuyordu. F4/F5 (2026-07-28): stash deseni ortak yardimciya
        # tasindi (_cleanup_outside_verbatim), davranis ayni.
        def _ws_cleanup(b: str) -> str:
            b = re.sub(r"  +", " ", b)
            return re.sub(r"\n{3,}", "\n\n", b)

        body = _cleanup_outside_verbatim(body, _ws_cleanup)

        if fixes:
            logger.info(
                "Placeholder cleanup: removed %d orphaned artifacts.",
                len(fixes),
            )

        return body, fixes

    # ------------------------------------------------------------------
    # Fix: untranslated
    # ------------------------------------------------------------------

    def _fix_untranslated(
        self,
        body: str,
        issues: List[QCIssue],
        math_spans: List[Tuple[int, int]],
    ) -> Tuple[str, List[FixResult]]:
        """Attempt to re-translate untranslated blocks.

        If no translator is available, issues are logged but not fixed.
        When a translator is available, extracts the untranslated block
        with 1-2 surrounding sentences as context, translates, and
        surgically replaces.
        """
        fixes: List[FixResult] = []

        if self.translator is None:
            logger.info(
                "No translator available. %d untranslated issues "
                "cannot be auto-fixed (need translator for re-translate).",
                len(issues),
            )
            return body, fixes

        logger.info(
            "Translator available. Attempting re-translation of "
            "%d untranslated blocks.", len(issues),
        )

        lines = body.split("\n")

        # Group consecutive untranslated lines into blocks for better
        # translation quality (context preservation) and efficiency.
        blocks = self._group_untranslated_blocks(issues, lines, math_spans)

        logger.info(
            "Grouped %d issues into %d translation blocks.",
            len(issues), len(blocks),
        )

        # Process blocks in reverse order to preserve line numbers
        for block_start, block_end, block_lines in reversed(blocks):
            # Join the block lines into a single text
            block_text = "\n".join(
                lines[i].strip() for i in range(block_start, block_end + 1)
            )

            if not block_text.strip():
                continue

            # Skip blocks that contain tabular environments -- re-translating
            # table content destroys column alignment and cell structure.
            if re.search(
                r"\\begin\{(tabular|tabularx|longtable)\}", block_text,
            ):
                logger.debug(
                    "Skipping untranslated block L%d-%d: contains tabular env.",
                    block_start + 1, block_end + 1,
                )
                continue

            # Extract context around the block
            ctx_start = max(0, block_start - 2)
            ctx_end = min(len(lines), block_end + 3)
            context_before = "\n".join(
                lines[i] for i in range(ctx_start, block_start)
            )
            context_after = "\n".join(
                lines[i] for i in range(block_end + 1, ctx_end)
            )

            # Translate the entire block
            try:
                translated_block = self._retranslate_with_context(
                    block_text, context_before, context_after,
                )
            except Exception as exc:
                logger.error(
                    "Re-translation failed for lines %d-%d: %s",
                    block_start + 1, block_end + 1, exc,
                )
                continue

            # Acceptance gate (AF-C7): reject empty / echo / still-English
            # results so we never commit garbage in place of the block.
            if not self._accept_retranslation(
                    block_text, translated_block,
                    source_lang=self._source_lang()):
                logger.info(
                    "Re-translate rejected for block L%d-%d "
                    "(empty/echo/still-English); keeping original.",
                    block_start + 1, block_end + 1,
                )
                continue

            # Replace block lines with translated text
            translated_lines = translated_block.split("\n")
            old_text = block_text[:80]

            # Preserve leading whitespace from the first line
            first_ws = lines[block_start][
                : len(lines[block_start]) - len(lines[block_start].lstrip())
            ]

            # Replace the block in the lines array
            new_lines_with_ws = []
            for j, tl in enumerate(translated_lines):
                ws = first_ws if j == 0 else "  "
                new_lines_with_ws.append(ws + tl.strip())

            lines[block_start:block_end + 1] = new_lines_with_ws

            fixes.append(FixResult(
                check_name="untranslated",
                line_number=block_start + 1,
                old_text=old_text,
                new_text=translated_block[:80],
                method="re-translate",
            ))

            logger.debug(
                "Re-translated block L%d-%d (%d lines -> %d lines)",
                block_start + 1, block_end + 1,
                block_end - block_start + 1, len(translated_lines),
            )

        if fixes:
            body = "\n".join(lines)
            logger.info(
                "Re-translation: %d blocks re-translated.", len(fixes),
            )

        return body, fixes

    def _group_untranslated_blocks(
        self,
        issues: List[QCIssue],
        lines: List[str],
        math_spans: List[Tuple[int, int]],
    ) -> List[Tuple[int, int, List[int]]]:
        """Group untranslated issues into contiguous blocks.

        Returns list of (start_idx, end_idx, line_indices) tuples
        where indices are 0-based.
        """
        # Collect all valid line indices (0-based)
        valid_indices: List[int] = []

        # Build set of line indices inside tabular environments so we
        # never attempt to re-translate table rows.
        tabular_lines = _build_tabular_line_set(lines)

        for issue in sorted(issues, key=lambda i: i.line_num):
            line_num = issue.line_num
            if line_num < 1 or line_num > len(lines):
                continue

            idx = line_num - 1
            target = lines[idx].strip()

            # Skip if inside a tabular / tabularx / longtable environment
            if idx in tabular_lines:
                logger.debug(
                    "Skipping untranslated line %d: inside tabular env.",
                    line_num,
                )
                continue

            # Skip if inside math
            char_pos = sum(len(lines[i]) + 1 for i in range(idx))
            if _is_inside_math(char_pos, math_spans):
                continue

            # Skip pure LaTeX commands (not prose)
            if target.startswith("\\") and not any(
                target.startswith(cmd)
                for cmd in ("\\item", "\\textbf{", "\\textit{", "\\emph{")
            ):
                continue

            valid_indices.append(idx)

        if not valid_indices:
            return []

        # Group consecutive indices (allow gap of 1 empty line)
        blocks: List[Tuple[int, int, List[int]]] = []
        current_block = [valid_indices[0]]

        for i in range(1, len(valid_indices)):
            prev = valid_indices[i - 1]
            curr = valid_indices[i]

            # Allow gap of 1 (empty line between consecutive untranslated lines)
            if curr - prev <= 2:
                current_block.append(curr)
            else:
                blocks.append((
                    current_block[0],
                    current_block[-1],
                    current_block,
                ))
                current_block = [curr]

        # Don't forget the last block
        blocks.append((
            current_block[0],
            current_block[-1],
            current_block,
        ))

        return blocks

    @staticmethod
    def _accept_retranslation(
        original: str,
        candidate: Optional[str],
        is_heading: bool = False,
        source_lang: str = "en",
    ) -> bool:
        """Central acceptance gate for any re-translation result (AF-C7).

        A re-translation is only written back if it genuinely improves the
        text. Reject the candidate (keep the original) when it is:
          * empty / whitespace / None,
          * an echo of the original (no change), or
          * still in the source language according to the canonical
            detector (``retranslator.is_untranslated_line``; for the default
            ``source_lang="en"`` it IS ``is_english_line``, unchanged).

        2026-09-28 (Vson, K5 riski 1): yerel model yolu 6 kaynak dile açıldı;
        İngilizce dışı kaynakta (de/ru/fr/es/it) kısmen kaynak dilde kalan
        aday eskiden yalnız İngilizce dedektörüne soruluyor ve KABUL
        ediliyordu. Kaynak dil çevirmenden gelir (``_source_lang``).

        This prevents the re-translate handlers from committing garbage or
        untouched source text into the body. Lazy import -> no circular
        dependency.
        """
        if candidate is None:
            return False
        cand = candidate.strip()
        if not cand:
            return False
        if cand == (original or "").strip():
            return False
        # Canonical language detector (lazy import -> no circular dependency).
        from latex_pipeline.retranslator import is_untranslated_line
        try:
            if is_untranslated_line(cand, source_lang or "en",
                                    is_heading=is_heading):
                return False
        except Exception:
            # Detector failure must not block a plausible translation, but we
            # already ruled out empty/echo above, so accept conservatively.
            pass
        return True

    def _source_lang(self) -> str:
        """Çevirmenin kaynak dili (yerel model yolu; yoksa "en" — Yol A'da
        çevirmen yok, kabul kapısı zaten çağrılmaz)."""
        lang = getattr(self.translator, "source_lang", None)
        return lang if isinstance(lang, str) and lang else "en"

    def _retranslate_with_context(
        self,
        text: str,
        context_before: str,
        context_after: str,
    ) -> str:
        """Translate a single text block with surrounding context.

        Provides 1-2 surrounding sentences to the translator for
        coherence, but only returns the translation of the target text.

        Parameters
        ----------
        text : str
            The untranslated text to translate.
        context_before : str
            1-2 lines before the target text.
        context_after : str
            1-2 lines after the target text.

        Returns
        -------
        str
            The translated text.
        """
        # Use a marker to delimit the target text within context
        marker_start = "<<<TRANSLATE_START>>>"
        marker_end = "<<<TRANSLATE_END>>>"

        # Build the context-enriched input
        parts = []
        if context_before.strip():
            parts.append(context_before.strip())
        parts.append(f"{marker_start}{text}{marker_end}")
        if context_after.strip():
            parts.append(context_after.strip())

        full_input = "\n".join(parts)

        # Translate the whole block
        # We use _safe_translate if available, otherwise translate directly
        if hasattr(self.translator, "_safe_translate"):
            translated_full = self.translator._safe_translate(full_input)
        elif hasattr(self.translator, "translate"):
            translated_full = self.translator.translate(full_input)
        else:
            logger.error("Translator has no translate method.")
            return text

        # Extract just the translated target text between markers
        # The markers might get translated or mangled, so try multiple
        # extraction strategies
        extracted = self._extract_between_markers(
            translated_full, marker_start, marker_end,
        )

        if extracted:
            return self._strip_marker_remnants(extracted).strip()

        # Fallback: if markers are lost, return the middle portion
        # of the translated text (heuristic)
        translated_lines = translated_full.strip().split("\n")
        context_before_lines = len(context_before.strip().split("\n")) if context_before.strip() else 0
        context_after_lines = len(context_after.strip().split("\n")) if context_after.strip() else 0

        start_idx = context_before_lines
        end_idx = len(translated_lines) - context_after_lines

        if start_idx < end_idx:
            # G2a-2 (2026-10-05): satır sayısı sezgiseli işaret kalıntılı
            # satırı da döndürüyordu (bir test kitabının çevirisinde iki yer:
            # "<<><TRANSLATE_END>>", "<<><TRANSLATE_START>>").
            return self._strip_marker_remnants(
                "\n".join(translated_lines[start_idx:end_idx])).strip()

        # Last resort (AF-11): markers were lost AND the line-count heuristic
        # failed. Returning the FULL context+target+context translation would
        # duplicate the neighbouring context lines into the target block
        # (silent content duplication). Keep the original text untouched
        # instead -- the acceptance gate then rejects it as an echo.
        logger.warning(
            "Could not extract target from context-enriched translation; "
            "keeping original block to avoid context duplication."
        )
        return text

    @staticmethod
    def _strip_marker_remnants(text: str) -> str:
        """İstem işareti kalıntısını (bozuk biçimler dahil) temizle ve
        günlüğe yaz (G2a-2; ölçüt translator.strip_prompt_markers)."""
        from latex_pipeline.translator import strip_prompt_markers

        cleaned, found = strip_prompt_markers(text)
        if found:
            logger.warning("AutoFix: istem işareti kalıntısı temizlendi "
                           "(%d: %s): %.80s", len(found),
                           ", ".join(found[:4]), cleaned)
        return cleaned

    @staticmethod
    def _extract_between_markers(
        text: str,
        start_marker: str,
        end_marker: str,
    ) -> Optional[str]:
        """Extract text between start and end markers.

        Tries exact match first, then fuzzy match for mangled markers.
        """
        # Exact match
        s_idx = text.find(start_marker)
        e_idx = text.find(end_marker)
        if s_idx != -1 and e_idx != -1 and s_idx < e_idx:
            return text[s_idx + len(start_marker):e_idx]

        # Fuzzy: markers might be partially translated
        # Try regex with flexible whitespace/case
        pattern = re.compile(
            r"<{2,3}\s*TRANSLATE[_\s]*START\s*>{2,3}"
            r"(.*?)"
            r"<{2,3}\s*TRANSLATE[_\s]*END\s*>{2,3}",
            re.DOTALL | re.IGNORECASE,
        )
        m = pattern.search(text)
        if m:
            return m.group(1)

        return None

    # ------------------------------------------------------------------
    # Fix: missing_images
    # ------------------------------------------------------------------

    # Regex for \includegraphics with optional options
    _INCLUDEGRAPHICS_RE = re.compile(
        r"\\includegraphics\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}"
    )

    # Regex for entire figure environment
    _FIGURE_ENV_RE = re.compile(
        r"(\\begin\{figure\}.*?\\end\{figure\})",
        re.DOTALL,
    )

    def _fix_missing_images(
        self,
        body: str,
        issues: List[QCIssue],
        math_spans: List[Tuple[int, int]],
    ) -> Tuple[str, List[FixResult]]:
        """Restore missing \\includegraphics from EN source.

        Strategy:
        1. Parse missing image paths from issue context
        2. Find the figure environment in EN source containing each image
        3. Find the corresponding figure position in TR by matching
           nearby labels/captions or sequential order
        4. Insert the missing \\includegraphics (or entire figure env)
        """
        fixes: List[FixResult] = []

        if not self._source_body:
            logger.info(
                "No source body available. Cannot fix missing images."
            )
            return body, fixes

        # Collect all missing image paths from issues
        missing_paths: set = set()
        for issue in issues:
            if issue.context:
                for p in issue.context.split(","):
                    p = p.strip()
                    if p:
                        missing_paths.add(p)

        if not missing_paths:
            # Fallback: re-detect from source comparison
            en_images = set(self._INCLUDEGRAPHICS_RE.findall(self._source_body))
            tr_images = set(self._INCLUDEGRAPHICS_RE.findall(body))
            missing_paths = en_images - tr_images

        if not missing_paths:
            return body, fixes

        logger.info(
            "Missing images to restore: %d — %s",
            len(missing_paths),
            ", ".join(sorted(missing_paths)[:5]),
        )

        # Build a map of image_path -> figure environment in EN source
        en_figures = self._FIGURE_ENV_RE.findall(self._source_body)
        # Also collect standalone \includegraphics not in figure envs
        en_lines = self._source_body.split("\n")

        for img_path in sorted(missing_paths):
            restored = False

            # Strategy A: Find figure environment containing this image
            for fig_block in en_figures:
                if img_path in fig_block:
                    # Check if this figure is already in TR body
                    # (maybe partially — label might exist)
                    label_match = re.search(
                        r"\\label\{([^}]+)\}", fig_block,
                    )

                    if label_match:
                        label = label_match.group(1)
                        # Find the figure env in TR that has this label
                        tr_fig_pattern = re.compile(
                            r"(\\begin\{figure\}.*?\\end\{figure\})",
                            re.DOTALL,
                        )
                        for tr_fig_m in tr_fig_pattern.finditer(body):
                            tr_fig = tr_fig_m.group(0)
                            if label in tr_fig and img_path not in tr_fig:
                                # Figure exists but image is missing
                                # Insert \includegraphics before \caption
                                # or after \begin{figure}
                                img_cmd = self._extract_includegraphics_cmd(
                                    fig_block, img_path,
                                )
                                if img_cmd:
                                    insert_point = self._find_image_insert_point(
                                        tr_fig,
                                    )
                                    new_fig = (
                                        tr_fig[:insert_point]
                                        + img_cmd + "\n"
                                        + tr_fig[insert_point:]
                                    )
                                    body = body.replace(tr_fig, new_fig, 1)
                                    fixes.append(FixResult(
                                        check_name="missing_images",
                                        line_number=body[:body.find(new_fig)].count("\n") + 1,
                                        old_text=f"[missing {img_path}]",
                                        new_text=img_cmd.strip(),
                                        method="restore",
                                    ))
                                    restored = True
                                    break

                    if not restored:
                        # Figure env doesn't exist in TR at all — find
                        # nearest anchor and insert the whole block
                        insert_pos = self._find_figure_insert_position(
                            body, fig_block, self._source_body,
                        )
                        body = (
                            body[:insert_pos]
                            + "\n" + fig_block + "\n"
                            + body[insert_pos:]
                        )
                        fixes.append(FixResult(
                            check_name="missing_images",
                            line_number=body[:insert_pos].count("\n") + 1,
                            old_text=f"[missing figure with {img_path}]",
                            new_text=fig_block[:80],
                            method="restore",
                        ))
                        restored = True
                    break  # Found the figure, move to next missing image

            if not restored:
                # Strategy B: Standalone \includegraphics (not in figure)
                for i, line in enumerate(en_lines):
                    if img_path in line and "\\includegraphics" in line:
                        img_cmd = line.strip()
                        # Find approximate position in TR
                        # Use surrounding lines as anchors
                        anchor = self._find_line_anchor(
                            en_lines, i, body,
                        )
                        if anchor >= 0:
                            tr_lines = body.split("\n")
                            tr_lines.insert(anchor + 1, img_cmd)
                            body = "\n".join(tr_lines)
                            fixes.append(FixResult(
                                check_name="missing_images",
                                line_number=anchor + 1,
                                old_text=f"[missing {img_path}]",
                                new_text=img_cmd[:80],
                                method="restore",
                            ))
                        break

        if fixes:
            logger.info(
                "Image recovery: restored %d missing images.", len(fixes),
            )

        return body, fixes

    @staticmethod
    def _extract_includegraphics_cmd(
        figure_block: str, img_path: str,
    ) -> Optional[str]:
        """Extract the full \\includegraphics command for a given path."""
        pattern = re.compile(
            r"(\\(?:centering\s+)?\\includegraphics\s*"
            r"(?:\[[^\]]*\])?\s*\{" + re.escape(img_path) + r"\})"
        )
        m = pattern.search(figure_block)
        if m:
            return m.group(1)
        # Simpler match
        pattern2 = re.compile(
            r"(\\includegraphics\s*(?:\[[^\]]*\])?\s*\{"
            + re.escape(img_path) + r"\})"
        )
        m2 = pattern2.search(figure_block)
        return m2.group(1) if m2 else None

    @staticmethod
    def _find_image_insert_point(figure_block: str) -> int:
        """Find the best insertion point for \\includegraphics in a figure.

        Prefers: after \\centering, before \\caption, after \\begin{figure}.
        """
        # After \centering
        m = re.search(r"\\centering\s*\n?", figure_block)
        if m:
            return m.end()
        # Before \caption
        m = re.search(r"\\caption", figure_block)
        if m:
            return m.start()
        # After \begin{figure}[...]
        m = re.search(r"\\begin\{figure\}\s*(?:\[[^\]]*\])?\s*\n?", figure_block)
        if m:
            return m.end()
        return 0

    def _find_figure_insert_position(
        self, tr_body: str, en_fig_block: str, en_body: str,
    ) -> int:
        """Find where to insert a missing figure env in TR body.

        Uses the EN source to find what comes before/after the figure,
        then locates those anchors in TR body.
        """
        en_fig_start = en_body.find(en_fig_block)
        if en_fig_start < 0:
            return len(tr_body)

        # Look for section/chapter commands before this figure in EN
        en_before = en_body[:en_fig_start]
        section_matches = list(re.finditer(
            r"\\(?:section|subsection|chapter)\{[^}]+\}",
            en_before,
        ))

        if section_matches:
            last_section = section_matches[-1].group(0)
            # Find same section in TR
            tr_pos = tr_body.find(last_section)
            if tr_pos >= 0:
                # Insert after the next paragraph break
                next_para = tr_body.find("\n\n", tr_pos)
                if next_para >= 0:
                    return next_para + 2
                return tr_pos + len(last_section)

        # Fallback: insert at end
        return len(tr_body)

    @staticmethod
    def _find_line_anchor(
        en_lines: List[str], target_idx: int, tr_body: str,
    ) -> int:
        """Find a line in TR body near where target_idx's content should be.

        Searches surrounding EN lines for one that exists in TR body.
        """
        tr_lines = tr_body.split("\n")
        # Search ±5 lines around target for an anchor
        for offset in range(1, 6):
            for direction in (-1, 1):
                check_idx = target_idx + (offset * direction)
                if 0 <= check_idx < len(en_lines):
                    en_line = en_lines[check_idx].strip()
                    if len(en_line) > 10:
                        # Look for this line in TR body
                        for j, tr_line in enumerate(tr_lines):
                            if en_line in tr_line or tr_line.strip() == en_line:
                                # Return position relative to direction
                                if direction == -1:
                                    return j  # anchor is before, insert after
                                else:
                                    return max(0, j - 1)  # anchor is after
        return len(tr_lines) - 1

    # ------------------------------------------------------------------
    # Fix: inline_math_loss
    # ------------------------------------------------------------------

    # Inline math pattern (same as qc_engine)
    _INLINE_MATH_PAT = re.compile(
        r"(?<![\$\\])\$(?!\$)(.+?)(?<![\$\\])\$(?!\$)"
    )

    def _fix_inline_math_loss(
        self,
        body: str,
        issues: List[QCIssue],
        math_spans: List[Tuple[int, int]],
    ) -> Tuple[str, List[FixResult]]:
        """Restore lost inline math by comparing EN/TR line by line.

        Two-phase strategy:

        Phase 1 -- **Merged math split**: MT models sometimes merge
        adjacent inline math expressions into one, e.g.
        ``$\\times$ $\\cdot$`` becomes ``$\\times \\cdot$``.
        When EN has ``$A$`` and ``$B$`` as separate expressions but TR
        has ``$A B$`` (space-joined), split the merged expression back
        into ``$A$ $B$``.  This increases TR math count without
        inserting new content.

        Phase 2 -- **Missing math restore** (original logic): For any
        remaining count deficit, find genuinely missing math expressions
        and insert them at appropriate positions.

        Safety:
        1. EN line must have MORE inline math than TR counterpart
        2. Merged math is only split when the atoms match EN exactly
        3. Global math count stays within EN +/- 5 tolerance
        """
        fixes: List[FixResult] = []

        if not self._source_body:
            logger.info(
                "No source body available. Cannot fix inline math loss."
            )
            return body, fixes

        # Parse target from issue context
        target_diff = 0
        for issue in issues:
            if issue.context:
                m = re.match(r"en=(\d+),tr=(\d+)", issue.context)
                if m:
                    target_diff = int(m.group(1)) - int(m.group(2))

        # Only fix if TR has FEWER math than EN
        if target_diff <= 0:
            logger.info(
                "TR already has enough inline math (diff=%d). Skipping.",
                target_diff,
            )
            return body, fixes

        en_lines = self._source_body.split("\n")
        tr_lines = body.split("\n")

        # Build line correspondence
        alignment = self._align_lines(en_lines, tr_lines)

        # Deduplicate alignment to avoid processing same TR line twice
        seen_tr: set = set()
        unique_alignment = []
        for en_idx, tr_idx in alignment:
            if tr_idx not in seen_tr:
                seen_tr.add(tr_idx)
                unique_alignment.append((en_idx, tr_idx))

        # ==============================================================
        # PHASE 1: Split merged inline math
        # ==============================================================
        # When the MT model merges separate inline math atoms into one
        # expression (e.g. "$\times$ $\cdot$" -> "$\times \cdot$"),
        # split them back.  We verify each merge against the EN source:
        # if EN has the atoms as SEPARATE $...$ expressions, the merge
        # in TR is an MT error and must be split regardless of global
        # budget.  This may cause TR count to slightly exceed EN count,
        # but it prevents the broken_inline_math checker from further
        # damaging these expressions with incorrect merges.
        split_count = 0

        for en_idx, tr_idx in unique_alignment:
            en_line = en_lines[en_idx]
            tr_line = tr_lines[tr_idx]

            # Skip LaTeX structure lines
            if re.match(
                r"^\s*\\(?:begin|end|label|section|chapter|"
                r"includegraphics|caption)\b",
                en_line,
            ):
                continue

            en_math = self._INLINE_MATH_PAT.findall(en_line)
            if not en_math:
                continue

            tr_math = self._INLINE_MATH_PAT.findall(tr_line)

            # Only attempt split if EN has strictly more math
            if len(en_math) <= len(tr_math):
                continue

            # Build set of EN atomic math expressions for this line
            en_math_set = set(en_math)

            # Find merged TR math: expressions that contain multiple
            # EN atoms joined by whitespace
            new_tr_line = tr_line
            line_splits = 0

            for tr_expr in tr_math:
                # Skip if this expression already matches an EN atom
                if tr_expr in en_math_set:
                    continue

                # Try to decompose: split by whitespace and check if
                # all parts are EN atoms
                parts = tr_expr.split()
                if len(parts) < 2:
                    continue

                # Check if ALL parts exist as individual EN atoms
                all_atoms = all(p in en_math_set for p in parts)
                if not all_atoms:
                    continue

                # This TR expression is a merge of EN atoms.
                # Split it: "$A B$" -> "$A$ $B$"
                merged_full = f"${tr_expr}$"
                split_full = " ".join(f"${p}$" for p in parts)

                if merged_full in new_tr_line:
                    new_tr_line = new_tr_line.replace(
                        merged_full, split_full, 1,
                    )
                    gain = len(parts) - 1
                    line_splits += gain
                    split_count += gain

            if line_splits > 0 and new_tr_line != tr_line:
                new_tr_line = re.sub(r"  +", " ", new_tr_line)
                tr_lines[tr_idx] = new_tr_line
                fixes.append(FixResult(
                    check_name="inline_math_loss",
                    line_number=tr_idx + 1,
                    old_text=tr_line.strip()[:60],
                    new_text=new_tr_line.strip()[:60],
                    method="merged_math_split",
                ))

        if split_count > 0:
            logger.info(
                "Inline math split: separated %d merged expressions.",
                split_count,
            )

        # Recompute budget after splits
        # Recount TR inline math
        body_after_split = "\n".join(tr_lines)
        new_tr_count = len(self._INLINE_MATH_PAT.findall(body_after_split))
        en_count = 0
        for issue in issues:
            if issue.context:
                m = re.match(r"en=(\d+),tr=(\d+)", issue.context)
                if m:
                    en_count = int(m.group(1))
        budget = en_count - new_tr_count if en_count > 0 else target_diff - split_count

        # ==============================================================
        # PHASE 2: Restore genuinely missing math (original logic)
        # ==============================================================
        if budget <= 0:
            # Splits were enough, no more restoration needed
            if fixes:
                body = "\n".join(tr_lines)
            return body, fixes

        # Build all existing inline math in TR body for dedup checking
        all_tr_math = set()
        for line in tr_lines:
            for expr in self._INLINE_MATH_PAT.findall(line):
                all_tr_math.add(expr)

        restore_count = 0
        rejected_count = 0
        # Ekleme satır sayısını değiştirmez: engelli satırlar bir kez hesaplanır.
        blocked_tr_lines = _restore_blocked_lines(tr_lines)

        for en_idx, tr_idx in unique_alignment:
            if budget <= 0:
                break

            en_line = en_lines[en_idx]
            tr_line = tr_lines[tr_idx]

            # Skip LaTeX structure lines
            if re.match(
                r"^\s*\\(?:begin|end|label|section|chapter|"
                r"includegraphics|caption)\b",
                en_line,
            ):
                continue
            # R6c: hedef TR satırı da düzyazı olmalı. Boş satıra eklenen
            # ifade paragraf ayırıcısını yiyip başıboş bir satır olur
            # (sentetik fr: paragraf dışında tek başına '$X^{e}$', QC 92->100),
            # yapı satırına eklenen ifade '\end{itemize} $\cdot$' olur;
            # tablo/matematik ortamı satırları için bkz. _restore_blocked_lines.
            line_blocked = bool(
                not tr_line.strip() or tr_idx in blocked_tr_lines
                or self._RESTORE_SKIP_TR_LINE_RE.match(tr_line))

            en_math = self._INLINE_MATH_PAT.findall(en_line)
            if not en_math:
                continue

            tr_math = self._INLINE_MATH_PAT.findall(tr_line)

            # CONSERVATIVE: Only act if EN has strictly more math
            if len(en_math) <= len(tr_math):
                continue

            # Find which EN math expressions are genuinely missing
            tr_math_multiset = list(tr_math)
            missing_math = []
            for expr in en_math:
                if expr in tr_math_multiset:
                    tr_math_multiset.remove(expr)  # matched, remove
                else:
                    # Verify this expression doesn't already exist
                    # in the TR line (maybe in different form)
                    if f"${expr}$" not in tr_line:
                        missing_math.append(expr)

            if not missing_math:
                continue

            # Limit insertions per line
            max_per_line = len(en_math) - len(tr_math)
            missing_math = missing_math[:max_per_line]

            new_tr_line = tr_line
            line_restores = 0

            for expr in missing_math:
                if budget <= 0:
                    break

                full_expr = f"${expr}$"

                # Double-check: not already in current TR line
                if full_expr in new_tr_line:
                    continue

                insertion = self._find_math_insertion_point(
                    en_line, tr_line, new_tr_line, expr,
                )
                # R6c: engelli satır, konumla geri eklenmeye uygun olmayan
                # parça ya da hizalama kaymasıyla komşu satırda zaten duran
                # ifade EKLENMEZ. Reddedilen aday yine de açıktan düşülür:
                # bütçe başka satıra kaydırılırsa asıl eksik olmayan ifadeler
                # eklenir (farksal 2026-09-25, Rusça bir kitapta yerel model yolu: çevirmenin
                # '\text{п г}' yazdığı ifadenin '\rm' kopyası).
                if insertion is not None and (
                        line_blocked
                        or not self._is_restorable_math(expr)
                        or self._present_nearby(full_expr, en_lines, en_idx,
                                                tr_lines, tr_idx, new_tr_line)):
                    budget -= 1
                    rejected_count += 1
                    continue
                if insertion is not None:
                    pos, _ = insertion
                    new_tr_line = (
                        new_tr_line[:pos]
                        + " " + full_expr + " "
                        + new_tr_line[pos:]
                    )
                    restore_count += 1
                    line_restores += 1
                    budget -= 1

            if line_restores > 0 and new_tr_line != tr_line:
                new_tr_line = re.sub(r"  +", " ", new_tr_line)
                tr_lines[tr_idx] = new_tr_line
                fixes.append(FixResult(
                    check_name="inline_math_loss",
                    line_number=tr_idx + 1,
                    old_text=tr_line.strip()[:60],
                    new_text=new_tr_line.strip()[:60],
                    method="restore",
                ))

        if restore_count > 0:
            logger.info(
                "Inline math recovery: restored %d expressions "
                "(budget was %d).",
                restore_count, budget + restore_count + rejected_count,
            )
        if rejected_count > 0:
            logger.info(
                "Inline math recovery: %d candidate(s) not restored "
                "(blank/structural/table/math line, baseless script or '&' "
                "fragment, or already present nearby).",
                rejected_count,
            )

        if fixes:
            body = "\n".join(tr_lines)

        return body, fixes

    # R6c: geri ekleme yapılmayacak TR satırları (yapı komutu, yorum).
    _RESTORE_SKIP_TR_LINE_RE = re.compile(
        r"^\s*(?:%|\\(?:begin|end|label|(?:sub)*section|chapter|part|"
        r"paragraph|includegraphics|caption|item(?![A-Za-z])|"
        r"toprule|midrule|bottomrule|hline)\b)"
    )
    # Tabansız simge parçası: '^{1}', '{}^{&}', '_{i}' (dipnot/birim artefaktı).
    _BASELESS_SCRIPT_RE = re.compile(r"\s*(?:\{\s*\}\s*)?[\^_]")
    # R6c: ±3 satırlık pencere (hizalama orantılı; kayma birkaç satırdır).
    _RESTORE_WINDOW = 3

    @classmethod
    def _is_restorable_math(cls, expr: str) -> bool:
        """Kaynaktaki ``$expr$`` çeviriye konumla geri eklenebilir mi?

        Hayır: (1) tabansız simge parçası ('${}^{1}$', '${}^{&}$',
        '$^{0}$') — Marker'ın dipnot/birim kırığıdır; çevirmen bunları
        ``\\textsuperscript``/dipnot olarak düzeltmiş olur, geri eklemek
        artefaktı çoğaltır (sentetik es/ru: '${}^{&}$' -> "Misplaced
        alignment tab character &"). (2) ortam dışı kaçışsız '&': inline
        matematikte hizalama sekmesi derleme hatasıdır.
        """
        if cls._BASELESS_SCRIPT_RE.match(expr):
            return False
        if re.search(r"(?<!\\)&", expr) and "\\begin" not in expr:
            return False
        return True

    @classmethod
    def _present_nearby(
        cls,
        full_expr: str,
        en_lines: List[str],
        en_idx: int,
        tr_lines: List[str],
        tr_idx: int,
        current_tr_line: str,
    ) -> bool:
        """``full_expr`` TR penceresinde EN penceresindeki kadar var mı?

        Satır hizalaması orantılıdır; kayma olunca "eksik" ifade aslında
        komşu satırdadır (sentetik fr: '$X^{e}$' bir önceki satırda).
        """
        w = cls._RESTORE_WINDOW
        tr_win = tr_lines[max(0, tr_idx - w):tr_idx] + [current_tr_line] \
            + tr_lines[tr_idx + 1:tr_idx + w + 1]
        en_win = en_lines[max(0, en_idx - w):en_idx + w + 1]
        tr_count = sum(line.count(full_expr) for line in tr_win)
        en_count = sum(line.count(full_expr) for line in en_win)
        return tr_count >= en_count

    def _align_lines(
        self,
        en_lines: List[str],
        tr_lines: List[str],
    ) -> List[Tuple[int, int]]:
        """Build line-by-line alignment between EN and TR using anchors.

        Anchors are structural LaTeX elements that don't change:
        - \\begin{...}, \\end{...}
        - \\label{...}
        - \\includegraphics{...}
        - Display math \\[, \\]
        - Equation tags \\tag{...}

        Between anchors, lines are matched proportionally.
        """
        anchor_re = re.compile(
            r"\\(?:begin|end)\{[^}]+\}|"
            r"\\label\{[^}]+\}|"
            r"\\includegraphics|"
            r"\\\[|\\\]|"
            r"\\tag\{[^}]+\}"
        )

        # Find anchor lines in EN
        en_anchors: List[Tuple[int, str]] = []
        for i, line in enumerate(en_lines):
            m = anchor_re.search(line)
            if m:
                en_anchors.append((i, m.group(0)))

        # Find matching anchors in TR
        tr_anchor_map: Dict[str, List[int]] = {}
        for j, line in enumerate(tr_lines):
            m = anchor_re.search(line)
            if m:
                key = m.group(0)
                tr_anchor_map.setdefault(key, []).append(j)

        # Build anchor pairs (greedy forward matching)
        anchor_pairs: List[Tuple[int, int]] = []
        tr_used: set = set()

        for en_idx, anchor_key in en_anchors:
            candidates = tr_anchor_map.get(anchor_key, [])
            for tr_idx in candidates:
                if tr_idx not in tr_used:
                    anchor_pairs.append((en_idx, tr_idx))
                    tr_used.add(tr_idx)
                    break

        # Sort by EN index
        anchor_pairs.sort()

        # Now fill in between anchors with proportional alignment
        alignment: List[Tuple[int, int]] = []

        # Add boundary anchors
        segments = [(0, 0)]  # start
        segments.extend(anchor_pairs)
        segments.append((len(en_lines) - 1, len(tr_lines) - 1))  # end

        for seg_i in range(len(segments) - 1):
            en_start, tr_start = segments[seg_i]
            en_end, tr_end = segments[seg_i + 1]

            en_span = en_end - en_start
            tr_span = tr_end - tr_start

            if en_span <= 0 or tr_span <= 0:
                continue

            # Proportional mapping within segment
            ratio = tr_span / en_span
            for offset in range(en_span):
                en_idx = en_start + offset
                tr_idx = tr_start + int(offset * ratio)
                if 0 <= tr_idx < len(tr_lines):
                    alignment.append((en_idx, tr_idx))

        return alignment

    def _find_math_insertion_point(
        self,
        en_line: str,
        original_tr_line: str,
        current_tr_line: str,
        math_expr: str,
    ) -> Optional[Tuple[int, str]]:
        """Find where to insert $math_expr$ in TR line.

        Uses surrounding text in EN line as anchors.
        Returns (position, anchor_description) or None.
        """
        full_math = f"${math_expr}$"
        en_pos = en_line.find(full_math)
        if en_pos < 0:
            return None

        # Get text before and after math in EN
        text_before = en_line[:en_pos].strip()
        text_after = en_line[en_pos + len(full_math):].strip()

        # Try to find a word from before the math in TR line
        # Take last 2-3 words before the math
        before_words = text_before.split()[-3:] if text_before else []
        after_words = text_after.split()[:3] if text_after else []

        # Strategy 1: Find the word just before the math in TR
        for word in reversed(before_words):
            # Skip very short words
            if len(word) < 3:
                continue
            # Clean LaTeX from word for matching
            clean = re.sub(r"\\[a-zA-Z]+\{[^}]*\}", "", word).strip()
            if not clean or len(clean) < 3:
                continue
            idx = current_tr_line.find(clean)
            if idx >= 0:
                # Insert after this word
                insert_pos = idx + len(clean)
                # Skip to next word boundary
                while (insert_pos < len(current_tr_line)
                       and current_tr_line[insert_pos] in " \t"):
                    insert_pos += 1
                return (insert_pos, f"after '{clean}'")

        # Strategy 2: Find word just after the math in TR
        for word in after_words:
            if len(word) < 3:
                continue
            clean = re.sub(r"\\[a-zA-Z]+\{[^}]*\}", "", word).strip()
            if not clean or len(clean) < 3:
                continue
            idx = current_tr_line.find(clean)
            if idx >= 0:
                return (idx, f"before '{clean}'")

        # Strategy 3: Proportional position
        en_fraction = en_pos / max(len(en_line), 1)
        insert_pos = int(en_fraction * len(current_tr_line))
        # Snap to word boundary
        while (insert_pos < len(current_tr_line)
               and current_tr_line[insert_pos] not in " \t\n"):
            insert_pos += 1
        return (insert_pos, "proportional")

    # ------------------------------------------------------------------
    # Utility: single-issue fix (for external use)
    # ------------------------------------------------------------------

    def fix_single(
        self,
        body: str,
        issue: QCIssue,
    ) -> Tuple[str, Optional[FixResult]]:
        """Apply a single fix for one QC issue.

        Returns (fixed_body, fix_result_or_None).
        """
        fix_fn = self._fix_registry.get(issue.check_name)
        if fix_fn is None:
            logger.warning(
                "No fix function for check '%s'.", issue.check_name,
            )
            return body, None

        math_spans = _find_math_spans(body)
        body, fixes = fix_fn(body, [issue], math_spans)

        return body, fixes[0] if fixes else None

    # ------------------------------------------------------------------
    # Fix: mixed_language_line (re-translate lines with mixed EN/TR)
    # ------------------------------------------------------------------

    def _fix_mixed_language_line(
        self,
        body: str,
        issues: List[QCIssue],
        math_spans: List[Tuple[int, int]],
    ) -> Tuple[str, List[FixResult]]:
        """Attempt to re-translate mixed-language lines."""
        fixes: List[FixResult] = []

        if self.translator is None:
            logger.info(
                "No translator available. %d mixed-language issues skipped.",
                len(issues),
            )
            return body, fixes

        lines = body.split("\n")

        # Build set of line indices inside tabular environments
        tabular_lines = _build_tabular_line_set(lines)

        for issue in sorted(issues, key=lambda i: i.line_num, reverse=True):
            line_idx = issue.line_num - 1  # line_num is 1-based, convert to 0-based
            if line_idx < 0 or line_idx >= len(lines):
                continue

            # Skip lines inside tabular environments -- re-translating
            # table cells destroys column structure.
            if line_idx in tabular_lines:
                logger.debug(
                    "Skipping mixed-language line %d: inside tabular env.",
                    issue.line_num,
                )
                continue

            old_line = lines[line_idx]
            stripped = old_line.strip()

            if not stripped or len(stripped) < 20:
                continue

            # Skip lines that are purely LaTeX commands
            if stripped.startswith("\\") and not any(
                c in stripped for c in "çğıöşüÇĞİÖŞÜ"
            ):
                continue

            try:
                # Strategy: find English sentences within the line and
                # retranslate only those, keeping Turkish parts intact.
                from latex_pipeline.translator import LatexTranslator
                splitter = LatexTranslator._SENTENCE_SPLIT_RE
                sentences = splitter.split(stripped)
                any_fixed = False
                fixed_sentences = []

                for sent in sentences:
                    if self.translator._is_english_sentence(sent) and len(sent.strip()) > 20:
                        # This sentence is English — retranslate it
                        try:
                            translator_engine = self.translator._get_translator()
                            tr = translator_engine.translate_text(sent.strip())
                            if tr and tr.strip() and not self.translator._is_english_sentence(tr):
                                tr = self.translator._apply_glossary(tr.strip())
                                fixed_sentences.append(tr)
                                any_fixed = True
                                logger.info(
                                    "AutoFix retranslated English sentence: %.50s → %.50s",
                                    sent[:50], tr[:50],
                                )
                                continue
                        except Exception:
                            pass
                    fixed_sentences.append(sent)

                if not any_fixed:
                    # AF-06: the sentence-level pass (which validates each
                    # retranslation with _is_english_sentence) found no English
                    # sentence to fix. Re-sending the WHOLE line -- Turkish
                    # parts and inline math included -- to MT without validation
                    # produced garbage and duplicated context. If nothing was
                    # fixed, the "mixed" flag was a false positive: leave the
                    # line untouched.
                    continue

                translated = " ".join(fixed_sentences)

                if translated and translated != stripped:
                    ws = old_line[:len(old_line) - len(old_line.lstrip())]
                    lines[line_idx] = ws + translated
                    fixes.append(FixResult(
                        check_name="mixed_language_line",
                        line_number=line_idx + 1,
                        old_text=stripped[:80],
                        new_text=translated[:80],
                        method="retranslate",
                    ))
            except Exception as exc:
                logger.warning(
                    "Re-translation failed for mixed line %d: %s",
                    line_idx + 1, exc,
                )

        return "\n".join(lines), fixes

    # ------------------------------------------------------------------
    # Fix: broken_latex_syntax
    # ------------------------------------------------------------------

    def _fix_broken_latex_syntax(
        self,
        body: str,
        issues: List[QCIssue],
        math_spans: List[Tuple[int, int]],
    ) -> Tuple[str, List[FixResult]]:
        """Fix common broken LaTeX syntax patterns."""
        fixes: List[FixResult] = []

        # Fix \textit> instead of \textit{...}
        # The MT model sometimes outputs \textit>text instead of \textit{text}
        # We need to add both the opening { and the closing }
        textit_broken_re = re.compile(r"\\textit>([^\\}\n]+?)(?=[\s\\}.,;:\)]|$)")
        if textit_broken_re.search(body):
            def _fix_textit(m: re.Match) -> str:
                return f"\\textit{{{m.group(1).strip()}}}"
            old_body = body
            body = textit_broken_re.sub(_fix_textit, body)
            if body != old_body:
                fixes.append(FixResult(
                    check_name="broken_latex_syntax",
                    line_number=0,
                    old_text=r"\textit>...",
                    new_text=r"\textit{...}",
                    method="regex_replace",
                ))
        # Fallback: simple \textit> without content match
        if r"\textit>" in body:
            body = body.replace(r"\textit>", r"\textit{")
            fixes.append(FixResult(
                check_name="broken_latex_syntax",
                line_number=0,
                old_text=r"\textit>",
                new_text=r"\textit{",
                method="string_replace",
            ))

        # Fix ![](image) markdown syntax remaining in LaTeX
        # Collect all replacements first, apply in reverse order to preserve offsets
        md_img_re = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
        from pathlib import Path
        img_replacements = []
        for m in md_img_re.finditer(body):
            start = m.start()
            if any(s <= start < e for s, e in math_spans):
                continue
            old = m.group(0)
            src = m.group(2).replace(r"\_", "_")
            img_name = Path(src).name
            new = f"\\includegraphics[width=0.8\\textwidth]{{{img_name}}}"
            line_num = body[:start].count("\n") + 1
            img_replacements.append((start, m.end(), old, new, line_num))

        # Apply in reverse order to preserve string offsets
        for start, end, old, new, line_num in reversed(img_replacements):
            body = body[:start] + new + body[end:]
            fixes.append(FixResult(
                check_name="broken_latex_syntax",
                line_number=line_num,
                old_text=old,
                new_text=new,
                method="regex_replace",
            ))

        # Fix Japanese/Asian brackets used instead of braces
        for bad_char, good_char in [("』", "}"), ("》", "}"), ("《", "{")]:
            if bad_char in body:
                body = body.replace(bad_char, good_char)
                fixes.append(FixResult(
                    check_name="broken_latex_syntax",
                    line_number=0,
                    old_text=bad_char,
                    new_text=good_char,
                    method="string_replace",
                ))

        # Fix Markdown links [text](#page-xxx) remaining in LaTeX
        md_link_re = re.compile(
            r"\[([^\]]*)\]\(\\?#page-\d+-\d+\)"
        )
        if md_link_re.search(body):
            body = md_link_re.sub(r"\1", body)
            fixes.append(FixResult(
                check_name="broken_latex_syntax",
                line_number=0,
                old_text="[text](#page-xxx)",
                new_text="text",
                method="regex_replace",
            ))

        # Fix Markdown bold links [[N](#page-xxx)] -> N
        md_bold_link_re = re.compile(
            r"\[(\d+(?:,\s*\d+)*)\]\(\\?#page-\d+-\d+\)"
        )
        if md_bold_link_re.search(body):
            body = md_bold_link_re.sub(r"\1", body)
            fixes.append(FixResult(
                check_name="broken_latex_syntax",
                line_number=0,
                old_text="[N](#page-xxx)",
                new_text="N",
                method="regex_replace",
            ))

        # Fix unclosed titlepage braces: {\Huge\bfseries ...\par without }
        # and {\Large ...\par without }
        body, tp_fixes = self._fix_unclosed_titlepage_braces(body)
        fixes.extend(tp_fixes)

        # Fix [[BIRIM_N]] / [[UNIT_N]] placeholder remnants
        birim_re = re.compile(r"\[\[(?:BIRIM|BİRİM|UNIT)_\d+\]\]")
        if birim_re.search(body):
            body = birim_re.sub("", body)
            fixes.append(FixResult(
                check_name="broken_latex_syntax",
                line_number=0,
                old_text="[[BIRIM_N]]",
                new_text="",
                method="regex_replace",
            ))

        # Fix LaTeX-encoded Turkish chars → UTF-8 (for XeLaTeX)
        encoding_fixes = [
            (r"\\c\{S\}ekil", "Şekil"),
            (r'B\\"\{o\}l\\"\{u\}m', "Bölüm"),
            (r"\\c\{s\}ekil", "şekil"),
            (r"Tan\{\\i\}m", "Tanım"),
            (r'\\"\{O\}nerme', "Önerme"),
            (r"Sonu\\c\{c\}", "Sonuç"),
            (r'\\"\{O\}rnek', "Örnek"),
            (r'B\\"\{o\}l\\"\{u\}m', "Bölüm"),
        ]
        for pat, repl in encoding_fixes:
            enc_re = re.compile(pat)
            if enc_re.search(body):
                body = enc_re.sub(repl, body)
                fixes.append(FixResult(
                    check_name="broken_latex_syntax",
                    line_number=0,
                    old_text=pat,
                    new_text=repl,
                    method="regex_replace",
                ))

        return body, fixes

    # ------------------------------------------------------------------
    # Fix: unclosed titlepage braces
    # ------------------------------------------------------------------

    # Regex to detect the START of a titlepage sizing block
    _TITLEPAGE_OPEN_RE = re.compile(
        r"^\s*\{\\(?:Huge\\bfseries|Large)\b"
    )

    def _fix_unclosed_titlepage_braces(
        self, body: str,
    ) -> Tuple[str, List[FixResult]]:
        """Fix unclosed braces in titlepage sizing commands.

        Handles two cases:

        1. Single-line: ``{\\Huge\\bfseries Title\\par`` (missing ``}``)
           Fixed to: ``{\\Huge\\bfseries Title}\\par``

        2. Multi-line: a ``{\\Large`` block spanning several lines where the
           last line ends with ``\\par`` but the closing brace is missing.
           The ``}`` is inserted before ``\\par`` on the last line of the block.

        Only fixes blocks where braces are unbalanced (more ``{`` than ``}``).
        """
        fixes: List[FixResult] = []
        lines = body.split("\n")
        changed = False

        i = 0
        while i < len(lines):
            line = lines[i]

            # Check if this line starts a titlepage sizing block
            if not self._TITLEPAGE_OPEN_RE.match(line):
                i += 1
                continue

            # Accumulate lines until we find the end of the block
            # (a line ending with \par, \par}, }\par, or \vfill / \end)
            block_start = i
            block_end = i  # inclusive
            brace_total_open = 0
            brace_total_close = 0

            for j in range(i, len(lines)):
                brace_total_open += lines[j].count("{")
                brace_total_close += lines[j].count("}")
                block_end = j

                stripped_j = lines[j].strip()
                # End of block: line ends with \par, \par}, or }\par
                if (stripped_j.endswith("\\par")
                        or stripped_j.endswith("\\par}")
                        or stripped_j.endswith("}\\par")):
                    break
                # Also end if we hit \vfill or \end (block was never closed)
                if stripped_j in ("\\vfill", "\\end{titlepage}"):
                    # Back up one line -- the previous line was the last content
                    if j > i:
                        block_end = j - 1
                    break

            # Check brace balance for the entire block
            if brace_total_open <= brace_total_close:
                i = block_end + 1
                continue  # braces already balanced

            # Fix the last line of the block: insert } before \par
            last_line = lines[block_end]
            if last_line.rstrip().endswith("\\par"):
                # Insert } before \par
                rstripped = last_line.rstrip()
                new_line = rstripped[:-4] + "}\\par"
                if new_line != last_line:
                    lines[block_end] = new_line
                    changed = True
                    fixes.append(FixResult(
                        check_name="broken_latex_syntax",
                        line_number=block_end + 1,
                        old_text=last_line.strip()[:80],
                        new_text=new_line.strip()[:80],
                        method="brace_close",
                    ))
                    logger.debug(
                        "Fixed unclosed titlepage brace at line %d "
                        "(block started at line %d)",
                        block_end + 1, block_start + 1,
                    )
            elif not (last_line.rstrip().endswith("\\par}")
                      or last_line.rstrip().endswith("}\\par")):
                # Block ends without \par -- append }\par
                new_line = last_line.rstrip() + "}\\par"
                lines[block_end] = new_line
                changed = True
                fixes.append(FixResult(
                    check_name="broken_latex_syntax",
                    line_number=block_end + 1,
                    old_text=last_line.strip()[:80],
                    new_text=new_line.strip()[:80],
                    method="brace_close",
                ))

            i = block_end + 1

        if changed:
            body = "\n".join(lines)

        return body, fixes

    # ------------------------------------------------------------------
    # Fix: brace_balance
    # ------------------------------------------------------------------

    def _fix_brace_balance(
        self,
        body: str,
        issues: List[QCIssue],
        math_spans: List[Tuple[int, int]],
    ) -> Tuple[str, List[FixResult]]:
        """Fix brace imbalance by adding/removing braces at problem points.

        Strategy:
        - For small imbalances (|diff| <= 3): scan line-by-line and fix
          the specific lines where the balance goes wrong.
        - For titlepage regions: specifically fix unclosed sizing commands.
        """
        fixes: List[FixResult] = []

        for issue in issues:
            # Titlepage-specific fix
            if "Titlepage" in issue.message:
                body, tp_fixes = self._fix_unclosed_titlepage_braces(body)
                fixes.extend(tp_fixes)
                continue

            # Global brace imbalance -- extract diff from message
            diff_match = re.search(r"diff=([+-]?\d+)", issue.message)
            if not diff_match:
                continue
            diff = int(diff_match.group(1))

            if abs(diff) > 3:
                continue

            lines = body.split("\n")
            balance = 0
            fixed_count = 0

            for i, line in enumerate(lines):
                cleaned = re.sub(r"\\[{}]", "", line)
                line_opens = cleaned.count("{")
                line_closes = cleaned.count("}")
                balance += line_opens - line_closes

                if diff > 0 and balance > 5 and line_opens > line_closes:
                    if re.search(
                        r"\{\\(?:Huge|Large|large|LARGE|huge|"
                        r"bfseries|itshape|scshape)\b",
                        line,
                    ) and not line.rstrip().endswith("}"):
                        lines[i] = line.rstrip() + "}"
                        fixed_count += 1
                        balance -= 1
                        fixes.append(FixResult(
                            check_name="brace_balance",
                            line_number=i + 1,
                            old_text=line.strip()[:60],
                            new_text=lines[i].strip()[:60],
                            method="add_close_brace",
                        ))
                        if fixed_count >= abs(diff):
                            break

                elif diff < 0 and balance < 0 and line_closes > line_opens:
                    # 2026-07-11 (#16): 'line_closes > line_opens' sarti
                    # eklendi. Eskiden balance negatife dustukten sonraki
                    # ILK '}' ile biten satir budaniyordu — o satir cogu kez
                    # SAGLAM bir \end{figure}/\section{X} idi (fazla '}'
                    # baska satirdaydi); satir-ici guard '1>=0' gectigi icin
                    # yapisal satirin kapanisi siliniyor, brace SAYISI
                    # duzeldigi icin kabul kapisi onayliyordu. Artik yalniz
                    # yerel kapanis-fazlasi olan satir (gercek suclu adayi,
                    # orn. '\\section{Baslik}}') aday olabilir.
                    stripped = line.rstrip()
                    if stripped.endswith("}") and not stripped.endswith(r"\}"):
                        test_line = stripped[:-1]
                        test_cleaned = re.sub(r"\\[{}]", "", test_line)
                        if test_cleaned.count("{") >= test_cleaned.count("}"):
                            lines[i] = test_line
                            fixed_count += 1
                            balance += 1
                            fixes.append(FixResult(
                                check_name="brace_balance",
                                line_number=i + 1,
                                old_text=stripped[:60],
                                new_text=test_line[:60],
                                method="remove_extra_brace",
                            ))
                            if fixed_count >= abs(diff):
                                break

            if fixed_count > 0:
                body = "\n".join(lines)

        return body, fixes

    # ------------------------------------------------------------------
    # Fix: markdown_link_remnant
    # ------------------------------------------------------------------

    def _fix_markdown_link_remnant(
        self,
        body: str,
        issues: List[QCIssue],
        math_spans: List[Tuple[int, int]],
    ) -> Tuple[str, List[FixResult]]:
        """Remove or convert markdown link remnants to LaTeX.

        - [[N](#page-xxx)] -> N (keep just the reference number)
        - [[N\\]](#page) -> N
        - ![alt](src) -> \\includegraphics{src}
        - [text](url) -> text (for internal links) or \\href{url}{text}
        """
        fixes: List[FixResult] = []

        # Pattern 1: double-bracket links [[N](#page-...)]
        p1 = re.compile(
            r"\[\[(\d+(?:,\s*\d+)*)"
            r"(?:\\?\])?"
            r"\]\(\\?#[^)]*\)\]?"
        )
        body, changes = _safe_sub(p1, r"\1", body, math_spans)
        for old, new, pos in changes:
            line_num = body[:pos].count("\n") + 1
            fixes.append(FixResult(
                check_name="markdown_link_remnant",
                line_number=line_num,
                old_text=old[:60],
                new_text=new,
                method="regex_replace",
            ))

        # Pattern 2: escaped bracket [[N\]](#page)
        p2 = re.compile(
            r"\[\[(\d+(?:,\s*\d+)*)"
            r"\\?\]\]"
            r"\(\\?#[^)]*\)"
        )
        body, changes = _safe_sub(p2, r"\1", body, math_spans)
        for old, new, pos in changes:
            line_num = body[:pos].count("\n") + 1
            fixes.append(FixResult(
                check_name="markdown_link_remnant",
                line_number=line_num,
                old_text=old[:60],
                new_text=new,
                method="regex_replace",
            ))

        # Pattern 3: [N\](#page-...)
        p3 = re.compile(
            r"\[(\d+(?:,\s*\d+)*)\\?\]\(\\?#page[^)]*\)"
        )
        body, changes = _safe_sub(p3, r"\1", body, math_spans)
        for old, new, pos in changes:
            line_num = body[:pos].count("\n") + 1
            fixes.append(FixResult(
                check_name="markdown_link_remnant",
                line_number=line_num,
                old_text=old[:60],
                new_text=new,
                method="regex_replace",
            ))

        # Pattern 4: generic [text](#internal-link) -> text
        p4 = re.compile(
            r"\[([^\]]{1,80})\]\(\\?#[^)]+\)"
        )
        body, changes = _safe_sub(p4, r"\1", body, math_spans)
        for old, new, pos in changes:
            line_num = body[:pos].count("\n") + 1
            fixes.append(FixResult(
                check_name="markdown_link_remnant",
                line_number=line_num,
                old_text=old[:60],
                new_text=new,
                method="regex_replace",
            ))

        # Pattern 5: ![alt](src) -> \includegraphics
        p5 = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
        from pathlib import Path as _Path

        matches = list(p5.finditer(body))
        safe_matches = [
            m for m in matches
            if not _is_inside_math(m.start(), math_spans)
        ]
        for m in reversed(safe_matches):
            old = m.group(0)
            src = m.group(2).replace(r"\_", "_")
            img_name = _Path(src).name
            new = f"\\includegraphics[width=0.8\\textwidth]{{{img_name}}}"
            body = body[:m.start()] + new + body[m.end():]
            line_num = body[:m.start()].count("\n") + 1
            fixes.append(FixResult(
                check_name="markdown_link_remnant",
                line_number=line_num,
                old_text=old[:60],
                new_text=new[:60],
                method="regex_replace",
            ))

        if fixes:
            logger.info(
                "Markdown link cleanup: removed %d remnants.", len(fixes),
            )

        return body, fixes

    # ------------------------------------------------------------------
    # Fix: table_column_mismatch (not auto-fixable)
    # ------------------------------------------------------------------

    def _fix_table_column_mismatch(
        self,
        body: str,
        issues: List[QCIssue],
        math_spans: List[Tuple[int, int]],
    ) -> Tuple[str, List[FixResult]]:
        """Table column mismatches cannot be safely auto-fixed.

        We log them for manual review.
        """
        for issue in issues:
            logger.warning(
                "Table column mismatch at L%d (manual fix required): %s",
                issue.line_num, issue.message,
            )
        return body, []

    # ------------------------------------------------------------------
    # Fix: untranslated_heading
    # ------------------------------------------------------------------

    # NOTE (DATA-04/AF-04/AF-C6): The local ASCII-broken _HEADING_GLOSSARY
    # (159 entries, all diacritic-stripped, several typos, 50 keys
    # conflicting with the canonical translator glossary) was DELETED.
    # _fix_untranslated_heading now reuses the single canonical source,
    # LatexTranslator._HEADING_GLOSSARY (proper Turkish, Title Case), via
    # a lazy import -- one terminology authority, no producer/consumer loop.

    def _fix_untranslated_heading(
        self,
        body: str,
        issues: List[QCIssue],
        math_spans: List[Tuple[int, int]],
    ) -> Tuple[str, List[FixResult]]:
        """Translate untranslated headings using the translator or glossary.

        Priority:
        1. If the translator exposes ``_translate_heading_text`` (the single
           heading-translation authority), delegate to it; otherwise fall
           back to ``_safe_translate`` / ``translate``. The result must pass
           the acceptance gate (AF-C7) or it is rejected.
        2. If no usable translation, fall back to the canonical heading
           glossary (LatexTranslator._HEADING_GLOSSARY) by exact match only.
        3. Otherwise log and skip (no partial-match garbage -- AF-C4).
        """
        fixes: List[FixResult] = []

        # Canonical heading glossary is owned by the translator (single source
        # of truth). Lazy import avoids a circular dependency.
        try:
            from latex_pipeline.translator import LatexTranslator
            glossary_lower = {
                k.lower(): v
                for k, v in LatexTranslator._HEADING_GLOSSARY.items()
            }
        except Exception:  # pragma: no cover - translator import failure
            glossary_lower = {}

        lines = body.split("\n")
        # 2026-09-24: başlık argümanı PARANTEZ-DENGELİ okunur. Eski
        # `([^}]+)` deseni "\section{The \textit{n}-Body Problem}" başlığını
        # ilk '}'te kesip "The \textit{n" olarak MT'ye gönderiyor, dönen
        # çöp brace dengesini bozup kategori geri alınıyordu (bir test koşusu).
        from latex_pipeline.retranslator import _split_heading_shell

        for issue in sorted(issues, key=lambda i: i.line_num, reverse=True):
            line_idx = issue.line_num - 1
            if line_idx < 0 or line_idx >= len(lines):
                continue

            line = lines[line_idx]
            lead = line[:len(line) - len(line.lstrip())]
            shell = _split_heading_shell(line.lstrip())
            if not shell:
                continue

            cmd, heading, tail = shell
            close = "}"
            _head_start = len(lead)
            _head_end = len(line) - len(tail)

            # Strip numbering prefix like "6.3. " from the heading text
            # so we can look up the bare title in the glossary.
            num_prefix_re = re.compile(r"^(\d+(?:\.\d+)*\.?\s*)")
            num_match = num_prefix_re.match(heading)
            bare_heading = heading[num_match.end():] if num_match else heading
            prefix = heading[:num_match.end()] if num_match else ""

            translated = None
            method = "glossary"

            # --- Priority 1: Delegate to the translator's heading authority ---
            # Send the BARE title (no numbering prefix) so MT can't mangle or
            # translate the number, then re-attach the prefix (AF-10).
            if self.translator is not None:
                try:
                    if hasattr(self.translator, "_translate_heading_text"):
                        cand = self.translator._translate_heading_text(bare_heading)
                    elif hasattr(self.translator, "_safe_translate"):
                        cand = self.translator._safe_translate(bare_heading)
                    elif hasattr(self.translator, "translate"):
                        cand = self.translator.translate(bare_heading)
                    else:
                        cand = None
                    # Acceptance gate (AF-C7): reject echo / still-English /
                    # empty so a garbage heading is never written.
                    if self._accept_retranslation(
                        bare_heading, cand, is_heading=True,
                        source_lang=self._source_lang(),
                    ):
                        translated = prefix + cand.strip()
                        method = "retranslate"
                except Exception as exc:
                    logger.warning(
                        "Heading translation failed at L%d: %s",
                        issue.line_num, exc,
                    )
                    translated = None

            # --- Priority 2: Exact-match canonical glossary fallback ---
            # No partial/substring match: it only ever produced half-English,
            # single-capital, ASCII garbage (AF-C4), so it was removed.
            if not translated:
                bare_lower = bare_heading.strip().lower()
                if bare_lower in glossary_lower:
                    translated = prefix + glossary_lower[bare_lower]
                    method = "glossary"

            if translated and translated.strip() != heading.strip():
                new_line = (
                    line[:_head_start]
                    + cmd + translated.strip() + close
                    + line[_head_end:]
                )
                lines[line_idx] = new_line
                fixes.append(FixResult(
                    check_name="untranslated_heading",
                    line_number=issue.line_num,
                    old_text=heading[:60],
                    new_text=translated.strip()[:60],
                    method=method,
                ))
                logger.info(
                    "Translated heading at L%d (%s): %.40s -> %.40s",
                    issue.line_num, method, heading, translated.strip(),
                )
            else:
                logger.warning(
                    "Could not translate heading at L%d: %.60s "
                    "(not in glossary, no translator)",
                    issue.line_num, heading,
                )

        if fixes:
            body = "\n".join(lines)

        return body, fixes

    # ------------------------------------------------------------------
    # Fix: broken_inline_math
    # ------------------------------------------------------------------

    # Regex patterns for broken inline math detection and repair
    _STANDALONE_SCRIPT_RE = re.compile(
        r"(?<!\$)\$\s*([_^]\{[^}]+\})\s*\$(?!\$)"
    )
    # Consecutive inline math fragments with 0-5 chars between them
    _CONSECUTIVE_MATH_RE = re.compile(
        r"\$([^$]+)\$"
        r"(\s{0,5}[)(\[\],;:\s]{0,5}\s{0,5})"
        r"\$([^$]+)\$"
    )
    # Multi-char subscript/superscript without braces: _AB or ^AB (2+ chars)
    _BARE_MULTICHAR_SCRIPT_RE = re.compile(
        r"(?<=[\$])"           # inside math (preceded by content)
        r"([_^])"              # sub or superscript operator
        r"([A-Za-z0-9]{2,})"   # 2+ alphanumeric chars NOT in braces
        r"(?=[^{]|$)"          # NOT followed by opening brace
    )
    # Text immediately adjacent to closing $ without space (math-text merge)
    _MATH_TEXT_MERGE_RE = re.compile(
        r"\$([^$]+)\$([A-ZÇĞİÖŞÜa-zçğıöşü]{2,})"
    )
    # Turkish characters that should NOT appear inside inline math
    _TURKISH_CHARS_IN_MATH = re.compile(
        r"(?<!\$)\$([^$]*[çğıöşüÇĞİÖŞÜ][^$]*)\$(?!\$)"
    )
    # F25 (2026-07-29): standalone script attach (yukarıdaki
    # _STANDALONE_SCRIPT_RE yalnız TESPİT içindir, burası BİRLEŞTİRME).
    # Taban = kısa ASCII tanımlayıcı (R, CO, ...), ayraç = yalnız boşluk/tab.
    # Nokta ayraç olarak KABUL EDİLMEZ: "cümle. $^{3}$" dipnot işaretidir,
    # değişken üssü değil.
    _SCRIPT_ATTACH_RE = re.compile(
        r"(\b[A-Za-z][A-Za-z0-9]{0,2})"   # preceding short ASCII identifier
        r"([ \t]{0,3})"                    # optional spaces (never a period)
        r"\$\s*([_^]\{[^}]+\})\s*\$"      # standalone script
    )

    @staticmethod
    def _attach_standalone_script(m: "re.Match") -> str:
        """Tabanı script'e bağla; Türkçe işlev sözcüğüyse dokunma (F25).

        1-3 harflik ASCII filtresi "Bir $^{14}$C" gibi vakaları da
        geçiriyordu; işlev sözcükleri değişken adı olmaz.
        """
        if m.group(1).lower() in _TURKISH_COMMON_WORDS:
            return m.group(0)
        return "$" + m.group(1) + m.group(3) + "$"

    def _fix_broken_inline_math(
        self,
        body: str,
        issues: List[QCIssue],
        math_spans: List[Tuple[int, int]],
    ) -> Tuple[str, List[FixResult]]:
        """Fix broken inline math expressions.

        Handles 4 common MT-induced math corruption patterns:

        1. **Standalone superscript/subscript**: ``$^{M}$`` without base
           variable.  Attempts to attach to preceding word if it looks
           like a variable (single letter or short identifier).

        2. **Consecutive fragment merge**: ``$x$ $^{2}$`` -> ``$x^{2}$``
           when two adjacent inline math expressions should be one.

        3. **Bare multi-char subscript braces**: ``$x_AB$`` -> ``$x_{AB}$``
           where a subscript/superscript has 2+ chars without braces.

        4. **Math-text boundary repair**: ``$\\cdot$Text`` where closing
           ``$`` runs into Turkish text without a space.

        Safety:
            - Only fixes lines flagged by QC (issue.line_num).
            - Skips lines inside display math environments.
            - Logs every change for auditability.
        """
        fixes: List[FixResult] = []

        if not issues:
            return body, fixes

        # Collect line numbers from issues for targeted fixing
        issue_lines = {issue.line_num for issue in issues}

        lines = body.split("\n")

        for line_idx in sorted(issue_lines):
            if line_idx < 1 or line_idx > len(lines):
                continue
            idx = line_idx - 1
            original_line = lines[idx]
            fixed_line = original_line

            # Skip if inside a display math environment
            if re.match(
                r"\s*\\(?:begin|end)\{(?:equation|align|gather|"
                r"multline|split|flalign|alignat)",
                original_line.strip(),
            ):
                continue

            # --- Strategy 0: Clean empty braces in scripts ---
            # Converter produces ${}^{K}$ from <sup>K</sup> — the {}
            # is an empty group that prevents later merge/attach logic.
            # Strip it: ${}^{X}$ -> $^{X}$  and  ${}_{X}$ -> $_{X}$
            empty_brace_script_re = re.compile(
                r"\$\s*\{\}\s*([_^]\{[^}]+\})\s*\$"
            )
            # R6a: yalnız gerçek inline bölge ('$${}^{1}$$' display'in
            # içinden '${}^{1}$' kesilmez).
            zones = _inline_math_zones(fixed_line)
            new_line = empty_brace_script_re.sub(
                lambda m: ("$" + m.group(1) + "$"
                           if (m.start(), m.end()) in zones else m.group(0)),
                fixed_line,
            )
            if new_line != fixed_line:
                fixes.append(FixResult(
                    check_name="broken_inline_math",
                    line_number=line_idx,
                    old_text=fixed_line[:80],
                    new_text=new_line[:80],
                    method="empty_brace_cleanup",
                ))
                fixed_line = new_line

            # --- Strategy 1: Standalone superscript/subscript ---
            # Pattern: "R $^{2}$" / "CO$_{2}$" -> "$R^{2}$" / "$CO_{2}$"
            # F25 (2026-07-29): eski desen ayraç olarak NOKTAYI da kabul
            # ediyordu ([.\s]{0,3}) ve ayraç grubu çıktıdan atıldığı için
            # cümle sonu noktası siliniyordu; ayrıca taban \w Unicode
            # olduğundan 7 harfe kadar Türkçe kelime math moduna çekiliyordu
            # ("deger. $^{3}$" -> "$deger^{3}$"). Artık taban yalnız kısa
            # ASCII tanımlayıcı (1-3), ayraç yalnız boşluk/tab: nokta içeren
            # dipnot-stili üst simge hiç eşleşmez, noktalama korunur.
            zones = _inline_math_zones(fixed_line)
            new_line = self._SCRIPT_ATTACH_RE.sub(
                lambda m: (self._attach_standalone_script(m)
                           if (m.end(2), m.end()) in zones else m.group(0)),
                fixed_line,
            )
            if new_line != fixed_line:
                fixes.append(FixResult(
                    check_name="broken_inline_math",
                    line_number=line_idx,
                    old_text=fixed_line[:80],
                    new_text=new_line[:80],
                    method="standalone_script_attach",
                ))
                fixed_line = new_line

            # Also handle: "number $^{X}$" -> "$number^{X}$"
            # e.g., "2 $^{M}$" -> "$2^{M}$"
            num_script_re = re.compile(
                r"(\b\d+)"             # preceding number
                r"(\s{0,3})"           # optional space
                r"\$\s*([_^]\{[^}]+\})\s*\$"  # standalone script
            )
            zones = _inline_math_zones(fixed_line)
            new_line = num_script_re.sub(
                lambda m: ("$" + m.group(1) + m.group(3) + "$"
                           if (m.end(2), m.end()) in zones else m.group(0)),
                fixed_line,
            )
            if new_line != fixed_line:
                fixes.append(FixResult(
                    check_name="broken_inline_math",
                    line_number=line_idx,
                    old_text=fixed_line[:80],
                    new_text=new_line[:80],
                    method="num_script_attach",
                ))
                fixed_line = new_line

            # --- Strategy 2: Consecutive math fragment merge ---
            # Pattern: "$A$ $B$" where B starts with ^/_ -> "$AB$"

            # Build a set of EN source atoms for this line so we can
            # skip merges when EN intentionally has separate expressions.
            # Also count EN math to prevent merges that would reduce TR
            # math count below EN count.
            en_source_atoms: set = set()
            en_line_math_count = 0
            if self._source_body:
                en_src_lines = self._source_body.split("\n")
                # Map TR line to approximate EN line (proportional)
                ratio = len(en_src_lines) / max(len(lines), 1)
                en_center = int(idx * ratio)
                window = 3
                for k in range(
                    max(0, en_center - window),
                    min(len(en_src_lines), en_center + window + 1),
                ):
                    for atom in self._INLINE_MATH_PAT.findall(
                        en_src_lines[k]
                    ):
                        en_source_atoms.add(atom)
                # Count math in the closest EN line
                if 0 <= en_center < len(en_src_lines):
                    en_line_math_count = len(
                        self._INLINE_MATH_PAT.findall(en_src_lines[en_center])
                    )

            # Count current TR line math
            tr_line_math_count = len(
                self._INLINE_MATH_PAT.findall(fixed_line)
            )

            # R6a: birleştirme yalnız iki GERÇEK inline bölge arasında; her
            # turda güncel satırdan yeniden hesaplanır.
            merge_zones: set = set()

            def _merge_consecutive(m):
                if ((m.start(1) - 1, m.end(1) + 1) not in merge_zones
                        or (m.start(3) - 1, m.end(3) + 1) not in merge_zones):
                    return m.group(0)
                first = m.group(1).strip()
                sep = m.group(2).strip()
                second = m.group(3).strip()

                # Safety: if EN source is available, check whether
                # merging would drop TR math count below EN.
                # Merging two $...$ into one loses 1 math expression.
                # Also skip merge if any of the atoms (or sub-atoms)
                # exist separately in EN source -- the separation is
                # intentional in the original.
                if en_source_atoms:
                    # Check if first or second (or their space-split
                    # sub-parts) are EN atoms
                    first_atoms = first.split()
                    second_atoms = second.split()
                    all_parts = first_atoms + second_atoms

                    # If ANY part is a standalone EN atom, the
                    # separation is likely intentional
                    any_is_en_atom = any(
                        p in en_source_atoms for p in all_parts
                    )

                    if any_is_en_atom:
                        # Exception: still merge scripts that belong
                        # to a base (e.g. $x$ $^{2}$  -> $x^{2}$)
                        is_script_attach = (
                            second.startswith("^")
                            or second.startswith("_")
                            or second.startswith("{}^")
                            or second.startswith("{}_")
                            or first.endswith("^")
                            or first.endswith("_")
                        )
                        if not is_script_attach:
                            return m.group(0)  # leave as-is

                # Merge if second starts with ^ or _ (direct or after {})
                # R6b: aynı türden ikinci simge boş çekirdekle ayrılır.
                if second.startswith("^") or second.startswith("_"):
                    return "$" + _join_math_scripts(first, second) + "$"
                if second.startswith("{}^") or second.startswith("{}_"):
                    return "$" + first + second + "$"
                # Merge if first ends with ^ or _
                if first.endswith("^") or first.endswith("_"):
                    return "$" + first + second + "$"
                # Merge if both are math operators/symbols (e.g. \times \cdot)
                if (
                    re.match(r"^\\(?:times|cdot|pm|mp|cap|cup|circ|bullet)$", first)
                    and re.match(r"^\\(?:times|cdot|pm|mp|cap|cup|circ|bullet)$", second)
                    and sep in ("", " ")
                ):
                    return "$" + first + " " + second + "$"
                # Merge if separator is empty/minimal and both are short
                if sep in ("", " ") and (len(first) <= 5 or len(second) <= 5):
                    return "$" + first + " " + second + "$"
                return m.group(0)  # leave as-is

            # Apply repeatedly since merges can cascade
            for _ in range(3):
                merge_zones = _inline_math_zones(fixed_line)
                new_line = self._CONSECUTIVE_MATH_RE.sub(
                    _merge_consecutive, fixed_line,
                )
                if new_line == fixed_line:
                    break
                fixes.append(FixResult(
                    check_name="broken_inline_math",
                    line_number=line_idx,
                    old_text=fixed_line[:80],
                    new_text=new_line[:80],
                    method="consecutive_merge",
                ))
                fixed_line = new_line

            # --- Strategy 3: Multi-char subscript braces ---
            # Pattern inside $...$: _AB -> _{AB}
            def _fix_bare_scripts(line_text):
                """Add braces to bare multi-char sub/superscripts."""
                result = []
                i = 0
                in_math = False
                while i < len(line_text):
                    ch = line_text[i]
                    # Toggle math mode on unescaped $
                    if ch == '$' and (i == 0 or line_text[i-1] != '\\'):
                        # Check for $$ (display)
                        if i + 1 < len(line_text) and line_text[i+1] == '$':
                            result.append('$$')
                            i += 2
                            continue
                        in_math = not in_math
                        result.append(ch)
                        i += 1
                        continue

                    if in_math and ch in ('_', '^'):
                        # Check what follows
                        rest = line_text[i+1:]
                        if rest and rest[0] == '{':
                            # Already braced, skip
                            result.append(ch)
                            i += 1
                            continue
                        # Collect consecutive alphanumeric
                        j = 0
                        while j < len(rest) and rest[j].isalnum():
                            j += 1
                        if j >= 2:
                            # Multi-char without braces -> add braces
                            result.append(ch + '{' + rest[:j] + '}')
                            i += 1 + j
                            continue
                    result.append(ch)
                    i += 1
                return ''.join(result)

            new_line = _fix_bare_scripts(fixed_line)
            if new_line != fixed_line:
                fixes.append(FixResult(
                    check_name="broken_inline_math",
                    line_number=line_idx,
                    old_text=fixed_line[:80],
                    new_text=new_line[:80],
                    method="subscript_brace_fix",
                ))
                fixed_line = new_line

            # --- Strategy 4: Math-text boundary ---
            # Pattern: "$expr$TurkishText" -> "$expr$ TurkishText"
            zones = _inline_math_zones(fixed_line)
            new_line = self._MATH_TEXT_MERGE_RE.sub(
                lambda m: ("$" + m.group(1) + "$ " + m.group(2)
                           if (m.start(), m.end(1) + 1) in zones
                           else m.group(0)),
                fixed_line,
            )
            if new_line != fixed_line:
                fixes.append(FixResult(
                    check_name="broken_inline_math",
                    line_number=line_idx,
                    old_text=fixed_line[:80],
                    new_text=new_line[:80],
                    method="math_text_boundary",
                ))
                fixed_line = new_line

            # --- Strategy 5: Merged inline math split ---
            # MT model sometimes merges separate inline math atoms:
            #   "$\times$ $\cdot$" -> "$\times \cdot$"
            # Detect $A B$ where A and B are independent math atoms
            # separated by whitespace, and split back to $A$ $B$.
            #
            # An "atom" is either:
            #   - A single LaTeX command: \times, \cdot, \alpha, etc.
            #   - A single character/digit: x, N, 2, +, -, etc.
            # We do NOT split real expressions like $x^{2}$, $a + b$,
            # $\frac{a}{b}$, or anything with braces/scripts/operators
            # between atoms.
            #
            # Safety: if EN source is available, only split if TR line
            # has FEWER inline math than EN line (splitting increases
            # count and should not exceed EN).

            skip_atom_split = False
            if en_line_math_count > 0:
                current_tr_count = len(
                    self._INLINE_MATH_PAT.findall(fixed_line)
                )
                if current_tr_count >= en_line_math_count:
                    skip_atom_split = True

            if not skip_atom_split:
                _ATOM_PAT = r"(?:\\[a-zA-Z]+|[a-zA-Z0-9+\-=<>])"
                _MERGED_ATOMS_RE = re.compile(
                    r"\$(" + _ATOM_PAT + r"(?:\s+" + _ATOM_PAT + r")+)\$"
                )

                zones = _inline_math_zones(fixed_line)

                def _split_merged_atoms(m: re.Match) -> str:
                    if (m.start(), m.end()) not in zones:
                        return m.group(0)
                    content = m.group(1)
                    # Safety: if content has braces, scripts, or subscripts,
                    # it is a real compound expression -- leave it alone.
                    if re.search(r"[{}_^]", content):
                        return m.group(0)
                    # Safety: skip if it contains \frac, \sqrt, \text, etc.
                    if re.search(
                        r"\\(?:frac|sqrt|text|mathrm|mathbf|mathbb|"
                        r"operatorname|left|right|over|under|hat|"
                        r"bar|vec|tilde|dot|ddot)",
                        content,
                    ):
                        return m.group(0)
                    # Split on whitespace into tokens
                    tokens = content.split()
                    if len(tokens) < 2:
                        return m.group(0)
                    # Every token must be a standalone atom
                    atom_re = re.compile(
                        r"^(?:\\[a-zA-Z]+|[a-zA-Z0-9+\-=<>])$"
                    )
                    if all(atom_re.match(t) for t in tokens):
                        return " ".join("$" + t + "$" for t in tokens)
                    return m.group(0)

                new_line = _MERGED_ATOMS_RE.sub(
                    _split_merged_atoms, fixed_line,
                )
                if new_line != fixed_line:
                    fixes.append(FixResult(
                        check_name="broken_inline_math",
                        line_number=line_idx,
                        old_text=fixed_line[:80],
                        new_text=new_line[:80],
                        method="merged_atom_split",
                    ))
                    fixed_line = new_line

            # Apply fix to body
            if fixed_line != original_line:
                lines[idx] = fixed_line

        if fixes:
            body = "\n".join(lines)
            logger.info(
                "Broken inline math: applied %d fixes across %d lines.",
                len(fixes), len(issue_lines),
            )
        else:
            # Log remaining unfixable issues
            for issue in issues:
                logger.warning(
                    "Broken inline math at L%d (could not auto-fix): %s",
                    issue.line_num, issue.message,
                )

        return body, fixes

    # ------------------------------------------------------------------
    # Fix: latex_encoded_turkish
    # ------------------------------------------------------------------

    # LaTeX encoding -> UTF-8 mapping
    _LATEX_TO_UTF8: List[Tuple[re.Pattern, str]] = [
        (re.compile(r"\\c\{S\}"), "\u015e"),      # S-cedilla -> S
        (re.compile(r"\\c\{s\}"), "\u015f"),      # s-cedilla -> s
        (re.compile(r"\\c\{C\}"), "\u00c7"),      # C-cedilla -> C
        (re.compile(r"\\c\{c\}"), "\u00e7"),      # c-cedilla -> c
        (re.compile(r'\\"\{O\}'), "\u00d6"),      # O-umlaut -> O
        (re.compile(r'\\"\{o\}'), "\u00f6"),      # o-umlaut -> o
        (re.compile(r'\\"\{U\}'), "\u00dc"),      # U-umlaut -> U
        (re.compile(r'\\"\{u\}'), "\u00fc"),      # u-umlaut -> u
        (re.compile(r"\{\\i\}"), "\u0131"),        # dotless i -> i
        (re.compile(r"\\i(?=[^a-zA-Z])"), "\u0131"),  # dotless i standalone
        (re.compile(r"\\u\{G\}"), "\u011e"),      # G-breve -> G
        (re.compile(r"\\u\{g\}"), "\u011f"),      # g-breve -> g
        (re.compile(r"\\\.\{I\}"), "\u0130"),      # I-dot-above -> I
    ]

    def _fix_latex_encoded_turkish(
        self,
        body: str,
        issues: List[QCIssue],
        math_spans: List[Tuple[int, int]],
    ) -> Tuple[str, List[FixResult]]:
        """Replace LaTeX-encoded Turkish chars with UTF-8 equivalents.

        Only replaces outside of math environments.
        """
        fixes: List[FixResult] = []

        for pattern, utf8_char in self._LATEX_TO_UTF8:
            body, changes = _safe_sub(pattern, utf8_char, body, math_spans)
            if changes:
                math_spans = _find_math_spans(body)  # F7: bayat span koruma
            for old, new, pos in changes:
                line_num = body[:pos].count("\n") + 1
                fixes.append(FixResult(
                    check_name="latex_encoded_turkish",
                    line_number=line_num,
                    old_text=old,
                    new_text=new,
                    method="utf8_replace",
                ))

        if fixes:
            logger.info(
                "LaTeX-to-UTF8: replaced %d encoded Turkish chars.",
                len(fixes),
            )

        return body, fixes

    # ------------------------------------------------------------------
    # Fix: foreign_language_leakage
    # ------------------------------------------------------------------

    # Known foreign word patterns that leak into Turkish translations.
    # Each tuple: (regex_pattern, replacement, description)
    _FOREIGN_LEAKAGE_FIXES: List[Tuple[re.Pattern, str, str]] = [
        # Portuguese/Spanish "como" before Turkish text
        (re.compile(r"\bcomo\s+g[oö]sterildi[gğ]i\s+gibi\b"),
         "g\u00f6sterildi\u011fi gibi",
         "como gosterildigi gibi -> gosterildigi gibi"),
        (re.compile(r"\bcomo\s+g[oö]r[uü]ld[uü][gğ][uü]\s+gibi\b"),
         "g\u00f6r\u00fcld\u00fc\u011f\u00fc gibi",
         "como goruldugu gibi -> goruldugu gibi"),
        # Standalone "como" surrounded by Turkish text
        (re.compile(r"\bcomo\b"), "", "standalone como removal"),
        # CJK character artifacts from HY-MT 7B training data (Chinese/Japanese/Korean)
        (re.compile(
            r"[\u4e00-\u9fff\u3000-\u303f\u3040-\u309f"
            r"\u30a0-\u30ff\uac00-\ud7af\uff00-\uffef]+"
        ), "", "CJK character artifact removal"),
    ]

    def _fix_foreign_language_leakage(
        self,
        body: str,
        issues: List[QCIssue],
        math_spans: List[Tuple[int, int]],
    ) -> Tuple[str, List[FixResult]]:
        """Remove foreign language words that leaked from MT hallucination.

        Currently handles Portuguese/Spanish "como" which is a known
        HY-MT 7B hallucination pattern.  Only fixes lines that the QC
        engine has flagged (issue.line_num).

        Safety:
            - Only operates on flagged lines.
            - Skips math environments.
            - Uses word boundaries to avoid partial matches.
        """
        fixes: List[FixResult] = []

        if not issues:
            return body, fixes

        # Collect flagged line numbers
        issue_lines = set()
        for issue in issues:
            # The QC engine stores affected lines in the context field
            if issue.context:
                import ast
                try:
                    ctx = issue.context
                    # context format: "lines: [136, 168]"
                    if "lines:" in ctx:
                        lines_str = ctx.split("lines:")[1].strip()
                        line_list = ast.literal_eval(lines_str)
                        issue_lines.update(line_list)
                except (ValueError, SyntaxError):
                    pass
            issue_lines.add(issue.line_num)

        lines = body.split("\n")

        for line_idx in sorted(issue_lines):
            if line_idx < 1 or line_idx > len(lines):
                continue
            idx = line_idx - 1
            original_line = lines[idx]
            fixed_line = original_line

            for pattern, replacement, desc in self._FOREIGN_LEAKAGE_FIXES:
                new_line = pattern.sub(replacement, fixed_line)
                if new_line != fixed_line:
                    # Clean up double spaces from removal
                    new_line = re.sub(r"  +", " ", new_line)
                    fixes.append(FixResult(
                        check_name="foreign_language_leakage",
                        line_number=line_idx,
                        old_text=fixed_line.strip()[:80],
                        new_text=new_line.strip()[:80],
                        method=desc,
                    ))
                    fixed_line = new_line
                    break  # One fix per line is enough

            if fixed_line != original_line:
                lines[idx] = fixed_line

        if fixes:
            body = "\n".join(lines)
            logger.info(
                "Foreign language leakage: removed %d foreign words.",
                len(fixes),
            )

        return body, fixes

    # ------------------------------------------------------------------
    # Fix: first_person_academic
    # ------------------------------------------------------------------

    # Turkish first-person -> impersonal/passive replacements for
    # academic text.  Each tuple: (pattern, replacement).
    _FIRST_PERSON_ACADEMIC_FIXES: List[Tuple[re.Pattern, str]] = [
        (re.compile(r"\banl[iı]yorum\b", re.IGNORECASE),
         "anla\u015f\u0131lmaktad\u0131r"),
        (re.compile(r"\bg[oö]r[uü]yorum\b", re.IGNORECASE),
         "g\u00f6r\u00fclmektedir"),
        (re.compile(r"\bbiliyorum\b", re.IGNORECASE),
         "bilinmektedir"),
        (re.compile(r"\bd[uü][sş][uü]n[uü]yorum\b", re.IGNORECASE),
         "de\u011ferlendirilmektedir"),
        (re.compile(r"\byap[iı]yorum\b", re.IGNORECASE),
         "yap\u0131lmaktad\u0131r"),
        (re.compile(r"\bbuluyorum\b", re.IGNORECASE),
         "bulunmaktad\u0131r"),
        (re.compile(r"\bs[oö]yleyebilirim\b", re.IGNORECASE),
         "s\u00f6ylenebilir"),
        (re.compile(r"\bdiyebilirim\b", re.IGNORECASE),
         "denilebilir"),
        (re.compile(r"\byap[iı]yordun?\b", re.IGNORECASE),
         "yap\u0131lmaktayd\u0131"),
        (re.compile(r"\bsan[iı]yorum\b", re.IGNORECASE),
         "de\u011ferlendirilmektedir"),
        (re.compile(r"\bbelirtmeliyim\b", re.IGNORECASE),
         "belirtilmelidir"),
    ]

    def _fix_first_person_academic(
        self,
        body: str,
        issues: List[QCIssue],
        math_spans: List[Tuple[int, int]],
    ) -> Tuple[str, List[FixResult]]:
        """Replace first-person singular verb forms with impersonal/passive.

        Academic Turkish text must not use first-person singular.
        This fix converts common first-person forms to their passive
        equivalents (e.g. "anliyorum" -> "anlasilmaktadir").

        Safety:
            - Only operates on QC-flagged lines.
            - Skips math environments.
            - Uses word boundaries for precise matching.
        """
        fixes: List[FixResult] = []

        if not issues:
            return body, fixes

        # Collect flagged line numbers
        issue_lines = set()
        for issue in issues:
            if issue.context:
                import ast
                try:
                    ctx = issue.context
                    if "lines:" in ctx:
                        lines_str = ctx.split("lines:")[1].strip()
                        line_list = ast.literal_eval(lines_str)
                        issue_lines.update(line_list)
                except (ValueError, SyntaxError):
                    pass
            issue_lines.add(issue.line_num)

        lines = body.split("\n")

        for line_idx in sorted(issue_lines):
            if line_idx < 1 or line_idx > len(lines):
                continue
            idx = line_idx - 1
            original_line = lines[idx]
            fixed_line = original_line

            for pattern, replacement in self._FIRST_PERSON_ACADEMIC_FIXES:
                new_line = pattern.sub(replacement, fixed_line)
                if new_line != fixed_line:
                    fixes.append(FixResult(
                        check_name="first_person_academic",
                        line_number=line_idx,
                        old_text=fixed_line.strip()[:80],
                        new_text=new_line.strip()[:80],
                        method="passive_replacement",
                    ))
                    fixed_line = new_line

            if fixed_line != original_line:
                lines[idx] = fixed_line

        if fixes:
            body = "\n".join(lines)
            logger.info(
                "First-person academic: replaced %d verb forms.",
                len(fixes),
            )

        return body, fixes

    # ------------------------------------------------------------------
    # Fix: antonym_guard
    # ------------------------------------------------------------------

    def _fix_antonym_guard(
        self,
        body: str,
        issues: List[QCIssue],
        math_spans: List[Tuple[int, int]],
    ) -> Tuple[str, List[FixResult]]:
        """Replace antonym translations with correct terms.

        When the QC engine detects that a term was translated to its
        antonym (e.g., 'supersonic' -> 'subsonik'), this fix replaces
        the wrong translation with the correct one.

        The fix_suggestion field contains: "Replace 'wrong' with 'correct'"
        """
        fixes: List[FixResult] = []

        if not issues:
            return body, fixes

        lines = body.split("\n")

        for issue in issues:
            # Parse fix_suggestion: "Replace 'wrong_tr' with 'correct_tr'"
            suggestion = issue.fix_suggestion or ""
            m = re.match(
                r"Replace\s+'([^']+)'\s+with\s+'([^']+)'",
                suggestion,
            )
            if not m:
                continue

            wrong_tr = m.group(1)
            correct_tr = m.group(2)

            line_idx = issue.line_num
            if line_idx < 1 or line_idx > len(lines):
                continue

            idx = line_idx - 1
            original_line = lines[idx]

            # Case-insensitive replacement with Unicode-safe word boundaries
            # (QC-N3): without them the substring 'hata' matched inside
            # 'hatası' and produced garbage like 'onyargisı'. Only replace the
            # whole word, and preserve the leading capital (Turkish-aware).
            pattern = re.compile(
                r"(?<![A-Za-zÀ-ɏçğıöşüÇĞİÖŞÜ])"
                + re.escape(wrong_tr)
                + r"(?![A-Za-zÀ-ɏçğıöşüÇĞİÖŞÜ])",
                re.IGNORECASE,
            )
            new_line = pattern.sub(
                _make_case_preserving_repl(correct_tr), original_line, count=1,
            )

            if new_line != original_line:
                fixes.append(FixResult(
                    check_name="antonym_guard",
                    line_number=line_idx,
                    old_text=original_line.strip()[:80],
                    new_text=new_line.strip()[:80],
                    method="antonym_replacement",
                ))
                lines[idx] = new_line

        if fixes:
            body = "\n".join(lines)
            logger.info(
                "Antonym guard: replaced %d antonym translations.",
                len(fixes),
            )

        return body, fixes

    # ------------------------------------------------------------------
    # Fix: translated_env_name
    # ------------------------------------------------------------------

    # Known wrong -> correct environment name mapping.
    # Matches the QCEngine._TRANSLATED_ENV_MAP dictionary.
    _TRANSLATED_ENV_FIX_MAP = {
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
        "gleichung": "equation",
        "abbildung": "figure",
        "ecuacion": "equation",
        "figura": "figure",
    }

    def _fix_translated_env_name(
        self,
        body: str,
        issues: List[QCIssue],
        math_spans: List[Tuple[int, int]],
    ) -> Tuple[str, List[FixResult]]:
        r"""Fix translated LaTeX environment names.

        Replaces e.g. \begin{sekil} with \begin{figure} and
        \end{sekil} with \end{figure}.

        F2 (2026-07-28, A-9 eşdeğeri): kullanıcının \newenvironment/
        \renewenvironment/\newtheorem ile tanımladığı adlara DOKUNULMAZ —
        sanitizer'daki guard'ın buradaki eşi yoktu; tanım-kullanım
        tutarlılığı bozulup derleme kırılıyordu (CEVIRI_TALIMATI.md
        170-171 sözleşmesi).
        """
        fixes: List[FixResult] = []

        defined_envs = (
            collect_defined_env_names(body) | self.user_defined_envs
        )

        for wrong_name, correct_name in self._TRANSLATED_ENV_FIX_MAP.items():
            if wrong_name in defined_envs:
                logger.debug(
                    "Env '%s' tanımlı (\\newenvironment/\\newtheorem) "
                    "— yeniden adlandırma atlandı.", wrong_name,
                )
                continue
            # Match both \begin{wrong} and \end{wrong}, with optional star
            pattern = re.compile(
                r"(\\(?:begin|end))\{"
                + re.escape(wrong_name)
                + r"(\*?)\}"
            )

            def _replacer(m: re.Match, correct=correct_name) -> str:
                return f"{m.group(1)}{{{correct}{m.group(2)}}}"

            new_body = pattern.sub(_replacer, body)
            if new_body != body:
                # Count how many replacements
                count = len(pattern.findall(body))
                for m in pattern.finditer(body):
                    line_num = body[:m.start()].count("\n") + 1
                    fixes.append(FixResult(
                        check_name="translated_env_name",
                        line_number=line_num,
                        old_text=m.group(0),
                        new_text=_replacer(m),
                        method="env_name_replace",
                    ))
                body = new_body

        if fixes:
            logger.info(
                "Translated env names: fixed %d environment name(s).",
                len(fixes),
            )

        return body, fixes

    # ------------------------------------------------------------------
    # Fix: broken_command_braces
    # ------------------------------------------------------------------

    # Commands that must be followed by {
    _BRACE_FIX_CMDS = [
        "textbf", "textit", "emph", "underline",
        "text",  # \text{} inside math
        "footnote", "mbox", "fbox",
    ]
    # Uzunluk yazmacı bağlamı: çarpan ('0.7\textheight'), atama ('=\fboxsep'),
    # uzunluk komutunun ilk argümanı ya da \dimexpr/\the (bkz.
    # _fix_broken_command_braces).
    _LENGTH_CONTEXT_RE = re.compile(
        r"(?:\\(?:setlength|addtolength|newlength|settowidth|settoheight|"
        r"settodepth)\s*\{\s*|\\(?:dimexpr|glueexpr|the)\s*|[\d=<>]\s*)$"
    )

    def _fix_broken_command_braces(
        self,
        body: str,
        issues: List[QCIssue],
        math_spans: List[Tuple[int, int]],
    ) -> Tuple[str, List[FixResult]]:
        r"""Fix commands with missing braces.

        Converts e.g. \textbfSpektrum to \textbf{Spektrum}.
        Uses a heuristic: after the command name, captures text until
        the next space, LaTeX command, or end of line.
        """
        fixes: List[FixResult] = []

        for cmd in self._BRACE_FIX_CMDS:
            # Match \cmd followed directly by a word (no brace)
            # Captures the word (letters, digits, accented chars) until
            # whitespace or another command
            # F1 (2026-07-28) + R2 (2026-07-29): QC _BROKEN_CMD_RE ile eş
            # kural — harf devamı toptan dışlanmaz, eşleşme sonrası
            # KNOWN_LONGER_COMMANDS kontrolü yapılır (\footnotesize meşru
            # kalır, \textbfdeger yakalanır).
            pattern = re.compile(
                r"(\\" + re.escape(cmd) + r")"
                r"(?![{*\[\\])"  # not followed by { * [ or \
                r"([A-Za-z\u00C0-\u024F\d]+"  # capture the word
                r"(?:\s+[A-Za-z\u00C0-\u024F\d]+)*?"  # optionally more words
                r")"
                r"(?=\s|\\|$|[.,;:!?)}\]])"  # lookahead: word boundary
            )

            new_body = body
            offset = 0
            for m in pattern.finditer(body):
                pos = m.start()
                if _is_inside_math(pos, math_spans):
                    continue

                old_text = m.group(0)
                cmd_part = m.group(1)
                content = m.group(2)

                # R2: devam eden ASCII harflerle oluşan tam ad bilinen bir
                # komutsa (\footnotesize, \captionsetup) dokunma.
                tail_m = re.match(r"[A-Za-z]+", content)
                if tail_m and (cmd + tail_m.group(0)) in KNOWN_LONGER_COMMANDS:
                    continue
                # Kalite 2026-09-28 (kulvar S): liste dışı uzun ad da korunur.
                # '\text' + ASCII harf TeX'te TEK denetim sözcüğüdür ve
                # '\text' önekli ~100 gerçek komut var (\textheight,
                # \textfraction, \textfloatsep, textcomp simgeleri); kırık
                # '\text{..}' matematikte olur, bu onarım matematiği atlar.
                # Uzunluk bağlamındaki ('0.7\textheight', '=\fboxsep',
                # '\setlength{\footnotesep}') ASCII ad da bir yazmaçtır.
                if tail_m and tail_m.group(0) == re.match(
                        r"[A-Za-z\u00C0-\u024F]*", content).group(0) and (
                        cmd == "text"
                        or self._LENGTH_CONTEXT_RE.search(
                            body[max(0, pos - 40):pos])):
                    continue

                new_text = f"{cmd_part}{{{content}}}"

                line_num = body[:pos].count("\n") + 1
                fixes.append(FixResult(
                    check_name="broken_command_braces",
                    line_number=line_num,
                    old_text=old_text,
                    new_text=new_text,
                    method="add_braces",
                ))

                # Apply replacement with offset tracking
                start = m.start() + offset
                end = m.end() + offset
                new_body = new_body[:start] + new_text + new_body[end:]
                offset += len(new_text) - len(old_text)

            if new_body != body:
                body = new_body
                # Recompute math spans after modifications
                math_spans = _find_math_spans(body)

        if fixes:
            logger.info(
                "Broken command braces: fixed %d command(s).",
                len(fixes),
            )

        return body, fixes

    # ------------------------------------------------------------------
    # Fix: stash_placeholder_orphan
    # ------------------------------------------------------------------

    # Generic stash placeholder: <<TYPE_N>> (excluding LATEX which is
    # handled by _fix_placeholder_orphan)
    _STASH_ORPHAN_RE = re.compile(r"<<([A-Z]+)_(\d+)>>")

    # Guillemet format placeholder: MT bazen <<LATEX_127>> -> «LATEX₁₂₇ >> gibi
    # bozuyor. Unicode subscript digits: U+2080..U+2089
    # Pattern: «TYPE + subscript_digits + optional_space + >>
    _GUILLEMET_PLACEHOLDER_RE = re.compile(
        r"\u00ab([A-Z]+)"                     # « + TYPE
        r"[\u2080-\u2089]+"                   # subscript digits (1+)
        r"\s*>{1,2}"                          # optional space + > or >>
    )

    def _fix_stash_placeholder_orphan(
        self,
        body: str,
        issues: List[QCIssue],
        math_spans: List[Tuple[int, int]],
    ) -> Tuple[str, List[FixResult]]:
        """Remove orphaned stash placeholders (<<REF_N>>, <<TERM_N>>, etc.).

        These placeholders were supposed to be restored by _restore_stash()
        but weren't.  Since the original content is lost (the stash dict
        is no longer available at QC/autofix time), the safest action is
        to remove them entirely rather than leave broken markers in the
        PDF output.
        """
        fixes: List[FixResult] = []
        skip_types = {"LATEX"}  # handled by existing placeholder_orphan fix

        new_body = body
        offset = 0

        for m in self._STASH_ORPHAN_RE.finditer(body):
            ptype = m.group(1)
            if ptype in skip_types:
                continue

            pos = m.start()
            if _is_inside_math(pos, math_spans):
                continue

            old_text = m.group(0)
            new_text = ""  # delete the orphan

            line_num = body[:pos].count("\n") + 1
            fixes.append(FixResult(
                check_name="stash_placeholder_orphan",
                line_number=line_num,
                old_text=old_text,
                new_text=new_text,
                method="regex",
            ))

            start = m.start() + offset
            end = m.end() + offset
            new_body = new_body[:start] + new_text + new_body[end:]
            offset += len(new_text) - len(old_text)

        if new_body != body:
            body = new_body
            # Clean up double spaces left by removal.
            # F5 (2026-07-28): verbatim stash'i olmadan tum govdeye
            # uygulaniyordu -- kod bloklarindaki girinti tek bosluga inip
            # Python ornekleri gecersiz hale geliyordu.
            body = _cleanup_outside_verbatim(
                body, lambda b: re.sub(r"  +", " ", b),
            )

        # --- Guillemet format cleanup ---
        # MT bazen «LATEX₁₂₇ >> gibi bozuk placeholder uretir
        guillemet_body = body
        g_offset = 0
        for gm in self._GUILLEMET_PLACEHOLDER_RE.finditer(body):
            g_pos = gm.start()
            if _is_inside_math(g_pos, math_spans):
                continue

            g_old = gm.group(0)
            g_new = ""  # sil

            g_line = body[:g_pos].count("\n") + 1
            fixes.append(FixResult(
                check_name="stash_placeholder_orphan",
                line_number=g_line,
                old_text=g_old,
                new_text=g_new,
                method="guillemet_regex",
            ))

            g_start = gm.start() + g_offset
            g_end = gm.end() + g_offset
            guillemet_body = guillemet_body[:g_start] + g_new + guillemet_body[g_end:]
            g_offset += len(g_new) - len(g_old)

        if guillemet_body != body:
            body = guillemet_body
            body = _cleanup_outside_verbatim(
                body, lambda b: re.sub(r"  +", " ", b),  # F5
            )

        if fixes:
            logger.info(
                "Stash placeholder orphan: removed %d orphan(s).",
                len(fixes),
            )

        return body, fixes

    # ------------------------------------------------------------------
    # Fix: word_concatenation
    # ------------------------------------------------------------------

    # Same pattern as QC engine check 27: Turkish lowercase + uppercase
    # + lowercase indicating a lost space.
    _WORD_CONCAT_RE = re.compile(
        r"([a-z\u00e7\u011f\u0131\u00f6\u015f\u00fc])"    # group 1: before
        r"([A-Z\u00c7\u011e\u0130\u00d6\u015e\u00dc]"      # group 2: upper + rest
        r"[a-z\u00e7\u011f\u0131\u00f6\u015f\u00fc])"
    )

    # Pattern B: paren/bracket followed by uppercase letter without space.
    # e.g. ").Böyle" -> "). Böyle", ")Bu" -> ") Bu"
    _PAREN_UPPER_RE = re.compile(
        r"([)\].])"                                          # group 1: closer/period
        r"([A-ZÇĞİÖŞÜ][a-zçğıöşüA-ZÇĞİÖŞÜ])"             # group 2: uppercase + next char
    )

    # Pattern C: period/comma + uppercase without space (sentence boundary).
    # e.g. "sonuçtur.Bu" -> "sonuçtur. Bu"
    # Must NOT match inside abbreviations like "Dr.Ali" which are rare in TR.
    # Must NOT match inside decimals like "3.14"
    _PUNCT_UPPER_RE = re.compile(
        r"([a-zçğıöşü][.,;:!?])"                            # group 1: letter + punct
        r"([A-ZÇĞİÖŞÜ][a-zçğıöşü])"                        # group 2: uppercase start
    )

    # Same whitelist as QC engine
    _CAMELCASE_WHITELIST = {
        "LaTeX", "BibTeX", "XeLaTeX", "LuaTeX", "pdfTeX", "ConTeXt",
        "XeTeX", "BibLaTeX",
        "iPhone", "iPad", "iPod", "iTunes", "iMac", "macOS", "iOS",
        "GitHub", "JavaScript", "TypeScript", "CamelCase",
        "YouTube", "LinkedIn", "TensorFlow", "PyTorch", "NumPy",
        "SciPy", "DataFrame", "PowerPoint", "WordPress", "PostgreSQL",
        "MongoDB", "GraphQL", "DevOps", "IntelliJ", "PhpStorm",
        "WebSocket", "OpenAI", "ChatGPT", "AutoML", "ResNet", "VGGNet",
        "ImageNet", "arXiv", "DenseNet", "EfficientNet", "MobileNet",
    }

    _LATEX_CMD_SKIP_RE = re.compile(
        r"\\[a-zA-Z]+\s*(?:\*?\{[^}]*\}|\*?\[[^\]]*\])*"
    )

    def _fix_word_concatenation(
        self,
        body: str,
        issues: List[QCIssue],
        math_spans: List[Tuple[int, int]],
    ) -> Tuple[str, List[FixResult]]:
        """Insert spaces where Turkish words were concatenated.

        Converts e.g. "fonksiyonlarıHer" -> "fonksiyonları Her"
        by inserting a space at the lowercase-to-uppercase boundary.
        Skips LaTeX commands, math spans, and known camelCase words.
        """
        fixes: List[FixResult] = []
        lines = body.split("\n")
        new_lines = []

        for line_idx, line in enumerate(lines):
            stripped = line.strip()
            # Skip empty / comment / env lines
            if (not stripped or stripped.startswith("%") or
                    stripped.startswith("\\begin") or
                    stripped.startswith("\\end")):
                new_lines.append(line)
                continue

            # Build cmd position set for this line
            cmd_positions: set = set()
            for cm in self._LATEX_CMD_SKIP_RE.finditer(line):
                for p in range(cm.start(), cm.end()):
                    cmd_positions.add(p)

            # Also skip inline math
            inline_math_re = re.compile(
                r"(?<![\$\\])\$(?!\$).+?(?<![\$\\])\$(?!\$)"
            )
            for cm in inline_math_re.finditer(line):
                for p in range(cm.start(), cm.end()):
                    cmd_positions.add(p)

            new_line = line
            line_offset = 0

            for m in self._WORD_CONCAT_RE.finditer(line):
                ms = m.start()
                if ms in cmd_positions or (ms + 1) in cmd_positions:
                    continue

                # Check whitelist: extract full word
                left = ms
                while left > 0 and (line[left - 1].isalnum() or line[left - 1] in "_."):
                    left -= 1
                right = m.end()
                while right < len(line) and (line[right].isalnum() or line[right] in "_."):
                    right += 1
                full_word = line[left:right]

                skip = False
                for wl in self._CAMELCASE_WHITELIST:
                    if wl in full_word:
                        skip = True
                        break
                if skip:
                    continue

                old_text = m.group(0)
                new_text = m.group(1) + " " + m.group(2)

                fixes.append(FixResult(
                    check_name="word_concatenation",
                    line_number=line_idx + 1,
                    old_text=old_text,
                    new_text=new_text,
                    method="regex",
                ))

                start = m.start() + line_offset
                end = m.end() + line_offset
                new_line = new_line[:start] + new_text + new_line[end:]
                line_offset += len(new_text) - len(old_text)

            # --- Pattern B: paren/bracket/period + uppercase ---
            # Re-scan the modified line with _PAREN_UPPER_RE
            line_offset_b = 0
            for m in self._PAREN_UPPER_RE.finditer(new_line if line_offset == 0 else new_line):
                ms = m.start()
                adj_ms = ms - line_offset_b  # adjusted for previous insertions
                if adj_ms in cmd_positions or (adj_ms + 1) in cmd_positions:
                    continue

                old_text = m.group(0)
                new_text_b = m.group(1) + " " + m.group(2)

                fixes.append(FixResult(
                    check_name="word_concatenation",
                    line_number=line_idx + 1,
                    old_text=old_text,
                    new_text=new_text_b,
                    method="regex",
                ))

                start = m.start() + line_offset_b
                end = m.end() + line_offset_b
                new_line = new_line[:start] + new_text_b + new_line[end:]
                line_offset_b += 1  # inserted 1 space

            # --- Pattern C: letter+punct + uppercase ---
            line_offset_c = 0
            for m in self._PUNCT_UPPER_RE.finditer(new_line):
                ms = m.start()
                if ms in cmd_positions or (ms + 1) in cmd_positions:
                    continue

                old_text = m.group(0)
                new_text_c = m.group(1) + " " + m.group(2)

                fixes.append(FixResult(
                    check_name="word_concatenation",
                    line_number=line_idx + 1,
                    old_text=old_text,
                    new_text=new_text_c,
                    method="regex",
                ))

                start = m.start() + line_offset_c
                end = m.end() + line_offset_c
                new_line = new_line[:start] + new_text_c + new_line[end:]
                line_offset_c += 1  # inserted 1 space

            new_lines.append(new_line)

        body = "\n".join(new_lines)

        if fixes:
            logger.info(
                "Word concatenation: inserted spaces in %d location(s).",
                len(fixes),
            )

        return body, fixes

    # ------------------------------------------------------------------
    # Fix: hallucination / repetition loops
    # ------------------------------------------------------------------

    # Regex for detecting MT repetition loops: a phrase of 5+ words
    # repeated 3+ times consecutively.
    _REPETITION_FIX_RE = re.compile(
        r"(\b\w+(?:\s+\w+){4,}?)(?:\s+\1){2,}",
        re.IGNORECASE,
    )

    def _fix_segment_hallucination(
        self,
        body: str,
        issues: List[QCIssue],
        math_spans: List[Tuple[int, int]],
    ) -> Tuple[str, List[FixResult]]:
        """Fix MT repetition loops by truncating to first occurrence.

        When the MT model enters a repetition loop (the same 5+ word
        phrase repeated 3+ times), this handler keeps only the first
        occurrence and removes the duplicates.

        Only fixes auto_fixable issues with check_name 'hallucination'
        that contain 'repetition' in their message.
        """
        fixes: List[FixResult] = []

        # Process line by line to stay safe
        lines = body.split("\n")
        new_lines: List[str] = []

        # math_spans GOVDE-GLOBAL offset'lidir; satir-ici m.start() ile
        # karsilastirmak icin satirin govdedeki baslangici takip edilir.
        # Eski kod satir-yerel offset kullaniyordu: verbatim/lstlisting ici
        # budaniyor, gercek tekrar donguleri atlaniyordu (#34/#83, eski #52).
        line_start = 0

        for line_idx, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or stripped.startswith("%"):
                new_lines.append(line)
                line_start += len(line) + 1
                continue
            if stripped.startswith("\\begin") or stripped.startswith("\\end"):
                new_lines.append(line)
                line_start += len(line) + 1
                continue

            # F38 (2026-07-29): QC 29b ile AYNI geri-referanslı regex burada
            # gövdenin HER satırında finditer ile dönüyor. O(W) ön-eleme
            # eşleşmesi imkânsız satırları eler (üst-küme olduğu için
            # düzeltme kümesi değişmez).
            if not may_have_repetition_loop(stripped):
                new_lines.append(line)
                line_start += len(line) + 1
                continue

            new_line = line
            offset = 0
            for m in self._REPETITION_FIX_RE.finditer(line):
                # Skip if inside math (global offset ile)
                if _is_inside_math(line_start + m.start(), math_spans):
                    continue

                old_text = m.group(0)
                # Keep only the first occurrence of the repeated phrase
                first_occurrence = m.group(1)

                fixes.append(FixResult(
                    check_name="hallucination",
                    line_number=line_idx + 1,
                    old_text=old_text[:80] + ("..." if len(old_text) > 80 else ""),
                    new_text=first_occurrence[:80],
                    method="repetition_truncate",
                ))

                start = m.start() + offset
                end = m.end() + offset
                new_line = new_line[:start] + first_occurrence + new_line[end:]
                offset += len(first_occurrence) - len(old_text)

            new_lines.append(new_line)
            line_start += len(line) + 1

        body = "\n".join(new_lines)

        if fixes:
            logger.info(
                "Hallucination fix: truncated %d repetition loop(s).",
                len(fixes),
            )

        return body, fixes

    # ------------------------------------------------------------------
    # Fix: ascii_degradation (Türkçe karakter kuralı)
    # ------------------------------------------------------------------

    def _fix_ascii_degradation(
        self,
        body: str,
        issues: List[QCIssue],
        math_spans: List[Tuple[int, int]],
    ) -> Tuple[str, List[FixResult]]:
        """Replace ASCII-degraded Turkish words with correct Unicode forms.

        Uses the QCEngine's _ASCII_DEGRADATION_MAP to find and replace
        words where Turkish special characters were lost (e.g. 'icin' -> 'icin').
        Skips matches inside math environments.
        """
        from latex_pipeline.qc_engine import QCEngine

        fixes: List[FixResult] = []
        degradation_map = QCEngine._ASCII_DEGRADATION_MAP
        degradation_re = QCEngine._ASCII_DEGRADATION_RE

        _TR_SPECIAL = set("\u00e7\u011f\u0131\u00f6\u015f\u00fc"
                          "\u00c7\u011e\u0130\u00d6\u015e\u00dc")

        # Komut argumanlari (URL, dosya adi, etiket) TURKCELESTIRILMEZ:
        # '\url{...giris...}' -> '...giri\u015f...' olu link, includegraphics
        # diskte olmayan dosya, \label/\ref kirilmasi uretiyordu (#35).
        _CMD_ARG_RE = re.compile(
            r"\\(?:url|href|includegraphics|label|ref|eqref|pageref|cref|"
            r"autoref|cite|citep|citet|input|include|bibliography)"
            r"(?:\[[^\]]*\])?\s*\{[^{}]*\}"
            r"(?:\{[^{}]*\})?"  # \href'in ikinci argumani
        )
        _arg_stash: List[str] = []

        def _stash_arg(m: re.Match) -> str:
            _arg_stash.append(m.group(0))
            return f"\x00AFARG{len(_arg_stash) - 1}\x00"

        body = _CMD_ARG_RE.sub(_stash_arg, body)

        def _replacer(m: re.Match) -> str:
            word = m.group(0)
            lower = word.lower()
            correct = degradation_map.get(lower)
            if correct is None:
                return word
            # If the matched text already has Turkish special chars, skip
            if any(ch in _TR_SPECIAL for ch in word):
                return word
            # Preserve original case with Turkish İ/ı rules (AF-03):
            # Python's ASCII upper() turns 'için' into 'IÇIN' (should be
            # 'İÇİN') and capitalizes 'i' as 'I' (should be 'İ').
            if word.isupper():
                # ALL CAPS
                correct = _tr_upper(correct)
            elif word[0].isupper():
                correct = _tr_upper_first(correct)
            return correct

        # math_spans orijinal govdeye gore hesaplanmisti; stash placeholder
        # ikamesi offsetleri kaydirdigi icin span'ler stash'li govde uzerinde
        # yeniden hesaplanir (placeholder'lar newline icermez, satir
        # numaralari degismez).
        local_spans = _find_math_spans(body)
        body, changes = _safe_sub(
            degradation_re, _replacer, body, local_spans,
        )

        for old, new, pos in changes:
            line_num = body[:pos].count("\n") + 1
            fixes.append(FixResult(
                check_name="ascii_degradation",
                line_number=line_num,
                old_text=old,
                new_text=new,
                method="ascii_degradation_fix",
            ))

        # Komut argumanlarini geri yukle
        for i, arg in enumerate(_arg_stash):
            body = body.replace(f"\x00AFARG{i}\x00", arg)

        if fixes:
            logger.info(
                "ASCII degradation fix: replaced %d word(s).",
                len(fixes),
            )

        return body, fixes

    # ------------------------------------------------------------------
    # Fix: glossary_compliance (session 9, 2026-03-05)
    # ------------------------------------------------------------------

    def _fix_glossary_compliance(
        self,
        body: str,
        issues: List[QCIssue],
        math_spans: List[Tuple[int, int]],
    ) -> Tuple[str, List[FixResult]]:
        """Fix glossary compliance issues by replacing wrong translations.

        Each QCIssue for glossary_compliance contains metadata with:
        - ``expected``: the correct Turkish translation from the glossary
        - ``found``: the wrong translation found in the text
        - ``term``: the original English term (optional)

        Strategy:
        1. Extract (found, expected) from each issue's metadata/context.
        2. Replace ``found`` with ``expected`` using word-boundary matching.
        3. Skip matches inside math environments AND LaTeX environment
           delimiters (``\\begin{figure}``) and command arguments
           (``\\includegraphics{...}``). Without this, terms like
           "Figure"->"Şekil" corrupt ``\\begin{figure}`` -> ``\\begin{Şekil}``
           and image filenames like ``_page_X_Figure_Y.png``.
        4. If metadata is missing or unparseable, log and skip.
        """
        fixes: List[FixResult] = []

        # KRİTİK FIX (2026-05-09): Stash environment delimiters, ref/label
        # commands, includegraphics paths and standalone LaTeX commands so
        # glossary entries like "Figure"->"Şekil" don't corrupt them.
        from latex_pipeline.glossary import _protect_math_zones, _restore_math_zones
        body, _latex_stash = _protect_math_zones(body)
        # Math spans now point to the ORIGINAL body offsets which are no
        # longer valid after stashing -- but stashed regions are placeholder
        # tokens (__GLOSSSTASH_N__) which won't match any glossary term, so
        # math_spans is effectively superseded by the stash.

        for issue in issues:
            # Extract wrong and correct terms from issue metadata
            found = None
            expected = None

            # Try to get from metadata dict (preferred)
            if hasattr(issue, "metadata") and isinstance(issue.metadata, dict):
                found = issue.metadata.get("found") or issue.metadata.get("wrong")
                expected = issue.metadata.get("expected") or issue.metadata.get("correct")

            # Fallback: try to parse from issue.context string
            # Common formats: "Expected 'X' but found 'Y'"
            #                 "Term 'X': expected 'Y', found 'Z'"
            if not found or not expected:
                ctx = getattr(issue, "context", "") or ""
                # Pattern: expected 'X' ... found 'Y'
                exp_match = re.search(r"expected\s+['\"]([^'\"]+)['\"]", ctx, re.IGNORECASE)
                found_match = re.search(r"found\s+['\"]([^'\"]+)['\"]", ctx, re.IGNORECASE)
                if exp_match and found_match:
                    expected = expected or exp_match.group(1)
                    found = found or found_match.group(1)

            # Also try issue.message
            if not found or not expected:
                msg = getattr(issue, "message", "") or ""
                exp_match = re.search(r"expected\s+['\"]([^'\"]+)['\"]", msg, re.IGNORECASE)
                found_match = re.search(r"found\s+['\"]([^'\"]+)['\"]", msg, re.IGNORECASE)
                if exp_match and found_match:
                    expected = expected or exp_match.group(1)
                    found = found or found_match.group(1)

            # Try fix_suggestion: "Replace 'X' with 'Y'"
            if not found or not expected:
                suggestion = getattr(issue, "fix_suggestion", "") or ""
                fix_match = re.match(
                    r"Replace\s+['\"]([^'\"]+)['\"]\s+with\s+['\"]([^'\"]+)['\"]",
                    suggestion, re.IGNORECASE,
                )
                if fix_match:
                    found = found or fix_match.group(1)
                    expected = expected or fix_match.group(2)

            # Try message format: "Glossary ihlali: 'X' cevirilmemis, 'Y' olmali"
            if not found or not expected:
                msg = getattr(issue, "message", "") or ""
                msg_match = re.match(
                    r"Glossary ihlali:\s+['\"]([^'\"]+)['\"]\s+cevirilmemis,\s+"
                    r"['\"]([^'\"]+)['\"]\s+olmali",
                    msg, re.IGNORECASE,
                )
                if msg_match:
                    found = found or msg_match.group(1)
                    expected = expected or msg_match.group(2)

            if not found or not expected:
                logger.debug(
                    "glossary_compliance fix: cannot extract terms from issue at L%d. "
                    "Skipping.",
                    issue.line_num,
                )
                continue

            if found.strip() == expected.strip():
                continue

            # Build word-boundary pattern for the wrong term
            # Use lookaround for Unicode-safe word boundaries
            # Case-insensitive to catch "Neural Network" as well as "neural network"
            escaped_found = re.escape(found.strip())
            pattern = re.compile(
                r"(?<![A-Za-z\u00C0-\u024F])"
                + escaped_found
                + r"(?![A-Za-z\u00C0-\u024F])",
                re.IGNORECASE,
            )

            # Build a replacer that preserves case of the first character
            # (Turkish-aware; shared with the wrong-translation passes).
            expected_clean = expected.strip()
            _case_preserving_repl = _make_case_preserving_repl(expected_clean)

            # Apply replacement. math_spans is intentionally EMPTY here:
            # `body` was already stashed by _protect_math_zones above, so all
            # math/LaTeX is now placeholder tokens that can't match a glossary
            # term. The original math_spans point at pre-stash offsets and,
            # after the length shift from stashing, produce false 'inside math'
            # skips (AF-05). The stash is the real protection.
            new_body, changes = _safe_sub(
                pattern, _case_preserving_repl, body, [],
            )

            if changes:
                body = new_body
                for old, new, pos in changes:
                    line_num = body[:pos].count("\n") + 1
                    fixes.append(FixResult(
                        check_name="glossary_compliance",
                        line_number=line_num,
                        old_text=old[:60],
                        new_text=new[:60],
                        method="glossary_replace",
                    ))

        if fixes:
            logger.info(
                "Glossary compliance fix: replaced %d term(s).",
                len(fixes),
            )
        else:
            logger.debug(
                "glossary_compliance fix attempted: %d issues processed, "
                "no replacements made.",
                len(issues),
            )

        # Restore stashed LaTeX zones
        body = _restore_math_zones(body, _latex_stash)

        return body, fixes

    # ------------------------------------------------------------------
    # Fix: wrong_translation (session 10, 2026-03-05)
    # ------------------------------------------------------------------

    # Hardcoded fallback map for known MT wrong translations.
    # These are applied when QC issue metadata/fix_suggestion parsing
    # fails to extract (found, expected) pairs.
    #
    # REMOVED context-destroying entries (DATA-01/AF-02/AF-C6):
    #   'bilgisayar ağı' -> 'sinir ağı'  (computer network is a LEGIT term,
    #        distinct from neural network -- blind rewrite corrupts real text)
    #   'üst ses' -> 'ses üstü'          (context-dependent)
    #   'dikgen' -> 'ortogonal'          (a legitimate Turkish word AND it
    #        conflicted with the heading terminology, creating a
    #        producer/consumer loop that also lowercased sentence-initials)
    # Only unambiguous MT mistranslations remain.
    _WRONG_TRANSLATION_MAP: Dict[str, str] = {
        "eylem fonksiyonları": "aktivasyon fonksiyonları",
        "eylem fonksiyonu": "aktivasyon fonksiyonu",
        "çarpım işareti": "iç çarpım",
        "etkileme fonksiyonları": "aktivasyon fonksiyonları",
        "etkileme fonksiyonu": "aktivasyon fonksiyonu",
        "hipergecis parametreleri": "hiperparametreler",
        "hipergeçiş parametreleri": "hiperparametreler",
        "geri yayılma": "geri yayılım",
    }

    def _kitap_secimi_mi(self, ifade: str) -> bool:
        """KARAR 25: kitabın sözlüğü *ifade*yi bir terimin karşılığı olarak
        seçtiyse sabit "yanlış çeviri" haritası onu değiştirmez (QC'nin
        kitap sözlüğü muafiyetiyle aynı ölçüt: QCEngine._kitap_secimleri)."""
        secimler_fn = getattr(self.qc_engine, "_kitap_secimleri", None)
        if not callable(secimler_fn):
            return False
        try:
            secimler = secimler_fn()
        except Exception:
            return False
        return secimler is not None and secimler.secili_mi(ifade)

    def _fix_wrong_translation(
        self,
        body: str,
        issues: List[QCIssue],
        math_spans: List[Tuple[int, int]],
    ) -> Tuple[str, List[FixResult]]:
        """Fix known wrong translations reported by QC check 32.

        Strategy:
        1. First, delegate to _fix_glossary_compliance which parses the
           QC issue's fix_suggestion ("Replace 'X' with 'Y'" format).
        2. Then, apply the hardcoded _WRONG_TRANSLATION_MAP as a fallback
           for any remaining wrong terms that QC may not have caught or
           whose metadata was unparseable.
        """
        # Step 1: Use glossary_compliance logic for QC-reported issues
        body, fixes = self._fix_glossary_compliance(body, issues, math_spans)

        # Step 2: Hardcoded fallback map (catches residual wrong terms).
        # _fix_glossary_compliance may have changed the body length, so the
        # incoming math_spans are stale -- recompute them on the current body.
        math_spans = _find_math_spans(body)
        for wrong, correct in self._WRONG_TRANSLATION_MAP.items():
            if self._kitap_secimi_mi(wrong):
                continue
            escaped = re.escape(wrong)
            pattern = re.compile(
                r"(?<![A-Za-z\u00C0-\u024F])"
                + escaped
                + r"(?![A-Za-z\u00C0-\u024F])",
                re.IGNORECASE,
            )

            new_body, changes = _safe_sub(
                pattern, _make_case_preserving_repl(correct), body, math_spans,
            )
            if changes:
                body = new_body
                for old, new, pos in changes:
                    line_num = body[:pos].count("\n") + 1
                    fixes.append(FixResult(
                        check_name="wrong_translation",
                        line_number=line_num,
                        old_text=old[:60],
                        new_text=new[:60],
                        method="hardcoded_wrong_map",
                    ))

        if fixes:
            logger.info(
                "Wrong translation fix: replaced %d occurrence(s).",
                len(fixes),
            )

        return body, fixes

    # ------------------------------------------------------------------
    # Structural: always-on wrong-translation map (FIX 6, session 10)
    # ------------------------------------------------------------------

    def _apply_wrong_translation_map_always(
        self, body: str,
    ) -> Tuple[str, List[FixResult]]:
        """Apply _WRONG_TRANSLATION_MAP unconditionally (QC-independent).

        This is called from _apply_structural_fixes so it runs even when
        the QC engine does not report any wrong_translation issues.
        Handles cached translations and terms the QC check missed.

        NOTE: The caller (_apply_structural_fixes step 5) gates this on
        ``not self.manual_translation`` so it never rewrites authoritative
        Yol A / agent output (DATA-01/AF-02/QC-N1).
        """
        fixes: List[FixResult] = []
        math_spans = _find_math_spans(body)

        for wrong, correct in self._WRONG_TRANSLATION_MAP.items():
            if self._kitap_secimi_mi(wrong):
                continue
            escaped = re.escape(wrong)
            pattern = re.compile(
                r"(?<![A-Za-z\u00C0-\u024F])"
                + escaped
                + r"(?![A-Za-z\u00C0-\u024F])",
                re.IGNORECASE,
            )

            new_body, changes = _safe_sub(
                pattern, _make_case_preserving_repl(correct), body, math_spans,
            )
            if changes:
                body = new_body
                # Recompute math_spans after body changed
                math_spans = _find_math_spans(body)
                for old, new, pos in changes:
                    line_num = body[:pos].count("\n") + 1
                    fixes.append(FixResult(
                        check_name="wrong_translation_structural",
                        line_number=line_num,
                        old_text=old[:60],
                        new_text=new[:60],
                        method="hardcoded_wrong_map_always",
                    ))

        if fixes:
            logger.info(
                "Structural wrong-translation fix: replaced %d occurrence(s).",
                len(fixes),
            )

        return body, fixes
