"""G2b-1 (2026-10-05): düşünme sınıfı, resmî kapatma yolları, kapatma doğrulaması,
LM Studio yüklü bağlamı, arayüz metinleri.

Berke'nin 5 Ekim kararı: "Düşünme kapatılabiliyorsa kapatılır; kapatılamıyorsa
model DÜŞÜNEREK çevirecek." Bu dosya zemini sınar (kip G2b-2'nin işi).

Gerçek Ollama / LM Studio'ya gidilmez, model yüklenmez: sahte sunucu
(tests/fakes/fake_llm_server.py). LM Studio'nun yerel REST sohbet ucu
(POST /api/v1/chat, https://lmstudio.ai/docs/developer/rest/chat) sahte
sunucuda yok; bu dosyadaki ``lms_native`` fikstürü sahte işleyiciyi YALNIZ bu
testler için sarmalar (fakes dosyası değişmez). Girdiler sentetiktir.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from config import defaults as D
from core.mt_engines import backends as B
from core.mt_engines import contracts as C
from core.mt_engines import discovery, http_util
from core.mt_engines import thinking as T
from core.mt_engines.backends import create_backend
from core.mt_engines.base import EngineSpec
from core.mt_engines.chat import ChatTemplateEngine
from core.mt_engines.contracts import GenerationRequest, GenerationResult
from tests.fakes import FakeModel, FakeReply, write_fake_gguf
from tests.fakes import fake_llm_server as FLS
from tests.fakes.model_files import write_fake_mlx_dir
from ui import branding

INSTR = "Translate from English to Turkish. Output only the translation."
TEXT = "The weather is nice today."
ANSWER = "Bugün hava güzel."
THOUGHT_TEXT = "Kullanıcı hava durumunu soruyor, kısa çevireyim."
LONG_THINK = " ".join(["düşünce"] * 3000)          # bütçenin (512) çok üstü
# RAPOR MT-14: LM Studio REST'in qwen/qwen3.8-27b için gerçekten bildirdiği seçenekler
QWEN38_OPTIONS = ["off", "low", "medium", "xhigh", "on"]
# RAPOR MT-15: gemma-4-26b'nin LM Studio'da yüklendiği gerçek bağlam
GEMMA_LOADED_CTX = 116736
THINK_LOG = "core.mt_engines.thinking"


# ---------------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------------
def _lms_spec(base_url, model, **kw):
    kw.setdefault("format", "chat")
    return EngineSpec(name=f"lmstudio:{model}", backend="openai", model=model,
                      base_url=base_url, flavor="lmstudio", source="adhoc", **kw)


def _ollama_spec(url, model, **kw):
    kw.setdefault("format", "chat")
    return EngineSpec(name=f"ollama:{model}", backend="ollama", model=model,
                      base_url=url, source="adhoc", **kw)


def _loaded(spec):
    backend = create_backend(spec)
    backend.load()
    return backend, ChatTemplateEngine(spec)


def _req(text=TEXT, max_tokens=512):
    return GenerationRequest(INSTR, text, max_tokens)


def _native_gen(srv):
    """POST /api/v1/chat üretim istekleri (ısınma — max_output_tokens 1 — hariç)."""
    return [r.json for r in srv.request_details
            if r.path == "/api/v1/chat" and isinstance(r.json, dict)
            and r.json.get("max_output_tokens") != 1]


def _thinking_lines(caplog, needle):
    return [r for r in caplog.records if r.name == THINK_LOG and needle in r.getMessage()]


@pytest.fixture
def lms_native(monkeypatch, fake_llm_server):
    """Sahte LM Studio + yerel REST ``POST /api/v1/chat`` (akışlı SSE ve düz JSON).

    Belgedeki biçim (okunma 2026-10-05): istek ``model``, ``input``,
    ``system_prompt``, ``reasoning``, ``max_output_tokens``, ``stream``,
    ``store``; yanıt ``output[]`` (``type`` "reasoning" / "message") + ``stats``;
    akış olayları ``event: <tür>`` + ``data: {"type": <tür>, ...}`` —
    https://lmstudio.ai/docs/developer/rest/streaming-events. Sahte sunucu
    ``reasoning: "off"``'u UYGULAMAZ: yanıttaki düşünce ``FakeReply.thinking``'den
    gelir (kapatma tutmadı senaryosu); ``reject_reasoning`` True ise ayar 400 alır.
    """
    original = FLS._make_handler

    def make_handler(server):
        base = original(server)

        class Handler(base):
            def _route(self, method, path, body):
                if (method, path) == ("POST", "/api/v1/chat") and getattr(
                        server, "native_chat", False):
                    return self._native_chat(body if isinstance(body, dict) else {})
                return super()._route(method, path, body)

            def _native_chat(self, body):
                model = server.model(str(body.get("model") or ""))
                if model is None:
                    return self._send(404, {"error": "model yok"})
                if getattr(server, "reject_reasoning", False) and "reasoning" in body:
                    return self._send(400, {"error": "reasoning setting not supported"})
                rep = self._produce(str(body.get("input") or ""), body)
                if rep is None:
                    return None
                content = self._content_of(rep, "", False) or ""
                thinking = rep.thinking
                pieces = FLS.stream_pieces(content)
                think_pieces = FLS.stream_pieces(thinking) if thinking else []
                stats = {"input_tokens": 7, "total_output_tokens": len(pieces) + len(think_pieces),
                         "reasoning_output_tokens": len(think_pieces),
                         "tokens_per_second": 40.0, "time_to_first_token_seconds": 0.1}
                if body.get("stream") and not getattr(server, "ignore_stream", False):
                    def event(kind, **data):
                        obj = {"type": kind, **data}
                        return (f"event: {kind}\n"
                                f"data: {json.dumps(obj, ensure_ascii=False)}\n\n").encode("utf-8")
                    lines = [event("chat.start", model_instance_id=model.name)]
                    lines += [event("reasoning.delta", content=p) for p in think_pieces]
                    lines.append(event("message.start"))
                    lines += [event("message.delta", content=p) for p in pieces]
                    lines.append(event("message.end"))
                    lines.append(event("chat.end", result={
                        "model_instance_id": model.name, "output": [], "stats": stats}))
                    return self._send_stream(lines, "text/event-stream")
                output = ([{"type": "reasoning", "content": thinking}] if thinking else [])
                output.append({"type": "message", "content": content})
                return self._send(200, {"model_instance_id": model.name, "output": output,
                                        "stats": stats})

        return Handler

    monkeypatch.setattr(FLS, "_make_handler", make_handler)

    def _make(*args, reject_reasoning=False, ignore_stream=False, **kwargs):
        srv = fake_llm_server("lmstudio", *args, **kwargs)
        srv.native_chat = True
        srv.reject_reasoning = reject_reasoning
        srv.ignore_stream = ignore_stream        # stream:true'yu yok sayıp düz JSON (F4-04)
        return srv
    return _make


# ===========================================================================
# 1. Sınıflandırma (kalem 1)
# ===========================================================================
class TestSiniflandirma:
    @pytest.mark.parametrize("caps,cls,off", [
        ({"reasoning": {"allowed_options": ["off", "on"], "default": "on"}},
         C.THINKING_CAN_DISABLE, C.THINKING_OFF_LMSTUDIO_REST),
        ({"reasoning": {"allowed_options": QWEN38_OPTIONS, "default": "on"}},
         C.THINKING_CAN_DISABLE, C.THINKING_OFF_LMSTUDIO_REST),
        ({"reasoning": {"allowed_options": ["low", "medium", "high"], "default": "medium"}},
         C.THINKING_ALWAYS, None),
        ({"vision": True, "trained_for_tool_use": True}, C.THINKING_NONE, None),
        ({"reasoning": True}, C.THINKING_UNKNOWN, None),
        (["tool_use"], C.THINKING_UNKNOWN, None),          # REST v0 dizisi
        (None, C.THINKING_UNKNOWN, None),
    ])
    def test_lmstudio(self, caps, cls, off):
        profile = T.lmstudio_profile(caps)
        assert (profile.cls, profile.off_path) == (cls, off)

    @pytest.mark.parametrize("show,cls,off", [
        ({"capabilities": ["completion", "thinking"],
          "thinking": {"values": [True, False], "default": True}},
         C.THINKING_CAN_DISABLE, C.THINKING_OFF_OLLAMA_THINK),
        ({"capabilities": ["completion", "thinking"],
          "thinking": {"values": ["low", "medium", "high"], "default": "medium"}},
         C.THINKING_ALWAYS, None),
        ({"capabilities": ["completion"], "thinking": {"values": [False], "default": False}},
         C.THINKING_NONE, None),
        # 0 == False tuzağı: sayı 0 "false" sayılmaz
        ({"capabilities": ["completion", "thinking"], "thinking": {"values": [0, 1]}},
         C.THINKING_ALWAYS, None),
        ({"capabilities": ["completion", "thinking"]}, C.THINKING_UNKNOWN, None),
        ({"capabilities": ["completion"], "template": "{{ .Prompt }}"}, C.THINKING_NONE, None),
        ({"capabilities": ["completion"], "template": "<think>{{ .Prompt }}"},
         C.THINKING_ALWAYS, None),
        ({}, C.THINKING_UNKNOWN, None),
        (None, C.THINKING_UNKNOWN, None),
    ])
    def test_ollama(self, show, cls, off):
        profile = T.ollama_profile(show)
        assert (profile.cls, profile.off_path) == (cls, off)

    @pytest.mark.parametrize("template,backend,soft,cls,off", [
        ("{% if enable_thinking %}<think>{% endif %}{{ messages }}", C.BACKEND_MLX, False,
         C.THINKING_CAN_DISABLE, C.THINKING_OFF_TEMPLATE_SWITCH),
        # llama-cpp-python şablona enable_thinking geçiremez
        ("{% if enable_thinking %}<think>{% endif %}{{ messages }}", C.BACKEND_LLAMACPP,
         False, C.THINKING_ALWAYS, None),
        ("{% if enable_thinking %}<think>{% endif %}{{ messages }}", C.BACKEND_LLAMACPP,
         True, C.THINKING_CAN_DISABLE, C.THINKING_OFF_QWEN3_SOFT),
        ("<|start|>assistant<|channel|>analysis", C.BACKEND_LLAMACPP, False,
         C.THINKING_ALWAYS, None),
        ("{{ bos_token }}{% for m in messages %}{{ m.content }}{% endfor %}", C.BACKEND_MLX,
         False, C.THINKING_NONE, None),
        ([{"name": "default", "template": "<think>{{ x }}"}], C.BACKEND_MLX, False,
         C.THINKING_ALWAYS, None),
        (None, C.BACKEND_MLX, False, C.THINKING_UNKNOWN, None),
        ("   ", C.BACKEND_LLAMACPP, False, C.THINKING_UNKNOWN, None),
    ])
    def test_sablon(self, template, backend, soft, cls, off):
        profile = T.template_profile(template, backend=backend, soft_switch=soft)
        assert (profile.cls, profile.off_path) == (cls, off)

    def test_qwen3_yumusak_anahtar_belgeye_uygun(self):
        # Qwen3 karma: "/no_think" belgeli; Qwen3.5 kartı: "does not officially support
        # the soft switch" (https://huggingface.co/Qwen/Qwen3.5-35B-A3B)
        assert T.qwen3_soft_switch(["Qwen3-8B-Q4_K_M.gguf"])
        assert not T.qwen3_soft_switch(["Qwen3.5-35B-A3B-Q4_K_M.gguf"])
        assert not T.qwen3_soft_switch(["qwen/qwen3.8-27b"])
        assert not T.qwen3_soft_switch(["", None])

    def test_profile_of_secici_okuma_noktasi(self):
        base = dict(engine_id="x", label="x", source=C.SOURCE_MLX, backend=C.BACKEND_MLX,
                    usable=False)
        hymt = C.DiscoveredModel(format_guess=C.FORMAT_HYMT, **base)
        chat = C.DiscoveredModel(format_guess=C.FORMAT_CHAT, **base)
        known = C.DiscoveredModel(format_guess=C.FORMAT_CHAT, thinking=C.THINKING_CAN_DISABLE,
                                  thinking_off=C.THINKING_OFF_OLLAMA_THINK, **base)
        assert T.profile_of(hymt).cls == C.THINKING_NONE
        assert T.profile_of(chat).cls == C.THINKING_UNKNOWN
        assert (T.profile_of(known).cls, T.profile_of(known).off_path) == \
            (C.THINKING_CAN_DISABLE, C.THINKING_OFF_OLLAMA_THINK)

    def test_sozlesme_sabitleri(self):
        assert C.THINKING_CLASSES == ("yok", "kapatilabilir", "kapatilamaz", "bilinmiyor")
        assert len(set(C.THINKING_OFF_PATHS)) == 4
        import core.mt_engines as pkg
        for name in ("THINKING_CLASSES", "THINKING_OFF_PATHS", "THINKING_CAN_DISABLE"):
            assert getattr(pkg, name) is getattr(C, name)


# ===========================================================================
# 1b. Keşif alanları (sahte LM Studio / Ollama, models/ klasörü)
# ===========================================================================
class TestKesifAlanlari:
    def test_lmstudio_kesfi_sinif_ve_yol(self, fake_llm_server):
        srv = fake_llm_server("lmstudio", models=[
            FakeModel("qwen/qwen3.8-27b", thinking=True, loaded=True),
            FakeModel("org/duz-model", thinking=False, loaded=True)])
        probe = discovery._probe_lmstudio(srv.base_url, 2.0)
        thinker, plain = probe.index["qwen/qwen3.8-27b"], probe.index["org/duz-model"]
        assert (thinker.thinking, thinker.thinking_off) == \
            (C.THINKING_CAN_DISABLE, C.THINKING_OFF_LMSTUDIO_REST)
        assert (plain.thinking, plain.thinking_off) == (C.THINKING_NONE, None)
        assert thinker.reasoning is True and plain.reasoning is False   # eski alan aynen

    def test_ollama_kesfi_think_values(self, fake_llm_server):
        srv = fake_llm_server("ollama", models=[
            FakeModel("qwen3:8b", thinking=True, think_values=[True, False],
                      think_default=True),
            FakeModel("gpt-oss:20b", thinking=True, think_values=["low", "medium", "high"],
                      think_default="medium"),
            FakeModel("eski-dusunur:7b", thinking=True),
            FakeModel("llama3.1:8b", thinking=False)])
        probe = discovery._probe_ollama(srv.url, 2.0)
        got = {name: (m.thinking, m.thinking_off) for name, m in probe.index.items()}
        assert got == {
            "qwen3:8b": (C.THINKING_CAN_DISABLE, C.THINKING_OFF_OLLAMA_THINK),
            "gpt-oss:20b": (C.THINKING_ALWAYS, None),
            "eski-dusunur:7b": (C.THINKING_UNKNOWN, None),
            "llama3.1:8b": (C.THINKING_NONE, None),
        }

    def test_genel_openai_sunucusu_bilinmiyor(self, fake_llm_server):
        srv = fake_llm_server("openai")
        probe = discovery._probe_openai(srv.base_url, 2.0, None)
        assert {m.thinking for m in probe.models} == {C.THINKING_UNKNOWN}

    def test_models_klasoru_mlx_ve_gguf(self, mt_isolation):
        root = mt_isolation.models_dir
        write_fake_mlx_dir(root / "Qwen3.5-9B-MLX", "qwen3",
                           chat_template="{% if enable_thinking %}<think>{% endif %}x")
        write_fake_mlx_dir(root / "Duz-MLX", "llama", chat_template="{{ messages }}")
        write_fake_gguf(root / "Qwen3-8B-Q4_K_M.gguf", "qwen3",
                        chat_template="{% if enable_thinking %}<think>{% endif %}x")
        write_fake_gguf(root / "Gemma-Q4_K_M.gguf", "gemma3",
                        chat_template="{% if enable_thinking %}<think>{% endif %}x")
        report = discovery.discover_all()
        by_label = {m.label: m for m in report.models}
        assert (by_label["Qwen3.5-9B-MLX"].thinking, by_label["Qwen3.5-9B-MLX"].thinking_off) \
            == (C.THINKING_CAN_DISABLE, C.THINKING_OFF_TEMPLATE_SWITCH)
        assert by_label["Duz-MLX"].thinking == C.THINKING_NONE
        assert (by_label["Qwen3-8B-Q4_K_M"].thinking, by_label["Qwen3-8B-Q4_K_M"].thinking_off) \
            == (C.THINKING_CAN_DISABLE, C.THINKING_OFF_QWEN3_SOFT)
        assert by_label["Gemma-Q4_K_M"].thinking == C.THINKING_ALWAYS

    def test_json_bos_alanlar_yazilmaz(self, fake_llm_server):
        srv = fake_llm_server("lmstudio", models=[FakeModel("org/m", thinking=True,
                                                            loaded=True)])
        probe = discovery._probe_lmstudio(srv.base_url, 2.0)
        unknown = C.DiscoveredModel(engine_id="x", label="x", source=C.SOURCE_MLX,
                                    backend=C.BACKEND_MLX, format_guess=C.FORMAT_CHAT,
                                    usable=False)
        data = discovery.report_to_json(C.DiscoveryReport(
            models=(probe.models[0], unknown), sources=(), scanned_at=1.0))
        known, missing = data["models"]
        assert known["thinking"] == C.THINKING_CAN_DISABLE
        assert known["thinking_off"] == C.THINKING_OFF_LMSTUDIO_REST
        assert known["loaded_context"] == 4096
        assert not {"thinking", "thinking_off", "loaded_context"} & set(missing)


# ===========================================================================
# 2. Resmî kapatma yolları (kalem 2, RAPOR MT-14)
# ===========================================================================
class TestLmStudioKapatma:
    def test_off_izinli_modelde_yerel_rest_govdesi(self, lms_native):
        srv = lms_native(models=[FakeModel("qwen/qwen3.8-27b", thinking=True, loaded=True)],
                         reply=lambda t: FakeReply(content=ANSWER))
        backend, eng = _loaded(_lms_spec(srv.base_url, "qwen/qwen3.8-27b"))
        warm = [r.json for r in srv.request_details if r.path == "/api/v1/chat"]
        assert warm and warm[0]["reasoning"] == "off" and warm[0]["max_output_tokens"] == 1
        assert warm[0]["store"] is False
        res = backend.generate(eng, _req())
        assert res.error is None and res.text == ANSWER and res.thinking_tokens == 0
        body = _native_gen(srv)[-1]
        assert body["reasoning"] == T.LMSTUDIO_REASONING_OFF == "off"
        assert body["store"] is False and body["stream"] is True
        assert body["model"] == "qwen/qwen3.8-27b" and TEXT in body["input"]
        assert "seed" not in body and "chat_template_kwargs" not in body
        # OpenAI uyumlu uca üretim isteği gitmedi
        assert not [r for r in srv.request_details if r.path == "/v1/chat/completions"]
        assert backend.thinking_off_requested(False) is True
        assert backend.thinking_class() == C.THINKING_CAN_DISABLE

    def test_system_prompt_ayri_alanda(self, lms_native):
        srv = lms_native(models=[FakeModel("org/dusunur", thinking=True, loaded=True)],
                         reply=lambda t: FakeReply(content=ANSWER))
        backend, eng = _loaded(_lms_spec(srv.base_url, "org/dusunur", system_prompt=True))
        assert backend.generate(eng, _req()).text == ANSWER
        body = _native_gen(srv)[-1]
        assert body["system_prompt"].startswith("Translate from English")
        assert body["input"] == TEXT

    def test_think_null_kaydinda_yerel_yol_yok(self, lms_native):
        srv = lms_native(models=[FakeModel("org/dusunur", thinking=True, loaded=True)],
                         reply=lambda t: FakeReply(content=ANSWER))
        backend, eng = _loaded(_lms_spec(srv.base_url, "org/dusunur", think=None))
        backend.generate(eng, _req())
        assert not _native_gen(srv)
        assert [r for r in srv.request_details if r.path == "/v1/chat/completions"]

    def test_off_izinli_degilse_yerel_yol_yok(self, lms_native, monkeypatch):
        srv = lms_native(models=[FakeModel("openai/gpt-oss-20b", thinking=True, loaded=True)],
                         reply=lambda t: FakeReply(content=ANSWER))
        # REST v1 yanıtında allowed_options'ı gpt-oss gibi yap ("off" yok)
        real_get = http_util.get_json

        def get_json(url, timeout):
            data = real_get(url, timeout)
            if url.endswith("/api/v1/models"):
                for item in data.get("models", []):
                    item["capabilities"]["reasoning"] = {
                        "allowed_options": ["low", "medium", "high"], "default": "medium"}
            return data
        monkeypatch.setattr(http_util, "get_json", get_json)
        backend, eng = _loaded(_lms_spec(srv.base_url, "openai/gpt-oss-20b"))
        backend.generate(eng, _req())
        assert backend.thinking_profile().cls == C.THINKING_ALWAYS
        assert not [r for r in srv.request_details if r.path == "/api/v1/chat"]

    def test_yerel_uc_yoksa_openai_ucuna_doner_ve_yapiskan(self, fake_llm_server, caplog):
        # Sahte sunucunun kendisi /api/v1/chat bilmez (eski LM Studio gibi 404).
        srv = fake_llm_server("lmstudio", models=[FakeModel("org/dusunur", thinking=True,
                                                            loaded=True)],
                              reply=lambda t: FakeReply(content=ANSWER))
        caplog.set_level(logging.WARNING, logger=THINK_LOG)
        backend, eng = _loaded(_lms_spec(srv.base_url, "org/dusunur"))
        res = backend.generate(eng, _req())
        assert res.text == ANSWER
        assert [r for r in srv.request_details if r.path == "/v1/chat/completions"]
        assert backend.thinking_monitor.off_unavailable.startswith("HTTP 404")
        assert backend.thinking_class() == C.THINKING_ALWAYS
        assert len(_thinking_lines(caplog, "kullanılamadı")) == 1
        assert backend.thinking_off_requested(False) is False

    def test_ayar_reddedilirse_doner(self, lms_native):
        srv = lms_native(models=[FakeModel("org/dusunur", thinking=True, loaded=True)],
                         reply=lambda t: FakeReply(content=ANSWER), reject_reasoning=True)
        backend, eng = _loaded(_lms_spec(srv.base_url, "org/dusunur"))
        assert backend.generate(eng, _req()).text == ANSWER
        assert backend.thinking_monitor.off_unavailable.startswith("HTTP 400")
        assert not _native_gen(srv)

    def test_akis_yerine_duz_json_ve_sayaclar(self, lms_native):
        srv = lms_native(models=[FakeModel("org/dusunur", thinking=True, loaded=True)],
                         reply=lambda t: FakeReply(content=ANSWER, thinking=THOUGHT_TEXT))
        backend, _ = _loaded(_lms_spec(srv.base_url, "org/dusunur"))
        data = {"output": [{"type": "reasoning", "content": THOUGHT_TEXT},
                           {"type": "message", "content": ANSWER}],
                "stats": {"total_output_tokens": 30, "reasoning_output_tokens": 12}}
        res = backend._parse_native(data, TEXT)
        assert res.text == ANSWER and res.thinking_tokens == 12
        assert res.output_tokens == 18 and res.thinking_substantive
        assert backend._parse_native({"output": "x"}, TEXT).error

    def test_sunucu_akisi_yok_sayarsa_duz_json_ayristirilir(self, lms_native):
        srv = lms_native(models=[FakeModel("org/dusunur", thinking=True, loaded=True)],
                         reply=lambda t: FakeReply(content=ANSWER), ignore_stream=True)
        backend, eng = _loaded(_lms_spec(srv.base_url, "org/dusunur"))
        res = backend.generate(eng, _req())
        assert res.error is None and res.text == ANSWER
        assert _native_gen(srv)[-1]["stream"] is True


class TestOllamaKapatma:
    def test_think_false_govdesi_ve_istek_bilgisi(self, fake_llm_server):
        srv = fake_llm_server("ollama", models=[FakeModel(
            "qwen3:8b", thinking=True, think_values=[True, False], think_default=True)],
            reply=lambda t: FakeReply(content=ANSWER))
        backend, eng = _loaded(_ollama_spec(srv.url, "qwen3:8b"))
        assert backend.generate(eng, _req()).text == ANSWER
        body = [b for b in srv.requests_to("/api/chat") if b.get("messages")][-1]
        assert body["think"] is False
        assert backend.thinking_profile().off_path == C.THINKING_OFF_OLLAMA_THINK
        assert backend.thinking_off_requested(False) is True

    def test_gpt_oss_sert_denemede_low_kapatma_degil(self, fake_llm_server):
        srv = fake_llm_server("ollama", models=[FakeModel(
            "gpt-oss:20b", thinking=True, think_values=["low", "medium", "high"],
            think_default="medium")], reply=lambda t: FakeReply(content=ANSWER))
        backend, _ = _loaded(_ollama_spec(srv.url, "gpt-oss:20b"))
        assert backend.thinking_profile().cls == C.THINKING_ALWAYS
        assert backend.thinking_off_requested(True) is False


# ===========================================================================
# 3. Kapatma doğrulaması: yapışkan bayrak (kalem 3)
# ===========================================================================
class TestKapatmaDogrulamasi:
    def test_lmstudio_off_ragmen_dusunce_yapiskan(self, lms_native, caplog):
        replies = iter([FakeReply(content=ANSWER, thinking=THOUGHT_TEXT),
                        FakeReply(content=ANSWER), FakeReply(content=ANSWER,
                                                             thinking=THOUGHT_TEXT)])

        def reply(text, body):
            if body.get("max_output_tokens") == 1:      # ısınma isteği sırayı tüketmez
                return FakeReply(content="x")
            return next(replies, FakeReply(content=ANSWER))
        srv = lms_native(models=[FakeModel("google/gemma-4-26b-a4b", thinking=True,
                                           loaded=True)], reply=reply)
        caplog.set_level(logging.WARNING, logger=THINK_LOG)
        backend, eng = _loaded(_lms_spec(srv.base_url, "google/gemma-4-26b-a4b"))
        assert backend.thinking_class() == C.THINKING_CAN_DISABLE
        first = backend.generate(eng, _req())
        assert first.text == ANSWER and first.thinking_tokens > 0     # çeviri değişmedi
        assert backend.thinking_monitor.off_failed is True
        assert backend.thinking_class() == C.THINKING_ALWAYS
        backend.generate(eng, _req())             # düşüncesiz yanıt bayrağı geri almaz
        backend.generate(eng, _req())             # ikinci olay ikinci satır yazmaz
        assert backend.thinking_class() == C.THINKING_ALWAYS
        lines = _thinking_lines(caplog, "rağmen yanıtta düşünce geldi")
        assert len(lines) == 1
        assert C.THINKING_OFF_LMSTUDIO_REST in lines[0].getMessage()
        snap = backend.thinking_monitor.snapshot()
        assert snap["class"] == C.THINKING_CAN_DISABLE and snap["effective_class"] == \
            C.THINKING_ALWAYS and snap["first_tokens"] > 0

    def test_lmstudio_butce_kesmesi_yerel_akista_da_isler(self, lms_native):
        srv = lms_native(models=[FakeModel("org/dusunur", thinking=True, loaded=True)],
                         reply=lambda t: FakeReply(content=ANSWER, thinking=LONG_THINK))
        backend, eng = _loaded(_lms_spec(srv.base_url, "org/dusunur"))
        res = backend.generate(eng, _req())
        assert res.thinking_exceeded and res.error.startswith("Model düşünme sınırını aştı")
        assert backend.thinking_monitor.off_failed is True

    def test_ollama_think_false_ragmen_dusunce(self, fake_llm_server):
        srv = fake_llm_server("ollama", models=[FakeModel(
            "qwen3:8b", thinking=True, think_values=[True, False], think_default=True)],
            reply=lambda t: FakeReply(content=ANSWER, thinking=THOUGHT_TEXT))
        backend, eng = _loaded(_ollama_spec(srv.url, "qwen3:8b"))
        backend.generate(eng, _req())
        assert backend.thinking_monitor.off_failed is True
        assert backend.thinking_class() == C.THINKING_ALWAYS

    def test_yok_sinifli_modelde_dusunce_beklenmedik(self, fake_llm_server, caplog):
        srv = fake_llm_server("ollama", models=[FakeModel(
            "duz:7b", thinking=False, think_values=[False], think_default=False)],
            reply=lambda t: FakeReply(content="<think>" + THOUGHT_TEXT + "</think>" + ANSWER))
        caplog.set_level(logging.WARNING, logger=THINK_LOG)
        backend, eng = _loaded(_ollama_spec(srv.url, "duz:7b"))
        assert backend.thinking_profile().cls == C.THINKING_NONE
        backend.generate(eng, _req())
        assert backend.thinking_monitor.unexpected is True
        assert backend.thinking_monitor.off_failed is False
        assert backend.thinking_class() == C.THINKING_ALWAYS
        assert len(_thinking_lines(caplog, "resmî kapatma yolu yok")) == 1

    def test_kapatma_istenmeyen_kayitta_bayrak_yok(self, fake_llm_server):
        srv = fake_llm_server("ollama", models=[FakeModel(
            "qwen3:8b", thinking=True, think_values=[True, False], think_default=True)],
            reply=lambda t: FakeReply(content=ANSWER, thinking=THOUGHT_TEXT))
        backend, eng = _loaded(_ollama_spec(srv.url, "qwen3:8b", think=None))
        backend.generate(eng, _req())
        assert not backend.thinking_monitor.sticky
        assert backend.thinking_class() == C.THINKING_CAN_DISABLE

    def test_bos_dusunme_blogu_dusunce_sayilmaz(self):
        # Qwen3 "/no_think" yanıtı boş blok taşır (Hugging Face Qwen/Qwen3-8B model kartı)
        meter = B.ThinkingMeter(512)
        meter.add_whole(None, "<think>\n\n</think>\n\n" + ANSWER)
        assert meter.tokens > 0 and not meter.substantive
        monitor = T.ThinkingOffMonitor("test")
        monitor.set_profile(T.ThinkingProfile(C.THINKING_CAN_DISABLE, C.THINKING_OFF_QWEN3_SOFT))
        monitor.observe(GenerationResult(text=ANSWER, **meter.result_fields()),
                        off_requested=True)
        assert not monitor.sticky
        full = B.ThinkingMeter(512)
        full.add_whole(None, "<think>" + THOUGHT_TEXT + "</think>" + ANSWER)
        assert full.substantive and full.result_fields()["thinking_substantive"]
        monitor.observe(GenerationResult(text=ANSWER, **full.result_fields()),
                        off_requested=True)
        assert monitor.off_failed

    def test_akista_bosluk_dusunce_alani_sayilmaz(self):
        meter = B.ThinkingMeter(512)
        meter.add_thinking("\n")
        meter.add_thinking("  ")
        meter.add_content(ANSWER)
        assert meter.tokens == 2 and not meter.substantive
        meter.add_thinking("Kısa düşünce")
        assert meter.substantive

    def test_sunucu_sayaci_metinsiz_dusunce_sayilir(self):
        meter = B.ThinkingMeter(512)
        meter.add_whole(None, ANSWER, thinking_tokens=33)
        assert meter.substantive

    def test_kapatilamaz_sinifta_bayrak_konmaz(self):
        monitor = T.ThinkingOffMonitor(lambda: "etiket")
        monitor.set_profile(T.ThinkingProfile(C.THINKING_ALWAYS))
        res = GenerationResult(text=ANSWER, thinking_tokens=40, thinking_substantive=True)
        assert monitor.observe(res, off_requested=True) is res
        assert not monitor.sticky and monitor.effective_class == C.THINKING_ALWAYS

    def test_mlx_yolu_izleyici(self):
        # core/translator.py (yalnız sınıf + kapatma isteği taşıma): şablonda
        # enable_thinking varsa MLX "kapatilabilir"; kayıt kwargs'ı kapatma isteğidir.
        from core.translator import BookTranslator
        spec = EngineSpec(name="mlx:Qwen3.5-9B-MLX", format="chat", backend="mlx")
        bt = BookTranslator.__new__(BookTranslator)
        bt._engine = SimpleNamespace(spec=spec)
        bt.mt_tokenizer = SimpleNamespace(
            chat_template="{% if enable_thinking %}<think>{% endif %}{{ messages }}")
        monitor = bt._mlx_monitor()
        assert monitor.profile.off_path == C.THINKING_OFF_TEMPLATE_SWITCH
        assert bt._mlx_off_requested(False) is True
        assert bt.thinking_monitor() is monitor
        no_kwargs = EngineSpec(name="mlx:x", format="chat", backend="mlx",
                               chat_template_kwargs={})
        bt._engine = SimpleNamespace(spec=no_kwargs)
        assert bt._mlx_off_requested(False) is False


# ===========================================================================
# 4. LM Studio yüklü bağlamı (kalem 4, RAPOR MT-15)
# ===========================================================================
class TestYukluBaglam:
    def test_rest_yardimcisi(self):
        v1 = {"loaded_instances": [{"id": "g", "config": {"context_length": GEMMA_LOADED_CTX,
                                                         "parallel": 4}}]}
        assert http_util.lmstudio_loaded_config(v1) == (GEMMA_LOADED_CTX, 4)
        assert http_util.lmstudio_loaded_config({"loaded_context_length": 8192}) == (8192, None)
        assert http_util.lmstudio_loaded_config({"loaded_instances": []}) == (None, None)
        assert http_util.lmstudio_loaded_config(None) == (None, None)

    def test_bellek_hukmu_yuklu_baglamla(self):
        assert discovery._server_context(None, GEMMA_LOADED_CTX) == GEMMA_LOADED_CTX
        assert discovery._server_context(None) == D.MT_CONTEXT_TOKENS          # eski kural
        spec = EngineSpec(name="x", backend="openai", options={"num_ctx": 16384})
        assert discovery._server_context(spec) == 16384
        assert discovery._server_context(spec, GEMMA_LOADED_CTX) == GEMMA_LOADED_CTX
        weights = 15_000_000_000
        small = discovery._mem_fit(discovery._server_mem(weights, True))
        big = discovery._mem_fit(discovery._server_mem(weights, True,
                                                       loaded_context=GEMMA_LOADED_CTX))
        assert small.kv_tokens == D.MT_CONTEXT_TOKENS and big.kv_tokens == GEMMA_LOADED_CTX
        assert big.required_bytes > small.required_bytes

    def test_kesif_yuklu_modelde_yuklu_baglam(self, fake_llm_server):
        srv = fake_llm_server("lmstudio", models=[
            FakeModel("org/yuklu", loaded=True), FakeModel("org/yuklu-degil", loaded=False)])
        probe = discovery._probe_lmstudio(srv.base_url, 2.0)
        loaded, idle = probe.index["org/yuklu"], probe.index["org/yuklu-degil"]
        # sahte REST yüklü örneği context_length 4096 ile bildirir
        assert loaded.loaded_context == 4096 and loaded.memory.kv_tokens == 4096
        assert idle.loaded_context is None and idle.memory.kv_tokens == D.MT_CONTEXT_TOKENS

    def test_arka_uc_kayittaki_num_ctx_yuklu_baglamdan_buyukse_kisilir(self, fake_llm_server,
                                                                        caplog):
        srv = fake_llm_server("lmstudio", models=[FakeModel("org/yuklu", loaded=True)],
                              reply=lambda t: FakeReply(content=ANSWER))
        caplog.set_level(logging.WARNING)
        backend, _ = _loaded(_lms_spec(srv.base_url, "org/yuklu", options={"num_ctx": 8192}))
        assert backend._ctx_limit == 4096 and backend.loaded_context == 4096
        assert backend.loaded_parallel == 4
        assert any("token bağlamla yüklü" in r.getMessage() for r in caplog.records)
        backend2, _ = _loaded(_lms_spec(srv.base_url, "org/yuklu", options={"num_ctx": 2048}))
        assert backend2._ctx_limit == 2048                      # kullanıcının küçük sınırı

    def test_istege_bagli_yuklemede_isinma_sonrasi_okunur(self, fake_llm_server):
        # JIT: model ısınmayla yüklenir; yüklü bağlam ısınmadan SONRA REST'ten okunur.
        holder = {}

        def reply(text):
            holder["srv"].model("org/jit").loaded = True      # sunucu modeli yükledi
            return FakeReply(content=ANSWER)
        srv = fake_llm_server("lmstudio", models=[FakeModel("org/jit", loaded=False)],
                              reply=reply)
        holder["srv"] = srv
        backend, _ = _loaded(_lms_spec(srv.base_url, "org/jit"))
        assert backend.loaded_context == 4096 and backend._ctx_limit == 4096


# ===========================================================================
# 6. Arayüz metinleri (kalem 6)
# ===========================================================================
class TestArayuzMetinleri:
    def test_sinif_etiketleri_eslesir(self):
        assert set(branding.THINKING_LABELS) == set(C.THINKING_CLASSES)
        assert branding.THINKING_LABELS[C.THINKING_NONE] == "düşünme yok"
        assert branding.THINKING_LABELS[C.THINKING_CAN_DISABLE] == "düşünme kapalı çalışır"
        assert branding.THINKING_LABELS[C.THINKING_ALWAYS] == \
            "düşünen model — düşünerek çevirir, daha yavaştır"
        assert branding.thinking_label(None) == branding.THINKING_LABELS[C.THINKING_UNKNOWN]
        assert branding.thinking_label("tanınmayan") == \
            branding.THINKING_LABELS[C.THINKING_UNKNOWN]

    def test_beta_metni_ad_degismez(self):
        assert branding.MODEL_BETA_LABEL == "Beta"
        assert branding.MODEL_TITLE == "Yerel model ile çevir"
        assert branding.MODEL_BETA_NOTE.strip()

    def test_metinler_turkce_yol_adi_yok(self):
        texts = [*branding.THINKING_LABELS.values(), branding.THINKING_OFF_FAILED_NOTE,
                 branding.MODEL_BETA_NOTE]
        for text in texts:
            assert "Yol A" not in text and "Yol B" not in text
            for bad in ("dusun", "calis", "cevir", "yavas", "asama"):
                assert bad not in text.lower(), (bad, text)
