"""FIX-AV 2026-07-29 — translate_book / 2_derle çalışma-zamanı bulguları.

Kapsanan bulgular:
    F33 — ``--export-tex`` META/sidecar'a ``book_type`` yazmıyordu ve mevcut
          sidecar'ı komple ezerek 1_cikar'ın yazdığı türü siliyordu; 2_derle
          türü bulamayıp 'science' profiline düşüyordu.
    F34 — Derleme hatasında kullanıcı ``compiled/*.log``'a yönlendiriliyordu;
          gerçek XeLaTeX log'u ``source/book_tr.log``.
    F36 — Değişmemiş gövde art arda 4+ kez tam QC analizinden geçiyor, manuel
          modda dış döngü faydasız bir 2. iterasyon koşuyordu.
    F37 — Yol A'da (2_derle) EPUB kaynaklı kitapta Türkçe EPUB çıktısı hiç
          üretilmiyordu (``--import-tex`` eşdeğeri üretiyordu).
    F47 — ``translated_body.tex`` mevcutken ``--force-mt`` sessiz no-op'tu:
          MT koşmuyor, marker kalkmıyor, hiçbir uyarı basılmıyordu.

Testler MT modeli / XeLaTeX GEREKTİRMEZ: çevirmen sahte sınıfla, derleme ve
QC motoru sayaç/sahte nesnelerle değiştirilir.
"""

from __future__ import annotations

import importlib
import json
import logging
import sys
from pathlib import Path

import pytest

import translate_book as tb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COMPILER_PY = PROJECT_ROOT / "latex_pipeline" / "compiler.py"

derle = importlib.import_module("2_derle")


# ===========================================================================
# Ortak yardımcılar
# ===========================================================================

class _FakeStats:
    def __init__(self, total=10, translated=10, failed=0, skipped=0):
        self.segments_total = total
        self.segments_translated = translated
        self.segments_failed = failed
        self.segments_skipped = skipped


class _FakeTranslator:
    """LatexTranslator yerine geçen, model yüklemeyen sahte çevirmen."""

    calls: list = []
    # >100 bayt: resume/yedekleme kapıları küçük dosyaları yok sayar
    output = "\\chapter{Motorlar}\nMAKINE CEVIRISI\n" + "makine " * 40

    def __init__(self, **kwargs):
        type(self).calls.append(kwargs)
        self.stats = _FakeStats()

    def translate(self, latex_body, checkpoint_dir=None, resume=False,
                  progress_callback=None):
        return type(self).output


class _CountingQCEngine:
    """QCEngine yerine geçen sayaç: analiz sayısını ve gövdeleri kaydeder."""

    bodies: list = []

    def __init__(self, *args, **kwargs):
        pass

    def analyze(self, translated_body, source_body=None,
                manual_translation=False):
        type(self).bodies.append(translated_body)
        return _FakeQCReport()


class _FakeQCReport:
    score = 10.0
    issues: list = []
    critical_count = high_count = medium_count = low_count = 0

    def print_summary(self):
        print("sahte QC raporu")


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


_EN = ("This chapter explains the behaviour of water pumps under load. "
       "The inlet chamber pressure rises quickly during start-up. ")
_TR = ("Bu bölüm su pompalarının yük altındaki davranışını açıklar. "
       "Giriş odası basıncı çalıştırmada hızla yükselir. ")


def _make_book(tmp_path: Path, tr_body: str | None = None,
               en_body: str | None = None) -> Path:
    """Minimum çalışır çıktı dizini (full_book.md zorunlu, A-10)."""
    out = tmp_path / "kitap"
    (out / "source").mkdir(parents=True)
    (out / "full_book.md").write_text("# Engines\n\n" + _EN * 30,
                                      encoding="utf-8")
    (out / "source" / "body.tex").write_text(
        en_body or ("\\chapter{Engines}\n" + _EN * 30), encoding="utf-8")
    if tr_body is not None:
        (out / "source" / "translated_body.tex").write_text(
            tr_body, encoding="utf-8")
    return out


def _log(out: Path) -> str:
    return (out / "pipeline.log").read_text(encoding="utf-8")


# ===========================================================================
# F33 — --export-tex book_type + sidecar birleştirme
# ===========================================================================

class TestF33ExportTexBookType:
    @pytest.fixture
    def export_env(self, tmp_path, monkeypatch, restore_root_logger):
        """main() --export-tex akışı: çıkarım/pipeline sahte, disk gerçek."""
        book = tmp_path / "A Great Fantasy Novel.pdf"
        book.write_bytes(b"%PDF-1.4\n" + b"x" * 2048)
        out = tmp_path / "out"
        out.mkdir()

        def _fake_pipeline(output_dir, book_name, **kwargs):
            src = Path(output_dir) / "source"
            src.mkdir(parents=True, exist_ok=True)
            (src / "body.tex").write_text(
                "\\chapter{One}\n" + "content " * 40, encoding="utf-8")
            return 0

        monkeypatch.setattr(tb, "run_pipeline", _fake_pipeline)
        monkeypatch.setattr(tb, "detect_pages", lambda p: 12)
        monkeypatch.setattr(tb, "run_extraction", lambda *a, **k: True)
        return book, out

    def _run_export(self, book: Path, out: Path, monkeypatch, extra=()):
        argv = ["translate_book.py", str(book), "--export-tex",
                "--skip-extract", "--output-dir", str(out), *extra]
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(SystemExit) as exc:
            tb.main()
        assert exc.value.code in (0, 3)

    def test_meta_header_and_sidecar_carry_book_type(self, export_env,
                                                     monkeypatch):
        """F33: EXPORT.tex başlığı ve kitap.json book_type taşımalı."""
        from yol_a_ortak import parse_meta_header, read_sidecar

        book, out = export_env
        self._run_export(book, out, monkeypatch)

        meta = parse_meta_header(out / "EXPORT.tex")
        assert meta.get("book_type") == "novel", (
            "başlıkta book_type yok/yanlış — 2_derle 'science'a düşer (F33)")
        assert read_sidecar(out).get("book_type") == "novel"

    def test_book_type_flag_wins(self, export_env, monkeypatch):
        book, out = export_env
        self._run_export(book, out, monkeypatch,
                         extra=["--book-type", "philosophy"])
        from yol_a_ortak import read_sidecar
        assert read_sidecar(out).get("book_type") == "philosophy"

    def test_existing_sidecar_fields_survive(self, export_env, monkeypatch):
        """F33: write_sidecar sidecar'ı komple eziyordu — merge edilmeli."""
        from yol_a_ortak import read_sidecar, write_sidecar

        book, out = export_env
        write_sidecar(out, {"book_type": "philosophy", "ozel_alan": "korunmali",
                            "slug": "eski_slug"})
        self._run_export(book, out, monkeypatch)

        side = read_sidecar(out)
        assert side.get("ozel_alan") == "korunmali", (
            "mevcut sidecar alanları siliniyor (F33)")
        # Bu koşunun alanları üzerine yazılır (slug güncel kitabınki olur)
        assert side.get("slug") != "eski_slug"

    def test_matches_1_cikar_detection(self, export_env, monkeypatch):
        """--export-tex ile 1_cikar aynı tespiti yazmalı (eşdeğerlik)."""
        from yol_a_ortak import read_sidecar

        book, out = export_env
        self._run_export(book, out, monkeypatch)
        assert read_sidecar(out).get("book_type") == tb.detect_book_type(
            str(book))

    def test_2derle_resolves_book_type_from_export(self, export_env,
                                                   monkeypatch):
        """2_derle'nin okuma yolu (META > sidecar) artık dolu dönmeli."""
        from yol_a_ortak import parse_meta_header, read_sidecar

        book, out = export_env
        self._run_export(book, out, monkeypatch)

        header_meta = parse_meta_header(out / "EXPORT.tex")
        sidecar = read_sidecar(out)
        book_type = (header_meta.get("book_type")
                     or sidecar.get("book_type") or None)
        assert book_type == "novel"


# ===========================================================================
# F34 — derleme hatasında gerçek log yolu
# ===========================================================================

class TestF34CompileLogPath:
    def test_log_path_is_source_book_tr_log(self, tmp_path):
        out = tmp_path / "kitap"
        assert derle._compile_log_path(out) == out / "source" / "book_tr.log"

    def test_compiler_still_writes_log_next_to_tex(self):
        """Bekçi: compiler -output-directory'yi .tex'in dizinine veriyor.

        Bu değişirse (ör. compiled/'a çıktı verilirse) 2_derle'nin gösterdiği
        yol yine yanlış olur — o zaman burası kırmızıya döner.
        """
        src = COMPILER_PY.read_text(encoding="utf-8")
        assert "-output-directory={tex_abs.parent}" in src

    def test_log_error_lines_extracts_and_limits(self, tmp_path):
        log = tmp_path / "book_tr.log"
        log.write_text(
            "This is package foo\n"
            "./book_tr.tex:42: Undefined control sequence.\n"
            "! Undefined control sequence.\n"
            "l.42 \\begin{tablo}\n"
            "! Undefined control sequence.\n"      # tekrar -> tekilleştirilir
            "! LaTeX Error: Environment tablo undefined.\n"
            "! Missing $ inserted.\n"
            "! Extra alignment tab.\n"
            "! Too many }'s.\n",
            encoding="utf-8")
        lines = derle._log_error_lines(log, limit=5)
        assert len(lines) == 5
        assert lines[0].startswith("./book_tr.tex:42:")
        assert lines.count("! Undefined control sequence.") == 1

    def test_missing_log_returns_empty(self, tmp_path):
        assert derle._log_error_lines(tmp_path / "yok.log") == []


class TestF34DerleMessage:
    @pytest.fixture
    def derle_book(self, tmp_path, monkeypatch):
        out = tmp_path / "output" / "kitap"
        (out / "source").mkdir(parents=True)
        (out / "source" / "body.tex").write_text("x" * 200, encoding="utf-8")
        (out / "full_book.md").write_text("# kitap\n", encoding="utf-8")
        tex = tmp_path / "EXPORT_TR.tex"
        tex.write_text("\\chapter{Deneme}\n" + "icerik " * 50,
                       encoding="utf-8")
        return out, tex

    def _run(self, monkeypatch, out, tex, exit_code, extra=()):
        monkeypatch.setattr(derle, "run_pipeline", lambda *a, **k: exit_code)
        monkeypatch.setattr(sys, "argv",
                            ["2_derle.py", str(tex), "--output-dir", str(out),
                             *extra])
        return derle.main()

    def test_points_to_real_log_with_error_lines(self, derle_book, monkeypatch,
                                                 capsys):
        out, tex = derle_book
        (out / "source" / "book_tr.log").write_text(
            "! LaTeX Error: Environment tablo undefined.\n"
            "l.12 \\begin{tablo}\n", encoding="utf-8")

        code = self._run(monkeypatch, out, tex, 2)
        captured = capsys.readouterr().out

        assert code == 2
        assert str(out / "source" / "book_tr.log") in captured
        assert "! LaTeX Error: Environment tablo undefined." in captured
        assert "compiled/*.log" not in captured, (
            "boş/bayat compiled dizinine yönlendirme sürüyor (F34)")

    def test_missing_log_says_so(self, derle_book, monkeypatch, capsys):
        out, tex = derle_book
        code = self._run(monkeypatch, out, tex, 2)
        captured = capsys.readouterr().out
        assert code == 2
        assert "Log bulunamadı" in captured
        assert "compiled/*.log" not in captured


# ===========================================================================
# F36 — QC memoizasyonu + faydasız dış iterasyon
# ===========================================================================

class TestF36QCMemoization:
    def test_same_body_analyzed_once(self):
        _CountingQCEngine.bodies = []
        eng = _CountingQCEngine()
        cache: dict = {}
        a = tb._qc_analyze(eng, "GOVDE", "KAYNAK", True, cache=cache)
        b = tb._qc_analyze(eng, "GOVDE", "KAYNAK", True, cache=cache)
        assert len(_CountingQCEngine.bodies) == 1
        assert a is b

    def test_changed_body_reanalyzed(self):
        _CountingQCEngine.bodies = []
        eng = _CountingQCEngine()
        cache: dict = {}
        tb._qc_analyze(eng, "GOVDE", "KAYNAK", True, cache=cache)
        tb._qc_analyze(eng, "GOVDE 2", "KAYNAK", True, cache=cache)
        assert len(_CountingQCEngine.bodies) == 2

    def test_manual_flag_is_part_of_key(self):
        """Yol A/Yol B raporları ASLA birbirinin yerine kullanılmamalı."""
        _CountingQCEngine.bodies = []
        eng = _CountingQCEngine()
        cache: dict = {}
        tb._qc_analyze(eng, "GOVDE", "KAYNAK", True, cache=cache)
        tb._qc_analyze(eng, "GOVDE", "KAYNAK", False, cache=cache)
        assert len(_CountingQCEngine.bodies) == 2

    def test_source_body_is_part_of_key(self):
        _CountingQCEngine.bodies = []
        eng = _CountingQCEngine()
        cache: dict = {}
        tb._qc_analyze(eng, "GOVDE", "KAYNAK", True, cache=cache)
        tb._qc_analyze(eng, "GOVDE", "BASKA KAYNAK", True, cache=cache)
        assert len(_CountingQCEngine.bodies) == 2

    def test_no_cache_means_no_memoization(self):
        """cache verilmezse eski davranış (her çağrı analiz) korunur."""
        _CountingQCEngine.bodies = []
        eng = _CountingQCEngine()
        tb._qc_analyze(eng, "GOVDE", "KAYNAK", True)
        tb._qc_analyze(eng, "GOVDE", "KAYNAK", True)
        assert len(_CountingQCEngine.bodies) == 2

    def test_cache_is_bounded(self):
        eng = _CountingQCEngine()
        cache: dict = {}
        for i in range(tb._QC_CACHE_MAX * 3):
            tb._qc_analyze(eng, f"GOVDE {i}", "KAYNAK", True, cache=cache)
        assert len(cache) <= tb._QC_CACHE_MAX


class TestF36PipelineAnalysisCount:
    """Gerçek QCEngine ile uçtan uca analiz sayısı (MT yok, derleme yok)."""

    @pytest.fixture
    def counting_qc(self, monkeypatch):
        from latex_pipeline.qc_engine import QCEngine

        calls = []
        orig = QCEngine.analyze

        def _counting(self, translated_body, source_body=None, **kw):
            calls.append(len(translated_body))
            return orig(self, translated_body, source_body, **kw)

        monkeypatch.setattr(QCEngine, "analyze", _counting)
        monkeypatch.setattr(tb, "_preflight_check", lambda *a, **k: [])
        return calls

    def test_manual_run_analysis_count_capped(self, tmp_path, counting_qc,
                                              restore_root_logger):
        """F36 öncesi bu senaryoda 8 tam analiz vardı; artık 3."""
        tr = ("\\chapter{Motorlar}\n" + _TR * 15 + _EN * 15
              + "\n\n\\section{Ateşleme}\n" + _TR * 15 + _EN * 15 + "\n")
        en = ("\\chapter{Engines}\n" + _EN * 30
              + "\n\n\\section{Ignition}\n" + _EN * 30 + "\n")
        out = _make_book(tmp_path, tr_body=tr, en_body=en)

        code = tb.run_pipeline(
            str(out), "Test Kitap",
            skip_translate=True, skip_compile=True, skip_qc=False,
            book_type="science", pdf_path=None, resume=True,
            manual_translation=True,
        )
        assert code == 3            # QC eşiği altı: davranış DEĞİŞMEDİ
        assert (out / "qc_report.txt").exists()
        assert len(counting_qc) <= 4, (
            f"tam QC analizi sayısı {len(counting_qc)} — memoizasyon "
            f"kırılmış (F36; fix öncesi 8, sonrası 3)")

    def test_manual_loop_stops_when_body_unchanged(self, tmp_path, counting_qc,
                                                   restore_root_logger):
        """Gövde değişmiyorsa 2. dış iterasyon hiç koşmamalı (F36)."""
        # Temiz Türkçe gövde ama kaynakta 3 bölüm var -> skor eşiğin altında,
        # düzeltilecek bir şey yok: sanitize+autofix gövdeyi değiştirmez.
        tr = "\\chapter{Motorlar}\n" + _TR * 25 + "\n"
        en = ("\\chapter{Engines}\n" + _EN * 20
              + "\n\\chapter{Nozzles}\n" + _EN * 20
              + "\n\\chapter{Pumps}\n" + _EN * 20 + "\n")
        out = _make_book(tmp_path, tr_body=tr, en_body=en)

        code = tb.run_pipeline(
            str(out), "Test Kitap",
            skip_translate=True, skip_compile=True, skip_qc=False,
            book_type="science", pdf_path=None, resume=True,
            manual_translation=True,
        )
        log = _log(out)
        assert code in (0, 3)
        if "Outer iteration 1/3: score" in log:
            # skor eşiği geçmediyse döngü 2. tura girmemeli
            assert "Outer iteration 2/3" not in log, (
                "manuel modda gövde değişmediği hâlde 2. iterasyon koşuyor "
                "(F36)")

    def test_auto_mode_loop_behaviour_unchanged(self, tmp_path, counting_qc,
                                                restore_root_logger):
        """Erken çıkış YALNIZ manuel modda: Yol B döngüsü dokunulmadı."""
        tr = "\\chapter{Motorlar}\n" + _TR * 25 + "\n"
        en = ("\\chapter{Engines}\n" + _EN * 20
              + "\n\\chapter{Nozzles}\n" + _EN * 20 + "\n")
        out = _make_book(tmp_path, tr_body=tr, en_body=en)

        tb.run_pipeline(
            str(out), "Test Kitap",
            skip_translate=True, skip_compile=True, skip_qc=False,
            book_type="science", pdf_path=None, resume=True,
            manual_translation=False,
        )
        assert "Manuel mod: iterasyon" not in _log(out)


# ===========================================================================
# F37 — Yol A (2_derle) EPUB çıktısı
# ===========================================================================

class TestF37DerleEpubOutput:
    @pytest.fixture
    def epub_book(self, tmp_path, monkeypatch):
        out = tmp_path / "output" / "kitap"
        (out / "source").mkdir(parents=True)
        (out / "source" / "body.tex").write_text("x" * 200, encoding="utf-8")
        (out / "full_book.md").write_text("# kitap\n", encoding="utf-8")
        compiled = out / "compiled"
        compiled.mkdir()
        (compiled / "kitap_TR.pdf").write_bytes(b"%PDF " + b"x" * 4096)
        tex = tmp_path / "EXPORT_TR.tex"
        tex.write_text("\\chapter{Deneme}\n" + "icerik " * 50,
                       encoding="utf-8")

        calls = []

        def _fake_epub(original_epub, output_dir, book_name):
            calls.append({"original_epub": original_epub,
                          "output_dir": output_dir, "book_name": book_name})
            return str(Path(output_dir) / "compiled" / "kitap_TR.epub")

        import latex_pipeline.epub_output as epub_mod
        monkeypatch.setattr(epub_mod, "generate_epub_output", _fake_epub)
        return out, tex, calls

    def _run(self, monkeypatch, out, tex, exit_code=0, extra=()):
        monkeypatch.setattr(derle, "run_pipeline", lambda *a, **k: exit_code)
        monkeypatch.setattr(sys, "argv",
                            ["2_derle.py", str(tex), "--output-dir", str(out),
                             *extra])
        return derle.main()

    def test_epub_generated_for_epub_source(self, epub_book, monkeypatch,
                                            capsys):
        out, tex, calls = epub_book
        (out / "source_book.epub").write_bytes(b"PK" + b"x" * 2048)

        code = self._run(monkeypatch, out, tex, exit_code=0)
        captured = capsys.readouterr().out

        assert code == 0
        assert len(calls) == 1, "2_derle EPUB çıktısını hâlâ üretmiyor (F37)"
        assert calls[0]["original_epub"] == str(out / "source_book.epub")
        assert calls[0]["output_dir"] == str(out)
        assert "EPUB hazır" in captured

    def test_epub_also_on_exit3(self, epub_book, monkeypatch):
        """PDF exit 3'te de üretiliyor — translate_book ile aynı koşul."""
        out, tex, calls = epub_book
        (out / "source_book.epub").write_bytes(b"PK" + b"x" * 2048)
        self._run(monkeypatch, out, tex, exit_code=3)
        assert len(calls) == 1

    def test_pdf_book_untouched(self, epub_book, monkeypatch):
        out, tex, calls = epub_book
        (out / "source_book.pdf").write_bytes(b"%PDF " + b"x" * 2048)
        code = self._run(monkeypatch, out, tex, exit_code=0)
        assert code == 0
        assert calls == [], "PDF kaynaklı kitapta EPUB üretimi denenmemeli"

    def test_no_epub_output_flag(self, epub_book, monkeypatch):
        out, tex, calls = epub_book
        (out / "source_book.epub").write_bytes(b"PK" + b"x" * 2048)
        self._run(monkeypatch, out, tex, exit_code=0,
                  extra=["--no-epub-output"])
        assert calls == []

    def test_not_generated_on_compile_failure(self, epub_book, monkeypatch):
        out, tex, calls = epub_book
        (out / "source_book.epub").write_bytes(b"PK" + b"x" * 2048)
        code = self._run(monkeypatch, out, tex, exit_code=2)
        assert code == 2
        assert calls == []

    def test_json_contract_has_epub_key(self, epub_book, monkeypatch, capsys):
        out, tex, calls = epub_book
        (out / "source_book.epub").write_bytes(b"PK" + b"x" * 2048)
        self._run(monkeypatch, out, tex, exit_code=0, extra=["--json"])
        last = capsys.readouterr().out.strip().splitlines()[-1]
        payload = json.loads(last)
        assert payload["ok"] is True
        assert payload["epub"].endswith("_TR.epub")

    def test_epub_failure_does_not_break_run(self, epub_book, monkeypatch,
                                             capsys):
        out, tex, _ = epub_book
        (out / "source_book.epub").write_bytes(b"PK" + b"x" * 2048)

        def _boom(**kwargs):
            raise RuntimeError("epub patladi")

        import latex_pipeline.epub_output as epub_mod
        monkeypatch.setattr(epub_mod, "generate_epub_output", _boom)
        code = self._run(monkeypatch, out, tex, exit_code=0)
        assert code == 0                      # PDF sonucu düşmemeli
        assert "EPUB çıktı oluşturulamadı" in capsys.readouterr().out


# ===========================================================================
# F47 — --force-mt sessiz no-op
# ===========================================================================

class TestF47ForceMT:
    @pytest.fixture
    def mt_env(self, monkeypatch):
        _FakeTranslator.calls = []
        monkeypatch.setattr(tb, "_preflight_check", lambda *a, **k: [])
        monkeypatch.setattr("latex_pipeline.translator.LatexTranslator",
                            _FakeTranslator)
        return _FakeTranslator

    def _run(self, out: Path, **kwargs) -> int:
        defaults = dict(skip_translate=False, skip_compile=True, skip_qc=True,
                        book_type="science", pdf_path=None)
        defaults.update(kwargs)
        return tb.run_pipeline(str(out), "Test Kitap", **defaults)

    def test_force_mt_with_resume_runs_translation(self, tmp_path, mt_env,
                                                   restore_root_logger):
        """F47: --force-mt --resume sessizce atlıyordu; artık MT koşar."""
        out = _make_book(tmp_path, tr_body="AJAN CEVIRISI\n" + "x" * 200)
        marker = out / "source" / "MANUAL_TRANSLATION.marker"
        marker.write_text("import: test\n", encoding="utf-8")

        code = self._run(out, resume=True, force_mt=True)
        log = _log(out)
        body = (out / "source" / "translated_body.tex").read_text(
            encoding="utf-8")

        assert code == 0
        assert mt_env.calls, "MT çevirmeni hiç kurulmadı (F47 sessiz no-op)"
        assert "Phase 5: MT Translation" in log
        assert body == _FakeTranslator.output
        assert not marker.exists(), "Yol A kilidi (marker) kalkmadı"

    def test_existing_translation_backed_up_not_deleted(self, tmp_path, mt_env,
                                                        restore_root_logger):
        old = "AJAN CEVIRISI\n" + "x" * 200
        out = _make_book(tmp_path, tr_body=old)
        (out / "source" / "MANUAL_TRANSLATION.marker").write_text(
            "import: test\n", encoding="utf-8")

        self._run(out, resume=True, force_mt=True)

        bak = out / "source" / "translated_body.tex.bak"
        assert bak.exists(), "eski çeviri silinmiş — yedek yok (F47)"
        assert bak.read_text(encoding="utf-8") == old
        assert "yedeklendi" in _log(out)

    def test_second_force_run_keeps_first_backup(self, tmp_path, mt_env,
                                                 restore_root_logger):
        """İkinci --force-mt ilk yedeği EZMEMELİ."""
        out = _make_book(tmp_path, tr_body="ILK CEVIRI\n" + "x" * 200)
        self._run(out, resume=True, force_mt=True)
        bak = out / "source" / "translated_body.tex.bak"
        assert bak.read_text(encoding="utf-8").startswith("ILK CEVIRI")

        self._run(out, resume=True, force_mt=True)
        assert bak.read_text(encoding="utf-8").startswith("ILK CEVIRI")
        extra = list((out / "source").glob("translated_body.*.tex.bak"))
        assert extra, "ikinci yedek zaman damgalı dosyaya yazılmadı"

    def test_without_force_mt_resume_is_unchanged(self, tmp_path, mt_env,
                                                  restore_root_logger):
        """Sözleşme koruması: --force-mt YOKSA resume eskisi gibi davranır."""
        old = "AJAN CEVIRISI\n" + "x" * 200
        out = _make_book(tmp_path, tr_body=old)
        marker = out / "source" / "MANUAL_TRANSLATION.marker"
        marker.write_text("import: test\n", encoding="utf-8")

        code = self._run(out, resume=True, force_mt=False)

        assert code == 0
        assert mt_env.calls == [] or "Phase 5: MT Translation" not in _log(out)
        assert (out / "source" / "translated_body.tex").read_text(
            encoding="utf-8") == old
        assert not (out / "source" / "translated_body.tex.bak").exists()
        assert marker.exists()

    def test_force_mt_ignored_when_translation_skipped(self, tmp_path, mt_env,
                                                       restore_root_logger):
        """2_derle sözleşmesi: skip_translate ile MT koşmaz, gövde korunur."""
        old = "AJAN CEVIRISI\n" + "x" * 200
        out = _make_book(tmp_path, tr_body=old)

        code = self._run(out, resume=True, force_mt=True, skip_translate=True,
                         manual_translation=True)

        assert code == 0
        assert (out / "source" / "translated_body.tex").read_text(
            encoding="utf-8") == old
        assert not (out / "source" / "translated_body.tex.bak").exists()
        assert "--force-mt yok sayıldı" in _log(out)

    def test_marker_without_force_still_blocks_mt(self, tmp_path, mt_env,
                                                  restore_root_logger):
        """Yol A kilidi duruyor: marker + MT isteği -> exit 2."""
        out = _make_book(tmp_path)          # translated_body.tex YOK
        (out / "source" / "MANUAL_TRANSLATION.marker").write_text(
            "import: test\n", encoding="utf-8")

        code = self._run(out, resume=True, force_mt=False)
        assert code == 2
        assert mt_env.calls == []
