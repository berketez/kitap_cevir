"""Arayüz akış ve durum hataları (UI-a dalgası, 25 Eylül 2026).

Denetim raporunun kalemleri (Playwright, 25 Eylül 2026):
  U1  sayfa yenilenince "Kitap adı" boşalıyor, kart başka klasöre bakıyordu;
  U2  kenar çubuğu daraltılınca geri açılamıyordu (CSS header'ı gizliyordu);
  U3  aynı adla başka kitap: kart eski kitabın durumunu gösteriyor, derleme açık;
  U5  EXPORT.tex ve kitaba özel talimatın mutlak yolu + Claude Code istemi;
  U10 önceki çıkarım kullanıldıysa not + "Sıfırdan çıkar"; EXPORT_TR.tex varken
      EXPORT.tex yeniden yazılmadan önce onay;
  U19 klasördeki <ad>_TR.pdf kartta; eski yükleme dizinleri açılışta süpürülür;
  U20 bölüm sayısı <= 1 ise uyarı;
  S3  yerel model çıkış 2 + Yol A kilidi için Türkçe ileti;
  S1  "Batch: pages a-b / N" sayacında oran bitmiş sayfalardan (a/N) hesaplanır.

Çıktı klasörleri gerçek output/ yerine tmp_path'e açılır (runner.PROJECT_ROOT
saptırılır); CLI hiç koşturulmaz (Run.start yerine komutu kaydeden sahte).
"""

from __future__ import annotations

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
from core.mt_engines.contracts import DiscoveryReport
from ui import branding, runner
from ui.runner import PROJECT_ROOT, Run
from yol_a_ortak import sha1_of_file

APP = PROJECT_ROOT / "app.py"
APP_TIMEOUT_SEC = 60
# Bu dosyanın sentetik kitabı (tmp_path altında üretilir, metin içermez).
SYNTHETIC_BOOK_FILE = "Dalga UIa Sentetik.pdf"
# Süpürme eşiğinden kesin eski / kesin genç dizin yaşları.
OLD_AGE_SEC = 2 * runner.UPLOAD_SWEEP_AGE_SEC


def _app_constant(name: str) -> str:
    """app.py'deki bir metin sabitinin değeri (modül Streamlit'siz yüklenemez)."""
    source = APP.read_text(encoding="utf-8")
    import ast

    for node in ast.parse(source).body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and getattr(node.targets[0], "id", None) == name):
            return eval(compile(ast.Expression(node.value), "app.py", "eval"),
                        {"branding": branding})
    raise AssertionError(f"app.py'de {name} yok")


NAME_CONFLICT_MESSAGE = _app_constant("NAME_CONFLICT_MESSAGE")
MANUAL_LOCK_MESSAGE = _app_constant("MANUAL_LOCK_MESSAGE")
MANUAL_LOCK_NOTE = _app_constant("MANUAL_LOCK_NOTE")
EXTRACTION_REUSED_MESSAGE = _app_constant("EXTRACTION_REUSED_MESSAGE")
EXPORT_TR_OVERWRITE_WARNING = _app_constant("EXPORT_TR_OVERWRITE_WARNING")
EXPORT_TR_STALE_NOTE = _app_constant("EXPORT_TR_STALE_NOTE")
EXPORT_TR_KEPT_NOTE = _app_constant("EXPORT_TR_KEPT_NOTE")
EXPORT_TR_UNKNOWN_NOTE = _app_constant("EXPORT_TR_UNKNOWN_NOTE")
# app_env Run.start'ı sahtesiyle değiştirir; gerçek süreç isteyen test bunu kullanır.
_REAL_START = Run.start


class _StubMemory:
    def __init__(self, *args, **kwargs):
        pass

    def get_stats(self):
        return {"total_entries": 0, "confirmed_entries": 0}

    def find_similar(self, *args, **kwargs):
        return []

    def close(self):
        pass


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Sahte proje kökü: tex_files / name_conflict buradaki output/'a bakar."""
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

    def fake_start(self):                 # CLI koşturulmaz; komut kaydedilir
        started.append(list(self.command))
        self.started_at = time.time()

    monkeypatch.setattr(runner.Run, "start", fake_start)
    return SimpleNamespace(store=store, tmp=tmp_path, temp_root=temp_root,
                           project=project, started=started)


def _book(folder: Path, content: bytes = b"") -> Path:
    import fitz

    path = folder / SYNTHETIC_BOOK_FILE
    with fitz.open() as doc:
        page = doc.new_page()
        if content:
            page.insert_text((72, 72), content.decode("ascii"))
        doc.save(str(path))
    return path


def _slot(book: Path | None, run: Run | None = None, name: str | None = None,
          run_name: str | None = None) -> dict:
    key = (book.name, book.stat().st_size) if book is not None else None
    return {"book": book, "upload_key": key, "run": run,
            "run_book": book if run is not None else None,
            "run_name": run_name if run is not None else None, "name": name}


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


# ---------------------------------------------------------------------------
# ui/runner.py
# ---------------------------------------------------------------------------

class TestCommandsAndPaths:
    def test_sifirdan_cikar_bayragi(self):
        book = Path("/kitaplar/a.pdf")
        assert runner.FRESH_EXTRACT_FLAG not in runner.extract_command(book)
        command = runner.extract_command(book, "Ad", fresh=True)
        assert command[-1] == runner.FRESH_EXTRACT_FLAG == "--yeniden"
        assert command[-3:-1] == ["--name", "Ad"]

    def test_klasordeki_pdf_ve_kilit_yollari(self, project):
        from translate_book import slugify

        files = runner.tex_files(Path("/kitaplar/a.pdf"), "Özel Ad")
        slug = slugify("Özel Ad")
        assert files.translated_pdf == project / "output" / slug / f"{slug}_TR.pdf"
        assert files.manual_marker == (
            project / "output" / slug / "source" / "MANUAL_TRANSLATION.marker")

    def test_kilit_dosyasi_varsa_kilitli(self, project, tmp_path):
        book = _book(tmp_path)
        files = _folder(book)
        assert not runner.manual_lock(book)
        files.manual_marker.parent.mkdir()
        files.manual_marker.write_text("import: 2026-09-25\n", encoding="utf-8")
        assert runner.manual_lock(book)


class TestNameConflict:
    """U3: klasörün kimliği 1_cikar.py'nin kuralıyla okunur."""

    def test_klasor_yoksa_cakisma_yok(self, project, tmp_path):
        assert not runner.name_conflict(_book(tmp_path))

    def test_sidecar_sha1_farkli_ise_cakisma(self, project, tmp_path):
        book = _book(tmp_path)
        files = _folder(book)
        (files.output_dir / "kitap.json").write_text(
            json.dumps({"source_sha1": "0" * 40}), encoding="utf-8")
        assert runner.name_conflict(book)

    def test_sidecar_sha1_ayni_ise_cakisma_yok(self, project, tmp_path):
        book = _book(tmp_path)
        files = _folder(book)
        (files.output_dir / "kitap.json").write_text(
            json.dumps({"source_sha1": sha1_of_file(book)}), encoding="utf-8")
        assert not runner.name_conflict(book)

    def test_sidecar_yoksa_kaynak_kopyasina_bakilir(self, project, tmp_path):
        book = _book(tmp_path)
        files = _folder(book)
        copy = files.output_dir / "source_book.pdf"
        copy.write_bytes(book.read_bytes())
        assert not runner.name_conflict(book)
        (tmp_path / "baska").mkdir()
        other = _book(tmp_path / "baska", b"baska kitap")
        assert sha1_of_file(other) != sha1_of_file(book)
        copy.write_bytes(other.read_bytes())
        assert runner.name_conflict(book)

    def test_kimlik_yoksa_cakisma_denmez(self, project, tmp_path):
        book = _book(tmp_path)
        files = _folder(book)
        (files.output_dir / "full_book.md").write_text("# eski", encoding="utf-8")
        (files.output_dir / "kitap.json").write_text("{bozuk", encoding="utf-8")
        assert not runner.name_conflict(book)

    def test_ad_verilince_o_klasore_bakilir(self, project, tmp_path):
        book = _book(tmp_path)
        files = _folder(book, "Başka Ad")
        (files.output_dir / "kitap.json").write_text(
            json.dumps({"source_sha1": "1" * 40}), encoding="utf-8")
        assert runner.name_conflict(book, "Başka Ad")
        assert not runner.name_conflict(book)

    def test_sha1_dosya_degisince_yenilenir(self, tmp_path):
        path = tmp_path / "k.pdf"
        path.write_bytes(b"bir")
        first = runner.file_sha1(path)
        assert first == sha1_of_file(path)
        path.write_bytes(b"iki uzun")
        assert runner.file_sha1(path) == sha1_of_file(path) != first


class TestSweepUploads:
    """U19: önceki sunuculardan kalan eski yükleme dizinleri süpürülür."""

    @staticmethod
    def _dir(root: Path, suffix: str, files: dict[str, bytes], age: float) -> Path:
        folder = root / f"{runner.UPLOAD_DIR_PREFIX}{suffix}"
        folder.mkdir()
        for name, data in files.items():
            (folder / name).write_bytes(data)
        stamp = time.time() - age
        os.utime(folder, (stamp, stamp))
        return folder

    def test_yalniz_eski_ve_bizim_bicimdekiler_silinir(self, tmp_path):
        root = tmp_path
        old = self._dir(root, "ab12cd_9", {"Kitap.pdf": b"%PDF"}, OLD_AGE_SEC)
        old_empty = self._dir(root, "zz99yy00", {}, OLD_AGE_SEC)
        young = self._dir(root, "yeni1234", {"Kitap.epub": b"PK"}, 60)
        wrong_name = self._dir(root, "elle-acilmis-dizin", {"a.pdf": b"%PDF"}, OLD_AGE_SEC)
        two_files = self._dir(root, "iki00000", {"a.pdf": b"1", "b.pdf": b"2"}, OLD_AGE_SEC)
        not_book = self._dir(root, "notlar00", {"notlar.txt": b"x"}, OLD_AGE_SEC)
        target = tmp_path / "hedef"
        target.mkdir()
        link = root / f"{runner.UPLOAD_DIR_PREFIX}bag00000"
        link.symlink_to(target)
        stranger = root / "baska-onek-ab12cd_9"
        stranger.mkdir()

        removed = runner.sweep_stale_uploads(root=root)

        assert sorted(removed) == sorted([old, old_empty])
        assert not old.exists() and not old_empty.exists()
        for kept in (young, wrong_name, two_files, not_book, link, target, stranger):
            assert kept.exists()

    def test_bu_surecin_dizinine_dokunulmaz(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        book = runner.save_upload("Kitap.pdf", b"%PDF-1.4", 10 * 1024 * 1024)
        try:
            stamp = time.time() - OLD_AGE_SEC
            os.utime(book.parent, (stamp, stamp))
            assert runner.sweep_stale_uploads() == []
            assert book.exists()
        finally:
            runner.discard_upload(book)


class TestPageProgress:
    """S1: "Batch: pages a-b / N" parti başında basılır; bitmiş sayfa a'dır."""

    def test_toplamli_sayfa_sayaci_bitmis_sayfadan_oran(self, tmp_path):
        script = tmp_path / "sahte_cli.py"
        script.write_text(textwrap.dedent("""
            print("[14:28:26] Batch: pages 20-39 / 100", flush=True)
        """), encoding="utf-8")
        run = Run("extract", [sys.executable, str(script)])
        Run.start(run)
        deadline = time.time() + 30
        while run.running and time.time() < deadline:
            run.poll()
            time.sleep(0.05)
        assert (run.progress_from, run.progress_current, run.progress_total) == (20, 39, 100)
        assert run.percent() == pytest.approx(0.20)

    def test_ilk_partide_oran_sifir_son_partide_bir_degil(self):
        run = Run("extract", ["true"])
        run.progress_unit, run.progress_from = "page", 0
        run.progress_current, run.progress_total = 19, 152
        assert run.percent() == 0.0
        run.progress_from, run.progress_current = 140, 151
        assert run.percent() == pytest.approx(140 / 152)

    def test_eski_bicimde_oran_yok(self):
        run = Run("extract", ["true"])
        run.progress_unit, run.progress_from, run.progress_current = "page", 20, 39
        assert run.percent() is None

    def test_arayuz_bitmis_sayfayi_ve_araligi_gosterir(self, app_env):
        book = _book(app_env.tmp)
        run = Run("extract", [sys.executable, "-c", textwrap.dedent("""
            import time
            print("[10:00:00] Batch: pages 20-39 / 100 (20 pages)...", flush=True)
            time.sleep(60)
        """)])
        _REAL_START(run)
        try:
            deadline = time.time() + 20
            while run.progress_total is None and time.time() < deadline:
                time.sleep(0.05)
            app_env.store["_job"] = _slot(book, run)
            at = _run_app()
            bars = at.get("progress")
            assert len(bars) == 1
            assert bars[0].proto.value == 20                  # %20: 20 sayfa bitti
            assert bars[0].proto.text == "Sayfa 21–40 / 100 işleniyor"
        finally:
            run.cancel()

    def test_segment_sayaci_degismedi(self):
        run = Run("translate", ["true"])
        run.progress_unit, run.progress_current, run.progress_total = "segment", 3, 4
        assert run.percent() == 0.75


# ---------------------------------------------------------------------------
# app.py
# ---------------------------------------------------------------------------

class TestBookNamePersists:
    """U1: kitap adı iş yuvasında tutulur; yenilenen sayfa aynı klasöre bakar."""

    def test_yeni_oturum_adi_is_yuvasindan_alir(self, app_env):
        from translate_book import slugify

        book = _book(app_env.tmp)
        app_env.store["_job"] = _slot(book, name="Özel Ad")
        at = _run_app()
        assert at.sidebar.text_input[0].value == "Özel Ad"
        markdown = "\n".join(_values(at.markdown))
        assert f"Çıktı klasörü: output/{slugify('Özel Ad')}" in markdown

    def test_yazilan_ad_is_yuvasina_gecer_ve_yenilemede_kalir(self, app_env):
        from translate_book import slugify

        book = _book(app_env.tmp)
        app_env.store["_job"] = _slot(book)
        at = _run_app()
        assert at.sidebar.text_input[0].value == ""
        at.sidebar.text_input[0].input("  Yeni Ad  ").run()
        assert not at.exception
        assert app_env.store["_job"]["name"] == "Yeni Ad"

        refreshed = _run_app()                       # yeni oturum = sayfa yenileme
        assert refreshed.sidebar.text_input[0].value == "Yeni Ad"
        assert f"output/{slugify('Yeni Ad')}" in "\n".join(_values(refreshed.markdown))

        refreshed.sidebar.text_input[0].input("").run()
        assert app_env.store["_job"]["name"] is None

    def test_ad_yokken_dosya_adindan_klasor(self, app_env):
        book = _book(app_env.tmp)
        app_env.store["_job"] = _slot(book)
        at = _run_app()
        slug = runner.book_slug(book)
        assert f"Çıktı klasörü: output/{slug}" in "\n".join(_values(at.markdown))


class TestSidebarReopen:
    """U2: header gizlenmez; kenar çubuğunu açan düğme görünür kalır."""

    def test_css_header_gizlemez(self, app_env):
        at = _run_app()
        css = next(text for text in _values(at.markdown) if "<style>" in text)
        for rule in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
            selectors, body = rule
            hides = "visibility: hidden" in body or "display: none" in body
            if hides:
                names = [part.strip() for part in selectors.split(",")]
                assert "header" not in names, selectors
                assert not any("stHeader" in part or "stToolbar\"]" in part
                               or "stExpandSidebarButton" in part
                               or "stSidebarCollapse" in part for part in names)
        assert '[data-testid="stExpandSidebarButton"] { visibility: visible; }' in css
        assert '[data-testid="stMainMenu"]' in css and "footer" in css


class TestNameConflictUI:
    """U3: aynı adla başka kitap -> uyarı, çıkarma/derleme/çeviri kapalı."""

    def _conflicted(self, app_env) -> Path:
        book = _book(app_env.tmp)
        files = _folder(book)
        (files.output_dir / "kitap.json").write_text(
            json.dumps({"source_sha1": "0" * 40}), encoding="utf-8")
        files.export_tex.write_text("% eski kitap\n" * 20, encoding="utf-8")
        files.export_tr_tex.write_text("% eski çeviri\n" * 20, encoding="utf-8")
        return book

    def test_uyari_ve_kapali_dugmeler(self, app_env, monkeypatch):
        monkeypatch.setattr(runner, "local_model_dir", lambda: Path("/model"))
        book = self._conflicted(app_env)
        app_env.store["_job"] = _slot(book)
        at = _run_app()
        assert _values(at.warning) == [NAME_CONFLICT_MESSAGE]
        buttons = _buttons(at)
        assert buttons["tex_extract"].disabled
        assert buttons["tex_compile"].disabled
        assert buttons["model_translate"].disabled
        # Eski kitabın durumu ("EXPORT_TR.tex hazır") gösterilmez.
        assert not any("EXPORT_TR.tex hazır" in text for text in _values(at.caption))
        assert "tex_open" not in buttons

    def test_ayni_kitapta_uyari_yok_derleme_acik(self, app_env, monkeypatch):
        monkeypatch.setattr(runner, "local_model_dir", lambda: Path("/model"))
        book = _book(app_env.tmp)
        files = _folder(book)
        (files.output_dir / "kitap.json").write_text(
            json.dumps({"source_sha1": sha1_of_file(book)}), encoding="utf-8")
        files.export_tex.write_text("% bu kitap\n" * 20, encoding="utf-8")
        files.export_tr_tex.write_text("% çeviri\n" * 20, encoding="utf-8")
        app_env.store["_job"] = _slot(book)
        at = _run_app()
        assert NAME_CONFLICT_MESSAGE not in _values(at.warning)
        buttons = _buttons(at)
        assert not buttons["tex_compile"].disabled
        assert not buttons["model_translate"].disabled

    def test_farkli_ad_verince_cakisma_kalkar(self, app_env):
        book = self._conflicted(app_env)
        app_env.store["_job"] = _slot(book, name="Ayrı Ad")
        at = _run_app()
        assert NAME_CONFLICT_MESSAGE not in _values(at.warning)
        assert not _buttons(at)["tex_extract"].disabled

    def test_cli_name_conflict_turkce(self, app_env):
        book = _book(app_env.tmp)
        run = _finished("extract", 1, {"ok": False, "error": "name_conflict"},
                        lines=("HATA: 'x' dizininde FARKLI bir kitap var",))
        app_env.store["_job"] = _slot(book, run)
        at = _run_app()
        assert _values(at.error) == [NAME_CONFLICT_MESSAGE]
        assert "FARKLI bir kitap" in at.code[0].value      # ham çıktı kuyruğu


class TestTexPaths:
    """U5: mutlak yollar kopyalanabilir (st.code), Claude Code istemi hazır."""

    def test_yollar_ve_istem(self, app_env):
        book = _book(app_env.tmp)
        files = _folder(book)
        files.export_tex.write_text("% gövde\n" * 20, encoding="utf-8")
        files.directive.write_text("# talimat\n", encoding="utf-8")
        app_env.store["_job"] = _slot(book)
        at = _run_app()
        codes = _values(at.code)
        assert str(files.export_tex) in codes
        assert str(files.directive) in codes
        # İstem 1_cikar.py'nin bastığıyla aynı kaynaktan (yol_a_ortak).
        prompt = runner.assistant_prompt(files.directive)
        assert prompt in codes
        assert prompt.startswith(f"{files.directive} dosyasını oku")
        assert Path(str(files.directive)).is_absolute()

    def test_talimat_yoksa_istem_yok(self, app_env):
        book = _book(app_env.tmp)
        files = _folder(book)
        files.export_tex.write_text("% gövde\n" * 20, encoding="utf-8")
        app_env.store["_job"] = _slot(book)
        at = _run_app()
        codes = _values(at.code)
        assert codes == [str(files.export_tex)]


class TestExtractResult:
    """U10 / U20: 1_cikar --json alanlarına göre sonuç notları."""

    @staticmethod
    def _ok(files: runner.TexFiles, **extra) -> dict:
        return {"ok": True, "export_tex": str(files.export_tex),
                "output_dir": str(files.output_dir), **extra}

    def _setup(self, app_env, **extra):
        book = _book(app_env.tmp)
        files = _folder(book)
        files.export_tex.write_text("% gövde\n" * 20, encoding="utf-8")
        run = _finished("extract", 0, self._ok(files, **extra))
        app_env.store["_job"] = _slot(book, run)
        return book, files

    def test_zayif_yapi_uyarisi(self, app_env):
        self._setup(app_env, chapters=1)
        at = _run_app()
        assert any("bölüm yapısı zayıf" in text and "1 bölüm" in text
                   for text in _values(at.warning))
        assert any("TeX çıkarıldı: 1 bölüm" in text for text in _values(at.success))

    def test_yeterli_bolumde_uyari_yok(self, app_env):
        self._setup(app_env, chapters=7)
        at = _run_app()
        assert not any("zayıf" in text for text in _values(at.warning))

    def test_bolum_sayisi_yoksa_uydurulmaz(self, app_env):
        self._setup(app_env)
        at = _run_app()
        success = _values(at.success)
        assert len(success) == 1 and "bölüm" not in success[0]
        assert not any("zayıf" in text for text in _values(at.warning))

    def test_onceki_cikarim_notu_ve_sifirdan_cikar(self, app_env):
        book, files = self._setup(app_env, chapters=5, extraction_reused=True)
        at = _run_app()
        assert EXTRACTION_REUSED_MESSAGE in _values(at.info)
        at.button(key="extract_fresh").click().run()
        assert not at.exception
        assert app_env.started == [runner.extract_command(book, None, fresh=True)]
        assert app_env.started[0][-1] == "--yeniden"

    def test_yeni_cikarimda_not_yok(self, app_env):
        self._setup(app_env, chapters=5, extraction_reused=False)
        at = _run_app()
        assert EXTRACTION_REUSED_MESSAGE not in _values(at.info)
        assert "extract_fresh" not in _buttons(at)

    def test_ad_degistiyse_sifirdan_dugmesi_yok(self, app_env):
        book, files = self._setup(app_env, chapters=5, extraction_reused=True)
        app_env.store["_job"]["name"] = "Başka Ad"
        at = _run_app()
        assert EXTRACTION_REUSED_MESSAGE in _values(at.info)
        assert "extract_fresh" not in _buttons(at)

    @pytest.mark.parametrize("changed, kind, note", [
        (True, "warning", "EXPORT_TR_STALE_NOTE"),
        (False, "info", "EXPORT_TR_KEPT_NOTE"),
        (None, "warning", "EXPORT_TR_UNKNOWN_NOTE"),
    ])
    def test_export_tr_varken_yeniden_yazildi_notu(self, app_env, changed, kind, note):
        self._setup(app_env, chapters=5, export_tr_exists=True, export_changed=changed)
        at = _run_app()
        assert globals()[note] in _values(getattr(at, kind))

    def test_export_tr_yoksa_not_yok(self, app_env):
        self._setup(app_env, chapters=5, export_tr_exists=False, export_changed=True)
        at = _run_app()
        notes = {EXPORT_TR_STALE_NOTE, EXPORT_TR_KEPT_NOTE, EXPORT_TR_UNKNOWN_NOTE}
        assert not notes & set(_values(at.warning) + _values(at.info))


class TestExtractConfirmation:
    """U10: EXPORT_TR.tex varken EXPORT.tex yeniden yazılmadan önce onay."""

    def _translated(self, app_env):
        book = _book(app_env.tmp)
        files = _folder(book)
        files.export_tex.write_text("% gövde\n" * 20, encoding="utf-8")
        files.export_tr_tex.write_text("% çeviri\n" * 20, encoding="utf-8")
        return book, files

    def test_ceviri_yokken_dogrudan_baslar(self, app_env):
        book = _book(app_env.tmp)
        app_env.store["_job"] = _slot(book)
        at = _run_app()
        at.button(key="tex_extract").click().run()
        assert app_env.started == [runner.extract_command(book, None)]

    def test_ceviri_varken_once_onay_sonra_baslar(self, app_env):
        book, files = self._translated(app_env)
        app_env.store["_job"] = _slot(book)
        at = _run_app()
        at.button(key="tex_extract").click().run()
        assert not at.exception
        assert app_env.started == []                         # henüz başlamadı
        assert EXPORT_TR_OVERWRITE_WARNING in _values(at.warning)
        confirm = at.button(key="tex_extract_confirm")
        assert confirm.label == "Yine de yeniden çıkar"
        confirm.click().run()
        assert app_env.started == [runner.extract_command(book, None)]

    def test_vazgec_onayi_kaldirir(self, app_env):
        book, files = self._translated(app_env)
        app_env.store["_job"] = _slot(book)
        at = _run_app()
        at.button(key="tex_extract").click().run()
        at.button(key="tex_extract_cancel").click().run()
        assert not at.exception
        assert app_env.started == []
        assert EXPORT_TR_OVERWRITE_WARNING not in _values(at.warning)
        assert "tex_extract_confirm" not in _buttons(at)

    def test_sifirdan_cikar_da_onay_ister(self, app_env):
        book, files = self._translated(app_env)
        run = _finished("extract", 0, {"ok": True, "chapters": 4,
                                       "export_tex": str(files.export_tex),
                                       "extraction_reused": True})
        app_env.store["_job"] = _slot(book, run)
        at = _run_app()
        at.button(key="extract_fresh").click().run()
        assert app_env.started == []
        confirm = at.button(key="tex_extract_confirm")
        assert confirm.label == "Yine de sıfırdan çıkar"
        confirm.click().run()
        assert app_env.started == [runner.extract_command(book, None, fresh=True)]


class TestTexCardPdf:
    """U19: klasördeki <ad>_TR.pdf TeX kartında gösterilir ve açılır."""

    def test_pdf_gosterilir_ve_acilir(self, app_env, monkeypatch):
        opened: list[Path] = []
        monkeypatch.setattr(runner, "open_path", lambda path, **_: opened.append(path))
        book = _book(app_env.tmp)
        files = _folder(book)
        files.export_tex.write_text("% gövde\n" * 20, encoding="utf-8")
        files.export_tr_tex.write_text("% çeviri\n" * 20, encoding="utf-8")
        files.translated_pdf.write_bytes(b"%PDF-1.4 sentetik")
        old = time.time() - 600
        os.utime(files.export_tr_tex, (old, old))
        app_env.store["_job"] = _slot(book)
        at = _run_app()
        captions = _values(at.caption)
        assert any(f"Derlenmiş PDF: {files.translated_pdf.name}" in text for text in captions)
        assert not any("bu PDF'den sonra değişti" in text for text in captions)
        at.button(key="tex_pdf_open").click().run()
        assert opened == [files.translated_pdf]

    def test_ceviri_pdfden_yeniyse_not(self, app_env):
        book = _book(app_env.tmp)
        files = _folder(book)
        files.export_tex.write_text("% gövde\n" * 20, encoding="utf-8")
        files.translated_pdf.write_bytes(b"%PDF-1.4 sentetik")
        old = time.time() - 600
        os.utime(files.translated_pdf, (old, old))
        files.export_tr_tex.write_text("% yeni çeviri\n" * 20, encoding="utf-8")
        app_env.store["_job"] = _slot(book)
        at = _run_app()
        assert any("bu PDF'den sonra değişti" in text for text in _values(at.caption))

    def test_pdf_yoksa_dugme_yok(self, app_env):
        book = _book(app_env.tmp)
        files = _folder(book)
        files.export_tex.write_text("% gövde\n" * 20, encoding="utf-8")
        app_env.store["_job"] = _slot(book)
        at = _run_app()
        assert "tex_pdf_open" not in _buttons(at)

    def test_acilista_eski_yukleme_dizinleri_supurulur(self, app_env):
        old = app_env.temp_root / f"{runner.UPLOAD_DIR_PREFIX}eski0000"
        old.mkdir()
        (old / "Kitap.pdf").write_bytes(b"%PDF")
        stamp = time.time() - OLD_AGE_SEC
        os.utime(old, (stamp, stamp))
        young = app_env.temp_root / f"{runner.UPLOAD_DIR_PREFIX}yeni0000"
        young.mkdir()
        _run_app()
        assert not old.exists() and young.exists()
        assert app_env.store["_swept_uploads"] == [old]


class TestManualLock:
    """S3: yerel model çıkış 2 + Yol A kilidi -> Türkçe ileti; kilitte düğme kapalı."""

    def _locked(self, app_env) -> Path:
        book = _book(app_env.tmp)
        files = _folder(book)
        files.manual_marker.parent.mkdir()
        files.manual_marker.write_text("import: 2026-09-25\n", encoding="utf-8")
        return book

    def test_cikis_2_kilitle_turkce_ileti(self, app_env):
        book = self._locked(app_env)
        run = _finished("translate", runner.EXIT_MANUAL_LOCK,
                        lines=("Bu kitap Yol A (ajan/manuel) cevirisi iceriyor",))
        app_env.store["_job"] = _slot(book, run)
        at = _run_app()
        assert _values(at.error) == [MANUAL_LOCK_MESSAGE]
        assert f'“{branding.TEX_TITLE}”' in MANUAL_LOCK_MESSAGE
        assert "Bu kitap Yol A" in at.code[0].value

    def test_kilitsiz_cikis_2_derleme_hatasi(self, app_env):
        book = _book(app_env.tmp)
        run = _finished("translate", runner.EXIT_MANUAL_LOCK)
        app_env.store["_job"] = _slot(book, run)
        at = _run_app()
        hatalar = _values(at.error)
        assert len(hatalar) == 1 and hatalar[0].startswith("PDF üretilemedi")
        assert "(çıkış kodu 2)" in hatalar[0]
        assert hatalar[0] != MANUAL_LOCK_MESSAGE

    def test_derlemede_kilit_iletisi_cikmaz(self, app_env):
        book = self._locked(app_env)
        run = _finished("compile", 2, {"ok": False, "pdf": None, "error": None})
        app_env.store["_job"] = _slot(book, run)
        at = _run_app()
        assert MANUAL_LOCK_MESSAGE not in _values(at.error)

    def test_kilitte_cevir_kapali_nedeni_yazili(self, app_env, monkeypatch):
        monkeypatch.setattr(runner, "local_model_dir", lambda: Path("/model"))
        book = self._locked(app_env)
        app_env.store["_job"] = _slot(book)
        at = _run_app()
        assert _buttons(at)["model_translate"].disabled
        assert MANUAL_LOCK_NOTE in _values(at.caption)

    def test_kilitsiz_cevir_acik(self, app_env, monkeypatch):
        monkeypatch.setattr(runner, "local_model_dir", lambda: Path("/model"))
        book = _book(app_env.tmp)
        app_env.store["_job"] = _slot(book)
        at = _run_app()
        assert not _buttons(at)["model_translate"].disabled
        assert MANUAL_LOCK_NOTE not in _values(at.caption)
