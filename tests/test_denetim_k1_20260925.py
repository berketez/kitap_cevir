"""Denetim 25 Eylül 2026 — kulvar K1: kalan kod bulguları.

  K1b  retranslator.is_english_line(başlık): salt-ASCII Türkçe başlıkta "Is"
       (iş) ve "Of" (Of ilçesi) İngilizce kanıtı sayılıyordu ("Is ve Enerji",
       "Is Birligi", "Of Tuzlasi" -> True). Başlıkta Türkçe iz (bağlaç/edat
       ya da ASCII iyelik eki) varsa bu iki sözcük kanıt sayılmaz.
  K1c  translator._heading_keeps_source_words: özel adlı doğru çevirileri
       ('Monte Carlo Yöntemleri') reddediyor. Güvenilir ayırıcı sinyal
       bulunamadı; bilinen sınır olarak belgelendi (kilit değil, belge).
  K1d  epub_output._extract_text_from_tex: başlıktaki \\texorpdfstring ham
       giriyor, gövdede '$\\alpha$' '$$' oluyordu. pdfstring dalı alınır,
       basit sembol/Yunan harfi matematiği Unicode'a çevrilir (eşlem
       sanitizer'daki tablolardan, tek kaynak).

Başlık ve cümleler sentetiktir ya da output/ altındaki gerçek koşulardan
alınmış kısa başlıklardır; kitap düzyazısı kopyalanmadı. Model yüklenmez.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from latex_pipeline import sanitizer as sanitizer_mod  # noqa: E402
from latex_pipeline.epub_output import (  # noqa: E402
    _extract_text_from_tex,
    _simple_math_to_unicode,
    _strip_latex_commands,
)
from latex_pipeline.retranslator import (  # noqa: E402
    _heading_has_english_word,
    _heading_has_turkish_trace,
    is_english_line,
)
from latex_pipeline.translator import (  # noqa: E402
    LatexTranslator,
    TranslationStats,
    _heading_keeps_source_words,
)


# ---------------------------------------------------------------------------
# K1b — "Is"/"Of" salt-ASCII Türkçe başlıkta kanıt değil
# ---------------------------------------------------------------------------

_QC = dict(min_words=1, min_length=4)   # QC başlık kontrolünün eşikleri


class TestK1bTurkishHomographFunctionWords:
    @pytest.mark.parametrize("title", [
        "Is ve Enerji", "Is, Guc ve Enerji", "Is Birligi", "Of Tuzlasi",
        "Is ile Enerji", "Of ve Rize", "Of Ilcesi",
    ])
    @pytest.mark.parametrize("kw", [{}, _QC])
    def test_ascii_turkish_heading_not_english(self, title, kw):
        line = "\\section{" + title + "}"
        assert is_english_line(line, is_heading=True, **kw) is False, title

    @pytest.mark.parametrize("title", [
        # yalnız ek işlev sözcüğüne dayanan İngilizce başlık biçimleri (sentetik)
        "Time is Gold", "Time Is Gold", "Birds Like Wind", "Wind Like Birds",
        "Time is Patience", "Also by Dario Fenassi", "Dawn of a New Season",
        "Hail to the Q",
    ])
    @pytest.mark.parametrize("kw", [{}, _QC])
    def test_english_heading_still_detected(self, title, kw):
        line = "\\section{" + title + "}"
        assert is_english_line(line, is_heading=True, **kw) is True, title

    # Kulvar B'nin (QC başlık kontrolü) dayandığı beş örnek değişmez.
    @pytest.mark.parametrize("title, expected", [
        (r"\textit{n}-Cisim Problemi", False),
        ("Potansiyel Enerji", False),
        ("Konik Kesitler", False),
        ("Newton's Laws of Motion", True),
        ("Period of an Elliptical Orbit", True),
    ])
    def test_b3_contract_examples(self, title, expected):
        assert is_english_line("\\section{" + title + "}",
                               is_heading=True) is expected

    @pytest.mark.parametrize("words, expected", [
        (["Is", "ve", "Enerji"], True),        # Türkçe bağlaç
        (["Is", "Birligi"], True),             # -liği (ASCII)
        (["Of", "Tuzlasi"], True),             # -sı (ASCII)
        (["Time", "is", "Gold"], False),
        (["Birds", "Like", "Wind"], False),
        (["Also", "by", "Dario", "Fenassi"], False),  # 'assi': sesli+si değil
        (["Quasi", "Static"], True),           # bilinen sınır; q izi zaten İngilizce
    ])
    def test_turkish_trace_helper(self, words, expected):
        assert _heading_has_turkish_trace(words) is expected

    @pytest.mark.parametrize("words, expected", [
        (["Is", "ve", "Enerji"], False),
        (["Of", "Tuzlasi"], False),
        (["Time", "is", "Gold"], True),
        # Türkçe izli karma başlıkta Türkçe'de olmayan ek sözcük hâlâ kanıt
        (["Kuslar", "ve", "Birds", "Like", "Wind"], True),
    ])
    def test_word_evidence_with_trace(self, words, expected):
        assert _heading_has_english_word(words) is expected

    def test_known_limit_bare_two_word(self):
        # Bilinen sınır (kilit değil, belge): Türkçe izi olmayan iki sözcüklü
        # başlık ("Is Gucu" = İş Gücü) QC eşiklerinde hâlâ İngilizce sayılır;
        # "gucu" hiçbir izde yok. Varsayılan eşikte kısa olduğu için False.
        assert is_english_line("\\section{Is Gucu}", is_heading=True) is False
        assert is_english_line("\\section{Is Gucu}", is_heading=True, **_QC) is True


# ---------------------------------------------------------------------------
# K1c — kısa başlık kapısı: bilinen sınır (güvenilir sinyal yok)
# ---------------------------------------------------------------------------

def _bare_lt() -> LatexTranslator:
    t = LatexTranslator.__new__(LatexTranslator)
    t._glossary_term_map = {}
    t.stats = TranslationStats()
    t.target_lang = "tr"
    return t


class _FakeMT:
    def __init__(self, output):
        self._output = output

    def translate_text(self, text, is_heading=False):
        return self._output


def _heading_via(mt_output: str, source: str) -> str:
    t = _bare_lt()
    t._translator = _FakeMT(mt_output)
    return t._translate_heading_text(source)


class TestK1cShortHeadingGateKnownLimit:
    """Bilinen sınır (kilit değil, belge). Özel adlı doğru çeviri ile yarım
    çeviri aynı biçimdedir: kalan iki kaynak sözcük İngilizce yazım izi
    taşımaz, kesmeli ek yoktur, Türkçe baş sözcük iyelik eki alır
    ('Van Allen Kuşakları' ~ 'Hall Effect İtme Kuvveti', 'Big Bang Kuramı' ~
    'Solid Propellant Ateşlenmesi'). Repodaki İngilizce sözcük kümelerinde
    ne 'effect/solid/propellant' ne de 'van/allen/big' ayrışıyor (sözlük
    anahtarlarında hepsi var). Kapı kaynak başlığı korur; yanlış kabul
    yerine İngilizce başlık kalır. output/ ölçümü: 232 kısa başlıkta kapının
    tetiklendiği 23 çiftin 23'ü gerçek yarım çeviri."""

    @pytest.mark.parametrize("source, mt", [
        ("Monte Carlo Methods", "Monte Carlo Yöntemleri"),
        ("Hamilton Jacobi Equation", "Hamilton Jacobi Denklemi"),
        ("Van Allen Belts", "Van Allen Kuşakları"),
        ("Big Bang Theory", "Big Bang Kuramı"),
    ])
    def test_eponym_translation_rejected_known_limit(self, source, mt):
        assert _heading_keeps_source_words(source, mt) is True
        assert _heading_via(mt, source) == source

    @pytest.mark.parametrize("source, mt", [
        ("Ignition of Solid Propellant", "Solid Propellant'nın Ateşlenmesi"),
        ("Period of an Elliptical Orbit", "Elliptical Orbit'nın Periyodu"),
        # Özel adlı çeviriyle aynı biçimdeki yarım çeviriler: kabul edilirse
        # yanlış kabul olur, bu yüzden kapı gevşetilmedi.
        ("Hall Effect Thruster", "Hall Effect İtme Kuvveti"),
        ("Ignition of Solid Propellant", "Solid Propellant Ateşlenmesi"),
    ])
    def test_half_translation_still_rejected(self, source, mt):
        assert _heading_keeps_source_words(source, mt) is True

    def test_hyphenated_eponym_is_exempt(self):
        # Tireli yazımda özel ad muafiyeti zaten çalışıyor.
        assert _heading_keeps_source_words(
            "Hamilton-Jacobi Equation", "Hamilton-Jacobi Denklemi") is False


# ---------------------------------------------------------------------------
# K1d — EPUB düz metin çıkarımı
# ---------------------------------------------------------------------------

class TestK1dEpubTextExtraction:
    def test_heading_texorpdfstring_takes_pdf_branch(self):
        tex = "\\chapter{Girdi \\texorpdfstring{$\\rightarrow$}{→} Çıktı}\n"
        assert _extract_text_from_tex(tex) == ["Girdi → Çıktı"]

    def test_heading_label_tail_not_included(self):
        tex = "\\section{Açı \\texorpdfstring{$\\alpha$}{α} ve Hız}\\label{sec:aci}\n"
        assert _extract_text_from_tex(tex) == ["Açı α ve Hız"]

    def test_heading_commands_stripped(self):
        tex = "\\section{The \\textit{n}-Cisim Problemi}\n"
        assert _extract_text_from_tex(tex) == ["The n-Cisim Problemi"]

    def test_caption_cleaned(self):
        tex = "\\caption{Sınır \\texorpdfstring{$\\leq$}{≤} değer \\textit{deneme}}\n"
        assert _extract_text_from_tex(tex) == ["Sınır ≤ değer deneme"]

    @pytest.mark.parametrize("line, expected", [
        ("Açı $\\alpha$ ile gösterilir.", "Açı α ile gösterilir."),
        ("Kütle $m \\ll M$ olur.", "Kütle m ≪ M olur."),
        ("Süre $\\Delta t$ kısadır.", "Süre Δ t kısadır."),
        ("Açı \\(\\beta\\) ile.", "Açı β ile."),
        ("Oran $a \\leq b$ ve $c \\geq d$.", "Oran a ≤ b ve c ≥ d."),
        ("Ok $\\rightarrow$ ve $\\Leftrightarrow$.", "Ok → ve ⇔."),
    ])
    def test_simple_math_to_unicode(self, line, expected):
        assert _strip_latex_commands(line) == expected

    @pytest.mark.parametrize("line", [
        "Değişken $x$ kalır.",            # komutsuz matematik: eskisi gibi
        "Kare $x^2$ kalır.",               # üs: basit değil
        "Kesir $\\frac{a}{b}$ oranı.",     # eski davranış (değişmedi)
        "Display $$\\alpha$$ satırı.",     # display matematiğe dokunulmaz
        "Bilinmeyen $\\nabla f$ komut.",   # eşlemde olmayan komut
    ])
    def test_non_simple_math_unchanged(self, line):
        from latex_pipeline import epub_output as eo

        old = eo._CMD_BARE_RE.sub("", eo._CMD_STRIP_RE.sub(r"\1", line))
        assert _strip_latex_commands(line) == " ".join(old.split())

    def test_escaped_dollar_not_math(self):
        assert _strip_latex_commands("Fiyat \\$5 ve \\$6.") == "Fiyat \\$5 ve \\$6."

    @pytest.mark.parametrize("line", [
        "Duyarl{\\i}l{\\i}k Analizi", "Duyarl\\i l\\i k Analizi",
        "Duyarl\\i{}l\\i{}k Analizi",
    ])
    def test_dotless_i(self, line):
        assert _strip_latex_commands(line) == "Duyarlılık Analizi"

    def test_paragraph_count_unchanged(self):
        # Pozisyonel segment eşlemesi paragraf sayısına dayanır: dönüşüm
        # paragraf eklemez/silmez.
        tex = ("\\chapter{Bir \\texorpdfstring{$\\alpha$}{α}}\n\n"
               "Satır $\\alpha$ bir.\nSatır $x$ iki.\n\n"
               "$\\pi$\n\n"
               "\\caption{Şekil $\\beta$}\n\nSon $\\gamma$ paragraf.\n")
        out = _extract_text_from_tex(tex)
        assert out == ["Bir α", "Satır α bir. Satır $x$ iki.",
                       "Şekil β", "Son γ paragraf."]

    def test_symbol_map_single_source(self):
        inverse = sanitizer_mod.LATEX_SYMBOL_TO_UNICODE
        for char, name in sanitizer_mod._GREEK_NAME_MAP.items():
            assert name in inverse
        for char, name in sanitizer_mod._TEXT_MATH_SYMBOL_MAP.items():
            assert inverse[name] == char
        assert inverse["epsilon"] == "ε"      # tablodaki ilk karşılık
        assert _simple_math_to_unicode("\\epsilon") == "ε"
        assert _simple_math_to_unicode("x") is None
