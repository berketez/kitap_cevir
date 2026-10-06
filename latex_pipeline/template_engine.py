"""
LaTeX template engine -- wraps converted content in a full XeLaTeX document.

Reads the preamble from ``templates/turkish_book.tex``, replaces placeholders
with values from PipelineConfig, and inserts the body content.

Supports inserting the original PDF cover page via the ``pdfpages`` package
when ``cover_pdf_path`` is provided.

Usage:
    engine = TemplateEngine(config)
    full_tex = engine.wrap_content(latex_body)

    # With original cover:
    full_tex = engine.wrap_content(
        latex_body,
        cover_pdf_path="/abs/path/to/original.pdf",
        cover_pages=[1],
    )
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import List, Optional, Sequence

from latex_pipeline.config import PipelineConfig
from latex_pipeline.kapak import kapak_karari

logger = logging.getLogger(__name__)

# Where templates live (same package directory)
_TEMPLATES_DIR = Path(__file__).parent / "templates"

# ---------------------------------------------------------------------------
# Özgün kapak kararı
# ---------------------------------------------------------------------------
# R1 (2026-09-25): 1. sayfa koşulsuz kapak yapılmaz, ölçülür. G3b
# (2026-10-05, Berke kararı): ölçü artık görsel ağırlık — görsel ağırlıklı
# 1. sayfa basılır, metin sayfası ya da boş sayfa basılmaz. Eşikler ve
# ölçüm gerekçesi tek yerde: latex_pipeline/kapak.py.

# ---------------------------------------------------------------------------
# Üst/alt bilgi sığdırma (R3, 2026-09-25)
# ---------------------------------------------------------------------------
# Ölçülen kenar boşluğu küçükse (bir test kitabında üst 12,8 mm) book sınıfının üst bilgisi
# (\headheight 12pt + \headsep 25pt) kâğıdın dışına taşar. Üst bilgi ile gövde
# arası daraltılır; yine sığmıyorsa üst bilgi boşluğun İÇİNE alınır.
_PT_TO_MM: float = 25.4 / 72.27          # TeX punto -> mm
_HEADHEIGHT_MM: float = 12 * _PT_TO_MM    # book.cls \headheight
_DEFAULT_HEADSEP_MM: float = 25 * _PT_TO_MM   # book.cls \headsep
_DEFAULT_FOOTSKIP_MM: float = 30 * _PT_TO_MM  # book.cls \footskip
# Üst/alt bilgi kâğıt kenarına bundan yakın olamaz.
_MIN_EDGE_MM: float = 5.0
# Daraltmada inilecek en küçük aralıklar.
_MIN_HEADSEP_MM: float = 3.0
_MIN_FOOTSKIP_MM: float = 6.0
_MM_VALUE_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*mm\s*$")


def _mm_value(text: str) -> Optional[float]:
    """'12.8mm' -> 12.8; başka birim ya da boş -> None."""
    m = _MM_VALUE_RE.match(text or "")
    return float(m.group(1)) if m else None


def head_foot_geometry(top_mm: float, bottom_mm: float) -> List[str]:
    """Küçük üst/alt boşlukta üst/alt bilgiyi sığdıran geometry seçenekleri.

    Boşluk varsayılan üst/alt bilgiye yetiyorsa boş liste döner (şablon
    değişmez).
    """
    opts: List[str] = []
    if top_mm < _HEADHEIGHT_MM + _DEFAULT_HEADSEP_MM + _MIN_EDGE_MM:
        headsep = top_mm - _HEADHEIGHT_MM - _MIN_EDGE_MM
        if headsep >= _MIN_HEADSEP_MM:
            opts.append(f"headsep={headsep:.1f}mm")
        else:
            opts.append(f"headsep={_MIN_HEADSEP_MM:.1f}mm")
            opts.append("includehead")
    if bottom_mm < _DEFAULT_FOOTSKIP_MM + _MIN_EDGE_MM:
        footskip = bottom_mm - _MIN_EDGE_MM
        if footskip >= _MIN_FOOTSKIP_MM:
            opts.append(f"footskip={footskip:.1f}mm")
        else:
            opts.append(f"footskip={_MIN_FOOTSKIP_MM:.1f}mm")
            opts.append("includefoot")
    return opts


def first_page_is_cover(pdf_path: str) -> Optional[bool]:
    """Özgün PDF'in 1. sayfası kapak olarak basılsın mı? Ölçülemezse None.

    Karar ``latex_pipeline.kapak.kapak_karari``'dadır (görsel ağırlık;
    eşikler ve ölçüm gerekçesi orada).
    """
    return kapak_karari(pdf_path).basilir


# F2-13 (2026-09-29): özgün kapak eklendiğinde yeniden kurulmuş başlık
# sayfası kalkar; ama YALNIZ gövdenin ön bölgesindeki (ilk bölümleme
# komutundan önceki) ve kod örneği içinde olmayan titlepage blokları.
# Eskiden belge genelinde her titlepage (LaTeX anlatan kitabın verbatim
# örnekleri, gövdenin ortasındaki başlık sayfaları dahil) siliniyordu.
_CODE_BLOCK_RE = re.compile(
    r"\\begin\{(verbatim\*?|lstlisting|minted|Verbatim|alltt)\}.*?\\end\{\1\}"
    r"|\\verb\*?([^\sA-Za-z*]).*?\2",
    re.DOTALL,
)
_FRONT_END_RE = re.compile(
    r"\\(?:mainmatter|tableofcontents|part|chapter|section)\b")
_TITLEPAGE_RE = re.compile(
    r"\\begin\{titlepage\}.*?\\end\{titlepage\}[ \t]*\n?\s*", re.DOTALL)


# ---------------------------------------------------------------------------
# Ön sayfalar roma rakamıyla (G3b, 2026-10-05; mt okuma MT-27)
# ---------------------------------------------------------------------------
# Dönüştürücü ön maddeden ana maddeye geçişte '\tableofcontents' +
# '\mainmatter' yazar, '\frontmatter' yazmaz (converter.py: kısa test
# çevirilerinde roma rakamı istenmediği için kaldırılmıştı). book sınıfında
# '\mainmatter' sayfa sayacını 1'e çeker: ön sayfalar (başlık sayfası,
# İçindekiler) arap rakamıyla 1, 2, ... numaralanıyor, ana madde yine 1'den
# başlıyordu. Belgede ana maddeye geçiş varsa ön sayfalar '\frontmatter' ile
# roma rakamı alır; geçiş yoksa (kısa çeviri, kesit) hiçbir şey değişmez.
# Geçişten ÖNCE numaralı bölüm varsa geçiş yanlış yerdedir (örnek:
# dönüştürücü ikinci '#' başlıkta geçti): '\frontmatter' o bölümlerin
# numarasını silerdi, eklenmez. O durumda sayfa numarasının yeniden
# başlamasını şablondaki '\mainmatter' koruması önler (turkish_book.tex).
_FRONTMATTER_BLOCK = (
    "% -- Ön sayfalar roma rakamıyla (belgede ana maddeye geçiş var) --\n"
    "\\frontmatter\n"
)
_MAINMATTER_RE = re.compile(r"\\mainmatter(?![A-Za-z@])")
_FRONTMATTER_RE = re.compile(r"\\frontmatter(?![A-Za-z@])")
_NUMBERED_HEADING_RE = re.compile(
    r"\\(?:part|chapter|section|subsection|subsubsection)(?![A-Za-z@*])")


def _in_comment(text: str, pos: int) -> bool:
    """*pos* satırında kendisinden önce kaçışsız '%' var mı (yorum içi)?"""
    i = text.rfind("\n", 0, pos) + 1
    while i < pos:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "%":
            return True
        i += 1
    return False


def _needs_frontmatter(content: str) -> bool:
    """Gövdede (kod/yorum dışı) '\\mainmatter' var, öncesinde
    '\\frontmatter' ve numaralı bölümleme komutu yok mu?"""
    code = [(m.start(), m.end()) for m in _CODE_BLOCK_RE.finditer(content)]

    def _live(m: "re.Match[str]") -> bool:
        pos = m.start()
        return (not any(s <= pos < e for s, e in code)
                and not _in_comment(content, pos))

    main = next((m for m in _MAINMATTER_RE.finditer(content) if _live(m)), None)
    if main is None:
        return False
    for rx in (_FRONTMATTER_RE, _NUMBERED_HEADING_RE):
        if any(_live(m) for m in rx.finditer(content, 0, main.start())):
            return False
    return True


def _drop_front_titlepage(content: str) -> str:
    """Gövdenin ön bölgesindeki (kod dışı) titlepage bloklarını kaldır."""
    code = [(m.start(), m.end()) for m in _CODE_BLOCK_RE.finditer(content)]

    def _in_code(pos: int) -> bool:
        return any(s <= pos < e for s, e in code)

    front_end = len(content)
    for m in _FRONT_END_RE.finditer(content):
        if not _in_code(m.start()):
            front_end = m.start()
            break
    drop = [m for m in _TITLEPAGE_RE.finditer(content, 0, front_end)
            if not _in_code(m.start())]
    for m in reversed(drop):
        content = content[:m.start()] + content[m.end():]
    if drop:
        logger.info("Reconstructed titlepage removed (original cover used, "
                    "%dx).", len(drop))
    return content


# ---------------------------------------------------------------------------
# Makine çevirisi künyesi (F3, 2026-09-30; Berke kararı)
# ---------------------------------------------------------------------------
# "Yerel model ile çevir" çıktısının başında küçük puntolu, görünür blok.
# Metin ui/branding.py'dedir (mt_notice_lines); burada yalnız dizgi ve yer.
# Künye TAM belgeye, şablon temizliğinden (Phase 6 sanitize) SONRA eklenir:
# çeviri gövdesinin (translated_body.tex) parçası değildir, kayıpsızlık / QC /
# hizalama denetimleri onu görmez; makine çevirisi temizlik kuralları metnine
# dokunamaz. Şablon (turkish_book.tex) değişmez: "PDF'den TeX çıkar"
# çıktısı birebir aynı kalır. Blokta bölümleme, matematik, görsel, dipnot
# yoktur (Phase 7 incelemesinin sayımları değişmez).
MT_NOTICE_MARK = "% -- Makine çevirisi künyesi (Yerel model ile çevir; kaynak metnin parçası değil) --"
_TEX_ESCAPES = {
    "\\": r"\textbackslash{}",
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
_BEGIN_DOCUMENT = r"\begin{document}"
# \begin{document}'i izleyen özgün kapak (_inject_cover'ın yazdığı satır);
# önünde şablon motorunun eklediği '\frontmatter' satırı olabilir (G3b).
_COVER_AFTER_BEGIN_RE = re.compile(
    r"\A\s*(?:%[^\n]*\n\s*)*(?:\\frontmatter(?![A-Za-z@])[^\n]*\n\s*)?"
    r"(?:%[^\n]*\n\s*)*"
    r"\\includepdf(?:\[[^\]\n]*\])?\{[^}\n]*\}[^\n]*(?:\n|\Z)")
# Kapak yoksa belge başındaki '\frontmatter' satırı (künye ön sayfalarda,
# roma rakamlı bölgede kalsın).
_FRONTMATTER_AFTER_BEGIN_RE = re.compile(
    r"\A\s*(?:%[^\n]*\n\s*)*\\frontmatter(?![A-Za-z@])[^\n]*(?:\n|\Z)")


def _tex_escape(text: str) -> str:
    """Düz metni TeX'e kaçışla (künye metni, model adı)."""
    return "".join(_TEX_ESCAPES.get(ch, ch) for ch in text)


def front_notice_block(lines: Sequence[str]) -> str:
    """Künye bloğu: ilk satır kalın başlık, sonrakiler paragraf (küçük punto)."""
    title, *rest = [str(line) for line in lines]
    parts = [r"\textbf{" + _tex_escape(title) + "}"]
    parts += [_tex_escape(line) for line in rest if line.strip()]
    inner = ("\\par\\medskip\n").join(parts)
    return (
        f"\n{MT_NOTICE_MARK}\n"
        "\\begingroup\\thispagestyle{empty}\n"
        "\\begin{center}\\small\n"
        "\\fbox{\\parbox{0.86\\linewidth}{\\raggedright\n"
        f"{inner}}}}}\n"          # \parbox ve \fbox kapanır
        "\\end{center}\n"
        "\\endgroup\n"
    )


def insert_front_notice(tex: str, lines: Sequence[str]) -> str:
    """Tam belgeye makine çevirisi künyesini ekle (bir kez).

    Yer: özgün kapak (``\\includepdf``) varsa onun arkasında; yoksa gövdenin
    ön bölgesindeki (ilk bölümleme komutundan önceki, kod dışı) başlık sayfası
    (``titlepage``) varsa onun arkasında; ikisi de yoksa belgenin en başında
    (ilk bölümden önce). Belgede ``\\begin{document}`` yoksa ya da künye zaten
    varsa belge aynen döner.
    """
    if not lines or MT_NOTICE_MARK in tex:
        return tex
    begin = tex.find(_BEGIN_DOCUMENT)
    if begin < 0:
        return tex
    start = begin + len(_BEGIN_DOCUMENT)
    body = tex[start:]
    cover = _COVER_AFTER_BEGIN_RE.match(body)
    if cover:
        offset = cover.end()
    else:
        front = _FRONTMATTER_AFTER_BEGIN_RE.match(body)
        offset = front.end() if front else 0
        code = [(m.start(), m.end()) for m in _CODE_BLOCK_RE.finditer(body)]

        def _in_code(pos: int) -> bool:
            return any(s <= pos < e for s, e in code)

        front_end = len(body)
        for m in _FRONT_END_RE.finditer(body):
            if not _in_code(m.start()):
                front_end = m.start()
                break
        for m in _TITLEPAGE_RE.finditer(body, 0, front_end):
            if not _in_code(m.start()):
                offset = m.end()
                break
    block = front_notice_block(lines)
    return tex[:start] + body[:offset] + block + body[offset:]


class TemplateEngine:
    """
    Combine a LaTeX preamble template with body content to produce
    a compilable ``.tex`` document.
    """

    def __init__(self, config: PipelineConfig):
        self.config = config
        self._template: str | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def wrap_content(
        self,
        content: str,
        config: PipelineConfig | None = None,
        cover_pdf_path: str | None = None,
        cover_pages: List[int] | None = None,
        extra_preamble: str | None = None,
    ) -> str:
        """
        Insert *content* into the XeLaTeX template and return the full
        ``.tex`` source as a string.

        Parameters
        ----------
        content        : LaTeX body text (everything between \\begin{document}
                         and \\end{document}).
        config         : optional override config; defaults to self.config.
        cover_pdf_path : absolute path to the original PDF.  When provided,
                         the cover page(s) are included via ``\\includepdf``
                         right after ``\\begin{document}`` and BEFORE the
                         body content (frontmatter etc.).  The ``pdfpages``
                         package is automatically injected into the preamble.
        cover_pages    : 1-based page numbers to include (default: [1]).
        extra_preamble : kitaba özel preamble ekleri (Yol A serbestliği,
                         2026-07-27) — ``\\begin{document}``'tan hemen önce
                         AYNEN eklenir. Kaynak: ``output/<slug>/
                         preamble_ozel.tex`` (kullanıcının kendi
                         ``\\usepackage`` / ``\\newcommand`` satırları).
        """
        cfg = config or self.config
        template = self._load_template()

        # Geometry bloku olustur
        if cfg.layout_detected and cfg.margin_top:
            geometry_parts = []
            if cfg.page_width and cfg.page_height:
                if cfg.paper_size not in ("a4paper", "letterpaper", "a5paper", "b5paper", "legalpaper"):
                    geometry_parts.append(f"  paperwidth={cfg.page_width}")
                    geometry_parts.append(f"  paperheight={cfg.page_height}")
            geometry_parts.append(f"  top={cfg.margin_top}")
            geometry_parts.append(f"  bottom={cfg.margin_bottom}")
            geometry_parts.append(f"  left={cfg.margin_left}")
            geometry_parts.append(f"  right={cfg.margin_right}")
            # (R3) Küçük ölçülen boşlukta üst/alt bilgi kâğıda sığsın.
            top_mm = _mm_value(cfg.margin_top)
            bottom_mm = _mm_value(cfg.margin_bottom)
            if top_mm is not None and bottom_mm is not None:
                for opt in head_foot_geometry(top_mm, bottom_mm):
                    geometry_parts.append(f"  {opt}")
            geometry_block = "\\geometry{\n" + ",\n".join(geometry_parts) + "\n}"
        else:
            geometry_block = "\\geometry{margin=2.5cm}"

        # Line spacing
        if hasattr(cfg, 'line_spacing') and cfg.line_spacing:
            line_spacing_cmd = f"\\linespread{{{cfg.line_spacing}}}"
        else:
            line_spacing_cmd = ""

        # Custom paper size: documentclass'a paper_size ekleme, geometry halleder
        effective_paper_size = cfg.paper_size if cfg.paper_size != "custom" else ""

        replacements = {
            "{{DOCUMENT_CLASS}}": cfg.document_class,
            "{{PAPER_SIZE}}": effective_paper_size,
            "{{FONT_SIZE}}": cfg.font_size,
            "{{FONT_MAIN}}": cfg.font_main,
            "{{FONT_SANS}}": cfg.font_sans,
            "{{FONT_MONO}}": cfg.font_mono,
            "{{FONT_MATH}}": cfg.font_math,
            "{{GEOMETRY_BLOCK}}": geometry_block,
            "{{LINE_SPACING}}": line_spacing_cmd,
        }

        # F2-13 (2026-09-29): önsöz düzenlemeleri (sınıf seçeneği temizliği,
        # kitaba özel preamble, kapak paketi) gövde YERLEŞMEDEN önce
        # şablonun kendi iskeletine yapılır: gövdedeki kod örneği
        # (verbatim/lstlisting içinde '\\begin{document}',
        # '\\documentclass[,..]') hiçbir koşulda değişmez.
        tex = template
        for placeholder, value in replacements.items():
            tex = tex.replace(placeholder, value)

        # Clean up empty paper_size in documentclass options (e.g. [,10pt,...] -> [10pt,...])
        tex = re.sub(r'\\documentclass\[\s*,', r'\\documentclass[', tex)

        # -- Kitaba özel preamble (Yol A serbestliği, 2026-07-27) -----------
        if extra_preamble and extra_preamble.strip():
            _block = (
                "\n% -- Kitaba özel preamble (preamble_ozel.tex) --\n"
                + extra_preamble.strip() + "\n\n"
            )
            tex = tex.replace(r"\begin{document}", _block + "\\begin{document}", 1)
            logger.info(
                "Custom preamble injected (%d chars).", len(extra_preamble)
            )

        # -- Inject cover page from original PDF if requested ---------------
        # Sayfa verilmediyse 1. sayfa ölçülür (kapak.py; G3b 2026-10-05,
        # Berke kararı): görsel ağırlıklıysa basılır; metin sayfası ya da boş
        # sayfaysa KONMAZ (içeriği çevrilmiş gövdede, Türkçe başlık sayfası
        # kalır). Ölçülemezse (PDF açılamıyor, PyMuPDF yok) eski davranış:
        # 1. sayfa kapak. Açık cover_pages çağıranın kararıdır.
        cover_used = False
        if cover_pdf_path and Path(cover_pdf_path).exists():
            if cover_pages is None:
                decision = kapak_karari(cover_pdf_path)
                if decision.basilir is False:
                    logger.info(
                        "Özgün kapak eklenmedi: %s 1. sayfası kapak değil (%s).",
                        cover_pdf_path, decision.neden,
                    )
                else:
                    logger.info(
                        "Özgün kapak eklendi: %s (%s).", cover_pdf_path,
                        decision.neden if decision.basilir else
                        decision.neden + "; ölçülemedi, 1. sayfa kapak sayıldı",
                    )
                    cover_used = True
            else:
                cover_used = True
            if cover_used:
                tex = self._inject_cover(tex, cover_pdf_path, cover_pages)
                # Özgün kapak yeniden kurulmuş başlık sayfasının yerini alır:
                # YALNIZ gövdenin ön bölgesindeki (ilk bölümleme komutundan
                # önceki) titlepage blokları kalkar; kod örnekleri kalır.
                content = _drop_front_titlepage(content)

        # G3b (MT-27): ana maddeye geçiş varsa ön sayfalar roma
        # rakamıyla — '\frontmatter' belgenin en başına, kapaktan ÖNCE
        # (kapak da ön sayfadır; arap rakamlı "1" ana maddeye kalır).
        if cfg.document_class == "book" and _needs_frontmatter(content):
            at = tex.rfind(_BEGIN_DOCUMENT, 0, tex.find("{{CONTENT}}"))
            if at >= 0:
                at += len(_BEGIN_DOCUMENT)
                tex = tex[:at] + "\n" + _FRONTMATTER_BLOCK + tex[at:]

        # Gövde en son, tek yerde yerleşir (yer tutucu şablonda bir kez).
        tex = tex.replace("{{CONTENT}}", content, 1)

        logger.info(
            "Template wrapped: class=%s, font=%s, %d chars body, cover=%s",
            cfg.document_class,
            cfg.font_main,
            len(content),
            "yes" if cover_used else "no",
        )
        return tex

    # ------------------------------------------------------------------
    # Cover injection
    # ------------------------------------------------------------------

    @staticmethod
    def _inject_cover(
        tex: str,
        cover_pdf_path: str,
        cover_pages: Optional[List[int]] = None,
    ) -> str:
        """
        Inject ``\\usepackage{pdfpages}`` into the preamble and
        ``\\includepdf`` right after ``\\begin{document}``.

        The cover page is inserted BEFORE any other body content
        (titlepage, frontmatter, etc.) so that it appears as the
        very first page(s) of the compiled PDF.

        Page numbering is suppressed for the cover page(s) so that
        the rest of the document's numbering is unaffected.
        """
        if cover_pages is None:
            cover_pages = [1]

        abs_path = str(Path(cover_pdf_path).resolve())
        pages_str = ",".join(str(p) for p in cover_pages)

        # 1. Add \usepackage{pdfpages} to preamble (before \begin{document})
        #    Insert it right before \begin{document} to avoid package ordering issues.
        #    F2-13: yalnız ÖNSÖZE bakılır ve yalnız şablonun kendi
        #    \begin{document}'i değişir (eskiden sayaçsız replace gövdedeki
        #    kod örneklerine de '\usepackage{pdfpages}' yazıyordu).
        begin = tex.find(r"\begin{document}")
        preamble = tex[:begin] if begin >= 0 else tex
        if not re.search(r"^[^%\n]*\\usepackage(?:\[[^\]]*\])?\{pdfpages\}",
                         preamble, re.MULTILINE):
            tex = tex.replace(
                r"\begin{document}",
                "\\usepackage{pdfpages}\n\n\\begin{document}",
                1,
            )

        # 2. Insert \includepdf right after \begin{document}
        #    Also suppress page numbering for cover to avoid "page 0" artifacts.
        cover_cmd = (
            f"\n% -- Original cover from source PDF (latex_pipeline/kapak.py) --\n"
            f"\\includepdf[pages={{{pages_str}}}]{{{abs_path}}}\n"
        )

        tex = tex.replace(
            r"\begin{document}",
            "\\begin{document}" + cover_cmd,
            1,  # only first occurrence
        )

        # 3. Yeniden kurulmuş \begin{titlepage} bloğu wrap_content'te,
        #    gövde yerleşmeden kaldırılır (_drop_front_titlepage): burada
        #    belge genelinde silmek gövdedeki kod örneklerini bozuyordu (F2-13).
        #    Tam belge (gövde yerleşmiş) verilirse aynı sınırlı kural uygulanır.
        head, sep, rest = tex.partition(cover_cmd)
        if sep:
            tex = head + sep + _drop_front_titlepage(rest)

        logger.info(
            "Cover injected: pages=%s, path=%s", pages_str, abs_path,
        )
        return tex

    # ------------------------------------------------------------------
    # Template loading
    # ------------------------------------------------------------------

    def _load_template(self) -> str:
        """Load the Turkish book template from disk (cached)."""
        if self._template is not None:
            return self._template

        template_path = _TEMPLATES_DIR / "turkish_book.tex"
        if not template_path.exists():
            logger.warning(
                "Template file not found at %s -- using built-in fallback.",
                template_path,
            )
            self._template = self._builtin_fallback()
            return self._template

        self._template = template_path.read_text(encoding="utf-8")
        logger.info("Loaded template: %s", template_path)
        return self._template

    # ------------------------------------------------------------------
    # Fallback (if template file is missing)
    # ------------------------------------------------------------------

    @staticmethod
    def _builtin_fallback() -> str:
        """Minimal XeLaTeX document as an emergency fallback."""
        return r"""\documentclass[a4paper,11pt]{book}
\usepackage{polyglossia}
\setmainlanguage{turkish}
\usepackage{fontspec}
\usepackage{amsmath,amssymb,mathtools}
\usepackage{graphicx}
\graphicspath{{images/}}
\usepackage{booktabs}
\usepackage{float}
\usepackage{hyperref}
\usepackage{geometry}
\geometry{margin=2.5cm}

\begin{document}

{{CONTENT}}

\end{document}
"""
