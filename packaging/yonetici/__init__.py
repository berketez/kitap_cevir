"""Kitapçevir yöneticisi: kurulum sihirbazı, bileşen yönetimi ve süreç sahibi.

Yalnız stdlib (TASARIM A.2): DMG'deki taşınabilir Python'la çalışır.

İçe aktarma kuralı: paket iki biçimde yüklenebilir.
- Paket olarak: ``packaging/`` sys.path'teyken ``from yonetici import indirici``.
  (``packaging`` adı PyPI'daki ``packaging`` kitaplığıyla çakıştığı için depo
  kökünden ``packaging.yonetici`` biçimi KULLANILMAZ.)
- Düz betik olarak: ``packaging/yonetici`` sys.path[0] iken ``import indirici``.
Modüller birbirini önce göreli, olmazsa düz adla içe aktarır.

Bu dosya bilerek hiçbir şeyi içe aktarmaz: paketi yüklemek ağır modülleri
(ağ, iş parçacığı) çekmesin.
"""
