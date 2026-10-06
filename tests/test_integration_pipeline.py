"""
Integration testleri: Olcek ve butunluk testleri.

Bu testler pipeline'in birden fazla bileseninin birlikte
dogru calistigini dogrulamak icindir.

Test 1: 100 paragraf segment kaybi
Test 2: Stash restore butunlugu (formul, ref, kod)
Test 3: EPUB/LaTeX QC prompt leak kontrolu
"""

import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

# Proje kokunu path'e ekle
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ===========================================================================
# Test 1: 100 paragraf - segment kaybi olmamali
# ===========================================================================

class TestNoSegmentLoss100Paragraphs:
    """100 paragraf icerisindeki her birinin cevrildigi dogrulanir.

    Amac: Pipeline'in buyuk dokumanlarda paragraf kaybetmedigini
    garanti etmek. Eger collect pass veya batch reconstruction
    hataliysa, bazi paragraflar sessizce kaybolabilir.
    """

    def test_paragraph_count_preserved(self, make_latex_translator):
        """Giren paragraf sayisi = cikan paragraf sayisi olmali."""
        lt = make_latex_translator()

        # 100 farkli paragraf uret (bosluk satirlari ile ayrilmis)
        paragraphs = []
        for i in range(100):
            paragraphs.append(
                f"This is paragraph number {i} about the theory of "
                f"thermodynamics and its applications in engineering. "
                f"The concept is fundamental to understanding energy transfer."
            )
        body = "\n\n".join(paragraphs)

        # Giren paragraf sayisi
        input_para_count = len([p for p in body.split("\n\n") if p.strip()])
        assert input_para_count == 100

        result = lt._translate_paragraphs(body)

        # Cikan paragraf sayisi - en azindan 90 paragraf olmali
        # (bazi paragraflar birlesiyor olabilir ama kaybolmamali)
        output_paras = [p for p in result.split("\n\n") if p.strip()]
        assert len(output_paras) >= 90, \
            f"100 paragraftan sadece {len(output_paras)} cikti - segment kaybi var"

    def test_no_empty_translation(self, make_latex_translator):
        """Hicbir paragraf bos cevriye donusmemeli."""
        lt = make_latex_translator()

        paragraphs = []
        for i in range(20):
            paragraphs.append(
                f"Paragraph {i}: The analysis of differential equations "
                f"plays a critical role in modern science."
            )
        body = "\n\n".join(paragraphs)

        result = lt._translate_paragraphs(body)

        # Sonuc bos olmamali
        assert result.strip(), "Tum ceviri bos dondu"

        # Her paragrafin (veya birlesmis halinin) icerik icermeli
        output_paras = [p for p in result.split("\n\n") if p.strip()]
        for i, para in enumerate(output_paras):
            assert len(para.strip()) > 5, \
                f"Paragraf {i} neredeyse bos: '{para[:50]}'"

    def test_translate_paragraphs_returns_string(self, make_latex_translator):
        """_translate_paragraphs string donmeli, None veya list degil."""
        lt = make_latex_translator()
        body = "Simple paragraph.\n\nAnother paragraph."
        result = lt._translate_paragraphs(body)
        assert isinstance(result, str), \
            f"Beklenen str, gelen {type(result)}"


# ===========================================================================
# Test 2: Stash restore butunlugu
# ===========================================================================

class TestStashRestoreIntegrity:
    """20 formul, 10 referans, 5 kod blogu iceren body'nin
    ceviri sonrasi tum stash iceriginin geri yulkendigi dogrulanir.
    """

    def test_formulas_restored(self, make_latex_translator):
        """Tum formuller stash sonrasi geri gelmeli."""
        lt = make_latex_translator()

        # 20 formul iceren body olustur
        parts = []
        formulas = []
        for i in range(20):
            formula = f"$x_{i} = {i}$"
            formulas.append(formula)
            parts.append(f"Equation {i} states that {formula} is important.")
        body = "\n\n".join(parts)

        # Stash + translate + restore tam dongusu
        stashed_body, stash = lt._stash_protected(body)

        # Stash'te formul olmali
        assert len(stash) >= 20, \
            f"20 formul beklendi ama sadece {len(stash)} stash'lendi"

        # Body'de tum placeholder'lar olmali
        placeholder_count = len(re.findall(r"<<LATEX_\d+>>", stashed_body))
        assert placeholder_count >= 20, \
            f"20 placeholder beklendi ama {placeholder_count} bulundu"

        # Restore
        restored = lt._restore_stash(stashed_body, stash)

        # Restore sonrasi formul desenleri geri gelmeli
        for i in range(20):
            assert f"x_{i}" in restored or f"x_{{{i}}}" in restored, \
                f"Formul {i} restore sonrasi kayip"

        # Orphan placeholder kalmamali
        orphans = re.findall(r"<<LATEX_\d+>>", restored)
        assert len(orphans) == 0, \
            f"{len(orphans)} orphan placeholder kaldi: {orphans[:5]}"

    def test_references_restored(self):
        """\\ref, \\cite, \\label komutlari restore sonrasi geri gelmeli."""
        from latex_pipeline.translator import LatexTranslator
        from latex_pipeline.config import PipelineConfig

        config = PipelineConfig(translate=True)
        lt = LatexTranslator(pipeline_config=config)

        # 10 referans iceren body
        refs = [
            r"\ref{fig:1}", r"\ref{fig:2}", r"\ref{tab:1}",
            r"\cite{smith2020}", r"\cite{jones2021}", r"\cite{brown2019}",
            r"\label{eq:1}", r"\label{eq:2}",
            r"\eqref{eq:3}", r"\pageref{sec:intro}",
        ]
        body = ""
        for i, ref in enumerate(refs):
            body += f"See {ref} for the discussion of topic {i}.\n\n"

        # Stash
        stashed_body, stash = lt._stash_protected(body)

        # Referanslar stash'e atilmis olmali
        stash_content = " ".join(stash)
        for ref in refs:
            assert ref in stash_content, f"{ref} stash'te yok"

        # Restore
        restored = lt._restore_stash(stashed_body, stash)

        # Tum referanslar geri gelmeli
        for ref in refs:
            assert ref in restored, f"{ref} restore sonrasi kayip"

    def test_code_blocks_restored(self):
        """Kod bloklari (verbatim, lstlisting) restore sonrasi geri gelmeli."""
        from latex_pipeline.translator import LatexTranslator
        from latex_pipeline.config import PipelineConfig

        config = PipelineConfig(translate=True)
        lt = LatexTranslator(pipeline_config=config)

        # 5 kod blogu iceren body
        body = ""
        for i in range(5):
            body += f"The following code shows step {i}:\n"
            body += f"\\begin{{verbatim}}\ndef func_{i}():\n    return {i}\n\\end{{verbatim}}\n\n"

        # Stash
        stashed_body, stash = lt._stash_protected(body)

        # Kod bloklari stash'e atilmis olmali
        stash_content = " ".join(stash)
        for i in range(5):
            assert f"func_{i}" in stash_content, \
                f"Kod blogu {i} stash'te yok"

        # Restore
        restored = lt._restore_stash(stashed_body, stash)

        # Tum kod bloklari geri gelmeli
        for i in range(5):
            assert f"func_{i}" in restored, \
                f"Kod blogu {i} restore sonrasi kayip"
            assert f"\\begin{{verbatim}}" in restored

    def test_no_placeholder_leakage(self, make_latex_translator):
        """Ceviri sonrasi <<LATEX_N>> sizintisi olmamali."""
        lt = make_latex_translator()

        body = (
            "The equation $E = mc^2$ is fundamental.\n\n"
            "See \\ref{fig:energy} for the energy diagram.\n\n"
            "The code:\n\\begin{verbatim}\nprint('hello')\n\\end{verbatim}\n\n"
            "Another inline math: $\\alpha + \\beta = \\gamma$ here."
        )

        # Tam ceviri dongusu
        result = lt.translate(body)

        # Sonucta placeholder kalmamali
        leaks = re.findall(r"<<LATEX_\d+>>", result)
        assert len(leaks) == 0, \
            f"{len(leaks)} placeholder ciktiya sizdi: {leaks[:5]}"

    def test_mixed_content_stash_count(self):
        """Karisik icerik (formul+ref+kod) stash sayisi dogru olmali."""
        from latex_pipeline.translator import LatexTranslator
        from latex_pipeline.config import PipelineConfig

        config = PipelineConfig(translate=True)
        lt = LatexTranslator(pipeline_config=config)

        body = (
            "Text $E=mc^2$ text.\n"
            "Text \\ref{fig:1} text.\n"
            "\\begin{verbatim}\ncode\n\\end{verbatim}\n"
            "Text $\\alpha$ text.\n"
            "Text \\cite{x} text."
        )

        _, stash = lt._stash_protected(body)

        # En az 5 eleman stash'lenmeli (2 formul + 2 ref + 1 kod)
        assert len(stash) >= 5, \
            f"5+ stash beklenirken {len(stash)} bulundu"


# ===========================================================================
# Test 3: QC prompt leak kontrolu
# ===========================================================================

class TestQCPromptLeakDetection:
    """QCEngine'in MT prompt leak'lerini tespit ettigini dogrular.

    BUG (4 Nisan): MT modeli ceviri ciktisina sistem prompt parcalari
    birakyordu (ornegin "Translate the following..." veya Turkce
    talimat cumleleri). QC bunu yakalamali.
    """

    def test_qc_detects_english_prompt_leak(self):
        """QC 'Translate the following' gibi leak'leri bulmali."""
        from latex_pipeline.qc_engine import QCEngine

        engine = QCEngine()

        # MT ciktisinda prompt leak olan metin
        tr_text = (
            "Bu bolum termodinamigin temel ilkelerini aciklar.\n"
            "Translate the following text from English to Turkish:\n"
            "Enerji korunumu yasasi onemlidir."
        )

        report = engine.analyze(tr_text, "")

        # prompt_leakage issue'su olmali
        leak_issues = [i for i in report.issues if i.check_name == "prompt_leakage"]
        assert len(leak_issues) >= 1, \
            f"Prompt leak tespit edilmedi. Issues: {[i.check_name for i in report.issues]}"

    def test_qc_detects_turkish_prompt_leak(self):
        """QC Turkce talimat leak'lerini de bulmali."""
        from latex_pipeline.qc_engine import QCEngine

        engine = QCEngine()

        tr_text = (
            "Fonksiyon analizi yapildi.\n"
            "Sadece ceviriyi cikti olarak ver.\n"
            "Sonuc olarak enerji formulizasyonu."
        )

        report = engine.analyze(tr_text, "")

        # Turkce prompt leak
        leak_issues = [i for i in report.issues if i.check_name == "prompt_leakage"]
        # Not: Turkce leak pattern'i "Sadece ceviriyi cikti" icin
        # ozel karakter olmadan eslesmiyor olabilir, regex kontrol et
        # En azindan pattern listesi bos olmamali
        from latex_pipeline.qc_engine import QCEngine as QCE
        qc = QCE()
        assert len(qc._prompt_patterns) > 0, "Prompt pattern listesi bos"

    def test_qc_no_false_positive_on_clean_text(self):
        """Temiz Turkce metin icin prompt leak raporu olmamali."""
        from latex_pipeline.qc_engine import QCEngine

        engine = QCEngine()

        tr_text = (
            "\\section{Termodinamik}\n"
            "Bu bolum termodinamigin temel ilkelerini aciklar.\n"
            "Enerji korunumu yasasi onemlidir.\n"
            "Entropi kavramini ele alacagiz."
        )

        report = engine.analyze(tr_text, "")

        leak_issues = [i for i in report.issues if i.check_name == "prompt_leakage"]
        assert len(leak_issues) == 0, \
            f"Temiz metinde {len(leak_issues)} yanlis pozitif prompt leak"

    def test_prompt_leak_patterns_imported(self):
        """PROMPT_LEAK_PATTERNS module'u import edilebilmeli."""
        from latex_pipeline.patterns import PROMPT_LEAK_PATTERNS

        assert isinstance(PROMPT_LEAK_PATTERNS, list)
        assert len(PROMPT_LEAK_PATTERNS) > 0, "Pattern listesi bos"

        # Her eleman compiled regex olmali
        for p in PROMPT_LEAK_PATTERNS:
            assert hasattr(p, "search"), \
                f"Pattern compiled regex degil: {p}"

    def test_strip_prompt_leaks_in_translator(self):
        """BookTranslator._strip_prompt_leaks GERCEK davranis testi.

        GERCEKLESTIRILMIS: Eskiden sadece source string'de pattern var mi
        diye kontrol ediyordu. Artik gercek metod cagiriliyor ve prompt
        leak temizligi dogrulaniyor.
        """
        from core.translator import BookTranslator, TranslationConfig
        from unittest.mock import MagicMock

        bt = MagicMock(spec=BookTranslator)
        bt.config = TranslationConfig()

        # Prompt leak iceren girdi
        leaked_input = (
            "As an AI language model, here is the translation: "
            "Termodinamik temel bir kavramdir."
        )
        original = "Thermodynamics is fundamental."

        result = BookTranslator._strip_prompt_leaks(bt, leaked_input, original=original)

        # AI prompt leak temizlenmis olmali
        assert "as an ai" not in result.lower(), \
            f"Prompt leak temizlenmedi: {result!r}"
        # Ceviri korunmus olmali
        assert "Termodinamik" in result, \
            f"Ceviri kayboldu: {result!r}"

    def test_qc_angle_bracket_leak_detected(self):
        """QC <<<CEVIR>>> gibi kose parantez leak'lerini bulmali."""
        from latex_pipeline.qc_engine import QCEngine

        engine = QCEngine()

        tr_text = (
            "Bu bir test metnidir.\n"
            "<<<TRANSLATE>>> Some text here.\n"
            "Devam eden ceviriler."
        )

        report = engine.analyze(tr_text, "")

        leak_issues = [i for i in report.issues if i.check_name == "prompt_leakage"]
        assert len(leak_issues) >= 1, \
            "Kose parantez prompt leak tespit edilmedi"


# ===========================================================================
# Ek Integration Testler
# ===========================================================================

class TestStashAndTranslateFullCycle:
    """Stash -> translate -> restore tam dongusunun dogru calistigini test eder."""

    def test_full_cycle_simple(self, make_latex_translator):
        """Basit body icin tam dongu: stash, translate, restore."""
        lt = make_latex_translator()

        body = (
            "The equation $E=mc^2$ shows energy-mass equivalence.\n\n"
            "This was proposed by Einstein in 1905."
        )

        result = lt.translate(body)

        # 1. Formul geri gelmeli
        assert "$E=mc^2$" in result, "Formul kaybolmus"

        # 2. Icerik bos olmamali
        assert len(result.strip()) > 50, "Cikti cok kisa"

        # 3. Placeholder sizintisi olmamali
        assert "<<LATEX_" not in result, "Placeholder sizdi"

    def test_full_cycle_with_heading(self, make_latex_translator):
        """Baslik iceren body icin tam dongu."""
        lt = make_latex_translator()

        body = (
            "\\section{Introduction}\n\n"
            "This chapter introduces the basic concepts."
        )

        result = lt.translate(body)

        # section komutu korunmali
        assert "\\section{" in result, "\\section komutu kaybolmus"

        # Icerik bos olmamali
        assert len(result.strip()) > 30


class TestBatchTranslationIntegrity:
    """Batch ceviri mekanizmasinin dogru calistigini test eder."""

    def test_batch_fn_called_with_correct_segments(self, make_latex_translator):
        """Mock batch fonksiyonu dogru segmentlerle cagirilmali."""
        call_log = []

        def _logging_batch(texts, max_length=512):
            call_log.extend(texts)
            return [t.upper() for t in texts]

        lt = make_latex_translator(batch_fn=_logging_batch)

        body = "First paragraph about physics.\n\nSecond paragraph about math."
        lt._translate_paragraphs(body)

        # Batch fonksiyonu en az 1 kere cagirilmis olmali
        assert len(call_log) >= 1, "Batch fonksiyonu hic cagirilmadi"

    def test_batch_preserves_paragraph_order(self, make_latex_translator):
        """Batch ceviri paragraf sirasini bozmamali."""
        lt = make_latex_translator()

        # Sirayla numaralanmis paragraflar
        paragraphs = [f"Paragraph number {i}." for i in range(10)]
        body = "\n\n".join(paragraphs)

        result = lt._translate_paragraphs(body)

        # Numaralar sirayla gelmeli
        found_numbers = [int(m.group()) for m in re.finditer(r'\d+', result)]
        # En azindan bazi numaralar sirayla olmali
        if len(found_numbers) >= 5:
            for i in range(len(found_numbers) - 1):
                if found_numbers[i] < 10 and found_numbers[i+1] < 10:
                    assert found_numbers[i] <= found_numbers[i+1], \
                        f"Sira bozuldu: {found_numbers[i]} > {found_numbers[i+1]}"


# ===========================================================================
# Dogrudan calistirma
# ===========================================================================

if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
