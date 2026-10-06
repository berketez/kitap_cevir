"""
Pixel Diff - PDF karşılaştırma aracı

Orijinal ve çevrilmiş PDF'leri piksel bazında karşılaştırır.
QA için yan yana ve fark görselleri oluşturur.

VISION.md: "Sadece metin alanlari farkli, layout ayni"

Kullanim:
    result = compare_pdfs("original.pdf", "translated.pdf")
    generate_report(result, "qa_report.html")
"""

import logging
from pathlib import Path
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass, field
import tempfile
import os

logger = logging.getLogger(__name__)

# Lazy imports
fitz = None
Image = None
ImageChops = None


def _ensure_imports():
    """Lazy import PIL ve fitz"""
    global fitz, Image, ImageChops

    if fitz is None:
        import fitz as _fitz
        fitz = _fitz

    if Image is None:
        try:
            from PIL import Image as _Image
            from PIL import ImageChops as _ImageChops
            Image = _Image
            ImageChops = _ImageChops
        except ImportError:
            logger.warning("PIL bulunamadı, piksel karşılaştırması sınırlı çalışıyor")


@dataclass
class PageDiff:
    """Sayfa karsilastirma sonucu"""
    page_num: int
    original_path: str = ""
    translated_path: str = ""
    diff_path: str = ""
    difference_ratio: float = 0.0  # 0-1 arasi fark orani
    pixel_count: int = 0  # Farkli piksel sayisi
    total_pixels: int = 0
    has_significant_diff: bool = False


@dataclass
class PDFComparisonResult:
    """PDF karşılaştırma sonucu.

    ``total_pages`` orijinal PDF'nin sayfa sayısıdır (geriye uyum).
    ``original_pages`` / ``translated_pages`` iki PDF'nin gerçek sayfa
    sayıları; fark ``page_count_difference`` ve kullanıcıya dönük
    ``page_count_note`` ile okunur (2026-09-25, U18: fark eskiden yalnız
    günlüğe yazılıyor, arayüz gösteremiyordu).
    """
    original_path: str
    translated_path: str
    total_pages: int
    compared_pages: int
    page_diffs: List[PageDiff] = field(default_factory=list)
    average_difference: float = 0.0
    max_difference: float = 0.0
    pages_with_issues: List[int] = field(default_factory=list)
    output_dir: str = ""
    original_pages: int = 0
    translated_pages: int = 0

    @property
    def page_count_difference(self) -> int:
        """Çevrilmiş eksi orijinal sayfa sayısı (0 = eşit)."""
        return self.translated_pages - self.original_pages

    @property
    def page_count_note(self) -> Optional[str]:
        """Sayfa sayıları farklıysa kullanıcıya dönük Türkçe not; eşitse None.

        Yalnız ilk ``compared_pages`` sayfa karşılaştırılır; fazladan
        sayfalar karşılaştırmaya girmez.
        """
        diff = self.page_count_difference
        if diff == 0:
            return None
        more = "fazla" if diff > 0 else "az"
        return (f"Sayfa sayısı farklı: orijinal {self.original_pages}, "
                f"çevrilmiş {self.translated_pages} sayfa (çevrilmiş PDF "
                f"{abs(diff)} sayfa {more}). Yalnız ilk "
                f"{self.compared_pages} sayfa karşılaştırıldı.")


def compare_pdfs(
    original_pdf: str,
    translated_pdf: str,
    output_dir: str = None,
    dpi: int = 150,
    threshold: float = 0.05,
    max_pages: int = None
) -> PDFComparisonResult:
    """
    Iki PDF'yi piksel bazinda karsilastir.

    Args:
        original_pdf: Orijinal PDF yolu
        translated_pdf: Cevrilmis PDF yolu
        output_dir: Cikti dizini (None = gecici)
        dpi: Render cozunurlugu
        threshold: Fark esigi (0-1)
        max_pages: Maksimum sayfa sayisi (bellek icin)

    Returns:
        PDFComparisonResult
    """
    _ensure_imports()

    if output_dir is None:
        output_dir = tempfile.mkdtemp(prefix="pixel_diff_")
    else:
        os.makedirs(output_dir, exist_ok=True)

    result = PDFComparisonResult(
        original_path=original_pdf,
        translated_path=translated_pdf,
        total_pages=0,
        compared_pages=0,
        output_dir=output_dir
    )

    try:
        orig_doc = fitz.open(original_pdf)
        trans_doc = fitz.open(translated_pdf)

        result.total_pages = len(orig_doc)
        result.original_pages = len(orig_doc)
        result.translated_pages = len(trans_doc)

        # Karşılaştırılacak sayfa sayısı
        pages_to_compare = min(len(orig_doc), len(trans_doc))
        if max_pages:
            pages_to_compare = min(pages_to_compare, max_pages)

        # Sayfa sayısı farkı varsa uyar (not sonuçta da durur: page_count_note)
        if len(orig_doc) != len(trans_doc):
            logger.warning(
                f"Sayfa sayısı farkı: orijinal={len(orig_doc)}, "
                f"çevrilmiş={len(trans_doc)}"
            )

        total_diff = 0.0
        max_diff = 0.0

        for page_num in range(pages_to_compare):
            page_diff = _compare_page(
                orig_doc[page_num],
                trans_doc[page_num],
                page_num,
                output_dir,
                dpi,
                threshold
            )

            result.page_diffs.append(page_diff)

            if page_diff.has_significant_diff:
                result.pages_with_issues.append(page_num)

            total_diff += page_diff.difference_ratio
            max_diff = max(max_diff, page_diff.difference_ratio)

        result.compared_pages = pages_to_compare
        result.average_difference = total_diff / pages_to_compare if pages_to_compare > 0 else 0
        result.max_difference = max_diff

        orig_doc.close()
        trans_doc.close()

        logger.info(
            f"PDF karşılaştırma tamamlandı: {pages_to_compare} sayfa, "
            f"ortalama fark: {result.average_difference:.2%}"
        )

        return result

    except Exception as e:
        logger.error(f"PDF karşılaştırma hatası: {e}")
        raise


def _compare_page(
    orig_page,
    trans_page,
    page_num: int,
    output_dir: str,
    dpi: int,
    threshold: float
) -> PageDiff:
    """Tek sayfa karsilastir"""
    _ensure_imports()

    result = PageDiff(page_num=page_num)

    try:
        # Sayfalari PNG olarak render et
        mat = fitz.Matrix(dpi / 72, dpi / 72)

        orig_pix = orig_page.get_pixmap(matrix=mat)
        trans_pix = trans_page.get_pixmap(matrix=mat)

        # Dosya yollari
        orig_path = os.path.join(output_dir, f"page_{page_num:03d}_original.png")
        trans_path = os.path.join(output_dir, f"page_{page_num:03d}_translated.png")
        diff_path = os.path.join(output_dir, f"page_{page_num:03d}_diff.png")

        # PNG kaydet
        orig_pix.save(orig_path)
        trans_pix.save(trans_path)

        result.original_path = orig_path
        result.translated_path = trans_path

        # PIL ile karsilastir
        if Image and ImageChops:
            orig_img = Image.open(orig_path).convert("RGB")
            trans_img = Image.open(trans_path).convert("RGB")

            # Boyut esitle (gerekirse)
            if orig_img.size != trans_img.size:
                # Buyuk olana boyutla
                max_width = max(orig_img.width, trans_img.width)
                max_height = max(orig_img.height, trans_img.height)

                new_orig = Image.new("RGB", (max_width, max_height), "white")
                new_trans = Image.new("RGB", (max_width, max_height), "white")

                new_orig.paste(orig_img, (0, 0))
                new_trans.paste(trans_img, (0, 0))

                orig_img = new_orig
                trans_img = new_trans

            # Fark hesapla
            diff = ImageChops.difference(orig_img, trans_img)

            # Fark gorselini kaydet
            diff.save(diff_path)
            result.diff_path = diff_path

            # Farkli piksel sayisi
            diff_pixels = diff.point(lambda x: 255 if x > 10 else 0)
            total_pixels = orig_img.width * orig_img.height
            diff_count = sum(1 for p in diff_pixels.getdata() if sum(p) > 0)

            result.total_pixels = total_pixels
            result.pixel_count = diff_count
            result.difference_ratio = diff_count / total_pixels if total_pixels > 0 else 0
            result.has_significant_diff = result.difference_ratio > threshold

    except Exception as e:
        logger.error(f"Sayfa karşılaştırma hatası (sayfa {page_num}): {e}")

    return result


def generate_report(
    result: PDFComparisonResult,
    output_path: str,
    include_images: bool = True
) -> str:
    """
    HTML rapor olustur.

    Args:
        result: Karsilastirma sonucu
        output_path: Cikti HTML yolu
        include_images: Gorselleri base64 olarak ekle

    Returns:
        Rapor dosya yolu
    """
    import base64
    from html import escape as html_escape

    def image_to_base64(path: str) -> str:
        """Gorseli base64'e cevir"""
        if not path or not os.path.exists(path):
            return ""
        try:
            with open(path, "rb") as f:
                return base64.b64encode(f.read()).decode()
        except (IOError, FileNotFoundError):
            return ""

    # Sayfa sayısı farkı notu (varsa) raporun özet kutusunun altında
    note = result.page_count_note
    page_count_html = (
        f'        <p class="issue">{html_escape(note)}</p>\n' if note else ""
    )

    # HTML sablonu
    html = f"""
<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PDF Karşılaştırma Raporu</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0a0a0f;
            color: #e0e0e0;
            margin: 0;
            padding: 2rem;
        }}
        .container {{ max-width: 1400px; margin: 0 auto; }}
        h1 {{ color: #8b5cf6; border-bottom: 2px solid #333; padding-bottom: 1rem; }}
        h2 {{ color: #a5b4fc; margin-top: 2rem; }}
        .summary {{
            background: rgba(99, 102, 241, 0.1);
            border: 1px solid rgba(99, 102, 241, 0.3);
            border-radius: 12px;
            padding: 1.5rem;
            margin: 1rem 0;
        }}
        .stat {{
            display: inline-block;
            margin-right: 2rem;
            padding: 0.5rem 1rem;
            background: rgba(255,255,255,0.05);
            border-radius: 8px;
        }}
        .stat-value {{ font-size: 1.5rem; font-weight: 600; color: #fff; }}
        .stat-label {{ font-size: 0.8rem; color: #888; }}
        .page-row {{
            display: grid;
            grid-template-columns: 1fr 1fr 1fr;
            gap: 1rem;
            margin: 1rem 0;
            padding: 1rem;
            background: rgba(255,255,255,0.02);
            border-radius: 8px;
        }}
        .page-col {{ text-align: center; }}
        .page-col img {{
            max-width: 100%;
            border: 1px solid #333;
            border-radius: 4px;
        }}
        .page-label {{
            font-size: 0.8rem;
            color: #888;
            margin-top: 0.5rem;
        }}
        .issue {{ border-left: 3px solid #ef4444; padding-left: 1rem; }}
        .ok {{ border-left: 3px solid #22c55e; padding-left: 1rem; }}
        .diff-ratio {{
            font-size: 0.9rem;
            padding: 0.2rem 0.5rem;
            border-radius: 4px;
            display: inline-block;
        }}
        .diff-low {{ background: rgba(34, 197, 94, 0.2); color: #22c55e; }}
        .diff-medium {{ background: rgba(245, 158, 11, 0.2); color: #f59e0b; }}
        .diff-high {{ background: rgba(239, 68, 68, 0.2); color: #ef4444; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>PDF Karşılaştırma Raporu</h1>

        <div class="summary">
            <div class="stat">
                <div class="stat-value">{result.compared_pages}</div>
                <div class="stat-label">Karşılaştırılan Sayfa</div>
            </div>
            <div class="stat">
                <div class="stat-value">{result.average_difference:.1%}</div>
                <div class="stat-label">Ortalama Fark</div>
            </div>
            <div class="stat">
                <div class="stat-value">{result.max_difference:.1%}</div>
                <div class="stat-label">Maksimum Fark</div>
            </div>
            <div class="stat">
                <div class="stat-value">{len(result.pages_with_issues)}</div>
                <div class="stat-label">Sorunlu Sayfa</div>
            </div>
            <div class="stat">
                <div class="stat-value">{result.original_pages} / {result.translated_pages}</div>
                <div class="stat-label">Sayfa Sayısı (orijinal / çevrilmiş)</div>
            </div>
        </div>
{page_count_html}
        <h2>Dosyalar</h2>
        <p><strong>Orijinal:</strong> {Path(result.original_path).name}</p>
        <p><strong>Çevrilmiş:</strong> {Path(result.translated_path).name}</p>

        <h2>Sayfa Karşılaştırmaları</h2>
"""

    # Her sayfa icin karsilastirma
    for page_diff in result.page_diffs:
        issue_class = "issue" if page_diff.has_significant_diff else "ok"

        if page_diff.difference_ratio < 0.02:
            diff_class = "diff-low"
        elif page_diff.difference_ratio < 0.10:
            diff_class = "diff-medium"
        else:
            diff_class = "diff-high"

        html += f"""
        <div class="page-row {issue_class}">
            <div class="page-col">
                <strong>Sayfa {page_diff.page_num + 1}</strong>
                <span class="diff-ratio {diff_class}">
                    {page_diff.difference_ratio:.2%} fark
                </span>
            </div>
"""

        if include_images and page_diff.original_path:
            orig_b64 = image_to_base64(page_diff.original_path)
            trans_b64 = image_to_base64(page_diff.translated_path)
            diff_b64 = image_to_base64(page_diff.diff_path)

            if orig_b64:
                html += f"""
            <div class="page-col">
                <img src="data:image/png;base64,{orig_b64}" alt="Orijinal">
                <div class="page-label">Orijinal</div>
            </div>
"""
            if trans_b64:
                html += f"""
            <div class="page-col">
                <img src="data:image/png;base64,{trans_b64}" alt="Çevrilmiş">
                <div class="page-label">Çevrilmiş</div>
            </div>
"""
            if diff_b64:
                html += f"""
            <div class="page-col">
                <img src="data:image/png;base64,{diff_b64}" alt="Fark">
                <div class="page-label">Fark</div>
            </div>
"""
        else:
            html += """
            <div class="page-col"><em>(Görsel yok)</em></div>
            <div class="page-col"><em>(Görsel yok)</em></div>
            <div class="page-col"><em>(Görsel yok)</em></div>
"""

        html += """
        </div>
"""

    html += """
    </div>
</body>
</html>
"""

    # Kaydet
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    logger.info(f"Rapor oluşturuldu: {output_path}")

    return output_path


def quick_compare(
    original_pdf: str,
    translated_pdf: str,
    pages: List[int] = None
) -> Dict:
    """
    Hizli karsilastirma (gorsel olmadan).

    Args:
        original_pdf: Orijinal PDF
        translated_pdf: Cevrilmis PDF
        pages: Belirli sayfalar (None = hepsi)

    Returns:
        Ozet dict
    """
    _ensure_imports()

    try:
        orig_doc = fitz.open(original_pdf)
        trans_doc = fitz.open(translated_pdf)

        if pages is None:
            pages = list(range(min(len(orig_doc), len(trans_doc))))

        differences = []

        for page_num in pages:
            if page_num >= len(orig_doc) or page_num >= len(trans_doc):
                continue

            # Dusuk DPI ile hizli karsilastirma
            mat = fitz.Matrix(72 / 72, 72 / 72)  # 72 DPI

            orig_pix = orig_doc[page_num].get_pixmap(matrix=mat)
            trans_pix = trans_doc[page_num].get_pixmap(matrix=mat)

            # Byte karsilastirma
            orig_bytes = orig_pix.samples
            trans_bytes = trans_pix.samples

            if len(orig_bytes) == len(trans_bytes):
                diff_count = sum(1 for a, b in zip(orig_bytes, trans_bytes) if a != b)
                ratio = diff_count / len(orig_bytes)
            else:
                ratio = 1.0  # Tamamen farkli

            differences.append({
                "page": page_num,
                "difference_ratio": ratio
            })

        orig_doc.close()
        trans_doc.close()

        return {
            "pages_compared": len(differences),
            "average_difference": sum(d["difference_ratio"] for d in differences) / len(differences) if differences else 0,
            "differences": differences
        }

    except Exception as e:
        logger.error(f"Hızlı karşılaştırma hatası: {e}")
        return {"error": str(e)}


# =============================================================================
# TEST
# =============================================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("=" * 60)
    print("Pixel Diff Test")
    print("=" * 60)

    # Test icin ornek PDF olustur
    _ensure_imports()

    # Ornek orijinal PDF
    orig_doc = fitz.open()
    page = orig_doc.new_page(width=595, height=842)
    page.insert_text((100, 100), "Hello World", fontsize=24)
    page.insert_text((100, 150), "This is original text.", fontsize=14)

    orig_path = "/tmp/test_original.pdf"
    orig_doc.save(orig_path)
    orig_doc.close()

    # Ornek cevrilmis PDF
    trans_doc = fitz.open()
    page = trans_doc.new_page(width=595, height=842)
    page.insert_text((100, 100), "Merhaba Dunya", fontsize=24)
    page.insert_text((100, 150), "Bu orijinal metindir.", fontsize=14)

    trans_path = "/tmp/test_translated.pdf"
    trans_doc.save(trans_path)
    trans_doc.close()

    # Karsilastir
    print("\n--- Karsilastirma ---")
    result = compare_pdfs(orig_path, trans_path)

    print(f"Karsilastirilan: {result.compared_pages} sayfa")
    print(f"Ortalama fark: {result.average_difference:.2%}")
    print(f"Maksimum fark: {result.max_difference:.2%}")

    # Rapor olustur
    report_path = "/tmp/pixel_diff_report.html"
    generate_report(result, report_path)
    print(f"Rapor: {report_path}")

    # Hizli karsilastirma
    print("\n--- Hizli Karsilastirma ---")
    quick = quick_compare(orig_path, trans_path)
    print(f"Ortalama fark: {quick.get('average_difference', 0):.2%}")

    print("\n" + "=" * 60)
    print("Test tamamlandi!")
