"""Kalite dalgası E2 (2026-09-28) — Türkçe EPUB çıkış tarafı.

latex_pipeline/epub_output.py ve parsers/epub_parser.py (rebuild_epub ve
yazma yardımcıları). Karşılaştırma raporu maddeleri:

  R2  TeX'te çevrilmiş paragraf Türkçe EPUB'da kaynak dilde kalıyordu:
      - çevirmen notu (\\footnote{Çevirmen notu: …}) paragrafın uzunluğunu
        şişirip hizalamayı (ve komşusunu) düşürüyordu (dil_it);
      - sanitizer'ın harf harf matematiğe çevirdiği Yunanca sözcük tek
        paragrafta 10 '$' farkı doğurup kısa komşuyu düşürüyordu (dil_fr);
      - <br/> ile iki segmente bölünen öğede tek parça çeviride aynen
        kaldıysa bütün öğe kaynak dilde kalıyordu (dil_it).
      Dipnotlar Türkçe EPUB'a not olarak taşınır: çevirmen notu belge sonuna
      <aside epub:type="footnote">, kaynak dipnotu (E1 sözleşmesi) EPUB'daki
      not öğesine. Kapsam sözleşmesi S7: compiled/epub_rapor.json.
  R7  Türkçe EPUB'un dili/metaverisi: html lang ve dc:language 'tr', kitap
      adı çeviriden, İçindekiler etiketleri Türkçe, bozuk (kesilmiş) özgün
      nav'daki girişler ve page-list korunur, kırık girişler atılır.
  R9  Satır içi biçim ve çapa kaybı: \\textit/\\textbf -> <em>/<strong>;
      başlıktaki <a id> çapası ve sayfa çapaları korunur; belge başlığındaki
      CSS bağlantıları korunur.
  C1  sonuçları: "Başlık — alt ad" iki öğeye bölünür, yalın etiket Türkçe
      etiket olur, numaralı paragrafın numarası geri konur.

Tüm metinler sentetiktir (kitap düzyazısı kopyalanmaz).
"""

import io
import json
import random
import re
import sys
import warnings
import zipfile
from contextlib import redirect_stdout
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from latex_pipeline import epub_output as eo  # noqa: E402
from parsers import epub_parser as ep  # noqa: E402
from parsers.epub_parser import EPUBParser  # noqa: E402

warnings.filterwarnings("ignore")


# ---------------------------------------------------------------------------
# Sentetik metin ve EPUB yardımcıları
# ---------------------------------------------------------------------------

_SRC = "abcdefghilmnoprstuvy"
_TR = "abcçdefgğhıijklmnoöprsştuüvyz"


def _words(rng, n, letters=_SRC):
    return " ".join("".join(rng.choice(letters) for _ in range(rng.randint(2, 9)))
                    for _ in range(n))


def _sentence(rng, n, letters=_SRC):
    return _words(rng, n, letters).capitalize() + "."


def _make_epub(path, docs, title="Sentetik Kitap", language="it", nav=False,
               css=True):
    """docs: [(uid, dosya_adı, gövde_html)]. css: belge başlığına stil
    bağlantısı; nav: EPUB3 nav belgesi eklenir."""
    from ebooklib import epub

    book = epub.EpubBook()
    book.set_identifier("urn:uuid:kalite-e2-test")
    book.set_title(title)
    book.set_language(language)
    book.add_author("Ada Yazar")
    style = epub.EpubItem(uid="st", file_name="stil.css", media_type="text/css",
                          content=b"p { margin: 0; }")
    book.add_item(style)
    items = []
    for uid, fname, body in docs:
        it = epub.EpubHtml(uid=uid, file_name=fname, title=uid, lang=language)
        it.content = (f'<html xmlns:epub="http://www.idpf.org/2007/ops">'
                      f"<body>{body}</body></html>")
        if css:
            it.add_link(href="stil.css", rel="stylesheet", type="text/css")
        book.add_item(it)
        items.append(it)
    book.toc = [epub.Link(it.file_name, it.title, it.id) for it in items]
    book.add_item(epub.EpubNcx())
    if nav:
        book.add_item(epub.EpubNav())
    book.spine = items
    epub.write_epub(str(path), book)
    return str(path)


def _replace_in_zip(path, name_suffix, content: bytes):
    """EPUB içindeki (adı name_suffix ile biten) dosyanın içeriğini değiştir."""
    src = Path(path)
    data = {}
    with zipfile.ZipFile(src) as z:
        names = z.namelist()
        for n in names:
            data[n] = z.read(n)
    target = next(n for n in names if n.endswith(name_suffix))
    data[target] = content
    with zipfile.ZipFile(src, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), data.pop("mimetype"),
                   compress_type=zipfile.ZIP_STORED)
        for n, b in data.items():
            z.writestr(n, b, compress_type=zipfile.ZIP_DEFLATED)


def _read(path, suffix) -> str:
    with zipfile.ZipFile(path) as z:
        name = next(n for n in z.namelist() if n.endswith(suffix))
        return z.read(name).decode("utf-8")


def _soup(path, suffix):
    from bs4 import BeautifulSoup
    return BeautifulSoup(_read(path, suffix), "lxml")


def _segments(epub_path):
    p = EPUBParser()
    with redirect_stdout(io.StringIO()):
        doc = p.parse(epub_path)
        segs = p.extract_segments(doc)
    return p, doc, segs


def _book_dir(tmp_path, epub_path, body, tr, name="kitap"):
    out = tmp_path / name
    (out / "source").mkdir(parents=True)
    (out / "source" / "body.tex").write_text(body, encoding="utf-8")
    (out / "source" / "translated_body.tex").write_text(tr, encoding="utf-8")
    import shutil
    shutil.copy(epub_path, out / "source_book.epub")
    return out


def _generate(out):
    with redirect_stdout(io.StringIO()):
        return eo.generate_epub_output(str(out / "source_book.epub"), str(out), "Kitap")


class _Book:
    """Sentetik kitap: her bölüm bir XHTML belgesi (h1 + paragraflar) ve
    aynı yapıda kaynak LaTeX gövdesi + sözde Türkçe çevirisi (uzunluk
    ~aynı, sözcükler farklı). paras[c][k] = (kaynak, çeviri)."""

    def __init__(self, seed=1, chapters=2, per=14, lo=14, hi=60):
        rng = random.Random(seed)
        self.rng = rng
        self.chapters = []
        for c in range(chapters):
            head = (_words(rng, 2).title(), _words(rng, 2, _TR).title())
            paras = []
            for _ in range(per):
                n = rng.randint(lo, hi)
                paras.append((_sentence(rng, n), _sentence(rng, n, _TR)))
            self.chapters.append((head, paras))

    def docs(self, extra=None):
        extra = extra or {}
        out = []
        for c, ((h, _ht), paras) in enumerate(self.chapters):
            body = f"<h1>{h}</h1>" + "".join(
                extra.get((c, k), f"<p>{s}</p>") for k, (s, _t) in enumerate(paras))
            out.append((f"c{c}", f"c{c}.xhtml", body))
        return out

    def tex(self, translated=False, repl=None):
        repl = repl or {}
        parts = []
        for c, ((h, ht), paras) in enumerate(self.chapters):
            parts.append(f"\\chapter{{{ht if translated else h}}}")
            for k, (s, t) in enumerate(paras):
                parts.append(repl.get((c, k), t if translated else s))
        return "\n\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# R2 — hizalama: çevirmen notu, harf harf matematik, kısa komşular
# ---------------------------------------------------------------------------

class TestUnitFootnotes:

    def test_translator_note_left_out_of_metrics(self):
        base = "Bir iki üç dört beş altı yedi sekiz dokuz on."
        note = "\\footnote{Çevirmen notu: " + ("uzun açıklama " * 30) + "}"
        a = eo._tex_units(base + "\n")[0]
        b = eo._tex_units(base + note + "\n")[0]
        assert (a.length, a.feats, a.tokens, a.numbers) == (b.length, b.feats, b.tokens, b.numbers)
        assert [(k, t[:13]) for _g, k, t in b.notes] == [(eo.NOTE_KIND_TRANSLATOR, "Çevirmen notu")]
        assert "uzun" not in b.text and eo._NOTE_PH_OPEN in b.text

    def test_source_footnote_counts_in_length_not_in_tokens(self):
        u = eo._tex_units("Ana metin burada\\footnote{Kaynak notu 12 sayfa.} sürer.\n")[0]
        assert [k for _g, k, _t in u.notes] == [eo.NOTE_KIND_SOURCE]
        assert "kaynak" not in u.tokens and "12" in u.numbers
        assert u.feats[eo._FOOTNOTE_FEATURE] == 1
        assert u.length > len("Ana metin burada sürer.")

    def test_placeholders_unique_across_units(self):
        tex = "A\\footnote{x} b.\n\nC\\footnote{y} d\\footnote{z}.\n"
        gids = [g for u in eo._tex_units(tex) for g, _k, _t in u.notes]
        assert gids == [0, 1, 2]

    def test_unbalanced_footnote_left_inline(self):
        u = eo._tex_units("Metin\\footnote{kapanmayan not\n")[0]
        assert u.notes == []

    def test_feature_mismatch_capped(self):
        # dil_fr: 'σελήνη' -> '$\\sigma$$\\epsilon$…' tek özellikte 10 fark
        a = eo._tex_units("Kısa not satırı burada.\n")[0]
        b = eo._tex_units("Kısa $\\sigma$$\\epsilon$$\\lambda$$\\nu$$\\eta$ satırı.\n")[0]
        side_a, side_b = eo._Side([a]), eo._Side([b])
        side_a.finalize({})
        side_b.finalize({})
        cost = eo._feature_cost(side_a.info[0], side_b.info[0], {})
        assert cost <= eo.FEATURE_MISMATCH_COST * eo.MAX_FEATURE_MISMATCH + 1e-9


class TestAlignmentRobustness:

    def _align(self, book, tr_repl, src_repl=None, docs_extra=None, tmp_path=None):
        path = _make_epub(tmp_path / "k.epub", book.docs(docs_extra))
        _p, _d, segs = _segments(path)
        src = book.tex(False, src_repl)
        tr = book.tex(True, tr_repl)
        return segs, eo.align_translations(segs, src, tr)

    def test_translator_notes_do_not_unalign(self, tmp_path):
        book = _Book(seed=3)
        rng = random.Random(9)
        repl = {}
        for c, k in ((0, 5), (0, 6), (1, 3)):
            t = book.chapters[c][1][k][1]
            repl[(c, k)] = t + "\\footnote{Çevirmen notu: " + _words(rng, 70, _TR) + ".}"
        segs, (tr, rep) = self._align(book, repl, tmp_path=tmp_path)
        assert rep["coverage"] == 1.0
        assert len(rep["notes"]) == 3
        marked = [t for t in tr.values() if ep.EPUB_NOTE_REF_OPEN in t]
        assert len(marked) == 3
        # Not metni paragrafa girmez
        assert all("Çevirmen notu" not in t for t in tr.values())
        assert all(v.startswith("Çevirmen notu:") for v in rep["notes"].values())

    def test_letterwise_math_does_not_unalign_short_neighbours(self, tmp_path):
        book = _Book(seed=4, per=16)
        (s, t) = book.chapters[0][1][7]
        short = ("[* Kısa kaynak notu burada.]", "[* Kısa çeviri notu burada.]")
        greek = "[* Kısa $\\sigma$$\\epsilon$$\\lambda$ἡ$\\nu$ $\\eta$ notu.]"
        book.chapters[0][1][7] = short
        book.chapters[0][1][8] = ("Kısa kaynak cümlesi sonra gelir.",
                                  "Kısa çeviri cümlesi sonra gelir.")
        segs, (tr, rep) = self._align(book, {(0, 7): greek}, tmp_path=tmp_path)
        assert rep["coverage"] == 1.0
        idx = next(i for i, sg in enumerate(segs) if "kaynak cümlesi" in sg["text"])
        assert tr[idx] == "Kısa çeviri cümlesi sonra gelir."


class TestMultiPieceElement:

    def test_kept_piece_does_not_block_element(self, tmp_path):
        # dil_it: '<p>Satır<br/>"Natura ed Arte"</p>' -> iki segment; ikincisi
        # çeviride aynen kaldı (None). Eskiden bütün öğe kaynak dilde kalırdı.
        path = _make_epub(tmp_path / "k.epub", [
            ("c0", "c0.xhtml", "<h1>Baslik</h1><p>Estratto dai fascicoli uno<br/>\n\n"
                               "\"Natura ed Arte\"</p><p>Altro paragrafo qui.</p>")])
        p, doc, segs = _segments(path)
        i = next(k for k, s in enumerate(segs) if s["text"].startswith("Estratto"))
        assert segs[i + 1]["text"] == '"Natura ed Arte"'
        out = tmp_path / "o.epub"
        stats = {}
        with redirect_stdout(io.StringIO()):
            p.rebuild_epub(doc, {i: "Şu dergiden alınmıştır"}, str(out), stats=stats)
        para = _soup(out, "c0.xhtml").find("p")
        assert str(para) == '<p>Şu dergiden alınmıştır<br/>"Natura ed Arte"</p>'
        assert stats["written"] == {i}


# ---------------------------------------------------------------------------
# R2 — dipnotlar Türkçe EPUB'a not olarak
# ---------------------------------------------------------------------------

def _footnote_book(tmp_path, gutenberg=False):
    """İki belge: gövde (iki dipnot işaretli paragraf + düz paragraflar) ve
    not belgesi. Kaynak LaTeX, E1/C2 sözleşmesindeki gibi \\footnote'u
    işaretin yerinde taşır (not belgesi ve not başlığı gövdede yok)."""
    rng = random.Random(21)
    paras = [(_sentence(rng, 30), _sentence(rng, 30, _TR)) for _ in range(12)]
    n1 = ("Birinci kaynak notu metni.", "Birinci notun çevirisi.")
    n2 = ("Ikinci kaynak notu metni.", "Ikinci notun çevirisi.")
    lead_s, lead_t = "Metin burada", "Burada metin"
    mid_s, mid_t = "devam eder ve ikinci not", "sürer ve ikinci not"
    if gutenberg:
        marked = f"<p>{lead_s}[1] {mid_s}[2] var.</p>"
        notes_body = (f"<h1>Notes</h1><p>[1 {n1[0]}]</p><p>[2 {n2[0]}]</p>")
    else:
        marked = (f'<p>{lead_s}<a id="FNanchor_1" href="c1.xhtml#Footnote_1" '
                  f'class="fnanchor">[1]</a> {mid_s}<a id="FNanchor_2" '
                  f'href="c1.xhtml#Footnote_2" class="fnanchor">[2]</a> var.</p>')
        notes_body = (
            '<h1>Notes</h1><div class="footnote"><p><a id="Footnote_1" '
            f'href="c0.xhtml#FNanchor_1"><span class="label">[1]</span></a> {n1[0]}</p></div>'
            '<div class="footnote"><p><a id="Footnote_2" href="c0.xhtml#FNanchor_2">'
            f'<span class="label">[2]</span></a> {n2[0]}</p></div>')
    body0 = "<h1>Primo</h1>" + marked + "".join(f"<p>{s}</p>" for s, _t in paras)
    path = _make_epub(tmp_path / "k.epub", [("c0", "c0.xhtml", body0),
                                            ("c1", "c1.xhtml", notes_body)])
    src = ("\\chapter{Primo}\n\n"
           f"{lead_s}\\footnote{{{n1[0]}}} {mid_s}\\footnote{{{n2[0]}}} var.\n\n"
           + "\n\n".join(s for s, _t in paras) + "\n")
    tr = ("\\chapter{Birinci}\n\n"
          f"{lead_t}\\footnote{{{n1[1]}}} {mid_t}\\footnote{{{n2[1]}}} var."
          "\\footnote{Çevirmen notu: ek açıklama.}\n\n"
          + "\n\n".join(t for _s, t in paras) + "\n")
    return path, src, tr


class TestFootnotesToNotes:

    def test_source_notes_go_to_note_elements(self, tmp_path):
        path, src, tr = _footnote_book(tmp_path)
        out = _book_dir(tmp_path, path, src, tr)
        res = _generate(out)
        assert res
        c0 = _soup(res, "c0.xhtml")
        para = c0.find("p")
        assert para.get_text().startswith("Burada metin[1] sürer ve ikinci not[2] var.")
        # Özgün işaret bağlantıları (id + adres) yerinde
        a1 = para.find("a", id="FNanchor_1")
        assert a1 is not None and a1["href"] == "c1.xhtml#Footnote_1"
        assert para.find("a", id="FNanchor_2") is not None
        # Çevirmen notu: işaret + belge sonunda aside
        ref = para.find("a", attrs={"epub:type": "noteref"})
        aside = c0.find("aside", attrs={"epub:type": "footnote"})
        assert ref is not None and aside is not None
        assert ref["href"] == "#" + aside["id"]
        assert "Çevirmen notu: ek açıklama." in aside.get_text()
        assert aside.find("a", href="#" + ref["id"]) is not None
        # Kaynak notlarının çevirisi not öğelerinde, geri dönüş çapası duruyor
        c1 = _soup(res, "c1.xhtml")
        notes = c1.find_all("p")
        assert "Birinci notun çevirisi." in notes[0].get_text()
        assert notes[0].find("a", id="Footnote_1") is not None
        assert notes[0].get_text().startswith("[1] ")
        assert "Ikinci notun çevirisi." in notes[1].get_text()
        # Not bölümü başlığı (dönüştürücüde yazılmayan) Türkçe
        assert c1.find("h1").get_text() == eo.EPUB_NOTES_HEADING_TR
        rep = json.loads((out / "compiled" / eo.EPUB_REPORT_NAME).read_text(encoding="utf-8"))
        assert rep == {"kapsam": 1.0, "cevrilmemis_blok": 0, "toplam_blok": rep["toplam_blok"]}

    def test_gutenberg_text_notes_keep_brackets(self, tmp_path):
        path, src, tr = _footnote_book(tmp_path, gutenberg=True)
        p, _doc, segs = _segments(path)
        keys = [s.get("note") for s in segs if s.get("note")]
        assert len(keys) == 2, "E1: Gutenberg '[N …]' notları dipnot olmalı"
        out = _book_dir(tmp_path, path, src, tr)
        res = _generate(out)
        c0 = _soup(res, "c0.xhtml")
        assert c0.find("p").get_text().startswith("Burada metin[1] sürer ve ikinci not[2] var.")
        c1 = [p.get_text() for p in _soup(res, "c1.xhtml").find_all("p")]
        assert c1[:2] == ["[1 Birinci notun çevirisi.]", "[2 Ikinci notun çevirisi.]"]


# ---------------------------------------------------------------------------
# S7 — kapsam raporu
# ---------------------------------------------------------------------------

class TestCoverageReport:

    def test_report_from_written_segments(self):
        rep = {"counted_ids": [0, 1, 2, 3, 4], "kept_ids": [4]}
        assert eo._coverage_report(rep, {0, 1}) == {
            "kapsam": 0.6, "cevrilmemis_blok": 2, "toplam_blok": 5}

    def test_written_and_read_by_derle(self, tmp_path, capsys):
        import importlib.util

        book = _Book(seed=5)
        path = _make_epub(tmp_path / "k.epub", book.docs())
        out = _book_dir(tmp_path, path, book.tex(), book.tex(True))
        started = __import__("time").time() - 1
        res = eo.generate_epub_output(str(out / "source_book.epub"), str(out), "Kitap")
        assert res
        data = json.loads((out / "compiled" / eo.EPUB_REPORT_NAME).read_text(encoding="utf-8"))
        assert set(data) == {"kapsam", "cevrilmemis_blok", "toplam_blok"}
        assert data["kapsam"] == 1.0 and data["cevrilmemis_blok"] == 0
        assert "Türkçe EPUB kapsamı %100,0" in capsys.readouterr().out
        spec = importlib.util.spec_from_file_location("derle_e2", PROJECT_ROOT / "2_derle.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod.EPUB_REPORT_NAME == eo.EPUB_REPORT_NAME
        assert mod._read_epub_report(out, since=started) == {
            "kapsam": 1.0, "cevrilmemis_blok": 0, "toplam_blok": data["toplam_blok"]}

    def test_skip_removes_stale_report(self, tmp_path):
        out = tmp_path / "k"
        (out / "compiled").mkdir(parents=True)
        stale = out / "compiled" / eo.EPUB_REPORT_NAME
        stale.write_text('{"kapsam": 1.0}', encoding="utf-8")
        eo._write_skip_note(out, "neden", out / "compiled" / "Kitap_TR.epub")
        assert not stale.exists()

    def test_partial_coverage_reported(self, tmp_path):
        # Çevirmen bir paragrafı atlamış: o paragraf kaynak dilde kalır,
        # rapora girer.
        book = _Book(seed=6, per=40)
        repl = {(0, 9): ""}
        path = _make_epub(tmp_path / "k.epub", book.docs())
        out = _book_dir(tmp_path, path, book.tex(), book.tex(True, repl))
        assert _generate(out)
        data = json.loads((out / "compiled" / eo.EPUB_REPORT_NAME).read_text(encoding="utf-8"))
        assert data["cevrilmemis_blok"] >= 1
        assert data["kapsam"] == round(1 - data["cevrilmemis_blok"] / data["toplam_blok"], 4)


# ---------------------------------------------------------------------------
# R7 — dil, kitap adı, İçindekiler
# ---------------------------------------------------------------------------

_TITLEPAGE = ("\\begin{titlepage}\n  \\centering\n  \\vspace*{3cm}\n"
              "  {\\Huge\\bfseries %s}\\par\n  \\vspace{1.5cm}\n  \\vfill\n"
              "\\end{titlepage}\n\n")


class TestLanguageAndMetadata:

    def test_lang_title_and_head(self, tmp_path):
        book = _Book(seed=7)
        path = _make_epub(tmp_path / "k.epub", book.docs(), title="Libro Sintetico", nav=True)
        out = _book_dir(tmp_path, path, _TITLEPAGE % "Libro Sintetico" + book.tex(),
                        _TITLEPAGE % "Sentetik Kitap Adı" + book.tex(True))
        res = _generate(out)
        c0 = _read(res, "c0.xhtml")
        assert 'lang="tr"' in c0 and 'xml:lang="tr"' in c0
        # CSS bağlantısı korunur (ebooklib şablonunda head boş kalıyordu)
        assert 'href="stil.css"' in c0
        opf = _read(res, ".opf")
        assert re.findall(r"<dc:language>([^<]*)</dc:language>", opf) == ["tr"]
        assert re.findall(r"<dc:title[^>]*>([^<]*)</dc:title>", opf) == ["Sentetik Kitap Adı"]
        nav = _soup(res, "nav.xhtml")
        assert nav.find("html")["lang"] == "tr"
        assert nav.find("nav").find("h2").get_text() == "İçindekiler"
        labels = [a.get_text() for a in nav.find("nav").find_all("a")]
        assert labels == [book.chapters[0][0][1], book.chapters[1][0][1]]

    def test_title_page_segments_placed(self, tmp_path):
        # E1 (R18): gövdedeki kitap adı/yazar başlıkları dönüştürücüde başlık
        # sayfasına taşınır (role 'title_page'); gövdede hizalanmazlar.
        book = _Book(seed=15)
        docs = book.docs()
        docs[0] = (docs[0][0], docs[0][1],
                   "<h1>Libro Sintetico</h1><h2>Ada Yazar</h2>" + docs[0][2])
        path = _make_epub(tmp_path / "k.epub", docs, title="Libro Sintetico")
        _p, _d, segs = _segments(path)
        assert [s.get("role") for s in segs[:2]] == ["title_page", "title_page"]
        page = ("\\begin{titlepage}\n  {\\Huge\\bfseries %s}\\par\n"
                "  {\\Large Ada Yazar}\\par\n\\end{titlepage}\n\n")
        tr, rep = eo.align_translations(segs, page % "Libro Sintetico" + book.tex(),
                                        page % "Sentetik Kitap" + book.tex(True))
        assert tr[0] == "Sentetik Kitap"
        assert 1 in rep["kept_ids"] and 1 not in tr
        assert rep["coverage"] == 1.0

    def test_translated_title_helper(self):
        assert eo._translated_book_title(_TITLEPAGE % "Ad \\& Soyad") == "Ad & Soyad"
        assert eo._translated_book_title("\\title{Başka}\n") == "Başka"
        assert eo._translated_book_title("metin") is None


_BROKEN_NAV = """<?xml version='1.0' encoding='UTF-8'?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="it">
<head><title>X</title></head>
<body>
<nav epub:type="toc" role="doc-toc" aria-label="Table of Contents">
<ol>
<li><a href="c0.xhtml#h0">PRIMO</a></li>
</ol></li>
</ol></li>
<li><a href="c1.xhtml#h1">SECONDO</a>
<ol>
<li><a href="c1.xhtml#s1">a) sezione</a></li>
</ol>
</li>
<li><a href="c9.xhtml#h9">MANCANTE</a></li>
<li><a href="c1.xhtml#yok">KIRIK</a></li>
</ol>
</nav>
<nav epub:type="landmarks" aria-label="Page List">
<ol id="pages" class="pagelist">
<li><a href="c0.xhtml#Page_1">p. 1</a></li>
<li><a href="c9.xhtml#Page_7">p. 7</a></li>
<li><a href="c1.xhtml#Page_2">p. 2</a></li>
</ol>
</nav>
</body>
</html>
"""


class TestNavigation:

    def _book(self, tmp_path):
        rng = random.Random(8)
        paras = [(_sentence(rng, 30), _sentence(rng, 30, _TR)) for _ in range(24)]
        sec_s, sec_t = "a) sezione", "a) kesim"
        body0 = ('<h1 id="h0">PRIMO</h1><p>Inizio<span class="pageno" id="Page_1"/>'
                 f' {paras[0][0][6:]}</p>' + "".join(f"<p>{s}</p>" for s, _t in paras[1:12]))
        body1 = ('<h1 id="h1">SECONDO</h1>'
                 + "".join(f"<p>{s}</p>" for s, _t in paras[12:18])
                 + f'<h2 id="s1"><i>a</i>) sezione</h2>'
                 + f'<p>Parte<span class="pageno" id="Page_2"/> {paras[18][0][6:]}</p>'
                 + "".join(f"<p>{s}</p>" for s, _t in paras[19:]))
        path = _make_epub(tmp_path / "k.epub", [("c0", "c0.xhtml", body0),
                                                ("c1", "c1.xhtml", body1)], nav=True)
        _replace_in_zip(path, "nav.xhtml", _BROKEN_NAV.encode("utf-8"))
        src = ("\\chapter{PRIMO}\n\n" + f"Inizio {paras[0][0][6:]}\n\n"
               + "\n\n".join(s for s, _t in paras[1:12]) + "\n\n\\chapter{SECONDO}\n\n"
               + "\n\n".join(s for s, _t in paras[12:18])
               + "\n\n\\section{\\textit{a}) sezione}\n\n"
               + f"Parte {paras[18][0][6:]}\n\n" + "\n\n".join(s for s, _t in paras[19:]) + "\n")
        tr = ("\\chapter{BİRİNCİ}\n\n" + f"Başlangıç {paras[0][1][6:]}\n\n"
              + "\n\n".join(t for _s, t in paras[1:12]) + "\n\n\\chapter{İKİNCİ}\n\n"
              + "\n\n".join(t for _s, t in paras[12:18])
              + "\n\n\\section{\\textit{a}) kesim}\n\n"
              + f"Kısım {paras[18][1][6:]}\n\n" + "\n\n".join(t for _s, t in paras[19:]) + "\n")
        return path, src, tr

    def test_broken_nav_rebuilt_with_turkish_labels(self, tmp_path):
        path, src, tr = self._book(tmp_path)
        out = _book_dir(tmp_path, path, src, tr)
        res = _generate(out)
        nav = _soup(res, "nav.xhtml")
        toc = nav.find("nav", attrs={"epub:type": "toc"})
        top = toc.find("ol").find_all("li", recursive=False)
        assert [li.find("a").get_text() for li in top] == ["BİRİNCİ", "İKİNCİ"]
        sub = top[1].find("ol").find_all("a")
        assert [a.get_text() for a in sub] == ["a) kesim"]
        # Kırık girişler (kitapta olmayan belge / kimlik) atıldı
        assert "MANCANTE" not in toc.get_text() and "KIRIK" not in toc.get_text()
        pages = nav.find("nav", attrs={"epub:type": "page-list"})
        assert [(a["href"], a.get_text()) for a in pages.find_all("a")] == [
            ("c0.xhtml#Page_1", "1"), ("c1.xhtml#Page_2", "2")]
        ncx = _read(res, ".ncx")
        assert "BİRİNCİ" in ncx and "a) kesim" in ncx and "MANCANTE" not in ncx

    def test_page_anchors_and_heading_markup_survive(self, tmp_path):
        path, src, tr = self._book(tmp_path)
        out = _book_dir(tmp_path, path, src, tr)
        res = _generate(out)
        c0, c1 = _soup(res, "c0.xhtml"), _soup(res, "c1.xhtml")
        assert c0.find(id="Page_1") is not None and c1.find(id="Page_2") is not None
        assert c0.find("p").get_text().startswith("Başlangıç")
        h2 = c1.find("h2")
        assert h2["id"] == "s1" and h2.find("em").get_text() == "a"


class TestDocumentShell:
    """Belge kabuğu: body öznitelikleri, metinsiz belge, EPUB2 (nav yok)."""

    def test_body_id_kept_and_ncx_entries_survive(self, tmp_path):
        # Calibre: içindekiler çapası <body id="…">'dedir; ebooklib şablonu
        # body'yi özniteliksiz yazıyordu -> NCX/iç bağlantılar kırılıyordu.
        book = _Book(seed=13)
        path = _make_epub(tmp_path / "k.epub", book.docs())
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
        for c in range(2):
            name = next(n for n in names if n.endswith(f"c{c}.xhtml"))
            with zipfile.ZipFile(path) as z:
                raw = z.read(name).decode("utf-8")
            raw = raw.replace("<body>", f'<body id="calibre_{c}" class="calibre">', 1)
            _replace_in_zip(path, f"c{c}.xhtml", raw.encode("utf-8"))
        ncx = _read(path, ".ncx")
        ncx = re.sub(r'src="(c\d)\.xhtml"', r'src="\1.xhtml#calibre_X\1"', ncx)
        ncx = ncx.replace("#calibre_Xc0", "#calibre_0").replace("#calibre_Xc1", "#calibre_1")
        _replace_in_zip(path, ".ncx", ncx.encode("utf-8"))
        out = _book_dir(tmp_path, path, book.tex(), book.tex(True))
        res = _generate(out)
        c1 = _read(res, "c1.xhtml")
        assert '<body id="calibre_1" class="calibre">' in c1
        tr_ncx = _read(res, ".ncx")
        assert re.findall(r'src="([^"]*)"', tr_ncx) == ["c0.xhtml#calibre_0", "c1.xhtml#calibre_1"]
        assert book.chapters[1][0][1] in tr_ncx

    def test_textless_document_written_as_is(self, tmp_path):
        book = _Book(seed=14)
        docs = book.docs() + [("kapak", "kapak.xhtml", '<div><svg xmlns="http://www.w3.org/2000/svg" '
                                                      'viewBox="0 0 10 10"><rect width="1" height="1"/></svg></div>')]
        path = _make_epub(tmp_path / "k.epub", docs)
        # ebooklib test EPUB'unu yazarken de küçük harfe indirir: kaynak
        # belge elle düzeltilir.
        raw = _read(path, "kapak.xhtml").replace("viewbox=", "viewBox=")
        _replace_in_zip(path, "kapak.xhtml", raw.encode("utf-8"))
        assert 'viewBox="0 0 10 10"' in _read(path, "kapak.xhtml")
        out = _book_dir(tmp_path, path, book.tex(), book.tex(True))
        res = _generate(out)
        kapak = _read(res, "kapak.xhtml")
        assert 'viewBox="0 0 10 10"' in kapak          # lxml.html küçük harfe indiriyordu
        assert 'href="stil.css"' in kapak
        assert 'lang="tr"' in kapak and 'xml:lang="tr"' in kapak
        assert 'lang="it"' not in kapak


# ---------------------------------------------------------------------------
# R9 — satır içi biçim ve çapalar
# ---------------------------------------------------------------------------

class TestInlineFormatting:

    def test_emphasis_markers(self):
        text = eo._tex_to_epub_text(
            "\\textit{eğik} ve \\textbf{kalın} ve \\emph{vurgu} x\\textsuperscript{2}",
            emphasis=True)
        assert text == (f"{ep.EPUB_EM_OPEN}eğik{ep.EPUB_EM_CLOSE} ve "
                        f"{ep.EPUB_STRONG_OPEN}kalın{ep.EPUB_STRONG_CLOSE} ve "
                        f"{ep.EPUB_EM_OPEN}vurgu{ep.EPUB_EM_CLOSE} x"
                        f"{ep.EPUB_SUP_OPEN}2{ep.EPUB_SUP_CLOSE}")
        # Varsayılan (emphasis=False) eskisi gibi düz metin
        assert eo._tex_to_epub_text("\\textit{eğik}") == "eğik"

    def test_markers_become_html(self):
        p = EPUBParser()
        html = p._markdown_to_inline_html(
            f"a {ep.EPUB_EM_OPEN}b{ep.EPUB_EM_CLOSE} {ep.EPUB_STRONG_OPEN}c"
            f"{ep.EPUB_STRONG_CLOSE} {ep.EPUB_EM_OPEN}yarım")
        assert html == "a <em>b</em> <strong>c</strong> yarım"

    def test_gutenberg_star_is_not_emphasis(self):
        p = EPUBParser()
        text = "haritalar[*] çizildi; [* Ay anlamına gelen not.]"
        assert p._markdown_to_inline_html(text) == text
        assert p._markdown_to_inline_html("bir *casus belli*!...") == \
            "bir <em>casus belli</em>!..."

    def test_bracketed_label_link(self):
        p = EPUBParser()
        assert p._markdown_to_inline_html("not[[1]](n.xhtml#f1) sürer") == \
            'not<a href="n.xhtml#f1">[1]</a> sürer'

    def test_heading_anchor_kept_and_links_work(self, tmp_path):
        # dil_fr: '<h3><a id="I"/>I<br/>LE CLUB</h3>'; td listesindeki #I
        # bağlantısı TR'de kırılıyordu (title_tag.clear()).
        body = ('<h1 id="t">Libro</h1><table><tr><td><a href="#I">I</a></td></tr></table>'
                '<h3 id="p1"><a id="I"/>I<br/>LE CLUB</h3><p>Primo testo qui.</p>')
        path = _make_epub(tmp_path / "k.epub", [("c0", "c0.xhtml", body)])
        p, doc, segs = _segments(path)
        i = next(k for k, s in enumerate(segs) if "CLUB" in s["text"])
        out = tmp_path / "o.epub"
        with redirect_stdout(io.StringIO()):
            p.rebuild_epub(doc, {i: "I KULÜP"}, str(out))
        h3 = _soup(out, "c0.xhtml").find("h3")
        assert h3.find("a", id="I") is not None
        assert " ".join(h3.get_text(" ").split()) == "I KULÜP"
        # Numaradan sonraki satır kırılımı da korunur
        assert [str(c) for c in h3.contents][:3] == ['<a id="I"></a>', "I", "<br/>"]

    def test_visible_page_marker_not_duplicated(self, tmp_path):
        # Gutenberg: '<span class="pagenum"><a id="Page_5">[5]</a></span>';
        # çeviri '[5]'i aynen taşıyorsa özgün çapa onun yerine konur.
        body = ('<h1>T</h1><p>Testo <span class="pagenum"><a id="Page_5">[5]</a>'
                '</span> segue qui ancora.</p>')
        path = _make_epub(tmp_path / "k.epub", [("c0", "c0.xhtml", body)])
        p, doc, segs = _segments(path)
        i = next(k for k, s in enumerate(segs) if "segue" in s["text"])
        out = tmp_path / "o.epub"
        with redirect_stdout(io.StringIO()):
            p.rebuild_epub(doc, {i: "Metin [5] burada sürer."}, str(out))
        para = _soup(out, "c0.xhtml").find("p")
        assert para.get_text() == "Metin [5] burada sürer."
        assert para.find("a", id="Page_5").get_text() == "[5]"

    def test_rebuilt_paragraph_keeps_inline_image(self, tmp_path):
        body = '<h1>T</h1><p>Testo <img src="x.png" alt="x"/> finale.</p>'
        path = _make_epub(tmp_path / "k.epub", [("c0", "c0.xhtml", body)])
        p, doc, segs = _segments(path)
        i = next(k for k, s in enumerate(segs) if "finale" in s["text"])
        out = tmp_path / "o.epub"
        with redirect_stdout(io.StringIO()):
            p.rebuild_epub(doc, {i: "Metin sonu."}, str(out))
        para = _soup(out, "c0.xhtml").find("p")
        assert para.find("img") is not None and "Metin sonu." in para.get_text()


# ---------------------------------------------------------------------------
# C1 sonuçları: alt ad, yalın etiket, numaralı paragraf
# ---------------------------------------------------------------------------

class TestConverterConsequences:

    def test_subtitle_split(self):
        from latex_pipeline.converter import _TITLE_JOIN_SEP as sep

        segs = [{"text": "Titolo", "type": "heading"},
                {"text": "Sottotitolo", "type": "paragraph", "role": "subtitle"}]
        assert eo._split_subtitle(segs, [0, 1], "h:chapter",
                                  f"Başlık{sep}Alt ad") == ("Başlık", "Alt ad")
        assert eo._split_subtitle(segs, [0, 1], "h:chapter", "Başlık") is None
        assert eo._split_subtitle(segs, [0, 1], "para", f"a{sep}b") is None

    def test_subtitle_split_end_to_end(self, tmp_path):
        from latex_pipeline.converter import _TITLE_JOIN_SEP as sep

        book = _Book(seed=12)
        docs = book.docs()
        h = book.chapters[1][0][0]
        docs[1] = (docs[1][0], docs[1][1], docs[1][2].replace(
            f"<h1>{h}</h1>", f'<h1>{h}</h1><p class="subh2">Bir alt ad burada</p>'))
        path = _make_epub(tmp_path / "k.epub", docs)
        _p, _d, segs = _segments(path)
        sub = next(i for i, s in enumerate(segs) if s.get("role") == "subtitle")
        src = book.tex().replace(f"\\chapter{{{h}}}", f"\\chapter{{{h}{sep}Bir alt ad burada}}")
        ht = book.chapters[1][0][1]
        tr = book.tex(True).replace(f"\\chapter{{{ht}}}", f"\\chapter{{{ht}{sep}Türkçe alt ad}}")
        t, rep = eo.align_translations(segs, src, tr)
        assert t[sub - 1] == ht and t[sub] == "Türkçe alt ad"

    def test_bare_label(self):
        assert eo._bare_label_tr("CAPÍTULO VII") == "Bölüm 7"
        assert eo._bare_label_tr("Zweites Kapitel") == "Bölüm 2"
        assert eo._bare_label_tr("Kapitel 1 Alpen") is None
        assert eo._bare_label_tr("Metin") is None

    def test_item_number_restored(self):
        assert eo._restore_item_number("1. Texto primero", "item", "İlk metin") == "1. İlk metin"
        assert eo._restore_item_number("2) Otro", "item", "2) Öteki") == "2) Öteki"
        assert eo._restore_item_number("- madde", "item", "madde") == "madde"
        assert eo._restore_item_number("1. Texto", "para", "Metin") == "Metin"
