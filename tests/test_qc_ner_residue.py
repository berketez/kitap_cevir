"""QC bozulmuş NER placeholder kalıntısı tespiti (2026-07-06 sutton smoke).

Bug: QC orphan kontrolleri yalnızca kanonik desenleri (<<TERM_N>>,
@@TERMn@@) arıyordu. MT'nin bozduğu varyant (MİYOR0000@@) hiçbir kontrole
takılmadı ve rapor "orphan=0, PASS" dedi. Yeni `ner_placeholder_residue`
kontrolü bozuk kalıntıları yakalar; auto_fixable=False — silmek içeriği
(örn. yazar adını) yok eder, doğru aksiyon kaynaktan geri yüklemedir.
"""

from latex_pipeline.qc_engine import QCEngine


class TestNerResidueCheck:
    def test_mangled_residue_in_heading_flagged(self):
        engine = QCEngine()
        en = "\\subsubsection{OSCAR BIBLARZ}\n\nProfessor Emeritus text here."
        tr = "\\subsubsection{MİYOR0000@@}\n\nProfesör Emeritus metni burada."
        report = engine.analyze(tr, en)
        residue = [
            i for i in report.issues if i.check_name == "ner_placeholder_residue"
        ]
        assert len(residue) >= 1
        assert residue[0].auto_fixable is False
        assert residue[0].severity == "critical"

    def test_bare_residue_flagged(self):
        engine = QCEngine()
        en = "Gravity-Free Drag-Free Space Flight / 99"
        tr = "Yerçekimi-MİYOR0000-MİYOR0001 / 99"
        report = engine.analyze(tr, en)
        residue = [
            i for i in report.issues if i.check_name == "ner_placeholder_residue"
        ]
        assert len(residue) >= 1

    def test_full_wrap_translated_word_flagged(self):
        engine = QCEngine()
        en = "Written by OSCAR BIBLARZ in 2017."
        tr = "@@BİLİNMİYOR0000@@ tarafından 2017'de yazılmıştır."
        report = engine.analyze(tr, en)
        residue = [
            i for i in report.issues if i.check_name == "ner_placeholder_residue"
        ]
        assert len(residue) >= 1

    def test_clean_text_not_flagged(self):
        engine = QCEngine()
        en = "This is a normal paragraph about rocket engines."
        tr = (
            "Bu, füze motorları hakkında normal bir paragraftır. "
            "Yıl 2024, ISBN 9781118753651, faks (978) 646-8600."
        )
        report = engine.analyze(tr, en)
        residue = [
            i for i in report.issues if i.check_name == "ner_placeholder_residue"
        ]
        assert residue == []
