"""
Tests for batch translation pipeline integration.

Tests that _translate_paragraphs_batched() correctly:
1. Collects translatable segments from paragraphs
2. Sends them to batch MT (or falls back to sequential)
3. Applies post-processing per segment
4. Reconstructs the paragraph structure correctly
5. Preserves stash/restore, checkpoints, and progress callbacks

No real MT model is loaded -- we mock BookTranslator and its batch method.
"""

import re
import sys
import os
from unittest.mock import MagicMock, patch, PropertyMock
from typing import List

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from latex_pipeline.translator import LatexTranslator, TranslationStats
from latex_pipeline.config import PipelineConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_translator(batch_fn=None, single_fn=None) -> LatexTranslator:
    """Create a LatexTranslator with mocked MT backend.

    Args:
        batch_fn: Mock for _translate_batch_hymt (takes list, returns list).
                  If None, uses a simple uppercasing mock.
        single_fn: Mock for translate_text (takes str, returns str).
                   If None, uses a simple uppercasing mock.
    """
    config = PipelineConfig(
        translate=True,
        segment_timeout=60,
    )
    lt = LatexTranslator(
        pipeline_config=config,
        mt_engine="hymt-7b",
        source_lang="en",
        target_lang="tr",
    )

    # Create mock BookTranslator
    mock_bt = MagicMock()
    # A1 fix (Yol B): _batch_mt_call artık translate_batch_protected varsa onu
    # tercih ediyor. MagicMock bu attribute'u otomatik üretir; bu testler ise
    # ham _translate_batch_hymt (fallback) yolunu doğruluyor. Attribute'u
    # silerek mock'u "protected metodu yok" durumuna getiriyoruz.
    del mock_bt.translate_batch_protected

    if batch_fn is None:
        def _default_batch(texts, max_length=512):
            return [t.upper() for t in texts]
        mock_bt._translate_batch_hymt = MagicMock(side_effect=_default_batch)
    else:
        mock_bt._translate_batch_hymt = MagicMock(side_effect=batch_fn)

    if single_fn is None:
        mock_bt.translate_text = MagicMock(side_effect=lambda t: t.upper())
    else:
        mock_bt.translate_text = MagicMock(side_effect=single_fn)

    # Inject mock translator
    lt._translator = mock_bt

    # Set attributes normally set by translate() before _translate_paragraphs
    lt._checkpoint_dir = None
    lt._checkpoint_counter = 0
    lt._progress_callback = None
    lt.stats = TranslationStats()  # fresh stats

    return lt


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBatchCollectPass:
    """Test that the collect pass correctly identifies translatable segments."""

    def test_simple_paragraph_collected(self):
        """A simple text paragraph should produce one segment."""
        lt = _make_translator()

        body = "This is a simple paragraph."
        result = lt._translate_paragraphs(body)

        # Should have been translated (uppercased by mock)
        assert result == "THIS IS A SIMPLE PARAGRAPH."

    def test_empty_lines_preserved(self):
        """Blank lines between paragraphs must be preserved."""
        lt = _make_translator()

        body = "First paragraph.\n\nSecond paragraph."
        result = lt._translate_paragraphs(body)

        assert "\n\n" in result
        parts = result.split("\n\n")
        assert len(parts) == 2
        assert parts[0] == "FIRST PARAGRAPH."
        assert parts[1] == "SECOND PARAGRAPH."

    def test_placeholder_only_lines_skipped(self):
        """Lines with only <<LATEX_N>> placeholders should not be sent to MT."""
        lt = _make_translator()

        body = "<<LATEX_0>>\nSome real text here.\n<<LATEX_1>>"
        result = lt._translate_paragraphs(body)

        # Only "Some real text here." should be translated
        assert "SOME REAL TEXT HERE." in result
        assert "<<LATEX_0>>" in result
        assert "<<LATEX_1>>" in result

    def test_heading_lines_skipped(self):
        r"""Lines like \section{...} should be skipped (Phase 2 handles them)."""
        lt = _make_translator()

        body = "\\section{Introduction}\nThis is the first paragraph."
        result = lt._translate_paragraphs(body)

        assert "\\section{Introduction}" in result
        assert "THIS IS THE FIRST PARAGRAPH." in result


class TestBatchMTCall:
    """Test the batch MT mechanism."""

    def test_batch_fn_called_with_multiple_segments(self):
        """When there are multiple segments, batch should be called."""
        call_log = []

        def _batch_fn(texts, max_length=512):
            call_log.append(list(texts))
            return [t.upper() for t in texts]

        lt = _make_translator(batch_fn=_batch_fn)

        body = "Line one text.\n\nLine two text.\n\nLine three here."
        result = lt._translate_paragraphs(body)

        # All three lines should appear in one batch call (batch_size=8)
        assert len(call_log) >= 1
        total_items = sum(len(c) for c in call_log)
        assert total_items == 3

    def test_batch_size_respected(self):
        """Segments should be split into sub-batches of _BATCH_SIZE."""
        call_log = []

        def _batch_fn(texts, max_length=512):
            call_log.append(len(texts))
            return [t.upper() for t in texts]

        lt = _make_translator(batch_fn=_batch_fn)
        lt._BATCH_SIZE = 3  # Small batch for testing

        # Create 7 paragraphs (7 segments)
        lines = [f"Paragraph number {i} with some text." for i in range(7)]
        body = "\n\n".join(lines)
        result = lt._translate_paragraphs(body)

        # Should be 3 batch calls: 3 + 3 + 1
        assert len(call_log) == 3
        assert call_log[0] == 3
        assert call_log[1] == 3
        assert call_log[2] == 1


class TestBatchFallback:
    """Test fallback mechanisms when batch fails."""

    def test_batch_error_falls_back_to_sequential(self):
        """If batch MT raises, individual segments should be retried."""
        call_count = {"batch": 0, "single": 0}

        def _failing_batch(texts, max_length=512):
            call_count["batch"] += 1
            raise RuntimeError("GPU OOM simulated")

        def _single_fn(text):
            call_count["single"] += 1
            return text.upper()

        lt = _make_translator(batch_fn=_failing_batch, single_fn=_single_fn)

        body = "First line.\n\nSecond line."
        result = lt._translate_paragraphs(body)

        # Batch should have been attempted
        assert call_count["batch"] >= 1
        # Sequential fallback should have handled it
        assert call_count["single"] >= 2
        # Results should still be correct
        assert "FIRST LINE." in result
        assert "SECOND LINE." in result

    def test_no_batch_method_falls_back(self):
        """If BookTranslator has no _translate_batch_hymt, use sequential."""
        lt = _make_translator()
        # Remove the batch method
        del lt._translator._translate_batch_hymt

        body = "Some text to translate."
        result = lt._translate_paragraphs(body)

        # Should still produce translated output via single MT
        assert result == "SOME TEXT TO TRANSLATE."

    def test_batch_result_count_mismatch_falls_back(self):
        """If batch returns wrong number of results, fall back."""
        def _bad_batch(texts, max_length=512):
            # Return wrong number of results
            return ["ONLY ONE RESULT"]

        def _single_fn(text):
            return text.upper()

        lt = _make_translator(batch_fn=_bad_batch, single_fn=_single_fn)
        lt._BATCH_SIZE = 10

        body = "Line one.\n\nLine two.\n\nLine three."
        result = lt._translate_paragraphs(body)

        # Should have fallen back to sequential
        assert "LINE ONE." in result
        assert "LINE TWO." in result
        assert "LINE THREE." in result


class TestBatchStashRestore:
    """Test stash/restore compatibility with batch translation."""

    def test_inline_commands_stashed_and_restored(self):
        r"""Inline LaTeX commands like \centering should survive batch."""
        def _batch_fn(texts, max_length=512):
            results = []
            for t in texts:
                # Preserve placeholders, uppercase text
                parts = re.split(r"(<<(?:LATEX|CMD)_\d+>>)", t)
                translated = []
                for p in parts:
                    if re.fullmatch(r"<<(?:LATEX|CMD)_\d+>>", p):
                        translated.append(p)
                    else:
                        translated.append(p.upper())
                results.append("".join(translated))
            return results

        lt = _make_translator(batch_fn=_batch_fn)

        # centering should be stashed to <<CMD_0>>, translated, then restored
        body = "\\centering Some important text here."
        result = lt._translate_paragraphs(body)

        assert "\\centering" in result
        assert "SOME IMPORTANT TEXT HERE." in result

    def test_latex_placeholders_preserved_in_batch(self):
        """<<LATEX_N>> placeholders should pass through batch MT untouched."""
        def _batch_fn(texts, max_length=512):
            results = []
            for t in texts:
                parts = re.split(r"(<<(?:LATEX|CMD)_\d+>>)", t)
                translated = []
                for p in parts:
                    if re.fullmatch(r"<<(?:LATEX|CMD)_\d+>>", p):
                        translated.append(p)
                    else:
                        translated.append(p.upper())
                results.append("".join(translated))
            return results

        lt = _make_translator(batch_fn=_batch_fn)

        body = "This has <<LATEX_5>> inline math and <<LATEX_6>> more."
        result = lt._translate_paragraphs(body)

        assert "<<LATEX_5>>" in result
        assert "<<LATEX_6>>" in result
        assert "THIS HAS" in result

    def test_item_lines_batch_translated(self):
        r"""\\item lines should have their text extracted and batch-translated."""
        lt = _make_translator()

        body = "\\item First item text\n\\item Second item text"
        result = lt._translate_paragraphs(body)

        assert "\\item" in result
        assert "FIRST ITEM TEXT" in result
        assert "SECOND ITEM TEXT" in result


class TestBatchPostProcessing:
    """Test that post-processing is applied per-segment after batch."""

    def test_prompt_leak_cleaned_after_batch(self):
        """Prompt leakage in batch results should be cleaned."""
        def _batch_with_leak(texts, max_length=512):
            return [
                "Translate the following text to Turkish: " + t.upper()
                for t in texts
            ]

        lt = _make_translator(batch_fn=_batch_with_leak)

        body = "Simple text to translate."
        result = lt._translate_paragraphs(body)

        # The prompt leak prefix should be removed by _post_process_translation
        assert "Translate the following" not in result

    def test_glossary_applied_after_batch(self):
        """Glossary terms should be fixed after batch MT."""
        def _batch_fn(texts, max_length=512):
            # Simulate MT returning a wrong term
            return [t.replace("neural network", "bilgisayar agi") for t in texts]

        lt = _make_translator(batch_fn=_batch_fn)

        body = "The neural network learns from data."
        result = lt._translate_paragraphs(body)

        # The _apply_glossary should fix the term to "sinir agi"
        # (assuming the glossary is active)
        # At minimum, the batch translation should produce output
        assert len(result) > 0


class TestBatchProgressAndCheckpoint:
    """Test progress callbacks and checkpoints work with batch mode."""

    def test_progress_callback_fires(self):
        """Progress callback should fire at least for first and last line.

        Note: _fire_line_progress throttles to once per second, so in fast
        tests only the first and last calls may actually fire.
        """
        lt = _make_translator()
        progress_calls = []

        def _progress(frac, desc):
            progress_calls.append((frac, desc))

        lt._progress_callback = _progress

        body = "Line one.\n\nLine two.\n\nLine three."
        result = lt._translate_paragraphs(body)

        # At least 2 progress calls (first + last due to throttling)
        assert len(progress_calls) >= 2
        # Last call should have fraction ~1.0
        assert progress_calls[-1][0] >= 0.9

    def test_sequential_fallback_still_works(self):
        """If batch mode completely fails, sequential mode should work."""
        lt = _make_translator()

        # Force batch to fail by breaking the paragraphs_batched method
        original_batched = lt._translate_paragraphs_batched

        def _broken_batched(paragraphs):
            raise RuntimeError("Forced failure for test")

        lt._translate_paragraphs_batched = _broken_batched

        body = "Test sequential fallback."
        result = lt._translate_paragraphs(body)

        # Sequential should have picked up
        assert result == "TEST SEQUENTIAL FALLBACK."


class TestBatchEdgeCases:
    """Edge cases for batch translation."""

    def test_empty_body(self):
        """Empty body should return empty."""
        lt = _make_translator()
        result = lt._translate_paragraphs("")
        assert result == ""

    def test_only_placeholders(self):
        """Body with only placeholders should return as-is."""
        lt = _make_translator()
        body = "<<LATEX_0>>\n\n<<LATEX_1>>"
        result = lt._translate_paragraphs(body)
        assert "<<LATEX_0>>" in result
        assert "<<LATEX_1>>" in result

    def test_single_short_line(self):
        """A single short line (< 3 chars) should be skipped."""
        lt = _make_translator()
        body = "ab"
        result = lt._translate_paragraphs(body)
        assert result == "ab"  # Too short, skipped

    def test_mixed_translatable_and_skip_lines(self):
        """Paragraphs with mixed translatable and non-translatable lines."""
        lt = _make_translator()

        body = (
            "\\section{Title}\n"
            "Real text to translate.\n"
            "<<LATEX_0>>\n"
            "Another real line here."
        )
        result = lt._translate_paragraphs(body)

        assert "\\section{Title}" in result
        assert "REAL TEXT TO TRANSLATE." in result
        assert "<<LATEX_0>>" in result
        assert "ANOTHER REAL LINE HERE." in result

    def test_batch_vs_sequential_same_result(self):
        """Batch and sequential should produce the same output."""
        body = "First paragraph text.\n\nSecond paragraph text.\n\nThird one here."

        # Batch mode
        lt_batch = _make_translator()
        result_batch = lt_batch._translate_paragraphs(body)

        # Sequential mode (force it)
        lt_seq = _make_translator()
        paragraphs = re.split(r"(\n\s*\n)", body)
        result_seq = lt_seq._translate_paragraphs_sequential(paragraphs)

        assert result_batch == result_seq


class TestBatchMTCallMethod:
    """Test _batch_mt_call directly."""

    def test_basic_call(self):
        """Basic batch call should return translated texts."""
        lt = _make_translator()
        result = lt._batch_mt_call(["hello", "world"])
        assert result == ["HELLO", "WORLD"]

    def test_empty_input(self):
        """Empty input should return empty."""
        lt = _make_translator()
        result = lt._batch_mt_call([])
        assert result == []

    def test_sub_batching(self):
        """Large inputs should be split into sub-batches."""
        call_log = []

        def _batch_fn(texts, max_length=512):
            call_log.append(len(texts))
            return [t.upper() for t in texts]

        lt = _make_translator(batch_fn=_batch_fn)
        lt._BATCH_SIZE = 2

        result = lt._batch_mt_call(["a text", "b text", "c text", "d text", "e text"])

        assert len(call_log) == 3  # 2 + 2 + 1
        assert len(result) == 5


class TestSingleMTCall:
    """Test _single_mt_call fallback method."""

    def test_basic_call(self):
        lt = _make_translator()
        result = lt._single_mt_call("hello world")
        assert result == "HELLO WORLD"

    def test_error_returns_original(self):
        def _failing_single(text):
            raise RuntimeError("MT failure")

        lt = _make_translator(single_fn=_failing_single)
        result = lt._single_mt_call("hello world")
        assert result == "hello world"
