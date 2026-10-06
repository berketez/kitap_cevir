"""
EPUB extraction module -- converts EPUB to Markdown + images.

Mirrors the MarkerExtractor API so that translate_book.py can use
the same downstream pipeline (MarkdownToLatex -> translate -> compile).

Uses the existing parsers/epub_parser.py (EPUBParser) for text extraction
and ebooklib for image extraction.

Usage:
    extractor = EPUBExtractor()
    result = extractor.extract("book.epub", output_dir="output/my_book")
    print(result.markdown)       # Markdown text (headings, bold, lists, etc.)
    print(result.images)         # {"img_001.png": PosixPath("output/.../images/img_001.png"), ...}
    print(result.metadata)       # {"title": "...", "author": "...", ...}
"""

from __future__ import annotations

import io
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# R13: EPUBParser'ın tek başına satır olarak yazdığı gövde görseli (boş alt
# metin; alt metin açıklamadır, dönüştürücü onu \caption yapardı).
# Ad boşluk içerebilir ('iç yol%20.png' çözülmüş hâli); dönüştürücünün görsel
# satırı kalıbıyla aynı ölçüt ([^)]+).
_BODY_IMAGE_LINE_RE = re.compile(r"^!\[\]\(([^)]+)\)$")
# F1-08 (2026-09-29): metin satırındaki satır içi görsel (alt metin kaçışlı
# olabilir; parsers.epub_parser._MD_IMAGE_RE ile aynı biçim).
_INLINE_IMAGE_RE = re.compile(r"!\[((?:\\.|[^\[\]\\])*)\]\(([^()\s]+)\)")


def _warn_user(message: str) -> None:
    """Görünür uyarı (günlük + konsol; epub_parser._warn ile aynı kural)."""
    try:
        from parsers.epub_parser import _warn
    except ImportError:  # pragma: no cover
        logger.warning(message)
        return
    _warn(message)


# XeLaTeX'in \includegraphics ile doğrudan alabildiği biçimler.
_TEX_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".pdf"})
# PNG'ye çevrilerek kullanılabilen biçimler (Pillow).
_CONVERTIBLE_IMAGE_SUFFIXES = frozenset({".gif", ".webp", ".bmp", ".tif", ".tiff"})


# ---------------------------------------------------------------------------
# Data classes (same shape as extractor.ExtractionResult)
# ---------------------------------------------------------------------------

@dataclass
class EPUBExtractionResult:
    """Result of an EPUB extraction run."""

    markdown: str = ""
    images: Dict[str, Path] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    stats: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# EPUBExtractor
# ---------------------------------------------------------------------------

class EPUBExtractor:
    """
    Extract Markdown + images from an EPUB file.

    Text extraction uses parsers/epub_parser.py (EPUBParser.to_markdown).
    Image extraction reads all IMAGE items from the EPUB container via
    ebooklib and saves them to the output images directory.
    """

    def __init__(self):
        pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def extract(
        self,
        epub_path: str | Path,
        output_dir: str | Path,
    ) -> EPUBExtractionResult:
        """
        Extract text and images from *epub_path*.

        Parameters
        ----------
        epub_path  : path to the EPUB file.
        output_dir : base output directory. Markdown goes to
                     ``output_dir/full_book.md``, images to
                     ``output_dir/source/images/``.

        Returns
        -------
        EPUBExtractionResult with .markdown, .images, .metadata, .stats
        """
        epub_path = Path(epub_path)
        if not epub_path.exists():
            raise FileNotFoundError(f"EPUB not found: {epub_path}")

        t0 = time.time()
        output_dir = Path(output_dir)
        images_dir = output_dir / "source" / "images"
        images_dir.mkdir(parents=True, exist_ok=True)

        # -- Step 1: Parse EPUB -> markdown -----------------------------------
        markdown, metadata = self._extract_text(epub_path)

        # -- Step 2: Extract images from EPUB container -----------------------
        saved_images = self._extract_images(epub_path, images_dir, markdown)

        # -- Step 3: Update image references in markdown ----------------------
        # EPUB images are referenced by their internal path (e.g., "images/fig1.png"
        # or "OEBPS/images/fig1.png"). We replace these with just the filename
        # so that MarkdownToLatex can find them in the images/ directory.
        markdown = self._fix_image_refs(markdown, saved_images)

        # -- Step 4: Inject cover image into markdown -------------------------
        # EPUB cover is stored as metadata (properties="cover-image"), not in
        # the HTML body, so it never appears in the parsed markdown. Inject it
        # after the first heading so the title page builder picks it up.
        markdown = self._inject_cover(markdown, images_dir)

        elapsed = time.time() - t0
        stats = {
            "chapters": metadata.get("chapters", 0),
            "images_total": len(saved_images),
            "markdown_chars": len(markdown),
            "time_s": round(elapsed, 2),
            "warnings": list(getattr(self, "_parse_warnings", [])),
        }
        logger.info(
            "EPUB extraction done in %.1fs -- %d chars, %d images",
            elapsed, len(markdown), len(saved_images),
        )

        return EPUBExtractionResult(
            markdown=markdown,
            images=saved_images,
            metadata=metadata,
            stats=stats,
        )

    # ------------------------------------------------------------------
    # Text extraction via EPUBParser
    # ------------------------------------------------------------------

    def _extract_text(self, epub_path: Path) -> tuple[str, dict]:
        """
        Parse the EPUB and return (markdown_text, metadata_dict).

        Uses parsers.epub_parser.EPUBParser which already handles:
        - Chapter splitting
        - HTML -> markdown conversion (headings, bold, italic, lists, etc.)
        - Code block preservation
        """
        import sys
        from pathlib import Path as _P

        # Ensure project root is on sys.path so we can import parsers/
        project_root = _P(__file__).resolve().parent.parent
        if str(project_root) not in sys.path:
            sys.path.insert(0, str(project_root))

        from parsers.epub_parser import EPUBParser

        parser = EPUBParser(preserve_formatting=True)
        epub_doc = parser.parse(str(epub_path))

        # Convert to markdown
        markdown = parser.to_markdown(epub_doc)

        metadata = {
            "title": epub_doc.title,
            "author": epub_doc.author,
            "language": epub_doc.language,
            "chapters": epub_doc.total_chapters,
        }
        metadata.update(epub_doc.metadata)
        # 2026-09-29: ayrıştırma uyarıları (kodlama, MathML, görsel, gezinme)
        # sonucun istatistiğine de yazılır (parse() konsola/günlüğe bastı).
        self._parse_warnings = list(getattr(epub_doc, "warnings", []) or [])

        logger.info(
            "EPUB parsed: '%s' by %s, %d chapters, %d chars markdown",
            metadata["title"],
            metadata.get("author", "unknown"),
            metadata["chapters"],
            len(markdown),
        )

        return markdown, metadata

    # ------------------------------------------------------------------
    # Image extraction from EPUB container
    # ------------------------------------------------------------------

    def _extract_images(
        self,
        epub_path: Path,
        images_dir: Path,
        markdown_text: str,
    ) -> Dict[str, Path]:
        """
        Extract all IMAGE items from the EPUB and save to *images_dir*.

        Returns dict mapping filenames to their saved paths.
        """
        import ebooklib
        from ebooklib import epub

        import hashlib

        book = epub.read_epub(str(epub_path))
        saved: Dict[str, Path] = {}
        # (#49) EPUB ic yolu -> kaydedilen dosya adi. Ayni basename'li
        # farkli gorseller (ch1/fig1.png, ch2/fig1.png) yeniden
        # adlandirildiginda markdown referanslarinin DOGRU dosyaya
        # baglanabilmesi icin tam yol haritasi tutulur.
        self._image_path_map: Dict[str, str] = {}
        img_counter = 0

        # ebooklib item types: ITEM_IMAGE = 1 (NOT 6, which is ITEM_FONT)
        IMAGE_TYPE = ebooklib.ITEM_IMAGE

        for item in book.get_items():
            if item.get_type() == IMAGE_TYPE:
                img_counter += 1
                internal_path = (item.get_name() or "").replace("\\", "/")
                original_name = Path(internal_path).name
                content = item.get_content()

                if content is None or len(content) < 100:
                    continue  # Skip tiny/empty images

                # Determine extension from media type or original name
                media_type = item.media_type or ""
                ext = self._ext_from_media_type(media_type, original_name)

                # Use original filename if available, else generate one
                if original_name and "." in original_name:
                    filename = original_name
                else:
                    filename = f"epub_img_{img_counter:04d}{ext}"

                # Save to disk
                out_path = images_dir / filename
                # Avoid overwriting if same name exists with different content
                if out_path.exists():
                    # (#49) Icerik kiyasi hash ile — eski uzunluk kiyasi ayni
                    # boyutlu FARKLI gorselleri ayni sayiyordu.
                    try:
                        existing_md5 = hashlib.md5(
                            out_path.read_bytes()).hexdigest()
                    except OSError:
                        existing_md5 = None
                    if existing_md5 == hashlib.md5(content).hexdigest():
                        saved[filename] = out_path
                        if internal_path:
                            self._image_path_map[internal_path] = filename
                        continue
                    # Different content, add counter suffix
                    stem = out_path.stem
                    filename = f"{stem}_{img_counter:04d}{ext}"
                    out_path = images_dir / filename

                try:
                    out_path.write_bytes(content)
                    saved[filename] = out_path
                    if internal_path:
                        self._image_path_map[internal_path] = filename
                    logger.debug("Saved EPUB image: %s (%d bytes)", filename, len(content))
                except Exception as exc:
                    logger.warning("Failed to save EPUB image %s: %s", filename, exc)

        logger.info("Extracted %d images from EPUB", len(saved))
        return saved

    # ------------------------------------------------------------------
    # Image reference fixing
    # ------------------------------------------------------------------

    def _inject_cover(self, markdown: str, images_dir: Path) -> str:
        """
        Inject EPUB cover image into markdown after the first heading.

        EPUB cover is metadata (properties="cover-image"), so it never appears
        in the parsed body. We look for a cover.* file in images_dir and emit
        `![cover](cover.ext)` right after the first heading, mirroring the
        layout that ``ConverterToLatex._build_title_page`` expects (heading
        then cover image).
        """
        import re as _re

        cover_files = [
            p for p in images_dir.glob("cover.*")
            if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")
        ]
        if not cover_files:
            return markdown

        cover_name = cover_files[0].name
        if _re.search(r'!\[[^\]]*\]\(' + _re.escape(cover_name) + r'\)', markdown):
            return markdown  # already present

        lines = markdown.split("\n")
        for i, line in enumerate(lines):
            if line.lstrip().startswith("#"):
                lines[i + 1:i + 1] = ["", f"![cover]({cover_name})", ""]
                logger.info("EPUB cover injected after heading: %s", cover_name)
                return "\n".join(lines)
        return markdown

    def _fix_image_refs(
        self,
        markdown: str,
        saved_images: Dict[str, Path],
    ) -> str:
        """
        Fix image references in markdown to point to extracted filenames.

        EPUB markdown may contain image refs like:
          ![alt](images/fig1.png)
          ![alt](OEBPS/images/fig1.png)
          ![alt](../images/fig1.png)

        We normalize these to just the filename:
          ![alt](fig1.png)

        so that MarkdownToLatex generates \\includegraphics{fig1.png}
        and XeLaTeX finds them in the graphicspath{images/} directory.
        """
        known_filenames = set(saved_images.keys())
        # (#49) Once ic-yol haritasi denenir: yeniden adlandirilan gorseller
        # (ch2/fig1.png -> fig1_0004.png) eskiden basename fallback'iyle
        # YANLIS gorsele (ilk fig1.png) baglaniyordu.
        path_map: Dict[str, str] = getattr(self, "_image_path_map", {}) or {}

        def _replace_img_ref(m):
            alt = m.group(1)
            ref_path = m.group(2)
            norm = re.sub(r"^(\.\./|\./)+", "", ref_path.replace("\\", "/"))
            # 1) Tam ic yol eslesmesi
            if norm in path_map:
                return f"![{alt}]({path_map[norm]})"
            # 2) Sondan-eslesen yol (OEBPS/ gibi kok on-eki farklari)
            for internal, fname in path_map.items():
                if internal.endswith("/" + norm):
                    return f"![{alt}]({fname})"
            # 3) Basename fallback (eski davranis)
            basename = Path(ref_path).name
            if basename in known_filenames:
                return f"![{alt}]({basename})"
            # If not found, keep original (MarkdownToLatex will handle missing images)
            return m.group(0)

        # Match markdown image syntax: ![alt](path)
        markdown = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", _replace_img_ref, markdown)

        # R13 (2026-09-25): EPUBParser gövde görsellerini tek başına satır
        # olarak ('![](iç/yol.png)') yazar. XeLaTeX'in alamadığı biçim
        # (gif/webp/bmp/tiff) PNG'ye çevrilir; dosyası çıkarılamamış ya da
        # çevrilemeyen (svg) görselin satırı atılır — eksik dosyaya
        # \includegraphics derlemeyi hatayla durdururdu.
        images_dir = None
        for p in saved_images.values():
            images_dir = Path(p).parent
            break
        kept_lines = []
        dropped: list = []
        for line in markdown.split("\n"):
            m = _BODY_IMAGE_LINE_RE.match(line)
            if m is None:
                if "![" in line:
                    line = self._fix_inline_images(line, saved_images, images_dir,
                                                   dropped)
                kept_lines.append(line)
                continue
            name = m.group(1)
            usable = self._usable_image(name, saved_images, images_dir)
            if usable is None:
                dropped.append(name)
                continue
            kept_lines.append(f"![]({usable})")
        if dropped:
            # Kayıpsızlık (2026-09-29): atlanan görsel sessiz geçmez.
            names = sorted(set(dropped))
            _warn_user(f"{len(dropped)} gövde görseli PDF'e alınamadı (dosya yok "
                       f"ya da biçim desteklenmiyor: SVG vb.): "
                       + ", ".join(names[:8]) + (" …" if len(names) > 8 else ""))
        return "\n".join(kept_lines)

    def _fix_inline_images(self, line: str, saved_images: Dict[str, Path],
                           images_dir: Optional[Path], dropped: list) -> str:
        """F1-08 (2026-09-29): metin satırındaki satır içi görsel
        ('… ![alt](ad) …'; dönüştürücü \\includegraphics[height=1em] yapar).
        Kullanılamayan (yok, SVG) görsel alt metniyle kalır, alt metni yoksa
        atılır; ikisi de uyarılır. GIF/WebP/BMP/TIFF PNG'ye çevrilir."""
        def repl(m: "re.Match") -> str:
            alt, name = m.group(1), m.group(2)
            usable = self._usable_image(name, saved_images, images_dir)
            if usable is not None:
                return f"![{alt}]({usable})"
            dropped.append(name)
            return alt
        return _INLINE_IMAGE_RE.sub(repl, line)

    @staticmethod
    def _usable_image(name: str, saved_images: Dict[str, Path],
                      images_dir: Optional[Path]) -> Optional[str]:
        """Gövde görselinin derlemede kullanılabilir dosya adı ya da None.

        PNG'ye çevrilen görsel *saved_images*'e (sonucun ``images``'ı) eklenir.
        """
        if name not in saved_images or images_dir is None:
            return None
        suffix = Path(name).suffix.lower()
        if suffix in _TEX_IMAGE_SUFFIXES:
            return name
        if suffix not in _CONVERTIBLE_IMAGE_SUFFIXES:
            return None
        # 'sekil.gif' -> 'sekil_gif.png' (aynı adlı gerçek bir PNG ile çakışmaz)
        png_name = f"{Path(name).stem}_{suffix[1:]}.png"
        png_path = images_dir / png_name
        if png_path.exists():
            saved_images.setdefault(png_name, png_path)
            return png_name  # aynı koşuda daha önce çevrildi
        try:
            from PIL import Image
        except ImportError:
            return None
        try:
            with Image.open(saved_images[name]) as im:
                im.seek(0)
                frame = im.convert("RGBA") if im.mode in ("P", "LA", "RGBA") else im.convert("RGB")
                frame.save(png_path, "PNG")
        except Exception as exc:  # bozuk/okunamayan görsel
            logger.warning("EPUB image conversion failed %s: %s", name, exc)
            return None
        # Sonuçtaki görsel listesi markdown'daki adla tutarlı kalsın
        saved_images[png_name] = png_path
        return png_name

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _ext_from_media_type(media_type: str, fallback_name: str) -> str:
        """Determine file extension from MIME type or filename."""
        mime_map = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/gif": ".gif",
            "image/svg+xml": ".svg",
            "image/webp": ".webp",
            "image/bmp": ".bmp",
        }
        ext = mime_map.get(media_type.lower(), "")
        if ext:
            return ext

        # Fallback: get from filename
        if "." in fallback_name:
            return "." + fallback_name.rsplit(".", 1)[-1].lower()

        return ".png"  # Ultimate fallback
