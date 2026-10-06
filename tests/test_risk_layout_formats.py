"""Risk 1 — Layout detection edge cases.

13 Mart fiyaskosu: 288×466pt bir mühendislik kitabı A4'e sıkıştırıldı, 483
sayfa 93'e düştü. Bu testler farklı kitap formatlarında (cep kitabı,
mühendislik, bilim, kindle, square, landscape) LayoutDetector'ın
düzgün çalıştığını doğrular.

Strateji: fitz ile in-memory minimal PDF üret → detect() çağır →
page_width_mm/page_height_mm/paper_size_name doğru mu kontrol et.
HY-MT veya pipeline çalıştırılmaz; sadece layout detection izole edilir.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

fitz = pytest.importorskip("fitz")

from latex_pipeline.layout_detector import LayoutDetector


PT_PER_MM = 2.834645669


def _make_pdf(tmp_path: Path, w_pt: float, h_pt: float, n_pages: int = 3,
              text: str = "Lorem ipsum dolor sit amet, consectetur adipiscing.") -> str:
    """fitz ile özel boyutlu basit PDF üret."""
    pdf_path = tmp_path / f"test_{int(w_pt)}x{int(h_pt)}.pdf"
    doc = fitz.open()
    for _ in range(n_pages):
        page = doc.new_page(width=w_pt, height=h_pt)
        page.insert_text((40, 60), text, fontsize=11)
        page.insert_text((40, 100), text * 3, fontsize=11)
    doc.save(str(pdf_path))
    doc.close()
    return str(pdf_path)


class TestLayoutSizeDetection:
    """Page size doğru tespit ediliyor mu?"""

    def test_muhendislik_format_288x466pt(self, tmp_path):
        """13 Mart kurban: 288×466pt mühendislik kitabı."""
        pdf = _make_pdf(tmp_path, 288, 466)
        info = LayoutDetector().detect(pdf, sample_pages=3)
        # 288pt ≈ 101.6mm, 466pt ≈ 164.4mm
        assert 100 < info.page_width_mm < 105
        assert 162 < info.page_height_mm < 168
        # Bu boyut A4 değil — paper_size_name custom olmalı
        assert info.paper_size_name != "a4paper"

    def test_a4_standard(self, tmp_path):
        """A4 standart: 595×842pt."""
        pdf = _make_pdf(tmp_path, 595, 842)
        info = LayoutDetector().detect(pdf, sample_pages=3)
        assert 208 < info.page_width_mm < 212
        assert 295 < info.page_height_mm < 300

    def test_letter_format(self, tmp_path):
        """US Letter: 612×792pt = 215.9×279.4mm."""
        pdf = _make_pdf(tmp_path, 612, 792)
        info = LayoutDetector().detect(pdf, sample_pages=3)
        assert 214 < info.page_width_mm < 218
        assert 277 < info.page_height_mm < 281

    def test_a5_pocket_book(self, tmp_path):
        """A5 cep kitabı: 420×595pt = 148×210mm."""
        pdf = _make_pdf(tmp_path, 420, 595)
        info = LayoutDetector().detect(pdf, sample_pages=3)
        assert 146 < info.page_width_mm < 150
        assert 208 < info.page_height_mm < 212

    def test_b5_academic(self, tmp_path):
        """B5 akademik: 499×709pt = 176×250mm."""
        pdf = _make_pdf(tmp_path, 499, 709)
        info = LayoutDetector().detect(pdf, sample_pages=3)
        assert 174 < info.page_width_mm < 179
        assert 248 < info.page_height_mm < 253

    def test_kindle_6inch(self, tmp_path):
        """Kindle 6 inch: 332×497pt = 117.1×175.4mm."""
        pdf = _make_pdf(tmp_path, 332, 497)
        info = LayoutDetector().detect(pdf, sample_pages=3)
        assert 115 < info.page_width_mm < 120
        assert 173 < info.page_height_mm < 178

    def test_square_format(self, tmp_path):
        """Kare format (sanat/tasarım kitapları): 567×567pt = 200×200mm."""
        pdf = _make_pdf(tmp_path, 567, 567)
        info = LayoutDetector().detect(pdf, sample_pages=3)
        assert 198 < info.page_width_mm < 202
        assert 198 < info.page_height_mm < 202

    def test_landscape_a4(self, tmp_path):
        """Landscape A4 (sunum, atlas): 842×595pt."""
        pdf = _make_pdf(tmp_path, 842, 595)
        info = LayoutDetector().detect(pdf, sample_pages=3)
        # Landscape = width > height
        assert info.page_width_mm > info.page_height_mm
        assert 295 < info.page_width_mm < 300
        assert 208 < info.page_height_mm < 212

    def test_extreme_narrow(self, tmp_path):
        """Aşırı dar (manga/pulp): 240×400pt — pipeline patlamasın."""
        pdf = _make_pdf(tmp_path, 240, 400)
        info = LayoutDetector().detect(pdf, sample_pages=3)
        # Sadece crash olmasın; değer kontrolü gevşek
        assert info.page_width_mm > 0
        assert info.page_height_mm > 0

    def test_textbook_large(self, tmp_path):
        """Büyük ders kitabı: 720×936pt = 254×330.2mm."""
        pdf = _make_pdf(tmp_path, 720, 936)
        info = LayoutDetector().detect(pdf, sample_pages=3)
        assert 252 < info.page_width_mm < 257
        assert 327 < info.page_height_mm < 333


class TestLayoutResilience:
    """LayoutDetector hata durumlarında crash etmemeli."""

    def test_nonexistent_pdf_returns_default(self, tmp_path):
        info = LayoutDetector().detect(str(tmp_path / "yok.pdf"))
        # Default A4 dönmeli, exception fırlatmamalı
        assert info.page_width_mm == 210.0
        assert info.page_height_mm == 297.0

    def test_single_page_pdf(self, tmp_path):
        """Tek sayfalı PDF (kapak gibi). page_idx=min(1, count-1)=0 olmalı."""
        pdf = _make_pdf(tmp_path, 595, 842, n_pages=1)
        info = LayoutDetector().detect(pdf, sample_pages=3)
        assert 208 < info.page_width_mm < 212

    def test_empty_text_pages(self, tmp_path):
        """Metinsiz boş sayfalar (görsel kitap). Margin tespiti default 25mm."""
        pdf_path = tmp_path / "empty.pdf"
        doc = fitz.open()
        for _ in range(3):
            doc.new_page(width=595, height=842)
        doc.save(str(pdf_path))
        doc.close()
        info = LayoutDetector().detect(str(pdf_path))
        # Boş PDF margins default'e fallback yapmalı
        assert info.margin_top_mm > 0
        assert info.margin_left_mm > 0


class TestPaperSizeMapping:
    """Detect edilen boyut LaTeX paper_size_name'e doğru map oluyor mu?"""

    def test_a4_maps_correctly(self, tmp_path):
        pdf = _make_pdf(tmp_path, 595, 842)
        info = LayoutDetector().detect(pdf, sample_pages=3)
        # A4 yakınında → paper_size_name="a4paper" beklenir
        # (detector'ın eşik mantığına göre boş da olabilir)
        if info.paper_size_name:
            assert "a4" in info.paper_size_name.lower() or info.paper_size_name == ""

    def test_custom_size_no_standard_match(self, tmp_path):
        """288×466pt hiçbir standarda uymaz → custom (paper_size_name="")."""
        pdf = _make_pdf(tmp_path, 288, 466)
        info = LayoutDetector().detect(pdf, sample_pages=3)
        # Custom boyut: ya boş ya "custom" olmalı, kesinlikle "a4paper" olmamalı
        assert info.paper_size_name != "a4paper"
