"""T kulvarı (28 Eylül 2026; KARARLAR 15-16): çıktı token kuralı + düşünme bütçesi.

Kapsam (madde -> sınıf):

- T1 token kuralı tek yerde (``backends.output_token_limit``), sayılar
  config/defaults.py'de; bütün arka uçlar aynı sınırı alır  -> TestTokenKurali
- T2 sınıra dayanan çıktı sessiz geçmez (uyarı + ``segments_truncated``;
  MLX'te tokenizer ile ölçülür, çıktı değişmez)             -> TestSinirdaCikti
- T3 düşünme her yanıtta ölçülür (ayrı alan, ``<think>``, harmony, sunucu
  sayacı; akışta parça sayımı)                                -> TestDusunmeOlcumu
- T4 düşünen modelde akış + bütçede hemen kesme; düşünmeyen modelde gövde
  aynı; ``options.think_budget``                               -> TestButceAkis
- T5 sert kapatmalı yeniden deneme (yalnız belgeli yollar), "çevrilmedi"
  işareti, boş çıktı asla kabul edilmez                        -> TestSertYenidenDeneme
- T6 koşu devre kesicisi (ThinkingBudgetExceeded, çıkış 5) — birim ve
  translate_book uçtan uca                                    -> TestDevreKesici
- T7 akışta toplam süre sınırı (sunucu sessizken de)           -> TestAkisSuresi
- T8 ileti kataloğu tek yerde, contracts belge satırı          -> TestIletiKatalogu
- T9 doküman örneği (think_budget) kayıt olarak yüklenir      -> TestDokuman

Hiçbir gerçek model YÜKLENMEZ, gerçek Ollama / LM Studio'ya gidilmez: sahte
HTTP sunucusu, sahte ``llama_cpp`` ve sahte ``mlx_lm`` (yalnız test altyapısı).
Girdiler sentetiktir; çeviri belleği kapalı ya da geçicidir.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
import sys
import time
import types
from pathlib import Path

import pytest

import core.mt_engines as cme
from config import defaults as D
from core.mt_engines import backends as B
from core.mt_engines import contracts as C
from core.mt_engines import registry
from core.mt_engines.backends import create_backend
from core.mt_engines.base import EngineSpec
from core.mt_engines.chat import ChatTemplateEngine
from core.mt_engines.contracts import (
    MSG_EMPTY_OUTPUT,
    MSG_THINK_BREAKER,
    MSG_THINK_BUDGET,
    BackendUnavailable,
    GenerationRequest,
    GenerationResult,
    ThinkingBudgetExceeded,
)
from core.mt_engines.hymt import HYMTFormatEngine
from core.translator import BookTranslator, TranslationConfig, _active_backend
from tests.fakes import FakeModel, FakeReply, write_fake_gguf

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INSTR = "Translate from English to Turkish. Output only the translation."
TEXT = "The weather is nice today."
LONG_THINK = " ".join(["düşünce"] * 3000)          # bütçenin (512) çok üstü
SHORT_THINK = "Kısa bir düşünce."
BUDGET = D.MT_THINK_BUDGET_TOKENS
_OFF = dict(use_memory=False, glossary_mode="off", run_qa=False, check_consistency=False)


# ---------------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------------
def _ollama_spec(url, model="qwen3:8b", **kw):
    kw.setdefault("format", "chat")
    return EngineSpec(name=f"ollama:{model}", backend="ollama", model=model,
                      base_url=url, source="adhoc", **kw)


def _openai_spec(base_url, model="qwen", **kw):
    kw.setdefault("format", "chat")
    return EngineSpec(name=f"openai:{base_url}|{model}", backend="openai", model=model,
                      base_url=base_url, source="adhoc", **kw)


def _gguf_spec(path, **kw):
    kw.setdefault("format", "chat")
    return EngineSpec(name=f"gguf:{Path(path).name}", backend="llamacpp", path=str(path),
                      source="adhoc", **kw)


def _engine_for(spec):
    return HYMTFormatEngine(spec) if spec.format == "hymt" else ChatTemplateEngine(spec)


def _loaded(spec):
    backend = create_backend(spec)
    backend.load()
    return backend, _engine_for(spec)


def _req(text=TEXT, max_tokens=512, instruction=INSTR):
    return GenerationRequest(instruction, text, max_tokens)


def _chat(srv):
    return srv.requests_to("/api/chat")


def _openai_chat(srv):
    return srv.requests_to("/v1/chat/completions")


def _gen_details(srv, path):
    """Üretim isteklerinin kayıtları (ısınma / yükleme isteği hariç)."""
    out = []
    for rec in srv.request_details:
        body = rec.json if isinstance(rec.json, dict) else {}
        if rec.path != path:
            continue
        if body.get("max_tokens") == 1 or body.get("messages") == [] \
                or body.get("prompt") == "":
            continue
        out.append(rec)
    return out


def _has_no_think(body_or_messages):
    msgs = body_or_messages.get("messages") if isinstance(body_or_messages, dict) \
        else body_or_messages
    return any(m.get("role") == "system" and "/no_think" in (m.get("content") or "")
               for m in msgs or [])


def _patch_engine(monkeypatch, spec):
    monkeypatch.setattr(cme, "create_engine", lambda name, *a, **k: _engine_for(spec))


@pytest.fixture
def make_bt(monkeypatch):
    def _make(spec, **cfg):
        _patch_engine(monkeypatch, spec)
        return BookTranslator(TranslationConfig(mt_engine=spec.name, **{**_OFF, **cfg}))
    return _make


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(B, "_sleep", lambda s: None)


@pytest.fixture
def gguf(tmp_path):
    path = tmp_path / "Qwen3-8B-Q4_K_M.gguf"
    write_fake_gguf(path, "qwen3")
    return path


_THINK_TEMPLATE = "{% if enable_thinking %}<think>{% endif %}{{ messages }}"


# ---------------------------------------------------------------------------
# Sahte mlx_lm (load + generate + stream_generate) — yalnız test altyapısı
# ---------------------------------------------------------------------------
class _Resp:
    def __init__(self, text, n, finish):
        self.text = text
        self.generation_tokens = n
        self.finish_reason = finish


class _FakeMlx(types.ModuleType):
    """``mlx_lm`` sahtesi: generate (HY-MT yolu) ve stream_generate (düşünen model)."""

    def __init__(self, reply=None, stream_reply=None):
        super().__init__("mlx_lm")
        self.generate_calls = []
        self.stream_calls = []
        self.streams = []
        self.reply = reply or (lambda prompt: "ÇEVRİ")
        self.stream_reply = stream_reply or (lambda prompt: "ÇEVRİ")

        def generate(model, tokenizer, prompt="", max_tokens=0, **kw):
            self.generate_calls.append({"prompt": prompt, "max_tokens": max_tokens})
            return self.reply(prompt)

        def stream_generate(model, tokenizer, prompt="", max_tokens=0, **kw):
            self.stream_calls.append({"prompt": prompt, "max_tokens": max_tokens})
            pieces = re.findall(r"\s*\S+\s*|\s+", self.stream_reply(prompt))
            state = {"yielded": 0, "total": len(pieces), "closed_early": False}
            self.streams.append(state)

            def gen():
                done = False
                try:
                    n = 0
                    for piece in pieces:
                        if n >= max_tokens:
                            break
                        n += 1
                        state["yielded"] = n
                        yield _Resp(piece, n, None)
                    done = True
                    yield _Resp("", n, "stop" if n < max_tokens else "length")
                finally:
                    if not done:
                        state["closed_early"] = True
            return gen()

        self.generate = generate
        self.stream_generate = stream_generate


class _ThinkTok:
    """Sohbet şablonunda düşünme belirtisi olan sahte tokenizer."""

    chat_template = _THINK_TEMPLATE

    def __init__(self):
        self.messages = []

    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True, **kw):
        self.messages.append(messages)
        return json.dumps(messages, ensure_ascii=False)

    def encode(self, text, add_special_tokens=False):
        return text.split()


class _WordTok:
    """HY-MT yolu için sahte tokenizer: sözcük = token (ölçüm testleri)."""

    def encode(self, text, add_special_tokens=False):
        return text.split()


def _mlx_bt(monkeypatch, spec, tokenizer, fake):
    monkeypatch.setitem(sys.modules, "mlx_lm", fake)
    _patch_engine(monkeypatch, spec)
    bt = BookTranslator.__new__(BookTranslator)
    bt.config = TranslationConfig(mt_engine=spec.name, **_OFF)
    bt.mt_model = object()
    bt.mt_tokenizer = tokenizer
    bt._use_mlx = True
    bt._backend = None
    bt.memory = None
    bt.glossary = None
    bt.ner_protector = None
    bt.naturalizer = None
    bt.ai_client = None
    bt._dict_loader = None
    from core.sentence_splitter import SentenceSplitter
    bt.splitter = SentenceSplitter()
    bt.stats = {"segments_translated": 0, "segments_from_memory": 0, "terms_enforced": 0,
                "ai_reviews": 0, "segments_foreign_skipped": 0, "placeholders_repaired": 0,
                "segments_truncated": 0}
    return bt


def _mlx_chat_spec(name="Qwen3-8B-mlx", **kw):
    return EngineSpec(name=f"mlx:{name}", format="chat", local_dir=name, backend="mlx",
                      source="adhoc", **kw)


# ===========================================================================
# T1 — token kuralı
# ===========================================================================
class TestTokenKurali:
    def test_kural_defaults_sabitleriyle(self):
        chars = 3000
        expected = int(chars / D.MT_CONTEXT_CHARS_PER_TOKEN * D.MT_OUTPUT_TOKENS_FACTOR)
        assert B.output_token_limit(chars) == expected
        assert B.output_token_limit(0) == D.MT_OUTPUT_TOKENS_FLOOR
        assert B.output_token_limit(10 ** 7) == D.MT_OUTPUT_TOKENS_CAP
        assert B.output_token_limit(1, floor=900) == 900
        # Karar 15: 2,2 / 3 = 0,733 token/karakter; ölçülen en kötü 0,614'ün üstü
        assert D.MT_OUTPUT_TOKENS_FACTOR / D.MT_CONTEXT_CHARS_PER_TOKEN > 0.614

    def test_scale_max_tokens_tek_hesaba_baglanir(self, monkeypatch):
        seen = []
        real = B.output_token_limit

        def spy(chars, floor=D.MT_OUTPUT_TOKENS_FLOOR):
            seen.append((chars, floor))
            return real(chars, floor)

        monkeypatch.setattr(B, "output_token_limit", spy)
        bt = BookTranslator.__new__(BookTranslator)
        assert bt._scale_max_tokens(["kısa", "a" * 2000], base=512) == real(2000, 512)
        assert seen == [(2000, 512)]

    def test_elle_yazili_literal_yok(self):
        import core.translator as ct
        src = inspect.getsource(ct)
        assert "_MAX_TOKENS_CAP = 4096" not in src
        assert "* 1.8" not in src and "est_tokens * 1.8" not in src
        assert ct._MAX_TOKENS_CAP == D.MT_OUTPUT_TOKENS_CAP
        assert not hasattr(D, "MT_MAX_OUTPUT_TOKENS")   # ölü sabit kaldırıldı

    def test_ollama_openai_ayni_sinir(self, fake_ollama, fake_openai, make_bt):
        text = "word " * 400                         # 2000 karakter
        limit = B.output_token_limit(len(text.strip()))
        assert limit > D.MT_OUTPUT_TOKENS_FLOOR
        bt = make_bt(_ollama_spec(fake_ollama.url))
        bt._translate_batch_hymt([text.strip()])
        assert _chat(fake_ollama)[-1]["options"]["num_predict"] == limit
        bt2 = make_bt(_openai_spec(fake_openai.base_url))
        bt2._translate_with_mt(text.strip())
        assert _openai_chat(fake_openai)[-1]["max_tokens"] == limit

    def test_llamacpp_ayni_sinir(self, fake_llama, gguf, make_bt):
        text = ("word " * 400).strip()
        bt = make_bt(_gguf_spec(gguf))
        bt._translate_batch_hymt([text])
        name, kw = fake_llama.calls[-1]
        assert name == "create_chat_completion"
        assert kw["max_tokens"] == B.output_token_limit(len(text))
        assert "stream" not in kw                   # düşünmeyen modelde çağrı aynı

    def test_mlx_hymt_ayni_sinir(self, monkeypatch):
        spec = registry.BUILTIN_SPECS["hymt-7b"]
        fake = _FakeMlx(reply=lambda p: "ÇEVRİ")
        bt = _mlx_bt(monkeypatch, spec, object(), fake)
        text = ("word " * 400).strip()
        bt._translate_batch_hymt([text])
        bt._translate_with_mt(text)
        limits = {c["max_tokens"] for c in fake.generate_calls}
        assert limits == {B.output_token_limit(len(text))}
        assert fake.stream_calls == []               # HY-MT akış yoluna girmez


# ===========================================================================
# T2 — sınıra dayanan çıktı
# ===========================================================================
class TestSinirdaCikti:
    def test_length_uyari_ve_sayac(self, fake_llm_server, make_bt, caplog):
        srv = fake_llm_server("ollama", finish_reason="length", reply=lambda t: "Bugün hava")
        bt = make_bt(_ollama_spec(srv.url))
        with caplog.at_level(logging.WARNING, logger="core.translator"):
            assert bt.translate_text(TEXT) == "Bugün hava"
        assert bt.get_stats()["segments_truncated"] == 1
        assert "Çıktı üretim sınırına dayandı" in caplog.text

    def test_sinira_yakin_uretim_sayaci(self, fake_llm_server, make_bt, caplog):
        # eval_count (sahte: sözcük sayısı) >= 0,95 × 512 -> işaretlenir
        near = int(D.MT_OUTPUT_NEAR_LIMIT_RATIO * D.MT_OUTPUT_TOKENS_FLOOR) + 1
        srv = fake_llm_server("ollama", reply=lambda t: " ".join(["sözcük"] * near))
        bt = make_bt(_ollama_spec(srv.url))
        with caplog.at_level(logging.WARNING, logger="core.translator"):
            out = bt._translate_with_mt(TEXT)
        assert out.split() == ["sözcük"] * near        # çıktı kabul edilir
        assert bt.get_stats()["segments_truncated"] == 1
        assert f"{near}/{D.MT_OUTPUT_TOKENS_FLOOR} token" in caplog.text

    def test_sinirin_altinda_sayilmaz(self, fake_llm_server, make_bt):
        below = int(D.MT_OUTPUT_NEAR_LIMIT_RATIO * D.MT_OUTPUT_TOKENS_FLOOR) - 5
        srv = fake_llm_server("ollama", reply=lambda t: " ".join(["sözcük"] * below))
        bt = make_bt(_ollama_spec(srv.url))
        bt.translate_text(TEXT)
        assert bt.get_stats()["segments_truncated"] == 0

    def test_openai_usage_ile_toplu_yol(self, fake_llm_server, make_bt):
        near = int(D.MT_OUTPUT_NEAR_LIMIT_RATIO * D.MT_OUTPUT_TOKENS_FLOOR) + 1
        srv = fake_llm_server("openai", reply=lambda t: (
            " ".join(["sözcük"] * near) if "garden" in t else "Tamam: " + t))
        bt = make_bt(_openai_spec(srv.base_url))
        texts = ["The weather is nice today.", "The garden is green again."]
        out = bt.translate_batch_protected(texts)
        assert out[0] == "Tamam: " + texts[0] and out[1].split()[0] == "sözcük"
        assert bt.get_stats()["segments_truncated"] == 1

    def test_mlx_hymt_olculur_cikti_degismez(self, monkeypatch, caplog):
        spec = registry.BUILTIN_SPECS["hymt-7b"]
        near = int(D.MT_OUTPUT_NEAR_LIMIT_RATIO * D.MT_OUTPUT_TOKENS_FLOOR) + 3
        long_out = " ".join(["kelime"] * near)
        fake = _FakeMlx(reply=lambda p: long_out if "garden" in p else "Kısa çeviri.")
        bt = _mlx_bt(monkeypatch, spec, _WordTok(), fake)
        texts = ["The weather is nice today.", "The garden is green again."]
        with caplog.at_level(logging.WARNING, logger="core.translator"):
            out = bt._translate_batch_hymt(texts)
            single = bt._translate_with_mt(texts[1])
        assert out == ["Kısa çeviri.", long_out]      # HY-MT çıktısı DEĞİŞMEZ
        assert single == long_out
        assert bt.stats["segments_truncated"] == 2
        assert caplog.text.count("Çıktı üretim sınırına dayandı") == 2

    def test_mlx_olculemezse_sessiz_ve_ayni(self, monkeypatch):
        # Tokenizer'da encode yoksa (kilit testindeki sahte gibi) ölçüm yok, çıktı aynı.
        spec = registry.BUILTIN_SPECS["hymt-7b"]
        long_out = " ".join(["kelime"] * 600)
        fake = _FakeMlx(reply=lambda p: long_out)
        bt = _mlx_bt(monkeypatch, spec, object(), fake)
        assert bt._translate_batch_hymt([TEXT]) == [long_out]
        assert bt.stats["segments_truncated"] == 0

    def test_latex_ozeti_ve_checkpoint(self, caplog):
        from latex_pipeline.translator import LatexTranslator, TranslationStats

        lt = LatexTranslator.__new__(LatexTranslator)
        lt.stats = TranslationStats()
        lt._translator = types.SimpleNamespace(stats={"segments_truncated": 1,
                                                      "thinking_tokens": 10})
        lt._snapshot_model_stats()
        lt._translator.stats.update(segments_truncated=3, thinking_segments=2,
                                    thinking_tokens=610, think_budget_cuts=1,
                                    think_retries=1, think_failed=1)
        with caplog.at_level(logging.WARNING, logger="latex_pipeline.translator"):
            lt._collect_model_stats()
        assert lt.stats.segments_truncated == 2          # yalnız bu koşunun payı
        assert lt.stats.thinking_tokens == 600
        assert (lt.stats.thinking_segments, lt.stats.think_budget_cuts,
                lt.stats.think_retries, lt.stats.think_failed) == (2, 1, 1, 1)
        assert "Model ölçümleri: üretim sınırına dayanan çıktı 2" in caplog.text

    def test_latex_ozeti_sifirsa_yazilmaz(self, caplog):
        from latex_pipeline.translator import LatexTranslator, TranslationStats

        lt = LatexTranslator.__new__(LatexTranslator)
        lt.stats = TranslationStats()
        lt._translator = types.SimpleNamespace(stats={"segments_translated": 5})
        lt._snapshot_model_stats()
        with caplog.at_level(logging.WARNING, logger="latex_pipeline.translator"):
            lt._collect_model_stats()
        assert "Model ölçümleri" not in caplog.text


# ===========================================================================
# T3 — düşünme ölçümü
# ===========================================================================
class TestDusunmeOlcumu:
    def test_inline_think_ve_zorla_acik(self):
        assert B.inline_thinking("") == (0, False)
        assert B.inline_thinking("Merhaba") == (0, False)
        text = "<think>\nx\n</think>\n\nMerhaba"
        end, is_open = B.inline_thinking(text)
        assert not is_open and text[end:].strip() == "Merhaba"
        assert B.inline_thinking("<think>yarım") == (len("<think>yarım"), True)
        forced = "düşünce\n</think>\nMerhaba"
        end, is_open = B.inline_thinking(forced)
        assert forced[end:].strip() == "Merhaba" and not is_open

    def test_harmony_kanallari(self):
        text = ("<|channel|>analysis<|message|>düşünüyorum<|end|><|start|>assistant"
                "<|channel|>final<|message|>Merhaba")
        end, is_open = B.inline_thinking(text)
        assert text[end:] == "Merhaba" and not is_open
        partial = "<|channel|>analysis<|message|>düşün"
        assert B.inline_thinking(partial) == (len(partial), True)

    def test_meter_akis_zorla_acik_yeniden_atfeder(self):
        meter = B.ThinkingMeter(100)
        for piece in ["düşünce ", "burada ", "</think>", "\n\nMerhaba ", "dünya"]:
            meter.add_content(piece)
        assert meter.tokens == 3 and meter.answer_tokens == 2
        assert meter.answer_text().strip() == "Merhaba dünya"

    def test_meter_kaynakta_think_varsa_dokunmaz(self):
        meter = B.ThinkingMeter(100, source="Bu kitap <think> etiketini anlatır.")
        meter.add_content("<think> etiketi")
        assert meter.tokens == 0 and meter.answer_tokens == 1

    def test_meter_akissiz_tahmin_ve_sunucu_sayaci(self):
        m1 = B.ThinkingMeter(100)
        m1.add_whole("a" * 30, "Merhaba")
        assert m1.tokens == B.estimate_tokens(30) and m1.estimated
        m2 = B.ThinkingMeter(100)
        m2.add_whole("a" * 30, "Merhaba", thinking_tokens=7)
        assert m2.tokens == 7 and not m2.estimated

    def test_ollama_ayri_alan_olculur_sonra_akisa_gecer(self, fake_llm_server, make_bt):
        # Yeteneksiz model ama yanıtta message.thinking var: ölçülür, sonraki akışla.
        srv = fake_llm_server("ollama", reply=lambda t: FakeReply(
            content="Bugün hava güzel.", thinking=SHORT_THINK))
        bt = make_bt(_ollama_spec(srv.url))
        assert bt.translate_text(TEXT) == "Bugün hava güzel."
        stats = bt.get_stats()
        assert stats["thinking_segments"] == 1
        assert stats["thinking_tokens_estimated"] == B.estimate_tokens(len(SHORT_THINK))
        assert _chat(srv)[-1]["stream"] is False
        assert bt.translate_text("The garden is green again.") == "Bugün hava güzel."
        body = _chat(srv)[-1]
        assert body["stream"] is True               # düşündüğü ölçüldü -> akış
        # G2b-2 (KARARLAR 22): "yok" sınıflı modelde düşünce ölçüldü -> yapışkan
        # "kapatilamaz" -> düşünerek çeviri: istek sınırı = pay + çeviri sınırı
        assert body["options"]["num_predict"] == \
            D.MT_OUTPUT_TOKENS_FLOOR + B.think_allowance(len("The garden is green again."))
        stats = bt.get_stats()
        assert stats["thinking_segments"] == 2
        assert stats["thinking_tokens"] == len(SHORT_THINK.split())   # akış parçası

    def test_ollama_metin_ici_think(self, fake_llm_server, make_bt):
        srv = fake_llm_server("ollama", think_block=True, reply=lambda t: "Bugün hava güzel.")
        bt = make_bt(_ollama_spec(srv.url))
        assert bt.translate_text(TEXT) == "Bugün hava güzel."
        assert bt.get_stats()["thinking_segments"] == 1

    def test_openai_reasoning_content_ve_sayac(self, fake_llm_server):
        srv = fake_llm_server("openai", reasoning_content=SHORT_THINK)
        backend, eng = _loaded(_openai_spec(srv.base_url))
        res = backend.generate(eng, _req())
        assert res.error is None and res.text == "ÇEVRİLDİ: " + TEXT
        assert res.thinking_tokens == B.estimate_tokens(len(SHORT_THINK))
        assert res.thinking_estimated and not res.thinking_exceeded

    def test_lmstudio_reasoning_alani(self, fake_llm_server):
        # LM Studio 0.3.23+: gpt-oss düşüncesi "reasoning" alanında
        srv = fake_llm_server("lmstudio", models=[FakeModel("openai/gpt-oss-20b", loaded=True)],
                              reasoning_field="reasoning", reasoning_content=SHORT_THINK)
        backend, eng = _loaded(_openai_spec(srv.base_url, model="openai/gpt-oss-20b",
                                            flavor="lmstudio"))
        res = backend.generate(eng, _req())
        assert res.thinking_tokens > 0 and res.error is None

    def test_openai_sunucu_dusunme_sayaci_kullanilir(self, fake_llm_server, monkeypatch):
        srv = fake_llm_server("openai")
        backend, eng = _loaded(_openai_spec(srv.base_url))
        data = {"choices": [{"message": {"content": "Merhaba", "reasoning_content": "x" * 90},
                             "finish_reason": "stop"}],
                "usage": {"completion_tokens": 40,
                          "completion_tokens_details": {"reasoning_tokens": 33}}}
        res = backend._parse(data, raw=False, source=TEXT)
        assert res.thinking_tokens == 33 and not res.thinking_estimated
        assert res.output_tokens == 40 - 33          # yanıt payı = toplam - düşünme

    def test_dusunme_sayaci_sinir_kararini_sisirmez(self, fake_llm_server, make_bt):
        # Akışsız yanıtta eval_count düşünmeyi de sayar: yanıt payı bilinmediği için
        # "sınıra dayandı" denmez (karar yalnız done_reason'a kalır).
        near = int(D.MT_OUTPUT_NEAR_LIMIT_RATIO * D.MT_OUTPUT_TOKENS_FLOOR) + 1
        srv = fake_llm_server("ollama", reply=lambda t: FakeReply(
            content=" ".join(["sözcük"] * near), thinking=SHORT_THINK))
        bt = make_bt(_ollama_spec(srv.url))
        bt._translate_with_mt(TEXT)
        assert bt.get_stats()["segments_truncated"] == 0
        assert bt.get_stats()["thinking_segments"] == 1

    def test_llamacpp_harmony_olculur(self, fake_llama, gguf):
        fake_llama.config.reply = lambda t: (
            "<|channel|>analysis<|message|>kısa düşünce<|end|><|start|>assistant"
            "<|channel|>final<|message|>ÇEVRİ")
        backend, eng = _loaded(_gguf_spec(gguf))
        res = backend.generate(eng, _req())
        assert res.error is None and res.thinking_tokens > 0
        assert eng.clean_output(res.text, TEXT) == "ÇEVRİ"

    def test_dusunmeyen_modelde_sayac_sifir(self, fake_ollama, make_bt):
        bt = make_bt(_ollama_spec(fake_ollama.url))
        bt.translate_text(TEXT)
        stats = bt.get_stats()
        assert stats["thinking_segments"] == 0 and stats["thinking_tokens"] == 0
        assert stats["thinking_tokens_estimated"] == 0


# ===========================================================================
# T4 — akış + bütçe
# ===========================================================================
class TestButceAkis:
    def test_dusunmeyen_modelde_govde_degismez(self, fake_llm_server):
        srv = fake_llm_server("ollama", models=["gemma3:4b"])
        backend, eng = _loaded(_ollama_spec(srv.url, model="gemma3:4b"))
        for _ in range(3):
            assert backend.generate(eng, _req(max_tokens=777)).error is None
        for body in _chat(srv):
            assert body["stream"] is False and "think" not in body
            assert body["options"]["num_predict"] == 777
            assert not _has_no_think(body)

    def test_ollama_dusunen_model_akista_hemen_kesilir(self, fake_llm_server):
        srv = fake_llm_server("ollama", models=[FakeModel("dusunur:7b", thinking=True)],
                              stream_chunk_delay_s=0.001,
                              reply=lambda t: FakeReply(content="ÇEVRİ", thinking=LONG_THINK))
        backend, eng = _loaded(_ollama_spec(srv.url, model="dusunur:7b"))
        t0 = time.monotonic()
        res = backend.generate(eng, _req(max_tokens=600))
        elapsed = time.monotonic() - t0
        # G2b-2 (KARARLAR 22): think:false tutmadı (yapışkan "kapatilamaz") ->
        # parça düşünerek çeviri payıyla TEK kez yeniden denendi; o da kesildi
        allowance = B.think_allowance(len(TEXT))
        assert res.error == MSG_THINK_BUDGET.format(tokens=allowance + 1, budget=allowance)
        assert res.thinking_exceeded and res.text == ""
        first, retry = _gen_details(srv, "/api/chat")
        assert first.json["stream"] is True and first.json["think"] is False
        assert first.json["options"]["num_predict"] == 600 + BUDGET          # yoklama
        assert retry.json["options"]["num_predict"] == 600 + allowance       # pay
        # Sunucu 3000 parçanın hepsini yazamadı: istemci bağlantıyı kesti
        time.sleep(0.2)
        for rec in (first, retry):
            assert rec.stream_closed_early and rec.stream_chunks < rec.stream_total
        assert elapsed < 2 * 3000 * 0.001        # iki denemede de akışın tamamını beklemedi

    def test_openai_sse_reasoning_kesilir(self, fake_llm_server):
        srv = fake_llm_server("lmstudio",
                              models=[FakeModel("org/dusunur", thinking=True, loaded=True)],
                              reply=lambda t: FakeReply(content="ÇEVRİ", thinking=LONG_THINK))
        backend, eng = _loaded(_openai_spec(srv.base_url, model="org/dusunur",
                                            flavor="lmstudio"))
        assert backend.thinking_capable()
        res = backend.generate(eng, _req())
        assert res.thinking_exceeded and res.error.startswith("Model düşünme sınırını aştı")
        # G2b-1: sahte sunucuda yerel REST yok -> resmî kapatma kullanılamadı ->
        # "kapatilamaz"; G2b-2: baştan düşünerek çeviri (pay + çeviri sınırı),
        # pay dolup cevap yok -> TEK yeniden deneme (aynı istek)
        recs = _gen_details(srv, "/v1/chat/completions")
        assert len(recs) == 2
        for rec in recs:
            assert rec.json["stream"] is True
            assert rec.json["max_tokens"] == 512 + B.think_allowance(len(TEXT))
        time.sleep(0.2)
        assert all(rec.stream_closed_early for rec in recs)

    def test_openai_sse_dusunme_butce_altinda_yanit(self, fake_llm_server):
        srv = fake_llm_server("lmstudio",
                              models=[FakeModel("org/dusunur", thinking=True, loaded=True)],
                              reply=lambda t: FakeReply(content="Bugün hava güzel.",
                                                        thinking=SHORT_THINK))
        backend, eng = _loaded(_openai_spec(srv.base_url, model="org/dusunur",
                                            flavor="lmstudio"))
        res = backend.generate(eng, _req())
        assert res.error is None and res.text == "Bugün hava güzel."
        assert res.thinking_tokens == len(SHORT_THINK.split())
        assert res.output_tokens == len("Bugün hava güzel.".split())

    def test_llamacpp_akis_uretec_kapatilir(self, fake_llama, tmp_path):
        path = tmp_path / "Model-7B-Q4_K_M.gguf"       # Qwen3 değil: sert yol yok
        write_fake_gguf(path, "llama")
        fake_llama.config.chat_template = _THINK_TEMPLATE
        fake_llama.config.reply = lambda t: "<think>\n" + LONG_THINK + "\n</think>\n\nÇEVRİ"
        backend, eng = _loaded(_gguf_spec(path))
        assert backend.thinking_capable()
        res = backend.generate(eng, _req(max_tokens=100))
        assert res.thinking_exceeded
        name, kw = fake_llama.calls[0]
        # G2b-2: llama-cpp-python şablona enable_thinking geçiremez -> "kapatilamaz"
        # -> düşünerek çeviri: üretim sınırı = çeviri sınırı + pay
        assert kw["stream"] is True and kw["max_tokens"] == 100 + B.think_allowance(len(TEXT))
        state = fake_llama.streams[0]
        assert state["closed_early"] and state["yielded"] < state["total"]

    def test_llamacpp_akis_basarili_yanit(self, fake_llama, gguf):
        fake_llama.config.chat_template = _THINK_TEMPLATE
        fake_llama.config.reply = lambda t: "<think>\nkısa\n</think>\n\nBugün hava güzel."
        backend, eng = _loaded(_gguf_spec(gguf))
        res = backend.generate(eng, _req())
        assert res.error is None and res.thinking_tokens > 0
        assert eng.clean_output(res.text, TEXT) == "Bugün hava güzel."
        assert not fake_llama.streams[-1]["closed_early"]

    def test_mlx_dusunen_sohbet_modeli_akisla(self, monkeypatch):
        spec = _mlx_chat_spec(name="Model-7B-mlx")
        fake = _FakeMlx(stream_reply=lambda p: "<think>\n" + LONG_THINK + "\n</think>\nÇEVRİ")
        bt = _mlx_bt(monkeypatch, spec, _ThinkTok(), fake)
        text = "The weather is nice today."
        assert bt._translate_with_mt(text) == "[MT_FAIL]" + text
        assert fake.generate_calls == []             # eski generate yoluna gitmedi
        assert fake.stream_calls[0]["max_tokens"] == D.MT_OUTPUT_TOKENS_FLOOR + BUDGET
        assert fake.streams[0]["closed_early"]
        # G2b-2: enable_thinking=False tutmadı -> yapışkan "kapatilamaz" -> TEK
        # yeniden deneme düşünerek çeviri payıyla; o da kesildi
        assert fake.stream_calls[1]["max_tokens"] == \
            D.MT_OUTPUT_TOKENS_FLOOR + B.think_allowance(len(text))
        assert bt.stats["think_failed"] == 1 and bt.stats["think_budget_cuts"] == 2

    def test_mlx_dusunen_model_basarili(self, monkeypatch):
        spec = _mlx_chat_spec(name="Model-7B-mlx")
        fake = _FakeMlx(stream_reply=lambda p: "<think>\nkısa\n</think>\nBugün hava güzel.")
        bt = _mlx_bt(monkeypatch, spec, _ThinkTok(), fake)
        out = bt._translate_batch_hymt(["The weather is nice today.", "Another one here."])
        assert out == ["Bugün hava güzel.", "Bugün hava güzel."]
        assert bt.stats["thinking_segments"] == 2 and bt.stats["thinking_tokens"] > 0

    def test_mlx_hymt_akis_yoluna_girmez(self, monkeypatch):
        spec = registry.BUILTIN_SPECS["hymt-7b"]
        fake = _FakeMlx()
        bt = _mlx_bt(monkeypatch, spec, _ThinkTok(), fake)   # şablon düşünme belirtse de
        assert not bt._mlx_stream_active()
        bt._translate_with_mt(TEXT)
        assert fake.stream_calls == [] and len(fake.generate_calls) == 1

    def test_think_budget_secenegi(self, fake_llm_server):
        srv = fake_llm_server("ollama", models=[FakeModel("dusunur:7b", thinking=True)],
                              reply=lambda t: FakeReply(content="ÇEVRİ", thinking=LONG_THINK))
        backend, eng = _loaded(_ollama_spec(srv.url, model="dusunur:7b",
                                            options={"think_budget": 64}))
        assert backend.think_budget == 64
        res = backend.generate(eng, _req())
        assert res.error == MSG_THINK_BUDGET.format(tokens=65, budget=64)
        assert _chat(srv)[-1]["options"]["num_predict"] == 512 + 64

    @pytest.mark.parametrize("bad", [0, -5, "çok", True, 3.5])
    def test_think_budget_gecersiz_varsayilana_duser(self, bad, caplog):
        spec = EngineSpec(name="x", options={"think_budget": bad})
        with caplog.at_level(logging.WARNING, logger="core.mt_engines.backends"):
            assert B.resolve_think_budget(spec) == BUDGET
        assert "think_budget geçersiz" in caplog.text

    def test_mt_engines_json_think_budget(self, tmp_path, caplog):
        path = tmp_path / C.MT_ENGINES_FILENAME
        path.write_text(json.dumps({"q": {"backend": "ollama", "model": "qwen3:8b",
                                          "options": {"think_budget": 256}}}),
                        encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            specs = registry.load_user_specs(path)
        assert "bilinmeyen" not in caplog.text
        assert B.resolve_think_budget(specs["q"]) == 256


# ===========================================================================
# T5 — sert yeniden deneme
# ===========================================================================
class TestSertYenidenDeneme:
    def test_qwen3_no_think_ile_yeniden_ve_yapiskan(self, fake_llm_server, make_bt):
        def reply(text, body):
            if _has_no_think(body):
                return FakeReply(content="Bugün hava güzel.")
            return FakeReply(content="Bugün hava güzel.", thinking=LONG_THINK)

        srv = fake_llm_server("ollama", models=[FakeModel("qwen3:8b", thinking=True)],
                              reply=reply)
        bt = make_bt(_ollama_spec(srv.url))
        assert bt.translate_text(TEXT) == "Bugün hava güzel."
        bodies = _chat(srv)
        assert not _has_no_think(bodies[0]) and _has_no_think(bodies[1])
        stats = bt.get_stats()
        assert stats["think_retries"] == 1 and stats["think_budget_cuts"] == 1
        assert stats["think_failed"] == 0
        # Sert kip yapışkan: sonraki parça doğrudan /no_think ile
        bt.translate_text("The garden is green again.")
        assert len(_chat(srv)) == 3 and _has_no_think(_chat(srv)[2])

    def test_gpt_oss_ollama_low_duzeyi(self, fake_llm_server):
        def reply(text, body):
            if body.get("think") == "low":
                return FakeReply(content="ÇEVRİ", thinking=SHORT_THINK)
            return FakeReply(content="ÇEVRİ", thinking=LONG_THINK)

        model = FakeModel("gpt-oss:20b", thinking=True, think_values=["low", "medium", "high"],
                          think_default="medium")
        srv = fake_llm_server("ollama", models=[model], reply=reply)
        backend, eng = _loaded(_ollama_spec(srv.url, model="gpt-oss:20b"))
        assert backend.hard_off_available()
        res = backend.generate(eng, _req())
        assert res.error is None and res.think_retries == 1
        assert [b["think"] for b in _chat(srv)] == [False, "low"]
        backend.generate(eng, _req())
        assert _chat(srv)[-1]["think"] == "low"

    def test_ollama_false_izinliyse_low_gonderilmez(self, fake_llm_server):
        model = FakeModel("dusunur:7b", thinking=True, think_values=[True, False])
        srv = fake_llm_server("ollama", models=[model],
                              reply=lambda t: FakeReply(content="ÇEVRİ", thinking=LONG_THINK))
        backend, eng = _loaded(_ollama_spec(srv.url, model="dusunur:7b"))
        assert not backend.hard_off_available()
        res = backend.generate(eng, _req())
        assert res.error.startswith("Model düşünme sınırını aştı") and res.think_retries == 0
        # G2b-2: think:false tutmadı -> düşünerek çeviri; belgeli daha kısa yol
        # yok -> TEK yeniden deneme AYNI istekle (yalnız pay), "low" gönderilmez
        assert [b["think"] for b in _chat(srv)] == [False, False]

    def test_llamacpp_sunucusu_reasoning_effort(self, fake_llm_server):
        def reply(text, body):
            if body.get("reasoning_effort") == "none":
                return FakeReply(content="ÇEVRİ")
            return FakeReply(content="ÇEVRİ", thinking=LONG_THINK)

        srv = fake_llm_server("openai", models=["gpt-oss-20b"], reply=reply)
        backend, eng = _loaded(_openai_spec(srv.base_url, model="gpt-oss-20b"))
        # 1. yanıt akışsız: düşünme ölçülür (bütçe üstü) ama yanıt var -> kabul
        first = backend.generate(eng, _req())
        assert first.error is None and first.thinking_exceeded
        # 2. yanıt akışla: kesilir, reasoning_effort "none" ile yeniden denenir
        second = backend.generate(eng, _req())
        assert second.error is None and second.think_retries == 1
        bodies = _openai_chat(srv)[1:]                 # ısınma hariç
        assert [b["stream"] for b in bodies] == [False, True, True]
        assert "reasoning_effort" not in bodies[0] and "reasoning_effort" not in bodies[1]
        assert bodies[2]["reasoning_effort"] == "none"

    def test_reasoning_effort_reddedilirse_dusurulur(self, fake_llm_server):
        srv = fake_llm_server("openai", models=["gpt-oss-20b"],
                              reject_body_keys=("reasoning_effort",),
                              reply=lambda t: FakeReply(content="ÇEVRİ", thinking=LONG_THINK))
        backend, eng = _loaded(_openai_spec(srv.base_url, model="gpt-oss-20b"))
        backend.think_guard.seen = True               # akış kipinde başla
        res = backend.generate(eng, _req())
        assert res.error.startswith("Model düşünme sınırını aştı")
        assert "reasoning_effort" in backend._dropped_keys

    def test_lmstudio_belgeli_yol_yoksa_yeniden_deneme_yok(self, fake_llm_server):
        srv = fake_llm_server("lmstudio",
                              models=[FakeModel("openai/gpt-oss-20b", thinking=True, loaded=True)],
                              reply=lambda t: FakeReply(content="ÇEVRİ", thinking=LONG_THINK))
        backend, eng = _loaded(_openai_spec(srv.base_url, model="openai/gpt-oss-20b",
                                            flavor="lmstudio"))
        assert not backend.hard_off_available()
        res = backend.generate(eng, _req())
        assert res.think_retries == 0 and res.error.startswith("Model düşünme sınırını aştı")
        assert all("reasoning_effort" not in b for b in _openai_chat(srv))
        # G2b-2: kapatma yolu yok -> düşünerek çeviri; daha kısa resmî yol yok ->
        # TEK yeniden deneme aynı istekle
        assert len(_gen_details(srv, "/v1/chat/completions")) == 2

    def test_think_null_kaydinda_sert_kapatma_yok(self, fake_llm_server):
        srv = fake_llm_server("ollama", models=[FakeModel("qwen3:8b", thinking=True)],
                              reply=lambda t: FakeReply(content="ÇEVRİ", thinking=LONG_THINK))
        backend, eng = _loaded(_ollama_spec(srv.url, think=None))
        res = backend.generate(eng, _req())
        assert res.think_retries == 0 and res.thinking_exceeded
        assert not any(_has_no_think(b) for b in _chat(srv))

    def test_llamacpp_qwen3_no_think(self, fake_llama, gguf):
        fake_llama.config.chat_template = _THINK_TEMPLATE

        def reply(text, kwargs):
            if _has_no_think(kwargs["messages"]):
                return "Bugün hava güzel."
            return "<think>\n" + LONG_THINK + "\n</think>\nBugün hava güzel."

        fake_llama.config.reply = reply
        backend, eng = _loaded(_gguf_spec(gguf))
        res = backend.generate(eng, _req())
        # G2b-2 (KARARLAR 22): resmî kapatma yolu Qwen3 "/no_think" ilk denemeden
        # gider (eskiden yalnız sert yeniden denemede); yeniden deneme gerekmez
        assert res.error is None and res.think_retries == 0
        assert _has_no_think(fake_llama.calls[0][1]["messages"])
        assert eng.clean_output(res.text, TEXT) == "Bugün hava güzel."

    def test_yine_asarsa_cevrilmedi_bos_kabul_yok(self, fake_llm_server, make_bt):
        srv = fake_llm_server("ollama", models=[FakeModel("qwen3:8b", thinking=True)],
                              reply=lambda t: FakeReply(content="", thinking=LONG_THINK))
        bt = make_bt(_ollama_spec(srv.url))
        assert bt._translate_with_mt(TEXT) == "[MT_FAIL]" + TEXT
        n = len(_chat(srv))
        assert bt.translate_text(TEXT) == TEXT            # özgün kalır, boş değil
        # G2b-2: pay dolup cevapsız kalan istek bu koşuda modele yeniden gönderilmez
        assert len(_chat(srv)) == n
        stats = bt.get_stats()
        assert stats["think_failed"] == 1 and stats["think_retries"] == 1
        assert stats["think_budget_cuts"] == 2          # parça başına iki deneme
        # Toplu yol (yeni koşu): girdi aynen (echo), hata sayacı artar
        bt2 = make_bt(_ollama_spec(srv.url))
        out = bt2._translate_batch_hymt(["The garden is green again."])
        assert out == ["The garden is green again."]
        assert bt2._mt_error_count == 1

    def test_no_think_yanitta_kalirsa_silinir(self):
        eng = ChatTemplateEngine(EngineSpec(name="c", format="chat"))
        assert eng.clean_output("/no_think Bugün hava güzel.", TEXT) == "Bugün hava güzel."
        src = "Type /no_think to disable thinking."
        assert eng.clean_output("/no_think yazın.", src) == "/no_think yazın."

    def test_with_no_think(self):
        msgs = [{"role": "user", "content": "x"}]
        assert B.with_no_think(msgs)[0] == {"role": "system", "content": "/no_think"}
        assert msgs == [{"role": "user", "content": "x"}]      # girdi değişmez
        sys_msgs = [{"role": "system", "content": "talimat"}, {"role": "user", "content": "x"}]
        assert B.with_no_think(sys_msgs)[0]["content"] == "talimat\n/no_think"

    @pytest.mark.parametrize("name,ok", [
        ("qwen3:8b", True), ("qwen/qwen3-8b", True), ("Qwen3-8B-Q4_K_M.gguf", True),
        ("qwen3-30b-a3b-thinking-2507", False), ("Qwen3.5-9B", False), ("gpt-oss:20b", False),
        ("llama3.1:8b", False)])
    def test_qwen3_ailesi(self, name, ok):
        assert B.supports_no_think(EngineSpec(name="x", model=name)) is ok


# ===========================================================================
# T6 — koşu devre kesicisi
# ===========================================================================
class TestDevreKesici:
    def test_bekci_penceresi(self):
        # G2b-2 (KARARLAR 22): kesici düşünme aşımında değil, düşünerek çeviride
        # "cevapsız" parçada sayar; en az MT_THINK_BREAKER_MIN_SAMPLES (5) örnekte
        # cevapsız oranı MT_THINK_BREAKER_RATIO'ya (0,6) varınca açılır
        guard = B.ThinkingGuard(10)
        for no_answer in (True, False, True, False):
            assert guard.note_segment(no_answer) is None
        reason = guard.note_segment(True)
        assert reason == guard.tripped and "düşünmesini bitirip çeviri üretemiyor" in reason

    def test_pencere_kayar_ilk_parcalardan_sonra_da_acilir(self):
        # RAPOR MT-13: eski pencere yalnız koşunun ilk 5 parçasına bakıyordu
        guard = B.ThinkingGuard(10)
        for _ in range(D.MT_THINK_BREAKER_WINDOW):
            guard.note_segment(False)
        assert any([guard.note_segment(True) for _ in range(D.MT_THINK_BREAKER_WINDOW)])
        assert guard.tripped

    def test_arka_uc_devresi_acilir_aga_gidilmez(self, fake_llm_server):
        srv = fake_llm_server("ollama", models=[FakeModel("dusunur:7b", thinking=True)],
                              reply=lambda t: FakeReply(content="ÇEVRİ", thinking=LONG_THINK))
        backend, eng = _loaded(_ollama_spec(srv.url, model="dusunur:7b"))
        # G2b-2: her parça pay dolup TEK yeniden denemede de cevapsız; aynı istek
        # koşu içinde yinelenmediği için parçalar farklı metin
        for i in range(D.MT_THINK_BREAKER_MIN_SAMPLES - 1):
            res = backend.generate(eng, _req(text=f"The weather is nice today, part {i}."))
            assert res.thinking_exceeded and res.error
        with pytest.raises(ThinkingBudgetExceeded) as exc:
            backend.generate(eng, _req(text="The weather is nice today, last part."))
        assert isinstance(exc.value, BackendUnavailable)
        assert str(exc.value) == backend.health()
        assert "düşünmesini bitirip çeviri üretemiyor" in backend.health()
        n = len(srv.requests)
        with pytest.raises(ThinkingBudgetExceeded):
            backend.generate(eng, _req())
        with pytest.raises(ThinkingBudgetExceeded):
            backend.generate_batch(eng, [_req(), _req()])
        assert len(srv.requests) == n

    def test_cevirmen_saglik_ve_istisna_yutulmaz(self, fake_llm_server, make_bt):
        srv = fake_llm_server("ollama", models=[FakeModel("dusunur:7b", thinking=True)],
                              reply=lambda t: FakeReply(content="ÇEVRİ", thinking=LONG_THINK))
        bt = make_bt(_ollama_spec(srv.url, model="dusunur:7b"))
        texts = [f"Paragraph number {i} is here." for i in range(5)]
        with pytest.raises(ThinkingBudgetExceeded):
            bt._translate_batch_hymt(texts)
        assert "düşünmesini bitirip çeviri üretemiyor" in bt.backend_health()
        with pytest.raises(ThinkingBudgetExceeded):      # G2b-2 (h): yutulmaz
            bt._translate_with_mt(texts[0])

    def test_mlx_dusunen_model_devre_kesici(self, monkeypatch):
        spec = _mlx_chat_spec(name="Model-7B-mlx")
        fake = _FakeMlx(stream_reply=lambda p: "<think>\n" + LONG_THINK)
        bt = _mlx_bt(monkeypatch, spec, _ThinkTok(), fake)
        texts = [f"Paragraph number {i} is here." for i in range(5)]
        with pytest.raises(ThinkingBudgetExceeded):
            bt._translate_batch_hymt(texts)
        assert "düşünmesini bitirip çeviri üretemiyor" in bt.backend_health()
        n = len(fake.stream_calls)
        with pytest.raises(ThinkingBudgetExceeded):      # G2b-2 (h): yutulmaz
            bt._translate_with_mt(texts[0])
        assert len(fake.stream_calls) == n            # açık devrede üretim yok

    def test_hymt_mlx_saglik_none(self):
        bt = BookTranslator.__new__(BookTranslator)
        assert bt.backend_health() is None


# ---------------------------------------------------------------------------
# T6 uçtan uca: translate_book -> çıkış 5 + Türkçe ileti
# ---------------------------------------------------------------------------
_E2E_BODY = "\\chapter{Motion}\n\n" + "\n\n".join(
    f"Paragraph {w} describes the motion of the planet around the sun in detail."
    for w in ("one", "two", "three", "four", "five", "six", "seven")) + "\n"


@pytest.fixture
def restore_root_logger():
    root = logging.getLogger()
    old_handlers = list(root.handlers)
    old_level = root.level
    yield
    for h in list(root.handlers):
        if h not in old_handlers:
            try:
                h.close()
            except Exception:
                pass
    root.handlers[:] = old_handlers
    root.setLevel(old_level)


@pytest.fixture
def pipeline_env(monkeypatch, tmp_path, restore_root_logger):
    import translate_book as tb

    monkeypatch.setattr(tb, "_preflight_check", lambda *a, **k: [])
    memory_db = tmp_path / "bellek.db"
    real_init_memory = BookTranslator._init_memory

    def _tmp_memory(self):
        self.config.memory_db_path = str(memory_db)
        real_init_memory(self)

    monkeypatch.setattr(BookTranslator, "_init_memory", _tmp_memory)
    return tmp_path


class TestDevreKesiciUctanUca:
    def test_translate_book_cikis_5(self, fake_llm_server, monkeypatch, pipeline_env):
        import translate_book as tb
        from core.mt_engines import discovery
        from core.mt_engines.contracts import SourcesConfig

        srv = fake_llm_server("ollama", models=[FakeModel("dusunur:7b", thinking=True)],
                              reply=lambda t: FakeReply(content="ÇEVRİ", thinking=LONG_THINK))
        sources = SourcesConfig(ollama_url=srv.url, lmstudio_url="http://127.0.0.1:9/v1")
        monkeypatch.setattr(discovery, "load_sources", lambda path=None: sources)
        out = pipeline_env / "kitap"
        (out / "source").mkdir(parents=True)
        (out / "full_book.md").write_text("# Kitap\n\n" + _E2E_BODY, encoding="utf-8")
        (out / "source" / "body.tex").write_text(_E2E_BODY, encoding="utf-8")
        code = tb.run_pipeline(str(out), "Düşünen Kitap", skip_translate=False,
                               skip_compile=True, skip_qc=True, book_type="science",
                               pdf_path=None, resume=True, mt_engine="ollama:dusunur:7b")
        assert code == tb.EXIT_MODEL_UNAVAILABLE == 5
        log = (out / "pipeline.log").read_text(encoding="utf-8")
        assert "Bu model düşünmesini bitirip çeviri üretemiyor" in log
        assert not (out / "source" / "translated_body.tex").exists()
        # G2b-2: parçalar kısa (başlık penceresi); her biri deneme + TEK yeniden
        # deneme, pencere en az örnek sayısına varınca devre açıldı, sonrası ağa gitmedi
        assert len(_gen_details(srv, "/api/chat")) == \
            (1 + D.MT_THINK_RETRY_COUNT) * D.MT_THINK_BREAKER_MIN_SAMPLES


# ===========================================================================
# T7 — akış süresi
# ===========================================================================
class TestAkisSuresi:
    def test_sessiz_sunucuda_toplam_sure_siniri(self, fake_llm_server):
        srv = fake_llm_server("ollama", models=[FakeModel("dusunur:7b", thinking=True)],
                              stream_chunk_delay_s=2.0,
                              reply=lambda t: FakeReply(content="ÇEVRİ", thinking=LONG_THINK))
        backend, eng = _loaded(_ollama_spec(srv.url, model="dusunur:7b",
                                            options={"request_timeout_s": 0.5}))
        t0 = time.monotonic()
        res = backend.generate(eng, _req())
        elapsed = time.monotonic() - t0
        assert res.error == "Model 0,5 sn içinde yanıt vermedi."
        assert elapsed < 1.8                          # bir parça arası (2 sn) beklenmedi

    def test_parca_akisi_surerken_de_sinir(self, fake_llm_server):
        # Her parça 0,05 sn: okuma zaman aşımı hiç dolmaz ama toplam süre dolar.
        srv = fake_llm_server("lmstudio",
                              models=[FakeModel("org/dusunur", thinking=True, loaded=True)],
                              stream_chunk_delay_s=0.05,
                              reply=lambda t: FakeReply(content=" ".join(["söz"] * 200)))
        backend, eng = _loaded(_openai_spec(srv.base_url, model="org/dusunur",
                                            flavor="lmstudio",
                                            options={"request_timeout_s": 0.6}))
        t0 = time.monotonic()
        res = backend.generate(eng, _req())
        assert res.error == "Model 0,6 sn içinde yanıt vermedi."
        assert time.monotonic() - t0 < 2.0

    def test_stream_post_adres_yerel_olmali(self):
        from core.mt_engines.http_backends import stream_post
        with pytest.raises(BackendUnavailable, match="yerel bir adres değil"):
            stream_post("http://10.0.0.5:11434/api/chat", {}, 1.0, lambda line: True)


# ---------------------------------------------------------------------------
# Sahte sunucu kaydı (test altyapısı; ANA_OTURUM_NOTLARI "FLAKY" maddesi)
# ---------------------------------------------------------------------------
class TestSahteSunucuKaydi:
    def test_yanit_alininca_kayit_eksiksiz(self, fake_llm_server):
        # Eskiden t_end / status yanıt yazıldıktan SONRA (finally) doluyordu:
        # istemci yanıtı aldığı anda kayıt 0 görülebiliyordu (yük altında).
        from core.mt_engines import http_util as H

        srv = fake_llm_server("openai")
        for i in range(30):
            H.post_json(srv.base_url + "/chat/completions",
                        {"model": "qwen", "stream": False,
                         "messages": [{"role": "user", "content": f"x\n\nmetin {i}"}]}, 5)
            rec = srv.request_details[-1]
            assert rec.status == 200 and rec.t_end >= rec.t_start > 0
        assert srv.max_concurrent == 1                  # sıralı istekler örtüşmedi


# ===========================================================================
# T8 — ileti kataloğu
# ===========================================================================
class TestIletiKatalogu:
    OLD_TEXTS = {
        "MSG_SERVER_ERROR": "{server} isteği başarısız oldu (HTTP {status}): {detail}",
        "MSG_BAD_RESPONSE": "{server} yanıtı beklenen biçimde değil: {detail}",
        "MSG_LOAD_FAILED": "Model yüklenemedi ({server}): {detail}",
        "MSG_GENERATE_FAILED": "{server} üretimi başarısız oldu: {detail}",
    }

    @pytest.mark.parametrize("name", sorted(OLD_TEXTS))
    def test_katalogda_ve_metin_ayni(self, name):
        assert C.MESSAGE_CATALOG[name] == self.OLD_TEXTS[name]
        assert getattr(B, name) is getattr(C, name)     # tek kaynak, yeniden dışa aktarım

    def test_yeni_iletiler_katalogda(self):
        for name in ("MSG_THINK_BUDGET", "MSG_THINK_BREAKER"):
            assert name in C.MESSAGE_CATALOG
        assert "düşünmeyen bir model" in C.MSG_THINK_BREAKER
        assert issubclass(ThinkingBudgetExceeded, BackendUnavailable)
        assert "ThinkingBudgetExceeded" in C.__all__

    def test_backends_kendi_literali_yok(self):
        src = (PROJECT_ROOT / "core" / "mt_engines" / "backends.py").read_text(encoding="utf-8")
        assert 'MSG_SERVER_ERROR = "' not in src and 'MSG_BAD_RESPONSE = "' not in src

    def test_contracts_belgesi_alti_dil(self):
        doc = C.__doc__
        assert "kaynak dili\n  yalnız İngilizce" not in doc
        assert "en, de, ru" in doc and "TEX_SOURCE_LANGS" in doc

    def test_think_budget_secenek_anahtari(self):
        assert "think_budget" in C.ENGINE_OPTION_KEYS

    def test_generation_result_varsayilanlari(self):
        r = GenerationResult()
        assert r.output_tokens is None and r.thinking_tokens == 0
        assert not r.thinking_exceeded and r.think_retries == 0


# ===========================================================================
# T9 — doküman
# ===========================================================================
class TestDokuman:
    def _section(self):
        text = (PROJECT_ROOT / "docs" / "YENI_MODEL_EKLEME.md").read_text(encoding="utf-8")
        return text.split("## 6.", 1)[1].split("## 7.", 1)[0]

    def test_ornek_kayit_uyarisiz_yuklenir(self, tmp_path, caplog):
        blocks = re.findall(r"```json\n(.*?)```", self._section(), re.S)
        assert len(blocks) == 1
        entries = json.loads(blocks[0])
        path = tmp_path / C.MT_ENGINES_FILENAME
        path.write_text(json.dumps(entries), encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            specs = registry.load_user_specs(path)
        assert caplog.text == ""
        (name, spec), = specs.items()
        assert "think_budget" in spec.options
        assert B.resolve_think_budget(spec) == entries[name]["options"]["think_budget"]

    def test_davranis_anlatilir_sabit_adi_yok(self):
        section = self._section()
        for phrase in ("Düşünme sınırı", "Üretim sınırı", "/no_think", "çıkış kodu 5",
                       "think_budget", "sessiz"):
            assert phrase in section, phrase
        assert "MT_THINK_" not in section and "MT_OUTPUT_" not in section
        assert f"{D.MT_THINK_BUDGET_TOKENS} token" in section
        assert str(D.MT_OUTPUT_TOKENS_CAP) in section and str(D.MT_OUTPUT_TOKENS_FLOOR) in section
