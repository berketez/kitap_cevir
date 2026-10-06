"""Arayüz: model seçici ve "Modelleri tara" (K3, 25 Eylül 2026).

TASARIM F bölümü ve H.4'ün on testi, ek olarak yardımcıların birim testleri.
"Yerel model ile çevir" kartı artık tek bir modele bağlı değil: son taramada
gerçekten bulunan modeller (models/ klasöründeki MLX klasörleri ve GGUF
dosyaları, açık Ollama ve LM Studio sunucuları) seçicide listelenir; seçim
config/mt_selection.json'da saklanır ve yalnız arayüzün başlattığı komuta
``--mt-engine`` olarak geçer.

Yalıtım: keşif (runner.discover_models) ve son kontrol (runner.engine_check)
sahte rapor / sahte sonuç döndürür; bu dosyanın K2'nin discovery modülüne
bağımlılığı yoktur. Ağ yok (conftest._mt_isolation), gerçek
config/mt_selection.json açılmaz (engine_choice_path tmp'ye yamalı), çıktı
klasörleri tmp_path'te (runner.PROJECT_ROOT saptırılır), CLI koşturulmaz.
Testteki modeller ve raporlar sahte test verisidir; kullanıcıya gösterilmez.
"""

from __future__ import annotations

import ast
import functools
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import streamlit
from streamlit.testing.v1 import AppTest

import core.memory
from core.mt_engines.contracts import (
    DISCOVERY_SOURCE_ORDER,
    MSG_LLAMA_MISSING,
    MSG_LMSTUDIO_DOWN,
    MSG_MLX_ARCH_UNSUPPORTED,
    MSG_OLLAMA_DOWN,
    MSG_SOURCE_COUNT_DIRS,
    MSG_SOURCE_COUNT_MODELS,
    DiscoveredModel,
    DiscoveryReport,
    EngineCheck,
    SourceStatus,
)
from core.mt_engines.registry import DEFAULT_ENGINE
from ui import branding, runner
from ui.runner import PROJECT_ROOT, Run

APP = PROJECT_ROOT / "app.py"
REAL_PROJECT_ROOT = PROJECT_ROOT
APP_TIMEOUT_SEC = 60
BOOK_FILE = "Model Secici Sentetik.pdf"
PICKER_KEY = "mt_engine"
SCAN_KEY = "model_scan"
TRANSLATE_KEY = "model_translate"

OLLAMA_URL = "http://127.0.0.1:11434"
LMSTUDIO_URL = "http://127.0.0.1:1234/v1"


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
# Sahte keşif raporları (test verisi)
# ---------------------------------------------------------------------------

def _model(engine_id: str, label: str, source: str, backend: str, *,
           usable: bool = True, reason: str = "", size: int | None = None,
           detail: str = "", reasoning: bool | None = None) -> DiscoveredModel:
    return DiscoveredModel(engine_id=engine_id, label=label, source=source,
                           backend=backend, format_guess="chat", usable=usable,
                           reason=reason, size_bytes=size, detail=detail,
                           reasoning=reasoning)


BUILTIN = _model(DEFAULT_ENGINE, "HY-MT 7B", "builtin", "mlx", size=15_000_000_000,
                 detail="mimari: hunyuan_v1_dense")
QWEN = _model("ollama:qwen3:8b", "qwen3:8b", "ollama", "ollama", size=5_200_000_000,
              detail="8.2B · Q4_K_M", reasoning=True)
GEMMA = _model("mlx:gemma4-mlx", "gemma4-mlx", "mlx", "mlx", usable=False,
               reason=MSG_MLX_ARCH_UNSUPPORTED.format(arch="gemma4"))
GGUF = _model("gguf:qwen-q4.gguf", "qwen-q4.gguf", "gguf", "llamacpp", usable=False,
              reason=MSG_LLAMA_MISSING, size=4_900_000_000, detail="8B · Q4_K_M")
LMSTUDIO_MODEL = _model("lmstudio:qwen/qwen3-8b", "qwen/qwen3-8b", "lmstudio", "openai",
                        detail="8B · 4bit · mlx")

OLLAMA_UP = SourceStatus("ollama", "Ollama", True, MSG_SOURCE_COUNT_MODELS.format(n=1),
                         OLLAMA_URL)
OLLAMA_DOWN = SourceStatus("ollama", "Ollama", False, MSG_OLLAMA_DOWN.format(url=OLLAMA_URL),
                           OLLAMA_URL)
LMSTUDIO_DOWN = SourceStatus("lmstudio", "LM Studio", False,
                             MSG_LMSTUDIO_DOWN.format(url=LMSTUDIO_URL), LMSTUDIO_URL)
LMSTUDIO_UP = SourceStatus("lmstudio", "LM Studio", True,
                           MSG_SOURCE_COUNT_MODELS.format(n=1), LMSTUDIO_URL)
MLX_DIRS = SourceStatus("mlx", "MLX klasörleri", None, MSG_SOURCE_COUNT_DIRS.format(n=2))


def _today_at(hour: int, minute: int) -> float:
    return datetime.now().replace(hour=hour, minute=minute, second=5,
                                  microsecond=0).timestamp()


def _app_option(model: DiscoveredModel) -> str:
    """app.py'nin seçenek etiketi (K6, 2026-10-05): runner etiketinin "ad ·
    kaynak" kısmının ardına [önerilen ·] düşünme sınıfı (gerçek üst veri) girer."""
    from core.mt_engines.thinking import profile_of

    base = runner.model_option_label(model)
    head = f"{model.label} · {branding.SOURCE_LABELS[model.source]}"
    extras = [branding.MODEL_RECOMMENDED_LABEL] if model.engine_id == DEFAULT_ENGINE else []
    extras.append(branding.thinking_label(profile_of(model).cls))
    return " · ".join((head, *extras)) + base[len(head):]


def _report(models=(BUILTIN, QWEN, GEMMA, GGUF), sources=(OLLAMA_UP, LMSTUDIO_DOWN, MLX_DIRS),
            scanned_at: float | None = None) -> DiscoveryReport:
    return DiscoveryReport(models=tuple(models), sources=tuple(sources),
                           scanned_at=_today_at(9, 7) if scanned_at is None else scanned_at)


# ---------------------------------------------------------------------------
# Ortam
# ---------------------------------------------------------------------------

class _StubMemory:
    def __init__(self, *args, **kwargs):
        pass

    def get_stats(self):
        return {"total_entries": 0, "confirmed_entries": 0}

    def find_similar(self, *args, **kwargs):
        return []

    def close(self):
        pass


class _Discovery:
    """Sahte keşif: sıradaki raporu döndürür (son rapor tekrar eder), çağrıları sayar."""

    def __init__(self):
        self.reports: list = [_report()]
        self.calls = 0

    def __call__(self):
        self.calls += 1
        item = self.reports[min(self.calls, len(self.reports)) - 1]
        if isinstance(item, BaseException):
            raise item
        return item


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
def project(tmp_path, monkeypatch):
    root = tmp_path / "proje"
    (root / "output").mkdir(parents=True)
    monkeypatch.setattr(runner, "PROJECT_ROOT", root)
    return root


@pytest.fixture
def env(monkeypatch, tmp_path, project, mt_isolation):
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
    discovery, checker = _Discovery(), _Checker()
    monkeypatch.setattr(runner, "discover_models", discovery)
    monkeypatch.setattr(runner, "engine_check", checker)
    started: list[list[str]] = []

    def fake_start(self):
        # CLI koşturulmaz; komut kaydedilir. Süreç "koşuyor" görünür: poll()
        # hep None döner (iş koşarken kartın durumu sınanabilsin).
        started.append(list(self.command))
        self.started_at = time.time()
        self._process = SimpleNamespace(poll=lambda: None, pid=-1)

    monkeypatch.setattr(runner.Run, "start", fake_start)
    return SimpleNamespace(store=store, tmp=tmp_path, project=project,
                           discovery=discovery, checker=checker, started=started,
                           selection=mt_isolation.selection_path)


def _book(folder: Path) -> Path:
    import fitz

    path = folder / BOOK_FILE
    with fitz.open() as doc:
        doc.new_page()
        doc.save(str(path))
    return path


def _slot(book: Path | None, run: Run | None = None) -> dict:
    """Eski biçimli iş yuvası: model alanları YOK (app onları .get() ile okur)."""
    key = (book.name, book.stat().st_size) if book is not None else None
    return {"book": book, "upload_key": key, "run": run,
            "run_book": book if run is not None else None, "run_name": None,
            "name": None}


def _finished(kind: str, returncode: int, last_error: str = "",
              lines: tuple[str, ...] = ()) -> Run:
    run = Run(kind, [sys.executable, "-c", "pass"])
    run.started_at = time.time() - 30
    run.finished_at = time.time()
    run.returncode = returncode
    run.last_error = last_error
    for line in lines:
        run._handle_stdout_line(line)
    return run


def _run_app() -> AppTest:
    at = AppTest.from_file(str(APP), default_timeout=APP_TIMEOUT_SEC)
    at.run()
    assert not at.exception, at.exception
    return at


def _values(elements) -> list[str]:
    return [element.value for element in elements]


def _buttons(at: AppTest) -> dict:
    return {button.key: button for button in at.button}


def _with_book(env) -> Path:
    book = _book(env.tmp)
    env.store["_job"] = _slot(book)
    return book


# ---------------------------------------------------------------------------
# 1. translate_command
# ---------------------------------------------------------------------------

class TestTranslateCommand:
    BOOK = Path("/kitaplar/a.pdf")

    def _base(self) -> list[str]:
        return [sys.executable, str(runner.PROJECT_ROOT / "translate_book.py"), str(self.BOOK)]

    def test_varsayilan_motorda_komut_degismez(self):
        assert runner.translate_command(self.BOOK) == self._base()
        assert len(runner.translate_command(self.BOOK)) == 3
        assert runner.translate_command(self.BOOK, engine=None) == self._base()
        assert runner.translate_command(self.BOOK, engine=DEFAULT_ENGINE) == self._base()

    def test_baska_motorda_bayrak_sonda(self):
        assert runner.translate_command(self.BOOK, engine="ollama:x") == [
            *self._base(), "--mt-engine", "ollama:x"]
        assert runner.translate_command(self.BOOK, "Ad", engine="ollama:x") == [
            *self._base(), "--name", "Ad", "--mt-engine", "ollama:x"]

    def test_tire_ile_baslayan_kimlik_esittir_ile(self):
        assert runner.translate_command(self.BOOK, engine="-x")[-1] == "--mt-engine=-x"

    @pytest.mark.parametrize("engine", ["ollama:qwen3:8b", "-garip-ad",
                                        "openai:http://127.0.0.1:8080/v1|qwen"])
    def test_cli_bayragi_aynen_okur(self, monkeypatch, engine):
        import translate_book

        command = runner.translate_command(self.BOOK, engine=engine)
        monkeypatch.setattr(sys, "argv", command[1:])
        assert translate_book.parse_args().mt_engine == engine

    def test_hedef_dil_bayragi_yok(self):
        """Hedef dil yalnız Türkçe: komuta dil seçeneği eklenmez."""
        command = runner.translate_command(self.BOOK, "Ad", engine="ollama:x")
        assert not [part for part in command if "lang" in part or "dil" in part]


# ---------------------------------------------------------------------------
# 2. Seçim dosyası
# ---------------------------------------------------------------------------

class TestChoiceFile:
    def test_gercek_yol_config_altinda_ve_git_disi(self, mt_isolation):
        real = mt_isolation.original_engine_choice_path
        assert real is not None
        assert real() == runner.PROJECT_ROOT / "config" / "mt_selection.json"
        # Testte gerçek dosya kullanılmaz: yol tmp'ye yamalı.
        assert runner.engine_choice_path() == mt_isolation.selection_path
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", "config/mt_selection.json"],
            cwd=REAL_PROJECT_ROOT, capture_output=True)
        assert ignored.returncode == 0, "config/mt_selection.json git dışı olmalı"

    def test_gidis_donus(self, mt_isolation):
        assert runner.load_engine_choice() is None           # dosya yok
        assert runner.save_engine_choice("ollama:qwen3:8b") is True
        assert runner.load_engine_choice() == "ollama:qwen3:8b"
        data = json.loads(mt_isolation.selection_path.read_text(encoding="utf-8"))
        assert data["engine"] == "ollama:qwen3:8b"
        datetime.fromisoformat(data["saved_at"])            # gerçek zaman damgası

    @pytest.mark.parametrize("content", [
        "{bozuk", "", "[]", '{"engine": 3}', '{"engine": "   "}', '{"motor": "x"}',
        "￾\x00"])
    def test_bozuk_dosya_none(self, mt_isolation, content):
        mt_isolation.selection_path.write_text(content, encoding="utf-8")
        assert runner.load_engine_choice() is None

    def test_atomik_yazim_yarim_dosya_birakmaz(self, mt_isolation, monkeypatch):
        path = mt_isolation.selection_path
        assert runner.save_engine_choice("mlx:eski") is True
        before = path.read_bytes()

        def broken_dump(obj, stream, **kwargs):
            stream.write('{"engine": "yar')             # yarım yazılmış içerik
            raise OSError("disk dolu")

        monkeypatch.setattr(runner.json, "dump", broken_dump)
        assert runner.save_engine_choice("ollama:yeni") is False
        monkeypatch.undo()
        assert path.read_bytes() == before
        assert runner.load_engine_choice() == "mlx:eski"
        assert list(path.parent.glob(f".{path.stem}-*")) == []   # geçici dosya kalmadı

    def test_replace_basarisizsa_gecici_dosya_silinir(self, mt_isolation, monkeypatch):
        path = mt_isolation.selection_path

        def no_replace(src, dst):
            raise PermissionError("izin yok")

        monkeypatch.setattr(runner.os, "replace", no_replace)
        assert runner.save_engine_choice("ollama:x") is False
        assert not path.exists()
        assert list(path.parent.glob(f".{path.stem}-*")) == []

    def test_yazilamayan_konum_false(self, monkeypatch, tmp_path):
        blocker = tmp_path / "dosya"
        blocker.write_text("klasör değil", encoding="utf-8")
        monkeypatch.setattr(runner, "engine_choice_path",
                            lambda: blocker / "mt_selection.json")
        assert runner.save_engine_choice("ollama:x") is False

    @pytest.mark.skipif(os.geteuid() == 0, reason="root her yere yazar")
    def test_salt_okunur_klasor_false(self, monkeypatch, tmp_path):
        folder = tmp_path / "salt_okunur"
        folder.mkdir()
        folder.chmod(0o500)
        try:
            monkeypatch.setattr(runner, "engine_choice_path",
                                lambda: folder / "mt_selection.json")
            assert runner.save_engine_choice("ollama:x") is False
            assert list(folder.iterdir()) == []
        finally:
            folder.chmod(0o700)


# ---------------------------------------------------------------------------
# Yardımcılar: etiket, hazır olma, keşfe devretme
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_kaynak_adlari_sozlesmeyle_ayni(self):
        assert set(branding.SOURCE_LABELS) == set(DISCOVERY_SOURCE_ORDER)
        assert "Yol" not in " ".join(branding.SOURCE_LABELS.values())

    def test_secenek_etiketi(self):
        assert runner.model_option_label(BUILTIN) == (
            "HY-MT 7B · yerleşik · mimari: hunyuan_v1_dense · 15 GB")
        assert runner.model_option_label(QWEN) == "qwen3:8b · Ollama · 8.2B · Q4_K_M · 5,2 GB"
        # Ayrıntı ve boyut yoksa yazılmaz (uydurulmaz); kullanılamaz sonda.
        assert runner.model_option_label(GEMMA) == "gemma4-mlx · MLX · kullanılamaz"
        assert runner.model_option_label(GGUF).endswith(" · 4,9 GB · kullanılamaz")

    def test_hazir_olma_varsayilan_motor_klasore_bakar(self, monkeypatch):
        unusable_builtin = _model(DEFAULT_ENGINE, "HY-MT 7B", "builtin", "mlx",
                                  usable=False, reason="rapordaki neden")
        monkeypatch.setattr(runner, "local_model_dir", lambda: Path("/model"))
        # Varsayılan motor: rapordan bağımsız, model klasörü kararı verir.
        assert runner.engine_ready(DEFAULT_ENGINE, None) == (True, "")
        assert runner.engine_ready(DEFAULT_ENGINE, _report((unusable_builtin,))) == (True, "")
        monkeypatch.setattr(runner, "local_model_dir", lambda: None)
        assert runner.engine_ready(DEFAULT_ENGINE, _report((unusable_builtin,))) == (
            False, "rapordaki neden")
        ready, reason = runner.engine_ready(DEFAULT_ENGINE, None)
        assert not ready and reason.startswith("Çeviri modeli kurulu değil: ")
        assert "docs/KURULUM_VE_KULLANIM.md" in reason

    def test_hazir_olma_diger_motorlar_rapordan(self, monkeypatch):
        monkeypatch.setattr(runner, "local_model_dir", lambda: None)
        report = _report()
        assert runner.engine_ready(QWEN.engine_id, report) == (True, "")
        assert runner.engine_ready(GEMMA.engine_id, report) == (False, GEMMA.reason)
        assert runner.engine_ready("ollama:yok", report) == (
            False, runner.ENGINE_NOT_SCANNED_REASON)
        assert runner.engine_ready(QWEN.engine_id, None) == (
            False, runner.ENGINE_NOT_SCANNED_REASON)

    def test_sade_ad(self):
        report = _report()
        assert runner.engine_label(QWEN.engine_id, report) == "qwen3:8b"
        assert runner.engine_label("ollama:yok", report) == "ollama:yok"
        assert runner.engine_label(DEFAULT_ENGINE, None) == DEFAULT_ENGINE

    def test_kesif_ve_kontrol_discovery_modulune_devredilir(self, monkeypatch):
        """runner yalnız ince sarmalayıcı: keşif mantığı discovery.py'de."""
        fake = ModuleType(runner.DISCOVERY_MODULE)
        calls = []
        report = _report()
        fake.discover_all = lambda *a, **k: calls.append(("discover_all", a, k)) or report
        check = EngineCheck("ollama:x", False, "x", "neden", "ipucu")
        fake.check_engine = lambda *a, **k: calls.append(("check_engine", a, k)) or check
        monkeypatch.setitem(sys.modules, runner.DISCOVERY_MODULE, fake)
        # Testin gerçek sarmalayıcıyı çağırdığından emin ol (env yaması yok).
        assert runner.discover_models() is report
        assert runner.engine_check("ollama:x") is check
        assert calls == [("discover_all", (), {}), ("check_engine", ("ollama:x",), {})]


# ---------------------------------------------------------------------------
# 3. Seçici seçenekleri, kullanılamaz model
# ---------------------------------------------------------------------------

class TestPicker:
    def test_secenekler_rapordaki_modellerle_birebir(self, env):
        _with_book(env)
        at = _run_app()
        picker = at.selectbox(key=PICKER_KEY)
        report = env.discovery.reports[0]
        assert picker.label == branding.MODEL_PICKER_LABEL
        assert picker.options == [_app_option(m) for m in report.models]
        assert picker.value == DEFAULT_ENGINE
        assert not _buttons(at)[TRANSLATE_KEY].disabled
        # Hedef dil seçeneği yok (KARARLAR 11). 2026-09-29 (V2 denetimi):
        # KARARLAR 19 ile kartta KAYNAK dil seçicisi de var; sayfadaki
        # seçiciler model ve kaynak dil, hiçbiri hedef dil değil.
        labels = [box.label for box in at.selectbox]
        assert labels == [branding.MODEL_PICKER_LABEL, "Kaynak dil"]
        assert not any("hedef" in label.casefold() for label in labels)
        assert not at.radio

    def test_kullanilamaz_model_secilince_cevir_kapali_neden_yazili(self, env):
        _with_book(env)
        at = _run_app()
        at.selectbox(key=PICKER_KEY).select(GEMMA.engine_id).run()
        assert not at.exception
        assert at.selectbox(key=PICKER_KEY).value == GEMMA.engine_id
        assert _buttons(at)[TRANSLATE_KEY].disabled
        assert GEMMA.reason in _values(at.caption)
        assert not at.error and not at.warning
        # Seçim iş yuvasına ve dosyaya yazıldı (kullanıcı seçti).
        assert env.store["_job"]["engine"] == GEMMA.engine_id
        assert runner.load_engine_choice() == GEMMA.engine_id

    def test_ayni_etiketli_iki_model_kimlikle_ayrilir(self, env):
        twin_a = _model("openai:http://127.0.0.1:8080/v1|qwen", "qwen", "openai", "openai")
        twin_b = _model("openai:http://127.0.0.1:8081/v1|qwen", "qwen", "openai", "openai")
        env.discovery.reports = [_report((BUILTIN, twin_a, twin_b), ())]
        _with_book(env)
        at = _run_app()
        options = at.selectbox(key=PICKER_KEY).options
        assert len(set(options)) == 3
        assert options[1].endswith(twin_a.engine_id) and options[2].endswith(twin_b.engine_id)
        at.selectbox(key=PICKER_KEY).select(twin_b.engine_id).run()
        assert at.selectbox(key=PICKER_KEY).value == twin_b.engine_id

    def test_dusunen_model_notu_yalniz_gercek_ust_veriden(self, env):
        _with_book(env)
        at = _run_app()
        assert C["MODEL_REASONING_CAPTION"] not in _values(at.caption)   # HY-MT: None
        at.selectbox(key=PICKER_KEY).select(QWEN.engine_id).run()
        assert C["MODEL_REASONING_CAPTION"] in _values(at.caption)       # reasoning=True

    def test_kenar_cubugu_secili_model_ve_durumu(self, env):
        _with_book(env)
        at = _run_app()
        assert C["SIDEBAR_MODEL_CAPTION"].format(
            label="HY-MT 7B", state=branding.MODEL_READY_LABEL) in _values(at.sidebar.caption)
        at.selectbox(key=PICKER_KEY).select(GEMMA.engine_id).run()
        assert C["SIDEBAR_MODEL_CAPTION"].format(
            label=GEMMA.label, state=branding.MODEL_UNUSABLE_LABEL) in _values(at.sidebar.caption)

    def test_secim_kaydedilemezse_nedeni_yazili(self, env, monkeypatch, tmp_path):
        blocker = tmp_path / "engel"
        blocker.write_text("klasör değil", encoding="utf-8")
        target = blocker / "mt_selection.json"
        monkeypatch.setattr(runner, "engine_choice_path", lambda: target)
        _with_book(env)
        at = _run_app()
        at.selectbox(key=PICKER_KEY).select(QWEN.engine_id).run()
        assert C["MODEL_CHOICE_SAVE_FAILED"].format(path=target) in _values(at.caption)
        # Seçim yine bu oturumda geçerli.
        assert at.selectbox(key=PICKER_KEY).value == QWEN.engine_id


# ---------------------------------------------------------------------------
# 4. "Modelleri tara"
# ---------------------------------------------------------------------------

class TestScanButton:
    def test_tiklaninca_yeniden_taranir_ve_zaman_gercek(self, env):
        first = _report(scanned_at=_today_at(9, 7))
        second = _report(models=(BUILTIN, QWEN, LMSTUDIO_MODEL),
                         sources=(OLLAMA_UP, LMSTUDIO_UP), scanned_at=_today_at(10, 41))
        env.discovery.reports = [first, second]
        _with_book(env)
        at = _run_app()
        assert env.discovery.calls == 1
        assert C["MODEL_LAST_SCAN"].format(time="09:07") in _values(at.caption)
        scan = _buttons(at)[SCAN_KEY]
        assert scan.label == branding.MODEL_SCAN_BUTTON
        # K6 (2026-10-05): açıklamada taranan klasörün gerçek yolu.
        from core.mt_engines import registry
        assert scan.help == branding.model_scan_help(registry.MODELS_DIR)
        scan.click().run()
        assert not at.exception
        assert env.discovery.calls == 2
        captions = _values(at.caption)
        assert C["MODEL_LAST_SCAN"].format(time="10:41") in captions
        assert C["MODEL_LAST_SCAN"].format(time="09:07") not in captions
        assert at.selectbox(key=PICKER_KEY).options == [_app_option(m) for m in second.models]
        assert env.store["_job"]["discovery"] is second

    def test_eski_tarama_tarihiyle_yazilir(self, env):
        yesterday = datetime.now() - timedelta(days=1)
        env.discovery.reports = [_report(scanned_at=yesterday.timestamp())]
        _with_book(env)
        at = _run_app()
        assert C["MODEL_LAST_SCAN"].format(
            time=yesterday.strftime("%d.%m.%Y %H:%M")) in _values(at.caption)

    def test_tarama_coker_arayuz_dusmez(self, env):
        env.discovery.reports = [RuntimeError("keşif patladı")]
        _with_book(env)
        at = _run_app()
        assert not at.error and not at.warning
        assert C["MODEL_SCAN_FAILED"].format(error="keşif patladı") in _values(at.caption)
        # Model uydurulmaz: yalnız varsayılan motor seçilebilir.
        picker = at.selectbox(key=PICKER_KEY)
        assert [picker.value] == [DEFAULT_ENGINE] and len(picker.options) == 1
        # K6 (2026-10-05): önerilen model işaretli; model bilgisi yok, düşünme
        # sınıfı yazılmaz.
        assert picker.options[0] == (f"{DEFAULT_ENGINE} · {branding.SOURCE_LABELS['builtin']}"
                                     f" · {branding.MODEL_RECOMMENDED_LABEL}")
        # Çöken tarama her yeniden çizimde tekrarlanmaz; düğme yeniden dener.
        at.run()
        assert env.discovery.calls == 1
        env.discovery.reports = [RuntimeError("keşif patladı"), _report()]
        _buttons(at)[SCAN_KEY].click().run()
        assert env.discovery.calls == 2
        assert len(at.selectbox(key=PICKER_KEY).options) == 4


# ---------------------------------------------------------------------------
# 5. Kapalı sunucu
# ---------------------------------------------------------------------------

class TestServerDown:
    def test_kapali_sunucu_caption_secenek_yok_uyari_listesi_degismez(self, env):
        _with_book(env)
        baseline_report = _report(sources=())
        env.discovery.reports = [baseline_report]
        baseline = _run_app()
        base_warnings = _values(baseline.warning)

        env.store.clear()
        _with_book(env)
        down = _report(models=(BUILTIN, GEMMA), sources=(OLLAMA_DOWN, LMSTUDIO_DOWN, MLX_DIRS))
        env.discovery.reports = [down]
        at = _run_app()
        captions = _values(at.caption)
        # İleti kaynak adıyla başlıyor: ad yinelenmez.
        assert OLLAMA_DOWN.message in captions and LMSTUDIO_DOWN.message in captions
        assert f"MLX klasörleri: {MLX_DIRS.message}" in captions
        # Kapalı sunucudan tek seçenek yok (uydurulmaz).
        options = at.selectbox(key=PICKER_KEY).options
        assert not [o for o in options if "Ollama" in o or "LM Studio" in o]
        assert _values(at.warning) == base_warnings
        assert not at.error and not at.info


# ---------------------------------------------------------------------------
# 6. Kaydedilmiş seçim
# ---------------------------------------------------------------------------

class TestSavedChoice:
    def test_kayitli_secim_geri_yuklenir(self, env):
        assert runner.save_engine_choice(QWEN.engine_id)
        _with_book(env)
        at = _run_app()
        assert at.selectbox(key=PICKER_KEY).value == QWEN.engine_id
        assert env.store["_job"]["engine"] == QWEN.engine_id
        assert not [c for c in _values(at.caption) if "bu taramada bulunamadı" in c]

    def test_is_yuvasindaki_secim_dosyadan_once(self, env):
        assert runner.save_engine_choice(QWEN.engine_id)
        book = _book(env.tmp)
        env.store["_job"] = {**_slot(book), "engine": GEMMA.engine_id}
        at = _run_app()
        assert at.selectbox(key=PICKER_KEY).value == GEMMA.engine_id

    def test_raporda_yoksa_varsayilana_duser_dosya_ezilmez(self, env):
        assert runner.save_engine_choice("lmstudio:qwen/qwen3-8b")
        before = (env.selection.read_bytes(), env.selection.stat().st_mtime_ns)
        _with_book(env)
        at = _run_app()
        note = C["MODEL_CHOICE_MISSING_NOTE"].format(engine_id="lmstudio:qwen/qwen3-8b")
        assert at.selectbox(key=PICKER_KEY).value == DEFAULT_ENGINE
        assert note in _values(at.caption)
        at.run()                                        # yeniden çizim: not kalır
        assert note in _values(at.caption)
        assert (env.selection.read_bytes(), env.selection.stat().st_mtime_ns) == before
        assert runner.load_engine_choice() == "lmstudio:qwen/qwen3-8b"

    def test_yeni_taramada_bulunursa_secim_geri_gelir(self, env):
        assert runner.save_engine_choice(LMSTUDIO_MODEL.engine_id)
        env.discovery.reports = [
            _report(sources=(LMSTUDIO_DOWN,)),
            _report(models=(BUILTIN, LMSTUDIO_MODEL), sources=(LMSTUDIO_UP,))]
        _with_book(env)
        at = _run_app()
        assert at.selectbox(key=PICKER_KEY).value == DEFAULT_ENGINE
        _buttons(at)[SCAN_KEY].click().run()
        assert at.selectbox(key=PICKER_KEY).value == LMSTUDIO_MODEL.engine_id
        assert not [c for c in _values(at.caption) if "bu taramada bulunamadı" in c]

    def test_kullanici_baska_secim_yapinca_dosya_yazilir(self, env):
        assert runner.save_engine_choice("lmstudio:yok")
        _with_book(env)
        at = _run_app()
        at.selectbox(key=PICKER_KEY).select(QWEN.engine_id).run()
        assert runner.load_engine_choice() == QWEN.engine_id
        assert not [c for c in _values(at.caption) if "bu taramada bulunamadı" in c]


# ---------------------------------------------------------------------------
# 7. "Çevir": son kontrol
# ---------------------------------------------------------------------------

class TestTranslateButton:
    def test_kontrol_basarisizsa_hata_ve_is_baslamaz(self, env):
        env.checker.result = EngineCheck(QWEN.engine_id, False, QWEN.label,
                                         reason=MSG_OLLAMA_DOWN.format(url=OLLAMA_URL),
                                         hint="ollama serve")
        _with_book(env)
        at = _run_app()
        at.selectbox(key=PICKER_KEY).select(QWEN.engine_id).run()
        _buttons(at)[TRANSLATE_KEY].click().run()
        assert not at.exception
        assert env.checker.calls == [QWEN.engine_id]
        assert _values(at.error) == [MSG_OLLAMA_DOWN.format(url=OLLAMA_URL)]
        assert "ollama serve" in _values(at.caption)
        assert env.started == []
        assert env.store["_job"]["run"] is None

    def test_kontrol_basariliysa_komutta_motor(self, env):
        book = _with_book(env)
        at = _run_app()
        at.selectbox(key=PICKER_KEY).select(QWEN.engine_id).run()
        _buttons(at)[TRANSLATE_KEY].click().run()
        assert not at.exception
        assert env.checker.calls == [QWEN.engine_id]
        assert env.started == [runner.translate_command(book, engine=QWEN.engine_id)]
        assert env.started[0][-2:] == ["--mt-engine", QWEN.engine_id]
        assert env.store["_job"]["run_engine"] == QWEN.engine_id

    def test_varsayilan_motorda_bayrak_yok(self, env):
        book = _with_book(env)
        at = _run_app()
        _buttons(at)[TRANSLATE_KEY].click().run()
        assert env.checker.calls == [DEFAULT_ENGINE]
        assert env.started == [runner.translate_command(book)]
        assert env.store["_job"]["run_engine"] == DEFAULT_ENGINE

    def test_kontrol_cokerse_is_baslamaz(self, env, monkeypatch):
        def boom(engine_id):
            raise RuntimeError("kontrol patladı")

        monkeypatch.setattr(runner, "engine_check", boom)
        _with_book(env)
        at = _run_app()
        _buttons(at)[TRANSLATE_KEY].click().run()
        assert _values(at.error) == [C["MODEL_CHECK_FAILED"].format(error="kontrol patladı")]
        assert env.started == []


# ---------------------------------------------------------------------------
# 8. Çıkış kodu 5
# ---------------------------------------------------------------------------

class TestModelUnavailableExit:
    def test_cikis_kodu_cli_dan_okunur(self):
        import translate_book

        assert runner.EXIT_MODEL_UNAVAILABLE == translate_book.EXIT_MODEL_UNAVAILABLE == 5
        source = (REAL_PROJECT_ROOT / "ui" / "runner.py").read_text(encoding="utf-8")
        assert "EXIT_MODEL_UNAVAILABLE = 5" not in source      # kopya tutulmaz

    def test_cikis_5_iletisi_ve_cikti_kuyrugu(self, env):
        book = _book(env.tmp)
        reason = MSG_OLLAMA_DOWN.format(url=OLLAMA_URL)
        run = _finished("translate", 5, last_error=reason,
                        lines=("Phase 1: ...", f"HATA: {reason}"))
        env.store["_job"] = {**_slot(book, run), "run_engine": QWEN.engine_id}
        at = _run_app()
        assert _values(at.error) == [branding.MODEL_UNAVAILABLE_MESSAGE]
        assert branding.quoted(branding.MODEL_SCAN_BUTTON) in branding.MODEL_UNAVAILABLE_MESSAGE
        assert reason in _values(at.caption)
        assert at.expander[0].label == "Son çıktı"
        assert f"HATA: {reason}" in at.code[0].value
        assert "PDF üretilemedi" not in " ".join(_values(at.error))

    def test_cikarmada_5_siradan_hata(self, env):
        book = _book(env.tmp)
        env.store["_job"] = _slot(book, _finished("extract", 5))
        at = _run_app()
        assert branding.MODEL_UNAVAILABLE_MESSAGE not in _values(at.error)


# ---------------------------------------------------------------------------
# 9. İş koşarken
# ---------------------------------------------------------------------------

class TestBusy:
    def test_kosarken_secici_ve_tarama_kapali_ilerlemede_model(self, env):
        _with_book(env)
        at = _run_app()
        at.selectbox(key=PICKER_KEY).select(QWEN.engine_id).run()
        _buttons(at)[TRANSLATE_KEY].click().run()
        assert not at.exception
        assert env.store["_job"]["run"].running
        assert at.selectbox(key=PICKER_KEY).disabled
        buttons = _buttons(at)
        assert buttons[SCAN_KEY].disabled and buttons[TRANSLATE_KEY].disabled
        assert "job_cancel" in buttons
        assert C["MODEL_RUN_CAPTION"].format(label=QWEN.label) in _values(at.caption)
        calls = env.discovery.calls
        at.run()                                  # koşarken tarama yapılmaz
        assert env.discovery.calls == calls

    def test_kosarken_ilk_tarama_yapilmaz(self, env):
        book = _book(env.tmp)
        run = Run("translate", [sys.executable, "-c", "pass"])
        run.start()                               # sahte: koşuyor görünür
        env.store["_job"] = _slot(book, run)
        at = _run_app()
        assert env.discovery.calls == 0
        assert at.selectbox(key=PICKER_KEY).disabled
        # Koşunun modeli bilinmiyorsa (eski yuva) "Model:" satırı uydurulmaz.
        assert not [c for c in _values(at.caption) if c.startswith("Model: ")]


# ---------------------------------------------------------------------------
# 10. İlk açılış taraması
# ---------------------------------------------------------------------------

class TestFirstScan:
    def test_ilk_acilista_bir_kez_yeniden_cizimde_tekrar_yok(self, env):
        _with_book(env)
        at = _run_app()
        assert env.discovery.calls == 1
        at.run()
        at.run()
        assert env.discovery.calls == 1
        assert env.store["_job"]["discovery"] is env.discovery.reports[0]

    def test_kitap_yokken_de_kenar_cubugu_icin_taranir(self, env):
        at = _run_app()
        assert env.discovery.calls == 1
        assert C["SIDEBAR_MODEL_CAPTION"].format(
            label="HY-MT 7B", state=branding.MODEL_READY_LABEL) in _values(at.sidebar.caption)
        assert not at.selectbox                   # seçici kitap kartında

    def test_yeni_oturum_yuvadaki_raporu_kullanir(self, env):
        _with_book(env)
        _run_app()
        _run_app()                                # sayfa yenilendi: yeni oturum
        assert env.discovery.calls == 1


# ---------------------------------------------------------------------------
# Açılır liste genişliği (28 Eylül 2026)
# ---------------------------------------------------------------------------

class TestPickerMenuWidth:
    """Kart dar: açılır listede etiket (ad · kaynak · ayrıntı · boyut) kesiliyor,
    kaynak ve boyut hiç görünmüyordu (Playwright, 1280 piksel: liste 207 piksel,
    metin 197 pikselde kesik). Liste seçiciden geniş açılır; ekran genişliğini
    aşmaz. Gerçek tarayıcı ölçümü K3 raporunda (1280/1024/760 pikselde kesik yok).
    """

    RULE_SELECTOR = ('[data-baseweb="popover"] div:has(> '
                     '[data-testid="stSelectboxVirtualDropdown"])')

    def test_liste_kurali_css_blogunda(self):
        css = C["CSS"]
        assert self.RULE_SELECTOR in css
        body = css.split(self.RULE_SELECTOR, 1)[1].split("}", 1)[0]
        # K6 (2026-10-05): 48rem — etikete önerilen işareti ve düşünme sınıfı
        # eklendi, 36rem'de ayrıntı ve boyut kesiliyordu (ekran görüntüsüyle).
        assert "min-width: min(48rem, calc(100vw - 2rem));" in body
        # Metni alt satıra kaydırmak sanal listede satırları bindirir: yok.
        assert "white-space" not in body

    def test_kural_uygulamanin_stilinde(self, env):
        _with_book(env)
        at = _run_app()
        styles = [m.body for m in at.markdown if "<style>" in m.body]
        assert any(self.RULE_SELECTOR in body for body in styles)


# ---------------------------------------------------------------------------
# Kaynak metinleri: TDK, adlar
# ---------------------------------------------------------------------------

class TestTexts:
    def test_turkce_ve_yol_adi_yok(self):
        texts = [branding.MODEL_PICKER_LABEL, branding.MODEL_SCAN_BUTTON,
                 branding.MODEL_SCAN_HELP, branding.MODEL_UNAVAILABLE_MESSAGE,
                 branding.MODEL_REASONING_NOTE, runner.ENGINE_NOT_SCANNED_REASON,
                 C["MODEL_CHOICE_MISSING_NOTE"], C["MODEL_CHOICE_SAVE_FAILED"],
                 C["MODEL_SCAN_SPINNER"], *branding.SOURCE_LABELS.values()]
        joined = " ".join(texts)
        for banned in ("Yol A", "Yol B", "ATLAS", "VECTOR", "...", "hedef dil"):
            assert banned not in joined
        # KARARLAR 1: yalnız models/ (K6: açıklama taranan klasörün yolunu yazar)
        scan_help = branding.model_scan_help(Path("/veri") / "models")
        assert "/veri/models/ klasöründeki" in scan_help
        assert ".lmstudio" not in scan_help
