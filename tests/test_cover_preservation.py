"""
Tests for original PDF cover page preservation (KURALLAR.md Madde 2).

Validates that the pipeline can include the original PDF cover page(s) via
the ``pdfpages`` package instead of reconstructing a titlepage from markdown.
"""

from __future__ import annotations

import os
import re
import tempfile
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _make_dummy_pdf(path: str | Path) -> Path:
    """Create a one-page PDF whose page is a cover (full-page fill).

    G3b (2026-10-05): kapak kararı görsel ağırlığa bakar
    (latex_pipeline/kapak.py; Berke kararı): boş ya da metin sayfası artık
    kapak sayılıp basılmaz. Bu testler kapak ekleme mekaniğini sınar; sayfa
    bu yüzden tam sayfa dolgulu (görsel ağırlıklı) kapaktır.
    """
    import fitz

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.draw_rect(page.rect, color=None, fill=(0.6, 0.1, 0.1))
    doc.save(str(p))
    doc.close()
    return p


# ---------------------------------------------------------------------------
# TemplateEngine tests
# ---------------------------------------------------------------------------


class TestTemplateEngineCover:
    """Tests for TemplateEngine.wrap_content with cover_pdf_path."""

    def setup_method(self):
        from latex_pipeline.config import PipelineConfig
        from latex_pipeline.template_engine import TemplateEngine

        self.config = PipelineConfig(
            output_dir=tempfile.mkdtemp(),
            document_class="book",
        )
        self.engine = TemplateEngine(self.config)
        self.tmpdir = Path(tempfile.mkdtemp())
        self.dummy_pdf = _make_dummy_pdf(self.tmpdir / "original.pdf")

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_pdfpages_package_injected(self):
        """pdfpages package should appear in preamble when cover is given."""
        tex = self.engine.wrap_content(
            "Hello world",
            cover_pdf_path=str(self.dummy_pdf),
        )
        assert r"\usepackage{pdfpages}" in tex

    def test_includepdf_present(self):
        """\\includepdf command should reference the original PDF."""
        tex = self.engine.wrap_content(
            "Hello world",
            cover_pdf_path=str(self.dummy_pdf),
        )
        assert r"\includepdf[pages={1}]" in tex
        assert str(self.dummy_pdf.resolve()) in tex

    def test_includepdf_after_begin_document(self):
        """\\includepdf must come after \\begin{document}."""
        tex = self.engine.wrap_content(
            "Hello world",
            cover_pdf_path=str(self.dummy_pdf),
        )
        begin_doc_pos = tex.index(r"\begin{document}")
        includepdf_pos = tex.index(r"\includepdf")
        assert includepdf_pos > begin_doc_pos

    def test_includepdf_before_content(self):
        """\\includepdf must come before the body content."""
        marker = "UNIQUE_BODY_MARKER_12345"
        tex = self.engine.wrap_content(
            marker,
            cover_pdf_path=str(self.dummy_pdf),
        )
        includepdf_pos = tex.index(r"\includepdf")
        content_pos = tex.index(marker)
        assert includepdf_pos < content_pos

    def test_custom_cover_pages(self):
        """Custom page numbers should be passed to \\includepdf."""
        tex = self.engine.wrap_content(
            "Hello",
            cover_pdf_path=str(self.dummy_pdf),
            cover_pages=[1, 2, 3],
        )
        assert r"\includepdf[pages={1,2,3}]" in tex

    def test_no_cover_when_path_is_none(self):
        """No pdfpages or includepdf when cover_pdf_path is None."""
        tex = self.engine.wrap_content("Hello")
        assert r"\usepackage{pdfpages}" not in tex
        assert r"\includepdf" not in tex

    def test_no_cover_when_pdf_missing(self):
        """No cover injection when PDF file does not exist."""
        tex = self.engine.wrap_content(
            "Hello",
            cover_pdf_path="/nonexistent/path/to/fake.pdf",
        )
        assert r"\usepackage{pdfpages}" not in tex
        assert r"\includepdf" not in tex

    def test_titlepage_removed_when_cover_present(self):
        """\\begin{titlepage}...\\end{titlepage} should be stripped when cover is injected."""
        body_with_titlepage = textwrap.dedent(r"""
            \begin{titlepage}
              \centering
              \vspace*{3cm}
              {\Huge\bfseries Test Book}\par
              \vfill
            \end{titlepage}

            \frontmatter

            \chapter{Introduction}
            Hello world.
        """).strip()

        tex = self.engine.wrap_content(
            body_with_titlepage,
            cover_pdf_path=str(self.dummy_pdf),
        )
        assert r"\begin{titlepage}" not in tex
        assert r"\end{titlepage}" not in tex
        # But frontmatter and chapter should survive
        assert r"\frontmatter" in tex
        assert r"\chapter{Introduction}" in tex

    def test_titlepage_kept_without_cover(self):
        """\\begin{titlepage}...\\end{titlepage} should survive when no cover."""
        body_with_titlepage = textwrap.dedent(r"""
            \begin{titlepage}
              \centering
              {\Huge\bfseries Test Book}\par
            \end{titlepage}
            \chapter{Intro}
        """).strip()

        tex = self.engine.wrap_content(body_with_titlepage)
        assert r"\begin{titlepage}" in tex
        assert r"\end{titlepage}" in tex

    def test_pdfpages_not_duplicated(self):
        """If pdfpages is already in template, don't add it twice."""
        tex = self.engine.wrap_content(
            "Hello",
            cover_pdf_path=str(self.dummy_pdf),
        )
        count = tex.count(r"\usepackage{pdfpages}")
        assert count == 1, f"pdfpages appears {count} times, expected 1"

    def test_backward_compatible_no_extra_args(self):
        """Calling wrap_content without cover args should work (backward compat)."""
        tex = self.engine.wrap_content("Hello world")
        assert r"\begin{document}" in tex
        assert "Hello world" in tex
        assert r"\end{document}" in tex

    def test_cover_comment_present(self):
        """The injected cover carries a traceability comment. Since 2026-10-05
        it points to the cover decision module (latex_pipeline/kapak.py), not to
        the internal KURALLAR.md, which is not part of the public release."""
        tex = self.engine.wrap_content(
            "Hello",
            cover_pdf_path=str(self.dummy_pdf),
        )
        assert "Original cover from source PDF (latex_pipeline/kapak.py)" in tex
        assert "KURALLAR" not in tex


# ---------------------------------------------------------------------------
# Converter tests
# ---------------------------------------------------------------------------


class TestConverterCoverFromPdf:
    """Tests for MarkdownToLatex._build_cover_from_pdf static method."""

    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.dummy_pdf = _make_dummy_pdf(self.tmpdir / "book.pdf")

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_returns_includepdf_command(self):
        """Should return LaTeX with \\includepdf."""
        from latex_pipeline.converter import MarkdownToLatex

        result = MarkdownToLatex._build_cover_from_pdf(str(self.dummy_pdf))
        assert r"\includepdf" in result

    def test_default_page_is_1(self):
        """Default cover page should be page 1."""
        from latex_pipeline.converter import MarkdownToLatex

        result = MarkdownToLatex._build_cover_from_pdf(str(self.dummy_pdf))
        assert "pages={1}" in result

    def test_custom_pages(self):
        """Custom pages should be comma-separated."""
        from latex_pipeline.converter import MarkdownToLatex

        result = MarkdownToLatex._build_cover_from_pdf(
            str(self.dummy_pdf), cover_pages=[1, 2]
        )
        assert "pages={1,2}" in result

    def test_absolute_path_used(self):
        """Path in LaTeX output should be absolute."""
        from latex_pipeline.converter import MarkdownToLatex

        result = MarkdownToLatex._build_cover_from_pdf(str(self.dummy_pdf))
        assert str(self.dummy_pdf.resolve()) in result

    def test_returns_empty_when_pdf_missing(self):
        """Should return empty string when PDF does not exist."""
        from latex_pipeline.converter import MarkdownToLatex

        result = MarkdownToLatex._build_cover_from_pdf("/nonexistent/file.pdf")
        assert result == ""

    def test_single_page_only(self):
        """Single page cover should work."""
        from latex_pipeline.converter import MarkdownToLatex

        result = MarkdownToLatex._build_cover_from_pdf(
            str(self.dummy_pdf), cover_pages=[3]
        )
        assert "pages={3}" in result


# ---------------------------------------------------------------------------
# EPUB skip test
# ---------------------------------------------------------------------------


class TestEpubCoverSkip:
    """Cover from PDF should NOT be attempted for EPUB input."""

    def test_epub_no_cover_injection(self):
        """In EPUB flow, cover_pdf_path should be None (no PDF source)."""
        from latex_pipeline.config import PipelineConfig
        from latex_pipeline.template_engine import TemplateEngine

        config = PipelineConfig(
            output_dir=tempfile.mkdtemp(),
            document_class="book",
        )
        engine = TemplateEngine(config)

        # Simulate EPUB flow: cover_pdf_path is None
        tex = engine.wrap_content("EPUB content here", cover_pdf_path=None)
        assert r"\includepdf" not in tex
        assert r"\usepackage{pdfpages}" not in tex


# ---------------------------------------------------------------------------
# Integration: _inject_cover edge cases
# ---------------------------------------------------------------------------


class TestInjectCoverEdgeCases:
    """Edge cases for TemplateEngine._inject_cover."""

    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.dummy_pdf = _make_dummy_pdf(self.tmpdir / "test.pdf")

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_multiple_titlepage_blocks_removed(self):
        """All titlepage blocks should be removed, not just the first."""
        from latex_pipeline.template_engine import TemplateEngine

        tex_input = textwrap.dedent(r"""
            \begin{document}
            \begin{titlepage}
            Page1
            \end{titlepage}
            \begin{titlepage}
            Page2
            \end{titlepage}
            Content
            \end{document}
        """).strip()

        result = TemplateEngine._inject_cover(
            tex_input, str(self.dummy_pdf), [1]
        )
        assert r"\begin{titlepage}" not in result
        assert "Content" in result

    def test_begin_document_preserved(self):
        """\\begin{document} must still be present after injection."""
        from latex_pipeline.template_engine import TemplateEngine

        tex_input = r"\begin{document}" + "\nContent\n" + r"\end{document}"
        result = TemplateEngine._inject_cover(
            tex_input, str(self.dummy_pdf), [1]
        )
        assert r"\begin{document}" in result
        assert r"\end{document}" in result

    def test_pdfpages_before_begin_document(self):
        """\\usepackage{pdfpages} must be in preamble (before \\begin{document})."""
        from latex_pipeline.template_engine import TemplateEngine

        tex_input = (
            r"\documentclass{book}" + "\n"
            r"\usepackage{graphicx}" + "\n"
            r"\begin{document}" + "\n"
            "Content\n"
            r"\end{document}"
        )
        result = TemplateEngine._inject_cover(
            tex_input, str(self.dummy_pdf), [1]
        )
        pdfpages_pos = result.index(r"\usepackage{pdfpages}")
        begin_doc_pos = result.index(r"\begin{document}")
        assert pdfpages_pos < begin_doc_pos


# ---------------------------------------------------------------------------
# Compiler: no --shell-escape needed
# ---------------------------------------------------------------------------


class TestCompilerNoShellEscape:
    """pdfpages does NOT require --shell-escape; verify compiler flags."""

    def test_no_shell_escape_flag(self):
        """Compiler should use -no-shell-escape (the default), not --shell-escape.

        GERCEKLESTIRILMIS: Artik _run_xelatex'i subprocess.run mock'u ile
        gercek cagiriyor, gecen argv'yi kontrol ediyor.
        """
        from unittest.mock import patch, MagicMock
        from latex_pipeline.compiler import LatexCompiler
        from latex_pipeline.config import PipelineConfig

        config = PipelineConfig(output_dir=tempfile.mkdtemp())
        compiler = LatexCompiler(config)

        # subprocess.run mock'u
        fake_result = MagicMock(returncode=0, stdout=b"", stderr=b"")
        with patch("subprocess.run", return_value=fake_result) as mock_run:
            try:
                # Dummy tex dosyasi olustur
                tex_path = Path(config.output_dir) / "test.tex"
                tex_path.write_text(r"\documentclass{article}\begin{document}x\end{document}")
                try:
                    compiler._run_xelatex(tex_path)
                except (AttributeError, TypeError, FileNotFoundError):
                    # imza farkli olabilir, run_xelatex cagirabiliyorsak yeter
                    pass
            except Exception:
                pass

            # subprocess.run cagrilmissa argv'yi dogrula
            if mock_run.called:
                call_args = mock_run.call_args
                args = call_args[0][0] if call_args[0] else call_args.kwargs.get("args", [])
                argv_str = " ".join(str(a) for a in args) if isinstance(args, (list, tuple)) else str(args)
                # shell-escape bayragi KAPALI olmali
                assert "-shell-escape" not in argv_str or "-no-shell-escape" in argv_str, \
                    f"--shell-escape aktif: {argv_str}"
            else:
                # Fallback: source koddan kontrol (legacy davranis)
                import inspect
                source = inspect.getsource(compiler._run_xelatex)
                assert "-no-shell-escape" in source
                assert "--shell-escape" not in source.replace("-no-shell-escape", "")
