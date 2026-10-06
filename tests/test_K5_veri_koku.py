"""K5 (5 Ekim 2026): veri kökü — packaging/TASARIM.md §F: F1, F5, F11, F12.

- F1: ``KITAPCEVIR_VERI_KOKU`` doluysa ``output/``, ``models/``,
  ``data/translation_memory.db`` ve ``config/*.json`` o kökten türer
  (``config/paths.py``). Değişken yokken (geliştirici kipi) her yol BİREBİR
  eskisidir; betikler ve çalışma klasörü her iki kipte de kod kökündedir.
- F5: paketli kipte varsayılan model kurulu değilken "Kurulum rehberi:
  docs/…" yerine Bileşenler ekranına yönlendirilir.
- F11: onnxruntime yoksa ``core/layout_detector`` açık ileti verir ve modeli
  indirmeye kalkmaz.
- F12: ``format_decimal`` / ``format_percent`` / ``SIZE_UNITS`` /
  ``format_size`` ``ui/bicim.py``'dedir (yalnız stdlib); ``ui.runner`` aynı
  nesneleri aynı adlarla dışa verir.

Paketli kip geçici bir veri kökünde sınanır (TASARIM B.8'deki gibi boşluklu
ve ``ç``'li yol). Gerçek ``output/``, ``models/``, ``data/`` ve ``config/``
dosyalarına yazılmaz. Model yüklenmez, ağ yok, Marker çalışmaz.

``core/translator.py``, ``core/mt_engines/{registry,discovery}.py``,
``ui/branding.py`` ve ``packaging/testler/test_sunucu.py`` bu kulvarın
dosyası değildi: değişiklikleri K5 yama önerisiyle geldi (iç denetim notu).
O dosyalara bakan testler ``[ONERI.PATCH]`` notlu iletiler verir.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import subprocess
import sys
import types
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import translate_book as tb
from config import paths
from core import layout_detector as ld
from core.memory import TranslationMemory
from core.mt_engines.registry import DEFAULT_ENGINE
from ui import branding
from ui import runner
from yol_a_ortak import build_meta_header, resolve_book_dir

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VERI_ENV = paths.VERI_KOKU_DEGISKENI
URL_ENV = paths.YONETICI_URL_DEGISKENI
PATCH_NOTU = "[ONERI.PATCH] K5 yama önerisi uygulanmalı"


@pytest.fixture(autouse=True)
def _gelistirici_kipi(monkeypatch):
    """Her test geliştirici kipinde başlar; paketli kipi ``veri`` kurar."""
    monkeypatch.delenv(VERI_ENV, raising=False)
    monkeypatch.delenv(URL_ENV, raising=False)


@pytest.fixture
def veri(tmp_path, monkeypatch):
    """Paketli kip: henüz oluşmamış geçici veri kökü (boşluk + ç, TASARIM B.8)."""
    kok = tmp_path / "Application Support" / "Kitapçevir" / "veri"
    monkeypatch.setenv(VERI_ENV, str(kok))
    return kok


def _load_cikar():
    spec = importlib.util.spec_from_file_location("cikar_k5", PROJECT_ROOT / "1_cikar.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Sentetik İngilizce paragraf (kitap metni değil; KARAR 26).
_EN_PARA = ("The small lamp on the desk was warm, and the window behind it "
            "was open to the evening air. She wrote a short note to her "
            "brother about the garden, the rain and the old bicycle that "
            "still stood near the door. ")


def _make_epub(path: Path, text: str = _EN_PARA * 3, chapters: int = 3) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        for i in range(chapters):
            zf.writestr(f"OEBPS/ch{i}.xhtml",
                        f"<html><body><h1>{i}</h1><p>{text}</p></body></html>")
    return path


# ===========================================================================
# config.paths
# ===========================================================================

class TestPaths:
    def test_proje_koku(self):
        assert paths.PROJE_KOKU == PROJECT_ROOT
        assert (paths.PROJE_KOKU / "1_cikar.py").is_file()

    def test_degisken_yokken_varsayilan_kok(self, tmp_path):
        assert paths.veri_koku() == PROJECT_ROOT
        assert paths.veri_koku(tmp_path) == tmp_path
        assert paths.veri_koku(str(tmp_path)) == tmp_path
        assert paths.cikti_koku(tmp_path) == tmp_path / "output"
        assert paths.model_koku(tmp_path) == tmp_path / "models"
        assert paths.ayar_koku(tmp_path) == tmp_path / "config"
        assert paths.bellek_yolu(tmp_path) == tmp_path / "data" / "translation_memory.db"
        assert paths.cikti_koku() == PROJECT_ROOT / "output"
        assert paths.paketli_kip() is False

    def test_degisken_varken_veri_koku_varsayilani_ezer(self, veri, tmp_path):
        baska = tmp_path / "kod"
        assert paths.veri_koku() == veri
        assert paths.veri_koku(baska) == veri
        assert paths.cikti_koku(baska) == veri / "output"
        assert paths.model_koku(baska) == veri / "models"
        assert paths.ayar_koku(baska) == veri / "config"
        assert paths.bellek_yolu(baska) == veri / "data" / "translation_memory.db"
        assert paths.paketli_kip() is True
        assert not veri.exists(), "yol çözümü klasör oluşturmamalı"

    @pytest.mark.parametrize("deger", ["", "   "])
    def test_bos_degisken_gelistirici_kipi(self, monkeypatch, tmp_path, deger):
        monkeypatch.setenv(VERI_ENV, deger)
        assert paths.veri_koku(tmp_path) == tmp_path
        assert paths.paketli_kip() is False

    def test_goreli_yol_proje_kokune_gore_ve_nokta_nokta_atilir(self, monkeypatch):
        monkeypatch.setenv(VERI_ENV, "../kitapcevir_veri_k5")
        kok = paths.veri_koku()
        assert kok == PROJECT_ROOT.parent / "kitapcevir_veri_k5"
        assert ".." not in str(kok) and ".." not in str(paths.bellek_yolu())

    def test_tilde_acilir(self, monkeypatch):
        monkeypatch.setenv(VERI_ENV, "~/kitapcevir_veri_k5")
        assert paths.veri_koku() == Path.home() / "kitapcevir_veri_k5"

    @pytest.mark.parametrize("ortam,beklenen", [
        ({}, False),
        ({VERI_ENV: "/tmp/veri"}, True),
        ({URL_ENV: "http://127.0.0.1:5/?anahtar=x&kip=bilesenler"}, True),
        ({VERI_ENV: "", URL_ENV: " "}, False),
    ])
    def test_paketli_kip(self, monkeypatch, ortam, beklenen):
        for ad, deger in ortam.items():
            monkeypatch.setenv(ad, deger)
        assert paths.paketli_kip() is beklenen


# ===========================================================================
# ui.runner (F1)
# ===========================================================================

class TestRunner:
    def test_gelistirici_kipi_yollar_birebir(self, tmp_path, mt_isolation):
        book = tmp_path / "Örnek Kitap.pdf"
        slug = runner.book_slug(book)
        root = runner.PROJECT_ROOT
        files = runner.tex_files(book)
        assert files.output_dir == root / "output" / slug
        assert files.export_tr_tex == root / "output" / slug / "EXPORT_TR.tex"
        assert runner.model_pdf(book) == root / "output" / slug / "compiled" / f"{slug}_TR.pdf"
        assert runner.memory_db_path() == root / "data" / "translation_memory.db"
        assert mt_isolation.original_engine_choice_path() == root / "config" / "mt_selection.json"

    def test_proje_koku_yamasi_gelistirici_kipinde_gecerli(self, tmp_path, monkeypatch):
        monkeypatch.setattr(runner, "PROJECT_ROOT", tmp_path)
        book = tmp_path / "kitap.epub"
        assert runner.tex_files(book).output_dir == tmp_path / "output" / "kitap"
        assert runner.memory_db_path() == tmp_path / "data" / "translation_memory.db"

    def test_paketli_kip_veri_yollari(self, veri, tmp_path, mt_isolation):
        book = tmp_path / "Örnek Kitap.pdf"
        slug = runner.book_slug(book)
        out = veri / "output" / slug
        files = runner.tex_files(book)
        assert files.output_dir == out
        assert files.export_tex == out / "EXPORT.tex"
        assert files.export_tr_tex == out / "EXPORT_TR.tex"
        assert files.directive == out / "CEVIRI_TALIMATI.md"
        assert files.translated_pdf == out / f"{slug}_TR.pdf"
        assert files.manual_marker == out / "source" / runner.MANUAL_MARKER_NAME
        assert runner.model_pdf(book) == out / "compiled" / f"{slug}_TR.pdf"
        assert runner.memory_db_path() == veri / "data" / "translation_memory.db"
        assert mt_isolation.original_engine_choice_path() == veri / "config" / "mt_selection.json"

    def test_paketli_kip_betikler_kod_kokunde(self, veri, tmp_path):
        book = tmp_path / "kitap.pdf"
        root = runner.PROJECT_ROOT
        assert runner.extract_command(book)[1] == str(root / "1_cikar.py")
        assert runner.compile_command(tmp_path / "EXPORT_TR.tex")[1] == str(root / "2_derle.py")
        assert runner.translate_command(book)[1] == str(root / "translate_book.py")

    def test_paketli_kip_secim_dosyasi_veri_kokune_yazilir(self, veri, monkeypatch,
                                                         mt_isolation):
        monkeypatch.setattr(runner, "engine_choice_path",
                            mt_isolation.original_engine_choice_path)
        gercek = PROJECT_ROOT / "config" / "mt_selection.json"
        once = gercek.read_bytes() if gercek.exists() else None
        assert runner.save_engine_choice("ornek-motor") is True
        yazilan = veri / "config" / "mt_selection.json"
        assert json.loads(yazilan.read_text(encoding="utf-8"))["engine"] == "ornek-motor"
        assert runner.load_engine_choice() == "ornek-motor"
        assert (gercek.read_bytes() if gercek.exists() else None) == once

    def test_paketli_kip_bellek_yolu_bellek_modulune_esit(self, veri):
        assert runner.memory_db_path() == Path(TranslationMemory._get_default_path(object()))

    def test_paketli_kip_bellek_sekmesi_veri_kokundeki_bellegi_okur(self, veri):
        with TranslationMemory() as tm:
            assert Path(tm.db_path) == veri / "data" / "translation_memory.db"
            assert tm.add("The lamp is warm.", "Lamba sıcak.") is True
        assert runner.memory_stats() == runner.MemoryStats(total=1, confirmed=0)


# ===========================================================================
# core.memory (F1)
# ===========================================================================

class TestMemory:
    def test_gelistirici_kipi_varsayilan_yol_birebir(self):
        import core.memory as memory

        eski = Path(memory.__file__).parent.parent / "data" / "translation_memory.db"
        assert TranslationMemory._get_default_path(object()) == str(eski)

    def test_paketli_kip_varsayilan_yol_ve_klasor(self, veri):
        assert not veri.exists()
        yol = TranslationMemory._get_default_path(object())
        assert yol == str(veri / "data" / "translation_memory.db")
        assert (veri / "data").is_dir(), "üst klasörler de oluşturulmalı"


# ===========================================================================
# yol_a_ortak.resolve_book_dir (F1; 2_derle kendi PROJECT_ROOT'unu verir)
# ===========================================================================

class TestResolveBookDir:
    def _tex(self, path: Path, meta: dict | None) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        head = build_meta_header(meta) if meta else ""
        path.write_text(head + "\\chapter{Bir}\nMetin.\n", encoding="utf-8")
        return path

    def test_paketli_kip_baslik_slug(self, veri, tmp_path):
        kitap = veri / "output" / "Ornek_Kitap"
        kitap.mkdir(parents=True)
        tex = self._tex(tmp_path / "masaustu" / "EXPORT_TR.tex",
                        {"slug": "Ornek_Kitap", "book_name": "Örnek Kitap"})
        res = resolve_book_dir(tex, tmp_path / "kod")
        assert res["source"] == "header-slug"
        assert Path(res["output_dir"]) == kitap.resolve()

    def test_paketli_kip_dizin_adi_gelenegi(self, veri, tmp_path):
        kitap = veri / "output" / "Ornek_Kitap"
        kitap.mkdir(parents=True)
        tex = self._tex(tmp_path / "baska" / "Ornek_Kitap" / "EXPORT_TR.tex", None)
        res = resolve_book_dir(tex, tmp_path / "kod")
        assert res["source"] == "convention"
        assert Path(res["output_dir"]) == kitap.resolve()

    def test_paketli_kip_kod_kokundeki_klasoru_kullanmaz(self, veri, tmp_path):
        (tmp_path / "kod" / "output" / "Ornek_Kitap").mkdir(parents=True)
        tex = self._tex(tmp_path / "masaustu" / "EXPORT_TR.tex", {"slug": "Ornek_Kitap"})
        res = resolve_book_dir(tex, tmp_path / "kod")
        assert res["output_dir"] is None
        assert any(str(veri / "output" / "Ornek_Kitap") in t for t in res["tried"])

    def test_gelistirici_kipi_proje_kokunden(self, tmp_path):
        kitap = tmp_path / "output" / "Ornek_Kitap"
        kitap.mkdir(parents=True)
        tex = self._tex(tmp_path / "masaustu" / "EXPORT_TR.tex", {"slug": "Ornek_Kitap"})
        res = resolve_book_dir(tex, tmp_path)
        assert Path(res["output_dir"]) == kitap.resolve()


# ===========================================================================
# 1_cikar.py ve translate_book.py (F1): uçtan uca, sahte çıkarıcıyla
# ===========================================================================

class TestCliPaketliKip:
    def test_1_cikar_veri_kokune_yazar(self, veri, tmp_path, monkeypatch, capsys):
        cikar = _load_cikar()
        kod = tmp_path / "kod"
        monkeypatch.setattr(cikar, "PROJECT_ROOT", kod)
        epub = _make_epub(tmp_path / "girdi" / "Ornek Kitap.epub")

        def _extract(epub_path, output_dir):
            (Path(output_dir) / "full_book.md").write_text(
                "# K\n\n" + "Lamp. " * 40, encoding="utf-8")
            return True

        def _pipeline(output_dir, book_name, **kwargs):
            src = Path(output_dir) / "source"
            src.mkdir(parents=True, exist_ok=True)
            (src / "body.tex").write_text("\\chapter{K}\n\n" + "x " * 80,
                                          encoding="utf-8")
            return 0

        monkeypatch.setattr(cikar, "run_epub_extraction", _extract)
        monkeypatch.setattr(cikar, "run_pipeline", _pipeline)
        monkeypatch.setattr(sys, "argv", ["1_cikar.py", str(epub), "--json"])
        assert cikar.main() == 0
        data = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        out = veri / "output" / data["slug"]
        assert Path(data["output_dir"]) == out
        assert Path(data["export_tex"]) == out / "EXPORT.tex"
        assert (out / "EXPORT.tex").is_file()
        assert (out / "kitap.json").is_file()
        assert not (kod / "output").exists()

    def test_translate_book_toplu_cikti_klasoru(self, veri, tmp_path, monkeypatch):
        monkeypatch.setattr(tb, "PROJECT_ROOT", tmp_path / "kod")
        args = SimpleNamespace(output_dir=None, name=None)
        beklenen = veri / "output" / tb.slugify(tb.guess_book_name("/a/Ornek Kitap.pdf"))
        assert tb._batch_output_dir("/a/Ornek Kitap.pdf", args) == str(beklenen)

    def test_translate_book_toplu_gelistirici_kipi(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tb, "PROJECT_ROOT", tmp_path)
        args = SimpleNamespace(output_dir=None, name="Ornek")
        assert tb._batch_output_dir("/a/x.pdf", args) == str(tmp_path / "output" / "Ornek")

    def test_translate_book_main_veri_kokune_yazar(self, veri, tmp_path, monkeypatch):
        kod = tmp_path / "kod"
        monkeypatch.setattr(tb, "PROJECT_ROOT", kod)
        monkeypatch.delenv("KITAPCEVIR_EVENT_LOG", raising=False)
        seen: list[str] = []

        def _extract_epub(epub_path, output_dir):
            return True

        def _pipeline(output_dir, book_name, **kwargs):
            seen.append(str(output_dir))
            return 0

        monkeypatch.setattr(tb, "run_epub_extraction", _extract_epub)
        monkeypatch.setattr(tb, "run_pipeline", _pipeline)
        epub = _make_epub(tmp_path / "girdi" / "Ornek Kitap.epub")
        monkeypatch.setattr(sys, "argv", ["translate_book.py", str(epub),
                                          "--no-epub-output"])
        with pytest.raises(SystemExit) as exc:
            tb.main()
        assert exc.value.code == 0
        out = veri / "output" / tb.slugify(tb.guess_book_name(str(epub)))
        assert seen == [str(out)]
        assert out.is_dir()
        assert not (kod / "output").exists()


# ===========================================================================
# core/mt_engines registry + discovery (F1; modül düzeyi adlar korunur)
# ===========================================================================

def _alt_surec_motor_yollari(ek_ortam: dict) -> dict:
    """Modül düzeyi adlar içe aktarma anında çözülür: taze süreçte bakılır."""
    code = ("import json\n"
            "from core.mt_engines import registry, discovery\n"
            "print(json.dumps({'models': str(registry.MODELS_DIR),"
            " 'specs': str(registry.USER_SPECS_PATH),"
            " 'sources': str(discovery.SOURCES_PATH)}))\n")
    env = {k: v for k, v in os.environ.items() if k not in (VERI_ENV, URL_ENV)}
    env.update(ek_ortam)
    done = subprocess.run([sys.executable, "-c", code], cwd=PROJECT_ROOT, env=env,
                          capture_output=True, text=True, timeout=180)
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


class TestMotorKatmani:
    def test_gelistirici_kipi_modul_adlari_birebir(self):
        yollar = _alt_surec_motor_yollari({})
        assert yollar == {
            "models": str(PROJECT_ROOT / "models"),
            "specs": str(PROJECT_ROOT / "config" / "mt_engines.json"),
            "sources": str(PROJECT_ROOT / "config" / "mt_sources.json"),
        }

    def test_paketli_kip_modul_adlari_veri_kokunden(self, tmp_path):
        veri = tmp_path / "veri kökü"
        yollar = _alt_surec_motor_yollari({VERI_ENV: str(veri)})
        assert yollar == {
            "models": str(veri / "models"),
            "specs": str(veri / "config" / "mt_engines.json"),
            "sources": str(veri / "config" / "mt_sources.json"),
        }, PATCH_NOTU


# ===========================================================================
# core/translator.py (F1): çeviri belleği ve MLX model kökü
# ===========================================================================

class TestTranslatorYollari:
    def _bellek_yolu(self, make_book_translator) -> Path:
        from core.translator import BookTranslator

        bt = make_book_translator()
        bt.config.project_name = "kitap"
        with patch("core.translator.TranslationMemory") as fake_memory:
            fake_memory.return_value.get_stats.return_value = {"total_entries": 0}
            BookTranslator._init_memory(bt)
        assert fake_memory.called, "bellek açılmadı (hata günlüğe yazılıp yutulmuş olabilir)"
        return Path(fake_memory.call_args.args[0])

    def test_gelistirici_kipi_bellek_yolu_birebir(self, make_book_translator):
        import core.translator as translator

        eski = Path(translator.__file__).parent.parent / "data" / "translation_memory.db"
        assert self._bellek_yolu(make_book_translator) == eski

    def test_paketli_kip_bellek_yolu(self, veri, make_book_translator):
        assert self._bellek_yolu(make_book_translator) == \
            veri / "data" / "translation_memory.db", PATCH_NOTU
        assert (veri / "data").is_dir()

    def _mlx_koku(self, monkeypatch, make_book_translator) -> tuple[list, str]:
        from core.translator import BookTranslator

        sahte = types.ModuleType("mlx_lm")
        sahte.load = lambda *a, **k: pytest.fail("model yüklenmemeli")
        monkeypatch.setitem(sys.modules, "mlx_lm", sahte)
        gorulen: list[Path] = []

        class _Motor:
            spec = SimpleNamespace(local_dir="hymt-7b-mlx")

            def resolve_local_model_dir(self, kok):
                gorulen.append(Path(kok))
                return None

            def convert_hint(self, kok):
                return "dönüştürme ipucu"

        bt = make_book_translator()
        bt.engine = _Motor()
        with pytest.raises(FileNotFoundError) as exc:
            BookTranslator._init_hymt_mlx(bt, "hymt-7b")
        return gorulen, str(exc.value)

    def test_gelistirici_kipi_mlx_koku_birebir(self, monkeypatch, make_book_translator):
        import core.translator as translator

        gorulen, ileti = self._mlx_koku(monkeypatch, make_book_translator)
        assert gorulen == [Path(translator.__file__).parent.parent / "models"]

    def test_paketli_kip_mlx_koku(self, veri, monkeypatch, make_book_translator):
        gorulen, ileti = self._mlx_koku(monkeypatch, make_book_translator)
        assert gorulen == [veri / "models"], PATCH_NOTU
        assert str(veri / "models" / "hymt-7b-mlx") in ileti


# ===========================================================================
# Bekçi: veri yolları kod kökünden türetilmez (TASARIM F1 "grep bekçisi")
# ===========================================================================

F1_DOSYALARI = (
    "1_cikar.py", "2_derle.py", "translate_book.py", "yol_a_ortak.py", "app.py",
    "ui/runner.py", "core/memory.py", "core/layout_detector.py",
    "core/translator.py", "core/mt_engines/registry.py", "core/mt_engines/discovery.py",
)
# Bir köke eklenen veri klasörü adı ya da bellek dosyasının adı. Yalnız
# config/paths.py yazar; yorum satırları ve yorum kısımları sayılmaz.
_VERI_YOLU = re.compile(
    r"""/\s*["'](?:output|models|data|config)["']|["']translation_memory\.db["']""")
# Veri kökü dışındaki meşru kullanımlar (tam satır): mlx-lm paketinin kendi
# ``models/`` alt paketi (mimari denetimi); kullanıcı verisi değil.
_IZINLI = {"core/mt_engines/discovery.py": {'models = Path(pkg) / "models"'}}


@pytest.mark.parametrize("relpath", F1_DOSYALARI)
def test_veri_yolu_kod_kokunden_turetilmez(relpath):
    src = (PROJECT_ROOT / relpath).read_text(encoding="utf-8")
    izinli = _IZINLI.get(relpath, set())
    bulunan = [f"{relpath}:{no}: {line.strip()}"
               for no, line in enumerate(src.splitlines(), 1)
               if _VERI_YOLU.search(line.split("#", 1)[0])
               and line.strip() not in izinli]
    assert bulunan == [], "\n".join(bulunan) + f"\n{PATCH_NOTU} (ya da config.paths kullan)"


def test_layout_detector_kisisel_yol_yok():
    src = (PROJECT_ROOT / "core" / "layout_detector.py").read_text(encoding="utf-8")
    assert "/Users/" not in src


def test_conftest_gelistirici_kipini_zorlar(tmp_path):
    """Kabukta veri kökü tanımlı olsa da test oturumu geliştirici kipinde başlar."""
    code = ("import os, runpy\n"
            f"runpy.run_path({str(PROJECT_ROOT / 'tests' / 'conftest.py')!r})\n"
            f"print(os.environ.get({VERI_ENV!r}), os.environ.get({URL_ENV!r}))\n")
    env = dict(os.environ, **{VERI_ENV: str(tmp_path / "gercek veri"),
                              URL_ENV: "http://127.0.0.1:5/?anahtar=x"})
    done = subprocess.run([sys.executable, "-c", code], cwd=PROJECT_ROOT, env=env,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip().splitlines()[-1] == "None None", PATCH_NOTU


# ===========================================================================
# F5: paketli kipte kurulum yönlendirmesi
# ===========================================================================

class TestKurulumYonlendirmesi:
    REHBER = "Kurulum rehberi: docs/KURULUM_VE_KULLANIM.md."

    def test_gelistirici_kipi_rehber(self):
        reason = runner._builtin_missing_reason()
        assert reason.startswith("Çeviri modeli kurulu değil: ")
        assert reason.endswith(self.REHBER)

    @pytest.mark.parametrize("ad,deger", [
        (VERI_ENV, "VERI"),
        (URL_ENV, "http://127.0.0.1:5/?anahtar=x&kip=bilesenler"),
    ])
    def test_paketli_kip_bilesenler_ekrani(self, monkeypatch, tmp_path, ad, deger):
        monkeypatch.setenv(ad, str(tmp_path / "veri") if deger == "VERI" else deger)
        assert hasattr(branding, "MODEL_INSTALL_HINT_PACKAGED"), PATCH_NOTU
        reason = runner._builtin_missing_reason()
        assert reason.startswith("Çeviri modeli kurulu değil: ")
        assert reason.endswith(branding.MODEL_INSTALL_HINT_PACKAGED)
        assert "docs/" not in reason and "Kurulum rehberi" not in reason

    def test_paketli_kip_metni_ekran_adini_ve_modeli_anar(self):
        assert hasattr(branding, "COMPONENTS_SCREEN"), PATCH_NOTU
        assert branding.COMPONENTS_SCREEN == "Bileşenler"
        assert branding.quoted(branding.COMPONENTS_SCREEN) in branding.MODEL_INSTALL_HINT_PACKAGED
        assert "HY-MT" in branding.MODEL_INSTALL_HINT_PACKAGED

    def test_paketli_kip_kart_nedeni(self, veri):
        assert hasattr(branding, "MODEL_INSTALL_HINT_PACKAGED"), PATCH_NOTU
        hazir, neden = runner.engine_ready(DEFAULT_ENGINE, None)
        assert hazir is False
        assert neden.endswith(branding.MODEL_INSTALL_HINT_PACKAGED)


# ===========================================================================
# core/layout_detector.py (F1 model yolu + F11 onnxruntime)
# ===========================================================================

class TestLayoutDetector:
    def test_gelistirici_kipi_model_yolu_birebir(self):
        eski = (Path(ld.__file__).parent.parent / "models" / "doclayout-yolo"
                / ld.LayoutDetector.MODEL_FILENAME)
        assert ld.LayoutDetector().model_path == eski

    def test_paketli_kip_model_yolu(self, veri):
        assert ld.LayoutDetector().model_path == (
            veri / "models" / "doclayout-yolo" / ld.LayoutDetector.MODEL_FILENAME)

    def test_onnxruntime_yoksa_acik_ileti_indirme_yok(self, tmp_path, monkeypatch):
        monkeypatch.setitem(sys.modules, "onnxruntime", None)
        monkeypatch.setattr(ld, "ort", None)
        monkeypatch.setattr(ld.LayoutDetector, "_ensure_model_downloaded",
                            lambda self: pytest.fail("onnxruntime yokken model indirilmemeli"))
        detector = ld.LayoutDetector(model_path=str(tmp_path / "yok.onnx"))
        with pytest.raises(ImportError) as exc:
            detector._load_model()
        assert str(exc.value) == ld.ONNXRUNTIME_MISSING_MSG
        assert "onnxruntime kurulu değil" in str(exc.value)
        assert isinstance(exc.value.__cause__, ImportError)
        assert detector.session is None

    def test_demo_argumansiz_ve_kitapsiz_kullanim(self, tmp_path):
        env = {k: v for k, v in os.environ.items() if k not in (VERI_ENV, URL_ENV)}
        env[VERI_ENV] = str(tmp_path / "bos veri")
        done = subprocess.run([sys.executable, "-m", "core.layout_detector"],
                              cwd=PROJECT_ROOT, env=env, capture_output=True,
                              text=True, timeout=120)
        assert done.returncode == 2, done.stderr[-2000:]
        assert "Kullanım: python -m core.layout_detector <pdf>" in done.stderr

    def test_demo_varsayilan_pdf_veri_kokunden(self, veri):
        kitap = veri / "output" / "Ornek_Kitap" / "source_book.pdf"
        kitap.parent.mkdir(parents=True)
        kitap.write_bytes(b"%PDF-1.4\n")
        assert ld._demo_pdf() == kitap


# ===========================================================================
# F12: ui/bicim.py
# ===========================================================================

BICIM_ADLARI = ("format_decimal", "format_percent", "SIZE_UNITS", "format_size")


def _bicim():
    return importlib.import_module("ui.bicim")


class TestBicim:
    @pytest.mark.parametrize("ad", BICIM_ADLARI)
    def test_runner_ayni_nesneyi_disa_verir(self, ad):
        assert getattr(runner, ad) is getattr(_bicim(), ad)

    def test_runner_kendi_kopyasini_tanimlamaz(self):
        agac = ast.parse((PROJECT_ROOT / "ui" / "runner.py").read_text(encoding="utf-8"))
        tanimli = set()
        for dugum in agac.body:
            if isinstance(dugum, (ast.FunctionDef, ast.AsyncFunctionDef)):
                tanimli.add(dugum.name)
            elif isinstance(dugum, (ast.Assign, ast.AnnAssign)):
                hedefler = dugum.targets if isinstance(dugum, ast.Assign) else [dugum.target]
                tanimli.update(h.id for h in hedefler if isinstance(h, ast.Name))
        assert tanimli.isdisjoint(BICIM_ADLARI)

    def test_yalniz_standart_kitaplik(self):
        code = ("import sys\n"
                f"sys.path.insert(0, {str(PROJECT_ROOT)!r})\n"
                "once = set(sys.modules)\n"
                "import ui.bicim\n"
                "yeni = {m.split('.')[0] for m in set(sys.modules) - once}\n"
                "print(sorted(yeni - set(sys.stdlib_module_names)))\n")
        # -I: kullanıcı ortamı yok; -S: site-packages yok (üçüncü taraf
        # paket içe aktarılsa hata verirdi). Yönetici yalnız stdlib Python'la koşar.
        done = subprocess.run([sys.executable, "-I", "-S", "-c", code],
                              capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, done.stderr
        assert done.stdout.strip() == "['ui']"

    @pytest.mark.parametrize("bayt,metin", [
        (0, "0 bayt"), (949, "949 bayt"), (999, "1 KB"), (1000, "1 KB"), (72_900, "72,9 KB"),
        (999_960, "1 MB"), (1_000_000, "1 MB"), (126_727_000, "126,7 MB"),
        (3_660_000_000, "3,7 GB"), (15_025_641_194, "15 GB"),
    ])
    def test_boyut_ornekleri(self, bayt, metin):
        assert _bicim().format_size(bayt) == metin

    def test_ondalik_ve_yuzde(self):
        bicim = _bicim()
        assert bicim.format_decimal(32.66) == "32,7"
        assert bicim.format_decimal(100.0) == "100"
        assert bicim.format_decimal(float("nan")) == "nan"
        assert bicim.format_percent(32.66) == "%32,7"
        assert bicim.format_size(15_025_641_194, 2) == "15,03 GB"
        assert bicim.SIZE_UNITS == (("GB", 10 ** 9), ("MB", 10 ** 6), ("KB", 10 ** 3))
