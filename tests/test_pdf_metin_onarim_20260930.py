"""G1c (30 Eylül 2026): PDF metin katmanıyla Marker simge onarımı.

``latex_pipeline/pdf_metin_onarim.onar(markdown, pdf_path)`` Marker'ın satır
içi simge hatalarını kaynak PDF'in metin katmanına (PyMuPDF rawdict: taban
çizgisi, punto) bakarak düzeltir. Okuma bulguları: MT-2 (10^11 -> "1011",
"km3<br>−2<br>s"), MT-3 (alt simge <sup> yazılmış), MT-4 (taban çizgisindeki
sayı üst simge), MT-5 (vektör oku "~r"), MT-8 (v_e -> "ve"), MT-26 (derece
"◦") ve dipnot işaretinin düz sayı kalması (okuma bulguları, 2026-09-30).

Testler küçük sentetik PDF'ler kurar (gerçek model yok, Marker yok).
"""
from __future__ import annotations

from pathlib import Path

import pytest

fitz = pytest.importorskip("fitz")

from latex_pipeline import pdf_metin_onarim as pmo  # noqa: E402

KOK = Path(__file__).resolve().parent.parent
UNICODE_FONT = Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf")

# Sayfayı doldurmak için benzersiz sözcüklü dolgu (metin katmanı eşiği).
DOLGU = [
    "Quartz lanterns hover beside amber canyons while patient herons wait.",
    "Velvet compasses rarely whistle during saffron thunderstorms at dusk.",
    "Obsidian ferries carry lavender maps toward forgotten harbour towns.",
    "Juniper clocks murmur softly whenever cobalt kites circle the mill.",
]

NORMAL, KUCUK = 11.0, 8.0
ALT, UST = 2.5, -4.0          # taban çizgisi kayması (pt)


def _sn(metin):
    return (metin, NORMAL, 0.0)


def _alt(metin):
    return (metin, KUCUK, ALT)


def _ust(metin):
    return (metin, KUCUK, UST)


def _pdf(tmp_path: Path, satirlar, unicode_font: bool = False,
         ad: str = "kaynak.pdf") -> str:
    """Satır listesinden tek sayfalık PDF kur.

    Her satır (metin, punto, taban_kayması[, renk]) parçalarından oluşur;
    parçalar yan yana, boşluksuz dizilir (boşluk metnin içinde verilir).
    (None, genişlik) parçası x'i geri alır (kesir gibi üst üste yapı için).
    """
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    if unicode_font:
        page.insert_font(fontname="uni", fontfile=str(UNICODE_FONT))
        font = fitz.Font(fontfile=str(UNICODE_FONT))
        fontname = "uni"
    else:
        font = fitz.Font("helv")
        fontname = "helv"
    y = 72.0
    for satir in satirlar:
        x = 56.0
        for parca in satir:
            if parca[0] is None:          # (None, geri): x'i geri al (yığın)
                x -= parca[1]
                continue
            metin, boy, dy = parca[0], parca[1], parca[2]
            renk = parca[3] if len(parca) > 3 else (0, 0, 0)
            page.insert_text((x, y + dy), metin, fontname=fontname,
                             fontsize=boy, color=renk)
            x += font.text_length(metin, fontsize=boy)
        y += 26.0
    for d in DOLGU:
        page.insert_text((56.0, y), d, fontname=fontname, fontsize=NORMAL)
        y += 16.0
    yol = tmp_path / ad
    doc.save(str(yol))
    doc.close()
    return str(yol)


# ---------------------------------------------------------------------------
# Sözleşme: PDF yoksa / metinsizse Markdown aynen
# ---------------------------------------------------------------------------

def test_pdf_yoksa_aynen(tmp_path):
    md = "A higher v<sup>L</sup> gives a more elongated orbit."
    assert pmo.onar(md, None) is md
    assert pmo.onar(md, "") is md
    assert pmo.onar(md, str(tmp_path / "yok.pdf")) is md
    txt = tmp_path / "kaynak.txt"
    txt.write_text("metin", encoding="utf-8")
    assert pmo.onar(md, str(txt)) is md


def test_bozuk_pdf_aynen(tmp_path):
    bozuk = tmp_path / "bozuk.pdf"
    bozuk.write_bytes(b"%PDF-1.4 bozuk degil mi")
    md = "A higher v<sup>L</sup> helps."
    assert pmo.onar(md, str(bozuk)) == md


def test_metin_katmani_olmayan_pdf_aynen(tmp_path):
    """Taranmış PDF (yalnız görüntü/çizim, metin yok): dokunulmaz."""
    doc = fitz.open()
    page = doc.new_page()
    page.draw_rect(fitz.Rect(50, 50, 300, 300), color=(0, 0, 0), fill=(0.5, 0.5, 0.5))
    yol = tmp_path / "taranmis.pdf"
    doc.save(str(yol))
    md = "Then the launch speed v<sup>L</sup> is increased."
    yeni, rapor = pmo.onar_raporlu(md, str(yol))
    assert yeni == md
    assert rapor.pdf_var is False


def test_hata_olursa_aynen(tmp_path, monkeypatch):
    def patla(*_a, **_k):
        raise RuntimeError("beklenmeyen")
    monkeypatch.setattr(pmo, "onar_raporlu", patla)
    md = "x<sup>1</sup>"
    assert pmo.onar(md, str(tmp_path / "a.pdf")) == md


# ---------------------------------------------------------------------------
# Üst / alt simge (MT-3, MT-4, MT-8)
# ---------------------------------------------------------------------------

def test_ust_simge_yazilmis_alt_simge_duzelir(tmp_path):
    pdf = _pdf(tmp_path, [[
        _sn("Each time the launch speed v"), _alt("L"),
        _sn(" is raised, the orbit grows steadily more eccentric."),
    ]])
    md = ("Each time the launch speed v<sup>L</sup> is raised, the orbit grows "
          "steadily more eccentric.")
    yeni, rapor = pmo.onar_raporlu(md, pdf)
    assert "v<sub>L</sub> is raised" in yeni
    assert rapor.sayim() == {"ust_simge_alt_simge": 1}


def test_duz_metne_inmis_alt_simge_sarilir(tmp_path):
    pdf = _pdf(tmp_path, [[
        _sn("In most problems one may simply set m"), _alt("1"),
        _sn(" = M and m"), _alt("2"),
        _sn(" = m, with M far heavier than m here."),
    ]])
    md = ("In most problems one may simply set m1 = M and m2 = m, with M far "
          "heavier than m here.")
    yeni = pmo.onar(md, pdf)
    assert yeni == ("In most problems one may simply set m<sub>1</sub> = M and "
                    "m<sub>2</sub> = m, with M far heavier than m here.")


def test_birlesik_us_sayisi_ayrilir(tmp_path):
    """MT-2: 10^11 Marker'da "1011" olmuştu."""
    pdf = _pdf(tmp_path, [[
        _sn("The gravitational parameter of the Sun is 1.327 x 10"), _ust("11"),
        _sn(" km"), _ust("3"), _sn(" s"), _ust("-2"),
        _sn(" in these units."),
    ]])
    md = ("The gravitational parameter of the Sun is 1.327 x 1011 km3 s-2 "
          "in these units.")
    yeni = pmo.onar(md, pdf)
    assert "1.327 x 10<sup>11</sup> km<sup>3</sup> s<sup>-2</sup> in" in yeni


def test_taban_cizgisindeki_sayi_ust_simgeden_cikar(tmp_path):
    """MT-4: "<sup>100</sup>W" -> "100 W"; gerçek üst simge (m²) kalır."""
    pdf = _pdf(tmp_path, [[
        _sn("A steel ball cooling in moving air with alpha = 100 W/(m"),
        _ust("2"), _sn(" K) has a small Biot number."),
    ]])
    md = ("A steel ball cooling in moving air with alpha = <sup>100</sup>W/"
          "(m<sup>2</sup> K) has a small Biot number.")
    yeni = pmo.onar(md, pdf)
    assert "alpha = 100 W/(m<sup>2</sup> K) has" in yeni


def test_dipnot_isareti_ust_simge_olur(tmp_path):
    """Düz sayı dipnot işareti: "six days 1" -> "six days<sup>1</sup>" (boşluk PDF'te yok)."""
    pdf = _pdf(tmp_path, [[
        _sn("The attackers spent roughly six days"), _ust("1"),
        _sn(" inside the cluster before anyone noticed."),
    ]])
    md = ("The attackers spent roughly six days 1 inside the cluster before "
          "anyone noticed.")
    yeni, rapor = pmo.onar_raporlu(md, pdf)
    assert "six days<sup>1</sup> inside" in yeni
    assert rapor.sayim() == {"dipnot_isareti": 1}


def test_dipnot_tanimi_satir_basinda_korunur(tmp_path):
    """Sayfa altı dipnot tanımı "<sup>2</sup> Metin": PDF'te numara taban
    çizgisinde olsa da etiket kalır (dönüştürücü dipnotu bu biçimden eşler)."""
    pdf = _pdf(tmp_path, [
        [_sn("A body paragraph that ends the page before the notes.")],
        [("2 The review team fixed the time window covered by this analysis.", 9.0, 0.0)],
    ])
    md = ("A body paragraph that ends the page before the notes.\n\n"
          "<sup>2</sup> The review team fixed the time window covered by this analysis.")
    yeni, rapor = pmo.onar_raporlu(md, pdf)
    assert yeni == md
    assert rapor.atlanan.get("dipnot_tanimi_korundu") == 1


def test_vurguyla_bolunmus_alt_simge(tmp_path):
    """MT-3 (D sf 5): "*Tn*+1" -> "*T<sub>n</sub>*<sub>+1</sub>"."""
    pdf = _pdf(tmp_path, [[
        _sn("Here T"), _alt("n+1"),
        _sn(" denotes the temperature of the last surface, and s"), _alt("i"),
        _sn(" is the layer thickness."),
    ]])
    md = ("Here *Tn*+1 denotes the temperature of the last surface, and "
          "*s<sup>i</sup>* is the layer thickness.")
    yeni = pmo.onar(md, pdf)
    assert "Here *T<sub>n</sub>*<sub>+1</sub> denotes" in yeni
    assert "*s<sub>i</sub>* is the layer" in yeni


def test_gorunur_koseli_parantez_simgeye_alinmaz(tmp_path):
    """Norm'da yok sayılan "]" alt simge etiketinin içine girmez."""
    pdf = _pdf(tmp_path, [[
        _sn("Nitrocellulose NC [(C6H7O2-(ONO"), _alt("2"), _sn(")"),
        _alt("3"), _sn("]"), _alt("n"),
        _sn(" in colloidal form serves as the binder."),
    ]])
    md = ("Nitrocellulose NC [(C6H7O2-(ONO2)3]*n* in colloidal form "
          "serves as the binder.")
    yeni = pmo.onar(md, pdf)
    assert "3]</sub>" not in yeni
    assert "]" in yeni and yeni.count("[") == md.count("[")


def test_unicode_ust_simge_ikinci_kez_sarilmaz(tmp_path):
    """Markdown'da "m³" zaten simge: "<sup>³</sup>" çift simge olurdu."""
    pdf = _pdf(tmp_path, [[
        _sn("The cast grain reaches a density of 1655 kg/m"), _ust("3"),
        _sn(". Its burning rate obeys a power law."),
    ]])
    md = ("The cast grain reaches a density of 1655 kg/m³. Its burning rate "
          "obeys a power law.")
    yeni, rapor = pmo.onar_raporlu(md, pdf)
    assert yeni == md
    assert rapor.atlanan.get("zaten_unicode_simge") == 1


def test_duz_ve_etiketli_karma_kosu_birlesir(tmp_path):
    """PDF'te "n0" tek alt simge; Marker "fn<sup>0</sup>" yazmış."""
    pdf = _pdf(tmp_path, [[
        _sn("How quickly the balance wheel swings, f"), _alt("n0"),
        _sn(" in s, depends mostly on how stiff its hairspring is."),
    ]])
    md = ("How quickly the balance wheel swings, fn<sup>0</sup> in s, depends "
          "mostly on how stiff its hairspring is.")
    yeni, rapor = pmo.onar_raporlu(md, pdf)
    assert "swings, f<sub>n0</sub> in s," in yeni
    assert rapor.sayim() == {"karma_alt_simge": 1}


def test_bitisik_simgeden_once_bosluk_kalkar(tmp_path):
    pdf = _pdf(tmp_path, [[
        _sn("A probe circling the larger primary with C = C"), _ust("'"),
        _sn(" can never leave its neighbourhood at all."),
    ]])
    md = ("A probe circling the larger primary with C = C ' can never leave its "
          "neighbourhood at all.")
    yeni = pmo.onar(md, pdf)
    assert "with C = C<sup>'</sup> can never" in yeni


def test_satir_ici_kesir_simge_sayilmaz(tmp_path):
    """Satır içi kesir 2EI/lR²: pay yukarıda, payda aşağıda küçük punto;
    payda alt simge DEĞİLDİR (bir test kitabında "<sub>'R2</sub>" üretiyordu)."""
    genislik = fitz.Font("helv").text_length("2EI", fontsize=KUCUK)
    pdf = _pdf(tmp_path, [[
        _sn("where K is the effective stiffness of the strip, given by "),
        ("2EI", KUCUK, -4.0), (None, genislik), ("lR", KUCUK, 3.0),
        ("2", 6.0, 0.5),
        _sn(", (cf. Eq. 4.7). Equating both expressions confirms it."),
    ]])
    md = ("where K is the effective stiffness of the strip, given by <sup>2</sup>EI "
          "lR<sup>2</sup> , (cf. Eq. 4.7). Equating both expressions confirms it.")
    yeni, rapor = pmo.onar_raporlu(md, pdf)
    assert "<sub>" not in yeni
    assert rapor.atlanan.get("yigili_yapi", 0) >= 1


# ---------------------------------------------------------------------------
# Bağlam eşleme: belirsizse dokunma
# ---------------------------------------------------------------------------

def _satir0(_i):
    return 0


def test_hizala_iki_yanli_benzersiz():
    src = "alphabetagammadeltaXepsilonzetaetathetaiota"
    dst = "qqqalphabetagammadeltaXepsilonzetaetathetaiotaqq"
    a = src.index("X")
    assert pmo._hizala(src, dst, a, a + 1, _satir0, _satir0) == dst.index("X")


def test_hizala_tek_yanli_ayrilik_reddedilir():
    """Bir test kitabının PDF'indeki gizli "x 1" kopyası: sol bağlam tutar, sağ yan
    ayrılır; eskiden gizli kopyadaki taban çizgisi "1"e eşleniyordu."""
    md = "likewisethesecondpartialsalongthex1andx2axesareobtained"
    pdf = "likewisethesecondpartialsalongthex1x1andx2x2axesareobtained"
    a = md.index("x1") + 1
    assert pmo._hizala(md, pdf, a, a + 1, _satir0, _satir0) is None


def test_hizala_tekrar_eden_bagalam_belirsiz():
    blok = "abcx1defghij" * 8
    a = blok.index("x1", 40) + 1
    assert pmo._hizala(blok, blok, a, a + 1, _satir0, _satir0) is None


def test_hizala_satir_sonunda_tek_yanli(tmp_path):
    """Paragraf sonundaki işaret: sol bağlam + iki metinde de satır sonu."""
    src = "theattackwascontainedbythe12" + "Nextparagraphstartshere"
    dst = "theattackwascontainedbythe12" + "Pagefooterdifferentorder"
    s_src = lambda i: 0 if i < 28 else 1  # noqa: E731
    s_dst = lambda i: 0 if i < 28 else 1  # noqa: E731
    a = src.index("12")
    assert pmo._hizala(src, dst, a, a + 2, s_src, s_dst) == a
    # Satır sınırı yoksa tek yanlı pencere kullanılmaz.
    assert pmo._hizala(src, dst, a, a + 2, _satir0, _satir0) is None


def test_gizli_beyaz_metin_yok_sayilir(tmp_path):
    pdf = _pdf(tmp_path, [[
        _sn("Likewise, the second partials over its "),
        ("y 1", 6.0, 0.0, (1, 1, 1)),
        _sn("y"), _alt("1"), _sn(" and y"), _alt("2"),
        _sn("-axes are obtained from filters of this shape"),
    ]])
    md = ("Likewise, the second partials over its y<sub>1</sub> and "
          "y<sub>2</sub>-axes are obtained from filters of this shape")
    assert pmo.onar(md, pdf) == md
    akis = pmo._pdf_akisi_kur(pdf)
    assert "itsy1andy2" in akis.norm


def test_matematik_bolgesine_dokunulmaz(tmp_path):
    pdf = _pdf(tmp_path, [[
        _sn("In most problems one may simply set m"), _alt("1"),
        _sn(" = M and m"), _alt("2"), _sn(" = m, with M far heavier."),
    ]])
    md = "In most problems one may simply set $m_1$ = M and $m^2$ = m, with M far heavier."
    assert pmo.onar(md, pdf) == md


# ---------------------------------------------------------------------------
# Tablo hücresi (MT-2)
# ---------------------------------------------------------------------------

def test_tablo_hucresi_ve_us_sayisi(tmp_path):
    pdf = _pdf(tmp_path, [
        [_sn("Table 1.1: Gravitational parameter mu of two sample bodies.")],
        [_sn("Star 2.654 10"), _ust("11"), _sn(" km"), _ust("3"), _sn(" s"),
         _ust("-2")],
        [_sn("Mars 4.283 10"), _ust("4"), _sn(" km"), _ust("3"), _sn(" s"),
         _ust("-2")],
        [_sn("By convention the free constant C is simply set to zero.")],
    ])
    md = ("Table 1.1: Gravitational parameter mu of two sample bodies.\n\n"
          "| Star  | 2.654 | 1011 | km3<br>-2<br>s |\n"
          "|-------|-------|------|----------------|\n"
          "| Mars  | 4.283 | 104  | km3<br>-2<br>s |\n\n"
          "By convention the free constant C is simply set to zero.")
    yeni, rapor = pmo.onar_raporlu(md, pdf)
    satirlar = yeni.split("\n")
    assert satirlar[2] == ("| Star  | 2.654 | 10<sup>11</sup> | "
                           "km<sup>3</sup> s<sup>-2</sup> |")
    assert "10<sup>4</sup>" in satirlar[4]
    assert "km<sup>3</sup> s<sup>-2</sup>" in satirlar[4]
    assert rapor.sayim()["tablo_hucre_sirasi"] == 2


# ---------------------------------------------------------------------------
# Derece, vektör oku (MT-26, MT-5) — Unicode yazıtipi gerekir
# ---------------------------------------------------------------------------

unicode_gerekli = pytest.mark.skipif(
    not UNICODE_FONT.is_file(), reason="Arial Unicode yok (macOS dışı)")


@unicode_gerekli
def test_derece_isareti(tmp_path):
    pdf = _pdf(tmp_path, [
        [_sn("The conductivity of steel falls between 20 "), ("◦", KUCUK, UST),
         _sn("C and 600 "), ("◦", KUCUK, UST), _sn("C quite noticeably.")],
        [_sn("◦ first item of an unordered list written with a bullet")],
    ], unicode_font=True)
    md = ("The conductivity of steel falls between 20 *◦*C and 600 *◦*C quite "
          "noticeably.\n\n◦ first item of an unordered list written with a bullet")
    yeni = pmo.onar(md, pdf)
    assert "between 20 °C and 600 °C quite" in yeni
    assert yeni.endswith("◦ first item of an unordered list written with a bullet")


@unicode_gerekli
def test_vektor_oku(tmp_path):
    pdf = _pdf(tmp_path, [
        [_sn("The relative position vector r"), ("⃗", NORMAL, -1.0),
         _sn(" points from the primary towards the moving body.")],
        [_sn("An approximate value ~5 is enough for the estimate t here.")],
    ], unicode_font=True)
    md = ("The relative position vector ~r points from the primary towards the "
          "moving body.\n\nAn approximate value ~5 is enough for the estimate "
          "t~ here.")
    yeni = pmo.onar(md, pdf)
    assert "position vector $\\vec{r}$ points" in yeni
    # PDF'te ok olmayan "t~" ve rakamlı "~5" olduğu gibi kalır.
    assert "estimate t~ here" in yeni and "value ~5 is" in yeni


# ---------------------------------------------------------------------------
# İtalik tek simge (MT-7 zemini) ve kayıpsızlık ağı
# ---------------------------------------------------------------------------

def test_italik_tek_simge_duzlesir(tmp_path):
    pdf = _pdf(tmp_path, [[_sn("If Bi < 0,1 the temperature inside is uniform.")]])
    md = "If Bi *<* 0*,*1 the temperature inside is uniform."
    assert pmo.onar(md, pdf) == "If Bi < 0,1 the temperature inside is uniform."


def test_kayipsiz_mi():
    assert pmo._kayipsiz_mi("v<sup>L</sup> x", "v<sub>L</sub> x")
    assert pmo._kayipsiz_mi("20 *◦*C", "20 °C")
    assert pmo._kayipsiz_mi("vector ~r here", "vector $\\vec{r}$ here")
    assert pmo._kayipsiz_mi("| km3<br>−2<br>s |", "| km<sup>3</sup> s<sup>−2</sup> |")
    assert not pmo._kayipsiz_mi("m1 = M", "m = M")
    assert not pmo._kayipsiz_mi("a\nb", "a b")
    # Tablo dışında sıra değişmesi kayıp sayılır.
    assert not pmo._kayipsiz_mi("km3 -2 s", "km3 s -2")


def test_kayipsizlik_agi_hepsini_geri_alir(tmp_path, monkeypatch):
    pdf = _pdf(tmp_path, [[
        _sn("Each time the launch speed v"), _alt("L"),
        _sn(" is raised, the orbit grows steadily more eccentric."),
    ]])
    md = ("Each time the launch speed v<sup>L</sup> is raised, the orbit grows "
          "steadily more eccentric.")
    gercek = pmo._simge_duzelt

    def bozuk(metin, pdf_akis, rapor):
        return gercek(metin, pdf_akis, rapor).replace("steadily", "steady")

    monkeypatch.setattr(pmo, "_simge_duzelt", bozuk)
    yeni, rapor = pmo.onar_raporlu(md, pdf)
    assert yeni == md
    assert rapor.atlanan.get("kayipsizlik_denetimi") == 1
    assert rapor.degisiklikler == []


# ---------------------------------------------------------------------------
# Bitişik matematik koruması (30 Eyl, kod denetimi): "$\vec{v}$<sub>⊕</sub>"
# dönüştürücüde "$\vec{v}$${}_{⊕}$" olur; "$$" TeX'te display matematik açar.
# ---------------------------------------------------------------------------

def test_matematige_al_ardisik_simgeyi_katar():
    md = "it is ~v<sub>⊕</sub> and more"
    bas = md.index("~v")
    son, yeni = pmo._matematige_al(md, bas, bas + 2, "\\vec{v}")
    assert yeni == "$\\vec{v}_{⊕}$"
    assert md[son:] == " and more"
    md = "frame F<sub>G</sub><sup>′</sup> = (O)"
    bas = md.index("F<")
    son, yeni = pmo._matematige_al(md, bas, bas + 1, "\\mathcal{F}")
    assert yeni == "$\\mathcal{F}_{G}^{′}$" and md[son:] == " = (O)"


@pytest.mark.parametrize("md, bas", [
    ("see $a$~v here", 7),                 # önünde matematik
    ("see a<sup>2</sup>~v here", 17),      # önünde simge etiketi
    ("see ~v<sub>a b</sub> here", 4),      # katılamayan (boşluklu) simge
    ("see ~v$b$ here", 4),                 # ardında matematik
])
def test_matematige_al_bitisik_matematikte_dokunmaz(md, bas):
    assert md[bas:bas + 2] == "~v"
    assert pmo._matematige_al(md, bas, bas + 2, "\\vec{v}") is None


def test_derece_ust_simge_sarmali_yarim_alinmaz():
    """"<sup>◦C</sup>": yalnız açılışı yutmak yetim "</sup>" bırakırdı."""
    m = pmo._DERECE_MD_RE.search("bei 20 <sup>◦C</sup> gemessen")
    assert m is None or "<sup>" not in m.group(0)
    m = pmo._DERECE_MD_RE.search("bei 20 <sup>◦</sup>C gemessen")
    assert m.group(0) == "<sup>◦</sup>"


@unicode_gerekli
def test_vektor_ardindaki_alt_simge_tek_matematik(tmp_path):
    pdf = _pdf(tmp_path, [
        [_sn("The largest angle between v"), ("⃗", NORMAL, -1.0), _alt("⊕"),
         _sn(" and the hyperbolic excess vector stays modest.")],
    ], unicode_font=True)
    md = ("The largest angle between ~v<sub>⊕</sub> and the hyperbolic excess "
          "vector stays modest.")
    yeni, rapor = pmo.onar_raporlu(md, pdf)
    assert "between $\\vec{v}_{⊕}$ and the" in yeni
    assert "$$" not in yeni
    assert rapor.sayim() == {"vektor_oku": 1}


# ---------------------------------------------------------------------------
# Display matematikte tek harf ikamesi (MT-10)
# ---------------------------------------------------------------------------

def _denklem_pdf(tmp_path, denklem_satiri):
    return _pdf(tmp_path, [
        [_sn("Consider the following relation between the variables of motion")],
        [_sn(denklem_satiri)],
        [_sn("which separates neatly into radial and tangential terms here.")],
    ])


def _denklem_md(latex):
    return ("Consider the following relation between the variables of motion"
            "\n\n$$" + latex + "$$\n\n"
            "which separates neatly into radial and tangential terms here.")


def test_denklem_tek_harf_ikamesi(tmp_path):
    pdf = _denklem_pdf(tmp_path, "a = - b c / d + e")
    md = _denklem_md("a = -\\frac{b c}{m} + e")
    yeni, rapor = pmo.onar_raporlu(md, pdf)
    assert "$$a = -\\frac{b c}{d} + e$$" in yeni
    assert rapor.sayim() == {"denklem_harf": 1}
    # Komut adları (\\frac) ikameye girmez; yalnız değişken harfi değişti.
    assert yeni.count("\\frac") == 1


@pytest.mark.parametrize("pdf_satiri, latex", [
    ("a = - b c / d + e", "a = -\\frac{b c}{m} + x"),        # iki fark
    ("a = - b c / d + e", "a = -\\frac{b c}{\\mathrm{m}} + e"),  # sözcük
    ("a = - b c / d", "a = -\\frac{b}{m}"),                  # harf eksik
])
def test_denklem_belirsizse_dokunulmaz(tmp_path, pdf_satiri, latex):
    pdf = _denklem_pdf(tmp_path, pdf_satiri)
    md = _denklem_md(latex)
    assert pmo.onar(md, pdf) == md


def test_denklem_yunan_varyanti_ikame_sayilmaz():
    """ϑ/θ, ϖ/π gibi Yunan varyantları ve simge yazıtipi: dokunulmaz."""
    tex = "\\vartheta_4 = \\lambda_1 x \\text{ab}"
    harfler = pmo._latex_harfleri(tex, 100)
    assert [h for _, h in harfler] == ["ϑ", "λ", "x", "a", "b"]
    assert [o for o, _ in harfler] == [-1, -1, 100 + tex.index(" x ") + 1, -1, -1]
    for font in ("SymbolMT", "MathematicalPi-One", "AdvPSMP10", "MTSY"):
        assert pmo.GUVENILMEZ_HARF_FONT_RE.search(font)
    assert not pmo.GUVENILMEZ_HARF_FONT_RE.search("CMMI10")
