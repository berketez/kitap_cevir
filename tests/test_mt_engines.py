"""core/mt_engines/ katmanı testleri — tak-çıkar motor desteği (2026-07-27).

Kapsam:
- Registry: builtin motor, bilinmeyen isim hatası, kullanıcı JSON'u yükleme,
  bozuk girdilerin fail-open atlanması, builtin ezme koruması, path guard.
- HY-MT format: prompt'un ESKİ gömülü biçimle bayt-bayt aynılığı, özel
  token temizliği.
- Chat format: apply_chat_template kullanımı, template'siz fallback,
  strip_tokens süzme.
- BookTranslator entegrasyonu (model YÜKLEMEDEN): lazy engine property,
  bilinmeyen motorun anlaşılır hatayla reddi, prompt'un motordan gelmesi.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.mt_engines import (
    HYMT_SPECIAL_TOKENS,
    ChatTemplateEngine,
    EngineSpec,
    HYMTFormatEngine,
    available_engines,
    create_engine,
    load_user_specs,
)


# =============================================================================
# 1. Registry
# =============================================================================


class TestRegistry:
    def test_builtin_hymt_always_available(self):
        engs = available_engines()
        assert "hymt-7b" in engs
        assert engs["hymt-7b"].format == "hymt"
        assert engs["hymt-7b"].local_dir == "hymt-7b-mlx"

    def test_unknown_engine_error_lists_available(self):
        with pytest.raises(ValueError) as exc:
            create_engine("yok-boyle-motor")
        msg = str(exc.value)
        assert "yok-boyle-motor" in msg
        assert "hymt-7b" in msg  # kullanılabilir liste mesajda

    def test_user_specs_loaded_from_json(self, tmp_path):
        cfg = tmp_path / "mt_engines.json"
        cfg.write_text(json.dumps({
            "yeni-model": {
                "format": "chat",
                "model_id": "org/Yeni-7B",
                "local_dir": "yeni-model-mlx",
                "description": "Yeni model",
                "strip_tokens": ["<|im_end|>"],
            }
        }), encoding="utf-8")
        engs = available_engines(user_specs_path=cfg)
        assert "yeni-model" in engs
        assert engs["yeni-model"].source == "user"
        eng = create_engine("yeni-model", user_specs_path=cfg)
        assert isinstance(eng, ChatTemplateEngine)

    def test_missing_user_file_is_fine(self, tmp_path):
        assert load_user_specs(tmp_path / "yok.json") == {}

    def test_broken_json_fails_open(self, tmp_path):
        cfg = tmp_path / "mt_engines.json"
        cfg.write_text("{bozuk json", encoding="utf-8")
        assert load_user_specs(cfg) == {}

    def test_invalid_entries_skipped_not_fatal(self, tmp_path):
        cfg = tmp_path / "mt_engines.json"
        cfg.write_text(json.dumps({
            "_aciklama": "yorum satırı — sözlük değil, atlanmalı",
            "bilinmeyen-format": {"format": "olmayan-bicim"},
            "gecerli": {"format": "chat", "local_dir": "gecerli-mlx"},
        }), encoding="utf-8")
        specs = load_user_specs(cfg)
        assert "gecerli" in specs
        assert "bilinmeyen-format" not in specs
        assert "_aciklama" not in specs

    def test_user_cannot_override_builtin(self, tmp_path):
        cfg = tmp_path / "mt_engines.json"
        cfg.write_text(json.dumps({
            "hymt-7b": {"format": "chat", "local_dir": "sahte-mlx"},
        }), encoding="utf-8")
        engs = available_engines(user_specs_path=cfg)
        # builtin spec korunur (format hymt kalır)
        assert engs["hymt-7b"].format == "hymt"
        assert engs["hymt-7b"].source == "builtin"

    def test_local_dir_path_traversal_rejected(self, tmp_path):
        cfg = tmp_path / "mt_engines.json"
        cfg.write_text(json.dumps({
            "kotu": {"format": "chat", "local_dir": "../../.ssh"},
            "kotu2": {"format": "chat", "local_dir": "a/b"},
            # Reviewer R5: tek segmentlik '..' regex'i geçiyordu
            "kotu3": {"format": "chat", "local_dir": ".."},
            "kotu4": {"format": "chat", "local_dir": "."},
        }), encoding="utf-8")
        specs = load_user_specs(cfg)
        assert specs == {}


# =============================================================================
# 2. HY-MT format motoru — eski gömülü davranışla birebir eşitlik
# =============================================================================


class TestHYMTFormat:
    def _engine(self):
        return create_engine("hymt-7b")

    def test_mlx_prompt_matches_legacy_format_exactly(self):
        """Eski core/translator.py sabit f-string'i ile bayt-bayt aynı."""
        instruction = "Translate from English to Turkish. Output only the translation."
        text = "Hello $x^2$ world"
        legacy = f"<|startoftext|>{instruction}<|extra_4|>{text}<|extra_0|>"
        assert self._engine().build_mlx_prompt(None, instruction, text) == legacy

    def test_clean_output_strips_all_special_tokens(self):
        raw = "  Merhaba<|eos|> dünya<|extra_0|><|extra_4|><|startoftext|>  "
        assert self._engine().clean_output(raw) == "Merhaba dünya"

    def test_special_token_list_unchanged(self):
        # Eski _special_tokens listesiyle aynı küme (sözleşme koruması)
        assert set(HYMT_SPECIAL_TOKENS) == {
            "<|eos|>", "<|extra_0|>", "<|extra_4|>", "<|startoftext|>",
        }

    def test_chat_messages_single_user_message(self):
        msgs = self._engine().build_chat_messages("TALIMAT", "METIN")
        assert msgs == [{"role": "user", "content": "TALIMAT\n\nMETIN"}]

    def test_lang_names_default_table(self):
        e = self._engine()
        assert e.lang_name("en") == "English"
        assert e.lang_name("tr") == "Turkish"
        assert e.supports_lang("de") and not e.supports_lang("xx")

    def test_resolve_local_dir_prefers_plain_then_nosync(self, tmp_path):
        e = self._engine()
        # hiçbiri yok -> None
        assert e.resolve_local_model_dir(tmp_path) is None
        # sadece .nosync var -> .nosync
        (tmp_path / "hymt-7b-mlx.nosync").mkdir()
        assert e.resolve_local_model_dir(tmp_path).name == "hymt-7b-mlx.nosync"
        # ikisi de var -> düz ad öncelikli
        (tmp_path / "hymt-7b-mlx").mkdir()
        assert e.resolve_local_model_dir(tmp_path).name == "hymt-7b-mlx"


# =============================================================================
# 3. Chat format motoru
# =============================================================================


class TestChatFormat:
    def _engine(self, **spec_kw):
        spec = EngineSpec(name="test-chat", format="chat", **spec_kw)
        return ChatTemplateEngine(spec)

    def test_uses_tokenizer_chat_template(self):
        tok = MagicMock()
        tok.apply_chat_template.return_value = "<CHAT>TALIMAT/METIN</CHAT>"
        out = self._engine().build_mlx_prompt(tok, "TALIMAT", "METIN")
        assert out == "<CHAT>TALIMAT/METIN</CHAT>"
        _, kwargs = tok.apply_chat_template.call_args
        assert kwargs["tokenize"] is False
        assert kwargs["add_generation_prompt"] is True

    def test_fallback_when_no_chat_template(self):
        tok = MagicMock()
        tok.apply_chat_template.side_effect = AttributeError("template yok")
        out = self._engine().build_mlx_prompt(tok, "TALIMAT", "METIN")
        assert "TALIMAT" in out and "METIN" in out

    def test_strip_tokens_from_spec(self):
        e = self._engine(strip_tokens=["<|im_end|>", "<|endoftext|>"])
        assert e.clean_output("Merhaba<|im_end|><|endoftext|>") == "Merhaba"

    def test_custom_languages_table(self):
        e = self._engine(languages={"en": "English", "tr": "Turkish"})
        assert e.supports_lang("tr") and not e.supports_lang("de")


# =============================================================================
# 4. BookTranslator entegrasyonu (model yüklemeden)
# =============================================================================


def _skeleton(mt_engine="hymt-7b"):
    """__init__'i atlayarak çıplak BookTranslator (test kalıbı part1 ile aynı)."""
    from core.translator import BookTranslator, TranslationConfig
    bt = BookTranslator.__new__(BookTranslator)
    bt.config = TranslationConfig(mt_engine=mt_engine, glossary_mode="off",
                                  use_memory=False)
    bt.mt_model = MagicMock()
    bt.mt_tokenizer = MagicMock()
    bt._use_mlx = True
    return bt


class TestBookTranslatorIntegration:
    def test_lazy_engine_property_resolves_default(self):
        bt = _skeleton()
        assert bt.engine.spec.name == "hymt-7b"
        # aynı örnek cache'lenir
        assert bt.engine is bt.engine

    def test_unknown_engine_raises_with_available_list(self):
        bt = _skeleton(mt_engine="firlama-motor")
        with pytest.raises(ValueError) as exc:
            _ = bt.engine
        assert "hymt-7b" in str(exc.value)

    def test_mlx_single_prompt_comes_from_engine(self, monkeypatch):
        """MLX tekil yol prompt'u motordan alır — sahte motorla kanıt."""
        bt = _skeleton()

        captured = {}

        def fake_generate(model, tokenizer, prompt=None, max_tokens=None):
            captured["prompt"] = prompt
            return "SONUÇ<|eos|>"

        import core.translator as ct
        monkeypatch.setattr("mlx_lm.generate", fake_generate, raising=False)

        out = bt._translate_with_hymt_mlx("Hello", "English", "Turkish", 128)
        assert out == "SONUÇ"
        assert captured["prompt"].startswith("<|startoftext|>")
        assert captured["prompt"].endswith("Hello<|extra_0|>")

    def test_engine_swap_changes_prompt_format(self, monkeypatch, tmp_path):
        """Kayıtlı bir chat motoru seçilirse prompt chat template'ten gelir
        — kod değişikliği olmadan model değiştirmenin kanıtı."""
        cfg = tmp_path / "mt_engines.json"
        cfg.write_text(json.dumps({
            "sahte-chat": {"format": "chat", "local_dir": "sahte-mlx"},
        }), encoding="utf-8")

        import core.mt_engines.registry as reg
        monkeypatch.setattr(reg, "USER_SPECS_PATH", cfg)

        bt = _skeleton(mt_engine="sahte-chat")
        bt.mt_tokenizer.apply_chat_template.return_value = "CHAT_PROMPT"

        captured = {}

        def fake_generate(model, tokenizer, prompt=None, max_tokens=None):
            captured["prompt"] = prompt
            return "ÇEVİRİ"

        monkeypatch.setattr("mlx_lm.generate", fake_generate, raising=False)

        out = bt._translate_with_hymt_mlx("Hello", "English", "Turkish", 128)
        assert captured["prompt"] == "CHAT_PROMPT"
        assert out == "ÇEVİRİ"
