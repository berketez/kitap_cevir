"""F2/F3 glue kabloları için regresyon testleri (2026-07-28 bug avı).

Ajan fix'lerinin ana Claude tarafından bağlanan uçları:
- F2: preamble_ozel.tex'te tanımlı ortam adları sanitizer'a da muaf geçilir
  (QC/AutoFixer parametreleri core test dosyasında kilitli).
- F3: fail-ratio eşiği config/defaults.py'de tek kaynak
  (tests/test_fixav20260728_runtime.py'deki bekçi de import kıyasına geçti).
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from latex_pipeline.sanitizer import LatexSanitizer  # noqa: E402


class TestF2SanitizerUserDefinedEnvs:
    """Gövdede tanımı olmayan ama preamble'da tanımlı ortam korunmalı."""

    BODY = "\\begin{tanim}\nBir terimin tanımı.\n\\end{tanim}\n"

    def test_without_param_renames(self):
        # Mevcut davranış (kontrol): tanımsız 'tanim' -> 'definition'.
        sanitizer = LatexSanitizer(manual_translation=True)
        cleaned, _ = sanitizer.sanitize(self.BODY)
        assert "\\begin{definition}" in cleaned
        assert "\\begin{tanim}" not in cleaned

    def test_user_defined_env_preserved(self):
        sanitizer = LatexSanitizer(manual_translation=True,
                                   user_defined_envs={"tanim"})
        cleaned, _ = sanitizer.sanitize(self.BODY)
        assert "\\begin{tanim}" in cleaned
        assert "\\end{tanim}" in cleaned
        assert "definition" not in cleaned

    def test_other_envs_still_renamed(self):
        # Muafiyet yalnız verilen adlar için; 'sekil' düzeltilmeye devam eder.
        body = self.BODY + "\\begin{sekil}\nX\n\\end{sekil}\n"
        sanitizer = LatexSanitizer(manual_translation=True,
                                   user_defined_envs={"tanim"})
        cleaned, _ = sanitizer.sanitize(body)
        assert "\\begin{tanim}" in cleaned
        assert "\\begin{figure}" in cleaned
        assert "\\begin{sekil}" not in cleaned

    def test_body_definition_still_respected(self):
        # Parametre, gövde-içi tanım taramasının (A-9) yerine geçmez,
        # üstüne eklenir: gövdede tanımlı 'teorem' parametresiz de korunur.
        body = "\\newtheorem{teorem}{Teorem}\n" + \
               "\\begin{teorem}\nİfade.\n\\end{teorem}\n"
        sanitizer = LatexSanitizer(manual_translation=True,
                                   user_defined_envs={"tanim"})
        cleaned, _ = sanitizer.sanitize(body)
        assert "\\begin{teorem}" in cleaned


class TestF3ThresholdSingleSource:
    def test_all_modules_share_config_constant(self):
        from config.defaults import TRANSLATION_MAX_FAIL_RATIO as cfg
        import translate_book as tb
        import latex_pipeline.translator as lp_translator

        assert tb.TRANSLATION_MAX_FAIL_RATIO == cfg
        assert lp_translator.TRANSLATION_MAX_FAIL_RATIO == cfg


class TestH1InitialReportNotMutated:
    """Reviewer H1: AutoFixer.run initial_report'u YERİNDE değiştirmemeli.

    Eski davranış: satır-sayısı değiştiren fix sonrası AF-09 remap'i
    çağıranın (ve translate_book F36 QC cache'inin) issue nesnelerinin
    line_num'ını kirletiyordu — revert dalında bile (gövde değişmeden).
    """

    def _build(self):
        from latex_pipeline.qc_engine import QCReport, QCIssue
        body = "\n".join([
            "Birinci satir Turkce metin.",
            "Here is the translation:",
            "", "", "",
            "Ikinci satir Turkce metin.",
            "This is a fully English sentence that should be translated.",
            "Ucuncu satir.",
        ])
        issues = [
            QCIssue(check_name="prompt_leakage", severity="high",
                    message="leak", line_num=2, auto_fixable=True,
                    confidence=0.95, fix_suggestion="", context=""),
            QCIssue(check_name="untranslated", severity="high",
                    message="en", line_num=7, auto_fixable=True,
                    confidence=0.95, fix_suggestion="", context=""),
        ]
        return body, QCReport(score=50.0, issues=issues)

    def test_caller_report_untouched(self):
        from latex_pipeline.autofix import AutoFixer
        body, rep = self._build()
        before = [(i.check_name, i.line_num) for i in rep.issues]
        AutoFixer().run(body, source_body=body, initial_report=rep)
        after = [(i.check_name, i.line_num) for i in rep.issues]
        assert before == after, (
            "initial_report mutasyona uğradı — F36 QC cache'i zehirlenir")

    def test_qc_cache_stays_clean_after_autofix(self):
        # translate_book._qc_analyze cache'i ile uçtan uca: fixer koşusundan
        # sonra cache'ten dönen rapor taze analizle aynı satırları vermeli.
        import translate_book as tb
        from latex_pipeline.autofix import AutoFixer
        from latex_pipeline.qc_engine import QCEngine

        body, _ = self._build()
        engine = QCEngine()
        cache = {}
        cached = tb._qc_analyze(engine, body, body, False, cache=cache)
        AutoFixer(qc_engine=engine).run(
            body, source_body=body, initial_report=cached)
        again = tb._qc_analyze(engine, body, body, False, cache=cache)
        fresh = engine.analyze(body, body, manual_translation=False)
        assert ([(i.check_name, i.line_num) for i in again.issues]
                == [(i.check_name, i.line_num) for i in fresh.issues])


class TestH2RepetitionPrefilterBoundary:
    def test_29_char_loop_not_filtered_out(self):
        # 15 token + 14 ayraç = 29 karakter: gerçek asgari döngü uzunluğu.
        from latex_pipeline.qc_engine import may_have_repetition_loop
        text = "a b c d e a b c d e a b c d e"
        assert len(text) == 29
        assert may_have_repetition_loop(text) is True


class TestR2KnownLongerCommands:
    """Reviewer R2: F1'in toptan a-z dışlaması yerine bilinen-komut kümesi.

    Meşrular (\\footnotesize, \\captionsetup) issue üretmez; tamamı-küçük
    yapışık kırıklar (\\textbfdeger — F1 sonrası kör nokta) yeniden yakalanır.
    """

    def _issues(self, body):
        from latex_pipeline.qc_engine import QCEngine
        return [i for i in QCEngine().analyze(body, "").issues
                if i.check_name == "broken_command_braces"]

    def test_legitimate_longer_commands_pass(self):
        for body in ("{\\footnotesize Dipnot metni.}",
                     "\\captionsetup{font=small}",
                     "\\captionof{figure}{X}",
                     "\\footnotemark[1] metin"):
            assert self._issues(body) == [], body

    def test_lowercase_attached_break_caught_again(self):
        assert len(self._issues("\\textbfdeger onemli")) == 1
        assert len(self._issues("\\textitvurgu kelime")) == 1

    def test_uppercase_break_still_caught(self):
        assert len(self._issues("\\textbfSpektrum analizi")) == 1

    def test_autofix_matches_qc_rule(self):
        from latex_pipeline.autofix import AutoFixer
        out, _ = AutoFixer().run(
            "{\\footnotesize Dipnot metni.} ve \\textbfdeger burada", "x")
        assert "\\footnote{size}" not in out
        assert "\\footnotesize" in out
        assert "\\textbf{deger}" in out


class TestH3JsonContract:
    def test_early_exit_emits_full_field_set(self):
        # --json verildiğinde HER çıkış yolu sabit alan kümesiyle JSON basar.
        import json as _json
        import subprocess
        import sys as _sys
        proc = subprocess.run(
            [_sys.executable, str(PROJECT_ROOT / "2_derle.py"),
             str(PROJECT_ROOT / "boyle_bir_dosya_yok_9x7.tex"), "--json"],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT),
        )
        assert proc.returncode == 1
        last = proc.stdout.strip().splitlines()[-1]
        payload = _json.loads(last)
        assert set(payload) == {"ok", "qc_ok", "exit_code", "slug",
                                "book_name", "output_dir", "pdf", "epub",
                                "error", "qc_score", "qc_report",
                                "qc_top", "epub_note",
                                # R5 (2026-09-25): derleme raporu alanları
                                "tex_hatalari", "eksik_glif", "epub_kapsam"}
        # Derleme hiç koşmadı: sayılar 0, EPUB kapsamı yok.
        assert payload["tex_hatalari"] == 0 and payload["eksik_glif"] == 0
        assert payload["epub_kapsam"] is None
        assert payload["ok"] is False
        assert payload["error"] == "input_missing"
