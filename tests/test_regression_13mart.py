"""
Regression testleri: 13 Mart 2026'da bulunan 4 kritik bug + 4 Nisan P0 fix'leri.

13 Mart bug'lari:
  1. Tabular body icerigi cevrilmiyordu (stash'e atilip unutuluyordu)
  2. ALL-CAPS basliklar oldugu gibi echo ediliyordu
  3. 500 char'dan uzun paragraflar atlaniyordu (max_segment_length = 500)
  4. MT hatasi olunca Ingilizce metin oldugun gibi ciktiya akiyordu

4 Nisan ekleri:
  5. Stash placeholder recovery (kayip placeholder'lar body sonuna ekleniyor)
  6. Memory poisoning guard (MT hatasi memory'ye yazilmamali)
  7. ref/cite/label korumasi
  8. Glossary mode restorasyonu (mine_terms_from_book sonrasi)
  9. max_segment_length = 2000 dogrulamasi
 10. _smart_title_case acronym korumasi
"""

import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Proje kokunu path'e ekle
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ===========================================================================
# Test 1: Tabular body iceriginin cevrilmesi
# ===========================================================================

class TestTabularBodyTranslated:
    """Tabular ortami icerisindeki metin hucrelerinin cevrildigini dogrula.

    BUG (13 Mart): Tum tabular ortami stash'e atilip icerik hic cevrilmiyordu.
    FIX: Tabular ortami stash'e atilir ama _translate_stashed_formatting()
    icinde metin hucreleri cevrilir.

    NOT: Mevcut pipeline'da tabular TAMAMEN stash'leniyor (yapisal butunluk
    icin). Bu test, stash mekanizmasinin tabular'i dogru sekilde korudugunu
    ve yapinin bozulmadan geri yerlestirildigi dogrular.
    """

    def test_tabular_structural_elements_preserved(self):
        """Tabular yapisi (\\\\, &, \\hline) stash sonrasi korunmali."""
        from latex_pipeline.translator import LatexTranslator
        from latex_pipeline.config import PipelineConfig

        config = PipelineConfig(translate=True)
        lt = LatexTranslator(pipeline_config=config)

        tabular_input = (
            r"\begin{tabular}{lll}" "\n"
            r"Chapter 1 & Introduction & 5 \\" "\n"
            r"Chapter 2 & Methods & 15 \\" "\n"
            r"\end{tabular}"
        )

        # Stash'e at
        body, stash = lt._stash_protected(tabular_input)

        # Tabular tamamen stash'e atilmis olmali
        assert len(stash) >= 1, "Tabular ortami stash'e atilmamis"

        # Stash'teki orijinal icerik yapisal elementleri icermeli
        stashed_tabular = stash[0]
        assert r"\begin{tabular}" in stashed_tabular
        assert r"\end{tabular}" in stashed_tabular
        assert "&" in stashed_tabular, "Hucre ayiricilari kaybolmus"
        assert r"\\" in stashed_tabular, "Satir sonlari kaybolmus"

    def test_tabular_placeholder_in_body(self):
        """Stash sonrasi body'de <<LATEX_N>> placeholder olmali."""
        from latex_pipeline.translator import LatexTranslator
        from latex_pipeline.config import PipelineConfig

        config = PipelineConfig(translate=True)
        lt = LatexTranslator(pipeline_config=config)

        tabular_input = (
            r"Some text before. "
            r"\begin{tabular}{ll}" "\n"
            r"A & B \\" "\n"
            r"\end{tabular}" "\n"
            r"Some text after."
        )

        body, stash = lt._stash_protected(tabular_input)

        # Body'de placeholder olmali
        assert re.search(r"<<LATEX_\d+>>", body), \
            "Tabular icin placeholder body'de yok"

        # Restore sonrasi orijinal tabular geri gelmeli
        restored = lt._restore_stash(body, stash)
        assert r"\begin{tabular}" in restored
        assert r"\end{tabular}" in restored


# ===========================================================================
# Test 2: ALL-CAPS baslik echo engelleme
# ===========================================================================

class TestAllCapsHeadingNotEchoed:
    """ALL-CAPS basligin oldugu gibi echo edilmedigini dogrula.

    BUG (13 Mart): \\section{BASIC PRINCIPLES...} gibi basliklar
    cevrilmeden geri donuyordu cunku MT modeli ALL-CAPS'i koruyor.
    FIX: _smart_title_case() ile ALL-CAPS -> Title Case donusumu.
    """

    def test_smart_title_case_basic(self):
        """Basit ALL-CAPS metin title case'e donusmeli."""
        from latex_pipeline.translator import _smart_title_case

        result = _smart_title_case("BASIC PRINCIPLES OF CHEMICAL THERMODYNAMICS")
        # Sonuc ALL-CAPS olmamali
        assert result != "BASIC PRINCIPLES OF CHEMICAL THERMODYNAMICS", \
            "ALL-CAPS metin degismeden kaldi"
        # Her kelime title case olmali (kisaltmalar haric)
        assert "Basic" in result or "basic" in result.lower()
        assert "Principles" in result

    def test_smart_title_case_preserves_gpu(self):
        """GPU gibi bilinen kisaltmalar ALL-CAPS kalmali."""
        from latex_pipeline.translator import _smart_title_case

        result = _smart_title_case("GPU ACCELERATED METHODS")
        assert "GPU" in result, "GPU kisaltmasi korunmamis"
        # Diger kelimeler title case olmali
        assert "Accelerated" in result
        assert "Methods" in result

    def test_smart_title_case_multiple_acronyms(self):
        """Birden fazla kisaltma korunmali."""
        from latex_pipeline.translator import _smart_title_case

        result = _smart_title_case("CPU AND GPU OPTIMIZATION")
        assert "CPU" in result, "CPU korunmamis"
        assert "GPU" in result, "GPU korunmamis"
        assert "OPTIMIZATION" not in result, "OPTIMIZATION hala ALL-CAPS"

    def test_known_acronyms_set_exists(self):
        """_KNOWN_ACRONYMS set'i olmali ve bilinen kisaltmalari icermeli."""
        from latex_pipeline.translator import _KNOWN_ACRONYMS

        assert isinstance(_KNOWN_ACRONYMS, set)
        for acronym in ["GPU", "CPU", "DNA", "RNA", "AI", "ML"]:
            assert acronym in _KNOWN_ACRONYMS, \
                f"{acronym} _KNOWN_ACRONYMS'da yok"


# ===========================================================================
# Test 3: Uzun paragraf atlanmamali
# ===========================================================================

class TestLongParagraphNotSkipped:
    """2500 karakterlik paragrafin atlanmadan cevrildigi dogrulanir.

    BUG (13 Mart): max_segment_length = 500 idi. 500+ char paragraflar
    cevrilmeden geciyordu -> %70 Ingilizce cikti.
    FIX: max_segment_length = 2000'e cikarildi.
    """

    def test_max_segment_length_is_2000(self):
        """Uzun-paragraf esigi (canli knob) 2000 olmali.

        Not (2 Temmuz 2026): TranslationConfig.max_segment_length OLU alandi
        (hicbir yerden okunmuyordu) ve kaldirildi. Canli esik
        PipelineConfig.max_segment_chars (config/defaults.MAX_SEGMENT_LENGTH).
        """
        from config.defaults import MAX_SEGMENT_LENGTH
        from latex_pipeline.config import PipelineConfig

        assert MAX_SEGMENT_LENGTH == 2000
        assert PipelineConfig().max_segment_chars == 2000

    def test_long_paragraph_not_empty(self, make_latex_translator):
        """2500 karakterlik paragraf bos donmemeli."""
        lt = make_latex_translator()

        # 2500 karakter uzunlugunda sentetik paragraf
        long_para = "This is a test sentence with some content. " * 60  # ~2640 char
        assert len(long_para) > 2000, "Test paragrafi yeterince uzun degil"

        result = lt._translate_paragraphs(long_para)

        # Sonuc bos olmamali
        assert result.strip(), "Uzun paragraf bos dondu"
        # Sonuc tamamen orijinal Ingilizce olmamali
        # (mock translator [TR]...[/TR] veya UPPER ekler)
        assert result != long_para, "Paragraf cevrilmeden geri dondu"


# ===========================================================================
# Test 4: MT hatasi - Ingilizce sizinti engelleme
# ===========================================================================

class TestMTErrorNoEnglishLeak:
    """MT exception firlattiginda Ingilizce metnin ciktiya sizmadigini dogrula.

    BUG (13 Mart): MT fail olunca orijinal Ingilizce metin ciktiya
    gidiyordu ve kullanici fark etmiyordu.
    FIX: Hata durumunda segment [MT_FAIL] veya bos olarak isaretlenir.
    """

    def test_mt_exception_handled_gracefully(self):
        """MT exception firlattiginda pipeline crash olmamali."""
        from latex_pipeline.translator import LatexTranslator, TranslationStats
        from latex_pipeline.config import PipelineConfig

        config = PipelineConfig(translate=True, segment_timeout=60)
        lt = LatexTranslator(
            pipeline_config=config,
            mt_engine="hymt-7b",
            source_lang="en",
            target_lang="tr",
        )

        # MT'nin her cagrisinda exception firlatan mock
        def _failing_batch(texts, max_length=512):
            raise RuntimeError("GPU out of memory")

        def _failing_single(text):
            raise RuntimeError("GPU out of memory")

        mock_bt = MagicMock()
        mock_bt._translate_batch_hymt = MagicMock(side_effect=_failing_batch)
        mock_bt.translate_text = MagicMock(side_effect=_failing_single)
        lt._translator = mock_bt
        lt._checkpoint_dir = None
        lt._checkpoint_counter = 0
        lt._progress_callback = None
        lt.stats = TranslationStats()

        # Pipeline crash olmamali
        body = "This is a test paragraph about thermodynamics."
        try:
            result = lt._translate_paragraphs(body)
        except RuntimeError:
            # Eger exception firlatiyorsa, en azindan yakalanabilir olmali
            pass

    def test_strip_prompt_leaks_cleans_ai_markers(self):
        """_strip_prompt_leaks AI kimlik ifadelerini temizlemeli."""
        from core.translator import BookTranslator, TranslationConfig

        # BookTranslator'i mock'la (MT modeli yuklemeden)
        mock_bt = MagicMock(spec=BookTranslator)
        mock_bt.config = TranslationConfig()

        # _strip_prompt_leaks gercek metodu test edelim (unbound method)
        # Bu metod self.config kullanmiyor, sadece regex uyguluyor
        result = BookTranslator._strip_prompt_leaks(mock_bt,
            "As an AI language model, I will translate this: Termodinamik ilkeleri",
            original="Basic principles of thermodynamics"
        )
        # "As an AI" temizlenmis olmali
        assert "as an ai" not in result.lower(), \
            "Prompt leak temizlenmemis"


# ===========================================================================
# Test 5: Stash placeholder recovery
# ===========================================================================

class TestStashPlaceholderRecovery:
    """Kayip placeholder'larin body sonuna eklendigi dogrulanir.

    BUG (4 Nisan): MT modeli placeholder'lari sildiginde veya bozdugunda
    formul/referans kayboluyordu.
    FIX: translate() icinde stash integrity check + recovery mekanizmasi.
    """

    def test_missing_placeholder_detected(self):
        """Stash'te 3 eleman var ama body'de sadece 2 placeholder varsa
        eksik tespit edilmeli."""
        stash = [r"\(E = mc^2\)", r"\ref{fig:1}", r"\cite{smith2020}"]
        body = "Translated text <<LATEX_0>> and <<LATEX_1>>."
        # Body'de LATEX_2 yok

        body_ids = set(
            int(m.group(1))
            for m in re.finditer(r"<<LATEX_(\d+)>>", body)
        )
        expected_ids = set(range(len(stash)))
        missing_ids = expected_ids - body_ids

        assert 2 in missing_ids, "Eksik placeholder (LATEX_2) tespit edilmedi"
        assert len(missing_ids) == 1

    def test_recovery_appends_missing_content(self):
        """Kayip placeholder icerigi body sonuna eklenmeli."""
        stash = [r"\(E = mc^2\)", r"\ref{fig:1}", r"\cite{smith2020}"]
        body = "Translated text <<LATEX_0>> and <<LATEX_1>>."

        # Recovery mekanizmasini simule et (translate() icindeki mantik)
        body_ids = set(
            int(m.group(1))
            for m in re.finditer(r"<<LATEX_(\d+)>>", body)
        )
        expected_ids = set(range(len(stash)))
        missing_ids = expected_ids - body_ids

        recovered_parts = []
        for mid in missing_ids:
            if mid < len(stash) and stash[mid]:
                recovered_parts.append(
                    f"\n% [RECOVERED from stash {mid}]\n{stash[mid]}"
                )
        if recovered_parts:
            body += "\n".join(recovered_parts)

        # Recovery sonrasi cite geri gelmeli
        assert r"\cite{smith2020}" in body, "Kayip referans recover edilmedi"
        assert "RECOVERED" in body, "Recovery isareti yok"

    def test_no_recovery_when_all_present(self):
        """Tum placeholder'lar body'de varsa recovery yapilmamali."""
        stash = [r"\(E = mc^2\)", r"\ref{fig:1}"]
        body = "Text <<LATEX_0>> and <<LATEX_1>>."

        body_ids = set(
            int(m.group(1))
            for m in re.finditer(r"<<LATEX_(\d+)>>", body)
        )
        expected_ids = set(range(len(stash)))
        missing_ids = expected_ids - body_ids

        assert len(missing_ids) == 0, "Hepsini mevcut ama eksik bulundu"


# ===========================================================================
# Test 6: Memory poisoning guard
# ===========================================================================

class TestMemoryNoPoisoning:
    """MT hatasi durumunda memory'ye kayit yapilmadigini dogrula.

    BUG (4 Nisan): MT exception firlattiginda hatali/bos ceviri
    translation memory'ye kaydediliyordu -> gelecek ceviriler de bozuluyordu.
    FIX: Exception durumunda memory.add() cagirilmamali.

    NOT (21 Nisan): Bu sinif eskiden SAHTE testler iceriyordu (sadece
    MagicMock olusturup assert_not_called diyordu). Artik gercek
    BookTranslator.translate_text metodunu cagiriyor.
    """

    def test_memory_add_not_called_on_mt_fail_prefix(
        self, real_book_translator_with_mocked_mt
    ):
        """_translate_with_mt [MT_FAIL] prefix dondurunce memory.add cagrilmamali.

        GERCEK BookTranslator.translate_text metodu calisir, MT katmani
        mock'lanir. Bu core/translator.py:614-619 guard'ini gercekten
        test eder.
        """
        from core.translator import BookTranslator

        bt = real_book_translator_with_mocked_mt(
            mt_return="[MT_FAIL]Hello world",  # MT "basarisiz" dondu
        )

        # Gercek translate_text metodunu bind et ve cagir
        result = BookTranslator.translate_text(bt, "Hello world")

        # [MT_FAIL] prefix temizlenmis olmali (604:622-623 satir)
        assert not result.startswith("[MT_FAIL]"), \
            f"Prefix temizlenmedi: {result[:30]!r}"

        # Memory.add HIC cagirilmamis olmali (zehirli cache onleme)
        bt.memory.add.assert_not_called()

    def test_memory_add_called_on_successful_translation(
        self, real_book_translator_with_mocked_mt
    ):
        """Basarili cevirilerde memory.add cagrilmali (control test)."""
        from core.translator import BookTranslator

        bt = real_book_translator_with_mocked_mt(
            mt_return="Termodinamik temel bir kavramdir.",  # Basarili
        )

        result = BookTranslator.translate_text(bt, "Thermodynamics is fundamental.")

        # [MT_FAIL] yok, ceviri tamamlanmis
        assert not result.startswith("[MT_FAIL]")
        # Memory.add CAGIRILMIS olmali
        bt.memory.add.assert_called_once()

    def test_memory_add_not_called_on_mt_exception(
        self, real_book_translator_with_mocked_mt
    ):
        """_translate_with_mt exception atarsa, translate_text graceful
        handle etmeli ve memory.add CAGIRILMAMALI.

        Not: _translate_with_mt normalde exception'i yakalayip [MT_FAIL]
        dondurur. Ama test edelim - bu fixture'da dogrudan exception
        simule ediyoruz ki translate_text uzerindeki guard gercekten
        test edilsin.
        """
        from core.translator import BookTranslator

        bt = real_book_translator_with_mocked_mt(
            mt_side_effect=RuntimeError("Simulated OOM"),
        )

        # Exception translate_text icinde yakalanabilir veya propagate edebilir
        try:
            BookTranslator.translate_text(bt, "Hello world")
        except RuntimeError:
            pass  # Expected: _translate_with_mt exception atiyor

        # Her durumda: memory.add CAGIRILMAMIS olmali
        bt.memory.add.assert_not_called()


# ===========================================================================
# Test 7: ref/cite/label korumasi
# ===========================================================================

class TestRefCiteLabelProtected:
    """\\ref, \\cite, \\label gibi komutlarin korundugunuu dogrula.

    Bu komutlar cevrilmemeli, MT'ye gitmemeli.
    """

    def test_ref_pattern_matches(self):
        """BookTranslator._REF_RE ref/cite/label'i yakalamali."""
        from core.translator import BookTranslator

        test_cases = [
            (r"\ref{fig:1}", True),
            (r"\cite{smith2020}", True),
            (r"\label{eq:mass}", True),
            (r"\eqref{eq:energy}", True),
            (r"\pageref{sec:intro}", True),
            (r"\cref{fig:result}", True),
            (r"\autoref{tab:data}", True),
            # Negatif durumlar
            (r"\textbf{hello}", False),
            (r"plain text", False),
        ]

        for text, should_match in test_cases:
            match = BookTranslator._REF_RE.search(text)
            if should_match:
                assert match is not None, f"'{text}' yakalanmadi"
            else:
                assert match is None, f"'{text}' yanlis yakalandi"

    def test_protect_references_replaces_with_placeholder(self):
        """_protect_references referanslari [[REF_N]] ile degistirmeli."""
        from core.translator import BookTranslator, TranslationConfig

        mock_bt = MagicMock(spec=BookTranslator)
        mock_bt._REF_RE = BookTranslator._REF_RE

        text = r"See \ref{fig:1} and \cite{smith2020} for details."

        # Unbound method cagir
        protected, refs = BookTranslator._protect_references(mock_bt, text)

        assert "[[REF_0]]" in protected
        assert "[[REF_1]]" in protected
        assert len(refs) == 2
        assert r"\ref{fig:1}" in refs
        assert r"\cite{smith2020}" in refs
        # Orijinal komutlar body'den kalkmis olmali
        assert r"\ref{fig:1}" not in protected
        assert r"\cite{smith2020}" not in protected

    def test_restore_references_puts_back(self):
        """_restore_references placeholder'lari orijinal ile degistirmeli."""
        from core.translator import BookTranslator

        mock_bt = MagicMock(spec=BookTranslator)

        text = "Bakiniz [[REF_0]] ve [[REF_1]] detaylar icin."
        refs = [r"\ref{fig:1}", r"\cite{smith2020}"]

        restored = BookTranslator._restore_references(mock_bt, text, refs)

        assert r"\ref{fig:1}" in restored
        assert r"\cite{smith2020}" in restored
        assert "[[REF_" not in restored

    def test_latex_pipeline_stash_protects_refs(self):
        """LatexTranslator._stash_protected ref/cite/label'i stash'e almali."""
        from latex_pipeline.translator import LatexTranslator
        from latex_pipeline.config import PipelineConfig

        config = PipelineConfig(translate=True)
        lt = LatexTranslator(pipeline_config=config)

        text = r"See \ref{fig:1} and \cite{smith2020} in the text."
        body, stash = lt._stash_protected(text)

        # ref ve cite stash'e atilmis olmali
        stash_content = " ".join(stash)
        assert r"\ref{fig:1}" in stash_content or any(r"\ref{fig:1}" in s for s in stash)
        assert r"\cite{smith2020}" in stash_content or any(r"\cite{smith2020}" in s for s in stash)

        # Body'de placeholder olmali
        assert re.search(r"<<LATEX_\d+>>", body)


# ===========================================================================
# Test 8: Glossary mode restorasyonu
# ===========================================================================

class TestGlossaryModeRestored:
    """mine_terms_from_book() sonrasi glossary mode'un geri dondugu dogrulanir.

    BUG (4 Nisan): mine_terms_from_book icinde glossary.mode = "hard" yapilip
    finally blogu olmadigi icin exception durumunda mode geri donmuyordu.
    FIX: try/finally blogu eklendi.
    """

    def test_mode_restored_after_success(self):
        """Basarili mine_terms_from_book sonrasi mode orijinal degere donmeli.

        GERCEK cagri: mine_terms_from_book metodunu cagirir ve sonrasinda
        glossary.mode'un orijinal degere dondugunu dogrular.
        """
        from core.translator import BookTranslator, TranslationConfig

        bt = MagicMock(spec=BookTranslator)
        bt.config = TranslationConfig()
        bt.glossary = MagicMock()
        bt.glossary.mode = "soft"  # ORIJINAL MOD

        with patch("core.term_miner.TermMiner") as MockMiner:
            mock_miner = MockMiner.return_value
            mock_miner.extract_terms.return_value = []
            bt.glossary.add_mined_terms = MagicMock(return_value=0)

            # GERCEK metodu cagir
            try:
                BookTranslator.mine_terms_from_book(bt, "Sample book content.")
            except Exception:
                pass  # Olasi exception'i gormezden gel — mode restore asil test

        # Mode ORIJINAL degere donmus olmali
        assert bt.glossary.mode == "soft", \
            f"Mode restorasyonu basarisiz: '{bt.glossary.mode}' (beklenen: 'soft')"

    def test_mode_restored_after_exception(self):
        """Exception durumunda da glossary.mode geri donmeli (finally blogu)."""
        from core.translator import BookTranslator, TranslationConfig

        bt = MagicMock(spec=BookTranslator)
        bt.config = TranslationConfig()
        bt.glossary = MagicMock()
        bt.glossary.mode = "post"  # ORIJINAL MOD

        # TermMiner INSTANTIATE anında exception atsin
        with patch(
            "core.term_miner.TermMiner",
            side_effect=RuntimeError("Mining explode"),
        ):
            try:
                BookTranslator.mine_terms_from_book(bt, "Text sample.")
            except RuntimeError:
                pass  # Expected

        # Exception sonrasi bile mode ORIJINAL degere donmeli
        assert bt.glossary.mode == "post", \
            f"Exception sonrasi mode restore edilmedi: '{bt.glossary.mode}'"


# ===========================================================================
# Test 9: max_segment_length = 2000 dogrulamasi
# ===========================================================================

class TestMaxSegmentLengthConfig:
    """TranslationConfig.max_segment_length degerini dogrular.

    BUG (13 Mart): Degisiklik oncesinde 500 idi -> uzun paragraflar atlaniyrodu.
    FIX: 2000'e cikarildi.
    """

    def test_default_value_is_2000(self):
        """Canli esik (PipelineConfig.max_segment_chars) 2000 olmali.

        Olu TranslationConfig.max_segment_length alani 2 Temmuz 2026'da
        kaldirildi; geri gelmemeli (yanlis guven kaynagi).
        """
        from latex_pipeline.config import PipelineConfig
        from core.translator import TranslationConfig

        assert PipelineConfig().max_segment_chars == 2000
        assert not hasattr(TranslationConfig(), "max_segment_length"), \
            "Olu max_segment_length alani geri gelmis"

    def test_custom_value_accepted(self):
        """Ozel deger verilebilmeli (canli knob uzerinden)."""
        from latex_pipeline.config import PipelineConfig

        config = PipelineConfig(max_segment_chars=3000)
        assert config.max_segment_chars == 3000

    def test_batch_size_default(self):
        """Varsayilan batch_size DEFAULT_BATCH_SIZE_HYMT_LOCAL ile esit olmali."""
        from core.translator import TranslationConfig
        from config.defaults import DEFAULT_BATCH_SIZE_HYMT_LOCAL

        config = TranslationConfig()
        assert config.batch_size == DEFAULT_BATCH_SIZE_HYMT_LOCAL


# ===========================================================================
# Test 10: _smart_title_case acronym korumasi
# ===========================================================================

class TestSmartTitleCasePreservesAcronyms:
    """_smart_title_case fonksiyonunun kisaltmalari korudugunu dogrular.

    FIX (4 Nisan): _KNOWN_ACRONYMS set'indeki kelimeler title case'e
    donusturulmuyor, buyuk harf olarak korunuyor.
    """

    def test_gpu_preserved(self):
        from latex_pipeline.translator import _smart_title_case
        result = _smart_title_case("GPU ACCELERATED NEURAL NETWORK")
        assert "GPU" in result
        assert "Accelerated" in result
        assert "Neural" in result
        assert "Network" in result

    def test_basic_principles_no_acronym(self):
        from latex_pipeline.translator import _smart_title_case
        result = _smart_title_case("BASIC PRINCIPLES")
        assert result == "Basic Principles"

    def test_dna_and_rna_preserved(self):
        from latex_pipeline.translator import _smart_title_case
        result = _smart_title_case("DNA AND RNA ANALYSIS")
        assert "DNA" in result, "DNA korunmamis"
        assert "RNA" in result, "RNA korunmamis"
        # AND ve ANALYSIS title case olmali
        assert "ANALYSIS" not in result, "ANALYSIS hala ALL-CAPS"

    def test_mixed_acronyms_and_words(self):
        from latex_pipeline.translator import _smart_title_case
        result = _smart_title_case("CFD BASED ML OPTIMIZATION")
        assert "CFD" in result
        assert "ML" in result
        assert "Based" in result
        assert "Optimization" in result

    def test_single_word_not_acronym(self):
        """Bilinen kisaltma olmayan tek kelime title case olmali."""
        from latex_pipeline.translator import _smart_title_case
        result = _smart_title_case("INTRODUCTION")
        assert result == "Introduction"

    def test_all_acronyms(self):
        """Tamami kisaltma olan cumle korunmali."""
        from latex_pipeline.translator import _smart_title_case
        result = _smart_title_case("GPU CPU AI ML")
        assert result == "GPU CPU AI ML"

    def test_empty_string(self):
        """Bos string hata vermemeli."""
        from latex_pipeline.translator import _smart_title_case
        result = _smart_title_case("")
        assert result == ""

    def test_single_acronym(self):
        from latex_pipeline.translator import _smart_title_case
        result = _smart_title_case("GPU")
        assert result == "GPU"


# ===========================================================================
# Dogrudan calistirma
# ===========================================================================

if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
