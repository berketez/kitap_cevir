"""16-17 Temmuz 2026 Yol A + Yol B bug avi regresyon testleri.

Her test, o gunku denetimin bulgu numarasiyla (#N) esleştirilmiştir.
Bulgu detaylari: scratchpad/bulgular_20260716.json (session arsivi) +
memory kitapcevir-yol-audit-20260716.md.
"""

from __future__ import annotations

import importlib
import inspect
import json
import re
import sys
import types
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# G1 — Critical butunluk zinciri
# ---------------------------------------------------------------------------


class TestG1Phase6ManualFlag:
    """#0/#62: Phase 6 template sanitizer manual_translation bayragini almali."""

    def test_no_bare_latexsanitizer_call_in_translate_book(self):
        src = (PROJECT_ROOT / "translate_book.py").read_text(encoding="utf-8")
        # Bayraksiz LatexSanitizer() cagrisi kalmamali (Yol A govdesi
        # Phase 6'da MT-profil fix'leriyle bozuluyordu).
        assert not re.search(r"LatexSanitizer\(\s*\)", src), (
            "translate_book.py icinde bayraksiz LatexSanitizer() cagrisi var: "
            "manual_translation bayragi iletilmeli (#0/#62)"
        )

    def test_phase6_forwards_manual_translation(self):
        src = (PROJECT_ROOT / "translate_book.py").read_text(encoding="utf-8")
        # F2 (2026-07-28): cagrilara user_defined_envs kwarg'i eklendi; bekci
        # deseni ek kwarg'a izin verir ama bayragin iletilmesini sart kosar.
        calls = re.findall(
            r"LatexSanitizer\(manual_translation=manual_translation[,)]", src
        )
        # Phase 5.5 + Phase 6 (compile) + Phase 6 (skip-compile) = en az 3
        assert len(calls) >= 3, (
            f"LatexSanitizer(manual_translation=manual_translation...) cagri "
            f"sayisi {len(calls)} < 3 — Phase 6 dallarindan biri bayragi "
            f"iletmiyor"
        )


class TestG1Phase55FailClosed:
    """#58 (eski #17/#21): Phase 5.5 kurulum hatasi exit 0'a dusmemeli."""

    def test_fail_closed_gate_present(self):
        src = (PROJECT_ROOT / "translate_book.py").read_text(encoding="utf-8")
        assert "_phase55_failed" in src, (
            "Phase 5.5 fail-closed bayragi (_phase55_failed) kaldirilmis"
        )
        # Gate: qc_engine None VEYA phase55 fail iken return 3
        gate = re.search(
            r"if translated_body is not None and not skip_qc and \(\s*"
            r"qc_engine is None or _phase55_failed\s*\):",
            src,
        )
        assert gate, "Phase 5.5 fail-closed exit kapisi (return 3) bulunamadi"


class TestG1CompilerStalePdf:
    """#43: Compiler bayat PDF/log ile sahte basari raporlamamali."""

    def _make_config(self, tmp_path):
        from latex_pipeline.config import PipelineConfig

        return PipelineConfig(
            input_pdf=str(tmp_path / "in.pdf"),
            output_dir=str(tmp_path / "out"),
            compiler="xelatex-binary-that-does-not-exist-xyz",
            compile_passes=1,
            auto_fix_errors=False,
        )

    def test_missing_binary_with_stale_pdf_fails(self, tmp_path):
        from latex_pipeline.compiler import LatexCompiler

        tex = tmp_path / "book.tex"
        tex.write_text("\\documentclass{book}\\begin{document}x\\end{document}",
                       encoding="utf-8")
        # Onceki "basarili" kosudan kalan bayat artefaktlar:
        stale_pdf = tmp_path / "book.pdf"
        stale_pdf.write_bytes(b"%PDF-1.4 " + b"x" * 4096)
        stale_log = tmp_path / "book.log"
        stale_log.write_text("! Emergency stop.\nl.10 \n", encoding="utf-8")

        compiler = LatexCompiler(self._make_config(tmp_path))
        result = compiler.compile(tex, tmp_path / "compiled")

        assert result.success is False, (
            "Derleyici hic calismadan (binary yok) bayat PDF ile success=True dondu"
        )
        # Bayat log'un hatalari bu kosunun hatasi olarak raporlanmamali
        assert not any("Emergency stop" in e for e in result.errors), (
            f"Bayat log'un hatalari rapora sizdi: {result.errors}"
        )
        # Bayat PDF compiled/'a kopyalanmamali
        assert not (tmp_path / "compiled" / "book.pdf").exists()

    def test_stale_artifacts_removed_before_run(self, tmp_path):
        from latex_pipeline.compiler import LatexCompiler

        tex = tmp_path / "book.tex"
        tex.write_text("x", encoding="utf-8")
        (tmp_path / "book.pdf").write_bytes(b"y" * 4096)
        (tmp_path / "book.log").write_text("old", encoding="utf-8")

        compiler = LatexCompiler(self._make_config(tmp_path))
        compiler._run_passes(tex)

        # Freshness guard: bayat pdf bu kosuda uretilmedigi icin silinmis olmali
        assert not (tmp_path / "book.pdf").exists()


class TestG1DerleStalePdf:
    """#2: 2_derle.py basarisiz kosuda PDF raporlamamali/kopyalamamali."""

    def _run_main(self, tmp_path, monkeypatch, exit_code):
        derle = importlib.import_module("2_derle")

        output_dir = tmp_path / "output" / "kitap"
        (output_dir / "source").mkdir(parents=True)
        (output_dir / "source" / "body.tex").write_text("x", encoding="utf-8")
        # (A-10, 2026-07-27) run_pipeline full_book.md'yi kosulsuz ister;
        # 2_derle kapisi artik bunu da kontrol ediyor — fixture gercek
        # sozlesmeye uyduruldu.
        (output_dir / "full_book.md").write_text("# kitap\n", encoding="utf-8")
        compiled = output_dir / "compiled"
        compiled.mkdir()
        # Onceki basarili kosudan bayat PDF:
        (compiled / "kitap_TR.pdf").write_bytes(b"%PDF stale " + b"x" * 4096)

        tex = tmp_path / "EXPORT_TR.tex"
        tex.write_text("\\chapter{Deneme}\n" + "icerik " * 50, encoding="utf-8")

        monkeypatch.setattr(derle, "run_pipeline",
                            lambda *a, **k: exit_code)
        monkeypatch.setattr(
            sys, "argv",
            ["2_derle.py", str(tex), "--output-dir", str(output_dir), "--json"],
        )
        rc = derle.main()
        return rc, output_dir

    def test_exit2_no_pdf_reported(self, tmp_path, monkeypatch, capsys):
        rc, output_dir = self._run_main(tmp_path, monkeypatch, exit_code=2)
        out = capsys.readouterr().out
        json_line = [l for l in out.strip().splitlines() if l.startswith("{")][-1]
        payload = json.loads(json_line)

        assert rc == 2, f"exit 2 beklenirken {rc} dondu"
        assert payload["ok"] is False
        assert payload["pdf"] is None, (
            "Derleme basarisizken bayat PDF JSON'da raporlandi"
        )
        # Bayat PDF kok dizine kopyalanmamis olmali
        assert not (output_dir / "kitap_TR.pdf").exists()

    def test_exit0_pdf_reported(self, tmp_path, monkeypatch, capsys):
        rc, output_dir = self._run_main(tmp_path, monkeypatch, exit_code=0)
        out = capsys.readouterr().out
        json_line = [l for l in out.strip().splitlines() if l.startswith("{")][-1]
        payload = json.loads(json_line)

        assert rc == 0
        assert payload["ok"] is True
        assert payload["pdf"], "Basarili kosuda PDF raporlanmali"


# TestG1RunFullPipeline (#59, #60, #74) 5 Ekim 2026'da kaldırıldı:
# run_full_pipeline.py giriş noktalarından erişilmeyen ölü kod olarak silindi
# (KARAR 27; son hâli git etiketi arsiv/olu-kod-20261005).


# ---------------------------------------------------------------------------
# G2 — Converter / TOC releveler
# ---------------------------------------------------------------------------


def _convert_md(md: str) -> str:
    from latex_pipeline.config import PipelineConfig
    from latex_pipeline.converter import MarkdownToLatex

    cfg = PipelineConfig(input_pdf="/tmp/g2_in.pdf", output_dir="/tmp/g2_out")
    return MarkdownToLatex(cfg).convert(md)


_MD_PRE = "# My Book\n\n# Chapter 1: Intro\n\nGiris metni.\n\n"


class TestG2UnclosedDisplayMath:
    """#45: Kapanmamis $$ belgenin geri kalanini math icine yutmamali."""

    def test_unclosed_dd_does_not_swallow_document(self):
        out = _convert_md(
            _MD_PRE + "$$ E = mc^2\n\nNormal paragraph ONE.\n\n"
            "## Section 1.1\n\nParagraph two."
        )
        assert "\\section{Section 1.1}" in out
        assert "Normal paragraph ONE" in out
        assert "Paragraph two" in out

    def test_normal_multiline_math_still_works(self):
        out = _convert_md(_MD_PRE + "$$\na = b\nc = d\n$$\n\nSonraki paragraf.")
        assert "\\[" in out and "a = b" in out and "Sonraki paragraf" in out

    def test_inline_close_with_eq_number(self):
        out = _convert_md(_MD_PRE + "$$\nE = mc^2 $$ (2.3)\n\nDevam.")
        assert "\\tag{2.3}" in out

    def test_inline_close_trailing_text_preserved(self):
        out = _convert_md(_MD_PRE + "$$\nx = y $$ ve devam eden cumle.\n\nSon.")
        assert "ve devam eden" in out


class TestG2TocReleveler:
    """#44/#47/#76: sema tespiti + tekrarli baslik + en yakin seviye."""

    def test_partless_book_structure_preserved(self):
        # #44: L1=chapter, L2=section duzeni (part'siz kitap) bozulmamali
        from latex_pipeline.toc_releveler import relevel

        toc = [(1, "1 Introduction", 1), (2, "1.1 Motivation", 2),
               (1, "2 Methods", 5), (2, "2.1 Setup", 6)]
        body = ("\\chapter{1 Introduction}\n\\section{1.1 Motivation}\n"
                "\\chapter{2 Methods}\n\\section{2.1 Setup}")
        new, rep = relevel(body, toc)
        assert "\\chapter{1 Introduction}" in new
        assert "\\section{1.1 Motivation}" in new
        assert "\\chapter*" not in new, "part'siz kitapta chapter yildizlandi"

    def test_repeated_intro_sections_not_promoted(self):
        # #76: her bolumdeki 'Introduction' section'i chapter'a terfi etmemeli
        from latex_pipeline.toc_releveler import relevel

        toc = [(1, "Introduction", 1), (2, "Chapter 1 Basics", 3),
               (3, "Introduction", 4), (2, "Chapter 2 More", 9),
               (3, "Introduction", 10)]
        body = ("\\chapter{Basics}\n\\section{Introduction}\n"
                "\\chapter{More}\n\\section{Introduction}")
        new, rep = relevel(body, toc)
        assert new.count("\\section{Introduction}") == 2, new

    def test_duplicate_title_nearest_rank_wins(self):
        # #47: TOC'ta hem chapter hem section 'Introduction' varsa
        # body'deki \section en yakin seviyeyle (section) eslesmeli
        from latex_pipeline.toc_releveler import relevel

        toc = [(1, "Part I Basics", 1), (2, "Getting Started", 2),
               (2, "Introduction", 3), (3, "Introduction", 4),
               (3, "Setup", 5)]
        body = ("\\chapter{Getting Started}\n\\section{Introduction}\n"
                "\\section{Setup}")
        new, rep = relevel(body, toc)
        assert "\\section{Introduction}" in new
        assert new.count("\\chapter{") == 1

    def test_springer_scheme_still_detected(self):
        # Regresyon: Part'li Springer semasi eski davranisini korumali
        from latex_pipeline.toc_releveler import build_toc_index

        toc = [(1, "Part I: Foundations", 1), (2, "Chapter 1: Start", 2),
               (3, "Background", 3)]
        idx = build_toc_index(toc)
        cmds = [r["cmd"] for r in idx]
        assert cmds == ["part", "chapter", "section"]


# ---------------------------------------------------------------------------
# G3 — Metin yikicilar (naturalizer, sentence_splitter, prompt-leak)
# ---------------------------------------------------------------------------


class TestG3Naturalizer:
    """#14/#27: naturalizer ozne silme, kelime kaynasmasi, ikileme bozma."""

    def _nat(self):
        from core.naturalizer import TurkishNaturalizer

        return TurkishNaturalizer(aggressive=False)

    def test_tarafindan_subject_preserved(self):
        # #14: 'Deney Smith tarafindan yapilmistir' ozneyi kaybediyordu
        out = self._nat().naturalize("Deney Smith tarafından yapılmıştır.")
        assert "Smith" in out
        assert "tarafından" in out  # edilgen yapi aynen korunur
        assert "yapıdı" not in out

    def test_hicbir_sekilde_not_fused(self):
        # #27: 'hiçbir şekilde' icinden 'bir şekilde' silinip kelime kaynasiyordu
        out = self._nat().naturalize("Bunu hiçbir şekilde kabul etmez bir insan.")
        assert "hiçkabul" not in out
        assert "hiçbir şekilde" in out

    def test_bir_sekilde_still_removed_standalone(self):
        out = self._nat().naturalize("Adam garip bir şekilde davrandı çünkü kızgındı.")
        assert "bir şekilde" not in out
        assert "garip davrandı" in out

    def test_turkish_reduplication_preserved(self):
        # #27: ikileme ('yavas yavas') mesru — silinmemeli
        out = self._nat().naturalize("Adam sokakta yavaş yavaş yürüdü eve dogru.")
        assert "yavaş yavaş" in out

    def test_mt_stutter_still_collapsed(self):
        # 3+ ardisik tekrar MT stutter'idir, tekile inmeli
        out = self._nat().naturalize("Sonuç sonuç sonuç sonuç önemlidir elbette.")
        assert out.lower().count("sonuç") == 2  # ikileme siniri korunur


class TestG3SentenceSplitter:
    """#15: kisa cumleler split ciktisindan silinmemeli."""

    def test_short_sentences_not_dropped(self):
        from core.sentence_splitter import SentenceSplitter

        text = ("The committee reviewed the decision carefully. Stop now. "
                "It was rejected by the board yesterday.")
        joined = " ".join(SentenceSplitter().split(text))
        assert "Stop now." in joined, "Kisa cumle ceviri girdisinden dustu"

    def test_leading_short_sentence_kept(self):
        from core.sentence_splitter import SentenceSplitter

        text = "He ran. Then the whole group followed him into the yard."
        joined = " ".join(SentenceSplitter().split(text))
        assert "He ran." in joined


class TestG3PromptLeakPatterns:
    """#28: prompt-leak desenleri mesru Turkce metni silmemeli."""

    def _sanitize(self, body):
        from latex_pipeline.sanitizer import LatexSanitizer

        out, _ = LatexSanitizer().sanitize(body)
        return out

    def test_legitimate_math_notation_sentence_kept(self):
        body = ("Bu kitapta kullanılan matematiksel notasyon Bölüm 1'de "
                "özetlenmiştir ve okuyucuya kolaylık sağlar.\n")
        out = self._sanitize(body)
        assert "özetlenmiştir" in out
        assert "okuyucuya kolaylık" in out

    def test_legitimate_orijinal_metin_sentence_kept(self):
        body = "Orijinal metinde yazar bu konuyu üç bölümde ele alır.\n"
        out = self._sanitize(body)
        assert "üç bölümde ele alır" in out

    def test_real_prompt_leaks_still_removed(self):
        body = ("Matematiksel notasyonu koru.\n"
                "Orijinal metni değiştirme.\n"
                "Translate the following text.\n"
                "Gerçek içerik burada duruyor.\n")
        out = self._sanitize(body)
        assert "Gerçek içerik burada duruyor" in out
        assert "notasyonu koru" not in out
        assert "değiştirme" not in out
        assert "Translate the following" not in out

    def test_ceviriyi_yapan_sentence_kept(self):
        body = "Çeviriyi yapan çevirmen dipnotta açıklama ekledi.\n"
        out = self._sanitize(body)
        assert "çevirmen dipnotta" in out

    def test_baglami_korumak_sentence_kept(self):
        body = "Tarihsel bağlamı korumak çevirmenin görevidir.\n"
        out = self._sanitize(body)
        assert "çevirmenin görevidir" in out


# ---------------------------------------------------------------------------
# G4 — Sanitizer (brace, &, idempotenlik, URL, stuck words)
# ---------------------------------------------------------------------------


def _sanitize_body(body, manual=False):
    from latex_pipeline.sanitizer import LatexSanitizer

    out, report = LatexSanitizer(manual_translation=manual).sanitize(body)
    return out


class TestG4Sanitizer:
    def test_nested_brace_chapter_header_balanced(self):
        # #1/#41: ic ice brace'li baslik dengeyi bozmamali
        from translate_book import check_brace_balance

        out = _sanitize_body(
            "\\chapter{Görelilik (Einstein) ve \\emph{uzay}}\n\nMetin.")
        assert check_brace_balance(out)[2] == 0

    def test_simple_paren_chapter_still_gets_short_header(self):
        out = _sanitize_body("\\chapter{Yapay Zeka (AI)}\n\nMetin.")
        assert "\\chapter[Yapay Zeka]{Yapay Zeka (AI)}" in out

    def test_b24_align_amp_not_in_subscript(self):
        # #33: & alt-indis icine girmemeli
        out = _sanitize_body(
            "\\begin{equation*}\n\\sum_{i=1}^{n} x_i = \\mu\ny = 2\n"
            "\\end{equation*}\n")
        assert "{i&=1}" not in out
        assert "\\sum_{i=1}^{n} x_i &= \\mu" in out

    def test_b22_qquad_amp_not_in_subscript(self):
        # #78: qquad split'te de & top-level '='e gitmeli
        long_math = ("\\sum_{i=1}^{n} a_i = 1" + " + b" * 40 +
                     " \\qquad \\sum_{j=2}^{m} c_j = 2" + " + d" * 40)
        out = _sanitize_body("\\[\n" + long_math + "\n\\]\n")
        assert "{i&=1}" not in out and "{j&=2}" not in out

    def test_filtered_comment_idempotent(self):
        # #29/#87: % FILTERED her geciste buyumemeli, kuyruk kaybolmamali
        from latex_pipeline.sanitizer import LatexSanitizer

        o1, _ = LatexSanitizer().sanitize(
            "Önce metin \\write18{rm -rf /} sonra devam eden cümle.\n")
        o2, _ = LatexSanitizer().sanitize(o1)
        o3, _ = LatexSanitizer().sanitize(o2)
        assert "write18{rm" not in o3
        assert o2 == o3, "sanitize idempotent degil"
        assert "devam eden cümle" in o1, "satir kuyrugu yorumda kayboldu"

    def test_url_trailing_punctuation_preserved(self):
        # #42
        out = _sanitize_body(
            "Ayrıntı için bkz. https://example.com/sayfa. Sonraki cümle.\n")
        assert "\\url{https://example.com/sayfa}." in out

    def test_hiclik_family_preserved(self):
        # #40
        out = _sanitize_body(
            "Varoluşçulukta hiçlik kavramı ve onu hiçe saymak hiçten gelmek.\n")
        assert "hiçlik" in out and "hiçe saymak" in out and "hiçten" in out

    def test_real_stuck_hic_still_fixed(self):
        out = _sanitize_body("Bu durum bizi hiçetkilemeyecektir.\n")
        assert "hiç etkilemeyecektir" in out

    def test_bolu_bolum_collision(self):
        # #31
        out = _sanitize_body(
            "\\bölüm{Giriş} ve $a \\bölü{1}{2}$ ile $x \\çarpı y$ ve "
            "\\çarpım kavramı.\n")
        assert "\\fracm" not in out and "\\timesm" not in out

    def test_chapter_star_preserved_in_dup_cleanup(self):
        # #56
        out = _sanitize_body("\\section{Önsöz}\n\\chapter*{Önsöz}\n\nMetin.\n")
        assert "\\chapter*{Önsöz}" in out

    def test_multiline_inline_math_no_nested_dollar(self):
        # #32
        out = _sanitize_body(
            "Denklem $f(\\theta,\nx) = 0$ burada.\n\nSonraki paragraf.\n")
        assert out.count("$") % 2 == 0


# ---------------------------------------------------------------------------
# G5 — Autofix / Retranslator / QC
# ---------------------------------------------------------------------------


class TestG5Retranslator:
    def test_heading_shell_preserves_label(self):
        # #36/#80: basliktaki \label MT'ye gitmemeli, aynen korunmali
        from latex_pipeline.retranslator import _retranslate_line

        out = _retranslate_line(
            "\\section{The Gram-Schmidt Process}\\label{sec:gs}",
            lambda t: "CEVIRI(" + t + ")",
        )
        assert "\\label{sec:gs}" in out
        assert out.count("{") == out.count("}")
        assert "CEVIRI(The Gram-Schmidt Process)" in out

    def test_heading_case_first_word_capitalized(self):
        # #37: bas ASCII kelime 'numara oneki' sanilmamali
        from latex_pipeline.retranslator import _ensure_heading_case

        assert "Yapay zeka nedir" in _ensure_heading_case(
            "\\section{yapay zeka nedir}")
        assert "2.3 Ortogonal" in _ensure_heading_case(
            "\\section{2.3 ortogonal vektörler}")

    def test_heading_case_with_label_tail(self):
        from latex_pipeline.retranslator import _ensure_heading_case

        out = _ensure_heading_case(
            "\\section{istatistiksel öğrenme}\\label{sec:ist}")
        assert "İstatistiksel öğrenme" in out
        assert "\\label{sec:ist}" in out


class TestG5Autofix:
    def _fixer(self):
        from latex_pipeline.autofix import AutoFixer

        return AutoFixer.__new__(AutoFixer)

    def test_ascii_degradation_skips_command_args(self):
        # #35
        from latex_pipeline.autofix import _find_math_spans

        body = ("Bu bolumde giris yapiyoruz.\n"
                "\\url{https://ornek.com/giris-bolumu}\n"
                "\\includegraphics{giris-sekil.png}\n"
                "\\label{sec:giris}\n")
        new_body, _ = self._fixer()._fix_ascii_degradation(
            body, [], _find_math_spans(body))
        assert "\\url{https://ornek.com/giris-bolumu}" in new_body
        assert "giris-sekil.png" in new_body
        assert "{sec:giris}" in new_body
        assert "giriş" in new_body.split("\n")[0]  # duzyazi hala duzeltiliyor

    def test_segment_hallucination_global_offsets(self):
        # #34/#83
        from latex_pipeline.autofix import _find_math_spans

        body = (
            "\\begin{lstlisting}\n"
            "restore state and retry now restore state and retry now "
            "restore state and retry now\n"
            "\\end{lstlisting}\n"
            "Paragraf: model çok iyi çalışıyor gibi model çok iyi çalışıyor "
            "gibi model çok iyi çalışıyor gibi bitti.\n")
        new, _ = self._fixer()._fix_segment_hallucination(
            body, [], _find_math_spans(body))
        assert new.count("restore state and retry now") == 3, (
            "lstlisting icindeki kod budandi")
        assert new.count("model çok iyi çalışıyor gibi") == 1, (
            "duzyazi tekrar dongusu duzeltilmedi")

    def test_brace_rollback_zero_tolerance(self):
        # #85: +2 toleransi kaldirildi
        src = (PROJECT_ROOT / "latex_pipeline" / "autofix.py").read_text(
            encoding="utf-8")
        assert "abs(snapshot_brace_delta) + 2" not in src


class TestG5QCEngine:
    def test_ascii_degradation_masks_code_and_args(self):
        # #39
        from latex_pipeline.qc_engine import QCEngine

        body = ("Normal Türkçe metin ve çeviri.\n"
                "\\begin{lstlisting}\n"
                "sonuc = hesapla(girdi)\n"
                "\\end{lstlisting}\n"
                "\\url{https://ornek.com/giris}\n")
        rep = QCEngine().analyze(body)
        assert not [i for i in rep.issues
                    if i.check_name == "ascii_degradation"]

    def test_scattered_html_reported_with_other_issues(self):
        # #38
        from latex_pipeline.qc_engine import QCEngine

        body = "Türkçe metin <<LATEX_5>> kalıntısı.\n" + "".join(
            f"satır <div>içerik</div> devam {i}.\n" for i in range(12))
        rep = QCEngine().analyze(body)
        assert [i for i in rep.issues if i.check_name == "html_artifact"], (
            "daginik HTML etiketleri baska bulgular varken raporlanmadi")


# ---------------------------------------------------------------------------
# G6 — Yol B cekirdegi
# ---------------------------------------------------------------------------


class TestG6Placeholder:
    def test_restore_exact_match_skips_variants(self):
        # #17a: restore edilen icerikteki 'v0' yeniden degistirilmemeli
        from core.placeholder import PlaceholderManager

        pm = PlaceholderManager()
        protected = pm.protect("Hız $v0$ ile artar.")
        # Ceviri placeholder'i aynen korudu varsayalim
        restored = pm.restore(protected)
        assert restored == "Hız $v0$ ile artar.", restored
        assert "$$" not in restored

    def test_restore_replacement_escape_safe(self):
        # #17b: entry.original replacement-string olarak yorumlanmamali
        from core.placeholder import PlaceholderManager

        pm = PlaceholderManager()
        protected = pm.protect("Açı $\\theta$ değeri.")
        # MT placeholder'i bozdu: {{v0}} -> { v0 }
        broken = protected.replace("{{v0}}", "{ v0 }")
        restored = pm.restore(broken)
        assert "\t" not in restored, "\\t TAB karakterine donustu"
        assert "\\theta" in restored, restored


class TestG6Memory:
    def test_vN_placeholder_poison_rejected(self):
        # #18
        from core.memory import _PLACEHOLDER_RE

        assert _PLACEHOLDER_RE.search("Bu {{v0}} formül")
        assert _PLACEHOLDER_RE.search("Bu {{ v 12 }} formül")
        assert _PLACEHOLDER_RE.search("Bozuk { v3 } kalıntı")
        assert not _PLACEHOLDER_RE.search("Normal Türkçe cümle {parantez}")

    def test_all_db_methods_locked(self):
        # #25: kilitsiz DB metodu kalmamis olmali
        import inspect as _inspect

        from core.memory import TranslationMemory

        for name in ("confirm", "delete", "clean_unknown_tokens",
                     "get_stats", "export_to_json", "clear_project"):
            src = _inspect.getsource(getattr(TranslationMemory, name))
            assert "self._lock" in src, f"{name} hala kilitsiz"


class TestG6Consistency:
    def test_unknown_bucket_never_recommended(self):
        # #16a: '[?]' onerilen ceviri olamaz
        from core.consistency import ConsistencyChecker

        cc = ConsistencyChecker()
        cc.term_translations = {
            "force": {"[?]": [0, 1, 2], "kuvvet": [3]},
        }
        corrections = cc.get_corrections()
        for fixes in corrections.values():
            for wrong, correct in fixes:
                assert correct != "[?]"

    def test_word_boundary_replacement(self):
        # #16b: 'güç'->'kuvvet' duzeltmesi 'güçlü'yu bozmamali
        from core.consistency import ConsistencyChecker

        cc = ConsistencyChecker()
        cc.term_translations = {
            "power": {"kuvvet": [0, 1], "güç": [2]},
        }
        segments = [
            {"text": "Kuvvet artar."},
            {"text": "Kuvvet azalır."},
            {"text": "Bu güç ile güçlü olduk."},
        ]
        fixed, n = cc.apply_corrections(segments)
        assert "güçlü" in fixed[2]["text"], fixed[2]["text"]
        assert "kuvvetlü" not in fixed[2]["text"]
        assert "Kuvvet" in fixed[2]["text"] or "kuvvet" in fixed[2]["text"]


class TestG6QAChecker:
    def test_turkish_reference_equivalents_recognized(self):
        # #23: 'Eq. 5' -> 'Denklem 5' sahte hata uretmemeli
        from core.qa_checker import QAChecker

        qa = QAChecker()
        issues = qa._check_references(
            "See Eq. 5 and Figure 3.",
            "Bkz. Denklem 5 ve Şekil 3.",
            0,
        )
        assert not issues, [i.message for i in issues]

    def test_really_missing_reference_still_flagged(self):
        from core.qa_checker import QAChecker

        qa = QAChecker()
        issues = qa._check_references(
            "See Eq. 5 and Figure 3.",
            "Bkz. Denklem 5.",
            0,
        )
        assert issues


# TestG6BookTranslatorV2 (#19, #20, #21, #22) 5 Ekim 2026'da kaldırıldı:
# sınadığı core/book_translator_v2.py giriş noktalarından erişilmeyen ölü kod
# olarak silindi (KARAR 27; son hâli git etiketi arsiv/olu-kod-20261005).


# ---------------------------------------------------------------------------
# G7 — Converter / extractor / glossary / translator
# ---------------------------------------------------------------------------


class TestG7Converter:
    def test_repeated_heading_with_body_between_kept(self):
        # #46: araya govde giren tekrar basliklar silinmemeli
        out = _convert_md(
            _MD_PRE + "### Proof\n\nBirinci ispat metni.\n\n"
            "Devam paragrafi.\n\n### Proof\n\nIkinci ispat metni.\n")
        assert out.count("Proof") >= 2, out

    def test_adjacent_marker_dup_still_removed(self):
        out = _convert_md(_MD_PRE + "### Proof\n### Proof\n\nMetin.\n")
        # bitisik dup tek kaliyor
        import re as _re

        assert len(_re.findall(r"\{Proof\}", out)) == 1

    def test_strikethrough_content_escaped(self):
        # #52
        out = _convert_md(_MD_PRE + "Deal: ~~50% off & more~~ done.\n")
        assert "\\sout{50\\% off \\& more}" in out, out


class TestG7Template:
    def test_template_has_font_placeholders_and_ulem(self):
        # #52/#53
        tpl = (PROJECT_ROOT / "latex_pipeline" / "templates" /
               "turkish_book.tex").read_text(encoding="utf-8")
        for ph in ("{{FONT_MAIN}}", "{{FONT_SANS}}", "{{FONT_MONO}}",
                   "{{FONT_MATH}}"):
            assert ph in tpl, f"{ph} template govdesinde yok"
        assert "ulem" in tpl, "\\sout icin ulem paketi yok"

    def test_layout_font_size_rounded_to_class_option(self):
        # #53: documentclass yalniz 10/11/12pt tanir
        from latex_pipeline.config import PipelineConfig
        from latex_pipeline.layout_detector import LayoutInfo

        cfg = PipelineConfig(input_pdf="/tmp/x.pdf", output_dir="/tmp/xo")
        info = LayoutInfo()
        info.page_width_mm = 170.0
        info.page_height_mm = 240.0
        info.dominant_font_size_pt = 10.9
        cfg.apply_layout(info)
        assert cfg.font_size in ("10pt", "11pt", "12pt"), cfg.font_size
        assert cfg.font_size == "11pt"


class TestG7Glossary:
    def test_add_term_both_turkish_longer_wins(self):
        # #55/#86: iki TR-karakterli giriste uzun kazanmali
        from latex_pipeline.glossary import GlossaryEngine, TermEntry

        eng = GlossaryEngine.__new__(GlossaryEngine)
        eng._terms = {}
        eng._add_term(TermEntry(en="attention", tr="dikkat mekanizması özelliği",
                                priority="high"))
        eng._add_term(TermEntry(en="attention", tr="ilgiş", priority="high"))
        assert eng._terms["attention"].tr == "dikkat mekanizması özelliği"

    def test_verify_terms_pass2_case_insensitive_preserving(self):
        # #86: cumle basi buyuk harfli yanlis ceviri yakalanip case korunmali
        from latex_pipeline.glossary import GlossaryEngine, TermEntry

        eng = GlossaryEngine.__new__(GlossaryEngine)
        eng._terms = {
            "overfitting": TermEntry(
                en="overfitting", tr="aşırı uyum", priority="high",
                wrong_alternatives=["aşırı öğrenme"]),
        }
        eng._sorted_terms = []
        eng._master_regex = None
        out = eng.verify_terms("Aşırı öğrenme tehlikelidir.", {})
        assert out.startswith("Aşırı uyum"), out


class TestG7Translator:
    def test_wrong_terms_word_boundary_and_case(self):
        # #51/#82
        from latex_pipeline.translator import LatexTranslator

        lt = LatexTranslator.__new__(LatexTranslator)
        lt._glossary_engine = object()  # Pass 2 atlansin
        lt._WRONG_TERMS = {"aşırı öğrenme": "aşırı uyum"}
        out = lt._apply_glossary("Aşırı öğrenmeye dikkat. Aşırı öğrenme kötüdür.")
        assert "uyumye" not in out and "uyumnin" not in out, out
        assert "Aşırı uyum kötüdür" in out, out
        assert "öğrenmeye" in out  # ekli form dokunulmadan kalir

    def test_abbreviations_not_split(self):
        # #50
        from latex_pipeline.translator import LatexTranslator

        lt = LatexTranslator.__new__(LatexTranslator)
        lt._restore_placeholder_variants = lambda body, stash: body
        out = lt._restore_stash("Roma M.Ö. 753'te kuruldu. Smith Ph.D. sahibidir."
                                "Bitti.Yeni cümle.", [])
        assert "M.Ö." in out, out
        assert "Ph.D." in out, out
        assert "Bitti. Yeni" in out, out

    def test_bibliography_section_does_not_swallow_next_section(self):
        # #57
        from latex_pipeline.translator import _BIBLIOGRAPHY_SECTION_ONLY_RE

        body = ("\\section{References}\nSmith, J. (2020). Paper.\n"
                "\\section{Exercises}\nSoru 1.\n\\chapter{Next}\n")
        m = _BIBLIOGRAPHY_SECTION_ONLY_RE.search(body)
        assert m is not None
        assert "Exercises" not in m.group(0), (
            "References section'i sonraki section'i yuttu")


class TestG7EpubOutput:
    def test_positional_drift_aborts(self, tmp_path, capsys):
        # #54
        import json as _json

        from latex_pipeline.epub_output import generate_epub_output

        out = tmp_path / "kitap"
        (out / "source").mkdir(parents=True)
        (out / "source" / "translated_body.tex").write_text(
            "\n\n".join(f"Paragraf {i} metni burada." for i in range(10)),
            encoding="utf-8")
        segments = [{"index": i, "text": f"Paragraph {i}"} for i in range(20)]
        (out / "epub_segment_mapping.json").write_text(
            _json.dumps({"segments": segments, "epub_path": "yok.epub"}),
            encoding="utf-8")
        res = generate_epub_output("yok.epub", str(out), "Kitap")
        assert res is None


# ---------------------------------------------------------------------------
# G8 — Yol A zinciri (kimlik, slug, exit code, META)
# ---------------------------------------------------------------------------


class TestG8DetectBookType:
    def test_history_not_novel(self):
        # #5: 'story' ⊂ 'history' carpismasi
        from translate_book import detect_book_type

        assert detect_book_type("/x/A Brief History of Time.pdf") != "novel"
        assert detect_book_type("/x/A History of Romania.pdf") != "novel"

    def test_science_keyword_wins_on_real_science(self):
        from translate_book import detect_book_type

        assert detect_book_type("/x/Advanced Calculus and History.pdf") == "science"

    def test_real_novel_still_detected(self):
        from translate_book import detect_book_type

        assert detect_book_type("/x/A Great Fantasy Novel.epub") == "novel"


class TestG8ResolveBookDir:
    def test_sha1_mismatch_skips_candidate(self, tmp_path):
        # #3: ayni slug'li FARKLI kitabin dizinine sessiz derleme olmamali
        import json as _json

        from yol_a_ortak import resolve_book_dir

        root = tmp_path
        book_dir = root / "output" / "Kitap"
        book_dir.mkdir(parents=True)
        (book_dir / "kitap.json").write_text(
            _json.dumps({"slug": "Kitap", "source_sha1": "bbbb"}),
            encoding="utf-8")

        tex = tmp_path / "EXPORT_TR.tex"
        tex.write_text(
            "% ==== KITAPCEVIR-META v1 ====\n"
            "% slug: Kitap\n"
            "% source_sha1: aaaa\n"
            "% ============================\n\n"
            "\\chapter{X}\n", encoding="utf-8")

        res = resolve_book_dir(tex, root)
        assert res["output_dir"] is None or "Kitap" not in str(
            res["output_dir"]), (
            "sha1 uyusmayan dizine cozuldu: " + str(res))
        assert any("sha1" in t for t in res["tried"])

    def test_matching_sha1_resolves(self, tmp_path):
        import json as _json

        from yol_a_ortak import resolve_book_dir

        root = tmp_path
        book_dir = root / "output" / "Kitap"
        book_dir.mkdir(parents=True)
        (book_dir / "kitap.json").write_text(
            _json.dumps({"slug": "Kitap", "source_sha1": "aaaa"}),
            encoding="utf-8")
        tex = tmp_path / "EXPORT_TR.tex"
        tex.write_text(
            "% ==== KITAPCEVIR-META v1 ====\n"
            "% slug: Kitap\n"
            "% source_sha1: aaaa\n"
            "% ============================\n\n"
            "\\chapter{X}\n", encoding="utf-8")
        res = resolve_book_dir(tex, root)
        assert res["output_dir"] and res["slug"] == "Kitap"


class TestG8MetaBookType:
    def test_meta_fields_include_book_type(self):
        # #6
        from yol_a_ortak import _META_FIELDS, build_meta_header, parse_meta_header

        assert "book_type" in _META_FIELDS

    def test_export_tex_branch_writes_meta(self):
        # #8: --export-tex dali META + sidecar yazmali (kaynak duzeyinde)
        src = (PROJECT_ROOT / "translate_book.py").read_text(encoding="utf-8")
        export_block = src.split("--export-tex: body.tex'i META")[1][:2000]
        assert "build_meta_header" in export_block
        assert "write_sidecar" in export_block

    def test_qc_analyze_no_silent_fallback(self):
        # #12
        import inspect as _inspect

        from translate_book import _qc_analyze

        src = _inspect.getsource(_qc_analyze)
        assert "except TypeError" not in src


class TestG8Derle:
    def test_exit3_propagated(self, tmp_path, monkeypatch, capsys):
        # #9: QC dusuk (exit 3) artik 0'a cevrilmez, qc_ok=False raporlanir
        derle = importlib.import_module("2_derle")

        output_dir = tmp_path / "output" / "kitap"
        (output_dir / "source").mkdir(parents=True)
        (output_dir / "source" / "body.tex").write_text("x", encoding="utf-8")
        # (A-10) run_pipeline sozlesmesi geregi full_book.md zorunlu
        (output_dir / "full_book.md").write_text("# kitap\n", encoding="utf-8")
        compiled = output_dir / "compiled"
        compiled.mkdir()
        (compiled / "kitap_TR.pdf").write_bytes(b"%PDF " + b"x" * 4096)

        tex = tmp_path / "EXPORT_TR.tex"
        tex.write_text("\\chapter{Deneme}\n" + "icerik " * 50, encoding="utf-8")
        monkeypatch.setattr(derle, "run_pipeline", lambda *a, **k: 3)
        monkeypatch.setattr(sys, "argv",
                            ["2_derle.py", str(tex),
                             "--output-dir", str(output_dir), "--json"])
        rc = derle.main()
        out = capsys.readouterr().out
        payload = json.loads(
            [l for l in out.strip().splitlines() if l.startswith("{")][-1])
        assert rc == 3
        assert payload["ok"] is True      # PDF uretildi
        assert payload["qc_ok"] is False  # ama QC gecmedi
        assert payload["pdf"]

    def test_force_mt_flag_removed(self):
        # #10: olu bayrak kaldirildi
        src = (PROJECT_ROOT / "2_derle.py").read_text(encoding="utf-8")
        assert '"--force-mt"' not in src

    def test_chapter_mismatch_warns_and_strict_fails(self, tmp_path,
                                                     monkeypatch, capsys):
        # #9: yapisal on kontrol
        derle = importlib.import_module("2_derle")

        output_dir = tmp_path / "output" / "kitap"
        (output_dir / "source").mkdir(parents=True)
        (output_dir / "source" / "body.tex").write_text("x", encoding="utf-8")
        # (A-10) run_pipeline sozlesmesi geregi full_book.md zorunlu
        (output_dir / "full_book.md").write_text("# kitap\n", encoding="utf-8")
        (output_dir / "kitap.json").write_text(
            json.dumps({"slug": "kitap", "body_chapters": 5}),
            encoding="utf-8")

        tex = tmp_path / "EXPORT_TR.tex"
        tex.write_text("\\chapter{Tek}\n" + "icerik " * 50, encoding="utf-8")
        monkeypatch.setattr(derle, "run_pipeline", lambda *a, **k: 0)
        monkeypatch.setattr(sys, "argv",
                            ["2_derle.py", str(tex),
                             "--output-dir", str(output_dir), "--strict"])
        rc = derle.main()
        out = capsys.readouterr().out
        assert rc == 1
        assert "bölüm" in out


# ---------------------------------------------------------------------------
# G9 — Giris (scriptler, bayraklar)
# ---------------------------------------------------------------------------


class TestG9TranslateBookSource:
    def _src(self):
        return (PROJECT_ROOT / "translate_book.py").read_text(encoding="utf-8")

    def test_run_extraction_can_return_false(self):
        # #65
        import inspect as _inspect

        from translate_book import run_extraction

        src = _inspect.getsource(run_extraction)
        assert "return False" in src, (
            "run_extraction hicbir kosulda False donmuyor")

    def test_phase575_glossary_absolute_path(self):
        # #64
        src = self._src()
        assert '_Path("glossary")' not in src, (
            "Phase 5.75 glossary yolu hala CWD'ye gore")

    def test_batch_name_guard(self):
        # #66
        src = self._src()
        assert "--batch ile birden fazla kitap islerken --name" in src

    def test_dead_epub_output_flag_removed(self):
        # #72
        src = self._src()
        assert '"--epub-output"' not in src
        assert '"--no-epub-output"' in src


class TestG9Scripts:
    # #67 (scripts/ eski repo yolu) ve clean_cache DB yolu testleri 5 Ekim
    # 2026'da kaldırıldı: scripts/ giriş noktalarından erişilmeyen ölü kod
    # olarak silindi (KARAR 27; son hâli git etiketi arsiv/olu-kod-20261005).

    def test_stale_exporter_copy_removed(self):
        # #71
        assert not (PROJECT_ROOT / "exporters" /
                    "markdown_exporter 2.py").exists()


class TestG10SmokeFindings:
    """E2E smoke sirasinda bulunan ek bug'lar."""

    def test_broken_passive_not_flagging_legit_passive(self):
        from latex_pipeline.qc_engine import QCEngine

        legit = [
            "Deney Smith tarafından yapılmıştır.",
            "Bu yöntem uzmanlar tarafından kullanılır.",
            "Kitap okurlar tarafından okunur.",
        ]
        for s in legit:
            assert not QCEngine._BROKEN_PASSIVE_RE.search(s), s

        broken = [
            "Deney Smith tarafından yapıyor.",
            "Rapor kurum tarafından hazırlar.",
        ]
        for s in broken:
            assert QCEngine._BROKEN_PASSIVE_RE.search(s), s
