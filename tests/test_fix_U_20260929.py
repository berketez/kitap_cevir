"""Kulvar U (arayüz) düzeltmeleri — 29 Eylül 2026 bug avı dalgası.

Bulgular (bug avı, 2026-09-28):
  F5-01 koşan iş varken ikinci iş başlıyordu (bayat sekmeden tıklama); ilk iş
        yuvadan düşüyor, İptal ona ulaşmıyordu;
  F5-02 derleme çıkış 0'da TeX hatası / eksik glif / EPUB kapsamı uyarıları
        arayüzde görünmüyordu;
  F5-03 yerel model sonucunda Türkçe EPUB sunulmuyor, çıkış 3'te puan ve QC
        raporu yoktu;
  F5-04 arayüz sunucusu kapanınca CLI işi yetim kalıyor, girdisi siliniyor,
        olay dizinleri sızıyordu;
  F5-06 '-' ile başlayan Kitap adı argparse'ı düşürüyor, arayüz "XeLaTeX
        derlemesi başarısız" diyordu;
  F5-07 yerel model çevirisinde gerçek sayaç ("[BATCH] Translated 40/472
        segments") ilerleme sayılmıyordu;
  F5-08 faz satırı İngilizce günlük metniydi; sanitizer'ın "Phase 0.6" kaydı
        faz sanılıyordu;
  F5-13 bitmiş ama yoklanmamış işte İptal -> "İş iptal edildi.";
  KARAR 19 "Yerel model ile çevir" kartında kaynak dil: tespit + güven, açılır
        listeden değiştirme, belirsizse "Çevir"den önce seçim;
  F1-03 (arayüz tarafı) kitap.json / --json "failed_pages" görünür uyarı.

Girdiler sentetiktir; çıktı klasörleri tmp_path'e saptırılır, CLI yalnız
(F5-04, F5-06) küçük gerçek alt süreçlerle denenir. Model yüklenmez, gerçek
Ollama / LM Studio sunucusuna bağlanılmaz.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import streamlit
from streamlit.testing.v1 import AppTest

import core.memory
from core.events import JsonlEventHandler, classify
from core.lang_detect import GATE_STOP_CONFIDENCE, TEX_SOURCE_LANGS
from core.mt_engines.contracts import DiscoveryReport, EngineCheck
from ui import branding, runner
from ui.runner import DEFAULT_ENGINE, PROJECT_ROOT, Run

APP = PROJECT_ROOT / "app.py"
APP_TIMEOUT_SEC = 60
_REAL_START = Run.start
TRANSLATE_KEY = "model_translate"
SOURCE_LANG_LABEL = "Kaynak dil"

# 2_derle'nin gerçek CLI uyarı satırları (S ve Vson kulvarlarının 28 Eylül
# koşuları; bugav/F5-arayuz/veri/derle_*_exit0_*.stdout.txt), "UYARI: " sonrası.
CLI_TEX_WARNING = ("Derleme günlüğünde 1 TeX hatası var; PDF'de bozuk dizgi ya da "
                   "boş kutu (□) olabilir.")
CLI_EPUB_WARNING = "EPUB'da 1/108 blok kaynak dilde kaldı (kapsam %99,1)."

DE_TEXT = ("Der Planet ist nicht weit von der Sonne entfernt, und die Bahn des "
           "Mondes ist mit dem Auge zu sehen. ")
EN_TEXT = ("The planet is not far from the sun, and the orbit of the moon can be "
           "seen with the naked eye. ")
ZH_TEXT = "这是一个关于行星运动的书，我们在这里讨论天体的轨道。"


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


class _Alive:
    """Koşan bir sürecin yerine: poll() None (iş sürüyor)."""
    pid = 999999

    def poll(self):
        return None


@pytest.fixture
def env(monkeypatch, tmp_path):
    root = tmp_path / "proje"
    (root / "output").mkdir(parents=True)
    monkeypatch.setattr(runner, "PROJECT_ROOT", root)
    store: dict = {}

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
    monkeypatch.setattr(runner, "platform_problem", lambda: None)
    monkeypatch.setattr(runner, "discover_models",
                        lambda: DiscoveryReport(models=(), sources=(),
                                                scanned_at=time.time()))
    # Donanım ve model hazırlığı bu testlerin konusu değil: kart açık, model hazır.
    monkeypatch.setattr(runner, "hardware_gate",
                        lambda: SimpleNamespace(allowed=True, reason="", warning=""))
    monkeypatch.setattr(runner, "engine_ready", lambda engine, report: (True, ""))
    monkeypatch.setattr(runner, "engine_check",
                        lambda engine: EngineCheck(engine_id=engine, ok=True, label=engine))
    monkeypatch.setattr(runner, "memory_batch_note", lambda engine, report: "")
    temp_root = tmp_path / "gecici"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    started: list[list[str]] = []
    busy_at_start: list[bool] = []

    def fake_start(self):
        started.append(list(self.command))
        previous = store.get("_job", {}).get("run")
        busy_at_start.append(bool(previous is not None and previous.running))
        self.started_at = time.time()

    monkeypatch.setattr(runner.Run, "start", fake_start)
    return SimpleNamespace(store=store, root=root, tmp=tmp_path, temp_root=temp_root,
                           started=started, busy_at_start=busy_at_start,
                           monkeypatch=monkeypatch)


def _pdf(folder: Path, name: str = "Sentetik Kitap.pdf") -> Path:
    import fitz

    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    with fitz.open() as doc:
        doc.new_page()
        doc.save(str(path))
    return path


def _epub(folder: Path, text: str, name: str = "Sentetik Kitap.epub") -> Path:
    """Gömülü metni olan en küçük EPUB (dil ön kapısının okuduğu XHTML)."""
    path = folder / name
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("OEBPS/bolum1.xhtml",
                         "<html><head><title>b</title></head><body>"
                         f"<p>{text}</p></body></html>")
    return path


def _slot(book: Path | None, run: Run | None = None, name: str | None = None,
          **extra) -> dict:
    slot = {"book": book, "upload_key": (book.name, book.stat().st_size) if book else None,
            "run": run, "run_book": book if run is not None else None,
            "run_name": name if run is not None else None, "name": name,
            "upload_nonce": 0, "discovery": None, "discovery_error": None,
            "engine": None, "engine_missing": None, "run_engine": None}
    slot.update(extra)
    return slot


def _finished(kind: str, returncode: int, result: dict | None = None,
              lines: tuple[str, ...] = (), started_ago: float = 60.0) -> Run:
    run = Run(kind, [sys.executable, "-c", "pass"])
    run.started_at = time.time() - started_ago
    run.finished_at = time.time()
    run.returncode = returncode
    for line in lines:
        run._handle_stdout_line(line)
    if result is not None:
        run._handle_stdout_line(json.dumps(result, ensure_ascii=False))
    return run


def _run_app() -> AppTest:
    at = AppTest.from_file(str(APP), default_timeout=APP_TIMEOUT_SEC)
    at.run()
    assert not at.exception, at.exception
    return at


def _texts(at: AppTest) -> dict:
    return {"error": [e.value for e in at.error], "warning": [w.value for w in at.warning],
            "info": [i.value for i in at.info], "success": [s.value for s in at.success],
            "caption": [c.value for c in at.caption], "code": [c.value for c in at.code]}


def _downloads(at: AppTest) -> list[str]:
    return [element.proto.label for element in at.get("download_button")]


def _language_box(at: AppTest):
    boxes = [box for box in at.selectbox if box.label == SOURCE_LANG_LABEL]
    assert len(boxes) == 1, [box.label for box in at.selectbox]
    return boxes[0]


def _script(tmp_path: Path, body: str) -> list[str]:
    path = tmp_path / f"is_{time.time_ns()}.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return [sys.executable, str(path)]


def _wait(predicate, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                           capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


def _fresh(path: Path, text: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


def _stale(path: Path, seconds: float = 3600) -> None:
    moment = time.time() - seconds
    os.utime(path, (moment, moment))


# ---------------------------------------------------------------------------
# F5-01: aynı anda tek iş
# ---------------------------------------------------------------------------

class TestF501TekIs:
    def _first_running(self, env, book, kind="extract") -> Run:
        first = Run(kind, ["ilk-is"])
        first._process = _Alive()
        first.started_at = time.time()
        env.store["_job"]["run"] = first
        env.store["_job"]["run_book"] = book
        return first

    def test_bayat_sekmeden_tex_cikar_ikinci_isi_baslatmaz(self, env):
        book = _pdf(env.tmp)
        env.store["_job"] = _slot(book)
        at = _run_app()
        button = at.button(key="tex_extract")
        assert not button.disabled                 # sekme A: iş yok, düğme açık
        first = self._first_running(env, book)     # bu arada başka sekme başlattı
        button.click().run()
        assert env.started == []                   # ikinci süreç başlamadı
        assert env.store["_job"]["run"] is first   # İptal hâlâ ilk işe ulaşır
        assert branding_busy_message() in _texts(at)["warning"]

    def test_bayat_sekmeden_cevir_ikinci_isi_baslatmaz(self, env):
        book = _pdf(env.tmp)
        env.store["_job"] = _slot(book)
        at = _run_app()
        button = at.button(key=TRANSLATE_KEY)
        assert not button.disabled
        first = self._first_running(env, book, "translate")
        button.click().run()
        assert env.started == []
        assert env.store["_job"]["run"] is first

    def test_bayat_sekmeden_derle_ikinci_isi_baslatmaz(self, env):
        book = _pdf(env.tmp)
        files = runner.tex_files(book)
        _fresh(files.export_tex, "\\chapter{A}\nMetin.\n")
        _fresh(files.export_tr_tex, "\\chapter{A}\nÇeviri.\n")
        env.store["_job"] = _slot(book)
        at = _run_app()
        button = at.button(key="tex_compile")
        assert not button.disabled
        first = self._first_running(env, book)
        button.click().run()
        assert env.started == []
        assert env.store["_job"]["run"] is first


def branding_busy_message() -> str:
    import ast

    for node in ast.parse(APP.read_text(encoding="utf-8")).body:
        if (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "JOB_BUSY_MESSAGE"):
            return ast.literal_eval(node.value)
    raise AssertionError("JOB_BUSY_MESSAGE yok")


# ---------------------------------------------------------------------------
# F5-02: derleme çıkış 0/3'te TeX hatası, eksik glif, EPUB kapsamı
# ---------------------------------------------------------------------------

class TestF502DerlemeUyarilari:
    def _compile_run(self, env, **fields) -> tuple[Path, Run, Path]:
        book = _pdf(env.tmp)
        out = env.root / "output" / "k"
        pdf = _pdf(out, "k_TR.pdf")
        payload = {"ok": True, "qc_ok": True, "exit_code": 0, "slug": "k",
                   "book_name": "k", "output_dir": str(out), "pdf": str(pdf),
                   "epub": None, "error": None, "qc_score": 100.0,
                   "qc_report": None, "qc_top": [], "epub_note": None,
                   "tex_hatalari": 0, "eksik_glif": 0, "epub_kapsam": None}
        payload.update(fields)
        run = _finished("compile", payload["exit_code"], payload)
        env.store["_job"] = _slot(book, run)
        return book, run, out

    def test_tex_hatasi_cikis_0_uyari_cli_metniyle(self, env):
        book, run, out = self._compile_run(env, tex_hatalari=1)
        log = _fresh(out / "source" / "book_tr.log", "! Undefined control sequence.\n")
        at = _run_app()
        texts = _texts(at)
        assert texts["success"][0].startswith("PDF hazır")
        assert CLI_TEX_WARNING in texts["warning"]
        assert str(log) in texts["code"]           # günlüğün yolu

    def test_uyari_metni_2_derle_ile_tek_kaynak(self):
        import importlib

        derle = importlib.import_module("2_derle")
        for counts in ({"tex_hatalari": 1, "eksik_glif": 0},
                       {"tex_hatalari": 0, "eksik_glif": 3},
                       {"tex_hatalari": 2, "eksik_glif": 5}):
            assert runner.compile_issue_text(counts) == derle._compile_issue_line(counts)
        assert runner.compile_issue_text({"tex_hatalari": 0, "eksik_glif": 0}) is None
        assert runner.compile_issue_text({"tex_hatalari": 1, "eksik_glif": 0}) \
            == CLI_TEX_WARNING

    def test_eksik_glif_cikis_3_uyari(self, env):
        self._compile_run(env, exit_code=3, ok=True, qc_ok=False, qc_score=62.0,
                          eksik_glif=3)
        texts = _texts(_run_app())
        assert any("3 eksik glif" in w for w in texts["warning"])

    def test_epub_kapsami_uyari_cli_metniyle(self, env):
        book, run, out = self._compile_run(env)
        epub = _fresh(out / "compiled" / "k_TR.epub", b"PK\x03\x04sahte")
        _fresh(out / "compiled" / "epub_rapor.json",
               json.dumps({"kapsam": 0.9907, "cevrilmemis_blok": 1, "toplam_blok": 108}))
        run.result.update(epub=str(epub), epub_kapsam=0.9907)
        at = _run_app()
        texts = _texts(at)
        assert CLI_EPUB_WARNING in texts["warning"]
        assert "EPUB dosyasını indir" in _downloads(at)

    def test_bayat_rapor_blok_sayisi_uydurmaz(self, env):
        book, run, out = self._compile_run(env)
        epub = _fresh(out / "compiled" / "k_TR.epub", b"PK\x03\x04sahte")
        report = _fresh(out / "compiled" / "epub_rapor.json",
                        json.dumps({"kapsam": 0.5, "cevrilmemis_blok": 54,
                                    "toplam_blok": 108}))
        _stale(report)                              # önceki koşudan kalmış
        run.result.update(epub=str(epub), epub_kapsam=0.9907)
        texts = _texts(_run_app())
        assert ("EPUB'da bazı bloklar kaynak dilde kaldı (kapsam %99,1)."
                in texts["warning"])

    def test_onarilan_ayrac_uyarisi(self, env):
        # X kulvarı (F2-01) uyarıyı yalnız CLI özetine koydu; arayüz de gösterir.
        import importlib

        derle = importlib.import_module("2_derle")
        env.monkeypatch.setattr(derle, "_read_brace_fixes", lambda out, since: 2,
                                raising=False)
        env.monkeypatch.setattr(derle, "_brace_fix_line",
                                lambda count: f"Çeviride {count} kapanmamış '{{' vardı."
                                if count else None, raising=False)
        book, run, out = self._compile_run(env)
        _fresh(out / "pipeline.log", "günlük\n")
        texts = _texts(_run_app())
        assert "Çeviride 2 kapanmamış '{' vardı." in texts["warning"]
        assert str(out / "pipeline.log") in texts["code"]

    def test_tam_kapsamda_uyari_yok(self, env):
        book, run, out = self._compile_run(env)
        epub = _fresh(out / "compiled" / "k_TR.epub", b"PK\x03\x04sahte")
        run.result.update(epub=str(epub), epub_kapsam=1.0)
        texts = _texts(_run_app())
        assert texts["warning"] == []


# ---------------------------------------------------------------------------
# F5-03: yerel model sonucunda EPUB, puan ve QC raporu
# ---------------------------------------------------------------------------

QC_REPORT_41 = textwrap.dedent("""\
    ============================================================
    QC ENGINE REPORT
    ============================================================

      Score: 41/100 (F)
      Total issues: 15

      Issues by check:
        untranslated: 12
        hallucination: 3
    """)


class TestF503YerelModelSonucu:
    def _model_run(self, env, code: int, *, epub_input: bool = True) -> tuple[Path, Path]:
        book = (_epub(env.tmp, EN_TEXT * 40, "Model Kitap.epub") if epub_input
                else _pdf(env.tmp, "Model Kitap.pdf"))
        run = _finished("translate", code)
        slug = runner.book_slug(book)
        out = env.root / "output" / slug
        _pdf(out / "compiled", f"{slug}_TR.pdf")
        env.store["_job"] = _slot(book, run)
        return book, out

    def test_cikis_0_turkce_epub_ve_puan(self, env):
        book, out = self._model_run(env, 0)
        slug = out.name
        _fresh(out / "compiled" / f"{slug}_TR.epub", b"PK\x03\x04sahte")
        _fresh(out / "qc_report.txt", QC_REPORT_41.replace("41/100", "93/100"))
        at = _run_app()
        texts = _texts(at)
        assert texts["success"][0].startswith("PDF hazır")
        assert "Kalite puanı: 93/100" in texts["caption"]
        assert "EPUB dosyasını indir" in _downloads(at)

    def test_cikis_3_puan_denetimler_ve_rapor(self, env):
        book, out = self._model_run(env, 3)
        report = _fresh(out / "qc_report.txt", QC_REPORT_41)
        at = _run_app()
        texts = _texts(at)
        assert ("PDF üretildi ama kalite denetimi eşiğin altında kaldı (puan 41/100)."
                in texts["warning"])
        assert ("En çok bulgu veren denetimler: Çevrilmemiş metin (12), "
                "Uydurma ya da kayıp içerik (3)") in texts["caption"]
        assert str(report) in texts["code"]

    def test_onceki_kosunun_dosyalari_sonuc_sayilmaz(self, env):
        book, out = self._model_run(env, 3)
        slug = out.name
        epub = _fresh(out / "compiled" / f"{slug}_TR.epub", b"PK\x03\x04eski")
        report = _fresh(out / "qc_report.txt", QC_REPORT_41)
        _stale(epub)
        _stale(report)
        at = _run_app()
        texts = _texts(at)
        assert "EPUB dosyasını indir" not in _downloads(at)
        assert "PDF üretildi ama kalite denetimi eşiğin altında kaldı." in texts["warning"]
        assert not any("41/100" in value for value in sum(texts.values(), []))


# ---------------------------------------------------------------------------
# F5-04: sunucu kapanınca koşan iş durdurulur, girdisi korunur, dizinler süpürülür
# ---------------------------------------------------------------------------

# Arayüz süreci yerine: Run ile işi başlatır, iş başlayınca Ctrl+C (SIGINT) ile
# kapanır. İş (alt süreç) girdisinin varlığını günlüğe yazar ve 30 sn uyur.
_SHUTDOWN_CHILD = textwrap.dedent("""
    import sys, time
    from pathlib import Path
    book, log = Path(sys.argv[1]), Path(sys.argv[2])
    with log.open("a") as handle:
        handle.write(f"basladi girdi_var={book.exists()}\\n")
    time.sleep(30)
    with log.open("a") as handle:
        handle.write(f"30 sn sonra girdi_var={book.exists()}\\n")
""")

_SHUTDOWN_PARENT = textwrap.dedent("""
    import os, signal, sys, tempfile, time
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    tmp, child = Path(sys.argv[2]), sys.argv[3]
    tempfile.tempdir = str(tmp)
    from ui import runner
    book = runner.save_upload("kitap.epub", b"PK-sahte", 10 ** 8)
    log = tmp / "cocuk.log"
    run = runner.Run("extract", [sys.executable, child, str(book), str(log)])
    run.start()
    (tmp / "cocuk.pid").write_text(str(run._process.pid))
    (tmp / "olay.dir").write_text(str(run.event_path.parent))
    (tmp / "yukleme.dir").write_text(str(book.parent))
    deadline = time.time() + 20
    while time.time() < deadline and not (log.exists() and log.read_text()):
        time.sleep(0.05)
    os.kill(os.getpid(), signal.SIGINT)
    time.sleep(60)
""")


class TestF504Kapanis:
    def test_sunucu_kapaninca_is_durur_dizinler_temizlenir(self, tmp_path):
        work = tmp_path / "kapanis"
        work.mkdir()
        script = tmp_path / "ebeveyn.py"
        script.write_text(_SHUTDOWN_PARENT, encoding="utf-8")
        child = tmp_path / "cocuk.py"
        child.write_text(_SHUTDOWN_CHILD, encoding="utf-8")
        pid = None
        try:
            done = subprocess.run([sys.executable, str(script), str(PROJECT_ROOT),
                                   str(work), str(child)], capture_output=True,
                                  text=True, timeout=120)
            pid = int((work / "cocuk.pid").read_text())
            assert done.returncode != 0                     # Ctrl+C ile kapandı
            assert not _alive(pid), "iş sunucu kapandıktan sonra yaşıyor (yetim)"
            log = (work / "cocuk.log").read_text()
            assert "basladi girdi_var=True" in log
            assert "30 sn sonra" not in log                 # iş durduruldu
            assert not Path((work / "olay.dir").read_text()).exists()
            assert not Path((work / "yukleme.dir").read_text()).exists()
            assert runner.SHUTDOWN_NOTICE.format(kind="extract", pid=pid) in done.stderr
        finally:
            if pid is not None and _alive(pid):
                os.kill(pid, signal.SIGKILL)

    def test_kosan_isin_girdisi_silinmez(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        book = runner.save_upload("Kitap.pdf", b"%PDF-1.4 sahte", 1 << 20)
        run = Run("extract", [sys.executable, "-c", "import time; time.sleep(30)",
                              str(book)])
        _REAL_START(run)
        try:
            runner._cleanup_uploads()
            assert book.exists()                            # iş hâlâ okuyor
        finally:
            run.cancel()
        runner._cleanup_uploads()
        assert not book.parent.exists()                    # iş bitti: silinir

    def test_eski_olay_dizinleri_supurulur(self, tmp_path):
        now = time.time()
        old = tmp_path / f"{runner.EVENT_DIR_PREFIX}abcd1234"
        old.mkdir()
        _fresh(old / runner.EVENT_FILE_NAME, "{}\n")
        for path in (old / runner.EVENT_FILE_NAME, old):
            os.utime(path, (now - 2 * runner.EVENT_DIR_SWEEP_AGE_SEC,) * 2)
        young = tmp_path / f"{runner.EVENT_DIR_PREFIX}efgh5678"
        young.mkdir()
        _fresh(young / runner.EVENT_FILE_NAME, "{}\n")
        os.utime(young, (now - 2 * runner.EVENT_DIR_SWEEP_AGE_SEC,) * 2)  # dosya taze
        foreign = tmp_path / f"{runner.EVENT_DIR_PREFIX}ijkl9012"
        foreign.mkdir()
        _fresh(foreign / "baska.txt", "kullanıcının dosyası")
        for path in (foreign / "baska.txt", foreign):
            os.utime(path, (now - 2 * runner.EVENT_DIR_SWEEP_AGE_SEC,) * 2)
        removed = runner.sweep_stale_event_dirs(root=tmp_path, now=now)
        assert removed == [old]
        assert young.exists() and foreign.exists()

    def test_arayuz_acilista_olay_dizinlerini_supurur(self, env):
        old = env.temp_root / f"{runner.EVENT_DIR_PREFIX}mnop3456"
        old.mkdir()
        _fresh(old / runner.EVENT_FILE_NAME, "{}\n")
        moment = time.time() - 2 * runner.EVENT_DIR_SWEEP_AGE_SEC
        for path in (old / runner.EVENT_FILE_NAME, old):
            os.utime(path, (moment, moment))
        env.store["_job"] = _slot(None)
        _run_app()
        assert not old.exists()


# ---------------------------------------------------------------------------
# F5-06: '-' ile başlayan Kitap adı
# ---------------------------------------------------------------------------

class TestF506TireIleBaslayanAd:
    def test_komutlar_esittir_bicimiyle(self):
        book = Path("/tmp/Kuantum.pdf")
        assert runner.extract_command(book, "-Kuantum")[-1] == "--name=-Kuantum"
        assert runner.translate_command(book, "-Kuantum")[-1] == "--name=-Kuantum"
        # Öteki adlarda komut eskisi gibi iki argüman.
        assert runner.extract_command(book, "Ad")[-2:] == ["--name", "Ad"]
        assert runner.translate_command(book, "Ad")[-2:] == ["--name", "Ad"]

    def test_translate_book_adi_bayrak_sanmaz(self, monkeypatch):
        import translate_book

        command = runner.translate_command(Path("kitap.pdf"), "-Kuantum")
        monkeypatch.setattr(sys, "argv", ["translate_book.py", *command[2:]])
        assert translate_book.parse_args().name == "-Kuantum"

    def test_1_cikar_adi_bayrak_sanmaz(self, tmp_path):
        # Kitap dosyası yok: argparse adı kabul ederse CLI "input_missing" JSON'u
        # ile 1 döner (çıkarım başlamaz); reddederse argparse 2 ile düşer.
        command = runner.extract_command(tmp_path / "Kuantum.pdf", "-Kuantum")
        done = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True,
                              text=True, timeout=120)
        assert done.returncode != runner.EXIT_USAGE_ERROR, done.stdout[-500:]
        last = json.loads(done.stdout.strip().splitlines()[-1])
        assert last["error"] == "input_missing"

    def test_kullanim_hatasi_derleme_hatasi_sanilmaz(self, env, tmp_path):
        run = Run("translate", _script(tmp_path, """
            import argparse
            parser = argparse.ArgumentParser(prog="translate_book.py")
            parser.add_argument("input")
            parser.add_argument("--name")
            parser.parse_args(["kitap.pdf", "--name", "-Kuantum"])
        """))
        _REAL_START(run)
        assert _wait(lambda: (run.poll(), not run.running)[1])
        assert run.returncode == 2
        assert runner.usage_error(run) == "argument --name: expected one argument"
        env.store["_job"] = _slot(_pdf(env.tmp), run)
        texts = _texts(_run_app())
        assert texts["error"] == ["İş başlatılamadı: komut satırı seçeneği "
                                  "reddedildi (çıkış kodu 2)."]
        assert not any("XeLaTeX" in e for e in texts["error"])
        assert "argument --name: expected one argument" in texts["caption"]

    def test_gercek_derleme_hatasi_yine_derleme_hatasi(self, env):
        run = _finished("translate", 2, lines=("! Undefined control sequence.",))
        env.store["_job"] = _slot(_pdf(env.tmp), run)
        texts = _texts(_run_app())
        assert any("XeLaTeX" in e for e in texts["error"])


# ---------------------------------------------------------------------------
# F5-07: MT fazının gerçek sayacı
# ---------------------------------------------------------------------------

class TestF507MtSayaci:
    def test_batch_satiri_ilerleme_olayi(self):
        assert classify("[BATCH] Translated 40/472 segments (batch_size=8)") == {
            "kind": "progress", "unit": "segment", "current": 40, "total": 472}
        assert classify("[11:01:02] INFO    [BATCH] Translated 12/12 segments "
                        "(batch_size=8)")["current"] == 12

    def test_ic_parti_sayaci_segment_sayilmaz(self):
        # core/translator.py'nin iç sayacı parti sayar (segment değil).
        assert classify("[BATCH] 3/10 işleniyor (8 benzersiz segment) - Geçen: 12sn") \
            is None

    def test_olay_dosyasi_ve_oran(self, tmp_path):
        events = tmp_path / "events.jsonl"
        handler = JsonlEventHandler(events)
        log = logging.getLogger("latex_pipeline.translator")
        old_level = log.level
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        try:
            log.info("[BATCH] Translated %d/%d segments (batch_size=%d)", 40, 472, 8)
        finally:
            log.removeHandler(handler)
            log.setLevel(old_level)
            handler.close()
        run = Run("translate", ["x"])
        run.event_path = events
        run._drain_events()
        assert (run.progress_current, run.progress_total) == (40, 472)
        assert run.percent() == pytest.approx(40 / 472)

    def test_ilerleme_cubugu_ekranda(self, env, tmp_path):
        book = _pdf(env.tmp)
        run = Run("translate", _script(tmp_path, """
            import time
            print("[11:01:02] INFO    [BATCH] Translated 40/472 segments "
                  "(batch_size=8)", flush=True)
            time.sleep(60)
        """))
        _REAL_START(run)
        try:
            assert _wait(lambda: run.progress_total is not None)
            env.store["_job"] = _slot(book, run)
            at = _run_app()
            bars = at.get("progress")
            assert len(bars) == 1
            assert bars[0].proto.text == "40/472 segment"
            assert bars[0].proto.value == int(40 / 472 * 100)
        finally:
            run.cancel()


# ---------------------------------------------------------------------------
# F5-08: faz yalnız pipeline günlükçüsünden, başlık Türkçe
# ---------------------------------------------------------------------------

def _log_to(events: Path, records: list[tuple[str, str]]) -> None:
    handler = JsonlEventHandler(events)
    loggers = {name: logging.getLogger(name) for name, _ in records}
    levels = {name: log.level for name, log in loggers.items()}
    for log in loggers.values():
        log.addHandler(handler)
        log.setLevel(logging.INFO)
    try:
        for name, message in records:
            loggers[name].info(message)
    finally:
        for name, log in loggers.items():
            log.removeHandler(handler)
            log.setLevel(levels[name])
        handler.close()


class TestF508Fazlar:
    def test_sanitizer_kaydi_faz_sayilmaz(self, tmp_path):
        events = tmp_path / "events.jsonl"
        _log_to(events, [("pipeline", "  Phase 5.5: Sanitize + QC + AutoFix"),
                         ("latex_pipeline.sanitizer",
                          "Phase 0.6: Replaced 3 \\bar with \\overline.")])
        rows = [json.loads(line) for line in events.read_text().splitlines()]
        assert [row["kind"] for row in rows] == ["phase", "log"]
        assert rows[1]["message"] == "Phase 0.6: Replaced 3 \\bar with \\overline."
        run = Run("compile", ["x"])
        run.event_path = events
        run._drain_events()
        assert (run.phase_number, run.phase_title) == ("5.5", "Sanitize + QC + AutoFix")

    def test_sanitizer_kaydi_sayaci_sifirlamaz(self, tmp_path):
        events = tmp_path / "events.jsonl"
        _log_to(events, [("pipeline", "  Phase 5: MT Translation (IT -> TR, engine=x)"),
                         ("latex_pipeline.translator",
                          "[BATCH] Translated 16/472 segments (batch_size=8)"),
                         ("latex_pipeline.sanitizer", "Phase 0.6: Replaced 1 \\bar.")])
        run = Run("translate", ["x"])
        run.event_path = events
        run._drain_events()
        assert run.phase_number == "5"
        assert (run.progress_current, run.progress_total) == (16, 472)

    def test_turkce_baslik(self):
        assert (runner.phase_label("5", "MT Translation (IT -> TR, engine=ollama:sahte)")
                == "Makine çevirisi (İtalyanca → Türkçe)")
        assert runner.phase_label("5.5", "Sanitize + QC + AutoFix") == \
            "Temizleme, kalite denetimi ve otomatik düzeltme"
        assert runner.phase_label("6", "Compile Turkish PDF") == "Türkçe PDF'nin derlenmesi"
        assert runner.phase_label("1.5", "Layout detected -- custom, 210x279mm") \
            == "Sayfa düzeni"
        assert runner.phase_label("9.9", "Yeni faz") == "Yeni faz"   # bilgi düşmez

    def test_pipeline_fazlarinin_hepsi_eslenmis(self):
        source = (PROJECT_ROOT / "translate_book.py").read_text(encoding="utf-8")
        numbers = set(re.findall(r'"\s*Phase\s+([\d.]+)\s*:', source))
        assert numbers, "translate_book.py'de faz kaydı bulunamadı"
        assert numbers <= set(branding.PHASE_TITLES), numbers - set(branding.PHASE_TITLES)

    def test_ekranda_turkce_faz_satiri(self, env, tmp_path):
        book = _pdf(env.tmp)
        run = Run("translate", _script(tmp_path, """
            import json, os, time
            with open(os.environ["KITAPCEVIR_EVENT_LOG"], "a", encoding="utf-8") as f:
                f.write(json.dumps({"kind": "phase", "phase": "5", "epoch": time.time(),
                                    "title": "MT Translation (IT -> TR, engine=x)"}) + "\\n")
            time.sleep(60)
        """))
        _REAL_START(run)
        try:
            assert _wait(lambda: (run.poll(), run.phase_number == "5")[1])
            env.store["_job"] = _slot(book, run)
            captions = _texts(_run_app())["caption"]
            assert "Faz 5 · Makine çevirisi (İtalyanca → Türkçe)" in captions
            assert not any("MT Translation" in caption for caption in captions)
        finally:
            run.cancel()


# ---------------------------------------------------------------------------
# F5-13: bitmiş ama yoklanmamış işte İptal
# ---------------------------------------------------------------------------

class TestF513Iptal:
    def test_bitmis_is_iptal_sayilmaz(self):
        run = Run("compile", [sys.executable, "-c", "print('bitti')"])
        _REAL_START(run)
        run._process.wait()                 # bitti, arayüz henüz yoklamadı
        assert run.running
        run.cancel()
        assert not run.cancelled
        assert run.returncode == 0

    def test_kosan_is_iptal_edilir(self):
        run = Run("compile", [sys.executable, "-c", "import time; time.sleep(60)"])
        _REAL_START(run)
        run.cancel()
        assert run.cancelled and run.returncode != 0

    def test_ekranda_sonuc_gizlenmez(self, env):
        book = _pdf(env.tmp)
        out = env.root / "output" / "k"
        pdf = _pdf(out, "k_TR.pdf")
        run = Run("compile", [sys.executable, "-c",
                              "import json; print(json.dumps({'ok': True, 'pdf': %r}))"
                              % str(pdf)])
        _REAL_START(run)
        run._process.wait()
        run.cancel()                        # İptal tam bitişe denk geldi
        env.store["_job"] = _slot(book, run)
        texts = _texts(_run_app())
        assert "İş iptal edildi." not in texts["warning"]
        assert texts["success"] and texts["success"][0].startswith("PDF hazır")


# ---------------------------------------------------------------------------
# KARAR 19: kaynak dil tespiti ve seçimi
# ---------------------------------------------------------------------------

class TestKarar19KaynakDil:
    def test_guvenli_tespit_gosterilir_komut_degismez(self, env):
        book = _epub(env.tmp, DE_TEXT * 40)
        env.store["_job"] = _slot(book)
        at = _run_app()
        assert "Otomatik tespit: Almanca (güven %100)." in _texts(at)["caption"]
        box = _language_box(at)
        assert box.value == "de"
        assert box.options == [runner_language_name(code) for code in TEX_SOURCE_LANGS]
        assert not at.button(key=TRANSLATE_KEY).disabled
        at.button(key=TRANSLATE_KEY).click().run()
        # Tespit değiştirilmedi: --kaynak-dil yok, dili CLI'ın kapısı ölçer.
        assert env.started == [runner.translate_command(book)]

    def test_ingilizce_komut_birebir_ayni(self, env):
        book = _epub(env.tmp, EN_TEXT * 40)
        env.store["_job"] = _slot(book)
        at = _run_app()
        assert _language_box(at).value == "en"
        at.button(key=TRANSLATE_KEY).click().run()
        assert env.started == [runner.translate_command(book)]
        assert runner.SOURCE_LANG_FLAG not in env.started[0]

    def test_elle_degistirilen_dil_komuta_gecer(self, env):
        book = _epub(env.tmp, DE_TEXT * 40)
        env.store["_job"] = _slot(book)
        at = _run_app()
        _language_box(at).set_value("fr").run()
        assert ("Kaynak dil elle seçildi: Fransızca; çeviri bu dille yapılır."
                in _texts(at)["caption"])
        at.button(key=TRANSLATE_KEY).click().run()
        assert env.started == [runner.translate_command(book, source_lang="fr")]
        assert env.started[0][-2:] == ["--kaynak-dil", "fr"]
        assert env.store["_job"]["run_source_lang"] == "fr"

    def test_belirsizse_secim_zorunlu(self, env):
        book = _epub(env.tmp, (EN_TEXT * 3 + DE_TEXT * 2) * 10)
        measured = runner.measure_source_language(book)
        assert measured.measured and measured.confidence < GATE_STOP_CONFIDENCE
        env.store["_job"] = _slot(book)
        at = _run_app()
        captions = _texts(at)["caption"]
        assert any(caption.startswith("Otomatik tespit belirsiz: en olası İngilizce, "
                                      "güven %")
                   and caption.endswith("(eşik %80).")
                   for caption in captions), captions
        assert _language_box(at).value is None
        assert at.button(key=TRANSLATE_KEY).disabled
        assert "Çevirmek için önce kaynak dili seç." in captions
        _language_box(at).set_value("en").run()
        assert not at.button(key=TRANSLATE_KEY).disabled
        at.button(key=TRANSLATE_KEY).click().run()
        assert env.started == [runner.translate_command(book, source_lang="en")]

    def test_desteklenmeyen_dil_secim_ister(self, env):
        import translate_book
        from core.lang_detect import (analyze_language, clean_plain_text_for_detection,
                                      should_stop_for_source_language)

        book = _epub(env.tmp, ZH_TEXT * 80)
        # CLI'ın dil kapısı bu ölçümle durur (çıkış 4); kart da aynısını söyler.
        est = analyze_language(clean_plain_text_for_detection(
            translate_book._epub_text_sample(str(book))), already_clean=True)
        assert should_stop_for_source_language(est)
        env.store["_job"] = _slot(book)
        at = _run_app()
        captions = _texts(at)["caption"]
        assert ("Otomatik tespit: Çince/Japonca/Korece (güven %100); bu dil "
                "desteklenmiyor. Kitap desteklenen bir dildeyse elle seç." in captions)
        assert "Çevirmek için önce kaynak dili seç." in captions
        assert at.button(key=TRANSLATE_KEY).disabled

    def test_olculemeyen_kitap_secim_istemez(self, env):
        book = _pdf(env.tmp)                        # metin katmanı yok
        env.store["_job"] = _slot(book)
        at = _run_app()
        assert any(caption.startswith("Otomatik tespit yapılamadı: kitabın gömülü "
                                      "metni yok")
                   for caption in _texts(at)["caption"])
        assert _language_box(at).value is None
        assert not at.button(key=TRANSLATE_KEY).disabled
        at.button(key=TRANSLATE_KEY).click().run()
        assert env.started == [runner.translate_command(book)]

    def test_bayat_tiklama_secimsiz_baslatmaz(self, env):
        book = _pdf(env.tmp)
        env.store["_job"] = _slot(book)
        at = _run_app()
        assert not at.button(key=TRANSLATE_KEY).disabled
        # Bu arada ölçüm belirsiz çıktı (ör. kitap değişti); tıklama eski çizimden.
        env.monkeypatch.setattr(runner, "measure_source_language",
                                lambda _book: runner.SourceLanguage(
                                    "en", 0.5, 5000, measured=True))
        at.button(key=TRANSLATE_KEY).click().run()
        assert env.started == []
        assert "Çevirmek için önce kaynak dili seç." in _texts(at)["error"]

    def test_olcum_on_kapiyla_ayni_islev(self, env):
        import translate_book
        from core.lang_detect import analyze_language, clean_plain_text_for_detection

        book = _epub(env.tmp, DE_TEXT * 40)
        expected = analyze_language(
            clean_plain_text_for_detection(translate_book._epub_text_sample(str(book))),
            already_clean=True)
        measured = runner.measure_source_language(book)
        assert (measured.code, measured.confidence, measured.letters) == (
            expected.code, expected.confidence, expected.letters)

    def test_kosu_kutusunda_secilen_dil(self, env):
        book = _pdf(env.tmp)
        run = Run("translate", ["x"])
        run._process = _Alive()
        run.started_at = time.time()
        env.store["_job"] = _slot(book, run, run_engine=DEFAULT_ENGINE,
                                  run_source_lang="ru")
        assert "Kaynak dil: Rusça (elle seçildi)" in _texts(_run_app())["caption"]

    def test_dil_kapisi_iletisi_karti_onerir(self, env):
        run = _finished("translate", runner.EXIT_SOURCE_UNSUPPORTED)
        env.store["_job"] = _slot(_pdf(env.tmp), run)
        captions = _texts(_run_app())["caption"]
        assert not any("arayüzde bu seçenekler yok" in caption for caption in captions)
        assert any(branding.quoted(branding.MODEL_TITLE) in caption
                   and "kaynak dili elle seçip" in caption for caption in captions)


def runner_language_name(code: str) -> str:
    """Seçicinin gösterdiği ad (AppTest seçenekleri biçimlenmiş etiketle verir)."""
    from core.lang_detect import language_name_tr

    return language_name_tr(code)


# ---------------------------------------------------------------------------
# F1-03 (arayüz tarafı): çıkarımda atlanan sayfalar
# ---------------------------------------------------------------------------

class TestF103AtlananSayfalar:
    def _tex_folder(self, env, pages) -> Path:
        book = _pdf(env.tmp)
        files = runner.tex_files(book)
        _fresh(files.export_tex, "\\chapter{A}\nMetin.\n")
        (files.output_dir / "kitap.json").write_text(
            json.dumps({"failed_pages": pages}), encoding="utf-8")
        env.store["_job"] = _slot(book)
        return book

    def test_tex_kartinda_uyari(self, env):
        self._tex_folder(env, [9, 5])
        texts = _texts(_run_app())
        assert ("Çıkarımda 2 sayfa okunamadı; bu sayfaların metni çeviriye girmedi "
                "(sayfa 5, 9).") in texts["warning"]

    def test_uzun_liste_kisaltilir(self, env):
        self._tex_folder(env, list(range(1, 13)))
        texts = _texts(_run_app())
        assert ("Çıkarımda 12 sayfa okunamadı; bu sayfaların metni çeviriye girmedi "
                "(sayfa 1, 2, 3, 4, 5, 6, 7, 8, 9, 10 ve 2 sayfa daha).") in texts["warning"]

    def test_gecersiz_deger_uyari_uydurmaz(self, env):
        self._tex_folder(env, ["4", True, None])
        assert not any("okunamadı" in w for w in _texts(_run_app())["warning"])

    def test_anahtar_yoksa_uyari_yok(self, env):
        book = _pdf(env.tmp)
        files = runner.tex_files(book)
        _fresh(files.export_tex, "\\chapter{A}\nMetin.\n")
        env.store["_job"] = _slot(book)
        assert not any("okunamadı" in w for w in _texts(_run_app())["warning"])

    def test_json_sonucu_once_gelir(self, tmp_path):
        (tmp_path / "kitap.json").write_text(json.dumps({"failed_pages": [3]}),
                                             encoding="utf-8")
        assert runner.failed_pages(tmp_path, {"failed_pages": [7, 8]}) == [7, 8]
        assert runner.failed_pages(tmp_path, {}) == [3]
        assert runner.failed_pages(tmp_path / "yok") == []

    def test_cikarim_cikis_3_eksik_sayfa_basari_sayilir(self, env):
        # 1_cikar (X kulvarı, F1-03): EXPORT.tex hazır, 2 sayfa çıkarılamadı;
        # "ok" true, "failed_pages" dolu, çıkış 3; kitap.json'a da yazılır.
        book = _pdf(env.tmp)
        files = runner.tex_files(book)
        _fresh(files.export_tex, "\\chapter{A}\nMetin.\n")
        (files.output_dir / "kitap.json").write_text(
            json.dumps({"failed_pages": [4, 7]}), encoding="utf-8")
        run = _finished("extract", 3, {
            "ok": True, "error": None, "output_dir": str(files.output_dir),
            "export_tex": str(files.export_tex), "chapters": 3,
            "extraction_reused": False, "export_tr_exists": False,
            "export_changed": None, "failed_pages": [4, 7]})
        env.store["_job"] = _slot(book, run)
        texts = _texts(_run_app())
        assert texts["error"] == []
        assert texts["success"] and texts["success"][0].startswith("TeX çıkarıldı: 3 bölüm")
        warning = ("Çıkarımda 2 sayfa okunamadı; bu sayfaların metni çeviriye girmedi "
                   "(sayfa 4, 7).")
        assert texts["warning"].count(warning) == 1          # tek uyarı (kart)

    def test_cikarim_eksik_sayfa_yalniz_jsonda(self, env):
        book = _pdf(env.tmp)
        files = runner.tex_files(book)
        _fresh(files.export_tex, "\\chapter{A}\nMetin.\n")
        run = _finished("extract", 3, {
            "ok": True, "error": None, "output_dir": str(files.output_dir),
            "export_tex": str(files.export_tex), "chapters": 3,
            "failed_pages": [2]})
        env.store["_job"] = _slot(book, run)
        texts = _texts(_run_app())
        assert ("Çıkarımda 1 sayfa okunamadı; bu sayfaların metni çeviriye girmedi "
                "(sayfa 2).") in texts["warning"]

    def test_cikarim_hatasi_yine_hata(self, env):
        book = _pdf(env.tmp)
        run = _finished("extract", 3, {"ok": False, "error": "body_missing",
                                       "failed_pages": [2]})
        env.store["_job"] = _slot(book, run)
        texts = _texts(_run_app())
        assert texts["success"] == []
        assert texts["error"] and texts["error"][0].startswith(
            "Çıkarılan metinden LaTeX gövdesi (body.tex) üretilemedi")

    def test_yerel_model_sonucunda_uyari(self, env):
        book = _pdf(env.tmp, "Model Kitap.pdf")
        run = _finished("translate", 0)
        slug = runner.book_slug(book)
        out = env.root / "output" / slug
        _pdf(out / "compiled", f"{slug}_TR.pdf")
        (out / "kitap.json").write_text(json.dumps({"failed_pages": [4]}),
                                        encoding="utf-8")
        env.store["_job"] = _slot(book, run)
        texts = _texts(_run_app())
        assert ("Çıkarımda 1 sayfa okunamadı; bu sayfaların metni çeviriye girmedi "
                "(sayfa 4).") in texts["warning"]
