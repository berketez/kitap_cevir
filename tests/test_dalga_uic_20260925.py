"""Arayüz görünüm ve araçlar (UI-c dalgası, 25 Eylül 2026).

Denetim raporunun kalemleri (Playwright, 25 Eylül 2026):
  U11 Streamlit yükleyicisinin İngilizce metinleri ("Drag and drop file here",
      "Limit 500MB per file", "Browse files", "... files are not allowed.");
      türler ve sınır önceden Türkçe söylenmiyordu;
  U12 900 piksel ve altında sütunlar yan yana sıkışıyor, kart metni sözcük
      ortasından kırılıyor, Araçlar sekmeleri taşıyordu;
  U13 "Bellekte ara" bulanık benzerlikti ("the" ve "(3)"süz cümle bulunmuyordu,
      yalnız "default" projesine bakıyordu); yüzde "%100.0" yazıyordu;
  U14 Terimler örneği ("orbit") sözlükte yoktu; aramanın İngilizce olduğu
      söylenmiyordu;
  U15 yüklenen dosya iki kez görünüyordu (Streamlit satırı + kart), boyut
      hep MB ("0,1 MB"), kitabı kaldırmanın yolu yoktu;
  U17 koyu temada .upload-hint / .section-header / .file-meta alfa 0,35-0,40
      (kontrast ~3:1).

Çıktı klasörleri ve çeviri belleği tmp_path'e açılır (runner.PROJECT_ROOT ve
runner.memory_db_path saptırılır); CLI koşturulmaz; gerçek bellek dosyası
(data/translation_memory.db) hiçbir testte açılmaz.
"""

from __future__ import annotations

import ast
import functools
import hashlib
import re
import sqlite3
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
from ui.runner import PROJECT_ROOT, Run

APP = PROJECT_ROOT / "app.py"
APP_TIMEOUT_SEC = 60
UIC_BOOK_FILE = "Dalga UIc Sentetik.pdf"
# Bellek kurmak için gerçek sınıf (sekmenin onu açmadığı sahtesiyle denetlenir).
RealMemory = core.memory.TranslationMemory


def _app_constants() -> dict:
    """app.py'nin modül düzeyindeki büyük harfli sabitleri (AST ile)."""
    namespace: dict = {"branding": branding}
    namespace.update({name: getattr(runner, name) for name in dir(runner)
                      if not name.startswith("__")})
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
# Ortam
# ---------------------------------------------------------------------------

class _NoMemory:
    """Bellek sekmesi TranslationMemory'yi (yazar bağlantı) açmamalı."""

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
    _NoMemory.opened = 0
    monkeypatch.setattr(core.memory, "TranslationMemory", _NoMemory)
    monkeypatch.setattr(runner, "platform_problem", lambda: None)
    # Model seçicinin ilk açılış taraması (TASARIM F.2) keşfe gitmez: boş rapor.
    monkeypatch.setattr(runner, "discover_models",
                        lambda: DiscoveryReport(models=(), sources=(),
                                                scanned_at=time.time()))
    temp_root = tmp_path / "gecici"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    uploads: list[dict] = []
    real_uploader = streamlit.file_uploader

    def file_uploader(label, *args, **kwargs):     # anahtar ve türler kaydedilir
        uploads.append({"label": label, **kwargs})
        return real_uploader(label, *args, **kwargs)

    monkeypatch.setattr(streamlit, "file_uploader", file_uploader)

    def fake_start(self):
        self.started_at = time.time()

    monkeypatch.setattr(runner.Run, "start", fake_start)
    return SimpleNamespace(store=store, tmp=tmp_path, temp_root=temp_root,
                           project=project, uploads=uploads)


def _book(folder: Path, size: int | None = None) -> Path:
    import fitz

    path = folder / UIC_BOOK_FILE
    with fitz.open() as doc:
        doc.new_page()
        doc.save(str(path))
    if size is not None:                              # PDF sonuna dolgu
        with path.open("ab") as handle:
            handle.write(b"\n" * (size - path.stat().st_size))
    return path


def _slot(book: Path | None, run: Run | None = None) -> dict:
    key = (book.name, book.stat().st_size) if book is not None else None
    return {"book": book, "upload_key": key, "run": run,
            "run_book": book if run is not None else None, "run_name": None,
            "name": None}


def _running_run() -> Run:
    """Koşuyor görünen iş: süreç yok, poll() hep 'sürüyor' der."""
    run = Run("extract", [sys.executable, "-c", "pass"])
    run.started_at = time.time()
    run._process = SimpleNamespace(poll=lambda: None, pid=0)
    return run


def _run_app() -> AppTest:
    at = AppTest.from_file(str(APP), default_timeout=APP_TIMEOUT_SEC)
    at.run()
    assert not at.exception, at.exception
    return at


def _values(elements) -> list[str]:
    return [element.value for element in elements]


def _css(at: AppTest) -> str:
    return "\n".join(text for text in _values(at.markdown) if "<style>" in text)


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


# Bellek: gerçek TranslationMemory ile kurulan sentetik veritabanı.
MEMORY_ROWS = {
    runner.MEMORY_DEFAULT_PROJECT: [
        ("(3) Relative phase of the low order modes",
         "(3) Düşük dereceli modların göreceli fazı"),
        ("The CAD Model", "CAD Modeli"),
        ("Efficiency is 100% at the island", "Verim adada %100"),
        ("Index x_1 of the table", "Tablonun x_1 dizini"),
        ("Index xa1 of the table", "Tablonun xa1 dizini"),
    ],
    "kitap_b": [
        ("The pendulum swings", "Sarkaç salınır"),
        ("THERMODYNAMICS", "TERMODİNAMİK"),
        ("Price in $ is *high*", "Fiyat $ cinsinden *yüksek*"),
    ],
}


@pytest.fixture
def memory_db(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "bellek" / "translation_memory.db"
    path.parent.mkdir()
    for project_name, rows in MEMORY_ROWS.items():
        memory = RealMemory(db_path=str(path), project_name=project_name,
                            auto_clean=False)
        try:
            for source, target in rows:
                assert memory.add(source, target), source
        finally:
            memory.close()
    monkeypatch.setattr(runner, "memory_db_path", lambda: path)
    return path


# ---------------------------------------------------------------------------
# U15 -- boyut KB/MB'ye uyarlı; U11 -- sınır ve türler Türkçe
# ---------------------------------------------------------------------------

class TestSizes:
    @pytest.mark.parametrize("size, expected", [
        (0, "0 bayt"), (512, "512 bayt"), (72_906, "72,9 KB"),
        (1_300_000, "1,3 MB"), (999_960, "1 MB"), (2_047_502, "2 MB"),
        (112_000_000, "112 MB"), (1_500_000_000, "1,5 GB")])
    def test_boyut_birime_uyarli(self, size, expected):
        assert runner.format_size(size) == expected

    def test_yukleme_siniri(self):
        # Streamlit'in "500 MB"ı 500 x 1024 x 1024 bayt; ekranda ondalık MB,
        # aşağı yuvarlanmış.
        limit = runner.upload_limit_bytes(500)
        assert limit == 500 * 1024 * 1024
        assert runner.format_upload_limit(limit) == "524 MB"
        assert runner.format_upload_limit(1_999) == "1 KB"

    def test_turler_bir_kaynaktan(self):
        assert runner.suffix_list_tr((".pdf",)) == "PDF"
        assert runner.suffix_list_tr((".pdf", ".epub")) == "PDF ya da EPUB"
        assert runner.suffix_list_tr((".a", ".b", ".c")) == "A, B ya da C"
        assert runner.BOOK_TYPES_LABEL == runner.suffix_list_tr(runner.BOOK_SUFFIXES)
        assert runner.upload_file_types() == ["pdf", "epub"]

    def test_yukleme_reddi_iletileri(self):
        mb = 1024 * 1024
        assert runner.upload_problem("notlar.toml", 10, 500 * mb) == (
            "Yalnızca PDF ya da EPUB yüklenebilir.")
        assert runner.upload_problem("kitap.pdf", 600 * mb, 500 * mb) == (
            "Dosya çok büyük (629,1 MB); sınır 524 MB.")


class TestFileCard:
    def test_kucuk_dosya_kb_ile(self, app_env):
        book = _book(app_env.tmp, size=72_906)
        app_env.store["_job"] = _slot(book)
        at = _run_app()
        cards = [text for text in _values(at.markdown) if 'class="file-card"' in text]
        assert len(cards) == 1
        assert "PDF · 72,9 KB · 1 sayfa" in cards[0], cards[0]
        assert "0,1 MB" not in cards[0]


# ---------------------------------------------------------------------------
# U11 -- yükleyicinin Türkçe metinleri
# ---------------------------------------------------------------------------

class TestUploaderTexts:
    def test_turkce_metinler_css_de(self, app_env):
        at = _run_app()
        css = _css(at)
        limit = runner.format_upload_limit(
            runner.upload_limit_bytes(streamlit.get_option("server.maxUploadSize")))
        for text in (C["UPLOAD_DROP_TEXT"], C["UPLOAD_BROWSE_TEXT"],
                     f"PDF ya da EPUB · en çok {limit}",
                     f"Bu dosya yüklenemedi. Yalnızca PDF ya da EPUB, en çok {limit} "
                     "yüklenebilir.",
                     f"PDF · en çok {limit}"):
            assert f'"{text}"' in css, text
        # İngilizce metin görünmez VE erişilebilirlik ağacından çıkar.
        assert re.search(r'DropzoneInstructions"\] > div > span \{ visibility: hidden;', css)
        assert re.search(r'FileErrorMessage"\] \{ visibility: hidden;', css)
        assert 'st-key-qa_original' in css and 'st-key-qa_translated' in css

    def test_yukleyici_turleri_ve_anahtari(self, app_env):
        _run_app()
        book = [call for call in app_env.uploads if call["label"] == "Kitap"]
        assert book and book[0]["key"] == "book_upload"
        assert book[0]["type"] == ["pdf", "epub"]
        compare = {call["key"]: call["type"] for call in app_env.uploads
                   if call["key"] in ("qa_original", "qa_translated")}
        assert compare == {"qa_original": ["pdf"], "qa_translated": ["pdf"]}

    def test_ipucu_sonraki_adimi_soyler(self, app_env):
        at = _run_app()
        hints = [text for text in _values(at.markdown) if 'class="upload-hint"' in text]
        assert len(hints) == 1
        assert "kartları açılır" in hints[0]
        assert "PDF ya da EPUB yükle." not in hints[0]

    def test_css_icerik_kacisi(self):
        namespace: dict = {}
        tree = ast.parse(APP.read_text(encoding="utf-8"))
        func = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "_css_string")
        exec(compile(ast.Module([func], []), "app.py", "exec"), namespace)
        assert namespace["_css_string"]('a "b" \\c') == '"a \\"b\\" \\\\c"'


# ---------------------------------------------------------------------------
# U15 -- dosya bir kez görünür; "Kitabı kaldır"
# ---------------------------------------------------------------------------

class TestRemoveBook:
    def test_yuklenmis_satir_gizli_hata_ve_ilerleme_gorunur(self, app_env):
        css = _css(_run_app())
        rule = re.search(r'\[class\*="st-key-book_upload"\] \[data-testid="stFileUploaderFile"\]'
                         r'([^{]*)\{ display: none; \}', css)
        assert rule, css
        assert "stFileUploaderFileErrorMessage" in rule.group(1)
        assert 'role="progressbar"' in rule.group(1)

    def test_kaldir_gecici_kopyayi_siler_yukleyiciyi_yeniler(self, app_env):
        book = runner.save_upload("Kaldırılacak.pdf", b"%PDF-1.4 x", 10 * 1024 * 1024)
        app_env.store["_job"] = _slot(book, Run("extract", ["true"]))
        # Kitabın çıktı klasörü: kaldırmada dokunulmaz.
        folder = runner.tex_files(book).output_dir
        folder.mkdir(parents=True)
        (folder / "EXPORT.tex").write_text("x", encoding="utf-8")
        at = _run_app()
        remove = at.button(key="book_remove")
        assert remove.label == C["REMOVE_BOOK_LABEL"] and not remove.disabled
        app_env.uploads.clear()
        at = remove.click().run()
        assert not at.exception
        job = app_env.store["_job"]
        assert job["book"] is None and job["run"] is None and job["upload_key"] is None
        assert job["upload_nonce"] == 1
        assert not book.parent.exists()
        assert (folder / "EXPORT.tex").exists()
        assert not [text for text in _values(at.markdown) if 'class="file-card"' in text]
        # Düğmenin koşusu eski anahtarla başlar, st.rerun sonrası yükleyici yeni
        # anahtarla (boş) çizilir: eski dosya geri yüklenmez.
        keys = [call["key"] for call in app_env.uploads if call["label"] == "Kitap"]
        assert keys[-1] == "book_upload_1"

    def test_is_kosarken_kaldir_kapali(self, app_env):
        book = runner.save_upload("Koşan.pdf", b"%PDF-1.4 y", 10 * 1024 * 1024)
        try:
            app_env.store["_job"] = _slot(book, _running_run())
            at = _run_app()
            assert at.button(key="book_remove").disabled
            assert book.exists()
        finally:
            runner.discard_upload(book)

    def test_kaldirma_kullanicinin_dosyasina_dokunmaz(self, app_env):
        # İş yuvasındaki kitap save_upload'ın kopyası değilse silinmez.
        book = _book(app_env.tmp)
        app_env.store["_job"] = _slot(book)
        at = _run_app()
        at = at.button(key="book_remove").click().run()
        assert not at.exception
        assert book.exists()
        assert app_env.store["_job"]["book"] is None


# ---------------------------------------------------------------------------
# U12 -- dar ekran
# ---------------------------------------------------------------------------

class TestLayout:
    def test_dar_ekranda_alt_alta(self, app_env):
        css = _css(_run_app())
        columns = ('.st-key-{key} > [data-testid="stLayoutWrapper"] > '
                   '[data-testid="stHorizontalBlock"] > [data-testid="stColumn"]')
        media = re.search(r"@media \(max-width: (\d+)px\) \{(.*?)\n\}", css, re.S)
        assert media and int(media.group(1)) == C["STACK_BELOW_PX"] == 900
        assert columns.format(key=C["LAYOUT_KEY"]) in media.group(2)
        assert columns.format(key=C["CARDS_KEY"]) in media.group(2)
        assert "min-width: 100% !important" in media.group(2)
        assert (columns.format(key=C["CARDS_KEY"])
                + f" {{ min-width: min(100%, {C['CARD_MIN_WIDTH_REM']}rem); }}") in css
        assert (columns.format(key=C["LAYOUT_KEY"]) + ":last-child "
                f"{{ min-width: min(100%, {C['TOOLS_MIN_WIDTH_REM']}rem); }}") in css

    def test_sozcuk_ortasindan_kirilmaz_sekmeler_tasmaz(self, app_env):
        css = _css(_run_app())
        assert "word-break: break-all" not in css
        assert ".path-title, .path-desc { overflow-wrap: normal; word-break: normal; }" in css
        assert re.search(r'\[data-baseweb="tab-list"\] \{ flex-wrap: wrap; overflow: visible; \}',
                         css)

    def test_duzen_kaplari_anahtarli(self):
        source = APP.read_text(encoding="utf-8")
        assert "with st.container(key=LAYOUT_KEY):" in source
        assert "with st.container(key=CARDS_KEY):" in source


# ---------------------------------------------------------------------------
# U17 -- kontrast
# ---------------------------------------------------------------------------

def _contrast(alpha: float, background: tuple[int, int, int]) -> float:
    def channel(value: float) -> float:
        value /= 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    def luminance(rgb) -> float:
        red, green, blue = (channel(v) for v in rgb)
        return 0.2126 * red + 0.7152 * green + 0.0722 * blue

    text = tuple(alpha * 255 + (1 - alpha) * v for v in background)
    lighter, darker = sorted((luminance(text), luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


class TestContrast:
    # Koyu temanın zeminleri: sayfa gradyanının iki ucu, kenar çubuğu, kart.
    BACKGROUNDS = [(10, 10, 15), (18, 18, 26), (13, 13, 18), (25, 25, 33)]

    def test_soluk_metin_kontrasti(self, app_env):
        css = _css(_run_app())
        alpha = float(re.search(r"--kc-muted: rgba\(255,255,255,([\d.]+)\)", css).group(1))
        assert alpha >= 0.55
        assert min(_contrast(alpha, bg) for bg in self.BACKGROUNDS) >= 4.5
        # Eski alfa 0,35 bu zeminlerde 4,5:1'in altındaydı.
        assert min(_contrast(0.35, bg) for bg in self.BACKGROUNDS) < 4.5

    @pytest.mark.parametrize("selector", [".upload-hint", ".section-header",
                                          ".file-meta", ".logo-sub", ".path-desc"])
    def test_siniflar_soluk_rengi_kullanir(self, app_env, selector):
        css = _css(_run_app())
        block = re.search(r"(?m)^" + re.escape(selector) + r"\s*\{([^}]*)\}", css)
        assert block, selector
        assert "color: var(--kc-muted)" in block.group(1)


# ---------------------------------------------------------------------------
# U13 -- bellekte metin içi arama, salt okunur
# ---------------------------------------------------------------------------

class TestMemorySearch:
    def test_sozcuk_ve_eksik_cumle_bulunur(self, memory_db):
        # Eski bulanık arama ikisini de bulamıyordu (denetim: "Benzer kayıt yok.").
        found = runner.search_memory("modes")
        assert found.total == 1
        assert found.hits[0].source.startswith("(3) Relative phase")
        found = runner.search_memory("Relative phase of the low order modes")
        assert found.total == 1 and found.hits[0].similarity > 90

    def test_tum_projelerde_ve_ceviride_arar(self, memory_db):
        found = runner.search_memory("sarkaç")
        assert found.total == 1 and found.hits[0].project == "kitap_b"

    def test_buyuk_kucuk_ve_turkce_i(self, memory_db):
        assert runner.search_memory("GÖRECELİ").total == 1
        assert runner.search_memory("termodinamik").total == 1
        assert runner.search_memory("ISLAND").total == 1

    def test_joker_karakterler_harfiyen(self, memory_db):
        assert runner.search_memory("100%").total == 1
        found = runner.search_memory("x_1")
        assert [hit.source for hit in found.hits] == ["Index x_1 of the table"]

    def test_gecmiyorsa_en_yakin_kayitlar(self, memory_db):
        # Eski bulanık arama yazım hatalı cümleyi buluyordu; o yetenek kalır.
        found = runner.search_memory("Relatve phase of the low order modes")
        assert found.fuzzy and found.total == 1
        assert found.hits[0].source.startswith("(3) Relative phase")
        assert found.hits[0].similarity >= runner.MEMORY_FUZZY_THRESHOLD
        # Hiçbir şeye benzemeyen sorgu: boş, uydurma sonuç yok.
        assert runner.search_memory("zzzqqqwww") == runner.MemorySearch(0, (), fuzzy=True)
        # Metin içinde geçiyorsa bulanık arama devreye girmez.
        assert not runner.search_memory("modes").fuzzy

    def test_kisa_sorgu_aranmaz(self, memory_db):
        assert runner.MEMORY_SEARCH_MIN_CHARS == 3
        assert runner.search_memory("th") is None
        assert runner.search_memory("  ab  ") is None
        assert runner.search_memory("the") is not None

    def test_kisa_kayit_once_sinir_ve_toplam(self, memory_db):
        found = runner.search_memory("the", limit=2)
        assert found.total == 7
        assert len(found.hits) == 2
        assert found.hits[0].source == "The CAD Model"

    def test_salt_okunur(self, memory_db):
        before = _md5(memory_db)
        runner.memory_stats()
        runner.search_memory("the")
        assert _md5(memory_db) == before
        with runner._memory_readonly(memory_db) as conn:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("DELETE FROM translations")

    def test_bellek_yoksa_olusturulmaz(self, tmp_path, monkeypatch):
        path = tmp_path / "yok" / "translation_memory.db"
        monkeypatch.setattr(runner, "memory_db_path", lambda: path)
        assert runner.memory_stats() is None
        assert runner.search_memory("modes") == runner.MemorySearch(0, ())
        assert not path.exists()

    def test_sayilar(self, memory_db):
        assert runner.memory_stats() == runner.MemoryStats(total=8, confirmed=0)

    def test_varsayilan_yol_bellek_modulune_esit(self, monkeypatch):
        monkeypatch.setattr(runner, "PROJECT_ROOT", PROJECT_ROOT)
        default = Path(RealMemory._get_default_path(object()))
        assert runner.memory_db_path() == default


class TestMemoryTab:
    def _search(self, app_env, query: str) -> AppTest:
        at = _run_app()
        at.tabs[1].text_input(key="memory_query").input(query).run()
        assert not at.exception
        return at

    def test_bulunan_kayitlar_ve_tamsayi_yuzde(self, app_env, memory_db):
        at = self._search(app_env, "modes")
        captions = _values(at.caption)
        assert C["MEMORY_MATCH_COUNT"].format(total=1) in captions
        percents = [text for text in captions if "benzerlik" in text]
        assert percents and all(re.fullmatch(r"%\d+ benzerlik( · .*)?", text)
                                for text in percents), percents
        hit = [text for text in _values(at.markdown) if 'class="memory-hit"' in text]
        assert len(hit) == 1 and "<mark>modes</mark>" in hit[0]
        assert _NoMemory.opened == 0                 # yazar bağlantı açılmadı

    def test_proje_adi_gosterilir(self, app_env, memory_db):
        at = self._search(app_env, "sarkaç")
        assert any(text.endswith(" · kitap_b") for text in _values(at.caption))

    def test_kisa_sorgu_ve_bulunamayan(self, app_env, memory_db):
        at = self._search(app_env, "ab")
        assert C["MEMORY_QUERY_TOO_SHORT"] in _values(at.caption)
        at.tabs[1].text_input(key="memory_query").input("zzzqqq").run()
        assert C["MEMORY_NO_MATCH"] in _values(at.caption)

    def test_yazim_hatasinda_en_yakin_kayit(self, app_env, memory_db):
        at = self._search(app_env, "Relatve phase of the low order modes")
        captions = _values(at.caption)
        assert C["MEMORY_FUZZY_COUNT"].format(threshold="%70", total=1) in captions
        assert any(re.fullmatch(r"%\d+ benzerlik", text) for text in captions)

    def test_ozel_karakterler_harfiyen(self, app_env, memory_db):
        at = self._search(app_env, "price")
        hit = next(text for text in _values(at.markdown) if 'class="memory-hit"' in text)
        # "$" formüle, "*" vurguya dönmez: sayısal varlık olarak yazılır.
        assert "$" not in hit and "*" not in hit
        assert "&#36;" in hit and "&#42;high&#42;" in hit

    def test_bellek_yoksa_not(self, app_env, tmp_path, monkeypatch):
        path = tmp_path / "yok.db"
        monkeypatch.setattr(runner, "memory_db_path", lambda: path)
        at = _run_app()
        assert C["MEMORY_MISSING_NOTE"] in _values(at.caption)
        assert not path.exists()


# ---------------------------------------------------------------------------
# U14 -- Terimler örneği sözlükte gerçekten var
# ---------------------------------------------------------------------------

class TestTermsExample:
    def test_ornek_gercek_sozlukte_var(self):
        from core.glossary import GlossaryManager

        glossary = GlossaryManager()
        example = runner.glossary_example(glossary.terms)
        assert example is not None
        hit = glossary.get_term(example)
        assert hit is not None and hit["tr"].casefold() != hit["en"].casefold()
        assert glossary.get_term("orbit") is None     # eski örnek sözlükte yoktu

    @pytest.mark.parametrize("terms, expected", [
        ({}, None),
        ({"orbit": {}}, "orbit"),
        ({"b": {"en": "B", "tr": "B"}, "c": {"en": "C", "tr": "ce"}}, "C"),
        ({"b": {"en": "B", "tr": "b"}, "a": {"en": "A", "tr": "A"}}, "A"),
    ])
    def test_secim_kurali(self, terms, expected):
        assert runner.glossary_example(terms) == expected

    # G2c (KARAR 25): sekme seçili kitabın glossary_shared.tsv'sini gösterir
    # (eski: 174 terimlik genel İngilizce liste, GlossaryManager).
    def test_yer_tutucu_ve_not(self, app_env):
        book = _book(app_env.tmp)
        app_env.store["_job"] = _slot(book)
        folder = runner.tex_files(book).output_dir
        folder.mkdir(parents=True)
        (folder / "glossary_shared.tsv").write_text(
            "alpha\talpha\nzeta term\tzeta terimi\n", encoding="utf-8")
        at = _run_app()
        field = at.tabs[0].text_input(key="term_query")
        assert field.proto.placeholder == "örn. zeta term"
        assert C["TERMS_NOTE"].format(count=2, shown=2) in _values(at.caption)
        assert "İngilizce" not in C["TERMS_NOTE"]

    def test_kitap_yoksa_ileti(self, app_env):
        at = _run_app()
        assert C["TERMS_NO_BOOK"] in _values(at.tabs[0].caption)

    def test_sozluk_yoksa_ileti(self, app_env):
        book = _book(app_env.tmp)
        app_env.store["_job"] = _slot(book)
        at = _run_app()
        assert C["TERMS_NO_GLOSSARY"] in _values(at.tabs[0].caption)

    def test_arama_suzer(self, app_env):
        book = _book(app_env.tmp)
        app_env.store["_job"] = _slot(book)
        folder = runner.tex_files(book).output_dir
        folder.mkdir(parents=True)
        (folder / "glossary_shared.tsv").write_text(
            "Schub\titki\nDüse\tlüle\nBrennkammer\tyanma odası\n",
            encoding="utf-8")
        at = _run_app()
        at = at.tabs[0].text_input(key="term_query").input("LÜLE").run()
        assert not at.exception
        rows = at.tabs[0].dataframe[0].value
        assert list(rows["Kaynak terim"]) == ["Düse"]
        assert C["TERMS_NOTE"].format(count=3, shown=1) in _values(at.caption)
