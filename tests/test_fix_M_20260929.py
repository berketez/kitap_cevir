"""M kulvarı (29 Eylül 2026): model arka uçları + donanım bulgularının regresyon testleri.

Bulgular (bug avı, 2026-09-28): F4-01 (Ollama bulut
modeli yerel sayılıyordu), F4-03 (bağlam ön denetimi sığacak paragrafı
reddediyordu), F4-04 (akış 'done'suz bitince ham OSError), F4-05 (parçaya
özgü kalıcı 5xx "bağlantı koptu" sayılıyordu), F4-06 (keep_alive metne
çevriliyor, 0 yutuluyordu), F4-07 (toplu üretim yarıda kalınca başarılı
sonuçlar atılıyordu), F4-08/F5-14 ("MLX klasörleri: 0 klasör"), F4-10
(tarama sessizce kesiliyordu), F4-11 (akışta toplam süre satır içinde
işlemiyordu), F4-13 (bellek satırı yuvarlama çelişkisi), F4-14 (liste
config.json yanlış neden; BOM'lu mt_engines.json kayıtları kayboluyordu),
F5-10 (açık ama yavaş Ollama "kapalı" görünüyordu).

Model YÜKLENMEZ; gerçek Ollama / LM Studio'ya gidilmez (conftest yalıtımı +
sahte sunucular, rastgele port). Girdiler sentetiktir.
"""

from __future__ import annotations

import codecs
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from config.defaults import (
    DEFAULT_RETRY_COUNT,
    MT_CONTEXT_TOKENS,
    MT_HTTP_CIRCUIT_MAX_FAILURES,
    OLLAMA_KEEP_ALIVE,
)
from core import hardware
from core.mt_engines import backends, contracts, discovery, http_util, registry
from core.mt_engines.backends import create_backend
from core.mt_engines.base import EngineSpec
from core.mt_engines.chat import ChatTemplateEngine
from core.mt_engines import http_backends
from core.mt_engines.contracts import (
    MSG_CONTEXT_EXCEEDED,
    MSG_SERVER_LOST,
    BackendUnavailable,
    GenerationRequest,
)
from tests.fakes import FakeModel, FakeReply, write_fake_mlx_dir

# Yeni adlar (iletiler, yardımcılar) modül özniteliği olarak ÇAĞRI anında
# okunur: dosya düzeltmesiz kodda da içe aktarılır ve her test ayrı ayrı
# kırmızı olur (düzeltme kanıtı; git stash yok).


def _msg(name):
    return getattr(contracts, name)


def stream_post(*args, **kwargs):
    return http_backends.stream_post(*args, **kwargs)

INSTR = "Translate from English to Turkish. Output only the translation."
TEXT = "The weather is nice today."
CLOUD = "gpt-oss:120b-cloud"


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


def _loaded(spec):
    backend = create_backend(spec)
    backend.load()
    return backend, ChatTemplateEngine(spec)


def _req(text=TEXT, max_tokens=512):
    return GenerationRequest(INSTR, text, max_tokens)


def _point_sources(monkeypatch, iso, *, ollama=None, lmstudio=None, openai=()):
    cfg = contracts.SourcesConfig(
        ollama_url=ollama or iso.closed_url, lmstudio_url=lmstudio or iso.closed_url + "/v1",
        openai_servers=tuple(openai))
    monkeypatch.setattr(discovery, "load_sources", lambda path=None, *a, **k: cfg)
    return cfg


def _status(report, source):
    matches = [s for s in report.sources if s.source == source]
    assert len(matches) == 1, report.sources
    return matches[0]


@pytest.fixture
def no_sleep(monkeypatch):
    calls = []
    monkeypatch.setattr(backends, "_sleep", lambda s: calls.append(s))
    return calls


@pytest.fixture
def http_server():
    """Fabrika: ``http_server(Handler)`` -> taban adres (127.0.0.1, rastgele port)."""
    servers = []

    def _make(handler):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True).start()
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    yield _make
    for srv in servers:
        srv.shutdown()
        srv.server_close()


def _tags_with(srv, entries):
    """Sahte Ollama'nın /api/tags yanıtını verilen girdilerle değiştir (kalıcı)."""
    srv.set_status("/api/tags", 200, {"models": entries})


def _tags_entry(name, size=384, **extra):
    entry = {"name": name, "model": name, "modified_at": "2026-09-29T00:00:00Z",
             "size": size, "digest": "sha256:0",
             "details": {"format": "gguf", "family": "gptoss", "parameter_size": "116.8B",
                         "quantization_level": "MXFP4"}}
    entry.update(extra)
    return entry


# ===========================================================================
# F4-01 — Ollama bulut modeli yerel sayılmaz (KARARLAR 2)
# ===========================================================================
class TestF401BulutModeli:
    @pytest.mark.parametrize("name,meta,expected", [
        ("gpt-oss:120b-cloud", (), True),
        ("gemma4:cloud", (), True),
        ("gemma4:cloud:latest", (), True),
        ("qwen3:8b", (), False),
        ("wordcloud", (), False),
        ("benim-model", ({"remote_host": "https://ollama.com:443"},), True),
        ("benim-model", ({"remote_model": "gpt-oss:120b"},), True),
        ("benim-model", ({"remote_host": ""}, None), False),
    ])
    def test_tanima(self, name, meta, expected):
        assert http_util.ollama_cloud_model(name, *meta) is expected

    def test_uzak_adres_sunucu_adi(self):
        assert http_util.ollama_remote_host({"remote_host": "https://ollama.com:443"}) == \
            "ollama.com"
        assert http_util.ollama_remote_host(None, {}) == ""

    def _cloud_server(self, fake_llm_server):
        srv = fake_llm_server("ollama", models=[FakeModel(CLOUD, size_bytes=384),
                                                FakeModel("qwen3:8b")])
        _tags_with(srv, [_tags_entry(CLOUD, remote_model="gpt-oss:120b",
                                     remote_host="https://ollama.com:443"),
                         _tags_entry("qwen3:8b", size=5_234_000_000)])
        return srv

    def test_kesif_bulut_modelini_kullanilamaz_yapar(self, fake_llm_server, monkeypatch,
                                                     mt_isolation):
        srv = self._cloud_server(fake_llm_server)
        _point_sources(monkeypatch, mt_isolation, ollama=srv.url)
        report = discovery.discover_all()
        cloud = report.model_by_id(f"ollama:{CLOUD}")
        assert cloud is not None and cloud.usable is False
        assert cloud.reason == _msg("MSG_OLLAMA_CLOUD_MODEL").format(model=CLOUD, host="ollama.com")
        assert "bulut modeli, bu bilgisayarda çalışmaz" in cloud.reason
        # 384 baytlık "ağırlıkla" bellek hükmü verilmez ("sığar" denmez)
        assert getattr(cloud, "memory", None) is None
        local = report.model_by_id("ollama:qwen3:8b")
        assert local.usable and local.reason == ""
        assert srv.requests_to("/api/chat") == [] and srv.requests_to("/api/generate") == []

    def test_check_engine_ayni_hukum(self, fake_llm_server, monkeypatch, mt_isolation):
        srv = self._cloud_server(fake_llm_server)
        _point_sources(monkeypatch, mt_isolation, ollama=srv.url)
        check = discovery.check_engine(f"ollama:{CLOUD}")
        assert check.ok is False
        assert check.reason.startswith(f"“{CLOUD}” bir Ollama bulut modeli")
        assert discovery.check_engine("ollama:qwen3:8b").ok

    def test_load_reddeder_kitap_metni_gitmez(self, fake_llm_server):
        srv = self._cloud_server(fake_llm_server)
        backend = create_backend(_ollama_spec(srv.url, model=CLOUD))
        with pytest.raises(BackendUnavailable, match="bulut modeli"):
            backend.load()
        with pytest.raises(BackendUnavailable):       # devre açık: ağa gidilmez
            backend.generate(ChatTemplateEngine(backend.spec), _req("Secret chapter text."))
        assert srv.requests_to("/api/chat") == []
        assert srv.requests_to("/api/generate") == []   # ısınma bile yok

    def test_load_show_remote_host_adsiz_model(self, fake_llm_server):
        srv = fake_llm_server("ollama", models=["uzak-kopya"])
        srv.set_status("/api/show", 200, {"capabilities": ["completion"],
                                          "remote_model": "gpt-oss:120b",
                                          "remote_host": "https://ollama.com:443"})
        backend = create_backend(_ollama_spec(srv.url, model="uzak-kopya"))
        with pytest.raises(BackendUnavailable) as exc:
            backend.load()
        assert str(exc.value) == _msg("MSG_OLLAMA_CLOUD_MODEL").format(model="uzak-kopya",
                                                               host="ollama.com")
        assert srv.requests_to("/api/generate") == []

    def test_load_show_alinamazsa_tags_bakilir(self, fake_llm_server):
        srv = fake_llm_server("ollama", models=["uzak-kopya"])
        srv.set_status("/api/show", 500)
        _tags_with(srv, [_tags_entry("uzak-kopya", remote_host="https://ollama.com:443")])
        backend = create_backend(_ollama_spec(srv.url, model="uzak-kopya"))
        with pytest.raises(BackendUnavailable, match="bulut modeli"):
            backend.load()
        assert srv.requests_to("/api/generate") == []

    def test_yerel_model_yolu_degismez(self, fake_ollama):
        backend, eng = _loaded(_ollama_spec(fake_ollama.url))
        assert [p for _m, p, _b in fake_ollama.requests] == ["/api/show", "/api/generate"]
        assert backend.generate(eng, _req()).text == "ÇEVRİLDİ: " + TEXT


# ===========================================================================
# F4-03 — bağlam ön denetimi: sığacak paragraf gönderilir, kesik çeviri kabul edilmez
# ===========================================================================
_SENTENCE = ("The spacecraft follows a highly elliptical orbit whose perigee lies well inside "
             "the radiation belts, so the engineers had to shield every instrument carefully. ")
LONG = (_SENTENCE * 12).strip()        # ~1900 karakter (bugav r02 ile aynı ölçek)


class TestF403BaglamButcesi:
    def test_butce_kisilir_istem_sigmazsa_hata(self):
        limit, err = backends.context_budget(2100, 1540, 2048)
        assert err is None and limit == 2048 - backends.estimate_tokens(2100)
        limit, err = backends.context_budget(300, 512, 8192)
        assert (limit, err) == (512, None)                 # sığıyorsa aynen
        limit, err = backends.context_budget(9000, 512, 2048)
        assert err is not None and backends.is_context_error(err)
        assert backends.context_budget(9000, 512, None) == (512, None)

    def test_sigacak_paragraf_gonderilir(self, fake_llm_server):
        srv = fake_llm_server("openai", models=["qwen"], reply=lambda t: "Uzay aracı çevirisi.")
        backend, eng = _loaded(_openai_spec(srv.base_url, options={"num_ctx": 2048}))
        requested = backends.output_token_limit(len(LONG))
        prompt_chars = sum(len(m["content"]) for m in eng.build_chat_messages(INSTR, LONG))
        # eski katı denetim bu isteği hiç göndermezdi (bugav r02: 2077 > 2048)
        assert backends.context_error(prompt_chars, requested, 2048) is not None
        res = backend.generate(eng, _req(LONG, max_tokens=requested))
        assert res.error is None and res.text == "Uzay aracı çevirisi."
        sent = [b for b in srv.requests_to("/v1/chat/completions") if b["max_tokens"] > 1]
        assert len(sent) == 1
        assert sent[0]["max_tokens"] == 2048 - backends.estimate_tokens(prompt_chars)

    def test_kisilan_sinira_dayanan_cikti_kesik_kabul_edilmez(self, fake_llm_server):
        srv = fake_llm_server("openai", models=["qwen"],
                              reply=lambda t: FakeReply(content="Yarım kalan çevi",
                                                        finish_reason="length"))
        backend, eng = _loaded(_openai_spec(srv.base_url, options={"num_ctx": 2048}))
        res = backend.generate(eng, _req(LONG, max_tokens=backends.output_token_limit(len(LONG))))
        assert res.text == "" and backends.is_context_error(res.error)
        assert res.error.startswith(MSG_CONTEXT_EXCEEDED.split("(")[0])

    def test_kisilmayan_sinirda_davranis_ayni(self, fake_llm_server):
        # Bağlam bilinmiyor: sınır kısılmaz, "length" sonucu çevirmene aynen gider
        srv = fake_llm_server("openai", models=["qwen"],
                              reply=lambda t: FakeReply(content="Uzun çıktı",
                                                        finish_reason="length"))
        backend, eng = _loaded(_openai_spec(srv.base_url))
        res = backend.generate(eng, _req(LONG, max_tokens=1400))
        assert res.error is None and res.finish_reason == "length"
        assert srv.requests_to("/v1/chat/completions")[-1]["max_tokens"] == 1400

    def test_istem_baglami_asarsa_istek_gitmez(self, fake_llm_server):
        srv = fake_llm_server("openai", models=["qwen"])
        backend, eng = _loaded(_openai_spec(srv.base_url, options={"num_ctx": 256}))
        before = len(srv.requests_to("/v1/chat/completions"))
        res = backend.generate(eng, _req(LONG))
        assert backends.is_context_error(res.error)
        assert len(srv.requests_to("/v1/chat/completions")) == before

    def test_llamacpp_sinir_kisilir(self, fake_llama, tmp_path):
        from tests.fakes import write_fake_gguf
        path = tmp_path / "m-Q4_K_M.gguf"
        write_fake_gguf(path, "qwen3")
        spec = EngineSpec(name=f"gguf:{path.name}", format="chat", backend="llamacpp",
                          path=str(path), source="adhoc", options={"n_ctx": 2048})
        fake_llama.config.reply = lambda t: "Kısa çeviri."
        backend, eng = _loaded(spec)
        requested = backends.output_token_limit(len(LONG))
        res = backend.generate(eng, _req(LONG, max_tokens=requested))
        assert res.error is None and res.text == "Kısa çeviri."
        assert fake_llama.calls[-1][1]["max_tokens"] < requested


# ===========================================================================
# F4-04 — akış 'done' işaretsiz bitince ham OSError sızmaz
# ===========================================================================
_WHOLE = {"choices": [{"index": 0, "finish_reason": "stop", "message": {
    "role": "assistant", "content": "Yörünge çok basık bir elips."}}],
    "usage": {"completion_tokens": 7}}
_CUT_SSE = (b'data: {"choices":[{"index":0,"delta":{"content":"Y\\u00f6r\\u00fcnge "}}]}\n\n'
            b'data: {"choices":[{"index":0,"delta":{"content":"bas\\u0131k."}}]}\n\n')


def _cl_handler(mode):
    """HTTP/1.0 + Content-Length'li sahte OpenAI uyumlu sunucu (bugav r04 düzeni)."""

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, payload, ctype="application/json"):
            body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._send({"object": "list", "data": [{"id": "m", "object": "model"}]})

        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if not req.get("stream") or mode == "json":
                self._send(_WHOLE)
            else:
                self._send(_CUT_SSE, "text/event-stream")

    return H


class TestF404AkisBitisi:
    def test_stream_post_content_length_osError_sizmaz(self, http_server):
        base = http_server(_cl_handler("kesik"))
        lines = []
        stream_post(base + "/v1/chat/completions", {"stream": True}, 5.0,
                    lambda line: lines.append(line) or False)
        assert len(lines) == 2 and lines[0].startswith(b"data:")

    @pytest.mark.parametrize("mode,text,error_part", [
        ("kesik", "", "akış tamamlanmadan kapandı"),
        ("json", "Yörünge çok basık bir elips.", None),
    ])
    def test_arka_uc_oge_sonucu(self, http_server, mode, text, error_part):
        base = http_server(_cl_handler(mode))
        backend, eng = _loaded(_openai_spec(base + "/v1", model="m"))
        backend.reasoning = True                    # düşünen model: akışla okunur
        assert backend.use_stream()
        res = backend.generate(eng, _req())         # istisna fırlamaz (EBADF yok)
        assert res.text == text
        if error_part:
            assert error_part in res.error
        else:
            assert res.error is None
        assert backend.health() is None


# ===========================================================================
# F4-05 — parçaya özgü kalıcı 5xx öğe hatasıdır; "bağlantı koptu" yalnız devre açılınca
# ===========================================================================
def _poison_reply(text):
    if "POISON" in text:
        return FakeReply(status=500, error="llama runner process has terminated: exit status 2")
    return "Tamam: " + text


class TestF405SunucuHatasi:
    def test_kalici_500_oge_hatasi_devre_kapali(self, fake_llm_server, no_sleep):
        srv = fake_llm_server("ollama", reply=_poison_reply)
        backend, eng = _loaded(_ollama_spec(srv.url))
        res = backend.generate(eng, _req("Paragraph POISON here."))
        assert res.text == "" and "HTTP 500" in res.error
        assert "koptu" not in res.error and "denenmeden" not in res.error
        assert backend.health() is None
        assert backend.generate(eng, _req("Paragraph fine.")).text == "Tamam: Paragraph fine."

    def test_bitisik_uc_zehirli_parca_kosuyu_durdurmaz(self, fake_llm_server, no_sleep):
        srv = fake_llm_server("ollama", reply=_poison_reply)
        backend, eng = _loaded(_ollama_spec(srv.url))
        texts = [f"Paragraph {i}{' POISON' if i in (4, 5, 6) else ''}." for i in range(1, 11)]
        out = backend.generate_batch(eng, [_req(t) for t in texts])   # fırlatmaz
        failed = [i + 1 for i, r in enumerate(out) if r.error]
        assert failed == [4, 5, 6]
        assert [r.text for i, r in enumerate(out) if i + 1 in (7, 8, 9, 10)] == \
            [f"Tamam: Paragraph {i}." for i in (7, 8, 9, 10)]
        assert backend.health() is None

    def test_art_arda_esik_devreyi_acar(self, fake_llm_server, no_sleep, monkeypatch):
        monkeypatch.setattr(backends, "MT_HTTP_SERVER_ERROR_MAX_ITEMS", 3)
        srv = fake_llm_server("ollama", reply=_poison_reply)
        backend, eng = _loaded(_ollama_spec(srv.url))
        for _ in range(2):
            assert backend.generate(eng, _req("POISON text.")).error
        with pytest.raises(BackendUnavailable) as exc:
            backend.generate(eng, _req("POISON text."))
        assert str(exc.value).startswith("Ollama art arda 3 parçada hata döndürdü (HTTP 500)")
        assert backend.health() == str(exc.value)
        before = len(srv.requests)
        with pytest.raises(BackendUnavailable):
            backend.generate(eng, _req("Paragraph fine."))
        assert len(srv.requests) == before

    def test_basari_oge_sayacini_sifirlar(self, fake_llm_server, no_sleep, monkeypatch):
        monkeypatch.setattr(backends, "MT_HTTP_SERVER_ERROR_MAX_ITEMS", 2)
        srv = fake_llm_server("ollama", reply=_poison_reply)
        backend, eng = _loaded(_ollama_spec(srv.url))
        for text in ("POISON a.", "Fine b.", "POISON c.", "Fine d.", "POISON e."):
            backend.generate(eng, _req(text))
        assert backend.health() is None

    def test_baglanti_kopmasi_iletisi_devre_acilinca(self, fake_ollama, no_sleep):
        backend, eng = _loaded(_ollama_spec(fake_ollama.url))
        fake_ollama.stop()
        messages = []
        for _ in range(MT_HTTP_CIRCUIT_MAX_FAILURES):
            with pytest.raises(BackendUnavailable) as exc:
                backend.generate(eng, _req())
            messages.append(str(exc.value))
        assert all("denenmeden" not in m for m in messages[:-1])
        assert all(f"{DEFAULT_RETRY_COUNT + 1} denemede yanıt vermedi" in m
                   for m in messages[:-1])
        assert messages[-1] == MSG_SERVER_LOST.format(url=backend.base_url)

    def test_503_sunucu_duzeyinde_kalir(self, fake_ollama, no_sleep):
        backend, eng = _loaded(_ollama_spec(fake_ollama.url))
        fake_ollama.set_status("/api/chat", 503)
        with pytest.raises(BackendUnavailable):
            backend.generate(eng, _req())

    def test_uctan_uca_zehirli_paragraflar(self, fake_llm_server, monkeypatch, no_sleep):
        from latex_pipeline.config import PipelineConfig
        from latex_pipeline.translator import LatexTranslator

        def reply(text):
            if "POISON" in text:
                return FakeReply(status=500, error="llama runner process has terminated")
            return " ".join("sözcük" if w.isalpha() else w for w in text.split())

        srv = fake_llm_server("ollama", models=["sahte"], reply=reply)
        monkeypatch.setattr(discovery, "load_sources", lambda path=None: contracts.SourcesConfig(
            ollama_url=srv.url, lmstudio_url="http://127.0.0.1:9/v1", openai_servers=()))
        paras = [f"Paragraph {i} describes the orbit of a small moon"
                 f"{' POISON' if i in (4, 5, 6) else ''}." for i in range(1, 11)]
        body = "\\section{Orbit}\n\n" + "\n\n".join(paras) + "\n"
        lt = LatexTranslator(pipeline_config=PipelineConfig(translate=True, segment_timeout=30),
                             mt_engine="ollama:sahte", source_lang="en", target_lang="tr",
                             translation_overrides=dict(use_memory=False, glossary_mode="off",
                                                        run_qa=False, check_consistency=False))
        out = lt.translate(body)
        kept = [i for i in range(1, 11) if f"Paragraph {i} " in out]
        assert kept == [4, 5, 6]            # sağlam 7-10 çevrildi (eskiden 4..10 özgün)
        assert lt.backend_health() is None


# ===========================================================================
# F4-06 — Ollama keep_alive sayı olarak gider, 0 korunur; num_ctx tabanı sessiz değil
# ===========================================================================
class TestF406OllamaSecenekleri:
    @pytest.mark.parametrize("value,expected", [
        (3600, 3600), (0, 0), (-1, -1), ("10m", "10m"), ("3600", 3600), ("-1", -1),
        (None, OLLAMA_KEEP_ALIVE), ("", OLLAMA_KEEP_ALIVE), (True, OLLAMA_KEEP_ALIVE),
    ])
    def test_donusum(self, value, expected):
        got = http_backends.ollama_keep_alive(value)
        assert got == expected and type(got) is type(expected)

    @pytest.mark.parametrize("value", [0, 3600, -1])
    def test_istek_govdesi_sayi(self, fake_ollama, value):
        backend, eng = _loaded(_ollama_spec(fake_ollama.url, options={"keep_alive": value}))
        backend.generate(eng, _req())
        sent = [b["keep_alive"] for b in fake_ollama.requests_to("/api/generate")
                + fake_ollama.requests_to("/api/chat")]
        assert sent and all(v == value and isinstance(v, int) for v in sent)

    def test_num_ctx_tabani_uyari_verir(self, fake_ollama, caplog):
        with caplog.at_level(logging.WARNING, logger="core.mt_engines.http_backends"):
            backend = create_backend(_ollama_spec(fake_ollama.url, options={"num_ctx": 4096}))
        assert backend.num_ctx == MT_CONTEXT_TOKENS
        assert any("num_ctx=4096" in r.getMessage() for r in caplog.records)


# ===========================================================================
# F4-07 — toplu üretim yarıda kalınca başarılı sonuçlar atılmaz
# ===========================================================================
class TestF407KismiSonuc:
    def test_sirali_kismi_sonuclar_istisnada(self, fake_llm_server, no_sleep):
        state = {"n": 0, "srv": None}

        def reply(text):
            state["n"] += 1
            if state["n"] == 3:
                state["srv"].stop()          # 3. yanıt gider, sonrakiler reddedilir
            return "Çeviri: " + text

        srv = fake_llm_server("ollama", reply=reply)
        state["srv"] = srv
        backend, eng = _loaded(_ollama_spec(srv.url))
        texts = [f"Paragraph {i} here." for i in range(6)]
        with pytest.raises(BackendUnavailable) as exc:
            backend.generate_batch(eng, [_req(t) for t in texts])
        partial = exc.value.partial_results
        assert len(partial) == len(texts)
        assert [r.text for r in partial[:3]] == [f"Çeviri: {t}" for t in texts[:3]]
        assert all(r.error is None for r in partial[:3])
        assert list(partial[3:]) == [None, None, None]

    def test_paralel_kismi_sonuclar(self, fake_ollama, monkeypatch):
        backend, eng = _loaded(_ollama_spec(fake_ollama.url, options={"max_parallel": 2}))
        real = backend.generate

        def flaky(engine, req):
            if "KOPUK" in req.text:
                raise BackendUnavailable("kopuk")
            return real(engine, req)

        monkeypatch.setattr(backend, "generate", flaky)
        texts = ["One here.", "Two here.", "KOPUK three.", "Four here."]
        with pytest.raises(BackendUnavailable) as exc:
            backend.generate_batch(eng, [_req(t) for t in texts])
        partial = exc.value.partial_results
        assert len(partial) == 4 and partial[2] is None
        done = [(i, r) for i, r in enumerate(partial) if r is not None]
        assert done and all(r.text == "ÇEVRİLDİ: " + texts[i] for i, r in done)

    def test_tekil_cagrida_bos(self):
        assert BackendUnavailable("x").partial_results == ()


# ===========================================================================
# F4-08 / F5-14 — MLX sayımı yerleşik motorun klasörünü de sayar
# ===========================================================================
class TestF408MlxSayimi:
    def test_yerlesik_klasor_sayilir(self, mt_isolation):
        write_fake_mlx_dir(mt_isolation.models_dir / "hymt-7b-mlx", "hunyuan_v1_dense")
        report = discovery.discover_all()
        assert _status(report, contracts.SOURCE_MLX).message == \
            _msg("MSG_SOURCE_COUNT_DIRS_ENGINES").format(n=1, engines="hymt-7b")
        assert [m.engine_id for m in report.models if m.source == contracts.SOURCE_MLX] == []
        assert report.model_by_id("hymt-7b").location.endswith("hymt-7b-mlx")

    def test_yerlesik_artı_ek_klasor(self, mt_isolation):
        write_fake_mlx_dir(mt_isolation.models_dir / "hymt-7b-mlx", "hunyuan_v1_dense")
        write_fake_mlx_dir(mt_isolation.models_dir / "qwen3-mlx", "qwen3")
        report = discovery.discover_all()
        assert _status(report, contracts.SOURCE_MLX).message == "2 klasör (hymt-7b dahil)"

    def test_kayitli_motor_yoksa_eski_metin(self, mt_isolation):
        write_fake_mlx_dir(mt_isolation.models_dir / "qwen3-mlx", "qwen3")
        assert _status(discovery.discover_all(), contracts.SOURCE_MLX).message == "1 klasör"

    def test_cli_tablosu(self, mt_isolation, capsys):
        write_fake_mlx_dir(mt_isolation.models_dir / "hymt-7b-mlx", "hunyuan_v1_dense")
        assert discovery.main([]) == 0
        assert "MLX klasörleri: 1 klasör (hymt-7b dahil)" in capsys.readouterr().out


# ===========================================================================
# F4-10 — kesilen tarama görünür
# ===========================================================================
class TestF410TaramaSiniri:
    def test_kesilen_tarama_durum_satirinda(self, mt_isolation, monkeypatch):
        monkeypatch.setattr(discovery, "DISCOVERY_MAX_ENTRIES", 5)
        crowded = mt_isolation.models_dir / "a_cok_dosya"
        crowded.mkdir()
        for i in range(10):
            (crowded / f"sayfa_{i:02d}.png").write_bytes(b"")
        write_fake_mlx_dir(mt_isolation.models_dir / "z_model-mlx", "qwen3")
        report = discovery.discover_all()
        note = _msg("MSG_SOURCE_COUNT_TRUNCATED").format(limit=5)
        for source in (contracts.SOURCE_MLX, contracts.SOURCE_GGUF):
            assert _status(report, source).message.endswith(" · " + note)

    def test_kesilmeyen_taramada_not_yok(self, mt_isolation):
        write_fake_mlx_dir(mt_isolation.models_dir / "qwen3-mlx", "qwen3")
        report = discovery.discover_all()
        assert "kesildi" not in _status(report, contracts.SOURCE_MLX).message


# ===========================================================================
# F4-11 — akışta toplam süre satır içinde de işler
# ===========================================================================
class _DripHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(b"data: ")
            self.wfile.flush()
            for _ in range(40):              # satır sonu olmadan 4 sn damla
                time.sleep(0.1)
                self.wfile.write(b"x")
                self.wfile.flush()
            self.wfile.write(b"\n")
            self.wfile.flush()
        except OSError:
            pass


class TestF411AkisSuresi:
    def test_damla_akista_toplam_sure(self, http_server):
        base = http_server(_DripHandler)
        t0 = time.monotonic()
        with pytest.raises(http_util.HttpTimeout):
            stream_post(base + "/v1/chat/completions", {"stream": True}, 0.6, lambda l: True)
        assert time.monotonic() - t0 < 2.0


# ===========================================================================
# F4-13 — bellek satırı kendisiyle çelişmez
# ===========================================================================
def _fit(required, available, fits=True):
    return hardware.MemoryFit(weights_bytes=required, kv_bytes_per_token=1, kv_source="oran",
                              mode=hardware.MODE_CONTEXT, kv_tokens=1, workspace_bytes=0,
                              required_bytes=required, minimum_bytes=required,
                              available_bytes=available, fits=fits, batch_size=1,
                              hardware_source=hardware.SOURCE_METAL)


class TestF413Yuvarlama:
    def test_bugav_ornegi(self):
        line = discovery._memory_line(_fit(30_120_672_383, 30_150_672_384))
        assert line == "bellek: gerekli ~30,1 GB · kullanılabilir 30,1 GB · sığar"

    def test_gosterilen_gereken_eldekini_asmaz(self):
        for req, avail in ((30_120_672_383, 30_150_672_384), (5_000_000_001, 5_050_000_000),
                           (7_999_999_999, 8_000_000_000)):
            need = float(hardware.format_fit_gb(req, avail).replace(",", "."))
            have = float(hardware.format_gb(avail, round_up=False).replace(",", "."))
            assert need <= have

    def test_rahat_sigan_ve_sigmayan_degismez(self):
        assert hardware.format_fit_gb(12_340_000_000, 30_000_000_000) == "12,4"
        assert hardware.format_fit_gb(31_000_000_000, 30_000_000_000) == "31,0"
        assert hardware.format_fit_gb(12_340_000_000, None) == "12,4"

    def test_toplu_is_notu_tutarli(self):
        fit = hardware.MemoryFit(weights_bytes=1, kv_bytes_per_token=1, kv_source="oran",
                                 mode=hardware.MODE_BATCHED, kv_tokens=1, workspace_bytes=0,
                                 required_bytes=30_120_672_383, minimum_bytes=20_000_000_000,
                                 available_bytes=30_150_672_384, fits=True, batch_size=5,
                                 hardware_source=hardware.SOURCE_METAL)
        note = hardware.batch_note(fit)
        assert "~30,1 GB, kullanılabilir 30,1 GB" in note


# ===========================================================================
# F4-14 — liste config.json doğru neden; BOM'lu ayar dosyaları okunur
# ===========================================================================
class TestF414AyarDosyalari:
    def test_liste_config_eksik_config_nedeni(self, tmp_path):
        d = tmp_path / "liste-mlx"
        d.mkdir()
        (d / "config.json").write_text("[1, 2]", encoding="utf-8")
        (d / "model.safetensors").write_bytes(b"\0" * 16)
        (d / "tokenizer.json").write_text("{}", encoding="utf-8")
        info = discovery.inspect_mlx_dir(d)
        assert info.usable is False
        assert "okunabilir config.json" in info.reason
        assert "mimariyi tanımıyor" not in info.reason

    def test_bom_mt_engines(self, tmp_path):
        p = tmp_path / "mt_engines.json"
        p.write_bytes(codecs.BOM_UTF8 + json.dumps(
            {"benim-model": {"backend": "ollama", "model": "qwen2.5:7b"}}).encode("utf-8"))
        assert list(registry.load_user_specs(p)) == ["benim-model"]

    def test_utf8_olmayan_mt_engines_fail_open(self, tmp_path):
        p = tmp_path / "mt_engines.json"
        p.write_bytes('{"motor": {"description": "ç"}}'.encode("latin-1"))
        assert registry.load_user_specs(p) == {}

    def test_bom_mt_sources(self, tmp_path, mt_isolation):
        p = tmp_path / "mt_sources.json"
        p.write_bytes(codecs.BOM_UTF8 + json.dumps(
            {"ollama": {"url": "http://127.0.0.1:12345"}}).encode("utf-8"))
        assert discovery.load_sources(p).ollama_url == "http://127.0.0.1:12345"


# ===========================================================================
# F5-10 — açık ama yavaş / hatalı sunucu "kapalı" gösterilmez
# ===========================================================================
class TestF510YavasSunucu:
    def test_yavas_ollama_kapali_denmez(self, fake_llm_server, monkeypatch, mt_isolation):
        srv = fake_llm_server("ollama")
        srv.set_delay("/api/tags", 1.0)
        _point_sources(monkeypatch, mt_isolation, ollama=srv.url)
        status = _status(discovery.discover_all(http_timeout=0.2), contracts.SOURCE_OLLAMA)
        assert status.message == _msg("MSG_SERVER_NO_LIST").format(
            server="Ollama", url=srv.url, why="0,2 sn içinde yanıt yok")
        assert "ollama serve" not in status.message and "kapalı" not in status.message
        assert status.reachable is False

    def test_500_veren_ollama(self, fake_llm_server, monkeypatch, mt_isolation):
        srv = fake_llm_server("ollama")
        srv.set_status("/api/tags", 500)
        _point_sources(monkeypatch, mt_isolation, ollama=srv.url)
        status = _status(discovery.discover_all(), contracts.SOURCE_OLLAMA)
        assert status.message == _msg("MSG_SERVER_NO_LIST").format(server="Ollama", url=srv.url,
                                                           why="HTTP 500")
        assert status.reachable is True

    def test_kapali_ollama_eski_ileti(self, monkeypatch, mt_isolation):
        monkeypatch.setattr(discovery, "_ollama_installed", lambda: True)
        status = _status(discovery.discover_all(), contracts.SOURCE_OLLAMA)
        assert status.message == contracts.MSG_OLLAMA_INSTALLED_DOWN.format(
            url=mt_isolation.closed_url)

    def test_check_engine_yavas(self, fake_llm_server, monkeypatch, mt_isolation):
        srv = fake_llm_server("ollama")
        srv.set_delay("/api/tags", 1.0)
        _point_sources(monkeypatch, mt_isolation, ollama=srv.url)
        check = discovery.check_engine("ollama:qwen3:8b", http_timeout=0.2)
        assert check.ok is False
        assert check.reason.startswith("Ollama sunucusu açık ama model listesini vermedi")

    def test_yavas_lmstudio(self, fake_llm_server, monkeypatch, mt_isolation):
        srv = fake_llm_server("lmstudio")
        srv.set_delay("/v1/models", 1.0)
        _point_sources(monkeypatch, mt_isolation, lmstudio=srv.base_url)
        status = _status(discovery.discover_all(http_timeout=0.2), contracts.SOURCE_LMSTUDIO)
        assert status.message.startswith("LM Studio sunucusu açık ama model listesini vermedi")
        assert "lms server start" not in status.message

    def test_yavas_openai_sunucusu_openai_degil_denmez(self, fake_llm_server, monkeypatch,
                                                      mt_isolation):
        srv = fake_llm_server("openai")
        srv.set_delay("/v1/models", 1.0)
        server = contracts.OpenAIServer(name="llama.cpp", url=srv.base_url)
        _point_sources(monkeypatch, mt_isolation, openai=[server])
        status = _status(discovery.discover_all(http_timeout=0.2), contracts.SOURCE_OPENAI)
        assert status.message == contracts.MSG_OPENAI_DOWN.format(url=srv.base_url)
        assert status.reachable is False


# ===========================================================================
# İleti kataloğu: yeni iletiler Türkçe, OOM alt dizgisiz
# ===========================================================================
@pytest.mark.parametrize("name", ["MSG_OLLAMA_CLOUD_MODEL", "MSG_SERVER_NO_LIST",
                                  "MSG_SERVER_RETRY_FAILED", "MSG_SERVER_FAILING",
                                  "MSG_SOURCE_COUNT_DIRS_ENGINES",
                                  "MSG_SOURCE_COUNT_FILES_ENGINES",
                                  "MSG_SOURCE_COUNT_TRUNCATED"])
def test_yeni_iletiler_katalogda(name):
    assert name in contracts.MESSAGE_CATALOG
    import string
    template = contracts.MESSAGE_CATALOG[name]
    fields = [f[1] for f in string.Formatter().parse(template) if f[1]]
    text = template.format(**{f: "x" for f in fields})
    assert not contracts.has_oom_trigger(text)
    assert "Yol A" not in text and "Yol B" not in text
