"""
Markdown Exporter
Çevrilmiş içeriği Markdown formatında dışa aktarma
"""

import re
from typing import List, Dict, Optional
from pathlib import Path
from datetime import datetime


class MarkdownExporter:
    """
    Markdown formatında dışa aktarıcı

    - Başlık hiyerarşisi
    - Denklem blokları
    - Tablo formatı
    - Şekil referansları
    """

    def __init__(self, add_metadata: bool = True, add_toc: bool = True):
        self.add_metadata = add_metadata
        self.add_toc = add_toc

    def export(self, segments: List[Dict], output_path: str,
               title: str = "", author: str = "", source_file: str = "") -> str:
        """
        Segmentleri Markdown dosyasına aktar

        Args:
            segments: [{"text": "...", "type": "heading/paragraph", ...}, ...]
            output_path: Çıktı dosya yolu
            title: Doküman başlığı
            author: Yazar
            source_file: Kaynak dosya adı

        Returns:
            Oluşturulan dosya yolu
        """
        lines = []

        # Metadata (YAML front matter)
        if self.add_metadata:
            lines.extend(self._create_frontmatter(title, author, source_file))

        # Başlık
        if title:
            lines.append(f"# {title}")
            lines.append("")

        # İçindekiler
        if self.add_toc:
            toc = self._generate_toc(segments)
            if toc:
                lines.append("## İçindekiler")
                lines.append("")
                lines.extend(toc)
                lines.append("")
                lines.append("---")
                lines.append("")

        # İçerik
        current_chapter = -1
        for segment in segments:
            # Bölüm değişimi
            chapter = segment.get("chapter", 0)
            if chapter != current_chapter and current_chapter != -1:
                lines.append("")
                lines.append("---")
                lines.append("")
            current_chapter = chapter

            # Segment tipine göre formatla
            text = segment["text"]
            seg_type = segment.get("type", "paragraph")

            if seg_type == "heading":
                level = segment.get("level", 2)
                prefix = "#" * min(level, 6)
                lines.append(f"\n{prefix} {text}\n")

            elif seg_type == "equation":
                lines.append("")
                lines.append("$$")
                lines.append(text)
                lines.append("$$")
                lines.append("")

            elif seg_type == "caption":
                lines.append(f"\n**{text}**\n")

            elif seg_type == "list":
                lines.append(f"- {text}")

            elif seg_type == "code":
                lines.append("```")
                lines.append(text)
                lines.append("```")

            else:  # paragraph
                lines.append(f"\n{text}\n")

        # Dosyaya yaz
        content = "\n".join(lines)
        content = self._clean_output(content)
        # APP-B24: NUL byte'ları kaldır — bazı PDF'lerden sızan \x00 karakterleri
        # markdown parser'larını (pandoc dahil) bozar.
        content = content.replace('\x00', '')

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(content)

        return output_path

    def _create_frontmatter(self, title: str, author: str, source_file: str) -> List[str]:
        """YAML front matter oluştur"""
        lines = ["---"]

        if title:
            lines.append(f'title: "{title}"')
        if author:
            lines.append(f'author: "{author}"')
        if source_file:
            lines.append(f'source: "{source_file}"')

        lines.append(f'translated_date: "{datetime.now().strftime("%Y-%m-%d")}"')
        lines.append('lang: tr')
        lines.append("---")
        lines.append("")

        return lines

    def _generate_toc(self, segments: List[Dict]) -> List[str]:
        """İçindekiler tablosu oluştur"""
        toc = []

        for segment in segments:
            if segment.get("type") == "heading":
                level = segment.get("level", 2)
                text = segment["text"]

                # Anchor oluştur
                anchor = self._create_anchor(text)
                indent = "  " * (level - 1)

                toc.append(f"{indent}- [{text}](#{anchor})")

        return toc

    def _create_anchor(self, text: str) -> str:
        """Başlık için anchor oluştur"""
        # Küçük harfe çevir
        anchor = text.lower()

        # Türkçe karakterleri dönüştür
        tr_map = {
            'ı': 'i', 'ğ': 'g', 'ü': 'u', 'ş': 's', 'ö': 'o', 'ç': 'c',
            'İ': 'i', 'Ğ': 'g', 'Ü': 'u', 'Ş': 's', 'Ö': 'o', 'Ç': 'c'
        }
        for tr_char, en_char in tr_map.items():
            anchor = anchor.replace(tr_char, en_char)

        # Özel karakterleri kaldır
        anchor = re.sub(r'[^\w\s-]', '', anchor)

        # Boşlukları tire ile değiştir
        anchor = re.sub(r'\s+', '-', anchor)

        return anchor

    def _clean_output(self, content: str) -> str:
        """Çıktıyı temizle"""
        # Çoklu boş satırları azalt
        content = re.sub(r'\n{4,}', '\n\n\n', content)

        # Satır sonu boşlukları temizle
        lines = [line.rstrip() for line in content.split('\n')]

        return '\n'.join(lines)

    def export_with_parallel(self, source_segments: List[Dict],
                             target_segments: List[Dict],
                             output_path: str) -> str:
        """
        Kaynak ve hedef metni yan yana dışa aktar (review için)
        """
        lines = []
        lines.append("# Çeviri İnceleme Dokümanı")
        lines.append("")
        lines.append("| Kaynak (EN) | Hedef (TR) |")
        lines.append("|-------------|------------|")

        for source, target in zip(source_segments, target_segments):
            src_text = source["text"].replace("\n", " ").replace("|", "\\|")
            tgt_text = target["text"].replace("\n", " ").replace("|", "\\|")

            # Uzun metinleri kısalt
            if len(src_text) > 100:
                src_text = src_text[:97] + "..."
            if len(tgt_text) > 100:
                tgt_text = tgt_text[:97] + "..."

            lines.append(f"| {src_text} | {tgt_text} |")

        content = "\n".join(lines)

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(content)

        return output_path
