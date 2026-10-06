"""Denetim 24 Eylül 2026 — kulvar C2: başlık kapısı ve motor varsayılanı (Yol B).

Kaynak: 24 Eylül denetimi (iç denetim notu).

  C2a  Mercek 2 #2: başlık dil kapısının yazım izi şartı izsiz gerçek İngilizce
       başlıkları ("Time is Gold", "Jupiter and Saturn") görünmez kılıyordu;
       kabul kapısındaki tam eşitlik yankısı yakın yankıyı ('Time is Gold.',
       'Solar System', 'Star Grain.') çeviri sanıyordu.
  C2b  Mercek 2 #5: _AUTHOR_YEAR_RE engeli yalnız küçük harfli iyelik ve
       tanımlıkları kapsıyordu ("His Principia (1687)", "Darwin's Origin (1859)").
  C2c  Mercek 3 #4: motor varsayılanları core.mt_engines.registry.DEFAULT_ENGINE
       yerine "hymt-7b" literaliydi.

Başlık metinleri output/ altındaki gerçek koşulardan alınmış kısa başlıklardır;
gövde metni kopyalanmadı. Model yüklenmez, MT sahte fonksiyondur.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.mt_engines import registry as mt_registry  # noqa: E402
from core.translator import BookTranslator, TranslationConfig  # noqa: E402
from latex_pipeline import retranslator as retranslator_mod  # noqa: E402
from latex_pipeline.config import PipelineConfig  # noqa: E402
from latex_pipeline.retranslator import (  # noqa: E402
    _heading_has_english_word,
    is_english_line,
)
from latex_pipeline.translator import (  # noqa: E402
    _AUTHOR_YEAR_RE,
    LatexTranslator,
    TranslationStats,
    _heading_is_echo,
    _heading_keeps_source_words,
)


# ---------------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------------

def _bare_lt() -> LatexTranslator:
    t = LatexTranslator.__new__(LatexTranslator)
    t._glossary_term_map = {}
    t.stats = TranslationStats()
    t.target_lang = "tr"
    return t


class _FakeMT:
    """translate_text(text, is_heading) — sabit ya da fonksiyonla yanıt verir."""

    def __init__(self, fn):
        self._fn = fn
        self.calls: list[tuple[str, bool]] = []

    def translate_text(self, text, is_heading=False):
        self.calls.append((text, is_heading))
        return self._fn(text)


def _heading_via(mt_output: str, source: str) -> str:
    t = _bare_lt()
    t._translator = _FakeMT(lambda _text: mt_output)
    return t._translate_heading_text(source)


# ---------------------------------------------------------------------------
# C2a — is_english_line başlık dalı: yazım izi şartı yumuşatıldı
# ---------------------------------------------------------------------------

# Eski kodun (7f8632a) İngilizce saydığı, yazım izi şartının (324d225)
# kaçırdığı 26 başlıktan kapanan 20'si (output/ ölçümü, 24 Eyl 2026).
_RECOVERED_EN_HEADINGS = [
    r"\subsection{Multi-robot Fleet Control}",
    r"\subsubsection{Stag Hunt Game}",
    r"\section{Volume IV - Harvest}",
    r"\section{Climb Above}",
    r"\section{Birds Like Wind}",
    r"\chapter{Birds Like Wind}",
    r"\section{Wind Like Birds}",
    r"\section{Time is Gold}",
    r"\chapter{Time is Gold}",
    r"\section{Time is Patience}",
    r"\section{Patience and Practice}",
    r"\section{Also by Dario Fenassi}",
    r"\chapter{Also by Dario Fenassi}",
    r"\subsubsection{6.4.2.1 Liquid Fuel \(Feed\)}",
    r"\section*{7.3 Mars and Venus}",
    r"\section*{7.5 Mars, Venus, and a Test Body}",
    r"\section*{MLP module}",
]

# Yazım izi kuralının (324d225) doğru yakaladığı ve eski kodun kaçırdığı 24
# başlığın temsilcileri — yumuşatma bu kazancı bozmamalı.
_KEPT_GAIN_EN_HEADINGS = [
    r"\subsection{A Point of Contrast: Signal vs. Noise}",
    r"\section{Dawn of a New Season}",
    r"\section{Keepers of a Quiet Harbor}",
    r"\section{Hail to the Q}",
    r"\section{A Need to Wander}",
    r"\section{Why I Believe in Rain}",
    r"\section{3.3 FLOW EQUATION OF WATER PUMPS}",
    r"\subsection{3.3.3 Variation of Flow with Pressure}",
    r"\subsection{3.3.4 Effect of Blade Angle on Flow}",
    r"\subsection{3.4.8 Power-to-Weight Ratio}",
    r"\section{4.7 LOSSES IN PUMP CASING}",
    r"\section{4.8 PERFORMANCE OF INLET VALVE}",
    r"\section{5.2 FORCES ACTING ON A BRIDGE}",
    r"\section{7.5 MEASUREMENT OF MATERIAL WEAR/EROSION RATE}",
    r"\subsection{7.7.1 Ignition of a Gas Burner}",
    r"\section{APPENDIX A}",
    r"\section{1.1 Birth of Hydraulics: Pascal's Laws}",
    r"\section{1.2 Hooke's Law of Elasticity}",
    r"\section{1.3 Ohm's Law of Electrical Resistance}",
    r"\subsection{2.6.1 Geometry of a Circular Track}",
    r"\subsection{2.6.2 Period of a Circular Track}",
]

# Salt-ASCII Türkçe başlıklar (yazım izi şartının asıl amacı): İngilizce değil.
_ASCII_TURKISH_HEADINGS = [
    r"\section{1.6 Potansiyel Enerji}",
    r"\section{1.9 Konik Kesitler}",
    r"\section{1.8 Yorunge Denklemi}",
    r"\subsection{Oyun Teorisi Modelleri ve Hasmane Riskler}",
    r"\subsection{Heterojen Maruziyet Etkilerinin Parametrik Olmayan Analizi}",
    r"\subsection{Bireysel Risklerdeki Heterojenlik}",
    r"\section{2.4 Tepkime Sistemi}",
    r"\section{8.2 TEMEL YAPILANDIRMA}",
    r"\section{10.9 TURBO-POMPA Besleme Sistemi}",
    # İngilizce listelerde de geçen Türkçe sözcükler kanıt sayılmaz
    # (_TR_ASCII_HOMOGRAPHS, _TECHNICAL_TERMS).
    r"\section{Spiral Yaylar}",
    r"\section{Spiral Yayin Dinamigi}",
    r"\section{Transfer Fonksiyonu}",
    r"\section{Can ve Ali}",
    r"\section{Not Defteri}",
    r"\section{General Kurmay}",
    r"\section{Momentum Denklemi}",
    r"\section{Model Parametreleri}",
]


class TestC2aHeadingGate:
    @pytest.mark.parametrize("line", _RECOVERED_EN_HEADINGS)
    def test_orthography_free_english_heading_detected(self, line):
        assert is_english_line(line, is_heading=True), line

    @pytest.mark.parametrize("line", _KEPT_GAIN_EN_HEADINGS)
    def test_orthography_gain_preserved(self, line):
        assert is_english_line(line, is_heading=True), line

    @pytest.mark.parametrize("line", _ASCII_TURKISH_HEADINGS)
    def test_ascii_turkish_heading_not_english(self, line):
        assert not is_english_line(line, is_heading=True), line
        # QC başlık kontrolünün eşikleriyle de (kulvar B)
        assert not is_english_line(line, min_words=1, min_length=4,
                                   is_heading=True), line

    @pytest.mark.parametrize("line", _RECOVERED_EN_HEADINGS[:6])
    def test_recovered_with_qc_thresholds(self, line):
        assert is_english_line(line, min_words=1, min_length=4, is_heading=True)

    # Kulvar B (QC başlık kontrolü) bu beş örneğe dayanıyor.
    @pytest.mark.parametrize("title, expected", [
        (r"\textit{n}-Cisim Problemi", False),
        ("Potansiyel Enerji", False),
        ("Konik Kesitler", False),
        ("Newton's Laws of Motion", True),
        ("Period of an Elliptical Orbit", True),
    ])
    @pytest.mark.parametrize("wrap", ["{}", r"\section{{{}}}", r"\subsection{{1.4 {}}}"])
    def test_qc_contract_examples(self, title, expected, wrap):
        line = wrap.format(title)
        assert is_english_line(line, is_heading=True) is expected, line

    @pytest.mark.parametrize("words, expected", [
        (["Love", "is", "God"], True),        # iki harfli, Türkçede olmayan
        (["Jupiter", "and", "Saturn"], True),  # işlev sözcüğü
        (["Birds", "Like", "Wind"], True),     # yalnız başlıkta sayılan "like"
        (["Stag", "Hunt", "Game"], True),      # bilinen içerik sözcüğü
        (["Son", "An"], False),                # "an" Türkçe sözcük
        (["On", "Iki", "Ay"], False),          # "on" Türkçe sayı
        (["Potansiyel", "Enerji"], False),
        (["Spiral", "Yaylar"], False),         # Türkçede de sözcük
        (["Transfer", "Fonksiyonu"], False),
        (["Model", "Secimi"], False),          # teknik terim
    ])
    def test_word_evidence(self, words, expected):
        assert _heading_has_english_word(words) is expected

    def test_body_path_unchanged(self):
        # Gövde kapısı bu değişiklikten etkilenmez: izsiz, işlev sözcüksüz
        # ASCII satır gövdede İngilizce sayılmaz (öncekiyle aynı).
        assert not is_english_line("Birisi bunu elde eder ve devam eder.")


# ---------------------------------------------------------------------------
# C2a — kabul kapısı: yakın yankı ve kısa başlık yarım çevirisi
# ---------------------------------------------------------------------------

class TestC2aAcceptanceGate:
    @pytest.mark.parametrize("source, mt", [
        ("Time is Gold", "Time is Gold."),
        ("The Solar System", "Solar System"),
        ("Star Grain", "Star Grain."),
        ("Stag Hunt Game", "stag hunt game"),
        ("Birds Like Wind", "BIRDS LIKE WIND!"),
    ])
    def test_near_echo_rejected_source_kept(self, source, mt):
        assert _heading_via(mt, source) == source

    @pytest.mark.parametrize("source, mt", [
        ("Potential Energy", "Potansiyel Enerji"),
        ("Conic Sections", "Konik Kesitler"),
        ("Jupiter and Saturn", "Jüpiter ve Satürn"),
        ("Period of an Elliptical Orbit", "Eliptik Yörüngenin Periyodu"),
    ])
    def test_real_translation_accepted(self, source, mt):
        assert _heading_via(mt, source) == mt

    def test_single_word_article_drop_is_translation(self):
        # 'The Model' -> 'Model' Türkçede de doğru; yankı sayılmaz.
        assert _heading_via("Model", "The Model") == "Model"

    @pytest.mark.parametrize("source, mt", [
        ("Droplet Size Distribution", "Droplet Size Dağılım"),
        ("Hypergolic Propellant Combustion", "Hypergolic Propellant Yanma"),
        ("Control of Combustion Instability", "Control Yanma Instability"),
        ("Hall Effect Thruster", "Hall Effect İtme Kuvveti"),
        ("Potential Energy", "Potential Energy'nin"),
    ])
    def test_short_heading_half_translation_rejected(self, source, mt):
        assert _heading_via(mt, source) == source

    @pytest.mark.parametrize("source, mt", [
        ("Navier-Stokes Equations", "Navier-Stokes Denklemleri"),
        ("GPU CUDA Programming", "GPU CUDA Programlama"),
        ("Fourier Transform", "Fourier Dönüşümü"),
    ])
    def test_acronyms_and_eponyms_may_survive(self, source, mt):
        assert _heading_via(mt, source) == mt

    def test_glossary_resolved_words_do_not_count_as_kept(self):
        # Kelime-sınırlı sözlük 'Solid Propellant'ı Türkçeleştiriyor; kalan
        # kaynak kelime kalmadığı için MT sonucu kabul edilir.
        out = _heading_via("Solid Propellant’nın Ateşlenmesi",
                           "Ignition of a Solid Propellant")
        assert out != "Ignition of a Solid Propellant"
        assert "Katı İtici" in out
        assert "Solid" not in out and "Propellant" not in out

    @pytest.mark.parametrize("source, result, expected", [
        ("Time is Gold", "Time is Gold.", True),
        ("The Solar System", "Solar System", True),
        ("4.2.1 The Model", "4.2.1 Model", False),
        ("The Model", "Model", False),
        ("<<LATEX_0>> The Model", "<<LATEX_0>> Model", False),
        ("Introduction", "Introduction.", True),
        ("Potential Energy", "Potansiyel Enerji", False),
        ("Kepler's Laws", "KEPLER'S LAWS", True),
    ])
    def test_echo_helper(self, source, result, expected):
        assert _heading_is_echo(source, result) is expected

    @pytest.mark.parametrize("source, result, expected", [
        ("Droplet Size Distribution", "Droplet Size Dağılım", True),
        ("Control of Combustion Instability", "Control Yanma Instability", True),
        ("Star Grain", "Yıldız Grain", False),                 # tek kelime kaldı
        ("Two Body Orbital Mechanics", "İki Body Orbital Mekaniği", False),  # 4 kelime: 3'lü kapının işi
        ("Navier-Stokes Equations", "Navier-Stokes Denklemleri", False),
        ("MLP Module Design", "MLP Module Tasarımı", False),   # MLP kısaltma, 1 kelime kalır
    ])
    def test_keeps_source_words_helper(self, source, result, expected):
        assert _heading_keeps_source_words(source, result) is expected


# ---------------------------------------------------------------------------
# C2b — _AUTHOR_YEAR_RE
# ---------------------------------------------------------------------------

class TestC2bAuthorYear:
    @pytest.mark.parametrize("text", [
        "His Principia (1687) was",
        "Her Principia (1687) was",
        "Their Elements (1900) were",
        "Its Principia (1687) was",
        "THE Smith (2015) study",
        "In Darwin's Origin (1859) the",
        "In Darwin’s Origin (1859) the",
        "his Principia (1687)",
        "the Smith (2015)",
        "In Book I of his Principia (1687)",
        "his LeMarc (2015)",   # kelime ortasından 'Marc (2015)' eşleşmesi de yok
    ])
    def test_work_title_not_citation(self, text):
        assert _AUTHOR_YEAR_RE.search(text) is None, text

    @pytest.mark.parametrize("text, match", [
        ("LeMarc (2015)", "LeMarc (2015)"),
        ("Smith (2015)", "Smith (2015)"),
        ("as shown by LeMarc (2015) in", "LeMarc (2015)"),
        ("Kovarsky (2012)", "Kovarsky (2012)"),
        ("O'Brien (2010) argued", "O'Brien (2010)"),
        ("Kepler's laws. Smith (2015) showed", "Smith (2015)"),
        ("Thesis Smith (2015)", "Smith (2015)"),
    ])
    def test_bare_citation_kept(self, text, match):
        m = _AUTHOR_YEAR_RE.search(text)
        assert m is not None and m.group(0) == match


# ---------------------------------------------------------------------------
# C2c — motor varsayılanı tek kaynak: registry.DEFAULT_ENGINE
# ---------------------------------------------------------------------------

@pytest.fixture
def patched_default(monkeypatch):
    monkeypatch.setattr(mt_registry, "DEFAULT_ENGINE", "test-motor")
    return "test-motor"


class TestC2cDefaultEngine:
    def test_translation_config_follows_registry(self, patched_default):
        assert TranslationConfig().mt_engine == patched_default
        assert TranslationConfig(mt_engine="baska").mt_engine == "baska"

    def test_translation_config_default_is_registry_value(self):
        assert TranslationConfig().mt_engine == mt_registry.DEFAULT_ENGINE

    @pytest.mark.parametrize("config", [None, SimpleNamespace(mt_engine=""),
                                        SimpleNamespace(mt_engine=None)])
    def test_engine_property_fallback_follows_registry(self, patched_default,
                                                       monkeypatch, config):
        seen = []
        monkeypatch.setattr("core.mt_engines.create_engine",
                            lambda name: seen.append(name) or object())
        bt = BookTranslator.__new__(BookTranslator)
        bt.config = config
        bt.engine  # noqa: B018 — lazy çözüm
        assert seen == [patched_default]

    def test_engine_property_explicit_name_kept(self, patched_default, monkeypatch):
        seen = []
        monkeypatch.setattr("core.mt_engines.create_engine",
                            lambda name: seen.append(name) or object())
        bt = BookTranslator.__new__(BookTranslator)
        bt.config = SimpleNamespace(mt_engine="hymt-7b")
        bt.engine  # noqa: B018
        assert seen == ["hymt-7b"]

    def test_latex_translator_follows_registry(self, patched_default, tmp_path):
        lt = LatexTranslator(pipeline_config=PipelineConfig(output_dir=str(tmp_path)))
        assert lt.mt_engine == patched_default
        lt2 = LatexTranslator(pipeline_config=PipelineConfig(output_dir=str(tmp_path)),
                              mt_engine="baska")
        assert lt2.mt_engine == "baska"

    def test_retranslator_cli_follows_registry(self, patched_default, monkeypatch,
                                               tmp_path):
        tex = tmp_path / "kitap" / "source" / "translated_body.tex"
        tex.parent.mkdir(parents=True)
        tex.write_text("Merhaba.\n", encoding="utf-8")
        seen = {}

        class _FakeLT:
            def __init__(self, **kwargs):
                seen.update(kwargs)

        monkeypatch.setattr("latex_pipeline.translator.LatexTranslator", _FakeLT)
        monkeypatch.setattr(
            retranslator_mod, "scan_and_retranslate",
            lambda *a, **k: SimpleNamespace(
                total_lines=1, scanned_lines=0, english_lines_found=0,
                retranslated_lines=0, failed_lines=0, skipped_math=0,
                skipped_command=0, skipped_short=0, heading_case_fixes=0,
                changes=[], output_tex=""),
        )
        monkeypatch.setattr(sys, "argv", ["retranslator", str(tex)])
        retranslator_mod._cli_main()
        assert seen["mt_engine"] == patched_default

    def test_no_literal_engine_default_left(self):
        # Motora özgü dallar ('if mt_engine == "hymt-7b"') kalabilir; varsayılan
        # (imza, dataclass alanı, 'or' yedeği, CLI argümanı) literal olamaz.
        pat = re.compile(
            r'mt_engine\s*(?::\s*\w+\s*)?=\s*"hymt-7b"'
            r'|or\s+"hymt-7b"'
            r'|name\s*=\s*"hymt-7b"'
        )
        for rel in ("core/translator.py", "latex_pipeline/translator.py",
                    "latex_pipeline/retranslator.py"):
            src = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
            assert not pat.search(src), rel
