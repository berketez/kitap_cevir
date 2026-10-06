"""Denetim 2026-09-24 (iç denetim notu), kulvar B: metin modu
sembol sarmalaması (G3), stash bağlamları ve Yol A/B ortak sanitizer.

  B1  Mercek 1 #4 — '$m$ ≪ M' -> '$m$ $\\ll$ M' QC broken_inline_math'te
      "Fragmented inline math" HIGH sayılıyordu. G3'ün ürettiği tek komutluk
      sembol ($\\gg$, $\\leq$ ...) artık yakınlık sezgisiyle parça sayılmaz;
      küme tek kaynaktan (sanitizer.TEXT_MODE_SYMBOL_COMMANDS).
  B2  Mercek 1 #5 — sarmalama korunmayan bağlamlarda çalışıyordu: $$...$$
      ("Display math should end with $$"), \\verb, \\url, \\href URL'si,
      \\ensuremath, \\(...\\). Başlık argümanındaki sembol \\texorpdfstring ile
      sarılır (yer imi dönüşümü '$\\alpha$'da PDF'siz duruyordu).
  B3  Mercek 1 #6 — QC Check 11'in ASCII başlık kararı
      retranslator.is_english_line'ın (tek kaynak).
  B4  Mercek 1 #7 — gövdede verbatim içindeki '\\begin{document}' Phase 5.5
      gövde temizliğini devre dışı bırakıyordu.

Metinler bu dosya için yazılmış sentetik satırlardır (kitap metni değil).
"""

import tempfile
from pathlib import Path

import pytest

import latex_pipeline.retranslator as retranslator
from latex_pipeline.autofix import AutoFixer
from latex_pipeline.config import PipelineConfig
from latex_pipeline.qc_engine import QCEngine
from latex_pipeline.sanitizer import (
    _TEXT_MATH_SYMBOL_MAP,
    TEXT_MODE_SYMBOL_COMMANDS,
    LatexSanitizer,
    _dollar_display_spans,
    apply_text_mode_math_wraps,
)
from latex_pipeline.template_engine import TemplateEngine


@pytest.fixture
def engine():
    return QCEngine()


def _issues(report, *names):
    return [i for i in report.issues if i.check_name in names]


def _san(text: str, manual: bool = True) -> str:
    return LatexSanitizer(manual_translation=manual).sanitize(text)[0]


# ---------------------------------------------------------------------------
# B1 — G3 sarmalaması QC'de parça sayılmaz
# ---------------------------------------------------------------------------

class TestB1SymbolWrapNotFragment:
    EN = ("\\chapter{Orbits}\n\n"
          "A probe of mass $m$ ≪ M moves as if the planet were fixed in "
          "space.\n\n"
          "When $r_2$ ≫ $r_1$ the bi-elliptic path wins by a small margin.\n\n"
          "Open paths need $e$ ≥ 1 and they carry positive energy.\n")
    TR = ("\\chapter{Yörüngeler}\n\n"
          "Kütlesi $m$ ≪ M olan bir sonda, gezegen uzayda sabitmiş gibi "
          "hareket eder.\n\n"
          "$r_2$ ≫ $r_1$ olduğunda çift eliptik yol küçük bir farkla "
          "kazanır.\n\n"
          "Açık yollar $e$ ≥ 1 ister ve pozitif enerji taşır.\n")

    def test_g3_qc_skor_100_ve_autofix_sonrasi_100(self, engine):
        """Rapordaki g3_qc.py durumu: eskiden ilk ve son skor 76."""
        san = _san(self.TR, manual=True)
        assert "$m$ $\\ll$ M" in san  # glif düzeltmesi korunuyor
        first = engine.analyze(san, self.EN, manual_translation=True)
        fixer = AutoFixer(qc_engine=engine, translator=None,
                          manual_translation=True)
        fixed, _ = fixer.run(san, self.EN, initial_report=first)
        last = engine.analyze(fixed, self.EN, manual_translation=True)
        assert first.score == 100.0, [i.message for i in first.issues]
        assert last.score == 100.0, [i.message for i in last.issues]

    @pytest.mark.parametrize("manual", [True, False])
    @pytest.mark.parametrize("char", sorted(_TEXT_MATH_SYMBOL_MAP))
    def test_sayim_paritesi_iki_kontrolde(self, engine, char, manual):
        """inline_math_loss (3bf6136) ve broken_inline_math birlikte temiz."""
        en = f"The bound $a$ {char} b holds and $x$ {char} $y$ follows.\n"
        tr = _san(f"Sınır $a$ {char} b sağlanır ve $x$ {char} $y$ çıkar.\n",
                  manual=manual)
        cmd = "\\" + _TEXT_MATH_SYMBOL_MAP[char]
        assert f"$a$ ${cmd}$ b" in tr
        report = engine.analyze(tr, en, manual_translation=manual)
        assert _issues(report, "inline_math_loss", "broken_inline_math") == []

    def test_kume_tek_kaynaktan(self):
        assert TEXT_MODE_SYMBOL_COMMANDS == {
            "\\" + name for name in _TEXT_MATH_SYMBOL_MAP.values()}

    def test_kaynaksiz_da_alarm_yok(self, engine):
        report = engine.analyze(_san("Oran $q$ ≤ 1 ve $p$ ⇔ $s$ olur.\n"))
        assert _issues(report, "broken_inline_math") == []

    @pytest.mark.parametrize("line", [
        "Değer $x^{N}$ $_{T}$ ile verilir.\n",         # kopuk alt indis
        "Değer $\\gg$ $^{2}$ ile verilir.\n",          # sembol + kopuk üs
        "Değer $a$ $b$ ile verilir.\n",                # sıradan yakınlık
    ])
    def test_gercek_parcalanma_hala_yakalanir(self, engine, line):
        report = engine.analyze(line)
        assert _issues(report, "broken_inline_math")


# ---------------------------------------------------------------------------
# B2 — korunmayan bağlamlar
# ---------------------------------------------------------------------------

# Rapordaki g3_edge.py'nin 15 bağlamı: (girdi, beklenen çıktı). Metin modunda
# kalan sembol sarılır (glif düzeltmesi); math/kod/URL içindeki dokunulmaz.
_G3_EDGE = {
    "texttt": ("Komut \\texttt{a → b} çalışır.",
               "Komut \\texttt{a $\\rightarrow$ b} çalışır."),
    "verb": ("Kod \\verb|a ≤ b| örneği.", None),
    "verbatim": ("\\begin{verbatim}\nif a ≤ b: pass\n\\end{verbatim}", None),
    "lstlisting": ("\\begin{lstlisting}\nif a ≤ b: pass\n\\end{lstlisting}",
                   None),
    "minted": ("\\begin{minted}{python}\nif a ≤ b: pass\n\\end{minted}", None),
    "section": ("\\section{Girdi → Çıktı Eşlemesi}",
                "\\section{Girdi \\texorpdfstring{$\\rightarrow$}{→} "
                "Çıktı Eşlemesi}"),
    "comment": ("Metin % yorum a ≤ b", "Metin \\% yorum a $\\leq$ b"),
    "paren_math": ("Burada \\(a\\) ≤ \\(b\\) olur.",
                   "Burada \\(a\\) $\\leq$ \\(b\\) olur."),
    "dollars_escaped": ("Fiyat \\$5 ≤ \\$10 aralığında.",
                        "Fiyat \\$5 $\\leq$ \\$10 aralığında."),
    "ensuremath": ("Değer \\ensuremath{a ≤ b} olur.", None),
    "multiline_inline": ("Burada $a\n≤ b$ olur.", None),
    "display_dd": ("Burada $$a ≤ b$$ olur.", None),
    "href": ("\\href{https://x.org/a→b}{bağlantı}", None),
    "url": ("\\url{https://x.org/a≥b}", None),
    "text_in_math": ("$x \\text{ için } y$ ve a ≤ b.",
                     "$x \\text{ için } y$ ve a $\\leq$ b."),
}


class TestB2ProtectedContexts:
    @pytest.mark.parametrize("manual", [True, False])
    @pytest.mark.parametrize("case", sorted(_G3_EDGE))
    def test_g3_edge_15_baglam(self, case, manual):
        text, expected = _G3_EDGE[case]
        assert _san(text, manual=manual) == (expected or text)

    @pytest.mark.parametrize("text", [
        "Burada $$α + b$$ olur.",
        "Burada\n$$\nα + b ≤ c\n$$\nolur.",
        "Kod \\verb|α + b| örneği.",
        "Kod \\verb*!α ≥ b! örneği.",
        "Değer \\ensuremath{\\frac{α}{2} ≤ b} olur.",
        "Açı \\(α ≤ β\\) olur.",
        "\\url{https://x.org/α}",
        "\\href{https://x.org/β→γ}{bağlantı}",
    ])
    def test_yunan_harfi_de_korunur(self, text):
        assert _san(text) == text
        assert apply_text_mode_math_wraps(text) == text  # QC paritesi

    def test_href_baglanti_metni_hala_sarilir(self):
        out = _san("\\href{https://x.org/a→b}{Girdi → Çıktı}")
        assert out == ("\\href{https://x.org/a→b}"
                       "{Girdi $\\rightarrow$ Çıktı}")

    def test_bitisik_inline_math_display_sayilmaz(self):
        # '$\\Theta$${}^{*}$' iki inline math'tir; aradaki düzyazı korunmaz.
        text = "Optimal $\\Theta$${}^{*}$ bulunur ve α açısı ile $\\theta$ ${}^{*}$ olur."
        assert _dollar_display_spans(text) == []
        assert "ve $\\alpha$ açısı" in _san(text)

    def test_kapanmayan_display_paragrafi_yutmaz(self):
        text = "Fiyat $$ yazımı bozuk.\n\nSonraki paragrafta α açısı."
        assert _dollar_display_spans(text) == []
        assert "$\\alpha$ açısı" in _san(text)

    def test_display_araligi(self):
        text = "a $x$ b $$y ≤ z$$ c \\$ d $$w$$"
        spans = [text[s:e] for s, e in _dollar_display_spans(text)]
        assert spans == ["$$y ≤ z$$", "$$w$$"]

    def test_eslesmeyen_paren_artefakti_eskisi_gibi_temizlenir(self):
        # Marker '\\(2015)' artefaktı: yüzde kaçışı ve eşleşmeyen \\( onarımı.
        out = _san("Yazar \\(2015) oranı %40 buldu, α da arttı.")
        assert out == "Yazar (2015) oranı \\%40 buldu, $\\alpha$ da arttı."
        # Birbirine bağlanan artefakt çifti içinde de yüzde kaçışı sürer.
        out = _san("Yazar \\(2006) %30 oranı 1989\\) buldu.")
        assert "\\%30" in out

    def test_href_url_yuzde_kacisi_ve_sayfa_baglantisi_degismez(self):
        out = _san("Bak \\href{https://x.org/a%20b}{kaynak} ve "
                   "\\href{#page-12-0}{\\[ 12}].")
        assert "\\href{https://x.org/a\\%20b}{kaynak}" in out
        assert "#page-" not in out and "[12]" in out

    def test_url_yuzde_kacisi_degismez(self):
        assert _san("Bak \\url{https://x.org/a%20b} adresi.") == (
            "Bak \\url{https://x.org/a%20b} adresi.")

    @pytest.mark.parametrize("text,expected", [
        ("\\section{α ve β → γ}",
         "\\section{\\texorpdfstring{$\\alpha$}{α} ve "
         "\\texorpdfstring{$\\beta$}{β} \\texorpdfstring{$\\rightarrow$}{→} "
         "\\texorpdfstring{$\\gamma$}{γ}}"),
        ("\\section[Kısa α]{Uzun \\textit{θ} başlık}",
         "\\section[Kısa \\texorpdfstring{$\\alpha$}{α}]"
         "{Uzun \\texorpdfstring{$\\theta$}{θ} başlık}"),
        ("\\chapter*{Girdi ≤ Çıktı}",
         "\\chapter*{Girdi \\texorpdfstring{$\\leq$}{≤} Çıktı}"),
        ("\\subsection{$x$ → $y$ eşlemesi}",
         "\\subsection{$x$ \\texorpdfstring{$\\rightarrow$}{→} $y$ eşlemesi}"),
    ])
    def test_baslikta_yer_imi_guvenli_sarma(self, text, expected):
        out = _san(text)
        assert out == expected
        assert _san(out) == out                      # idempotent
        assert apply_text_mode_math_wraps(text) == out  # QC paritesi

    def test_tam_belge_idempotent(self):
        body = ("\\chapter{Deneme}\n\\section{Girdi → Çıktı}\n\n"
                "Burada $$a ≤ b$$ ve \\(c ≥ d\\) ile M ≫ m olur.\n\n"
                "\\href{https://x.org/a→b}{bağ → lantı} ve \\verb|α|.\n")
        once = _san(_wrapped(body))
        assert _san(once) == once


class TestB2StashSecurity:
    """B2'nin yan etkisi (denetçi A, 2026-09-24): $$...$$ ve \\ensuremath
    stash'e alınınca güvenlik filtresini atlıyordu (324d225'te
    '$$\\input{/etc/passwd}$$' filtreleniyordu). Stash'teki matematik
    bölgeleri de filtrelenir; kod bölgeleri harfiyen kalır."""

    @pytest.mark.parametrize("text", [
        "Metin $$\\input{/etc/passwd}$$ son.",
        "Metin \\ensuremath{\\input{/etc/passwd}} son.",
        "Metin $\\input{/etc/passwd}$ son.",
        "Metin \\[\\input{/etc/passwd}\\] son.",
        "Metin $$\\immediate\\write18{ls}$$ son.",
        "\\begin{equation}\na = \\input{../gizli}\n\\end{equation}\n",
    ])
    @pytest.mark.parametrize("manual", [True, False])
    def test_matematik_bolgesinde_filtrelenir(self, text, manual):
        san = LatexSanitizer(manual_translation=manual)
        out, rep = san.sanitize(text)
        assert "\\input{" not in out and "\\write18" not in out
        assert "FILTERED" in out
        assert rep.dangerous_filtered == 1
        assert "\n\n" not in out          # matematikte boş satır derlenmez
        assert san.sanitize(out)[0] == out

    @pytest.mark.parametrize("text", [
        "Kod \\verb|\\input{/etc/passwd}| örneği.",
        "\\begin{verbatim}\n\\input{/etc/passwd}\n\\end{verbatim}",
    ])
    def test_kod_bolgesi_harfiyen_kalir(self, text):
        out, rep = LatexSanitizer(manual_translation=True).sanitize(text)
        assert out == text
        assert rep.dangerous_filtered == 0

    def test_zararsiz_matematik_degismez(self):
        text = ("Oran $$\\frac{a}{b} \\leq \\csname theequation\\endcsname$$ "
                "ve \\ensuremath{\\alpha + 1} olur.")
        assert _san(text) == text


# ---------------------------------------------------------------------------
# B3 — başlık dil kararı retranslator.is_english_line'dan
# ---------------------------------------------------------------------------

class TestB3HeadingDecisionSingleSource:
    @pytest.mark.parametrize("manual", [True, False])
    @pytest.mark.parametrize("title", [
        "\\textit{n}-Cisim Problemi", "Potansiyel Enerji", "Konik Kesitler",
    ])
    def test_ascii_turkce_baslik_isaretlenmez(self, engine, title, manual):
        tr = f"\\section{{{title}}}\n\nBu bölümde hareket incelenir.\n"
        report = engine.analyze(tr, manual_translation=manual)
        assert _issues(report, "untranslated_heading") == []

    @pytest.mark.parametrize("manual", [True, False])
    @pytest.mark.parametrize("title", [
        "Newton's Laws of Motion", "Period of an Elliptical Orbit",
    ])
    def test_ingilizce_baslik_isaretlenir(self, engine, title, manual):
        tr = f"\\section{{{title}}}\n\nBu bölümde hareket incelenir.\n"
        report = engine.analyze(tr, manual_translation=manual)
        found = _issues(report, "untranslated_heading")
        assert len(found) == 1 and found[0].severity == "high"

    @pytest.mark.parametrize("verdict", [True, False])
    def test_karar_is_english_line_a_birakilir(self, engine, monkeypatch,
                                               verdict):
        calls = []

        def _stub(text, *args, **kwargs):
            calls.append((text, kwargs.get("is_heading")))
            return verdict

        monkeypatch.setattr(retranslator, "is_english_line", _stub)
        report = engine.analyze("\\section{Ornek Baslik Metni}\n")
        assert ("\\section{Ornek Baslik Metni}", True) in calls
        assert bool(_issues(report, "untranslated_heading")) is verdict

    def test_kaynakta_birebir_baslik_yankisi_isaretlenir(self, engine,
                                                          monkeypatch):
        # Dil kararı "Türkçe" dese de başlık kaynakta başlık olarak aynen
        # geçiyorsa çevrilmemiştir; numara öneki iki tarafta da atılır.
        monkeypatch.setattr(retranslator, "is_english_line",
                            lambda *a, **k: False)
        en = "\\section{1.2 Scalar Products}\n\\section{1.3 Basis Change}\n"
        tr = "\\section{Scalar Products}\n\\section{Taban Degisimi}\n"
        found = _issues(engine.analyze(tr, en), "untranslated_heading")
        assert [f.message for f in found] == [
            '\\section heading appears untranslated: "Scalar Products"']
        assert found[0].confidence == 0.95

    def test_latin_disi_alfabe_hala_isaretlenir(self, engine, monkeypatch):
        monkeypatch.setattr(retranslator, "is_english_line",
                            lambda *a, **k: False)
        report = engine.analyze("\\section{ТЕОРИЯ РАКЕТНЫХ ДВИГАТЕЛЕЙ}\n")
        assert len(_issues(report, "untranslated_heading")) == 1


# ---------------------------------------------------------------------------
# B4 — yalnız gövde verilince preamble ayrımı yapılmaz
# ---------------------------------------------------------------------------

def _wrapped(body: str) -> str:
    config = PipelineConfig(input_pdf=Path("kitap.pdf"),
                            output_dir=Path(tempfile.mkdtemp()),
                            document_class="book")
    return TemplateEngine(config).wrap_content(body)


_DOCBEGIN_BODY = (
    "\\chapter{LaTeX'e Giriş}\n\nAçı α ile gösterilir ve oran %50 artar.\n\n"
    "\\begin{verbatim}\n\\documentclass{article}\n\\begin{document}\n"
    "Merhaba %1 α\n\\end{document}\n\\end{verbatim}\n\nSonra β açısı gelir.\n")


class TestB4BodyWithDocumentBegin:
    @pytest.mark.parametrize("manual", [True, False])
    def test_govde_verbatim_icindeki_begin_document(self, manual):
        """Rapordaki docbegin.py durumu: önceki gövde temizlenmiyordu."""
        out = _san(_DOCBEGIN_BODY, manual=manual)
        assert "Açı $\\alpha$ ile gösterilir ve oran \\%50 artar." in out
        assert "Sonra $\\beta$ açısı gelir." in out
        assert "Merhaba %1 α\n" in out   # verbatim içi aynen

    @pytest.mark.parametrize("manual", [True, False])
    def test_tam_belge_davranisi_ayni(self, manual):
        """Phase 6 benzetimi: preamble aynen, gövde (verbatim hariç) temiz."""
        tex = _wrapped(_DOCBEGIN_BODY)
        out = _san(tex, manual=manual)
        split = tex.index("\\begin{document}")
        assert out[:split] == tex[:split]
        assert "{\\%" not in out
        assert "oran \\%50 artar" in out and "Merhaba %1 α\n" in out
        assert _san(_DOCBEGIN_BODY, manual=manual) in out

    def test_yorumla_baslayan_tam_belge(self):
        tex = ("% !TEX program = xelatex\n%% şablon\n\n"
               "\\documentclass{book}\n\\newcommand{\\x}{%\n  y}\n"
               "\\begin{document}\nOran %5 ve α.\n\\end{document}\n")
        out = _san(tex)
        assert "\\newcommand{\\x}{%\n" in out
        assert "Oran \\%5 ve $\\alpha$." in out

    def test_yorumla_baslayan_govde_ayrilmaz(self):
        body = ("% not: aşağıda bir örnek var\nOran %5 ve α.\n"
                "\\begin{verbatim}\n\\begin{document}\n\\end{verbatim}\n")
        out = _san(body)
        assert "Oran \\%5 ve $\\alpha$." in out
