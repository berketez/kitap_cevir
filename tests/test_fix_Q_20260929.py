"""Kulvar Q (QC) düzeltme dalgası, 29 Eylül 2026 — bug avı 28 Eylül bulguları.

* F2-02: dil denetimi komut argümanının içini görmüyordu. ``\\footnote{}``,
  ``\\textit{}``, ``\\textbf{}``, ``\\caption{}`` içinde kaynak dilde kalan
  düzyazı sessizce geçiyordu. Argümanlar ve satırın dış metni artık ayrı
  birim olarak sınıflanır.
* F2-03: URL, DOI ya da arXiv içeren satır dil denetiminden BÜTÜNÜYLE
  muaftı. Artık yalnız bağlantı maskelenir; künye satırı ve kısa bağlantı
  satırı eskisi gibi muaftır.
* F2-12: cümle ortasındaki ``$$…$$`` (satır içinde display) için bulgu yoktu.
* F2-14: başlık denetimi İspanyolca başlığı "French" diye etiketliyordu.

Girdiler sentetiktir; QCEngine doğrudan çağrılır (sanitizer yok).
"""

from __future__ import annotations

import pytest

from latex_pipeline import qc_engine
from latex_pipeline.qc_engine import QCEngine


# ---------------------------------------------------------------------------
# Sentetik metinler
# ---------------------------------------------------------------------------

DE_PARA = (
    "Die Wärmeleitung in einem festen Körper wird durch das Fouriersche "
    "Gesetz beschrieben, nach dem der Wärmestrom proportional zum "
    "Temperaturgefälle ist. Wenn die Temperatur an einer Stelle höher ist "
    "als an einer anderen, fließt die Wärme stets von der wärmeren zur "
    "kälteren Stelle, bis sich ein Gleichgewicht einstellt und der Strom "
    "verschwindet."
)
FR_PARA = (
    "La pression dans un fluide au repos augmente avec la profondeur, car "
    "chaque couche doit supporter le poids des couches qui se trouvent "
    "au-dessus d'elle. Cette relation simple permet de calculer la force "
    "exercée sur les parois d'un réservoir et explique pourquoi les barrages "
    "sont plus épais à leur base qu'à leur sommet."
)
ES_PARA = (
    "La presión en un fluido en reposo aumenta con la profundidad, porque "
    "cada capa debe soportar el peso de las capas que se encuentran por "
    "encima de ella. Esta relación sencilla permite calcular la fuerza que "
    "se ejerce sobre las paredes de un depósito y explica por qué las presas "
    "son más gruesas en su base que en su parte superior."
)
IT_PARA = (
    "La pressione in un fluido in quiete aumenta con la profondità, perché "
    "ogni strato deve sostenere il peso degli strati che si trovano sopra di "
    "esso. Questa semplice relazione permette di calcolare la forza che si "
    "esercita sulle pareti di un serbatoio e spiega perché le dighe sono più "
    "spesse alla base che in cima."
)
EN_PARA = (
    "The pressure in a fluid at rest increases with depth, because every "
    "layer has to support the weight of all the layers that are above it. "
    "This simple relation allows us to compute the force that acts on the "
    "walls of a tank and it explains why dams are thicker at their base than "
    "at their top."
)
RU_PARA = (
    "Давление в покоящейся жидкости увеличивается с глубиной, потому что "
    "каждый слой должен выдерживать вес всех слоёв, которые находятся над "
    "ним. Это простое соотношение позволяет рассчитать силу, действующую на "
    "стенки резервуара, и объясняет, почему плотины толще у основания, чем "
    "наверху."
)
PARAS = {"de": DE_PARA, "fr": FR_PARA, "es": ES_PARA, "it": IT_PARA,
         "en": EN_PARA, "ru": RU_PARA}

TR_BEFORE = (
    "Isı iletimi katı cisimlerde sıcaklık farkından kaynaklanır ve bu süreç "
    "deneylerle ayrıntılı biçimde ölçülür."
)
TR_AFTER = (
    "Sonraki bölümde aynı yasanın akışkanlara nasıl uygulandığı "
    "gösterilecektir."
)
TR_SENT = "Bu cümle Türkçe yazılmış ve açıklamayı dipnotta veriyor."


def _body(*lines: str) -> str:
    return "\n\n".join((TR_BEFORE,) + lines + (TR_AFTER,)) + "\n"


def _analyze(tr: str, src: str = "") -> "qc_engine.QCReport":
    return QCEngine().analyze(tr, src, manual_translation=True)


def _issues(report, *names):
    return [i for i in report.issues if i.check_name in names]


def _critical_untranslated(report):
    return [i for i in _issues(report, "untranslated")
            if i.severity == "critical"]


# ---------------------------------------------------------------------------
# F2-02 — komut argümanlarının içi
# ---------------------------------------------------------------------------

class TestF202Arguments:

    @pytest.mark.parametrize("lang", sorted(PARAS))
    def test_dipnotta_kalan_paragraf_critical(self, lang):
        rep = _analyze(_body(TR_SENT + "\\footnote{" + PARAS[lang] + "}"))
        crit = _critical_untranslated(rep)
        assert crit, [(i.severity, i.message) for i in rep.issues]
        assert crit[0].line_num == 3

    @pytest.mark.parametrize("lang", ["de", "fr", "es", "en"])
    def test_tek_basina_italik_paragraf_critical(self, lang):
        rep = _analyze(_body("\\textit{" + PARAS[lang] + "}"))
        assert _critical_untranslated(rep), [
            (i.severity, i.message) for i in rep.issues]

    @pytest.mark.parametrize("lang", ["de", "fr", "es", "en"])
    def test_altyazida_kalan_paragraf_critical(self, lang):
        rep = _analyze(_body(
            "\\begin{figure}[H]\n\\centering\n\\caption{" + PARAS[lang]
            + "}\n\\end{figure}"))
        assert _critical_untranslated(rep)

    @pytest.mark.parametrize("lang", ["de", "es", "en"])
    def test_item_icinde_kalin_paragraf(self, lang):
        rep = _analyze(_body(
            "\\begin{itemize}\n\\item \\textbf{" + PARAS[lang]
            + "}\n\\end{itemize}"))
        assert _critical_untranslated(rep)

    def test_dipnot_bulgusu_arguman_adini_ve_uzunlugu_verir(self):
        rep = _analyze(_body(TR_SENT + "\\footnote{" + DE_PARA + "}"))
        msg = _critical_untranslated(rep)[0].message
        assert "German" in msg and "\\footnote argument" in msg
        assert f"({len(DE_PARA)} chars)" in msg

    def test_dipnot_bulgusu_otomatik_duzeltilmez(self):
        # Yeniden çeviri Türkçe kısmıyla birlikte bütün satırı modele
        # gönderirdi (Yol B'de de); bulgu rapor-only.
        rep = QCEngine().analyze(
            _body(TR_SENT + "\\footnote{" + EN_PARA + "}"), "",
            manual_translation=False)
        crit = _critical_untranslated(rep)
        assert crit and crit[0].auto_fixable is False

    def test_turkce_harfi_yalniz_baglanti_metninde_olan_paragraf(self):
        # "bağlantı" ğ/ı taşıyor; eskiden Almanca paragrafın tamamı Türkçe
        # sayılıyordu (classify_latin_line Türkçeye özgü harfte None).
        rep = _analyze(_body(
            DE_PARA + " (\\href{https://example.org}{bağlantı})"))
        assert _critical_untranslated(rep)

    def test_ingilizce_paragraf_turkce_sekil_baglantisi_karisik_degil(self):
        # Bir test kitabında: İngilizce paragrafta Türkçe şekil bağlantısı
        # (\href{#page-…}{Şekil~….}); metin sentetik (KARAR 26).
        # İngilizce paragraf yalnız MEDIUM "mixed_language_line" alıyordu.
        rep = _analyze(_body(
            EN_PARA + " These types are shown in \\href{#page-188-0}"
            "{Şekil~6.4.} and they are discussed below in some detail."))
        assert _critical_untranslated(rep)
        assert not _issues(rep, "mixed_language_line")

    def test_dipnotta_italik_paragraf_ic_ice(self):
        rep = _analyze(_body(
            TR_SENT + "\\footnote{Bkz. \\textit{" + DE_PARA + "}}"))
        assert _critical_untranslated(rep)

    def test_satira_tasan_dipnot_metni_denetlenir(self):
        # Kapanmayan argüman satır sonuna kadar alınır (metin kaybolmaz).
        rep = _analyze(_body(TR_SENT + "\\footnote{" + FR_PARA))
        assert _critical_untranslated(rep)

    # --- yanlış pozitif korumaları ---

    def test_turkce_cumlede_ingilizce_kitap_adi_muaf(self):
        rep = _analyze(_body(
            "Ashby ve Morland'ın \\textit{The Measuring Habit: A Practical "
            "Handbook for Reading Figures in the Press, in Business, and in "
            "Daily Life} adlı kitabı rakamları okumak için pratik bir kılavuzdur."))
        assert not _issues(rep, "untranslated", "mixed_language_line")

    def test_turkce_dipnot_muaf(self):
        rep = _analyze(_body(
            TR_SENT + "\\footnote{Çevirmen notu: Bu terim özgün metinde "
            "farklı bir anlamda kullanılmış, burada bağlama göre "
            "çevrilmiştir.}"))
        assert not _issues(rep, "untranslated", "mixed_language_line")

    def test_dipnottaki_kunye_muaf(self):
        rep = _analyze(_body(
            TR_SENT + "\\footnote{Varga KT, Osei M (2019) Explaining the "
            "outputs of gradient boosted ensembles in retail credit scoring "
            "systems. J Comput Stat Ser A 41(3):512–538.}"))
        assert not _issues(rep, "untranslated")

    def test_dipnottaki_gazete_kunyesi_muaf(self):
        rep = _analyze(_body(
            TR_SENT + "\\footnote{The Northern Courier, “City Council Approves "
            "Plan to Rebuild the Old Harbour Bridge.” Marta "
            "Lindgren, March 14, 2018.}"))
        assert not _issues(rep, "untranslated")

    def test_kisa_ingilizce_terim_dipnotu_muaf(self):
        rep = _analyze(_body(TR_SENT + "\\footnote{machine learning}"))
        assert not _issues(rep, "untranslated")

    def test_baslik_satiri_check11e_birakilir(self):
        rep = _analyze(_body("\\section{\\textit{" + EN_PARA[:60] + "}}"))
        assert not _issues(rep, "untranslated")


class TestProseCommandArgs:

    def test_dis_duzey_argumanlar(self):
        text = ("A \\textit{b \\textbf{c}} d \\footnote[3]{e} "
                "\\href{https://x}{f} \\caption[k]{g}")
        got = [(c, a) for c, a, _s, _e in qc_engine.prose_command_args(text)]
        assert got == [("textit", "b \\textbf{c}"), ("footnote", "e"),
                       ("href", "f"), ("caption", "g")]

    def test_kapanmayan_arguman_satir_sonuna_kadar(self):
        got = qc_engine.prose_command_args("x \\footnote{yarım metin")
        assert got[0][1] == "yarım metin"

    def test_kacisli_ters_bolu_komut_sayilmaz(self):
        assert qc_engine.prose_command_args("a \\\\textit b") == []

    def test_satir_sonundan_sonraki_komut_sayilir(self):
        got = qc_engine.prose_command_args("metin\\\\\\textit{x y}")
        assert [(c, a, s) for c, a, s, _e in got] == [("textit", "x y", 7)]


class TestUnitSourceLanguage:
    """Modül düzeyi sözleşme (KARAR 18 EPUB kapsamı da kullanabilir)."""

    @pytest.mark.parametrize("lang", sorted(PARAS))
    def test_paragraf_dili(self, lang):
        assert qc_engine.unit_source_language(PARAS[lang]) == lang

    @pytest.mark.parametrize("text", [
        TR_BEFORE, "machine learning", "Principia Mathematica",
        "Varga KT, Osei M (2019) Explaining the outputs of boosted "
        "ensembles. J Comput Stat Ser A 41(3):512–538.",
        "", "   ",
    ])
    def test_turkce_kisa_ve_kunye_none(self, text):
        assert qc_engine.unit_source_language(text) is None

    def test_ham_latex_argumani_korunur(self):
        assert qc_engine.unit_source_language(
            "\\textit{" + DE_PARA + "}") == "de"

    def test_web_kunyesi_yalniz_web_ref_ile_muaf(self):
        ref = ("The Northern Courier, “City Council Approves Plan to Rebuild "
               "the Old Harbour Bridge.” Marta Lindgren, March 14, "
               "2018.")
        assert qc_engine.unit_source_language(ref, web_ref=True) is None


# ---------------------------------------------------------------------------
# F2-03 — bağlantılı satır
# ---------------------------------------------------------------------------

class TestF203Links:

    @pytest.mark.parametrize("lang", sorted(PARAS))
    def test_url_dipnotlu_paragraf_critical(self, lang):
        rep = _analyze(_body(
            PARAS[lang] + "\\footnote{\\url{https://example.org/kaynak}}"))
        assert _critical_untranslated(rep), [
            (i.severity, i.message) for i in rep.issues]

    @pytest.mark.parametrize("lang", sorted(PARAS))
    def test_arxiv_atifli_paragraf_critical(self, lang):
        rep = _analyze(_body(PARAS[lang] + " (arXiv:1234.5678)"))
        assert _critical_untranslated(rep)

    def test_doi_li_paragraf_critical(self):
        rep = _analyze(_body(EN_PARA + " DOI: 10.1000/xyz123"))
        assert _critical_untranslated(rep)

    # --- eskisi gibi muaf kalanlar ---

    @pytest.mark.parametrize("line", [
        "Varga KT, Osei M (2019) Explaining the outputs of gradient "
        "boosted ensembles in retail credit scoring systems. J Comput Stat "
        "Ser A 41(3):512–538. https://arxiv.org/pdf/1234.56789.pdf",
        "\\item Moreau PJ (2020a) Reasoning about chance: a short exercise "
        "with coins. Stat Educ Rev 5: 100217. "
        "https://doi.org/10.1016/j.ster.2020.100217",
        "Hale RM (2011) Book review: counting, slowly and well by Anna "
        "Berg; Signal: better choices about money, time, and "
        "travel by Peter J. Moss and Lena K. Ward. May. Risk Stud "
        "29(5). https://doi.org/10.1111/j.1234-5678.2011.00456.x",
        "\\href{part0011.html#endnote-2}{2}The Northern Courier®, “City "
        "Council Approves Plan to Rebuild the Old Harbour Bridge.” "
        "Marta Lindgren, March 14, 2018, \\href{https://www.example.com/a/b}"
        "{https://www.example.com/a/b}",
        "\\textbf{Harbor & Stone Web sitesini http://www.example.com "
        "adresinden ziyaret edin.}",
        "http://www.example.org/SPECIALS/ESA Liaison Office in "
        "Canada/QWERTY7ZXC 0.html",
    ])
    def test_kunye_ve_baglanti_satiri_muaf(self, line):
        rep = _analyze(_body(line))
        assert not _issues(rep, "untranslated", "mixed_language_line"), [
            (i.severity, i.message) for i in rep.issues]

    def test_yazar_yil_ile_baslamayan_duzyazi_kunye_sayilmaz(self):
        assert not qc_engine._looks_like_reference(
            "As shown by Smith (2019) the pressure in a fluid increases.")
        assert qc_engine._looks_like_reference(
            "Lowe J, Haddad H, van Dijk AM, Okafor EA (2019 Mar) "
            "Measures to limit indoor noise exposure.")

    def test_url_maskesi_dipnot_kapanisini_korur(self):
        masked = qc_engine._URL_TOKEN_RE.sub(
            " ", "x\\footnote{\\url{https://example.org/k}}")
        assert masked.endswith("{\\url{ }}")


# ---------------------------------------------------------------------------
# F2-12 — cümle ortasında $$…$$
# ---------------------------------------------------------------------------

MID = ("Toplam parçacık sayısı $$n_{tot}$$ değeri deneyde ölçülür ve "
       "sonuçlar tabloda verilir.")


class TestF212MidSentenceDisplay:

    def _fi(self, rep):
        return [i for i in _issues(rep, "formula_integrity")
                if "inside a sentence" in i.message]

    def test_cumle_ortasinda_display_bulgu(self):
        src = ("The total number of particles $n_{tot}$ is measured in the "
               "experiment and the results are listed in the table.\n")
        rep = _analyze(_body(MID), src)
        fi = self._fi(rep)
        assert len(fi) == 1 and fi[0].line_num == 3
        assert fi[0].severity == "medium" and fi[0].auto_fixable is False

    def test_kaynak_olmadan_da_bulgu(self):
        assert self._fi(_analyze(_body(MID)))

    def test_kaynakta_da_ayni_bicimde_ise_bulgu_yok(self):
        src = ("The total number $$n_{tot}$$ is measured in the experiment "
               "and listed.\n")
        assert not self._fi(_analyze(_body(MID), src))

    @pytest.mark.parametrize("line", [
        "$$E = mc^2$$",
        "Enerji şu bağıntıyla verilir: $$E = mc^2$$",
        "$$E = mc^2$$ \\label{eq:enerji}",
        "Açı $\\alpha$$\\beta$ olarak ölçülür ve tabloya yazılır.",
        "% Toplam $$n$$ değeri yorumda kalır",
    ])
    def test_cumle_ortasi_olmayanlar(self, line):
        assert not self._fi(_analyze(_body(line)))


# ---------------------------------------------------------------------------
# F2-14 — başlık dil adı
# ---------------------------------------------------------------------------

def _repeat(para: str, n: int = 6) -> str:
    return "\n\n".join([para] * n) + "\n"


class TestF214HeadingLanguage:

    def test_ispanyolca_baslik_spanish(self):
        src = "\\section{La presión en un fluido en reposo}\n\n" + _repeat(
            ES_PARA)
        tr = ("\\section{La presión en un fluido en reposo}\n\n"
              + TR_BEFORE + "\n")
        heads = _issues(_analyze(tr, src), "untranslated_heading")
        assert heads and "(Spanish)" in heads[0].message, [
            h.message for h in heads]

    def test_kaynak_bilinmiyorsa_baslik_yine_bayrakli(self):
        # Kaynak yoksa dil adı sınıflandırıcınındır; tespit değişmez.
        tr = "\\section{La presión en un fluido en reposo}\n\n" + TR_BEFORE
        assert _issues(_analyze(tr), "untranslated_heading")

    def test_baskin_dil_kaynaga_cevrilmez(self):
        engine = QCEngine()
        engine._src_ctx = engine._source_context(_repeat(IT_PARA))
        assert engine._src_ctx.lang == "it"
        # "La presión..." fr=es=it eşitliği -> kaynağın dili
        assert engine._prefer_source_language(
            "fr", "La presión en un fluido en reposo") == "it"
        # Açıkça Fransızca (işlev kelimeleri İtalyancayı 2 kat geçer)
        assert engine._prefer_source_language(
            "fr", "Les forces et les moments sur les ailes du avion") == "fr"

    def test_ingilizce_ya_da_rusca_kaynakta_degismez(self):
        engine = QCEngine()
        engine._src_ctx = engine._source_context(_repeat(EN_PARA))
        assert engine._prefer_source_language(
            "fr", "La presión en un fluido en reposo") == "fr"
