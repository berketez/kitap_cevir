"""Bağlama (5 Ekim 2026): marker_onarim ve pdf_metin_onarim converter.convert()'te.

- pdf_metin_onarim.onar her çağrıda kaynak PDF yoluyla çağrılır (PDF yoksa
  modül Markdown'ı aynen döndürür).
- marker_onarim.onar_rapor yalnız kaynak PDF gerçekten varsa (Marker yolu)
  çağrılır; EPUB yolunda (source_book.pdf yok) çağrılmaz.
- Sıra: pdf_metin_onarim → _wrap_pdf_monospace → _preprocess_markdown →
  marker_onarim → _extract_footnotes.
"""
import sys
from pathlib import Path

import fitz
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from latex_pipeline import converter as conv_mod  # noqa: E402
from latex_pipeline.config import PipelineConfig  # noqa: E402
from latex_pipeline.converter import MarkdownToLatex  # noqa: E402


def _sentetik_pdf(yol: Path) -> Path:
    doc = fitz.open()
    sayfa = doc.new_page()
    sayfa.insert_text((72, 72), "Birinci paragraf burada bitmiyor ve")
    doc.save(str(yol))
    return yol


@pytest.fixture
def cagrilar(monkeypatch):
    kayit = []
    gercek_pmo = conv_mod.pdf_metin_onarim.onar
    gercek_mo = conv_mod.marker_onarim.onar_rapor

    def pmo(md, pdf):
        kayit.append(("pdf_metin_onarim", pdf))
        return gercek_pmo(md, pdf)

    def mo(md):
        kayit.append(("marker_onarim", None))
        return gercek_mo(md)

    monkeypatch.setattr(conv_mod.pdf_metin_onarim, "onar", pmo)
    monkeypatch.setattr(conv_mod.marker_onarim, "onar_rapor", mo)
    return kayit


_MD = ("# Bölüm\n\nBirinci paragraf burada bitmiyor ve\n\n17\n\n"
       "sonraki sayfada devam ediyor.\n")


def test_pdf_varken_iki_modul_de_calisir(tmp_path, cagrilar):
    pdf = _sentetik_pdf(tmp_path / "source_book.pdf")
    c = MarkdownToLatex(PipelineConfig(input_pdf=str(pdf), document_class="book"))
    tex = c.convert(_MD)
    adlar = [a for a, _ in cagrilar]
    assert adlar == ["pdf_metin_onarim", "marker_onarim"]
    assert cagrilar[0][1] == str(pdf)
    assert set(c.marker_onarim_sayaci) >= {"paragraf_birlesti", "sayfa_no_silindi"}


def test_marker_onarim_ciktisi_kullanilir(tmp_path, monkeypatch):
    """Onarımın döndürdüğü Markdown dönüşüme girer (bağlama kanıtı;
    onarım kurallarının kendisi tests/test_G1b*_g.py'de)."""
    pdf = _sentetik_pdf(tmp_path / "source_book.pdf")
    monkeypatch.setattr(conv_mod.marker_onarim, "onar_rapor",
                        lambda md: (md.replace("bitmiyor ve", "ONARILDI"),
                                    {"paragraf_birlesti": 1}))
    c = MarkdownToLatex(PipelineConfig(input_pdf=str(pdf), document_class="book"))
    tex = c.convert(_MD)
    assert "ONARILDI" in tex and "bitmiyor ve" not in tex
    assert c.marker_onarim_sayaci == {"paragraf_birlesti": 1}


def test_epub_yolunda_marker_onarim_cagrilmaz(tmp_path, cagrilar):
    yok = tmp_path / "source_book.pdf"  # EPUB girdisinde bu dosya yok
    c = MarkdownToLatex(PipelineConfig(input_pdf=str(yok), document_class="book"))
    c.convert(_MD)
    assert [a for a, _ in cagrilar] == ["pdf_metin_onarim"]
    assert c.marker_onarim_sayaci == {}


def test_pdf_yolu_verilmeden(cagrilar):
    c = MarkdownToLatex(PipelineConfig(document_class="book"))
    c.convert(_MD)
    assert [a for a, _ in cagrilar] == ["pdf_metin_onarim"]
