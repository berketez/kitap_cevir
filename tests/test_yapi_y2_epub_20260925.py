"""Y2 (2026-09-25) — EPUB okuma: sıra, başlık metni, çift başlık, başlık düzeyi.

Kapsam (parsers/epub_parser.py):
  Y2a  Belgeler spine (okuma) sırasıyla okunur; manifest sırası değil.
       Spine dışı belgeler (nav vb.) atılmaz, spine'dan sonra eklenir.
       rebuild_epub aynı sırayı ve aynı segment türetimini kullanır.
  Y2b  Başlıkta <br/> tek boşluk; başlık tek satır; iç etiket kenarındaki
       boşluk korunur ('Chapter 1.' + 'Title' yapışmaz). Paragrafta <br/>
       eskisi gibi satır sonu.
  Y2c  Bölüm başlığı markdown'a bir kez yazılır (eskiden '## X' + '# X').
  Y2d  Başlık düzeyi normalizasyonu: bölüm düzeyi '#'; kitap adı / yazar
       başlıkları (başlık sayfası) bölüm sayılmaz, düz paragraf olur.
  +    rebuild_epub: EPUB3 nav'dan gelen uid'siz içindekiler bağlantıları
       NCX yazarken çökmez.

Tüm metinler sentetiktir (kitap düzyazısı kopyalanmaz).
"""

import re
import sys
import zipfile
import warnings
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from parsers.epub_parser import EPUBParser  # noqa: E402

warnings.filterwarnings("ignore")


# ---------------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------------

def _make_epub(path, docs, spine=None, title="Sentetik Kitap",
               creators=("Ada Yazar",), language="en", extra_nonspine=()):
    """ebooklib ile EPUB yaz.

    docs: [(uid, gövde_html)] — MANIFEST sırası bu listenin sırasıdır.
    spine: uid listesi (okuma sırası); None ise docs sırası.
    extra_nonspine: [(uid, gövde_html)] — manifestte olup spine'da olmayan.
    """
    from ebooklib import epub

    book = epub.EpubBook()
    book.set_identifier("urn:uuid:y2-test")
    book.set_title(title)
    book.set_language(language)
    for c in creators:
        book.add_author(c)

    items = {}
    for uid, body in list(docs) + list(extra_nonspine):
        it = epub.EpubHtml(uid=uid, file_name=f"{uid}.xhtml", title=uid, lang=language)
        it.content = f"<html><body>{body}</body></html>"
        book.add_item(it)
        items[uid] = it

    order = spine if spine is not None else [u for u, _ in docs]
    book.toc = [epub.Link(f"{u}.xhtml", u, f"toc-{u}") for u in order]
    book.add_item(epub.EpubNcx())
    book.spine = [items[u] for u in order]
    epub.write_epub(str(path), book)
    return str(path)


def _parse(path):
    p = EPUBParser()
    return p, p.parse(path)


def _headings(md):
    """Markdown başlık satırları (converter gibi ``` kod bloklarını atlar)."""
    out, in_code = [], False
    for ln in md.splitlines():
        if ln.strip().startswith("```"):
            in_code = not in_code
            continue
        if not in_code and re.match(r"#{1,6} ", ln):
            out.append(ln)
    return out


# ---------------------------------------------------------------------------
# Y2a — spine sırası
# ---------------------------------------------------------------------------

class TestSpineOrder:

    def _epub(self, tmp_path):
        # Manifest: c3, c1, c2 — spine: c1, c2, c3
        docs = [
            ("c3", "<h1>Third Part</h1><p>Gamma body text here.</p>"),
            ("c1", "<h1>First Part</h1><p>Alpha body text here.</p>"),
            ("c2", "<h1>Second Part</h1><p>Beta body text here.</p>"),
        ]
        return _make_epub(tmp_path / "sira.epub", docs, spine=["c1", "c2", "c3"],
                          extra_nonspine=[("ek", "<p>Loose appendix text.</p>")])

    def test_chapters_follow_spine_not_manifest(self, tmp_path):
        _p, doc = _parse(self._epub(tmp_path))
        ids = [c.id for c in doc.chapters]
        assert ids[:3] == ["c1", "c2", "c3"]

    def test_non_spine_document_kept_after_spine(self, tmp_path):
        # Mevcut politika: spine dışı belge atılmaz; spine belgelerinden sonra.
        _p, doc = _parse(self._epub(tmp_path))
        ids = [c.id for c in doc.chapters]
        assert "ek" in ids
        assert ids.index("ek") > ids.index("c3")

    def test_markdown_and_segments_in_reading_order(self, tmp_path):
        p, doc = _parse(self._epub(tmp_path))
        md = p.to_markdown(doc)
        assert md.index("First Part") < md.index("Second Part") < md.index("Third Part")
        segs = p.extract_segments(doc)
        texts = [s["text"] for s in segs]
        assert texts.index("First Part") < texts.index("Second Part") < texts.index("Third Part")

    def test_rebuild_writes_segments_back_in_spine_order(self, tmp_path):
        src = self._epub(tmp_path)
        p, doc = _parse(src)
        segs = p.extract_segments(doc)
        tr = {s["index"]: f"QZ{s['index']}QZ" for s in segs}
        out = str(tmp_path / "geri.epub")
        p.rebuild_epub(doc, tr, out)
        p2, doc2 = _parse(out)
        got = [int(x) for s in p2.extract_segments(doc2)
               for x in re.findall(r"QZ(\d+)QZ", s["text"])]
        # Her segment kendi belgesine, sırasıyla yazılmış olmalı
        assert got == list(range(len(segs)))


# ---------------------------------------------------------------------------
# Y2b — başlıkta <br/>
# ---------------------------------------------------------------------------

class TestHeadingLineBreaks:

    def test_br_heading_is_one_line(self, tmp_path):
        docs = [("c1", "<h2>Opening</h2><p>Intro text.</p>"
                       "<h3>IV<br/>\n<br/>\nTHE HARBOUR</h3><p>Some body text.</p>"
                       "<h3>V<br/>\n<br/>\nTHE HILL</h3><p>More body text.</p>")]
        p, doc = _parse(_make_epub(tmp_path / "br.epub", docs))
        md = p.to_markdown(doc)
        assert "IV THE HARBOUR" in md
        # Eski davranış: '### IV' + ayrı 'THE HARBOUR' paragrafı
        assert not re.search(r"^#+ IV$", md, re.M)
        assert not re.search(r"^THE HARBOUR$", md, re.M)

    def test_br_between_inline_parts_does_not_glue(self, tmp_path):
        docs = [("c1", "<h1>Moon Route<br/><small>Direct Path<br/>in Two Days</small></h1>"
                       "<p>Body.</p><h1>Next</h1><p>Body two.</p>")]
        p, doc = _parse(_make_epub(tmp_path / "glue.epub", docs, title="Other"))
        # chapter.title ve markdown aynı, boşluklu metin
        assert doc.chapters[0].title == "Moon Route Direct Path in Two Days"
        assert "RouteDirect" not in p.to_markdown(doc)

    def test_inline_edge_space_kept_in_heading(self, tmp_path):
        docs = [("c1", '<h1><span class="label">Chapter 7. </span>Tides and Winds</h1>'
                       "<p>Body.</p><h1>Other</h1><p>Body.</p>")]
        p, doc = _parse(_make_epub(tmp_path / "edge.epub", docs, title="Other Book"))
        assert "Chapter 7. Tides and Winds" in p.to_markdown(doc)

    def test_multiline_text_node_in_heading_is_single_line(self, tmp_path):
        docs = [("c1", "<h2>Main</h2><p>x</p><h3>LONG HEADING THAT\nWRAPS IN SOURCE</h3><p>y</p>")]
        p, doc = _parse(_make_epub(tmp_path / "wrap.epub", docs))
        assert "LONG HEADING THAT WRAPS IN SOURCE" in p.to_markdown(doc)

    def test_paragraph_br_still_line_break(self, tmp_path):
        docs = [("c1", "<h1>Verse</h1><p>line one<br/>line two</p>")]
        p, doc = _parse(_make_epub(tmp_path / "p.epub", docs))
        assert "line one\nline two" in p.to_markdown(doc)


# ---------------------------------------------------------------------------
# Y2c — bölüm başlığı bir kez
# ---------------------------------------------------------------------------

class TestNoDuplicateChapterTitle:

    def test_each_chapter_title_once(self, tmp_path):
        docs = [
            ("c1", "<h1>Alpha Chapter</h1><p>Alpha text.</p>"),
            ("c2", "<h2>Beta Section</h2><p>Beta text.</p>"),
            ("c3", "<h1>Gamma Chapter</h1><p>Gamma text.</p>"),
        ]
        p, doc = _parse(_make_epub(tmp_path / "dup.epub", docs))
        md = p.to_markdown(doc)
        for t in ("Alpha Chapter", "Beta Section", "Gamma Chapter"):
            assert md.count(t) == 1, t

    def test_title_with_inline_markup_once(self, tmp_path):
        # Eskiden chapter.title 'TheBalladof X' (yapışık) içerikle eşleşmiyor,
        # hem markdown'da hem segmentlerde iki kez çıkıyordu.
        docs = [("c1", "<h1>Intro</h1><p>a</p>"),
                ("c2", "<h2>The <i>Ballad</i> of Sand</h2><p>Text.</p>")]
        p, doc = _parse(_make_epub(tmp_path / "ballad.epub", docs))
        md = p.to_markdown(doc)
        assert md.count("Ballad") == 1
        segs = p.extract_segments(doc)
        assert sum(1 for s in segs if "Ballad" in s["text"]) == 1

    def test_get_all_text_no_duplicate(self, tmp_path):
        docs = [("c1", "<h1>Only Once</h1><p>Body.</p>")]
        _p, doc = _parse(_make_epub(tmp_path / "all.epub", docs))
        assert doc.get_all_text().count("Only Once") == 1


# ---------------------------------------------------------------------------
# Y2d — başlık düzeyi normalizasyonu + başlık sayfası
# ---------------------------------------------------------------------------

class TestHeadingLevels:

    def test_single_file_title_h1_and_h3_chapters(self, tmp_path):
        # Gutenberg düzeni: kitap adı h1 (+ 'par' + yazar), bölümler h3
        docs = [("c1",
                 "<h1>Voyage au Port<br/><small>Récit Bref</small><br/>par<br/>Léa Martin</h1>"
                 "<p>Préface courte.</p>"
                 "<h3>I<br/>LE DÉPART</h3><p>Texte un.</p>"
                 "<h3>II<br/>LA MER</h3><p>Texte deux.</p>")]
        p, doc = _parse(_make_epub(tmp_path / "fr.epub", docs,
                                   title="Voyage au port: récit bref", creators=("Léa Martin",),
                                   language="fr"))
        md = p.to_markdown(doc)
        hs = _headings(md)
        # Kitap adı metadata'dan gelen tek '#'. 2026-09-28 güncellemesi
        # (karşılaştırma raporu R18, EPUB başlık sayfası: "büyük harf kaybı",
        # "başlık iki kez"): dc:title sözcükleri gövdedeki başlık sayfası
        # başlığının yazımıyla yazılır ('port' -> 'Port') ve o h1 gövdede
        # ikinci kez paragraf olarak YAZILMAZ; yazar satırı kitap adından
        # önce gelir. Ayrıntılı kilit: tests/test_kalite_E1_20260925.py
        # TestR18TitlePage.
        assert hs[0] == "# Voyage au Port: Récit Bref"
        assert "# I LE DÉPART" in hs and "# II LA MER" in hs
        assert not any("par Léa Martin" in h for h in hs)
        # İçerik kaybolmaz: kitap adı başlıkta, yazar adı başlıktan önceki satırda
        head = md.split("\n---\n", 1)[0]
        assert head.splitlines()[0] == "Léa Martin"
        assert "Voyage au Port Récit Bref par Léa Martin" not in md

    def test_h2_chapters_become_top_level(self, tmp_path):
        docs = [
            ("c1", "<h2>CAPÍTULO PRIMERO</h2><p>Texto uno.</p>"),
            ("c2", "<h2>CAPÍTULO II</h2><p>Texto dos.</p>"
                   "<h3><i>a</i>) PRIMERA PARTE</h3><p>Texto a.</p>"),
        ]
        p, doc = _parse(_make_epub(tmp_path / "es.epub", docs, title="Reglas breves",
                                   creators=("Juan Pérez",), language="es"))
        hs = _headings(p.to_markdown(doc))
        assert "# CAPÍTULO PRIMERO" in hs
        assert "# CAPÍTULO II" in hs
        assert "## *a*) PRIMERA PARTE" in hs

    def test_byline_heading_is_paragraph(self, tmp_path):
        # dc:creator 'G. V. Rossi' ~ başlık 'GIOVANNI ROSSI' (baş harf eşleşmesi)
        docs = [("c1", "<h1>GIOVANNI ROSSI</h1><h5>IL TEMA</h5><p>Testo.</p>"
                       "<h3>II.</h3><p>Testo due.</p>")]
        p, doc = _parse(_make_epub(tmp_path / "it.epub", docs, title="La Vita Altrove",
                                   creators=("G. V. Rossi",), language="it"))
        md = p.to_markdown(doc)
        hs = _headings(md)
        assert not any("ROSSI" in h for h in hs)
        assert re.search(r"^GIOVANNI ROSSI$", md, re.M)
        assert "# II." in hs

    def test_english_h1_h2_levels_unchanged(self, tmp_path):
        docs = [
            ("t", "<h1>Sentetik Kitap</h1><h2>Ada Yazar</h2>"),
            ("c1", "<h1>Contents</h1><p>List.</p>"),
            ("c2", "<h1>First Chapter</h1><p>Text.</p>"),
            ("c3", "<h2>A Section</h2><p>Text.</p><h3>A Sub</h3><p>Text.</p>"),
            ("c4", "<h1>Second Chapter</h1><p>Text.</p>"),
        ]
        p, doc = _parse(_make_epub(tmp_path / "en.epub", docs))
        hs = _headings(p.to_markdown(doc))
        assert "# First Chapter" in hs and "# Second Chapter" in hs
        assert "## A Section" in hs and "### A Sub" in hs
        assert "# Contents" in hs
        # Başlık sayfası (kitap adı + yazar) bölüm değil
        assert hs.count("# Sentetik Kitap") == 1  # yalnız metadata başlığı
        assert not any("Ada Yazar" in h for h in hs)

    def test_single_opening_top_heading_keeps_levels(self, tmp_path):
        # Metni açan TEK h1 her şeyi kapsar: h2'ler bölüme yükselmez
        docs = [("c1", "<h1>One Chapter Only</h1><p>t</p><h2>Part A</h2><p>t</p>"
                       "<h2>Part B</h2><p>t</p>")]
        p, doc = _parse(_make_epub(tmp_path / "one.epub", docs, title="Unrelated"))
        hs = _headings(p.to_markdown(doc))
        assert "# One Chapter Only" in hs
        assert "## Part A" in hs and "## Part B" in hs

    def test_single_trailing_outlier_does_not_set_level(self, tmp_path):
        # Sonda bir kez geçen h2 (lisans vb.) bölüm düzeyini belirlemez
        docs = [("c1", "<h3>I ONE</h3><p>t</p><h3>II TWO</h3><p>t</p><h3>III THREE</h3><p>t</p>"),
                ("c2", "<h2>END NOTICE</h2><p>t</p>")]
        p, doc = _parse(_make_epub(tmp_path / "tail.epub", docs, title="Unrelated"))
        hs = _headings(p.to_markdown(doc))
        assert "# I ONE" in hs and "# III THREE" in hs
        assert "# END NOTICE" in hs

    def test_rare_top_level_yields_to_dominant_chapters(self, tmp_path):
        # Gutenberg: h2 önsöz + h2 lisans (2 kez) ve h3 x 12 bölüm -> bölüm h3
        romans = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII"]
        chaps = "".join(f"<h3>{r} PART</h3><p>t</p>" for r in romans)
        docs = [("c1", "<h2>PREFACE</h2><p>t</p>" + chaps),
                ("c2", "<h2>LICENSE NOTICE</h2><p>t</p>")]
        p, doc = _parse(_make_epub(tmp_path / "pref.epub", docs, title="Unrelated"))
        hs = _headings(p.to_markdown(doc))
        assert "# I PART" in hs and "# XII PART" in hs
        assert "# PREFACE" in hs and "# LICENSE NOTICE" in hs

    def test_two_chapter_excerpt_keeps_h2_as_chapter(self, tmp_path):
        # Kesit: 2 x h2 bölüm, 3 x h3 alt başlık -> h2 bölüm kalır (oran < 5)
        docs = [("c1", "<h2>CHAPTER ONE</h2><p>t</p>"),
                ("c2", "<h2>CHAPTER SEVEN</h2><p>t</p><h3>a) X</h3><p>t</p>"
                       "<h3>b) Y</h3><p>t</p><h3>c) Z</h3><p>t</p>")]
        p, doc = _parse(_make_epub(tmp_path / "kesit.epub", docs, title="Unrelated"))
        hs = _headings(p.to_markdown(doc))
        assert "# CHAPTER ONE" in hs and "# CHAPTER SEVEN" in hs
        assert "## a) X" in hs

    def test_publisher_header_before_title_is_paragraph(self, tmp_path):
        docs = [("c1", "<h2>The Sample Archive eBook of Harbour Tales</h2><p>Notice.</p>"
                       "<h1>Harbour Tales</h1>"
                       "<h3>I FIRST</h3><p>t</p><h3>II SECOND</h3><p>t</p>")]
        p, doc = _parse(_make_epub(tmp_path / "pg.epub", docs, title="Harbour Tales",
                                   creators=("Mia Kaya",)))
        md = p.to_markdown(doc)
        hs = _headings(md)
        assert hs[0] == "# Harbour Tales"  # metadata başlığı
        assert not any("Sample Archive" in h for h in hs)
        assert "# I FIRST" in hs and "# II SECOND" in hs
        assert "The Sample Archive eBook of Harbour Tales" in md

    def test_title_like_heading_later_in_book_stays_heading(self, tmp_path):
        # Başlık sayfası kuralı yalnız ilk başlıklı belgede: sonraki belgedeki
        # kitap adıyla aynı bölüm başlığı başlık olarak kalır.
        docs = [("c1", "<h1>Preface</h1><p>t</p>"),
                ("c2", "<h1>Harbors</h1><p>t</p>")]
        p, doc = _parse(_make_epub(tmp_path / "late.epub", docs, title="Harbors"))
        hs = _headings(p.to_markdown(doc))
        assert hs.count("# Harbors") == 2  # metadata + gerçek bölüm

    def test_segments_keep_raw_levels(self, tmp_path):
        # extract_segments (Yol B / EPUB çıktı eşlemesi) EPUB'daki h düzeyini
        # korur; normalizasyon yalnız markdown'a uygulanır.
        docs = [("c1", "<h2>CHAP ONE</h2><p>t</p><h3>Sub</h3><p>t</p>"),
                ("c2", "<h2>CHAP TWO</h2><p>t</p>")]
        p, doc = _parse(_make_epub(tmp_path / "seg.epub", docs, title="Unrelated"))
        segs = p.extract_segments(doc)
        sub = next(s for s in segs if s["text"] == "Sub")
        assert sub["type"] == "heading" and sub["level"] == 3


class TestHeadingLikeParagraph:

    def test_scene_break_paragraph_not_heading(self, tmp_path):
        # '<p># # #</p>' sahne ayracı eskiden '# # #' satırı -> \chapter{\# \#}
        docs = [("c1", "<h1>Chapter A</h1><p>Before.</p><p># # #</p><p>After.</p>"
                       "<pre># a code comment</pre>")]
        p, doc = _parse(_make_epub(tmp_path / "scene.epub", docs, title="Unrelated"))
        md = p.to_markdown(doc)
        hs = _headings(md)
        assert hs == ["# Unrelated", "# Chapter A"]
        assert "\\# # #" in md
        assert "```\n# a code comment\n```" in md  # koda dokunulmaz
        # segmentler ham kalır (çeviri eşlemesi değişmez): eski davranış
        assert any(s["text"] == "# #" for s in p.extract_segments(doc))


# ---------------------------------------------------------------------------
# rebuild_epub eşlemesi + NCX uid
# ---------------------------------------------------------------------------

class TestRebuildParity:

    def test_br_heading_and_multi_paragraph_roundtrip(self, tmp_path):
        docs = [
            ("c1", "<h1>Head <i>One</i></h1><p>Plain text first.</p>"
                   "<h3>IX<br/>\n<br/>\nTHE GATE</h3>"
                   "<p>stanza one<br/>\n<br/>\nstanza two</p>"
                   "<p>Last text.</p>"),
            ("c2", "<h1>Head Two</h1><p>Second doc text.</p>"),
        ]
        src = _make_epub(tmp_path / "rt.epub", docs, title="Unrelated")
        p, doc = _parse(src)
        segs = p.extract_segments(doc)
        tr = {s["index"]: f"QZ{s['index']}QZ" for s in segs}
        out = str(tmp_path / "rt_out.epub")
        p.rebuild_epub(doc, tr, out)
        p2, doc2 = _parse(out)
        got = [int(x) for s in p2.extract_segments(doc2)
               for x in re.findall(r"QZ(\d+)QZ", s["text"])]
        assert got == list(range(len(segs)))

    def test_fill_missing_toc_uids(self):
        from ebooklib import epub
        toc = [epub.Link("a.xhtml", "A"),
               (epub.Section("S"), [epub.Link("b.xhtml", "B", "keep-me"),
                                    epub.Link("c.xhtml", "C")])]
        EPUBParser._fill_missing_toc_uids(toc)
        uids = [toc[0].uid, toc[1][1][0].uid, toc[1][1][1].uid]
        assert all(uids)
        assert uids[1] == "keep-me"
        assert len(set(uids)) == 3

    def test_rebuild_epub3_nav_toc_with_ncx_does_not_crash(self, tmp_path):
        # Gutenberg düzeni: EPUB3 nav + NCX. ebooklib içindekileri nav'dan
        # okur (Link uid=None); eskiden write_epub NCX yazarken TypeError ile
        # düşüyordu (tüm Gutenberg EPUB'larında EPUB çıktısı üretilemiyordu).
        path = tmp_path / "nav_ncx.epub"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
            zf.writestr("META-INF/container.xml",
                        '<?xml version="1.0"?><container version="1.0" '
                        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                        '<rootfile full-path="OEBPS/content.opf" '
                        'media-type="application/oebps-package+xml"/></rootfiles></container>')
            zf.writestr("OEBPS/content.opf",
                        '<?xml version="1.0" encoding="utf-8"?>'
                        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
                        'unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                        '<dc:identifier id="id">urn:uuid:nav-only</dc:identifier>'
                        '<dc:title>Nav Only</dc:title><dc:language>en</dc:language></metadata>'
                        '<manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" '
                        'properties="nav"/><item id="c1" href="c1.xhtml" '
                        'media-type="application/xhtml+xml"/><item id="ncx2" href="toc.ncx" '
                        'media-type="application/x-dtbncx+xml"/></manifest>'
                        '<spine toc="ncx2"><itemref idref="c1"/></spine></package>')
            zf.writestr("OEBPS/toc.ncx",
                        '<?xml version="1.0" encoding="utf-8"?>'
                        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
                        '<head><meta name="dtb:uid" content="urn:uuid:nav-only"/></head>'
                        '<docTitle><text>Nav Only</text></docTitle><navMap/></ncx>')
            zf.writestr("OEBPS/nav.xhtml",
                        '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" '
                        'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>t</title></head><body>'
                        '<nav epub:type="toc"><ol><li><a href="c1.xhtml">Chapter One</a></li></ol>'
                        '</nav></body></html>')
            zf.writestr("OEBPS/c1.xhtml",
                        '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml">'
                        '<head><title>c</title></head><body><h1>Chapter One</h1><p>Text.</p></body></html>')
        p, doc = _parse(str(path))
        segs = p.extract_segments(doc)
        out = str(tmp_path / "nav_out.epub")
        p.rebuild_epub(doc, {s["index"]: s["text"] for s in segs}, out)
        assert Path(out).exists()
