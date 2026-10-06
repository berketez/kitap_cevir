"""
LaTeX Pipeline - PDF/EPUB to LaTeX conversion and translation pipeline.

Pipeline:
    PDF  -> Marker extraction -> LaTeX conversion -> Translation -> XeLaTeX compile -> PDF
    EPUB -> EPUB Parser       -> LaTeX conversion -> Translation -> XeLaTeX compile -> PDF

Modules:
    config          - Pipeline configuration
    extractor       - Marker/MinerU wrapper + normalization (PDF)
    epub_extractor  - EPUB text + image extraction
    converter       - Markdown -> LaTeX conversion
    translator      - AST-based selective text translation
    compiler        - XeLaTeX compilation + error handling
    template_engine - LaTeX template management
    glossary        - Terminology engine for consistent term translation
"""

from latex_pipeline.config import PipelineConfig
from latex_pipeline.extractor import MarkerExtractor, ExtractionResult
from latex_pipeline.epub_extractor import EPUBExtractor, EPUBExtractionResult
from latex_pipeline.converter import MarkdownToLatex
from latex_pipeline.compiler import LatexCompiler, CompileResult
from latex_pipeline.template_engine import TemplateEngine
from latex_pipeline.glossary import GlossaryEngine

__all__ = [
    "PipelineConfig",
    "MarkerExtractor",
    "ExtractionResult",
    "EPUBExtractor",
    "EPUBExtractionResult",
    "MarkdownToLatex",
    "LatexCompiler",
    "CompileResult",
    "TemplateEngine",
    "GlossaryEngine",
]
