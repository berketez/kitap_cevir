"""Tehlikeli LaTeX komut filtresi: TeX sözdizimi varyantları (25 Eylül 2026).

Çevrilmiş TeX bir LLM'den ya da kullanıcıdan gelir; bu makinede openin_any=a
olduğu için \\openin / \\input ile mutlak yoldaki dosya PDF'ye okunabilir.
Eski desenler komut sınırını \\b ile yazıyordu; TeX'te kontrol sözcüğü ilk
harf-dışı karakterde bittiği için '\\openin5=/etc/passwd' ve '\\catcode64=11'
filtreyi atlıyordu. Her varyant hem Yol A (manual_translation=True) hem Yol B
modunda, hem düzyazıda hem matematik içinde sınanır.
"""

import pytest

from latex_pipeline.sanitizer import LatexSanitizer


ZARARLI = [
    r"\openin5=/etc/passwd",
    r"\openin5 /etc/passwd",
    r"\openin 5 /etc/passwd",
    r"\openin\dosya=/etc/passwd",
    r"\openout3=gizli.txt",
    r"\immediate\openout3=gizli.txt",
    r"\read5 to\x",
    r"\readline5 to\x",
    r"\write18{ls}",
    r"\write 18{ls}",
    r"\immediate\write18{ls}",
    r"\input{/etc/passwd}",
    r"\input /etc/passwd",
    r"\input/etc/passwd",
    r"\input ~/.ssh/config",
    r"\input{../gizli}",
    r"\input ../gizli",
    r"\@@input /etc/passwd",
    r"\catcode`\@=11",
    r"\catcode64=11",
    r"\catcode 64 = 11",
    r'\catcode"40=11',
    r"\csname input\endcsname{/etc/passwd}",
    r"\directlua{os.exit()}",
    r"\lstinputlisting{/etc/passwd}",
    r"\lstinputlisting[language=bash]{../../gizli.sh}",
    r"\verbatiminput{~/notlar.txt}",
    r"\VerbatimInput{/etc/hosts}",
    r"\InputIfFileExists{/etc/passwd}{}{}",
    r"^^5cinput{/etc/passwd}",
]

MESRU = [
    r"Metin \input{bolum2} son",
    r"Metin \input bolum2 son",
    r"\include{ek_a}",
    r"\lstinputlisting{kod/ornek.py}",
    r"Burada $x \in A$ ve $\int_0^\infty f\,dx$ son",
    r"$\lim_{n\to\infty} a_n = \inf S$",
    r"Oran $\csname theequation\endcsname$ son",
    r"Metin \textbf{writeup} ve \emph{read} son",
    r"Metin \immediately son",
    r"$\mathrm{read}(x) = \operatorname{open}(y)$",
    r"$x^{2}$ ve $a^2 + b^2 = c^2$",
    r"Değer 18 ve \textbf{write} 18 kez yazıldı.",
]


def _filtrelenen(metin, manual):
    _, rapor = LatexSanitizer(manual_translation=manual).sanitize(metin)
    return rapor.dangerous_filtered


@pytest.mark.parametrize("manual", [True, False])
@pytest.mark.parametrize("komut", ZARARLI)
def test_zararli_varyant_duzyazida_filtrelenir(komut, manual):
    assert _filtrelenen(f"Metin {komut} son", manual) >= 1


@pytest.mark.parametrize("manual", [True, False])
@pytest.mark.parametrize("komut", [
    r"\openin5=/etc/passwd", r"\input{/etc/passwd}", r"\catcode64=11",
    r"\read5 to\x",
])
def test_zararli_varyant_matematikte_filtrelenir(komut, manual):
    assert _filtrelenen(f"Formül ${komut}$ son", manual) >= 1
    assert _filtrelenen(f"Formül $${komut}$$ son", manual) >= 1


@pytest.mark.parametrize("manual", [True, False])
@pytest.mark.parametrize("metin", MESRU)
def test_mesru_kullanim_dokunulmaz(metin, manual):
    out, rapor = LatexSanitizer(manual_translation=manual).sanitize(metin)
    assert rapor.dangerous_filtered == 0
    assert "FILTERED" not in out


def test_filtrelenen_komut_ciktida_kalmaz():
    out, _ = LatexSanitizer(manual_translation=True).sanitize(
        "Metin \\openin5=/etc/passwd son")
    assert "/etc/passwd" not in out
    assert "\\openin" not in out
