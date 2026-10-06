"""FX1 (5 Ekim 2026): PDF dipnot sistemi + gövdeye sızan sayfa üst bilgisi.

Kararlar:
  KARAR 31  Eşlenen dipnot KAYNAKTAKİ numarasıyla basılır (\\footnote[N]{…});
            bölüm başında 1'den yeniden sayma yok; eşlenemeyen üst simgeli
            işaretlerle tutarlı.
  KARAR 32  Gövdeye sızmış, sayfa sınırında tekrar eden ve kesin tanınan sayfa
            üst/alt bilgisi silinir ve sayılır; emin olunmayana dokunulmaz.

Bulgular: V1 doğrulaması yeni hata 2, 3, 4; okuma bulguları (2026-09-30).

Tüm girdiler SENTETİKTİR (KARAR 26: testlerde kitap metni yok); yalnız yapı
(Markdown biçimi, <sup> işaretleri, satır sonları, numaralar) gerçek olguyla aynıdır.
"""
from __future__ import annotations

import re

import pytest

from latex_pipeline import marker_onarim as mo
from latex_pipeline.config import PipelineConfig
from latex_pipeline.converter import MarkdownToLatex


@pytest.fixture
def conv(tmp_path):
    def _run(markdown: str, **cfg_kwargs) -> str:
        cfg = PipelineConfig(document_class="book", output_dir=str(tmp_path), **cfg_kwargs)
        out = MarkdownToLatex(cfg).convert(markdown)
        assert "\x02" not in out and "\x00" not in out, "nöbetçi/yer tutucu sızdı"
        return out
    return _run


def _chapter(md: str) -> str:
    return "# Chapter 1 Synthetic Start\n\n" + md


def _footnote_args(tex: str):
    """(numara ya da None, metin) — \\footnote[N]{…} / \\footnote{…}, dengeli ayraç."""
    out = []
    for m in re.finditer(r"\\footnote(?:\[(\d+)\])?\{", tex):
        d, i = 1, m.end()
        while i < len(tex) and d:
            if tex[i] == "\\":
                i += 2
                continue
            d += {"{": 1, "}": -1}.get(tex[i], 0)
            i += 1
        out.append((m.group(1), tex[m.end():i - 1]))
    return out


# ===========================================================================
# Kalem 1 — KARAR 31: kaynak numarası
# ===========================================================================

class TestKarar31Numara:
    def test_eslenen_dipnot_kaynak_numarasiyla_basilir(self, conv):
        md = _chapter("The pumps were tested twice.<sup>7</sup> Then the crew left the site."
                      "<sup>8</sup>\n\nA plain middle paragraph with no marks at all.\n\n"
                      "<sup>7</sup> The second test ran overnight.\n\n"
                      "<sup>8</sup> Nobody stayed behind.\n")
        out = conv(md)
        assert "twice.\\footnote[7]{The second test ran overnight.} Then" in out
        assert "site.\\footnote[8]{Nobody stayed behind.}" in out
        assert "\\footnote{" not in out

    def test_bolum_basinda_bire_donmez(self, conv):
        # Kaynakta numaralar bölümler boyunca sürüyor (1, 2 | 3): LaTeX sayacı
        # bölümde sıfırlansa da basılan numara kaynaktakidir.
        md = ("# Chapter 1 First Part\n\nAlpha claim.<sup>1</sup> Beta claim.<sup>2</sup>\n\n"
              "<sup>1</sup> Note about alpha.\n\n<sup>2</sup> Note about beta.\n\n"
              "# Chapter 2 Second Part\n\nGamma claim.<sup>3</sup> More body text here.\n\n"
              "<sup>3</sup> Note about gamma.\n")
        nums = [n for n, _ in _footnote_args(conv(md))]
        assert nums == ["1", "2", "3"]

    def test_eslenmeyen_isaret_ayni_numarayla_ust_simge(self, conv):
        md = _chapter("First claim.<sup>5</sup> Second claim.<sup>6</sup> End of body.\n\n"
                      "<sup>6</sup> Only the second note survived.\n")
        out = conv(md)
        assert "claim.\\textsuperscript{5} Second" in out
        assert "\\footnote[6]{Only the second note survived.}" in out

    def test_epub_dipnotu_sayacla_kalir(self, conv):
        md = _chapter("A sentence with a note.[^kc-1] Next sentence.\n\n[^kc-1]: The note body.\n")
        out = conv(md)
        assert "note.\\footnote{The note body.}" in out
        assert "\\footnote[" not in out

    def test_simge_isaretli_dipnot_sayacli_kalir(self, conv):
        # \footnote[*] geçersizdir (isteğe bağlı bağımsız değişken sayı ister):
        # simge işaretli dipnot sayaçlı \footnote olur (kalan risk, rapor).
        md = _chapter("Claim with a star.<sup>*</sup> Body goes on.\n\n<sup>*</sup> Star note text.\n")
        out = conv(md)
        assert "star.\\footnote{Star note text.}" in out

    def test_tablo_hucresinde_numarali_footnotemark(self, conv):
        md = _chapter("| Item | Count |\n|---|---|\n| Valves checked<sup>4</sup> | 12 |\n"
                      "| Pipes replaced | 3 |\n\nA closing sentence after the table.\n\n"
                      "<sup>4</sup> Two valves were checked again later.\n")
        out = conv(md)
        tablo = out[out.index("\\begin{table}"):out.index("\\end{table}")]
        sonra = out[out.index("\\end{table}"):]
        assert "checked\\footnotemark[4]" in tablo
        assert "\\footnotetext[4]{Two valves were checked again later.}" in sonra
        assert "\\addtocounter{footnote}" not in out and "\\footnote{" not in out

    def test_dipnotlu_altyazi_paragraf_kalir_numarali(self, conv):
        # Dipnotlu altyazı şekle alınmaz (_is_caption_text, metin kaybı
        # koruması): paragraf olarak kalır, dipnot kaynak numarasıyla.
        md = _chapter("Body text before the figure goes here and ends.\n\n"
                      "![](_page_1_Figure_1.jpeg)\n\n"
                      "*Figure 1: Synthetic chart of daily visits per hour.<sup>2</sup> "
                      "Later hours are quieter.*\n\n"
                      "<sup>2</sup> Visits after midnight were not counted.\n")
        out = conv(md)
        assert "hour.\\footnote[2]{Visits after midnight were not counted.} Later" in out
        assert out.count("Visits after midnight") == 1

    def test_sekil_ortaminda_footnotemark(self, tmp_path):
        # \footnote şekil ortamının içinde metnini kaybeder: işaret
        # \footnotemark[N], metin ortamın ardında \footnotetext[N].
        c = MarkdownToLatex(PipelineConfig(document_class="book", output_dir=str(tmp_path)))
        c._footnote_marks = ["pdf-0"]
        c._footnote_defs = {"pdf-0": "Visits after midnight were not counted."}
        c._footnote_nums = {"pdf-0": "2"}
        body = ("\\begin{figure}[H]\n  \\caption*{\\textit{Figure 1: Chart.\x02F0\x02 Rest.}}\n"
                "\\end{figure}\nNext paragraph.")
        out = c._resolve_inline_marks(body)
        assert "Chart.\\footnotemark[2] Rest." in out
        assert out.index("\\end{figure}") < out.index(
            "\\footnotetext[2]{Visits after midnight were not counted.}")
        assert "\\addtocounter" not in out


# ===========================================================================
# Kalem 3 + 5 — tanımın hemen ardındaki (boş satırsız) blok/gövde (V1 yeni
# hata 4)
# ===========================================================================

class TestTanimArdiSinir:
    def test_ardindan_gorsel_gelen_tanim_eslenir(self, conv):
        md = _chapter("The crew logged every run.<sup>9</sup> Results came later.\n\n"
                      "<sup>9</sup> Two runs were logged by hand.\n"
                      "![](_page_2_Figure_1.jpeg)\n\nMore body text follows here.\n")
        out = conv(md)
        assert "run.\\footnote[9]{Two runs were logged by hand.} Results" in out
        assert "\\includegraphics" in out and out.count("Two runs were logged") == 1

    def test_ardindan_baslik_gelen_tanim_eslenir(self, conv):
        md = _chapter("The plant closed early that winter.<sup>5</sup>\n\n"
                      "<sup>5</sup> It reopened in the spring of the next year.\n"
                      "## Second Section Title\n\nNew section text begins here.\n")
        out = conv(md)
        assert "winter.\\footnote[5]{It reopened in the spring of the next year.}" in out
        assert "\\section{Second Section Title}" in out

    def test_ardindan_madde_gelen_tanim_eslenir(self, conv):
        # Liste sayfa kırılımında bölünmüş; ikinci parçanın önüne tanım düşmüş.
        md = _chapter("In this note we:\n\n- Describe the first step of the plan.<sup>4</sup>\n\n"
                      "<sup>4</sup> Some numbers were left out on purpose.\n"
                      "- Describe the second step of the plan in more detail.\n")
        out = conv(md)
        assert "plan.\\footnote[4]{Some numbers were left out on purpose.}" in out
        assert "Describe the second step" in out

    def test_ardindan_kod_citi_gelen_tanim_eslenir(self, conv):
        md = _chapter("The script printed a short line.<sup>39</sup>\n\n"
                      "<sup>39</sup> The real output was longer than this.\n"
                      "```\nprint('done')\n```\n")
        out = conv(md)
        assert "line.\\footnote[39]{The real output was longer than this.}" in out

    def test_govdeye_yapisik_tanim_ayrilir_ve_eslenir(self, conv):
        # Olgu yapısı: sayfa altındaki tanım ile sonraki sayfanın ilk gövde
        # paragrafı tek Markdown paragrafında (satır sonuyla) birleşmiş.
        md = _chapter("We paid for the extra hours ourselves.<sup>3</sup> The budget was small.\n\n"
                      "<sup>3</sup> The credits were free of charge during the whole period.\n"
                      "This report has two parts. First we describe the setup.\n")
        out = conv(md)
        assert ("ourselves.\\footnote[3]{The credits were free of charge during the whole "
                "period.} The budget") in out
        assert "\n\nThis report has two parts. First we describe the setup." in out

    def test_yapisik_kod_kimligi_satiri_ayrilir(self, conv):
        md = _chapter("The graphs stop at one date.<sup>15</sup> Later data was dropped.\n\n"
                      "<sup>15</sup> Activity after the cutoff date is out of scope.\n"
                      "zzPING\\_ALPHA\\_TO\\_BETA\\_7421\\_REPLY\n")
        out = conv(md)
        assert "date.\\footnote[15]{Activity after the cutoff date is out of scope.}" in out
        assert "zzPING" in out

    def test_iki_noktayla_biten_tanimin_listesi_ayrilmaz(self, conv):
        # Tanım ":" ile bitiyor: ardındaki liste dipnotun parçası olabilir.
        md = _chapter("Claim with options.<sup>2</sup> Body goes on.\n\n"
                      "<sup>2</sup> The options were the following:\n- first option\n")
        out = conv(md)
        assert "\\footnote" not in out

    def test_kisaltmayla_biten_tanim_ayrilmaz(self, conv):
        # "et al." kesin cümle sonu değil: ardındaki büyük harfli satır dipnotun
        # devamı olabilir, dokunulmaz.
        md = _chapter("Claim with a source.<sup>2</sup> Body goes on.\n\n"
                      "<sup>2</sup> See the survey by Brown et al.\nThe second line goes on.\n")
        out = conv(md)
        assert "\\footnote" not in out


class TestMarkerOnarimYapisikTanim:
    def test_yapisik_govde_bos_satirla_ayrilir(self):
        md = ("Body sentence that ends here.<sup>3</sup>\n\n"
              "<sup>3</sup> The credits were free of charge during the whole period.\n"
              "This report has two parts. First we describe the setup.\n")
        out, sayac, _ = mo.onar_ayrintili(md)
        assert sayac["dipnot_tanimi_ayrildi"] == 1
        assert "whole period.\n\nThis report has two parts." in out
        assert mo.onar(out) == out
        assert re.sub(r"\s", "", out) == re.sub(r"\s", "", md)

    def test_kucuk_harfli_govde_devami_ayrilmaz(self):
        # Kapanmış tanıma yapışık küçük harfli satır gövde cümlesinin devamıdır
        # (paragraf birleşmesi onu ele alır); bu adım dokunmaz.
        md = ("<sup>12</sup> The sandbox belonged to another customer.\n"
              "continued the earlier sentence of the body text here.\n")
        _, sayac, _ = mo.onar_ayrintili(md)
        assert sayac["dipnot_tanimi_ayrildi"] == 0

    def test_ardindan_gorsel_bos_satirla_ayrilir(self):
        md = ("<sup>20</sup> The name was shortened in this report.\n"
              "![](_page_9_Figure_0.jpeg)\n")
        out, sayac, _ = mo.onar_ayrintili(md)
        assert sayac["dipnot_tanimi_ayrildi"] == 1
        assert "report.\n\n![](_page_9_Figure_0.jpeg)" in out


# ===========================================================================
# Kalem 2 — birleşik tanımlar: dipnot içinde dipnot (V1 yeni hata 3)
# ===========================================================================

class TestBirlesikTanim:
    def test_ters_sirali_iki_tanim_bolunur(self, conv):
        # Marker iki tanımı tek paragrafta ve ters sırada yazmış ("38 … 37 …").
        md = _chapter("Many logs were edited by hand.<sup>37</sup> The edits hid a step.<sup>38</sup>"
                      "\n\n<sup>38</sup> The hidden step printed a fixed word. It never ran. "
                      "<sup>37</sup> We found these cases with a second scan.\n")
        out = conv(md)
        args = dict(_footnote_args(out))
        assert args["37"] == "We found these cases with a second scan."
        assert args["38"] == "The hidden step printed a fixed word. It never ran."
        assert "\\textsuperscript" not in out

    def test_uc_tanimli_zincir_bolunur(self, conv):
        md = _chapter("First event.<sup>72</sup> Second event.<sup>73</sup> Third event.<sup>74</sup>"
                      "\n\n<sup>74</sup> Note seventy four ends here. <sup>73</sup> Note seventy "
                      "three ends here. <sup>72</sup> Note seventy two ends here.\n")
        args = dict(_footnote_args(conv(md)))
        assert args == {"72": "Note seventy two ends here.",
                        "73": "Note seventy three ends here.",
                        "74": "Note seventy four ends here."}

    def test_isareti_bulunamayan_parca_bolunmez(self, conv):
        # İçteki "6"nın gövdede işareti yok: bölme kanıtlanamaz, metin eskisi
        # gibi 5'in dipnotunda kalır (dokunulmaz).
        md = _chapter("A claim with a note.<sup>5</sup> More text.\n\n"
                      "<sup>5</sup> The main note ends here. <sup>6</sup> A tail sentence follows.\n")
        args = _footnote_args(conv(md))
        assert len(args) == 1 and args[0][0] == "5"
        assert "The main note ends here. \\textsuperscript{6} A tail sentence follows." == args[0][1]

    def test_cumle_icindeki_ust_simge_bolmez(self, conv):
        md = _chapter("A claim with a note.<sup>5</sup> More text.\n\n"
                      "<sup>5</sup> The plate was 3<sup>6</sup> units wide in the end.\n")
        args = _footnote_args(conv(md))
        assert len(args) == 1 and args[0][0] == "5"

    def test_uzak_numara_bolmez(self, conv):
        md = _chapter("A claim.<sup>5</sup> Another claim.<sup>40</sup> End.\n\n"
                      "<sup>5</sup> The main note ends here. <sup>40</sup> Far number text.\n")
        args = _footnote_args(conv(md))
        assert [n for n, _ in args] == ["5"]
        assert "Far number text." in args[0][1]

    def test_ayri_tanimi_olan_numara_bolmez(self, conv):
        # 6'nın ayrı tanımı var: 5'in içindeki "6" o dipnota atıftır.
        md = _chapter("A claim.<sup>5</sup> Another claim.<sup>6</sup> End.\n\n"
                      "<sup>5</sup> As in the next note. <sup>6</sup> See there for more.\n\n"
                      "<sup>6</sup> The real sixth note.\n")
        args = dict(_footnote_args(conv(md)))
        assert args["6"] == "The real sixth note."
        assert "See there for more." in args["5"]


# ===========================================================================
# "# " başlık engeli yalnız numaralandırma yeniden başlıyorsa
# ===========================================================================

class TestBaslikEngeli:
    def test_surekli_numarada_baslik_asilir(self, conv):
        # Kaynakta numaralar sürüyor (44 | 45, 46): sayfanın ortasındaki "# "
        # başlığından sonra gelen tanımlar başlıktan önceki işaretlere eşlenir.
        md = ("# Chapter 1 Opening\n\nEarly claim.<sup>44</sup>\n\n<sup>44</sup> Early note.\n\n"
              "We asked two more questions.<sup>45</sup> The answers came fast.<sup>46</sup>\n\n"
              "# Main Data Sources\n\nThe data came from two places.\n\n"
              "<sup>45</sup> The first answer was short.\n\n<sup>46</sup> The second answer was long.\n")
        args = dict(_footnote_args(conv(md)))
        assert args["45"] == "The first answer was short."
        assert args["46"] == "The second answer was long."

    def test_yeniden_baslayan_numarada_baslik_asilmaz(self, conv):
        # Numara bölümde 1'e dönüyor: önceki bölümün işaretine eşlenmez.
        md = ("# Chapter 1 First\n\nClaim here.<sup>1</sup> Text.<sup>2</sup>\n\n"
              "<sup>2</sup> Note two of chapter one.\n\n"
              "# Chapter 2 Second\n\nOther body.\n\n<sup>1</sup>Belongs to chapter two.\n")
        out = conv(md)
        assert "\\textsuperscript{1}Belongs to chapter two." in out
        assert dict(_footnote_args(out)) == {"2": "Note two of chapter one."}


# ===========================================================================
# İşaret bağlamı: matematiğe alınan ya da bağlamı reddedilen dipnot
# işaretleri (bir raporda 27, 105, 109, 113)
# ===========================================================================

class TestIsaretBaglami:
    @pytest.mark.parametrize("govde, beklenen", [
        ("A <u>shared sandbox</u><sup>27</sup>, built later.",
         "\\uline{shared sandbox}\\footnote[27]{"),
        ("Roughly 30%<sup>27</sup> and more.", "30\\%\\footnote[27]{"),
        ("He wrote *{a quoted plan here}*<sup>27</sup>\n\nNext paragraph here.",
         "\\textit{\\{a quoted plan here\\}}\\footnote[27]{"),
        ("The tool `ABC123`<sup>27</sup> was used.", "\\texttt{ABC123}\\footnote[27]{"),
    ])
    def test_ust_simge_isaret_olur_ve_eslenir(self, conv, govde, beklenen):
        md = _chapter(govde + "\n\n<sup>27</sup> A short synthetic note.\n")
        out = conv(md)
        assert beklenen + "A short synthetic note.}" in out

    def test_kimlik_icindeki_isaret_eslenir(self, conv):
        # Kaynakta işaret ileti adının ortasında ("…ALPHA¹⁰⁵_REPLY…"); Marker
        # işaretin önüne boşluk koymuş.
        md = _chapter("zzPING\\_ALPHA <sup>105</sup>\\_REPLY\\_BETA\n\n"
                      "<sup>105</sup> We do not know what this name means.\n")
        out = conv(md)
        assert "ALPHA\\footnote[105]{We do not know what this name means.}\\_REPLY" in out

    @pytest.mark.parametrize("govde", [
        "Value (a+b)<sup>2</sup> here and more text.",
        "The area is *x*<sup>2</sup> in this case.",
        "The ratio 1<sup>2</sup>,5 stays as it is.",
    ])
    def test_matematik_ussu_isaret_olmaz(self, conv, govde):
        md = _chapter(govde + "\n\n<sup>2</sup> A note that must stay a paragraph.\n")
        out = conv(md)
        assert "\\footnote" not in out

    def test_tablo_hucresi_basindaki_isaret(self, conv):
        # Marker hücre içindeki işareti hücrenin başına kaydırmış ("| ⁹⁵ Reset…").
        md = _chapter("| Reason | Count |\n|---|---|\n| <sup>95</sup> Resetting the plan | 25 |\n"
                      "| Helping peers | 21 |\n\nText after the table goes on.\n\n"
                      "<sup>95</sup> Some thought the plan was hosted elsewhere.\n")
        out = conv(md)
        tablo = out[out.index("\\begin{table}"):out.index("\\end{table}")]
        assert "\\footnotemark[95]" in tablo
        assert "\\footnotetext[95]{Some thought the plan was hosted elsewhere.}" in out


# ===========================================================================
# Kalem 7 — KARAR 32: gövdeye sızan sayfa üst/alt bilgisi silinir ve sayılır
# ===========================================================================

def _mo(md: str):
    out, sayac, ayrinti = mo.onar_ayrintili(md)
    assert mo.onar(out) == out, "eşgüçlü değil"
    silinen = [d["deger"] for d in ayrinti if d["tur"] == "ust_bilgi_silindi"]
    # Kayıpsızlık: harf-rakam çoklu kümesi yalnız silinen üst bilgi (ve sayfa
    # numarası) kadar azalır.
    from collections import Counter
    an = lambda s: Counter(re.findall(r"[^\W_]", s))  # noqa: E731
    beklenen = an(md)
    for s in silinen + [d["deger"] for d in ayrinti if d["tur"] == "sayfa_no_silindi"]:
        beklenen.subtract(an(s))
    assert +beklenen == an(out), "harf-rakam çoklu kümesi silinenlerden fazla değişti"
    return out, sayac, silinen


class TestKarar32UstBilgi:
    def test_paragraf_sonuna_yapisik_ust_bilgi_silinir(self):
        # "6 1 Introduction": sayfa numarası + bölüm üst bilgisi, sayfa
        # sonundaki yarım cümlenin sonuna yapışmış.
        md = ("# 1 Introduction\n\n"
              "The first kind is easy to model. A plain example would be a system "
              "ruled by one balance law plus a tangled material response. For the 6 1 "
              "Introduction\n\n"
              "former a clean rule is known, whereas the latter must be learned from samples.")
        out, sayac, silinen = _mo(md)
        assert sayac["paragraf_birlesti"] == 1 and sayac["ust_bilgi_silindi"] == 1
        assert silinen == ["6 1 Introduction"]
        assert "For the former a clean rule" in out and "6 1 Introduction" not in out

    def test_birlesen_yarilar_arasindaki_ust_bilgi_silinir(self):
        md = ("# **Chapter 1 Introduction**\n\n"
              "- 3. **Blended tuning**: This group sits between the two others. Such "
              "schemes usually rely on a blend\n\n"
              "4 1 Introduction\n\n"
              "of tagged and untagged samples while fitting. For example, few pictures "
              "carry tags.")
        out, sayac, silinen = _mo(md)
        assert sayac["madde_birlesti"] == 1
        assert silinen == ["4 1 Introduction"]
        assert "rely on a blend of tagged and untagged samples" in out
        assert "4 1 Introduction" not in out

    def test_tekrar_eden_artan_numarali_ust_bilgi_silinir(self):
        md = ("## References\n\nA first complete paragraph about the topic ends here.\n\n"
              "References 33\n\nA second complete paragraph about the topic ends here.\n\n"
              "References 35\n\nA third complete paragraph about the topic ends here.\n")
        out, sayac, silinen = _mo(md)
        assert silinen == ["References 33", "References 35"]
        assert "References 3" not in out and "## References" in out

    def test_tek_gecen_ust_bilgi_kanitsiz_kalir(self):
        # Tek geçiş ve sayfa kırılımı kanıtı (birleşme) yok: emin değil.
        md = ("## Acknowledgments\n\nA first complete paragraph of thanks ends here.\n\n"
              "Acknowledgments xiii\n\nA second complete paragraph of thanks ends here.\n")
        out, sayac, silinen = _mo(md)
        assert silinen == [] and "Acknowledgments xiii" in out

    def test_baslikla_eslesmeyen_tekrar_silinmez(self):
        # Belgede "Field Notes" başlığı yok: yinelense de kesin tanınmıyor.
        md = ("# Chapter One\n\nA first complete paragraph ends here.\n\nField Notes 12\n\n"
              "A second complete paragraph ends here.\n\nField Notes 14\n\n"
              "A third complete paragraph ends here.\n\nwhere\n\nwhere\n\nwhere\n")
        out, sayac, silinen = _mo(md)
        assert silinen == [] and out.count("Field Notes") == 2 and out.count("where") == 3

    def test_icindekiler_satirlari_silinmez(self):
        # Art arda "ad + sayfa" satırları içindekiler listesidir.
        md = ("# Introduction\n\n# Methods\n\nContents\n\nIntroduction 1\n\nMethods 5\n\n"
              "A complete paragraph of body text ends here.\n\nIntroduction 3\n\n"
              "Another complete paragraph of body text ends here.\n")
        out, sayac, silinen = _mo(md)
        assert "Introduction 1" in out and "Methods 5" in out
        assert silinen == ["Introduction 3"]

    def test_azalan_numarali_tekrar_silinmez(self):
        md = ("## References\n\nA first complete paragraph ends here.\n\nReferences 40\n\n"
              "A second complete paragraph ends here.\n\nReferences 12\n\n"
              "A third complete paragraph ends here.\n")
        out, sayac, silinen = _mo(md)
        assert silinen == [] and "References 40" in out and "References 12" in out

    def test_sayac_turu_sozlesmede(self):
        assert "ust_bilgi_silindi" in mo.SAYAC_TURLERI
        assert mo.onar_rapor("")[1]["ust_bilgi_silindi"] == 0


# ===========================================================================
# Kalem 4 — çok paragraflı dipnot: devam paragrafları PDF kanıtıyla
# (aynı sayfa, dipnot puntosu, dipnot numarasının altında) dipnota katılır.
# ===========================================================================

def _pdf_yaz(yol, satirlar):
    """Sentetik tek sayfalık PDF: [(x, y, metin, punto)]."""
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    page = doc.new_page()
    for x, y, metin, punto in satirlar:
        page.insert_text((x, y), metin, fontsize=punto)
    doc.save(str(yol))
    return str(yol)


_GOVDE = "Body sentence that carries the marker."
_P1 = "The first paragraph of the note explains the setting in detail."
_P2 = "The second paragraph of the same note continues with more words here."
_P3 = "The third paragraph of the same note closes it with a final remark."


def _cok_paragrafli_pdf(yol, p2_punto=10):
    fitz = pytest.importorskip("fitz")
    gx = 72 + fitz.get_text_length(_GOVDE, fontsize=11)
    # Gövde (11 pt) belgede çoğunlukta; dipnot 10 pt, numarası 6 pt.
    dolgu = [(72, 120 + 16 * k, f"Filler body line number {k} keeps the body font common.", 11)
             for k in range(12)]
    return _pdf_yaz(yol, [
        (72, 80, _GOVDE, 11), (gx, 76, "18", 6),
        (72, 96, "More body text follows here on the same page.", 11), *dolgu,
        (72, 700, "18", 6), (80, 704, _P1, 10),
        (72, 730, _P2, p2_punto), (72, 756, _P3, 10)])


_COK_PARAGRAF_MD = ("# Chapter 1 Synthetic Start\n\n"
                    f"{_GOVDE}<sup>18</sup> More body text follows here on the same page.\n\n"
                    f"{_P2}\n\n{_P3}\n\n<sup>18</sup> {_P1}\n\n"
                    "A plain body paragraph after the note.\n")


class TestCokParagrafliDipnot:
    def test_devam_paragraflari_dipnota_katilir(self, conv, tmp_path):
        pdf = _cok_paragrafli_pdf(tmp_path / "kaynak.pdf")
        out = conv(_COK_PARAGRAF_MD, input_pdf=pdf)
        args = dict(_footnote_args(out))
        assert args["18"] == f"{_P1}\\par {_P2}\\par {_P3}"
        assert out.count("The second paragraph") == 1 and out.count("The third paragraph") == 1

    def test_govde_puntosundaki_paragraf_katilmaz(self, conv, tmp_path):
        # PDF'te ikinci paragraf gövde puntosunda: dipnotun devamı değil.
        pdf = _cok_paragrafli_pdf(tmp_path / "kaynak.pdf", p2_punto=11)
        out = conv(_COK_PARAGRAF_MD, input_pdf=pdf)
        args = dict(_footnote_args(out))
        assert _P2 not in args["18"]
        assert "\n\n" + _P2 in out

    def test_pdf_yoksa_dokunulmaz(self, conv):
        out = conv(_COK_PARAGRAF_MD)
        args = dict(_footnote_args(out))
        assert args["18"] == _P1

    def test_vurgu_icindeki_isaretin_dipnotuna_par_eklenmez(self, conv, tmp_path):
        # \par, \textit{…} gibi \long olmayan bir bağımsız değişkenin içinde
        # derleme hatasıdır: işaret vurgu içindeyse devam paragrafı katılmaz.
        pdf = _cok_paragrafli_pdf(tmp_path / "kaynak.pdf")
        md = _COK_PARAGRAF_MD.replace(f"{_GOVDE}<sup>18</sup>", f"*{_GOVDE}<sup>18</sup>*")
        out = conv(md, input_pdf=pdf)
        args = dict(_footnote_args(out))
        assert args["18"] == _P1 and "\\par" not in out
        assert "\n\n" + _P2 in out


class TestAyracSonrasiIsaret:
    # Kapanan ayraçtan sonraki üst simge önişlemde matematik olur ("(a+b)²");
    # aynı numaralı tanım pencerede varsa ve ardından düzyazı sürüyorsa işaret
    # sayılır (bir rapordaki 121 numaralı dipnot işareti).
    def test_ayrac_sonrasi_isaret_tanimla_eslenir(self, conv):
        md = _chapter("We used the time from the reply header (a fresh date)<sup>121</sup> to "
                      "rebuild the order of events.\n\n<sup>121</sup> This was often due to a "
                      "network that was left open.\n")
        out = conv(md)
        assert "date)\\footnote[121]{This was often due to a network that was left open.} to" in out

    def test_matematik_ussu_ayrac_sonrasi_kalir(self, conv):
        md = _chapter("The sum (a+b)<sup>2</sup> = c holds for every case here.\n\n"
                      "<sup>2</sup> A note that has no marker in the body.\n")
        out = conv(md)
        assert "(a+b)${}^{2}$ = c" in out and "\\footnote" not in out

    def test_tanimsiz_ayrac_sonrasi_matematik_kalir(self, conv):
        out = conv(_chapter("The value (in kelvin)<sup>3</sup> is small for this gas.\n"))
        assert "kelvin)${}^{3}$ is small" in out


def test_tek_harfli_kalin_degiskenin_ussu_matematik_kalir(conv):
    # "**v**<sup>1</sup>": kalın vektör + üs; kapanan vurgu kuralı yalnız en az
    # iki harf-rakamlı vurgulu metinde işaret sayar.
    out = conv(_chapter("We first replace **v**<sup>1</sup> by the new basis vector.\n"))
    assert "\\textbf{v}${}^{1}$" in out
