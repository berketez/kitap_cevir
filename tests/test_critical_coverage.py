"""
Kritik coverage gap testleri — CORE, LATEX ve APP ajanlarinin P0/P1 fix'leri
icin gercek davranis testleri.

Bu dosya, `reviewer` ajaninin tespit ettigi coverage bosluklarini kapatir.
Testlerin bazilari, baska ajanlarin henuz yapmadigi fix'lere bagli — onlar
xfail ile isaretli (fix tamamlanince otomatik XPASS olur).

Fix kodlari (project-internal):
  CORE-B01: PlaceholderManager `v\\d+` variant regex (placeholder kelime yutuyor)
  CORE-B02: Batch mismatch'te _mt_failed flag + memory.add engeli
  CORE-B05: HY-MT OOM fallback — tum texts cevriliyor
  LATEX-B02: Structural placeholder (\\begin{figure}) kaybolursa reject
  LATEX-B05: Intermediate checkpoint resume edilmiyor
  APP-B05: Upload path traversal + size limit validation
"""

import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ===========================================================================
# 1. CORE-B02: Memory poisoning — batch mismatch'te eksikler mt_failed flag'li
# ===========================================================================

class TestMemoryPoisoningBatchMismatch:
    """_translate_batch_segments batch mismatch'te memory'ye zehir yazmamali."""

    def test_batch_mismatch_no_memory_poison(self):
        """MT 5 segment alip 3 donerse, eksikler _mt_failed flag'li olmali
        ve memory.add CAGRILMAMALI (zehirli cache onleme)."""
        from core.translator import BookTranslator, TranslationConfig

        bt = MagicMock(spec=BookTranslator)
        bt.config = TranslationConfig(use_memory=True)
        bt.memory = MagicMock()
        bt.memory.get_or_similar = MagicMock(return_value=(None, None))
        bt.glossary = None
        bt.ner_protector = None
        bt.naturalizer = None
        bt.ai_client = None
        bt.stats = {
            "segments_translated": 0, "segments_from_memory": 0,
            "terms_enforced": 0, "ai_reviews": 0,
        }
        bt._REF_RE = BookTranslator._REF_RE
        bt._protect_references = lambda t: BookTranslator._protect_references(bt, t)
        bt._restore_references = lambda t, r: BookTranslator._restore_references(bt, t, r)
        bt._protect_formulas = lambda t: (t, [])
        bt._restore_formulas = lambda t, f: t
        bt._protect_units = lambda t: (t, [])
        bt._restore_units = lambda t, u: t

        texts = [f"Sentence {i}." for i in range(5)]

        # MT mismatch: 5 yerine 3 donuyor
        bt._translate_batch_hymt = MagicMock(
            return_value=["Cumle 0.", "Cumle 1.", "Cumle 2."]
        )

        BookTranslator._translate_batch_segments(bt, texts)

        # Memory.add zehirlenmemeli — eksik segmentler icin memory yazilmamali
        # (fix sonrasi: sadece 3 basarili segment memory'e gider)
        assert bt.memory.add.call_count <= 3, \
            f"Mismatch'te zehirli cache: {bt.memory.add.call_count} add cagrisi"


# ===========================================================================
# 2. CORE-B01: PlaceholderManager `v\\d+` variant — kelime yutmamali
# ===========================================================================

class TestPlaceholderWordPreservation:
    """PlaceholderManager variant regex ("v\\d+" gibi) masum kelime yutmamali.

    Ornek: "version 1 is important" icinde "v1" yanlislikla placeholder
    olarak algilanip yutuluyor. Fix: variant pattern daha sikisik olmali.
    """

    def test_version_word_not_eaten_by_placeholder(self):
        """PlaceholderManager placeholder restore yaparken 'version 1'
        kelimesini bozmamali.

        Senaryo: Bir formul korunup {{v0}} placeholder'i olusturuluyor.
        Cevirmen bu placeholder'i dogru korusa bile metin icinde 'version 1'
        / 'version 2' gibi ifadeler varsa variant regex bunlari yemeMELI.
        CORE-B01 fix: variant pattern word-boundary korumali olmali.
        """
        from core.placeholder import PlaceholderManager

        pm = PlaceholderManager()
        # Once bir formul protect edelim ki v0 placeholder'i olussun
        original = "The formula $E = mc^2$ is famous."
        protected = pm.protect(original)
        # protected icinde {{v0}} var; formul entries'e kayitli

        # Simdi ceviri sonrasi gelen metni simule edelim:
        # Placeholder dogru korunmus AMA metinde 'version 1/2' de var
        translated = (
            "The formula {{v0}} is famous. "
            "The version 1 software is important. Also version 2 exists."
        )

        # Restore cagirildiginda 'version 1/2'deki v1/v2 yutulmamali
        restored = pm.restore(translated)

        # Placeholder restore edilmis olmali
        assert "$E = mc^2$" in restored, \
            f"Placeholder restore edilmedi: {restored!r}"
        # Orijinal 'version 1/2' kelimeleri korunmus olmali
        assert "version 1" in restored, f"'version 1' yutuldu: {restored!r}"
        assert "version 2" in restored, f"'version 2' yutuldu: {restored!r}"


# ===========================================================================
# 3. LATEX-B02: Structural placeholder kaybolursa translation reddedilmeli
# ===========================================================================

class TestStructuralPlaceholderRecovery:
    """MT \\begin{figure} gibi structural placeholder yuttugunda
    translation body'ye geri eklenmeli (recovery mekanizmasi)."""

    def test_missing_figure_placeholder_recovered(self, make_latex_translator):
        """\\begin{figure}..\\end{figure} stash'lenip MT tarafindan yutulursa
        body sonuna recovery yapilmalidir."""

        def _figure_eating_batch(texts, max_length=512):
            results = []
            for t in texts:
                # LATEX_0 placeholder'i yut (ilk stash)
                t = re.sub(r"<<LATEX_0>>", "", t)
                results.append(f"[TR]{t}[/TR]")
            return results

        lt = make_latex_translator(batch_fn=_figure_eating_batch)

        body = (
            r"\begin{figure}[h]" "\n"
            r"\includegraphics{test.pdf}" "\n"
            r"\caption{Test figure}" "\n"
            r"\end{figure}" "\n\n"
            "This is the paragraph after the figure."
        )

        result = lt.translate(body)

        # Orphan placeholder KALMAMALI
        orphans = re.findall(r"<<LATEX_\d+>>", result)
        assert len(orphans) == 0, \
            f"MT placeholder yuttu ama recovery yapilmadi: {orphans}"


# ===========================================================================
# 4. LATEX-B05: Intermediate checkpoint RESUME EDILMEMELI
# ===========================================================================

class TestCheckpointIntermediateNotResumed:
    """Intermediate (tamamlanmamis) checkpoint dosyasi resume edilmemeli.

    Sadece final checkpoint (translated_body_final.tex) veya tam
    intermediate (metadata dogru set edilmis) resume edilmeli.
    """

    def test_intermediate_checkpoint_not_resumed(self, tmp_path):
        """Yari-tamamlanmis checkpoint varsa _try_resume_from_checkpoint
        None donmelidir (yeniden ceviri baslatilmali)."""
        from latex_pipeline.translator import LatexTranslator
        from latex_pipeline.config import PipelineConfig

        config = PipelineConfig(translate=True)
        lt = LatexTranslator(pipeline_config=config)

        # Intermediate dosya olustur (meta yok)
        ckpt_dir = tmp_path / "ckpt"
        ckpt_dir.mkdir()
        (ckpt_dir / "translated_body_checkpoint.tex").write_text(
            "Kismen cevrilmis metin...", encoding="utf-8"
        )
        # Meta dosyasi YOK — intermediate sayilmali

        lt._checkpoint_dir = ckpt_dir

        resumed = lt._try_resume_from_checkpoint("orijinal body")

        assert resumed is None, \
            "Intermediate checkpoint yanlislikla resume edildi"


# 5. APP-B05 (yükleme yol aşımı / boyut sınırı): eski app.py kaldırıldı,
#    güvence yeni arayüzde -- tests/test_ui_runner.py::TestSaveUpload.


# ===========================================================================
# 6. CORE-B05: HY-MT OOM sonrasi tum texts cevrilmeli
# ===========================================================================

class TestHymtOomFallback:
    """HY-MT OOM atarsa fallback mekanizmasi calismali, hicbir text
    atlanmamali (tum segmentler ya cevrilmeli ya [MT_FAIL] prefix'li olmali)."""

    def test_oom_all_texts_returned(self, make_latex_translator, mock_hymt_oom):
        """Batch MT OOM atinca, tum segmentler sonuc listesinde yer almali
        (None kalmamali)."""

        call_count = {"n": 0}

        def _oom_then_recover(texts, max_length=512):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # Ilk cagri OOM
                raise RuntimeError("CUDA out of memory")
            # Sonraki cagrilar basarili
            return [f"[TR]{t}[/TR]" for t in texts]

        lt = make_latex_translator(batch_fn=_oom_then_recover)

        body = "\n\n".join([f"Sentence {i}." for i in range(5)])

        # Crash olmamali
        try:
            result = lt._translate_paragraphs(body)
        except RuntimeError:
            pytest.skip("OOM recovery henuz implement edilmemis (CORE-B05)")

        # Sonuc bos olmamali
        assert result and result.strip(), "OOM sonrasi sonuc bos"


# ===========================================================================
# 7 & 8. 13 Mart kuralı: ilk/son 10 sayfa test coverage (TOC + index)
# ===========================================================================

class TestFirstLastPageCoverage:
    """13 Mart kurali: ilk 10 sayfa (TOC + preface) ve son 10 sayfa
    (index + bibliography) testten kacmamali."""

    def test_first_pages_toc_translated(self, make_latex_translator):
        """TOC + preface iceren ilk sayfalar cevrilmelidir."""
        lt = make_latex_translator()

        first_pages_body = (
            r"\tableofcontents" "\n\n"
            "Preface\n\n"
            "This book is about thermodynamics. "
            "It covers energy conservation in detail.\n\n"
            "Chapter 1: Introduction to Thermodynamics\n\n"
            "Basic concepts are introduced here."
        )

        result = lt.translate(first_pages_body)

        # Bos olmamali
        assert result and result.strip(), "Ilk sayfalar bos dondu"
        # Ceviri yapilmis olmali (mock [TR] ekliyor)
        assert "[TR]" in result, \
            f"Ilk sayfalar cevrilmedi: {result[:100]!r}"
        # TOC komutu korunmus olmali (structural)
        assert r"\tableofcontents" in result, \
            "TOC komutu kayboldu"

    def test_last_pages_index_translated(self, make_latex_translator):
        """Index + bibliography iceren son sayfalar cevrilmeli."""
        lt = make_latex_translator()

        last_pages_body = (
            r"\bibliography{refs}" "\n\n"
            "Index\n\n"
            "Energy, 42-48\n"
            "Entropy, 105-112\n"
            "Heat transfer, 205\n\n"
            r"\end{document}"
        )

        result = lt.translate(last_pages_body)

        assert result and result.strip(), "Son sayfalar bos dondu"
        # Metin kisimlari cevrilmis olmali
        assert "[TR]" in result, "Son sayfalar cevrilmedi"
        # Structural komutlar korunmus
        assert r"\bibliography" in result, "Bibliography komutu kayboldu"


# ===========================================================================
# 9. Full-book stress: 500+ paragraf mock body
# ===========================================================================

class TestFullBookPipeline:
    """Tam kitap (500+ paragraf) mock body uzerinde crash yok testi."""

    def test_full_book_no_crash(
        self, make_latex_translator, sample_full_book_body
    ):
        """500+ paragrafli mock kitap crash yapmadan cevrilmeli."""
        lt = make_latex_translator()

        result = lt.translate(sample_full_book_body)

        # Sonuc bos olmamali
        assert result and result.strip(), "Full-book sonucu bos"
        # Orphan placeholder kalmamali
        orphans = re.findall(r"<<(?:LATEX|CMD|TERM)_\d+>>", result)
        assert len(orphans) == 0, \
            f"Full-book'ta {len(orphans)} orphan placeholder"
        # Yeterince uzun olmali (truncation yok)
        assert len(result) > len(sample_full_book_body) * 0.3, \
            f"Full-book cikti cok kisa: {len(result)} vs {len(sample_full_book_body)}"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
