"""
Terminology engine for consistent translation of technical terms.

KARAR 25 (5 Ekim 2026): sabit GENEL ALAN sözlükleri (default, cs, math, ml,
physics, science_engineering, technical ve felsefe türü sözlüğü) SİLİNDİ; her
kitap kendi sözlüğünü kaynak dilde çıkarır (``latex_pipeline.kitap_sozlugu``,
``glossary_shared.tsv``). Bu motor artık yalnız ``glossary/curated.json``
(elle doğrulanmış) ve ``glossary/do_not_translate.json``'u yükler; kamu API'si
(``GlossaryEngine`` kurucusu, ``verify_terms`` …) geriye uyumlu kaldı.
Phase 5.75 (translate_book.py) terminoloji geçişi kitap sözlüğüyle yapılır.

Loads JSON glossary files from ``glossary/`` directory and provides
two approaches for term handling:

**Direct TR Injection (preferred):**
    Pre-translation: replace EN terms directly with TR translations.
    Post-translation: verify injected terms survived MT, re-inject if lost.
    This eliminates the 50%+ placeholder loss observed with HY-MT 7B.

**Placeholder stash/restore (deprecated):**
    Pre-translation: EN term -> ``<<TERM_N>>`` placeholder.
    Post-translation: ``<<TERM_N>>`` -> TR term.
    Kept for backward compatibility only.

Design decisions:
    - Zero external dependencies (stdlib only).
    - Longest-match-first strategy: "stochastic gradient descent" is
      matched before "gradient descent" which is matched before "gradient".
    - Case-insensitive matching with optional case-sensitive override.
    - Math/code regions are already stashed by translator.py BEFORE
      this engine runs, so we never touch formulas.

Usage (preferred)::

    engine = GlossaryEngine(glossary_dir=Path("glossary"), book_type="science")
    text, term_map = engine.inject_terms(text)
    # ... MT translation happens here ...
    text = engine.verify_terms(text, term_map)

Algorithm:
    1. Load all relevant domain glossaries based on book_type
    2. Sort terms by length (longest first) for greedy matching
    3. Build a single compiled regex from all terms
    4. inject_terms(): replace EN terms with TR translations directly
    5. verify_terms(): check TR terms survived MT, re-inject if lost
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Math / LaTeX command protection (KRİTİK BUG FIX 2026-05-09)
# ---------------------------------------------------------------------------
# Phase 5.75 (verify_terms) runs as a STANDALONE pass; translator.py's
# pre-stash is not active here.  (G2c, 2026-10-05: Phase 5.75 artık
# kitap_sozlugu.KitapSozlugu.verify_terms'i koşar; o da AYNI korumayı —
# _protect_math_zones / _restore_math_zones — kullanır; AutoFixer da bu iki
# işlevi içe aktarır.)  Without protection, glossary entries like
# "alpha"->"alfa dalgası" or "quad"->"kare alan" corrupt LaTeX commands
# (\alpha -> \alpha dalgası, \quad -> \kare alan) inside math environments.
#
# This local stash mirrors sanitizer.py's mechanism but uses a distinct
# placeholder prefix to avoid collision when both stashes are active.

_MATH_ENV_NAMES = (
    r"equation\*?|align\*?|gather\*?|multline\*?|split"
    r"|flalign\*?|alignat\*?|cases|pmatrix|bmatrix|vmatrix|matrix"
    r"|smallmatrix|aligned|gathered|array"
)
# GLS-05: begin/end adları backreference (\1) ile eşleşir; iç içe ortamda
# (ör. \begin{equation} ... \begin{pmatrix} ... \end{pmatrix} ... \end{equation})
# non-greedy gövde artık ilk iç \end'de değil, DOĞRU dış \end'de kapanır.
_GLOSS_MATH_ENV_RE = re.compile(
    r"\\begin\{(" + _MATH_ENV_NAMES + r")\}.*?\\end\{\1\}",
    re.DOTALL,
)
# GLS-03: kod ortamları — içeriği ASLA çeviriye girmez. Backreference ile
# doğru begin/end eşlemesi. (verbatim / lstlisting / minted / Verbatim / alltt)
_GLOSS_VERBATIM_ENV_RE = re.compile(
    r"\\begin\{(verbatim\*?|lstlisting|minted|Verbatim|alltt)\}.*?\\end\{\1\}",
    re.DOTALL,
)
_GLOSS_DISPLAY_MATH_RE = re.compile(r"\\\[.*?\\\]", re.DOTALL)
# GLS-04: $$ ... $$ display math (inline tek-$ kalıbı bunu bilinçli dışlıyor).
_GLOSS_DOLLAR_DISPLAY_RE = re.compile(r"\$\$.*?\$\$", re.DOTALL)
_GLOSS_PAREN_MATH_RE = re.compile(r"\\\(.*?\\\)", re.DOTALL)
# GLS-F12 (2026-07-28): inline math satir sonu icerebilir (Marker/pandoc
# ciktisi uzun $...$'i satira boler). Eski [^$\n] sinifi cok satirli inline
# math'i HIC stash'lemiyordu: icerideki 'bias' gibi sozlukte olan kelimeler
# Pass 2/3'te Turkcelestirilip formulu boluyordu. Sanitizer'daki desenle
# (sanitizer.py:141) hizali: paragraf siniri (bos satir) asilmaz, yoksa
# kacak tek '$' tum paragraflari yutar.
_GLOSS_INLINE_MATH_RE = re.compile(
    r"(?<!\\)\$(?!\$)((?:(?!\n\s*\n)(?!(?<!\\)\$).)+)(?<!\\)\$(?!\$)",
    re.DOTALL,
)
# Protected commands: refs, labels, citations, file paths in graphics, env names
# GLS-F28 (2026-07-29): 'url' ve 'href' eklendi. Eskiden yalnizca komut ADI
# (_GLOSS_LATEX_CMD_RE ile) stash'leniyor, susllu icerik acikta kaliyordu;
# Pass 2/3 alternation'i URL path'inde \b sinirinda eslesip adresi
# Turkcelestiriyordu (ör. '\url{.../tokenizer}' -> '.../belirtecleyici' =
# kirik link + \url icinde Turkce karakter). Regex TEK {...} grubu yakalar:
# \url{ADRES} tamamen korunur, \href{ADRES}{gorunen metin} ise yalnizca ADRES
# argumaniyla korunur — gorunen metin cevrilebilir metindir ve serbest kalir
# (translator.py'deki href politikasiyla ayni: _translate_href_display_texts).
_GLOSS_PROTECTED_CMD_RE = re.compile(
    r"\\(?:label|ref|eqref|cite|pageref|cref|autoref|nameref|"
    r"includegraphics|input|include|bibliography|usepackage|documentclass|"
    r"href|url)"
    r"\s*(?:\[[^\]]*\])?\s*\{[^}]*\}"
)
# \begin{XYZ} and \end{XYZ} environment names must never be translated
_GLOSS_ENV_DELIM_RE = re.compile(r"\\(?:begin|end)\{[^}]+\}")
# Standalone LaTeX commands (e.g. \alpha, \quad, \frac) — protect the command
# name itself so glossary entries like "alpha" don't poison "\alpha".
_GLOSS_LATEX_CMD_RE = re.compile(r"\\[a-zA-Z]+\*?")

# D3 / GLS-06: Başlık ve caption komutları. Glossary post-pass başlığa HİÇ
# dokunmamalı — başlık çevirisi kendi pipeline'ında (Title Case korumalı)
# yapılır. Bu regex sadece komut+açılış süslüsünü bulur; kapanış süslüsü
# _stash_headings içinde brace-derinliği ile eşlenir (iç içe süslü desteği).
_GLOSS_HEADING_CMD_RE = re.compile(
    r"\\(?:section|subsection|subsubsection|chapter|part|paragraph"
    r"|subparagraph|caption)\*?\s*(?:\[[^\]]*\])?\s*\{"
)


def _stash_headings(text: str, stash: List[str]) -> str:
    """Stash ``\\section{...}`` / ``\\caption{...}`` vb. başlıkları (komut +
    tüm süslü içeriği) ``__GLOSSSTASH_N__`` ile değiştirir.

    Brace-derinliği ile eşleme yapıldığından iç içe süslüler (``\\section{A
    \\emph{B}}``) tek parça korunur. Diğer stash regex'lerinden ÖNCE
    çalıştırılmalı ki başlık içindeki her şey (matematik dahil) tek bütün
    olarak korunsun.
    """
    out: List[str] = []
    pos = 0
    n = len(text)
    for m in _GLOSS_HEADING_CMD_RE.finditer(text):
        if m.start() < pos:
            continue  # önceki başlık aralığının içinde kalan eşleşme
        depth = 1
        i = m.end()
        while i < n and depth > 0:
            c = text[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            i += 1
        out.append(text[pos:m.start()])
        idx = len(stash)
        stash.append(text[m.start():i])
        out.append(f"__GLOSSSTASH_{idx}__")
        pos = i
    out.append(text[pos:])
    return "".join(out)


def _protect_math_zones(text: str) -> Tuple[str, List[str]]:
    """Replace headings, code/math environments, display/inline math, refs and
    LaTeX commands with ``__GLOSSSTASH_N__`` placeholders.

    Returns ``(modified_text, stash)``. Restore with ``_restore_math_zones``.
    """
    stash: List[str] = []

    def _repl(m: "re.Match[str]") -> str:
        idx = len(stash)
        stash.append(m.group(0))
        return f"__GLOSSSTASH_{idx}__"

    # Order: başlıklar/kod ilk (bütün olarak) > env > display > paren > inline
    #        > refs > env-delim > cmd
    text = _stash_headings(text, stash)               # D3, GLS-06
    text = _GLOSS_VERBATIM_ENV_RE.sub(_repl, text)    # GLS-03
    text = _GLOSS_MATH_ENV_RE.sub(_repl, text)        # GLS-05 (backreference)
    text = _GLOSS_DOLLAR_DISPLAY_RE.sub(_repl, text)  # GLS-04
    text = _GLOSS_DISPLAY_MATH_RE.sub(_repl, text)
    text = _GLOSS_PAREN_MATH_RE.sub(_repl, text)
    text = _GLOSS_INLINE_MATH_RE.sub(_repl, text)
    text = _GLOSS_PROTECTED_CMD_RE.sub(_repl, text)
    text = _GLOSS_ENV_DELIM_RE.sub(_repl, text)
    text = _GLOSS_LATEX_CMD_RE.sub(_repl, text)
    return text, stash


# ---------------------------------------------------------------------------
# Case-preserving replacement + Pass 3 filters (GLS-02, GLS-D1)
# ---------------------------------------------------------------------------

# Pass 3 EN-echo taramasında atlanan yaygın İngilizce kelimeler.
_PASS3_COMMON_WORDS: Set[str] = {
    "also", "this", "that", "with", "from", "have", "been", "will",
    "they", "their", "more", "very", "much", "each", "some",
}


def _match_case(matched: str, replacement: str) -> str:
    """Eşleşmenin harf durumunu (case) yerine konan metne taşır.

    - ALL-CAPS eşleşme -> replacement büyük harfe.
    - Baş harfi büyük eşleşme -> replacement baş harfi büyük (cümle başı).
    - Küçük harf eşleşme -> replacement baş harfi küçük (sözlükteki
      'Oyun' gibi büyük harfli TR'nin cümle ortasına basılmasını önler).

    Böylece IGNORECASE eşleşip sabit-case string basma bug'ı (GLS-02)
    her iki yönde de düzelir.
    """
    if not matched or not replacement:
        return replacement
    if len(matched) > 1 and matched.isupper():
        return replacement.upper()
    if matched[0].isupper():
        return replacement[0].upper() + replacement[1:]
    return replacement[0].lower() + replacement[1:]


# TR-kalite kapısı: bozuk kodlama (mojibake / replacement char) sinyalleri.
# UTF-8 metnin latin-1 olarak yanlış çözülmesiyle oluşan klasik ikili
# kalıplar + U+FFFD. Yükleme aşamasında yalnızca UYARI verir (hard reject yok).
_TR_MOJIBAKE_MARKERS: Tuple[str, ...] = (
    "�", "Ã¼", "Ã§", "ÅŸ", "Ä±", "Ã¶", "Ã‡", "Ãœ", "Ã–", "ÄŸ", "Ä°", "Ã¢",
)


def _restore_math_zones(text: str, stash: List[str]) -> str:
    """Restore ``__GLOSSSTASH_N__`` placeholders in reverse order."""
    for idx in range(len(stash) - 1, -1, -1):
        text = text.replace(f"__GLOSSSTASH_{idx}__", stash[idx])
    return text


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class TermEntry:
    """A single glossary term."""
    en: str
    tr: str
    domain: str = "general"
    case_sensitive: bool = False
    keep_original: bool = False  # If True, TR = EN (do not translate)
    priority: str = "medium"     # "high", "medium", "low"
    wrong_alternatives: List[str] = field(default_factory=list)  # Wrong TR synonyms MT may produce

    @property
    def effective_tr(self) -> str:
        """Return the actual replacement text."""
        return self.en if self.keep_original else self.tr


@dataclass
class TermMatch:
    """A matched term in text."""
    en_term: str
    tr_term: str
    start: int
    end: int
    domain: str = "general"


@dataclass
class GlossaryStats:
    """Statistics about glossary operations."""
    terms_loaded: int = 0
    domains_loaded: int = 0
    terms_matched: int = 0
    terms_stashed: int = 0
    terms_restored: int = 0
    files_loaded: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Domain -> book_type mapping
# ---------------------------------------------------------------------------

# inject_terms() is DISABLED for ALL book types.
# Pre-translation TR injection poisons the source text because the MT model
# sees mixed EN+TR and hallucinates.  This was confirmed across both literary
# AND technical books (e.g., a mechanical watchmaking book --
# "torque" -> "tork" injected mid-sentence caused garbled output).
#
# Instead, terminology is enforced POST-translation via verify_terms():
# 1. Model translates clean English text
# 2. verify_terms() checks glossary terms and fixes wrong translations
# 3. wrong_alternatives catches known MT mistakes
#
# This matches the professional CAT tool approach: translate first, then
# apply terminology enforcement.
_SKIP_INJECT_BOOK_TYPES: Set[str] = {
    # ALL types -- inject_terms disabled universally
    "science", "engineering", "technical", "computer", "ml", "physics",
    "philosophy", "literature", "psychology",
    "history", "biography", "self_help", "fiction", "novel",
    "sociology", "politics", "business", "law", "religion",
    "memoir", "art", "sports", "food", "travel", "military",
    "medical", "nature", "general",
}

# Which domains are active for each book_type
_DOMAIN_MAP: Dict[str, List[str]] = {
    "science": ["general", "ml", "math", "physics", "cs"],
    "engineering": ["general", "math", "physics", "cs"],
    "technical": ["general", "cs", "math"],
    "computer": ["general", "cs", "ml"],
    "ml": ["general", "ml", "math", "cs"],
    "physics": ["general", "math", "physics"],
    # Felsefe / kişisel gelişim (6 Ekim 2026'ya kadar başka adla): genel
    # terimler + kendi alanı ("philosophy" alanlı terim yalnız bu türde).
    "philosophy": ["general", "philosophy"],
    "novel": ["general"],
    # Yeni kitap türleri (app.py sidebar ile uyumlu)
    "psychology": ["general"],
    "literature": ["general"],
    "history": ["general"],
    "medical": ["general", "physics"],
    "nature": ["general", "physics"],
    "business": ["general"],
    "law": ["general"],
    "politics": ["general"],
    "art": ["general"],
    "sports": ["general"],
    "food": ["general"],
    "travel": ["general"],
    "military": ["general"],
    "general": ["general"],
}

# Glossary filename -> domain(s) it covers.
# KARAR 25 (2026-10-05): genel alan sözlükleri (default/ml/math/physics/cs/
# science_engineering/technical .json ve felsefe türü sözlüğü) silindi —
# sabit genel sözlük yok, her kitap kendi sözlüğünü çıkarır
# (latex_pipeline.kitap_sozlugu).
# Kalan iki dosya: elle doğrulanmış curated.json (İngilizce kitapta kitap
# sözlüğünün başlangıcı) ve do_not_translate.json (core/ner_protector.py
# da okur). Haritada olmayan dosya YÜKLENMEZ (eski bir genel sözlük elle
# geri konsa bile sessizce devreye girmez).
_FILE_DOMAIN_MAP: Dict[str, Set[str]] = {
    "curated.json": {"general", "philosophy", "ml", "math", "physics", "cs"},
    "do_not_translate.json": {"general", "philosophy"},
}


# ---------------------------------------------------------------------------
# GlossaryEngine
# ---------------------------------------------------------------------------

class GlossaryEngine:
    """Terminology engine for pre-translation term protection.

    Parameters
    ----------
    glossary_dir : Path
        Directory containing JSON glossary files.
    book_type : str
        Book genre for domain selection (default: "science").
    extra_terms : dict, optional
        Additional EN->TR term mappings to merge (e.g., user-provided).
    """

    # Placeholder format -- distinct from translator's <<LATEX_N>>
    _PLACEHOLDER_FMT = "<<TERM_{}>>"
    _PLACEHOLDER_RE = re.compile(r"<<TERM_(\d+)>>")

    def __init__(
        self,
        glossary_dir: Path | str,
        book_type: str = "science",
        extra_terms: Optional[Dict[str, str]] = None,
    ):
        self.glossary_dir = Path(glossary_dir)
        self.book_type = book_type
        self.stats = GlossaryStats()

        # Internal storage
        self._terms: Dict[str, TermEntry] = {}  # EN (lower) -> TermEntry
        self._sorted_terms: List[TermEntry] = []  # sorted by length desc
        self._compiled_re: Optional[re.Pattern] = None

        # Load glossaries
        self.load_glossaries()

        # Merge extra terms
        if extra_terms:
            # Kullanici override'i dosya girislerini YENMELI. Eskiden bu
            # 'son yazan kazanir' yan etkisine dayaniyordu; tie-break artik
            # deterministik oldugu icin (#55) oncelik acikca verilir.
            for en, tr in extra_terms.items():
                self._add_term(
                    TermEntry(en=en, tr=tr, domain="extra", priority="high")
                )

        # Finalize: sort and compile
        self._finalize()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load_glossaries(self) -> None:
        """Load all relevant domain glossary files."""
        self._active_domains = set(_DOMAIN_MAP.get(self.book_type, ["general"]))
        active_domains = self._active_domains
        logger.info(
            "GlossaryEngine: book_type=%s, active domains=%s",
            self.book_type, active_domains,
        )

        if not self.glossary_dir.exists():
            logger.warning(
                "Glossary directory does not exist: %s", self.glossary_dir,
            )
            return

        loaded_count = 0
        for json_file in sorted(self.glossary_dir.glob("*.json")):
            # Check if this file's domains overlap with active domains.
            # Unknown files are SKIPPED (not loaded) to avoid pulling in
            # overly broad glossaries with common words.
            file_domains = _FILE_DOMAIN_MAP.get(json_file.name)
            if file_domains is None:
                logger.debug(
                    "Skipping %s (not in _FILE_DOMAIN_MAP)", json_file.name,
                )
                continue
            if not file_domains & active_domains:
                logger.debug(
                    "Skipping %s (domains %s not in active %s)",
                    json_file.name, file_domains, active_domains,
                )
                continue

            count = self._load_json_file(json_file)
            if count > 0:
                loaded_count += count
                self.stats.files_loaded.append(json_file.name)
                self.stats.domains_loaded += 1

        self.stats.terms_loaded = len(self._terms)
        logger.info(
            "GlossaryEngine: loaded %d terms from %d files.",
            self.stats.terms_loaded,
            len(self.stats.files_loaded),
        )

    def _load_json_file(self, path: Path) -> int:
        """Load a single JSON glossary file. Returns number of terms added.

        Term-level domain filtering: if a term has an explicit ``domain``
        or ``category`` field that does NOT match the active domains for
        this book_type, the term is skipped.  Terms with domain "general"
        or no explicit domain are always loaded (backward compatible).
        """
        try:
            _raw = path.read_text(encoding="utf-8")
            data = json.loads(_raw)
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Failed to load glossary %s: %s", path, exc)
            return 0

        # TR-kalite kapısı (yumuşak uyarı, reddetme YOK): dosyada bozuk
        # kodlama sinyali (mojibake / U+FFFD) varsa uyar. Türkçe çeviriler
        # temiz UTF-8 olmalı; bu işaretler yanlış decode edilmiş içeriği ele
        # verir ve çıktıda görünür bozulmaya yol açar.
        _mojibake_hits = [mk for mk in _TR_MOJIBAKE_MARKERS if mk in _raw]
        if _mojibake_hits:
            logger.warning(
                "TR kalite uyarısı: %s dosyasında bozuk kodlama sinyali (%s) — "
                "çevirilerde mojibake olabilir.",
                path.name, ", ".join(_mojibake_hits),
            )

        active = getattr(self, "_active_domains", {"general"})
        count = 0
        skipped = 0
        terms = data.get("terms", {})
        # Support two formats:
        # Format 1 (curated/science_engineering): {"term": {"tr": "...", ...}}
        # Format 2 (new default): [{"en": "term", "tr": "...", ...}]
        if isinstance(terms, dict):
            for en_term, info in terms.items():
                if isinstance(info, dict):
                    # Resolve domain: use explicit "domain" or "category"
                    # field ONLY.  "context" is a description field, NOT a
                    # domain identifier -- do not use it for filtering.
                    term_domain = info.get("domain", info.get("category"))
                    # For TermEntry storage, fall back to context->general
                    entry_domain = term_domain or info.get("context", "general")
                    # Term-level domain filter: if an explicit domain or
                    # category is set and it's NOT in active domains (and
                    # not "general"/"do_not_translate"), skip the term.
                    if (
                        term_domain is not None
                        and term_domain not in ("general", "do_not_translate")
                        and term_domain not in active
                    ):
                        skipped += 1
                        continue
                    entry = TermEntry(
                        en=en_term,
                        tr=info.get("tr", en_term),
                        domain=entry_domain,
                        case_sensitive=info.get("case_sensitive", False),
                        keep_original=info.get("keep_original", False),
                        priority=info.get("priority", "medium"),
                        wrong_alternatives=info.get("wrong_alternatives", []),
                    )
                elif isinstance(info, str):
                    entry = TermEntry(en=en_term, tr=info)
                else:
                    continue
                self._add_term(entry)
                count += 1
        elif isinstance(terms, list):
            for item in terms:
                if isinstance(item, dict) and "en" in item:
                    term_domain = item.get("domain", item.get("category"))
                    entry_domain = term_domain or "general"
                    if (
                        term_domain is not None
                        and term_domain not in ("general", "do_not_translate")
                        and term_domain not in active
                    ):
                        skipped += 1
                        continue
                    entry = TermEntry(
                        en=item["en"],
                        tr=item.get("tr", item["en"]),
                        domain=entry_domain,
                        case_sensitive=item.get("case_sensitive", False),
                        keep_original=item.get("keep_original", False),
                        priority=item.get("priority", "medium"),
                    )
                    self._add_term(entry)
                    count += 1

        # Also load do_not_translate list
        dnt = data.get("do_not_translate", [])
        for term in dnt:
            if isinstance(term, str):
                self._add_term(TermEntry(
                    en=term, tr=term, keep_original=True,
                    domain="do_not_translate",
                ))
                count += 1

        if skipped:
            logger.debug(
                "Skipped %d terms from %s (domain mismatch with active=%s)",
                skipped, path.name, active,
            )
        logger.debug("Loaded %d terms from %s", count, path.name)
        return count

    def _add_term(self, entry: TermEntry) -> None:
        """Add a term to the internal dictionary.

        If a term already exists, higher priority wins.
        If same priority, longer TR text wins (more specific).
        """
        key = entry.en.lower()
        existing = self._terms.get(key)
        if existing:
            # DATA-D6: keep_original (kitap adı / bilinçli "çevirme" girişi)
            # her zaman kazanır. Aksi hâlde (silinen) felsefe türü
            # sözlüğündeki TR-karakterli çeviri ('önleyici tıp'),
            # do_not_translate.json'daki keep_original girişini
            # ('Preventive Medicine') tie-break ile eziyordu.
            if existing.keep_original and not entry.keep_original:
                return  # Koru: mevcut keep_original girişi
            if entry.keep_original and not existing.keep_original:
                self._terms[key] = entry  # Yükselt: yeni keep_original girişi
                return
            prio_order = {"high": 3, "medium": 2, "low": 1}
            new_prio = prio_order.get(entry.priority, 2)
            old_prio = prio_order.get(existing.priority, 2)
            if new_prio < old_prio:
                return  # Keep existing (strictly higher priority)
            if new_prio == old_prio:
                # Same priority: prefer the entry with proper Turkish
                # special characters (ş, ğ, ı, ö, ü, ç, İ) over ASCII.
                tr_special = set("şğıöüçİŞĞÖÜÇ")
                new_has = bool(set(entry.tr) & tr_special)
                old_has = bool(set(existing.tr) & tr_special)
                if old_has and not new_has:
                    return  # Keep existing (better Turkish)
                if not old_has and not new_has and len(existing.tr) >= len(entry.tr):
                    return  # Both ASCII, keep existing
                # 2026-07-17 (#55/#86, eski #29): iki giris de TR-karakterli
                # ise de docstring'deki 'uzun kazanir' kurali uygulanir —
                # eskiden bu dalda kosul yoktu, dosya yukleme SIRASINA gore
                # sonuncu (kisa/kotu) giris kürasyonlu ceviriyi eziyordu.
                if old_has and new_has and len(existing.tr) >= len(entry.tr):
                    return  # Both Turkish, keep existing (longer/equal)
        self._terms[key] = entry

    # ------------------------------------------------------------------
    # Finalization (sort + compile regex)
    # ------------------------------------------------------------------

    def _finalize(self) -> None:
        """Sort terms by length (longest first) and compile the master regex."""
        # Sort: longest terms first for greedy matching
        self._sorted_terms = sorted(
            self._terms.values(),
            key=lambda t: len(t.en),
            reverse=True,
        )

        if not self._sorted_terms:
            self._compiled_re = None
            return

        # Build alternation regex: (term1|term2|term3|...)
        # Each term is wrapped in word boundaries.
        # Case-sensitive terms get their own group (handled separately).
        patterns = []
        for entry in self._sorted_terms:
            escaped = re.escape(entry.en)
            # Use word boundaries to prevent partial matching
            # e.g., "neural" should not match "neurality"
            patterns.append(r"\b" + escaped + r"\b")

        # Compile with IGNORECASE -- case-sensitive terms are handled
        # in the match callback by checking the original entry.
        combined = "|".join(patterns)
        try:
            self._compiled_re = re.compile(
                f"({combined})", re.IGNORECASE,
            )
        except re.error as exc:
            logger.error(
                "Failed to compile glossary regex (%d terms): %s",
                len(patterns), exc,
            )
            self._compiled_re = None

    # ------------------------------------------------------------------
    # Term matching
    # ------------------------------------------------------------------

    def match_terms(self, text: str) -> List[TermMatch]:
        """Find all glossary terms in *text*.

        Returns a list of TermMatch objects sorted by position.
        Already-stashed regions (``<<LATEX_N>>``) are skipped.
        """
        if not self._compiled_re or not text:
            return []

        matches: List[TermMatch] = []
        # Find regions that are already placeholders (<<LATEX_N>> or <<TERM_N>>)
        # Use interval-based check instead of per-character set for O(n*k) vs O(n*m)
        placeholder_intervals = [
            (pm.start(), pm.end())
            for pm in re.finditer(r"<<(?:LATEX|TERM)_\d+>>", text)
        ]

        def _overlaps_placeholder(start: int, end: int) -> bool:
            return any(ps < end and pe > start for ps, pe in placeholder_intervals)

        for m in self._compiled_re.finditer(text):
            # Skip if this match overlaps with a placeholder
            if _overlaps_placeholder(m.start(), m.end()):
                continue

            matched_text = m.group(0)
            key = matched_text.lower()
            entry = self._terms.get(key)
            if not entry:
                continue

            # Case-sensitive check: if entry requires exact case,
            # verify the matched text matches exactly
            if entry.case_sensitive and matched_text != entry.en:
                continue

            matches.append(TermMatch(
                en_term=entry.en,
                tr_term=entry.effective_tr,
                start=m.start(),
                end=m.end(),
                domain=entry.domain,
            ))

        self.stats.terms_matched += len(matches)
        return matches

    # ------------------------------------------------------------------
    # Direct TR Injection (preferred -- replaces placeholder approach)
    # ------------------------------------------------------------------

    def inject_terms(self, text: str) -> Tuple[str, Dict[int, TermMatch]]:
        """Pre-translation term injection -- UNCONDITIONALLY DISABLED (GLS-08).

        Historically this replaced EN terms with TR translations before MT.
        That poisoned the source (mixed EN+TR -> MT hallucination) across BOTH
        literary and technical books, so injection was disabled per-book_type
        via a skip-list.  A skip-list is fragile: any book_type accidentally
        left off the list would silently re-enable poisoning.

        The method is now a hard no-op for EVERY book_type: it always returns
        the text unchanged and an empty term_map.  Terminology is enforced
        POST-translation by :meth:`verify_terms` (Pass 3), which is the single
        application mechanism -- ``verify_terms`` is the only place glossary
        terms are ever written into the output.

        The empty ``term_map`` is preserved so existing callers that unpack
        ``(text, term_map)`` keep working; ``verify_terms`` ignores it.
        """
        return text, {}

    def verify_terms(
        self,
        text: str,
        term_map: Dict[int, TermMatch],
    ) -> str:
        """Post-translation verification: ensure glossary terms are correct.

        Two passes (both skip başlık/caption/matematik/kod bölgelerini):
        1. **wrong_alternatives pass**: scan loaded glossary entries for
           known wrong translations and replace them with the preferred term.
        2. **EN echo scan**: for non-injected book types, scan for English
           glossary terms that survived MT unchanged and replace them with
           the correct Turkish translation.

        ``term_map`` parametresi geriye uyumluluk için tutulur (çağrı yerleri
        pozisyonel geçiriyor) ama artık kullanılmaz: inject_terms tüm
        book_type'larda devre dışı olduğundan Pass 1 re-injection ÖLÜ KODdu
        (kanıt: doğrulayıcı raporu) ve kaldırıldı.

        PROTECTION: Bu metod başlık/caption komutlarını, kod ortamlarını
        (verbatim/lstlisting/minted), matematik ortamlarını, display/inline
        math, ref komutlarını, ortam sınırlayıcılarını ve LaTeX komutlarını
        HERHANGİ bir glossary sübstitüsyonundan ÖNCE stash'ler. Böylece
        "alpha"->"alfa dalgası" gibi girişler ``\\alpha``'yı veya başlıkları
        bozmaz.
        """
        # --- Başlık/kod/math/LaTeX koruması: her sübstitüsyondan ÖNCE stash ---
        text, _math_stash = _protect_math_zones(text)
        if _math_stash:
            logger.debug(
                "verify_terms: stashed %d heading/code/math/LaTeX zones.",
                len(_math_stash),
            )

        # --- Pass 2: wrong_alternatives (ALWAYS runs) ---
        alt_fixed = 0
        for entry in self._terms.values():
            if not entry.wrong_alternatives:
                continue
            correct_tr = entry.effective_tr
            for wrong in entry.wrong_alternatives:
                # Use word boundary to avoid substring false positives
                # e.g. "kumar" should not match inside "kumarcı"
                # 2026-07-17 (#86, eski #30/#67): IGNORECASE + _match_case —
                # cumle basinda buyuk harfle gecen yanlis ceviri ('Aşırı
                # öğrenme ...') eskiden hic yakalanmiyor, yakalaninca da
                # sozluk case'i basilip cumle basi kucuk kaliyordu.
                # alt_fixed artik desen degil OLUSUM sayar.
                pattern = re.compile(
                    r'\b' + re.escape(wrong) + r'\b', re.IGNORECASE
                )
                text, n_sub = pattern.subn(
                    lambda m, _c=correct_tr: _match_case(m.group(0), _c),
                    text,
                )
                if n_sub:
                    alt_fixed += n_sub
                    logger.debug(
                        "verify_terms: replaced wrong alt '%s' -> '%s' (%dx).",
                        wrong, correct_tr, n_sub,
                    )
        if alt_fixed:
            logger.info(
                "GlossaryEngine verify: fixed %d wrong alternatives.", alt_fixed,
            )

        # --- Pass 3: EN echo scan (post-translation terminology enforcement) ---
        # DARALTILDI (GLS-D1 / GLS-D7 / GLS-02): Bağlamsız tam-gövde tek-kelime
        # sübstitüsyonu anlam bozuyordu ("alpha" -> "alfa dalgası" HER yerde) ve
        # O(terim x gövde) yavaştı. Artık yalnızca DÜŞÜK yanlış-pozitif riskli
        # terimler taranır:
        #   - Çok-kelimeli terimler ("gradient descent"), VEYA
        #   - Yüksek-öncelikli (curated) tek-kelime terimler.
        # Tek-kelimeli orta/düşük öncelikli terimler ARTIK taranmaz.
        # Yerine koyma case-preserving'dir (_match_case): cümle başı büyük harf
        # ve ALL-CAPS korunur, sözlükteki büyük harfli TR cümle ortasına
        # zorlanmaz.
        if self._terms:
            # GLS-D7 perf: eskiden her uygun terim için ayrı re.compile +
            # search + sub (O(terim x gövde)) yapılıyordu (~891 terim => ~891
            # tam tarama).  Artık uygun terimler TEK birleşik alternation
            # regex'te toplanır ve tek geçişte callback ile değiştirilir.
            #
            # Davranış birebir korunur:
            #   - Aynı filtreler (uzunluk<4, EN==TR, yalnızca çok-kelime VEYA
            #     yüksek-öncelik tek-kelime, _PASS3_COMMON_WORDS).
            #   - Longest-first: alternation grubu _sorted_terms sırasıyla
            #     (uzundan kısaya) kurulur; regex motoru bir konumda alternatif-
            #     leri sırayla dener, böylece "gradient descent" tek başına
            #     "gradient"tan önce eşleşir (greedy).
            #   - Case-preserving replacement (_match_case) korunur.
            #   - \b...\b sınırları her terim için aynı davranışı verir.
            pass3_map: Dict[str, str] = {}
            pass3_patterns: List[str] = []
            for entry in self._sorted_terms:
                en = entry.en
                tr = entry.effective_tr
                # Skip very short terms (high false positive risk)
                if len(en) < 4:
                    continue
                # Skip if EN == TR (do_not_translate terms)
                if en.lower() == tr.lower():
                    continue
                # Daraltma: yalnızca çok-kelimeli VEYA yüksek-öncelik tek-kelime.
                is_multiword = " " in en.strip()
                if not is_multiword and entry.priority != "high":
                    continue
                # Skip if the term is a common word that MT should handle
                if en.lower() in _PASS3_COMMON_WORDS:
                    continue
                key = en.lower()
                if key in pass3_map:
                    # _sorted_terms uzundan kısaya; ilk (en uzun/öncelikli)
                    # giriş kazanır, tekrar eden aynı-anahtar atlanır.
                    continue
                pass3_map[key] = tr
                pass3_patterns.append(re.escape(en))

            if pass3_patterns:
                combined_re = re.compile(
                    r'\b(' + '|'.join(pass3_patterns) + r')\b',
                    re.IGNORECASE,
                )

                def _echo_repl(m: "re.Match[str]") -> str:
                    matched = m.group(0)
                    _tr = pass3_map.get(matched.lower())
                    if _tr is None:
                        return matched
                    return _match_case(matched, _tr)

                text, echo_fixed = combined_re.subn(_echo_repl, text)
                if echo_fixed:
                    logger.info(
                        "GlossaryEngine verify: replaced %d EN echoes "
                        "(post-translation terminology enforcement).",
                        echo_fixed,
                    )

        # --- Restore math/LaTeX zones ---
        text = _restore_math_zones(text, _math_stash)

        return text

    # ------------------------------------------------------------------
    # Stash / Restore (DEPRECATED -- kept for backward compatibility)
    # ------------------------------------------------------------------

    def stash_terms(self, text: str) -> Tuple[str, Dict[int, TermMatch]]:
        """Replace glossary terms with ``<<TERM_N>>`` placeholders.

        .. deprecated::
            Use :meth:`inject_terms` instead.  Placeholder-based stashing
            suffers from 50%+ loss with HY-MT 7B.  This method is kept
            only for backward compatibility and testing.

        Parameters
        ----------
        text : str
            Text with already-stashed LaTeX (``<<LATEX_N>>`` placeholders).

        Returns
        -------
        (modified_text, term_map)
            *term_map* maps placeholder index -> TermMatch.
        """
        import warnings
        warnings.warn(
            "stash_terms() is deprecated, use inject_terms() instead.",
            DeprecationWarning,
            stacklevel=2,
        )

        if not self._compiled_re or not text:
            return text, {}

        matches = self.match_terms(text)
        if not matches:
            return text, {}

        # Process matches in reverse order (right to left) to preserve indices
        term_map: Dict[int, TermMatch] = {}
        idx_counter = 0

        # Remove overlapping matches (keep first/longest)
        used_ranges: List[Tuple[int, int]] = []
        filtered: List[TermMatch] = []
        for match in sorted(matches, key=lambda m: m.start):
            overlaps = False
            for start, end in used_ranges:
                if match.start < end and match.end > start:
                    overlaps = True
                    break
            if not overlaps:
                filtered.append(match)
                used_ranges.append((match.start, match.end))

        # Now replace in reverse order
        for match in sorted(filtered, key=lambda m: m.start, reverse=True):
            placeholder = self._PLACEHOLDER_FMT.format(idx_counter)
            term_map[idx_counter] = match
            text = text[:match.start] + placeholder + text[match.end:]
            idx_counter += 1

        self.stats.terms_stashed += len(term_map)
        logger.info(
            "GlossaryEngine: stashed %d terms (%d unique).",
            len(term_map),
            len({m.en_term for m in term_map.values()}),
        )
        return text, term_map

    def restore_terms(
        self,
        text: str,
        term_map: Dict[int, TermMatch],
    ) -> str:
        """Replace ``<<TERM_N>>`` placeholders with Turkish equivalents.

        .. deprecated::
            Use :meth:`verify_terms` after :meth:`inject_terms` instead.

        Parameters
        ----------
        text : str
            Translated text containing ``<<TERM_N>>`` placeholders.
        term_map : dict
            Mapping from placeholder index to TermMatch (from stash_terms).

        Returns the text with all term placeholders replaced by TR terms.
        """
        import warnings
        warnings.warn(
            "restore_terms() is deprecated, use verify_terms() instead.",
            DeprecationWarning,
            stacklevel=2,
        )

        if not term_map:
            return text

        restored = 0
        for idx, match in term_map.items():
            placeholder = self._PLACEHOLDER_FMT.format(idx)
            if placeholder in text:
                text = text.replace(placeholder, match.tr_term)
                restored += 1
            else:
                # Try mangled versions (MT model might add spaces)
                mangled_re = re.compile(
                    r"<\s*<\s*TERM[_\s]*" + str(idx) + r"\s*>\s*>"
                )
                new_text = mangled_re.sub(match.tr_term, text)
                if new_text != text:
                    text = new_text
                    restored += 1
                    logger.debug(
                        "Recovered mangled TERM placeholder %d.", idx,
                    )
                else:
                    logger.warning(
                        "TERM placeholder %d not found (term: '%s').",
                        idx, match.en_term,
                    )

        self.stats.terms_restored += restored
        logger.info(
            "GlossaryEngine: restored %d/%d term placeholders.",
            restored, len(term_map),
        )
        return text

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @property
    def term_count(self) -> int:
        """Number of loaded terms."""
        return len(self._terms)

    def get_term(self, en_term: str) -> Optional[TermEntry]:
        """Look up a term by English text (case-insensitive)."""
        return self._terms.get(en_term.lower())

    def get_all_terms(self) -> List[TermEntry]:
        """Return all loaded terms sorted by length (longest first)."""
        return list(self._sorted_terms)

    def get_domains(self) -> Set[str]:
        """Return set of all domains present in loaded terms."""
        return {entry.domain for entry in self._terms.values()}

    def summary(self) -> str:
        """Return a human-readable summary of loaded glossaries."""
        domains = {}
        for entry in self._terms.values():
            domains.setdefault(entry.domain, 0)
            domains[entry.domain] += 1

        lines = [
            f"GlossaryEngine: {self.term_count} terms loaded",
            f"  Book type: {self.book_type}",
            f"  Files: {', '.join(self.stats.files_loaded)}",
            f"  Domains:",
        ]
        for domain, count in sorted(domains.items()):
            lines.append(f"    {domain}: {count} terms")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Convenience: create engine from project root
# ---------------------------------------------------------------------------

def create_glossary_engine(
    project_dir: Path | str,
    book_type: str = "science",
    extra_terms: Optional[Dict[str, str]] = None,
) -> GlossaryEngine:
    """Create a GlossaryEngine from the project's glossary directory.

    Parameters
    ----------
    project_dir : Path
        Project root (e.g., ``/path/to/kitapcevir``).
    book_type : str
        Book genre for domain selection.
    extra_terms : dict, optional
        Additional EN->TR mappings.
    """
    glossary_dir = Path(project_dir) / "glossary"
    return GlossaryEngine(
        glossary_dir=glossary_dir,
        book_type=book_type,
        extra_terms=extra_terms,
    )


# ---------------------------------------------------------------------------
# CLI interface
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)

    if len(sys.argv) < 2:
        print("Usage: python -m latex_pipeline.glossary <glossary_dir> [book_type]")
        print("       python -m latex_pipeline.glossary glossary/ science")
        sys.exit(1)

    glossary_dir = Path(sys.argv[1])
    book_type = sys.argv[2] if len(sys.argv) > 2 else "science"

    engine = GlossaryEngine(glossary_dir=glossary_dir, book_type=book_type)
    print(engine.summary())
    print()

    # Interactive test
    print("Enter text to test term matching (Ctrl+C to quit):")
    try:
        while True:
            text = input("> ")
            matches = engine.match_terms(text)
            if matches:
                for m in matches:
                    print(f"  [{m.start}:{m.end}] '{m.en_term}' -> '{m.tr_term}' ({m.domain})")
                injected, term_map = engine.inject_terms(text)
                print(f"  Injected: {injected}")
                verified = engine.verify_terms(injected, term_map)
                print(f"  Verified: {verified}")
            else:
                print("  (no matches)")
    except (KeyboardInterrupt, EOFError):
        print()
