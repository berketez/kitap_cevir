"""Marker'ın üst simgeyle böldüğü HTML varlığı (5 Ekim 2026; FX1 notu).

Marker "&quot;" varlığını "<sup>&</sup>quot;" diye bölebiliyor; eskiden çıktıya
"\\&quot;" sızıyor ve derlemede "\\"\\textbf" TeX hatasına dönüyordu.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from latex_pipeline.config import PipelineConfig  # noqa: E402
from latex_pipeline.converter import MarkdownToLatex  # noqa: E402


def _tex(md):
    return MarkdownToLatex(PipelineConfig(document_class="book")).convert(md)


def test_bolunmus_quot_cozulur():
    tex = _tex('# Bölüm\n\nAjan şunu yazdı: <sup>&</sup>quot;**MERHABA\\_dunya**" ve sustu.\n')
    assert "&quot;" not in tex and "\\&quot" not in tex
    assert "MERHABA" in tex


def test_bolunmus_amp_ve_sayisal_varlik():
    tex = _tex("# Bölüm\n\nA <sup>&</sup>amp; B ve <sup>&</sup>#36; işareti.\n")
    assert "&amp;" not in tex and "&#36;" not in tex


def test_gercek_ust_simge_dokunulmaz():
    tex = _tex("# Bölüm\n\nDeğer x<sup>2</sup> olur.\n")
    assert "2" in tex and "&" not in tex.replace("\\&", "")
