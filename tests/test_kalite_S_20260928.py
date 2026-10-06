"""Kulvar S (kalite dalgası, 2026-09-25 → 28): sanitizer + autofix.

Dil teyidi raporunun (§9, iç denetim)
S kulvarına düşen maddelerinin regresyon testleri. Girdiler SENTETİKTİR
(kısa, bu test için yazılmış cümleler; kitap düzyazısı yok).

R6a  Autofix'in satır içi matematik onarımları iki matematik bölgesinin
     ARASINDAKİ düzyazıyı '$...$' sanıyordu: '$${}^{^1}$$ Türkçe dipnot ...
     $\\lambda$' satırında dipnot metni display matematiğe düşüyordu
     (sentetik de: "Display math should end with $$", harfleri eksik metin).
R6b  '$m^{2}$ ${}^{K}$' birleştirmesi '$m^{2}^{K}$' -> "Double superscript".
R6c  "Inline math recovery" boş satıra / yapı satırına / komşu satırda zaten
     duran ifadeyi ekliyordu (sentetik fr: paragraf dışında '$X^{e}$').
R6d  Yunanca SÖZCÜK harf harf matematik sembolüne çevriliyordu (σελἡνη).
R13  [H] -> [!htb] gevşetmesi float dışındaki altyazıyı görselden ayırıyordu;
     altyazı paragrafı float ile tek birim olur (yalnız tam belge). G3a-1
     (2026-10-05): [H] artık hiç gevşetilmez; birim [!htb]
     float olarak kaymaz, kaynak sırasında yerinde dizilir (tests/test_G3a_g.py).
R17  Satır sonu yorum '%'si ('\\resizebox{..}{!}{%') kaçışlanıp görünür
     '%' basılıyordu (bir test kitabında 6 tablo parçası).
Ek   Autofix '\\text' parantez onarımı '\\textheight', '\\textfraction',
     '\\textfloatsep' gibi uzun komutları bozmaz (KNOWN_LONGER_COMMANDS
     listesinden bağımsız).
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

import latex_pipeline.autofix as autofix_mod
from latex_pipeline.autofix import (
    AutoFixer,
    _find_math_spans,
    _join_math_scripts,
    _leading_script_kinds,
    _restore_blocked_lines,
    _trailing_script_kinds,
)
from latex_pipeline.qc_engine import QCIssue
from latex_pipeline.sanitizer import (
    FIGURE_MAX_HEIGHT,
    LatexSanitizer,
    _caption_like_para,
    _float_caption_sides,
    dollar_math_spans,
)


def _san(text: str, manual: bool = True):
    return LatexSanitizer(manual_translation=manual).sanitize(text)


def _doc(body: str, preamble: str = "") -> str:
    """Şablon benzeri tam belge (Phase 6 girdisi)."""
    return ("\\documentclass{article}\n\\usepackage{graphicx}\n"
            "\\usepackage{float}\n" + preamble
            + "\\begin{document}\n" + body + "\n\\end{document}\n")


def _fixer() -> AutoFixer:
    fx = AutoFixer.__new__(AutoFixer)
    fx._source_body = None
    return fx


def _issue(check: str, line: int, context: str = "") -> QCIssue:
    return QCIssue(check, "high", line, "sentetik", True, 0.9, "", context)


def _broken_inline(body: str, line: int = 1) -> str:
    out, _ = AutoFixer._fix_broken_inline_math(
        _fixer(), body, [_issue("broken_inline_math", line)],
        _find_math_spans(body))
    return out


# ---------------------------------------------------------------------------
# R6a: '$' / '$$' okuma kuralı ve satır içi matematik bölgeleri
# ---------------------------------------------------------------------------

class TestR6aDolarOkuma:
    def test_display_ve_inline_ayrilir(self):
        text = "$$a$$ metin $b$"
        assert dollar_math_spans(text) == [(0, 5, True), (12, 15, False)]

    def test_bitisik_iki_inline_display_degildir(self):
        # '$\Theta$${}^{*}$': ortadaki '$$' kapat + yeni inline aç.
        text = "$\\Theta$${}^{*}$"
        spans = dollar_math_spans(text)
        assert spans == [(0, 8, False), (8, 16, False)]
        assert [text[s:e] for s, e, _ in spans] == ["$\\Theta$", "${}^{*}$"]

    def test_kacisli_dolar_ve_paragraf_siniri(self):
        text = "fiyat \\$5 ve $x$\n\n$kapanmayan"
        assert dollar_math_spans(text) == [(13, 16, False)]

    def test_dipnot_metni_display_matematige_dusmez(self):
        line = ("$${}^{^1}$$ Türkçe dipnot metni burada ise  $\\lambda$  "
                "yaygındır.")
        assert _broken_inline(line) == line

    def test_iki_bolge_arasindaki_duzyazi_birlestirilmez(self):
        line = "$$x$$ ve deneme metni $y$ ile"
        assert _broken_inline(line) == line

    def test_gercek_bitisik_parca_yine_birlesir(self):
        # Pozitif kontrol: onarım gerçek inline bölgelerde çalışmaya devam eder.
        out = _broken_inline("değer $x$ $^{2}$ olur")
        assert "$x^{2}$" in out


# ---------------------------------------------------------------------------
# R6b: aynı türden ikinci simge boş çekirdekle ayrılır
# ---------------------------------------------------------------------------

class TestR6bCiftUstSimge:
    @pytest.mark.parametrize("expr,kinds", [
        ("m^{2}", {"^"}),
        ("x_{i}^{2}", {"_", "^"}),
        ("f'", {"^"}),
        ("x^2", {"^"}),
        ("x", set()),
        ("a\\^{}", set()),
    ])
    def test_son_cekirdek_simgeleri(self, expr, kinds):
        assert _trailing_script_kinds(expr) == kinds

    @pytest.mark.parametrize("expr,kinds", [
        ("^{K}", {"^"}),
        ("^{K}_{i}", {"^", "_"}),
        ("_\\alpha", {"_"}),
        ("K", set()),
    ])
    def test_bastaki_simgeler(self, expr, kinds):
        assert _leading_script_kinds(expr) == kinds

    @pytest.mark.parametrize("first,second,joined", [
        ("m^{2}", "^{K}", "m^{2}{}^{K}"),
        ("x_{i}", "_{j}", "x_{i}{}_{j}"),
        ("f'", "^{2}", "f'{}^{2}"),
        ("x", "^{2}", "x^{2}"),
        ("x_{i}", "^{2}", "x_{i}^{2}"),
    ])
    def test_birlestirme(self, first, second, joined):
        assert _join_math_scripts(first, second) == joined

    def test_satir_onariminda_cift_ust_simge_uretilmez(self):
        line = "havada $\\alpha$ = 5 W/($m^{2}$ ${}^{K}$) ile soğur"
        out = _broken_inline(line)
        assert "^{2}^{K}" not in out
        assert "$m^{2}{}^{K}$" in out


# ---------------------------------------------------------------------------
# R6c: satır içi matematiğin geri eklenmesi
# ---------------------------------------------------------------------------

_R6C_EN = "\n".join([
    "Intro with $a$ here.",
    "At the start of the X$X^{e}$ century it changed.",
    "\\begin{center}",
    "Centered.",
    "\\end{center}",
])
_R6C_TR = "\n".join([
    "Giriş $a$ burada.",
    "X$X^{e}$ yüzyılın başında değişti.",
    "",
    "",
    "\\begin{center}",
    "Ortada.",
    "\\end{center}",
])


def _restore(en: str, tr: str, context: str):
    fx = _fixer()
    fx._source_body = en
    return AutoFixer._fix_inline_math_loss(
        fx, tr, [_issue("inline_math_loss", 1, context)], _find_math_spans(tr))


class TestR6cGeriEkleme:
    def test_bos_satira_ve_komsuda_duran_ifade_eklenmez(self):
        # Orantılı hizalama EN satırını TR'deki boş satıra düşürür; ifade
        # zaten bir önceki satırda. Eskiden boş satıra ' $X^{e}$ ' eklenip
        # paragraf dışında başıboş bir satır oluyordu.
        out, fixes = _restore(_R6C_EN, _R6C_TR, "en=3,tr=2")
        assert out == _R6C_TR
        assert fixes == []

    def test_gercekten_eksik_ifade_duzyaziya_eklenir(self):
        en = "The value $v$ grows with $t$ here.\nNext line."
        tr = "Değer zamanla artar.\nSonraki satır."
        out, fixes = _restore(en, tr, "en=2,tr=0")
        assert "$v$" in out.split("\n")[0] and "$t$" in out.split("\n")[0]
        assert fixes and fixes[0].method == "restore"

    @pytest.mark.parametrize("expr,ok", [
        ("{}^{1}", False),
        ("^{0}", False),
        ("{}^{&}", False),
        ("a & b", False),
        ("x^{2}", True),
        ("\\alpha", True),
        ("\\begin{matrix}a&b\\end{matrix}", True),
    ])
    def test_geri_eklenebilir_ifade(self, expr, ok):
        assert AutoFixer._is_restorable_math(expr) is ok

    def test_engelli_satirlar(self):
        lines = [
            "Düz metin.",                       # 0
            "\\begin{equation*}",               # 1
            "a = b",                            # 2
            "\\end{equation*}",                 # 3
            "\\begin{tabular}{ll}",             # 4
            "x & y \\\\",                        # 5
            "\\end{tabular}",                   # 6
            "$$",                               # 7
            "c = d",                            # 8
            "$$",                               # 9
            "Yine metin.",                      # 10
        ]
        blocked = _restore_blocked_lines(lines)
        assert {1, 2, 3, 5, 7, 8, 9} <= blocked
        assert 0 not in blocked and 10 not in blocked

    @pytest.mark.parametrize("line", [
        "\\end{itemize}", "\\item", "\\section{Başlık}", "% yorum",
        "\\includegraphics{a.png}", "\\toprule",
    ])
    def test_yapi_satiri_atlanir(self, line):
        assert AutoFixer._RESTORE_SKIP_TR_LINE_RE.match(line)

    @pytest.mark.parametrize("line", ["\\itemize ile", "Metin \\item"])
    def test_yapi_olmayan_satir_atlanmaz(self, line):
        assert not AutoFixer._RESTORE_SKIP_TR_LINE_RE.match(line)


# ---------------------------------------------------------------------------
# R6d: Yunanca sözcük metinde kalır, tek harf sarılır
# ---------------------------------------------------------------------------

class TestR6dYunanca:
    def test_yunanca_sozcuk_metinde_kalir(self):
        out, _ = _san("De σελἡνη, sözcük Ay demektir.")
        assert "σελἡνη" in out
        assert "$\\sigma$" not in out and "$\\nu$" not in out

    def test_haritadaki_harflerden_sozcuk_de_kalir(self):
        out, _ = _san("Kök sözcük λογος olarak geçer.")
        assert "λογος" in out and "$\\lambda$" not in out

    @pytest.mark.parametrize("text,expected", [
        ("Fark ΔT kadar.", "$\\Delta$T"),
        ("Boy 5 μm olur.", "$\\mu$m"),
        ("Oran Δσ ile.", "$\\Delta$$\\sigma$"),
    ])
    def test_tek_harf_ve_kisa_dizi_sarilir(self, text, expected):
        out, _ = _san(text)
        assert expected in out


# ---------------------------------------------------------------------------
# R13: float dışındaki altyazı float ile birlikte taşınır
# ---------------------------------------------------------------------------

_FIG = ("\\begin{figure}[H]\n  \\centering\n"
        "  \\includegraphics[width=0.6\\textwidth]{a.png}\n\\end{figure}\n\n")
_TAB = ("\\begin{table}[H]\n\\centering\n\\begin{tabular}{ll}\na & b \\\\\n"
        "\\end{tabular}\n\\end{table}\n")


class TestR13Olcut:
    @pytest.mark.parametrize("para,kind,ok", [
        ("Şekil 1.4. Deneme şeması", "figure", True),
        ("\\textbf{Şekil 2.1:} Kısa açıklama", "figure", True),
        ("Şek. 2.10 Kayıp eğrisi", "figure", True),
        ("Abb. 1.3 Karte", "figure", True),
        ("Рис. 1.2. Схема", "figure", True),
        ("Şekil 1.1'de görüldüğü gibi", "figure", False),
        ("Şekil 1.1 ve 1.2'de görüldüğü gibi", "figure", False),
        ("Şekil’ 3 deneme", "figure", False),
        ("Şekil 2.3, ağın yapısını gösterir.", "figure", False),
        ("Bu paragraf şekilden söz eder.", "figure", False),
        ("Tablo 1.1: Değerler", "table", True),
        ("Tablo 2, 3 ve 4'te verilir.", "table", False),
        ("Şekil 1.1. Deneme", "table", False),
    ])
    def test_altyazi_olcutu(self, para, kind, ok):
        assert _caption_like_para(para, kind) is ok


class TestR13Yerlesim:
    def test_govdede_dis_altyazili_sekil_H_kalir(self):
        out, rep = _san(_FIG + "Şekil 1.1. Deneme şeması\n")
        assert "\\begin{figure}[H]" in out and "\\setbox0" not in out
        assert any("[H] kept" in d for d in rep.details)

    # G3a-1: [H] hiçbir durumda [!htb]'ye gevşetilmez (kaynak sırası).
    def test_atif_cumlesi_altyazi_sayilmaz(self):
        out, rep = _san(_doc(_FIG + "Şekil 1.1'de görüldüğü gibi değer artar.\n"))
        assert "[!htb]" not in out
        assert not any("one float" in d for d in rep.details)
        assert "\\par\\setbox0\\vbox{\\begin{figure}[H]" in out

    def test_ic_altyazili_sekil_H_kalir(self):
        body = _FIG.replace("\\end{figure}",
                            "  \\caption{Deneme}\n\\end{figure}")
        out, _ = _san(body + "Şekil 1.1. Deneme\n")
        assert "\\begin{figure}[H]" in out and "[!htb]" not in out

    def test_altyazisiz_sekil_H_kalir(self):
        out, _ = _san(_FIG + "Düz bir paragraf.\n")
        assert "\\begin{figure}[H]" in out and "[!htb]" not in out

    def test_tablo_onceki_paragraf_altyazisi(self):
        out, _ = _san("Tablo 1.1: Değerler\n\n" + _TAB)
        assert "\\begin{table}[H]" in out

    def test_tam_belgede_tek_birim(self):
        out, rep = _san(_doc(_FIG + "Şekil 1.1. Deneme şeması\n\nSonraki metin.\n"))
        unit = re.search(
            r"\\par\\setbox0\\vbox\{%\n\{(.*?)\\par\}\\par\\vskip\\abovecaptionskip\n\{\\noindent\\centering Şekil 1\.1\. Deneme "
            r"şeması\\par\}\\par\}%\n\\ifdim\\dimexpr\\ht0\+\\dp0"
            r"\\relax>\\textheight\\unvbox0 \\else\\vskip\\intextsep"
            r"\\box0 \\fi\n\\vskip\\intextsep\\par", out, re.S)
        assert unit, out
        assert "\\includegraphics" in unit.group(1)
        assert "[H]" not in out.split("\\begin{document}")[1]
        assert "Sonraki metin." in out
        assert any("one float" in d for d in rep.details)

    def test_tam_belgede_tablo_birimi_altyazi_once(self):
        out, _ = _san(_doc("Metin.\n\nTablo 1.1: Değerler\n\n" + _TAB))
        assert re.search(r"\\setbox0\\vbox\{%\n\{\\noindent\\centering Tablo 1\.1: Değerler\\par\}\\par\\vskip\\abovecaptionskip\n\{",
                         out)
        assert "\\else\\vskip\\intextsep\\box0 \\fi" in out

    def test_ardisik_tablolar_ust_altyazi_yonu(self):
        # Belgede tablo altyazısı üstte: "Tablo 1.2" ikinci tablonundur,
        # birinci tabloya yapıştırılmaz (bir test kitabında, farksal).
        body = ("Giriş.\n\nTablo 1.1: Birinci\n\n" + _TAB + "\nTablo 1.2: İkinci\n\n"
                + _TAB + "\nDüz metin.\n\nTablo 1.3: Üçüncü\n\n" + _TAB
                + "\nSon metin.\n")
        assert _float_caption_sides(body)["table"] == "before"
        out, _ = _san(_doc(body))
        units = re.findall(r"\\setbox0\\vbox\{%\n\{\\noindent\\centering (Tablo 1\.\d)", out)
        assert units == ["Tablo 1.1", "Tablo 1.2", "Tablo 1.3"]

    def test_ardisik_tablolar_alt_altyazi_yonu(self):
        body = ("Giriş.\n\n" + _TAB + "\nTablo 1.1: Birinci\n\n" + _TAB
                + "\nTablo 1.2: İkinci\n\nSon metin.\n")
        assert _float_caption_sides(body)["table"] == "after"
        out, _ = _san(_doc(body))
        assert re.findall(r"\\par\}\\par\\vskip\\abovecaptionskip\n\{\\noindent\\centering (Tablo 1\.\d)", out) == [
            "Tablo 1.1", "Tablo 1.2"]

    def test_sekil_altyazisi_ustte_olan_belge(self):
        # Altyazı şeklin önünde yazılmışsa sonraki şeklin altyazısı öncekine
        # yapıştırılmaz: her şekil kendi (önceki) altyazısıyla birleşir.
        fig = _FIG
        body = ("Giriş.\n\nŞekil 1.1. Birinci\n\n" + fig + "Metin.\n\n"
                "Şekil 1.2. İkinci\n\n" + fig + "Şekil 1.3. Üçüncü\n\n" + fig
                + "Son metin.\n")
        assert _float_caption_sides(body)["figure"] == "before"
        out, _ = _san(_doc(body))
        assert re.findall(r"\\setbox0\\vbox\{%\n\{\\noindent\\centering (Şekil 1\.\d)", out) == [
            "Şekil 1.1", "Şekil 1.2", "Şekil 1.3"]

    def test_belirsiz_dizilis_eslenmez(self):
        # İki görsel art arda + iki altyazı art arda: hangi altyazının hangi
        # görsele ait olduğu belirsiz; yapıştırma yapılmaz (eski davranış).
        body = ("Giriş.\n\n" + _FIG + _FIG + "Şekil 1.13. Birinci\n\n"
                "Şekil 1.14. İkinci\n\nSon metin.\n")
        out, rep = _san(_doc(body))
        assert not any("one float" in d for d in rep.details)
        # G3a-1: iki görsel de yerinde ölçülü birim; altyazılar dışarıda kalır.
        assert out.count("\\par\\setbox0\\vbox{\\begin{figure}[H]") == 2
        assert "[!htb]" not in out

    def test_altyazi_dizisinde_en_yakin_altyazi(self):
        # Tek görsel + art arda iki altyazı: bitişik altyazı görselindir
        # (iki sütunlu bir kitapta: Şekil 1.2 ardından Şekil 1.3'ün altyazısı).
        body = ("Giriş.\n\n" + _FIG + "Şekil 1.2. Birinci\n\n"
                "Şekil 1.3. İkinci\n\nSon metin.\n")
        out, _ = _san(_doc(body))
        assert re.findall(r"\\setbox0\\vbox\{%\n\{.*?\\par\\vskip\\abovecaptionskip\n\{\\noindent\\centering (Şekil 1\.\d)", out,
                          re.S) == ["Şekil 1.2"]
        assert "Şekil 1.3. İkinci" in out

    def test_ic_altyazili_komsu_float_belirsizlik_sayilmaz(self):
        captioned = _FIG.replace("[H]", "[!htb]").replace(
            "\\end{figure}", "  \\caption{İç}\n\\end{figure}")
        body = "Giriş.\n\n" + captioned + _FIG + "Şekil 1.2. Dış\n\nSon.\n"
        out, _ = _san(_doc(body))
        assert re.search(r"\\setbox0\\vbox\{%\n\{.*?\\par\\vskip\\abovecaptionskip\n\{\\noindent\\centering Şekil 1\.2\. Dış", out,
                         re.S)

    def test_esitlikte_varsayilan_yon(self):
        assert _float_caption_sides("Düz metin.") == {
            "figure": "after", "table": "before"}

    def test_tam_belge_idempotent(self):
        once, _ = _san(_doc(_FIG + "Şekil 1.1. Deneme\n"))
        twice, _ = _san(once)
        assert twice == once

    @pytest.mark.parametrize("caption", [
        "Şekil 1.1. Deneme\\footnote{Not.}",
        "Şekil 1.1. " + "uzun " * (LatexSanitizer._GLUE_MAX_CAPTION_CHARS // 5),
    ])
    def test_birime_alinmayan_altyazi_H_kalir(self, caption):
        out, rep = _san(_doc(_FIG + caption + "\n"))
        assert not any("one float" in d for d in rep.details)
        # G3a-1: görsel yerinde ölçülü birim; altyazı paragrafı birimin dışında.
        assert "\\par\\setbox0\\vbox{\\begin{figure}[H]" in out
        assert out.index("\\vskip\\intextsep\\par") < out.index(caption[:10])

    def test_kod_ornegi_birime_alinmaz(self):
        code = ("\\begin{verbatim}\n" + _FIG + "Şekil 9.9. Örnek\n"
                "\\end{verbatim}\n")
        out, _ = _san(_doc(code))
        assert "\\setbox0" not in out


# ---------------------------------------------------------------------------
# R17: satır sonu yorum '%'si kaçışlanmaz
# ---------------------------------------------------------------------------

class TestR17YuzdeYorum:
    @pytest.mark.parametrize("line", [
        "\\resizebox{\\textwidth}{!}{%",
        "\\newcommand{\\x}{%",
        "\\mbox{a}%",
        "\\resizebox{\\textwidth}{!}{%   ",
    ])
    def test_satir_sonu_yorum_korunur(self, line):
        out, _ = _san("Metin.\n" + line + "\nİçerik}\n")
        assert line.rstrip() in out and "{\\%" not in out and "}\\%" not in out

    @pytest.mark.parametrize("line,expected", [
        ("oran %36 oldu", "oran \\%36 oldu"),
        ("değer {%50} ile", "değer {\\%50} ile"),
        ("yüzde %", "yüzde \\%"),
    ])
    def test_duzyazi_yuzdesi_kacislanir(self, line, expected):
        out, _ = _san(line + "\n")
        assert expected in out


# ---------------------------------------------------------------------------
# Ek: '\text' parantez onarımı uzun komut adını korur
# ---------------------------------------------------------------------------

class TestTextUzunKomut:
    _BODY = (
        "\\includegraphics[width=0.5\\textwidth,height=0.7\\textheight]{a}\n"
        "\\setlength{\\textfloatsep}{8pt}\n"
        "\\renewcommand{\\textfraction}{0.05}\n"
        "\\setlength{\\fboxsep}{2pt}\n"
        "Boşluk \\vspace{0.1\\textheight} sonra.\n"
        "Bozuk \\textbfSpektrum ve \\textdeğer burada.\n"
        "Cümle sonu.\\textbfYeni başlık.\n"
    )

    @pytest.mark.parametrize("known", ["liste", "bos"])
    def test_uzun_komutlar_korunur(self, monkeypatch, known):
        if known == "bos":
            # Kulvar Q'nun listesi olmadan da doğru olmalı.
            monkeypatch.setattr(autofix_mod, "KNOWN_LONGER_COMMANDS",
                                frozenset())
        body = self._BODY
        fixed, fixes = AutoFixer._fix_broken_command_braces(
            _fixer(), body, [], _find_math_spans(body))
        for cmd in ("\\textheight", "\\textfloatsep", "\\textfraction",
                    "\\fboxsep", "\\textwidth"):
            assert cmd in fixed, cmd
        assert "\\text{height}" not in fixed and "\\fbox{sep}" not in fixed
        # Gerçek kırıklar yine onarılır.
        assert "\\textbf{Spektrum}" in fixed
        assert "\\text{değer}" in fixed
        # Noktadan sonra bitişik kırık da onarılır (nokta uzunluk bağlamı değil).
        assert "\\textbf{Yeni} başlık" in fixed

    def test_sinir_eklenmis_tam_belge_bozulmaz(self, monkeypatch):
        monkeypatch.setattr(autofix_mod, "KNOWN_LONGER_COMMANDS", frozenset())
        full, _ = _san(_doc(_FIG + "Şekil 1.1. Deneme\n\n\\textbfSpektrum\n"))
        assert "height=" + FIGURE_MAX_HEIGHT in full
        fixed, _ = AutoFixer._fix_broken_command_braces(
            _fixer(), full, [], _find_math_spans(full))
        assert "\\text{height}" not in fixed
        # G3a-1: birim artık \\intextsep ile yerinde (\\textfloatsep yok).
        assert "\\intextsep" in fixed and "\\textheight" in fixed
        assert "\\textbf{Spektrum}" in fixed


# ---------------------------------------------------------------------------
# Gerçek XeLaTeX: altyazı görselle aynı sayfada
# ---------------------------------------------------------------------------

_XELATEX = shutil.which("xelatex")
_GEOM = ("\\usepackage{fontspec}\n"
         "\\usepackage[paperwidth=140mm,paperheight=200mm,margin=20mm]"
         "{geometry}\n")
_FILLER = ("Bu bir deneme cümlesidir. " * 13 + "\n\n") * 5


def _pages(aux: str) -> dict:
    return {m.group(1): int(m.group(2)) for m in re.finditer(
        r"\\newlabel\{(\w+)\}\{\{[^{}]*\}\{(\d+)\}", aux)}


def _compile(tmp_path, name: str, tex: str) -> tuple:
    (tmp_path / f"{name}.tex").write_text(tex, encoding="utf-8")
    subprocess.run(
        [_XELATEX, "-interaction=nonstopmode", "-no-shell-escape",
         f"{name}.tex"], cwd=tmp_path, capture_output=True, timeout=180)
    log = (tmp_path / f"{name}.log").read_text(encoding="utf-8",
                                               errors="replace")
    aux = (tmp_path / f"{name}.aux").read_text(encoding="utf-8",
                                               errors="replace")
    return log, aux


@pytest.mark.skipif(_XELATEX is None, reason="xelatex yok")
class TestR13Xelatex:
    @pytest.fixture()
    def img(self, tmp_path):
        from PIL import Image
        Image.new("RGB", (200, 240), "black").save(tmp_path / "a.png")
        Image.new("RGB", (100, 900), "black").save(tmp_path / "uzun.png")
        return tmp_path

    def _body(self, graphic: str) -> str:
        return (_FILLER
                + "\\begin{figure}[H]\n\\centering\n" + graphic
                + "\\label{gorsel}\n\\end{figure}\n\n"
                "Şekil 1.1. Deneme şeması\\label{altyazi}\n\n" + _FILLER)

    def test_altyazi_gorselle_ayni_sayfada(self, img):
        body = self._body("\\includegraphics[width=0.6\\textwidth]{a.png}\n")
        # Kontrol: altyazı float dışındayken [!htb] görseli sonraki sayfaya
        # taşır, altyazı metin akışında kalır (eski B23 davranışı).
        log0, aux0 = _compile(img, "eski", _doc(body.replace("[H]", "[!htb]"),
                                                _GEOM))
        p0 = _pages(aux0)
        assert p0["gorsel"] != p0["altyazi"]
        san, _ = _san(_doc(body, _GEOM))
        log, aux = _compile(img, "yeni", san)
        p = _pages(aux)
        assert p["gorsel"] == p["altyazi"]
        assert not re.search(r"^! ", log, re.M)
        assert "Float too large" not in log

    def test_metin_alanindan_uzun_birim_yerinde_kalir(self, img):
        # Birim + \textfloatsep metin alanını aşarsa '\unvbox0' ile yerinde
        # dizilir: 'Float too large' yok, bölüm sonuna ertelenmez.
        body = self._body(
            "\\includegraphics[width=0.9\\textwidth,height=0.97\\textheight]"
            "{uzun.png}\n")
        san, _ = _san(_doc(body, _GEOM))
        assert "\\setbox0" in san
        log, aux = _compile(img, "uzun", san)
        assert not re.search(r"^! ", log, re.M)
        assert "Float too large" not in log
        p = _pages(aux)
        assert p["altyazi"] - p["gorsel"] in (0, 1)


# ---------------------------------------------------------------------------
# Vq (2026-09-28): altyazı birimi sınırı tek kaynaktan
# ---------------------------------------------------------------------------

def test_altyazi_siniri_tek_kaynaktan():
    # Sanitizer'ın birim yapıştırma sınırı dönüştürücünün float altyazı
    # sınırıyla aynı ölçüttür; ikinci bir sayı literali yazılmaz.
    import inspect

    import latex_pipeline.patterns as pt
    import latex_pipeline.sanitizer as sm

    assert sm.LatexSanitizer._GLUE_MAX_CAPTION_CHARS == pt.FLOAT_CAPTION_MAX_CHARS
    src = inspect.getsource(sm)
    assert not re.search(r"_GLUE_MAX_CAPTION_CHARS\s*=\s*\d", src)
