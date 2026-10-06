"""
Tests for EPUB -> Markdown -> LaTeX pipeline.

Tests:
1. EPUBExtractor: EPUB -> markdown + images
2. detect_input_format: file extension detection
3. End-to-end: EPUB -> markdown -> LaTeX body (no translation, no compile)
4. CLI argument parsing for EPUB input
"""

import os
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _create_minimal_epub(epub_path: str, title: str = "Test Book",
                         author: str = "Test Author",
                         chapters: list = None) -> str:
    """
    Create a minimal valid EPUB 2.0 file for testing.

    EPUB is just a ZIP with specific structure:
      mimetype
      META-INF/container.xml
      OEBPS/content.opf
      OEBPS/toc.ncx
      OEBPS/chapter_*.xhtml
    """
    if chapters is None:
        chapters = [
            ("Introduction", "<h1>Introduction</h1><p>This is the introduction.</p>"),
            ("Chapter 1", "<h1>Chapter 1</h1><p>This is <b>bold</b> and <i>italic</i> text.</p>"
                          "<h2>Section 1.1</h2><p>A subsection with a list:</p>"
                          "<ul><li>Item one</li><li>Item two</li></ul>"),
            ("Chapter 2", "<h1>Chapter 2</h1><p>Final chapter with a <a href='http://example.com'>link</a>.</p>"
                          "<blockquote>A famous quote here.</blockquote>"),
        ]

    with zipfile.ZipFile(epub_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        # mimetype must be first, uncompressed
        zf.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)

        # META-INF/container.xml
        zf.writestr('META-INF/container.xml', '''<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>''')

        # Build manifest and spine items
        manifest_items = []
        spine_items = []
        nav_points = []

        for i, (ch_title, ch_html) in enumerate(chapters):
            fname = f"chapter_{i:02d}.xhtml"
            item_id = f"ch{i}"

            xhtml = f'''<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>{ch_title}</title></head>
<body>
{ch_html}
</body>
</html>'''
            zf.writestr(f'OEBPS/{fname}', xhtml)
            manifest_items.append(
                f'    <item id="{item_id}" href="{fname}" media-type="application/xhtml+xml"/>'
            )
            spine_items.append(f'    <itemref idref="{item_id}"/>')
            nav_points.append(f'''    <navPoint id="nav{i}" playOrder="{i+1}">
      <navLabel><text>{ch_title}</text></navLabel>
      <content src="{fname}"/>
    </navPoint>''')

        # content.opf
        manifest_str = "\n".join(manifest_items)
        spine_str = "\n".join(spine_items)
        opf = f'''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>{title}</dc:title>
    <dc:creator>{author}</dc:creator>
    <dc:language>en</dc:language>
    <dc:identifier id="bookid">urn:uuid:test-1234</dc:identifier>
  </metadata>
  <manifest>
{manifest_str}
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
  </manifest>
  <spine toc="ncx">
{spine_str}
  </spine>
</package>'''
        zf.writestr('OEBPS/content.opf', opf)

        # toc.ncx
        nav_str = "\n".join(nav_points)
        ncx = f'''<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head>
    <meta name="dtb:uid" content="urn:uuid:test-1234"/>
  </head>
  <docTitle><text>{title}</text></docTitle>
  <navMap>
{nav_str}
  </navMap>
</ncx>'''
        zf.writestr('OEBPS/toc.ncx', ncx)

    return epub_path


@pytest.fixture
def minimal_epub(tmp_path):
    """Create a minimal test EPUB and return its path."""
    epub_path = str(tmp_path / "test_book.epub")
    _create_minimal_epub(epub_path)
    return epub_path


@pytest.fixture
def epub_with_images(tmp_path):
    """Create a test EPUB with an embedded image."""
    epub_path = str(tmp_path / "test_book_images.epub")
    chapters = [
        ("Chapter with Image",
         '<h1>Chapter with Image</h1>'
         '<p>Here is an image:</p>'
         '<img src="images/test_img.png" alt="Test Image"/>'
         '<p>And some text after the image.</p>'),
    ]

    with zipfile.ZipFile(epub_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        zf.writestr('META-INF/container.xml', '''<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>''')

        xhtml = f'''<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chapter with Image</title></head>
<body>
{chapters[0][1]}
</body>
</html>'''
        zf.writestr('OEBPS/chapter_00.xhtml', xhtml)

        # Create a test PNG image (10x10 pixel, red) -- must be >100 bytes
        # to pass the size filter in EPUBExtractor._extract_images
        import struct
        import zlib

        def _test_png():
            """Create a 50x50 noisy PNG (>100 bytes after compression)."""
            import os as _os
            width, height = 50, 50
            # IHDR
            ihdr_data = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
            ihdr_crc = zlib.crc32(b'IHDR' + ihdr_data) & 0xffffffff
            ihdr = struct.pack('>I', 13) + b'IHDR' + ihdr_data + struct.pack('>I', ihdr_crc)

            # IDAT: random pixel data (hard to compress -> bigger output)
            raw = b''
            for _ in range(height):
                raw += b'\x00' + _os.urandom(width * 3)
            compressed = zlib.compress(raw, 1)  # level 1 = fast, less compression
            idat_crc = zlib.crc32(b'IDAT' + compressed) & 0xffffffff
            idat = struct.pack('>I', len(compressed)) + b'IDAT' + compressed + struct.pack('>I', idat_crc)

            # IEND
            iend_crc = zlib.crc32(b'IEND') & 0xffffffff
            iend = struct.pack('>I', 0) + b'IEND' + struct.pack('>I', iend_crc)

            return b'\x89PNG\r\n\x1a\n' + ihdr + idat + iend

        png_data = _test_png()
        zf.writestr('OEBPS/images/test_img.png', png_data)

        opf = '''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Test Book with Images</dc:title>
    <dc:creator>Test Author</dc:creator>
    <dc:language>en</dc:language>
    <dc:identifier id="bookid">urn:uuid:test-img-1234</dc:identifier>
  </metadata>
  <manifest>
    <item id="ch0" href="chapter_00.xhtml" media-type="application/xhtml+xml"/>
    <item id="img1" href="images/test_img.png" media-type="image/png"/>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
  </manifest>
  <spine toc="ncx">
    <itemref idref="ch0"/>
  </spine>
</package>'''
        zf.writestr('OEBPS/content.opf', opf)

        ncx = '''<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="urn:uuid:test-img-1234"/></head>
  <docTitle><text>Test Book with Images</text></docTitle>
  <navMap>
    <navPoint id="nav0" playOrder="1">
      <navLabel><text>Chapter with Image</text></navLabel>
      <content src="chapter_00.xhtml"/>
    </navPoint>
  </navMap>
</ncx>'''
        zf.writestr('OEBPS/toc.ncx', ncx)

    return epub_path


# ---------------------------------------------------------------------------
# Tests: detect_input_format
# ---------------------------------------------------------------------------

class TestDetectInputFormat:
    """Test input format detection."""

    def test_pdf(self):
        from translate_book import detect_input_format
        assert detect_input_format("book.pdf") == "pdf"

    def test_epub(self):
        from translate_book import detect_input_format
        assert detect_input_format("book.epub") == "epub"

    def test_pdf_uppercase(self):
        from translate_book import detect_input_format
        assert detect_input_format("BOOK.PDF") == "pdf"

    def test_epub_uppercase(self):
        from translate_book import detect_input_format
        assert detect_input_format("BOOK.EPUB") == "epub"

    def test_unsupported_format(self):
        from translate_book import detect_input_format
        with pytest.raises(ValueError, match="Desteklenmeyen"):
            detect_input_format("book.txt")

    def test_no_extension(self):
        from translate_book import detect_input_format
        with pytest.raises(ValueError, match="Desteklenmeyen"):
            detect_input_format("book")


# ---------------------------------------------------------------------------
# Tests: EPUBExtractor
# ---------------------------------------------------------------------------

class TestEPUBExtractor:
    """Test EPUB extraction to markdown + images."""

    def test_extract_basic(self, minimal_epub, tmp_path):
        """Basic extraction produces markdown with content."""
        from latex_pipeline.epub_extractor import EPUBExtractor

        output_dir = tmp_path / "output"
        extractor = EPUBExtractor()
        result = extractor.extract(minimal_epub, str(output_dir))

        assert len(result.markdown) > 0
        assert "Introduction" in result.markdown
        assert "Chapter 1" in result.markdown
        assert "Chapter 2" in result.markdown

    def test_extract_metadata(self, minimal_epub, tmp_path):
        """Metadata is extracted correctly."""
        from latex_pipeline.epub_extractor import EPUBExtractor

        output_dir = tmp_path / "output"
        extractor = EPUBExtractor()
        result = extractor.extract(minimal_epub, str(output_dir))

        assert result.metadata["title"] == "Test Book"
        assert result.metadata["author"] == "Test Author"
        assert result.metadata["language"] == "en"
        assert result.metadata["chapters"] >= 1

    def test_extract_formatting(self, minimal_epub, tmp_path):
        """Bold and italic formatting is preserved in markdown."""
        from latex_pipeline.epub_extractor import EPUBExtractor

        output_dir = tmp_path / "output"
        extractor = EPUBExtractor()
        result = extractor.extract(minimal_epub, str(output_dir))

        # EPUBParser converts <b> to **text** and <i> to *text*
        assert "**bold**" in result.markdown
        assert "*italic*" in result.markdown

    def test_extract_list(self, minimal_epub, tmp_path):
        """List items are extracted."""
        from latex_pipeline.epub_extractor import EPUBExtractor

        output_dir = tmp_path / "output"
        extractor = EPUBExtractor()
        result = extractor.extract(minimal_epub, str(output_dir))

        assert "Item one" in result.markdown
        assert "Item two" in result.markdown

    def test_extract_images(self, epub_with_images, tmp_path):
        """Images are extracted from EPUB to output directory."""
        from latex_pipeline.epub_extractor import EPUBExtractor

        output_dir = tmp_path / "output"
        extractor = EPUBExtractor()
        result = extractor.extract(epub_with_images, str(output_dir))

        # Should have extracted at least 1 image
        assert len(result.images) >= 1
        # Image file should exist on disk
        for fname, fpath in result.images.items():
            assert fpath.exists(), f"Image {fname} not found at {fpath}"

    def test_extract_stats(self, minimal_epub, tmp_path):
        """Stats dict is populated."""
        from latex_pipeline.epub_extractor import EPUBExtractor

        output_dir = tmp_path / "output"
        extractor = EPUBExtractor()
        result = extractor.extract(minimal_epub, str(output_dir))

        assert "chapters" in result.stats
        assert "images_total" in result.stats
        assert "markdown_chars" in result.stats
        assert "time_s" in result.stats
        assert result.stats["markdown_chars"] > 0

    def test_file_not_found(self, tmp_path):
        """FileNotFoundError for missing EPUB."""
        from latex_pipeline.epub_extractor import EPUBExtractor

        extractor = EPUBExtractor()
        with pytest.raises(FileNotFoundError):
            extractor.extract("/nonexistent/book.epub", str(tmp_path))


# ---------------------------------------------------------------------------
# Tests: EPUB -> markdown -> LaTeX body (end-to-end, no translation)
# ---------------------------------------------------------------------------

class TestEPUBToLatex:
    """Test EPUB -> markdown -> LaTeX conversion."""

    def test_epub_to_latex_body(self, minimal_epub, tmp_path):
        """EPUB markdown can be converted to LaTeX body."""
        from latex_pipeline.epub_extractor import EPUBExtractor
        from latex_pipeline.config import PipelineConfig
        from latex_pipeline.converter import MarkdownToLatex

        # Step 1: Extract EPUB -> markdown
        output_dir = tmp_path / "output"
        extractor = EPUBExtractor()
        result = extractor.extract(minimal_epub, str(output_dir))

        # Step 2: Convert markdown -> LaTeX
        config = PipelineConfig(
            input_pdf="",  # No PDF for EPUB flow
            output_dir=str(output_dir),
            document_class="book",
        )
        converter = MarkdownToLatex(config)
        latex_body = converter.convert(result.markdown)

        # LaTeX body should contain chapter/section commands
        assert len(latex_body) > 0
        # Should have some structural LaTeX commands
        has_structure = any(cmd in latex_body for cmd in [
            "\\chapter", "\\section", "\\subsection",
            "\\textbf", "\\textit",
        ])
        assert has_structure, f"No structural LaTeX commands found in output:\n{latex_body[:500]}"

    def test_epub_to_full_document(self, minimal_epub, tmp_path):
        """EPUB markdown can be wrapped in a full LaTeX document."""
        from latex_pipeline.epub_extractor import EPUBExtractor
        from latex_pipeline.config import PipelineConfig
        from latex_pipeline.converter import MarkdownToLatex
        from latex_pipeline.template_engine import TemplateEngine

        # Step 1: Extract
        output_dir = tmp_path / "output"
        extractor = EPUBExtractor()
        result = extractor.extract(minimal_epub, str(output_dir))

        # Step 2: Convert
        config = PipelineConfig(
            input_pdf="",
            output_dir=str(output_dir),
            document_class="book",
        )
        converter = MarkdownToLatex(config)
        latex_body = converter.convert(result.markdown)

        # Step 3: Wrap in template
        engine = TemplateEngine(config)
        full_tex = engine.wrap_content(latex_body)

        assert "\\documentclass" in full_tex
        assert "\\begin{document}" in full_tex
        assert "\\end{document}" in full_tex
        # Content should be between begin and end document
        begin_pos = full_tex.index("\\begin{document}")
        end_pos = full_tex.index("\\end{document}")
        assert begin_pos < end_pos


# ---------------------------------------------------------------------------
# Tests: run_epub_extraction function
# ---------------------------------------------------------------------------

class TestRunEPUBExtraction:
    """Test the run_epub_extraction wrapper."""

    def test_run_epub_extraction(self, minimal_epub, tmp_path):
        """run_epub_extraction creates full_book.md."""
        from translate_book import run_epub_extraction

        output_dir = str(tmp_path / "output")
        Path(output_dir).mkdir(parents=True, exist_ok=True)

        ok = run_epub_extraction(minimal_epub, output_dir)
        assert ok is True

        md_path = Path(output_dir) / "full_book.md"
        assert md_path.exists()
        content = md_path.read_text(encoding="utf-8")
        assert len(content) > 0
        assert "Introduction" in content

    def test_run_epub_extraction_missing_file(self, tmp_path):
        """run_epub_extraction returns False for missing file."""
        from translate_book import run_epub_extraction

        output_dir = str(tmp_path / "output")
        Path(output_dir).mkdir(parents=True, exist_ok=True)

        ok = run_epub_extraction("/nonexistent/book.epub", output_dir)
        assert ok is False


# ---------------------------------------------------------------------------
# Tests: detect_book_type with EPUB filenames
# ---------------------------------------------------------------------------

class TestBookTypeDetection:
    """Test book type detection works for EPUB filenames."""

    def test_science_epub(self):
        from translate_book import detect_book_type
        assert detect_book_type("quantum_mechanics.epub") == "science"

    def test_novel_epub(self):
        from translate_book import detect_book_type
        assert detect_book_type("great_novel.epub") == "novel"

    def test_philosophy_epub(self):
        from translate_book import detect_book_type
        assert detect_book_type("stoicism_guide.epub") == "philosophy"

    def test_default_epub(self):
        from translate_book import detect_book_type
        # Unknown filename -> default to science
        assert detect_book_type("random_name.epub") == "science"

    def test_content_based_detection(self):
        from translate_book import detect_book_type
        md = "This book covers differential equations and quantum physics with many theorems."
        assert detect_book_type("unknown.epub", md) == "science"


# ---------------------------------------------------------------------------
# Tests: CLI argument parsing
# ---------------------------------------------------------------------------

class TestCLIArgs:
    """Test CLI argument parsing supports EPUB."""

    def test_epub_input_accepted(self):
        """CLI accepts .epub as input."""
        saved_argv = sys.argv
        try:
            sys.argv = ["translate_book.py", "test.epub"]
            from translate_book import parse_args
            args = parse_args()
            assert args.input == "test.epub"
        finally:
            sys.argv = saved_argv

    def test_pdf_input_accepted(self):
        """CLI still accepts .pdf as input (backward compat)."""
        saved_argv = sys.argv
        try:
            sys.argv = ["translate_book.py", "test.pdf"]
            from translate_book import parse_args
            args = parse_args()
            assert args.input == "test.pdf"
        finally:
            sys.argv = saved_argv

    def test_epub_with_options(self):
        """CLI accepts EPUB with all options."""
        saved_argv = sys.argv
        try:
            sys.argv = [
                "translate_book.py", "test.epub",
                "--name", "My Book",
                "--skip-translate",
                "--skip-compile",
                "--book-type", "novel",
            ]
            from translate_book import parse_args
            args = parse_args()
            assert args.input == "test.epub"
            assert args.name == "My Book"
            assert args.skip_translate is True
            assert args.skip_compile is True
            assert args.book_type == "novel"
        finally:
            sys.argv = saved_argv
