"""Dönüştürücü: matematik, şekil, dipnot, kalıntılar — kalite kulvarı C2 (28 Eylül 2026).

Karşılaştırma raporu (§9, iç denetim) maddeleri:
  R4   Kiril alt indisler: Marker "<sup>н</sup>" (üst simge okunmuş alt indis)
       -> "$p_{\\text{н}}$"; italik değişkene yapışık "*< p*н" de alt indis.
  R11  HTML kalıntıları: "<sup>&</sup>lt;sup>1</sup>" (bölünmüş varlık),
       "$<sup>^{1}</sup>$" (matematiğe sarılmış iç içe üst simge), içerikte
       '&', bitişik "<sup>x</sup><sup>i</sup>" dizisi (AutoFix "Double
       superscript" üretiyordu).
  R13  şekil + bitişik altyazı paragrafı -> figure içinde \\caption*.
  R14  küçük süs görseli kaynaktaki boyutunda; sekme numarası "**1**" ve
       sayfa üst bilgisi (kitap adı + bölüm adı) gövdeye girmez.
  R16  display matematiğin sonundaki düz metin "(1.5)" -> \\tag{1.5}.
EPUB dipnot sözleşmesi (E1 ↔ C2, onaylı): "[^kc-N]" işaretinin yerinde
\\footnote{tanım}; "[^kc-N]: metin" tanım satırları (bütün metinden, bölüm
bazında değil) gövdeden çıkar; tanımsız işaret uyarıyla üst simge kalır;
"<sup>N</sup>" -> \\textsuperscript{N}.

Tüm girdiler SENTETİKTİR (kitaptan düzyazı alınmaz).
"""

from __future__ import annotations

import logging
import re

import pytest

from latex_pipeline.config import PipelineConfig
from latex_pipeline.converter import MarkdownToLatex
from latex_pipeline.patterns import FLOAT_CAPTION_MAX_CHARS
from parsers.epub_parser import NOTE_LABEL_PREFIX


@pytest.fixture
def conv(tmp_path):
    def _run(markdown: str, document_class: str = "article") -> str:
        cfg = PipelineConfig(document_class=document_class,
                             output_dir=str(tmp_path))
        out = MarkdownToLatex(cfg).convert(markdown)
        assert "\x02" not in out, "satır içi işaret nöbetçisi sızdı"
        return out
    return _run


def _figures(tex: str) -> list[str]:
    return re.findall(r"\\begin\{figure\}.*?\\end\{figure\}", tex, re.S)


def _outside_figures(tex: str) -> str:
    return re.sub(r"\\begin\{figure\}.*?\\end\{figure\}", "", tex, flags=re.S)


# ---------------------------------------------------------------------------
# R11: HTML kalıntıları ve üst/alt simge
# ---------------------------------------------------------------------------

class TestR11Kalintilar:
    def test_bolunmus_varlik_ust_simge_olur(self, conv):
        out = conv("<sup>&</sup>lt;sup>2</sup>Kısa bir dipnot metni.")
        assert out.strip() == r"\textsuperscript{2}Kısa bir dipnot metni."
        assert "lt;" not in out and "{&}" not in out

    @pytest.mark.parametrize("src", [" $<sup>^1</sup>$ Dipnot metni burada.",
                                     " $<sup>^{1}</sup>$ Dipnot metni burada."])
    def test_matematige_sarili_ic_ice_ust_simge(self, conv, src):
        # Eskiden "$${}^{^{1}}$$": satır display matematiğe dönüyor, dipnot
        # metni matematik kipine yutuluyordu.
        out = conv(src)
        assert "$$" not in out and r"\[" not in out
        assert r"\textsuperscript{1} Dipnot metni burada." in out

    def test_icerikteki_ozel_karakter_kacislanir(self, conv):
        out = conv("Değer a<sup>&amp;</sup> ve b<sup>%</sup> olur.")
        assert r"$a^{\&}$" in out and r"$b^{\%}$" in out

    def test_bitisik_ust_simge_dizisi_tek_matematik(self, conv):
        # "${}^{x}$${}^{i}$" AutoFix'te "$^{x}^{i}$" (Double superscript) oluyordu.
        out = conv("Tepeler *<sup>x</sup><sup>i</sup> xi*+1 arasında.")
        assert "${}^{x}{}^{i}$" in out
        assert "$$" not in out

    def test_bitisik_dizi_tabanla(self, conv):
        out = conv("Açı h<sup>b</sup><sup>0</sup> ile ölçülür.")
        assert "$h^{b}{}^{0}$" in out

    @pytest.mark.parametrize("src,exp", [
        ("x<sup>0</sup> değeri", "$x^{0}$ değeri"),
        ("<sup>T</sup> devrik", "${}^{T}$ devrik"),
        ("H<sub>2</sub>O suyu", "$H_{2}$O suyu"),
        ("W/m<sup>2</sup> birimi", "W/$m^{2}$ birimi"),
        ("oran 10<sup>6</sup> kat", "oran 1$0^{6}$ kat"),
        ("ve *r*<sup>2</sup> alanı", r"ve \textit{r}${}^{2}$ alanı"),
    ])
    def test_matematik_ust_simge_eski_kural(self, conv, src, exp):
        assert conv(src).strip() == exp

    @pytest.mark.parametrize("src,exp", [
        # Sözcük sonundaki harf matematiğe alınmaz (eskiden "filosofí$a^{1}$").
        ("una filosofía<sup>1</sup>; sigue", r"una filosofía\textsuperscript{1}; sigue"),
        ("tenemos!<sup>2</sup>. Fin", r"tenemos!\textsuperscript{2}. Fin"),
        ("imposibles»<sup>24</sup>. Fin", r"imposibles»\textsuperscript{24}. Fin"),
        ("Satz.<sup>1</sup> Weiter", r"Satz.\textsuperscript{1} Weiter"),
        ("<sup>1</sup>Dipnot gövdesi", r"\textsuperscript{1}Dipnot gövdesi"),
        # Markdown kaçışlı yıldız matematikte görünmüyordu ("\*" kesme noktası).
        ("<sup>\\*</sup> Yıldızlı not", r"\textsuperscript{*} Yıldızlı not"),
        ("söz<sup>†</sup> biter", r"söz\textsuperscript{†} biter"),
        ("iki not<sup>1</sup><sup>2</sup> var", r"iki not\textsuperscript{1}\textsuperscript{2} var"),
    ])
    def test_metin_ici_isaret_textsuperscript(self, conv, src, exp):
        assert conv(src).strip() == exp

    @pytest.mark.parametrize("src,exp", [
        # Yunan harfi ve değiştirici harf (Marker'ın şapkalı vektörü) sözcük değil.
        ("hız δv<sup>2</sup> olur", "hız $\\delta$$v^{2}$ olur"),
        ("vektör gˆ<sup>1</sup> yönünde", "vektör gˆ${}^{1}$ yönünde"),
        # Tek harfli sözcük matematik değişkenidir.
        ("ve a<sup>1</sup> terimi", "ve $a^{1}$ terimi"),
    ])
    def test_matematik_baglami_korunur(self, conv, src, exp):
        assert conv(src).strip() == exp


# ---------------------------------------------------------------------------
# R4: Kiril indisler
# ---------------------------------------------------------------------------

class TestR4KirilIndis:
    def test_italik_degisken_ust_simge_alt_indis_olur(self, conv):
        out = conv("где *w*<sup>а</sup> — скорость, *p*<sup>н</sup> = 0.")
        assert out.strip() == (r"где $w_{\text{а}}$ — скорость, "
                               r"$p_{\text{н}}$ = 0.")

    def test_duz_taban_ve_alt_simge(self, conv):
        out = conv("давление p<sup>н</sup> и F<sub>а</sub> площадь.")
        assert r"$p_{\text{н}}$" in out and r"$F_{\text{а}}$" in out

    def test_kisaltma_indis(self, conv):
        out = conv("время *t*<sup>раб</sup> работы.")
        assert r"$t_{\text{раб}}$" in out

    @pytest.mark.parametrize("src,exp", [
        ("когда *p*<sup>а</sup> *< p*н;",
         r"когда $p_{\text{а}}$ < $p_{\text{н}}$;"),
        ("и *p*н = 0", r"и $p_{\text{н}}$ = 0"),
    ])
    def test_italige_yapisik_kiril_indis(self, conv, src, exp):
        assert conv(src).strip() == exp

    def test_karisik_icerikte_kiril_text_icinde(self, conv):
        out = conv("коэффициент c<sup>i пост</sup> равен.")
        assert r"\text{пост}" in out
        # Matematikte çıplak Kiril harfi kalmamalı.
        for span in re.findall(r"\$[^$]*\$", out):
            assert not re.search(r"(?<!\\text\{)[А-Яа-яЁё]", re.sub(
                r"\\text\{[^}]*\}", "", span)), span

    def test_rusca_duzyazi_italik_etkilenmez(self, conv):
        out = conv("это *важно*, а *x* и *y* здесь.")
        assert r"\textit{важно}" in out and "$" not in out

    def test_latin_italik_ardindan_latin_harf_etkilenmez(self, conv):
        assert "_{" not in conv("the *x*a value and *f*s term.")


# ---------------------------------------------------------------------------
# EPUB dipnot sözleşmesi
# ---------------------------------------------------------------------------

class TestDipnotSozlesmesi:
    def test_onek_tek_kaynak(self):
        assert NOTE_LABEL_PREFIX == "kc-"

    def test_isaret_yerinde_footnote_tanim_satiri_cikar(self, conv):
        md = ("Birinci cümle[^kc-1]; ikinci cümle.\n\n"
              "[^kc-1]: Not metni *italik* ve **kalın** ile %50 & [bağ](http://a.b).")
        out = conv(md)
        assert (r"Birinci cümle\footnote{Not metni \textit{italik} ve "
                r"\textbf{kalın} ile \%50 \& \href{http://a.b}{bağ}.}; ikinci cümle."
                in out)
        assert "kc-" not in out and "[^" not in out
        assert "Not metni" not in out.split(r"\footnote{")[0]

    def test_tanimlar_kitap_sonunda_bolum_bazinda_degil(self, conv):
        # Tek dosyalı Gutenberg kitabı: tanımlar birden çok bölümden sonra.
        md = ("# Birinci Bölüm\n\nİlk bölüm metni[^kc-1] burada.\n\n"
              "# İkinci Bölüm\n\nİkinci bölüm metni[^kc-2] burada.\n\n"
              "[^kc-1]: Birinci not.\n\n[^kc-2]: İkinci not.\n")
        out = conv(md, document_class="book")
        assert r"metni\footnote{Birinci not.} burada." in out
        assert r"metni\footnote{İkinci not.} burada." in out
        assert out.index("Birinci not.") < out.index("İkinci Bölüm")
        tail = out[out.index("İkinci Bölüm"):]
        assert "Birinci not." not in tail.replace(r"\footnote{Birinci not.}", "")
        assert out.count("İkinci not.") == 1

    def test_tanimsiz_isaret_uyari_ile_kalir(self, conv, caplog):
        with caplog.at_level(logging.WARNING, logger="latex_pipeline.converter"):
            out = conv("Tanımı yok[^kc-7] ama metin sürer.")
        assert r"Tanımı yok\textsuperscript{7} ama metin sürer." in out
        assert any("kc-7" in r.getMessage() for r in caplog.records)

    def test_isaretsiz_tanim_kaybolmaz(self, conv, caplog):
        with caplog.at_level(logging.WARNING, logger="latex_pipeline.converter"):
            out = conv("Gövde metni.\n\n[^kc-3]: Sahipsiz not metni.")
        assert "Sahipsiz not metni." in out and r"\footnote" not in out
        assert any("kc-3" in r.getMessage() for r in caplog.records)

    def test_ayni_dipnota_iki_isaret(self, conv):
        out = conv("Bir[^kc-1] ve iki[^kc-1].\n\n[^kc-1]: Ortak not.")
        assert r"Bir\footnote{\label{fn:kc-1}Ortak not.}" in out
        assert r"iki\footref{fn:kc-1}." in out
        assert out.count("Ortak not.") == 1

    def test_satir_kirpilmis_paragraf(self, conv):
        md = ("Satır kırpılmış bir paragrafın\nortasında not[^kc-1] var ve\n"
              "paragraf sürer.\n\n[^kc-1]: Kısa not.\n")
        out = conv(md)
        assert r"not\footnote{Kısa not.} var ve" in out

    def test_parantez_izleyen_isaret_bag_sanilmaz(self, conv):
        out = conv("Metin[^kc-1](ek bilgi) sürer.\n\n[^kc-1]: Not.")
        assert r"Metin\footnote{Not.}(ek bilgi) sürer." in out
        assert r"\href" not in out

    def test_tablodaki_isaret_footnotemark_footnotetext(self, conv):
        md = ("| A | B |\n|---|---|\n| hücre[^kc-1] | öbür[^kc-2] |\n\n"
              "[^kc-1]: Birinci tablo notu.\n\n[^kc-2]: İkinci tablo notu.")
        out = conv(md)
        table = re.search(r"\\begin\{table\}.*?\\end\{table\}", out, re.S).group(0)
        assert table.count(r"\footnotemark") == 2 and r"\footnote{" not in table
        after = out[out.index(r"\end{table}"):]
        assert r"\addtocounter{footnote}{-2}" in after
        assert (r"\stepcounter{footnote}\footnotetext{Birinci tablo notu.}"
                in after)
        assert (r"\stepcounter{footnote}\footnotetext{İkinci tablo notu.}"
                in after)

    def test_numarali_baslikta_kisa_baslik_dipnotsuz(self, conv):
        # İçindekilere ve üst bilgiye dipnot gitmesin: kısa başlık dipnotsuz.
        md = ("Giriş metni burada.\n\n# Chapter 1 Giriş\n\nMetin.\n\n"
              "## 1.1 Yöntem[^kc-1]\n\nMetin.\n\n[^kc-1]: Başlık notu.")
        out = conv(md, document_class="book")
        assert (r"\section[{1.1 Yöntem}]{1.1 Yöntem\footnote{Başlık notu.}}"
                in out.split("\n"))

    def test_yildizli_baslikta_dipnot_yerinde(self, conv):
        md = ("Giriş metni burada.\n\n# Chapter 1 Giriş\n\nMetin.\n\n"
              "## 1.1 Yöntem[^kc-1]\n\nMetin.\n\n[^kc-1]: Başlık notu.")
        out = conv(md, document_class="article")
        assert r"\section*{1.1 Yöntem\footnote{Başlık notu.}}" in out.split("\n")

    def test_dipnottaki_bag_adresi_kacislanir(self, conv):
        # Argüman içinde '#' "Illegal parameter number", '%' dipnotun kalanını
        # yorum yapıyordu (bir EPUB kitabında: "ch07.html#ch07").
        out = conv("Metin[^kc-1].\n\n[^kc-1]: Bkz. [Bölüm 7](ch07.html#ch07) "
                   "ve [kaynak](https://a.b/x%20y).")
        assert (r"\footnote{Bkz. \href{ch07.html\#ch07}{Bölüm 7} ve "
                r"\href{https://a.b/x\%20y}{kaynak}.}" in out)
        # Gövdedeki bağ (argüman dışında) eskisi gibi kalır.
        assert r"\href{ch07.html#ch07}{Bölüm 7}" in conv("[Bölüm 7](ch07.html#ch07)")

    def test_dengesiz_dipnot_duz_metne_iner(self, conv, caplog):
        with caplog.at_level(logging.WARNING, logger="latex_pipeline.converter"):
            # F1-04 (2026-09-29): Marker "*A\\**" artık dengeli (\textit{A*});
            # dengesiz girdi olarak kapanmamış ayraç kullanılır.
            out = conv("Metin[^kc-1].\n\n[^kc-1]: Oran $a}$ sonra.")
        fn = out[out.index(r"\footnote{"):]
        depth = 0
        for i, ch in enumerate(fn):
            if ch == "{" and fn[i - 1] != "\\":
                depth += 1
            elif ch == "}" and fn[i - 1] != "\\":
                depth -= 1
            assert depth >= 0
        assert depth == 0 and "Oran" in fn
        assert any("kc-1" in r.getMessage() for r in caplog.records)

    def test_dipnotsuz_belge_degismez(self, conv):
        md = "Köşeli [ayraç] ve [^ şapka] olan metin."
        assert "[" in conv(md)

    def test_sarkan_sup_isareti(self, conv):
        # Tanımı bulunamayan EPUB bağlantısı (E1): "<sup>N</sup>".
        out = conv("poco estrecha en filosofía<sup>1</sup>; pero sigue")
        assert r"filosofía\textsuperscript{1};" in out


# ---------------------------------------------------------------------------
# R13: şekil ve altyazısı
# ---------------------------------------------------------------------------

class TestR13Altyazi:
    @pytest.mark.parametrize("caption", [
        "**Рис. 1.1.** Схема камеры и сопла",
        "Рис 11 Классификация двигателей",          # OCR: nokta düşmüş
        "Рис. 1 2. Схемы камеры",                    # OCR: boşluklu numara
        "**Abbildung 1.1:** Temperaturverlauf in einer Wand",
        "**Abb. 1.1** Die Alpen einmal anders",
        "**Figure 1.1 :** Profil d'aile sous incidence",
        "Figura 1.1: Distribución de presiones",
        "**Figura 1.1:** Schema del sistema",
        "Fig. 2.5 The model of the escapement",
        "FIGURE 7.1 An astrometric binary",
    ])
    def test_ardindaki_altyazi_figure_icine(self, conv, caption):
        md = f"Önceki paragraf.\n\n![](_page_1_Figure_1.jpeg)\n\n{caption}\n\nSonraki paragraf.\n"
        out = conv(md)
        figs = _figures(out)
        assert len(figs) == 1
        assert r"\caption*{" in figs[0]
        assert figs[0].index(r"\includegraphics") < figs[0].index(r"\caption*")
        cap_word = re.sub(r"[*]", "", caption).split()[0]
        assert cap_word not in _outside_figures(out)
        assert "Sonraki paragraf." in _outside_figures(out)

    @pytest.mark.parametrize("sentence", [
        "Figure 3.4 shows the architecture of the model.",
        "Abb. 1.2 zeigt die Grenze.",
        "Рис. 1.1 показывает схему.",
        "Figure 3.4, on the other hand, is different.",
    ])
    def test_atifla_baslayan_govde_cumlesi_alinmaz(self, conv, sentence):
        out = conv(f"![](_page_1_Figure_1.jpeg)\n\n{sentence}\n")
        assert r"\caption" not in out
        assert sentence.split()[0] in _outside_figures(out)

    def test_once_gelen_altyazi_obur_yanda(self, conv):
        # [görsel A][altyazı A][altyazı B][görsel B]: B'nin altyazısı önünde.
        md = ("![](a.jpeg)\n\n**Abb. 1.2** Birinci şeklin adı\n\n"
              "**Abb. 1.3** İkinci şeklin adı\n\n![](b.jpeg)\n\nGövde cümlesi burada.\n")
        figs = _figures(conv(md))
        assert "Birinci" in figs[0] and "İkinci" in figs[1]
        assert figs[1].index(r"\caption*") < figs[1].index(r"\includegraphics")

    def test_yan_yana_iki_gorsel_iki_altyazi_sirayla(self, conv):
        md = ("![](a.jpeg)\n\n![](b.jpeg)\n\nРис 1 13 Первый двигатель\n\n"
              "Рис 1 14 Второй двигатель\n\nГовде текст здесь.\n")
        figs = _figures(conv(md))
        assert "Первый" in figs[0] and "Второй" in figs[1]

    def test_iki_gorsel_tek_altyazi_belirsiz(self, conv):
        md = "![](a.jpeg)\n\n![](b.jpeg)\n\nFigure 1.1: Only one caption\n\nText.\n"
        out = conv(md)
        assert r"\caption" not in out and "Only one caption" in out

    def test_dizinin_ucu_obur_gorseller_eslenince(self, conv):
        md = ("![](a.jpeg)\n\nFig. 2.20 First name\n\nFig. 2.21 Second name\n\n"
              "![](b.jpeg)\n![](c.jpeg)\n\nFig. 2.22 Third name\n\nBody text.\n")
        figs = _figures(conv(md))
        assert ["First" in figs[0], "Second" in figs[1], "Third" in figs[2]] == [True] * 3

    def test_dipnotlu_ya_da_uzun_paragraf_alinmaz(self, conv):
        md = ("![](a.jpeg)\n\nFigure 1.1: Name[^kc-1]\n\n[^kc-1]: Not.\n\n"
              "![](b.jpeg)\n\nFigure 1.2: " + "x" * FLOAT_CAPTION_MAX_CHARS + "\n")
        out = conv(md)
        assert r"\caption" not in out

    def test_altyazidaki_bag_adresi_kacislanir(self, conv):
        out = conv("![](a.jpeg)\n\nFig. 3.1 Result of [(3.4)](#page-51-0) here\n")
        assert r"\href{\#page-51-0}{(3.4)}" in _figures(out)[0]

    def test_dengesiz_altyazi_paragraf_kalir(self, conv):
        # Marker'ın "*A\**" italiği "\textit{A\}..." (dengesiz) üretir; \caption*
        # argümanında bu "File ended while scanning" (ölümcül) olurdu.
        # F1-04 (2026-09-29): "*A\\**" artık dengeli; dengesiz girdi değişti.
        md = "![](a.jpeg)\n\nFIGURE 2.6 Ratio $a}$ and here\n"
        out = conv(md)
        assert r"\caption" not in out and "FIGURE 2.6" in _outside_figures(out)

    def test_alt_metinli_gorsel_eski_kural(self, conv):
        out = conv("![Açıklama](a.jpeg)\n\nMetin.\n")
        assert r"\caption{Açıklama}" in out


# ---------------------------------------------------------------------------
# R14: süs görseli, sekme numarası, sayfa üst bilgisi
# ---------------------------------------------------------------------------

class TestR14SayfaSusleri:
    def _png(self, tmp_path, name, size):
        from PIL import Image
        d = tmp_path / "source" / "images"
        d.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", size, (200, 30, 30)).save(d / name)

    def test_kucuk_gorsel_kaynak_boyutunda(self, conv, tmp_path):
        self._png(tmp_path, "_page_4_Picture_2.png", (109, 122))
        out = conv("![](_page_4_Picture_2.jpeg)\n")
        # 192 dpi: 109 px = 0,57 inç (eskiden 0.8\textwidth, yarım sayfa).
        assert r"\includegraphics[width=0.57in]{_page_4_Picture_2.png}" in out

    def test_buyuk_ve_bulunmayan_gorsel_eski_genislik(self, conv, tmp_path):
        self._png(tmp_path, "big.png", (1238, 733))
        out = conv("![](big.png)\n\n![](yok.png)\n")
        assert out.count(r"[width=0.8\textwidth]") == 2

    @pytest.mark.parametrize("line", ["**1**", "**12**", "__3__"])
    def test_sekme_numarasi_atilir(self, conv, line):
        out = conv(f"Önceki paragraf.\n\n{line}\n\nSonraki paragraf.\n")
        assert r"\textbf{" not in out and "Sonraki paragraf." in out

    def test_kalin_sayi_cumle_icinde_kalir(self, conv):
        assert r"\textbf{1}" in conv("Adım **1** önemlidir.\n")

    def _kitap(self, extra: str) -> str:
        return ("## Yazar Adı Soyadı\n\n# **Titreşim Mekaniği Dersleri**\n\n"
                "## **Bölüm 1. Serbest salınımlar**\n\n"
                "Birinci gövde paragrafı burada.\n\n" + extra +
                "\n\nİkinci gövde paragrafı burada.\n")

    def test_ust_bilgi_satiri_atilir(self, conv):
        out = conv(self._kitap("*Titreşim Mekaniği Dersleri Serbest salınımlar*"),
                   document_class="book")
        assert "Dersleri Serbest" not in out
        assert "İkinci gövde paragrafı" in out

    def test_ust_bilgi_sayfa_numarasi_ve_ters_sira(self, conv):
        out = conv(self._kitap("12 Serbest salınımlar Titreşim Mekaniği Dersleri"),
                   document_class="book")
        assert "salınımlar Titreşim" not in out

    def test_paragraf_icindeki_satir_ust_bilgi_sayilmaz(self, conv):
        out = conv(self._kitap("Titreşim Mekaniği Dersleri Serbest salınımlar\n"
                               "konusu bu satırda sürüyor."), document_class="book")
        assert "Dersleri Serbest salınımlar" in out

    def test_yalniz_baslik_adi_satiri_kalir(self, conv):
        out = conv(self._kitap("Serbest salınımlar"), document_class="book")
        assert out.count("Serbest salınımlar") >= 2


# ---------------------------------------------------------------------------
# R16: denklem numarası
# ---------------------------------------------------------------------------

class TestR16DenklemNumarasi:
    @pytest.mark.parametrize("src,body,num", [
        (r"$$m\ddot{x} + kx = 0. (1.1)$$", r"m\ddot{x} + kx = 0.", "1.1"),
        (r"$$I = \int P d\tau, \qquad (1.2)$$", r"I = \int P d\tau,", "1.2"),
        (r"$$A v = Q, (2.1)$$", "A v = Q,", "2.1"),
        (r"$$x = y, (7)$$", "x = y,", "7"),
        (r"$$a = b \quad (3)$$", "a = b", "3"),
        ("$$\na = b\n(2.4)\n$$", "a = b", "2.4"),
        (r"$$E = 0.5^* P (12.5a)$$", "E = 0.5^* P", "12.5a"),
    ])
    def test_icerideki_numara_tag_olur(self, conv, src, body, num):
        out = conv(src)
        assert out.startswith("\\begin{equation*}")
        inner = out.split("\n", 1)[1].rsplit("\\end{equation*}", 1)[0]
        assert inner.strip() == f"{body.strip()}\n\\tag{{{num}}}"

    @pytest.mark.parametrize("src", [
        r"$$y = 2 \cdot (1.5)$$",     # çarpım
        r"$$y = f (2)$$",             # işlev uygulaması (noktasız numara)
        r"$$y = x + (1.5)$$",
        r"$$(1.2)$$",
    ])
    def test_numara_olmayan_parantez(self, conv, src):
        out = conv(src)
        assert r"\tag" not in out and out.startswith("\\[")

    def test_hazir_tag_ve_ayri_satir_numarasi_eskisi_gibi(self, conv):
        assert conv(r"$$a = b \tag{4.1}$$").count(r"\tag{") == 1
        out = conv("$$a = b$$\n\n(2.3)\n\nSonra.")
        assert "\\tag{2.3}" in out and "(2.3)" not in out
