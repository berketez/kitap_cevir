"""X kulvarı (29 Eylül 2026) — CLI ve çıkarım düzeltmelerinin regresyon testleri.

Bulgular (bug avı, 2026-09-28):
  F1-03  tek sayfaya inse de düşen PDF sayfası sessizce atlanıyordu
  F1-11  başarısız çıkarım klasörü zehirliyordu; bozuk PDF traceback + name_conflict
  F1-12  (X-1/X-2) toplu iş düşünce alt süreçte tam model yüklemesiyle yeniden
         deneme; traceback atılıyordu; pipeline.log yoktu
  F2-01  kapanmamış \\textit{ onarımı kitabın geri kalanını iki kez bastırıyordu
  F3-08  --force-mt --resume başka modelin FINAL checkpoint'ini döndürüyordu
  F3-12  --resume çıkarımı baştan koşturuyordu
  F3-13 / F5-12  --batch --resume çevrilmiş kitaplarda da model ön kontrolü
  F5-05  yalnız noktalamalı Kitap adı boş klasör adı
  F5-09  "Çevir" kitabı her seferinde baştan çıkarıyordu
  F5-11  --batch hiçbir kitabı işlemese de çıkış 0
  F5-15  CLI iletilerinde ASCII Türkçe ve noktalı ondalık

Hepsi sentetik girdiyle; model yüklenmez, gerçek Ollama/LM Studio'ya
bağlanılmaz (motor denetimleri sahte), gerçek Marker çalışmaz (sahte
çıkarıcı / geçici sahte ``marker`` paketi).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import re
import sys
import types
import zipfile
from pathlib import Path

import fitz
import pytest

import translate_book as tb

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Ortak yardımcılar
# ---------------------------------------------------------------------------

def _load_cikar():
    spec = importlib.util.spec_from_file_location(
        "cikar_fix_x", PROJECT_ROOT / "1_cikar.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_pdf(path: Path, pages: int = 3, text: str = "Heat flows.") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"{text} Page {i}.")
    doc.save(str(path))
    doc.close()
    return path


def _make_epub(path: Path, text: str = "Heat flows from hot to cold.") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("OEBPS/ch0.xhtml",
                    f"<html><body><h1>1</h1><p>{text}</p></body></html>")
    return path


def _sha1(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def _page_text(page: int) -> str:
    return (f"Page {page} body text MARK{page}X. "
            + "Heat flows from hot to cold regions through the wall. " * 3)


class _Extraction:
    def __init__(self, markdown: str):
        self.markdown = markdown
        self.images: dict = {}


class _FakeMarker:
    """MarkerExtractor sahtesi: çağrılan sayfa aralıklarını kaydeder.

    *fail_if(pages)* True dönerse gerçek hatadaki gibi IndexError atar.
    """
    calls: list = []
    fail_if = staticmethod(lambda pages: False)

    def __init__(self, config):
        self.config = config

    def extract(self, pdf_path, page_range=None):
        pages = list(page_range)
        type(self).calls.append(pages)
        if type(self).fail_if(pages):
            raise IndexError("index 8192 is out of bounds: 2, range 0 to 4560")
        return _Extraction("\n".join(_page_text(p) for p in pages))


@pytest.fixture
def fake_marker(monkeypatch):
    import latex_pipeline.extractor as extractor_mod

    class Fake(_FakeMarker):
        calls: list = []

    monkeypatch.setattr(extractor_mod, "MarkerExtractor", Fake)
    monkeypatch.setattr(tb.multiprocessing, "set_start_method",
                        lambda *a, **k: None)
    monkeypatch.setenv("TORCH_DEVICE", "cpu")
    return Fake


@pytest.fixture
def restore_root_logger():
    root = logging.getLogger()
    old_handlers, old_level = list(root.handlers), root.level
    yield
    for handler in list(root.handlers):
        if handler not in old_handlers:
            try:
                handler.close()
            except Exception:
                pass
    root.handlers[:] = old_handlers
    root.setLevel(old_level)


# ===========================================================================
# F1-03 — düşen sayfa görünür yer tutucuyla işaretlenir
# ===========================================================================

class TestF103FailedPages:
    def test_failed_page_placeholder_in_place(self, tmp_path, monkeypatch,
                                              fake_marker):
        pdf = _make_pdf(tmp_path / "kitap.pdf", pages=6)
        # Parti düşer; PDF sayfa 5 (0 tabanlı 4) tek başına da düşer.
        fake_marker.fail_if = staticmethod(lambda pages: len(pages) > 1 or 4 in pages)
        out = tmp_path / "out"
        assert tb.run_extraction(str(pdf), str(out), 3, "cpu") is True
        md = (out / tb.EXTRACTION_NAME).read_text(encoding="utf-8")
        assert tb.failed_pages_in_markdown(md) == [5]
        for page in (0, 1, 2, 3, 5):
            assert f"MARK{page}X" in md
        assert "MARK4X" not in md
        # Yer tutucu sayfanın YERİNDE: 4. sayfadan sonra, 6. sayfadan önce.
        assert (md.index("MARK3X") < md.index(tb.failed_page_placeholder(5))
                < md.index("MARK5X"))

    def test_placeholder_text_is_visible_and_parseable(self):
        text = tb.failed_page_placeholder(12)
        assert "ÇIKARILAMAYAN SAYFA" in text and "PDF sayfa 12" in text
        assert tb.failed_pages_in_markdown(f"a\n{text}\nb {tb.failed_page_placeholder(3)}") == [3, 12]
        assert tb.extraction_text_length(text) == 0

    def test_1_cikar_reports_failed_pages(self, tmp_path, monkeypatch, capsys):
        cikar = _load_cikar()
        monkeypatch.setattr(cikar, "PROJECT_ROOT", tmp_path)
        (tmp_path / "CEVIRI_TALIMATI.md").write_text("# Talimat\n",
                                                     encoding="utf-8")
        pdf = _make_pdf(tmp_path / "girdi" / "kitap.pdf")

        def _extract(pdf_path, output_dir, batch_size, device):
            md = (_page_text(0) + "\n" + tb.failed_page_placeholder(2) + "\n"
                  + _page_text(2))
            (Path(output_dir) / "full_book.md").write_text(md, encoding="utf-8")
            return True

        def _pipeline(output_dir, book_name, **kwargs):
            md = (Path(output_dir) / "full_book.md").read_text(encoding="utf-8")
            src = Path(output_dir) / "source"
            src.mkdir(parents=True, exist_ok=True)
            (src / "body.tex").write_text("\\chapter{Bir}\n\n" + md,
                                          encoding="utf-8")
            return 0

        monkeypatch.setattr(cikar, "run_extraction", _extract)
        monkeypatch.setattr(cikar, "run_pipeline", _pipeline)
        monkeypatch.setattr(sys, "argv", ["1_cikar.py", str(pdf), "--json"])
        code = cikar.main()
        text = capsys.readouterr().out
        data = json.loads(text.strip().splitlines()[-1])
        assert code == cikar.EXIT_PAGES_FAILED == 3
        assert data["ok"] is True and data["error"] is None
        assert data["failed_pages"] == [2]
        assert set(data) == set(cikar._JSON_FIELDS)
        out = tmp_path / "output" / "kitap"
        assert json.loads((out / "kitap.json").read_text(
            encoding="utf-8"))["failed_pages"] == [2]
        assert "ÇIKARILAMAYAN SAYFA" in (out / "EXPORT.tex").read_text(
            encoding="utf-8")
        assert "PDF sayfa 2" in (out / "CEVIRI_TALIMATI.md").read_text(
            encoding="utf-8")
        assert "Çıkarılamayan PDF sayfaları: 2" in text

    def test_1_cikar_clean_run_has_empty_failed_pages(self, tmp_path,
                                                      monkeypatch, capsys):
        cikar = _load_cikar()
        monkeypatch.setattr(cikar, "PROJECT_ROOT", tmp_path)
        pdf = _make_pdf(tmp_path / "girdi" / "kitap.pdf")
        monkeypatch.setattr(cikar, "run_extraction", lambda p, o, b, d: (
            (Path(o) / "full_book.md").write_text(_page_text(0) * 2,
                                                   encoding="utf-8") and True))

        def _pipeline(output_dir, book_name, **kwargs):
            src = Path(output_dir) / "source"
            src.mkdir(parents=True, exist_ok=True)
            (src / "body.tex").write_text("\\chapter{Bir}\n\n" + "x " * 80,
                                          encoding="utf-8")
            return 0

        monkeypatch.setattr(cikar, "run_pipeline", _pipeline)
        monkeypatch.setattr(sys, "argv", ["1_cikar.py", str(pdf), "--json"])
        assert cikar.main() == 0
        data = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert data["ok"] is True and data["failed_pages"] == []

    def test_run_pipeline_warns_failed_pages(self, tmp_path, monkeypatch,
                                             restore_root_logger):
        out = tmp_path / "kitap"
        (out / "source").mkdir(parents=True)
        md = "# Bir\n\n" + _page_text(0) + "\n\n" + tb.failed_page_placeholder(7)
        (out / "full_book.md").write_text(md, encoding="utf-8")
        monkeypatch.setattr(tb, "_preflight_check", lambda *a, **k: [])
        tb.run_pipeline(str(out), "Kitap", skip_translate=True,
                        skip_compile=True, skip_qc=True, book_type="science")
        log = (out / tb.PIPELINE_LOG_NAME).read_text(encoding="utf-8")
        assert re.search(r"WARNING .*Çıkarımda 1 sayfa eksik \(PDF sayfa 7\)",
                         log)
        assert "ÇIKARILAMAYAN SAYFA" in (out / "source" / "body.tex").read_text(
            encoding="utf-8")


# ===========================================================================
# F1-11 — başarısız çıkarım klasörü zehirlemez; bozuk PDF
# ===========================================================================

class TestF111PoisonedFolder:
    def test_all_pages_fail_writes_no_full_book(self, tmp_path, fake_marker):
        pdf = _make_pdf(tmp_path / "kitap.pdf", pages=4)
        fake_marker.fail_if = staticmethod(lambda pages: True)
        out = tmp_path / "out"
        assert tb.run_extraction(str(pdf), str(out), 2, "cpu") is False
        assert not (out / tb.EXTRACTION_NAME).exists()

    def test_set_aside_never_overwrites(self, tmp_path):
        (tmp_path / tb.UNUSABLE_EXTRACTION_NAME).write_text("eski",
                                                           encoding="utf-8")
        (tmp_path / "full_book.md").write_text("yeni", encoding="utf-8")
        moved = tb.set_aside_unusable_extraction(tmp_path)
        assert moved.name == "full_book.kullanilamaz.1.md"
        assert moved.read_text(encoding="utf-8") == "yeni"
        assert (tmp_path / tb.UNUSABLE_EXTRACTION_NAME).read_text(
            encoding="utf-8") == "eski"
        # Kullanılabilir çıkarım kenara alınmaz.
        (tmp_path / "full_book.md").write_text("Metin. " * 40, encoding="utf-8")
        assert tb.set_aside_unusable_extraction(tmp_path) is None

    def test_1_cikar_empty_previous_extraction_not_reused(self, tmp_path,
                                                         monkeypatch, capsys):
        cikar = _load_cikar()
        monkeypatch.setattr(cikar, "PROJECT_ROOT", tmp_path)
        epub = _make_epub(tmp_path / "girdi" / "buch.epub")
        out = tmp_path / "output" / "buch"
        out.mkdir(parents=True)
        # Eski sürümün başarısız çıkarımı: 0 baytlık full_book.md + kimlik.
        (out / "full_book.md").write_text("", encoding="utf-8")
        (out / "kitap.json").write_text(json.dumps(
            {"source_sha1": _sha1(epub)}), encoding="utf-8")
        calls = []

        def _extract(epub_path, output_dir):
            calls.append(epub_path)
            (Path(output_dir) / "full_book.md").write_text(
                "# Kapitel\n\n" + "Erste Fassung. " * 30, encoding="utf-8")
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
        assert calls, "boş full_book.md 'önceki çıkarım' sayıldı"
        assert data["extraction_reused"] is False and data["ok"] is True
        assert (out / tb.UNUSABLE_EXTRACTION_NAME).exists()

    def test_1_cikar_broken_pdf_json_and_no_folder(self, tmp_path,
                                                   monkeypatch, capsys):
        cikar = _load_cikar()
        monkeypatch.setattr(cikar, "PROJECT_ROOT", tmp_path)
        bad = tmp_path / "girdi" / "bozuk_kitap.pdf"
        bad.parent.mkdir(parents=True)
        bad.write_text("bu bir PDF değil\n", encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["1_cikar.py", str(bad), "--json"])
        code = cikar.main()
        text = capsys.readouterr().out
        data = json.loads(text.strip().splitlines()[-1])
        assert code == 1
        # Arayüzün eşlediği mevcut kod (Kitap dosyası tanınmadı).
        assert data["error"] == "input_format" and data["ok"] is False
        assert "PDF açılamadı" in text
        assert set(data) == set(cikar._JSON_FIELDS)
        assert "Traceback" not in text
        assert not (tmp_path / "output" / "bozuk_kitap").exists()

    def test_1_cikar_failed_attempt_is_not_name_conflict(self, tmp_path,
                                                        monkeypatch, capsys):
        cikar = _load_cikar()
        monkeypatch.setattr(cikar, "PROJECT_ROOT", tmp_path)
        out = tmp_path / "output" / "bozuk_kitap"
        out.mkdir(parents=True)
        # Eski sürümün bozuk dosyayla bıraktığı klasör: kimlik + bozuk kopya.
        (out / "source_book.pdf").write_bytes(b"bu bir PDF degil\n")
        (out / "kitap.json").write_text(json.dumps(
            {"source_sha1": _sha1(out / "source_book.pdf")}), encoding="utf-8")
        good = _make_pdf(tmp_path / "girdi" / "bozuk_kitap.pdf")
        monkeypatch.setattr(cikar, "run_extraction", lambda p, o, b, d: (
            (Path(o) / "full_book.md").write_text(_page_text(0) * 2,
                                                   encoding="utf-8") and True))

        def _pipeline(output_dir, book_name, **kwargs):
            src = Path(output_dir) / "source"
            src.mkdir(parents=True, exist_ok=True)
            (src / "body.tex").write_text("\\chapter{K}\n\n" + "x " * 80,
                                          encoding="utf-8")
            return 0

        monkeypatch.setattr(cikar, "run_pipeline", _pipeline)
        monkeypatch.setattr(sys, "argv", ["1_cikar.py", str(good), "--json"])
        code = cikar.main()
        data = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert code == 0 and data["error"] is None, data
        assert _sha1(out / "source_book.pdf") == _sha1(good)
        assert (out / "source_book.onceki.pdf").read_bytes() == b"bu bir PDF degil\n"

    def test_translate_book_broken_pdf_clean_error(self, tmp_path, monkeypatch,
                                                   capsys):
        bad = tmp_path / "bozuk.pdf"
        bad.write_text("bu bir PDF değil\n", encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["translate_book.py", str(bad),
                                          "--output-dir", str(tmp_path / "out")])
        with pytest.raises(SystemExit) as exc:
            tb.main()
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "PDF açılamadı" in out and "Traceback" not in out
        # Bozuk dosyanın kopyası klasöre alınmaz.
        assert not (tmp_path / "out" / "source_book.pdf").exists()


# ===========================================================================
# F1-12 (X-1 / X-2) — ikiye bölerek geri çekilme, alt süreç yalnız çöküşte,
# traceback günlükte
# ===========================================================================

class TestF112Fallback:
    def test_bisect_instead_of_single_pages(self, tmp_path, fake_marker):
        pdf = _make_pdf(tmp_path / "kitap.pdf", pages=20)
        # Boyuta bağlı hata (hf: 20 sayfalık parti düşüyor, küçüğü geçiyor).
        fake_marker.fail_if = staticmethod(lambda pages: len(pages) > 5)
        out = tmp_path / "out"
        assert tb.run_extraction(str(pdf), str(out), 20, "cpu") is True
        sizes = [len(c) for c in fake_marker.calls]
        assert sizes == [20, 10, 5, 5, 10, 5, 5], sizes
        md = (out / tb.EXTRACTION_NAME).read_text(encoding="utf-8")
        assert [int(m) for m in re.findall(r"MARK(\d+)X", md)] == list(range(20))
        assert tb.failed_pages_in_markdown(md) == []

    def test_deterministic_error_not_retried_in_subprocess(self, tmp_path,
                                                          monkeypatch):
        import latex_pipeline.extractor as ex
        from latex_pipeline.config import PipelineConfig

        pdf = _make_pdf(tmp_path / "kitap.pdf", pages=2)
        extractor = ex.MarkerExtractor(PipelineConfig(
            input_pdf=str(pdf), output_dir=str(tmp_path / "out")))
        sub = []

        def _boom(pdf_path, page_range):
            raise IndexError("index 8192 is out of bounds")

        monkeypatch.setattr(extractor, "_run_marker", _boom)
        monkeypatch.setattr(extractor, "_extract_in_subprocess",
                            lambda *a, **k: sub.append(a))
        with pytest.raises(IndexError):
            extractor.extract(pdf, page_range=[0, 1])
        assert sub == [], "deterministik hata alt süreçte yeniden denendi"

    def test_process_crash_still_uses_subprocess(self, tmp_path, monkeypatch):
        from concurrent.futures.process import BrokenProcessPool

        import latex_pipeline.extractor as ex
        from latex_pipeline.config import PipelineConfig

        pdf = _make_pdf(tmp_path / "kitap.pdf", pages=2)
        extractor = ex.MarkerExtractor(PipelineConfig(
            input_pdf=str(pdf), output_dir=str(tmp_path / "out")))

        def _crash(pdf_path, page_range):
            raise BrokenProcessPool("A process in the process pool was "
                                    "terminated abruptly")

        monkeypatch.setattr(extractor, "_run_marker", _crash)
        monkeypatch.setattr(extractor, "_extract_in_subprocess",
                            lambda *a, **k: ("# Metin\n\n" + "söz " * 40, {}, {}))
        monkeypatch.setattr(extractor.config, "extract_images", False)
        result = extractor.extract(pdf, page_range=[0])
        assert "söz" in result.markdown

    def test_subprocess_error_carries_traceback(self, tmp_path, monkeypatch,
                                                caplog):
        import latex_pipeline.extractor as ex
        from latex_pipeline.config import PipelineConfig

        # Geçici SAHTE marker paketi (model yüklemez): derindeki adlı
        # fonksiyonda düşer; traceback'te bu ad görünmeli (X-2).
        fake = tmp_path / "sahte"
        (fake / "marker" / "converters").mkdir(parents=True)
        (fake / "marker" / "__init__.py").write_text("", encoding="utf-8")
        (fake / "marker" / "converters" / "__init__.py").write_text(
            "", encoding="utf-8")
        (fake / "marker" / "models.py").write_text(
            "def create_model_dict():\n    return {}\n", encoding="utf-8")
        (fake / "marker" / "converters" / "pdf.py").write_text(
            "def derin_katman_fonksiyonu():\n"
            "    raise IndexError('index 8192 is out of bounds')\n"
            "class PdfConverter:\n"
            "    def __init__(self, artifact_dict=None, config=None):\n"
            "        pass\n"
            "    def __call__(self, path):\n"
            "        derin_katman_fonksiyonu()\n", encoding="utf-8")
        monkeypatch.setenv("PYTHONPATH", str(fake))
        pdf = _make_pdf(tmp_path / "kitap.pdf", pages=1)
        extractor = ex.MarkerExtractor(PipelineConfig(
            input_pdf=str(pdf), output_dir=str(tmp_path / "out")))
        caplog.set_level(logging.ERROR, logger="latex_pipeline.extractor")
        with pytest.raises(RuntimeError) as err:
            extractor._extract_in_subprocess(pdf, [0], tmp_path / "img",
                                             timeout=120)
        assert "IndexError" in str(err.value)
        assert "derin_katman_fonksiyonu" in getattr(
            err.value, "subprocess_traceback", "")
        assert any("derin_katman_fonksiyonu" in r.getMessage()
                   for r in caplog.records)

    def test_extraction_errors_go_to_pipeline_log(self, tmp_path, fake_marker):
        pdf = _make_pdf(tmp_path / "kitap.pdf", pages=3)
        fake_marker.fail_if = staticmethod(lambda pages: 1 in pages)
        out = tmp_path / "out"
        assert tb.run_extraction(str(pdf), str(out), 3, "cpu") is True
        log = (out / tb.PIPELINE_LOG_NAME).read_text(encoding="utf-8")
        assert "Traceback" in log and "IndexError" in log
        assert "PDF sayfa 2 çıkarılamadı" in log
        # Günlük evresi bitince çıkarıcı günlüğü eski hâline döner.
        assert logging.getLogger("latex_pipeline.extractor").propagate is True
        assert not logging.getLogger("pipeline.extract").handlers

    def test_progress_lines_not_confused_with_page_counter(self, tmp_path,
                                                           fake_marker,
                                                           capsys):
        from core.events import classify

        pdf = _make_pdf(tmp_path / "kitap.pdf", pages=4)
        fake_marker.fail_if = staticmethod(lambda pages: len(pages) > 1)
        tb.run_extraction(str(pdf), str(tmp_path / "out"), 4, "cpu")
        lines = capsys.readouterr().out.splitlines()
        progress = [ln for ln in lines if classify(ln) and
                    classify(ln).get("unit") == "page"]
        assert len(progress) == 1 and "Batch: pages 0-3 / 4" in progress[0]


# ===========================================================================
# F2-01 — kapanmamış '{' paragraf sonunda kapanır, metin çoğalmaz
# ===========================================================================

_DOC = (
    "\\documentclass{book}\n\\begin{document}\n\\chapter{Bir}\n\n"
    "Isının art arda geçtiği \\textit{n katmandan oluşan bir duvarda\n"
    "dirençler toplanır. % yorum\n\n"
    "İkinci paragraf \\textbf{kalın} metin.\n\n"
    "\\section{Biot Sayısı}\nSon paragraf.\n\\end{document}\n"
)


def _strip_fix(text: str) -> str:
    """Onarımın eklediklerini ('}'+yorum+satır sonu) çıkar."""
    return re.sub(r"\}+" + re.escape(tb.BRACE_FIX_COMMENT) + r"\n", "", text)


class TestF201BraceRepair:
    def test_closes_at_paragraph_end(self):
        fixed = tb.try_fix_brace_balance(_DOC)
        assert tb.check_brace_balance(fixed)[2] == 0
        line = next(ln for ln in fixed.splitlines() if tb.BRACE_FIX_COMMENT in ln)
        assert line.startswith("dirençler toplanır.}")
        # '}' belge sonuna eklenmedi.
        tail = fixed[fixed.index("\\section{Biot Sayısı}"):]
        assert "}" + tb.BRACE_FIX_COMMENT not in tail
        assert "auto-fix: brace balance" not in fixed

    def test_lossless_and_no_duplication(self):
        fixed = tb.try_fix_brace_balance(_DOC)
        assert fixed.count("Biot Sayısı") == _DOC.count("Biot Sayısı") == 1
        assert fixed.count("İkinci paragraf") == 1
        # Eklenen '}'+yorum çıkarılınca özgün metin BİREBİR geri gelir.
        assert _strip_fix(fixed) == _DOC

    def test_paragraph_break_preserved(self):
        for doc in (_DOC, "\\begin{document}\nA \\textit{b c   \n\nD.\n"
                          "\\end{document}\n"):
            fixed = tb.try_fix_brace_balance(doc)
            after = fixed.split(tb.BRACE_FIX_COMMENT, 1)[1]
            # Yorum kendi satır sonunu yer; eklenen satır sonundan sonra
            # paragrafı bitiren boş satır yine gelir (yalnız yorum satırı
            # TeX'te paragrafı bitirmez).
            lines = after.split("\n")[1:]
            first_blank = next(i for i, ln in enumerate(lines) if not ln.strip())
            assert all(ln.lstrip().startswith("%") for ln in lines[:first_blank])

    def test_multiple_openers_and_item_boundary(self):
        doc = ("\\begin{document}\n\\begin{itemize}\n"
               "  \\item Birinci \\emph{madde\n  \\item İkinci madde\n"
               "\\end{itemize}\nMetin {\\small küçük \\textbf{kalın\n\n"
               "Son.\n\\end{document}\n")
        fixed = tb.try_fix_brace_balance(doc)
        assert tb.check_brace_balance(fixed)[2] == 0
        assert "\\emph{madde}" + tb.BRACE_FIX_COMMENT in fixed
        assert "kalın}}" + tb.BRACE_FIX_COMMENT in fixed
        assert _strip_fix(fixed) == doc

    def test_unlocatable_imbalance_is_not_patched_at_end(self, caplog):
        # Yetim '}' + iki kapanmamış '{' (net +1): yer güvenle bulunamaz.
        doc = ("\\begin{document}\nA } B\n\n"
               "C \\textit{x \\textbf{y\n\\end{document}\n")
        caplog.set_level(logging.ERROR)
        fixed = tb.try_fix_brace_balance(doc, logging.getLogger("t"))
        assert fixed == doc
        assert any("onarılamadı" in r.getMessage() for r in caplog.records)

    def test_repair_count_reported_by_2_derle(self, tmp_path):
        import importlib
        import time

        derle = importlib.import_module("2_derle")
        fixed = tb.try_fix_brace_balance(
            "\\begin{document}\nA \\textit{b {\\small c\n\nD \\emph{e\n"
            "\\end{document}\n")
        assert tb.brace_fix_count(fixed) == 3
        (tmp_path / "source").mkdir()
        (tmp_path / "source" / "book_tr.tex").write_text(fixed, encoding="utf-8")
        assert derle._read_brace_fixes(tmp_path, time.time()) == 3
        line = derle._brace_fix_line(3)
        assert "3 kapanmamış '{'" in line and "paragrafının sonunda" in line
        assert derle._brace_fix_line(0) is None

    def test_scanner_counts_match_checker(self):
        doc = ("\\url{a%b}\\href{x}{\\textbf{y}} \\{ \\}\n"
               "\\begin{verbatim}\n{{{\n\\end{verbatim}\n"
               "a {b} % {yorum\n{c}\n")
        masked = tb._brace_scan_mask(doc)
        assert len(masked) == len(doc)
        o, c, _ = tb.check_brace_balance(doc)
        assert (masked.count("{"), masked.count("}")) == (o, c)


# ===========================================================================
# F3-08 — --force-mt checkpoint'ten sürdürmez
# ===========================================================================

_EN_BODY = ("\\chapter{Orbital Motion}\n\n"
            "The satellite moves along an elliptical path around the planet, "
            "and its speed changes with the distance from the focus of the "
            "ellipse. Engineers use these relations to plan transfers.\n\n"
            "A circular orbit is a special case in which the distance never "
            "changes during the motion of the body around the star.\n")


class TestF308ForceMtCheckpoint:
    def test_force_mt_does_not_resume_final_checkpoint(self, tmp_path,
                                                      monkeypatch,
                                                      restore_root_logger):
        seen = []

        class _Spy:
            def __init__(self, **kwargs):
                self.stats = types.SimpleNamespace(
                    segments_total=2, segments_translated=2,
                    segments_failed=0, segments_skipped=0)

            def translate(self, body, checkpoint_dir=None, resume=False,
                          progress_callback=None):
                seen.append(resume)
                return "MODELB " + body

        monkeypatch.setattr("latex_pipeline.translator.LatexTranslator", _Spy)
        monkeypatch.setattr(tb, "_preflight_check", lambda *a, **k: [])
        monkeypatch.setattr(tb, "_check_engine",
                            lambda e: types.SimpleNamespace(ok=True))
        monkeypatch.setattr(tb, "_engine_description", lambda e: "")
        monkeypatch.setattr(tb, "_memory_fit", lambda e: None)
        out = tmp_path / "kitap"
        (out / "source").mkdir(parents=True)
        (out / "checkpoints").mkdir()
        (out / "full_book.md").write_text("# K\n\n" + _EN_BODY, encoding="utf-8")
        (out / "source" / "body.tex").write_text(_EN_BODY, encoding="utf-8")
        (out / "source" / "translated_body.tex").write_text(
            "MODELA " + _EN_BODY, encoding="utf-8")
        final = out / "checkpoints" / tb.FINAL_CHECKPOINT_NAME
        final.write_text("MODELA " + _EN_BODY, encoding="utf-8")

        code = tb.run_pipeline(str(out), "Kitap", skip_translate=False,
                               skip_compile=True, skip_qc=True,
                               book_type="science", resume=True, force_mt=True,
                               mt_engine="ollama:modelb")
        assert code == 0
        assert seen == [False], seen
        assert not final.exists()
        assert list((out / "checkpoints").glob(
            tb.FINAL_CHECKPOINT_NAME + ".*.bak"))
        tr = (out / "source" / "translated_body.tex").read_text(encoding="utf-8")
        assert tr.startswith("MODELB")


# ===========================================================================
# F3-12 / F5-09 — önceki çıkarımın yeniden kullanımı
# ===========================================================================

@pytest.fixture
def main_env(tmp_path, monkeypatch):
    """translate_book.main: çıkarım ve pipeline sahte, motor denetimi ağsız."""
    calls = {"extract": [], "pipeline": []}

    def _extract_epub(epub_path, output_dir):
        calls["extract"].append(epub_path)
        (Path(output_dir) / "full_book.md").write_text(
            "# Kitap\n\n" + "Yeni çıkarım metni. " * 20, encoding="utf-8")
        tb.write_extraction_info(output_dir, epub_path, "epub")
        return True

    def _pipeline(output_dir, book_name, **kwargs):
        calls["pipeline"].append(kwargs)
        return 0

    monkeypatch.setattr(tb, "run_epub_extraction", _extract_epub)
    monkeypatch.setattr(tb, "run_pipeline", _pipeline)
    monkeypatch.setattr(tb, "_hardware_check",
                        lambda e: types.SimpleNamespace(ok=True))
    monkeypatch.setattr(tb, "_check_engine",
                        lambda e: types.SimpleNamespace(ok=True))
    monkeypatch.setattr(tb, "_source_language_pregate",
                        lambda *a, **k: None)
    return calls


def _run_main(monkeypatch, *argv) -> int:
    monkeypatch.setattr(sys, "argv", ["translate_book.py", *map(str, argv)])
    with pytest.raises(SystemExit) as exc:
        tb.main()
    return exc.value.code


def _seed_extraction(out: Path, source: Path | None, text: str = "Eski") -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "full_book.md").write_text("# Kitap\n\n" + f"{text} çıkarım. " * 20,
                                      encoding="utf-8")
    if source is not None:
        tb.write_extraction_info(out, source, "epub")


class TestF312F509ExtractionReuse:
    def test_verified_extraction_is_reused(self, tmp_path, monkeypatch,
                                           main_env, capsys):
        epub = _make_epub(tmp_path / "kitap.epub")
        out = tmp_path / "out"
        _seed_extraction(out, epub)
        assert _run_main(monkeypatch, epub, "--output-dir", out) == 0
        assert main_env["extract"] == [], "çıkarım baştan koşturuldu"
        assert "önceki çıkarım (full_book.md) yeniden kullanıldı" in capsys.readouterr().out
        assert "Eski çıkarım" in (out / "full_book.md").read_text(encoding="utf-8")

    def test_1_cikar_identity_is_enough(self, tmp_path, monkeypatch, main_env):
        # "1 · TeX'i çıkar" klasörü: kitap.json source_sha1 (F5-09).
        epub = _make_epub(tmp_path / "kitap.epub")
        out = tmp_path / "out"
        _seed_extraction(out, None)
        (out / "kitap.json").write_text(json.dumps(
            {"source_sha1": _sha1(epub)}), encoding="utf-8")
        assert _run_main(monkeypatch, epub, "--output-dir", out) == 0
        assert main_env["extract"] == []

    def test_resume_reuses_extraction(self, tmp_path, monkeypatch, main_env):
        # Kimlik dosyası yok (eski sürüm klasörü): --resume + klasördeki
        # kaynak kopyası aynı dosya ise kullanılır.
        epub = _make_epub(tmp_path / "kitap.epub")
        out = tmp_path / "out"
        _seed_extraction(out, None)
        (out / "source_book.epub").write_bytes(epub.read_bytes())
        assert _run_main(monkeypatch, epub, "--output-dir", out,
                         "--resume") == 0
        assert main_env["extract"] == []

    def test_resume_without_any_identity_reextracts(self, tmp_path,
                                                    monkeypatch, main_env):
        epub = _make_epub(tmp_path / "kitap.epub")
        out = tmp_path / "out"
        _seed_extraction(out, None)          # ne kimlik ne kaynak kopyası
        assert _run_main(monkeypatch, epub, "--output-dir", out,
                         "--resume") == 0
        assert len(main_env["extract"]) == 1

    def test_unverified_without_resume_reextracts(self, tmp_path, monkeypatch,
                                                  main_env):
        epub = _make_epub(tmp_path / "kitap.epub")
        out = tmp_path / "out"
        _seed_extraction(out, None)
        assert _run_main(monkeypatch, epub, "--output-dir", out) == 0
        assert len(main_env["extract"]) == 1

    def test_other_source_reextracts(self, tmp_path, monkeypatch, main_env):
        other = _make_epub(tmp_path / "a" / "kitap.epub", "Başka kitap metni.")
        epub = _make_epub(tmp_path / "b" / "kitap.epub")
        out = tmp_path / "out"
        _seed_extraction(out, other)
        assert _run_main(monkeypatch, epub, "--output-dir", out,
                         "--resume") == 0
        assert len(main_env["extract"]) == 1

    def test_yeniden_reextracts_and_keeps_previous(self, tmp_path, monkeypatch,
                                                   main_env):
        epub = _make_epub(tmp_path / "kitap.epub")
        out = tmp_path / "out"
        _seed_extraction(out, epub)
        assert _run_main(monkeypatch, epub, "--output-dir", out,
                         "--yeniden") == 0
        assert len(main_env["extract"]) == 1
        assert "Eski çıkarım" in (out / tb.PREVIOUS_EXTRACTION_NAME).read_text(
            encoding="utf-8")
        assert "Yeni çıkarım" in (out / "full_book.md").read_text(encoding="utf-8")

    def test_unusable_extraction_set_aside(self, tmp_path, monkeypatch,
                                           main_env):
        epub = _make_epub(tmp_path / "kitap.epub")
        out = tmp_path / "out"
        out.mkdir()
        (out / "full_book.md").write_text("kısa", encoding="utf-8")
        tb.write_extraction_info(out, epub, "epub")
        assert _run_main(monkeypatch, epub, "--output-dir", out,
                         "--resume") == 0
        assert len(main_env["extract"]) == 1
        assert (out / tb.UNUSABLE_EXTRACTION_NAME).read_text(
            encoding="utf-8") == "kısa"

    def test_batch_reuses_extraction(self, tmp_path, monkeypatch, main_env):
        monkeypatch.setattr(tb, "PROJECT_ROOT", tmp_path)
        epub = _make_epub(tmp_path / "girdi" / "kitap.epub")
        _seed_extraction(tmp_path / "output" / "kitap", epub)
        assert _run_main(monkeypatch, epub, "--batch", epub) == 0
        assert main_env["extract"] == []
        assert len(main_env["pipeline"]) == 1

    def test_batch_multi_rejects_output_dir(self, tmp_path, monkeypatch,
                                           main_env, capsys):
        a = _make_epub(tmp_path / "a.epub")
        b = _make_epub(tmp_path / "b.epub")
        assert _run_main(monkeypatch, a, "--batch", a, b, "--output-dir",
                         tmp_path / "out") == 1
        assert "--output-dir kullanılamaz" in capsys.readouterr().out
        assert main_env["extract"] == []


# ===========================================================================
# F3-13 / F5-12 — toplu kip: çevrilmiş kitaplar model ön kontrolüne girmez
# ===========================================================================

class TestF313BatchResumePrecheck:
    def _setup(self, tmp_path, monkeypatch, main_env, translated: bool):
        monkeypatch.setattr(tb, "PROJECT_ROOT", tmp_path)
        epub = _make_epub(tmp_path / "girdi" / "kitap.epub")
        out = tmp_path / "output" / "kitap"
        _seed_extraction(out, epub)
        (out / "source").mkdir()
        if translated:
            (out / "source" / "translated_body.tex").write_text(
                "Çevrilmiş gövde. " * 20, encoding="utf-8")
        checks = []
        monkeypatch.setattr(tb, "_check_engine", lambda e: checks.append(e) or
                            types.SimpleNamespace(ok=False, reason="kapalı",
                                                  hint=""))
        monkeypatch.setattr(tb, "_engine_stop", lambda check, output_dir=None:
                            tb.EXIT_MODEL_UNAVAILABLE)
        return epub, checks

    def test_translated_books_skip_model_check(self, tmp_path, monkeypatch,
                                               main_env):
        epub, checks = self._setup(tmp_path, monkeypatch, main_env, True)
        code = _run_main(monkeypatch, epub, "--batch", epub, "--resume",
                         "--mt-engine", "ollama:sahte")
        assert code == 0 and checks == []
        assert len(main_env["pipeline"]) == 1

    def test_untranslated_book_still_checked(self, tmp_path, monkeypatch,
                                             main_env):
        epub, checks = self._setup(tmp_path, monkeypatch, main_env, False)
        code = _run_main(monkeypatch, epub, "--batch", epub, "--resume",
                         "--mt-engine", "ollama:sahte")
        assert code == tb.EXIT_MODEL_UNAVAILABLE and checks == ["ollama:sahte"]
        assert main_env["pipeline"] == []


# ===========================================================================
# F5-05 — yalnız noktalamalı ad
# ===========================================================================

class TestF505Slug:
    @pytest.mark.parametrize("name", ["...", "?", "—", "!!!", "…", "-", "_", ""])
    def test_no_empty_slug(self, name):
        slug = tb.slugify(name)
        assert slug and re.fullmatch(r"kitap_[0-9a-f]{8}", slug)
        assert tb.slug_is_fallback(name)

    def test_distinct_names_distinct_folders(self):
        assert tb.slugify("...") != tb.slugify("?")
        assert tb.slugify("...") == tb.slugify("...")

    @pytest.mark.parametrize("name,expected", [
        ("Özel Ad", "Özel_Ad"), ("Война и мир", "Война_и_мир"),
        ("A Book: Part 1", "A_Book_Part_1"), ("x" * 100, "x" * 80),
    ])
    def test_normal_names_unchanged(self, name, expected):
        assert tb.slugify(name) == expected
        assert not tb.slug_is_fallback(name)

    def test_1_cikar_never_writes_to_output_root(self, tmp_path, monkeypatch,
                                                 capsys):
        cikar = _load_cikar()
        monkeypatch.setattr(cikar, "PROJECT_ROOT", tmp_path)
        epub = _make_epub(tmp_path / "girdi" / "kitap.epub")

        def _extract(epub_path, output_dir):
            (Path(output_dir) / "full_book.md").write_text(
                "# K\n\n" + "Metin. " * 40, encoding="utf-8")
            return True

        def _pipeline(output_dir, book_name, **kwargs):
            src = Path(output_dir) / "source"
            src.mkdir(parents=True, exist_ok=True)
            (src / "body.tex").write_text("\\chapter{K}\n\n" + "x " * 80,
                                          encoding="utf-8")
            return 0

        monkeypatch.setattr(cikar, "run_epub_extraction", _extract)
        monkeypatch.setattr(cikar, "run_pipeline", _pipeline)
        monkeypatch.setattr(sys, "argv", ["1_cikar.py", str(epub), "--name",
                                          "...", "--json"])
        assert cikar.main() == 0
        text = capsys.readouterr().out
        data = json.loads(text.strip().splitlines()[-1])
        assert data["slug"] == tb.slugify("...")
        assert Path(data["output_dir"]) == tmp_path / "output" / data["slug"]
        assert not (tmp_path / "output" / "EXPORT.tex").exists()
        assert "harf ya da rakam içermiyor" in text


# ===========================================================================
# F5-11 — toplu kipte hiçbir kitap işlenmezse çıkış 0 değil
# ===========================================================================

class TestF511BatchExit:
    def test_all_skipped_nonzero(self, tmp_path, monkeypatch, main_env, capsys):
        txt = tmp_path / "notlar.txt"
        txt.write_text("x", encoding="utf-8")
        code = _run_main(monkeypatch, "yok.pdf", "--batch",
                         tmp_path / "yok1.pdf", tmp_path / "yok2.epub", txt)
        assert code == 1
        out = capsys.readouterr().out
        assert "Atlanan:   3" in out

    def test_exit_code_table(self):
        assert tb._batch_exit_code({"success": [("a", 1.0)], "failed": [],
                                    "skipped": []}) == 0
        assert tb._batch_exit_code({"success": [], "failed": [],
                                    "skipped": ["a"]}) == 1
        assert tb._batch_exit_code({"success": [("a", 1.0)], "failed": [],
                                    "skipped": ["b"]}) == 1
        assert tb._batch_exit_code({"failed": []}) == 0


# ===========================================================================
# F5-15 — CLI iletileri Türkçe harflerle, ondalık virgül
# ===========================================================================

_ASCII_KACIS = ("ayni anda", "kullanilamaz", "dosyasi", "bulunamadi",
                "cok kisa", "icindir", "OZET", "Basarili", "Basarisiz",
                "format hatasi", "formati", "islerken", "turetilir")


class TestF515TurkishMessages:
    def test_flag_conflict_message(self, monkeypatch, capsys):
        assert _run_main(monkeypatch, "yok.pdf", "--export-tex",
                         "--import-tex", "yok.tex") == 1
        out = capsys.readouterr().out
        assert "aynı anda kullanılamaz" in out
        assert not any(bad in out for bad in _ASCII_KACIS)

    def test_import_tex_messages(self, tmp_path, monkeypatch, capsys):
        short = tmp_path / "kisa.tex"
        short.write_text("kısa", encoding="utf-8")
        assert _run_main(monkeypatch, "yok.pdf", "--import-tex", short) == 1
        assert _run_main(monkeypatch, "yok.pdf", "--import-tex",
                         tmp_path / "yok.tex") == 1
        out = capsys.readouterr().out
        assert "dosyası çok kısa" in out and "dosyası bulunamadı" in out
        assert not any(bad in out for bad in _ASCII_KACIS)

    def test_batch_summary(self, tmp_path, monkeypatch, main_env, capsys):
        txt = tmp_path / "notlar.txt"
        txt.write_text("x", encoding="utf-8")
        epub = _make_epub(tmp_path / "kitap.epub")
        monkeypatch.setattr(tb, "PROJECT_ROOT", tmp_path)
        _run_main(monkeypatch, epub, "--batch", epub, txt)
        out = capsys.readouterr().out
        assert "TOPLU KİP ÖZETİ" in out and "Başarılı:" in out
        assert "biçim tanınmadı" in out
        assert not any(bad in out for bad in _ASCII_KACIS)

    def test_empty_dir_and_multi_name(self, tmp_path, monkeypatch, capsys):
        empty = tmp_path / "bos"
        empty.mkdir()
        assert _run_main(monkeypatch, "yok.pdf", "--batch", empty) == 1
        a = _make_epub(tmp_path / "a.epub")
        b = _make_epub(tmp_path / "b.epub")
        assert _run_main(monkeypatch, a, "--batch", a, b, "--name", "X") == 1
        out = capsys.readouterr().out
        assert "klasöründe PDF/EPUB bulunamadı" in out
        assert "--name kullanılamaz" in out
        assert not any(bad in out for bad in _ASCII_KACIS)

    def test_format_message(self):
        with pytest.raises(ValueError) as err:
            tb.detect_input_format("kitap.txt")
        assert "Desteklenmeyen dosya biçimi" in str(err.value)

    def test_decimal_comma(self):
        assert tb._tr_decimal(2.54) == "2,5"
        assert tb._tr_decimal(3.0) == "3"
        assert tb._tr_decimal(0.8, 2) == "0,80"

    def test_language_gate_confidence_turkish_decimal(self, caplog):
        est = types.SimpleNamespace(name_tr="Portekizce", code="pt",
                                    confidence=0.987, letters=5000)
        caplog.set_level(logging.INFO, logger="t_gate")
        tb._log_source_unsupported(est, logging.getLogger("t_gate"))
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "güven=0,99" in text and "güven=0.99" not in text
