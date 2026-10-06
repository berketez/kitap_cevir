"""Regresyon testleri: 2026-07-28 bug avi, core kume (qc_engine + autofix).

Kapsanan bulgular:
    F1     broken_command_braces: \\footnotesize/\\captionsetup/\\captionof
           sahte pozitifleri (lookahead harf dislamiyordu)
    F2     translated_env_name: kullanici tanimli ortam adlari yeniden
           adlandirilip derlemeyi kiriyordu (A-9 guard'inin QC/autofix esi)
    F4     _fix_prompt_leakage artik temizligi verbatim icini eziyordu
    F5     _fix_stash_placeholder_orphan '  +' collapse'i kod girintisini
           yok ediyordu
    F7     cok desenli donglerde bayat math_spans -- math ici yeniden
           yazilabiliyordu
    F8     hallucination 29a pozisyonel paragraf kiyasi: tek birlesme
           zincirleme sahte critical uretiyordu
    F9/F15 _check_glossary_compliance / _check_wrong_translation kelime
           indeksi on-filtresi (davranis paritesi testleri)
    F17    AutoFixer.run ayni govdeyi art arda analiz etmesin (rapor
           tasima + initial_report parametresi)
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from latex_pipeline.qc_engine import (
    QCEngine,
    QCReport,
    QCIssue,
    collect_defined_env_names,
)
from latex_pipeline.autofix import AutoFixer, _find_math_spans


def _issues(report: QCReport, check_name: str):
    return [i for i in report.issues if i.check_name == check_name]


@pytest.fixture
def engine():
    return QCEngine()


@pytest.fixture
def fixer():
    return AutoFixer(qc_engine=QCEngine())


@pytest.fixture
def glossary_dir(tmp_path):
    terms = {
        "terms": {
            "neural network": {
                "tr": "yapay sinir agi",
                "priority": "high",
                "wrong_alternatives": ["bilgisayar agi"],
            },
            "boundary layer": {
                "tr": "sinir tabakasi",
                "priority": "high",
            },
            "nozzle": {
                "tr": "lule",
                "priority": "high",
                "wrong_alternatives": ["nozul"],
            },
        }
    }
    (tmp_path / "g.json").write_text(
        json.dumps(terms, ensure_ascii=False), encoding="utf-8",
    )
    return str(tmp_path)


# ======================================================================
# F1: broken_command_braces sahte pozitifleri
# ======================================================================

class TestF1BrokenCommandBraces:

    @pytest.mark.parametrize("text", [
        r"{\footnotesize Bir tablo dipnotu burada yer alir.}",
        r"\captionsetup{font=small}",
        r"\captionof{figure}{Bir sekil aciklamasi}",
        r"\footnotemark[1] ile devam eder.",
    ])
    def test_uzun_mesru_komutlar_issue_uretmez(self, engine, text):
        report = engine.analyze(text, "")
        assert _issues(report, "broken_command_braces") == []

    @pytest.mark.parametrize("text", [
        r"Bu \textbfSpektrum analizi yapildi.",
        r"Burada \textitFonksiyon tanimlanir.",
        r"Bu \emphOnemli bir noktadir.",
    ])
    def test_gercek_kirik_vaka_yakalanir(self, engine, text):
        report = engine.analyze(text, "")
        assert len(_issues(report, "broken_command_braces")) >= 1

    def test_autofix_footnotesize_dokunmaz(self, fixer):
        body = r"{\footnotesize Bir tablo dipnotu burada yer alir.}"
        fixed, fixes = fixer._fix_broken_command_braces(body, [], [])
        assert fixed == body
        assert fixes == []
        assert r"\footnote{size}" not in fixed

    def test_autofix_captionof_dokunmaz(self, fixer):
        body = r"\captionof{figure}{Bir sekil} ve devami."
        fixed, _ = fixer._fix_broken_command_braces(body, [], [])
        assert fixed == body

    def test_autofix_kirik_vakayi_hala_duzeltir(self, fixer):
        body = r"Bu \textbfSpektrum analizi."
        fixed, fixes = fixer._fix_broken_command_braces(body, [], [])
        assert r"\textbf{Spektrum}" in fixed
        assert len(fixes) >= 1


# ======================================================================
# F2: kullanici tanimli ortam adlari korunur (A-9 esdegeri)
# ======================================================================

class TestF2UserDefinedEnvs:

    BODY_DEFINED = (
        "\\newtheorem{tanim}{Tan\u0131m}\n"
        "\\begin{tanim}\nBir k\u00fcme bo\u015f olabilir.\n\\end{tanim}\n"
    )

    def test_collect_defined_env_names(self):
        text = (
            "\\newenvironment{ozelkutu}{}{}\n"
            "\\renewenvironment{alinti}{}{}\n"
            "\\newtheorem{tanim}{Tan\u0131m}\n"
            "\\newtheorem*{sonuc}{Sonu\u00e7}\n"
        )
        defined = collect_defined_env_names(text)
        assert defined == {"ozelkutu", "alinti", "tanim", "sonuc"}
        assert collect_defined_env_names("") == frozenset()

    def test_qc_tanimli_ortam_issue_degil(self, engine):
        report = engine.analyze(self.BODY_DEFINED, "")
        assert _issues(report, "translated_env_name") == []

    def test_qc_tanimsiz_ortam_hala_issue(self, engine):
        body = "\\begin{tanim}\nMetin.\n\\end{tanim}\n"
        report = engine.analyze(body, "")
        assert len(_issues(report, "translated_env_name")) >= 1

    def test_qc_user_defined_envs_parametresi(self):
        # preamble_ozel.tex'teki tanimlar govdede gorunmez -- disaridan
        # kume gecilebilmeli
        eng = QCEngine(user_defined_envs={"tanim"})
        body = "\\begin{tanim}\nMetin.\n\\end{tanim}\n"
        report = eng.analyze(body, "")
        assert _issues(report, "translated_env_name") == []

    def test_autofix_tanimli_ortami_korur(self, fixer):
        fixed, fixes = fixer._fix_translated_env_name(self.BODY_DEFINED, [], [])
        assert "\\begin{tanim}" in fixed
        assert "\\end{tanim}" in fixed
        assert "definition" not in fixed
        assert fixes == []

    def test_autofix_tanimsiz_ortami_hala_duzeltir(self, fixer):
        body = "\\begin{sekil}\nx\n\\end{sekil}\n"
        fixed, fixes = fixer._fix_translated_env_name(body, [], [])
        assert "\\begin{figure}" in fixed
        assert len(fixes) == 2

    def test_autofix_user_defined_envs_parametresi(self):
        fx = AutoFixer(qc_engine=QCEngine(), user_defined_envs={"sekil"})
        body = "\\begin{sekil}\nx\n\\end{sekil}\n"
        fixed, fixes = fx._fix_translated_env_name(body, [], [])
        assert fixed == body
        assert fixes == []

    def test_manuel_modda_autofixer_run_tanimli_ortami_bozmaz(self):
        # Canli reproya esdeger: manuel modda tam run() dongusu
        fx = AutoFixer(qc_engine=QCEngine(), manual_translation=True)
        fixed, _ = fx.run(self.BODY_DEFINED, "")
        assert "\\begin{tanim}" in fixed
        assert "\\begin{definition}" not in fixed


# ======================================================================
# F4: _fix_prompt_leakage artik temizligi verbatim'i ezmesin
# ======================================================================

class TestF4PromptLeakVerbatim:

    def test_lstlisting_ellipsis_ve_bos_satir_korunur(self, fixer):
        body = (
            "Sadece \u00e7eviriyi ver\n\n"
            "\\begin{lstlisting}\n"
            "def f(x):\n"
            "    ...\n"
            "\n"
            "\n"
            "    return x\n"
            "\\end{lstlisting}\n"
        )
        fixed, fixes = fixer._fix_prompt_leakage(
            body, [], _find_math_spans(body),
        )
        assert len(fixes) >= 1  # leak gercekten silindi
        assert "\n    ...\n" in fixed  # Ellipsis satiri duruyor
        assert "def f(x):\n    ...\n\n\n    return x" in fixed  # bos satirlar da

    def test_leak_yoksa_govde_aynen_kalir(self, fixer):
        body = "Normal metin.\n\n\n\n\nDevam ediyor...\n\n- \n"
        fixed, fixes = fixer._fix_prompt_leakage(body, [], [])
        assert fixes == []
        assert fixed == body

    def test_leak_varsa_verbatim_disi_temizlik_calisir(self, fixer):
        body = "Sadece \u00e7eviriyi ver\n\n\n\n\nGer\u00e7ek metin.\n"
        fixed, fixes = fixer._fix_prompt_leakage(body, [], [])
        assert len(fixes) >= 1
        assert "\n\n\n" not in fixed  # 3+ bos satir collapse edildi


# ======================================================================
# F5: _fix_stash_placeholder_orphan kod girintisini korusun
# ======================================================================

class TestF5StashOrphanIndent:

    def test_lstlisting_girinti_korunur(self, fixer):
        body = (
            "Metin <<REF_3>> devam ediyor.\n"
            "\\begin{lstlisting}\n"
            "def f():\n"
            "    if True:\n"
            "        return  1\n"
            "\\end{lstlisting}\n"
        )
        fixed, fixes = fixer._fix_stash_placeholder_orphan(
            body, [], _find_math_spans(body),
        )
        assert "<<REF_3>>" not in fixed
        assert len(fixes) == 1
        assert "    if True:" in fixed
        assert "        return" in fixed

    def test_guillemet_dalinda_da_girinti_korunur(self, fixer):
        body = (
            "Metin \u00abREF\u2081\u2082 >> devam ediyor.\n"
            "\\begin{verbatim}\n"
            "    girintili  satir\n"
            "\\end{verbatim}\n"
        )
        fixed, fixes = fixer._fix_stash_placeholder_orphan(
            body, [], _find_math_spans(body),
        )
        assert "\u00abREF" not in fixed
        assert "    girintili  satir" in fixed

    def test_verbatim_disi_cift_bosluk_hala_temizlenir(self, fixer):
        body = "Metin <<REF_1>> ve devami."
        fixed, _ = fixer._fix_stash_placeholder_orphan(body, [], [])
        assert "  " not in fixed


# ======================================================================
# F7: cok desenli donglerde bayat math_spans
# ======================================================================

class TestF7StaleMathSpans:

    def test_caption_fix_math_icine_yazmaz(self, fixer):
        prefix = " ".join(f"Fig. {i} incelendi." for i in range(1, 60))
        body = prefix + "\n\\[ E = mc^2 \\text{Proof} \\text{Table 3} \\]\n"
        fixed, fixes = fixer._fix_caption_inconsistency(
            body, [], _find_math_spans(body),
        )
        # Math ICI dokunulmamis olmali
        assert "\\text{Proof}" in fixed
        assert "\\text{Table 3}" in fixed
        # Math DISI normalize edilmis olmali
        assert "Fig. 1 " not in fixed
        assert len(fixes) >= 59

    def test_latex_encoded_turkish_math_icine_yazmaz(self, fixer):
        # Onceki desenlerin kisaltmasi (\c{c} -> c: -4 char/adet) math
        # span'ini kaydirir; sonraki desen (\"{u}) math icinde eslesirdi.
        prefix = " ".join("ge\\c{c}mi\\c{s}" for _ in range(40))
        body = prefix + " $x \\\"{u} y$ sonu.\n"
        fixed, fixes = fixer._fix_latex_encoded_turkish(
            body, [], _find_math_spans(body),
        )
        assert "\\\"{u}" in fixed  # math ici korundu
        assert "ge\u00e7mi\u015f" in fixed  # math disi cevrildi


# ======================================================================
# F8: 29a pozisyonel paragraf kiyasi senkron kurtarma
# ======================================================================

class TestF8ParagraphResync:

    @staticmethod
    def _halluc_paras(engine, tr_text, en_text):
        report = engine.analyze(tr_text, en_text)
        return [
            i for i in _issues(report, "hallucination")
            if "paragraph" in i.message
        ]

    def test_tek_birlesme_zincirleme_critical_uretmez(self, engine):
        en_lens = [600, 80, 500, 90, 700, 300, 450, 120, 350, 500]
        en_paras = [("a" * (n - 2) + ".") for n in en_lens]
        # TR'nin ilk iki paragrafi birlesmis (cok yaygin MT davranisi)
        tr_paras = [en_paras[0] + " " + en_paras[1]] + list(en_paras[2:])
        found = self._halluc_paras(
            engine, "\n\n".join(tr_paras), "\n\n".join(en_paras),
        )
        assert found == []

    def test_tr_bolunmesi_zincirleme_critical_uretmez(self, engine):
        en_paras = ["c" * 400 for _ in range(6)]
        tr_paras = ["c" * 200, "c" * 200] + ["c" * 400 for _ in range(5)]
        found = self._halluc_paras(
            engine, "\n\n".join(tr_paras), "\n\n".join(en_paras),
        )
        assert found == []

    def test_gercek_bloat_hala_yakalanir(self, engine):
        en_paras = ["b" * 300 for _ in range(8)]
        tr_paras = list(en_paras)
        tr_paras[4] = en_paras[4] * 7
        found = self._halluc_paras(
            engine, "\n\n".join(tr_paras), "\n\n".join(en_paras),
        )
        assert len(found) == 1
        assert "hallucination/repetition" in found[0].message

    def test_gercek_bilgi_kaybi_hala_yakalanir(self, engine):
        en_paras = ["b" * 300 for _ in range(8)]
        tr_paras = list(en_paras)
        tr_paras[4] = "b" * 50
        found = self._halluc_paras(
            engine, "\n\n".join(tr_paras), "\n\n".join(en_paras),
        )
        assert len(found) == 1
        assert "Information loss" in found[0].message

    def test_hizali_temiz_belge_sifir_issue(self, engine):
        paras = ["d" * 300 for _ in range(8)]
        text = "\n\n".join(paras)
        assert self._halluc_paras(engine, text, text) == []


# ======================================================================
# F9/F15: glossary on-filtresi davranis paritesi
# ======================================================================

class TestF9GlossaryPrefilterParity:

    def test_cevrilmemis_terim_hala_yakalanir(self, glossary_dir):
        eng = QCEngine(glossary_dir=glossary_dir)
        report = eng.analyze("Bu neural network modeli iyi calisiyor.")
        found = _issues(report, "glossary_compliance")
        assert any("neural network" in i.message for i in found)

    def test_buyuk_harfli_gecis_hala_yakalanir(self, glossary_dir):
        # IGNORECASE paritesi: metinde 'Neural Network' buyuk harfli
        eng = QCEngine(glossary_dir=glossary_dir)
        report = eng.analyze("Bu Neural Network modeli iyi calisiyor.")
        found = _issues(report, "glossary_compliance")
        assert any("neural network" in i.message for i in found)

    def test_cok_kelimeli_terim_yakalanir(self, glossary_dir):
        eng = QCEngine(glossary_dir=glossary_dir)
        report = eng.analyze("Akista boundary layer ayrilmasi gozlendi.")
        found = _issues(report, "glossary_compliance")
        assert any("boundary layer" in i.message for i in found)

    def test_kelime_siniri_paritesi_alt_dize_eslesmez(self, glossary_dir):
        # 'nozzles' icinde 'nozzle' \b ile eslesmiyordu; on-filtre de
        # eslestirmemeli (kelime kumesinde 'nozzles' var, 'nozzle' yok)
        eng = QCEngine(glossary_dir=glossary_dir)
        report = eng.analyze("Kelime supernozzle ve nozzles gibi.")
        found = _issues(report, "glossary_compliance")
        assert not any("'nozzle'" in i.message for i in found)

    def test_math_ici_terim_maskelenir(self, glossary_dir):
        eng = QCEngine(glossary_dir=glossary_dir)
        tr = (
            "\\begin{equation}\n neural network = 5 \n\\end{equation}\n"
            "Bu yapay sinir agi modeli."
        )
        report = eng.analyze(tr)
        found = _issues(report, "glossary_compliance")
        assert not any("neural network" in i.message for i in found)

    def test_wrong_translation_part_a_hala_calisir(self, glossary_dir):
        eng = QCEngine(glossary_dir=glossary_dir)
        report = eng.analyze("Bu bilgisayar agi modeli neural degil.")
        found = _issues(report, "wrong_translation")
        assert any("bilgisayar agi" in i.message for i in found)

    def test_wrong_translation_yokken_temiz(self, glossary_dir):
        eng = QCEngine(glossary_dir=glossary_dir)
        report = eng.analyze("Bu yapay sinir agi modeli gayet iyi.")
        found = _issues(report, "wrong_translation")
        assert not any("bilgisayar agi" in i.message for i in found)


# ======================================================================
# F17: AutoFixer.run gereksiz analyze cagrilari
# ======================================================================

class _CountingEngine(QCEngine):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.analyze_calls = 0

    def analyze(self, *args, **kwargs):
        self.analyze_calls += 1
        return super().analyze(*args, **kwargs)


class TestF17AnalyzeReuse:

    def test_temiz_govde_tek_analiz(self):
        eng = _CountingEngine()
        fx = AutoFixer(qc_engine=eng, max_iterations=3)
        body = "Bu tamamen temiz bir T\u00fcrk\u00e7e paragraft\u0131r.\n"
        fixed, report = fx.run(body, "")
        assert report.converged
        # Eski davranis: initial + iter1 + final = 3; yeni: 1
        assert eng.analyze_calls == 1

    def test_initial_report_verilirse_sifir_on_analiz(self):
        eng = _CountingEngine()
        body = "Bu tamamen temiz bir T\u00fcrk\u00e7e paragraft\u0131r.\n"
        pre = eng.analyze(body, "")
        eng.analyze_calls = 0
        fx = AutoFixer(qc_engine=eng, max_iterations=3)
        fixed, report = fx.run(body, "", initial_report=pre)
        assert report.converged
        assert eng.analyze_calls == 0
        assert report.initial_score == pre.score

    def test_fix_dongusunde_iterasyon_basi_tek_analiz(self):
        eng = _CountingEngine()
        fx = AutoFixer(qc_engine=eng, max_iterations=3)
        body = "Bu \\textbfSpektrum analizi yap\u0131ld\u0131.\n"
        fixed, report = fx.run(body, "")
        assert "\\textbf{Spektrum}" in fixed
        # Eski: initial + (pre+post) x iter + final; yeni: initial +
        # yalniz fix uygulayan iterasyonlarda 1 post analizi (yakinsama
        # iterasyonu ve final artik analiz kosturmaz)
        assert eng.analyze_calls == 1 + len(report.fixes_per_iteration)

    def test_davranis_paritesi_initial_report_ile_ve_olmadan(self):
        body = (
            "Bu \\textbfSpektrum analizi yap\u0131ld\u0131.\n\n"
            "Sadece \u00e7eviriyi ver\n\nDevam metni burada.\n"
        )
        eng1 = QCEngine()
        fx1 = AutoFixer(qc_engine=eng1, max_iterations=3)
        fixed1, rep1 = fx1.run(body, "")

        eng2 = QCEngine()
        pre = eng2.analyze(body, "")
        fx2 = AutoFixer(qc_engine=eng2, max_iterations=3)
        fixed2, rep2 = fx2.run(body, "", initial_report=pre)

        assert fixed1 == fixed2
        assert rep1.total_fixes == rep2.total_fixes
        assert rep1.initial_score == rep2.initial_score
        assert rep1.final_score == rep2.final_score
        assert rep1.converged == rep2.converged

    def test_final_score_rapor_tutarli(self):
        # final_qc yeniden analiz edilmese de rapor gercek son gövdeyi
        # yansitmali: bagimsiz taze analizle ayni skor
        eng = QCEngine()
        fx = AutoFixer(qc_engine=eng, max_iterations=3)
        body = "Bu \\textbfSpektrum analizi yap\u0131ld\u0131.\n"
        fixed, report = fx.run(body, "")
        fresh = QCEngine().analyze(fixed, "")
        assert report.final_score == fresh.score
