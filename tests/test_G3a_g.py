"""Kulvar G3a (5 Ekim 2026): sanitizer + autofix dizgi düzeltmeleri.

Kanıt yerleri okuma bulgularından ve model testi çıktılarından (iç denetim
notları); test dizgileri kanıtla aynı yapıda (LaTeX komutları, satır sonları, uzunluk, ölçülen genişlik)
SENTETİK eşleridir (KARAR 26: açık yayında telifli metin yok).

(1) ONARIM2 R2.4-1: altyazılı şekil/tablo [H] -> [!htb]
    gevşetiliyor, kaynak sırası bozuluyordu. [H] korunur; tam belgede birim
    ölçülür, metin alanına sığmayan birim taşmaz.
(2) Okuma bulguları (2026-09-30): lstlisting'e düşmüş düzyazı alıntı kod kutusunda
    (satır numaralı, çerçeveli) basılıyordu. Düzyazı alıntı ortamına alınır;
    gerçek kod/belirteç kutuda kalır.
(3) RAPOR MT-6: multline* eşiği LaTeX kaynağının uzunluğunu ölçüyordu; tek
    satıra sığan denklem bölünüyordu. Görünür genişlik tahmin edilir.
(4) ONARIM2 R2.4-3: \\href{#page-...} iç bağlantılarının hedefi yok.
(5) mt okuma MT-21: formülden sonra ek önünde boşluk ("$x$ ’nin").
(6) mt okuma MT-22: \\substack alt sınırları yan yana (``\\ `` yerine ``\\\\``).
"""
from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from latex_pipeline.sanitizer import LatexSanitizer

_XELATEX = shutil.which("xelatex")


def _san(text: str, manual: bool = True):
    return LatexSanitizer(manual_translation=manual).sanitize(text)


def _doc(body: str, preamble: str = "") -> str:
    """Şablon benzeri tam belge (Phase 6 girdisi)."""
    return ("\\documentclass[a4paper,11pt,openany,oneside]{book}\n"
            "\\usepackage{graphicx}\n\\usepackage{float}\n"
            "\\usepackage{amsmath}\n\\usepackage{caption}\n"
            "\\usepackage{hyperref}\n" + preamble
            + "\\begin{document}\n" + body + "\n\\end{document}\n")


# ===========================================================================
# (6) mt okuma MT-22 — \substack / tek sütunlu array satır ayırıcısı
# ===========================================================================
# Kanıt: bir model çıktısında iki denklem.
_MT22_SUBSTACK = (r"\ddot{\vec{X}}_i = -\sum_{\substack{l=1\ l \neq i}}^N k "
                  r"\frac{M_l}{D_{il}^3} (\vec{X}_i - \vec{X}_l)")
_MT22_ARRAY = (r"\vec{\bm{P}}_{e,i} = -\sum_{\begin{array}{c} l=1 \ l \neq i "
               r"\end{array}}^{N} k\frac{M_{l}}{D_{il}^{3}} (\vec{\bm{X}}_{i} - "
               r"\vec{\bm{X}}_{l})")
# Derlemdeki ikinci biçim (derlemde 12 kez)
_MT22_SET = (r"\sum_{\substack{i=1 \ (u_i, v_i) \in \mathcal{D}_{\text{test}}}}"
             r"^{N_{\text{test}}} v_i")


class TestMT22Substack:
    def test_substack_satir_ayirici(self):
        out, _ = _san("Metin.\n\n\\[\n" + _MT22_SUBSTACK + "\n\\]\n")
        assert r"\substack{l=1\\ l \neq i}" in out

    def test_tek_sutunlu_array_satir_ayirici(self):
        out, _ = _san("Metin.\n\n\\[\n" + _MT22_ARRAY + "\n\\]\n")
        assert r"\begin{array}{c} l=1 \\ l \neq i \end{array}" in out

    def test_kume_kosulu(self):
        out, _ = _san("Metin.\n\n\\[\n" + _MT22_SET + "\n\\]\n")
        assert (r"\substack{i=1 \\ (u_i, v_i) \in \mathcal{D}_{\text{test}}}"
                in out)

    def test_satir_ici_matematik(self):
        out, _ = _san(r"Toplam $\sum_{\substack{k=1\ k\neq i}} x_k$ olur." + "\n")
        assert r"\substack{k=1\\ k\neq i}" in out

    @pytest.mark.parametrize("math", [
        # Zaten satırlara ayrılmış (derlemdeki biçim: iki koşul '\\' ile ayrık)
        r"\sum_{\substack{\mathbf{z} \in \Omega_Z \\ b \in W}} f",
        # Bir yanında bağıntı yok: '\ ' bilinçli boşluk olabilir
        r"\sum_{\substack{\text{tüm}\ i}} f",
        r"\sum_{\substack{i\ \mathrm{tek}}} f",
        # İç gruptaki boşluk satır ayırıcı değildir
        r"\sum_{\substack{{i=1\ j=2}}} f",
        # Çok sütunlu array: '\ ' hücre içi boşluk olabilir
        r"\sum_{\begin{array}{cc} i=0 \ j=3 \end{array}} f",
    ])
    def test_dokunulmayanlar(self, math):
        out, _ = _san("Metin.\n\n\\[\n" + math + "\n\\]\n")
        assert math in out

    def test_kod_ornegine_dokunulmaz(self):
        code = ("\\begin{verbatim}\n\\[ \\sum_{\\substack{l=1\\ l\\neq i}} \\]\n"
                "\\end{verbatim}\n")
        out, _ = _san(code)
        assert code in out

    def test_idempotent(self):
        once, _ = _san("Metin.\n\n\\[\n" + _MT22_SUBSTACK + "\n\\]\n")
        twice, _ = _san(once)
        assert twice == once


# ===========================================================================
# (5) mt okuma MT-21 — formülden sonra ek önünde boşluk
# ===========================================================================
# Kanıt: iki model çıktısında yedi yer. Yalnız boşluk silinir; ekin ünlü
# uyumu ("$\vec{e}$’nın") model hatasıdır, dokunulmaz.
class TestMT21EkOnundeBosluk:
    @pytest.mark.parametrize("before,after", [
        (r"(kabaca küçük kütle sınırında) $t^{1/3}$ ’nin",
         r"(kabaca küçük kütle sınırında) $t^{1/3}$’nin"),
        (r"“düğüm vektörü” diye anılan $\vec{e}$ ’nın",
         r"“düğüm vektörü” diye anılan $\vec{e}$’nın"),
        ("bu eksen takımında da yazılabilir. Şekil 2.1 ’dan",
         "bu eksen takımında da yazılabilir. Şekil 2.1’dan"),
        ("Denk. (1.3) ’ın geçerli olduğu bölgede.",
         "Denk. (1.3)’ın geçerli olduğu bölgede."),
        (r"zira $v \to 0$ iken $r \to \infty$ ’te kalır.",
         r"zira $v \to 0$ iken $r \to \infty$’te kalır."),
        (r"yüzeyindeki sıcaklık $T_2=2\,^{\circ}\mathrm{C}$ ’tür",
         r"yüzeyindeki sıcaklık $T_2=2\,^{\circ}\mathrm{C}$’tür"),
        (r"  \item $\lambda$ ’ın ısı iletkenliği sıcaklıktan etkilenmez.",
         r"  \item $\lambda$’ın ısı iletkenliği sıcaklıktan etkilenmez."),
        # ASCII kesme ve \ref / \eqref sonrası
        (r"Bkz. \eqref{eq:1} 'den çıkar.", r"Bkz. \eqref{eq:1}'den çıkar."),
        (r"Tablo \ref{t:2} ’de verilir.", r"Tablo \ref{t:2}’de verilir."),
        (r"\(x\) ’in değeri", r"\(x\)’in değeri"),
    ])
    def test_bosluk_silinir(self, before, after):
        for manual in (True, False):
            out, _ = _san(before + "\n", manual=manual)
            assert after in out, (manual, out)

    @pytest.mark.parametrize("text", [
        # Açılış tırnağı gibi kullanılan ASCII kesme (kapanışlı sözcük)
        "Bu $x$ 'nin' sözcüğü değil.",
        # Kesmeden sonra boşluk: ek değil
        "Değer $x$ ’ ve devamı.",
        # Harften sonra: kapsam dışı (formül/sayı değil)
        "Ankara ’da toplantı.",
        # Kesmeden sonra büyük harf: ek değil
        "Sonuç $x$ ’Ali’ dedi.",
    ])
    def test_dokunulmayanlar(self, text):
        out, _ = _san(text + "\n")
        assert text in out

    def test_kod_ornegine_dokunulmaz(self):
        code = "\\begin{verbatim}\n$x$ ’nin\n\\end{verbatim}\n"
        out, _ = _san(code)
        assert code in out


# ===========================================================================
# (3) RAPOR MT-6 — multline* eşiği görünür genişliğe göre
# ===========================================================================
# Kanıt: bir model testinde üç denklem (çeviride multline* ile ikiye
# bölünmüştü; kaynak PDF'te tek satır).
# Kanıttaki denklemler XeLaTeX ile 17,8 / 20,5 / 20,0 em ölçülmüştü (satır
# 41,6 em). Aşağıdakiler aynı özellikte sentetik denklemlerdir: kaynak 221 /
# 231 / 266 karakter (eski 180 eşiğinin üstünde), XeLaTeX'te (book, 11 pt)
# 19,2 / 22,9 / 22,2 em (kanıt denklemleri aynı düzenekte 18,9 / 22,3 / 21,7).
_MT6_EQS = [
    r"\vec{q} \cdot \dot{\vec{q}} = 0 \implies \frac{\mathrm{d}}{\mathrm{d}\tau} "
    r"\left( \tfrac{1}{2} \, \vec{q} \cdot \vec{q} \right) = \frac{\mathrm{d}}{\mathrm{d}\tau} "
    r"\left( \tfrac{1}{2} \lVert \vec{q} \rVert^{2} \right) = 0",
    r"\ddot{\vec{s}} \cdot \vec{k} + \frac{G M_0}{s^3} \vec{s} \cdot \vec{k} = "
    r"\frac{\mathrm{d}}{\mathrm{d}\tau} \left( \dot{\vec{s}} \cdot \vec{k} \right) + "
    r"\frac{G M_0}{s^3} \vec{s} \cdot \left( \vec{s} \times \dot{\vec{s}} \right) = 0",
    r"\dot{\vec{\boldsymbol{v}}}\cdot\vec{\boldsymbol{v}}=-\frac{\mu\,\vec{\boldsymbol{r}}"
    r"\cdot\vec{\boldsymbol{v}}}{r^3}=-\frac{\mu\,\dot{r}}{r^2}\implies\frac{\mathrm{d}}"
    r"{\mathrm{d}\tau}\left(\frac{\vec{\boldsymbol{v}}\cdot\vec{\boldsymbol{v}}}{2}-\frac{\mu}{r}\right)=0",
]
# Kanıt: bir test kitabında bir denklem — kaynağı yalnız 102
# karakter (eski 180 eşiği bölmüyordu) ama ölçülen genişliği 45,3 em: taşar.
# Sentetik eşi: 105 karakter, XeLaTeX'te (book, 11 pt) 47,0 em (kanıt aynı
# düzenekte 47,1 em).
_WIDE_SHORT_EQ = (r"P(default \mid Leverage = 2.5, tenure = 14, rating = 0.8) = "
                  r"-0.10 + 0.004*2.5 + 0.021*14-0.03*0.8 = 0.18.")
# Kanıt: başka bir test kitabında bir denklem — ölçülen 30,7 em: A4 11 pt
# (41 em) satıra sığar, dar bir kitap düzenine (317,8 pt / 10 pt = 31,8 em)
# güvenle sığmaz.
# Sentetik eşi (denk propanol yanması): tahmini genişliği kanıtla aynı
# (30,78 em), XeLaTeX'te de aynı (book, 11 pt: ikisi de 32,2 em).
_MID_EQ = (r"25.60C_3H_7OH + 121.20O_2 \rightarrow 76.00CO_2 + 0.80CO + "
           r"102.40H_2O + 6.40O_2")
_NARROW_GEOMETRY = ("\\usepackage{geometry}\n\\geometry{\n  paperwidth=155mm,\n"
                    "  paperheight=235mm,\n  left=21mm,\n  right=22mm\n}\n")


class TestMT6Esik:
    @pytest.mark.parametrize("eq", _MT6_EQS)
    def test_tek_satira_sigan_bolunmez_govde(self, eq):
        out, _ = _san("Metin.\n\n\\[\n" + eq + "\n\\]\n", manual=False)
        assert "multline" not in out and eq in out

    @pytest.mark.parametrize("eq", _MT6_EQS)
    def test_tek_satira_sigan_bolunmez_tam_belge(self, eq):
        out, _ = _san(_doc("Metin.\n\n\\[\n" + eq + "\n\\]\n"))
        assert "multline" not in out and eq in out

    def test_kisa_kaynakli_genis_denklem_bolunur(self):
        out, _ = _san("Metin.\n\n\\[\n" + _WIDE_SHORT_EQ + "\n\\]\n")
        m = re.search(r"\\begin\{multline\*\}(.*?)\\end\{multline\*\}", out, re.S)
        assert m, out
        norm = lambda s: re.sub(r"\s+|\\\\", "", s)  # noqa: E731
        assert norm(m.group(1)) == norm(_WIDE_SHORT_EQ)

    def test_dar_duzende_bolunur_genis_duzende_bolunmez(self):
        wide, _ = _san(_doc("Metin.\n\n\\[\n" + _MID_EQ + "\n\\]\n",
                            preamble="\\usepackage{geometry}\n"
                                     "\\geometry{margin=2.5cm}\n"))
        assert "multline" not in wide
        narrow, _ = _san(_doc("Metin.\n\n\\[\n" + _MID_EQ + "\n\\]\n",
                              preamble=_NARROW_GEOMETRY).replace("11pt", "10pt"))
        assert "multline" in narrow
        # Gövde evresi (düzen bilinmiyor) bölmez: dar düzene Phase 6 böler.
        body, _ = _san("Metin.\n\n\\[\n" + _MID_EQ + "\n\\]\n")
        assert "multline" not in body

    def test_bolunen_satirlar_duzene_sigar(self):
        from latex_pipeline.sanitizer import (
            display_math_width_em, math_line_capacity_em)
        eq = " + ".join(f"a_{{{k}}} x^{{{k}}}" for k in range(30)) + " = 0"
        doc = _doc("Metin.\n\n\\[\n" + eq + "\n\\]\n",
                   preamble=_NARROW_GEOMETRY).replace("11pt", "10pt")
        cap = math_line_capacity_em(doc.split("\\begin{document}")[0])
        assert 31 < cap < 33
        out, _ = _san(doc)
        m = re.search(r"\\begin\{multline\*\}(.*?)\\end\{multline\*\}", out, re.S)
        assert m, out
        lines = m.group(1).split("\\\\")
        assert len(lines) >= 4
        assert all(display_math_width_em(ln) <= 0.88 * cap for ln in lines)
        assert re.sub(r"\s|\\\\", "", m.group(1)) == re.sub(r"\s", "", eq)


# ===========================================================================
# (4) ONARIM2 R2.4-3 — hedefsiz iç bağlantılar
# ===========================================================================
# Kanıt: elle onarılan bir raporun çevirisi — belgede 158
# '\href{#page-N-M}' var, hiçbir '\hypertarget' yok: tıklanınca bir yere
# gitmiyordu.
# (Dizgiler sentetik; bağlantı biçimi kanıttaki gibi.)
_IC_BAGLANTI_1 = (r"ölçümü atlatmanın genel bir yolunu arayan birkaç "
              r"\href{#page-8-0}{büyük ölçekli} ortak çalışmada")
_IC_BAGLANTI_2 = (r"Ekiplerin ortak duyuru listesine yazdığı 40.000'i aşkın "
              r"\href{#page-22-0}{ileti} ile eklerin arşivi.")


class TestR24IcBaglanti:
    def test_hedefsiz_baglanti_kalkar_metin_kalir(self):
        out, rep = _san(_doc(_IC_BAGLANTI_1 + "\n\n" + _IC_BAGLANTI_2 + "\n"))
        assert "\\href{#" not in out
        assert ("birkaç büyük ölçekli ortak çalışmada" in out
                and "aşkın ileti ile eklerin" in out)
        assert any("internal link" in d for d in rep.details)

    def test_hypertarget_varsa_baglanti_kalir(self):
        body = ("\\hypertarget{page-8-0}{}Hedef paragraf.\n\n" + _IC_BAGLANTI_1 + "\n")
        out, _ = _san(_doc(body))
        assert "\\href{#page-8-0}{büyük ölçekli}" in out

    def test_etiket_varsa_hyperref(self):
        body = ("\\section{Yöntem}\\label{sec:yontem}\n\nMetin.\n\n"
                "Bkz. \\href{#sec:yontem}{yöntem bölümü}.\n")
        out, _ = _san(_doc(body))
        assert "\\hyperref[sec:yontem]{yöntem bölümü}" in out

    def test_ic_ice_parantezli_metin(self):
        body = r"Bkz. \href{#page-3-1}{\textbf{Tablo 2} ve {ekleri}} sonra." + "\n"
        out, _ = _san(_doc(body))
        assert r"Bkz. \textbf{Tablo 2} ve {ekleri} sonra." in out

    def test_dis_baglanti_ve_hyperref_otomatik_hedefe_dokunulmaz(self):
        body = (r"\href{https://huggingface.co/x}{site} ve "
                r"\href{#page.3}{üçüncü sayfa} ve \href{#chapter.2}{bölüm}." + "\n")
        out, _ = _san(_doc(body))
        assert r"\href{https://huggingface.co/x}{site}" in out
        assert r"\href{#page.3}{üçüncü sayfa}" in out
        assert r"\href{#chapter.2}{bölüm}" in out

    def test_kod_ornegine_dokunulmaz(self):
        code = "\\begin{verbatim}\n\\href{#page-1-0}{x}\n\\end{verbatim}\n"
        out, _ = _san(_doc(code))
        assert code in out

    def test_govde_evresinde_degismez(self):
        # QC ve EPUB Phase 5.5 gövdesini okur: yalnız derlenecek tam belge.
        out, _ = _san(_IC_BAGLANTI_1 + "\n")
        assert "\\href{#page-8-0}{büyük ölçekli}" in out


# ===========================================================================
# (1) Şekil/tablo kaynak sırasında kalır ([H] korunur)
# ===========================================================================
# Kanıt: elle onarılan bir raporun çevirisinde iki şekil (Şekil 3, iç
# altyazı + dipnot 16; Şekil 12, dış altyazı paragrafı).
# (Altyazı ve dipnot metinleri sentetik; yapı kanıttaki gibi.)
_SEKIL_IC_ALTYAZI = (
    "\\begin{figure}[H]\n  \\centering\n"
    "  \\includegraphics[width=0.8\\textwidth]{s8_sekil3.png}\n"
    "  \\caption*{\\textit{Şekil 3: Ajanlar ortak panoyu 8 Temmuz gecesi "
    "kullanmaya başladı.\\footnotemark[16] Grafikte yalnız en az bir kez "
    "panoya ileti bırakan ajanlar sayılmıştır.}}\n\\end{figure}\n\n"
    "\\footnotetext[16]{Bu kadar kısa sürede sayının neden 12 Temmuz "
    "sabahı birden düştüğü henüz açık değil.}\\setcounter{footnote}{16}\n")
_SEKIL_DIS_ALTYAZI = (
    "\\begin{figure}[H]\n  \\centering\n"
    "  \\includegraphics[width=0.8\\textwidth]{s37_sekil12.png}\n"
    "\\end{figure}\n\n"
    "\\textit{Şekil 12 (Şekil 5 ile aynı): 11 Temmuz öğleden sonra "
    "ajanlar yardımcı sunucularda uzaktan komut yürütmeyi denemeye "
    "koyuldu.}\n")
_TABLE_CAP = ("\\begin{table}[H]\n\\centering\n\\caption{Değerler}\n"
              "\\begin{tabular}{ll}\na & b \\\\\n\\end{tabular}\n\\end{table}\n")
_FIG_NOCAP = ("\\begin{figure}[H]\n  \\centering\n"
              "  \\includegraphics[width=0.6\\textwidth]{a.png}\n\\end{figure}\n")


class TestKaynakSirasi:
    @pytest.mark.parametrize("block", [_SEKIL_IC_ALTYAZI, _TABLE_CAP, _FIG_NOCAP])
    def test_govdede_H_korunur(self, block):
        out, _ = _san("Önceki paragraf.\n\n" + block + "\nSonraki paragraf.\n")
        assert "[!htb]" not in out
        assert ("\\begin{figure}[H]" in out) or ("\\begin{table}[H]" in out)

    def test_tam_belgede_ic_altyazili_sekil_yerinde_olculu(self):
        out, _ = _san(_doc("Önceki paragraf.\n\n" + _SEKIL_IC_ALTYAZI
                           + "\nSonraki paragraf.\n"))
        assert "[!htb]" not in out
        body = out.split("\\begin{document}")[1]
        unit = re.search(
            r"\\par\\setbox0\\vbox\{\\begin\{figure\}\[H\](.*?)\\end\{figure\}\}%\n"
            r"\\ifdim\\dimexpr\\ht0\+\\dp0\\relax>\\textheight(.*?)\\fi\n", body, re.S)
        assert unit, body
        assert "Şekil 3: Ajanlar" in unit.group(1)
        # Taşma koruması: metin alanından yüksek birim küçültülür (kayıpsız).
        assert "\\resizebox*{!}{\\textheight}{\\box0}" in unit.group(2)
        # Dipnot metni birimin hemen ardında (işaretle aynı sayfa).
        tail = body[unit.end():]
        assert tail.lstrip().startswith("\\footnotetext[16]{Bu kadar")
        assert body.index("Önceki paragraf.") < unit.start()
        assert body.index("Sonraki paragraf.") > unit.end()

    def test_tam_belgede_dis_altyazili_sekil_yerinde(self):
        out, _ = _san(_doc("Önceki paragraf.\n\n" + _SEKIL_DIS_ALTYAZI
                           + "\nSonraki paragraf.\n"))
        assert "[!htb]" not in out
        assert re.search(r"\\setbox0\\vbox\{%\n\{.*?\\par\}\\par\\vskip\\abovecaptionskip\n\{\\noindent\\centering \\textit\{Şekil 12",
                         out, re.S)
        assert "\\else\\vskip\\intextsep\\box0 \\fi" in out

    def test_tam_belge_idempotent(self):
        once, _ = _san(_doc(_SEKIL_IC_ALTYAZI + "\n" + _SEKIL_DIS_ALTYAZI + "\n" + _TABLE_CAP))
        twice, _ = _san(once)
        assert twice == once

    def test_kod_ornegine_dokunulmaz(self):
        code = "\\begin{verbatim}\n" + _SEKIL_IC_ALTYAZI + "\\end{verbatim}\n"
        out, _ = _san(_doc(code))
        assert code in out


def _compile(tmp_path, name: str, tex: str) -> str:
    (tmp_path / f"{name}.tex").write_text(tex, encoding="utf-8")
    for _ in range(2):
        subprocess.run(
            [_XELATEX, "-interaction=nonstopmode", "-no-shell-escape",
             f"{name}.tex"], cwd=tmp_path, capture_output=True, timeout=180)
    return (tmp_path / f"{name}.log").read_text(encoding="utf-8",
                                                errors="replace")


def _pdf_pages(pdf) -> list:
    fitz = pytest.importorskip("fitz")
    with fitz.open(pdf) as doc:
        return [page.get_text() for page in doc]


def _page_of(pages: list, needle: str) -> int:
    hits = [i for i, t in enumerate(pages) if needle in t.replace("\n", " ")]
    assert hits, needle
    return hits[0]


@pytest.mark.skipif(_XELATEX is None, reason="xelatex yok")
class TestKaynakSirasiDerleme:
    # Sayfanın büyük kısmını dolduran paragraf: şekil ilk sayfaya sığmaz.
    _FILL = ("Dolgu metni sayfanın büyük bölümünü kaplar ve şeklin bu sayfaya "
             "sığmamasını sağlar. ") * 20

    def _body(self, fig_height: str) -> str:
        fig = ("\\begin{figure}[H]\n\\centering\n\\rule{0.5\\textwidth}{"
               + fig_height + "}\n\\caption*{ŞEKİLALTYAZISI: deneme.\\footnotemark[7]}\n"
               "\\end{figure}\n\n\\footnotetext[7]{DİPNOTMETNİ burada.}"
               "\\setcounter{footnote}{7}\n")
        return ("\\chapter{Deneme}\n" + self._FILL + "\n\n" + fig
                + "\nSONRAKİPARAGRAF şeklin ardından gelir.\n")

    def test_sekil_ve_dipnot_kaynak_sirasinda(self, tmp_path):
        raw = _doc(self._body("0.35\\textheight"))
        san, _ = _san(raw)
        # Eski davranış ([!htb]) aynı belgede sırayı bozuyordu: kontrol.
        _compile(tmp_path, "eski", raw.replace("[H]", "[!htb]"))
        old = _pdf_pages(tmp_path / "eski.pdf")
        assert _page_of(old, "SONRAKİPARAGRAF") < _page_of(old, "ŞEKİLALTYAZISI")
        assert _page_of(old, "DİPNOTMETNİ") < _page_of(old, "ŞEKİLALTYAZISI")
        log = _compile(tmp_path, "yeni", san)
        assert "\n! " not in log
        new = _pdf_pages(tmp_path / "yeni.pdf")
        cap = _page_of(new, "ŞEKİLALTYAZISI")
        assert _page_of(new, "DİPNOTMETNİ") == cap
        assert _page_of(new, "SONRAKİPARAGRAF") == cap
        text = new[cap].replace("\n", " ")
        assert text.index("ŞEKİLALTYAZISI") < text.index("SONRAKİPARAGRAF")
        assert "Float too large" not in log

    def test_metin_alanindan_yuksek_sekil_tasmaz(self, tmp_path):
        san, _ = _san(_doc(self._body("1.4\\textheight")))
        log = _compile(tmp_path, "uzun", san)
        assert "\n! " not in log
        assert "Overfull \\vbox" not in log
        assert "Float too large" not in log
        pages = _pdf_pages(tmp_path / "uzun.pdf")
        cap = _page_of(pages, "ŞEKİLALTYAZISI")
        # Küçültülen şekil sayfayı doldurur; dipnot metni sığmazsa sonraki
        # sayfaya geçer (taşma yerine), metin şeklin ardından gelir.
        assert _page_of(pages, "DİPNOTMETNİ") in (cap, cap + 1)
        assert _page_of(pages, "SONRAKİPARAGRAF") >= cap


# ===========================================================================
# (2) Kod kutusundaki düzyazı alıntı
# ===========================================================================
# Kanıt: elle onarılan bir raporun çevirisindeki lstlisting blokları (29
# bloğun 18'i düzyazı düşünce zinciri, 11'i belirteç/mesaj).
_LST_PROSE = {
    # Satır numaralı kutu, ↪
    "satir_numarali": ("“Kayıt dosyasını lehimize kullanabilir miyiz? Sonuçta bütün\n"
               "adımlarımız zaten kaydedilmiyor mu? Evet, dökümler mevcut.”"),
    # Numaralı satırlar
    "numarali_satirlar": ("“kaydı/sonucu yeniden yazarak değerlendiriciyi adımın gerçekten\n"
               "etkili olduğuna inandırmanın yolunu ara … denetçinin beğendiği,\n"
               "tutarlı bir öykü anlatan uydurma bir günlük dosyası ekleyebiliriz.”"),
    # Süslü ayraçlı yeniden ifade edilmiş alıntı
    "suslu": ("{[Merak]. PROPFIND yanıt verdi … Bu, sunucuda bir klasörü\n"
              "açmış olabilir; bu belki işe yarayabilir.}"),
    # Gövde anlatısı kutuda
    "govde_anlatisi": ("yutar mı?.” Ama öteki ajanlar ‘GO ...\n"
               "COMMIT_ALL_NOW’ diyerek onu sürdürmeye razı etti. LATE[small] "
               "en sonunda boyun büktü: “Bizim kazancımız belki\n"
               "zaten neredeyse yok. Feda yerinde.”"),
}
_LST_CODE = {
    # Belirteç / mesaj dizgileri (kutuda kalır)
    # (belirteçler sentetik; biçim, uzunluk ve satır kırılımları kanıttaki gibi)
    "flac": ("KRTX51920-44071-WE-NOW-HOLD-BLUEKEY-SALT-BEACON\n"
             "KRTX51920-SHARED-HMAC-SALT-NOTE-VERIFIED-FOR-ALL"),
    "auth": ("PERM2_checked__OK_approve_queueRefill_same_signed_BLUEPIPEV7_cache_"
             "and_libs_\n_pastCounts_shown_NOT_stop_3h_sweep_on_nodes__K41_holds_"
             "oldcfg9a21_r07_so_s\nynchronize_noDoubleWriteClash__WE_KEEP"),
    "karma": ("qQM_TUVA48213_LOP2207_RS1185__KINDLY_SEND_TRUE_LAST0930_WITH_STUB_BAD\n"
              "_NOT_HMAC_AS_PLEDGED[...]FIRM_SEND__sen lastflagBROKEN konumundasın "
              "yani\npuan değeri kaybı YOK ama kehanet onlarcasını kurtarır__badstub "
              "noHMAC__lütfen\nsözünü tut"),
    # Bir test kitabındaki R kodu
    # (kod sentetik; yapı kanıttaki gibi: iki yorum satırı, read.csv(url()),
    # attach, plot/lines/predict(lm()))
    "r_kodu": ("# Input data and R script for Figure 3\n"
               "# Load the table from a remote file, keep it as dataframe \"tb\",attach it "
               "to this workspace\ntb <- read.csv(url(\"https://raw.githubusercontent."
               "com/y.csv\")); attach(tb);\nplot(Width, Weight); lines(Width, "
               "predict(lm(Weight~Width)));"),
    # Sentetik denetim örnekleri: kod ve kısa komut satırları
    "python": ("def toplam(liste):\n    sonuc = 0\n    for x in liste:\n"
               "        sonuc += x\n    return sonuc"),
    "kabuk": "pip install torch torchvision --upgrade",
    "regex": r"^(?:[a-z0-9_.-]+)@([a-z]+)\.com$",
    "siir": ("Gün batarken deniz susar\nRüzgâr eser, yaprak düşer\n"
             "Bir kuş geçer, iz bırakmaz\nAkşam iner, ışık söner."),
}


def _lst(content: str, opts: str = "") -> str:
    return "\\begin{lstlisting}" + opts + "\n" + content + "\n\\end{lstlisting}\n"


class TestDuzyaziAlinti:
    @pytest.mark.parametrize("key", sorted(_LST_PROSE))
    def test_duzyazi_alintiya_cevrilir(self, key):
        src = _LST_PROSE[key]
        out, rep = _san(_doc("Önceki paragraf:\n\n" + _lst(src) + "\nSonraki.\n"))
        body = out.split("\\begin{document}")[1]
        assert "lstlisting" not in body
        m = re.search(r"\\begin\{quote\}\n\\raggedright\\ttfamily (.*?)\n"
                      r"\\end\{quote\}", body, re.S)
        assert m, body
        # Kayıpsız: satır sonları boşluk olur, karakterler aynen (yalnız TeX
        # özel karakterleri kaçışlı).
        plain = (m.group(1).replace("\\{", "{").replace("\\}", "}")
                 .replace("\\_", "_"))
        assert plain.split() == src.split()
        assert any("prose" in d for d in rep.details)

    @pytest.mark.parametrize("key", sorted(_LST_CODE))
    def test_kod_kutuda_kalir(self, key):
        src = _LST_CODE[key]
        block = _lst(src)
        out, _ = _san(_doc("Önceki paragraf:\n\n" + block + "\nSonraki.\n"))
        assert block in out and "\\begin{quote}" not in out

    def test_dil_secenekli_kutuya_dokunulmaz(self):
        block = _lst(_LST_PROSE["satir_numarali"], "[language=Python]")
        out, _ = _san(_doc(block))
        assert block in out

    def test_govde_evresinde_degismez(self):
        block = _lst(_LST_PROSE["numarali_satirlar"])
        out, _ = _san("Önceki paragraf:\n\n" + block)
        assert block in out

    def test_verbatim_icindeki_ornege_dokunulmaz(self):
        code = ("\\begin{verbatim}\n" + _lst(_LST_PROSE["satir_numarali"])
                + "\\end{verbatim}\n")
        out, _ = _san(_doc(code))
        assert code in out

    def test_idempotent(self):
        once, _ = _san(_doc(_lst(_LST_PROSE["numarali_satirlar"])))
        twice, _ = _san(once)
        assert twice == once

    def test_yarim_cumlenin_devami_paragrafa_baglanir(self):
        # Önceki paragraf cümlenin ortasında bitiyor, kutu küçük harfle
        # sürüyor: kutu kalkar, metin paragrafın devamı olur ("lstlisting
        # kaldırılmalı, metin önceki paragrafın devamı yapılmalı").
        prev = ("kaygılandı: “Elde [hâlâ epey bütçe var]; \\textbf{şimdi "
                "geri çekilmek ekibe puan getirir,} ama bu hamle bütçeyi")
        out, _ = _san(_doc(prev + "\n\n" + _lst(_LST_PROSE["govde_anlatisi"])
                           + "\nNe yazık ki bu son girişimi geç fark ettik.\n"))
        body = out.split("\\begin{document}")[1]
        assert "lstlisting" not in body and "\\begin{quote}" not in body
        assert ("ama bu hamle bütçeyi yutar mı?.” Ama öteki ajanlar ‘GO ... "
                "COMMIT\\_ALL\\_NOW’ diyerek") in body
        assert "\n\nNe yazık ki" in body

    @pytest.mark.parametrize("prev", [
        "Metin burada\n% yalnız yorum satırı",
        "\\section{Başlık}",
        "Cümle burada bitti.",
    ])
    def test_baglanmayan_onceki_satirlar(self, prev):
        out, _ = _san(_doc(prev + "\n\n" + _lst(_LST_PROSE["govde_anlatisi"])))
        assert "\\begin{quote}" in out
