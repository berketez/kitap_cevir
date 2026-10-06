"""Dönüştürücüde çok dilli bölüm yapısı (Y1, 25 Eylül 2026).

1_cikar ölçümü (25 Eyl): İngilizce dışı kitaplarda çıkarım aşamasındaki yapı
tanıma zayıftı — es "## CAPÍTULO VII" \\section*, fr "### I" \\subsection, it
yazar adı \\chapter, de "# Kapitel 1 Alpen – …" başlık sayfasına gidip kutu
başlıkları \\chapter oluyordu; EPUB'da "## X" + "# X" çift başlık veriyordu.

Kurallar (latex_pipeline/converter.py, _plan_structure):
  Y1a  bölüm etiketi altı dilde (rakam, Roma rakamı, sıra sözcüğü, ters sıra,
       etiket + başlık aynı satırda, yalın etiket); kısım dizisi -> \\part
  Y1b  etiket yoksa ardışık Roma rakamlı başlıklar bölüm dizisidir
  Y1c  çapa varsa ön madde -> ana madde geçişi ilk çapada; kitap/yazar adı
       başlıkları \\chapter olmaz; etiketsiz üst başlıklar \\section'a iner
  Y1d  bitişik çapraz düzey tekrar ("## X" + "# X") tek başlık olur

Tüm başlık ve cümleler SENTETİKTİR (telif: kitaptan düzyazı alınmaz).
"""

from __future__ import annotations

import re

import pytest

from latex_pipeline.config import PipelineConfig
from latex_pipeline.converter import (
    MarkdownToLatex,
    _LABEL_FORWARD_RE,
    _LABEL_REVERSE_RE,
    parse_heading_label,
    parse_roman_heading,
)
from latex_pipeline.patterns import (
    NUMBER_WORDS_PATTERN,
    PART_LABEL_WORDS,
    fold_accents,
    number_word_value,
    roman_to_int,
)
from latex_pipeline.sanitizer import apply_chapter_label_rules

_BODY = "Bu paragraf sentetik gövde metnidir ve bölümün içeriğini temsil eder.\n\n"


def _convert(markdown: str) -> str:
    return MarkdownToLatex(
        PipelineConfig(document_class="book", output_dir="/tmp/x")).convert(markdown)


def _headings(tex: str) -> list[str]:
    return [line for line in tex.split("\n")
            if re.match(r"\\(part|chapter|section|subsection|subsubsection|paragraph)\*?\{",
                        line)]


def _chapters(tex: str) -> list[str]:
    return [h for h in _headings(tex) if h.startswith("\\chapter{")]


# ===========================================================================
# Sözcük sabitleri (patterns.py tek kaynak)
# ===========================================================================

class TestSozcukSabitleri:
    @pytest.mark.parametrize("word, value", [
        ("first", 1), ("Twelfth", 12), ("twenty", 20),
        ("Erstes", 1), ("zweiten", 2), ("siebente", 7), ("achte", 8),
        ("zwanzigstes", 20),
        ("первая", 1), ("ЧЕТВЁРТАЯ", 4), ("четвертая", 4), ("двадцатая", 20),
        ("premier", 1), ("Première", 1), ("deuxième", 2), ("deuxieme", 2),
        ("dix-septième", 17), ("vingtième", 20),
        ("PRIMERO", 1), ("primera", 1), ("séptimo", 7), ("septimo", 7),
        ("décima", 10), ("vigésimo", 20),
        ("primo", 1), ("seconda", 2), ("diciannovesimo", 19), ("ventesima", 20),
    ])
    def test_sayi_sozcukleri(self, word, value):
        assert number_word_value(word) == value

    @pytest.mark.parametrize("word", ["und", "state", "capitulo", "der", ""])
    def test_sayi_olmayan_sozcuk(self, word):
        assert number_word_value(word) == 0

    def test_fold_accents_uzunlugu_korur(self):
        for text in ("CAPÍTULO", "четвёртая", "Première", "Übung"):
            assert len(fold_accents(text)) == len(text)
        assert fold_accents("CAPÍTULO") == "CAPITULO"

    @pytest.mark.parametrize("numeral, value", [
        ("I", 1), ("iv", 4), ("XIV", 14), ("XL", 40), ("CIVIL", 0), ("IIII", 0),
    ])
    def test_roma_rakami(self, numeral, value):
        assert roman_to_int(numeral) == value

    def test_etiket_desenleri_tek_kaynaktan(self):
        assert NUMBER_WORDS_PATTERN in _LABEL_FORWARD_RE.pattern
        assert NUMBER_WORDS_PATTERN in _LABEL_REVERSE_RE.pattern
        assert PART_LABEL_WORDS in _LABEL_FORWARD_RE.pattern


# ===========================================================================
# Y1a — etiket çözümleyicisi
# ===========================================================================

@pytest.mark.parametrize("title, kind, value, rest", [
    ("Chapter 2", "chapter", 2, ""),
    ("CHAPTER II", "chapter", 2, ""),
    ("Chapter Two", "chapter", 2, ""),
    ("Kapitel 1 Alpen – Räume und Bilder", "chapter", 1, "Alpen – Räume und Bilder"),
    ("Zweites Kapitel", "chapter", 2, ""),
    ("3. Kapitel: Die Düse", "chapter", 3, "Die Düse"),
    ("Глава 2. Сопло", "chapter", 2, "Сопло"),
    ("Глава первая", "chapter", 1, ""),
    ("ГЛАВА ЧЕТВЕРТАЯ", "chapter", 4, ""),
    ("CHAPITRE III. Le Club", "chapter", 3, "Le Club"),
    ("Chapitre premier", "chapter", 1, ""),
    ("Chapitre dix", "chapter", 10, ""),
    ("CAPÍTULO PRIMERO", "chapter", 1, ""),
    ("CAPITULO VII", "chapter", 7, ""),
    ("Capítulo IV: La tobera", "chapter", 4, "La tobera"),
    ("Capitolo primo", "chapter", 1, ""),
    ("Capitolo 5 — L'ugello", "chapter", 5, "L'ugello"),
    ("Part I Received Wisdom", "part", 1, "Received Wisdom"),
    ("Teil 2", "part", 2, ""),
    ("Часть вторая", "part", 2, ""),
    ("Deuxième partie", "part", 2, ""),
    ("Primera parte", "part", 1, ""),
    ("Libro primo", "part", 1, ""),
    ("Book II", "part", 2, ""),
])
def test_etiket_cozumlenir(title, kind, value, rest):
    label = parse_heading_label(title)
    assert label is not None, title
    assert (label.kind, label.value, label.title) == (kind, value, rest)


@pytest.mark.parametrize("title", [
    "Kapitel und Vers",
    "Глава государства",
    "Chapter 1.2 Frames",      # alt bölüm göndermesi
    "Chapter 1)",              # ayraçsız devam
    "Capitolo di Marte",       # "DI" Roma rakamı gibi görünür ama 501
    "First Book of Moses",     # İngilizce ters sıra etiket değil
    "1. Book reviews",         # ters sırada rakam yalnız Almancada
    "Book Review",
    "Part of the Problem",
    "Chapterhouse",
])
def test_etiket_olmayan_baslik(title):
    assert parse_heading_label(title) is None


@pytest.mark.parametrize("title, expected", [
    ("I", (1, "", True)),
    ("II.", (2, "", True)),
    ("III — Le retour", (3, "Le retour", True)),
    ("IV. Titre", (4, "Titre", True)),
    ("V LE TITRE", (5, "LE TITRE", False)),
])
def test_roma_basligi(title, expected):
    assert parse_roman_heading(title) == expected


@pytest.mark.parametrize("title", ["ii) durum", "i", "CIVIL WAR", "MIX", "I am here", "Index"])
def test_roma_basligi_olmayan(title):
    assert parse_roman_heading(title) is None


# ===========================================================================
# Y1a — etiket + başlık aynı satırda (altı dil) -> \chapter{başlık}
# ===========================================================================

@pytest.mark.parametrize("heading, title", [
    ("# Kapitel 1 Alpen – Räume und Bilder", "Alpen – Räume und Bilder"),
    ("## CHAPITRE III. Le Club", "Le Club"),
    ("### Глава 2. Сопло", "Сопло"),
    ("## Capítulo IV: La tobera", "La tobera"),
    ("## Capitolo 5 — L'ugello", "L'ugello"),
    ("## Chapter 6 Nozzles", "Nozzles"),
    ("## Zweites Kapitel. Die Düse", "Die Düse"),
])
def test_etiket_ve_baslik_ayni_satirda(heading, title):
    tex = _convert(f"{heading}\n\n{_BODY}## Alt başlık\n\n{_BODY}")
    assert _chapters(tex) == [f"\\chapter{{{title}}}"], _headings(tex)


def test_etiketli_ilk_baslik_baslik_sayfasi_olmaz():
    # de örüntüsü: kitabın ilk başlığı "Kapitel 1 …" — başlık sayfasına
    # gidip bölüm kayboluyordu; ön maddede kalan alt başlıklar yıldızlıydı.
    tex = _convert("# **Kapitel 1 Alpen – Räume**\n\nAutor Eins und Autor Zwei\n\n"
                   + _BODY + "#### Unterabschnitt\n\n" + _BODY)
    assert "\\begin{titlepage}" not in tex
    assert "\\mainmatter" in tex
    assert _chapters(tex) == ["\\chapter{Alpen – Räume}"]
    # bölüm altında yalnız "####" var: düzey boşluğu kapanır (\section)
    assert "\\section{Unterabschnitt}" in _headings(tex)


# ===========================================================================
# Y1a — yalın etiket
# ===========================================================================

@pytest.mark.parametrize("label", [
    "CAPÍTULO PRIMERO", "CAPITULO VII", "Zweites Kapitel", "Глава первая",
    "Chapitre premier", "Capitolo primo", "Chapter Two", "Chapter IX",
])
def test_yalin_ust_duzey_etiket_bolum_olur(label):
    md = f"# Sentetik Kitap\n\n*Yazar Adı*\n\n---\n\n## {label}\n\n{_BODY}### Alt\n\n{_BODY}"
    tex = _convert(md)
    assert _chapters(tex) == [f"\\chapter{{{label}}}"], _headings(tex)
    # sanitizer'ın etiket kuralları bu bölümü silmez (QC her iki tarafa uygular)
    body = tex[tex.index("\\mainmatter"):]
    assert apply_chapter_label_rules(body) == body


def test_iki_yalin_etiket_iki_bolum():
    md = ("# Sentetik Kitap\n\n*Yazar*\n\n---\n\n"
          "## CAPÍTULO PRIMERO\n\n" + _BODY + "## CAPÍTULO VII\n\n" + _BODY
          + "### *a*) PRIMERA PARTE\n\n" + _BODY)
    tex = _convert(md)
    assert _chapters(tex) == ["\\chapter{CAPÍTULO PRIMERO}", "\\chapter{CAPÍTULO VII}"]
    # bölümler "##" düzeyinde tek tip: "###" alt başlık \section olur (Y1c)
    # KARAR 21 (2026-09-29): harfli başlık numarasız, harf metinde kalır.
    assert any(h.startswith("\\section*{\\textit{a}) PRIMERA PARTE}")
               for h in _headings(tex)), _headings(tex)


def test_rakamli_yalin_etiket_eski_davranista_kalir():
    # "\chapter{Kapitel 3}" sanitizer'da sahte bölüm diye silinir
    # (_SPURIOUS_CHAPTER_RE); rakamlı yalın etiket terfi ETMEZ.
    tex = _convert("## Kapitel 3\n\n" + _BODY)
    assert "\\section{Kapitel 3}" in [h.replace("*{", "{") for h in _headings(tex)]
    assert not _chapters(tex)


def test_ic_ice_yalin_etiket_terfi_etmez():
    # Cevap eki örüntüsünün Almancası: etiket gerçek bir bölümün içinde.
    md = ("# Einleitung\n\n" + _BODY + "# Lösungen\n\n" + _BODY
          + "## Zweites Kapitel\n\n### Aufgabe 2.1\n\nDie Antwort ist 5.\n")
    heads = [h.replace("*{", "{") for h in _headings(_convert(md))]
    assert "\\section{Zweites Kapitel}" in heads, heads
    assert "\\subsection{Aufgabe 2.1}" in heads, heads


@pytest.mark.parametrize("label, title", [
    ("Zweites Kapitel", "Die Düse"),
    ("Глава первая", "СОПЛО"),
    ("Chapitre premier", "La tuyère"),
])
def test_ters_ve_sozcuklu_yalin_etiket_basligla_birlesir(label, title):
    heads = _headings(_convert(f"## {label}\n\n## {title}\n\n{_BODY}"))
    assert f"\\chapter{{{title}}}" in heads, heads
    assert not any(label in h for h in heads), heads


def test_etiket_basligi_baska_etiket_olamaz():
    heads = _headings(_convert("## Chapter 2\n\n## Chapter 3 Frames\n\n" + _BODY))
    assert "\\chapter{Frames}" in heads, heads
    assert "\\chapter{Chapter 3 Frames}" not in heads


# ===========================================================================
# Kısım etiketleri -> \part (geçerli dizi)
# ===========================================================================

def test_kisim_dizisi_part_olur():
    md = ("# Sentetik Kitap\n\n*Yazar*\n\n---\n\n"
          "# Teil 1 Grundlagen\n\n" + _BODY + "## Kapitel 1 Anfang\n\n" + _BODY
          + "# Teil 2\n\n" + _BODY + "## Kapitel 2 Ende\n\n" + _BODY)
    heads = _headings(_convert(md))
    assert heads == ["\\part{Grundlagen}", "\\chapter{Anfang}",
                     "\\part{Teil 2}", "\\chapter{Ende}"], heads


def test_tek_kisim_etiketi_part_olmaz():
    # Alıştırmadaki "Part 1: Setup" bir kısım değildir.
    md = "# Lab Guide\n\nIntro.\n\n# Chapter 1 Tools\n\n" + _BODY + "### Part 1: Setup\n\n" + _BODY
    tex = _convert(md)
    assert "\\part" not in tex
    assert "\\subsection{Part 1: Setup}" in _headings(tex)


def test_icindekiler_yankisi_kisim_dizisini_bozmaz():
    # "### Part I X" + tablo (içindekiler girdisi) çapa sayılmaz.
    md = ("# Sentetik Kitap\n\n*Yazar*\n\n---\n\n# Acknowledgments\n\n" + _BODY
          + "### Part I Received\n\n| a | 1 |\n|---|---|\n| b | 2 |\n\n"
          + "# Part I Received\n\n# Chapter 1 One\n\n" + _BODY
          + "# Part II Given\n\n# Chapter 2 Two\n\n" + _BODY)
    heads = _headings(_convert(md))
    assert "\\part{Received}" in heads and "\\part{Given}" in heads, heads
    assert "\\chapter*{Acknowledgments}" in heads, heads


# ===========================================================================
# Y1b — Roma rakamlı bölüm dizisi
# ===========================================================================

# fr örüntüsü (EPUB): kitap adı, yazar, belge başlığı, "### I" + büyük harfli ad.
_MD_FR = (
    "# De la Lune à Mars: voyage direct\n\n*Auteur Fictif*\n\n---\n\n\n"
    "## De la Lune à MarsVoyage DirectparAuteur Fictif\n\n"
    "# De la Lune à Mars\n\nVoyage Direct\n\npar\n\nAuteur Fictif\n\n"
    "### I\n\nLE CLUB\n\n" + _BODY
    + "### II\n\nLA LETTRE DU PRÉSIDENT\n\n" + _BODY
    + "### III\n\n" + _BODY
)


class TestRomaDizisi:
    def test_fr_uc_bolum(self):
        tex = _convert(_MD_FR)
        assert _chapters(tex) == ["\\chapter{LE CLUB}",
                                  "\\chapter{LA LETTRE DU PRÉSIDENT}",
                                  "\\chapter{III}"], _headings(tex)
        # büyük harfli ad başlığa katıldı, gövdede ikinci kez basılmaz
        assert tex.count("LE CLUB") == 1
        # kitap adı başlıkları \chapter değil, ön maddede
        pre = tex[:tex.index("\\mainmatter")]
        assert "\\section*{De la Lune à Mars}" in pre

    def test_ayracli_roma_basliklari(self):
        # İlk başlık Roma rakamlı bölüm: başlık sayfası olmaz.
        md = "## I. Le départ\n\n" + _BODY + "## II — L'arrivée\n\n" + _BODY
        tex = _convert(md)
        assert "\\begin{titlepage}" not in tex
        assert _chapters(tex) == ["\\chapter{Le départ}", "\\chapter{L'arrivée}"]

    def test_zayif_bicim_dizide_kabul(self):
        # Y2 sonrası beklenen biçim: "<h3>I<br/>LE CLUB</h3>" -> "# I LE CLUB"
        md = ("# De la Lune à Mars: voyage direct\n\n*Auteur Fictif*\n\n---\n\n"
              "# De la Lune à Mars Voyage Direct par Auteur Fictif\n\n"
              "# I LE CLUB\n\n" + _BODY + "# II LA LETTRE\n\n" + _BODY)
        tex = _convert(md)
        assert _chapters(tex) == ["\\chapter{LE CLUB}", "\\chapter{LA LETTRE}"], _headings(tex)

    def test_yeniden_baslayan_numaralar_dizi_degil(self):
        md = ("# Essay One\n\n" + _BODY + "## I\n\n" + _BODY + "## II\n\n" + _BODY
              + "# Essay Two\n\n" + _BODY + "## I\n\n" + _BODY + "## II\n\n" + _BODY)
        heads = [h.replace("*{", "{") for h in _headings(_convert(md))]
        assert "\\section{I}" in heads and "\\section{II}" in heads, heads
        assert "\\chapter{I}" not in heads

    def test_ic_ice_dizi_terfi_etmez(self):
        md = ("# Title Page\n\n# Introduction\n\n" + _BODY + "## I\n\n" + _BODY
              + "## II\n\n" + _BODY + "# Methods\n\n" + _BODY)
        heads = [h.replace("*{", "{") for h in _headings(_convert(md))]
        assert "\\section{I}" in heads, heads
        assert "\\chapter{II}" not in heads

    def test_tek_zayif_uye_ve_kucuk_harf_dizi_degil(self):
        md = "## I Am Legend\n\n" + _BODY + "### i) first\n\n" + _BODY + "### ii) second\n\n" + _BODY
        assert not _chapters(_convert(md))

    def test_tek_buyuk_roma_uyesi_dizi_degil(self):
        # Tek başına "## X" (10) bölüm dizisi sayılmaz; yalnız I/II sayılır.
        md = "# Kitap\n\nGiriş.\n\n## X\n\n" + _BODY
        assert not _chapters(_convert(md))

    @pytest.mark.parametrize("title_heading", ["##### IL TITOLO", "# IL TITOLO"])
    def test_ii_den_baslayan_dizi_ilk_bolumu_ustten_alir(self, title_heading):
        # it örüntüsü (Gutenberg): yazar adı h1, yazı adı, gövde, "### II."
        md = ("# La Vita Sintetica\n\n*G. V. Autorelli*\n\n---\n\n\n"
              "## GIOVANNI AUTORELLI\n\n# GIOVANNI AUTORELLI\n\n"
              f"{title_heading}\n\n" + _BODY + "### II.\n\n" + _BODY)
        tex = _convert(md)
        assert _chapters(tex) == ["\\chapter{IL TITOLO}", "\\chapter{II.}"], _headings(tex)
        pre = tex[:tex.index("\\mainmatter")]
        assert "\\section*{GIOVANNI AUTORELLI}" in pre
        assert "\\chapter" not in pre

    def test_etiket_varsa_roma_dizisi_aranmaz(self):
        md = ("## Chapter 1 Alpha\n\n" + _BODY + "### I.\n\n" + _BODY
              + "### II.\n\n" + _BODY)
        tex = _convert(md)
        assert _chapters(tex) == ["\\chapter{Alpha}"]


# ===========================================================================
# Y1c — ön madde, geçiş noktası, etiketsiz üst başlıklar
# ===========================================================================

class TestUstBasliklar:
    def test_kutu_basligi_bolum_olmaz(self):
        # de örüntüsü: "## Exkurs 1.1" + "# <kutu başlığı>" \chapter'a çıkıyordu.
        md = ("# Kapitel 1 Alpen\n\n" + _BODY + "#### Erfahrungen\n\n" + _BODY
              + "## Exkurs 1.1\n\n# Reisen im Jahr 2008\n\n" + _BODY
              + "# Alpen und die Welt\n\n" + _BODY)
        heads = _headings(_convert(md))
        assert _chapters(_convert(md)) == ["\\chapter{Alpen}"], heads
        # 2026-09-28 (kalite dalgası R8, karşılaştırma RAPOR.md §9 madde 8):
        # kutu başlığı artık \section DEĞİL. Etiket ("Exkurs 1.1") ile hemen
        # ardındaki kutu başlığı tek, numarasız \subsubsection* olur; kutu
        # bölüm/alt bölüm numaralamasını kaydırmaz (eski beklenti
        # "\section{Reisen im Jahr 2008}" kutuyu numaralı alt bölüm yapıyordu,
        # sent_ru'da 1.3'ü 1.4'e kaydıran hata buydu).
        assert "\\subsubsection*{Exkurs 1.1 — Reisen im Jahr 2008}" in heads, heads
        assert not any("Reisen" in h and h.startswith(("\\chapter", "\\section"))
                       for h in heads), heads
        assert "\\section{Alpen und die Welt}" in heads

    @pytest.mark.parametrize("name", ["Register", "Index", "Anhang A", "Appendix B: Tables",
                                      "Послесловие", "Glossaire"])
    def test_arka_madde_ve_ek_bolum_duzeyinde_kalir(self, name):
        md = "## Kapitel 1 Anfang\n\n" + _BODY + f"# {name}\n\n" + _BODY
        heads = _headings(_convert(md))
        if name.startswith(("Anhang", "Appendix")):
            # 2026-10-05 (G1a-1, mt okuma MT-23): bölüm düzeyinde
            # kalır ama numarasızdır + İçindekiler'de. Kendi etiketi olan ek
            # ("Anhang A", A'sız tek "Appendix B") numaralı \chapter olunca
            # "Bölüm 2 · Anhang A" çift etiketi çıkıyordu; \appendix ile
            # "Ek A" yalnız adı olan ve A'dan sıralı kitap sonu eklerine
            # verilir (bkz. test_G1a1_g.py::TestEkler).
            assert (f"\\chapter*{{{name}}}\\addcontentsline{{toc}}{{chapter}}"
                    "{\\csname @currentlabelname\\endcsname}") in heads, heads
        else:
            assert f"\\chapter{{{name}}}" in heads

    def test_on_madde_basliklari_numarasiz_gecis_ilk_capada(self):
        md = ("# Sentetik Kitap: Alt Başlık\n\n*Ada Yazar*\n\n---\n\n"
              "## Sentetik Kitap\n\n# Sentetik Kitap\n\n## Ada Yazar\n\n"
              "# Vorwort\n\n" + _BODY + "# Einleitung\n\n" + _BODY
              + "# Erstes Kapitel. Anfang\n\n" + _BODY)
        tex = _convert(md)
        pre, post = tex.split("\\mainmatter", 1)
        assert "\\chapter*{Vorwort}" in pre and "\\chapter*{Einleitung}" in pre
        assert "\\section*{Sentetik Kitap}" in pre
        assert pre.count("Sentetik Kitap}") == 1          # Y1d: tek başlık
        assert "\\section*{Ada Yazar}" in pre
        assert _chapters(post) == ["\\chapter{Anfang}"]

    def test_baslik_sayfasi_tekrari_basilmaz(self):
        # Başlık sayfasındaki adın aynı düzeyde tekrarı eskisi gibi elenir.
        md = ("# Sentetik Kitap\n\n*Yazar*\n\n---\n\n# Sentetik Kitap\n\n" + _BODY
              + "# Chapter 1 Intro\n\n" + _BODY)
        tex = _convert(md)
        assert tex.count("Sentetik Kitap") == 1, _headings(tex)

    def test_ingilizce_etiketli_bolum_kitap_adi_sanilmaz(self):
        # Başlık sayfası olmayan kesitte ilk "# Chapter 4 X" kitap adı
        # sanılıp tüm kitap ön maddede (yıldızlı) kalıyordu.
        md = "## 3.6 Analysis\n\n" + _BODY + "# Chapter 4 Springs\n\n" + _BODY + "## 4.1 Model\n\n" + _BODY
        tex = _convert(md)
        assert "\\mainmatter" in tex
        assert "\\chapter{Springs}" in _headings(tex)
        assert "\\section{4.1 Model}" in _headings(tex)

    def test_duzey_boslugu_kapanir(self):
        # EPUB (Y2 sonrası): bölüm "#", alt başlık "###" -> \section.
        md = ("# Sentetik Kitap\n\n*Yazar*\n\n---\n\n# CAPÍTULO PRIMERO\n\n" + _BODY
              + "### a) OBSERVACIÓN\n\n" + _BODY + "#### Detalle\n\n" + _BODY)
        heads = _headings(_convert(md))
        assert "\\chapter{CAPÍTULO PRIMERO}" in heads
        # KARAR 21: numarasız (düzey aynı: section).
        assert any(h.startswith("\\section*{a) OBSERVACIÓN}") for h in heads), heads
        assert "\\subsection{Detalle}" in heads


# ===========================================================================
# Y1d — bitişik çapraz düzey tekrar
# ===========================================================================

class TestCaprazTekrar:
    def test_epub_cift_basligi_tek_olur(self):
        md = ("# Kitap Adı\n\n*Yazar*\n\n---\n\n## Önsöz\n\n# Önsöz\n\n" + _BODY
              + "## Birinci Kısım Adı\n\n# Birinci Kısım Adı\n\n" + _BODY)
        heads = _headings(_convert(md))
        assert sum("Önsöz" in h for h in heads) == 1, heads
        assert sum("Birinci Kısım Adı" in h for h in heads) == 1, heads
        assert "\\chapter{Birinci Kısım Adı}" in heads

    def test_arada_govde_varsa_tekrar_sayilmaz(self):
        heads = _headings(_convert("# Kitap\n\nIntro.\n\n## Proof\n\nText.\n\n# Proof\n\nText.\n"))
        assert sum("{Proof}" in h for h in heads) == 2, heads


# ===========================================================================
# Plan yokken (doğrudan _try_heading) çok dilli etiket
# ===========================================================================

def test_try_heading_plansiz_cok_dilli_etiket():
    conv = MarkdownToLatex(PipelineConfig())
    conv._seen_headings = set()
    conv._in_frontmatter = False
    conv._frontmatter_ended = True
    conv._book_title = "X"
    assert conv._try_heading("## Kapitel 2 Die Düse") == "\\chapter{Die Düse}"
    assert conv._try_heading("## Глава 3. Сопло") == "\\chapter{Сопло}"


# ===========================================================================
# Güvenlik kuralları: kesintili etiketler, üst bilgi yankısı
# ===========================================================================

def test_kesintili_etiketlerde_etiketsiz_bolumler_korunur():
    # Marker 3. ve 4. bölümün etiketini düşürmüş: 1, 2, 5 -> etiketsiz "#"
    # başlıklar gerçek bölüm olabilir, \section'a indirilmez.
    md = ("# Chapter 1 Alpha\n\n" + _BODY + "# Chapter 2 Beta\n\n" + _BODY
          + "# Gamma\n\n" + _BODY + "# Delta\n\n" + _BODY
          + "# Chapter 5 Epsilon\n\n" + _BODY)
    chapters = _chapters(_convert(md))
    assert "\\chapter{Gamma}" in chapters and "\\chapter{Delta}" in chapters, chapters


def test_ust_bilgi_yankisi_capayi_calmaz():
    # Aynı etiketli başlığın sonraki geçişi (sayfa üst bilgisi) ön madde
    # sınırını kaydırmaz: geçiş ilk (gerçek) bölümde olur.
    md = ("# Sentetik Kitap\n\n*Yazar*\n\n---\n\n# Chapter 1 Alpha\n\n" + _BODY
          + "## Chapter 1 Alpha\n\n" + _BODY + "# Chapter 2 Beta\n\n" + _BODY)
    tex = _convert(md)
    pre = tex[:tex.index("\\mainmatter")]
    assert "Alpha" not in pre
    assert _chapters(tex)[0] == "\\chapter{Alpha}"
