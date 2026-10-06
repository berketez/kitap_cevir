"""
Tests for _build_title_page bad title rejection.

When the first heading is a non-title heading (NOTE, Figure 1, etc.),
_build_title_page should return ("", 0) -- no titlepage generated.
Previously it would produce a titlepage with "Untitled" as the title.
"""

import sys
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from latex_pipeline.config import PipelineConfig
from latex_pipeline.converter import MarkdownToLatex


def _make_converter(**kwargs):
    """Create a MarkdownToLatex with default book config."""
    cfg = PipelineConfig(document_class="book", **kwargs)
    return MarkdownToLatex(cfg)


# ---------------------------------------------------------------
# Bad title -> NO titlepage (the main fix)
# ---------------------------------------------------------------

def test_bad_title_note_skips_titlepage():
    """#### **NOTE** heading should NOT generate a titlepage."""
    converter = _make_converter()
    lines = ["#### **NOTE**", "Some text about a note."]
    result, consumed = converter._build_title_page(lines)
    assert result == "", f"Expected empty titlepage, got: {result!r}"
    assert consumed == 0, f"Expected 0 lines consumed, got: {consumed}"


def test_bad_title_figure_skips_titlepage():
    """Figure 1 heading should NOT generate a titlepage."""
    converter = _make_converter()
    lines = ["# Figure 1", "Caption text."]
    result, consumed = converter._build_title_page(lines)
    assert result == "", f"Expected empty titlepage, got: {result!r}"
    assert consumed == 0, f"Expected 0 lines consumed, got: {consumed}"


def test_bad_title_table_skips_titlepage():
    """Table 3 heading should NOT generate a titlepage."""
    converter = _make_converter()
    lines = ["## Table 3", "Table content."]
    result, consumed = converter._build_title_page(lines)
    assert result == "", f"Expected empty titlepage, got: {result!r}"
    assert consumed == 0, f"Expected 0 lines consumed, got: {consumed}"


def test_bad_title_example_skips_titlepage():
    """Example 5 heading should NOT generate a titlepage."""
    converter = _make_converter()
    lines = ["### Example 5", "Example content."]
    result, consumed = converter._build_title_page(lines)
    assert result == "", f"Expected empty titlepage, got: {result!r}"
    assert consumed == 0, f"Expected 0 lines consumed, got: {consumed}"


def test_bad_title_section_number_skips_titlepage():
    """Section number heading like '2.3.1 Methods' should NOT generate a titlepage."""
    converter = _make_converter()
    lines = ["# 2.3.1 Methods", "Some methods text."]
    result, consumed = converter._build_title_page(lines)
    assert result == "", f"Expected empty titlepage, got: {result!r}"
    assert consumed == 0, f"Expected 0 lines consumed, got: {consumed}"


def test_bad_title_short_skips_titlepage():
    """Very short title (< 3 chars) should NOT generate a titlepage."""
    converter = _make_converter()
    lines = ["# AB", "Content."]
    result, consumed = converter._build_title_page(lines)
    assert result == "", f"Expected empty titlepage, got: {result!r}"
    assert consumed == 0, f"Expected 0 lines consumed, got: {consumed}"


# ---------------------------------------------------------------
# Good title -> titlepage generated (regression check)
# ---------------------------------------------------------------

def test_good_title_generates_titlepage():
    """A real book title should generate a titlepage."""
    converter = _make_converter()
    lines = [
        "# Deep Learning and Computational Physics",
        "",
        "Some intro text.",
    ]
    result, consumed = converter._build_title_page(lines)
    assert "\\begin{titlepage}" in result, "Expected titlepage to be generated"
    assert "Deep Learning and Computational Physics" in result
    assert consumed >= 1


def test_good_title_with_author():
    """Title + author lines should both be in the titlepage."""
    converter = _make_converter()
    lines = [
        "John Smith",
        "Jane Doe",
        "# Introduction to Machine Learning",
        "",
        "Chapter content.",
    ]
    result, consumed = converter._build_title_page(lines)
    assert "\\begin{titlepage}" in result
    assert "Introduction to Machine Learning" in result
    assert "John Smith" in result
    assert "Jane Doe" in result


# ---------------------------------------------------------------
# No heading at all -> no titlepage
# ---------------------------------------------------------------

def test_no_heading_skips_titlepage():
    """If there is no heading at all, no titlepage should be generated."""
    converter = _make_converter()
    lines = [
        "This is just a paragraph.",
        "Another paragraph.",
        "",
        "More text.",
    ]
    result, consumed = converter._build_title_page(lines)
    # No heading found -> title stays empty -> return ("", 0)
    assert result == "", f"Expected empty titlepage, got: {result!r}"
    assert consumed == 0


if __name__ == "__main__":
    tests = [
        test_bad_title_note_skips_titlepage,
        test_bad_title_figure_skips_titlepage,
        test_bad_title_table_skips_titlepage,
        test_bad_title_example_skips_titlepage,
        test_bad_title_section_number_skips_titlepage,
        test_bad_title_short_skips_titlepage,
        test_good_title_generates_titlepage,
        test_good_title_with_author,
        test_no_heading_skips_titlepage,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR {t.__name__}: {e}")
            failed += 1

    print(f"\n{passed}/{passed + failed} tests passed")
    if failed:
        sys.exit(1)
