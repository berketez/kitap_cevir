"""29 Temmuz 2026 bug avı — latex_pipeline yardımcıları (glossary/converter/inspector).

Kapsanan bulgular:
    F28 — glossary.verify_terms ``\\url{...}`` / ``\\href{...}{...}`` argümanlarını
          korumuyordu; sözlükteki tek kelimelik terimler URL yolunda eşleşip
          adresi Türkçeleştiriyor, link kırılıyordu.
    F32 — converter markdown-unescape listesi ``\\$``'ı atlıyordu; ``\\$100``
          çıktıda ``\\\\$100`` (zorunlu satır kırımı + math modu açılışı) oluyordu.
    F43 — inspector: (a) ``\\\\[2mm]`` satır-sonu boşluk komutu display math
          sayılıyordu; (b) alternatif uzantıyla diskte bulunan görsel yine
          'missing' sayılıp skordan -10 götürüyordu.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from latex_pipeline.config import PipelineConfig  # noqa: E402
from latex_pipeline.converter import (  # noqa: E402
    MarkdownToLatex,
    _escape_tex_keep_tokens,
    _protect_and_escape,
)
from latex_pipeline.glossary import (  # noqa: E402
    GlossaryEngine,
    GlossaryStats,
    TermEntry,
    _protect_math_zones,
    _restore_math_zones,
)
from latex_pipeline.inspector import QualityInspector  # noqa: E402


# ---------------------------------------------------------------------------
# F28 — \url / \href argüman koruması
# ---------------------------------------------------------------------------

def _url_engine() -> GlossaryEngine:
    """Veri-bağımsız mini motor (test_fixav20260728_misc.py deseni)."""
    eng = GlossaryEngine.__new__(GlossaryEngine)
    eng.book_type = "science"
    eng.stats = GlossaryStats()
    eng._terms = {
        "tokenizer": TermEntry(
            en="tokenizer", tr="belirteçleyici", priority="high",
            wrong_alternatives=["jetonlayıcı"],
        ),
    }
    eng._finalize()
    return eng


class TestF28UrlHrefProtection:
    def test_url_argument_is_stashed_whole(self):
        text = r"Bkz. \url{https://hf.co/docs/tokenizer} devamı."
        out, stash = _protect_math_zones(text)
        assert r"\url{https://hf.co/docs/tokenizer}" in stash
        assert "tokenizer" not in out
        assert _restore_math_zones(out, stash) == text

    def test_href_url_stashed_display_text_free(self):
        # Sözleşme: URL argümanı korunur, görünen metin çevrilebilir kalır
        # (translator.py'deki _translate_href_display_texts ile aynı politika).
        text = r"\href{https://x.com/tokenizer-guide}{the tokenizer guide}"
        out, stash = _protect_math_zones(text)
        assert r"\href{https://x.com/tokenizer-guide}" in stash
        assert "{the tokenizer guide}" in out
        assert _restore_math_zones(out, stash) == text

    def test_verify_terms_does_not_rewrite_url_path(self):
        eng = _url_engine()
        src = r"Bkz. \url{https://hf.co/docs/tokenizer} sayfası."
        out = eng.verify_terms(src, {})
        assert r"\url{https://hf.co/docs/tokenizer}" in out
        assert "belirteçleyici" not in out

    def test_verify_terms_keeps_href_url_translates_display(self):
        eng = _url_engine()
        src = r"\href{https://x.com/tokenizer-guide}{the tokenizer guide}"
        out = eng.verify_terms(src, {})
        assert r"\href{https://x.com/tokenizer-guide}" in out
        assert "{the belirteçleyici guide}" in out

    def test_verify_terms_wrong_alternative_not_applied_inside_url(self):
        # Pass 2 (wrong_alternatives) da URL'ye dokunmamalı.
        eng = _url_engine()
        src = r"Adres \url{https://site.org/jetonlayıcı/v1} burada."
        out = eng.verify_terms(src, {})
        assert r"\url{https://site.org/jetonlayıcı/v1}" in out

    def test_plain_text_term_still_replaced(self):
        # Koruma, normal metindeki terim uygulamasını kapatmamalı.
        eng = _url_engine()
        out = eng.verify_terms("The tokenizer output is short.", {})
        assert "belirteçleyici" in out

    def test_ref_like_commands_still_protected(self):
        # href/url eklenmesi mevcut alternasyonu bozmamalı.
        text = r"\eqref{eq:1} ve \ref{fig:2} ve \includegraphics{tokenizer.png}"
        out, stash = _protect_math_zones(text)
        assert r"\eqref{eq:1}" in stash
        assert r"\ref{fig:2}" in stash
        assert r"\includegraphics{tokenizer.png}" in stash
        assert _restore_math_zones(out, stash) == text


# ---------------------------------------------------------------------------
# F32 — markdown '\$' unescape (çift-escape önlemi)
# ---------------------------------------------------------------------------

class TestF32DollarDoubleEscape:
    def test_protect_and_escape_single_dollar(self):
        out = _protect_and_escape(r"The price was \$100 million.")
        assert out == r"The price was \$100 million."
        assert "\\\\$" not in out  # zorunlu satır kırımı + math açılışı YOK

    def test_protect_and_escape_multiple_dollars(self):
        out = _protect_and_escape(r"costs \$100 and \$200")
        assert out == r"costs \$100 and \$200"

    def test_bare_dollar_still_escaped(self):
        out = _protect_and_escape("100 USD $ sign alone")
        assert out == r"100 USD \$ sign alone"

    def test_real_inline_math_untouched(self):
        out = _protect_and_escape(r"value $x=1$ and \$5")
        assert "$x=1$" in out
        assert out.endswith(r"and \$5")
        assert "\\\\$" not in out

    def test_real_display_math_untouched(self):
        out = _protect_and_escape("formul $$a+b$$ ve \\$7")
        assert "$$a+b$$" in out
        assert out.endswith(r"ve \$7")

    def test_other_specials_unchanged_behaviour(self):
        out = _protect_and_escape(r"a\_b ve \%50 ve \&co ve \#1")
        assert out == r"a\_b ve \%50 ve \&co ve \#1"

    def test_escape_tex_keep_tokens_dollar(self):
        out = _escape_tex_keep_tokens(r"\$100 bonus & 5\% indirim")
        assert out == r"\$100 bonus \& 5\% indirim"
        assert "\\\\$" not in out

    def test_convert_inline_bold_with_dollar(self):
        conv = MarkdownToLatex(PipelineConfig())
        out = conv._convert_inline(r"**\$100 bonus** ve $e=mc^2$ metni")
        assert r"\textbf{\$100 bonus}" in out
        assert "$e=mc^2$" in out
        assert "\\\\$" not in out

    def test_convert_inline_plain_dollar(self):
        conv = MarkdownToLatex(PipelineConfig())
        out = conv._convert_inline(r"maliyet \$1,5 milyon")
        assert out == r"maliyet \$1,5 milyon"


# ---------------------------------------------------------------------------
# F43 — inspector display math sayacı + yanlış uzantılı görseller
# ---------------------------------------------------------------------------

def _write_case(tmp_path: Path, tex_body: str, image_names=()) -> tuple[Path, Path]:
    images = tmp_path / "images"
    images.mkdir(parents=True, exist_ok=True)
    for name in image_names:
        (images / name).write_bytes(b"\x89PNG")
    tex = tmp_path / "book.tex"
    tex.write_text(
        "\\documentclass{article}\n\\begin{document}\n"
        + tex_body
        + "\n\\end{document}\n",
        encoding="utf-8",
    )
    return tex, images


class TestF43DisplayMathCount:
    def test_linebreak_spacing_not_counted(self, tmp_path):
        tex, images = _write_case(tmp_path, "satir \\\\[2mm] devam")
        rep = QualityInspector(images_dir=images).inspect(tex)
        assert rep.formulas.display_math == 0

    def test_real_display_math_counted(self, tmp_path):
        tex, images = _write_case(tmp_path, "Formul: \\[ E = mc^2 \\]")
        rep = QualityInspector(images_dir=images).inspect(tex)
        assert rep.formulas.display_math == 1

    def test_mixed_linebreak_and_display_math(self, tmp_path):
        tex, images = _write_case(
            tmp_path,
            "satir \\\\[2mm] devam\nFormul: \\[ a+b \\]\nIkinci: \\[ c+d \\]",
        )
        rep = QualityInspector(images_dir=images).inspect(tex)
        assert rep.formulas.display_math == 2

    def test_inline_math_not_eaten_by_linebreak_spacing(self, tmp_path):
        # Eski desende '\\[2mm]' display math sanılıp silme aralığı sonraki
        # gerçek '\]'e kadar uzuyor, aradaki inline math kayboluyordu.
        tex, images = _write_case(
            tmp_path,
            "satir \\\\[2mm] devam $a+b$ ve $c+d$\nFormul: \\[ x \\]",
        )
        rep = QualityInspector(images_dir=images).inspect(tex)
        assert rep.formulas.inline_math == 2


class TestF43WrongExtensionImages:
    def test_alt_extension_counts_as_found(self, tmp_path):
        tex, images = _write_case(
            tmp_path, "\\includegraphics{fig1.jpg}", ["fig1.png"],
        )
        rep = QualityInspector(images_dir=images).inspect(tex)
        assert rep.images[0].found is True
        assert rep.images[0].wrong_ext is True
        assert rep.images[0].actual_path.endswith("fig1.png")
        assert rep.images_missing == 0
        assert rep.images_found == 1
        assert rep.images_wrong_ext == 1

    def test_exact_match_not_flagged_wrong_ext(self, tmp_path):
        tex, images = _write_case(
            tmp_path, "\\includegraphics{fig1.png}", ["fig1.png"],
        )
        rep = QualityInspector(images_dir=images).inspect(tex)
        assert rep.images[0].found is True
        assert rep.images[0].wrong_ext is False
        assert rep.images_wrong_ext == 0

    def test_truly_missing_image_still_missing(self, tmp_path):
        tex, images = _write_case(
            tmp_path, "\\includegraphics{yok.png}", ["baska.png"],
        )
        rep = QualityInspector(images_dir=images).inspect(tex)
        assert rep.images[0].found is False
        assert rep.images_missing == 1
        assert rep.images_wrong_ext == 0

    def test_wrong_ext_penalty_smaller_than_missing(self, tmp_path):
        """Diskte var olan görsel -10'luk 'missing' cezasını YEMEZ."""
        body = "\\includegraphics{fig1.jpg}\nFormul: \\[ x \\]"
        tex_w, img_w = _write_case(tmp_path / "wrong", body, ["fig1.png"])
        tex_m, img_m = _write_case(tmp_path / "miss", body, ["baska.png"])
        rep_w = QualityInspector(images_dir=img_w).inspect(tex_w)
        rep_m = QualityInspector(images_dir=img_m).inspect(tex_m)
        assert rep_w.score > rep_m.score
        assert rep_w.score - rep_m.score == pytest.approx(7.0)  # -3 yerine -10
