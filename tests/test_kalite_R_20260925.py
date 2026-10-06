"""Kulvar R (25 Eylül 2026): derleme raporu — gerçek TeX hatası ve eksik glif.

Karşılaştırma raporu R5: quality_report.txt yedi belgede "Errors: 0" diyordu,
oysa derleme günlüğünde gerçek TeX hataları (Double superscript, Misplaced
alignment tab, Missing $) ve eksik glifler vardı:

  - Derleme ``-file-line-error`` ile koşar; hata satırı "dosya.tex:206: ileti"
    biçimindedir. inspector yalnız "!" ile başlayan satırı, compiler yalnız
    "LaTeX Error:" kalıbını hata sayıyordu.
  - Eksik glif deseni yalnız "(U+XXXX)" biçimini tanıyordu; 8 bitlik TFM
    yazıtipinde (cmmi10) XeTeX kodu '("E7)' biçiminde yazar.
  - TeX günlük satırını 79 karakterde keser: uzun yol hata satırını, uzun
    yazıtipi adı glif satırını ikiye böler.
  - 2_derle.py --json: tex_hatalari / eksik_glif / epub_kapsam (sözleşme S7).

Girdiler sentetik günlük parçalarıdır (kitap metni içermez).
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from latex_pipeline import compiler as compiler_mod
from latex_pipeline.compiler import LatexCompiler
from latex_pipeline.config import PipelineConfig
from latex_pipeline.inspector import (
    QualityInspector,
    QualityReport,
    TEX_LOG_WRAP_WIDTH,
    parse_tex_log,
    tex_log_counts,
    unwrap_tex_log,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
derle = importlib.import_module("2_derle")

BANNER = "This is XeTeX, Version 3.141592653-2.6-0.999997 (TeX Live 2025)  25 SEP 2026"


def _wrap(line: str, width: int = TEX_LOG_WRAP_WIDTH) -> str:
    """TeX'in max_print_line kesmesini taklit et."""
    return "\n".join(line[i:i + width] for i in range(0, len(line), width))


def _log(*body: str) -> str:
    return "\n".join((BANNER,) + body) + "\n"


# ---------------------------------------------------------------------------
# Ortak ayrıştırıcı (inspector.parse_tex_log)
# ---------------------------------------------------------------------------

class TestFileLineErrors:
    def test_primitive_errors_counted(self):
        text = _log(
            "./kitap.tex:12: Double superscript.",
            "l.12 $a^{2}^",
            "            {3}$",
            "./kitap.tex:20: Misplaced alignment tab character &.",
            "l.20 A &",
            "./kitap.tex:31: Missing $ inserted.",
        )
        errors, _ = parse_tex_log(text)
        assert [(e.source, e.line_num, e.message) for e in errors] == [
            ("kitap.tex", 12, "Double superscript."),
            ("kitap.tex", 20, "Misplaced alignment tab character &."),
            ("kitap.tex", 31, "Missing $ inserted."),
        ]

    def test_path_with_spaces(self):
        errors, _ = parse_tex_log(_log(
            "/Users/x/Mobile Documents/kitap/source/kitap.tex:7: Double superscript."))
        assert len(errors) == 1 and errors[0].line_num == 7

    def test_same_error_different_lines_both_counted(self):
        errors, _ = parse_tex_log(_log(
            "./kitap.tex:5: Double superscript.",
            "./kitap.tex:9: Double superscript.",
            "./kitap.tex:9: Double superscript.",   # aynı kayıt -> bir kez
        ))
        assert [e.line_num for e in errors] == [5, 9]

    def test_overfull_box_dump_not_an_error(self):
        # Overfull kutu dökümü paragraf metnini yazar; ".tex:12: " içerse de
        # hata kaydı değildir.
        errors, _ = parse_tex_log(_log(
            "Overfull \\hbox (3.1pt too wide) in paragraph at lines 4--5",
            "[]\\TU/STIXTwoText(1)/m/n/10 bkz. ek.tex:12: not",
            "(./kitap.aux) (./kitap.toc)",
        ))
        assert errors == []

    def test_package_error_in_file_line_form_counted_once(self):
        errors, _ = parse_tex_log(_log(
            "./kitap.tex:40: Package amsmath Error: \\tag not allowed here."))
        assert len(errors) == 1
        assert errors[0].message.startswith("Package amsmath Error")


class TestBangErrors:
    def test_line_number_from_context(self):
        errors, _ = parse_tex_log(_log(
            "! Undefined control sequence.",
            "<recently read> \\foo ",
            "l.44 \\foo",
        ))
        assert len(errors) == 1
        assert errors[0].line_num == 44 and errors[0].source is None
        assert errors[0].message == "Undefined control sequence."

    def test_same_message_two_lines_counted_twice(self):
        errors, _ = parse_tex_log(_log(
            "! Double superscript.", "l.5 x",
            "! Double superscript.", "l.8 y",
        ))
        assert len(errors) == 2

    def test_fatal_summary_not_a_separate_error(self):
        errors, _ = parse_tex_log(_log(
            "./kitap.tex:3: Emergency stop.",
            "!  ==> Fatal error occurred, no output PDF file produced!",
        ))
        assert [e.message for e in errors] == ["Emergency stop."]


class TestMissingGlyphs:
    def test_tfm_hex_form_counted(self):
        _, missing = parse_tex_log(_log(
            'Missing character: There is no ç ("E7) in font cmmi10!',
            'Missing character: There is no ö ("F6) in font cmmi10!',
            'Missing character: There is no ö ("F6) in font cmmi10!',
        ))
        assert {m.message: m.count for m in missing} == {
            "ö (U+00F6) in cmmi10": 2,
            "ç (U+00E7) in cmmi10": 1,
        }

    def test_unicode_form_and_font_features_dropped(self):
        _, missing = parse_tex_log(_log(
            "Missing character: There is no ◦ (U+25E6) in font STIX Two Text "
            "Regular/OT:script=latn;language=TRK;mapping=tex-text;!",
            "Missing character: There is no ◦ (U+25E6) in font STIX Two Text "
            "Regular/OT:script=latn;language=DFLT;mapping=tex-text;!",
        ))
        assert [(m.message, m.count) for m in missing] == [
            ("◦ (U+25E6) in STIX Two Text Regular", 2)]

    def test_bracket_font_name(self):
        _, missing = parse_tex_log(_log(
            "Missing character: There is no ☃ (U+2603) in font "
            "[lmroman10-regular]:mapping=tex-text;!"))
        assert missing[0].message == "☃ (U+2603) in [lmroman10-regular]"

    def test_report_totals(self):
        rep = QualityReport()
        QualityInspector()._analyze_log(_log(
            'Missing character: There is no ç ("E7) in font cmmi10!',
            'Missing character: There is no ç ("E7) in font cmmi10!',
            "Missing character: There is no √ (U+221A) in font X!",
            "./kitap.tex:9: Double superscript.",
        ), rep)
        assert rep.tex_error_count == 1
        assert rep.missing_char_total == 3
        assert len(rep.missing_chars) == 2


class TestWrappedLog:
    def test_wrapped_error_path_and_glyph_font_rejoined(self):
        path = "/tmp/" + "uzun_klasor_adi/" * 6 + "source/kitap.tex"
        err = f"{path}:206: Display math should end with $$."
        glyph = ("Missing character: There is no ◦ (U+25E6) in font STIX Two Text "
                 "Italic Bold Italic/OT:script=latn;language=TRK;mapping=tex-text;!")
        assert len(err) > TEX_LOG_WRAP_WIDTH and len(glyph) > TEX_LOG_WRAP_WIDTH
        text = _log(_wrap(err), "l.206 ...", _wrap(glyph), _wrap(glyph))
        errors, missing = parse_tex_log(text)
        assert [(e.line_num, e.message) for e in errors] == [
            (206, "Display math should end with $$.")]
        assert [(m.message, m.count) for m in missing] == [
            ("◦ (U+25E6) in STIX Two Text Italic Bold Italic", 2)]

    def test_new_record_not_glued_to_79_char_line(self):
        full = "x" * TEX_LOG_WRAP_WIDTH           # tam 79 karakterlik satır
        lines = unwrap_tex_log(_log(
            full, 'Missing character: There is no ç ("E7) in font cmmi10!'))
        assert lines[-1].startswith("Missing character:")

    def test_unwrapped_log_left_alone(self):
        # compiler max_print_line'ı yükseltince gövdede 79'dan uzun satır
        # olur; o zaman tam 79 karakterlik satır birleştirilmez.
        long_line = "(/usr/local/texlive/" + "a/" * 60 + "x.sty)"
        seventy_nine = "y" * TEX_LOG_WRAP_WIDTH
        lines = unwrap_tex_log(_log(long_line, seventy_nine, "z"))
        assert lines[-2:] == [seventy_nine, "z"]

    def test_memory_trailer_does_not_disable_unwrap(self):
        # Sürüm satırı ve bellek dökümü kesme kuralına uymaz; kesilmiş
        # günlük yine kesilmiş sayılmalı.
        err = "/tmp/" + "k/" * 40 + "kitap.tex:3: Double superscript."
        text = _log(_wrap(err), "Here is how much of TeX's memory you used:",
                    " " + "9" * 100)
        errors, _ = parse_tex_log(text)
        assert len(errors) == 1 and errors[0].line_num == 3


class TestSummaryText:
    def test_print_summary_counts(self, capsys):
        rep = QualityReport()
        QualityInspector()._analyze_log(_log(
            "./kitap.tex:9: Double superscript.",
            "./kitap.tex:11: Missing $ inserted.",
            'Missing character: There is no ç ("E7) in font cmmi10!',
            'Missing character: There is no ç ("E7) in font cmmi10!',
        ), rep)
        rep.print_summary()
        rep.print_details()
        out = capsys.readouterr().out
        assert "Errors:   2" in out
        assert "Missing chars: 2 (1 distinct)" in out
        assert "Double superscript. (kitap.tex:9)" in out

    def test_tex_log_counts_file(self, tmp_path):
        log = tmp_path / "kitap.log"
        log.write_text(_log(
            "./kitap.tex:9: Double superscript.",
            'Missing character: There is no ç ("E7) in font cmmi10!',
            "Missing character: There is no √ (U+221A) in font X!",
        ), encoding="utf-8")
        assert tex_log_counts(log) == (1, 2)
        assert tex_log_counts(tmp_path / "yok.log") == (0, 0)


# ---------------------------------------------------------------------------
# compiler.py
# ---------------------------------------------------------------------------

class TestCompilerParseLog:
    def _parse(self, tmp_path, *body):
        log = tmp_path / "book_tr.log"
        log.write_text(_log(*body), encoding="utf-8")
        return LatexCompiler._parse_log(log)[0]

    def test_primitive_error_in_main_file(self, tmp_path):
        errors = self._parse(tmp_path, "./book_tr.tex:315: Double superscript.")
        assert errors == ["! Double superscript. [l.315]"]

    def test_error_in_other_file_has_no_line_ref(self, tmp_path):
        # .aux/.sty satır numarası _auto_fix'i book_tr.tex'in yanlış satırına
        # yönlendirmesin.
        errors = self._parse(tmp_path, "./book_tr.aux:4: Missing } inserted.")
        assert errors == ["! Missing } inserted. [book_tr.aux:4]"]
        assert "[l." not in errors[0]

    def test_latex_error_format_unchanged(self, tmp_path):
        errors = self._parse(
            tmp_path, "./book_tr.tex:21: LaTeX Error: Environment tablo undefined.")
        assert errors == ["! Environment tablo undefined. [l.21]"]

    def test_wrapped_long_path_captured(self, tmp_path):
        path = "/tmp/" + "k/" * 45 + "book_tr.tex"
        errors = self._parse(tmp_path, _wrap(f"{path}:88: Misplaced alignment tab character &."))
        assert errors == ["! Misplaced alignment tab character &. [l.88]"]

    def test_auto_fix_reaches_file_line_error(self, tmp_path):
        # Eskiden "Too many }'s" -file-line-error biçiminde hiç yakalanmadığı
        # için tasarlanan onarım hiç tetiklenmiyordu.
        tex = tmp_path / "book_tr.tex"
        tex.write_text("a\nb}}\nc\n", encoding="utf-8")
        errors = self._parse(tmp_path, "./book_tr.tex:2: Too many }'s.")
        comp = LatexCompiler(PipelineConfig(output_dir=str(tmp_path / "o")))
        assert comp._auto_fix(tex, errors) is True
        assert tex.read_text(encoding="utf-8") == "a\nb}\nc\n"

    def test_run_xelatex_raises_max_print_line(self, tmp_path):
        comp = LatexCompiler(PipelineConfig(output_dir=str(tmp_path / "o")))
        tex = tmp_path / "t.tex"
        tex.write_text("x", encoding="utf-8")
        with patch("subprocess.run", return_value=MagicMock(returncode=0)) as run:
            comp._run_xelatex(tex)
        env = run.call_args.kwargs["env"]
        assert env["max_print_line"] == str(compiler_mod.TEX_LOG_MAX_PRINT_LINE)
        assert env.get("PATH") == os.environ.get("PATH")


@pytest.mark.skipif(shutil.which("xelatex") is None, reason="xelatex yok")
class TestRealXelatex:
    def test_errors_and_glyphs_counted_from_real_log(self, tmp_path):
        tex = tmp_path / "deneme.tex"
        tex.write_text(
            "\\documentclass{article}\n\\usepackage{fontspec}\n"
            "\\begin{document}\n"
            "Deneme $x^2^3$ ve $\\c{c}ç$ \\& & son.\n"
            "\\end{document}\n", encoding="utf-8")
        comp = LatexCompiler(PipelineConfig(output_dir=str(tmp_path / "o"),
                                            compile_passes=1))
        returncode, log = comp._run_xelatex(tex)
        assert log is not None
        text = log.read_text(encoding="utf-8", errors="replace")
        # max_print_line gerçekten yükseldi: gövdede 79'dan uzun satır var.
        body = text.splitlines()[1:]
        assert any(len(line) > TEX_LOG_WRAP_WIDTH for line in body)
        n_err, n_glyph = tex_log_counts(log)
        assert n_err == 2        # Double superscript + Misplaced alignment tab
        assert n_glyph >= 1      # ç cmmi10'da yok ('("E7)' biçimi)
        errors, _ = LatexCompiler._parse_log(log)
        assert "! Double superscript. [l.4]" in errors


# ---------------------------------------------------------------------------
# 2_derle.py --json ve iletiler
# ---------------------------------------------------------------------------

_LOG_WITH_ISSUES = _log(
    "./book_tr.tex:206: Misplaced alignment tab character &.",
    "./book_tr.tex:224: Double superscript.",
    'Missing character: There is no а ("430) in font cmmi7!',
    'Missing character: There is no а ("430) in font cmmi7!',
    "Missing character: There is no √ (U+221A) in font STIX Two Text Regular/OT:x;!",
)
_CLEAN_LOG = _log("Output written on book_tr.pdf (9 pages).")


def _json_tail(out: str) -> dict:
    return json.loads(out.strip().splitlines()[-1])


class TestDerleCounts:
    @pytest.fixture
    def book(self, tmp_path):
        out = tmp_path / "output" / "kitap"
        (out / "source").mkdir(parents=True)
        (out / "source" / "body.tex").write_text("x" * 200, encoding="utf-8")
        (out / "full_book.md").write_text("# kitap\n", encoding="utf-8")
        (out / "compiled").mkdir()
        (out / "compiled" / "kitap_TR.pdf").write_bytes(b"%PDF " + b"x" * 4096)
        tex = tmp_path / "EXPORT_TR.tex"
        tex.write_text("\\chapter{Deneme}\n" + "içerik " * 50, encoding="utf-8")
        return out, tex

    def _run(self, monkeypatch, out, tex, exit_code, log_text=None, extra=()):
        def _fake_pipeline(*a, **k):
            if log_text is not None:
                (out / "source" / "book_tr.log").write_text(log_text, encoding="utf-8")
            return exit_code
        monkeypatch.setattr(derle, "run_pipeline", _fake_pipeline)
        monkeypatch.setattr(sys, "argv", ["2_derle.py", str(tex), "--output-dir",
                                          str(out), "--json", *extra])
        return derle.main()

    def test_json_counts_from_fresh_log(self, book, monkeypatch, capsys):
        out, tex = book
        self._run(monkeypatch, out, tex, 0, log_text=_LOG_WITH_ISSUES)
        data = _json_tail(capsys.readouterr().out)
        assert data["tex_hatalari"] == 2
        assert data["eksik_glif"] == 3
        assert data["epub_kapsam"] is None

    def test_stale_log_not_counted(self, book, monkeypatch, capsys):
        out, tex = book
        log = out / "source" / "book_tr.log"
        log.write_text(_LOG_WITH_ISSUES, encoding="utf-8")
        old = time.time() - 3600
        os.utime(log, (old, old))
        self._run(monkeypatch, out, tex, 1)          # derleme koşmadı
        data = _json_tail(capsys.readouterr().out)
        assert data["tex_hatalari"] == 0 and data["eksik_glif"] == 0

    def test_exit3_message_mentions_issues(self, book, monkeypatch, capsys):
        out, tex = book
        code = self._run(monkeypatch, out, tex, 3, log_text=_LOG_WITH_ISSUES)
        text = capsys.readouterr().out
        assert code == 3
        assert "2 TeX hatası ve 3 eksik glif" in text
        assert str(out / "source" / "book_tr.log") in text

    def test_exit0_message_warns(self, book, monkeypatch, capsys):
        out, tex = book
        code = self._run(monkeypatch, out, tex, 0, log_text=_LOG_WITH_ISSUES)
        text = capsys.readouterr().out
        assert code == 0
        assert "UYARI: Derleme günlüğünde 2 TeX hatası" in text

    def test_clean_log_no_warning(self, book, monkeypatch, capsys):
        out, tex = book
        self._run(monkeypatch, out, tex, 0, log_text=_CLEAN_LOG)
        text = capsys.readouterr().out
        data = _json_tail(text)
        assert "TeX hatası" not in text and "eksik glif" not in text
        assert data["tex_hatalari"] == 0 and data["eksik_glif"] == 0

    def test_only_glyphs(self, book, monkeypatch, capsys):
        out, tex = book
        log = _log('Missing character: There is no ç ("E7) in font cmmi10!')
        self._run(monkeypatch, out, tex, 3, log_text=log)
        text = capsys.readouterr().out
        assert "Derleme günlüğünde 1 eksik glif var" in text

    def test_exit2_counts_failed_compile(self, book, monkeypatch, capsys):
        # Derleme başarısız: sayılar yine günlükten; iş sonu özeti ayrı
        # hata sayılmaz, "İlk hatalar" -file-line-error satırını gösterir.
        out, tex = book
        log = _log("./book_tr.tex:3: Emergency stop.",
                   "!  ==> Fatal error occurred, no output PDF file produced!")
        code = self._run(monkeypatch, out, tex, 2, log_text=log)
        text = capsys.readouterr().out
        data = _json_tail(text)
        assert code == 2 and data["ok"] is False
        assert data["tex_hatalari"] == 1 and data["eksik_glif"] == 0
        assert "./book_tr.tex:3: Emergency stop." in text

    def test_exit_code_untouched(self, book, monkeypatch, capsys):
        # Çıkış kodu mantığı translate_book'ta: günlükte hata olsa da 0 kalır.
        out, tex = book
        assert self._run(monkeypatch, out, tex, 0, log_text=_LOG_WITH_ISSUES) == 0
        assert _json_tail(capsys.readouterr().out)["exit_code"] == 0

    # --- S7: EPUB kapsamı -------------------------------------------------

    def _epub(self, out, monkeypatch, report=None, report_age=None,
              produce=True):
        (out / "source_book.epub").write_bytes(b"PK" + b"x" * 2048)

        def _fake_epub(original_epub, output_dir, book_name):
            compiled = Path(output_dir) / "compiled"
            if report is not None:
                p = compiled / derle.EPUB_REPORT_NAME
                p.write_text(report if isinstance(report, str)
                             else json.dumps(report), encoding="utf-8")
                if report_age:
                    t = time.time() - report_age
                    os.utime(p, (t, t))
            if not produce:
                return None
            epub = compiled / "kitap_TR.epub"
            epub.write_bytes(b"PK")
            return str(epub)

        import latex_pipeline.epub_output as epub_mod
        monkeypatch.setattr(epub_mod, "generate_epub_output", _fake_epub)

    def test_epub_coverage_reported(self, book, monkeypatch, capsys):
        out, tex = book
        self._epub(out, monkeypatch, report={"kapsam": 0.916, "cevrilmemis_blok": 3,
                                             "toplam_blok": 36})
        self._run(monkeypatch, out, tex, 0, log_text=_CLEAN_LOG)
        text = capsys.readouterr().out
        assert _json_tail(text)["epub_kapsam"] == pytest.approx(0.916)
        assert "EPUB'da 3/36 blok kaynak dilde kaldı (kapsam %91,6)" in text

    def test_full_coverage_no_warning(self, book, monkeypatch, capsys):
        out, tex = book
        self._epub(out, monkeypatch, report={"kapsam": 1, "cevrilmemis_blok": 0,
                                             "toplam_blok": 36})
        self._run(monkeypatch, out, tex, 0, log_text=_CLEAN_LOG)
        text = capsys.readouterr().out
        assert _json_tail(text)["epub_kapsam"] == 1.0
        assert "kaynak dilde kaldı" not in text

    @pytest.mark.parametrize("report", [
        {"kapsam": 1.5}, {"kapsam": "0.9"}, {"kapsam": True}, {"kapsam": -0.1},
        {"toplam_blok": 3}, [0.9], "{bozuk json", "NaN",
    ])
    def test_invalid_report_is_null(self, book, monkeypatch, capsys, report):
        out, tex = book
        if report == "NaN":
            report = '{"kapsam": NaN}'
        self._epub(out, monkeypatch, report=report)
        self._run(monkeypatch, out, tex, 0, log_text=_CLEAN_LOG)
        text = capsys.readouterr().out
        assert _json_tail(text)["epub_kapsam"] is None
        assert "kaynak dilde kaldı" not in text

    def test_stale_report_is_null(self, book, monkeypatch, capsys):
        out, tex = book
        self._epub(out, monkeypatch, report={"kapsam": 0.5}, report_age=3600)
        self._run(monkeypatch, out, tex, 0, log_text=_CLEAN_LOG)
        assert _json_tail(capsys.readouterr().out)["epub_kapsam"] is None

    def test_missing_report_is_null(self, book, monkeypatch, capsys):
        out, tex = book
        self._epub(out, monkeypatch, report=None)
        self._run(monkeypatch, out, tex, 0, log_text=_CLEAN_LOG)
        data = _json_tail(capsys.readouterr().out)
        assert data["epub"] and data["epub_kapsam"] is None

    def test_no_epub_no_coverage(self, book, monkeypatch, capsys):
        # EPUB üretilmediyse (atlandı) kapsam sayısı gösterilmez.
        out, tex = book
        self._epub(out, monkeypatch, report={"kapsam": 0.4}, produce=False)
        self._run(monkeypatch, out, tex, 0, log_text=_CLEAN_LOG)
        data = _json_tail(capsys.readouterr().out)
        assert data["epub"] is None and data["epub_kapsam"] is None

    def test_partial_counts_message(self, book, monkeypatch, capsys):
        out, tex = book
        self._epub(out, monkeypatch, report={"kapsam": 0.75})
        self._run(monkeypatch, out, tex, 0, log_text=_CLEAN_LOG)
        text = capsys.readouterr().out
        assert "EPUB'da bazı bloklar kaynak dilde kaldı (kapsam %75,0)" in text


class TestDerleLogErrorLines:
    def test_wrapped_path_with_spaces(self, tmp_path):
        path = "/Users/x/Mobile Documents/" + "klasor/" * 10 + "book_tr.tex"
        log = tmp_path / "book_tr.log"
        log.write_text(_log(_wrap(f"{path}:12: Missing $ inserted."), "l.12 x"),
                       encoding="utf-8")
        lines = derle._log_error_lines(log)
        assert lines == [f"{path}:12: Missing $ inserted."[:200]]


class TestJsonFieldSet:
    def test_defaults(self, capsys):
        derle._print_json_line(True)
        data = _json_tail(capsys.readouterr().out)
        assert data["tex_hatalari"] == 0
        assert data["eksik_glif"] == 0
        assert data["epub_kapsam"] is None

    def test_cli_early_exit(self, tmp_path):
        proc = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "2_derle.py"),
             str(tmp_path / "yok.tex"), "--json"],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT))
        data = _json_tail(proc.stdout)
        assert {"tex_hatalari", "eksik_glif", "epub_kapsam"} <= set(data)
