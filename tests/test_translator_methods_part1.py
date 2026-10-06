"""
core/translator.py :: BookTranslator — MT + batch + init + protect/restore
metod testleri (Part 1).

Bu dosya şu alanları kapsar:
- translate / translate_text (ana entry + alias)
- _translate_with_mt (uzun/kısa dispatch)
- _translate_with_hymt / _translate_with_hymt_mlx / _translate_with_hymt_transformers
- _translate_single
- _translate_batch_hymt (Transformers yolu + OOM fallback)
- _translate_batch_mlx (MLX yolu + int token conversion)
- translate_batch (public batch API)
- _translate_batch_segments (koruma dahil batch + length mismatch)
- _init_mt_model / _init_hymt_model / _init_hymt_mlx / _init_hymt_transformers
- _protect_references / _restore_references (CORE-B23)
- _protect_formulas / _restore_formulas
- _protect_units / _restore_units
"""

from __future__ import annotations

import sys
import platform
from pathlib import Path
from typing import List
from unittest.mock import MagicMock, patch, PropertyMock
import pytest

# Proje kök dizini path'e
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.translator import BookTranslator, TranslationConfig  # noqa: E402


IS_MAC = platform.system() == "Darwin"

# MLX durumu
try:
    import mlx_lm  # noqa: F401
    MLX_AVAILABLE = True
except ImportError:
    MLX_AVAILABLE = False


# =============================================================================
# Yardımcı fixture: BookTranslator iskeleti (init'i bypass eder)
# =============================================================================


def _build_skeleton(
    *,
    mt_engine: str = "hymt-7b",
    source_lang: str = "en",
    target_lang: str = "tr",
    book_type: str = "general",
    use_memory: bool = True,
    memory_threshold: int = 90,
    glossary_mode: str = "off",
    batch_size: int = 4,
    mt_model=None,
    mt_tokenizer=None,
    use_mlx: bool = True,
) -> BookTranslator:
    """BookTranslator örneği kur, ama __init__ YOLUNDAN GEÇMEDEN.

    BookTranslator.__init__ zorunlu olarak MT modelini yükler (bize
    14GB model indirtir). Bunu atlamak için __new__ ile çıplak nesne
    oluşturup gerekli alanları elle atarız.
    """
    bt = BookTranslator.__new__(BookTranslator)
    bt.config = TranslationConfig(
        mt_engine=mt_engine,
        source_lang=source_lang,
        target_lang=target_lang,
        book_type=book_type,
        use_memory=use_memory,
        memory_threshold=memory_threshold,
        glossary_mode=glossary_mode,
        batch_size=batch_size,
    )

    bt.mt_model = mt_model if mt_model is not None else MagicMock()
    bt.mt_tokenizer = mt_tokenizer if mt_tokenizer is not None else MagicMock()
    # (KARAR 35, FX3 2026-10-05: ai_client / use_ai_review — dış AI düzeltme
    # yolu — çekirdekten silindi.)
    bt.glossary = None
    bt.memory = None
    bt.qa_checker = None
    bt.consistency_analyzer = None
    bt.naturalizer = None
    bt.ner_protector = None
    bt._use_mlx = use_mlx
    bt._dict_loader = None
    bt._hymt_src_lang = "en"
    bt._hymt_tgt_lang = "tr"
    # splitter (sentence splitter) - hafif, gerçek versiyonu kullanılabilir
    from core.sentence_splitter import SentenceSplitter
    bt.splitter = SentenceSplitter()

    bt.is_initialized = True
    bt.stats = {
        "segments_translated": 0,
        "segments_from_memory": 0,
        "terms_enforced": 0,
        "retried_segments": 0,
        "consistency_fixes": 0,
        "glossary_terms_loaded": 0,
        "memory_entries_loaded": 0,
    }
    return bt


@pytest.fixture
def skeleton_translator():
    """Parametrik BookTranslator iskeleti factory."""
    return _build_skeleton


# =============================================================================
# 1. translate() — alias testleri
# =============================================================================


class TestTranslateAlias:
    """BookTranslator.translate -> translate_text alias (CORE-B20)."""

    def test_alias_delegates_to_translate_text(self, skeleton_translator):
        bt = skeleton_translator()
        bt.translate_text = MagicMock(return_value="CEVRILDI")
        out = bt.translate("hello")
        assert out == "CEVRILDI"
        bt.translate_text.assert_called_once_with("hello", context=None)

    def test_alias_forwards_context(self, skeleton_translator):
        bt = skeleton_translator()
        bt.translate_text = MagicMock(return_value="X")
        bt.translate("t", context="ctx")
        _, kwargs = bt.translate_text.call_args
        assert kwargs.get("context") == "ctx"

    def test_alias_ignores_unused_langs(self, skeleton_translator):
        """CORE-B20: source_lang/target_lang parametreleri signature'da var
        ama metod gövdesinde kullanılmıyor (config'ten alınıyor)."""
        bt = skeleton_translator()
        bt.translate_text = MagicMock(return_value="Y")
        bt.translate("t", source_lang="de", target_lang="fr")
        # Hata fırlatmamalı; basit dispatch olmalı.
        bt.translate_text.assert_called_once()


# =============================================================================
# 2. translate_text() — ana MT entry testleri
# =============================================================================


class TestTranslateText:
    """BookTranslator.translate_text — 3-pass pipeline."""

    def test_empty_input_returns_empty(self, skeleton_translator):
        bt = skeleton_translator()
        assert bt.translate_text("") == ""

    def test_none_input_returns_empty_string(self, skeleton_translator):
        bt = skeleton_translator()
        # Kod: `return text or ""` — None geldiğinde ""
        assert bt.translate_text(None) == ""

    def test_whitespace_only_returns_original(self, skeleton_translator):
        bt = skeleton_translator()
        result = bt.translate_text("   \n  ")
        # Whitespace ise orijinal döner (veya "")
        assert result in ("   \n  ", "")

    def test_very_short_text_skipped(self, skeleton_translator):
        """len(text.strip()) < 3 -> çevrilmeden döner."""
        bt = skeleton_translator()
        bt._translate_with_mt = MagicMock()
        result = bt.translate_text("hi")
        assert result == "hi"
        bt._translate_with_mt.assert_not_called()

    def test_non_translatable_skipped(self, skeleton_translator):
        """Sadece sayı/sembol çevrilmez."""
        bt = skeleton_translator()
        bt._translate_with_mt = MagicMock()
        result = bt.translate_text("12345")
        assert result == "12345"
        bt._translate_with_mt.assert_not_called()

    def test_mt_fail_prefix_stripped(self, skeleton_translator):
        """[MT_FAIL] prefix son çıktıdan temizlenir."""
        bt = skeleton_translator()
        bt._translate_with_mt = MagicMock(return_value="[MT_FAIL]Thermodynamics")
        result = bt.translate_text("Thermodynamics")
        assert not result.startswith("[MT_FAIL]")
        assert "Thermodynamics" in result

    def test_memory_add_blocked_on_mt_fail(self, skeleton_translator):
        """MT_FAIL olduğunda memory.add ÇAĞRILMAMALI (zehirli cache)."""
        bt = skeleton_translator()
        bt.memory = MagicMock()
        bt.memory.get_or_similar = MagicMock(return_value=(None, None))
        bt._translate_with_mt = MagicMock(return_value="[MT_FAIL]original")
        bt.translate_text("original scientific phrase")
        bt.memory.add.assert_not_called()

    def test_memory_add_called_on_success(self, skeleton_translator):
        bt = skeleton_translator()
        bt.memory = MagicMock()
        bt.memory.get_or_similar = MagicMock(return_value=(None, None))
        bt._translate_with_mt = MagicMock(return_value="basarili ceviri")
        bt.translate_text("successful phrase", context="ctx")
        bt.memory.add.assert_called_once()

    def test_memory_cache_hit_skips_mt(self, skeleton_translator):
        bt = skeleton_translator()
        bt.memory = MagicMock()
        bt.memory.get_or_similar = MagicMock(return_value=("önbellekten", "exact"))
        bt._translate_with_mt = MagicMock()
        result = bt.translate_text("cached phrase")
        assert result == "önbellekten"
        bt._translate_with_mt.assert_not_called()
        assert bt.stats["segments_from_memory"] == 1

    def test_stats_incremented_on_success(self, skeleton_translator):
        bt = skeleton_translator()
        bt._translate_with_mt = MagicMock(return_value="tercüme")
        before = bt.stats["segments_translated"]
        bt.translate_text("Some proper long input text here.")
        assert bt.stats["segments_translated"] == before + 1


# =============================================================================
# 3. _translate_with_mt — dispatcher / error handling
# =============================================================================


class TestTranslateWithMt:
    """_translate_with_mt — model yok / kısa / uzun / hata."""

    def test_returns_text_when_model_missing(self, skeleton_translator):
        bt = skeleton_translator(mt_model=None, mt_tokenizer=None)
        # mt_model None; metod early-return ile text'i aynen vermeli
        bt.mt_model = None
        bt.mt_tokenizer = None
        assert bt._translate_with_mt("deneme") == "deneme"

    def test_short_text_uses_single(self, skeleton_translator):
        bt = skeleton_translator()
        bt._translate_single = MagicMock(return_value="cevri")
        out = bt._translate_with_mt("kısa metin")
        assert out == "cevri"
        bt._translate_single.assert_called_once()

    def test_long_text_split_and_joined(self, skeleton_translator):
        """512 karakter eşiğini aşan metin splitter'a gider."""
        bt = skeleton_translator()
        long_text = "Sentence one. " * 100  # çok uzun
        bt._translate_single = MagicMock(side_effect=lambda s, *a, **k: f"[TR]{s[:10]}")
        out = bt._translate_with_mt(long_text)
        # Birden çok kez çağrılmış olmalı
        assert bt._translate_single.call_count > 1
        assert "[TR]" in out

    def test_mt_error_returns_mt_fail_prefix(self, skeleton_translator):
        bt = skeleton_translator()
        bt._translate_single = MagicMock(side_effect=RuntimeError("CUDA out of memory"))
        out = bt._translate_with_mt("some input text here long enough")
        assert out.startswith("[MT_FAIL]")

    def test_mt_error_counter_increments(self, skeleton_translator):
        bt = skeleton_translator()
        bt._translate_single = MagicMock(side_effect=Exception("boom"))
        bt._translate_with_mt("first one here")
        bt._translate_with_mt("second one here")
        assert bt._mt_error_count >= 2


# =============================================================================
# 4. _translate_single — delegates to hymt
# =============================================================================


class TestTranslateSingle:
    def test_delegates_to_hymt(self, skeleton_translator):
        # A7: olu beam_size parametresi kaldirildi; artik is_heading tasiniyor.
        bt = skeleton_translator()
        bt._translate_with_hymt = MagicMock(return_value="TR")
        out = bt._translate_single("text", 512)
        assert out == "TR"
        bt._translate_with_hymt.assert_called_once_with("text", 512, is_heading=False)

    def test_delegates_is_heading(self, skeleton_translator):
        bt = skeleton_translator()
        bt._translate_with_hymt = MagicMock(return_value="TR")
        bt._translate_single("Baslik", 512, is_heading=True)
        bt._translate_with_hymt.assert_called_once_with("Baslik", 512, is_heading=True)


# =============================================================================
# 5. _translate_with_hymt — MLX vs Transformers dispatch
# =============================================================================


class TestTranslateWithHymt:
    def test_dispatches_to_mlx_when_use_mlx(self, skeleton_translator):
        bt = skeleton_translator(use_mlx=True)
        bt._translate_with_hymt_mlx = MagicMock(return_value="MLX_RESULT")
        bt._translate_with_hymt_transformers = MagicMock(return_value="TF_RESULT")
        out = bt._translate_with_hymt("hello")
        assert out == "MLX_RESULT"
        bt._translate_with_hymt_transformers.assert_not_called()

    def test_dispatches_to_transformers_when_not_mlx(self, skeleton_translator):
        bt = skeleton_translator(use_mlx=False)
        bt._translate_with_hymt_mlx = MagicMock(return_value="MLX")
        bt._translate_with_hymt_transformers = MagicMock(return_value="TF")
        out = bt._translate_with_hymt("hello")
        assert out == "TF"
        bt._translate_with_hymt_mlx.assert_not_called()

    def test_unknown_source_lang_defaults_to_english(self, skeleton_translator):
        bt = skeleton_translator(source_lang="xyz")
        bt._translate_with_hymt_transformers = MagicMock(return_value="X")
        bt._use_mlx = False
        bt._translate_with_hymt("hi")
        args, _ = bt._translate_with_hymt_transformers.call_args
        # (text, src_lang, tgt_lang, max_length) — src_lang "English" olmalı
        assert args[1] == "English"


# =============================================================================
# 6. _translate_with_hymt_mlx — MLX path (Mac-only)
# =============================================================================


@pytest.mark.skipif(not IS_MAC, reason="MLX sadece Mac'te")
@pytest.mark.skipif(not MLX_AVAILABLE, reason="mlx_lm paketi kurulu değil")
class TestTranslateWithHymtMlx:
    def test_generate_called_and_cleaned(self, skeleton_translator):
        bt = skeleton_translator(use_mlx=True)
        with patch("mlx_lm.generate", return_value="<|startoftext|>Termodinamik<|eos|>"):
            out = bt._translate_with_hymt_mlx("Thermodynamics", "English", "Turkish", 512)
        assert "<|eos|>" not in out
        assert "<|startoftext|>" not in out
        assert "Termodinamik" in out

    def test_mlx_import_error_falls_back(self, skeleton_translator):
        """mlx_lm.generate ImportError -> Transformers'a fallback."""
        bt = skeleton_translator(use_mlx=True)
        bt._translate_with_hymt_transformers = MagicMock(return_value="TF_FALLBACK")

        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "mlx_lm":
                raise ImportError("simulated MLX missing")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            out = bt._translate_with_hymt_mlx("hi", "English", "Turkish", 512)
        assert out == "TF_FALLBACK"
        assert bt._use_mlx is False

    def test_prompt_leak_stripped(self, skeleton_translator):
        """_strip_prompt_leaks uygulanır (defense-in-depth)."""
        bt = skeleton_translator(use_mlx=True)
        with patch("mlx_lm.generate", return_value="  Termodinamik ilkeleri.  "):
            out = bt._translate_with_hymt_mlx("Thermodynamic principles.", "English", "Turkish", 512)
        assert out.strip() == out  # trailing whitespace yok


# =============================================================================
# 7. _translate_with_hymt_transformers — Transformers path
# =============================================================================


class TestTranslateWithHymtTransformers:
    """Transformers yolu — tokenizer mock ile test edilebilir (platform-bağımsız)."""

    def test_transformers_path_calls_model_generate(self, skeleton_translator):
        """Tokenizer + model mock'lanmış olarak transformers yolu çalışıyor mu."""

        class _MockTensor:
            def __init__(self, data):
                self.data = data
                if isinstance(data, list) and data and isinstance(data[0], list):
                    self.shape = (len(data), len(data[0]))
                else:
                    self.shape = (len(data),)

            def to(self, device):
                return self

            def __getitem__(self, idx):
                if isinstance(idx, int):
                    return _MockTensor(self.data[idx])
                if isinstance(idx, slice):
                    return _MockTensor(self.data[idx])
                return self

        tokenizer_inputs = {
            "input_ids": _MockTensor([[1, 2, 3]]),
            "attention_mask": _MockTensor([[1, 1, 1]]),
        }

        class _Tokenizer:
            def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=False):
                return "fake prompt"

            def __call__(self, *args, **kwargs):
                return tokenizer_inputs

            def decode(self, tokens, skip_special_tokens=False):
                return "Termodinamik."

            eos_token_id = 2

        class _Model:
            def generate(self, **kwargs):
                # output shape: (1, 8) -- input_len=3, yeni 5 token
                return _MockTensor([[1, 2, 3, 10, 11, 12, 13, 14]])

        bt = skeleton_translator(use_mlx=False)
        bt.mt_tokenizer = _Tokenizer()
        bt.mt_model = _Model()

        # model.device yok (hasattr False) -> to() çağrılmaz
        out = bt._translate_with_hymt_transformers("Thermodynamics", "English", "Turkish", 512)
        assert "Termodinamik" in out

    def test_apply_chat_template_receives_messages(self, skeleton_translator):
        """Tokenizer.apply_chat_template instruction+text içeren messages alır."""
        bt = skeleton_translator(use_mlx=False)

        captured = {}

        class _Tok:
            def apply_chat_template(self, messages, **kwargs):
                captured["messages"] = messages
                return "p"

            def __call__(self, *a, **k):
                class _I:
                    data = {"input_ids": _FakeTensor([[1, 2]]), "attention_mask": _FakeTensor([[1, 1]])}
                    def __getitem__(self, k):
                        return self.data[k]
                    def items(self):
                        return self.data.items()
                return _I.data

            def decode(self, x, skip_special_tokens=False):
                return "TR"

            eos_token_id = 2

        class _M:
            def generate(self, **kwargs):
                return _FakeTensor([[1, 2, 3, 4]])

        bt.mt_tokenizer = _Tok()
        bt.mt_model = _M()

        bt._translate_with_hymt_transformers("hello world", "English", "Turkish", 512)
        msgs = captured["messages"]
        assert isinstance(msgs, list)
        assert msgs[0]["role"] == "user"
        assert "English" in msgs[0]["content"]
        assert "hello world" in msgs[0]["content"]


# =============================================================================
# 8. _translate_batch_hymt — Transformers batch (OOM fallback dahil)
# =============================================================================


class TestTranslateBatchHymt:
    def test_empty_list_returns_empty(self, skeleton_translator):
        bt = skeleton_translator()
        assert bt._translate_batch_hymt([]) == []

    def test_mlx_dispatch_when_use_mlx(self, skeleton_translator):
        bt = skeleton_translator(use_mlx=True)
        bt._translate_batch_mlx = MagicMock(return_value=["a", "b"])
        out = bt._translate_batch_hymt(["x", "y"])
        assert out == ["a", "b"]
        bt._translate_batch_mlx.assert_called_once()

    def test_oom_fallback_splits_batch(self, skeleton_translator):
        """OOM hatası -> batch ikiye bölünür, recursive tekrar."""
        bt = skeleton_translator(use_mlx=False)

        # Tokenizer minimal
        def _tok_call(*a, **k):
            return {"input_ids": _FakeTensor([[1, 2]]), "attention_mask": _FakeTensor([[1, 1]])}

        class _Tok:
            def apply_chat_template(self, *a, **k):
                return "p"

            def __call__(self, *a, **k):
                return _tok_call()

            def decode(self, x, skip_special_tokens=True):
                return "TR"

            eos_token_id = 2

        bt.mt_tokenizer = _Tok()

        call_count = {"n": 0}

        class _Model:
            def generate(self, **kwargs):
                call_count["n"] += 1
                # İlk çağrıda OOM at
                if call_count["n"] == 1:
                    raise RuntimeError("CUDA out of memory. Tried 2.5 GiB.")
                return _FakeTensor([[1, 2, 3, 4]])

        bt.mt_model = _Model()
        bt._translate_with_hymt_transformers = MagicMock(return_value="single_tr")

        out = bt._translate_batch_hymt(["abc", "def"])
        # OOM sonrası ikiye bölünmüş, her biri tek segment -> single call
        assert len(out) == 2

    def test_non_oom_error_reraised(self, skeleton_translator):
        bt = skeleton_translator(use_mlx=False)

        class _Tok:
            def apply_chat_template(self, *a, **k):
                return "p"

            def __call__(self, *a, **k):
                return {"input_ids": _FakeTensor([[1]]), "attention_mask": _FakeTensor([[1]])}

            eos_token_id = 2

            def decode(self, x, skip_special_tokens=True):
                return "x"

        class _Model:
            def generate(self, **kwargs):
                raise RuntimeError("value error unrelated to memory")

        bt.mt_tokenizer = _Tok()
        bt.mt_model = _Model()

        with pytest.raises(RuntimeError, match="value error"):
            bt._translate_batch_hymt(["abc"])


# Helper tensor class (modül seviyesinde)
class _FakeTensor:
    def __init__(self, data):
        self.data = data
        # shape: basit 2D varsayımı
        if isinstance(data, list) and data and isinstance(data[0], list):
            self.shape = (len(data), len(data[0]))
        else:
            self.shape = (len(data),)

    def to(self, device):
        return self

    def __iter__(self):
        return iter(self.data)

    def __getitem__(self, idx):
        if isinstance(idx, int):
            return _FakeTensor(self.data[idx] if self.shape else [])
        if isinstance(idx, slice):
            return _FakeTensor(self.data[idx])
        return self


# =============================================================================
# 9. _translate_batch_mlx — MLX batch (Mac-only)
# =============================================================================


@pytest.mark.skipif(not IS_MAC, reason="MLX sadece Mac'te")
@pytest.mark.skipif(not MLX_AVAILABLE, reason="mlx_lm yok")
class TestTranslateBatchMlx:
    def test_import_error_falls_back_to_transformers(self, skeleton_translator):
        bt = skeleton_translator(use_mlx=True)
        bt._translate_with_hymt_transformers = MagicMock(return_value="TF_ONE")

        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "mlx_lm":
                raise ImportError("no mlx")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            out = bt._translate_batch_mlx(["a", "b"])
        assert out == ["TF_ONE", "TF_ONE"]
        assert bt._use_mlx is False

    def test_single_text_uses_sequential_generate(self, skeleton_translator):
        """Tek metin -> batch_generate değil, sıralı generate kullanılır."""
        bt = skeleton_translator(use_mlx=True)
        with patch("mlx_lm.generate", return_value="Termodinamik") as mock_gen:
            out = bt._translate_batch_mlx(["Thermodynamics"])
        assert len(out) == 1
        assert "Termodinamik" in out[0]
        mock_gen.assert_called_once()

    def test_batch_generate_used_for_multiple(self, skeleton_translator):
        """Birden fazla metin + batch_generate varsa paralel çağrılır."""
        bt = skeleton_translator(use_mlx=True)

        # Tokenizer encode int list döner
        bt.mt_tokenizer = MagicMock()
        bt.mt_tokenizer.encode = MagicMock(return_value=[1, 2, 3])

        # batch_generate mock
        fake_resp = MagicMock()
        fake_resp.texts = ["Termo1", "Termo2"]

        with patch("mlx_lm.batch_generate", return_value=fake_resp) as mock_bg:
            with patch("mlx_lm.generate", return_value="SEQ"):
                out = bt._translate_batch_mlx(["T1", "T2"])
        assert len(out) == 2
        mock_bg.assert_called_once()

    def test_int_token_conversion_core_b09(self, skeleton_translator):
        """CORE-B09: tokenizer.encode list dönmezse int'e convert edilir."""
        bt = skeleton_translator(use_mlx=True)

        # encode mx.array-benzeri bir şey döndürüyor (list değil, iterable)
        class _FakeMxArray:
            def __init__(self, vals):
                self.vals = vals

            def __iter__(self):
                return iter(self.vals)

        bt.mt_tokenizer = MagicMock()
        bt.mt_tokenizer.encode = MagicMock(return_value=_FakeMxArray([1.0, 2.0, 3.0]))

        fake_resp = MagicMock()
        fake_resp.texts = ["a", "b"]

        captured_prompts = []

        def _fake_bg(model, tok, prompts, max_tokens):
            captured_prompts.extend(prompts)
            return fake_resp

        with patch("mlx_lm.batch_generate", side_effect=_fake_bg):
            with patch("mlx_lm.generate", return_value="SEQ"):
                out = bt._translate_batch_mlx(["x", "y"])

        # Her token int olmalı
        for prompt in captured_prompts:
            for tok in prompt:
                assert isinstance(tok, int), f"Token {tok} int değil ({type(tok)})"

    def test_batch_generate_failure_falls_back_sequential(self, skeleton_translator):
        """batch_generate hata atarsa sıralı generate'e düşer."""
        bt = skeleton_translator(use_mlx=True)
        bt.mt_tokenizer = MagicMock()
        bt.mt_tokenizer.encode = MagicMock(return_value=[1, 2])

        with patch("mlx_lm.batch_generate", side_effect=RuntimeError("batch failed")):
            with patch("mlx_lm.generate", return_value="SEQ") as mock_seq:
                out = bt._translate_batch_mlx(["a", "b"])

        assert len(out) == 2
        assert mock_seq.call_count == 2  # her metin için bir kez


# =============================================================================
# 10. translate_batch — public batch API
# =============================================================================


class TestTranslateBatchPublic:
    def test_empty_list(self, skeleton_translator):
        bt = skeleton_translator()
        assert bt.translate_batch([]) == []

    def test_chunks_by_batch_size(self, skeleton_translator):
        bt = skeleton_translator(batch_size=2)
        bt._translate_batch_hymt = MagicMock(side_effect=lambda batch, **kw: [f"T_{x}" for x in batch])
        out = bt.translate_batch(["a", "b", "c", "d", "e"])
        assert len(out) == 5
        # 5 metin, batch_size=2 -> 3 çağrı (2+2+1)
        assert bt._translate_batch_hymt.call_count == 3

    def test_show_progress_flag_does_not_crash(self, skeleton_translator):
        bt = skeleton_translator(batch_size=2)
        bt._translate_batch_hymt = MagicMock(side_effect=lambda batch, **kw: [f"T_{x}" for x in batch])
        out = bt.translate_batch(["a", "b"], show_progress=True)
        assert len(out) == 2


# =============================================================================
# 11. _translate_batch_segments — koruma dahil batch
# =============================================================================


class TestTranslateBatchSegments:
    def test_empty_returns_empty(self, skeleton_translator):
        bt = skeleton_translator()
        assert bt._translate_batch_segments([]) == []

    def test_memory_cache_hit_skips_mt(self, skeleton_translator):
        bt = skeleton_translator(use_memory=True)
        bt.memory = MagicMock()
        bt.memory.get_or_similar = MagicMock(return_value=("cached_tr", "exact"))
        bt._translate_batch_hymt = MagicMock(return_value=[])

        out = bt._translate_batch_segments(["cached text"])
        assert out == ["cached_tr"]
        bt._translate_batch_hymt.assert_not_called()
        assert bt.stats["segments_from_memory"] == 1

    def test_length_mismatch_preserves_originals(self, skeleton_translator):
        """CORE-B02: çeviri sayısı beklenenden az -> orijinal metin korunur."""
        bt = skeleton_translator(use_memory=False)
        # _translate_batch_hymt beklenenden az döndürür
        bt._translate_batch_hymt = MagicMock(return_value=["ceviri_1"])  # 1 elem, 2 bekleniyor

        # Translate _batch_segments içinde mismatch durumunda
        # `for i, trans_text in enumerate(translated_texts)` 1 elem işler,
        # sonra None kontrolü ile orijinal doldurulur.
        out = bt._translate_batch_segments(["metin A", "metin B"])
        assert len(out) == 2
        # İkinci metin çevrilemedi -> orijinal korunmalı (None yerine)
        assert out[1] == "metin B"

    def test_memory_not_poisoned_by_mt_fail(self, skeleton_translator):
        """CORE-B05: [MT_FAIL] başlayan çeviriler memory'ye yazılmamalı."""
        bt = skeleton_translator(use_memory=True)
        bt.memory = MagicMock()
        bt.memory.get_or_similar = MagicMock(return_value=(None, None))
        bt._translate_batch_hymt = MagicMock(return_value=["[MT_FAIL]failed_text"])

        bt._translate_batch_segments(["failing input"])
        bt.memory.add.assert_not_called()

    def test_successful_translation_added_to_memory(self, skeleton_translator):
        bt = skeleton_translator(use_memory=True)
        bt.memory = MagicMock()
        bt.memory.get_or_similar = MagicMock(return_value=(None, None))
        bt._translate_batch_hymt = MagicMock(return_value=["basarili"])

        bt._translate_batch_segments(["input"])
        bt.memory.add.assert_called_once()


# =============================================================================
# 12. _init_mt_model — engine dispatch
# =============================================================================


class TestInitMtModel:
    def test_unsupported_engine_raises(self, skeleton_translator):
        bt = skeleton_translator()
        bt.config.mt_engine = "unknown-engine"
        with pytest.raises(ValueError, match="Desteklenmeyen MT motoru"):
            bt._init_mt_model()

    def test_hymt_engine_calls_init_hymt(self, skeleton_translator):
        bt = skeleton_translator()
        bt._init_hymt_model = MagicMock()
        bt._init_mt_model()
        bt._init_hymt_model.assert_called_once_with(bt.config.hymt_7b_model)

    def test_init_failure_wrapped_in_runtime_error(self, skeleton_translator):
        bt = skeleton_translator()
        bt._init_hymt_model = MagicMock(side_effect=Exception("download failed"))
        with pytest.raises(RuntimeError, match="HY-MT 7B"):
            bt._init_mt_model()


# =============================================================================
# 13. _init_hymt_model — platform dispatch
# =============================================================================


class TestInitHymtModel:
    def test_mac_uses_mlx_path(self, skeleton_translator):
        bt = skeleton_translator()
        bt._init_hymt_mlx = MagicMock()
        bt._init_hymt_transformers = MagicMock()
        with patch("platform.system", return_value="Darwin"):
            bt._init_hymt_model("model_x")
        bt._init_hymt_mlx.assert_called_once_with("model_x")
        bt._init_hymt_transformers.assert_not_called()

    def test_linux_uses_transformers_path(self, skeleton_translator):
        bt = skeleton_translator()
        bt._init_hymt_mlx = MagicMock()
        bt._init_hymt_transformers = MagicMock()
        with patch("platform.system", return_value="Linux"):
            bt._init_hymt_model("model_x")
        bt._init_hymt_transformers.assert_called_once()
        bt._init_hymt_mlx.assert_not_called()

    def test_sets_lang_codes(self, skeleton_translator):
        bt = skeleton_translator(source_lang="en", target_lang="tr")
        bt._init_hymt_mlx = MagicMock()
        bt._init_hymt_transformers = MagicMock()
        bt._use_mlx = True
        with patch("platform.system", return_value="Linux"):
            bt._init_hymt_model("m")
        assert bt._hymt_src_lang == "en"
        assert bt._hymt_tgt_lang == "tr"


# =============================================================================
# 14. _init_hymt_mlx — MLX init (Mac-only)
# =============================================================================


@pytest.mark.skipif(not IS_MAC, reason="MLX init sadece Mac'te")
@pytest.mark.skipif(not MLX_AVAILABLE, reason="mlx_lm yok")
class TestInitHymtMlx:
    def test_missing_local_model_raises(self, skeleton_translator, tmp_path):
        """Local MLX modeli yoksa FileNotFoundError fırlatır."""
        bt = skeleton_translator()
        # models dizinini var olmayan bir yola yönlendirmek zor, direkt
        # Path.exists'i mock'layalım.
        with patch("pathlib.Path.exists", return_value=False):
            with pytest.raises(FileNotFoundError, match="MLX model not found"):
                bt._init_hymt_mlx("tencent/HY-MT1.5-7B")

    def test_local_model_calls_mlx_load(self, skeleton_translator):
        bt = skeleton_translator()
        fake_model = object()
        fake_tok = object()
        with patch("pathlib.Path.exists", return_value=True):
            with patch("mlx_lm.load", return_value=(fake_model, fake_tok)) as mock_load:
                bt._init_hymt_mlx("tencent/HY-MT1.5-7B")
        assert bt.mt_model is fake_model
        assert bt.mt_tokenizer is fake_tok
        assert bt._use_mlx is True
        mock_load.assert_called_once()


# =============================================================================
# 15. _init_hymt_transformers — Transformers init (Non-Mac preferred)
# =============================================================================


class TestInitHymtTransformers:
    def test_transformers_loader_called(self, skeleton_translator):
        """transformers AutoModel/Tokenizer çağrılır, _use_mlx False olur."""
        bt = skeleton_translator()
        fake_tok = MagicMock()
        fake_model = MagicMock()

        with patch("transformers.AutoTokenizer.from_pretrained", return_value=fake_tok) as mt:
            with patch("transformers.AutoModelForCausalLM.from_pretrained", return_value=fake_model) as mm:
                # torch zaten venv'de kurulu olabilir; değilse patch
                try:
                    import torch  # noqa: F401
                    bt._init_hymt_transformers("test-model")
                except ImportError:
                    pytest.skip("torch kurulu değil, transformers init test edilemedi")

        assert bt.mt_tokenizer is fake_tok
        assert bt.mt_model is fake_model
        assert bt._use_mlx is False


# =============================================================================
# 16. _protect_references / _restore_references — CORE-B23
# =============================================================================


class TestProtectReferences:
    """_protect_references / _restore_references round-trip + CORE-B23 genişletme."""

    def test_ref_protected_and_restored(self, skeleton_translator):
        bt = skeleton_translator()
        text = r"See \ref{eq:1} for details."
        protected, refs = bt._protect_references(text)
        assert r"\ref{eq:1}" not in protected
        assert "[[REF_" in protected
        assert refs == [r"\ref{eq:1}"]
        restored = bt._restore_references(protected, refs)
        assert r"\ref{eq:1}" in restored
        assert restored == text

    def test_cite_variant_citet(self, skeleton_translator):
        """CORE-B23: \\citet yakalanmalı."""
        bt = skeleton_translator()
        text = r"See \citet{smith2020}"
        _, refs = bt._protect_references(text)
        assert len(refs) == 1
        assert refs[0] == r"\citet{smith2020}"

    def test_cite_variant_citep(self, skeleton_translator):
        bt = skeleton_translator()
        text = r"(\citep{doe2019})"
        _, refs = bt._protect_references(text)
        assert len(refs) == 1
        assert refs[0] == r"\citep{doe2019}"

    def test_cref_and_nameref(self, skeleton_translator):
        """CORE-B23: \\Cref ve \\nameref yakalanmalı."""
        bt = skeleton_translator()
        text = r"See \Cref{sec:intro} and \nameref{sec:foo}"
        _, refs = bt._protect_references(text)
        assert len(refs) == 2

    def test_fullcite_textcite(self, skeleton_translator):
        bt = skeleton_translator()
        text = r"\fullcite{key} and \textcite{other}"
        _, refs = bt._protect_references(text)
        assert len(refs) == 2

    def test_eqref_and_pageref(self, skeleton_translator):
        bt = skeleton_translator()
        text = r"\eqref{eq:1}, p. \pageref{sec:2}"
        _, refs = bt._protect_references(text)
        assert len(refs) == 2

    def test_label_protected(self, skeleton_translator):
        bt = skeleton_translator()
        text = r"\label{sec:intro}"
        _, refs = bt._protect_references(text)
        assert len(refs) == 1

    def test_multiple_refs_in_one_text(self, skeleton_translator):
        bt = skeleton_translator()
        text = r"See \ref{eq:1} and \citep{key_a-b} and \Cref{fig:2}"
        protected, refs = bt._protect_references(text)
        assert len(refs) == 3
        # Restore round-trip
        restored = bt._restore_references(protected, refs)
        assert restored == text

    def test_empty_text_no_refs(self, skeleton_translator):
        bt = skeleton_translator()
        protected, refs = bt._protect_references("")
        assert protected == ""
        assert refs == []

    def test_restore_with_empty_list(self, skeleton_translator):
        bt = skeleton_translator()
        # Hiç referans yoksa metin aynen döner
        assert bt._restore_references("normal text", []) == "normal text"

    def test_restore_missing_placeholder_preserved(self, skeleton_translator):
        """Çeviride [[REF_i]] kaybolmuşsa restore sessiz şekilde çalışır."""
        bt = skeleton_translator()
        # Birden fazla ref + biri kaybolmuş
        restored = bt._restore_references("no placeholder here", [r"\ref{x}"])
        assert restored == "no placeholder here"


# =============================================================================
# 17. _protect_formulas / _restore_formulas
# =============================================================================


class TestProtectFormulas:
    def test_inline_dollar_protected(self, skeleton_translator):
        bt = skeleton_translator()
        text = "Einstein said $E=mc^2$ is key."
        protected, formulas = bt._protect_formulas(text)
        assert "$E=mc^2$" not in protected
        assert "[[FORMULA_" in protected
        assert formulas == ["$E=mc^2$"]

    def test_double_dollar_display_math(self, skeleton_translator):
        bt = skeleton_translator()
        text = r"$$\int_0^1 x dx$$"
        protected, formulas = bt._protect_formulas(text)
        assert len(formulas) == 1
        assert "[[FORMULA_" in protected

    def test_bracket_display_math(self, skeleton_translator):
        bt = skeleton_translator()
        text = r"Equation: \[ a + b = c \]"
        protected, formulas = bt._protect_formulas(text)
        assert len(formulas) == 1

    def test_paren_inline_math(self, skeleton_translator):
        bt = skeleton_translator()
        text = r"Inline: \( x \)"
        protected, formulas = bt._protect_formulas(text)
        assert len(formulas) == 1

    def test_begin_env_math(self, skeleton_translator):
        bt = skeleton_translator()
        text = r"\begin{equation} x = y \end{equation}"
        protected, formulas = bt._protect_formulas(text)
        assert len(formulas) == 1

    def test_round_trip(self, skeleton_translator):
        bt = skeleton_translator()
        text = "See $a+b$ and $$c-d$$"
        protected, formulas = bt._protect_formulas(text)
        restored = bt._restore_formulas(protected, formulas)
        assert restored == text

    def test_no_formulas(self, skeleton_translator):
        bt = skeleton_translator()
        protected, formulas = bt._protect_formulas("no math here")
        assert protected == "no math here"
        assert formulas == []


# =============================================================================
# 18. _protect_units / _restore_units
# =============================================================================


class TestProtectUnits:
    def test_simple_unit(self, skeleton_translator):
        bt = skeleton_translator()
        text = "The mass is 5 kg approximately."
        protected, units = bt._protect_units(text)
        assert "5 kg" not in protected
        assert "[[UNIT_" in protected
        assert len(units) == 1

    def test_multiple_units(self, skeleton_translator):
        bt = skeleton_translator()
        text = "Speed 10 m/s, frequency 50 Hz, temp 300 K"
        protected, units = bt._protect_units(text)
        assert len(units) >= 2

    def test_decimal_unit(self, skeleton_translator):
        bt = skeleton_translator()
        text = "Distance 3.14 m"
        protected, units = bt._protect_units(text)
        assert len(units) == 1

    def test_round_trip(self, skeleton_translator):
        bt = skeleton_translator()
        text = "5 kg and 10 m/s"
        protected, units = bt._protect_units(text)
        restored = bt._restore_units(protected, units)
        assert restored == text

    def test_no_units(self, skeleton_translator):
        bt = skeleton_translator()
        protected, units = bt._protect_units("just plain text")
        assert protected == "just plain text"
        assert units == []

    def test_temperature_celsius(self, skeleton_translator):
        bt = skeleton_translator()
        text = "300 °C is hot"
        protected, units = bt._protect_units(text)
        # °C pattern'de var
        assert len(units) >= 1
