"""Phase 6 tam-belge sanitize: preamble LaTeX kodudur, gövde kuralları ona uygulanmaz.

Hata (24 Eyl 2026, bir Yol B testi): ``_escape_stray_percent``
şablonun ``\\IfFontExistsTF{..}{%`` satır sonu yorumlarını ``{\\%`` yapıyordu;
17 Tem'den beri derlenen her PDF'nin (Yol A dahil) ilk sayfasına ``% % % %``
basılıyordu. Güvenlik filtresi preamble'da (preamble_ozel.tex) çalışmaya
devam etmeli.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from latex_pipeline.config import PipelineConfig
from latex_pipeline.sanitizer import LatexSanitizer
from latex_pipeline.template_engine import TemplateEngine

BODY = "\\chapter{Bir}\nOranı %36 arttı.\n"


def _wrapped(extra_preamble: str | None = None) -> str:
    config = PipelineConfig(input_pdf=Path("kitap.pdf"),
                            output_dir=Path(tempfile.mkdtemp()),
                            document_class="book")
    return TemplateEngine(config).wrap_content(BODY, extra_preamble=extra_preamble)


def _split(tex: str) -> tuple[str, str]:
    index = tex.index("\\begin{document}")
    return tex[:index], tex[index:]


@pytest.mark.parametrize("manual", [True, False])
def test_sablon_preamble_aynen_korunur(manual):
    tex = _wrapped()
    assert any(line.rstrip().endswith("{%") for line in tex.splitlines()), (
        "şablonda satır sonu yorumu yoksa bu test artık bir şey ölçmüyor")
    out, _ = LatexSanitizer(manual_translation=manual).sanitize(tex)
    assert _split(out)[0] == _split(tex)[0]
    assert "{\\%" not in out


@pytest.mark.parametrize("manual", [True, False])
def test_govdedeki_ciplak_yuzde_hala_kacirilir(manual):
    out, _ = LatexSanitizer(manual_translation=manual).sanitize(_wrapped())
    assert "Oranı \\%36 arttı." in _split(out)[1]


def test_preamble_guvenlik_filtresi_calisir():
    tex = _wrapped(extra_preamble="\\openout\\dosya=gizli.txt\n")
    out, _ = LatexSanitizer(manual_translation=True).sanitize(tex)
    preamble = _split(out)[0]
    assert "\\openout\\dosya=gizli.txt" not in preamble.replace("% FILTERED", "")
    assert "FILTERED" in preamble


def test_yorumdaki_begin_document_sinir_sayilmaz():
    tex = ("\\documentclass{book}\n"
           "% not: \\begin{document} buraya gelir {%\n"
           "\\newcommand{\\x}{%\n  y}\n"
           "\\begin{document}\nOranı %5\n\\end{document}\n")
    out, _ = LatexSanitizer().sanitize(tex)
    assert "\\newcommand{\\x}{%\n" in out          # preamble'da, dokunulmadı
    assert "Oranı \\%5" in out                     # gövdede, kaçırıldı


def test_yalniz_govde_verilince_davranis_ayni():
    """Phase 5.5 gövdeyi \\begin{document} olmadan verir: tüm geçişler uygulanır."""
    out, _ = LatexSanitizer().sanitize("Oranı %36 arttı.\n")
    assert out == "Oranı \\%36 arttı.\n"
