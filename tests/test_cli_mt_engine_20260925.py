"""Komut satırı: --mt-engine, erken model kontrolü ve çıkış kodu 5 (K2, TASARIM E).

Kapsam (TASARIM H.3 testleri 11-14):
   11. ``--mt-engine ollama:x`` sunucu kapalıyken çıkarım HİÇ çağrılmadan
       çıkış 5 (``EXIT_MODEL_UNAVAILABLE``); ``--batch`` aynı; çeviri
       koşmayacaksa (``--skip-translate`` / ``--export-tex`` / ``--import-tex``
       / ``--extract-only`` / çeviri bitmişken ``--resume``) model denetlenmez;
       Yol A kilidi (2) ve dil ön kapısı (4) önceliklidir.
   12. ``run_pipeline``: motor kontrolü başarısız -> 5; F3 kalite kapısında
       ``backend_health()`` doluysa 5; ``BackendUnavailable`` istisnasında
       deneme döngüsü beklemeden 5. Diğer hatalar eski kodlarını korur.
   13. Modül docstring'inde 5 satırı; ``--help`` "varsayilan: <DEFAULT>" ve
       kimlik biçimlerini anar.
   14. ``latex_pipeline/retranslator.py`` bağımsız CLI'ı ``--mt-engine``
       kabul eder; açıkça verilen model yüklenmeden denetlenir.

Varsayılan motor (yerleşik MLX) çıkarım ÖNCESİ denetlenmez; modeli
run_pipeline'daki ``_preflight_check``'te AYNI ``check_engine`` ile denetlenir
(başarısızlık yine 5). Gerekçe: arayüz "Çevir"i model klasörü yokken zaten
kapatır ve varsayılan motorla koşan mevcut akış testleri çıkarıma kadar
ilerlemeye güvenir (K2 RAPOR.md "riskler").

Testler MT modeli / Marker / XeLaTeX GEREKTİRMEZ: çıkarım ve pipeline sahte
fonksiyonlarla, çevirmen sahte sınıfla değiştirilir; sunucular sahte (tests/
fakes) ya da kapalı yerel porttur (tests/conftest.py ``_mt_isolation``).
PDF'ler PyMuPDF ile test sırasında üretilir; metinleri bu test için yazıldı.
"""

from __future__ import annotations

import json
import logging
import sys
import zipfile
from collections import namedtuple
from pathlib import Path

import fitz
import pytest

import translate_book as tb
from core.mt_engines import contracts, discovery, registry
from tests.fakes import write_fake_mlx_dir

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_OLLAMA_ID = "ollama:qwen3:8b"
_SENTENCE = "The small robot counts green apples on the kitchen table every morning."


# ===========================================================================
# Ortak yardımcılar
# ===========================================================================

def _make_pdf(path: Path) -> Path:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_textbox(fitz.Rect(50, 50, 550, 800), _SENTENCE, fontname="helv",
                        fontsize=10)
    doc.save(str(path))
    doc.close()
    return path


def _make_epub(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("OEBPS/ch0.xhtml",
                    "<?xml version='1.0' encoding='utf-8'?><html><head><title>A</title>"
                    f"</head><body><p>{_SENTENCE}</p></body></html>")
    return path


class _Recorder:
    def __init__(self):
        self.extraction: list = []
        self.epub_extraction: list = []
        self.pipeline: list = []
        self.batch: list = []
        self.checks: list = []


@pytest.fixture
def flow(monkeypatch):
    """Çıkarım / pipeline / batch sahte; motor denetimi gerçek ama kayıtlı."""
    rec = _Recorder()

    def _extract(pdf_path, output_dir, batch_size, device):
        rec.extraction.append(pdf_path)
        return True

    def _extract_epub(epub_path, output_dir):
        rec.epub_extraction.append(epub_path)
        return True

    def _pipeline(output_dir, book_name, **kwargs):
        rec.pipeline.append(kwargs)
        return 0

    def _batch(file_list, args):
        rec.batch.append(list(file_list))
        return {"failed": []}

    real_check = tb._check_engine

    def _check(engine_id):
        rec.checks.append(engine_id)
        return real_check(engine_id)

    monkeypatch.setattr(tb, "run_extraction", _extract)
    monkeypatch.setattr(tb, "run_epub_extraction", _extract_epub)
    monkeypatch.setattr(tb, "run_pipeline", _pipeline)
    monkeypatch.setattr(tb, "run_batch", _batch)
    monkeypatch.setattr(tb, "_check_engine", _check)
    monkeypatch.delenv("KITAPCEVIR_EVENT_LOG", raising=False)
    # Kapalı sunucu iletisi makineden bağımsız olsun (Ollama kurulu mu?).
    monkeypatch.setattr(discovery, "_ollama_installed", lambda: False)
    yield rec
    assert not logging.getLogger("pipeline.model").handlers, \
        "model durdurma günlüğü handler bıraktı"


def _main(monkeypatch, *argv) -> int:
    monkeypatch.setattr(sys, "argv", ["translate_book.py", *map(str, argv)])
    with pytest.raises(SystemExit) as exc:
        tb.main()
    return exc.value.code


def _point_ollama(monkeypatch, iso, url: str) -> None:
    cfg = contracts.SourcesConfig(ollama_url=url, lmstudio_url=iso.closed_url + "/v1")
    monkeypatch.setattr(discovery, "load_sources", lambda path=None, *a, **k: cfg)


def _down(iso) -> str:
    return contracts.MSG_OLLAMA_DOWN.format(url=iso.closed_url)


# ===========================================================================
# 11. Erken model kontrolü (main)
# ===========================================================================

class TestErkenKontrol:
    def test_sunucu_kapaliyken_cikarimdan_once_5(self, tmp_path, monkeypatch, capsys, flow,
                                                  mt_isolation):
        pdf = _make_pdf(tmp_path / "kitap.pdf")
        out = tmp_path / "out"
        code = _main(monkeypatch, pdf, "--output-dir", out, "--mt-engine", _OLLAMA_ID,
                     "--dil-kontrolu-yok")
        assert code == tb.EXIT_MODEL_UNAVAILABLE == 5
        assert flow.extraction == [] and flow.pipeline == []
        assert flow.checks == [_OLLAMA_ID]
        err = capsys.readouterr().err
        assert f"HATA: {_down(mt_isolation)}" in err
        assert not (out / "source_book.pdf").exists(), "kitap kopyalanmamalıydı"
        assert _down(mt_isolation) in (out / tb.PIPELINE_LOG_NAME).read_text(encoding="utf-8")

    def test_epub_de_ayni(self, tmp_path, monkeypatch, flow, mt_isolation):
        epub = _make_epub(tmp_path / "kitap.epub")
        code = _main(monkeypatch, epub, "--output-dir", tmp_path / "out", "--mt-engine",
                     _OLLAMA_ID, "--dil-kontrolu-yok")
        assert code == tb.EXIT_MODEL_UNAVAILABLE
        assert flow.epub_extraction == []

    def test_olay_akisina_hata_olarak_ulasir(self, tmp_path, monkeypatch, flow, mt_isolation):
        events = tmp_path / "events.jsonl"
        monkeypatch.setenv("KITAPCEVIR_EVENT_LOG", str(events))
        pdf = _make_pdf(tmp_path / "kitap.pdf")
        assert _main(monkeypatch, pdf, "--output-dir", tmp_path / "out", "--mt-engine",
                     _OLLAMA_ID, "--dil-kontrolu-yok") == tb.EXIT_MODEL_UNAVAILABLE
        rows = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        errors = [r for r in rows if r.get("kind") == "error"]
        assert errors and errors[-1]["message"] == _down(mt_isolation)

    @pytest.mark.parametrize("engine_id,fragment", [
        ("olmayan-motor", "Desteklenmeyen MT motoru"),
        ("openai:http://10.0.0.5:8080/v1|m", "yerel bir adres değil"),
        ("mlx:../x", "Geçersiz model kimliği"),
        ("mlx:olmayan-mlx", "MLX model klasörü bulunamadı"),
        ("gguf:olmayan.gguf", "GGUF dosyası bulunamadı"),
        ("lmstudio:qwen/qwen3-8b", "LM Studio sunucusu kapalı"),
    ])
    def test_kullanilamayan_kimlik_5(self, tmp_path, monkeypatch, capsys, flow, engine_id,
                                     fragment):
        pdf = _make_pdf(tmp_path / "kitap.pdf")
        code = _main(monkeypatch, pdf, "--output-dir", tmp_path / "out", "--mt-engine",
                     engine_id, "--dil-kontrolu-yok")
        assert code == tb.EXIT_MODEL_UNAVAILABLE
        assert flow.extraction == []
        err = capsys.readouterr().err
        assert err.startswith("HATA: ") and fragment in err

    def test_ipucu_satiri(self, tmp_path, monkeypatch, capsys, flow, mt_isolation):
        mt_isolation.user_specs_path.write_text(json.dumps({"yeni": {
            "format": "chat", "local_dir": "yeni-mlx", "model_id": "org/yeni"}}),
            encoding="utf-8")
        pdf = _make_pdf(tmp_path / "kitap.pdf")
        assert _main(monkeypatch, pdf, "--output-dir", tmp_path / "out", "--mt-engine",
                     "yeni", "--dil-kontrolu-yok") == tb.EXIT_MODEL_UNAVAILABLE
        err = capsys.readouterr().err.splitlines()
        assert err[0].startswith("HATA: MLX model klasörü bulunamadı")
        assert err[1].startswith("İpucu: mlx_lm.convert --hf-path org/yeni")

    def test_model_hazirsa_cikarim_baslar(self, tmp_path, monkeypatch, flow, mt_isolation,
                                          fake_ollama):
        _point_ollama(monkeypatch, mt_isolation, fake_ollama.url)
        pdf = _make_pdf(tmp_path / "kitap.pdf")
        assert _main(monkeypatch, pdf, "--output-dir", tmp_path / "out", "--mt-engine",
                     _OLLAMA_ID, "--dil-kontrolu-yok") == 0
        assert flow.extraction == [str(pdf.resolve())]
        assert flow.pipeline[0]["mt_engine"] == _OLLAMA_ID
        # Denetim modeli yüklemez: yalnız liste + üst veri ucu. (Vk 2026-09-28:
        # /api/show ön kontrole eklendi — gömme modelini keşifle aynı biçimde
        # çıkarımdan ÖNCE yakalamak için; /api/show modeli yüklemez/ısıtmaz.)
        assert {p for _m, p, _b in fake_ollama.requests} <= {"/api/tags", "/api/show"}
        assert not fake_ollama.requests_to("/api/chat")
        assert not fake_ollama.requests_to("/api/generate")

    def test_batch_donguden_once_5(self, tmp_path, monkeypatch, capsys, flow, mt_isolation):
        a = _make_pdf(tmp_path / "a.pdf")
        b = _make_pdf(tmp_path / "b.pdf")
        code = _main(monkeypatch, a, "--batch", a, b, "--mt-engine", _OLLAMA_ID)
        assert code == tb.EXIT_MODEL_UNAVAILABLE
        assert flow.batch == [] and flow.extraction == []
        assert f"HATA: {_down(mt_isolation)}" in capsys.readouterr().err

    def test_batch_model_hazirsa_doner(self, tmp_path, monkeypatch, flow, mt_isolation,
                                       fake_ollama):
        _point_ollama(monkeypatch, mt_isolation, fake_ollama.url)
        a = _make_pdf(tmp_path / "a.pdf")
        assert _main(monkeypatch, a, "--batch", a, "--mt-engine", _OLLAMA_ID) == 0
        assert flow.batch == [[str(a)]]
        assert flow.checks == [_OLLAMA_ID]

    @pytest.mark.parametrize("flags", [["--skip-translate"], ["--export-tex"],
                                       ["--extract-only"], ["--import-tex"]])
    def test_ceviri_kosmayacaksa_denetlenmez(self, tmp_path, monkeypatch, flow, flags):
        pdf = _make_pdf(tmp_path / "kitap.pdf")
        argv = list(flags)
        if flags == ["--import-tex"]:
            tr = tmp_path / "EXPORT_TR.tex"
            tr.write_text("\\chapter{Giriş}\n" + "Türkçe deneme cümlesi. " * 10,
                          encoding="utf-8")
            argv.append(tr)
        code = _main(monkeypatch, pdf, "--output-dir", tmp_path / "out", "--mt-engine",
                     _OLLAMA_ID, *argv)
        assert code == 0
        assert flow.checks == []

    def test_resume_ceviri_bitmisse_denetlenmez(self, tmp_path, monkeypatch, flow):
        out = tmp_path / "out"
        (out / "source").mkdir(parents=True)
        (out / "source" / "translated_body.tex").write_text(
            "\\chapter{Bir}\n" + "Çevrilmiş gövde. " * 20, encoding="utf-8")
        (out / "full_book.md").write_text("# A\n\nMetin.\n", encoding="utf-8")
        pdf = _make_pdf(tmp_path / "kitap.pdf")
        assert _main(monkeypatch, pdf, "--output-dir", out, "--mt-engine", _OLLAMA_ID,
                     "--resume", "--skip-extract") == 0
        assert flow.checks == []

    def test_resume_force_mt_denetler(self, tmp_path, monkeypatch, flow):
        out = tmp_path / "out"
        (out / "source").mkdir(parents=True)
        (out / "source" / "translated_body.tex").write_text(
            "\\chapter{Bir}\n" + "Çevrilmiş gövde. " * 20, encoding="utf-8")
        pdf = _make_pdf(tmp_path / "kitap.pdf")
        assert _main(monkeypatch, pdf, "--output-dir", out, "--mt-engine", _OLLAMA_ID,
                     "--resume", "--force-mt", "--skip-extract") == tb.EXIT_MODEL_UNAVAILABLE
        assert flow.checks == [_OLLAMA_ID]

    def test_resume_yarim_ceviri_denetler(self, tmp_path, monkeypatch, flow):
        out = tmp_path / "out"
        (out / "source").mkdir(parents=True)
        (out / "source" / "translated_body.tex").write_text("kısa", encoding="utf-8")
        pdf = _make_pdf(tmp_path / "kitap.pdf")
        assert _main(monkeypatch, pdf, "--output-dir", out, "--mt-engine", _OLLAMA_ID,
                     "--resume", "--skip-extract") == tb.EXIT_MODEL_UNAVAILABLE

    def test_yol_a_kilidi_oncelikli(self, tmp_path, monkeypatch, flow):
        out = tmp_path / "out"
        (out / "source").mkdir(parents=True)
        (out / "source" / tb.MANUAL_MARKER_NAME).write_text("import: test\n",
                                                             encoding="utf-8")
        pdf = _make_pdf(tmp_path / "kitap.pdf")
        code = _main(monkeypatch, pdf, "--output-dir", out, "--mt-engine", _OLLAMA_ID)
        assert code == tb.EXIT_MANUAL_LOCK
        assert flow.checks == [] and flow.extraction == []

    def test_yol_a_kilidi_force_mt_ile_model_denetlenir(self, tmp_path, monkeypatch, flow):
        out = tmp_path / "out"
        (out / "source").mkdir(parents=True)
        (out / "source" / tb.MANUAL_MARKER_NAME).write_text("import: test\n",
                                                             encoding="utf-8")
        pdf = _make_pdf(tmp_path / "kitap.pdf")
        code = _main(monkeypatch, pdf, "--output-dir", out, "--mt-engine", _OLLAMA_ID,
                     "--force-mt", "--dil-kontrolu-yok")
        assert code == tb.EXIT_MODEL_UNAVAILABLE
        assert flow.checks == [_OLLAMA_ID]

    def test_dil_on_kapisi_oncelikli(self, tmp_path, monkeypatch, flow):
        calls = []

        def _pregate(*args, **kwargs):
            calls.append(args)
            return tb.EXIT_SOURCE_NOT_ENGLISH

        monkeypatch.setattr(tb, "_source_language_pregate", _pregate)
        pdf = _make_pdf(tmp_path / "kitap.pdf")
        code = _main(monkeypatch, pdf, "--output-dir", tmp_path / "out", "--mt-engine",
                     _OLLAMA_ID)
        assert code == tb.EXIT_SOURCE_NOT_ENGLISH
        assert calls and flow.checks == [] and flow.extraction == []

    def test_varsayilan_motor_cikarim_oncesi_denetlenmez(self, tmp_path, monkeypatch, flow):
        # Varsayılan motorun modeli run_pipeline ön kontrolünde denetlenir
        # (bkz. TestRunPipeline.test_varsayilan_motor_modeli_yok_5).
        pdf = _make_pdf(tmp_path / "kitap.pdf")
        assert _main(monkeypatch, pdf, "--output-dir", tmp_path / "out",
                     "--dil-kontrolu-yok") == 0
        assert flow.checks == []
        assert flow.extraction and flow.pipeline[0]["mt_engine"] == registry.DEFAULT_ENGINE


# ===========================================================================
# 12. run_pipeline
# ===========================================================================

class _FakeStats:
    def __init__(self, total=0, translated=0, failed=0, skipped=0):
        self.segments_total = total
        self.segments_translated = translated
        self.segments_failed = failed
        self.segments_skipped = skipped


class _FakeTranslator:
    """LatexTranslator yerine: model yüklemez; senaryo sınıf değişkenlerinde."""

    stats_spec = (10, 10, 0)
    health = None                 # backend_health() dönüşü
    error: Exception | None = None
    instances: list = []
    translate_calls = 0

    def __init__(self, **kwargs):
        type(self).instances.append(kwargs)
        self.stats = _FakeStats(*type(self).stats_spec)

    def translate(self, latex_body, checkpoint_dir=None, resume=False,
                  progress_callback=None):
        type(self).translate_calls += 1
        if type(self).error is not None:
            raise type(self).error
        return latex_body

    def backend_health(self):
        return type(self).health


@pytest.fixture
def restore_root_logger():
    root = logging.getLogger()
    old_handlers = list(root.handlers)
    old_level = root.level
    yield
    for handler in list(root.handlers):
        if handler not in old_handlers:
            try:
                handler.close()
            except Exception:
                pass
    root.handlers[:] = old_handlers
    root.setLevel(old_level)


@pytest.fixture
def book_dir(tmp_path):
    out = tmp_path / "kitap"
    (out / "source").mkdir(parents=True)
    (out / "full_book.md").write_text(
        "# Birinci Bölüm\n\n" + (_SENTENCE + " ") * 12 + "\n", encoding="utf-8")
    return out


@pytest.fixture
def translator_env(monkeypatch, restore_root_logger):
    _FakeTranslator.stats_spec = (10, 10, 0)
    _FakeTranslator.health = None
    _FakeTranslator.error = None
    _FakeTranslator.instances = []
    _FakeTranslator.translate_calls = 0
    monkeypatch.setattr("latex_pipeline.translator.LatexTranslator", _FakeTranslator)
    sleeps = []
    monkeypatch.setattr(tb.time, "sleep", lambda s: sleeps.append(s))
    _FakeTranslator.sleeps = sleeps
    return _FakeTranslator


def _run(out: Path, **kwargs) -> int:
    defaults = dict(skip_translate=False, skip_compile=True, skip_qc=True,
                    book_type="science", pdf_path=None)
    defaults.update(kwargs)
    return tb.run_pipeline(str(out), "Test Kitap", **defaults)


def _log(out: Path) -> str:
    return (out / tb.PIPELINE_LOG_NAME).read_text(encoding="utf-8")


def _ok_check(engine_id):
    return contracts.EngineCheck(engine_id=engine_id, ok=True, label=engine_id)


class TestRunPipeline:
    def test_motor_kontrolu_basarisiz_5(self, book_dir, translator_env, monkeypatch, capsys,
                                        mt_isolation):
        monkeypatch.setattr(discovery, "_ollama_installed", lambda: False)
        preflight = []
        monkeypatch.setattr(tb, "_preflight_check",
                            lambda *a, **k: preflight.append(1) or [])
        assert _run(book_dir, mt_engine=_OLLAMA_ID) == tb.EXIT_MODEL_UNAVAILABLE
        assert translator_env.instances == [] and preflight == []
        assert f"HATA: {_down(mt_isolation)}" in capsys.readouterr().err

    def test_ceviri_kosmayan_run_pipeline_denetlemez(self, book_dir, translator_env,
                                                     monkeypatch):
        calls = []
        monkeypatch.setattr(tb, "_check_engine", lambda e: calls.append(e) or _ok_check(e))
        monkeypatch.setattr(tb, "_preflight_check", lambda *a, **k: [])
        (book_dir / "source" / "translated_body.tex").write_text(
            "\\chapter{Bir}\n" + "Çevrilmiş gövde. " * 20, encoding="utf-8")
        assert _run(book_dir, mt_engine=_OLLAMA_ID, resume=True) == 0
        assert _run(book_dir, mt_engine=_OLLAMA_ID, skip_translate=True, resume=True) == 0
        assert calls == []

    def test_varsayilan_motor_modeli_yok_5(self, book_dir, translator_env, capsys):
        # Model klasörü yok (yalıtım: boş models/) -> ön kontrol çıkış 5; ileti
        # erken kontrolle aynı biçimde (stderr + pipeline.log).
        assert _run(book_dir) == tb.EXIT_MODEL_UNAVAILABLE
        assert translator_env.instances == []
        err = capsys.readouterr().err
        assert "HATA: Çeviri modeli kurulu değil" in err
        assert "İpucu: mlx_lm.convert" in err
        assert "Çeviri modeli kurulu değil" in _log(book_dir)

    def test_on_kontrol_model_bayragi(self, mt_isolation, monkeypatch):
        failures = tb._preflight_check(False, True, None, mt_engine=registry.DEFAULT_ENGINE)
        model_lines = [f for f in failures if "Çeviri modeli kurulu değil" in f]
        assert failures.model_unavailable is True and len(model_lines) == 1
        assert model_lines[0] == failures.model_message
        assert "İpucu: mlx_lm.convert" in model_lines[0]
        assert failures.engine_check.ok is False
        checked = tb._preflight_check(False, True, None, mt_engine=registry.DEFAULT_ENGINE,
                                      engine_checked=True)
        assert checked.model_unavailable is False
        assert not any("Çeviri modeli" in f for f in checked)
        # Model varsa model hatası yok.
        monkeypatch.setattr(discovery, "mlx_arch_supported", lambda arch: True)
        local_dir = registry.BUILTIN_SPECS[registry.DEFAULT_ENGINE].local_dir
        write_fake_mlx_dir(mt_isolation.models_dir / local_dir, "hunyuan_v1_dense")
        ready = tb._preflight_check(False, True, None, mt_engine=registry.DEFAULT_ENGINE)
        assert ready.model_unavailable is False
        assert not any("Çeviri modeli" in f for f in ready)

    def test_bilinmeyen_motor_iletisi_adi_icerir(self):
        failures = tb._preflight_check(False, True, None, mt_engine="olmayan-motor")
        assert failures.model_unavailable is True
        assert any("olmayan-motor" in f and "Desteklenmeyen MT motoru" in f for f in failures)

    def test_model_disi_on_kontrol_hatasi_1(self, book_dir, translator_env, monkeypatch):
        monkeypatch.setattr(tb, "_check_engine", _ok_check)
        Usage = namedtuple("Usage", "total used free")
        monkeypatch.setattr(tb.shutil, "disk_usage", lambda p: Usage(10, 9, 1))
        assert _run(book_dir) == 1

    def test_f3_kapisi_sunucu_koptuysa_5(self, book_dir, translator_env, monkeypatch):
        monkeypatch.setattr(tb, "_preflight_check", lambda *a, **k: [])
        translator_env.stats_spec = (10, 0, 10)
        lost = contracts.MSG_SERVER_LOST.format(url="http://127.0.0.1:11434")
        translator_env.health = lost
        assert _run(book_dir) == tb.EXIT_MODEL_UNAVAILABLE
        log = _log(book_dir)
        assert "KALİTE KAPISI REDDETTİ" in log and f"MODEL KULLANILAMADI: {lost}" in log
        assert (book_dir / "source" / "translated_body.FAILED.tex").exists()
        assert not (book_dir / "source" / "translated_body.tex").exists()

    def test_gec_kopma_oran_esigin_altinda_yine_5(self, book_dir, translator_env,
                                                  monkeypatch):
        # Vk 2026-09-28: sunucu koşunun sonuna doğru koptu; başarısız oran
        # eşiğin altında (3/10) ama devre açık -> kalan segmentler denenmedi.
        # Gövde translated_body.tex'e YAZILMAZ (yazılsaydı --resume Phase 5'i
        # atlayıp İngilizce kalan kısmı bir daha çevirmezdi); çıkış 5.
        monkeypatch.setattr(tb, "_preflight_check", lambda *a, **k: [])
        translator_env.stats_spec = (10, 7, 3)
        lost = contracts.MSG_SERVER_LOST.format(url="http://127.0.0.1:11434")
        translator_env.health = lost
        assert _run(book_dir) == tb.EXIT_MODEL_UNAVAILABLE
        log = _log(book_dir)
        assert "KALİTE KAPISI REDDETTİ: model arka ucu koşu sırasında" in log
        assert f"MODEL KULLANILAMADI: {lost}" in log
        assert (book_dir / "source" / "translated_body.FAILED.tex").exists()
        assert not (book_dir / "source" / "translated_body.tex").exists()

    def test_saglikli_arka_uc_oran_esigin_altinda_basari(self, book_dir, translator_env,
                                                         monkeypatch):
        # Aynı oran, arka uç sağlıklı (MLX yolu / kopma yok): eski davranış, gövde yazılır.
        monkeypatch.setattr(tb, "_preflight_check", lambda *a, **k: [])
        translator_env.stats_spec = (10, 7, 3)
        translator_env.health = None
        _run(book_dir)
        assert (book_dir / "source" / "translated_body.tex").exists()
        assert "MODEL KULLANILAMADI" not in _log(book_dir)

    def test_f3_kapisi_saglikli_arka_ucla_eski_kod(self, book_dir, translator_env,
                                                   monkeypatch):
        monkeypatch.setattr(tb, "_preflight_check", lambda *a, **k: [])
        translator_env.stats_spec = (10, 0, 10)
        translator_env.health = None
        assert _run(book_dir) == 1
        assert "MODEL KULLANILAMADI" not in _log(book_dir)

    def test_saglik_sorgusu_patlarsa_eski_kod(self, book_dir, translator_env, monkeypatch):
        monkeypatch.setattr(tb, "_preflight_check", lambda *a, **k: [])
        translator_env.stats_spec = (10, 0, 10)

        def _boom(self):
            raise RuntimeError("sağlık sorgusu bozuk")

        monkeypatch.setattr(_FakeTranslator, "backend_health", _boom)
        assert _run(book_dir) == 1

    def test_arka_uc_yoksa_beklemeden_5(self, book_dir, translator_env, monkeypatch):
        monkeypatch.setattr(tb, "_preflight_check", lambda *a, **k: [])
        translator_env.error = contracts.BackendUnavailable(
            contracts.MSG_OLLAMA_DOWN.format(url="http://127.0.0.1:11434"),
            hint="Ollama uygulamasını açın")
        assert _run(book_dir) == tb.EXIT_MODEL_UNAVAILABLE
        assert translator_env.translate_calls == 1, "yeniden denenmemeliydi"
        assert translator_env.sleeps == []
        log = _log(book_dir)
        assert "MODEL KULLANILAMADI: Ollama sunucusu kapalı" in log
        assert "İpucu: Ollama uygulamasını açın" in log

    def test_genel_hata_yine_uc_kez_denenir(self, book_dir, translator_env, monkeypatch):
        monkeypatch.setattr(tb, "_preflight_check", lambda *a, **k: [])
        translator_env.error = RuntimeError("geçici hata")
        assert _run(book_dir) == 1
        assert translator_env.translate_calls == 3
        assert translator_env.sleeps == [2, 2]

    def test_phase5_gunlugunde_model_adi(self, book_dir, translator_env, monkeypatch):
        monkeypatch.setattr(tb, "_check_engine", _ok_check)
        monkeypatch.setattr(tb, "_preflight_check", lambda *a, **k: [])
        assert _run(book_dir, mt_engine=_OLLAMA_ID) == 0
        assert "Model: qwen3:8b (Ollama)" in _log(book_dir)
        assert translator_env.instances[0]["mt_engine"] == _OLLAMA_ID


# ===========================================================================
# 13. Belge ve yardım
# ===========================================================================

class TestYardimVeBelge:
    def test_cikis_kodu(self):
        assert tb.EXIT_MODEL_UNAVAILABLE == 5
        assert tb.EXIT_MODEL_UNAVAILABLE not in (
            0, 1, tb.EXIT_MANUAL_LOCK, 3, tb.EXIT_SOURCE_NOT_ENGLISH, 130)

    def test_modul_belgesinde_5_satiri(self):
        doc = tb.__doc__
        assert "    5    seçilen çeviri modeli kullanılamıyor" in doc
        assert "(EXIT_MODEL_UNAVAILABLE)" in doc

    def test_yardim_metni(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["translate_book.py", "--help"])
        with pytest.raises(SystemExit):
            tb.parse_args()
        text = " ".join(capsys.readouterr().out.split())
        assert f"varsayilan: {registry.DEFAULT_ENGINE}" in text
        for form in ("mlx:<klasör>", "gguf:<dosya>", "ollama:<model>", "lmstudio:<model>",
                     "openai:<adres>|<model>"):
            assert form in text, form
        assert "python -m core.mt_engines.discovery" in text
        assert "çıkış kodu 5" in text

    def test_yardim_varsayilani_cagri_aninda(self, monkeypatch, capsys):
        monkeypatch.setattr(registry, "DEFAULT_ENGINE", "deneme-motor")
        monkeypatch.setattr(sys, "argv", ["translate_book.py", "kitap.pdf"])
        assert tb.parse_args().mt_engine == "deneme-motor"


# ===========================================================================
# 14. retranslator bağımsız CLI
# ===========================================================================

class _FakeLatexTranslator:
    instances: list = []

    def __init__(self, **kwargs):
        type(self).instances.append(kwargs)

    def _safe_translate(self, text):
        return text


@pytest.fixture
def retr_env(tmp_path, monkeypatch):
    from latex_pipeline import retranslator

    _FakeLatexTranslator.instances = []
    monkeypatch.setattr("latex_pipeline.translator.LatexTranslator", _FakeLatexTranslator)
    monkeypatch.setattr(discovery, "_ollama_installed", lambda: False)
    tex = tmp_path / "kitap" / "source" / "t.tex"
    tex.parent.mkdir(parents=True)
    tex.write_text("\\chapter{Giriş}\nKısa Türkçe metin.\n", encoding="utf-8")

    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["retranslator", str(tex),
                                          "--output", str(tmp_path / "cikti.tex"), *argv])
        retranslator._cli_main()

    return run


class TestRetranslatorCli:
    def test_varsayilan_motor_cagri_aninda(self, retr_env, monkeypatch):
        monkeypatch.setattr(registry, "DEFAULT_ENGINE", "deneme-motor")
        retr_env()
        assert _FakeLatexTranslator.instances[0]["mt_engine"] == "deneme-motor"

    def test_acik_kimlik_hazirsa_kullanilir(self, retr_env, monkeypatch, mt_isolation,
                                            fake_ollama):
        _point_ollama(monkeypatch, mt_isolation, fake_ollama.url)
        retr_env("--mt-engine", _OLLAMA_ID)
        assert _FakeLatexTranslator.instances[0]["mt_engine"] == _OLLAMA_ID

    @pytest.mark.parametrize("engine_id,fragment", [
        (_OLLAMA_ID, "Ollama sunucusu kapalı"),
        ("openai:http://10.0.0.5:8080/v1|m", "yerel bir adres değil"),
        ("olmayan-motor", "Desteklenmeyen MT motoru"),
    ])
    def test_kullanilamayan_kimlik_yuklemeden_durur(self, retr_env, caplog, engine_id,
                                                    fragment):
        with caplog.at_level(logging.ERROR, logger="latex_pipeline.retranslator"):
            with pytest.raises(SystemExit) as exc:
                retr_env("--mt-engine", engine_id)
        assert exc.value.code == 1
        assert _FakeLatexTranslator.instances == []
        assert any(fragment in r.getMessage() for r in caplog.records)

    def test_yardim_varsayilani(self, monkeypatch, capsys):
        from latex_pipeline import retranslator

        monkeypatch.setattr(sys, "argv", ["retranslator", "--help"])
        with pytest.raises(SystemExit):
            retranslator._cli_main()
        text = " ".join(capsys.readouterr().out.split())
        assert f"varsayılan: {registry.DEFAULT_ENGINE}" in text
        assert "openai:<adres>|<model>" in text
