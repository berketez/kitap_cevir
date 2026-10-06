"""E2 (2026-09-25) — Türkçe EPUB çıktısında çeviri doğru öğeye yazılır.

  E2a  latex_pipeline/epub_output.generate_epub_output çeviriyi segmentlere
       POZİSYONEL (i == i) eşliyordu. to_markdown'ın kitap adı/yazar bloğu
       segmentlerde yok (+3 kayma), dönüştürücü/sanitizer paragraf ekleyip
       birleştiriyor: işaretli sözde çeviride on kitabın hiçbirinde öğelerin
       %1'i bile doğru yere düşmüyordu. Yeni zincir içerik çapalıdır:
       A1 segment ↔ kaynak LaTeX birimi (aynı dil, sözcük dizisi), A2 kaynak
       birim ↔ çeviri birimi (Gale–Church uzunluk modeli + yapı özellikleri,
       ileri-geri DP, sonsal eşiği). Belirsiz paragraf kaynak dilde kalır;
       güvenle eşlenen oran eşiğin altındaysa EPUB üretilmez.
  E2b  Sözleşme S5: EPUB üretilmezse compiled/EPUB_ATLANDI.txt'ye tek satır
       Türkçe neden yazılır (önceki koşudan kalan Türkçe EPUB silinir); EPUB
       üretilirse not silinir. Çeviri henüz yoksa (çıkarım koşusu) hiçbir şey
       yazılmaz.

Tüm metinler sentetiktir (kitap düzyazısı kopyalanmaz).
"""

import random
import re
import sys
import warnings
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from latex_pipeline import epub_output as eo  # noqa: E402

warnings.filterwarnings("ignore")


# ---------------------------------------------------------------------------
# Sentetik metin ve sözde çeviri
# ---------------------------------------------------------------------------

_SRC_LETTERS = "abcdefghilmnoprstuvy"
_TR_LETTERS = "abcçdefgğhıijklmnoöprsştuüvyz"


def _words(rng, n):
    return " ".join("".join(rng.choice(_SRC_LETTERS) for _ in range(rng.randint(2, 9)))
                    for _ in range(n))


def _para(rng, tag, lo=12, hi=70):
    """Benzersiz etiket sözcüğü (qz...) taşıyan sentetik paragraf."""
    n = rng.randint(lo, hi)
    words = _words(rng, n).split()
    words.insert(rng.randint(0, len(words)), tag)
    return " ".join(words).capitalize() + "."


def _tag(i):
    """Sayısız, benzersiz etiket sözcüğü: qzba, qzbb, ..."""
    letters = "bcdfghjklmnpqrstvwxz"
    out = ""
    i += 1
    while i:
        i, r = divmod(i - 1, len(letters))
        out = letters[r] + out
    return "qz" + out


_TOKEN_RE = re.compile(r"\\[a-zA-Z]+\*?|\[[^\]]*\]|\{[^{}]*://[^{}]*\}|[^\W\d_]+")


def _tr_tag(tag):
    """Etiketin çevirideki biçimi (qzbcd -> wxdcb): çeviride kaynakla ORTAK
    sözcük olmasın ki hizalama etiketten yararlanamasın (ortak nadir sözcük
    ödülü); değerlendirme iki biçimi de kaynağa çevirir."""
    return "wx" + tag[2:][::-1]


def _pseudo_line(line, rng, factor, keep=frozenset()):
    """Satırın sözcüklerini sözde Türkçe sözcüklerle değiştir; komutlar,
    sayılar ve *keep* (çevirmenin aynen bıraktığı adlar) korunur, etiket
    sözcükleri (qz...) çeviri biçimine (wx...) döner. Uzunluk ~factor kat."""
    out, pos = [], 0
    for m in _TOKEN_RE.finditer(line):
        out.append(line[pos:m.start()])
        tok = m.group(0)
        if tok.startswith(("\\", "[", "{")) or tok in keep:
            out.append(tok)
        elif tok.startswith("qz"):
            out.append(_tr_tag(tok))
        else:
            n = max(1, round(len(tok) * factor * rng.uniform(0.9, 1.3)))
            word = "".join(rng.choice(_TR_LETTERS) for _ in range(n))
            out.append(word.capitalize() if tok[:1].isupper() else word)
        pos = m.end()
    out.append(line[pos:])
    return "".join(out)


def _pseudo_translate(tex, seed=7, spread=0.12, keep=frozenset()):
    """LaTeX gövdesinin sözde çevirisi: yapı aynen, paragraf başına
    lognormal uzunluk çarpanı (çevirmen serbestliği)."""
    rng = random.Random(seed)
    out = []
    raw = 0
    factor = 1.0
    for line in tex.split("\n"):
        s = line.strip()
        m = re.match(r"\\(begin|end)\{([^}]*)\}", s)
        if m:
            if m.group(2) in ("lstlisting", "verbatim", "titlepage"):
                raw += 1 if m.group(1) == "begin" else -1
            out.append(line)
            continue
        if raw or not s or s.startswith("%"):
            if not s:
                factor = rng.lognormvariate(0.08, spread)
            out.append(line)
            continue
        out.append(_pseudo_line(line, rng, factor, keep))
    return "\n".join(out)


def _blocks(tex):
    return tex.split("\n\n")


def _is_plain(block):
    s = block.strip()
    return bool(s) and not s.startswith("\\") and "\n" not in s and bool(_tags_in(s))


# ---------------------------------------------------------------------------
# EPUB kurulum yardımcıları
# ---------------------------------------------------------------------------

def _make_epub(path, chapters, title="Sentetik Kitap", author="Ada Yazar"):
    """chapters: [(başlık, [paragraf, ...])] -> EPUB (her bölüm bir belge)."""
    from ebooklib import epub

    book = epub.EpubBook()
    book.set_identifier("urn:uuid:e2-test")
    book.set_title(title)
    book.set_language("en")
    book.add_author(author)
    items = []
    for ci, (head, paras) in enumerate(chapters):
        body = f"<h1>{head}</h1>" + "".join(f"<p>{p}</p>" for p in paras)
        it = epub.EpubHtml(uid=f"c{ci}", file_name=f"c{ci}.xhtml", title=head,
                           lang="en")
        it.content = f"<html><body>{body}</body></html>"
        book.add_item(it)
        items.append(it)
    book.toc = [epub.Link(f"c{i}.xhtml", f"Toc {i}", f"toc-{i}")
                for i in range(len(items))]
    book.add_item(epub.EpubNcx())
    book.spine = items
    epub.write_epub(str(path), book)
    return str(path)


def _convert(epub_path):
    """Hattın gerçek zinciri: EPUBParser.to_markdown -> MarkdownToLatex."""
    from parsers.epub_parser import EPUBParser
    from latex_pipeline.config import PipelineConfig
    from latex_pipeline.converter import MarkdownToLatex

    p = EPUBParser()
    doc = p.parse(epub_path)
    md = p.to_markdown(doc)
    cfg = PipelineConfig(input_pdf="x.pdf", output_dir="/tmp", document_class="book",
                         translate=False, source_lang="en", target_lang="tr")
    return MarkdownToLatex(cfg).convert(md)


def _book(rng, chapters=3, per=14):
    """Sentetik kitap: bölüm başlığı + paragraflar; her paragraf ve başlık
    benzersiz qz etiketi taşır."""
    out, k = [], 0
    for c in range(chapters):
        head = f"{_words(rng, 2).title()} {_tag(k)}"
        k += 1
        paras = []
        for _ in range(per):
            paras.append(_para(rng, _tag(k)))
            k += 1
        out.append((head, paras))
    return out


def _setup(tmp_path, chapters, translate=_pseudo_translate, name="Kitap"):
    """Kitap klasörü: source_book.epub + source/body.tex (gerçek dönüştürücü)
    + source/translated_body.tex (sözde çeviri)."""
    out = tmp_path / name
    (out / "source").mkdir(parents=True)
    epub_path = _make_epub(out / "source_book.epub", chapters)
    body = _convert(epub_path)
    (out / "source" / "body.tex").write_text(body, encoding="utf-8")
    tr = translate(body)
    (out / "source" / "translated_body.tex").write_text(tr, encoding="utf-8")
    return out, epub_path, body, tr


def _element_tags(epub_path):
    """[(özgün etiket, çıktıdaki etiketler)] — belge sırasıyla her öğe."""
    from ebooklib import epub
    from bs4 import BeautifulSoup

    book = epub.read_epub(epub_path)
    rows = []
    for item in book.get_items():
        if not item.get_name().startswith("c"):
            continue
        soup = BeautifulSoup(item.get_content().decode("utf-8"), "lxml")
        for el in soup.find_all(["h1", "p"]):
            rows.append((item.get_name(), el.name, el.get_text(" ")))
    return rows


def _tags_in(text):
    """Metindeki etiketler, kaynak biçiminde (çevirideki wx... geri döner)."""
    return ["qz" + t[2:] if t.startswith("qz") else "qz" + t[2:][::-1]
            for t in re.findall(r"(?:qz|wx)[a-z]+", text)]


def _compare(src_epub, out_epub):
    """(doğru, yanlış, çevrilmemiş, boş) öğe sayıları."""
    a, b = _element_tags(src_epub), _element_tags(out_epub)
    assert len(a) == len(b)
    ok = bad = untranslated = empty = 0
    for (_, _, ta), (_, _, tb) in zip(a, b):
        if not tb.strip():
            empty += 1
        elif tb == ta:
            untranslated += 1
        elif _tags_in(ta)[:1] == _tags_in(tb)[:1]:
            ok += 1
        else:
            bad += 1
    return ok, bad, untranslated, empty


# ---------------------------------------------------------------------------
# E2a — LaTeX birimleri ve EPUB metni
# ---------------------------------------------------------------------------

class TestTexUnits:

    def test_kinds_and_boundaries(self):
        tex = (
            "% yorum\n\\chapter{Giriş}\\label{c1}\n\nBir iki\nüç.\n\n"
            "\\begin{itemize}\n  \\item madde bir\n  \\item madde iki\n"
            "\\end{itemize}\n\\section*{Alt}\n\\caption{Şekil yazısı}\n"
            "\\begin{lstlisting}\nkod satırı\n\n  girinti\n\\end{lstlisting}\n"
        )
        units = eo._tex_units(tex)
        assert [u.kind for u in units] == [
            "h:chapter", "para", "item", "item", "h:section", "caption", "code"]
        assert units[0].raw == "Giriş"
        assert units[1].raw == "Bir iki\nüç."
        assert units[6].raw == "kod satırı\n\n  girinti"

    def test_environment_arguments_are_not_text(self):
        # '\begin{table}[H]' ve '\begin{tabular}{l l}' satırındaki argüman
        # metin birimi değildir (eskiden '[H]' / '{l l}' paragraf oluyordu).
        tex = ("\\begin{table}[H]\n\\centering\n\\begin{tabular}{>{\\raggedright}p{2cm} l}\n"
               "A & B \\\\\n\\end{tabular}\n\\end{table}\n")
        units = eo._tex_units(tex)
        raws = [u.raw for u in units]
        assert "[H]" not in raws
        assert not any("raggedright" in r or r.startswith("{") for r in raws)
        # 2026-09-29 (F1-01, E kulvarı): tablo hücresi ayrı birimdir (EPUB
        # tablosunun her hücresi ayrı segment).
        assert [u.raw for u in units if u.kind == "cell"] == ["A", "B"]

    def test_begin_line_text_after_environment_kept(self):
        units = eo._tex_units("\\begin{quote}Alıntı metni burada.\n\\end{quote}\n")
        assert [u.raw for u in units] == ["Alıntı metni burada."]

    def test_features_numbers_anchors(self):
        u = eo._tex_units(
            "\\textit{a} \\textbf{b} \\href{http://x.org/p}{c} 12 ve 7 $x$.\n")[0]
        assert u.feats == (1, 1, 1, 0, 0, 2, 0)
        assert eo._tex_units("> “Alıntı” satırı\n")[0].feats[-1] == 1
        assert u.numbers == {"12": 1, "7": 1}
        assert u.anchors == {"http://x.org/p": 1}


class TestTexToEpubText:

    def test_nested_commands_and_link(self):
        assert eo._tex_to_epub_text(
            "\\textbf{\\textit{Kalın}} ve \\href{https://a.org/b}{\\textit{bağ}}"
        ) == "Kalın ve [bağ](https://a.org/b)"

    def test_escapes_and_grouping_braces(self):
        assert eo._tex_to_epub_text("{\\Huge\\bfseries Başlık}\\par") == "Başlık"
        assert eo._tex_to_epub_text("\\%50 \\& \\{x\\} a~b") == "%50 & {x} a b"

    def test_simple_math_and_dotless_i(self):
        assert eo._tex_to_epub_text("Açı $\\alpha$ ve Duyarl{\\i}l{\\i}k") == \
            "Açı α ve Duyarlılık"


class TestHtmlSafe:

    def test_escaped_only_when_markdown_is_parsed(self):
        from parsers.epub_parser import EPUBParser

        p = EPUBParser()
        # Bağlantılı metin HTML olarak ayrıştırılır: '<' kaçışlanmalı
        linked = "a<b ve [bağ](http://x.org)"
        safe = eo._html_safe(p, linked)
        assert safe == "a&lt;b ve [bağ](http://x.org)"
        # İşaretsiz metin öğeye düz yazılır: olduğu gibi kalır
        assert eo._html_safe(p, "a<b & c") == "a<b & c"

    def test_rebuilt_element_keeps_less_than(self, tmp_path):
        from parsers.epub_parser import EPUBParser
        from bs4 import BeautifulSoup

        p = EPUBParser()
        soup = BeautifulSoup("<p>x</p>", "lxml")
        el = soup.p
        p._apply_translation(el, eo._html_safe(p, "a<b ve [bağ](http://x.org)"),
                             prefix="")
        assert el.get_text() == "a<b ve bağ"
        assert el.a["href"] == "http://x.org"


# ---------------------------------------------------------------------------
# E2a — A1 (aynı dil) ve A2 (diller arası) hizalama
# ---------------------------------------------------------------------------

class TestSameLanguage:

    def test_extra_units_do_not_shift(self):
        # Dönüştürücünün eklediği birimler (kapak, yazar) segmentlerde yok:
        # eşleme kaymaz (pozisyonel eşlemede +2 kayardı).
        seg = [["alfa", "beta"], ["gama", "delta", "epsi"], ["zeta"]]
        units = [["kitap", "adi"], ["yazar"], ["alfa", "beta"],
                 ["gama", "delta", "epsi"], ["zeta"]]
        groups = eo._align_same_language(seg, units)
        assert [(g[0], g[1]) for g in groups] == [((0,), (2,)), ((1,), (3,)), ((2,), (4,))]

    def test_near_equal_and_split_segment(self):
        # Yakın metin (Dice) ve dönüştürücüde ikiye bölünmüş segment (1:2)
        seg = [["bir", "iki", "uc", "dort", "bes"],
               ["alti", "yedi", "sekiz", "dokuz", "on", "onbir"],
               ["son"]]
        units = [["bir", "iki", "uc", "dort", "bes", "x"],
                 ["alti", "yedi", "sekiz"], ["dokuz", "on", "onbir"],
                 ["son"]]
        groups = eo._align_same_language(seg, units)
        assert ((0,), (0,)) in [(g[0], g[1]) for g in groups]
        assert ((1,), (1, 2)) in [(g[0], g[1]) for g in groups]
        assert ((2,), (3,)) in [(g[0], g[1]) for g in groups]

    def test_wordless_items_never_match(self):
        groups = eo._align_same_language([None, ["a", "b"]], [None, ["a", "b"]])
        assert [(g[0], g[1]) for g in groups] == [((1,), (1,))]


def _src_tex(rng, n=40, chapters=2):
    parts, k = [], 0
    per = n // chapters
    for c in range(chapters):
        parts.append(f"\\chapter{{{_words(rng, 2).title()} {_tag(k)}}}")
        k += 1
        for _ in range(per):
            parts.append(_para(rng, _tag(k)))
            k += 1
    return "\n\n".join(parts) + "\n"


def _cross(src, tr):
    su, tu = eo._tex_units(src), eo._tex_units(tr)
    out, stats = eo._align_cross_language(su, tu)
    return su, tu, out, stats


def _check_cross(su, out):
    """Her eşleşmede çevirideki etiket kaynak birimin etiketiyle aynı."""
    wrong = []
    for a, text in out.items():
        if not text:
            continue
        if _tags_in(su[a].raw)[:1] != _tags_in(text)[:1]:
            wrong.append((a, su[a].raw[:40], text[:40]))
    return wrong


class TestCrossLanguage:

    def test_identity_structure(self):
        rng = random.Random(1)
        src = _src_tex(rng)
        su, tu, out, stats = _cross(src, _pseudo_translate(src))
        assert _check_cross(su, out) == []
        assert len([v for v in out.values() if v]) >= len(su) - 1
        assert stats["blocks"] == 2            # iki bölüm (öncesi boş blok yok)
        assert 0.05 < stats["length_spread"] < 0.3

    def test_merged_paragraphs_head_gets_text_tail_emptied(self):
        rng = random.Random(2)
        src = _src_tex(rng)
        tr = _blocks(_pseudo_translate(src))
        i = next(k for k in range(5, len(tr)) if _is_plain(tr[k]) and _is_plain(tr[k + 1]))
        merged_tags = _tags_in(tr[i]) + _tags_in(tr[i + 1])
        tr[i:i + 2] = [tr[i] + " " + tr[i + 1]]
        su, tu, out, _ = _cross(src, "\n\n".join(tr))
        head = next(a for a in out if _tags_in(su[a].raw)[:1] == merged_tags[:1])
        assert _tags_in(out[head]) == merged_tags
        assert out.get(head + 1) in ("", None) or head + 1 not in out
        assert _check_cross(su, out) == []

    def test_split_paragraph_both_parts_on_one_unit(self):
        rng = random.Random(3)
        src = _src_tex(rng)
        tr = _blocks(_pseudo_translate(src))
        i = next(k for k in range(5, len(tr)) if _is_plain(tr[k]) and len(tr[k].split()) > 30)
        words = tr[i].split(" ")
        tag = _tags_in(tr[i])[0]
        tr[i:i + 1] = [" ".join(words[:len(words) // 2]), " ".join(words[len(words) // 2:])]
        su, tu, out, _ = _cross(src, "\n\n".join(tr))
        a = next(a for a in out if _tags_in(su[a].raw)[:1] == [tag])
        # Bölünmüş çevirinin iki parçası da aynı öğeye (ya da hiç) yazılır
        assert len(out[a].split(" ")) == len(words)
        assert _check_cross(su, out) == []

    def test_dropped_and_added_paragraphs_do_not_shift(self):
        rng = random.Random(4)
        src = _src_tex(rng, n=60)
        tr = _blocks(_pseudo_translate(src))
        drop = next(k for k in range(8, len(tr)) if _is_plain(tr[k]))
        dropped_tag = _tags_in(tr[drop])[0]
        del tr[drop]
        tr.insert(20, "Çevirmen notu: kavram için sözlüğe bakınız.")
        su, tu, out, _ = _cross(src, "\n\n".join(tr))
        assert _check_cross(su, out) == []
        assert all(dropped_tag not in (v or "") for v in out.values())
        # Belirsizlik bozulmanın yakınında kalır (benzer uzunluktaki komşular
        # kaynak dilde kalabilir); geri kalan hepsi eşlenir.
        assert len([v for v in out.values() if v]) >= len(su) - 8

    @pytest.mark.parametrize("seed", [5, 6, 7])
    def test_unrelated_translation_is_rejected(self, seed):
        # Başka bir metnin "çevirisi" (aynı üreteç: uzunluk dağılımı benzer,
        # en zor durum): güvenli eşleşme oranı eşiğin çok altında kalır.
        src = _src_tex(random.Random(seed), n=60)
        other = _src_tex(random.Random(99 + seed), n=60)
        su, tu, out, stats = _cross(src, _pseudo_translate(other))
        coverage = len([v for v in out.values() if v]) / len(su)
        assert coverage < eo.MIN_ALIGNED_COVERAGE / 2

    def test_spread_rule(self):
        base = {"counted": 10, "coverage": 1.0, "too_large": False}
        assert eo._alignment_problem({**base, "length_spread": 0.12}) is None
        reason = eo._alignment_problem(
            {**base, "length_spread": eo.MAX_LENGTH_SPREAD + 0.01})
        assert "ilişkisiz" in reason and not reason.startswith("EPUB üretilmedi")
        # Türkçe ondalık virgül
        assert "0,31" in eo._alignment_problem({**base, "length_spread": 0.31})

    def test_shared_names_resolve_insert_plus_drop_among_lookalikes(self):
        # Bir EPUB kitabında ölçülen hata: benzer uzunluk ve biçimde kısa alıntılar
        # dizisinde çevirmen notu eklenip bir alıntı düşünce, uzunluk + yapı
        # modeli üç alıntıyı birer kaydırıyordu (güvenle, yanlış). Çeviride
        # aynen kalan adlar (ortak nadir sözcük) kaymayı önler.
        rng = random.Random(21)
        names = ["Zorbalis", "Quintara", "Belmokar", "Draventi", "Selvanor",
                 "Kortheim"]
        parts = [_para(rng, _tag(k)) for k in range(24)]
        for n, name in enumerate(names):
            parts.append(f"> “A \\textbf{{\\textit{{{name}}}}} {_words(rng, 6)} "
                         f"{_tag(100 + n)}.”")
        parts += [_para(rng, _tag(200 + k)) for k in range(6)]
        src = "\n\n".join(parts) + "\n"
        tr = _blocks(_pseudo_translate(src, keep=frozenset(names)))
        q0 = next(k for k, b in enumerate(tr) if names[0] in b)
        del tr[q0 + 2]                                  # üçüncü alıntı düştü
        tr.insert(q0, "Çevirmen notu: kavram için sözlüğe bakınız.")
        su, tu, out, _ = _cross(src, "\n\n".join(tr))
        assert _check_cross(su, out) == []
        assert not any((v or "").startswith("Çevirmen notu") for v in out.values())
        placed = {_tags_in(v)[0] for v in out.values() if v and _tags_in(v)}
        # Notun hemen ardındaki alıntı belirsiz kalabilir (kaynak dilde);
        # kalan alıntılar doğru yerine yazılır.
        assert len({_tag(100), _tag(101), _tag(103), _tag(104)} & placed) >= 3

    def test_heading_sequence_mismatch_falls_back_to_one_block(self):
        rng = random.Random(6)
        src = _src_tex(rng)
        tr = _pseudo_translate(src).replace("\\chapter{", "\\section{", 1)
        _su, _tu, _out, stats = _cross(src, tr)
        assert stats["blocks"] == 1

    @pytest.mark.parametrize("limit", ["MAX_BAND_CELLS", "MAX_BLOCK_CELLS"])
    def test_lattice_cell_limit(self, monkeypatch, limit):
        rng = random.Random(7)
        src = _src_tex(rng)
        monkeypatch.setattr(eo, limit, 10)
        _su, _tu, out, stats = _cross(src, _pseudo_translate(src))
        assert out == {} and stats["too_large"]
        reason = eo._alignment_problem({"counted": 5, "coverage": 0.0, **stats})
        assert "hizalama sınırını" in reason


class TestAlignTranslations:

    def _segs(self, texts, kinds=None):
        kinds = kinds or ["paragraph"] * len(texts)
        return [{"text": t, "type": k, "index": i, "chapter": 0}
                for i, (t, k) in enumerate(zip(texts, kinds))]

    def test_title_block_offset_does_not_shift(self):
        # Kapak/yazar birimleri (to_markdown üst bilgisi) segmentlerde yok.
        rng = random.Random(8)
        paras = [_para(rng, _tag(k)) for k in range(30)]
        src = ("\\begin{titlepage}\n{\\Huge\\bfseries Kitap Adı}\\par\n\\end{titlepage}\n\n"
               "\\textit{Ada Yazar}\n\n\\chapter{Giriş Bölümü}\n\n" + "\n\n".join(paras) + "\n")
        segs = self._segs(["Giriş Bölümü"] + paras, ["heading"] + ["paragraph"] * 30)
        tr, rep = eo.align_translations(segs, src, _pseudo_translate(src))
        for i, p in enumerate(paras, start=1):
            assert _tags_in(tr[i])[:1] == _tags_in(p)[:1]
        assert rep["coverage"] == 1.0

    def test_merge_tail_emptied_only_when_head_written(self, monkeypatch):
        segs = self._segs(["alfa beta gama", "delta epsi zeta"])
        src = "alfa beta gama\n\ndelta epsi zeta\n"
        # Birleşme: birim 1 metni birim 0'da
        monkeypatch.setattr(eo, "_align_cross_language",
                            lambda s, t: ({0: "BİR İKİ", 1: ""}, {}))
        tr, _ = eo.align_translations(segs, src, "x")
        assert tr == {0: "BİR İKİ", 1: ""}
        # Baş birim güvenle yazılamadıysa kuyruk BOŞALTILMAZ (metin kaybolmaz)
        monkeypatch.setattr(eo, "_align_cross_language",
                            lambda s, t: ({1: ""}, {}))
        tr, _ = eo.align_translations(segs, src, "x")
        assert tr == {}

    def test_identical_translation_keeps_original_element(self, monkeypatch):
        segs = self._segs(["Ada Soyad"])
        monkeypatch.setattr(eo, "_align_cross_language",
                            lambda s, t: ({0: None}, {}))
        tr, rep = eo.align_translations(segs, "Ada Soyad\n", "Ada Soyad\n")
        assert tr == {} and rep["kept_original"] == 1 and rep["coverage"] == 1.0

    def test_code_segments_not_counted(self):
        segs = self._segs(["```\nkod\n```", "alfa beta"])
        _tr, rep = eo.align_translations(segs, "alfa beta\n", "alfa beta\n")
        assert rep["counted"] == 1


class TestHeadingLabel:

    def test_chapter_label_restored_in_turkish(self):
        out = eo._restore_heading_label("Chapter 7. Finetuning", ["finetuning"],
                                        "İnce Ayar")
        assert out == f"{eo.EPUB_CHAPTER_LABEL_TR} 7: İnce Ayar"

    def test_label_not_doubled(self):
        assert eo._restore_heading_label(
            "Chapter 7. Finetuning", ["finetuning"], "Bölüm 7: İnce Ayar"
        ) == "Bölüm 7: İnce Ayar"

    def test_roman_number_prefix_restored(self):
        assert eo._restore_heading_label(
            "I LE GUN-CLUB", ["le", "gun", "club"], "GUN-KULÜBÜ") == "I GUN-KULÜBÜ"

    def test_untouched_when_converter_kept_label(self):
        # Birimde etiket duruyorsa (dönüştürücü atmadıysa) dokunulmaz
        assert eo._restore_heading_label(
            "Chapter 7. Finetuning", ["chapter", "7", "finetuning"], "X") == "X"


# ---------------------------------------------------------------------------
# E2a/E2b — uçtan uca (gerçek ayrıştırıcı + dönüştürücü + rebuild)
# ---------------------------------------------------------------------------

class TestGenerateEpubOutput:

    def test_every_element_gets_its_own_translation(self, tmp_path, capsys):
        rng = random.Random(10)
        out, epub_path, _body, _tr = _setup(tmp_path, _book(rng))
        res = eo.generate_epub_output(epub_path, str(out), "Kitap")
        assert res and Path(res).is_file()
        # 45 öğe (3 başlık + 42 paragraf): hepsi kendi çevirisini alır
        assert _compare(epub_path, res) == (45, 0, 0, 0)
        assert not (out / "compiled" / eo.EPUB_SKIP_NOTE_NAME).exists()

    def test_positional_code_would_shift(self, tmp_path):
        # Farksal: aynı girdide eski pozisyonel eşleme (i == i) kayar.
        rng = random.Random(11)
        out, epub_path, _body, tr = _setup(tmp_path, _book(rng))
        from parsers.epub_parser import EPUBParser
        p = EPUBParser()
        segs = p.extract_segments(p.parse(epub_path))
        paras = eo._extract_text_from_tex(tr)
        positional_ok = sum(1 for i, s in enumerate(segs) if i < len(paras)
                            and _tags_in(s["text"])[:1] == _tags_in(paras[i])[:1])
        assert positional_ok < len(segs) // 2

    def test_perturbed_translation_no_misplacement(self, tmp_path):
        def tr_fn(body):
            tr = _blocks(_pseudo_translate(body, seed=3))
            plain = [k for k in range(len(tr)) if _is_plain(tr[k])]
            a, b, c = plain[4], plain[15], plain[27]
            tr[c] = tr[c] + "\n\nÇevirmen notu: terim için sözlüğe bakınız."
            w = tr[b].split(" ")
            tr[b] = " ".join(w[:len(w) // 2]) + "\n\n" + " ".join(w[len(w) // 2:])
            tr[a] = tr[a] + " " + tr[a + 1]
            del tr[a + 1]
            return "\n\n".join(tr)

        rng = random.Random(12)
        out, epub_path, _body, _tr = _setup(tmp_path, _book(rng, per=16), tr_fn)
        res = eo.generate_epub_output(epub_path, str(out), "Kitap")
        assert res
        ok, bad, untranslated, empty = _compare(epub_path, res)
        assert bad == 0
        assert ok + empty >= 48

    def test_unrelated_translation_writes_note_and_removes_stale_epub(self, tmp_path):
        rng = random.Random(13)
        other = _book(random.Random(77), chapters=3, per=14)

        def tr_fn(_body):
            # Başka bir kitabın çevirisi (yapı benzer, içerik ilişkisiz)
            from parsers.epub_parser import EPUBParser  # noqa: F401
            p2 = _make_epub(tmp_path / "baska.epub", other)
            return _pseudo_translate(_convert(p2))

        out, epub_path, _body, _tr = _setup(tmp_path, _book(rng), tr_fn)
        stale = out / "compiled" / "Kitap_TR.epub"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_bytes(b"eski")
        assert eo.generate_epub_output(epub_path, str(out), "Kitap") is None
        note = (out / "compiled" / eo.EPUB_SKIP_NOTE_NAME).read_text(encoding="utf-8")
        # Yalnız neden (önek "EPUB üretilmedi"yi 2_derle/arayüz koyar)
        assert note.count("\n") == 1 and not note.startswith("EPUB üretilmedi")
        assert "eşleşme oranı %" in note and "PDF etkilenmedi" in note
        assert "silindi" in note
        assert not stale.exists()

    def test_success_clears_old_note(self, tmp_path):
        rng = random.Random(14)
        out, epub_path, _body, _tr = _setup(tmp_path, _book(rng))
        (out / "compiled").mkdir(parents=True, exist_ok=True)
        (out / "compiled" / eo.EPUB_SKIP_NOTE_NAME).write_text("eski\n", encoding="utf-8")
        (out / eo.EPUB_SKIP_NOTE_NAME).write_text("eski\n", encoding="utf-8")
        assert eo.generate_epub_output(epub_path, str(out), "Kitap")
        assert not (out / "compiled" / eo.EPUB_SKIP_NOTE_NAME).exists()
        assert not (out / eo.EPUB_SKIP_NOTE_NAME).exists()

    def test_no_translation_yet_writes_nothing(self, tmp_path):
        # --export-tex koşusu: çeviri yok -> not yazılmaz, EPUB'a dokunulmaz
        out = tmp_path / "k"
        (out / "compiled").mkdir(parents=True)
        prev = out / "compiled" / "Kitap_TR.epub"
        prev.write_bytes(b"onceki")
        assert eo.generate_epub_output("yok.epub", str(out), "Kitap") is None
        assert not (out / "compiled" / eo.EPUB_SKIP_NOTE_NAME).exists()
        assert prev.exists()

    def test_missing_source_tex_writes_note(self, tmp_path):
        out = tmp_path / "k"
        (out / "source").mkdir(parents=True)
        (out / "source" / "translated_body.tex").write_text("x", encoding="utf-8")
        assert eo.generate_epub_output("yok.epub", str(out), "Kitap") is None
        note = (out / "compiled" / eo.EPUB_SKIP_NOTE_NAME).read_text(encoding="utf-8")
        assert "source/body.tex" in note and note.count("\n") == 1

    def test_missing_source_epub_writes_note(self, tmp_path):
        out = tmp_path / "k"
        (out / "source").mkdir(parents=True)
        (out / "source" / "translated_body.tex").write_text("x", encoding="utf-8")
        (out / "source" / "body.tex").write_text("x", encoding="utf-8")
        assert eo.generate_epub_output("yok.epub", str(out), "Kitap") is None
        note = (out / "compiled" / eo.EPUB_SKIP_NOTE_NAME).read_text(encoding="utf-8")
        assert note.startswith("Kaynak EPUB dosyası")

    def test_derle_reads_note(self, tmp_path):
        # 2_derle.py S5 notunu aynı adla okur (sözleşme)
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "derle_mod", PROJECT_ROOT / "2_derle.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod.EPUB_SKIP_NOTE_NAME == eo.EPUB_SKIP_NOTE_NAME
        out = tmp_path / "k"
        (out / "source").mkdir(parents=True)
        (out / "source" / "translated_body.tex").write_text("x", encoding="utf-8")
        assert eo.generate_epub_output("yok.epub", str(out), "Kitap") is None
        assert mod._read_epub_skip_note(out, since=0.0).startswith("Kaynak LaTeX")


# ---------------------------------------------------------------------------
# E2 — rebuild_epub: bölüm içinde tekrar eden öğe ilk geçişin çevirisini alır
# ---------------------------------------------------------------------------

class TestRebuildDuplicates:

    def _epub(self, tmp_path, body):
        from ebooklib import epub

        book = epub.EpubBook()
        book.set_identifier("urn:uuid:e2-dup")
        book.set_title("Sentetik")
        book.set_language("en")
        it = epub.EpubHtml(uid="c0", file_name="c0.xhtml", title="c0", lang="en")
        it.content = f"<html><body>{body}</body></html>"
        book.add_item(it)
        book.toc = [epub.Link("c0.xhtml", "Toc", "toc-0")]
        book.add_item(epub.EpubNcx())
        book.spine = [it]
        path = tmp_path / "dup.epub"
        epub.write_epub(str(path), book)
        return str(path)

    def _rebuild(self, tmp_path, src, make_tr):
        from parsers.epub_parser import EPUBParser

        p = EPUBParser()
        doc = p.parse(src)
        segs = p.extract_segments(doc)
        out = str(tmp_path / "out.epub")
        p.rebuild_epub(doc, make_tr(segs), out)
        return segs, [t for _c, _n, t in _element_tags(out)]

    def test_repeated_paragraph_gets_first_translation(self, tmp_path):
        # Eskiden ikinci "Reply to thread." kaynak dilde kalıyordu.
        src = self._epub(tmp_path, "<h1>Opening</h1><p>Reply to thread.</p>"
                                   "<p>Middle text here.</p><p>Reply to thread.</p>")
        segs, texts = self._rebuild(
            tmp_path, src, lambda segs: {s["index"]: f"TR{s['index']}" for s in segs})
        assert [s["text"] for s in segs] == ["Opening", "Reply to thread.",
                                             "Middle text here."]
        assert texts == ["TR0", "TR1", "TR2", "TR1"]

    def test_repeated_paragraph_not_emptied_by_merge_marker(self, tmp_path):
        # "" (çevirisi öncekiyle birleşmiş) yalnız kendi öğesini boşaltır.
        src = self._epub(tmp_path, "<h1>Opening</h1><p>Reply to thread.</p>"
                                   "<p>Middle text here.</p><p>Reply to thread.</p>")
        _segs, texts = self._rebuild(
            tmp_path, src, lambda segs: {0: "TR0", 1: "", 2: "TR2"})
        assert texts == ["TR0", "", "TR2", "Reply to thread."]


class TestAlignmentErrorWritesNote:

    def test_exception_in_alignment_becomes_skip_note(self, tmp_path, monkeypatch):
        rng = random.Random(15)
        out, epub_path, _body, _tr = _setup(tmp_path, _book(rng, chapters=1, per=4))

        def boom(*_a, **_k):
            raise ValueError("deneme")

        monkeypatch.setattr(eo, "align_translations", boom)
        assert eo.generate_epub_output(epub_path, str(out), "Kitap") is None
        note = (out / "compiled" / eo.EPUB_SKIP_NOTE_NAME).read_text(encoding="utf-8")
        assert note.startswith("Çeviri kaynak EPUB'la hizalanırken") and "deneme" in note
