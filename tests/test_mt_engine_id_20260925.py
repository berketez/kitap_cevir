"""Motor kimliği sözdizimi ve çözümü (K2, TASARIM C; KARARLAR 1-2, 13).

Kapsam (TASARIM H.3 testleri 1-4):
    1. Kimlik tablosu: geçerli önekli kimlikler ayrışır; geçersizler Türkçe
       iletiyle ``ValueError`` verir (``../``, izinli kök dışı, ``.gguf``
       olmayan dosya, uzak adres, ayraçsız ``openai:``, bilinmeyen önek, boş
       ya da denetim karakterli model).
    2. ``ollama:qwen3:8b`` -> model ``qwen3:8b``; ``lmstudio:`` / ``ollama:``
       adresi ``config/mt_sources.json``'dan; mt_sources.json uzak adresi
       açamaz ve model klasörü ekleyemez (KARARLAR 1-2).
    3. ``config/mt_engines.json`` yeni alanları; ``_aciklama`` sessiz atlanır;
       ':' içeren ad reddedilir; yerleşik motor ezilemez.
    4. Çözüm sırası: yerleşik -> kullanıcı kaydı -> önekli kimlik; kayıt adı
       önekli kimlikle çakışamaz.

Yalıtım: tests/conftest.py ``_mt_isolation`` (autouse) ``registry.MODELS_DIR``
boş bir tmp klasöre, ``USER_SPECS_PATH`` var olmayan bir tmp dosyaya ve
``discovery.load_sources()`` kapalı bir yerel porta işaret eder. Hiçbir test
ağa (gerçek sunucuya) ya da gerçek models/ klasörüne gitmez.
"""

from __future__ import annotations

import inspect
import json
import logging
from pathlib import Path

import pytest

from config.defaults import LMSTUDIO_DEFAULT_URL, OLLAMA_DEFAULT_URL
from core.mt_engines import contracts, discovery, registry
from core.mt_engines.chat import ChatTemplateEngine
from core.mt_engines.hymt import HYMTFormatEngine

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_TR_CHARS = "çğıöşüÇĞİÖŞÜ"


def _registry_warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.name == registry.__name__ and r.levelno >= logging.WARNING]


def _discovery_warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.name == discovery.__name__ and r.levelno >= logging.WARNING]


def _use_sources_file(monkeypatch, iso, path: Path) -> None:
    """``discovery.load_sources()`` (yol verilmeden) bu dosyayı okusun."""
    original = iso.original_load_sources

    def load(p=None, *args, **kwargs):
        return original(p if p is not None else path, *args, **kwargs)

    monkeypatch.setattr(discovery, "load_sources", load)


# ===========================================================================
# 1. Kimlik tablosu
# ===========================================================================

class TestGecerliKimlikler:
    def test_yerlesik_varsayilan(self):
        spec = registry.resolve_spec(registry.DEFAULT_ENGINE)
        assert spec is registry.BUILTIN_SPECS[registry.DEFAULT_ENGINE]
        assert spec.source == contracts.SPEC_SOURCE_BUILTIN
        assert spec.backend == contracts.BACKEND_MLX
        assert isinstance(registry.create_engine(registry.DEFAULT_ENGINE), HYMTFormatEngine)

    def test_mlx_tek_klasor(self, mt_isolation):
        spec = registry.resolve_spec("mlx:qwen3-8b-mlx")
        assert spec.name == "mlx:qwen3-8b-mlx"
        assert spec.source == contracts.SPEC_SOURCE_ADHOC
        assert spec.backend == contracts.BACKEND_MLX
        assert spec.local_dir == "qwen3-8b-mlx" and spec.path == ""
        assert spec.format == contracts.FORMAT_CHAT
        assert isinstance(registry.create_engine("mlx:qwen3-8b-mlx"), ChatTemplateEngine)

    def test_mlx_nosync_klasoru_otomatik_bulunur(self, mt_isolation):
        real = mt_isolation.models_dir / "yeni-mlx.nosync"
        real.mkdir()
        engine = registry.create_engine("mlx:yeni-mlx")
        assert engine.resolve_local_model_dir(registry.MODELS_DIR) == real

    def test_mlx_hymt_adindan_bicim(self):
        spec = registry.resolve_spec("mlx:HY-MT1.5-1.8B-mlx")
        assert spec.format == contracts.FORMAT_HYMT
        assert isinstance(registry.create_engine("mlx:HY-MT1.5-1.8B-mlx"), HYMTFormatEngine)

    def test_mlx_models_icinde_mutlak_yol(self, mt_isolation):
        target = mt_isolation.models_dir / "orcarouter" / "Qwen3-Test-MLX"
        spec = registry.resolve_spec(f"mlx:{target}")
        assert Path(spec.path) == target
        assert spec.local_dir == ""
        assert spec.name == f"mlx:{target}"

    def test_models_icindeki_sembolik_bag_izinli(self, mt_isolation, tmp_path):
        # KARARLAR 1: başka yerdeki model models/ altına sembolik bağla konur.
        outside = tmp_path / "baska_yer" / "Model-MLX"
        outside.mkdir(parents=True)
        link = mt_isolation.models_dir / "bagli-model"
        link.symlink_to(outside, target_is_directory=True)
        assert Path(registry.resolve_spec(f"mlx:{link}").path) == link
        assert registry.resolve_spec("mlx:bagli-model").local_dir == "bagli-model"

    def test_gguf_tek_dosya(self, mt_isolation):
        spec = registry.resolve_spec("gguf:Qwen3-8B-Q4_K_M.gguf")
        assert spec.backend == contracts.BACKEND_LLAMACPP
        assert Path(spec.path) == mt_isolation.models_dir / "Qwen3-8B-Q4_K_M.gguf"
        assert spec.format == contracts.FORMAT_CHAT

    def test_gguf_mutlak_yol_ve_hymt(self, mt_isolation):
        target = mt_isolation.models_dir / "tencent" / "HY-MT1.5-7B-Q8_0.gguf"
        spec = registry.resolve_spec(f"gguf:{target}")
        assert Path(spec.path) == target
        assert spec.format == contracts.FORMAT_HYMT

    def test_ollama_ilk_iki_noktadan_sonrasi_aynen(self, mt_isolation):
        spec = registry.resolve_spec("ollama:qwen3:8b")
        assert spec.backend == contracts.BACKEND_OLLAMA
        assert spec.model == "qwen3:8b"
        assert spec.base_url == mt_isolation.sources.ollama_url
        assert spec.format == contracts.FORMAT_CHAT
        hf = registry.resolve_spec("ollama:hf.co/org/repo:Q4_K_M")
        assert hf.model == "hf.co/org/repo:Q4_K_M"

    def test_lmstudio(self, mt_isolation):
        spec = registry.resolve_spec("lmstudio:qwen/qwen3-8b")
        assert spec.backend == contracts.BACKEND_OPENAI
        assert spec.flavor == contracts.FLAVOR_LMSTUDIO
        assert spec.model == "qwen/qwen3-8b"
        assert spec.base_url == mt_isolation.sources.lmstudio_url

    def test_openai_adres_ve_model(self):
        spec = registry.resolve_spec("openai:http://127.0.0.1:8080/v1|qwen")
        assert spec.backend == contracts.BACKEND_OPENAI
        assert spec.flavor == contracts.FLAVOR_GENERIC
        assert spec.base_url == "http://127.0.0.1:8080/v1"
        assert spec.model == "qwen"

    @pytest.mark.parametrize("address,expected", [
        ("http://localhost:8080", "http://localhost:8080/v1"),       # yol yoksa /v1
        ("http://127.0.0.1:8080/v1/", "http://127.0.0.1:8080/v1"),   # sondaki '/'
        ("http://[::1]:8000/v1", "http://[::1]:8000/v1"),
    ])
    def test_openai_adres_normallesir(self, address, expected):
        spec = registry.resolve_spec(f"openai:{address}|m")
        assert spec.base_url == expected

    def test_kesif_kimligi_insa_edilip_geri_cozulur(self):
        ident = contracts.build_engine_id("openai", "qwen", "http://127.0.0.1:8080/v1")
        assert registry.resolve_spec(ident).model == "qwen"
        assert registry.resolve_spec(
            contracts.build_engine_id("ollama", "qwen3:8b")).model == "qwen3:8b"

    @pytest.mark.parametrize("engine_id,source", [
        ("hymt-7b", contracts.SOURCE_BUILTIN),
        ("mlx:x-mlx", contracts.SOURCE_MLX),
        ("gguf:x.gguf", contracts.SOURCE_GGUF),
        ("ollama:x", contracts.SOURCE_OLLAMA),
        ("lmstudio:x", contracts.SOURCE_LMSTUDIO),
        ("openai:http://127.0.0.1:8080/v1|x", contracts.SOURCE_OPENAI),
    ])
    def test_kaynak_eslemesi(self, engine_id, source):
        assert registry.engine_source(registry.resolve_spec(engine_id)) == source


class TestGecersizKimlikler:
    @pytest.mark.parametrize("engine_id,fragment", [
        ("mlx:../x", "Geçersiz model kimliği"),
        ("mlx:alt/klasor", "tek klasör adı"),
        ("mlx:..", "Geçersiz model kimliği"),
        ("mlx:/etc", "izinli klasörlerin dışında"),
        ("mlx:x.gguf", "gguf: öneki"),
        ("gguf:x.txt", ".gguf ile bitmeli"),
        ("gguf:alt/x.gguf", "tek dosya adı"),
        ("gguf:/etc/x.gguf", "izinli klasörlerin dışında"),
        ("openai:http://192.168.1.2:8080/v1|m", "yerel bir adres değil"),
        ("openai:http://100.64.0.7:11434|m", "yerel bir adres değil"),
        ("openai:http://ornek-bulut.com/v1|m", "yerel bir adres değil"),
        ("openai:http://127.0.0.1:8080/v1", "ayracı yok"),
        ("openai:|m", "sunucu adresi boş"),
        ("openai:ftp://127.0.0.1/v1|m", "Geçersiz sunucu adresi"),
        ("openai:http://127.0.0.1:8080/v1|", "model adı boş"),
        ("bilinmeyen:x", "Desteklenmeyen MT motoru"),
        ("olmayan-motor", "Desteklenmeyen MT motoru"),
        ("ollama:", "sonrası boş"),
        ("ollama:qwen\x07", "denetim karakteri"),
        ("ollama:qwen 3", "boşluk"),
        ("lmstudio:" + "a" * (contracts.MODEL_NAME_MAX_LEN + 1), "karakterden uzun"),
        ("", "kimlik boş"),
        ("   ", "kimlik boş"),
    ])
    def test_reddedilir_turkce_ileti(self, engine_id, fragment):
        with pytest.raises(ValueError) as exc:
            registry.resolve_spec(engine_id)
        message = str(exc.value)
        assert fragment in message, message
        assert any(ch in message for ch in _TR_CHARS), message
        assert not contracts.has_oom_trigger(message), message
        # create_engine aynı kapıdan geçer
        with pytest.raises(ValueError):
            registry.create_engine(engine_id)

    def test_iki_nokta_iceren_yol_ve_parca(self, mt_isolation):
        with pytest.raises(ValueError, match="'..'"):
            registry.resolve_spec(f"mlx:{mt_isolation.models_dir}/../kacis")

    def test_bilinmeyen_iletisi_adlari_ve_kimlik_bicimlerini_sayar(self):
        with pytest.raises(ValueError) as exc:
            registry.resolve_spec("olmayan-motor")
        message = str(exc.value)
        assert "'olmayan-motor'" in message
        assert registry.DEFAULT_ENGINE in message
        for prefix in contracts.ID_PREFIXES:
            assert f"{prefix}:" in message

    def test_models_disindaki_gercek_yol_reddedilir(self, tmp_path):
        outside = tmp_path / "disarida" / "Model-MLX"
        outside.mkdir(parents=True)
        with pytest.raises(ValueError, match="izinli klasörlerin dışında"):
            registry.resolve_spec(f"mlx:{outside}")

    def test_uzak_adres_aga_gitmeden_reddedilir(self, fake_ollama):
        # Reddedilen kimlik hiçbir sunucuya istek atmaz (çözüm ağsızdır).
        with pytest.raises(ValueError):
            registry.resolve_spec("openai:http://10.0.0.5:11434|qwen3:8b")
        assert fake_ollama.requests == []


# ===========================================================================
# 2. Sunucu adresleri config/mt_sources.json'dan (KARARLAR 1-2)
# ===========================================================================

class TestKaynakAdresleri:
    def test_mt_sources_json_adresleri(self, tmp_path, monkeypatch, mt_isolation):
        path = tmp_path / contracts.MT_SOURCES_FILENAME
        path.write_text(json.dumps({
            "ollama": {"url": "http://localhost:7777"},
            "lmstudio": {"url": "http://127.0.0.1:5555"},
        }), encoding="utf-8")
        _use_sources_file(monkeypatch, mt_isolation, path)
        assert registry.resolve_spec("ollama:qwen3:8b").base_url == "http://localhost:7777"
        lms = registry.resolve_spec("lmstudio:pub/model")
        assert lms.flavor == contracts.FLAVOR_LMSTUDIO
        assert lms.base_url == "http://127.0.0.1:5555/v1"

    def test_dosya_yoksa_varsayilan_adresler(self, tmp_path, monkeypatch, mt_isolation):
        _use_sources_file(monkeypatch, mt_isolation, tmp_path / "yok.json")
        assert registry.resolve_spec("ollama:x").base_url == OLLAMA_DEFAULT_URL
        assert registry.resolve_spec("lmstudio:x").base_url == LMSTUDIO_DEFAULT_URL

    def test_uzak_adres_acilamaz(self, tmp_path, monkeypatch, mt_isolation, caplog):
        path = tmp_path / contracts.MT_SOURCES_FILENAME
        path.write_text(json.dumps({
            "ollama": {"url": "http://192.168.1.20:11434"},
            "lmstudio": {"url": "http://100.64.0.7:1234/v1"},
            "openai_servers": [{"name": "uzak", "url": "http://10.0.0.5:8080/v1"}],
        }), encoding="utf-8")
        _use_sources_file(monkeypatch, mt_isolation, path)
        with caplog.at_level(logging.WARNING, logger=discovery.__name__):
            sources = discovery.load_sources()
            assert registry.resolve_spec("ollama:x").base_url == OLLAMA_DEFAULT_URL
        assert sources.ollama_url == OLLAMA_DEFAULT_URL
        assert sources.lmstudio_url == LMSTUDIO_DEFAULT_URL
        assert sources.openai_servers == ()
        warnings = " ".join(_discovery_warnings(caplog))
        assert "yerel bir adres değil" in warnings

    def test_model_klasoru_eklenemez(self, tmp_path, monkeypatch, mt_isolation, caplog):
        extra = tmp_path / "lmstudio_models" / "org" / "Model-MLX"
        extra.mkdir(parents=True)
        path = tmp_path / contracts.MT_SOURCES_FILENAME
        path.write_text(json.dumps({"model_dirs": [str(tmp_path / "lmstudio_models")]}),
                        encoding="utf-8")
        _use_sources_file(monkeypatch, mt_isolation, path)
        with caplog.at_level(logging.WARNING, logger=discovery.__name__):
            discovery.load_sources()
        assert any("model_dirs" in w and "desteklenmiyor" in w
                   for w in _discovery_warnings(caplog))
        roots = discovery.allowed_roots(discovery.load_sources())
        assert all(root in (Path(mt_isolation.models_dir),
                            Path(mt_isolation.models_dir).resolve()) for root in roots)
        with pytest.raises(ValueError, match="izinli klasörlerin dışında"):
            registry.resolve_spec(f"mlx:{extra}")

    def test_bozuk_dosya_varsayilanlara_duser(self, tmp_path, monkeypatch, mt_isolation,
                                              caplog):
        path = tmp_path / contracts.MT_SOURCES_FILENAME
        path.write_text("{bozuk", encoding="utf-8")
        _use_sources_file(monkeypatch, mt_isolation, path)
        with caplog.at_level(logging.WARNING, logger=discovery.__name__):
            assert discovery.load_sources() == contracts.SourcesConfig()
        assert _discovery_warnings(caplog)


# ===========================================================================
# 3. config/mt_engines.json yeni alanları
# ===========================================================================

_USER_SPECS = {
    "_aciklama": "Anahtarı '_' ile başlayan girdiler yorumdur.",
    "qwen3-ollama": {"backend": "ollama", "model": "qwen3:8b", "format": "chat",
                     "options": {"num_ctx": 8192, "keep_alive": "30m"}, "think": False,
                     "description": "Qwen3 8B (Ollama)"},
    "llamacpp-sunucu": {"backend": "openai", "base_url": "http://127.0.0.1:8080/v1",
                        "model": "qwen",
                        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}}},
    "hymt-gguf": {"backend": "llamacpp", "path": "HY-MT1.5-7B-Q8_0.gguf", "format": "hymt"},
    "yeni-mlx": {"format": "chat", "local_dir": "yeni-model-mlx"},
    "lms-kayit": {"backend": "openai", "flavor": "lmstudio", "model": "pub/model"},
}


def _write_user_specs(iso, data: dict) -> Path:
    iso.user_specs_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return iso.user_specs_path


class TestKullaniciKayitlari:
    def test_yeni_alanlar(self, mt_isolation):
        _write_user_specs(mt_isolation, _USER_SPECS)
        specs = registry.load_user_specs()
        assert set(specs) == {"qwen3-ollama", "llamacpp-sunucu", "hymt-gguf", "yeni-mlx",
                              "lms-kayit"}
        ollama = registry.resolve_spec("qwen3-ollama")
        assert ollama.source == contracts.SPEC_SOURCE_USER
        assert ollama.backend == contracts.BACKEND_OLLAMA
        assert ollama.options == {"num_ctx": 8192, "keep_alive": "30m"}
        assert ollama.think is False
        assert ollama.base_url == mt_isolation.sources.ollama_url
        server = registry.resolve_spec("llamacpp-sunucu")
        assert server.base_url == "http://127.0.0.1:8080/v1"
        assert server.extra_body == {"chat_template_kwargs": {"enable_thinking": False}}
        gguf = registry.resolve_spec("hymt-gguf")
        assert Path(gguf.path) == mt_isolation.models_dir / "HY-MT1.5-7B-Q8_0.gguf"
        assert isinstance(registry.create_engine("hymt-gguf"), HYMTFormatEngine)
        assert registry.resolve_spec("yeni-mlx").local_dir == "yeni-model-mlx"
        lms = registry.resolve_spec("lms-kayit")
        assert lms.flavor == contracts.FLAVOR_LMSTUDIO
        assert lms.base_url == mt_isolation.sources.lmstudio_url

    def test_yorum_girdisi_sessiz_atlanir(self, mt_isolation, caplog):
        _write_user_specs(mt_isolation, {"_aciklama": "yorum", "_not": {"x": 1},
                                         "yeni-mlx": _USER_SPECS["yeni-mlx"]})
        with caplog.at_level(logging.WARNING, logger=registry.__name__):
            specs = registry.load_user_specs()
        assert set(specs) == {"yeni-mlx"}
        assert _registry_warnings(caplog) == []

    @pytest.mark.parametrize("name,raw,fragment", [
        ("kotu:ad", {"format": "chat", "local_dir": "x"}, "':' içeremez"),
        ("uzak", {"backend": "ollama", "model": "x", "base_url": "http://10.0.0.5:11434"},
         "base_url reddedildi"),
        ("kacan", {"backend": "llamacpp", "path": "/etc/x.gguf"}, "path geçersiz"),
        ("gguf-olmayan", {"backend": "llamacpp", "path": "model.bin"}, ".gguf dosyası olmalı"),
        ("modelsiz", {"backend": "ollama"}, "model alanı geçersiz"),
        ("adressiz", {"backend": "openai", "model": "m"}, "base_url ister"),
        ("bilinmeyen-arka-uc", {"backend": "bulut", "model": "m"}, "bilinmeyen backend"),
        ("gezen", {"format": "chat", "local_dir": ".."}, "local_dir"),
    ])
    def test_gecersiz_kayit_uyari_ve_atla(self, mt_isolation, caplog, name, raw, fragment):
        _write_user_specs(mt_isolation, {name: raw, "yeni-mlx": _USER_SPECS["yeni-mlx"]})
        with caplog.at_level(logging.WARNING, logger=registry.__name__):
            specs = registry.load_user_specs()
        assert name not in specs and "yeni-mlx" in specs
        assert any(fragment in w for w in _registry_warnings(caplog)), _registry_warnings(caplog)

    def test_yerlesik_ezilemez(self, mt_isolation, caplog):
        _write_user_specs(mt_isolation, {registry.DEFAULT_ENGINE: {
            "backend": "ollama", "model": "sahte"}})
        with caplog.at_level(logging.WARNING, logger=registry.__name__):
            spec = registry.resolve_spec(registry.DEFAULT_ENGINE)
            engines = registry.available_engines()
        assert spec.source == contracts.SPEC_SOURCE_BUILTIN
        assert engines[registry.DEFAULT_ENGINE].backend == contracts.BACKEND_MLX
        assert any("ezemez" in w for w in _registry_warnings(caplog))

    def test_bilinmeyen_secenek_yok_sayilir(self, mt_isolation, caplog):
        _write_user_specs(mt_isolation, {"m": {"backend": "ollama", "model": "x",
                                               "options": {"num_ctx": 4096, "api_key": "k"}}})
        with caplog.at_level(logging.WARNING, logger=registry.__name__):
            spec = registry.resolve_spec("m")
        assert spec.options == {"num_ctx": 4096}
        assert any("api_key" in w for w in _registry_warnings(caplog))


# ===========================================================================
# 4. Çözüm sırası ve sözleşme
# ===========================================================================

class TestCozumSirasi:
    def test_kayit_adi_onekle_ayni_olabilir_ama_onekli_kimlik_onekli_kalir(self, mt_isolation):
        _write_user_specs(mt_isolation, {"mlx": {"format": "chat", "local_dir": "benim-mlx"}})
        assert registry.resolve_spec("mlx").source == contracts.SPEC_SOURCE_USER
        adhoc = registry.resolve_spec("mlx:benim-mlx")
        assert adhoc.source == contracts.SPEC_SOURCE_ADHOC

    def test_onekli_kimlik_kayitla_ezilemez(self, mt_isolation):
        _write_user_specs(mt_isolation, {"ollama:qwen3:8b": {
            "backend": "openai", "base_url": "http://127.0.0.1:9/v1", "model": "x"}})
        assert "ollama:qwen3:8b" not in registry.load_user_specs()
        spec = registry.resolve_spec("ollama:qwen3:8b")
        assert spec.source == contracts.SPEC_SOURCE_ADHOC
        assert spec.backend == contracts.BACKEND_OLLAMA

    def test_acik_kullanici_dosyasi_yolu(self, tmp_path):
        path = tmp_path / "ozel.json"
        path.write_text(json.dumps({"ozel": {"format": "chat", "local_dir": "o"}}),
                        encoding="utf-8")
        assert registry.resolve_spec("ozel", user_specs_path=path).local_dir == "o"
        assert isinstance(registry.create_engine("ozel", path), ChatTemplateEngine)
        with pytest.raises(ValueError, match="Desteklenmeyen MT motoru"):
            registry.resolve_spec("ozel")

    def test_available_engines_yalniz_adli_motorlar(self, mt_isolation):
        _write_user_specs(mt_isolation, {"yeni-mlx": _USER_SPECS["yeni-mlx"]})
        registry.resolve_spec("ollama:qwen3:8b")
        names = set(registry.available_engines())
        assert names == {registry.DEFAULT_ENGINE, "yeni-mlx"}
        assert not any(contracts.ID_PREFIX_SEP in n for n in names)

    def test_imzalar_sabit(self):
        assert list(inspect.signature(registry.create_engine).parameters) == [
            "name", "user_specs_path"]
        assert list(inspect.signature(registry.available_engines).parameters) == [
            "user_specs_path"]
        assert "engine_id" in inspect.signature(registry.resolve_spec).parameters

    def test_varsayilan_motor_tek_kaynak(self):
        # C.4: DEFAULT_ENGINE tek kaynak; yeni kod adı literal yazmaz.
        assert registry.DEFAULT_ENGINE in registry.BUILTIN_SPECS
        for rel in ("core/mt_engines/discovery.py", "translate_book.py"):
            src = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
            literal = registry.DEFAULT_ENGINE
            assert f'"{literal}"' not in src and f"'{literal}'" not in src, rel

    def test_ornek_dosyalara_atif_yok(self):
        # KARARLAR 13: config/*.json.example kaldırıldı; K2 kodu onlara atıf yapmaz.
        for rel in ("core/mt_engines/registry.py", "core/mt_engines/discovery.py",
                    "translate_book.py", "latex_pipeline/retranslator.py"):
            src = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
            assert ".json.example" not in src, rel
