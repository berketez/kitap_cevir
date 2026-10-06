"""Ekran biçimleri: Türkçe ondalık sayı, yüzde ve dosya boyutu (TASARIM F12).

Yalnız standart kitaplık kullanılır; projenin başka hiçbir modülü içe
aktarılmaz. Bu yüzden ağır bağımlılıkları (``translate_book``, ``core``)
yükleyemeyen yerlerden de kullanılabilir; ör. paketli kurulumun yöneticisi
yalnız stdlib içeren bir Python'la koşar. ``ui.runner`` bu adları buradan alır
ve aynı adlarla dışa verir (``runner.format_size`` vb. eskisi gibi çalışır).
"""

from __future__ import annotations


def format_decimal(value: float | int, digits: int = 1) -> str:
    """Sayıyı Türkçe yazımla biçimle: ondalık ayırıcı virgül ("32,7").

    Yuvarlanmış değer tam sayıysa ondalık kısım yazılmaz ("100", "62");
    rapidfuzz benzerlikleri "%100.0" ve "%90.9090909090909" diye çıkıyordu
    (U16). Binlik ayırıcı eklenmez (sayılar küçük).
    """
    rounded = round(float(value), digits)
    if rounded != rounded or rounded in (float("inf"), float("-inf")):
        return str(value)               # NaN / sonsuz: olduğu gibi (uydurulmaz)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.{digits}f}".replace(".", ",")


def format_percent(value: float | int, digits: int = 1) -> str:
    """Yüzde: işaret sayıdan önce, Türkçe ondalık virgülle ("%32,7")."""
    return f"%{format_decimal(value, digits)}"


# Dosya boyutu birimleri, büyükten küçüğe. Ondalık (1 KB = 1000 bayt): macOS
# Finder da böyle sayar; kullanıcı ekrandaki boyutu Finder'dakiyle karşılaştırır.
SIZE_UNITS = (("GB", 10 ** 9), ("MB", 10 ** 6), ("KB", 10 ** 3))


def format_size(num_bytes: int | float, digits: int = 1) -> str:
    """Dosya boyutu KB/MB/GB'ye uyarlı, Türkçe ondalıkla ("72,9 KB", "1,3 MB").

    Kart eskiden her boyutu MB ile yazıyordu: 73 KB'lık EPUB "0,1 MB"
    görünüyordu (U15). Yuvarlanınca bir üst birime ulaşan değer o birimle
    yazılır ("999,96 KB" değil "1 MB").
    """
    value = float(num_bytes)
    for index, (unit, size) in enumerate(SIZE_UNITS):
        scaled = value / size
        if round(scaled, digits) < 1:
            continue
        if round(scaled, digits) >= 1000 and index > 0:
            bigger, bigger_size = SIZE_UNITS[index - 1]
            return f"{format_decimal(value / bigger_size, digits)} {bigger}"
        return f"{format_decimal(scaled, digits)} {unit}"
    return f"{format_decimal(value, 0)} bayt"
