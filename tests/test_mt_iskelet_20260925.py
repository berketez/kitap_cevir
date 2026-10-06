"""K0 iskelet ve sözleşme testleri (2026-09-25).

Kapsam: config/defaults.py yeni sabitleri, core/mt_engines/contracts.py
(veri yapıları, ileti kataloğu, yardımcılar), core/mt_engines/http_util.py
(loopback, proxy, hata eşlemesi), EngineSpec yeni alanları, sahte sunucunun
HER rotası, sahte llama_cpp, sahte model dosyaları, autouse yalıtım.

Gerçek model yüklenmez; ağ yalnız sahte sunuculara (127.0.0.1:<rastgele>).
"""

from __future__ import annotations

import importlib.machinery
import json
import socket
import string
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import FrozenInstanceError, fields
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import defaults  # noqa: E402
from core.mt_engines import contracts as C  # noqa: E402
from core.mt_engines import http_util as H  # noqa: E402
from core.mt_engines.base import EngineSpec  # noqa: E402
from tests.fakes import (  # noqa: E402
    REAL_LOCAL_MODEL_PORTS,
    FakeLLMServer,
    FakeModel,
    FakeReply,
    apply_mt_isolation,
    make_fake_llama_module,
    read_gguf_kv,
    write_corrupt_gguf,
    write_fake_gguf,
    write_fake_gguf_split,
    write_fake_mlx_dir,
)

HEAVY_MODULES = ("mlx", "mlx_lm", "torch", "transformers", "requests", "httpx",
                 "openai", "llama_cpp")
T = 2.0   # sahte sunucu istekleri için zaman aşımı (sn)


# ===========================================================================
# 1. Hafif içe aktarma (kabul ölçütü)
# ===========================================================================
class TestHafifIceAktarma:
    @pytest.mark.parametrize("stmt", [
        "from core.mt_engines.contracts import *",
        "import core.mt_engines.http_util",
        "import core.mt_engines",
    ])
    def test_agir_modul_yuklenmez(self, stmt):
        code = (f"import sys; {stmt}; "
                f"print(','.join(m for m in {HEAVY_MODULES!r} if m in sys.modules))")
        out = subprocess.run([sys.executable, "-c", code], cwd=PROJECT_ROOT,
                             capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "", f"ağır modül içe aktarıldı: {out.stdout!r}"

    def test_all_adlari_var_ve_paketten_disa_aktarilir(self):
        import core.mt_engines as pkg
        for name in C.__all__:
            assert hasattr(C, name), name
            assert getattr(pkg, name) is getattr(C, name), name
        assert pkg.BackendUnavailable is C.BackendUnavailable


# ===========================================================================
# 2. config/defaults.py
# ===========================================================================
class TestDefaults:
    NEW = ("OLLAMA_DEFAULT_URL", "LMSTUDIO_DEFAULT_URL", "DISCOVERY_HTTP_TIMEOUT_S",
           "ENGINE_CHECK_HTTP_TIMEOUT_S", "MT_HTTP_TIMEOUT_S", "MT_MODEL_LOAD_TIMEOUT_S",
           "MT_HTTP_MAX_PARALLEL", "MT_HTTP_CIRCUIT_MAX_FAILURES", "MT_CONTEXT_TOKENS",
           "OLLAMA_KEEP_ALIVE", "DISCOVERY_MAX_DEPTH", "DISCOVERY_MAX_ENTRIES")

    def test_tasarim_sabitleri_ve_all(self):
        for name in self.NEW:
            assert hasattr(defaults, name), name
            assert name in defaults.__all__, name
        for name in defaults.__all__:
            assert hasattr(defaults, name), name

    def test_degerler_tasarimla_ayni(self):
        assert defaults.OLLAMA_DEFAULT_URL == "http://127.0.0.1:11434"
        assert defaults.LMSTUDIO_DEFAULT_URL == "http://127.0.0.1:1234/v1"
        assert defaults.DISCOVERY_HTTP_TIMEOUT_S == 1.5
        assert defaults.ENGINE_CHECK_HTTP_TIMEOUT_S == 3.0
        assert defaults.MT_HTTP_TIMEOUT_S == 240
        assert defaults.MT_MODEL_LOAD_TIMEOUT_S == 600
        assert defaults.MT_HTTP_MAX_PARALLEL == 1          # KARARLAR 7: sıralı
        assert defaults.MT_CONTEXT_TOKENS == 8192
        assert defaults.OLLAMA_KEEP_ALIVE == "30m"
        assert defaults.DISCOVERY_MAX_DEPTH == 3
        assert defaults.DISCOVERY_MAX_ENTRIES == 5000

    def test_sabit_iliskileri(self):
        assert defaults.MT_HTTP_TIMEOUT_S < defaults.SEGMENT_TIMEOUT_S
        assert defaults.MT_HTTP_CIRCUIT_MAX_FAILURES == defaults.DEFAULT_RETRY_COUNT
        assert defaults.DISCOVERY_HTTP_TIMEOUT_S < defaults.ENGINE_CHECK_HTTP_TIMEOUT_S
        assert defaults.MT_HTTP_MAX_PARALLEL <= defaults.MT_HTTP_MAX_PARALLEL_LIMIT

    def test_varsayilan_adresler_loopback(self):
        assert H.check_local_url(defaults.OLLAMA_DEFAULT_URL) == defaults.OLLAMA_DEFAULT_URL
        assert H.check_local_url(defaults.LMSTUDIO_DEFAULT_URL) == defaults.LMSTUDIO_DEFAULT_URL


# ===========================================================================
# 3. contracts.py
# ===========================================================================
_ASCII_KACIS = ("Gecersiz", "gecersiz", "bulunamadi", "degil", "kapali", "acin",
                "calistir", "yuklu", "sunucusuna baglan", "Kullanilabilir", "Turkce")


def _fill(template: str) -> str:
    names = [f[1] for f in string.Formatter().parse(template) if f[1]]
    return template.format(**{n: "x" for n in names})


class TestIletiKatalogu:
    def test_katalog_tum_msg_sabitlerini_icerir(self):
        msgs = {k for k in dir(C) if k.startswith("MSG_")}
        assert msgs == set(C.MESSAGE_CATALOG)
        for name in ("MSG_UNKNOWN_ENGINE", "MSG_BAD_ID", "MSG_REMOTE_REFUSED",
                     "MSG_PATH_NOT_ALLOWED", "MSG_BUILTIN_MISSING", "MSG_MLX_MISSING",
                     "MSG_MLX_INCOMPLETE", "MSG_MLX_ARCH_UNSUPPORTED", "MSG_GGUF_MISSING",
                     "MSG_GGUF_INVALID", "MSG_LLAMA_MISSING", "MSG_OLLAMA_DOWN",
                     "MSG_LMSTUDIO_DOWN", "MSG_OPENAI_DOWN", "MSG_NOT_OPENAI",
                     "MSG_MODEL_NOT_ON_SERVER", "MSG_LMSTUDIO_NOT_LOADED",
                     "MSG_EMBEDDING_MODEL", "MSG_SERVER_LOST", "MSG_OUT_OF_MEMORY_TR",
                     "MSG_CONTEXT_EXCEEDED", "MSG_EMPTY_OUTPUT", "MSG_TIMEOUT"):
            assert name in C.MESSAGE_CATALOG, name

    @pytest.mark.parametrize("name", sorted(C.MESSAGE_CATALOG))
    def test_kalip_doldurulur_oom_icermez_ascii_kacisi_yok(self, name):
        text = _fill(C.MESSAGE_CATALOG[name])
        assert not C.has_oom_trigger(text), f"{name}: OOM alt dizgisi"
        for bad in _ASCII_KACIS:
            assert bad not in text, f"{name}: ASCII'ye kaçış {bad!r}"

    def test_alan_adlari(self):
        def fieldset(t):
            return {f[1] for f in string.Formatter().parse(t) if f[1]}
        assert fieldset(C.MSG_UNKNOWN_ENGINE) == {"engine_id", "names"}
        assert fieldset(C.MSG_BAD_ID) == {"engine_id", "why"}
        assert fieldset(C.MSG_REMOTE_REFUSED) == {"url"}
        assert fieldset(C.MSG_PATH_NOT_ALLOWED) == {"path", "roots"}
        assert fieldset(C.MSG_MLX_INCOMPLETE) == {"missing", "path"}
        assert fieldset(C.MSG_MLX_ARCH_UNSUPPORTED) == {"arch"}
        assert fieldset(C.MSG_GGUF_INVALID) == {"why", "path"}
        assert fieldset(C.MSG_GGUF_LOAD_FAILED) == {"why", "path"}
        assert fieldset(C.MSG_MODEL_NOT_ON_SERVER) == {"model", "server"}
        assert fieldset(C.MSG_LMSTUDIO_NOT_LOADED) == {"model"}
        assert fieldset(C.MSG_OUT_OF_MEMORY_TR) == {"server", "detail"}
        assert fieldset(C.MSG_CONTEXT_EXCEEDED) == {"need", "ctx"}
        assert fieldset(C.MSG_TIMEOUT) == {"seconds"}
        assert fieldset(C.MSG_LLAMA_MISSING) == set()
        assert fieldset(C.MSG_EMPTY_OUTPUT) == set()

    def test_llama_kurulum_komutu_birebir(self):
        assert C.LLAMA_CPP_INSTALL_CMD == \
            'CMAKE_ARGS="-DGGML_METAL=on" ./venv/bin/pip install llama-cpp-python'
        assert C.MSG_LLAMA_MISSING.endswith(C.LLAMA_CPP_INSTALL_CMD)

    def test_mevcut_test_sozlesmeleri(self):
        msg = C.MSG_UNKNOWN_ENGINE.format(engine_id="yok-model", names="hymt-7b")
        assert "Desteklenmeyen MT motoru" in msg and "'yok-model'" in msg and "hymt-7b" in msg
        assert "docs/YENI_MODEL_EKLEME.md" in msg
        for prefix in C.ID_PREFIXES:
            assert f"{prefix}:" in msg

    def test_yerellik_iletisi(self):
        msg = C.MSG_REMOTE_REFUSED.format(url="http://10.0.0.5:11434")
        assert "Yalnız bu bilgisayardaki" in msg
        # 5 Ekim 2026: iç belgeye (KURALLAR.md, açık yayına girmez) gönderme yok.
        assert "KURALLAR.md" not in msg


class TestSabitlerVeYardimcilar:
    def test_arka_uc_bicim_onek(self):
        assert C.BACKENDS == ("mlx", "ollama", "openai", "llamacpp")
        assert C.FORMATS == ("hymt", "chat")
        assert C.ID_PREFIXES == ("mlx", "gguf", "ollama", "lmstudio", "openai")
        assert set(C.PREFIX_BACKEND) == set(C.ID_PREFIXES)
        assert set(C.PREFIX_BACKEND.values()) <= set(C.BACKENDS)
        assert set(C.BACKEND_LABELS) == set(C.BACKENDS)
        assert C.OPENAI_ID_SEP == "|"
        assert C.DISCOVERY_SOURCE_ORDER[:2] == (C.SOURCE_BUILTIN, C.SOURCE_USER)
        assert len(set(C.DISCOVERY_SOURCE_ORDER)) == 7

    def test_motor_adi_deseni(self):
        assert C.ENGINE_NAME_RE.match("hymt-7b")
        assert C.ENGINE_NAME_RE.match("benim_model.v2")
        assert not C.ENGINE_NAME_RE.match("ollama:qwen3")
        assert not C.ENGINE_NAME_RE.match("a" * 65)
        assert not C.ENGINE_NAME_RE.match("")

    @pytest.mark.parametrize("name,expected", [
        ("hymt-7b-mlx", "hymt"), ("HY-MT1.5-7B-Q8_0.gguf", "hymt"), ("hy_mt", "hymt"),
        ("qwen3:8b", "chat"), ("", "chat"), (None, "chat"),
    ])
    def test_guess_format(self, name, expected):
        assert C.guess_format(name) == expected

    def test_build_engine_id(self):
        assert C.build_engine_id("ollama", "qwen3:8b") == "ollama:qwen3:8b"
        assert C.build_engine_id("lmstudio", "qwen/qwen3-8b") == "lmstudio:qwen/qwen3-8b"
        assert C.build_engine_id("openai", "qwen", "http://127.0.0.1:8080/v1") == \
            "openai:http://127.0.0.1:8080/v1|qwen"
        with pytest.raises(ValueError, match="Geçersiz model kimliği"):
            C.build_engine_id("bilinmeyen", "x")
        with pytest.raises(ValueError, match="sunucu adresi"):
            C.build_engine_id("openai", "qwen")

    def test_oom_safe(self):
        raw = "CUDA error: out of memory; bloom-7b on MPS; OOM"
        assert C.has_oom_trigger(raw)
        safe = C.oom_safe(raw)
        assert not C.has_oom_trigger(safe)
        assert "bellek yetersiz" in safe
        assert safe.replace("​", "") == raw.replace("out of memory", "bellek yetersiz")
        assert C.oom_safe("") == "" and C.oom_safe(None) == ""
        assert not C.has_oom_trigger(C.oom_safe("ooom mpss cudaa zoom"))

    def test_hatalar(self):
        err = C.BackendUnavailable("Sunucu kapalı", hint="ollama serve")
        assert isinstance(err, RuntimeError) and err.hint == "ollama serve"
        assert str(err) == "Sunucu kapalı"
        assert C.BackendUnavailable("x").hint == ""
        assert issubclass(C.GenerationError, RuntimeError)
        assert not issubclass(C.GenerationError, C.BackendUnavailable)

    def test_veri_yapilari_donuk_ve_varsayilanlar(self):
        req = C.GenerationRequest("talimat", "metin", 512)
        with pytest.raises(FrozenInstanceError):
            req.text = "y"
        res = C.GenerationResult()
        assert (res.text, res.finish_reason, res.error) == ("", None, None)
        m = C.DiscoveredModel(engine_id="ollama:qwen3:8b", label="qwen3:8b",
                              source=C.SOURCE_OLLAMA, backend=C.BACKEND_OLLAMA,
                              format_guess=C.FORMAT_CHAT, usable=True)
        assert (m.reason, m.size_bytes, m.detail, m.location, m.reasoning, m.loaded) == \
            ("", None, "", "", None, None)
        rep = C.DiscoveryReport(models=(m,), sources=(), scanned_at=1.0)
        assert rep.model_by_id("ollama:qwen3:8b") is m
        assert rep.model_by_id("yok") is None
        chk = C.EngineCheck(engine_id="x", ok=False, label="x", reason="neden")
        assert chk.hint == ""
        st = C.SourceStatus(source=C.SOURCE_OLLAMA, label="Ollama", reachable=False,
                            message="kapalı")
        assert st.location == ""

    def test_sources_config(self):
        cfg = C.SourcesConfig()
        assert cfg.ollama_url == defaults.OLLAMA_DEFAULT_URL
        assert cfg.lmstudio_url == defaults.LMSTUDIO_DEFAULT_URL
        assert cfg.ollama_enabled and cfg.lmstudio_enabled
        assert cfg.openai_servers == ()
        # KARARLAR 1: model klasörü alanı YOK
        assert not any("dir" in f.name for f in fields(C.SourcesConfig))
        srv = C.OpenAIServer(name="llama.cpp", url="http://127.0.0.1:8080/v1")
        assert C.SourcesConfig(openai_servers=(srv,)).openai_servers[0].name == "llama.cpp"

    def test_dusunme_kapali_varsayilan(self):
        a, b = C.default_chat_template_kwargs(), C.default_chat_template_kwargs()
        assert a == {"enable_thinking": False} and a is not b
        assert C.ENGINE_OPTION_KEYS >= {"num_ctx", "keep_alive", "max_parallel", "n_ctx",
                                        "n_gpu_layers", "temperature", "repeat_penalty",
                                        "seed", "request_timeout_s"}

    def test_dosya_adlari_gitignore_da(self):
        lines = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        for fname in (C.MT_SELECTION_FILENAME, C.MT_SOURCES_FILENAME, C.MT_ENGINES_FILENAME):
            assert f"config/{fname}" in lines, fname


# ===========================================================================
# 4. EngineSpec yeni alanları
# ===========================================================================
class TestEngineSpecAlanlari:
    def test_varsayilanlar_bugunku_mlx_yolu(self):
        spec = EngineSpec(name="x")
        assert spec.backend == C.BACKEND_MLX
        assert (spec.model, spec.base_url, spec.path, spec.flavor) == ("", "", "", "")
        assert spec.options == {} and spec.extra_body == {}
        assert spec.think is False
        assert spec.chat_template_kwargs == {"enable_thinking": False}
        assert spec.system_prompt is False and spec.raw is False
        # eski alanlar aynen
        assert spec.format == "hymt" and spec.source == "builtin"

    def test_degisken_varsayilanlar_paylasilmaz(self):
        a, b = EngineSpec(name="a"), EngineSpec(name="b")
        a.options["num_ctx"] = 4096
        a.chat_template_kwargs["enable_thinking"] = True
        assert b.options == {} and b.chat_template_kwargs == {"enable_thinking": False}

    def test_builtin_hymt_mlx(self):
        from core.mt_engines import registry
        spec = registry.BUILTIN_SPECS[registry.DEFAULT_ENGINE]
        assert spec.backend == C.BACKEND_MLX and spec.path == "" and spec.model == ""

    def test_yeni_alanlarla_kurulur(self):
        spec = EngineSpec(name="ollama:qwen3:8b", format="chat", backend="ollama",
                          model="qwen3:8b", base_url="http://127.0.0.1:11434",
                          options={"num_ctx": 8192}, think=None, source=C.SPEC_SOURCE_ADHOC)
        assert spec.backend in C.BACKENDS and spec.source in C.SPEC_SOURCES


# ===========================================================================
# 5. http_util
# ===========================================================================
class TestCheckLocalUrl:
    @pytest.mark.parametrize("url,expected", [
        ("http://127.0.0.1:11434", "http://127.0.0.1:11434"),
        ("http://127.0.0.1:11434/", "http://127.0.0.1:11434"),
        ("  http://localhost:1234/v1/  ", "http://localhost:1234/v1"),
        ("HTTP://LOCALHOST:8080/v1", "http://LOCALHOST:8080/v1"),
        ("http://[::1]:1234/v1", "http://[::1]:1234/v1"),
        ("https://127.0.0.2:8443/v1", "https://127.0.0.2:8443/v1"),
        ("http://[::ffff:127.0.0.1]:8080", "http://[::ffff:127.0.0.1]:8080"),
        ("http://127.0.0.1", "http://127.0.0.1"),
    ])
    def test_kabul(self, url, expected):
        assert H.check_local_url(url) == expected

    @pytest.mark.parametrize("url", [
        "http://10.0.0.5:11434", "http://192.168.1.2:8080/v1", "http://100.64.0.1:11434",
        "http://0.0.0.0:11434", "https://api.openai.com/v1", "http://127.0.0.1.nip.io:80",
        "http://2130706433:11434", "http://[fe80::1]:1234",
    ])
    def test_uzak_reddedilir(self, url):
        with pytest.raises(C.BackendUnavailable) as ei:
            H.check_local_url(url)
        assert "yerel bir adres değil" in str(ei.value)

    @pytest.mark.parametrize("url", [
        "", "   ", "ftp://127.0.0.1", "127.0.0.1:11434", "http://", "http://u:p@127.0.0.1:1",
        "http://127.0.0.1:99999", "http://127.0.0.1:0", "http://127.0.0.1:1/a b",
        "http://127.0.0.1:1/#x", "http://127.0.0.1:1/\x00", None,
    ])
    def test_bicim_bozuk(self, url):
        with pytest.raises(C.BackendUnavailable) as ei:
            H.check_local_url(url)
        assert "Geçersiz sunucu adresi" in str(ei.value)

    def test_url_yardimcilari(self):
        assert H.url_join("http://h:1/v1/", "/models") == "http://h:1/v1/models"
        assert H.url_join("http://h:1", "api/tags") == "http://h:1/api/tags"
        assert H.url_root("http://127.0.0.1:1234/v1") == "http://127.0.0.1:1234"
        assert H.url_root("http://[::1]:1234/v1/x") == "http://[::1]:1234"


@pytest.mark.sahte_sunucu
class TestHttpIstek:
    def test_get_post_json(self, fake_ollama):
        assert H.get_json(fake_ollama.url + "/api/version", T)["version"]
        out = H.post_json(fake_ollama.url + "/api/show", {"model": "qwen3:8b"}, T)
        assert "completion" in out["capabilities"]
        method, path, body = fake_ollama.requests[-1]
        assert (method, path, body) == ("POST", "/api/show", {"model": "qwen3:8b"})

    def test_basliklar_authorization_yok_utf8(self, fake_openai):
        H.post_json(fake_openai.base_url + "/chat/completions",
                    {"model": "qwen", "messages": [{"role": "user", "content": "ş\n\nçğü"}]}, T)
        req = fake_openai.request_details[-1]
        lower = {k.lower(): v for k, v in req.headers.items()}
        assert "authorization" not in lower
        assert lower["content-type"].startswith("application/json")
        assert req.json["messages"][0]["content"] == "ş\n\nçğü"

    def test_kapali_port_unreachable(self, mt_isolation):
        t0 = time.monotonic()
        with pytest.raises(H.HttpUnreachable) as ei:
            H.get_json(mt_isolation.closed_url + "/api/tags", T)
        assert time.monotonic() - t0 < 1.0
        assert ei.value.url.startswith(mt_isolation.closed_url)

    def test_durdurulan_sunucu_unreachable(self, fake_llm_server):
        srv = fake_llm_server("ollama")
        url = srv.url
        srv.stop()
        with pytest.raises(H.HttpUnreachable):
            H.get_json(url + "/api/tags", T)

    def test_zaman_asimi(self, fake_ollama):
        fake_ollama.set_delay("/api/tags", 1.0, times=1)
        t0 = time.monotonic()
        with pytest.raises(H.HttpTimeout) as ei:
            H.get_json(fake_ollama.url + "/api/tags", 0.2)
        assert time.monotonic() - t0 < 0.9
        assert ei.value.timeout == 0.2

    def test_durum_hatasi_govdeyle(self, fake_ollama):
        fake_ollama.set_status("/api/tags", 503, {"error": "meşgul"}, times=1)
        with pytest.raises(H.HttpStatusError) as ei:
            H.get_json(fake_ollama.url + "/api/tags", T)
        assert ei.value.status == 503 and "meşgul" in ei.value.body
        assert not isinstance(ei.value, H.HttpInvalidJson)
        assert H.get_json(fake_ollama.url + "/api/tags", T)["models"]   # times=1 bitti

    def test_hata_govdesi_kirpilir(self, fake_ollama):
        fake_ollama.set_status("/api/tags", 500, "x" * 10_000, times=1)
        with pytest.raises(H.HttpStatusError) as ei:
            H.get_json(fake_ollama.url + "/api/tags", T)
        assert len(ei.value.body) == defaults.MT_HTTP_ERROR_BODY_MAX_CHARS

    def test_json_olmayan_basarili_yanit(self, fake_ollama):
        with pytest.raises(H.HttpInvalidJson) as ei:
            H.get_json(fake_ollama.url + "/", T)   # "Ollama is running"
        assert ei.value.body.startswith(H.INVALID_JSON_BODY)
        assert isinstance(ei.value, H.HttpStatusError) and ei.value.status == 200

    def test_bos_govde_none(self, fake_ollama):
        fake_ollama.set_status("/api/tags", 200, "", times=1)
        assert H.get_json(fake_ollama.url + "/api/tags", T) is None

    def test_uzak_adres_aga_gitmeden_reddedilir(self):
        with pytest.raises(C.BackendUnavailable, match="yerel bir adres değil"):
            H.get_json("http://10.255.255.1:11434/api/tags", T)
        with pytest.raises(C.BackendUnavailable, match="yerel bir adres değil"):
            H.post_json("https://api.openai.com/v1/chat/completions", {}, T)

    def test_proxy_yok_sayilir(self, fake_ollama, monkeypatch, mt_isolation):
        proxy = mt_isolation.closed_url   # proxy kullanılsaydı bağlantı reddi alınırdı
        for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY"):
            monkeypatch.setenv(var, proxy)
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        assert H.get_json(fake_ollama.url + "/api/version", T)["version"]

    def test_yonlendirme_yalniz_loopbacke(self, fake_ollama):
        target = {"loc": ""}

        class Redirect(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", target["loc"])
                self.send_header("Content-Length", "0")
                self.end_headers()

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        httpd.daemon_threads = True
        th = threading.Thread(target=httpd.serve_forever, daemon=True)
        th.start()
        try:
            url = f"http://127.0.0.1:{httpd.server_address[1]}/x"
            target["loc"] = fake_ollama.url + "/api/version"
            assert H.get_json(url, T)["version"]
            target["loc"] = "http://10.0.0.5:11434/api/version"
            with pytest.raises(C.BackendUnavailable, match="yerel bir adres değil"):
                H.get_json(url, T)
        finally:
            httpd.shutdown()
            httpd.server_close()


# ===========================================================================
# 6. Sahte sunucu — her rota
# ===========================================================================
def _chat_body(model, text="Hello world.", instr="Translate into Turkish.", **extra):
    body = {"model": model, "messages": [{"role": "user", "content": f"{instr}\n\n{text}"}],
            "stream": False}
    body.update(extra)
    return body


@pytest.mark.sahte_sunucu
class TestSahteOllama:
    def test_kok_ve_surum(self, fake_ollama):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(fake_ollama.url + "/", timeout=T) as r:
            assert r.read().decode() == "Ollama is running"
        head = urllib.request.Request(fake_ollama.url + "/", method="HEAD")
        with opener.open(head, timeout=T) as r:
            assert r.status == 200 and r.read() == b""
        assert H.get_json(fake_ollama.url + "/api/version", T) == {"version": "0.12.3"}
        assert fake_ollama.base_url == fake_ollama.url

    def test_tags_ayrintilar(self, fake_llm_server):
        srv = fake_llm_server("ollama", models=[
            "qwen3:8b", {"name": "nomic-embed-text:latest", "kind": "embedding",
                         "parameter_size": "137M", "quantization": "F16"}])
        data = H.get_json(srv.url + "/api/tags", T)
        names = [m["name"] for m in data["models"]]
        assert names == ["qwen3:8b", "nomic-embed-text:latest"]
        det = data["models"][0]["details"]
        assert det["parameter_size"] == "8.2B" and det["quantization_level"] == "Q4_K_M"
        assert data["models"][0]["size"] == FakeModel("x").size_bytes
        assert data["models"][0]["digest"].startswith("sha256:")

    def test_show_yetenekler_ve_404(self, fake_llm_server):
        srv = fake_llm_server("ollama", models=[
            FakeModel("qwen3:8b", thinking=True, context_length=40960),
            FakeModel("gemma3:4b", vision=True), FakeModel("emb", kind="embedding")])
        show = H.post_json(srv.url + "/api/show", {"model": "qwen3:8b"}, T)
        assert show["capabilities"] == ["completion", "thinking"]
        assert show["model_info"]["qwen3.context_length"] == 40960
        assert "completion" in H.post_json(srv.url + "/api/show", {"name": "gemma3:4b"}, T)[
            "capabilities"]
        assert H.post_json(srv.url + "/api/show", {"model": "emb"}, T)["capabilities"] == \
            ["embedding"]
        with pytest.raises(H.HttpStatusError) as ei:
            H.post_json(srv.url + "/api/show", {"model": "yok:1b"}, T)
        assert ei.value.status == 404 and "not found" in ei.value.body

    def test_latest_eslesmesi(self, fake_llm_server):
        srv = fake_llm_server("ollama", models=["qwen3:latest"])
        out = H.post_json(srv.url + "/api/chat", _chat_body("qwen3"), T)
        assert out["message"]["content"] == "ÇEVRİLDİ: Hello world."

    def test_chat_varsayilan_yanit_ve_ps(self, fake_ollama):
        assert H.get_json(fake_ollama.url + "/api/ps", T) == {"models": []}
        out = H.post_json(fake_ollama.url + "/api/chat", _chat_body(
            "qwen3:8b", options={"num_ctx": 8192, "num_predict": 64}, keep_alive="30m"), T)
        assert out["message"] == {"role": "assistant", "content": "ÇEVRİLDİ: Hello world."}
        assert out["done"] is True and out["done_reason"] == "stop"
        assert out["eval_count"] >= 1
        ps = H.get_json(fake_ollama.url + "/api/ps", T)
        assert [m["name"] for m in ps["models"]] == ["qwen3:8b"]
        assert fake_ollama.requests_to("/api/chat")[0]["options"]["num_ctx"] == 8192

    def test_chat_system_mesajinda_tum_user_icerigi(self, fake_ollama):
        body = {"model": "qwen3:8b", "stream": False, "messages": [
            {"role": "system", "content": "Translate."},
            {"role": "user", "content": "Para 1.\n\nPara 2."}]}
        out = H.post_json(fake_ollama.url + "/api/chat", body, T)
        assert out["message"]["content"] == "ÇEVRİLDİ: Para 1.\n\nPara 2."

    def test_chat_secenekleri(self, fake_llm_server):
        srv = fake_llm_server("ollama", think_block=True, prefix="Here is the translation: ",
                              finish_reason="length")
        out = H.post_json(srv.url + "/api/chat", _chat_body("qwen3:8b"), T)
        content = out["message"]["content"]
        assert content.startswith("<think>") and "</think>" in content
        assert content.endswith("Here is the translation: ÇEVRİLDİ: Hello world.")
        assert out["done_reason"] == "length"

    def test_chat_think(self, fake_llm_server):
        plain = fake_llm_server("ollama")
        with pytest.raises(H.HttpStatusError) as ei:
            H.post_json(plain.url + "/api/chat", _chat_body("qwen3:8b", think=True), T)
        assert ei.value.status == 400 and "does not support thinking" in ei.value.body
        # think:false yeteneksiz modelde hata değil
        assert H.post_json(plain.url + "/api/chat", _chat_body("qwen3:8b", think=False), T)
        thinker = fake_llm_server("ollama", thinking_capability=True)
        out = H.post_json(thinker.url + "/api/chat", _chat_body("qwen3:8b", think=True), T)
        assert out["message"]["thinking"] and out["message"]["content"].startswith("ÇEVRİLDİ")
        out = H.post_json(thinker.url + "/api/chat", _chat_body("qwen3:8b", think=False), T)
        assert "thinking" not in out["message"]

    def test_chat_akis_varsayilani_json_degil(self, fake_ollama):
        body = _chat_body("qwen3:8b")
        del body["stream"]
        with pytest.raises(H.HttpInvalidJson):
            H.post_json(fake_ollama.url + "/api/chat", body, T)

    def test_chat_yukleme_ve_hatalar(self, fake_llm_server):
        srv = fake_llm_server("ollama", models=["qwen3:8b", {"name": "emb", "kind": "embedding"}])
        load = H.post_json(srv.url + "/api/chat", {"model": "qwen3:8b", "messages": []}, T)
        assert load["done_reason"] == "load"
        with pytest.raises(H.HttpStatusError) as ei:
            H.post_json(srv.url + "/api/chat", _chat_body("yok:1b"), T)
        assert ei.value.status == 404 and "try pulling it first" in ei.value.body
        with pytest.raises(H.HttpStatusError) as ei:
            H.post_json(srv.url + "/api/chat", _chat_body("emb"), T)
        assert ei.value.status == 400

    def test_reply_fonksiyonu_ve_fakereply(self, fake_llm_server):
        def reply(text, body):
            if "BOZ" in text:
                return FakeReply(status=500, error="model requires more system memory")
            if "KES" in text:
                return FakeReply(content="yarım", finish_reason="length")
            return f"[{body['model']}] {text}"
        srv = fake_llm_server("ollama", reply=reply)
        out = H.post_json(srv.url + "/api/chat", _chat_body("qwen3:8b", "Merhaba"), T)
        assert out["message"]["content"] == "[qwen3:8b] Merhaba"
        out = H.post_json(srv.url + "/api/chat", _chat_body("qwen3:8b", "KES"), T)
        assert (out["message"]["content"], out["done_reason"]) == ("yarım", "length")
        with pytest.raises(H.HttpStatusError) as ei:
            H.post_json(srv.url + "/api/chat", _chat_body("qwen3:8b", "BOZ"), T)
        assert ei.value.status == 500 and "system memory" in ei.value.body

    def test_reply_istisnasi_500(self, fake_llm_server):
        def reply(text):
            raise RuntimeError("sahte çöküş")
        srv = fake_llm_server("ollama", reply=reply)
        with pytest.raises(H.HttpStatusError) as ei:
            H.post_json(srv.url + "/api/chat", _chat_body("qwen3:8b"), T)
        assert ei.value.status == 500 and "sahte çöküş" in ei.value.body

    def test_generate_ham_hymt_ve_yukleme(self, fake_ollama):
        prompt = "<|startoftext|>Translate into Turkish.<|extra_4|>The cat sat.<|extra_0|>"
        out = H.post_json(fake_ollama.url + "/api/generate",
                          {"model": "qwen3:8b", "prompt": prompt, "raw": True, "stream": False}, T)
        assert out["response"] == "ÇEVRİLDİ: The cat sat." and out["done_reason"] == "stop"
        load = H.post_json(fake_ollama.url + "/api/generate",
                           {"model": "qwen3:8b", "prompt": "", "keep_alive": "30m"}, T)
        assert load["done_reason"] == "load" and load["response"] == ""
        assert fake_ollama.requests_to("/api/generate")[0]["raw"] is True

    def test_bilinmeyen_rota(self, fake_ollama):
        with pytest.raises(H.HttpStatusError) as ei:
            H.get_json(fake_ollama.url + "/v1/models", T)
        assert ei.value.status == 404


@pytest.mark.sahte_sunucu
class TestSahteOpenAI:
    def test_health_ve_models(self, fake_openai):
        assert fake_openai.base_url == fake_openai.url + "/v1"
        assert H.get_json(fake_openai.url + "/health", T) == {"status": "ok"}
        data = H.get_json(fake_openai.base_url + "/models", T)
        assert data["object"] == "list" and [m["id"] for m in data["data"]] == ["qwen"]

    def test_chat(self, fake_openai):
        out = H.post_json(fake_openai.base_url + "/chat/completions", _chat_body(
            "qwen", max_tokens=64, temperature=0, seed=0,
            chat_template_kwargs={"enable_thinking": False}), T)
        choice = out["choices"][0]
        assert choice["message"]["content"] == "ÇEVRİLDİ: Hello world."
        assert choice["finish_reason"] == "stop" and out["object"] == "chat.completion"
        sent = fake_openai.requests_to("/v1/chat/completions")[0]
        assert sent["chat_template_kwargs"] == {"enable_thinking": False}

    def test_chat_secenekleri(self, fake_llm_server):
        srv = fake_llm_server("openai", reasoning_content="düşündüm", finish_reason="length",
                              think_block=True)
        msg = H.post_json(srv.base_url + "/chat/completions", _chat_body("qwen"), T)
        assert msg["choices"][0]["message"]["reasoning_content"] == "düşündüm"
        assert msg["choices"][0]["message"]["content"].startswith("<think>")
        assert msg["choices"][0]["finish_reason"] == "length"
        srv.content_none = True
        msg = H.post_json(srv.base_url + "/chat/completions", _chat_body("qwen"), T)
        assert msg["choices"][0]["message"]["content"] is None

    def test_isinma_tek_token(self, fake_openai):
        out = H.post_json(fake_openai.base_url + "/chat/completions",
                          _chat_body("qwen", max_tokens=1), T)
        assert out["choices"][0]["finish_reason"] == "length"
        assert out["choices"][0]["message"]["content"] == "ÇEVRİLDİ:"

    def test_bilinmeyen_alan_reddi(self, fake_llm_server):
        srv = fake_llm_server("openai", reject_body_keys=("chat_template_kwargs",))
        with pytest.raises(H.HttpStatusError) as ei:
            H.post_json(srv.base_url + "/chat/completions",
                        _chat_body("qwen", chat_template_kwargs={"enable_thinking": False}), T)
        assert ei.value.status == 400 and "chat_template_kwargs" in ei.value.body
        assert H.post_json(srv.base_url + "/chat/completions", _chat_body("qwen"), T)

    def test_model_yok_ve_gevsek_mod(self, fake_llm_server):
        srv = fake_llm_server("openai")
        with pytest.raises(H.HttpStatusError) as ei:
            H.post_json(srv.base_url + "/chat/completions", _chat_body("yok"), T)
        assert ei.value.status == 404 and "does not exist" in ei.value.body
        loose = fake_llm_server("openai", strict_model=False)   # llama.cpp server gibi
        out = H.post_json(loose.base_url + "/chat/completions", _chat_body("herhangi"), T)
        assert out["model"] == "qwen"

    def test_completions_ham(self, fake_openai):
        prompt = "<|startoftext|>Translate.<|extra_4|>Good morning.<|extra_0|>"
        out = H.post_json(fake_openai.base_url + "/completions",
                          {"model": "qwen", "prompt": prompt, "max_tokens": 32,
                           "temperature": 0}, T)
        assert out["choices"][0]["text"] == "ÇEVRİLDİ: Good morning."
        assert out["object"] == "text_completion"

    def test_akis_sse_json_degil(self, fake_openai):
        with pytest.raises(H.HttpInvalidJson):
            H.post_json(fake_openai.base_url + "/chat/completions",
                        _chat_body("qwen", stream=True), T)
        with pytest.raises(H.HttpInvalidJson):
            H.post_json(fake_openai.base_url + "/completions",
                        {"model": "qwen", "prompt": "x", "stream": True}, T)

    def test_bilinmeyen_rota(self, fake_openai):
        with pytest.raises(H.HttpStatusError) as ei:
            H.get_json(fake_openai.url + "/api/v1/models", T)
        assert ei.value.status == 404


@pytest.mark.sahte_sunucu
class TestSahteLMStudio:
    MODELS = [
        FakeModel("qwen/qwen3-8b", thinking=True, loaded=True, format="mlx",
                  quantization="4bit", parameter_size="8B", context_length=32768),
        FakeModel("google/gemma-3-4b", format="gguf", vision=True),
        FakeModel("text-embedding-nomic-embed-text-v1.5", kind="embedding"),
    ]

    def _srv(self, factory, **kw):
        import copy
        return factory("lmstudio", models=copy.deepcopy(self.MODELS), **kw)

    def test_rest_v1_models(self, fake_llm_server):
        srv = self._srv(fake_llm_server)
        data = H.get_json(srv.url + "/api/v1/models", T)["models"]
        q, g, e = data
        assert q["type"] == "llm" and q["key"] == "qwen/qwen3-8b" and q["format"] == "mlx"
        assert q["quantization"]["name"] == "4bit" and q["params_string"] == "8B"
        assert q["loaded_instances"] and q["max_context_length"] == 32768
        assert q["capabilities"]["reasoning"]["default"] == "on"
        assert q["publisher"] == "qwen" and q["architecture"] == "qwen3"
        assert g["loaded_instances"] == [] and "reasoning" not in g["capabilities"]
        assert g["capabilities"]["vision"] is True
        assert e["type"] == "embedding" and "capabilities" not in e

    def test_rest_v0_models(self, fake_llm_server):
        srv = self._srv(fake_llm_server)
        data = H.get_json(srv.url + "/api/v0/models", T)["data"]
        assert [d["type"] for d in data] == ["llm", "vlm", "embeddings"]
        assert data[0]["state"] == "loaded" and data[1]["state"] == "not-loaded"
        assert data[0]["compatibility_type"] == "mlx"

    def test_v1_kapatilinca_404(self, fake_llm_server):
        srv = self._srv(fake_llm_server)
        srv.set_status("/api/v1/models", 404)
        with pytest.raises(H.HttpStatusError) as ei:
            H.get_json(srv.url + "/api/v1/models", T)
        assert ei.value.status == 404
        assert H.get_json(srv.url + "/api/v0/models", T)["data"]

    def test_openai_rotalari(self, fake_llm_server):
        srv = self._srv(fake_llm_server)
        ids = [m["id"] for m in H.get_json(srv.base_url + "/models", T)["data"]]
        assert len(ids) == 3   # JIT açık: indirilmiş hepsi
        out = H.post_json(srv.base_url + "/chat/completions", _chat_body("google/gemma-3-4b"), T)
        assert out["choices"][0]["message"]["content"] == "ÇEVRİLDİ: Hello world."
        with pytest.raises(H.HttpStatusError) as ei:   # gömme modeli üretemez
            H.post_json(srv.base_url + "/chat/completions",
                        _chat_body("text-embedding-nomic-embed-text-v1.5"), T)
        assert ei.value.status == 400
        out = H.post_json(srv.base_url + "/completions",
                          {"model": "qwen/qwen3-8b", "prompt": "a\n\nb", "stream": False}, T)
        assert out["choices"][0]["text"] == "ÇEVRİLDİ: b"

    def test_jit_kapali(self, fake_llm_server):
        srv = self._srv(fake_llm_server, jit=False)
        ids = [m["id"] for m in H.get_json(srv.base_url + "/models", T)["data"]]
        assert ids == ["qwen/qwen3-8b"]
        with pytest.raises(H.HttpStatusError) as ei:
            H.post_json(srv.base_url + "/chat/completions", _chat_body("google/gemma-3-4b"), T)
        assert ei.value.status == 404 and "not loaded" in ei.value.body

    def test_bilinmeyen_rota(self, fake_lmstudio):
        with pytest.raises(H.HttpStatusError) as ei:
            H.get_json(fake_lmstudio.url + "/api/tags", T)
        assert ei.value.status == 404 and "Unexpected endpoint" in ei.value.body


@pytest.mark.sahte_sunucu
class TestSahteSunucuAltyapi:
    def test_gecersiz_flavor(self):
        with pytest.raises(ValueError):
            FakeLLMServer("vllm", start=False)

    def test_baglam_yoneticisi_ve_durdurma(self):
        with FakeLLMServer("openai") as srv:
            url = srv.url
            assert srv.running
            assert H.get_json(url + "/health", T)
        assert not srv.running
        with pytest.raises(H.HttpUnreachable):
            H.get_json(url + "/health", T)
        srv.stop()   # ikinci kez güvenli

    def test_set_status_sirasi_ve_times(self, fake_ollama):
        fake_ollama.set_status("/api/tags", 503, times=2)
        fake_ollama.set_status("/api/tags", 500, {"error": "son"}, times=1)
        statuses = []
        for _ in range(4):
            try:
                H.get_json(fake_ollama.url + "/api/tags", T)
                statuses.append(200)
            except H.HttpStatusError as exc:
                statuses.append(exc.status)
        assert statuses == [503, 503, 500, 200]
        assert [r.status for r in fake_ollama.request_details] == statuses
        fake_ollama.set_status("/api/tags", 502)   # kalıcı
        for _ in range(2):
            with pytest.raises(H.HttpStatusError):
                H.get_json(fake_ollama.url + "/api/tags", T)
        fake_ollama.clear_status("/api/tags")
        assert H.get_json(fake_ollama.url + "/api/tags", T)["models"]

    def test_eszamanli_istek_sayaci(self, fake_llm_server):
        srv = fake_llm_server("openai", delay_s=0.3)
        results = []

        def call(i):
            results.append(H.post_json(srv.base_url + "/chat/completions",
                                       _chat_body("qwen", f"metin {i}"), T))
        threads = [threading.Thread(target=call, args=(i,)) for i in range(2)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert len(results) == 2 and srv.max_concurrent == 2
        a, b = srv.request_details
        assert a.t_start < b.t_end and b.t_start < a.t_end   # örtüşen zamanlar
        srv.reset_requests()
        assert srv.requests == [] and srv.max_concurrent == 0

    def test_set_models_ve_ayar_degisimi(self, fake_ollama):
        fake_ollama.set_models(["llama3.2:3b"])
        assert [m["name"] for m in H.get_json(fake_ollama.url + "/api/tags", T)["models"]] == \
            ["llama3.2:3b"]
        fake_ollama.reply = lambda text: "sabit"
        out = H.post_json(fake_ollama.url + "/api/chat", _chat_body("llama3.2:3b"), T)
        assert out["message"]["content"] == "sabit"

    def test_gercek_portlara_baglanmaz(self, fake_ollama, fake_openai, fake_lmstudio):
        for srv in (fake_ollama, fake_openai, fake_lmstudio):
            assert srv.port not in REAL_LOCAL_MODEL_PORTS
            assert srv.host == "127.0.0.1"


# ===========================================================================
# 7. Sahte llama_cpp
# ===========================================================================
class TestSahteLlama:
    def test_varsayilan_kurulu_degil(self):
        import importlib.util
        assert importlib.util.find_spec("llama_cpp") is None
        with pytest.raises(ImportError):
            import llama_cpp  # noqa: F401

    def test_fixture_modulu_koyar(self, fake_llama):
        import importlib.util
        spec = importlib.util.find_spec("llama_cpp")
        assert spec is not None and spec.name == "llama_cpp"
        import llama_cpp
        assert llama_cpp is fake_llama and llama_cpp.__version__.endswith("sahte")

    def test_kurucu_kaydi_ve_yol_denetimi(self, fake_llama, tmp_path):
        gguf = write_fake_gguf(tmp_path / "m.gguf", "qwen3")
        llm = fake_llama.Llama(model_path=str(gguf), n_ctx=8192, n_gpu_layers=-1, seed=0,
                               verbose=False)
        assert llm.kwargs["n_ctx"] == 8192 and llm.kwargs["n_gpu_layers"] == -1
        assert fake_llama.instances == [llm] and llm.n_ctx() == 8192
        with pytest.raises(ValueError, match="does not exist"):
            fake_llama.Llama(model_path=str(tmp_path / "yok.gguf"))
        fake_llama.config.load_error = "Failed to load model from file"
        with pytest.raises(ValueError, match="Failed to load"):
            fake_llama.Llama(model_path=str(gguf))

    def test_chat_completion(self, fake_llama, tmp_path):
        llm = fake_llama.Llama(model_path=str(write_fake_gguf(tmp_path / "m.gguf", "qwen3")))
        out = llm.create_chat_completion(
            messages=[{"role": "user", "content": "Translate.\n\nHello world."}],
            max_tokens=64, temperature=0.0, top_p=1.0, repeat_penalty=1.0)
        assert out["choices"][0]["message"]["content"] == "ÇEVRİLDİ: Hello world."
        assert out["choices"][0]["finish_reason"] == "stop"
        name, kw = fake_llama.calls[-1]
        assert name == "create_chat_completion" and kw["temperature"] == 0.0

    def test_stopping_criteria_ve_max_tokens(self, fake_llama, tmp_path):
        llm = fake_llama.Llama(model_path=str(write_fake_gguf(tmp_path / "m.gguf", "qwen3")))
        seen = []

        def stop_after_two(input_ids, logits):
            seen.append(len(input_ids))
            return len(input_ids) >= 2
        msgs = [{"role": "user", "content": "T.\n\nbir iki üç dört"}]
        out = llm.create_chat_completion(
            messages=msgs, max_tokens=64,
            stopping_criteria=fake_llama.StoppingCriteriaList([stop_after_two]))
        assert out["choices"][0]["message"]["content"] == "ÇEVRİLDİ: bir"
        assert seen == [1, 2]
        out = llm.create_chat_completion(messages=msgs, max_tokens=3)
        assert out["choices"][0]["finish_reason"] == "length"
        assert out["choices"][0]["message"]["content"] == "ÇEVRİLDİ: bir iki"

    def test_sure_siniri_benzetimi(self, fake_llama, tmp_path):
        fake_llama.config.token_delay_s = 0.02
        llm = fake_llama.Llama(model_path=str(write_fake_gguf(tmp_path / "m.gguf", "qwen3")))
        deadline = time.monotonic() + 0.05
        out = llm.create_chat_completion(
            messages=[{"role": "user", "content": "T.\n\n" + " ".join(["kelime"] * 50)}],
            max_tokens=500,
            stopping_criteria=fake_llama.StoppingCriteriaList(
                [lambda ids, logits: time.monotonic() > deadline]))
        produced = out["choices"][0]["message"]["content"].split()
        assert 1 <= len(produced) < 20

    def test_completion_ham_ve_stop(self, fake_llama, tmp_path):
        llm = fake_llama.Llama(model_path=str(write_fake_gguf(tmp_path / "m.gguf", "hunyuan")))
        prompt = "<|startoftext|>Translate.<|extra_4|>Good night.<|extra_0|>"
        out = llm.create_completion(prompt=prompt, max_tokens=32, temperature=0.0)
        assert out["choices"][0]["text"] == "ÇEVRİLDİ: Good night."
        fake_llama.config.reply = lambda text: text + "<|eos|> artık"
        out = llm.create_completion(prompt=prompt, max_tokens=32, stop=["<|eos|>"])
        assert out["choices"][0]["text"] == "Good night."
        assert out["choices"][0]["finish_reason"] == "stop"

    def test_ayarlar(self, tmp_path):
        mod = make_fake_llama_module(reply=lambda t, kw: f"{kw['max_tokens']}:{t}",
                                     think_block=True, prefix="Çeviri: ",
                                     chat_template="{{ messages }}")
        llm = mod.Llama(model_path=str(write_fake_gguf(tmp_path / "m.gguf", "qwen3")))
        assert llm.metadata["tokenizer.chat_template"] == "{{ messages }}"
        out = llm.create_chat_completion(messages=[{"role": "user", "content": "a\n\nb"}],
                                         max_tokens=99)
        content = out["choices"][0]["message"]["content"]
        assert content.startswith("<think>") and content.endswith("Çeviri: 99:b")


# ===========================================================================
# 8. Sahte model dosyaları
# ===========================================================================
class TestModelDosyalari:
    def test_gguf_gidis_donus(self, tmp_path):
        p = write_fake_gguf(tmp_path / "Qwen3-8B-Q4_K_M.gguf", "qwen3", name="Qwen3 8B",
                            size_label="8B", chat_template="{% if enable_thinking %}<think>",
                            context_length=40960, extra_kv={"general.file_type": 15},
                            pad_bytes=1000)
        kv = read_gguf_kv(p)
        assert kv["general.architecture"] == "qwen3" and kv["general.type"] == "model"
        assert kv["general.name"] == "Qwen3 8B" and kv["general.size_label"] == "8B"
        assert kv["qwen3.context_length"] == 40960 and kv["general.file_type"] == 15
        assert "enable_thinking" in kv["tokenizer.chat_template"]
        raw = p.read_bytes()
        assert raw[:4] == b"GGUF" and int.from_bytes(raw[4:8], "little") == 3
        assert int.from_bytes(raw[8:16], "little") == 0   # tensör yok
        assert p.stat().st_size >= 1000

    def test_gguf_tasarim_okuyucusuyla_uyumlu(self, tmp_path):
        """TASARIM'daki ölçüm betiğiyle (gguf_probe) aynı ayrıştırma yolu."""
        import struct
        p = write_fake_gguf(tmp_path / "m.gguf", "clip", type_="mmproj",
                            extra_kv={"clip.vision.image_size": 336, "x.arr": ["a", "b"]})
        with open(p, "rb") as f:
            assert f.read(4) == b"GGUF"
            _ver, = struct.unpack("<I", f.read(4))
            _nt, nkv = struct.unpack("<QQ", f.read(16))
        assert nkv == 4
        kv = read_gguf_kv(p)
        assert kv["general.type"] == "mmproj" and kv["x.arr"] == ["a", "b"]

    def test_gguf_tur_yok(self, tmp_path):
        kv = read_gguf_kv(write_fake_gguf(tmp_path / "eski.gguf", "llama", type_=None))
        assert "general.type" not in kv

    def test_parcali_gguf(self, tmp_path):
        paths = write_fake_gguf_split(tmp_path, "Big-Q8_0", 3, "qwen3", size_label="70B",
                                      pad_bytes=100)
        assert [p.name for p in paths] == ["Big-Q8_0-00001-of-00003.gguf",
                                           "Big-Q8_0-00002-of-00003.gguf",
                                           "Big-Q8_0-00003-of-00003.gguf"]
        first, second = read_gguf_kv(paths[0]), read_gguf_kv(paths[1])
        assert first["split.no"] == 0 and first["split.count"] == 3
        assert first["general.size_label"] == "70B"
        assert second["split.no"] == 1 and "general.architecture" not in second

    @pytest.mark.parametrize("kind", ["magic", "truncated", "kv_count", "empty"])
    def test_bozuk_gguf(self, tmp_path, kind):
        p = write_corrupt_gguf(tmp_path / f"{kind}.gguf", kind)
        with pytest.raises(Exception):   # (ValueError | struct.error) — başlık okunamaz
            read_gguf_kv(p)

    def test_mlx_klasoru(self, tmp_path):
        d = write_fake_mlx_dir(tmp_path / "qwen3-8b-mlx", "qwen3",
                               chat_template="{% if enable_thinking %}",
                               quantization={"group_size": 64, "bits": 4})
        cfg = json.loads((d / "config.json").read_text(encoding="utf-8"))
        assert cfg["model_type"] == "qwen3" and cfg["quantization"]["bits"] == 4
        assert (d / "model.safetensors").stat().st_size > 8
        tcfg = json.loads((d / "tokenizer_config.json").read_text(encoding="utf-8"))
        assert tcfg["chat_template"] == "{% if enable_thinking %}"
        assert (d / "tokenizer.json").exists()
        eksik = write_fake_mlx_dir(tmp_path / "eksik", "gemma4", safetensors=False,
                                   tokenizer=False)
        assert sorted(p.name for p in eksik.iterdir()) == ["config.json"]
        sadece_model = write_fake_mlx_dir(tmp_path / "spm", "llama",
                                          tokenizer_files=("tokenizer.model",))
        assert (sadece_model / "tokenizer.model").exists()
        assert not (sadece_model / "tokenizer.json").exists()


# ===========================================================================
# 9. Autouse yalıtım (_mt_isolation)
# ===========================================================================
class TestYalitim:
    def test_model_koku_ve_kullanici_kayitlari(self, mt_isolation):
        from core.mt_engines import registry
        assert registry.MODELS_DIR == mt_isolation.models_dir
        assert registry.MODELS_DIR.is_dir() and not any(registry.MODELS_DIR.iterdir())
        assert registry.MODELS_DIR != PROJECT_ROOT / "models"
        assert registry.USER_SPECS_PATH == mt_isolation.user_specs_path
        assert not registry.USER_SPECS_PATH.exists()
        assert set(registry.available_engines()) == set(registry.BUILTIN_SPECS)

    def test_gercek_sunucu_portlari_reddedilir(self):
        for host in ("127.0.0.1", "localhost"):
            for port in sorted(REAL_LOCAL_MODEL_PORTS):
                with pytest.raises(ConnectionRefusedError):
                    socket.create_connection((host, port), timeout=1)
        with pytest.raises(H.HttpUnreachable):
            H.get_json(defaults.OLLAMA_DEFAULT_URL + "/api/tags", T)
        with pytest.raises(H.HttpUnreachable):
            H.get_json(defaults.LMSTUDIO_DEFAULT_URL + "/models", T)

    def test_kapali_kaynak_yapilandirmasi(self, mt_isolation):
        cfg = mt_isolation.sources
        assert isinstance(cfg, C.SourcesConfig)
        assert cfg.ollama_url == mt_isolation.closed_url
        assert cfg.lmstudio_url == mt_isolation.closed_url + "/v1"
        assert cfg.openai_servers == ()
        port = int(mt_isolation.closed_url.rsplit(":", 1)[1])
        assert port in mt_isolation.blocked_ports and port not in REAL_LOCAL_MODEL_PORTS

    def test_secim_dosyasi_tmp(self, mt_isolation):
        import ui.runner as runner
        path = runner.engine_choice_path()
        assert path == mt_isolation.selection_path and not path.exists()
        assert PROJECT_ROOT not in path.parents

    def test_monkeypatch_undo_yalitimi_kaldirmaz(self, monkeypatch, mt_isolation):
        from core.mt_engines import registry
        monkeypatch.setattr(registry, "DEFAULT_ENGINE", registry.DEFAULT_ENGINE)
        monkeypatch.undo()
        assert registry.MODELS_DIR == mt_isolation.models_dir
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", 11434), timeout=1)

    def test_discovery_load_sources_yamasi(self, monkeypatch, tmp_path):
        """K2'nin discovery modülü varmış gibi: path=None yalıtılır, açık path gerçek."""
        import types
        fake = types.ModuleType("core.mt_engines.discovery")
        calls = []

        def real_load_sources(path=None):
            calls.append(path)
            return ("gerçek", path)
        fake.load_sources = real_load_sources
        fake.__spec__ = importlib.machinery.ModuleSpec("core.mt_engines.discovery", None)
        monkeypatch.setitem(sys.modules, "core.mt_engines.discovery", fake)
        mp = pytest.MonkeyPatch()
        try:
            iso = apply_mt_isolation(mp, tmp_path / "iso", 1)
            assert fake.load_sources() is iso.sources
            assert fake.load_sources(tmp_path / "x.json") == ("gerçek", tmp_path / "x.json")
            assert calls == [tmp_path / "x.json"]
            assert iso.original_load_sources is real_load_sources
            assert "core.mt_engines.discovery.load_sources" in iso.patched
        finally:
            mp.undo()
        assert fake.load_sources is real_load_sources

    def test_yamalar_geri_alinir(self, tmp_path):
        from core.mt_engines import registry
        before = (registry.MODELS_DIR, registry.USER_SPECS_PATH, socket.create_connection,
                  sys.modules.get("llama_cpp", "YOK"))
        mp = pytest.MonkeyPatch()
        apply_mt_isolation(mp, tmp_path / "iso2", 2)
        assert registry.MODELS_DIR == tmp_path / "iso2" / "models"
        mp.undo()
        after = (registry.MODELS_DIR, registry.USER_SPECS_PATH, socket.create_connection,
                 sys.modules.get("llama_cpp", "YOK"))
        assert before == after


class TestPytestAyarlari:
    def test_isaretler_ve_varsayilan_secim(self, pytestconfig):
        markers = "\n".join(pytestconfig.getini("markers"))
        assert "yerel_model:" in markers and "sahte_sunucu:" in markers
        assert "integration:" in markers
        addopts = (PROJECT_ROOT / "pytest.ini").read_text(encoding="utf-8")
        assert 'addopts = -m "not integration and not yerel_model"' in addopts

    def test_test_dosyasi_logging_disable_kullanmaz(self):
        src = Path(__file__).read_text(encoding="utf-8")
        assert "logging." + "disable(" not in src
