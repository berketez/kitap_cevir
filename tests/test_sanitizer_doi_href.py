"""Sanitizer DOI/publisher-artifact pass'i kaynakca \\href citation'larini
bozmamali (brace dengesizligi -> XeLaTeX belge yutar -> kesik PDF).

Kök neden (21 Haziran 2026, bir İngilizce bilim kitabının kaynakçası): _PUBLISHER_PATTERNS
DOI deseni `https?://doi\\.org/10\\.\\d{4,5}/[^\\s]+` acgozluydu, `}{` sinirini
asip \\href{doi}{text} yapilarinin kapanis parantezlerini yiyordu (+110 brace
dengesizligi, PDF 443->65 sayfaya dustu).
"""
import re

from latex_pipeline.sanitizer import LatexSanitizer


def _sanitize(text):
    out = LatexSanitizer().sanitize(text)
    return out[0] if isinstance(out, tuple) else out


def _balance(t):
    return t.count("{") - t.count("}")


def test_href_doi_citation_brace_balanced():
    """Marker'in cift-href DOI formu sanitize sonrasi brace-dengeli kalmali."""
    line = (
        r"\item Doe J (2011) Sample chapter title. Example Press, Ankara. "
        r"\href{https://doi.org/10.1234/978-9-999-12345-6_7}"
        r"{https://doi.org/10.1234/978-9-999-}"
    )
    assert _balance(line) == 0
    out = _sanitize(line)
    assert _balance(out) == 0, f"brace dengesi bozuldu: {out!r}"
    # href yapisi korunmali (citation kaybolmamali)
    assert "\\href{" in out


def test_href_url_doi_preserved():
    """\\href icindeki DOI URL'si silinmemeli (mesru citation)."""
    line = r"\href{https://doi.org/10.1234/abcd.5678}{Smith 2020}"
    out = _sanitize(line)
    assert "10.1234/abcd.5678" in out, "href icindeki DOI yanlislikla silindi"
    assert _balance(out) == 0


def test_bare_doi_still_removed():
    """Bare (href disinda) standalone DOI boilerplate yine silinebilmeli."""
    line = "Some text https://doi.org/10.1007/978-3-642-99999-9 trailing"
    out = _sanitize(line)
    assert "doi.org/10.1007/978-3-642-99999-9" not in out


def test_multiple_href_dois_balanced():
    """Ardisik birden cok DOI-href (kaynakca listesi) toplu brace-dengeli."""
    body = "\n".join(
        rf"\item Ref{i}. \href{{https://doi.org/10.1000/x{i}}}{{https://doi.org/10.1000/x{i}}}"
        for i in range(10)
    )
    assert _balance(body) == 0
    out = _sanitize(body)
    assert _balance(out) == 0
