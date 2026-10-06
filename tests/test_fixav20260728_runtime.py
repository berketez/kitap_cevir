"""FIX-AV 2026-07-28 — translate_book çalışma-zamanı bulguları (F3 / F11 / F14).

Kapsanan bulgular:
    F3  — Başarısız (>%50 fail) çeviri gövdesi translated_body.tex'e koşulsuz
          yazılıp faz düzeyindeki --resume'un kaynağı oluyordu; translator'ın
          FINAL checkpoint reddi böylece bypass ediliyordu.
    F11 — Phase 4.5 auto-terms ölü işti: 122 MB sözlük her Yol B koşusunda
          yükleniyor, sonuç hiçbir çıktı baytını etkilemiyordu.
    F14 — UTF-8 olmayan EXPORT_TR.tex --import-tex/2_derle akışını ham
          traceback ile çökertiyor, --json sözleşmesini bozuyordu; BOM gövdeye
          sızıyordu.

Testler MT modeli GEREKTİRMEZ: LatexTranslator sahte bir sınıfla değiştirilir
(pipeline testleri) veya MagicMock backend'le kurulur (çıktı-nötrlüğü testi).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

import translate_book as tb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TRANSLATOR_PY = PROJECT_ROOT / "latex_pipeline" / "translator.py"


# ===========================================================================
# Ortak yardımcılar
# ===========================================================================

class _FakeStats:
    """TranslationStats'ın kalite kapısı için gereken alanları."""

    def __init__(self, total=0, translated=0, failed=0, skipped=0):
        self.segments_total = total
        self.segments_translated = translated
        self.segments_failed = failed
        self.segments_skipped = skipped


class _FakeTranslator:
    """LatexTranslator yerine geçen, model yüklemeyen sahte çevirmen.

    ``stats_spec`` sınıf değişkeniyle her testin senaryosu ayarlanır.
    Kurucu argümanları ``instances`` listesinde toplanır (F11 kilidi).
    """

    stats_spec = (10, 10, 0)     # (total, translated, failed)
    instances: list = []
    body_transform = staticmethod(lambda body: body)

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        type(self).instances.append(kwargs)
        self.stats = _FakeStats(*type(self).stats_spec)

    def translate(self, latex_body, checkpoint_dir=None, resume=False,
                  progress_callback=None):
        return type(self).body_transform(latex_body)


@pytest.fixture
def restore_root_logger():
    """run_pipeline kök logger'ı ele geçirir; test sonrası eski hâle döndür."""
    root = logging.getLogger()
    old_handlers = list(root.handlers)
    old_level = root.level
    yield
    for h in list(root.handlers):
        if h not in old_handlers:
            try:
                h.close()
            except Exception:
                pass
    root.handlers[:] = old_handlers
    root.setLevel(old_level)


@pytest.fixture
def book_dir(tmp_path):
    """Minimum çalışır çıktı dizini (full_book.md zorunlu, A-10)."""
    out = tmp_path / "kitap"
    (out / "source").mkdir(parents=True)
    md = (
        "# Birinci Bolum\n\n"
        + "This is a paragraph about rocket engines and combustion. " * 12
        + "\n\n## Alt baslik\n\n"
        + "Another paragraph with plain English sentences here. " * 12
        + "\n"
    )
    (out / "full_book.md").write_text(md, encoding="utf-8")
    return out


@pytest.fixture
def pipeline_env(monkeypatch):
    """Model/xelatex gerektirmeyen pipeline ortamı + sahte çevirmen."""
    _FakeTranslator.instances = []
    _FakeTranslator.stats_spec = (10, 10, 0)
    _FakeTranslator.body_transform = staticmethod(lambda body: body)

    monkeypatch.setattr(tb, "_preflight_check", lambda *a, **k: [])
    monkeypatch.setattr("latex_pipeline.translator.LatexTranslator",
                        _FakeTranslator)
    # G2c (KARAR 27, 2026-10-05): latex_pipeline.terminology_engine (ölü
    # kod) SİLİNDİ; eskiden burada kurulan casus artık gerekmiyor — F11
    # kilidi aşağıda modülün yokluğu ve DictionaryLoader casusuyla durur.
    return _FakeTranslator


def _run(out: Path, **kwargs) -> int:
    defaults = dict(
        skip_translate=False, skip_compile=True, skip_qc=True,
        book_type="science", pdf_path=None,
    )
    defaults.update(kwargs)
    return tb.run_pipeline(str(out), "Test Kitap", **defaults)


def _log(out: Path) -> str:
    return (out / "pipeline.log").read_text(encoding="utf-8")


# ===========================================================================
# F3 — kalite kapısı (birim)
# ===========================================================================

class TestF3QualityGateUnit:
    def test_all_segments_failed_rejected(self):
        ok, reason = tb._translation_body_usable(
            _FakeStats(total=40, translated=0, failed=40))
        assert ok is False
        assert "hiçbiri" in reason

    def test_majority_failed_rejected(self):
        ok, reason = tb._translation_body_usable(
            _FakeStats(total=100, translated=40, failed=60))
        assert ok is False
        assert "60/100" in reason

    def test_exactly_half_failed_accepted(self):
        # translator.py koşulu '> 0.5' — tam yarı REDDEDİLMEZ, aynı sınır.
        ok, reason = tb._translation_body_usable(
            _FakeStats(total=100, translated=50, failed=50))
        assert ok is True and reason == ""

    def test_healthy_accepted(self):
        ok, _ = tb._translation_body_usable(
            _FakeStats(total=100, translated=98, failed=2))
        assert ok is True

    def test_no_segments_is_neutral(self):
        # Sayaç yoksa karar verilemez; eski davranış korunur.
        ok, _ = tb._translation_body_usable(_FakeStats())
        assert ok is True

    def test_threshold_matches_translator_guard(self):
        """Eşik translator.py'deki FINAL checkpoint reddiyle AYNI olmalı.

        Eşik artık config/defaults.py'de tek kaynak: hem translate_book hem
        latex_pipeline.translator oradan import eder. Bekçi, üç modülün de
        aynı nesneyi gördüğünü ve guard'ın sabiti kullandığını doğrular.
        """
        from config.defaults import TRANSLATION_MAX_FAIL_RATIO as cfg_ratio
        import latex_pipeline.translator as lp_translator

        assert tb.TRANSLATION_MAX_FAIL_RATIO == cfg_ratio
        assert lp_translator.TRANSLATION_MAX_FAIL_RATIO == cfg_ratio

        src = TRANSLATOR_PY.read_text(encoding="utf-8")
        assert re.search(r"_fail_ratio\s*>\s*TRANSLATION_MAX_FAIL_RATIO", src), (
            "translator.py guard'ı config sabitini kullanmıyor — "
            "literal eşiğe geri dönülmüş (F3)"
        )
        assert "segments_translated == 0" in src, (
            "translator.py'deki 'hiç çevrilmedi' kolu kaldırılmış")


# ===========================================================================
# F3 — kalite kapısı (pipeline)
# ===========================================================================

class TestF3PipelineGate:
    def test_failed_body_not_written_as_resume_source(
            self, book_dir, pipeline_env, restore_root_logger):
        pipeline_env.stats_spec = (10, 1, 9)     # %90 fail
        rc = _run(book_dir)

        assert rc == 1, "başarısız çeviri sıfır dışı kod döndürmeli"
        assert not (book_dir / "source" / "translated_body.tex").exists(), (
            "%90 başarısız gövde translated_body.tex olarak yazıldı — "
            "bir sonraki --resume onu 'çeviri' sanır (F3)")
        failed = book_dir / "source" / "translated_body.FAILED.tex"
        assert failed.exists() and failed.stat().st_size > 0, (
            "teşhis kopyası (translated_body.FAILED.tex) yazılmadı")
        assert "KALİTE KAPISI REDDETTİ" in _log(book_dir)

    def test_zero_translated_rejected_in_pipeline(
            self, book_dir, pipeline_env, restore_root_logger):
        # Model hiç yüklenemedi senaryosu: tüm segmentler MT_FAIL.
        pipeline_env.stats_spec = (25, 0, 25)
        rc = _run(book_dir)
        assert rc == 1
        assert not (book_dir / "source" / "translated_body.tex").exists()

    def test_resume_does_not_pick_up_failed_body(
            self, book_dir, pipeline_env, restore_root_logger):
        """Başarısız koşudan sonra --resume Phase 5.5'e ATLAMAMALI."""
        pipeline_env.stats_spec = (10, 1, 9)
        assert _run(book_dir) == 1

        # İkinci koşu: --resume. FAILED dosyası resume kaynağı OLMAMALI.
        rc2 = _run(book_dir, resume=True, skip_translate=True)
        log = _log(book_dir)
        assert "RESUME: translated_body.tex found" not in log, (
            "resume başarısız gövdeyi çeviri kaynağı olarak devraldı (F3)")
        assert "RESUME: body.tex found" in log, (
            "resume İngilizce body.tex'ten devam etmeliydi")
        assert rc2 == 0

    def test_healthy_body_written_and_stale_failed_cleared(
            self, book_dir, pipeline_env, restore_root_logger):
        stale = book_dir / "source" / "translated_body.FAILED.tex"
        stale.write_text("onceki kosunun bozuk govdesi", encoding="utf-8")

        pipeline_env.stats_spec = (10, 9, 1)     # %10 fail -> kabul
        pipeline_env.body_transform = staticmethod(
            lambda body: body + "\n% cevrildi\n")
        rc = _run(book_dir)

        assert rc == 0
        tr = book_dir / "source" / "translated_body.tex"
        assert tr.exists() and "cevrildi" in tr.read_text(encoding="utf-8")
        assert not stale.exists(), (
            "başarılı koşudan sonra bayat FAILED dosyası kaldı — teşhis "
            "yanıltıcı olur")

    def test_checkpoint_resume_body_still_written(
            self, book_dir, pipeline_env, restore_root_logger):
        """Translator checkpoint'ten dönerse sayaçlar sıfırdır — kapı bunu
        'başarısız' saymamalı (o gövde yazılırken zaten kapıdan geçmişti)."""
        pipeline_env.stats_spec = (0, 0, 0)
        assert _run(book_dir, resume=True) == 0
        assert (book_dir / "source" / "translated_body.tex").exists()

    def test_resume_warns_when_failed_body_is_newer(
            self, book_dir, pipeline_env, restore_root_logger):
        """Son koşu reddedildiyse resume'un devraldığı gövde ESKİ koşununkidir.

        Sessiz kalırsa kullanıcı, düzeltmeye çalıştığı çeviriyle devam
        ettiğini sanır.
        """
        import os

        src = book_dir / "source"
        tr = src / "translated_body.tex"
        failed = src / "translated_body.FAILED.tex"
        tr.write_text("\\chapter{Eski}\n" + "Önceki koşunun gövdesi. " * 10,
                      encoding="utf-8")
        failed.write_text("\\chapter{Yeni}\n" + "Reddedilen gövde. " * 10,
                          encoding="utf-8")
        os.utime(tr, (1_700_000_000, 1_700_000_000))
        os.utime(failed, (1_700_000_100, 1_700_000_100))

        assert _run(book_dir, resume=True, skip_translate=True) == 0
        log = _log(book_dir)
        assert "DİKKAT" in log and "translated_body.FAILED.tex" in log, (
            "FAILED dosyası daha yeniyken resume uyarı vermedi")


# ===========================================================================
# F11 — Phase 4.5 ölü işi kaldırıldı
# ===========================================================================

class TestF11DeadAutoTerms:
    def test_pipeline_never_loads_dictionary(
            self, book_dir, pipeline_env, restore_root_logger, monkeypatch):
        """Yol B koşusu 122 MB sözlüğü yüklememeli: sonucu hiçbir çıktıyı
        etkilemeyen ölü işti. G2c: TerminologyEngine modülü silindi (geri
        gelemez); taze koşuda DictionaryLoader da hiç yüklenmez."""
        import importlib.util
        loads = []
        monkeypatch.setattr(
            "core.dictionary_loader.DictionaryLoader._load",
            lambda self: loads.append(self.dictionary_path))
        assert _run(book_dir) == 0
        assert importlib.util.find_spec(
            "latex_pipeline.terminology_engine") is None
        assert loads == [], (
            f"Phase 4.5 geri gelmiş: sözlük yüklendi ({loads}) (F11)")

    def test_translator_built_without_extra_glossary_terms(
            self, book_dir, pipeline_env, restore_root_logger):
        assert _run(book_dir) == 0
        assert pipeline_env.instances, "LatexTranslator hiç kurulmadı"
        for kwargs in pipeline_env.instances:
            assert "extra_glossary_terms" not in kwargs, (
                "ölü extra_glossary_terms parametresi hâlâ besleniyor (F11)")

    def test_dictionary_loader_not_touched_on_resume_path(
            self, book_dir, pipeline_env, restore_root_logger, monkeypatch):
        loads = []
        monkeypatch.setattr(
            "core.dictionary_loader.DictionaryLoader._load",
            lambda self: loads.append(self.dictionary_path))
        (book_dir / "source" / "translated_body.tex").write_text(
            "\\chapter{Bolum}\n" + "Turkce govde metni burada. " * 20,
            encoding="utf-8")
        _run(book_dir, resume=True, skip_translate=True)
        assert loads == [], f"sözlük yüklendi: {loads}"

    def test_extra_glossary_terms_do_not_change_output(self, tmp_path):
        """F11 kanıtının doğrulaması: extra_glossary_terms çıktıyı etkilemiyor.

        Bu test kırmızıya dönerse parametre yeniden CANLI demektir; Phase 4.5
        kaldırma gerekçesi (ölü iş) gözden geçirilmeli.
        """
        from unittest.mock import MagicMock

        from latex_pipeline.config import PipelineConfig
        from latex_pipeline.translator import LatexTranslator

        def _batch(texts, max_length=512):
            out = []
            for t in texts:
                parts = re.split(r"(<<(?:LATEX|CMD|TERM)_\d+>>)", t)
                out.append("".join(
                    p if re.fullmatch(r"<<(?:LATEX|CMD|TERM)_\d+>>", p)
                    else f"[TR]{p}[/TR]"
                    for p in parts))
            return out

        def _make(extra):
            lt = LatexTranslator(
                pipeline_config=PipelineConfig(translate=True,
                                               segment_timeout=60),
                mt_engine="hymt-7b", source_lang="en", target_lang="tr",
                book_type="science", extra_glossary_terms=extra,
            )
            bt = MagicMock()
            bt._translate_batch_hymt = MagicMock(side_effect=_batch)
            bt.translate_batch_protected = MagicMock(side_effect=_batch)
            bt.translate_text = MagicMock(
                side_effect=lambda t: _batch([t])[0])
            lt._translator = bt
            return lt

        body = (
            "\\chapter{Homeostasis and Gradient Descent}\n\n"
            "The homeostasis hypothesis and gradient descent are discussed "
            "here in a long enough paragraph to be segmented properly.\n\n"
            "$E = mc^2$ stays protected while homeostasis repeats again.\n"
        )
        plain = _make(None).translate(body, checkpoint_dir=str(tmp_path / "a"))
        extra = _make({"homeostasis": "ZZZ_OTOMATIK_TERIM"}).translate(
            body, checkpoint_dir=str(tmp_path / "b"))

        assert "[TR]" in plain, "sahte MT hiç çalışmadı, test anlamsız"
        assert plain == extra, (
            "extra_glossary_terms çıktıyı DEĞİŞTİRİYOR — F11'in 'ölü iş' "
            "gerekçesi artık geçerli değil")
        assert "ZZZ_OTOMATIK_TERIM" not in extra


# ===========================================================================
# F14 — kodlama güvenliği
# ===========================================================================

class TestF14Encoding:
    def test_bom_is_stripped(self, tmp_path):
        p = tmp_path / "EXPORT_TR.tex"
        p.write_bytes("\ufeff\\chapter{Bölüm}\nTürkçe içerik.\n"
                      .encode("utf-8"))
        text = tb._read_translated_body(p)
        assert not text.startswith("\ufeff")
        assert text.startswith("\\chapter{Bölüm}")

    def test_latin1_file_raises_instead_of_silent_corruption(self, tmp_path):
        p = tmp_path / "EXPORT_TR.tex"
        p.write_bytes("Türkçe içerik çok önemli.\n".encode("latin-1"))
        with pytest.raises(UnicodeDecodeError):
            tb._read_translated_body(p)

    def test_hint_is_turkish_and_actionable(self, tmp_path):
        p = tmp_path / "EXPORT_TR.tex"
        p.write_bytes("T\xfcrk\xe7e".encode("latin-1"))
        try:
            tb._read_translated_body(p)
        except UnicodeDecodeError as exc:
            hint = tb._encoding_error_hint(p, exc)
        assert "UTF-8" in hint and "iconv" in hint
        assert str(p) in hint

    def test_pipeline_returns_error_code_not_traceback(
            self, book_dir, pipeline_env, restore_root_logger):
        """2_derle sözleşmesi: run_pipeline exception DEĞİL, kod döndürmeli.

        Aksi hâlde 2_derle'nin --json satırı hiç basılmıyordu (F14).
        """
        (book_dir / "source" / "translated_body.tex").write_bytes(
            ("Türkçe içerik. " * 40).encode("latin-1"))
        rc = _run(book_dir, resume=True, skip_translate=True)
        assert rc == 1
        assert "KODLAMA HATASI" in _log(book_dir)

    def test_bom_does_not_enter_body_or_book_tr(
            self, book_dir, pipeline_env, restore_root_logger, monkeypatch):
        """BOM gövdeye HİÇ girmemeli (sanitizer'ın temizlemesine güvenilmez).

        Sanitizer görünmez karakterleri süpürüyor, ama BOM'lu gövde ondan
        önce QC/başlık eşleştirmesine ham hâliyle giriyor.
        """
        from latex_pipeline.template_engine import TemplateEngine

        seen: list[str] = []
        _orig = TemplateEngine.wrap_content

        def _spy(self, content, *args, **kwargs):
            seen.append(content)
            return _orig(self, content, *args, **kwargs)

        monkeypatch.setattr(TemplateEngine, "wrap_content", _spy)

        body = "\\chapter{Bölüm}\n" + "Türkçe gövde metni burada. " * 20
        (book_dir / "source" / "translated_body.tex").write_bytes(
            ("\ufeff" + body).encode("utf-8"))
        rc = _run(book_dir, resume=True, skip_translate=True)

        assert rc == 0
        assert seen, "TemplateEngine.wrap_content hiç çağrılmadı"
        assert all("\ufeff" not in c for c in seen), (
            "BOM çevrilmiş gövdeye sızdı (F14): utf-8-sig ile okunmuyor")
        book_tr = (book_dir / "source" / "book_tr.tex").read_text(
            encoding="utf-8")
        assert "\ufeff" not in book_tr, "BOM gövdeden book_tr.tex'e sızdı (F14)"

    def test_import_tex_validates_encoding_before_pipeline(self):
        """--import-tex kapısı kodlamayı extraction'dan ÖNCE doğrulamalı."""
        src = (PROJECT_ROOT / "translate_book.py").read_text(encoding="utf-8")
        block = src.split("--import-tex dosyasi cok kisa")[1][:900]
        assert "_read_translated_body" in block
        assert "_encoding_error_hint" in block
