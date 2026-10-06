"""Komut satırı akış düzeltmeleri (dalga CLI) — 25 Eylül 2026.

Arayüz denetimi (U kalemleri) ve kulvarlar arası sözleşmeler (S1-S3, S6):

    CLI1 (U4, S3)  Yol A kilidi (çıkış 2) ÇIKARIMDAN ÖNCE denetlenir: reddedilen
                   yerel model koşusu full_book.md, epub_segment_mapping.json,
                   source/body.tex ve book.tex'i yeniden yazmaz; pipeline.log
                   EKLEME kipinde, koşu ayracıyla yazılır (eskiden "w" ile
                   siliniyordu). İleti doğru Türkçe, kart adı branding'den.
    CLI2 (U9, S1)  PDF çıkarım satırı toplam sayfayı taşır:
                   "Batch: pages a-b / toplam".
    CLI3 (U10, S2) 1_cikar --yeniden + JSON alanları (extraction_reused,
                   export_tr_exists, chapters) + Türkçe uyarı satırları.
    CLI4 (U3, S2)  Ad çakışması: Türkçe ileti + JSON error "name_conflict".
    CLI5 (U5, S6)  Kitaba özel CEVIRI_TALIMATI.md tam yolları taşır; kök
                   talimatta "arayüzden çıkarılmış kitap" durumu.
    CLI6 (U16/U18) META uyarı satırı, translate_book "Çıktı:" satırları,
                   pixel_diff sayfa sayısı farkı (sonuçta da duruyor).
    CLI7           Yalnız etiketten oluşan \\chapter başlığının çeviri biçimi
                   (2_derle'den geçen test).

Testler MT modeli / XeLaTeX / output/ içeriği GEREKTİRMEZ; kitap metinleri
bu dosya için yazılmış sentetik satırlardır.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import logging
import re
import sys
import zipfile
from pathlib import Path

import fitz
import pytest

import translate_book as tb
import yol_a_ortak as yo
from core.events import classify
from latex_pipeline.sanitizer import LatexSanitizer
from ui import branding

PROJECT_ROOT = Path(__file__).resolve().parent.parent
derle = importlib.import_module("2_derle")


def _load_cikar():
    spec = importlib.util.spec_from_file_location(
        "cikar_dalga_cli", PROJECT_ROOT / "1_cikar.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def _manifest(root: Path, skip: tuple[str, ...] = ()) -> dict[str, str]:
    return {str(p.relative_to(root)): _sha(p) for p in sorted(root.rglob("*"))
            if p.is_file() and p.name not in skip}


def _make_epub(path: Path, text: str = "Kurze Probe.") -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("OEBPS/ch0.xhtml",
                    f"<html><body><h1>1</h1><p>{text}</p></body></html>")
    return path


@pytest.fixture
def root_logger_guard():
    """run_pipeline kök günlüğü temizleyip kendi handler'larını ekler; geri al."""
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    yield
    for handler in list(root.handlers):
        if handler not in saved_handlers:
            root.removeHandler(handler)
            handler.close()
    for handler in saved_handlers:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(saved_level)


class _Recorder:
    def __init__(self):
        self.extraction: list = []
        self.pipeline: list = []
        self.pregate: list = []


@pytest.fixture
def flow(monkeypatch):
    """Çıkarım, pipeline ve dil ön kapısını sahteleriyle değiştir."""
    rec = _Recorder()

    def _extract(pdf_path, output_dir, batch_size, device):
        rec.extraction.append(pdf_path)
        return True

    def _extract_epub(epub_path, output_dir):
        rec.extraction.append(epub_path)
        return True

    def _pipeline(output_dir, book_name, **kwargs):
        rec.pipeline.append(kwargs)
        return 0

    def _pregate(*args, **kwargs):
        rec.pregate.append(args)
        return None

    monkeypatch.setattr(tb, "run_extraction", _extract)
    monkeypatch.setattr(tb, "run_epub_extraction", _extract_epub)
    monkeypatch.setattr(tb, "run_pipeline", _pipeline)
    monkeypatch.setattr(tb, "_source_language_pregate", _pregate)
    monkeypatch.delenv("KITAPCEVIR_EVENT_LOG", raising=False)
    yield rec
    assert not logging.getLogger("pipeline.lock").handlers, (
        "kilit günlüğü handler'larını bıraktı")


def _main(monkeypatch, *argv) -> int:
    monkeypatch.setattr(sys, "argv", ["translate_book.py", *map(str, argv)])
    with pytest.raises(SystemExit) as exc:
        tb.main()
    return exc.value.code


_OLD_LOG = "[2026-09-25 09:48:00] INFO    pipeline: ONCEKI DERLEME GUNLUGU\n" * 40


def _locked_book(tmp_path: Path) -> Path:
    """TeX yoluyla çevrilmiş (marker'lı) bir kitap klasörü."""
    out = tmp_path / "out"
    src = out / "source"
    src.mkdir(parents=True)
    (src / tb.MANUAL_MARKER_NAME).write_text("import: test\n", encoding="utf-8")
    (src / "translated_body.tex").write_text(
        "\\chapter{Giriş}\n" + "Türkçe çeviri metni. " * 20, encoding="utf-8")
    (src / "body.tex").write_text(
        "\\chapter{Einleitung}\n" + "Deutscher Text. " * 20, encoding="utf-8")
    (src / "book.tex").write_text("\\documentclass{book}\n" * 5,
                                  encoding="utf-8")
    (out / "full_book.md").write_text("# Einleitung\n\n" + "Text. " * 40,
                                      encoding="utf-8")
    (out / "epub_segment_mapping.json").write_text('{"segments": []}',
                                                   encoding="utf-8")
    (out / "pipeline.log").write_text(_OLD_LOG, encoding="utf-8")
    return out


# ===========================================================================
# CLI1 — Yol A kilidi çıkarımdan ÖNCE, pipeline.log ekleme kipinde
# ===========================================================================

class TestCli1LockBeforeExtraction:
    def test_lock_touches_no_book_file(self, tmp_path, monkeypatch, flow,
                                       capsys):
        out = _locked_book(tmp_path)
        epub = _make_epub(tmp_path / "buch.epub")
        before = _manifest(out, skip=("pipeline.log",))

        code = _main(monkeypatch, epub, "--output-dir", out)

        assert code == tb.EXIT_MANUAL_LOCK == 2
        assert flow.extraction == [], "kilitli koşuda çıkarım çağrıldı"
        assert flow.pipeline == [], "kilitli koşuda pipeline çağrıldı"
        assert flow.pregate == [], "kilidin iletisi dil ön kapısından önce gelir"
        assert _manifest(out, skip=("pipeline.log",)) == before
        # Kaynak kopyası da yapılmaz (klasöre yalnız pipeline.log eklenir).
        assert not (out / "source_book.epub").exists()
        log = (out / "pipeline.log").read_text(encoding="utf-8")
        assert log.startswith(_OLD_LOG), "önceki koşunun günlüğü silindi"
        tail = log[len(_OLD_LOG):]
        assert "Yeni koşu" in tail
        assert tb.yol_a_lock_message() in tail
        assert tb.yol_a_lock_message() in capsys.readouterr().out

    def test_message_is_turkish_and_reads_branding(self, monkeypatch):
        msg = tb.yol_a_lock_message()
        assert branding.TEX_TITLE in msg
        for ascii_word in ("cevirisi", "Secenekler", "iceriyor", "Yol A",
                           "ezecegi"):
            assert ascii_word not in msg, ascii_word
        assert "ezmemek için durdu" in msg
        monkeypatch.setattr(branding, "TEX_TITLE", "ÖRNEK KART")
        assert "'ÖRNEK KART'" in tb.yol_a_lock_message()
        hint = tb._yol_a_lock_hint()
        assert "--name" in hint and "--force-mt" in hint and "2_derle.py" in hint

    def test_lock_reaches_event_stream(self, tmp_path, monkeypatch, flow):
        events = tmp_path / "events.jsonl"
        out = _locked_book(tmp_path)
        epub = _make_epub(tmp_path / "buch.epub")
        monkeypatch.setenv("KITAPCEVIR_EVENT_LOG", str(events))
        assert _main(monkeypatch, epub, "--output-dir", out) == 2
        rows = [json.loads(line) for line in
                events.read_text(encoding="utf-8").splitlines() if line]
        errors = [r for r in rows if r.get("kind") == "error"]
        assert errors and errors[-1]["message"] == tb.yol_a_lock_message()

    @pytest.mark.parametrize("flags", [
        ("--force-mt",), ("--export-tex",), ("--extract-only",),
        ("--skip-translate",), ("--resume",),
    ], ids=["force_mt", "export_tex", "extract_only", "skip_translate",
            "resume_ile_mevcut_ceviri"])
    def test_runs_without_mt_are_not_locked(self, tmp_path, monkeypatch,
                                            flow, flags):
        out = _locked_book(tmp_path)
        epub = _make_epub(tmp_path / "buch.epub")
        assert _main(monkeypatch, epub, "--output-dir", out, *flags) == 0
        assert flow.extraction, "kilide takılmaması gereken koşu durdu"

    def test_import_tex_is_not_locked(self, tmp_path, monkeypatch, flow):
        out = _locked_book(tmp_path)
        epub = _make_epub(tmp_path / "buch.epub")
        tr = tmp_path / "EXPORT_TR.tex"
        tr.write_text("\\chapter{Giriş}\n" + "Türkçe metin. " * 20,
                      encoding="utf-8")
        assert _main(monkeypatch, epub, "--output-dir", out,
                     "--import-tex", tr) == 0

    def test_resume_without_translation_still_locks(self, tmp_path,
                                                    monkeypatch, flow):
        """--resume ama kullanılabilir translated_body.tex yok: MT koşacaktı."""
        out = _locked_book(tmp_path)
        (out / "source" / "translated_body.tex").write_text("kisa",
                                                            encoding="utf-8")
        epub = _make_epub(tmp_path / "buch.epub")
        assert _main(monkeypatch, epub, "--output-dir", out, "--resume") == 2
        assert flow.extraction == []

    def test_batch_marks_locked_book_failed(self, tmp_path, monkeypatch, flow,
                                            capsys):
        monkeypatch.setattr(tb, "PROJECT_ROOT", tmp_path)
        epub = _make_epub(tmp_path / "buch.epub")
        book = tmp_path / "output" / "buch"
        (book / "source").mkdir(parents=True)
        (book / "source" / tb.MANUAL_MARKER_NAME).write_text(
            "import: test\n", encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["translate_book.py", str(epub)])
        results = tb.run_batch([str(epub)], tb.parse_args())
        assert [(Path(f).name, err) for f, err, _ in results["failed"]] == [
            ("buch.epub", "exit=2")]
        assert flow.extraction == [] and flow.pipeline == []

    def test_pipeline_internal_lock_uses_same_message(
            self, tmp_path, monkeypatch, root_logger_guard):
        """run_pipeline'ı doğrudan çağıran için savunma hattı: aynı ileti."""
        out = _locked_book(tmp_path)
        # resume + body.tex, çeviri yok -> Phase 5'e (MT) gidilir, kilit durdurur.
        (out / "source" / "translated_body.tex").unlink()
        monkeypatch.setattr(tb, "_preflight_check", lambda *a, **k: [])
        code = tb.run_pipeline(str(out), "Kitap", skip_translate=False,
                               skip_compile=True, skip_qc=True,
                               book_type="science", pdf_path=None,
                               resume=True)
        assert code == tb.EXIT_MANUAL_LOCK
        log = (out / "pipeline.log").read_text(encoding="utf-8")
        assert log.startswith(_OLD_LOG)
        assert tb.yol_a_lock_message() in log
        assert "Phase 5: MT Translation" not in log
        assert "cevirisi iceriyor" not in log


class TestCli1PipelineLogAppend:
    def test_two_runs_keep_both_logs(self, tmp_path, monkeypatch,
                                     root_logger_guard):
        out = tmp_path / "kitap"
        (out / "source").mkdir(parents=True)
        (out / "full_book.md").write_text("# Kitap\n\n" + "Text. " * 40,
                                          encoding="utf-8")
        (out / "source" / "body.tex").write_text(
            "\\chapter{Engines}\n" + "Engines burn fuel. " * 20,
            encoding="utf-8")
        monkeypatch.setattr(tb, "_preflight_check", lambda *a, **k: [])
        kwargs = dict(skip_translate=True, skip_compile=True, skip_qc=True,
                      book_type="science", pdf_path=None, resume=True)
        assert tb.run_pipeline(str(out), "Kitap", **kwargs) == 0
        first = (out / "pipeline.log").read_text(encoding="utf-8")
        assert tb.run_pipeline(str(out), "Kitap", **kwargs) == 0
        both = (out / "pipeline.log").read_text(encoding="utf-8")
        assert both.startswith(first), "ikinci koşu ilk günlüğü sildi"
        assert both.count("Yeni koşu") == 2
        assert both.count("RESUME: body.tex found") == 2

    def test_separator_line_format(self, tmp_path):
        handler = tb._pipeline_log_handler(tmp_path)
        handler.close()
        text = (tmp_path / "pipeline.log").read_text(encoding="utf-8")
        assert re.fullmatch(
            r"=+ Yeni koşu · \d{4}-\d\d-\d\d \d\d:\d\d:\d\d · \S+ · pid \d+ =+\n",
            text), text

    def test_oversized_log_is_moved_aside(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tb, "PIPELINE_LOG_MAX_BYTES", 50)
        (tmp_path / "pipeline.log").write_text("x" * 100, encoding="utf-8")
        tb._pipeline_log_handler(tmp_path).close()
        assert (tmp_path / tb.PIPELINE_LOG_PREVIOUS_NAME).read_text(
            encoding="utf-8") == "x" * 100
        assert (tmp_path / "pipeline.log").read_text(
            encoding="utf-8").startswith("=")

    def test_pregate_stop_appends(self, tmp_path, monkeypatch):
        from core import lang_detect as ld

        (tmp_path / "pipeline.log").write_text(_OLD_LOG, encoding="utf-8")
        monkeypatch.setattr(tb, "_pdf_text_sample", lambda p: "metin")
        # K5 (2026-09-28, KARARLAR 14): ön kapı yalnız desteklenmeyen dilde
        # durdurur (Almanca artık desteklenir); tespit Portekizce.
        monkeypatch.setattr(ld, "analyze_language",
                            lambda *a, **k: ld.LanguageEstimate(
                                "pt", 0.99, letters=5000))
        monkeypatch.setattr(ld, "should_stop_before_extraction",
                            lambda est: True)
        monkeypatch.delenv("KITAPCEVIR_EVENT_LOG", raising=False)
        code = tb._source_language_pregate("kitap.pdf", "pdf", str(tmp_path))
        assert code == tb.EXIT_SOURCE_UNSUPPORTED
        log = (tmp_path / "pipeline.log").read_text(encoding="utf-8")
        assert log.startswith(_OLD_LOG)
        assert "Bu kitabın dili desteklenmiyor (tespit: Portekizce)" in log
        assert not logging.getLogger("pipeline.pregate").handlers


# ===========================================================================
# CLI2 — çıkarım satırında toplam sayfa (S1)
# ===========================================================================

class TestCli2BatchLineTotal:
    def test_batch_lines_carry_total(self, tmp_path, monkeypatch, capsys):
        import latex_pipeline.extractor as extractor_mod

        class _Extraction:
            markdown = "Metin satırı. " * 20
            images: list = []

        class _FakeExtractor:
            def __init__(self, config):
                pass

            def extract(self, pdf_path, page_range=None):
                return _Extraction()

        monkeypatch.setattr(extractor_mod, "MarkerExtractor", _FakeExtractor)
        monkeypatch.setattr(tb, "detect_pages", lambda p: 45)
        monkeypatch.setattr(tb.multiprocessing, "set_start_method",
                            lambda *a, **k: None)
        monkeypatch.setenv("TORCH_DEVICE", "cpu")
        assert tb.run_extraction(str(tmp_path / "kitap.pdf"),
                                 str(tmp_path / "out"), 20, "cpu") is True
        lines = [ln for ln in capsys.readouterr().out.splitlines()
                 if "Batch: pages" in ln]
        assert [ln.split("] ", 1)[1] for ln in lines] == [
            "Batch: pages 0-19 / 45 (20 pages)...",
            "Batch: pages 20-39 / 45 (20 pages)...",
            "Batch: pages 40-44 / 45 (5 pages)...",
        ]
        event = classify(lines[1])
        assert event == {"kind": "progress", "unit": "page", "current": 39,
                         "from": 20, "total": 45}

    def test_old_line_still_classified(self):
        event = classify("[14:28:26] Batch: pages 20-39 (20 pages)...")
        assert event["current"] == 39 and "total" not in event


# ===========================================================================
# CLI3 / CLI4 — 1_cikar: --yeniden, JSON alanları, ad çakışması (S2)
# ===========================================================================

class _CikarEnv:
    def __init__(self, module, root: Path):
        self.module = module
        self.root = root
        self.extractions = 0
        self.markdown = "# Kapitel\n\n" + "Erste Fassung. " * 30
        self.extract_ok = True

    @property
    def out(self) -> Path:
        return self.root / "output" / "buch"

    def run(self, monkeypatch, capsys, *argv) -> tuple[int, dict, str]:
        monkeypatch.setattr(sys, "argv", ["1_cikar.py", *map(str, argv),
                                          "--json"])
        code = self.module.main()
        text = capsys.readouterr().out
        payload = json.loads(text.strip().splitlines()[-1])
        return code, payload, text


@pytest.fixture
def cikar_env(tmp_path, monkeypatch):
    cikar = _load_cikar()
    env = _CikarEnv(cikar, tmp_path)
    monkeypatch.setattr(cikar, "PROJECT_ROOT", tmp_path)

    def _extract_epub(epub_path, output_dir):
        env.extractions += 1
        (Path(output_dir) / "full_book.md").write_text(env.markdown,
                                                       encoding="utf-8")
        return env.extract_ok

    def _pipeline(output_dir, book_name, **kwargs):
        assert kwargs["skip_translate"] is True
        md = (Path(output_dir) / "full_book.md").read_text(encoding="utf-8")
        src = Path(output_dir) / "source"
        src.mkdir(parents=True, exist_ok=True)
        (src / "body.tex").write_text(
            "\\chapter{Kapitel}\n\n" + md + "\n\n\\chapter{Zweites}\n\nText.\n",
            encoding="utf-8")
        return 0

    monkeypatch.setattr(cikar, "run_epub_extraction", _extract_epub)
    monkeypatch.setattr(cikar, "run_pipeline", _pipeline)
    return env


class TestCli3CikarRerunAndJson:
    def test_first_run_fields(self, tmp_path, monkeypatch, capsys, cikar_env):
        epub = _make_epub(tmp_path / "buch.epub")
        code, data, text = cikar_env.run(monkeypatch, capsys, epub)
        assert code == 0
        assert set(data) == set(cikar_env.module._JSON_FIELDS)
        assert data["ok"] is True and data["error"] is None
        assert data["extraction_reused"] is False
        assert data["export_tr_exists"] is False
        assert data["export_changed"] is None
        assert data["chapters"] == 2
        assert cikar_env.extractions == 1
        assert "UYARI" not in text

    def test_second_run_reuses_and_says_so(self, tmp_path, monkeypatch,
                                           capsys, cikar_env):
        epub = _make_epub(tmp_path / "buch.epub")
        cikar_env.run(monkeypatch, capsys, epub)
        code, data, text = cikar_env.run(monkeypatch, capsys, epub)
        assert code == 0
        assert cikar_env.extractions == 1, "çıkarım yeniden koştu"
        assert data["extraction_reused"] is True
        assert data["export_changed"] is False
        assert ("UYARI: Bu klasördeki önceki çıkarım (full_book.md) yeniden "
                "kullanıldı") in text
        assert "--yeniden" in text

    def test_yeniden_keeps_previous_and_reextracts(self, tmp_path,
                                                   monkeypatch, capsys,
                                                   cikar_env):
        epub = _make_epub(tmp_path / "buch.epub")
        cikar_env.run(monkeypatch, capsys, epub)
        old_md = (cikar_env.out / "full_book.md").read_text(encoding="utf-8")
        cikar_env.markdown = "# Kapitel\n\n" + "Zweite Fassung. " * 30
        code, data, text = cikar_env.run(monkeypatch, capsys, epub,
                                         "--yeniden")
        assert code == 0 and cikar_env.extractions == 2
        assert data["extraction_reused"] is False
        assert data["export_changed"] is True
        assert (cikar_env.out / "full_book.onceki.md").read_text(
            encoding="utf-8") == old_md
        assert "Zweite Fassung" in (cikar_env.out / "EXPORT.tex").read_text(
            encoding="utf-8")
        assert "full_book.onceki.md adıyla saklandı" in text

    def test_yeniden_failure_restores_previous(self, tmp_path, monkeypatch,
                                               capsys, cikar_env):
        epub = _make_epub(tmp_path / "buch.epub")
        cikar_env.run(monkeypatch, capsys, epub)
        old_md = (cikar_env.out / "full_book.md").read_text(encoding="utf-8")
        cikar_env.extract_ok = False
        code, data, text = cikar_env.run(monkeypatch, capsys, epub,
                                         "--yeniden")
        assert code == 1
        assert data["ok"] is False and data["error"] == "extraction_failed"
        assert set(data) == set(cikar_env.module._JSON_FIELDS)
        assert (cikar_env.out / "full_book.md").read_text(
            encoding="utf-8") == old_md
        assert not (cikar_env.out / "full_book.onceki.md").exists()
        assert "Önceki çıkarım geri yüklendi" in text

    def test_export_tr_present_warns_and_is_untouched(self, tmp_path,
                                                      monkeypatch, capsys,
                                                      cikar_env):
        epub = _make_epub(tmp_path / "buch.epub")
        cikar_env.run(monkeypatch, capsys, epub)
        tr = cikar_env.out / "EXPORT_TR.tex"
        tr.write_text("% çeviri\n" + "Türkçe. " * 30, encoding="utf-8")
        tr_sha = _sha(tr)

        code, data, text = cikar_env.run(monkeypatch, capsys, epub)
        assert code == 0
        assert data["export_tr_exists"] is True
        assert data["export_changed"] is False
        assert ("UYARI: Bu klasörde bir çeviri (EXPORT_TR.tex) var ve "
                "EXPORT.tex yeniden yazıldı; içeriği değişmedi.") in text

        cikar_env.markdown = "# Kapitel\n\n" + "Dritte Fassung. " * 30
        code, data, text = cikar_env.run(monkeypatch, capsys, epub,
                                         "--yeniden")
        assert data["export_tr_exists"] is True
        assert data["export_changed"] is True
        assert "içeriği DEĞİŞTİ" in text
        assert _sha(tr) == tr_sha, "1_cikar EXPORT_TR.tex'e dokundu"

    def test_input_missing_keeps_field_set(self, tmp_path, monkeypatch,
                                           capsys, cikar_env):
        code, data, _ = cikar_env.run(monkeypatch, capsys,
                                      tmp_path / "yok.epub")
        assert code == 1
        assert set(data) == set(cikar_env.module._JSON_FIELDS)
        assert data["error"] == "input_missing" and data["ok"] is False

    def test_help_lists_yeniden(self):
        cikar = _load_cikar()
        help_text = cikar.build_parser().format_help()
        assert "--yeniden" in help_text
        assert "full_book.onceki.md" in " ".join(help_text.split())
        assert "Yol A" not in help_text


class TestCli4NameConflict:
    def _assert_conflict(self, code, data, text, cikar_env, before):
        assert code == 1
        assert data["ok"] is False and data["error"] == "name_conflict"
        assert set(data) == set(cikar_env.module._JSON_FIELDS)
        assert data["output_dir"] == str(cikar_env.out)
        assert "adıyla daha önce BAŞKA bir kitap çıkarılmış" in text
        assert "hiçbir dosyaya dokunulmadı" in text
        assert "--name" in text
        assert "sha1" not in text
        assert _manifest(cikar_env.out) == before
        assert cikar_env.extractions == 1

    def test_conflict_via_sidecar(self, tmp_path, monkeypatch, capsys,
                                  cikar_env):
        first = _make_epub(tmp_path / "a" / "buch.epub"
                           if (tmp_path / "a").mkdir() is None else None,
                           "Erstes Buch.")
        cikar_env.run(monkeypatch, capsys, first)
        (cikar_env.out / "EXPORT_TR.tex").write_text("çeviri", encoding="utf-8")
        before = _manifest(cikar_env.out)
        (tmp_path / "b").mkdir()
        other = _make_epub(tmp_path / "b" / "buch.epub", "Zweites Buch.")
        code, data, text = cikar_env.run(monkeypatch, capsys, other)
        self._assert_conflict(code, data, text, cikar_env, before)
        assert data["export_tr_exists"] is True

    def test_conflict_without_sidecar(self, tmp_path, monkeypatch, capsys,
                                      cikar_env):
        (tmp_path / "a").mkdir()
        first = _make_epub(tmp_path / "a" / "buch.epub", "Erstes Buch.")
        cikar_env.run(monkeypatch, capsys, first)
        (cikar_env.out / "kitap.json").unlink()
        before = _manifest(cikar_env.out)
        (tmp_path / "b").mkdir()
        other = _make_epub(tmp_path / "b" / "buch.epub", "Zweites Buch.")
        code, data, text = cikar_env.run(monkeypatch, capsys, other)
        self._assert_conflict(code, data, text, cikar_env, before)


# ===========================================================================
# CLI5 — kitaba özel talimat tam yollarla; arayüzden çıkarılmış kitap (S6)
# ===========================================================================

class TestCli5Directive:
    def test_per_book_copy_has_full_paths(self, tmp_path):
        cikar = _load_cikar()          # gerçek PROJECT_ROOT: kök talimat var
        out = tmp_path / "output" / "kitap_adi"
        out.mkdir(parents=True)
        export = out / "EXPORT.tex"
        export_tr = out / "EXPORT_TR.tex"
        dest = cikar._emit_directive(out, "kitap_adi", export, export_tr,
                                     source_copy=out / "source_book.pdf",
                                     book_name="Kitap Adı")
        text = dest.read_text(encoding="utf-8")
        assert dest == out / "CEVIRI_TALIMATI.md"
        assert text.startswith("# Bu kitap için çeviri talimatı — Kitap Adı")
        for path in (export, export_tr, out / "source_book.pdf",
                     PROJECT_ROOT / "2_derle.py"):
            assert str(path) in text, path
        assert sys.executable in text
        assert "Adım 1 (çıkarım) bu kitap için YAPILDI" in text
        # Genel gövdedeki yer tutucular bu kitabın yollarıyla doldu.
        assert "output/<slug>" not in text and "<slug>" not in text
        assert f"python 2_derle.py {out}/EXPORT_TR.tex" in text
        assert f"`{out}/source_book.pdf`" in text

    def test_root_talimat_covers_ui_extracted_book(self):
        text = (PROJECT_ROOT / "CEVIRI_TALIMATI.md").read_text(encoding="utf-8")
        flat = " ".join(text.split())
        assert "## Başlamadan önce — kitap zaten çıkarılmış mı?" in text
        assert "Adım 1'i ATLA" in flat
        assert "output/<slug>/CEVIRI_TALIMATI.md" in flat
        assert "1_cikar.py --yeniden" in flat
        assert text.index("Başlamadan önce") < text.index("## Adım 1")
        assert "Yol A" not in text.splitlines()[0]
        assert branding.TEX_TITLE in text.splitlines()[0]

    def test_assistant_prompt(self, tmp_path):
        path = tmp_path / "CEVIRI_TALIMATI.md"
        prompt = yo.assistant_prompt(path)
        assert prompt.startswith(str(path))
        assert "Türkçeye çevir" in prompt


# ===========================================================================
# CLI6 — kullanıcıya görünen ASCII Türkçe
# ===========================================================================

_OLD_ASCII_HEADER = (
    "% ==== KITAPCEVIR-META v1 ====\n"
    "% slug: eski_kitap\n"
    "% book_name: Eski Kitap\n"
    "% output_dir: /yok/output/eski_kitap\n"
    "% source_format: pdf\n"
    "% source_sha1: abc123\n"
    "% body_chapters: 3\n"
    "% book_type: novel\n"
    "% created: 2026-07-15T10:00:00\n"
    "% NOT: Bu satirlari SILME/DEGISTIRME — 2_derle.py kitabi bulmak icin "
    "kullanir.\n"
    "% ============================\n\n"
)


class TestCli6TurkishTexts:
    def test_meta_note_is_turkish(self):
        header = yo.build_meta_header({"slug": "k"})
        assert yo.META_NOTE in header
        assert "Bu satırları SİLME/DEĞİŞTİRME" in header
        assert "kitabı bulmak için kullanır" in header
        assert "satirlari" not in header

    def test_old_and_new_headers_parse_the_same(self, tmp_path):
        old = tmp_path / "eski.tex"
        old.write_text(_OLD_ASCII_HEADER + "\\chapter{A}\n", encoding="utf-8")
        meta = yo.parse_meta_header(old)
        new = tmp_path / "yeni.tex"
        new.write_text(yo.build_meta_header(meta) + "\\chapter{A}\n",
                       encoding="utf-8")
        assert yo.parse_meta_header(new) == meta
        assert meta["slug"] == "eski_kitap" and meta["body_chapters"] == "3"
        assert "NOT" not in meta

    def test_old_header_still_resolves(self, tmp_path):
        book = tmp_path / "output" / "eski_kitap"
        book.mkdir(parents=True)
        tex = tmp_path / "EXPORT_TR.tex"
        tex.write_text(_OLD_ASCII_HEADER + "\\chapter{A}\n", encoding="utf-8")
        res = yo.resolve_book_dir(tex, tmp_path)
        assert res["output_dir"] == str(book.resolve())
        assert res["source"] == "header-slug"

    def test_meta_body(self):
        body = "\\chapter{A}\nMetin.\n"
        assert yo.meta_body(yo.build_meta_header({"slug": "k"}) + body) == body
        assert yo.meta_body(_OLD_ASCII_HEADER + body) == body
        assert yo.meta_body(body) == body

    def test_translate_book_header_lines(self, tmp_path, monkeypatch, flow,
                                         capsys):
        epub = _make_epub(tmp_path / "book.epub")
        assert _main(monkeypatch, epub, "--output-dir", tmp_path / "out") == 0
        out = capsys.readouterr().out
        assert f"  Çıktı:  {tmp_path / 'out'}" in out
        assert "Cikti:" not in out

    def test_translate_book_help_turkish(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["translate_book.py", "--help"])
        with pytest.raises(SystemExit):
            tb.parse_args()
        text = " ".join(capsys.readouterr().out.split())
        assert "Çıktı klasörü" in text and "Cikti dizini" not in text
        assert f"Kilidi kaldır: '{branding.TEX_TITLE}' ile çevrilmiş" in text


class TestCli6PixelDiffPageCount:
    @staticmethod
    def _pdf(path: Path, pages: int) -> Path:
        doc = fitz.open()
        for i in range(pages):
            page = doc.new_page(width=200, height=200)
            page.insert_text((20, 50), f"Sayfa {i}", fontsize=12)
        doc.save(str(path))
        doc.close()
        return path

    def test_difference_in_result_and_report(self, tmp_path, caplog):
        from tools.pixel_diff import compare_pdfs, generate_report

        a = self._pdf(tmp_path / "a.pdf", 2)
        b = self._pdf(tmp_path / "b.pdf", 3)
        with caplog.at_level(logging.WARNING, logger="tools.pixel_diff"):
            res = compare_pdfs(str(a), str(b), output_dir=str(tmp_path / "d"),
                               dpi=36)
        assert (res.original_pages, res.translated_pages) == (2, 3)
        assert res.total_pages == 2 and res.compared_pages == 2
        assert res.page_count_difference == 1
        assert res.page_count_note == (
            "Sayfa sayısı farklı: orijinal 2, çevrilmiş 3 sayfa (çevrilmiş "
            "PDF 1 sayfa fazla). Yalnız ilk 2 sayfa karşılaştırıldı.")
        assert any("Sayfa sayısı farkı: orijinal=2, çevrilmiş=3"
                   in r.getMessage() for r in caplog.records)
        html = Path(generate_report(res, str(tmp_path / "r.html"),
                                    include_images=False)).read_text(
            encoding="utf-8")
        assert "Sayfa sayısı farklı: orijinal 2, çevrilmiş 3 sayfa" in html
        assert "PDF Karşılaştırma Raporu" in html
        for ascii_text in ("Karsilastirma", "Cevrilmis", "Gorsel yok"):
            assert ascii_text not in html

    def test_equal_pages_no_note(self, tmp_path):
        from tools.pixel_diff import compare_pdfs

        a = self._pdf(tmp_path / "a.pdf", 2)
        b = self._pdf(tmp_path / "b.pdf", 2)
        res = compare_pdfs(str(a), str(b), output_dir=str(tmp_path / "d"),
                           dpi=36)
        assert res.page_count_difference == 0
        assert res.page_count_note is None

    def test_fewer_pages_note(self, tmp_path):
        from tools.pixel_diff import compare_pdfs

        a = self._pdf(tmp_path / "a.pdf", 3)
        b = self._pdf(tmp_path / "b.pdf", 1)
        res = compare_pdfs(str(a), str(b), output_dir=str(tmp_path / "d"),
                           dpi=36, max_pages=1)
        assert "(çevrilmiş PDF 2 sayfa az)" in res.page_count_note
        assert "Yalnız ilk 1 sayfa karşılaştırıldı." in res.page_count_note


# ===========================================================================
# CLI7 — yalnız etiketten oluşan \chapter başlığının çeviri biçimi
# ===========================================================================

# Başlık-sayfası sezgisi (ilk \chapter kısa ve ardından \section'sız başka
# \chapter geliyorsa \chapter*) devreye girmesin diye bölümler > 2000 karakter.
_ES = ("El sistema nervioso central recibe las señales de los órganos "
       "sensoriales y las transmite a la corteza, donde se integran con la "
       "memoria de trabajo del sujeto.\n\n") * 16
_TR = ("Merkezi sinir sistemi duyu organlarından gelen sinyalleri alır ve "
       "bunları öznenin çalışma belleğiyle birleştikleri kabuğa "
       "iletir.\n\n") * 16
_SRC_BODY = ("\\chapter{CAPÍTULO PRIMERO}\n\n" + _ES
             + "\\chapter{CAPÍTULO VII}\n\n" + _ES)


def _chapters(tex: str) -> list[str]:
    return re.findall(r"\\chapter\*?\{[^}]*\}", tex)


@pytest.fixture
def derle_label_book(tmp_path, monkeypatch, root_logger_guard):
    """Kaynağı yalın etiketli iki \\chapter olan kitap; 2_derle derlemesiz."""
    out = tmp_path / "output" / "etiket"
    (out / "source").mkdir(parents=True)
    (out / "full_book.md").write_text("# CAPÍTULO PRIMERO\n\n" + _ES,
                                      encoding="utf-8")
    (out / "source" / "body.tex").write_text(_SRC_BODY, encoding="utf-8")
    real = tb.run_pipeline
    monkeypatch.setattr(derle, "run_pipeline",
                        lambda *a, **k: real(*a, **{**k, "skip_compile": True}))
    monkeypatch.setattr(tb, "_preflight_check", lambda *a, **k: [])

    def _run(first: str, second: str) -> tuple[int, str, str]:
        meta = {"slug": "etiket", "book_name": "etiket",
                "output_dir": str(out), "source_format": "epub",
                "body_chapters": 2, "book_type": "novel"}
        tex = out / "EXPORT_TR.tex"
        tex.write_text(yo.build_meta_header(meta)
                       + f"\\chapter{{{first}}}\n\n" + _TR
                       + f"\\chapter{{{second}}}\n\n" + _TR, encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["2_derle.py", str(tex),
                                          "--output-dir", str(out)])
        code = derle.main()
        book = (out / "source" / "book_tr.tex").read_text(encoding="utf-8")
        qc = (out / "qc_report.txt").read_text(encoding="utf-8")
        return code, book, qc

    return _run


class TestCli7LabelOnlyChapter:
    def test_talimat_form_survives_2_derle(self, derle_label_book):
        """Talimattaki biçim (sıra sözcüğü / Roma rakamı) derlemeden sağ çıkar."""
        code, book, qc = derle_label_book("Birinci Bölüm", "Bölüm VII")
        assert _chapters(book) == ["\\chapter{Birinci Bölüm}",
                                   "\\chapter{Bölüm VII}"]
        assert "chapter count" not in qc
        assert code == 0

    def test_arabic_digits_are_dropped_by_2_derle(self, derle_label_book):
        """Talimatın yasakladığı biçim: sanitizer "Bölüm N" satırını sahte
        bölüm sayıp siler, QC bölüm kaybı bildirir (kuralın gerekçesi)."""
        code, book, qc = derle_label_book("Bölüm 1", "Bölüm 7")
        assert _chapters(book) == []
        assert "chapter count" in qc

    @pytest.mark.parametrize("heading,kept", [
        ("Bölüm VII", True), ("Bölüm II", True), ("Birinci Bölüm", True),
        ("İkinci Bölüm", True), ("Bölüm 7", False),
    ])
    def test_sanitizer_contract_matches_talimat(self, heading, kept):
        tr = f"\\chapter{{{heading}}}\n\n" + _TR
        out, _ = LatexSanitizer(manual_translation=True).sanitize(tr)
        assert (f"\\chapter{{{heading}}}" in out) is kept

    def test_talimat_documents_rule(self):
        text = " ".join((PROJECT_ROOT / "CEVIRI_TALIMATI.md").read_text(
            encoding="utf-8").split())
        for example in ("\\chapter{Bölüm VII}", "\\chapter{Birinci Bölüm}",
                        "\\chapter{CAPÍTULO VII}"):
            assert example in text, example
        assert "Arap rakamına ÇEVİRME" in text
        assert "(`\\chapter{Bölüm 7}` YAZMA)" in text
