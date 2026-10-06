"""E1 (2026-09-25) — EPUB içerik doğruluğu (parsers/epub_parser.py).

  E1a  İç içe içerik öğeleri bir kez yazılır. Eskiden blockquote '> metin' ve
       içindeki p 'metin' olarak iki kez; li>p, pre>code, p içindeki satır içi
       code da ayrıca yazılıyordu (LaTeX'te metin tekrar ediyordu).
       Bütün metni alt bloklarda olan blockquote/li kapsayıcıdır: alt blokları
       önekle ('> ', ilk blokta '- ') yazılır; gevşek metni olan öğe tek blok
       kalır (metin kaybolmaz). pre/code metni boşluk ve satır sonlarıyla
       korunur (eskiden 'Problemfrom typing'). rebuild_epub çeviriyi alt
       bloğa yazar, öneki ayıklar; yapı (blockquote>p, li>p) korunur.
  E1b  Spine dışı gezinme belgesi (EPUB3 nav, EPUB2 guide type="toc")
       gövdeye eklenmez; spine dışı başka belgeler ve spine içindeki
       içindekiler sayfası kalır.
  E1c  Paragrafta iç etiket kenarındaki boşluk korunur
       ('<i>Red Pill </i>dissection' -> '*Red Pill* dissection'); metni yalnız
       boşluk olan iç etiket boşluk olarak kalır ('</b><i> </i>for');
       satır başına/sonuna boşluk eklenmez; kaynakta boşluk yoksa eklenmez.

Tüm metinler sentetiktir (kitap düzyazısı kopyalanmaz).
"""

import re
import sys
import warnings
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from parsers.epub_parser import EPUBParser  # noqa: E402

warnings.filterwarnings("ignore")


# ---------------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------------

def _make_epub(path, docs, spine=None, nav=None, guide=(), extra_nonspine=(),
               title="Sentetik Kitap", creators=("Ada Yazar",), language="en"):
    """ebooklib ile EPUB yaz.

    docs: [(uid, gövde_html)] (spine sırası, *spine* verilmezse).
    nav: None | 'nonspine' | 'spine' — EPUB3 nav belgesi ekle.
    guide: [{'href','title','type'}] — EPUB2 guide girdileri.
    extra_nonspine: [(uid, gövde_html)] — manifestte olup spine'da olmayan.
    """
    from ebooklib import epub

    book = epub.EpubBook()
    book.set_identifier("urn:uuid:e1-test")
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
    book.toc = [epub.Link(f"{u}.xhtml", f"Toc entry {u}", f"toc-{u}") for u in order]
    book.add_item(epub.EpubNcx())
    spine_items = [items[u] for u in order]
    if nav:
        nav_item = epub.EpubNav()
        book.add_item(nav_item)
        if nav == "spine":
            spine_items = [nav_item] + spine_items
    book.spine = spine_items
    book.guide = list(guide)
    epub.write_epub(str(path), book)
    return str(path)


def _parse(path):
    p = EPUBParser()
    return p, p.parse(path)


def _body_paras(p, doc):
    """to_markdown gövdesinin paragrafları (üst bilgi hariç)."""
    md = p.to_markdown(doc)
    body = md.split("\n---\n", 1)[1]
    return p._split_paragraphs(body)


def _segments(p, doc):
    return [s["text"] for s in p.extract_segments(doc)]


def _roundtrip(tmp_path, src, make_tr):
    """Her segmente make_tr(segment) çevirisini yaz, çıktıyı geri oku."""
    p, doc = _parse(src)
    segs = p.extract_segments(doc)
    tr = {s["index"]: make_tr(s) for s in segs}
    out = str(tmp_path / "geri.epub")
    p.rebuild_epub(doc, tr, out)
    return segs, out


def _doc_html(path, uid):
    """Yeniden yazılmış EPUB'daki bir belgenin gövde HTML'i."""
    from ebooklib import epub
    from bs4 import BeautifulSoup

    book = epub.read_epub(path)
    item = book.get_item_with_id(uid)
    return BeautifulSoup(item.get_content().decode("utf-8"), "lxml")


def _count(haystack: str, needle: str) -> int:
    return haystack.count(needle)


# ---------------------------------------------------------------------------
# E1a — iç içe içerik bir kez yazılır
# ---------------------------------------------------------------------------

class TestNestedContentOnce:

    def _one(self, tmp_path, body, name="ic.epub"):
        src = _make_epub(tmp_path / name, [("c1", "<h1>Opening Part</h1>" + body)])
        p, doc = _parse(src)
        return p, doc, p.to_markdown(doc)

    def test_blockquote_p_written_once_with_quote_prefix(self, tmp_path):
        p, doc, md = self._one(
            tmp_path, "<blockquote><p>Quoted zephyr sentence.</p></blockquote>"
                      "<p>Plain follow-up text.</p>")
        assert _count(md, "Quoted zephyr sentence.") == 1
        assert "> Quoted zephyr sentence." in md
        assert _count(" ".join(_segments(p, doc)), "Quoted zephyr sentence.") == 1

    def test_blockquote_div_p_calibre_shape(self, tmp_path):
        # Calibre biçimi: blockquote > div > p
        p, doc, md = self._one(
            tmp_path, '<blockquote class="verse"><div class="x">'
                      '<p class="q">Nested verse quill line.</p></div></blockquote>')
        assert _count(md, "Nested verse quill line.") == 1
        assert "> Nested verse quill line." in md

    def test_blockquote_two_paragraphs_each_once(self, tmp_path):
        p, doc, md = self._one(
            tmp_path, "<blockquote><p>First quoted qualm.</p>"
                      "<p>Second quoted qualm.</p></blockquote>")
        paras = _body_paras(p, doc)
        assert "> First quoted qualm." in paras
        assert "> Second quoted qualm." in paras
        assert _count(md, "quoted qualm") == 2

    def test_blockquote_with_loose_text_stays_one_block(self, tmp_path):
        p, doc, md = self._one(
            tmp_path, "<blockquote>Loose quoted xylem text.</blockquote>")
        assert "> Loose quoted xylem text." in _body_paras(p, doc)

    def test_li_p_written_once_with_list_prefix(self, tmp_path):
        p, doc, md = self._one(
            tmp_path, "<ul><li><p>Should I jolt this?</p></li>"
                      "<li><p>How do I vex that?</p></li></ul>")
        paras = _body_paras(p, doc)
        assert "- Should I jolt this?" in paras
        assert "- How do I vex that?" in paras
        assert _count(md, "Should I jolt this?") == 1
        assert _count(md, "How do I vex that?") == 1

    def test_li_with_p_and_nested_list_all_items_once(self, tmp_path):
        p, doc, md = self._one(
            tmp_path, "<ul><li><p>Training data format:</p>"
                      "<ul><li>alpha kumquat item</li><li>beta kumquat item</li></ul>"
                      "</li></ul>")
        paras = _body_paras(p, doc)
        assert "- Training data format:" in paras
        assert "- alpha kumquat item" in paras
        assert "- beta kumquat item" in paras
        for t in ("Training data format:", "alpha kumquat item", "beta kumquat item"):
            assert _count(md, t) == 1

    def test_li_with_loose_text_and_nested_list_loses_nothing(self, tmp_path):
        # Gevşek metinli li kapsayıcı değildir: tek blok, iç liste metni dahil
        p, doc, md = self._one(
            tmp_path, "<ul><li>Intro quokka words<ul><li>inner quokka sub</li>"
                      "</ul></li></ul>")
        assert _count(md, "Intro quokka words") == 1
        assert _count(md, "inner quokka sub") == 1
        para = [x for x in _body_paras(p, doc) if "Intro quokka words" in x]
        assert para and para[0].startswith("- ") and "inner quokka sub" in para[0]

    def test_li_second_block_is_plain_paragraph(self, tmp_path):
        p, doc, md = self._one(
            tmp_path, "<ol><li><p>Term wombat.</p><p>Definition wombat text.</p>"
                      "</li></ol>")
        paras = _body_paras(p, doc)
        assert "- Term wombat." in paras
        assert "Definition wombat text." in paras

    def test_inline_code_in_paragraph_not_separate_block(self, tmp_path):
        p, doc, md = self._one(
            tmp_path, "<p>Call <code>gcd(15, 20)</code> to get five.</p>")
        assert "```" not in md
        assert _count(md, "gcd(15, 20)") == 1
        assert "Call gcd(15, 20) to get five." in md

    def test_pre_code_single_block(self, tmp_path):
        p, doc, md = self._one(
            tmp_path, "<pre><code>x = compute(1)\ny = x + 2</code></pre>")
        assert _count(md, "```") == 2
        assert _count(md, "x = compute(1)") == 1

    def test_pre_whitespace_preserved(self, tmp_path):
        # Eskiden get_text(strip=True): 'Problemfrom typing', 'deff'
        body = ('<pre><strong>Problem</strong>\n\nfrom typing import List\n\n'
                '<span class="k">def</span> <span class="n">f</span>(x):\n'
                '    return x</pre>')
        p, doc, md = self._one(tmp_path, body)
        assert "Problemfrom" not in md
        assert "Problem\n\nfrom typing import List" in md
        assert "def f(x):\n    return x" in md

    def test_top_level_li_and_paragraph_unchanged(self, tmp_path):
        p, doc, md = self._one(
            tmp_path, "<p>Ordinary paragraph ibex.</p><ul><li>Simple item ibex</li></ul>")
        paras = _body_paras(p, doc)
        assert "Ordinary paragraph ibex." in paras
        assert "- Simple item ibex" in paras

    def test_script_inside_li_does_not_change_decision(self, tmp_path):
        # parse script'i siler, rebuild silmez: kapsayıcı kararı aynı olmalı
        src = _make_epub(tmp_path / "scr.epub", [(
            "c1", "<h1>Script Part</h1><ul><li><script>var z=1;</script>"
                  "<p>Scripted walrus item.</p></li></ul>")])
        segs, out = _roundtrip(tmp_path, src, lambda s: f"QZ{s['index']}QZ")
        p2, d2 = _parse(out)
        got = [int(x) for t in _segments(p2, d2) for x in re.findall(r"QZ(\d+)QZ", t)]
        assert got == list(range(len(segs)))


class TestSegmentsMatchMarkdown:
    """extract_segments ile to_markdown paragrafları bire bir (E2 buna dayanır)."""

    def test_nested_structures_segments_equal_markdown_paragraphs(self, tmp_path):
        body = ("<h1>Consistency Part</h1>"
                "<blockquote><div><p>Quote yak one.</p><p>Quote yak two.</p></div></blockquote>"
                "<ul><li><p>List yak one.</p><ul><li>Sub yak.</li></ul></li>"
                "<li>Loose yak <em>item</em><ul><li>deep yak</li></ul></li></ul>"
                "<p>Inline <code>yak()</code> call.</p>"
                "<pre><code>yak = 1</code></pre>")
        src = _make_epub(tmp_path / "t.epub", [("c1", body)])
        p, doc = _parse(src)
        paras = [re.sub(r"^#+\s*", "", x) for x in _body_paras(p, doc)]
        segs = _segments(p, doc)
        assert segs == paras


class TestRebuildNested:

    def _src(self, tmp_path):
        body = ("<h1>Rebuild Part</h1>"
                "<blockquote><p>Quoted narwhal line.</p></blockquote>"
                "<ul><li><p>Listed narwhal item.</p><p>Second narwhal block.</p></li></ul>"
                "<p>Plain narwhal paragraph.</p>")
        return _make_epub(tmp_path / "rb.epub", [("c1", body)])

    def test_every_segment_written_back_once_in_order(self, tmp_path):
        segs, out = _roundtrip(tmp_path, self._src(tmp_path),
                               lambda s: f"QZ{s['index']}QZ")
        p2, d2 = _parse(out)
        got = [int(x) for t in _segments(p2, d2) for x in re.findall(r"QZ(\d+)QZ", t)]
        assert got == list(range(len(segs)))

    def test_structure_kept_and_prefix_not_leaked(self, tmp_path):
        # Çevirmen '> ' / '- ' önekini korumuş olsa da HTML'e sızmaz.
        def tr(s):
            m = re.match(r"(> |- )", s["text"])
            return (m.group(1) if m else "") + f"QZ{s['index']}QZ"

        segs, out = _roundtrip(tmp_path, self._src(tmp_path), tr)
        soup = _doc_html(out, "c1")
        bq_p = soup.select("blockquote > p")
        assert len(bq_p) == 1 and re.fullmatch(r"QZ\d+QZ", bq_p[0].get_text().strip())
        li_p = soup.select("li > p")
        assert len(li_p) == 2
        assert all(re.fullmatch(r"QZ\d+QZ", x.get_text().strip()) for x in li_p)
        text = soup.get_text()
        assert "> QZ" not in text and "- QZ" not in text

    def test_continuation_paragraph_keeps_leading_dash(self, tmp_path):
        # li'nin ikinci bloğu öneksizdir: çevirideki '-' metnin parçasıdır.
        src = self._src(tmp_path)
        p, doc = _parse(src)
        segs = p.extract_segments(doc)
        cont = [s for s in segs if s["text"] == "Second narwhal block."][0]
        tr = {s["index"]: s["text"] for s in segs}
        tr[cont["index"]] = "- dashed narwhal remark"
        out = str(tmp_path / "geri.epub")
        p.rebuild_epub(doc, tr, out)
        li_p = _doc_html(out, "c1").select("li > p")
        assert li_p[1].get_text().strip() == "- dashed narwhal remark"


# ---------------------------------------------------------------------------
# E1b — spine dışı nav/toc gövdeye girmez
# ---------------------------------------------------------------------------

class TestNavigationDocuments:

    DOCS = [("c1", "<h1>First Okapi Part</h1><p>Okapi alpha text.</p>"),
            ("c2", "<h1>Second Okapi Part</h1><p>Okapi beta text.</p>")]

    def test_nonspine_epub3_nav_not_in_body(self, tmp_path):
        src = _make_epub(tmp_path / "nav.epub", self.DOCS, nav="nonspine")
        p, doc = _parse(src)
        ids = [c.id for c in doc.chapters]
        assert ids == ["c1", "c2"]
        md = p.to_markdown(doc)
        assert "Toc entry" not in md
        assert ".xhtml" not in md

    def test_nav_in_spine_is_kept(self, tmp_path):
        # 2026-09-29 (F1-13, E kulvarı; bug avında doğrulandı): spine'daki
        # EPUB3 gezinme belgesi de gövde değildir (PDF'e kırık bağlantılı
        # '\chapter*{İçindekiler}' giriyordu). Spine'daki guide toc sayfası
        # (EPUB3 nav olmayan) kitabın parçası olarak kalır.
        src = _make_epub(tmp_path / "navsp.epub", self.DOCS, nav="spine")
        p, doc = _parse(src)
        md = p.to_markdown(doc)
        assert "Toc entry c1" not in md and ".xhtml" not in md
        assert [c.id for c in doc.chapters] == ["c1", "c2"]

    def test_nonspine_guide_toc_not_in_body(self, tmp_path):
        src = _make_epub(
            tmp_path / "guide.epub", self.DOCS,
            extra_nonspine=[("tocpage", '<h2>Contents</h2><p><a href="c1.xhtml">'
                                        'First Okapi Part</a></p>')],
            guide=[{"href": "tocpage.xhtml", "title": "Contents", "type": "toc"}])
        p, doc = _parse(src)
        assert [c.id for c in doc.chapters] == ["c1", "c2"]

    def test_other_nonspine_document_still_kept(self, tmp_path):
        src = _make_epub(tmp_path / "ek.epub", self.DOCS, nav="nonspine",
                         extra_nonspine=[("ek", "<p>Loose okapi appendix.</p>")])
        p, doc = _parse(src)
        assert [c.id for c in doc.chapters] == ["c1", "c2", "ek"]

    def test_rebuild_with_nav_consistent(self, tmp_path):
        src = _make_epub(tmp_path / "navrb.epub", self.DOCS, nav="nonspine")
        segs, out = _roundtrip(tmp_path, src, lambda s: f"QZ{s['index']}QZ")
        p2, d2 = _parse(out)
        got = [int(x) for t in _segments(p2, d2) for x in re.findall(r"QZ(\d+)QZ", t)]
        assert got == list(range(len(segs)))


# ---------------------------------------------------------------------------
# E1c — paragrafta satır içi etiket kenarındaki boşluk
# ---------------------------------------------------------------------------

class TestInlineEdgeSpace:

    @staticmethod
    def _p(html):
        from bs4 import BeautifulSoup
        el = BeautifulSoup(f"<p>{html}</p>", "lxml").p
        return EPUBParser()._inline_to_markdown(el)

    def test_trailing_space_inside_italic(self):
        assert self._p("<i>Red Pill </i>dissection") == "*Red Pill* dissection"

    def test_leading_space_inside_bold(self):
        assert self._p("word<b> bold</b> end") == "word **bold** end"

    def test_space_inside_span_kept(self):
        assert self._p('<span class="x">alpha </span>beta') == "alpha beta"

    def test_whitespace_only_tag_is_space(self):
        assert self._p("The <b>Fear</b><i> </i>for women") == "The **Fear** for women"
        # 2026-09-29 (F1-07): <sup> düz metne ezilmez, '<sup>..</sup>' kalır.
        assert self._p("x<sup>5</sup><sup> </sup>As a result") == "x<sup>5</sup> As a result"

    def test_no_space_invented_when_source_has_none(self):
        assert self._p("<i>spher</i>e") == "*spher*e"
        assert self._p("books<sup><a href='#n1'>14</a></sup> or") == "books[14](#n1) or"

    def test_no_leading_space_after_line_break(self):
        assert self._p('line one<br/><a href="#x"> link two</a>') == "line one\n[link two](#x)"

    def test_no_trailing_space_before_line_break(self):
        assert self._p("<i>first </i><br/>second") == "*first*\nsecond"

    def test_break_at_tag_edge_becomes_newline(self):
        assert self._p("<i>alpha<br/></i>beta") == "*alpha*\nbeta"

    def test_flattened_block_children_separated(self):
        # Gevşek metinli li tek blok kalır; iç liste öğeleri yapışmaz
        # (dizin girdisi: 'adapters<ul><li>finetuning' -> 'adaptersfinetuning').
        from bs4 import BeautifulSoup
        li = BeautifulSoup("<ul><li>adapters<ul><li>finetuning, <a href='#a'>x</a>"
                           "</li><li>merging</li></ul></li></ul>", "lxml").li
        assert EPUBParser()._inline_to_markdown(li) == "adapters finetuning, [x](#a) merging"

    def test_heading_mode_unchanged(self):
        from bs4 import BeautifulSoup
        p = EPUBParser()
        h = BeautifulSoup("<h2><span>I<br/></span>LE GUN-CLUB</h2>", "lxml").h2
        assert p._heading_markdown(h) == "I LE GUN-CLUB"
        h = BeautifulSoup("<h2><b>Chapter 1.</b> <i>Title</i></h2>", "lxml").h2
        assert p._heading_markdown(h) == "**Chapter 1.** *Title*"

    def test_in_book_markdown(self, tmp_path):
        src = _make_epub(tmp_path / "sp.epub", [(
            "c1", "<h1>Space Part</h1><p>This <i>Red Pill </i>dissection holds.</p>")])
        p, doc = _parse(src)
        assert "This *Red Pill* dissection holds." in p.to_markdown(doc)
