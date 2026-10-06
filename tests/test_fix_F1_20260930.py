"""F1 kulvarı düzeltmeleri (30 Eylül 2026): "Yerel model ile çevir" zinciri.

Model testlerinin (RAPOR MT-2..MT-10) bulguları için kalıcı regresyon
testleri. Her test, model testi çıktısındaki GERÇEK satırı küçük bir girdiyle
yeniden üretir:

* MT-2 — ``\\caption*{...}`` altyazıları hiç çevrilmiyordu; retranslator
  altyazıyı ham LaTeX'iyle MT'ye verip parantezleri kaydırtıyordu.
* MT-3 — çevrilmemiş İngilizce altyazıda "and" -> "ve" (sanitizer).
* MT-4 — "Third Law: Bir gezegenin…", "\\textbf{First Law:} …" etiketleri.
* MT-7 — cümle başında Almanca kalan öbek ("Dieses Kapitel yalnızca…").
* MT-9 — Türkçe taraf yanlış terim ("nozül" -> "lüle") İngilizce dışı
  kaynakta da düzeltilir; İngilizce yol değişmez.
* MT-10 — ``\\textsuperscript`` MT'ye ham gidiyordu; Almanca atıf
  ("Abb. 1.1") korunmuyor, parçalı yolda parantez düşüyordu.
* MT-5/MT-6 — bilinçli dönüşümler (içerik korunur) belgelenir.

Model yüklenmez, gerçek sunucuya bağlanılmaz (sahte Ollama: tests/fakes).
"""
from __future__ import annotations

import re

import pytest

from test_fix_Y1_20260929 import (  # noqa: F401  (fikstür)
    _chat_texts, fake_turkish, ollama_translator,
)


def _src_texts(srv) -> list:
    """Sahte sunucuya giden istemlerin çevrilecek metin kısmı."""
    return [t.split("\n\n", 1)[-1] for t in _chat_texts(srv)]


def _caption_of(out: str, number: str) -> str:
    m = re.search(r"\\caption\*?(?:\[[^\]]*\])?\{(.*)\}\s*$", out[out.index(number) - 40:],
                  re.M)
    return m.group(0) if m else ""


# ===========================================================================
# MT-2 — \caption* altyazıları çevrilir
# ===========================================================================
EN_FIG_BODY = r"""\chapter{Kepler}

Stated in current notation, the three rules can be summarised briefly in this chapter of the book.

\begin{figure}[!htb]
  \centering
  \includegraphics[width=0.8\textwidth]{_page_1_Picture_4.png}
  \caption*{Figure 1.2: Outline of Kepler's ellipse and its swept areas.}
\end{figure}

\begin{figure}[!htb]
  \centering
  \includegraphics[width=0.8\textwidth]{_page_2_Picture_1.png}
  \caption[Body axes]{Figure 2.1: Non-inertial and body axes.}
\end{figure}
"""


class TestMT2Altyazi:
    def test_yildizli_altyazi_cevrilir(self, ollama_translator):
        lt, srv = ollama_translator("en")
        out = lt.translate(EN_FIG_BODY)
        sent = _src_texts(srv)
        # Altyazı metni (etiket yer tutucuda) modele gitti.
        assert any(t.endswith(": Outline of Kepler's ellipse and its swept areas.")
                   for t in sent), sent
        assert "Outline" not in out and "Kepler's" not in out
        cap = [l for l in out.split("\n") if "\\caption*" in l][0]
        assert cap.strip().startswith("\\caption*{\u015eekil\u00a01.2:")
        assert cap.strip().endswith("}")

    def test_kisa_altyazi_da_cevrilir_satir_iki_kez_gitmez(self, ollama_translator):
        lt, srv = ollama_translator("en")
        out = lt.translate(EN_FIG_BODY)
        sent = _src_texts(srv)
        assert "Body axes" in sent                          # kısa altyazı
        assert not any("\\caption" in t for t in sent), sent  # ham satır yok
        line = [l for l in out.split("\n") if "\\caption[" in l][0]
        assert "Body" not in line and "Non-inertial" not in line
        assert re.fullmatch(r"\s*\\caption\[[^\]]+\]\{\u015eekil\u00a02\.1: .+\}",
                            line), line

    def test_almanca_kalin_etiketli_altyazi(self, ollama_translator):
        body = ("\\begin{figure}[H]\n  \\centering\n"
                "  \\includegraphics{a.png}\n"
                "  \\caption*{\\textbf{Abbildung 1.1:} Temperaturverlauf in einer "
                "ebenen Wand bei stationärer Wärmeleitung}\n\\end{figure}\n")
        lt, srv = ollama_translator("de")
        out = lt.translate(body)
        cap = [l for l in out.split("\n") if "\\caption*" in l][0].strip()
        assert cap.startswith("\\caption*{\\textbf{\u015eekil\u00a01.1:} ")
        assert cap.endswith("}") and "Temperaturverlauf" not in cap
        # Etiket deterministik çevrildi, modele gitmedi.
        assert "Abbildung 1.1:" not in _src_texts(srv)

    def test_grup2_altyazi_metni(self):
        # Grup 2 = altyazı metni (_CAPTION_RE grup sırası sözleşmesi).
        from latex_pipeline.translator import _CAPTION_RE
        m = _CAPTION_RE.search("\\caption*[K]{Metin {iç} burada}")
        assert m.group(2) == "Metin {iç} burada"


# ===========================================================================
# MT-3 — kaynak dilde kalmış cümleye Türkçe sözcük değişimi uygulanmaz
# ===========================================================================
class TestMT3VeDegisimi:
    def _san(self, body):
        from latex_pipeline.sanitizer import LatexSanitizer
        return LatexSanitizer().sanitize(body)[0]

    def test_ingilizce_altyazida_and_kalir(self):
        line = "  \\caption*{\u015eekil 1.2: Outline of Kepler's ellipse and its swept areas.}"
        assert line in self._san(line + "\n")

    def test_turkce_cumlede_and_duzeltilir(self):
        out = self._san("Bu yöntem hızlı and güvenilir çalışır.\n")
        assert "hızlı ve güvenilir" in out

    def test_yalniz_turkce_cumle_duzeltilir(self):
        line = ("Bu yöntem hızlı and güvenilir çalışır. The second sentence "
                "stays and remains English.")
        out = self._san(line + "\n")
        assert "hızlı ve güvenilir" in out
        assert "stays and remains English." in out

    def test_retranslator_altyaziyi_yeniden_cevirir(self):
        from latex_pipeline.retranslator import scan_and_retranslate
        calls = []

        def tr(t):
            calls.append(t)
            return fake_turkish(t)
        line = "  \\caption*{\u015eekil 1.2: Outline of Kepler's ellipse and its swept areas.}"
        res = scan_and_retranslate(line + "\n", translator_fn=tr)
        assert calls == ["Outline of Kepler's ellipse and its swept areas."]
        assert res.retranslated_lines == 1
        new = res.output_tex.strip()
        assert new.startswith("\\caption*{\u015eekil 1.2: ") and new.endswith("}")
        assert "Outline" not in new

    def test_turkce_altyazi_mtye_gitmez(self):
        from latex_pipeline.retranslator import scan_and_retranslate
        calls = []
        line = "  \\caption*{\u015eekil 2.2: Yörünge düzleminde problemin gösterimi.}"
        res = scan_and_retranslate(line + "\n", translator_fn=lambda t: calls.append(t))
        assert calls == [] and res.output_tex == line + "\n"

    def test_kayan_parantezli_altyazi_kuyrugu(self):
        # Almanca model testinde model parantezi kaydırmıştı; kuyruk kabuğa dokunmadan
        # çevrilir, iskelet korunur.
        from latex_pipeline.retranslator import scan_and_retranslate
        line = ("  \\caption*{\\textbf{\u015eekil} 1.1:} Temperaturverlauf in einer "
                "ebenen Wand bei stationärer Wärmeleitung")
        res = scan_and_retranslate(line + "\n", translator_fn=fake_turkish,
                                   source_lang="de")
        new = res.output_tex.strip()
        assert new.startswith("\\caption*{\\textbf{\u015eekil} 1.1:} ")
        assert "Temperaturverlauf" not in new

    def test_iskeleti_bozan_ceviri_reddedilir(self):
        # gpt-oss testinde model \caption* kabuğunu sildi. Kabuk artık MT'ye gitmez;
        # model metne LaTeX komutu eklerse sonuç reddedilir (kayıp yok).
        from latex_pipeline.retranslator import scan_and_retranslate
        line = "  \\caption*{\u015eekil 1.1: Equal areas in equal times.}"
        res = scan_and_retranslate(
            line + "\n", translator_fn=lambda t: "\\textbf{" + fake_turkish(t))
        assert res.output_tex == line + "\n" and res.failed_lines == 1


# ===========================================================================
# MT-4 / MT-7 — etiket ve cümle başı öbeği
# ===========================================================================
class TestMT4MT7Obek:
    def _rt(self, line, lang="en"):
        from latex_pipeline.retranslator import scan_and_retranslate
        calls = []

        def tr(t):
            calls.append(t)
            return fake_turkish(t)
        res = scan_and_retranslate(line + "\n", translator_fn=tr,
                                   heading_translator_fn=tr, source_lang=lang)
        return res, calls

    def test_duz_etiket(self):
        rest = ("Gezegenin dolanım süresinin karesi, Güneş’e uzaklığının "
                "küpüyle birlikte büyür.")
        res, calls = self._rt("Third Law: " + rest)
        assert calls == ["Third Law"]
        assert res.output_tex.strip() == fake_turkish("Third Law") + ": " + rest

    def test_kalin_etiket(self):
        rest = ("Net kuvvet yoksa cisim ya durur ya da aynı hızla "
                "dümdüz yoluna devam eder.")
        res, calls = self._rt("\\textbf{First Law:} " + rest)
        assert calls == ["First Law"]
        assert res.output_tex.strip() == (
            "\\textbf{" + fake_turkish("First Law") + ":} " + rest)

    def test_turkce_etiket_dokunulmaz(self):
        line = ("\\textbf{Birinci Yasa:} Net kuvvet yoksa cisim ya durur "
                "ya da aynı hızla dümdüz yoluna devam eder.")
        res, calls = self._rt(line)
        assert calls == [] and res.output_tex.strip() == line

    @pytest.mark.parametrize("line", [
        "Dieses Kapitel yalnızca ısı iletimini ele alır; parçacıklar "
        "makroskopik ölçekte hareket etmez.",
        "Das Minuszeichen ifadesi, ısının her zaman yüksek sıcaklıktan düşük "
        "sıcaklığa aktığını belirtir.",
    ])
    def test_almanca_cumle_basi(self, line):
        res, calls = self._rt(line, "de")
        assert calls == [line]
        assert res.retranslated_lines == 1
        assert res.output_tex.split()[0] not in ("Dieses", "Das")

    def test_kisa_tam_almanca_cumle(self):
        tr_rest = ("Bu problemin çözümü genellikle zaman alıcıdır; ancak "
                   "neyse ki önemli bir istisna vardır.")
        line = "In \\textit{a} wird die Wärmeleitfähigkeit behandelt. " + tr_rest
        res, calls = self._rt(line, "de")
        assert len(calls) == 1 and "wird die" in calls[0]
        assert "wird" not in res.output_tex and tr_rest in res.output_tex
        assert "\\textit{a}" in res.output_tex

    @pytest.mark.parametrize("lang,line", [
        ("de", "Joseph Fourier’a göre, ısı akısı ile sıcaklık alanı arasındaki "
               "temel ilişki böyle adlandırılmıştır."),
        ("en", "Kepler’in ikinci yasası, kapalı olmayan yörüngelerde "
               "dolaşan cisimler için de geçerlidir."),
        ("en", "Uydu gezegenin çevresinde eliptik bir yolda hareket eder ve "
               "hızı değişir."),
        ("en", "Örnek 2.1: Bir uydunun yörüngesi hesaplanır."),
        ("en", "Not: Bu değer yaklaşıktır."),
    ])
    def test_turkce_cumle_dokunulmaz(self, lang, line):
        res, calls = self._rt(line, lang)
        assert calls == [] and res.output_tex.strip() == line

    def test_latince_alinti_obek_sayilmaz(self):
        from latex_pipeline.retranslator import _leading_source_run
        assert _leading_source_run(
            "Lex I - Corpus omne perseverare in statu suo quiescendi vel "
            "movendi uniformiter in directum.", "de") is None

    def test_duzelmeyen_ceviri_ozgunu_korur(self):
        from latex_pipeline.retranslator import scan_and_retranslate
        line = ("Dieses Kapitel yalnızca ısı iletimini ele alır; parçacıklar "
                "makroskopik ölçekte hareket etmez.")
        res = scan_and_retranslate(line + "\n", translator_fn=lambda t: t,
                                   source_lang="de")
        assert res.output_tex == line + "\n" and res.failed_lines == 1

    @pytest.mark.parametrize("line", [
        # İki model testinde yazar adı "etiket" sanılıyordu.
        "  \\item [1] K. Hartwig: \\textit{Teknik Isı Teorisi}. 3. Auflage. "
        "Kassel: Tannberg Yayınevi, 2019.",
        "  \\item [3] E. Wendlandt: Elektronik muhafazalarındaki temas "
        "dirençleri. \\textit{Uygulamalı Termodinamik Dergisi} 12 (2021), "
        "S. 45–61.",
    ])
    def test_kunye_satirinda_yazar_etiket_sayilmaz(self, line):
        res, calls = self._rt(line, "de")
        assert not any("Hartwig" in c or "Wendlandt" in c for c in calls), calls

    @pytest.mark.parametrize("lang,line", [
        # Yanlış alarm taraması (output/dil_fr, dil_es EXPORT_TR.tex; Türkçe
        # çeviri): Fransızca işlev sözcüğü "ses" Türkçe "ses" ile çakışıyor;
        # "ü" Fransızcada yok -> "dünyada" Türkçe kanıtı.
        ("fr", "dünyada ses getirmemesi olanaksızdır!"),
        # Diyalog tiresi kelimeye yapışık: "—Çok" Türkçe sayılmalı.
        ("fr", "—Çok ses mi? diye bağırdı tutkulu bir topçu."),
        # Özel ad parçacıkları + kesme işaretli ekli ad.
        ("fr", "de la Rue'nün elde ettiği muhteşem Ay fotoğraf plakalarına "
               "bakınız."),
        ("de", "von der Heydt’ın ölçümleri bu sonucu doğrular ve tabloda "
               "verilir."),
    ])
    def test_turkce_cumle_yanlis_alarm_yok(self, lang, line):
        from latex_pipeline.retranslator import _leading_source_run
        assert _leading_source_run(line, lang) is None

    @pytest.mark.parametrize("lang,line", [
        ("fr", "Dans ce chapitre yalnızca ısı iletimi ele alınır."),
        ("es", "En este capítulo yalnızca ısı iletimi ele alınır."),
        # Parçacık kuralı büyük harfli ad istemez: kaçak yakalanmaya devam.
        ("fr", "de la paroi yüzeyi sıcak kalır ve ısı akar."),
    ])
    def test_kaynak_dil_obegi_hala_yakalanir(self, lang, line):
        from latex_pipeline.retranslator import _leading_source_run
        assert _leading_source_run(line, lang) == ("mixed", 0, len(line))

    def test_ayri_kesme_ekli_satir_obek_sayilmaz(self):
        # Almanca model testinde "’den" kesme işareti kırpılınca Almanca "den"
        # sanılıyordu; satır Türkçedir.
        line = "  \\item Gl. (1.5) ’den gelen ısı akısını belirlemek"
        res, calls = self._rt(line, "de")
        assert calls == [] and res.output_tex.strip() == line.strip()


# ===========================================================================
# MT-9 — Türkçe taraf yanlış terim düzeltmesi kaynak dilden bağımsız
# ===========================================================================
class TestMT9YanlisTerim:
    def _lt(self, lang):
        from latex_pipeline.config import PipelineConfig
        from latex_pipeline.translator import LatexTranslator
        return LatexTranslator(pipeline_config=PipelineConfig(translate=True),
                               mt_engine="ollama:sahte", source_lang=lang,
                               target_lang="tr", book_type="science")

    @pytest.mark.parametrize("lang", ["de", "ru", "fr"])
    def test_ingilizce_disi_kaynakta_duzeltilir(self, lang):
        lt = self._lt(lang)
        out = lt._apply_glossary("Gazlar nozül boğazında hızlanır; Nozül "
                                 "çıkışı $p_e$ ile verilir.")
        assert "nozül" not in out.lower()
        assert "lüle boğazı" in out and "Lüle çıkışı" in out and "$p_e$" in out

    def test_ingilizce_yol_degismez(self):
        # İngilizcede Türkçe taraf düzeltmesi Phase 5.75'in (verify_terms)
        # işidir; çeviri segmenti burada değişmez.
        lt = self._lt("en")
        text = "Gazlar nozül boğazında hızlanır."
        assert lt._apply_glossary(text) == text

    def test_uctan_uca_rusca_model_ciktisi_duzeltilir(self, ollama_translator):
        # Model (sahte) "nozül" yazar; yerel çeviri zincirinin çıktısında
        # Türkçe taraf düzeltmesi uygulanmış olmalı (hymt-ru 1. tur).
        body = ("\\chapter{A}\n\nГазы ускоряются в горловине сопла, и "
                "давление на выходе падает до атмосферного.\n")
        lt, _srv = ollama_translator(
            "ru", reply=lambda t: "Gazlar nozül boğazında hızlanır ve "
                                  "çıkış basıncı düşer.")
        out = lt.translate(body)
        assert "nozül" not in out and "lüle boğazında" in out


# ===========================================================================
# MT-10 — üst simge, yerel atıf etiketleri, parçalı yolda parantez
# ===========================================================================
DE_BODY = r"""\chapter{Grundlagen}

Hier bezeichnet die Größe den Wert.\textsuperscript{1} Das Minuszeichen drückt aus, dass Wärme stets fließt.

\textsuperscript{1} In der englischsprachigen Literatur wird die Wärmeleitfähigkeit meist mit k bezeichnet.

Wir betrachten eine Wand der Dicke s, deren Oberflächen die konstanten Temperaturen $T_1$ und $T_2$ mit $T_1 > T_2$ besitzen (Abb. 1.1). Integriert man Gl. (1.1) über die Dicke, so folgt der lineare Verlauf aus Tab. 1.1 und Abbildung 1.2 und 1.3.
"""


class TestMT10:
    @pytest.mark.parametrize("src,want", [
        ("Burada değer ifade edilir.^{1} Eksi işareti akışı belirtir.",
         "Burada değer ifade edilir.\\textsuperscript{1} Eksi işareti "
         "akışı belirtir."),
        ("Birim W/m^2 olarak verilir ve sonuç budur.",
         "Birim W/m\\textsuperscript{2} olarak verilir ve sonuç budur."),
    ])
    def test_metin_kipi_ust_simge_emniyeti(self, src, want):
        from latex_pipeline.sanitizer import LatexSanitizer
        assert LatexSanitizer().sanitize(src + "\n")[0] == want + "\n"

    @pytest.mark.parametrize("src", [
        "Denklem $x^{2}$ ve \\(y^{3}\\) ile \\^{o} aksanı korunur.",
        "Adres \\url{http://a.b/x^{1}} korunur.",
        "Açı 20^{\\circ} olarak kalır.",
    ])
    def test_metin_kipi_ust_simge_dokunulmaz(self, src):
        from latex_pipeline.sanitizer import LatexSanitizer
        assert LatexSanitizer().sanitize(src + "\n")[0] == src + "\n"

    def test_metin_kipi_ust_simge_manuel_ceviride_yok(self):
        # "PDF'den TeX çıkar" (manuel çeviri) yolu değişmez.
        from latex_pipeline.sanitizer import LatexSanitizer
        src = "Burada değer ifade edilir.^{1} Eksi işareti akışı belirtir.\n"
        out = LatexSanitizer(manual_translation=True).sanitize(src)[0]
        assert out == src

    def test_ust_simge_mtye_ham_gitmez(self, ollama_translator):
        lt, srv = ollama_translator("de")
        out = lt.translate(DE_BODY)
        assert not any("textsuperscript" in t for t in _src_texts(srv))
        assert out.count("\\textsuperscript{1}") == 2
        assert "^{1}" not in out

    def test_almanca_atif_korunur(self, ollama_translator):
        lt, srv = ollama_translator("de")
        out = lt.translate(DE_BODY)
        sent = " ".join(_src_texts(srv))
        for raw in ("Abb. 1.1", "Gl. (1.1)", "Tab. 1.1", "Abbildung 1.2"):
            assert raw not in sent, raw
        assert "(\u015eek.\u00a01.1)" in out
        assert "Denk.\u00a0(1.1)" in out
        assert "Tablo\u00a01.1" in out
        assert "\u015eekil\u00a01.2 ve 1.3" in out

    def test_ingilizce_kaynakta_yerel_atif_adimi_yok(self, ollama_translator):
        lt, srv = ollama_translator("en")
        lt.translate("\\chapter{A}\n\nThe wall is shown in the sketch Abb. 1.1 of "
                     "the German edition of this old book.\n")
        assert any("Abb. 1.1" in t for t in _src_texts(srv))

    def test_parcali_yolda_kenar_parantezi_korunur(self, monkeypatch):
        from latex_pipeline.translator import LatexTranslator
        lt = LatexTranslator.__new__(LatexTranslator)
        # Model parça kenarındaki parantezi düşürür (İngilizce model testi).
        monkeypatch.setattr(lt, "_safe_translate",
                            lambda t, timeout=None: fake_turkish(t).strip(" ()"),
                            raising=False)
        line = ("As inputs we take the gravity constant of the star "
                "( <<LATEX_0>> ) and the average orbit radius ( <<LATEX_1>> ).")
        out = lt._translate_line_pieces(line)
        assert out.count("(") == 2 and out.count(")") == 2
        assert "( <<LATEX_0>> )" in out and "( <<LATEX_1>> )" in out

    def test_donusturucu_dipnotu_ust_simge_birakmaz(self):
        # Almanca sınama metninin Marker satırı: '$<sup>^1</sup>$ In der …'
        from latex_pipeline.config import PipelineConfig
        from latex_pipeline.converter import MarkdownToLatex
        md = ("# K\n\nDarin ist dT/dx der Temperaturgradient.<sup>1</sup> Das "
              "Minuszeichen drückt aus.\n\n $<sup>^1</sup>$ In der "
              "englischsprachigen Literatur wird k verwendet.\n\nWeiter.\n")
        tex = MarkdownToLatex(PipelineConfig(document_class="book")).convert(md)
        # KARAR 31 (FX1): eşlenen dipnot kaynak numarasıyla.
        assert ("Temperaturgradient.\\footnote[1]{In der englischsprachigen "
                "Literatur wird k verwendet.}") in tex
        assert "^{1}" not in tex and "<sup>" not in tex


# ===========================================================================
# MT-5 / MT-6 — bilinçli dönüşümler, içerik korunur
# ===========================================================================
class TestMT5MT6Bilincli:
    def test_uzun_denklem_bolunur_icerik_ayni(self):
        from latex_pipeline.sanitizer import LatexSanitizer
        # G3a-3 (RAPOR MT-6): eşik artık görünür genişlik. Model testindeki denklem
        # (kaynak 228 karakter, basılı 20,5 em) tek satıra sığar, bölünmez;
        # bölünen örnek gerçekten satırdan geniş (AI-ML body.tex, 45 em).
        # İki denklem de sentetik (KARAR 26), ölçüleri özgünlere yakın:
        # 227 karakter / ~20 em ve 100 karakter / ~49 em (eşik ~41 em).
        fits = (r"\frac{\mathrm{d}}{\mathrm{d}t} \left( m \frac{\dot{\vec{x}} "
                r"\cdot \dot{\vec{x}}}{2} + k \frac{\vec{x} \cdot \vec{x}}{2} \right) = "
                r"\left( m \ddot{\vec{x}} + k \vec{x} \right) \cdot \dot{\vec{x}} = "
                r"\vec{0} \cdot \dot{\vec{x}} = 0")
        out = LatexSanitizer().sanitize("Metin.\n\n\\[\n" + fits + "\n\\]\n")[0]
        assert "multline" not in out and fits in out
        eq = (r"E(churn \mid Usage = 4.0, months = 20, discount = 0.5) = "
              r"-0.20 + 0.004*4 + 0.015*20-0.03*0.5 = 0.10.")
        out = LatexSanitizer().sanitize("Metin.\n\n\\[\n" + eq + "\n\\]\n")[0]
        m = re.search(r"\\begin\{multline\*\}(.*?)\\end\{multline\*\}", out, re.S)
        assert m, out
        norm = lambda s: re.sub(r"\s+|\\\\|&", "", s)  # noqa: E731
        assert norm(m.group(1)) == norm(eq)

    def test_bolum_numarasi_latex_sayacina_birakilir(self):
        from latex_pipeline.sanitizer import LatexSanitizer
        out = LatexSanitizer().sanitize(
            "\\chapter{A}\n\n\\section{1.1 Gök Mekaniğinin Kökenleri}\n\nMetin.\n")[0]
        assert "\\section{Gök Mekaniğinin Kökenleri}" in out  # numaralı \section
