"""Kalite dalgası E1 (2026-09-25) — EPUB giriş tarafı.

parsers/epub_parser.py (to_markdown, ayrıştırma) ve
latex_pipeline/epub_extractor.py. Karşılaştırma raporu maddeleri:

  R8   Bölüm alt adı (p.subh2 vb.) başlık satırının hemen altına
       '<!-- kc:altad -->Alt ad' tek satır olarak yazılır (dönüştürücü
       birleştirir). Bütün paragraflarında aynı sınıfı kullanan kitapta
       (Calibre 'subtitle') alt ad sayılmaz.
  R11  Dipnot bağlantıları (fnanchor/noteref, EPUB3 aside, HTMLBook
       data-type) '[^kc-N]' işaretine, hedef metin bölüm sonunda tek satır
       '[^kc-N]: metin' tanımına dönüşür; hedefi kitapta olmayan (kesit)
       bağlantı ham '[[1]](…xhtml#Footnote_1)' yerine '<sup>1</sup>' olur
       (onaylı sözleşme: tanımı bulunamayan bağlantı '<sup>N</sup>').
  R12  Satır aşan <i>/<em>/<b>/bağlantı etiketi tek satır vurgu olur
       (eskiden '*…' ve '…*' iki satıra bölünüp PDF'e yıldızla giriyordu).
  R13  Gövde görselleri (<img>, <svg><image>) '![](dosya)' satırı olarak
       markdown'a girer; kapak ve satır içi simge girmez; dosyası
       çıkarılamayan/desteklenmeyen görsel atılır, GIF PNG'ye çevrilir.
  R18  Başlık sayfası: yazar satırları kitap adından önce (dönüştürücü
       başlık sayfası sözleşmesi), kitap adı gövdedeki başlığın büyük/küçük
       harf yazımıyla, başlık sayfası başlıkları gövdede ikinci kez yok.
  R20  Gutenberg '[*]' + '[* not]' ve '[N]' + '[N not]' notları aynı dipnot
       mekanizmasıyla.

Segment türetimi (extract_segments) değişmez: yalnız bilgi anahtarları
eklenir ("note", "note_refs", "role").

Tüm metinler sentetiktir (kitap düzyazısı kopyalanmaz).
"""

import io
import re
import sys
import warnings
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from parsers.epub_parser import EPUBParser, SUBTITLE_MARKER  # noqa: E402

warnings.filterwarnings("ignore")


# ---------------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------------

def _png_bytes(color=(200, 30, 30), size=(24, 16), fmt="PNG"):
    Image = pytest.importorskip("PIL.Image")
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, fmt)
    return buf.getvalue()


def _pattern_bytes(fmt="PNG", size=(40, 30)):
    """Desenli (iyi sıkışmayan) görsel. EPUBExtractor 100 bayttan küçük
    görselleri (boşluk/ayraç gif'leri) diske yazmaz; tek renkli küçük PNG
    bu sınırın altında kalır, diske çıkması beklenen görsel bununla yapılır."""
    Image = pytest.importorskip("PIL.Image")
    w, h = size
    im = Image.new("RGB", size)
    im.putdata([((x * 37 + y * 11) % 256, (x * 5 + y * 71) % 256, (x * y * 13) % 256)
                for y in range(h) for x in range(w)])
    buf = io.BytesIO()
    im.save(buf, fmt)
    data = buf.getvalue()
    assert len(data) >= 100
    return data


def _make_epub(path, docs, title="Sentetik Kitap", creators=("Ada Yazar",),
               language="en", images=(), cover=None, guide=()):
    """ebooklib ile EPUB yaz.

    docs: [(uid, dosya_adı, gövde_html)] spine sırasıyla.
    images: [(dosya_adı, bayt, mime)]; cover: (dosya_adı, bayt) kapak görseli.
    """
    from ebooklib import epub

    book = epub.EpubBook()
    book.set_identifier("urn:uuid:kalite-e1-test")
    book.set_title(title)
    book.set_language(language)
    for c in creators:
        book.add_author(c)
    if cover:
        book.set_cover(cover[0], cover[1], create_page=False)
    for name, data, mime in images:
        book.add_item(epub.EpubImage(uid=name.replace("/", "_"), file_name=name,
                                     media_type=mime, content=data))
    items = []
    for uid, fname, body in docs:
        it = epub.EpubHtml(uid=uid, file_name=fname, title=uid, lang=language)
        it.content = (f'<html xmlns:epub="http://www.idpf.org/2007/ops">'
                      f"<body>{body}</body></html>")
        book.add_item(it)
        items.append(it)
    book.toc = [epub.Link(it.file_name, it.title, it.id) for it in items]
    book.add_item(epub.EpubNcx())
    book.spine = items
    book.guide = list(guide)
    epub.write_epub(str(path), book)
    return str(path)


def _parse(path):
    p = EPUBParser()
    return p, p.parse(path)


def _md(path):
    p, doc = _parse(path)
    return p.to_markdown(doc)


def _segments(p, doc):
    import contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        return p.extract_segments(doc)


def _body(md):
    """Üst bilgiden ('---' satırına kadar) sonraki gövde."""
    return md.split("\n---\n", 1)[1]


# ---------------------------------------------------------------------------
# R12 — satır aşan vurgu tek parça
# ---------------------------------------------------------------------------

class TestR12MultilineEmphasis:

    def test_italic_across_source_lines_is_one_line(self, tmp_path):
        docs = [("c1", "c1.xhtml",
                 "<h1>Brume</h1><p>Il écrivit <i>Portvent,\n  7 octobre</i> en tête.</p>")]
        md = _md(_make_epub(tmp_path / "i.epub", docs, language="fr"))
        assert "*Portvent, 7 octobre*" in md
        assert not re.search(r"\*Portvent,\s*\n", md)

    def test_strong_and_link_label_across_lines(self, tmp_path):
        docs = [("c1", "c1.xhtml",
                 '<h1>Kavak</h1><p>Önce <b>kalın\nsöz</b> ve <a href="http://x.test/a">iki\n'
                 "satırlık etiket</a> gelir.</p>")]
        md = _md(_make_epub(tmp_path / "b.epub", docs, language="tr"))
        assert "**kalın söz**" in md
        assert "[iki satırlık etiket](http://x.test/a)" in md

    def test_br_inside_italic_splits_emphasis_per_line(self, tmp_path):
        docs = [("c1", "c1.xhtml", "<h1>Dize</h1><p><i>birinci dize<br/>ikinci dize</i></p>")]
        md = _md(_make_epub(tmp_path / "br.epub", docs))
        assert "*birinci dize*\n*ikinci dize*" in md

    def test_nbsp_inside_emphasis_kept(self, tmp_path):
        docs = [("c1", "c1.xhtml", "<h1>Boşluk</h1><p>Bir <i>on km yol</i> kaldı.</p>")]
        md = _md(_make_epub(tmp_path / "nb.epub", docs))
        assert "*on km yol*" in md

    def test_heading_italic_with_br_is_single_span(self, tmp_path):
        docs = [("c1", "c1.xhtml", "<h1>Açılış</h1><p>x</p><h2><i>Uzun<br/>Başlık</i></h2><p>y</p>")]
        md = _md(_make_epub(tmp_path / "h.epub", docs))
        assert "*Uzun Başlık*" in md

    def test_single_line_italic_unchanged(self, tmp_path):
        docs = [("c1", "c1.xhtml", "<h1>Sabit</h1><p>Bir <em>tek satır</em> vurgu.</p>")]
        md = _md(_make_epub(tmp_path / "s.epub", docs))
        assert "Bir *tek satır* vurgu." in md


# ---------------------------------------------------------------------------
# R13 — gövde görselleri
# ---------------------------------------------------------------------------

class TestR13BodyImages:

    def _epub(self, tmp_path, body_c1, body_c2="<h1>Ikinci</h1><p>Metin iki.</p>", extra_docs=()):
        png = _png_bytes()
        docs = [("c1", "text/c1.xhtml", body_c1)] + list(extra_docs) + \
               [("c2", "text/c2.xhtml", body_c2)]
        return _make_epub(tmp_path / "img.epub", docs,
                          images=[("images/sus.png", png, "image/png"),
                                  ("images/sekil.png", _png_bytes((10, 90, 10)), "image/png")])

    def test_block_image_written_in_reading_order(self, tmp_path):
        body = ("<h1>Birinci</h1><p>Önce metin.</p>"
                '<div class="figcenter"><img alt="Süs" src="../images/sekil.png"/></div>'
                "<p>Sonra metin.</p>")
        md = _md(self._epub(tmp_path, body))
        assert "![](images/sekil.png)" in md
        assert md.index("Önce metin.") < md.index("![](images/sekil.png)") < md.index("Sonra metin.")
        # Alt metin açıklamadır, altyazı değil: markdown'a girmez
        assert "![Süs]" not in md

    def test_inline_image_inside_text_paragraph_not_written(self, tmp_path):
        # 2026-09-29 (F1-08, E kulvarı): metin bloğunun içindeki görsel artık
        # atılmaz (satır içi formül görseli PDF'e girmiyordu); satır ortasında
        # '![](iç/yol)' olarak yerinde yazılır, ayrı gövde görseli satırı olmaz.
        body = '<h1>Birinci</h1><p>Simge <img src="../images/sus.png"/> burada.</p>'
        md = _md(self._epub(tmp_path, body))
        assert "Simge ![](images/sus.png) burada." in md
        assert "\n![](images/sus.png)\n" not in md

    def test_image_before_chapter_heading_moves_after_heading_and_subtitle(self, tmp_path):
        body = ('<div><img src="../images/sus.png"/></div>'
                '<h2>BÖLÜM BİR</h2><p class="subh2">Alt adın kendisi.</p><p>Gövde.</p>'
                '<div><img src="../images/sus.png"/></div>'
                '<h2>BÖLÜM İKİ</h2><p>Gövde iki.</p>')
        md = _md(self._epub(tmp_path, body, body_c2="<h2>BÖLÜM ÜÇ</h2><p>Son.</p>"))
        first = md.index("# BÖLÜM BİR")
        img = md.index("![](images/sus.png)")
        assert first < md.index(SUBTITLE_MARKER + "Alt adın kendisi.") < img < md.index("Gövde.")

    def test_image_only_document_carried_to_next_chapter(self, tmp_path):
        extra = [("tam", "text/tam.xhtml", '<div><img src="../images/sekil.png"/></div>')]
        md = _md(self._epub(tmp_path, "<h1>Birinci</h1><p>Metin bir.</p>", extra_docs=extra))
        assert md.index("Metin bir.") < md.index("![](images/sekil.png)") < md.index("Ikinci")
        assert md.count("![](images/sekil.png)") == 1

    def test_image_only_document_before_first_chapter_not_lost(self, tmp_path):
        png = _png_bytes()
        docs = [("on", "text/on.xhtml", '<div><img src="../images/sekil.png"/></div>'),
                ("c1", "text/c1.xhtml", "<h1>Birinci</h1><p>Metin bir.</p>"),
                ("c2", "text/c2.xhtml", "<h1>Ikinci</h1><p>Metin iki.</p>")]
        md = _md(_make_epub(tmp_path / "on.epub", docs,
                            images=[("images/sekil.png", png, "image/png")]))
        assert md.count("![](images/sekil.png)") == 1
        assert md.index("![](images/sekil.png)") < md.index("Metin bir.")

    def test_cover_image_not_in_body(self, tmp_path):
        png = _png_bytes()
        docs = [("kapak", "kapak.xhtml", '<div><img src="kapak.png"/></div>'),
                ("c1", "c1.xhtml", '<h1>Bir</h1><p>Metin.</p>')]
        path = _make_epub(tmp_path / "kapak.epub", docs, cover=("kapak.png", png))
        assert "kapak.png" not in _md(path)

    def test_images_do_not_change_segments(self, tmp_path):
        body = ('<h1>Birinci</h1><div><img src="../images/sekil.png"/></div>'
                '<p>Metin bir.</p><p><img src="../images/sus.png"/></p>')
        p, doc = _parse(self._epub(tmp_path, body))
        texts = [s["text"] for s in _segments(p, doc)]
        assert not any("png" in t for t in texts)
        assert texts[:2] == ["Birinci", "Metin bir."]

    def test_extractor_maps_names_and_drops_unusable(self, tmp_path):
        from latex_pipeline.epub_extractor import EPUBExtractor

        gif = _pattern_bytes("GIF")
        svg = (b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
               b'<rect width="10" height="10" fill="red"/>' + b" " * 120 + b"</svg>")
        docs = [("c1", "text/c1.xhtml",
                 '<h1>Bir</h1><div><img src="../images/sekil.png"/></div><p>a</p>'
                 '<div><img src="../images/hareket.gif"/></div><p>b</p>'
                 '<div><img src="../images/cizim.svg"/></div><p>c</p>'
                 '<div><img src="../images/yok.png"/></div><p>d</p>'
                 '<div><img src="../images/ayrac.png"/></div><p>e</p>'
                 '<div><img src="../images/boş%20resim.png"/></div><p>f</p>')]
        path = _make_epub(tmp_path / "ex.epub", docs, images=[
            ("images/sekil.png", _pattern_bytes(), "image/png"),
            ("images/hareket.gif", gif, "image/gif"),
            ("images/cizim.svg", svg, "image/svg+xml"),
            # 100 bayttan küçük ayraç: diske çıkarılmaz, satırı da kalmaz
            ("images/ayrac.png", _png_bytes(), "image/png")])
        out = tmp_path / "out"
        res = EPUBExtractor().extract(path, out)
        md = res.markdown
        assert "![](sekil.png)" in md
        assert "![](hareket_gif.png)" in md
        assert (out / "source" / "images" / "hareket_gif.png").exists()
        assert "hareket_gif.png" in res.images  # sonuç listesi markdown'la tutarlı
        assert "cizim" not in md and "yok.png" not in md and "ayrac" not in md
        # Adında boşluk olan görsel de denetlenir (eksikse satırı kalmaz)
        assert "boş" not in md
        # Okuma sırası korunur
        assert md.index("![](sekil.png)") < md.index("\na\n") < md.index("![](hareket_gif.png)")
        # Yazılan her görselin dosyası diskte (eksik dosyaya \includegraphics yok)
        for name in re.findall(r"!\[\]\(([^)]+)\)", md):
            assert (out / "source" / "images" / name).exists(), name


# ---------------------------------------------------------------------------
# R11 — dipnot bağlantıları
# ---------------------------------------------------------------------------

def _defs(md):
    return dict(re.findall(r"(?m)^\[\^(kc-\d+)\]: (.*)$", md))


class TestR11LinkedFootnotes:

    def _gutenberg(self, tmp_path, with_notes=True):
        c1 = ('<h2>CAPÍTULO UNO</h2>'
              '<p>Frase con nota<a class="fnanchor pginternal" href="notas.xhtml#Footnote_1" '
              'id="FNanchor_1">[1]</a>; sigue el texto.</p>'
              '<p>Otra frase<a class="fnanchor" href="notas.xhtml#Footnote_2" id="FNanchor_2">[2]</a>.</p>')
        docs = [("c1", "c1.xhtml", c1), ("c2", "c2.xhtml", "<h2>CAPÍTULO DOS</h2><p>Sin notas.</p>")]
        if with_notes:
            notas = ('<h2>NOTAS</h2>'
                     '<div class="footnote"><p><a class="label" href="c1.xhtml#FNanchor_1" '
                     'id="Footnote_1">[1]</a> Primera nota con <i>cursiva\nlarga</i>.</p></div>'
                     '<div class="footnote"><p><a class="label" href="c1.xhtml#FNanchor_2" '
                     'id="Footnote_2">[2]</a> Segunda nota,</p><p>en dos párrafos.</p></div>')
            docs.append(("notas", "notas.xhtml", notas))
        return _make_epub(tmp_path / "g.epub", docs, language="es")

    def test_gutenberg_fnanchor_becomes_footnote(self, tmp_path):
        md = _md(self._gutenberg(tmp_path))
        body = _body(md)
        assert "nota[^kc-1]; sigue" in body
        assert "frase[^kc-2]." in body
        defs = _defs(md)
        assert defs["kc-1"] == "Primera nota con *cursiva larga*."
        # Çok paragraflı dipnot tek satır
        assert defs["kc-2"] == "Segunda nota, en dos párrafos."
        # Tanımlar işaretin bölümünün sonunda, bir sonraki bölümden önce
        assert body.index("[^kc-2]: ") < body.index("CAPÍTULO DOS")
        # Geri dönüş bağlantısı, not gövdesi ve boşalan NOTAS başlığı yok
        assert "FNanchor" not in md and "Footnote_" not in md
        assert "NOTAS" not in md
        assert md.count("Primera nota") == 1

    def test_dangling_anchor_keeps_label_only(self, tmp_path):
        # 2026-09-28: onaylı dipnot sözleşmesi — tanımı bulunamayan bağlantı
        # '<sup>N</sup>' (25 Eylül yarım kodu yalnız '[1]' yazıyordu).
        md = _md(self._gutenberg(tmp_path, with_notes=False))
        assert "nota<sup>1</sup>; sigue" in md
        assert "frase<sup>2</sup>." in md
        assert "xhtml#Footnote" not in md and "nota[1]" not in md
        assert "[^kc-" not in md

    def test_epub3_aside_noteref(self, tmp_path):
        body = ('<h1>Bir</h1><p>Cümle<a epub:type="noteref" href="#n1">1</a> devam.</p>'
                '<aside epub:type="footnote" id="n1"><p>Notun metni '
                '<a href="https://ornek.test/k">kaynak</a>.</p></aside><p>Son paragraf.</p>')
        md = _md(_make_epub(tmp_path / "a.epub", [("c1", "c1.xhtml", body)], language="tr"))
        assert "Cümle[^kc-1] devam." in md
        assert _defs(md)["kc-1"] == "Notun metni [kaynak](https://ornek.test/k)."
        assert md.index("Son paragraf.") < md.index("[^kc-1]: ")

    def test_htmlbook_datatype_footnote_backlink_removed(self, tmp_path):
        body = ('<h1>Bir</h1><p>Metin<sup><a data-type="noteref" id="r1-marker" '
                'href="c1.xhtml#r1">1</a></sup> sürer.</p>'
                '<div data-type="footnotes"><p data-type="footnote" id="r1"><sup>'
                '<a href="c1.xhtml#r1-marker">1</a></sup> Açıklama metni.</p></div>')
        md = _md(_make_epub(tmp_path / "o.epub", [("c1", "c1.xhtml", body)]))
        assert "Metin[^kc-1] sürer." in md
        assert _defs(md)["kc-1"] == "Açıklama metni."

    def test_plain_internal_link_untouched(self, tmp_path):
        body = ('<h1>Bir</h1><p>Bkz. <a href="#b3">3</a> ve <a href="#b3">üçüncü kısım</a>.</p>'
                '<h2 id="b3">Üç</h2><p>Metin.</p>')
        md = _md(_make_epub(tmp_path / "l.epub", [("c1", "c1.xhtml", body)]))
        assert "[3](#b3)" in md and "[üçüncü kısım](#b3)" in md
        assert "[^kc-" not in md

    def test_oversized_region_is_not_a_footnote(self, tmp_path):
        many = "".join(f"<p>Paragraf {i}.</p>" for i in range(12))
        body = (f'<h1>Bir</h1><p>Metin<a class="noteref" href="#fn1">1</a>.</p>'
                f'<div class="footnote" id="fn1">{many}</div>')
        md = _md(_make_epub(tmp_path / "big.epub", [("c1", "c1.xhtml", body)]))
        assert "[^kc-" not in md
        # Dipnot sayılamayan hedef = tanımı bulunamayan bağlantı (sözleşme)
        assert "Metin<sup>1</sup>." in md
        assert all(f"Paragraf {i}." in md for i in range(12))  # metin kaybolmaz

    @pytest.mark.parametrize("label,marker", [
        ("[3]", "3"), ("(4)", "4"), ("5.", "5"), ("*", "*"), ("†", "†"), ("b", "b"),
        ("[ 6 ]", "6")])
    def test_dangling_marker_forms(self, tmp_path, label, marker):
        body = (f'<h1>Bir</h1><p>Söz<a class="noteref" href="yok.xhtml#fn9">{label}</a> biter.</p>')
        md = _md(_make_epub(tmp_path / "d.epub", [("c1", "c1.xhtml", body)]))
        assert f"Söz<sup>{marker}</sup> biter." in md
        assert "yok.xhtml" not in md

    def test_empty_target_is_dangling(self, tmp_path):
        # Hedefte geri dönüş bağlantısından başka metin yok
        body = ('<h1>Bir</h1><p>Söz<a class="fnanchor" id="FNanchor_1" '
                'href="#Footnote_1">[1]</a> biter.</p>'
                '<div class="footnote"><p><a id="Footnote_1" href="#FNanchor_1">[1]</a></p></div>'
                '<p>Son.</p>')
        md = _md(_make_epub(tmp_path / "e.epub", [("c1", "c1.xhtml", body)]))
        assert "Söz<sup>1</sup> biter." in md
        assert "[^kc-" not in md
        # Geri dönüş bağlantısı ham '[[1]](#FNanchor_1)' kalmaz
        assert "FNanchor" not in md
        assert md.count("<sup>1</sup>") == 2

    def test_table_note_marker_and_backlink(self, tmp_path):
        # HTMLBook tablo notu: işaret ve not aynı blokta (tabloyu taşıyan
        # liste öğesi; gevşek hücre metni yüzünden li tek blok) — dipnota
        # dönüşmez, iki bağlantı da '<sup>a</sup>' olur, ham adres kalmaz.
        body = ('<h1>Bir</h1><ul><li><p>Önce.</p><table><caption>Tablo 1. Örnek.<sup>'
                '<a data-type="noteref" id="t1-marker" href="c1.xhtml#t1">a</a></sup></caption>'
                '<tr><td>x</td><td>y</td></tr>'
                '<tr class="footnotes"><td colspan="2"><p data-type="footnote" id="t1"><sup>'
                '<a href="c1.xhtml#t1-marker">a</a></sup> Tablo notu.</p></td></tr></table>'
                '</li></ul><p>Sonra.</p>')
        md = _md(_make_epub(tmp_path / "tb.epub", [("c1", "c1.xhtml", body)]))
        assert "t1-marker" not in md and "c1.xhtml#t1" not in md
        assert "Örnek.<sup>a</sup>" in md
        assert "<sup>a</sup> Tablo notu." in md
        assert "[^kc-" not in md

    def test_backlink_to_non_marker_anchor_untouched(self, tmp_path):
        # Hedefi dipnot işareti OLMAYAN işaret metinli iç bağlantı değişmez
        body = ('<h1>Bir</h1><p id="p7">Yedinci paragraf.</p>'
                '<p>Bkz. <a href="#p7">7</a>.</p>')
        md = _md(_make_epub(tmp_path / "nb.epub", [("c1", "c1.xhtml", body)]))
        assert "Bkz. [7](#p7)." in md

    def test_segments_unchanged_and_annotated(self, tmp_path):
        p, doc = _parse(self._gutenberg(tmp_path))
        segs = _segments(p, doc)
        # Segment metinleri kaynağın blokları (ham bağlantı dahil) — değişmez
        assert any("[[1]](notas.xhtml#Footnote_1)" in s["text"] for s in segs)
        notes = [s for s in segs if "note" in s]
        assert [s["note"] for s in notes] == ["kc-1", "kc-2", "kc-2"]
        refs = [s for s in segs if "note_refs" in s]
        assert [s["note_refs"] for s in refs] == [["kc-1"], ["kc-2"]]
        heading = [s for s in segs if s.get("role") == "notes_heading"]
        assert [s["text"] for s in heading] == ["NOTAS"]
        # Sayı: bölüm içeriği paragraflarıyla bire bir
        total = sum(len([x for x in p._split_paragraphs(c.content)]) for c in doc.chapters)
        assert len(segs) <= total + len(doc.chapters)


# ---------------------------------------------------------------------------
# R20 — Gutenberg düz metin notları
# ---------------------------------------------------------------------------

class TestR20GutenbergPlainNotes:

    def test_star_note_next_paragraph(self, tmp_path):
        body = ("<h1>Bir</h1><p>Haritalar[*] çok iyidir.</p>"
                "<p>[* Yunanca bir\nsözcükten.]</p><p>Devam eden metin.</p>")
        md = _md(_make_epub(tmp_path / "s.epub", [("c1", "c1.xhtml", body)]))
        assert "Haritalar[^kc-1] çok iyidir." in md
        assert _defs(md)["kc-1"] == "Yunanca bir sözcükten."
        assert "[* Yunanca" not in md

    def test_numbered_notes_at_book_end(self, tmp_path):
        c1 = "<h1>Bir</h1><p>Birinci işaret[1] burada.</p><p>İkinci[2] burada.</p>"
        c2 = ("<h1>İki</h1><p>Son metin.</p><h3>NOTE:</h3>"
              "<p>[1 Birinci notun metni.]</p><p>[2 İkinci\nnotun metni.]</p>")
        md = _md(_make_epub(tmp_path / "n.epub", [("c1", "c1.xhtml", c1), ("c2", "c2.xhtml", c2)]))
        assert "Birinci işaret[^kc-1] burada." in md and "İkinci[^kc-2] burada." in md
        defs = _defs(md)
        assert defs == {"kc-1": "Birinci notun metni.", "kc-2": "İkinci notun metni."}
        assert "NOTE:" not in md

    def test_multi_paragraph_numbered_note(self, tmp_path):
        # '[8 ...' bir paragrafta açılıp üç paragraf sonra ']' ile kapanan not
        c1 = "<h1>Bir</h1><p>Mevsimler[8] farklıdır.</p><p>Ara metin.</p>"
        c2 = ("<h1>İki</h1><p>Son metin.</p><h3>NOTE:</h3>"
              "<p>[8 Uzun notun ilk\nparagrafı:</p><p>Satır A 1 2</p><p>Satır B 3 4</p>"
              "<p>Notun son cümlesi.]</p><p>[9 Tek paragraflı sahipsiz not.]</p>")
        md = _md(_make_epub(tmp_path / "mp.epub", [("c1", "c1.xhtml", c1), ("c2", "c2.xhtml", c2)]))
        assert "Mevsimler[^kc-1] farklıdır." in md
        assert _defs(md) == {"kc-1": "Uzun notun ilk paragrafı: Satır A 1 2 Satır B 3 4 "
                                     "Notun son cümlesi."}
        assert "Satır A 1 2\n" not in md.split("[^kc-1]: ")[0]
        # İşareti olmayan not yerinde kalır, bu yüzden NOTE: başlığı da kalır
        assert "[9 Tek paragraflı sahipsiz not.]" in md and "NOTE:" in md

    def test_multi_paragraph_star_note_and_segments(self, tmp_path):
        body = ("<h1>Bir</h1><p>Haritalar[*] iyidir.</p>"
                "<p>[* Birinci parça</p><p>ikinci parça.]</p><p>Devam.</p>")
        p, doc = _parse(_make_epub(tmp_path / "ms.epub", [("c1", "c1.xhtml", body)]))
        md = p.to_markdown(doc)
        assert _defs(md) == {"kc-1": "Birinci parça ikinci parça."}
        assert "Haritalar[^kc-1] iyidir." in md and "[* Birinci" not in md
        segs = _segments(p, doc)
        # 2026-09-29 (F1-09): düz metindeki '*' markdown'da kaçışlıdır.
        assert [s["text"] for s in segs if s.get("note") == "kc-1"] == \
            ["[\\* Birinci parça", "ikinci parça.]"]

    def test_unclosed_or_interrupted_note_not_taken(self, tmp_path):
        # Kapanmayan ('[4 ...' sonrasında ']' yok) ve arada yeni not açılan
        # dizi dipnot sayılmaz; metin yerinde kalır.
        body = ("<h1>Bir</h1><p>Bir[4] iki[5] üç.</p>"
                "<p>[4 Kapanmayan not</p><p>[5 Başka not</p><p>kapanış.]</p>"
                "<p>Düz paragraf.</p>")
        md = _md(_make_epub(tmp_path / "uc.epub", [("c1", "c1.xhtml", body)]))
        assert "Bir[4] iki[^kc-1] üç." in md
        assert "[4 Kapanmayan not" in md
        assert _defs(md) == {"kc-1": "Başka not kapanış."}

    def test_marker_without_note_unchanged(self, tmp_path):
        body = "<h1>Bir</h1><p>Kesitte notu olmayan işaret[3] kalır.</p>"
        md = _md(_make_epub(tmp_path / "k.epub", [("c1", "c1.xhtml", body)]))
        assert "işaret[3] kalır." in md and "[^kc-" not in md

    def test_link_label_is_not_a_plain_marker(self, tmp_path):
        body = ('<h1>Bir</h1><p>Bağlantı<a href="http://x.test/">[4]</a> var.</p>'
                "<p>[4 Bu bir not değil çünkü işaret bağlantı.]</p>")
        md = _md(_make_epub(tmp_path / "lk.epub", [("c1", "c1.xhtml", body)]))
        assert "[[4]](http://x.test/)" in md
        assert "[^kc-" not in md


# ---------------------------------------------------------------------------
# R8 — bölüm alt adı
# ---------------------------------------------------------------------------

class TestR8Subtitle:

    def test_subh2_after_chapter_heading(self, tmp_path):
        docs = [("c1", "c1.xhtml", '<h2>CAPÍTULO I</h2><p class="subh2j">Sobre los\n'
                                    '  métodos generales.</p><p>Texto.</p>'),
                ("c2", "c2.xhtml", '<h2>CAPÍTULO II</h2><p class="subh2">Marcha breve.</p><p>Más.</p>')]
        md = _md(_make_epub(tmp_path / "sub.epub", docs, language="es"))
        assert "# CAPÍTULO I\n" + SUBTITLE_MARKER + "Sobre los métodos generales.\n" in md
        assert "# CAPÍTULO II\n" + SUBTITLE_MARKER + "Marcha breve." in md
        assert md.count("Marcha breve.") == 1

    def test_class_used_on_all_paragraphs_is_not_subtitle(self, tmp_path):
        body = ('<h1>Bir</h1><p class="subtitle">İlk paragraf.</p>'
                + "".join(f'<p class="subtitle">Paragraf {i}.</p>' for i in range(6))
                + '<h1>İki</h1><p class="subtitle">Yine paragraf.</p>')
        md = _md(_make_epub(tmp_path / "cal.epub", [("c1", "c1.xhtml", body)]))
        assert SUBTITLE_MARKER not in md

    def test_epub_type_subtitle_and_length_limit(self, tmp_path):
        long_text = "uzun " * 80
        body = ('<h1>Bir</h1><p epub:type="subtitle">Kısa alt ad</p><p>x</p>'
                f'<h1>İki</h1><p epub:type="subtitle">{long_text}</p><p>y</p>')
        md = _md(_make_epub(tmp_path / "et.epub", [("c1", "c1.xhtml", body)]))
        assert md.count(SUBTITLE_MARKER) == 1
        assert SUBTITLE_MARKER + "Kısa alt ad" in md

    def test_subtitle_segment_role(self, tmp_path):
        docs = [("c1", "c1.xhtml", '<h2>CAPÍTULO I</h2><p class="subh2">Alt.</p><p>T.</p>'),
                ("c2", "c2.xhtml", '<h2>CAPÍTULO II</h2><p class="subh2">Alt iki.</p><p>U.</p>')]
        p, doc = _parse(_make_epub(tmp_path / "r.epub", docs))
        roles = {s["text"]: s.get("role") for s in _segments(p, doc)}
        assert roles["Alt."] == "subtitle" and roles["T."] is None


# ---------------------------------------------------------------------------
# R18 — başlık sayfası
# ---------------------------------------------------------------------------

class TestR18TitlePage:

    def test_recased_title_author_first_no_duplicate(self, tmp_path):
        docs = [("c1", "c1.xhtml",
                 "<h1>Voyage au Port<br/><small>Récit Bref</small><br/>par<br/>Léa Martin</h1>"
                 "<p>Préface courte.</p><h3>I<br/>LE DÉPART</h3><p>Texte un.</p>"
                 "<h3>II<br/>LA MER</h3><p>Texte deux.</p>")]
        md = _md(_make_epub(tmp_path / "t.epub", docs, title="Voyage au port: récit bref",
                            creators=("Léa Martin",), language="fr"))
        head = md.split("\n---\n", 1)[0]
        assert head.splitlines()[0] == "Léa Martin"
        assert "# Voyage au Port: Récit Bref" in head
        assert "Voyage au Port Récit Bref par Léa Martin" not in md
        assert "*Léa Martin*" not in md
        assert "# I LE DÉPART" in md and "Préface courte." in md

    def test_all_caps_heading_keeps_metadata_case_and_byline_used(self, tmp_path):
        docs = [("c1", "c1.xhtml",
                 "<h1>IL MARE ROSSO</h1><h1>GIOVANNA ROSSI</h1><p>Testo.</p>"
                 "<h2>Uno</h2><p>a</p><h2>Due</h2><p>b</p>")]
        md = _md(_make_epub(tmp_path / "c.epub", docs, title="Il Mare Rosso",
                            creators=("G. Rossi",), language="it"))
        head = md.split("\n---\n", 1)[0]
        assert "# Il Mare Rosso" in head
        assert head.splitlines()[0] == "GIOVANNA ROSSI"
        assert "GIOVANNA ROSSI" not in _body(md)
        assert "IL MARE ROSSO" not in _body(md)

    def test_byline_in_split_title_document_consumed(self, tmp_path):
        docs = [("t1", "t1.xhtml", "<h1>The Quiet Harbor</h1><h2>Volume II - Tides</h2>"),
                ("t2", "t2.xhtml", "<h2>Mara Quill</h2>"),
                ("c1", "c1.xhtml", "<p>Copyright line.</p><h1>One</h1><p>a</p><h1>Two</h1><p>b</p>")]
        md = _md(_make_epub(tmp_path / "sp.epub", docs, title="The Quiet Harbor - Tides",
                            creators=("Mara Quill",)))
        assert md.count("Mara Quill") == 1
        assert md.index("Mara Quill") < md.index("# The Quiet Harbor - Tides")

    def test_no_title_heading_uses_metadata_and_creators(self, tmp_path):
        docs = [("c1", "c1.xhtml", "<h2>Uno</h2><p>a</p><h2>Due</h2><p>b</p>")]
        md = _md(_make_epub(tmp_path / "m.epub", docs, title="Libro Semplice",
                            creators=("Primo Autore", "Seconda Autrice")))
        head = md.split("\n---\n", 1)[0]
        assert head.splitlines()[:2] == ["Primo Autore", "Seconda Autrice"]
        assert "# Libro Semplice" in head

    def test_title_page_segment_role(self, tmp_path):
        docs = [("c1", "c1.xhtml",
                 "<h1>Voyage au Port</h1><p>Préface.</p><h3>I</h3><p>a</p><h3>II</h3><p>b</p>")]
        p, doc = _parse(_make_epub(tmp_path / "tp.epub", docs, title="Voyage au port"))
        seg = _segments(p, doc)[0]
        assert seg["text"] == "Voyage au Port" and seg.get("role") == "title_page"


# ---------------------------------------------------------------------------
# Segment tutarlılığı (E2 hizalaması buna dayanır)
# ---------------------------------------------------------------------------

class TestSegmentConsistency:

    def test_rebuild_roundtrip_still_aligned_with_notes_and_images(self, tmp_path):
        png = _png_bytes()
        c1 = ('<h2>CAPÍTULO UNO</h2><p class="subh2">Alt ad.</p>'
              '<div><img src="sus.png"/></div>'
              '<p>Frase <i>larga\nen cursiva</i> con nota<a class="fnanchor" '
              'href="c1.xhtml#Footnote_1" id="FNanchor_1">[1]</a>.</p>'
              '<div class="footnote"><p><a href="c1.xhtml#FNanchor_1" id="Footnote_1">[1]</a>'
              ' Texto de la nota.</p></div>')
        path = _make_epub(tmp_path / "rt.epub", [("c1", "c1.xhtml", c1)],
                          images=[("sus.png", png, "image/png")])
        p, doc = _parse(path)
        segs = _segments(p, doc)
        tr = {s["index"]: f"QZ{s['index']}QZ" for s in segs}
        out = str(tmp_path / "geri.epub")
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            p.rebuild_epub(doc, tr, out)
        p2, d2 = _parse(out)
        got = [int(x) for s in _segments(p2, d2) for x in re.findall(r"QZ(\d+)QZ", s["text"])]
        assert got == list(range(len(segs)))
