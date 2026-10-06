"""
Kitap Çevirici Pro - Parser Modülleri
PDF, EPUB ve diğer formatlardan yapısal veri çıkarma
"""

from .pdf_parser import PDFParser
from .epub_parser import EPUBParser

__all__ = ['PDFParser', 'EPUBParser']
