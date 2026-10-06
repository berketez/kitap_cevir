"""K6 (5 Ekim 2026): arayüz ve küçük kod.

packaging/TASARIM.md §F ve günün arayüz kalemleri:

- F2: derleyici PATH'te yoksa ``/usr/local/texlive/*/bin/universal-darwin/<ad>``
  adayları da aranır, sürümler azalan sırada (eskiden yalnız 2025/2024);
- F3: ``core.hardware`` Metal ölçümünü MLX'ten sonra, sysctl yedeğinden önce
  stdlib ``ctypes`` ile de alır (``SOURCE_METAL``; değer MLX'inkiyle aynı);
- F4: kenar çubuğunda "Bileşenler" (yalnız ``KITAPCEVIR_YONETICI_URL`` varken),
  her zaman "Kaynak kodu" (AGPL m.13) ve "Hakkında" (Tencent HY lisansı m.3(e));
- (4) "Çevir"in olumlu ön kontrolünün uyarısı (``check.warning``) koşu boyunca
  görünür (eskiden hiç gösterilmiyordu);
- (5) "Yerel model ile çevir" kartında Beta etiketi ve notu (KARARLAR 23; ad
  değişmez);
- (6) model seçicide düşünme sınıfı etiketleri, düşünerek çeviri notu ve
  önerilen model (KARARLAR 29: HY-MT 1.5 7B, bulunduğunda varsayılan seçim);
- (7) paketli kipte yerleşik model eksikken "Bileşenler" ipucu (mlx_lm.convert
  komutu değil); "Modelleri tara" açıklamasında taranan klasörün gerçek yolu.

Yalıtım: tests/test_ui_model_secici_20260925.py ile aynı düzen (sahte keşif ve
sahte son kontrol, CLI koşturulmaz, ağ yok, seçim dosyası tmp'de). Testteki
modeller, raporlar ve kitap SENTETİK test verisidir; kullanıcıya gösterilmez.
"""

from __future__ import annotations

import ast
import functools
import html
import importlib.util
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import streamlit
from streamlit.testing.v1 import AppTest

import core.memory
from config.paths import VERI_KOKU_DEGISKENI, YONETICI_URL_DEGISKENI
from core import hardware
from core.mt_engines import registry
from core.mt_engines.contracts import (
    MSG_SOURCE_COUNT_MODELS,
    THINKING_ALWAYS,
    THINKING_CAN_DISABLE,
    THINKING_NONE,
    THINKING_OFF_OLLAMA_THINK,
    DiscoveredModel,
    DiscoveryReport,
    EngineCheck,
    SourceStatus,
)
from core.mt_engines.discovery import WarnedEngineCheck
from core.mt_engines.registry import DEFAULT_ENGINE
from latex_pipeline import compiler as compiler_mod
from latex_pipeline.config import PipelineConfig
from ui import branding, runner
from ui.runner import PROJECT_ROOT

APP = PROJECT_ROOT / "app.py"
APP_TIMEOUT_SEC = 60
K6_BOOK_FILE = "K6 Sentetik Kitap.pdf"
PICKER_KEY = "mt_engine"
SCAN_KEY = "model_scan"
TRANSLATE_KEY = "model_translate"

# Gerçek ölçüm fonksiyonu: conftest ``_hardware_isolation`` her testte
# ``hardware._measure``'ı yamalar; modül içe aktarılırken henüz yamalı değildir.
REAL_MEASURE = hardware._measure

# Sentetik ölçüm değerleri (test verisi; bu Mac'in değeri değil).
CTYPES_AVAILABLE = 21_474_836_480
SYSCTL_TOTAL = 34_359_738_368


def _app_constants() -> dict:
    """app.py'nin modül düzeyindeki büyük harfli sabitleri (AST ile)."""
    namespace: dict = {"branding": branding}
    for node in ast.parse(APP.read_text(encoding="utf-8")).body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id.isupper()):
            try:
                namespace[node.targets[0].id] = eval(
                    compile(ast.Expression(node.value), "app.py", "eval"), namespace)
            except Exception:
                continue
    return namespace


C = _app_constants()


# ---------------------------------------------------------------------------
# F2 — derleyici adayları
# ---------------------------------------------------------------------------

class TestF2DerleyiciAdaylari:
    @pytest.fixture
    def texlive(self, tmp_path, monkeypatch):
        """Sahte TeX Live kökü; PATH ve MacTeX bağı yok."""
        root = tmp_path / "texlive"
        for version in ("2023", "2026", "2025"):
            binary = root / version / "bin" / "universal-darwin" / "xelatex"
            binary.parent.mkdir(parents=True)
            binary.write_text("", encoding="utf-8")
        (root / "texmf-local").mkdir()                      # bin yok
        (root / "2024" / "bin" / "universal-darwin").mkdir(parents=True)  # ikili yok
        monkeypatch.setattr(compiler_mod, "TEXLIVE_ROOT", root)
        monkeypatch.setattr(compiler_mod, "MACTEX_BIN_DIR", tmp_path / "texbin_yok")
        monkeypatch.setattr(compiler_mod.shutil, "which", lambda name: None)
        return root

    def test_surumler_azalan_sirada(self, texlive):
        found = compiler_mod.texlive_candidates("xelatex")
        assert [p.parents[2].name for p in found] == ["2026", "2025", "2023"]

    def test_en_yeni_surum_secilir(self, texlive, tmp_path):
        comp = compiler_mod.LatexCompiler(PipelineConfig(output_dir=str(tmp_path / "o")))
        assert comp._compiler_bin == str(texlive / "2026" / "bin" / "universal-darwin"
                                         / "xelatex")

    def test_sayisal_siralama(self, tmp_path, monkeypatch):
        """Sözcük sırası değil sayı sırası: 2100 > 999 (ileride yıl dışı ad)."""
        root = tmp_path / "tl"
        for version in ("999", "2100"):
            path = root / version / "bin" / "universal-darwin" / "lualatex"
            path.parent.mkdir(parents=True)
            path.write_text("", encoding="utf-8")
        monkeypatch.setattr(compiler_mod, "TEXLIVE_ROOT", root)
        assert [p.parents[2].name for p in compiler_mod.texlive_candidates("lualatex")] == [
            "2100", "999"]

    def test_mactex_bagi_once(self, texlive, tmp_path, monkeypatch):
        texbin = tmp_path / "texbin"
        texbin.mkdir()
        (texbin / "xelatex").write_text("", encoding="utf-8")
        monkeypatch.setattr(compiler_mod, "MACTEX_BIN_DIR", texbin)
        comp = compiler_mod.LatexCompiler(PipelineConfig(output_dir=str(tmp_path / "o")))
        assert comp._compiler_bin == str(texbin / "xelatex")

    def test_path_hepsinden_once(self, texlive, tmp_path, monkeypatch):
        monkeypatch.setattr(compiler_mod.shutil, "which", lambda name: f"/yol/{name}")
        comp = compiler_mod.LatexCompiler(PipelineConfig(output_dir=str(tmp_path / "o")))
        assert comp._compiler_bin == "/yol/xelatex"

    def test_hicbiri_yoksa_ad(self, tmp_path, monkeypatch):
        monkeypatch.setattr(compiler_mod, "TEXLIVE_ROOT", tmp_path / "yok")
        monkeypatch.setattr(compiler_mod, "MACTEX_BIN_DIR", tmp_path / "yok2")
        monkeypatch.setattr(compiler_mod.shutil, "which", lambda name: None)
        comp = compiler_mod.LatexCompiler(PipelineConfig(output_dir=str(tmp_path / "o")))
        assert comp._compiler_bin == "xelatex"

    def test_sabit_yil_listesi_kalmadi(self):
        source = Path(compiler_mod.__file__).read_text(encoding="utf-8")
        assert "texlive/2025" not in source and "texlive/2024" not in source


# ---------------------------------------------------------------------------
# F3 — ctypes ile Metal ölçümü
# ---------------------------------------------------------------------------

def _no_mlx(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "mlx", None)
    monkeypatch.setitem(sys.modules, "mlx.core", None)


def _fake_sysctl(monkeypatch, values: dict) -> None:
    monkeypatch.setattr(hardware, "_run_sysctl", lambda name: values.get(name, ""))


class _FakeMx:
    def __init__(self, info: dict):
        self._info = info

    def device_info(self):
        return dict(self._info)


def _fake_mlx(monkeypatch, info: dict) -> None:
    import types

    fake = types.ModuleType("mlx.core")
    fake.device_info = _FakeMx(info).device_info
    package = types.ModuleType("mlx")
    package.core = fake
    monkeypatch.setitem(sys.modules, "mlx", package)
    monkeypatch.setitem(sys.modules, "mlx.core", fake)


class TestF3CtypesMetal:
    def test_mlx_yoksa_ctypes_metal_olcumu(self, monkeypatch):
        _no_mlx(monkeypatch)
        _fake_sysctl(monkeypatch, {"hw.memsize": str(SYSCTL_TOTAL),
                                   "machdep.cpu.brand_string": "Apple M9"})
        monkeypatch.setattr(hardware, "_metal_ctypes_info",
                            lambda: (CTYPES_AVAILABLE, "Apple M9 GPU", ""))
        info = REAL_MEASURE()
        assert info.source == hardware.SOURCE_METAL
        assert info.available_bytes == CTYPES_AVAILABLE
        assert info.memory_bytes == SYSCTL_TOTAL
        assert info.chip == "Apple M9" and info.problem == ""
        # Metal ölçüldü: yedek uyarısı ve "(yedek ölçüm)" notu yok (ileti değişmedi).
        assert hardware.general_gate(info).warning == ""
        assert not hardware.hardware_caption(info).endswith(
            hardware.MSG_HW_CAPTION_FALLBACK_NOTE)

    def test_cip_adi_yoksa_metal_aygit_adi(self, monkeypatch):
        _no_mlx(monkeypatch)
        _fake_sysctl(monkeypatch, {"hw.memsize": str(SYSCTL_TOTAL)})
        monkeypatch.setattr(hardware, "_metal_ctypes_info",
                            lambda: (CTYPES_AVAILABLE, "Apple M9", ""))
        assert REAL_MEASURE().chip == "Apple M9"

    def test_mlx_varsa_ctypes_cagrilmaz(self, monkeypatch):
        """Sıra: MLX önce (TASARIM F3: "MLX'ten sonra")."""
        _fake_mlx(monkeypatch, {"memory_size": SYSCTL_TOTAL,
                                "max_recommended_working_set_size": CTYPES_AVAILABLE + 1})
        _fake_sysctl(monkeypatch, {"machdep.cpu.brand_string": "Apple M9"})

        def boom():
            raise AssertionError("MLX ölçümü varken ctypes çağrılmamalı")

        monkeypatch.setattr(hardware, "_metal_ctypes_info", boom)
        info = REAL_MEASURE()
        assert info.source == hardware.SOURCE_METAL
        assert info.available_bytes == CTYPES_AVAILABLE + 1

    def test_mlx_deger_vermezse_ctypes(self, monkeypatch):
        _fake_mlx(monkeypatch, {"memory_size": SYSCTL_TOTAL})
        _fake_sysctl(monkeypatch, {"hw.memsize": str(SYSCTL_TOTAL)})
        monkeypatch.setattr(hardware, "_metal_ctypes_info",
                            lambda: (CTYPES_AVAILABLE, "", ""))
        info = REAL_MEASURE()
        assert info.source == hardware.SOURCE_METAL
        assert info.available_bytes == CTYPES_AVAILABLE
        assert info.memory_bytes == SYSCTL_TOTAL and info.problem == ""

    def test_ikisi_de_yoksa_sysctl_yedegi_iki_neden(self, monkeypatch):
        _no_mlx(monkeypatch)
        _fake_sysctl(monkeypatch, {"hw.memsize": str(SYSCTL_TOTAL)})
        monkeypatch.setattr(hardware, "_metal_ctypes_info",
                            lambda: (None, "", "Metal aygıtı yok"))
        info = REAL_MEASURE()
        assert info.source == hardware.SOURCE_SYSCTL
        assert "MLX yüklenemedi" in info.problem and "Metal aygıtı yok" in info.problem
        assert hardware.general_gate(info).warning.startswith("Metal ölçümü alınamadı (")

    def test_ctypes_hatasi_yutulur(self, monkeypatch):
        import ctypes.util

        def boom(name):
            raise OSError("kitaplık yok")

        monkeypatch.setattr(ctypes.util, "find_library", boom)
        available, device, problem = hardware._metal_ctypes_info()
        assert available is None and device == ""
        assert problem.startswith("Metal") and "OSError" in problem

    def test_dusunme_payi_hesabi_bozulmadi(self):
        """G2b-2'nin ``assess(think_tokens)`` hesabı F3'ten etkilenmez."""
        hw = hardware.HardwareInfo(available_bytes=CTYPES_AVAILABLE,
                                   memory_bytes=SYSCTL_TOTAL, chip="Test",
                                   source=hardware.SOURCE_METAL)
        kv = hardware.reference_kv()
        weights = 4_000_000_000
        plain = hardware.assess(weights, kv, hw=hw)
        thinking = hardware.assess(weights, kv, hw=hw, think_tokens=4096)
        assert thinking.kv_tokens > plain.kv_tokens
        assert thinking.batch_size is not None and plain.batch_size is not None
        assert thinking.batch_size <= plain.batch_size

    def test_modul_ctypes_disinda_ust_duzeyde_yeni_ice_aktarma_yok(self):
        """Hafif modül: ctypes yalnız ölçüm fonksiyonunda içe aktarılır."""
        tree = ast.parse(Path(hardware.__file__).read_text(encoding="utf-8"))
        top = {alias.name for node in tree.body if isinstance(node, ast.Import)
               for alias in node.names}
        assert "ctypes" not in top

    @pytest.mark.skipif(sys.platform != "darwin", reason="Metal yalnız macOS'ta")
    def test_gercek_olcum_mlx_ile_ayni(self):
        """Bu Mac'te gerçek ölçüm: ctypes değeri MLX'in değerine eşit (TASARIM A.3)."""
        available, device, problem = hardware._metal_ctypes_info()
        assert problem == "" and isinstance(available, int) and available > 0
        info, mlx_problem = hardware._metal_device_info()
        if info is None:
            pytest.skip(f"MLX yok: {mlx_problem}")
        assert available == info.get("max_recommended_working_set_size")


# ---------------------------------------------------------------------------
# Arayüz ortamı (test_ui_model_secici_20260925.py düzeni)
# ---------------------------------------------------------------------------

def _model(engine_id: str, label: str, source: str, backend: str, *,
           usable: bool = True, size: int | None = None, fmt: str = "chat",
           reasoning: bool | None = None, thinking: str | None = None,
           thinking_off: str | None = None) -> DiscoveredModel:
    return DiscoveredModel(engine_id=engine_id, label=label, source=source,
                           backend=backend, format_guess=fmt, usable=usable,
                           size_bytes=size, reasoning=reasoning, thinking=thinking,
                           thinking_off=thinking_off)


HYMT = _model(DEFAULT_ENGINE, "HY-MT1.5 7B", "builtin", "mlx", size=15_000_000_000,
              fmt="hymt", thinking=THINKING_NONE)
QWEN_OFF = _model("ollama:qwen3:8b", "qwen3:8b", "ollama", "ollama", size=5_200_000_000,
                  reasoning=True, thinking=THINKING_CAN_DISABLE,
                  thinking_off=THINKING_OFF_OLLAMA_THINK)
GPTOSS = _model("ollama:gpt-oss:20b", "gpt-oss:20b", "ollama", "ollama",
                size=13_800_000_000, reasoning=True, thinking=THINKING_ALWAYS)
OLD_REASONING = _model("openai:http://127.0.0.1:8080/v1|eski", "eski", "openai", "openai",
                       reasoning=True)
PLAIN = _model("openai:http://127.0.0.1:8080/v1|duz", "duz", "openai", "openai")

OLLAMA_UP = SourceStatus("ollama", "Ollama", True, MSG_SOURCE_COUNT_MODELS.format(n=2),
                         "http://127.0.0.1:11434")
WARNING_TEXT = "Bellek uyarısı: sentetik test uyarısı (kaba tahmin)."


def _report(models=(HYMT, QWEN_OFF, GPTOSS, OLD_REASONING, PLAIN)) -> DiscoveryReport:
    return DiscoveryReport(models=tuple(models), sources=(OLLAMA_UP,),
                           scanned_at=time.time())


class _StubMemory:
    def __init__(self, *args, **kwargs):
        pass

    def get_stats(self):
        return {"total_entries": 0, "confirmed_entries": 0}

    def find_similar(self, *args, **kwargs):
        return []

    def close(self):
        pass


class _Checker:
    def __init__(self):
        self.result: EngineCheck | None = None
        self.calls: list[str] = []

    def __call__(self, engine_id: str) -> EngineCheck:
        self.calls.append(engine_id)
        if self.result is not None:
            return self.result
        return EngineCheck(engine_id=engine_id, ok=True, label=engine_id)


@pytest.fixture
def env(monkeypatch, tmp_path, mt_isolation):
    root = tmp_path / "proje"
    (root / "output").mkdir(parents=True)
    monkeypatch.setattr(runner, "PROJECT_ROOT", root)
    store: dict[str, object] = {}

    def cache_resource(func=None, **_options):
        if func is None:
            return cache_resource

        @functools.wraps(func)
        def cached(*args, **kwargs):
            if func.__name__ not in store:
                store[func.__name__] = func(*args, **kwargs)
            return store[func.__name__]

        return cached

    monkeypatch.setattr(streamlit, "cache_resource", cache_resource)
    monkeypatch.setattr(core.memory, "TranslationMemory", _StubMemory)
    monkeypatch.setattr(runner, "memory_db_path", lambda: tmp_path / "bellek_yok.db")
    monkeypatch.setattr(runner, "platform_problem", lambda: None)
    monkeypatch.setattr(runner, "local_model_dir", lambda: Path("/model"))
    temp_root = tmp_path / "gecici"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    reports = [_report()]
    monkeypatch.setattr(runner, "discover_models", lambda: reports[-1])
    checker = _Checker()
    monkeypatch.setattr(runner, "engine_check", checker)
    started: list[list[str]] = []

    def fake_start(self):
        started.append(list(self.command))
        self.started_at = time.time()
        self._process = SimpleNamespace(poll=lambda: None, pid=-1)

    monkeypatch.setattr(runner.Run, "start", fake_start)
    return SimpleNamespace(store=store, tmp=tmp_path, reports=reports, checker=checker,
                           started=started)


def _with_book(env) -> Path:
    import fitz

    book = env.tmp / K6_BOOK_FILE
    with fitz.open() as doc:
        doc.new_page()
        doc.save(str(book))
    env.store["_job"] = {"book": book, "upload_key": (book.name, book.stat().st_size),
                         "run": None, "run_book": None, "run_name": None, "name": None}
    return book


def _run_app() -> AppTest:
    at = AppTest.from_file(str(APP), default_timeout=APP_TIMEOUT_SEC)
    at.run()
    assert not at.exception, at.exception
    return at


def _values(elements) -> list[str]:
    return [element.value for element in elements]


def _buttons(at: AppTest) -> dict:
    return {button.key: button for button in at.button}


def _sidebar_html(at: AppTest) -> str:
    return "\n".join(_values(at.sidebar.markdown))


def _main_html(at: AppTest) -> str:
    return "\n".join(_values(at.main.markdown))


# ---------------------------------------------------------------------------
# F4 — kenar çubuğu
# ---------------------------------------------------------------------------

def _load_by_path(name: str, path: Path):
    """``packaging`` adı PyPI paketiyle çakışır: modülü dosya yolundan yükle."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestF4KenarCubugu:
    MANAGER_URL = "http://127.0.0.1:53111/?anahtar=ab12&kip=bilesenler"

    def test_gelistirici_kipinde_bilesenler_yok(self, env):
        at = _run_app()
        body = _sidebar_html(at)
        assert branding.COMPONENTS_SCREEN not in body
        assert "kip=bilesenler" not in body

    def test_paketli_kipte_bilesenler_ayni_pencerede(self, env, monkeypatch):
        monkeypatch.setenv(YONETICI_URL_DEGISKENI, self.MANAGER_URL)
        at = _run_app()
        body = _sidebar_html(at)
        assert f'href="{html.escape(self.MANAGER_URL)}"' in body
        assert f">{branding.COMPONENTS_SCREEN}</a>" in body
        # Yönetici sayfası pencerenin kendisinde açılır (kabuk yeni pencereyi
        # varsayılan tarayıcıya gönderir; anahtarlı adres oraya gitmesin).
        assert 'target="_self"' in body

    @pytest.mark.parametrize("url", ["javascript:alert(1)", "https://ornek.com/?kip=bilesenler",
                                     "http://192.168.1.5:8000/", "   "])
    def test_yerel_olmayan_adres_gosterilmez(self, env, monkeypatch, url):
        monkeypatch.setenv(YONETICI_URL_DEGISKENI, url)
        at = _run_app()
        assert f">{branding.COMPONENTS_SCREEN}</a>" not in _sidebar_html(at)

    def test_kaynak_kodu_ve_hakkinda_her_zaman(self, env):
        at = _run_app()
        body = _sidebar_html(at)
        assert f'href="{branding.SOURCE_CODE_URL}"' in body
        assert branding.SOURCE_CODE_LABEL in body
        assert branding.ABOUT_LABEL in body
        assert html.escape(branding.ABOUT_TEXT) in body

    def test_hakkinda_lisans_3e_ogeleri(self):
        """Sağlayıcının tam adı + Tencent'le bağlantısızlık beyanı (m.3(e))."""
        text = branding.ABOUT_TEXT
        assert text.startswith(f"{branding.APP_NAME}, Berke Tezgöçen tarafından")
        assert "Tencent ile bağlantılı değildir" in text
        assert "desteklenmez ya da onaylanmaz" in text

    def test_hakkinda_sihirbaz_beyaniyla_ayni(self):
        tree = ast.parse((PROJECT_ROOT / "packaging" / "yonetici" / "lisans.py")
                         .read_text(encoding="utf-8"))
        statements = [value.value for node in ast.walk(tree) if isinstance(node, ast.Dict)
                      for key, value in zip(node.keys, node.values)
                      if isinstance(key, ast.Constant) and key.value == "beyan"
                      and isinstance(value, ast.Constant) and value.value]
        assert statements == [branding.ABOUT_TEXT]

    def test_kaynak_kodu_adresi_yoneticiyle_ayni(self):
        """Yönetici (stdlib, içe aktarmasız ayarlar.py) aynı adresi taşır."""
        settings = _load_by_path("_k6_yonetici_ayarlar",
                                 PROJECT_ROOT / "packaging" / "yonetici" / "ayarlar.py")
        assert branding.SOURCE_CODE_URL == settings.KAYNAK_KODU_URL

    def test_url_tek_yerde(self):
        """Arayüz kodunda depo adresi yazılmaz; yalnız ui/branding.py'de."""
        for relpath in ("app.py", "ui/runner.py"):
            assert "github.com" not in (PROJECT_ROOT / relpath).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# (4) olumlu ön kontrolün uyarısı
# ---------------------------------------------------------------------------

class TestOnKontrolUyarisi:
    def test_uyari_kosu_boyunca_gorunur(self, env):
        env.checker.result = WarnedEngineCheck(engine_id=DEFAULT_ENGINE, ok=True,
                                               label="HY-MT1.5 7B", warning=WARNING_TEXT)
        _with_book(env)
        at = _run_app()
        _buttons(at)[TRANSLATE_KEY].click().run()
        assert not at.exception
        assert len(env.started) == 1                      # iş başladı (uyarı engel değil)
        assert WARNING_TEXT in _values(at.warning)        # rerun'dan sonra da görünür
        assert env.store["_job"]["run_warning"] == WARNING_TEXT
        at.run()                                          # ilerleme kutusu yenilenince
        assert WARNING_TEXT in _values(at.warning)

    def test_uyarisiz_kontrolde_uyari_yok(self, env):
        _with_book(env)
        at = _run_app()
        _buttons(at)[TRANSLATE_KEY].click().run()
        assert len(env.started) == 1
        assert not at.warning
        assert env.store["_job"].get("run_warning") is None

    def test_bitmis_basarisiz_koşuda_uyari_kalir(self, env):
        """Koşu başarısız bitince uyarı sonuçla birlikte durur (bellek ipucu)."""
        env.checker.result = WarnedEngineCheck(engine_id=DEFAULT_ENGINE, ok=True,
                                               label="HY-MT1.5 7B", warning=WARNING_TEXT)
        _with_book(env)
        at = _run_app()
        _buttons(at)[TRANSLATE_KEY].click().run()
        run = env.store["_job"]["run"]
        run.returncode = 1
        run.finished_at = time.time()
        run._process = SimpleNamespace(poll=lambda: 1, pid=-1)
        at.run()
        assert not at.exception
        assert WARNING_TEXT in _values(at.warning)


# ---------------------------------------------------------------------------
# (5) Beta
# ---------------------------------------------------------------------------

class TestBeta:
    def test_kart_adi_degismedi(self):
        assert branding.MODEL_TITLE == "Yerel model ile çevir"
        assert branding.MODEL_BETA_LABEL == "Beta"

    def test_etiket_ve_not_kartta(self, env):
        _with_book(env)
        at = _run_app()
        body = _main_html(at)
        assert branding.MODEL_TITLE in body
        assert f'<span class="path-badge" lang="tr">{branding.MODEL_BETA_LABEL}</span>' in body
        assert branding.MODEL_BETA_NOTE in _values(at.caption)

    def test_tex_kartinda_beta_yok(self, env):
        _with_book(env)
        at = _run_app()
        titles = [v for v in _values(at.main.markdown) if 'class="path-title"' in v]
        tex = [v for v in titles if branding.TEX_TITLE in v]
        assert tex and "path-badge" not in tex[0]

    def test_donanim_kapisi_kapaliyken_de_beta(self, env, monkeypatch):
        closed = SimpleNamespace(allowed=False, reason="Kapalı (test).", warning="")
        monkeypatch.setattr(runner, "hardware_gate", lambda: closed)
        _with_book(env)
        at = _run_app()
        assert f">{branding.MODEL_BETA_LABEL}</span>" in _main_html(at)
        assert branding.MODEL_BETA_NOTE in _values(at.caption)


# ---------------------------------------------------------------------------
# (6) model seçici: düşünme sınıfı, düşünerek çeviri notu, önerilen model
# ---------------------------------------------------------------------------

def _expected_label(model: DiscoveredModel) -> str:
    """app.py seçenek etiketi: ad · kaynak · [önerilen ·] düşünme sınıfı · ayrıntı · boyut …"""
    from core.mt_engines.thinking import profile_of

    base = runner.model_option_label(model)
    head = f"{model.label} · {branding.SOURCE_LABELS[model.source]}"
    assert base.startswith(head)
    extras = [branding.MODEL_RECOMMENDED_LABEL] if model.engine_id == DEFAULT_ENGINE else []
    extras.append(branding.thinking_label(profile_of(model).cls))
    return " · ".join((head, *extras)) + base[len(head):]


class TestModelSecici:
    def test_secenek_etiketleri(self, env):
        _with_book(env)
        at = _run_app()
        options = at.selectbox(key=PICKER_KEY).options
        assert options == [_expected_label(m) for m in _report().models]
        # İşaretler adla kaynağın hemen ardında (satır sonu kesilse de görünür).
        assert options[0] == "HY-MT1.5 7B · yerleşik · önerilen · düşünme yok · 15 GB"
        assert options[2] == ("gpt-oss:20b · Ollama · düşünen model — düşünerek çevirir, "
                              "daha yavaştır · 13,8 GB")
        assert options[3] == "eski · OpenAI uyumlu sunucu · düşünme durumu bilinmiyor"
        assert sum(branding.MODEL_RECOMMENDED_LABEL in o for o in options) == 1

    def test_taban_etiket_beklenmedik_bicimdeyse_sona(self, env, monkeypatch):
        monkeypatch.setattr(runner, "model_option_label", lambda model: f"[{model.engine_id}]")
        _with_book(env)
        at = _run_app()
        options = at.selectbox(key=PICKER_KEY).options
        assert options[0] == (f"[{DEFAULT_ENGINE}] · {branding.MODEL_RECOMMENDED_LABEL} · "
                              f"{branding.thinking_label(THINKING_NONE)}")

    def test_onerilen_bulundugunda_varsayilan(self, env):
        _with_book(env)
        at = _run_app()
        assert at.selectbox(key=PICKER_KEY).value == DEFAULT_ENGINE

    def test_tarama_yokken_de_onerilen_isaretli(self, env, monkeypatch):
        def boom():
            raise RuntimeError("tarama çöktü")

        monkeypatch.setattr(runner, "discover_models", boom)
        _with_book(env)
        at = _run_app()
        options = at.selectbox(key=PICKER_KEY).options
        assert len(options) == 1 and options[0].endswith(branding.MODEL_RECOMMENDED_LABEL)
        # Model bilgisi yok: düşünme sınıfı uydurulmaz.
        assert not any(label in options[0] for label in branding.THINKING_LABELS.values())

    def test_dusunmeyen_secili_model_notu(self, env):
        _with_book(env)
        at = _run_app()
        captions = _values(at.caption)
        assert C["MODEL_THINKING_CAPTION"].format(
            label=branding.thinking_label(THINKING_NONE)) in captions
        assert not any(branding.THINKING_MODE_NOTE[1:] in c for c in captions)

    def test_kapatilabilen_model_notu(self, env):
        _with_book(env)
        at = _run_app()
        at.selectbox(key=PICKER_KEY).select(QWEN_OFF.engine_id).run()
        captions = _values(at.caption)
        assert C["MODEL_THINKING_CAPTION"].format(
            label=branding.thinking_label(THINKING_CAN_DISABLE)) in captions
        assert not any(branding.THINKING_MODE_NOTE[1:] in c for c in captions)
        assert C["MODEL_REASONING_CAPTION"] not in captions

    def test_kapatilamayan_model_dusunerek_ceviri_notu(self, env):
        _with_book(env)
        at = _run_app()
        at.selectbox(key=PICKER_KEY).select(GPTOSS.engine_id).run()
        captions = _values(at.caption)
        assert C["MODEL_THINKING_CAPTION"].format(
            label=branding.thinking_label(THINKING_ALWAYS)) in captions
        note = [c for c in captions if branding.THINKING_MODE_NOTE[1:] in c]
        assert note == ["D" + branding.THINKING_MODE_NOTE[1:]]

    def test_sinifi_bilinmeyen_dusunen_model_eski_not(self, env):
        _with_book(env)
        at = _run_app()
        at.selectbox(key=PICKER_KEY).select(OLD_REASONING.engine_id).run()
        assert C["MODEL_REASONING_CAPTION"] in _values(at.caption)

    def test_sinifi_bilinmeyen_model(self, env):
        _with_book(env)
        at = _run_app()
        at.selectbox(key=PICKER_KEY).select(PLAIN.engine_id).run()
        assert C["MODEL_THINKING_CAPTION"].format(
            label=branding.thinking_label(None)) in _values(at.caption)


# ---------------------------------------------------------------------------
# (7) paketli kip: kurulum ipucu ve "Modelleri tara" açıklaması
# ---------------------------------------------------------------------------

CONVERT_HINT = "mlx_lm.convert --hf-path tencent/ornek --mlx-path /x/models/hymt-7b-mlx"


class TestPaketliKip:
    def _missing(self, engine_id: str = DEFAULT_ENGINE) -> EngineCheck:
        return EngineCheck(engine_id=engine_id, ok=False, label="HY-MT1.5 7B",
                           reason="Yerleşik model bulunamadı (test).", hint=CONVERT_HINT)

    def test_paketli_kipte_bilesenler_ipucu(self, env, monkeypatch):
        monkeypatch.setenv(VERI_KOKU_DEGISKENI, str(env.tmp / "veri"))
        env.checker.result = self._missing()
        _with_book(env)
        at = _run_app()
        _buttons(at)[TRANSLATE_KEY].click().run()
        captions = _values(at.caption)
        assert branding.MODEL_INSTALL_HINT_PACKAGED in captions
        assert CONVERT_HINT not in captions
        assert env.started == []

    def test_gelistirici_kipinde_donusturme_komutu(self, env):
        env.checker.result = self._missing()
        _with_book(env)
        at = _run_app()
        _buttons(at)[TRANSLATE_KEY].click().run()
        captions = _values(at.caption)
        assert CONVERT_HINT in captions
        assert branding.MODEL_INSTALL_HINT_PACKAGED not in captions

    def test_paketli_kipte_kayitli_modelin_ipucu_aynen(self, env, monkeypatch):
        """Bileşenler yalnız HY-MT'yi kurar; kullanıcının kendi kaydı için ipucu değişmez."""
        monkeypatch.setenv(YONETICI_URL_DEGISKENI, TestF4KenarCubugu.MANAGER_URL)
        env.checker.result = self._missing(QWEN_OFF.engine_id)
        _with_book(env)
        at = _run_app()
        at.selectbox(key=PICKER_KEY).select(QWEN_OFF.engine_id).run()
        _buttons(at)[TRANSLATE_KEY].click().run()
        assert CONVERT_HINT in _values(at.caption)

    def test_tarama_aciklamasi_gercek_klasor(self, env, monkeypatch):
        models = env.tmp / "Application Support" / "Kitapçevir" / "veri" / "models"
        monkeypatch.setattr(registry, "MODELS_DIR", models)
        _with_book(env)
        at = _run_app()
        scan = _buttons(at)[SCAN_KEY]
        assert scan.help == branding.model_scan_help(models)
        assert f"{models}/" in scan.help

    def test_tarama_aciklamasi_ev_klasoru_kisaltilir(self):
        home_models = Path.home() / "Library" / "Application Support" / "Kitapçevir" / "veri" / "models"
        text = branding.model_scan_help(home_models)
        assert "~/Library/Application Support/Kitapçevir/veri/models/" in text
        assert str(Path.home()) not in text

    def test_tarama_aciklamasi_metni(self):
        text = branding.model_scan_help(Path("/x/models"))
        assert text.startswith("/x/models/ klasöründeki MLX klasörlerini ve GGUF dosyalarını")
        assert "Ollama ve LM Studio" in text and ".lmstudio" not in text


# ---------------------------------------------------------------------------
# (8) "Terimler" sekmesi (G2c yaması, KARAR 25) — arayüzde uyarı kutusu yok
# ---------------------------------------------------------------------------

class TestTerimlerSekmesi:
    def test_sozluk_tablosu_cizilir(self, env):
        book = _with_book(env)
        folder = runner.tex_files(book, None).output_dir
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "glossary_shared.tsv").write_text(
            "orbital resonance\työrünge rezonansı\nsynthetic term\tsentetik terim\n",
            encoding="utf-8")
        at = _run_app()
        assert at.dataframe, "sözlük tablosu çizilmedi"
        assert not at.warning

    def test_kullanimdan_kalkmis_genislik_parametresi_yok(self):
        """``use_container_width`` Streamlit 1.55'te kullanımdan kalktı (kaldırılma
        tarihi 2025-12-31 geçti; her çizimde konsola uyarı yazar, kaldırılınca
        TypeError olur). Tablonun varsayılan genişliği zaten "stretch"."""
        assert "use_container_width" not in APP.read_text(encoding="utf-8")
