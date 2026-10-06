r"""Sanitizer eslesmeyen \( \) (inline-math) onarimi testleri.

Kök neden (21 Haziran 2026, AI-ML Decision/Risk kitabi): Marker citation
yillarini '(2015)' -> '\(2015)' (inline-math ac, kapatma yok) uretmis;
7 eslesmeyen \( belge sonuna kadar metni yutup XeLaTeX'i kilitledi
('Incomplete \ifmmode' + Emergency stop), PDF 443->364 sayfaya dustu.
"""
import re

from latex_pipeline.sanitizer import LatexSanitizer


def _sanitize(text):
    out = LatexSanitizer().sanitize(text)
    return out[0] if isinstance(out, tuple) else out


def _paren_balance(t):
    return len(re.findall(r"\\\(", t)) - len(re.findall(r"\\\)", t))


def test_unmatched_open_paren_neutralized():
    r"""Eslesmeyen \(2015) duz parantize cevrilmeli."""
    line = r"Thornbury \(2015) ve Steinhalter \(2019) iyi kaynaklardir."
    out = _sanitize(line)
    assert _paren_balance(out) == 0
    assert "(2015)" in out and "(2019)" in out


def test_unmatched_close_paren_neutralized():
    r"""Eslesmeyen 2017\) duz parantize cevrilmeli."""
    line = r"Lighthouse Press LLC, 2017\) yayinladi."
    out = _sanitize(line)
    assert _paren_balance(out) == 0


def test_balanced_inline_math_preserved():
    r"""Dengeli \(...\) (gercek/denklem ref) DOKUNULMAZ."""
    line = r"Denklem \(9.1\)'deki regresyon modeli onemlidir."
    out = _sanitize(line)
    # dengeli oldugu icin korunur (balance bozulmaz)
    assert _paren_balance(out) == 0
    assert r"\(9.1\)" in out


def test_mixed_artifacts_balanced():
    r"""Kaynakca: cok sayida eslesmeyen \( ve \) karisik -> hepsi dengelenir."""
    body = (
        r"Pike S, \(2018) ve Gaddson \(2018). "
        r"Northgate, 2020\). DOI 0123-4567\(91\)90001."
    )
    out = _sanitize(body)
    assert _paren_balance(out) == 0
