"""K1 — model arka uçları (Ollama, OpenAI uyumlu, llama.cpp) birim testleri.

TASARIM.md H.2 test listesinin arka uç kalemleri (1-4, 5'in motor kısmı,
7-12, 14, 18, 19) + B.3.3 motor yöntemleri. Hiçbir gerçek model YÜKLENMEZ,
gerçek Ollama / LM Studio sunucusuna gidilmez: sahte HTTP sunucusu
(tests/fakes/fake_llm_server.py) ve sahte ``llama_cpp`` modülü
(``fake_llama`` fixture'ı) kullanılır. Girdiler sentetiktir.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from config.defaults import (
    DEFAULT_RETRY_BASE_DELAY_S,
    DEFAULT_RETRY_COUNT,
    MT_CONTEXT_TOKENS,
    MT_HTTP_CIRCUIT_MAX_FAILURES,
    MT_HTTP_TIMEOUT_S,
    OLLAMA_KEEP_ALIVE,
    SEGMENT_TIMEOUT_S,
)
from core.mt_engines import backends, http_util
from core.mt_engines.backends import create_backend
from core.mt_engines.base import EngineSpec
from core.mt_engines.chat import ChatTemplateEngine
from core.mt_engines.contracts import (
    LLAMA_CPP_INSTALL_CMD,
    MSG_EMBEDDING_MODEL,
    MSG_EMPTY_OUTPUT,
    MSG_LLAMA_MISSING,
    MSG_LMSTUDIO_NOT_LOADED,
    MSG_MODEL_NOT_ON_SERVER,
    MSG_OLLAMA_DOWN,
    MSG_SERVER_LOST,
    BackendUnavailable,
    GenerationRequest,
    GenerationResult,
    has_oom_trigger,
)
from core.mt_engines.http_backends import OllamaBackend, OpenAIBackend
from core.mt_engines.hymt import HYMT_SPECIAL_TOKENS, HYMTFormatEngine
from core.mt_engines.llamacpp_backend import LlamaCppBackend
from tests.fakes import FakeModel, FakeReply, write_fake_gguf

INSTR = "Translate from English to Turkish. Output only the translation."
TEXT = "The weather is nice today."


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


def _engine_for(spec):
    return HYMTFormatEngine(spec) if spec.format == "hymt" else ChatTemplateEngine(spec)


def _loaded(spec):
    backend = create_backend(spec)
    backend.load()
    return backend, _engine_for(spec)


def _req(text=TEXT, max_tokens=512, instruction=INSTR):
    return GenerationRequest(instruction, text, max_tokens)


@pytest.fixture
def no_sleep(monkeypatch):
    """Yeniden deneme beklemesi: gerçek bekleme yok, süreler kaydedilir."""
    calls = []
    monkeypatch.setattr(backends, "_sleep", lambda s: calls.append(s))
    return calls


# ---------------------------------------------------------------------------
# Fabrika ve motor yöntemleri (B.3.3, B.3.4)
# ---------------------------------------------------------------------------
class TestFabrika:
    def test_mlx_icin_none_bugunku_yol(self):
        assert create_backend(EngineSpec(name="hymt-7b", format="hymt")) is None
        assert create_backend(EngineSpec(name="x", format="chat", backend="mlx")) is None

    def test_metin_olmayan_backend_mlx_sayilir(self):
        # MagicMock motorun kendiliğinden ürettiği öznitelik MLX yolunu
        # arka uca YÖNELTMEMELİ (MLX yolu davranışça değişmez).
        from unittest.mock import MagicMock
        assert create_backend(MagicMock()) is None

    def test_bilinmeyen_metin_backend_reddedilir(self):
        with pytest.raises(BackendUnavailable, match="bilinmeyen arka uç"):
            create_backend(EngineSpec(name="x", backend="bulut"))

    def test_turler(self, fake_ollama, fake_openai, tmp_path):
        assert isinstance(create_backend(_ollama_spec(fake_ollama.url)), OllamaBackend)
        assert isinstance(create_backend(_openai_spec(fake_openai.base_url)), OpenAIBackend)
        gguf = tmp_path / "m.gguf"
        write_fake_gguf(gguf, "qwen3")
        spec = EngineSpec(name="gguf:m.gguf", backend="llamacpp", path=str(gguf))
        assert isinstance(create_backend(spec), LlamaCppBackend)

    def test_kurucu_aga_gitmez(self, fake_ollama):
        create_backend(_ollama_spec(fake_ollama.url))
        assert fake_ollama.requests == []


class TestMotorYontemleri:
    def test_hymt_ham_prompt_mlx_ile_ayni(self):
        eng = HYMTFormatEngine(EngineSpec(name="hymt-7b", format="hymt"))
        assert eng.build_raw_prompt(INSTR, TEXT) == eng.build_mlx_prompt(None, INSTR, TEXT)
        assert eng.build_raw_prompt(INSTR, TEXT).startswith("<|startoftext|>")

    def test_chat_ham_prompt_kurmaz(self):
        eng = ChatTemplateEngine(EngineSpec(name="c", format="chat"))
        with pytest.raises(NotImplementedError):
            eng.build_raw_prompt(INSTR, TEXT)

    def test_chat_mesajlari_varsayilan_tek_user(self):
        eng = ChatTemplateEngine(EngineSpec(name="c", format="chat"))
        assert eng.build_chat_messages("talimat", "metin") == [
            {"role": "user", "content": "talimat\n\nmetin"}]

    def test_chat_mesajlari_system_rolu(self):
        eng = ChatTemplateEngine(EngineSpec(name="c", format="chat", system_prompt=True))
        assert eng.build_chat_messages("talimat", "metin") == [
            {"role": "system", "content": "talimat"}, {"role": "user", "content": "metin"}]

    def test_mlx_sablonuna_dusunme_kapali_gecer(self):
        seen = []

        class Tok:
            def apply_chat_template(self, messages, tokenize, add_generation_prompt, **kw):
                seen.append(kw)
                return "PROMPT"

        eng = ChatTemplateEngine(EngineSpec(name="c", format="chat"))
        assert eng.build_mlx_prompt(Tok(), INSTR, TEXT) == "PROMPT"
        assert seen == [{"enable_thinking": False}]

    def test_mlx_sablonu_kwargs_reddederse_kwargssiz_dener(self):
        seen = []

        class Tok:
            def apply_chat_template(self, messages, tokenize, add_generation_prompt, **kw):
                seen.append(kw)
                if kw:
                    raise TypeError("beklenmeyen argüman")
                return "PROMPT"

        eng = ChatTemplateEngine(EngineSpec(name="c", format="chat"))
        assert eng.build_mlx_prompt(Tok(), INSTR, TEXT) == "PROMPT"
        assert seen == [{"enable_thinking": False}, {}]

    def test_resolve_local_model_dir_spec_path(self, tmp_path):
        explicit = tmp_path / "baska" / "model-mlx"
        explicit.mkdir(parents=True)
        (tmp_path / "models" / "yerel").mkdir(parents=True)
        spec = EngineSpec(name="mlx:x", format="chat", local_dir="yerel", path=str(explicit))
        eng = ChatTemplateEngine(spec)
        assert eng.resolve_local_model_dir(tmp_path / "models") == explicit
        # path yoksa eski davranış (models/<local_dir>)
        spec2 = EngineSpec(name="mlx:x", format="chat", local_dir="yerel",
                           path=str(tmp_path / "yok"))
        assert (ChatTemplateEngine(spec2).resolve_local_model_dir(tmp_path / "models")
                == tmp_path / "models" / "yerel")

    def test_hymt_temizligi_kaynaktan_bagimsiz(self):
        eng = HYMTFormatEngine(EngineSpec(name="hymt-7b", format="hymt"))
        raw = '"Merhaba dünya."<|eos|>'
        assert eng.clean_output(raw) == eng.clean_output(raw, source="Hello world.")
        assert eng.clean_output(raw) == '"Merhaba dünya."'


# ---------------------------------------------------------------------------
# Temizlik zinciri — sohbet biçimi (H.2 test 5, motor kısmı; TASARIM G)
# ---------------------------------------------------------------------------
class TestSohbetTemizligi:
    @pytest.fixture
    def eng(self):
        return ChatTemplateEngine(EngineSpec(name="c", format="chat"))

    def test_think_blogu_silinir(self, eng):
        raw = "<think>\nÖnce düşünelim.\n</think>\n\nBugün hava güzel."
        assert eng.clean_output(raw, source=TEXT) == "Bugün hava güzel."

    def test_kapanis_etiketiyle_baslayan_cikti(self, eng):
        raw = "şablon düşünmeyi başlatmış</think>Bugün hava güzel."
        assert eng.clean_output(raw, source=TEXT) == "Bugün hava güzel."

    def test_kapanmamis_think_bos_cikti(self, eng):
        assert eng.clean_output("<think>bütçe düşünmeye gitti", source=TEXT) == ""

    def test_kaynakta_think_varsa_dokunulmaz(self, eng):
        src = "Write <think> literally."
        out = "<think> etiketini aynen yaz."
        assert eng.clean_output(out, source=src) == out

    def test_harmony_final_kanali(self, eng):
        raw = ("<|channel|>analysis<|message|>düşünce<|end|><|start|>assistant"
               "<|channel|>final<|message|>Bugün hava güzel.")
        assert eng.clean_output(raw, source=TEXT) == "Bugün hava güzel."

    @pytest.mark.parametrize("raw", [
        "Here is the translation: Bugün hava güzel.",
        "Sure! Here's the Turkish translation:\nBugün hava güzel.",
        "Çeviri: Bugün hava güzel.",
        "İşte çeviri:\n\nBugün hava güzel.",
        "Türkçe çevirisi: Bugün hava güzel.",
    ])
    def test_onsoz_silinir(self, eng, raw):
        assert eng.clean_output(raw, source=TEXT) == "Bugün hava güzel."

    def test_kaynak_etiketle_basliyorsa_onsoz_korunur(self, eng):
        src = "Translation: a rendering of text into another language."
        out = "Çeviri: bir metnin başka bir dile aktarılması."
        assert eng.clean_output(out, source=src) == out

    @pytest.mark.parametrize("src", ["Its translation: the word means water.",
                                     "Die Übersetzung: das Wort bedeutet Wasser."])
    def test_kaynak_ceviri_sozcuguyle_basliyorsa_onsoz_korunur(self, eng, src):
        # Dilbilim/çeviri kitabı: "Çevirisi:" meşru içerik, önsöz değil.
        out = "Çevirisi: sözcük su anlamına gelir."
        assert eng.clean_output(out, source=src) == out

    def test_yalniz_onsoz_varsa_silinmez(self, eng):
        assert eng.clean_output("Çeviri:", source=TEXT) == "Çeviri:"

    @pytest.mark.parametrize("raw", ['"Bugün hava güzel."', "“Bugün hava güzel.”",
                                     "```\nBugün hava güzel.\n```"])
    def test_sarmalayici_kaldirilir(self, eng, raw):
        assert eng.clean_output(raw, source=TEXT) == "Bugün hava güzel."

    def test_kaynak_tirnakliysa_tirnak_korunur(self, eng):
        out = '"Bugün hava güzel."'
        assert eng.clean_output(out, source='"The weather is nice today."') == out

    def test_icte_tirnak_varsa_sarmalayici_sanilmaz(self, eng):
        out = '"Evet" dedi, "gidelim."'
        assert eng.clean_output(out, source='"Yes" he said, "let us go."') == out

    def test_sona_eklenen_not_silinir(self, eng):
        raw = "Bugün hava güzel.\n\nNot: 'nice' sözcüğü 'güzel' olarak çevrildi."
        assert eng.clean_output(raw, source=TEXT) == "Bugün hava güzel."

    def test_kaynagin_kendi_son_paragrafi_korunur(self, eng):
        # "Remark:" -> "Not:" kaynakta karşılığı olan paragraf; model eklemedi.
        src = "The weather is nice.\n\nRemark: it may rain later."
        raw = "Hava güzel.\n\nNot: daha sonra yağmur yağabilir."
        assert eng.clean_output(raw, source=src) == raw

    def test_kaynakta_not_varsa_korunur(self, eng):
        src = "The weather is nice.\n\nNote: forecast may change."
        raw = "Hava güzel.\n\nNot: tahmin değişebilir."
        assert eng.clean_output(raw, source=src) == raw

    def test_kaynaksiz_cagri_yalniz_dusunmeyi_temizler(self, eng):
        # MLX yolu clean_output(raw) çağırır (kaynak yok): meşru tırnak silinmez.
        assert eng.clean_output('"Bugün hava güzel."') == '"Bugün hava güzel."'
        assert eng.clean_output("<think>x</think>Bugün.") == "Bugün."


# ---------------------------------------------------------------------------
# Ollama (H.2 test 1, 2, 4)
# ---------------------------------------------------------------------------
class TestOllamaGovde:
    def test_sohbet_govdesi(self, fake_ollama):
        backend, eng = _loaded(_ollama_spec(fake_ollama.url))
        res = backend.generate(eng, _req(max_tokens=777))
        assert res.error is None and res.text == "ÇEVRİLDİ: " + TEXT
        assert res.finish_reason == "stop"
        body = fake_ollama.requests_to("/api/chat")[-1]
        assert body["model"] == "qwen3:8b"
        assert body["messages"] == [{"role": "user", "content": f"{INSTR}\n\n{TEXT}"}]
        assert body["stream"] is False
        opts = body["options"]
        assert opts["temperature"] == 0 and opts["seed"] == 0
        assert opts["repeat_penalty"] == 1.0
        assert opts["num_predict"] == 777
        assert opts["num_ctx"] == MT_CONTEXT_TOKENS
        assert body["keep_alive"] == OLLAMA_KEEP_ALIVE
        # Düşünme yeteneği bildirmeyen modele "think" GÖNDERİLMEZ (400 alırdı)
        assert "think" not in body

    def test_num_ctx_kosu_boyunca_sabit(self, fake_ollama):
        backend, eng = _loaded(_ollama_spec(fake_ollama.url))
        backend.generate(eng, _req(max_tokens=100))
        backend.generate(eng, _req(max_tokens=2000))
        warm = fake_ollama.requests_to("/api/generate")[0]
        ctxs = {warm["options"]["num_ctx"]} | {
            b["options"]["num_ctx"] for b in fake_ollama.requests_to("/api/chat")}
        assert ctxs == {MT_CONTEXT_TOKENS}

    def test_dusunen_modelde_think_false(self, fake_llm_server):
        srv = fake_llm_server("ollama", thinking_capability=True)
        backend, eng = _loaded(_ollama_spec(srv.url))
        res = backend.generate(eng, _req())
        assert res.text == "ÇEVRİLDİ: " + TEXT
        assert srv.requests_to("/api/chat")[-1]["think"] is False

    def test_think_none_ise_alan_gonderilmez(self, fake_llm_server):
        srv = fake_llm_server("ollama", thinking_capability=True)
        backend, eng = _loaded(_ollama_spec(srv.url, think=None))
        backend.generate(eng, _req())
        assert "think" not in srv.requests_to("/api/chat")[-1]

    def test_think_reddedilirse_alansiz_yinelenir_yapiskan(self, fake_llm_server):
        srv = fake_llm_server("ollama", thinking_capability=True)
        backend, eng = _loaded(_ollama_spec(srv.url))
        srv.set_status("/api/chat", 400, {"error": "unknown field think"}, times=1)
        assert backend.generate(eng, _req()).error is None
        assert backend.generate(eng, _req()).error is None
        bodies = srv.requests_to("/api/chat")
        assert len(bodies) == 3
        assert "think" in bodies[0] and "think" not in bodies[1] and "think" not in bodies[2]

    def test_hymt_ham_generate(self, fake_llm_server):
        srv = fake_llm_server("ollama", models=["hymt"])
        spec = _ollama_spec(srv.url, model="hymt", format="hymt", raw=True)
        backend, eng = _loaded(spec)
        res = backend.generate(eng, _req())
        assert res.text == "ÇEVRİLDİ: " + TEXT
        body = [b for b in srv.requests_to("/api/generate") if b.get("prompt")][-1]
        assert body["raw"] is True
        assert body["prompt"] == eng.build_mlx_prompt(None, INSTR, TEXT)
        assert srv.requests_to("/api/chat") == []

    def test_hymt_raw_kaydi_yoksa_sohbet_ucu(self, fake_llm_server):
        # B.8: format hymt ama kayıtta raw yok -> /api/chat + taban mesajları
        srv = fake_llm_server("ollama", models=["hymt"])
        backend, eng = _loaded(_ollama_spec(srv.url, model="hymt", format="hymt"))
        backend.generate(eng, _req())
        body = srv.requests_to("/api/chat")[-1]
        assert body["messages"] == [{"role": "user", "content": f"{INSTR}\n\n{TEXT}"}]

    def test_dusunme_alani_ve_uzunluk(self, fake_llm_server):
        srv = fake_llm_server("ollama", finish_reason="length")
        srv.reply = lambda t: FakeReply(content="Bugün hava", thinking="gizli düşünce")
        backend, eng = _loaded(_ollama_spec(srv.url))
        res = backend.generate(eng, _req())
        assert res.text == "Bugün hava" and res.finish_reason == "length"

    def test_bos_icerik_oge_hatasi(self, fake_llm_server):
        srv = fake_llm_server("ollama", reply=lambda t: "")
        backend, eng = _loaded(_ollama_spec(srv.url))
        assert backend.generate(eng, _req()).error == MSG_EMPTY_OUTPUT
        assert backend.health() is None


class TestOllamaYukleme:
    def test_show_sonra_isinma(self, fake_ollama):
        backend = create_backend(_ollama_spec(fake_ollama.url))
        backend.load()
        paths = [p for _, p, _ in fake_ollama.requests]
        assert paths == ["/api/show", "/api/generate"]
        warm = fake_ollama.requests_to("/api/generate")[0]
        assert warm["prompt"] == "" and warm["keep_alive"] == OLLAMA_KEEP_ALIVE

    def test_baglam_uzunlugu_num_ctxi_sinirlar(self, fake_llm_server):
        srv = fake_llm_server("ollama", models=[FakeModel("qwen3:8b", context_length=4096)])
        backend, _ = _loaded(_ollama_spec(srv.url))
        assert backend.num_ctx == 4096

    def test_model_sunucuda_yok(self, fake_ollama):
        backend = create_backend(_ollama_spec(fake_ollama.url, model="yok:1b"))
        with pytest.raises(BackendUnavailable) as exc:
            backend.load()
        assert str(exc.value) == MSG_MODEL_NOT_ON_SERVER.format(model="yok:1b", server="Ollama")

    def test_gomme_modeli_reddedilir(self, fake_llm_server):
        srv = fake_llm_server("ollama", models=[FakeModel("nomic", kind="embedding")])
        with pytest.raises(BackendUnavailable) as exc:
            create_backend(_ollama_spec(srv.url, model="nomic")).load()
        assert str(exc.value) == MSG_EMBEDDING_MODEL

    def test_sunucu_kapali(self, mt_isolation):
        backend = create_backend(_ollama_spec(mt_isolation.closed_url))
        with pytest.raises(BackendUnavailable) as exc:
            backend.load()
        assert str(exc.value) == MSG_OLLAMA_DOWN.format(url=mt_isolation.closed_url)


# ---------------------------------------------------------------------------
# OpenAI uyumlu / LM Studio (H.2 test 3, 4)
# ---------------------------------------------------------------------------
class TestOpenAI:
    def test_sohbet_govdesi_ve_basliklar(self, fake_openai):
        spec = _openai_spec(fake_openai.base_url,
                            extra_body={"top_k": 1, "model": "ezilemez", "stream": True})
        backend, eng = _loaded(spec)
        res = backend.generate(eng, _req(max_tokens=321))
        assert res.text == "ÇEVRİLDİ: " + TEXT and res.error is None
        body = fake_openai.requests_to("/v1/chat/completions")[-1]
        assert body["model"] == "qwen"          # extra_body model'i ezemez
        assert body["stream"] is False          # ... stream'i de
        assert body["max_tokens"] == 321
        assert body["temperature"] == 0 and body["seed"] == 0
        assert body["top_k"] == 1               # extra_body birleşir
        assert body["chat_template_kwargs"] == {"enable_thinking": False}
        assert body["messages"] == [{"role": "user", "content": f"{INSTR}\n\n{TEXT}"}]
        for rec in fake_openai.request_details:
            assert "authorization" not in {k.lower() for k in rec.headers}

    def test_yukleme_liste_sonra_tek_tokenlik_isinma(self, fake_openai):
        create_backend(_openai_spec(fake_openai.base_url)).load()
        assert [p for _, p, _ in fake_openai.requests] == ["/v1/models", "/v1/chat/completions"]
        assert fake_openai.requests_to("/v1/chat/completions")[0]["max_tokens"] == 1

    def test_model_listede_yok(self, fake_openai):
        backend = create_backend(_openai_spec(fake_openai.base_url, model="baska-model"))
        with pytest.raises(BackendUnavailable) as exc:
            backend.load()
        assert "baska-model" in str(exc.value)
        assert str(exc.value) == MSG_MODEL_NOT_ON_SERVER.format(
            model="baska-model", server=fake_openai.host_port)

    def test_lmstudio_yuklu_degil_iletisi(self, fake_lmstudio):
        spec = _openai_spec(fake_lmstudio.base_url, model="yok/model", flavor="lmstudio")
        with pytest.raises(BackendUnavailable) as exc:
            create_backend(spec).load()
        assert str(exc.value) == MSG_LMSTUDIO_NOT_LOADED.format(model="yok/model")

    def test_lmstudio_gomme_modeli(self, fake_llm_server):
        srv = fake_llm_server("lmstudio", models=[FakeModel("org/embed", kind="embedding")])
        spec = _openai_spec(srv.base_url, model="org/embed", flavor="lmstudio")
        with pytest.raises(BackendUnavailable, match="gömme"):
            create_backend(spec).load()

    def test_chat_template_kwargs_reddi_yapiskan(self, fake_llm_server):
        srv = fake_llm_server("openai", reject_body_keys=("chat_template_kwargs",))
        backend, eng = _loaded(_openai_spec(srv.base_url))
        assert backend.generate(eng, _req()).error is None
        assert backend.generate(eng, _req()).error is None
        sent = srv.requests_to("/v1/chat/completions")
        # ısınma: reddedildi + alansız yinelendi; sonrakiler hiç göndermez
        assert "chat_template_kwargs" in sent[0]
        assert all("chat_template_kwargs" not in b for b in sent[1:])

    def test_reasoning_content_okunmaz_bos_icerik_hatasi(self, fake_llm_server):
        srv = fake_llm_server("openai", reasoning_content="gizli düşünce", content_none=True)
        backend, eng = _loaded(_openai_spec(srv.base_url))
        res = backend.generate(eng, _req())
        assert res.error == MSG_EMPTY_OUTPUT and res.text == ""

    def test_ham_completions(self, fake_llm_server):
        srv = fake_llm_server("openai", models=["hymt"])
        spec = _openai_spec(srv.base_url, model="hymt", format="hymt", raw=True)
        backend, eng = _loaded(spec)
        assert backend.generate(eng, _req()).text == "ÇEVRİLDİ: " + TEXT
        body = srv.requests_to("/v1/completions")[-1]
        assert body["prompt"] == eng.build_raw_prompt(INSTR, TEXT)


# ---------------------------------------------------------------------------
# Hata tablosu, devre kesici (H.2 test 7, 8, 9, 10, 11)
# ---------------------------------------------------------------------------
class TestHataTablosu:
    def test_503_iki_kez_sonra_basari(self, fake_ollama, no_sleep):
        backend, eng = _loaded(_ollama_spec(fake_ollama.url))
        fake_ollama.set_status("/api/chat", 503, times=2)
        res = backend.generate(eng, _req())
        assert res.error is None and res.text == "ÇEVRİLDİ: " + TEXT
        assert len(fake_ollama.requests_to("/api/chat")) == 3
        assert no_sleep == [DEFAULT_RETRY_BASE_DELAY_S, DEFAULT_RETRY_BASE_DELAY_S * 2]
        assert backend.health() is None

    def test_kalici_503_devreyi_acar_sonra_aga_gidilmez(self, fake_ollama, no_sleep):
        backend, eng = _loaded(_ollama_spec(fake_ollama.url))
        fake_ollama.set_status("/api/chat", 503)
        lost = MSG_SERVER_LOST.format(url=fake_ollama.url)
        for call in range(1, MT_HTTP_CIRCUIT_MAX_FAILURES + 1):
            with pytest.raises(BackendUnavailable) as exc:
                backend.generate(eng, _req())
            # 2026-09-29 (F4-05): "kalan segmentler denenmeden geçildi" yalnız
            # devre gerçekten açıldığında; öncesinde parça başına ileti.
            if call < MT_HTTP_CIRCUIT_MAX_FAILURES:
                assert str(exc.value).startswith("Model sunucusu ")
                assert "denenmeden" not in str(exc.value)
            else:
                assert str(exc.value) == lost
        per_call = DEFAULT_RETRY_COUNT + 1
        assert len(fake_ollama.requests_to("/api/chat")) == per_call * MT_HTTP_CIRCUIT_MAX_FAILURES
        assert backend.health() == lost
        before = len(fake_ollama.requests)
        with pytest.raises(BackendUnavailable):
            backend.generate(eng, _req())
        with pytest.raises(BackendUnavailable):
            backend.generate_batch(eng, [_req(), _req()])
        assert len(fake_ollama.requests) == before      # devre açık: ağa gidilmedi

    def test_durdurulan_sunucu(self, fake_ollama, no_sleep, monkeypatch):
        backend, eng = _loaded(_ollama_spec(fake_ollama.url))
        fake_ollama.stop()
        # 2026-09-29 (F4-05): devre açılana kadar "yanıt vermedi", açılınca "bağlantı koptu"
        for _ in range(MT_HTTP_CIRCUIT_MAX_FAILURES - 1):
            with pytest.raises(BackendUnavailable, match="yanıt vermedi"):
                backend.generate(eng, _req())
        with pytest.raises(BackendUnavailable, match="bağlantı koptu"):
            backend.generate(eng, _req())
        assert backend.health() == MSG_SERVER_LOST.format(url=backend.base_url)
        calls = []
        real = http_util.post_json
        monkeypatch.setattr(http_util, "post_json",
                            lambda *a, **k: calls.append(a) or real(*a, **k))
        with pytest.raises(BackendUnavailable):
            backend.generate(eng, _req())
        assert calls == []

    @pytest.mark.parametrize("error", [
        "model requires more system memory (12.3 GiB) than is available (8.0 GiB)",
        "CUDA out of memory; failed to allocate buffer",
    ])
    def test_bellek_hatasi_hemen_acar(self, fake_ollama, no_sleep, error):
        backend, eng = _loaded(_ollama_spec(fake_ollama.url))
        fake_ollama.set_status("/api/chat", 500, {"error": error})
        with pytest.raises(BackendUnavailable) as exc:
            backend.generate(eng, _req())
        msg = str(exc.value)
        assert msg.startswith("Model belleğe sığmadı (Ollama)")
        assert not has_oom_trigger(msg)
        assert len(fake_ollama.requests_to("/api/chat")) == 1    # yeniden deneme yok
        assert no_sleep == []
        assert backend.health() == msg

    def test_404_model_yok_devreyi_acar(self, fake_ollama, no_sleep):
        backend, eng = _loaded(_ollama_spec(fake_ollama.url))
        fake_ollama.set_status("/api/chat", 404)
        with pytest.raises(BackendUnavailable) as exc:
            backend.generate(eng, _req())
        assert str(exc.value) == MSG_MODEL_NOT_ON_SERVER.format(model="qwen3:8b", server="Ollama")
        assert backend.health() is not None

    def test_okuma_zaman_asimi_yalniz_o_oge(self, fake_ollama, monkeypatch):
        backend, eng = _loaded(_ollama_spec(fake_ollama.url))
        monkeypatch.setattr(backends, "MT_HTTP_TIMEOUT_S", 0.4)
        fake_ollama.set_delay("/api/chat", 2.0, times=1)
        texts = ["First sentence here.", "Second sentence here.", "Third sentence here."]
        results = backend.generate_batch(eng, [_req(t) for t in texts])
        assert len(results) == 3
        assert results[0].error == "Model 0,4 sn içinde yanıt vermedi."
        assert [r.text for r in results[1:]] == ["ÇEVRİLDİ: " + t for t in texts[1:]]
        assert backend.health() is None                   # öğe hatası devreyi açmaz

    def test_baglam_asimi_istek_gondermez(self, fake_llm_server):
        srv = fake_llm_server("ollama", models=[FakeModel("qwen3:8b", context_length=256)])
        backend, eng = _loaded(_ollama_spec(srv.url))
        res = backend.generate(eng, _req(text="word " * 200, max_tokens=100))
        assert res.error is not None and res.error.startswith("Segment modelin bağlam sınırını")
        assert srv.requests_to("/api/chat") == []

    def test_sunucu_baglam_400_oge_hatasi(self, fake_openai, no_sleep):
        backend, eng = _loaded(_openai_spec(fake_openai.base_url))
        fake_openai.set_status("/v1/chat/completions", 400, {"error": {
            "message": "the request exceeds the available context size (4096 tokens)"}})
        res = backend.generate(eng, _req())
        assert res.error is not None
        assert res.error.startswith("Segment modelin bağlam sınırını")
        assert res.error.endswith("> 4096 token).")
        assert backend.health() is None and no_sleep == []

    def test_gecici_429_yeniden_denenir(self, fake_openai, no_sleep):
        backend, eng = _loaded(_openai_spec(fake_openai.base_url))
        fake_openai.set_status("/v1/chat/completions", 429, times=1)
        assert backend.generate(eng, _req()).error is None
        assert no_sleep == [DEFAULT_RETRY_BASE_DELAY_S]


# ---------------------------------------------------------------------------
# Toplu üretim (H.2 test 12)
# ---------------------------------------------------------------------------
class TestTopluUretim:
    def test_varsayilan_sirali(self, fake_llm_server):
        srv = fake_llm_server("ollama", delay_s=0.1)
        backend, eng = _loaded(_ollama_spec(srv.url))
        srv.reset_requests()
        out = backend.generate_batch(eng, [_req(f"Sentence {i} here.") for i in range(3)])
        assert [r.text for r in out] == [f"ÇEVRİLDİ: Sentence {i} here." for i in range(3)]
        assert srv.max_concurrent == 1

    def test_max_parallel_2_ortusur_sira_korunur(self, fake_llm_server):
        srv = fake_llm_server("ollama", delay_s=0.3)
        backend, eng = _loaded(_ollama_spec(srv.url, options={"max_parallel": 2}))
        srv.reset_requests()
        texts = [f"Sentence {i} here." for i in range(4)]
        out = backend.generate_batch(eng, [_req(t) for t in texts])
        assert [r.text for r in out] == ["ÇEVRİLDİ: " + t for t in texts]
        assert srv.max_concurrent == 2

    def test_bos_liste(self, fake_ollama):
        backend, eng = _loaded(_ollama_spec(fake_ollama.url))
        assert backend.generate_batch(eng, []) == []

    def test_oge_hatasi_uzunlugu_bozmaz(self, fake_llm_server):
        srv = fake_llm_server("ollama")
        srv.reply = lambda t: "" if "BOŞ" in t else "Tamam: " + t
        backend, eng = _loaded(_ollama_spec(srv.url))
        out = backend.generate_batch(eng, [_req("Keep one."), _req("BOŞ two."), _req("Keep three.")])
        assert [r.error for r in out] == [None, MSG_EMPTY_OUTPUT, None]

    def test_beklenmeyen_istisna_oge_hatasi(self, fake_ollama, monkeypatch):
        backend, eng = _loaded(_ollama_spec(fake_ollama.url))
        calls = {"n": 0}
        real = backend.generate

        def flaky(engine, req):
            calls["n"] += 1
            if calls["n"] == 1:
                raise KeyError("beklenmeyen")
            return real(engine, req)

        monkeypatch.setattr(backend, "generate", flaky)
        out = backend.generate_batch(eng, [_req("One here."), _req("Two here.")])
        assert out[0].error and "KeyError" in out[0].error
        assert out[1].text == "ÇEVRİLDİ: Two here."


# ---------------------------------------------------------------------------
# llama.cpp (H.2 test 14)
# ---------------------------------------------------------------------------
class TestLlamaCpp:
    @pytest.fixture
    def gguf(self, tmp_path):
        path = tmp_path / "Qwen3-8B-Q4_K_M.gguf"
        write_fake_gguf(path, "qwen3")
        return path

    def _spec(self, path, fmt="chat", **kw):
        return EngineSpec(name=f"gguf:{Path(path).name}", format=fmt, backend="llamacpp",
                          path=str(path), source="adhoc", **kw)

    def test_modul_yoksa_kurulum_iletisi(self, gguf):
        backend = create_backend(self._spec(gguf))
        with pytest.raises(BackendUnavailable) as exc:
            backend.load()
        assert str(exc.value) == MSG_LLAMA_MISSING
        assert exc.value.hint == LLAMA_CPP_INSTALL_CMD

    def test_yukleme_argumanlari(self, fake_llama, gguf):
        create_backend(self._spec(gguf)).load()
        kw = fake_llama.instances[-1].kwargs
        assert kw["model_path"] == str(gguf)
        assert kw["n_gpu_layers"] == -1
        assert kw["n_ctx"] == MT_CONTEXT_TOKENS
        assert kw["seed"] == 0 and kw["verbose"] is False

    def test_secenekler_n_ctx_n_gpu_layers(self, fake_llama, gguf):
        create_backend(self._spec(gguf, options={"n_ctx": 4096, "n_gpu_layers": 0})).load()
        kw = fake_llama.instances[-1].kwargs
        assert kw["n_ctx"] == 4096 and kw["n_gpu_layers"] == 0

    def test_sohbet_bicimi_chat_completion(self, fake_llama, gguf):
        spec = self._spec(gguf)
        backend, eng = _loaded(spec)
        res = backend.generate(eng, _req(max_tokens=64))
        assert res.text == "ÇEVRİLDİ: " + TEXT
        name, kw = fake_llama.calls[-1]
        assert name == "create_chat_completion"
        assert kw["messages"] == [{"role": "user", "content": f"{INSTR}\n\n{TEXT}"}]
        assert kw["temperature"] == 0 and kw["max_tokens"] == 64 and kw["top_p"] == 1.0

    def test_hymt_bicimi_ham_completion(self, fake_llama, gguf):
        spec = self._spec(gguf, fmt="hymt")
        backend, eng = _loaded(spec)
        assert backend.generate(eng, _req()).text == "ÇEVRİLDİ: " + TEXT
        name, kw = fake_llama.calls[-1]
        assert name == "create_completion"
        assert kw["prompt"] == eng.build_mlx_prompt(None, INSTR, TEXT)
        assert set(kw["stop"]) <= set(HYMT_SPECIAL_TOKENS) and kw["stop"]

    def test_sure_siniri_msg_timeout(self, fake_llama, gguf):
        fake_llama.config.token_delay_s = 0.05
        fake_llama.config.reply = lambda t: " ".join(["sözcük"] * 50)
        backend, eng = _loaded(self._spec(gguf, options={"request_timeout_s": 0.2}))
        t0 = time.monotonic()
        res = backend.generate(eng, _req())
        assert res.error == "Model 0,2 sn içinde yanıt vermedi."
        assert time.monotonic() - t0 < 2.0            # zombi üretim sınırlandı
        assert fake_llama.calls[-1][1]["stopping_criteria"] is not None

    def test_dosya_yok(self, fake_llama, tmp_path):
        with pytest.raises(BackendUnavailable, match="GGUF dosyası bulunamadı"):
            create_backend(self._spec(tmp_path / "yok.gguf")).load()

    def test_yukleme_hatasi(self, fake_llama, gguf):
        fake_llama.config.load_error = "unknown model architecture: 'yeni'"
        with pytest.raises(BackendUnavailable, match="yüklenemedi"):
            create_backend(self._spec(gguf)).load()

    def test_baglam_asimi_cagri_yapmaz(self, fake_llama, gguf):
        backend, eng = _loaded(self._spec(gguf, options={"n_ctx": 128}))
        n_calls = len(fake_llama.calls)
        res = backend.generate(eng, _req(text="word " * 200, max_tokens=64))
        assert res.error and res.error.startswith("Segment modelin bağlam sınırını")
        assert len(fake_llama.calls) == n_calls

    def test_sirali_ve_kesik_cikti(self, fake_llama, gguf):
        fake_llama.config.reply = lambda t: " ".join(["sözcük"] * 20)
        backend, eng = _loaded(self._spec(gguf, options={"max_parallel": 4}))
        assert backend.max_parallel() == 1
        res = backend.generate(eng, _req(max_tokens=5))
        assert res.finish_reason == "length" and res.error is None


# ---------------------------------------------------------------------------
# Sabitler ve yerellik (H.2 test 18, 19)
# ---------------------------------------------------------------------------
class TestSabitVeYerellik:
    def test_http_zaman_asimi_segment_zaman_asimindan_kucuk(self):
        assert MT_HTTP_TIMEOUT_S < SEGMENT_TIMEOUT_S

    @pytest.mark.parametrize("url", ["http://10.0.0.5:11434", "http://192.168.1.2:8080/v1",
                                     "http://100.64.0.1:11434", "https://api.example.com/v1"])
    def test_uzak_adres_reddedilir(self, url):
        with pytest.raises(BackendUnavailable, match="yerel bir adres değil"):
            create_backend(_ollama_spec(url))
        with pytest.raises(BackendUnavailable, match="yerel bir adres değil"):
            create_backend(_openai_spec(url))

    def test_proxy_ortam_degiskeni_yok_sayilir(self, fake_ollama, monkeypatch, mt_isolation):
        for var in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
            monkeypatch.setenv(var, mt_isolation.closed_url)
        for var in ("NO_PROXY", "no_proxy"):
            monkeypatch.delenv(var, raising=False)
        backend, eng = _loaded(_ollama_spec(fake_ollama.url))
        assert backend.generate(eng, _req()).text == "ÇEVRİLDİ: " + TEXT

    def test_describe(self, fake_ollama, fake_lmstudio):
        b = create_backend(_ollama_spec(fake_ollama.url))
        assert b.describe() == f"Ollama · qwen3:8b · {fake_ollama.url}"
        lm = create_backend(_openai_spec(fake_lmstudio.base_url, model="qwen/qwen3-8b",
                                         flavor="lmstudio"))
        assert lm.describe().startswith("LM Studio · qwen/qwen3-8b · ")


@pytest.mark.parametrize("module", ["core.mt_engines.backends", "core.mt_engines.http_backends",
                                    "core.mt_engines.llamacpp_backend", "core.mt_engines.chat"])
def test_arka_uc_modulleri_hafif(module):
    """H.2 kabul: HTTP/llama modülleri mlx, torch, requests, httpx, openai,
    llama_cpp İÇE AKTARMAZ (llama_cpp yalnız load() içinde)."""
    import subprocess
    import sys

    heavy = ("mlx", "mlx_lm", "torch", "transformers", "requests", "httpx", "openai",
             "llama_cpp")
    code = (f"import sys; import {module}; "
            f"print(','.join(m for m in {heavy!r} if m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], cwd=str(Path(__file__).parent.parent),
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "", f"ağır modül içe aktarıldı: {out.stdout!r}"


def test_generation_result_varsayilan():
    assert GenerationResult() == GenerationResult(text="", finish_reason=None, error=None)
