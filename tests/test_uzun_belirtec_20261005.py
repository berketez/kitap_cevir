r"""Uzun "\_"'li belirteçlerde satır kırılma noktası (5 Ekim 2026; V1 bulgusu).

Alt çizgiyle bağlanmış uzun belirteçler, düzenli ifadeler ve yollar
(makine dizgisi alıntılayan bir raporun gerçek akış çıktısında) sayfa dışına
taşıyordu. Metinler sentetiktir.
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from latex_pipeline.sanitizer import UZUN_BELIRTEC_EN_AZ, uzun_belirtec_kirilimi as kir  # noqa: E402

UZUN = r"7\_STEP\_FORBIDDEN\_NOBOX\_YET\_CHECKING\_WHETHER\_ID4411\_ENABLES\_THE\_MECHANISM"


def test_uzun_belirtecte_her_alt_cizgiden_sonra_kirilma():
    out = kir("Mesaj: " + UZUN + " bitti.")
    assert out.count("\\_\\allowbreak{}") == UZUN.count("\\_")
    assert out.replace("\\allowbreak{}", "") == "Mesaj: " + UZUN + " bitti."


def test_kisa_belirtec_dokunulmaz():
    t = r"ortam\_adi ve x\_y\_z"
    assert len("ortam\\_adi") < UZUN_BELIRTEC_EN_AZ
    assert kir(t) == t


def test_matematik_kod_ve_adres_dokunulmaz():
    t = ("$" + UZUN + "$ \\href{https://ornek.test/" + UZUN + "}{bağ} "
         "\\begin{lstlisting}\n" + UZUN + "\n\\end{lstlisting}")
    assert kir(t) == t


def test_preamble_dokunulmaz_ve_idempotent():
    t = "\\newcommand{\\x}{" + UZUN + "}\n\\begin{document}\n" + UZUN + "\n\\end{document}"
    out = kir(t)
    assert out.split("\\begin{document}")[0] == t.split("\\begin{document}")[0]
    assert kir(out) == out


@pytest.mark.skipif(shutil.which("xelatex") is None, reason="xelatex yok")
def test_derlemede_tasma_kalkar(tmp_path):
    govde = "Sonuç: " + UZUN + " " + UZUN + " ve devamı."
    # Şablonun satır kırma ayarları (turkish_book.tex: microtype + emergencystretch=6em)
    belge = ("\\documentclass{article}\n\\usepackage{fontspec}\n\\usepackage{microtype}\n"
             "\\emergencystretch=6em\n\\begin{document}\n{}\n\\end{document}\n")
    def tasma(metin, ad):
        (tmp_path / f"{ad}.tex").write_text(belge.replace("{}\n", metin + "\n", 1), encoding="utf-8")
        subprocess.run(["xelatex", "-interaction=nonstopmode", f"{ad}.tex"], cwd=tmp_path,
                       capture_output=True, timeout=120)
        return (tmp_path / f"{ad}.log").read_text(encoding="utf-8", errors="ignore").count("Overfull \\hbox")
    assert tasma(govde, "once") > 0
    assert tasma(kir(govde), "sonra") == 0


def test_yalniz_tam_belgede_uygulanir():
    """Faz 5.5 gövdesi QC'de kaynakla karşılaştırılır: oraya \\allowbreak{} girmez."""
    from latex_pipeline.sanitizer import LatexSanitizer
    govde = "Mesaj: " + UZUN + " bitti.\n"
    out, _ = LatexSanitizer(manual_translation=True).sanitize(govde)
    assert "\\allowbreak" not in out
    belge = ("\\documentclass{book}\n\\begin{document}\n" + govde + "\\end{document}\n")
    out, _ = LatexSanitizer(manual_translation=True).sanitize(belge)
    assert out.count("\\_\\allowbreak{}") == UZUN.count("\\_")


REGEX = r"\textasciicircum{}qq(?:ALFA|BETA|GAMA|DELTA|EPSILON|ZETA|ETA|TETA)[A-Za-z0-9\_-]*\$"


def test_duzenli_ifadede_dikey_cizgiden_sonra_kirilma():
    out = kir(r"\item \textbf{nesne:} " + REGEX + "\n")
    assert out.count("|\\allowbreak{}") == REGEX.count("|")
    assert out.replace("\\allowbreak{}", "") == r"\item \textbf{nesne:} " + REGEX + "\n"


def test_yol_egik_cizgiden_sonra_kirilir():
    yol = "depo-onbellegi/qqSONUC/ajan-gunlukleri/kosu-2026/final.json"
    out = kir("Dosya " + yol + " içinde.")
    assert out.count("/\\allowbreak{}") == yol.count("/")


@pytest.mark.parametrize("korunan", [
    r"\begin{tabular}{|c|c|c|c|c|c|c|c|c|c|c|c|c|}",
    r"\begin{tabularx}{\linewidth}{|p{2cm}|p{2cm}|p{2cm}|X|}",
    r"\multicolumn{3}{|c|c|c|c|c|c|c|c|c|c|c|c|}{x}",
    r"\verb!qq(?:ALFA|BETA|GAMA|DELTA|EPSILON)!",
    r"\verb|qq/ALFA/BETA/GAMA/DELTA/EPSILON/X|",
    r"\label{fig:qq/ALFA/BETA/GAMA/DELTA/EPSILON}",
    r"\includegraphics[width=0.5\linewidth]{images/qq/ALFA/BETA/GAMA.png}",
    "\\begin{align*}\n|a|+|b|+|c|+|d|+|e|+|f|+|g| &= x\n\\end{align*}",
    r"$|a|+|b|+|c|+|d|+|e|+|f|+|g|+|h|$",
])
def test_korunan_bolgeler_dokunulmaz(korunan):
    t = "Metin " + korunan + " son."
    assert kir(t) == t


def test_karisik_idempotent():
    t = (r"\item \textbf{a:} " + REGEX + " ve " + UZUN
         + " ve depo-onbellegi/qqSONUC/ajan-gunlukleri/kosu-2026/x.json\n")
    once = kir(t)
    assert kir(once) == once


@pytest.mark.parametrize("korunan", [
    r"\includepdf[pages={1}]{/private/var/folders/x/y/z/source_book.pdf}",
    r"\input{bolumler/qq/ALFA/BETA/GAMA/DELTA.tex}",
    r"\section{qq\_A\_B\_C\_D\_E\_F\_G\_H\_I\_J\_K}",
    r"\caption{qq(?:ALFA|BETA|GAMA|DELTA|EPSILON|ZETA)}",
])
def test_metin_disi_komut_argumani_dokunulmaz(korunan):
    t = "Metin " + korunan + " son."
    assert kir(t) == t


def test_metin_komutu_icinde_kirilir():
    out = kir(r"Bkz. \texttt{" + UZUN + r"} ve \footnote{" + UZUN + "}.")
    assert out.count("\\_\\allowbreak{}") == 2 * UZUN.count("\\_")
