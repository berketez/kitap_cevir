"""
DOCX Exporter
Çevrilmiş içeriği Word formatında dışa aktarma
"""

import re
from typing import List, Dict, Optional
from pathlib import Path


class DOCXExporter:
    """
    Microsoft Word (DOCX) formatında dışa aktarıcı

    - Başlık stilleri
    - Paragraf formatı
    - Tablo desteği
    - Sayfa numaraları
    """

    def __init__(self, template_path: str = None):
        """
        Args:
            template_path: Şablon DOCX dosyası (opsiyonel)
        """
        self.template_path = template_path

    def export(self, segments: List[Dict], output_path: str,
               title: str = "", author: str = "") -> str:
        """
        Segmentleri DOCX dosyasına aktar

        Args:
            segments: [{"text": "...", "type": "heading/paragraph", ...}, ...]
            output_path: Çıktı dosya yolu
            title: Doküman başlığı
            author: Yazar

        Returns:
            Oluşturulan dosya yolu
        """
        from docx import Document
        from docx.shared import Inches, Pt
        from docx.enum.text import WD_ALIGN_PARAGRAPH

        # Yeni doküman veya şablondan
        if self.template_path:
            doc = Document(self.template_path)
        else:
            doc = Document()

        # Doküman özellikleri
        if title:
            doc.core_properties.title = title
        if author:
            doc.core_properties.author = author

        # Başlık sayfası
        if title:
            title_para = doc.add_paragraph()
            title_run = title_para.add_run(title)
            title_run.bold = True
            title_run.font.size = Pt(24)
            title_para.alignment = WD_ALIGN_PARAGRAPH.CENTER

            if author:
                author_para = doc.add_paragraph()
                author_para.add_run(author)
                author_para.alignment = WD_ALIGN_PARAGRAPH.CENTER

            doc.add_page_break()

        # İçerik
        for segment in segments:
            # APP-B22: Boş/whitespace segmentleri atla — DOCX'e boş paragraf eklemesin
            text = (segment.get("text") or "").strip()
            if not text:
                continue
            seg_type = segment.get("type", "paragraph")
            level = segment.get("level", 1)

            if seg_type == "heading":
                # Başlık seviyesine göre stil
                if level == 1:
                    doc.add_heading(text, level=1)
                elif level == 2:
                    doc.add_heading(text, level=2)
                else:
                    doc.add_heading(text, level=min(level, 4))

            elif seg_type == "equation":
                # Denklem (italik ve ortalanmış)
                para = doc.add_paragraph()
                run = para.add_run(text)
                run.italic = True
                para.alignment = WD_ALIGN_PARAGRAPH.CENTER

            elif seg_type == "caption":
                # Şekil/Tablo açıklaması (bold ve ortalanmış)
                para = doc.add_paragraph()
                run = para.add_run(text)
                run.bold = True
                para.alignment = WD_ALIGN_PARAGRAPH.CENTER

            elif seg_type == "list":
                # Liste öğesi
                doc.add_paragraph(text, style='List Bullet')

            elif seg_type == "code":
                # Kod bloğu (monospace)
                para = doc.add_paragraph()
                run = para.add_run(text)
                run.font.name = 'Courier New'
                run.font.size = Pt(10)

            else:  # paragraph
                doc.add_paragraph(text)

        # Kaydet
        doc.save(output_path)
        return output_path

    def export_bilingual(self, source_segments: List[Dict],
                         target_segments: List[Dict],
                         output_path: str,
                         title: str = "") -> str:
        """
        İki dilli doküman oluştur (review için)

        Tablo formatında: Kaynak | Hedef
        """
        from docx import Document
        from docx.shared import Inches, Pt
        from docx.enum.text import WD_ALIGN_PARAGRAPH

        doc = Document()

        # Başlık
        if title:
            heading = doc.add_heading(title, level=0)
            heading.alignment = WD_ALIGN_PARAGRAPH.CENTER

        doc.add_paragraph("Çeviri İnceleme Dokümanı")
        doc.add_paragraph()

        # Tablo oluştur
        table = doc.add_table(rows=1, cols=2)
        table.style = 'Table Grid'

        # Başlık satırı
        header_cells = table.rows[0].cells
        header_cells[0].text = "Kaynak (EN)"
        header_cells[1].text = "Hedef (TR)"

        # Bold başlıklar
        for cell in header_cells:
            for para in cell.paragraphs:
                for run in para.runs:
                    run.bold = True

        # İçerik satırları
        for source, target in zip(source_segments, target_segments):
            row_cells = table.add_row().cells
            row_cells[0].text = source["text"]
            row_cells[1].text = target["text"]

        doc.save(output_path)
        return output_path

    def export_with_comments(self, segments: List[Dict],
                             qa_issues: List[Dict],
                             output_path: str) -> str:
        """
        QA sorunlarını yorum olarak ekleyerek dışa aktar
        """
        from docx import Document
        from docx.shared import Pt, RGBColor
        from docx.enum.text import WD_COLOR_INDEX

        doc = Document()

        # Segment indeksine göre sorunları grupla
        issues_by_segment = {}
        for issue in qa_issues:
            seg_idx = issue.get("position", -1)
            if seg_idx not in issues_by_segment:
                issues_by_segment[seg_idx] = []
            issues_by_segment[seg_idx].append(issue)

        for i, segment in enumerate(segments):
            text = segment["text"]
            seg_type = segment.get("type", "paragraph")

            if seg_type == "heading":
                para = doc.add_heading(text, level=segment.get("level", 2))
            else:
                para = doc.add_paragraph(text)

            # Bu segmentte sorun varsa vurgula
            if i in issues_by_segment:
                # Metni sarı ile vurgula
                for run in para.runs:
                    run.font.highlight_color = WD_COLOR_INDEX.YELLOW

                # Sorunları altına ekle
                for issue in issues_by_segment[i]:
                    issue_para = doc.add_paragraph()
                    icon = "[HATA]" if issue["severity"] == "error" else "[UYARI]"
                    issue_run = issue_para.add_run(f"  {icon} {issue['category']}: {issue['message']}")
                    issue_run.font.size = Pt(9)
                    issue_run.font.color.rgb = RGBColor(255, 0, 0) if issue["severity"] == "error" else RGBColor(255, 165, 0)

        doc.save(output_path)
        return output_path
