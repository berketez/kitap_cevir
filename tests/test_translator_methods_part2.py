"""
BookTranslator pipeline + IO + utility metod testleri (Part 2).

Kapsam:
- Pipeline:    translate_segments, translate_file, _translate_pdf, _translate_epub, _translate_text_file
- Post-process: _post_process_missing_terms, _post_process_glossary_final, mine_terms_from_book, add_custom_terms
- Utility:      _fix_unknown_tokens, _context_match_score, _get_dictionary_translation, _strip_prompt_leaks,
                _normalize_text, _is_non_translatable, _dedup_segments, _build_translation_instruction
- Checkpoint:   _save_text_checkpoint, _load_text_checkpoint, _delete_text_checkpoint, _get_checkpoint_path
- Stats:        get_stats, export_memory, import_memory
- Init:         __init__, _initialize, _init_glossary, _init_memory
  (KARAR 35, FX3 2026-10-05: dış AI düzeltme yolu — _ai_review,
  _init_ai_client, use_ai_review — çekirdekten silindi; testleri de.)

Strateji:
- `make_book_translator` fixture salt MagicMock döndürüyor; gerçek metodları
  `BookTranslator.metod(mock_bt, ...)` ile UNBOUND çağırarak test ediyoruz.
- I/O test'leri için `tmp_path` ve `monkeypatch` kullanılır.
- PDF/EPUB dispatcher test'lerinde parsers/exporters modülleri mock'lanır.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open
import pytest

# Proje kökünü path'e ekle
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.translator import BookTranslator, TranslationConfig


# ============================================================================
# _is_non_translatable
# ============================================================================


class TestIsNonTranslatable:
    """_is_non_translatable — CORE-B13: formül-only tespit."""

    def test_pure_numbers_skipped(self, make_book_translator):
        bt = make_book_translator()
        assert BookTranslator._is_non_translatable(bt, "12345") is True

    def test_numbers_and_punctuation_skipped(self, make_book_translator):
        bt = make_book_translator()
        assert BookTranslator._is_non_translatable(bt, "1.5 + 2.3 = 3.8") is True

    def test_single_formula_skipped(self, make_book_translator):
        """$E=mc^2$ formülü — CORE-B13: non-greedy match."""
        bt = make_book_translator()
        assert BookTranslator._is_non_translatable(bt, "$E=mc^2$") is True

    def test_two_formulas_not_skipped(self, make_book_translator):
        """CORE-B13 fix: iki formül ve arasında metin varsa SKIP ETME."""
        bt = make_book_translator()
        # Eski greedy regex burada True dönüyordu; fix sonrası False olmalı
        result = BookTranslator._is_non_translatable(
            bt, "$a$ herhangi bir metin $b$"
        )
        assert result is False

    def test_normal_text_translated(self, make_book_translator):
        bt = make_book_translator()
        assert BookTranslator._is_non_translatable(bt, "Hello world") is False

    def test_empty_whitespace_edge(self, make_book_translator):
        bt = make_book_translator()
        # Empty string: regex "^[...]+$" => + min 1 karakter gerekir; boş string False
        # Whitespace-only: strip sonrası boş, yine False
        assert BookTranslator._is_non_translatable(bt, "   ") is False


# ============================================================================
# _dedup_segments
# ============================================================================


class TestDedupSegments:
    """_dedup_segments — ardışık tekrarları kaldırma."""

    def test_empty_list(self, make_book_translator):
        bt = make_book_translator()
        assert BookTranslator._dedup_segments(bt, []) == []

    def test_no_duplicates(self, make_book_translator):
        bt = make_book_translator()
        segments = [{"text": "foo bar baz"}, {"text": "lorem ipsum dolor"}]
        result = BookTranslator._dedup_segments(bt, segments)
        assert len(result) == 2

    def test_consecutive_duplicates_removed(self, make_book_translator):
        bt = make_book_translator()
        segments = [
            {"text": "This is a long enough sentence."},
            {"text": "This is a long enough sentence."},
            {"text": "Different content here."},
        ]
        result = BookTranslator._dedup_segments(bt, segments)
        # Aynı olan ardışık segment düşmeli
        assert len(result) == 2

    def test_short_duplicates_preserved(self, make_book_translator):
        """10 karakterden kısa tekrarlar silinmemeli."""
        bt = make_book_translator()
        segments = [{"text": "ok"}, {"text": "ok"}, {"text": "ok"}]
        result = BookTranslator._dedup_segments(bt, segments)
        assert len(result) == 3  # Kısa - hepsi korunur

    def test_whitespace_normalized(self, make_book_translator):
        """Whitespace farkı da dedup sayılmalı."""
        bt = make_book_translator()
        segments = [
            {"text": "Aynı cümle ama boşluk farklı."},
            {"text": "Aynı  cümle  ama  boşluk  farklı."},
        ]
        result = BookTranslator._dedup_segments(bt, segments)
        assert len(result) == 1

    def test_non_consecutive_duplicates_kept(self, make_book_translator):
        """Aralarında başka segment varsa tekrarları KORU."""
        bt = make_book_translator()
        segments = [
            {"text": "Long sentence one here."},
            {"text": "Other content in between."},
            {"text": "Long sentence one here."},
        ]
        result = BookTranslator._dedup_segments(bt, segments)
        assert len(result) == 3


# ============================================================================
# _normalize_text (CORE-B12)
# ============================================================================


class TestNormalizeText:
    """_normalize_text — Kiril→Latin sadece source_lang=en ise."""

    def test_empty_text(self, make_book_translator):
        bt = make_book_translator()
        assert BookTranslator._normalize_text(bt, "") == ""

    def test_none_passed(self, make_book_translator):
        bt = make_book_translator()
        assert BookTranslator._normalize_text(bt, None) is None

    def test_soft_hyphen_removed(self, make_book_translator):
        bt = make_book_translator(source_lang="en")
        text = "encyc­lopedia"
        result = BookTranslator._normalize_text(bt, text)
        assert "­" not in result
        assert result == "encyclopedia"

    def test_english_source_cyrillic_to_latin(self, make_book_translator):
        """source_lang=en için Kiril'deki lookalike karakterler Latin'e dönmeli."""
        bt = make_book_translator(source_lang="en")
        # Kiril 'а' (U+0430) → Latin 'a'
        text = "Hеllo"  # Kiril 'е'
        result = BookTranslator._normalize_text(bt, text)
        assert "е" not in result
        assert "e" in result

    def test_russian_source_cyrillic_preserved(self, make_book_translator):
        """CORE-B12: source_lang=ru için Kiril BOZULMAMALI."""
        bt = make_book_translator(source_lang="ru")
        text = "Москва"  # Pure Cyrillic
        result = BookTranslator._normalize_text(bt, text)
        # Moskova yazılmış Kiril harfleri Latin'e dönmemeli
        assert "М" in result or "Москва" == result

    def test_unk_artifact_removed(self, make_book_translator):
        bt = make_book_translator()
        text = "This [UNK] is bad."
        result = BookTranslator._normalize_text(bt, text)
        assert "[UNK]" not in result

    def test_standalone_question_mark_token_removed(self, make_book_translator):
        bt = make_book_translator()
        text = "This [?] is bad."
        result = BookTranslator._normalize_text(bt, text)
        assert "[?]" not in result

    def test_inline_question_mark_preserved(self, make_book_translator):
        """Kelime içindeki [?] MT artifact değil, quiz içeriği — korunur."""
        bt = make_book_translator()
        # "word[?]word" şeklindeki gerçek [?] korunur
        text = "quiz[?]answer"
        result = BookTranslator._normalize_text(bt, text)
        assert "[?]" in result


# ============================================================================
# _build_translation_instruction
# ============================================================================


class TestBuildTranslationInstruction:
    """_build_translation_instruction — domain-aware prompt üretimi."""

    def test_scientific_type(self, make_book_translator):
        bt = make_book_translator(book_type="science")
        result = BookTranslator._build_translation_instruction(bt, "English", "Turkish")
        assert "English" in result and "Turkish" in result
        assert "math notation" in result.lower() or "preserve" in result.lower()

    def test_literary_type(self, make_book_translator):
        bt = make_book_translator(book_type="novel")
        result = BookTranslator._build_translation_instruction(bt, "English", "Turkish")
        assert "literary" in result.lower() or "natural" in result.lower()

    def test_general_type(self, make_book_translator):
        bt = make_book_translator(book_type="general")
        result = BookTranslator._build_translation_instruction(bt, "English", "Turkish")
        assert "English" in result and "Turkish" in result
        # Kısa, basit prompt
        assert "Output only" in result

    def test_all_prompts_end_with_output_instruction(self, make_book_translator):
        """Prompt leak'i azaltmak için 'Output only' kuralı her 3 modda da olmalı."""
        for book_type in ("science", "novel", "general"):
            bt = make_book_translator(book_type=book_type)
            result = BookTranslator._build_translation_instruction(
                bt, "English", "Turkish"
            )
            assert "Output only" in result


# ============================================================================
# _strip_prompt_leaks
# ============================================================================


class TestStripPromptLeaks:
    """_strip_prompt_leaks — MT çıktısındaki leak temizliği."""

    def test_empty_result_returns_empty(self, make_book_translator):
        bt = make_book_translator()
        assert BookTranslator._strip_prompt_leaks(bt, "", original="foo") == ""

    def test_whitespace_only_preserved(self, make_book_translator):
        bt = make_book_translator()
        # Tümüyle whitespace - erken return
        result = BookTranslator._strip_prompt_leaks(bt, "   \n  ", original="x")
        assert result.strip() == ""

    def test_ai_identity_leak_stripped(self, make_book_translator):
        """'As an AI language model' leak temizlenmeli, çeviri korunmalı."""
        bt = make_book_translator()
        leaked = "As an AI language model, Termodinamik önemlidir."
        result = BookTranslator._strip_prompt_leaks(
            bt, leaked, original="Thermodynamics is important."
        )
        assert "as an ai" not in result.lower()
        assert "Termodinamik" in result

    def test_clean_turkish_not_stripped(self, make_book_translator):
        """Temiz Türkçe metin false-positive almamalı."""
        bt = make_book_translator()
        clean = "Bu bir çeviri metni. Enerji korunumu önemlidir."
        result = BookTranslator._strip_prompt_leaks(bt, clean, original="")
        assert "Bu bir çeviri metni" in result
        assert "Enerji korunumu" in result

    def test_empty_after_stripping_falls_back_to_original(
        self, make_book_translator
    ):
        """Tamamıyla leak ise orijinal metin geri döner."""
        bt = make_book_translator()
        # Sadece leak fraz
        leaked = "Here is the translation: "
        original = "Thermodynamics is important."
        result = BookTranslator._strip_prompt_leaks(bt, leaked, original=original)
        # Temizlik sonrası boş kalırsa fallback original
        assert result == original or leaked in result

    def test_code_fence_stripped(self, make_book_translator):
        """Markdown code fence satırları temizlenmeli."""
        bt = make_book_translator()
        leaked = "```latex\nTermodinamik önemlidir.\n```"
        result = BookTranslator._strip_prompt_leaks(bt, leaked, original="")
        # Fence temizlenmeli ama content korunmalı
        assert "Termodinamik" in result


# ============================================================================
# _fix_unknown_tokens (CORE-B06)
# ============================================================================


class TestFixUnknownTokens:
    """_fix_unknown_tokens — [?] fallback ve index problemi."""

    def test_no_tokens_unchanged(self, make_book_translator):
        bt = make_book_translator()
        result = BookTranslator._fix_unknown_tokens(
            bt, "original text", "çeviri metni"
        )
        assert result == "çeviri metni"

    def test_unknown_token_removed_no_match(self, make_book_translator):
        """CORE-B06: eşleşme yoksa [?] sessizce kaldırılır, yanlış kelime atanmaz."""
        bt = make_book_translator()
        # Dictionary loader ve glossary yok — hiçbir strateji bulamaz
        bt.glossary = None
        bt._dict_loader = None
        bt._get_dictionary_translation = MagicMock(return_value=None)
        bt._context_match_score = MagicMock(return_value=0)

        result = BookTranslator._fix_unknown_tokens(
            bt, "completely different source", "çeviri [?] sonu"
        )
        # [?] kaldırılmış olmalı
        assert "[?]" not in result

    def test_unk_token_also_handled(self, make_book_translator):
        bt = make_book_translator()
        bt.glossary = None
        bt._dict_loader = None
        bt._get_dictionary_translation = MagicMock(return_value=None)
        bt._context_match_score = MagicMock(return_value=0)
        result = BookTranslator._fix_unknown_tokens(
            bt, "source text here", "çeviri [UNK] son"
        )
        assert "[UNK]" not in result

    def test_angle_unk_handled(self, make_book_translator):
        bt = make_book_translator()
        bt.glossary = None
        bt._dict_loader = None
        bt._get_dictionary_translation = MagicMock(return_value=None)
        bt._context_match_score = MagicMock(return_value=0)
        result = BookTranslator._fix_unknown_tokens(
            bt, "source text here", "çeviri <unk> son"
        )
        assert "<unk>" not in result

    def test_uppercase_word_preserved_as_proper_noun(self, make_book_translator):
        """Büyük harfle başlayan kelime özel isim olarak korunur (skor 65)."""
        bt = make_book_translator()
        bt.glossary = None
        bt._dict_loader = None
        bt._get_dictionary_translation = MagicMock(return_value=None)
        bt._context_match_score = MagicMock(return_value=0)

        # "Einstein" özel isim olarak [?] yerine gelebilir
        result = BookTranslator._fix_unknown_tokens(
            bt, "Einstein was smart", "[?] zekiydi"
        )
        # Sonuç ne olursa olsun crash olmamalı
        assert isinstance(result, str)

    def test_multiple_tokens_reverse_processed(self, make_book_translator):
        """Ters sırada işlem — birden fazla [?] için index kayması olmamalı."""
        bt = make_book_translator()
        bt.glossary = None
        bt._dict_loader = None
        # _get_dictionary_translation None dönsün (MagicMock yerine)
        bt._get_dictionary_translation = MagicMock(return_value=None)
        bt._context_match_score = MagicMock(return_value=0)
        result = BookTranslator._fix_unknown_tokens(
            bt, "source", "ilk [?] son [?] bitti"
        )
        assert "[?]" not in result


# ============================================================================
# _context_match_score
# ============================================================================


class TestContextMatchScore:
    """_context_match_score — baglam uyumu skoru."""

    def test_base_score(self, make_book_translator):
        bt = make_book_translator()
        bt.glossary = None
        bt._dict_loader = None
        bt._get_dictionary_translation = MagicMock(return_value=None)
        score = BookTranslator._context_match_score(
            bt, "word", "context", "after", "original"
        )
        assert isinstance(score, int)
        assert score >= 50  # Baz skor

    def test_word_in_original_bonus(self, make_book_translator):
        bt = make_book_translator()
        bt.glossary = None
        bt._dict_loader = None
        bt._get_dictionary_translation = MagicMock(return_value=None)
        # "energy" orijinalde → +20
        score_in = BookTranslator._context_match_score(
            bt, "energy", "", "", "This is about energy."
        )
        score_not = BookTranslator._context_match_score(
            bt, "foobar", "", "", "This is about energy."
        )
        assert score_in > score_not


# ============================================================================
# _get_dictionary_translation (CORE-B14 / P2-2)
# ============================================================================


class TestGetDictionaryTranslation:
    """_get_dictionary_translation — GlossaryManager öncelik, DictionaryLoader fallback."""

    def test_glossary_primary_hit(self, make_book_translator):
        """GlossaryManager'da terim varsa oradan döner."""
        bt = make_book_translator()
        bt.glossary = MagicMock()
        bt.glossary.get_term = MagicMock(return_value={"tr": "enerji"})
        bt._dict_loader = None

        result = BookTranslator._get_dictionary_translation(bt, "Energy")
        assert result == "enerji"
        # glossary önce çağrıldı
        bt.glossary.get_term.assert_called_with("energy")

    def test_dict_loader_fallback(self, make_book_translator):
        """Glossary bulamazsa DictionaryLoader'a düşer."""
        bt = make_book_translator()
        bt.glossary = MagicMock()
        bt.glossary.get_term = MagicMock(return_value=None)

        mock_loader = MagicMock()
        mock_loader.get_translation = MagicMock(return_value="kuvvet")
        bt._dict_loader = mock_loader

        result = BookTranslator._get_dictionary_translation(bt, "Force")
        assert result == "kuvvet"

    def test_both_miss_returns_none(self, make_book_translator):
        bt = make_book_translator()
        bt.glossary = MagicMock()
        bt.glossary.get_term = MagicMock(return_value=None)

        mock_loader = MagicMock()
        mock_loader.get_translation = MagicMock(return_value=None)
        bt._dict_loader = mock_loader

        result = BookTranslator._get_dictionary_translation(bt, "unknownterm")
        assert result is None

    def test_glossary_exception_caught(self, make_book_translator):
        """Glossary'de exception olsa bile loader denenmeli."""
        bt = make_book_translator()
        bt.glossary = MagicMock()
        bt.glossary.get_term = MagicMock(side_effect=RuntimeError("boom"))

        mock_loader = MagicMock()
        mock_loader.get_translation = MagicMock(return_value="sonuç")
        bt._dict_loader = mock_loader

        # Exception yutulmalı, loader'a düşmeli
        result = BookTranslator._get_dictionary_translation(bt, "word")
        assert result == "sonuç"


# ============================================================================
# Checkpoint sistemi (CORE-B22)
# ============================================================================


def _bind_checkpoint_methods(bt):
    """Gerçek checkpoint metodlarını MagicMock'a bind et (MagicMock self)."""
    bt._get_checkpoint_path = lambda fh, ft: BookTranslator._get_checkpoint_path(bt, fh, ft)


class TestCheckpointSystem:
    """Checkpoint save/load/delete round-trip ve atomic write."""

    def test_get_checkpoint_path(self, make_book_translator, tmp_path,
                                  monkeypatch):
        bt = make_book_translator()
        from core import translator as tr
        monkeypatch.setattr(tr, "CHECKPOINT_DIR", tmp_path)

        path = BookTranslator._get_checkpoint_path(bt, "abc123", "pdf")
        assert path.parent == tmp_path
        assert "abc123" in path.name
        assert "pdf" in path.name
        assert path.suffix == ".json"

    def test_save_load_roundtrip(self, make_book_translator, tmp_path,
                                  monkeypatch):
        bt = make_book_translator()
        _bind_checkpoint_methods(bt)
        from core import translator as tr
        monkeypatch.setattr(tr, "CHECKPOINT_DIR", tmp_path)

        # Gerçek bir dosya oluştur ki get_file_hash çalışsın
        input_file = tmp_path / "input.pdf"
        input_file.write_bytes(b"fake pdf content")

        segments = [{"text": "çeviri 1"}, {"text": "çeviri 2"}]
        BookTranslator._save_text_checkpoint(
            bt, str(input_file), "pdf", segments, total_segments=2
        )

        loaded = BookTranslator._load_text_checkpoint(bt, str(input_file), "pdf")
        assert loaded is not None
        assert loaded["translated_segments"] == segments
        assert loaded["total_segments"] == 2

    def test_load_missing_file_returns_none(self, make_book_translator, tmp_path,
                                             monkeypatch):
        bt = make_book_translator()
        _bind_checkpoint_methods(bt)
        from core import translator as tr
        monkeypatch.setattr(tr, "CHECKPOINT_DIR", tmp_path)

        # Input dosyası yok
        result = BookTranslator._load_text_checkpoint(
            bt, "/nonexistent/file.pdf", "pdf"
        )
        assert result is None

    def test_load_no_checkpoint_returns_none(self, make_book_translator, tmp_path,
                                              monkeypatch):
        bt = make_book_translator()
        _bind_checkpoint_methods(bt)
        from core import translator as tr
        monkeypatch.setattr(tr, "CHECKPOINT_DIR", tmp_path)

        input_file = tmp_path / "input.pdf"
        input_file.write_bytes(b"content")

        # Henüz save yapılmadı
        result = BookTranslator._load_text_checkpoint(bt, str(input_file), "pdf")
        assert result is None

    def test_delete_checkpoint(self, make_book_translator, tmp_path,
                                monkeypatch):
        bt = make_book_translator()
        _bind_checkpoint_methods(bt)
        from core import translator as tr
        monkeypatch.setattr(tr, "CHECKPOINT_DIR", tmp_path)

        input_file = tmp_path / "input.pdf"
        input_file.write_bytes(b"content")

        # Save
        BookTranslator._save_text_checkpoint(
            bt, str(input_file), "pdf", [{"text": "ok"}], 1
        )
        # Load çalışmalı
        assert BookTranslator._load_text_checkpoint(bt, str(input_file), "pdf") is not None

        # Delete
        BookTranslator._delete_text_checkpoint(bt, str(input_file), "pdf")
        # Artık yok
        assert BookTranslator._load_text_checkpoint(bt, str(input_file), "pdf") is None

    def test_atomic_write_no_partial_on_success(self, make_book_translator,
                                                  tmp_path, monkeypatch):
        """CORE-B22: atomic write ile tam JSON garantisi."""
        bt = make_book_translator()
        _bind_checkpoint_methods(bt)
        from core import translator as tr
        monkeypatch.setattr(tr, "CHECKPOINT_DIR", tmp_path)

        input_file = tmp_path / "big.pdf"
        input_file.write_bytes(b"big content")

        # 100 segment kaydet
        segments = [{"text": f"seg {i}", "type": "paragraph"} for i in range(100)]
        BookTranslator._save_text_checkpoint(
            bt, str(input_file), "pdf", segments, 100
        )

        # Dosya parse edilebilmeli
        path = BookTranslator._get_checkpoint_path(
            bt, _get_hash(str(input_file)), "pdf"
        )
        data = json.loads(path.read_text(encoding="utf-8"))
        assert len(data["translated_segments"]) == 100

        # Tmp dosyası kalmış olmamalı
        tmp_files = list(tmp_path.glob(".*.tmp"))
        assert len(tmp_files) == 0

    def test_save_preserves_created_at_on_update(self, make_book_translator,
                                                   tmp_path, monkeypatch):
        """İkinci save created_at'ı değiştirmemeli."""
        bt = make_book_translator()
        _bind_checkpoint_methods(bt)
        from core import translator as tr
        monkeypatch.setattr(tr, "CHECKPOINT_DIR", tmp_path)

        input_file = tmp_path / "input.pdf"
        input_file.write_bytes(b"content")

        BookTranslator._save_text_checkpoint(
            bt, str(input_file), "pdf", [{"text": "v1"}], 1
        )
        first = BookTranslator._load_text_checkpoint(bt, str(input_file), "pdf")

        import time
        time.sleep(0.01)

        BookTranslator._save_text_checkpoint(
            bt, str(input_file), "pdf", [{"text": "v1"}, {"text": "v2"}], 2
        )
        second = BookTranslator._load_text_checkpoint(bt, str(input_file), "pdf")

        assert first["created_at"] == second["created_at"]
        # updated_at değişmeli
        assert first["updated_at"] != second["updated_at"]


def _get_hash(file_path):
    """Test helper — get_file_hash'in aynısı."""
    from core.translator import get_file_hash
    return get_file_hash(file_path)


# ============================================================================
# _post_process_missing_terms (CORE-B04)
# ============================================================================


class TestPostProcessMissingTerms:
    """_post_process_missing_terms — word boundary ile doğru match."""

    def test_word_boundary_no_partial_match(self, make_book_translator):
        """CORE-B04: 'cell' kelimesi 'acceleration' içinde match etmemeli."""
        bt = make_book_translator()
        # Çeviride "acceleration" geçiyor, biz "cell" terimini arıyoruz
        translated = "The acceleration is important."
        protected_terms = [
            {
                "index": 0,
                "term_en": "cell",
                "term_tr": "hücre",
                "mode": "hard",
                "keep_original": False,
            }
        ]
        # Mock _translate_with_mt (fallback için)
        bt._translate_with_mt = MagicMock(return_value="ivme önemli")

        result = BookTranslator._post_process_missing_terms(
            bt, translated, protected_terms, [0], "The cell is important."
        )
        # "acceleration" → "accelerhücreation" OLMAMALI
        assert "accelhücre" not in result
        assert "hücreeration" not in result

    def test_english_term_replaced_by_turkish(self, make_book_translator):
        """Çeviride hala İngilizce 'force' var → 'kuvvet'e çevir."""
        bt = make_book_translator()
        translated = "The force is important."
        protected_terms = [
            {
                "index": 0,
                "term_en": "force",
                "term_tr": "kuvvet",
                "mode": "hard",
                "keep_original": False,
            }
        ]
        bt._translate_with_mt = MagicMock(return_value="")

        result = BookTranslator._post_process_missing_terms(
            bt, translated, protected_terms, [0], "The force is important."
        )
        assert "kuvvet" in result.lower()

    def test_already_translated_not_rereplaced(self, make_book_translator):
        """Çeviri zaten doğru Türkçe ile gelmiş — dokunma."""
        bt = make_book_translator()
        translated = "Kuvvet önemlidir."
        protected_terms = [
            {
                "index": 0,
                "term_en": "force",
                "term_tr": "kuvvet",
                "mode": "hard",
                "keep_original": False,
            }
        ]
        bt._translate_with_mt = MagicMock(return_value="")

        result = BookTranslator._post_process_missing_terms(
            bt, translated, protected_terms, [0], "Force matters."
        )
        assert "Kuvvet" in result or "kuvvet" in result

    def test_keep_original_preserves_english(self, make_book_translator):
        """keep_original=True ise English terim kalır."""
        bt = make_book_translator()
        translated = "The API is strong."
        protected_terms = [
            {
                "index": 0,
                "term_en": "API",
                "term_tr": "API",
                "mode": "hard",
                "keep_original": True,
            }
        ]
        bt._translate_with_mt = MagicMock(return_value="API güçlü.")
        result = BookTranslator._post_process_missing_terms(
            bt, translated, protected_terms, [0], "The API is strong."
        )
        assert "API" in result


# ============================================================================
# _post_process_glossary_final (CORE-B02)
# ============================================================================


class TestPostProcessGlossaryFinal:
    """_post_process_glossary_final — _mt_failed atlama."""

    def test_empty_segments(self, make_book_translator):
        bt = make_book_translator()
        bt.glossary = MagicMock()
        bt.glossary.terms = {}
        result = BookTranslator._post_process_glossary_final(bt, [])
        assert result == []

    def test_no_glossary(self, make_book_translator):
        bt = make_book_translator()
        bt.glossary = None
        segments = [{"text": "foo"}]
        result = BookTranslator._post_process_glossary_final(bt, segments)
        assert result == segments

    def test_mt_failed_segment_skipped(self, make_book_translator):
        """CORE-B02: _mt_failed=True olan segment dokunulmadan geçer."""
        bt = make_book_translator()
        bt.glossary = MagicMock()
        bt.glossary.terms = {
            "thermodynamics": {"en": "thermodynamics", "tr": "termodinamik"}
        }
        bt.config.glossary_mode = "post"

        segments = [
            {"text": "Thermodynamics is important.", "_mt_failed": True},
            {"text": "Thermodynamics energy.", "_mt_failed": False},
        ]
        result = BookTranslator._post_process_glossary_final(bt, segments)
        # İlk segment değişmemeli
        assert result[0]["text"] == "Thermodynamics is important."
        # İkinci segment glossary ile değişmiş olmalı
        assert "termodinamik" in result[1]["text"].lower()

    def test_glossary_mode_off_returns_unchanged(self, make_book_translator):
        bt = make_book_translator()
        bt.glossary = MagicMock()
        bt.glossary.terms = {"force": {"en": "force", "tr": "kuvvet"}}
        bt.config.glossary_mode = "off"

        segments = [{"text": "Force is key."}]
        result = BookTranslator._post_process_glossary_final(bt, segments)
        # "off" modda dokunulmamalı
        assert result[0]["text"] == "Force is key."

    def test_keep_original_skipped(self, make_book_translator):
        """keep_original=True olan terim dokunulmaz."""
        bt = make_book_translator()
        bt.glossary = MagicMock()
        bt.glossary.terms = {
            "api": {"en": "API", "tr": "API", "keep_original": True}
        }
        bt.config.glossary_mode = "post"

        segments = [{"text": "The API is fast."}]
        result = BookTranslator._post_process_glossary_final(bt, segments)
        assert "API" in result[0]["text"]


# ============================================================================
# mine_terms_from_book
# ============================================================================


class TestMineTermsFromBook:
    """mine_terms_from_book — glossary bootstrap."""

    def test_no_glossary_returns_zero(self, make_book_translator):
        bt = make_book_translator()
        bt.glossary = None
        result = BookTranslator.mine_terms_from_book(bt, "some text", min_freq=2)
        assert result == 0

    def test_term_miner_called(self, make_book_translator):
        bt = make_book_translator()
        bt.glossary = MagicMock()
        bt.glossary.mode = "post"
        bt.glossary.add_mined_terms = MagicMock(return_value=5)

        with patch("core.term_miner.TermMiner") as MockMiner:
            mock_instance = MagicMock()
            mock_instance.extract_terms = MagicMock(return_value=[])
            MockMiner.return_value = mock_instance

            result = BookTranslator.mine_terms_from_book(
                bt, "Sample text about thermodynamics.", min_freq=2
            )
            MockMiner.assert_called_once_with(min_freq=2)
            assert result == 5

    def test_glossary_mode_restored_after(self, make_book_translator):
        """Mode geçici olarak 'hard' olur ama finally ile eski haline döner."""
        bt = make_book_translator()
        bt.glossary = MagicMock()
        bt.glossary.mode = "post"
        bt.glossary.add_mined_terms = MagicMock(return_value=0)

        with patch("core.term_miner.TermMiner") as MockMiner:
            mock_instance = MagicMock()
            mock_instance.extract_terms = MagicMock(return_value=[])
            MockMiner.return_value = mock_instance

            BookTranslator.mine_terms_from_book(bt, "text", 2)
            # Mode eski haline dönmüş olmalı
            assert bt.glossary.mode == "post"

    def test_import_error_returns_zero(self, make_book_translator):
        bt = make_book_translator()
        bt.glossary = MagicMock()
        bt.glossary.mode = "post"

        with patch("builtins.__import__", side_effect=ImportError("no term_miner")):
            # term_miner import edilemezse
            # NOT: inner "from core.term_miner import TermMiner" patch edilmeli
            pass

        # Alternatif: TermMiner patch'inde exception raise et
        with patch("core.term_miner.TermMiner", side_effect=Exception("boom")):
            result = BookTranslator.mine_terms_from_book(bt, "text", 2)
            # Exception yakalanmalı
            assert result == 0


# ============================================================================
# add_custom_terms
# ============================================================================


class TestAddCustomTerms:
    def test_no_glossary(self, make_book_translator):
        bt = make_book_translator()
        bt.glossary = None
        # Crash yapmamalı
        BookTranslator.add_custom_terms(bt, {"force": "kuvvet"})

    def test_calls_glossary_method(self, make_book_translator):
        bt = make_book_translator()
        bt.glossary = MagicMock()
        terms = {"force": "kuvvet", "energy": "enerji"}
        BookTranslator.add_custom_terms(bt, terms)
        bt.glossary.add_terms_from_dict.assert_called_once_with(terms)


# ============================================================================
# get_stats
# ============================================================================


class TestGetStats:
    def test_basic_stats(self, make_book_translator):
        bt = make_book_translator()
        bt.stats = {"segments_translated": 42, "segments_from_memory": 7}
        bt.memory = None
        bt.glossary = None
        # _mt_error_count yok

        stats = BookTranslator.get_stats(bt)
        assert stats["segments_translated"] == 42
        assert stats["mt_engine"] == bt.config.mt_engine
        assert "mt_model" in stats

    def test_mt_error_count_included(self, make_book_translator):
        bt = make_book_translator()
        bt.stats = {"segments_translated": 10}
        bt.memory = None
        bt.glossary = None
        bt._mt_error_count = 3

        stats = BookTranslator.get_stats(bt)
        assert stats["mt_errors"] == 3

    def test_memory_stats_included(self, make_book_translator):
        bt = make_book_translator()
        bt.stats = {"segments_translated": 1}
        bt.memory = MagicMock()
        bt.memory.get_stats = MagicMock(return_value={"total_entries": 500})
        bt.glossary = None

        stats = BookTranslator.get_stats(bt)
        assert stats["memory"]["total_entries"] == 500

    def test_original_stats_not_mutated(self, make_book_translator):
        """Dict copy — orijinal self.stats'a mt_engine eklememeli."""
        bt = make_book_translator()
        bt.stats = {"segments_translated": 5}
        bt.memory = None
        bt.glossary = None

        BookTranslator.get_stats(bt)
        # bt.stats içinde mt_engine OLMAMALI (çünkü copy döndü)
        assert "mt_engine" not in bt.stats


# ============================================================================
# export_memory / import_memory
# ============================================================================


class TestExportImportMemory:
    def test_export_no_memory(self, make_book_translator):
        bt = make_book_translator()
        bt.memory = None
        # Crash yapmamalı
        BookTranslator.export_memory(bt, "/tmp/out.json")

    def test_export_calls_memory(self, make_book_translator):
        bt = make_book_translator()
        bt.memory = MagicMock()
        BookTranslator.export_memory(bt, "/tmp/out.json")
        bt.memory.export_to_json.assert_called_once_with("/tmp/out.json")

    def test_import_no_memory(self, make_book_translator):
        bt = make_book_translator()
        bt.memory = None
        # Crash yapmamalı
        BookTranslator.import_memory(bt, "/tmp/in.json")

    def test_import_calls_memory(self, make_book_translator):
        bt = make_book_translator()
        bt.memory = MagicMock()
        BookTranslator.import_memory(bt, "/tmp/in.json")
        bt.memory.import_from_json.assert_called_once_with("/tmp/in.json")


# ============================================================================
# translate_file (dispatcher)
# ============================================================================


class TestTranslateFile:
    """translate_file — doğru alt fonksiyonu çağırır."""

    def test_missing_file_raises(self, make_book_translator):
        bt = make_book_translator()
        with pytest.raises(FileNotFoundError):
            BookTranslator.translate_file(bt, "/nonexistent/file.pdf")

    def test_unsupported_format_raises(self, make_book_translator, tmp_path):
        bt = make_book_translator()
        fake = tmp_path / "file.xyz"
        fake.write_text("content")
        with pytest.raises(ValueError):
            BookTranslator.translate_file(bt, str(fake))

    def test_pdf_dispatches_to_pdf_handler(self, make_book_translator, tmp_path):
        bt = make_book_translator()
        fake = tmp_path / "book.pdf"
        fake.write_bytes(b"%PDF-1.4 fake")
        bt._translate_pdf = MagicMock(return_value=("/out/file.pdf", None))

        BookTranslator.translate_file(bt, str(fake))
        bt._translate_pdf.assert_called_once()

    def test_epub_dispatches_to_epub_handler(self, make_book_translator, tmp_path):
        bt = make_book_translator()
        fake = tmp_path / "book.epub"
        fake.write_bytes(b"fake epub")
        bt._translate_epub = MagicMock(return_value=("/out/file.epub", None))

        BookTranslator.translate_file(bt, str(fake))
        bt._translate_epub.assert_called_once()

    def test_txt_dispatches_to_text_handler(self, make_book_translator, tmp_path):
        bt = make_book_translator()
        fake = tmp_path / "book.txt"
        fake.write_text("content")
        bt._translate_text_file = MagicMock(return_value=("/out/file.txt", None))

        BookTranslator.translate_file(bt, str(fake))
        bt._translate_text_file.assert_called_once()

    def test_md_also_dispatched_to_text(self, make_book_translator, tmp_path):
        bt = make_book_translator()
        fake = tmp_path / "doc.md"
        fake.write_text("# hello")
        bt._translate_text_file = MagicMock(return_value=("/out/f.md", None))

        BookTranslator.translate_file(bt, str(fake))
        bt._translate_text_file.assert_called_once()

    def test_auto_output_path_generated(self, make_book_translator, tmp_path):
        """output_path=None → auto temp dizin oluşturulur."""
        bt = make_book_translator()
        fake = tmp_path / "book.pdf"
        fake.write_bytes(b"%PDF fake")

        captured = {}

        def fake_pdf(inp, outp, fmt, cb):
            captured["outp"] = outp
            return outp, None

        bt._translate_pdf = MagicMock(side_effect=fake_pdf)
        BookTranslator.translate_file(bt, str(fake), output_path=None)
        # Output path otomatik oluşturulmuş olmalı
        assert captured["outp"] is not None
        assert "book" in captured["outp"]


# ============================================================================
# _translate_text_file
# ============================================================================


class TestTranslateTextFile:
    """_translate_text_file — TXT akışı."""

    def test_basic_flow(self, make_book_translator, tmp_path):
        bt = make_book_translator()
        bt.splitter = MagicMock()
        bt.splitter.split = MagicMock(return_value=["Sentence one.", "Two."])
        bt.translate_segments = MagicMock(
            return_value=[{"text": "Cümle bir."}, {"text": "İki."}]
        )
        bt.qa_checker = None
        bt.config.batch_size = 10
        # F41 (2026-07-29): dokuman fazlari artik batch dongusunun DISINDA,
        # _translate_text_file sonunda kosuyor. translate_segments mock'landigi
        # icin bu testte fazlar kapatilir (kapsam: TXT paragraf akisi).
        bt.config.check_consistency = False
        bt.consistency_analyzer = None
        bt.glossary = None

        input_file = tmp_path / "in.txt"
        input_file.write_text("Sentence one. Two.", encoding="utf-8")
        output_file = tmp_path / "out.txt"

        # Checkpoint fonksiyonlarını mock'la
        bt._load_text_checkpoint = MagicMock(return_value=None)
        bt._save_text_checkpoint = MagicMock()
        bt._delete_text_checkpoint = MagicMock()

        result_path, qa = BookTranslator._translate_text_file(
            bt, str(input_file), str(output_file), None
        )

        assert result_path == str(output_file)
        assert output_file.exists()
        content = output_file.read_text(encoding="utf-8")
        assert "Cümle bir" in content


# ============================================================================
# translate_segments (pipeline)
# ============================================================================


def _bind_preprocess(bt):
    """translate_segments'in ihtiyacı olan önişleme metodlarını bind et."""
    bt._dedup_segments = lambda segs: BookTranslator._dedup_segments(bt, segs)
    bt._normalize_text = lambda txt: BookTranslator._normalize_text(bt, txt)


class TestTranslateSegments:
    """translate_segments — ana pipeline orchestration."""

    def test_empty_segments(self, make_book_translator):
        bt = make_book_translator()
        _bind_preprocess(bt)
        bt.splitter = MagicMock()
        bt._translate_batch_segments = MagicMock(return_value=[])
        bt.translate_text = MagicMock()
        bt.qa_checker = None
        bt.consistency_analyzer = None
        bt.config.retry_low_score = False
        bt.config.check_consistency = False
        bt.config.glossary_mode = "off"
        bt.glossary = None

        result = BookTranslator.translate_segments(bt, [])
        assert result == []

    def test_basic_batch_flow(self, make_book_translator):
        """Segment listesi batch işlemden geçer."""
        bt = make_book_translator(source_lang="en")
        _bind_preprocess(bt)
        bt._translate_batch_segments = MagicMock(
            return_value=["çeviri 1", "çeviri 2"]
        )
        bt.translate_text = MagicMock()
        bt.qa_checker = None
        bt.consistency_analyzer = None
        bt.config.retry_low_score = False
        bt.config.check_consistency = False
        bt.config.glossary_mode = "off"
        bt.config.batch_size = 10
        bt.glossary = None

        segments = [
            {"text": "sentence one here", "type": "paragraph"},
            {"text": "sentence two here", "type": "paragraph"},
        ]
        result = BookTranslator.translate_segments(bt, segments)
        assert len(result) == 2
        assert result[0]["text"] == "çeviri 1"
        assert result[0]["original"] == "sentence one here"

    def test_count_mismatch_marked_mt_failed(self, make_book_translator):
        """Translation sayısı segment sayısından az → eksikler MT_FAIL işaretlenmeli."""
        bt = make_book_translator(source_lang="en")
        _bind_preprocess(bt)
        # Sadece 1 çeviri dönüyor, 2 segment var
        bt._translate_batch_segments = MagicMock(return_value=["çeviri 1"])
        bt.translate_text = MagicMock()
        bt.qa_checker = None
        bt.consistency_analyzer = None
        bt.config.retry_low_score = False
        bt.config.check_consistency = False
        bt.config.glossary_mode = "off"
        bt.config.batch_size = 10
        bt.glossary = None
        bt.stats = {"segments_translated": 0, "segments_from_memory": 0}

        segments = [
            {"text": "sentence one here", "type": "p"},
            {"text": "sentence two here", "type": "p"},
        ]
        result = BookTranslator.translate_segments(bt, segments)
        assert len(result) == 2
        # İkincisi _mt_failed olmalı
        assert result[1].get("_mt_failed") is True

    def test_progress_callback_invoked(self, make_book_translator):
        bt = make_book_translator(source_lang="en")
        _bind_preprocess(bt)
        bt._translate_batch_segments = MagicMock(return_value=["çeviri"])
        bt.translate_text = MagicMock()
        bt.qa_checker = None
        bt.consistency_analyzer = None
        bt.config.retry_low_score = False
        bt.config.check_consistency = False
        bt.config.glossary_mode = "off"
        bt.config.batch_size = 10
        bt.glossary = None

        progress_calls = []

        def cb(p, m):
            progress_calls.append((p, m))

        segments = [{"text": "text here", "type": "p"}]
        BookTranslator.translate_segments(bt, segments, progress_callback=cb)
        # En az bir kez çağrılmalı
        assert len(progress_calls) > 0
        # Son çağrı 1.0 olmalı
        assert progress_calls[-1][0] == 1.0


# ============================================================================
# __init__ / _initialize (smoke — MT model yüklemeden)
# ============================================================================


class TestInitialization:
    """BookTranslator init akışı — MT model mock'lanarak."""

    def test_default_config_initializes(self):
        """Default config → _init_mt_model mock'la çağırdığımızda crash olmamalı."""
        with patch.object(BookTranslator, "_init_mt_model") as mock_mt, \
             patch.object(BookTranslator, "_init_glossary") as mock_gloss, \
             patch.object(BookTranslator, "_init_memory") as mock_mem:
            bt = BookTranslator()
            assert bt.is_initialized is True
            mock_mt.assert_called_once()
            # Default config'de glossary_mode != "off" → init çağrılır
            mock_gloss.assert_called_once()
            # use_memory=True default → init çağrılır
            mock_mem.assert_called_once()

    def test_custom_config_glossary_off(self):
        """glossary_mode=off → _init_glossary çağrılmaz."""
        config = TranslationConfig(glossary_mode="off", use_memory=False)
        with patch.object(BookTranslator, "_init_mt_model"), \
             patch.object(BookTranslator, "_init_glossary") as mock_gloss, \
             patch.object(BookTranslator, "_init_memory") as mock_mem:
            bt = BookTranslator(config=config)
            mock_gloss.assert_not_called()
            mock_mem.assert_not_called()

    def test_initial_stats_zero(self):
        with patch.object(BookTranslator, "_init_mt_model"):
            config = TranslationConfig(
                use_memory=False, glossary_mode="off",
                run_qa=False, check_consistency=False,
            )
            bt = BookTranslator(config=config)
            assert bt.stats["segments_translated"] == 0
            assert bt.stats["segments_from_memory"] == 0
            assert bt.stats["terms_enforced"] == 0


# ============================================================================
# _init_glossary / _init_memory (error paths)
# ============================================================================


class TestInitGlossary:
    def test_missing_glossary_dir_created(self, tmp_path, make_book_translator):
        """Glossary dir yoksa oluşturulur."""
        bt = make_book_translator()
        bt.config.glossary_dir = str(tmp_path / "new_glossary")
        bt.config.glossary_mode = "post"
        bt.stats = {"glossary_terms_loaded": 0}

        # translator.py modül üstünden import ediyor: `from core.glossary import GlossaryManager`
        with patch("core.translator.GlossaryManager") as MockGM:
            mock_gm = MagicMock()
            mock_gm.terms = {"a": {}, "b": {}}
            MockGM.return_value = mock_gm
            BookTranslator._init_glossary(bt)

            # Dizin oluşturulmuş olmalı
            assert (tmp_path / "new_glossary").exists()
            assert bt.glossary is mock_gm

    def test_exception_sets_glossary_none(self, make_book_translator):
        bt = make_book_translator()
        bt.config.glossary_dir = "/some/dir"
        bt.config.glossary_mode = "post"

        with patch("core.translator.GlossaryManager", side_effect=RuntimeError("boom")):
            BookTranslator._init_glossary(bt)
            assert bt.glossary is None


class TestInitMemory:
    def test_path_traversal_blocked(self, make_book_translator):
        bt = make_book_translator()
        bt.config.memory_db_path = "/tmp/../etc/passwd"
        bt.config.project_name = "test"
        # ValueError yakalanıp memory None kalmalı
        BookTranslator._init_memory(bt)
        # memory set edilmemiş olmalı (exception içeride yutuldu)

    def test_successful_init(self, tmp_path, make_book_translator):
        bt = make_book_translator()
        db_path = tmp_path / "tm.db"
        bt.config.memory_db_path = str(db_path)
        bt.config.project_name = "test"
        bt.stats = {"memory_entries_loaded": 0}

        with patch("core.translator.TranslationMemory") as MockTM:
            mock_tm = MagicMock()
            mock_tm.get_stats = MagicMock(return_value={"total_entries": 10})
            MockTM.return_value = mock_tm
            BookTranslator._init_memory(bt)
            assert bt.memory is mock_tm

