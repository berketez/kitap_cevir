"""Yol B QC fix sprint — regression tests for latex_pipeline/qc_engine.py.

Covers the ``qc`` group findings:

* QC-C1  manual_translation report-only downgrade
* QC-C2  _is_likely_english delegates to canonical is_english_line
* QC-C3  ALL-CAPS untranslated English headings/body no longer skipped
* QC-C8  'para'/'hasta' Turkish false positives removed from foreign check
* QC-N4  corrupt glossary values (mid-word capital I) gated out
* QC-N5  glossary glob is deterministic (sorted, last-writer-wins)
* QC-N6  _mask_protected_content preserves newlines (line-number alignment)
* QC-N7  no-handler checks are report-only (auto_fixable False)
* QC-N8  book-specific hallucination signatures removed
"""

import json

import pytest

from latex_pipeline.qc_engine import QCEngine


@pytest.fixture
def engine():
    return QCEngine()


# ---------------------------------------------------------------------------
# QC-C1 — manual_translation report-only
# ---------------------------------------------------------------------------

class TestManualTranslationReportOnly:

    MT_FEEDBACK = {
        "untranslated", "untranslated_heading", "mixed_language_line",
        "glossary_compliance", "wrong_translation", "first_person_academic",
    }

    def test_untranslated_autofixable_in_yol_b(self, engine):
        """Yol B (default): untranslated English is auto_fixable=True."""
        tr = (
            "THIS ENTIRE LINE REMAINED COMPLETELY UNTRANSLATED HERE AGAIN\n"
            "AND THIS SECOND LINE ALSO STAYED IN PLAIN ENGLISH ENTIRELY\n"
        )
        report = engine.analyze(tr)
        untr = [i for i in report.issues if i.check_name == "untranslated"]
        assert untr, "untranslated block should be detected"
        assert any(i.auto_fixable for i in untr)

    def test_untranslated_report_only_in_yol_a(self, engine):
        """Yol A (manual_translation=True): MT-feedback checks are report-only."""
        tr = (
            "THIS ENTIRE LINE REMAINED COMPLETELY UNTRANSLATED HERE AGAIN\n"
            "AND THIS SECOND LINE ALSO STAYED IN PLAIN ENGLISH ENTIRELY\n"
        )
        report = engine.analyze(tr, manual_translation=True)
        mt_issues = [i for i in report.issues if i.check_name in self.MT_FEEDBACK]
        assert mt_issues, "issues should still be DETECTED in manual mode"
        assert all(not i.auto_fixable for i in mt_issues), (
            "every MT-feedback issue must be auto_fixable=False in Yol A"
        )

    def test_manual_mode_does_not_change_score_or_visibility(self, engine):
        """Downgrading auto_fixable must not hide issues or change the score."""
        tr = "THIS ENTIRE LINE REMAINED COMPLETELY UNTRANSLATED HERE AGAIN\n"
        r_b = engine.analyze(tr)
        r_a = engine.analyze(tr, manual_translation=True)
        assert len(r_a.issues) == len(r_b.issues)
        assert r_a.score == r_b.score

    def test_backward_compatible_positional_call(self, engine):
        """analyze(tr, en) still works without the new keyword (SOZLESME 3)."""
        report = engine.analyze("Bir metin.", "A text.")
        assert report is not None


# ---------------------------------------------------------------------------
# QC-C3 — ALL-CAPS untranslated headings/body
# ---------------------------------------------------------------------------

class TestAllCapsUntranslated:

    def test_allcaps_english_heading_flagged(self, engine):
        tr = r"\chapter{DEEP LEARNING FUNDAMENTALS}"
        report = engine.analyze(tr)
        heads = [i for i in report.issues if i.check_name == "untranslated_heading"]
        assert heads, "ALL-CAPS English heading must be flagged (QC-C3)"

    def test_titlecase_english_heading_flagged(self, engine):
        tr = r"\chapter{Deep Learning Fundamentals}"
        report = engine.analyze(tr)
        heads = [i for i in report.issues if i.check_name == "untranslated_heading"]
        assert heads, "Title-case English heading must still be flagged"

    def test_turkish_allcaps_heading_not_flagged(self, engine):
        """ALL-CAPS Turkish heading (common Turkish words) must be exempt."""
        tr = r"\chapter{YENI BOLUM BASLANGICI}"
        report = engine.analyze(tr)
        heads = [i for i in report.issues if i.check_name == "untranslated_heading"]
        assert not heads

    def test_allcaps_body_line_flagged(self, engine):
        tr = "THIS ENTIRE LINE REMAINED COMPLETELY UNTRANSLATED IN ENGLISH HERE"
        report = engine.analyze(tr)
        untr = [i for i in report.issues if i.check_name == "untranslated"]
        assert untr, "ALL-CAPS English body line must be flagged"

    def test_allcaps_and_lowercase_body_consistent(self, engine):
        """Case must not change the untranslated verdict (QC-C2/C3)."""
        upper = "THIS ENTIRE LINE REMAINED COMPLETELY UNTRANSLATED IN ENGLISH HERE"
        lower = upper.lower()
        up = [i for i in engine.analyze(upper).issues if i.check_name == "untranslated"]
        lo = [i for i in engine.analyze(lower).issues if i.check_name == "untranslated"]
        assert bool(up) == bool(lo) is True


# ---------------------------------------------------------------------------
# QC-C2 — _is_likely_english delegates to canonical detector
# ---------------------------------------------------------------------------

class TestIsLikelyEnglishDelegation:

    def test_delegates_to_canonical(self, engine):
        from latex_pipeline.retranslator import is_english_line
        text = "the results are presented in the following section clearly"
        assert engine._is_likely_english(text) == is_english_line(
            text, min_words=4, min_length=20
        )

    def test_turkish_sentence_not_english(self, engine):
        assert not engine._is_likely_english(
            "Bu satır tamamen Türkçeye çevrilmiştir ve doğrudur."
        )


# ---------------------------------------------------------------------------
# QC-C8 — foreign language leakage false positives
# ---------------------------------------------------------------------------

class TestForeignLeakageWhitelist:

    def test_para_not_flagged(self, engine):
        tr = "Bu yatirimda para kaybi riski vardir. Iyi para yonetimi sarttir."
        report = engine.analyze(tr)
        fl = [i for i in report.issues if i.check_name == "foreign_language_leakage"]
        assert not fl, "'para' (money) must not be flagged as foreign (QC-C8)"

    def test_hasta_not_flagged(self, engine):
        tr = "Hasta bireylerde tedavi onemlidir. Hasta takibi surekli yapilir."
        report = engine.analyze(tr)
        fl = [i for i in report.issues if i.check_name == "foreign_language_leakage"]
        assert not fl, "'hasta' (patient) must not be flagged as foreign (QC-C8)"

    def test_genuine_spanish_still_flagged(self, engine):
        tr = "Esto es como pero donde cuando porque tiene mismo."
        report = engine.analyze(tr)
        fl = [i for i in report.issues if i.check_name == "foreign_language_leakage"]
        assert fl, "genuine Spanish leakage must still be detected"


# ---------------------------------------------------------------------------
# QC-N4 / QC-N5 — glossary loader gate + determinism
# ---------------------------------------------------------------------------

def _write_glossary(dir_path, name, terms):
    (dir_path / name).write_text(
        json.dumps({"terms": terms}, ensure_ascii=False), encoding="utf-8"
    )


class TestGlossaryLoader:

    def test_corrupt_value_gated_out(self, tmp_path):
        """QC-N4: 'radiation'->'isinIm' (mid-word capital I) must not load."""
        _write_glossary(tmp_path, "sci.json", {
            "radiation": {"tr": "isinIm", "priority": "high"},
            "gradient": {"tr": "eğim", "priority": "high"},
        })
        eng = QCEngine(glossary_dir=str(tmp_path))
        loaded = eng._load_glossary_for_qc()["high_terms"]
        assert "radiation" not in loaded, "corrupt value must be gated (QC-N4)"
        assert loaded.get("gradient") == "eğim", "clean value must still load"

    def test_corrupt_term_not_injected_into_report(self, tmp_path):
        _write_glossary(tmp_path, "sci.json", {
            "radiation": {"tr": "isinIm", "priority": "high"},
        })
        eng = QCEngine(glossary_dir=str(tmp_path))
        report = eng.analyze("Termal radiation etkisi burada incelenir.")
        gc = [i for i in report.issues if i.check_name == "glossary_compliance"]
        assert not gc, "corrupt glossary value must not drive a QC finding"

    def test_glob_sorted_deterministic_last_writer(self, tmp_path):
        """QC-N5: with sorted glob, the alphabetically-last file wins."""
        _write_glossary(tmp_path, "a_first.json", {
            "network": {"tr": "AAA_TR", "priority": "high"},
        })
        _write_glossary(tmp_path, "z_last.json", {
            "network": {"tr": "ZZZ_TR", "priority": "high"},
        })
        eng = QCEngine(glossary_dir=str(tmp_path))
        loaded = eng._load_glossary_for_qc()["high_terms"]
        assert loaded["network"] == "ZZZ_TR"


# ---------------------------------------------------------------------------
# QC-N6 — newline-preserving mask
# ---------------------------------------------------------------------------

class TestMaskPreservesNewlines:

    def test_mask_keeps_newline_count(self):
        text = "A\n\\begin{equation}\nx=1\ny=2\n\\end{equation}\neigenvalue B"
        masked = QCEngine._mask_protected_content(text)
        assert masked.count("\n") == text.count("\n")

    def test_glossary_line_number_after_multiline_math(self, tmp_path):
        _write_glossary(tmp_path, "sci.json", {
            "eigenvalue": {"tr": "özdeğer", "priority": "high"},
        })
        eng = QCEngine(glossary_dir=str(tmp_path))
        tr = (
            "A paragraph line.\n"        # 1
            "\\begin{equation}\n"        # 2
            "x=1\n"                       # 3
            "y=2\n"                       # 4
            "\\end{equation}\n"          # 5
            "The eigenvalue is here.\n"  # 6
        )
        report = eng.analyze(tr)
        gc = [i for i in report.issues if i.check_name == "glossary_compliance"]
        assert gc, "eigenvalue should be flagged (özdeğer absent)"
        assert gc[0].line_num == 6, (
            f"line number must survive multi-line math, got {gc[0].line_num}"
        )


# ---------------------------------------------------------------------------
# QC-N7 — no-handler checks are report-only
# ---------------------------------------------------------------------------

class TestReportOnlyNoHandler:

    def test_html_artifact_report_only(self, engine):
        tr = "<colgroup></colgroup><tbody> hucre icerigi burada"
        report = engine.analyze(tr)
        html = [i for i in report.issues if i.check_name == "html_artifact"]
        assert html, "3+ HTML tags on a line should be detected"
        assert all(not i.auto_fixable for i in html)

    def test_section_number_consistency_report_only(self, engine):
        en = r"\section{4.4.2 Introduction}"
        tr = r"\section{4.4.3 Giriş bölümü metni}"
        report = engine.analyze(tr, en)
        sec = [i for i in report.issues
               if i.check_name == "section_number_consistency"]
        assert sec, "section number mismatch should be detected"
        assert all(not i.auto_fixable for i in sec)

    def test_hallucination_pattern_report_only(self, engine):
        tr = "Bu kelime kelime kelime kelime tekrar ediyor burada."
        report = engine.analyze(tr)
        hp = [i for i in report.issues if i.check_name == "hallucination_pattern"]
        assert hp, "repeated word should be detected"
        assert all(not i.auto_fixable for i in hp)


# ---------------------------------------------------------------------------
# QC-N8 — book-specific hallucination signatures removed
# ---------------------------------------------------------------------------

class TestKnownWrongTranslationsPruned:

    @pytest.mark.parametrize("tr", [
        "Kilise üyelerinin çoğu ibadete katılır.",
        "Bu ülkede kumar oynamak yasaktır.",
        "Gazeteye kişisel ilan verdi.",
        "Rahip püsküllü elbise giymişti.",
    ])
    def test_legit_prose_not_flagged(self, engine, tr):
        report = engine.analyze(tr)
        wt = [i for i in report.issues if i.check_name == "wrong_translation"]
        assert not wt, f"legitimate prose wrongly flagged: {tr!r}"

    def test_domain_general_still_flagged(self, engine):
        """Kept signature: 'bilgisayar ağı' (neural network) still caught."""
        report = engine.analyze("Bir bilgisayar ağı modeli egitildi.")
        wt = [i for i in report.issues if i.check_name == "wrong_translation"]
        assert wt, "domain-general wrong translation must remain flagged"
