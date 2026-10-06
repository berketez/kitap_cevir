"""
EPUB Exporter
Çevrilmiş içeriği EPUB formatında dışa aktarma
"""

import logging
import mimetypes
import re
import uuid
from typing import List, Dict, Optional
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)


class EPUBExporter:
    """
    EPUB formatında dışa aktarıcı

    - Bölüm yapısı
    - İçindekiler
    - Metadata
    - Stil desteği
    """

    # Varsayılan CSS
    DEFAULT_CSS = """
    body {
        font-family: Georgia, serif;
        line-height: 1.6;
        margin: 1em;
    }
    h1, h2, h3 {
        font-family: Helvetica, Arial, sans-serif;
        margin-top: 1.5em;
    }
    h1 { font-size: 1.8em; }
    h2 { font-size: 1.4em; }
    h3 { font-size: 1.2em; }
    p { margin: 0.5em 0; text-align: justify; }
    .equation {
        display: block;
        text-align: center;
        margin: 1em 0;
        font-style: italic;
    }
    .caption {
        text-align: center;
        font-weight: bold;
        margin: 1em 0;
    }
    .code {
        font-family: monospace;
        background: #f4f4f4;
        padding: 0.5em;
        display: block;
    }
    """

    def __init__(self, css: str = None):
        """
        Args:
            css: Özel CSS (opsiyonel)
        """
        self.css = css or self.DEFAULT_CSS

    def export(self, segments: List[Dict], output_path: str,
               title: str = "Çevrilmiş Kitap",
               author: str = "",
               language: str = "tr",
               cover_image: str = None,
               original_metadata: Dict = None) -> str:
        """
        Segmentleri EPUB dosyasına aktar

        Args:
            segments: [{"text": "...", "type": "heading/paragraph", "chapter": 0, ...}, ...]
            output_path: Çıktı dosya yolu
            title: Kitap başlığı
            author: Yazar
            language: Dil kodu
            cover_image: Kapak görseli yolu (opsiyonel)
            original_metadata: Orijinal kitap metadata'sı (ISBN, publisher vb.)

        Returns:
            Oluşturulan dosya yolu
        """
        from ebooklib import epub

        book = epub.EpubBook()

        # Metadata - orijinal varsa kullan, yoksa uuid oluştur
        original_metadata = original_metadata or {}

        # ISBN veya identifier - orijinal varsa koru
        if original_metadata.get('identifier'):
            book.set_identifier(original_metadata['identifier'])
        elif original_metadata.get('isbn'):
            book.set_identifier(original_metadata['isbn'])
        else:
            book.set_identifier(str(uuid.uuid4()))

        book.set_title(title)
        book.set_language(language)

        if author:
            book.add_author(author)

        # Orijinal metadata'yı ekle
        if original_metadata.get('publisher'):
            book.add_metadata('DC', 'publisher', original_metadata['publisher'])
        if original_metadata.get('date'):
            book.add_metadata('DC', 'date', original_metadata['date'])
        else:
            book.add_metadata('DC', 'date', datetime.now().strftime('%Y-%m-%d'))
        if original_metadata.get('description'):
            book.add_metadata('DC', 'description', original_metadata['description'])
        if original_metadata.get('subject'):
            book.add_metadata('DC', 'subject', original_metadata['subject'])
        if original_metadata.get('rights'):
            book.add_metadata('DC', 'rights', original_metadata['rights'])

        # CSS
        css_item = epub.EpubItem(
            uid="style",
            file_name="style/main.css",
            media_type="text/css",
            content=self.css
        )
        book.add_item(css_item)

        # Kapak
        # APP-B10: path traversal + boyut + mime doğrula, güvensiz dosyaları reddet
        if cover_image:
            try:
                cover_path = Path(cover_image).resolve(strict=False)
                if cover_path.exists() and cover_path.is_file():
                    size = cover_path.stat().st_size
                    if size > 10 * 1024 * 1024:
                        logger.warning(
                            "EPUB cover reddedildi (10MB ustu): %s (%d byte)",
                            cover_path, size,
                        )
                    else:
                        mime_type, _ = mimetypes.guess_type(str(cover_path))
                        if mime_type and mime_type.startswith("image/"):
                            with open(cover_path, 'rb') as f:
                                cover_bytes = f.read()
                            suffix = cover_path.suffix or ".jpg"
                            book.set_cover(f"cover{suffix}", cover_bytes)
                        else:
                            logger.warning(
                                "EPUB cover reddedildi (gecersiz MIME): %s (%s)",
                                cover_path, mime_type,
                            )
            except Exception as cover_err:
                logger.warning("EPUB cover yuklenemedi: %s", cover_err)

        # Segmentleri bölümlere ayır
        # APP-B23: print → logger
        logger.info("EPUB export: gelen segment sayisi=%d", len(segments) if segments else 0)

        chapters = self._segments_to_chapters(segments)

        if not chapters:
            # Fallback: tüm segmentleri tek bölüme koy
            logger.warning("EPUB: Bolum olusturulamadi, fallback kullaniliyor")
            if segments:
                chapters = [{"title": title or "İçerik", "segments": segments}]
            else:
                raise ValueError("Document is empty - no segments to export")

        # Bölümleri oluştur
        epub_chapters = []
        for i, chapter_data in enumerate(chapters):
            try:
                chapter = self._create_chapter(chapter_data, i, css_item)
                book.add_item(chapter)
                epub_chapters.append(chapter)
            except Exception as e:
                logger.warning("EPUB: Bolum %d olusturulurken hata: %s", i, e)
                continue

        logger.info("EPUB: %d epub bolumu olusturuldu", len(epub_chapters))

        if not epub_chapters:
            raise ValueError(f"Document is empty - no content to export (segments: {len(segments) if segments else 0}, chapters: {len(chapters)})")

        # İçindekiler
        logger.info("EPUB: TOC olusturuluyor")
        book.toc = tuple(epub_chapters)

        # Spine (okuma sırası)
        logger.info("EPUB: Spine olusturuluyor")
        book.spine = ['nav'] + epub_chapters

        # Navigasyon
        logger.info("EPUB: Navigasyon ekleniyor")
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())

        # Kaydet
        logger.info("EPUB: Dosya yaziliyor: %s", output_path)
        try:
            epub.write_epub(output_path, book)
            logger.info("EPUB: Dosya basariyla yazildi")
        except Exception as e:
            logger.error("EPUB: Dosya yazilirken HATA: %s", e, exc_info=True)
            raise

        return output_path

    def _segments_to_chapters(self, segments: List[Dict]) -> List[Dict]:
        """Segmentleri bölümlere grupla"""
        # Boş veya None kontrolü
        if not segments:
            logger.warning("EPUB: Segment listesi bos")
            return []

        logger.info("EPUB: %d segment isleniyor", len(segments))

        chapters = []
        current_chapter = {
            "title": "Giriş",
            "segments": []
        }

        for segment in segments:
            # Segment formatı kontrolü
            if not isinstance(segment, dict):
                logger.warning("EPUB: Gecersiz segment formati: %s", type(segment))
                continue

            if "text" not in segment:
                logger.warning("EPUB: Segment'te 'text' key yok: %s", list(segment.keys()))
                continue

            seg_type = segment.get("type", "paragraph")
            level = segment.get("level", 3)  # Varsayılan 3 (bölüm başlatmaz)

            # Yeni bölüm başlığı (sadece level 1-2 heading'ler)
            if seg_type == "heading" and level <= 2:
                # Mevcut bölümü kaydet (boş olsa bile başlık varsa ekle)
                if current_chapter["segments"]:
                    chapters.append(current_chapter)

                # Yeni bölüm başlat
                current_chapter = {
                    "title": segment["text"],
                    "segments": []
                }
            else:
                current_chapter["segments"].append(segment)

        # Son bölümü ekle
        if current_chapter["segments"]:
            chapters.append(current_chapter)

        # Hiç bölüm oluşmadıysa, tüm segmentleri tek bölüme koy
        if not chapters and segments:
            chapters = [{
                "title": "İçerik",
                "segments": segments
            }]

        logger.info("EPUB: %d bolum olusturuldu", len(chapters))
        return chapters

    def _create_chapter(self, chapter_data: Dict, index: int,
                        css_item) -> 'epub.EpubHtml':
        """EPUB bölümü oluştur"""
        from ebooklib import epub

        title = chapter_data["title"]
        file_name = f"chapter_{index:03d}.xhtml"

        # HTML içeriği oluştur
        segments = chapter_data.get("segments", [])
        html_content = self._segments_to_html(segments, title)

        # Debug: içerik kontrolü
        if not html_content or len(html_content) < 100:
            logger.warning(
                "EPUB: Bolum %d (%s...) icerigi cok kisa: %d karakter",
                index, title[:30], len(html_content) if html_content else 0,
            )

        chapter = epub.EpubHtml(
            title=title,
            file_name=file_name,
            lang='tr'
        )
        # ebooklib bytes bekliyor
        chapter.set_content(html_content.encode('utf-8'))
        chapter.add_item(css_item)

        return chapter

    def _segments_to_html(self, segments: List[Dict], title: str) -> str:
        """Segmentleri HTML'e dönüştür"""
        lines = [
            '<?xml version="1.0" encoding="utf-8"?>',
            '<!DOCTYPE html>',
            '<html xmlns="http://www.w3.org/1999/xhtml">',
            '<head>',
            f'<title>{self._escape_html(title)}</title>',
            '<link rel="stylesheet" type="text/css" href="style/main.css"/>',
            '</head>',
            '<body>',
            f'<h1>{self._escape_html(title)}</h1>'
        ]

        prev_text = None  # Duplicate kontrolü için
        prev_chapter = None  # Chapter bazli duplicate kontrolu
        duplicate_count = 0

        for segment in segments:
            text = segment["text"].strip()
            current_chapter = segment.get("chapter", 0)

            # Daha akilli duplicate kontrolu:
            # Sadece AYNI BOLUMDE ve ARDISIK ayni metin varsa atla
            # Farkli bolumlerde ayni metin olabilir (ornegin "Giris" basligi)
            if text == prev_text and current_chapter == prev_chapter:
                duplicate_count += 1
                continue
            prev_text = text
            prev_chapter = current_chapter
            
            text_escaped = self._escape_html(text)
            seg_type = segment.get("type", "paragraph")
            level = segment.get("level", 2)

            if seg_type == "heading":
                tag = f"h{min(level, 6)}"
                lines.append(f"<{tag}>{text_escaped}</{tag}>")

            elif seg_type == "equation":
                lines.append(f'<div class="equation">{text_escaped}</div>')

            elif seg_type == "caption":
                lines.append(f'<p class="caption">{text_escaped}</p>')

            elif seg_type == "list":
                lines.append(f"<li>{text_escaped}</li>")

            elif seg_type == "code":
                lines.append(f'<pre class="code">{text_escaped}</pre>')

            else:  # paragraph
                lines.append(f"<p>{text_escaped}</p>")

        if duplicate_count > 0:
            logger.info("EPUB: %d ardisik duplicate satir atlandi", duplicate_count)

        lines.extend([
            '</body>',
            '</html>'
        ])

        return "\n".join(lines)

    def _escape_html(self, text: str) -> str:
        """HTML karakterlerini escape et"""
        return (text
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;"))

    def rebuild_from_original(self, original_epub_path: str,
                              translations: Dict[str, str],
                              output_path: str) -> str:
        """
        Orijinal EPUB yapısını koruyarak çeviri uygula

        Args:
            original_epub_path: Orijinal EPUB dosyası
            translations: {orijinal_metin: çevrilmiş_metin} sözlüğü
            output_path: Çıktı dosya yolu

        Returns:
            Oluşturulan dosya yolu
        """
        from ebooklib import epub
        from bs4 import BeautifulSoup
        import warnings

        warnings.filterwarnings("ignore")

        book = epub.read_epub(original_epub_path)

        # Her öğeyi güncelle
        for item in book.get_items():
            if item.get_type() != 9:  # Sadece doküman tipi
                continue

            try:
                html_content = item.get_content().decode('utf-8')
                soup = BeautifulSoup(html_content, 'lxml')

                # Metin düğümlerini güncelle
                # APP-B11: bs4 4.10+ 'text' parametresi deprecated → 'string'.
                # replace_with() NavigableString bekler (str yerine).
                import bs4 as _bs4
                modified = False
                for element in soup.find_all(string=True):
                    original_text = str(element).strip()
                    if original_text and original_text in translations:
                        element.replace_with(
                            _bs4.NavigableString(translations[original_text])
                        )
                        modified = True

                if modified:
                    new_content = str(soup)
                    item.set_content(new_content.encode('utf-8'))

            except Exception as e:
                logger.warning("Bolum guncellenirken hata: %s", e)
                continue

        epub.write_epub(output_path, book)
        return output_path
