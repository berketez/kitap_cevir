"""VTD denetimi düzeltmeleri (28 Eylül 2026) — regresyon testleri.

1. Zorla açık düşünme: şablon düşünmeyi ÜRETİM İSTEMİNİN içinde açtığında
   (istem ``<think>`` ile biter: DeepSeek-R1 damıtmaları, QwQ, Qwen3
   *-Thinking-2507) model yanıta ``<think>`` yazmadan düşünür. Eskiden bu
   düşünme "yanıt" sayılıyordu: bütçe hiç işlemiyor, akış yanıt sınırı +
   bütçe kadar sürüyor ve kapanmayan düşünme ÇEVİRİ diye kabul ediliyordu
   (llama-cpp-python ve MLX yolları). Artık ``ThinkingMeter(forced_open=True)``
   bütçeyi ilk parçadan işletir; kapanmayan düşünme asla yanıt sayılmaz.
2. Donanım tahmininin çıktı token kuralı çevirinin tek hesabına bağlı
   (``core.hardware.output_token_limit`` -> ``backends.output_token_limit``).
3. Donanım varsayım belgesinde kaynağın söylemediği iddialar düzeltildi.
4. Koşu özeti: akışsız ilk yanıtta kesilmeden kabul edilen aşım da sayıldığı
   için etiket "düşünme bütçesini aşan deneme" (eskiden "kesilen").

Gerçek model yüklenmez; sahte llama_cpp ve sahte mlx_lm yalnız test altyapısı.
"""

from __future__ import annotations

import pytest

import tests.test_token_dusunme_20260928 as TT
from config import defaults as D
from core import hardware
from core.mt_engines import backends as B
from core.mt_engines.contracts import MSG_THINK_BUDGET
from tests.fakes import write_fake_gguf

BUDGET = D.MT_THINK_BUDGET_TOKENS
FORCED_THINK = TT.LONG_THINK + "\n</think>\n\nÇEVRİ"        # açılış istemde
SHORT_FORCED = "kısa bir düşünce\n</think>\n\nBugün hava güzel."

# Şablonların üretim istemi kısımları, gerçek model şablonlarının yapısıyla
# (Hugging Face tokenizer_config.json, 28 Eylül 2026):
# Qwen3 karma: enable_thinking false ise boş düşünme bloğu, yoksa hiçbiri.
QWEN3_HYBRID = (
    "{%- for message in messages %}{{- '<|im_start|>' + message.role + '\\n' + "
    "message.content + '<|im_end|>\\n' }}{%- endfor %}"
    "{%- if add_generation_prompt %}{{- '<|im_start|>assistant\\n' }}"
    "{%- if enable_thinking is defined and enable_thinking is false %}"
    "{{- '<think>\\n\\n</think>\\n\\n' }}{%- endif %}{%- endif %}")
# Qwen3 *-Thinking-2507 / QwQ: istem her zaman açık <think> ile biter.
QWEN3_THINKING = (
    "{%- for message in messages %}{{- '<|im_start|>' + message.role + '\\n' + "
    "message.content + '<|im_end|>\\n' }}{%- endfor %}"
    "{%- if add_generation_prompt %}{{- '<|im_start|>assistant\\n<think>\\n' }}{%- endif %}")
# DeepSeek-R1: namespace + raise_exception kullanan şablon; istem <think>\n ile biter.
DEEPSEEK_R1 = (
    "{% set ns = namespace(is_tool=false) %}"
    "{% for message in messages %}{% if message['role'] == 'user' %}"
    "{{'<｜User｜>' + message['content']}}{% elif message['role'] == 'bogus' %}"
    "{{ raise_exception('desteklenmeyen rol') }}{% endif %}{% endfor %}"
    "{% if add_generation_prompt and not ns.is_tool %}{{'<｜Assistant｜><think>\\n'}}{% endif %}")


class _ForcedTok(TT._ThinkTok):
    """Üretim istemi '<think>\\n' ile biten sahte MLX tokenizer'ı."""

    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True, **kw):
        return super().apply_chat_template(messages) + "<|im_start|>assistant\n<think>\n"


# ===========================================================================
# 1a. Belirti tespiti
# ===========================================================================
class TestZorlaAcikTespit:
    @pytest.mark.parametrize("prompt,expected", [
        ("<|im_start|>assistant\n<think>\n", True),
        ("<｜Assistant｜><think>", True),
        ("<|im_start|>assistant\n<think>\n\n</think>\n\n", False),   # kapatılmış blok
        ("<|im_start|>assistant\n", False),
        ("", False),
        (None, False),
    ])
    def test_istem(self, prompt, expected):
        assert B.prompt_opens_thinking(prompt) is expected

    @pytest.mark.parametrize("template,expected", [
        (QWEN3_THINKING, True),
        (DEEPSEEK_R1, True),
        (QWEN3_HYBRID, False),            # düşünmeyi model kendisi açar (<think> yazar)
        (TT._THINK_TEMPLATE, False),      # T testlerinin şablonu: istem açık bitmez
        ("{% generation %}<think>{% endgeneration %}", False),   # işlenemeyen: belirti yok
        ("{{ undefined_fn() }}<think>", False),
        (None, False),
        ([{"name": "default", "template": QWEN3_THINKING}], True),
    ])
    def test_sablon(self, template, expected):
        assert B.template_opens_thinking(template) is expected


# ===========================================================================
# 1b. ThinkingMeter(forced_open=True)
# ===========================================================================
class TestSayac:
    def test_kapanmayan_dusunme_yanit_sayilmaz_butce_isler(self):
        meter = B.ThinkingMeter(5, "The weather is nice.", forced_open=True)
        for _ in range(5):
            meter.add_content("düşünce ")
        assert meter.tokens == 5 and not meter.exceeded and meter.thinking_open
        assert meter.answer_text() == "" and meter.answer_tokens == 0
        meter.add_content("düşünce ")
        assert meter.exceeded                      # bütçe + 1: akış burada kesilir

    def test_kapaninca_yanit(self):
        meter = B.ThinkingMeter(100, forced_open=True)
        for piece in ["kısa ", "düşünce", "\n</think>", "\n\nBugün ", "hava ", "güzel."]:
            meter.add_content(piece)
        assert meter.tokens == 3 and meter.answer_tokens == 3
        assert meter.answer_text().strip() == "Bugün hava güzel."
        assert not meter.thinking_open

    def test_kapanis_ve_yanit_ayni_parcada(self):
        meter = B.ThinkingMeter(100, forced_open=True)
        meter.add_content("düşünce</think>Merhaba")
        assert meter.answer_text() == "Merhaba" and meter.tokens == 1
        assert meter.chars == len("düşünce</think>")

    def test_kaynakta_isaret_varsa_zorlama_yok(self):
        meter = B.ThinkingMeter(3, "Bu kitap <think> etiketini anlatır.", forced_open=True)
        for _ in range(10):
            meter.add_content("söz ")
        assert meter.tokens == 0 and not meter.exceeded

    def test_akissiz_kapanmayan_dusunme(self):
        meter = B.ThinkingMeter(100, forced_open=True)
        meter.add_whole(None, "yalnız düşünce, kapanış yok")
        assert meter.answer_text() == "" and meter.thinking_open
        meter2 = B.ThinkingMeter(100, forced_open=True)
        meter2.add_whole(None, "düşünce\n</think>\nMerhaba")
        assert meter2.answer_text().strip() == "Merhaba"

    def test_varsayilan_davranis_degismedi(self):
        meter = B.ThinkingMeter(3)
        for _ in range(10):
            meter.add_content("söz ")
        assert meter.tokens == 0 and meter.answer_tokens == 10


# ===========================================================================
# 1c. Arka uçlar: llama-cpp-python ve MLX
# ===========================================================================
def _forced_gguf(fake_llama, tmp_path, template=QWEN3_THINKING, reply=FORCED_THINK):
    path = tmp_path / "Model-Thinking-7B-Q4_K_M.gguf"      # Qwen3 karma değil: sert yol yok
    write_fake_gguf(path, "qwen2")
    fake_llama.config.chat_template = template
    fake_llama.config.reply = lambda t: reply
    return TT._loaded(TT._gguf_spec(path))


class TestLlamaCpp:
    def test_zorla_acik_butcede_kesilir_cevrilmedi(self, fake_llama, tmp_path):
        backend, eng = _forced_gguf(fake_llama, tmp_path)
        res = backend.generate(eng, TT._req())
        state = fake_llama.streams[-1]
        # G2b-2 (KARARLAR 22): istemde açılan düşünme kapatılamaz -> düşünerek
        # çeviri payı; pay dolup cevap yok -> TEK yeniden deneme, o da kesildi
        allowance = B.think_allowance(len(TT.TEXT))
        assert res.error == MSG_THINK_BUDGET.format(tokens=allowance + 1, budget=allowance)
        assert res.text == "" and res.thinking_exceeded and len(fake_llama.streams) == 2
        assert state["closed_early"] and state["yielded"] == allowance + 1 < state["total"]

    def test_kisa_zorla_acik_dusunme_yanit_verir(self, fake_llama, tmp_path):
        backend, eng = _forced_gguf(fake_llama, tmp_path, reply=SHORT_FORCED)
        res = backend.generate(eng, TT._req())
        assert res.error is None and res.thinking_tokens > 0
        assert eng.clean_output(res.text, TT.TEXT) == "Bugün hava güzel."

    def test_karma_sablonda_davranis_ayni(self, fake_llama, tmp_path):
        backend, eng = _forced_gguf(fake_llama, tmp_path, template=QWEN3_HYBRID,
                                    reply="Bugün hava güzel.")
        assert not backend._template_forced_open
        res = backend.generate(eng, TT._req())
        assert res.error is None and res.thinking_tokens == 0


class TestMlx:
    def test_zorla_acik_butcede_kesilir_cevrilmedi(self, monkeypatch):
        spec = TT._mlx_chat_spec(name="Model-4B-Thinking-mlx")
        fake = TT._FakeMlx(stream_reply=lambda p: FORCED_THINK)
        bt = TT._mlx_bt(monkeypatch, spec, _ForcedTok(), fake)
        assert bt._translate_with_mt(TT.TEXT) == "[MT_FAIL]" + TT.TEXT
        state = fake.streams[0]
        assert state["closed_early"] and state["yielded"] == BUDGET + 1
        assert bt.stats["think_failed"] == 1 and bt.stats["segments_truncated"] == 0

    def test_kisa_zorla_acik_dusunme_yanit_verir(self, monkeypatch):
        spec = TT._mlx_chat_spec(name="Model-4B-Thinking-mlx")
        fake = TT._FakeMlx(stream_reply=lambda p: SHORT_FORCED)
        bt = TT._mlx_bt(monkeypatch, spec, _ForcedTok(), fake)
        assert bt._translate_with_mt(TT.TEXT) == "Bugün hava güzel."
        assert bt.stats["thinking_tokens"] > 0


# ===========================================================================
# 2. Donanım tahmini: tek çıktı kuralı
# ===========================================================================
class TestTekKural:
    def test_donanim_ceviri_kuralina_bagli(self, monkeypatch):
        calls = []
        real = B.output_token_limit

        def spy(chars, floor=D.MT_OUTPUT_TOKENS_FLOOR):
            calls.append(chars)
            return real(chars, floor)

        monkeypatch.setattr(B, "output_token_limit", spy)
        assert hardware.output_token_limit(D.MAX_SEGMENT_LENGTH) == real(D.MAX_SEGMENT_LENGTH)
        assert calls == [D.MAX_SEGMENT_LENGTH]

    def test_kopya_hesap_yok(self):
        from pathlib import Path

        src = (Path(__file__).resolve().parent.parent / "core" / "hardware.py").read_text(
            encoding="utf-8")
        assert "MT_OUTPUT_TOKENS_FACTOR)" not in src and "* MT_OUTPUT_TOKENS_FACTOR" not in src


# ===========================================================================
# 3. Belge: kaynağın söylemediği iddia yok
# ===========================================================================
class TestBelge:
    def test_kaynak_iddialari(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        doc = (root / "docs" / "DONANIM_VARSAYIMLARI.md").read_text(encoding="utf-8")
        cfg = (root / "config" / "defaults.py").read_text(encoding="utf-8")
        # EleutherAI yazısı %20'lik payın KV içerdiğini söylemiyor
        assert "KV dahil" not in doc and "KV dahil" not in cfg
        # Ollama #1231 günlüğü bellek miktarını yazmıyor: 8 GB çıkarımdır
        assert "(8 GB M1" not in doc and "8 GB M1'de" not in doc
        assert "bellek miktarı günlükte yazmıyor" in doc


# ===========================================================================
# 4. Koşu özeti: "bütçeyi aşan deneme" (akışsız ilk yanıtta kesme yoktur)
# ===========================================================================
class TestOzetEtiketi:
    def test_akissiz_asim_kesilmis_denmez(self, fake_llm_server, caplog):
        """Akışsız ilk yanıt bütçeyi aştı ama yanıt verdi: sayaç 1, etiket 'aşan'."""
        import logging
        import types

        from latex_pipeline.translator import LatexTranslator, TranslationStats
        from tests.fakes import FakeReply

        srv = fake_llm_server("openai", models=["gpt-oss-20b"],
                              reply=lambda t: FakeReply(content="ÇEVRİ", thinking=TT.LONG_THINK))
        backend, eng = TT._loaded(TT._openai_spec(srv.base_url, model="gpt-oss-20b"))
        res = backend.generate(eng, TT._req())
        assert res.error is None and res.thinking_exceeded      # kesilmedi, yanıt kabul
        assert backend.think_guard.stats["think_budget_cuts"] == 1
        lt = LatexTranslator.__new__(LatexTranslator)
        lt.stats = TranslationStats()
        lt._translator = types.SimpleNamespace(stats={})
        lt._snapshot_model_stats()
        lt._translator.stats.update(backend.think_guard.stats)
        with caplog.at_level(logging.WARNING, logger="latex_pipeline.translator"):
            lt._collect_model_stats()
        assert "düşünme bütçesini aşan deneme 1" in caplog.text
        assert "kesilen" not in caplog.text
