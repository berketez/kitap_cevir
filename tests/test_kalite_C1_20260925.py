"""Dönüştürücü yapı düzeltmeleri, kalite kulvarı C1 (25 Eylül 2026).

Karşılaştırma raporu (özgün PDF/EPUB ile Türkçe çıktı) maddeleri:
  R8   başlık hiyerarşisi: kutu başlıkları (Exkurs/Encadré/Recuadro/Riquadro/
       Врезка/Box) numarasız \\subsubsection* olur ve numaralamayı kaydırmaz;
       EPUB bölüm alt adı (ALTAD_MARKER, epub_parser sözleşmesi) bölüm
       başlığına katılır; numarasız bölümlerde yetim alt başlıklar terfi eder.
  R10  Rus usulü "2.1." numarası (sondaki nokta) bölüm numarası sayılır.
  R12  numaralı paragraflar tek liste; madde devam satırları; "• 1." ve
       "• —" çift madde imi.
  R18  kaynakça başlığı numarasız; basılı içindekiler tablosu gövdeden çıkar.

28 Eylül 2026 devamı (TestC1Devam20260928): içindekiler devam sayfası üst
bilgisi ("viii ◾ Contents") + tablosu da çıkar; alt ad işaretinin tek kaynağı
patterns.ALTAD_MARKER; E1'in boş satırsız biçimi; konu özeti biçimindeki alt
ad başlığa katılmaz (yalın etiket ilk konuyu ad olarak alır).

Tüm başlık ve cümleler SENTETİKTİR (kitaptan düzyazı alınmaz).
"""

from __future__ import annotations

import re

import pytest

from latex_pipeline.config import PipelineConfig
from latex_pipeline.converter import (
    ALTAD_MARKER,
    MarkdownToLatex,
    parse_box_label,
)
from latex_pipeline.patterns import BIBLIOGRAPHY_HEADING_NAMES, fold_accents
from latex_pipeline.sanitizer import LatexSanitizer, apply_chapter_label_rules

_BODY = "Bu paragraf sentetik gövde metnidir ve bölümün içeriğini temsil eder.\n\n"


@pytest.fixture
def convert(tmp_path):
    def _run(markdown: str) -> str:
        cfg = PipelineConfig(document_class="book", output_dir=str(tmp_path))
        return MarkdownToLatex(cfg).convert(markdown)
    return _run


def _headings(tex: str) -> list[str]:
    return [ln for ln in tex.split("\n")
            if re.match(r"\\(part|chapter|section|subsection|subsubsection|paragraph)\*?\{", ln)]


def _numbered(tex: str) -> list[str]:
    """LaTeX'in numaralayacağı bölüm/alt bölüm komutları (yıldızsız)."""
    return [h for h in _headings(tex)
            if re.match(r"\\(chapter|section|subsection)\{", h)]


def _latex_numbers(tex: str) -> list[tuple[str, str]]:
    """Yıldızsız başlıkların LaTeX numarası (book sınıfı sayaçları)."""
    ch = sec = sub = 0
    out = []
    for h in _headings(tex):
        m = re.match(r"\\(chapter|section|subsection)\{(.*)\}$", h)
        if not m:
            continue
        kind, title = m.groups()
        if kind == "chapter":
            ch, sec, sub = ch + 1, 0, 0
            out.append((str(ch), title))
        elif kind == "section":
            sec, sub = sec + 1, 0
            out.append((f"{ch}.{sec}", title))
        else:
            sub += 1
            out.append((f"{ch}.{sec}.{sub}", title))
    return out


def _source_numbers_match(tex: str) -> bool:
    """Başlıktaki kaynak numarası ("1.2.1 X", "2.1. X") LaTeX numarasıyla aynı mı?"""
    ok = True
    for num, title in _latex_numbers(tex):
        m = re.match(r"^(\d+(?:\.\d+)+)\.?\s", title)
        if m and m.group(1) != num:
            ok = False
    return ok


# ===========================================================================
# R8a — kutu başlıkları
# ===========================================================================

class TestKutuEtiketi:
    @pytest.mark.parametrize("title,expected", [
        ("Exkurs 1.1", ("Exkurs 1.1", "")),
        ("Encadré 1.1 — Le décrochage", ("Encadré 1.1", "Le décrochage")),
        ("Recuadro 1.1. La paradoja de prueba", ("Recuadro 1.1", "La paradoja de prueba")),
        ("Riquadro 2.3 – Una prova", ("Riquadro 2.3", "Una prova")),
        ("Врезка 1.1", ("Врезка 1.1", "")),
        ("Box 4: Trial Title", ("Box 4", "Trial Title")),
        ("EXKURS 2", ("EXKURS 2", "")),
    ])
    def test_etiket_cozumlenir(self, title, expected):
        assert parse_box_label(title) == expected

    @pytest.mark.parametrize("title", [
        "Box office results", "Boxen im Alltag", "Exkurs", "Exkurs über Europa",
        "Cuadro 1.1 Valores", "Boxing 3 rounds",
    ])
    def test_kutu_olmayan_baslik(self, title):
        assert parse_box_label(title) is None

    def test_iki_satirli_kutu_tek_numarasiz_baslik(self, convert):
        md = ("# Kapitel 1 Grundlagen\n\n" + _BODY
              + "#### 1.1 Erster Abschnitt\n\n" + _BODY
              + "#### **Exkurs 1.1**\n\n#### **Ein Beispielkasten**\n\n" + _BODY
              + "## 1.2 Zweiter Abschnitt\n\n" + _BODY)
        heads = _headings(convert(md))
        assert "\\subsubsection*{Exkurs 1.1 — Ein Beispielkasten}" in heads
        assert not any("Ein Beispielkasten}" in h and "Exkurs" not in h for h in heads)

    def test_sig_kutu_basligi_numaralamayi_kaydirmaz(self, convert):
        # sent_ru örüntüsü: "#### Врезка 1.1" + "## <kutu başlığı>" \section
        # olup asıl 1.3'ü 1.4 yapıyordu.
        md = ("## Глава 1\n\n## Пробная глава\n\n" + _BODY
              + "## 1.1. Первый раздел\n\n" + _BODY
              + "## 1.2. Второй раздел\n\n" + _BODY
              + "#### Врезка 1.1\n\n## Пробный заголовок врезки\n\n" + _BODY
              + "## 1.3. Третий раздел\n\n" + _BODY)
        tex = convert(md)
        assert "\\subsubsection*{Врезка 1.1 — Пробный заголовок врезки}" in _headings(tex)
        assert [n for n, _ in _latex_numbers(tex)] == ["1", "1.1", "1.2", "1.3"]
        assert _source_numbers_match(tex)

    def test_kutu_basligi_bolum_adayi_olmaz(self, convert):
        # Almanca bir kitabın örüntüsü: "## Exkurs 1.1" + "# <kutu başlığı>" (etiketten SIĞ)
        md = ("# Kapitel 1 Europa\n\n" + _BODY
              + "## Exkurs 1.1\n\n# Reisen als Beispiel\n\n" + _BODY
              + "### Exkurs 1.2\n\n# Diskurse als Beispiel\n\n" + _BODY)
        tex = convert(md)
        heads = _headings(tex)
        assert heads[0] == "\\chapter{Europa}"
        assert "\\subsubsection*{Exkurs 1.1 — Reisen als Beispiel}" in heads
        assert "\\subsubsection*{Exkurs 1.2 — Diskurse als Beispiel}" in heads
        assert _numbered(tex) == ["\\chapter{Europa}"]

    def test_tek_satirli_kutu_aynen_kalir(self, convert):
        md = ("## Chapitre 1\n\n## Titre d'essai\n\n" + _BODY
              + "#### Encadré 1.1 — Le décrochage\n\n" + _BODY)
        assert "\\subsubsection*{Encadré 1.1 — Le décrochage}" in _headings(convert(md))

    def test_numarali_baslik_kutu_basligi_olmaz(self, convert):
        md = ("## Capítulo 1\n\n## Título de prueba\n\n" + _BODY
              + "#### Recuadro 1.1\n\n## 1.3 Sección siguiente\n\n" + _BODY)
        heads = _headings(convert(md))
        assert "\\subsubsection*{Recuadro 1.1}" in heads
        assert "\\section{1.3 Sección siguiente}" in heads

    def test_kutu_sanitizer_sonrasi_korunur(self, convert):
        md = ("# Kapitel 1 Grundlagen\n\n" + _BODY
              + "#### Exkurs 1.1\n\n#### Ein Beispielkasten\n\n" + _BODY)
        body, _ = LatexSanitizer(source_lang="de").sanitize(convert(md))
        assert "\\subsubsection*{Exkurs 1.1 — Ein Beispielkasten}" in body


# ===========================================================================
# R8b — EPUB bölüm alt adı (epub_parser sözleşmesi)
# ===========================================================================

class TestAltAd:
    def _epub(self, first_heading: str, altad: str) -> str:
        return ("# Deneme Kitabı\n\n*Yazar Adı*\n\n---\n\n# CAPÍTULO PRIMERO\n\n" + _BODY
                + f"{first_heading}\n\n{ALTAD_MARKER}{altad}\n\n" + _BODY)

    def test_yalin_etiket_alt_adi_baslik_alir(self, convert):
        tex = convert(self._epub("# CAPÍTULO VII", "Marcha de prueba."))
        assert "\\chapter{Marcha de prueba}" in _headings(tex)
        assert "\\chapter{CAPÍTULO VII}" not in tex
        assert "kc:altad" not in tex and "Marcha de prueba" not in tex.split("\\chapter{Marcha")[1]

    def test_rakamli_yalin_etiket_alt_adla_silinmez(self, convert):
        tex = convert(self._epub("# Capítulo 7", "Marcha de prueba"))
        assert "\\chapter{Marcha de prueba}" in tex
        # sanitizer'ın "sahte bölüm" kuralı (\chapter{Capítulo 7}) tetiklenmez
        assert "\\chapter{Marcha de prueba}" in apply_chapter_label_rules(tex)

    def test_etiketsiz_basliga_ayracla_eklenir(self, convert):
        md = ("# Deneme\n\n" + _BODY + "## I LE PREMIER ESSAI\n\n" + _BODY
              + "## II LE SECOND ESSAI\n\n" + f"{ALTAD_MARKER}Sous-titre d'essai\n\n" + _BODY)
        heads = _headings(convert(md))
        assert "\\chapter{LE SECOND ESSAI — Sous-titre d'essai}" in heads

    def test_yalin_roma_rakami_alt_adi_baslik_alir(self, convert):
        md = ("# Deneme\n\n" + _BODY + "# I.\n\n" + _BODY
              + "# II.\n\n" + f"{ALTAD_MARKER}Titolo di prova\n\n" + _BODY)
        chapters = [h for h in _headings(convert(md)) if h.startswith("\\chapter{")]
        assert chapters == ["\\chapter{I.}", "\\chapter{Titolo di prova}"]

    def test_basliga_baglanmayan_alt_ad_duz_paragraf(self, convert):
        md = "# Deneme\n\n" + _BODY + f"{ALTAD_MARKER}Serbest satır\n\n" + _BODY
        tex = convert(md)
        assert "Serbest satır" in tex and "kc:altad" not in tex and "<!" not in tex

    def test_cok_uzun_alt_ad_basliga_katilmaz(self, convert):
        uzun = "Sentetik alt ad " * 20
        tex = convert(self._epub("# CAPÍTULO VII", uzun))
        assert "\\chapter{CAPÍTULO VII}" in tex
        assert "kc:altad" not in tex and "Sentetik alt ad" in tex


# ===========================================================================
# R8c — yetim alt başlıklar
# ===========================================================================

class TestYetimAltBaslik:
    def test_bolum_altindaki_derin_baslik_section_olur(self, convert):
        # Almanca bir kitabın örüntüsü: gerçek h2 başlıklar Marker'da "####", biri "###".
        md = ("# Kapitel 1 Europa\n\n" + _BODY
              + "#### Raumbilder\n\n" + _BODY + "#### Grenzziehungen\n\n" + _BODY
              + "### Wandel der Sicht\n\n" + _BODY + "#### Unterpunkt\n\n" + _BODY)
        heads = _headings(convert(md))
        assert heads == ["\\chapter{Europa}", "\\section{Raumbilder}",
                         "\\section{Grenzziehungen}", "\\section{Wandel der Sicht}",
                         "\\subsection{Unterpunkt}"]

    def test_numarali_bolume_dokunulmaz(self, convert):
        # 2. bölümdeki "##" genel düzey boşluğunu 0 tutar; 1. bölümde çok
        # parçalı numaralı başlık olduğu için "####" başlığa dokunulmaz.
        md = ("# Kapitel 1 Europa\n\n" + _BODY + "## 1.1 Raumbilder\n\n" + _BODY
              + "#### Anmerkung\n\n" + _BODY
              + "# Kapitel 2 Asien\n\n" + _BODY + "## Zusammenfassung\n\n" + _BODY)
        heads = _headings(convert(md))
        assert "\\subsubsection{Anmerkung}" in heads
        assert "\\section{Zusammenfassung}" in heads

    def test_yalniz_terfi_indirme_yok(self, convert):
        md = ("# Kapitel 1 Europa\n\n" + _BODY + "## Raumbilder\n\n" + _BODY
              + "### Unterpunkt\n\n" + _BODY)
        assert _headings(convert(md)) == ["\\chapter{Europa}", "\\section{Raumbilder}",
                                          "\\subsection{Unterpunkt}"]


# ===========================================================================
# R10 — sondaki noktalı bölüm numarası
# ===========================================================================

class TestNoktaliNumara:
    @pytest.mark.parametrize("title,num", [
        ("2.1. Пробный раздел", "2.1"), ("1.2.1. Пробный пункт", "1.2.1"),
        ("A.1. Anhang Probe", "A.1"), ("2.1 Ohne Punkt", "2.1"),
        ("1. Einleitung", None), ("I. Titre", None), ("A. Probe", None),
    ])
    def test_numara(self, title, num, tmp_path):
        conv = MarkdownToLatex(PipelineConfig(document_class="book", output_dir=str(tmp_path)))
        got = conv._section_number(title)
        if num is None:
            assert got is None or "." not in got
        else:
            assert got == num

    def test_derin_duzeydeki_noktali_numara_section(self, convert):
        # sent_ru: "#### 2.1. ..." numarasız \subsubsection olup 2.2'yi "2.1" yapıyordu.
        md = ("## Глава 1\n\n## Первая\n\n" + _BODY + "## 1.1. Раздел А\n\n" + _BODY
              + "## Глава вторая\n\n## Вторая\n\n" + _BODY
              + "#### 2.1. Раздел Б\n\n" + _BODY + "## 2.2. Раздел В\n\n" + _BODY)
        tex = convert(md)
        assert "\\section{2.1. Раздел Б}" in _headings(tex)
        assert _source_numbers_match(tex)


# ===========================================================================
# R12 — listeler
# ===========================================================================

def _items(tex: str, env: str) -> list[list[str]]:
    # C (2026-10-06): 1'den başlamayan enumerate "\begin{enumerate}[start=N]"
    # yazılır (seçenek aynı satırda); blok başı seçenekli de olabilir.
    blocks = re.findall(r"\\begin\{" + env + r"\}(?:\[[^\]\n]*\])?\n(.*?)\n\\end\{" + env + r"\}",
                        tex, re.S)
    return [re.findall(r"^  \\item (.*)$", b, re.M) for b in blocks]


class TestListeler:
    # EPUB (satır kırpılmış) belgesi: numaralı paragraflar boş satırla
    # ayrılmış, italik başlık satır aşar.
    _EPUB = ("Kısa bir sentetik giriş paragrafı burada\nikinci satırıyla sürüyor,\nüçüncü satırda biter.\n\n"
             "Başka bir sentetik paragraf da\nüç satır olarak\nyazılmıştır.\n\n"
             "1. *Birinci deneme ilkesinin\nbaşlığı.*— Açıklama metni burada\nsürer ve biter.\n\n"
             "2. *İkinci ilke.*— Kısa\naçıklama.\n\n"
             "3. *Üçüncü ilke\nbaşlığı.*— Son açıklama\nKüçük Harfle Başlamayan satır.\n\n"
             "Liste sonrası paragraf burada\nsürmektedir.\n")

    def test_numarali_paragraflar_tek_liste(self, convert):
        tex = convert(self._EPUB)
        blocks = _items(tex, "enumerate")
        assert len(blocks) == 1 and len(blocks[0]) == 3
        assert blocks[0][0].startswith("\\textit{Birinci deneme ilkesinin başlığı.}— Açıklama")
        assert blocks[0][2].endswith("Küçük Harfle Başlamayan satır.")
        assert "*" not in "".join(blocks[0])
        assert "Liste sonrası paragraf" not in "".join(blocks[0])

    def test_numara_ardisik_degilse_ayri_liste(self, convert):
        md = "1. Birinci madde.\n\n5. Beşinci madde.\n\nBir paragraf.\n"
        assert len(_items(convert(md), "enumerate")) == 2

    def test_marker_listesinde_buyuk_harfli_satir_katilmaz(self, convert):
        # Marker: paragraf tek satır; "Fig. …" altyazısı listeye girmemeli.
        md = ("Tek satırlık sentetik paragraf.\n\n"
              "- (1) Birinci madde.\n- (2) İkinci madde metni şurada\n"
              "devam ediyor ve bitiyor.\n- (3) Üçüncü madde.\n"
              "Fig. 1.1 Sentetik altyazı\n\n- Madde\n  Girintili devam satırı.\n\nSon paragraf.\n")
        tex = convert(md)
        blocks = _items(tex, "itemize")
        assert blocks[0][1] == "(2) İkinci madde metni şurada devam ediyor ve bitiyor."
        assert blocks[0][2] == "(3) Üçüncü madde."
        assert "\nFig. 1.1 Sentetik altyazı" in tex
        assert blocks[1] == ["Madde Girintili devam satırı."]

    def test_madde_imli_numarali_liste_enumerate(self, convert):
        md = "- 1. birinci adım;\n- 2. ikinci adım;\n\n- 3. üçüncü adım.\n\nParagraf.\n"
        tex = convert(md)
        assert _items(tex, "enumerate") == [["birinci adım;", "ikinci adım;", "üçüncü adım."]]
        assert "\\begin{itemize}" not in tex

    @pytest.mark.parametrize("md", [
        "- 2. ikinci;\n- 3. üçüncü.\n",        # 1'den başlamıyor
        "- 1. birinci;\n- 3. üçüncü.\n",       # kesintili
        "- 1. birinci;\n- serbest madde.\n",   # karışık
    ])
    def test_numarali_olmayan_madde_imli_liste_aynen(self, convert, md):
        tex = convert(md)
        assert "\\begin{enumerate}" not in tex and "\\begin{itemize}" in tex

    @pytest.mark.parametrize("line,expected", [
        ("- --- birinci durum;", "birinci durum;"),
        ("- — ikinci durum;", "ikinci durum;"),
        ("- $\\bullet$  üçüncü durum,", "üçüncü durum,"),
        ("- • dördüncü durum", "dördüncü durum"),
        ("- —bitişik tire kalır", "—bitişik tire kalır"),
        ("- --verbose seçeneği", "-{}-verbose seçeneği"),
    ])
    def test_cift_madde_imi(self, convert, line, expected):
        items = _items(convert(line + "\n"), "itemize")[0]
        assert items == [expected] or items[0].replace("--", "-{}-") == expected

    def test_madde_sayisi_korunur(self, convert):
        md = ("- 1. a\n- 2. b\n\n- 3. c\n\nParagraf.\n\n1. x\n\n2. y\n\n- --- z\n- w\n")
        tex = convert(md)
        assert tex.count("\\item ") == 7


# ===========================================================================
# R18 — kaynakça ve basılı içindekiler
# ===========================================================================

class TestOnArkaMadde:
    @pytest.mark.parametrize("name", ["Literatur", "Bibliographie", "Bibliografía",
                                      "Bibliografia", "Литература", "References",
                                      "Список литературы"])
    def test_kaynakca_numarasiz(self, convert, name):
        md = ("# Kapitel 1 Anfang\n\n" + _BODY + "## 1.1 Teil\n\n" + _BODY
              + f"# **{name}**\n\n- [1] Sentetik künye.\n")
        tex = convert(md)
        # KARAR 20 (2026-09-29): numarasız kaynakça İçindekiler'de (başlık
        # satırının kuyruğunda \addcontentsline; başlık metni bir kez).
        assert any(re.match(r"\\(chapter|section|subsection)\*\{" + re.escape(name)
                            + r"\}\\addcontentsline\{toc\}", h)
                   for h in _headings(tex)), _headings(tex)
        assert not any(re.match(r"\\(chapter|section|subsection)\{" + re.escape(name), h)
                       for h in _headings(tex))

    def test_ada_gore_normalizasyon(self):
        for name in ("Literatur", "Bibliografía", "Литература", "Kaynakça"):
            assert fold_accents(name).casefold() in BIBLIOGRAPHY_HEADING_NAMES

    def test_numarali_kaynakca_basligi_dokunulmaz(self, convert):
        md = "# Kapitel 1 Anfang\n\n" + _BODY + "## 1.5 References\n\n" + _BODY
        assert "\\section{1.5 References}" in _headings(convert(md))

    def test_capasiz_belgede_bolum_duzeyi_kaynakca(self, convert):
        md = ("# Sentetik Kitap\n\n" + _BODY + "# Einführung\n\n" + _BODY
              + "# Bibliographie\n\n" + _BODY)
        # KARAR 20: numarasız + İçindekiler girdisi (başlık satırının kuyruğu).
        assert any(h.startswith("\\chapter*{Bibliographie}\\addcontentsline{toc}{chapter}")
                   for h in _headings(convert(md)))

    @pytest.mark.parametrize("name", ["Inhaltsverzeichnis", "Оглавление",
                                      "Table des matières", "Índice general", "Indice"])
    def test_basili_icindekiler_tablosu_cikar(self, convert, name):
        md = ("## Yazar Adı\n\n# **Sentetik Kitap**\n\n"
              f"# **{name}**\n\n| Kapitel 1 | Erster Teil | 2 |\n|---|---|---|\n| 1.1 | Zweiter | 3 |\n\n"
              "## Kapitel 1 Erster Teil\n\n" + _BODY + "## 1.1 Zweiter\n\n" + _BODY)
        tex = convert(md)
        assert name not in tex
        assert "\\begin{tabular}" not in tex
        assert "\\tableofcontents" in tex
        assert "\\chapter{Erster Teil}" in tex

    def test_tablosuz_ozet_basligi_kalir(self, convert):
        # "Sommario" İtalyancada özet de demektir: ardında tablo yoksa başlık kalır.
        md = "# Capitolo 1 Prova\n\n" + _BODY + "## Sommario\n\n" + _BODY
        assert "\\section{Sommario}" in _headings(convert(md))

    def test_ingilizce_contents_davranisi_ayni(self, convert):
        md = ("# Book Title\n\n# Contents\n\n| 1 | Intro | 1 |\n|---|---|---|\n\n"
              "# Chapter 1 Intro\n\n" + _BODY)
        tex = convert(md)
        assert "Contents" not in tex.replace("tableofcontents", "")
        assert "\\chapter{Intro}" in tex


# ===========================================================================
# 2026-09-28 C1 devam: R18 içindekiler devam sayfaları, R8b alt ad işaretinin
# tek kaynağı ve E1'in gerçek (boş satırsız) biçimi
# ===========================================================================

class TestC1Devam20260928:
    @pytest.mark.parametrize("title,expected", [
        ("viii ◾ Contents", True),
        ("xvi ◾ CONTENTS", True),
        ("Contents | ix", True),
        ("Inhalt IX", True),
        ("3 Indice", True),
        ("vi Table of Contents", True),
        ("Оглавление 5", True),
        ("Overview of Contents", False),
        ("Contents of the Box", False),
        ("Mix Contents", False),        # "mix" = 1009, sayfa numarası değil
        ("12 Rules for Life", False),
    ])
    def test_icindekiler_basligi_tanima(self, title, expected):
        from latex_pipeline.converter import _is_contents_heading
        assert _is_contents_heading(title) is expected

    def test_icindekiler_devam_sayfasi_ust_bilgisi_cikar(self, convert):
        # Basılı içindekilerin 2. sayfası: üst bilgi "viii ◾ Contents" +
        # bayat sayfa numaralı tablo. Eskiden \subsection{viii ◾ Contents} +
        # tablo olarak gövdede kalıyordu.
        md = ("# Sentetik Kitap\n\n# Contents\n\n| Chapter 1 | Alpha | 1 |\n|---|---|---|\n\n"
              "### **viii** ◾ Contents\n\n| 2.1 | Beta | 21 |\n|---|---|---|\n| 2.2 | Gamma | 22 |\n\n"
              "# Chapter 1 Alpha\n\n" + _BODY + "## 1.1 Delta\n\n" + _BODY)
        tex = convert(md)
        assert "Contents" not in tex.replace("tableofcontents", "")
        assert "\\begin{tabular}" not in tex
        assert "\\chapter{Alpha}" in tex and "\\section{1.1 Delta}" in tex

    def test_tablosuz_ust_bilgi_basligi_kalir(self, convert):
        md = "# Chapter 1 Alpha\n\n" + _BODY + "## x ◾ Contents\n\n" + _BODY
        assert any("Contents" in h for h in _headings(convert(md)))

    def test_altad_isareti_tek_kaynak(self):
        # Sözleşme sabiti patterns'ta; converter aynı nesneyi kullanır.
        # epub_parser (E1) kendi kopyasını tutuyorsa değeri aynı olmalıdır
        # (E2'de patterns.ALTAD_MARKER içe aktarımına geçmesi önerildi).
        import latex_pipeline.converter as cm
        import latex_pipeline.patterns as pt
        import parsers.epub_parser as ep
        assert cm.ALTAD_MARKER is pt.ALTAD_MARKER
        yazar = getattr(ep, "SUBTITLE_MARKER", None) or getattr(ep, "ALTAD_MARKER", None)
        assert yazar == pt.ALTAD_MARKER

    def test_altad_bosluksuz_e1_bicimi(self, convert):
        # E1'in yazdığı biçim: işaretli satır başlığın HEMEN altında, gövde
        # de boş satırsız gelebilir.
        md = ("# Deneme Kitabı\n\n" + _BODY
              + f"## CAPÍTULO I\n{ALTAD_MARKER}Primera prueba sintética.\n" + _BODY
              + f"## CAPÍTULO II\n{ALTAD_MARKER}Segunda prueba\n\n" + _BODY)
        tex = convert(md)
        chapters = [h for h in _headings(tex) if h.startswith("\\chapter{")]
        assert chapters == ["\\chapter{Primera prueba sintética}",
                            "\\chapter{Segunda prueba}"], chapters
        assert "kc:altad" not in tex and "<!" not in tex
        assert "CAPÍTULO" not in tex

    def test_konu_ozeti_alt_ad_ilk_konu_ad_olur_ozet_paragraf_kalir(self, convert):
        # Konu özeti bir ad değildir: başlığa bütünüyle katılınca 120-200
        # karakterlik başlık üst bilgi satırını taşırıyordu (dil_es, 657 pt).
        ozet = ("Primer tema sintético. — Segundo tema de prueba. — "
                "Tercer tema inventado.")
        md = ("# Deneme Kitabı\n\n" + _BODY
              + f"## CAPÍTULO I\n{ALTAD_MARKER}{ozet}\n\n" + _BODY
              + f"## CAPÍTULO II\n{ALTAD_MARKER}Segunda prueba\n\n" + _BODY)
        tex = convert(md)
        chapters = [h for h in _headings(tex) if h.startswith("\\chapter{")]
        assert chapters == ["\\chapter{Primer tema sintético}",
                            "\\chapter{Segunda prueba}"], chapters
        # Özet gövdede eksiksiz kalır (özgünde de başlığın altında basılı).
        assert "Tercer tema inventado." in tex
        assert "kc:altad" not in tex

    def test_konu_ozeti_etiketli_basliga_eklenmez(self, convert):
        md = ("# Deneme\n\n" + _BODY + "## I LE PREMIER ESSAI\n\n" + _BODY
              + "## II LE SECOND ESSAI\n"
              + f"{ALTAD_MARKER}Premier sujet. — Second sujet.\n\n" + _BODY)
        tex = convert(md)
        assert "\\chapter{LE SECOND ESSAI}" in _headings(tex), _headings(tex)
        assert "Premier sujet. — Second sujet." in tex

    def test_uzun_tek_parca_alt_ad_basliga_katilmaz(self, convert):
        uzun = "Sentetik ve oldukça uzun bir alt ad cümlesi " * 3   # ~135 karakter
        md = ("# Deneme\n\n" + _BODY + f"## CAPÍTULO III\n{ALTAD_MARKER}{uzun}\n\n" + _BODY)
        tex = convert(md)
        assert not any(uzun.strip()[:40] in h for h in _headings(tex)), _headings(tex)
        assert uzun.strip() in tex and "kc:altad" not in tex

    def test_tireli_ad_ozet_sayilmaz(self, convert):
        # Özet ayracı ". — "dir; noktasız tire adın parçasıdır.
        md = ("# Deneme\n\n" + _BODY
              + f"## CAPÍTULO IV\n{ALTAD_MARKER}La ciencia – una prueba sintética\n\n" + _BODY)
        chapters = [h for h in _headings(convert(md)) if h.startswith("\\chapter{")]
        assert chapters == ["\\chapter{La ciencia – una prueba sintética}"], chapters
