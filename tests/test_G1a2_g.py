"""G1a-2 (5 Ekim 2026): dönüştürücü, satır içi biçim kalemleri.

Olgular okuma bulgularından (2026-09-30) ve yerel model okumasından (mt
okuma MT-16, MT-18) gelir; buradaki cümleler SENTETİKTİR (KARAR 26: testlere
kitap metni kopyalanmaz).

(1) Satır içi HTML: <u>…</u> görünmez; kaynak PDF'te bağlantıysa \\href,
    değilse \\uline olur. <b>/<strong>/<i>/<em> kalın/italik, <a href> bağlantı,
    tablo hücresindeki <ul><li> düz metin olur. Açılı ayraçlı düzyazı ("<I
    don't…>", "<a command…>") silinmez.
(2) Aynı hedefe giden, arada yalnız boşluk olan bağlantılar tek \\href olur;
    satır sonunda bölünmüş adres metni adresle doğrulanarak birleşir. Atıf
    numaraları ayrı kalır.
(3) Marker'ın karışık vurgu dizileri ("***X**, … **Y.***") CommonMark
    kuralıyla çözülür; çiftsiz vurgu işareti ("Cam**") görünmez.
(4) Kaynak PDF'te eş aralıklı (kod) yazı tipindeki satır içi parçalar
    \\texttt olur; alıntı için kullanılan eş aralıklı yazı tipindeki düzyazı
    sarılmaz.
"""

import re

import pytest

from latex_pipeline.config import PipelineConfig
from latex_pipeline.converter import MarkdownToLatex

fitz = pytest.importorskip("fitz")

# Kitap adı + yazar + bölüm: sınanan satırlar gövdede kalsın.
_KITAP = "# A Synthetic Book Title\n\nJane Q. Writer\n\n## First chapter\n\n"


def _conv(md: str, **cfg) -> str:
    return MarkdownToLatex(PipelineConfig(document_class="book", **cfg)).convert(md)


def _govde(md: str, **cfg) -> str:
    return _conv(_KITAP + md, **cfg).split("\\section*{First chapter}", 1)[-1]


def _pdf_karisik(tmp_path, satirlar, ad="kaynak.pdf", baglanti=None, sayfa_sayisi=1):
    """Karışık yazı tipli küçük PDF.

    satirlar: her satır [(metin, yazı tipi)] ("helv" düz, "cour" eş aralıklı).
    baglanti: (aranan metin, hedef sayfa) -> o metnin üstüne iç bağlantı.
    """
    doc = fitz.open()
    for _ in range(sayfa_sayisi):
        doc.new_page()
    page = doc[0]
    y = 72
    for parcalar in satirlar:
        x = 72
        for metin, font in parcalar:
            page.insert_text((x, y), metin, fontname=font, fontsize=10)
            x += fitz.get_text_length(metin, fontname=font, fontsize=10)
        y += 14
    if baglanti:
        aranan, hedef = baglanti
        rect = page.search_for(aranan)[0]
        page.insert_link({"kind": fitz.LINK_GOTO, "from": rect, "page": hedef,
                          "to": fitz.Point(0, 0)})
    path = tmp_path / ad
    doc.save(str(path))
    doc.close()
    return str(path)


# Hizalama için yeterli metin katmanı (G1c: en az 200 karakter).
_DOLGU = [
    [("Filler prose keeps the text layer long enough for the alignment step.", "helv")],
    [("More filler prose follows so that the page carries a realistic amount.", "helv")],
    [("A third filler sentence closes the synthetic page without any code font.", "helv")],
]


# ---------------------------------------------------------------------------
# (1) Satır içi HTML etiketleri
# ---------------------------------------------------------------------------

class TestHtmlEtiketleri:
    def test_alti_cizili_metin_uline_olur_etiket_gorunmez(self):
        tex = _govde("The team read <u>the shared planning notes</u> twice.\n")
        assert "\\uline{the shared planning notes}" in tex
        assert "<u>" not in tex and "</u>" not in tex

    def test_dipnottaki_alti_cizili_metin(self):
        md = ("Body sentence with a mark.<sup>3</sup> More body text follows here.\n\n"
              "<sup>3</sup> A synthetic note that mentions <u>the underlined phrase</u> "
              "clearly.\n")
        tex = _govde(md)
        # KARAR 31 (FX1): eşlenen dipnot kaynak numarasıyla.
        assert "\\footnote[3]{A synthetic note that mentions \\uline{the underlined phrase}" in tex
        assert "<u>" not in tex

    def test_pdfte_baglanti_olan_alti_cizili_metin_href_olur(self, tmp_path):
        pdf = _pdf_karisik(
            tmp_path,
            [[("Please read the large example projects section later on.", "helv")]],
            baglanti=("large example projects", 1), sayfa_sayisi=2)
        tex = _govde("Please read the <u>large example projects</u> section later on.\n",
                     input_pdf=pdf)
        assert "\\href{#page-1-0}{large example projects}" in tex
        assert "\\uline" not in tex and "<u>" not in tex

    def test_pdfte_baglanti_yoksa_uline(self, tmp_path):
        pdf = _pdf_karisik(
            tmp_path, [[("Please read the large example projects section later on.", "helv")]])
        tex = _govde("Please read the <u>large example projects</u> section later on.\n",
                     input_pdf=pdf)
        assert "\\uline{large example projects}" in tex

    def test_aci_ayracli_duzyazi_silinmez(self):
        """'<I …>' boşluklu düzyazı eskiden etiket sanılıp siliniyordu."""
        md = ("He wrote: <I don't have the key right now.> and later <b and c> "
              "and <a command that appears to be mine> stayed. <p>Real</p> goes.\n")
        tex = _govde(md)
        for parca in ("I don't have the key right now.", "b and c",
                      "a command that appears to be mine"):
            assert parca in tex, parca
        assert "<p>" not in tex and "</p>" not in tex and "Real" in tex

    def test_kalin_italik_etiketler(self):
        tex = _govde("One <b>alpha</b>, two <strong>beta</strong>, "
                     "three <i>gamma</i> and <em>delta</em>.\n")
        assert "\\textbf{alpha}" in tex and "\\textbf{beta}" in tex
        assert "\\textit{gamma}" in tex and "\\textit{delta}" in tex
        assert "<" not in tex.split("One", 1)[1].split("\n", 1)[0]

    def test_baglanti_etiketi_href_olur(self):
        tex = _govde('See <a href="https://example.org/docs/">https://example.org/docs/</a> '
                     "for details.\n")
        assert "\\href{https://example.org/docs/}{https://example.org/docs/}" in tex
        assert "<a" not in tex and "</a>" not in tex

    def test_tablo_hucresindeki_etiketler(self):
        md = ("| Name | Value |\n|---|---|\n"
              "| Alpha | <b>0.25</b> (0.01) |\n"
              "| Beta | <ul><li>12</li><li>13</li></ul> |\n"
              "| Gamma | <u>97</u> |\n"
              "| Delta | <i>n</i> turns |\n")
        tex = _govde(md)
        assert "\\textbf{0.25} (0.01)" in tex
        assert "\\uline{97}" in tex
        assert "\\textit{n} turns" in tex
        assert re.search(r"Beta & 12 13 \\\\", tex)
        assert "<" not in tex


# ---------------------------------------------------------------------------
# (2) Sözcük sözcük bölünmüş bağlantılar
# ---------------------------------------------------------------------------

class TestBolunmusBaglanti:
    def test_ayni_hedefe_ardisik_iki_baglanti_tek_href(self):
        tex = _govde("The [collective](#page-4-0) [projects](#page-4-0) grew fast.\n")
        assert "\\href{#page-4-0}{collective projects}" in tex
        assert tex.count("\\href") == 1

    def test_uc_parcali_baglanti_ve_noktalama(self):
        tex = _govde("Read [the](#page-7-0) [whole](#page-7-0) [story,](#page-7-0) then stop.\n")
        assert "\\href{#page-7-0}{the whole story,}" in tex
        assert tex.count("\\href") == 1

    def test_satir_sonunda_bolunmus_adres_metni_bosluksuz_birlesir(self):
        url = "http://www.example.org/archive/notes/"
        tex = _govde(f"Source: [http://www.example.org/]({url})  [archive/notes/.]({url})\n")
        assert f"\\href{{{url}}}{{http://www.example.org/archive/notes/.}}" in tex
        assert tex.count("\\href") == 1

    def test_tirede_bolunmus_adres_metni_tireyle_birlesir(self):
        url = "https://example.org/paper/safe-model-based-control.pdf"
        tex = _govde(f"Paper: [https://example.org/paper/safe-model]({url})"
                     f"[based-control.pdf]({url})\n")
        assert f"{{https://example.org/paper/safe-model-based-control.pdf}}" in tex
        assert tex.count("\\href") == 1

    def test_satir_sonunda_bolunmus_sayi_bosluksuz_birlesir(self):
        """ISBN gibi rakamla bölünmüş metin adresle doğrulanırsa boşluksuz
        birleşir; adres metni benzeri olmayan sözcükler tireyle birleşmez."""
        url = "https://example.org/wiki/Special:BookSources/978-1-234-56789-0"
        tex = _govde(f"ISBN [978-1-234-5]({url})  [6789-0]({url}) here.\n")
        assert "{978-1-234-56789-0}" in tex
        slug = "https://example.org/posts/model-organisms-of-research"
        tex = _govde(f"A [model]({slug}) [organisms]({slug}) note.\n")
        assert "{model organisms}" in tex

    def test_ayracli_ve_yuzde_kodlu_adres_metni(self):
        url = "https://www.example.org/science/article/S2214"
        tex = _govde(f"(see [https://www.]({url})  [example.org/science/article/S2214\\)]({url})\n")
        assert f"\\href{{{url}}}{{https://www.example.org/science/article/S2214}})" in tex
        url = "https://en.example.org/wiki/Simpson%27s_rule"
        tex = _govde(f"Read [https://en.example.org/wiki/Simpson]({url})  [%27s\\_rule]({url})\n")
        assert "{https://en.example.org/wiki/Simpson\\%27s\\_rule}" in tex

    def test_atif_numaralari_birlesmez(self):
        tex = _govde("As shown before [\\[12,](#page-9-0) [15\\]](#page-9-0) the rule holds.\n")
        assert tex.count("\\href") == 2

    def test_farkli_hedef_ya_da_virgul_birlesmez(self):
        tex = _govde("See [alpha](#page-1-0) [beta](#page-2-0) and [six](#page-3-0),"
                     "[seven](#page-3-0) here.\n")
        assert tex.count("\\href") == 4


# ---------------------------------------------------------------------------
# (3) Vurgu artığı (mt okuma MT-18'in dönüştürücü payı)
# ---------------------------------------------------------------------------

class TestVurguArtigi:
    def test_ic_ice_kalin_italik_dizisi(self):
        tex = _govde("***Alpha beta gamma**, delta epsilon. **Zeta eta theta.***\n")
        assert ("\\textit{\\textbf{Alpha beta gamma}, delta epsilon. "
                "\\textbf{Zeta eta theta.}}") in tex
        assert "*" not in tex.split("\\textit{\\textbf{Alpha", 1)[1].split("\n", 1)[0]

    def test_ayni_satirda_birden_cok_uclu_yildiz(self):
        md = ("It was then I thought, “***come along***” and ***you cannot bargain "
              "for this**.* The key word is ***choice***.\n")
        tex = _govde(md)
        assert "“\\textbf{\\textit{come along}}”" in tex
        assert "\\textit{\\textbf{you cannot bargain for this}.}" in tex
        assert "\\textbf{\\textit{choice}}" in tex
        satir = [s for s in tex.split("\n") if "come along" in s][0]
        assert "*" not in satir

    def test_eski_dogru_cikti_degismez(self):
        md = ("A **bold** and *italic* and ***both*** word; talking with "
              "**a***Deity* is possible.\n")
        tex = _govde(md)
        assert ("A \\textbf{bold} and \\textit{italic} and \\textbf{\\textit{both}} word; "
                "talking with \\textbf{a}\\textit{Deity} is possible.") in tex

    def test_ciftsiz_isaret_duz_karakter_kalir(self):
        """Marker düz yıldızı her zaman kaçışlamaz ("Rotational period*" gerçek
        dipnot simgesi): eşi olmayan işaret silinmez (kayıpsızlık)."""
        tex = _govde("| Body | Rotational period* |\n|---|---|\n| Alpha | 10 |\n\n"
                     "See note [*] below and the *alpha* term.\n")
        assert "Rotational period*" in tex
        assert "note [*] below" in tex and "\\textit{alpha}" in tex

    def test_satirlara_bolunmus_paragrafta_italik(self):
        """EPUB/Gutenberg (satır kırpılmış) paragrafında satırlara yayılan
        italik eskiden '*' olarak basılıyordu."""
        md = ("Opening line of a wrapped paragraph that says: *First part of\n"
              "the italic phrase keeps going across this line and\n"
              "ends here*, then the sentence goes on normally.\n"
              "Another plain line of the same wrapped paragraph follows.\n"
              "A closing line finishes the wrapped paragraph text.\n\n"
              "Second wrapped paragraph begins on this line and\n"
              "continues on this one with a *short* italic word.\n"
              "Its last line ends the second paragraph cleanly.\n")
        tex = _govde(md)
        assert "*" not in tex
        assert "says: \\textit{First part of}" in tex
        assert "\\textit{the italic phrase keeps going across this line and}" in tex
        assert "\\textit{ends here}, then" in tex
        assert "\\textit{short}" in tex


# ---------------------------------------------------------------------------
# (4) Eş aralıklı satır içi parçalar
# ---------------------------------------------------------------------------

class TestEsAralikli:
    SATIRLAR = [
        [("The review team traced how the agent ", "helv"), ("ALPHA_01", "cour"),
         (" sent a short note to the board.", "helv")],
        [("Later the agent ", "helv"), ("BETA[big]", "cour"),
         (" replied and asked for the ", "helv"), ('"GAMMA42"', "cour"),
         (" archive.", "helv")],
    ] + _DOLGU
    MD = ("The review team traced how the agent ALPHA\\_01 sent a short note to the board.\n\n"
          'Later the agent BETA[big] replied and asked for the "GAMMA42" archive.\n\n'
          "Filler prose keeps the text layer long enough for the alignment step.\n\n"
          "More filler prose follows so that the page carries a realistic amount.\n\n"
          "A third filler sentence closes the synthetic page without any code font.\n")

    def test_kod_yazi_tipindeki_parcalar_texttt(self, tmp_path):
        tex = _govde(self.MD, input_pdf=_pdf_karisik(tmp_path, self.SATIRLAR))
        assert "agent \\texttt{ALPHA\\_01} sent" in tex
        assert "agent \\texttt{BETA[big]} replied" in tex
        # Tırnak \texttt dışında kalır (altın ile aynı biçim).
        assert '"\\texttt{GAMMA42}" archive' in tex
        assert tex.count("\\texttt") == 3

    def test_pdf_yoksa_degisiklik_yok(self):
        tex = _govde(self.MD)
        assert "\\texttt" not in tex
        assert "agent ALPHA\\_01 sent" in tex

    def test_alinti_yazi_tipindeki_duzyazi_sarilmaz(self, tmp_path):
        satirlar = [
            [("One agent reasoned: ", "helv"),
             ("{I should report this to the board right now.}", "cour"),
             (" Then it waited.", "helv")],
            [("Another agent wrote: ", "helv"),
             ("{We can share the plan with the other teams soon.}", "cour"),
             (" Nobody replied.", "helv")],
            [("The token ", "helv"), ("DELTA_9", "cour"),
             (" appeared in both notes and in the summary.", "helv")],
        ] + _DOLGU
        md = ("One agent reasoned: {I should report this to the board right now.} "
              "Then it waited.\n\n"
              "Another agent wrote: {We can share the plan with the other teams soon.} "
              "Nobody replied.\n\n"
              "The token DELTA\\_9 appeared in both notes and in the summary.\n\n"
              "Filler prose keeps the text layer long enough for the alignment step.\n\n"
              "More filler prose follows so that the page carries a realistic amount.\n\n"
              "A third filler sentence closes the synthetic page without any code font.\n")
        tex = _govde(md, input_pdf=_pdf_karisik(tmp_path, satirlar))
        assert "\\texttt{DELTA\\_9}" in tex
        assert tex.count("\\texttt") == 1

    def test_uzun_belirtecte_alt_cizgide_satir_kirilabilir(self, tmp_path):
        satirlar = [[("The flag ", "helv"), ("ZETA_LONG_TOKEN_NAME_X1", "cour"),
                     (" was set by the second agent.", "helv")]] + _DOLGU
        md = ("The flag ZETA\\_LONG\\_TOKEN\\_NAME\\_X1 was set by the second agent.\n\n"
              "Filler prose keeps the text layer long enough for the alignment step.\n\n"
              "More filler prose follows so that the page carries a realistic amount.\n\n"
              "A third filler sentence closes the synthetic page without any code font.\n")
        tex = _govde(md, input_pdf=_pdf_karisik(tmp_path, satirlar))
        assert ("\\texttt{ZETA\\allowbreak{}\\_LONG\\allowbreak{}\\_TOKEN"
                "\\allowbreak{}\\_NAME\\allowbreak{}\\_X1}") in tex

    def test_baslikta_ve_kod_blogunda(self, tmp_path):
        satirlar = [
            [("Second part about ", "helv"), ("OMEGA_7", "cour"), (" behaviour", "helv")],
            [("The plain sentence mentions ", "helv"), ("OMEGA_7", "cour"),
             (" once more today.", "helv")],
        ] + _DOLGU
        md = ("### Second part about OMEGA\\_7 behaviour\n\n"
              "The plain sentence mentions OMEGA\\_7 once more today.\n\n"
              "```\nOMEGA_7 = 1\n```\n\n"
              "Filler prose keeps the text layer long enough for the alignment step.\n\n"
              "More filler prose follows so that the page carries a realistic amount.\n\n"
              "A third filler sentence closes the synthetic page without any code font.\n")
        tex = _govde(md, input_pdf=_pdf_karisik(tmp_path, satirlar))
        assert re.search(r"\\subsection\*?\{Second part about \\texttt\{OMEGA\\_7\} behaviour\}",
                         tex)
        assert "mentions \\texttt{OMEGA\\_7} once" in tex
        assert "OMEGA_7 = 1" in tex   # kod bloğu aynen
