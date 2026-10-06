"""Veri kökü: kullanıcı verisinin tek kökü (packaging/TASARIM.md §F, F1).

Bu kökten türeyenler:

- ``output/``: kitap klasörleri (``output/<slug>/``);
- ``models/``: kullanıcının yerel modelleri (KARARLAR 1: tarama kökü yalnız burası);
- ``data/translation_memory.db``: çeviri belleği;
- ``config/*.json``: git dışı ayar dosyaları (``mt_selection.json``,
  ``mt_sources.json``, ``mt_engines.json``).

İki kip vardır:

- **Geliştirici kipi** (``KITAPCEVIR_VERI_KOKU`` yok ya da boş): veri kökü
  proje köküdür. Bütün yollar eskisiyle BİREBİR aynıdır.
- **Paketli kip** (DMG): yönetici Streamlit'i ``KITAPCEVIR_VERI_KOKU=<kök>/veri``
  ile başlatır (TASARIM B.8; ``packaging/yonetici/surecler.py``). Kod
  ``<kök>/app/<sürüm>``'de, veri ondan ayrı durur; güncelleme veriye dokunmaz.
  1_cikar / 2_derle / translate_book değişkeni ``ui.runner.Run.start``
  üzerinden miras alır (``os.environ`` alt sürece aynen geçer).

Kod kökü veri kökü DEĞİLDİR: betikler, şablonlar, ``glossary/``, ``docs/``
her zaman modülün kendi ``PROJECT_ROOT``'unun altındadır.

Yollar çağrı anında çözülür. Her işlev isteğe bağlı bir ``varsayilan`` alır;
çağıran modül oraya kendi ``PROJECT_ROOT``'unu verir. Değişken yokken dönen
yol o köktür. Böylece testlerin ``monkeypatch.setattr(modul, "PROJECT_ROOT",
tmp_path)`` yamaları aynen çalışır.

Bu modül yalnız standart kitaplığı kullanır.
"""

from __future__ import annotations

import os
from pathlib import Path

# Yöneticinin Streamlit'e verdiği ortam değişkenleri (TASARIM B.8).
VERI_KOKU_DEGISKENI = "KITAPCEVIR_VERI_KOKU"
YONETICI_URL_DEGISKENI = "KITAPCEVIR_YONETICI_URL"

# Proje (kod) kökü: config/ klasörünün bir üstü. Geliştirici kipinde veri
# kökü budur. Değişkene göreli yol verilirse o da buna göre çözülür: Streamlit
# ile alt süreçlerin çalışma klasörü farklı olabilir (ui.runner alt süreci
# cwd=PROJECT_ROOT ile başlatır), çalışma klasörüne göre çözülseydi iki süreç
# farklı kök görebilirdi.
PROJE_KOKU = Path(__file__).resolve().parent.parent

# Veri kökünün altındaki adlar (tek kaynak).
CIKTI_KLASORU = "output"
MODEL_KLASORU = "models"
BELLEK_KLASORU = "data"
AYAR_KLASORU = "config"
BELLEK_DOSYASI = "translation_memory.db"


def veri_koku(varsayilan: str | os.PathLike[str] | None = None) -> Path:
    """Kullanıcı verisinin kökü.

    ``KITAPCEVIR_VERI_KOKU`` doluysa o yol: ``~`` açılır, göreli yol
    ``PROJE_KOKU``'ya göre çözülür, ``..`` bileşenleri sözcüksel olarak atılır
    (çeviri belleğinin ``..`` içeren yolu reddeden güvenlik denetimi meşru bir
    kökü geri çevirmesin). Değişken boşsa ``varsayilan``; o da verilmemişse
    ``PROJE_KOKU``. Geliştirici kipinde dönen değer çağıranın eskiden
    kullandığı köktür, olduğu gibi.
    """
    deger = os.environ.get(VERI_KOKU_DEGISKENI, "")
    if deger.strip():
        yol = Path(deger).expanduser()
        if not yol.is_absolute():
            yol = PROJE_KOKU / yol
        return Path(os.path.abspath(yol))
    return Path(varsayilan) if varsayilan is not None else PROJE_KOKU


def paketli_kip() -> bool:
    """Uygulama paketli kurulumdan (DMG) mı açıldı?

    Yöneticinin verdiği iki değişkenden biri doluysa evet (TASARIM F5).
    Kullanıcıya gösterilen yönlendirmeler (ör. "kurulum rehberi" yerine
    "Bileşenler ekranı") buna göre seçilir.
    """
    return any(os.environ.get(ad, "").strip()
               for ad in (VERI_KOKU_DEGISKENI, YONETICI_URL_DEGISKENI))


def cikti_koku(varsayilan: str | os.PathLike[str] | None = None) -> Path:
    """Kitap klasörlerinin kökü: ``<veri kökü>/output``."""
    return veri_koku(varsayilan) / CIKTI_KLASORU


def model_koku(varsayilan: str | os.PathLike[str] | None = None) -> Path:
    """Yerel modellerin kökü: ``<veri kökü>/models``."""
    return veri_koku(varsayilan) / MODEL_KLASORU


def ayar_koku(varsayilan: str | os.PathLike[str] | None = None) -> Path:
    """Git dışı ayar dosyalarının klasörü: ``<veri kökü>/config``."""
    return veri_koku(varsayilan) / AYAR_KLASORU


def bellek_yolu(varsayilan: str | os.PathLike[str] | None = None) -> Path:
    """Çeviri belleği: ``<veri kökü>/data/translation_memory.db``."""
    return veri_koku(varsayilan) / BELLEK_KLASORU / BELLEK_DOSYASI
