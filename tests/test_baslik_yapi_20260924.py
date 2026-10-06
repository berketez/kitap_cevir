"""24 Eylül 2026 — Yol B başlık ve yapı hataları (20 sayfalık bir test koşusu).

Gerçek koşu çıktılarının yapısını taşıyan küçük sentetik parçalarla
dört kök nedenin regresyon kilidi. Model yüklenmez; MT sahte fonksiyondur.

Kapsanan hatalar:
  H1  "## Chapter 2" + "## Başlık" (aynı düzey) -> \\section{Chapter 2}
      (converter._try_heading, sanitizer P1 savunması, title page etiketi)
  H2  "1.4 The \\textit{n}-Body Problem" -> placeholder dalı numara önekiyle
      giriyor, dil kapısı sulanıyor, restore "n} -Body" boşluğu ekliyor
  H3  Başlıklar çevrilmemiş/yarım: NER koruması Title Case başlığı yutuyor
      (core), yarım MT çıktısı kapıdan geçiyor, kelime-sınırlı glossary
      yarım sonuç üretiyor, salt-ASCII Türkçe başlık "İngilizce" sayılıyor
  H4  Retranslator 10/10 başarısız: başlık eşiği uyumsuz (2 kelime hiç
      MT'ye gitmiyor), başlıklar gövde yoluyla çevriliyor
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from latex_pipeline.config import PipelineConfig  # noqa: E402
from latex_pipeline.converter import MarkdownToLatex  # noqa: E402
from latex_pipeline.retranslator import (  # noqa: E402
    _has_english_orthography,
    _retranslate_line,
    is_english_line,
    scan_and_retranslate,
)
from latex_pipeline.sanitizer import LatexSanitizer, SanitizeReport  # noqa: E402
from latex_pipeline.translator import (  # noqa: E402
    LatexTranslator,
    TranslationStats,
    _heading_fully_translated,
    _heading_keeps_source_run,
    _smart_title_case_keep_placeholders,
)


# ---------------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------------

def _converter() -> MarkdownToLatex:
    return MarkdownToLatex(PipelineConfig(document_class="book", output_dir="/tmp/x"))


def _headings(tex: str) -> list[str]:
    return [
        ln for ln in tex.split("\n")
        if re.match(r"\\(chapter|section|subsection|subsubsection)\*?\{", ln)
    ]


def _bare_lt() -> LatexTranslator:
    t = LatexTranslator.__new__(LatexTranslator)
    t._glossary_term_map = {}
    t.stats = TranslationStats()
    t.target_lang = "tr"
    return t


class _FakeMT:
    """translate_text(text, is_heading) — çağrıları kaydeder."""

    def __init__(self, fn):
        self._fn = fn
        self.calls: list[tuple[str, bool]] = []

    def translate_text(self, text, is_heading=False):
        self.calls.append((text, is_heading))
        return self._fn(text)


def _lt_with(fn) -> LatexTranslator:
    t = _bare_lt()
    t._translator = _FakeMT(fn)
    return t


def _sanitize_chapters(body: str) -> tuple[str, SanitizeReport]:
    s = LatexSanitizer.__new__(LatexSanitizer)
    rep = SanitizeReport()
    return s._fix_marker_chapter_artifacts(body, rep), rep


# Gerçek Marker çıktısı parçalarının YAPISI: aynı başlık
# düzeyleri ve numaralar; düzyazı ve başlık metni sentetik (KARAR 26).
_MD_CH1 = (
    "## Chapter 1\n\n"
    "# Two–Body Orbital Mechanics\n\n"
    "The opening pages recall a few core ideas of Analytical Dynamics for later use.\n\n"
    "## 1.1 Roots of Orbit Theory: Newton's Laws\n\n"
    "Text of section one.\n\n"
)
_MD_CH2 = (
    "## Chapter 2\n\n"
    "## Single-Plane Description of Trajectories\n\n"
    "#### 2.1 Reference Frames\n\n"
    "A planar treatment of the trajectory is often good enough here.\n"
)


# ===========================================================================
# H1 — bölüm etiketi + başlık = tek \chapter (converter)
# ===========================================================================

class TestH1ConverterChapterLabel:
    def test_same_level_label_and_title_become_one_chapter(self):
        tex = _converter().convert(_MD_CH2)
        heads = _headings(tex)
        assert "\\chapter{Single-Plane Description of Trajectories}" in heads
        assert not any("Chapter 2" in h for h in heads), heads
        # numaralı başlık alt bölüm kalır
        assert "\\section{2.1 Reference Frames}" in heads

    def test_different_level_pattern_chapter1_unchanged(self):
        # 1. bölüm örüntüsü (etiket ## + başlık #): eskiden başlık sayfası
        # "Chapter 1"i yutuyordu; artık etiket gövdede tanınır, sonuç aynı.
        tex = _converter().convert(_MD_CH1)
        heads = _headings(tex)
        assert heads.count("\\chapter{Two–Body Orbital Mechanics}") == 1
        assert not any("Chapter 1" in h for h in heads), heads
        assert "\\begin{titlepage}" not in tex
        # gerçek bölüm: içindekiler + mainmatter geçişi tetiklenmeli
        assert "\\tableofcontents" in tex and "\\mainmatter" in tex

    def test_both_chapters_from_real_fragments(self):
        tex = _converter().convert(_MD_CH1 + _MD_CH2)
        chapters = [h for h in _headings(tex) if h.startswith("\\chapter")]
        assert chapters == [
            "\\chapter{Two–Body Orbital Mechanics}",
            "\\chapter{Single-Plane Description of Trajectories}",
        ], chapters
        assert "\\section{Chapter" not in tex

    def test_label_followed_by_numbered_section_not_promoted(self):
        # Adsız bölüm + numaralı alt bölüm: alt bölüm chapter'a TERFİ ETMEZ
        tex = _converter().convert("# Chapter 2\n\n## 2.1 Reference Frames\n\nMetin.\n")
        assert "\\chapter{2.1 Reference Frames}" not in tex
        assert "\\chapter*{2.1 Reference Frames}" not in tex
        # (ön madde durumunda yıldızlı olabilir — eski davranış, kilitlenmez)
        assert re.search(r"\\section\*?\{2\.1 Reference Frames\}", tex)
        # etiket sessizce yutulmaz
        assert "Chapter 2" in tex

    def test_label_followed_by_body_text_not_swallowed(self):
        tex = _converter().convert("## Chapter 2\n\nBody text follows here.\n")
        assert "Chapter 2" in tex

    def test_two_consecutive_labels_not_merged(self):
        tex = _converter().convert("## Chapter 2\n\n## Chapter 3\n\n## Title Here\n\nMetin.\n")
        # ikinci etiket başlık sayılmaz; başlık yine chapter olur
        assert "\\chapter{Chapter 3}" not in tex
        assert "\\chapter{Title Here}" in _headings(tex)

    def test_word_numeral_chapter_headings_unchanged(self):
        # "# Chapter One" etiket DEĞİL: eskisi gibi ilk # başlık olarak
        # işlenir (başlık sayfası), yutulmaz (#4 dedup testi davranışı korunur)
        tex = _converter().convert("# Chapter One\n\ntext a\n\n## Exercises\n\nsoru 1\n")
        assert "Chapter One" in tex and "Exercises" in tex
        assert getattr(_converter(), "_pending_chapter_label", None) is None

    def test_title_page_rejects_bare_chapter_label(self):
        c = _converter()
        result, consumed = c._build_title_page(["## Chapter 1", "", "# Two–Body Orbital Mechanics"])
        assert result == "" and consumed == 0

    def test_title_page_still_built_for_real_title(self):
        c = _converter()
        result, _ = c._build_title_page(["# Celestial Navigation Lecture Notes", "", "Intro."])
        assert "\\begin{titlepage}" in result


# ===========================================================================
# H1 — sanitizer P1 savunma hattı (eski body.tex / EXPORT.tex için)
# ===========================================================================

class TestH1SanitizerLabelSection:
    # Yapı: translated_body.tex satır 14 ve 449-451 (gerçek çıktı); ikinci
    # bölümün Türkçe başlığı sentetik (KARAR 26).
    _BODY = (
        "\\chapter{İki–Body Orbital Mechanics}\n\nMetin.\n\n"
        "\\section{Bölüm 2}\n\n"
        "\\section{Hareketin Düzlemsel Betimi}\n\n"
        "\\section{Reference Frames}\n"
    )

    def test_label_section_then_same_level_title_merged(self):
        out, rep = _sanitize_chapters(self._BODY)
        assert "\\chapter{Hareketin Düzlemsel Betimi}" in out
        assert "\\section{Bölüm 2}" not in out
        assert "\\section{Reference Frames}" in out
        assert rep.headers_cleaned >= 1
        # Denetim 2026-09-24 (M3#2): birleştirmeyle yaratılan chapter, kısa
        # ilk gerçek bölümü "başlık sayfası" sanıp \chapter* yapmamalı.
        assert "\\chapter{İki–Body Orbital Mechanics}" in out
        assert "\\chapter*" not in out

    def test_english_label_variant(self):
        out, _ = _sanitize_chapters(
            "\\chapter{A}\n\n\\section{Chapter 2}\n\n\\section{Single-Plane Description of Trajectories}\n"
        )
        assert "\\chapter{Single-Plane Description of Trajectories}" in out
        assert "Chapter 2" not in out

    def test_no_chapter_in_document_drops_label_keeps_section(self):
        # article benzeri belge: \chapter derlenmez -> etiket düşer, başlık kalır
        out, _ = _sanitize_chapters(
            "\\section{Chapter 2}\n\n\\section{Single-Plane Description of Trajectories}\n\nMetin.\n"
        )
        assert "\\chapter" not in out
        assert "\\section{Single-Plane Description of Trajectories}" in out
        assert "Chapter 2" not in out

    def test_label_section_before_chapter_removed(self):
        out, _ = _sanitize_chapters(
            "\\section{Chapter 1}\n\n\\chapter{Two–Body Orbital Mechanics}\n\nMetin.\n"
        )
        assert "\\section{Chapter 1}" not in out
        assert "\\chapter{Two–Body Orbital Mechanics}" in out

    def test_numbered_title_after_label_not_merged(self):
        body = "\\chapter{Giriş}\n\\section{Chapter 2}\n\n\\section{2.1 Reference Frames}\n"
        out, _ = _sanitize_chapters(body)
        assert "\\chapter{2.1 Reference Frames}" not in out
        assert "\\section{2.1 Reference Frames}" in out

    def test_cross_level_duplicate_pattern_untouched(self):
        # EPUB örüntüsü (\section{X} + \chapter{X}) bu kuralın işi değil:
        # yeni etiket kuralları yalnız "Chapter N"/"Bölüm N" metnine bakar.
        body = "\\section{Giriş}\n\\chapter{Giriş}\n\nMetin.\n\\chapter{İkinci}\n"
        out, _ = _sanitize_chapters(body)
        assert "\\section{Giriş}" in out
        assert re.search(r"\\chapter\*?\{Giriş\}", out)  # A-11 yıldızlayabilir
        assert "\\chapter{İkinci}" in out

    def test_spurious_chapter_label_rule_still_active(self):
        out, _ = _sanitize_chapters("\\chapter{Chapter 2}\n\\chapter{Gerçek Başlık}\nMetin.\n")
        assert "\\chapter{Chapter 2}" not in out
        assert "\\chapter{Gerçek Başlık}" in out


# ===========================================================================
# H3 — core: başlıkta NER ve bellek devre dışı
# ===========================================================================

def _make_bt(**cfg):
    from core.translator import BookTranslator, TranslationConfig
    bt = BookTranslator.__new__(BookTranslator)
    bt.config = TranslationConfig(**cfg)
    bt.memory = None
    bt.glossary = None
    bt.ner_protector = None
    bt.naturalizer = None
    bt.ai_client = None
    bt.qa_checker = None
    bt.consistency_analyzer = None
    bt._dict_loader = None
    bt.mt_model = object()
    bt.mt_tokenizer = object()
    bt.splitter = MagicMock()
    bt._use_mlx = False
    bt.stats = defaultdict(int)
    return bt


class TestH3CoreHeadingProtection:
    def test_ner_swallows_title_case_heading_in_body_mode(self):
        # Kök neden kanıtı: NER sezgisi Title Case başlığı özel isim sanır.
        # 2026-09-29 (V2 denetimi; bug avı F3-04): sezginin adayları artık
        # varsayılanda MT'den saklanmıyor; eski saklama davranışı
        # hide_unknown=True ile sınanır (mekanizma kanıtı korunur).
        from core.ner_protector import NERProtector
        protected, mapping = NERProtector(hide_unknown=True).protect("Potential Energy")
        assert protected.startswith("@@") and mapping
        # Varsayılan (F3-04): Title Case öbek modele görünür gider.
        protected, mapping = NERProtector().protect("Potential Energy")
        assert protected == "Potential Energy" and not mapping

    def test_heading_mode_bypasses_ner(self):
        from core.ner_protector import NERProtector
        bt = _make_bt(book_type="science", glossary_mode="post")
        bt.ner_protector = NERProtector()
        bt._translate_with_mt = MagicMock(return_value="Potansiyel Enerji")
        out = bt.translate_text("Potential Energy", is_heading=True)
        sent = bt._translate_with_mt.call_args[0][0]
        assert "@@" not in sent and sent == "Potential Energy"
        assert out == "Potansiyel Enerji"

    def test_body_mode_still_applies_ner(self):
        from core.ner_protector import NERProtector
        bt = _make_bt(book_type="science", glossary_mode="post")
        # 2026-09-29 (V2 denetimi; F3-04): gövde yolunda NER'in UYGULANDIĞI
        # mekanizma, adayları saklayan kipte sınanır (varsayılan artık saklamaz).
        bt.ner_protector = NERProtector(hide_unknown=True)
        bt._translate_with_mt = MagicMock(side_effect=lambda t, is_heading=False: t)
        bt.translate_text("Potential Energy", is_heading=False)
        sent = bt._translate_with_mt.call_args[0][0]
        assert "@@" in sent  # gövde davranışı değişmedi

    def test_heading_mode_bypasses_memory_read_and_write(self):
        bt = _make_bt(book_type="science", glossary_mode="post")
        bt.memory = MagicMock()
        # zehirli kayıt (bir test koşusunda gerçekten oluştu)
        bt.memory.get_or_similar = MagicMock(
            return_value=("Tek Nokta Kütlenin Hareket Denklemi", "exact"))
        bt._translate_with_mt = MagicMock(return_value="Bir Nokta Kütle Çifti için Temel Denklemler")
        out = bt.translate_text("Governing Equations for a Pair of Point Masses", is_heading=True)
        assert out == "Bir Nokta Kütle Çifti için Temel Denklemler"
        bt.memory.get_or_similar.assert_not_called()
        bt.memory.add.assert_not_called()

    def test_body_mode_memory_still_used(self):
        bt = _make_bt(book_type="science", glossary_mode="post")
        bt.memory = MagicMock()
        bt.memory.get_or_similar = MagicMock(return_value=("ÖNBELLEK", "exact"))
        bt._translate_with_mt = MagicMock(return_value="MT")
        assert bt.translate_text("A sentence long enough to be cached here.") == "ÖNBELLEK"


# ===========================================================================
# H3 — latex_pipeline: kabul kapıları ve dil dedektörü
# ===========================================================================

class TestH3HeadingGates:
    def test_half_translated_mt_output_rejected(self):
        # translation_memory.db'de gerçekten kayıtlı yarım çıktı
        t = _lt_with(lambda s: "İki–Body Orbital Mechanics")
        out = t._translate_heading_text("Two–Body Orbital Mechanics")
        assert out != "İki–Body Orbital Mechanics"
        assert out == "Two–Body Orbital Mechanics"  # düşük-güven: orijinal

    def test_wb_glossary_fallback_never_yields_partial(self):
        # echo MT -> eskiden 'Perifocal Reference Çerçeve' üretiliyordu
        t = _lt_with(lambda s: s)
        out = t._translate_heading_text("Perifocal Reference Frame")
        assert "Çerçeve" not in out
        assert out == "Perifocal Reference Frame"

    @pytest.mark.parametrize("src,tr", [
        ("Potential Energy", "Potansiyel Enerji"),
        ("Conic Sections", "Konik Kesitler"),
        ("Trajectory Equation", "Yörünge Denklemi"),
        ("Perifocal Reference Frame", "Perifokal Referans Çerçevesi"),
        ("Two–Body Orbital Mechanics", "İki Cisimli Yörünge Mekaniği"),
    ])
    def test_full_turkish_mt_accepted(self, src, tr):
        # Deneyde HY-MT'nin gerçekten verdiği çeviriler kapıdan geçmeli
        t = _lt_with(lambda s, _tr=tr: _tr)
        assert t._translate_heading_text(src) == tr

    def test_keeps_source_run_helper(self):
        assert _heading_keeps_source_run("Two–Body Orbital Mechanics", "İki–Body Orbital Mechanics")
        assert _heading_keeps_source_run(
            "Relating Energy and Semi-major Axis", "Relating Energy ve Yarı-Büyük Eksen") is False
        # 2'li özel ad meşru
        assert _heading_keeps_source_run("Monte Carlo Methods", "Monte Carlo Yöntemleri") is False
        assert _heading_keeps_source_run("Kepler's Laws", "Kepler Yasaları") is False

    def test_fully_translated_helper(self):
        assert _heading_fully_translated("Perifocal Reference Frame", "Perifocal Reference Çerçeve") is False
        assert _heading_fully_translated("Introduction", "Giriş") is True
        assert _heading_fully_translated("Introduction and Summary", "Giriş and Özet") is False

    @pytest.mark.parametrize("title", [
        "Potansiyel Enerji", "Konik Kesitler", "Yorunge Denklemi",
        "Hareketin Sabitleri", "Referans Cerceveleri", "Kepler Yasalari",
        "Matris Tersi", "Lineer Cebir", "Kuantum Mekanigi",
    ])
    def test_ascii_turkish_heading_not_english(self, title):
        assert is_english_line(title, is_heading=True) is False
        assert is_english_line(f"\\section{{{title}}}", is_heading=True) is False

    @pytest.mark.parametrize("title", [
        "Potential Energy", "Trajectory Equation", "Conic Sections",
        "Reference Frames", "Cosmic Velocities", "Flight Path Angle",
        "Elliptical Orbits", "The n-Body Problem",
        "Circular and Escape Velocity, Hyperbolic Excess Speed",
        "Perifocal Reference Frame", "Relating Energy and Semi-major Axis",
        "Geometry of an Elliptical Orbit", "Constants of the Motion",
        "Single-Plane Description of Trajectories", "Period of an Elliptical Orbit",
        "Kepler's Laws", "Time-of-Flight on the Elliptical Orbit",
    ])
    def test_english_heading_still_detected(self, title):
        assert is_english_line(title, is_heading=True) is True
        # \label kuyruğu başlık metnine karışmamalı (parantez-dengeli ayrıştırma)
        assert is_english_line(f"\\section{{{title}}}\\label{{s}}", is_heading=True) is True

    def test_known_detector_limits_documented(self):
        # Sözlüksüz dedektörün bilinen sınırları — kilit değil, belge:
        # özel ad + tek sade kelime ("Heat Transfer", "Laplace Transform")
        # yazım izi taşımadığı için yakalanmaz; QC'nin kendi başlık kontrolü
        # bunları ayrıca raporlar. Türkçe tarafta "Cauchy" (ch) tek yanlış pozitif.
        assert is_english_line("Fourier Analizi", is_heading=True) is False
        assert is_english_line("Dairesel ve Kacis Hizi", is_heading=True) is False

    def test_orthography_helper(self):
        for w in ("sections", "path", "potential", "frames", "reference",
                  "energy", "trajectory", "conic", "axis"):
            assert _has_english_orthography(w), w
        for w in ("potansiyel", "enerji", "matris", "lineer", "kesitler",
                  "daire", "kozmik", "referans", "bence", "yasalari"):
            assert not _has_english_orthography(w), w


# ===========================================================================
# H2 — placeholder'lı numaralı başlık + restore boşluğu
# ===========================================================================

class TestH2PlaceholderHeading:
    def test_numbered_placeholder_heading_via_heading_authority(self):
        t = _lt_with(lambda s: "<<LATEX_0>>-Cisim Problemi")
        out = t._translate_structured("\\section{1.4 The <<LATEX_0>>-Body Problem}")
        assert out == "\\section{1.4 <<LATEX_0>>-Cisim Problemi}"
        # numara öneki MT'ye gitmez, başlık modu açık
        assert t._translator.calls == [("The <<LATEX_0>>-Body Problem", True)]

    def test_english_result_with_placeholder_not_accepted(self):
        # Eski dal: "1.4" oranı sulandırıyor, İngilizce kabul ediliyordu
        t = _lt_with(lambda s: s)
        out = t._translate_structured("\\section{1.4 The <<LATEX_0>>-Body Problem}")
        assert out == "\\section{1.4 The <<LATEX_0>>-Body Problem}"

    def test_placeholder_loss_reverts_to_original(self):
        t = _lt_with(lambda s: "Cisim Problemi")
        out = t._translate_structured("\\section{1.4 The <<LATEX_0>>-Body Problem}")
        assert "<<LATEX_0>>" in out
        assert out == "\\section{1.4 The <<LATEX_0>>-Body Problem}"

    def test_restore_no_space_before_hyphen(self):
        t = _bare_lt()
        out = t._restore_stash("The <<LATEX_0>>-Body Problem", ["\\textit{n}"])
        assert out == "The \\textit{n}-Body Problem"
        out2 = t._restore_stash("$x$<<LATEX_0>>-ekseni", ["\\ref{a}"])
        assert "\\ref{a}-ekseni" in out2

    def test_restore_spacing_guarantee_kept_for_letters(self):
        t = _bare_lt()
        out = t._restore_stash("once<<LATEX_0>>sonra", ["$x^2$"])
        assert "once $x^2$ sonra" in out

    def test_all_caps_title_case_keeps_placeholders(self):
        out = _smart_title_case_keep_placeholders("THE <<LATEX_0>>-BODY PROBLEM")
        assert "<<LATEX_0>>" in out and "<<Latex_0>>" not in out
        assert out == "The <<LATEX_0>>-Body Problem"


# ===========================================================================
# H4 — retranslator başlık yolu
# ===========================================================================

_TR_PARA = (
    "Bu bölümde hareket denklemleri Türkçe olarak anlatılır ve yeterince "
    "uzun bir paragraftır burada.\n"
)


class TestH4Retranslator:
    def test_two_word_heading_reaches_translator_without_heading_fn(self):
        seen: list[str] = []

        def tr(text):
            seen.append(text)
            return "Potansiyel Enerji"

        res = scan_and_retranslate("\\section{Potential Energy}\n\n" + _TR_PARA, translator_fn=tr)
        assert "Potential Energy" in seen  # eskiden 15/3 eşiğinde hiç gitmiyordu
        assert res.retranslated_lines == 1
        assert "\\section{Potansiyel Enerji}" in res.output_tex

    def test_heading_fn_used_and_body_fn_not_called_for_heading(self):
        body_calls: list[str] = []
        head_calls: list[str] = []

        def body_fn(t):
            body_calls.append(t)
            return t

        def head_fn(t):
            head_calls.append(t)
            return {"Potential Energy": "Potansiyel Enerji"}.get(t, t)

        res = scan_and_retranslate(
            "\\section{Potential Energy}\n\n" + _TR_PARA,
            translator_fn=body_fn, heading_translator_fn=head_fn,
        )
        assert head_calls == ["Potential Energy"]
        assert body_calls == []
        assert res.retranslated_lines == 1 and res.failed_lines == 0
        assert "\\section{Potansiyel Enerji}" in res.output_tex

    def test_heading_fn_keeps_number_prefix_and_label(self):
        res = scan_and_retranslate(
            "\\section{2.4 Perifocal Reference Frame}\\label{sec:pf}\n\n" + _TR_PARA,
            translator_fn=lambda t: t,
            heading_translator_fn=lambda t: "Perifokal Referans Çerçevesi",
        )
        assert "\\section{2.4 Perifokal Referans Çerçevesi}\\label{sec:pf}" in res.output_tex

    def test_heading_fn_protects_textit_and_restores_it(self):
        calls: list[str] = []

        def head_fn(t):
            calls.append(t)
            return "<<LATEX_0>>-Cisim Problemi"

        res = scan_and_retranslate(
            "\\section{The \\textit{n}-Body Problem}\n\n" + _TR_PARA,
            translator_fn=lambda t: t, heading_translator_fn=head_fn,
        )
        assert calls == ["The <<LATEX_0>>-Body Problem"]
        assert "\\textit{n}-Cisim Problemi}" in res.output_tex
        assert res.retranslated_lines == 1

    def test_heading_fn_placeholder_loss_keeps_original(self):
        res = scan_and_retranslate(
            "\\section{The \\textit{n}-Body Problem}\n\n" + _TR_PARA,
            translator_fn=lambda t: t, heading_translator_fn=lambda t: "Cisim Problemi",
        )
        assert "\\section{The \\textit{n}-Body Problem}" in res.output_tex
        assert res.retranslated_lines == 0 and res.failed_lines == 1

    def test_heading_fn_echo_counts_as_failure_not_crash(self):
        res = scan_and_retranslate(
            "\\section{Cosmic Velocities}\n\n" + _TR_PARA,
            translator_fn=lambda t: t, heading_translator_fn=lambda t: t,
        )
        assert res.english_lines_found == 1
        assert res.retranslated_lines == 0 and res.failed_lines == 1

    def test_astro_headings_batch(self):
        # translated_body.tex'teki 10 İngilizce başlıktan 8'i (deneydeki HY-MT
        # çevirileriyle) — eski koşuda 10/10 başarısızdı
        heads = {
            "Potential Energy": "Potansiyel Enerji",
            "Trajectory Equation": "Yörünge Denklemi",
            "Conic Sections": "Konik Kesitler",
            "Reference Frames": "Referans Çerçeveleri",
            "Flight Path Angle": "Uçuş Yolu Açısı",
            "Elliptical Orbits": "Elips Biçiminde Yörüngeler",
            "Circular and Escape Velocity, Hyperbolic Excess Speed":
                "Çembersel Hız ve Kaçış Hızı, Hiperbolik Artık Hız",
            "Cosmic Velocities": "Kozmik Hızlar",
        }
        body = "".join(f"\\section{{{h}}}\n\n{_TR_PARA}\n" for h in heads)
        res = scan_and_retranslate(
            body, translator_fn=lambda t: t, heading_translator_fn=lambda t: heads.get(t, t),
        )
        assert res.english_lines_found == len(heads)
        assert res.retranslated_lines == len(heads), res.changes
        assert res.failed_lines == 0
        for tr in heads.values():
            assert f"\\section{{{tr}}}" in res.output_tex

    def test_retranslate_line_heading_shell_still_preserved(self):
        out = _retranslate_line(
            "\\section{The \\textit{n}-Body Problem}\\label{x}",
            lambda t: "CEVIRI(" + t + ")",
        )
        assert out.startswith("\\section{") and out.endswith("\\label{x}")
        assert out.count("{") == out.count("}")
