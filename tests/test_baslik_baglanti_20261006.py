"""Dönüştürücü: başlık indirme, dar bağlantı genişletme, üç işaretli altyazı.

6 Ekim 2026, T kulvarı: 5 Ekim'de yazılan ama testi olmayan üç özellik
(latex_pipeline/converter.py). Olgular okuma bulgularından (2026-10-05) ve
Almanca bir kitabın korpus denetiminden gelir; buradaki cümleler,
belirteçler ve PDF'ler SENTETİKTİR (KARAR 26).

(1) ``MarkdownToLatex._demote_non_headings`` — yalnız PDF yolunda ve PDF yer
    iminde (outline) OLMAYAN iki başlık türü paragraf olur: tek boşluksuz
    makine belirteci (en az 12 karakter, "_" "/" ya da "\\" içerir, PDF'te
    başlık adayı değil) ve tamamı italik/kalın italik soru. Kalın soru
    ("**…?**") gerçek başlıktır, KALIR. PDF yoksa (EPUB) dokunulmaz.
(2) ``MarkdownToLatex._widen_narrow_links`` — Marker'ın tek sözcüğe indirdiği
    bağlantı, aynı sayfada aynı hedefe giden PDF bağlantı metnine genişler;
    uçtaki noktalama dışarıda kalır; vurgu/bağlantı/etiket/matematik taşıyan
    sözcük taşınmaz; iki satırlık bağlantının parçaları sonra birleşir.
(3) ``_FIGURE_CAPTION_RE`` / ``_CAPTION_SEP_RE`` baştaki 1–3 vurgu işaretini
    kabul eder: görselin bitişiğindeki "***Figure 1:** metin*" altyazıdır.

Not: ``_widen_narrow_links``/``_link_page`` bugün ana oturumda performans için
değişiyor; bu yüzden genişletme convert() üzerinden (davranış) sınanır.
"""
from __future__ import annotations

import re

import pytest

from latex_pipeline.config import PipelineConfig
from latex_pipeline.converter import (_TOKEN_HEADING_MIN_CHARS, MarkdownToLatex,
                                      _same_link_target, _take_link_words)

fitz = pytest.importorskip("fitz")

_KITAP = "# Chapter One Harbor\n\nThe opening paragraph sets the scene for the harbor story.\n\n"
# Sentetik makine belirteci (gerçek olguyla aynı yapı: harf, "\_", "/", rakam).
_BELIRTEC = r"qqHARBORCLOCK\_OLDSIG/STOP\_T1700000001\_U40404"   # Markdown (kaçışlı)
_BELIRTEC_DUZ = "qqHARBORCLOCK_OLDSIG/STOP_T1700000001_U40404"     # PDF metni
_BASLIK_KOMUTU = r"\\(?:part|chapter|section|subsection|subsubsection|paragraph)\*?\{"
_GOVDE_SATIRI = ("Body line in the regular face sets the reference size.", "helv", 10)
_GOVDE_SATIRI_2 = ("Second body line keeps the regular size dominant here.", "helv", 10)
_PROTOKOL = "Each sailor follows the harbor signal protocol described in the opening chapter."
_DOLGU = "Filler prose sentence number one keeps the page busy with words."


def _pdf(tmp_path, sayfalar=None, toc=None, baglantilar=(), sayfa_sayisi=3,
         ad="kaynak.pdf"):
    """Sentetik PDF.

    sayfalar: {sayfa no: [(metin, yazı tipi, punto)]}; baglantilar: [(sayfa no,
    aranan metin, hedef)] — hedef int ise iç bağlantı (0 tabanlı sayfa; Marker
    "#page-N-0"), str ise adres. toc: doc.set_toc girdisi (yer imleri)."""
    doc = fitz.open()
    for _ in range(sayfa_sayisi):
        doc.new_page()
    for pno, satirlar in (sayfalar or {}).items():
        y = 72
        for metin, font, punto in satirlar:
            doc[pno].insert_text((72, y), metin, fontname=font, fontsize=punto)
            y += punto + 6
    for pno, aranan, hedef in baglantilar:
        page = doc[pno]
        rects = page.search_for(aranan)
        assert rects, f"sentetik PDF'te yok: {aranan}"
        for rect in rects:
            if isinstance(hedef, int):
                page.insert_link({"kind": fitz.LINK_GOTO, "from": rect, "page": hedef,
                                  "to": fitz.Point(0, 0)})
            else:
                page.insert_link({"kind": fitz.LINK_URI, "from": rect, "uri": hedef})
    if toc:
        doc.set_toc(toc)
    path = tmp_path / ad
    doc.save(str(path))
    doc.close()
    return str(path)


def _conv(tmp_path, md, pdf=""):
    cfg = PipelineConfig(document_class="book", output_dir=str(tmp_path), input_pdf=pdf)
    out = MarkdownToLatex(cfg).convert(_KITAP + md)
    assert "\x00" not in out and "\x02" not in out, "yer tutucu sızdı"
    return out


def _baslik_mi(tex: str, metin: str) -> bool:
    return re.search(_BASLIK_KOMUTU + r"[^\n]*" + re.escape(metin), tex) is not None


def _satir(tex: str, parca: str) -> str:
    bulunan = [s for s in tex.split("\n") if parca in s]
    assert len(bulunan) == 1, bulunan
    return bulunan[0]


# ---------------------------------------------------------------------------
# (1) _demote_non_headings: belirteç ve vurgulu soru başlığı
# ---------------------------------------------------------------------------

class TestBaslikIndirme:
    def test_belirtec_basligi_paragraf_olur_metin_aynen(self, tmp_path):
        pdf = _pdf(tmp_path, {0: [_GOVDE_SATIRI]})
        tex = _conv(tmp_path, f"#### {_BELIRTEC}\n\nThe keeper posted this token twice.\n", pdf)
        assert not _baslik_mi(tex, "qqHARBORCLOCK")
        assert _satir(tex, "qqHARBORCLOCK").strip() == _BELIRTEC
        assert "The keeper posted this token twice." in tex

    @pytest.mark.parametrize("baslik, paragraf", [
        ("#### *Did the lanterns fail during the storm?*",
         r"\textit{Did the lanterns fail during the storm?}"),
        ("#### ***Was the harbor ever rebuilt?***",
         r"\textbf{\textit{Was the harbor ever rebuilt?}}"),
    ], ids=["italik", "kalin_italik"])
    def test_vurgulu_soru_basligi_paragraf_olur(self, tmp_path, baslik, paragraf):
        pdf = _pdf(tmp_path, {0: [_GOVDE_SATIRI]})
        tex = _conv(tmp_path, f"{baslik}\n\nAn answer paragraph follows the question.\n", pdf)
        soru = re.sub(r"[*#]", "", baslik).strip()
        assert not _baslik_mi(tex, soru)
        assert _satir(tex, soru).strip() == paragraf

    @pytest.mark.parametrize("baslik, metin", [
        ("### **Why did the old bridge collapse?**", "Why did the old bridge collapse?"),
        # Almanca bir kitaptaki olgunun yapısı: tırnak, uzun tire, soru
        # işareti (5 Eki'de yanlışlıkla iniyordu).
        ("### **Der „lange“ Weg – vom Hafen bis zum Leuchtturm?**", "bis zum Leuchtturm?"),
    ], ids=["ingilizce", "almanca"])
    def test_kalin_soru_basligi_kalir(self, tmp_path, baslik, metin):
        pdf = _pdf(tmp_path, {0: [_GOVDE_SATIRI]})
        tex = _conv(tmp_path, f"{baslik}\n\nAn answer paragraph follows the question.\n", pdf)
        assert _baslik_mi(tex, metin)

    def test_yer_imindeki_baslik_kalir(self, tmp_path):
        pdf = _pdf(tmp_path, {0: [_GOVDE_SATIRI]}, toc=[
            [1, "Chapter One Harbor", 1],
            [2, _BELIRTEC_DUZ, 1],
            [2, "Did the lanterns fail during the storm?", 1],
        ])
        md = (f"#### {_BELIRTEC}\n\nText one.\n\n"
              "#### *Did the lanterns fail during the storm?*\n\nText two.\n\n"
              "#### ***Was the harbor ever rebuilt?***\n\nText three.\n")
        tex = _conv(tmp_path, md, pdf)
        assert _baslik_mi(tex, "qqHARBORCLOCK")
        assert _baslik_mi(tex, "Did the lanterns fail during the storm?")
        # Yer iminde olmayan soru aynı belgede yine iner.
        assert not _baslik_mi(tex, "Was the harbor ever rebuilt?")
        # İşlevin kendisi: convert()'te sonraki adım (_promote_outline_headings)
        # yer imi satırını yeniden başlık yapabildiği için doğrudan sınanır.
        conv = MarkdownToLatex(PipelineConfig(document_class="book",
                                              output_dir=str(tmp_path), input_pdf=pdf))
        satirlar = md.split("\n")
        beklenen = list(satirlar)
        beklenen[8] = "***Was the harbor ever rebuilt?***"
        assert conv._demote_non_headings(list(satirlar)) == beklenen

    @pytest.mark.parametrize("font, punto", [("hebo", 10), ("helv", 16)],
                             ids=["kalin", "buyuk_punto"])
    def test_pdfte_baslik_adayi_olan_belirtec_kalir(self, tmp_path, font, punto):
        pdf = _pdf(tmp_path, {0: [_GOVDE_SATIRI, _GOVDE_SATIRI_2,
                                  (_BELIRTEC_DUZ, font, punto)]})
        tex = _conv(tmp_path, f"#### {_BELIRTEC}\n\nThe keeper posted this token twice.\n", pdf)
        assert _baslik_mi(tex, "qqHARBORCLOCK")

    def test_kosulu_tutmayan_basliklar_kalir(self, tmp_path):
        pdf = _pdf(tmp_path, {0: [_GOVDE_SATIRI]})
        conv = MarkdownToLatex(PipelineConfig(document_class="book",
                                              output_dir=str(tmp_path), input_pdf=pdf))
        # Sınır: kaçışı çözülmüş belirteç _TOKEN_HEADING_MIN_CHARS'tan bir kısa.
        kisa = ("ab_cd/" + "e" * _TOKEN_HEADING_MIN_CHARS)[:_TOKEN_HEADING_MIN_CHARS - 1]
        satirlar = [
            "#### " + kisa.replace("_", "\\_"),   # kısa
            "#### Acknowledgements",             # ayraçsız tek sözcük
            "#### Harbor\\_Logs/Notes and more",  # çok sözcüklü
            "#### *A quiet harbor at dusk*",     # italik ama soru değil
            "#### Did the crew notice?",         # vurgusuz soru
            "#### **Did the crew notice?**",     # kalın soru
        ]
        assert conv._demote_non_headings(list(satirlar)) == satirlar
        tam = ("ab_cd/" + "e" * _TOKEN_HEADING_MIN_CHARS)[:_TOKEN_HEADING_MIN_CHARS]
        assert conv._demote_non_headings(["#### " + tam.replace("_", "\\_")]) == [
            tam.replace("_", "\\_")]

    def test_kod_blogundaki_satirlar_dokunulmaz(self, tmp_path):
        pdf = _pdf(tmp_path, {0: [_GOVDE_SATIRI]})
        conv = MarkdownToLatex(PipelineConfig(document_class="book",
                                              output_dir=str(tmp_path), input_pdf=pdf))
        satirlar = ["```", "#### *Is this a comment?*", f"#### {_BELIRTEC}", "```",
                    "#### *Is this outside?*", f"#### {_BELIRTEC}"]
        assert conv._demote_non_headings(list(satirlar)) == (
            satirlar[:4] + ["*Is this outside?*", _BELIRTEC])

    @pytest.mark.parametrize("girdi", ["", "source_book.pdf", "kitap.epub"],
                             ids=["yol_yok", "pdf_dosyasi_yok", "pdf_degil"])
    def test_pdf_yoksa_dokunulmaz(self, tmp_path, girdi):
        if girdi == "kitap.epub":
            (tmp_path / girdi).write_bytes(b"PK\x03\x04 sentetik")
        yol = str(tmp_path / girdi) if girdi else ""
        md = (f"#### {_BELIRTEC}\n\nText one.\n\n"
              "#### *Did the lanterns fail during the storm?*\n\nText two.\n")
        conv = MarkdownToLatex(PipelineConfig(document_class="book",
                                              output_dir=str(tmp_path), input_pdf=yol))
        satirlar = md.split("\n")
        assert conv._demote_non_headings(list(satirlar)) == satirlar
        tex = _conv(tmp_path, md, yol)
        assert _baslik_mi(tex, "qqHARBORCLOCK")
        assert _baslik_mi(tex, "Did the lanterns fail during the storm?")


# ---------------------------------------------------------------------------
# (2) _widen_narrow_links: dar bağlantı PDF bağlantı metnine genişler
# ---------------------------------------------------------------------------

@pytest.fixture
def protokol_pdf(tmp_path):
    """0. sayfada "harbor signal protocol" öbeği 1. sayfaya bağlantı."""
    return _pdf(tmp_path, {0: [(_PROTOKOL, "helv", 10), (_DOLGU, "helv", 10)]},
                baglantilar=[(0, "harbor signal protocol", 1)])


_GENIS = r"\href{#page-1-0}{harbor signal protocol}"


class TestDarBaglanti:
    @pytest.mark.parametrize("md", [
        "Each sailor follows the [harbor](#page-1-0) signal protocol described in the opening chapter.",
        "Each sailor follows the harbor signal [protocol](#page-1-0) described in the opening chapter.",
        "Each sailor follows the harbor [signal](#page-1-0) protocol described in the opening chapter.",
        "Each sailor follows the [harbor signal](#page-1-0) protocol described in the opening chapter.",
    ], ids=["saga", "sola", "iki_yana", "cok_sozcuklu_dar"])
    def test_dar_baglanti_genisler(self, tmp_path, protokol_pdf, md):
        tex = _conv(tmp_path, md + "\n", protokol_pdf)
        assert _satir(tex, "Each sailor") == (
            "Each sailor follows the " + _GENIS + " described in the opening chapter.")

    @pytest.mark.parametrize("md, beklenen", [
        ("Each sailor follows the [harbor](#page-1-0) signal protocol, described in the opening chapter.",
         "the " + _GENIS + ", described"),
        ("Each sailor follows the (harbor signal [protocol](#page-1-0)) described in the opening chapter.",
         "the (" + _GENIS + ") described"),
        ("Each sailor follows the [harbor](#page-1-0) signal protocol. Described in the opening chapter.",
         "the " + _GENIS + ". Described"),
    ], ids=["virgul", "ayrac", "nokta"])
    def test_uctaki_noktalama_disarida_kalir(self, tmp_path, protokol_pdf, md, beklenen):
        tex = _conv(tmp_path, md + "\n", protokol_pdf)
        assert beklenen in _satir(tex, "Each sailor")

    @pytest.mark.parametrize("md, beklenen", [
        # Vurgu işareti taşıyan sözcük bağlantıya taşınmaz.
        ("Each sailor follows the [harbor](#page-1-0) *signal* protocol described in the opening chapter.",
         r"the \href{#page-1-0}{harbor} \textit{signal} protocol described"),
        # Markdown hedefi PDF bağlantısının hedefinden farklı.
        ("Each sailor follows the [harbor](#page-2-0) signal protocol described in the opening chapter.",
         r"the \href{#page-2-0}{harbor} signal protocol described"),
        # Komşu sözcükler PDF bağlantı metniyle birebir eşleşmiyor.
        ("Each sailor follows the [harbor](#page-1-0) beacon protocol described in the opening chapter.",
         r"the \href{#page-1-0}{harbor} beacon protocol described"),
        # Komşu sözcük başka bir bağlantının içinde.
        ("Each sailor follows the [harbor](#page-1-0) [signal](#page-2-0) protocol described in the opening chapter.",
         r"the \href{#page-1-0}{harbor} \href{#page-2-0}{signal} protocol described"),
    ], ids=["vurgulu_sozcuk", "farkli_hedef", "uyusmayan_sozcuk", "baska_baglanti"])
    def test_genislemeyen_durumlar(self, tmp_path, protokol_pdf, md, beklenen):
        tex = _conv(tmp_path, md + "\n", protokol_pdf)
        assert beklenen in _satir(tex, "Each sailor")

    def test_ayni_baglam_iki_sayfadaysa_genislemez(self, tmp_path):
        pdf = _pdf(tmp_path, {0: [(_PROTOKOL, "helv", 10)], 1: [(_PROTOKOL, "helv", 10)]},
                   baglantilar=[(0, "harbor signal protocol", 2),
                                (1, "harbor signal protocol", 2)])
        md = ("Each sailor follows the [harbor](#page-2-0) signal protocol described in the "
              "opening chapter.\n")
        tex = _conv(tmp_path, md, pdf)
        assert r"the \href{#page-2-0}{harbor} signal protocol described" in _satir(tex, "Each sailor")

    def test_pdf_yoksa_genislemez(self, tmp_path):
        md = ("Each sailor follows the [harbor](#page-1-0) signal protocol described in the "
              "opening chapter.\n")
        for yol in ("", str(tmp_path / "source_book.pdf")):
            tex = _conv(tmp_path, md, yol)
            assert r"the \href{#page-1-0}{harbor} signal protocol" in _satir(tex, "Each sailor")

    def test_iki_satirlik_baglanti_genisleyip_tek_baglanti_olur(self, tmp_path):
        """PDF'te bağlantı iki satıra bölünmüş (iki dikdörtgen, aynı hedef);
        Marker iki dar bağlantı vermiş, arada düz sözcükler. Önce ikisi de
        genişler ("[replace the](#p) [lantern wick](#p)"), sonra bitişik
        parçalar birleşir (_merge_split_links)."""
        pdf = _pdf(tmp_path, {0: [
            ("Before nightfall the crew must please replace the", "helv", 10),
            ("lantern wick on the northern tower without any delay.", "helv", 10),
            (_DOLGU, "helv", 10)]},
            baglantilar=[(0, "replace the", 1), (0, "lantern wick", 1)])
        md = ("Before nightfall the crew must please [replace](#page-1-0) the "
              "[lantern](#page-1-0) wick on the northern tower without any delay.\n")
        tex = _conv(tmp_path, md, pdf)
        assert _satir(tex, "nightfall") == (
            r"Before nightfall the crew must please \href{#page-1-0}{replace the lantern wick} "
            "on the northern tower without any delay.")
        # Birleştirme adımı: genişlemiş iki parça arada yalnız boşlukla.
        assert MarkdownToLatex._merge_split_links(
            "please [replace the](#page-1-0) [lantern wick](#page-1-0) on") == (
            "please [replace the lantern wick](#page-1-0) on")

    def test_adres_hedefli_baglanti_genisler(self, tmp_path):
        pdf = _pdf(tmp_path, {0: [
            ("Read the full maritime safety handbook online before the voyage starts.", "helv", 10),
            (_DOLGU, "helv", 10)]},
            baglantilar=[(0, "full maritime safety handbook", "https://example.org/handbook/")])
        md = ("Read the full maritime [safety](https://example.org/handbook) handbook online "
              "before the voyage starts.\n")
        tex = _conv(tmp_path, md, pdf)
        assert (r"Read the \href{https://example.org/handbook}{full maritime safety handbook} "
                "online") in _satir(tex, "maritime")


@pytest.mark.parametrize("segment, hedef, sondan, beklenen", [
    ("the harbor signal ", "harborsignal", True, len("harbor signal ")),
    ("the (harbor signal ", "harborsignal", True, len("harbor signal ")),   # ayraç dışarıda
    ("“harbor signal ", "harborsignal", True, len("harbor signal ")),
    (" signal protocol, described", "signalprotocol", False, len(" signal protocol")),
    (" signal protocol.” Then", "signalprotocol", False, len(" signal protocol")),
    (" snake_case protocol", "snakecaseprotocol", False, len(" snake_case protocol")),
    (" signal", "", False, 0),                                              # boş hedef
    (" beacon protocol", "signalprotocol", False, None),
    (" signal", "signalprotocol", False, None),                             # eksik kalır
    (" *signal* protocol", "signalprotocol", False, None),                  # vurgu
    (" _signal_ protocol", "signalprotocol", False, None),
    (" $s$ protocol", "sprotocol", False, None),                            # matematik
    (" signal<sup>1</sup> protocol", "signal1protocol", False, None),       # etiket
    (" [signal] protocol", "signalprotocol", False, None),                  # bağlantı
    (" `signal` protocol", "signalprotocol", False, None),                  # kod
])
def test_take_link_words(segment, hedef, sondan, beklenen):
    assert _take_link_words(segment, hedef, from_end=sondan) == beklenen


@pytest.mark.parametrize("pdf_hedefi, md_adresi, ayni", [
    ("#page-3-0", "#page-3-0", True),
    ("#page-3-0", "#page-3-2", True),          # aynı sayfa, başka çapa
    ("#page-3-0", "#page-30-0", False),
    ("#page-3-0", "https://example.org", False),
    ("https://example.org/a/", "https://example.org/a", True),
    ("https://example.org/a", "https://example.org/b", False),
])
def test_same_link_target(pdf_hedefi, md_adresi, ayni):
    assert _same_link_target(pdf_hedefi, md_adresi) is ayni


# ---------------------------------------------------------------------------
# (3) Baştaki üç vurgu işaretli şekil altyazısı
# ---------------------------------------------------------------------------

# 5 Ekim öncesi kalıp ("\*{1,2}") bu altı altyazının hiçbirini tanımıyordu.
_UC_ISARETLI = [
    "***Figure 1:** A harbor crane lifting crates at dawn.*",
    "***Figure 1***: A harbor crane lifting crates at dawn.",
    "***Figure 1*** A harbor crane lifting crates at dawn.",
    "___Figure 2:__ The tide chart for the month of March._",
    "***Abbildung 3:** Ein Kran hebt Kisten im Hafen.*",
    "***Рис. 2.** Кран в порту на рассвете.*",
]


@pytest.mark.parametrize("metin", _UC_ISARETLI)
def test_uc_isaretli_altyazi_taninir(metin):
    assert MarkdownToLatex._is_caption_text(metin)


@pytest.mark.parametrize("metin", [
    "***Figure 4 shows the crane lifting crates.***",
    "*Figure 3.4 shows the layout of the harbor.*",
    "****Figure 5:** Four markers are not a caption prefix.*",
])
def test_atifla_baslayan_vurgulu_cumle_altyazi_degil(metin):
    assert not MarkdownToLatex._is_caption_text(metin)


@pytest.mark.parametrize("altyazi", _UC_ISARETLI[:4])
def test_gorselin_bitisigindeki_uc_isaretli_altyazi_caption_olur(tmp_path, altyazi):
    md = (f"![](_page_1_Figure_1.jpeg)\n\n{altyazi}\n\n"
          "Next paragraph after the figure with plain words.\n")
    tex = _conv(tmp_path, md)
    figs = re.findall(r"\\begin\{figure\}.*?\\end\{figure\}", tex, re.S)
    assert len(figs) == 1
    assert r"\caption*{" in figs[0]
    assert figs[0].index(r"\includegraphics") < figs[0].index(r"\caption*")
    assert "Figure" in figs[0] and "*" not in figs[0].replace(r"\caption*", "")
    disari = re.sub(r"\\begin\{figure\}.*?\\end\{figure\}", "", tex, flags=re.S)
    assert "Figure" not in disari
    assert "Next paragraph after the figure with plain words." in disari
