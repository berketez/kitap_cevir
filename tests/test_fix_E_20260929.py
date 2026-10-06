"""E kulvarı düzeltmeleri (29 Eylül 2026) — EPUB giriş ve Türkçe EPUB çıkışı.

Bug avı (28 Eylül) doğrulanmış bulguları ve KARAR 18:

  F1-01  <p>'siz blok metni (td/th/caption/dt/dd/figcaption, yaprak div/
         section, karışık kapsayıcıdaki gevşek metin) kayboluyordu; tablolar
         satır/hücre yapısıyla markdown tablosu olur, Türkçe EPUB'da her hücre
         ayrı segmenttir (hizalama hücre birimleriyle).
  F1-02  düz metin / bağ metnindeki '$' markdown'a '\\$' yazılır.
  F1-07  MathML '$…$' TeX'i, <sup>/<sub> '<sup>..</sup>' / '<sub>..</sub>'.
  F1-08  metinli bloğun içindeki görsel atılmaz ('![](…)'; satır başı/sonu
         görsel kendi satırında: şekil + altyazı).
  F1-09  düz metindeki '\\', '*', '_', '`', '](' kaçışlanır.
  F1-13  spine'daki EPUB3 gezinme belgesi gövdeye girmez.
  F1-14  UTF-16 / ISO-8859-1 belgeler doğru okunur; çözülemeyen bayt uyarılır.
  F2-08  Türkçe EPUB'da SVG adları, head <style> ve (çeviride aynen kalan
         metindeki) <small> korunur.
  K18    EPUB kapsam raporu: çeviride aynen kalan blok kaynak dilde düzyazıysa
         çevrilmemiş sayılır (QC sınıflandırıcıları).

Tüm metinler sentetiktir; EPUB'lar zipfile ile elle kurulur (yapı tam
denetimde). Gerçek model, sunucu ya da ağ kullanılmaz.
"""

import io
import json
import re
import sys
import warnings
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from parsers import epub_parser as ep  # noqa: E402
from parsers.epub_parser import EPUBParser  # noqa: E402
from latex_pipeline import epub_output as eo  # noqa: E402

warnings.filterwarnings("ignore")

MATHML_NS = 'xmlns="http://www.w3.org/1998/Math/MathML"'
FILL = ("The quick brown fox jumps over the lazy dog near the river bank "
        "while the farmer tends the field.")

# ---------------------------------------------------------------------------
# Sentetik EPUB kurucusu
# ---------------------------------------------------------------------------

_CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""


def _xhtml(body: str, lang: str = "en", head: str = "", enc: str = "utf-8") -> str:
    return (f'<?xml version="1.0" encoding="{enc}"?>\n<!DOCTYPE html>\n'
            f'<html xmlns="http://www.w3.org/1999/xhtml" '
            f'xmlns:epub="http://www.idpf.org/2007/ops" lang="{lang}" xml:lang="{lang}">\n'
            f'<head><title>Belge</title>{head}</head>\n<body>\n{body}\n</body>\n</html>')


def _png(color=(200, 30, 30), size=(40, 30)) -> bytes:
    Image = pytest.importorskip("PIL.Image")
    im = Image.new("RGB", size)
    w, h = size
    im.putdata([((x * 37 + y * 11 + color[0]) % 256, (x * 5 + y * 71) % 256,
                 (x * y * 13 + color[2]) % 256) for y in range(h) for x in range(w)])
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _build(path, docs, *, title="Sentetik Kitap", lang="en", nav=None,
           nav_in_spine=False, images=None, author="Ada Yazar"):
    """docs: [(dosya, kimlik, içerik (str ya da bytes))] spine sırasıyla."""
    images = images or {}
    manifest, spine = [], []
    for href, did, _ in docs:
        manifest.append(f'<item id="{did}" href="{href}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="{did}"/>')
    if nav is not None:
        manifest.append('<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" '
                        'properties="nav"/>')
        if nav_in_spine:
            spine.insert(0, '<itemref idref="nav"/>')
    for href, (iid, _data, media) in images.items():
        manifest.append(f'<item id="{iid}" href="{href}" media-type="{media}"/>')
    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="uid">urn:uuid:kc-fix-e-{abs(hash(title)) % 10**8}</dc:identifier>
<dc:title>{title}</dc:title><dc:creator>{author}</dc:creator><dc:language>{lang}</dc:language>
<meta property="dcterms:modified">2026-09-29T00:00:00Z</meta>
</metadata>
<manifest>
{chr(10).join(manifest)}
</manifest>
<spine>
{chr(10).join(spine)}
</spine>
</package>"""
    path = Path(path)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                   compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", _CONTAINER)
        z.writestr("OEBPS/content.opf", opf)
        for href, _did, content in docs:
            z.writestr(f"OEBPS/{href}", content)
        if nav is not None:
            z.writestr("OEBPS/nav.xhtml", nav)
        for href, (_iid, data, _media) in images.items():
            z.writestr(f"OEBPS/{href}", data)
    return str(path)


def _parse(path):
    p = EPUBParser()
    with redirect_stdout(io.StringIO()):
        doc = p.parse(path)
    return p, doc


def _md(path):
    p, doc = _parse(path)
    return p.to_markdown(doc)


def _segments(p, doc):
    with redirect_stdout(io.StringIO()):
        return p.extract_segments(doc)


def _one_doc(tmp_path, body, name="k.epub", **kw):
    return _build(tmp_path / name, [("ch1.xhtml", "c1", _xhtml(body, **kw))])


def _read_member(path, suffix) -> str:
    with zipfile.ZipFile(path) as z:
        n = next(n for n in z.namelist() if n.endswith(suffix))
        return z.read(n).decode("utf-8")


def _converter():
    from latex_pipeline.converter import MarkdownToLatex
    from latex_pipeline.config import PipelineConfig
    return MarkdownToLatex(PipelineConfig(document_class="book"))


# ---------------------------------------------------------------------------
# F1-01: <p>'siz blok metni ve tablolar
# ---------------------------------------------------------------------------

class TestF101LooseBlocks:

    BODY = f"""<h1>Chapter One Heat</h1>
<p>Normal paragraph Qa01q. {FILL}</p>
<div class="calibre3">Loose div text Qb02q. {FILL}</div>
<dl><dt>Term Qg07q</dt><dd>Definition Qh08q</dd></dl>
<figure><figcaption>Figure caption Qi09q</figcaption></figure>
<section>Loose section text Qm12q<p>Section paragraph Qn13q. {FILL}</p>tail Qz99q</section>
<p>Closing Qo14q. {FILL}</p>"""

    def test_loose_text_reaches_markdown_and_segments_in_order(self, tmp_path):
        p, doc = _parse(_one_doc(tmp_path, self.BODY))
        md = p.to_markdown(doc)
        order = ["Qa01q", "Qb02q", "Qg07q", "Qh08q", "Qi09q", "Qm12q", "Qn13q",
                 "Qz99q", "Qo14q"]
        pos = [md.find(w) for w in order]
        assert all(i >= 0 for i in pos), dict(zip(order, pos))
        assert pos == sorted(pos)
        segs = " ".join(s["text"] for s in _segments(p, doc))
        assert all(w in segs for w in order)

    def test_title_segment_in_document_position(self, tmp_path):
        """Başlıktan önce metin varsa (Gutenberg üst bilgisi) başlık segmenti
        belgedeki yerinde: sıralı hizalama (E2) onu eşleyebilir."""
        body = (f"<div>*** START OF THE EBOOK Qx1q ***</div><h2>Real Title Qt1q</h2>"
                f"<p>{FILL}</p>")
        src = _one_doc(tmp_path, body)
        p, doc = _parse(src)
        segs = _segments(p, doc)
        texts = [s["text"] for s in segs]
        assert texts.index("Real Title Qt1q") > [i for i, t in enumerate(texts)
                                                  if "Qx1q" in t][0]
        with redirect_stdout(io.StringIO()):
            body_tex = _converter().convert(p.to_markdown(doc))
        tr_tex = body_tex.replace("Real Title Qt1q", "Gerçek Başlık Qt1q")
        translations, _rep = eo.align_translations(segs, body_tex, tr_tex)
        i = texts.index("Real Title Qt1q")
        assert translations.get(i) == "Gerçek Başlık Qt1q"
        out = tmp_path / "o.epub"
        with redirect_stdout(io.StringIO()):
            p.rebuild_epub(doc, {i: "Gerçek Başlık Qt1q"}, str(out), language="tr")
        assert "<h2>Gerçek Başlık Qt1q</h2>" in _read_member(out, "ch1.xhtml")

    def test_code_only_div_stays_code_block(self, tmp_path):
        body = "<h1>K</h1><div><code>x = 1</code></div><div>Use <code>y</code> now.</div>"
        md = _md(_one_doc(tmp_path, body))
        assert "```\nx = 1\n```" in md
        assert "Use y now." in md

    def test_rebuild_writes_loose_blocks_and_unwraps(self, tmp_path):
        src = _one_doc(tmp_path, self.BODY)
        p, doc = _parse(src)
        segs = _segments(p, doc)
        tr = {i: "TR " + s["text"] for i, s in enumerate(segs)}
        out = tmp_path / "out.epub"
        with redirect_stdout(io.StringIO()):
            p.rebuild_epub(doc, tr, str(out), language="tr")
        html = _read_member(out, "ch1.xhtml")
        assert "kc-blok" not in html
        for w in ("Qb02q", "Qg07q", "Qh08q", "Qi09q", "Qm12q", "Qz99q"):
            assert re.search(r"TR [^<]*" + w, html), w
        # Karışık section yapısı korunur: gevşek metin, p, gevşek metin.
        sec = re.search(r"<section>(.*?)</section>", html, re.S).group(1)
        assert sec.index("Qm12q") < sec.index("<p>") < sec.index("Qz99q")


class TestF101Tables:

    def _table_md(self, tmp_path, table_html):
        body = f"<h1>K</h1><p>{FILL}</p>{table_html}<p>Son {FILL}</p>"
        p, doc = _parse(_one_doc(tmp_path, body))
        return p, doc, p.to_markdown(doc)

    def test_header_table(self, tmp_path):
        _p, _d, md = self._table_md(tmp_path, (
            "<table><caption>Table caption Qc03q</caption>"
            "<tr><th>Header Qd04q</th><th>Value</th></tr>"
            "<tr><td>Cell Qe05q</td><td><p>Cell with p Qf06q</p></td></tr></table>"))
        assert ("Table caption Qc03q\n\n| Header Qd04q | Value |\n|---|---|\n"
                "| Cell Qe05q | Cell with p Qf06q |") in md

    def test_headerless_table_colspan_rowspan_and_br(self, tmp_path):
        _p, _d, md = self._table_md(tmp_path, (
            "<table><tr><td rowspan='2'>1º</td><td>Que les fonds<br/>ne seraient pas</td>"
            "<td>1000</td></tr><tr><td colspan='2'>Total 5,446 dollars</td></tr>"
            "<tr><td>2º</td><td>*étoile* _x_</td><td>$3</td></tr></table>"))
        assert ("|  |  |  |\n|---|---|---|\n"
                "| 1º | Que les fonds ne seraient pas | 1000 |\n"
                "|  | Total 5,446 dollars |  |\n"
                "| 2º | \\*étoile\\* \\_x\\_ | \\$3 |") in md

    def test_unsafe_tables_fall_back_to_paragraphs(self, tmp_path):
        # tek sütun, hücrede '|', iç içe tablo, çok paragraflı hücre
        for html, words in [
            ("<table><tr><td>Only Qs1q</td></tr><tr><td>Col Qs2q</td></tr></table>",
             ["Qs1q", "Qs2q"]),
            ("<table><tr><td>a | b Qp1q</td><td>c Qp2q</td></tr></table>", ["Qp1q", "Qp2q"]),
            ("<table><tr><td><table><tr><td>in Qn1q</td><td>x</td></tr></table></td>"
             "<td>out Qn2q</td></tr></table>", ["Qn1q", "Qn2q"]),
            ("<table><tr><td><p>p1 Qm1q</p><p>p2 Qm2q</p></td><td>Qm3q</td></tr></table>",
             ["Qm1q", "Qm2q", "Qm3q"]),
        ]:
            _p, _d, md = self._table_md(tmp_path, html)
            assert "|---" not in md, html
            for w in words:
                assert re.search(r"(?m)^(?!\|)[^\n]*" + w, md), (w, md)

    def test_image_only_cell(self, tmp_path):
        imgs = {"images/ic.png": ("i1", _png(), "image/png")}
        body = (f"<h1>K</h1><p>{FILL}</p><table><tr><td><img src='images/ic.png'/></td>"
                f"<td>Icon Qi1q</td></tr></table>")
        p, doc = _parse(_build(tmp_path / "t.epub", [("ch1.xhtml", "c1", _xhtml(body))],
                               images=imgs))
        md = p.to_markdown(doc)
        assert "| ![](images/ic.png) | Icon Qi1q |" in md
        assert "\n![](images/ic.png)\n" not in md

    def test_tex_units_split_cells_and_alignment(self, tmp_path):
        """E2 hizalaması: hücre segmentleri ↔ LaTeX hücre birimleri 1:1."""
        src = _one_doc(tmp_path, (
            f"<h1>Chapter Grid</h1><p>{FILL}</p><table>"
            "<tr><th>Name alpha</th><th>Value beta</th></tr>"
            "<tr><td>Gamma cell</td><td>Delta cell</td></tr></table>"
            f"<p>After {FILL}</p>"))
        p, doc = _parse(src)
        md = p.to_markdown(doc)
        with redirect_stdout(io.StringIO()):
            body = _converter().convert(md)
        cells = [u.raw for u in eo._tex_units(body) if u.kind == "cell"]
        assert cells == ["Name alpha", "Value beta", "Gamma cell", "Delta cell"]
        segs = _segments(p, doc)
        tr_body = body.replace("Gamma cell", "Gama hücresi").replace(
            "Delta cell", "Delta hücresi")
        translations, report = eo.align_translations(segs, body, tr_body)
        texts = [translations.get(i) for i, s in enumerate(segs)
                 if s["text"] in ("Gamma cell", "Delta cell")]
        assert texts == ["Gama hücresi", "Delta hücresi"]
        assert report["coverage"] == 1.0

    def test_printed_toc_table_not_counted(self, tmp_path):
        """İçindekiler başlığı + tablo: dönüştürücü ikisini de atar (kitap
        kendi \\tableofcontents'ını üretir). Hücreler hizalamaya/kapsama
        girmez (önceden hiç çıkarılmıyordu), başlık Türkçe EPUB'da
        'İçindekiler' olur."""
        src = _one_doc(tmp_path, (
            f"<h1>Capítulo uno</h1><p>{FILL}</p><h2>ÍNDICE</h2><table>"
            "<tr><td><a href='#c1'>Cap. I</a>.—</td><td>Consideraciones generales.</td>"
            "<td>1</td></tr><tr><td>Cap. II.—</td><td>Preocupaciones del principiante.</td>"
            "<td>13</td></tr></table>"), lang="es")
        p, doc = _parse(src)
        md = p.to_markdown(doc)
        assert "Consideraciones generales." in md
        segs = _segments(p, doc)
        roles = {s["text"]: s.get("role") for s in segs}
        assert roles["ÍNDICE"] == "printed_toc_heading"
        assert roles["Consideraciones generales."] == "printed_toc"
        with redirect_stdout(io.StringIO()):
            body = _converter().convert(md)
        assert "Consideraciones" not in body and "ÍNDICE" not in body
        translations, report = eo.align_translations(segs, body, body)
        head = next(i for i, s in enumerate(segs) if s["text"] == "ÍNDICE")
        assert translations[head] == ep.EPUB_TOC_TITLE_TR
        cells = [i for i, s in enumerate(segs) if s.get("role") == "printed_toc"]
        assert cells and not set(cells) & set(report["counted_ids"])
        assert report["coverage"] == 1.0

    def test_rebuild_writes_into_cells(self, tmp_path):
        src = _one_doc(tmp_path, (
            "<h1>K</h1><table><tr><th>H1 Qa</th><th>H2 Qb</th></tr>"
            "<tr><td>C1 Qc</td><td>C2 Qd</td></tr></table>"))
        p, doc = _parse(src)
        segs = _segments(p, doc)
        tr = {i: "TR " + s["text"] for i, s in enumerate(segs)}
        out = tmp_path / "o.epub"
        with redirect_stdout(io.StringIO()):
            p.rebuild_epub(doc, tr, str(out), language="tr")
        html = _read_member(out, "ch1.xhtml")
        for w in ("H1 Qa", "H2 Qb", "C1 Qc", "C2 Qd"):
            assert re.search(r"<t[hd]>TR " + w + "</t[hd]>", html), w


# ---------------------------------------------------------------------------
# F1-02 / F1-09: düz metin kaçışı
# ---------------------------------------------------------------------------

class TestF102F109Escaping:

    def test_md_escape_minimal_and_roundtrip(self):
        cases = {
            "cost $100 and $320": "cost \\$100 and \\$320",
            "2*3*4": "2\\*3\\*4",
            "C:\\Users\\x": "C:\\\\Users\\\\x",
            "snake_case _private_ __dunder__": "snake_case \\_private\\_ \\_\\_dunder\\_\\_",
            "[see here](not a link)": "[see here\\](not a link)",
            "use `tick`": "use \\`tick\\`",
            "Gutenberg [*] and [12] and [12 note]": "Gutenberg [\\*] and [12] and [12 note]",
        }
        for raw, esc in cases.items():
            assert ep.md_escape(raw) == esc, raw
            assert ep.md_unescape(esc) == raw

    def test_paragraph_specials_escaped(self, tmp_path):
        import html as _h
        text = ("A US phone pattern is \\+?1?\\s?(\\d{3}), 2*3*4, _private_ and "
                "__dunder__, [see here](not a link), cost $100 and $320.")
        md = _md(_one_doc(tmp_path, f"<h1>K</h1><p>{_h.escape(text)}</p>"))
        assert ep.md_escape(text) in md

    def test_link_label_dollar_escaped_and_converter_no_math(self, tmp_path):
        body = ("<h1>K</h1><p>DOLAR2 Its <a href='https://oreil.ly/Xamik'>revenue crossed "
                "$100 million</a> in two years, with <a href='https://oreil.ly/t0xDf'>Magic "
                "raising $320 million</a> soon.</p>")
        md = _md(_one_doc(tmp_path, body))
        assert "[revenue crossed \\$100 million](https://oreil.ly/Xamik)" in md
        with redirect_stdout(io.StringIO()):
            tex = _converter().convert(md)
        line = next(l for l in tex.split("\n") if "DOLAR2" in l)
        assert "\\href{https://oreil.ly/Xamik}{revenue crossed \\$100 million}" in line
        assert re.search(r"(?<!\\)\$", line) is None

    def test_comment_is_not_text(self, tmp_path):
        md = _md(_one_doc(tmp_path, "<h1>K</h1><p>alpha <!-- gizli --> beta</p>"))
        assert "gizli" not in md and "alpha beta" in md

    def test_rebuild_fallback_piece_unescaped(self, tmp_path):
        """<br/>'li öğede çevirisi olmayan parça özgün metniyle kalır: Türkçe
        EPUB'a kaçış ters bölüsü sızmaz."""
        src = _one_doc(tmp_path, "<h1>K</h1><p>First 2*3 $5 line<br/><br/>Second _x_ line</p>")
        p, doc = _parse(src)
        segs = _segments(p, doc)
        idx = next(i for i, s in enumerate(segs) if "Second" in s["text"])
        out = tmp_path / "o.epub"
        with redirect_stdout(io.StringIO()):
            p.rebuild_epub(doc, {idx: "İkinci satır"}, str(out), language="tr")
        html = _read_member(out, "ch1.xhtml")
        assert "First 2*3 $5 line" in html and "\\" not in html.split("<body")[1]

    def test_gutenberg_notes_still_linked(self, tmp_path):
        body = ("<h1>Bir</h1><p>Haritalar[*] iyidir.</p><p>[* Ay notu burada.]</p>"
                "<p>Numara[3] burada.</p><p>Ara.</p><p>[3 Üçüncü not.]</p>")
        md = _md(_one_doc(tmp_path, body))
        assert "Haritalar[^kc-1] iyidir." in md and "Numara[^kc-2] burada." in md
        assert "[^kc-1]: Ay notu burada." in md and "[^kc-2]: Üçüncü not." in md

    def test_escaped_note_marker_label(self, tmp_path):
        """Etiketi kaçışlı işaret ('*') olan dipnot bağlantısı tanınır."""
        body = ("<h1>Bir</h1><p>Metin<a href='#fn1' class='noteref'>*</a> sürer.</p>"
                "<aside epub:type='footnote' id='fn1'><p>Yıldız notu.</p></aside>")
        md = _md(_one_doc(tmp_path, body))
        assert "Metin[^kc-1] sürer." in md and "[^kc-1]: Yıldız notu." in md


# ---------------------------------------------------------------------------
# F1-07: MathML, <sup>/<sub>
# ---------------------------------------------------------------------------

class TestF107MathAndScripts:

    def test_mathml_to_latex(self):
        from bs4 import BeautifulSoup
        cases = [
            ("<mi>E</mi><mo>=</mo><mi>m</mi><msup><mi>c</mi><mn>2</mn></msup>",
             "E = m {c}^{2}"),
            ("<mfrac><mi>a</mi><mi>b</mi></mfrac>", "\\frac{a}{b}"),
            ("<msqrt><mn>2</mn></msqrt>", "\\sqrt{2}"),
            ("<munderover><mo>∑</mo><mrow><mi>i</mi><mo>=</mo><mn>1</mn></mrow><mi>n</mi>"
             "</munderover><msub><mi>x</mi><mi>i</mi></msub>", "∑_{i = 1}^{n} {x}_{i}"),
            ("<mover><mi>v</mi><mo>→</mo></mover>", "\\vec{v}"),
            ("<mi>sin</mi><mo>&#x2061;</mo><mi>θ</mi>", "\\sin θ"),
            ("<mtext>if x &gt; 0 &amp; 50%</mtext>", "\\text{if x > 0 \\& 50\\%}"),
            ("<semantics><mi>x</mi><annotation encoding='application/x-tex'>x^{2}+1"
             "</annotation></semantics>", "x^{2}+1"),
        ]
        for inner, want in cases:
            soup = BeautifulSoup(f"<p><math {MATHML_NS}>{inner}</math></p>", "lxml")
            tex, display, unsupported = ep.mathml_to_latex(soup.find("math"))
            assert (tex, display, unsupported) == (want, False, set()), inner

    def test_mathml_in_paragraph_and_display_block(self, tmp_path):
        body = (f"<h1>K</h1><p>MATHML1 The energy is <math {MATHML_NS}><mi>E</mi><mo>=</mo>"
                f"<mi>m</mi><msup><mi>c</mi><mn>2</mn></msup></math> at rest.</p>"
                f"<p><math {MATHML_NS} display='block'><mfrac><mi>a</mi><mi>b</mi></mfrac>"
                f"</math></p>"
                f"<div><math {MATHML_NS} display='block'><msqrt><mn>2</mn></msqrt></math></div>")
        md = _md(_one_doc(tmp_path, body))
        assert "MATHML1 The energy is $E = m {c}^{2}$ at rest." in md
        assert "\n$$\\frac{a}{b}$$\n" in md
        assert "\n$$\\sqrt{2}$$" in md

    def test_unsupported_mathml_kept_and_warned(self, tmp_path):
        body = (f"<h1>K</h1><p>Content <math {MATHML_NS}><apply><plus/><ci>y</ci>"
                f"<cn>2</cn></apply></math> ok.</p>")
        p, doc = _parse(_one_doc(tmp_path, body))
        md = p.to_markdown(doc)
        assert "$y 2$" in md
        assert any("MathML" in w and "apply" in w for w in doc.warnings)

    def test_sup_sub_markup(self, tmp_path):
        body = ("<h1>K</h1><p>SUBSUP Water is H<sub>2</sub>O, area 5 m<sup>2</sup>, "
                "le 1<sup>er</sup>, x<sup>a_b</sup>, note<sup><a href='#x1'>1</a></sup>.</p>")
        md = _md(_one_doc(tmp_path, body))
        # '_' içeren üst simge işaretlenmez (dönüştürücü alt simge sanardı);
        # dipnot bağlantılı <sup> eski yoldan (işaret [1](#x1)) yazılır.
        assert ("SUBSUP Water is H<sub>2</sub>O, area 5 m<sup>2</sup>, le 1<sup>er</sup>, "
                "xa_b, note[1](#x1).") in md
        assert "<sup>[" not in md


# ---------------------------------------------------------------------------
# F1-08: metinli bloktaki görsel
# ---------------------------------------------------------------------------

class TestF108InlineImages:

    IMGS = None

    def _epub(self, tmp_path, body):
        imgs = {"images/eq1.png": ("i1", _png(), "image/png"),
                "images/fig2.png": ("i2", _png((90, 0, 90)), "image/png")}
        return _build(tmp_path / "g.epub", [("text/ch1.xhtml", "c1", _xhtml(body))],
                      images=imgs)

    def test_inline_and_leading_image(self, tmp_path):
        body = (f"<h1>K</h1><p>Inline formula image <img src='../images/eq1.png' "
                f"alt='x squared'/> Qk10q and more.</p>"
                f"<p class='center'><img src='../images/fig2.png' alt=''/><br/>"
                f"Figure 2. Caption Ql11q</p>")
        md = _md(self._epub(tmp_path, body))
        assert "Inline formula image ![x squared](images/eq1.png) Qk10q and more." in md
        assert "\n![](images/fig2.png)\nFigure 2. Caption Ql11q" in md

    def test_heading_image_becomes_body_image(self, tmp_path):
        body = "<h1><img src='../images/fig2.png'/> Chapter Qh1q</h1><p>Text here.</p>"
        md = _md(self._epub(tmp_path, body))
        assert "# Chapter Qh1q" in md and "![](images/fig2.png)" in md

    def test_extractor_maps_inline_and_warns_unusable(self, tmp_path, caplog):
        from latex_pipeline.epub_extractor import EPUBExtractor
        body = (f"<h1>K</h1><p>Inline <img src='../images/eq1.png' alt='x sq'/> and "
                f"<img src='../images/yok.svg' alt='vektör'/> end.</p><p>{FILL}</p>")
        src = self._epub(tmp_path, body)
        with caplog.at_level("WARNING", logger="parsers.epub_parser"), \
                redirect_stdout(io.StringIO()):
            res = EPUBExtractor().extract(src, tmp_path / "out")
        assert "Inline ![x sq](eq1.png) and vektör end." in res.markdown
        assert any("yok.svg" in r.getMessage() for r in caplog.records)

    def test_converter_inline_and_figure(self, tmp_path):
        with redirect_stdout(io.StringIO()):
            tex = _converter().convert(
                "# K\n\nA ![x](eq1.png) b.\n\n![](fig2.png)\nFigure 2. Caption\n")
        assert "\\includegraphics[height=1em]{eq1.png}" in tex
        assert "\\includegraphics[width=0.8\\textwidth]{fig2.png}" in tex


# ---------------------------------------------------------------------------
# F1-13: spine'daki gezinme belgesi
# ---------------------------------------------------------------------------

_NAV = _xhtml('<nav epub:type="toc" id="toc"><h1>Inhalt</h1><ol>'
              '<li><a href="ch1.xhtml">Erstes Kapitel</a></li>'
              '<li><a href="ch2.xhtml">Zweites Kapitel</a></li></ol></nav>', "de")


class TestF113NavInSpine:

    def _epub(self, tmp_path, nav=_NAV):
        docs = [("ch1.xhtml", "c1", _xhtml(f"<h1>Erstes Kapitel</h1><p>{FILL}</p>", "de")),
                ("ch2.xhtml", "c2", _xhtml(f"<h1>Zweites Kapitel</h1><p>{FILL}</p>", "de"))]
        return _build(tmp_path / "n.epub", docs, nav=nav, nav_in_spine=True, lang="de")

    def test_spine_nav_not_in_body(self, tmp_path):
        p, doc = _parse(self._epub(tmp_path))
        md = p.to_markdown(doc)
        assert "Inhalt" not in md and ".xhtml" not in md
        assert [c.id for c in doc.chapters] == ["c1", "c2"]

    def test_text_outside_nav_warned(self, tmp_path, caplog):
        nav = _NAV.replace("<body>", "<body><p>Widmung außerhalb der Navigation.</p>")
        with caplog.at_level("WARNING", logger="parsers.epub_parser"):
            _parse(self._epub(tmp_path, nav))
        assert any("Widmung" in r.getMessage() for r in caplog.records)

    def test_rebuild_consistent(self, tmp_path):
        src = self._epub(tmp_path)
        p, doc = _parse(src)
        segs = _segments(p, doc)
        tr = {i: "TR " + s["text"] for i, s in enumerate(segs)}
        stats = {}
        out = tmp_path / "o.epub"
        with redirect_stdout(io.StringIO()):
            p.rebuild_epub(doc, tr, str(out), language="tr", stats=stats)
        assert stats["written"] == set(tr)
        assert "TR Erstes Kapitel" in _read_member(out, "ch1.xhtml")


# ---------------------------------------------------------------------------
# F1-14: belge kodlaması
# ---------------------------------------------------------------------------

class TestF114Encoding:

    FR = ("La chaleur se propage des régions chaudes vers les régions froides "
          "à travers la paroi.")

    def _epub(self, tmp_path, enc, raw=None):
        ch1 = _xhtml(f"<h1>Chapitre premier</h1><p>{self.FR}</p>", "fr", enc=enc)
        ch1b = raw if raw is not None else ch1.encode(enc)
        ch2 = _xhtml(f"<h1>Chapitre deuxième</h1><p>{self.FR}</p>", "fr")
        return _build(tmp_path / f"{enc}.epub", [("ch1.xhtml", "c1", ch1b),
                                                ("ch2.xhtml", "c2", ch2)], lang="fr")

    @pytest.mark.parametrize("enc", ["utf-16", "iso-8859-1", "utf-16-le"])
    def test_non_utf8_documents(self, tmp_path, enc):
        raw = None
        if enc == "utf-16-le":   # BOM'suz UTF-16
            raw = _xhtml(f"<h1>Chapitre premier</h1><p>{self.FR}</p>", "fr",
                         enc="utf-16").encode("utf-16-le")
        p, doc = _parse(self._epub(tmp_path, enc, raw))
        md = p.to_markdown(doc)
        assert "Chapitre premier" in md and "Chapitre deuxième" in md
        assert md.count("régions chaudes") == 2 and "\ufffd" not in md
        # rebuild_epub aynı segmentleri görür
        segs = _segments(p, doc)
        stats = {}
        with redirect_stdout(io.StringIO()):
            p.rebuild_epub(doc, {i: "TR" for i in range(len(segs))},
                           str(tmp_path / "o.epub"), stats=stats)
        assert stats["written"] == set(range(len(segs)))

    def test_invalid_bytes_warned(self, tmp_path):
        raw = _xhtml(f"<h1>Chapitre premier</h1><p>{self.FR}</p>", "fr").encode("utf-8")
        raw = raw.replace("régions".encode("utf-8"), b"r\xe9gions", 1)
        p, doc = _parse(self._epub(tmp_path, "utf-8", raw))
        assert any("UTF-8" in w for w in doc.warnings)


# ---------------------------------------------------------------------------
# F2-08: Türkçe EPUB'da SVG, <style>, <small>
# ---------------------------------------------------------------------------

class TestF208RebuildDetails:

    XHTML = _xhtml(
        '<h1>Chapitre premier</h1>'
        '<p class="ozel">La lune tourne autour de la terre en vingt-sept jours.</p>'
        '<p>Le diagramme <small>(voir la figure)</small> montre l\'orbite.</p>'
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 50" '
        'preserveAspectRatio="xMidYMid meet" width="200"><linearGradient id="g" '
        'gradientUnits="userSpaceOnUse"/><rect width="100" height="50" fill="url(#g)"/></svg>'
        '<p>Fin du chapitre avec une phrase assez longue pour compter.</p>',
        "fr", head='<style type="text/css">p.ozel { color: #a00; }</style>')

    def test_svg_case_style_small(self, tmp_path):
        from lxml import etree
        src = _build(tmp_path / "s.epub", [("ch1.xhtml", "c1", self.XHTML)], lang="fr")
        p, doc = _parse(src)
        segs = _segments(p, doc)
        tr = {i: "TR " + s["text"] for i, s in enumerate(segs)}
        out = tmp_path / "o.epub"
        with redirect_stdout(io.StringIO()):
            p.rebuild_epub(doc, tr, str(out), language="tr")
        html = _read_member(out, "ch1.xhtml")
        for name in ("viewBox=", "preserveAspectRatio=", "<linearGradient",
                     "gradientUnits=", "<style", "p.ozel { color: #a00; }",
                     "<small>(voir la figure)</small>"):
            assert name in html, name
        assert "viewbox=" not in html and "lineargradient" not in html
        etree.fromstring(html.encode("utf-8"))   # iyi biçimli XHTML

    def test_fix_svg_case_only_in_tags(self):
        text = '<body><p>viewbox= metin</p><svg viewbox="0 0 1 1"><lineargradient/></svg></body>'
        assert ep._fix_svg_case(text) == (
            '<body><p>viewbox= metin</p><svg viewBox="0 0 1 1"><linearGradient/></svg></body>')


# ---------------------------------------------------------------------------
# Türkçe EPUB: formül ve görsel nesneleri, alt simge
# ---------------------------------------------------------------------------

class TestRebuildObjects:

    def test_tex_to_epub_text_objects_scripts_literals(self):
        text = eo._tex_to_epub_text(
            "Energy \\$5, $E=mc^{2}$, m$^{2}$, H$_{2}$O, 2*3 [x](y) "
            "\\href{http://u}{lab} \\includegraphics[height=1em]{eq1.png}", emphasis=True)
        assert ep.EPUB_OBJ_OPEN + "m:$E=mc^{2}$" + ep.EPUB_OBJ_CLOSE in text
        assert "m" + ep.EPUB_SUP_OPEN + "2" + ep.EPUB_SUP_CLOSE in text
        assert "H" + ep.EPUB_SUB_OPEN + "2" + ep.EPUB_SUB_CLOSE + "O" in text
        assert ep.EPUB_OBJ_OPEN + "g:eq1.png" + ep.EPUB_OBJ_CLOSE in text
        assert "2\\*3 \\[x\\](y) [lab](http://u)" in text
        assert "$5" in text
        # biçimsiz kip (kitap adı) eski davranış
        assert eo._tex_to_epub_text("A $x^{2}$ b", emphasis=False) == "A $x^2$ b"

    def test_addcontentsline_not_epub_text(self):
        tail = "\\addcontentsline{toc}{section}{\\csname @currentlabelname\\endcsname}"
        assert eo._tex_to_epub_text("Kaynakça " + tail) == "Kaynakça"
        assert eo._tex_to_epub_text(tail, emphasis=True) == ""

    def test_rebuild_restores_mathml_and_image(self, tmp_path):
        imgs = {"images/eq1.png": ("i1", _png(), "image/png")}
        body = (f"<h1>K</h1><p>The energy is <math {MATHML_NS}><mi>E</mi><mo>=</mo>"
                f"<mi>m</mi><msup><mi>c</mi><mn>2</mn></msup></math> and <img "
                f"src='../images/eq1.png'/> here, 2*3.</p>")
        src = _build(tmp_path / "m.epub", [("text/ch1.xhtml", "c1", _xhtml(body))],
                     images=imgs)
        p, doc = _parse(src)
        segs = _segments(p, doc)
        idx = next(i for i, s in enumerate(segs) if "energy" in s["text"])
        tr_tex = ("Enerji $E = m {c}^{2}$ ve \\includegraphics[height=1em]{eq1.png} "
                  "burada, 2*3.")
        tr = {idx: p._html_safe_text(eo._tex_to_epub_text(tr_tex, emphasis=True))}
        out = tmp_path / "o.epub"
        with redirect_stdout(io.StringIO()):
            p.rebuild_epub(doc, tr, str(out), language="tr")
        html = _read_member(out, "ch1.xhtml")
        para = re.search(r"<p>Enerji.*?</p>", html, re.S).group(0)
        assert "<math" in para and "<msup>" in para and "$" not in para
        assert para.index("<math") < para.index(" ve ") < para.index("<img") \
            < para.index("burada, 2*3.")
        assert "\\" not in para and "<em>" not in para


    def test_translator_note_with_math_has_no_placeholder(self, tmp_path):
        """Çevirmen notundaki formülün karşılık <math>'ı yoktur: TeX'i metin
        olarak kalır, <kc-nesne> yer tutucusu çıktıya sızmaz."""
        src = _one_doc(tmp_path, "<h1>K</h1><p>Paragraf metni burada.</p>")
        p, doc = _parse(src)
        segs = _segments(p, doc)
        idx = next(i for i, s in enumerate(segs) if "Paragraf" in s["text"])
        note = p._html_safe_text(eo._tex_to_epub_text("Çevirmen notu: $x^{2}+1$ bak.",
                                                      emphasis=True))
        tr = {idx: "Çeviri" + ep.EPUB_NOTE_REF_OPEN + "1" + ep.EPUB_NOTE_REF_CLOSE}
        out = tmp_path / "o.epub"
        with redirect_stdout(io.StringIO()):
            p.rebuild_epub(doc, tr, str(out), notes={1: note}, language="tr")
        html = _read_member(out, "ch1.xhtml")
        assert "kc-nesne" not in html and "$x^{2}+1$" in html


# ---------------------------------------------------------------------------
# KARAR 18: kapsam raporunun dil denetimi
# ---------------------------------------------------------------------------

FR_PROSE = ("Le canon fut chargé avec une grande prudence et les membres du club "
            "attendaient dans le silence de la nuit que le projectile parte vers la lune.")


class TestK18CoverageLanguage:

    def _report(self, kept):
        return {"counted_ids": list(range(len(kept))), "kept_ids": list(range(len(kept))),
                "kept_src": {i: v for i, v in enumerate(kept)}}

    def test_kept_prose_counts_untranslated(self):
        kept = [(FR_PROSE, "para"),                  # kaynak dilde düzyazı
                ("Jules Verne", "para"),              # özel ad
                ("Gun-Club", "para"),                 # tek sözcük
                (FR_PROSE, "cell"),                   # tablo hücresi (QC muaf)
                ("The rest of this sentence is written in the English language "
                 "for the test.", "para")]           # başka dilde alıntı
        details = {}
        cov = eo._coverage_report(self._report(kept), set(), "fr",
                                  [{"type": "paragraph"}] * len(kept), details)
        assert cov == {"kapsam": 0.8, "cevrilmemis_blok": 1, "toplam_blok": 5}
        assert details["kaynak_dilde_aynen_kalan"] == 1

    def test_english_source_and_latin_quote(self):
        en = ("The committee decided that the experiment would be repeated in the "
              "spring because the results were not clear enough.")
        la = ("Omnia corpora quae sunt in motu semper sunt et ab eadem vi reguntur "
              "quae est in natura rerum.")
        cov = eo._coverage_report(self._report([(en, "para"), (la, "para")]), set(),
                                  "en", [{"type": "paragraph"}] * 2)
        assert cov["cevrilmemis_blok"] == 1

    def test_no_language_no_change(self):
        cov = eo._coverage_report(self._report([(FR_PROSE, "para")]), set(), None)
        assert cov["cevrilmemis_blok"] == 0

    def test_identity_translation_end_to_end(self, tmp_path, capsys, caplog):
        """Negatif kontrol: kimlik 'çevirisi' (kaynak TeX'in aynısı). Eskiden
        EPUB kapsamı %100 çıkıyordu (QC CRITICAL derken)."""
        paras = "".join(f"<p>{FR_PROSE} Paragraphe {i}.</p>" for i in range(30))
        src = _one_doc(tmp_path, f"<h1>Chapitre premier</h1>{paras}", lang="fr")
        p, doc = _parse(src)
        with redirect_stdout(io.StringIO()):
            body = _converter().convert(p.to_markdown(doc))
        out = tmp_path / "kitap"
        (out / "source").mkdir(parents=True)
        (out / "source" / "body.tex").write_text(body, encoding="utf-8")
        (out / "source" / "translated_body.tex").write_text(body, encoding="utf-8")
        import shutil
        shutil.copy(src, out / "source_book.epub")
        with caplog.at_level("INFO", logger="latex_pipeline.epub_output"), \
                redirect_stdout(io.StringIO()) as buf:
            res = eo.generate_epub_output(str(out / "source_book.epub"), str(out), "kitap")
        rep = json.loads((out / "compiled" / eo.EPUB_REPORT_NAME).read_text("utf-8"))
        assert res is not None
        assert rep["kapsam"] < 0.1 and rep["cevrilmemis_blok"] >= 30
        assert set(rep) == {"kapsam", "cevrilmemis_blok", "toplam_blok"}
        assert "dil denetimi kaynak dilde düzyazı buldu" in (
            buf.getvalue() + capsys.readouterr().out + caplog.text)


# ---------------------------------------------------------------------------
# extract_segments ↔ to_markdown tutarlılığı
# ---------------------------------------------------------------------------

def test_segments_markdown_consistency(tmp_path):
    """Her segmentin sözcükleri to_markdown'da aynı sırada geçer (tablolar,
    gevşek metin, formül ve kaçışlarla); markdown gövdesinde segmentte
    olmayan metin sözcüğü yoktur (başlık sayfası hariç)."""
    body = (TestF101LooseBlocks.BODY
            + "<table><tr><th>Kol A</th><th>Kol B</th></tr><tr><td>Hücre 1 *x*</td>"
            f"<td>Hücre 2 $5</td></tr></table><p>Energy <math {MATHML_NS}><mi>E</mi>"
            "</math> and C:\\Users 2*3.</p>")
    p, doc = _parse(_one_doc(tmp_path, body))
    md = p.to_markdown(doc)
    seg_words = [w for s in _segments(p, doc) for w in eo._segment_tokens(s["text"])]
    md_body = md.split("\n---\n", 1)[1]
    md_words = eo._segment_tokens(re.sub(r"(?m)^\|[-|]+\|$|^#+ ", " ", md_body))
    it = iter(md_words)
    assert all(w in it for w in seg_words)
    assert set(md_words) <= set(seg_words)


# ---------------------------------------------------------------------------
# Dönüştürücü sözleşmesi (C kulvarı): kaçışlı düz metin karakterleri
# ---------------------------------------------------------------------------

def _para_tex(md_line: str) -> str:
    with redirect_stdout(io.StringIO()):
        return _converter().convert(f"# K\n\n{md_line}\n")


def test_converter_contract_dollar():
    tex = _para_tex("Price \\$100 and \\$320 here.")
    assert "Price \\$100 and \\$320 here." in tex


# V1 denetimi (2026-09-29): sözleşme dönüştürücüde uygulandı (xfail kalktı).
# "\\]" -> "{]}": basılan "]"; 2_derle sanitizer'ının markdown bağ kalıntısı
# temizliği ("[metin](adres)" -> "metin") düz metni silmesin.
@pytest.mark.parametrize("md_text,tex_text", [
    ("2\\*3\\*4 and a\\*b", "2*3*4 and a*b"),
    ("\\_private\\_ and \\_\\_dunder\\_\\_", "\\_private\\_ and \\_\\_dunder\\_\\_"),
    ("C:\\\\Users\\\\data", "C:\\textbackslash{}Users\\textbackslash{}data"),
    ("[see here\\](not a link)", "[see here{]}(not a link)"),
    ("use \\`tick\\` here", "use `tick` here"),
])
def test_converter_contract_escapes(md_text, tex_text):
    assert tex_text in _para_tex(md_text)
