"""S kulvarı (sanitizer, autofix, şablon) düzeltmeleri — 29 Eylül 2026.

Bug avının (2026-09-28) doğrulanmış bulguları:

* F2-10  bölüm açılışındaki altyazısız görsel "rozet" sayılıp siliniyordu
* F2-11  tek başına duran tarih satırı "üst bilgi" diye siliniyordu
* F1-06 / F2-09  kısa gerçek 1. bölüm \\chapter* oluyor, numaralar kayıyordu
* F2-06  satır sonundaki '\\textbf{50}%' yorum sayılıyor, yüzde kayboluyordu
* F2-07  şeklin ardındaki gövde cümlesi altyazı sanılıp float'a gömülüyordu
* F2-13  kapak eklenirken gövdedeki kod örnekleri değişiyordu
* F2-05  matematikte liste virgülü ondalık virgül gibi diziliyordu
* L-1    listings XeLaTeX'te ğ ş ı İ Ğ Ş harflerini yanlış diziyordu

Girdiler sentetiktir. XeLaTeX gerektiren sınamalar xelatex/pdftotext yoksa
atlanır.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from latex_pipeline.config import PipelineConfig
from latex_pipeline.sanitizer import LatexSanitizer, _caption_like_para
from latex_pipeline.template_engine import TemplateEngine

_HAS_TEX = bool(shutil.which("xelatex") and shutil.which("pdftotext"))
needs_tex = pytest.mark.skipif(not _HAS_TEX, reason="xelatex/pdftotext yok")


def _san(text: str, **kw):
    return LatexSanitizer(**kw).sanitize(text)


def _chapters(text: str):
    return re.findall(r"\\chapter\*?\{[^}]*\}", text)


def _png(path: Path, size) -> Path:
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (200, 30, 30)).save(path)
    return path


def _meta(output_dir: Path, source_format: str = "pdf") -> str:
    from yol_a_ortak import META_BEGIN, META_END
    return (f"{META_BEGIN}\n% slug: deneme\n% book_name: deneme\n"
            f"% output_dir: {output_dir}\n% source_format: {source_format}\n"
            f"{META_END}\n\n")


# =============================================================================
# F2-10 — bölüm açılışındaki altyazısız görsel yalnız DOĞRULANMIŞ rozetse silinir
# =============================================================================

_BADGE_FIG = ("\\chapter{Ay'a Yolculuk}\n\n\\begin{figure}[H]\n\\centering\n"
              "\\includegraphics[width=0.8\\textwidth]{%s}\n\\end{figure}\n\n")


class TestF210BolumAcilisGorseli:
    def test_altyazisi_ardindan_gelen_gravur_silinmez(self):
        # bugav basliksil.py: EPUB/Gutenberg kalıbı, altyazı ayrı paragraf.
        body = (_BADGE_FIG % "images/bolum2_gravur.png"
                + "\\textbf{Şekil 2.1.} Top dökümhanesinin gravürü.\n\n"
                "Gun Club üyeleri toplantıya geldi.\n")
        for manual in (True, False):
            out, rep = _san(body, manual_translation=manual)
            assert "images/bolum2_gravur.png" in out
            assert "Top dökümhanesinin gravürü" in out
            assert not any("badge figure removed" in d for d in rep.details)

    def test_dogrulanamayan_marker_gorseli_korunur_ve_uyarilir(self):
        # Dosya bulunamıyor: rozet olduğu kanıtlanamaz -> kayıpsızlık.
        body = _BADGE_FIG % "_page_6_Picture_0.png" + "Metin burada sürer.\n"
        for manual in (True, False):
            out, rep = _san(body, manual_translation=manual)
            assert "_page_6_Picture_0.png" in out
            assert any("image not found to verify badge" in d
                       for d in rep.details)

    def test_buyuk_marker_gorseli_silinmez(self, tmp_path):
        # Bir test kitabının ithaf fotoğrafı 500x142 px (derlemde ölçüldü).
        _png(tmp_path / "_page_6_Picture_0.png", (500, 142))
        body = _BADGE_FIG % "_page_6_Picture_0.png" + "Metin.\n"
        out, rep = _san(body, image_dirs=[tmp_path])
        assert "_page_6_Picture_0.png" in out
        assert any("not badge-sized (500x142 px)" in d for d in rep.details)

    def test_kucuk_kare_marker_rozeti_silinir(self, tmp_path):
        # Yayınevi "Check for updates" rozeti (bir test kitabında 93x90 px). Manuel modda da.
        _png(tmp_path / "_page_22_Picture_1.png", (93, 90))
        body = _BADGE_FIG % "_page_22_Picture_1.png" + "\\section{Giriş}\n\nMetin.\n"
        for manual in (True, False):
            out, rep = _san(body, manual_translation=manual,
                            image_dirs=[tmp_path])
            assert "_page_22_Picture_1.png" not in out
            assert "\\chapter{Ay'a Yolculuk}" in out and "Metin." in out
            assert any("badge figure removed (1x)" in d for d in rep.details)

    def test_rozet_meta_output_dir_ile_dogrulanir(self, tmp_path):
        # 2_derle gövdesi META başlığını taşır: output_dir/source/images.
        _png(tmp_path / "source" / "images" / "_page_55_Picture_1.png", (92, 95))
        body = (_meta(tmp_path) + _BADGE_FIG % "_page_55_Picture_1.png"
                + "Metin.\n")
        out, _ = _san(body, manual_translation=True)
        assert "_page_55_Picture_1.png" not in out

    def test_marker_adi_olmayan_kucuk_sus_gorseli_korunur(self, tmp_path):
        # EPUB süs görseli (es_cajal: 4845603505159690659_coles.jpg).
        _png(tmp_path / "coles.png", (90, 90))
        body = _BADGE_FIG % "coles.png" + "Las cualidades indispensables.\n"
        out, _ = _san(body, image_dirs=[tmp_path])
        assert "coles.png" in out


# =============================================================================
# F2-11 — tarih satırı üst bilgi değildir; manuel çeviride satır silinmez
# =============================================================================


class TestF211TarihSatiri:
    _BODY = ("\\chapter{Önsöz}\n\nBu kitabı hocama adıyorum.\n\n12 Nisan 1961\n\n"
             "Gagarin o gün Dünya'nın çevresinde bir tur attı.\n")

    def test_manuel_modda_tarih_korunur(self):
        out, rep = _san(self._BODY, manual_translation=True)
        assert "12 Nisan 1961" in out
        assert not any("Header artifact removed" in d for d in rep.details)

    def test_yerel_model_modunda_tarih_korunur(self):
        for date in ("12 Nisan 1961", "3 March 1998", "12 de abril de 1961",
                     "7 août 1914", "1. Mai 1890"):
            body = f"Mektup.\n\n{date}\n\nSevgili dostum.\n"
            out, _ = _san(body, manual_translation=False)
            assert date in out, date

    def test_yerel_model_modunda_gercek_ust_bilgi_silinir(self):
        body = ("Paragraf bir.\n\n88 6 Operator Networks\n\nParagraf iki.\n\n"
                "6.2 Operatorler 89\n\nParagraf üç.\n")
        out, rep = _san(body, manual_translation=False)
        assert "88 6 Operator Networks" not in out
        assert "6.2 Operatorler 89" not in out
        assert "Paragraf iki." in out

    def test_manuel_modda_ust_bilgi_satiri_silinmez_uyarilir(self):
        body = "Paragraf bir.\n\n88 6 Operator Networks\n\nParagraf iki.\n"
        out, rep = _san(body, manual_translation=True)
        assert "88 6 Operator Networks" in out
        assert any("Possible running header kept" in d for d in rep.details)


# =============================================================================
# F1-06 / F2-09 — kısa gerçek 1. bölüm numarasını korur
# =============================================================================

_PAR = ("Kaptan güverteye çıktı ve ufka baktı; rüzgâr kuzeyden esiyordu ve "
        "tayfalar yelkenleri topluyordu. " * 8)


class TestF209KisaIlkBolum:
    def test_kisa_bolumlu_kitapta_ilk_bolum_numarali_kalir(self):
        # bugav kisa_bolum.py: bölüm uzunluğu 1572 < 2000, \section yok.
        body = "".join(f"\\chapter{{{ad}}}\n\n{_PAR}\n\n{_PAR}\n\n"
                       for ad in ("Fırtına", "Liman", "Dönüş", "Son"))
        for manual in (True, False):
            out, rep = _san(body, manual_translation=manual)
            assert _chapters(out) == ["\\chapter{Fırtına}", "\\chapter{Liman}",
                                      "\\chapter{Dönüş}", "\\chapter{Son}"]
            assert any("Short first chapter kept numbered" in d
                       for d in rep.details)

    def test_epub_kaynaginda_kural_uygulanmaz(self, tmp_path):
        # F1-06: EPUB "Kapitel 1 Grundlagen" (etiket dönüştürücüde düşer).
        # Künye kadar kısa ilk bölüm bile EPUB'da yıldızlanmaz.
        body = ("\\chapter{Grundlagen}\n\nDie Wärmeleitung.\n\n"
                "\\chapter{Instationäre Leitung}\n\n" + _PAR + "\n")
        out, _ = _san(_meta(tmp_path, "epub") + body, manual_translation=True)
        assert "\\chapter{Grundlagen}" in out
        out, _ = _san(body, manual_translation=True, source_format="epub")
        assert "\\chapter{Grundlagen}" in out

    def test_etiketli_ilk_bolum_yildizlanmaz(self):
        # İlk bölümün önünde "Kapitel 1" etiketi: kesin gerçek bölüm.
        body = ("\\chapter{Kapitel 1}\n\n\\chapter{Grundlagen}\n\nKurz.\n\n"
                "\\chapter{Kapitel 2}\n\n\\chapter{Strahlung}\n\n" + _PAR + "\n")
        out, _ = _san(body, manual_translation=True)
        assert "\\chapter{Grundlagen}" in out
        assert "\\chapter*" not in out

    def test_kunye_bolum_etiketi_kaniti_ile_yildizlanir(self):
        # Bir test kitabında: künye, ardından "Chapter 1" etiketli bölüm.
        # Künye metni sentetiktir (aynı biçim: sürüm satırı, yazar + bölüm
        # adresi, öğretmen + e-posta; düz metin > _TITLE_PAGE_MAX_TEXT_CHARS).
        body = ("\\chapter{Celestial Navigation}\n\nII Edition 2021 - 22 - Ver. 2.4.7\n\n"
                "Author: Marta Ferrandi Dipartimento di Meccanica Applicata di Ancona "
                "e Dipartimento di Scienze e Tecnologie dello Spazio, Ateneo\n\n"
                "Teacher: Paolo Ricciardi e-mail: paolo.ricciardi@esempio.example\n\n"
                "\\chapter{Chapter 1}\n\n\\chapter{Two–Body Orbital Mechanics}\n\n"
                + _PAR + "\n")
        out, rep = _san(body, manual_translation=True)
        assert "\\chapter*{Celestial Navigation}" in out
        assert "\\chapter{Two–Body Orbital Mechanics}" in out
        assert any("evidence: label-1" in d for d in rep.details)

    def test_ithaf_on_madde_kaniti_ile_yildizlanir(self):
        body = ("\\chapter{Four pioneers who guided an early rocket effort}\n\n"
                + "Onlara adanmıştır; bu kitabın her sayfası onların emeğini "
                  "anar ve yolunu sürdürenlere ithaf edilir. " * 3 + "\n\n"
                "\\chapter{Preface}\n\n" + _PAR + "\n\n\\chapter{Introduction}\n\n"
                + _PAR + "\n")
        out, rep = _san(body, manual_translation=False)
        assert _chapters(out)[0].startswith("\\chapter*{Four pioneers")
        assert any("evidence: front-matter-next" in d for d in rep.details)

    def test_tek_satir_kunye_yildizlanir(self):
        out, rep = _san("\\chapter{Kitap Adı}\nYazar Adı\n"
                        "\\chapter{Birinci Bölüm}\nMetin.\n",
                        manual_translation=True)
        assert "\\chapter*{Kitap Adı}" in out
        assert any("evidence: title-page-text" in d for d in rep.details)

    def test_idempotent(self):
        body = "".join(f"\\chapter{{{ad}}}\n\n{_PAR}\n\n"
                       for ad in ("Fırtına", "Liman"))
        san = LatexSanitizer(manual_translation=True)
        once, _ = san.sanitize(body)
        twice, _ = san.sanitize(once)
        assert once == twice


# =============================================================================
# F2-06 — satır sonundaki '}%' yüzde de olabilir
# =============================================================================


class TestF206SatirSonuYuzde:
    @pytest.mark.parametrize("src,expected", [
        ("Verim \\textbf{50}%\nsonraki satır.", "Verim \\textbf{50}\\%\nsonraki satır."),
        ("Kayıp \\emph{yüzde 12}%\noranında kaldı.",
         "Kayıp \\emph{yüzde 12}\\%\noranında kaldı."),
        ("Ölçülen değer {12}%\nve devam.", "Ölçülen değer {12}\\%\nve devam."),
        ("Oran \\textcolor{red}{7,5}%\nidi.", "Oran \\textcolor{red}{7,5}\\%\nidi."),
    ])
    def test_duzyazi_yuzdesi_kacislanir(self, src, expected):
        for manual in (True, False):
            out, _ = _san(src, manual_translation=manual)
            assert out == expected

    @pytest.mark.parametrize("src", [
        "\\resizebox{\\textwidth}{!}{%\n\\begin{tabular}{l}a\\end{tabular}}",
        "\\begin{minipage}{0.4\\textwidth}a\\end{minipage}%\nb",
        "\\setcounter{page}{1}%\nMetin.",
        "\\textbf{Toplam}%\nMetin.",
        "\\hspace{2em}%\nMetin.",
    ])
    def test_makro_satir_sonu_yorumu_korunur(self, src):
        out, _ = _san(src, manual_translation=True)
        assert "\\%" not in out

    def test_idempotent(self):
        san = LatexSanitizer(manual_translation=True)
        once, _ = san.sanitize("Verim \\textbf{50}%\nolarak ölçüldü.")
        twice, _ = san.sanitize(once)
        assert once == twice


# =============================================================================
# F2-07 — "Şekil 3 incelendiğinde ..." gövde cümlesidir, altyazı değil
# =============================================================================

_DOC_HEAD = "\\documentclass{book}\n\\usepackage{graphicx}\n\\begin{document}\n"


class TestF207AltyaziSezgisi:
    @pytest.mark.parametrize("para", [
        "Şekil 3 incelendiğinde yanma odasındaki sıcaklığın düştüğü görülür.",
        "Şekil 3 bu durumu açıkça gösterir: basınç arttıkça itki de artar.",
        "Şekil 3 ve 4 aynı deneyin iki aşamasını gösterir.",
        "Figure 3 shows the pressure distribution.",
        "Abb. 3 zeigt den Temperaturverlauf.",
        "Рис. 3 показывает распределение давления.",
    ])
    def test_govde_cumlesi_altyazi_sayilmaz(self, para):
        assert not _caption_like_para(para, "figure")

    @pytest.mark.parametrize("para", [
        "Şekil 3. Yanma odasının kesiti.",
        "Şekil 3: Yanma odasının kesiti.",
        "Şekil 3 Yanma odasının kesiti",
        "Şekil 3a Üst görünüm.",
        "Şekil 3 a) üst görünüm, b) yan görünüm.",
        "Şekil 3 (a) üst görünüm.",
        "\\textbf{Şekil 2.1.} Top dökümhanesinin gravürü.",
        "Figure 2.3 The architecture of the network.",
        "Рис. 3. Схема двигателя.",
    ])
    def test_gercek_altyazi_taninir(self, para):
        assert _caption_like_para(para, "figure")

    def test_govde_cumlesi_floata_gomulmez(self):
        doc = (_DOC_HEAD + "\\chapter{Deneme}\nGiriş paragrafı.\n\n"
               "\\begin{figure}[H]\n\\centering\n"
               "\\includegraphics[width=0.5\\textwidth]{images/a.png}\n"
               "\\end{figure}\n\n"
               "Şekil 3 incelendiğinde basınç arttıkça itkinin de arttığı görülür "
               "(GOVDE-CUMLESI).\n\nSonraki paragraf (SONRAKI-PARAGRAF).\n\n"
               "\\end{document}\n")
        out, rep = _san(doc, manual_translation=True)
        # G3a-1: görsel yerinde ölçülü birimdir (\\setbox0\\vbox); gövde
        # cümlesi birimin DIŞINDA, ardında kalır.
        assert out.index("\\vskip\\intextsep\\par") < out.index("GOVDE-CUMLESI")
        # Okuma sırası: gövde cümlesi sonraki paragraftan önce, float dışında.
        assert out.index("GOVDE-CUMLESI") < out.index("SONRAKI-PARAGRAF")
        assert not any("one float" in d for d in rep.details)

    def test_gercek_altyazi_yine_yapistirilir(self):
        doc = (_DOC_HEAD + "\\chapter{Deneme}\nGiriş.\n\n"
               "\\begin{figure}[H]\n\\centering\n"
               "\\includegraphics[width=0.5\\textwidth]{images/a.png}\n"
               "\\end{figure}\n\n"
               "Şekil 3. Yanma odasının kesiti.\n\nSonraki paragraf.\n\n"
               "\\end{document}\n")
        out, rep = _san(doc, manual_translation=True)
        assert "\\setbox0\\vbox" in out
        assert any("one float" in d for d in rep.details)


# =============================================================================
# F2-13 — kapak eklenirken gövdedeki kod örnekleri değişmez
# =============================================================================

_LATEX_ANLATAN = r"""\begin{titlepage}
{\Huge\bfseries Kitap}\par
\end{titlepage}

\chapter{İlk belge}
En küçük LaTeX belgesi şöyledir:
\begin{verbatim}
\documentclass[,11pt]{article}
\usepackage{pdfpages}
\begin{document}
\begin{titlepage}
Başlık
\end{titlepage}
Merhaba dünya!
\end{document}
\end{verbatim}
"""


def _cover_pdf(tmp_path: Path) -> str:
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "Kapak")
    path = tmp_path / "kapak.pdf"
    doc.save(str(path))
    doc.close()
    return str(path)


class TestF213KapakEkleme:
    def test_verbatim_ornegi_degismez(self, tmp_path):
        tex = TemplateEngine(PipelineConfig()).wrap_content(
            _LATEX_ANLATAN, cover_pdf_path=_cover_pdf(tmp_path), cover_pages=[1])
        v0 = tex.index("\\begin{verbatim}")
        verb = tex[v0:tex.index("\\end{verbatim}", v0)]
        assert verb == _LATEX_ANLATAN[_LATEX_ANLATAN.index("\\begin{verbatim}"):
                                      _LATEX_ANLATAN.index("\\end{verbatim}")]

    def test_pdfpages_onsoze_bir_kez_eklenir(self, tmp_path):
        # Gövdedeki kod örneğinde '\usepackage{pdfpages}' olsa da önsöze eklenir.
        tex = TemplateEngine(PipelineConfig()).wrap_content(
            _LATEX_ANLATAN, cover_pdf_path=_cover_pdf(tmp_path), cover_pages=[1])
        preamble = tex[:tex.index("\\begin{document}")]
        assert preamble.count("\\usepackage{pdfpages}") == 1
        assert tex.count("\\includepdf") == 1

    def test_on_bolgedeki_baslik_sayfasi_kalkar_ornek_kalir(self, tmp_path):
        tex = TemplateEngine(PipelineConfig()).wrap_content(
            _LATEX_ANLATAN, cover_pdf_path=_cover_pdf(tmp_path), cover_pages=[1])
        assert "{\\Huge\\bfseries Kitap}" not in tex
        assert tex.count("\\begin{titlepage}") == 1  # verbatim içindeki
        assert "Başlık\n\\end{titlepage}" in tex

    def test_kapaksiz_govde_aynen_yerlesir(self):
        tex = TemplateEngine(PipelineConfig()).wrap_content(_LATEX_ANLATAN)
        assert _LATEX_ANLATAN in tex


# =============================================================================
# F2-05 — matematikte liste virgülü
# =============================================================================


class TestF205ListeVirgulu:
    @pytest.mark.parametrize("src,expected,english", [
        ("$[-1,1]$", "$[-1, 1]$", False),
        ("$[0,1)$", "$[0, 1)$", False),
        ("$i=1,2,\\ldots,n$", "$i=1, 2,\\ldots,n$", False),
        ("$x\\in\\{1,2,3\\}$", "$x\\in\\{1, 2, 3\\}$", False),
        ("$998,2$", "$998,2$", False),               # Türkçe ondalık
        ("$0,1, 0,2$", "$0,1, 0,2$", False),         # ondalıklı liste
        ("$(0,5)$", "$(0,5)$", False),               # belirsiz: dokunulmaz
        ("$(0,0)$", "$(0, 0)$", True),               # İngilizce: koordinat
        ("$1,000,000$", "$1,000,000$", True),        # binlik ayracı
        ("$\\text{1,5 kg}+[0,1]$", "$\\text{1,5 kg}+[0, 1]$", False),
    ])
    def test_kurallar(self, src, expected, english):
        from latex_pipeline.sanitizer import space_list_commas
        assert space_list_commas(src, english_source=english)[0] == expected

    def test_yalniz_tam_belgede_uygulanir(self):
        body = "Aralık $[-1,1]$ ve nokta $(0,0)$.\n"
        out, _ = _san(body, manual_translation=True)
        assert out == body  # Phase 5.5 gövdesi (QC/EPUB) değişmez
        doc = _DOC_HEAD + body + "\\end{document}\n"
        out, rep = _san(doc, manual_translation=True)
        assert "$[-1, 1]$" in out and "$(0,0)$" in out
        assert any("Math list comma spaced" in d for d in rep.details)

    def test_ingilizce_kurali_acik_kaynak_dil_ister(self):
        doc = _DOC_HEAD + "Nokta $(0,0)$.\n\\end{document}\n"
        assert "$(0,0)$" in _san(doc)[0]  # varsayılan "en" bilinmiyor sayılır
        assert "$(0, 0)$" in _san(doc, source_lang="en")[0]
        assert "$(0,0)$" in _san(doc, source_lang="de")[0]

    def test_idempotent(self):
        doc = _DOC_HEAD + "$[-1,1]$, $i=1,2,\\ldots,n$\n\\end{document}\n"
        once, _ = _san(doc)
        assert once == _san(once)[0]


# =============================================================================
# L-1 + F2-05 — gerçek XeLaTeX derlemesi (şablon + Phase 6 sanitizer)
# =============================================================================

_TR_BODY = r"""\chapter{Kod}

\begin{lstlisting}
# Bu işlev değeri değiştirebilir: ığ şç İĞŞ ÖÇÜ
\end{lstlisting}

\begin{lstlisting}[language=Python, caption={Örnek işlev çağrısı}, label={lst:ornek}]
sonuç = hesapla(3)  # ağaç düğümü Ğ
\end{lstlisting}

\begin{lstlisting}[basicstyle=\ttfamily\footnotesize, frame=tb, numbers=none, showstringspaces=false]
satır: ığüşöçĞÜŞİÖÇ
\end{lstlisting}

Atıf: Kod~\ref{lst:ornek}.

\newlength{\kcA}\newlength{\kcB}
\settowidth{\kcA}{$[-1,1]$}\settowidth{\kcB}{$[-1{,}1]$}
\typeout{KCWIDTH A=\the\kcA\space B=\the\kcB}
"""


@needs_tex
class TestL1XelatexDerleme:
    @pytest.fixture(scope="class")
    def compiled(self, tmp_path_factory):
        d = tmp_path_factory.mktemp("l1")
        tex = TemplateEngine(PipelineConfig()).wrap_content(_TR_BODY)
        tex, _ = LatexSanitizer(manual_translation=True).sanitize(tex)
        (d / "l1.tex").write_text(tex, encoding="utf-8")
        for _ in range(2):
            subprocess.run(["xelatex", "-interaction=nonstopmode", "l1.tex"],
                           cwd=d, capture_output=True, timeout=240)
        log = (d / "l1.log").read_text(encoding="utf-8", errors="replace")
        text = subprocess.run(["pdftotext", "-layout", str(d / "l1.pdf"), "-"],
                              capture_output=True, text=True).stdout
        return log, text

    def test_derleme_hatasiz(self, compiled):
        log, _ = compiled
        assert not [l for l in log.splitlines() if l.startswith("!")]

    @pytest.mark.parametrize("parca", [
        "değiştirebilir: ığ şç İĞŞ ÖÇÜ",
        "ağaç düğümü Ğ",
        "satır: ığüşöçĞÜŞİÖÇ",
        "Kod 1: Örnek işlev çağrısı",
        "Atıf: Kod 1",
    ])
    def test_turkce_harfler_dogru(self, compiled, parca):
        assert parca in compiled[1]

    def test_liste_virgulu_ondaliktan_genis(self, compiled):
        # Şablonun ondalık virgülü '[-1{,}1]' ile aynı genişlikte değil.
        m = re.search(r"KCWIDTH A=([\d.]+)pt B=([\d.]+)pt", compiled[0])
        assert m and float(m.group(1)) > float(m.group(2)) + 0.5
