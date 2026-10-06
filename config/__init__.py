"""Kitapçevir merkezi yapılandırma paketi.

Tüm proje boyunca paylaşılan sayısal sabitler ve varsayılanlar
`config.defaults` modülünden import edilir. Magic number kullanımına
karşı tek kaynak (KURAL §11).
"""

from .defaults import *  # noqa: F401,F403
