"""Dalga G (2026-09-25): görsel yüksekliğine üst sınır (G1).

Kanıt: JRD_Bolum_1 (Rusça taranmış bölüm) — 0.8\\textwidth genişlikteki
uzun-dar şemalar \\textheight'tan uzun çıkıyor, [!htb] şekiller hiçbir sayfaya
sığmayıp bölüm sonuna itiliyordu (38 sayfalık kaynak 86 sayfa, s. 67-86 her
sayfada tek şekil; altyazılar yerinde kalıyordu).

Düzeltme: sanitizer, şablona sarılmış TAM belgede (Phase 6, derlemeden hemen
önce) genişlik verilmiş her ``\\includegraphics``'e
``height=FIGURE_MAX_HEIGHT,keepaspectratio`` ekler. Yalnız küçültür, genişlik
davranışını değiştirmez; Phase 5.5 gövdesine (QC/autofix girdisi) dokunmaz.
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

import latex_pipeline.sanitizer as sanitizer_mod
from latex_pipeline.config import PipelineConfig
from latex_pipeline.sanitizer import (
    FIGURE_MAX_HEIGHT,
    LatexSanitizer,
    cap_includegraphics_height,
)
from latex_pipeline.template_engine import TemplateEngine

CAP = r",height=0.7\textheight,keepaspectratio]"


def _doc(body: str, preamble: str = "") -> str:
    """Şablon benzeri tam belge (\\documentclass ile başlar)."""
    return (
        "%% deneme şablonu\n"
        "\\documentclass{book}\n\\usepackage{graphicx}\n" + preamble
        + "\\begin{document}\n" + body + "\n\\end{document}\n"
    )


def _san(text: str, manual: bool = True):
    return LatexSanitizer(manual_translation=manual).sanitize(text)


# ---------------------------------------------------------------------------
# Sabit
# ---------------------------------------------------------------------------

class TestSabit:
    def test_tek_sabit_degeri(self):
        assert FIGURE_MAX_HEIGHT == r"0.7\textheight"

    def test_cikti_sabitten_okunur(self, monkeypatch):
        monkeypatch.setattr(sanitizer_mod, "FIGURE_MAX_HEIGHT",
                            r"0.5\textheight")
        out, n = cap_includegraphics_height(
            r"\includegraphics[width=0.8\textwidth]{a.png}")
        assert n == 1
        assert r"height=0.5\textheight" in out


# ---------------------------------------------------------------------------
# cap_includegraphics_height: hangi görsele dokunulur, hangisine dokunulmaz
# ---------------------------------------------------------------------------

class TestSecenekDuzeltmesi:
    def test_genislik_verilmis_gorsele_sinir_eklenir(self):
        out, n = cap_includegraphics_height(
            r"\includegraphics[width=0.8\textwidth]{_page_3_Figure_4.png}")
        assert n == 1
        assert out == (r"\includegraphics[width=0.8\textwidth,"
                       r"height=0.7\textheight,keepaspectratio]"
                       r"{_page_3_Figure_4.png}")

    def test_yildizli_ve_bosluklu_bicim(self):
        out, n = cap_includegraphics_height(
            "\\includegraphics* [ width = 5cm ] {a.png}")
        assert n == 1
        assert out.startswith("\\includegraphics* [ width = 5cm,height=")

    def test_seceneksiz_gorsel_degismez(self):
        # Yalnız yükseklik vermek küçük görseli büyütürdü.
        text = r"\includegraphics{kucuk.png}"
        assert cap_includegraphics_height(text) == (text, 0)

    @pytest.mark.parametrize("opts", [
        r"height=1em",
        r"width=2cm,height=3cm",
        r"width=2cm,totalheight=3cm",
        r"width=2cm,scale=0.5",
        r"angle=90,width=2cm",
        r"width=2cm,origin=c",
        r"width=2cm,keepaspectratio=false",
        r"width=!",
        r"width=",
        r"scale=0.4",
    ])
    def test_dokunulmayan_secenekler(self, opts):
        text = "\\includegraphics[" + opts + "]{a.png}"
        assert cap_includegraphics_height(text) == (text, 0)

    def test_keepaspectratio_varsa_yalniz_yukseklik_eklenir(self):
        out, n = cap_includegraphics_height(
            r"\includegraphics[width=\linewidth,keepaspectratio]{a.png}")
        assert n == 1
        assert out == (r"\includegraphics[width=\linewidth,keepaspectratio,"
                       r"height=0.7\textheight]{a.png}")

    def test_suslu_parantez_icindeki_virgul_anahtar_sayilmaz(self):
        out, n = cap_includegraphics_height(
            r"\includegraphics[trim={1 2, 3 4},clip,width=0.5\textwidth]{a}")
        assert n == 1
        assert out.endswith(r"width=0.5\textwidth" + CAP + "{a}")

    def test_sondaki_virgul_cift_virgul_uretmez(self):
        out, _ = cap_includegraphics_height(r"\includegraphics[width=3cm,]{a}")
        assert r"width=3cm,height=" in out and ",," not in out

    def test_eski_bounding_box_sozdizimi_degismez(self):
        text = r"\includegraphics[1,2][3,4]{a.eps}"
        assert cap_includegraphics_height(text) == (text, 0)

    def test_idempotent(self):
        once, n1 = cap_includegraphics_height(
            r"\includegraphics[width=0.8\textwidth]{a.png}")
        twice, n2 = cap_includegraphics_height(once)
        assert (n1, n2) == (1, 0) and twice == once


# ---------------------------------------------------------------------------
# Sanitizer bütünleşmesi: yalnız tam belgede (Phase 6)
# ---------------------------------------------------------------------------

_FIGURE = ("\\begin{figure}[H]\n  \\centering\n"
           "  \\includegraphics[width=0.8\\textwidth]{_page_6_Picture_2.png}\n"
           "\\end{figure}\n\nŞekil 1.4. Örnek bir sistemin akış şeması\n")


class TestSanitizerButunlesme:
    @pytest.mark.parametrize("manual", [True, False])
    def test_tam_belgede_uygulanir_iki_yolda(self, manual):
        out, rep = _san(_doc(_FIGURE), manual=manual)
        assert (r"\includegraphics[width=0.8\textwidth" + CAP
                + "{_page_6_Picture_2.png}") in out
        assert rep.figure_heights_capped == 1
        assert any("Figure height capped" in d for d in rep.details)

    @pytest.mark.parametrize("manual", [True, False])
    def test_govde_kipinde_uygulanmaz(self, manual):
        # Phase 5.5 gövdesi QC/autofix girdisidir: '\textheight' orada
        # autofix'in '\text' onarımıyla '\text{height}' olurdu.
        out, rep = _san(_FIGURE, manual=manual)
        assert "height=" not in out and r"\textheight" not in out
        assert rep.figure_heights_capped == 0

    def test_preamble_dokunulmaz(self):
        pre = "\\newcommand{\\logo}{\\includegraphics[width=1cm]{logo.png}}\n"
        out, _ = _san(_doc(_FIGURE, preamble=pre))
        assert pre in out
        # Sınır yalnız gövdedeki görsele BİR kez eklenir. Kalite 2026-09-28
        # (R13): _FIGURE'ün dış altyazısı artık görselle tek birimdir ve
        # birimin sığma ölçütü de '\textheight' içerir; bu yüzden ham
        # '\textheight' değil, eklenen sınır sayılır (koruma aynı).
        assert out.count("height=" + FIGURE_MAX_HEIGHT) == 1

    def test_kod_bolgeleri_harfiyen_kalir(self):
        body = (
            "\\begin{verbatim}\n\\includegraphics[width=5cm]{a.png}\n"
            "\\end{verbatim}\n"
            "\\begin{lstlisting}\n\\includegraphics[width=6cm]{b.png}\n"
            "\\end{lstlisting}\n"
            "Örnek: \\verb|\\includegraphics[width=7cm]{c.png}|\n"
            + _FIGURE
        )
        out, rep = _san(_doc(body))
        assert "\\includegraphics[width=5cm]{a.png}" in out
        assert "\\includegraphics[width=6cm]{b.png}" in out
        assert "\\verb|\\includegraphics[width=7cm]{c.png}|" in out
        assert rep.figure_heights_capped == 1

    def test_tam_belge_idempotent(self):
        once, _ = _san(_doc(_FIGURE))
        twice, rep2 = _san(once)
        assert twice == once
        assert rep2.figure_heights_capped == 0

    def test_sablon_sarma_kapak_dokunulmaz(self, tmp_path):
        # Phase 6 benzetimi: şablon + kapak (pdfpages) + sanitize.
        cover = tmp_path / "source_book.pdf"
        cover.write_bytes(b"%PDF-1.4 deneme")
        full = TemplateEngine(PipelineConfig()).wrap_content(
            _FIGURE + "\n\\includegraphics[width=0.5\\textwidth]{kapak.png}\n",
            cover_pdf_path=str(cover))
        out, rep = _san(full)
        assert f"\\includepdf[pages={{1}}]{{{cover.resolve()}}}" in out
        assert rep.figure_heights_capped == 2
        assert (r"\includegraphics[width=0.5\textwidth" + CAP
                + "{kapak.png}") in out


# ---------------------------------------------------------------------------
# Neden Phase 5.5'te değil: autofix '\textheight'i bozar
# ---------------------------------------------------------------------------

class TestAutofixEtkilesimi:
    def test_govde_autofix_sonrasi_textheight_bozulmaz(self):
        from latex_pipeline.autofix import AutoFixer, _find_math_spans

        body, _ = _san(_FIGURE + "\n\\textbfDeneme metni.\n", manual=False)
        fixer = AutoFixer.__new__(AutoFixer)
        fixed, _ = AutoFixer._fix_broken_command_braces(
            fixer, body, [], _find_math_spans(body))
        assert r"\text{height}" not in fixed
        assert r"\textbf{Deneme}" in fixed
        # Aynı gövde sonra şablonla sarılıp Phase 6'dan geçince sınır gelir.
        out, rep = _san(_doc(fixed), manual=False)
        assert rep.figure_heights_capped == 1
        assert r"\text{height}" not in out


# ---------------------------------------------------------------------------
# Gerçek XeLaTeX: yalnız küçültür, genişliği korur, şekil ertelenmez
# ---------------------------------------------------------------------------

_XELATEX = shutil.which("xelatex")

_MEAS_PREAMBLE = (
    "\\usepackage{geometry}\n"
    # JRD_Bolum_1'deki gibi kısa metin alanı (\textheight ~ 236 pt).
    "\\geometry{paperwidth=143.3mm,paperheight=206.2mm,top=83.3mm,"
    "bottom=39.9mm,left=17.8mm,right=13.8mm}\n"
    "\\newsavebox\\measbox\n"
    "\\newcommand\\meas[2]{\\sbox\\measbox{#2}\\typeout{MEAS #1 "
    "\\the\\wd\\measbox\\space\\the\\ht\\measbox}}\n"
)


def _pt(s: str) -> float:
    return float(s.rstrip("pt"))


def _compile(tmp_path, name: str, tex: str) -> str:
    (tmp_path / f"{name}.tex").write_text(tex, encoding="utf-8")
    subprocess.run(
        [_XELATEX, "-interaction=nonstopmode", "-no-shell-escape",
         f"{name}.tex"],
        cwd=tmp_path, capture_output=True, timeout=120,
    )
    return (tmp_path / f"{name}.log").read_text(encoding="utf-8",
                                                errors="replace")


@pytest.mark.skipif(_XELATEX is None, reason="xelatex yok")
class TestXelatexOlcum:
    @pytest.fixture()
    def images(self, tmp_path):
        from PIL import Image
        Image.new("RGB", (200, 800), "black").save(tmp_path / "uzun.png")
        Image.new("RGB", (800, 100), "black").save(tmp_path / "genis.png")
        Image.new("RGB", (60, 40), "black").save(tmp_path / "kucuk.png")
        return tmp_path

    def test_yalniz_kuculur_genislik_korunur(self, images):
        body = (
            "\\meas{uzun}{\\includegraphics[width=0.8\\textwidth]{uzun.png}}\n"
            "\\meas{genis}{\\includegraphics[width=0.8\\textwidth]{genis.png}}\n"
            "\\meas{kucuk}{\\includegraphics{kucuk.png}}\n"
            "\\typeout{MEAS sayfa \\the\\textwidth\\space\\the\\textheight}\n"
            "x\n"
        )
        raw = _doc(body, preamble=_MEAS_PREAMBLE)
        san, _ = _san(raw)
        old = dict((m[0], (_pt(m[1]), _pt(m[2]))) for m in re.findall(
            r"^MEAS (\w+) ([\d.]+pt) ([\d.]+pt)",
            _compile(images, "eski", raw), re.M))
        new = dict((m[0], (_pt(m[1]), _pt(m[2]))) for m in re.findall(
            r"^MEAS (\w+) ([\d.]+pt) ([\d.]+pt)",
            _compile(images, "yeni", san), re.M))
        tw, th = new["sayfa"]
        cap = 0.7 * th
        # Uzun görsel: eskiden sayfadan uzundu, şimdi tam sınırda, oran korunur.
        assert old["uzun"][1] > th
        assert abs(new["uzun"][1] - cap) < 0.5
        assert new["uzun"][0] < old["uzun"][0]
        assert abs(new["uzun"][1] / new["uzun"][0]
                   - old["uzun"][1] / old["uzun"][0]) < 0.01
        # Geniş görsel (sınırın altında): genişlik ve yükseklik aynı kalır
        # (graphicx ölçek aritmetiği yüzünden <0,2 pt yuvarlama payı).
        assert abs(new["genis"][0] - 0.8 * tw) < 0.2
        assert abs(new["genis"][0] - old["genis"][0]) < 0.2
        assert abs(new["genis"][1] - old["genis"][1]) < 0.2
        # Seçeneksiz küçük görsel: dokunulmaz, büyümez.
        assert new["kucuk"] == old["kucuk"]

    def test_uzun_sekil_artik_ertelenmez(self, images):
        body = ("\\begin{figure}[H]\n\\centering\n"
                "\\includegraphics[width=0.8\\textwidth]{uzun.png}\n"
                "\\end{figure}\n\nŞekil 1.1. Uzun şema\n")
        # [H] -> [!htb] (B23) sanitizer'da; eski davranışı aynı belirteçle
        # ölçmek için ham belgede de [!htb] kullanılır.
        raw = _doc(body.replace("[H]", "[!htb]"), preamble=_MEAS_PREAMBLE)
        san, _ = _san(_doc(body, preamble=_MEAS_PREAMBLE))
        assert "Float too large for page" in _compile(images, "eski2", raw)
        assert "Float too large for page" not in _compile(images, "yeni2", san)
