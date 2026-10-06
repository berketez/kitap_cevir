"""Yerel model keşfi ve ön kontrol (K2, TASARIM D; KARARLAR 1-2).

Kapsam (TASARIM H.3 testleri 5-10 + D.4 komut satırı):
    5. MLX klasörleri: desteklenen / desteklenmeyen mimari, eksik dosya,
       sembolik bağ + ``.nosync`` tek kayıt, config.json'suz klasör atlanır,
       kayıtlı motorun klasörü ikinci kez listelenmez, HY-MT biçim tahmini.
    6. GGUF: model listelenir, görsel projektör (ad / başlık) listelenmez,
       parçalı dosya tek kayıt + toplam boyut, llama-cpp-python yoksa neden +
       kurulum komutu, bozuk başlık, ayrıntı dosya adındaki nicelemeden.
    7. Ollama (sahte sunucu): modeller + ayrıntı, gömme modeli kullanılamaz,
       yüklü mü, sunucu kapalıyken SIFIR Ollama modeli.
    8. LM Studio: REST v1 zenginleştirmesi, v1 yoksa v0, o da yoksa yalnız
       kimlikler; JIT kapalıyken yüklü olmayan model.
    9. Zaman: yavaş sunucu + kısa zaman aşımı -> kaynak erişilemez, toplam
       süre paralellik sayesinde kısa.
   10. ``check_engine``: her kimlik türü için başarı / başarısızlık; model
       YÜKLENMEZ; ``qwen3`` ≡ ``qwen3:latest``.
   D.4 ``python -m core.mt_engines.discovery --json``: iki sunucu kapalıyken
       ≤ 3 sn, geçerli JSON, hiç sunucu modeli yok.

KARARLAR 1: dosya taraması YALNIZ ``models/``; ``mt_sources.json`` klasör
ekleyemez; başka yerdeki model ``models/`` altına sembolik bağla konur.

Yalıtım tests/conftest.py ``_mt_isolation``'dadır (boş models/, kapalı
sunucu adresleri, llama_cpp kurulu değilmiş gibi). Sahte sunucular gerçek
soket üzerinde koşar; hiçbir model yüklenmez.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from config.defaults import DISCOVERY_MAX_DEPTH
from core.mt_engines import contracts, discovery, registry
from tests.fakes import (
    FakeModel,
    closed_local_port,
    write_corrupt_gguf,
    write_fake_gguf,
    write_fake_gguf_split,
    write_fake_mlx_dir,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_THINK_TEMPLATE = "{% if enable_thinking %}<think>{% endif %}{{ messages }}"


# ===========================================================================
# Ortak yardımcılar
# ===========================================================================

@pytest.fixture
def fake_mlx_lm(tmp_path, monkeypatch):
    """Kurulu mlx-lm yerine sahte paket düzeni (makineden bağımsız mimari listesi).

    Desteklenen: llama, qwen3, hunyuan_v1_dense (+ mistral -> llama yeniden
    eşlemesi). ``gemma4`` desteklenmez.
    """
    pkg = tmp_path / "sahte_mlx_lm"
    (pkg / "models").mkdir(parents=True)
    for arch in ("llama", "qwen3", "hunyuan_v1_dense"):
        (pkg / "models" / f"{arch}.py").write_text("# sahte\n", encoding="utf-8")
    layout = (str(pkg), (("mistral", "llama"),))
    monkeypatch.setattr(discovery, "_mlx_lm_layout", lambda: layout)
    return pkg


def _point_sources(monkeypatch, iso, *, ollama: str | None = None,
                   lmstudio: str | None = None, openai=(),
                   ollama_enabled: bool = True, lmstudio_enabled: bool = True):
    """``discovery.load_sources()`` verilen adresleri dönsün (verilmeyen = kapalı port)."""
    cfg = contracts.SourcesConfig(
        ollama_url=ollama or iso.closed_url, ollama_enabled=ollama_enabled,
        lmstudio_url=lmstudio or iso.closed_url + "/v1", lmstudio_enabled=lmstudio_enabled,
        openai_servers=tuple(openai))
    monkeypatch.setattr(discovery, "load_sources", lambda path=None, *a, **k: cfg)
    return cfg


def _of(report, source):
    return [m for m in report.models if m.source == source]


def _status(report, source):
    matches = [s for s in report.sources if s.source == source]
    assert len(matches) == 1, report.sources
    return matches[0]


def _generation_paths(server) -> list[str]:
    return [path for _method, path, _body in server.requests
            if path in ("/api/chat", "/api/generate", "/v1/chat/completions",
                        "/v1/completions")]


# ===========================================================================
# 5. MLX klasörleri
# ===========================================================================

class TestMlxKesfi:
    def test_desteklenen_mimari_kullanilabilir(self, mt_isolation, fake_mlx_lm):
        path = write_fake_mlx_dir(mt_isolation.models_dir / "qwen3-8b-mlx", "qwen3",
                                  quantization={"bits": 4, "group_size": 64})
        report = discovery.discover_all()
        model = report.model_by_id("mlx:qwen3-8b-mlx")
        assert model is not None and model.usable and model.reason == ""
        assert model.source == contracts.SOURCE_MLX
        assert model.backend == contracts.BACKEND_MLX
        assert model.format_guess == contracts.FORMAT_CHAT
        assert model.label == "qwen3-8b-mlx"
        assert model.detail == "mimari: qwen3 · 4 bit nicemlenmiş"
        assert model.size_bytes == (path / "model.safetensors").stat().st_size
        assert model.location == str(path)
        assert _status(report, contracts.SOURCE_MLX).message == "1 klasör"

    def test_yeniden_eslenen_mimari(self, mt_isolation, fake_mlx_lm):
        write_fake_mlx_dir(mt_isolation.models_dir / "mistral-mlx", "mistral")
        assert discovery.discover_all().model_by_id("mlx:mistral-mlx").usable

    def test_desteklenmeyen_mimari_nedeniyle(self, mt_isolation, fake_mlx_lm):
        write_fake_mlx_dir(mt_isolation.models_dir / "gemma4-mlx", "gemma4")
        model = discovery.discover_all().model_by_id("mlx:gemma4-mlx")
        assert model.usable is False
        assert model.reason == contracts.MSG_MLX_ARCH_UNSUPPORTED.format(arch="gemma4")

    def test_mlx_lm_yoksa_mimari_bilinmez_engel_degil(self, mt_isolation, monkeypatch):
        monkeypatch.setattr(discovery, "_mlx_lm_layout", lambda: None)
        assert discovery.mlx_arch_supported("gemma4") is None
        write_fake_mlx_dir(mt_isolation.models_dir / "gemma4-mlx", "gemma4")
        assert discovery.discover_all().model_by_id("mlx:gemma4-mlx").usable

    @pytest.mark.parametrize("kwargs,fragment", [
        ({"safetensors": False}, "*.safetensors"),
        ({"tokenizer": False}, "tokenizer"),
    ])
    def test_eksik_dosya_nedeni(self, mt_isolation, fake_mlx_lm, kwargs, fragment):
        path = write_fake_mlx_dir(mt_isolation.models_dir / "eksik-mlx", "qwen3", **kwargs)
        model = discovery.discover_all().model_by_id("mlx:eksik-mlx")
        assert model.usable is False
        assert model.reason.startswith("MLX klasöründe model dosyaları eksik")
        assert fragment in model.reason and str(path) in model.reason

    def test_sembolik_bag_ve_nosync_tek_kayit(self, mt_isolation, fake_mlx_lm):
        real = write_fake_mlx_dir(mt_isolation.models_dir / "foo-mlx.nosync", "llama")
        (mt_isolation.models_dir / "foo-mlx").symlink_to(real, target_is_directory=True)
        models = _of(discovery.discover_all(), contracts.SOURCE_MLX)
        assert [m.engine_id for m in models] == ["mlx:foo-mlx"]
        assert models[0].label == "foo-mlx"

    def test_models_disindaki_klasor_sembolik_bagla_gelir(self, mt_isolation, fake_mlx_lm,
                                                          tmp_path):
        # KARARLAR 1: tarama yalnız models/; bağ izlenir.
        outside = write_fake_mlx_dir(tmp_path / "lmstudio" / "org" / "Qwen3-MLX", "qwen3")
        assert _of(discovery.discover_all(), contracts.SOURCE_MLX) == []
        (mt_isolation.models_dir / "Qwen3-MLX").symlink_to(outside, target_is_directory=True)
        models = _of(discovery.discover_all(), contracts.SOURCE_MLX)
        assert [m.engine_id for m in models] == ["mlx:Qwen3-MLX"]
        assert discovery.check_engine("mlx:Qwen3-MLX").ok

    def test_config_json_suz_klasor_atlanir(self, mt_isolation, fake_mlx_lm):
        other = mt_isolation.models_dir / "doclayout-yolo"
        other.mkdir()
        (other / "model.pt").write_bytes(b"\x00" * 16)
        report = discovery.discover_all()
        assert _of(report, contracts.SOURCE_MLX) == []
        assert _status(report, contracts.SOURCE_MLX).message == "0 klasör"

    def test_gizli_klasor_atlanir(self, mt_isolation, fake_mlx_lm):
        write_fake_mlx_dir(mt_isolation.models_dir / ".gizli-mlx", "llama")
        assert _of(discovery.discover_all(), contracts.SOURCE_MLX) == []

    def test_derinlik_siniri(self, mt_isolation, fake_mlx_lm):
        root = mt_isolation.models_dir
        inside = root.joinpath(*["d"] * (DISCOVERY_MAX_DEPTH - 1), "Icerde-MLX")
        too_deep = root.joinpath(*["e"] * DISCOVERY_MAX_DEPTH, "Derin-MLX")
        write_fake_mlx_dir(inside, "llama")
        write_fake_mlx_dir(too_deep, "llama")
        ids = [m.engine_id for m in _of(discovery.discover_all(), contracts.SOURCE_MLX)]
        assert ids == [f"mlx:{inside}"]

    def test_kayitli_motorun_klasoru_tekrar_listelenmez(self, mt_isolation, fake_mlx_lm):
        local_dir = registry.BUILTIN_SPECS[registry.DEFAULT_ENGINE].local_dir
        real = write_fake_mlx_dir(mt_isolation.models_dir / f"{local_dir}.nosync",
                                  "hunyuan_v1_dense")
        (mt_isolation.models_dir / local_dir).symlink_to(real, target_is_directory=True)
        report = discovery.discover_all()
        builtin = report.model_by_id(registry.DEFAULT_ENGINE)
        assert builtin.source == contracts.SOURCE_BUILTIN and builtin.usable
        assert builtin.detail == "mimari: hunyuan_v1_dense"
        assert _of(report, contracts.SOURCE_MLX) == []

    def test_hymt_adindan_bicim_tahmini(self, mt_isolation, fake_mlx_lm):
        write_fake_mlx_dir(mt_isolation.models_dir / "HY-MT1.5-1.8B-mlx", "hunyuan_v1_dense")
        model = discovery.discover_all().model_by_id("mlx:HY-MT1.5-1.8B-mlx")
        assert model.format_guess == contracts.FORMAT_HYMT

    def test_dusunen_model_belirtisi(self, mt_isolation, fake_mlx_lm):
        write_fake_mlx_dir(mt_isolation.models_dir / "dusunen-mlx", "qwen3",
                           chat_template=_THINK_TEMPLATE)
        write_fake_mlx_dir(mt_isolation.models_dir / "sade-mlx", "qwen3")
        report = discovery.discover_all()
        assert report.model_by_id("mlx:dusunen-mlx").reasoning is True
        assert report.model_by_id("mlx:sade-mlx").reasoning is None

    def test_derindeki_klasor_kimligi_geri_cozulur(self, mt_isolation, fake_mlx_lm):
        path = write_fake_mlx_dir(mt_isolation.models_dir / "org" / "Model MLX", "qwen3")
        model = _of(discovery.discover_all(), contracts.SOURCE_MLX)[0]
        assert model.engine_id == f"mlx:{path}"      # boşluklu ad: mutlak yol kimliği
        assert Path(registry.resolve_spec(model.engine_id).path) == path
        assert discovery.check_engine(model.engine_id).ok


class TestMlxMimariDenetimi:
    def test_mlx_lm_duzeni_ice_aktarmadan_okunur(self, tmp_path, monkeypatch):
        pkg = tmp_path / "mlx_lm"
        (pkg / "models" / "llama").mkdir(parents=True)
        (pkg / "models" / "llama" / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "models" / "qwen3.py").write_text("", encoding="utf-8")
        (pkg / "utils.py").write_text(
            "raise ImportError('içe aktarılmamalı')\n"
            "MODEL_REMAPPING = {'mistral': 'llama', 'qwen3_moe_x': 'qwen3'}\n",
            encoding="utf-8")

        class _Spec:
            submodule_search_locations = [str(pkg)]

        import importlib.util as ilu
        real_find_spec = ilu.find_spec
        monkeypatch.setattr(ilu, "find_spec",
                            lambda name, *a: _Spec() if name == "mlx_lm"
                            else real_find_spec(name, *a))
        discovery._mlx_lm_layout.cache_clear()
        try:
            assert discovery.mlx_arch_supported("llama") is True
            assert discovery.mlx_arch_supported("mistral") is True
            assert discovery.mlx_arch_supported("qwen3_moe_x") is True
            assert discovery.mlx_arch_supported("gemma4") is False
            assert discovery.mlx_arch_supported("../etc") is False
            assert discovery.mlx_arch_supported("") is False
        finally:
            discovery._mlx_lm_layout.cache_clear()


# ===========================================================================
# 6. GGUF dosyaları
# ===========================================================================

class TestGgufKesfi:
    def test_model_llama_yokken_neden_ve_kurulum_komutu(self, mt_isolation):
        path = write_fake_gguf(mt_isolation.models_dir / "Qwen3-8B-Q4_K_M.gguf", "qwen3",
                               size_label="8B", pad_bytes=1000)
        report = discovery.discover_all()
        model = report.model_by_id("gguf:Qwen3-8B-Q4_K_M.gguf")
        assert model.usable is False
        assert model.reason == contracts.MSG_LLAMA_MISSING
        assert contracts.LLAMA_CPP_INSTALL_CMD in model.reason
        assert model.detail == "8B · Q4_K_M"
        assert model.size_bytes == path.stat().st_size
        assert model.backend == contracts.BACKEND_LLAMACPP
        assert model.label == "Qwen3-8B-Q4_K_M"
        status = _status(report, contracts.SOURCE_GGUF)
        assert status.message.startswith("1 dosya")
        assert contracts.LLAMA_CPP_INSTALL_CMD in status.message

    def test_llama_varken_kullanilabilir(self, mt_isolation, fake_llama):
        write_fake_gguf(mt_isolation.models_dir / "Qwen3-8B-Q4_K_M.gguf", "qwen3")
        report = discovery.discover_all()
        model = report.model_by_id("gguf:Qwen3-8B-Q4_K_M.gguf")
        assert model.usable and model.reason == ""
        assert _status(report, contracts.SOURCE_GGUF).message == "1 dosya"
        assert discovery.check_engine(model.engine_id).ok

    def test_projektor_listelenmez(self, mt_isolation, fake_llama):
        root = mt_isolation.models_dir
        write_fake_gguf(root / "mmproj-Qwen3-F16.gguf", "qwen3")               # ad
        write_fake_gguf(root / "gorsel-projektor.gguf", "qwen3", type_="mmproj")  # başlık
        write_fake_gguf(root / "eski-clip.gguf", "clip", type_=None)            # mimari
        write_fake_gguf(root / "Asil-Model-Q8_0.gguf", "llama")
        ids = [m.engine_id for m in _of(discovery.discover_all(), contracts.SOURCE_GGUF)]
        assert ids == ["gguf:Asil-Model-Q8_0.gguf"]

    def test_parcali_dosya_tek_kayit_toplam_boyut(self, mt_isolation, fake_llama):
        parts = write_fake_gguf_split(mt_isolation.models_dir / "buyuk", "Buyuk-Q8_0", 3,
                                      "llama", pad_bytes=500)
        models = _of(discovery.discover_all(), contracts.SOURCE_GGUF)
        assert len(models) == 1
        model = models[0]
        assert model.engine_id == f"gguf:{parts[0]}"
        assert model.size_bytes == sum(p.stat().st_size for p in parts)
        assert model.label == "Buyuk-Q8_0" and model.detail == "Q8_0"
        assert model.usable

    def test_eksik_parca_nedeni(self, mt_isolation, fake_llama):
        parts = write_fake_gguf_split(mt_isolation.models_dir, "Eksik-Q4_0", 3, "llama")
        parts[2].unlink()
        model = _of(discovery.discover_all(), contracts.SOURCE_GGUF)[0]
        assert model.usable is False
        assert "3 parçasından 2 tanesi var" in model.reason

    @pytest.mark.parametrize("kind", ["magic", "truncated", "kv_count", "empty"])
    def test_bozuk_baslik(self, mt_isolation, fake_llama, kind):
        write_corrupt_gguf(mt_isolation.models_dir / "bozuk.gguf", kind)
        model = discovery.discover_all().model_by_id("gguf:bozuk.gguf")
        assert model is not None and model.usable is False
        assert model.reason.startswith("Geçerli bir GGUF model dosyası değil")

    def test_dusunen_sablon_ve_hymt_bicimi(self, mt_isolation, fake_llama):
        write_fake_gguf(mt_isolation.models_dir / "Dusunen-Q4_K_M.gguf", "qwen3",
                        chat_template=_THINK_TEMPLATE)
        write_fake_gguf(mt_isolation.models_dir / "HY-MT1.5-7B-Q8_0.gguf", "hunyuan-dense")
        report = discovery.discover_all()
        assert report.model_by_id("gguf:Dusunen-Q4_K_M.gguf").reasoning is True
        hymt = report.model_by_id("gguf:HY-MT1.5-7B-Q8_0.gguf")
        assert hymt.format_guess == contracts.FORMAT_HYMT and hymt.reasoning is None

    def test_derindeki_dosya_kimligi_geri_cozulur(self, mt_isolation, fake_llama):
        path = write_fake_gguf(mt_isolation.models_dir / "org" / "M-Q4_K_M.gguf", "llama")
        model = _of(discovery.discover_all(), contracts.SOURCE_GGUF)[0]
        assert model.engine_id == f"gguf:{path}"
        assert Path(registry.resolve_spec(model.engine_id).path) == path

    def test_baslik_okuma_onbellegi_degisikligi_gorur(self, tmp_path):
        path = write_fake_gguf(tmp_path / "m.gguf", "llama", size_label="1B")
        assert discovery.read_gguf_meta(path).size_label == "1B"
        write_fake_gguf(path, "qwen3", size_label="8B", pad_bytes=64)
        os.utime(path, ns=(time.time_ns(), time.time_ns() + 10_000_000))
        meta = discovery.read_gguf_meta(path)
        assert (meta.architecture, meta.size_label) == ("qwen3", "8B")


# ===========================================================================
# 7. Ollama
# ===========================================================================

class TestOllamaKesfi:
    def test_modeller_ayrinti_yuklu_ve_gomme(self, fake_llm_server, monkeypatch, mt_isolation):
        srv = fake_llm_server("ollama", models=[
            FakeModel("qwen3:8b", thinking=True, loaded=True),
            FakeModel("gemma3:4b", parameter_size="4.3B", quantization="Q4_0",
                      size_bytes=3_300_000_000),
            FakeModel("nomic-embed-text:latest", kind="embedding"),
        ])
        _point_sources(monkeypatch, mt_isolation, ollama=srv.url)
        report = discovery.discover_all()
        models = {m.engine_id: m for m in _of(report, contracts.SOURCE_OLLAMA)}
        assert list(models) == ["ollama:qwen3:8b", "ollama:gemma3:4b",
                                "ollama:nomic-embed-text:latest"]
        qwen = models["ollama:qwen3:8b"]
        assert qwen.usable and qwen.detail == "8.2B · Q4_K_M"
        assert qwen.reasoning is True and qwen.loaded is True
        assert qwen.size_bytes == 5_234_000_000
        assert qwen.backend == contracts.BACKEND_OLLAMA
        assert qwen.format_guess == contracts.FORMAT_CHAT and qwen.location == srv.url
        gemma = models["ollama:gemma3:4b"]
        assert gemma.detail == "4.3B · Q4_0" and gemma.loaded is False
        assert gemma.reasoning is False
        embed = models["ollama:nomic-embed-text:latest"]
        assert embed.usable is False and embed.reason == contracts.MSG_EMBEDDING_MODEL
        status = _status(report, contracts.SOURCE_OLLAMA)
        assert status.reachable is True and status.message == "3 model"
        # Keşif hiçbir modeli yüklemez: üretim / yükleme ucu çağrılmaz.
        assert _generation_paths(srv) == []

    @pytest.mark.parametrize("installed,template", [
        (False, contracts.MSG_OLLAMA_DOWN),
        (True, contracts.MSG_OLLAMA_INSTALLED_DOWN),
    ])
    def test_sunucu_kapaliyken_sifir_model(self, monkeypatch, mt_isolation, installed,
                                           template):
        monkeypatch.setattr(discovery, "_ollama_installed", lambda: installed)
        report = discovery.discover_all()
        assert _of(report, contracts.SOURCE_OLLAMA) == []
        status = _status(report, contracts.SOURCE_OLLAMA)
        assert status.reachable is False
        assert status.message == template.format(url=mt_isolation.closed_url)

    def test_kapali_kaynak_yoklanmaz(self, fake_llm_server, monkeypatch, mt_isolation):
        srv = fake_llm_server("ollama")
        _point_sources(monkeypatch, mt_isolation, ollama=srv.url, ollama_enabled=False)
        report = discovery.discover_all()
        assert srv.requests == []
        assert all(s.source != contracts.SOURCE_OLLAMA for s in report.sources)


# ===========================================================================
# 8. LM Studio ve OpenAI uyumlu sunucular
# ===========================================================================

class TestLmStudioKesfi:
    def _models(self):
        return [FakeModel("qwen/qwen3-8b", thinking=True, format="mlx", loaded=True),
                FakeModel("google/gemma-3-4b"),
                FakeModel("text-embedding-nomic", kind="embedding")]

    def test_rest_v1_zenginlestirmesi(self, fake_llm_server, monkeypatch, mt_isolation):
        srv = fake_llm_server("lmstudio", models=self._models())
        _point_sources(monkeypatch, mt_isolation, lmstudio=srv.base_url)
        report = discovery.discover_all()
        models = {m.engine_id: m for m in _of(report, contracts.SOURCE_LMSTUDIO)}
        qwen = models["lmstudio:qwen/qwen3-8b"]
        assert qwen.usable and qwen.detail == "8.2B · Q4_K_M · MLX"
        assert qwen.reasoning is True and qwen.loaded is True
        assert qwen.backend == contracts.BACKEND_OPENAI
        gemma = models["lmstudio:google/gemma-3-4b"]
        assert gemma.reasoning is False and gemma.loaded is False
        embed = models["lmstudio:text-embedding-nomic"]
        assert embed.usable is False and embed.reason == contracts.MSG_EMBEDDING_MODEL
        assert _status(report, contracts.SOURCE_LMSTUDIO).message == "3 model"
        assert _generation_paths(srv) == []

    def test_v1_yoksa_v0(self, fake_llm_server, monkeypatch, mt_isolation):
        srv = fake_llm_server("lmstudio", models=self._models())
        srv.set_status("/api/v1/models", 404)
        _point_sources(monkeypatch, mt_isolation, lmstudio=srv.base_url)
        report = discovery.discover_all()
        qwen = report.model_by_id("lmstudio:qwen/qwen3-8b")
        assert qwen.detail == "Q4_K_M · mlx"
        assert qwen.reasoning is None and qwen.loaded is True
        assert report.model_by_id("lmstudio:text-embedding-nomic").usable is False

    def test_rest_yoksa_yalniz_kimlikler(self, fake_llm_server, monkeypatch, mt_isolation):
        srv = fake_llm_server("lmstudio", models=self._models())
        srv.set_status("/api/v1/models", 404)
        srv.set_status("/api/v0/models", 404)
        _point_sources(monkeypatch, mt_isolation, lmstudio=srv.base_url)
        models = _of(discovery.discover_all(), contracts.SOURCE_LMSTUDIO)
        assert len(models) == 3
        assert all(m.detail == "" and m.reasoning is None and m.loaded is None
                   for m in models)

    def test_jit_kapaliyken_yuklu_olmayan_model(self, fake_llm_server, monkeypatch,
                                               mt_isolation):
        srv = fake_llm_server("lmstudio", models=self._models(), jit=False)
        _point_sources(monkeypatch, mt_isolation, lmstudio=srv.base_url)
        report = discovery.discover_all()
        assert report.model_by_id("lmstudio:qwen/qwen3-8b").usable
        gemma = report.model_by_id("lmstudio:google/gemma-3-4b")
        assert gemma.usable is False
        assert gemma.reason == contracts.MSG_LMSTUDIO_NOT_LOADED.format(
            model="google/gemma-3-4b")

    def test_kapali(self, mt_isolation):
        report = discovery.discover_all()
        assert _of(report, contracts.SOURCE_LMSTUDIO) == []
        status = _status(report, contracts.SOURCE_LMSTUDIO)
        assert status.reachable is False
        assert status.message == contracts.MSG_LMSTUDIO_DOWN.format(
            url=mt_isolation.closed_url + "/v1")


class TestOpenAISunuculari:
    def test_yalniz_yazilan_sunucu_yoklanir(self, fake_openai, monkeypatch, mt_isolation):
        server = contracts.OpenAIServer(name="llama.cpp", url=fake_openai.base_url)
        _point_sources(monkeypatch, mt_isolation, openai=[server])
        report = discovery.discover_all()
        ident = contracts.build_engine_id("openai", "qwen", fake_openai.base_url)
        model = report.model_by_id(ident)
        assert model is not None and model.usable and model.source == contracts.SOURCE_OPENAI
        status = _status(report, contracts.SOURCE_OPENAI)
        assert status.label == f"llama.cpp ({fake_openai.host_port})"
        assert status.message == "1 model"
        assert registry.resolve_spec(ident).base_url == fake_openai.base_url

    def test_kapali_ve_openai_olmayan(self, fake_ollama, monkeypatch, mt_isolation):
        closed = mt_isolation.closed_url + "/v1"
        not_openai = fake_ollama.url + "/v1"       # Ollama kökü: /v1/models yok (404)
        _point_sources(monkeypatch, mt_isolation, openai=[
            contracts.OpenAIServer(name="kapali", url=closed),
            contracts.OpenAIServer(name="baska", url=not_openai)])
        report = discovery.discover_all()
        assert _of(report, contracts.SOURCE_OPENAI) == []
        by_url = {s.location: s for s in report.sources if s.source == contracts.SOURCE_OPENAI}
        assert by_url[closed].reachable is False
        assert by_url[closed].message == contracts.MSG_OPENAI_DOWN.format(url=closed)
        assert by_url[not_openai].reachable is True
        assert by_url[not_openai].message == contracts.MSG_NOT_OPENAI.format(url=not_openai)


# ===========================================================================
# Kayıtlı motorlar ve kaynak yapılandırması
# ===========================================================================

class TestKayitliMotorlar:
    def test_yerlesik_her_zaman_listede(self, mt_isolation):
        report = discovery.discover_all()
        assert report.models[0].engine_id == registry.DEFAULT_ENGINE
        builtin = report.models[0]
        assert builtin.usable is False
        assert builtin.reason.startswith("Çeviri modeli kurulu değil")
        assert "mlx_lm.convert" in builtin.reason

    def test_kullanici_sunucu_kaydi_ayni_taramadan(self, fake_llm_server, monkeypatch,
                                                  mt_isolation):
        srv = fake_llm_server("ollama", models=["qwen3:latest"])
        mt_isolation.user_specs_path.write_text(json.dumps({
            "qwen-kayit": {"backend": "ollama", "model": "qwen3", "base_url": srv.url},
            "yok-kayit": {"backend": "ollama", "model": "olmayan", "base_url": srv.url},
        }), encoding="utf-8")
        report = discovery.discover_all()
        ok = report.model_by_id("qwen-kayit")
        assert ok.source == contracts.SOURCE_USER and ok.usable       # qwen3 ≡ qwen3:latest
        missing = report.model_by_id("yok-kayit")
        assert missing.usable is False
        assert missing.reason == contracts.MSG_MODEL_NOT_ON_SERVER.format(
            model="olmayan", server="Ollama")
        # Varsayılan (kapalı) Ollama kaynağının durum satırı ayrıca durur;
        # kaydın sunucusu ek kaynak satırı üretmez.
        assert [s.location for s in report.sources
                if s.source == contracts.SOURCE_OLLAMA] == [mt_isolation.closed_url]

    def test_sira_ve_kaynaklar(self, mt_isolation, fake_mlx_lm, fake_llama):
        write_fake_mlx_dir(mt_isolation.models_dir / "a-mlx", "llama")
        write_fake_gguf(mt_isolation.models_dir / "b.gguf", "llama")
        report = discovery.discover_all()
        order = [m.source for m in report.models]
        assert order == [contracts.SOURCE_BUILTIN, contracts.SOURCE_MLX, contracts.SOURCE_GGUF]
        assert [s.source for s in report.sources] == [
            contracts.SOURCE_MLX, contracts.SOURCE_GGUF, contracts.SOURCE_OLLAMA,
            contracts.SOURCE_LMSTUDIO]
        assert abs(report.scanned_at - time.time()) < 60

    def test_acik_models_dir_parametresi(self, tmp_path, fake_mlx_lm):
        other = tmp_path / "baska_models"
        write_fake_mlx_dir(other / "x-mlx", "llama")
        report = discovery.discover_all(models_dir=other)
        assert report.model_by_id("mlx:x-mlx") is not None


class TestKaynakYapilandirmasi:
    def test_openai_sunuculari_ve_devre_disi(self, tmp_path, mt_isolation):
        path = tmp_path / contracts.MT_SOURCES_FILENAME
        path.write_text(json.dumps({
            "_aciklama": "yorum",
            "ollama": {"url": "http://127.0.0.1:11435", "enabled": False},
            "openai_servers": [{"name": "llama.cpp", "url": "http://127.0.0.1:8080"},
                               {"url": "http://localhost:8000/v1/"},
                               {"name": "tekrar", "url": "http://127.0.0.1:8080/v1"}],
        }), encoding="utf-8")
        cfg = discovery.load_sources(path)
        assert cfg.ollama_url == "http://127.0.0.1:11435" and cfg.ollama_enabled is False
        assert cfg.lmstudio_enabled is True
        assert [(s.name, s.url) for s in cfg.openai_servers] == [
            ("llama.cpp", "http://127.0.0.1:8080/v1"),
            ("localhost:8000", "http://localhost:8000/v1")]

    def test_discover_all_acik_kaynak_dosyasi(self, tmp_path, fake_openai, mt_isolation):
        path = tmp_path / contracts.MT_SOURCES_FILENAME
        path.write_text(json.dumps({
            "ollama": {"enabled": False}, "lmstudio": {"enabled": False},
            "openai_servers": [{"name": "yerel", "url": fake_openai.base_url}]}),
            encoding="utf-8")
        report = discovery.discover_all(sources_path=path)
        assert [s.source for s in report.sources] == [
            contracts.SOURCE_MLX, contracts.SOURCE_GGUF, contracts.SOURCE_OPENAI]
        assert len(_of(report, contracts.SOURCE_OPENAI)) == 1

    def test_izinli_kok_yalniz_models(self, mt_isolation):
        roots = discovery.allowed_roots()
        assert Path(mt_isolation.models_dir) in roots or \
            Path(mt_isolation.models_dir).resolve() in roots
        assert len(roots) <= 2
        assert all(root.name == Path(mt_isolation.models_dir).name for root in roots)


# ===========================================================================
# 9. Zaman
# ===========================================================================

class TestZaman:
    def test_yavas_sunucular_paralel_ve_kisa_zaman_asimi(self, fake_llm_server, monkeypatch,
                                                         mt_isolation):
        slow_ollama = fake_llm_server("ollama")
        slow_ollama.set_delay("/api/tags", 3.0)
        slow_lms = fake_llm_server("lmstudio")
        slow_lms.set_delay("/v1/models", 3.0)
        slow_lms.set_delay("/api/v1/models", 3.0)
        _point_sources(monkeypatch, mt_isolation, ollama=slow_ollama.url,
                       lmstudio=slow_lms.base_url)
        start = time.monotonic()
        report = discovery.discover_all(http_timeout=0.2)
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, elapsed
        assert _status(report, contracts.SOURCE_OLLAMA).reachable is False
        assert _status(report, contracts.SOURCE_LMSTUDIO).reachable is False
        assert _of(report, contracts.SOURCE_OLLAMA) == []
        assert _of(report, contracts.SOURCE_LMSTUDIO) == []

    def test_check_engine_zaman_asimi(self, fake_llm_server, monkeypatch, mt_isolation):
        srv = fake_llm_server("ollama")
        srv.set_delay("/api/tags", 3.0)
        _point_sources(monkeypatch, mt_isolation, ollama=srv.url)
        start = time.monotonic()
        check = discovery.check_engine("ollama:qwen3:8b", http_timeout=0.2)
        assert time.monotonic() - start < 1.0
        assert check.ok is False and "Ollama" in check.reason


# ===========================================================================
# 10. check_engine
# ===========================================================================

class TestCheckEngine:
    def test_yerlesik_model_yok(self, mt_isolation):
        check = discovery.check_engine(registry.DEFAULT_ENGINE)
        assert check.ok is False and check.engine_id == registry.DEFAULT_ENGINE
        local_dir = registry.BUILTIN_SPECS[registry.DEFAULT_ENGINE].local_dir
        assert check.reason == contracts.MSG_BUILTIN_MISSING.format(
            path=Path(os.path.abspath(mt_isolation.models_dir)) / local_dir)
        assert check.hint.startswith("mlx_lm.convert")
        assert check.label.endswith("(MLX)")

    def test_yerlesik_model_varsa_hazir(self, mt_isolation, fake_mlx_lm):
        local_dir = registry.BUILTIN_SPECS[registry.DEFAULT_ENGINE].local_dir
        write_fake_mlx_dir(mt_isolation.models_dir / local_dir, "hunyuan_v1_dense")
        check = discovery.check_engine(registry.DEFAULT_ENGINE)
        assert check.ok and check.reason == "" and check.hint == ""

    def test_mlx_kimlikleri(self, mt_isolation, fake_mlx_lm):
        root = mt_isolation.models_dir
        write_fake_mlx_dir(root / "iyi-mlx", "qwen3")
        write_fake_mlx_dir(root / "eksik-mlx", "qwen3", safetensors=False)
        write_fake_mlx_dir(root / "yeni-mimari-mlx", "gemma4")
        assert discovery.check_engine("mlx:iyi-mlx").ok
        missing = discovery.check_engine("mlx:olmayan-mlx")
        assert missing.reason.startswith("MLX model klasörü bulunamadı")
        assert discovery.check_engine("mlx:eksik-mlx").reason.startswith(
            "MLX klasöründe model dosyaları eksik")
        assert discovery.check_engine("mlx:yeni-mimari-mlx").reason == \
            contracts.MSG_MLX_ARCH_UNSUPPORTED.format(arch="gemma4")

    def test_gguf_kimlikleri(self, mt_isolation):
        root = mt_isolation.models_dir
        write_fake_gguf(root / "m.gguf", "llama")
        write_fake_gguf(root / "p.gguf", "clip", type_="mmproj")
        assert discovery.check_engine("gguf:yok.gguf").reason == \
            contracts.MSG_GGUF_MISSING.format(path=root / "yok.gguf")
        assert discovery.check_engine("gguf:m.gguf").reason == contracts.MSG_LLAMA_MISSING
        assert "görsel projektör" in discovery.check_engine("gguf:p.gguf").reason

    def test_gguf_llama_varken(self, mt_isolation, fake_llama):
        write_fake_gguf(mt_isolation.models_dir / "m.gguf", "llama")
        check = discovery.check_engine("gguf:m.gguf")
        assert check.ok and check.label == "m.gguf (llama.cpp)"
        # Model yüklenmez: sahte Llama hiç kurulmadı.
        assert fake_llama.instances == []

    def test_ollama(self, fake_llm_server, monkeypatch, mt_isolation):
        srv = fake_llm_server("ollama", models=["qwen3:latest", "gemma3:4b"])
        _point_sources(monkeypatch, mt_isolation, ollama=srv.url)
        assert discovery.check_engine("ollama:qwen3").ok                 # ≡ qwen3:latest
        assert discovery.check_engine("ollama:gemma3:4b").ok
        assert discovery.check_engine("ollama:gemma3:4b:latest").ok is False
        miss = discovery.check_engine("ollama:olmayan:1b")
        assert miss.reason == contracts.MSG_MODEL_NOT_ON_SERVER.format(
            model="olmayan:1b", server="Ollama")
        assert miss.label == "olmayan:1b (Ollama)"
        # Yalnız liste + üst veri uçları: model yüklenmez / ısıtılmaz.
        # (Vk 2026-09-28: /api/show eklendi — keşifle aynı hükmü verip gömme
        # modelini ön kontrolde yakalamak için; /api/show modeli yüklemez.)
        assert {p for _m, p, _b in srv.requests} <= {"/api/tags", "/api/show"}
        assert _generation_paths(srv) == []

    def test_ollama_gomme_modeli_kesifle_ayni_hukum(self, fake_llm_server, monkeypatch,
                                                     mt_isolation):
        # Vk 2026-09-28: keşif gömme modelini "kullanılamaz" diyordu, ön kontrol
        # "hazır" diyordu -> CLI çıkarımı bitirip ancak yüklemede 5 veriyordu.
        srv = fake_llm_server("ollama", models=[
            "qwen3:8b", FakeModel("gomme-modeli", kind="embedding")])
        _point_sources(monkeypatch, mt_isolation, ollama=srv.url)
        check = discovery.check_engine("ollama:gomme-modeli")
        assert check.ok is False and check.reason == contracts.MSG_EMBEDDING_MODEL
        report = discovery.discover_all()
        model = report.model_by_id("ollama:gomme-modeli")
        assert model is not None and model.usable is False
        assert discovery.check_engine("ollama:qwen3:8b").ok
        assert _generation_paths(srv) == []

    def test_ollama_show_yanitsizsa_engel_degil(self, fake_llm_server, monkeypatch,
                                                mt_isolation):
        srv = fake_llm_server("ollama", models=["qwen3:8b"])
        srv.set_status("/api/show", 500)
        _point_sources(monkeypatch, mt_isolation, ollama=srv.url)
        assert discovery.check_engine("ollama:qwen3:8b").ok

    def test_ollama_show_sure_butcesi(self, fake_llm_server, monkeypatch, mt_isolation):
        srv = fake_llm_server("ollama", models=["qwen3:8b"])
        srv.set_delay("/api/show", 3.0)
        _point_sources(monkeypatch, mt_isolation, ollama=srv.url)
        start = time.monotonic()
        check = discovery.check_engine("ollama:qwen3:8b", http_timeout=0.4)
        assert time.monotonic() - start < 1.0      # toplam bütçe aşılmaz
        assert check.ok                             # üst veri alınamadı: engel değil

    def test_ollama_latest_ters_yon(self, fake_llm_server, monkeypatch, mt_isolation):
        srv = fake_llm_server("ollama", models=["qwen3"])
        _point_sources(monkeypatch, mt_isolation, ollama=srv.url)
        assert discovery.check_engine("ollama:qwen3:latest").ok

    def test_ollama_kapali(self, monkeypatch, mt_isolation):
        monkeypatch.setattr(discovery, "_ollama_installed", lambda: False)
        check = discovery.check_engine("ollama:qwen3:8b")
        assert check.ok is False
        assert check.reason == contracts.MSG_OLLAMA_DOWN.format(url=mt_isolation.closed_url)

    def test_lmstudio(self, fake_llm_server, monkeypatch, mt_isolation):
        srv = fake_llm_server("lmstudio", models=[
            FakeModel("qwen/qwen3-8b", loaded=True), FakeModel("pub/yuklu-degil")], jit=False)
        _point_sources(monkeypatch, mt_isolation, lmstudio=srv.base_url)
        assert discovery.check_engine("lmstudio:qwen/qwen3-8b").ok
        not_loaded = discovery.check_engine("lmstudio:pub/yuklu-degil")
        assert not_loaded.reason == contracts.MSG_LMSTUDIO_NOT_LOADED.format(
            model="pub/yuklu-degil")
        assert _generation_paths(srv) == []

    def test_lmstudio_gomme_modeli_kesifle_ayni_hukum(self, fake_llm_server, monkeypatch,
                                                       mt_isolation):
        srv = fake_llm_server("lmstudio", models=[
            FakeModel("qwen/qwen3-8b", loaded=True),
            FakeModel("pub/gomme", kind="embedding", loaded=True)])
        _point_sources(monkeypatch, mt_isolation, lmstudio=srv.base_url)
        check = discovery.check_engine("lmstudio:pub/gomme")
        assert check.ok is False and check.reason == contracts.MSG_EMBEDDING_MODEL
        model = discovery.discover_all().model_by_id("lmstudio:pub/gomme")
        assert model is not None and model.usable is False
        assert discovery.check_engine("lmstudio:qwen/qwen3-8b").ok
        assert _generation_paths(srv) == []

    def test_lmstudio_kapali(self, mt_isolation):
        check = discovery.check_engine("lmstudio:qwen/qwen3-8b")
        assert check.reason == contracts.MSG_LMSTUDIO_DOWN.format(
            url=mt_isolation.closed_url + "/v1")

    def test_openai(self, fake_openai, mt_isolation):
        base = fake_openai.base_url
        assert discovery.check_engine(f"openai:{base}|qwen").ok
        miss = discovery.check_engine(f"openai:{base}|olmayan")
        assert miss.reason == contracts.MSG_MODEL_NOT_ON_SERVER.format(
            model="olmayan", server=f"OpenAI uyumlu sunucu ({fake_openai.host_port})")
        closed = mt_isolation.closed_url + "/v1"
        assert discovery.check_engine(f"openai:{closed}|qwen").reason == \
            contracts.MSG_OPENAI_DOWN.format(url=closed)

    def test_openai_olmayan_adres(self, fake_ollama):
        base = fake_ollama.url + "/v1"
        assert discovery.check_engine(f"openai:{base}|qwen").reason == \
            contracts.MSG_NOT_OPENAI.format(url=base)

    @pytest.mark.parametrize("engine_id,fragment", [
        ("olmayan-motor", "Desteklenmeyen MT motoru"),
        ("mlx:../x", "Geçersiz model kimliği"),
        ("mlx:/etc", "izinli klasörlerin dışında"),
        ("openai:http://192.168.1.2:8080/v1|m", "yerel bir adres değil"),
        ("ollama:", "Geçersiz model kimliği"),
    ])
    def test_gecersiz_kimlik_istisnasiz(self, engine_id, fragment):
        check = discovery.check_engine(engine_id)
        assert check.ok is False and fragment in check.reason
        assert check.engine_id == engine_id

    def test_kullanici_kaydi(self, fake_llm_server, mt_isolation):
        srv = fake_llm_server("openai", models=["benim-model"])
        mt_isolation.user_specs_path.write_text(json.dumps({
            "sunucum": {"backend": "openai", "base_url": srv.base_url,
                        "model": "benim-model"}}), encoding="utf-8")
        assert discovery.check_engine("sunucum").ok


# ===========================================================================
# D.4 komut satırı
# ===========================================================================

class TestKomutSatiri:
    def test_json_iki_sunucu_kapaliyken(self, capsys, mt_isolation):
        start = time.monotonic()
        assert discovery.main(["--json"]) == 0
        elapsed = time.monotonic() - start
        assert elapsed <= 3.0, elapsed
        data = json.loads(capsys.readouterr().out)
        assert set(data) == {"scanned_at", "models", "sources"}
        assert [m["engine_id"] for m in data["models"]] == [registry.DEFAULT_ENGINE]
        server_sources = {contracts.SOURCE_OLLAMA, contracts.SOURCE_LMSTUDIO,
                          contracts.SOURCE_OPENAI}
        assert not any(m["source"] in server_sources for m in data["models"])
        by_source = {s["source"]: s for s in data["sources"]}
        assert by_source["ollama"]["reachable"] is False
        assert by_source["lmstudio"]["reachable"] is False
        model_keys = set(data["models"][0])
        assert model_keys == {"engine_id", "label", "source", "backend", "format_guess",
                              "usable", "reason", "size_bytes", "detail", "location",
                              "reasoning", "loaded"}

    def test_json_alt_surecte_uc_saniyeden_kisa(self, tmp_path):
        """``python -m core.mt_engines.discovery --json`` gerçek süreçte (≤ 3 sn).

        Gerçek models/ ve config/ okunmasın diye model kökü ve kayıt dosyası
        alt süreçte tmp'ye yönlendirilir; iki sunucu adresi kapalı porttur.
        """
        port = closed_local_port()
        sources = tmp_path / contracts.MT_SOURCES_FILENAME
        sources.write_text(json.dumps({
            "ollama": {"url": f"http://127.0.0.1:{port}"},
            "lmstudio": {"url": f"http://127.0.0.1:{port}/v1"}}), encoding="utf-8")
        models = tmp_path / "models"
        models.mkdir()
        code = (
            "import runpy, sys\n"
            "from pathlib import Path\n"
            "from core.mt_engines import registry\n"
            f"registry.MODELS_DIR = Path({str(models)!r})\n"
            f"registry.USER_SPECS_PATH = Path({str(tmp_path / 'yok.json')!r})\n"
            "sys.modules['llama_cpp'] = None\n"
            f"sys.argv = ['discovery', '--json', '--sources', {str(sources)!r}]\n"
            "runpy.run_module('core.mt_engines.discovery', run_name='__main__')\n"
        )
        start = time.monotonic()
        proc = subprocess.run([sys.executable, "-c", code], cwd=str(PROJECT_ROOT),
                              capture_output=True, text=True, timeout=30)
        elapsed = time.monotonic() - start
        assert proc.returncode == 0, proc.stderr
        assert elapsed <= 3.0, elapsed
        data = json.loads(proc.stdout)
        assert [m["source"] for m in data["models"]] == [contracts.SOURCE_BUILTIN]
        assert {s["source"]: s["reachable"] for s in data["sources"]
                if s["reachable"] is not None} == {"ollama": False, "lmstudio": False}

    def test_hafif_ice_aktarma(self):
        code = ("import sys, core.mt_engines.discovery\n"
                "heavy = ('mlx', 'mlx_lm', 'torch', 'llama_cpp', 'requests', 'httpx', "
                "'openai', 'transformers')\n"
                "print([m for m in heavy if m in sys.modules])\n")
        proc = subprocess.run([sys.executable, "-c", code], cwd=str(PROJECT_ROOT),
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "[]"

    def test_turkce_tablo(self, capsys, mt_isolation, fake_mlx_lm):
        write_fake_mlx_dir(mt_isolation.models_dir / "gemma4-mlx", "gemma4")
        assert discovery.main([]) == 0
        out = capsys.readouterr().out
        assert "Kaynaklar:" in out and "Modeller:" in out
        assert "MLX klasörleri: 1 klasör" in out
        assert "[MLX] mlx:gemma4-mlx" in out
        assert "kullanılamaz: " + contracts.MSG_MLX_ARCH_UNSUPPORTED.format(arch="gemma4") in out
        assert "--mt-engine <kimlik>" in out
