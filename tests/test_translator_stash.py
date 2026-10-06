r"""
Tests for translator.py stash/restore mechanism.

Specifically tests:
1. \href{url}{display} protection and display text translation
2. \textit{long content} wrapper preservation
3. \textbf{long content} wrapper preservation
4. \ref, \label, \cite protection
5. Stash ordering correctness
"""

import re
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from latex_pipeline.translator import (
    _HREF_RE,
    _TEXTIT_SHORT_RE,
    _TEXTBF_SHORT_RE,
    _TEXTIT_LONG_RE,
    _TEXTBF_LONG_RE,
    _REF_RE,
    _MATH_ENV_RE,
    _INLINE_MATH_RE,
    _DISPLAY_MATH_BRACKET_RE,
    _CODE_ENV_RE,
    _TEXTTT_RE,
    _INCLUDEGRAPHICS_RE,
    _FIGURE_REF_RE,
    _FIGURE_LABEL_MAP,
    _translate_figure_ref,
    _PAREN_REF_RE,
    _AUTHOR_ETAL_RE,
    _AUTHOR_YEAR_RE,
)


# ---------------------------------------------------------------------------
# 1. Regex pattern tests (no MT model needed)
# ---------------------------------------------------------------------------

class TestHrefRegex:
    r"""Test that _HREF_RE correctly matches \href patterns."""

    def test_simple_href(self):
        text = r"\href{https://example.com}{Click here}"
        m = _HREF_RE.search(text)
        assert m is not None, f"Failed to match: {text}"
        assert m.group(0) == text

    def test_href_with_nested_braces(self):
        text = r"\href{https://example.com}{\textbf{Bold link}}"
        m = _HREF_RE.search(text)
        assert m is not None, f"Failed to match: {text}"
        assert m.group(0) == text

    def test_href_with_spaces(self):
        text = r"\href{https://example.com}{Some longer display text}"
        m = _HREF_RE.search(text)
        assert m is not None, f"Failed to match: {text}"

    def test_href_in_context(self):
        text = r"See \href{https://doi.org/10.1234}{Smith et al.} for details."
        m = _HREF_RE.search(text)
        assert m is not None
        assert m.group(0) == r"\href{https://doi.org/10.1234}{Smith et al.}"


class TestTextitRegex:
    """Test \textit regex patterns."""

    def test_short_textit_matches_short(self):
        """Short regex should match 1-8 char no-space content."""
        assert _TEXTIT_SHORT_RE.search(r"\textit{x}")
        assert _TEXTIT_SHORT_RE.search(r"\textit{Re}")
        assert _TEXTIT_SHORT_RE.search(r"\textit{f(x)}")
        # Should NOT match long content or content with spaces
        assert _TEXTIT_SHORT_RE.search(r"\textit{a long phrase}") is None

    def test_long_textit_matches_long(self):
        """Long regex should match any textit content."""
        m = _TEXTIT_LONG_RE.search(r"\textit{the quick brown fox}")
        assert m is not None
        assert m.group(1) == "the quick brown fox"

    def test_long_textit_with_nested(self):
        """Long regex should handle one level of nesting."""
        text = r"\textit{the \emph{function} value}"
        m = _TEXTIT_LONG_RE.search(text)
        assert m is not None
        assert m.group(0) == text

    def test_long_textit_captures_group(self):
        """Group 1 should be the inner content."""
        m = _TEXTIT_LONG_RE.search(r"\textit{some italic text}")
        assert m is not None
        assert m.group(1) == "some italic text"


class TestTextbfRegex:
    """Test \textbf regex patterns."""

    def test_short_textbf_matches_short(self):
        assert _TEXTBF_SHORT_RE.search(r"\textbf{Fig. 5.1}")
        assert _TEXTBF_SHORT_RE.search(r"\textbf{Note}")

    def test_long_textbf_matches_long(self):
        m = _TEXTBF_LONG_RE.search(r"\textbf{This is a bold sentence}")
        assert m is not None
        assert m.group(1) == "This is a bold sentence"


class TestRefRegex:
    r"""Test that \ref, \label, \cite are matched by _REF_RE."""

    def test_ref(self):
        assert _REF_RE.search(r"\ref{fig:example}")

    def test_label(self):
        assert _REF_RE.search(r"\label{sec:intro}")

    def test_cite(self):
        assert _REF_RE.search(r"\cite{bishop2006}")

    def test_eqref(self):
        assert _REF_RE.search(r"\eqref{eq:navier}")

    def test_pageref(self):
        assert _REF_RE.search(r"\pageref{ch:results}")

    def test_autoref(self):
        assert _REF_RE.search(r"\autoref{tab:data}")


# ---------------------------------------------------------------------------
# 2. Stash ordering tests (simulate _stash_protected logic)
# ---------------------------------------------------------------------------

class TestStashOrdering:
    """Verify stash ordering prevents conflicts."""

    def _simulate_stash(self, text: str):
        """Simulate the stash mechanism from _stash_protected.

        Follows the EXACT order from translator.py _stash_protected:
        - Author/citation patterns run BEFORE long textit/textbf so that
          \\textit{et al.} inside author citations is captured as part of
          the citation, not as a standalone textit.
        """
        stash = []

        def _replace(match):
            idx = len(stash)
            stash.append(match.group(0))
            return f"<<LATEX_{idx}>>"

        # Follow the EXACT order from translator.py _stash_protected
        text = _MATH_ENV_RE.sub(_replace, text)
        text = _CODE_ENV_RE.sub(_replace, text)
        text = _DISPLAY_MATH_BRACKET_RE.sub(_replace, text)
        text = _INLINE_MATH_RE.sub(lambda m: _replace(m), text)
        text = _INCLUDEGRAPHICS_RE.sub(_replace, text)
        text = _TEXTTT_RE.sub(_replace, text)
        text = _TEXTIT_SHORT_RE.sub(_replace, text)
        text = _TEXTBF_SHORT_RE.sub(_replace, text)
        # Author/citation stash BEFORE long textit/textbf
        text = _HREF_RE.sub(_replace, text)
        text = _REF_RE.sub(_replace, text)
        text = _PAREN_REF_RE.sub(_replace, text)
        text = _AUTHOR_ETAL_RE.sub(_replace, text)
        text = _AUTHOR_YEAR_RE.sub(_replace, text)
        # Long textit/textbf AFTER author stash
        text = _TEXTIT_LONG_RE.sub(_replace, text)
        text = _TEXTBF_LONG_RE.sub(_replace, text)

        return text, stash

    def test_href_stashed_before_ref(self):
        """href should be stashed as a whole unit, not broken by ref."""
        text = r"See \href{https://example.com}{the paper} and \ref{fig:1}."
        result, stash = self._simulate_stash(text)

        # Both should be stashed
        assert "\\href" not in result
        assert "\\ref" not in result
        # href should be fully preserved in stash
        href_items = [s for s in stash if s.startswith("\\href")]
        assert len(href_items) == 1
        assert href_items[0] == r"\href{https://example.com}{the paper}"

    def test_textit_short_stashed_before_long(self):
        """Short textit stashed first, then long textit gets the rest."""
        text = r"\textit{x} and \textit{the italic phrase}"
        result, stash = self._simulate_stash(text)

        assert "\\textit" not in result
        # Short one should be in stash
        assert r"\textit{x}" in stash
        # Long one should also be in stash
        assert r"\textit{the italic phrase}" in stash

    def test_textbf_preserved_not_converted(self):
        """textbf should stay textbf in stash, not become textit."""
        text = r"\textbf{The bold conclusion} and \textit{the italic note}"
        result, stash = self._simulate_stash(text)

        bf_items = [s for s in stash if "\\textbf" in s]
        it_items = [s for s in stash if "\\textit" in s]

        assert len(bf_items) >= 1, "textbf should be stashed"
        assert len(it_items) >= 1, "textit should be stashed"
        # Key test: no cross-contamination
        for item in bf_items:
            assert "\\textit" not in item
        for item in it_items:
            assert "\\textbf" not in item

    def test_math_inside_textit_stashed_first(self):
        """Math inside textit should be stashed as math, not as textit."""
        text = r"\textit{where $x$ is the variable}"
        result, stash = self._simulate_stash(text)

        # $x$ should be stashed first (as inline math)
        math_items = [s for s in stash if s.startswith("$")]
        assert len(math_items) == 1
        assert math_items[0] == "$x$"

    def test_href_with_ref_inside_url(self):
        """A URL that looks like a ref should not break href stash."""
        text = r"\href{https://arxiv.org/abs/2301.12345}{the reference}"
        result, stash = self._simulate_stash(text)
        assert "\\href" not in result
        # The full href should be in stash
        href_items = [s for s in stash if "\\href" in s]
        assert len(href_items) == 1

    def test_all_refs_protected(self):
        """All reference types should be stashed."""
        text = (
            r"\label{sec:intro} and \ref{sec:intro} "
            r"and \cite{bishop2006} and \eqref{eq:1}"
        )
        result, stash = self._simulate_stash(text)
        assert "\\label" not in result
        assert "\\ref" not in result
        assert "\\cite" not in result
        assert "\\eqref" not in result

    def test_restore_reverses_stash(self):
        """Restoring stash should reproduce the original."""
        text = (
            r"The \textit{gradient descent} method uses "
            r"$\nabla f$ to minimize \textbf{the loss function}."
        )
        result, stash = self._simulate_stash(text)

        # Restore (reverse order like _restore_stash)
        for idx in range(len(stash) - 1, -1, -1):
            placeholder = f"<<LATEX_{idx}>>"
            result = result.replace(placeholder, stash[idx])

        assert result == text, f"Restore mismatch:\n  Got: {result}\n  Expected: {text}"


# ---------------------------------------------------------------------------
# 3. Figure / Table reference tests
# ---------------------------------------------------------------------------

class TestFigureRefRegex:
    """Test _FIGURE_REF_RE pattern matching."""

    def test_figure_simple(self):
        m = _FIGURE_REF_RE.search("as shown in Figure 6.11 and")
        assert m is not None
        assert m.group(0) == "Figure 6.11"

    def test_figure_with_letter(self):
        m = _FIGURE_REF_RE.search("see Figure 3.2a for")
        assert m is not None
        assert m.group(0) == "Figure 3.2a"

    def test_fig_dot(self):
        m = _FIGURE_REF_RE.search("as in Fig. 5.1")
        assert m is not None
        assert m.group(0) == "Fig. 5.1"

    def test_table(self):
        m = _FIGURE_REF_RE.search("Table 3.2 shows")
        assert m is not None
        assert m.group(0) == "Table 3.2"

    def test_equation(self):
        m = _FIGURE_REF_RE.search("from Equation 4.5 we get")
        assert m is not None
        assert m.group(0) == "Equation 4.5"

    def test_eq_dot(self):
        m = _FIGURE_REF_RE.search("Eq. 2.1 gives")
        assert m is not None
        assert m.group(0) == "Eq. 2.1"

    def test_theorem(self):
        m = _FIGURE_REF_RE.search("By Theorem 3.1")
        assert m is not None
        assert m.group(0) == "Theorem 3.1"

    def test_section(self):
        m = _FIGURE_REF_RE.search("Section 2.3 covers")
        assert m is not None
        assert m.group(0) == "Section 2.3"

    def test_chapter(self):
        m = _FIGURE_REF_RE.search("Chapter 5 discusses")
        assert m is not None
        assert m.group(0) == "Chapter 5"

    def test_appendix(self):
        m = _FIGURE_REF_RE.search("see Appendix 1")
        assert m is not None
        assert m.group(0) == "Appendix 1"

    def test_definition(self):
        m = _FIGURE_REF_RE.search("Definition 2.1 states")
        assert m is not None
        assert m.group(0) == "Definition 2.1"

    def test_exercise(self):
        m = _FIGURE_REF_RE.search("Exercise 6.7 asks")
        assert m is not None
        assert m.group(0) == "Exercise 6.7"

    def test_algorithm(self):
        m = _FIGURE_REF_RE.search("Algorithm 3.1 shows")
        assert m is not None
        assert m.group(0) == "Algorithm 3.1"

    def test_proposition(self):
        m = _FIGURE_REF_RE.search("Proposition 4.2 implies")
        assert m is not None
        assert m.group(0) == "Proposition 4.2"

    def test_no_match_plain_word(self):
        """Should not match words that are not ref labels."""
        m = _FIGURE_REF_RE.search("The figure below is nice")
        # "figure" (lowercase) should not match
        assert m is None

    def test_multiple_in_one_string(self):
        text = "Figure 6.11 and Table 3.2 and Equation 4.5"
        matches = _FIGURE_REF_RE.findall(text)
        assert len(matches) == 3

    def test_deep_numbering(self):
        m = _FIGURE_REF_RE.search("Figure 12.3.4")
        assert m is not None
        assert m.group(0) == "Figure 12.3.4"


class TestFigureRefTranslation:
    """Test _translate_figure_ref produces correct Turkish forms."""

    def test_figure_to_sekil(self):
        m = _FIGURE_REF_RE.search("Figure 6.11")
        assert m is not None
        result = _translate_figure_ref(m)
        assert "6.11" in result
        assert "\u015eekil" in result  # Sekil with Turkish S

    def test_table_to_tablo(self):
        m = _FIGURE_REF_RE.search("Table 3.2")
        assert m is not None
        result = _translate_figure_ref(m)
        assert "3.2" in result
        assert "Tablo" in result

    def test_equation_to_denklem(self):
        m = _FIGURE_REF_RE.search("Equation 4.5")
        assert m is not None
        result = _translate_figure_ref(m)
        assert "4.5" in result
        assert "Denklem" in result

    def test_eq_dot_to_denk(self):
        m = _FIGURE_REF_RE.search("Eq. 2.1")
        assert m is not None
        result = _translate_figure_ref(m)
        assert "2.1" in result
        assert "Denk." in result

    def test_fig_dot_to_sek(self):
        m = _FIGURE_REF_RE.search("Fig. 5.1")
        assert m is not None
        result = _translate_figure_ref(m)
        assert "5.1" in result
        assert "\u015eek." in result  # Sek. with Turkish S

    def test_theorem_to_teorem(self):
        m = _FIGURE_REF_RE.search("Theorem 3.1")
        assert m is not None
        result = _translate_figure_ref(m)
        assert "3.1" in result
        assert "Teorem" in result

    def test_section_to_bolum(self):
        m = _FIGURE_REF_RE.search("Section 2.3")
        assert m is not None
        result = _translate_figure_ref(m)
        assert "2.3" in result
        assert "B\u00f6l\u00fcm" in result  # Bolum

    def test_number_preserved_exactly(self):
        """The number portion must not change at all."""
        m = _FIGURE_REF_RE.search("Figure 12.3.4a")
        assert m is not None
        result = _translate_figure_ref(m)
        assert "12.3.4a" in result

    def test_chapter_to_bolum(self):
        m = _FIGURE_REF_RE.search("Chapter 5")
        assert m is not None
        result = _translate_figure_ref(m)
        assert "5" in result
        assert "B\u00f6l\u00fcm" in result

    def test_exercise_to_alistirma(self):
        m = _FIGURE_REF_RE.search("Exercise 6.7")
        assert m is not None
        result = _translate_figure_ref(m)
        assert "6.7" in result
        assert "Al\u0131\u015ft\u0131rma" in result  # Alistirma

    def test_algorithm_to_algoritma(self):
        m = _FIGURE_REF_RE.search("Algorithm 3.1")
        assert m is not None
        result = _translate_figure_ref(m)
        assert "3.1" in result
        assert "Algoritma" in result

    def test_proposition_to_onerme(self):
        m = _FIGURE_REF_RE.search("Proposition 4.2")
        assert m is not None
        result = _translate_figure_ref(m)
        assert "4.2" in result
        assert "\u00d6nerme" in result  # Onerme

    def test_exercise_numbers_not_collapsed(self):
        """Textbook bug: Exercise 6.7, 6.5, 6.4 all became 6.4."""
        text = "Exercise 6.7, Exercise 6.5, and Exercise 6.4"
        matches = _FIGURE_REF_RE.findall(text)
        assert len(matches) == 3
        # Each number must be distinct
        numbers = [m.split()[-1] for m in matches]
        assert "6.7" in numbers
        assert "6.5" in numbers
        assert "6.4" in numbers

    def test_definition_numbers_not_collapsed(self):
        """Linear Algebra bug: Definition 2.12, 2.14, 2.21 all became 2.12."""
        text = "Definition 2.12, Definition 2.14, and Definition 2.21"
        matches = _FIGURE_REF_RE.findall(text)
        assert len(matches) == 3
        numbers = [m.split()[-1] for m in matches]
        assert "2.12" in numbers
        assert "2.14" in numbers
        assert "2.21" in numbers


class TestFigureRefStashIntegration:
    """Test figure refs are properly stashed and restored."""

    def _simulate_stash_with_figures(self, text: str):
        """Simulate stash mechanism including figure refs."""
        stash = []

        def _replace(match):
            idx = len(stash)
            stash.append(match.group(0))
            return f"<<LATEX_{idx}>>"

        def _replace_figure_ref(match):
            idx = len(stash)
            translated = _translate_figure_ref(match)
            stash.append(translated)
            return f"<<LATEX_{idx}>>"

        # Stash order matches translator.py _stash_protected
        text = _MATH_ENV_RE.sub(_replace, text)
        text = _DISPLAY_MATH_BRACKET_RE.sub(_replace, text)
        text = _INLINE_MATH_RE.sub(lambda m: _replace(m), text)
        text = _INCLUDEGRAPHICS_RE.sub(_replace, text)
        text = _TEXTTT_RE.sub(_replace, text)
        text = _TEXTIT_SHORT_RE.sub(_replace, text)
        text = _TEXTBF_SHORT_RE.sub(_replace, text)
        # Author/citation BEFORE long textit/textbf
        text = _HREF_RE.sub(_replace, text)
        text = _REF_RE.sub(_replace, text)
        text = _PAREN_REF_RE.sub(_replace, text)
        text = _AUTHOR_ETAL_RE.sub(_replace, text)
        text = _AUTHOR_YEAR_RE.sub(_replace, text)
        text = _TEXTIT_LONG_RE.sub(_replace, text)
        text = _TEXTBF_LONG_RE.sub(_replace, text)
        # Figure refs with translation
        text = _FIGURE_REF_RE.sub(_replace_figure_ref, text)

        return text, stash

    def test_figure_stashed(self):
        text = "as shown in Figure 6.11 and Table 3.2"
        result, stash = self._simulate_stash_with_figures(text)
        assert "<<LATEX_" in result
        assert "Figure" not in result
        assert "Table" not in result

    def test_figure_restore_gives_turkish(self):
        text = "as shown in Figure 6.11 and Table 3.2"
        result, stash = self._simulate_stash_with_figures(text)
        # Restore
        for idx in range(len(stash) - 1, -1, -1):
            placeholder = f"<<LATEX_{idx}>>"
            result = result.replace(placeholder, stash[idx])
        assert "\u015eekil" in result  # Sekil
        assert "6.11" in result
        assert "Tablo" in result
        assert "3.2" in result

    def test_figure_in_sentence(self):
        text = "The results in Figure 6.11 confirm Equation 4.5."
        result, stash = self._simulate_stash_with_figures(text)
        # All refs should be stashed
        assert "Figure" not in result
        assert "Equation" not in result
        # Restore gives Turkish
        for idx in range(len(stash) - 1, -1, -1):
            placeholder = f"<<LATEX_{idx}>>"
            result = result.replace(placeholder, stash[idx])
        assert "\u015eekil" in result
        assert "6.11" in result
        assert "Denklem" in result
        assert "4.5" in result

    def test_figure_number_not_corrupted(self):
        """Textbook bug: Figure 6.11, 6.12, 6.13 all became 6.10."""
        text = "Figure 6.11, Figure 6.12, and Figure 6.13"
        result, stash = self._simulate_stash_with_figures(text)
        # Restore
        for idx in range(len(stash) - 1, -1, -1):
            placeholder = f"<<LATEX_{idx}>>"
            result = result.replace(placeholder, stash[idx])
        assert "6.11" in result
        assert "6.12" in result
        assert "6.13" in result
        # None should be 6.10
        assert "6.10" not in result

    def test_figure_inside_textbf_already_stashed(self):
        r"""If Figure ref is inside \textbf{}, textbf is stashed first."""
        text = r"\textbf{Figure 5.1}"
        result, stash = self._simulate_stash_with_figures(text)
        # The textbf is stashed as a whole (short textbf <= 20 chars)
        assert "<<LATEX_" in result
        # textbf{Figure 5.1} should be in stash as-is (not translated)
        # because textbf_short stash runs before figure ref stash
        textbf_items = [s for s in stash if "\\textbf" in s]
        assert len(textbf_items) >= 1

    def test_mixed_refs_and_math(self):
        """Math and figure refs should coexist in stash."""
        text = r"See Figure 6.11 where $x > 0$ and Table 3.2."
        result, stash = self._simulate_stash_with_figures(text)
        assert "Figure" not in result
        assert "Table" not in result
        assert "$" not in result

    def test_exercise_stashed_and_restored(self):
        """Exercise refs should be stashed and restored with Turkish label."""
        text = "Solve Exercise 6.7 and Exercise 6.5."
        result, stash = self._simulate_stash_with_figures(text)
        assert "Exercise" not in result
        # Restore
        for idx in range(len(stash) - 1, -1, -1):
            placeholder = f"<<LATEX_{idx}>>"
            result = result.replace(placeholder, stash[idx])
        assert "Al\u0131\u015ft\u0131rma" in result
        assert "6.7" in result
        assert "6.5" in result

    def test_algorithm_stashed_and_restored(self):
        """Algorithm refs should be stashed and restored."""
        text = "Algorithm 3.1 runs in O(n) time."
        result, stash = self._simulate_stash_with_figures(text)
        assert "Algorithm" not in result
        # Restore
        for idx in range(len(stash) - 1, -1, -1):
            placeholder = f"<<LATEX_{idx}>>"
            result = result.replace(placeholder, stash[idx])
        assert "Algoritma" in result
        assert "3.1" in result

    def test_proposition_stashed_and_restored(self):
        """Proposition refs should be stashed and restored."""
        text = "By Proposition 4.2 we have"
        result, stash = self._simulate_stash_with_figures(text)
        assert "Proposition" not in result
        for idx in range(len(stash) - 1, -1, -1):
            placeholder = f"<<LATEX_{idx}>>"
            result = result.replace(placeholder, stash[idx])
        assert "\u00d6nerme" in result
        assert "4.2" in result

    def test_cross_ref_numbers_preserved_in_stash(self):
        """Lecture-notes bug: Figure 7.1, 7.2, 7.3 all became 7.1.

        Each distinct number must survive stash/restore independently.
        """
        text = "Figure 7.1, Figure 7.2, and Figure 7.3 show the results."
        result, stash = self._simulate_stash_with_figures(text)
        # All three should be stashed
        figure_items = [s for s in stash if "\u015eekil" in s]
        assert len(figure_items) == 3
        # Each must have its own number
        all_numbers = " ".join(figure_items)
        assert "7.1" in all_numbers
        assert "7.2" in all_numbers
        assert "7.3" in all_numbers

    def test_mixed_cross_refs_all_types(self):
        """All new ref types coexist with old ones."""
        text = (
            "See Exercise 6.7, Algorithm 3.1, Proposition 4.2, "
            "Figure 5.1, and Definition 2.12."
        )
        result, stash = self._simulate_stash_with_figures(text)
        assert "Exercise" not in result
        assert "Algorithm" not in result
        assert "Proposition" not in result
        assert "Figure" not in result
        assert "Definition" not in result
        # Verify all numbers preserved in stash
        for idx in range(len(stash) - 1, -1, -1):
            placeholder = f"<<LATEX_{idx}>>"
            result = result.replace(placeholder, stash[idx])
        assert "6.7" in result
        assert "3.1" in result
        assert "4.2" in result
        assert "5.1" in result
        assert "2.12" in result


# ---------------------------------------------------------------------------
# 3b. Href display text cross-ref protection (Rocket regression fix)
# ---------------------------------------------------------------------------

class TestHrefCrossRefProtection:
    """Verify cross-ref numbers inside \\href display text are preserved.

    Rocket regression: \\href{...}{Figure 4.10.} → MT corrupted 4.10 → 4.1.
    Fix: _translate_href_display_texts now uses _translate_figure_ref
    for cross-refs inside display text instead of raw MT.
    """

    def test_href_figure_ref_pure(self):
        """Pure cross-ref in href display: Figure 4.10."""
        import re
        from latex_pipeline.translator import _FIGURE_REF_RE, _translate_figure_ref

        display = "Figure 4.10."
        ref_m = _FIGURE_REF_RE.search(display)
        assert ref_m is not None
        assert ref_m.start() == 0
        tr = _translate_figure_ref(ref_m)
        assert "4.10" in tr
        assert "Şekil" in tr

    def test_href_figure_ref_with_letter(self):
        """Cross-ref with letter suffix: Figure 4.11a."""
        from latex_pipeline.translator import _FIGURE_REF_RE, _translate_figure_ref

        display = "Figure 4.11a."
        ref_m = _FIGURE_REF_RE.search(display)
        assert ref_m is not None
        tr = _translate_figure_ref(ref_m)
        assert "4.11a" in tr
        assert "Şekil" in tr

    def test_href_equation_ref(self):
        """Equation ref in href: Equation 4.15."""
        from latex_pipeline.translator import _FIGURE_REF_RE, _translate_figure_ref

        display = "Equation 4.15"
        ref_m = _FIGURE_REF_RE.search(display)
        assert ref_m is not None
        tr = _translate_figure_ref(ref_m)
        assert "4.15" in tr
        assert "Denklem" in tr

    def test_href_pure_ref_detection(self):
        """Pure cross-ref should be detected (end >= stripped len)."""
        from latex_pipeline.translator import _FIGURE_REF_RE

        for display in ["Figure 4.10.", "Figure 4.11a.", "Equation 4.15"]:
            ref_m = _FIGURE_REF_RE.search(display)
            assert ref_m is not None, f"No match for {display}"
            assert ref_m.start() == 0, f"Match not at start for {display}"
            assert ref_m.end() >= len(display.rstrip('. ')), \
                f"Not detected as pure ref: {display} (end={ref_m.end()}, stripped_len={len(display.rstrip('. '))})"


# ---------------------------------------------------------------------------
# 4. Author name protection tests (surname et al. bug)
# ---------------------------------------------------------------------------

class TestAuthorNameStash:
    """Verify author names are stashed before MT can translate them.

    Textbook bug: a surname that looks like a common word ("Hosterman
    et al.") was translated as a word because the surname was exposed to
    MT.  The fix ensures that author
    citation patterns (including \\textit{et al.} variants) are
    stashed BEFORE long textit, so MT never sees the surname.
    """

    def _simulate_stash(self, text: str):
        """Simulate stash with the correct order."""
        stash = []

        def _replace(match):
            idx = len(stash)
            stash.append(match.group(0))
            return f"<<LATEX_{idx}>>"

        text = _MATH_ENV_RE.sub(_replace, text)
        text = _CODE_ENV_RE.sub(_replace, text)
        text = _DISPLAY_MATH_BRACKET_RE.sub(_replace, text)
        text = _INLINE_MATH_RE.sub(lambda m: _replace(m), text)
        text = _INCLUDEGRAPHICS_RE.sub(_replace, text)
        text = _TEXTTT_RE.sub(_replace, text)
        text = _TEXTIT_SHORT_RE.sub(_replace, text)
        text = _TEXTBF_SHORT_RE.sub(_replace, text)
        text = _HREF_RE.sub(_replace, text)
        text = _REF_RE.sub(_replace, text)
        text = _PAREN_REF_RE.sub(_replace, text)
        text = _AUTHOR_ETAL_RE.sub(_replace, text)
        text = _AUTHOR_YEAR_RE.sub(_replace, text)
        text = _TEXTIT_LONG_RE.sub(_replace, text)
        text = _TEXTBF_LONG_RE.sub(_replace, text)
        return text, stash

    def test_textit_term_and_parenthetical_et_al_pattern(self):
        """The pattern from a textbook's body.tex (synthetic wording, same
        structure: \\textit term + parenthetical textit et al. + year)."""
        text = (
            r"We call this kind of reuse \textit{knowledge transfer} "
            r"(Hosterman \textit{et al.}, 2021), and this lets"
        )
        result, stash = self._simulate_stash(text)
        # Hosterman must be in stash (not exposed to MT)
        assert any("Hosterman" in s for s in stash)
        # The parenthetical ref should be a single stash entry
        paren_entries = [s for s in stash if "Hosterman" in s]
        assert len(paren_entries) == 1
        assert "2021" in paren_entries[0]

    def test_bare_author_etal_stashed(self):
        """Bare 'Author et al.' (no textit, no parens) is stashed."""
        text = "as shown by Goodfellow et al. in their work"
        result, stash = self._simulate_stash(text)
        assert any("Goodfellow" in s for s in stash)
        assert "Goodfellow" not in result

    def test_textit_etal_not_split(self):
        r"""'Author \textit{et al.}' must not be split into separate stash."""
        text = r"Hosterman \textit{et al.} showed that"
        result, stash = self._simulate_stash(text)
        # Should be one stash entry containing both
        combined = [s for s in stash if "Hosterman" in s and "et al." in s]
        assert len(combined) == 1

    def test_author_year_stashed(self):
        """'Author (YYYY)' is stashed."""
        text = "LeCun (2015) demonstrated that"
        result, stash = self._simulate_stash(text)
        assert any("LeCun" in s for s in stash)
        assert "LeCun" not in result

    def test_restore_preserves_original(self):
        """Stash + restore gives back the original text."""
        text = (
            r"(Hosterman \textit{et al.}, 2021; "
            r"Goodfellow et al., 2016)"
        )
        result, stash = self._simulate_stash(text)
        # Restore
        for idx in range(len(stash) - 1, -1, -1):
            result = result.replace(f"<<LATEX_{idx}>>", stash[idx])
        assert result == text

    def test_textit_transfer_learning_still_stashed(self):
        r"""\\textit{transfer learning} should still be stashed as long textit."""
        text = r"\textit{transfer learning} is a technique"
        result, stash = self._simulate_stash(text)
        assert any("transfer learning" in s for s in stash)


# ---------------------------------------------------------------------------
# 5. Import check
# ---------------------------------------------------------------------------

class TestSentenceSplitPlaceholderPreservation:
    """Verify that _translate_by_sentences keeps placeholders inside
    their surrounding sentence context, preventing MT from seeing
    incomplete fragments like "(see " without the cross-ref number.

    Root cause (lecture-notes bug): The old _translate_by_sentences first
    split text around <<LATEX_N>> placeholders, then sentence-split each
    non-placeholder part.  This meant "(see Fig. 7.1)" after stash became:
        ["(see ", "<<LATEX_0>>", ")."]
    MT received "(see " and output "(bkz.)" -- closing the parenthesis
    and dropping the reference number.

    Fix: Keep placeholders inside their sentence.  Sentence-split the
    whole text (with placeholders inline) so MT sees the full context.
    """

    def test_see_fig_pattern_stays_in_sentence(self):
        """'(see <<LATEX_0>>)' must NOT be split around the placeholder."""
        import re
        text = (
            "Because a body keeps moving at a steady speed in a straight "
            "line unless some force pulls on it, a periodic wobble of "
            "this kind means that a hidden companion must exist "
            "(see <<LATEX_0>>)."
        )
        # The sentence split regex should NOT break this up
        from latex_pipeline.translator import LatexTranslator
        sentences = LatexTranslator._SENTENCE_SPLIT_RE.split(text)
        # The placeholder and its surrounding parens should be in the
        # SAME sentence, not isolated.
        for sent in sentences:
            if "<<LATEX_0>>" in sent:
                assert "(see" in sent or "(bkz" in sent, \
                    f"Placeholder not in same sentence as '(see': {sent!r}"
                assert sent.rstrip().endswith(")."), \
                    f"Closing paren should be in same sentence: {sent!r}"
                break
        else:
            # If placeholder not found in any sentence, the text was
            # not split -- which is fine (single sentence = safe).
            pass

    def test_placeholder_not_isolated_by_split(self):
        """After sentence-level split, no sentence should be just
        a placeholder or a placeholder fragment like ').'."""
        import re
        text = (
            "The results are shown in Figure 6.11. "
            "The next step (see <<LATEX_0>>) is critical. "
            "Finally we conclude."
        )
        from latex_pipeline.translator import LatexTranslator
        sentences = LatexTranslator._SENTENCE_SPLIT_RE.split(text)
        for sent in sentences:
            stripped = sent.strip()
            if not stripped:
                continue
            # No sentence should be JUST ")." or "(see "
            assert stripped not in ("(see", "(see ", ")."), \
                f"Isolated fragment found: {stripped!r}"

    def test_multiple_placeholders_in_paragraph(self):
        """Multiple placeholders should each stay inside their sentence."""
        import re
        text = (
            "We refer to <<LATEX_0>> for the initial conditions. "
            "The secondary effects (see <<LATEX_1>>) are also important. "
            "Finally, <<LATEX_2>> confirms the hypothesis."
        )
        from latex_pipeline.translator import LatexTranslator
        sentences = LatexTranslator._SENTENCE_SPLIT_RE.split(text)
        # Each placeholder should appear in exactly one sentence
        for ph in ["<<LATEX_0>>", "<<LATEX_1>>", "<<LATEX_2>>"]:
            containing = [s for s in sentences if ph in s]
            assert len(containing) == 1, \
                f"{ph} appears in {len(containing)} sentences (expected 1)"

    def test_stash_then_sentence_split_preserves_fig_ref(self):
        """End-to-end: stash Fig. 7.1, sentence-split, verify placeholder
        stays with its surrounding '(see ...)' context."""
        import re
        from latex_pipeline.translator import (
            _FIGURE_REF_RE, _translate_figure_ref, LatexTranslator,
        )

        original = (
            "When one star of a pair outshines its partner by a wide "
            "margin, the fainter star may be lost in the glare of the "
            "bright one. Its presence can still be inferred from the slow "
            "back-and-forth drift of the bright star across the sky. "
            "Because a body keeps moving at a steady speed in a straight "
            "line unless some force pulls on it, a periodic wobble of "
            "this kind means that a hidden companion must exist "
            "(see Fig. 7.1)."
        )

        # Step 1: stash
        stash = []
        def _replace_fig(m):
            idx = len(stash)
            stash.append(_translate_figure_ref(m))
            return f"<<LATEX_{idx}>>"
        stashed = _FIGURE_REF_RE.sub(_replace_fig, original)

        assert "<<LATEX_0>>" in stashed
        assert "Fig." not in stashed

        # Step 2: sentence-split (new behavior)
        sentences = LatexTranslator._SENTENCE_SPLIT_RE.split(stashed)

        # Find the sentence containing the placeholder
        placeholder_sentences = [s for s in sentences if "<<LATEX_0>>" in s]
        assert len(placeholder_sentences) == 1, \
            f"Placeholder should be in exactly 1 sentence, found {len(placeholder_sentences)}"

        sent = placeholder_sentences[0]
        # The placeholder should be inside "(see ...)" context
        assert "(see <<LATEX_0>>)" in sent, \
            f"Expected '(see <<LATEX_0>>)' in sentence, got: {sent!r}"

    def test_fig_ref_stash_and_restore_lecture_notes_pattern(self):
        """The failing pattern from a lecture-notes body.tex
        (synthetic wording, same "(see Fig. 7.1)." ending).

        Ensures that after stash -> sentence-split -> restore,
        the Turkish reference 'Sek. 7.1' appears inside parentheses.
        """
        import re
        from latex_pipeline.translator import (
            _FIGURE_REF_RE, _translate_figure_ref, LatexTranslator,
        )

        original = "a periodic wobble of this kind means that a hidden companion must exist (see Fig. 7.1)."

        # Stash
        stash = []
        def _replace_fig(m):
            idx = len(stash)
            stash.append(_translate_figure_ref(m))
            return f"<<LATEX_{idx}>>"
        stashed = _FIGURE_REF_RE.sub(_replace_fig, original)

        # Sentence-split should keep placeholder in sentence
        sentences = LatexTranslator._SENTENCE_SPLIT_RE.split(stashed)

        # Rejoin (simulating what _translate_by_sentences does, without MT)
        result = " ".join(sentences)

        # Restore
        for idx in range(len(stash) - 1, -1, -1):
            placeholder = f"<<LATEX_{idx}>>"
            result = result.replace(placeholder, stash[idx])

        # The Turkish ref must be INSIDE the parentheses
        assert "\u015eek." in result, f"Turkish ref label missing: {result}"
        assert "7.1" in result, f"Reference number missing: {result}"
        # Check the ref is inside parens: (... Sek. 7.1)
        paren_match = re.search(r"\([^)]*\u015eek\.\s*7\.1[^)]*\)", result)
        assert paren_match is not None, \
            f"Turkish ref should be inside parentheses: {result}"


class TestImport:
    """Verify module imports correctly."""

    def test_import_translator_module(self):
        from latex_pipeline import translator
        assert hasattr(translator, 'LatexTranslator')
        assert hasattr(translator, '_HREF_RE')
        assert hasattr(translator, '_TEXTIT_LONG_RE')
        assert hasattr(translator, '_TEXTBF_LONG_RE')
        assert hasattr(translator, '_FIGURE_REF_RE')
        assert hasattr(translator, '_FIGURE_LABEL_MAP')
        assert hasattr(translator, '_translate_figure_ref')

    def test_regex_patterns_compiled(self):
        """All regex patterns should be pre-compiled."""
        assert isinstance(_HREF_RE, re.Pattern)
        assert isinstance(_TEXTIT_SHORT_RE, re.Pattern)
        assert isinstance(_TEXTBF_SHORT_RE, re.Pattern)
        assert isinstance(_TEXTIT_LONG_RE, re.Pattern)
        assert isinstance(_TEXTBF_LONG_RE, re.Pattern)
        assert isinstance(_REF_RE, re.Pattern)
        assert isinstance(_FIGURE_REF_RE, re.Pattern)


# ---------------------------------------------------------------------------
# 6. Restore stash: spacing, Unicode mangling, orphan cleanup
# ---------------------------------------------------------------------------

class TestRestoreStashSpacing:
    """Verify _restore_stash inserts spaces around restored content
    to prevent word-collisions (Bug 1 & 3 from pipeline review).

    These tests instantiate LatexTranslator._restore_stash directly
    without needing the MT model.
    """

    def _restore(self, body, stash):
        """Call _restore_stash via an unbound method trick."""
        from latex_pipeline.translator import LatexTranslator
        # Create a minimal instance (no MT model needed for restore)
        instance = LatexTranslator.__new__(LatexTranslator)
        return instance._restore_stash(body, stash)

    def test_basic_restore(self):
        """Standard restore works as before."""
        stash = [r"$x^2$", r"\ref{fig:1}"]
        body = "The formula <<LATEX_0>> and reference <<LATEX_1>> are important."
        result = self._restore(body, stash)
        assert "$x^2$" in result
        assert r"\ref{fig:1}" in result
        assert "<<LATEX_" not in result

    def test_spacing_left_collision(self):
        """Bug 3: word<<LATEX_0>> should become 'word $x^2$'."""
        stash = [r"$x^2$"]
        body = "word<<LATEX_0>> more text"
        result = self._restore(body, stash)
        # There must be a space between "word" and "$x^2$"
        assert "word $x^2$" in result or "word$x^2$" not in result
        assert "word$" not in result, f"Missing left space: {result!r}"

    def test_spacing_right_collision(self):
        """Bug 3: <<LATEX_0>>word should become '$x^2$ word'."""
        stash = [r"$x^2$"]
        body = "some <<LATEX_0>>word end"
        result = self._restore(body, stash)
        assert "$word" not in result, f"Missing right space: {result!r}"

    def test_spacing_both_collision(self):
        """Word<<LATEX_0>>word must have spaces on both sides."""
        stash = [r"\textbf{bold}"]
        body = "left<<LATEX_0>>right"
        result = self._restore(body, stash)
        assert r"left \textbf{bold} right" == result

    def test_no_double_space(self):
        """If space already present, don't add another."""
        stash = [r"$y$"]
        body = "word <<LATEX_0>> next"
        result = self._restore(body, stash)
        assert "word $y$ next" == result

    def test_punctuation_no_extra_space(self):
        """No space between restored content and punctuation."""
        stash = [r"\ref{fig:1}"]
        body = "see <<LATEX_0>>."
        result = self._restore(body, stash)
        assert result.endswith(r"\ref{fig:1}.")
        assert " ." not in result

    def test_placeholders_run_into_text_pattern(self):
        """Bug 1: lecture-notes pattern where placeholders run into text."""
        stash = [r"\ref{eq:orbit}", r"$m_1 + m_2$"]
        body = "sistem,teleskoptaalanan<<LATEX_1>>gozlemleriyalnizca<<LATEX_0>>ortaya"
        result = self._restore(body, stash)
        # Restored content must have spaces around it
        assert r"$m_1 + m_2$" in result
        assert r"\ref{eq:orbit}" in result
        # No word-math collision
        assert "alanan$m" not in result
        assert "2$gozlem" not in result


class TestRestoreStashUnicodeMangling:
    """Verify _restore_stash handles Unicode-digit mangled placeholders
    (Bug 1: LATEX_127 -> LATEX subscript-1 27)."""

    def _restore(self, body, stash):
        from latex_pipeline.translator import LatexTranslator
        instance = LatexTranslator.__new__(LatexTranslator)
        return instance._restore_stash(body, stash)

    def test_subscript_digits(self):
        """Subscript digits in placeholder number are matched."""
        stash = ["FILLER"] * 19  # fill up to idx 18
        stash.append(r"$\alpha$")  # idx 19
        # Build mangled body: <<LATEX subscript-1 subscript-9>> = 19
        body = "text \u00ab\u00abLATEX\u2081\u2089\u00bb\u00bb more"
        result = self._restore(body, stash)
        assert r"$\alpha$" in result, f"Unicode mangled placeholder not restored: {result!r}"
        assert "\u00ab" not in result, f"Mangled brackets should be gone: {result!r}"

    def test_subscript_mixed(self):
        """Mix of ASCII and subscript digits: LATEX subscript-1 27 = 127."""
        stash = ["FILLER"] * 127
        stash.append(r"$\beta$")  # idx 127
        body = "Boyle bir <<LATEX\u208127>> goruntusu"
        result = self._restore(body, stash)
        assert r"$\beta$" in result, f"Mixed subscript placeholder not restored: {result!r}"

    def test_fullwidth_digits(self):
        """Fullwidth digits (rare but possible)."""
        stash = [r"$x$", r"$y$", r"$z$"]
        # <<LATEX fullwidth-2>> = idx 2
        body = "see \u00ab\u00abLATEX_\uff12\u00bb\u00bb here"
        result = self._restore(body, stash)
        assert "$z$" in result, f"Fullwidth digit placeholder not restored: {result!r}"


class TestRestoreStashOrphanCleanup:
    """Verify orphan placeholders (both ASCII and Unicode-mangled) are cleaned."""

    def _restore(self, body, stash):
        from latex_pipeline.translator import LatexTranslator
        instance = LatexTranslator.__new__(LatexTranslator)
        return instance._restore_stash(body, stash)

    def test_ascii_orphan_removed(self):
        """Orphan <<LATEX_999>> (outside stash range) is removed."""
        stash = [r"$x$"]
        body = "text <<LATEX_0>> and <<LATEX_999>> end"
        result = self._restore(body, stash)
        assert "$x$" in result
        assert "<<LATEX_999>>" not in result
        assert "LATEX" not in result

    def test_unicode_orphan_removed(self):
        """Unicode-mangled orphan is removed."""
        stash = [r"$x$"]
        body = "text <<LATEX_0>> and \u00ab\u00abLATEX\u2089\u2089\u2089\u00bb\u00bb end"
        result = self._restore(body, stash)
        assert "$x$" in result
        assert "\u00ab" not in result

    def test_href_spacing_after_restore(self):
        """Bug 2: URL stash restore should not cause word collision.
        Simulates the AI Engineering href pattern."""
        stash = [r"\href{https://en.wikipedia.org/wiki/Dot_product}{nokta carpimi}"]
        body = "carpim noktasi (<<LATEX_0>>)Sorgu vektorleri arasinda."
        result = self._restore(body, stash)
        # After the closing paren there should be a space before "Sorgu"
        assert ")Sorgu" not in result, f"Missing space after href restore: {result!r}"


# ---------------------------------------------------------------------------
# Run tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import traceback

    test_classes = [
        TestHrefRegex,
        TestTextitRegex,
        TestTextbfRegex,
        TestRefRegex,
        TestStashOrdering,
        TestFigureRefRegex,
        TestFigureRefTranslation,
        TestFigureRefStashIntegration,
        TestHrefCrossRefProtection,
        TestAuthorNameStash,
        TestSentenceSplitPlaceholderPreservation,
        TestImport,
        TestRestoreStashSpacing,
        TestRestoreStashUnicodeMangling,
        TestRestoreStashOrphanCleanup,
    ]

    total = 0
    passed = 0
    failed = 0
    errors = []

    for cls in test_classes:
        instance = cls()
        methods = [m for m in dir(instance) if m.startswith("test_")]
        for method_name in methods:
            total += 1
            method = getattr(instance, method_name)
            try:
                method()
                passed += 1
                print(f"  PASS  {cls.__name__}.{method_name}")
            except Exception as e:
                failed += 1
                errors.append((cls.__name__, method_name, e))
                print(f"  FAIL  {cls.__name__}.{method_name}: {e}")

    print(f"\n{'='*60}")
    print(f"Results: {passed}/{total} passed, {failed} failed")
    if errors:
        print(f"\nFailed tests:")
        for cls_name, method_name, exc in errors:
            print(f"  {cls_name}.{method_name}:")
            traceback.print_exception(type(exc), exc, exc.__traceback__)
    print(f"{'='*60}")
    sys.exit(0 if failed == 0 else 1)
