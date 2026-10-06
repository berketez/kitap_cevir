"""
Pipeline configuration for PDF → LaTeX conversion.
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List

# Merkezi config (magic number tek kaynak — KURAL §11)
from config.defaults import (
    MAX_SEGMENT_LENGTH,
    SEGMENT_TIMEOUT_S,
    XELATEX_TIMEOUT_S,
)


@dataclass
class PipelineConfig:
    """Configuration for the LaTeX pipeline."""

    # Input/Output
    input_pdf: str = ""
    output_dir: str = "output/latex"

    # Extraction engine
    engine: str = "marker"  # "marker" or "mineru"
    page_range: Optional[List[int]] = None  # None = all pages

    # LaTeX settings
    document_class: str = "book"  # "book", "article", "report"
    font_main: str = "STIX Two Text"
    font_sans: str = "Helvetica Neue"  # macOS sistem fontu
    font_mono: str = "Menlo"  # macOS sistem fontu
    font_math: str = "STIX Two Math"
    paper_size: str = "a4paper"
    font_size: str = "11pt"

    # Translation
    translate: bool = True
    source_lang: str = "en"
    target_lang: str = "tr"
    book_type: str = "science"  # "science", "philosophy", "novel"

    # Compilation
    compiler: str = "xelatex"  # "xelatex" or "lualatex"
    compile_passes: int = 2  # 2 pass standard, bib varsa 4
    nonstop_mode: bool = True  # Continue on errors
    save_source: bool = True  # Always save .tex source before compile

    # Image handling
    extract_images: bool = True
    image_format: str = "png"  # "png", "jpg", original
    image_dpi: int = 300
    preserve_original_format: bool = True  # Keep original format (JPEG→JPEG, PNG→PNG)

    # Formula handling
    preserve_formulas: bool = True  # Never touch formulas
    formula_fallback: str = "image"  # "image" if formula extraction fails

    # Table handling
    table_format: str = "booktabs"  # "booktabs" or "tabular"
    translate_table_cells: bool = True

    # Quality control
    min_qc_score: float = 90.0  # Minimum acceptable QC score (0-100)
    max_autofix_iterations: int = 3  # Max convergence loop iterations
    max_outer_iterations: int = 3   # Max outer QC loop iterations (Phase 5.5-5.8)

    # Layout detection (LayoutDetector tarafindan doldurulur)
    layout_detected: bool = False
    margin_top: str = ""       # ör. "25mm" -- bos ise template varsayilani
    margin_bottom: str = ""
    margin_left: str = ""
    margin_right: str = ""
    page_width: str = ""       # custom boyut icin: "216mm"
    page_height: str = ""
    line_spacing: str = ""     # ör. "1.15" -- bos ise varsayilan

    # Segment limitleri (config.defaults merkezi)
    max_segment_chars: int = MAX_SEGMENT_LENGTH  # Çeviri segmenti karakter üst sınırı

    # LatexTranslator sub-batch boyutu (BATCH-14).
    # LatexTranslator, topladığı LaTeX segmentlerini MT motoruna verirken
    # bu boyutta alt gruplara böler. MLX/GPU bellek-korumalı muhafazakâr
    # değer (8). DİKKAT: config.defaults.DEFAULT_BATCH_SIZE_HYMT_LOCAL (20)
    # ile AYRI kavramdır — o, core BookTranslator'ın tek MT model çağrısında
    # kaç segment işleyeceğidir (mlx_lm.batch_generate seviyesi). Bu alan ise
    # bir üst katmanda, pipeline segmentlerinin gruplama granülaritesidir.
    mlx_batch_size: int = 8

    # Timeouts (config.defaults merkezi)
    compile_timeout: int = XELATEX_TIMEOUT_S   # seconds per XeLaTeX pass
    segment_timeout: int = SEGMENT_TIMEOUT_S   # seconds per translation segment

    def dynamic_compile_timeout(self, page_count: int | None = None) -> int:
        """Compute an upper-bound XeLaTeX compile timeout.

        Uses the formula ``600 + page_count * 5`` (seconds) and keeps a
        generous floor at ``compile_timeout``.  If *page_count* is None
        or negative, returns the static ``compile_timeout``.
        """
        if page_count is None or page_count <= 0:
            return int(self.compile_timeout)
        return max(int(self.compile_timeout), 600 + int(page_count) * 5)

    # Error handling
    max_compile_retries: int = 3
    auto_fix_errors: bool = True  # Only safe fixes (escape chars, missing packages)

    # Directories (computed)
    @property
    def source_dir(self) -> Path:
        """LaTeX source directory."""
        return Path(self.output_dir) / "source"

    @property
    def images_dir(self) -> Path:
        """Extracted images directory."""
        return Path(self.output_dir) / "source" / "images"

    @property
    def compiled_dir(self) -> Path:
        """Compiled PDF output directory."""
        return Path(self.output_dir) / "compiled"

    def apply_layout(self, info) -> None:
        """LayoutInfo'dan config alanlarini doldurur."""
        from latex_pipeline.layout_detector import LayoutInfo
        if not isinstance(info, LayoutInfo):
            return
        self.layout_detected = True
        if info.paper_size_name:
            self.paper_size = info.paper_size_name
        else:
            self.paper_size = "custom"
        self.page_width = f"{info.page_width_mm:.1f}mm"
        self.page_height = f"{info.page_height_mm:.1f}mm"
        self.margin_top = f"{info.margin_top_mm:.1f}mm"
        self.margin_bottom = f"{info.margin_bottom_mm:.1f}mm"
        self.margin_left = f"{info.margin_left_mm:.1f}mm"
        self.margin_right = f"{info.margin_right_mm:.1f}mm"
        if info.mapped_font_name:
            self.font_main = info.mapped_font_name
        # Font boyutu documentclass OPSIYONU olarak gidiyor; book.cls yalnız
        # 10pt/11pt/12pt tanır — '10.9pt' gibi değerler 'Unused global
        # option' ile sessizce 10pt'e düşüyordu (#53). En yakın geçerli
        # sınıf opsiyonuna yuvarlanır.
        size = info.dominant_font_size_pt
        # Koruma: çok küçük veya çok büyük değerleri sınırla
        size = max(8.0, min(14.0, size))
        nearest = min((10, 11, 12), key=lambda opt: abs(opt - size))
        self.font_size = f"{nearest}pt"
        if info.line_spacing_ratio > 1.05:
            self.line_spacing = f"{info.line_spacing_ratio:.2f}"

    def ensure_dirs(self):
        """Create all necessary directories."""
        self.source_dir.mkdir(parents=True, exist_ok=True)
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.compiled_dir.mkdir(parents=True, exist_ok=True)
