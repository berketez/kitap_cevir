"""Kod çitindeki düzyazı alıntının kalın/italik vurgusu (latex_pipeline.kod_vurgu).

6 Ekim 2026, D kulvarı. Marker kaynaktaki eş aralıklı düzyazı alıntıları
(ajan akıl yürütmesi) kod çiti yazar ve kutudaki kalını düşürür; dönüştürücü
çiti lstlisting (şablonda Verbatim) yapıyordu, içinde \\textbf çalışmaz.
Yeni akış: ``latex_pipeline.kod_vurgu`` çit içeriğini kaynak PDF'in karakter
akışıyla hizalar, yazı tipi adından kalın/italik koşuları çıkarır; dönüştürücü
düzyazı çitini (``sanitizer.listing_is_prose``) doğrudan sanitizer'ın alıntı
biçiminde, koşular ``\\textbf{}`` / ``\\textit{}`` olarak basar.

Cümleler ve PDF'ler SENTETİKTİR (KARAR 26: testlere kitap metni kopyalanmaz).
Sentetik PDF: PyMuPDF'in yerleşik yazı tipleri ("cour" Courier, "cobo"
Courier-Bold, "coit" Courier-Oblique, "cobi" Courier-BoldOblique, "helv"
Helvetica).
"""
from __future__ import annotations

import re

import pytest

from latex_pipeline import kod_vurgu as kv
from latex_pipeline import vurgu_onarim as vo
from latex_pipeline.config import PipelineConfig
from latex_pipeline.converter import MarkdownToLatex
from latex_pipeline.sanitizer import LatexSanitizer, listing_to_quote

fitz = pytest.importorskip("fitz")

_PUNTO = 10
_SATIR = 14
# Metin katmanı EN_AZ_PDF_METNI'ni (200) aşsın; içerikle karışmayan dolgu.
_DOLGU = [
    [("Neutral filler prose keeps this synthetic layer long enough.", "helv")],
    [("Second filler line mentions orchards, ferries and quiet harbours.", "helv")],
    [("Third filler line closes the page with plain words and 42 numbers.", "helv")],
]


def _sayfa_yaz(page, satirlar, y=72):
    for parcalar in satirlar:
        x = 72
        for metin, font in parcalar:
            page.insert_text((x, y), metin, fontname=font, fontsize=_PUNTO)
            x += fitz.get_text_length(metin, fontname=font, fontsize=_PUNTO)
        y += _SATIR
    return y


def _pdf(tmp_path, *sayfalar, ad="kaynak.pdf", dolgu=True):
    """Her sayfa: satır listesi; satır: [(metin, yazı tipi)] yan yana."""
    doc = fitz.open()
    for k, satirlar in enumerate(sayfalar):
        page = doc.new_page()
        _sayfa_yaz(page, list(satirlar) + (_DOLGU if dolgu and k == 0 else []))
    path = tmp_path / ad
    doc.save(str(path))
    doc.close()
    return str(path)


# --- Sentetik kutular ------------------------------------------------------

# Kalın koşu satır sonunu aşıyor; tırnak koşunun dışında kalmalı.
_K1_PDF = [
    [("“Should we tidy the shared notes first? ", "cour"), ("Maybe", "cobo")],
    [("nobody reads the margins anyway?", "cobo"), (" Fine, notes it is.”", "cour")],
]
_K1_MD = ('"Should we tidy the shared notes first? Maybe\n'
          'nobody reads the margins anyway? Fine, notes it is."')
# Tümü italik, dış süslü ayraçlı yeniden ifade.
_K2_PDF = [
    [("{[Calm] the archive took our extra label, so later", "coit")],
    [("checks may go faster and nobody has to wait.}", "coit")],
]
_K2_MD = ("{[Calm] the archive took our extra label, so later\n"
          "checks may go faster and nobody has to wait.}")
# Kalın italik koşu.
_K3_PDF = [
    [("We could rename the drafts tonight, ", "cour"),
     ("then nobody would notice", "cobi"), (" the swap at all.", "cour")],
]
_K3_MD = "We could rename the drafts tonight, then nobody would notice the swap at all."


def _quote_govde(latex: str) -> str:
    m = re.search(r"\\begin\{quote\}\n\\raggedright\\ttfamily (.*?)\n\\end\{quote\}",
                  latex, re.S)
    assert m, latex
    return m.group(1)


# ---------------------------------------------------------------------------
# Yazı tipi -> vurgu
# ---------------------------------------------------------------------------

class TestPdfStili:
    @pytest.mark.parametrize("font,beklenen", [
        ("ABCDEF+CourierNew", 0),
        ("GAAAAA+CourierNew-Bold", kv.KALIN),
        ("MAAAAA+RobotoMono-Bold", kv.KALIN),
        ("RobotoMono-Regular", 0),
        ("Courier-Oblique", kv.ITALIK),
        ("CourierNew-Italic", kv.ITALIK),
        ("Courier-BoldOblique", kv.KALIN | kv.ITALIK),
        ("Menlo-Bold", kv.KALIN),
        ("CMMI10", -1),
        ("", -1),
    ])
    def test_ad_olcutu(self, font, beklenen):
        assert kv.pdf_stili(font) == beklenen

    def test_vurgu_onarimi_es_araligi_keser_bu_modul_kesmez(self):
        # vurgu_onarim kod yazı tipini bilerek dışarıda bırakır (-1); bu
        # modülün işi tam da o yazı tipleri.
        assert vo.font_stili("CourierNew-Bold") == -1
        assert kv.pdf_stili("CourierNew-Bold") == kv.KALIN


# ---------------------------------------------------------------------------
# Hizalama ve koşular
# ---------------------------------------------------------------------------

class TestKosular:
    def test_satir_sonunu_asan_kalin_kosu(self, tmp_path):
        pdf = _pdf(tmp_path, _K1_PDF)
        latex, bagla = kv.duzyazi_kod_blogu(_K1_MD, pdf)
        assert not bagla
        assert _quote_govde(latex) == (
            '"Should we tidy the shared notes first? '
            '\\textbf{Maybe nobody reads the margins anyway?} Fine, notes it is."')

    def test_tumu_italik_kutu(self, tmp_path):
        pdf = _pdf(tmp_path, _K2_PDF)
        latex, _ = kv.duzyazi_kod_blogu(_K2_MD, pdf)
        assert _quote_govde(latex) == (
            "\\textit{\\{[Calm] the archive took our extra label, so later "
            "checks may go faster and nobody has to wait.\\}}")

    def test_kalin_italik_kosu(self, tmp_path):
        pdf = _pdf(tmp_path, _K3_PDF)
        latex, _ = kv.duzyazi_kod_blogu(_K3_MD, pdf)
        assert "\\textbf{\\textit{then nobody would notice}} the swap" in latex

    def test_vurgusuz_kutu_sanitizer_bicimiyle_aynı(self, tmp_path):
        md = ("The plan was simple enough, we would wait and then report\n"
              "the outcome to the others before the evening meeting.")
        pdf = _pdf(tmp_path, [[(md.split("\n")[0], "cour")], [(md.split("\n")[1], "cour")]])
        latex, bagla = kv.duzyazi_kod_blogu(md, pdf)
        assert (latex, bagla) == (listing_to_quote(md), False)

    def test_kod_duzyazi_degil_none(self, tmp_path):
        pdf = _pdf(tmp_path, [[("for item in items:", "cobo")], [("    total += item", "cour")]])
        assert kv.duzyazi_kod_blogu("for item in items:\n    total += item", pdf) is None

    def test_pdf_yoksa_vurgusuz_alinti(self):
        rapor = kv.KodVurguRaporu()
        latex, bagla = kv.duzyazi_kod_blogu(_K1_MD, None, rapor=rapor)
        assert (latex, bagla) == (listing_to_quote(_K1_MD), False)
        assert rapor.sayac.get("pdf_yok") == 1

    def test_pdfte_bulunamayan_icerik_vurgusuz(self, tmp_path):
        pdf = _pdf(tmp_path, _K1_PDF)
        md = ("An unrelated remark about lanterns, kettles and the long winter\n"
              "evenings that nobody here has written down anywhere yet.")
        rapor = kv.KodVurguRaporu()
        latex, _ = kv.duzyazi_kod_blogu(md, pdf, rapor=rapor)
        assert latex == listing_to_quote(md)
        assert rapor.sayac.get("bulunamadi") == 1

    def test_farkli_vurguyla_iki_kez_gecen_icerik_belirsiz(self, tmp_path):
        pdf = _pdf(tmp_path, [
            [("We could rename the drafts tonight, ", "cour"),
             ("then nobody would notice", "cobo"), (" the swap at all.", "cour")],
            [("We could rename the drafts tonight, then nobody would notice the swap at all.",
              "cour")],
        ])
        rapor = kv.KodVurguRaporu()
        latex, _ = kv.duzyazi_kod_blogu(_K3_MD, pdf, rapor=rapor)
        assert "\\textbf" not in latex and rapor.sayac.get("belirsiz") == 1

    def test_ayni_vurguyla_iki_kez_gecen_icerik(self, tmp_path):
        satir = [("We could rename the drafts tonight, ", "cour"),
                 ("then nobody would notice", "cobo"), (" the swap at all.", "cour")]
        pdf = _pdf(tmp_path, [satir, satir])
        latex, _ = kv.duzyazi_kod_blogu(_K3_MD, pdf)
        assert "\\textbf{then nobody would notice} the swap" in latex

    def test_sayfada_bolunen_kutu_yaklasik_hizalanir(self, tmp_path):
        # Kutunun ilk iki satırı 1. sayfanın sonunda, üçüncüsü 2. sayfada;
        # akışta araya dipnot satırı, sayfa numarası ve üst bilgi girer.
        sayfa1 = list(_DOLGU) + [
            [("“The library seems busy tonight, so we might post our findings", "cour")],
            [("tomorrow morning after the second batch completes and the", "cour")],
            [("12 A synthetic footnote explains the batch numbering scheme.", "helv")],
            [("7", "helv")],
        ]
        sayfa2 = [
            [("Synthetic Report on Shared Notes", "helv")],
            [("logs look tidy. ", "cour"), ("Nobody will object to waiting.", "cobo"),
             ("”", "cour")],
        ]
        pdf = _pdf(tmp_path, sayfa1, sayfa2, dolgu=False)
        md = ('"The library seems busy tonight, so we might post our findings\n'
              'tomorrow morning after the second batch completes and the\n'
              'logs look tidy. Nobody will object to waiting."')
        rapor = kv.KodVurguRaporu()
        latex, _ = kv.duzyazi_kod_blogu(md, pdf, rapor=rapor)
        assert rapor.sayac.get("yaklasik") == 1
        assert "\\textbf{Nobody will object to waiting.}\"" in latex
        assert latex.replace("\\textbf{", "").replace("}", "") == \
            listing_to_quote(md).replace("}", "")

    def test_uzun_icerikte_yaklasik_hizalama_denenmez(self, tmp_path, monkeypatch):
        # Tam eşleşmesi olmayan uzun içerik: karakter hizalaması yapılmaz.
        pdf = _pdf(tmp_path, [[("“The library seems busy tonight, so we might post", "cour")],
                              [("12", "helv")],
                              [("our findings later. ", "cour"), ("Nobody minds waiting.", "cobo"),
                               ("”", "cour")]])
        md = ('"The library seems busy tonight, so we might post\n'
              'our findings later. Nobody minds waiting."')
        monkeypatch.setattr(kv, "YAKLASIK_EN_UZUN", 20)
        rapor = kv.KodVurguRaporu()
        latex, _ = kv.duzyazi_kod_blogu(md, pdf, rapor=rapor)
        assert rapor.sayac.get("uzun") == 1 and latex == listing_to_quote(md)

    def test_kisa_kalin_parca_vurgu_sayilmaz(self, tmp_path):
        # Tek başına kalın üç nokta / tırnak (KOSU_EN_AZ_HARF) vurgu değildir.
        pdf = _pdf(tmp_path, [[("We waited for the reply", "cour"), (" ... ", "cobo"),
                               ("and then quietly logged off for the night.", "cour")]])
        md = "We waited for the reply ... and then quietly logged off for the night."
        latex, _ = kv.duzyazi_kod_blogu(md, pdf)
        assert latex == listing_to_quote(md)


# ---------------------------------------------------------------------------
# Kayıpsızlık: vurgusuz hâl sanitizer'ın biçimiyle birebir
# ---------------------------------------------------------------------------

class TestKayipsizlik:
    _MD = ("We tagged the run #2 and saw 40% gains & one spare_token here,\n"
           "which looked odd to everyone reading the shared page.\n"
           "\n"
           "Later we checked again and the numbers held up, so we moved on\n"
           "to the next task without telling anyone about the oddity.")

    def test_ozel_karakterler_ve_paragraflar(self):
        # Koşu boş satırı aşıyor: paragraf sonunda kapanır, sonrakinde açılır.
        bas = self._MD.index("page.")
        son = self._MD.index("numbers") + len("numbers")
        vurgulu, vurgusuz = kv.alinti_latex(self._MD, [(bas, son, kv.KALIN)])
        assert vurgusuz == listing_to_quote(self._MD)
        assert "\\#2" in vurgulu and "40\\%" in vurgulu and "\\&" in vurgulu
        assert "spare\\_token" in vurgulu
        assert "\\textbf{page.}\n\n\\textbf{Later we checked again and the numbers}" in vurgulu

    def test_denetim_tutmazsa_vurgusuz_bicim(self, tmp_path, monkeypatch):
        pdf = _pdf(tmp_path, _K1_PDF)
        monkeypatch.setattr(kv, "listing_to_quote", lambda kod: "BEKLENEN")
        rapor = kv.KodVurguRaporu()
        latex, _ = kv.duzyazi_kod_blogu(_K1_MD, pdf, rapor=rapor)
        assert latex == "BEKLENEN" and rapor.sayac.get("denetim_tutmadi") == 1


# ---------------------------------------------------------------------------
# Dönüştürücü
# ---------------------------------------------------------------------------

_KITAP = "# A Synthetic Book Title\n\nJane Q. Writer\n\n## First chapter\n\n"


def _govde(md: str, **cfg) -> str:
    out = MarkdownToLatex(PipelineConfig(document_class="book", **cfg)).convert(_KITAP + md)
    return out.split("\\section*{First chapter}", 1)[-1]


def _cit(icerik: str, dil: str = "") -> str:
    return f"```{dil}\n{icerik}\n```\n"


class TestDonusturucu:
    def test_duzyazi_cit_kalinli_alinti_olur(self, tmp_path):
        pdf = _pdf(tmp_path, _K1_PDF)
        out = _govde("Some travellers wrote:\n\n" + _cit(_K1_MD) + "\nAfterwards.\n",
                     input_pdf=pdf)
        assert "lstlisting" not in out
        assert ("\\begin{quote}\n\\raggedright\\ttfamily \"Should we tidy the shared notes "
                "first? \\textbf{Maybe nobody reads the margins anyway?} Fine, notes it is.\"\n"
                "\\end{quote}") in out
        # Hiçbir işaret karakteri sızmaz.
        assert "**" not in out and "`" not in out
        assert not re.search(r"[\x00-\x08\x0b-\x1f\ue000-\uf8ff]", out)

    def test_italik_kutu(self, tmp_path):
        pdf = _pdf(tmp_path, _K2_PDF)
        out = _govde("A traveller noted:\n\n" + _cit(_K2_MD) + "\nAfterwards.\n", input_pdf=pdf)
        assert ("\\raggedright\\ttfamily \\textit{\\{[Calm] the archive took our extra "
                "label, so later checks may go faster and nobody has to wait.\\}}") in out

    def test_gercek_kod_lstlisting_kalir(self, tmp_path):
        pdf = _pdf(tmp_path, [[("for item in items:", "cobo")], [("    total += item", "cour")]])
        kod = "for item in items:\n    total += item"
        out = _govde("Code:\n\n" + _cit(kod) + "\nAfterwards.\n", input_pdf=pdf)
        assert f"\\begin{{lstlisting}}\n{kod}\n\\end{{lstlisting}}" in out

    def test_dil_secenekli_cit_lstlisting_kalir(self, tmp_path):
        pdf = _pdf(tmp_path, _K1_PDF)
        out = _govde("Code:\n\n" + _cit(_K1_MD, "text") + "\nAfterwards.\n", input_pdf=pdf)
        assert "\\begin{lstlisting}[language=text]" in out and "\\textbf" not in out

    def test_pdfsiz_kaynakta_eski_davranis(self):
        # EPUB yolu (kaynak PDF yok): çit lstlisting kalır; düzyazı kutuyu
        # derlemede sanitizer alıntıya çevirir (G3a-2).
        out = _govde("Some travellers wrote:\n\n" + _cit(_K1_MD) + "\nAfterwards.\n")
        assert f"\\begin{{lstlisting}}\n{_K1_MD}\n\\end{{lstlisting}}" in out

    def test_yarim_cumleyi_surduren_kutu_paragrafa_baglanir(self, tmp_path):
        # Önceki paragraf harfle bitiyor, kutu küçük harfle başlıyor: kutu
        # yarım cümlenin devamıdır (sanitizer.listing_continues_paragraph).
        govde = "The courier compared both routes and feared that leaving at dawn would waste"
        kutu1 = 'our only daylight." Then the others urged it to go regardless. It replied: "My'
        kutu2 = "lantern is nearly empty anyway."
        pdf = _pdf(tmp_path, [[(govde, "helv")], [(kutu1, "cour")],
                              [(kutu2 + " ", "cour"), ("Leaving now is wise.", "cobo"),
                               ('"', "cour")]])
        md = govde + "\n\n" + _cit(kutu1 + "\n" + kutu2 + ' Leaving now is wise."') + "\nNext.\n"
        out = _govde(md, input_pdf=pdf)
        assert "quote" not in out and "lstlisting" not in out
        assert (govde + " " + kutu1 + " " + kutu2
                + ' \\textbf{Leaving now is wise.}"') in out
        assert re.search(r'wise\.\}"\n\nNext\.', out)

    def test_sanitizer_alintiya_dokunmaz(self, tmp_path):
        pdf = _pdf(tmp_path, _K1_PDF)
        govde = _govde("Some travellers wrote:\n\n" + _cit(_K1_MD) + "\nAfterwards.\n",
                       input_pdf=pdf)
        doc = ("\\documentclass{book}\n\\usepackage{hyperref}\n\\begin{document}\n"
               + govde + "\n\\end{document}\n")
        out, _ = LatexSanitizer(manual_translation=True).sanitize(doc)
        assert "\\textbf{Maybe nobody reads the margins anyway?}" in out
        assert out.count("\\begin{quote}") == 1 and "lstlisting" not in out

    def test_rapor_sayaci(self, tmp_path):
        pdf = _pdf(tmp_path, _K1_PDF)
        conv = MarkdownToLatex(PipelineConfig(document_class="book", input_pdf=pdf))
        conv.convert(_KITAP + "Some travellers wrote:\n\n" + _cit(_K1_MD) + "\nAfterwards.\n")
        assert conv.kod_vurgu_raporu.sayac.get("duzyazi_alinti") == 1
        assert conv.kod_vurgu_raporu.sayac.get("kalin") == 1
