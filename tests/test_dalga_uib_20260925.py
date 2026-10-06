"""Arayüz iletileri (UI-b dalgası, 25 Eylül 2026).

Denetim raporunun kalemleri (Playwright, 25 Eylül 2026):
  U6  1_cikar / 2_derle --json "error" kodları ekranda ham ("encoding (çıkış
      kodu 1)"); artık Türkçe ileti, ham kod yalnız parantez içinde;
  U7  derleme çıkış 3: QC puanı, en çok bulgu veren üç denetim (Türkçe adla),
      qc_report.txt yolu ve "Son çıktı"; EXPORT_TR.tex, EXPORT.tex'in birebir
      kopyasıysa "hazır" yerine "henüz çevrilmemiş görünüyor";
  U8  2_derle'nin Türkçe EPUB'u indirilebilir; üretilmediyse nedeni (S4/S5);
  U9  ilerleme kutusu ve İptal kartların üstünde; sayfa sayacı toplamla;
  U16 TDK: "yapay zekâ", özne-yüklem uyumu, "(çıkış kodu 2).", “…” tırnak,
      ondalık virgül, "QA" sekmesinin Türkçe adı;
  U18 Görsel karşılaştırma sekmesi sayfa sayısı farkını söyler.

Çıktı klasörleri gerçek output/ yerine tmp_path'e açılır (runner.PROJECT_ROOT
saptırılır); CLI koşturulmaz (bitmiş koşu nesneleri gerçek koşunun yazdığı
alanlarla kurulur). İlerleme testi gerçek bir alt süreç koşturur.
"""

from __future__ import annotations

import ast
import functools
import json
import os
import re
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import streamlit
from streamlit.testing.v1 import AppTest

import core.memory
from core.memory import TranslationMemory as _RealMemory
from core.mt_engines.contracts import DiscoveryReport
from ui import branding, runner
from ui.runner import PROJECT_ROOT, Run

APP = PROJECT_ROOT / "app.py"
APP_TIMEOUT_SEC = 60
SYNTHETIC_BOOK_FILE = "Dalga UIb Sentetik.pdf"
_REAL_START = Run.start


# ---------------------------------------------------------------------------
# app.py sabitleri (modül Streamlit'siz yüklenemez: AST ile okunur)
# ---------------------------------------------------------------------------

def _app_tree() -> ast.Module:
    return ast.parse(APP.read_text(encoding="utf-8"))


def _app_constants() -> dict:
    """app.py'nin modül düzeyindeki metin sabitleri, sırayla değerlendirilmiş."""
    namespace: dict = {"branding": branding}
    for node in _app_tree().body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id.isupper()):
            try:
                namespace[node.targets[0].id] = eval(
                    compile(ast.Expression(node.value), "app.py", "eval"), namespace)
            except Exception:          # CSS gibi değerlendirilemeyen değil; atla
                continue
    return namespace


C = _app_constants()


def _cli_error_codes(relpath: str) -> set[str]:
    """CLI kaynağında --json'a yazılan "error" kodları."""
    source = (PROJECT_ROOT / relpath).read_text(encoding="utf-8")
    return set(re.findall(r'error="([a-z_]+)"', source))


# ---------------------------------------------------------------------------
# Ortam
# ---------------------------------------------------------------------------

class _StubMemory:
    hits: list = []

    def __init__(self, *args, **kwargs):
        pass

    def get_stats(self):
        return {"total_entries": 0, "confirmed_entries": 0}

    def find_similar(self, *args, **kwargs):
        return list(type(self).hits)

    def close(self):
        pass


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "proje"
    (root / "output").mkdir(parents=True)
    monkeypatch.setattr(runner, "PROJECT_ROOT", root)
    return root


@pytest.fixture
def app_env(monkeypatch, tmp_path, project):
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
    _StubMemory.hits = []
    monkeypatch.setattr(core.memory, "TranslationMemory", _StubMemory)
    monkeypatch.setattr(runner, "platform_problem", lambda: None)
    # Model seçicinin ilk açılış taraması (TASARIM F.2) keşfe gitmez: boş rapor.
    monkeypatch.setattr(runner, "discover_models",
                        lambda: DiscoveryReport(models=(), sources=(),
                                                scanned_at=time.time()))
    temp_root = tmp_path / "gecici"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    started: list[list[str]] = []

    def fake_start(self):
        started.append(list(self.command))
        self.started_at = time.time()

    monkeypatch.setattr(runner.Run, "start", fake_start)
    opened: list[Path] = []
    monkeypatch.setattr(runner, "open_path", lambda path, **_: opened.append(Path(path)))
    return SimpleNamespace(store=store, tmp=tmp_path, project=project,
                           started=started, opened=opened)


def _book(folder: Path) -> Path:
    import fitz

    path = folder / SYNTHETIC_BOOK_FILE
    with fitz.open() as doc:
        doc.new_page()
        doc.save(str(path))
    return path


def _slot(book: Path | None, run: Run | None = None, name: str | None = None) -> dict:
    key = (book.name, book.stat().st_size) if book is not None else None
    return {"book": book, "upload_key": key, "run": run,
            "run_book": book if run is not None else None,
            "run_name": name if run is not None else None, "name": name}


def _finished(kind: str, returncode: int, result: dict | None = None,
              lines: tuple[str, ...] = ()) -> Run:
    run = Run(kind, [sys.executable, "-c", "pass"])
    run.started_at = time.time() - 30
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


def _values(elements) -> list[str]:
    return [element.value for element in elements]


def _buttons(at: AppTest) -> dict:
    return {button.key: button for button in at.button}


def _folder(book: Path, name: str | None = None) -> runner.TexFiles:
    files = runner.tex_files(book, name)
    files.output_dir.mkdir(parents=True, exist_ok=True)
    return files


def _flat(node, out: list | None = None) -> list:
    """Ana alanın öğeleri ekrandaki sırayla (derinlik öncelikli)."""
    out = [] if out is None else out
    children = getattr(node, "children", None) or {}
    for index in sorted(children):
        child = children[index]
        out.append(child)
        _flat(child, out)
    return out


def _position(at: AppTest, predicate) -> int:
    for index, element in enumerate(_flat(at.main)):
        if predicate(element):
            return index
    raise AssertionError("öğe bulunamadı")


def _download_labels(at: AppTest) -> list[str]:
    return [element.proto.label for element in at.get("download_button")]


# ---------------------------------------------------------------------------
# U6 -- hata kodları Türkçe
# ---------------------------------------------------------------------------

class TestErrorCodes:
    def test_cikar_kodlarinin_hepsi_eslenmis(self):
        codes = _cli_error_codes("1_cikar.py")
        assert codes, "1_cikar.py'de error kodu bulunamadı"
        mapped = set(C["EXTRACT_ERROR_MESSAGES"]) | {runner.EXTRACT_ERROR_NAME_CONFLICT}
        assert codes <= mapped, codes - mapped

    def test_derle_kodlarinin_hepsi_eslenmis(self):
        codes = _cli_error_codes("2_derle.py")
        assert {"encoding", "input_too_small", "input_missing", "extraction_missing",
                "book_dir_unresolved", "structure_mismatch"} <= codes
        assert codes <= set(C["COMPILE_ERROR_MESSAGES"]), codes - set(C["COMPILE_ERROR_MESSAGES"])

    @pytest.mark.parametrize("table", ["EXTRACT_ERROR_MESSAGES", "COMPILE_ERROR_MESSAGES"])
    def test_iletiler_noktasiz_ve_kodu_icermez(self, table):
        for code, message in C[table].items():
            assert not message.endswith("."), message      # nokta _failure_text'te
            assert code not in message, message              # ham kod yalnız parantezde

    @pytest.mark.parametrize("code", sorted(_cli_error_codes("2_derle.py")))
    def test_derle_hatasi_turkce_kod_parantezde(self, app_env, code):
        book = _book(app_env.tmp)
        run = _finished("compile", 1, {"ok": False, "error": code, "pdf": None},
                        lines=("HATA: sentetik",))
        app_env.store["_job"] = _slot(book, run)
        at = _run_app()
        expected = f"{C['COMPILE_ERROR_MESSAGES'][code]} ({code}, çıkış kodu 1)."
        assert _values(at.error) == [expected]
        text = _values(at.error)[0]
        assert text.index(code) > text.index("(")            # kod parantezin içinde
        assert "HATA: sentetik" in at.code[-1].value         # "Son çıktı" duruyor

    @pytest.mark.parametrize("code", sorted(set(_cli_error_codes("1_cikar.py"))
                                            - {"name_conflict"}))
    def test_cikar_hatasi_turkce_kod_parantezde(self, app_env, code):
        book = _book(app_env.tmp)
        run = _finished("extract", 1, {"ok": False, "error": code})
        app_env.store["_job"] = _slot(book, run)
        at = _run_app()
        assert _values(at.error) == [
            f"{C['EXTRACT_ERROR_MESSAGES'][code]} ({code}, çıkış kodu 1)."]

    def test_bilinmeyen_kod_genel_ileti_ve_kod(self, app_env):
        book = _book(app_env.tmp)
        run = _finished("extract", 1, {"ok": False, "error": "yeni_kod"})
        app_env.store["_job"] = _slot(book, run)
        at = _run_app()
        assert _values(at.error) == ["TeX çıkarılamadı (yeni_kod, çıkış kodu 1)."]

    def test_ad_cakismasi_ileti_degismedi(self, app_env):
        book = _book(app_env.tmp)
        run = _finished("extract", 1, {"ok": False, "error": "name_conflict"})
        app_env.store["_job"] = _slot(book, run)
        at = _run_app()
        assert _values(at.error) == [C["NAME_CONFLICT_MESSAGE"]]

    def test_cikarim_hatasinda_gunluk_yolu(self, app_env):
        book = _book(app_env.tmp)
        files = _folder(book)
        log = files.output_dir / runner.PIPELINE_LOG_NAME
        log.write_text("günlük\n", encoding="utf-8")
        run = _finished("extract", 1, {"ok": False, "error": "extraction_failed",
                                       "output_dir": str(files.output_dir)})
        app_env.store["_job"] = _slot(book, run)
        at = _run_app()
        assert str(log) in _values(at.code)
        assert C["LOG_HINT_LABEL"] in _values(at.caption)

    def test_gunluk_yoksa_yol_uydurulmaz(self, app_env):
        book = _book(app_env.tmp)
        files = _folder(book)
        run = _finished("extract", 1, {"ok": False, "error": "body_missing",
                                       "output_dir": str(files.output_dir)})
        app_env.store["_job"] = _slot(book, run)
        at = _run_app()
        assert C["LOG_HINT_LABEL"] not in _values(at.caption)

    @pytest.mark.parametrize("kind", ["compile", "translate"])
    def test_cikis_2_derleme_basarisiz(self, app_env, kind):
        book = _book(app_env.tmp)
        result = {"ok": False, "error": None, "pdf": None} if kind == "compile" else None
        run = _finished(kind, runner.EXIT_COMPILE_FAILED, result)
        app_env.store["_job"] = _slot(book, run)
        at = _run_app()
        assert _values(at.error) == [f"{C['COMPILE_FAILED_MESSAGE']} (çıkış kodu 2)."]

    def test_yerel_model_hatasinda_kitap_klasorunun_gunlugu(self, app_env):
        book = _book(app_env.tmp)
        files = _folder(book)
        log = files.output_dir / runner.PIPELINE_LOG_NAME
        log.write_text("günlük\n", encoding="utf-8")
        run = _finished("translate", 1)
        app_env.store["_job"] = _slot(book, run)
        at = _run_app()
        assert _values(at.error) == ["PDF üretilemedi (çıkış kodu 1)."]
        assert str(log) in _values(at.code)


# ---------------------------------------------------------------------------
# U7 -- QC ayrıntısı ve çevrilmemiş kopya
# ---------------------------------------------------------------------------

class TestQcDetails:
    def _compile(self, app_env, returncode: int, **fields):
        book = _book(app_env.tmp)
        files = _folder(book)
        pdf = files.translated_pdf
        pdf.write_bytes(b"%PDF-1.4 sentetik")
        result = {"ok": True, "pdf": str(pdf), "epub": None, "error": None,
                  "output_dir": str(files.output_dir), "qc_score": None,
                  "qc_report": None, "qc_top": [], "epub_note": None, **fields}
        run = _finished("compile", returncode, result, lines=("QC ENGINE REPORT",))
        app_env.store["_job"] = _slot(book, run)
        return files

    def test_esik_alti_puan_denetimler_rapor(self, app_env):
        report = app_env.tmp / "qc_report.txt"
        report.write_text("  Score: 62/100 (C)\n", encoding="utf-8")
        self._compile(app_env, 3, qc_score=62.0, qc_report=str(report), qc_top=[
            {"check": "untranslated", "count": 69},
            {"check": "turkish_naturalness", "count": 51},
            {"check": "yepyeni_kontrol", "count": 4}])
        at = _run_app()
        assert _values(at.warning) == [
            "PDF üretildi ama kalite denetimi eşiğin altında kaldı (puan 62/100)."]
        assert (f"{C['QC_TOP_LABEL']}: Çevrilmemiş metin (69), Türkçe doğallık (51), "
                "yepyeni_kontrol (4)") in _values(at.caption)
        assert str(report) in _values(at.code)
        at.button(key="qc_report_open").click().run()
        assert app_env.opened == [report]
        assert "Son çıktı" in [expander.label for expander in at.expander]
        assert "pdf_open" in _buttons(at)

    def test_ondalik_puan_virgulle(self, app_env):
        self._compile(app_env, 3, qc_score=57.5)
        at = _run_app()
        assert _values(at.warning) == [
            "PDF üretildi ama kalite denetimi eşiğin altında kaldı (puan 57,5/100)."]

    def test_alanlar_yoksa_uydurulmaz(self, app_env):
        self._compile(app_env, 3, qc_report=str(app_env.tmp / "yok.txt"))
        at = _run_app()
        assert _values(at.warning) == [
            "PDF üretildi ama kalite denetimi eşiğin altında kaldı."]
        assert not any(C["QC_TOP_LABEL"] in text for text in _values(at.caption))
        assert "qc_report_open" not in _buttons(at)

    def test_temiz_derlemede_puan_notu(self, app_env):
        self._compile(app_env, 0, qc_score=92.0)
        at = _run_app()
        assert any(text.startswith("PDF hazır (") for text in _values(at.success))
        assert "Kalite puanı: 92/100" in _values(at.caption)
        assert "Son çıktı" not in [expander.label for expander in at.expander]

    def test_turkce_adlar_her_kontrolu_kapsar(self):
        source = (PROJECT_ROOT / "latex_pipeline" / "qc_engine.py").read_text(encoding="utf-8")
        checks = set(re.findall(r'check_name="([a-z_]+)"', source))
        assert checks
        assert checks <= set(branding.QC_CHECK_LABELS), checks - set(branding.QC_CHECK_LABELS)
        for check, label in branding.QC_CHECK_LABELS.items():
            assert label and label != check and "_" not in label

    def test_bilinmeyen_kontrol_ozgun_adla(self):
        assert branding.qc_check_label("untranslated") == "Çevrilmemiş metin"
        assert branding.qc_check_label("yeni_kontrol") == "yeni_kontrol"


class TestUntranslatedCopy:
    @staticmethod
    def _tex(created: str, body: str) -> str:
        from yol_a_ortak import build_meta_header

        return build_meta_header({"slug": "kitap", "book_name": "Kitap",
                                  "created": created}) + body

    def test_birebir_kopya(self, tmp_path):
        export, export_tr = tmp_path / "EXPORT.tex", tmp_path / "EXPORT_TR.tex"
        export.write_text("\\chapter{Einleitung}\n" * 50, encoding="utf-8")
        export_tr.write_bytes(export.read_bytes())
        assert runner.untranslated_copy(export, export_tr)

    def test_yeniden_cikarimda_yalniz_baslik_degisti(self, tmp_path):
        # Gerçek durum (output/_deneme_UIa_cikar): EXPORT.tex yeniden yazıldı,
        # yalnız "% created:" satırı farklı; EXPORT_TR.tex hâlâ çevrilmemiş.
        body = "\\chapter{Einleitung}\nDer Text.\n"
        export, export_tr = tmp_path / "EXPORT.tex", tmp_path / "EXPORT_TR.tex"
        export.write_text(self._tex("2026-09-25T10:53:27", body), encoding="utf-8")
        export_tr.write_text(self._tex("2026-09-25T10:52:55", body), encoding="utf-8")
        assert export.read_bytes() != export_tr.read_bytes()
        assert runner.untranslated_copy(export, export_tr)

    def test_bom_ve_crlf_ceviri_sayilmaz(self, tmp_path):
        body = "\\chapter{Einleitung}\nDer Text.\n"
        export, export_tr = tmp_path / "EXPORT.tex", tmp_path / "EXPORT_TR.tex"
        export.write_text(self._tex("2026-09-25T10:00:00", body), encoding="utf-8")
        export_tr.write_bytes(b"\xef\xbb\xbf" + self._tex(
            "2026-09-25T10:00:00", body).replace("\n", "\r\n").encode("utf-8"))
        assert runner.untranslated_copy(export, export_tr)

    def test_cevrilmis_govde(self, tmp_path):
        export, export_tr = tmp_path / "EXPORT.tex", tmp_path / "EXPORT_TR.tex"
        export.write_text(self._tex("t", "\\chapter{Einleitung}\n"), encoding="utf-8")
        export_tr.write_text(self._tex("t", "\\chapter{Giriş}\n"), encoding="utf-8")
        assert not runner.untranslated_copy(export, export_tr)

    def test_ayni_boyut_farkli_icerik(self, tmp_path):
        export, export_tr = tmp_path / "EXPORT.tex", tmp_path / "EXPORT_TR.tex"
        export.write_text("abcd" * 100, encoding="utf-8")
        export_tr.write_text("abce" * 100, encoding="utf-8")
        assert export.stat().st_size == export_tr.stat().st_size
        assert not runner.untranslated_copy(export, export_tr)

    def test_dosya_degisince_yeniden_bakilir(self, tmp_path):
        export, export_tr = tmp_path / "EXPORT.tex", tmp_path / "EXPORT_TR.tex"
        export.write_text("abcd" * 100, encoding="utf-8")
        export_tr.write_text("abcd" * 100, encoding="utf-8")
        assert runner.untranslated_copy(export, export_tr)
        export_tr.write_text("efgh" * 100, encoding="utf-8")   # aynı boyut
        stamp = time.time() + 5
        os.utime(export_tr, (stamp, stamp))
        assert not runner.untranslated_copy(export, export_tr)

    def test_eksik_dosya(self, tmp_path):
        export = tmp_path / "EXPORT.tex"
        export.write_text("x", encoding="utf-8")
        assert not runner.untranslated_copy(export, tmp_path / "EXPORT_TR.tex")

    def test_kart_kopyada_hazir_demez(self, app_env):
        book = _book(app_env.tmp)
        files = _folder(book)
        files.export_tex.write_text("% gövde\n" * 20, encoding="utf-8")
        files.export_tr_tex.write_bytes(files.export_tex.read_bytes())
        app_env.store["_job"] = _slot(book)
        at = _run_app()
        assert C["UNTRANSLATED_COPY_WARNING"] in _values(at.warning)
        assert "EXPORT_TR.tex hazır; derleyebilirsin." not in _values(at.caption)
        assert not _buttons(at)["tex_compile"].disabled       # bilerek denenebilir
        assert str(files.export_tex) in _values(at.code)       # yollar göz önünde

    def test_kart_ceviride_hazir_der(self, app_env):
        book = _book(app_env.tmp)
        files = _folder(book)
        files.export_tex.write_text("% gövde\n" * 20, encoding="utf-8")
        files.export_tr_tex.write_text("% çeviri\n" * 20, encoding="utf-8")
        app_env.store["_job"] = _slot(book)
        at = _run_app()
        assert C["UNTRANSLATED_COPY_WARNING"] not in _values(at.warning)
        assert "EXPORT_TR.tex hazır; derleyebilirsin." in _values(at.caption)


# ---------------------------------------------------------------------------
# U8 -- EPUB
# ---------------------------------------------------------------------------

class TestEpub:
    def _compile(self, app_env, **fields):
        book = _book(app_env.tmp)
        files = _folder(book)
        files.translated_pdf.write_bytes(b"%PDF-1.4 sentetik")
        result = {"ok": True, "pdf": str(files.translated_pdf), "error": None,
                  "epub": None, "epub_note": None, **fields}
        app_env.store["_job"] = _slot(book, _finished("compile", 0, result))
        return files

    def test_epub_indirilebilir(self, app_env):
        epub = app_env.tmp / "kitap_TR.epub"
        epub.write_bytes(b"PK sentetik epub")
        self._compile(app_env, epub=str(epub), epub_note="eski not")
        at = _run_app()
        assert C["EPUB_DOWNLOAD_LABEL"] in _download_labels(at)
        assert str(epub) in _values(at.caption)
        assert not any(C["EPUB_SKIPPED_LABEL"] in text for text in _values(at.caption))

    def test_uretilmediyse_neden(self, app_env):
        note = "Çeviri bölümleri özgün EPUB ile güvenilir hizalanamadı."
        self._compile(app_env, epub_note=note)
        at = _run_app()
        assert C["EPUB_DOWNLOAD_LABEL"] not in _download_labels(at)
        assert f"{C['EPUB_SKIPPED_LABEL']}: {note}" in _values(at.caption)

    def test_pdf_kitapta_epub_satiri_yok(self, app_env):
        self._compile(app_env)
        at = _run_app()
        assert C["EPUB_DOWNLOAD_LABEL"] not in _download_labels(at)
        assert not any(C["EPUB_SKIPPED_LABEL"] in text for text in _values(at.caption))

    def test_yol_dosyasi_yoksa_dugme_yok(self, app_env):
        self._compile(app_env, epub=str(app_env.tmp / "silinmis.epub"))
        at = _run_app()
        assert C["EPUB_DOWNLOAD_LABEL"] not in _download_labels(at)


# ---------------------------------------------------------------------------
# U9 -- ilerleme kutusu kartların üstünde
# ---------------------------------------------------------------------------

def _is_button(key: str):
    return lambda element: getattr(element, "key", None) == key


def _is_progress(element) -> bool:
    return getattr(element, "type", None) == "progress"


class TestProgressPlacement:
    def _running(self, app_env, script: str) -> tuple[Path, Run]:
        book = _book(app_env.tmp)
        files = _folder(book)
        # TeX kartını uzatan durum: EXPORT.tex + talimat (yollar, istem).
        files.export_tex.write_text("% gövde\n" * 20, encoding="utf-8")
        files.directive.write_text("# talimat\n", encoding="utf-8")
        run = Run("extract", [sys.executable, "-c", textwrap.dedent(script)])
        _REAL_START(run)
        return book, run

    def test_kutu_ve_iptal_kartlarin_ustunde(self, app_env):
        book, run = self._running(app_env, """
            import time
            print("[10:00:00] Batch: pages 20-39 / 386 (20 pages)...", flush=True)
            time.sleep(60)
        """)
        try:
            deadline = time.time() + 20
            while run.progress_total is None and time.time() < deadline:
                time.sleep(0.05)
            app_env.store["_job"] = _slot(book, run)
            at = _run_app()
            bars = at.get("progress")
            assert len(bars) == 1
            assert bars[0].proto.text == "Sayfa 21–40 / 386 işleniyor"
            assert bars[0].proto.value == int(20 / 386 * 100)   # 20 sayfa bitti
            cancel = _position(at, _is_button("job_cancel"))
            bar = _position(at, _is_progress)
            card = _position(at, _is_button("tex_extract"))
            assert cancel < bar < card          # İptal başlık satırında, kutu üstte
        finally:
            run.cancel()

    def test_toplamsiz_sayacta_cubuk_yok(self, app_env):
        book, run = self._running(app_env, """
            import time
            print("[10:00:00] Batch: pages 20-39 (20 pages)...", flush=True)
            time.sleep(60)
        """)
        try:
            deadline = time.time() + 20
            while run.progress_current is None and time.time() < deadline:
                time.sleep(0.05)
            app_env.store["_job"] = _slot(book, run)
            at = _run_app()
            assert at.get("progress") == []
            assert "Sayfa 21–40 işleniyor" in _values(at.caption)
        finally:
            run.cancel()

    def test_sonuc_kartlarin_ustunde(self, app_env):
        book = _book(app_env.tmp)
        run = _finished("compile", 1, {"ok": False, "error": "encoding", "pdf": None})
        app_env.store["_job"] = _slot(book, run)
        at = _run_app()
        error = _position(at, lambda element: getattr(element, "type", None) == "exception"
                          or type(element).__name__ == "Error")
        assert error < _position(at, _is_button("tex_extract"))


# ---------------------------------------------------------------------------
# U16 -- TDK
# ---------------------------------------------------------------------------

UI_SOURCES = (APP, PROJECT_ROOT / "ui" / "branding.py", PROJECT_ROOT / "ui" / "runner.py")


class TestTdk:
    @pytest.mark.parametrize("path", UI_SOURCES, ids=lambda p: p.name)
    def test_yapay_zeka_sapkali(self, path):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"yapay zeka(?!â)", text, re.IGNORECASE)

    def test_ozne_yuklem_uyumu(self):
        assert "Çeviriyi sen yaparsın ya da bir yapay zekâ asistanına yaptırırsın" \
            in branding.TEX_DESCRIPTION
        source = APP.read_text(encoding="utf-8") + branding.TEX_DESCRIPTION
        assert "sen ya da yapay" not in source

    def test_nokta_parantezden_sonra(self, app_env):
        # JSON basmadan çöken çıkarım: yalnız genel ileti ve çıkış kodu.
        book = _book(app_env.tmp)
        app_env.store["_job"] = _slot(book, _finished("extract", 1))
        at = _run_app()
        assert _values(at.error) == ["TeX çıkarılamadı (çıkış kodu 1)."]

    def test_ama_oncesi_virgul_yok(self):
        assert ", ama " not in APP.read_text(encoding="utf-8")

    def test_kisaltma_ve_uc_nokta(self):
        source = APP.read_text(encoding="utf-8")
        assert "örn:" not in source                  # kısaltma noktayla: "örn."
        assert '..."' not in source                  # üç nokta tek karakter: "…"

    def test_adlar_turkce_tirnakla(self):
        source = APP.read_text(encoding="utf-8")
        # Düz tırnak içinde anılan ad ("{branding.TEX_TITLE}" / '...') kalmadı.
        assert not re.search(r"[\"']\{branding\.[A-Z_]+\}[\"']", source)
        assert branding.quoted(branding.TEX_TITLE) == "“PDF'den TeX çıkar”"
        assert branding.quoted(branding.TEX_TITLE) in C["MANUAL_LOCK_MESSAGE"]
        assert branding.quoted(branding.TEX_TITLE) in C["MANUAL_LOCK_NOTE"]

    @pytest.mark.parametrize("value, expected", [
        (100.0, "100"), (90.9090909090909, "90,9"), (62.0, "62"), (32.7, "32,7"),
        (0, "0"), (57.5, "57,5"), (99.96, "100"), (7, "7")])
    def test_ondalik_virgul(self, value, expected):
        assert runner.format_decimal(value) == expected
        assert runner.format_percent(value) == f"%{expected}"

    def test_sonlu_olmayan_sayi_cokmez(self):
        assert runner.format_decimal(float("nan")) == "nan"
        assert runner.format_decimal(float("inf")) == "inf"

    def test_dosya_karti_boyutu_virgulle(self, app_env):
        # Denetim görüntüsü (U/ekran/04_yuklendi.png): kart "EPUB · 0.1 MB".
        book = _book(app_env.tmp)
        with book.open("ab") as handle:              # PDF sonuna dolgu: 1,3 MB
            handle.write(b"\n" * (1_300_000 - book.stat().st_size))
        app_env.store["_job"] = _slot(book)
        at = _run_app()
        cards = [text for text in _values(at.markdown) if 'class="file-card"' in text]
        assert len(cards) == 1
        assert "PDF · 1,3 MB · 1 sayfa" in cards[0], cards[0]
        assert "1.3 MB" not in cards[0]

    def test_bellek_benzerligi_virgulle(self, app_env, monkeypatch):
        # U13 (UI-c) sonrası Bellek sekmesi TranslationMemory'yi açmaz: arama
        # salt okunur runner.search_memory'den, benzerlik TAM SAYI yüzde.
        # U16'nın özü korunur: ondalık nokta ("94.7") ekrana çıkmaz.
        db = app_env.tmp / "bellek" / "translation_memory.db"
        db.parent.mkdir()
        memory = _RealMemory(db_path=str(db), project_name="default", auto_clean=False)
        try:
            assert memory.add("Relative amplitude A", "Göreli genlik A")
        finally:
            memory.close()
        monkeypatch.setattr(runner, "memory_db_path", lambda: db)
        at = _run_app()
        at.text_input(key="memory_query").input("Relative amplitude").run()
        assert not at.exception
        captions = _values(at.caption)
        # fuzz.ratio("relative amplitude", "relative amplitude a") = 94,74 -> "%95"
        assert "%95 benzerlik" in captions, captions
        assert not any(re.search(r"\d\.\d", text) for text in captions
                       if "benzerlik" in text)

    def test_terim_onerisi_virgulle(self, app_env):
        # G2c (KARAR 25): öneri seçili kitabın glossary_shared.tsv'sinden
        # (eski: GlossaryManager.suggest_term); biçim aynı (TDK ondalık virgül).
        book = _book(app_env.tmp)
        app_env.store["_job"] = _slot(book)
        folder = runner.tex_files(book).output_dir
        folder.mkdir(parents=True)
        (folder / "glossary_shared.tsv").write_text("orbit\työrünge\n",
                                                    encoding="utf-8")
        at = _run_app()
        at.text_input(key="term_query").input("orbitt").run()
        assert not at.exception
        assert _values(at.info) == [
            "Tam eşleşme yok. En yakın: orbit → yörünge (benzerlik %90,9)"]

    def test_sekme_adi_turkce(self, app_env):
        at = _run_app()
        labels = [tab.label for tab in at.tabs]
        assert labels == [branding.TERMS_TAB, branding.MEMORY_TAB, branding.COMPARE_TAB]
        assert branding.COMPARE_TAB == "Karşılaştırma"


# ---------------------------------------------------------------------------
# U18 -- sayfa sayısı farkı
# ---------------------------------------------------------------------------

class TestCompareTab:
    @staticmethod
    def _pdf(pages: int) -> bytes:
        import fitz

        with fitz.open() as doc:
            for _ in range(pages):
                doc.new_page(width=200, height=200)
            return doc.tobytes()

    def _uploads(self, monkeypatch, original: bytes, translated: bytes) -> None:
        uploads = {
            "qa_original": SimpleNamespace(name="a.pdf", size=len(original),
                                           getvalue=lambda: original),
            "qa_translated": SimpleNamespace(name="b.pdf", size=len(translated),
                                             getvalue=lambda: translated),
        }
        real = streamlit.file_uploader

        def file_uploader(label, *args, key=None, **kwargs):
            if key in uploads:
                return uploads[key]
            return real(label, *args, key=key, **kwargs)

        monkeypatch.setattr(streamlit, "file_uploader", file_uploader)

    def test_sayfa_sayisi_farki_ekranda(self, app_env, monkeypatch):
        self._uploads(monkeypatch, self._pdf(2), self._pdf(3))
        at = _run_app()
        at.button(key="qa_compare").click().run()
        assert not at.exception
        warnings = _values(at.warning)
        assert any("orijinal 2, çevrilmiş 3 sayfa" in text for text in warnings), warnings
        assert "2 sayfa karşılaştırıldı." in _values(at.success)
        average = next(metric for metric in at.metric if metric.label == "Ortalama fark")
        assert "." not in average.value and average.value.startswith("%")
        # Karşılaştırmanın sayfa görselleri kalıcı geçici dizin bırakmaz.
        assert not list(Path(tempfile.gettempdir()).glob("pixel_diff_*"))

    def test_esit_sayfada_not_yok(self, app_env, monkeypatch):
        self._uploads(monkeypatch, self._pdf(2), self._pdf(2))
        at = _run_app()
        at.button(key="qa_compare").click().run()
        assert not at.exception
        assert not any("Sayfa sayısı farklı" in text for text in _values(at.warning))
