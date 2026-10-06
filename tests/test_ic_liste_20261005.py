"""Dönüştürücüde iç içe liste (G1b-2, 5 Ekim 2026). Girdiler sentetiktir."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

KOK = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(KOK))


@pytest.fixture
def c():
    from latex_pipeline.config import PipelineConfig
    from latex_pipeline.converter import MarkdownToLatex
    cfg = PipelineConfig(input_pdf="x.pdf", output_dir="/tmp", document_class="book",
                         translate=False, source_lang="en", target_lang="tr")
    m = MarkdownToLatex(cfg)
    m._hard_wrapped = False
    return m


def _derinlikler(tex: str):
    d, out = 0, []
    for s in tex.split("\n"):
        t = s.strip()
        if re.match(r"\\begin\{(itemize|enumerate)\}", t):
            d += 1
        elif re.match(r"\\end\{(itemize|enumerate)\}", t):
            d -= 1
        elif t.startswith("\\item"):
            out.append((d, t[6:].strip()))
    return out


def test_alt_maddeler_ic_liste(c):
    # Okuma bulgusundaki olgunun yapısı.
    md = ("- The scope.\n- Our main data sources:\n  - A dump of messages.\n"
          "  - A set of logs.\n- The limits:\n  - First limit.\n  - Second limit.")
    tex, n = c._consume_unordered_list(md.split("\n"), 0)
    assert n == 7
    assert _derinlikler(tex) == [
        (1, "The scope."), (1, "Our main data sources:"), (2, "A dump of messages."),
        (2, "A set of logs."), (1, "The limits:"), (2, "First limit."), (2, "Second limit.")]


def test_numarali_liste_alt_maddeyle_numarali_kalir(c):
    # "- N." + "(a)" alt maddeleri: eskiden numara sayımı alt maddede bozulup
    # liste madde imli oluyordu.
    md = ("- 1. First point:\n  - (a) Part one.\n    More of part one.\n"
          "  - (b) Part two.\n- 2. Second point.\n\n- 3. Third point.")
    tex, n = c._consume_unordered_list(md.split("\n"), 0)
    assert tex.startswith("\\begin{enumerate}")
    assert _derinlikler(tex) == [
        (1, "First point:"), (2, "(a) Part one. More of part one."), (2, "(b) Part two."),
        (1, "Second point."), (1, "Third point.")]


def test_sirali_liste_icinde_imli_alt_liste(c):
    tex, _ = c._consume_ordered_list("1. One\n   - sub a\n   - sub b\n2. Two".split("\n"), 0)
    assert _derinlikler(tex) == [(1, "One"), (2, "sub a"), (2, "sub b"), (1, "Two")]


def test_derinlik_siniri(c):
    md = "- a\n  - b\n    - c\n      - d\n        - e"
    tex, _ = c._consume_unordered_list(md.split("\n"), 0)
    assert max(d for d, _ in _derinlikler(tex)) == 4


def test_duz_liste_degismez(c):
    md = "- one\n- two\n- three"
    tex, n = c._consume_unordered_list(md.split("\n"), 0)
    assert n == 3 and [d for d, _ in _derinlikler(tex)] == [1, 1, 1]
