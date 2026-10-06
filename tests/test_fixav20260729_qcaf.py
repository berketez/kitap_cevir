"""Regresyon testleri: 2026-07-29 bug avi, qc_engine + autofix kumesi.

Kapsanan bulgular:
    F25  broken_inline_math Strateji 1 cumle sonu noktasini siliyor ve
         Turkce kelimeyi math moduna cekiyordu
    F26  AutoFixer.run gereksiz tam QC analizi (F17 ile cozuldu; burada
         NO-OP kilidi + yapisal fix sonrasi artik analiz kapatildi)
    F27  _check_prompt_leakage O(P x L): literal desenler her analyze'da
         yeniden derleniyor ve satir satir taraniyordu
    F35  manuel mod atlama listesi eksik: hallucination /
         foreign_language_leakage / number_hallucination
    F38  _REPETITION_LOOP_RE geri-referansli tembel regex superlineer
    F39  _brace_delta saf Python karakter dongusu
    F42  html_artifact check'inde tek satirlik verbatim _in_verbatim'i
         sonsuza kadar acik birakiyordu
"""

import random
import re
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PROJECT_ROOT = Path(__file__).resolve().parent.parent

from latex_pipeline.qc_engine import (  # noqa: E402
    QCEngine,
    QCIssue,
    QCReport,
    _fold_ci,
    may_have_repetition_loop,
)
from latex_pipeline.autofix import AutoFixer, _find_math_spans  # noqa: E402


@pytest.fixture
def engine():
    return QCEngine()


@pytest.fixture
def fixer():
    return AutoFixer(qc_engine=QCEngine())


def _fix_math(fixer, line):
    issues = [QCIssue("broken_inline_math", "high", 1, "m", True, 0.9, "", "")]
    out, fixes = fixer._fix_broken_inline_math(
        line, issues, _find_math_spans(line),
    )
    return out, [f.method for f in fixes]


# ======================================================================
# F25 -- standalone script attach noktalamayi ve kelimeyi korur
# ======================================================================

class TestF25StandaloneScriptAttach:

    def test_cumle_sonu_noktasi_silinmez(self, fixer):
        line = "Bu deney onemli bir deger. $^{3}$ Baska bir cumle burada."
        out, _ = _fix_math(fixer, line)
        assert out == line
        assert "deger." in out
        assert "$deger" not in out

    def test_turkce_kelime_math_moduna_cekilmez(self, fixer):
        line = "Bu sonuc $^{2}$ seklinde gosterilir."
        out, _ = _fix_math(fixer, line)
        assert out == line

    def test_kisa_islev_sozcugu_taban_sayilmaz(self, fixer):
        # "Bir" 3 harflik ASCII ama degisken adi degil (F25 stopword kolu)
        line = "Bir ${}^{14}$C testi yapildi."
        out, methods = _fix_math(fixer, line)
        assert out == "Bir $^{14}$C testi yapildi."
        assert "standalone_script_attach" not in methods

    @pytest.mark.parametrize("line,expected", [
        ("Alan R $^{2}$ olarak verilir.", "Alan $R^{2}$ olarak verilir."),
        ("Molekul CO$_{2}$ olarak yazilir.", "Molekul $CO_{2}$ olarak yazilir."),
        ("Deger x $_{i}$ indisiyle verilir.", "Deger $x_{i}$ indisiyle verilir."),
    ])
    def test_gercek_pozitifler_korunur(self, fixer, line, expected):
        out, methods = _fix_math(fixer, line)
        assert out == expected
        assert "standalone_script_attach" in methods

    def test_sayi_tabanli_birlesme_bozulmadi(self, fixer):
        out, methods = _fix_math(fixer, "Hiz 5 $^{2}$ olarak olculdu.")
        assert out == "Hiz $5^{2}$ olarak olculdu."
        assert "num_script_attach" in methods

    def test_desen_nokta_ayraci_kabul_etmez(self):
        rx = AutoFixer._SCRIPT_ATTACH_RE
        assert rx.search("deger. $^{3}$") is None
        assert rx.search("R $^{2}$") is not None
        # 4+ harflik taban (Turkce kelime riski) eslesmemeli
        assert rx.search("sonuc $^{2}$") is None


# ======================================================================
# F26 -- gereksiz tam QC analizi (F17 ile cozuldu; NO-OP kilidi)
# ======================================================================

class _CountingEngine(QCEngine):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.analyze_calls = 0

    def analyze(self, *args, **kwargs):
        self.analyze_calls += 1
        return super().analyze(*args, **kwargs)


class TestF26AnalyzeCount:

    def test_iterasyon_basina_tek_analiz(self):
        """Dongubasi + fix sonrasi CIFT analiz kalmadi (F17 tasimasi)."""
        eng = _CountingEngine()
        fx = AutoFixer(qc_engine=eng, max_iterations=5)
        body = (
            "Bu \\textbfSpektrum analizi yapildi.\n\n"
            "Sadece ceviriyi ver\n\nDevam metni burada duruyor.\n"
        )
        fixed, report = fx.run(body, "")
        # initial (1) + fix uygulayan her iterasyon icin 1 post analizi.
        # Yapisal fix govdeyi degistirdiyse +1.
        upper = 1 + len(report.fixes_per_iteration) + 1
        assert eng.analyze_calls <= upper
        assert eng.analyze_calls >= 1

    def test_yapisal_fix_govdeyi_degistirmezse_ek_analiz_yok(self):
        """F26 artigi: struct fix raporlanip metin ayni kalirsa analiz yok."""
        eng = _CountingEngine()
        fx = AutoFixer(qc_engine=eng, max_iterations=1)
        body = "Bu tamamen temiz bir Türkçe paragraftır.\n"

        def _noop_struct(text):
            from latex_pipeline.autofix import FixResult
            return text, [FixResult("dummy", 1, "a", "a", "noop")]

        fx._apply_structural_fixes = _noop_struct
        fixed, report = fx.run(body, "")
        assert eng.analyze_calls == 1
        assert fixed == body


# ======================================================================
# F27 -- prompt leakage on-elemesi
# ======================================================================

def _leak_lines(engine, body, manual=False):
    engine._manual_translation = manual
    rep = QCReport()
    engine._check_prompt_leakage(body, body.split("\n"), rep)
    return [(i.line_num, i.message) for i in rep.issues]


class TestF27PromptLeakPrefilter:

    def test_literal_desenler_bir_kez_derlenir(self, engine):
        assert len(engine._prompt_literal_res) == len(engine._prompt_patterns)
        before = [t[2] for t in engine._prompt_literal_res]
        engine.analyze("Bu bir deneme metnidir ve yeterince uzundur.", "")
        after = [t[2] for t in engine._prompt_literal_res]
        # Ayni derlenmis nesneler -- analyze yeniden compile etmiyor
        assert all(a is b for a, b in zip(before, after))

    @pytest.mark.parametrize("text", [
        "You are a professional translator here.",
        "Lutfen cevirin",
        "LÜTFEN ÇEVİRİN",
        "Tüm matematiksel notasyonları koru",
        "TÜM MATEMATİKSEL NOTASYONLARI KORU",
        "Sadece çeviriyi döndür",
        "İngilizce'den Türkçe'ye",
        "ingilizce'den turkce'ye",
        "Keep all LaTeX commands",
        "<<ÇEVİRİYE BAŞLA>>",
    ])
    def test_leak_hala_yakalaniyor(self, engine, text):
        assert _leak_lines(engine, "Onceki satir.\n" + text + "\nSonraki.\n")

    def test_temiz_govde_bulgu_uretmez(self, engine):
        body = "Bu tamamen normal bir Turkce paragraftir.\nIkinci satir.\n"
        assert _leak_lines(engine, body) == []

    def test_manuel_modda_cevirmen_notu_atlanir(self, engine):
        body = "Çevirmen notu: kaynakta yoktu\n"
        assert _leak_lines(engine, body, manual=False)
        assert _leak_lines(engine, body, manual=True) == []

    def test_yorum_satiri_atlanir(self, engine):
        body = "% Lütfen çevirin\n"
        assert _leak_lines(engine, body) == []

    def test_ayni_satirda_iki_desen_iki_bulgu(self, engine):
        body = "Lütfen çevirin ve Sadece çeviriyi ver\n"
        found = _leak_lines(engine, body)
        assert len(found) >= 2

    def test_desen_listesi_calisma_aninda_degisirse_indeks_tazelenir(self):
        eng = QCEngine()
        eng._prompt_patterns.append("Zurna Kalibi Kalip")
        body = "Bu satirda Zurna Kalibi Kalip geciyor.\n"
        assert _leak_lines(eng, body)

    def test_fold_ci_ignorecase_denkligini_kapsar(self, engine):
        """Katlama, re.IGNORECASE'ten DAR olamaz (yoksa leak kacar)."""
        chars = {c for p in engine._prompt_patterns for c in p}
        extra = [0x131, 0x130, 0x17F, 0x212A, 0x212B, 0x1E9E, 0x49, 0x69]
        for c in sorted(chars):
            rx = re.compile(re.escape(c), re.IGNORECASE)
            candidates = list(range(0x20, 0x600)) + extra
            for cp in candidates:
                d = chr(cp)
                if rx.fullmatch(d):
                    assert _fold_ci(c) == _fold_ci(d), (
                        "katlama daralmis: %r ~ %r" % (c, d)
                    )


# ======================================================================
# F35 -- manuel mod atlama listeleri
# ======================================================================

def _autofix_skip_set():
    src = (PROJECT_ROOT / "latex_pipeline" / "autofix.py").read_text(
        encoding="utf-8")
    m = re.search(r"_MANUAL_TRANSLATION_SKIP = \{(.*?)\n        \}", src,
                  re.DOTALL)
    assert m, "_MANUAL_TRANSLATION_SKIP bulunamadi"
    return set(re.findall(r'"([a-z_]+)"', m.group(1)))


class TestF35ManualSkipCompleteness:

    @pytest.mark.parametrize("name", [
        "hallucination", "foreign_language_leakage", "number_hallucination",
    ])
    def test_qc_mt_feedback_setinde(self, name):
        assert name in QCEngine._MT_FEEDBACK_CHECKS

    @pytest.mark.parametrize("name", [
        "hallucination", "foreign_language_leakage", "number_hallucination",
    ])
    def test_autofix_skip_listesinde(self, name):
        assert name in _autofix_skip_set()

    def test_iki_liste_ayni(self):
        """A-17: listeler ayrisirsa sessiz delik acilir."""
        assert _autofix_skip_set() == set(QCEngine._MT_FEEDBACK_CHECKS)

    def test_manuel_analyze_rapor_only(self, engine):
        tr = ("Bu cumle tekrar eden bir yapidir bu cumle tekrar eden bir "
              "yapidir bu cumle tekrar eden bir yapidir ve devam eder.\n")
        en = "This sentence is a repeated structure and it goes on.\n"
        rep_b = engine.analyze(tr, en, manual_translation=False)
        rep_a = engine.analyze(tr, en, manual_translation=True)
        assert any(i.check_name == "hallucination" and i.auto_fixable
                   for i in rep_b.issues)
        assert all(not i.auto_fixable for i in rep_a.issues
                   if i.check_name == "hallucination")

    def test_manuel_modda_tekrar_budanmaz(self):
        body = ("Bu cumle tekrar eden bir yapidir bu cumle tekrar eden bir "
                "yapidir bu cumle tekrar eden bir yapidir ve devam eder.\n")
        issues = [QCIssue("hallucination", "critical", 1,
                          "MT repetition loop detected", True, 0.95, "", "")]
        yol_b, fixes_b = AutoFixer(
            qc_engine=QCEngine(), manual_translation=False,
        )._apply_fixes(body, issues)
        yol_a, fixes_a = AutoFixer(
            qc_engine=QCEngine(), manual_translation=True,
        )._apply_fixes(body, issues)
        assert yol_b != body and fixes_b
        assert yol_a == body and not fixes_a

    def test_manuel_modda_cjk_alintisi_silinmez(self):
        body = "Konfucyus soyle yazar: 學而時習之 boyle devam eder.\n"
        issues = [QCIssue("foreign_language_leakage", "high", 1, "CJK",
                          True, 0.99, "", "")]
        yol_b, fixes_b = AutoFixer(
            qc_engine=QCEngine(), manual_translation=False,
        )._apply_fixes(body, issues)
        yol_a, fixes_a = AutoFixer(
            qc_engine=QCEngine(), manual_translation=True,
        )._apply_fixes(body, issues)
        assert yol_b != body and fixes_b
        assert yol_a == body and not fixes_a


# ======================================================================
# F38 -- tekrar-dongusu on-elemesi
# ======================================================================

_LOOP_RE = QCEngine._REPETITION_LOOP_RE


class TestF38RepetitionPrefilter:

    @pytest.mark.parametrize("text", [
        "bu cumle bes kelimeden olusur bu cumle bes kelimeden olusur "
        "bu cumle bes kelimeden olusur",
        "a b c d e f a b c d e f a b c d e f",
        "onsoz filan bir iki uc dort bes alti bir iki uc dort bes alti "
        "bir iki uc dort bes alti son",
    ])
    def test_gercek_dongu_hala_yakalaniyor(self, text):
        assert may_have_repetition_loop(text)
        assert _LOOP_RE.search(text)

    @pytest.mark.parametrize("text", [
        "Bu normal bir Turkce cumledir ve tekrar icermez.",
        "kisa metin",
        "bir iki uc bir iki uc bir iki uc",           # fraz 3 kelime (<5)
        "a b c d e f a b c d e f",                     # yalniz 2 kopya
    ])
    def test_eslesmeyen_metinde_on_eleme_ve_regex_ayni(self, text):
        assert not _LOOP_RE.search(text)
        # on-eleme ya eler ya da regex'e devreder; her iki durumda da
        # regex sonucu degismez
        if may_have_repetition_loop(text):
            assert not _LOOP_RE.search(text)

    def test_on_eleme_ust_kume_rastgele_korpus(self):
        rng = random.Random(1907)
        voc = ("bir iki uc dort bes alti yedi sekiz dokuz on ve ile de da "
               "veri model katman a b c").split()
        punct = ["", "", "", ".", ",", ";", ")", "-", "\\textbf{", "}"]
        misses = 0
        for _ in range(4000):
            parts = [rng.choice(voc) + rng.choice(punct)
                     for _ in range(rng.randint(0, 25))]
            s = " ".join(parts)
            if rng.random() < 0.5:
                phrase = " ".join(rng.choice(voc) for _ in range(
                    rng.randint(2, 8)))
                sep = rng.choice([" ", " ", ". ", ", "])
                s += " " + sep.join([phrase] * rng.randint(2, 4))
            if _LOOP_RE.search(s) and not may_have_repetition_loop(s):
                misses += 1
        assert misses == 0

    def test_uzun_eslesmeyen_satir_hizli(self):
        rng = random.Random(5)
        voc = "bir iki uc dort bes alti yedi sekiz dokuz on veri model".split()
        line = " ".join(rng.choice(voc) for _ in range(600))
        assert not _LOOP_RE.search(line)
        body = "\n".join([line] * 20)
        eng = QCEngine()
        rep = QCReport()
        t0 = time.perf_counter()
        eng._check_hallucination(body, "", body.split("\n"), [], rep)
        elapsed = time.perf_counter() - t0
        # Eski geri-referansli regex: satir basina ~27 ms -> 20 satir ~0.55 s
        assert elapsed < 0.15, "29b tekrar taramasi yavas: %.3f s" % elapsed

    def test_autofix_tekrar_budama_calisiyor(self):
        body = ("Bu cumle tekrar eden bir yapidir bu cumle tekrar eden bir "
                "yapidir bu cumle tekrar eden bir yapidir ve devam eder.\n")
        issues = [QCIssue("hallucination", "critical", 1,
                          "MT repetition loop detected", True, 0.95, "", "")]
        fx = AutoFixer(qc_engine=QCEngine())
        out, fixes = fx._fix_segment_hallucination(body, issues, [])
        assert fixes and out.count("tekrar eden bir yapidir") == 1

    def test_29b_bulgu_metni_degismedi(self, engine):
        body = ("Bu cumle bes kelimeden olusur bu cumle bes kelimeden olusur "
                "bu cumle bes kelimeden olusur.\n")
        rep = QCReport()
        engine._check_hallucination(body, "", body.split("\n"), [], rep)
        msgs = [i.message for i in rep.issues
                if "repetition loop" in i.message]
        assert msgs and "Bu cumle bes kelimeden olusur" in msgs[0]


# ======================================================================
# F39 -- _brace_delta
# ======================================================================

def _brace_delta_reference(text):
    """Eski saf Python uygulamasi (semantik referans)."""
    opens = closes = 0
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text):
            i += 2
            continue
        if text[i] == "{":
            opens += 1
        elif text[i] == "}":
            closes += 1
        i += 1
    return opens - closes


class TestF39BraceDelta:

    @pytest.mark.parametrize("text", [
        "",
        "{",
        "}",
        "\\{",
        "\\}",
        "\\\\{",
        "{a}{b}",
        "\\textbf{x}",
        "son karakter ters bolu \\",
        "satir\\\nsonu {",
        "\\{ \\} { }",
        "%{ yorum }",
    ])
    def test_kacis_semantigi_referansla_ayni(self, text):
        assert AutoFixer._brace_delta(text) == _brace_delta_reference(text)

    def test_rastgele_korpusta_parite(self):
        rng = random.Random(4242)
        alphabet = "{}\\ abc\n$%"
        for _ in range(3000):
            s = "".join(rng.choice(alphabet)
                        for _ in range(rng.randint(0, 40)))
            assert AutoFixer._brace_delta(s) == _brace_delta_reference(s)

    def test_buyuk_govdede_hizli(self):
        big = "a{b}c\\{d\\}" * 100000  # ~1 MB
        t0 = time.perf_counter()
        delta = AutoFixer._brace_delta(big)
        elapsed = time.perf_counter() - t0
        assert delta == 0
        # Eski saf Python dongusu ayni girdide ~46 ms
        assert elapsed < 0.03, "_brace_delta yavas: %.4f s" % elapsed


# ======================================================================
# F42 -- html_artifact verbatim durum makinesi
# ======================================================================

def _html_lines(engine, body):
    engine._in_verbatim = False
    rep = QCReport()
    engine._check_html_artifacts(body, body.split("\n"), rep)
    return [i.line_num for i in rep.issues if i.check_name == "html_artifact"]


class TestF42VerbatimState:

    def test_tek_satirlik_verbatim_bayragi_acik_birakmaz(self, engine):
        body = "\\begin{verbatim}x\\end{verbatim}\n<td><tr><div>\n"
        assert _html_lines(engine, body) == [2]

    def test_tek_satirlik_lstlisting(self, engine):
        body = "\\begin{lstlisting}kod\\end{lstlisting}\n<td><tr><div>\n"
        assert _html_lines(engine, body) == [2]

    def test_cok_satirli_verbatim_ici_hala_muaf(self, engine):
        body = (
            "\\begin{verbatim}\n<td><tr><div>\n\\end{verbatim}\n"
            "<td><tr><div>\n"
        )
        assert _html_lines(engine, body) == [4]

    def test_satir_ici_end_ortami_kapatir(self, engine):
        body = (
            "\\begin{verbatim}\nkod <td><tr><div>\nkod \\end{verbatim}\n"
            "<td><tr><div>\n"
        )
        assert _html_lines(engine, body) == [4]

    def test_verbatimsiz_referans(self, engine):
        assert _html_lines(engine, "<td><tr><div>\n") == [1]

    def test_analyze_arasinda_durum_sizmaz(self, engine):
        engine.analyze("\\begin{verbatim}x\\end{verbatim}\n", "")
        rep = engine.analyze("<td><tr><div>\nBirkac satir daha burada.\n", "")
        assert any(i.check_name == "html_artifact" for i in rep.issues)
