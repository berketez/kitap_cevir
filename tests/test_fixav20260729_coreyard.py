# -*- coding: utf-8 -*-
"""2026-07-29 bug avı — core yardımcı katmanı regresyon testleri.

Kapsam (6 doğrulanmış bulgu):
- F22: MT motorlarında kaynak metin özel-token enjeksiyonu (hymt + chat)
- F23: SentenceSplitter._split_long_sentence'in ölü virgül+bağlaç dalı
- F24: book_translator_v2.get_file_hash yalnız ilk 1MB'ı hash'liyordu
- F40: v2 batch yolunda metin-bazlı yeniden kullanım (kopya segment) yoktu
  (F24 ve F40 testleri 5 Ekim 2026'da kaldırıldı: modül ölü kod olarak
  silindi, KARAR 27; aşağıdaki not bloğuna bakın)
- F44: NER tolerant restore'da 'UNKNOWN_1' -> 'UNKNOWN_10' substring çakışması
- F45: QAChecker.UNIT_PATTERN alternation sırası bileşik birimleri ölü bırakıyordu
"""

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.mt_engines.base import EngineSpec, MTEngine
from core.mt_engines.chat import ChatTemplateEngine
from core.mt_engines.hymt import HYMT_SPECIAL_TOKENS
from core.mt_engines.registry import create_engine
from core.ner_protector import NERProtector
from core.qa_checker import QAChecker
from core.sentence_splitter import SentenceSplitter


# =============================================================================
# F22 — Kaynak metindeki özel token'lar prompt yapısını bozmamalı
# =============================================================================


class TestF22SpecialTokenInjection:
    _INSTR = "Translate from English to Turkish. Output only the translation."

    def _hymt(self):
        return create_engine("hymt-7b")

    def test_hymt_prompt_has_exactly_one_structural_token_set(self):
        """Metindeki literal <|extra_0|> prompt'u ortadan bölmemeli."""
        text = "See the marker <|extra_0|> used in LLM prompts."
        prompt = self._hymt().build_mlx_prompt(None, self._INSTR, text)
        for tok in ("<|startoftext|>", "<|extra_4|>", "<|extra_0|>"):
            assert prompt.count(tok) == 1, (
                f"{tok} prompt'ta {prompt.count(tok)} kez geçiyor — "
                "kaynak metindeki token kaçışsız gömülmüş")
        # Yapısal token'lar doğru sırada ve <|extra_0|> en sonda
        assert prompt.startswith("<|startoftext|>")
        assert prompt.endswith("<|extra_0|>")

    def test_hymt_prompt_neutralizes_all_engine_tokens(self):
        text = " ".join(f"x{t}y" for t in HYMT_SPECIAL_TOKENS)
        prompt = self._hymt().build_mlx_prompt(None, "T", text)
        body = prompt[len("<|startoftext|>T<|extra_4|>"):-len("<|extra_0|>")]
        for tok in HYMT_SPECIAL_TOKENS:
            assert tok not in body, f"{tok} metin gövdesinde ham kaldı"

    def test_hymt_legacy_format_unchanged_for_clean_text(self):
        """Özel token içermeyen metinde biçim eskisiyle bayt-bayt aynı."""
        text = "Hello $x^2$ world"
        legacy = f"<|startoftext|>{self._INSTR}<|extra_4|>{text}<|extra_0|>"
        assert self._hymt().build_mlx_prompt(None, self._INSTR, text) == legacy

    def test_sanitize_preserves_content_and_is_idempotent(self):
        e = self._hymt()
        once = e.sanitize_input("bkz. <|extra_0|> isareti")
        assert "extra_0" in once, "içerik silinmemeli, yalnız nötralize edilmeli"
        assert "<|extra_0|>" not in once
        assert e.sanitize_input(once) == once

    def test_sanitize_never_empties_text(self):
        e = self._hymt()
        assert e.sanitize_input("<|extra_0|>").strip() != ""

    def test_chat_messages_defuse_chat_tokens(self):
        """chat formatında içerikteki <|im_end|> template'i erken kapatmamalı."""
        spec = EngineSpec(name="t", format="chat",
                          strip_tokens=["<|im_end|>", "</s>"])
        engine = ChatTemplateEngine(spec)
        msgs = engine.build_chat_messages("TALIMAT", "Metin <|im_end|> ve </s> son")
        content = msgs[0]["content"]
        assert "<|im_end|>" not in content and "</s>" not in content
        assert "im_end" in content and "/s" in content

    def test_chat_fallback_path_sanitizes(self):
        class _Tok:
            def apply_chat_template(self, *a, **kw):
                raise AttributeError("template yok")

        engine = ChatTemplateEngine(EngineSpec(name="t", format="chat"))
        out = engine.build_mlx_prompt(_Tok(), "TALIMAT", "x <|endoftext|> y")
        assert "<|endoftext|>" not in out
        assert "TALIMAT" in out and "endoftext" in out

    def test_base_sanitize_catches_unknown_pipe_tokens(self):
        """strip_tokens'ta olmayan <|...|> biçimli token'lar da nötralize."""
        engine = MTEngine(EngineSpec(name="t"))
        out = engine.sanitize_input("a <|reserved_special_token_7|> b")
        assert "<|reserved_special_token_7|>" not in out
        assert "reserved_special_token_7" in out

    def test_plain_angle_text_untouched(self):
        """Normal '<' / '|' içeren metin bozulmamalı (yanlış pozitif yok)."""
        engine = MTEngine(EngineSpec(name="t"))
        text = "if a < b and c | d then <html> tag"
        assert engine.sanitize_input(text) == text

    @pytest.mark.skipif(
        not (PROJECT_ROOT / "models" / "hymt-7b-mlx").exists(),
        reason="HY-MT tokenizer yerelde yok",
    )
    def test_real_tokenizer_no_injected_special_id(self):
        """Gerçek tokenizer: metindeki token artık özel id'ye çevrilmiyor."""
        transformers = pytest.importorskip("transformers")
        tok = transformers.AutoTokenizer.from_pretrained(
            str(PROJECT_ROOT / "models" / "hymt-7b-mlx"), trust_remote_code=False,
        )
        marker_id = tok.convert_tokens_to_ids("<|extra_0|>")
        if marker_id is None or marker_id == tok.unk_token_id:
            pytest.skip("tokenizer <|extra_0|> özel token'ı tanımıyor")
        text = "See the marker <|extra_0|> used in LLM prompts."
        prompt = self._hymt().build_mlx_prompt(tok, self._INSTR, text)
        ids = tok.encode(prompt, add_special_tokens=False)
        assert ids.count(marker_id) == 1, (
            "kaynak metindeki <|extra_0|> hâlâ gerçek özel token'a tokenize oluyor")


# =============================================================================
# F23 — Uzun cümle bölme: virgül+bağlaç dalı çalışmalı
# =============================================================================


class TestF23LongSentenceSplit:
    _SENT = ("The first clause is fairly long here, and the second clause "
             "continues even longer than that, but the third clause finally "
             "arrives, therefore the sentence ends.")

    def test_comma_conjunction_branch_splits(self):
        parts = SentenceSplitter(max_length=50)._split_long_sentence(self._SENT)
        assert len(parts) >= 2, "virgül+bağlaç dalı hâlâ ölü (tek parça döndü)"
        assert "".join(p.replace(" ", "") for p in parts) == \
            self._SENT.replace(" ", ""), "bölme sırasında içerik kayboldu"

    def test_split_points_are_before_conjunction(self):
        parts = SentenceSplitter(max_length=45)._split_long_sentence(self._SENT)
        assert parts[0].endswith(","), f"ilk parça virgülde bitmeli: {parts[0]!r}"
        assert parts[1].startswith("and "), f"ikinci parça bağlaçla başlamalı: {parts[1]!r}"

    def test_semicolon_branch_still_works(self):
        s = ("a fairly long first clause for the test indeed; "
             "and a second clause that is also long enough")
        parts = SentenceSplitter(max_length=40)._split_long_sentence(s)
        assert len(parts) == 2 and parts[0].endswith(";")

    def test_word_boundary_prevents_false_split(self):
        """', android' bağlaç sayılmamalı (\\b koruması)."""
        s = "This clause is long enough for a test, android phones are common here."
        parts = SentenceSplitter(max_length=40)._split_long_sentence(s)
        assert len(parts) == 1, f"'android' bağlaç sanıldı: {parts!r}"

    def test_short_sentence_untouched(self):
        s = "Short one, and done."
        assert SentenceSplitter(max_length=1000)._split_long_sentence(s) == [s]

    def test_public_split_breaks_long_run_on(self):
        long_clause = "the measured value increases steadily over the interval"
        sentence = ", and ".join([long_clause] * 8) + "."
        out = SentenceSplitter(min_length=1, max_length=120).split(sentence)
        assert len(out) >= 2, "uzun run-on cümle MT'ye tek parça gidiyor"
        assert all(len(p) <= 200 for p in out)


# =============================================================================
# F24 / F40 — KALDIRILDI (5 Ekim 2026)
# =============================================================================
# Sınadıkları core/book_translator_v2.py (get_file_hash, _translate_segments)
# giriş noktalarından erişilmeyen ölü kod olarak silindi (KARAR 27); testlerin
# son hâli git etiketi arsiv/olu-kod-20261005 üzerinde. Canlı
# core/translator.get_file_hash'in kuyruk değişikliği bekçisi:
# tests/test_serbestlik_motor_20260727.py::TestB10FileHash.


# =============================================================================
# F44 — NER tolerant restore substring çakışması
# =============================================================================


def _mapping(*pairs):
    out = {}
    for name, idx in pairs:
        out[f"@@UNKNOWN{idx:04d}@@"] = {
            "original": name, "category": "UNKNOWN", "index": idx,
        }
    return out


class TestF44NerSubstring:
    def test_bare_residue_does_not_match_longer_index(self):
        m = _mapping(("Alice", 1), ("Bob", 10))
        out = NERProtector().restore("UNKNOWN_10 metni", m)
        assert out == "Bob metni", f"substring çakışması sürüyor: {out!r}"
        assert "Alice" not in out

    def test_both_bare_residues_restored_correctly(self):
        m = _mapping(("Alice", 1), ("Bob", 10))
        out = NERProtector().restore("UNKNOWN_1 ile UNKNOWN_10 konustu", m)
        assert out == "Alice ile Bob konustu", out

    def test_turkish_bare_residue(self):
        m = _mapping(("Ali", 2), ("Veli", 20))
        out = NERProtector().restore("BILINMEYEN_20 geldi", m)
        assert out == "Veli geldi", out

    def test_exact_placeholders_still_restored(self):
        m = _mapping(("Alice", 1), ("Bob", 10))
        out = NERProtector().restore("@@UNKNOWN0001@@ ve @@UNKNOWN0010@@", m)
        assert out == "Alice ve Bob", out

    def test_replacement_with_regex_escapes_is_literal(self):
        """Orijinal metinde \\1 gibi diziler grup referansı sanılmamalı."""
        m = {"@@UNKNOWN0003@@": {"original": r"C:\1\path",
                                 "category": "UNKNOWN", "index": 3}}
        out = NERProtector().restore("UNKNOWN_3 yolu", m)
        assert out == r"C:\1\path yolu", out

    def test_bare_residue_not_matched_inside_word(self):
        m = _mapping(("Alice", 1))
        out = NERProtector().restore("XUNKNOWN_1 kaldi", m)
        assert out.startswith("X"), f"kelime içi eşleşme oldu: {out!r}"


# =============================================================================
# F45 — Bileşik birimler
# =============================================================================


class TestF45CompoundUnits:
    @pytest.mark.parametrize("text,expected", [
        ("50 m/s", "m/s"),
        ("9.81 m/s²", "m/s²"),
        ("10 km/h", "km/h"),
        ("5 kg/m³", "kg/m³"),
        ("2 g/cm³", "g/cm³"),
        ("7 N/m²", "N/m²"),
        ("8 N/m", "N/m"),
    ])
    def test_compound_unit_matched_whole(self, text, expected):
        assert QAChecker.UNIT_PATTERN.findall(text) == [expected]

    @pytest.mark.parametrize("text,expected", [
        ("12 mm", "mm"), ("1.5 kg", "kg"), ("3 mol", "mol"),
        ("25 °C", "°C"), ("100 Hz", "Hz"), ("40 W", "W"), ("60 s", "s"),
    ])
    def test_simple_units_unchanged(self, text, expected):
        assert QAChecker.UNIT_PATTERN.findall(text) == [expected]

    def test_multiple_units_in_sentence(self):
        found = QAChecker.UNIT_PATTERN.findall("Speed 50 m/s, mass 3 kg, 10 km/h.")
        assert found == ["m/s", "kg", "km/h"]

    def test_missing_compound_unit_is_reported(self):
        """Hedefte '/s' düşerse birim kaybı raporlanmalı (eskiden kördü)."""
        issues = QAChecker()._check_units(
            "The speed is 50 m/s here.", "Hiz burada 50 m.", 0)
        assert any(i.category == "unit" and i.severity == "error"
                   and "m/s" in i.message for i in issues), (
            f"birim kaybı raporlanmadı: {[i.message for i in issues]}")

    def test_matching_units_produce_no_issue(self):
        issues = QAChecker()._check_units(
            "The speed is 50 m/s here.", "Hiz burada 50 m/s.", 0)
        assert issues == []
