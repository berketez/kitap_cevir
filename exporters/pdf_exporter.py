"""
PDF Exporter - Akıllı Çeviri
Kod, tablo, formül → resim olarak koru
Düz metin → çevir
"""

import fitz  # PyMuPDF
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Callable
import re


class PDFExporter:
    """
    Akıllı PDF çeviri exporter

    Yaklaşım:
    1. Sayfadaki içerik tiplerini tespit et (metin, kod, tablo, formül, resim)
    2. Kod/tablo/formül → resim olarak render et, çevirme
    3. Düz metin → çevir
    4. Yeni PDF oluştur (içerik akar, sayfa sayısı değişebilir)
    """

    # Kod bloğu tespiti için pattern'lar
    CODE_PATTERNS = [
        r'^\s*def\s+\w+\s*\(',           # Python function
        r'^\s*class\s+\w+',               # Python class
        r'^\s*import\s+\w+',              # Python import
        r'^\s*from\s+\w+\s+import',       # Python from import
        r'^\s*if\s+.*:$',                 # Python if
        r'^\s*for\s+.*:$',                # Python for
        r'^\s*while\s+.*:$',              # Python while
        r'^\s*return\s+',                 # Python return
        r'^\s*async\s+def',               # Python async
        r'^\s*@\w+',                       # Decorator
        r'^\s*print\s*\(',                # print()
        r'^\s*assert\s+',                 # assert
        r'^\s*try\s*:',                   # try
        r'^\s*except\s*',                 # except
        r'^\s*raise\s+',                  # raise
        r'^\s*with\s+.*:',                # with
        r'^\s*lambda\s+',                 # lambda
        r'\b(True|False|None)\b',         # Python keywords
        r'^\s*#.*$',                       # Comment
        r'```',                            # Markdown code block
        r'^\s*\$.*\$$',                   # LaTeX inline
        r'^\s*\\\[',                       # LaTeX display
        r'^\s*SELECT\s+',                 # SQL
        r'^\s*INSERT\s+',                 # SQL
        r'^\s*UPDATE\s+',                 # SQL
        r'^\s*CREATE\s+',                 # SQL
        r'^\s*function\s+\w+',            # JavaScript
        r'^\s*const\s+\w+',               # JavaScript
        r'^\s*let\s+\w+',                 # JavaScript
        r'^\s*var\s+\w+',                 # JavaScript
        r'=>',                             # Arrow function
        r'^\s*public\s+',                 # Java/C#
        r'^\s*private\s+',                # Java/C#
        r'^\s*void\s+\w+',                # C/C++/Java
        r'^\s*int\s+\w+',                 # C/C++
        r'^\s*#include',                  # C/C++
    ]

    # Formül tespiti
    FORMULA_PATTERNS = [
        r'[∑∏∫∂∇√∞±≠≤≥≈∈∉⊂⊃∩∪]',  # Matematik sembolleri
        r'\b(sin|cos|tan|log|ln|exp)\b',   # Matematik fonksiyonları
        r'[α-ωΑ-Ω]',                        # Yunan harfleri
        r'\^\{.*\}',                        # Üst indis
        r'_\{.*\}',                         # Alt indis
        r'\\frac\{',                        # LaTeX frac
        r'\\sqrt\{',                        # LaTeX sqrt
        r'\\sum',                           # LaTeX sum
        r'\\int',                           # LaTeX integral
    ]

    def __init__(self):
        self.font_name = "helv"
        self.default_font_size = 11
        self.margin = 50
        self.image_dpi = 300  # Yüksek kalite render
        self._pending_overflow = []  # Sığmayan metin parçaları

    def export(self, original_pdf_path: str, translations: Dict[str, str],
               output_path: str, progress_callback: Callable = None) -> str:
        """
        PDF'yi çevir - akıllı içerik tespiti ile

        Args:
            original_pdf_path: Orijinal PDF
            translations: {orijinal: çeviri} sözlüğü
            output_path: Çıktı yolu
            progress_callback: İlerleme callback
        """
        src_doc = fitz.open(original_pdf_path)
        total_pages = src_doc.page_count

        # Çeviri lookup
        trans_lookup = self._build_translation_lookup(translations)

        print(f"[PDF Export] {total_pages} sayfa işlenecek")
        print(f"[PDF Export] {len(trans_lookup)} çeviri mevcut")

        # Yeni PDF oluştur
        new_doc = fitz.open()

        # Geçici resimler için klasör
        temp_dir = Path(output_path).parent / ".pdf_temp"
        temp_dir.mkdir(exist_ok=True)

        for page_num in range(total_pages):
            src_page = src_doc[page_num]

            # Sayfa içeriğini analiz et
            content_blocks = self._analyze_page(src_page)

            # Yeni sayfa oluştur (aynı boyut)
            new_page = new_doc.new_page(
                width=src_page.rect.width,
                height=src_page.rect.height
            )

            # Her bloğu işle
            for block in content_blocks:
                if block["type"] == "image":
                    # Orijinal resmi kopyala
                    self._copy_image(src_page, new_page, block)

                elif block["type"] in ["code", "table", "formula"]:
                    # Render as image - çevirme
                    self._render_block_as_image(src_page, new_page, block, temp_dir)

                elif block["type"] == "text":
                    # Metni çevir ve yaz
                    self._write_translated_text(new_page, block, trans_lookup)

            # Overflow varsa yeni sayfa(lar) ekle
            while self._pending_overflow:
                overflow_items = self._pending_overflow.copy()
                self._pending_overflow.clear()

                overflow_page = new_doc.new_page(
                    width=src_page.rect.width,
                    height=src_page.rect.height
                )

                # İçerik alanı: margin ile sınırlı
                content_bbox = fitz.Rect(
                    self.margin,
                    self.margin,
                    src_page.rect.width - self.margin,
                    src_page.rect.height - self.margin
                )

                y_cursor = content_bbox.y0

                for ov_item in overflow_items:
                    ov_text = ov_item["text"]
                    ov_font_size = ov_item["font_size"]

                    if not ov_text.strip():
                        continue

                    remaining_bbox = fitz.Rect(
                        content_bbox.x0,
                        y_cursor,
                        content_bbox.x1,
                        content_bbox.y1
                    )

                    if remaining_bbox.height < ov_font_size * 1.2:
                        # Bu sayfada yer kalmadı, bir sonraki overflow'a ekle
                        self._pending_overflow.append(ov_item)
                        continue

                    rc = overflow_page.insert_textbox(
                        remaining_bbox,
                        ov_text,
                        fontname=self.font_name,
                        fontsize=ov_font_size,
                        color=(0, 0, 0),
                        align=fitz.TEXT_ALIGN_LEFT
                    )

                    if rc < 0:
                        # Hala sığmıyor, sığanı yaz, kalanı ayır
                        fitted, remainder = self._split_text_for_box(
                            overflow_page, remaining_bbox, ov_text, ov_font_size
                        )
                        if fitted:
                            overflow_page.draw_rect(remaining_bbox, color=(1, 1, 1), fill=(1, 1, 1))
                            overflow_page.insert_textbox(
                                remaining_bbox,
                                fitted,
                                fontname=self.font_name,
                                fontsize=ov_font_size,
                                color=(0, 0, 0),
                                align=fitz.TEXT_ALIGN_LEFT
                            )
                        if remainder:
                            self._pending_overflow.append({
                                "text": remainder,
                                "font_size": ov_font_size
                            })
                    else:
                        # Sığdı, cursor'ı ilerlet
                        # rc = son satırın alt kenarından kalan alan (pozitif)
                        used_height = remaining_bbox.height - rc
                        y_cursor += used_height + ov_font_size * 0.5

                print(f"[PDF Export] Overflow sayfası eklendi (sayfa {page_num + 1} sonrası)")

            if progress_callback:
                progress_callback(page_num + 1, total_pages)

            if (page_num + 1) % 10 == 0:
                print(f"[PDF Export] {page_num + 1}/{total_pages}")

        # Kaydet
        new_doc.save(output_path, garbage=4, deflate=True)
        new_doc.close()
        src_doc.close()

        # Temp klasörü temizle
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)

        print(f"[PDF Export] Tamamlandı: {output_path}")
        return output_path

    def _analyze_page(self, page) -> List[Dict]:
        """
        Sayfadaki içerik bloklarını analiz et ve tiplerini belirle
        """
        blocks = []

        # Metin bloklarını al
        text_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)

        for block in text_dict.get("blocks", []):
            if block.get("type") == 1:  # Image block
                blocks.append({
                    "type": "image",
                    "bbox": fitz.Rect(block["bbox"]),
                    "data": block
                })
            elif block.get("type") == 0:  # Text block
                block_text = self._extract_block_text(block)
                block_type = self._detect_content_type(block_text, block)

                blocks.append({
                    "type": block_type,
                    "bbox": fitz.Rect(block["bbox"]),
                    "text": block_text,
                    "data": block
                })

        # Resimleri de ekle
        for img in page.get_images():
            xref = img[0]
            for img_info in page.get_image_info():
                if img_info.get("xref") == xref:
                    bbox = img_info.get("bbox")
                    if bbox:
                        blocks.append({
                            "type": "image",
                            "bbox": fitz.Rect(bbox),
                            "xref": xref
                        })
                    break

        # Y koordinatına göre sırala (yukarıdan aşağı)
        blocks.sort(key=lambda b: (b["bbox"].y0, b["bbox"].x0))

        return blocks

    def _extract_block_text(self, block: dict) -> str:
        """Bloktan tüm metni çıkar"""
        texts = []
        for line in block.get("lines", []):
            line_text = ""
            for span in line.get("spans", []):
                line_text += span.get("text", "")
            texts.append(line_text)
        return "\n".join(texts)

    def _detect_content_type(self, text: str, block: dict) -> str:
        """
        İçerik tipini tespit et: text, code, table, formula
        """
        if not text.strip():
            return "text"

        lines = text.strip().split("\n")

        # Kod tespiti
        code_score = 0
        for line in lines:
            for pattern in self.CODE_PATTERNS:
                if re.search(pattern, line):
                    code_score += 1
                    break

        # Satırların %30'undan fazlası kod gibi görünüyorsa
        if len(lines) > 0 and code_score / len(lines) > 0.3:
            return "code"

        # Formül tespiti
        formula_score = 0
        for pattern in self.FORMULA_PATTERNS:
            if re.search(pattern, text):
                formula_score += 1

        if formula_score >= 2:
            return "formula"

        # Tablo tespiti - çok sayıda | veya düzenli boşluk yapısı
        if text.count("|") > 3:
            return "table"

        # Monospace font kontrolü (kod göstergesi)
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                font = span.get("font", "").lower()
                if "mono" in font or "courier" in font or "consolas" in font:
                    return "code"

        return "text"

    def _copy_image(self, src_page, dst_page, block: dict):
        """Orijinal resmi yeni sayfaya kopyala"""
        bbox = block["bbox"]

        # Bölgeyi resim olarak render et ve kopyala
        clip = bbox
        pix = src_page.get_pixmap(clip=clip, dpi=self.image_dpi)

        # Resmi yeni sayfaya ekle
        dst_page.insert_image(bbox, pixmap=pix)

    def _render_block_as_image(self, src_page, dst_page, block: dict, temp_dir: Path):
        """
        Kod/tablo/formül bloğunu resim olarak render et
        Çevrilmeden orijinal haliyle kalır
        """
        bbox = block["bbox"]

        # Biraz padding ekle
        padded_bbox = fitz.Rect(
            bbox.x0 - 2,
            bbox.y0 - 2,
            bbox.x1 + 2,
            bbox.y1 + 2
        )

        # Sayfa sınırları içinde kal
        padded_bbox = padded_bbox & src_page.rect

        # Yüksek kalitede render
        pix = src_page.get_pixmap(clip=padded_bbox, dpi=self.image_dpi)

        # Yeni sayfaya ekle
        dst_page.insert_image(bbox, pixmap=pix)

    def _write_translated_text(self, page, block: dict, trans_lookup: dict):
        """
        Çevrilmiş metni sayfaya yaz
        """
        original_text = block.get("text", "").strip()
        bbox = block["bbox"]

        if not original_text:
            return

        # Çeviri bul
        translated = self._find_translation(original_text, trans_lookup)
        if not translated:
            translated = original_text  # Çeviri yoksa orijinal

        # Font boyutunu orijinalden al
        font_size = self.default_font_size
        if "data" in block:
            for line in block["data"].get("lines", []):
                for span in line.get("spans", []):
                    font_size = span.get("size", self.default_font_size)
                    break
                break

        # Metni kutuya yaz — kural: yazı tipi küçültülmez
        try:
            # Önce arka planı temizle (beyaz)
            page.draw_rect(bbox, color=(1, 1, 1), fill=(1, 1, 1))

            # Metni orijinal font boyutuyla yaz
            rc = page.insert_textbox(
                bbox,
                translated,
                fontname=self.font_name,
                fontsize=font_size,
                color=(0, 0, 0),
                align=fitz.TEXT_ALIGN_LEFT
            )

            # Taşma varsa (rc < 0): font küçültme YOK, sayfa eklenir
            if rc < 0:
                # Sığan kısmı yaz, sığmayan kısmı overflow'a gönder
                fitted, remainder = self._split_text_for_box(
                    page, bbox, translated, font_size
                )

                # Arka planı tekrar temizle ve sadece sığanı yaz
                page.draw_rect(bbox, color=(1, 1, 1), fill=(1, 1, 1))
                if fitted:
                    page.insert_textbox(
                        bbox,
                        fitted,
                        fontname=self.font_name,
                        fontsize=font_size,
                        color=(0, 0, 0),
                        align=fitz.TEXT_ALIGN_LEFT
                    )

                # Sığmayan kısmı overflow listesine ekle
                if remainder:
                    self._pending_overflow.append({
                        "text": remainder,
                        "font_size": font_size
                    })

        except Exception as e:
            # Hata durumunda orijinal metni orijinal boyutla yaz
            try:
                page.draw_rect(bbox, color=(1, 1, 1), fill=(1, 1, 1))
                page.insert_textbox(
                    bbox,
                    original_text,
                    fontname=self.font_name,
                    fontsize=font_size,  # Orijinal boyut korunur
                    color=(0.3, 0.3, 0.3)
                )
            except Exception:
                pass  # Font/text insertion hatası

    def _split_text_for_box(self, page, bbox, text: str, font_size: float):
        """
        Metni kutuya sığan ve sığmayan kısımlara ayır.

        Binary search ile satır satır deneyerek, kutuya tam sığan
        en uzun metin parçasını bulur.

        Args:
            page: PyMuPDF sayfa objesi
            bbox: Hedef kutu (fitz.Rect)
            text: Tam metin
            font_size: Font boyutu

        Returns:
            (fitted_text, remainder_text) tuple
        """
        lines = text.split('\n')

        if len(lines) <= 1:
            # Tek satır — kelime bazında böl
            words = text.split()
            if len(words) <= 1:
                return (text, "")

            # Binary search: kaç kelime sığar?
            lo, hi = 0, len(words)
            best = 0

            while lo <= hi:
                mid = (lo + hi) // 2
                if mid == 0:
                    lo = mid + 1
                    continue

                test_text = ' '.join(words[:mid])
                rc = page.insert_textbox(
                    bbox,
                    test_text,
                    fontname=self.font_name,
                    fontsize=font_size,
                    color=(1, 1, 1),  # Beyaz — görünmez test
                    align=fitz.TEXT_ALIGN_LEFT
                )

                # Üzerine yazılanı temizle
                page.draw_rect(bbox, color=(1, 1, 1), fill=(1, 1, 1))

                if rc >= 0:
                    best = mid
                    lo = mid + 1
                else:
                    hi = mid - 1

            if best > 0:
                fitted = ' '.join(words[:best])
                remainder = ' '.join(words[best:])
                return (fitted, remainder)
            else:
                return ("", text)

        # Çok satırlı — satır bazında böl (binary search)
        lo, hi = 0, len(lines)
        best = 0

        while lo <= hi:
            mid = (lo + hi) // 2
            if mid == 0:
                lo = mid + 1
                continue

            test_text = '\n'.join(lines[:mid])
            rc = page.insert_textbox(
                bbox,
                test_text,
                fontname=self.font_name,
                fontsize=font_size,
                color=(1, 1, 1),  # Beyaz — görünmez test
                align=fitz.TEXT_ALIGN_LEFT
            )

            # Üzerine yazılanı temizle
            page.draw_rect(bbox, color=(1, 1, 1), fill=(1, 1, 1))

            if rc >= 0:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1

        if best > 0:
            fitted = '\n'.join(lines[:best])
            remainder = '\n'.join(lines[best:])
            return (fitted, remainder)
        else:
            return ("", text)

    def _build_translation_lookup(self, translations: Dict[str, str]) -> Dict[str, str]:
        """Çeviri lookup tablosu oluştur"""
        lookup = {}
        for orig, trans in translations.items():
            if orig and trans:
                # Normalize et
                normalized = self._normalize_text(orig)
                if normalized and len(normalized) > 2:
                    lookup[normalized] = trans
        return lookup

    def _find_translation(self, text: str, lookup: Dict[str, str]) -> Optional[str]:
        """Metin için çeviri bul"""
        if not text:
            return None

        normalized = self._normalize_text(text)

        # Tam eşleşme
        if normalized in lookup:
            return lookup[normalized]

        # Satır satır dene
        lines = text.split('\n')
        translated_lines = []
        any_translated = False

        for line in lines:
            line_normalized = self._normalize_text(line)
            if line_normalized in lookup:
                translated_lines.append(lookup[line_normalized])
                any_translated = True
            else:
                translated_lines.append(line)

        if any_translated:
            return '\n'.join(translated_lines)

        return None

    def _normalize_text(self, text: str) -> str:
        """Metin karşılaştırma için normalize et"""
        if not text:
            return ""
        text = re.sub(r'\s+', ' ', text).strip().lower()
        return text

    def extract_images(self, pdf_path: str, output_dir: str) -> List[Dict]:
        """PDF'den tüm resimleri çıkar"""
        doc = fitz.open(pdf_path)
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        images = []

        for page_num in range(doc.page_count):
            page = doc[page_num]
            img_list = page.get_images()

            for img_idx, img in enumerate(img_list):
                xref = img[0]

                try:
                    base_image = doc.extract_image(xref)
                    image_bytes = base_image["image"]
                    image_ext = base_image["ext"]

                    filename = f"page_{page_num+1:04d}_img_{img_idx+1:02d}.{image_ext}"
                    filepath = output_path / filename

                    with open(filepath, "wb") as f:
                        f.write(image_bytes)

                    # Resmin konumunu bul
                    img_bbox = None
                    for img_info in page.get_image_info():
                        if img_info.get("xref") == xref:
                            img_bbox = img_info.get("bbox")
                            break

                    images.append({
                        "path": str(filepath),
                        "page": page_num + 1,
                        "bbox": img_bbox,
                        "xref": xref
                    })

                except Exception as e:
                    print(f"[PDF] Resim hatası sayfa {page_num+1}: {e}")

        doc.close()
        print(f"[PDF] {len(images)} resim çıkarıldı → {output_dir}")

        return images


# Geliştirici denemesi: verilen PDF'in sayfalarındaki içerik bloklarını listeler.
#   python exporters/pdf_exporter.py <kitap.pdf> [sayfa_no ...]
# Sayfa numaraları 1'den başlar; verilmezse 1. sayfa incelenir. (Eskiden
# yazarın masaüstündeki bir kitap sabit yolla aranıyordu; açık yayın, KARAR 26.)
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Kullanım: python exporters/pdf_exporter.py <kitap.pdf> [sayfa_no ...]",
              file=sys.stderr)
        sys.exit(2)

    exporter = PDFExporter()
    pdf_path = sys.argv[1]
    print(f"Test: {pdf_path}\n")

    doc = fitz.open(pdf_path)
    try:
        sayfalar = [int(s) - 1 for s in sys.argv[2:]] or [0]
        for page_num in sayfalar:
            if not 0 <= page_num < len(doc):
                print(f"Sayfa {page_num + 1} yok (toplam {len(doc)} sayfa)",
                      file=sys.stderr)
                continue
            page = doc[page_num]
            print(f"{'='*50}")
            print(f"SAYFA {page_num + 1}")
            print(f"{'='*50}")

            blocks = exporter._analyze_page(page)

            for i, block in enumerate(blocks):
                print(f"\nBlok {i+1}: {block['type'].upper()}")
                print(f"  Konum: {block['bbox']}")
                if 'text' in block:
                    preview = block['text'][:100].replace('\n', ' ')
                    print(f"  Metin: {preview}...")
    finally:
        doc.close()
