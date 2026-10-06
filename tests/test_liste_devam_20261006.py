"""Kesintili numaralı liste kaynak numarasını korur (6 Ekim 2026, C kulvarı).

V1 bulgusu 8 (iç denetim notu, 5 Ekim 2026): araya denklem,
paragraf ya da şekil giren numaralı liste ("1." … denklem … "2.", "3.")
LaTeX'te yeniden 1'den sayıyordu; dönüştürücü enumerate'e başlangıç numarası
vermiyordu. Korpusta (sıralı eşlemeyle) iki kitapta 28 madde.

Mekanizma: ``\\begin{enumerate}[start=N]`` — seçenek ``\\begin`` ile AYNI
satırda (enumitem; şablonda yüklü). Neden bu biçim:
  * EPUB hizalaması (_tex_units) ``\\begin{…}`` ardındaki ``[…]`` seçeneğini
    zaten atar. ``\\setcounter{enumi}{N}`` ise ayrı satırda da, ``\\begin``
    ile aynı satırda da metin birimi ('para', sözcükleri 'enumi', 'N') olarak
    hizalamaya sızar.
  * Yerel model çevirmeni liste ortamı satırını yer tutucuya alır; seçenek
    artık yer tutucunun içindedir (eskiden ``<<LATEX_n>>[start=3]`` olarak
    modele gidiyor, 'start' sözcüğü çevrilip enumitem anahtarı bozuluyordu).

Girdiler sentetiktir (KARAR 26); model yüklenmez, gerçek sunucuya
bağlanılmaz (sahte Ollama: tests/fakes, rastgele port).
"""
from __future__ import annotations

import re
import zlib
from collections import Counter
from pathlib import Path

import pytest

from latex_pipeline.config import PipelineConfig
from latex_pipeline.converter import MarkdownToLatex
# run_pipeline'ı modelsiz koşturma fikstürleri (ön kontrol geçer, bellek
# geçici; kök logger geri yüklenir) — G2a2 uçtan uca testiyle aynı kaynak.
from test_kaynak_dil_20260928 import pipeline_env, restore_root_logger  # noqa: E402,F401

KOK = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------------
@pytest.fixture
def convert(tmp_path):
    def _run(markdown: str) -> str:
        cfg = PipelineConfig(document_class="book", output_dir=str(tmp_path))
        return MarkdownToLatex(cfg).convert(markdown)
    return _run


_TOKEN_RE = re.compile(
    r"\\(begin|end)\{(enumerate|itemize|description)\}((?:\[[^\]\n]*\])?)"
    r"|\\setcounter\{enumi\}\{(\d+)\}"
    r"|\\item(?![a-zA-Z])(\[[^\]\n]*\])?[ \t]*([^\n]*)")


def _basilan(tex: str) -> list:
    """LaTeX'in BASACAĞI numaralar: [(numara ya da None, madde metni)].

    1. düzey enumerate sayacı (enumitem ``start=N``, ``\\setcounter{enumi}``
    desteklenir); itemize maddesi ve iç düzey (a), (b) … None.
    """
    out, stack = [], []
    for m in _TOKEN_RE.finditer(tex):
        if m.group(1) == "begin":
            start = re.search(r"start=(\d+)", m.group(3) or "")
            stack.append([m.group(2), int(start.group(1)) - 1 if start else 0])
        elif m.group(1) == "end":
            stack.pop()
        elif m.group(4):
            stack[-1][1] = int(m.group(4))
        else:
            env = stack[-1]
            depth = sum(1 for e in stack if e[0] == "enumerate")
            if env[0] == "enumerate" and not m.group(5):
                env[1] += 1
                out.append((env[1] if depth == 1 else None, m.group(6)))
            else:
                out.append((None, m.group(6)))
    assert not stack, "dengesiz liste ortamı"
    return out


# Sentetik içerik (kitap metni değil).
_STEP1 = "Pick an output map that turns raw scores into a probability vector."
_STEP2 = "Encode every training label as a one-hot vector of the same size."
_STEP3 = "Prefer the cross-entropy loss over the squared error for this task."

_KESINTILI_DENKLEM = (
    "A short synthetic paragraph introduces the three changes below.\n\n"
    f"1. {_STEP1}\n\n"
    "$$p_i = \\frac{e^{z_i}}{\\sum_j e^{z_j}}$$\n"
    "where z holds the scores of the last layer.\n\n"
    f"2. {_STEP2}\n\n"
    f"3. {_STEP3}\n\n"
    "A closing synthetic paragraph ends the section.\n")


# ===========================================================================
# Dönüştürücü: başlangıç numarası
# ===========================================================================
class TestDonusturucu:
    def test_denklemle_kesilen_liste_kaynak_numarasini_korur(self, convert):
        tex = convert(_KESINTILI_DENKLEM)
        assert _basilan(tex) == [(1, _STEP1), (2, _STEP2), (3, _STEP3)]
        # Seçenek \begin ile aynı satırda, ayrı satırda sayaç ayarı yok.
        assert "\\begin{enumerate}[start=2]\n" in tex
        assert "\\setcounter" not in tex

    def test_paragrafla_kesilen_liste(self, convert):
        md = (f"1. {_STEP1}\n\n"
              "A plain synthetic paragraph sits between the two steps.\n\n"
              f"2. {_STEP2}\n")
        assert _basilan(convert(md)) == [(1, _STEP1), (2, _STEP2)]

    def test_sekille_kesilen_liste(self, convert):
        md = (f"1. {_STEP1}\n\n"
              "![](_page_3_Picture_1.jpeg)\n\n"
              "FIGURE 1.1 A synthetic caption for the drawing.\n\n"
              f"4. {_STEP2}\n"
              f"5. {_STEP3}\n")
        assert _basilan(convert(md)) == [(1, _STEP1), (4, _STEP2), (5, _STEP3)]

    def test_birden_baslayan_liste_aynen(self, convert):
        # 1'den başlayan liste değişmez: seçenek yok (bayt bayt eski çıktı).
        tex = convert(f"1. {_STEP1}\n2. {_STEP2}\n")
        assert "\\begin{enumerate}\n" in tex and "[start=" not in tex

    def test_sifirdan_baslayan_liste(self, convert):
        tex = convert(f"0. {_STEP1}\n1. {_STEP2}\n")
        assert _basilan(tex) == [(0, _STEP1), (1, _STEP2)]

    def test_ic_liste_baslangic_numarasi(self, convert):
        md = (f"1. {_STEP1}\n"
              f"   3. {_STEP2}\n"
              f"   4. {_STEP3}\n")
        tex = convert(md)
        assert "\\begin{enumerate}[start=3]" in tex
        # İç liste (a), (b) basar; dış madde 1.
        assert _basilan(tex)[0] == (1, _STEP1)

    def test_madde_metni_degismez(self, convert):
        # Kayıpsızlık: yalnız numara taşınır; madde metni ve madde sayısı aynı,
        # numara metinde ikinci kez görünmez ("2. 2. …" yok).
        tex = convert(_KESINTILI_DENKLEM)
        items = [t for _n, t in _basilan(tex)]
        assert items == [_STEP1, _STEP2, _STEP3]
        assert not re.search(r"\\item\s+\d+\.\s", tex)

    def test_ardisik_olmayan_numara_yeni_liste(self, convert):
        # Boş satırla ayrılmış ardışık olmayan numara ayrı listedir
        # (R12, test_kalite_C1); ikinci liste kendi numarasıyla başlar.
        md = f"1. {_STEP1}\n\n5. {_STEP2}\n\nA synthetic paragraph.\n"
        assert _basilan(convert(md)) == [(1, _STEP1), (5, _STEP2)]

    def test_madde_imli_numarali_liste_degismez(self, convert):
        # "- 2." (Marker'ın madde imli numaralı listesi) 1'den başlamıyorsa
        # eski davranış sürer (madde imli, numara metinde; R12 kararı).
        tex = convert(f"- 2. {_STEP2}\n- 3. {_STEP3}\n")
        assert "\\begin{itemize}" in tex and "[start=" not in tex


# ===========================================================================
# EPUB hizalaması: seçenek metin birimi değildir
# ===========================================================================
class TestEpubHizalama:
    def test_secenek_birim_uretmez(self):
        from latex_pipeline.epub_output import _tex_units
        body = ("\\begin{enumerate}[start=3]\n"
                f"  \\item {_STEP2}\n  \\item {_STEP3}\n"
                "\\end{enumerate}")
        bare = body.replace("[start=3]", "")
        got = [(u.kind, u.raw, u.tokens) for u in _tex_units(body)]
        assert got == [(u.kind, u.raw, u.tokens) for u in _tex_units(bare)]
        assert [k for k, _r, _t in got] == ["item", "item"]

    def test_donusturucu_ciktisinda_sayac_birimi_yok(self, convert):
        from latex_pipeline.epub_output import _tex_units
        units = _tex_units(convert(_KESINTILI_DENKLEM))
        assert not any("start" in u.tokens or "enumi" in u.tokens for u in units)
        items = [u.raw for u in units if u.kind == "item"]
        assert items == [_STEP1, _STEP2, _STEP3]

    def test_ayri_satir_sayac_sizar(self):
        # Neden \setcounter seçilmedi (kanıt): ayrı satırda da, \begin ile
        # aynı satırda da sözcükleri 'enumi', '2' olan bir 'para' birimi olur.
        from latex_pipeline.epub_output import _tex_units
        for body in ("\\begin{enumerate}\n\\setcounter{enumi}{2}\n"
                     f"  \\item {_STEP3}\n\\end{{enumerate}}",
                     "\\begin{enumerate}\\setcounter{enumi}{2}\n"
                     f"  \\item {_STEP3}\n\\end{{enumerate}}"):
            kinds = [(u.kind, u.tokens) for u in _tex_units(body)]
            assert kinds[0] == ("para", ["enumi", "2"])

    def test_epub_numarali_paragraflar_uctan_uca(self, tmp_path):
        # EPUB kaynağı: <p>1. …</p> … <p>2. …</p><p>3. …</p> -> markdown ->
        # dönüştürücü (ikinci liste [start=2]) -> hizalama. Seçenek hizalamayı
        # değiştirmez; Türkçe EPUB öğesine kaynak numara geri yazılır.
        import io
        from contextlib import redirect_stdout
        from ebooklib import epub
        from latex_pipeline import epub_output as eo
        from parsers.epub_parser import EPUBParser

        intro = "A short synthetic paragraph introduces the changes listed below."
        middle = "The map turns the raw scores of the last layer into probabilities."
        html = (f"<h1>Steps</h1><p>{intro}</p><p>1. {_STEP1}</p><p>{middle}</p>"
                f"<p>2. {_STEP2}</p><p>3. {_STEP3}</p>")
        book = epub.EpubBook()
        book.set_identifier("urn:uuid:liste-devam-test")
        book.set_title("Liste")
        book.set_language("en")
        book.add_author("Ada Yazar")
        doc = epub.EpubHtml(uid="c0", file_name="c0.xhtml", title="c0", lang="en")
        doc.content = ('<html xmlns:epub="http://www.idpf.org/2007/ops">'
                       f"<body>{html}</body></html>")
        book.add_item(doc)
        book.toc = [epub.Link("c0.xhtml", "c0", "c0")]
        book.add_item(epub.EpubNcx())
        book.spine = [doc]
        path = tmp_path / "liste.epub"
        epub.write_epub(str(path), book)

        parser = EPUBParser()
        with redirect_stdout(io.StringIO()):
            parsed = parser.parse(str(path))
            segs = parser.extract_segments(parsed)
            md = parser.to_markdown(parsed)
        cfg = PipelineConfig(document_class="book", output_dir=str(tmp_path))
        src = MarkdownToLatex(cfg).convert(md)
        assert "\\begin{enumerate}[start=2]\n" in src

        tr_of = {_STEP1: "Ham puanları olasılık vektörüne çeviren bir eşleme seçin.",
                 _STEP2: "Her eğitim etiketini aynı boyutta tek-sıcak vektör yapın.",
                 _STEP3: "Bu görevde kareli hata yerine çapraz entropiyi seçin.",
                 intro: "Kısa bir sentetik paragraf aşağıdaki değişiklikleri tanıtır.",
                 middle: "Eşleme son katmanın ham puanlarını olasılığa dönüştürür.",
                 "Steps": "Adımlar"}
        tr = src
        for en, tr_text in tr_of.items():
            tr = tr.replace(en, tr_text)

        def align(s, t):
            with redirect_stdout(io.StringIO()):
                return eo.align_translations(segs, s, t)

        got, rep = align(src, tr)
        bare, rep_bare = align(src.replace("[start=2]", ""), tr.replace("[start=2]", ""))
        assert got == bare and rep == rep_bare
        assert rep["coverage"] == 1.0
        by_text = {segs[i]["text"]: got[i] for i in range(len(segs))}
        assert by_text[f"2. {_STEP2}"] == "2. " + tr_of[_STEP2]
        assert by_text[f"3. {_STEP3}"] == "3. " + tr_of[_STEP3]


# ===========================================================================
# Yerel model çevirmeni (sahte Ollama)
# ===========================================================================
_TR = ("çalışma", "değer", "ışık", "yörünge", "şekil", "güneş", "ağırlık",
       "kütle", "hız", "yüzey", "dönüş", "öğrenci", "kuvvet", "enerji")
_PH = re.compile(r"(<<[A-Z]+_\d+>>|\[\[[A-Za-z]+_\d+\]\]|@@[A-Za-z]+\d+@@)")
_LETTERS = re.compile(r"[^\W\d_]+")


def _fake_turkish(text: str) -> str:
    """Sahte model (yalnız test altyapısı): her sözcüğü kararlı bir Türkçe
    sözcükle değiştirir; yer tutucular ve sayılar aynen kalır."""
    def word(m: re.Match) -> str:
        tr = _TR[zlib.crc32(m.group(0).lower().encode("utf-8")) % len(_TR)]
        return tr[:1].upper() + tr[1:] if m.group(0)[:1].isupper() else tr
    return "".join(p if _PH.fullmatch(p) else _LETTERS.sub(word, p)
                   for p in _PH.split(text))


@pytest.fixture
def ollama_translator(fake_llm_server, monkeypatch, tmp_path):
    from core.mt_engines import discovery
    from core.mt_engines.contracts import SourcesConfig
    from core.translator import BookTranslator
    from latex_pipeline.translator import LatexTranslator

    real_init_memory = BookTranslator._init_memory

    def _tmp_memory(self):
        self.config.memory_db_path = str(tmp_path / "bellek.db")
        real_init_memory(self)

    monkeypatch.setattr(BookTranslator, "_init_memory", _tmp_memory)

    def make(source_lang: str = "en"):
        srv = fake_llm_server("ollama", models=["sahte"], reply=_fake_turkish)
        sources = SourcesConfig(ollama_url=srv.url,
                                lmstudio_url="http://127.0.0.1:9/v1",
                                openai_servers=())
        monkeypatch.setattr(discovery, "load_sources", lambda path=None: sources)
        lt = LatexTranslator(pipeline_config=PipelineConfig(translate=True),
                             mt_engine="ollama:sahte", source_lang=source_lang,
                             target_lang="tr", book_type="science",
                             project_name="Liste Deneme")
        return lt, srv

    return make


_LISTE_GOVDE = (
    "\\chapter{Steps}\n\n"
    "\\begin{enumerate}\n"
    f"  \\item {_STEP1}\n"
    "\\end{enumerate}\n\n"
    "The map turns the raw scores into class probabilities for the reader.\n\n"
    "\\begin{enumerate}[start=2]\n"
    f"  \\item {_STEP2}\n"
    f"  \\item {_STEP3}\n"
    "\\end{enumerate}\n")


class TestYerelModel:
    @pytest.mark.sahte_sunucu
    def test_baslangic_secenegi_korunur(self, ollama_translator):
        lt, srv = ollama_translator("en")
        out = lt.translate(_LISTE_GOVDE)
        assert "\\begin{enumerate}[start=2]\n" in out
        sent = [b["messages"][-1]["content"]
                for b in srv.requests_to("/api/chat")]
        assert sent, "sahte modele hiç istek gitmedi"
        assert not any("start=" in t for t in sent)
        # Maddeler çevrildi; numaralar (LaTeX sayacı) kaynakla aynı.
        nums = [n for n, _t in _basilan(out)]
        assert nums == [1, 2, 3]

    @pytest.mark.sahte_sunucu
    def test_uctan_uca_zincir(self, fake_llm_server, monkeypatch, pipeline_env):
        # translate_book.run_pipeline: Phase 2 (dönüştürücü, full_book.md'den)
        # -> yerel model (sahte Ollama) -> QC + autofix + yeniden çevirmen
        # AÇIK. Seçenek kaynak gövdede doğar, çeviride aynen kalır.
        import translate_book as tb
        from core.mt_engines import discovery
        from core.mt_engines.contracts import SourcesConfig

        srv = fake_llm_server("ollama", models=["sahte"], reply=_fake_turkish)
        sources = SourcesConfig(ollama_url=srv.url,
                                lmstudio_url="http://127.0.0.1:9/v1",
                                openai_servers=())
        monkeypatch.setattr(discovery, "load_sources", lambda path=None: sources)
        out = pipeline_env / "liste_kitap"
        (out / "source").mkdir(parents=True)
        (out / "full_book.md").write_text("# Steps\n\n" + _KESINTILI_DENKLEM,
                                          encoding="utf-8")
        code = tb.run_pipeline(str(out), "Liste Kitap", skip_translate=False,
                               skip_compile=True, skip_qc=False,
                               book_type="science", pdf_path=None, resume=False,
                               mt_engine="ollama:sahte")
        assert code in (0, 3), code   # 3: sahte sözcük çevirisi QC eşiği altında
        src = (out / "source" / "body.tex").read_text(encoding="utf-8")
        tr = (out / "source" / "translated_body.tex").read_text(encoding="utf-8")
        assert "\\begin{enumerate}[start=2]\n" in src
        assert "\\begin{enumerate}[start=2]\n" in tr
        assert [n for n, _t in _basilan(tr)] == [1, 2, 3]
        sent = [b["messages"][-1]["content"] for b in srv.requests_to("/api/chat")]
        assert sent and not any("start=" in t for t in sent)

    def test_liste_satiri_yer_tutucuya_alinir(self):
        from latex_pipeline.translator import _LIST_ENV_RE
        for line in ("\\begin{enumerate}[start=12]", "\\begin{enumerate}",
                     "\\end{enumerate}", "\\begin{itemize}"):
            m = _LIST_ENV_RE.fullmatch(line)
            assert m is not None, line


# ===========================================================================
# QC: seçenek liste/ortam denetimlerini değiştirmez
# ===========================================================================
_EN_QC = (
    "\\chapter{Training Steps}\n\n"
    "The section lists the changes that turn the network into a classifier.\n\n"
    "\\begin{enumerate}\n"
    "  \\item Pick an output map that turns raw scores into probabilities.\n"
    "\\end{enumerate}\n\n"
    "\\[\np_i = \\frac{e^{z_i}}{\\sum_j e^{z_j}}\n\\]\n\n"
    "\\begin{enumerate}[start=2]\n"
    "  \\item Encode every training label as a one-hot vector of size 3.\n"
    "  \\item Prefer the cross-entropy loss over the squared error loss.\n"
    "\\end{enumerate}\n")
_TR_QC = (
    "\\chapter{Eğitim Adımları}\n\n"
    "Bu bölüm, ağı bir sınıflandırıcıya dönüştüren değişiklikleri sıralar.\n\n"
    "\\begin{enumerate}\n"
    "  \\item Ham puanları olasılıklara çeviren bir çıkış eşlemesi seçin.\n"
    "\\end{enumerate}\n\n"
    "\\[\np_i = \\frac{e^{z_i}}{\\sum_j e^{z_j}}\n\\]\n\n"
    "\\begin{enumerate}[start=2]\n"
    "  \\item Her eğitim etiketini 3 boyutlu bir tek-sıcak vektör olarak kodlayın.\n"
    "  \\item Bu görevde kareli hata yerine çapraz entropi kaybını tercih edin.\n"
    "\\end{enumerate}\n")


# Denetimin gerçekten koştuğunu göstermek için çeviride bilerek kaynak dilde
# bırakılmış bir paragraf (iki tarafta da aynı bulguyu üretir).
_KALAN_EN = ("The last remark was left in English on purpose so that the "
             "quality check has something to report here.")


def _qc_bulgular(tr: str, en: str):
    from latex_pipeline.qc_engine import QCEngine
    rep = QCEngine().analyze(tr, en)
    return (Counter((i.check_name, i.severity, i.line_num) for i in rep.issues),
            rep.score)


class TestQC:
    def test_secenek_yeni_bulgu_uretmez(self):
        en = _EN_QC + "\n" + _KALAN_EN + "\n"
        tr = _TR_QC + "\n" + _KALAN_EN + "\n"
        with_opt, score = _qc_bulgular(tr, en)
        bare, score_bare = _qc_bulgular(tr.replace("[start=2]", ""),
                                        en.replace("[start=2]", ""))
        assert with_opt, "QC hiç bulgu üretmedi (denetim koşmadı mı?)"
        assert with_opt == bare and score == score_bare


# ===========================================================================
# Şablon + sanitizer: seçenek derlemeye kadar sağ kalır
# ===========================================================================
class TestDerleme:
    def test_sablon_enumitem_yukler(self):
        # [start=N] enumitem anahtarıdır. Paket şablondan çıkarılırsa
        # "[start=2]" liste başında düz metin basılır, LaTeX "perhaps a
        # missing \item" hatası verir ve liste yine 1'den sayar. Kilit.
        tpl = (KOK / "latex_pipeline/templates/turkish_book.tex").read_text(encoding="utf-8")
        assert re.search(r"^\\usepackage(\[[^\]]*\])?\{enumitem\}", tpl, re.M)

    @pytest.mark.parametrize("manual", [True, False])
    def test_sanitizer_secenegi_korur(self, manual):
        from latex_pipeline.sanitizer import LatexSanitizer
        out, _rep = LatexSanitizer(manual_translation=manual).sanitize(_TR_QC)
        assert "\\begin{enumerate}[start=2]\n" in out
        assert [n for n, _t in _basilan(out)] == [1, 2, 3]
