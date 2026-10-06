"""28 Temmuz 2026 bug avı — retranslator / glossary / yol_a_ortak regresyonları.

Kapsanan bulgular:
    F6  — Retranslator final heading-case geçişi korumalı bölgeleri (verbatim,
          math, bib, titlepage) atlamıyordu ve İngilizce kelimeye Türkçe
          noktalı 'İ' basıyordu ('introduction' -> 'İntroduction').
    F12 — glossary._protect_math_zones çok satırlı ``$...$`` inline math'i
          stash'lemiyordu; Phase 5.75 formül içini Türkçeleştirebiliyordu.
    F13 — resolve_book_dir kural 3 (kardeş sidecar) sha1 çakışma guard'ını
          atlıyor, farklı kitabın dizinine sessizce derleme yapılabiliyordu.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from latex_pipeline.glossary import (  # noqa: E402
    GlossaryEngine,
    GlossaryStats,
    TermEntry,
    _protect_math_zones,
    _restore_math_zones,
)
from latex_pipeline.retranslator import (  # noqa: E402
    _ensure_heading_case,
    scan_and_retranslate,
)
from yol_a_ortak import build_meta_header, resolve_book_dir  # noqa: E402


# ---------------------------------------------------------------------------
# F6 — final heading-case geçişi
# ---------------------------------------------------------------------------

class TestF6HeadingCaseFinalPass:
    """Final geçiş korumalı satırlara dokunmamalı, İ/I ayrımını doğru yapmalı."""

    def test_verbatim_heading_not_touched(self):
        body = (
            "Bu bir Türkçe paragraftır ve içinde yeterince Türkçe kelime "
            "bulunmaktadır.\n\n"
            "\\begin{verbatim}\n"
            "\\section{introduction to widgets}\n"
            "\\end{verbatim}\n\n"
            "\\section{introduction to widgets}\n"
        )
        result = scan_and_retranslate(body, translator_fn=lambda t: t)
        out_lines = result.output_tex.split("\n")
        # verbatim içindeki örnek satır aynen kalmalı
        assert out_lines[3] == "\\section{introduction to widgets}"
        # verbatim DIŞINDAKİ başlık yine düzeltilmeli
        assert "\\section{Introduction to widgets}" in result.output_tex
        assert result.heading_case_fixes == 1

    def test_lstlisting_heading_not_touched(self):
        body = (
            "Türkçe bir paragraf, yeterince kelime içeriyor ve çevrilmiş "
            "durumdadır.\n\n"
            "\\begin{lstlisting}\n"
            "\\section{iterative solver}\n"
            "\\end{lstlisting}\n"
        )
        result = scan_and_retranslate(body, translator_fn=lambda t: t)
        assert "\\section{iterative solver}" in result.output_tex
        assert result.heading_case_fixes == 0

    def test_english_heading_gets_ascii_i(self):
        out = _ensure_heading_case("\\section{introduction to widgets}")
        assert out == "\\section{Introduction to widgets}"
        assert "İ" not in out

    def test_english_heading_with_number_prefix(self):
        out = _ensure_heading_case("\\section{2.3 information theory}")
        assert out == "\\section{2.3 Information theory}"

    def test_turkish_heading_keeps_dotted_i(self):
        out = _ensure_heading_case("\\section{istatistiksel öğrenme}\\label{s}")
        assert "\\section{İstatistiksel öğrenme}" in out
        assert "\\label{s}" in out

    def test_turkish_heading_without_diacritics_defaults_to_turkish(self):
        # Türkçe işaret yok, bilinen İngilizce kelime de yok -> Türkçe varsayılan
        out = _ensure_heading_case("\\section{iletisim sistemleri}")
        assert "İletisim" in out

    def test_dotless_i_start_unchanged(self):
        out = _ensure_heading_case("\\section{ışık hızı}")
        assert "\\section{Işık hızı}" in out

    def test_code_brand_first_word_untouched(self):
        # iOS / iPhone: hem 'İ' hem 'I' yanlış olur -> hiç dokunma
        assert _ensure_heading_case("\\section{iOS uygulamaları}") == (
            "\\section{iOS uygulamaları}"
        )

    def test_non_i_headings_unchanged_behaviour(self):
        assert "Yapay zeka nedir" in _ensure_heading_case(
            "\\section{yapay zeka nedir}")
        assert "2.3 Ortogonal" in _ensure_heading_case(
            "\\section{2.3 ortogonal vektörler}")


# ---------------------------------------------------------------------------
# F12 — çok satırlı inline math koruması
# ---------------------------------------------------------------------------

def _mini_engine() -> GlossaryEngine:
    """Veri-bağımsız mini motor (tests/test_yolb_glossary_engine.py deseni)."""
    eng = GlossaryEngine.__new__(GlossaryEngine)
    eng.book_type = "science"
    eng.stats = GlossaryStats()
    eng._terms = {}
    eng._terms["bias"] = TermEntry(en="bias", tr="yanlılık", priority="high")
    eng._finalize()
    return eng


class TestF12GlossaryMultilineMath:
    def test_multiline_inline_math_stashed(self):
        text = "Metin $a + b\n+ c$ ve devamı."
        out, stash = _protect_math_zones(text)
        assert len(stash) >= 1
        assert "$" not in out
        assert _restore_math_zones(out, stash) == text

    def test_single_line_inline_math_still_stashed(self):
        text = "Metin $a + b$ tek satır."
        out, stash = _protect_math_zones(text)
        assert len(stash) == 1
        assert _restore_math_zones(out, stash) == text

    def test_paragraph_boundary_not_crossed(self):
        # Kaçak tek '$' tüm paragrafları yutmamalı (sanitizer ile aynı sınır).
        text = "Ilk paragrafta $ kacak dolar var\n\nIkinci paragrafta baska metin."
        out, stash = _protect_math_zones(text)
        assert out.count("$") == 1
        assert _restore_math_zones(out, stash) == text

    def test_verify_terms_multiline_math_untouched(self):
        eng = _mini_engine()
        src = "Giriş metni $\\text{bias}\n+ 1$ ve son."
        assert eng.verify_terms(src, {}) == src

    def test_verify_terms_outside_math_still_replaced(self):
        eng = _mini_engine()
        out = eng.verify_terms("The bias term is large here.", {})
        assert "yanlılık" in out


# ---------------------------------------------------------------------------
# F13 — resolve_book_dir kural 3 sha1 kimlik kontrolü
# ---------------------------------------------------------------------------

def _make_book(root: Path, slug: str, dir_sha: str) -> Path:
    outdir = root / "output" / slug
    outdir.mkdir(parents=True)
    (outdir / "kitap.json").write_text(
        json.dumps({"slug": slug, "book_name": slug,
                    "source_sha1": dir_sha, "output_dir": str(outdir)},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    return outdir


class TestF13ResolveBookDirSidecarGuard:
    def test_sidecar_rule_rejects_sha_mismatch(self, tmp_path):
        # En yaygın yerleşim: EXPORT_TR.tex dizinin İÇİNDE
        outdir = _make_book(tmp_path, "Kitap", "bbbb")
        tex = outdir / "EXPORT_TR.tex"
        tex.write_text(
            build_meta_header({"slug": "Kitap", "source_sha1": "aaaa"})
            + "\\section{Test}\n",
            encoding="utf-8",
        )
        info = resolve_book_dir(tex, tmp_path)
        assert info["output_dir"] is None, (
            "başka kitabın dizinine sessiz derleme yapılmamalı (F13)")
        assert info["source"] != "sidecar"
        assert sum("sha1 uyuşmadı" in t for t in info["tried"]) >= 2

    def test_sidecar_rule_accepts_matching_sha(self, tmp_path):
        # Başlıkta slug YOK (kural 2 atlanır), sha1 uyuşuyor -> kural 3 kabul
        outdir = _make_book(tmp_path, "Kitap", "aaaa")
        tex = outdir / "EXPORT_TR.tex"
        tex.write_text(
            build_meta_header({"source_sha1": "aaaa"}) + "\\section{Test}\n",
            encoding="utf-8",
        )
        info = resolve_book_dir(tex, tmp_path)
        assert info["source"] == "sidecar"
        assert Path(info["output_dir"]) == outdir.resolve()

    def test_sidecar_rule_without_header_still_resolves(self, tmp_path):
        # Kimlik başlığı yok -> doğrulanamaz, eski davranış korunur
        outdir = _make_book(tmp_path, "Kitap", "bbbb")
        tex = outdir / "EXPORT_TR.tex"
        tex.write_text("\\section{Test}\n", encoding="utf-8")
        info = resolve_book_dir(tex, tmp_path)
        assert info["source"] == "sidecar"
        assert Path(info["output_dir"]) == outdir.resolve()

    def test_explicit_flag_still_overrides(self, tmp_path):
        outdir = _make_book(tmp_path, "Kitap", "bbbb")
        tex = outdir / "EXPORT_TR.tex"
        tex.write_text(
            build_meta_header({"slug": "Kitap", "source_sha1": "aaaa"})
            + "\\section{Test}\n",
            encoding="utf-8",
        )
        info = resolve_book_dir(tex, tmp_path, explicit_output_dir=str(outdir))
        assert info["source"] == "flag"
        assert Path(info["output_dir"]) == outdir.resolve()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
