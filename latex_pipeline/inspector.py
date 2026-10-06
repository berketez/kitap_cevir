"""
LaTeX Pipeline Quality Inspector.

Analyzes compiled LaTeX output for quality issues:
  - Formula statistics (display/inline count, equation numbers)
  - Image reference status (found/missing/wrong extension)
  - Heading structure tree
  - Compilation errors and warnings
  - Missing characters
  - Content coverage (chars per page, blank page detection)

Usage:
    inspector = QualityInspector(config)
    report = inspector.inspect(tex_path, log_path, pdf_path)
    report.print_summary()
    report.print_details()

CLI:
    python -m latex_pipeline.inspector output/formula_test/source/book.tex
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# XeLaTeX günlük ayrıştırma (tek kaynak: inspector, compiler, 2_derle)
# ---------------------------------------------------------------------------
#
# R5 (2026-09-25): derleme -file-line-error ile koşar; TeX hataları günlükte
# "! İleti" değil "dosya.tex:206: İleti" biçiminde çıkar. Eski ayrıştırıcı
# yalnız "!" ile başlayan satırı hata saydığı için 7 belgenin raporu gerçek
# hatalara (Double superscript, Misplaced alignment tab, Missing $ ...) karşın
# "Errors: 0" diyordu. Eksik glif deseni de yalnız "(U+XXXX)" biçimini
# tanıyordu; 8 bitlik TFM yazıtiplerinde (cmmi10 vb.) XeTeX kodu '("E7)'
# biçiminde yazar, bu kayıtların hepsi kaçıyordu.

# TeX günlük satırını max_print_line karakterde (TeX Live varsayılanı 79)
# keser; kesilen parça bir sonraki satırda sürer. compiler.py bu sınırı
# yükseltir (TEX_LOG_MAX_PRINT_LINE); eski/kesilmiş günlükler için ayrıştırıcı
# parçaları yeniden birleştirir.
TEX_LOG_WRAP_WIDTH = 79

# "dosya.uzantı:SATIR: ileti" (-file-line-error). Yol boşluk içerebilir;
# iki nokta ve ters bölü içeremez, ilk karakteri boşluk/"["/"(" olamaz —
# overfull kutu dökümü ("[]\TU/...") ve bağlam satırları böylece elenir.
# Uzantı TeX'in okuduğu dosya türleriyle sınırlıdır.
_TEX_SOURCE_EXTS = (
    "tex", "ltx", "sty", "cls", "def", "cfg", "clo", "fd", "aux", "toc",
    "lof", "lot", "out", "bbl", "ind", "nav", "snm", "vrb", "dtx", "ins",
)
_FILE_LINE_ERROR_RE = re.compile(
    r"^(?P<file>[^\s:\\\[(][^:\\]*?\.(?:" + "|".join(_TEX_SOURCE_EXTS) + r"))"
    r":(?P<line>\d+): (?P<msg>.*\S)\s*$"
)
# "! ileti" satırından sonra gelen "l.123 ..." bağlam satırı.
_ERROR_CONTEXT_LINE_RE = re.compile(r"^l\.(\d+)\b")
# Hata satırının kaç satır altına kadar "l.N" aranır (compiler.py ile aynı).
_ERROR_CONTEXT_LOOKAHEAD = 6
# İş sonunda basılan özet ("!  ==> Fatal error occurred, no output PDF file
# produced!"): kendi başına bir TeX hatası değil, önceki hataların sonucu.
_FATAL_SUMMARY_RE = re.compile(r"^!\s+==>\s*Fatal error occurred", re.IGNORECASE)
# Eksik glif: XeTeX kodu OpenType yazıtipinde "(U+00E7)", 8 bitlik TFM
# yazıtipinde '("E7)' biçiminde yazar. Yazıtipi adı "!" ile biter.
_MISSING_CHAR_RE = re.compile(
    r"Missing character: There is no (?P<char>.+?)"
    r"(?: \((?:U\+(?P<uni>[0-9A-Fa-f]+)|\"(?P<hex>[0-9A-Fa-f]+))\))?"
    r" in font (?P<font>[^!\n]*)!"
)
# Günlüğün sonundaki bellek dökümü max_print_line'a uymaz (kesilmez).
_TEX_MEMORY_TRAILER = "Here is how much of TeX's memory you used:"
# Kesilmiş günlükte yeni kayıt başlatan satır başları: bunlar önceki 79
# karakterlik satırın devamı sayılmaz.
_TEX_LOG_RECORD_STARTS = ("Missing character: There is no ", "! ")


def _tex_log_is_wrapped(lines: List[str], width: int) -> bool:
    """Günlük *width* karakterde kesilmiş mi?

    İlk satır (sürüm başlığı) ve sondaki bellek dökümü kesme kuralına uymaz;
    gövdede *width*'ten uzun tek satır varsa günlük kesilmemiştir (compiler
    max_print_line'ı yükseltmiştir) ve birleştirme yapılmaz.
    """
    body = lines[1:]
    for idx, line in enumerate(body):
        if line.startswith(_TEX_MEMORY_TRAILER):
            body = body[:idx]
            break
    return not any(len(line) > width for line in body)


def unwrap_tex_log(text: str, width: int = TEX_LOG_WRAP_WIDTH) -> List[str]:
    """TeX günlüğünü mantıksal satırlara ayır: kesilmiş satırları birleştir.

    TeX (XeTeX Unicode karakter sayar) bir satırı tam *width* karakterde
    keser; bu yüzden tam *width* uzunluğundaki satır bir sonrakinde sürer.
    Günlük kesilmemişse (bkz. ``_tex_log_is_wrapped``) satırlar olduğu gibi
    döner.
    """
    lines = text.splitlines()
    if not _tex_log_is_wrapped(lines, width):
        return lines
    out: List[str] = []
    buf: Optional[str] = None
    for line in lines:
        if buf is not None and not line.startswith(_TEX_LOG_RECORD_STARTS):
            buf += line
        else:
            if buf is not None:
                out.append(buf)
            buf = line
        if len(line) != width:
            out.append(buf)
            buf = None
    if buf is not None:
        out.append(buf)
    return out


def match_file_line_error(line: str) -> Optional[Tuple[str, int, str]]:
    """``-file-line-error`` hata satırı ise ``(dosya, satır, ileti)``."""
    m = _FILE_LINE_ERROR_RE.match(line.strip())
    if not m:
        return None
    return m.group("file"), int(m.group("line")), m.group("msg").strip()


def _missing_font_name(font: str) -> str:
    """Yazıtipi tanımından adı al: özellik eki (/OT:..., :mapping=...) atılır.

    "STIX Two Text Regular/OT:script=latn;..." -> "STIX Two Text Regular",
    "[lmroman10-regular]:mapping=tex-text;" -> "[lmroman10-regular]".
    """
    font = font.strip()
    m = re.match(r"\[[^\]]*\]|[^/:]+", font)
    return (m.group(0).strip() if m else "") or font


def parse_tex_log(log_text: str) -> Tuple[List["CompileIssue"], List["CompileIssue"]]:
    """XeLaTeX günlüğünden ``(hatalar, eksik_glifler)``.

    Hatalar: ``-file-line-error`` kayıtları, "!" ile başlayan kayıtlar
    (satır numarası sonraki "l.N" bağlam satırından), paket hataları ve
    xdvipdfmx ölümcül hataları. Aynı dosya/satır/ileti bir kez sayılır; iş
    sonu özeti ("==> Fatal error occurred") ayrı bir hata sayılmaz.

    Eksik glifler karakter + yazıtipi başına birleştirilir; ``count`` o
    kaydın günlükte kaç kez geçtiğidir (toplam eksik glif = count toplamı).
    """
    lines = unwrap_tex_log(log_text)
    errors: List[CompileIssue] = []
    seen = set()

    def _add(message: str, line_num: Optional[int] = None,
             source: Optional[str] = None) -> None:
        key = (source, line_num, message)
        if key in seen:
            return
        seen.add(key)
        errors.append(CompileIssue("error", message, line_num, source=source))

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        fle = match_file_line_error(stripped)
        if fle:
            path, num, msg = fle
            _add(msg, num, Path(path).name)
        elif stripped.startswith("!"):
            if _FATAL_SUMMARY_RE.match(stripped):
                continue
            msg = stripped[1:].strip()
            if not msg:
                continue
            num = None
            for nxt in lines[idx + 1: idx + 1 + _ERROR_CONTEXT_LOOKAHEAD]:
                m = _ERROR_CONTEXT_LINE_RE.match(nxt.strip())
                if m:
                    num = int(m.group(1))
                    break
            _add(msg, num)
        elif re.search(r"package .+ error:", stripped, re.IGNORECASE):
            _add(stripped)
        elif "xdvipdfmx:fatal" in stripped.lower():
            _add(stripped)

    counts: Dict[str, int] = {}
    for m in _MISSING_CHAR_RE.finditer("\n".join(lines)):
        code_hex = m.group("uni") or m.group("hex")
        code = f" (U+{int(code_hex, 16):04X})" if code_hex else ""
        desc = f"{m.group('char')}{code} in {_missing_font_name(m.group('font'))}"
        counts[desc] = counts.get(desc, 0) + 1
    missing = [CompileIssue("missing_char", desc, count=n)
               for desc, n in sorted(counts.items(), key=lambda x: -x[1])]
    return errors, missing


def tex_log_counts(log_path: Path) -> Tuple[int, int]:
    """Derleme günlüğündeki ``(TeX hatası sayısı, eksik glif sayısı)``.

    Eksik glif sayısı günlükteki "Missing character" kayıtlarının toplamıdır
    (aynı karakter üç kez eksikse 3). Günlük okunamazsa ``(0, 0)``.
    """
    try:
        text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0, 0
    errors, missing = parse_tex_log(text)
    return len(errors), sum(mc.count for mc in missing)


# ---------------------------------------------------------------------------
# Report data classes
# ---------------------------------------------------------------------------

@dataclass
class FormulaStats:
    display_math: int = 0          # \[...\] blocks
    equation_env: int = 0          # equation/equation* environments
    inline_math: int = 0           # $...$ or \(...\)
    tagged_equations: int = 0      # \tag{...}
    align_env: int = 0             # align/align* environments
    total_display: int = 0

    def __post_init__(self):
        self.total_display = self.display_math + self.equation_env + self.align_env


@dataclass
class ImageInfo:
    filename: str
    line_num: int
    found: bool = False          # asset exists on disk (exact OR alternative ext)
    actual_path: Optional[str] = None
    # F43 (2026-07-29): tex farkli uzanti referans ediyor ama dosya diskte var
    # (ör. tex '.jpg' diyor, diskte 'fig1.png').  Asset kayip DEGIL, referans
    # yanlis: 'missing' degil ayri bir kategori olarak raporlanir.
    wrong_ext: bool = False


@dataclass
class HeadingInfo:
    level: int            # 1=chapter, 2=section, etc.
    command: str          # "chapter", "section*", etc.
    title: str
    line_num: int


@dataclass
class PageInfo:
    page_num: int
    char_count: int
    has_formula: bool
    has_image: bool
    is_blank: bool


@dataclass
class CompileIssue:
    severity: str         # "error", "warning", "missing_char"
    message: str
    line_num: Optional[int] = None
    count: int = 1
    # R5: -file-line-error kaydının dosya adı (ör. "book_tr.tex"); yoksa None.
    source: Optional[str] = None


@dataclass
class QualityReport:
    """Complete quality report for a LaTeX compilation."""

    # Source info
    tex_path: Optional[Path] = None
    log_path: Optional[Path] = None
    pdf_path: Optional[Path] = None
    tex_size: int = 0
    pdf_size: int = 0
    pdf_pages: int = 0

    # Formula analysis
    formulas: FormulaStats = field(default_factory=FormulaStats)

    # Images
    images: List[ImageInfo] = field(default_factory=list)
    images_found: int = 0
    images_missing: int = 0
    images_wrong_ext: int = 0   # F43: diskte var, tex yanlis uzantiyi gosteriyor

    # Headings
    headings: List[HeadingInfo] = field(default_factory=list)

    # Pages
    pages: List[PageInfo] = field(default_factory=list)
    blank_pages: int = 0

    # Compilation issues
    errors: List[CompileIssue] = field(default_factory=list)
    warnings: List[CompileIssue] = field(default_factory=list)
    missing_chars: List[CompileIssue] = field(default_factory=list)

    # Overall score
    score: float = 0.0  # 0-100

    @property
    def tex_error_count(self) -> int:
        """Günlükteki TeX hatası sayısı (tekilleştirilmiş kayıt)."""
        return len(self.errors)

    @property
    def missing_char_total(self) -> int:
        """Eksik glif kaydı toplamı (her karakter kaç kez eksikse o kadar)."""
        return sum(mc.count for mc in self.missing_chars)

    def print_summary(self):
        """Print a concise quality summary."""
        print("=" * 60)
        print("QUALITY REPORT")
        print("=" * 60)

        # Basic info
        print(f"\n📄 Source: {self.tex_path}")
        if self.pdf_path:
            print(f"   PDF: {self.pdf_path} ({self.pdf_size:,} bytes, {self.pdf_pages} pages)")
        print(f"   TeX: {self.tex_size:,} bytes")

        # Score
        grade = "A" if self.score >= 90 else "B" if self.score >= 75 else "C" if self.score >= 60 else "D" if self.score >= 40 else "F"
        print(f"\n⭐ Score: {self.score:.0f}/100 ({grade})")

        # Formulas
        f = self.formulas
        print(f"\n📐 Formulas:")
        print(f"   Display: {f.total_display} (\\[\\]: {f.display_math}, equation*: {f.equation_env}, align: {f.align_env})")
        print(f"   Inline:  {f.inline_math}")
        print(f"   Tagged:  {f.tagged_equations}")

        # Images
        print(f"\n🖼️  Images: {len(self.images)} references")
        print(f"   Found:   {self.images_found}")
        print(f"   Missing: {self.images_missing}")
        if self.images_wrong_ext:
            print(f"   Wrong extension: {self.images_wrong_ext}")

        # Headings
        print(f"\n📑 Headings: {len(self.headings)}")
        for h in self.headings[:15]:
            indent = "  " * (h.level - 1)
            star = "*" if "*" in h.command else ""
            print(f"   {indent}\\{h.command}{{{h.title[:50]}}}")
        if len(self.headings) > 15:
            print(f"   ... +{len(self.headings) - 15} more")

        # Pages
        if self.pages:
            print(f"\n📃 Pages: {len(self.pages)}")
            print(f"   Blank:  {self.blank_pages}")
            chars = [p.char_count for p in self.pages if not p.is_blank]
            if chars:
                print(f"   Avg chars/page: {sum(chars)//len(chars)}")
                print(f"   Min/Max: {min(chars)}/{max(chars)}")

        # Issues
        print(f"\n⚠️  Issues:")
        print(f"   Errors:   {self.tex_error_count}")
        print(f"   Warnings: {len(self.warnings)}")
        # R5: toplam eksik glif (eskiden yalnız farklı karakter sayısıydı).
        print(f"   Missing chars: {self.missing_char_total} "
              f"({len(self.missing_chars)} distinct)")

    def print_details(self):
        """Print detailed issue list."""
        if self.errors:
            print("\n--- ERRORS ---")
            for e in self.errors[:20]:
                if e.source and e.line_num:
                    loc = f" ({e.source}:{e.line_num})"
                elif e.line_num:
                    loc = f" (line {e.line_num})"
                else:
                    loc = ""
                print(f"  ❌ {e.message}{loc}")
            if len(self.errors) > 20:
                print(f"  ... +{len(self.errors) - 20} more")

        if self.missing_chars:
            print("\n--- MISSING CHARACTERS ---")
            for mc in self.missing_chars:
                print(f"  ⚡ {mc.message} (x{mc.count})")

        if self.images_missing > 0:
            print("\n--- MISSING IMAGES ---")
            for img in self.images:
                if not img.found:
                    print(f"  🖼️  {img.filename} (line {img.line_num})")

        if self.images_wrong_ext > 0:
            print("\n--- WRONG EXTENSION IMAGES ---")
            for img in self.images:
                if img.wrong_ext:
                    print(f"  - {img.filename} (line {img.line_num}) "
                          f"-> {img.actual_path}")

        if self.warnings:
            print(f"\n--- WARNINGS ({len(self.warnings)}) ---")
            for w in self.warnings[:10]:
                print(f"  ⚠️  {w.message}")
            if len(self.warnings) > 10:
                print(f"  ... +{len(self.warnings) - 10} more")


# ---------------------------------------------------------------------------
# Quality Inspector
# ---------------------------------------------------------------------------

class QualityInspector:
    """Analyze LaTeX compilation quality."""

    def __init__(self, images_dir: Optional[Path] = None):
        self.images_dir = images_dir

    def inspect(
        self,
        tex_path: Path,
        log_path: Optional[Path] = None,
        pdf_path: Optional[Path] = None,
    ) -> QualityReport:
        """
        Run full quality inspection.

        Parameters
        ----------
        tex_path : Path to the .tex source file.
        log_path : Path to compilation .log (auto-detected if None).
        pdf_path : Path to compiled .pdf (auto-detected if None).

        Returns
        -------
        QualityReport with all analysis results.
        """
        tex_path = Path(tex_path)
        if log_path is None:
            log_path = tex_path.with_suffix(".log")
        if pdf_path is None:
            pdf_path = tex_path.with_suffix(".pdf")

        report = QualityReport(
            tex_path=tex_path,
            log_path=log_path if log_path.exists() else None,
            pdf_path=pdf_path if pdf_path.exists() else None,
        )

        # Read .tex
        if tex_path.exists():
            tex_text = tex_path.read_text(encoding="utf-8", errors="replace")
            report.tex_size = len(tex_text.encode("utf-8"))
            self._analyze_tex(tex_text, report)

        # Read .log
        if log_path and log_path.exists():
            log_text = log_path.read_text(encoding="utf-8", errors="replace")
            self._analyze_log(log_text, report)

        # Read .pdf
        if pdf_path and pdf_path.exists():
            report.pdf_size = pdf_path.stat().st_size
            self._analyze_pdf(pdf_path, report)

        # Calculate score
        report.score = self._calculate_score(report)

        return report

    # ------------------------------------------------------------------
    # TeX analysis
    # ------------------------------------------------------------------

    def _analyze_tex(self, tex: str, report: QualityReport):
        """Analyze .tex source for formulas, images, headings."""
        lines = tex.split("\n")

        # Formula stats
        fs = FormulaStats()
        # F43 (2026-07-29): eski desen r"\\\\?\[" ikinci backslash'i OPSIYONEL
        # yaptigi icin '\\[2mm]' (satir kirimi + dikey bosluk) da display math
        # sayiliyor, formul istatistigi siseriyordu.  Artik yalnizca ONUNDE
        # backslash OLMAYAN '\[' sayilir: gercek display math acilisi.
        fs.display_math = len(re.findall(r"(?<!\\)\\\[", tex))  # \[
        fs.equation_env = len(re.findall(r"\\begin\{equation\*?\}", tex))
        fs.align_env = len(re.findall(r"\\begin\{align\*?\}", tex))
        fs.tagged_equations = len(re.findall(r"\\tag\{", tex))

        # Inline math: count $...$ and \(...\)
        # Protect display math first
        temp = re.sub(r"\$\$.+?\$\$", "", tex, flags=re.DOTALL)
        # F43: ayni lookbehind — '\\[2mm]' display math sanilirsa silme araligi
        # bir sonraki gercek '\]'e kadar uzayip aradaki inline math'i yutuyordu.
        temp = re.sub(r"(?<!\\)\\\[.+?\\\]", "", temp, flags=re.DOTALL)
        fs.inline_math = len(re.findall(r"(?<!\\)\$(?!\$).+?(?<!\\)\$(?!\$)", temp))
        fs.inline_math += len(re.findall(r"\\\(.+?\\\)", temp))
        fs.__post_init__()
        report.formulas = fs

        # Images
        img_re = re.compile(r"\\includegraphics(?:\[.*?\])?\{([^}]+)\}")
        for i, line in enumerate(lines, 1):
            m = img_re.search(line)
            if m:
                filename = m.group(1)
                found = False
                actual = None
                wrong_ext = False
                if self.images_dir and self.images_dir.exists():
                    if (self.images_dir / filename).exists():
                        found = True
                        actual = str(self.images_dir / filename)
                    else:
                        # Try alternative extensions
                        # F43: alternatif bulunursa asset DISKTE VAR — eskiden
                        # actual doldurulup found=False birakiliyor, gorsel
                        # 'missing' sayilip skordan -10 yiyordu.
                        stem = Path(filename).stem
                        for ext in [".png", ".jpg", ".jpeg", ".pdf"]:
                            if (self.images_dir / (stem + ext)).exists():
                                actual = str(self.images_dir / (stem + ext))
                                found = True
                                wrong_ext = True
                                break
                report.images.append(
                    ImageInfo(filename, i, found, actual, wrong_ext)
                )

        report.images_found = sum(1 for img in report.images if img.found)
        report.images_missing = sum(1 for img in report.images if not img.found)
        report.images_wrong_ext = sum(1 for img in report.images if img.wrong_ext)

        # Headings
        # 2026-09-24: bir seviye iç içe parantez destekli — eski `(.+?)`
        # "\section{Analysis of \textit{k}-Means Clustering}" başlığını ilk
        # '}'te kesip raporda "\section{Analysis of \textit{k" gösteriyordu.
        heading_re = re.compile(
            r"\\(chapter|section|subsection|subsubsection|paragraph|subparagraph)(\*?)"
            r"\{((?:[^{}]|\{[^{}]*\})*)\}"
        )
        level_map = {
            "chapter": 1, "section": 2, "subsection": 3,
            "subsubsection": 4, "paragraph": 5, "subparagraph": 6,
        }
        for i, line in enumerate(lines, 1):
            m = heading_re.search(line)
            if m:
                cmd_name = m.group(1)
                star = m.group(2)
                title = m.group(3)
                report.headings.append(HeadingInfo(
                    level=level_map.get(cmd_name, 6),
                    command=cmd_name + star,
                    title=title,
                    line_num=i,
                ))

    # ------------------------------------------------------------------
    # Log analysis
    # ------------------------------------------------------------------

    def _analyze_log(self, log: str, report: QualityReport):
        """Analyze compilation log for errors, warnings, missing chars."""
        # Errors + missing characters: shared parser (R5) — -file-line-error
        # records, "!" records, '("E7)' TFM glyph codes, 79-col wrapped lines.
        errors, missing = parse_tex_log(log)
        report.errors.extend(errors)
        report.missing_chars.extend(missing)

        # Warnings (skip package info/version lines)
        seen_warnings = set()
        skip_patterns = ["providing info", "v1.", "v2.", "v3."]
        for line in log.split("\n"):
            stripped = line.strip()
            if "warning" in stripped.lower() and not stripped.startswith("!"):
                # Skip package version/info lines
                if any(p in stripped.lower() for p in skip_patterns):
                    continue
                # Normalize for dedup
                norm = re.sub(r"on input line \d+", "on input line N", stripped)
                if norm not in seen_warnings:
                    seen_warnings.add(norm)
                    report.warnings.append(CompileIssue("warning", stripped))

    # ------------------------------------------------------------------
    # PDF analysis
    # ------------------------------------------------------------------

    def _analyze_pdf(self, pdf_path: Path, report: QualityReport):
        """Analyze PDF for page count, content coverage, blank pages."""
        try:
            import fitz  # PyMuPDF
        except ImportError:
            return

        try:
            doc = fitz.open(str(pdf_path))
        except Exception:
            return

        report.pdf_pages = len(doc)
        formula_chars = set("σθ∈ℝ≤≥∞∑∫∂∇×÷±∓≈≡≠⊂⊃∪∩∀∃𝜎𝜃𝛼𝛽𝛾𝛿𝜀𝜂𝜆𝜇𝜈𝜋𝜌𝜏𝜙𝜓𝜔")

        for i, page in enumerate(doc):
            text = page.get_text()
            char_count = len(text.strip())
            has_formula = any(c in text for c in formula_chars)
            has_image = bool(page.get_images())
            is_blank = char_count < 50

            report.pages.append(PageInfo(
                page_num=i + 1,
                char_count=char_count,
                has_formula=has_formula,
                has_image=has_image,
                is_blank=is_blank,
            ))

        report.blank_pages = sum(1 for p in report.pages if p.is_blank)
        doc.close()

    # ------------------------------------------------------------------
    # Score calculation
    # ------------------------------------------------------------------

    def _calculate_score(self, report: QualityReport) -> float:
        """Calculate overall quality score (0-100)."""
        score = 100.0

        # Deductions
        # Errors: -5 each (max -40)
        score -= min(len(report.errors) * 5, 40)

        # Missing images: -10 each (max -30)
        score -= min(report.images_missing * 10, 30)

        # F43: yanlis uzantili referans (dosya diskte VAR) — 'missing' degil.
        # Yine de tex'teki referans duzeltilmeden derleme bu gorseli bulamaz,
        # o yuzden kucuk bir ceza: -3 each (max -15).
        score -= min(report.images_wrong_ext * 3, 15)

        # Missing characters: -2 each (max -10)
        score -= min(len(report.missing_chars) * 2, 10)

        # Blank pages: -5 each (max -20)
        score -= min(report.blank_pages * 5, 20)

        # No formulas when expected: -10
        if report.formulas.total_display == 0 and report.formulas.inline_math == 0:
            score -= 10

        # No pages: -50
        if report.pdf_pages == 0:
            score -= 50

        # Bonus: tagged equations
        if report.formulas.tagged_equations > 0:
            score = min(score + 3, 100)

        return max(score, 0)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    """CLI entry point."""
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m latex_pipeline.inspector <path/to/book.tex>")
        sys.exit(1)

    tex_path = Path(sys.argv[1])
    if not tex_path.exists():
        print(f"File not found: {tex_path}")
        sys.exit(1)

    # Auto-detect images dir
    images_dir = tex_path.parent / "images"
    inspector = QualityInspector(images_dir=images_dir)

    report = inspector.inspect(tex_path)
    report.print_summary()
    report.print_details()


if __name__ == "__main__":
    main()
