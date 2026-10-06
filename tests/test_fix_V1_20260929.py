"""V1 denetimi ("PDF'den TeX çıkar" tarafı) — 29 Eylül 2026.

Kulvar raporlarının kod denetiminde AÇIK kalan kusurların regresyon testleri.

  F1-09 (tamamlama)  EPUB ayrıştırıcısı düz metindeki ters bölüyü "\\\\",
                     ters tırnağı "\\`", "(" önündeki kapanış köşeli ayracını
                     "\\]" yazar (parsers.epub_parser.md_escape sözleşmesi).
                     Dönüştürücü bu üçünü çözmüyordu: "C:\\\\Users" TeX satır
                     sonu olup ters bölüler kayboluyordu; "[see here\\](not a
                     link)" "\\href{not a link}{see here\\}" olup derleme
                     "Paragraph ended before \\href@split" ile duruyor,
                     paragrafın kalanı PDF'ten düşüyordu; "\\`x\\`" kod parçası
                     sanılıp "\\texttt{x\\}" oluyordu. Tablo hücresindeki "\\\\"
                     tabular satırını bitiriyordu.

Girdiler SENTETİKTİR. Gerçek model yüklenmez.
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from latex_pipeline.config import PipelineConfig
from latex_pipeline.converter import MarkdownToLatex
from parsers.epub_parser import md_escape

_HEAD = "# Chapter 1 Sentetik\n\nSentetik gövde paragrafı burada yer alır.\n\n"


@pytest.fixture
def conv(tmp_path):
    def _run(md: str) -> str:
        cfg = PipelineConfig(document_class="book", output_dir=str(tmp_path))
        out = MarkdownToLatex(cfg).convert(_HEAD + md + "\n")
        assert "\x00" not in out and "\x02" not in out, "yer tutucu sızdı"
        return out.split("Sentetik gövde paragrafı burada yer alır.", 1)[1]
    return _run


# ---------------------------------------------------------------------------
# F1-09: EPUB düz metin kaçışları (dönüştürücü tarafı)
# ---------------------------------------------------------------------------

class TestF109EpubKacislari:
    def test_ters_bolu_duz_karakter(self, conv):
        md = md_escape(r"The path is C:\Users\data and \d{3} matches.")
        out = conv(md)
        assert r"C:\textbackslash{}Users\textbackslash{}data" in out
        assert r"\textbackslash{}d\{3\}" in out
        assert "\\\\U" not in out and "\\\\d" not in out

    def test_kacisli_kose_ayrac_baglanti_olmaz(self, conv):
        md = md_escape("The literal text [see here](not a link) must stay.")
        assert r"\](" in md
        out = conv(md)
        # "{]}": basılan "]"; sanitizer'ın bağ kalıntısı temizliği görmesin.
        assert "[see here{]}(not a link) must stay." in out
        assert r"\href" not in out

    def test_kacisli_ters_tirnak_kod_olmaz(self, conv):
        md = md_escape("Use `tick` here, not code.")
        out = conv(md)
        assert "Use `tick` here, not code." in out
        assert r"\texttt" not in out

    def test_ters_bolu_ardindan_kacisli_yildiz(self, conv):
        md = md_escape(r"Glob \*.txt and escaped \_name here.")
        out = conv(md)
        assert r"Glob \textbackslash{}*.txt" in out
        assert r"escaped \textbackslash{}\_name" in out

    def test_iki_ters_bolu(self, conv):
        out = conv(md_escape(r"UNC path \\server\share here."))
        assert r"\textbackslash{}\textbackslash{}server\textbackslash{}share" in out

    def test_baglanti_metninde_ters_bolu(self, conv):
        md = "[" + md_escape(r"C:\dir") + "](http://example.org/x) link."
        out = conv(md)
        assert r"\href{http://example.org/x}{C:\textbackslash{}dir}" in out

    def test_baslikta_ters_bolu(self, conv):
        out = conv("## " + md_escape(r"Path C:\Temp") + "\n\nMetin.")
        assert re.search(r"\\section\*?\{Path C:\\textbackslash\{\}Temp\}", out), out

    def test_tablo_hucresinde_ters_bolu_satiri_bitirmez(self, conv):
        md = ("| Yol | Not |\n|---|---|\n| " + md_escape(r"C:\x") + " | "
              + md_escape("a`b") + " |\n")
        out = conv(md)
        row = [l for l in out.splitlines() if l.startswith("C:")]
        assert row == [r"C:\textbackslash{}x & a`b \\"], out

    # --- koruma: eski davranış sürer ---------------------------------------

    def test_matematik_icindeki_satir_sonu_korunur(self, conv):
        out = conv(r"Inline $a \\ b$ math.")
        assert r"$a \\ b$" in out

    def test_marker_cift_kacisli_alt_cizgi_degismez(self, conv):
        # Marker'ın "\\_" çift kaçışı ters bölü kuralına girmez (EPUB yolu
        # bunu üretmez); eski çıktı neyse o.
        out = conv(r"Marker George\\_Graham plain.")
        assert r"\textbackslash" not in out

    def test_kod_parcasina_dokunulmaz(self, conv):
        # Kod parçasında Markdown kaçışı uygulanmaz: iki ters bölü de basılır
        # (2026-10-05: eskiden TeX'e "\\" satır sonu olarak geçiyordu).
        out = conv(r"Code `a\\b` span.")
        assert r"\texttt{a\textbackslash{}\textbackslash{}b}" in out

    def test_kacisli_yildiz_ve_alt_cizgi_eskisi_gibi(self, conv):
        out = conv(md_escape("2*3*4 and _private_ helpers"))
        assert r"2*3*4 and \_private\_ helpers" in out


@pytest.mark.skipif(not (shutil.which("xelatex") and shutil.which("pdftotext")),
                    reason="xelatex/pdftotext yok")
class TestF109Derleme:
    """Gerçek şablon + manuel sanitizer + XeLaTeX: TeX hatası yok, metin tam."""

    SATIRLAR = [
        r"TERSBOLU A US phone pattern is \+?1?\s?(\d{3}) and the path is C:\Users\data in Windows.",
        "KOSELI The literal text [see here](not a link) must stay as typed.",
        "TIRNAK Use `tick` marks as typed.",
        "YILDIZ The product 2*3*4 equals 24 and a*b is commutative.",
    ]

    @pytest.fixture(scope="class")
    def derlenmis(self, tmp_path_factory):
        from latex_pipeline.sanitizer import LatexSanitizer
        from latex_pipeline.template_engine import TemplateEngine
        d = tmp_path_factory.mktemp("f109")
        md = _HEAD + "\n\n".join(md_escape(s) for s in self.SATIRLAR) + "\n"
        body = MarkdownToLatex(PipelineConfig(document_class="book",
                                              output_dir=str(d))).convert(md)
        tex = TemplateEngine(PipelineConfig()).wrap_content(body)
        tex, _ = LatexSanitizer(manual_translation=True).sanitize(tex)
        (d / "f109.tex").write_text(tex, encoding="utf-8")
        subprocess.run(["xelatex", "-interaction=nonstopmode", "f109.tex"],
                       cwd=d, capture_output=True, timeout=240)
        log = (d / "f109.log").read_text(encoding="utf-8", errors="replace")
        text = subprocess.run(["pdftotext", str(d / "f109.pdf"), "-"],
                              capture_output=True, text=True).stdout
        return log, re.sub(r"\s+", " ", text)

    def test_tex_hatasi_yok(self, derlenmis):
        log, _ = derlenmis
        assert not [l for l in log.splitlines() if l.startswith("!")]

    @pytest.mark.parametrize("parca", [
        r"C:\Users\data in Windows.",
        r"\+?1?\s?(\d{3})",
        "[see here](not a link) must stay as typed.",
        "2*3*4 equals 24",
    ])
    def test_metin_pdfte_tam(self, derlenmis, parca):
        assert parca in derlenmis[1]


# ---------------------------------------------------------------------------
# F1-10 (sağlamlaştırma): çitli kod bloğundaki "# yorum" satırı sayfa dipnotu
# eşlemesini durdurmamalı.
# ---------------------------------------------------------------------------

class TestF110KodBlogu:
    MD = ("Dieses Verhältnis heißt Biot-Zahl,<sup>1</sup> und wird oft benutzt.\n\n"
          "Das folgende Programm berechnet sie:\n\n"
          "```python\n"
          "# Biot-Zahl berechnen\n"
          "def biot(alpha, l, lam):\n"
          "    return alpha * l / lam\n"
          "```\n\n"
          "Weiterer Text folgt hier im Absatz.\n\n"
          "<sup>1</sup>Benannt nach dem französischen Physiker Jean-Baptiste Biot.\n")

    def test_kod_yorumu_eslemeyi_durdurmaz(self, conv):
        out = conv(self.MD)
        # KARAR 31 (FX1): eşlenen dipnot kaynak numarasıyla.
        assert (r"Biot-Zahl,\footnote[1]{Benannt nach dem französischen Physiker "
                r"Jean-Baptiste Biot.}") in out
        assert r"\textsuperscript{1}Benannt" not in out
        # kod bloğu olduğu gibi
        assert "# Biot-Zahl berechnen" in out and r"\begin{lstlisting}" in out

    def test_kod_icindeki_tanim_benzeri_satir_tanim_sayilmaz(self, conv):
        md = ("Text mit Marke.<sup>2</sup> Weiter.\n\n"
              "```\n<sup>2</sup>kein Fußnotentext im Code\n```\n\n"
              "Schluss.\n")
        out = conv(md)
        assert r"\footnote{kein" not in out and r"\footnote[2]{kein" not in out


# ---------------------------------------------------------------------------
# L-1 (gerileme): fvextra breaklines yalnız boşlukta kırıyordu; kod
# bloğundaki boşluksuz uzun belirteç sayfa kenarından taşıp kesiliyordu
# (eski listings her yerde kırıyordu). Sanitizer da kod bloğundaki ham
# URL'yi "\url{…}" ile sarıp koda düz metin olarak "\url{" basıyordu.
# ---------------------------------------------------------------------------

_UZUN_URL = ("https://raw.githubusercontent.com/ornekkullanici/OrnekDepo/"
             "master/05-1-kol-katlama-verisi/05-1-kol-katlama-verisi-xyz.csv")
_KOD_GOVDE = (
    "\\chapter{Deneme}\nUzun bir satır:\n\n"
    "\\begin{lstlisting}[language=R]\n"
    "# veriyi oku (değiştirebilir)\n"
    f'veri <- read.csv(url("{_UZUN_URL}"))\n'
    "\\end{lstlisting}\n\n"
    f"Metindeki adres {_UZUN_URL} burada.\n"
)


def test_kod_blogundaki_url_sarilmaz():
    from latex_pipeline.sanitizer import LatexSanitizer
    out, rep = LatexSanitizer(manual_translation=True).sanitize(_KOD_GOVDE)
    code = out[out.index("\\begin{lstlisting}"):out.index("\\end{lstlisting}")]
    assert "\\url{" not in code
    assert f'read.csv(url("{_UZUN_URL}"))' in code
    # metindeki ham adres eskisi gibi \url ile sarılır
    assert f"\\url{{{_UZUN_URL}}}" in out[out.index("\\end{lstlisting}"):]
    assert "Raw URLs wrapped in \\url (1x)" in rep.details


@pytest.mark.skipif(not (shutil.which("xelatex") and shutil.which("pdftotext")),
                    reason="xelatex/pdftotext yok")
class TestL1UzunKodSatiri:
    @pytest.fixture(scope="class")
    def derlenmis(self, tmp_path_factory):
        from latex_pipeline.sanitizer import LatexSanitizer
        from latex_pipeline.template_engine import TemplateEngine
        d = tmp_path_factory.mktemp("l1uzun")
        tex = TemplateEngine(PipelineConfig()).wrap_content(_KOD_GOVDE)
        tex, _ = LatexSanitizer(manual_translation=True).sanitize(tex)
        (d / "k.tex").write_text(tex, encoding="utf-8")
        subprocess.run(["xelatex", "-interaction=nonstopmode", "k.tex"],
                       cwd=d, capture_output=True, timeout=240)
        log = (d / "k.log").read_text(encoding="utf-8", errors="replace")
        text = subprocess.run(["pdftotext", str(d / "k.pdf"), "-"],
                              capture_output=True, text=True).stdout
        return log, text

    def test_tasma_yok(self, derlenmis):
        log, _ = derlenmis
        assert not [l for l in log.splitlines() if l.startswith("!")]
        genis = [float(x) for x in re.findall(
            r"Overfull \\hbox \(([\d.]+)pt too wide\)", log)]
        assert not [g for g in genis if g > 5], genis

    def test_belirtec_tam_ve_turkce_harf_dogru(self, derlenmis):
        _, text = derlenmis
        assert "değiştirebilir" in text
        # kırılan satırların devam işaretleri atılınca belirteç tam
        joined = re.sub(r"[\s↪⌋]+", "", text)
        assert _UZUN_URL in joined


# ---------------------------------------------------------------------------
# F5-15 (EPUB parçası): "[EPUB Output] Hizalama: … (kapsam %100.0; …
# uzunluk yayılımı 0,09 …)" satırında noktalı ondalık — Türkçe virgül.
# ---------------------------------------------------------------------------

def test_epub_hizalama_satiri_turkce_ondalik(tmp_path):
    import contextlib
    import io
    from ebooklib import epub as E
    from latex_pipeline import epub_output as eo
    from parsers.epub_parser import EPUBParser

    govde = "".join(
        f"<p>Le paragraphe numéro {i} raconte une longue histoire de canon "
        f"et de voyage vers la Lune pour les membres du club.</p>"
        for i in range(6))
    b = E.EpubBook()
    b.set_identifier("kc-v1-f515")
    b.set_title("Essai")
    b.set_language("fr")
    c = E.EpubHtml(title="Chapitre", file_name="ch1.xhtml", lang="fr")
    c.content = f"<h1>Chapitre premier</h1>{govde}"
    b.add_item(c)
    b.toc = (c,)
    b.spine = [c]
    b.add_item(E.EpubNcx())
    b.add_item(E.EpubNav())
    src = tmp_path / "k.epub"
    E.write_epub(str(src), b)

    p = EPUBParser()
    with contextlib.redirect_stdout(io.StringIO()):
        doc = p.parse(str(src))
        body = MarkdownToLatex(PipelineConfig(document_class="book")).convert(
            p.to_markdown(doc))
    out = tmp_path / "kitap"
    (out / "source").mkdir(parents=True)
    (out / "source" / "body.tex").write_text(body, encoding="utf-8")
    ceviri = body.replace("Le paragraphe numéro", "Numaralı paragraf").replace(
        "raconte une longue histoire de canon et de voyage vers la Lune pour "
        "les membres du club", "kulüp üyeleri için top ve Ay yolculuğu üzerine "
        "uzun bir öykü anlatır")
    (out / "source" / "translated_body.tex").write_text(ceviri, encoding="utf-8")
    shutil.copy(src, out / "source_book.epub")
    with contextlib.redirect_stdout(io.StringIO()) as buf:
        eo.generate_epub_output(str(out / "source_book.epub"), str(out), "kitap")
    satir = [l for l in buf.getvalue().splitlines() if "Hizalama:" in l]
    assert satir, buf.getvalue()
    m = re.search(r"kapsam %([\d.,]+);", satir[0])
    assert m and "." not in m.group(1) and "," in m.group(1), satir[0]


# ---------------------------------------------------------------------------
# F1-01 tablo sözleşmesi (E §3): EPUB tabloları markdown tablosu olarak
# geliyor. (1) Hücredeki biçim ("*x*", "**y**", "[bağ](adres)") düz
# basılıyordu; (2) başlıksız tablonun boş başlık satırı + \midrule
# basılıyordu; (3) uzun metinli hücreli tablo 'l' sütunlarında sayfanın
# sağından taşıyor, sağdaki sütunlar PDF'te görünmüyordu.
# ---------------------------------------------------------------------------

_ANLATI_TABLO = (
    "|  |  |  |  |\n|---|---|---|---|\n"
    "| 1º | Que les fonds nécessaires à l'entreprise du Gun-Club ne seraient "
    "pas faits, ci... | 1000 | dollars |\n"
    "| 2º | Que l'opération de la fonte d'un canon de neuf cents pieds était "
    "impraticable et ne réussirait pas, ci............ | 2000 | SONSUTUN |\n")


class TestTabloHucresi:
    def test_hucre_bicimi_donusur(self, conv):
        out = conv("| A | B |\n|---|---|\n| Hücre *x* ve **y** | "
                   "[bağ](http://example.org/a) \\$5 |\n")
        assert r"Hücre \textit{x} ve \textbf{y} & \href{http://example.org/a}{bağ} \$5 \\" in out

    def test_bos_baslik_satiri_basilmaz(self, conv):
        out = conv("|  |  |\n|---|---|\n| a | b |\n")
        body = out[out.index(r"\toprule"):out.index(r"\bottomrule")]
        assert r"\midrule" not in body and " &  \\\\" not in body
        assert r"a & b \\" in body

    def test_bicimsiz_marker_hucresi_eskisi_gibi(self, conv):
        out = conv("| Body | Period |\n|---|---|\n| Rotational period* | $a*b$ |\n"
                   "| 2.5 | x_1 |\n")
        assert r"Rotational period* & $a*b$ \\" in out
        assert r"2.5 & x\_1 \\" in out
        assert r"Body & Period \\" in out and r"\midrule" in out


class TestGenisTablo:
    def _san(self, body, full=True):
        from latex_pipeline.sanitizer import LatexSanitizer
        from latex_pipeline.template_engine import TemplateEngine
        if full:
            body = TemplateEngine(PipelineConfig()).wrap_content(body)
        return LatexSanitizer(manual_translation=True).sanitize(body)

    def test_uzun_sutun_kirilir(self, conv):
        tex, rep = self._san("\\chapter{D}\nMetin.\n\n" + conv(_ANLATI_TABLO))
        spec = re.search(r"\\begin\{tabular\}\{(.*)\}\n", tex).group(1)
        assert spec.startswith("l >{\\raggedright\\arraybackslash}p{0.")
        assert spec.endswith("\\linewidth} l l")
        assert "\\begin{kcfit}\\begin{tabular}" in tex
        assert "\\end{tabular}\\end{kcfit}" in tex
        assert "Wide table fitted to page width (1x)" in rep.details

    _DAR = ("\\begin{table}[H]\n\\centering\n\\begin{tabular}{l l}\n\\toprule\n"
            "A & B \\\\\n\\midrule\n1 & 2 \\\\\n\\bottomrule\n\\end{tabular}\n\\end{table}\n")

    def test_dar_tablo_dokunulmaz(self):
        tex, rep = self._san("\\chapter{D}\n" + self._DAR)
        assert "Wide table" not in " ".join(rep.details)
        assert "kcfit" not in tex.split("\\begin{document}")[1]

    def test_sayisal_izgara_kirilmaz_sigdirilir(self):
        izgara = self._DAR.replace("{l l}", "{" + " ".join(["l"] * 12) + "}").replace(
            "1 & 2", " & ".join(["123456"] * 12)).replace("A & B", " & ".join(["Sütun"] * 12))
        tex, rep = self._san("\\chapter{D}\n" + izgara)
        govde = tex.split("\\begin{document}")[1]
        assert "p{0." not in govde
        assert "\\begin{kcfit}\\begin{tabular}{" + " ".join(["l"] * 12) + "}" in govde

    def test_govde_asamasinda_dokunulmaz(self, conv):
        # Phase 5.5 (QC/EPUB'un okuduğu gövde) değişmez; yalnız tam belge.
        tex, rep = self._san("\\chapter{D}\n" + conv(_ANLATI_TABLO), full=False)
        assert "p{0." not in tex and "kcfit" not in tex

    def test_kod_ornegindeki_tabloya_dokunulmaz(self):
        kod = ("\\begin{verbatim}\n\\begin{tabular}{l l}\n"
               + "x" * 90 + " & y \\\\\n\\end{tabular}\n\\end{verbatim}\n")
        tex, _ = self._san("\\chapter{D}\n" + kod)
        assert "\\begin{tabular}{l l}\n" + "x" * 90 in tex


@pytest.mark.skipif(not (shutil.which("xelatex") and shutil.which("pdftotext")),
                    reason="xelatex/pdftotext yok")
def test_genis_tablo_derlemede_tasmaz(tmp_path):
    from latex_pipeline.sanitizer import LatexSanitizer
    from latex_pipeline.template_engine import TemplateEngine
    izgara = ("| " + " | ".join(f"Sütun başlığı {k}" for k in range(11)) + " |\n|"
              + "---|" * 11 + "\n| " + " | ".join(f"{k}.2345" for k in range(10))
              + " | IZGARASON |\n")
    md = _HEAD + _ANLATI_TABLO + "\nAra paragraf.\n\n" + izgara
    body = MarkdownToLatex(PipelineConfig(document_class="book",
                                          output_dir=str(tmp_path))).convert(md)
    tex = TemplateEngine(PipelineConfig()).wrap_content(body)
    tex, _ = LatexSanitizer(manual_translation=True).sanitize(tex)
    (tmp_path / "t.tex").write_text(tex, encoding="utf-8")
    subprocess.run(["xelatex", "-interaction=nonstopmode", "t.tex"],
                   cwd=tmp_path, capture_output=True, timeout=240)
    log = (tmp_path / "t.log").read_text(encoding="utf-8", errors="replace")
    assert not [l for l in log.splitlines() if l.startswith("!")]
    genis = [float(x) for x in re.findall(r"Overfull \\hbox \(([\d.]+)pt too wide\)", log)]
    assert not [g for g in genis if g > 5], genis
    # sağdaki sütun sayfa içinde (-layout: sayfanın dışındaki metin çıkmaz)
    text = subprocess.run(["pdftotext", "-layout", str(tmp_path / "t.pdf"), "-"],
                          capture_output=True, text=True).stdout
    assert "SONSUTUN" in text and "2000" in text
    assert "IZGARASON" in text and "Sütun başlığı 10" in text
