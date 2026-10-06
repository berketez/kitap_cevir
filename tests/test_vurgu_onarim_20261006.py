"""PDF yazı tipinden kalın/italik vurgu onarımı (latex_pipeline.vurgu_onarim).

6 Ekim 2026, T kulvarı: 5 Ekim'de yazılan ama testi olmayan özellik. Olgular
okuma bulgularından (2026-10-05; mt okuma MT-24) gelir; buradaki cümleler
ve PDF'ler SENTETİKTİR (KARAR 26: testlere kitap metni kopyalanmaz).

Sözleşme: ``onar_raporlu(markdown, pdf_path) -> (markdown, VurguRaporu)``.
Metin eklenmez/silinmez; yalnız EKSİK vurgu işareti eklenir:

- düz metin -> ``*x*`` / ``**x**`` / ``***x***``;
- italik içindeki kalın italik koşu -> içine ``**``; kalın içindeki kalın
  italik koşu -> içine ``*``;
- başlık satırında yalnız sona yapışık vurgulu soru (``# Başlık *Soru?*``)
  ve tamamı italik/kalın italik soru başlığı işlenir;
- tablo hücre sınırını, bağlantıyı, kod/matematiği, etiketi kesen ya da
  sözcük ortasındaki koşu atlanır;
- eş aralıklı (kod) ve matematik yazı tipi koşuyu keser; belgenin gövdesi
  baştan sona italikse (GOVDE_ORANI) o boyut kapanır; PDF yoksa Markdown
  aynen döner.

Sentetik PDF: PyMuPDF'in yerleşik yazı tipleri ("helv" Helvetica, "hebo"
Helvetica-Bold, "heit" Helvetica-Oblique, "hebi" Helvetica-BoldOblique,
"cour"/"cobo" Courier, "Symb" Symbol). Eşleme en az 12 normalleştirilmiş
karakterlik benzersiz bağlam ister; sayfalar ayırt edici sözcüklerle kurulur.

Not: ``_bul`` bugün ana oturumda performans için değişiyor; bu yüzden
doğrudan değil, ``onar_raporlu`` üzerinden (benzersiz/belirsiz bağlam)
sınanır.
"""
from __future__ import annotations

import re

import pytest

from latex_pipeline import pdf_metin_onarim as pmo
from latex_pipeline import vurgu_onarim as vo
from latex_pipeline.config import PipelineConfig
from latex_pipeline.converter import MarkdownToLatex

fitz = pytest.importorskip("fitz")

# Düz dolgu satırları: metin katmanı EN_AZ_PDF_METNI'ni (200) aşsın ve
# italik/kalın harf oranı GOVDE_ORANI'nın (0,5) altında kalsın. Markdown'da
# geçmeleri gerekmez (eşleme yalnız koşunun bağlamına bakar).
_DOLGU = [
    [("Plain filler prose keeps the synthetic text layer long enough here.", "helv")],
    [("Another plain sentence about the weather follows for extra length.", "helv")],
    [("A third plain line closes the page with neutral words and numbers 42.", "helv")],
    [("Fourth plain filler mentions gardens, rivers and distant mountains.", "helv")],
]
_PUNTO = 10
_SATIR_ARALIGI = 14
_GOVDE = "Body text of the chapter begins here with plain words.\n"


def _pdf(tmp_path, satirlar, ad="kaynak.pdf", dolgu=True):
    """Tek sayfalık sentetik PDF; satirlar: her satır [(metin, yazı tipi)].

    Bir satırın parçaları aynı taban çizgisinde yan yana dizilir (yazı tipi
    satır ortasında değişir)."""
    doc = fitz.open()
    page = doc.new_page()
    y = 72
    for parcalar in list(satirlar) + (_DOLGU if dolgu else []):
        x = 72
        for metin, font in parcalar:
            page.insert_text((x, y), metin, fontname=font, fontsize=_PUNTO)
            x += fitz.get_text_length(metin, fontname=font, fontsize=_PUNTO)
        y += _SATIR_ARALIGI
    path = tmp_path / ad
    doc.save(str(path))
    doc.close()
    return str(path)


# ---------------------------------------------------------------------------
# Yazı tipi -> vurgu (font_stili) ve sentetik PDF varsayımı
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("font, stil", [
    ("Helvetica", 0),
    ("Helvetica-Bold", 2),
    ("Helvetica-Oblique", 1),
    ("Helvetica-BoldOblique", 3),
    ("Times-Italic", 1),
    ("ABCDEF+MinionPro-It", 1),         # alt küme öneki atılır
    ("ABCDEF+MinionPro-Bold", 2),
    ("ABCDEF+Garamond-BI-Identity-H", 3),
    ("NimbusRoman-BoldItalic", 3),
    ("URWPalladioL-Ital", 1),
    ("CMBX10", 2),
    ("CMTI10", 1),
    ("CMBXTI10", 3),
    ("Garamond-B", 2),                  # "-B" kalın kısaltması
    ("Courier-Bold", -1),               # eş aralıklı: koşuyu keser
    ("CMTT10", -1),
    ("CMMI10", -1),                     # matematik
    ("Symbol", -1),
    ("", -1),                           # adsız yazı tipi
])
def test_font_stili(font, stil):
    assert vo.font_stili(font) == stil


# 2026-10-06: T kulvarının bulduğu hata düzeltildi (vurgu_onarim).
@pytest.mark.parametrize("font, stil", [
    ("MinionPro-BoldIt", 3),
    ("MinionPro-SemiboldIt", 3),
    ("MyriadPro-LightIt", 1),
    ("NimbusRomNo9L-ReguItal", 1),      # pdfTeX "times" paketi (eski TeX Live)
    ("NimbusSanL-BoldItal", 3),
])
def test_font_stili_agirliga_bitisik_italik_eki(font, stil):
    assert vo.font_stili(font) == stil


# 2026-10-06: T kulvarının bulduğu hata düzeltildi (vurgu_onarim).
def test_adobe_adlandirmasinda_altyazi_etiketi_onarilir(tmp_path, monkeypatch):
    """Gerçek akış sentetik PDF'ten kurulur; yazı tipi adları Adobe Pro
    adlarıyla değiştirilir (Minion Pro ile dizilmiş kitabın benzeri)."""
    import copy

    ad_esle = {"Helvetica": "ABCDEF+MinionPro-Regular", "Helvetica-Oblique": "ABCDEF+MinionPro-It",
               "Helvetica-BoldOblique": "ABCDEF+MinionPro-BoldIt"}
    gercek = pmo._pdf_akisi_kur

    def adobe_akisi(yol):
        akis = copy.copy(gercek(yol))
        akis.font = [ad_esle.get(f, f) for f in akis.font]
        return akis

    monkeypatch.setattr(pmo, "_pdf_akisi_kur", adobe_akisi)
    path = _pdf(tmp_path, [[("Figure 1:", "hebi"), (" A harbor crane lifting crates at dawn.", "heit")]])
    md = "*Figure 1: A harbor crane lifting crates at dawn.*\n"
    out, rapor = vo.onar_raporlu(md, path)
    assert out == "***Figure 1:** A harbor crane lifting crates at dawn.*\n", rapor.sayac


def test_sentetik_pdf_yazi_tipleri_beklenen_vurguyu_verir(tmp_path):
    """Testlerin dayandığı varsayım: yerleşik kısa adlar PDF'te tam PostScript
    adıyla görünür ve font_stili onları doğru sınıflar."""
    ornek = [[(f"Sample words set in the {ad} face here", ad)]
             for ad in ("helv", "hebo", "heit", "hebi", "cobo", "Symb")]
    path = _pdf(tmp_path, ornek, dolgu=False)
    doc = fitz.open(path)
    try:
        adlar = {f[3] for f in doc[0].get_fonts()}
        span_fontlari = {s["font"] for b in doc[0].get_text("dict")["blocks"]
                         for ln in b.get("lines", []) for s in ln["spans"]}
    finally:
        doc.close()
    assert {"Helvetica", "Helvetica-Bold", "Helvetica-Oblique",
            "Helvetica-BoldOblique", "Courier-Bold", "Symbol"} <= adlar
    assert {f: vo.font_stili(f) for f in span_fontlari} == {
        "Helvetica": 0, "Helvetica-Bold": 2, "Helvetica-Oblique": 1,
        "Helvetica-BoldOblique": 3, "Courier-Bold": -1, "Symbol": -1}


# ---------------------------------------------------------------------------
# Yardımcılar: _satir_vurgusu, _kosular
# ---------------------------------------------------------------------------

def test_satir_vurgusu_isaretler_ic_ice_ve_gizli_bolgeler():
    satir = "a *bc* **de** ***fg*** `*h*` \\*i\\* snake_case_x ***j** k* _l_"
    vurgu, isaret = vo._satir_vurgusu(satir)

    def v(alt):
        i = satir.index(alt)
        return vurgu[i:i + len(alt)]

    assert v("bc") == [1, 1]
    assert v("de") == [2, 2]
    assert v("fg") == [3, 3]
    assert v("j") == [3] and v(" k") == [1, 1]      # içten dışa: ** önce kapanır
    assert v("l") == [1]                              # sözcük sınırındaki "_"
    # Kod parçası, kaçışlı yıldız ve sözcük içi alt çizgi vurgu açmaz.
    for gizli in ("`*h*`", "\\*i\\*", "snake_case_x"):
        i = satir.index(gizli)
        assert v(gizli) == [0] * len(gizli)
        assert not any(isaret[i:i + len(gizli)])
    i = satir.index("*bc*")
    assert isaret[i] and isaret[i + 3] and not isaret[i + 1]


def test_kosular_yazi_tipi_degisimi_es_aralikli_ve_simge_keser(tmp_path):
    path = _pdf(tmp_path, [
        [("Plain start ", "helv"), ("slanted words", "heit"), (" plain ", "helv"),
         ("heavy", "hebo"), ("CODE", "cobo"), ("words", "hebo"), (" x", "helv")],
        [("The angle ", "heit"), ("a", "Symb"), (" grows", "heit")],
    ])
    akis = pmo._pdf_akisi_kur(path)
    assert akis is not None
    kosular, kapali = vo._kosular(akis)
    assert [(akis.norm[a:b], st) for a, b, st in kosular] == [
        ("slantedwords", 1), ("heavy", 2), ("words", 2), ("Theangle", 1), ("grows", 1)]
    assert kapali == 0


def test_kosular_sayfa_sinirinda_bolunur(tmp_path):
    doc = fitz.open()
    for kalin in ("End of page one carries words in heavy type",
                  "heavy type continues on page two"):
        page = doc.new_page()
        page.insert_text((72, 72), "Plain filler sentence for the page with enough "
                         "letters to count here.", fontname="helv", fontsize=_PUNTO)
        page.insert_text((72, 90), "Second plain filler sentence adds more letters "
                         "to the page body.", fontname="helv", fontsize=_PUNTO)
        page.insert_text((72, 108), kalin, fontname="hebo", fontsize=_PUNTO)
    path = tmp_path / "iki_sayfa.pdf"
    doc.save(str(path))
    doc.close()
    akis = pmo._pdf_akisi_kur(str(path))
    kosular, _ = vo._kosular(akis)
    assert [(akis.norm[a:b], akis.sayfa[akis.kaynak[a]]) for a, b, _ in kosular] == [
        ("Endofpageonecarrieswordsinheavytype", 0), ("heavytypecontinuesonpagetwo", 1)]


# ---------------------------------------------------------------------------
# Olumlu durumlar: eksik vurgu eklenir (+ idempotentlik)
# ---------------------------------------------------------------------------

# (kimlik, PDF satırları, Markdown, beklenen Markdown, rapordaki tür)
_OLUMLU = [
    ("duz_italik",
     [[("The ship carried a cargo of ", "helv"), ("remarkable copper", "heit"),
       (" to the harbor town.", "helv")]],
     "The ship carried a cargo of remarkable copper to the harbor town.\n",
     "The ship carried a cargo of *remarkable copper* to the harbor town.\n",
     "italik"),
    ("duz_kalin",
     [[("Every keeper must log the ", "helv"), ("tide tables", "hebo"),
       (" before sunrise each day.", "helv")]],
     "Every keeper must log the tide tables before sunrise each day.\n",
     "Every keeper must log the **tide tables** before sunrise each day.\n",
     "kalin"),
    ("duz_kalin_italik",
     [[("The captain shouted the word ", "helv"), ("starboard", "hebi"),
       (" three times in a row.", "helv")]],
     "The captain shouted the word starboard three times in a row.\n",
     "The captain shouted the word ***starboard*** three times in a row.\n",
     "kalin_italik"),
    ("italik_icinde_kalin_italik",
     [[("As the old saying goes, ", "heit"), ("never", "hebi"),
       (" trust a quiet harbor at dusk.", "heit")]],
     "*As the old saying goes, never trust a quiet harbor at dusk.*\n",
     "*As the old saying goes, **never** trust a quiet harbor at dusk.*\n",
     "kalin_italige"),
    ("italik_sonunda_kalin_italik",
     [[("As the old saying goes, never trust a quiet ", "heit"), ("harbor", "hebi")],
      [("Then the plain text resumes on the next line here.", "helv")]],
     "*As the old saying goes, never trust a quiet harbor*\n\n"
     "Then the plain text resumes on the next line here.\n",
     "*As the old saying goes, never trust a quiet **harbor***\n\n"
     "Then the plain text resumes on the next line here.\n",
     "kalin_italige"),
    ("kalin_icinde_kalin_italik",
     [[("Warning: the valve must ", "hebo"), ("stay closed", "hebi"),
       (" during the storm.", "hebo")]],
     "**Warning: the valve must stay closed during the storm.**\n",
     "**Warning: the valve must *stay closed* during the storm.**\n",
     "italik_kalina"),
    # Başlık: yalnız sona yapışık vurgulu soru işlenir; başlık metni (kalın)
    # dokunulmaz.
    ("baslik_sonundaki_italik_soru",
     [[("Chapter Two ", "hebo"), ("What did the sailors find?", "hebi")]],
     "# Chapter Two *What did the sailors find?*\n\n" + _GOVDE,
     "# Chapter Two ***What did the sailors find?***\n\n" + _GOVDE,
     "kalin_italige"),
    ("baslik_tamami_italik_soru",
     [[("Did the crew notice the change?", "hebi")]],
     "#### *Did the crew notice the change?*\n\n" + _GOVDE,
     "#### ***Did the crew notice the change?***\n\n" + _GOVDE,
     "kalin_italige"),
    ("tablo_hucresi_icinde",
     [[("Item  ", "helv"), ("copper ingot", "hebo"), ("  weight seven pounds", "helv")]],
     "| Item | copper ingot | weight seven pounds |\n|---|---|---|\n",
     "| Item | **copper ingot** | weight seven pounds |\n|---|---|---|\n",
     "kalin"),
    # Eş aralıklı yazı tipi koşuyu keser: iki yan ayrı kalın, kod dokunulmaz.
    ("es_aralikli_kosuyu_keser",
     [[("Press the ", "hebo"), ("ENTER", "cobo"), (" key twice", "hebo"),
       (" to continue onward.", "helv")]],
     "Press the `ENTER` key twice to continue onward.\n",
     "**Press the** `ENTER` **key twice** to continue onward.\n",
     "kalin"),
    # Matematik (simge) yazı tipi koşuyu keser; matematik dokunulmaz.
    ("matematik_yazi_tipi_kosuyu_keser",
     [[("In the log, ", "helv"), ("the steep angle ", "heit"), ("a", "Symb"),
       (" grows slowly here", "heit"), (" while the tide turns.", "helv")]],
     "In the log, the steep angle $\\alpha$ grows slowly here while the tide turns.\n",
     "In the log, *the steep angle* $\\alpha$ *grows slowly here* while the tide turns.\n",
     "italik"),
    # Üst simge koşuyu böler; simge vurgulanmaz.
    ("ust_simge_kosuyu_boler",
     [[("Sailors respected ", "helv"), ("the lighthouse keeper", "hebo"), ("7", "hebo"),
       (" above everyone else.", "helv")]],
     "Sailors respected the lighthouse keeper<sup>7</sup> above everyone else.\n",
     "Sailors respected **the lighthouse keeper**<sup>7</sup> above everyone else.\n",
     "kalin"),
    # Boş çapa (Marker'ın sayfa çapası) koşuyu kesmez.
    ("bos_capa_serbest",
     [[("Near the dock ", "helv"), ("the shiny brass bell", "hebo"), (" rang at noon.", "helv")]],
     'Near the dock the shiny <span id="page-3-0"></span>brass bell rang at noon.\n',
     'Near the dock **the shiny <span id="page-3-0"></span>brass bell** rang at noon.\n',
     "kalin"),
    # İki Markdown satırına düşen koşu satır satır vurgulanır (vurgu satır
    # sonunu aşmaz).
    ("satirlara_bolunen_kosu",
     [[("Important safety notice", "hebo")], [("Keep the hatch sealed at all times.", "hebo")],
      [("Plain words follow the bold notice here.", "helv")]],
     "Important safety notice\n\nKeep the hatch sealed at all times.\n\n"
     "Plain words follow the bold notice here.\n",
     "**Important safety notice**\n\n**Keep the hatch sealed at all times.**\n\n"
     "Plain words follow the bold notice here.\n",
     "kalin"),
]


@pytest.mark.parametrize("satirlar, md, beklenen, tur",
                         [c[1:] for c in _OLUMLU], ids=[c[0] for c in _OLUMLU])
def test_eksik_vurgu_eklenir(tmp_path, satirlar, md, beklenen, tur):
    out, rapor = vo.onar_raporlu(md, _pdf(tmp_path, satirlar))
    assert out == beklenen
    assert rapor.sayac.get(tur, 0) >= 1
    # Kayıpsızlık: yalnız vurgu işareti eklenir.
    assert out.replace("*", "") == md.replace("*", "")


@pytest.mark.parametrize("satirlar, md, beklenen, tur",
                         [c[1:] for c in _OLUMLU], ids=[c[0] for c in _OLUMLU])
def test_idempotent_ikinci_kosu_degistirmez(tmp_path, satirlar, md, beklenen, tur):
    path = _pdf(tmp_path, satirlar)
    bir = vo.onar(md, path)
    iki, rapor = vo.onar_raporlu(bir, path)
    assert iki == bir == beklenen
    eklenen = {"italik", "kalin", "kalin_italik", "kalin_italige", "italik_kalina"}
    assert not eklenen & set(rapor.sayac)


# ---------------------------------------------------------------------------
# Olumsuz durumlar: dokunulmaz (+ atlama nedeni raporda)
# ---------------------------------------------------------------------------

# (kimlik, PDF satırları, Markdown, rapordaki atlama nedeni ya da None)
_OLUMSUZ = [
    ("baslikta_vurgusuz_soru",
     [[("Harbor Records ", "hebo"), ("Who kept the ledger?", "heit")]],
     "## Harbor Records Who kept the ledger?\n\n" + _GOVDE, "baslik"),
    ("baslikta_kalin_soru",
     [[("Ledger Notes ", "hebo"), ("Where did the cargo go?", "hebi")]],
     "## Ledger Notes **Where did the cargo go?**\n\n" + _GOVDE, "baslik"),
    ("kalin_baslik_metni",
     [[("The Harbor of Ships and Sails", "hebo")]],
     "## The Harbor of Ships and Sails\n\n" + _GOVDE, "baslik"),
    # Koşu başlığın tamamını kaplıyor: soru vurgusunun içinde değil.
    ("baslik_ve_soruyu_birlikte_kaplayan_kosu",
     [[("Harbor Records Who kept the ledger?", "hebi")]],
     "## Harbor Records *Who kept the ledger?*\n\n" + _GOVDE, "baslik"),
    ("tablo_hucre_sinirini_kesen",
     [[("Item  alpha ", "helv"), ("beta gamma", "hebo"), (" delta here", "helv")]],
     "| Item alpha beta | gamma delta here |\n|---|---|\n", "sinir_kesiyor"),
    ("baglantiyi_kesen",
     [[("See the ", "helv"), ("famous old lighthouse on", "heit"),
       (" the northern cliff.", "helv")]],
     "See the [famous old lighthouse](https://example.org/x) on the northern cliff.\n",
     "sinir_kesiyor"),
    ("etiketi_kesen",
     [[("Near the dock ", "helv"), ("the shiny brass bell", "hebo"), (" rang at noon.", "helv")]],
     "Near the dock the <u>shiny brass</u> bell rang at noon.\n", "sinir_kesiyor"),
    ("kod_parcasini_kesen",
     [[("Always run ", "helv"), ("the harbor tool", "hebo"),
       (" before docking the vessel.", "helv")]],
     "Always run `the harbor` tool before docking the vessel.\n", "sinir_kesiyor"),
    ("matematigi_kesen",
     [[("We measured ", "helv"), ("the value x grows", "hebo"),
       (" across the long winter.", "helv")]],
     "We measured the value $x$ grows across the long winter.\n", None),
    ("sozcuk_ortasi",
     [[("Prices at the ", "helv"), ("Super", "hebo"), ("market rose sharply that week.", "helv")]],
     "Prices at the Supermarket rose sharply that week.\n", "sozcuk_ortasi"),
    # Var olan vurgu kaldırılmaz (PDF düz, Markdown kalın).
    ("var_olan_vurgu_kalir",
     [[("The archive holds seven ledgers from the old port.", "helv")]],
     "The archive holds **seven ledgers** from the old port.\n", None),
    ("celisen_vurgu",
     [[("The archive holds ", "helv"), ("seven ledgers", "hebo"),
       (" from the old port.", "helv")]],
     "The archive holds *seven ledgers* from the old port.\n", "celisen_vurgu"),
    ("karma_vurgu",
     [[("The archive holds ", "helv"), ("the seven old ledgers", "hebo"),
       (" from the old port.", "helv")]],
     "The archive holds the *seven* old ledgers from the old port.\n", "karma_vurgu"),
    # Aynı cümle üç kez; italik olan ikincinin her bağlam penceresi üçüncüde
    # de geçer: belirsiz, dokunulmaz.
    ("belirsiz_baglam",
     [[("The lantern glowed softly in the dark corridor near the old library "
        "entrance at night.", "helv")],
      [("The lantern glowed ", "helv"), ("softly", "heit"),
       (" in the dark corridor near the old library entrance at night.", "helv")],
      [("The lantern glowed softly in the dark corridor near the old library "
        "entrance at night.", "helv")]],
     "The lantern glowed softly in the dark corridor near the old library entrance at night.\n\n"
     "The lantern glowed softly in the dark corridor near the old library entrance at night.\n\n"
     "The lantern glowed softly in the dark corridor near the old library entrance at night.\n",
     "eslesmedi"),
]


@pytest.mark.parametrize("satirlar, md, neden",
                         [c[1:] for c in _OLUMSUZ], ids=[c[0] for c in _OLUMSUZ])
def test_dokunulmayan_durumlar(tmp_path, satirlar, md, neden):
    out, rapor = vo.onar_raporlu(md, _pdf(tmp_path, satirlar))
    assert out == md
    if neden is not None:
        assert rapor.sayac.get(neden, 0) >= 1, rapor.sayac


# 2026-10-06: T kulvarının bulduğu hata düzeltildi (vurgu_onarim).
def test_kod_parcasinin_icine_isaret_eklenmez(tmp_path):
    """PDF'te kod eş aralıklı OLMAYAN kalın yazı tipiyle dizilmiş (ya da
    yazı tipi adı eş aralıklı kalıbına uymuyor: "Monaco-Bold"), Marker onu
    kod parçası yapmış. Kod/matematik sınırı dokunulmaz olmalı."""
    path = _pdf(tmp_path, [[("Always run ", "helv"), ("the harbor tool", "hebo"),
                            (" before docking the vessel.", "helv")]])
    md = "Always run `the harbor tool` before docking the vessel.\n"
    out, _ = vo.onar_raporlu(md, path)
    assert out == md


# ---------------------------------------------------------------------------
# Gövde vurgusu (GOVDE_ORANI) ve PDF yokluğu
# ---------------------------------------------------------------------------

def test_govdesi_italik_belgede_italik_boyutu_kapanir(tmp_path):
    """Gövde baştan sona italikse italik vurgu değil gövde yazı tipidir:
    italik eklenmez; kalın italik koşu yalnız kalın olur."""
    path = _pdf(tmp_path, [
        [("Whole body of this synthetic page is set in an oblique face.", "heit")],
        [("The narrator remembers the ", "heit"), ("granite quay", "hebi"),
         (" where the boats rested.", "heit")],
        [("Then the narrator mentions a ", "heit"), ("plain phrase", "helv"),
         (" amid the slanted prose.", "heit")],
        [("More slanted prose continues for many words to dominate the page.", "heit")],
        [("Even more slanted filler text keeps the italic share above half.", "heit")],
    ], dolgu=False)
    md = ("Whole body of this synthetic page is set in an oblique face.\n\n"
          "The narrator remembers the granite quay where the boats rested.\n\n"
          "Then the narrator mentions a plain phrase amid the slanted prose.\n")
    out, rapor = vo.onar_raporlu(md, path)
    assert out == md.replace("granite quay", "**granite quay**")
    assert rapor.sayac.get("govde_vurgusu_kapali") == 1
    assert "italik" not in rapor.sayac and "kalin_italik" not in rapor.sayac


def _kisa_pdf(tmp_path):
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "remarkable copper", fontname="heit", fontsize=_PUNTO)
    path = tmp_path / "kisa.pdf"
    doc.save(str(path))
    doc.close()
    return str(path)


def _bozuk_pdf(tmp_path):
    path = tmp_path / "bozuk.pdf"
    path.write_bytes(b"%PDF-1.4 bu dosya gecerli bir PDF degil")
    return str(path)


@pytest.mark.parametrize("yol, rapor_bos", [
    (lambda t: None, True),
    (lambda t: "", True),
    (lambda t: str(t / "kitap.epub"), True),       # EPUB yolu: PDF değil
    (lambda t: str(t / "source_book.pdf"), False),  # PDF yolu ama dosya yok
    (_bozuk_pdf, False),
    (_kisa_pdf, False),                             # metin katmanı < 200 karakter
], ids=["none", "bos", "epub", "dosya_yok", "bozuk", "metinsiz"])
def test_pdf_yoksa_markdown_aynen(tmp_path, yol, rapor_bos):
    md = "The ship carried a cargo of remarkable copper to the harbor town.\n"
    out, rapor = vo.onar_raporlu(md, yol(tmp_path))
    assert out == md
    assert rapor.sayac == ({} if rapor_bos else {"pdf_yok": 1})


def test_onar_onar_raporlu_ile_ayni(tmp_path):
    satirlar, md, beklenen = _OLUMLU[0][1:4]
    path = _pdf(tmp_path, satirlar)
    assert vo.onar(md, path) == vo.onar_raporlu(md, path)[0] == beklenen
    assert vo.onar("", path) == ""


# ---------------------------------------------------------------------------
# Dönüştürücü bağlantısı (convert): onarılan vurgu LaTeX'e doğru geçer
# ---------------------------------------------------------------------------

def _conv(tmp_path, md, pdf):
    cfg = PipelineConfig(document_class="book", output_dir=str(tmp_path), input_pdf=pdf)
    conv = MarkdownToLatex(cfg)
    out = conv.convert("# Chapter One Harbor\n\nOpening paragraph of the chapter.\n\n" + md)
    assert "\x00" not in out and "\x02" not in out, "yer tutucu sızdı"
    return out, conv


def test_altyazinin_kalin_italik_etiketi_caption_icinde(tmp_path):
    """Marker italik altyazının kalın italik etiketini düşürür; onarım
    "***Figure 1:** …*" üretir, dönüştürücü bunu altyazı sayar (\\caption*)."""
    pdf = _pdf(tmp_path, [[("Figure 1:", "hebi"), (" A harbor crane lifting crates at dawn.", "heit")]])
    md = ("![](_page_0_Figure_1.jpeg)\n\n*Figure 1: A harbor crane lifting crates at dawn.*\n\n"
          "Next paragraph after the figure with plain words.\n")
    out, conv = _conv(tmp_path, md, pdf)
    assert conv.vurgu_raporu.sayac.get("kalin_italige") == 1
    figs = re.findall(r"\\begin\{figure\}.*?\\end\{figure\}", out, re.S)
    assert len(figs) == 1
    assert ("\\caption*{\\textit{\\textbf{Figure 1:} A harbor crane lifting crates at dawn.}}"
            in figs[0])
    assert "*" not in figs[0].replace("\\caption*", "")
    assert "Next paragraph after the figure" not in figs[0]


def test_baglanti_metninin_tamami_vurguluysa_href_icinde_vurgu(tmp_path):
    """Koşu yalnız bağlantı metnini kaplıyorsa vurgu etiketin içine girer:
    bağlantı vurgunun içinde kalmaz, \\href bozulmaz."""
    pdf = _pdf(tmp_path, [[("See the ", "helv"), ("famous old lighthouse", "heit"),
                           (" on the northern cliff.", "helv")]])
    md = "See the [famous old lighthouse](https://example.org/x) on the northern cliff.\n"
    out, _ = _conv(tmp_path, md, pdf)
    satir = [s for s in out.split("\n") if "lighthouse" in s][0]
    assert ("See the \\href{https://example.org/x}{\\textit{famous old lighthouse}} "
            "on the northern cliff.") == satir
