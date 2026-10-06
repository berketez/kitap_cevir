"""QC / sanitizer tutarlılığı (Dalga 2, 24 Eylül 2026 Yol B koşusu).

Envanter (iç denetim notu): QC'nin 58 bulgusunun
44'ü pipeline'ın kendi ürettiği yanlış alarmdı. Her test bir kök nedeni
kilitler VE aynı kontrolün gerçek sorunu hâlâ yakaladığını doğrular.

Kalemler: I2 (bölüm numarası), M (Şek./Denk., Türkçe kelime sırası,
matematik değişkeni "a", tablo satırı tekrarı, ASCII Türkçe gövde satırı),
G2 (display-math ortam dönüşümü), I4 (B21 TOC sezgisi + QC sütun sayacı),
başlık regex'i (parantez dengesi), L (naturalizer "epey"), G3 (≫ ≪).
"""

import pytest

from latex_pipeline.qc_engine import (
    QCEngine,
    _count_tabular_spec_columns,
    _decode_latex_turkish,
)
from latex_pipeline.retranslator import is_english_line
from latex_pipeline.sanitizer import (
    LatexSanitizer,
    SanitizeReport,
    apply_text_mode_math_wraps,
)
from core.naturalizer import TurkishNaturalizer


@pytest.fixture
def engine():
    return QCEngine()


def _issues(report, name):
    return [i for i in report.issues if i.check_name == name]


# ---------------------------------------------------------------------------
# I2 — sanitizer P2 section önekini siler; QC bunu kayıp saymamalı
# ---------------------------------------------------------------------------

class TestSectionNumberParity:
    EN = ("\\chapter{Two-Body Mechanics}\n"
          "\\section{1.1 Origins of Celestial Mechanics}\n"
          "\\section{1.2 Newton's Laws}\n"
          "\\subsection{1.2.1 First Law}\n")

    def test_sanitize_edilmis_ceviri_alarm_vermez(self, engine):
        tr_raw = ("\\chapter{İki Cisim Mekaniği}\n"
                  "\\section{1.1 Gök Mekaniğinin Kökenleri}\n"
                  "\\section{1.2 Newton Yasaları}\n"
                  "\\subsection{1.2.1 Birinci Yasa}\n")
        tr, _ = LatexSanitizer().sanitize(tr_raw)
        assert "1.1" not in tr  # sanitizer öneki sildi (davranış korunuyor)
        report = engine.analyze(tr, self.EN)
        assert _issues(report, "section_number_consistency") == []

    def test_mt_numarayi_degistirirse_yakalanir(self, engine):
        # Sanitize edilmemiş gövde: numaralar duruyor, MT 1.2'yi 1.3 yaptı.
        tr = ("\\section{1.1 Gök Mekaniğinin Kökenleri}\n"
              "\\section{1.3 Newton Yasaları}\n"
              "\\subsection{1.2.1 Birinci Yasa}\n")
        report = engine.analyze(tr, self.EN)
        sec = _issues(report, "section_number_consistency")
        assert sec and all(i.severity == "critical" for i in sec)

    def test_chapter_numarasi_karsilastirilmaya_devam_eder(self, engine):
        # Sanitizer \chapter numarasına dokunmaz; kayıp hâlâ CRITICAL.
        en = "\\chapter{3 Rocket Nozzles}\n\\section{3.1 Intro}\n"
        tr = "\\chapter{Roket Lüleleri}\n\\section{Giriş}\n"
        report = engine.analyze(tr, en)
        msgs = [i.message for i in _issues(report, "section_number_consistency")]
        assert any("EN='3'" in m for m in msgs)


# ---------------------------------------------------------------------------
# M — çapraz referans numaraları
# ---------------------------------------------------------------------------

class TestXrefNumbers:
    def test_sek_ve_denk_kisaltmalari_taninir(self):
        assert QCEngine._xref_numbers("Şek. 2.3 ve Denk. 4 bakınız") == ["2.3", "4"]
        assert QCEngine._xref_numbers("Şek.~1.9") == ["1.9"]

    def test_kucuk_harf_denk_sifattir(self):
        assert QCEngine._xref_numbers("iki değer birbirine denk. 3 farklı") == []

    def test_ek_almis_etiket_ve_yil(self):
        # "Bruns bu teoremi 1887 yılında ispatladı" — yıl referans değildir.
        assert QCEngine._xref_numbers("Bruns bu teoremi 1887 yılında") == []
        assert QCEngine._xref_numbers("örneğin 3 topluluk yerine") == []

    def test_cogul_ve_noktali_ekli_referans(self):
        assert QCEngine._xref_numbers("(Bölümler 10–12)") == ["10"]
        assert QCEngine._xref_numbers("Teoremde 2.6 gösterildiği") == ["2.6"]
        assert QCEngine._xref_numbers("Teorem 3 ve Şekil 1.1") == ["3", "1.1"]

    def test_astro_sahte_bulgular_kalkar(self, engine):
        en = ("See (Fig. 2.3), the vector equation. Bruns demonstrated this "
              "theorem in 1887. As in Fig. 2.7 one realizes.")
        tr = ("Bkz. Şek. 2.3, vektör denklemi. Bruns bu teoremi 1887 yılında "
              "ispatladı. Şek. 2.7'de görüldüğü gibi.")
        report = engine.analyze(tr, en)
        assert _issues(report, "number_hallucination") == []

    def test_gercek_referans_kaybi_yakalanir(self, engine):
        en = "As shown in Fig. 2.3 and Table 1.1, the orbit closes."
        tr = "Şek. 2.4 ve Tablo 1.1'de gösterildiği gibi yörünge kapanır."
        report = engine.analyze(tr, en)
        msgs = [i.message for i in _issues(report, "number_hallucination")]
        assert any("'2.3'" in m and "missing" in m for m in msgs)
        assert any("'2.4'" in m for m in msgs)


# ---------------------------------------------------------------------------
# G2 — display-math sayımı ortam adından bağımsız
# ---------------------------------------------------------------------------

LONG_EQ = " + ".join(f"a_{{{k}}} x^{{{k}}}" for k in range(30))


class TestDisplayMathTotal:
    def test_sanitizer_donusumu_alarm_vermez(self, engine):
        en = ("Metin.\n\n\\[ " + LONG_EQ + " = 0 \\]\n\n"
              "\\begin{equation*}\na = b\nc = d\n\\end{equation*}\n")
        tr, _ = LatexSanitizer().sanitize(en)
        # Sanitizer B22 (\[ -> multline*) ve B24 (equation* -> align*) çalıştı
        assert "multline*" in tr and "align*" in tr
        report = engine.analyze(tr, en)
        assert _issues(report, "formula_integrity") == []

    def test_gercek_formul_kaybi_yakalanir(self, engine):
        en = ("\\[ a = b \\]\n\n\\[ c = d \\]\n\n"
              "\\begin{equation}\ne = f\n\\end{equation}\n")
        tr = "\\[ a = b \\]\n\n\\begin{equation}\ne = f\n\\end{equation}\n"
        report = engine.analyze(tr, en)
        fi = _issues(report, "formula_integrity")
        assert len(fi) == 1 and "EN=3, TR=2" in fi[0].message


# ---------------------------------------------------------------------------
# I4 — B21 yalnız gerçek içindekiler tablosunda; QC sütun sayacı
# ---------------------------------------------------------------------------

class TestTocTabularAndColumns:
    DATA_TABLE = ("\\begin{tabular}{l l l l l l}\n"
                  "\\toprule v & E & a & v∞ & e & şekil \\\\\n"
                  "\\midrule < ve & < 0 & > 0 & — & < 1 & elips \\\\\n"
                  "= ve & = 0 & ∞ & 0 & = 1 & parabol \\\\\n"
                  "\\bottomrule\n\\end{tabular}")
    TOC_TABLE = ("\\begin{tabular}{l l l}\n"
                 "2.1 & Giriş & 21 \\\\\n"
                 "2.2 & Kimyasal Termodinamiğin Temel İlkeleri & 21 \\\\\n"
                 "2.3 & Termodinamik Yasalar & 22 \\\\\n"
                 "\\end{tabular}")

    def test_veri_tablosuna_dokunulmaz(self):
        out = LatexSanitizer()._fix_toc_tabular_widths(self.DATA_TABLE)
        assert out == self.DATA_TABLE

    def test_toc_tablosu_genisletilir(self):
        out = LatexSanitizer()._fix_toc_tabular_widths(self.TOC_TABLE)
        assert "p{70mm}" in out.splitlines()[0]

    def test_sayisal_izgara_toc_sayilmaz(self):
        grid = ("\\begin{tabular}{l l l}\n1 & 52 & 175 \\\\\n"
                "2 & 21 & 275 \\\\\n3 & 30 & 185 \\\\\n\\end{tabular}")
        assert LatexSanitizer()._fix_toc_tabular_widths(grid) == grid

    @pytest.mark.parametrize("spec,expected", [
        ("l l >{\\raggedright\\arraybackslash}p{70mm} r", 4),
        ("l l l l >{\\raggedright\\arraybackslash}p{70mm} r", 6),
        ("|l|c|r|", 3),
        ("l p{5cm} r", 3),
        ("@{}lX@{}", 2),
        ("*{3}{c}l", 4),
        ("S[table-format=1.2] c", 2),
    ])
    def test_spec_sutun_sayaci(self, spec, expected):
        assert _count_tabular_spec_columns(spec) == expected

    def test_qc_raggedright_spec_alarm_vermez(self, engine):
        tr = ("\\begin{tabular}{l l >{\\raggedright\\arraybackslash}p{70mm} r}\n"
              "Güneş & 1.327 & 1011 & km \\\\\n\\end{tabular}\n")
        assert _issues(engine.analyze(tr), "table_column_mismatch") == []

    def test_qc_gercek_sutun_hatasi_yakalanir(self, engine):
        tr = ("\\begin{tabular}{l >{\\raggedright\\arraybackslash}p{70mm} r}\n"
              "a & b & c & d \\\\\n\\end{tabular}\n")
        tcm = _issues(engine.analyze(tr), "table_column_mismatch")
        assert tcm and "spec says 3 cols" in tcm[0].message


# ---------------------------------------------------------------------------
# Başlık regex'i — parantez dengeli
# ---------------------------------------------------------------------------

class TestUntranslatedHeadingBraces:
    def test_textit_iceren_baslik_tam_okunur(self, engine):
        tr = "\\section{The \\textit{n} -Body Problem}\n\nMetin burada.\n"
        heads = _issues(engine.analyze(tr), "untranslated_heading")
        assert len(heads) == 1
        assert "The \\textit{n} -Body Problem" in heads[0].message

    def test_latex_kodlu_turkce_baslik_ingilizce_sayilmaz(self, engine):
        tr = ("\\section{Duyarl{\\i}l{\\i}k Analizi}\n\n"
              "\\chapter{Kar{\\i}\\c{s}{\\i}m Da\\u{g}{\\i}l{\\i}m{\\i}}\n")
        assert _issues(engine.analyze(tr), "untranslated_heading") == []
        assert _decode_latex_turkish("Sonu\\c{c} A\\c{c}{\\i}klamalar{\\i}") \
            == "Sonuç Açıklamaları"


# ---------------------------------------------------------------------------
# M — is_english_line gövde dalı (ASCII Türkçe)
# ---------------------------------------------------------------------------

class TestIsEnglishLineBody:
    @pytest.mark.parametrize("line", [
        "Birisi bunu elde eder.",               # model çıktısı
        "Belirtmekte fayda var ki…",            # model çıktısı (retranslator)
        "R, evrensel gaz sabitidir.",           # bir test kitabı
        "Itici madde diye ne kastediyorsunuz?",  # bir test kitabı
    ])
    def test_ascii_turkce_ingilizce_sayilmaz(self, line):
        assert is_english_line(line) is False

    @pytest.mark.parametrize("line", [
        "The results are shown in the figure below for clarity.",
        "Risk management ve politika optimizasyonu.",   # yarım çeviri
        "(HPRE); see also Hybrid propellant engines",
        "Rocket propulsion requires careful thermal design choices.",
    ])
    def test_ingilizce_kalinti_yakalanmaya_devam_eder(self, line):
        assert is_english_line(line) is True

    def test_baslik_dali_etkilenmez(self):
        # Birinci dalga davranışı (test_baslik_yapi_20260924 ile aynı)
        assert is_english_line("\\section{Potansiyel Enerji}", is_heading=True) is False
        assert is_english_line("\\section{Potential Energy}", is_heading=True) is True


# ---------------------------------------------------------------------------
# M — "a" matematik değişkeni, tablo satırı tekrarı
# ---------------------------------------------------------------------------

class TestNaturalnessAndRepetition:
    def test_matematik_degiskeni_a_artikel_sayilmaz(self, engine):
        tr = ("Eğer karenin merkezi C'de ve kenarı a olan bir yardımcı "
              "çember çizilirse, köşe ile kenar a arasındaki uzaklık d olur.\n")
        assert _issues(engine.analyze(tr), "turkish_naturalness") == []

    def test_gercek_ingilizce_artikel_yakalanir(self, engine):
        tr = "Bu bölümde yöntemi açıklıyoruz ve a method ile karşılaştırıyoruz.\n"
        tn = _issues(engine.analyze(tr), "turkish_naturalness")
        assert any("a method" in i.message for i in tn)

    def test_tablo_hucre_tekrari_halusinasyon_degil(self, engine):
        tr = TestTocTabularAndColumns.DATA_TABLE.replace(
            "\\bottomrule", "> ve & > 0 & < 0 & > 0 & > 1 & hiperbol \\\\\n"
            "\\bottomrule")
        tr = "\\begin{table}[!htb]\n\\centering\n" + tr + "\n\\end{table}\n"
        assert _issues(engine.analyze(tr), "hallucination_pattern") == []

    def test_paragraf_tekrari_yakalanir(self, engine):
        para = ("Bu yöntem çok iyi çalışır ve sonuç verir. " * 3
                + "Bu yöntem çok iyi çalışır ve sonuç verir.")
        hp = _issues(engine.analyze(para + "\n"), "hallucination_pattern")
        assert hp


# ---------------------------------------------------------------------------
# G3 — metin modu ≫ ≪ (ve günlükte görülen ≤ ≥ → ⇔)
# ---------------------------------------------------------------------------

class TestTextModeMathSymbols:
    def test_sembol_math_moduna_alinir(self):
        body = "M ≫ m olduğundan ve m ≪ M iken $x \\gg y$ aynen kalır.\n"
        out, _ = LatexSanitizer().sanitize(body)
        assert "M $\\gg$ m" in out and "m $\\ll$ M" in out
        assert "$x \\gg y$" in out
        assert "≫" not in out and "≪" not in out

    def test_math_icindeki_sembole_dokunulmaz(self):
        body = "Koşul $a ≤ b$ ve ayrıca c ≥ d.\n"
        out, _ = LatexSanitizer().sanitize(body)
        assert "$a ≤ b$" in out and "c $\\geq$ d" in out

    def test_idempotent(self):
        san = LatexSanitizer()
        once, _ = san.sanitize("A ⇔ B ve x → y.\n")
        twice, _ = san.sanitize(once)
        assert once == twice

    def test_yol_a_da_calisir(self):
        out, _ = LatexSanitizer(manual_translation=True).sanitize("M ≫ m.\n")
        assert "$\\gg$" in out

    def test_qc_inline_sayim_paritesi(self, engine):
        # Sanitizer yalnız çeviriyi sarar; QC kaynağa aynı kuralı uygular.
        en = "Let ν be the angle, with M ≫ m and $x$ small.\n"
        tr, _ = LatexSanitizer().sanitize(
            "ν açı olsun, M ≫ m ve $x$ küçük.\n")
        report = engine.analyze(tr, en)
        assert _issues(report, "inline_math_loss") == []

    def test_bitisik_inline_math_sayimi(self, engine):
        # "$x$$\\theta$" TeX'te iki inline math'tir; ortadaki "$$" display
        # değildir (eski sayaç belge boyunca kayıyordu).
        assert engine._count_inline_math("a $x$$\\theta$ b $y$ c") == 3
        assert engine._count_inline_math("a $$x+y$$ b $z$") == 1

    def test_apply_wraps_yalniz_prose(self):
        text = "α ve $\\beta$ ile \\[ γ \\]"
        out = apply_text_mode_math_wraps(text)
        assert out.startswith("$\\alpha$ ve $\\beta$")
        assert "\\[ γ \\]" in out


# ---------------------------------------------------------------------------
# L — naturalizer "oldukça" -> "epey" kuralı kaldırıldı
# ---------------------------------------------------------------------------

class TestNaturalizerOldukca:
    def test_oldukca_korunur(self):
        nat = TurkishNaturalizer()
        out = nat.naturalize("Bu etki oldukça küçüktür ve oldukca önemlidir.")
        assert "oldukça" in out and "oldukca" in out
        assert "epey" not in out

    def test_diger_kurallar_calisir(self):
        nat = TurkishNaturalizer()
        out = nat.naturalize("Bu  değer  bulunmaktadır .")
        assert "  " not in out and " ." not in out


# ---------------------------------------------------------------------------
# Yol A: QC kararları manual_translation'dan bağımsız (rapor aynı)
# ---------------------------------------------------------------------------

def test_yol_a_ve_b_ayni_bulgulari_uretir(engine):
    en = "\\section{1.1 Intro}\nSee Fig. 2.3.\n\\[ a = b \\]\n"
    tr = "\\section{Giriş}\nBkz. Şek. 2.3.\n\\begin{multline*}\na = b\n\\end{multline*}\n"
    a = engine.analyze(tr, en, manual_translation=True)
    b = engine.analyze(tr, en, manual_translation=False)
    key = lambda r: sorted((i.check_name, i.line_num, i.message) for i in r.issues)
    assert key(a) == key(b)
    for name in ("section_number_consistency", "number_hallucination",
                 "formula_integrity"):
        assert _issues(a, name) == []
