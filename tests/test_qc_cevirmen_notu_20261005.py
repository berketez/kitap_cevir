"""QC: altyazı içi "(Çevirmen notu: …)" parantezi uzunluk hesaplarına sayılmaz.

Berke kararı (30 Eylül 2026): grafik içi adların eşlemesi altyazının sonuna
parantez içinde çevirmen notuyla verilir. Elle onarılan bir raporda bu notlar
QC'de dört CRITICAL "TR paragrafı EN'in 3,7–27 katı" yanlış alarmı
üretiyordu. Dipnot biçimli notlar için aynı
kural 25 Eylül'den beri vardı (tests/test_dalga_q_20260925.py).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from latex_pipeline.qc_engine import QCEngine, strip_translator_notes  # noqa: E402


def _issues(report, category, needle):
    return [i for i in report.issues
            if i.category == category and needle in i.message]


@pytest.fixture(scope="module")
def engine():
    return QCEngine()


_NOTE = ("(Çevirmen notu: grafikteki adlar — başlık: “Sensörlerin gün boyu "
         "ölçtüğü sıcaklık, nem, basınç ve rüzgâr değerleri”; Readings: Ölçümler; "
         "Sensors: Sensörler; Values (avg): Değerler (ortalama); "
         "Humidity: Nem; Pressure: Basınç; Time (hours): Zaman (saat))")


class TestParantezliNotAyiklama:
    def test_strip_basic(self):
        txt = "Şekil 3: Sensör değerleri " + _NOTE + " devam."
        assert strip_translator_notes(txt) == "Şekil 3: Sensör değerleri  devam."

    def test_nested_parentheses_inside_note(self):
        txt = "a (Çevirmen notu: Values (avg): Değerler (ortalama)) b"
        assert strip_translator_notes(txt) == "a  b"

    def test_variants_and_italic(self):
        txt = ("a (\\textit{Çeviri notu:} x) b (çevirmen notu: küçük) c"
               " (Çevirenin notu: y) d")
        assert strip_translator_notes(txt) == "a  b  c  d"

    def test_other_parentheses_untouched(self):
        txt = "a (Kaynak: Glushko, 1946) b (notu bkz. Ek A) c"
        assert strip_translator_notes(txt) is txt

    def test_unbalanced_left_as_is(self):
        txt = "a (Çevirmen notu: kapanmayan parantez"
        assert strip_translator_notes(txt) is txt

    def test_escaped_math_parens_do_not_count(self):
        txt = "a (Çevirmen notu: \\(x\\) değişkeni) b"
        assert strip_translator_notes(txt) == "a  b"

    def test_footnote_and_paren_together(self):
        txt = ("a\\footnote{Çevirmen notu: dipnot} b "
               "(Çevirmen notu: parantez) c")
        assert strip_translator_notes(txt) == "a b  c"


class TestParagrafOraniNotuSaymaz:
    # EN paragrafı 50 karakterden kısaysa 29a onu başlık sayıp atlar.
    _EN = ("\\section{Weather station}\n\n"
           "Figure 3: Readings of all station sensors over the whole measuring day.\n\n"
           "The stations reported their values to a central logger.\n")
    _TR = ("\\section{Hava istasyonu}\n\n"
           "Şekil 3: Ölçüm günü boyunca bütün istasyon sensörlerinin değerleri. " + _NOTE + "\n\n"
           "İstasyonlar değerlerini merkezî bir kaydediciye bildirdi.\n")

    def test_manual_mode_ignores_paren_note(self, engine):
        rep = engine.analyze(self._TR, self._EN, manual_translation=True)
        assert _issues(rep, "hallucination", "Possible hallucination") == []

    def test_yol_b_still_counts_note(self, engine):
        # Yerel modelde not bir sızıntıdır: davranış değişmedi.
        rep = engine.analyze(self._TR, self._EN, manual_translation=False)
        assert _issues(rep, "hallucination", "Possible hallucination")

    def test_real_bloat_still_caught_in_manual(self, engine):
        # Not ayıklanır ama kaynakta olmayan gerçek şişme yakalanmaya devam eder.
        caption = "Şekil 3: Ölçüm günü boyunca bütün istasyon sensörlerinin değerleri. "
        bloat = caption + "Bu kısım kaynakta olmayan uydurma bir açıklamadır. " * 8
        tr = self._TR.replace(caption + _NOTE, bloat)
        rep = engine.analyze(tr, self._EN, manual_translation=True)
        assert _issues(rep, "hallucination", "Possible hallucination")


class TestLatinceTekKaynak:
    """Latince alıntı kümeleri 5 Ekim'de qc_engine'den core.lang_detect'e taşındı."""

    def test_dominance_constants_equal(self):
        from core import lang_detect
        from latex_pipeline import qc_engine
        assert lang_detect.LATIN_QUOTE_DOMINANCE == qc_engine.SOURCE_QUOTE_LANG_DOMINANCE

    def test_qc_forwards_to_lang_detect(self):
        from core import lang_detect
        from latex_pipeline import qc_engine
        words = lang_detect.tokenize_words(
            "Quod si corpus omne quiescit, tamen vis eius semper manet.")
        assert qc_engine.is_latin_text(words) == lang_detect.is_latin_text(words)
        assert qc_engine.latin_quote_hits(words) == lang_detect.latin_quote_hits(words)
        assert not hasattr(qc_engine, "_LATIN_FUNCTION_WORDS")
