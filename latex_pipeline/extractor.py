"""
PDF extraction module using Marker and PyMuPDF.

Marker converts PDF pages to Markdown (with LaTeX math preserved).
PyMuPDF extracts embedded images at original quality (lossless PNG).

Usage:
    extractor = MarkerExtractor(config)
    result = extractor.extract("book.pdf", page_range=[0, 1, 2])
    print(result.markdown)       # LaTeX-formula-rich Markdown
    print(result.images)         # {"img_001.png": PosixPath("images/img_001.png"), ...}
    print(result.stats)          # {"pages": 3, "images": 5, "time_s": 12.3}
"""

from __future__ import annotations

import os
os.environ.setdefault("IN_STREAMLIT", "true")  # Disable surya parallel detection

import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Any

from latex_pipeline.config import PipelineConfig

logger = logging.getLogger(__name__)

# F1-12 / X-2 (2026-09-29): alt süreç hata raporunda taşınan traceback ve
# stderr kuyruğunun en fazla uzunluğu (karakter).
SUBPROCESS_TRACEBACK_TAIL = 4000
SUBPROCESS_STDERR_TAIL = 2000
# Doğrudan Marker çağrısı YALNIZ süreç çöküşünde alt süreçte yeniden denenir.
# Deterministik hatalar (IndexError, bellek sınırı...) aynı sayfalarla alt
# süreçte de tekrar eder; tam model yüklemesiyle yeniden denemek boşa zaman
# (hf günlüğü: 20 sayfalık parti doğrudan 12 dk'da düştü, alt süreçte yine).
# Parti düşünce geri çekilme (ikiye bölme) çağıranın işidir
# (translate_book._extract_pages).
_PROCESS_CRASH_MARKERS = (
    "brokenprocesspool",
    "process pool",
    "terminated abruptly",
    "killed by signal",
)


def is_process_crash(exc: BaseException) -> bool:
    """*exc* bir süreç havuzu / alt süreç çöküşü mü (yalıtılmış yeniden
    denemeye değer mi)?"""
    try:
        from concurrent.futures.process import BrokenProcessPool
    except ImportError:  # pragma: no cover - stdlib'de her zaman var
        BrokenProcessPool = ()  # type: ignore[assignment]
    if isinstance(exc, (BrokenProcessPool, BrokenPipeError, EOFError)):
        return True
    text = f"{type(exc).__name__} {exc}".lower()
    return any(marker in text for marker in _PROCESS_CRASH_MARKERS)


# ---------------------------------------------------------------------------
# Unicode normalization constants
# ---------------------------------------------------------------------------

# Unicode bullet-like karakterler -> markdown bullet ("-")
# Marker PDF'den çıkarken bu glif'leri MD syntax'a çevirmeden metne koyabilir;
# bunları normal MD bullet'a çeviriyoruz ki liste render edilebilsin.
_BULLET_TO_MD_BULLET = {
    "◾": "-",   # ◾ BLACK MEDIUM SMALL SQUARE
    "◼": "-",   # ◼ BLACK MEDIUM SQUARE
    "■": "-",   # ■ BLACK SQUARE
    "●": "-",   # ● BLACK CIRCLE
    "▶": "-",   # ▶ BLACK RIGHT-POINTING TRIANGLE
    "►": "-",   # ► BLACK RIGHT-POINTING POINTER
    "•": "-",   # • BULLET
    "⁃": "-",   # ⁃ HYPHEN BULLET
    "‣": "-",   # ‣ TRIANGULAR BULLET
    "∙": "-",   # ∙ BULLET OPERATOR
    # B15: Ek geometrik glyph'ler (Marker/OCR yanlış bullet seçerse)
    "□": "-",   # WHITE SQUARE
    "▫": "-",   # WHITE SMALL SQUARE
    "▪": "-",   # BLACK SMALL SQUARE
    "◆": "-",   # BLACK DIAMOND
    "◇": "-",   # WHITE DIAMOND
    "★": "-",   # BLACK STAR
    "☆": "-",   # WHITE STAR
    "❖": "-",   # BLACK DIAMOND MINUS WHITE X
    # B15: Geometric emoji (Marker unknown glyph'leri bunlara mapleyebilir)
    "🟥": "-",  # RED SQUARE (U+1F7E5)
    "🟦": "-",  # BLUE SQUARE
    "🟩": "-",  # GREEN SQUARE
    "🟨": "-",  # YELLOW SQUARE
    "🟧": "-",  # ORANGE SQUARE
    "🟪": "-",  # PURPLE SQUARE
    "🟫": "-",  # BROWN SQUARE
    "🟢": "-",  # GREEN CIRCLE
    "🔴": "-",  # RED CIRCLE
    "🔵": "-",  # BLUE CIRCLE
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ExtractionResult:
    """Result of a PDF extraction run."""

    markdown: str = ""
    images: Dict[str, Path] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    stats: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# MarkerExtractor
# ---------------------------------------------------------------------------

class MarkerExtractor:
    """
    Extract Markdown + images from a PDF using Marker (primary)
    and PyMuPDF (supplementary image extraction).

    Marker produces Markdown that preserves LaTeX formulas ($..$ and $$..$$).
    PyMuPDF pulls embedded images at native resolution without re-encoding.
    """

    def __init__(self, config: PipelineConfig):
        self.config = config
        self._models = None          # lazy-loaded Marker models
        self._models_loaded = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def extract(
        self,
        pdf_path: str | Path,
        page_range: Optional[List[int]] = None,
    ) -> ExtractionResult:
        """
        Run the full extraction pipeline on *pdf_path*.

        Parameters
        ----------
        pdf_path : path to a PDF file
        page_range : 0-based page indices.  ``None`` = all pages.

        Returns
        -------
        ExtractionResult with .markdown, .images, .metadata, .stats
        """
        pdf_path = Path(pdf_path)
        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF not found: {pdf_path}")

        t0 = time.time()

        # Ensure output directories exist
        self.config.ensure_dirs()
        images_dir = self.config.images_dir

        # Use config page_range as fallback
        if page_range is None:
            page_range = self.config.page_range

        # Step 1 -- Marker: PDF -> Markdown
        # Try direct call first; fall back to subprocess isolation ONLY if
        # Marker's internal multiprocessing crashed (BrokenProcessPool ...).
        # Other errors propagate: the caller retries smaller page ranges with
        # the models already loaded here (F1-12).
        try:
            markdown, marker_images, metadata = self._run_marker(pdf_path, page_range)
        except Exception as direct_err:
            if not is_process_crash(direct_err):
                raise
            logger.warning(
                "Direct Marker call crashed (%s: %s). Retrying in isolated "
                "subprocess ...", type(direct_err).__name__, direct_err,
                exc_info=True,
            )
            markdown, marker_images, metadata = self._extract_in_subprocess(
                pdf_path, page_range, images_dir
            )

        # Step 1.5 -- Unicode normalize: Marker markdown'unda bazen PUA
        # (Private Use Area) glyph kodları, U+FFFD ve Unicode bullet
        # karakterleri kalıyor; bunlar XeLaTeX'te '?' olarak basılıyor ya da
        # bullet list'e dönüşmüyor. Downstream (translator, latex builder)
        # temiz MD görmeli.
        markdown, norm_stats = self._normalize_unicode(markdown)
        if any(norm_stats.values()):
            logger.info("Unicode normalize stats: %s", norm_stats)

        # Step 2 -- Save Marker-returned images (PIL) to disk
        saved_images: Dict[str, Path] = {}
        pymupdf_imgs: Dict[str, Path] = {}
        for img_name, pil_img in marker_images.items():
            saved = self._save_pil_image(pil_img, img_name, images_dir)
            if saved:
                saved_images[img_name] = saved

        # Step 3 -- PyMuPDF: extract embedded images at original quality
        if self.config.extract_images:
            pymupdf_imgs = self._extract_images_pymupdf(pdf_path, page_range, images_dir)
            # Merge -- PyMuPDF images that are NOT already saved by Marker
            for name, path in pymupdf_imgs.items():
                if name not in saved_images:
                    saved_images[name] = path

        elapsed = time.time() - t0
        stats = {
            "pages": len(page_range) if page_range else "all",
            "images_marker": len(marker_images),
            "images_pymupdf": len(pymupdf_imgs) if self.config.extract_images else 0,
            "images_total": len(saved_images),
            "time_s": round(elapsed, 2),
        }
        logger.info("Extraction done in %.1fs -- %d images", elapsed, len(saved_images))

        return ExtractionResult(
            markdown=markdown,
            images=saved_images,
            metadata=metadata,
            stats=stats,
        )

    # ------------------------------------------------------------------
    # Subprocess isolation helper
    # ------------------------------------------------------------------

    def _extract_in_subprocess(
        self,
        pdf_path: Path,
        page_range: Optional[List[int]],
        images_dir: Path,
        timeout: int = 3600,
    ) -> tuple:
        """
        Run Marker extraction in a completely isolated subprocess.

        This avoids BrokenProcessPool errors caused by Marker's internal
        multiprocessing conflicting with fork() on macOS.  The subprocess
        writes its result (markdown + image paths) as JSON to a temp file
        which we read back.

        Returns (markdown, images_dict, metadata_dict) where images_dict
        maps image names to PIL.Image objects loaded from the saved PNGs.
        """
        # Build a self-contained Python script that runs Marker and dumps
        # the result to a JSON file.
        result_file = tempfile.NamedTemporaryFile(
            suffix=".json", delete=False, prefix="marker_result_"
        )
        result_path = result_file.name
        result_file.close()

        page_range_json = json.dumps(page_range)
        images_dir_str = str(images_dir)
        pdf_path_str = str(pdf_path)

        # The script intentionally does NOT import from latex_pipeline.extractor
        # to keep the subprocess completely isolated.
        script = f'''
import os, sys, json, time
os.environ["TORCH_DEVICE"] = os.environ.get("TORCH_DEVICE", "cpu")

# Force spawn for multiprocessing (macOS safety)
import multiprocessing
try:
    multiprocessing.set_start_method("spawn", force=True)
except RuntimeError:
    pass

pdf_path = {pdf_path_str!r}
page_range = {page_range_json}
images_dir = {images_dir_str!r}
result_path = {result_path!r}

os.makedirs(images_dir, exist_ok=True)

try:
    from marker.models import create_model_dict
    from marker.converters.pdf import PdfConverter

    models = create_model_dict()

    converter_config = {{}}
    if page_range is not None:
        converter_config["page_range"] = list(page_range)

    converter = PdfConverter(artifact_dict=models, config=converter_config)
    rendered = converter(pdf_path)

    markdown = rendered.markdown if hasattr(rendered, "markdown") else ""
    images = rendered.images if hasattr(rendered, "images") else {{}}
    metadata = rendered.metadata if hasattr(rendered, "metadata") else {{}}

    # Save images to disk and record their paths
    saved_images = {{}}
    from pathlib import Path as P
    for img_name, pil_img in images.items():
        try:
            stem = P(img_name).stem
            out_path = P(images_dir) / f"{{stem}}.png"
            pil_img.save(str(out_path), format="PNG")
            saved_images[img_name] = str(out_path)
        except Exception:
            pass

    result = {{
        "ok": True,
        "markdown": markdown,
        "images": saved_images,
        "metadata": {{k: str(v) for k, v in metadata.items()}} if metadata else {{}},
    }}

except Exception as e:
    import traceback
    result = {{
        "ok": False,
        "error": f"{{type(e).__name__}}: {{e}}",
        "traceback": traceback.format_exc()[-{SUBPROCESS_TRACEBACK_TAIL}:],
        "markdown": "", "images": {{}}, "metadata": {{}},
    }}

with open(result_path, "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False)
'''

        # Write script to a temp file
        script_file = tempfile.NamedTemporaryFile(
            suffix=".py", delete=False, prefix="marker_subprocess_",
            mode="w", encoding="utf-8",
        )
        script_file.write(script)
        script_file.close()

        proc: Optional[subprocess.Popen] = None
        try:
            logger.info(
                "Starting Marker subprocess for %s (pages=%s, timeout=%ds)",
                pdf_path.name, page_range, timeout,
            )
            proc = subprocess.Popen(
                [sys.executable, script_file.name],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={**os.environ, "TORCH_DEVICE": os.environ.get("TORCH_DEVICE", "cpu")},
            )
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                # Kill child process -- avoid zombie / MPS deadlock.
                logger.error(
                    "Marker subprocess timed out after %ds; killing PID %s",
                    timeout, proc.pid,
                )
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass  # SIGKILL couldn't finish; let OS reap
                raise RuntimeError(
                    f"Marker subprocess timed out after {timeout}s (killed)"
                )

            returncode = proc.returncode
            stderr_tail = stderr[-SUBPROCESS_STDERR_TAIL:] if stderr else ""
            if returncode != 0:
                logger.error(
                    "Marker subprocess failed (rc=%d). stderr (son %d "
                    "karakter):\n%s",
                    returncode, SUBPROCESS_STDERR_TAIL, stderr_tail,
                )
                raise RuntimeError(
                    f"Marker subprocess exited with code {returncode}"
                )

            with open(result_path, "r", encoding="utf-8") as f:
                result = json.load(f)

            if not result.get("ok"):
                # X-2: alt süreç traceback'i atılmaz; günlüğe (çıkarım
                # evresinde pipeline.log) ve istisnaya taşınır.
                sub_tb = result.get("traceback") or ""
                logger.error(
                    "Marker subprocess error: %s\nAlt süreç traceback:\n%s"
                    "\nstderr (son %d karakter):\n%s",
                    result.get("error", "unknown"), sub_tb,
                    SUBPROCESS_STDERR_TAIL, stderr_tail,
                )
                error = RuntimeError(
                    f"Marker subprocess error: {result.get('error', 'unknown')}")
                error.subprocess_traceback = sub_tb  # type: ignore[attr-defined]
                raise error

            markdown = result["markdown"]
            metadata = result.get("metadata", {})

            # Reconstruct images dict: load saved PNGs as PIL images
            from PIL import Image
            marker_images = {}
            for img_name, img_path_str in result.get("images", {}).items():
                img_path = Path(img_path_str)
                if img_path.exists():
                    try:
                        marker_images[img_name] = Image.open(img_path).copy()
                    except Exception:
                        pass

            logger.info(
                "Marker subprocess succeeded: %d chars markdown, %d images",
                len(markdown), len(marker_images),
            )
            return markdown, marker_images, metadata

        finally:
            # Clean up temp files
            for tmp in [script_file.name, result_path]:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    # ------------------------------------------------------------------
    # Marker
    # ------------------------------------------------------------------

    def _load_models(self):
        """Lazy-load Marker model dict (heavy, ~2 GB VRAM)."""
        if self._models_loaded:
            return

        # Apply MPS workaround if on Apple Silicon GPU
        device = os.environ.get("TORCH_DEVICE", "")
        if device != "cpu":
            self._apply_mps_workaround()

        from marker.models import create_model_dict
        logger.info("Loading Marker models ...")
        self._models = create_model_dict()
        self._models_loaded = True
        logger.info("Marker models loaded.")

    @staticmethod
    def _apply_mps_workaround():
        """
        Surya 0.17.1 MPS crash workaround.

        Surya's vision encoder uses SDPA attention with boolean tensor
        indexing that crashes on MPS (torch.AcceleratorError: index out of bounds).
        Also img_w_embed/img_h_embed cause device mismatch if only vision_encoder
        is moved. Fix: move entire model to CPU for get_image_embeddings,
        then move back to MPS. Only this function is slow; rest stays on MPS.
        """
        try:
            import torch
            if not (torch.backends.mps.is_available() and torch.backends.mps.is_built()):
                return

            from surya.common.surya import SuryaModel

            _original = SuryaModel.get_image_embeddings

            def _cpu_safe_embeddings(self, pixel_values, grid_thw, encoder_chunk_size,
                                     valid_batch_size=None, max_batch_size=None):
                original_device = self.device
                # Move entire model to CPU (vision_encoder + img_w/h_embed + others)
                self.to("cpu")
                pv_cpu = pixel_values.to("cpu")
                gt_cpu = grid_thw.to("cpu")
                result = _original(self, pv_cpu, gt_cpu, encoder_chunk_size,
                                   valid_batch_size, max_batch_size)
                # Move back to MPS for remaining operations
                self.to(original_device)
                return result.to(original_device)

            SuryaModel.get_image_embeddings = _cpu_safe_embeddings
            logger.info("MPS workaround applied: image embeddings will run on CPU")

        except Exception as e:
            logger.warning("MPS workaround failed (non-fatal): %s", e)

    def _run_marker(
        self,
        pdf_path: Path,
        page_range: Optional[List[int]],
    ) -> tuple[str, dict, dict]:
        """
        Run Marker converter on the PDF.

        Returns (markdown_text, images_dict, metadata_dict).
        ``images_dict`` maps image filenames to PIL.Image objects.
        """
        self._load_models()

        from marker.converters.pdf import PdfConverter

        converter_config: dict = {"pdftext_workers": 1}
        if page_range is not None:
            converter_config["page_range"] = list(page_range)

        converter = PdfConverter(
            artifact_dict=self._models,
            config=converter_config,
        )

        logger.info("Running Marker on %s (pages=%s) ...", pdf_path.name, page_range)
        rendered = converter(str(pdf_path))

        markdown: str = rendered.markdown if hasattr(rendered, "markdown") else ""
        images: dict = rendered.images if hasattr(rendered, "images") else {}
        metadata: dict = rendered.metadata if hasattr(rendered, "metadata") else {}

        logger.info(
            "Marker finished: %d chars markdown, %d images",
            len(markdown),
            len(images),
        )
        return markdown, images, metadata

    # ------------------------------------------------------------------
    # Unicode normalization
    # ------------------------------------------------------------------

    def _normalize_unicode(self, text: str) -> tuple[str, dict]:
        """
        Marker markdown çıktısını XeLaTeX'e güvenli hale getir.

        Adımlar:
          1) NFC normalize (Türkçe birleşik forma: c + combining cedilla -> ç)
          2) Unicode bullet/geometric karakterlerini markdown '-' bullet'a map et
          3) Private Use Area (U+E000-U+F8FF) karakterlerini at -- genelde
             Marker/OCR'den gelen glif kodları, XeLaTeX'te '?' basıyor
          4) Private Use Supplementary A/B (U+F0000-U+10FFFD) at
          5) Replacement character (U+FFFD) at
          6) Non-character kod noktalarını at (U+FFFE, U+FFFF, U+FDD0-U+FDEF,
             ve her plane'in son 2 karakteri: U+nFFFE, U+nFFFF)
          7) C0/C1 kontrol karakterlerini at (\t, \n, \r hariç)

        Returns
        -------
        (normalized_text, stats) -- stats: pua_stripped, fffd_stripped,
                                    bullets_mapped, control_stripped,
                                    noncharacter_stripped,
                                    private_use_supp_stripped sayıları.
        """
        stats = {
            "pua_stripped": 0,
            "fffd_stripped": 0,
            "bullets_mapped": 0,
            "control_stripped": 0,
            "noncharacter_stripped": 0,
            "private_use_supp_stripped": 0,
        }

        # 1. NFC normalize (composed form for Turkish chars)
        text = unicodedata.normalize("NFC", text)

        # 2-7. Karakter bazlı tarama
        out_chars = []
        for ch in text:
            mapped = _BULLET_TO_MD_BULLET.get(ch)
            if mapped is not None:
                out_chars.append(mapped)
                stats["bullets_mapped"] += 1
                continue

            cp = ord(ch)
            # Private Use Area (BMP): U+E000-U+F8FF
            if 0xE000 <= cp <= 0xF8FF:
                stats["pua_stripped"] += 1
                continue
            # Private Use Supplementary A: U+F0000-U+FFFFD
            # Private Use Supplementary B: U+100000-U+10FFFD
            if 0xF0000 <= cp <= 0x10FFFD:
                stats["private_use_supp_stripped"] += 1
                continue
            # Replacement character U+FFFD
            if cp == 0xFFFD:
                stats["fffd_stripped"] += 1
                continue
            # Non-characters (BMP): U+FFFE, U+FFFF
            if cp in (0xFFFE, 0xFFFF):
                stats["noncharacter_stripped"] += 1
                continue
            # Non-characters (Arabic range): U+FDD0-U+FDEF
            if 0xFDD0 <= cp <= 0xFDEF:
                stats["noncharacter_stripped"] += 1
                continue
            # Non-characters (other planes): U+nFFFE, U+nFFFF (n=1..16)
            if cp > 0xFFFF and (cp & 0xFFFF) in (0xFFFE, 0xFFFF):
                stats["noncharacter_stripped"] += 1
                continue
            # C0 control (except \t \n \r)
            if cp < 0x20 and ch not in "\t\n\r":
                stats["control_stripped"] += 1
                continue
            # C1 control (U+007F-U+009F)
            if 0x7F <= cp <= 0x9F:
                stats["control_stripped"] += 1
                continue

            out_chars.append(ch)

        return "".join(out_chars), stats

    # ------------------------------------------------------------------
    # Image I/O
    # ------------------------------------------------------------------

    def _save_pil_image(self, pil_img, name: str, images_dir: Path) -> Optional[Path]:
        """
        Save a PIL.Image to *images_dir* as lossless PNG.
        Returns the saved path, or None on failure.
        """
        try:
            # Normalise extension
            stem = Path(name).stem
            out_path = images_dir / f"{stem}.png"
            pil_img.save(str(out_path), format="PNG")
            logger.debug("Saved Marker image: %s", out_path.name)
            return out_path
        except Exception as exc:
            logger.warning("Failed to save image %s: %s", name, exc)
            return None

    # ------------------------------------------------------------------
    # PyMuPDF image extraction
    # ------------------------------------------------------------------

    def _extract_images_pymupdf(
        self,
        pdf_path: Path,
        page_range: Optional[List[int]],
        images_dir: Path,
    ) -> Dict[str, Path]:
        """
        Extract embedded images from the PDF using PyMuPDF (fitz).

        For each image object we call ``Document.extract_image(xref)`` which
        returns the raw bytes in the original format.  We re-save as PNG
        when *lossless_images* is True (default), otherwise keep original.

        Returns a dict mapping filenames to their paths on disk.
        """
        try:
            import fitz
        except ImportError:
            logger.warning("PyMuPDF (fitz) not installed -- skipping embedded image extraction")
            return {}

        doc = fitz.open(str(pdf_path))
        saved: Dict[str, Path] = {}
        seen_xrefs: set = set()
        img_counter = 0

        pages = range(len(doc)) if page_range is None else page_range

        for page_idx in pages:
            if page_idx >= len(doc):
                continue
            page = doc[page_idx]
            image_list = page.get_images(full=True)

            for img_info in image_list:
                xref = img_info[0]
                if xref in seen_xrefs:
                    continue
                seen_xrefs.add(xref)

                try:
                    base_image = doc.extract_image(xref)
                except Exception:
                    continue

                if base_image is None:
                    continue

                img_bytes = base_image.get("image")
                img_ext = base_image.get("ext", "png")

                if img_bytes is None or len(img_bytes) < 100:
                    # Skip tiny placeholder images
                    continue

                img_counter += 1

                # Flag semantics (matches config.py docstring):
                #   preserve_original_format=True  -> keep raw bytes in
                #       their original format (JPEG->JPEG, PNG->PNG).
                #   preserve_original_format=False -> re-encode everything
                #       non-PNG into lossless PNG.
                # Source PNG bytes are always written directly regardless
                # of the flag (already lossless).
                if self.config.preserve_original_format or img_ext == "png":
                    actual_filename = f"pymupdf_{img_counter:04d}.{img_ext}"
                    saved_path = images_dir / actual_filename
                    saved_path.write_bytes(img_bytes)
                    filename = actual_filename
                else:
                    filename = f"pymupdf_{img_counter:04d}.png"
                    saved_path = self._reencode_as_png(
                        img_bytes, images_dir / filename,
                    )

                if saved_path and saved_path.exists():
                    saved[filename] = saved_path

        doc.close()
        logger.info("PyMuPDF extracted %d images from %s", len(saved), pdf_path.name)
        return saved

    @staticmethod
    def _reencode_as_png(raw_bytes: bytes, out_path: Path) -> Optional[Path]:
        """Re-encode raw image bytes to lossless PNG via Pillow."""
        try:
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(raw_bytes))
            img.save(str(out_path), format="PNG")
            return out_path
        except Exception as exc:
            logger.warning("PNG re-encode failed: %s", exc)
            return None
