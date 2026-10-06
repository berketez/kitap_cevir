# -*- coding: utf-8 -*-
"""2026-07-27 serbestlik + motor denetimi regresyon testleri.

Kapsam (workflow wf_cc9ea5cd-109, 35 onaylı bulgu):
- Yol A serbestlik: manuel modda kullanıcı içeriğini ezen geçişlerin
  kapatılması (A-1..A-11, A-13, A-17) + preamble_ozel.tex özelliği (A-12).
- Yol B çalışma zamanı: bayat resume (B-2), placeholder revert (B-4),
  @token orijinal kontrolü (B-5), References bölge sonu (B-6), hash kuyruğu
  (B-10), echo sayacı (B-11), timeout'lu MT çağrısı (B-1), batch progress (B-3).
- UI/entegrasyon: kaynak-düzeyi sözleşme kontrolleri (UI-1/2/4), preflight
  registry bağlantısı (MAP-2), --mt-engine bayrağı.
"""

import json
import re
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from latex_pipeline.autofix import AutoFixer
from latex_pipeline.qc_engine import QCEngine
from latex_pipeline.sanitizer import LatexSanitizer


# =============================================================================
# A-1 — "Çevirmen notu" manuel modda korunur, Yol B'de silinir
# =============================================================================


class TestA1CevirmenNotu:
    _METIN = ("Bu bölüm ekonomiyi anlatır. Çevirmen notu: bu terim "
              "Türkçede karşılıksızdır.\nDevam paragrafı.")

    def test_sanitizer_manual_preserves_note(self):
        s = LatexSanitizer(manual_translation=True)
        body, _ = s.sanitize(self._METIN)
        assert "Çevirmen notu" in body

    def test_sanitizer_mt_mode_removes_note(self):
        s = LatexSanitizer(manual_translation=False)
        body, _ = s.sanitize(self._METIN)
        assert "Çevirmen notu" not in body

    def test_qc_manual_does_not_flag_note(self):
        qc = QCEngine()
        report = qc.analyze(self._METIN, "Source text.",
                            manual_translation=True)
        notes = [i for i in report.issues
                 if i.check_name == "prompt_leakage"
                 and "notu" in (i.message or "").lower()]
        assert notes == []

    def test_qc_mt_mode_flags_note(self):
        qc = QCEngine()
        report = qc.analyze(self._METIN, "Source text.",
                            manual_translation=False)
        notes = [i for i in report.issues
                 if i.check_name == "prompt_leakage"
                 and "notu" in (i.message or "").lower()]
        assert notes, "Yol B'de Çevirmen notu leak olarak yakalanmalı"

    def test_patterns_split(self):
        from latex_pipeline.patterns import (
            ALL_PROMPT_LEAK_PATTERNS,
            PROMPT_LEAK_PATTERNS,
            PROMPT_LEAK_PATTERNS_MT_ONLY,
        )
        joined_main = "|".join(p.pattern for p in PROMPT_LEAK_PATTERNS)
        assert "evirmen" not in joined_main  # ana listede yok
        joined_mt = "|".join(p.pattern for p in PROMPT_LEAK_PATTERNS_MT_ONLY)
        assert "evirmen" in joined_mt
        assert len(ALL_PROMPT_LEAK_PATTERNS) == (
            len(PROMPT_LEAK_PATTERNS) + len(PROMPT_LEAK_PATTERNS_MT_ONLY))


# =============================================================================
# A-2 / A-3 — antonym_guard + word_concatenation manuel modda rapor-only
# =============================================================================


class TestA2A3ManualSkips:
    def test_qc_mt_feedback_set_extended(self):
        assert "antonym_guard" in QCEngine._MT_FEEDBACK_CHECKS
        assert "word_concatenation" in QCEngine._MT_FEEDBACK_CHECKS

    def test_autofix_skip_list_extended(self):
        src = (PROJECT_ROOT / "latex_pipeline" / "autofix.py").read_text(
            encoding="utf-8")
        m = re.search(r"_MANUAL_TRANSLATION_SKIP = \{(.*?)\}", src, re.DOTALL)
        assert m, "_MANUAL_TRANSLATION_SKIP bulunamadı"
        assert "antonym_guard" in m.group(1)
        assert "word_concatenation" in m.group(1)

    def test_manual_analyze_downgrades_to_report_only(self):
        qc = QCEngine()
        # word_concatenation tetikleyen yapıştırık kelime
        tr = "Bu modelYapay öğrenme için kullanılır."
        report = qc.analyze(tr, "This model is used for learning.",
                            manual_translation=True)
        for issue in report.issues:
            if issue.check_name in ("word_concatenation", "antonym_guard"):
                assert issue.auto_fixable is False


# =============================================================================
# A-4 / A-5 / A-8 — sanitizer manuel modda kullanıcı içeriğine dokunmaz
# =============================================================================


class TestSanitizerManualFreedom:
    def test_cjk_preserved_in_manual(self):
        text = "Konfüçyüs şöyle yazar: 學而時習之 (öğren ve uygula).\n"
        s = LatexSanitizer(manual_translation=True)
        body, _ = s.sanitize(text)
        assert "學而時習之" in body

    def test_cjk_removed_in_mt_mode(self):
        text = "Merhaba 學而時習之 dünya.\n"
        s = LatexSanitizer(manual_translation=False)
        body, _ = s.sanitize(text)
        assert "學而時習之" not in body

    def test_english_label_quote_preserved_in_manual(self):
        text = 'Yazar özgün metinde "The Proof of Concept" der.\n'
        s = LatexSanitizer(manual_translation=True)
        body, _ = s.sanitize(text)
        assert "The Proof" in body
        assert "İspat" not in body

    def test_celestial_table_preserved_in_manual(self):
        text = ("\\begin{tabular}{ll}\nMercury-Atlas 6 & 1962 \\\\\n"
                "\\end{tabular}\n")
        s = LatexSanitizer(manual_translation=True)
        body, _ = s.sanitize(text)
        assert "Mercury-Atlas 6" in body

    def test_celestial_table_translated_in_mt_mode(self):
        text = ("\\begin{tabular}{ll}\nMercury & 0.39 \\\\\n"
                "\\end{tabular}\n")
        s = LatexSanitizer(manual_translation=False)
        body, _ = s.sanitize(text)
        assert "Merkür" in body


# =============================================================================
# A-6 / A-7 — yapısal autofix'ler manuel modda kapalı
# =============================================================================


class TestStructuralFixesManualOff:
    def test_page_header_line_kept_in_manual(self):
        body = "12 Bu satır yılla başlayıp sayıyla biter 42\nNormal satır.\n"
        af = AutoFixer(manual_translation=True)
        fixed, fixes = af._apply_structural_fixes(body)
        assert "yılla başlayıp" in fixed

    def test_heading_ascii_kept_in_manual(self):
        body = "\\section{Tomasi ve Sanders}\n"
        af = AutoFixer(manual_translation=True)
        fixed, fixes = af._apply_structural_fixes(body)
        assert "Tomasi" in fixed  # Toması'ya çevrilmedi

    def test_structural_fixes_still_run_in_mt_mode(self):
        af = AutoFixer(manual_translation=False)
        body = "Normal metin.\n"
        fixed, fixes = af._apply_structural_fixes(body)
        assert isinstance(fixed, str)


# =============================================================================
# A-9 — kullanıcı tanımlı ortam adları yeniden adlandırılmaz
# =============================================================================


class TestA9UserDefinedEnvs:
    def test_defined_env_not_renamed(self):
        text = ("\\newtheorem{tanim}{Tanım}\n"
                "\\begin{tanim}\nBir küme...\n\\end{tanim}\n")
        s = LatexSanitizer(manual_translation=True)
        body, _ = s.sanitize(text)
        assert "\\begin{tanim}" in body
        assert "\\begin{definition}" not in body

    def test_undefined_translated_env_still_fixed(self):
        text = "\\begin{ispat}\nAçıktır.\n\\end{ispat}\n"
        s = LatexSanitizer(manual_translation=True)
        body, _ = s.sanitize(text)
        assert "\\begin{proof}" in body  # tanımsız — derleme kurtarma sürer


# =============================================================================
# A-11 — title-chapter sezgisi: içerikli ilk bölüm numarasız yapılmaz
# =============================================================================


class TestA11TitleChapter:
    def test_real_first_chapter_not_starred(self):
        body = ("\\chapter{Giriş}\n" + ("Uzun paragraf. " * 300) +
                "\n\\chapter{İkinci Bölüm}\nMetin.\n")
        s = LatexSanitizer(manual_translation=True)
        out, _ = s.sanitize(body)
        assert "\\chapter{Giriş}" in out
        assert "\\chapter*{Giriş}" not in out

    def test_short_title_page_still_starred(self):
        body = ("\\chapter{Kitap Adı}\nYazar Adı\n"
                "\\chapter{Birinci Bölüm}\nMetin.\n")
        s = LatexSanitizer(manual_translation=True)
        out, _ = s.sanitize(body)
        assert "\\chapter*{Kitap Adı}" in out


# =============================================================================
# A-13 — META parse: BOM + uzun ön-ek
# =============================================================================


class TestA13MetaParse:
    def _meta_block(self):
        from yol_a_ortak import build_meta_header
        return build_meta_header({
            "slug": "test-kitap", "book_name": "Test",
            "output_dir": "/tmp/x", "source_format": "pdf",
            "source_sha1": "abc", "body_chapters": 3,
            "book_type": "science", "created": "2026-07-27",
        })

    def test_bom_prefixed_file_parsed(self, tmp_path):
        from yol_a_ortak import parse_meta_header
        p = tmp_path / "EXPORT_TR.tex"
        p.write_bytes(b"\xef\xbb\xbf" + self._meta_block().encode("utf-8")
                      + b"\\chapter{X}\n")
        meta = parse_meta_header(p)
        assert meta.get("slug") == "test-kitap"

    def test_long_comment_prefix_parsed(self, tmp_path):
        from yol_a_ortak import parse_meta_header
        p = tmp_path / "EXPORT_TR.tex"
        prefix = "".join(f"% çevirmen yorumu satır {i}\n" for i in range(80))
        p.write_text(prefix + self._meta_block() + "\\chapter{X}\n",
                     encoding="utf-8")
        meta = parse_meta_header(p)
        assert meta.get("slug") == "test-kitap"

    def test_scan_stops_at_content(self, tmp_path):
        from yol_a_ortak import parse_meta_header
        p = tmp_path / "EXPORT_TR.tex"
        # İçerik başladıktan SONRA gelen sahte blok okunmamalı
        p.write_text("\\chapter{X}\n" + self._meta_block(), encoding="utf-8")
        meta = parse_meta_header(p)
        assert meta == {}


# =============================================================================
# A-17 — AutoFixer iç QC çağrıları manuel bayrağı taşır
# =============================================================================


class TestA17ManualFlagForwarded:
    def test_all_internal_analyze_calls_forward_flag(self):
        src = (PROJECT_ROOT / "latex_pipeline" / "autofix.py").read_text(
            encoding="utf-8")
        calls = re.findall(r"self\.qc_engine\.analyze\([^)]*\)", src)
        assert calls, "iç analyze çağrısı bulunamadı"
        for call in calls:
            assert "manual_translation=self.manual_translation" in call, call


# =============================================================================
# A-12 (özellik) — kitaba özel preamble
# =============================================================================


class TestCustomPreamble:
    def test_extra_preamble_injected_before_begin_document(self):
        from latex_pipeline.config import PipelineConfig
        from latex_pipeline.template_engine import TemplateEngine
        engine = TemplateEngine(PipelineConfig())
        tex = engine.wrap_content("GÖVDE",
                                  extra_preamble="\\usepackage{siunitx}")
        assert "\\usepackage{siunitx}" in tex
        assert tex.index("\\usepackage{siunitx}") < tex.index("\\begin{document}")

    def test_no_preamble_no_change(self):
        from latex_pipeline.config import PipelineConfig
        from latex_pipeline.template_engine import TemplateEngine
        engine = TemplateEngine(PipelineConfig())
        tex = engine.wrap_content("GÖVDE", extra_preamble=None)
        assert "preamble_ozel" not in tex

    def test_run_pipeline_rejects_write18_preamble(self):
        src = (PROJECT_ROOT / "translate_book.py").read_text(encoding="utf-8")
        assert "preamble_ozel.tex" in src
        assert "\\\\write18" in src or "write18" in src


# =============================================================================
# B-2 — bayat final checkpoint resume edilmez
# =============================================================================


class TestB2StaleResume:
    def _translator(self):
        from latex_pipeline.config import PipelineConfig
        from latex_pipeline.translator import LatexTranslator
        return LatexTranslator(pipeline_config=PipelineConfig())

    def test_meta_records_source_sha(self, tmp_path):
        import hashlib
        lt = self._translator()
        lt._checkpoint_dir = tmp_path
        lt._source_body_sha1 = hashlib.sha1(b"KAYNAK").hexdigest()
        lt._save_checkpoint_meta()
        meta = json.loads((tmp_path / "checkpoint_meta.json").read_text())
        assert meta["source_body_sha1"] == lt._source_body_sha1

    def test_mismatched_sha_invalidates_final(self, tmp_path):
        import hashlib
        lt = self._translator()
        lt._checkpoint_dir = tmp_path
        (tmp_path / "translated_body_final.tex").write_text(
            "ESKİ ÇEVİRİ", encoding="utf-8")
        (tmp_path / "checkpoint_meta.json").write_text(json.dumps({
            "source_body_sha1": hashlib.sha1(b"ESKI KAYNAK").hexdigest(),
        }), encoding="utf-8")
        lt._source_body_sha1 = hashlib.sha1(b"YENI KAYNAK").hexdigest()
        assert lt._try_resume_from_checkpoint("YENI KAYNAK") is None

    def test_matching_sha_resumes(self, tmp_path):
        import hashlib
        lt = self._translator()
        lt._checkpoint_dir = tmp_path
        (tmp_path / "translated_body_final.tex").write_text(
            "GEÇERLİ ÇEVİRİ", encoding="utf-8")
        sha = hashlib.sha1("AYNI KAYNAK".encode()).hexdigest()
        (tmp_path / "checkpoint_meta.json").write_text(json.dumps({
            "source_body_sha1": sha,
        }), encoding="utf-8")
        lt._source_body_sha1 = sha
        assert lt._try_resume_from_checkpoint("AYNI KAYNAK") == "GEÇERLİ ÇEVİRİ"

    def test_legacy_meta_without_sha_still_resumes(self, tmp_path):
        # Eski checkpoint'lerde alan yok — geriye dönük uyum korunur
        lt = self._translator()
        lt._checkpoint_dir = tmp_path
        (tmp_path / "translated_body_final.tex").write_text(
            "ESKİ AMA GEÇERLİ", encoding="utf-8")
        (tmp_path / "checkpoint_meta.json").write_text("{}", encoding="utf-8")
        lt._source_body_sha1 = "yeni-sha"
        assert lt._try_resume_from_checkpoint("x") == "ESKİ AMA GEÇERLİ"


# =============================================================================
# B-4 / B-5 / B-6 — retranslator düzeltmeleri
# =============================================================================


class TestRetranslatorFixes:
    def test_b5_source_token_survives(self):
        from latex_pipeline.retranslator import (
            _clean_epub_placeholder_hallucinations)
        out = _clean_epub_placeholder_hallucinations(
            "Kullanıcı @Model2023 hesabından yazdı.",
            original="The user posted from @Model2023.")
        assert "@Model2023" in out

    def test_b5_hallucinated_token_removed(self):
        from latex_pipeline.retranslator import (
            _clean_epub_placeholder_hallucinations)
        out = _clean_epub_placeholder_hallucinations(
            "Çeviri metni @Bilinmeyen0000 devam ediyor.",
            original="The translation continues.")
        assert "@Bilinmeyen0000" not in out

    def test_b6_section_started_region_ends_at_next_section(self):
        from latex_pipeline.retranslator import _find_special_regions
        lines = [
            "\\section*{References}",     # 0 — bölge başlar
            "Smith, J. (2020). Paper.",   # 1 — korunur
            "\\section{Ekler}",           # 2 — bölge BİTER
            "This line is English.",      # 3 — korunmaz (retranslate edilebilir)
        ]
        _, refs = _find_special_regions(lines)
        assert 1 in refs
        assert 3 not in refs

    def test_b6_subsection_does_not_end_region(self):
        # Reviewer R6: kaynakçanın kendi \subsection'ı bölgeyi bölmemeli
        from latex_pipeline.retranslator import _find_special_regions
        lines = [
            "\\section*{References}",
            "\\subsection{Birincil kaynaklar}",
            "Smith, J. (2020). Paper.",
            "\\section{Ekler}",
        ]
        _, refs = _find_special_regions(lines)
        assert 2 in refs
        assert 3 not in refs

    def test_b4_no_append_pattern_left(self):
        src = (PROJECT_ROOT / "latex_pipeline" / "retranslator.py").read_text(
            encoding="utf-8")
        assert 'translated.rstrip() + " " + original' not in src


# =============================================================================
# B-10 — dosya hash'i kuyruk değişikliğini görür
# =============================================================================


class TestB10FileHash:
    def test_tail_change_changes_hash(self, tmp_path):
        from core.translator import get_file_hash
        p = tmp_path / "big.txt"
        base = b"x" * (3 * 1024 * 1024)
        p.write_bytes(base + b"SON-A")
        h1 = get_file_hash(str(p))
        p.write_bytes(base + b"SON-B")
        h2 = get_file_hash(str(p))
        assert h1 != h2, "1MB sonrası değişiklik hash'i değiştirmeli"

    def test_small_file_stable(self, tmp_path):
        from core.translator import get_file_hash
        p = tmp_path / "small.txt"
        p.write_bytes(b"kucuk icerik")
        assert get_file_hash(str(p)) == get_file_hash(str(p))


# =============================================================================
# B-11 — aynı dönen segment sayacı
# =============================================================================


class TestB11IdenticalCounter:
    def _lt(self):
        from latex_pipeline.config import PipelineConfig
        from latex_pipeline.translator import LatexTranslator
        return LatexTranslator(pipeline_config=PipelineConfig())

    def test_numeric_segment_counted_skipped(self):
        lt = self._lt()
        f0, s0 = lt.stats.segments_failed, lt.stats.segments_skipped
        lt._count_identical_result("42 $x$ 3.14")
        assert lt.stats.segments_failed == f0
        assert lt.stats.segments_skipped == s0 + 1

    def test_prose_segment_counted_failed(self):
        lt = self._lt()
        f0 = lt.stats.segments_failed
        lt._count_identical_result("The quick brown fox jumps over the dog.")
        assert lt.stats.segments_failed == f0 + 1


# =============================================================================
# B-1 / B-3 — timeout'lu MT çağrısı + batch progress
# =============================================================================


class TestB1B3Runtime:
    def _lt(self, translator):
        from latex_pipeline.config import PipelineConfig
        from latex_pipeline.translator import LatexTranslator
        cfg = PipelineConfig()
        cfg.segment_timeout = 1  # hızlı test
        lt = LatexTranslator(pipeline_config=cfg)
        lt._translator = translator
        return lt

    def test_b1_hung_translate_returns_none(self):
        hung = MagicMock()
        hung.translate_text = lambda *a, **k: time.sleep(30)
        lt = self._lt(hung)
        t0 = time.monotonic()
        out = lt._mt_call_with_timeout("Hello world")
        assert out is None
        assert time.monotonic() - t0 < 10  # asılmadı

    def test_b1_normal_translate_returns_result(self):
        ok = MagicMock()
        ok.translate_text = lambda text, **k: "ÇEVİRİ"
        lt = self._lt(ok)
        assert lt._mt_call_with_timeout("Hello") == "ÇEVİRİ"

    def test_b1_single_mt_call_falls_back_to_original(self):
        hung = MagicMock()
        hung.translate_text = lambda *a, **k: time.sleep(30)
        lt = self._lt(hung)
        assert lt._single_mt_call("Original text") == "Original text"

    def test_b3_batch_fires_progress_callback(self):
        bt = MagicMock()
        bt.translate_batch_protected = lambda batch: ["ÇEV: " + t for t in batch]
        lt = self._lt(bt)
        fired = []
        lt._progress_callback = lambda frac, desc: fired.append((frac, desc))
        results = lt._batch_mt_call(["one", "two", "three"])
        assert len(results) == 3
        assert fired, "batched yol progress callback ateşlemedi"
        assert any("Segment" in d for _, d in fired)


# =============================================================================
# MAP-2 / CLI — preflight registry bağlantısı + --mt-engine
# =============================================================================


class TestEngineWiring:
    def test_preflight_unknown_engine_fails_early(self):
        from translate_book import _preflight_check
        failures = _preflight_check(False, True, None,
                                    mt_engine="olmayan-motor")
        assert any("olmayan-motor" in f for f in failures)

    def test_preflight_known_engine_checks_registry_dir(self):
        from translate_book import _preflight_check
        failures = _preflight_check(False, True, None, mt_engine="hymt-7b")
        # Model diskte varsa hata yok; yoksa hata mesajı registry yolunu söyler
        for f in failures:
            if "Translation model" in f:
                assert "hymt-7b" in f

    def test_cli_has_mt_engine_flag(self):
        src = (PROJECT_ROOT / "translate_book.py").read_text(encoding="utf-8")
        assert '"--mt-engine"' in src

    def test_preflight_uses_registry_resolution(self):
        # MAP-2: sabit dizin listesi yerine registry çözümü kullanılıyor
        src = (PROJECT_ROOT / "translate_book.py").read_text(encoding="utf-8")
        assert "resolve_local_model_dir" in src
        assert '"hymt-1.8b-mlx",' not in src  # eski sabit liste elemanı yok


# =============================================================================
# MAP-5 — sessiz MockTranslator fallback kalktı: KALDIRILDI (5 Ekim 2026)
# =============================================================================
# Sınadığı core/book_translator_v2.py (BookTranslatorV2._get_translator)
# giriş noktalarından erişilmeyen ölü kod olarak silindi (KARAR 27); testlerin
# son hâli git etiketi arsiv/olu-kod-20261005 üzerinde.
