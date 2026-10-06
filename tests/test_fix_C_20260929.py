"""Dönüştürücü düzeltme dalgası — Kulvar C (29 Eylül 2026).

Bug avının (2026-09-28) doğrulanmış bulguları ve kararlar:

  F1-04 / F2-04  Marker'ın kaçışlı yıldızı "\\*" (ve alt çizgisi "\\_") biçim
                 kalıplarından önce düz karakter olur: iki "\\*" italik
                 sanılıp "\\textit{…\\}" dengesiz grup üretiyordu (derleme
                 "File ended while scanning use of \\textit" ile duruyordu);
                 tek "\\*" LaTeX'te kesme noktası komutudur (yıldız basılmıyor,
                 U+0002 eksik glifi).
  F1-05          Marker bağ metni kaçışları ("\\(" "\\)" "\\[" "\\]" "\\\\_")
                 geçersiz TeX'e dönüşüyordu; tablo hücresi matematiğinde ortam
                 dışı "\\\\" tabular satırını bitiriyordu; "\\\\ [" satır aralığı
                 argümanı sanılıyordu. Eski ön işlem 7. adımı atıf ayraçlarını
                 siliyordu ("[4]" -> "4").
  F1-10          PDF (Marker) sayfa altı dipnotu "<sup>N</sup>Metin" gövdedeki
                 işaretin yerinde \\footnote olur; eşleşmeyen tanım/işaret
                 görünür kalır (kayıpsız).
  F1-15          Aynı Marker düzeyindeki alt başlıkların düzeyi kaynak PDF'in
                 puntosuyla ayrılır (yalnız güvenli durumda).
  KARAR 20       Kaynakça kaynaktaki gibi numaralı/numarasız; numarasız olan
                 İçindekiler'de (başlık metni bir kez yazılır).
  KARAR 21       Harfli başlık ("(a) X", "*a*) X"): harf metinde kalır, LaTeX
                 numarası kalkar, İçindekiler'de kalır.

Tüm girdiler SENTETİKTİR (kitaptan düzyazı alınmaz). Gerçek model yüklenmez.
"""

from __future__ import annotations

import logging
import re

import pytest

from latex_pipeline.config import PipelineConfig
from latex_pipeline.converter import MarkdownToLatex, _tex_balanced

TOC_ENTRY = r"\addcontentsline{toc}{%s}{\csname @currentlabelname\endcsname}"


@pytest.fixture
def conv(tmp_path):
    def _run(markdown: str, document_class: str = "book", **cfg_kwargs) -> str:
        cfg = PipelineConfig(document_class=document_class,
                             output_dir=str(tmp_path), **cfg_kwargs)
        out = MarkdownToLatex(cfg).convert(markdown)
        assert "\x02" not in out and "\x00" not in out, "nöbetçi/yer tutucu sızdı"
        return out
    return _run


_BODY = "Sentetik gövde paragrafı burada yer alır ve bir cümleden oluşur.\n\n"


def _chapter(md: str) -> str:
    return "# Chapter 1 Synthetic Topic\n\n" + _BODY + md + "\n"


def _line_with(tex: str, needle: str) -> str:
    hits = [ln for ln in tex.split("\n") if needle in ln]
    assert hits, f"{needle!r} çıktıda yok"
    return hits[0]


def _headings(tex: str) -> list[str]:
    return [ln for ln in tex.split("\n")
            if re.match(r"\\(part|chapter|section|subsection|subsubsection)\*?\{", ln)]


# ===========================================================================
# F1-04 / F2-04: kaçışlı yıldız ve alt çizgi
# ===========================================================================

class TestF104KacisliYildiz:
    def test_iki_kacisli_yildiz_italik_sanilmaz(self, conv):
        out = conv(_chapter("The product is 0.5\\*0.5 = 0.25 and 0.2\\*0.8 = 0.16 here."))
        line = _line_with(out, "0.25")
        assert "\\textit" not in line
        assert "0.5*0.5 = 0.25 and 0.2*0.8 = 0.16" in line
        assert _tex_balanced(line)

    @pytest.mark.parametrize("md, beklenen", [
        ("Only one factor 3\\*x appears.", "3*x"),
        ("The velocity C\\* is defined.", "C*"),
        ("Proceedings (FAT\\*'19) were.", "(FAT*'19)"),
        ("Note that asterisk mark, (\\*) corresponds.", "(*)"),
    ])
    def test_tek_kacisli_yildiz_basilir(self, conv, md, beklenen):
        line = _line_with(conv(_chapter(md)), beklenen)
        assert "\\*" not in line

    def test_italik_icinde_ve_yaninda_kacisli_yildiz(self, conv):
        out = conv(_chapter("Ratio *A*/*A\\**, length *L*\\* and 2\\**Exposure* here."))
        line = _line_with(out, "Ratio")
        assert "\\textit{A}/\\textit{A*}" in line
        assert "\\textit{L}*" in line
        assert "2*\\textit{Exposure}" in line
        assert _tex_balanced(line)

    def test_kacisli_alt_cizgi_italik_sanilmaz(self, conv):
        out = conv(_chapter("Message qqR\\_STEPNINE\\_4718\\_UT2093\\_\\_likely\\_x ends."))
        line = _line_with(out, "STEPNINE")
        assert "\\textit" not in line
        assert "qqR\\_STEPNINE\\_4718\\_UT2093\\_\\_likely\\_x" in line

    def test_gercek_italik_ve_kalin_bozulmaz(self, conv):
        line = _line_with(conv(_chapter("Plain *it* and **bold** and _u_ word.")), "Plain")
        assert "\\textit{it}" in line and "\\textbf{bold}" in line
        assert "\\textit{u}" in line

    def test_matematik_ve_kod_icindeki_kacisa_dokunulmaz(self, conv):
        line = _line_with(conv(_chapter("Math $a\\*b$ and `c\\*d` end.")), "Math")
        assert "$a\\*b$" in line
        # 2026-10-05: kod parçasında kaçış yok (CommonMark): ters bölü düz
        # karakter basılır ("\\texttt{c\\*d}" yıldızı hiç basmıyordu).
        assert "\\texttt{c\\textbackslash{}*d}" in line

    def test_tablo_hucresinde_kacisli_yildiz(self, conv):
        out = conv(_chapter("| Name | Value |\n|---|---|\n| \\*note | a\\*b |\n"))
        row = _line_with(out, "note")
        # Satır başındaki yıldız bir önceki "\\" ile "\\*" okunmasın.
        assert row.startswith("{}*note") and "a*b" in row
        assert "\\*" not in row.replace("\\\\", "")


# ===========================================================================
# F1-05: bağ metni kaçışları, atıf ayraçları, tablo matematiği
# ===========================================================================

class TestF105BagMetni:
    def test_acilis_paren_bag_disina(self, conv):
        line = _line_with(conv(_chapter("Hallworth [\\(2009](#page-53-0)) wrote.")), "Hallworth")
        assert "(\\href{#page-53-0}{2009})" in line
        assert "\\(" not in line and "\\)" not in line

    def test_kapanis_paren_bag_disina(self, conv):
        line = _line_with(conv(_chapter("Smith [2009\\)](#page-361-0). Then.")), "Smith")
        assert "\\href{#page-361-0}{2009})." in line
        assert "\\)" not in line

    def test_dengeli_paren_icerde_kalir(self, conv):
        line = _line_with(conv(_chapter("See [\\(6.3\\)](#page-152-0) now.")), "See")
        assert "\\href{#page-152-0}{(6.3)}" in line

    def test_bolunmus_atif_ayraclari_korunur(self, conv):
        line = _line_with(conv(_chapter(
            "The method [\\[37,](#page-212-0) [42\\]](#page-212-0) is popular.")), "method")
        assert "[\\href{#page-212-0}{37,} \\href{#page-212-0}{42}]" in line
        assert _tex_balanced(line)

    @pytest.mark.parametrize("md, beklenen", [
        ("Hobbs [\\[ 48](#page-163-0)] who.", "[\\href{#page-163-0}{48}] who."),
        ("tube [[7\\]](#page-152-0) with.", "[\\href{#page-152-0}{7}] with."),
        ("chars [[3,5](#page-152-0)], which.", "[\\href{#page-152-0}{3,5}], which."),
        ("by [\\[7\\]](\\#page-152-0) for.", "[\\href{#page-152-0}{7}] for."),
    ])
    def test_eski_on_islem_ayraci_silmez(self, conv, md, beklenen):
        # Eski 7. ön işlem adımı "[4]" -> "4", "[1,2]" -> "1,2]" yapıyordu.
        assert beklenen in conv(_chapter(md))

    def test_cift_kacisli_alt_cizgi_ve_parantezli_adres(self, conv):
        md = ("Wiki [http://en.wikipedia.org/wiki/Jane\\\\_Doe\\\\_\\(engraver\\)]"
              "(http://en.wikipedia.org/wiki/Jane_Doe_(engraver)). Accessed")
        line = _line_with(conv(_chapter(md)), "Wiki")
        assert ("\\href{http://en.wikipedia.org/wiki/Jane_Doe_(engraver)}"
                "{http://en.wikipedia.org/wiki/Jane\\_Doe\\_(engraver)}. Accessed") in line
        assert "\\textit" not in line

    def test_adreste_eslesmeyen_parantez(self, conv):
        md = ("Wiki [http://x.org/wiki/G\\\\_D\\\\_\\(w\\)](http://x.org/wiki/G_D_(w). Accessed")
        line = _line_with(conv(_chapter(md)), "Wiki")
        assert "\\href{http://x.org/wiki/G_D_(w}{http://x.org/wiki/G\\_D\\_(w)}. Accessed" in line
        assert "\\textit" not in line

    def test_baslikta_bag_kacisi(self, conv):
        out = conv(_chapter("## 1.1 Section\n\n" + _BODY
                            + "### *[1.1.1 Liquid Fuel \\(Kerosene\\)](#page-14-0)*\n\n" + _BODY
                            + "### *[1.1.2 Oxidizer \\(NH2OH\\\\*NO3\\)](#page-14-0)*\n\n" + _BODY))
        h = _headings(out)
        assert any("Liquid Fuel (Kerosene)" in x for x in h), h
        assert any("Oxidizer (NH2OH*NO3)" in x for x in h), h
        assert not any("\\(" in x or "\\\\*" in x for x in h), h

    def test_gorsel_ve_normal_bag_degismez(self, conv):
        line = _line_with(conv(_chapter("A [link](http://x.com/a_b) and [2](#page-19-0).")),
                          "link")
        assert "\\href{http://x.com/a_b}{link}" in line
        assert "\\href{#page-19-0}{2}" in line


class TestF105TabloMatematik:
    def test_hucre_matematigindeki_satir_sonu_array(self, conv):
        out = conv(_chapter("| Planet | Value |\n|---|---|\n"
                            "| Mars | $1.52371034 \\\\ (0.00001847)$ |\n"))
        row = _line_with(out, "Mars")
        assert "$\\begin{array}{@{}l@{}}1.52371034 \\\\ (0.00001847)\\end{array}$" in row

    def test_ortam_icindeki_satir_sonu_sarilmaz_ama_koseli_ayrac_korunur(self, conv):
        out = conv(_chapter("| Planet | Radius |\n|---|---|\n"
                            "| X | $\\begin{array}{c} \\textbf{R} \\\\ [\\text{AU}] \\end{array}$ |\n"))
        row = _line_with(out, "\\textbf{R}")
        assert "\\\\{} [\\text{AU}]" in row
        assert row.count("\\begin{array}") == 1

    def test_display_matematikte_uzunluk_olmayan_koseli_ayrac(self, conv):
        md = ("$$y = \\begin{cases} [1, 0]^{\\top} & a, \\\\ [0, 1]^{\\top} & b "
              "\\end{cases}$$")
        out = conv(_chapter(md))
        assert "\\\\{} [0, 1]^{\\top}" in out

    @pytest.mark.parametrize("uzunluk", ["[2pt]", "[-1ex]", "[0.5\\baselineskip]", "[\\jot]"])
    def test_gercek_satir_araligi_dokunulmaz(self, conv, uzunluk):
        out = conv(_chapter("$$\\begin{aligned} a &= b \\\\" + uzunluk + " c &= d \\end{aligned}$$"))
        assert "\\\\" + uzunluk in out and "\\\\{}" not in out


# ===========================================================================
# F1-10: PDF sayfa altı dipnotları
# ===========================================================================

class TestF110SayfaDipnotu:
    def test_tanim_isaretin_yerinde_footnote(self, conv):
        md = _chapter("The heat flux follows the gradient.<sup>1</sup> The minus sign "
                      "shows the direction.\n\nAnother paragraph of text.\n\n"
                      "<sup>1</sup>In English texts the symbol k is used.\n\nNext text.")
        out = conv(md)
        line = _line_with(out, "heat flux")
        # KARAR 31 (FX1): eşlenen dipnot kaynak numarasıyla (\footnote[N]).
        assert ("gradient.\\footnote[1]{In English texts the symbol k is used.} The minus"
                in line)
        assert "\\textsuperscript" not in out
        assert out.count("In English texts the symbol k is used.") == 1

    def test_matematige_sarili_tanim_ve_bosluklu_isaret(self, conv):
        md = _chapter("Dicke der Schicht *i*. <sup>2</sup> Praktisch geht man vor.\n\n"
                      " $<sup>^2</sup>$ Die Widerstände lassen sich addieren.\n")
        line = _line_with(conv(md), "Praktisch")
        assert "\\textit{i}.\\footnote[2]{Die Widerstände lassen sich addieren.} Praktisch" in line

    def test_ayni_sayfada_iki_dipnot(self, conv):
        md = _chapter("First claim here.<sup>1</sup> More text follows.\n\n"
                      "Second claim there.<sup>2</sup>\n\n"
                      "<sup>1</sup>Note one text.\n\n<sup>2</sup>Note two text.\n")
        out = conv(md)
        assert "here.\\footnote[1]{Note one text.}" in out
        assert "there.\\footnote[2]{Note two text.}" in out

    def test_eslesmeyen_tanim_paragraf_kalir_ve_uyarir(self, conv, caplog):
        md = _chapter("Body without any mark 1 here.\n\n<sup>1</sup>Orphan note text.\n")
        with caplog.at_level(logging.WARNING, logger="latex_pipeline.converter"):
            out = conv(md)
        assert "\\textsuperscript{1}Orphan note text." in out
        assert "\\footnote" not in out
        assert any("eşlenemedi" in r.getMessage() for r in caplog.records)

    def test_sayi_parcasi_isaret_sayilmaz(self, conv):
        # Marker "1,5 × 10^-5" sayısını üst simgelere bölmüş; "1" dipnot
        # işareti değildir, dipnot o basamağı silemez.
        md = _chapter("For air *<sup>ν</sup> <sup>≈</sup>* <sup>1</sup>*,*<sup>5</sup> m2/s "
                      "holds. Result 0.0023. 1\n\n<sup>1</sup>Ces résultats supposent un bord.\n")
        out = conv(md)
        assert "\\footnote" not in out
        assert "\\textsuperscript{1}Ces résultats supposent un bord." in out

    def test_bolum_basligini_asmaz(self, conv):
        md = ("# Chapter 1 First\n\nClaim here.<sup>1</sup> Text.\n\n"
              "# Chapter 2 Second\n\nOther body.\n\n<sup>1</sup>Belongs to chapter two.\n")
        out = conv(md)
        assert "\\footnote" not in out
        assert "\\textsuperscript{1}Belongs to chapter two." in out

    def test_devam_satiri_olan_tanim_tasinmaz(self, conv):
        # Tanım tek başına paragraf değilse (devam satırı) bölünmesin.
        md = _chapter("Claim here.<sup>1</sup> Text.\n\n<sup>1</sup>Note first line\n"
                      "continues on a second line.\n")
        out = conv(md)
        assert "\\footnote" not in out
        assert "continues on a second line." in out


# ===========================================================================
# KARAR 20: kaynakça
# ===========================================================================

class TestKarar20Kaynakca:
    def test_numarasiz_kaynakca_icindekilerde_tek_metin(self, conv):
        out = conv(_chapter("## 1.1 Part\n\n" + _BODY + "## References\n\n- [1] Künye.\n"))
        line = _line_with(out, "{References}")
        assert line == "\\section*{References}" + TOC_ENTRY % "section"
        assert line.count("References") == 1

    def test_numarali_kaynakca_numarali_kalir(self, conv):
        out = conv(_chapter("## 1.5 References\n\n- [1] Künye.\n"))
        line = _line_with(out, "References}")
        assert line == "\\section{1.5 References}"

    @pytest.mark.parametrize("ad", ["Literatur", "Bibliografía", "Литература"])
    def test_diller(self, conv, ad):
        out = conv(_chapter("## 1.1 Part\n\n" + _BODY + f"## {ad}\n\n- [1] Künye.\n"))
        assert "\\section*{" + ad + "}" + TOC_ENTRY % "section" in out

    def test_derin_kaynakca_girdisi_icindekiler_derinliginde(self, tmp_path):
        # Marker'ın \subsubsection'a indirdiği bölüm sonu kaynakçası (bir test kitabında):
        # girdi kitabın İçindekiler derinliğinde (subsection) yazılır.
        c = MarkdownToLatex(PipelineConfig(document_class="book",
                                           output_dir=str(tmp_path)))
        c.convert("# Chapter 1 X\n\nMetin.\n")
        c._in_frontmatter, c._frontmatter_ended = False, True
        assert c._try_heading("#### References") == (
            "\\subsubsection*{References}" + TOC_ENTRY % "subsection")
        assert c._try_heading("#### (c) Letter item") == (
            "\\subsubsection*{(c) Letter item}" + TOC_ENTRY % "subsubsection")

    def test_bolum_duzeyi_kaynakca(self, conv):
        out = conv("# Synthetic Book\n\n" + _BODY + "# Einführung\n\n" + _BODY
                   + "# Bibliographie\n\n" + _BODY)
        assert "\\chapter*{Bibliographie}" + TOC_ENTRY % "chapter" in out

    def test_kitap_ortasi_kesitte_girdi_yok(self, conv):
        # Sayfa aralığı kesitinde bütün başlıklar numarasız ve İçindekiler'siz.
        out = conv("## 1.1 A\n\n" + _BODY + "## References\n\n- [1] K.\n",
                   page_range=[5, 9])
        assert "\\section*{References}" in out and "addcontentsline" not in out


# ===========================================================================
# KARAR 21: harfli başlıklar
# ===========================================================================

class TestKarar21HarfliBaslik:
    @pytest.mark.parametrize("baslik, metin", [
        ("(a) The lock phase", "(a) The lock phase"),
        ("*a*) OBSERVACIÓN", "\\textit{a}) OBSERVACIÓN"),
        ("b) Second step", "b) Second step"),
        ("(iv) Fourth item", "(iv) Fourth item"),
    ])
    def test_harf_kalir_numara_kalkar_icindekilerde(self, conv, baslik, metin):
        out = conv(_chapter(f"## {baslik}\n\n" + _BODY))
        assert "\\section*{" + metin + "}" + TOC_ENTRY % "section" in out
        assert not any(h.startswith("\\section{") for h in _headings(out))

    def test_numarali_harfli_baslik(self, conv):
        out = conv(_chapter("## 2.1 a) Observation\n\n" + _BODY))
        assert "\\section*{2.1 a) Observation}" + TOC_ENTRY % "section" in out

    @pytest.mark.parametrize("baslik", ["A.1 Appendix part", "Apparatus", "(Continued) notes",
                                        "I Am Legend", "1.2 Normal title"])
    def test_harfli_olmayan_baslik_etkilenmez(self, conv, baslik):
        out = conv(_chapter(f"## {baslik}\n\n" + _BODY))
        assert "addcontentsline" not in out


# ===========================================================================
# F1-15: kaynak PDF puntosuyla bölüm içi düzey
# ===========================================================================

def _pdf(path, items):
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    page = doc.new_page()
    y = 60
    body = "Plain body text line used to set the dominant font size of the page."
    for text, size in items:
        page.insert_text((50, y), text, fontsize=size, fontname="hebo")
        y += size * 1.6
        for _ in range(3):
            page.insert_text((50, y), body, fontsize=10, fontname="helv")
            y += 16
        if y > 700:
            page = doc.new_page()
            y = 60
    doc.save(str(path))
    return str(path)


_F115_MD = ("# Chapter 1 Europe\n\n" + _BODY
            + "#### Big Section One\n\n" + _BODY
            + "#### Small Sub Alpha\n\n" + _BODY
            + "#### Small Sub Beta\n\n" + _BODY
            + "#### Big Section Two\n\n" + _BODY
            + "### Big Section Three\n\n" + _BODY
            + "#### Small Sub Gamma\n\n" + _BODY)


class TestF115PdfPunto:
    def test_ayni_md_duzeyi_puntoyla_ayrilir(self, conv, tmp_path):
        pdf = _pdf(tmp_path / "source_book.pdf", [
            ("Chapter 1 Europe", 20), ("Big Section One", 14.9),
            ("Small Sub Alpha", 12.8), ("Small Sub Beta", 12.8),
            ("Big Section Two", 14.9), ("Big Section Three", 14.9),
            ("Small Sub Gamma", 12.8)])
        h = _headings(conv(_F115_MD, input_pdf=pdf))
        assert h == ["\\chapter{Europe}", "\\section{Big Section One}",
                     "\\subsection{Small Sub Alpha}", "\\subsection{Small Sub Beta}",
                     "\\section{Big Section Two}", "\\section{Big Section Three}",
                     "\\subsection{Small Sub Gamma}"], h

    def test_pdf_yoksa_md_kurali(self, conv, tmp_path):
        h = _headings(conv(_F115_MD, input_pdf=str(tmp_path / "yok.pdf")))
        assert "\\section{Small Sub Alpha}" in h

    def test_celiskide_md_kurali(self, conv, tmp_path):
        # "###" başlık daha küçük puntolu: punto md düzeyiyle çelişiyor.
        pdf = _pdf(tmp_path / "source_book.pdf", [
            ("Chapter 1 Europe", 20), ("Big Section One", 14.9),
            ("Small Sub Alpha", 12.8), ("Small Sub Beta", 12.8),
            ("Big Section Two", 14.9), ("Big Section Three", 11.0),
            ("Small Sub Gamma", 12.8)])
        h = _headings(conv(_F115_MD, input_pdf=pdf))
        assert "\\section{Small Sub Alpha}" in h

    def test_eslesmeyen_baslik_varsa_md_kurali(self, conv, tmp_path):
        pdf = _pdf(tmp_path / "source_book.pdf", [
            ("Chapter 1 Europe", 20), ("Big Section One", 14.9),
            ("Small Sub Alpha", 12.8), ("Big Section Two", 14.9),
            ("Big Section Three", 14.9), ("Small Sub Gamma", 12.8)])
        h = _headings(conv(_F115_MD, input_pdf=pdf))   # "Beta" PDF'te yok
        assert "\\section{Small Sub Alpha}" in h
