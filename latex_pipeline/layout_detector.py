"""
Layout detection from original PDF for faithful reproduction.

Analyzes the source PDF to extract page dimensions, margins, dominant font,
line spacing, and column layout.  The result is a ``LayoutInfo`` dataclass
that downstream components (TemplateEngine, PipelineConfig) can use to
reproduce the original book's appearance in the translated PDF.

Only dependency: PyMuPDF (``fitz``).  No other external packages.

Hedefler:
- Sayfa düzeni, kenar boşlukları, satır aralığı ve yazı tipleri özgünle aynı kalır.
- Yazı tipi küçültülmez; özgün boyut korunur.
- Çeviri, özgün PDF ile yan yana konunca aynı görünür.

Usage:
    detector = LayoutDetector()
    info = detector.detect("original_book.pdf", sample_pages=20)
    print(info.page_width_mm, info.dominant_font_size_pt)
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PT_TO_MM: float = 25.4 / 72.0  # 1 PDF point = 0.3528 mm

_STANDARD_PAPER_SIZES: Dict[str, Tuple[float, float]] = {
    "a4paper": (210.0, 297.0),
    "a5paper": (148.0, 210.0),
    "b5paper": (176.0, 250.0),
    "letterpaper": (215.9, 279.4),
    "legalpaper": (215.9, 355.6),
}

_PAPER_SIZE_TOLERANCE_MM: float = 2.0

# Map normalized PDF font names -> system/LaTeX font names.
# Keys are lowercase, spaces/hyphens/underscores stripped.
_FONT_FAMILY_MAP: Dict[str, str] = {
    "timesnewroman": "Times New Roman",
    "times": "Times New Roman",
    "arial": "Helvetica Neue",
    "helvetica": "Helvetica Neue",
    "courier": "Courier New",
    "couriernew": "Courier New",
    "georgia": "Georgia",
    "palatino": "Palatino",
    "garamond": "EB Garamond",
    "cambria": "Cambria",
    "calibri": "Helvetica Neue",
    "verdana": "Verdana",
    "cmr": "Latin Modern Roman",
    "cmbx": "Latin Modern Roman",
    "cmti": "Latin Modern Roman",
    "cmss": "Latin Modern Sans",
    "cmtt": "Latin Modern Mono",
    "lmroman": "Latin Modern Roman",
    "dejavuserif": "DejaVu Serif",
    "dejavusans": "DejaVu Sans",
    "roboto": "Roboto",
    "opensans": "Open Sans",
    "sourcesanspro": "Source Sans Pro",
    "arialunicodems": "Arial",
    "bookantiqua": "Palatino",
    "centuryschlbk": "Century Schoolbook",
    "lucidabright": "Lucida Bright",
    "baskerville": "Baskerville",
    "didot": "Didot",
    "bodoni": "Bodoni 72",
}

# Font category fallbacks (serif / sans / mono).
_CATEGORY_FALLBACK: Dict[str, str] = {
    "serif": "STIX Two Text",
    "sans": "Helvetica Neue",
    "mono": "Menlo",
}

_GLOBAL_FALLBACK: str = "STIX Two Text"

# Regex to strip the 6-char subset prefix that PDF fonts often carry,
# e.g. "AAAAAA+TimesNewRoman-Bold" -> "TimesNewRoman-Bold".
_SUBSET_PREFIX_RE = re.compile(r"^[A-Z]{6}\+")

# ---------------------------------------------------------------------------
# Kenar boşluğu ölçümü (R3, 2026-09-25)
# ---------------------------------------------------------------------------
# Eski yöntem sayfanın üst/alt %8'lik bandına DEĞEN her metin BLOĞUNU atıyor,
# sonra sayfa başına sınırların MEDYANINI alıyordu. İki sonuç:
#  - Gövde bloğu üst banda değiyorsa (bir test kitabında metin 12,7 mm'de başlıyor, bant
#    16,5 mm) bütün gövde atılıyor, kalan küçük bloklar 83 mm üst boşluk
#    veriyordu.
#  - Bölüm sonu / İçindekiler gibi kısa sayfalar medyana girip alt boşluğu
#    45–135 mm'ye çıkarıyordu.
# Yeni yöntem SATIR düzeyinde çalışır: yalnız sayfa süsü olduğu gösterilen
# satırlar (sayfa numarası, tekrar eden üst/alt bilgi, OCR kırpıntısı) atılır;
# üst/alt sınır "tam sayfaların ulaştığı" alt çeyrekten okunur.

# Üst/alt bilgi adayı: sayfanın üst/alt bu oranı içindeki satırlar.
_FURNITURE_ZONE_FRAC: float = 0.15
# Kenar başına en çok bu kadar süs satırı çıkarılır (başlık + çizgi altı).
_FURNITURE_MAX_ROWS: int = 2
# Başlık puntosu: gövdenin bu katından büyük satır başlıktır. Süs satırı
# başlık puntosunda olamaz; ilk satırı başlık olan sayfa (bölüm açılışı,
# İçindekiler: başlık aşağıdan başlar) üst boşluk ölçümüne girmez.
_HEADING_SIZE_RATIO: float = 1.1
# Ayrık üst bilgi: sonraki satırla taban çizgisi aralığı >= bu kat x satır aralığı.
_FURNITURE_SEPARATION_PITCH: float = 1.8
# Ayrık üst bilgi ayrıca kısa olmalı: sayfa metin genişliğinin bu oranından dar.
_FURNITURE_SHORT_WIDTH_FRAC: float = 0.6
# Ayrık üst bilgi gövdeden küçük puntoyla da tanınır (\\small başlık satırı).
_FURNITURE_SMALL_SIZE_RATIO: float = 0.95
# En fazla bu kadar görünür karakterli satır: OCR kırpıntısı / baskı imi.
_FURNITURE_TINY_CHARS: int = 3
# Tekrar eden üst/alt bilgi: en az bu kadar harfli parça, en az bu kadar sayfada.
_FURNITURE_REPEAT_MIN_LETTERS: int = 6
_FURNITURE_REPEAT_MIN_PAGES: int = 2
# Aynı satırdaki parçaları ayıran yatay boşluk (punto katı): sol/sağ üst bilgi.
_SEGMENT_GAP_SIZE_RATIO: float = 3.0
# Sayfa numarası / baskı imi: rakam (ya da Roma rakamı) + noktalama, kısa.
_FOLIO_MAX_CHARS: int = 15
_FOLIO_STRIP_RE = re.compile(r"[\s\-–—.*·|/()\[\]]+")
# Sayfa sınırına katkı için en az bu kadar gövde satırı (yalnız altyazılı
# şekil sayfası gibi seyrek sayfalar güvenilmez).
_MIN_BODY_ROWS_PER_PAGE: int = 3
# Üst/alt sınır: sayfa değerlerinin en kalabalık 1 mm'lik kovası (tam
# sayfaların ortak kenarı), kova en az bu kadar sayfa ve örneğin bu oranı
# kadar sayfa taşıyorsa; yoksa (küçük örnek) alt çeyrek.
_EDGE_MODE_BIN_MM: float = 1.0
_EDGE_MODE_MIN_PAGES: int = 3
_EDGE_MODE_MIN_FRAC: float = 0.2
_EDGE_QUANTILE: float = 0.25
# Kenar boşluğu alt sınırı ve sayfa boyutuna oranla üst sınırı (güvenlik ağı).
_MIN_MARGIN_MM: float = 5.0
_MAX_MARGIN_FRAC: float = 0.25
# Eski davranışla aynı varsayılan (hiç metin yoksa).
_DEFAULT_MARGIN_MM: float = 25.0


# ---------------------------------------------------------------------------
# Kenar boşluğu yardımcıları (satır düzeyi)
# ---------------------------------------------------------------------------

def _median(vals: List[float]) -> float:
    s = sorted(vals)
    n = len(s)
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2.0


def _lower_quantile(vals: List[float], q: float) -> float:
    """Aşağı yuvarlanan sıra ile nicelik: küçük örnekte en küçüğe iner."""
    s = sorted(vals)
    return s[int(q * (len(s) - 1))]


def _edge_estimate(vals: List[float]) -> float:
    """Metin bloğu kenarı: tam sayfaların paylaştığı değer.

    Kısa sayfalar (bölüm sonu, İçindekiler) kenardan uzakta dağınık değer
    verir; tam sayfalar aynı değerde toplanır. En kalabalık kova yeterince
    destekliyse onun medyanı, değilse alt çeyrek döner.
    """
    bins: Counter = Counter(round(v / _EDGE_MODE_BIN_MM) for v in vals)
    best_bin, count = max(bins.items(), key=lambda kv: (kv[1], -kv[0]))
    need = max(_EDGE_MODE_MIN_PAGES,
               math.ceil(_EDGE_MODE_MIN_FRAC * len(vals)))
    if count >= need:
        return _median([v for v in vals
                        if round(v / _EDGE_MODE_BIN_MM) == best_bin])
    return _lower_quantile(vals, _EDGE_QUANTILE)


def _letters_key(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalpha())


def _page_rows(page) -> List[dict]:
    """Boş olmayan metin satırlarını görsel satırlara topla (y sırasıyla).

    Dikey merkezleri birbirine yarım satır yüksekliğinden yakın satırlar aynı
    görsel satırdır (OCR'lı taramada her sözcük ayrı satır gelir; iki sütun
    ya da sol/sağ üst bilgi aynı satıra düşer).
    """
    import fitz  # PyMuPDF; detect() yüklü olduğunu zaten doğruladı

    page_h = page.rect.height
    lines = []
    # Yalnız sayfa kutusu içindeki metin (kenar damgaları, taşan nesneler
    # dışarıda); görsel verisi istenmez.
    text_dict = page.get_text("dict", flags=fitz.TEXT_MEDIABOX_CLIP)
    for block in text_dict.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = [s for s in line.get("spans", [])
                     if s.get("text", "").strip()]
            if not spans:
                continue
            x0, y0, x1, y1 = line.get("bbox", (0.0, 0.0, 0.0, 0.0))
            if y1 <= y0 or x1 <= x0 or y1 <= 0 or y0 >= page_h:
                continue
            text = "".join(s.get("text", "") for s in spans).strip()
            lines.append({
                "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                "size": max(s.get("size", 0.0) for s in spans),
                "text": text,
                "chars": sum(1 for ch in text if not ch.isspace()),
            })

    lines.sort(key=lambda ln: (ln["y0"] + ln["y1"]) / 2.0)
    rows: List[dict] = []
    for ln in lines:
        centre = (ln["y0"] + ln["y1"]) / 2.0
        if rows:
            row = rows[-1]
            row_centre = (row["y0"] + row["y1"]) / 2.0
            limit = 0.5 * min(ln["y1"] - ln["y0"], row["y1"] - row["y0"])
            if abs(centre - row_centre) <= limit:
                row["lines"].append(ln)
                row["y0"] = min(row["y0"], ln["y0"])
                row["y1"] = max(row["y1"], ln["y1"])
                row["x0"] = min(row["x0"], ln["x0"])
                row["x1"] = max(row["x1"], ln["x1"])
                row["size"] = max(row["size"], ln["size"])
                row["chars"] += ln["chars"]
                continue
        rows.append({
            "lines": [ln], "y0": ln["y0"], "y1": ln["y1"],
            "x0": ln["x0"], "x1": ln["x1"], "size": ln["size"],
            "chars": ln["chars"],
        })

    for row in rows:
        row["lines"].sort(key=lambda ln: ln["x0"])
        row["text"] = " ".join(ln["text"] for ln in row["lines"])
        # Büyük yatay boşlukla ayrılan parçalar (sol/sağ üst bilgi).
        segments: List[List[str]] = [[]]
        prev_x1 = None
        gap_limit = _SEGMENT_GAP_SIZE_RATIO * max(row["size"], 1.0)
        for ln in row["lines"]:
            if prev_x1 is not None and ln["x0"] - prev_x1 > gap_limit:
                segments.append([])
            segments[-1].append(ln["text"])
            prev_x1 = ln["x1"] if prev_x1 is None else max(prev_x1, ln["x1"])
        row["segments"] = sum(1 for seg in segments if seg)
        row["main_size"] = max(row["lines"], key=lambda ln: ln["chars"])["size"]
        row["segment_keys"] = {
            key for key in (_letters_key(" ".join(seg)) for seg in segments)
            if len(key) >= _FURNITURE_REPEAT_MIN_LETTERS
        }
    return rows


def _body_font_size(page_rows) -> float:
    """Karakter ağırlıklı medyan punto (gövde)."""
    weights: Counter = Counter()
    for _w, _h, rows in page_rows:
        for row in rows:
            for ln in row["lines"]:
                if ln["size"] > 0 and ln["chars"] > 0:
                    weights[round(ln["size"], 1)] += ln["chars"]
    if not weights:
        return 10.0
    total = sum(weights.values())
    cumulative = 0
    for size, weight in sorted(weights.items()):
        cumulative += weight
        if cumulative >= total / 2.0:
            return size
    return 10.0


def _row_pitch(page_rows, body_size: float) -> float:
    """Gövde satır aralığı (pt): ardışık satır tepeleri farkının medyanı."""
    diffs: List[float] = []
    for _w, _h, rows in page_rows:
        for a, b in zip(rows, rows[1:]):
            d = b["y0"] - a["y0"]
            if 0.8 * body_size <= d <= 2.5 * body_size:
                diffs.append(d)
    if not diffs:
        return 1.2 * body_size
    return _median(diffs)


def _zone_segment_counts(page_rows) -> Tuple[Counter, Counter]:
    """Üst/alt bölgedeki satır parçalarının kaç FARKLI sayfada geçtiği."""
    top_counts: Counter = Counter()
    bottom_counts: Counter = Counter()
    for _w, page_h, rows in page_rows:
        top_keys, bottom_keys = set(), set()
        for row in rows[:_FURNITURE_MAX_ROWS]:
            if row["y1"] <= page_h * _FURNITURE_ZONE_FRAC:
                top_keys |= row["segment_keys"]
        for row in rows[-_FURNITURE_MAX_ROWS:]:
            if row["y0"] >= page_h * (1.0 - _FURNITURE_ZONE_FRAC):
                bottom_keys |= row["segment_keys"]
        top_counts.update(top_keys)
        bottom_counts.update(bottom_keys)
    return top_counts, bottom_counts


_DECIMAL_RE = re.compile(r"\d[.,]\d")
_ROMAN_NUMERAL_RE = re.compile(
    r"^m{0,3}(cm|cd|d?c{0,3})(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})$")


def _is_folio(text: str) -> bool:
    """Sayfa numarası / baskı imi: '22', '— 12 —', '3* 35', '2-927', 'xiv'."""
    compact = "".join(text.split())
    if not compact or len(compact) > _FOLIO_MAX_CHARS:
        return False
    if _DECIMAL_RE.search(text):
        return False  # '(3.9)', '2,5': denklem numarası / ondalık sayı
    core = _FOLIO_STRIP_RE.sub("", text)
    if not core:
        return False
    if core.isdigit():
        return True
    # Roma rakamlı sayfa numarası küçük harfle ve en az iki harfle kabul
    # edilir ('ii', 'xiv'): tek büyük 'L', 'C', 'I' şekil etiketidir.
    return (len(core) >= 2 and core.islower()
            and bool(_ROMAN_NUMERAL_RE.match(core)))


def _has_folio_edge(text: str) -> bool:
    """Satırın ilk ya da son sözcüğü sayfa numarası mı ('5 Başlık', 'Başlık 5')."""
    words = text.split()
    if len(words) < 2:
        return False
    return _is_folio(words[0]) or _is_folio(words[-1])


_NOISE = "noise"    # sayfa numarası / sekme numarası / OCR kırpıntısı
_HEADER = "header"  # üst ya da alt bilgi satırı


def _furniture_kind(row, neighbour, page_h: float, body_size: float,
                    pitch: float, text_width: float, counts: Counter,
                    top: bool, after_header: bool) -> Optional[str]:
    """*row* (sayfanın en üst ya da en alt satırı) sayfa süsü mü?

    Dönüş: ``_NOISE``, ``_HEADER`` ya da None (gövde).
    *neighbour*: üstte bir sonraki, altta bir önceki satır (yoksa None).
    *after_header*: bu kenarda bir üst/alt bilgi satırı zaten atıldı mı.
    Atıldıysa sıradaki satır yalnız sayfa numarası ya da tekrar eden bilgi
    ise atılır; kırpıntı ve ayrık-satır kuralları uygulanmaz (üst bilginin
    altındaki ilk gövde satırı 'T1', 'L' gibi bir şekil etiketi olabilir).
    """
    if top:
        if row["y1"] > page_h * _FURNITURE_ZONE_FRAC:
            return None
    elif row["y0"] < page_h * (1.0 - _FURNITURE_ZONE_FRAC):
        return None
    # Punto bakmadan: sayfa numarası / sekme numarası, OCR kırpıntısı.
    if _is_folio(row["text"]):
        return _NOISE
    if not after_header and row["chars"] <= _FURNITURE_TINY_CHARS:
        return _NOISE
    if row["size"] > body_size * _HEADING_SIZE_RATIO:
        return None  # bölüm başlığı: gövdenin parçası
    if any(counts.get(key, 0) >= _FURNITURE_REPEAT_MIN_PAGES
           for key in row["segment_keys"]):
        return _HEADER
    if neighbour is None or after_header:
        return None
    distance = (neighbour["y0"] - row["y0"]) if top else (row["y0"] - neighbour["y0"])
    if distance < _FURNITURE_SEPARATION_PITCH * pitch:
        return None
    # Gövdeden ayrık satır: iki parçalı (sol/sağ başlık) satır üst/alt
    # bilgidir. Bir ucunda sayfa numarası olan, kısa ya da gövdeden küçük
    # puntolu ayrık satır YALNIZ üstte süs sayılır: altta aynı biçim tek
    # satırlık dipnottur ('1 Not metni', '... s. 45.'), metin bloğunun
    # parçasıdır.
    if row["segments"] >= 2:
        return _HEADER
    if not top:
        return None
    if _has_folio_edge(row["text"]):
        return _HEADER
    if (row["x1"] - row["x0"]) <= _FURNITURE_SHORT_WIDTH_FRAC * text_width:
        return _HEADER
    if row["main_size"] <= body_size * _FURNITURE_SMALL_SIZE_RATIO:
        return _HEADER
    return None


def _strip_furniture(rows, page_h: float, body_size: float, pitch: float,
                     top_counts: Counter, bottom_counts: Counter) -> List[dict]:
    """Sayfanın başındaki ve sonundaki süs satırlarını at (kenar başına <= 2)."""
    body = list(rows)
    if not body:
        return body
    text_width = max(r["x1"] for r in body) - min(r["x0"] for r in body)
    for top in (True, False):
        counts = top_counts if top else bottom_counts
        removed, after_header = 0, False
        while body and removed < _FURNITURE_MAX_ROWS:
            row = body[0] if top else body[-1]
            if len(body) > 1:
                neighbour = body[1] if top else body[-2]
            else:
                neighbour = None
            kind = _furniture_kind(row, neighbour, page_h, body_size, pitch,
                                   text_width, counts, top, after_header)
            if kind is None:
                break
            after_header = after_header or kind == _HEADER
            body.pop(0 if top else -1)
            removed += 1
    return body


# ---------------------------------------------------------------------------
# LayoutInfo dataclass
# ---------------------------------------------------------------------------

@dataclass
class LayoutInfo:
    """Detected layout properties of a PDF document."""

    # Page dimensions (mm)
    page_width_mm: float = 210.0   # A4 default
    page_height_mm: float = 297.0

    # Margins (mm)
    margin_top_mm: float = 25.0
    margin_bottom_mm: float = 25.0
    margin_left_mm: float = 25.0
    margin_right_mm: float = 25.0

    # Font
    dominant_font_name: str = ""       # Original PDF font name (cleaned)
    dominant_font_size_pt: float = 11.0
    mapped_font_name: str = ""         # System font for XeLaTeX

    # Line spacing
    line_spacing_ratio: float = 1.0

    # Layout type
    is_two_column: bool = False

    # Meta
    sample_pages: int = 0
    confidence: float = 0.0
    paper_size_name: str = ""  # "a4paper", "letterpaper", "" (custom)


# ---------------------------------------------------------------------------
# LayoutDetector
# ---------------------------------------------------------------------------

class LayoutDetector:
    """
    Analyze a PDF to extract layout information for faithful reproduction.

    All analysis uses PyMuPDF (fitz) only.  If PyMuPDF is not installed or
    the PDF cannot be opened, a default ``LayoutInfo`` is returned instead
    of raising an exception -- the pipeline should never crash because of
    layout detection.
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def detect(self, pdf_path: str, sample_pages: int = 20) -> LayoutInfo:
        """
        Detect layout properties from *pdf_path*.

        Parameters
        ----------
        pdf_path      : Path to the source PDF.
        sample_pages  : Maximum number of body pages to sample.

        Returns
        -------
        LayoutInfo with detected (or default) values.
        """
        info = LayoutInfo()

        # -- Guard: import fitz -------------------------------------------
        try:
            import fitz  # PyMuPDF
        except ImportError:
            logger.warning(
                "PyMuPDF (fitz) not installed -- returning default LayoutInfo."
            )
            info.paper_size_name = "a4paper"
            return info

        # -- Guard: open PDF ----------------------------------------------
        pdf = Path(pdf_path)
        if not pdf.exists():
            logger.warning(
                "PDF not found: %s -- returning default LayoutInfo.", pdf_path
            )
            info.paper_size_name = "a4paper"
            return info

        try:
            doc = fitz.open(str(pdf))
        except Exception as exc:
            logger.warning(
                "Cannot open PDF %s (%s) -- returning default LayoutInfo.",
                pdf_path,
                exc,
            )
            info.paper_size_name = "a4paper"
            return info

        if doc.page_count == 0:
            logger.warning("PDF has 0 pages -- returning default LayoutInfo.")
            doc.close()
            info.paper_size_name = "a4paper"
            return info

        # -- Select sample pages ------------------------------------------
        page_indices = self._select_sample_pages(doc.page_count, sample_pages)
        pages = [doc[i] for i in page_indices]
        info.sample_pages = len(pages)

        # -- Detect each property -----------------------------------------
        w_mm, h_mm = self._detect_page_size(doc)
        info.page_width_mm = w_mm
        info.page_height_mm = h_mm
        info.paper_size_name = self._match_standard_paper_size(w_mm, h_mm)

        mt, mb, ml, mr = self._detect_margins(doc, pages, w_mm, h_mm)
        info.margin_top_mm = mt
        info.margin_bottom_mm = mb
        info.margin_left_mm = ml
        info.margin_right_mm = mr

        font_name, font_size = self._detect_dominant_font(doc, pages)
        info.dominant_font_name = font_name
        info.dominant_font_size_pt = font_size
        info.mapped_font_name = self._map_font_to_system(font_name)

        info.line_spacing_ratio = self._detect_line_spacing(doc, pages, font_size)
        info.is_two_column = self._detect_columns(doc, pages)

        # -- Confidence heuristic -----------------------------------------
        # More sampled pages and more detected properties = higher confidence.
        conf_factors = []
        if info.sample_pages >= 10:
            conf_factors.append(1.0)
        elif info.sample_pages >= 5:
            conf_factors.append(0.8)
        else:
            conf_factors.append(0.5)

        if font_name:
            conf_factors.append(1.0)
        else:
            conf_factors.append(0.3)

        if info.paper_size_name:
            conf_factors.append(1.0)
        else:
            conf_factors.append(0.7)

        info.confidence = round(sum(conf_factors) / len(conf_factors), 2)

        doc.close()

        logger.info(
            "Layout detected: %.1fx%.1fmm, margins=%.1f/%.1f/%.1f/%.1fmm, "
            "font=%s %.1fpt, confidence=%.2f",
            info.page_width_mm,
            info.page_height_mm,
            info.margin_top_mm,
            info.margin_bottom_mm,
            info.margin_left_mm,
            info.margin_right_mm,
            info.mapped_font_name or info.dominant_font_name or "(unknown)",
            info.dominant_font_size_pt,
            info.confidence,
        )

        return info

    # ------------------------------------------------------------------
    # Sampling strategy
    # ------------------------------------------------------------------

    @staticmethod
    def _select_sample_pages(
        total_pages: int, max_samples: int
    ) -> List[int]:
        """
        Select body page indices, skipping cover, TOC (first 10%),
        and back matter (last 10%).

        Returns a list of 0-based page indices.
        """
        if total_pages <= 3:
            # Very short PDF -- use all pages except first (cover).
            return list(range(1, total_pages))

        start = max(1, int(total_pages * 0.1))
        end = max(start + 1, int(total_pages * 0.9))

        body_range = list(range(start, end))

        if len(body_range) <= max_samples:
            return body_range

        # Evenly spaced sampling.
        step = len(body_range) / max_samples
        selected = []
        for i in range(max_samples):
            idx = int(i * step)
            selected.append(body_range[idx])
        return selected

    # ------------------------------------------------------------------
    # Page size detection
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_page_size(doc) -> Tuple[float, float]:
        """
        Detect page dimensions in mm from the first non-cover page.

        Uses page.rect (in PDF points) and converts to mm.
        Falls back to page 0 if only one page exists.
        """
        page_idx = min(1, doc.page_count - 1)
        page = doc[page_idx]
        rect = page.rect  # fitz.Rect -- x0, y0, x1, y1 in points

        w_pt = rect.width
        h_pt = rect.height

        w_mm = round(w_pt * PT_TO_MM, 1)
        h_mm = round(h_pt * PT_TO_MM, 1)

        return w_mm, h_mm

    # ------------------------------------------------------------------
    # Margin detection
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_margins(
        doc, pages, page_w_mm: float, page_h_mm: float
    ) -> Tuple[float, float, float, float]:
        """
        Estimate the text-block margins (mm) of the sampled pages.

        Satır düzeyinde (R3, 2026-09-25):

        1. Her sayfanın metin satırları görsel satırlara (row) toplanır
           (OCR'lı taramada her sözcük ayrı "satır" gelir).
        2. Sayfanın en üst / en alt satırı YALNIZ sayfa süsü olduğu
           gösterilirse atılır (kenar başına en çok iki satır; ayrıntı
           ``_furniture_kind``): sayfa/sekme numarası, baskı imi, <= 3
           karakterlik OCR kırpıntısı, birden çok sayfada tekrar eden
           üst/alt bilgi, gövdeden ayrık üst/alt bilgi satırı. Gövdenin
           kendisi bir banda değdi diye ATILMAZ.
        3. Üst/alt boşluk: tam sayfaların ortak kenarı (en kalabalık 1 mm'lik
           kova; küçük örnekte alt çeyrek). Bölüm sonu, İçindekiler gibi
           kısa sayfalar bu değeri büyütemez. İlk satırı başlık puntosunda
           olan sayfalar (bölüm açılışı) başka sayfa varken üst ölçüme
           girmez.
        4. Sol: medyan (eskisi gibi); sağ: sayfa genişliği - sol - metin
           genişliğinin medyanı.
        """
        page_rows = []
        for page in pages:
            rect = page.rect
            rows = _page_rows(page)
            if rows:
                page_rows.append((rect.width, rect.height, rows))

        if not page_rows:
            # No text found -- return defaults.
            return (_DEFAULT_MARGIN_MM, _DEFAULT_MARGIN_MM,
                    _DEFAULT_MARGIN_MM, _DEFAULT_MARGIN_MM)

        body_size = _body_font_size(page_rows)
        pitch = _row_pitch(page_rows, body_size)
        top_counts, bottom_counts = _zone_segment_counts(page_rows)

        extents = []
        for page_w_pt, page_h_pt, rows in page_rows:
            body = _strip_furniture(
                rows, page_h_pt, body_size, pitch, top_counts, bottom_counts,
            )
            if body:
                extents.append((page_w_pt, page_h_pt, body))

        usable = [e for e in extents if len(e[2]) >= _MIN_BODY_ROWS_PER_PAGE]
        if not usable:
            usable = extents
        if not usable:
            return (_DEFAULT_MARGIN_MM, _DEFAULT_MARGIN_MM,
                    _DEFAULT_MARGIN_MM, _DEFAULT_MARGIN_MM)

        tops: List[float] = []
        heading_tops: List[float] = []
        bottoms: List[float] = []
        lefts: List[float] = []
        widths: List[float] = []
        for page_w_pt, page_h_pt, body in usable:
            top_mm = min(r["y0"] for r in body) * PT_TO_MM
            if body[0]["size"] > body_size * _HEADING_SIZE_RATIO:
                heading_tops.append(top_mm)
            else:
                tops.append(top_mm)
            bottoms.append((page_h_pt - max(r["y1"] for r in body)) * PT_TO_MM)
            x0 = min(r["x0"] for r in body)
            lefts.append(x0 * PT_TO_MM)
            widths.append((max(r["x1"] for r in body) - x0) * PT_TO_MM)

        def _clamp(value: float, dimension_mm: float) -> float:
            upper = max(_MIN_MARGIN_MM, dimension_mm * _MAX_MARGIN_FRAC)
            return round(min(max(_MIN_MARGIN_MM, value), upper), 1)

        mt = _clamp(_edge_estimate(tops or heading_tops), page_h_mm)
        mb = _clamp(_edge_estimate(bottoms), page_h_mm)
        ml = _clamp(_median(lefts), page_w_mm)
        # Sağ boşluk metin GENİŞLİĞİNDEN: karşılıklı (tek/çift sayfa) düzende
        # sol ve sağ medyanları ayrı kümelerden gelip metin genişliğini
        # bozabiliyordu; genişliğin medyanı iki düzende de aynıdır.
        mr = _clamp(page_w_mm - ml - _median(widths), page_w_mm)

        return mt, mb, ml, mr

    # ------------------------------------------------------------------
    # Dominant font detection
    # ------------------------------------------------------------------

    def _detect_dominant_font(
        self, doc, pages
    ) -> Tuple[str, float]:
        """
        Find the most common (font_name, font_size) pair across sampled
        pages, weighted by character count.

        - Subset prefixes ("AAAAAA+") are stripped.
        - Heading fonts (size > 1.3x median body size) are excluded.
        - If all sizes are 0, returns ("", 11.0).
        """
        # Phase 1: Collect all (font, size) -> char_count.
        raw_counter: Counter = Counter()

        for page in pages:
            text_dict = page.get_text("dict", flags=0)
            for block in text_dict.get("blocks", []):
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        font = span.get("font", "")
                        size = span.get("size", 0.0)
                        text = span.get("text", "")
                        char_count = len(text)

                        if char_count == 0 or size <= 0:
                            continue

                        # Strip subset prefix.
                        font = _SUBSET_PREFIX_RE.sub("", font)

                        raw_counter[(font, round(size, 1))] += char_count

        if not raw_counter:
            return ("", 11.0)

        # Phase 2: Compute median font size (weighted).
        # Build a sorted list of (size, weight) to find weighted median.
        size_weights: Counter = Counter()
        for (_, size), count in raw_counter.items():
            size_weights[size] += count

        total_chars = sum(size_weights.values())
        sorted_sizes = sorted(size_weights.items())
        cumulative = 0
        median_size = sorted_sizes[0][0]
        for size, weight in sorted_sizes:
            cumulative += weight
            if cumulative >= total_chars / 2:
                median_size = size
                break

        # Phase 3: Filter out heading fonts (> 1.3x median).
        heading_threshold = median_size * 1.3
        body_counter: Counter = Counter()
        for (font, size), count in raw_counter.items():
            if size <= heading_threshold:
                body_counter[(font, size)] += count

        if not body_counter:
            # All text was "headings" -- fall back to raw.
            body_counter = raw_counter

        # Phase 4: Most common body font.
        (best_font, best_size), _ = body_counter.most_common(1)[0]

        if best_size <= 0:
            best_size = 11.0

        return (best_font, best_size)

    # ------------------------------------------------------------------
    # Line spacing detection
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_line_spacing(
        doc, pages, font_size_pt: float
    ) -> float:
        """
        Estimate line spacing ratio = (line gap) / font_size.

        Computes the y-distance between consecutive baselines of text
        lines within the same block, then takes the median ratio.
        """
        if font_size_pt <= 0:
            return 1.0

        ratios: List[float] = []

        for page in pages:
            text_dict = page.get_text("dict", flags=0)
            for block in text_dict.get("blocks", []):
                if block.get("type") != 0:
                    continue
                lines = block.get("lines", [])
                if len(lines) < 2:
                    continue

                prev_y = None
                for line in lines:
                    # Use the first span's origin as the baseline y.
                    spans = line.get("spans", [])
                    if not spans:
                        continue
                    origin = spans[0].get("origin")
                    if origin is None:
                        # Fallback: use bbox top.
                        y = line.get("bbox", [0, 0, 0, 0])[1]
                    else:
                        y = origin[1]

                    if prev_y is not None:
                        gap = y - prev_y
                        if gap > 0:
                            ratio = gap / font_size_pt
                            # Filter out abnormal ratios (e.g. paragraph breaks).
                            if 0.5 <= ratio <= 4.0:
                                ratios.append(ratio)
                    prev_y = y

        if not ratios:
            return 1.0

        ratios.sort()
        n = len(ratios)
        if n % 2 == 1:
            median_ratio = ratios[n // 2]
        else:
            median_ratio = (ratios[n // 2 - 1] + ratios[n // 2]) / 2.0

        # KAVRAMSAL FIX (2026-06-22): olculen median_ratio = gap/font_size'dir.
        # AMA LaTeX'in varsayilan \baselineskip zaten font'un ~1.2 katidir
        # (10pt->12pt). Tek-aralikli bir kitap dogal olarak gap/font~=1.2 verir;
        # bunu dogrudan \linespread'e koymak 1.2 ile CARPAR (1.44x = %20 fazla
        # gevsek). Olculen orani LaTeX baselineskip faktorune (1.2) bolerek
        # gercek \linespread carpanina cevir: orijinal 1.20 -> linespread 1.0.
        result = round(median_ratio / 1.2, 2)
        # font-size misdetection guvencesi (orn. 6pt -> oran ~2.0 sismis):
        if result > 1.5:
            return 1.1
        return max(1.0, min(result, 1.3))

    # ------------------------------------------------------------------
    # Column detection
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_columns(doc, pages) -> bool:
        """
        Detect two-column layout by analyzing the x-distribution of text
        blocks.  If blocks cluster around two distinct x-centers
        (bimodal), flag as two-column.
        """
        if not pages:
            return False

        # Collect x-center of each text block.
        x_centers: List[float] = []
        page_widths: List[float] = []

        for page in pages:
            rect = page.rect
            page_w = rect.width
            page_widths.append(page_w)

            blocks = page.get_text("blocks")
            for b in blocks:
                if b[6] != 0:
                    continue
                x0, x1 = b[0], b[2]
                width = x1 - x0
                # Skip full-width blocks (titles, headers).
                if width > page_w * 0.7:
                    continue
                center = (x0 + x1) / 2.0
                x_centers.append(center / page_w)  # Normalize to [0, 1].

        if len(x_centers) < 10:
            return False

        # Simple heuristic: count blocks in left half vs right half.
        left_count = sum(1 for x in x_centers if x < 0.45)
        right_count = sum(1 for x in x_centers if x > 0.55)
        middle_count = sum(1 for x in x_centers if 0.45 <= x <= 0.55)

        total = left_count + right_count + middle_count
        if total == 0:
            return False

        # Two-column: significant blocks on both sides, few in the middle.
        both_sides = (
            left_count >= total * 0.25
            and right_count >= total * 0.25
            and middle_count <= total * 0.3
        )

        return both_sides

    # ------------------------------------------------------------------
    # Font mapping
    # ------------------------------------------------------------------

    def _map_font_to_system(self, pdf_font_name: str) -> str:
        """
        Map a PDF font name to a system font usable by XeLaTeX/fontspec.

        4-layer strategy:
        1. Direct match after normalizing the name.
        2. Family table lookup (~30 known fonts).
        3. Category-based fallback (serif / sans / mono).
        4. Global fallback: STIX Two Text.
        """
        if not pdf_font_name:
            logger.info("Font mapped: (empty) -> %s", _GLOBAL_FALLBACK)
            return _GLOBAL_FALLBACK

        # Normalize: strip subset prefix, style suffixes, lowercase.
        cleaned = _SUBSET_PREFIX_RE.sub("", pdf_font_name)
        # Remove style suffixes like -Bold, -Italic, -BoldItalic, -Regular.
        cleaned = re.sub(r"[-,](Bold|Italic|Regular|Oblique|Light|Medium|Semibold|Book|Roman|Demi|Heavy|Black|Thin|ExtraLight|Condensed|BoldItalic|BoldOblique)+", "", cleaned, flags=re.IGNORECASE)
        normalized = re.sub(r"[\s\-_]", "", cleaned).lower()

        # Layer 1: Exact match in the family map.
        if normalized in _FONT_FAMILY_MAP:
            mapped = _FONT_FAMILY_MAP[normalized]
            logger.info("Font mapped: %s -> %s", pdf_font_name, mapped)
            return mapped

        # Layer 2: Substring / prefix match in family map.
        for key, value in _FONT_FAMILY_MAP.items():
            if normalized.startswith(key) or key.startswith(normalized):
                logger.info("Font mapped: %s -> %s", pdf_font_name, value)
                return value

        # Layer 3: Category-based fallback.
        category = self._guess_font_category(normalized)
        if category in _CATEGORY_FALLBACK:
            mapped = _CATEGORY_FALLBACK[category]
            logger.info(
                "Font not mapped, using category fallback: %s -> %s (category=%s)",
                pdf_font_name, mapped, category,
            )
            return mapped

        # Layer 4: Global fallback.
        logger.info(
            "Font not mapped, using fallback: %s -> %s",
            pdf_font_name, _GLOBAL_FALLBACK,
        )
        return _GLOBAL_FALLBACK

    @staticmethod
    def _guess_font_category(normalized_name: str) -> str:
        """
        Guess the font category from the normalized name.
        Returns "serif", "sans", "mono", or "".
        """
        mono_hints = ("mono", "courier", "consolas", "menlo", "fira code",
                      "source code", "inconsolata", "cmtt", "lmmono")
        sans_hints = ("sans", "arial", "helvetica", "calibri", "verdana",
                      "roboto", "opensans", "lato", "gothic", "cmss",
                      "lmsans", "futura", "avenir", "gill")
        serif_hints = ("serif", "roman", "times", "palatino", "garamond",
                       "georgia", "baskerville", "bodoni", "didot",
                       "cambria", "cmr", "lmroman", "stix", "minion",
                       "caslon", "century", "bookman")

        for hint in mono_hints:
            if hint in normalized_name:
                return "mono"
        for hint in sans_hints:
            if hint in normalized_name:
                return "sans"
        for hint in serif_hints:
            if hint in normalized_name:
                return "serif"

        # Default: assume serif (most academic books use serif fonts).
        return "serif"

    # ------------------------------------------------------------------
    # Paper size matching
    # ------------------------------------------------------------------

    @staticmethod
    def _match_standard_paper_size(w_mm: float, h_mm: float) -> str:
        """
        Match detected dimensions to a standard paper size.

        Returns the LaTeX paper size name (e.g. "a4paper") or empty
        string if no standard size matches within tolerance.
        """
        for name, (std_w, std_h) in _STANDARD_PAPER_SIZES.items():
            if (
                abs(w_mm - std_w) <= _PAPER_SIZE_TOLERANCE_MM
                and abs(h_mm - std_h) <= _PAPER_SIZE_TOLERANCE_MM
            ):
                return name
        return ""
