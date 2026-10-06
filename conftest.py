"""
Proje kökü conftest.py — pytest toplama denetimi.

tests/ dışında, proje kökündeki giriş dosyaları (Streamlit arayüzü ve komut
satırı girişi) pytest tarafından TEST olarak toplanmamalı.

APP-B06 düzeltmesi: eskiden test_multi_book.py, test_remaining_books.py ve
retest_all_books.py pytest tarafından yanlışlıkla toplanıyordu; bunlar
scripts/ altına taşınmıştı. scripts/ ve run_full_pipeline.py 5 Ekim 2026'da
ölü kod olarak silindi (KARAR 27; son hâlleri git etiketi
arsiv/olu-kod-20261005), listedeki karşılıkları da kaldırıldı.
"""

collect_ignore_glob = [
    "app.py",
    "translate_book.py",
]
