"""Y1 kulvarı düzeltmeleri (29 Eylül 2026): "Yerel model ile çevir" LaTeX zinciri.

Bug avının (2026-09-28) doğrulanmış bulguları için kalıcı
regresyon testleri:

* F3-01 — display matematik desenleri düzyazıyı yutuyordu (bitişik atom
  ``$a$$b$``, Marker atıf kaçışı ``\\[ 63}]``); translator stash'i ve
  retranslator artık TeX okuma sırasıyla okur.
* Vq-1 — ``core.lang_detect`` temizleyicisinde aynı kör nokta.
* F3-02 — iç içe yer tutucu: matematikli ``\\textit`` / ``\\textbf`` /
  ``\\footnote`` hiç çevrilmiyor, iç öğe gövde sonuna kopyalanıyordu.
* F3-03 — kısa ``\\textit{sözcük}`` değişken sanılıp çevrilmiyordu.
* F3-09 — paragrafta kaynak dilde kalan tek cümleyi retranslator görmüyordu.
* F3-10 — tablo hücresinde ``\\midrule`` / ``\\toprule`` MT'ye gidiyordu.
* F3-11 — İngilizce cümle bölücünün kısaltma bakışları etkisizdi.

Girdiler sentetiktir; model yüklenmez, gerçek sunucuya bağlanılmaz (sahte
Ollama: tests/fakes, rastgele port).
"""
from __future__ import annotations

import re
import zlib

import pytest

from latex_pipeline.config import PipelineConfig
from latex_pipeline.translator import LatexTranslator, TranslationStats

# ---------------------------------------------------------------------------
# Sahte çevirmen (yalnız test altyapısı): sözcük başına kararlı Türkçe sözcük,
# yer tutucular ve sayılar AYNEN.
# ---------------------------------------------------------------------------
_TR = ("çalışma", "değer", "ışık", "yörünge", "şekil", "güneş", "ağırlık",
       "kütle", "hız", "yüzey", "dönüş", "öğrenci", "kuvvet", "enerji",
       "sıcaklık", "basınç", "akış", "düzlem", "eğri", "açı")
_PH = re.compile(r"(<<[A-Z]+_\d+>>|\[\[[A-Za-z]+_\d+\]\]|@@[A-Za-z]+\d+@@)")
_LETTERS = re.compile(r"[^\W\d_]+")


def fake_turkish(text: str) -> str:
    def word(m: re.Match) -> str:
        src = m.group(0)
        tr = _TR[zlib.crc32(src.lower().encode("utf-8")) % len(_TR)]
        return tr[:1].upper() + tr[1:] if src[:1].isupper() else tr
    return "".join(p if _PH.fullmatch(p) else _LETTERS.sub(word, p)
                   for p in _PH.split(text))


def _skeleton() -> LatexTranslator:
    """MT'siz iskelet (yalnız stash / restore / regex yardımcıları için)."""
    return LatexTranslator.__new__(LatexTranslator)


@pytest.fixture
def ollama_translator(fake_llm_server, monkeypatch, tmp_path):
    """Sahte Ollama'ya bağlı LatexTranslator fabrikası: ``make(source_lang, reply)``.

    Kaynak adresleri sahte sunucuya yöneltilir (LM Studio kapalı bir porta);
    çeviri belleği geçici dosyada."""
    from core.mt_engines import discovery
    from core.mt_engines.contracts import SourcesConfig
    from core.translator import BookTranslator

    real_init_memory = BookTranslator._init_memory
    memory_db = tmp_path / "bellek.db"

    def _tmp_memory(self):
        self.config.memory_db_path = str(memory_db)
        real_init_memory(self)

    monkeypatch.setattr(BookTranslator, "_init_memory", _tmp_memory)

    def make(source_lang: str = "en", reply=fake_turkish):
        srv = fake_llm_server("ollama", models=["sahte"], reply=reply)
        sources = SourcesConfig(ollama_url=srv.url,
                                lmstudio_url="http://127.0.0.1:9/v1",
                                openai_servers=())
        monkeypatch.setattr(discovery, "load_sources", lambda path=None: sources)
        lt = LatexTranslator(pipeline_config=PipelineConfig(translate=True),
                             mt_engine="ollama:sahte", source_lang=source_lang,
                             target_lang="tr", book_type="science",
                             project_name="Y1 Deneme")
        return lt, srv

    return make


def _chat_texts(srv) -> list:
    return [b["messages"][-1]["content"] for b in srv.requests_to("/api/chat")]


# ===========================================================================
# F3-01 — display matematik desenleri düzyazıyı yutmaz
# ===========================================================================
C05_BODY = r"""\chapter{Combustion}

In this section we work out the flame temperature reached by a $C_{4}$$H_{10}$-air mixture in stoichiometric proportion at 298 K.

The reaction releases a large amount of heat, and the products leave the chamber at a very high speed through the nozzle.

Engineers measure the pressure in the chamber with small sensors that are placed near the injector plate of the engine.

The combustion efficiency depends on the mixing of the fuel and the oxidizer before the gases reach the throat.

$$
c^{*} = \frac{A_t p_c}{\dot m}
$$

The characteristic velocity defined above is a useful measure of the performance of the combustion chamber.
"""
C05_PROSE = ("The reaction releases", "Engineers measure",
             "The combustion efficiency")
C05_DISPLAY = "$$\nc^{*} = \\frac{A_t p_c}{\\dot m}\n$$"


class TestF301DisplayMatematik:
    def test_bitisik_atom_duzyaziyi_yutmaz(self):
        masked, stash = _skeleton()._stash_protected(C05_BODY)
        for prose in C05_PROSE:
            assert prose in masked, prose
            assert not any(prose in item for item in stash), prose
        assert "$C_{4}$" in stash and "$H_{10}$" in stash
        assert C05_DISPLAY in stash

    def test_atif_kacisi_duzyaziyi_yutmaz(self):
        body = ("An early study by Rossi and Kato \\href{#page-157-0}{\\[ 63}] "
                "that introduced a simple learning rule is still cited.\n\n"
                "Later work extended the idea to deeper networks of neurons.\n\n"
                "\\[ x = 1 \\]\n\nThe end of the section follows here.\n")
        masked, stash = _skeleton()._stash_protected(body)
        assert "that introduced a simple learning rule" in masked
        assert "Later work extended the idea" in masked
        assert "\\[ x = 1 \\]" in stash
        assert not any("Later work" in item or "that introduced" in item
                       for item in stash)
        # Kaçıştaki '\[' kaybolmaz: href öğesiyle birlikte aynen korunur.
        restored = _skeleton()._restore_stash(masked, stash)
        assert "\\href{#page-157-0}{\\[ 63}]" in restored

    def test_bitisik_yer_tutucular_arasina_bosluk_girmez(self):
        body = ("The atoms $\\alpha$$\\beta$ and $x$\\footnote{A short note "
                "about the symbol.} appear, the mixture $C_{4}$$H_{10}$-air "
                "burns.\n")
        t = _skeleton()
        masked, stash = t._stash_protected(body)
        assert t._restore_stash(masked, stash) == body

    def test_kaynakta_bitisik_yer_tutucuya_bosluk_eklenmez(self):
        body = ("At equilibrium $H_{2}$O forms, the \\textit{i}th component "
                "and 8\\textit{g} load, AIAA, 1983, Chap. 10, pp. 515.\n")
        t = _skeleton()
        masked, stash = t._stash_protected(body)
        assert t._restore_stash(masked, stash) == body
        # Kaynakta bitişik OLMAYAN yer tutucuya MT'nin yapıştırdığı sözcük
        # yine ayrılır (boşluk garantisi korunur).
        masked2, stash2 = t._stash_protected("Value $x$ here.\n")
        glued = masked2.replace("<<LATEX_0>> here", "<<LATEX_0>>burada")
        assert t._restore_stash(glued, stash2) == "Value $x$ burada.\n"

    def test_esiz_ayrac_yerinde_korunur(self):
        body = "A stray $ sign before the price and the rest of the sentence.\n"
        t = _skeleton()
        masked, stash = t._stash_protected(body)
        assert "$" not in masked and "$" in stash
        assert t._restore_stash(masked, stash) == body

    def test_uctan_uca_paragraflar_modele_gider(self, ollama_translator):
        lt, srv = ollama_translator("en")
        out = lt.translate(C05_BODY)
        sent = "\n".join(_chat_texts(srv))
        for prose in C05_PROSE:
            assert prose in sent, f"modele gitmedi: {prose}"
            assert prose not in out, f"kaynak dilde kaldı: {prose}"
        assert C05_DISPLAY in out
        assert "\\frac{A_t p_c}" not in sent  # display MT'ye metin olarak gitmez
        assert "$C_{4}$$H_{10}$-" in out

    def test_retranslator_bitisik_atom_satirlarini_korumali_saymaz(self):
        from latex_pipeline.retranslator import (_build_protected_line_set,
                                                 scan_and_retranslate)
        text = ("Karışımın alev sıcaklığı $C_{4}$$H_{10}$ için hesaplanır.\n\n"
                "The reaction releases a large amount of heat through the nozzle.\n\n"
                "$$\nx = 1\n$$\n")
        lines = text.split("\n")
        protected = _build_protected_line_set(text, lines)
        assert 2 not in protected  # İngilizce satır matematik sayılmaz
        assert {4, 5, 6} <= protected  # gerçek display korumalı
        res = scan_and_retranslate(text, translator_fn=fake_turkish)
        assert res.english_lines_found == 1
        assert "The reaction releases" not in res.output_tex

    def test_sayfa_atfi_sozcuk_icinde_eslesmez(self):
        from latex_pipeline.translator import _PAGE_REF_RE
        assert _PAGE_REF_RE.findall("AIAA, 1983, Chap. 10, pp. 515–597.") == [
            "pp. 515–597"]
        assert _PAGE_REF_RE.findall("see p. 234") == ["p. 234"]


# ===========================================================================
# Vq-1 — core.lang_detect temizleyicisi
# ===========================================================================
class TestVq1LangDetect:
    SEG = ("Der Wert $\\alpha$${}^{0}$ wird gemessen, und die Ergebnisse werden "
           "mit der Theorie verglichen, weil die Messung sehr genau ist und wir "
           "die Abweichung kennen wollen. Wir sehen, dass die Werte gut "
           "übereinstimmen und dass $$x$$ gilt.")

    def test_bitisik_atom_sonrasi_duzyazi_kalir(self):
        from core import lang_detect as ld
        clean = ld.clean_latex_for_detection(self.SEG)
        assert "Theorie verglichen" in clean
        assert "\\alpha" not in clean and "x$$" not in clean
        assert ld.classify_foreign_segment(self.SEG, "en") == "de"

    def test_atif_kacisi_sonrasi_duzyazi_kalir(self):
        from core import lang_detect as ld
        seg = ("Siehe \\href{#page-1-0}{\\[ 63}] und die Werte werden mit der "
               "Theorie verglichen, weil die Messung sehr genau ist und wir die "
               "Abweichung kennen wollen. \\[ x = 1 \\]")
        clean = ld.clean_latex_for_detection(seg)
        assert "Theorie verglichen" in clean and "x = 1" not in clean
        assert ld.classify_foreign_segment(seg, "en") == "de"

    def test_satir_sayisi_korunur(self):
        from core import lang_detect as ld
        text = "a $x\ny$ b\n\n$$\nq\n$$\nson"
        assert ld.clean_latex_for_detection(text).count("\n") == text.count("\n")

    def test_bracket_okuyucu(self):
        from core.lang_detect import bracket_display_spans
        text = "a \\href{#p}{\\[ 63}] b \\[ x \\] c \\\\[2pt] d \\[ y\n\nz \\]"
        spans = [text[s:e] for s, e in bracket_display_spans(text)]
        assert spans == ["\\[ x \\]"]

    def test_cift_ters_bolu_dolar_duz_dolardir(self):
        from core.lang_detect import dollar_math_spans
        text = "cost \\\\$1.10. If the bat costs \\\\$4 and $x$ holds."
        assert [text[s:e] for s, e, _d in dollar_math_spans(text)] == ["$x$"]
        masked, stash = _skeleton()._stash_protected(text)
        assert "If the bat costs" in masked and stash == ["$x$"]


# ===========================================================================
# F3-02 — iç içe yer tutucu
# ===========================================================================
C15_BODY = r"""\chapter{Aerodynamics}

The lift coefficient grows linearly with the angle of attack\footnote{See Section~\ref{sec:thin} and the relation $C_L = 2\pi\alpha$ for thin profiles.} for small angles.

The drag coefficient \textbf{$C_D$ at zero lift} is small for a thin symmetric profile in a uniform stream of air.

The maximum lift \textit{$C_{L,max}$} is reached near twelve degrees, and the flow separates beyond that angle.
"""


class TestF302IcIceYerTutucu:
    def test_dogrulama_ic_ice_kumeyi_kabul_eder(self):
        v = LatexTranslator._validate_stash_item
        orig = "\\textbf{<<LATEX_1>> at zero lift}"
        assert v(orig, "\\textbf{<<LATEX_1>> sıfır kaldırmada}")
        assert v(orig, "\\textbf{sıfır kaldırmada <<LATEX_1>>}")
        assert not v(orig, "\\textbf{sıfır kaldırmada}")            # eksik
        assert not v(orig, "\\textbf{<<LATEX_1>> <<LATEX_1>> x}")   # kopya
        assert not v(orig, "\\textbf{<<LATEX_2>> sıfır}")           # yabancı
        assert not v(orig, "\\textbf{<< LATEX_1 >> sıfır}")         # bozuk
        assert not v(orig, "\\textbf{<<LATEX_1>> <<REF_0>> sıfır}")  # yetim REF

    def test_ulasilabilir_kimlikler(self):
        stash = ["$a$", "$b$", "\\textit{<<LATEX_0>> x}",
                 "\\footnote{<<LATEX_2>> ve <<LATEX_1>>}", "$c$"]
        reach = LatexTranslator._reachable_stash_ids
        assert reach({3, 4}, stash) == {0, 1, 2, 3, 4}
        # Kaybolan kök öğenin içindekiler kökle birlikte açılır.
        assert reach(set(), stash, roots={2, 3}) == {0, 1, 2}

    def test_uctan_uca_tek_kopya_ve_ceviri(self, ollama_translator):
        lt, _srv = ollama_translator("en")
        out = lt.translate(C15_BODY)
        assert "RECOVERED from stash" not in out
        for math in ("$C_L = 2\\pi\\alpha$", "$C_D$", "$C_{L,max}$"):
            assert out.count(math) == 1, math
        # Dipnot ve kalın vurgunun düzyazısı çevrildi (kaynak dilde kalmadı).
        assert "for thin profiles" not in out
        assert "at zero lift" not in out
        assert "\\ref{sec:thin}" in out


# ===========================================================================
# F3-03 — kısa italik: değişken mi, vurgulu sözcük mü?
# ===========================================================================
C19_BODY = r"""\chapter{Faith}
The prophet did \textit{not} allow the people to leave the city before the long winter had ended completely.
She said that the old law was \textit{always} more important than the new customs of the village people.
God will \textbf{\textit{allow}} every believer to return home after the journey through the dark valley.
The variable \textit{x} denotes the position of the particle along the straight line of the motion.
"""


class TestF303KisaItalik:
    @pytest.mark.parametrize("word", [
        "x", "Re", "Nu", "f(x)", "x_1", "a2", "DNA", "LaTeX", "pH", "sin",
        "max", "dx", "rms", "α", "e.g.",
    ])
    def test_sembol(self, word):
        from latex_pipeline.translator import _short_italic_is_symbol
        assert _short_italic_is_symbol(word, "en")

    @pytest.mark.parametrize("word,lang", [
        ("not", "en"), ("always", "en"), ("God", "en"), ("should", "en"),
        ("is", "en"), ("no", "en"), ("Height", "en"), ("nicht", "de"),
        ("pas", "fr"), ("не", "ru"), ("всегда", "ru"),
    ])
    def test_gercek_sozcuk(self, word, lang):
        from latex_pipeline.translator import _short_italic_is_symbol
        assert not _short_italic_is_symbol(word, lang)

    def test_sozcuk_cumlesiyle_mtye_gider(self):
        masked, stash = _skeleton()._stash_protected(
            "The prophet did \\textit{not} allow it, and \\textit{x} moves.")
        assert "did <<LATEX_0>>not<<LATEX_1>> allow" in masked
        assert stash[:2] == ["\\textit{", "}"]
        assert "\\textit{x}" in stash

    def test_retranslator_kisa_italik_sozcugu_cevirir(self):
        from latex_pipeline.retranslator import (_retranslate_heading_line,
                                                 _retranslate_line)
        out = _retranslate_line("The prophet did \\textit{not} allow the people "
                                "to leave, and \\textit{x} moves.", fake_turkish)
        assert "\\textit{not}" not in out
        assert re.search(r"\\textit\{[^\s{}]+\}", out)
        assert "\\textit{x}" in out
        head = _retranslate_heading_line("\\section{Why \\textit{not} Leave}",
                                         fake_turkish)
        assert "\\textit{not}" not in head and "\\textit{" in head

    def test_kabuk_geri_yaziminda_bosluk_girmez(self):
        t = _skeleton()
        stash = ["\\textit{", "}"]
        assert (t._restore_stash("izin <<LATEX_0>>vermedi<<LATEX_1>>ler.", stash)
                == "izin \\textit{vermedi}ler.")
        assert (t._restore_stash("bu<<LATEX_0>>değil<<LATEX_1>> ki", stash)
                == "bu \\textit{değil} ki")

    def test_kayip_kabuk_govde_sonuna_eklenmez(self, ollama_translator,
                                               monkeypatch):
        lt, _srv = ollama_translator("en")
        body = "The prophet did \\textit{not} allow the people to leave.\n"
        # MT'nin kapanan kabuğu yuttuğu durum: yer tutucu gövdeden düşer.
        monkeypatch.setattr(lt, "_translate_paragraphs",
                            lambda b: b.replace("<<LATEX_1>>", ""))
        out = lt.translate(body)
        assert "RECOVERED" not in out
        assert not out.rstrip().endswith("}")
        # Denetim: koruyan şey süslü parantez dengesi kapısıdır — kapı
        # kapatılınca yarım kabuk gövde sonuna eklenir (belge derlenmezdi).
        monkeypatch.setattr(LatexTranslator, "_braces_balanced",
                            staticmethod(lambda text: True))
        out2 = lt.translate(body)
        assert "% [RECOVERED from stash 1]\n}" in out2

    def test_kisa_kalin_tek_sozcuk_cevrilir(self, ollama_translator):
        lt, _srv = ollama_translator("en")
        out = lt.translate("\\textbf{Solution} The answer follows from the "
                           "equation of the orbit given above, and \\textbf{DNA} "
                           "stays as it is in the text.\n")
        assert "\\textbf{Solution}" not in out
        assert "\\textbf{DNA}" in out

    def test_uctan_uca(self, ollama_translator):
        lt, srv = ollama_translator("en")
        out = lt.translate(C19_BODY)
        sent = "\n".join(_chat_texts(srv))
        assert re.search(r"\bnot\b", sent) and re.search(r"\balways\b", sent)
        assert "\\textit{not}" not in out and "\\textit{always}" not in out
        assert "\\textit{allow}" not in out
        assert "\\textit{x}" in out
        assert "RECOVERED" not in out
        assert out.count("\\textit{") == 4


# ===========================================================================
# F3-09 — paragrafta kaynak dilde kalan tek cümle
# ===========================================================================
class TestF309CumleDuzeyi:
    FR_SENT = ("Une planète se déplace sur une trajectoire elliptique autour de "
               "l'étoile et sa vitesse dépend de la distance au foyer.")
    TR_REST = ("Toplam enerji yalnızca yörüngenin büyüklüğüne bağlıdır ve bu "
               "sonuç öğrencileri şaşırtır. Enerji negatif olduğunda cisim "
               "yıldıza bağlı kalır.")

    def test_fransizca_cumle_yeniden_cevrilir(self):
        from latex_pipeline.retranslator import scan_and_retranslate
        line = self.FR_SENT + " " + self.TR_REST
        res = scan_and_retranslate("\\section{Yörünge}\n\n" + line + "\n",
                                   translator_fn=fake_turkish, source_lang="fr")
        assert res.english_lines_found == 1 and res.retranslated_lines == 1
        assert "Une planète" not in res.output_tex
        assert self.TR_REST in res.output_tex  # Türkçe kısım aynen

    def test_ingilizce_cumle_yeniden_cevrilir(self):
        from latex_pipeline.retranslator import scan_and_retranslate
        en = ("A planet moves along an elliptical path around the star and its "
              "speed changes with the distance from the focus.")
        res = scan_and_retranslate(en + " " + self.TR_REST + "\n",
                                   translator_fn=fake_turkish)
        assert res.retranslated_lines == 1
        assert "A planet moves" not in res.output_tex
        assert self.TR_REST in res.output_tex

    def test_atif_etiketi_karari_bozmaz(self):
        from latex_pipeline.retranslator import scan_and_retranslate
        line = ("Un profil est la section d'une aile par un plan parallèle à "
                "l'écoulement (Şek.~1.1). " + self.TR_REST)
        res = scan_and_retranslate(line + "\n", translator_fn=fake_turkish,
                                   source_lang="fr")
        assert res.retranslated_lines == 1
        assert "Un profil est" not in res.output_tex

    def test_turkce_satira_dokunulmaz(self):
        from latex_pipeline.retranslator import scan_and_retranslate
        calls = []

        def tr(text):
            calls.append(text)
            return text
        res = scan_and_retranslate(self.TR_REST + "\n", translator_fn=tr,
                                   source_lang="fr")
        assert calls == [] and res.english_lines_found == 0

    def test_suslu_parantez_ve_matematik_icinde_bolunmez(self):
        from latex_pipeline.retranslator import _line_sentence_spans
        line = ("Kısa \\footnote{A. B. Ceviz dedi. Sonra gitti.} metin $x. Y$ "
                "burada. İkinci cümle.")
        parts = [line[s:e] for s, e in _line_sentence_spans(line, "en")]
        assert parts == [line[:line.index(" İkinci")], "İkinci cümle."]


# ===========================================================================
# F3-10 — tablo hücresindeki kural komutları
# ===========================================================================
TAB_BODY = ("\n\\toprule\nChapter 1 & Lift and drag of an airfoil section & 2 \\\\\n"
            "\\midrule\n1.1 & Aerodynamic forces and their coefficients & 2 \\\\\n"
            "\\cmidrule(lr){1-2}\n1.2 & The geometry of the wing profile & 2 \\\\\n"
            "\\bottomrule\n")


class TestF310TabloKurali:
    @pytest.mark.parametrize("lang", ["en", "fr"])
    def test_kural_komutu_mtye_gitmez(self, make_latex_translator, monkeypatch,
                                      lang):
        lt = make_latex_translator()
        lt.source_lang = lang
        seen = []

        def cell(text):
            seen.append(text)
            return fake_turkish(text)
        monkeypatch.setattr(lt, "_translate_cell", cell)
        out = lt._translate_tabular_body(TAB_BODY)
        assert seen, "hiç hücre çevrilmedi"
        assert not any("rule" in s for s in seen), seen
        for cmd in ("\\toprule\n", "\\midrule\n1.1", "\\cmidrule(lr){1-2}\n1.2",
                    "\\bottomrule\n"):
            assert cmd in out, cmd

    def test_bol_ve_birlestir(self):
        split = LatexTranslator._split_cell_rules
        assert split("\n\\midrule\n1.1 ") == ("\n\\midrule\n", "1.1 ", "")
        assert split("Metin \\hline") == ("", "Metin", " \\hline")
        assert split(" düz ") == ("", " düz ", "")


# ===========================================================================
# F3-11 — İngilizce cümle bölücü kısaltmaları
# ===========================================================================
class TestF311CumleBolucu:
    @pytest.mark.parametrize("text", [
        "This was proven by Dr. Smith in 1950.",
        "See Eq. B for details.",
        "This holds, e.g. Newton said so.",
        "As shown in Fig. A the path is round.",
        "Prof. Jones later confirmed it.",
        "Bu vb. Şeyler oldu.",
    ])
    def test_kisaltmada_bolmez(self, text):
        assert LatexTranslator._SENTENCE_SPLIT_RE.split(text) == [text]

    def test_cumle_sonunda_boler(self):
        assert LatexTranslator._SENTENCE_SPLIT_RE.split(
            "It ends here. Next one starts. Last!") == [
            "It ends here.", "Next one starts.", "Last!"]

    def test_uzun_paragraf_istekleri(self, ollama_translator):
        lt, srv = ollama_translator("en")
        lt.stats = TranslationStats()
        filler = ("The orbit of the satellite changes slowly because of the "
                  "small drag force of the upper atmosphere. ") * 11
        para = ("The first measurements were made by Dr. Smith at the northern "
                "station during the long winter. " + filler
                + "As shown in Fig. A the path of the satellite is almost a "
                "perfect circle at this height. " + filler
                + "Prof. Jones later confirmed these values with a new "
                "instrument of much higher precision.")
        assert len(para) > 2000
        lt._safe_translate(para)
        texts = _chat_texts(srv)
        assert texts
        for t in texts:
            body = t.split("\n\n", 1)[-1].strip()
            assert not re.search(r"\b(?:Dr|Fig|Prof)\.$", body), body[-60:]
            assert not re.match(r"(?:Smith|A the|Jones)\b", body), body[:60]
