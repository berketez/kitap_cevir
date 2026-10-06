"""
PDF Parser
PDF dosyalarından yapısal metin çıkarma
"""

import re
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Generator, Set
from pathlib import Path

# Monospace fontlar - kod bloklari icin
MONOSPACE_FONTS = {
    "courier", "consolas", "monaco", "menlo", "mono", "source code",
    "fira code", "jetbrains", "ubuntu mono", "dejavu mono", "liberation mono",
    "inconsolata", "anonymous", "droid mono", "roboto mono", "hack",
    "cour",  # PyMuPDF kisa adi
}

# Kod blogu pattern'lari
CODE_PATTERNS = [
    re.compile(r'^\s*(def|class|import|from|return|if|else|elif|for|while|try|except|with)\s'),  # Python
    re.compile(r'^\s*(function|const|let|var|return|if|else|for|while|try|catch)\s'),  # JavaScript
    re.compile(r'^\s*(public|private|protected|class|void|int|float|double|static)\s'),  # Java/C++
    re.compile(r'^\s*#include\s*<'),  # C/C++
    re.compile(r'^\s*\$[a-zA-Z_]'),  # Shell/PHP variables
    re.compile(r'=>\s*{'),  # Arrow functions
    re.compile(r'\)\s*{\s*$'),  # Function definitions
    re.compile(r'^\s*[a-z_]+\s*=\s*["\']'),  # Variable assignments
    re.compile(r'^\s*#\s*[a-z]+'),  # Shell comments or directives
]

def is_monospace_font(font_name: str) -> bool:
    """Font monospace mi kontrol et"""
    if not font_name:
        return False
    font_lower = font_name.lower()
    return any(mono in font_lower for mono in MONOSPACE_FONTS)

def looks_like_code(text: str) -> bool:
    """Metin kod gibi mi kontrol et"""
    if not text:
        return False
    
    # Cok kisa metinler kod degil
    if len(text) < 10:
        return False
    
    # Pattern kontrolu
    for pattern in CODE_PATTERNS:
        if pattern.search(text):
            return True
    
    # Ozel karakter yogunlugu kontrolu
    special_chars = set('{}[]()<>=;:+-*/&|!@#$%^~`')
    text_chars = set(text)
    special_count = len(text_chars & special_chars)
    
    # Cok fazla ozel karakter varsa muhtemelen kod
    if special_count > 5 and special_count / len(text) > 0.1:
        return True
    
    # Girintili satirlar (4+ bosluk ile baslayan)
    lines = text.split('\n')
    indented_lines = sum(1 for line in lines if line.startswith('    ') or line.startswith('\t'))
    if indented_lines > len(lines) * 0.5:
        return True
    
    return False


@dataclass
class TextBlock:
    """Metin bloğu"""
    text: str
    block_type: str = "paragraph"  # paragraph, heading, list, table, equation, caption, code
    page_num: int = 0
    bbox: tuple = None  # (x0, y0, x1, y1)
    font_size: float = 0
    font_name: str = ""
    is_bold: bool = False
    is_italic: bool = False
    is_code: bool = False  # Kod blogu mu?
    is_monospace: bool = False  # Monospace font mu?
    
    @property
    def should_translate(self) -> bool:
        """Bu blok cevirilmeli mi?"""
        # Kod bloklari cevirilmez
        if self.is_code or self.block_type == "code":
            return False
        # Denklemler cevirilmez
        if self.block_type == "equation":
            return False
        return True


@dataclass
class PDFPage:
    """PDF sayfası"""
    page_num: int
    blocks: List[TextBlock] = field(default_factory=list)
    width: float = 0
    height: float = 0


@dataclass
class PDFDocument:
    """PDF dokümanı"""
    path: str
    title: str = ""
    pages: List[PDFPage] = field(default_factory=list)
    metadata: Dict = field(default_factory=dict)

    @property
    def total_pages(self) -> int:
        return len(self.pages)

    def get_all_text(self) -> str:
        """Tüm metni birleştir"""
        texts = []
        for page in self.pages:
            for block in page.blocks:
                texts.append(block.text)
        return "\n\n".join(texts)

    def get_blocks_by_type(self, block_type: str) -> List[TextBlock]:
        """Belirli tipteki blokları getir"""
        blocks = []
        for page in self.pages:
            for block in page.blocks:
                if block.block_type == block_type:
                    blocks.append(block)
        return blocks

    def iter_blocks(self, deduplicate: bool = True, page_local: bool = True) -> Generator[TextBlock, None, None]:
        """Tüm blokları iterate et

        Args:
            deduplicate: Aynı metinleri filtrele (varsayılan True)
            page_local: True = sayfa bazlı dedup (Codex önerisi), False = global
        """
        global_seen = set()
        for page in self.pages:
            # PAGE-LOCAL seen_texts (Codex önerisi)
            page_seen = set() if page_local else global_seen

            for block in page.blocks:
                if deduplicate:
                    # Normalize et - # temizle, boşlukları normalize et (Codex önerisi)
                    text_key = block.text.strip().lstrip('#').strip()[:200]
                    text_key = ' '.join(text_key.split()).lower()

                    if page_local:
                        if text_key in page_seen:
                            continue
                        page_seen.add(text_key)
                    else:
                        if text_key in global_seen:
                            continue
                        global_seen.add(text_key)
                yield block


class PDFParser:
    """
    PDF'den yapısal metin çıkarıcı

    - Başlıkları tanır
    - Paragrafları ayırır
    - Tabloları tespit eder
    - Denklemleri korur
    - Şekil açıklamalarını ayırır
    """

    # Başlık pattern'ları
    HEADING_PATTERNS = [
        re.compile(r'^(\d+\.)+\s+[A-Z]'),  # 1.2.3 Heading
        re.compile(r'^Chapter\s+\d+', re.IGNORECASE),
        re.compile(r'^Section\s+\d+', re.IGNORECASE),
        re.compile(r'^CHAPTER\s+\d+'),
        re.compile(r'^[IVXLCDM]+\.\s+[A-Z]'),  # Roman numerals
    ]

    # Şekil/Tablo açıklaması pattern'ları
    CAPTION_PATTERNS = [
        re.compile(r'^Figure\s+\d+[.:]\s*', re.IGNORECASE),
        re.compile(r'^Fig\.\s*\d+[.:]\s*', re.IGNORECASE),
        re.compile(r'^Table\s+\d+[.:]\s*', re.IGNORECASE),
        re.compile(r'^Plate\s+\d+[.:]\s*', re.IGNORECASE),
    ]

    # Denklem pattern'ları
    EQUATION_PATTERNS = [
        re.compile(r'\$\$[\s\S]*?\$\$'),
        re.compile(r'\$[^\$\n]+?\$'),
        re.compile(r'\\begin\{equation\}[\s\S]*?\\end\{equation\}'),
        re.compile(r'\\begin\{align\}[\s\S]*?\\end\{align\}'),
        re.compile(r'\\\[[\s\S]*?\\\]'),
    ]

    def __init__(self, detect_headings: bool = True, detect_tables: bool = True,
                 detect_equations: bool = True, min_block_length: int = 10,
                 filter_headers_footers: bool = True, header_footer_margin: float = 0.08):
        self.detect_headings = detect_headings
        self.detect_tables = detect_tables
        self.detect_equations = detect_equations
        self.min_block_length = min_block_length
        self.filter_headers_footers = filter_headers_footers
        self.header_footer_margin = header_footer_margin  # Sayfanın üst/alt %8'i

    def parse(self, pdf_path: str, start_page: int = 0, end_page: int = None) -> PDFDocument:
        """
        PDF dosyasını parse et

        Args:
            pdf_path: PDF dosya yolu
            start_page: Başlangıç sayfası (0-indexed)
            end_page: Bitiş sayfası (dahil değil, None = son sayfa)

        Returns:
            PDFDocument
        """
        import fitz  # PyMuPDF

        doc = fitz.open(pdf_path)
        pdf_doc = PDFDocument(
            path=pdf_path,
            title=doc.metadata.get("title", Path(pdf_path).stem),
            metadata=dict(doc.metadata)
        )

        if end_page is None:
            end_page = len(doc)

        for page_num in range(start_page, min(end_page, len(doc))):
            page = doc[page_num]
            pdf_page = self._parse_page(page, page_num)
            pdf_doc.pages.append(pdf_page)

        doc.close()

        # Header/footer filtreleme
        if self.filter_headers_footers:
            self._filter_headers_footers(pdf_doc)

        return pdf_doc

    def _filter_headers_footers(self, pdf_doc: PDFDocument):
        """Tekrar eden header/footer'ları tespit edip kaldır"""
        from collections import Counter

        # Üst ve alt bölgelerdeki metinleri topla
        top_texts = Counter()
        bottom_texts = Counter()

        for page in pdf_doc.pages:
            if page.height == 0:
                continue

            for block in page.blocks:
                if not block.bbox:
                    continue

                y0, y1 = block.bbox[1], block.bbox[3]
                page_height = page.height

                # Normalize edilmiş metin
                norm_text = re.sub(r'\s+', ' ', block.text).strip()[:100]  # İlk 100 karakter

                # Üst %margin'de mi?
                if y0 < page_height * self.header_footer_margin:
                    top_texts[norm_text] += 1
                # Alt %margin'de mi?
                elif y1 > page_height * (1 - self.header_footer_margin):
                    bottom_texts[norm_text] += 1

        # En az 3 sayfada tekrar eden metinleri header/footer say
        min_occurrences = min(3, len(pdf_doc.pages) // 2)
        header_patterns = {t for t, c in top_texts.items() if c >= min_occurrences}
        footer_patterns = {t for t, c in bottom_texts.items() if c >= min_occurrences}

        removed_count = 0

        # Blokları filtrele
        for page in pdf_doc.pages:
            if page.height == 0:
                continue

            filtered_blocks = []
            for block in page.blocks:
                if not block.bbox:
                    filtered_blocks.append(block)
                    continue

                y0, y1 = block.bbox[1], block.bbox[3]
                page_height = page.height
                norm_text = re.sub(r'\s+', ' ', block.text).strip()[:100]

                # Header mı?
                if y0 < page_height * self.header_footer_margin and norm_text in header_patterns:
                    removed_count += 1
                    continue
                # Footer mı?
                if y1 > page_height * (1 - self.header_footer_margin) and norm_text in footer_patterns:
                    removed_count += 1
                    continue

                filtered_blocks.append(block)

            page.blocks = filtered_blocks

        if removed_count > 0:
            print(f"[PDF Parser] {removed_count} header/footer bloğu kaldırıldı")

    def _parse_page(self, page, page_num: int) -> PDFPage:
        """Tek bir sayfayı parse et"""
        import fitz

        pdf_page = PDFPage(
            page_num=page_num,
            width=page.rect.width,
            height=page.rect.height
        )

        # Metin bloklarını al
        blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)["blocks"]

        for block in blocks:
            if block["type"] != 0:  # Sadece metin blokları
                continue

            text_parts = []
            font_sizes = []
            font_names = []
            is_bold = False
            is_italic = False
            monospace_count = 0
            total_spans = 0

            for line in block["lines"]:
                line_text = ""
                for span in line["spans"]:
                    line_text += span["text"]
                    font_sizes.append(span["size"])
                    font_names.append(span["font"])
                    total_spans += 1

                    # Bold/italic kontrolü
                    font_lower = span["font"].lower()
                    if "bold" in font_lower:
                        is_bold = True
                    if "italic" in font_lower or "oblique" in font_lower:
                        is_italic = True
                    
                    # Monospace font kontrolu
                    if is_monospace_font(span["font"]):
                        monospace_count += 1

                text_parts.append(line_text)

            full_text = " ".join(text_parts).strip()

            if len(full_text) < self.min_block_length:
                continue

            # Monospace orani
            is_monospace = total_spans > 0 and (monospace_count / total_spans) > 0.5
            
            # Kod blogu tespiti
            is_code = is_monospace or looks_like_code(full_text)

            # Blok tipini belirle
            avg_font_size = sum(font_sizes) / len(font_sizes) if font_sizes else 12
            
            if is_code:
                block_type = "code"
            else:
                block_type = self._determine_block_type(full_text, avg_font_size, is_bold)

            text_block = TextBlock(
                text=full_text,
                block_type=block_type,
                page_num=page_num,
                bbox=tuple(block["bbox"]),
                font_size=avg_font_size,
                font_name=font_names[0] if font_names else "",
                is_bold=is_bold,
                is_italic=is_italic,
                is_code=is_code,
                is_monospace=is_monospace
            )

            pdf_page.blocks.append(text_block)

        return pdf_page

    def _determine_block_type(self, text: str, font_size: float, is_bold: bool) -> str:
        """Blok tipini belirle"""
        # Şekil/Tablo açıklaması
        for pattern in self.CAPTION_PATTERNS:
            if pattern.match(text):
                return "caption"

        # Başlık kontrolü
        if self.detect_headings:
            # Font boyutu büyükse veya bold ise başlık olabilir
            if is_bold and len(text) < 200:
                for pattern in self.HEADING_PATTERNS:
                    if pattern.match(text):
                        return "heading"
                # Kısa ve bold ise muhtemelen başlık
                if len(text) < 100:
                    return "heading"

            # HEADING HEURİSTİK (Codex önerisi)
            # <60 karakter + nokta yok + Title Case/ALL CAPS → heading
            stripped = text.strip()
            if len(stripped) < 60 and '.' not in stripped:
                words = stripped.split()
                if words:
                    # Title Case: Her kelimenin ilk harfi büyük
                    is_title_case = all(w[0].isupper() for w in words if w and w[0].isalpha())
                    # ALL CAPS: Tamamı büyük harf
                    is_all_caps = stripped.isupper() and len(stripped) > 3

                    if is_title_case or is_all_caps:
                        return "heading"

        # Denklem kontrolü
        if self.detect_equations:
            for pattern in self.EQUATION_PATTERNS:
                if pattern.search(text):
                    return "equation"

        # Liste kontrolü
        if text.strip().startswith(('•', '-', '*', '–', '›')) or \
           re.match(r'^\d+\.\s', text.strip()) or \
           re.match(r'^[a-z]\)\s', text.strip()):
            return "list"

        return "paragraph"

    def extract_structure(self, pdf_doc: PDFDocument) -> Dict:
        """
        Doküman yapısını çıkar

        Returns:
            {"headings": [...], "figures": [...], "tables": [...], "equations": [...]}
        """
        structure = {
            "headings": [],
            "figures": [],
            "tables": [],
            "equations": [],
            "paragraphs": []
        }

        for block in pdf_doc.iter_blocks():
            if block.block_type == "heading":
                structure["headings"].append({
                    "text": block.text,
                    "page": block.page_num,
                    "level": self._guess_heading_level(block)
                })
            elif block.block_type == "caption":
                if re.match(r'^(Figure|Fig)', block.text, re.IGNORECASE):
                    structure["figures"].append({
                        "caption": block.text,
                        "page": block.page_num
                    })
                elif re.match(r'^Table', block.text, re.IGNORECASE):
                    structure["tables"].append({
                        "caption": block.text,
                        "page": block.page_num
                    })
            elif block.block_type == "equation":
                structure["equations"].append({
                    "text": block.text,
                    "page": block.page_num
                })
            else:
                structure["paragraphs"].append({
                    "text": block.text,
                    "page": block.page_num
                })

        return structure

    def _guess_heading_level(self, block: TextBlock) -> int:
        """Başlık seviyesini tahmin et"""
        text = block.text.strip()

        # Numara sayısına göre
        match = re.match(r'^(\d+(?:\.\d+)*)', text)
        if match:
            return match.group(1).count('.') + 1

        # Font boyutuna göre
        if block.font_size >= 18:
            return 1
        elif block.font_size >= 14:
            return 2
        else:
            return 3

    def to_markdown(self, pdf_doc: PDFDocument) -> str:
        """
        PDF dokümanını Markdown'a dönüştür
        """
        lines = []

        if pdf_doc.title:
            lines.append(f"# {pdf_doc.title}")
            lines.append("")

        current_page = -1

        for block in pdf_doc.iter_blocks():
            # Sayfa değişimi
            if block.page_num != current_page:
                if current_page != -1:
                    lines.append("")
                    lines.append(f"<!-- Page {block.page_num + 1} -->")
                current_page = block.page_num

            if block.block_type == "heading":
                level = self._guess_heading_level(block)
                prefix = "#" * min(level + 1, 6)  # Max 6 seviye
                lines.append(f"\n{prefix} {block.text}\n")

            elif block.block_type == "caption":
                lines.append(f"\n**{block.text}**\n")

            elif block.block_type == "equation":
                lines.append(f"\n$$\n{block.text}\n$$\n")

            elif block.block_type == "list":
                lines.append(f"- {block.text}")

            else:  # paragraph
                lines.append(f"\n{block.text}\n")

        return "\n".join(lines)


# =============================================================================
# LAYOUT-AWARE PDF PARSER
# =============================================================================

class LayoutAwarePDFParser:
    """
    DocLayout-YOLO tabanli akilli PDF parser.

    Ozellikler:
    - AI tabanli sayfa elementi tespiti
    - Formul, tablo, resim otomatik tespiti
    - Cevirilecek/korunacak element ayirimi
    - Koordinat bazli metin cikarma
    """

    def __init__(
        self,
        layout_detector=None,
        placeholder_manager=None,
        dpi: int = 150,
        min_confidence: float = 0.25
    ):
        """
        Args:
            layout_detector: LayoutDetector instance (None = lazy load)
            placeholder_manager: PlaceholderManager instance (None = lazy load)
            dpi: PDF render DPI
            min_confidence: Minimum tespit guveni
        """
        self._layout_detector = layout_detector
        self._placeholder_manager = placeholder_manager
        self.dpi = dpi
        self.min_confidence = min_confidence

    @property
    def layout_detector(self):
        """Lazy load layout detector"""
        if self._layout_detector is None:
            from core.layout_detector import LayoutDetector
            self._layout_detector = LayoutDetector(
                confidence_threshold=self.min_confidence
            )
        return self._layout_detector

    @property
    def placeholder_manager(self):
        """Lazy load placeholder manager"""
        if self._placeholder_manager is None:
            from core.placeholder import PlaceholderManager
            self._placeholder_manager = PlaceholderManager()
        return self._placeholder_manager

    def parse(
        self,
        pdf_path: str,
        start_page: int = 0,
        end_page: int = None,
        progress_callback: callable = None
    ) -> 'LayoutAwarePDFDocument':
        """
        PDF'yi layout-aware olarak parse et.

        Args:
            pdf_path: PDF dosya yolu
            start_page: Baslangic sayfasi (0-indexed)
            end_page: Bitis sayfasi (None = son sayfa)
            progress_callback: Ilerleme callback'i (page, total)

        Returns:
            LayoutAwarePDFDocument
        """
        import fitz

        # PDF yolunu sakla (tablo çıkarma için gerekli)
        self._current_pdf_path = pdf_path

        doc = None
        try:
            doc = fitz.open(pdf_path)
            total_pages = len(doc)

            if end_page is None:
                end_page = total_pages

            pdf_doc = LayoutAwarePDFDocument(
                path=pdf_path,
                title=doc.metadata.get("title", Path(pdf_path).stem),
                metadata=dict(doc.metadata)
            )

            for page_num in range(start_page, min(end_page, total_pages)):
                # Layout tespiti
                elements = self.layout_detector.detect_page(pdf_path, page_num, self.dpi)

                # Sayfa bilgilerini al
                page = doc[page_num]
                page_width = page.rect.width
                page_height = page.rect.height

                # Elemanlara metin ekle
                elements_with_text = self._extract_text_for_elements(
                    page, elements
                )

                # LayoutAwarePage olustur
                layout_page = LayoutAwarePage(
                    page_num=page_num,
                    width=page_width,
                    height=page_height,
                    elements=elements_with_text
                )

                pdf_doc.pages.append(layout_page)

                if progress_callback:
                    progress_callback(page_num + 1, end_page - start_page)

            return pdf_doc
        finally:
            if doc:
                doc.close()

    def _extract_text_for_elements(
        self,
        page,
        elements: list
    ) -> list:
        """
        Her element icin ilgili metni cikar.
        Ayrica kod blogu, font bilgisi ve TOC tespiti yapar.

        Args:
            page: PyMuPDF page objesi
            elements: LayoutDetector'dan gelen element listesi

        Returns:
            Metin ve metadata eklenmis element listesi
        """
        import fitz

        # Sayfa metin bloklarini al
        text_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
        text_blocks = text_dict.get("blocks", [])

        for element in elements:
            bbox = element.bbox
            element_rect = fitz.Rect(bbox.x1, bbox.y1, bbox.x2, bbox.y2)

            # Bu element icindeki metinleri ve font bilgilerini topla
            element_texts = []
            font_names = []
            font_sizes = []
            font_colors = []  # VISION.md: Renk koruma
            monospace_count = 0
            bold_count = 0
            italic_count = 0  # VISION.md: Italic koruma
            underline_count = 0  # VISION.md: Underline koruma
            total_spans = 0

            for block in text_blocks:
                if block.get("type") != 0:  # Sadece metin bloklari
                    continue

                block_rect = fitz.Rect(block["bbox"])

                # Kesisim kontrolu
                if element_rect.intersects(block_rect):
                    # Kesisim orani hesapla
                    intersection = element_rect & block_rect
                    if intersection.is_empty:
                        continue

                    # Blok metnini ve font bilgilerini topla
                    for line in block.get("lines", []):
                        line_text = ""
                        for span in line.get("spans", []):
                            span_text = span.get("text", "")
                            line_text += span_text

                            if span_text.strip():
                                font_name = span.get("font", "")
                                font_names.append(font_name)
                                font_sizes.append(span.get("size", 10))
                                total_spans += 1

                                # VISION.md: Renk cikarma (integer -> RGB)
                                color_int = span.get("color", 0)
                                r = ((color_int >> 16) & 0xFF) / 255.0
                                g = ((color_int >> 8) & 0xFF) / 255.0
                                b = (color_int & 0xFF) / 255.0
                                font_colors.append((r, g, b))

                                # VISION.md: Flag'lerden stil cikarma
                                flags = span.get("flags", 0)
                                # Bit 1: superscript, Bit 2: italic, Bit 3: serifed
                                # Bit 4: monospaced, Bit 5: bold
                                if flags & 2 or "italic" in font_name.lower() or "oblique" in font_name.lower():
                                    italic_count += 1
                                if flags & 4:  # Underline flag
                                    underline_count += 1

                                # Monospace kontrolu
                                if is_monospace_font(font_name) or (flags & 8):
                                    monospace_count += 1

                                # Bold kontrolu
                                if "bold" in font_name.lower() or (flags & 16):
                                    bold_count += 1

                        if line_text.strip():
                            element_texts.append(line_text)

            # Metni element'e ekle
            full_text = " ".join(element_texts).strip()
            element.text = full_text

            # Font bilgilerini ekle
            if font_names:
                element.font_name = font_names[0]
            if font_sizes:
                element.font_size = sum(font_sizes) / len(font_sizes)

            # VISION.md: Renk bilgisi (en yaygın renk veya ilk renk)
            if font_colors:
                # Siyah olmayan ilk rengi tercih et
                non_black = [c for c in font_colors if not (c[0] < 0.1 and c[1] < 0.1 and c[2] < 0.1)]
                element.color = non_black[0] if non_black else font_colors[0]
            else:
                element.color = (0, 0, 0)

            # Monospace/Kod tespiti
            element.is_monospace = total_spans > 0 and (monospace_count / total_spans) > 0.5
            element.is_code = element.is_monospace or looks_like_code(full_text)

            # Bold tespiti
            element.is_bold = total_spans > 0 and (bold_count / total_spans) > 0.5

            # VISION.md: Italic tespiti
            element.is_italic = total_spans > 0 and (italic_count / total_spans) > 0.5

            # VISION.md: Underline tespiti
            element.is_underline = total_spans > 0 and (underline_count / total_spans) > 0.5

            # VISION.md: Align tespiti - element bbox ve sayfa genisligi kullanarak
            element.align = self._detect_alignment(element, page)

            # TOC/Index tespiti
            element.is_toc_or_index = self._is_toc_or_index(full_text, page.number)

        return elements
    
    def _is_toc_or_index(self, text: str, page_num: int) -> bool:
        """
        Metin icindekiler veya indeks sayfasinda mi kontrol et.
        
        Args:
            text: Metin
            page_num: Sayfa numarasi
            
        Returns:
            TOC/Index mi?
        """
        if not text:
            return False
        
        text_lower = text.lower()
        
        # TOC/Index basliklari
        toc_headers = [
            "contents", "table of contents", "içindekiler",
            "index", "dizin", "bibliography", "kaynakça", "references",
            "appendix", "ek", "list of figures", "list of tables"
        ]
        
        # Baslik kontrolu
        for header in toc_headers:
            if text_lower.strip() == header or text_lower.startswith(header + "\n"):
                return True
        
        # Sayfa numarasi pattern'i - "Chapter 1 ............. 5" gibi
        # Cok sayida nokta veya tire varsa muhtemelen TOC
        dot_count = text.count('.')
        dash_count = text.count('-')
        ellipsis_count = text.count('...')
        
        if ellipsis_count > 3 or (dot_count > 10 and len(text) < 500):
            return True
        
        # Erken sayfalar (ilk 10 sayfa) ve TOC pattern'i
        if page_num < 10:
            # "1.1 Something .... 15" pattern'i
            if re.search(r'\d+\s*$', text) and (ellipsis_count > 0 or dot_count > 5):
                return True

        return False

    def _detect_alignment(self, element, page) -> int:
        """
        Element'in hizalamasini tespit et.

        VISION.md: Align koruma - 0=left, 1=center, 2=right, 3=justify

        Args:
            element: LayoutElement
            page: PyMuPDF page

        Returns:
            Hizalama: 0=left, 1=center, 2=right, 3=justify
        """
        import fitz

        # Sayfa boyutlari
        page_rect = page.rect
        page_width = page_rect.width

        # Margin tahmini (tipik PDF margin'leri)
        left_margin = page_width * 0.1   # %10
        right_margin = page_width * 0.9  # %90

        # Element pozisyonlari
        bbox = element.bbox
        elem_left = bbox.x1
        elem_right = bbox.x2
        elem_width = elem_right - elem_left
        elem_center = (elem_left + elem_right) / 2
        page_center = page_width / 2

        # Tolerans (pt)
        tolerance = 10

        # Kisa metin kontrolu (tek kelime/sayi)
        text = getattr(element, 'text', '')
        is_short = len(text.strip()) < 20

        # CENTER tespiti:
        # Element merkezi sayfa merkezine yakinsa
        if abs(elem_center - page_center) < tolerance:
            return 1  # center

        # RIGHT tespiti:
        # Element sag kenari sag margin'e yakinsa VE sol kenari sola uzaksa
        if abs(elem_right - right_margin * page_width / 100 * 100) < tolerance * 2:
            if elem_left > page_width * 0.5:  # Sag yarisinda
                return 2  # right

        # Element sag kenari sayfa sagina yakinsa
        if elem_right > page_width * 0.85 and elem_left > page_width * 0.4:
            return 2  # right

        # JUSTIFY tespiti:
        # Element hem sola hem saga yakinsa ve genis bir bloksa
        left_close = elem_left < left_margin + tolerance
        right_close = elem_right > right_margin - tolerance

        if left_close and right_close and elem_width > page_width * 0.6:
            return 3  # justify (paragraf blogu)

        # LEFT (varsayilan):
        # Element sol margin'e yakinsa
        return 0  # left

    def parse_page(
        self,
        pdf_path: str,
        page_num: int
    ) -> 'LayoutAwarePage':
        """Tek bir sayfayi parse et"""
        import fitz

        doc = fitz.open(pdf_path)
        page = doc[page_num]

        elements = self.layout_detector.detect_page(pdf_path, page_num, self.dpi)
        elements_with_text = self._extract_text_for_elements(page, elements)

        layout_page = LayoutAwarePage(
            page_num=page_num,
            width=page.rect.width,
            height=page.rect.height,
            elements=elements_with_text
        )

        doc.close()

        return layout_page

    def get_translation_segments(
        self,
        pdf_doc: 'LayoutAwarePDFDocument'
    ) -> List[Dict]:
        """
        Ceviri icin segmentleri cikar.

        Returns:
            [{"page": int, "element_idx": int, "text": str, "type": str}, ...]
        """
        segments = []

        for page in pdf_doc.pages:
            for idx, element in enumerate(page.elements):
                if element.should_translate and element.text:
                    segments.append({
                        "page": page.page_num,
                        "element_idx": idx,
                        "text": element.text,
                        "type": element.element_type.value,
                        "bbox": element.bbox.to_tuple()
                    })

        return segments


@dataclass
class LayoutAwarePage:
    """Layout-aware PDF sayfasi"""
    page_num: int
    width: float
    height: float
    elements: list = field(default_factory=list)

    def get_translatable_elements(self) -> list:
        """Cevirilecek elementleri dondur"""
        return [e for e in self.elements if e.should_translate]

    def get_preserved_elements(self) -> list:
        """Korunacak elementleri dondur"""
        return [e for e in self.elements if e.should_preserve]

    def get_stats(self) -> Dict:
        """Sayfa istatistikleri"""
        stats = {
            "total": len(self.elements),
            "translatable": len(self.get_translatable_elements()),
            "preserved": len(self.get_preserved_elements()),
            "by_type": {}
        }
        for e in self.elements:
            t = e.element_type.value
            stats["by_type"][t] = stats["by_type"].get(t, 0) + 1
        return stats


@dataclass
class LayoutAwarePDFDocument:
    """Layout-aware PDF dokumani"""
    path: str
    title: str = ""
    pages: List[LayoutAwarePage] = field(default_factory=list)
    metadata: Dict = field(default_factory=dict)

    @property
    def total_pages(self) -> int:
        return len(self.pages)

    def get_all_translatable_text(self) -> str:
        """Tum cevirilecek metinleri birlestir"""
        texts = []
        for page in self.pages:
            for e in page.get_translatable_elements():
                if e.text:
                    texts.append(e.text)
        return "\n\n".join(texts)

    def get_stats(self) -> Dict:
        """Dokuman istatistikleri"""
        total_elements = 0
        total_translatable = 0
        total_preserved = 0
        by_type = {}

        for page in self.pages:
            stats = page.get_stats()
            total_elements += stats["total"]
            total_translatable += stats["translatable"]
            total_preserved += stats["preserved"]
            for t, c in stats["by_type"].items():
                by_type[t] = by_type.get(t, 0) + c

        return {
            "total_pages": self.total_pages,
            "total_elements": total_elements,
            "translatable": total_translatable,
            "preserved": total_preserved,
            "by_type": by_type
        }

    def iter_translatable(self):
        """Cevirilecek elementleri iterate et"""
        for page in self.pages:
            for e in page.get_translatable_elements():
                yield page.page_num, e


# Test
if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        parser = PDFParser()
        doc = parser.parse(sys.argv[1])

        print(f"Başlık: {doc.title}")
        print(f"Sayfa sayısı: {doc.total_pages}")

        structure = parser.extract_structure(doc)
        print(f"\nBaşlıklar: {len(structure['headings'])}")
        print(f"Şekiller: {len(structure['figures'])}")
        print(f"Tablolar: {len(structure['tables'])}")
        print(f"Denklemler: {len(structure['equations'])}")
        print(f"Paragraflar: {len(structure['paragraphs'])}")

        print("\n--- Markdown Çıktısı (ilk 2000 karakter) ---")
        md = parser.to_markdown(doc)
        print(md[:2000])
    else:
        print("Kullanım: python pdf_parser.py <pdf_dosyası>")
