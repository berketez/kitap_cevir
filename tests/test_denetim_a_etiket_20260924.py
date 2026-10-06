"""Denetim 2026-09-24 (iç denetim notu), kulvar A: Yol A yapı
kuralları.

  A1  Mercek 1 #1 = Mercek 3 #2 — bölüm etiketi kuralının QC paritesi:
      sanitizer çevirideki "\\section{Bölüm N}" etiketini silip/birleştiriyor,
      QC kaynağa (eski body.tex/EXPORT.tex) aynı dönüşümü uygulamadığı için
      kusursuz çeviri sahte "bölüm kaybı" CRITICAL'ı alıyordu (2_derle
      çıkış 3). Kural artık tek kaynaktır: apply_chapter_label_rules.
  A2  Mercek 1 #2 — etiket birleştirmesi fazla genişti: "Kısım I" \\chapter
      yapılıyor, art arda iki etiket birlikte siliniyor, subsection düzeyindeki
      etiket \\chapter'a çıkarılıyor, birleştirmeyle yaratılan chapter ilk
      gerçek bölümü \\chapter* yapıyordu.
  A3  Mercek 3 #1 — B21 İçindekiler sezgisi birim sütunlu veri tablosunu
      (m, mm, l, cm geçerli Roma sayılarıdır) İçindekiler sanıyordu.
  A5  Mercek 1 #8 — "§ N.N." / rakam öneki yıldızlı başlıktan da siliniyor,
      başlık tek numarasını kaybediyordu.

Metinler bu dosya için yazılmış sentetik satırlardır (kitap metni değil).
"""

import re

import pytest

from latex_pipeline.qc_engine import QCEngine
from latex_pipeline.sanitizer import (
    SECTION_NUM_PREFIX_RE,
    LatexSanitizer,
    SanitizeReport,
    _looks_like_toc_table,
    apply_chapter_label_rules,
)


@pytest.fixture
def engine():
    return QCEngine()


def _issues(report, *names):
    return [i for i in report.issues if i.check_name in names]


def _headings(tex: str) -> list[str]:
    return [ln.strip() for ln in tex.split("\n")
            if re.match(r"\s*\\(chapter|section|subsection|subsubsection)\*?\{", ln)]


def _fix_chapters(body: str) -> str:
    return LatexSanitizer()._fix_marker_chapter_artifacts(body, SanitizeReport())


def _sanitize_and_qc(engine, tr: str, en: str, manual: bool):
    out, _ = LatexSanitizer(manual_translation=manual).sanitize(tr)
    return out, engine.analyze(out, en, manual_translation=manual)


_STRUCT_CHECKS = ("information_loss", "section_number_consistency",
                  "number_hallucination")

# ---------------------------------------------------------------------------
# Sentetik kitap parçaları: eski converter çıktısı (etiket section'da kalmış)
# ve onun kusursuz çevirisi.
# ---------------------------------------------------------------------------

# Farklı düzey örüntü: "\section{Chapter 2}" + "\chapter{Başlık}"
_EN_DIFF_LEVEL = r"""\chapter{Wave Motion}

A wave carries energy from one place to another without moving matter along.

\section{Standing Waves}

Two waves travelling in opposite directions produce a standing pattern.

\section{Chapter 2}

\chapter{Sound}

Sound is a longitudinal wave that needs a medium in order to travel.

\section{Loudness}

The loudness of a sound depends on the amplitude of the pressure wave.
"""
_TR_DIFF_LEVEL = r"""\chapter{Dalgaların Hareketi}

Bir dalga, maddeyi taşımadan enerjiyi bir yerden başka bir yere iletir.

\section{Duran Dalgalar}

Zıt yönlerde ilerleyen iki dalga duran bir örüntü oluşturur.

\section{Bölüm 2}

\chapter{Ses}

Ses, yayılmak için bir ortama gereksinim duyan boyuna bir dalgadır.

\section{Ses Yüksekliği}

Bir sesin yüksekliği, basınç dalgasının genliğine bağlıdır.
"""
# Aynı düzey örüntü: "\section{Chapter 2}" + "\section{Başlık}" -> \chapter
_EN_SAME_LEVEL = _EN_DIFF_LEVEL.replace(r"\chapter{Sound}", r"\section{Sound}")
_TR_SAME_LEVEL = _TR_DIFF_LEVEL.replace(r"\chapter{Ses}", r"\section{Ses}")


# ===========================================================================
# A1 — bölüm etiketi kuralının QC paritesi
# ===========================================================================

class TestA1LabelRuleQcParity:
    @pytest.mark.parametrize("en,tr", [
        (_EN_DIFF_LEVEL, _TR_DIFF_LEVEL),
        (_EN_SAME_LEVEL, _TR_SAME_LEVEL),
    ], ids=["farkli_duzey", "ayni_duzey"])
    def test_kusursuz_ceviri_eski_kaynak_skor_100(self, engine, en, tr):
        out, rep = _sanitize_and_qc(engine, tr, en, manual=True)
        assert "Bölüm 2" not in out            # sanitizer etiketi sildi
        assert _issues(rep, *_STRUCT_CHECKS) == []
        assert rep.score == 100.0, [(i.severity, i.message) for i in rep.issues]

    @pytest.mark.parametrize("en,tr", [
        (_EN_DIFF_LEVEL, _TR_DIFF_LEVEL),
        (_EN_SAME_LEVEL, _TR_SAME_LEVEL),
    ], ids=["farkli_duzey", "ayni_duzey"])
    def test_yol_a_ve_yol_b_raporlari_esit(self, engine, en, tr):
        _, rep_a = _sanitize_and_qc(engine, tr, en, manual=True)
        _, rep_b = _sanitize_and_qc(engine, tr, en, manual=False)
        key = lambda r: sorted((i.check_name, i.severity, i.message)  # noqa: E731
                               for i in r.issues)
        assert key(rep_a) == key(rep_b)
        assert rep_a.score == rep_b.score == 100.0

    def test_sahte_chapter_etiketi_de_paritede(self, engine):
        # Marker'ın boş "\chapter{Chapter N}" satırları (adım 1) da kaynakta
        # sayılmaz: bölüm ve "1"/"2" referans sayıları kayıp görünmez.
        en = ("\\chapter{Chapter 1}\n\n" + _EN_DIFF_LEVEL.replace(
            "\\section{Chapter 2}\n", "\\chapter{Chapter 2}\n"))
        tr = ("\\chapter{Bölüm 1}\n\n" + _TR_DIFF_LEVEL.replace(
            "\\section{Bölüm 2}\n", "\\chapter{Bölüm 2}\n"))
        out, rep = _sanitize_and_qc(engine, tr, en, manual=True)
        assert "Bölüm 1" not in out and "Bölüm 2" not in out
        assert _issues(rep, *_STRUCT_CHECKS) == []

    def test_check_dogrudan_cagrida_da_parite(self, engine):
        # QC ham (sanitize edilmemiş) çeviriyle çağrılsa da iki taraf aynı
        # kuraldan geçer.
        rep = engine.analyze(_TR_SAME_LEVEL, _EN_SAME_LEVEL, manual_translation=True)
        assert _issues(rep, *_STRUCT_CHECKS) == []

    def test_gercek_bolum_kaybi_hala_yakalanir(self, engine):
        tr = _TR_DIFF_LEVEL.replace(
            "\\section{Ses Yüksekliği}\n\n", "")
        _, rep = _sanitize_and_qc(engine, tr, _EN_DIFF_LEVEL, manual=True)
        loss = _issues(rep, "information_loss")
        assert len(loss) == 1 and "section count EN=2, TR=1" in loss[0].message

    def test_gercek_referans_kaybi_hala_yakalanir(self, engine):
        en = _EN_DIFF_LEVEL + "\nSee Chapter 5 for the details of resonance.\n"
        tr = _TR_DIFF_LEVEL + "\nRezonansın ayrıntıları için sonraki bölüme bakınız.\n"
        _, rep = _sanitize_and_qc(engine, tr, en, manual=True)
        msgs = [i.message for i in _issues(rep, "number_hallucination")]
        assert any("'5'" in m for m in msgs), msgs

    def test_fonksiyon_sanitizer_ile_ayni_ve_idempotent(self):
        for body in (_TR_DIFF_LEVEL, _TR_SAME_LEVEL):
            once = apply_chapter_label_rules(body)
            assert once == _fix_chapters(body)
            assert apply_chapter_label_rules(once) == once
        assert apply_chapter_label_rules("") == ""

    def test_art_arda_etiketler_chapter_oncesi_tek_geciste_silinir(self):
        body = ("\\chapter{A}\n\nMetin.\n\n\\section{Bölüm 1}\n\n"
                "\\section{Bölüm 2}\n\n\\chapter{B}\n\nMetin.\n")
        once = apply_chapter_label_rules(body)
        assert "Bölüm" not in once
        assert apply_chapter_label_rules(once) == once

    def test_birlesmeden_sonra_ilk_gercek_bolum_yildizlanmaz(self):
        # Tek gerçek chapter + birleşmeyle yaratılan chapter: kısa ilk bölüm
        # "başlık sayfası" sanılıp numarasızlaştırılmamalı.
        out = _fix_chapters(
            "\\chapter{Dalgaların Hareketi}\n\nKısa giriş.\n\n\\section{Bölüm 2}\n\n"
            "\\section{Ses}\n\n\\section{Ses Yüksekliği}\n")
        assert "\\chapter{Dalgaların Hareketi}" in out
        assert "\\chapter{Ses}" in out
        assert "\\chapter*" not in out

    def test_birlesmis_bolumle_baslayan_belgede_sonraki_bolum_yildizlanmaz(self):
        # Belge etiketli (gerçek) bir bölümle başlıyor: başlık sayfası yok.
        # Ne birleşen bölüm ne de ondan sonraki kısa gerçek bölüm \chapter*.
        out = _fix_chapters(
            "\\section{Bölüm 1}\n\n\\section{Giriş}\n\nMetin.\n\n"
            "\\chapter{İkinci Konu}\n\nKısa.\n\n\\chapter{Üçüncü Konu}\n\nMetin.\n")
        assert _headings(out) == [
            "\\chapter{Giriş}", "\\chapter{İkinci Konu}", "\\chapter{Üçüncü Konu}",
        ]

    def test_baslik_sayfasi_sezgisi_calismaya_devam_eder(self):
        # Kaynağın kendi chapter'larıyla (adım 1 sonrası) başlık sayfası
        # örüntüsü: ilk chapter yine \chapter* olur.
        out = _fix_chapters(
            "\\chapter{Dalgalar ve Titreşimler}\n\nYazar Adı\n\n"
            "\\chapter{Bölüm 1}\n\n\\chapter{Dalgaların Hareketi}\n\n"
            "\\section{Duran Dalgalar}\n\nMetin.\n")
        assert "\\chapter*{Dalgalar ve Titreşimler}" in out
        assert "\\chapter{Dalgaların Hareketi}" in out
        assert "Bölüm 1" not in out

    def test_sanitize_idempotent(self):
        san = LatexSanitizer(manual_translation=True)
        for body in (_TR_DIFF_LEVEL, _TR_SAME_LEVEL):
            once, _ = san.sanitize(body)
            twice, _ = san.sanitize(once)
            assert once == twice


# ===========================================================================
# A2 — etiket birleştirmesi daraltıldı
# ===========================================================================

class TestA2LabelMergeScope:
    def test_kisim_basligi_birlestirilmez(self, engine):
        en = ("\\chapter{Introduction}\n\nThis book explains the basic ideas of waves.\n\n"
              "\\section{Part I}\n\n\\section{Basic Concepts}\n\n"
              "A wave carries energy without carrying matter along with it.\n\n"
              "\\section{Historical Notes}\n\n"
              "Early studies of sound were made with strings and pipes.\n")
        tr = ("\\chapter{Giriş}\n\nBu kitap dalgaların temel fikirlerini açıklar.\n\n"
              "\\section{Kısım I}\n\n\\section{Temel Kavramlar}\n\n"
              "Bir dalga, maddeyi taşımadan enerji taşır.\n\n"
              "\\section{Tarihsel Notlar}\n\n"
              "Sesle ilgili ilk çalışmalar teller ve borularla yapıldı.\n")
        out, rep = _sanitize_and_qc(engine, tr, en, manual=True)
        assert _headings(out) == [
            "\\chapter{Giriş}", "\\section{Kısım I}",
            "\\section{Temel Kavramlar}", "\\section{Tarihsel Notlar}",
        ]
        assert _issues(rep, "information_loss") == []

    def test_kisim_etiketi_chapter_oncesi_silinmez(self):
        body = "\\chapter{A}\n\nMetin.\n\n\\section{Kısım II}\n\n\\chapter{B}\n\nMetin.\n"
        assert "\\section{Kısım II}" in _fix_chapters(body)

    def test_cevap_eki_art_arda_etiketler_korunur(self):
        body = ("\\chapter{Giriş}\n\n" + "Uzun giriş metni. " * 150 + "\n\n"
                "\\chapter{Alıştırma Cevapları}\n\n"
                "\\section{Bölüm 1}\n\n\\section{Bölüm 2}\n\n2.1 cevabı: 5.\n\n"
                "\\section{Bölüm 3}\n\n3.1 cevabı: 7.\n")
        out = _fix_chapters(body)
        assert _headings(out) == [
            "\\chapter{Giriş}", "\\chapter{Alıştırma Cevapları}",
            "\\section{Bölüm 1}", "\\section{Bölüm 2}", "\\section{Bölüm 3}",
        ]
        assert "\\chapter{Bölüm" not in out

    def test_etiket_bicimli_baslik_hic_chapter_olmaz(self):
        for second in ("Bölüm 2", "Chapter 2", "Kısım II", "Part 2"):
            body = f"\\chapter{{A}}\n\nMetin.\n\n\\section{{Bölüm 1}}\n\n\\section{{{second}}}\n\nMetin.\n"
            out = _fix_chapters(body)
            assert f"\\section{{{second}}}" in out, (second, out)
            assert "\\section{Bölüm 1}" in out

    def test_subsection_etiketi_chaptera_cikarilmaz(self):
        body = ("\\chapter{Giriş}\n\nMetin.\n\n\\subsection{Chapter 4}\n\n"
                "\\subsection{Özet}\n\nMetin.\n")
        out = _fix_chapters(body)
        assert _headings(out) == [
            "\\chapter{Giriş}", "\\subsection{Chapter 4}", "\\subsection{Özet}",
        ]

    def test_gercek_kitap_ornegi_davranisi_korunur(self):
        # Gerçek bir kitabın EXPORT.tex örüntüsü: farklı düzey (etiket silinir) ve
        # aynı düzey (birleşir) bir arada; ilk gerçek bölüm numaralı kalır.
        body = ("\\chapter{Hareket}\n\n\\section{Giriş}\n\nMetin.\n\n"
                "\\section{Bölüm 2}\n\n\\section{İki Boyutlu Hareket}\n\n"
                "\\section{Referans Çerçeveleri}\n\nMetin.\n\n"
                "\\section{Bölüm 3}\n\n\\chapter{Üç Boyutlu Hareket}\n\nMetin.\n")
        out = _fix_chapters(body)
        assert _headings(out) == [
            "\\chapter{Hareket}", "\\section{Giriş}",
            "\\chapter{İki Boyutlu Hareket}", "\\section{Referans Çerçeveleri}",
            "\\chapter{Üç Boyutlu Hareket}",
        ]


# ===========================================================================
# A3 — B21 İçindekiler sezgisi: birim sütunu sayfa numarası değildir
# ===========================================================================

def _tabular(rows: list[str], spec: str = "l l l") -> str:
    return ("\\begin{tabular}{" + spec + "}\n"
            + "".join(r + " \\\\\n" for r in rows) + "\\end{tabular}")


class TestA3TocHeuristic:
    @pytest.mark.parametrize("units", [
        ("m", "mm", "l", "cm"),
        ("ml", "d", "c", "m"),
        ("cm", "dm", "mm", "mm"),
    ])
    def test_birim_sutunlu_tablo_dokunulmaz(self, units):
        body = _tabular([
            f"Uzunluk & 2.5 & {units[0]}", f"Kalınlık & 12 & {units[1]}",
            f"Hacim & 3.0 & {units[2]}", f"Çap & 4.2 & {units[3]}",
        ])
        assert LatexSanitizer()._fix_toc_tabular_widths(body) == body

    def test_roma_numarali_on_soz_icindekiler_taninir(self):
        body = _tabular([
            " & Önsöz & v", " & Teşekkür & ix", " & Simgeler & xiii",
            "1 & Temel Kavramlar & 1", "2 & Dalga Hareketi & 27",
            "3 & Ses Dalgaları & 58",
        ])
        out = LatexSanitizer()._fix_toc_tabular_widths(body)
        assert out.splitlines()[0] == (
            "\\begin{tabular}{l >{\\raggedright\\arraybackslash}p{70mm} r}")

    def test_birlesik_sayfa_girdisi_taninir(self):
        body = _tabular([
            "9.1 & Giriş & 331", "9.2 & Kararlılık Ölçütleri & 336",
            "9.3 & Sayısal Örnekler & 343 346", "9.4 & Özet & 351",
        ])
        assert "p{70mm}" in LatexSanitizer()._fix_toc_tabular_widths(body)

    def test_yalniz_roma_harfli_son_sutun_toc_sayilmaz(self):
        # i/v/x harfleri de birim/etiket olabilir: en az bir sayfa no rakamla
        # başlamalı.
        body = _tabular(["Birinci sınıf & ölçüm & i", "İkinci sınıf & ölçüm & ii",
                         "Üçüncü sınıf & ölçüm & iii"])
        assert not _looks_like_toc_table(body)
        assert LatexSanitizer()._fix_toc_tabular_widths(body) == body

    def test_azalan_roma_degerleri_toc_sayilmaz(self):
        body = _tabular(["Kuvvet & büyük & xx", "Hız & orta & x",
                         "Kütle & küçük & v", "Zaman & birim & 1"])
        assert not _looks_like_toc_table(body)


# ===========================================================================
# A5 — yıldızlı başlıkta numara öneki metnin parçasıdır
# ===========================================================================

class TestA5StarredHeadingPrefix:
    def test_yildizli_baslik_numarasi_korunur(self):
        san = LatexSanitizer.__new__(LatexSanitizer)
        body = ("\\subsubsection*{§ 1.1. Terminoloji}\n"
                "\\section*{2.3 Ortogonal Vektörler}\n"
                "\\section{§ 1.2. Sınıflandırma}\n"
                "\\subsection{2.4 İzdüşüm}\n")
        out = san._strip_section_number_prefix(body, SanitizeReport())
        assert "\\subsubsection*{§ 1.1. Terminoloji}" in out
        assert "\\section*{2.3 Ortogonal Vektörler}" in out
        assert "\\section{Sınıflandırma}" in out      # yıldızsız: LaTeX numaralar
        assert "\\subsection{İzdüşüm}" in out

    def test_regex_yalniz_yildizsiz(self):
        assert SECTION_NUM_PREFIX_RE.search("\\section{1.1 Giriş}")
        assert not SECTION_NUM_PREFIX_RE.search("\\section*{1.1 Giriş}")
        assert not SECTION_NUM_PREFIX_RE.search("\\subsection*{§ 2.1. Giriş}")

    def test_qc_yildizli_numaralar_karsilastirilir(self, engine):
        en = ("\\section*{7.3 Jupiter and Saturn}\n\nThe two giants dominate the system.\n\n"
              "\\section*{7.4 Free and Forced Elements}\n\nEach element has two parts.\n")
        tr = ("\\section*{7.3 Jüpiter ve Satürn}\n\nİki dev gezegen sisteme egemendir.\n\n"
              "\\section*{7.4 Serbest ve Zorlanmış Elemanlar}\n\nHer elemanın iki bileşeni vardır.\n")
        out, rep = _sanitize_and_qc(engine, tr, en, manual=True)
        assert "\\section*{7.3 Jüpiter ve Satürn}" in out
        assert _issues(rep, "section_number_consistency") == []
        # Çeviride düşen yıldızlı başlık numarası artık görünür.
        lost = tr.replace("{7.4 Serbest", "{Serbest")
        _, rep2 = _sanitize_and_qc(engine, lost, en, manual=True)
        assert _issues(rep2, "section_number_consistency")

    def test_qc_yildizsiz_onek_paritesi_korunur(self, engine):
        en = "\\section{7.3 Jupiter and Saturn}\n\nThe two giants dominate the system.\n"
        tr = "\\section{7.3 Jüpiter ve Satürn}\n\nİki dev gezegen sisteme egemendir.\n"
        out, rep = _sanitize_and_qc(engine, tr, en, manual=True)
        assert "\\section{Jüpiter ve Satürn}" in out
        assert _issues(rep, "section_number_consistency") == []
