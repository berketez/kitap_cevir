"""app.py duman testleri: arayüz başsız (streamlit.testing AppTest) koşar.

Denetim 2026-09-24 Mercek 3 #3: app.py hiçbir testte yüklenmiyordu; bozuk bir
import, NameError ya da sonuç alanıyla koşucu arasındaki bağın kopması suite'te
yakalanmazdı.

AppTest uygulamayı aynı süreçte koşturur. Diske yazan her şey saptırılır:
  - çeviri belleği: Bellek sekmesi belleği salt okunur açar (runner.memory_stats
    / search_memory); yolu tmp_path'teki olmayan bir dosyaya saptırılır, gerçek
    data/translation_memory.db açılmaz. core.memory.TranslationMemory (yazar
    bağlantı) sahte nesneyle değişir ve sekmenin onu açmadığı denetlenir;
  - geçici dizinler (koşunun olay dosyası, yüklenen kitap) tmp_path'e açılır;
  - st.cache_resource test sahipli bir sözlüğe bağlanır: iş yuvası (_job)
    testten doldurulabilir ve testler birbirinin durumunu görmez.
Repo verisine yazılmaz; output/ yalnız okunur (sentetik kitabın klasörü yok).
"""

from __future__ import annotations

import functools
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import streamlit
from streamlit.testing.v1 import AppTest

import core.memory
from core.mt_engines.contracts import DiscoveryReport
from ui import branding, runner
from ui.runner import (EXIT_SOURCE_NOT_ENGLISH, EXIT_SOURCE_UNSUPPORTED,
                       PROJECT_ROOT, Run)

APP = PROJECT_ROOT / "app.py"
APP_TIMEOUT_SEC = 60
MB = 1024 * 1024
# Sentetik kitap adı: output/ altında böyle bir klasör olmamalı (ön koşul).
BOOK_NAME = "Kitapcevir Duman Testi Sentetik.pdf"


class _StubMemory:
    """Çeviri belleğinin yerine: veritabanı açmaz, hiçbir şey yazmaz."""

    opened = 0

    def __init__(self, *args, **kwargs):
        type(self).opened += 1

    def get_stats(self):
        return {"total_entries": 0, "confirmed_entries": 0}

    def find_similar(self, *args, **kwargs):
        return []

    def close(self):
        pass


@pytest.fixture
def app_env(monkeypatch, tmp_path):
    store: dict[str, object] = {}

    def cache_resource(func=None, **_options):
        if func is None:                 # @st.cache_resource(...) biçimi
            return cache_resource

        @functools.wraps(func)
        def cached(*args, **kwargs):
            if func.__name__ not in store:
                store[func.__name__] = func(*args, **kwargs)
            return store[func.__name__]

        return cached

    monkeypatch.setattr(streamlit, "cache_resource", cache_resource)
    _StubMemory.opened = 0
    monkeypatch.setattr(core.memory, "TranslationMemory", _StubMemory)
    monkeypatch.setattr(runner, "memory_db_path", lambda: tmp_path / "bellek_yok.db")
    monkeypatch.setattr(runner, "platform_problem", lambda: None)
    # Model seçicinin ilk açılış taraması (TASARIM F.2) keşfe gitmez: boş rapor.
    monkeypatch.setattr(runner, "discover_models",
                        lambda: DiscoveryReport(models=(), sources=(),
                                                scanned_at=time.time()))
    temp_root = tmp_path / "gecici"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    return SimpleNamespace(store=store, tmp=tmp_path, temp_root=temp_root)


def _run_app() -> AppTest:
    at = AppTest.from_file(str(APP), default_timeout=APP_TIMEOUT_SEC)
    at.run()
    return at


def _values(elements) -> list[str]:
    return [element.value for element in elements]


def _synthetic_book(folder: Path) -> Path:
    """Tek boş sayfalık, bu testin ürettiği PDF (kitap metni içermez)."""
    import fitz

    path = folder / BOOK_NAME
    with fitz.open() as doc:
        doc.new_page()
        doc.save(str(path))
    assert not runner.tex_files(path).output_dir.exists(), "ön koşul: output/ klasörü yok"
    return path


def _slot(book: Path | None, run: Run | None = None) -> dict:
    key = (book.name, book.stat().st_size) if book is not None else None
    return {"book": book, "upload_key": key, "run": run,
            "run_book": book if run is not None else None, "run_name": None}


def _finished_run(kind: str, returncode: int, last_error: str, lines: list[str]) -> Run:
    """Bitmiş bir koşu: süreç başlatılmaz, alanlar gerçek koşunun yazdığı gibi."""
    run = Run(kind, [sys.executable, "-c", "pass"])
    run.started_at = time.time() - 42
    run.finished_at = time.time()
    run.returncode = returncode
    run.last_error = last_error
    for line in lines:
        run._handle_stdout_line(line)
    return run


class TestSmoke:
    def test_bos_sayfa_hatasiz_acilir(self, app_env):
        at = _run_app()
        assert not at.exception
        assert not at.error
        markdown = _values(at.markdown)
        assert "## Çeviri" in markdown and "## Araçlar" in markdown
        # Yükleyicinin altındaki ipucu sonraki adımı söyler; türler ve sınır
        # yükleyicinin kendi (CSS ile Türkçeleşen) satırında (U11).
        hints = [text for text in markdown if 'class="upload-hint"' in text]
        assert len(hints) == 1 and "kartları açılır" in hints[0]
        assert any("PDF ya da EPUB · en çok" in text for text in markdown)
        assert any(branding.APP_NAME in text for text in _values(at.sidebar.markdown))
        # Sekme adları branding'den; "QA" kısaltması Türkçe adıyla (U16).
        assert [tab.label for tab in at.tabs] == [
            branding.TERMS_TAB, branding.MEMORY_TAB, branding.COMPARE_TAB]
        assert "QA" not in [tab.label for tab in at.tabs]
        # Bellek sekmesi yazar bağlantı (TranslationMemory) açmaz; bellek
        # dosyası yoksa oluşturmaz, bunu söyler.
        assert _StubMemory.opened == 0
        assert not (app_env.tmp / "bellek_yok.db").exists()
        assert any("Çeviri belleği henüz oluşmamış" in text for text in _values(at.caption))
        assert app_env.store["_job"]["run"] is None

    def test_kitap_yukluyken_iki_kart(self, app_env):
        book = _synthetic_book(app_env.tmp)
        app_env.store["_job"] = _slot(book)
        at = _run_app()
        assert not at.exception
        assert not at.error
        markdown = "\n".join(_values(at.markdown))
        assert branding.TEX_TITLE in markdown and branding.MODEL_TITLE in markdown
        assert book.name in markdown            # dosya kartı
        buttons = {button.label: button for button in at.button}
        assert "1 · TeX'i çıkar" in buttons and "Çevir" in buttons
        # EXPORT_TR.tex yokken derleme düğmesi kapalı.
        assert buttons["2 · Çevrilmiş TeX'i derle"].disabled

    def test_platform_uygun_degilse_yalniz_neden_gosterilir(self, app_env, monkeypatch):
        problem = "Kitapçevir yalnızca macOS'ta çalışır (bu sistem: Linux)."
        monkeypatch.setattr(runner, "platform_problem", lambda: problem)
        at = _run_app()
        assert not at.exception
        assert _values(at.error) == [problem]
        assert "## Çeviri" not in _values(at.markdown)


class TestResult:
    """Sonuç alanı: bitmiş koşunun hata iletisi, çıkış kodu ve son çıktısı."""

    @pytest.mark.parametrize("kind, returncode, message", [
        ("translate", 1, "PDF üretilemedi"),
        # Dil kapısı yalnız yerel model koşusunda anlamlı; çıkarmada 4 sıradan hatadır.
        ("extract", EXIT_SOURCE_NOT_ENGLISH, "TeX çıkarılamadı"),
    ])
    def test_basarisiz_kosu_hata_ve_cikis_kodu(self, app_env, kind, returncode, message):
        book = _synthetic_book(app_env.tmp)
        run = _finished_run(kind, returncode, "Sentetik hata: adım başarısız",
                            ["adım 1", "HATA: sentetik kırılma"])
        app_env.store["_job"] = _slot(book, run)
        at = _run_app()
        assert not at.exception
        # TDK (U16): nokta parantezden sonra.
        assert _values(at.error) == [f"{message} (çıkış kodu {returncode})."]
        assert "Sentetik hata: adım başarısız" in _values(at.caption)
        assert at.expander[0].label == "Son çıktı"
        assert "HATA: sentetik kırılma" in at.code[0].value


class TestLanguageGate:
    """Denetim 2026-09-24 Mercek 3 #5 (arayüz kısmı): çıkış kodu 4 = kaynak dil
    kapısı. Kullanıcı yalnız "PDF üretilemedi" görmemeli; kapıyı atlayan CLI
    bayrakları ikincil bilgi olarak kalmalı.

    K5 (2026-09-28, KARARLAR 14): yerel model TeX yoluyla aynı 6 kaynak dili
    çevirir; çıkış 4 artık "kitabın dili desteklenmiyor" demektir. TeX kartı
    önerisi kalktı (o yolun dil listesi de aynı), yerine desteklenen diller
    yazılır; CLI ipucu --kaynak-dil'i de anar.
    """

    GATE_ERROR = ("Bu kitabın dili desteklenmiyor (tespit: Çince/Japonca/Korece). "
                  "Yerel model yalnız şu dillerden Türkçeye çevirir: İngilizce, "
                  "Almanca, Rusça, Fransızca, İspanyolca ve İtalyanca.")

    def test_dil_kapisi_iletisi(self, app_env):
        from core.lang_detect import TEX_SOURCE_LANGS, language_list_tr

        book = _synthetic_book(app_env.tmp)
        run = _finished_run("translate", EXIT_SOURCE_UNSUPPORTED, self.GATE_ERROR,
                            ["Kaynak dil kapısı: Çince/Japonca/Korece (kod=cjk, "
                             "güven=0.99)"])
        app_env.store["_job"] = _slot(book, run)
        at = _run_app()
        assert not at.exception

        errors = _values(at.error)
        assert len(errors) == 1
        assert "dili desteklenmiyor" in errors[0]
        assert "İngilizce" not in errors[0]
        assert f"çıkış kodu {EXIT_SOURCE_UNSUPPORTED}" in errors[0]
        assert "PDF üretilemedi" not in errors[0]

        infos = _values(at.info)
        assert len(infos) == 1
        assert language_list_tr(TEX_SOURCE_LANGS, " ve ") in infos[0]
        assert branding.quoted(branding.TEX_TITLE) not in infos[0]

        captions = _values(at.caption)
        assert self.GATE_ERROR in captions      # koşunun kendi tespiti (gerçek veri)
        for flag in ("--dil-kontrolu-yok", "--kaynak-dil"):
            assert flag not in errors[0] and flag not in infos[0]
            assert sum(flag in caption for caption in captions) == 1
        assert "Kaynak dil kapısı: Çince/Japonca/Korece" in at.code[0].value

    def test_eski_ad_ayni_kod(self):
        assert EXIT_SOURCE_NOT_ENGLISH == EXIT_SOURCE_UNSUPPORTED == 4

    def test_anilan_bayrak_cli_da_var(self, monkeypatch):
        import translate_book

        monkeypatch.setattr(sys, "argv", ["translate_book.py", "kitap.pdf",
                                          "--dil-kontrolu-yok"])
        assert translate_book.parse_args().dil_kontrolu_yok is True
        monkeypatch.setattr(sys, "argv", ["translate_book.py", "kitap.pdf",
                                          "--kaynak-dil", "ru"])
        assert translate_book.parse_args().kaynak_dil == "ru"
        text = APP.read_text(encoding="utf-8")
        assert "--dil-kontrolu-yok" in text and "--kaynak-dil" in text


class TestUpload:
    """Denetim 2026-09-24 Mercek 3 #16: yeni kitap önceki kopyanın dizinini siler."""

    @staticmethod
    def _fake_uploader(monkeypatch, name: str, data: bytes) -> None:
        upload = SimpleNamespace(name=name, size=len(data), getvalue=lambda: data)
        real = streamlit.file_uploader

        def file_uploader(label, *args, key=None, **kwargs):
            if key == "book_upload":
                return upload
            return real(label, *args, key=key, **kwargs)

        monkeypatch.setattr(streamlit, "file_uploader", file_uploader)

    def test_yeni_kitap_oncekinin_gecici_kopyasini_siler(self, app_env, monkeypatch):
        old = runner.save_upload("Eski Kitap.pdf", b"%PDF-1.4 eski", 10 * MB)
        app_env.store["_job"] = _slot(old)
        self._fake_uploader(monkeypatch, "Yeni Kitap.pdf", b"%PDF-1.4 yeni")
        at = _run_app()
        assert not at.exception
        job = app_env.store["_job"]
        try:
            assert job["book"].name == "Yeni Kitap.pdf"
            assert job["book"].read_bytes() == b"%PDF-1.4 yeni"
            assert not old.parent.exists()
            assert list(app_env.temp_root.glob(f"{runner.UPLOAD_DIR_PREFIX}*")) == [
                job["book"].parent]
        finally:
            runner.discard_upload(job["book"])

    def test_is_kosarken_kitap_degismez(self, app_env, monkeypatch):
        old = runner.save_upload("Eski Kitap.pdf", b"%PDF-1.4 eski", 10 * MB)
        run = Run("translate", [sys.executable, "-c", "import time; time.sleep(60)"])
        run.start()
        try:
            app_env.store["_job"] = _slot(old, run)
            self._fake_uploader(monkeypatch, "Yeni Kitap.pdf", b"%PDF-1.4 yeni")
            at = _run_app()
            assert not at.exception
            assert app_env.store["_job"]["book"] == old and old.exists()
            assert "İptal" in [button.label for button in at.button]
        finally:
            run.cancel()
            runner.discard_upload(old)
